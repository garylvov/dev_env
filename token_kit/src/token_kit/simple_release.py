"""Reversible per-user command switch; never edits either release's source."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
from .core.store import atomic_bytes

OPEN = '# >>> token-kit simple launcher >>>'
CLOSE = '# <<< token-kit simple launcher <<<'


def _capture(path: Path) -> dict:
    if path.is_symlink():
        return {'kind': 'symlink', 'target': os.readlink(path)}
    if not path.exists():
        return {'kind': 'absent'}
    if not path.is_file():
        raise ValueError(f'Not a file: {path}')
    return {'kind': 'file', 'hex': path.read_bytes().hex(), 'mode': path.stat().st_mode & 0o777}


def _restore(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    if record['kind'] == 'absent':
        path.unlink(missing_ok=True)
    elif record['kind'] == 'symlink':
        import uuid
        temporary = path.parent / ('.token-kit-link-' + uuid.uuid4().hex)
        temporary.symlink_to(record['target'])
        os.replace(temporary, path)
    else:
        # Replace a symlink itself, never write through it.
        atomic_bytes(path, bytes.fromhex(record['hex']))
        path.chmod(record['mode'])


def activation_plan(release: Path, legacy: Path, home: Path) -> list[dict]:
    release, legacy, home = release.resolve(), legacy.resolve(), home.absolute()
    new = release / 'token_kit/src/token_kit/bin/token-kit'
    old = legacy / 'token_kit/src/token_kit/bin/token-kit'
    if not new.is_file() or not old.is_file() or new == old:
        raise ValueError('Distinct existing new and legacy launcher paths are required')
    directory = home / '.local/share/token-kit/simple-bin'
    dispatcher = directory / 'token-kit'
    script = ('#!/usr/bin/env bash\n# Token Kit release dispatcher\nset -e\n'
              'if [[ -n ${TOKEN_KIT_RUN:-} && -z ${TOKEN_KIT_SIMPLE_RUN:-} ]]; then\n'
              f'  exec {shlex.quote(str(old))} "$@"\nfi\n'
              'case "${1:-}" in hook|prompts|probe|census|config|legacy)\n'
              f'  exec {shlex.quote(str(old))} "$@" ;;\nesac\n'
              f'exec {shlex.quote(str(new))} "$@"\n')
    bashrc = home / '.bashrc'
    if bashrc.is_symlink():
        raise ValueError('Shell configuration is symlinked; leave it intact and select the release explicitly')
    body = bashrc.read_text() if bashrc.exists() else ''
    if OPEN in body or CLOSE in body:
        raise ValueError('An activation block already exists; use its rollback before switching releases')
    block = (f'\n{OPEN}\n'
             'if [[ -z ${TOKEN_KIT_RUN:-} || -n ${TOKEN_KIT_SIMPLE_RUN:-} ]]; then\n'
             f'  case ":$PATH:" in *:{shlex.quote(str(directory))}:*) ;; *) export PATH={shlex.quote(str(directory))}:"$PATH" ;; esac\n'
             f'fi\n{CLOSE}\n')
    link = home / '.local/bin/token-kit'
    changes = [(dispatcher, {'kind': 'file', 'hex': script.encode().hex(), 'mode': 0o755}),
               (link, {'kind': 'symlink', 'target': str(dispatcher)}),
               (bashrc, {'kind': 'file', 'hex': (body + block).encode().hex(),
                        'mode': (bashrc.stat().st_mode & 0o777) if bashrc.exists() else 0o644})]
    return [{'path': str(path), 'before': _capture(path), 'after': after} for path, after in changes]


def apply_activation(plan: list[dict], backup: Path) -> Path:
    for row in plan:
        if _capture(Path(row['path'])) != row['before']:
            raise ValueError('Activation preview changed; no files modified')
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    journal = backup / 'activation.json'
    journal.write_text(json.dumps(plan, indent=2) + '\n')
    journal.chmod(0o600)
    # The rollback code is standalone, so it works even after this release moves.
    rollback = backup / 'rollback.py'
    rollback.write_text('''#!/usr/bin/env python3
import json, os
from pathlib import Path
rows = json.loads((Path(__file__).parent / 'activation.json').read_text())
def capture(p):
    if p.is_symlink(): return {'kind':'symlink','target':os.readlink(p)}
    if not p.exists(): return {'kind':'absent'}
    return {'kind':'file','hex':p.read_bytes().hex(),'mode':p.stat().st_mode & 0o777}
for row in rows:
    if capture(Path(row['path'])) not in (row['before'], row['after']):
        raise SystemExit('Changed since activation; refusing to overwrite: ' + row['path'])
for row in reversed(rows):
    p, before = Path(row['path']), row['before']
    if capture(p) == before: continue
    p.unlink(missing_ok=True)
    if before['kind'] == 'symlink': p.symlink_to(before['target'])
    elif before['kind'] == 'file':
        p.write_bytes(bytes.fromhex(before['hex']))
        p.chmod(before['mode'])
print('Previous Token Kit command and shell configuration restored.')
''')
    rollback.chmod(0o700)
    applied = []
    try:
        for row in plan:
            if _capture(Path(row['path'])) != row['before']:
                raise ValueError('Activation target changed during publication')
            applied.append(row)
            _restore(Path(row['path']), row['after'])
    except Exception:
        for row in reversed(applied):
            _restore(Path(row['path']), row['before'])
        raise
    return rollback
