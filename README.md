# Basketgraph: Brazilian supermarket prices over MCP and REST

Basketgraph gives AI agents prices of Brazilian supermarkets, through a read-only remote MCP server and REST API. It answers
"how much is X in Y" and "where is it cheapest". Prices are in BRL, and every price carries its capture time (`captured_at`)
and its age (`age_hours`). Coverage: 293 stores in 19 states of Brazil, as of 2026-11-20.
Each product is scheduled once a week, but in practice only 29% of current prices are under 7 days old (2026-11-24; see "Data coverage and caveats").

Basketgraph is the product name, run by Nallaworks. The repo, the URLs and the `precos://` resources use the older name "Precos".

- Website: https://precos.lapps.studio
- REST base: https://precos.lapps.studio/api/v1 (OpenAPI 3.1: https://precos.lapps.studio/api/v1/openapi.json)
- Source (MCP server and demo, GPL-3.0): https://github.com/siriani/precos-mcp

## Try it first (no key)

The demo runs on your machine, with no key and no signup. Tested on Linux with Python 3.11:

```bash
git clone https://github.com/siriani/precos-mcp.git && cd precos-mcp
python -m venv .venv-mcp && .venv-mcp/bin/pip install -r mcp_server/requirements.txt
PYTHONPATH=. .venv-mcp/bin/python -m mcp_server --demo --http --port 5057
```

`http://localhost:5057/mcp` then serves the tools over synthetic data. Point Claude Code at it with
`claude mcp add --transport http precos-demo http://localhost:5057/mcp` (no header). Every response is marked: `attribution` starts
with `DEMO DATA (synthetic, not real prices).` Use it to try the tools, never to decide anything.

## Get a key

Free and instant: generate a key at https://precos.lapps.studio/chave?utm_source=readme (no email, no sign-up; the key is shown once). One key works for MCP and REST, and the limits below are shared between them.

- Keys are issued by the form, with a daily limit per IP and overall. If the form is full, try again the next day. Never post personal data or keys in an issue; to revoke a key, open an issue at https://github.com/siriani/precos-mcp/issues with only the key prefix (first 12 characters).
- The free plan is 30 calls per minute and 100 per day, per key. There is no paid plan yet: if that does not fit, ask on https://precos.lapps.studio/contato?utm_source=readme and say what you are building.

## Connect (remote MCP)

Streamable HTTP at `https://mcp-precos.lapps.studio/mcp`. Authenticate with `Authorization: Bearer <key>`; no other auth header is supported.
Claude Code (one command):

```bash
claude mcp add --transport http precos https://mcp-precos.lapps.studio/mcp --header "Authorization: Bearer <key>"
```

The same server as JSON, in the Claude Code format. other clients name the fields differently, but all any client needs is the URL and the header:

```json
{
  "mcpServers": {
    "precos": {
      "type": "http",
      "url": "https://mcp-precos.lapps.studio/mcp",
      "headers": { "Authorization": "Bearer <key>" }
    }
  }
}
```

## Tools

All tools are read-only.

| Tool | What it returns |
|---|---|
| `search_products` | Search supermarket products by name, brand or barcode (GTIN/EAN). Call this first to get a `product_id`. |
| `get_product` | Product details by `product_id`, `ean` or `slug`, with a current price summary. |
| `get_prices` | Current price of one product in each store of a city or state, cheapest first. Prices older than `max_age_days` (default 30) are left out and counted in `omitted_stale`. |
| `compare_basket` | Total cost of a shopping list per store in one city, the cheapest store, and the cheapest way to split the list between two stores. |
| `get_price_history` | Price series of one product and its change over the period. Observations are at most weekly per store and often sparser; days without data are omitted, never interpolated. |
| `list_baskets` | Predefined baskets (family of three, barbecue, ...). |
| `get_basket` | Current cost of a predefined basket in a city or state, with 7- and 30-day change and item coverage. |
| `get_price_index` | Matched-model price index (compares only products found in both periods) by category and location, with top increases and decreases and the sample size. |
| `list_locations` | Covered states and cities (Brazil only), with store count and data freshness. Call this when unsure whether a city is covered. |
| `list_stores` | Monitored stores and how fresh each one is. |
| `list_categories` | Product categories available for filtering. |

## Example call

A trimmed response from the demo server. Prices, stores and brands are synthetic; the cities are real. `omitted_stale` counts prices older than `max_age_days`, `omitted_replicated` counts prices
copied from another store, and `truncated` is true when `limit` cut the list.

`get_prices` with `{"product_id": 1, "city": "Marilia"}` (accents and case in city names are ignored; "arroz" is rice, "Mercearia" is grocery; names and
categories are in Portuguese):

