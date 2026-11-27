"""Consultas de cesta: list_baskets, get_basket e compare_basket.

Preco atual sempre de current_prices. Historico (price_history) so com product_id IN (...) e faixa de data.
Devolve dict com envelope; Pydantic nao entra aqui.
"""
import re
from collections import defaultdict
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations

import sqlalchemy as sa

from mcp_server import common, db
from mcp_server.brand import BRAND
from mcp_server.timeutil import DEFAULT_COUNTRY, DEFAULT_TZ, cutoff_naive

PRODUCT_URL = BRAND["site_url"] + "/produto/"
MAX_COMPARE_ITEMS = 30
MAX_BASKET_ITEMS = 100
MAX_BASKETS = 100
CURRENT_MAX_AGE_DAYS = 7
PAST_WINDOW_DAYS = 7  # janela para achar o preco de 7 ou 30 dias atras (coleta semanal)
PAST_ROW_LIMIT = 50000
ITEM_KEYS = ("product_id", "ean", "query")
_CENT = Decimal("0.01")


def _dec(value):
    return Decimal(str(value))


def _money(value):
    return float(_dec(value).quantize(_CENT, rounding=ROUND_HALF_UP))


def _change_pct(now_sum, past_sum):
    if not past_sum:
        return None
    return round(float((_dec(now_sum) / _dec(past_sum) - 1) * 100), 1)


# ---------- funcao pura ----------

def basket_totals(items, prices_by_store):
    """Totais de uma lista de compras por loja (funcao pura, sem banco).

    items: [{"key": ..., "quantity": int}]
    prices_by_store: {store_id: {key: preco unitario}}; so entram chaves de items.
    Devolve (stores, best_store, best_split):
      stores: lojas com pelo menos um item, da mais barata para a mais cara.
      best_store: loja com todos os itens (a mais barata); sem nenhuma, a de mais itens, com complete=False.
      best_split: menor total comprando em at 2 lojas (cada item na mais barata do par); cobre o maior
                  numero de itens primeiro, depois o menor total. None sem nenhum preco.
    """
    qty = {it["key"]: Decimal(int(it["quantity"])) for it in items}
    n_items = len(qty)
    price = {}
    for store_id, by_key in prices_by_store.items():
        found = {k: _dec(v) for k, v in by_key.items() if k in qty}
        if found:
            price[store_id] = found

    stores = []
    for store_id, found in price.items():
        total = sum((qty[k] * p for k, p in found.items()), Decimal(0))
        stores.append({
            "store_id": store_id,
            "total": total,
            "items_found": len(found),
            "items_missing": [k for k in qty if k not in found],
        })
    stores.sort(key=lambda s: (s["total"], s["store_id"]))

    best_store = None
    complete = [s for s in stores if s["items_found"] == n_items]
    if complete:
        pick = min(complete, key=lambda s: (s["total"], s["store_id"]))
        best_store = {"store_id": pick["store_id"], "total": pick["total"], "complete": True}
    elif stores:
        pick = min(stores, key=lambda s: (-s["items_found"], s["total"], s["store_id"]))
        best_store = {"store_id": pick["store_id"], "total": pick["total"], "complete": False}

    best_split = None
    if price:
        ids = sorted(price)
        all_keys = sorted({k for found in price.values() for k in found}, key=str)
        best_rank = None
        for combo in [(sid,) for sid in ids] + list(combinations(ids, 2)):
            assign = []
            for k in all_keys:
                options = [(price[sid][k], sid) for sid in combo if k in price[sid]]
                if options:
                    unit, sid = min(options)
                    assign.append({"key": k, "store_id": sid, "unit_price": unit})
            total = sum((qty[a["key"]] * a["unit_price"] for a in assign), Decimal(0))
            rank = (-len(assign), total, len(combo), combo)
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_split = {"store_ids": list(combo), "total": total, "assignments": assign}

    return stores, best_store, best_split


# ---------- produtos ----------

def _product_cols(m):
    return (
        sa.select(
            m.Product.id.label("id"),
            m.Product.ean.label("ean"),
            m.Product.name_canonical.label("name"),
            m.Product.brand_canonical.label("brand"),
            m.Product.weight_volume.label("size"),
            m.Product.slug.label("slug"),
            m.Category.name.label("category"),
        )
        .select_from(m.Product)
        .outerjoin(m.Category, m.Category.id == m.Product.category_id)
    )


