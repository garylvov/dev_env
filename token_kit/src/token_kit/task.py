"""token-kit-task -- the working folder, titled and findable.

    token-kit-task new "<title>" [--root DIR]
    token-kit-task lane <task-dir> <name> [--under LANE-DIR]
    token-kit-task list [--root DIR] [--all] [--open]
    token-kit-task retitle <task-dir> "<new title>" [--summary "<one line>"]
    token-kit-task find <words>... [--all]
    token-kit-task resume <words-or-path> [--go]
    token-kit-task done <task-dir>
    token-kit-task reopen <task-dir>

THREE DEFECTS THIS CLOSES.

1. NOTHING TITLED THE FOLDER. The guide said `<task>/` and whoever was holding
   the keyboard picked a name, so two pieces of work a month apart sorted next
   to each other under names only their author understood. `new` names it
   `<YYYY-MM-DD>_<HHMM>_<slug>`: the 24h stamp in the NAME so a plain `ls`
   sorts by age, the slug so a person can read it.
2. THE FIRST TITLE IS A GUESS. It is made before the work, which is the worst
   moment to name anything. `retitle` makes the real one cheap: the slug part
   of the folder changes, the date and time part does not (so sorting and age
   stay true), and a RELATIVE SYMLINK is left at the old name, because that
   name is already written into briefs, out.md files, a supervisor registry
   keyed by the state file's path, and the argv of agents that are still
   running. A rename with no symlink breaks all of them silently.
3. DISCOVERY DEPENDED ON THE CURRENT DIRECTORY. `list` could only see the root
   you were standing in. So every task also appends a row to a machine-wide
   append-only index under the XDG state dir, and `list --all`, `find` and
   `resume` read that instead of guessing where you left things.

THE HEADER IS THE INTERFACE. This tool parses ONLY the `Started:`, `Status:`,
`Cwd:` and `Summary:` lines under the `# <title>` heading of STATE.md. The rest
of the file is free text a person and the main thread own; nothing here rewrites
it. Machine rows stay ISO with an offset (they are sorted and parsed); every
time a person reads is `token_kit.timefmt.human`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from token_kit import timefmt

def default_root() -> Path:
    """Where `new` and `list` look when no --root is given.

    Beside Claude Code's own per-user data, never in the directory you happen to
    stand in: working folders are then in ONE place on the machine, no project
    tree grows a folder it did not ask for, and `--root` still puts one anywhere.
    The project a task belongs to is its `Cwd:` line, not its location.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "token_kit" / "work"

def resolve_root(root) -> Path:
    return Path(root).expanduser().absolute() if root else default_root()



#: The folder name: sortable stamp first, readable slug last.
DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(.+)$")

#: The only lines this tool reads back out of STATE.md.
HEADER_KEYS = ("Started", "Status", "Cwd", "Summary")
HEADER_RE = re.compile(r"^(" + "|".join(HEADER_KEYS) + r"):[ \t]*(.*)$")
TITLE_RE = re.compile(r"^#[ \t]+(.*)$")

SLUG_MAX = 40
STATE_NAME = "STATE.md"
PROMPTS_NAME = "PROMPTS.md"
INDEX_NAME = "tasks.tsv"

#: How a row's free-text column is cut so a listing stays one line per task.
COLUMN = 56

LANE_STUB = """\
every claim below is a lead to verify

## Goal

## Owns

## Done when

## Hand back
out.md in this directory, line 1 = RESULT: ...
"""


# --------------------------------------------------------------------- naming
def slug(title: str, limit: int = SLUG_MAX) -> str:
    """Lowercase ascii words, hyphen separated, cut at a word boundary.

    A title is prose: it carries accents, punctuation and capitals, none of
    which belong in a path that gets typed, tab completed and pasted into a
    brief. An empty result is "task", never an empty path component.
    """
    folded = unicodedata.normalize("NFKD", str(title))
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", ascii_only) if w]
    out: list[str] = []
    for word in words:
        candidate = "-".join(out + [word])
        if out and len(candidate) > limit:
            break
        out.append(word)
    text = "-".join(out)[:limit].strip("-")
    return text or "task"


def dir_name(when: datetime, title: str) -> str:
    return f"{when.strftime('%Y-%m-%d')}_{when.strftime('%H%M')}_{slug(title)}"


