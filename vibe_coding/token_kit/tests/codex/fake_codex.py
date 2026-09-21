"""A fake `codex` that speaks the app-server protocol from a fixture.

Invoked exactly as the real one is -- `<launcher> app-server --stdio` -- so the
tests exercise the dispatcher's real argv, env and wire handling offline.

The loop is select()-driven rather than `for line in sys.stdin`, because a
STEER has to be read while a turn is still in flight; a blocking read could
only ever model turns that finish before the next message arrives.

Env knobs
    FAKE_CODEX_RECORD    path; line 1 is {"argv":..,"env":{..}}, then one line
                         per JSON-RPC message received.
    FAKE_CODEX_SCENARIO  ok | fail | authfail | silent | slow | quota
    FAKE_CODEX_ANSWER    the agent message text (default PROVEN)
    FAKE_CODEX_FINISH    `slow`: the turn completes when this path exists.
    FAKE_CODEX_TURN_MAX_S  `slow`: hard stop, so a wedged test cannot hang.

`slow` echoes every steered text into the final answer, which is how a test
can tell a message that reached the live turn from one that did not.
"""

from __future__ import annotations

import json
import os
import select
import sys
import time

RECORD = os.environ.get("FAKE_CODEX_RECORD")
SCENARIO = os.environ.get("FAKE_CODEX_SCENARIO", "ok")
ANSWER = os.environ.get("FAKE_CODEX_ANSWER", "PROVEN")
FINISH = os.environ.get("FAKE_CODEX_FINISH")
TURN_MAX_S = float(os.environ.get("FAKE_CODEX_TURN_MAX_S", "60"))
THREAD_ID = "th-fake-0001"
TURN_ID = "tn-fake-0001"

#: The real 0.153.4 vocabulary (v2/ErrorNotification.json, CodexErrorInfo).
QUOTA_ERROR = {"message": "Usage limit reached. You've reached your usage limit.",
               "codexErrorInfo": "usageLimitExceeded"}


