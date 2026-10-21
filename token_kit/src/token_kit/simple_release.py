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



def _short_link(home: Path, dispatcher: Path) -> list:
    """`tk` beside `token-kit`, unless something else already owns that name."""
    short = home / '.local/bin/tk'
    if short.is_symlink() and os.readlink(short) != str(dispatcher):
        return []
    if short.exists() and not short.is_symlink():
        return []
    return [(short, {'kind': 'symlink', 'target': str(dispatcher)})]

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
               *_short_link(home, dispatcher),
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


def _shell_block(home: Path, directory: Path) -> tuple[Path, dict]:
    rc = home / '.bashrc'
    if rc.is_symlink():
        raise ValueError('Shell configuration is symlinked; refusing to replace it')
    body = rc.read_text() if rc.exists() else ''
    block = (f'{OPEN}\nexport PATH={shlex.quote(str(directory))}:"$PATH"\n{CLOSE}')
    if OPEN in body or CLOSE in body:
        if body.count(OPEN) != 1 or body.count(CLOSE) != 1 or body.index(OPEN) > body.index(CLOSE):
            raise ValueError('Ambiguous launcher block in shell configuration')
        left, right = body.index(OPEN), body.index(CLOSE) + len(CLOSE)
        previous = (f'{OPEN}\n'
                    'if [[ -z ${TOKEN_KIT_RUN:-} || -n ${TOKEN_KIT_SIMPLE_RUN:-} ]]; then\n'
                    f'  case ":$PATH:" in *:{shlex.quote(str(directory))}:*) ;; *) export PATH={shlex.quote(str(directory))}:"$PATH" ;; esac\n'
                    f'fi\n{CLOSE}')
        if body[left:right] not in (previous, block):
            raise ValueError('Customized launcher block preserved')
        body = body[:left] + block + body[right:]
    else:
        body += '\n' + block + '\n'
    return rc, {'kind': 'file', 'hex': body.encode().hex(),
                'mode': rc.stat().st_mode & 0o777 if rc.exists() else 0o644}


def fresh_install_plan(release: Path, home: Path) -> list[dict]:
    """Install the launcher when no previous Token Kit checkout exists."""
    release, home = release.resolve(), home.absolute()
    new = release / 'token_kit/src/token_kit/bin/token-kit'
    if not new.is_file():
        raise ValueError(f'Launcher not found: {new}')
    directory = home / '.local/share/token-kit/simple-bin'
    dispatcher = directory / 'token-kit'
    marker = '# Token Kit release dispatcher\n'
    script = '#!/usr/bin/env bash\n' + marker + f'exec {shlex.quote(str(new))} "$@"\n'
    current = _capture(dispatcher)
    installed = bytes.fromhex(current['hex']).decode(errors='replace') if current['kind'] == 'file' else None
    if installed is not None and installed != script:
        if marker not in installed or f'exec {shlex.quote(str(new))} "$@"' not in installed:
            # A dispatcher that routes to a pinned legacy release must not lose that route.
            raise ValueError(f'Existing dispatcher differs: {dispatcher}; pass --legacy-root to upgrade it')
        script = installed  # Already dispatches to this release (with its legacy routes).
    link = home / '.local/bin/token-kit'
    if link.exists() and not link.is_symlink():
        raise ValueError(f'Existing command is not a symlink; refusing to replace it: {link}')
    changes = [(dispatcher, {'kind': 'file', 'hex': script.encode().hex(), 'mode': 0o755}),
               (link, {'kind': 'symlink', 'target': str(dispatcher)}),
               *_short_link(home, dispatcher),
               _shell_block(home, directory)]
    return [{'path': str(path), 'before': _capture(path), 'after': after}
            for path, after in changes if _capture(path) != after]


