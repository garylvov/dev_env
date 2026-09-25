"""Small CLI projections; authoritative recovery bundles remain untouched."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import shlex

from .core.lifecycle import execution_reconciled
from .core.store import Store


def selected(record, keys):
    return {key: record[key] for key in keys if key in record}


def excerpt(value, limit=160):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def relevant_runs(records):
    relevant = [row for row in records if row.get("status") not in ("exited", "reconciled", "completed")
                or row.get("recovery_pending")
                or (row.get("recovery_to") and row.get("status") != "reconciled")
                or (row.get("recovery_from") and not row.get("recovery_completed")
                    and row.get("status") != "reconciled")
                or Store._run_error_unmarked(row)]
    if not relevant and records:
        relevant = [max(records, key=lambda row: (row.get("created_at", ""), row.get("run_id", "")))]
    return relevant


def run_row(row, directory):
    result = selected(row, ("run_id", "status", "engine", "model", "recovery_from", "recovery_to",
                            "recovery_pending", "recovery_completed", "recovery_status", "halt_kind"))
    result["unresolved_errors"] = Store._run_error_unmarked(row)
    result["path"] = str(directory / "runs" / row["run_id"] / "run.json")
    return result


def worker_row(row, task):
    result = selected(row, ("agent_id", "parent_agent", "phase", "ticket", "generation", "engine", "model",
                            "native_id", "owner_agent", "owner_run", "managed_run_id", "execution_mode", "operations_reconciled"))
    result["path"] = str(task / "agents" / row["agent_id"] / "lifecycle.json")
    return result


def resume_view(bundle, task):
    task = Path(task)
    directory = task / "agents" / bundle["agent_id"]
    result = {key: value for key, value in bundle.items()
              if key not in ("children", "runs", "pending_messages", "trigger_pyramid")}
    result["trigger_pyramid"] = selected(bundle["trigger_pyramid"], ("path", "source"))
    children = bundle.get("children", [])
    result["children"] = [worker_row(row, task) for row in children if not execution_reconciled(row)]
    result["omitted_children"] = dict(Counter(row.get("phase", "unknown") for row in children
                                                if execution_reconciled(row)))
    result["pending_messages"] = []
    for row in bundle.get("pending_messages", []):
        message = selected(row, ("message_id", "created_at", "source", "agent_id", "kind", "ticket"))
        message["text"] = excerpt(row.get("text", ""))
        message["text_truncated"] = message["text"] != row.get("text", "")
        message["path"] = str(directory / "messages" / (row["message_id"] + ".json"))
        result["pending_messages"].append(message)
    runs = bundle.get("runs", [])
    relevant = relevant_runs(runs)
    result["runs"] = [run_row(row, directory) for row in relevant]
    result["omitted_runs"] = len(runs) - len(relevant)
    result["full_command"] = shlex.join(["token-kit", "resume", str(task), "--agent", bundle["agent_id"], "--full"])
    return result


def status_view(bundle, task):
    task = Path(task)
    result = {"task": selected(bundle["task"], ("task_id", "title", "status", "workspace")), "agents": []}
    for entry in bundle["agents"]:
        agent = entry["agent"]
        directory = task / "agents" / agent["agent_id"]
        relevant = relevant_runs(entry["runs"])
        result["agents"].append({
            "agent": selected(agent, ("agent_id", "parent_agent")),
            "worker": worker_row(entry["worker"], task) if entry["worker"] else None,
            "runs": [run_row(row, directory) for row in relevant],
            "omitted_runs": len(entry["runs"]) - len(relevant),
        })
    result["full_command"] = shlex.join(["token-kit", "status", str(task), "--full"])
    return result


def dumps(value):
    """Valid JSON, one row per collection item instead of deep indentation."""
    lines = []
    for key, item in value.items():
        prefix = "  " + json.dumps(key) + ": "
        if isinstance(item, list) and item:
            rendered = "[\n" + ",\n".join("    " + json.dumps(row, ensure_ascii=False) for row in item) + "\n  ]"
        else:
            rendered = json.dumps(item, ensure_ascii=False)
        lines.append(prefix + rendered)
    return "{\n" + ",\n".join(lines) + "\n}"
