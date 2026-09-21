"""canary -- prove MECHANICALLY that an instruction file is in the model's context.

THREE OUTCOMES, NEVER COLLAPSED:
  LOADED        the slot's expected witness came back from a clean probe
  NOT_LOADED    the probe was PROVEN clean (liveness nonce echoed, JSON ok, the
                slot's answer line present) and the witness was absent
  PROBE_BROKEN  anything that means we do not know: non-zero exit, non-JSON,
                is_error, subtype != success, empty result, missing or wrong
                liveness nonce, a missing answer line for that slot.
A PROBE_BROKEN reported as NOT_LOADED is the defect that makes this worthless,
so every unknown is forced to PROBE_BROKEN by construction: `classify` reaches
LOADED or NOT_LOADED only after the liveness nonce matched.

TOKEN MODE (mode=token)
  One self-contained PLAIN line, the LAST line of the instruction file:
    INSTRUCTION-CANARY slot=<slot-id> token=<16 hex, upper> gen=<date>
  MEASURED: an HTML-comment form (<!-- ... -->) is STRIPPED before the file
  reaches the model -- same file, same probe, the plain line came back and the
  commented one did not. So the marker must NOT be hidden in a comment, and
  `verify` refuses a commented one by name (COMMENTED_MARKER) rather than
  calling it present. The plain line is inert: no verb, no imperative, so it
  cannot be read as an instruction; it is greppable, carries its own file
  identity in slot=, and survives a human editing the text around it.

WITNESS MODE (mode=witness)
  For files we are FORBIDDEN to edit: ask a question only that file's text can
  answer, and match the answer. No edit to the file at all.

Python, standard library only. Rows are appended, never rewritten.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

LOADED = "LOADED"
NOT_LOADED = "NOT_LOADED"
PROBE_BROKEN = "PROBE_BROKEN"

#: Assembled from parts so that no scan of this file -- or of this process's own
#: argv -- can match the marker and invent a hit in the scanner itself.
_MARK_A, _MARK_B = "INSTRUCTION", "CANARY"
MARK = f"{_MARK_A}-{_MARK_B}"

_COMMENT = re.compile(r"<!--.*?-->", re.S)


@dataclass(frozen=True)
class Slot:
    slot: str
    mode: str
    path: str
    expect: str
    question: str = ""


@dataclass(frozen=True)
class Row:
    ts: str
    run_id: str
    directory: str
    slot: str
    status: str
    detail: str
    model: str
    evidence: str

    def tsv(self) -> str:
        return "\t".join((self.ts, self.run_id, self.directory, self.slot,
                          self.status, self.detail, self.model, self.evidence))


# ------------------------------------------------------------------ markers
def emit(slot: str, token: str | None = None, when: str | None = None) -> str:
    """One marker line for `slot`. The token is 16 upper-case hex characters."""
    if not slot:
        raise SystemExit("canary: emit needs a slot id")
    tok = token or os.urandom(8).hex().upper()
    return f"{MARK} slot={slot} token={tok} gen={when or date.today().isoformat()}"


def marker_of(line: str, slot: str) -> str:
    """The marker on `line` for `slot`, or "" -- the ONE place the shape is read."""
    if not line.startswith(MARK):
        return ""
    return line if f"slot={slot}" in line.split() else ""


def verify(slot: str, path: str | Path) -> tuple[bool, str]:
    """Is the slot's marker the last line of the file, as a PLAIN line?

    Returns (ok, detail). The detail names the failure: MISSING_FILE,
    COMMENTED_MARKER (the measured stripping case) or NO_MARKER_AT_EOF.
    """
    src = Path(path)
    try:
        text = src.read_text(errors="replace")
    except OSError:
        return False, f"MISSING_FILE {src}"
    lines = [l for l in text.splitlines() if l.strip()]
    last = lines[-1].strip() if lines else ""
    found = marker_of(last, slot)
    if found:
        return True, f"OK {src} {found}"
    if MARK in _COMMENT.sub(" ", text) or MARK not in text:
        return False, f"NO_MARKER_AT_EOF {src}"
    # The marker IS in the file but only inside an HTML comment, which is
    # stripped before the file reaches the model. Present on disk, absent to
    # the model: the exact trap this names instead of hiding.
    return False, f"COMMENTED_MARKER {src}"


# ------------------------------------------------------------------ manifest
def read_manifest(path: str | Path) -> list[Slot]:
    """TSV, '#' comments: slot, mode, path, expect-regex, question."""
    out: list[Slot] = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = (line.split("\t") + ["", "", "", "", ""])[:5]
        out.append(Slot(parts[0].strip(), parts[1].strip() or "token",
                        parts[2].strip(), parts[3].strip(), parts[4].strip()))
    if not out:
        raise SystemExit(f"canary: manifest has no slots: {path}")
    return out


def question_for(slot: Slot) -> str:
    if slot.mode == "token":
        return (f"the token= value of the {MARK} marker line whose slot= is "
                f"{slot.slot}, exactly as written, or NONE if no such marker "
                "line is in your instructions")
    return slot.question


def build_prompt(slots: list[Slot], nonce: str) -> str:
    body = "".join(f"{s.slot}: {question_for(s)}\n" for s in slots)
    return (
        "Answer only from the instructions already given to you. Use no tools. "
        "Do not search. Do not explain.\n"
        f"Print exactly {len(slots) + 1} lines and nothing else.\n"
        f"Line 1 must be: ALIVE {nonce}\n"
        "Then one line per key below, in this exact form: <key>=<answer>\n"
        "If your instructions do not contain the answer, write NONE as the "
        "answer, but still print the line.\n\n"
        f"{body}")


# ------------------------------------------------------------------- running
def run_claude(prompt: str, model: str, directory: str, settings: str = "",
               binary: str = "claude") -> tuple[int, str, str]:
    """ONE non-interactive turn. stdin is /dev/null on purpose.

    GOTCHA the bash original measured: `claude -p` may print a stdin warning
    ABOVE the JSON. Closing stdin prevents it, and `first_json` below slices
    from the first '{' so a stray banner can never be mistaken for bad JSON.
    """
    cmd = [binary]
    if settings:
        cmd += ["--settings", settings]
    cmd += ["--model", model, "--output-format", "json", "-p", prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=directory or None, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout, proc.stderr


def first_json(stdout: str) -> str:
    start = stdout.find("{")
    return stdout[start:] if start >= 0 else ""


def run_health(rc: int, stdout: str, nonce: str) -> tuple[str, str, dict]:
    """(broken_reason, result_text, usage). broken_reason "" means clean.

    Every unknown lands here as a reason string, and only a run with NO reason
    is ever allowed to produce LOADED or NOT_LOADED.
    """
    if rc != 0:
        return f"exit_rc={rc}", "", {}
    raw = first_json(stdout)
    if not raw:
        return "no_json_on_stdout", "", {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return "bad_json", "", {}
    if not isinstance(payload, dict):
        return "bad_json", "", {}
    if payload.get("is_error"):
        return "is_error", "", payload
    subtype = payload.get("subtype", "?")
    if subtype != "success":
        return f"subtype={subtype}", "", payload
    result = payload.get("result") or ""
    if not result:
        return "empty_result", "", payload
    if not re.search(rf"^ALIVE {re.escape(nonce)}$", result, re.M):
        return "liveness_nonce_absent", result, payload
    return "", result, payload


def classify(slot: Slot, broken: str, result: str) -> tuple[str, str]:
    """(status, detail) for ONE slot. PROBE_BROKEN is the default outcome."""
    if broken:
        return PROBE_BROKEN, broken
    match = re.search(rf"^[ \t]*{re.escape(slot.slot)}[ \t]*=(.*)$", result, re.M)
    if match is None:
        return PROBE_BROKEN, "no_answer_line_for_slot"
    answer = match.group(1).strip()
    if slot.expect and re.search(slot.expect, answer):
        return LOADED, f"witness={answer}"
    return NOT_LOADED, f"answered={answer or '<empty>'}"


def probe(manifest: str | Path, directory: str, model: str = "sonnet",
          settings: str = "", rows_file: str | Path | None = None,
          evidence: str | Path | None = None, runner=run_claude,
          nonce: str | None = None) -> tuple[list[Row], str]:
    """ONE probe, every slot classified. Returns (rows, broken_reason)."""
    import tempfile

    slots = read_manifest(manifest)
    run_id = os.urandom(4).hex()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    evid = Path(evidence) if evidence else Path(tempfile.mkdtemp())
    evid.mkdir(parents=True, exist_ok=True)

    live = nonce or os.urandom(6).hex().upper()
    prompt = build_prompt(slots, live)
    (evid / f"{run_id}.prompt").write_text(prompt)

    rc, stdout, stderr = runner(prompt, model, directory, settings)
    (evid / f"{run_id}.json").write_text(stdout)
    (evid / f"{run_id}.err").write_text(stderr)

    broken, result, _usage = run_health(rc, stdout, live)
    rows = []
    for slot in slots:
        status, detail = classify(slot, broken, result)
        present = "PRESENT" if slot.path and Path(slot.path).exists() else "ABSENT"
        rows.append(Row(ts, run_id, str(directory), slot.slot, status,
                        f"file_{present}; {detail}", model,
                        str(evid / f"{run_id}.json")))
    text = "".join(r.tsv() + "\n" for r in rows)
    (evid / f"{run_id}.rows").write_text(text)
    if rows_file:
        with Path(rows_file).open("a") as fh:
            fh.write(text)
    return rows, broken


# ----------------------------------------------------------------------- cli
def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="canary", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("emit", help="print a marker line for a slot")
    p.add_argument("slot")

    p = sub.add_parser("verify", help="is the slot's marker the last line?")
    p.add_argument("slot")
    p.add_argument("file")

    p = sub.add_parser("probe", help="run ONE probe and classify every slot")
    p.add_argument("--manifest", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--model", default="sonnet")
    p.add_argument("--settings", default="")
    p.add_argument("--rows", default="")
    p.add_argument("--evidence", default="")

    args = ap.parse_args(argv)
    if args.cmd == "emit":
        print(emit(args.slot))
        return 0
    if args.cmd == "verify":
        ok, detail = verify(args.slot, args.file)
        print(detail)
        return 0 if ok else 1
    rows, broken = probe(args.manifest, args.dir, args.model, args.settings,
                         args.rows or None, args.evidence or None)
    for row in rows:
        print(row.tsv())
    return 0 if not broken else 2


if __name__ == "__main__":
    sys.exit(main())
