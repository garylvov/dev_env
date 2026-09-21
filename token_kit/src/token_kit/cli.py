#!/usr/bin/env python3
"""token_kit -- put the whole token-spend kit on a machine with one command.

Subcommands: install, uninstall, probe, census, hook.

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

# The units this installer puts on a machine. reference_bash/ is NOT one of
# them: those scripts are the behaviour spec the Python ports must match, kept
# for reading and for the census, never installed.
COMPONENTS = [
    ("profile-env", "machine facts resolved into ~/.config/token_kit/env.sh"),
    ("settings-keys", "the measured settings.json keys"),
    ("pretooluse-hook", "the PreToolUse hook (call cap + matrix routing)"),
    ("sessionstart-menu", "the SessionStart row menu, generated from the table"),
    ("respawn-reader", "the PostToolUse(Agent)+Stop lane respawn reader"),
    ("supervisor", "token-kit-supervise, the rollover supervisor"),
    ("codex-bin", "the codex-dispatch / codex-job / codex-run shims"),
    ("agents", "the compressed personas and the codex wrapper agents"),
    ("row-agents", "one generated agent file per matrix row candidate"),
    ("agent-trigger-matrix", "agent_trigger_matrix.toml, the routing table"),
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
]

#: Where an adopted (pre-existing, real) agent file is moved to. It is MOVED,
#: never deleted, and the uninstall puts it back.
ADOPT_DIR = ".pre_token_kit"


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
def link(rep: Report, man: Manifest, dest: Path, src: Path, label: str,
         adopt: bool = False) -> None:
    if not src.exists():
        rep.fail(f"{label}: source missing: {src}")
        return
    if adopt and dest.exists() and not dest.is_symlink():
        # --adopt-personas: the operator's own file is MOVED aside, never
        # deleted, and the manifest remembers where so uninstall restores it.
        aside = dest.parent / ADOPT_DIR / dest.name
        if rep.dry:
            rep.plan(f"adopt {label}: mv {dest} -> {aside}, then link")
            return
        aside.parent.mkdir(parents=True, exist_ok=True)
        if aside.exists():
            rep.conflict(f"{label}: {aside} already exists -- {dest} left untouched")
            return
        dest.rename(aside)
        man.add("adopted", str(aside), str(dest))
        rep.change(f"adopted {label}: your file is at {aside}")
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

    from token_kit import profiles as profiles_mod
    from token_kit.manifest import Manifest
    from token_kit.settings_merge import merge

    lay = Layout()
    rep = Report(args.dry_run)
    prof = profiles_mod.load(KIT_DIR / "profiles", args.profile)
    detected = "--profile" if args.profile else "autodetected"

    print("token_kit installer")
    print(f"  clone       {KIT_DIR}")
    print(f"  profile     {prof.name} ({detected})  {prof.source}")
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

    ccfg = codex_launcher.load_config(KIT_DIR / "profiles", prof.name)
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
        rep.note(f"codex   not found (optional; codex-* agents will not dispatch). "
                 f"profile launcher = {ccfg.launcher}, node_local_home = "
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
    for d in (lay.conf_dir, lay.bin_dir, lay.agents_dir):
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

    # -- agents + matrix ----------------------------------------------------
    print(f"\nagents -> {lay.agents_dir}")
    agent_files = sorted((KIT_DIR / "agents").glob("*.md"))
    if not agent_files:
        rep.fail(f"no agent files in {KIT_DIR / 'agents'} "
                 f"-- run `census --import` first")
    for f in agent_files:
        link(rep, man, lay.agents_dir / f.name, f, f.name, adopt=args.adopt_personas)
    print(f"  {len(agent_files)} agent file(s)")

    # One generated agent file per (matrix row, distinct claude candidate).
    # The row's model, effort and header are frontmatter, so a spawn on one of
    # these carries the row's economics without the router rewriting anything.
    print("\ngenerated row agents")
    gen_dir = KIT_DIR / "agents" / "generated"
    generated = generate_row_agents(rep, gen_dir)
    for f in generated:
        if not rep.dry:
            man.add("file", str(f))
        link(rep, man, lay.agents_dir / f.name, f, f.name, adopt=args.adopt_personas)
    print(f"  {len(generated)} generated agent file(s)")

    print("\nrouting table")
    link(rep, man, lay.conf_dir / "agent_trigger_matrix.toml",
         KIT_DIR / "agent_trigger_matrix.toml", "agent_trigger_matrix.toml")
    for problem in validate_matrix():
        rep.conflict(f"matrix: {problem}")

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


def generate_row_agents(rep: Report, gen_dir: Path) -> list[Path]:
    """Write <kit>/agents/generated/row-*.md from the routing table.

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
    for stale in gen_dir.glob("row-*.md"):
        if stale.name not in keep:
            stale.unlink()
            rep.change(f"removed stale row agent {stale.name}")
    return made


