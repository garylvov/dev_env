"""Claude window defaults for resolved model IDs, never mutable family aliases.

Verified 2026-09-25 against https://code.claude.com/docs/en/model-config
(Extended context, Sonnet 5 context window, third-party deployments) and
https://platform.claude.com/docs/en/models/overview . The fallback is conservative:
provider/gateway deployments can budget only 200K even for a 1M-capable model.
An actual reported window or Token Kit's explicit override can refine it.
"""
from __future__ import annotations

import os
from collections.abc import Mapping


_NATIVE_MILLION = frozenset({
    "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5",
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
})
_STANDARD = frozenset({
    "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-haiku-4-5", "claude-haiku-4-5-20251001",
})
_PROVIDERS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
              "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
              "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST")


def claude_window(model: str, *, reported=None,
                  environ: Mapping[str, str] | None = None) -> tuple[int | None, str | None]:
    """Return (window, provenance); unresolved/custom IDs remain unknown.

    ``reported`` is reserved for an actual client-reported window, not token usage.
    This function does not interpret arbitrary transcript fields as window sizes.
    Explicit Token Kit overrides take precedence at the runtime layer.
    """
    if reported is not None:
        if type(reported) is not int or reported <= 0:
            raise ValueError("Reported context window must be a positive integer")
        return reported, "reported"
    env = os.environ if environ is None else environ
    known = model in _NATIVE_MILLION | _STANDARD
    # Claude strips [1m] before recognizing a model. Recognition here governs
    # its explicit MAX_CONTEXT override only; defaults still require exact IDs.
    recognized = known or (model.lower().endswith("[1m]") and
                           model[:-4] in _NATIVE_MILLION | _STANDARD)
    enabled = lambda key: env.get(key, "").lower() in ("1", "true")
    declared = env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
    # The managed adapter sets DISABLE_COMPACT=1. For recognized and bare
    # claude-* IDs this is required for Claude's own declared window to apply.
    custom = not model.lower().startswith("claude-") and model != "unknown"
    declared_applies = (enabled("DISABLE_COMPACT") or custom)
    if not recognized and "[1m]" in model.lower() and not enabled("CLAUDE_CODE_DISABLE_1M_CONTEXT"):
        declared_applies = False
    if declared is not None and declared_applies:
        if not declared.isascii() or not declared.isdecimal() or int(declared) <= 0:
            raise ValueError("CLAUDE_CODE_MAX_CONTEXT_TOKENS must be a positive integer")
        return int(declared), "env_override:CLAUDE_CODE_MAX_CONTEXT_TOKENS"
    if not known:
        return None, None
    if enabled("CLAUDE_CODE_DISABLE_1M_CONTEXT"):
        return 200_000, "documented_default:disable_1m"
    base_url = env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    custom_provider = (base_url not in ("", "https://api.anthropic.com") or
                       any(env.get(key, "").lower() not in ("", "0", "false")
                           for key in _PROVIDERS))
    if custom_provider:
        return 200_000, "documented_default:provider_conservative"
    return (1_000_000 if model in _NATIVE_MILLION else 200_000), "documented_default"