def free_path(parent: Path, name: str) -> Path:
    """`name`, or name-2, name-3 ... The collision suffix is never silent: the
    caller prints the path it actually got."""
    candidate = parent / name
    n = 1
    while candidate.exists() or candidate.is_symlink():
        n += 1
        candidate = parent / f"{name}-{n}"
    return candidate


# --------------------------------------------------------------- the STATE.md
def state_body(title: str, when: datetime, cwd: str, summary: str = "") -> str:
    return (f"# {title}\n"
            f"\n"
            f"Started: {timefmt.human(when)}\n"
            f"Status: open\n"
            f"Cwd: {cwd}\n"
            f"Summary: {summary}\n"
            f"\n"
            f"## Decisions\n"
            f"\n"
            f"## In flight\n"
            f"\n"
            f"## Next\n")


class Header:
    """The parsed head of a task's STATE.md. Absent keys are empty strings."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.title = ""
        self.values = {key: "" for key in HEADER_KEYS}
        try:
            text = self.path.read_text(errors="replace")
        except OSError:
            return
        for line in text.splitlines():
            if line.startswith("## "):
                break                       # the free text starts here
            if not self.title:
                found = TITLE_RE.match(line)
                if found:
                    self.title = found.group(1).strip()
                    continue
            found = HEADER_RE.match(line)
            if found:
                self.values[found.group(1)] = found.group(2).strip()

    def __getitem__(self, key: str) -> str:
        return self.values.get(key, "")

    @property
    def status(self) -> str:
        return self["Status"] or "open"

    @property
    def started(self) -> str:
        return self["Started"]


def rewrite_header(path: Path, title: str | None = None, **values: str) -> None:
    """Change the title line and named header lines IN PLACE, and nothing else.

    A key that is not there yet is inserted after the last header line, so a
    hand-written STATE.md gains `Status:` rather than being reformatted.
    """
    lines = path.read_text(errors="replace").splitlines()
    want = {k: v for k, v in values.items() if v is not None}
    seen: set[str] = set()
    out: list[str] = []
    last_header = -1
    titled = False
    body = False
    for line in lines:
        if line.startswith("## "):
            # The header ends at the first section. A `Status:` line further
            # down is somebody's prose and is none of this tool's business.
            body = True
        if body:
            out.append(line)
            continue
        if not titled and TITLE_RE.match(line):
            titled = True
            out.append(f"# {title}" if title is not None else line)
            continue
        found = HEADER_RE.match(line)
        if found and found.group(1) in want:
            out.append(f"{found.group(1)}: {want[found.group(1)]}")
            seen.add(found.group(1))
            last_header = len(out) - 1
            continue
        if found:
            last_header = len(out)
        out.append(line)
    missing = [k for k in HEADER_KEYS if k in want and k not in seen]
    for key in missing:
        at = last_header + 1 if last_header >= 0 else len(out)
        out.insert(at, f"{key}: {want[key]}")
        last_header = at
    path.write_text("\n".join(out) + "\n")


# -------------------------------------------------------------------- the index
def index_path() -> Path:
    from token_kit import config as config_mod
    return config_mod.state_home() / INDEX_NAME


def index_append(event: str, path: Path, title: str) -> None:
    """ONE row, ONE write, O_APPEND. Two processes appending at the same moment
    interleave whole rows and never half rows; a reader that meets a torn last
    line (a crash mid write) drops it instead of failing.

    FAILS OPEN: an index that cannot be written must never stop a task from
    being created. The task on disk is the truth; this is only the map to it.
    """
    row = (f"{timefmt.iso()}\t{event}\t{Path(path).absolute()}"
           f"\t{str(title).replace(chr(9), ' ')}\n")
    try:
        target = index_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, row.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:                       # noqa: BLE001 -- fail open
        print(f"token-kit-task: index not updated ({exc})", file=sys.stderr)


def index_rows(path: Path | None = None) -> list[tuple[str, str, str, str]]:
    """Every well formed row, oldest first. A torn last line is dropped."""
    target = Path(path) if path else index_path()
    try:
        text = target.read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4 or not parts[2].startswith("/"):
            continue
        out.append((parts[0], parts[1], parts[2], parts[3]))
    return out


# ---------------------------------------------------------------------- tasks
class Task:
    """One task directory. Everything read back comes from the header."""

    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        self.header = Header(self.path / STATE_NAME)

    @property
    def is_task(self) -> bool:
        return (self.path / STATE_NAME).is_file()

    @property
    def title(self) -> str:
        return self.header.title or self.path.name

    @property
    def summary(self) -> str:
        return self.header["Summary"]

    @property
    def status(self) -> str:
        return self.header.status

    @property
    def cwd(self) -> str:
        return self.header["Cwd"] or str(self.path)

    @property
    def stamp(self) -> str:
        """The sortable part of the name, or the mtime when the name has none."""
        found = DIR_RE.match(self.path.name)
        if found:
            return f"{found.group(1)}_{found.group(2)}"
        try:
            return datetime.fromtimestamp(
                (self.path / STATE_NAME).stat().st_mtime).strftime("%Y-%m-%d_%H%M")
        except OSError:
            return "0000-00-00_0000"

    @property
    def lanes(self) -> int:
        root = self.path / "lanes"
        try:
            return sum(1 for p in root.iterdir() if p.is_dir())
        except OSError:
            return 0

    def newest_result(self) -> str:
        """The RESULT line of the most recently written out.md under this task."""
        best, newest = "", -1.0
        for out in (self.path / "lanes").rglob("out.md"):
            try:
                when = out.stat().st_mtime
            except OSError:
                continue
            if when <= newest:
                continue
            try:
                with out.open("r", errors="replace") as fh:
                    first = fh.readline().strip()
            except OSError:
                continue
            newest, best = when, first
        return best

    def row(self) -> str:
        """One line, the same shape for `list`, `list --all` and `find`."""
        started = self.header.started or timefmt.human_day(
            datetime.strptime(self.stamp, "%Y-%m-%d_%H%M"))
        tail = self.summary or self.newest_result()
        return (f"{started:<26} {self.status:<5} {cut(self.title, COLUMN):<{COLUMN}} "
                f"lanes={self.lanes:<3} {cut(tail, COLUMN):<{COLUMN}} {self.path}")


def cut(text: str, limit: int = COLUMN) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def tasks_in(root: Path) -> list[Task]:
    """Task directories directly under `root`. A symlink left by `retitle` is
    skipped: it is the SAME task under an old name, and listing it twice would
    make a rename look like new work."""
    out = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for path in entries:
        if path.is_symlink() or not path.is_dir():
            continue
        task = Task(path)
        if task.is_task:
            out.append(task)
    return out


def tasks_from_index() -> tuple[list[Task], int]:
    """Every task the index knows, newest first, plus how many have gone away.

    A path is resolved so that a task reached through a `retitle` symlink and
    the same task under its new name collapse to one entry.
    """
    seen: dict[str, Task] = {}
    gone = 0
    for _when, _event, path, _title in index_rows():
        p = Path(path)
        if not (p / STATE_NAME).is_file():
            gone += 1
            continue
        key = str(p.resolve())
        if key not in seen:
            seen[key] = Task(p.resolve())
    out = sorted(seen.values(), key=lambda t: t.stamp, reverse=True)
    return out, gone


# ----------------------------------------------------------------------- verbs
def cmd_new(args) -> int:
    root = resolve_root(args.root)
    when = datetime.now().astimezone()
    root.mkdir(parents=True, exist_ok=True)
    task = free_path(root, dir_name(when, args.title))
    task.mkdir()
    (task / "lanes").mkdir()
    (task / STATE_NAME).write_text(
        state_body(args.title, when, str(Path.cwd()), args.summary))
    index_append("new", task, args.title)
    print(task)
    return 0


def cmd_lane(args) -> int:
    task = Path(args.task_dir).expanduser().absolute()
    if not (task / STATE_NAME).is_file():
        print(f"token-kit-task: not a task directory (no {STATE_NAME}): {task}",
              file=sys.stderr)
        return 1
    if "/" in args.name:
        print("token-kit-task: a lane name is one path component. For a nested "
              "lane use --under <lane-dir>.", file=sys.stderr)
        return 1
    # ONE way to nest, and this is it: --under names the PARENT LANE's version
    # directory, so a worker lands in a lanes/ folder inside its lead's version
    # folder. A name with a slash in it is refused rather than guessed at.
    base = Path(args.under).expanduser().absolute() if args.under else task
    if args.under and not base.is_dir():
        print(f"token-kit-task: --under is not a directory: {base}", file=sys.stderr)
        return 1
    lane = base / "lanes" / args.name
    lane.mkdir(parents=True, exist_ok=True)
    version = 0
    while (lane / f"v{version}").exists():
        version += 1                    # a retry is a new version, never a rewrite
    out = lane / f"v{version}"
    out.mkdir()
    (out / "in.md").write_text(LANE_STUB)
    print(out)
    return 0


def cmd_list(args) -> int:
    if args.all:
        tasks, gone = tasks_from_index()
        if gone:
            print(f"# {gone} task(s) in the index no longer exist on disk")
    else:
        root = resolve_root(args.root)
        tasks = sorted(tasks_in(root), key=lambda t: t.stamp, reverse=True)
        if not tasks:
            print(f"# no tasks under {root} (try --all for the machine wide index)")
            return 0
    for task in tasks:
        if args.open and task.status != "open":
            continue
        print(task.row())
    return 0


def cmd_retitle(args) -> int:
    task = Path(args.task_dir).expanduser().absolute()
    state = task / STATE_NAME
    if not state.is_file():
        print(f"token-kit-task: not a task directory: {task}", file=sys.stderr)
        return 1
    # The task may have been reached through an older name's symlink. Rename the
    # REAL directory, or the rename would move a link and orphan the task.
    real = task.resolve()
    found = DIR_RE.match(real.name)
    if not found:
        print(f"token-kit-task: {real.name} has no <date>_<time>_<slug> prefix; "
              "the stamp is what keeps sorting and age true, so this folder "
              "cannot be retitled by this tool.", file=sys.stderr)
        return 1
    target = real.parent / f"{found.group(1)}_{found.group(2)}_{slug(args.title)}"
    if target == real:
        rewrite_header(real / STATE_NAME, title=args.title,
                       **({"Summary": args.summary} if args.summary is not None else {}))
        index_append("retitle", real, args.title)
        print(real)
        return 0
    if target.exists() or target.is_symlink():
        print(f"token-kit-task: REFUSING: {target} already exists", file=sys.stderr)
        return 1

    old = real.name
    real.rename(target)
    # Every name this task was known by must keep resolving, and must point at
    # the CURRENT directory: a symlink chain breaks the moment one link in it is
    # cleaned up, so an existing link is RE-POINTED, never left to chain.
    for entry in target.parent.iterdir():
        if entry.is_symlink() and os.readlink(entry) in (old, str(real)):
            entry.unlink()
            entry.symlink_to(target.name)
    link = target.parent / old
    if not link.exists() and not link.is_symlink():
        link.symlink_to(target.name)          # RELATIVE: the pair can be moved
    rewrite_header(target / STATE_NAME, title=args.title,
                   **({"Summary": args.summary} if args.summary is not None else {}))
    index_append("retitle", target, args.title)
    print(target)
    return 0


def _flip(args, status: str, event: str) -> int:
    task = Path(args.task_dir).expanduser().absolute()
    state = task / STATE_NAME
    if not state.is_file():
        print(f"token-kit-task: not a task directory: {task}", file=sys.stderr)
        return 1
    rewrite_header(state, Status=status)
    index_append(event, task.resolve(), Header(state).title)
    print(f"{task.resolve()} {status}")
    return 0


def cmd_done(args) -> int:
    return _flip(args, "done", "done")


def cmd_reopen(args) -> int:
    return _flip(args, "open", "reopen")


# ------------------------------------------------------------------- searching
#: A body is streamed and bounded: a PROMPTS.md is the whole of what somebody
#: typed for a week, and `find` must not become a reason to read all of it.
BODY_MAX_BYTES = 200_000


def body_has(path: Path, words: list[str], limit: int = BODY_MAX_BYTES) -> bool:
    """Every word somewhere in the first `limit` bytes. Streamed, never slurped."""
    want = set(words)
    read = 0
    try:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                read += len(line)
                low = line.lower()
                for word in list(want):
                    if word in low:
                        want.discard(word)
                if not want:
                    return True
                if read >= limit:
                    break
    except OSError:
        return False
    return not want


def matches(task: Task, words: list[str]) -> int | None:
    """0 for a name hit, 1 for a body hit, None for no hit. All words must hit.

    The rank exists because a word in a title is a much stronger signal than the
    same word buried in a transcript of everything anyone ever typed.
    """
    names = " ".join([task.title, task.summary, task.path.name]).lower()
    if all(word in names for word in words):
        return 0
    for name in (STATE_NAME, PROMPTS_NAME):
        if body_has(task.path / name, words):
            return 1
    return None


def search(words: list[str], everywhere: bool, root: Path) -> list[Task]:
    words = [w.lower() for w in words if w.strip()]
    if not words:
        return []
    pool = tasks_from_index()[0] if everywhere else sorted(
        tasks_in(root), key=lambda t: t.stamp, reverse=True)
    ranked = []
    for task in pool:
        rank = matches(task, words)
        if rank is not None:
            ranked.append((rank, task))
    ranked.sort(key=lambda pair: (pair[0], [-ord(c) for c in pair[1].stamp]))
    return [task for _rank, task in ranked]


def cmd_find(args) -> int:
    hits = search(args.words, args.all, resolve_root(args.root))
    if not hits:
        print("# no task matched")
        return 1
    for task in hits:
        print(task.row())
    return 0


def resume_command(task: Task) -> str:
    return ("token-kit-supervise launch "
            f"--state-file {task.path / STATE_NAME} --cwd {task.cwd}")


def cmd_resume(args) -> int:
    root = resolve_root(args.root)
    direct = Path(" ".join(args.words)).expanduser()
    if (direct / STATE_NAME).is_file():
        hits = [Task(direct.resolve())]
    else:
        # A resume is asked from wherever the person happens to be standing, so
        # it searches the machine wide index unless told otherwise.
        hits = search(args.words, not args.local, root)
    if not hits:
        print("# no task matched", file=sys.stderr)
        return 1
    if len(hits) > 1:
        # NEVER GUESS. Several matches print as rows and exit 2, so a script
        # that pipes this cannot silently resume the wrong piece of work.
        for task in hits:
            print(task.row())
        print(f"# {len(hits)} tasks matched; name one of them", file=sys.stderr)
        return 2
    task = hits[0]
    rewrite_header(task.path / STATE_NAME, Status="open")
    index_append("reopen", task.path, task.title)
    command = resume_command(task)
    print(command)
    if args.go:
        os.execvp(command.split()[0], command.split())
    return 0


# ------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="token-kit-task",
                                description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="verb", required=True)

    s = sub.add_parser("new", help="create a titled task folder")
    s.add_argument("title")
    s.add_argument("--root", default=None)
    s.add_argument("--summary", default="")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("lane", help="create the next version of a lane")
    s.add_argument("task_dir")
    s.add_argument("name")
    s.add_argument("--under", default="", help="nest under this lane version dir")
    s.set_defaults(fn=cmd_lane)

    s = sub.add_parser("list", help="one line per task")
    s.add_argument("--root", default=None)
    s.add_argument("--all", action="store_true", help="the machine wide index")
    s.add_argument("--open", action="store_true", help="only tasks still open")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("retitle", help="rename the work once you know what it is")
    s.add_argument("task_dir")
    s.add_argument("title")
    s.add_argument("--summary", default=None)
    s.set_defaults(fn=cmd_retitle)

    s = sub.add_parser("find", help="search titles, summaries and bodies")
    s.add_argument("words", nargs="+")
    s.add_argument("--root", default=None)
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("resume", help="print the command that restarts a task")
    s.add_argument("words", nargs="+")
    s.add_argument("--root", default=None)
    s.add_argument("--local", action="store_true", help="search --root, not the index")
    s.add_argument("--go", action="store_true", help="run it instead of printing it")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("done", help="mark a task done")
    s.add_argument("task_dir")
    s.set_defaults(fn=cmd_done)

    s = sub.add_parser("reopen", help="mark a task open again")
    s.add_argument("task_dir")
    s.set_defaults(fn=cmd_reopen)
    return p


def legacy_main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


def main(argv=None) -> int:
    """The historical executable now uses the shared task core."""
    from token_kit.__main__ import main as unified_main
    return unified_main(argv)


if __name__ == "__main__":
    sys.exit(main())
