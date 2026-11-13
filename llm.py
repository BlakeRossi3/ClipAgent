"""The only module that talks to the Anthropic API.

Usage::

    from llm import complete

    res = complete(
        "haiku",
        system=[INSTRUCTIONS, strategy_block],   # stable -> cached
        user=window_text,                        # varies per call
        purpose="pick.score_window",
        max_tokens=1024,
        json_schema=SCORE_SCHEMA,
        source_id=source_id,
    )
    res.data["score"]

Behaviour:

* **Budget**: estimates worst-case cost and calls ``budget.check_budget`` before
  every request (raises ``budget.BudgetExceeded``); records actual usage via
  ``budget.record_usage`` after every request, including failed-validation retries.
* **Prompt caching**: the system prompt is sent as text blocks with a
  ``cache_control`` breakpoint on the last one (``cache_system=True``). Put
  stable text (instructions, strategy) in ``system`` and per-call text in
  ``user``. Never put timestamps or ids in ``system``.
* **Structured output**: with ``json_schema`` the request uses
  ``output_config.format`` (JSON schema); unsupported constraints are stripped
  from the wire schema and re-checked client-side. Parse/validation failures are
  retried ``json_retries`` times, then :class:`LLMOutputError`.
* **Retries**: the SDK retries 408/409/429/5xx/connection errors
  (``LLM_MAX_RETRIES``); anything left surfaces as :class:`LLMError`.
* **Refusals**: ``stop_reason == "refusal"`` raises :class:`LLMRefusal`. Sonnet
  requests opt into server-side refusal fallbacks (``LLM_SERVER_FALLBACKS``).
* **Fake mode**: if a handler is installed with :func:`set_fake_handler`, or
  ``FAKE_LLM=true``, no network call is made. The default fake returns a
  deterministic instance of ``json_schema`` (or a short text). Usage is still
  recorded (``fake=1``) so budget logic is exercised.
* **Credentials**: only ``settings.anthropic_api_key`` is used, passed
  explicitly; ambient ``ANTHROPIC_*`` credentials are never picked up.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal

import budget
import db
from config import get_settings

log = logging.getLogger(__name__)

Tier = Literal["haiku", "sonnet"]
TIERS: tuple[str, ...] = ("haiku", "sonnet")
SONNET_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """The call could not be completed (API error, missing key, ...)."""


class LLMRefusal(LLMError):
    """The model declined the request (``stop_reason == "refusal"``)."""


class LLMOutputError(LLMError):
    """Structured output was missing, unparsable or failed schema validation."""

    def __init__(self, message: str, *, text: str = ""):
        super().__init__(message)
        self.text = text


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """What a fake handler receives (and what would be sent to the API)."""

    tier: str
    model: str
    system: tuple[str, ...]
    user: str
    purpose: str
    max_tokens: int
    json_schema: dict[str, Any] | None
    effort: str
    cache_system: bool
    source_id: int | None = None
    clip_id: int | None = None
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    data: Any                 # parsed JSON when json_schema was given, else None
    usage: Usage              # of the final attempt
    cost_usd: float           # summed over all attempts
    model: str                # model that actually served the request
    tier: str
    stop_reason: str | None
    fake: bool = False
    request_id: str | None = None
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class FakeReply:
    """Full-control return value for a fake handler."""

    text: str | None = None
    data: Any = None          # serialised to JSON text if text is None
    usage: Usage | None = None
    stop_reason: str = "end_turn"
    model: str | None = None


FakeHandler = Callable[[LLMRequest], "str | dict | list | FakeReply"]


@dataclass(slots=True)
class _Raw:
    text: str
    usage: Usage
    model: str
    stop_reason: str | None
    request_id: str | None
    fake: bool


@dataclass(slots=True)
class _State:
    fake_handler: FakeHandler | None = None
    client: Any = None
    client_key: tuple[str, str] | None = None
    injected: bool = False


_state = _State()


# ---------------------------------------------------------------------------
# fake mode / client injection
# ---------------------------------------------------------------------------

def set_fake_handler(handler: FakeHandler | None) -> None:
    """Route every :func:`complete` call to ``handler`` (``None`` to remove)."""
    _state.fake_handler = handler


@contextmanager
def fake_handler(handler: FakeHandler) -> Iterator[None]:
    prev = _state.fake_handler
    _state.fake_handler = handler
    try:
        yield
    finally:
        _state.fake_handler = prev


def set_client(client: Any | None) -> None:
    """Inject an Anthropic-compatible client (tests). ``None`` restores the default."""
    _state.client = client
    _state.injected = client is not None
    _state.client_key = None


def reset() -> None:
    """Remove fake handler and injected client."""
    _state.fake_handler = None
    set_client(None)


def is_fake() -> bool:
    return _state.fake_handler is not None or (get_settings().fake_llm and not _state.injected)


def _get_client() -> Any:
    if _state.injected:
        return _state.client
    s = get_settings()
    if not s.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not configured (set it in config.env, or FAKE_LLM=true)")
    key = (s.anthropic_api_key, s.anthropic_base_url)
    if _state.client is None or _state.client_key != key:
        import anthropic

        _state.client = anthropic.Anthropic(
            api_key=s.anthropic_api_key,
            base_url=s.anthropic_base_url,
            max_retries=s.llm_max_retries,
            timeout=s.llm_timeout_sec,
        )
        _state.client_key = key
    return _state.client


# ---------------------------------------------------------------------------
# JSON schema helpers
# ---------------------------------------------------------------------------

# Constraints the structured-output endpoint does not accept; enforced client-side.
_UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems")


def to_api_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``schema`` that the API accepts: unsupported constraints removed and
    ``additionalProperties: false`` on every object."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED}
            if out.get("type") == "object" or "properties" in out:
                out.setdefault("additionalProperties", False)
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(copy.deepcopy(schema))


_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_json(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Small JSON-schema validator covering the subset we use. Returns error strings."""
    errs: list[str] = []
    if "anyOf" in schema:
        if all(validate_json(value, sub, path) for sub in schema["anyOf"]):
            errs.append(f"{path}: matches no anyOf branch")
        return errs
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    typ = schema.get("type")
    if typ is not None:
        types = typ if isinstance(typ, list) else [typ]
        if not any(_JSON_TYPES[t](value) for t in types):
            return errs + [f"{path}: expected {typ}, got {type(value).__name__}"]
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing {req!r}")
        # Objects are closed unless the schema explicitly allows extra keys (matches to_api_schema).
        if "properties" in schema and schema.get("additionalProperties", False) is False:
            extra = set(value) - set(props)
            if extra:
                errs.append(f"{path}: unexpected keys {sorted(extra)}")
        for k, sub in props.items():
            if k in value:
                errs += validate_json(value[k], sub, f"{path}.{k}")
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errs += validate_json(item, schema["items"], f"{path}[{i}]")
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
    elif _JSON_TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} > maximum {schema['maximum']}")
    return errs


