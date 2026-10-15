"""Resolved machine facts: defaults, run-time detection, one optional override.

There are no shipped profile files and no `--profile` flag. A machine fact is
either DETECTED here or has a sane default, so the kit runs from anywhere with
nothing to edit. Exactly one file may override anything:

    ~/.config/token_kit/config.toml      never required, never written by the
                                         installer

with the tables `[codex]`, `[supervisor]` and `[router]`. Unknown keys and
unknown tables are tolerated on purpose: the router reads `[router]` itself,
and a key this module has not heard of must not stop a machine from starting.

THE ONE DETECTION THAT MATTERS is whether the codex home has to live on
node-local disk. Codex keeps its state in SQLite, and SQLite's POSIX locking is
unreliable over a network filesystem -- it fails on launch with "locking
protocol". So: if the home directory (or an already-existing ~/.codex) sits on
a network filesystem, codex needs a node-local home. That is read from
/proc/mounts, never from a hostname: a hostname is a name, a mount is evidence.
Off Linux there is no /proc/mounts, and the answer is False -- a laptop's home
is local disk.

Every resolved value carries where it came from (detected / default / override)
so `token-kit config` can print the machine's whole decision in one screen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Filesystem types whose POSIX locking cannot be relied on for SQLite. Chosen
#: as "the network and FUSE filesystems a home directory is actually served
#: from"; a type not in this set is treated as local disk.
NETWORK_FSTYPES = frozenset({
    "nfs", "nfs3", "nfs4", "lustre", "gpfs", "beegfs", "panfs",
    "cifs", "smb3", "smbfs", "afs", "ceph", "glusterfs", "9p", "vboxsf",
    "fuse.sshfs", "fuse.glusterfs", "fuse.cephfs", "fuse.s3fs",
})

MOUNTS = "/proc/mounts"

#: The one file that may override anything resolved here.
OVERRIDE_ENV = "TOKEN_KIT_CONFIG"


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "token_kit"


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "token_kit"


def override_path() -> Path:
    return Path(os.environ.get(OVERRIDE_ENV) or (config_home() / "config.toml"))


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------
def read_mounts(mounts: str | Path = MOUNTS) -> list[tuple[str, str]]:
    """[(mountpoint, fstype)] from a mounts table, or [] if there is none.

    Mount points are unescaped (the kernel writes \\040 for a space).
    """
    try:
        text = Path(mounts).read_text()
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point = fields[1].replace("\\040", " ").replace("\\011", "\t")
        out.append((point, fields[2]))
    return out


def fstype_of(path: str | Path, mounts: str | Path = MOUNTS) -> str:
    """The filesystem type serving `path`: the LONGEST matching mount point.

    Longest wins because mount points nest -- "/" always matches, so a shorter
    match would report the root filesystem for every path on the machine.
    """
    target = Path(os.path.expanduser(str(path))).resolve(strict=False)
    best_len, best_type = -1, ""
    for point, fstype in read_mounts(mounts):
        candidate = Path(point)
        if candidate == target or candidate in target.parents:
            if len(str(candidate)) > best_len:
                best_len, best_type = len(str(candidate)), fstype
    return best_type


def needs_node_local_codex_home(mounts: str | Path = MOUNTS,
                                home: str | Path | None = None) -> bool:
    """Is the codex home on a filesystem whose locking SQLite cannot trust?

    An existing ~/.codex is asked first (it may be a symlink onto somewhere
    else entirely); otherwise the home directory itself answers.
    """
    root = Path(os.path.expanduser(str(home))) if home else Path(os.path.expanduser("~"))
    codex_home = root / ".codex"
    probe = codex_home if codex_home.exists() else root
    return fstype_of(probe, mounts) in NETWORK_FSTYPES


# --------------------------------------------------------------------------
# the override file
# --------------------------------------------------------------------------
def read_override(path: str | Path | None = None) -> dict:
    """The override file as a plain dict. Absent is {}. Unparseable REFUSES.

    An absent file is the normal case. A file that exists and does not parse is
    never coerced into the defaults: a person who wrote one means it.
    """
    import tomllib

    src = Path(path) if path is not None else override_path()
    if not Path(src).is_file():
        return {}
    with Path(src).open("rb") as handle:
        try:
            return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"token_kit: {src} is not valid TOML ({exc})")


# --------------------------------------------------------------------------
# the resolved answer
# --------------------------------------------------------------------------
@dataclass
class Resolved:
    source_file: Path
    state_dir: Path
    evidence_dir: Path
    data_root: Path
    codex_launcher: str
    codex_binary: str
    codex_tmp_root: Path
    codex_home_prefix: str
    codex_node_local_home: bool
    codex_bypass_approvals: bool
    hook_matcher: str
    session_start: bool
    settings: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    #: key -> "detected" | "default" | "override"
    origins: dict = field(default_factory=dict)

    def table(self, name: str) -> dict:
        """One table of the override file, verbatim, for a consumer that owns
        its own keys (the router does). Absent is {}."""
        value = self.raw.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def as_env(self) -> dict:
        """The values the kit's own shims read, as a flat env mapping."""
        return {
            "TK_STATE_DIR": str(self.state_dir),
            "TK_EVIDENCE_DIR": str(self.evidence_dir),
            "TK_DATA_ROOT": str(self.data_root),
            "TK_CODEX_LAUNCHER": self.codex_launcher,
            "TK_CODEX_NODE_LOCAL_HOME": "yes" if self.codex_node_local_home else "no",
        }


