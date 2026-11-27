"""Consultas de catalogo (T7): busca de produtos, ficha, categorias, locais e lojas.

Somente leitura. Cada funcao recebe parametros nomeados (**params) e devolve dict com envelope.
Preco atual sempre de current_prices; preco proprio = is_replicated = 0.
Biblioteca padrao + SQLAlchemy + o proprio pacote; sem Pydantic.
"""
import base64
import os
import re
import statistics

import sqlalchemy as sa

from mcp_server import db
from mcp_server.brand import BRAND
from mcp_server.common import (
    QueryError,
    as_bool,
    as_int,
    as_str,
    check_country,
    db_errors,
    envelope,
    fold,
    normalize_gtin,
    resolve_location,
)
from mcp_server.timeutil import DEFAULT_COUNTRY, DEFAULT_TZ, age_hours, iso_local

PRODUCT_SITE = BRAND["site_url"] + "/"
FULLTEXT_FLAG = "PRECOS_SEARCH_FULLTEXT"  # "1" liga a busca FULLTEXT (so MySQL/MariaDB)
MAX_BIGINT = 9_223_372_036_854_775_807


# ---------- entrada ----------

def _gtin_query(text):
    """Candidatos EAN se a consulta for so digitos (com espaco ou hifen) de 8 a 14; senao []."""
    if re.fullmatch(r"[\d\s-]+", text):
        return normalize_gtin(text)
    return []


def _like(text, prefix=False):
    """Padrao LIKE com escape '!' (funciona igual em MySQL e SQLite)."""
    escaped = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return escaped + "%" if prefix else "%" + escaped + "%"


def _encode_cursor(offset):
    return base64.urlsafe_b64encode(f"o{offset}".encode()).decode()


def _decode_cursor(cursor):
    text = as_str(cursor, "cursor", 100)
    if text is None:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(text.encode()).decode()
    except ValueError:  # inclui binascii.Error e UnicodeDecodeError
        decoded = ""
    if not re.fullmatch(r"o\d{1,9}", decoded):
        raise QueryError(
            "invalid_parameter", "Invalid value for cursor.",
            "Use next_cursor from the previous response, or omit cursor.", 400,
        )
    return int(decoded[1:])


# ---------- saida ----------

def _query(build, params):
    """Abre a sessao de leitura, traduz erros do banco e aplica o envelope."""
    country = check_country(params.get("country"))
    context = as_bool(params.get("context"), "context", False)
    with db.session_scope() as session, db_errors():
        payload = build(session, params, country)
    return envelope(payload, country, context)


def _product_dict(prod, category_name):
    return {
        "product_id": int(prod.id),
        "ean": prod.ean,
        "ean_verified": bool(prod.ean),
        "name": prod.name_canonical,
        "brand": prod.brand_canonical,
        "size": prod.weight_volume,
        "category": category_name,
        "url": PRODUCT_SITE + "produto/" + prod.slug if prod.slug else PRODUCT_SITE,
    }


def _store_ref(store, locale):
    """StoreRef: loja com fuso e pais de db.store_locale (padrao America/Sao_Paulo, BR)."""
    tz, country = locale.get(int(store.id), (DEFAULT_TZ, DEFAULT_COUNTRY))
    return {
        "store_id": int(store.id),
        "chain": store.name or "",
        "unit": store.unit or "",
        "city": store.city or "",
        "state": store.state or "",
        "country": country,
        "timezone": tz,
    }


def _last_seen(rows, locale):
    """Horario mais recente das linhas, no fuso da loja que o registrou."""
    newest = max(rows, key=lambda r: r.captured_at)
    tz = locale.get(int(newest.store_id), (DEFAULT_TZ, DEFAULT_COUNTRY))[0]
    return iso_local(newest.captured_at, tz)


# ---------- consultas base ----------

def _models():
    return db.load_models()


def _stores(session, ids):
    """{store_id: linha de stores} para os ids pedidos."""
    if ids is not None and not ids:
        return {}
    m = _models()
    stmt = sa.select(m.Store.id, m.Store.name, m.Store.unit, m.Store.city, m.Store.state)
    if ids is not None:
        stmt = stmt.where(m.Store.id.in_(list(ids)))
    return {int(r.id): r for r in session.execute(stmt).all()}


def _own_prices(session, product_ids, store_ids):
    """Precos proprios (is_replicated = 0) de produtos nas lojas: uma consulta em current_prices."""
    if not product_ids or not store_ids:
        return []
    cp = _models().CurrentPrice
    stmt = sa.select(
        cp.product_id, cp.store_id, cp.price, cp.price_regular, cp.is_promotion, cp.captured_at,
    ).where(
        cp.product_id.in_(list(product_ids)),
        cp.store_id.in_(list(store_ids)),
        cp.is_replicated == 0,
    )
    return session.execute(stmt).all()


