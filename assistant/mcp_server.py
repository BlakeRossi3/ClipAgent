"""Sterling's tools as a stdio MCP server, for the Claude Code backend (assistant/claude_code.py).

Run as ``python -m assistant.mcp_server`` by Claude Code (``--mcp-config``); one process per
agent run. Minimal JSON-RPC 2.0 over newline-delimited stdin/stdout (MCP stdio transport):
``initialize``, ``notifications/initialized``, ``ping``, ``tools/list``, ``tools/call``.

The process is told who it serves through environment variables set by ``claude_code``:

* ``CLIPAGENT_MCP_AGENT`` / ``CLIPAGENT_MCP_ALLOWED`` - the agent and its exact tool allowlist.
  Only those tools are listed, and :func:`assistant.tools.dispatch` refuses anything else (the
  same check as the in-process runtime; ``--allowedTools`` on the Claude Code side is a second lock).
* ``CLIPAGENT_MCP_CHANNEL`` / ``_SOURCE`` / ``_MESSAGE_ID`` - the turn's :class:`ToolContext`.
* ``CLIPAGENT_MCP_RUN_ID`` - the parent ``as_agent_runs`` row (``delegate`` links sub-agent runs to it).
* ``CLIPAGENT_MCP_CALLS`` - a JSONL file; every call (sub-agents' included) is appended so the
  parent can store the same audit trail as the in-process path.

Tool results are wrapped in ``<untrusted>`` exactly like the in-process loop. Nothing but
JSON-RPC is ever written to stdout (logging goes to stderr).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, BinaryIO

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "clipagent", "version": "1.0"}


class Server:
    def __init__(self, environ: dict[str, str] | None = None):
        env = dict(os.environ if environ is None else environ)
        self.agent = env.get("CLIPAGENT_MCP_AGENT") or "main"
        self.allowed = frozenset(x for x in (env.get("CLIPAGENT_MCP_ALLOWED") or "").split(",") if x)
        self.channel = env.get("CLIPAGENT_MCP_CHANNEL") or "system"
        self.source = env.get("CLIPAGENT_MCP_SOURCE") or "system"
        mid = env.get("CLIPAGENT_MCP_MESSAGE_ID") or ""
        self.message_id = int(mid) if mid.isdigit() else None
        rid = env.get("CLIPAGENT_MCP_RUN_ID") or ""
        self.run_id = int(rid) if rid.isdigit() else None
        self.calls_path = Path(env["CLIPAGENT_MCP_CALLS"]) if env.get("CLIPAGENT_MCP_CALLS") else None
        home = env.get("CLIPAGENT_MCP_HOME")
        if home:
            from assistant import claude_code

            claude_code.set_home(Path(home))

    # -- tools ------------------------------------------------------------------
    def tool_list(self) -> list[dict[str, Any]]:
        from assistant import tools

        out = [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
               for t in tools.available(sorted(self.allowed))]
        if "delegate" in self.allowed:
            from assistant import registry, runtime

            d = runtime._delegate_definition(registry.load_agents(skills=registry.load_skills()))
            out.append({"name": d["name"], "description": d["description"], "inputSchema": d["input_schema"]})
        return out

    def call(self, name: str, args: Any) -> dict[str, Any]:
        from assistant import runtime, tools

        ctx = tools.ToolContext(channel=self.channel, agent=self.agent, source=self.source,
                                message_id=self.message_id)
        if name == "delegate" and "delegate" in self.allowed:
            content, ok = runtime.delegate_via_mcp(args if isinstance(args, dict) else {}, ctx, self.run_id)
        else:
            res = tools.dispatch(name, args if args is not None else {}, ctx, allowed=self.allowed)
            content, ok = res.content, res.ok
        self._record(ctx.calls)
        return {"content": [{"type": "text", "text": runtime.wrap_untrusted(f"tool:{name}", content)}],
                "isError": not ok}

    def _record(self, calls: list[Any]) -> None:
        if self.calls_path is None or not calls:
            return
        with open(self.calls_path, "a", encoding="utf-8") as fh:
            for c in calls:
                fh.write(json.dumps(c.to_dict(), ensure_ascii=False, default=str) + "\n")

    # -- JSON-RPC -----------------------------------------------------------------
    def handle(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict):
            return _error(None, -32600, "invalid request")
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # a notification: never answered
            return None
        try:
            if method == "initialize":
                asked = str(params.get("protocolVersion") or "")
                return _ok(mid, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[1],
                                 "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO})
            if method == "ping":
                return _ok(mid, {})
            if method == "tools/list":
                return _ok(mid, {"tools": self.tool_list()})
            if method == "tools/call":
                return _ok(mid, self.call(str(params.get("name") or ""), params.get("arguments")))
            return _error(mid, -32601, f"method not found: {method}")
        except Exception as exc:  # a crash must answer, not hang Claude Code
            import logging

            logging.getLogger(__name__).exception("mcp request failed")
            return _ok(mid, {"content": [{"type": "text", "text": f"error: {type(exc).__name__}"}], "isError": True}) \
                if method == "tools/call" else _error(mid, -32603, type(exc).__name__)


def _ok(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin: BinaryIO, stdout: BinaryIO, server: Server | None = None) -> None:
    srv = server or Server()
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            reply: dict[str, Any] | None = _error(None, -32700, "parse error")
        else:
            reply = srv.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


def main() -> int:
    import logging

    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # anything printed by imported code must not corrupt the protocol
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    serve(sys.stdin.buffer, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
