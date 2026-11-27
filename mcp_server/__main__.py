"""Entrada do servidor MCP: `python -m mcp_server` (stdio), `--http` (Streamable HTTP), `--list-tools`.

Logs vao sempre para o stderr. Sem --http, o stdout e exclusivo do protocolo stdio.
"""
import argparse
import logging
import os
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mcp_server", description="Precos MCP server.")
    parser.add_argument("--http", action="store_true",
                        help="serve Streamable HTTP at /mcp; every request needs 'Authorization: Bearer <key>'")
    parser.add_argument("--host", default=os.getenv("PRECOS_MCP_HOST", "127.0.0.1"),
                        help="HTTP bind address (default: PRECOS_MCP_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PRECOS_MCP_PORT", "8000")),
                        help="HTTP port (default: PRECOS_MCP_PORT or 8000)")
    parser.add_argument("--list-tools", action="store_true",
                        help="print the tool names and exit, without opening the database")
    parser.add_argument("--demo", action="store_true",
                        help="synthetic data, no key, no database and no Redis (same as PRECOS_DEMO=1)")
    args = parser.parse_args(argv)

    # Antes de qualquer import do pacote de consulta: a demo e decidida pela variavel de ambiente.
    if args.demo:
        os.environ["PRECOS_DEMO"] = "1"

    logging.basicConfig(stream=sys.stderr, level=os.getenv("PRECOS_MCP_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from mcp_server.schemas import TOOL_SPECS

    if args.list_tools:
        print("\n".join(TOOL_SPECS))
        return 0

    from mcp_server import demo
    from mcp_server.server import build_http_app, build_server

    if demo.enabled():
        logging.getLogger("precos.mcp").warning("demo mode: synthetic data, no key, no database, no Redis")

    if args.http:
        import uvicorn

        uvicorn.run(build_http_app(args.host), host=args.host, port=args.port, log_level="info")
    else:
        build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
