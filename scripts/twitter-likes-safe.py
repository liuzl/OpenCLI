#!/usr/bin/env python3
"""Bounded, read-only Likes archive launcher for POSIX local filesystems."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import stat
import sys


def archive_path(raw):
    requested = Path(raw).expanduser().absolute()
    # Refuse leaf aliases: checkpoint rename would replace a symlink instead of
    # updating its target, and path locks cannot cover arbitrary hardlink names.
    if requested.is_symlink():
        raise ValueError(f"Refusing symlink file: {requested}")
    target = requested.resolve()
    if target.name.lower().endswith('.opencli-lock'):
        raise ValueError('The .opencli-lock suffix is reserved for persistent lock files')
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"Expected a regular file with one link: {target}")
    return target


def acquire_locks(paths):
    """Return inheritable flock descriptors; never unlink the lock inodes."""
    held = []
    try:
        for target in sorted(set(paths)):
            target.parent.mkdir(parents=True, exist_ok=True)
            lock = str(target) + '.opencli-lock'
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            held.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ValueError(f"Unsafe lock file: {lock}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(f"Archive or checkpoint is already in use: {target}") from None
            os.set_inheritable(fd, True)
        return held
    except BaseException:
        for fd in held:
            os.close(fd)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('username', nargs='?')
    parser.add_argument('--output-file', required=True)
    parser.add_argument('--resume-file', required=True)
    parser.add_argument('--audit-dir', type=Path)
    parser.add_argument('--max-pages', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 100000:
        parser.error('--max-pages must be between 1 and 100000')
    os.umask(0o077)
    output = archive_path(args.output_file)
    resume = archive_path(args.resume_file)
    if str(output).lower() == str(resume).lower():
        raise ValueError('Archive and checkpoint must be different files')
    held = acquire_locks([output, resume])
    try:
        # Recheck after acquiring both locks; cooperating writers cannot race us.
        if archive_path(output) != output or archive_path(resume) != resume:
            raise ValueError('Archive path changed while acquiring locks')
        root = Path(__file__).resolve().parent.parent
        node = shutil.which('node')
        if not node:
            raise ValueError('node is not available on PATH')
        command = [node, str(root / 'dist/src/main.js'), 'twitter', 'likes']
        if args.username:
            command.append(args.username)
        command += ['--all', '--max-pages', str(args.max_pages),
                    '--output-file', str(output), '--resume-file', str(resume),
                    '--format', 'json', '--trace', 'off']
        if args.audit_dir:
            command += ['--audit-dir', str(args.audit_dir.absolute())]
        # Exec retains the flock descriptors in Node. SIGKILL releases them in
        # the kernel; there is no stale PID/lease timeout or separate child left.
        os.execv(node, command)
    finally:
        for fd in held:
            os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'Likes archive launcher: {error}', file=sys.stderr)
        sys.exit(1)
