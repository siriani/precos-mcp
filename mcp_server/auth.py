"""
Chave de API, limite de uso e medição do /api/v1 e do MCP.

Sem Flask e sem Pydantic. Mensagens de erro saem em inglês (regra da API).
O token nunca é gravado nem logado: só o SHA-256 (key_hash) e o prefixo.
"""
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from mcp_server.brand import BRAND

log = logging.getLogger(__name__)

TOKEN_PREFIX = "pk_live_"
DOCS_URL = BRAND["site_url"] + "/llms.txt"
KEY_HINT = 'Send "Authorization: Bearer pk_live_..." with your API key.'
CACHE_SECONDS = 60  # revogação vale em até 1 minuto
USAGE_TTL = 45 * 24 * 3600
MINUTE_TTL = 120
DAY_TTL = 3 * 24 * 3600

# plano -> (chamadas por minuto, chamadas por dia)
PLAN_LIMITS = {"free": (30, 100), "pro": (120, 50000), "internal": (600, 1000000)}

# Tabelas próprias, com MetaData próprio (o site não as conhece).
# Colunas iguais às da migração 2026_11_002_api_keys_usage.sql.
metadata = sa.MetaData()

api_keys = sa.Table(
    "api_keys",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("key_hash", sa.CHAR(64), unique=True, nullable=False),
    sa.Column("key_prefix", sa.String(16), nullable=False),
    sa.Column("owner", sa.String(120), nullable=False),
    sa.Column("plan", sa.String(20), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("revoked_at", sa.DateTime, nullable=True),
)

api_usage = sa.Table(
    "api_usage",
    metadata,
    sa.Column("key_id", sa.Integer, primary_key=True),
    sa.Column("tool", sa.String(40), primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("calls", sa.Integer, nullable=False, default=0),
    sa.Column("errors", sa.Integer, nullable=False, default=0),
)


class ApiError(Exception):
    """Erro da API no formato {"error": {"code", "message", "hint", "docs"}}."""

    def __init__(self, code: str, status: int, message: str, hint: str, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.hint = hint
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint, "docs": DOCS_URL}}


@dataclass(frozen=True)
class KeyInfo:
    id: int
    plan: str
    owner: str


def generate_key() -> tuple[str, str, str]:
    """Devolve (token, key_hash, prefix). O token deve ser mostrado ao dono uma única vez."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, hash_key(token), token[:12]


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_cache: dict[str, tuple[float, KeyInfo]] = {}  # key_hash -> (expira em monotonic, KeyInfo)


def verify_key(token, session) -> KeyInfo:
    """Confere a chave contra api_keys (não revogada). Cache de 60 s por processo."""
    if not token:
        raise ApiError("missing_api_key", 401, "API key required.", KEY_HINT)
    key_hash = hash_key(token)
    now = time.monotonic()
    cached = _cache.get(key_hash)
    if cached and cached[0] > now:
        return cached[1]
    try:
        row = session.execute(
            sa.select(api_keys.c.id, api_keys.c.plan, api_keys.c.owner).where(
                api_keys.c.key_hash == key_hash, api_keys.c.revoked_at.is_(None)
            )
        ).first()
    except Exception as exc:
        log.warning("auth: banco ou tabela api_keys indisponivel (%s)", type(exc).__name__)
        raise ApiError(
            "auth_unavailable", 503,
            "Authentication is temporarily unavailable.",
            "Retry in a few seconds.",
        ) from exc
    if row is None:
        raise ApiError("invalid_api_key", 401, "API key not recognized or revoked.", KEY_HINT)
    info = KeyInfo(id=row.id, plan=row.plan, owner=row.owner)
    _cache[key_hash] = (now + CACHE_SECONDS, info)
    return info


_redis = None


def _get_redis():
    """Cliente próprio: sem novas tentativas e timeout curto, para não travar a API."""
    global _redis
    if _redis is None:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        _redis = redis.Redis(
            retry=Retry(NoBackoff(), 0),
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB", 0)),
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
    return _redis


def check_rate(key: KeyInfo, client=None, now: datetime | None = None) -> int:
    """Conta a chamada no minuto e no dia (UTC). Levanta ApiError se passou do limite.

    Devolve as chamadas que ainda restam no minuto (X-RateLimit-Remaining).
    Falha do Redis nega a chamada (rate_limiter_unavailable); nunca libera por falha.
    """
    per_minute, per_day = PLAN_LIMITS.get(key.plan, PLAN_LIMITS["free"])
    now = now or datetime.now(timezone.utc)
    minute_key = f"precos:api:rl:{key.id}:{now.strftime('%Y%m%d%H%M')}"
    day_key = f"precos:api:quota:{key.id}:{now.strftime('%Y%m%d')}"
    try:
        r = client if client is not None else _get_redis()
        minute_calls = r.incr(minute_key)
        r.expire(minute_key, MINUTE_TTL)
        day_calls = r.incr(day_key)
        r.expire(day_key, DAY_TTL)
    except Exception as exc:
        log.warning("rate limiter indisponivel (%s)", type(exc).__name__)
        raise ApiError(
            "rate_limiter_unavailable", 503,
            "Rate limiter is unavailable, so the request was refused.",
            "Retry in a few seconds.",
        ) from exc

    if minute_calls > per_minute:
        retry = 60 - now.second
        raise ApiError(
            "rate_limited", 429,
            "Rate limit exceeded for this API key.",
            f"Limit is {per_minute} calls per minute. Retry after {retry} seconds.",
            retry_after=retry,
        )
    if day_calls > per_day:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        retry = int((tomorrow - now).total_seconds())
        raise ApiError(
            "rate_limited", 429,
            "Daily quota exceeded for this API key.",
            f"Daily limit is {per_day} calls. It resets at 00:00 UTC.",
            retry_after=retry,
        )
    return max(per_minute - minute_calls, 0)


def meter(key_id: int, tool: str, ok: bool = True) -> None:
    """Soma a chamada no hash do dia (UTC). Falha aqui só loga: o limite já passou."""
    try:
        r = _get_redis()
        key = f"precos:api:usage:{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        r.hincrby(key, f"{key_id}:{tool}:{'ok' if ok else 'err'}", 1)
        r.expire(key, USAGE_TTL)
    except Exception as exc:
        log.warning("medicao de uso descartada (%s)", type(exc).__name__)
