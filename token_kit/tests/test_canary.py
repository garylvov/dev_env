"""Guard for the instruction canary, ported from the bash tool's selftest.

Nothing here calls a model: `claude` is a fake script the test writes, so the
three outcomes can be driven on purpose -- which is the only way to prove that
PROBE_BROKEN is never quietly reported as NOT_LOADED.

Cases:
  1 emit produces the plain marker line, and verify accepts it at EOF
  2 a marker hidden in an HTML comment is NOT accepted -- it is stripped before
    the file reaches the model, so "on disk" is not "in context" (measured)
  3 a clean run whose answer carries the token is LOADED
  4 a clean run whose answer is NONE is NOT_LOADED
  5..10 every unknown is PROBE_BROKEN: non-zero exit, non-JSON, is_error,
    subtype != success, empty result, a wrong liveness nonce, and a missing
    answer line for the slot
 11 rows are APPENDED to the rows file, never rewritten
 12 the guard can fail: classify with its liveness check removed calls a run
    with the wrong nonce NOT_LOADED
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit import canary  # noqa: E402

SLOT = canary.Slot("s1", "token", "", "ABCDEF0123456789")


def fake_run(payload, rc=0, raw=None):
    """A runner that answers with whatever `payload` says, ignoring the prompt."""
    def run(prompt, model, directory, settings=""):
        return rc, (raw if raw is not None else json.dumps(payload)), ""
    return run


def ok_payload(result):
    return {"is_error": False, "subtype": "success", "result": result,
            "usage": {"input_tokens": 1}}


class Markers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "CLAUDE.md"

    def tearDown(self):
        self.tmp.cleanup()

    # 1
    def test_emit_then_verify(self):
        line = canary.emit("s1", token="ABCDEF0123456789", when="2026-01-01")
        self.assertTrue(line.startswith(canary.MARK))
        self.assertIn("slot=s1", line)
        self.file.write_text("some instructions\n" + line + "\n")
        ok, detail = canary.verify("s1", self.file)
        self.assertTrue(ok, detail)
        ok, detail = canary.verify("other", self.file)
        self.assertFalse(ok)
        self.assertIn("NO_MARKER_AT_EOF", detail)

    # 2 -- the measured fact, as a live refusal
    def test_a_commented_marker_is_refused_by_name(self):
        line = canary.emit("s1", token="ABCDEF0123456789", when="2026-01-01")
        self.file.write_text(f"some instructions\n<!-- {line} -->\n")
        ok, detail = canary.verify("s1", self.file)
        self.assertFalse(ok, "a commented marker never reaches the model")
        self.assertIn("COMMENTED_MARKER", detail)

    def test_a_missing_file_is_named_not_crashed(self):
        ok, detail = canary.verify("s1", self.file.parent / "gone.md")
        self.assertFalse(ok)
        self.assertIn("MISSING_FILE", detail)


class Outcomes(unittest.TestCase):
    NONCE = "DEADBEEF"

    def health(self, rc, stdout):
        return canary.run_health(rc, stdout, self.NONCE)

    def classify(self, result, rc=0):
        broken, text, _ = self.health(rc, json.dumps(ok_payload(result)))
        return canary.classify(SLOT, broken, text)

    # 3
    def test_the_witness_comes_back_loaded(self):
        status, detail = self.classify(f"ALIVE {self.NONCE}\ns1=ABCDEF0123456789")
        self.assertEqual(canary.LOADED, status)
        self.assertIn("witness=ABCDEF0123456789", detail)

    # 4
    def test_a_clean_run_with_none_is_not_loaded(self):
        status, detail = self.classify(f"ALIVE {self.NONCE}\ns1=NONE")
        self.assertEqual(canary.NOT_LOADED, status)
        self.assertIn("answered=NONE", detail)

    # 5..10
    def test_every_unknown_is_probe_broken(self):
        cases = {
            "exit_rc=1": (1, json.dumps(ok_payload(f"ALIVE {self.NONCE}\ns1=x"))),
            "no_json_on_stdout": (0, "not json at all"),
            "bad_json": (0, "{oops"),
            "is_error": (0, json.dumps({"is_error": True, "subtype": "success",
                                        "result": "x"})),
            "subtype=error_during_execution": (
                0, json.dumps({"subtype": "error_during_execution", "result": "x"})),
            "empty_result": (0, json.dumps(ok_payload(""))),
            "liveness_nonce_absent": (
                0, json.dumps(ok_payload("ALIVE SOMETHINGELSE\ns1=ABCDEF0123456789"))),
        }
        for expected, (rc, stdout) in cases.items():
            broken, text, _ = self.health(rc, stdout)
            self.assertEqual(expected, broken, f"health misread {expected}")
            status, detail = canary.classify(SLOT, broken, text)
            self.assertEqual(canary.PROBE_BROKEN, status,
                             f"{expected} was not forced to PROBE_BROKEN")
            self.assertEqual(expected, detail)

    def test_a_missing_answer_line_is_probe_broken(self):
        status, detail = self.classify(f"ALIVE {self.NONCE}\nsomethingelse=1")
        self.assertEqual(canary.PROBE_BROKEN, status)
        self.assertEqual("no_answer_line_for_slot", detail)

    # 12 -- the guard shown to fail with the liveness check removed
    def test_without_the_liveness_check_a_broken_run_reads_as_not_loaded(self):
        stdout = json.dumps(ok_payload(f"ALIVE WRONGNONCE\ns1=NONE"))
        broken, text, _ = self.health(0, stdout)
        self.assertEqual(canary.PROBE_BROKEN,
                         canary.classify(SLOT, broken, text)[0])
        # the same run, classified as if the nonce had never been checked
        status, _ = canary.classify(SLOT, "", json.loads(stdout)["result"])
        self.assertEqual(canary.NOT_LOADED, status,
                         "dropping the liveness check is exactly the defect "
                         "that makes this tool worthless")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "slots.tsv"
        self.manifest.write_text(
            "# slot\tmode\tpath\texpect\tquestion\n"
            f"s1\ttoken\t{self.root / 'CLAUDE.md'}\tABCDEF0123456789\t\n"
            f"s2\twitness\t{self.root / 'AGENTS.md'}\tblue\twhat colour is the sky\n")
        (self.root / "CLAUDE.md").write_text("x\n")

    def tearDown(self):
        self.tmp.cleanup()

    # 11
    def test_rows_are_appended_and_both_modes_classified(self):
        rows_file = self.root / "rows.tsv"
        result = "ALIVE NONCE1\ns1=ABCDEF0123456789\ns2=blue"
        rows, broken = canary.probe(
            self.manifest, str(self.root), rows_file=rows_file,
            evidence=self.root / "ev", nonce="NONCE1",
            runner=fake_run(ok_payload(result)))
        self.assertEqual("", broken)
        self.assertEqual([canary.LOADED, canary.LOADED], [r.status for r in rows])
        self.assertIn("file_PRESENT", rows[0].detail)
        self.assertIn("file_ABSENT", rows[1].detail)
        first = rows_file.read_text()
        canary.probe(self.manifest, str(self.root), rows_file=rows_file,
                     evidence=self.root / "ev", nonce="NONCE1",
                     runner=fake_run(ok_payload(result)))
        self.assertTrue(rows_file.read_text().startswith(first),
                        "the rows file was rewritten instead of appended to")
        self.assertEqual(4, len(rows_file.read_text().splitlines()))

    def test_a_broken_run_marks_every_slot_broken(self):
        rows, broken = canary.probe(
            self.manifest, str(self.root), evidence=self.root / "ev",
            nonce="NONCE1", runner=fake_run(None, rc=7, raw=""))
        self.assertEqual("exit_rc=7", broken)
        self.assertEqual({canary.PROBE_BROKEN}, {r.status for r in rows})


if __name__ == "__main__":
    unittest.main()
