#!/usr/bin/env python3
"""One bounded, restartable Likes sync tick. No scheduler is installed by this script."""
import argparse
import importlib.util
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid


def save_state(file, state):
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_name(file.name + '.tmp-' + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, file)
        fd = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def read_rows(file, count):
    rows = [json.loads(line) for line in file.read_text().splitlines() if line.strip()] if file.exists() else []
    ids = [row.get('id') for row in rows]
    if len(rows) != count or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Archive rows do not match receipt count or unique string IDs')
    return rows


def tick(root, username, collect, now, interval=21600, mode="snapshot", baseline_rows=None, overlap=200):
    """Caller holds the state lock. collect(snapshot_directory) returns a CLI receipt.

    Logical time is injected for deterministic qualification; production uses time.time.
    Snapshot mode performs full refreshes. New-likes mode stops at a frozen
    known-ID overlap and starts the next cycle at the head; neither installs a scheduler.
    """
    if mode not in ('snapshot', 'new-likes') or type(overlap) is not int or overlap < 1:
        raise ValueError('Invalid synchronization mode/overlap')
    root = Path(root)
    state_file = root / 'sync-state.json'
    if state_file.exists():
        state = json.loads(state_file.read_text())
        if state.get('version') != 1 or state.get('username') != username or state.get('mode', 'snapshot') != mode:
            raise ValueError('State version/account mismatch')
    else:
        state = {'version': 1, 'username': username, 'mode': mode, 'nextDue': 0, 'pending': None,
                 'completedSnapshots': 0, 'members': {}, 'runs': [], 'failures': 0}
        if mode == 'new-likes':
            if not baseline_rows:
                raise ValueError('New-likes mode requires a non-empty baseline on its first run')
            for row in baseline_rows:
                ident = row.get('id')
                if not isinstance(ident, str) or not ident or ident in state['members']:
                    raise ValueError('Invalid or duplicate baseline ID')
                state['members'][ident] = {'record': row, 'firstSeen': now, 'lastSeen': None,
                                           'status': 'baseline', 'misses': 0}
            state['completedCycles'] = 0
    if now < state['nextDue']:
        return {'status': 'not_due', 'nextDue': state['nextDue']}
    if state['pending'] is None:
        state['pending'] = uuid.uuid4().hex
        if mode == 'new-likes':
            state['cycleKnownIds'] = list(state['members'])  # Freeze across all continuation ticks.
        save_state(state_file, state)  # Commit identity before starting the child.
    snapshot = root / 'snapshots' / state['pending']
    snapshot.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        receipt = collect(snapshot)
        if receipt.get('source') != 'likes' or receipt.get('username') != username:
            raise ValueError('Receipt account/source mismatch')
        count = receipt.get('count')
        if type(count) is not int or count < 0 or type(receipt.get('complete')) is not bool:
            raise ValueError('Invalid receipt count/completion')
        if receipt['complete'] and (receipt.get('httpStatus') or receipt.get('stopReason') in ('rate_limited', 'http_error')):
            raise ValueError('Error receipt cannot prove snapshot completion')
        rows = read_rows(snapshot / 'archive.jsonl', count)
        retry = receipt.get('retryAfterSeconds')
        if retry is not None and (type(retry) not in (int, float) or not math.isfinite(retry) or retry < 0):
            raise ValueError('Invalid retry delay')
    except Exception as error:
        state['failures'] += 1
        state['nextDue'] = now + min(3600, 60 * 2 ** min(state['failures'] - 1, 6))
        event = {'at': now, 'status': 'error', 'errorType': type(error).__name__, 'nextDue': state['nextDue']}
        state['runs'].append(event)
        save_state(state_file, state)
        return event

    ids = {row['id'] for row in rows}
    added = sum(i not in state['members'] for i in ids)
    for row in rows:
        old = state['members'].get(row['id'], {})
        state['members'][row['id']] = {'record': row, 'firstSeen': old.get('firstSeen', now),
                                      'lastSeen': now, 'status': 'observed', 'misses': 0}
    absent = 0
    overlap_reached = False
    if mode == 'new-likes':
        known = set(state['cycleKnownIds'])
        required = min(overlap, len(known))
        overlap_reached = required > 0 and len(rows) >= required and all(row['id'] in known for row in rows[-required:])
        overlap_reached = overlap_reached and not receipt.get('httpStatus') and receipt.get('stopReason') not in ('rate_limited', 'http_error')
    if mode == 'new-likes' and (receipt['complete'] or overlap_reached):
        state['completedCycles'] += 1
        state['pending'] = None
        state.pop('cycleKnownIds', None)
        state['failures'] = 0
        delay, status = interval, 'incremental_complete'
    elif receipt['complete']:
        for ident, item in state['members'].items():
            if ident not in ids:
                item['misses'] += 1
                item['status'] = 'not_observed' if item['misses'] >= 2 else 'pending_missing'
                absent += 1
        state['completedSnapshots'] += 1
        state['pending'] = None
        delay, status = interval, 'snapshot_complete'
        state['failures'] = 0
    elif receipt.get('stopReason') == 'rate_limited':
        # Never retry earlier than the remote deadline. No busy-loop retry.
        delay, status = max(60, retry if retry is not None else 900), 'rate_limited'
    elif receipt.get('httpStatus'):
        state['failures'] += 1
        delay, status = min(3600, 60 * 2 ** min(state['failures'] - 1, 6)), 'http_error'
    else:
        delay, status = 60, 'partial'
        state['failures'] = 0
    state['nextDue'] = now + delay
    event = {'at': now, 'status': status, 'count': count, 'added': added, 'absent': absent,
             'completedSnapshots': state['completedSnapshots'], 'nextDue': state['nextDue']}
    if mode == 'new-likes':
        event.update(completedCycles=state['completedCycles'], boundary='known_overlap' if overlap_reached else ('api_end' if receipt['complete'] else 'pending'))
    state['runs'].append(event)
    save_state(state_file, state)
    return event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('username')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--max-pages', type=int, default=5)
    parser.add_argument('--mode', choices=['snapshot', 'new-likes'], default='snapshot')
    parser.add_argument('--baseline-archive', type=Path)
    parser.add_argument('--overlap', type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 100000:
        parser.error('--max-pages must be between 1 and 100000')
    args.username = args.username.lstrip('@').lower()
    if not re.fullmatch(r'[a-z0-9_]{1,15}', args.username):
        parser.error('username must be an X handle')
    os.umask(0o077)
    safe_path = Path(__file__).with_name('twitter-likes-safe.py')
    spec = importlib.util.spec_from_file_location('safe_likes', safe_path)
    safe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(safe)
    root = args.state_dir.resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    state_path = safe.archive_path(root / 'sync-state.json')
    held = safe.acquire_locks([state_path])
    def collect(snapshot):
        command = [sys.executable, str(safe_path), args.username, '--max-pages', str(args.max_pages),
                   '--output-file', str(snapshot / 'archive.jsonl'), '--resume-file', str(snapshot / 'resume.json')]
        # The child inherits the outer lock as well as its per-archive locks.
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, pass_fds=held)
        if result.returncode:
            raise RuntimeError('Collector failed')  # Do not log arbitrary browser error text.
        return json.loads(result.stdout)
    try:
        baseline = None
        if args.mode == 'new-likes' and not state_path.exists():
            if not args.baseline_archive:
                raise ValueError('--baseline-archive is required for the first new-likes run')
            baseline = [json.loads(line) for line in args.baseline_archive.read_text().splitlines() if line.strip()]
        print(json.dumps(tick(root, args.username, collect, time.time(), mode=args.mode,
                              baseline_rows=baseline, overlap=args.overlap)))
    finally:
        for fd in held:
            os.close(fd)


if __name__ == '__main__':
    main()