def _product_info(row):
    return {
        "product_id": int(row.id),
        "ean": row.ean,
        "ean_verified": bool(row.ean),
        "name": row.name,
        "brand": row.brand,
        "size": row.size,
        "category": row.category,
        "url": PRODUCT_URL + (row.slug or str(row.id)),
    }


def _products_by_id(session, m, ids):
    if not ids:
        return {}
    rows = session.execute(_product_cols(m).where(m.Product.id.in_(list(ids))).limit(len(ids))).all()
    return {int(r.id): r for r in rows}


def _first_by_text(session, m, text):
    """Primeiro produto cujo nome ou marca contem todas as palavras (ordem por id)."""
    tokens = [t for t in re.sub(r"[%_\\]", " ", text).lower().split() if t]
    if not tokens:
        return None
    conditions = [
        sa.or_(
            sa.func.lower(m.Product.name_canonical).like(f"%{t}%"),
            sa.func.lower(m.Product.brand_canonical).like(f"%{t}%"),
        )
        for t in tokens
    ]
    stmt = _product_cols(m).where(*conditions).order_by(m.Product.id).limit(1)
    return session.execute(stmt).first()


# ---------- preco ----------

def _lowest_by_product(rows):
    """{product_id: menor preco entre as lojas}."""
    out = {}
    for row in rows:
        pid, price = int(row.product_id), _dec(row.price)
        out[pid] = min(out.get(pid, price), price)
    return out


def _past_prices(session, m, product_ids, store_ids, days):
    """Menor preco por produto, com o ultimo registro de cada loja numa janela de PAST_WINDOW_DAYS ate `days` atras.

    Sempre por product_id (indice) e por data; preco replicado fica de fora, como no atual.
    """
    if not product_ids or not store_ids:
        return {}
    ph = m.PriceHistory
    stmt = (
        sa.select(ph.product_id, ph.store_id, ph.price)
        .where(
            ph.product_id.in_(list(product_ids)),
            ph.store_id.in_(list(store_ids)),
            ph.captured_at > cutoff_naive(days + PAST_WINDOW_DAYS),
            ph.captured_at <= cutoff_naive(days),
            sa.or_(ph.is_replicated.is_(None), ph.is_replicated == sa.false()),
        )
        .order_by(ph.captured_at.desc())
        .limit(PAST_ROW_LIMIT)
    )
    latest = {}
    for row in session.execute(stmt).all():
        latest.setdefault((int(row.product_id), int(row.store_id)), row)  # o primeiro de cada par e o mais novo
    return _lowest_by_product(latest.values())


def _store_refs(session, m, store_ids, locale):
    if not store_ids:
        return {}
    rows = session.execute(
        sa.select(m.Store.id, m.Store.name, m.Store.unit, m.Store.city, m.Store.state)
        .where(m.Store.id.in_(list(store_ids)))
        .limit(len(store_ids))
    ).all()
    refs = {}
    for r in rows:
        timezone, country = locale.get(int(r.id), (DEFAULT_TZ, DEFAULT_COUNTRY))
        refs[int(r.id)] = {
            "store_id": int(r.id),
            "chain": r.name,
            "unit": r.unit,
            "city": r.city or "",
            "state": r.state or "",
            "country": country,
            "timezone": timezone,
        }
    return refs


# ---------- parametros ----------

