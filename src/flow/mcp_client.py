"""Cliente MCP: permite que um agente use tools de servidores MCP externos (AgentConfig.mcp_servers).

Formato: {"nome": {"url": "http://host:9000/mcp"}}  ou  {"nome": {"command": "python", "args": ["srv.py"]}}
As tools aparecem para o agente como `nome.tool` e entram na allowlist por `nome.tool` ou `nome.*`.
"""
import asyncio
import threading


def _run(coro_fn):
    """Roda uma corrotina em thread própria (o Django aqui é síncrono; evita conflito de event loop)."""
    box = {}

    def target():
        try:
            box["v"] = asyncio.run(coro_fn())
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


def _client_for(cfg: dict):
    from fastmcp import Client

    if "url" in cfg:
        return Client(cfg["url"])
    return Client({"mcpServers": {"s": cfg}})


def list_tools(servers: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, cfg in (servers or {}).items():
        async def go(cfg=cfg):
            async with _client_for(cfg) as c:
                return await c.list_tools()

        try:
            for t in _run(go):
                out[f"{name}.{t.name}"] = {"server": name, "tool": t.name, "description": (t.description or "").split("\n")[0],
                                           "schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}}
        except Exception as e:  # noqa: BLE001 — servidor indisponível não derruba o agente
            out[f"{name}.__error__"] = {"server": name, "tool": "__error__", "description": f"indisponível: {e}", "schema": {}}
    return out


def allowed(allowlist: list[str], qualified: str) -> bool:
    server = qualified.split(".", 1)[0]
    return qualified in allowlist or f"{server}.*" in allowlist


def call_tool(servers: dict, qualified: str, args: dict) -> str:
    server, tool_name = qualified.split(".", 1)
    cfg = (servers or {}).get(server)
    if cfg is None:
        raise KeyError(f"servidor MCP '{server}' não configurado")

    async def go():
        async with _client_for(cfg) as c:
            r = await c.call_tool(tool_name, args)
            return "\n".join(getattr(x, "text", str(x)) for x in r.content)

    return _run(go)
