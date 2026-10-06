"""Transcript fallback for exec installations that do not run trusted hooks."""
from __future__ import annotations
import json
import re
from pathlib import Path
from .simple_adapters import codex_usage
from .rollover import effective_limit


def fallback_summary(log: Path, home: Path, workspace: Path, view, options, control=None):
    """Bind the owned client's startup session header to exact persisted metadata."""
    with log.open('rb') as stream:
        header = stream.read(65536).decode('utf-8', errors='replace')
    match = re.search(r'^session id: ([0-9a-f-]{36})$', header, re.MULTILINE)
    if not match:
        raise ValueError('Headless rollover cannot identify the exec transcript; no successor launched')
    session = match.group(1)
    candidates = list((home / 'sessions').glob(f'*/*/*/rollout-*{session}.jsonl'))
    if len(candidates) != 1:
        raise ValueError('Headless rollover transcript unavailable or ambiguous; no successor launched')
    with candidates[0].open() as stream:
        row = json.loads(stream.readline(262145))
    meta = row.get('payload', {}) if isinstance(row, dict) else {}
    if not isinstance(meta, dict):
        raise ValueError('Headless rollover transcript metadata is malformed; no successor launched')
    if (row.get('type') != 'session_meta' or meta.get('id') != session
            or meta.get('source') != 'exec' or meta.get('cwd') != str(workspace)
            or any(meta.get(key) for key in ('parent_thread_id', 'parent_session_id', 'agent_path'))):
        raise ValueError('Headless rollover transcript identity mismatch; no successor launched')
    used, window = codex_usage(candidates[0])
    window = options.context_window or window
    if used is None or window is None:
        raise ValueError('Headless rollover token telemetry unavailable; no successor launched')
    from .simple_runtime import rollover_growth
    baseline = None
    with candidates[0].open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
                info = event.get('payload', {}).get('info') or {}
                value = (info.get('last_token_usage') or {}).get('total_tokens')
                if (event.get('type') == 'event_msg' and
                        event.get('payload', {}).get('type') == 'token_count' and
                        type(value) is int and value >= 0):
                    baseline = value
                    break
            except (ValueError, AttributeError, TypeError):
                continue
    guard = control if control is not None else {}
    guard.setdefault('startup_tokens', baseline if baseline is not None else used)
    guard.update(context_tokens=used, telemetry_window=window)
    if not rollover_growth(guard, used, window, effective_limit(options.rollover, window)):
        if guard.get('loop_warning'):
            import sys
            print('Token Kit: ' + guard['loop_warning'], file=sys.stderr)
        return None
    if native_workers_may_have_run(candidates[0]):
        raise ValueError('Headless rollover cannot verify native-worker shutdown without hooks; no successor launched')
    note = (f'Context is at {used / window * 100:.0f}%. Finish summarizing everything into '
            f'{view.output.resolve()} now: update sparse Current state and append History, '
            f'ensure every ask in {view.assignment.resolve()} has a History response. '
            'Save done, next, open asks, decisions and dead ends. Then stop; a fresh session '
            'will continue from these files. Do no further task work in this summary turn.')
    return session, note, used


def summary_argv(argv, session, note):
    boundary = argv.index('--')
    return (*argv[:boundary], 'resume', session, '--', note)


def native_workers_may_have_run(transcript: Path) -> bool:
    """Only explicit pre-execution failures clear a spawn; unknown outcomes refuse."""
    calls = {}
    paths = {}
    with transcript.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get('type') != 'response_item':
                continue
            payload = row.get('payload', {})
            if not isinstance(payload, dict):
                continue
            kind = payload.get('type')
            name = str(payload.get('name', '')).split('.')[-1]
            call = payload.get('call_id')
            if kind == 'function_call' and name == 'spawn_agent':
                if not isinstance(call, str) or call in calls:
                    return True
                calls[call] = False
            elif kind == 'function_call' and name == 'followup_task' and calls:
                # A retry can run a previously failed worker. Its outcome is unknown.
                return True
            elif kind == 'function_call_output' and call in calls:
                calls[call] = False
                output = payload.get('output')
                try:
                    result = json.loads(output) if isinstance(output, str) else output
                except ValueError:
                    result = None
                if isinstance(result, dict):
                    path = result.get('task_name')
                    if isinstance(path, str):
                        paths[path] = call
                    # A tool-level error with no worker identity means spawn itself failed.
                    elif result.get('error') and not result.get('agent_id'):
                        calls[call] = True
            elif kind == 'agent_message' and payload.get('author') in paths:
                call = paths[payload['author']]
                content = payload.get('content', [])
                text = '\n'.join(item.get('text', '') for item in content if isinstance(item, dict))
                # This exact client startup error precedes model execution. Generic
                # "agent errored" messages can occur after work and are insufficient.
                calls[call] = bool(re.search(
                    r'Agent errored: .*"type"\s*:\s*"invalid_request_error".*'
                    r'"message"\s*:\s*"The .* model is not supported when using Codex', text))
    return any(not failed for failed in calls.values())
