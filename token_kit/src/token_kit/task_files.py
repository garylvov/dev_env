"""Plain task files and bounded recovery, independent of worker lifecycles."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .simple_types import RecoveryInput, TaskView
from .simple_guidance import CONCURRENCY

MAX_FILE = 16_384
MAX_BUNDLE = 65_536
MAX_ENTRIES = 4096
PREFACE = ("Token Kit uses ordinary editable files. Historical Token Kit checkpoint, ticket, "
           "closure, retirement, reconciliation, required STATE-format and model-routing protocols "
           "are superseded; no such commands or approvals are required. Actual user/site instructions "
           "still apply. Preserve useful notes and write results when useful. Verify uncertain external "
           "outcomes before repeating an action. Saved results are reports, not independently verified facts.\n\n" +
           CONCURRENCY)


# Exact frozen legacy generated text. Marker presence alone never proves ownership.
_LEGACY_POLICY_BODY = 'Token Kit is guidance, not approval. Main orchestrates; workers execute. Follow repo rules.\nBookkeeping drift is advisory. Repair and continue authorized work without asking.\nOwn identity. Resume: token-kit resume TASK --agent ID.\nSTATE: Objective/Completed/Evidence/Unresolved/Next; ~200 words. Compression only: dated verbatim STATE in\nhistorical_state.md; no routine append. Read selectively; checkpoint evidence/IDs.\n\nSmart coordinator; bounded Mid work. Announce route/tier/model/reason/changes. Scoped overrides beat map; isolate siblings.\nMap supersedes default prose, not explicit assignments. Escalate for complexity, not delay: checkpoint/report to the main thread.\n\nNative prepare/bind --parent; status TASK. Checkpoint, ticketed rollover/complete, confirm stop.\nNever retry uncertain spawn. Managed recovery: structured compaction or exact legacy run/runtime evidence;\nAuto-restart excludes unknown stops/errors/manual interrupts. Dead PID proves no external completion; parent exit no native closure.\nOrphans: worker retire per agent_trigger_matrix.md.\nRetired means neither native closure nor success/retry authority. Replace remaining work only.\nVerify unknown operations before retry; report once, never poll.'

def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Task path escapes task folder: {path}")
    return path


def safe_task_path(view: TaskView, path: Path) -> Path:
    """Reject paths/symlink targets outside this canonical task."""
    return _inside(view.root, path)


def _component(name: str) -> str:
    if not name or name in (".", "..") or Path(name).name != name or "\\" in name:
        raise ValueError("Agent name must be one folder component")
    return name


def _read(root: Path, path: Path, diagnostics: list[str], limit: int = MAX_FILE) -> str:
    try:
        _inside(root, path)
        with path.open('rb') as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            diagnostics.append(f"Excerpt truncated; full text: {path}")
        return raw[:limit].decode('utf-8', errors='replace')
    except (OSError, ValueError) as exc:
        diagnostics.append(f"Unavailable {path}: {exc}")
        return ""


def _json(root: Path, path: Path, diagnostics: list[str]) -> dict:
    text = _read(root, path, diagnostics, 262_144)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    if text:
        diagnostics.append(f"Invalid optional metadata: {path}")
    return {}


def _strip_wrapper(text: str) -> str:
    # Only complete, leading, positively identified generated wrappers are removed.
    opening = re.compile(r"<!-- token-kit worker policy(?: v[\w.-]+)? -->\n")
    closing = "<!-- /token-kit worker policy -->"
    while opening.match(text):
        end = text.find(closing)
        if end < 0 or '<!-- token-kit worker policy' in text[opening.match(text).end():end]:
            break
        wrapped = text[opening.match(text).end():end].rstrip('\n')
        if wrapped != _LEGACY_POLICY_BODY:
            # Unknown versions, task-specific preferences, and user edits remain
            # intact. The recovery preface supersedes only obsolete protocol.
            break
        rest = text[end + len(closing):].lstrip('\n')
        if rest.startswith('Token Kit record: '):
            record, sep, body = rest.partition('\n\n')
            try:
                metadata = json.loads(record[len('Token Kit record: '):])
            except ValueError:
                break
            if not sep or not isinstance(metadata, dict) or not isinstance(metadata.get('task'), str):
                break
            rest = body
        text = rest
    return text


def _agent_dir(root: Path, agent: str) -> Path:
    _component(agent)
    candidates = [root / 'agents' / agent, root / 'lanes' / agent]
    for path in candidates:
        _inside(root, path)
        if path.is_dir():
            return path
    return candidates[0]


def load_task(path: Path, workspace: Path | None = None, agent: str = 'coordinator') -> TaskView:
    """Resolve existing prose without creating or repairing any files."""
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Task must be a directory')
    diagnostics: list[str] = []
    new = _json(root, root / '.token-kit/session.json', diagnostics) if (root / '.token-kit/session.json').exists() else {}
    old = _json(root, root / 'task.json', diagnostics) if (root / 'task.json').exists() else {}
    agent_dir = _agent_dir(root, agent)
    legacy = bool(old) or (root / 'lanes').is_dir() or (root / 'agents/coordinator').is_dir()
    base = agent_dir if agent != 'coordinator' or (root / 'agents/coordinator').is_dir() else root
    state = _inside(root, base / 'STATE.md')
    alternate = root / 'STATE.md' if base != root and agent == 'coordinator' else None
    if alternate is not None:
        _inside(root, alternate)
        if not alternate.is_file():
            alternate = None
        elif not state.is_file():
            diagnostics.append(f"Canonical STATE missing ({state}); using root STATE fallback")
            state, alternate = alternate, None
    assignment = _inside(root, base / ('task.md' if base == root else 'in.md'))
    if not assignment.exists() and base == root and (root / 'in.md').is_file():
        assignment = _inside(root, root / 'in.md')
    preferences = next((_inside(root, root / name) for name in ('preferences.md', 'trigger_pyramid.md') if (root / name).is_file()), None)
    state_text = _read(root, state, diagnostics) if state.exists() else ''
    legacy_cwd = re.search(r'^Cwd:\s*(.+)$', state_text, re.MULTILINE)
    per_agent = new.get('agent_settings', {})
    selected_metadata = per_agent.get(agent, {}) if isinstance(per_agent, dict) else {}
    worker_workspace = selected_metadata.get('workspace') if isinstance(selected_metadata, dict) and agent != 'coordinator' else None
    candidates = [workspace] if workspace is not None else [worker_workspace, new.get('workspace'), old.get('workspace'), legacy_cwd.group(1).strip() if legacy_cwd else None]
    resolved_workspace = None
    for candidate in candidates:
        if not isinstance(candidate, (str, Path)) or not str(candidate).strip():
            continue
        candidate = Path(candidate).expanduser()
        if candidate.is_absolute() and candidate.is_dir():
            resolved_workspace = candidate.resolve()
            break
        diagnostics.append(f"Workspace unavailable: {candidate}")
    if resolved_workspace is None:
        diagnostics.append('No usable workspace recorded; select one before project work')
    labels_path = root / '.token-kit/labels.json'
    labels = _json(root, labels_path, diagnostics) if labels_path.exists() else {}
    title = labels.get('title') or new.get('title') or old.get('title')
    if not isinstance(title, str) or not title.strip():
        heading = re.search(r'^#\s+(.+)$', state_text, re.MULTILINE)
        title = heading.group(1) if heading else root.name
    return TaskView(root, resolved_workspace, title, assignment, state, _inside(root, base / 'out.md'), preferences, alternate, legacy, tuple(diagnostics))


@contextmanager
def _metadata(view: TaskView):
    directory = _inside(view.root, view.root / '.token-kit')
    directory.mkdir(exist_ok=True)
    lock = _inside(view.root, directory / 'metadata.lock')
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = _inside(view.root, directory / 'session.json')
        if path.exists():
            # Never replace unreadable metadata and lose another owner's process record.
            with path.open('rb') as source:
                raw = source.read(262_145)
            if len(raw) > 262_144:
                raise ValueError('Session metadata exceeds size limit')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Session metadata must be an object')
        else:
            data = {}
        yield data
        fd, temporary = tempfile.mkstemp(prefix='.session-', dir=directory)
        try:
            with os.fdopen(fd, 'w') as destination:
                json.dump(data, destination, indent=2)
                destination.write('\n')
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _view_agent(view: TaskView) -> str:
    """Agent selection is encoded by the TaskView working STATE location."""
    parent = view.state.parent.relative_to(view.root)
    if len(parent.parts) == 2 and parent.parts[0] in ('agents', 'lanes'):
        return _component(parent.parts[1])
    return 'coordinator'


def save_settings(view: TaskView, settings: dict, *, agent: str | None = None) -> None:
    selected = _component(agent) if agent is not None else _view_agent(view)
    with _metadata(view) as metadata:
        container = metadata if selected == 'coordinator' else metadata.setdefault('agent_settings', {}).setdefault(selected, {})
        if view.workspace is not None:
            container['workspace'] = str(view.workspace.expanduser().resolve())
        previous = container.get('settings', {})
        container['settings'] = {**(previous if isinstance(previous, dict) else {}), **settings}


def load_settings(view: TaskView, *, agent: str | None = None) -> dict:
    selected = _component(agent) if agent is not None else _view_agent(view)
    data = _json(view.root, view.root / '.token-kit/session.json', [])
    if selected != 'coordinator':
        per_agent = data.get('agent_settings', {})
        data = per_agent.get(selected, {}) if isinstance(per_agent, dict) else {}
        if not isinstance(data, dict):
            data = {}
    # A saved new settings object is the direct pointer; never enumerate historical
    # runs after the first successful continuation has persisted resolved settings.
    if isinstance(data.get('settings'), dict):
        return dict(data['settings'])
    old = _json(view.root, view.root / 'task.json', []) if (view.root / 'task.json').exists() else {}
    if selected == 'coordinator' and isinstance(old.get('settings'), dict):
        return dict(old['settings'])
    directory = _agent_dir(view.root, selected) / 'runs'
    candidates = []
    for folder in _entries(view.root, directory, []):
        record = _json(view.root, folder / 'run.json', [])
        if not record or record.get('engine') not in ('claude', 'codex'):
            continue
        try:
            created = datetime.fromisoformat(str(record.get('created_at', ''))).timestamp()
        except (ValueError, OverflowError):
            created = 0.0
        candidates.append((created, folder.name, record))
    previous = max(candidates, key=lambda item: item[:2])[2] if candidates else {}
    settings = {key: previous[key] for key in ('engine', 'model', 'effort', 'yolo',
                'context_window', 'max_rollovers', 'max_rollovers_explicit') if key in previous}
    if previous.get('rollover_tokens') is not None:
        settings['rollover'] = previous['rollover_tokens']
    if previous.get('no_rollover') is True:
        settings['rollover'] = None
    if settings.get('max_rollovers') == 10 and settings.get('max_rollovers_explicit') is None:
        settings.pop('max_rollovers')  # The historical, untagged default is not user intent.
    usage = previous.get('usage')
    if isinstance(usage, dict) and isinstance(usage.get('current_model'), str) and usage['current_model'] != 'unknown':
        settings['model'] = usage['current_model']
    return settings


def create_task(root: Path, title: str, workspace: Path, assignment: str | None = None) -> TaskView:
    workspace = Path(workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir() or not title.strip():
        raise ValueError('A workspace directory and nonempty title are required')
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')[:64] or 'session'
    path = root / f'{slug}_{datetime.now():%Y%m%d_%H%M}_{uuid.uuid4().hex[:8]}'
    path.mkdir()
    (path / 'task.md').write_text(assignment + '\n' if assignment is not None else '')
    (path / 'STATE.md').write_text('')
    view = load_task(path, workspace)
    with _metadata(view) as metadata:
        metadata.update(schema=1, title=title, workspace=str(workspace), settings={})
    return load_task(path, workspace)


def _entries(root: Path, directory: Path, diagnostics: list[str]) -> list[Path]:
    try:
        _inside(root, directory)
        paths = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(paths) >= MAX_ENTRIES:
                    diagnostics.append(f'Directory excerpt limited to {MAX_ENTRIES} entries: {directory}')
                    break
                paths.append(Path(entry.path))
        return paths
    except (OSError, ValueError):
        return []


def mark_messages_presented(view: TaskView, message_ids: tuple[str, ...], agent: str = 'coordinator') -> None:
    """Record only the IDs captured in a prompt that was actually injected.

    This records presentation attempted, not external execution or acknowledgment.
    Call after the client handshake, never by rebuilding a later recovery bundle.
    """
    _component(agent)
    if not message_ids:
        return
    if any(not isinstance(identifier, str) or not identifier for identifier in message_ids):
        raise ValueError('Message IDs must be nonempty strings')
    with _metadata(view) as metadata:
        cursors = metadata.setdefault('presented_messages', {})
        old_ids = cursors.get(agent, [])
        cursors[agent] = sorted(set(old_ids) | set(message_ids))


def recovery_input(view: TaskView, agent: str = 'coordinator', mark_presented: bool = False) -> RecoveryInput:
    if agent != 'coordinator':
        view = load_task(view.root, view.workspace, agent)
    diagnostics = list(view.diagnostics)
    sections = [PREFACE, f'Task folder: {view.root}']
    paths: list[Path] = []
    budget = MAX_BUNDLE - len('\n\n'.join(sections).encode())

    def include(label: str, path: Path, text: str | None = None, limit: int = MAX_FILE):
        nonlocal budget
        if path not in paths:
            paths.append(path)
        if text is None:
            text = _strip_wrapper(_read(view.root, path, diagnostics, limit))
        heading = f'{label}: {path}\n'
        raw = (heading + (text or '[missing or empty]')).encode()
        allowance = max(0, min(budget, len(raw)))
        if allowance < len(raw):
            diagnostics.append(f'Recovery text budget reached; consult {path}')
        if allowance:
            sections.append(raw[:allowance].decode('utf-8', errors='ignore'))
            budget -= allowance + 2

    include('Assignment', view.assignment)
    include('Working STATE (authoritative working path)', view.state)
    if not _read(view.root, view.state, []).strip():
        fallback = _inside(view.root, view.root / '.token-kit' / 'slots' / _component(agent) / 'state-snapshot.md')
        if fallback.is_file():
            include('Previous saved STATE fallback; working notes are missing or empty', fallback)
    if view.alternate_state:
        other = _read(view.root, view.alternate_state, diagnostics)
        current = _read(view.root, view.state, diagnostics)
        if other != current:
            include('Divergent legacy root STATE (preserved; do not silently overwrite either copy)', view.alternate_state, other, 8192)
    if view.preferences:
        include('Optional task preferences; current explicit choices take precedence', view.preferences, limit=4096)
    if view.output.is_file():
        include('Saved result (unverified)', view.output, limit=4096)
    agent_dir = _agent_dir(view.root, agent)
    acknowledged: set[str] = set()
    if (agent_dir / 'agent.json').exists():
        metadata = _json(view.root, agent_dir / 'agent.json', diagnostics)
        checkpoint = metadata.get('latest_checkpoint')
        if isinstance(checkpoint, str):
            try:
                checkpoint = _component(checkpoint)
                manifest = _json(view.root, agent_dir / 'checkpoints' / checkpoint / 'manifest.json', diagnostics)
                acknowledged = {x for x in manifest.get('incorporated_messages', []) if isinstance(x, str)}
                if not _read(view.root, view.state, [], MAX_FILE).strip():
                    snapshot = agent_dir / 'checkpoints' / checkpoint / 'STATE.md'
                    if snapshot.is_file():
                        include('Historical snapshot fallback; working STATE is missing/empty', snapshot)
            except (ValueError, TypeError):
                diagnostics.append('Could not read legacy incorporated-message IDs')
    session = _json(view.root, view.root / '.token-kit/session.json', diagnostics) if (view.root / '.token-kit/session.json').exists() else {}
    cursors = session.get('presented_messages', {})
    cursor = cursors.get(agent, []) if isinstance(cursors, dict) else []
    presented = {x for x in cursor if isinstance(x, str)} if isinstance(cursor, list) else set()
    ids: list[str] = []
    message_dirs = [agent_dir / 'messages']
    if agent == 'coordinator' and view.root / 'messages' not in message_dirs:
        message_dirs.append(view.root / 'messages')
    message_entries = [entry for directory in message_dirs for entry in _entries(view.root, directory, diagnostics)]
    for entry in sorted(message_entries):
        if entry.suffix != '.json' or entry.stem in acknowledged | presented | set(ids):
            continue
        row = _json(view.root, entry, diagnostics)
        identifier, content = row.get('message_id'), row.get('text')
        if identifier != entry.stem or not isinstance(content, str):
            continue
        if budget < 1024 or len(ids) >= 8:
            diagnostics.append(f'More messages remain available in {agent_dir / "messages"}')
            break
        label = 'Historical worker observation; no lifecycle action required' if row.get('source') == 'worker_lifecycle' else 'Steering (presentation attempted; delivery may be uncertain)'
        include(label, entry, _strip_wrapper(content[:2048]), 2048)
        ids.append(identifier)
    if presented:
        sections.append(f'Previously presented steering remains in the task message folders; it is not a fresh instruction.')
    # Surface child output even when coordinator STATE still describes an old blocker.
    if agent == 'coordinator':
        for folder in ('agents', 'lanes'):
            for child in sorted(_entries(view.root, view.root / folder, diagnostics))[:32]:
                try:
                    output = _inside(view.root, child / 'out.md')
                    if output != view.output and output.is_file():
                        include('Available worker result (unverified)', output, limit=2048)
                except ValueError as exc:
                    diagnostics.append(str(exc))
    if mark_presented and ids:
        try:
            mark_messages_presented(view, tuple(ids), agent)
        except (OSError, ValueError, TypeError) as exc:
            diagnostics.append(f'Presentation cursor could not be saved; messages may repeat: {exc}')
    return RecoveryInput('\n\n'.join(sections), tuple(paths), tuple(ids), tuple(diagnostics))


def has_work_context(view: TaskView, agent: str = 'coordinator') -> bool:
    """Whether readable work exists, excluding labels, preferences and guidance."""
    selected = _view_agent(view) if agent == 'coordinator' else _component(agent)
    recovery = recovery_input(view, agent=selected)
    for path in recovery.paths:
        if path == view.preferences:
            continue
        if path.suffix == '.json':
            if path.stem not in recovery.message_ids:
                continue
            message = _json(view.root, path, [])
            # Lifecycle notices alone are not objectives for another paid turn.
            if message.get('source') == 'worker_lifecycle':
                continue
            text = message.get('text')
            if isinstance(text, str) and _strip_wrapper(text).strip():
                return True
        elif _strip_wrapper(_read(view.root, path, [])).strip():
            return True
    return False
