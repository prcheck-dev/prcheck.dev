"""Single choke point for model calls.

Every call ticks the budget, is schema-validated, and gets exactly one retry
carrying the validation error back to the model. A failed or unconfigured call
returns ``None`` (a "degrade") rather than raising, so a review always finishes
with a verdict instead of a 500.

Backends (``PRCHECK_LLM_BACKEND``):
  * ``anthropic``          Anthropic Messages API (default)
  * ``openai``             OpenAI-compatible /chat/completions (OpenAI, Azure
                           OpenAI, Fireworks, Together, Martian, ...)
  * ``deterministic``      no external call; always degrades (used when no key
                           is configured, and in tests)
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

from .budget import Budget
from .schema import SchemaError, validate

LOGGER = logging.getLogger("reviews.llm")

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"


def _conf(name: str, default=None):
    return getattr(settings, name, default)


def resolve_backend() -> str:
    backend = str(_conf("PRCHECK_LLM_BACKEND", "anthropic")).strip().lower()
    # Fall back to a no-op backend when the chosen one has no credentials, so a
    # missing key degrades cleanly instead of raising on every review.
    if backend == "anthropic" and not _conf("ANTHROPIC_API_KEY"):
        return "deterministic"
    if backend in {"openai", "openai-compatible"} and not _conf("PRCHECK_OPENAI_API_KEY"):
        return "deterministic"
    return backend


def _retryable(status: int | None, message: str) -> bool:
    if status == 429 or (isinstance(status, int) and 500 <= status < 600):
        return True
    msg = message.casefold()
    return any(m in msg for m in ("rate limit", "overloaded", "timeout", "timed out"))


class Completer:
    """Default completer: one single-turn model query. Tests substitute a fake."""

    def complete(self, system_prompt: str, prompt: str, *, max_tokens: int | None = None):
        """Return ``(text, usage_dict)``."""
        self.last_usage: dict = {}
        backend = str(_conf("PRCHECK_LLM_BACKEND", "anthropic")).strip().lower()
        if backend == "anthropic":
            return self._complete_anthropic(system_prompt, prompt, max_tokens=max_tokens)
        if backend in {"openai", "openai-compatible"}:
            return self._complete_openai(system_prompt, prompt, max_tokens=max_tokens)
        raise RuntimeError(f"unsupported PRCHECK_LLM_BACKEND: {backend}")

    # -- Anthropic ---------------------------------------------------------- #
    def _complete_anthropic(self, system_prompt, prompt, *, max_tokens=None):
        api_key = _conf("ANTHROPIC_API_KEY")
        model = _conf("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL
        base = str(_conf("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).rstrip("/")
        payload = {
            "model": model,
            "max_tokens": max_tokens or int(_conf("PRCHECK_LLM_MAX_TOKENS", 4096)),
            "temperature": float(_conf("PRCHECK_LLM_TEMPERATURE", 0)),
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }
        raw = self._post(
            f"{base}/v1/messages",
            payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            provider="anthropic",
        )
        data = json.loads(raw)
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        self.last_usage = {
            "provider": "anthropic",
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "usd": 0.0,
        }
        if not text.strip():
            raise RuntimeError(f"anthropic returned empty content (model={model})")
        return text, self.last_usage

    # -- OpenAI-compatible (incl. Azure OpenAI) ----------------------------- #
    def _complete_openai(self, system_prompt, prompt, *, max_tokens=None):
        api_key = _conf("PRCHECK_OPENAI_API_KEY")
        base = str(_conf("PRCHECK_OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        model = _conf("PRCHECK_OPENAI_MODEL") or "gpt-4o"
        api_version = _conf("PRCHECK_OPENAI_API_VERSION")  # set => Azure OpenAI
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": float(_conf("PRCHECK_LLM_TEMPERATURE", 0)),
            "max_tokens": max_tokens or int(_conf("PRCHECK_LLM_MAX_TOKENS", 4096)),
            "response_format": {"type": "json_object"},
        }
        if api_version:  # Azure: api-key header + api-version query, no /v1
            url = f"{base}/chat/completions?api-version={api_version}"
            headers = {"api-key": api_key, "content-type": "application/json"}
        else:
            url = f"{base}/chat/completions"
            headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
        try:
            raw = self._post(url, payload, headers=headers, provider="openai")
        except RuntimeError as exc:
            # Reasoning models (o-series, gpt-5.x) reject `max_tokens` and a
            # non-default `temperature`, and sometimes `response_format`. Adapt
            # the payload once from the provider's own 400 message and retry.
            adjusted = _adapt_reasoning_payload(payload, str(exc))
            if adjusted is None:
                raise
            payload = adjusted
            raw = self._post(url, payload, headers=headers, provider="openai")
        data = json.loads(raw)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("openai-compatible response had no choices")
        text = (choices[0].get("message") or {}).get("content") or ""
        finish = choices[0].get("finish_reason")
        # Reasoning models can spend the whole budget on hidden reasoning tokens
        # and return empty content (finish_reason=length). Retry once with a much
        # larger cap so big/complex files don't silently degrade.
        if not text.strip() and finish == "length":
            bumped = dict(payload)
            cap = int(bumped.get("max_completion_tokens") or bumped.get("max_tokens") or 4096)
            bumped.pop("max_tokens", None)
            bumped["max_completion_tokens"] = min(cap * 3, 32000)
            raw = self._post(url, bumped, headers=headers, provider="openai")
            data = json.loads(raw)
            choices = data.get("choices") or []
            text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
            usage = data.get("usage") or {}
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_usage = {
            "provider": "openai",
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
            "usd": 0.0,
        }
        if not text.strip():
            raise RuntimeError(f"openai-compatible returned empty content (model={model})")
        return text, self.last_usage

    # -- shared HTTP with retry/backoff ------------------------------------- #
    def _post(self, url: str, payload: dict, *, headers: dict, provider: str) -> str:
        timeout_s = float(_conf("PRCHECK_LLM_TIMEOUT_S", 120))
        max_attempts = max(1, int(_conf("PRCHECK_LLM_MAX_RETRIES", 4)))
        data = json.dumps(payload).encode("utf-8")
        headers = {**headers, "user-agent": "prcheck-reviews/1.0", "accept": "application/json"}
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            req = Request(url, data=data, headers=headers, method="POST")
            try:
                with urlopen(req, timeout=timeout_s) as resp:
                    return resp.read().decode("utf-8")
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = RuntimeError(f"{provider} HTTP {exc.code}: {detail}")
                if not _retryable(exc.code, detail) or attempt == max_attempts - 1:
                    raise last_error from exc
            except (URLError, TimeoutError) as exc:
                last_error = RuntimeError(f"{provider} request failed: {getattr(exc, 'reason', exc)}")
                if attempt == max_attempts - 1:
                    raise last_error from exc
            time.sleep(min(30.0, 2.0 ** attempt))
        raise last_error or RuntimeError(f"{provider} request failed")


def _adapt_reasoning_payload(payload: dict, error: str) -> dict | None:
    """Return a payload adjusted for a reasoning model, or None if unrelated.

    Only triggers on the specific parameter complaints these models raise, so a
    genuine 400 (bad request) is not masked by a blind retry.
    """
    err = error.casefold()
    changed = False
    new = dict(payload)
    if "max_completion_tokens" in err or ("max_tokens" in err and "unsupported" in err) \
            or "'max_tokens'" in err:
        new["max_completion_tokens"] = new.pop("max_tokens", None) or 4096
        changed = True
    if "temperature" in err:
        new.pop("temperature", None)
        changed = True
    if "response_format" in err:
        new.pop("response_format", None)
        changed = True
    return new if changed else None


def _extract_json(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = match.group(1) if match else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(candidate[start:end + 1])


def structured_call(
    completer: Completer,
    budget: Budget,
    *,
    session: str,
    system_prompt: str,
    prompt: str,
    schema: dict,
    stage: str | None = None,
    max_tokens: int | None = None,
) -> dict | None:
    """One schema-validated model call with one retry. ``None`` means degrade."""
    if resolve_backend() == "deterministic":
        LOGGER.info("reviews_llm_degraded stage=%s session=%s reason=no_backend", stage, session)
        return None

    attempt_prompt = prompt
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            text, usage = completer.complete(system_prompt, attempt_prompt, max_tokens=max_tokens)
        except Exception as exc:  # network / provider error
            LOGGER.warning(
                "reviews_llm_call_error stage=%s session=%s attempt=%d error=%s",
                stage, session, attempt, exc,
            )
            return None

        usage = usage or getattr(completer, "last_usage", {}) or {}
        budget.tick_turn(
            tokens=int(usage.get("total_tokens") or 0),
            usd=float(usage.get("usd") or 0.0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            provider=str(usage.get("provider") or ""),
        )
        LOGGER.info(
            "reviews_llm_call_success stage=%s session=%s attempt=%d tokens=%d latency_ms=%d",
            stage, session, attempt, int(usage.get("total_tokens") or 0),
            round((time.monotonic() - started) * 1000),
        )
        try:
            data = _extract_json(text)
            validate(data, schema)
            return data
        except (ValueError, SchemaError) as exc:
            LOGGER.warning(
                "reviews_llm_output_invalid stage=%s session=%s attempt=%d error=%s",
                stage, session, attempt, exc,
            )
            if attempt == 2:
                return None
            attempt_prompt = (
                f"{prompt}\n\nYour previous output failed validation: {exc}\n"
                "Respond with ONLY a valid JSON object matching the required schema."
            )
    return None