def sample_from_schema(schema: dict[str, Any]) -> Any:
    """Deterministic minimal instance of ``schema`` (used by the default fake)."""
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return sample_from_schema(schema["anyOf"][0])
    typ = schema.get("type", "object")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "null")
    if typ == "object":
        return {k: sample_from_schema(v) for k, v in schema.get("properties", {}).items()}
    if typ == "array":
        n = max(1, int(schema.get("minItems", 1)))
        return [sample_from_schema(schema.get("items", {"type": "string"})) for _ in range(n)]
    if typ in ("integer", "number"):
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if lo is not None and hi is not None:
            v = (lo + hi) / 2
        elif lo is not None:
            v = lo
        elif hi is not None:
            v = hi
        else:
            v = 1
        return int(v) if typ == "integer" else float(v)
    if typ == "boolean":
        return True
    if typ == "null":
        return None
    s = "fake"
    if "minLength" in schema:
        s = s.ljust(int(schema["minLength"]), "x")
    return s


def parse_json_text(text: str) -> Any:
    """Parse JSON from model text, tolerating a surrounding ```json fence."""
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if m:
        t = m.group(1)
    return json.loads(t)


def approx_tokens(*texts: str) -> int:
    """Rough token estimate (~4 chars/token) for budget pre-checks and fake usage."""
    return sum(len(t) for t in texts) // 4 + 1


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def complete(
    tier: Tier,
    *,
    system: str | Sequence[str],
    user: str,
    purpose: str,
    max_tokens: int = 2048,
    cache_system: bool = True,
    json_schema: dict[str, Any] | None = None,
    effort: str | None = None,
    source_id: int | None = None,
    clip_id: int | None = None,
    json_retries: int = 1,
) -> LLMResult:
    """Run one judgment call. See the module docstring for the contract.

    ``system`` may be a string or a sequence of stable strings (each becomes a
    text block; the cache breakpoint goes on the last). ``purpose`` is a dotted
    label such as ``"pick.score_window"`` recorded in ``llm_usage``. ``effort``
    defaults to ``HAIKU_EFFORT`` / ``SONNET_EFFORT``. Thinking tokens count as
    output, so leave headroom in ``max_tokens``.

    Raises ``budget.BudgetExceeded``, :class:`LLMRefusal`, :class:`LLMOutputError`
    or :class:`LLMError`.
    """
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
    s = get_settings()
    model = s.haiku_model if tier == "haiku" else s.sonnet_model
    parts = (system,) if isinstance(system, str) else tuple(system)
    if not parts or not all(isinstance(p, str) and p for p in parts):
        raise ValueError("system must be a non-empty string or sequence of non-empty strings")
    req = LLMRequest(
        tier=tier, model=model, system=parts, user=user, purpose=purpose, max_tokens=max_tokens,
        json_schema=json_schema, effort=effort or (s.haiku_effort if tier == "haiku" else s.sonnet_effort),
        cache_system=cache_system, source_id=source_id, clip_id=clip_id,
    )

    total_cost = 0.0
    attempts = 1 + (max(0, json_retries) if json_schema is not None else 0)
    schema_text = json.dumps(json_schema) if json_schema else ""
    for attempt in range(1, attempts + 1):
        req = _with_attempt(req, attempt)
        estimate = budget.estimate_cost(model, approx_tokens(*parts, user, schema_text), max_tokens)
        budget.check_budget(estimate)

        raw = _call_fake(req) if is_fake() else _call_api(req)
        cost = budget.record_usage(
            raw.model, purpose, input_tokens=raw.usage.input_tokens, output_tokens=raw.usage.output_tokens,
            cache_read_tokens=raw.usage.cache_read_tokens, cache_write_tokens=raw.usage.cache_write_tokens,
            tier=tier, source_id=source_id, clip_id=clip_id, request_id=raw.request_id, fake=raw.fake,
        )
        total_cost += cost

        if raw.stop_reason == "refusal":
            db.log_event("llm", f"{purpose}: model refused", level="warning",
                         data={"model": raw.model, "request_id": raw.request_id})
            raise LLMRefusal(f"{raw.model} refused the request for {purpose}")

        if json_schema is None:
            return LLMResult(raw.text, None, raw.usage, total_cost, raw.model, tier, raw.stop_reason,
                             raw.fake, raw.request_id, attempt)

        try:
            data = parse_json_text(raw.text)
            problems = validate_json(data, json_schema)
        except (json.JSONDecodeError, ValueError) as exc:
            data, problems = None, [f"invalid JSON: {exc}"]
        if not problems:
            return LLMResult(raw.text, data, raw.usage, total_cost, raw.model, tier, raw.stop_reason,
                             raw.fake, raw.request_id, attempt)
        db.log_event("llm", f"{purpose}: structured output rejected (attempt {attempt}/{attempts})",
                     level="warning", data={"problems": problems[:5], "stop_reason": raw.stop_reason})
        if attempt == attempts:
            raise LLMOutputError(f"{purpose}: invalid structured output: {problems[:3]}", text=raw.text)
    raise AssertionError("unreachable")  # pragma: no cover


