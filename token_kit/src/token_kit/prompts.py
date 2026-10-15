"""prompts.py -- what the USER actually typed, lifted out of the CLI's own
session transcripts and written to ONE markdown file.

WHY: a handoff file records what was DECIDED. It never holds what was ASKED, in
the asker's own words, because a paraphrase is written by the same thing that
is about to act on it.  The transcripts already hold the original sentences;
nothing had to be remembered, only extracted.

THE SHAPE OF THE SOURCE (established by reading real transcripts, not from
memory).  Each line of `<projects>/<slug of cwd>/<session>.jsonl` is one JSON
record.  A record the user typed looks like:

    {"type": "user", "uuid": ..., "timestamp": "2026-09-19T06:31:02.123Z",
     "sessionId": ..., "cwd": ..., "isSidechain": false,
     "promptSource": "typed",
     "message": {"role": "user", "content": "<the text>"}}

`message.content` is either that plain string or a list of blocks; only the
`text` blocks are the user's.  Every other `type: "user"` record in the file is
the harness talking to itself, and each such class is excluded by name below so
that a new one shows up as an unknown rather than as a fake prompt.

DE-DUPLICATION IS NOT OPTIONAL: a resumed session copies the earlier records
forward into the new file, so the same prompt appears in two transcripts with
ONE uuid.  The uuid is the identity; the file name is not.

PRIVACY: this is the user's own words on the user's own disk.  The output is
written 0600 and only ever to the path asked for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: One prompt is capped at this many bytes; a pasted file should not become the
#: whole document. The cut is marked, never silent.
MAX_PROMPT_BYTES = 20_000

#: Stamped into the output when a prompt is cut, with the byte count.
CUT_MARKER = "[... {n} bytes cut]"

DEFAULT_OUT = "PROMPTS.md"

#: Blocks the harness appends to a message. They are not typed, so they are
#: stripped out of a kept prompt -- and a record that is NOTHING but these is
#: not a prompt at all.
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

#: The slash-command wrapper: three sibling tags the CLI writes when a command
#: is run. The command NAME is what the user typed; the rest is scaffolding.
COMMAND_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
COMMAND_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
COMMAND_TAG_RE = re.compile(r"</?command-(?:name|message|args|contents)>", re.S)

#: `<bash-input>` is the ctrl-b bash line: the user typed it. Its output is not.
BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)

#: Every reason a `type: "user"` record is NOT a typed prompt. The names are
#: public: the tests assert one case each, and `--stdout` reports the tally.
EXCLUSIONS = (
    "tool_result",        # a tool's output, handed back as a user turn
    "sidechain",          # a subagent's transcript, not the user's
    "meta",               # isMeta: caveats, hand-backs, harness notices
    "compact_summary",    # "This session is being continued..." / isCompactSummary
    "system_prompt",      # promptSource == system: notifications, agent mail, ticks
    "task_notification",  # <task-notification> from a background agent
    "command_output",     # <local-command-stdout>, <bash-stdout>, caveats
    "hook_feedback",      # a hook's message, injected as a user turn
    "interrupt_marker",   # "[Request interrupted by user]" -- written by the CLI
    "empty",              # nothing left once the reminders are stripped
)


@dataclass(frozen=True)
class Prompt:
    uuid: str
    session: str
    when: datetime
    text: str

    @property
    def key(self) -> tuple:
        """Sort key. The timestamp orders it; the uuid breaks ties, so two
        prompts stamped the same millisecond still come out in one fixed
        order and a re-run rewrites byte-identical output."""
        return (self.when, self.uuid)


class Stats:
    def __init__(self) -> None:
        self.files = 0
        self.records = 0
        self.torn = 0
        self.duplicates = 0
        self.kept = 0
        self.excluded: dict[str, int] = {name: 0 for name in EXCLUSIONS}

    def drop(self, why: str) -> None:
        self.excluded[why] = self.excluded.get(why, 0) + 1

    def line(self) -> str:
        drops = " ".join(f"{k}={v}" for k, v in sorted(self.excluded.items()) if v)
        return (f"prompts={self.kept} sessions={self.files} torn_lines={self.torn} "
                f"duplicates={self.duplicates} records={self.records} [{drops}]")


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def transcripts(cwd: str, projects_root: Path, session: str = "") -> list[Path]:
    """The session files for one working directory, oldest name first.

    Only the top level: `<session>/subagents/*.jsonl` beside them holds the
    SUBAGENT transcripts, which are not the user speaking.
    """
    from token_kit.router.hook import slug

    root = projects_root / slug(str(Path(cwd).resolve()))
    if not root.is_dir():
        return []
    files = sorted(p for p in root.glob("*.jsonl") if p.is_file())
    if session:
        files = [p for p in files if p.stem == session]
    return files


def iter_records(path: Path, stats: Stats):
    """Stream one file line by line. A torn line is COUNTED, never fatal:
    a transcript being appended to right now has a half-written last line."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            stats.records += 1
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                stats.torn += 1
                continue
            if isinstance(rec, dict):
                yield rec
            else:
                stats.torn += 1


