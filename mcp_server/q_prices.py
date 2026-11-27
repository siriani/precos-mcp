"""Consultas de preco da camada de leitura: get_prices, get_price_history e get_price_index.

Biblioteca padrao + SQLAlchemy + o proprio pacote. Sem Pydantic: a validacao da entrada
fica aqui (common.as_*) e o esquema vive em schemas.py. Devolve dict com envelope.

Regras do banco real (docs/plano/sprint-03.md): preco atual sempre de current_prices;
historico sempre filtrado por product_id e por data; LIMIT em toda consulta.
"""
import json
import logging
import re
import statistics
from collections import defaultdict
from datetime import timedelta

import sqlalchemy as sa

from catalog.price_index import MIN_SAMPLE, jevons_index, matched_pairs
from mcp_server import db, demo
from mcp_server.brand import BRAND
from mcp_server.auth import _get_redis
from mcp_server.common import (
    QueryError,
    as_bool,
    as_int,
    as_str,
    check_country,
    db_errors,
    envelope,
    fetch_current_prices,
    fold,
    normalize_gtin,
    price_dict,
    resolve_location,
)
from mcp_server.timeutil import DEFAULT_COUNTRY, DEFAULT_TZ, cutoff_naive

log = logging.getLogger(__name__)

PRODUCT_URL = BRAND["site_url"] + "/produto/{slug}"
METHODOLOGY_URL = BRAND["site_url"] + "/metodologia"
INDEX_METHOD = "matched_model_geomean_v0"
INDEX_PREFIX = "precos:index"  # chave do indice pre-calculado (workers/index_job.py)
INDEX_TOP_STORED = 50  # o job guarda o topo 50; a consulta corta no top pedido
PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90}
BOX_DAYS = 7
PRICE_HISTORY_ROW_LIMIT = 200000
UNIT_BASIS = {"kg": "kg", "l": "L", "un": "unit", "unit": "unit"}
_MAX_ID = 2**63 - 1


# ---------- parametros ----------

def _product_param(params):
    """Exatamente um de product_id ou ean (8 a 14 digitos)."""
    product_id = as_int(params.get("product_id"), "product_id", 1, _MAX_ID)
    ean = as_str(params.get("ean"), "ean", 14)
    if (product_id is None) == (ean is None):
        raise QueryError(
            "invalid_parameter", "Send exactly one of product_id or ean.",
            "Use product_id from search_products, or an ean with 8 to 14 digits.", 400,
        )
    if ean is not None and not re.fullmatch(r"\d{8,14}", ean):
        raise QueryError("invalid_parameter", "Invalid value for ean.", "Use 8 to 14 digits for ean.", 400)
    return product_id, ean


def _state_param(value):
    state = as_str(value, "state", 2)
    if state is not None and len(state) != 2:
        raise QueryError("invalid_parameter", "Invalid value for state.", "Use a two-letter state code.", 400)
    return state.upper() if state else None


# ---------- produto e lojas ----------

def _resolve_product(session, product_id, ean):
    """Produto por product_id ou EAN (forma canonica GTIN-13 primeiro). Ausente -> product_not_found."""
    m = db.load_models()
    if product_id is not None:
        product = session.get(m.Product, product_id)
    else:
        candidates = normalize_gtin(ean)
        found = []
        if candidates:
            found = session.execute(
                sa.select(m.Product).where(m.Product.ean.in_(candidates)).limit(len(candidates))
            ).scalars().all()
        product = next((p for p in found if p.ean == candidates[0]), found[0] if found else None)
    if product is None:
        raise QueryError(
            "product_not_found", "Product not found.",
            "Call search_products to get a product_id, or check the ean.", 404,
        )
    return product


def _product_info(product):
    """Campos publicos do produto (sem image_url nem description)."""
    ean = product.ean or None
    return {
        "product_id": int(product.id),
        "ean": ean,
        "ean_verified": ean is not None,
        "name": product.name_canonical,
        "brand": product.brand_canonical,
        "size": product.weight_volume,
        "category": product.category.name if product.category else None,
        "url": PRODUCT_URL.format(slug=product.slug or product.id),
    }


