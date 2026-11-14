"""One-shot model calls that need no tools and no conversation: the heartbeat's "anything to do?" check.

The API with the saved key (one cached client, so the connection is reused) on
``ASSISTANT_QUICK_MODEL``; without a key, a tool-less one-turn Claude Code run (``claude -p``);
the runtime's :class:`FakeBackend` in fake mode. Costs are recorded as ``assistant.<agent>`` usage
and count against the assistant's daily budget. Conversation is not done here: that is the warm
brain (``assistant/brain.py``).
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

import db
from assistant import runtime
from assistant.settings import get_assistant_settings

_backend: Any = None
_api_backend: Any = None
_lock = threading.Lock()


def set_backend(backend: Any) -> None:
    global _backend
    _backend = backend


def reset() -> None:
    global _backend, _api_backend
    _backend = _api_backend = None


class ClaudeCodeQuick:
    """No API key: one tool-less ``claude -p`` turn (no resume, no MCP, max-turns 1)."""

    name = "claude_code_quick"
    fake = False
    streams = False

    def create(self, req: runtime.ModelRequest) -> runtime.ModelReply:
        from assistant import claude_code

        parts = [m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], default=str)
                 for m in req.messages]
        res = claude_code.run_prompt("\n\n".join(parts), system="\n\n".join(b for b in req.system if b),
                                     role="sub", max_turns=1, effort="low", model="haiku", timeout=90)
        usage = runtime.Usage(res.input_tokens, res.output_tokens, res.cache_read_tokens, res.cache_write_tokens)
        reply = runtime.ModelReply([{"type": "text", "text": res.text}], res.text, [], "end_turn", usage, res.model,
                                   res.session_id)
        reply.request_id = f"cc:{'api' if res.billed_to_api else 'plan'}:{res.session_id or ''}"
        if not res.billed_to_api:
            claude_code.record_notional(res.notional_usd)
        return reply


def get_backend() -> Any:
    global _api_backend
    if _backend is not None:
        return _backend
    import config

    if get_assistant_settings().fake:
        installed = runtime._backend
        return installed if isinstance(installed, runtime.FakeBackend) else runtime.FakeBackend()
    if config.get_settings().anthropic_api_key:
        with _lock:
            if _api_backend is None:
                _api_backend = runtime.AnthropicBackend(timeout=30.0, max_retries=1)
            return _api_backend
    return ClaudeCodeQuick()


def call(backend: Any, req: runtime.ModelRequest, on_text: Callable[[str], None] | None = None) -> runtime.ModelReply:
    if on_text is not None and getattr(backend, "streams", True) and hasattr(backend, "stream"):
        return backend.stream(req, on_text)
    return backend.create(req)


def record(agent: str, reply: runtime.ModelReply, fake: bool) -> float:
    rid = reply.request_id or ""
    if rid.startswith("cc:"):   # Claude Code: plan runs cost $0 here, API-billed runs count
        cost = runtime.cost_of(reply.model, reply.usage) if rid.startswith("cc:api") else 0.0
        db.insert_llm_usage(model=reply.model, purpose=f"{runtime.PURPOSE_PREFIX}{agent}", tier=runtime.TIER,
                            input_tokens=reply.usage.input_tokens, output_tokens=reply.usage.output_tokens,
                            cache_read_tokens=reply.usage.cache_read_tokens,
                            cache_write_tokens=reply.usage.cache_write_tokens, cost_usd=cost,
                            request_id=rid[:120], fake=False)
        return cost
    return runtime._record_usage(agent, reply, fake)