```json
{
  "as_of": "2026-11-24T11:32:11Z",
  "country": "BR",
  "currency": "BRL",
  "attribution": "DEMO DATA (synthetic, not real prices). Retail prices observed on retailer websites by Basketgraph (https://precos.lapps.studio). Methodology: precos://methodology.",
  "product": {
    "product_id": 1,
    "ean": "2000000000015",
    "ean_verified": true,
    "name": "Arroz branco tipo 1",
    "brand": "Sol Dourado",
    "size": "5kg",
    "category": "Mercearia",
    "url": "https://precos.lapps.studio/produto/demo-01"
  },
  "prices": [
    {
      "price": 24.15,
      "price_regular": null,
      "is_promotion": false,
      "captured_at": "2026-11-23T08:32:11-03:00",
      "age_hours": 24.0,
      "store": {
        "store_id": 2,
        "chain": "Rede Alfa",
        "unit": "Centro",
        "city": "Marília",
        "state": "SP",
        "country": "BR",
        "timezone": "America/Sao_Paulo"
      },
      "unit_price": 4.83,
      "unit_price_basis": "kg",
      "store_url": null
    },
    {
      "price": 24.65,
      "price_regular": null,
      "is_promotion": false,
      "captured_at": "2026-11-23T08:32:11-03:00",
      "age_hours": 24.0,
      "store": {
        "store_id": 4,
        "chain": "Rede Beta",
        "unit": "Centro",
        "city": "Marília",
        "state": "SP",
        "country": "BR",
        "timezone": "America/Sao_Paulo"
      },
      "unit_price": 4.93,
      "unit_price_basis": "kg",
      "store_url": null
    }
  ],
  "summary": {
    "min": 24.15,
    "median": 24.4,
    "max": 24.65,
    "store_count": 2,
    "cheapest_store": {
      "store_id": 2,
      "chain": "Rede Alfa",
      "unit": "Centro",
      "city": "Marília",
      "state": "SP",
      "country": "BR",
      "timezone": "America/Sao_Paulo"
    }
  },
  "omitted_stale": 0,
  "omitted_replicated": 0,
  "truncated": false
}
```

## Same data over REST

```bash
curl -H "Authorization: Bearer <key>" 'https://precos.lapps.studio/api/v1/products/search?query=arroz&city=Marilia'
```

Take a `product_id` from that response and call `https://precos.lapps.studio/api/v1/prices?product_id=<id>&city=Marilia`. The other
endpoints are in the OpenAPI spec above. Responses include `country`, `currency`, `as_of` and `attribution`.

## Rate limits and errors

Errors are `{"error": {"code": "...", "message": "...", "hint": "...", "docs": "..."}}` with a matching HTTP status.

- Free plan: 30 calls per minute and 100 per day, per key.
- 401 `unauthorized`: key missing or invalid. Send only `Authorization: Bearer <key>`.
- 429 `rate_limited`: minute or day limit hit. REST sends a `Retry-After` header with the seconds to wait. MCP says how many seconds to wait when the minute limit is hit; the day limit resets at 00:00 UTC.
- 503 `rate_limiter_unavailable`: the limiter is down, so the request was rejected. Retry with a delay.
- 504 `query_timeout`: narrow the filter and retry.

## Data coverage and caveats

- Each product is scheduled once a week, at a random slot, and collected in the stores that sell it. In practice most prices are not renewed every week. On 2026-11-24: 29% of current prices were less than 7 days old, 70% less than 30 days, and the other 30% were older than 30 days. There is no continuous feed, no intraday history, and days without data are omitted, never interpolated.
- `get_prices` leaves out prices older than `max_age_days`, which is 30 by default; on 2026-11-24 that leaves out about 30% of current prices. Pass a lower `max_age_days` for fresher prices only, and read `age_hours` on every price. `omitted_stale` tells you how many were left out.
- Prices are **advertised online prices** read from retailer websites, not in-store prices.
- The series starts in **March 2026**, so year-over-year comparisons are not possible yet. `list_locations` returns the current store count.
- Replicated prices (a price copied from another store, not observed by us) are excluded unless you set `include_replicated`. A product matched by name instead of barcode may be wrong; such a product has `ean_verified=false`.
- `precos://methodology` and `precos://coverage` are MCP resources (read them with `resources/read`); there is no web page for them yet.
- Brazil only, prices in BRL. No product images or descriptions through the API.

## License and contact

Free sample datasets, published separately, are under CC BY 4.0. Access through the API and the MCP server follows the Terms of use at https://precos.lapps.studio/termos#en (English summary): you may use the data in your own
product, analysis and agent answers, but not resell or redistribute the raw data. Attribute it with the line below, which is also the `attribution` field of every
answer. `precos://methodology` opens only in an MCP client; where a link cannot be opened, you may drop the `Methodology:` part:

```
Retail prices observed on retailer websites by Basketgraph (https://precos.lapps.studio). Methodology: precos://methodology.
```

Questions: https://precos.lapps.studio/contato or https://github.com/siriani/precos-mcp/issues. Code license: see `LICENSE`.
