"""Servidor MCP do Precos: as 11 ferramentas de consulta, por stdio ou Streamable HTTP.

Esquemas vem de schemas.TOOL_SPECS; a consulta em si fica em queries.call.
No modo HTTP toda chamada exige chave (Authorization: Bearer), com limite e medicao (auth.py).
No stdio nada vai para o stdout alem do protocolo: logs vao para o stderr.
"""
import contextvars
import inspect
import json
import logging
import os
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from mcp_server import auth, db, demo, queries
from mcp_server.brand import BRAND
from mcp_server.common import QueryError
from mcp_server.schemas import TOOL_SPECS

log = logging.getLogger("precos.mcp")

SERVER_NAME = BRAND["slug"]
SERVER_INSTRUCTIONS = (
    BRAND["description_short"] + " "
    "Call search_products first to get a product_id. Methodology: precos://methodology."
)

METHODOLOGY = """# Methodology

- Prices are collected weekly from the public websites of the monitored stores.
- Prices are in BRL. Timestamps carry the store's UTC offset.
- Observations are weekly. Days without data are omitted from series, never interpolated.
- A price copied from another store is marked as replicated and is left out unless a tool asks for it.
- Coverage is Brazil today. See precos://coverage for the cities and freshness.
"""

# Contexto da chave da requisicao HTTP; no stdio fica None e nada e medido.
_key_ctx: contextvars.ContextVar = contextvars.ContextVar("precos_mcp_key", default=None)


def _error_payload(code: str, message: str, hint: str) -> str:
    return json.dumps({"error": {"code": code, "message": message, "hint": hint, "docs": auth.DOCS_URL}},
                      ensure_ascii=False)


def _record(tool: str, ok: bool) -> None:
    info = _key_ctx.get()
    if info is not None:
        auth.meter(info.id, tool, ok)


def _signature_for(input_model, output_model) -> inspect.Signature:
    """Assinatura keyword-only a partir dos campos do modelo de entrada (descricao e exemplos preservados)."""
    params = []
    for name, field in input_model.model_fields.items():
        annotation = Annotated[
            field.annotation,
            Field(description=field.description, examples=field.examples),
            *field.metadata,
        ]
        default = inspect.Parameter.empty if field.is_required() else field.default
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation))
    return inspect.Signature(params, return_annotation=output_model)


def _make_tool(name: str, spec):
    input_model, output_model = spec.input_model, spec.output_model

    def tool(**kwargs: Any):
        try:
            params = input_model.model_validate(kwargs).model_dump(exclude_unset=True)
        except ValidationError as exc:
            detail = "; ".join(str(err.get("msg", "")) for err in exc.errors())
            raise ToolError(_error_payload("invalid_parameter", detail or "Invalid parameters.",
                                           "Check the tool input schema.")) from exc
        try:
            payload = queries.call(name, params)
        except QueryError as exc:
            _record(name, ok=False)
            raise ToolError(_error_payload(exc.code, exc.message, exc.hint)) from exc
        except Exception:
            _record(name, ok=False)
            raise
        _record(name, ok=True)
        return output_model.model_validate(payload)

    tool.__name__ = name
    tool.__signature__ = _signature_for(input_model, output_model)
    tool.__annotations__ = {p: a.annotation for p, a in tool.__signature__.parameters.items()}
    tool.__annotations__["return"] = output_model
    return tool


def build_server() -> MCPServer:
    mcp = MCPServer(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    for name, spec in TOOL_SPECS.items():
        mcp.add_tool(
            _make_tool(name, spec),
            name=name,
            description=spec.description,
            annotations=ToolAnnotations(readOnlyHint=True),
            structured_output=True,
        )

    @mcp.resource("precos://methodology", name="methodology", mime_type="text/markdown",
                  description="How prices are collected, their frequency and limits.")
    def methodology() -> str:
        return METHODOLOGY

    @mcp.resource("precos://coverage", name="coverage", mime_type="application/json",
                  description="Covered cities and states, with store count and data freshness.")
    def coverage() -> str:
        return json.dumps(queries.call("list_locations", {}), ensure_ascii=False, indent=2)

    return mcp


# ---------------------------------------------------------------- HTTP: chave por cabecalho

def _bearer(scope) -> str | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == b"authorization":
            value = raw_value.decode("latin-1").strip()
            if value.lower().startswith("bearer "):
                return value[7:].strip() or None
            return None
    return None


def _authorize(token: str | None):
    """Chamada sincrona (roda em threadpool): confere a chave e aplica o limite."""
    with db.session_scope() as session:
        info = auth.verify_key(token, session)
    auth.check_rate(info)
    return info


async def _send_json(send, status: int, body: dict, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    raw_headers = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
    raw_headers += headers or []
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": data})


class KeyMiddleware:
    """ASGI: exige Authorization: Bearer em tudo, menos /healthz. Guarda a chave no contextvar."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/healthz":
            body = {"status": "ok", "mode": "demo"} if demo.enabled() else {"status": "ok"}
            await _send_json(send, 200, body)
            return
        if demo.enabled():
            # Demo: sem chave, sem Redis e sem medicao; so o limite em memoria (60 por minuto).
            try:
                demo.check_rate()
            except auth.ApiError as exc:
                headers = [(b"retry-after", str(exc.retry_after).encode())] if exc.retry_after is not None else []
                await _send_json(send, exc.status, exc.to_dict(), headers)
                return
            await self.app(scope, receive, send)
            return
        try:
            info = await run_in_threadpool(_authorize, _bearer(scope))
        except auth.ApiError as exc:
            headers = []
            if exc.status == 401:
                headers.append((b"www-authenticate", b"Bearer"))
            if exc.retry_after is not None:
                headers.append((b"retry-after", str(exc.retry_after).encode()))
            await _send_json(send, exc.status, exc.to_dict(), headers)
            return
        except Exception as exc:
            log.warning("auth: falha inesperada (%s)", type(exc).__name__)
            err = auth.ApiError("auth_unavailable", 503, "Authentication is temporarily unavailable.",
                                "Retry in a few seconds.")
            await _send_json(send, 503, err.to_dict())
            return
        token = _key_ctx.set(info)
        try:
            await self.app(scope, receive, send)
        finally:
            _key_ctx.reset(token)


def build_http_app(host: str = "127.0.0.1"):
    """App ASGI do modo HTTP: sem estado, resposta JSON, em /mcp. Hosts aceitos em MCP_ALLOWED_HOSTS."""
    allowed = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed) if allowed else None
    starlette_app = build_server().streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=security,
        host=host,
    )
    return KeyMiddleware(starlette_app)
