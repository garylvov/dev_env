"""hook.py -- ONE entry point for the PreToolUse and SessionStart events.

Two jobs, one file, exactly as the bash reader had them:

  A) THE ROUTER, on `tool_name == "Agent"`.  Reads agent_trigger_matrix.toml,
     picks a row, runs the `prefer` ladder, and REWRITES the spawn through
     hookSpecificOutput.updatedInput -- model, subagent_type and a header
     prepended to the prompt.  Proven to take effect in CLI 2.1.278.

  B) THE CALL CAP, on every other tool inside a lane.  The CLI is the sole
     writer of the count: we read the tool_use blocks in
     <projects>/<slug>/<session>/subagents/agent-<id>.jsonl and never keep a
     counter of our own.  The discriminator is `agent_id`: a subagent's stdin
     carries one, the main thread's does not, and session_id is shared.  No
     process scan happens anywhere here, so law 14 cannot bite.

  and SessionStart, which injects the generated row menu as additionalContext.

THE TRAP THE TWO THRESHOLDS SOLVE: a hard deny at the cap denies the very
Write the dying agent needs to save its state, so the lane dies with nothing.
Hence a one-shot WARN deny that tells the agent its count, and a FLOOR past
which only the lane's own out.md write and SubagentHandback survive.

FAIL OPEN, LOUDLY: every failure path here allows the call and appends an
event row.  A router that blocks a spawn, or a cap that wedges a lane, is a
worse defect than one that misses.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

#: Substrings the CLI's own tools use; the floor lets exactly these through.
HANDBACK_TOOL = "SubagentHandback"


# --------------------------------------------------------------------------
# where things live.  Every one of these is an env key the installer's env.sh
# already writes, so no module here carries a machine path.
# --------------------------------------------------------------------------
def state_root() -> Path:
    env = os.environ.get("LANE_RECYCLER_STATE")
    if env:
        return Path(env)
    base = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.expanduser("~")
    return Path(base) / ".lane_recycler"


def projects_root() -> Path:
    env = os.environ.get("LANE_RECYCLER_PROJECTS_ROOT")
    if env:
        return Path(env)
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(claude) / "projects"


def data_root(payload: dict) -> str:
    """Paths in a brief are matched relative to this.

    The session's own cwd is the honest default: it is the project root the
    globs in the table are written against.  The installer overrides it.
    """
    return os.environ.get("LANE_RECYCLER_DATA_ROOT") or str(payload.get("cwd") or "")


def evidence_dir(payload: dict) -> str:
    env = os.environ.get("TK_EVIDENCE_DIR")
    if env:
        return env.rstrip("/")
    return f"{data_root(payload).rstrip('/')}/agrescap/evidence"


def slug(cwd: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in cwd)


# --------------------------------------------------------------------------
# output primitives
# --------------------------------------------------------------------------
def _emit(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
    return 0


def allow() -> int:
    """Silence plus exit 0 is how a hook says 'no opinion'."""
    return 0


def deny(reason: str) -> int:
    return _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                         "permissionDecision": "deny",
                                         "permissionDecisionReason": reason}})


def rewrite(payload: dict, reason: str, updated: dict) -> int:
    tool_input = dict(payload.get("tool_input") or {})
    tool_input.update(updated)
    return _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                         "permissionDecision": "allow",
                                         "permissionDecisionReason": reason,
                                         "updatedInput": tool_input}})


class Events:
    """`cat >>` rows only -- one writer, never a rewrite (NFS)."""

    def __init__(self, path: Path, agent_id: str = "none", calls: int = -1):
        self.path = path
        self.agent_id = agent_id or "none"
        self.calls = calls

    def row(self, kind: str, detail: str = "") -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp}\t{self.agent_id}\t{self.calls}\t{kind}\t{detail}\n")
        except OSError:
            pass


def claim(path: Path) -> bool:
    """One-shot marker.  True only for the first claimant, ever."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    os.close(fd)
    return True


# --------------------------------------------------------------------------
# A) the router
# --------------------------------------------------------------------------
def _budget_line(warn: int, floor: int, hard: int) -> str:
    return (f"budget: warn {warn} / floor {floor} / hard {hard} calls; past the floor only a\n"
            f"write of your own out.md and SubagentHandback are permitted.")


