"""Classify a codex failure, and board a quota refusal where a reader sees it.

WHY THIS FILE EXISTS
    rc 42 was `absent|auth|busy|protocol`: three ways codex is missing and one
    way it is broken.  "the account is out of quota" was none of them, so a
    maxed-out codex looked like an ordinary turn failure (rc 3) and the caller
    retried into the same wall.  This module names that case from the protocol's
    own vocabulary and writes the one file a router can read.

WHERE THE NAMES COME FROM (codex-cli 0.153.4, read, not remembered)
    `codex app-server generate-json-schema --out <dir> --experimental`, then
    `v2/ErrorNotification.json`: the `error` notification carries a `TurnError`
    whose `codexErrorInfo` is one of
        contextWindowExceeded, sessionBudgetExceeded, usageLimitExceeded,
        rateLimitExceeded, serverOverloaded, cyberPolicy,
        misalignmentPolicyViolation, internalServerError, unauthorized,
        badRequest, threadRollbackFailed, sandboxError, other
    plus the object forms httpConnectionFailed / responseStreamConnectionFailed
    / responseStreamDisconnected / responseTooManyFailedAttempts (each with an
    `httpStatusCode`) and `activeTurnNotSteerable {turnKind: review|compact}`.
    The same enum appears snake_case in the binary's own strings
    (`usage_limit_exceeded`, `rate_limit_exceeded`, `rate_limit_reached`,
    `workspace_member_credits_depleted`, `spend_control_reached`), so both
    spellings are matched.

THE COOLDOWN MARKER
    `token_kit.router` is an empty reserved package today -- it has no cooldown
    verb to call.  So the marker is written directly, and if a `cooldown`
    callable ever appears on the router it is called as well.  The path is NOT
    in `agent_trigger_matrix.toml`'s `[codex]` block: that block has
    `run/binary/refusal_code/agents_dir/dispatch` and no cooldown key.  Adding
    one is in this lane's out.md under INSTALLER PATCH; until then the path is
    `<state dir>/codex-cooldown.json`, overridable with
    TOKEN_KIT_CODEX_COOLDOWN_MARKER.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

#: camelCase (wire) and snake_case (binary strings) spellings of "no quota".
QUOTA_ERROR_INFOS = frozenset({
    "usageLimitExceeded", "usage_limit_exceeded",
    "rateLimitExceeded", "rate_limit_exceeded",
    "rateLimitReached", "rate_limit_reached",
    "usageLimitReached", "usage_limit_reached",
    "workspaceMemberCreditsDepleted", "workspace_member_credits_depleted",
    "spendControlReached", "spend_control_reached",
})

#: Free-text fallbacks, for the paths where only `message` is populated.
QUOTA_TEXT_MARKERS = (
    "usage limit", "rate limit", "too many requests", "429",
    "credits depleted", "out of credits", "quota",
)

#: codexErrorInfo values that mean "codex is up, this turn cannot steer".
NOT_STEERABLE = "activeTurnNotSteerable"

REASON_QUOTA = "quota"


def error_info_name(error: Any) -> str:
    """The `codexErrorInfo` discriminator, whether it is a string or an object."""
    if isinstance(error, dict):
        info = error.get("codexErrorInfo")
    else:
        info = error
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        return next(iter(info))
    return ""


def is_quota(error: Any) -> bool:
    """True when this TurnError / JSON-RPC error means the account is maxed out."""
    name = error_info_name(error)
    if name in QUOTA_ERROR_INFOS:
        return True
    text = ""
    if isinstance(error, dict):
        text = " ".join(
            str(error.get(key, "")) for key in ("message", "additionalDetails", "data")
        )
    elif isinstance(error, str):
        text = error
    text = text.lower()
    return any(marker in text for marker in QUOTA_TEXT_MARKERS)


def state_root() -> Path:
    """Where job dirs and the cooldown marker live.

    Order: the explicit override, then the profile's evidence dir, then an
    XDG state dir.  No cluster path is ever written in a module (census rule).
    """
    override = os.environ.get("TOKEN_KIT_CODEX_JOB_DIR")
    if override:
        return Path(override)
    try:
        from token_kit import profiles as profiles_mod

        kit_dir = Path(__file__).resolve().parents[3]
        prof = profiles_mod.load(kit_dir / "profiles")
        return prof.evidence_dir / "codex-jobs"
    except Exception:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        return Path(base) / "token_kit" / "codex-jobs"


def cooldown_marker_path() -> Path:
    override = os.environ.get("TOKEN_KIT_CODEX_COOLDOWN_MARKER")
    if override:
        return Path(override)
    return state_root().parent / "codex-cooldown.json"


def board_quota(detail: str, *, retry_after_s: float = 900.0) -> Path:
    """Write the cooldown marker, and call the router's verb if one exists.

    Returns the marker path.  Never raises: a refusal that cannot be boarded
    must still reach the caller as rc 42, and the caller prints the reason.
    """
    path = cooldown_marker_path()
    payload = {
        "reason": REASON_QUOTA,
        "detail": detail[:2000],
        "host": socket.gethostname(),
        "at": time.time(),
        "retry_after_s": retry_after_s,
        "until": time.time() + retry_after_s,
        "source": "token_kit.codex",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    try:  # the router may grow a cooldown verb; call it when it does.
        from token_kit import router as router_mod

        verb = getattr(router_mod, "cooldown", None)
        if callable(verb):
            verb(**payload)
    except Exception:
        pass
    return path


def cooldown_active(now: float | None = None) -> float:
    """Seconds left on a boarded cooldown, 0.0 when there is none."""
    path = cooldown_marker_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    left = float(data.get("until", 0.0)) - (now if now is not None else time.time())
    return left if left > 0 else 0.0
