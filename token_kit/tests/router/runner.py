"""runner.py -- feed router_cases.jsonl to hook.py and check every expectation.

The case file is the behaviour contract of the SOFT router, one case per line,
and this module is the only thing that knows how to stage one: a tmp world, a
markdown guide built from the fixture, an optional `[router]` config file, an
optional fake subagent transcript, then hook.handle() with the case stdin.

Three substitutions and no others are applied to the stored case text, so the
kit carries no machine path: `$CWD` for the session's working directory,
`$AGENTS_DIR` for the codex runner agents directory and `$TMP` for the world.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE / "router_cases.jsonl"
FIXTURE_MATRIX = HERE / "fixtures" / "matrix.md"


class Env:
    """The tmp world one run of the case file happens in."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state = tmp / "state"
        self.projects = tmp / "projects"
        self.agents = tmp / "agents"
        self.slots = tmp / "codex-slots"
        self.root = tmp / "root"
        for d in (self.state, self.projects, self.agents, self.slots, self.root):
            d.mkdir(parents=True, exist_ok=True)

    def expand(self, text: str) -> str:
        return (text.replace("$CWD", str(self.root))
                    .replace("$DATA_ROOT", str(self.root))
                    .replace("$AGENTS_DIR", str(self.agents))
                    .replace("$TMP", str(self.tmp)))


def _apply_replaces(text: str, pairs) -> str:
    """`sed -i "s|<re>|<rep>|"` semantics: per line, first match only."""
    out = []
    for line in text.splitlines(keepends=True):
        for rx, rep in pairs:
            line = re.sub(rx, lambda _m, r=rep: r, line, count=1)
        out.append(line)
    return "".join(out)


def build_matrix(case: dict, env: Env, base_text: str, dest: Path) -> Path:
    fixture = case.get("matrix_fixture", "live")
    if isinstance(fixture, dict):
        if "literal" in fixture:
            dest.write_text(fixture["literal"], encoding="utf-8")
            return dest
        pairs = [(p[0], p[1]) for p in fixture.get("replace", [])]
        dest.write_text(_apply_replaces(base_text, pairs), encoding="utf-8")
        return dest
    dest.write_text(base_text, encoding="utf-8")
    return dest


def write_router_config(case: dict, env: Env, index: int) -> None:
    """The `[router]` overrides for this case, or no config file at all.

    Codex settings are code defaults now, so this ONE optional file is the
    only way a case can make codex present, absent, bound-out or wrapper-less
    -- and writing it exercises the reader that production also uses.
    """
    cfg = case.get("router_config")
    if not cfg:
        os.environ["TOKEN_KIT_CONFIG"] = str(env.tmp / "no-such-config.toml")
        return
    lines = ["[router]"]
    for key, value in cfg.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f'{key} = "{env.expand(str(value))}"')
    path = env.tmp / f"config.{index}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["TOKEN_KIT_CONFIG"] = str(path)