def _store_refs(session, store_ids):
    """{store_id: StoreRef} das lojas pedidas, com fuso e pais da loja."""
    ids = sorted(set(store_ids))
    if not ids:
        return {}
    m = db.load_models()
    locale = db.store_locale(session)
    rows = session.execute(
        sa.select(m.Store.id, m.Store.name, m.Store.unit, m.Store.city, m.Store.state)
        .where(m.Store.id.in_(ids))
        .limit(len(ids))
    ).all()
    refs = {}
    for row in rows:
        store_id = int(row[0])
        timezone, country = locale.get(store_id, (DEFAULT_TZ, DEFAULT_COUNTRY))
        refs[store_id] = {
            "store_id": store_id,
            "chain": row[1],
            "unit": row[2],
            "city": row[3],
            "state": row[4],
            "country": country,
            "timezone": timezone,
        }
    return refs


def _scope_stores(session, city, state, country, chain):
    """store_ids do local (cidade e/ou UF, no pais), filtrados pela rede se chain for dado."""
    store_ids = resolve_location(session, city=city, state=state, country=country)
    if chain:
        refs = _store_refs(session, store_ids)
        want = fold(chain)
        store_ids = [sid for sid in store_ids if fold(refs.get(sid, {}).get("chain")) == want]
    return store_ids


# ---------- get_prices ----------

def get_prices(**params):
    """Preco atual do produto em cada loja do local, do mais barato ao mais caro."""
    country = check_country(params.get("country"))
    context = as_bool(params.get("context"), "context")
    product_id, ean = _product_param(params)
    city = as_str(params.get("city"), "city", 120)
    state = _state_param(params.get("state"))
    chain = as_str(params.get("chain"), "chain", 120)
    max_age_days = as_int(params.get("max_age_days"), "max_age_days", 1, 365, default=30)
    include_replicated = as_bool(params.get("include_replicated"), "include_replicated")
    limit = as_int(params.get("limit"), "limit", 1, 50, default=20)

    with db.session_scope() as session, db_errors():
        product = _resolve_product(session, product_id, ean)
        product_info = _product_info(product)
        unit_basis = UNIT_BASIS.get(str(product.unit_measure or "").lower())
        store_ids = _scope_stores(session, city, state, country, chain)
        # Uma consulta em current_prices: no maximo uma linha por loja (a tabela tem PK produto+loja).
        rows, _ = fetch_current_prices(session, [product.id], store_ids, None, True)
        refs = _store_refs(session, store_ids)

        cutoff = cutoff_naive(max_age_days)
        omitted_replicated = omitted_stale = 0
        kept = []
        for row in rows:
            if row.is_replicated and not include_replicated:
                omitted_replicated += 1
            elif row.captured_at < cutoff:
                omitted_stale += 1
            else:
                kept.append(row)
        kept.sort(key=lambda r: (float(r.price), int(r.store_id)))

        items = []
        for row in kept[:limit]:
            store = refs[int(row.store_id)]
            item = price_dict(row, store["timezone"])
            item.pop("is_replicated", None)
            has_unit = row.unit_price_normalized is not None and unit_basis is not None
            item.update({
                "store": store,
                "unit_price": round(float(row.unit_price_normalized), 2) if has_unit else None,
                "unit_price_basis": unit_basis if has_unit else None,
                "store_url": row.store_url,
            })
            items.append(item)

        prices = [float(r.price) for r in kept]
        summary = {
            "min": round(min(prices), 2) if prices else None,
            "median": round(statistics.median(prices), 2) if prices else None,
            "max": round(max(prices), 2) if prices else None,
            "store_count": len(kept),
            "cheapest_store": refs[int(kept[0].store_id)] if kept else None,
        }

    payload = {
        "product": product_info,
        "prices": items,
        "summary": summary,
        "omitted_stale": omitted_stale,
        "omitted_replicated": omitted_replicated,
        "truncated": False,
    }
    return envelope(payload, country, context)


# ---------- get_price_history ----------

