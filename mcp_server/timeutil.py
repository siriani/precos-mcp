"""
Tempo e moeda da camada de leitura (API v1 e MCP).

A gravação legada guarda `captured_at` como horário ingênuo em UTC−3 fixo
(`get_local_now()` em models.py). Aqui esse valor é convertido para UTC e,
depois, para o fuso IANA da loja, com offset ISO 8601.

Só biblioteca padrão (zoneinfo).
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Gravação legada: horário ingênuo em UTC−3 fixo.
LEGACY_OFFSET = timezone(timedelta(hours=-3))

DEFAULT_TZ = "America/Sao_Paulo"
DEFAULT_COUNTRY = "BR"

CURRENCIES = {"BR": "BRL", "US": "USD"}

_warned_zones = set()


def now_utc():
    """Instante atual, com fuso UTC."""
    return datetime.now(timezone.utc)


def _as_utc(aware):
    if aware.tzinfo is None:
        raise ValueError("datetime sem fuso: informe um datetime com tzinfo")
    return aware.astimezone(timezone.utc)


def legacy_to_utc(naive):
    """Horário ingênuo da gravação legada (UTC−3) para datetime aware em UTC."""
    return naive.replace(tzinfo=LEGACY_OFFSET).astimezone(timezone.utc)


def _zone(tz_name):
    """Fuso IANA; se desconhecido ou sem base de fusos, cai em UTC−3 fixo (avisa uma vez)."""
    key = tz_name or DEFAULT_TZ
    try:
        return ZoneInfo(key)
    except Exception:
        if key not in _warned_zones:
            _warned_zones.add(key)
            log.warning("Fuso %r indisponível; usando UTC-3 fixo", key)
        return LEGACY_OFFSET


def iso_local(naive, tz_name):
    """Horário legado no fuso da loja, em ISO 8601 com offset (segundos)."""
    local = legacy_to_utc(naive).astimezone(_zone(tz_name))
    return local.isoformat(timespec="seconds")


def iso_utc(aware):
    """Datetime aware em ISO 8601 UTC com sufixo Z."""
    return _as_utc(aware).isoformat(timespec="seconds").replace("+00:00", "Z")


def age_hours(naive, now=None):
    """Idade, em horas (1 casa), de um `captured_at` legado."""
    now = _as_utc(now) if now is not None else now_utc()
    delta = now - legacy_to_utc(naive)
    return round(delta.total_seconds() / 3600, 1)


def cutoff_naive(days, now=None):
    """Limite de `captured_at` para filtro SQL: now − `days` em UTC, convertido para o formato legado (ingênuo, UTC−3)."""
    base = _as_utc(now) if now is not None else now_utc()
    return (base - timedelta(days=days)).astimezone(LEGACY_OFFSET).replace(tzinfo=None)


def currency_for(country):
    """Moeda do país (ex.: BR -> BRL); None se o país não tem dado."""
    return CURRENCIES.get((country or "").upper())
