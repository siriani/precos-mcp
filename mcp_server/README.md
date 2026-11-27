# Precos MCP server

Read-only MCP server for Brazilian supermarket prices observed weekly on retailer sites. It exposes
11 tools (product search, current prices, baskets, price history, price index, locations, stores and
categories). Every payload carries `as_of`, `country`, `currency` and `attribution`; every price
carries `captured_at` (ISO 8601 with offset) and `age_hours`. Output is always in English.

Nothing here writes to the database and nothing triggers a collection.

## Install

From the repository root:

```bash
python -m venv .venv-mcp
.venv-mcp/bin/pip install -r mcp_server/requirements.txt
```

The dependencies are kept apart from the site's `requirements.txt` on purpose: the website does not
import `mcp` or `pydantic`, and only serves the generated `mcp_server/openapi.json`.

## stdio

Local use, no API key: whoever runs it already has database access.

```bash
PYTHONPATH=. .venv-mcp/bin/python -m mcp_server
```

A client configuration looks like:

```json
{
  "mcpServers": {
    "precos": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/precos",
      "env": { "PRECOS_DB_RO_URL": "mysql+pymysql://user:pass@host/db" }
    }
  }
}
```

Keep stdout for the protocol: logs go to stderr.

## HTTP

```bash
PYTHONPATH=. .venv-mcp/bin/python -m mcp_server --http --host 0.0.0.0 --port 8000
```

- Endpoint: `http://127.0.0.1:8000/mcp` (Streamable HTTP, stateless, JSON responses).
- `/healthz` is open; everything else needs `Authorization: Bearer pk_live_...`.
- `python -m mcp_server --list-tools` prints the 11 tool names and exits without touching the database.

Docker (separate stack, container `precos-mcp`, published on port 5056):

```bash
cp infra/mcp.env.example infra/mcp.env   # fill in on the server, never commit it
docker compose -f infra/docker-compose.mcp.yml up -d --build
```

## Environment variables

| Variable | Meaning |
|---|---|
| `PRECOS_DB_RO_URL` | SQLAlchemy URL of the read-only database user. Falls back to `models.DATABASE_URL` when empty. |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB` | Redis used for rate limiting and usage metering. |
| `MCP_ALLOWED_HOSTS` | Comma-separated `Host` header values accepted by the HTTP transport. |
| `PRECOS_SEARCH_FULLTEXT` | `1` switches product search to `FULLTEXT`, after the index migration is applied; `0` uses `LIKE`. |

There are no secrets in this repository: real values live in the vault and are injected by
`infra/mcp.env` (git-ignored) or by the MCP client. Without the `api_keys`/`api_usage` tables the HTTP
transport answers `auth_unavailable`; the stdio mode is unaffected.

## Tests

```bash
python -m compileall -q mcp_server && PYTHONPATH=. python -m pytest -q tests/test_mcp_*.py
```