def _spawn(payload: dict) -> int:
    from . import ladder, matrix as matrix_mod, route as route_mod

    session = str(payload.get("session_id") or "nosession")
    sstate = state_root() / session
    events = Events(sstate / "spawn_events.tsv", calls=0)

    try:
        m = matrix_mod.load()
    except matrix_mod.MatrixError as exc:
        events.row("matrix_parse_failed", str(exc)[:200])
        return allow()

    tool_input = payload.get("tool_input") or {}
    desc = str(tool_input.get("description") or "")
    prompt = str(tool_input.get("prompt") or "")
    warn, floor, hard = m.budget()

    try:
        name, how = route_mod.route(m, desc, prompt, data_root(payload))
    except route_mod.UnknownRow as exc:
        events.row("deny_unknown_row", exc.name)
        return deny(f"MATRIX - unknown row '{exc.name}'. The valid rows are:\n"
                    f"{matrix_mod.menu_text(m)}\n"
                    "Fix the ROW: line in the spawn prompt, or delete it and let the "
                    "description route.")

    if name is None:
        return _unrouted(payload, m, events, sstate, desc, prompt, warn, floor, hard)

    row = m.row(name) or {}
    shape = str(row.get("shape", "claude-direct"))

    if shape == "refuse" or row.get("action") == "refuse-and-substitute":
        events.row("refuse", name)
        watch = f"{evidence_dir(payload)}/watch/<name>.tsv"
        return deny(
            f"MATRIX ROW {name} - REFUSED. A model may not be the loop. Do this instead: "
            f"write a shell loop that appends ONE line per sample to a status file at "
            f"{watch}, start it with nohup, and read the file ONCE when you next need it. "
            f"A model may read the status file; a model may not be the waiting. "
            f"Done when: {row.get('stop', '')}. If you believe this spawn is not a "
            f"watch/poll/wait, put an explicit 'ROW: <name>' line in the prompt.")

    # -- the ladder --------------------------------------------------------
    chosen, skipped = ladder.choose(row, m, sstate)
    for cand, reason in skipped:
        events.row("skip_candidate", f"{name}/{cand.text}/{reason}")
    if chosen is not None:
        events.row("candidate", f"{name}/{chosen.text}")
    else:
        events.row("no_candidate", name)

    claude_models = [c.model for c in ladder.candidates(row) if not c.is_codex]
    out_model = (chosen.model if (chosen and not chosen.is_codex)
                 else (claude_models[0] if claude_models
                       else str(row.get("model") or m.default_model())))
    out_agent = str(row.get("agent") or "general-purpose")
    effort = (chosen.effort if chosen else str(row.get("effort")
                                               or m.defaults.get("effort", "medium")))

    # -- the shape header --------------------------------------------------
    shape_line = ""
    codex_reason = ladder.codex_unavailable_reason(m.codex, sstate)
    rc = m.codex.get("refusal_code", 42)

    if chosen is not None and chosen.is_codex:
        cmd = ladder.dispatch_command(m.codex, chosen)
        wrapper = Path(os.path.expanduser(str(m.codex.get("agents_dir", "")))) / f"codex-{name}.md"
        if str(m.codex.get("agents_dir", "")) and wrapper.is_file():
            out_agent = wrapper.stem
        shape_line = (
            f"codex runs this row. Dispatch it with EXACTLY this command - the model and "
            f"the effort are the chosen candidate's, never the config default:\n  {cmd}\n"
            f"You cannot spawn an agent; that dispatcher IS the delegation path. If it exits "
            f"{rc} (its refusal code) with reason=quota, report it back with "
            f"`router cooldown --arm` so later spawns skip codex, and do the work yourself.")
    elif shape == "codex-direct":
        events.row("codex_fallback", name)
        shape_line = (f"codex is not available here ({codex_reason or 'no codex candidate'}), so "
                      f"this row FELL BACK to its Claude candidate {out_model}. Do the work "
                      f"yourself.")
    elif shape == "claude-plans-codex-executes":
        if codex_reason is None:
            cmd = str(m.codex.get("dispatch", ""))
            shape_line = (
                "delegation: you own the judgement. Do NOT do mechanistic reads yourself - hand "
                "every lookup, log read, grep and deterministic transform to codex with:\n"
                f"  {cmd}\n"
                f"You cannot spawn an agent; that dispatcher script IS the delegation path. If "
                f"it exits {rc} (its refusal code), stop delegating and do the reads yourself "
                f"for the rest of this lane.")
        else:
            events.row("codex_fallback", name)

    reviewer = str(row.get("reviewer") or "")
    header = (
        f"[MATRIX ROW: {name} | shape={shape} | routed by {how}]\n"
        f"candidate: {chosen.text if chosen else 'none available - row model ' + out_model}\n"
        f"load: {row.get('load') or 'none - the row carries its own judgement'}\n"
        f"stop: {row.get('stop') or '(none named)'}\n"
        f"max_report: {row.get('max_report', m.defaults.get('max_report', 2000))} bytes - a "
        f"longer report is truncated with a pointer\n"
        f"repo_home: {row.get('repo_home', 'unset')} (law 7)\n"
        f"effort: {effort}\n"
        + (f"reviewer: {reviewer} must run on the result before it is done\n" if reviewer else "")
        + _budget_line(warn, floor, hard)
        + (f"\n{shape_line}" if shape_line else "")
    )

    events.row("routed", f"{name}/{how}/{out_agent}/{out_model}")
    return rewrite(payload,
                   f"matrix: row {name} ({how}) -> {out_agent}/{out_model}",
                   {"subagent_type": out_agent, "model": out_model,
                    "prompt": header + "\n\n" + prompt})