def message_text(rec: dict) -> str | None:
    """The user's own text, or None when the record carries none.

    A list content block may hold `tool_result` -- that is the harness handing
    a tool's output back, and it disqualifies the whole record.
    """
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(parts) if parts else None


def strip_reminders(text: str) -> str:
    return REMINDER_RE.sub("", text)


def unwrap_command(text: str) -> str:
    """A slash command, reduced to the one line the user typed.

    The CLI stores `<command-name>/loop</command-name>` plus a message and an
    args tag. Keeping the tags would put XML in the user's own words; dropping
    the record would lose that they ran the command at all.
    """
    name = COMMAND_NAME_RE.search(text)
    if not name:
        return text
    args = COMMAND_ARGS_RE.search(text)
    line = name.group(1).strip()
    if args and args.group(1).strip():
        line = f"{line} {args.group(1).strip()}"
    return line


def classify(rec: dict, text: str | None) -> str:
    """"" when the record is a typed prompt, else the exclusion class."""
    if text is None:
        return "tool_result"
    if rec.get("isSidechain"):
        return "sidechain"
    if rec.get("isMeta"):
        return "meta"
    if rec.get("isCompactSummary"):
        return "compact_summary"
    body = strip_reminders(text).strip()
    if not body:
        return "empty"
    if body.startswith("This session is being continued from a previous conversation"):
        return "compact_summary"
    if rec.get("promptSource") == "system":
        return "system_prompt"
    if body.startswith("<task-notification>"):
        return "task_notification"
    if body.startswith(("<local-command-stdout>", "<local-command-caveat>",
                        "<bash-stdout>", "<bash-stderr>")):
        return "command_output"
    if body.startswith(("<user-prompt-submit-hook>", "<hook-feedback>",
                        "<post-tool-use-hook>")):
        return "hook_feedback"
    if body.startswith("[Request interrupted"):
        return "interrupt_marker"
    return ""


def clean(text: str) -> str:
    """The kept text: reminders out, command wrapper reduced, edges trimmed."""
    body = strip_reminders(text)
    if "<command-name>" in body:
        body = unwrap_command(body)
    elif "<bash-input>" in body:
        found = BASH_INPUT_RE.search(body)
        if found:
            body = "! " + found.group(1).strip()
    body = COMMAND_TAG_RE.sub("", body)
    return body.strip()