#: default value per resolved key, for everything that is not detected.
_DEFAULTS = {
    "data_root": "~",
    "codex_launcher": "codex",
    "codex_binary": "",
    "codex_tmp_root": "/tmp",
    "codex_home_prefix": "codex-home",
    "codex_bypass_approvals": True,
    "hook_matcher": "*",
    "session_start": True,
}

#: override key -> (table, resolved attribute)
_OVERRIDE_KEYS = {
    ("codex", "launcher"): "codex_launcher",
    ("codex", "binary"): "codex_binary",
    ("codex", "tmp_root"): "codex_tmp_root",
    ("codex", "node_local_tmp_root"): "codex_tmp_root",
    ("codex", "home_prefix"): "codex_home_prefix",
    ("codex", "node_local_home_prefix"): "codex_home_prefix",
    ("codex", "node_local_home"): "codex_node_local_home",
    ("codex", "bypass_approvals"): "codex_bypass_approvals",
    ("paths", "state_dir"): "state_dir",
    ("paths", "evidence_dir"): "evidence_dir",
    ("paths", "data_root"): "data_root",
    ("hook", "matcher"): "hook_matcher",
    ("hook", "session_start"): "session_start",
}

_PATH_ATTRS = {"state_dir", "evidence_dir", "data_root", "codex_tmp_root"}


def resolve(override: str | Path | dict | None = None,
            mounts: str | Path = MOUNTS) -> Resolved:
    """Defaults <- detection <- the override file. Never raises on absence."""
    raw = override if isinstance(override, dict) else read_override(override)
    src = Path("<dict>") if isinstance(override, dict) else (
        Path(override) if override is not None else override_path())

    state = state_home()
    node_local = needs_node_local_codex_home(mounts)
    out = Resolved(
        source_file=src,
        state_dir=state,
        evidence_dir=state / "evidence",
        data_root=Path(os.path.expanduser(_DEFAULTS["data_root"])),
        codex_launcher=_DEFAULTS["codex_launcher"],
        codex_binary=_DEFAULTS["codex_binary"],
        codex_tmp_root=Path(_DEFAULTS["codex_tmp_root"]),
        codex_home_prefix=_DEFAULTS["codex_home_prefix"],
        codex_node_local_home=node_local,
        codex_bypass_approvals=_DEFAULTS["codex_bypass_approvals"],
        hook_matcher=_DEFAULTS["hook_matcher"],
        session_start=_DEFAULTS["session_start"],
        settings={"subagentPromptCacheTtl": "1h"},
        raw=raw,
    )
    out.origins = {name: "default" for name in vars(out)
                   if name not in ("source_file", "raw", "origins")}
    out.origins["state_dir"] = "detected"
    out.origins["evidence_dir"] = "detected"
    out.origins["codex_node_local_home"] = "detected"

    for (table, key), attr in _OVERRIDE_KEYS.items():
        section = raw.get(table)
        if not isinstance(section, dict) or key not in section:
            continue
        value = section[key]
        if attr in _PATH_ATTRS:
            value = Path(os.path.expanduser(str(value)))
        setattr(out, attr, value)
        out.origins[attr] = "override"
    settings = raw.get("settings")
    if isinstance(settings, dict):
        out.settings = dict(settings)
        out.origins["settings"] = "override"
    return out


def describe(resolved: Resolved | None = None) -> str:
    """One screen: every resolved value and where it came from."""
    res = resolved or resolve()
    lines = [f"override file  {res.source_file}"
             f"  ({'present' if Path(res.source_file).is_file() else 'absent -- fine'})",
             f"mounts table   {MOUNTS}"
             f"  ({'readable' if read_mounts() else 'absent; home treated as local disk'})",
             ""]
    width = max(len(k) for k in res.origins)
    for key in sorted(res.origins):
        lines.append(f"  {key:<{width}}  {getattr(res, key)!s:<44} [{res.origins[key]}]")
    extra = [name for name in res.raw if name not in ("codex", "paths", "hook", "settings")]
    if extra:
        lines += ["", f"  other override tables, passed through untouched: {', '.join(sorted(extra))}"]
    return "\n".join(lines)
