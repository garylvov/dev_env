"""Cooperative, serialized handoff to a fresh managed client.

Only the original supervisor stops its own child. This controller never sends a
termination signal, and it does not reconcile external operations.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
import socket
import time

from .core.store import Store, component, now, process_identity, read_json, write_json


@dataclass
class Handoff:
    recovery_from: str | None
    _lock: object

    def release(self) -> None:
        if self._lock is not None:
            handle, self._lock = self._lock, None
            handle.close()


def _record(store, agent, run_id):
    return store.safe(store.agent_path(agent) / 'runs' / component(run_id) / 'run.json')


def _validate(store, record, engine):
    if record.get('engine') != engine:
        raise ValueError('Continuation requires the previous run\'s same engine')
    if record.get('host') != socket.gethostname():
        raise ValueError('Continue on the original host; cross-host shutdown cannot be verified')
    for prefix in ('supervisor', 'child'):
        if store._pid(record, prefix) is None:
            raise ValueError(f'Missing {prefix} PID; cannot verify continuation shutdown')


def _dead(store, record, prefix):
    dead = store._process_dead(record, prefix)
    if not dead:
        expected = record.get(prefix + '_identity')
        if not expected or process_identity(store._pid(record, prefix)) != expected:
            raise ValueError(f'Uncertain {prefix} identity; refusing continuation')
    return dead


def _unchanged(before, after):
    keys = ('run_id', 'engine', 'host', 'supervisor_pid', 'supervisor_identity',
            'child_pid', 'child_identity', 'recovery_from', 'recovery_to')
    if any(before.get(key) != after.get(key) for key in keys):
        raise ValueError('Run identity or recovery links changed during continuation; retry')


@contextmanager
def handoff(store: Store, agent: str, engine: str, timeout: float = 15):
    """Hold a per-task reservation until the caller claims its successor.

The yielded handle must be released immediately after claim, before launching the
new client. Exiting this context releases it on all error paths as well.
"""
    if timeout < 0:
        raise ValueError('Continuation timeout must be nonnegative')
    handle = store.safe(store.path / '.continue.lock').open('a')
    reservation = Handoff(None, handle)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another continuation is already in progress') from exc
        selected = store.continuation_candidate(agent)
        if selected is None:
            yield reservation
            return
        ancestors = store.recovery_ancestors(agent, selected)
        own_run = os.environ.get('TOKEN_KIT_RUN')
        ancestor_ids = {row.get('run_id') for row in ancestors}
        if own_run and own_run in ancestor_ids | {selected}:
            raise ValueError('Run continuation from another terminal, outside the target managed session')
        target = _record(store, agent, selected)
        before = read_json(target)
        _validate(store, before, engine)
        # Lock order matches hooks: runtime first, then the task store.
        with store.safe(target.parent / 'runtime.lock').open('a') as runtime_lock:
            fcntl.flock(runtime_lock, fcntl.LOCK_EX)
            with store.locked():
                if store._continuation_candidate_locked(agent) != selected:
                    raise ValueError('Continuation target changed; retry')
                record = read_json(target)
                _unchanged(before, record)
                _validate(store, record, engine)
                supervisor_dead = _dead(store, record, 'supervisor')
                child_dead = _dead(store, record, 'child')
                if supervisor_dead and not child_dead:
                    raise ValueError('Original supervisor is gone but its child is alive; cannot request cooperative shutdown')
                control_path = store.safe(target.parent / 'runtime.json')
                if not supervisor_dead:
                    control = read_json(control_path)
                    if control.get('engine', engine) != engine:
                        raise ValueError('Runtime engine disagrees with run record')
                    if control.get('halt_kind') != 'continuation_requested':
                        control.setdefault('continuation_previous_halt_kind', control.get('halt_kind'))
                        control.setdefault('continuation_previous_reason', control.get('reason'))
                    control.update(phase='halted', halt_kind='continuation_requested',
                                   reason='Explicit user request for fresh continuation')
                    write_json(control_path, control)
                record['continuation_requested_at'] = now()
                record['continuation_requested'] = True
                write_json(target, record)
        deadline = time.monotonic() + timeout
        while True:
            with store.locked():
                if store._continuation_candidate_locked(agent) != selected:
                    raise ValueError('Continuation target changed while waiting; no successor launched')
                record = read_json(target)
                _unchanged(before, record)
                _validate(store, record, engine)
                supervisor_dead = _dead(store, record, 'supervisor')
                child_dead = _dead(store, record, 'child')
                if supervisor_dead and child_dead:
                    if record.get('halt_kind') != 'continuation_requested':
                        record.setdefault('continuation_previous_halt_kind', record.get('halt_kind'))
                    record.update(status='interrupted', halt_kind='continuation_requested',
                                  continuation_stopped_at=now())
                    write_json(target, record)
                    reservation.recovery_from = selected
                    break
            if time.monotonic() >= deadline:
                raise ValueError('Previous runner has not stopped; continuation timed out without launching a replacement. Retry after it exits.')
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        yield reservation
    finally:
        reservation.release()