def parse_when(rec: dict) -> datetime | None:
    stamp = rec.get("timestamp")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def collect(cwd: str, projects_root: Path, since: str = "", session: str = "",
            stats: Stats | None = None) -> tuple[list[Prompt], Stats]:
    stats = stats or Stats()
    seen: set[str] = set()
    out: list[Prompt] = []
    for path in transcripts(cwd, projects_root, session):
        stats.files += 1
        for rec in iter_records(path, stats):
            if rec.get("type") != "user":
                continue
            text = message_text(rec)
            why = classify(rec, text)
            if why:
                stats.drop(why)
                continue
            uuid = str(rec.get("uuid") or "")
            if uuid and uuid in seen:
                stats.duplicates += 1        # a resumed session repeats history
                continue
            when = parse_when(rec)
            if when is None:
                stats.drop("empty")
                continue
            if since and when.strftime("%Y-%m-%d") < since:
                continue
            if uuid:
                seen.add(uuid)
            out.append(Prompt(uuid=uuid or f"{path.stem}:{len(out)}",
                              session=str(rec.get("sessionId") or path.stem),
                              when=when, text=clean(text or "")))
    stats.kept = len(out)
    out.sort(key=lambda p: p.key)
    return out, stats


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def cap(text: str, limit: int = MAX_PROMPT_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    head = raw[:limit].decode("utf-8", errors="ignore")
    return head.rstrip() + "\n" + CUT_MARKER.format(n=len(raw) - limit)


def quote(text: str) -> str:
    return "\n".join(("> " + ln).rstrip() for ln in text.splitlines())


def render(prompts: list[Prompt], cwd: str, limit: int = MAX_PROMPT_BYTES) -> str:
    """The whole file. Nothing volatile goes in it -- no run time, no counts --
    so that a re-run over unchanged transcripts rewrites identical bytes."""
    head = [
        "# What the user said, verbatim",
        "",
        f"Extracted from the session transcripts for `{cwd}`. Times are local.",
        "Tool output, subagent transcripts, compaction summaries and harness",
        "notices are excluded; this file is only what was typed.",
        "",
    ]
    # A PERSON reads this file, so the times are written the way a person says
    # them: the date once, as a heading, and then a clock time per prompt. The
    # ORDER and the de-duplication still run on the real timestamp, so the
    # output stays byte-identical across re-runs.
    from token_kit import timefmt

    body = []
    day = ""
    for p in prompts:
        today = timefmt.human_day(p.when)
        if today != day:
            day = today
            body.append(f"## {today}")
            body.append("")
        body.append(f"### {timefmt.human_short(p.when)} · {p.session[:8]}")
        body.append("")
        body.append(quote(cap(p.text, limit)))
        body.append("")
    return "\n".join(head + body).rstrip() + "\n"


def write_private(path: Path, text: str) -> int:
    """Write 0600, and nowhere but here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)           # an existing file keeps its old mode otherwise
    return len(text.encode("utf-8"))


def refresh(cwd: str, out: Path, projects_root: Path | None = None,
            since: str = "", limit: int = MAX_PROMPT_BYTES) -> Stats:
    """Extract and write in one call. This is what the supervisor uses."""
    if projects_root is None:
        from token_kit.router.hook import projects_root as _root
        projects_root = _root()
    prompts, stats = collect(cwd, projects_root, since=since)
    write_private(out, render(prompts, cwd, limit))
    return stats


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="token-kit-prompts",
        description="Extract what the user typed from the session transcripts.")
    p.add_argument("--cwd", default=".", help="the working directory whose sessions to read")
    p.add_argument("--out", default=DEFAULT_OUT, help=f"output file (default {DEFAULT_OUT})")
    p.add_argument("--since", default="", metavar="YYYY-MM-DD")
    p.add_argument("--session", default="", metavar="ID", help="one session only")
    p.add_argument("--stdout", action="store_true", help="print instead of writing")
    return p


def main(argv=None) -> int:
    from token_kit.router.hook import projects_root

    args = build_parser().parse_args(argv)
    started = time.time()
    cwd = str(Path(args.cwd).resolve())
    prompts, stats = collect(cwd, projects_root(), since=args.since, session=args.session)
    text = render(prompts, cwd)
    if args.stdout:
        sys.stdout.write(text)
    else:
        out = Path(args.out).expanduser()
        size = write_private(out, text)
        print(f"prompts: wrote {out} ({size} bytes, mode 0600)")
    print(f"prompts: {stats.line()} in {time.time() - started:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
