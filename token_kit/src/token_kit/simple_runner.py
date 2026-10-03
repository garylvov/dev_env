"""Folder-first client launching and automatic context rollover."""
from __future__ import annotations
from dataclasses import asdict, replace
import os
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from .simple_types import TaskView, LaunchOptions
from .simple_process import Slot, identity, read, write
from . import simple_runtime


def _stop(child, record, timeout=8):
    if child.poll() is not None:
        return child.returncode == 0
    if identity(child.pid) != record.get('child_identity'):
        return False
    child.send_signal(signal.SIGTERM)
    try:
        child.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _observe(path, notices):
    try:
        value = read(path)
        if not isinstance(value, dict):
            raise ValueError('expected an object')
        return value
    except (OSError, ValueError) as error:
        diagnostic = f'Optional process observation unavailable ({path.name}); native fallback remains available'
        if diagnostic not in notices:
            print('Token Kit: ' + diagnostic, file=sys.stderr)
            notices.add(diagnostic)
        return {}


def run_session(view: TaskView, options: LaunchOptions, prompt: str | None = None,
                agent: str = 'coordinator', resume: bool = False) -> int:
    from .task_files import recovery_input, save_settings, mark_messages_presented, has_work_context
    from .simple_adapters import prepare
    slot = Slot(view.root, agent).acquire(resume=resume)
    completed = 0
    notices = set()
    next_prompt = prompt
    pending_ids = ()
    try:
        if resume:
            recovered = recovery_input(view, agent=agent)
            next_prompt = recovered.text if has_work_context(view, agent=agent) else None
            if prompt:
                next_prompt = ((next_prompt + '\n\n') if next_prompt else '') + 'Current user request:\n' + prompt
            pending_ids = recovered.message_ids
        save_settings(view, asdict(options))
        while True:
            effective = options
            if options.max_rollovers is not None and completed >= options.max_rollovers:
                effective = replace(options, rollover=None)
            launch_started = False
            try:
                run = slot.claim(options.engine)
                plan = prepare(view, effective, next_prompt, run, agent=agent)
                try:
                    from .simple_recovery import native_recipe
                    write(slot.directory / 'native-recovery.json', native_recipe(view, options, agent))
                except (OSError, ValueError) as error:
                    print(f'Token Kit: native recovery recipe unavailable: {error}', file=sys.stderr)
                environment = {key: value for key, value in os.environ.items()
                               if not key.startswith('TOKEN_KIT_')}
                environment.update(plan.env)
                environment.update(TOKEN_KIT_TASK=str(view.root.resolve()), TOKEN_KIT_AGENT=agent,
                                   TOKEN_KIT_RUN=run.name, TOKEN_KIT_SIMPLE_RUN=str(run))
                try:
                    diagnostics = json.loads(environment.get('TOKEN_KIT_SIMPLE_CAPABILITIES', '{}')).get('diagnostics', [])
                except (ValueError, AttributeError):
                    diagnostics = []
                for diagnostic in diagnostics:
                    if diagnostic not in notices:
                        print('Token Kit: ' + diagnostic, file=sys.stderr)
                        notices.add(diagnostic)
                simple_runtime.initialize(run, effective,
                       environment.get('TOKEN_KIT_SIMPLE_SESSION_ID'), view.state,
                       assignment=view.assignment, output=view.output)
                launch_started = True
                child = slot.spawn(plan.argv, plan.cwd, environment)
            except Exception:
                # Before spawn, no client can exist. Corrected configuration
                # must be immediately retryable; only actual spawn uncertainty persists.
                if not launch_started and slot.record:
                    slot.publish(status='exited', exit_code=1)
                raise
            started_at = time.monotonic()
            rollover = False
            continuing = False
            try:
                while child.poll() is None:
                    control = _observe(run / 'control.json', notices)
                    if pending_ids and control.get('armed') and (control.get('valid_usage') or control.get('activity')):
                        try:
                            mark_messages_presented(view, pending_ids, agent=agent)
                            pending_ids = ()
                        except (OSError, ValueError) as error:
                            print(f'Token Kit: message delivery cursor unavailable: {error}', file=sys.stderr)
                            pending_ids = ()  # leave persisted cursor unchanged, do not spam warnings
                    diagnostic = control.get('degraded')
                    if effective.rollover is not None and not control.get('armed') and time.monotonic() - started_at >= 5:
                        diagnostic = 'Managed rollover handshake not observed; native compaction remains available'
                    if diagnostic and diagnostic not in notices:
                        print('Token Kit: ' + diagnostic, file=sys.stderr)
                        notices.add(diagnostic)
                    request = _observe(slot.directory / 'continue.json', notices)
                    continuing = request.get('run_id') == run.name
                    if control.get('continue_request') and control.get('continue_request') != request.get('request_id'):
                        from .simple_process import locked
                        with locked(run / 'control.lock'):
                            current = _observe(run / 'control.json', notices)
                            if current.get('continue_request') == control.get('continue_request'):
                                current.pop('continue_request', None)
                                current['phase'] = 'running'
                                write(run / 'control.json', current)
                                control = current
                    safe = (control.get('phase') == 'ready' and not control.get('children')
                            and not control.get('unknown_children'))
                    # Explicit continuation cooperatively requests a boundary too.
                    if continuing and control.get('phase') == 'running':
                        from .simple_process import locked
                        with locked(run / 'control.lock'):
                            current = _observe(run / 'control.json', notices)
                            current['phase'] = 'requested'
                            current['continue_request'] = request.get('request_id')
                            write(run / 'control.json', current)
                    if safe:
                        latest = _observe(run / 'control.json', notices)
                        if latest.get('children') or latest.get('unknown_children') or latest.get('phase') != 'ready':
                            continue
                        if not _stop(child, slot.record):
                            if child.poll() is not None:
                                slot.publish(status='exited', exit_code=child.returncode)
                                return child.returncode or 1
                            print('Token Kit: client shutdown could not be verified; no successor launched.', file=sys.stderr)
                            return 1
                        rollover = not continuing
                        break
                    time.sleep(.1)
            except KeyboardInterrupt:
                _stop(child, slot.record)
                if child.poll() is not None:
                    slot.publish(status='exited', exit_code=child.returncode)
                return 130
            final_control = _observe(run / 'control.json', notices)
            if effective.rollover is not None and not final_control.get('armed'):
                diagnostic = 'Managed rollover handshake not observed; native compaction remains available'
                if diagnostic not in notices:
                    print('Token Kit: ' + diagnostic, file=sys.stderr)
                    notices.add(diagnostic)
            if pending_ids and final_control.get('armed') and (final_control.get('valid_usage') or final_control.get('activity')):
                try:
                    mark_messages_presented(view, pending_ids, agent=agent)
                    pending_ids = ()
                except (OSError, ValueError) as error:
                    print(f'Token Kit: message delivery cursor unavailable: {error}', file=sys.stderr)
            if final_control.get('continue_request'):
                final_request = _observe(slot.directory / 'continue.json', notices)
                continuing = final_control['continue_request'] == final_request.get('request_id')
                rollover = False  # a canceled user continuation is never an automatic restart
            elif child.returncode == 0 and final_control.get('phase') == 'ready':
                rollover = not continuing
            # Include notes written during shutdown. Snapshot never overwrites live files.
            try:
                if view.state.is_file():
                    with view.state.open('rb') as source:
                        saved_state = source.read(65_537)
                    if saved_state.strip():
                        from .core.store import atomic_bytes
                        from .simple_process import safe
                        snapshot = safe(view.root, slot.directory / 'state-snapshot.md')
                        atomic_bytes(snapshot, saved_state[:65_536])
                recovery = recovery_input(view, agent=agent, mark_presented=False)
                (run / 'handoff.md').write_text(recovery.text)
                from .simple_recovery import native_recipe
                write(slot.directory / 'native-recovery.json', native_recipe(view, options, agent))
            except (OSError, ValueError) as error:
                print(f'Token Kit: handoff snapshot unavailable: {error}', file=sys.stderr)
            slot.publish(status='exited', exit_code=child.returncode)
            if continuing:
                return 0
            if not rollover:
                return child.returncode or 0
            control = _observe(run / 'control.json', notices)
            if completed and not (control.get('activity', 0) > 0 and control.get('valid_usage')):
                print('Token Kit: successor requested rollover without fresh activity and usage; stopped.', file=sys.stderr)
                return 1
            completed += 1
            recovered = recovery_input(view, agent=agent)
            next_prompt = recovered.text if has_work_context(view, agent=agent) else None
            pending_ids = recovered.message_ids
    finally:
        slot.close()
