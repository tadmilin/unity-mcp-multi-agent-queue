"""Installer boundaries and file integration; no Editor or service is started."""
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import install


@unittest.skipUnless(sys.platform == 'win32', 'Windows installer')
class InstallTests(unittest.TestCase):
    def test_workflow_script_metadata_has_valid_unique_guids(self):
        # Unity may exit successfully while silently skipping a script whose
        # .meta GUID is malformed. Compiler-only checks do not cover this.
        scripts = install.REPO / 'queue/UnityProject/Assets/Editor'
        seen = set()
        for source in scripts.rglob('*.cs'):
            with self.subTest(script=str(source.relative_to(scripts))):
                text = source.with_suffix('.cs.meta').read_text(encoding='utf-8-sig')
                match = re.search(r'^guid: ([0-9a-f]{32})\s*$', text, re.MULTILINE)
                self.assertIsNotNone(match, 'Unity requires exactly 32 hexadecimal GUID characters')
                self.assertNotIn(match.group(1), seen)
                seen.add(match.group(1))

    def test_install_lock_covers_native_server_metadata(self):
        def pins(path):
            result = set()
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.startswith('-r '):
                    result.update(pins(path.parent / line[3:].strip()))
                elif line and not line.startswith('#'):
                    result.add(line.split()[0].lower().replace('_', '-'))
            return result
        metadata = tomllib.loads((install.REPO / 'queue/UnityProject/Tools/UnityNativeObserver/pyproject.toml').read_text())
        selected = pins(install.REPO / install.INSTALL_LOCK)
        missing = set(metadata['project']['dependencies']) - selected
        self.assertFalse(missing, f'Native dependencies absent from installer lock: {sorted(missing)}')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='queue install test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ('Assets', 'Packages', 'ProjectSettings'):
            (self.root / directory).mkdir()
        (self.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.0.0f1\n')
        self.manifest = self.root / 'Packages/manifest.json'
        self.original = b'{"dependencies":{"example.existing":"1.0.0"},"testables":[]}\n'
        self.manifest.write_bytes(self.original)

    def test_check_is_read_only(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        install.preflight(self.root)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_copy_and_real_coordinator_setup_preserve_manifest(self):
        # Only environment provisioning is substituted. The coordinator subprocess is real.
        with patch.object(install, 'prepare_environment', return_value=Path(sys.executable)):
            result = install.install(self.root)
        self.assertEqual(result['status'], 'installed_pending_unity_validation')
        self.assertFalse(result['services_started'])
        self.assertFalse(result['unity_validated'])
        actual = json.loads(self.manifest.read_text())
        self.assertEqual(actual['dependencies']['example.existing'], '1.0.0')
        self.assertEqual(actual['testables'], [])
        self.assertEqual(actual['dependencies'][install.PACKAGE], 'file:' + install.PACKAGE)
        self.assertTrue((self.root / 'JobQueue/queue.sqlite3').is_file())
        self.assertTrue((self.root / 'Tools/UnityReloadDiagnostics/observe_reload.py').is_file())
        self.assertTrue((self.root / 'Assets/Editor/UnityWorkflow/EditorQueueWorkflow.cs').is_file())
        self.assertTrue((self.root / 'Packages' / install.PACKAGE / 'Editor/Tools/EditorQueueGateway.cs').is_file())
        self.assertEqual((self.root / install.STATE / 'manifest.before.json').read_bytes(), self.original)
        with self.assertRaisesRegex(ValueError, 'already installed'):
            install.install(self.root)

    def test_live_lock_refuses_without_writes(self):
        (self.root / 'Temp').mkdir()
        (self.root / 'Temp/UnityLockfile').touch()
        with self.assertRaisesRegex(ValueError, 'lock exists'):
            install.install(self.root)
        self.assertFalse((self.root / install.STATE).exists())

    def test_existing_tooling_refused(self):
        (self.root / 'Tools/UnityWorkflowMcp').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            install.install(self.root)
        self.assertEqual(self.manifest.read_bytes(), self.original)

    def test_dependency_failure_leaves_import_roots_untouched(self):
        failure = subprocess.CalledProcessError(1, ['pip'])
        with patch.object(install, 'prepare_environment', side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                install.install(self.root)
        self.assertEqual(self.manifest.read_bytes(), self.original)
        self.assertEqual(list((self.root / 'Assets').iterdir()), [])
        self.assertFalse((self.root / 'Tools').exists())
        receipt = json.loads((self.root / install.STATE / 'install-result.json').read_text())
        self.assertEqual(receipt['status'], 'failed')
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            install.install(self.root)

    def test_manifest_change_during_download_refuses(self):
        changed = b'{"dependencies":{"another":"2.0.0"}}'
        def provision(*_):
            self.manifest.write_bytes(changed)
            return Path(sys.executable)
        with patch.object(install, 'prepare_environment', side_effect=provision):
            with self.assertRaisesRegex(ValueError, 'changed'):
                install.install(self.root)
        self.assertEqual(self.manifest.read_bytes(), changed)
        self.assertFalse((self.root / 'Tools').exists())


if __name__ == '__main__':
    unittest.main()
