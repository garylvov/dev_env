"""Best-effort usage hooks. A safe boundary requests rollover, never a closure."""
from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
import re
import shlex
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from token_kit.simple_process import locked, read, write

EVENTS = ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop', 'SubagentStart', 'SubagentStop', 'PreCompact')
# Launch capture only needs shell commands, so other tools skip the hook process.
MATCHED = {'PreToolUse': 'Bash'}


# Claude Code delivers these through UserPromptSubmit, but the user did not write them.
MACHINE_TAGS = ('<task-notification>', '<teammate-message', '<cross-session-message')


def _machine_message(prompt: str) -> bool:
    body = re.sub(r'<system-reminder>.*?</system-reminder>', '', prompt, flags=re.S).lstrip()
    return body.startswith(MACHINE_TAGS)

def hooks():
    command = shlex.join([sys.executable, str(Path(__file__).resolve())])
    entry = {'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]}
    return {**{event: [entry] for event in EVENTS},
            **{event: [dict(entry, matcher=matcher)] for event, matcher in MATCHED.items()}}


def _record_error(control: dict, exc: Exception):
    # Recording is best effort: never block the tool call, but leave a trace.
    control['record_errors'] = (control.get('record_errors', []) +
                                [f'{datetime.now().astimezone().isoformat()} {type(exc).__name__}: {exc}'])[-20:]


def _worker_label(control: dict, payload: dict) -> str:
    native = str(payload.get('agent_id') or '')
    if not native:
        return 'main'
    known = control.get('workers', {}).get(native)
    return known['name'] if known else f"{payload.get('agent_type') or 'worker'}-{native[:6]}"


def _records(control: dict, payload: dict, event: str) -> bool:
    """Worker and experiment records; True when the event needs no further handling."""
    from token_kit import simple_records as records
    root = Path(control['task_root'])
    tool = payload.get('tool_name')
    tool_input = payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {}
    native = str(payload.get('agent_id') or '')
    if event == 'PreToolUse':
        command = tool_input.get('command')
        if tool == 'Bash' and isinstance(command, str) and records.is_launch(command):
            records.record_launch(root, command, _worker_label(control, payload))
        return True
    if event == 'PostToolUse' and tool == 'SubagentHandback' and native:
        message = tool_input.get('message')
        if isinstance(message, str) and message.strip():
            control.setdefault('handbacks', {})[native] = message
        return True
    output = Path(control.get('output') or control['state'])
    if event == 'PostToolUse' and tool == 'Agent' and not native:
        response = payload.get('tool_response') if isinstance(payload.get('tool_response'), dict) else {}
        worker_id = str(response.get('agentId') or '')
        if worker_id:
            workers = control.setdefault('workers', {})
            workers[worker_id] = records.record_worker_start(root, tool_input, response)
            control['workers'] = dict(list(workers.items())[-256:])
            # Foreground workers finish before their Agent call returns.
            pending = control.get('pending_finals', {}).pop(worker_id, None)
            if pending:
                records.record_worker_final(root, output, workers[worker_id], pending['report'], pending['transcript'])
        return False
    if event == 'SubagentStop' and native and payload.get('agent_type'):
        report = control.get('handbacks', {}).pop(native, None) or payload.get('last_assistant_message') or ''
        transcript = payload.get('agent_transcript_path')
        worker = control.get('workers', {}).get(native)
        if worker:
            records.record_worker_final(root, output, worker, report, transcript)
        else:
            pending = control.setdefault('pending_finals', {})
            pending[native] = {'report': report, 'transcript': transcript}
            control['pending_finals'] = dict(list(pending.items())[-32:])
    return False


def usage_sample(path: Path, expected: str) -> dict:
    """Read at most the final MiB of the exact provider-supplied transcript."""
    from token_kit.core.context_window import claude_window
    with path.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 1024 * 1024))
        if size > 1024 * 1024:
            stream.readline()
        lines = stream.read(1024 * 1024).splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (row.get('type') != 'assistant' or row.get('isSidechain') or
                row.get('sessionId', expected) != expected):
            continue
        message = row.get('message') or {}
        usage = message.get('usage') or {}
        if 'input_tokens' not in usage:
            continue
        values = [usage.get(key, 0) for key in ('input_tokens', 'cache_read_input_tokens',
                  'cache_creation_input_tokens', 'output_tokens')]
        if any(type(value) is not int or value < 0 for value in values):
            return {}
        window, _ = claude_window(message.get('model', 'unknown'))
        return {'context_tokens': sum(values), 'context_window': window}
    return {}


