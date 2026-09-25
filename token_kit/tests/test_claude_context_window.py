import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core.context_window import claude_window
from token_kit.core.ledger import summarize


class ClaudeContextWindowTests(unittest.TestCase):
    def test_native_model_defaults_and_unknown_aliases(self):
        for model in ("claude-fable-5-1", "claude-fable-5", "claude-sonnet-5",
                      "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5"):
            self.assertEqual(claude_window(model, environ={}), (1_000_000, "documented_default"))
        for model in ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"):
            self.assertEqual(claude_window(model, environ={})[0], 200_000)
        for model in ("fable", "opus", "unknown", "gateway/fable", "claude-opus-99",
                      "gateway/claude-fable-5-1", "claude-fable-5-1[1m]"):
            self.assertEqual(claude_window(model, environ={}), (None, None))

    def test_effective_provider_and_disabled_context_are_conservative(self):
        for env in ({"CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"},
                    {"ANTHROPIC_BASE_URL": "https://gateway.test"},
                    {"CLAUDE_CODE_USE_BEDROCK": "1"},
                    {"CLAUDE_CODE_USE_VERTEX": "true"},
                    {"CLAUDE_CODE_USE_FOUNDRY": "1"},
                    {"CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1"}):
            self.assertEqual(claude_window("claude-fable-5-1", environ=env)[0], 200_000)
        self.assertEqual(claude_window("claude-fable-5-1", environ={
            "CLAUDE_CODE_USE_BEDROCK": "0", "ANTHROPIC_BASE_URL": "https://api.anthropic.com/"})[0], 1_000_000)

    def test_actual_report_wins_and_invalid_window_is_not_a_default(self):
        self.assertEqual(claude_window("custom", reported=320_000, environ={}), (320_000, "reported"))
        for invalid in (True, False, 0, -1, "1000000", 1.5):
            with self.assertRaises(ValueError):
                claude_window("claude-fable-5-1", reported=invalid, environ={})

    def test_claude_declared_window_follows_compaction_and_custom_id_rules(self):
        env = {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1"}
        self.assertEqual(claude_window("claude-fable-5-1", environ=env),
                         (100_000, "env_override:CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
        self.assertEqual(claude_window("custom", environ=env)[0], 100_000)
        self.assertEqual(claude_window("claude-opus-4-8[1m]", environ=env),
                         (100_000, "env_override:CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
        self.assertEqual(claude_window("claude-opus-4-8[1m]", environ={
            **env, "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"})[0], 100_000)
        self.assertIsNone(claude_window("custom[1m]", environ=env)[0])
        self.assertEqual(claude_window("custom[1m]", environ={**env, "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"})[0], 100_000)
        self.assertEqual(claude_window("claude-fable-5-1", environ={
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000"})[0], 1_000_000)
        for invalid in ("0", "-1", "NaN", "100k", ""):
            with self.assertRaises(ValueError):
                claude_window("claude-fable-5-1", environ={**env, "CLAUDE_CODE_MAX_CONTEXT_TOKENS": invalid})

    def test_cached_model_and_environment_changes_do_not_retain_window(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path, cache = Path(directory) / "transcript.jsonl", Path(directory) / "cache.json"
            def append(model, identifier, usage=True, **fields):
                message = dict(model=model, id=identifier, **fields)
                if usage:
                    message["usage"] = dict(input_tokens=100, output_tokens=3,
                                             cache_read_input_tokens=50, cache_creation_input_tokens=20)
                with path.open("a") as stream:
                    stream.write(json.dumps(dict(type="assistant", message=message)) + "\n")
            append("claude-fable-5-1", "one")
            sample = summarize(path, "claude", cache=cache)
            self.assertEqual(sample["context_tokens"], 173)
            self.assertEqual(sample["context_window"], 1_000_000)
            self.assertEqual(sample["context_window_source"], "documented_default")
            with patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"}):
                self.assertEqual(summarize(path, "claude", cache=cache)["context_window"], 200_000)
            append("custom", "two", usage=False, context_window_size=1_000_000)
            sample = summarize(path, "claude", cache=cache)
            self.assertIsNone(sample["context_window"])
            self.assertIsNone(sample["context_tokens"])
            append("claude-sonnet-4-6", "three")
            sample = summarize(path, "claude", cache=cache)
            self.assertEqual(sample["context_window"], 200_000)
            self.assertEqual(sum(m["total"] for m in sample["models"].values()), 346)


if __name__ == "__main__":
    unittest.main()
