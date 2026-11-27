"""Helpers das consultas do MCP e da API v1: erros, parametros, local, precos, envelope e teto de resposta.

Biblioteca padrao + SQLAlchemy + o proprio pacote. Devolve dict; Pydantic nao entra aqui.
"""
import contextlib
import difflib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Mapping

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from mcp_server import db, demo
from mcp_server.brand import BRAND
from mcp_server.timeutil import (
    DEFAULT_COUNTRY,
    age_hours,
    currency_for,
    cutoff_naive,
    iso_local,
    iso_utc,
    now_utc,
)

log = logging.getLogger(__name__)

SUPPORTED_COUNTRIES = ("BR",)
DOCS_URL = BRAND["docs_url"]
JSONLD_CONTEXT_URL = "https://w3id.org/precos/context.jsonld"
ATTRIBUTION = BRAND["attribution"]
MAX_RESPONSE_CHARS = 25000
PLACES_TTL_SECONDS = 600
TIMEOUT_ERRNOS = {3024, 1969}  # ER_QUERY_TIMEOUT (MySQL), ER_STATEMENT_TIMEOUT (MariaDB)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

_places_cache = None  # (id(engine), monotonic, [(store_id, city_norm, state_norm, city_text)])


class QueryError(Exception):
    """Erro de consulta com codigo, dica acionavel e HTTP sugerido."""

    def __init__(self, code, message, hint="", status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint, "docs": DOCS_URL}}


@contextlib.contextmanager
def db_errors():
    """Traduz erro do banco: timeout -> query_timeout (504); qualquer outro -> database_unavailable (503)."""
    try:
        yield
    except QueryError:
        raise
    except SQLAlchemyError as exc:
        if db.mysql_errno(exc) in TIMEOUT_ERRNOS:
            raise QueryError(
                "query_timeout", "The query took longer than the allowed time.",
                "Narrow by city or reduce days.", 504,
            ) from exc
        log.warning("erro de banco na consulta: %s", type(exc).__name__)
        raise QueryError(
            "database_unavailable", "The price database is unavailable right now.",
            "Retry in a few seconds.", 503,
        ) from exc


# ---------- parametros ----------

def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def as_int(value, name, minimum, maximum, default=None):
    if _is_blank(value):
        return default
    if isinstance(value, str):
        text = value.strip()
        number = int(text) if re.fullmatch(r"[+-]?\d+", text) else None
    elif isinstance(value, int) and not isinstance(value, bool):
        number = value
    else:
        number = None
    if number is None or not minimum <= number <= maximum:
        raise QueryError(
            "invalid_parameter", f"Invalid value for {name}.",
            f"Valid range for {name}: {minimum} to {maximum}.", 400,
        )
    return number


def as_bool(value, name, default=False):
    if _is_blank(value):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise QueryError("invalid_parameter", f"Invalid value for {name}.", f"Use true or false for {name}.", 400)


def as_str(value, name, max_length=200, default=None):
    if _is_blank(value):
        return default
    text = str(value).strip()
    if len(text) > max_length:
        raise QueryError(
            "invalid_parameter", f"Invalid value for {name}.",
            f"Use at most {max_length} characters for {name}.", 400,
        )
    return text


def check_country(value):
    code = str(value or DEFAULT_COUNTRY).strip().upper()
    if code not in SUPPORTED_COUNTRIES:
        raise QueryError(
            "invalid_parameter", f"No price data for country {code}.",
            f"Supported countries: {', '.join(SUPPORTED_COUNTRIES)}", 400,
        )
    return code


# ---------- local ----------