def _product_query(m):
    """Produto com o nome da categoria (uma consulta, sem N+1)."""
    return sa.select(m.Product, m.Category.name).outerjoin(m.Category, m.Category.id == m.Product.category_id)


def _price_summary(rows, locale, stores):
    values = [float(r.price) for r in rows]
    cheapest = min(rows, key=lambda r: (float(r.price), r.store_id))
    return {
        "min": round(min(values), 2),
        "median": round(statistics.median(values), 2),
        "max": round(max(values), 2),
        "store_count": len(rows),
        "cheapest_store": _store_ref(stores[int(cheapest.store_id)], locale),
    }


# ---------- ferramentas ----------

def search_products(**params):
    """Busca por nome, marca ou EAN. Paginacao por cursor; faixa de preco e lojas no local pedido."""
    return _query(_search_products, params)


def _search_products(session, p, country):
    m = _models()
    query = as_str(p.get("query"), "query", 200)
    if query is None:
        raise QueryError("invalid_parameter", "Missing query.", "Send a product name, brand or barcode in query.", 400)
    city = as_str(p.get("city"), "city", 120)
    state = as_str(p.get("state"), "state", 2)
    category = as_str(p.get("category"), "category", 100)
    limit = as_int(p.get("limit"), "limit", 1, 50, default=10)
    offset = _decode_cursor(p.get("cursor"))

    store_ids = resolve_location(session, city, state, country)
    stmt = _product_query(m)

    gtin = _gtin_query(query)
    if gtin:
        stmt = stmt.where(m.Product.ean.in_(gtin))
    elif os.getenv(FULLTEXT_FLAG) == "1":
        stmt = stmt.where(sa.text("MATCH(name_canonical, brand_canonical) AGAINST (:q)").bindparams(q=query))
    else:
        # Cada palavra tem de aparecer (em qualquer ordem) no nome ou na marca:
        # "cafe 500g" acha "Cafe Pilao 500g". "500 g" vira "500g" para casar com o padrão dos nomes.
        tokens = re.sub(r"(\d)\s+(g|kg|ml|l|un)\b", r"\1\2", query, flags=re.I).split()[:8]
        for tok in tokens:
            stmt = stmt.where(sa.or_(
                m.Product.name_canonical.like(_like(tok), escape="!"),
                m.Product.brand_canonical.like(_like(tok), escape="!"),
            ))

    if category:
        category_id = session.scalar(sa.select(m.Category.id).where(m.Category.slug == category))
        if category_id is None:
            raise QueryError(
                "invalid_parameter", f"Unknown category: {category}.",
                "Call list_categories for the valid category slugs.", 400,
            )
        stmt = stmt.where(m.Product.category_id == category_id)

    relevance = sa.case(
        (m.Product.name_canonical == query, 0),
        (m.Product.name_canonical.like(_like(query, prefix=True), escape="!"), 1),
        else_=2,
    )
    stmt = stmt.order_by(relevance, sa.func.length(m.Product.name_canonical), m.Product.id)
    rows = session.execute(stmt.offset(offset).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]

    locale = db.store_locale(session)
    prices = {}
    for r in _own_prices(session, [prod.id for prod, _ in rows], store_ids):
        prices.setdefault(int(r.product_id), []).append(r)

    products = []
    for prod, category_name in rows:
        found = prices.get(int(prod.id), [])
        values = [float(r.price) for r in found]
        products.append({
            **_product_dict(prod, category_name),
            "min_price": round(min(values), 2) if values else None,
            "max_price": round(max(values), 2) if values else None,
            "store_count": len(found),
            "last_seen": _last_seen(found, locale) if found else None,
        })
    return {
        "products": products,
        "next_cursor": _encode_cursor(offset + limit) if more else None,
    }


def get_product(**params):
    """Ficha do produto por product_id, ean ou slug; resumo de preco no local, se pedido."""
    return _query(_get_product, params)


def _get_product(session, p, country):
    m = _models()
    given = [k for k in ("product_id", "ean", "slug") if p.get(k) not in (None, "")]
    if len(given) != 1:
        raise QueryError(
            "invalid_parameter", "Send exactly one of product_id, ean or slug.",
            "Use product_id from search_products.", 400,
        )
    key = given[0]
    stmt = _product_query(m)
    if key == "product_id":
        stmt = stmt.where(m.Product.id == as_int(p["product_id"], "product_id", 1, MAX_BIGINT))
    elif key == "ean":
        candidates = normalize_gtin(str(p["ean"]))
        if not candidates:
            raise QueryError("invalid_parameter", "Invalid value for ean.", "Use 8 to 14 digits.", 400)
        stmt = stmt.where(m.Product.ean.in_(candidates))
    else:
        stmt = stmt.where(m.Product.slug == as_str(p["slug"], "slug", 200))

    found = session.execute(stmt).first()
    if found is None:
        raise QueryError(
            "product_not_found", "Product not found.",
            "Call search_products to find the product_id.", 404,
        )
    prod, category_name = found

    city = as_str(p.get("city"), "city", 120)
    state = as_str(p.get("state"), "state", 2)
    store_ids = resolve_location(session, city, state, country)
    rows = _own_prices(session, [prod.id], store_ids)
    summary = None
    if rows:
        locale = db.store_locale(session)
        stores = _stores(session, {int(r.store_id) for r in rows})
        summary = _price_summary(rows, locale, stores)
    return {"product": _product_dict(prod, category_name), "price_summary": summary}


