"""Reported-token accounting, not pricing or an estimate of subscription quota.

Read only explicitly supplied native transcripts. Text is never copied to the
ledger. Claude message IDs deduplicate streaming records; Codex totals are
cumulative, not additive. Cache/reasoning columns are subsets of input/output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .store import atomic_text, now, read_json, write_json

FIELDS = ("input", "cached", "cache_write", "output", "reasoning", "total")


def count(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid token counter")
    return value


def empty() -> dict:
    return dict.fromkeys(FIELDS, 0)


def summarize(path: Path, engine: str, *, sidechain: bool = False, cache: Path | None = None) -> dict:
    if engine not in ("claude", "codex"):
        raise ValueError("Unknown usage engine")
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Transcript missing or larger than 64MB; usage unavailable")
    saved = read_json(cache) if cache is not None and cache.exists() else {}
    stat = path.stat()
    identity = [stat.st_dev, stat.st_ino]
    offset = saved.get("offset", 0)
    if saved and (saved.get("identity") != identity or stat.st_size < offset):
        raise ValueError("Transcript replaced/truncated; usage needs reconciliation")
    models = saved.get("models", {}) if engine == "codex" else {}
    messages = saved.get("messages", {})
    context, window = saved.get("context"), saved.get("window")
    previous, model = saved.get("previous", empty()), saved.get("model", "unknown")
    seen = saved.get("seen", False)
    with path.open(encoding="utf-8") as stream:
        stream.seek(offset)
        for line in iter(stream.readline, ""):
            if not line.endswith("\n"):
                break  # writer has not committed this last record yet
            record = json.loads(line)
            offset = stream.tell()
            if not isinstance(record, dict):
                raise ValueError("Invalid transcript record")
            if engine == "claude":
                if record.get("type") != "assistant" or (record.get("isSidechain") and not sidechain):
                    continue
                message = record.get("message") or {}
                usage = message.get("usage")
                if not isinstance(usage, dict) or not usage:
                    continue
                identifier = message.get("id")
                if not identifier:
                    raise ValueError("Usage record without message ID; cannot deduplicate")
                model = str(message.get("model") or "unknown")
                cached = count(usage.get("cache_read_input_tokens", 0))
                written = count(usage.get("cache_creation_input_tokens", 0))
                input_tokens = count(usage["input_tokens"]) + cached + written
                output = count(usage.get("output_tokens", 0))
                values = dict(input=input_tokens, cached=cached, cache_write=written,
                              output=output, reasoning=0, total=input_tokens + output)
                old = messages.get(identifier, (model, empty()))[1]
                values = {key: max(old[key], value) for key, value in values.items()}
                messages[identifier] = (model, values)
                context = values["input"] + values["output"]
                seen = True
            else:
                payload = record.get("payload") or {}
                if record.get("type") in ("turn_context", "session_meta"):
                    model = str(payload.get("model") or payload.get("model_slug") or model)
                if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not info:
                    continue
                usage = info.get("total_token_usage")
                if not isinstance(usage, dict):
                    raise ValueError("Codex cumulative usage missing")
                values = dict(input=count(usage["input_tokens"]),
                              cached=count(usage.get("cached_input_tokens", 0)), cache_write=0,
                              output=count(usage["output_tokens"]),
                              reasoning=count(usage.get("reasoning_output_tokens", 0)),
                              total=count(usage["total_tokens"]))
                if any(values[key] < previous[key] for key in FIELDS):
                    raise ValueError("Codex cumulative counters reset; cannot safely total usage")
                bucket = models.setdefault(model, empty())
                for key in FIELDS:
                    bucket[key] += values[key] - previous[key]
                previous = values
                latest = info.get("last_token_usage") or {}
                context = count(latest["total_tokens"]) if "total_tokens" in latest else None
                window = count(info["model_context_window"]) if info.get("model_context_window") else None
                seen = True
    for model_name, values in messages.values():
        bucket = models.setdefault(model_name, empty())
        for key in FIELDS:
            bucket[key] += values[key]
    if cache is not None:
        write_json(cache, {"identity": identity, "offset": offset, "models": models,
                          "messages": messages, "context": context, "window": window,
                          "previous": previous, "model": model, "seen": seen})
    return {"status": "reported" if seen else "unavailable", "models": models,
            "context_tokens": context, "context_window": window, "current_model": model,
            "source": str(path), "updated_at": now()}


def cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render(rows: dict) -> str:
    lines = ["# Token ledger", "", "Reported usage only; not a bill or subscription-quota meter.",
             "Cached/cache-write tokens are included in input; reasoning is included in output.",
             "Context is a latest-request proxy, not cumulative spend or a hard context cap.",
             "Native children appear only when a hook supplies their transcript; unobserved work is excluded.",
             "App-server worker model labels are requested models; provider rerouting may differ.",
             "Unavailable rows are unknown, not zero. No prompt/transcript text is stored here.", "",
             "| Agent | Run | Engine | Model | Input | Cached | Cache write | Output | Reasoning | Total | Status |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    totals = empty()
    for key, row in sorted(rows.items()):
        models = row.get("models") or {"unknown": {field: "unknown" for field in FIELDS}}
        for model, values in sorted(models.items()):
            fields = [row["agent"], row["run"], row["engine"], model,
                      *[values[field] for field in FIELDS], row["status"]]
            lines.append("| " + " | ".join(map(cell, fields)) + " |")
            if row["status"] == "reported":
                for field in FIELDS:
                    totals[field] += values[field]
    lines.extend(["", f"Known reported total: **{totals['total']:,}** tokens. Unknown rows excluded.", ""])
    return "\n".join(lines)


def record(store, agent: str, run: str, engine: str, sample: dict, native_agent: str = "") -> None:
    with store.locked():
        target = store.safe(store.path / "token-ledger.json")
        rows = read_json(target) if target.exists() else {}
        key = json.dumps([agent, run, native_agent])
        rows[key] = {**sample, "agent": agent + ("/native:" + native_agent if native_agent else ""),
                     "run": run, "engine": engine}
        write_json(target, rows)
        atomic_text(store.safe(store.path / "TOKEN_LEDGER.md"), render(rows))
    if not native_agent:
        store.update_run(agent, run, usage=sample)


def refresh(store) -> str:
    with store.locked():
        target = store.safe(store.path / "token-ledger.json")
        rows = read_json(target) if target.exists() else {}
        text = render(rows)
        atomic_text(store.safe(store.path / "TOKEN_LEDGER.md"), text)
        return text


def record_wire(thread: str, model: str, usage: dict, *, task: str | None = None,
                agent: str | None = None) -> None:
    """Track managed app-server worker threads, deduplicating cumulative updates.