def _parse_items(raw):
    """Itens da lista: cada um com exatamente um de product_id, ean ou query; quantity 1 a 1000."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise common.QueryError(
            "invalid_parameter", "items must be a non-empty list.",
            f"Send 1 to {MAX_COMPARE_ITEMS} items, each with product_id, ean or query.", 400,
        )
    if len(raw) > MAX_COMPARE_ITEMS:
        raise common.QueryError(
            "invalid_parameter", "Too many items in the basket.",
            f"Valid range for items: 1 to {MAX_COMPARE_ITEMS}.", 400,
        )
    items = []
    for key, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise common.QueryError("invalid_parameter", "Each item must be an object.",
                                    "Use {\"query\": ..., \"quantity\": ...} or {\"product_id\": ...}.", 400)
        given = [k for k in ITEM_KEYS if entry.get(k) not in (None, "")]
        if len(given) != 1:
            raise common.QueryError(
                "invalid_parameter", "Each item needs exactly one of product_id, ean or query.",
                "Send one of product_id, ean or query per item.", 400,
            )
        kind = given[0]
        quantity = common.as_int(entry.get("quantity"), "quantity", 1, 1000, default=1)
        if kind == "product_id":
            value = common.as_int(entry["product_id"], "product_id", 1, 2**63 - 1)
            label = f"product_id {value}"
        elif kind == "ean":
            value = str(entry["ean"]).strip()
            if not re.fullmatch(r"\d{8,14}", value):
                raise common.QueryError("invalid_parameter", "Invalid value for ean.",
                                        "Use a barcode with 8 to 14 digits.", 400)
            label = value
        else:
            value = common.as_str(entry["query"], "query", 200)
            label = value
        items.append({"key": key, "kind": kind, "value": value, "input": label, "quantity": quantity})
    return items


def _resolve_products(session, m, items):
    """{key: linha do produto} e a lista de produtos encontrados por query (matched_products)."""
    ids = [it["value"] for it in items if it["kind"] == "product_id"]
    eans = set()
    for it in items:
        if it["kind"] == "ean":
            eans.update(common.normalize_gtin(it["value"]))
    by_id = _products_by_id(session, m, ids)
    by_ean = {}
    if eans:
        rows = session.execute(_product_cols(m).where(m.Product.ean.in_(sorted(eans))).limit(len(eans))).all()
        by_ean = {str(r.ean): r for r in rows}

    resolved, matched = {}, []
    for it in items:
        if it["kind"] == "product_id":
            row = by_id.get(it["value"])
        elif it["kind"] == "ean":
            row = next((by_ean[c] for c in common.normalize_gtin(it["value"]) if c in by_ean), None)
        else:
            row = _first_by_text(session, m, it["value"])
            if row is not None:
                matched.append({"input": it["value"], "product": _product_info(row)})
        if row is None:
            raise common.QueryError(
                "product_not_found", f"Product not found: {it['input']}.",
                "Call search_products to find the product, then send its product_id.", 404,
            )
        resolved[it["key"]] = row
    return resolved, matched


# ---------- ferramentas ----------

def list_baskets(country=DEFAULT_COUNTRY, context=False):
    country = common.check_country(country)
    m = db.load_models()
    with common.db_errors(), db.session_scope() as session:
        rows = session.execute(
            sa.select(
                m.Basket.slug, m.Basket.name, m.Basket.description,
                sa.func.count(m.BasketItem.id).label("items_count"),
            )
            .select_from(m.Basket)
            .outerjoin(m.BasketItem, m.BasketItem.basket_id == m.Basket.id)
            .group_by(m.Basket.id, m.Basket.slug, m.Basket.name, m.Basket.description)
            .order_by(m.Basket.id)
            .limit(MAX_BASKETS)
        ).all()
    baskets = [
        {"slug": r.slug, "name": r.name, "description": r.description or "", "items_count": int(r.items_count)}
        for r in rows
    ]
    return common.envelope({"baskets": baskets}, country, context)


def get_basket(slug=None, city=None, state=None, country=DEFAULT_COUNTRY, context=False):
    country = common.check_country(country)
    slug_text = common.as_str(slug, "slug", 100)
    if slug_text is None:
        raise common.QueryError("invalid_parameter", "slug is required.",
                                "Call list_baskets to see the basket slugs.", 400)
    city_text = common.as_str(city, "city", 120)
    state_text = common.as_str(state, "state", 2)
    state_text = state_text.upper() if state_text else None
    m = db.load_models()
    with common.db_errors(), db.session_scope() as session:
        basket = session.execute(
            sa.select(m.Basket.id, m.Basket.slug, m.Basket.name).where(m.Basket.slug == slug_text).limit(1)
        ).first()
        if basket is None:
            raise common.QueryError("basket_not_found", f"Basket not found: {slug_text}.",
                                    "Call list_baskets to see the available baskets.", 404)
        lines = session.execute(
            sa.select(m.BasketItem.product_id, m.BasketItem.quantity)
            .where(m.BasketItem.basket_id == basket.id)
            .order_by(m.BasketItem.id)
            .limit(MAX_BASKET_ITEMS)
        ).all()
        line_pids = [int(r.product_id) for r in lines]
        pids = sorted(set(line_pids))
        products = _products_by_id(session, m, pids)
        store_ids = common.resolve_location(session, city=city_text, state=state_text, country=country)

        rows, _omitted = common.fetch_current_prices(session, pids, store_ids, CURRENT_MAX_AGE_DAYS, False)
        now = _lowest_by_product(rows)
        past7 = _past_prices(session, m, pids, store_ids, 7)
        past30 = _past_prices(session, m, pids, store_ids, 30)

    items, missing = [], []
    cost = Decimal(0)
    sums = {"7": [Decimal(0), Decimal(0)], "30": [Decimal(0), Decimal(0)]}  # [agora, passado] so nos mesmos itens
    for line in lines:
        pid, qty = int(line.product_id), Decimal(int(line.quantity))
        product = products.get(pid)
        if product is None:
            continue
        price = now.get(pid)
        if price is None:
            missing.append(product.name)
            continue
        subtotal = qty * price
        cost += subtotal
        items.append({
            "product": _product_info(product),
            "quantity": int(qty),
            "unit_price": _money(price),
            "subtotal": _money(subtotal),
            "substituted": False,
        })
        for key, past in (("7", past7), ("30", past30)):
            if pid in past:
                sums[key][0] += subtotal
                sums[key][1] += qty * past[pid]

    payload = {
        "slug": basket.slug,
        "name": basket.name,
        "city": city_text,
        "state": state_text,
        "cost": _money(cost) if items else None,
        "change_7d_pct": _change_pct(*sums["7"]),
        "change_30d_pct": _change_pct(*sums["30"]),
        "items": items,
        "items_missing": missing,
        "coverage": round(len(items) / len(lines), 2) if lines else 0.0,
    }
    return common.envelope(payload, country, context)


def compare_basket(items=None, city=None, max_age_days=7, country=DEFAULT_COUNTRY, context=False):
    country = common.check_country(country)
    parsed = _parse_items(items)
    city_text = common.as_str(city, "city", 120)
    if city_text is None:
        raise common.QueryError("invalid_parameter", "city is required.",
                                "Send the city of the comparison, for example 'Marilia'.", 400)
    days = common.as_int(max_age_days, "max_age_days", 1, 365, default=7)
    m = db.load_models()
    with common.db_errors(), db.session_scope() as session:
        store_ids = common.resolve_location(session, city=city_text, country=country)
        resolved, matched = _resolve_products(session, m, parsed)
        pids = sorted({int(row.id) for row in resolved.values()})
        rows, _omitted = common.fetch_current_prices(session, pids, store_ids, days, include_replicated=False)
        locale = db.store_locale(session)

        by_product = defaultdict(list)
        for row in rows:
            by_product[int(row.product_id)].append((int(row.store_id), row.price))
        prices_by_store = defaultdict(dict)
        for it in parsed:
            for store_id, price in by_product.get(int(resolved[it["key"]].id), []):
                prices_by_store[store_id][it["key"]] = price

        stores, best_store, best_split = basket_totals(
            [{"key": it["key"], "quantity": it["quantity"]} for it in parsed], prices_by_store,
        )
        refs = _store_refs(session, m, prices_by_store.keys(), locale)

    inputs = {it["key"]: it["input"] for it in parsed}
    payload = {
        "stores": [
            {
                "store": refs[s["store_id"]],
                "total": _money(s["total"]),
                "items_found": s["items_found"],
                "items_missing": [inputs[k] for k in s["items_missing"]],
            }
            for s in stores
        ],
        "matched_products": matched,
        "best_store": None if best_store is None else {
            "store": refs[best_store["store_id"]],
            "total": _money(best_store["total"]),
            "complete": best_store["complete"],
        },
        "best_split": None if best_split is None else {
            "stores": [refs[sid] for sid in best_split["store_ids"]],
            "total": _money(best_split["total"]),
            "items": [
                {"input": inputs[a["key"]], "store_id": a["store_id"], "price": _money(a["unit_price"])}
                for a in best_split["assignments"]
            ],
        },
    }
    return common.envelope(payload, country, context)