def _with_attempt(req: LLMRequest, attempt: int) -> LLMRequest:
    return req if req.attempt == attempt else replace(req, attempt=attempt)


def _call_fake(req: LLMRequest) -> _Raw:
    handler = _state.fake_handler or _default_fake
    reply = handler(req)
    if not isinstance(reply, FakeReply):
        reply = FakeReply(text=reply) if isinstance(reply, str) else FakeReply(data=reply)
    text = reply.text if reply.text is not None else json.dumps(reply.data)
    usage = reply.usage or Usage(input_tokens=approx_tokens(*req.system, req.user), output_tokens=approx_tokens(text))
    return _Raw(text=text, usage=usage, model=reply.model or req.model, stop_reason=reply.stop_reason,
                request_id=None, fake=True)


def _default_fake(req: LLMRequest) -> str | dict | list:
    if req.json_schema is not None:
        return sample_from_schema(req.json_schema)
    return f"[fake {req.tier}:{req.purpose}] " + req.user[:80]


def _call_api(req: LLMRequest) -> _Raw:
    import anthropic

    blocks: list[dict[str, Any]] = [{"type": "text", "text": p} for p in req.system]
    if req.cache_system:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    output_config: dict[str, Any] = {"effort": req.effort}
    if req.json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": to_api_schema(req.json_schema)}
    kwargs: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "system": blocks,
        "messages": [{"role": "user", "content": req.user}],
        "output_config": output_config,
    }
    client = _get_client()
    try:
        if req.tier == "sonnet" and get_settings().llm_server_fallbacks:
            resp = client.beta.messages.create(**kwargs, betas=[SONNET_FALLBACK_BETA], fallbacks="default")
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.APIStatusError as exc:
        db.log_event("llm", f"{req.purpose}: API error {exc.status_code}", level="error",
                     data={"model": req.model, "status": exc.status_code, "message": str(exc)[:500]})
        raise LLMError(f"Anthropic API error {exc.status_code} for {req.purpose}") from exc
    except anthropic.APIError as exc:  # connection / timeout after SDK retries
        db.log_event("llm", f"{req.purpose}: API unavailable", level="error",
                     data={"model": req.model, "message": str(exc)[:500]})
        raise LLMError(f"Anthropic API unavailable for {req.purpose}: {exc}") from exc

    u = resp.usage
    usage = Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    )
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    return _Raw(text=text, usage=usage, model=getattr(resp, "model", None) or req.model,
                stop_reason=getattr(resp, "stop_reason", None),
                request_id=getattr(resp, "_request_id", None) or getattr(resp, "id", None), fake=False)
