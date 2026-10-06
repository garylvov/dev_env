"""Transcript fallback for exec installations that do not run trusted hooks."""
from __future__ import annotations
import json
import re
from pathlib import Path
from .simple_adapters import codex_usage
from .rollover import effective_limit


def fallback_summary(log: Path, home: Path, workspace: Path, view, options):
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
    meta = row.get('payload', {})
    if (row.get('type') != 'session_meta' or meta.get('id') != session
            or meta.get('source') != 'exec' or meta.get('cwd') != str(workspace)
            or any(meta.get(key) for key in ('parent_thread_id', 'parent_session_id', 'agent_path'))):
        raise ValueError('Headless rollover transcript identity mismatch; no successor launched')
    used, window = codex_usage(candidates[0])
    window = options.context_window or window
    if used is None or window is None:
        raise ValueError('Headless rollover token telemetry unavailable; no successor launched')
    if used < effective_limit(options.rollover, window):
        return None
    note = (f'Context is at {used / window * 100:.0f}%. Finish summarizing everything into '
            f'{view.output.resolve()} now: update sparse Current state and append History, '
            f'ensure every ask in {view.assignment.resolve()} has a History response. '
            'Save done, next, open asks, decisions and dead ends. Then stop; a fresh session '
            'will continue from these files. Do no further task work in this summary turn.')
    return session, note, used


def summary_argv(argv, session, note):
    boundary = argv.index('--')
    return (*argv[:boundary], 'resume', session, '--', note)
