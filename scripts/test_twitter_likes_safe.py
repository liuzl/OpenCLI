import importlib.util
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

MODULE = Path(__file__).with_name('twitter-likes-safe.py').resolve()
spec = importlib.util.spec_from_file_location('safe_likes', MODULE)
safe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safe)


class SafeLikesTest(unittest.TestCase):
    def test_aliases_and_non_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = base / 'out'
            out.write_text('data')
            alias = base / 'alias'
            alias.symlink_to(out)
            with self.assertRaises(ValueError):
                safe.archive_path(alias)
            alias.unlink()
            os.link(out, alias)
            with self.assertRaises(ValueError):
                safe.archive_path(out)
            with self.assertRaises(ValueError):
                safe.archive_path(base)

    def test_lock_namespace_is_reserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'reserved'):
                safe.archive_path(Path(tmp) / 'out.opencli-lock')

    def test_symlink_parent_has_same_lock_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / 'real'
            real.mkdir()
            alias = base / 'alias'
            alias.symlink_to(real, target_is_directory=True)
            self.assertEqual(safe.archive_path(real / 'new'), safe.archive_path(alias / 'new'))

    def test_locks_survive_exec_to_node_and_release_after_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, resume = Path(tmp) / 'out', Path(tmp) / 'resume'
            code = f'''
import importlib.util, os
from pathlib import Path
spec = importlib.util.spec_from_file_location('safe_likes', {str(MODULE)!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fds = m.acquire_locks([Path({str(output)!r}), Path({str(resume)!r})])
os.execvp('node', ['node', '-e', "console.log('READY'); setInterval(() => {{}}, 1000)"])
'''
            process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 10)[0])
                self.assertEqual(process.stdout.readline().strip(), 'READY')
                for target in [output, resume]:
                    with self.assertRaisesRegex(ValueError, 'already in use'):
                        safe.acquire_locks([target])
                process.kill()
                process.wait(timeout=10)
                fds = safe.acquire_locks([output, resume])
                for fd in fds:
                    os.close(fd)
                # Empty lock files intentionally remain: unlinking creates races.
                self.assertTrue(Path(str(output) + '.opencli-lock').exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)

    def test_lock_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            victim = Path(tmp) / 'victim'
            victim.write_text('unchanged')
            Path(str(out) + '.opencli-lock').symlink_to(victim)
            with self.assertRaises(OSError):
                safe.acquire_locks([out])
            self.assertEqual(victim.read_text(), 'unchanged')

    def test_launch_rejects_same_path_without_starting_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = str(Path(tmp) / 'same')
            result = subprocess.run([sys.executable, str(MODULE), '--output-file', file,
                                     '--resume-file', file], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('different files', result.stderr)
            self.assertFalse(Path(file).exists())


if __name__ == '__main__':
    unittest.main()
