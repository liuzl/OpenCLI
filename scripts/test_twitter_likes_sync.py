import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('likes_sync', Path(__file__).with_name('twitter-likes-sync.py'))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def collector(ids, complete=True, **extra):
    def collect(directory):
        rows = [{'id': ident, 'text': 'fixture ' + ident} for ident in ids]
        (directory / 'archive.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return {'source': 'likes', 'username': 'fixture', 'count': len(ids), 'complete': complete, **extra}
    return collect


def state(root):
    return json.loads((Path(root) / 'sync-state.json').read_text())


class SyncTest(unittest.TestCase):
    def test_accelerated_seven_days_and_restarts(self):
        with tempfile.TemporaryDirectory() as root:
            calls = 0
            for step in range(29):  # t=0 through t=7 days, every six hours.
                now = 1000 + step * 21600
                ids = ['original'] + [f'new-{day}' for day in range(step // 4 + 1)]
                underlying = collector(ids)
                def collect(directory):
                    nonlocal calls
                    calls += 1
                    return underlying(directory)
                result = sync.tick(root, 'fixture', collect, now)
                self.assertEqual(result['status'], 'snapshot_complete')
                self.assertEqual(sync.tick(root, 'fixture', collect, now)['status'], 'not_due')
                self.assertEqual(sync.tick(root, 'fixture', collect, now - 3600)['status'], 'not_due')
            saved = state(root)
            self.assertEqual(calls, 29)
            self.assertEqual(saved['completedSnapshots'], 29)
            self.assertEqual(len(saved['members']), 9)
            self.assertEqual(len(saved['runs']), 29)
            self.assertTrue(all(x['status'] == 'observed' for x in saved['members'].values()))

    def test_unlike_absence_and_relike_do_not_delete_content(self):
        with tempfile.TemporaryDirectory() as root:
            sync.tick(root, 'fixture', collector(['a', 'b']), 0, interval=10)
            sync.tick(root, 'fixture', collector(['a']), 10, interval=10)
            self.assertEqual(state(root)['members']['b']['status'], 'pending_missing')
            sync.tick(root, 'fixture', collector(['a']), 20, interval=10)
            self.assertEqual(state(root)['members']['b']['status'], 'not_observed')
            self.assertEqual(state(root)['members']['b']['record']['text'], 'fixture b')
            sync.tick(root, 'fixture', collector(['a', 'b']), 30, interval=10)
            self.assertEqual(state(root)['members']['b']['status'], 'observed')
            self.assertEqual(state(root)['members']['b']['firstSeen'], 0)

    def test_partial_and_http_errors_never_mark_missing(self):
        with tempfile.TemporaryDirectory() as root:
            sync.tick(root, 'fixture', collector(['a', 'b']), 0, interval=10)
            sync.tick(root, 'fixture', collector(['c'], False), 10)
            sync.tick(root, 'fixture', collector(['c'], False, stopReason='http_error', httpStatus=503), 70)
            saved = state(root)
            self.assertEqual(saved['members']['b']['misses'], 0)
            self.assertEqual(saved['members']['c']['status'], 'observed')
            self.assertEqual(saved['completedSnapshots'], 1)

    def test_rate_limit_cooldown_and_same_snapshot_resume(self):
        with tempfile.TemporaryDirectory() as root:
            paths = []
            def limited(directory):
                paths.append(directory)
                return collector(['a'], False, stopReason='rate_limited', httpStatus=429,
                                 retryAfterSeconds=3600)(directory)
            sync.tick(root, 'fixture', limited, 100)
            for now in [101, 200, 1000, 3699]:
                self.assertEqual(sync.tick(root, 'fixture', limited, now)['status'], 'not_due')
            self.assertEqual(len(paths), 1)
            def recovered(directory):
                self.assertEqual(directory, paths[0])
                return collector(['a', 'b'])(directory)
            self.assertEqual(sync.tick(root, 'fixture', recovered, 3700)['status'], 'snapshot_complete')
            self.assertEqual(len(state(root)['members']), 2)

    def test_crash_after_archive_before_ledger_commit_is_replayed_once(self):
        with tempfile.TemporaryDirectory() as root:
            paths = []
            def crash(directory):
                paths.append(directory)
                collector(['a'])(directory)
                raise SystemExit('simulated process termination')
            with self.assertRaises(SystemExit):
                sync.tick(root, 'fixture', crash, 100)
            self.assertEqual(state(root)['completedSnapshots'], 0)
            def replay(directory):
                self.assertEqual(paths[0], directory)
                return collector(['a'])(directory)
            sync.tick(root, 'fixture', replay, 101)
            self.assertEqual(state(root)['completedSnapshots'], 1)
            self.assertEqual(sync.tick(root, 'fixture', replay, 102)['status'], 'not_due')

    def test_real_sigkill_between_archive_and_ledger_commit(self):
        with tempfile.TemporaryDirectory() as root:
            module = str(Path(__file__).with_name('twitter-likes-sync.py').resolve())
            script = f"""
import importlib.util, os, signal, json
from pathlib import Path
spec = importlib.util.spec_from_file_location('sync', {module!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
def collect(directory):
    (directory / 'archive.jsonl').write_text(json.dumps({{'id': 'a', 'text': 'fixture a'}}) + '\\n')
    os.kill(os.getpid(), signal.SIGKILL)
m.tick({root!r}, 'fixture', collect, 100)
"""
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, -9, result.stderr)
            pending = state(root)['pending']
            self.assertTrue(pending)
            def replay(directory):
                self.assertEqual(directory.name, pending)
                return collector(['a'])(directory)
            self.assertEqual(sync.tick(root, 'fixture', replay, 101)['status'], 'snapshot_complete')
            self.assertEqual(state(root)['completedSnapshots'], 1)

    def test_disconnected_transport_retries_with_bounded_backoff(self):
        with tempfile.TemporaryDirectory() as root:
            def disconnected(_):
                raise TimeoutError()
            now = 0
            for _ in range(10):
                result = sync.tick(root, 'fixture', disconnected, now)
                self.assertEqual(result['status'], 'error')
                self.assertLessEqual(result['nextDue'] - now, 3600)
                now = result['nextDue']
            self.assertEqual(sync.tick(root, 'fixture', collector(['a']), now)['status'], 'snapshot_complete')
            self.assertEqual(state(root)['failures'], 0)

    def test_corruption_or_bad_receipt_never_reconciles_absence(self):
        with tempfile.TemporaryDirectory() as root:
            sync.tick(root, 'fixture', collector(['a', 'b']), 0, interval=10)
            bad = [collector(['a', 'a']), collector(['a'], username='wrong'),
                   collector(['a'], httpStatus=429), collector(['a'], retryAfterSeconds=float('nan'))]
            now = 10
            for collect in bad:
                result = sync.tick(root, 'fixture', collect, now)
                self.assertEqual(result['status'], 'error')
                now = result['nextDue']
                self.assertEqual(state(root)['members']['b']['status'], 'observed')
                self.assertEqual(state(root)['completedSnapshots'], 1)
            with self.assertRaises(ValueError):
                sync.tick(root, 'another-account', collector([]), now)


if __name__ == '__main__':
    unittest.main()
