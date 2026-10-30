"""Windows process-role regressions. Live Editor acceptance is recorded separately."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from initial_wait import command_line_arguments, editor_command, live_unity_process


class EditorIdentityTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='Editor identity ')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.image = self.root / 'Editor/Unity.exe'
        assembly = self.image.parent / 'Data/Managed/UnityEditor.dll'
        assembly.parent.mkdir(parents=True)
        assembly.touch()
        self.project = self.root / 'Project with spaces'
        self.command = [str(self.image), '-projectPath', str(self.project)]

    def accepts(self, args=None, image=None, workspace=None):
        return editor_command(str(image or self.image), subprocess.list2cmdline(args or self.command),
                              workspace or self.project)

    def test_editor_with_quoted_project_and_unrelated_flags(self):
        self.assertTrue(self.accepts(self.command + ['-useHub', '-accessToken', 'secret-do-not-log']))

    def test_hub_unity_executable_rejected(self):
        hub = self.root / 'Unity Hub/resources/unity.exe'
        self.assertFalse(self.accepts([str(hub), 'serve'], image=hub))
        self.assertFalse(self.accepts([str(hub), '-projectPath', str(self.project)], image=hub))

    def test_worker_role_flags_independently_rejected(self):
        for args in (['-adb2'], ['-batchMode'], ['-name', 'AssetImportWorkerHW2'],
                     ['-NAME=assetimportworker0'], ['-ADB2=true'], ['-batchmode=true']):
            with self.subTest(args=args):
                self.assertFalse(self.accepts(self.command + args))

    def test_foreign_or_missing_project_rejected(self):
        self.assertFalse(self.accepts(workspace=self.root / 'Other'))
        self.assertFalse(self.accepts([str(self.image), '-useHub']))
        self.assertFalse(self.accepts([str(self.image), '-projectPath']))
        self.assertFalse(self.accepts([str(self.image), '-projectPath', 'relative']))
        self.assertFalse(self.accepts(self.command + ['-projectPath', str(self.project)]))

    def test_equal_and_case_variant_project_flag(self):
        self.assertTrue(self.accepts([str(self.image), '-PROJECTPATH=' + str(self.project)]))

    def test_command_image_must_match_open_process_image(self):
        self.assertFalse(self.accepts([str(self.root / 'other.exe'), *self.command[1:]]))

    def test_absent_or_unreadable_command_fails_closed(self):
        for value in (None, '', ' ', '\x00'):
            self.assertFalse(editor_command(str(self.image), value, self.project))

    def test_windows_quoting_preserves_path_with_spaces(self):
        self.assertEqual(self.command, command_line_arguments(subprocess.list2cmdline(self.command)))

    def test_invalid_pid_types_fail_closed(self):
        for value in (True, 0, -1, 2**32, '24960'):
            self.assertFalse(live_unity_process(value, 1))


if __name__ == '__main__':
    unittest.main()
