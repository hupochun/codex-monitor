"""Artifact behavior, not just archive membership: run without the checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='monitor distribution ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def run_cli(self, entry, *args):
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        return subprocess.run([sys.executable, '-I', str(entry), *map(str, args)],
                              cwd=self.root, env=env, capture_output=True, text=True,
                              encoding='utf-8', timeout=20)

    def test_archive_relocated_skill_runs_job_and_installs_again(self):
        from codex_monitor.distribution import build_skill_archive
        archive = build_skill_archive(self.root / 'release.zip')
        extracted = self.root / 'another computer with spaces'
        with zipfile.ZipFile(archive) as z:
            self.assertFalse(any('monitor-location' in name or '__pycache__' in name for name in z.namelist()))
            for name in z.namelist():
                self.assertNotIn(str(ROOT).encode(), z.read(name))
            z.extractall(extracted)
        skill = extracted / 'codex-training-monitor'
        entry = skill / 'scripts' / 'monitor.py'
        result = self.run_cli(entry, '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / 'second installation'
        result = self.run_cli(entry, 'install-skill', '--destination', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / 'scripts' / 'codex_monitor' / 'cli.py').is_file())
        state = self.root / 'state'
        spec = self.root / 'job.json'
        spec.write_text(json.dumps({'command': [sys.executable, '-u', '-c',
                         'print("step=0\\nstep=1\\nstep=2")'], 'cwd': str(self.root),
                         'bridge': {'enabled': False},
                         'health': {'pattern': r'step=(?P<step>\d+)'}}), encoding='utf-8')
        result = self.run_cli(entry, '--state-dir', state, 'submit', '--spec', spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        job_id = json.loads(result.stdout)['id']
        result = self.run_cli(entry, '--state-dir', state, 'wait-ready', job_id, '--timeout', 10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.run_cli(entry, '--state-dir', state, 'status', job_id)
            job = json.loads(result.stdout)
            if job['delivery'] == 'disabled':
                break
            time.sleep(.1)
        self.assertEqual((job['status'], job['health'], job['delivery']),
                         ('succeeded', 'healthy', 'disabled'))

    def test_install_is_self_contained_idempotent_and_upgrade_preserves_backup(self):
        from codex_monitor.distribution import install_skill
        target = self.root / 'skill'
        first = install_skill(target)
        self.assertEqual(first['status'], 'installed')
        self.assertFalse((target / 'monitor-location.json').exists())
        self.assertEqual(install_skill(target)['status'], 'unchanged')
        # A release payload change requires explicit upgrade, preserving originals.
        from unittest.mock import patch
        with patch('codex_monitor.distribution.__version__', '9.0.0'):
            with self.assertRaises(ValueError):
                install_skill(target)
            result = install_skill(target, upgrade=True)
        self.assertTrue(Path(result['backup']).is_dir())
        self.assertEqual(json.loads((target / 'installation.json').read_text())['version'], '9.0.0')

    def test_local_changes_and_unknown_directory_are_not_overwritten(self):
        from codex_monitor.distribution import install_skill
        target = self.root / 'skill'
        target.mkdir()
        (target / 'private.txt').write_text('keep me')
        with self.assertRaises(ValueError):
            install_skill(target, upgrade=True)
        self.assertEqual((target / 'private.txt').read_text(), 'keep me')
        fresh = self.root / 'fresh'
        install_skill(fresh)
        (fresh / 'SKILL.md').write_text('my customized instructions')
        with self.assertRaises(ValueError):
            install_skill(fresh, upgrade=True)
        self.assertEqual((fresh / 'SKILL.md').read_text(), 'my customized instructions')

    def test_migrate_legacy_without_following_old_source_pointer(self):
        from codex_monitor.distribution import install_skill
        target = self.root / 'legacy skill'
        (target / 'scripts').mkdir(parents=True)
        (target / 'scripts' / 'monitor.py').write_bytes((ROOT / 'tests/fixtures/legacy_launcher.py.txt').read_bytes())
        (target / 'SKILL.md').write_text('---\nname: codex-training-monitor\n---\n')
        (target / 'monitor-location.json').write_text('{"source": "nonexistent-original-checkout"}')
        result = install_skill(target, upgrade=True)
        self.assertFalse((target / 'monitor-location.json').exists())
        self.assertTrue((Path(result['backup']) / 'monitor-location.json').exists())
        self.assertTrue((target / 'scripts/codex_monitor/cli.py').is_file())

    def test_concurrent_installer_is_rejected_before_replacement(self):
        from codex_monitor.distribution import install_skill
        from unittest.mock import patch
        target = self.root / 'skill'
        import codex_monitor.distribution as distribution
        original = distribution._payload
        nested = []
        entered = False
        def payload():
            nonlocal entered
            if entered:
                return original()
            entered = True
            try:
                install_skill(target, upgrade=True)
            except ValueError as exc:
                nested.append(str(exc))
            return original()
        with patch('codex_monitor.distribution._payload', side_effect=payload):
            result = install_skill(target)
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(len(nested), 1)
        self.assertIn('installation is already in progress', nested[0])

    def test_legacy_internal_symlink_and_manifest_symlink_are_rejected(self):
        from codex_monitor.distribution import install_skill
        outside = self.root / 'external launcher'
        outside.write_bytes((ROOT / 'tests/fixtures/legacy_launcher.py.txt').read_bytes())
        target = self.root / 'legacy linked skill'
        (target / 'scripts').mkdir(parents=True)
        try:
            (target / 'scripts/monitor.py').symlink_to(outside)
        except OSError as exc:
            self.skipTest(f'Symlinks unavailable: {exc}')
        (target / 'SKILL.md').write_text('---\nname: codex-training-monitor\n---\n')
        (target / 'monitor-location.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            install_skill(target, upgrade=True)
        self.assertTrue(outside.exists())
        modern = self.root / 'modern linked skill'
        install_skill(modern)
        manifest = modern / 'installation.json'
        external_manifest = self.root / 'external manifest'
        manifest.rename(external_manifest)
        manifest.symlink_to(external_manifest)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            install_skill(modern, upgrade=True)

    def test_windows_reparse_flag_detected_without_new_pathlib_api(self):
        from codex_monitor.distribution import _is_link
        from unittest.mock import Mock
        path = Mock()
        path.lstat.return_value = type('Metadata', (), {'st_mode': 0o40755, 'st_file_attributes': 0x400})()
        self.assertTrue(_is_link(path))


if __name__ == '__main__':
    unittest.main()