def validate_matrix() -> list[str]:
    """Every way the installed table is internally wrong, as one line each."""
    try:
        from token_kit.router import matrix as matrix_mod
        return matrix_mod.validate(matrix_mod.load(KIT_DIR / "agent_trigger_matrix.toml"))
    except Exception as exc:  # noqa: BLE001
        return [str(exc)]


def env_file_body(prof) -> str:
    lines = [
        f"# generated by token_kit -- profile {prof.name}. Do not edit by hand;",
        f"# edit {prof.source.name} in the clone's profiles/ and re-run install.sh.",
        f"export TOKEN_KIT_CLONE={KIT_DIR}",
    ]
    for k, v in prof.as_env().items():
        lines.append(f"export {k}={v}")
    lines += [
        "# consumed by the kit's own tools:",
        f"export CAMPAIGN_DIR=${{CAMPAIGN_DIR:-{prof.campaign_dir}}}",
        f"export LANE_RECYCLER_DATA_ROOT=${{LANE_RECYCLER_DATA_ROOT:-{prof.data_root}}}",
        f"export LANE_RECYCLER_MATRIX=${{LANE_RECYCLER_MATRIX:-{KIT_DIR / 'agent_trigger_matrix.toml'}}}",
        f"export CODEX_LAUNCHER=${{CODEX_LAUNCHER:-{prof.codex_launcher}}}",
    ]
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

    # An adopted file goes back exactly where it was, before anything else
    # could claim the name. The symlink above is already gone by now.
    for row in man.of_kind("adopted"):
        aside, dest = Path(row[1]), Path(row[2])
        if not aside.is_file():
            rep.conflict(f"{aside} is gone -- cannot restore {dest}")
            continue
        if dest.exists():
            rep.conflict(f"{dest} exists again -- your file stays at {aside}")
            continue
        if rep.dry:
            rep.plan(f"restore {aside} -> {dest}")
        else:
            aside.rename(dest)
            rep.change(f"restored your own {dest.name}")
            try:
                aside.parent.rmdir()
            except OSError:
                pass

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

    agents = [p for p in lay.agents_dir.glob("*.md")
              if p.is_symlink() and str(KIT_DIR) in os.readlink(p)]
    check(bool(agents), f"{len(agents)} kit agent file(s) resolvable in {lay.agents_dir}")
    unresolved = [p.name for p in agents if not p.resolve().is_file()]
    check(not unresolved, f"every kit agent link resolves ({unresolved or 'all good'})")
    no_name = [p.name for p in agents
               if not any(l.startswith("name:") for l in p.read_text().splitlines()[:8])]
    check(not no_name, f"every kit agent carries a name: field ({no_name or 'all good'})")

    matrix = lay.conf_dir / "agent_trigger_matrix.toml"
    if not matrix.exists():
        matrix = KIT_DIR / "agent_trigger_matrix.toml"
    try:
        import tomllib
        with matrix.open("rb") as fh:
            table = tomllib.load(fh)
        rows = table.get("row", [])
        check(bool(rows), f"matrix parses as TOML: {len(rows)} [[row]] block(s)")
        unnamed = [i for i, r in enumerate(rows) if not (r.get("id") or r.get("name"))]
        check(not unnamed, f"every row carries an id/name ({unnamed or 'all good'})")
    except Exception as exc:  # noqa: BLE001 -- any parse failure is a probe failure
        check(False, f"matrix does not parse: {matrix}: {exc}")

    problems = validate_matrix()
    check(not problems, f"matrix validates ({problems[:3] or 'no problems'})")

    gen = [p for p in lay.agents_dir.glob("row-*.md") if p.is_symlink()]
    check(bool(gen), f"{len(gen)} generated row agent(s) linked in {lay.agents_dir}")

    violations = census_modules()
    check(not violations,
          f"installed modules carry no live cluster path/user/host "
          f"({len(violations)} violation(s))")

    if live:
        canary = KIT_DIR / "reference_bash" / "instruction_canary" / "canary"
        if canary.is_file():
            proc = subprocess.run(["bash", str(canary), "selftest"], capture_output=True)
            for line in proc.stdout.decode().splitlines()[-3:]:
                print(f"        {line}")
            check(proc.returncode == 0, "live canary ran")
        else:
            check(False, "canary not present")

    print(f"  probe: {npass} pass, {nfail} fail")
    return 0 if nfail == 0 else 1


