"""Scoped process ownership; records never certify completion of task work."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid


def read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(path: Path):
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def identity(pid: int) -> str | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def dead(record: dict, prefix: str) -> bool:
    if record.get('host') != socket.gethostname():
        return False
    pid = record.get(prefix + '_pid')
    expected = record.get(prefix + '_identity')
    if not isinstance(pid, int) or not expected:
        return False
    actual = identity(pid)
    if actual is not None:
        return actual != expected
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    return False


def safe(root: Path, path: Path) -> Path:
    if not path.is_relative_to(root):
        raise ValueError('Process metadata escapes task folder')
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise ValueError(f'Process metadata must not be symlinked: {item}')
    return path


class Slot:
    def __init__(self, root: Path, agent: str):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', agent):
            raise ValueError('Agent must be one safe folder name')
        self.root = root.resolve()
        self.agent = agent
        self.directory = safe(self.root, self.root / '.token-kit' / 'slots' / agent)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.current = self.directory / 'current.json'
        self.handles = []
        self.record = {}
        self.bridge = None

    def acquire(self, resume=False, timeout=15):
        # SH fences every old EX launch/continue route without serializing new slots.
        fence = safe(self.root, self.root / '.continue.lock').open('a')
        self.handles.append(fence)
        try:
            fcntl.flock(fence, fcntl.LOCK_SH | fcntl.LOCK_NB)
            slot = (self.directory / 'launch.lock').open('a')
            self.handles.append(slot)
            try:
                fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not resume:
                    raise ValueError('This agent already has a managed session')
                previous = read(self.current)
                if previous.get('host') != socket.gethostname():
                    raise ValueError('Continue on the original host; process identity is uncertain')
                if dead(previous, 'supervisor'):
                    raise ValueError('Session owner unavailable; cannot request cooperative shutdown')
                target = self.root / '.token-kit' / 'runs' / str(previous.get('run_id')) / 'control.json'
                control = read(target)
                if not control.get('armed') or control.get('threshold') is None:
                    raise ValueError('Safe continuation unavailable for this session; stop it in its original terminal, then continue')
                request = {'run_id': previous.get('run_id'), 'request_id': uuid.uuid4().hex}
                request_path = self.directory / 'continue.json'
                write(request_path, request)
                deadline = time.monotonic() + timeout
                try:
                    while True:
                        try:
                            fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise ValueError('Session has not reached a safe stop boundary; no duplicate launched')
                            time.sleep(.1)
                finally:
                    with locked(self.directory / 'request.lock'):
                        current_request = read(request_path)
                        if current_request.get('request_id') == request['request_id']:
                            request_path.unlink(missing_ok=True)
            previous = read(self.current)
            if previous and previous.get('status') != 'exited' and not dead(previous, 'child'):
                raise ValueError('Previous client is alive or its launch outcome is uncertain; no duplicate launched')
            # Audit legacy processes only on task admission. Parsed lifecycle is irrelevant.
            with locked(safe(self.root, self.root / '.lock')):
                agents = safe(self.root, self.root / 'agents')
                entries = 0
                if agents.is_dir():
                    for directory in agents.iterdir():
                        if not directory.is_dir():
                            continue
                        runs = safe(self.root, directory / 'runs')
                        if not runs.is_dir():
                            continue
                        for run in runs.iterdir():
                            entries += 1
                            if entries > 10000:
                                raise ValueError('Legacy process inventory exceeds bounded admission check')
                            path = safe(self.root, run / 'run.json')
                            if not path.exists():
                                continue
                            if path.stat().st_size > 65536:
                                raise ValueError('Legacy process record too large to establish ownership')
                            old = read(path)
                            if not isinstance(old, dict):
                                raise ValueError('Malformed legacy process record; ownership uncertain')
                            # Other simple slots own their own process locks. Old
                            # supervisors anywhere in this task share the EX fence.
                            if old.get('simple_launcher'):
                                continue
                            if old.get('status') not in ('starting', 'running', 'interrupted', 'exited', 'reconciled'):
                                raise ValueError('Unrecognized legacy process record; ownership uncertain')
                            if old.get('status') in ('starting', 'running', 'interrupted'):
                                if not dead(old, 'child') or not dead(old, 'supervisor'):
                                    raise ValueError('Legacy client or supervisor may still be running; continue with its original launcher')
            return self
        except Exception:
            self.close()
            raise

    def claim(self, engine: str) -> Path:
        run_id = uuid.uuid4().hex
        run = safe(self.root, self.root / '.token-kit' / 'runs' / run_id)
        run.mkdir(parents=True)
        self.record = {'run_id': run_id, 'agent_id': self.agent, 'engine': engine,
                       'host': socket.gethostname(), 'supervisor_pid': os.getpid(),
                       'supervisor_identity': identity(os.getpid()), 'status': 'starting',
                       'schema_version': 1, 'simple_launcher': True}
        if (self.root / 'task.json').exists():
            self.bridge = safe(self.root, self.root / 'agents' / self.agent / 'runs' / run_id / 'run.json')
        self.publish()
        return run

    def publish(self, **fields):
        self.record.update(fields)
        # Publish the old-compatible durable claim before exposing executable child.
        with locked(safe(self.root, self.root / '.lock')):
            if self.bridge:
                write(self.bridge, self.record)
            write(self.current, self.record)

    def spawn(self, argv, cwd, env, **stdio):
        incoming, release = os.pipe()
        child = None
        try:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                      str(incoming), *argv], cwd=cwd, env=env,
                                     pass_fds=(incoming,), **stdio)
            os.close(incoming)
            incoming = -1
            start = identity(child.pid)
            if start is None:
                raise ValueError('Cannot verify child identity before release')
            self.publish(child_pid=child.pid, child_identity=start, status='running')
            write(self.root / '.token-kit' / 'runs' / self.record['run_id'] / 'process.json', self.record)
            os.write(release, b'1')
            return child
        except Exception:
            if child is None:
                self.publish(status='exited', exit_code=1)
            raise
        finally:
            if incoming >= 0:
                os.close(incoming)
            os.close(release)

    def close(self):
        for handle in reversed(self.handles):
            handle.close()
        self.handles = []


if __name__ == '__main__':
    descriptor = int(sys.argv[1])
    permitted = os.read(descriptor, 1) == b'1'
    os.close(descriptor)
    if permitted:
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    sys.exit(125)