def _unrouted(payload, m, events, sstate, desc, prompt, warn, floor, hard) -> int:
    from . import matrix as matrix_mod

    events.row("unrouted", desc or "(no description)")
    default_model = m.default_model()
    if claim(sstate / "menu_denied"):
        return deny(
            f"MATRIX - this spawn matched no row, so it would run on [defaults] "
            f"(model {default_model}, no load, no stop condition, no shape). This deny happens "
            f"ONCE per session; retry the SAME spawn and it will go through unrouted. Before "
            f"you retry, add a 'ROW: <name>' line as the FIRST line of the spawn prompt:\n"
            f"{matrix_mod.menu_text(m)}\n"
            f"If none fits, retry unchanged and open a row for it.")
    header = (
        f"[MATRIX: UNROUTED - no row matched description \"{desc}\"]\n"
        f"This spawn is running on [defaults] (model {default_model}). An unrouted spawn is the\n"
        f"signal that the table needs a row. Say so in one line of your report.\n"
        f"{_budget_line(warn, floor, hard)}")
    return rewrite(payload, "matrix: unrouted, defaults applied",
                   {"model": default_model, "prompt": header + "\n\n" + prompt})


# --------------------------------------------------------------------------
# B) the call cap
# --------------------------------------------------------------------------
def count_calls(jsonl: Path) -> int:
    """tool_use BLOCKS, not records -- the count the budget is written against."""
    n = 0
    try:
        with jsonl.open(encoding="utf-8") as fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list):
                    n += sum(1 for b in content
                             if isinstance(b, dict) and b.get("type") == "tool_use")
    except OSError:
        return -1
    return n


def lane_dir_of(jsonl: Path) -> str:
    """The `LANE_DIR: <abs path>` line in the spawn prompt, first record.

    Nothing has to be registered anywhere: the CLI already wrote the brief.
    """
    import re
    try:
        with jsonl.open(encoding="utf-8") as fh:
            first = fh.readline()
        rec = json.loads(first)
    except (OSError, ValueError):
        return ""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        text = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    else:
        text = str(content or "")
    m = re.search(r"LANE_DIR:[ \t]*(/\S+)", text)
    if m:
        return m.group(1)
    m = re.search(r"(/\S+)/in\.md", text)
    return m.group(1) if m else ""


def _cap(payload: dict) -> int:
    from . import matrix as matrix_mod

    agent_id = str(payload.get("agent_id") or "")
    session = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")
    tool = str(payload.get("tool_name") or "")
    target = str((payload.get("tool_input") or {}).get("file_path") or "")

    if not agent_id or not session or not cwd:
        return allow()

    state = state_root() / session / agent_id
    events = Events(state / "events.tsv", agent_id=agent_id)
    jsonl = projects_root() / slug(cwd) / session / "subagents" / f"agent-{agent_id}.jsonl"

    if not jsonl.is_file():
        events.row("unresolved", str(jsonl))
        return allow()

    calls = count_calls(jsonl)
    events.calls = calls
    if calls <= 0:
        events.row("uncountable", str(jsonl))
        return allow()

    try:
        m = matrix_mod.load()
        warn, floor, hard = m.budget()
    except matrix_mod.MatrixError as exc:
        events.row("matrix_parse_failed", str(exc)[:200])
        return allow()

    lane = lane_dir_of(jsonl)
    out_md = f"{lane}/out.md" if lane else ""

    is_state_write = False
    if tool in ("Write", "Edit", "NotebookEdit"):
        is_state_write = bool(out_md) and target == out_md
    elif tool == HANDBACK_TOOL:
        is_state_write = True
    elif tool == "Read":
        is_state_write = bool(out_md) and target in (out_md, f"{lane}/in.md")

    if calls < warn:
        return allow()

    if calls >= floor:
        if is_state_write:
            events.row("permit_state_write", tool)
            return allow()
        if lane and claim(state / "respawn_requested"):
            try:
                with (Path(lane) / "RESPAWN_REQUEST.md").open("a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\tlane={lane}\t"
                             f"agent={agent_id}\tcalls={calls}\treason=floor\t"
                             f"resume=in.md+out.md\n")
            except OSError:
                pass
        events.row("deny_floor", tool)
        if calls >= hard:
            return deny(f"LANE RECYCLER - PAST HARD CEILING. calls={calls} hard={hard}. Stop "
                        f"now: call {HANDBACK_TOOL} with one line saying out.md is written (or "
                        f"not) and why. No other tool will be permitted.")
        return deny(
            f"LANE RECYCLER - FLOOR. calls={calls} floor={floor} hard={hard}. This lane is over "
            f"budget and is being recycled. ONLY these are still permitted: the Write tool on "
            f"{out_md or 'your out.md'} (not a bash heredoc - bash is denied here), and "
            f"{HANDBACK_TOOL}. Write out.md now in the brief's format: line 1 RESULT, line 2 "
            f"absolute paths, a RESUME block whose first line is the single next command, then "
            f"'### SESSION n status=CONTINUE next=<one line>'. Then hand back: 'recycled at "
            f"{calls} calls; out.md written; respawn a fresh lane with the same in.md'. Do not "
            f"explain, do not verify, do not read anything else.")

    if is_state_write:
        return allow()
    if claim(state / "warned"):
        events.row("deny_warn", tool)
        return deny(
            f"LANE RECYCLER - WARN (one time only; your next call goes through). calls={calls} "
            f"warn={warn} floor={floor}. From the floor on, only a Write to "
            f"{out_md or 'your out.md'} and {HANDBACK_TOOL} are permitted. Estimate the calls "
            f"your remaining work needs. If it does not fit in {floor - calls} calls, stop that "
            f"work NOW, write out.md (RESULT line, absolute paths, RESUME block whose first "
            f"line is the single next command, '### SESSION n status=CONTINUE next=<one "
            f"line>'), and hand back asking for a fresh lane on the same in.md. If it does fit, "
            f"continue and finish.")
    events.row("warn_passed", tool)
    return allow()


