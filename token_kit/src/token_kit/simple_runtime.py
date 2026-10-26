"""Best-effort usage hooks. A safe boundary requests rollover, never a closure."""
from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
import re
import shlex
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from token_kit.simple_process import locked, read, write

EVENTS = ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop', 'SubagentStart', 'SubagentStop', 'PreCompact')
# Launch capture only needs shell commands, so other tools skip the hook process.
MATCHED = {'PreToolUse': 'Bash'}


# Claude Code delivers these through UserPromptSubmit, but the user did not write them.
MACHINE_TAGS = ('<task-notification>', '<teammate-message', '<cross-session-message', '<agent-message',
                '[Cross-session delivery notice]')
# A child no hook has mentioned for this long is presumed gone (TaskStop, crash).
CHILD_TTL = 1800
# Claude Code clips session_crons prompts at 1000 chars with this marker.
CLIP_MARKER = '\u2026 [+'
MIN_CLIPPED_HEAD = 200
# Background work that dies with the session: named Agent workers report as 'teammate'.
WORKER_TASKS = ('subagent', 'teammate', 'workflow')
# A due rollover waits this long for background workers, then proceeds and ends them.
WAIT_LIMIT = 1800
STANDARD_WINDOW = 200_000
DONE_TASK = ('completed', 'failed', 'killed', 'stopped', 'cancelled', 'canceled', 'done', 'error')


def _machine_message(prompt: str) -> bool:
    body = re.sub(r'<system-reminder>.*?</system-reminder>', '', prompt, flags=re.S).lstrip()
    return body.startswith(MACHINE_TAGS)

# uv may remove the launching interpreter mid-session; hooks then resolve one the launcher's way.
_INTERPRETER = 'p="$1"; shift; [ -x "$p" ] && exec "$p" "$@"; exec uv run --python ">=3.11" --no-project python "$@"'


def hooks():
    command = shlex.join(['sh', '-c', _INTERPRETER, 'token-kit-hook', sys.executable, str(Path(__file__).resolve())])
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


ALIASES = {'opus': 'claude-opus-5-5', 'sonnet': 'claude-sonnet-5-5', 'fable': 'claude-fable-5-1'}


def model_window(model: str) -> tuple[int | None, str | None]:
    """claude_window, but dated, [1m] and alias ids resolve, and other claude-* ids assume 200K."""
    from token_kit.core.context_window import claude_window
    window, source = claude_window(model)
    if window:
        return window, source
    base = re.sub(r'-[0-9]{8}$', '', model.lower().removesuffix('[1m]'))
    window, source = claude_window(ALIASES.get(base, base))
    if window:
        return window, source
    if base.startswith('claude-') or base == 'haiku':
        return 200_000, 'assumed_default'
    return None, None


def usage_sample(path: Path, expected: str) -> dict:
    """Read at most the final MiB of the exact provider-supplied transcript."""
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
        model = message.get('model', 'unknown')
        window, source = model_window(model)
        sample = {'context_tokens': sum(values), 'context_window': window}
        if source == 'assumed_default':
            sample['window_note'] = f'Unrecognized model {model}; assuming a {window}-token window for rollover'
        return sample
    return {}


