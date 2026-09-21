"""A fake `codex` that speaks the app-server protocol from a fixture.

Invoked exactly as the real one is -- `<launcher> app-server --stdio` -- so the
tests exercise the dispatcher's real argv, env and wire handling offline.

Env knobs
    FAKE_CODEX_RECORD    path; line 1 is {"argv":..,"env":{..}}, then one line
                         per JSON-RPC message received.
    FAKE_CODEX_SCENARIO  ok | fail | authfail | silent
    FAKE_CODEX_ANSWER    the agent message text (default PROVEN)
"""

from __future__ import annotations

import json
import os
import sys

RECORD = os.environ.get("FAKE_CODEX_RECORD")
SCENARIO = os.environ.get("FAKE_CODEX_SCENARIO", "ok")
ANSWER = os.environ.get("FAKE_CODEX_ANSWER", "PROVEN")
THREAD_ID = "th-fake-0001"
TURN_ID = "tn-fake-0001"


def record(obj: object) -> None:
    if not RECORD:
        return
    with open(RECORD, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj) + "\n")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    record({"argv": sys.argv[1:],
            "env": {k: os.environ.get(k, "")
                    for k in ("TOKIO_WORKER_THREADS", "CODEX_HOME", "RAYON_NUM_THREADS")}})
    if "app-server" not in sys.argv[1:]:
        sys.stderr.write("fake codex: expected the app-server subcommand\n")
        return 2
    if SCENARIO == "silent":
        # Models the measured TOKIO_WORKER_THREADS<=4 trap: rc 0, no output.
        return 0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        record(msg)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            if SCENARIO == "authfail":
                emit({"id": msg_id, "error": {"code": -32000,
                                              "message": "not logged in; run codex login"}})
                return 0
            emit({"id": msg_id, "result": {"userAgent": "fake/0.0", "codexHome": "/tmp/fake",
                                           "platformFamily": "unix", "platformOs": "linux"}})
        elif method == "thread/start":
            params = msg.get("params") or {}
            emit({"id": msg_id, "result": {
                "thread": {"id": THREAD_ID},
                "model": params.get("model"),
                "reasoningEffort": params.get("effort"),
                "cwd": params.get("cwd"),
                "sandbox": {"type": "readOnly"},
                "approvalPolicy": params.get("approvalPolicy"),
            }})
        elif method == "thread/resume":
            params = msg.get("params") or {}
            emit({"id": msg_id, "result": {"thread": {"id": params.get("threadId")},
                                           "model": params.get("model")}})
        elif method == "turn/start":
            params = msg.get("params") or {}
            thread_id = params.get("threadId")
            emit({"method": "turn/started",
                  "params": {"threadId": thread_id,
                             "turn": {"id": TURN_ID, "status": "inProgress", "items": []}}})
            if SCENARIO == "fail":
                emit({"method": "turn/completed",
                      "params": {"threadId": thread_id,
                                 "turn": {"id": TURN_ID, "status": "failed", "items": [],
                                          "error": {"message": "model refused"}}}})
                emit({"id": msg_id, "result": {"turn": {"id": TURN_ID, "status": "failed",
                                                        "items": []}}})
                continue
            item = {"type": "agentMessage", "id": "it-1", "text": ANSWER}
            emit({"method": "item/completed",
                  "params": {"threadId": thread_id, "turnId": TURN_ID,
                             "completedAtMs": 0, "item": item}})
            emit({"method": "thread/tokenUsage/updated",
                  "params": {"threadId": thread_id, "turnId": TURN_ID,
                             "tokenUsage": {"last": {"inputTokens": 11, "outputTokens": 7},
                                            "total": {"inputTokens": 11, "outputTokens": 7}}}})
            emit({"method": "turn/completed",
                  "params": {"threadId": thread_id,
                             "turn": {"id": TURN_ID, "status": "completed", "items": [item]}}})
            emit({"id": msg_id, "result": {"turn": {"id": TURN_ID, "status": "completed",
                                                    "items": [item]}}})
        elif msg_id is not None:
            emit({"id": msg_id, "result": {}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
