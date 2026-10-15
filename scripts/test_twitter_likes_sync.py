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


class NewLikesTest(unittest.TestCase):
    baseline = [{'id': f'old-{n}', 'text': 'old fixture'} for n in range(300)]

    def tick(self, root, collect, now, **kwargs):
        return sync.tick(root, 'fixture', collect, now, interval=10,
                         mode='new-likes', baseline_rows=self.baseline, **kwargs)

    def test_new_posts_stop_on_known_overlap_without_reaching_history_end(self):
        with tempfile.TemporaryDirectory() as root:
            ids = ['new-a', 'new-b'] + [f'old-{n}' for n in range(200)]
            result = self.tick(root, collector(ids, False, stopReason='page_cap'), 0)
            self.assertEqual(result['status'], 'incremental_complete')
            self.assertEqual(result['boundary'], 'known_overlap')
            self.assertEqual(result['added'], 2)
            self.assertEqual(state(root)['members']['old-299']['status'], 'baseline')
            self.assertEqual(state(root)['completedSnapshots'], 0)

    def test_does_not_stop_at_first_known_id_or_inadequate_overlap(self):
        with tempfile.TemporaryDirectory() as root:
            ids = ['old-0', 'new-a'] + [f'old-{n}' for n in range(1, 100)]
            result = self.tick(root, collector(ids, False), 0)
            self.assertEqual(result['status'], 'partial')
            self.assertIn('new-a', state(root)['members'])
            self.assertIsNotNone(state(root)['pending'])

    def test_burst_larger_than_page_budget_and_arrivals_during_pagination(self):
        with tempfile.TemporaryDirectory() as root:
            first = [f'new-{n}' for n in range(500)]
            paths = []
            def start(directory):
                paths.append(directory)
                return collector(first, False)(directory)
            self.assertEqual(self.tick(root, start, 0)['status'], 'partial')
            self.assertEqual(len(state(root)['cycleKnownIds']), 300)
            # Late arrival is inserted at the head after the first page; the
            # continuation cursor does not show it, so the next cycle must.
            burst = [f'new-{n}' for n in range(600)]
            continued = burst + [f'old-{n}' for n in range(200)]
            def resume(directory):
                self.assertEqual(directory, paths[0])
                return collector(continued, False)(directory)
            result = self.tick(root, resume, 60)
            self.assertEqual(result['status'], 'incremental_complete')
            def next_cycle(directory):
                self.assertNotEqual(directory, paths[0])
                return collector(['late-arrival'] + burst[:200], False)(directory)
            self.assertEqual(self.tick(root, next_cycle, 70)['added'], 1)
            self.assertEqual(len(state(root)['members']), 901)
            self.assertIsNone(state(root)['pending'])

    def test_rate_limit_preserves_cycle_boundary_and_cooldown(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.tick(root, collector(['new-a'], False, stopReason='rate_limited',
                                              httpStatus=429, retryAfterSeconds=300), 0)
            self.assertEqual(result['status'], 'rate_limited')
            boundary = state(root)['cycleKnownIds']
            self.assertNotIn('new-a', boundary)
            def unexpected(_):
                self.fail('Must not call during cooldown')
            self.assertEqual(self.tick(root, unexpected, 299)['status'], 'not_due')
            result = self.tick(root, collector(['new-a'] + boundary[:200], False), 300)
            self.assertEqual(result['status'], 'incremental_complete')
            self.assertEqual(result['added'], 0)  # Already stored before cooldown.

    def test_new_like_on_old_post_is_not_filtered_by_post_date(self):
        with tempfile.TemporaryDirectory() as root:
            def collect(directory):
                receipt = collector(['old-post-new-like'] + [f'old-{n}' for n in range(200)], False)(directory)
                file = directory / 'archive.jsonl'
                rows = [json.loads(line) for line in file.read_text().splitlines()]
                rows[0]['created_at'] = 'Mon Jan 01 00:00:00 +0000 2001'
                file.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                return receipt
            self.assertEqual(self.tick(root, collect, 0)['added'], 1)
            self.assertIn('old-post-new-like', state(root)['members'])

    def test_seven_days_of_new_likes_without_full_history_completion(self):
        with tempfile.TemporaryDirectory() as root:
            for step in range(29):
                ids = [f'new-{n}' for n in reversed(range(step + 1))] + [f'old-{n}' for n in range(200)]
                result = self.tick(root, collector(ids, False, stopReason='page_cap'), step * 21600)
                self.assertEqual(result['status'], 'incremental_complete')
                self.assertEqual(result['added'], 1)
                self.assertEqual(self.tick(root, collector(ids, False), step * 21600)['status'], 'not_due')
            self.assertEqual(state(root)['completedCycles'], 29)
            self.assertEqual(state(root)['completedSnapshots'], 0)
            self.assertEqual(len(state(root)['members']), 329)

    def test_repeated_polls_and_mode_switch_are_safe(self):
        with tempfile.TemporaryDirectory() as root:
            ids = ['new-a'] + [f'old-{n}' for n in range(200)]
            self.tick(root, collector(ids, False), 0)
            self.assertEqual(self.tick(root, collector(ids, False), 10)['added'], 0)
            self.assertEqual(len(state(root)['members']), 301)
            with self.assertRaises(ValueError):
                sync.tick(root, 'fixture', collector([]), 20, mode='snapshot')


if __name__ == '__main__':
    unittest.main()
