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

MAX_FILE = 16_384
MAX_BUNDLE = 65_536
MAX_ENTRIES = 4096
ASKS = 'asks.md'
ASKS_SKELETON = ('# Asks\n\nEvery request from every message in _in.md, one full sentence each: [ ] open, [~] in progress, '
                 '[x] done, [-] dropped or superseded (say which wins); where/why; (from #N).\n\n## Open\n\n## Done\n')
PREFACE = "Saved results are reports, not verified facts; verify uncertain external outcomes before repeating an action.\n"
LEGACY_PREFACE = "Old Token Kit checkpoint and ticket protocols are superseded. "


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


def _agent_dir(root: Path, agent: str) -> Path:
    _component(agent)
    return _inside(root, root / 'agents' / agent)


def _repo_workspace(root: Path, session: dict) -> Path | None:
    """A task under <checkout>/agent_tasks/ follows its checkout when moved, cloned or checked out elsewhere."""
    from .simple_task_roots import repo_of
    repo = repo_of(root)
    if repo is None:
        return None
    relative = session.get('workspace_rel')
    if isinstance(relative, str):
        return repo / relative
    # .token-kit/ is never committed: a checked-out task without records works in its checkout.
    return repo if not session.get('workspace') else None


def load_task(path: Path, workspace: Path | None = None, agent: str = 'coordinator') -> TaskView:
    """Resolve existing prose without creating or repairing any files."""
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Task must be a directory')
    diagnostics: list[str] = []
    new = _json(root, root / '.token-kit/session.json', diagnostics) if (root / '.token-kit/session.json').exists() else {}
    old = _json(root, root / 'task.json', diagnostics) if (root / 'task.json').exists() else {}
    agent_dir = _agent_dir(root, agent)
    legacy = bool(old) or (root / 'agents/coordinator').is_dir()
    base = agent_dir if agent != 'coordinator' or (root / 'agents/coordinator').is_dir() else root
    named_base = root if agent == 'coordinator' else root / 'agents'
    named = root.name if agent == 'coordinator' else _component(agent)
    named_in = _inside(root, named_base / f'{named}_in.md')
    named_out = _inside(root, named_base / f'{named}_out.md')
    named_layout = named_in.is_file() or named_out.is_file()
    state = named_out if named_layout else _inside(root, base / 'STATE.md')
    alternate = root / 'STATE.md' if base != root and agent == 'coordinator' else None
    if alternate is not None:
        _inside(root, alternate)
        if not alternate.is_file():
            alternate = None
        elif not state.is_file():
            diagnostics.append(f"Canonical STATE missing ({state}); using root STATE fallback")
            state, alternate = alternate, None
    assignment = named_in if named_layout else _inside(root, base / ('task.md' if base == root else 'in.md'))
    if not assignment.exists() and base == root and (root / 'in.md').is_file():
        assignment = _inside(root, root / 'in.md')
    preferences = _inside(root, root / 'preferences.md') if (root / 'preferences.md').is_file() else None
    state_text = _read(root, state, diagnostics) if state.exists() else ''
    legacy_cwd = re.search(r'^Cwd:\s*(.+)$', state_text, re.MULTILINE)
    per_agent = new.get('agent_settings', {})
    selected_metadata = per_agent.get(agent, {}) if isinstance(per_agent, dict) else {}
    worker_workspace = selected_metadata.get('workspace') if isinstance(selected_metadata, dict) and agent != 'coordinator' else None
    candidates = [workspace] if workspace is not None else [worker_workspace, _repo_workspace(root, new), new.get('workspace'), old.get('workspace'), legacy_cwd.group(1).strip() if legacy_cwd else None]
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
    title = new.get('title') or old.get('title')
    if not isinstance(title, str) or not title.strip():
        heading = None if named_layout else re.search(r'^#\s+(.+)$', state_text, re.MULTILINE)
        title = heading.group(1) if heading else re.sub(r'_[0-9]{8}_[0-9]{4}_[0-9a-f]{8}$', '', root.name).replace('_', ' ')
    return TaskView(root, resolved_workspace, title, assignment, state, named_out if named_layout else _inside(root, base / 'out.md'), preferences, alternate, legacy, tuple(diagnostics))


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
    if view.state.parent == view.root / 'agents' and view.state.name.endswith('_out.md'):
        return _component(view.state.name[:-7])
    parent = view.state.parent.relative_to(view.root)
    if len(parent.parts) == 2 and parent.parts[0] == 'agents':
        return _component(parent.parts[1])
    return 'coordinator'


