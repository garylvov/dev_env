"""Guard for the one human time format, and for the machine formats it must
NOT have touched.

Cases:
  1 the format itself, including the three that a naive %I/%p gets wrong:
    midnight, noon, and an hour with a leading zero
  2 human() is date plus clock, in that order
  3 the MACHINE formats are unchanged: the supervisor's ledger rows, the
    respawn registry, the task index and the codex event rows are all ISO with
    an offset, because they are parsed and sorted
"""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit import timefmt  # noqa: E402

ISO_ROW = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


class HumanFormat(unittest.TestCase):
    def test_1_the_cases_a_naive_format_gets_wrong(self):
        self.assertEqual("12:05am", timefmt.human_short(at("2026-09-21T00:05")))
        self.assertEqual("12:30pm", timefmt.human_short(at("2026-09-21T12:30")))
        self.assertEqual("9:07am", timefmt.human_short(at("2026-09-21T09:07")))
        self.assertEqual("10:42pm", timefmt.human_short(at("2026-09-21T22:42")))
        self.assertEqual("11:59pm", timefmt.human_short(at("2026-09-21T23:59")))

    def test_1b_no_leading_zero_on_the_hour_and_lowercase_meridiem(self):
        text = timefmt.human_short(at("2026-09-21T09:07"))
        self.assertFalse(text.startswith("0"))
        self.assertEqual(text, text.lower())

    def test_2_human_is_the_day_then_the_clock(self):
        when = at("2026-09-21T22:42")
        self.assertEqual("Mon 21 Sep 2026", timefmt.human_day(when))
        self.assertEqual("Mon 21 Sep 2026, 10:42pm", timefmt.human(when))
        self.assertEqual(f"{timefmt.human_day(when)}, {timefmt.human_short(when)}",
                         timefmt.human(when))

    def test_2b_a_posix_timestamp_is_accepted_too(self):
        when = datetime.now().astimezone()
        self.assertEqual(timefmt.human(when), timefmt.human(when.timestamp()))

    def test_2c_iso_round_trips(self):
        text = timefmt.iso(at("2026-09-21T22:42:11-04:00"))
        self.assertTrue(ISO_ROW.match(text), text)
        self.assertEqual(at("2026-09-21T22:42:11-04:00"), timefmt.parse_iso(text))
        self.assertIsNone(timefmt.parse_iso("not a time"))


class MachineFormatsUnchanged(unittest.TestCase):
    """A human column must never have leaked into a row something PARSES."""

    def test_3_the_supervisor_ledger_row_is_iso_with_an_offset(self):
        from token_kit.supervisor import main as sup
        self.assertTrue(ISO_ROW.match(sup.stamp()), sup.stamp())

    def test_3b_the_respawn_registry_row_is_iso(self):
        from token_kit.respawn import reader
        self.assertIsNotNone(timefmt.parse_iso(reader.stamp()))
        self.assertIn("T", reader.stamp())

    def test_3c_the_task_index_row_is_iso(self):
        from token_kit import task as task_mod
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "tasks.tsv"
            real = task_mod.index_path
            task_mod.index_path = lambda: index
            try:
                task_mod.index_append("new", Path(tmp), "a title")
            finally:
                task_mod.index_path = real
            row = index.read_text().rstrip("\n").split("\t")
        self.assertTrue(ISO_ROW.match(row[0]), row[0])
        self.assertEqual(["new", str(Path(tmp)), "a title"], row[1:])


if __name__ == "__main__":
    unittest.main()