# --------------------------------------------------------------------------
# the entry point cli.py calls
# --------------------------------------------------------------------------
def handle(payload: dict) -> int:
    if not isinstance(payload, dict) or not payload:
        return allow()
    try:
        if payload.get("hook_event_name") == "SessionStart":
            return session_start()
        if payload.get("tool_name") == "Agent":
            return _spawn(payload)
        return _cap(payload)
    except Exception as exc:  # noqa: BLE001 -- fail OPEN, loudly, never wedge a session
        try:
            Events(state_root() / str(payload.get("session_id") or "nosession")
                   / "spawn_events.tsv").row("hook_error", f"{type(exc).__name__}: {exc}"[:300])
        except Exception:  # pragma: no cover
            pass
        return allow()


def session_start() -> int:
    from . import matrix as matrix_mod

    try:
        m = matrix_mod.load()
    except matrix_mod.MatrixError:
        return allow()
    return _emit({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": matrix_mod.menu_text(m)}})


# --------------------------------------------------------------------------
# modes -- not a hook; for the menu, the guard and humans
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    from . import matrix as matrix_mod

    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else ""

    if mode == "--menu":
        print(matrix_mod.menu_text(matrix_mod.load()))
        return 0
    if mode == "--session-start":
        return session_start()
    if mode == "--compile":
        m = matrix_mod.load()
        print(json.dumps({"budget": dict(zip(("warn", "floor", "hard"), m.budget())),
                          "defaults": m.defaults, "codex": m.codex,
                          "concurrency": m.concurrency, "rows": m.rows}, indent=2))
        return 0
    if mode == "--validate":
        problems = matrix_mod.validate(matrix_mod.load())
        for p in problems:
            print(f"  {p}")
        print(f"matrix: {len(problems)} problem(s)")
        return 1 if problems else 0
    if mode == "cooldown":
        return _cooldown(argv[1:])
    if mode == "--gen-agents":
        from .gen_agents import main as gen_main
        return gen_main(argv[1:])

    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return allow()
    return handle(payload)


def _cooldown(argv: list[str]) -> int:
    """`router cooldown --arm --session <id>` after a dispatcher rc 42 quota.

    This is the reader half of the refusal: the dispatcher cannot write the
    marker itself (it does not know the session), so whoever ran it reports it
    back here, and every later spawn in the session skips codex candidates
    until the window passes.
    """
    from . import ladder, matrix as matrix_mod

    arm = "--arm" in argv
    session = "nosession"
    reason = "quota"
    for i, a in enumerate(argv):
        if a == "--session" and i + 1 < len(argv):
            session = argv[i + 1]
        if a == "--reason" and i + 1 < len(argv):
            reason = argv[i + 1]
    if not arm:
        print("usage: cooldown --arm [--session <id>] [--reason quota]", file=sys.stderr)
        return 2
    if reason != "quota":
        print(f"cooldown: reason={reason} does not arm the cooldown (only 'quota' does; "
              f"'absent' and 'busy' are per-call)", file=sys.stderr)
        return 0
    m = matrix_mod.load()
    sstate = state_root() / session
    marker = ladder.arm_cooldown(m.codex, sstate)
    Events(sstate / "spawn_events.tsv").row("cooldown_armed", f"{reason}/{marker}")
    print(str(marker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
