"""LiteLLM model gateway (P08 8.1): ``ChatModel`` + ``EmbeddingModel`` over LiteLLM's OpenAI-compatible HTTP API.

Request mapping (verified empirically against LiteLLM 1.104.2 → Ollama, see docs/architecture/models.md):

=================================  ===========================================  ==================================
gateway request field              reaches Ollama ``/api/chat`` as              note
=================================  ===========================================  ==================================
``model`` (alias)                  ``model`` = Ollama tag of the alias          LiteLLM model_list lookup
``max_tokens``                     ``options.num_predict``
``temperature``                    ``options.temperature``
``response_format.json_schema``    ``format`` = the JSON schema                 Ollama structured outputs
``think`` (top level)              ``think``                                    Ollama thinking control
``num_ctx`` (top level)            ``options.num_ctx``                          overrides the config default
``keep_alive`` (top level)         ``keep_alive``                               keeps residency stable
``timeout`` (top level)            – (LiteLLM-side upstream timeout)            proxy aborts → HTTP 408
=================================  ===========================================  ==================================

Only ``message.content`` is returned. ``reasoning_content``/``thinking`` and inline ``<think>`` blocks are counted
(``reasoning_chars``) and discarded – never stored, logged or re-sent. Every HTTP call is persisted as a
``model_invocations`` row plus ``model.invocation.started/finished`` events (when a session factory is configured).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.config import HermclawConfig, ModelProfileConfig, ModelsConfig
from hermclaw.core.errors import ConfigError, HermclawError, ModelError, ModelOutputInvalid, ModelTimeout, ValidationFailed
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.events.store import append_event
from hermclaw.models.profiles import ProfileRegistry, assert_architecture, resolve_api_key, validate_http_url
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.models.tokens import ContextBudget, estimate_tokens, validate_context
from hermclaw.persistence.models import ModelInvocation

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")
SessionFactory = Callable[[], AsyncSession]

SOURCE_TYPE = "model_gateway"
MAX_EXCERPT_CHARS = 4000
MAX_ERROR_CHARS = 2000
MAX_REPAIR_ECHO_CHARS = 6000

# Stable error codes (ModelError.code) ------------------------------------------------------------------------------
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"  # connection to LiteLLM/Ollama failed
MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"  # 5xx from LiteLLM: Ollama could not load/run the model
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"  # 404: model not present on the Ollama host
MODEL_TIMEOUT = ModelTimeout.code
MODEL_PROTOCOL_ERROR = "MODEL_PROTOCOL_ERROR"  # malformed/unexpected response body
MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
MODEL_BAD_REQUEST = "MODEL_BAD_REQUEST"
MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
MODEL_KIND_MISMATCH = "MODEL_KIND_MISMATCH"
EMBEDDING_DIMENSION_MISMATCH = "EMBEDDING_DIMENSION_MISMATCH"

#: Failures that justify the configured technical fallback (never invalid output, auth or bad requests).
TECHNICAL_FAILURE_CODES = frozenset({MODEL_UNAVAILABLE, MODEL_LOAD_FAILED, MODEL_NOT_FOUND, MODEL_TIMEOUT, MODEL_PROTOCOL_ERROR})

_LOAD_FAILURE_HINTS = (
    "requires more system memory",
    "failed to load",
    "llama runner",
    "out of memory",
    "cuda error",
    "model runner",
    "unable to allocate",
)
_CONNECTION_HINTS = ("apiconnectionerror", "connection refused", "cannot connect", "connecterror", "name or service not known")
_THINK_BLOCK = re.compile(r"<(think|thinking)>.*?</\1>", re.S | re.I)
_THINK_OPEN = re.compile(r"<(think|thinking)>", re.I)
_FENCE = re.compile(r"^```[A-Za-z0-9_+-]*[ \t]*\r?\n?(.*?)\r?\n?```\s*$", re.S)


def is_technical_failure(exc: BaseException) -> bool:
    """True for failures where the model could not be executed (load error, connection error, timeout)."""
    return isinstance(exc, ModelError) and exc.code in TECHNICAL_FAILURE_CODES


# ----------------------------------------------------------------------------------------------- helpers
def strip_reasoning(content: str) -> tuple[str, int]:
    """Remove inline ``<think>…</think>`` blocks (and an unterminated leading ``<think>``). Returns (content, chars)."""
    removed = 0

    def _drop(match: re.Match[str]) -> str:
        nonlocal removed
        removed += len(match.group(0))
        return ""

    out = _THINK_BLOCK.sub(_drop, content)
    opener = _THINK_OPEN.search(out)
    if opener is not None and not out[: opener.start()].strip():
        removed += len(out) - opener.start()
        out = out[: opener.start()]
    return out.strip() if removed else out, removed


def extract_json(text: str) -> Any:
    """Parse the JSON value of a model answer: plain JSON, a fenced code block or the first JSON object/array."""
    s = text.strip().lstrip("﻿")
    if not s:
        raise ValueError("empty content")
    fenced = _FENCE.match(s)
    if fenced:
        s = fenced.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for start, ch in enumerate(s):
        if ch in "{[":
            try:
                value, _end = decoder.raw_decode(s, start)
            except json.JSONDecodeError:
                continue
            return value
    raise ValueError("no JSON value found in content")


def format_validation_error(exc: ValidationError, *, limit: int = 20) -> str:
    lines = []
    for err in exc.errors(include_url=False)[:limit]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        lines.append(f"- {loc}: {err.get('msg', 'invalid')}")
    extra = exc.error_count() - limit
    if extra > 0:
        lines.append(f"- … {extra} more errors")
    return "\n".join(lines)


def schema_name(schema: dict[str, Any] | type[BaseModel]) -> str:
    raw = schema.__name__ if isinstance(schema, type) else str(schema.get("title") or "response")
    name = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64]
    return name or "response"


def response_format_for(json_schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI ``response_format`` for a raw JSON schema (or an already wrapped ``{name, schema, strict}``)."""
    if "schema" in json_schema and "name" in json_schema and isinstance(json_schema["schema"], dict):
        wrapped = {
            "name": schema_name({"title": json_schema["name"]}),
            "schema": json_schema["schema"],
            "strict": bool(json_schema.get("strict", True)),
        }
    else:
        wrapped = {"name": schema_name(json_schema), "schema": json_schema, "strict": True}
    return {"type": "json_schema", "json_schema": wrapped}


