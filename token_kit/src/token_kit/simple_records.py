"""Hook-written worker records and experiment launches; no model tokens spent."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from .simple_process import locked

EXPERIMENTS = 'experiments.md'
EXPERIMENTS_SKELETON = ('# Experiments\n\n- [running|?|done|abandoned] E<n> <date> <question> cmd: <exact command> '
                        'where: <job id / output path> (from #N); closed lines add verdict: <one line> -> <doc link>, '
                        'or why: when abandoned. Hooks append [?] lines for launches under ## Unlabeled; label them and move them to ## Open.\n\n'
                        '## Open\n\n## Closed\n')
UNLABELED = '## Unlabeled'
# Commands that start long-lived work. Searched per shell segment.
LAUNCH_PATTERNS = (r'\bsystemd-run\b', r'\bsbatch\b', r'\bsrun\b', r'\bfleet\b', r'\bnohup ', r'\bsetsid ',
                   r'\btorchrun\b', r'\baccelerate\s+launch\b', r'\bray\s+job\s+submit\b',
                   r'(?:^|[\s/=])train[\w.-]*\.py\b', r'(?:^|\s)-m\s+(?:[\w.]+\.)?train\w*\b')
# Segments that only inspect: reading a train script or asking a scheduler for status.
QUERY_PATTERNS = (
    r'^\s*(?:sudo\s+)?(?:cat|less|more|head|tail|grep|rg|sed|awk|ls|cd|wc|diff|git|vim?|nano|echo|printf|find|stat|file|which|type|man)\b',
    r'\bpytest\b',
    r'\bfleet\s+(?:status|ls|list|logs?|show|ps|top|tail|watch)\b',
    r'\b(?:squeue|sacct|sinfo|scontrol\s+show)\b',
    r'\bsystemctl\s+(?:--user\s+)?(?:status|is-active|is-failed|list-\S+|show|cat)\b',
    r'\bjournalctl\b', r'\bpgrep\b', r'(?:^|\s)--help\b',
)
MAX_COMMAND = 300


def is_launch(command: str) -> bool:
    for segment in re.split(r'&&|\|\||[;|\n]', command):
        if (any(re.search(p, segment) for p in LAUNCH_PATTERNS)
                and not any(re.search(p, segment) for p in QUERY_PATTERNS)):
            return True
    return False


def _stamp() -> str:
    return datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M')


def _inside(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Record path escapes task folder: {path}')
    return path


def _lock(root: Path, name: str):
    records = _inside(root, root / '.token-kit')
    records.mkdir(exist_ok=True)
    return locked(records / (name + '.lock'))


def record_launch(root: Path, command: str, by: str) -> int:
    """Append an unlabeled [?] line at the end of the file, never rewriting what the model edits."""
    path = _inside(root, root / EXPERIMENTS)
    shown = ' '.join(command.split())
    if len(shown) > MAX_COMMAND:
        shown = shown[:MAX_COMMAND] + '...'
    with _lock(root, EXPERIMENTS):
        if not path.exists():
            path.write_text(EXPERIMENTS_SKELETON)
        text = path.read_text()
        number = max((int(n) for n in re.findall(r'^- \[[^\]]*\] E([0-9]+)\b', text, re.M)), default=0) + 1
        headings = re.findall(r'^## .*$', text, re.M)
        lead = '' if text.endswith('\n') or not text else '\n'
        if not headings or headings[-1].strip() != UNLABELED:
            lead += '\n' + UNLABELED + '\n\n'
        with path.open('a') as stream:
            stream.write(f'{lead}- [?] E{number} {_stamp()} unlabeled launch by {by}: {shown}\n')
        return number


def experiments_note(root: Path) -> str:
    """One kickoff line naming open experiment ids and status, never their contents."""
    try:
        text = (root / EXPERIMENTS).read_text()
    except (OSError, UnicodeDecodeError):
        return ''
    sections = re.findall(r'^## (?:Open|Unlabeled)\b(.*?)(?=^## |\Z)', text, re.M | re.S)
    labels = []
    for status, number in re.findall(r'^- \[([^\]]*)\] (E[0-9]+)\b', ''.join(sections), re.M):
        status = status.strip()
        labels.append(number + {'running': ' (running)', '?': ' (unlabeled)'}.get(status, ''))
    if not labels:
        return ''
    shown = ', '.join(labels[:20]) + (f', +{len(labels) - 20} more' if len(labels) > 20 else '')
    return f'Open experiments: {shown} -- see {root / EXPERIMENTS}\n'


def worker_name(tool_input: dict, agent_id: str) -> str:
    name = tool_input.get('name') if isinstance(tool_input.get('name'), str) else ''
    if not name.strip():
        description = tool_input.get('description') if isinstance(tool_input.get('description'), str) else ''
        name = (description or tool_input.get('subagent_type') or 'worker') + '-' + agent_id[:6]
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', name.strip()).strip('-.')[:80] or 'worker-' + agent_id[:6]


def record_worker_start(root: Path, tool_input: dict, response: dict) -> dict:
    """Write agents/<name>_in.md from an Agent tool call; returns the agentId mapping entry."""
    agent_id = str(response.get('agentId') or '')
    name = worker_name(tool_input, agent_id)
    model = str(response.get('resolvedModel') or tool_input.get('model') or 'inherited')
    mode = 'background' if response.get('status') == 'async_launched' else 'foreground'
    folder = _inside(root, root / 'agents')
    folder.mkdir(exist_ok=True)
    path = _inside(root, folder / f'{name}_in.md')
    with _lock(root, path.name):
        header = '' if path.exists() else f'# {name}\n'
        with path.open('a') as stream:
            stream.write(f'{header}\n### start {_stamp()}\nagentId: {agent_id} | subagent_type: '
                         f'{tool_input.get("subagent_type") or "general-purpose"} | model: {model} | {mode}\n\n'
                         f'{tool_input.get("prompt", "")}\n')
    return {'name': name, 'model': model}


def record_worker_final(root: Path, output: Path, worker: dict, report: str, transcript: str | None) -> None:
    """Append the final report to agents/<name>_out.md and one History line to the task output."""
    name = worker['name']
    path = _inside(root, root / 'agents' / f'{name}_out.md')
    path.parent.mkdir(exist_ok=True)
    with _lock(root, path.name):
        header = '' if path.exists() else f'# {name}\n'
        with path.open('a') as stream:
            stream.write(f'{header}\n### final {_stamp()}\ntranscript: {transcript or "unknown"}\n\n{report.strip()}\n')
    first = next((line.strip() for line in report.splitlines() if line.strip()), '(empty report)')
    if len(first) > 120:
        first = first[:120] + '...'
    output = _inside(root, output)
    with _lock(root, output.name):
        with output.open('a') as stream:
            stream.write(f'- worker {name} ({worker.get("model", "unknown")}) finished: {first} -> agents/{name}_out.md\n')