def fold(text):
    """Sem acento e sem caixa, com espacos colapsados."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def _store_places(session):
    global _places_cache
    key = id(session.get_bind())
    now = time.monotonic()
    cached = _places_cache
    if cached and cached[0] == key and now - cached[1] < PLACES_TTL_SECONDS:
        return cached[2]
    m = db.load_models()
    rows = session.execute(sa.select(m.Store.id, m.Store.city, m.Store.state)).all()
    places = [(int(r[0]), fold(r[1]), fold(r[2]), r[1] or "") for r in rows]
    _places_cache = (key, now, places)
    return places


def _city_not_found(city, names):
    close = difflib.get_close_matches(fold(city), list(names), n=5, cutoff=0.0)
    suggestions = [names[c] for c in close]
    if suggestions:
        hint = f"Did you mean: {', '.join(suggestions)}? Call list_locations."
    else:
        hint = "Call list_locations to see the supported cities."
    return QueryError("city_not_found", f"City not found: {city}.", hint, 404)


def resolve_location(session, city=None, state=None, country=DEFAULT_COUNTRY):
    """store_ids das lojas do local (cidade e/ou UF, no pais). Cidade inexistente -> city_not_found.

    Sem cidade, devolve todas as lojas da UF/pais; lista vazia se nada bater.
    """
    country = check_country(country)
    locale = db.store_locale(session)
    want_city = fold(city) if not _is_blank(city) else None
    want_state = fold(state) if not _is_blank(state) else None
    ids, names = [], {}
    for store_id, city_norm, state_norm, city_text in _store_places(session):
        if locale.get(store_id, (None, DEFAULT_COUNTRY))[1] != country:
            continue
        if want_state and state_norm != want_state:
            continue
        if city_norm:
            names.setdefault(city_norm, city_text)
        if want_city and city_norm != want_city:
            continue
        ids.append(store_id)
    if want_city and not ids:
        raise _city_not_found(city, names)
    return ids


def normalize_gtin(text):
    """Candidatos de busca por EAN: forma canonica GTIN-13 (como normalize_ean) e, se diferente, a crua.

    Devolve [] se nao houver de 8 a 14 digitos.
    """
    digits = re.sub(r"\D", "", str(text or ""))
    if not 8 <= len(digits) <= 14:
        return []
    canonical = digits.zfill(13) if len(digits) <= 13 else digits
    return [canonical] if canonical == digits else [canonical, digits]


# ---------- saida ----------

def _get(obj, key):
    mapping = getattr(obj, "_mapping", obj)
    if isinstance(mapping, Mapping):
        return mapping.get(key)
    return getattr(obj, key, None)


def store_dict(store, locale):
    """locale = (timezone, country) da loja, como devolvido por db.store_locale."""
    timezone, country = locale
    return {
        "id": int(_get(store, "id")),
        "name": _get(store, "name"),
        "city": _get(store, "city"),
        "state": _get(store, "state"),
        "timezone": timezone,
        "country": country,
        "currency": currency_for(country),
    }


def price_dict(row, tz):
    """Um preco: valor, promocao/replicacao e data no fuso da loja, com idade em horas."""
    price = _get(row, "price")
    regular = _get(row, "price_regular")
    captured = _get(row, "captured_at")
    return {
        "price": round(float(price), 2) if price is not None else None,
        "price_regular": round(float(regular), 2) if regular is not None else None,
        "is_promotion": bool(_get(row, "is_promotion")),
        "is_replicated": bool(_get(row, "is_replicated")),
        "captured_at": iso_local(captured, tz) if captured is not None else None,
        "age_hours": round(age_hours(captured), 2) if captured is not None else None,
    }


def attribution() -> str:
    """Credito da resposta. Na demo, o aviso de dado sintetico vem antes."""
    return f"{demo.ATTRIBUTION_PREFIX} {ATTRIBUTION}" if demo.enabled() else ATTRIBUTION


def envelope(payload, country=DEFAULT_COUNTRY, context=False):
    """Envelope comum; as chaves do envelope prevalecem sobre as do payload."""
    country = check_country(country)
    head = {
        "as_of": iso_utc(now_utc()),
        "country": country,
        "currency": currency_for(country),
        "attribution": attribution(),
    }
    if context:
        head["@context"] = JSONLD_CONTEXT_URL
    out = dict(head)
    for key, value in (payload or {}).items():
        out.setdefault(key, value)
    return out


def _size(obj):
    return len(json.dumps(obj, ensure_ascii=False, default=str))


def fit_response(payload, list_field, max_chars=MAX_RESPONSE_CHARS):
    """Corta a lista principal (busca binaria) ate caber em max_chars e marca truncated=true."""
    out = dict(payload)
    if _size(out) <= max_chars:
        out.setdefault("truncated", False)
        return out
    items = list(out.get(list_field) or [])
    lo, hi, best = 0, len(items), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _size({**out, list_field: items[:mid], "truncated": True}) <= max_chars:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    out[list_field] = items[:best]
    out["truncated"] = True
    return out


# ---------- precos atuais ----------

def fetch_current_prices(session, product_ids, store_ids, max_age_days=None, include_replicated=False):
    """Uma consulta em current_prices (juncao com stores). Devolve (linhas, omitidos).

    Omitidos = linhas descartadas por preco replicado (sem include_replicated) ou mais antigo que max_age_days.
    """
    if not product_ids or not store_ids:
        return [], 0
    m = db.load_models()
    cp, st = m.CurrentPrice, m.Store
    stmt = (
        sa.select(
            cp.product_id, cp.store_id, cp.price, cp.price_regular, cp.is_promotion,
            cp.is_replicated, cp.captured_at, cp.store_url, cp.unit_price_normalized,
            st.name.label("store_name"), st.city.label("store_city"), st.state.label("store_state"),
        )
        .join(st, st.id == cp.store_id)
        .where(cp.product_id.in_(list(product_ids)), cp.store_id.in_(list(store_ids)))
    )
    with db_errors():
        rows = session.execute(stmt).all()
    cutoff = cutoff_naive(max_age_days) if max_age_days else None
    kept, omitted = [], 0
    for row in rows:
        if not include_replicated and row.is_replicated:
            omitted += 1
        elif cutoff is not None and row.captured_at < cutoff:
            omitted += 1
        else:
            kept.append(row)
    return kept, omitted