def initialize(run: Path, options, expected_session: str | None, state: Path,
               assignment: Path | None = None, output: Path | None = None,
               kickoff_prompt: str | None = None):
    write(run / 'control.json', {'run_id': run.name,
          'expected_session': expected_session, 'armed': False, 'phase': 'running',
          'threshold': options.rollover, 'context_window': options.context_window,
          'state': str(state), 'assignment': str(assignment) if assignment else None,
          'output': str(output or state), 'kickoff_prompt': kickoff_prompt,
          'task_root': os.environ.get('TOKEN_KIT_TASK', str(state.parent)), 'children': [], 'activity': 0, 'valid_usage': False})


def rollover_growth(control: dict, used: int, window: int, limit: int) -> bool:
    """Amortize startup context before requesting another managed handoff."""
    baseline = control.setdefault('startup_tokens', used)
    minimum = max(20_000, int(window * 0.10))
    control['minimum_growth'] = minimum
    control['token_growth'] = max(0, used - baseline)
    if baseline >= limit:
        control.setdefault('loop_warning',
            f'Startup baseline {baseline} tokens already meets rollover threshold {limit}; '
            f'continuing until at least {minimum} tokens of new work accumulate.')
    return used >= limit and used - baseline >= minimum


def handle(run: Path, payload: dict, nonce: str) -> dict:
    with locked(run / 'control.lock'):
        control = read(run / 'control.json')
        if nonce != control.get('run_id') or nonce != run.name:
            return {}
        expected = control.get('expected_session')
        # Unknown and child events never bind or control a parent. Environment
        # inheritance alone is not identity evidence.
        if not expected or payload.get('session_id') != expected:
            return {}
        event = payload.get('hook_event_name')
        try:
            done = _records(control, payload, event)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            _record_error(control, exc)
            done = event in ('PreToolUse',)
        if done:
            write(run / 'control.json', control)
            return {}
        if event in ('SubagentStart', 'SubagentStop'):
            native = payload.get('agent_id')
            if event == 'SubagentStart' and control['phase'] == 'ready':
                control['phase'] = 'requested'
            children = set(control.get('children', []))
            if not native and event == 'SubagentStart':
                control['unknown_children'] = True
                write(run / 'control.json', control)
            if native:
                if event == 'SubagentStart':
                    children.add(str(native))
                else:
                    children.discard(str(native))
                control['children'] = sorted(children)
                write(run / 'control.json', control)
            return {}
        if (payload.get('agent_id') or payload.get('agent_type') or
                payload.get('parent_thread_id') or payload.get('parent_session_id')):
            return {}
        if event == 'SessionStart':
            control['armed'] = True
        if not control.get('armed'):
            if payload.get('tool_name') == 'Agent':
                write(run / 'control.json', control)  # keep the worker map
            return {}
        if event == 'UserPromptSubmit' and control.get('assignment'):
            prompt = payload.get('prompt')
            prompt_id = payload.get('prompt_id') or payload.get('turn_id')
            seen = control.get('recorded_prompt_ids', [])
            if prompt_id and prompt_id in seen:
                return {}
            if isinstance(prompt, str) and _machine_message(prompt):
                return {}
            if not isinstance(prompt, str) or not prompt.strip():
                control['degraded'] = 'Cannot record user ask: prompt payload is missing'
                write(run / 'control.json', control)
                return {'decision': 'block', 'reason': control['degraded']}
            kickoff = control.pop('kickoff_prompt', None)
            if prompt != kickoff:
                from token_kit.task_files import append_ask
                try:
                    number = append_ask(Path(control['task_root']), Path(control['assignment']), prompt)
                    control['last_ask'] = number
                except (OSError, ValueError) as exc:
                    control['degraded'] = f'Cannot record user ask before work: {exc}'
                    write(run / 'control.json', control)
                    return {'decision': 'block', 'reason': control['degraded']}
            if prompt_id:
                control['recorded_prompt_ids'] = (seen + [prompt_id])[-2048:]
        if event == 'PostToolUse' and control['phase'] == 'running':
            control['activity'] += 1
        sample = {}
        transcript = payload.get('transcript_path')
        if transcript:
            try:
                sample = usage_sample(Path(transcript), expected)
            except (OSError, ValueError, ImportError, TypeError):
                sample = {}
        used = sample.get('context_tokens')
        window = control.get('context_window') or sample.get('context_window')
        from token_kit.rollover import effective_limit
        try:
            limit = (effective_limit(control['threshold'], window)
                     if control.get('threshold') is not None else None)
        except ValueError:
            limit = None
        if event == 'Stop' and control.get('threshold') is not None and (used is None or limit is None):
            control['degraded'] = 'Current context usage/window unavailable; native compaction remains enabled'
        elif used is not None and limit is not None:
            control.pop('degraded', None)
        if type(used) is int and used >= 0:
            control.setdefault('startup_tokens', used)
        crossed = (type(used) is int and type(window) is int and window > 0 and limit is not None
                   and rollover_growth(control, used, window, limit))
        if isinstance(used, (int, float)) and used > 0:
            control['valid_usage'] = True
            control['context_tokens'] = used
            if type(window) is int and window > 0:
                control['telemetry_window'] = window
        output = {}
        if event == 'UserPromptSubmit' and not control.get('guidance_presented'):
            guidance = os.environ.get('TOKEN_KIT_SIMPLE_GUIDANCE')
            if guidance:
                output = {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': guidance}}
                control['guidance_presented'] = True
        # Native children of unknown fate defer this segment's managed rollover.
        if control.get('children') or control.get('unknown_children'):
            control['degraded'] = 'Native workers are active; using native compaction until they finish'
        elif control.get('threshold') is not None:
            if control['phase'] == 'requested' and event == 'Stop':
                control['phase'] = 'ready'
                output = {'continue': False, 'stopReason': 'Token Kit is carrying this session into fresh context.'}
            elif event == 'PreCompact' and crossed:
                control['phase'] = 'ready'
                control['reason'] = 'parent_compaction'
                # Do not veto native compaction. Supervisor handles verified shutdown.
            elif crossed and control['phase'] == 'running' and event in ('PostToolUse', 'Stop'):
                control['phase'] = 'requested'
                percent = f"{used / window * 100:.0f}%" if window else f"{used} tokens"
                from token_kit.simple_guidance import summary_request
                assignment = control.get('assignment')
                task_root = Path(control['task_root'])
                note = summary_request(percent, control.get('output') or control['state'],
                                       assignment or 'the assignment file', control['state'],
                                       task_root / 'asks.md' if assignment and Path(assignment).resolve().parent == task_root.resolve() else None,
                                       task_root / 'experiments.md' if (task_root / 'experiments.md').is_file() else None)
                output_path = Path(control.get('output') or control['state'])
                from token_kit.task_files import safe_task_path
                from token_kit.simple_types import TaskView
                root = Path(control['task_root']).resolve()
                safe_task_path(TaskView(root, None, '', output_path, output_path, output_path), output_path)
                entry = (f"\n### rollover {datetime.now().astimezone().isoformat()} at {percent} ({used} tokens)\n"
                         "Summary requested; fresh context will follow verified session exit.\n")
                control['rollover_entry'] = entry
                with output_path.open('a') as history:
                    history.write(entry)
                output = ({'decision': 'block', 'reason': note} if event == 'Stop' else
                          {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': note}})
        write(run / 'control.json', control)
        return output


def main():
    try:
        run = Path(os.environ['TOKEN_KIT_SIMPLE_RUN'])
        payload = json.load(sys.stdin)
        print(json.dumps(handle(run, payload, os.environ.get('TOKEN_KIT_RUN', ''))))
    except (OSError, ValueError, KeyError, TypeError):
        # Optional hook/telemetry failure never blocks ordinary client work.
        print('{}')


if __name__ == '__main__':
    main()