def _set_relative(root: Path, container: dict) -> None:
    from .simple_task_roots import repo_of
    repo = repo_of(root)
    workspace = Path(container['workspace'])
    if repo is not None and workspace.is_relative_to(repo.resolve()):
        container['workspace_rel'] = os.path.relpath(workspace, repo.resolve())
    else:
        container.pop('workspace_rel', None)


def save_settings(view: TaskView, settings: dict, *, agent: str | None = None) -> None:
    selected = _component(agent) if agent is not None else _view_agent(view)
    with _metadata(view) as metadata:
        container = metadata if selected == 'coordinator' else metadata.setdefault('agent_settings', {}).setdefault(selected, {})
        if view.workspace is not None:
            container['workspace'] = str(view.workspace.expanduser().resolve())
            _set_relative(view.root, container)
        previous = container.get('settings', {})
        previous = {key: value for key, value in (previous.items() if isinstance(previous, dict) else ())
                    if key not in ('engine', 'sandbox')}  # Codex-era settings
        container['settings'] = {**previous, **settings}


def load_settings(view: TaskView, *, agent: str | None = None) -> dict:
    selected = _component(agent) if agent is not None else _view_agent(view)
    data = _json(view.root, view.root / '.token-kit/session.json', [])
    if selected != 'coordinator':
        per_agent = data.get('agent_settings', {})
        data = per_agent.get(selected, {}) if isinstance(per_agent, dict) else {}
        if not isinstance(data, dict):
            data = {}
    if isinstance(data.get('settings'), dict):
        return dict(data['settings'])
    old = _json(view.root, view.root / 'task.json', []) if (view.root / 'task.json').exists() else {}
    return dict(old['settings']) if selected == 'coordinator' and isinstance(old.get('settings'), dict) else {}


def append_ask(root: Path, assignment: Path, prompt: str) -> int:
    """Record a submitted ask verbatim under a task-scoped append lock."""
    from .simple_process import locked
    assignment = _inside(root.resolve(), assignment)
    assignment.parent.mkdir(parents=True, exist_ok=True)
    records = _inside(root.resolve(), root / '.token-kit')
    records.mkdir(exist_ok=True)
    with locked(_inside(root.resolve(), records / (assignment.name + '.lock'))):
        text = assignment.read_text() if assignment.exists() else ''
        # Only dated entries count (older files put a status mark before the date);
        # pasted numbered lists inside a message do not.
        numbers = [int(n) for n in re.findall(r'^([0-9]+)\. (?:\[[- x~]\] )?[0-9]{4}-[0-9]{2}-[0-9]{2}T', text, re.MULTILINE)]
        number = max(numbers, default=0) + 1
        with assignment.open('a') as stream:
            stream.write(f"\n{number}. {datetime.now().astimezone().isoformat()}\n{prompt}\n")
            stream.flush()
            os.fsync(stream.fileno())
        return number