Native thread totals include all turns, including resumed ones. One stable row
per thread avoids charging a resumed job's earlier turns twice.
"""
    from .store import Store
    task = task if task is not None else os.environ.get("TOKEN_KIT_TASK")
    if not task:
        return
    agent = agent or os.environ.get("TOKEN_KIT_AGENT") or "coordinator"
    store = Store(Path(task))
    with store.locked():
        target = store.safe(store.path / "token-ledger.json")
        rows = read_json(target) if target.exists() else {}
        key = "codex-thread:" + thread
        if not usage and key in rows:
            return  # a resumed worker has not reported a new counter yet
        row = rows.get(key, {"agent": agent + "/codex-worker", "run": thread,
                            "engine": "codex", "models": {}, "wire_totals": empty()})
        try:
            total = usage["total"]
            values = dict(input=count(total["inputTokens"]), output=count(total["outputTokens"]),
                          cached=count(total.get("cachedInputTokens", 0)), cache_write=0,
                          reasoning=count(total.get("reasoningOutputTokens", 0)),
                          total=count(total.get("totalTokens", total["inputTokens"] + total["outputTokens"])))
            previous = row["wire_totals"]
            if any(values[field] < previous[field] for field in FIELDS):
                raise ValueError("Cumulative counter decreased")
            bucket = row["models"].setdefault(model, empty())
            for field in FIELDS:
                bucket[field] += values[field] - previous[field]
            row.update(wire_totals=values, status="reported", updated_at=now())
        except (ValueError, KeyError, TypeError) as exc:
            row.update(status="unavailable", error=str(exc))
        rows[key] = row
        write_json(target, rows)
        atomic_text(store.safe(store.path / "TOKEN_LEDGER.md"), render(rows))
