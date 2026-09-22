#!/usr/bin/env python3
"""token_kit -- put the whole token-spend kit on a machine with one command.

Subcommands: install, uninstall, probe, census, config, hook, prompts.

Reached through `install.sh`, which is a thin bootstrap: it makes sure `uv` is
present and then runs this file with a modern Python. Nothing here imports a
third-party package, so there is no environment to build and a hook launched
this way starts in well under a tenth of a second on a warm machine.

Promises the installer keeps:
  * writes only inside ~/.claude, ~/.config/token_kit, and the clone;
  * idempotent -- a second run changes nothing and says NO CHANGES;
  * everything installed is a SYMLINK back into the clone, so `git pull`
    updates the machine and nothing goes stale;
  * never overwrites a file it does not own: a real file where a symlink
    belongs is reported as a CONFLICT and left exactly as it was;
  * backs up settings.json with a dated name, then merges with the rules in
    settings_merge.py;
  * --uninstall removes exactly what the manifest says was added.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# argparse, shutil, subprocess, time and datetime are imported inside the
# functions that need them. The `hook` path runs on EVERY tool call, so it
# imports as little as it can get away with -- measured worth ~20 ms a call.

if __package__ in (None, ""):  # run as a plain file path by `uv run`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The kit's own modules are imported inside the commands for the same reason:
# nothing the hook does not need is loaded on the hook path.
if False:  # for readers and type checkers only
    from token_kit.manifest import Manifest

KIT_DIR = Path(__file__).resolve().parent.parent.parent   # .../token_kit
CLI_PATH = Path(__file__).resolve()

#: The routing chart, by preferred name. It is a markdown chart a person edits
#: by hand; the .toml spelling is accepted while the change lands.
MATRIX_NAMES = ("agent_trigger_matrix.md",)


def matrix_path() -> Path:
    for name in MATRIX_NAMES:
        candidate = KIT_DIR / name
        if candidate.is_file():
            return candidate
    return KIT_DIR / MATRIX_NAMES[0]


# The units this installer puts on a machine.
COMPONENTS = [
    ("config-env", "the resolved machine facts, in ~/.config/token_kit/env.sh"),
    ("settings-keys", "the measured settings.json keys"),
    ("pretooluse-hook", "the PreToolUse hook (call cap + matrix routing)"),
    ("sessionstart-menu", "the SessionStart row menu, generated from the chart"),
    ("respawn-reader", "the PostToolUse(Agent)+Stop lane respawn reader"),
    ("supervisor", "token-kit-supervise, the rollover supervisor"),
    ("codex-bin", "the codex-dispatch / codex-job / codex-run shims"),
    ("prompts", "token-kit-prompts, the verbatim user-prompt extractor"),
    ("task", "token-kit-task, titled working folders that can be found again"),
    ("kind-agents", "one generated agent file per kind and Claude candidate"),
    ("agent-trigger-matrix", "the routing chart itself"),
]

#: Executables linked into ~/.config/token_kit/bin. The name on the left is
#: what the operator types and what settings.json points at.
EXECUTABLES = [
    ("respawn-reader", "src/token_kit/respawn/bin/respawn-reader"),
    ("token-kit-supervise", "src/token_kit/supervisor/bin/token-kit-supervise"),
    ("codex-dispatch", "src/token_kit/codex/bin/codex-dispatch"),
    ("codex-job", "src/token_kit/codex/bin/codex-job"),
    # The interactive entry point: the kit's own launcher, so a new cluster
    # machine needs no hand-written wrapper in anybody's home directory.
    ("codex-run", "src/token_kit/codex/bin/codex-run"),
    # What the user typed, extracted from the transcripts on demand and at
    # every rollover. An operator runs it by hand too, so it is on PATH.
    ("token-kit-prompts", "src/token_kit/bin/token-kit-prompts"),
    # The working folder itself: titled at creation, retitled once the work is
    # understood, and findable from any directory through the machine wide index.
    ("token-kit-task", "src/token_kit/bin/token-kit-task"),
    ("token-kit-workflow", "src/token_kit/bin/token-kit-workflow"),
]

# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
class Report:
    def __init__(self, dry: bool):
        self.dry = dry
        self.changes = 0
        self.conflicts = 0
        self.failures = 0

    def ok(self, msg): print(f"  ok       {msg}")
    def note(self, msg): print(f"  {msg}")
    def plan(self, msg): print(f"  would    {msg}")

    def change(self, msg):
        self.changes += 1
        print(f"  CHANGE   {msg}")

    def conflict(self, msg):
        self.conflicts += 1
        print(f"  CONFLICT {msg}")

    def fail(self, msg):
        self.failures += 1
        print(f"  FAIL     {msg}")


# --------------------------------------------------------------------------
# destinations
# --------------------------------------------------------------------------
class Layout:
    def __init__(self, home: Path | None = None):
        self.home = home or Path(os.path.expanduser("~"))
        self.claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", self.home / ".claude"))
        self.settings = self.claude_dir / "settings.json"
        self.agents_dir = self.claude_dir / "agents"
        self.conf_dir = self.home / ".config" / "token_kit"
        self.bin_dir = self.conf_dir / "bin"
        self.manifest = self.conf_dir / "manifest.tsv"
        self.env_file = self.conf_dir / "env.sh"
        self.hook_shim = self.bin_dir / "token_kit_hook.sh"
        #: Generated agent files live here, not in the clone: they are produced
        #: from the chart at install time and belong to this machine.
        self.gen_agents_dir = self.conf_dir / "agents"


def load_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"install: {path} is not valid JSON ({exc}) -- refusing to touch it")


def write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tk.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


# --------------------------------------------------------------------------
# symlinking
# --------------------------------------------------------------------------
def link(rep: Report, man: Manifest, dest: Path, src: Path, label: str) -> None:
    if not src.exists():
        rep.fail(f"{label}: source missing: {src}")
        return
    if dest.is_symlink():
        if Path(os.readlink(dest)) == src:
            rep.ok(label)
            man.add("symlink", str(dest), str(src))
            return
        if rep.dry:
            rep.plan(f"re-point {dest} -> {src}")
            return
        dest.unlink()
        dest.symlink_to(src)
        rep.change(f"re-pointed {label}")
    elif dest.exists():
        rep.conflict(f"{label}: {dest} exists and is NOT a symlink -- left untouched")
        return
    else:
        if rep.dry:
            rep.plan(f"ln -s {src} {dest}")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(src)
        rep.change(f"linked {label}")
    man.add("symlink", str(dest), str(src))


def write_generated(rep: Report, man: Manifest, path: Path, body: str,
                    label: str, mode: int | None = None) -> None:
    if path.is_file() and path.read_text() == body:
        rep.ok(f"{label} current")
        man.add("file", str(path))
        return
    if rep.dry:
        rep.plan(f"write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    if mode is not None:
        path.chmod(mode)
    rep.change(f"wrote {path}")
    man.add("file", str(path))


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
def cmd_install(args) -> int:
    import shutil

    from token_kit import config as config_mod
    from token_kit.manifest import Manifest
    from token_kit.settings_merge import merge

    lay = Layout()
    rep = Report(args.dry_run)
    prof = config_mod.resolve()

    print("token_kit installer")
    print(f"  clone       {KIT_DIR}")
    print(f"  config      {prof.source_file}"
          f" ({'present' if Path(prof.source_file).is_file() else 'absent -- fine, everything is detected'})")
    print(f"  state dir   {prof.state_dir}")
    print(f"  codex home  {'node-local (home is on a network filesystem)' if prof.codex_node_local_home else 'the ordinary ~/.codex (home is local disk)'}")
    print(f"  claude dir  {lay.claude_dir}")
    print(f"  config dir  {lay.conf_dir}")
    print(f"  python      {sys.version.split()[0]}")
    if rep.dry:
        print("  MODE        DRY RUN -- nothing will be written")

    # -- dependencies -------------------------------------------------------
    print("\ndependencies")
    uv = shutil.which("uv") or os.environ.get("TOKEN_KIT_UV", "")
    claude = shutil.which("claude")
    if uv:
        rep.ok(f"uv      {uv}")
    else:
        rep.fail("uv      MISSING (required; install.sh installs it: "
                 "curl -LsSf https://astral.sh/uv/install.sh | sh)")
    if claude:
        rep.ok(f"claude  {claude}")
    else:
        rep.fail("claude  MISSING (required: npm i -g @anthropic-ai/claude-code)")
    # How codex is reached is the PROFILE's decision, so report what the kit's
    # own launcher resolves, not what happens to be on PATH.
    from token_kit.codex import launcher as codex_launcher

    ccfg = codex_launcher.load_config()
    codex = codex_launcher.resolve_binary(ccfg)
    named = os.path.expanduser(ccfg.launcher or "codex")
    if ccfg.node_local_home:
        where = (f"codex   {codex or 'MISSING'} "
                 f"(kit launcher; CODEX_HOME -> "
                 f"{codex_launcher.node_local_home(ccfg)})")
    else:
        where = f"codex   {codex or named}"
    if codex or Path(named).is_file():
        rep.ok(where)
    else:
        rep.note(f"codex   not found (optional; codex rows will not dispatch). "
                 f"launcher = {ccfg.launcher}, node_local_home = "
                 f"{str(ccfg.node_local_home).lower()}")
    if rep.failures:
        print("\ninstall: REFUSING -- required dependencies are missing (above)")
        return 1

    # -- components ---------------------------------------------------------
    print("\ncomponents")
    for name, what in COMPONENTS:
        rep.ok(f"{name:<22} {what}")
    print(f"  {len(COMPONENTS)} component(s)")

    man = Manifest(lay.manifest)

    # -- dirs + generated files --------------------------------------------
    print("\nconfig")
    for d in (lay.conf_dir, lay.bin_dir, lay.agents_dir, lay.gen_agents_dir):
        if d.is_dir():
            continue
        if rep.dry:
            rep.plan(f"mkdir -p {d}")
        else:
            d.mkdir(parents=True, exist_ok=True)
            rep.change(f"created {d}")

    env_body = env_file_body(prof)
    write_generated(rep, man, lay.env_file, env_body, "env.sh")

    shim_body = hook_shim_body(lay, uv or "uv", resolve_interpreter(uv))
    write_generated(rep, man, lay.hook_shim, shim_body, "hook shim", mode=0o755)

    # -- executables --------------------------------------------------------
    print(f"\nexecutables -> {lay.bin_dir}")
    for name, rel in EXECUTABLES:
        link(rep, man, lay.bin_dir / name, KIT_DIR / rel, name)
    print(f"  {len(EXECUTABLES)} executable(s)")

    # -- agents + chart -----------------------------------------------------
    # NOTHING checked in is linked as an agent. Every agent file this installer
    # puts on the machine is GENERATED from the chart, one per (row, distinct
    # claude candidate): the row's model, effort and header ride in the
    # frontmatter, so a spawn on one carries the row's economics with no
    # rewriting, and a row that goes away takes its file with it.
    print(f"\ngenerated row agents -> {lay.gen_agents_dir}")
    generated = generate_kind_agents(rep, lay.gen_agents_dir)
    for f in generated:
        if not rep.dry:
            man.add("file", str(f))
        link(rep, man, lay.agents_dir / f.name, f, f.name)
    print(f"  {len(generated)} generated agent file(s)")

    print("\nrouting chart")
    chart = matrix_path()
    link(rep, man, lay.conf_dir / chart.name, chart, chart.name)
    for problem in validate_matrix():
        rep.conflict(f"chart: {problem}")

    # -- settings -----------------------------------------------------------
    print(f"\nsettings  {lay.settings}")
    current = load_settings(lay.settings)
    new, mrep = merge(current, prof.settings, str(lay.hook_shim), prof.hook_matcher)

    # The other three events. The PreToolUse shim already dispatches on
    # hook_event_name, so SessionStart reuses it; the respawn reader is its own
    # executable because it must stay off the per-tool-call path.
    # A Stop entry takes NO matcher -- there is nothing to match on.
    reader = str(lay.bin_dir / "respawn-reader")
    extra: list[tuple[str, str | None, str, object]] = []
    if prof.session_start:
        new, srep = merge(new, {}, str(lay.hook_shim), "*", event="SessionStart")
        extra.append(("SessionStart", "*", str(lay.hook_shim), srep))
    new, prep = merge(new, {}, reader, "Agent", event="PostToolUse")
    extra.append(("PostToolUse", "Agent", reader, prep))
    new, strep = merge(new, {}, reader, None, event="Stop")
    extra.append(("Stop", None, reader, strep))

    for key in mrep.keys_same:
        rep.ok(f"key {key} already {json.dumps(current[key])}")
    for key, have, want in mrep.key_conflicts:
        rep.conflict(f"key {key} is {json.dumps(have)}, kit wants {json.dumps(want)} -- yours kept")
    if mrep.hook_present:
        rep.ok(f"hook already registered (matcher {prof.hook_matcher!r})")
    for event, _matcher, cmd, r in extra:
        if r.hook_present:
            rep.ok(f"{event} hook already registered -> {cmd}")

    anything = mrep.changed or any(r.hook_added for _e, _m, _c, r in extra)
    if not anything:
        rep.ok("settings.json needs no change")
    elif rep.dry:
        if mrep.keys_added:
            rep.plan(f"add keys: {', '.join(mrep.keys_added)}")
        if mrep.hook_added:
            rep.plan(f"register PreToolUse hook (matcher {prof.hook_matcher!r}) -> {lay.hook_shim}")
        for event, matcher, cmd, r in extra:
            if r.hook_added:
                rep.plan(f"register {event} hook "
                         f"({'no matcher' if matcher is None else 'matcher ' + repr(matcher)})"
                         f" -> {cmd}")
        rep.plan(f"back up {lay.settings} first")
    else:
        if lay.settings.is_file():
            from datetime import datetime
            bk = lay.settings.with_name(
                lay.settings.name + ".token_kit-backup-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
            shutil.copy2(lay.settings, bk)
            rep.change(f"backed up -> {bk}")
            man.add("backup", str(bk), str(lay.settings))
        write_settings(lay.settings, new)
        for key in mrep.keys_added:
            man.add("setting", key, "ADDED")
            rep.change(f"added key {key}")
        if mrep.hook_added:
            man.add("hook", "PreToolUse", prof.hook_matcher, str(lay.hook_shim))
            rep.change(f"registered PreToolUse hook -> {lay.hook_shim}")
        for event, matcher, cmd, r in extra:
            if r.hook_added:
                man.add("hook", event, matcher or "", cmd)
                rep.change(f"registered {event} hook -> {cmd}")

    # -- verdict ------------------------------------------------------------
    print()
    if rep.dry:
        print(f"DRY RUN complete: components={len(COMPONENTS)} "
              f"conflicts={rep.conflicts} failures={rep.failures}")
        print("dry-run = PASS" if not rep.failures else "dry-run = FAIL")
        return 0 if not rep.failures else 1

    man.save()
    if rep.changes == 0:
        print("NO CHANGES -- already installed (idempotent re-run)")
    else:
        print(f"installed: {rep.changes} change(s)")
    if rep.conflicts:
        print(f"conflicts: {rep.conflicts} (listed above; nothing of yours was overwritten)")

    print("\nprobe")
    prc = probe(lay, live=args.live)
    print()
    if rep.failures == 0 and prc == 0:
        print("install = PASS")
        print(f"  uninstall: bash {KIT_DIR / 'install.sh'} --uninstall")
        return 0
    print(f"install = FAIL   failures={rep.failures} probe_rc={prc}")
    return 1


def generate_kind_agents(rep: Report, gen_dir: Path) -> list[Path]:
    """Write row-*.md into `gen_dir` from the routing chart.

    In a dry run it writes into a throwaway directory instead, so the count is
    real (the generator runs) while the clone is untouched.
    """
    try:
        from token_kit.router import gen_agents
        from token_kit.router import matrix as matrix_mod
    except ImportError as exc:
        rep.fail(f"row agents: router package not importable ({exc})")
        return []
    try:
        m = matrix_mod.load()
    except Exception as exc:  # noqa: BLE001 -- a bad table is a install failure
        rep.fail(f"row agents: {exc}")
        return []
    if rep.dry:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            made = gen_agents.generate(m, Path(tmp))
            rep.plan(f"generate {len(made)} row agent file(s) into {gen_dir}")
        return []
    made = gen_agents.generate(m, gen_dir)
    # A row that has gone away must not leave its agent file behind.
    keep = {p.name for p in made}
    for stale in gen_dir.glob("*.md"):
        if stale.name not in keep:
            stale.unlink()
            rep.change(f"removed stale row agent {stale.name}")
    return made


def validate_matrix() -> list[str]:
    """Every way the installed chart is internally wrong, as one line each."""
    try:
        from token_kit.router import matrix as matrix_mod
        return matrix_mod.validate(matrix_mod.load(matrix_path()))
    except Exception as exc:  # noqa: BLE001
        return [str(exc)]


def env_file_body(prof) -> str:
    """What the shims source. Every value is DETECTED or defaulted; to change
    one, write ~/.config/token_kit/config.toml and re-run install.sh."""
    lines = [
        "# generated by token_kit install -- see env_file_body() in cli.py.",
        "# Values are detected at install time; override them in",
        f"# {prof.source_file} and re-run install.sh.",
        f"export TOKEN_KIT_CLONE={KIT_DIR}",
    ]
    for k, v in prof.as_env().items():
        lines.append(f"export {k}={v}")
    lines.append(f"export TK_MATRIX=${{TK_MATRIX:-{matrix_path()}}}")
    return "\n".join(lines) + "\n"


def resolve_interpreter(uv: str) -> str:
    """The interpreter path to burn into the hook, or "" if uv cannot say.

    MEASURED on the cluster login node: going through `uv run` costs ~130-150 ms
    per hook invocation, while the interpreter uv would have chosen, called
    directly, costs ~40 ms. The hook fires on EVERY tool call, so the resolved
    path is worth writing down. The shim keeps `uv run` as its fallback, so a
    moved or garbage-collected interpreter degrades in speed, not in function.
    """
    if not uv:
        return ""
    import subprocess
    try:
        out = subprocess.run([uv, "python", "find", ">=3.11"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    path = out.stdout.strip()
    return path if out.returncode == 0 and Path(path).is_file() else ""


def hook_shim_body(lay: Layout, uv: str, interpreter: str = "") -> str:
    """The command Claude Code runs on every PreToolUse event.

    It stays as small as it can be: source the machine's env file, then exec
    the CLI IN THE CLONE -- so a `git pull` changes behaviour with no
    re-install -- through the interpreter resolved at install time.
    """
    fast = ""
    if interpreter:
        fast = (f'PY="{interpreter}"\n'
                'if [ -x "$PY" ]; then exec "$PY" ' f'"{CLI_PATH}" hook "$@"; fi\n'
                "# interpreter gone (uv gc, reinstall): fall back to uv, which re-resolves.\n")
    return (
        "#!/usr/bin/env bash\n"
        "# generated by token_kit install -- see hook_shim_body() in cli.py.\n"
        f'[ -r "{lay.env_file}" ] && . "{lay.env_file}"\n'
        + fast +
        f'exec "{uv}" run --python \'>=3.11\' --no-project "{CLI_PATH}" hook "$@"\n'
    )


# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------
def cmd_uninstall(args) -> int:
    from token_kit.manifest import Manifest
    from token_kit.settings_merge import unmerge

    lay = Layout()
    rep = Report(args.dry_run)
    man = Manifest(lay.manifest)
    print(f"uninstall (manifest: {lay.manifest})")
    if not man.rows:
        print("  nothing to do: no manifest")
        return 0

    for row in man.of_kind("symlink"):
        dest, src = Path(row[1]), row[2]
        if dest.is_symlink():
            if os.readlink(dest) == src:
                if rep.dry:
                    rep.plan(f"rm {dest}")
                else:
                    dest.unlink()
                    rep.change(f"removed symlink {dest}")
            else:
                rep.conflict(f"{dest} points elsewhere now -- left alone")
        elif dest.exists():
            rep.conflict(f"{dest} is no longer a symlink -- left alone")

    for row in man.of_kind("file"):
        p = Path(row[1])
        if p.is_file():
            if rep.dry:
                rep.plan(f"rm {p}")
            else:
                p.unlink()
                rep.change(f"removed {p}")

    if lay.settings.is_file():
        current = load_settings(lay.settings)
        new = current
        keys = man.added_setting_keys()
        # One unmerge PER EVENT: there are four hook rows now, and asking the
        # manifest for "the" hook command would leave three dangling.
        events = man.hook_rows() or [("PreToolUse", "", str(lay.hook_shim))]
        for event, _matcher, cmd in events:
            new = unmerge(new, keys, cmd, event=event)
            keys = []
        if new == current:
            rep.ok("settings.json already carries nothing of ours")
        elif rep.dry:
            rep.plan(f"rewrite {lay.settings} (drop hooks "
                     f"{sorted({e for e, _m, _c in events})} + keys "
                     f"{man.added_setting_keys()})")
        else:
            write_settings(lay.settings, new)
            rep.change(f"settings.json: removed our hooks "
                       f"{sorted({e for e, _m, _c in events})} and keys "
                       f"{man.added_setting_keys()}")

    if not rep.dry:
        man.delete()
        for d in (lay.bin_dir, lay.conf_dir):
            try:
                d.rmdir()
            except OSError:
                pass
    print(f"\nuninstall: changes={rep.changes} conflicts={rep.conflicts} failures={rep.failures}")
    return 1 if rep.failures else 0


# --------------------------------------------------------------------------
# probe -- "did it take?"
# --------------------------------------------------------------------------
def cmd_probe(args) -> int:
    return probe(Layout(), live=args.live)


def live_canary() -> tuple[str, str]:
    """ONE real model call: is a planted instruction file actually in context?

    This is the only thing in the kit that spends money, so it is opt-in
    (`--live`) and it runs exactly one probe on the cheapest tier.
    """
    import tempfile

    from token_kit import canary

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        line = canary.emit("probe")
        token = line.split("token=")[1].split()[0]
        (root / "CLAUDE.md").write_text(
            "Scratch instruction file for a one-shot context probe.\n" + line + "\n")
        manifest = root / "slots.tsv"
        manifest.write_text(f"probe\ttoken\t{root / 'CLAUDE.md'}\t{token}\t\n")
        rows, broken = canary.probe(manifest, str(root), model="haiku",
                                    evidence=root / "evidence")
    row = rows[0]
    return row.status, f"{row.status}  {row.detail}  (broken={broken or 'no'})"


def probe(lay: Layout, live: bool = False) -> int:
    import subprocess
    import time

    from token_kit.manifest import Manifest

    npass = nfail = 0

    def check(cond, msg):
        nonlocal npass, nfail
        if cond:
            npass += 1
            print(f"  PASS  {msg}")
        else:
            nfail += 1
            print(f"  FAIL  {msg}")

    try:
        settings = load_settings(lay.settings)
        check(True, "settings.json parses")
    except SystemExit as exc:
        settings = {}
        check(False, str(exc))

    ttl = settings.get("subagentPromptCacheTtl")
    check(ttl is not None,
          f"subagentPromptCacheTtl = {ttl}" if ttl else
          "subagentPromptCacheTtl UNSET (subagent cache writes stay 5m)")

    man = Manifest(lay.manifest)
    hookcmd = man.hook_command("PreToolUse") or str(lay.hook_shim)
    registered = [h for e in settings.get("hooks", {}).get("PreToolUse", [])
                  for h in e.get("hooks", []) if h.get("command") == hookcmd]
    check(len(registered) == 1, f"PreToolUse hook registered exactly once ({len(registered)})")
    check(os.access(hookcmd, os.X_OK), f"hook is executable: {hookcmd}")

    # every OTHER event the manifest says we registered, and its executable
    for event, _matcher, cmd in man.hook_rows():
        if event == "PreToolUse":
            continue
        n = len([h for e in settings.get("hooks", {}).get(event, [])
                 for h in e.get("hooks", []) if h.get("command") == cmd])
        check(n == 1, f"{event} hook registered exactly once ({n}) -> {cmd}")
        check(os.access(cmd, os.X_OK), f"{event} hook is executable: {cmd}")

    for name, _rel in EXECUTABLES:
        p = lay.bin_dir / name
        check(p.is_symlink() and os.access(p, os.X_OK),
              f"executable linked and runnable: {p}")

    if os.access(hookcmd, os.X_OK):
        t0 = time.monotonic()
        proc = subprocess.run([hookcmd], input=b'{"tool_name":"Read","session_id":"probe"}',
                              capture_output=True)
        dt = (time.monotonic() - t0) * 1000
        check(proc.returncode == 0,
              f"hook exits 0 on a main-thread call, in {dt:.0f} ms "
              f"(stderr: {proc.stderr.decode()[:120]!r})")

    check(lay.env_file.is_file(), f"env.sh present ({lay.env_file})")

    # EVERY agent file we install is generated into lay.gen_agents_dir; nothing
    # checked in is linked. The generator names its own files, so the test is
    # WHERE a link points, never what it is called.
    agents = [p for p in lay.agents_dir.glob("*.md") if p.is_symlink()
              and Path(os.readlink(p)).parent == lay.gen_agents_dir]
    check(bool(agents), f"{len(agents)} generated row agent(s) linked in {lay.agents_dir}")
    unresolved = [p.name for p in agents if not p.resolve().is_file()]
    check(not unresolved, f"every generated agent link resolves ({unresolved or 'all good'})")
    no_name = [p.name for p in agents
               if not any(l.startswith("name:") for l in p.read_text().splitlines()[:8])]
    check(not no_name, f"every generated agent carries a name: field ({no_name or 'all good'})")
    adopted = [p.name for p in lay.agents_dir.glob("*.md")
               if p.is_symlink() and str(KIT_DIR) in os.readlink(p)]
    check(not adopted,
          f"no checked-in agent file is linked ({adopted or 'none, as intended'})")

    chart = lay.conf_dir / matrix_path().name
    check(chart.is_file(), f"routing chart linked: {chart}")
    problems = validate_matrix()
    check(not problems, f"chart validates ({problems[:3] or 'no problems'})")

    violations = census_modules()
    check(not violations,
          f"installed modules carry no machine fact and no project word "
          f"({len(violations)} violation(s))")

    if live:
        status, detail = live_canary()
        for line in detail.splitlines():
            print(f"        {line}")
        check(status == "LOADED",
              f"live canary: an instruction file planted in a scratch directory "
              f"came back {status}")

    print(f"  probe: {npass} pass, {nfail} fail")
    return 0 if nfail == 0 else 1


# --------------------------------------------------------------------------
# census -- the sanitisation and portability worklist
# --------------------------------------------------------------------------
#: The detector lives in a data file beside this one, so that the census can
#: scan cli.py itself without matching its own patterns. See the file's header.
CENSUS_PATTERNS_FILE = Path(__file__).resolve().parent / "census_patterns.toml"


def census_patterns() -> list[tuple[str, str]]:
    """The rows of the data file, plus one built here.

    The name of whoever is running is a machine fact like any other, but it
    differs per machine, so it is read from the environment rather than written
    into the detector. A name too short to be distinctive is skipped: it would
    match ordinary words and make the census cry wolf.
    """
    import tomllib
    with CENSUS_PATTERNS_FILE.open("rb") as fh:
        table = tomllib.load(fh)
    rows = [(str(p["kind"]), str(p["pattern"])) for p in table.get("pattern", [])]
    import getpass
    import re as _re
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 -- no passwd entry is not a census failure
        user = ""
    if len(user) >= 4 and user.isalnum():
        rows.append(("username", _re.escape(user)))
    return rows


#: Anything that looks like an auth file never enters the repo.
SECRET_NAMES = ("auth.json", "credentials", ".env", "id_rsa", ".netrc")
SECRET_SUFFIXES = (".pem", ".key")

#: Directories the kit-wide word scan does not own. The router's chart, its
#: cases and its agent files are another lane's to keep clean.
CENSUS_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "agents"}
CENSUS_SKIP_RELATIVE = ("tests/router", "src/token_kit/router")
CENSUS_SKIP_SUFFIXES = (".pyc", ".png", ".jpg", ".gz", ".zip")


def module_files() -> list[Path]:
    """Everything that is INSTALLED and therefore runs on someone's machine.

    Their target is ZERO: a machine path, a user name, a host name or a project
    word in one of them is a defect, because a machine fact is DETECTED at run
    time and a project name has no business in a kit that runs from anywhere.
    """
    out = [p for p in sorted((KIT_DIR / "src" / "token_kit").rglob("*.py"))
           if "__pycache__" not in p.parts]
    for sub in ("respawn", "supervisor", "codex"):
        out += sorted((KIT_DIR / "src" / "token_kit" / sub / "bin").glob("*"))
    out.append(KIT_DIR / "install.sh")
    return [p for p in out if p.is_file() and p != CENSUS_PATTERNS_FILE
            and not _skipped(p)]


def _skipped(path: Path) -> bool:
    """Is this file another lane's to keep clean? The router's package, chart,
    cases and agent files are guarded by the lane that owns them."""
    try:
        rel = str(path.relative_to(KIT_DIR))
    except ValueError:
        return False
    return any(rel.startswith(skip) for skip in CENSUS_SKIP_RELATIVE)


def kit_files() -> list[Path]:
    """Every file in the kit the word scan owns: all of it but the router's
    chart, cases and agent files, and but the detector itself."""
    out = []
    chart_names = set(MATRIX_NAMES)
    for path in sorted(KIT_DIR.rglob("*")):
        if not path.is_file() or path == CENSUS_PATTERNS_FILE:
            continue
        rel = path.relative_to(KIT_DIR)
        if set(rel.parts) & CENSUS_SKIP_DIRS or path.suffix in CENSUS_SKIP_SUFFIXES:
            continue
        if _skipped(path):
            continue
        if str(rel) in chart_names:
            continue
        out.append(path)
    return out


def scan(files, patterns) -> list[tuple[str, str, int, str]]:
    """(kind, relative path, line number, line) for every match."""
    import re
    compiled = [(kind, re.compile(pat)) for kind, pat in patterns]
    rows = []
    for f in files:
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        try:
            rel = str(f.relative_to(KIT_DIR))
        except ValueError:
            rel = str(f)
        for lineno, line in enumerate(text.splitlines(), 1):
            for kind, rx in compiled:
                if rx.search(line):
                    rows.append((kind, rel, lineno, line.replace("\t", " ").strip()[:200]))
    return rows


def census_modules() -> list[tuple[str, str, int, str]]:
    """The live-path violations. The whole point is that this stays empty."""
    return scan(module_files(), census_patterns())


def census_project_words() -> list[tuple[str, str, int, str]]:
    """A project name anywhere in the kit. Also zero: the kit runs from
    anywhere, so it may not name the place it grew up in."""
    words = [(k, p) for k, p in census_patterns() if k == "project_word"]
    return scan(kit_files(), words)


def cmd_census(args) -> int:
    violations = census_modules()
    print(f"census: machine facts in installed modules = {len(violations)}"
          f"  (target 0)")
    for kind, rel, lineno, text in violations:
        print(f"  VIOLATION {kind:<14} {rel}:{lineno}  {text[:100]}")

    words = census_project_words()
    print(f"census: project words anywhere in the kit = {len(words)}  (target 0)")
    for kind, rel, lineno, text in words:
        print(f"  PROJECT   {kind:<14} {rel}:{lineno}  {text[:100]}")

    secrets = [p for p in kit_files()
               if p.name in SECRET_NAMES or p.suffix in SECRET_SUFFIXES]
    print(f"census: secret-shaped files = {len(secrets)}  (target 0)")
    for p in secrets:
        print(f"  SECRET    {p.relative_to(KIT_DIR)}")

    print(f"census: scanned {len(module_files())} installed module(s), "
          f"{len(kit_files())} file(s) for project words")
    return 1 if (violations or words or secrets) else 0


def cmd_config(args) -> int:
    """Print what this machine resolved, and where each value came from."""
    from token_kit import config as config_mod
    print(config_mod.describe())
    return 0


def cmd_prompts(args) -> int:
    """What the user typed, verbatim, out of the CLI's own transcripts.

    The work is all in token_kit.prompts; this is the same argument surface as
    the `token-kit-prompts` shim, so either spelling does the same thing.
    """
    from token_kit import prompts as prompts_mod

    argv = ["--cwd", args.cwd, "--out", args.out]
    if args.since:
        argv += ["--since", args.since]
    if args.session:
        argv += ["--session", args.session]
    if args.stdout:
        argv += ["--stdout"]
    return prompts_mod.main(argv)


# --------------------------------------------------------------------------
# hook -- the PreToolUse entry point
# --------------------------------------------------------------------------
def cmd_task(args) -> int:
    """Titled working folders. Same argument surface as the `token-kit-task`
    shim, so either spelling does the same thing."""
    from token_kit import task as task_mod

    return task_mod.main(args.rest)


def cmd_workflow(args) -> int:
    from token_kit import workflow

    return workflow.main(args.rest)


# --------------------------------------------------------------------------
# hook -- the PreToolUse entry point
# --------------------------------------------------------------------------
def cmd_hook(args) -> int:
    """Read the hook event on stdin and hand it to the router.

    The router (call cap + matrix routing) is another lane's package. Until it
    lands, this exits 0 with no output, which Claude Code reads as "the hook has
    no opinion". It is deliberately not a stub that pretends to enforce a
    budget: a cap that does not count is worse than no cap.
    """
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except (json.JSONDecodeError, ValueError):
        return 0  # fail OPEN: a hook that wedges a session is the worse defect

    try:
        from token_kit.router import handle  # type: ignore
    except ImportError:
        return 0
    return handle(payload)


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse

    from token_kit import prompts as prompts_mod

    ap = argparse.ArgumentParser(prog="token_kit", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="install the kit on this machine")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--live", action="store_true", help="probe with the live canary (costs cents)")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("uninstall", help="remove exactly what install added")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("probe", help="check that the install took")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("census", help="machine facts and project words: both must be zero")
    p.set_defaults(fn=cmd_census)

    p = sub.add_parser("config", help="print the resolved machine facts and their origin")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("task", help="create, retitle, find and resume task folders")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("workflow", help="portable agent checkpoints and fresh-session launches")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_workflow)

    p = sub.add_parser("hook", help="PreToolUse hook entry point (reads stdin)")
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser("prompts", help="write what the user typed, verbatim, to one file")
    p.add_argument("--cwd", default=".")
    p.add_argument("--out", default=prompts_mod.DEFAULT_OUT)
    p.add_argument("--since", default="", metavar="YYYY-MM-DD")
    p.add_argument("--session", default="", metavar="ID")
    p.add_argument("--stdout", action="store_true")
    p.set_defaults(fn=cmd_prompts)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    # The hook fires on every single tool call, so it skips argparse entirely.
    if sys.argv[1:] == ["hook"]:
        sys.exit(cmd_hook(None))
    sys.exit(main())