def create_task(root: Path, title: str, workspace: Path, assignment: str | None = None) -> TaskView:
    workspace = Path(workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir() or not title.strip():
        raise ValueError('A workspace directory and nonempty title are required')
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')[:64] or 'session'
    path = root / f'{slug}_{datetime.now():%Y%m%d_%H%M}_{uuid.uuid4().hex[:8]}'
    path.mkdir()
    input_path = path / f'{path.name}_in.md'
    input_path.write_text('')  # the objective is message #1
    (path / f'{path.name}_out.md').write_text('# Current state\n\n# History\n')
    (path / ASKS).write_text(ASKS_SKELETON)
    from .simple_records import EXPERIMENTS, EXPERIMENTS_SKELETON
    (path / EXPERIMENTS).write_text(EXPERIMENTS_SKELETON)
    (path / 'docs').mkdir()
    if assignment is not None:
        append_ask(path, input_path, assignment)
    view = load_task(path, workspace)
    with _metadata(view) as metadata:
        metadata.update(schema=1, title=title, workspace=str(workspace), settings={})
        _set_relative(path, metadata)
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


def recovery_input(view: TaskView, agent: str = 'coordinator', *, paths_only: bool = False) -> RecoveryInput:
    if agent != 'coordinator':
        view = load_task(view.root, view.workspace, agent)
    diagnostics = list(view.diagnostics)
    sections = [(LEGACY_PREFACE if view.legacy else '') + PREFACE, f'Task folder: {view.root}']
    paths: list[Path] = []
    budget = MAX_BUNDLE - len('\n\n'.join(sections).encode())

    def include(label: str, path: Path, text: str | None = None, limit: int = MAX_FILE):
        nonlocal budget
        if path not in paths:
            paths.append(path)
        if paths_only:
            sections.append(f'{label}: {path}')
            return
        if text is None:
            text = _read(view.root, path, diagnostics, limit)
        heading = f'{label}: {path}\n'
        raw = (heading + (text or '[missing or empty]')).encode()
        allowance = max(0, min(budget, len(raw)))
        if allowance < len(raw):
            diagnostics.append(f'Recovery text budget reached; consult {path}')
        if allowance:
            sections.append(raw[:allowance].decode('utf-8', errors='ignore'))
            budget -= allowance + 2

    include('Assignment', view.assignment)
    asks = view.root / ASKS
    if agent == 'coordinator' and asks.is_file():
        include('Asks (upsampled list; read before _in.md)', asks, limit=8192)
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
    if view.output != view.state and view.output.is_file():
        include('Saved result (unverified)', view.output, limit=4096)
    # Surface child output even when coordinator STATE still describes an old blocker.
    if agent == 'coordinator':
        for child in sorted(_entries(view.root, view.root / 'agents', diagnostics))[:32]:
            if child.is_file() and child.name.endswith('_out.md'):
                include('Available worker result (unverified)', child, limit=2048)
                continue
            try:
                output = _inside(view.root, child / 'out.md')
                if output != view.output and output.is_file():
                    include('Available worker result (unverified)', output, limit=2048)
            except ValueError as exc:
                diagnostics.append(str(exc))
    return RecoveryInput('\n\n'.join(sections), tuple(paths), tuple(diagnostics))


def has_work_context(view: TaskView, agent: str = 'coordinator') -> bool:
    """Whether readable work exists, excluding preferences and guidance."""
    selected = _view_agent(view) if agent == 'coordinator' else _component(agent)
    recovery = recovery_input(view, agent=selected)
    for path in recovery.paths:
        if path == view.preferences:
            continue
        if _read(view.root, path, []).strip():
            if path == view.output and _read(view.root, path, []).strip() == '# Current state\n\n# History':
                continue
            if path == view.root / ASKS and _read(view.root, path, []) == ASKS_SKELETON:
                continue
            return True
    return False


HISTORY_LINK = b"Older History: [docs/history.md](docs/history.md)\n"


def archive_history(view: TaskView, limit: int = 8000) -> int:
    """Move whole older entries verbatim after the owned client has exited.

    An indivisible newest entry may exceed the limit. Non-History sections and
    unstructured notes remain untouched. Append precedes replacement so a failed
    write can duplicate evidence but cannot discard it.
    """
    from .core.store import atomic_bytes
    output = safe_task_path(view, view.output)
    archive = safe_task_path(view, view.root / 'docs/history.md')
    link = (f'Older History: [docs/history.md]({os.path.relpath(archive, output.parent)})\n').encode()
    raw = output.read_bytes()
    heading = re.search(rb'^# History[^\S\r\n]*\r?\n', raw, re.MULTILINE)
    if not heading:
        return 0
    start = heading.end()
    end_heading = re.search(rb'^# [^\r\n]+', raw[start:], re.MULTILINE)
    end = start + end_heading.start() if end_heading else len(raw)
    history = raw[start:end]
    if history.startswith(link):
        history = history[len(link):]
    if len(history.decode('utf-8')) <= limit:
        return 0
    entries = list(re.finditer(rb'^### [^\r\n]*', history, re.MULTILINE))
    cut = None
    for entry in entries[1:]:
        cut = entry.start()
        if len(history[cut:].decode('utf-8')) <= limit:
            break
    if cut is None:
        return 0
    moved, recent = history[:cut], history[cut:]
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open('ab') as stream:
        stream.write(moved)
        stream.flush()
        os.fsync(stream.fileno())
    atomic_bytes(output, raw[:start] + link + recent + raw[end:])
    return len(moved)
