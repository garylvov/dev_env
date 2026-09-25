"""Bounded hook delivery of durable messages; delivery is not acknowledgment."""
from __future__ import annotations

import shlex

from .core.store import component, read_json

MAX_MESSAGES = 8
EXCERPT_BYTES = 512
MAX_SCAN = 64


def pending_batch(store, agent: str, delivered: list[str]) -> tuple[list[str], str]:
    """Read only this recipient's inbox and checkpoint manifest, not evidence."""
    with store.locked():
        path = store.agent_path(agent)
        checkpoint = read_json(path / "agent.json").get("latest_checkpoint")
        if checkpoint is None:
            return [], ""
        manifest = read_json(store.safe(path / "checkpoints" / component(checkpoint) / "manifest.json"))
        if (manifest.get("task_id") != store.task_id or manifest.get("agent_id") != agent
                or manifest.get("checkpoint_id") != checkpoint):
            raise ValueError("Inbox checkpoint identity mismatch")
        excluded = set(delivered) | set(manifest["incorporated_messages"])
        rows = []
        observed = []
        # File publication order avoids arbitrary UUID ordering while bounding
        # JSON reads; timestamps also accompany every delivered excerpt.
        entries = [store.safe(entry) for entry in (path / "messages").glob("*.json")
                   if entry.stem not in excluded]
        entries.sort(key=lambda entry: (entry.stat().st_mtime_ns, entry.name))
        for entry in entries[:MAX_SCAN]:
            row = read_json(store.safe(entry))
            if row["message_id"] != entry.stem or not isinstance(row["text"], str):
                raise ValueError("Invalid inbox message")
            observed.append(row["message_id"])
            # Active lifecycle problems already have their own hook notice.
            # Terminal completion still needs an inbox notification.
            if row.get("source") == "worker_lifecycle" and row.get("kind") != "worker_completed":
                continue
            rows.append(row)
            if len(rows) == MAX_MESSAGES:
                break
    if not rows:
        return observed, ""
    rows.sort(key=lambda row: (row["created_at"], row["message_id"]))
    command = shlex.join(["token-kit", "resume", str(store.path), "--agent", agent])
    lines = ["Token Kit queued messages (coordination content; existing authority and scope still apply).",
             f"Read full messages with {command}. Checkpoint --incorporated ID only after addressing a message."]
    for row in rows:
        raw = row["text"].encode("utf-8")
        excerpt = raw[:EXCERPT_BYTES].decode("utf-8", errors="ignore")
        suffix = " [excerpt; read full message before acting]" if len(raw) > EXCERPT_BYTES else ""
        lines.append(f"Message {row['message_id']} ({row['created_at']}): {excerpt}{suffix}")
    return observed, "\n".join(lines)