def record(obj: object) -> None:
    if not RECORD:
        return
    with open(RECORD, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj) + "\n")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class Server:
    def __init__(self) -> None:
        self.turns = 0
        self.active_turn = ""          # "" means no turn in flight
        self.turn_req_id = None
        self.turn_deadline = 0.0
        self.steered: list[str] = []

    def next_turn_id(self) -> str:
        self.turns += 1
        return TURN_ID if self.turns == 1 else f"tn-fake-{self.turns:04d}"

    # -- handlers --------------------------------------------------------
    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            if SCENARIO == "authfail":
                emit({"id": msg_id, "error": {"code": -32000,
                                              "message": "not logged in; run codex login"}})
                raise SystemExit(0)
            emit({"id": msg_id, "result": {"userAgent": "fake/0.0", "codexHome": "/tmp/fake",
                                           "platformFamily": "unix", "platformOs": "linux"}})
        elif method == "thread/start":
            emit({"id": msg_id, "result": {
                "thread": {"id": THREAD_ID},
                "model": params.get("model"),
                "reasoningEffort": params.get("effort"),
                "cwd": params.get("cwd"),
                "sandbox": {"type": "readOnly"},
                "approvalPolicy": params.get("approvalPolicy"),
            }})
        elif method == "thread/resume":
            emit({"id": msg_id, "result": {"thread": {"id": params.get("threadId")},
                                           "model": params.get("model")}})
        elif method == "turn/start":
            self.start_turn(msg_id, params)
        elif method == "turn/steer":
            self.steer(msg_id, params)
        elif method == "turn/interrupt":
            emit({"id": msg_id, "result": {}})
            if self.active_turn:
                self.finish_turn(status="aborted")
        elif msg_id is not None:
            emit({"id": msg_id, "result": {}})

    def start_turn(self, msg_id, params: dict) -> None:
        thread_id = params.get("threadId")
        turn_id = self.next_turn_id()
        self.active_turn = turn_id
        self.turn_req_id = msg_id
        self.thread_id = thread_id
        self.steered = []
        self.turn_deadline = time.monotonic() + TURN_MAX_S
        emit({"method": "turn/started",
              "params": {"threadId": thread_id,
                         "turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        if SCENARIO == "quota":
            emit({"method": "error",
                  "params": {"threadId": thread_id, "turnId": turn_id,
                             "willRetry": False, "error": QUOTA_ERROR}})
            emit({"method": "turn/completed",
                  "params": {"threadId": thread_id,
                             "turn": {"id": turn_id, "status": "failed", "items": [],
                                      "error": QUOTA_ERROR}}})
            emit({"id": msg_id, "result": {"turn": {"id": turn_id, "status": "failed",
                                                    "items": []}}})
            self.active_turn = ""
            return
        if SCENARIO == "fail":
            self.finish_turn(status="failed", error={"message": "model refused"})
            return
        if SCENARIO != "slow":
            self.finish_turn(status="completed")

    def steer(self, msg_id, params: dict) -> None:
        """The real guard, reproduced: an exact active-turn id or nothing."""
        expected = params.get("expectedTurnId")
        if expected is None:
            emit({"id": msg_id, "error": {"code": -32600,
                                          "message": "Invalid request: missing field "
                                                     "`expectedTurnId`"}})
            return
        if expected == "":
            emit({"id": msg_id, "error": {"code": -32600,
                                          "message": "expectedTurnId must not be empty"}})
            return
        if not self.active_turn:
            emit({"id": msg_id, "error": {"code": -32600,
                                          "message": "no active turn to steer"}})
            return
        if expected != self.active_turn:
            emit({"id": msg_id,
                  "error": {"code": -32600,
                            "message": f"expectedTurnId {expected} does not match the active "
                                       f"turn {self.active_turn}"}})
            return
        for item in params.get("input") or []:
            if item.get("type") == "text":
                self.steered.append(item.get("text", ""))
        emit({"id": msg_id, "result": {"turnId": self.active_turn}})

    def finish_turn(self, status: str, error: dict | None = None) -> None:
        turn_id, msg_id = self.active_turn, self.turn_req_id
        thread_id = getattr(self, "thread_id", THREAD_ID)
        self.active_turn = ""
        self.turn_req_id = None
        if status != "completed":
            turn = {"id": turn_id, "status": status, "items": [],
                    "error": error or {"message": status}}
            emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
            emit({"id": msg_id, "result": {"turn": {"id": turn_id, "status": status,
                                                    "items": []}}})
            return
        text = ANSWER
        if self.steered:
            text = ANSWER + " " + " ".join(self.steered)
        item = {"type": "agentMessage", "id": f"it-{turn_id}", "text": text}
        emit({"method": "item/completed",
              "params": {"threadId": thread_id, "turnId": turn_id,
                         "completedAtMs": 0, "item": item}})
        emit({"method": "thread/tokenUsage/updated",
              "params": {"threadId": thread_id, "turnId": turn_id,
                         "tokenUsage": {"last": {"inputTokens": 11, "outputTokens": 7},
                                        "total": {"inputTokens": 11, "outputTokens": 7}}}})
        emit({"method": "turn/completed",
              "params": {"threadId": thread_id,
                         "turn": {"id": turn_id, "status": "completed", "items": [item]}}})
        emit({"id": msg_id, "result": {"turn": {"id": turn_id, "status": "completed",
                                                "items": [item]}}})


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

    server = Server()
    buf = ""
    while True:
        if server.active_turn and SCENARIO == "slow":
            done = (FINISH and os.path.exists(FINISH)) or time.monotonic() > server.turn_deadline
            if done:
                server.finish_turn(status="completed")
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not ready:
            continue
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            return 0
        buf += chunk.decode("utf-8", "replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            record(msg)
            server.handle(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
