"""Fake interactive client: checkpoint one segment, then exit its fresh successor."""
import json
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import runtime
from token_kit.core.store import Store, read_json

if "features" in sys.argv:
    print("hooks stable true")
    raise SystemExit(0)

signal.alarm(10)
store = Store(Path(os.environ["TOKEN_KIT_TASK"]))
agent = os.environ["TOKEN_KIT_AGENT"]
run = store.agent_path(agent) / "runs" / os.environ["TOKEN_KIT_RUN"]
first = len(list((run.parent).iterdir())) == 1
engine = read_json(run / "runtime.json")["engine"]
transcript = run / "fake-native.jsonl"
size = 200 if first else 20
if engine == "claude":
    rows = [{"type": "assistant", "message": {"id": "m1", "model": "opus", "usage": {
        "input_tokens": size, "output_tokens": 5}}}]
else:
    rows = [{"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": size, "output_tokens": 5, "total_tokens": size + 5},
                "last_token_usage": {"total_tokens": size + 5}}}}]
transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))

def hook(event):
    return runtime.handle(store, agent, run, {"hook_event_name": event, "session_id": run.name,
                                             "transcript_path": str(transcript)})

hook("SessionStart")
result = hook("Stop")
if first:
    assert result.get("decision") == "block", result
    store.checkpoint(agent)
    assert hook("Stop").get("continue") is False
    signal.pause()  # supervisor must stop this old client before spawning another
else:
    assert result == {}, result
    hook("SessionEnd")