# --------------------------------------------------------------------------
# census -- the sanitisation and portability worklist
# --------------------------------------------------------------------------
#: The detector lives in a data file beside this one, so that the census can
#: scan cli.py itself without matching its own patterns. See the file's header.
CENSUS_PATTERNS_FILE = Path(__file__).resolve().parent / "census_patterns.toml"


def census_patterns() -> list[tuple[str, str]]:
    import tomllib
    with CENSUS_PATTERNS_FILE.open("rb") as fh:
        table = tomllib.load(fh)
    return [(str(p["kind"]), str(p["pattern"])) for p in table.get("pattern", [])]

# Anything that looks like an auth file never enters the repo. This is a
# refusal, not a warning.
SECRET_NAMES = ("auth.json", "credentials", ".env", "id_rsa", ".netrc")
SECRET_SUFFIXES = (".pem", ".key")

# Not secrets, but PERSONAL: files that describe one operator's own machine and
# have no business in a public repo. They are skipped at import, not censused.
PERSONAL_GLOBS = ("manifest.*.tsv",)

# The six tools whose behaviour the Python ports must match. agent_memory is
# deliberately absent: it was stopped incomplete and its guard never ran.
REFERENCE_TOOLS = ["token_supervisor", "lane_recycler", "codex_native",
                   "codex_surface", "instruction_canary", "matrix_final"]
AGENT_SOURCES = ["matrix_final/agents", "codex_native/agents"]


def module_files() -> list[Path]:
    """Everything that is INSTALLED and therefore runs on someone's machine.

    The frozen bash spec under reference_bash/ is not here: it is read, never
    run, and its rows are the portability worklist. These files are the live
    path, and their target is ZERO -- a cluster path, a username or a hostname
    in one of them is a defect, because it belongs in a profile TOML that
    `token_kit.profiles` reads.
    """
    out = [p for p in sorted((KIT_DIR / "src" / "token_kit").rglob("*.py"))
           if "__pycache__" not in p.parts]
    for sub in ("respawn", "supervisor", "codex"):
        out += sorted((KIT_DIR / "src" / "token_kit" / sub / "bin").glob("*"))
    out.append(KIT_DIR / "install.sh")
    return [p for p in out if p.is_file() and p != CENSUS_PATTERNS_FILE]


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


