"""Best-effort usage hooks. A safe boundary requests rollover, never a closure."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from token_kit.simple_process import locked, read, write, identity

EVENTS = ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop', 'SubagentStart', 'SubagentStop', 'PreCompact')


def hooks():
    command = shlex.join([sys.executable, str(Path(__file__).resolve())])
    return {event: [{'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]}]
            for event in EVENTS}


def codex_config():
    result = ['--enable', 'hooks']
    for event, groups in hooks().items():
        command = json.dumps(groups[0]['hooks'][0]['command'])
        value = '[{ hooks = [{ type = "command", command = ' + command + ', timeout = 5 }] }]'
        result.extend(['-c', f'hooks.{event}={value}'])
    return result


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


def owned_hook_ancestry(run: Path) -> bool:
    """Prove this hook descends from the owned Codex process, not a nested CLI."""
    record = read(run / 'process.json')
    owned = record.get('child_pid')
    if not isinstance(owned, int) or identity(owned) != record.get('child_identity'):
        return False
    try:
        executable = Path(f'/proc/{owned}/exe').resolve(strict=True)
        if 'codex' not in executable.name.lower():
            return False
        pid = os.getppid()
        for _ in range(64):
            actual = Path(f'/proc/{pid}/exe').resolve(strict=True)
            if actual == executable or 'codex' in actual.name.lower():
                return pid == owned and identity(pid) == record['child_identity']
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            pid = int(fields[1])
            if pid <= 1:
                return False
    except (OSError, ValueError, IndexError):
        return False
    return False


def initialize(run: Path, options, expected_session: str | None, state: Path):
    write(run / 'control.json', {'run_id': run.name, 'engine': options.engine,
          'expected_session': expected_session, 'armed': False, 'phase': 'running',
          'threshold': options.rollover, 'context_window': options.context_window,
          'state': str(state), 'children': [], 'activity': 0, 'valid_usage': False})


def handle(run: Path, payload: dict, nonce: str) -> dict:
    with locked(run / 'control.lock'):
        control = read(run / 'control.json')
        if nonce != control.get('run_id') or nonce != run.name:
            return {}
        expected = control.get('expected_session')
        event = payload.get('hook_event_name')
        if control.get('engine') == 'codex':
            from token_kit.simple_adapters import codex_root_session
            if not owned_hook_ancestry(run):
                return {}
            child_notice = (event in ('SubagentStart', 'SubagentStop') and
                            control.get('armed') and expected and
                            payload.get('session_id') == expected and
                            not payload.get('parent_thread_id') and
                            not payload.get('parent_session_id'))
            if not child_notice:
                proven = codex_root_session(payload)
                if not proven or (expected and expected != proven):
                    return {}
                expected = proven
                control['expected_session'] = proven
        # Unknown and child events never bind or control a parent. Environment
        # inheritance alone is not identity evidence.
        if not expected or payload.get('session_id') != expected:
            return {}
        event = payload.get('hook_event_name')
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
            return {}
        if event == 'PostToolUse' and control['phase'] == 'running':
            control['activity'] += 1
        sample = {}
        transcript = payload.get('transcript_path')
        if transcript:
            try:
                if control['engine'] == 'codex':
                    from token_kit.simple_adapters import codex_usage_sample
                    sample = codex_usage_sample(Path(transcript))
                else:
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
        crossed = isinstance(used, (int, float)) and limit is not None and used >= limit
        if isinstance(used, (int, float)) and used > 0:
            control['valid_usage'] = True
            control['context_tokens'] = used
        output = {}
        if event == 'UserPromptSubmit' and not control.get('guidance_presented'):
            guidance = os.environ.get('TOKEN_KIT_SIMPLE_GUIDANCE')
            if guidance:
                output = {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': guidance}}
                control['guidance_presented'] = True
        # Native children of unknown fate defer this segment's managed rollover.
        if control.get('children') or control.get('unknown_children'):
            control['degraded'] = 'Native workers are active; using native compaction until they stop'
        elif control.get('threshold') is not None:
            if control['phase'] == 'requested' and event == 'Stop':
                control['phase'] = 'ready'
                output = {'continue': False, 'stopReason': 'Token Kit is carrying this session into fresh context.'}
            elif event == 'PreCompact':
                control['phase'] = 'ready'
                control['reason'] = 'parent_compaction'
                # Do not veto native compaction. Supervisor handles verified shutdown.
            elif crossed and control['phase'] == 'running' and event in ('PostToolUse', 'Stop'):
                control['phase'] = 'requested'
                note = ('Please briefly update ' + control['state'] +
                        ' with useful context, remaining work, constraints, and uncertain external work. '
                        'Use ordinary file edits; the launcher will handle the context rollover.')
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
