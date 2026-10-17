"""Swappable LLM layer. The agents only ever ask for JSON or short text.

LLM_PROVIDER = anthropic | openai | gemini | offline   (default: first one with a key, else offline)

`offline` needs no key: agents fall back to rule-based planning and answers
built straight from the graph rows, so the whole desk still runs (and the
graph is still doing all the work).
"""
from __future__ import annotations

import json
import os
import re

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-5",
    "gemini": "gemini-2.5-flash",
}


def provider() -> str:
    p = os.getenv("LLM_PROVIDER", "").lower()
    if p:
        return p
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    return "offline"


def model_name() -> str:
    p = provider()
    return os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(p, "rules")


def _anthropic(system: str, user: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic()
    kwargs = dict(
        model=model_name(),
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user}],
    )
    try:
        # Server-side fallback: if the request is declined, the API reroutes it itself.
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except anthropic.BadRequestError:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    return "".join(b.text for b in resp.content if b.type == "text")


def _openai(system: str, user: str, max_tokens: int) -> str:
    from openai import OpenAI

    resp = OpenAI().chat.completions.create(
        model=model_name(),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_completion_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


def _gemini(system: str, user: str, max_tokens: int) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    resp = client.models.generate_content(
        model=model_name(),
        contents=user,
        config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=max_tokens),
    )
    return resp.text or ""


_BACKENDS = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini}


def available() -> bool:
    return provider() in _BACKENDS


def text(system: str, user: str, max_tokens: int = 2000) -> str | None:
    if not available():
        return None
    return _BACKENDS[provider()](system, user, max_tokens).strip()


def json_call(system: str, user: str, max_tokens: int = 2000) -> dict | None:
    """Ask for a JSON object; tolerate code fences and surrounding prose."""
    out = text(system + "\nReply with one JSON object only, no prose.", user, max_tokens)
    if out is None:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
