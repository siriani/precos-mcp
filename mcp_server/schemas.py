"""Esquemas Pydantic das 11 ferramentas (MCP e REST /api/v1).

Só o servidor MCP e o gerador do OpenAPI importam este módulo. O site nunca
importa: o requirements.txt dele não tem pydantic.
Todo texto que sai pela API ou pelo MCP (descrições, exemplos, erros) é inglês.
Comentários ficam em português.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mcp_server.brand import BRAND

SITE_HOST = BRAND["site_url"].split("://", 1)[1]


# ---------------------------------------------------------------- utilitários

def _exactly_one(model, names):
    """Exige exatamente um dos campos preenchidos (product_id, ean, slug...)."""
    given = [n for n in names if getattr(model, n) not in (None, "")]
    if len(given) != 1:
        raise ValueError(f"Send exactly one of: {', '.join(names)}.")
    return model


# ---------------------------------------------------------------- entrada

class _Input(BaseModel):
    """Base de entrada: rejeita campos desconhecidos e traz os parâmetros comuns."""

    model_config = ConfigDict(extra="forbid")

    country: str = Field(
        default="BR",
        description="ISO 3166-1 alpha-2 country code. Supported countries: BR.",
        examples=["BR"],
    )
    context: bool = Field(
        default=False,
        description="When true, the response also carries the JSON-LD '@context' field.",
    )


class _BasketItemInput(BaseModel):
    """Um item da lista de compras: exatamente um de product_id, ean ou query."""

    model_config = ConfigDict(extra="forbid")

    product_id: int | None = Field(
        default=None,
        ge=1,
        description="Product id from search_products. Send exactly one of product_id, ean or query.",
        examples=[1024],
    )
    ean: str | None = Field(
        default=None,
        pattern=r"^\d{8,14}$",
        description="Barcode with 8 to 14 digits. A 12-digit UPC is also matched as GTIN-13.",
        examples=["012345678905"],
    )
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Free-text product search. The first search result is used and returned in matched_products so the caller can check it.",
        examples=["white rice 5kg"],
    )
    quantity: int = Field(
        default=1,
        ge=1,
        le=1000,
        description="Units of this item in the basket.",
    )

    @model_validator(mode="after")
    def _one_key(self):
        return _exactly_one(self, ("product_id", "ean", "query"))


class SearchProductsInput(_Input):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="Product name, brand or barcode. A query of 8 to 14 digits is searched as a barcode (EAN/GTIN).",
        examples=["Nescau 400g", "012345678905"],
    )
    city: str | None = Field(
        default=None,
        max_length=120,
        description="City name. Accents and letter case are ignored.",
        examples=["Marilia"],
    )
    state: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Two-letter state code.",
        examples=["SP"],
    )
    category: str | None = Field(
        default=None,
        max_length=100,
        description="Category slug from list_categories.",
        examples=["grains"],
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum products in the response (1 to 50).")
    cursor: str | None = Field(
        default=None,
        description="next_cursor from the previous response. Omit it for the first page.",
    )


class GetProductInput(_Input):
    product_id: int | None = Field(
        default=None,
        ge=1,
        description="Product id from search_products. Send exactly one of product_id, ean or slug.",
        examples=[1024],
    )
    ean: str | None = Field(
        default=None,
        pattern=r"^\d{8,14}$",
        description="Barcode with 8 to 14 digits. A 12-digit UPC is also matched as GTIN-13.",
        examples=["012345678905"],
    )
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Product slug, the last part of the product URL.",
        examples=["white-rice-5kg"],
    )
    city: str | None = Field(
        default=None,
        max_length=120,
        description="City name for the price summary. Accents and letter case are ignored.",
        examples=["Marilia"],
    )
    state: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Two-letter state code for the price summary.",
        examples=["SP"],
    )

    @model_validator(mode="after")
    def _one_key(self):
        return _exactly_one(self, ("product_id", "ean", "slug"))


class GetPricesInput(_Input):
    product_id: int | None = Field(
        default=None,
        ge=1,
        description="Product id from search_products. Send exactly one of product_id or ean.",
        examples=[1024],
    )
    ean: str | None = Field(
        default=None,
        pattern=r"^\d{8,14}$",
        description="Barcode with 8 to 14 digits. A 12-digit UPC is also matched as GTIN-13.",
        examples=["012345678905"],
    )
    city: str | None = Field(
        default=None,
        max_length=120,
        description="City name. Accents and letter case are ignored.",
        examples=["Marilia"],
    )
    state: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Two-letter state code.",
        examples=["SP"],
    )
    chain: str | None = Field(
        default=None,
        max_length=120,
        description="Retail chain name to filter by.",
        examples=["Atacadao"],
    )
    max_age_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Prices collected more than this many days ago are left out and counted in omitted_stale. Every price carries captured_at and age_hours, so filter again on your side if you need fresher data.",
    )
    include_replicated: bool = Field(
        default=False,
        description="Include prices copied from another store. They are not real observations, so the default is false.",
    )
    limit: int = Field(default=20, ge=1, le=50, description="Maximum prices in the response (1 to 50).")

    @model_validator(mode="after")
    def _one_key(self):
        return _exactly_one(self, ("product_id", "ean"))


class CompareBasketInput(_Input):
    items: list[_BasketItemInput] = Field(
        min_length=1,
        max_length=30,
        description="Shopping list with up to 30 items.",
    )
    city: str = Field(
        min_length=1,
        max_length=120,
        description="City name. Required, because a basket is compared inside one city. Accents and letter case are ignored.",
        examples=["Marilia"],
    )
    max_age_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description="Prices collected more than this many days ago are not used.",
    )


class GetPriceHistoryInput(_Input):
    product_id: int | None = Field(
        default=None,
        ge=1,
        description="Product id from search_products. Send exactly one of product_id or ean.",
        examples=[1024],
    )
    ean: str | None = Field(
        default=None,
        pattern=r"^\d{8,14}$",
        description="Barcode with 8 to 14 digits. A 12-digit UPC is also matched as GTIN-13.",
        examples=["012345678905"],
    )
    days: int = Field(default=30, ge=1, le=365, description="Length of the series in days (1 to 365).")
    city: str | None = Field(
        default=None,
        max_length=120,
        description="City name. Accents and letter case are ignored.",
        examples=["Marilia"],
    )
    state: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Two-letter state code.",
        examples=["SP"],
    )
    chain: str | None = Field(
        default=None,
        max_length=120,
        description="Retail chain name to filter by.",
        examples=["Atacadao"],
    )

    @model_validator(mode="after")
    def _one_key(self):
        return _exactly_one(self, ("product_id", "ean"))


class ListBasketsInput(_Input):
    """Sem parâmetros além de country e context."""


class GetBasketInput(_Input):
    slug: str = Field(
        min_length=1,
        max_length=100,
        description="Basket slug from list_baskets.",
        examples=["family-of-three"],
    )
    city: str | None = Field(
        default=None,
        max_length=120,
        description="City name. Accents and letter case are ignored.",
        examples=["Marilia"],
    )
    state: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Two-letter state code.",
        examples=["SP"],
    )


class GetPriceIndexInput(_Input):
    period: Literal["7d", "30d", "90d"] = Field(
        default="30d",
        description="Length of the index series: 7d, 30d or 90d.",
    )
    category: str | None = Field(
        default=None,
        max_length=100,
        description="Category slug from list_categories.",
        examples=["grains"],
    )
    city: str | None = Field(
        default=None,
        max_length=120,
        description="City name. Send city, state or category.",
        examples=["Marilia"],
    )
    state: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Two-letter state code. Send city, state or category.",
        examples=["SP"],
    )
    top: int = Field(default=10, ge=1, le=50, description="How many top increases and top decreases to return (1 to 50).")

    @model_validator(mode="after")
    def _scope(self):
        if not (self.city or self.state or self.category):
            raise ValueError("Send at least one of: city, state, category.")
        return self


class ListLocationsInput(_Input):
    state: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Two-letter state code to filter by.",
        examples=["SP"],
    )


class ListStoresInput(_Input):
    city: str | None = Field(
        default=None,
        max_length=120,
        description="City name. Accents and letter case are ignored.",
        examples=["Marilia"],
    )
    state: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Two-letter state code.",
        examples=["SP"],
    )
    chain: str | None = Field(
        default=None,
        max_length=120,
        description="Retail chain name to filter by.",
        examples=["Atacadao"],
    )


class ListCategoriesInput(_Input):
    """Sem parâmetros além de country e context."""


# ---------------------------------------------------------------- saída: blocos

class _Envelope(BaseModel):
    """Campos que toda resposta traz."""

    model_config = ConfigDict(populate_by_name=True)

    as_of: str = Field(
        description="Time of the response in UTC, ISO 8601 with a 'Z' suffix.",
        examples=["2026-11-21T02:30:00Z"],
    )
    country: str = Field(
        description="Country of all data in this response.",
        examples=["BR"],
    )
    currency: str = Field(
        description="ISO 4217 currency of every price in this response.",
        examples=["BRL"],
    )
    attribution: str = Field(
        description="Source credit to show together with the data.",
        examples=[f"Prices observed on retailers' public websites by {SITE_HOST}"],
    )
    jsonld_context: str | None = Field(
        default=None,
        alias="@context",
        description="JSON-LD context URL. Present only when context=true.",
    )


class _Listed(_Envelope):
    """Resposta com uma lista principal, que pode ser cortada para caber no teto de tamanho."""

    truncated: bool = Field(
        default=False,
        description="True when the main list was cut to fit the 25,000-character response limit.",
    )


class StoreRef(BaseModel):
    store_id: int = Field(description="Internal store id.", examples=[17])
    chain: str = Field(description="Retail chain name.", examples=["Atacadao"])
    unit: str = Field(description="Store unit (branch) name inside the chain.", examples=["Marilia"])
    city: str = Field(description="City where the store is.", examples=["Marilia"])
    state: str = Field(description="Two-letter state code.", examples=["SP"])
    country: str = Field(description="Country code of the store.", examples=["BR"])
    timezone: str = Field(
        description="IANA time zone of the store. Used to express captured_at in local time.",
        examples=["America/Sao_Paulo"],
    )


class ProductInfo(BaseModel):
    product_id: int = Field(description="Product id, to use in other tools.", examples=[1024])
    ean: str | None = Field(
        description="GTIN-13 barcode, or null when the product has none.",
        examples=["7891000100103"],
    )
    ean_verified: bool = Field(
        description="True when the product has a barcode. Products without one were matched by name and may be merged wrongly.",
    )
    name: str = Field(description="Product name.", examples=["White rice type 1 5kg"])
    brand: str | None = Field(description="Brand name, or null when unknown.", examples=["Example Brand"])
    size: str | None = Field(
        description="Package size as printed by the store, for example '5kg'.",
        examples=["5kg"],
    )
    category: str | None = Field(description="Category name.", examples=["Grains"])
    url: str = Field(
        description=f"Product page on {SITE_HOST}.",
        examples=[f"{BRAND['site_url']}/produto/white-rice-5kg"],
    )


class SearchProduct(ProductInfo):
    min_price: float | None = Field(
        description="Lowest current price in the chosen place, in the response currency. Null without a current price.",
        examples=[24.9],
    )
    max_price: float | None = Field(
        description="Highest current price in the chosen place, in the response currency. Null without a current price.",
        examples=[31.5],
    )
    store_count: int = Field(description="Number of stores with a current price for this product.", examples=[4])
    last_seen: str | None = Field(
        description="When the product was last seen in any store, ISO 8601 with offset.",
        examples=["2026-11-20T23:30:00-03:00"],
    )


class PriceSummary(BaseModel):
    min: float | None = Field(description="Lowest current price.", examples=[24.9])
    median: float | None = Field(description="Median current price.", examples=[27.9])
    max: float | None = Field(description="Highest current price.", examples=[31.5])
    store_count: int = Field(description="Number of stores in the summary.", examples=[4])
    cheapest_store: StoreRef | None = Field(description="Store with the lowest current price, or null.")


class PriceItem(BaseModel):
    store: StoreRef = Field(description="Store that sells the product.")
    price: float = Field(description="Current shelf price on the store website, in the response currency.", examples=[24.9])
    price_regular: float | None = Field(
        description="Regular price without the promotion, or null when none is shown.",
        examples=[27.9],
    )
    is_promotion: bool = Field(description="True when the price is a promotional price.")
    unit_price: float | None = Field(
        description="Price per unit of measure (per kg or per L), or null when the size cannot be read.",
        examples=[4.98],
    )
    unit_price_basis: Literal["kg", "L", "unit"] | None = Field(
        description="Unit of unit_price: kg, L or unit. Null together with unit_price.",
        examples=["kg"],
    )
    captured_at: str = Field(
        description="When the price was collected, ISO 8601 with the store offset.",
        examples=["2026-11-20T23:30:00-03:00"],
    )
    age_hours: float = Field(description="Hours between captured_at and the response time, one decimal.", examples=[36.5])
    store_url: str | None = Field(description="Product page on the store website.", examples=["https://store.example/p/1024"])


class HistoryPoint(BaseModel):
    date: str = Field(description="Calendar date of the observation, YYYY-MM-DD.", examples=["2026-11-20"])
    price: float = Field(description="Median price on that date, in the response currency.", examples=[24.9])


class Extreme(BaseModel):
    value: float = Field(description="Price value.", examples=[22.5])
    date: str = Field(description="Calendar date of the price, YYYY-MM-DD.", examples=["2026-10-02"])
    store: StoreRef | None = Field(description="Store where the price was seen, or null.")


class SeriesPoint(BaseModel):
    date: str = Field(description="Calendar date, YYYY-MM-DD. Days without data are not included.", examples=["2026-11-20"])
    min: float = Field(description="Lowest price that day.", examples=[24.5])
    median: float = Field(description="Median price that day.", examples=[24.9])
    max: float = Field(description="Highest price that day.", examples=[26.0])
    store_count: int = Field(description="Number of stores with a price that day.", examples=[3])


class HistorySummary(BaseModel):
    first: HistoryPoint | None = Field(description="Earliest observation in the period.")
    last: HistoryPoint | None = Field(description="Latest observation in the period.")
    change_pct: float | None = Field(
        description="Percent change from first to last observation, one decimal. Null when there are fewer than two observations.",
        examples=[-3.2],
    )
    lowest: Extreme | None = Field(description="Lowest price in the period, with date and store.")
    highest: Extreme | None = Field(description="Highest price in the period, with date and store.")


class MatchedProduct(BaseModel):
    input: str = Field(description="The query text sent for this basket item.", examples=["white rice 5kg"])
    product: ProductInfo = Field(description="Product matched by the first search result. Check it before you trust the total.")


class StoreTotal(BaseModel):
    store: StoreRef = Field(description="Store of this total.")
    total: float = Field(description="Sum of the prices of the items this store has, in the response currency.", examples=[182.4])
    items_found: int = Field(description="Number of basket items this store has a current price for.", examples=[9])
    items_missing: list[str] = Field(description="Basket items without a current price in this store, as sent by the caller.")


class BestStore(BaseModel):
    store: StoreRef = Field(description="Store with the lowest total.")
    total: float = Field(description="Total of this store, in the response currency.", examples=[182.4])
    complete: bool = Field(description="True when this store has a price for every item. False when no store has them all.")


class SplitItem(BaseModel):
    input: str = Field(description="Basket item as sent by the caller.", examples=["white rice 5kg"])
    store_id: int = Field(description="Store with the lowest price for this item in the split.", examples=[17])
    price: float = Field(description="Price of this item in that store, in the response currency.", examples=[24.9])


class BestSplit(BaseModel):
    stores: list[StoreRef] = Field(description="Up to two stores. One store means buying everything in one place is cheapest.")
    total: float = Field(description="Total when each item is bought in its cheaper store of the pair.", examples=[171.0])
    items: list[SplitItem] = Field(description="Where each item is bought in the split.")


class BasketInfo(BaseModel):
    slug: str = Field(description="Basket slug, to use in get_basket.", examples=["family-of-three"])
    name: str = Field(description="Basket name.", examples=["Family of three"])
    description: str = Field(description="What the basket contains and who it is for.")
    items_count: int = Field(description="Number of items in the basket.", examples=[30])


class BasketItemOut(BaseModel):
    product: ProductInfo = Field(description="Product used for this basket item.")
    quantity: int = Field(description="Units of this item in the basket.", examples=[2])
    unit_price: float = Field(description="Current price of one unit in the city or state, in the response currency.", examples=[24.9])
    subtotal: float = Field(description="unit_price multiplied by quantity.", examples=[49.8])
    substituted: bool = Field(description="Always false. Items without a price are listed in items_missing, never replaced.")


class LocationInfo(BaseModel):
    state: str = Field(description="Two-letter state code.", examples=["SP"])
    city: str = Field(description="City name.", examples=["Marilia"])
    store_count: int = Field(description="Number of monitored stores in the city.", examples=[12])
    chain_count: int = Field(description="Number of retail chains in the city.", examples=[5])
    freshest_at: str | None = Field(
        description="Most recent collection in the city, ISO 8601 with offset. Null when there is none.",
        examples=["2026-11-20T23:30:00-03:00"],
    )


class StoreInfo(StoreRef):
    products_with_price: int = Field(description="Products this store has a price for, from its own collection.", examples=[850])
    last_collected_at: str | None = Field(
        description="Last own price collection, ISO 8601 with offset. Null when there is none.",
        examples=["2026-11-20T23:30:00-03:00"],
    )
    median_age_hours: float | None = Field(
        description="Median age of the store's own prices in hours, one decimal. Null when there are none.",
        examples=[40.2],
    )


class CategoryInfo(BaseModel):
    slug: str = Field(description="Category slug, to use as the category filter.", examples=["grains"])
    name: str = Field(description="Category name.", examples=["Grains"])
    product_count: int = Field(description="Number of products in the category.", examples=[1200])


class IndexPoint(BaseModel):
    date: str = Field(description="Calendar date of the index point, YYYY-MM-DD.", examples=["2026-11-20"])
    index: float = Field(description="Index value, with the first point of the series equal to 100.", examples=[99.5])


class IndexMover(BaseModel):
    product: ProductInfo = Field(description="Product whose price moved.")
    change_pct: float = Field(description="Percent change of the matched price over the period, one decimal.", examples=[12.4])


class ErrorBody(BaseModel):
    code: str = Field(description="Machine-readable error code, for example invalid_parameter or product_not_found.", examples=["invalid_parameter"])
    message: str = Field(description="What went wrong, in one sentence.", examples=["The limit must be between 1 and 50."])
    hint: str = Field(description="What the caller can do next. Always actionable.", examples=["Use limit between 1 and 50."])
    docs: str = Field(description="Link to the API documentation.", examples=[BRAND["docs_url"]])


class ErrorResponse(BaseModel):
    error: ErrorBody = Field(description="Error details.")


# ---------------------------------------------------------------- saída: ferramentas

class SearchProductsOutput(_Listed):
    products: list[SearchProduct] = Field(description="Matching products, best match first.")
    next_cursor: str | None = Field(description="Pass as cursor to get the next page. Null on the last page.")


class GetProductOutput(_Envelope):
    product: ProductInfo = Field(description="Product details.")
    price_summary: PriceSummary | None = Field(
        description="Summary of current prices in the chosen city or state. Null when there is no current price.",
    )


class GetPricesOutput(_Listed):
    product: ProductInfo = Field(description="Product whose prices are listed.")
    prices: list[PriceItem] = Field(description="Current prices, cheapest first.")
    summary: PriceSummary = Field(description="Summary of the listed prices.")
    omitted_stale: int = Field(description="Prices left out because they are older than max_age_days.", examples=[3])
    omitted_replicated: int = Field(description="Prices left out because they were copied from another store.", examples=[0])


class CompareBasketOutput(_Listed):
    stores: list[StoreTotal] = Field(description="Total per store, cheapest first.")
    matched_products: list[MatchedProduct] = Field(description="Products matched for basket items sent as query. Check them.")
    best_store: BestStore | None = Field(description="Cheapest single store. Null when no store has any item.")
    best_split: BestSplit | None = Field(description="Cheapest total buying in at most two stores. Null when no store has any item.")


class GetPriceHistoryOutput(_Listed):
    product: ProductInfo = Field(description="Product of the series.")
    series: list[SeriesPoint] = Field(description="Daily series, oldest first. Observations are weekly, so most days are missing.")
    summary: HistorySummary = Field(description="First and last observation, change over the period, and the extremes.")


class ListBasketsOutput(_Listed):
    baskets: list[BasketInfo] = Field(description="Predefined baskets.")


class GetBasketOutput(_Envelope):
    slug: str = Field(description="Basket slug.", examples=["family-of-three"])
    name: str = Field(description="Basket name.", examples=["Family of three"])
    city: str | None = Field(description="City of the cost, or null when the cost is for a state.", examples=["Marilia"])
    state: str | None = Field(description="Two-letter state code of the cost.", examples=["SP"])
    cost: float | None = Field(description="Current cost of the items that have a price, in the response currency. Null when none has.", examples=[412.7])
    change_7d_pct: float | None = Field(
        description="Percent change of the cost over 7 days, on the same items priced on both dates. Null without a comparison.",
        examples=[1.8],
    )
    change_30d_pct: float | None = Field(
        description="Percent change of the cost over 30 days, on the same items priced on both dates. Null without a comparison.",
        examples=[-0.6],
    )
    items: list[BasketItemOut] = Field(description="Basket items that have a current price.")
    items_missing: list[str] = Field(description="Names of basket items with no current price. They are not replaced.")
    coverage: float = Field(description="Share of basket items with a price, from 0 to 1.", examples=[0.93])


class GetPriceIndexOutput(_Envelope):
    method: Literal["matched_model_geomean_v0"] = Field(
        description="Index method. matched_model_geomean_v0: same products in the same stores, geometric mean of price ratios.",
        examples=["matched_model_geomean_v0"],
    )
    period: str = Field(description="Period of the series: 7d, 30d or 90d.", examples=["30d"])
    index_series: list[IndexPoint] = Field(description="Weekly index points, oldest first.")
    change_pct: float | None = Field(description="Percent change of the index over the period. Null without enough points.", examples=[1.2])
    products_in_sample: int = Field(description="Products in the sample.", examples=[1500])
    pairs_in_sample: int = Field(description="Product and store pairs used in the sample.", examples=[4200])
    pairs_dropped_outliers: int = Field(description="Pairs dropped because their price ratio was outside 0.2 to 5.", examples=[12])
    top_increases: list[IndexMover] = Field(description="Products with the largest price increases.")
    top_decreases: list[IndexMover] = Field(description="Products with the largest price decreases.")
    methodology_url: str = Field(
        description="Page that explains how the index is built.",
        examples=[f"{BRAND['site_url']}/metodologia"],
    )


class ListLocationsOutput(_Listed):
    locations: list[LocationInfo] = Field(description="Covered cities.")


class ListStoresOutput(_Listed):
    stores: list[StoreInfo] = Field(description="Monitored stores.")


class ListCategoriesOutput(_Listed):
    categories: list[CategoryInfo] = Field(description="Product categories.")


# ---------------------------------------------------------------- registro das ferramentas

@dataclass(frozen=True)
class ToolSpec:
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]


TOOL_SPECS: dict[str, ToolSpec] = {
    "search_products": ToolSpec(
        "Search supermarket products by name, brand or barcode (GTIN/EAN). Call this first to get a `product_id`.",
        SearchProductsInput,
        SearchProductsOutput,
    ),
    "get_product": ToolSpec(
        "Product details by `product_id`, `ean` or `slug`, with a current price summary.",
        GetProductInput,
        GetProductOutput,
    ),
    "get_prices": ToolSpec(
        'Current price of one product in each store of a city or state, cheapest first. Answers "how much is X in Y" and "where is it cheapest".',
        GetPricesInput,
        GetPricesOutput,
    ),
    "compare_basket": ToolSpec(
        "Total cost of a shopping list per store in one city, the cheapest store, and the best split across two stores.",
        CompareBasketInput,
        CompareBasketOutput,
    ),
    "get_price_history": ToolSpec(
        "Price series of one product and its change over the period. Observations are weekly; days without data are omitted, never interpolated.",
        GetPriceHistoryInput,
        GetPriceHistoryOutput,
    ),
    "list_baskets": ToolSpec(
        "Predefined baskets (family of three, barbecue, ...).",
        ListBasketsInput,
        ListBasketsOutput,
    ),
    "get_basket": ToolSpec(
        "Current cost of a predefined basket in a city or state, with 7- and 30-day change and item coverage.",
        GetBasketInput,
        GetBasketOutput,
    ),
    "get_price_index": ToolSpec(
        "Matched-model price index by category and location, with top increases and decreases and the sample size.",
        GetPriceIndexInput,
        GetPriceIndexOutput,
    ),
    "list_locations": ToolSpec(
        "Covered countries, states and cities, with store count and data freshness. Call this when unsure whether a city is covered.",
        ListLocationsInput,
        ListLocationsOutput,
    ),
    "list_stores": ToolSpec(
        "Monitored stores and how fresh each one is.",
        ListStoresInput,
        ListStoresOutput,
    ),
    "list_categories": ToolSpec(
        "Product categories available for filtering.",
        ListCategoriesInput,
        ListCategoriesOutput,
    ),
}
