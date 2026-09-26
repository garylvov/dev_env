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
        return True
    if identity(child.pid) != record.get('child_identity'):
        return False
    child.send_signal(signal.SIGTERM)
    try:
        child.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def run_session(view: TaskView, options: LaunchOptions, prompt: str | None = None,
                agent: str = 'coordinator', resume: bool = False) -> int:
    from .task_files import recovery_input, save_settings
    from .simple_adapters import prepare
    slot = Slot(view.root, agent).acquire(resume=resume)
    completed = 0
    notices = set()
    next_prompt = prompt
    try:
        if resume and next_prompt is None:
            next_prompt = recovery_input(view, agent=agent, mark_presented=True).text or None
        save_settings(view, asdict(options))
        while True:
            run = slot.claim(options.engine)
            effective = options
            if options.max_rollovers is not None and completed >= options.max_rollovers:
                effective = replace(options, rollover=None)
            try:
                plan = prepare(view, effective, next_prompt, run, agent=agent)
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
                       environment.get('TOKEN_KIT_SIMPLE_SESSION_ID'), view.state)
                child = slot.spawn(plan.argv, plan.cwd, environment)
            except Exception:
                # A failed spawn/publication may be uncertain. Never erase its claim.
                raise
            rollover = False
            continuing = False
            try:
                while child.poll() is None:
                    control = read(run / 'control.json')
                    diagnostic = control.get('degraded')
                    if diagnostic and diagnostic not in notices:
                        print('Token Kit: ' + diagnostic, file=sys.stderr)
                        notices.add(diagnostic)
                    request = read(slot.directory / 'continue.json')
                    continuing = request.get('run_id') == run.name
                    safe = control.get('phase') == 'ready'
                    # Explicit continuation cooperatively requests a boundary too.
                    if continuing and control.get('phase') == 'running':
                        from .simple_process import locked
                        with locked(run / 'control.lock'):
                            current = read(run / 'control.json')
                            current['phase'] = 'requested'
                            write(run / 'control.json', current)
                    if safe:
                        if not _stop(child, slot.record):
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
            if child.returncode == 0 and read(run / 'control.json').get('phase') == 'ready':
                rollover = not continuing
            # Include notes written during shutdown. Snapshot never overwrites live files.
            try:
                recovery = recovery_input(view, agent=agent, mark_presented=False)
                (run / 'handoff.md').write_text(recovery.text)
            except (OSError, ValueError) as error:
                print(f'Token Kit: handoff snapshot unavailable: {error}', file=sys.stderr)
            slot.publish(status='exited', exit_code=child.returncode)
            if continuing:
                return 0
            if not rollover:
                return child.returncode or 0
            control = read(run / 'control.json')
            if completed and not (control.get('activity', 0) > 0 and control.get('valid_usage')):
                print('Token Kit: successor requested rollover without fresh activity and usage; stopped.', file=sys.stderr)
                return 1
            completed += 1
            next_prompt = recovery_input(view, agent=agent, mark_presented=True).text or None
    finally:
        slot.close()
