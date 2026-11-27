"""Modo demo do servidor MCP: sem chave, sem Redis e sem banco real.

Ligado por PRECOS_DEMO=1 (ou `python -m mcp_server --demo`). O banco e um SQLite em memoria carregado de
mcp_server/demo_fixture.json, com datas relativas a hoje. Todo dado e sintetico, e a atribuicao diz isso.
Este modulo so importa a biblioteca padrao e auth (que nao abre conexao); db e models entram sob demanda,
porque db.py importa este modulo.
"""
import functools
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from mcp_server import auth
from mcp_server.brand import BRAND

FIXTURE_PATH = Path(__file__).with_name("demo_fixture.json")
ATTRIBUTION_PREFIX = "DEMO DATA (synthetic, not real prices)."
RATE_PER_MINUTE = 60  # no processo inteiro, sem chave
AGES = range(1, 58, 7)  # dias atras de cada semana do historico; a mais nova tem 1 dia

_rate_lock = threading.Lock()
_rate_minute = None
_rate_count = 0


def enabled() -> bool:
    """True quando o processo roda em modo demo (lido a cada chamada)."""
    return os.getenv("PRECOS_DEMO") == "1"


@functools.cache
def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _product_info(product: dict, category_names: dict) -> dict:
    """Campos publicos do produto, na mesma forma de q_catalog._product_dict."""
    ean = product["ean"]
    return {
        "product_id": product["id"],
        "ean": ean,
        "ean_verified": True,
        "name": product["name"],
        "brand": product["brand"],
        "size": product["size"],
        "category": category_names[product["category"]],
        "url": BRAND["site_url"] + "/produto/" + product["slug"],
    }


def build_engine():
    """Engine SQLite em memoria com a fixture carregada. Nao usa DATABASE_URL nem Redis."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from mcp_server import db
    from mcp_server.timeutil import LEGACY_OFFSET

    m = db.load_models()
    fx = _fixture()
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool,
                              connect_args={"check_same_thread": False})
    m.Base.metadata.create_all(engine)

    now = datetime.now(UTC).astimezone(LEGACY_OFFSET).replace(tzinfo=None)  # horario legado, UTC-3 ingenuo
    category_ids = {c["slug"]: c["id"] for c in fx["categories"]}
    magnitude = {p["id"]: Decimal(str(p["magnitude"])) for p in fx["products"]}

    with sessionmaker(bind=engine)() as s:
        s.add_all([
            m.Category(id=c["id"], name=c["name"], slug=c["slug"]) for c in fx["categories"]
        ] + [
            m.Store(id=st["id"], name=st["chain"], slug=f"{st['chain']}-{st['unit']}-{st['city']}".lower().replace(" ", "-"),
                    unit=st["unit"], city=st["city"], state=st["state"], platform="demo",
                    external_id=f"demo-{st['id']}")
            for st in fx["stores"]
        ] + [
            m.Product(id=p["id"], ean=p["ean"], slug=p["slug"], name_canonical=p["name"], brand_canonical=p["brand"],
                      category_id=category_ids[p["category"]], unit_magnitude=magnitude[p["id"]],
                      unit_measure=p["unit"], weight_volume=p["size"])
            for p in fx["products"]
        ])
        for pid, sid, price in fx["prices"]:
            value = Decimal(price)
            s.add(m.CurrentPrice(
                product_id=pid, store_id=sid, price=value, is_promotion=False, is_replicated=False,
                unit_price_normalized=round(value / magnitude[pid], 2), captured_at=now - timedelta(days=1),
            ))
        history_id = 0  # SQLite nao autoincrementa BigInteger: os ids vem explicitos
        for pid, sid, series in fx["history"]:
            for age, price in zip(AGES, series):
                history_id += 1
                s.add(m.PriceHistory(id=history_id, product_id=pid, store_id=sid, price=Decimal(price),
                                     is_promotion=False, is_replicated=False, captured_at=now - timedelta(days=age)))
        for b_id, basket in enumerate(fx["baskets"], start=1):
            s.add(m.Basket(id=b_id, name=basket["name"], slug=basket["slug"], description=basket["description"],
                           icon=basket["icon"]))
            for item_id, (pid, qty) in enumerate(basket["items"], start=1):
                s.add(m.BasketItem(id=item_id, basket_id=b_id, product_id=pid, quantity=Decimal(str(qty))))
        s.commit()
    return engine


def index_for(key: str) -> dict | None:
    """Resposta pronta do indice para a chave (mesma forma do job), com datas relativas a hoje.

    None quando a demo nao tem essa chave; quem chama responde index_not_computed.
    """
    fx = _fixture()
    raw = fx["index"].get(key)
    if raw is None:
        return None
    now = datetime.now(UTC)
    names = {c["slug"]: c["name"] for c in fx["categories"]}
    products = {p["id"]: p for p in fx["products"]}

    def mover(pid, pct):
        return {"product": _product_info(products[pid], names), "change_pct": pct}

    return {
        "computed_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": raw["method"],
        "period": raw["period"],
        "index_series": [{"date": (now.date() - timedelta(days=d)).isoformat(), "index": v}
                         for d, v in raw["index_series"]],
        "change_pct": raw["change_pct"],
        "products_in_sample": raw["products_in_sample"],
        "pairs_in_sample": raw["pairs_in_sample"],
        "pairs_dropped_outliers": raw["pairs_dropped_outliers"],
        "top_increases": [mover(pid, pct) for pid, pct in raw["top_increases"]],
        "top_decreases": [mover(pid, pct) for pid, pct in raw["top_decreases"]],
    }


def check_rate(now: float | None = None) -> None:
    """Limite em memoria de RATE_PER_MINUTE chamadas por minuto, no processo inteiro. Passou -> 429 com Retry-After."""
    global _rate_minute, _rate_count
    t = time.time() if now is None else now
    minute = int(t // 60)
    with _rate_lock:
        if minute != _rate_minute:
            _rate_minute, _rate_count = minute, 0
        _rate_count += 1
        over = _rate_count > RATE_PER_MINUTE
    if over:
        retry = 60 - int(t % 60)
        raise auth.ApiError(
            "rate_limited", 429, "Demo rate limit exceeded.",
            f"The demo allows {RATE_PER_MINUTE} calls per minute for the whole server. "
            f"Retry after {retry} seconds. No key is needed in the demo.",
            retry_after=retry,
        )