def get_price_history(**params):
    """Serie diaria (mediana, minimo, maximo por dia) de um produto, sem interpolar dias sem dado."""
    country = check_country(params.get("country"))
    context = as_bool(params.get("context"), "context")
    product_id, ean = _product_param(params)
    days = as_int(params.get("days"), "days", 1, 365, default=30)
    city = as_str(params.get("city"), "city", 120)
    state = _state_param(params.get("state"))
    chain = as_str(params.get("chain"), "chain", 120)

    with db.session_scope() as session, db_errors():
        product = _resolve_product(session, product_id, ean)
        product_info = _product_info(product)
        store_ids = _scope_stores(session, city, state, country, chain)
        refs = _store_refs(session, store_ids)

        obs = []
        if store_ids:
            m = db.load_models()
            ph = m.PriceHistory
            # Filtro por product_id (indice product_id+captured_at) e por data; LIMIT com folga para detectar excesso.
            rows = session.execute(
                sa.select(ph.store_id, ph.price, ph.captured_at, ph.is_replicated)
                .where(
                    ph.product_id == product.id,
                    ph.store_id.in_(store_ids),
                    ph.captured_at >= cutoff_naive(days),
                )
                .order_by(ph.captured_at)
                .limit(PRICE_HISTORY_ROW_LIMIT + 1)
            ).all()
            if len(rows) > PRICE_HISTORY_ROW_LIMIT:
                raise QueryError(
                    "query_timeout", "The price history is too large for one query.",
                    "Narrow by city, state or chain, or reduce days.", 504,
                )
            obs = [(int(r.store_id), float(r.price), r.captured_at) for r in rows if not r.is_replicated]

        by_day = defaultdict(list)
        for store_id, price, when in obs:
            by_day[when.date()].append((price, store_id))
        series = []
        for day in sorted(by_day):
            prices = [p for p, _ in by_day[day]]
            series.append({
                "date": day.isoformat(),
                "min": round(min(prices), 2),
                "median": round(statistics.median(prices), 2),
                "max": round(max(prices), 2),
                "store_count": len({s for _, s in by_day[day]}),
            })

        def extreme(choice):
            store_id, value, when = choice
            return {"value": round(value, 2), "date": when.date().isoformat(), "store": refs.get(store_id)}

        first = series[0] if series else None
        last = series[-1] if series else None
        change_pct = None
        if len(series) >= 2 and first["median"] > 0:
            change_pct = round((last["median"] - first["median"]) / first["median"] * 100, 1)
        summary = {
            "first": {"date": first["date"], "price": first["median"]} if first else None,
            "last": {"date": last["date"], "price": last["median"]} if last else None,
            "change_pct": change_pct,
            "lowest": extreme(min(obs, key=lambda o: o[1])) if obs else None,
            "highest": extreme(max(obs, key=lambda o: o[1])) if obs else None,
        }

    payload = {"product": product_info, "series": series, "summary": summary, "truncated": False}
    return envelope(payload, country, context)


# ---------- indice de precos ----------

def _box_bounds(start, period_days):
    """Caixas de 7 dias a partir de start, ate start + period_days: (k, inicio, fim), fim exclusivo."""
    end = start + timedelta(days=period_days)
    k = 0
    while True:
        lo = start + timedelta(days=BOX_DAYS * k)
        if lo >= end:
            return
        yield k, lo, min(lo + timedelta(days=BOX_DAYS), end)
        k += 1


def paired_index(observations, start, period_days, min_sample=MIN_SAMPLE):
    """Indice de precos com pares presentes na caixa-base e na caixa do ponto.

    observations: dicts com product_id, store_id, price e captured_at (horario legado, sem fuso).
    A caixa-base e a primeira de 7 dias; cada ponto compara a sua caixa com a base
    (matched_pairs) e usa jevons_index (media geometrica x 100, razoes fora de [0,2; 5] descartadas).
    Pares que so existem em outra caixa nao entram.

    Devolve: series [{date, index}] (so pontos com indice), change_pct (ultimo ponto - 100, ou None),
    pairs_in_sample e products_in_sample (da ultima comparacao) e pairs_dropped_outliers (soma).
    """
    bounds = list(_box_bounds(start, period_days))
    boxes = [[o for o in observations if lo <= o["captured_at"] < hi] for _, lo, hi in bounds]
    series = []
    if not boxes or not boxes[0]:
        return {"series": series, "change_pct": None, "pairs_in_sample": 0,
                "products_in_sample": 0, "pairs_dropped_outliers": 0}

    base = boxes[0]
    series.append({"date": bounds[0][1].date().isoformat(), "index": 100.0})
    dropped = 0
    last = None
    for (k, lo, _hi), box in zip(bounds[1:], boxes[1:]):
        last = jevons_index(matched_pairs(base, box), base_value=100.0, min_sample=min_sample)
        dropped += last["outliers_removed"]
        if last["index"] is not None:
            series.append({"date": lo.date().isoformat(), "index": round(last["index"], 2)})

    change_pct = None
    if last is not None and last["index"] is not None:
        change_pct = round(last["index"] - 100.0, 1)
    return {
        "series": series,
        "change_pct": change_pct,
        "pairs_in_sample": last["sample_size"] if last else 0,
        "products_in_sample": last["products"] if last else 0,
        "pairs_dropped_outliers": dropped,
    }