def request_hash(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _content_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):  # OpenAI content parts
        return "".join(str(p.get("text", "")) for p in raw if isinstance(p, dict) and p.get("type", "text") == "text")
    return str(raw)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:MAX_ERROR_CHARS]
    if isinstance(data, dict):
        err = data.get("error", data.get("detail", data))
        if isinstance(err, dict):
            return str(err.get("message") or err)[:MAX_ERROR_CHARS]
        return str(err)[:MAX_ERROR_CHARS]
    return str(data)[:MAX_ERROR_CHARS]


def classify_http_error(status: int, message: str) -> str:
    low = message.lower()
    if status in (408, 504):
        return MODEL_TIMEOUT
    if status in (401, 403):
        return MODEL_AUTH_FAILED
    if status == 429:
        return MODEL_RATE_LIMITED
    if any(h in low for h in _CONNECTION_HINTS):
        return MODEL_UNAVAILABLE
    if any(h in low for h in _LOAD_FAILURE_HINTS):
        return MODEL_LOAD_FAILED
    if status == 404:
        return MODEL_NOT_FOUND
    if status in (502, 503):
        return MODEL_UNAVAILABLE
    if status >= 500:
        return MODEL_LOAD_FAILED
    return MODEL_BAD_REQUEST


@dataclass(frozen=True)
class GatewayOptions:
    #: ``keep_alive`` sent with every request so a call does not shorten the residency chosen by ModelResidency.
    keep_alive: str | None = "10m"
    #: forward ``think``/``num_ctx``/``keep_alive`` (Ollama passthrough, see module docstring)
    ollama_passthrough: bool = True
    #: extra attempts on the same alias after a timeout before the technical fallback is used ("repeated timeout")
    timeout_retries: int = 1
    #: client-side slack on top of the proxy-side ``timeout`` so the proxy's 408 normally arrives first
    timeout_grace_seconds: float = 5.0
    enforce_context: bool = True
    context_reserve_tokens: int = 0
    embed_batch_size: int = 64
    validate_architecture: bool = True
    max_excerpt_chars: int = MAX_EXCERPT_CHARS