def write_transcript(env: Env, case: dict, session: str, cwd: str) -> None:
    pf = case.get("projects_fixture")
    if not pf:
        return
    from token_kit.router.hook import slug
    lane = Path(env.expand(pf.get("lane_dir", str(env.tmp / "lane"))))
    lane.mkdir(parents=True, exist_ok=True)
    d = env.projects / slug(cwd) / session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"agent-{pf['agent_id']}.jsonl"
    lines = [json.dumps({"type": "user", "message": {"content": [
        {"type": "text", "text": f"LANE_DIR: {lane}"}]}})]
    call = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
    lines += [call] * int(pf["calls"])
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_one(case: dict, env: Env, base_text: str, index: int) -> list[str]:
    """Returns the list of failure messages for this case (empty == pass)."""
    from token_kit.router import hook as hook_mod

    mx = build_matrix(case, env, base_text, env.tmp / f"mx.{index}.md")
    os.environ.update({
        "LANE_RECYCLER_MATRIX": str(mx),
        "LANE_RECYCLER_STATE": str(env.state),
        "LANE_RECYCLER_PROJECTS_ROOT": str(env.projects),
        "TOKEN_KIT_CODEX_SLOT_DIR": str(env.slots),
        # the machine-wide dispatcher marker, pointed at nothing: a real one
        # on this host must not decide a case.
        "TOKEN_KIT_CODEX_COOLDOWN_MARKER": str(env.tmp / "no-dispatcher-cooldown.json"),
    })
    os.environ.pop("LANE_RECYCLER_DATA_ROOT", None)
    os.environ.pop("TK_EVIDENCE_DIR", None)
    write_router_config(case, env, index)
    for key in ("LANE_RECYCLER_WARN", "LANE_RECYCLER_FLOOR", "LANE_RECYCLER_HARD"):
        os.environ.pop(key, None)

    expected = case.get("expected", {})
    bad: list[str] = []

    # -- mode cases --------------------------------------------------------
    mode = case.get("mode")
    if mode:
        buf = io.StringIO()
        with redirect_stdout(buf):
            hook_mod.main([mode])
        out = buf.getvalue()
        for s in expected.get("stdout_contains", []):
            if s not in out:
                bad.append(f"stdout missing: {s}")
        for s in expected.get("stdout_absent", []):
            if s in out:
                bad.append(f"stdout should not contain: {s}")
        mw = expected.get("stdout_max_words")
        if mw is not None and len(out.split()) >= mw:
            bad.append(f"{len(out.split())} words >= {mw}")
        jp = expected.get("json_path")
        if jp:
            node = json.loads(out)
            for part in jp.split("."):
                node = node.get(part, {})
            for s in expected.get("json_path_contains", []):
                if s not in str(node):
                    bad.append(f"{jp} missing: {s}")
        for path, want in (expected.get("json_field") or {}).items():
            node = json.loads(out)
            for part in path.split("."):
                node = node.get(part, {})
            if node != want:
                bad.append(f"{path} = {node!r}, expected {want!r}")
        return bad

    # -- hook cases --------------------------------------------------------
    stdin = json.loads(env.expand(json.dumps(case["hook_stdin"])))
    session = str(stdin.get("session_id", ""))
    write_transcript(env, case, session, str(stdin.get("cwd", "")))

    pre = case.get("precondition") or {}
    if "write_marker" in pre:
        marker = Path(env.expand(pre["write_marker"].replace("$STATE", str(env.state))))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("armed\n", encoding="utf-8")
        age = float(pre.get("age_s", 0))
        os.utime(marker, (time.time() - age, time.time() - age))

    if "write_agent" in pre:
        env.agents.mkdir(parents=True, exist_ok=True)
        (env.agents / f"{pre['write_agent']}.md").write_text(
            f"---\nname: {pre['write_agent']}\n---\n", encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf):
        hook_mod.handle(stdin)
    raw = buf.getvalue().strip()
    out = json.loads(raw) if raw else {}
    hso = out.get("hookSpecificOutput", {})
    decision = hso.get("permissionDecision", "allow")
    reason = hso.get("permissionDecisionReason", "")
    updated = hso.get("updatedInput", {})
    new_prompt = updated.get("prompt", "")

    if decision != expected["decision"]:
        bad.append(f"decision {decision}, expected {expected['decision']}")
    for field in ("model", "subagent_type"):
        if field in expected:
            got = updated.get(field, "NULL")
            want = expected[field] if expected[field] is not None else "NULL"
            if got != want:
                bad.append(f"{field}={got} expected {want}")
    for s in expected.get("prompt_header_contains", []):
        if s not in new_prompt:
            bad.append(f"prompt header missing: {s}")
    for s in expected.get("prompt_header_absent", []):
        if s in new_prompt:
            bad.append(f"prompt header should not contain: {s}")
    if expected.get("reason_contains") and expected["reason_contains"] not in reason:
        bad.append(f"reason missing: {expected['reason_contains']}")
    for s in expected.get("reason_contains_all", []):
        if s not in reason:
            bad.append(f"reason missing: {s}")

    # the router writes spawn_events.tsv per session; the cap writes
    # events.tsv per agent.  A case may expect a row from either.
    agent_id = str(stdin.get("agent_id") or "")
    paths = [env.state / session / "spawn_events.tsv"]
    if agent_id:
        paths.append(env.state / session / agent_id / "events.tsv")
    events = "".join(p.read_text(encoding="utf-8") for p in paths if p.is_file())
    if expected.get("event_row") and expected["event_row"] not in events:
        bad.append(f"no event row matching: {expected['event_row']!r}")
    if expected.get("candidate"):
        # the chosen one is logged as `candidate<TAB><row>/<candidate>`; a skip
        # is a different verb, so an exact field match cannot confuse the two.
        chosen = [ln.split("\t")[4] for ln in events.splitlines()
                  if len(ln.split("\t")) > 4 and ln.split("\t")[3] == "candidate"]
        if not any(c.endswith("/" + expected["candidate"]) for c in chosen):
            bad.append(f"chosen candidate not logged: {expected['candidate']} (logged: {chosen})")
    return bad


def load_cases(path: Path = CASES) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if c.get("kind") == "note":
            continue
        cases.append(c)
    return cases


def run_all(tmp: Path, cases_path: Path = CASES,
            matrix_path: Path = FIXTURE_MATRIX) -> tuple[int, int, list[str]]:
    """(passed, failed, messages).  Cases run in file order; some depend on it."""
    env = Env(tmp)
    base_text = env.expand(matrix_path.read_text(encoding="utf-8"))
    raw_cases = env.expand(cases_path.read_text(encoding="utf-8"))
    passed = failed = 0
    messages: list[str] = []
    saved = dict(os.environ)
    try:
        for i, line in enumerate(raw_cases.splitlines()):
            if not line.strip():
                continue
            case = json.loads(line)
            if case.get("kind") == "note":
                continue
            bad = run_one(case, env, base_text, i)
            if bad:
                failed += 1
                messages.extend(f"FAIL {case['name']} -- {b}" for b in bad)
            else:
                passed += 1
                messages.append(f"ok   {case['name']}")
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return passed, failed, messages