def list_categories(**params):
    """Categorias com produtos, para usar como filtro."""
    return _query(_list_categories, params)


def _list_categories(session, p, country):
    m = _models()
    count = sa.func.count(m.Product.id)
    stmt = (
        sa.select(m.Category.slug, m.Category.name, count.label("product_count"))
        .select_from(m.Category)
        .outerjoin(m.Product, m.Product.category_id == m.Category.id)
        .where(m.Category.slug.is_not(None))
        .group_by(m.Category.id, m.Category.slug, m.Category.name)
        .having(count > 0)
        .order_by(m.Category.name)
    )
    rows = session.execute(stmt).all()
    return {
        "categories": [
            {"slug": r.slug, "name": r.name, "product_count": int(r.product_count)} for r in rows
        ],
    }


def list_locations(**params):
    """Cidades cobertas no pais, com lojas, redes e idade do dado mais recente."""
    return _query(_list_locations, params)


def _list_locations(session, p, country):
    m = _models()
    state = as_str(p.get("state"), "state", 2)
    locale = db.store_locale(session)
    stores = session.execute(sa.select(m.Store.id, m.Store.name, m.Store.city, m.Store.state)).all()
    newest = {
        int(r.store_id): r.captured
        for r in session.execute(
            sa.select(m.CurrentPrice.store_id, sa.func.max(m.CurrentPrice.captured_at).label("captured"))
            .where(m.CurrentPrice.is_replicated == 0)
            .group_by(m.CurrentPrice.store_id)
        ).all()
    }

    cities = {}
    for s in stores:
        tz, store_country = locale.get(int(s.id), (DEFAULT_TZ, DEFAULT_COUNTRY))
        if store_country != country or not s.city or not s.state:
            continue
        uf = s.state.upper()
        if state and uf != state.upper():
            continue
        entry = cities.setdefault((uf, s.city), {"stores": 0, "chains": set(), "fresh": None})
        entry["stores"] += 1
        entry["chains"].add(s.name)
        captured = newest.get(int(s.id))
        if captured is not None and (entry["fresh"] is None or captured > entry["fresh"][0]):
            entry["fresh"] = (captured, tz)

    locations = []
    for (uf, city), entry in sorted(cities.items()):
        fresh = entry["fresh"]
        locations.append({
            "state": uf,
            "city": city,
            "store_count": entry["stores"],
            "chain_count": len(entry["chains"]),
            "freshest_at": iso_local(fresh[0], fresh[1]) if fresh else None,
        })
    return {"locations": locations}


def list_stores(**params):
    """Lojas monitoradas com produtos com preco proprio e idade da coleta."""
    return _query(_list_stores, params)


def _list_stores(session, p, country):
    city = as_str(p.get("city"), "city", 120)
    state = as_str(p.get("state"), "state", 2)
    chain = as_str(p.get("chain"), "chain", 120)
    locale = db.store_locale(session)

    store_ids = resolve_location(session, city, state, country)
    stores = _stores(session, store_ids)
    if chain:
        wanted = fold(chain)
        stores = {sid: s for sid, s in stores.items() if fold(s.name) == wanted}

    cp = _models().CurrentPrice
    collected = {}
    if stores:
        rows = session.execute(
            sa.select(cp.store_id, cp.captured_at).where(
                cp.store_id.in_(list(stores)), cp.is_replicated == 0,
            )
        ).all()
        for r in rows:
            collected.setdefault(int(r.store_id), []).append(r.captured_at)

    out = []
    for store in sorted(stores.values(), key=lambda s: (s.name or "", s.unit or "", s.id)):
        times = collected.get(int(store.id), [])
        out.append({
            **_store_ref(store, locale),
            "products_with_price": len(times),
            "last_collected_at": iso_local(max(times), locale.get(int(store.id), (DEFAULT_TZ, DEFAULT_COUNTRY))[0])
            if times else None,
            "median_age_hours": round(statistics.median(age_hours(t) for t in times), 1) if times else None,
        })
    return {"stores": out}