def launcher_install_plan(release: Path, legacy: Path, home: Path) -> list[dict]:
    """Upgrade cached old command paths as well as the per-user entrypoint."""
    release, legacy, home = release.resolve(), legacy.resolve(), home.absolute()
    new = release / 'token_kit/src/token_kit/bin/token-kit'
    old = legacy / 'token_kit/src/token_kit/bin/token-kit'
    pinned = old.with_name('token-kit-legacy')
    if new == old or not new.is_file() or not old.is_file():
        raise ValueError('Distinct existing new and legacy launcher paths are required')
    marker = '# Token Kit release dispatcher\n'
    original = _capture(old)
    if original['kind'] != 'file':
        raise ValueError('Legacy launcher must be a regular file')
    changes = []
    if not pinned.exists():
        # This sibling keeps the original script-relative Python source unchanged.
        if old.read_bytes() != new.read_bytes():
            raise ValueError('Unrecognized legacy launcher; refusing to replace it')
        changes.append((pinned, original))
    elif marker not in old.read_text() or pinned.is_symlink() or pinned.read_bytes() != new.read_bytes():
        raise ValueError('Existing legacy backup or launcher is unrecognized')
    script = ('#!/usr/bin/env bash\n' + marker + 'set -e\n'
              'case "${1:-}" in run|continue|pick|find|list|status|--help|-h|"")\n'
              f'  exec {shlex.quote(str(new))} "$@" ;;\nesac\n'
              'if [[ -n ${TOKEN_KIT_RUN:-} && -z ${TOKEN_KIT_SIMPLE_RUN:-} ]]; then\n'
              f'  exec {shlex.quote(str(pinned))} "$@"\nfi\n'
              'case "${1:-}" in hook|hooks|prompts|probe|census|config|legacy)\n'
              f'  exec {shlex.quote(str(pinned))} "$@" ;;\nesac\n'
              f'exec {shlex.quote(str(new))} "$@"\n')
    directory = home / '.local/share/token-kit/simple-bin'
    dispatcher = directory / 'token-kit'
    record = {'kind': 'file', 'hex': script.encode().hex(), 'mode': 0o755}
    changes.extend(((dispatcher, record), (old, record)))
    # The historical installer location also forwards to this installer.
    old_install = legacy / 'token_kit/install.sh'
    old_install_backup = old_install.with_name('install-legacy.sh')
    installer_marker = '# Token Kit installer forwarding shim\n'
    if old_install.is_file():
        if old_install.is_symlink() or old_install_backup.is_symlink():
            raise ValueError('Installer paths must not be symlinks')
        if not old_install_backup.exists():
            changes.append((old_install_backup, _capture(old_install)))
        elif installer_marker not in old_install.read_text():
            raise ValueError('Existing installer backup is unrecognized')
        forward = ('#!/usr/bin/env bash\n' + installer_marker +
                   f'exec bash {shlex.quote(str(release / "token_kit/install.sh"))} '
                   f'--legacy-root {shlex.quote(str(legacy))} "$@"\n')
        changes.append((old_install, {'kind': 'file', 'hex': forward.encode().hex(), 'mode': 0o755}))
    changes.extend(((home / '.local/bin/token-kit', {'kind': 'symlink', 'target': str(dispatcher)}),
                    *_short_link(home, dispatcher),
                    _shell_block(home, directory)))
    return [{'path': str(path), 'before': _capture(path), 'after': after}
            for path, after in changes if _capture(path) != after]


def main() -> None:
    import argparse
    from datetime import datetime
    parser = argparse.ArgumentParser(description='Install the simplified Token Kit launcher; leave task and client configuration intact.')
    parser.add_argument('--legacy-root', type=Path, help='previous Token Kit checkout to keep serving hooks; omit for a fresh install')
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[3]
    plan = (launcher_install_plan(release, args.legacy_root, args.home) if args.legacy_root
            else fresh_install_plan(release, args.home))
    if args.dry_run:
        print(json.dumps({'changed': [row['path'] for row in plan]}, indent=2))
    elif not plan:
        print('Token Kit launcher is already installed.')
    else:
        backup = args.home / '.local/share/token-kit' / ('launcher-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        rollback = apply_activation(plan, backup)
        print('Token Kit installed. Existing command paths now use the simplified launcher.' if args.legacy_root
              else f'Token Kit installed: {args.home / ".local/bin/token-kit"} (open a new shell or source ~/.bashrc).')
        print(f'Rollback: python3.11 {shlex.quote(str(rollback))}')


if __name__ == '__main__':
    main()
