"""Conexao de leitura da camada de consulta (MCP e /api/v1).

Engine propria, separada de models.engine: o MAX_EXECUTION_TIME daqui nunca pode vazar para o site.
"""
import contextlib
import logging
import os
import sys
import threading
import time

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from mcp_server import demo
from mcp_server.timeutil import DEFAULT_COUNTRY, DEFAULT_TZ

log = logging.getLogger(__name__)

LOCALE_TTL_SECONDS = 600
QUERY_MAX_MS = 5000

_lock = threading.Lock()
_engine = None
_session_factory = None
_models = None
_locale_cache = None  # (id(engine), monotonic, {store_id: (timezone, country)})


def load_models():
    """Importa models.py sem deixar o print de aviso (models.py:23) sujar o stdout do MCP."""
    global _models
    if _models is None:
        with contextlib.redirect_stdout(sys.stderr):
            import models as loaded
        _models = loaded
    return _models


def mysql_errno(exc):
    """Codigo numerico do erro do driver (ex.: 1054 coluna ausente, 3024 timeout), ou None."""
    args = getattr(getattr(exc, "orig", None), "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _database_url():
    return os.getenv("PRECOS_DB_RO_URL") or load_models().DATABASE_URL


def _build_engine(url):
    kwargs = {"pool_size": 3, "pool_pre_ping": True, "pool_recycle": 60}
    if make_url(url).get_backend_name() == "mysql":
        # Produção é MariaDB: max_statement_time em segundos (MAX_EXECUTION_TIME é só do MySQL e quebra a conexão).
        kwargs["connect_args"] = {"init_command": f"SET SESSION max_statement_time={QUERY_MAX_MS / 1000:g}"}
    return sa.create_engine(url, **kwargs)


def _make_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _get_session_factory():
    global _engine, _session_factory
    if _session_factory is None:
        with _lock:
            if _session_factory is None:
                # Demo: SQLite em memoria da fixture; sem DATABASE_URL e sem banco.
                _engine = demo.build_engine() if demo.enabled() else _build_engine(_database_url())
                _session_factory = _make_factory(_engine)
    return _session_factory


def set_engine(engine):
    """Troca a engine (testes). Os caches se invalidam sozinhos: a chave e a engine."""
    global _engine, _session_factory
    with _lock:
        _engine = engine
        _session_factory = _make_factory(engine)


@contextlib.contextmanager
def session_scope():
    session = _get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _is_missing_column(exc):
    return mysql_errno(exc) == 1054 or "no such column" in str(exc).lower() or "unknown column" in str(exc).lower()


def store_locale(session):
    """{store_id: (timezone, country)}, em cache de 10 min.

    Sem as colunas da T5 (stores.timezone/country), usa os padroes da T1 e avisa uma vez por cache.
    """
    global _locale_cache
    key = id(session.get_bind())
    now = time.monotonic()
    cached = _locale_cache
    if cached and cached[0] == key and now - cached[1] < LOCALE_TTL_SECONDS:
        return cached[2]
    try:
        rows = session.execute(sa.text("SELECT id, timezone, country FROM stores")).all()
        locale = {int(r[0]): (r[1] or DEFAULT_TZ, (r[2] or DEFAULT_COUNTRY).upper()) for r in rows}
    except DBAPIError as exc:
        if not _is_missing_column(exc):
            raise
        session.rollback()
        log.warning("stores sem colunas timezone/country; usando padroes %s/%s", DEFAULT_TZ, DEFAULT_COUNTRY)
        rows = session.execute(sa.text("SELECT id FROM stores")).all()
        locale = {int(r[0]): (DEFAULT_TZ, DEFAULT_COUNTRY) for r in rows}
    _locale_cache = (key, now, locale)
    return locale