@dataclass
class _Attempt:
    result: ChatResult
    valid: bool
    parsed: Any = None
    error: str | None = None


@dataclass
class _InvocationRecord:
    id: uuid.UUID
    profile: ModelProfileConfig
    ctx: CallContext
    repair_attempt: int
    fallback_used: bool
    started: float
    persisted: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


Validator = Callable[[str], tuple[bool, Any, str | None]]


def _non_empty(content: str) -> tuple[bool, Any, str | None]:
    return (bool(content.strip()), None, None if content.strip() else "empty content")


class LiteLLMGateway:
    """Model gateway. Implements ``ChatModel`` and ``EmbeddingModel`` (hermclaw.models.protocols)."""

    def __init__(
        self,
        models: ModelsConfig,
        *,
        api_key: str | None = None,
        session_factory: SessionFactory | None = None,
        http_client: httpx.AsyncClient | None = None,
        options: GatewayOptions | None = None,
        base_url: str | None = None,
        embedding_alias: str | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.models = models
        self.registry = ProfileRegistry(models)
        self.options = options or GatewayOptions()
        if self.options.validate_architecture:
            for issue in assert_architecture(models):
                log.warning("model profile warning", extra={"alias": issue.alias, "role": issue.role, "issue": issue.message})
        self.base_url = validate_http_url(base_url or models.litellm.base_url, what="litellm base_url")
        self._api_key = api_key
        self._redactor = redactor or DEFAULT_REDACTOR
        if api_key:
            self._redactor.add_literal(api_key)
        self._session_factory = session_factory
        self._own_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(models.litellm.request_timeout_seconds, connect=10.0))
        emb = self.registry.get(embedding_alias) if embedding_alias else self.registry.embedding_profile()
        if emb is not None and emb.kind != "embedding":
            raise ConfigError(f"'{emb.alias}' is not an embedding profile", code=MODEL_KIND_MISMATCH)
        self._embedding = emb
        self.dimensions: int = (emb.embedding_dimensions or 0) if emb else 0
        self.model_name: str = emb.model if emb else ""

    # ------------------------------------------------------------------------------------------ lifecycle
    @classmethod
    def from_config(
        cls,
        config: HermclawConfig | None = None,
        *,
        session_factory: SessionFactory | None = None,
        http_client: httpx.AsyncClient | None = None,
        options: GatewayOptions | None = None,
    ) -> LiteLLMGateway:
        from hermclaw.core.config import get_config

        cfg = config or get_config()
        api_key = resolve_api_key(cfg.models.litellm.api_key_ref, required=True)
        return cls(cfg.models, api_key=api_key, session_factory=session_factory, http_client=http_client, options=options)

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def __aenter__(self) -> LiteLLMGateway:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # ------------------------------------------------------------------------------------------ public API
    async def chat(
        self,
        alias: str,
        messages: list[ChatMessage],
        *,
        ctx: CallContext,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> ChatResult:
        """One chat completion (with automatic technical fallback). Returns only the final content."""

        async def run(profile: ModelProfileConfig, fallback_used: bool) -> _Attempt:
            return await self._invoke(
                profile,
                messages,
                ctx=ctx,
                max_tokens=max_tokens,
                temperature=temperature,
                json_schema=json_schema,
                timeout_seconds=timeout_seconds,
                repair_attempt=0,
                fallback_used=fallback_used,
                validator=_non_empty,
            )

        return (await self.call_with_fallback(alias, run, ctx=ctx)).result

    async def structured(
        self,
        alias: str,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        ctx: CallContext,
        max_repairs: int = 2,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> StructuredResult[T]:
        """JSON-schema constrained call + Pydantic validation + ≤ ``max_repairs`` repair calls.

        Repairs re-send the original conversation, the previous *final content* and the validation errors – never
        reasoning. Empty content (e.g. everything went into thinking) counts as invalid. Raises
        ``ModelOutputInvalid`` when the budget is exhausted; technical failures switch to the fallback alias."""
        if max_repairs < 0:
            raise ValueError("max_repairs must be >= 0")
        json_schema = schema.model_json_schema()

        def validator(content: str) -> tuple[bool, Any, str | None]:
            if not content.strip():
                return False, None, "The answer contained no final content (only reasoning or nothing)."
            try:
                data = extract_json(content)
            except ValueError as exc:
                return False, None, f"The answer is not valid JSON ({exc})."
            try:
                return True, schema.model_validate(data), None
            except ValidationError as exc:
                return False, None, "The JSON does not match the schema:\n" + format_validation_error(exc)

        conversation = list(messages)
        pinned: ModelProfileConfig | None = None  # set once the fallback served a call – no switching back and forth
        errors: list[str] = []
        invocation_ids: list[str] = []
        last: _Attempt | None = None
        for attempt_no in range(max_repairs + 1):
            convo = list(conversation)

            async def run(
                profile: ModelProfileConfig, fallback_used: bool, _convo: list[ChatMessage] = convo, _n: int = attempt_no
            ) -> _Attempt:
                return await self._invoke(
                    profile,
                    _convo,
                    ctx=ctx,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    json_schema=json_schema,
                    timeout_seconds=timeout_seconds,
                    repair_attempt=_n,
                    fallback_used=fallback_used,
                    validator=validator,
                )

            if pinned is not None:
                last = await run(pinned, True)
            else:
                last = await self.call_with_fallback(alias, run, ctx=ctx)
                if last.result.fallback_used:
                    pinned = self.registry.get(last.result.alias)
            if last.result.invocation_id:
                invocation_ids.append(str(last.result.invocation_id))
            if last.valid:
                return StructuredResult(value=last.parsed, result=last.result, repair_attempts=attempt_no)
            error = last.error or "invalid answer"
            errors.append(error)
            conversation = list(messages) + self._repair_messages(last.result.content, error, schema_title=schema_name(schema))
        assert last is not None
        raise ModelOutputInvalid(
            f"'{last.result.alias}' returned no valid {schema.__name__} after {max_repairs} repair attempts",
            details={
                "alias": last.result.alias,
                "schema": schema.__name__,
                "attempts": max_repairs + 1,
                "errors": [self._redactor.text(e)[:MAX_ERROR_CHARS] for e in errors],
                "invocation_ids": invocation_ids,
                "fallback_used": last.result.fallback_used,
            },
        )

    async def embed(self, texts: list[str], *, ctx: CallContext) -> list[list[float]]:
        """Embeddings via the embedding alias (``/v1/embeddings``). Never falls back to another model: vectors of
        different models are not comparable."""
        profile = self._embedding
        if profile is None:
            raise ConfigError("no enabled embedding profile configured", code="MODEL_ROLE_UNKNOWN")
        if not texts:
            return []
        for idx, text in enumerate(texts):
            est = estimate_tokens(text)
            if self.options.enforce_context and est > profile.context_tokens:
                raise ValidationFailed(
                    f"embedding input #{idx} needs ~{est} tokens, context window of '{profile.alias}' is {profile.context_tokens}",
                    code="CONTEXT_OVERFLOW",
                    details={"index": idx, "estimated_tokens": est, "context_tokens": profile.context_tokens},
                )
        out: list[list[float]] = []
        size = max(1, self.options.embed_batch_size)
        for start in range(0, len(texts), size):
            out.extend(await self._embed_batch(profile, texts[start : start + size], ctx=ctx))
        return out

    async def call_with_fallback(
        self,
        alias: str,
        fn: Callable[[ModelProfileConfig, bool], Awaitable[R]],
        *,
        ctx: CallContext,
    ) -> R:
        """Run ``fn(profile, fallback_used)`` on ``alias``; on a *technical* failure (load/connection error, repeated
        timeout) run it once on the configured fallback alias and emit ``planner.fallback.used`` (warning).
        Invalid output, auth errors and bad requests are never a reason to switch models."""
        primary = self.registry.get(alias)
        timeouts = 0
        while True:
            try:
                return await fn(primary, False)
            except ModelError as exc:
                if not is_technical_failure(exc):
                    raise
                if exc.code == MODEL_TIMEOUT and timeouts < self.options.timeout_retries:
                    timeouts += 1
                    log.warning("model timeout, retrying same alias", extra={"alias": alias, "retry": timeouts})
                    continue
                fallback = self.registry.fallback_for(alias)
                if fallback is None or fallback.kind != primary.kind:
                    raise
                await self._emit_fallback(primary, fallback, exc, ctx=ctx, timeouts=timeouts)
                try:
                    return await fn(fallback, True)
                except HermclawError as fb_exc:
                    fb_exc.details.setdefault("primary_alias", primary.alias)
                    fb_exc.details.setdefault("primary_error_code", exc.code)
                    fb_exc.details.setdefault("fallback_alias", fallback.alias)
                    raise

    # ------------------------------------------------------------------------------------------ internals
    @staticmethod
    def _repair_messages(previous: str, error: str, *, schema_title: str) -> list[ChatMessage]:
        msgs: list[ChatMessage] = []
        if previous.strip():
            echo = previous if len(previous) <= MAX_REPAIR_ECHO_CHARS else previous[:MAX_REPAIR_ECHO_CHARS] + "…"
            msgs.append(ChatMessage(role="assistant", content=echo))
        msgs.append(
            ChatMessage(
                role="user",
                content=(
                    f"Your previous answer was rejected by the runtime validator for '{schema_title}'.\n{error}\n\n"
                    "Answer again with exactly one JSON value that satisfies the required JSON schema. "
                    "No markdown, no code fences, no explanations."
                ),
            )
        )
        return msgs

    def _timeout(self, profile: ModelProfileConfig, timeout_seconds: float | None) -> float:
        t = float(timeout_seconds if timeout_seconds is not None else profile.timeout_seconds)
        return max(1.0, min(t, float(self.models.litellm.request_timeout_seconds)))

    def _chat_body(
        self,
        profile: ModelProfileConfig,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": profile.alias,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "timeout": timeout,
        }
        if json_schema is not None:
            body["response_format"] = response_format_for(json_schema)
        if self.options.ollama_passthrough:
            body["think"] = profile.think
            body["num_ctx"] = profile.context_tokens
            if self.options.keep_alive:
                body["keep_alive"] = self.options.keep_alive
        return body

    async def _invoke(
        self,
        profile: ModelProfileConfig,
        messages: Sequence[ChatMessage],
        *,
        ctx: CallContext,
        max_tokens: int | None,
        temperature: float | None,
        json_schema: dict[str, Any] | None,
        timeout_seconds: float | None,
        repair_attempt: int,
        fallback_used: bool,
        validator: Validator,
    ) -> _Attempt:
        if profile.kind != "chat":
            raise ConfigError(f"'{profile.alias}' is not a chat profile", code=MODEL_KIND_MISMATCH)
        out_tokens = profile.max_output_tokens if max_tokens is None else max_tokens
        if out_tokens <= 0:
            raise ConfigError(f"max_tokens for '{profile.alias}' must be > 0", code="INVALID_TOKEN_BUDGET")
        budget: ContextBudget | None = None
        if self.options.enforce_context:
            budget = validate_context(profile, messages, max_tokens=out_tokens, reserve_tokens=self.options.context_reserve_tokens)
        timeout = self._timeout(profile, timeout_seconds)
        body = self._chat_body(
            profile,
            messages,
            max_tokens=out_tokens,
            temperature=profile.temperature if temperature is None else temperature,
            json_schema=json_schema,
            timeout=timeout,
        )
        record = await self._start(
            profile,
            ctx,
            repair_attempt=repair_attempt,
            fallback_used=fallback_used,
            meta={
                "request_hash": request_hash(body),
                "max_tokens": out_tokens,
                "context_tokens": profile.context_tokens,
                "estimated_prompt_tokens": budget.prompt_tokens if budget else None,
                "structured": json_schema is not None,
                "think": profile.think,
            },
        )
        try:
            data = await self._post_json("/v1/chat/completions", body, upstream_timeout=timeout)
            result = self._parse_chat(profile, data, record)
            valid, parsed, error = validator(result.content)
        except BaseException as exc:
            await self._finish_error(record, exc)
            raise
        await self._finish(record, result=result, valid=valid, error=error)
        return _Attempt(result=result, valid=valid, parsed=parsed, error=error)

    async def _embed_batch(self, profile: ModelProfileConfig, batch: list[str], *, ctx: CallContext) -> list[list[float]]:
        body: dict[str, Any] = {"model": profile.alias, "input": batch, "timeout": self._timeout(profile, None)}
        if self.options.ollama_passthrough:
            body["options"] = {"num_ctx": profile.context_tokens}
            if self.options.keep_alive:
                body["keep_alive"] = self.options.keep_alive
        record = await self._start(
            profile,
            ctx,
            repair_attempt=0,
            fallback_used=False,
            meta={
                "request_hash": request_hash(body),
                "inputs": len(batch),
                "estimated_prompt_tokens": sum(estimate_tokens(t) for t in batch),
            },
        )
        try:
            data = await self._post_json("/v1/embeddings", body, upstream_timeout=self._timeout(profile, None))
            vectors = self._parse_embeddings(profile, data, expected=len(batch))
        except BaseException as exc:
            await self._finish_error(record, exc)
            raise
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        result = ChatResult(
            content="",
            alias=profile.alias,
            model=profile.model,
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")) if usage else None,
            completion_tokens=None,
            latency_ms=int((time.monotonic() - record.started) * 1000),
            finish_reason="embedded",
            invocation_id=record.id,
        )
        await self._finish(
            record, result=result, valid=True, error=None, excerpt=f"{len(vectors)} vectors x {len(vectors[0]) if vectors else 0} dims"
        )
        return vectors

    async def _post_json(self, path: str, body: dict[str, Any], *, upstream_timeout: float) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = await self._client.post(
                url,
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=self._headers(),
                timeout=httpx.Timeout(upstream_timeout + self.options.timeout_grace_seconds, connect=10.0),
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeout(
                f"model call to '{body.get('model')}' timed out after {upstream_timeout:.0f}s", details={"alias": body.get("model")}
            ) from exc
        except httpx.TransportError as exc:
            raise ModelError(
                f"LiteLLM unreachable: {type(exc).__name__}",
                code=MODEL_UNAVAILABLE,
                details={"alias": body.get("model"), "base_url": self.base_url},
            ) from exc
        if resp.status_code >= 400:
            message = self._redactor.text(_error_text(resp))
            code = classify_http_error(resp.status_code, message)
            details = {"alias": body.get("model"), "http_status": resp.status_code}
            if code == MODEL_TIMEOUT:
                raise ModelTimeout(f"model call to '{body.get('model')}' timed out (HTTP {resp.status_code}): {message}", details=details)
            raise ModelError(f"model call to '{body.get('model')}' failed (HTTP {resp.status_code}): {message}", code=code, details=details)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelError("LiteLLM returned a non-JSON body", code=MODEL_PROTOCOL_ERROR, details={"alias": body.get("model")}) from exc
        if not isinstance(data, dict):
            raise ModelError("LiteLLM returned an unexpected body", code=MODEL_PROTOCOL_ERROR, details={"alias": body.get("model")})
        return data

    def _parse_chat(self, profile: ModelProfileConfig, data: dict[str, Any], record: _InvocationRecord) -> ChatResult:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ModelError("chat response without choices", code=MODEL_PROTOCOL_ERROR, details={"alias": profile.alias})
        choice = choices[0]
        raw_message = choice.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        reasoning_chars = 0
        for key in ("reasoning_content", "reasoning", "thinking"):
            value = message.get(key)
            if isinstance(value, str):
                reasoning_chars += len(value)
        psf = message.get("provider_specific_fields")
        if isinstance(psf, dict):
            for key in ("reasoning_content", "reasoning", "thinking"):
                value = psf.get(key)
                if isinstance(value, str) and key not in message:
                    reasoning_chars += len(value)
        content, inline = strip_reasoning(_content_text(message.get("content")))
        reasoning_chars += inline
        usage_raw = data.get("usage")
        usage = usage_raw if isinstance(usage_raw, dict) else {}
        raw_usage = {k: v for k, v in usage.items() if isinstance(v, int | float) and not isinstance(v, bool)}
        finish = choice.get("finish_reason")
        return ChatResult(
            content=content,
            alias=profile.alias,
            model=profile.model,
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
            latency_ms=int((time.monotonic() - record.started) * 1000),
            finish_reason=str(finish)[:32] if finish is not None else None,
            reasoning_chars=reasoning_chars,
            invocation_id=record.id,
            fallback_used=record.fallback_used,
            raw_usage=raw_usage,
        )

    def _parse_embeddings(self, profile: ModelProfileConfig, data: dict[str, Any], *, expected: int) -> list[list[float]]:
        items = data.get("data")
        if not isinstance(items, list) or len(items) != expected:
            raise ModelError(
                f"embedding response has {len(items) if isinstance(items, list) else 'no'} vectors, expected {expected}",
                code=MODEL_PROTOCOL_ERROR,
                details={"alias": profile.alias},
            )
        ordered = sorted(items, key=lambda it: int(it.get("index", 0)) if isinstance(it, dict) else 0)
        vectors: list[list[float]] = []
        for item in ordered:
            vec = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vec, list) or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in vec):
                raise ModelError(
                    "embedding response contains a malformed vector", code=MODEL_PROTOCOL_ERROR, details={"alias": profile.alias}
                )
            if profile.embedding_dimensions and len(vec) != profile.embedding_dimensions:
                raise ModelError(
                    f"embedding has {len(vec)} dimensions, profile '{profile.alias}' expects {profile.embedding_dimensions}",
                    code=EMBEDDING_DIMENSION_MISMATCH,
                    details={"alias": profile.alias, "got": len(vec), "expected": profile.embedding_dimensions},
                )
            vectors.append([float(x) for x in vec])
        return vectors

    # ------------------------------------------------------------------------------------------ persistence
    async def _start(
        self,
        profile: ModelProfileConfig,
        ctx: CallContext,
        *,
        repair_attempt: int,
        fallback_used: bool,
        meta: dict[str, Any],
    ) -> _InvocationRecord:
        record = _InvocationRecord(
            id=uuid.uuid4(),
            profile=profile,
            ctx=ctx,
            repair_attempt=repair_attempt,
            fallback_used=fallback_used,
            started=time.monotonic(),
            meta=meta,
        )
        if self._session_factory is None:
            return record
        async with self._session_factory() as session:
            session.add(
                ModelInvocation(
                    id=record.id,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    alias=profile.alias,
                    model=profile.model,
                    role=profile.role,
                    purpose=ctx.purpose[:64],
                    status="started",
                    repair_attempt=repair_attempt,
                    fallback_used=fallback_used,
                    request_hash=meta.get("request_hash"),
                )
            )
            await append_event(
                session,
                EventType.MODEL_INVOCATION_STARTED,
                source_type=SOURCE_TYPE,
                source_id=profile.alias,
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                payload=self._event_payload(record),
            )
            await session.commit()
        record.started = time.monotonic()
        record.persisted = True
        return record

    def _event_payload(self, record: _InvocationRecord, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "invocation_id": str(record.id),
            "alias": record.profile.alias,
            "model": record.profile.model,
            "role": record.profile.role,
            "purpose": record.ctx.purpose,
            "repair_attempt": record.repair_attempt,
            "fallback_used": record.fallback_used,
        }
        payload.update({k: v for k, v in record.meta.items() if v is not None})
        payload.update(extra)
        return payload

    async def _finish(
        self, record: _InvocationRecord, *, result: ChatResult, valid: bool, error: str | None, excerpt: str | None = None
    ) -> None:
        status = "succeeded" if valid else "invalid"
        fields: dict[str, Any] = {
            "status": status,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "reasoning_chars": result.reasoning_chars,
            "latency_ms": result.latency_ms,
            "finish_reason": result.finish_reason,
            "response_valid": valid,
            "response_excerpt": self._redactor.text(excerpt if excerpt is not None else result.content)[: self.options.max_excerpt_chars],
            "error_code": None if valid else "MODEL_OUTPUT_INVALID",
            "error_message": None if valid or error is None else self._redactor.text(error)[:MAX_ERROR_CHARS],
        }
        await self._write_finish(
            record,
            fields,
            severity=Severity.info if valid else Severity.warning,
            payload=self._event_payload(
                record,
                status=status,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                reasoning_chars=result.reasoning_chars,
                latency_ms=result.latency_ms,
                finish_reason=result.finish_reason,
                response_valid=valid,
                content_chars=len(result.content),
            ),
            duration_ms=result.latency_ms,
        )

    async def _finish_error(self, record: _InvocationRecord, exc: BaseException) -> None:
        latency = int((time.monotonic() - record.started) * 1000)
        if isinstance(exc, asyncio.CancelledError):
            status, code, message = "cancelled", "CANCELLED", "call cancelled"
        elif isinstance(exc, ModelTimeout):
            status, code, message = "timeout", exc.code, exc.message
        elif isinstance(exc, HermclawError):
            status, code, message = "failed", exc.code, exc.message
        else:
            status, code, message = "failed", "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"
        message = self._redactor.text(message)[:MAX_ERROR_CHARS]
        fields = {"status": status, "latency_ms": latency, "error_code": code[:64], "error_message": message, "response_valid": None}
        try:
            await asyncio.shield(
                self._write_finish(
                    record,
                    fields,
                    severity=Severity.warning,
                    payload=self._event_payload(record, status=status, error_code=code, latency_ms=latency),
                    duration_ms=latency,
                )
            )
        except Exception:
            log.exception("could not persist failed model invocation", extra={"invocation_id": str(record.id)})

    async def _write_finish(
        self,
        record: _InvocationRecord,
        fields: dict[str, Any],
        *,
        severity: Severity,
        payload: dict[str, Any],
        duration_ms: int,
    ) -> None:
        if self._session_factory is None or not record.persisted:
            return
        try:
            async with self._session_factory() as session:
                row = await session.get(ModelInvocation, record.id)
                if row is not None:
                    for key, value in fields.items():
                        setattr(row, key, value)
                    row.finished_at = datetime.now(UTC)
                await append_event(
                    session,
                    EventType.MODEL_INVOCATION_FINISHED,
                    source_type=SOURCE_TYPE,
                    source_id=record.profile.alias,
                    job_id=record.ctx.job_id,
                    step_id=record.ctx.step_id,
                    attempt_id=record.ctx.attempt_id,
                    severity=severity,
                    payload=payload,
                    duration_ms=duration_ms,
                )
                await session.commit()
        except Exception:
            # The model answer is already paid for – keep it; the row stays 'started' (visible as orphan).
            log.exception("could not persist model invocation result", extra={"invocation_id": str(record.id)})

    async def _emit_fallback(
        self,
        primary: ModelProfileConfig,
        fallback: ModelProfileConfig,
        exc: ModelError,
        *,
        ctx: CallContext,
        timeouts: int,
    ) -> None:
        reason = self._redactor.text(exc.message)[:MAX_ERROR_CHARS]
        log.warning(
            "technical model fallback",
            extra={"primary_alias": primary.alias, "fallback_alias": fallback.alias, "error_code": exc.code},
        )
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            await append_event(
                session,
                EventType.PLANNER_FALLBACK_USED,
                source_type=SOURCE_TYPE,
                source_id=primary.alias,
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                severity=Severity.warning,
                payload={
                    "primary_alias": primary.alias,
                    "primary_model": primary.model,
                    "fallback_alias": fallback.alias,
                    "fallback_model": fallback.model,
                    "role": primary.role,
                    "purpose": ctx.purpose,
                    "reason": reason,
                    "error_code": exc.code,
                    "timeouts": timeouts,
                },
            )
            await session.commit()