def cmd_census(args) -> int:
    import shutil

    from token_kit import profiles as profiles_mod

    ref_dir = KIT_DIR / "reference_bash"
    if args.do_import:
        # No literal path here: where the frozen bash spec is copied FROM is a
        # profile key (`[paths] reference_source`), like every other machine
        # fact in this kit.
        source = args.source or str(
            profiles_mod.load(KIT_DIR / "profiles", args.profile).reference_source)
        src = Path(os.path.expanduser(source))
        if not (src / "tools").is_dir():
            print(f"census: no {src}/tools", file=sys.stderr)
            return 1
        ref_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for tool in REFERENCE_TOOLS:
            s = src / "tools" / tool
            if not s.is_dir():
                print(f"  MISSING tools/{tool} (skipped)")
                continue
            d = ref_dir / tool
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d, ignore=shutil.ignore_patterns(*PERSONAL_GLOBS))
            n += 1
        matrix = src / "agent_trigger_matrix.toml"
        if matrix.is_file():
            shutil.copy2(matrix, KIT_DIR / "agent_trigger_matrix.toml")
            n += 1
        # agent prompt files are CONTENT, not bash: they are installed, so they
        # live outside reference_bash/.
        agents_dir = KIT_DIR / "agents"
        agents_dir.mkdir(exist_ok=True)
        nag = 0
        for rel in AGENT_SOURCES:
            for f in sorted((src / "tools" / rel).glob("*.md")):
                shutil.copy2(f, agents_dir / f.name)
                nag += 1
        print(f"census: imported {n} component(s) into {ref_dir}, {nag} agent file(s)")

        refused = 0
        for p in ref_dir.rglob("*"):
            if p.is_file() and (p.name in SECRET_NAMES or p.suffix in SECRET_SUFFIXES):
                print(f"census: REFUSED secret-shaped file: {p}")
                p.unlink()
                refused += 1
        print(f"census: secret-shaped files refused: {refused}")
        if refused:
            return 1

    patterns = census_patterns()
    # The agent prompt files under reference_bash/*/agents are the same bytes
    # as the installed ones in agents/; censusing both would double every row.
    targets = sorted(p for p in ref_dir.rglob("*")
                     if p.is_file() and p.parent.name != "agents")
    matrix = KIT_DIR / "agent_trigger_matrix.toml"
    if matrix.is_file():
        targets.append(matrix)
    targets += sorted((KIT_DIR / "agents").glob("*.md"))

    rows = scan(targets, patterns)

    # THE NUMBER THAT MUST BE ZERO: the same patterns over the modules that
    # actually get installed and run. reference_bash rows above are a
    # worklist; these are a defect.
    violations = census_modules()
    print(f"census: live-path violations in installed modules = {len(violations)}"
          f"  (target 0)")
    for kind, rel, lineno, text in violations:
        print(f"  VIOLATION {kind:<12} {rel}:{lineno}  {text[:100]}")

    out = KIT_DIR / "HARDCODED.tsv"
    with out.open("w") as fh:
        fh.write("kind\tfile\tline_no\tline_text\n")
        for r in rows:
            fh.write("\t".join(str(x) for x in r) + "\n")

    by_kind, by_file = {}, {}
    for kind, rel, _, _ in rows:
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_file[rel] = by_file.get(rel, 0) + 1
    print(f"census: {len(rows)} row(s) -> {out}")
    print("\ncounts by kind:")
    for k, c in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<12} {c:5d}")
    print("\nfiles with the most rows:")
    for k, c in sorted(by_file.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {k:<50} {c:5d}")
    return 1 if violations else 0


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
    ap = argparse.ArgumentParser(prog="token_kit", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="install the kit on this machine")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--profile", default=None)
    p.add_argument("--live", action="store_true", help="probe with the live canary (costs cents)")
    p.add_argument("--adopt-personas", action="store_true",
                   help="move a pre-existing real agent file aside into "
                        f"~/.claude/agents/{ADOPT_DIR}/ and link the kit's one "
                        "(nothing is ever deleted; uninstall puts it back)")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("uninstall", help="remove exactly what install added")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--profile", default=None)
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("probe", help="check that the install took")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("census", help="the sanitisation / portability worklist")
    p.add_argument("--import", dest="do_import", action="store_true",
                   help="re-copy the reference bash tools first")
    p.add_argument("--source", default=None,
                   help="where to import from (default: the profile's reference_source)")
    p.add_argument("--profile", default=None)
    p.set_defaults(fn=cmd_census)

    p = sub.add_parser("hook", help="PreToolUse hook entry point (reads stdin)")
    p.set_defaults(fn=cmd_hook)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    # The hook fires on every single tool call, so it skips argparse entirely.
    if sys.argv[1:] == ["hook"]:
        sys.exit(cmd_hook(None))
    sys.exit(main())