def initialize(run: Path, options, expected_session: str | None, state: Path,
               assignment: Path | None = None, output: Path | None = None,
               kickoff_prompt: str | None = None):
    write(run / 'control.json', {'run_id': run.name,
          'expected_session': expected_session, 'armed': False, 'phase': 'running', 'launch_model': options.model,
          'threshold': None if options.rollover in (0, 'off') else options.rollover, 'context_window': options.context_window,
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


def _history_line(control: dict, text: str):
    output = Path(control.get('output') or control['state'])
    with output.open('a') as history:
        history.write(f'- {datetime.now().astimezone().isoformat(timespec="minutes")} {text}\n')


def _busy(task) -> bool:
    return (isinstance(task, dict) and task.get('type') in WORKER_TASKS
            and str(task.get('status', '')).lower() not in DONE_TASK)


def _waiting_on(control: dict, payload: dict) -> list[str]:
    tasks = payload.get('background_tasks') if isinstance(payload.get('background_tasks'), list) else []
    names = [str(t.get('description') or t.get('id') or t['type']) for t in tasks if _busy(t)]
    workers = control.get('workers', {})
    names += [workers.get(c, {}).get('name', c) for c in control.get('children', [])]
    return sorted(set(names))


def waiting_note(names: list[str], deadline: float) -> str:
    shown = ', '.join(names[:8]) + (f' and {len(names) - 8} more' if len(names) > 8 else '') or 'unnamed workers'
    return (f'Token Kit: rollover is due but waits for background workers ({shown}), because ending this '
            f'session ends them. TaskStop idle ones; run long workers detached (see guidance). At '
            f'{datetime.fromtimestamp(deadline).strftime("%H:%M")} rollover proceeds anyway and ends any still '
            'running; their agents/ records let the successor relaunch them.')


def _settle_children(control: dict, payload: dict, now: float):
    """Forget children no hook has mentioned for CHILD_TTL; a main Stop resolves the rest."""
    seen = control.get('child_seen', {})
    children = [c for c in control.get('children', []) if now - seen.get(c, now) < CHILD_TTL]
    if payload.get('hook_event_name') == 'Stop':
        tasks = payload.get('background_tasks')
        if isinstance(tasks, list):
            # Parent-scoped registry: the source of truth when the client sends it.
            busy = any(_busy(t) for t in tasks)
            control['unknown_children'] = busy and not children
            if not busy:
                children = []
        else:
            control.pop('unknown_children', None)
    control['children'] = children
    control['child_seen'] = {c: seen[c] for c in children if c in seen}


def _cron_prompt(control: dict, prompt: str) -> bool:
    """A recorded loop firing is the session's own schedule, not a new user message.

    Claude Code owns the clip marker; only a long clipped head stands in for the whole prompt.
    """
    for known in control.get('cron_prompts', []):
        head, clipped, _ = known.partition(CLIP_MARKER)
        if prompt == known or (clipped and len(head.strip()) >= MIN_CLIPPED_HEAD and prompt.startswith(head)):
            return True
    return False


def _snapshot_loops(control: dict, payload: dict):
    """Mirror the parent's session crons into loops.md so a fresh session can recreate them."""
    from token_kit.simple_records import record_loops
    crons = payload['session_crons']
    tasks = payload.get('background_tasks') if isinstance(payload.get('background_tasks'), list) else []
    try:
        control['cron_seen'] = record_loops(Path(control['task_root']), crons, tasks, control.get('cron_seen', {}),
                                            may_clear=bool(control.get('cron_seen')))
    except (OSError, ValueError) as exc:
        _record_error(control, exc)
    control['cron_prompts'] = [c['prompt'] for c in crons if isinstance(c, dict) and isinstance(c.get('prompt'), str)][:32]


def _asks_nudge(control: dict) -> dict:
    """One non-blocking reminder per recorded message whose turn ended without asks.md changing."""
    number, recorded = control.get('last_ask'), control.get('last_ask_at')
    root = Path(control['task_root'])
    assignment = control.get('assignment')
    if not number or not recorded or control.get('nudged') == number or not assignment:
        return {}
    if Path(assignment).resolve().parent != root.resolve():
        return {}
    asks = root / 'asks.md'
    if asks.is_file() and asks.stat().st_mtime >= recorded:
        return {}
    control['nudged'] = number
    return {'hookSpecificOutput': {'hookEventName': 'Stop',
            'additionalContext': f'Token Kit: asks.md not updated for #{number}.'}}


def handle(run: Path, payload: dict, nonce: str) -> dict:
    with locked(run / 'control.lock'):
        control = read(run / 'control.json')
        if nonce != control.get('run_id') or nonce != run.name:
            return {}
        expected = control.get('expected_session')
        event = payload.get('hook_event_name')
        now = time.time()
        # /clear keeps this process but starts a new session id. Only an armed
        # run's own hook process sees it, so follow the new id.
        if (event == 'SessionStart' and payload.get('source') == 'clear' and expected and control.get('armed')
                and payload.get('session_id') and payload.get('session_id') != expected
                and not payload.get('agent_id')):
            expected = control['expected_session'] = str(payload['session_id'])
            control.pop('startup_tokens', None)
            try:
                _history_line(control, f'/clear started session {expected}; Token Kit follows it.')
            except OSError as exc:
                _record_error(control, exc)
        # Unknown and child events never bind or control a parent. Environment
        # inheritance alone is not identity evidence.
        if not expected or payload.get('session_id') != expected:
            return {}
        native = str(payload.get('agent_id') or '')
        if native in control.get('children', []):
            control.setdefault('child_seen', {})[native] = now
        try:
            done = _records(control, payload, event)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            _record_error(control, exc)
            done = event in ('PreToolUse',)
        if done:
            write(run / 'control.json', control)
            return {}
        if event in ('SubagentStart', 'SubagentStop'):
            if event == 'SubagentStart' and control['phase'] == 'ready':
                control['phase'] = 'requested'
            children = set(control.get('children', []))
            if not native and event == 'SubagentStart':
                control['unknown_children'] = True
            if native:
                if event == 'SubagentStart':
                    children.add(native)
                    control.setdefault('child_seen', {})[native] = now
                else:
                    children.discard(native)
                    control.get('child_seen', {}).pop(native, None)
                control['children'] = sorted(children)
            write(run / 'control.json', control)
            return {}
        if (native or payload.get('agent_type') or
                payload.get('parent_thread_id') or payload.get('parent_session_id')):
            if native in control.get('children', []):
                write(run / 'control.json', control)
            return {}
        if event == 'SessionStart':
            control['armed'] = True
        if not control.get('armed'):
            if payload.get('tool_name') == 'Agent':
                write(run / 'control.json', control)  # keep the worker map
            return {}
        output = {}
        if event == 'UserPromptSubmit' and control.get('assignment'):
            prompt = payload.get('prompt')
            prompt_id = payload.get('prompt_id') or payload.get('turn_id')
            seen = control.get('recorded_prompt_ids', [])
            if prompt_id and prompt_id in seen:
                return {}
            if isinstance(prompt, str) and (_machine_message(prompt) or _cron_prompt(control, prompt)):
                return {}
            failure = None
            if not isinstance(prompt, str) or not prompt.strip():
                failure = 'prompt payload is missing'
            elif prompt != control.pop('kickoff_prompt', None):
                from token_kit.task_files import append_ask
                try:
                    number = append_ask(Path(control['task_root']), Path(control['assignment']), prompt)
                    control['last_ask'] = number
                    control['last_ask_at'] = now
                except (OSError, ValueError) as exc:
                    failure = str(exc)
            if failure:
                # Never discard the user's message; the model records it instead.
                control['record_warning'] = f'Cannot record user ask: {failure}'
                output = {'hookSpecificOutput': {'hookEventName': event, 'additionalContext':
                          f"Token Kit could not record this message; append it verbatim, dated and numbered, to {control['assignment']}."}}
            elif prompt_id:
                control['recorded_prompt_ids'] = (seen + [prompt_id])[-2048:]
        if event == 'PostToolUse' and control['phase'] == 'running':
            control['activity'] += 1
        if event == 'Stop' and isinstance(payload.get('session_crons'), list):
            _snapshot_loops(control, payload)
        sample = {}
        transcript = payload.get('transcript_path')
        if transcript:
            try:
                sample = usage_sample(Path(transcript), expected)
            except (OSError, ValueError, ImportError, TypeError):
                sample = {}
        if sample.get('window_note'):
            control['window_note'] = sample['window_note']
        # Transcripts drop [1m]; Claude Code gives a model launched without it the standard window.
        launched = control.get('launch_model')
        if sample.get('context_window') and launched and not launched.lower().endswith('[1m]'):
            sample['context_window'] = min(sample['context_window'], STANDARD_WINDOW)
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
            control['peak_tokens'] = max(used, control.get('peak_tokens', 0))
        crossed = (type(used) is int and type(window) is int and window > 0 and limit is not None
                   and rollover_growth(control, used, window, limit))
        if isinstance(used, (int, float)) and used > 0:
            control['valid_usage'] = True
            control['context_tokens'] = used
            if type(window) is int and window > 0:
                control['telemetry_window'] = window
        _settle_children(control, payload, now)
        # Native children of unknown fate defer this segment's managed rollover, up to WAIT_LIMIT.
        waiting = bool(control.get('children') or control.get('unknown_children'))
        due = crossed or control['phase'] == 'requested'
        if waiting and due and control.get('threshold') is not None:
            since = control.setdefault('waiting_since', now)
            if now - since >= WAIT_LIMIT:
                waiting = False
                if not control.get('wait_expired'):
                    control['wait_expired'] = True
                    names = ', '.join(_waiting_on(control, payload)) or 'unnamed workers'
                    try:
                        _history_line(control, f'Rollover waited {WAIT_LIMIT // 60} min for workers; '
                                               f'proceeding, which ends: {names}.')
                    except OSError as exc:
                        _record_error(control, exc)
        elif not waiting:
            control.pop('waiting_since', None)
        if waiting:
            control['degraded'] = 'Native workers are active; using native compaction until they finish'
            if due and event == 'Stop' and control.get('waiting_since'):
                note = waiting_note(_waiting_on(control, payload), control['waiting_since'] + WAIT_LIMIT)
                output = {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': note}}
        elif control.get('threshold') is not None:
            if control['phase'] == 'requested' and event == 'Stop':
                control['phase'] = 'ready'
                output = {'continue': False, 'stopReason': 'Token Kit is carrying this session into fresh context.'}
            elif control['phase'] == 'requested' and event == 'SessionStart' and control.get('summary_note'):
                # Native compaction ran mid-summary; restate the request. The Stop boundary still decides.
                output = {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': control['summary_note']}}
            elif event == 'PreCompact' and crossed and control['phase'] == 'running':
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
                                       task_root / 'asks.md' if assignment and Path(assignment).resolve().parent == task_root.resolve() else None)
                control['summary_note'] = note
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
        if event == 'Stop' and not output:
            try:
                output = _asks_nudge(control)
            except OSError:
                output = {}
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
