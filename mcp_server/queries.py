"""Fachada da camada de consulta: liga as 11 ferramentas de schemas.TOOL_SPECS as funcoes q_*.

Quem chama (MCP e /api/v1) usa so TOOLS, ROUTES e call(); as funcoes q_* ficam como estao.
"""
from mcp_server.common import fit_response
from mcp_server.q_baskets import compare_basket, get_basket, list_baskets
from mcp_server.q_catalog import get_product, list_categories, list_locations, list_stores, search_products
from mcp_server.q_prices import get_price_history, get_price_index, get_prices
TOOLS = {
    "search_products": search_products,
    "get_product": get_product,
    "get_prices": get_prices,
    "compare_basket": compare_basket,
    "get_price_history": get_price_history,
    "list_baskets": list_baskets,
    "get_basket": get_basket,
    "get_price_index": get_price_index,
    "list_locations": list_locations,
    "list_stores": list_stores,
    "list_categories": list_categories,
}

# (nome da ferramenta, metodo HTTP, caminho REST), igual a tabela do contrato (sprint-03, T12).
ROUTES = [
    ("search_products", "GET", "/api/v1/products/search"),
    ("get_product", "GET", "/api/v1/products/lookup"),
    ("get_prices", "GET", "/api/v1/prices"),
    ("compare_basket", "POST", "/api/v1/baskets/compare"),
    ("get_price_history", "GET", "/api/v1/prices/history"),
    ("list_baskets", "GET", "/api/v1/baskets"),
    ("get_basket", "GET", "/api/v1/baskets/{slug}"),
    ("get_price_index", "GET", "/api/v1/price-index"),
    ("list_locations", "GET", "/api/v1/locations"),
    ("list_stores", "GET", "/api/v1/stores"),
    ("list_categories", "GET", "/api/v1/categories"),
]

# Lista principal de cada resposta que pode ser cortada para caber no teto de tamanho.
LIST_FIELDS = {
    "search_products": "products",
    "get_prices": "prices",
    "compare_basket": "stores",
    "get_price_history": "series",
    "list_baskets": "baskets",
    "list_locations": "locations",
    "list_stores": "stores",
    "list_categories": "categories",
}

def call(name, params):
    """Executa a ferramenta com os parametros (ja validados pelo chamador) e aplica o teto de tamanho."""
    payload = TOOLS[name](**params)
    field = LIST_FIELDS.get(name)
    return fit_response(payload, field) if field else payload
