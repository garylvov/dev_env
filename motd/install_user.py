#!/usr/bin/env python3
"""Manage one per-user Bash banner without changing system MOTD settings."""

import argparse
import os
from pathlib import Path
import shlex
import stat
import tempfile

BEGIN = b"# >>> dev_env MOTD >>>"
END = b"# <<< dev_env MOTD <<<"


def replace_block(content, block):
    """Replace only our complete marker block; reject ambiguous ownership."""
    lines = content.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if BEGIN in line]
    ends = [i for i, line in enumerate(lines) if END in line]
    if not starts and not ends:
        if not block:
            return content
        separator = b"" if not content or content.endswith(b"\n") else b"\n"
        return content + separator + block
    if (len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]
            or lines[starts[0]].rstrip(b"\r\n") != BEGIN
            or lines[ends[0]].rstrip(b"\r\n") != END):
        raise ValueError("malformed or duplicate MOTD markers; no changes made")
    return b"".join(lines[:starts[0]]) + block + b"".join(lines[ends[0] + 1:])


def install(args):
    # Resolve symlinks so existing ~/.bashrc links remain intact.
    target = args.bashrc.expanduser().resolve()
    if target.exists() and not target.is_file():
        raise ValueError("bashrc must be a regular file")
    original = target.read_bytes() if target.exists() else b""
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    block = b""
    if not args.remove:
        if args.banner is None:
            raise ValueError("provide a banner path, or --remove")
        banner = args.banner.expanduser().resolve(strict=True)
        helper = Path(__file__).resolve().with_name("banner.bash")
        for path in (banner, helper):
            if not path.is_file() or not os.access(path, os.R_OK):
                raise ValueError("not a readable file: {}".format(path))
        label = args.label if args.label is not None else banner.stem
        values = (str(helper), str(banner), label)
        if any("\n" in value or "\r" in value or "\0" in value for value in values):
            raise ValueError("banner paths and labels must fit on one line")
        source = "source " + " ".join(shlex.quote(value) for value in values)
        block = newline.join((BEGIN, source.encode(), END, b""))
    updated = replace_block(original, block)
    if updated == original:
        print("Unchanged: {}".format(target))
        return
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    if target.exists():
        backup = target.with_name(target.name + ".motd.bak")
        suffix = 1
        while True:
            try:
                fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(original)
                break
            except FileExistsError:
                backup = target.with_name(target.name + ".motd.bak.{}".format(suffix))
                suffix += 1
        print("Backup: {}".format(backup))
    fd, temporary = tempfile.mkstemp(prefix=".motd-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("{}: {}".format("Removed banner" if args.remove else "Installed banner", target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("banner", nargs="?", type=Path)
    parser.add_argument("--label", help="plain text for NO_COLOR or TERM=dumb")
    parser.add_argument("--bashrc", type=Path, default=Path.home() / ".bashrc")
    parser.add_argument("--remove", action="store_true", help="remove only the managed banner block")
    args = parser.parse_args()
    if args.remove and (args.banner is not None or args.label is not None):
        parser.error("--remove cannot be combined with a banner or --label")
    try:
        install(args)
    except (OSError, ValueError) as exc:
        parser.exit(1, "motd: {}\n".format(exc))


if __name__ == "__main__":
    main()