def _movers(observations, start, period_days, top):
    """(altas, quedas) por produto: mediana da variacao % dos pares, da caixa-base ate a ultima caixa."""
    bounds = list(_box_bounds(start, period_days))
    if len(bounds) < 2:
        return [], []
    _, base_lo, base_hi = bounds[0]
    _, last_lo, last_hi = bounds[-1]
    base = [o for o in observations if base_lo <= o["captured_at"] < base_hi]
    last = [o for o in observations if last_lo <= o["captured_at"] < last_hi]
    by_product = defaultdict(list)
    for pair in matched_pairs(base, last):
        by_product[pair["product_id"]].append((pair["current_price"] / pair["base_price"] - 1) * 100)
    changes = {pid: round(statistics.median(vals), 1) for pid, vals in by_product.items()}
    ups = sorted((p for p in changes if changes[p] > 0), key=lambda p: (-changes[p], p))[:top]
    downs = sorted((p for p in changes if changes[p] < 0), key=lambda p: (changes[p], p))[:top]
    return [(p, changes[p]) for p in ups], [(p, changes[p]) for p in downs]


# ---------- indice pre-calculado ----------

INDEX_HINT = (
    "Indexes are precomputed nightly for each state and for the 20 largest cities. "
    "Pass state (two-letter code), or city, with optional category (a slug from list_categories) "
    "and period (7d, 30d or 90d)."
)


def _slot(value):
    """Parte da chave: texto sem acento e sem caixa, espacos viram hifen; vazio vira '*'."""
    return fold(value).replace(" ", "-") if value else "*"


def index_key(country, state, city, category, period):
    """Chave Redis do indice: precos:index:{pais}:{UF}:{cidade|*}:{categoria|*}:{periodo}. Usada pelo job e pela consulta."""
    return ":".join((INDEX_PREFIX, country, state, _slot(city), _slot(category), period))


def _index_state(session, country, city):
    """UF do indice quando so a cidade foi dada: a unica UF das lojas com esse nome."""
    store_ids = resolve_location(session, city=city, country=country)
    states = {ref["state"] for ref in _store_refs(session, store_ids).values() if ref["state"]}
    if len(states) == 1:
        return states.pop()
    raise QueryError(
        "invalid_parameter", "Send state together with city.",
        "This city name exists in more than one state: use city and state, e.g. city=Campinas&state=SP.", 400,
    )


def _read_index(key):
    """Le a chave pronta. Redis fora do ar -> index_unavailable; sem chave -> index_not_computed."""
    if demo.enabled():
        stored = demo.index_for(key)
        if stored is None:
            raise QueryError("index_not_computed", "Index not computed yet for this scope.", INDEX_HINT, 404)
        return stored
    try:
        raw = _get_redis().get(key)
    except Exception as exc:
        log.warning("indice de precos indisponivel no Redis (%s)", type(exc).__name__)
        raise QueryError(
            "index_unavailable", "The price index store is unavailable right now.",
            "Retry in a few seconds.", 503,
        ) from exc
    if raw is None:
        raise QueryError("index_not_computed", "Index not computed yet for this scope.", INDEX_HINT, 404)
    return json.loads(raw)


def _movers_out(rows, products):
    return [{"product": products[p], "change_pct": c} for p, c in rows if p in products]


def get_price_index(**params):
    """Indice de precos por UF (ou cidade) e categoria, lido do resultado pre-calculado pelo job noturno."""
    country = check_country(params.get("country"))
    context = as_bool(params.get("context"), "context")
    period = as_str(params.get("period"), "period", 3, default="30d")
    if period not in PERIOD_DAYS:
        raise QueryError("invalid_parameter", "Invalid value for period.", "Valid periods: 7d, 30d, 90d.", 400)
    category = as_str(params.get("category"), "category", 100)
    city = as_str(params.get("city"), "city", 120)
    state = _state_param(params.get("state"))
    top = as_int(params.get("top"), "top", 1, 50, default=10)
    if not (city or state):
        raise QueryError(
            "invalid_parameter", "Send state or city.",
            "Indexes exist for each state and for the 20 largest cities: pass state (two-letter code) or city.", 400,
        )
    if state is None:
        with db.session_scope() as session, db_errors():
            state = _index_state(session, country, city)

    stored = _read_index(index_key(country, state, city, category, period))
    payload = dict(stored)
    payload["top_increases"] = stored["top_increases"][:top]
    payload["top_decreases"] = stored["top_decreases"][:top]
    payload["methodology_url"] = METHODOLOGY_URL
    payload["truncated"] = False
    return envelope(payload, country, context)
