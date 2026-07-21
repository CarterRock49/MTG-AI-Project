"""Test-only bug logs must never masquerade as production run failures."""

from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from Playersim import debug as debug_module  # noqa: E402


class DebugLogIsolationTest(unittest.TestCase):
    def test_test_runner_detection_covers_direct_pytest_and_spawn_marker(self):
        self.assertTrue(debug_module._looks_like_test_process(
            argv=["tests/scenario_test.py"], environ={}))
        self.assertTrue(debug_module._looks_like_test_process(
            argv=["pytest"], environ={}))
        self.assertTrue(debug_module._looks_like_test_process(
            argv=["C:/Python/Lib/unittest/__main__.py", "discover"],
            environ={},
        ))
        self.assertTrue(debug_module._looks_like_test_process(
            argv=["python", "-m", "unittest", "discover"], environ={}))
        self.assertTrue(debug_module._looks_like_test_process(
            argv=["main.py", "--multiprocessing-fork"],
            environ={"PLAYERSIM_TEST_MODE": "1"},
        ))
        self.assertFalse(debug_module._looks_like_test_process(
            argv=["main.py", "--run-id", "production"], environ={}))

    def test_ide_test_launchers_are_detected(self):
        # IDE test runners drive tests through a launcher script whose argv is
        # neither ``-m unittest`` nor a ``*_test.py`` path.  Each must still be
        # recognised so its logs land in bugs/tests, not top-level bugs/.
        ide_launchers = [
            # VS Code Python extension, modern and legacy launchers.
            ["C:/x/.vscode/ms-python/python_files/unittestadapter/"
             "execution.py", "--udiscovery", "-s", "tests"],
            ["python", "-m", "unittestadapter.execution", "--udiscovery"],
            ["C:/x/.vscode/pythonFiles/visualstudio_py_testlauncher.py",
             "--us=tests", "--ut=test"],
            # PyCharm test runners.
            ["C:/x/.pycharm_helpers/_jb_unittest_runner.py", "true", "tests"],
            ["C:/x/.pycharm_helpers/_jb_pytest_runner.py", "--target",
             "tests/foo.py"],
        ]
        for argv in ide_launchers:
            with self.subTest(argv=argv):
                self.assertTrue(
                    debug_module._looks_like_test_process(argv=argv, environ={}),
                    argv,
                )
                self.assertEqual(
                    debug_module._resolve_bug_log_directory(
                        argv=argv, environ={}),
                    str(Path("bugs") / "tests"),
                )

    def test_import_stack_fallback_catches_unknown_launchers(self):
        # A launcher we do not enumerate is still caught when a test module is
        # live on the import stack as debug.py first loads.
        self.assertTrue(debug_module._looks_like_test_frame(
            "C:/proj/tests/producible_mana_observation_test.py"))
        self.assertTrue(debug_module._looks_like_test_frame(
            "C:/proj/pkg/test_helpers.py"))
        self.assertFalse(debug_module._looks_like_test_frame(
            "C:/proj/main.py"))
        self.assertFalse(debug_module._looks_like_test_frame(
            "C:/proj/Playersim/environment.py"))
        self.assertTrue(debug_module._test_context_in_stack(
            frames=["C:/proj/main.py", "C:/proj/tests/foo_test.py"]))
        self.assertFalse(debug_module._test_context_in_stack(
            frames=["C:/proj/main.py", "C:/proj/Playersim/environment.py"]))

    def test_explicit_test_mode_off_overrides_every_signal(self):
        # An operator can force production routing even from a test-shaped argv.
        self.assertFalse(debug_module._current_process_is_test(
            argv=["tests/smoke_test.py"],
            environ={"PLAYERSIM_TEST_MODE": "0"}))
        self.assertFalse(debug_module._current_process_is_test(
            argv=["C:/x/_jb_pytest_runner.py"],
            environ={"PLAYERSIM_TEST_MODE": "false"}))

    def test_test_logs_use_isolated_directory_and_override_wins(self):
        self.assertEqual(
            debug_module._resolve_bug_log_directory(
                argv=["tests/smoke_test.py"], environ={}),
            str(Path("bugs") / "tests"),
        )
        self.assertEqual(
            debug_module._resolve_bug_log_directory(
                argv=["tests/smoke_test.py"],
                environ={"PLAYERSIM_BUG_LOG_DIR": "custom/logs"},
            ),
            str(Path("custom") / "logs"),
        )
        handler_parent = Path(
            debug_module.error_handler.baseFilename).parent
        self.assertEqual(handler_parent.name, "tests")
        self.assertEqual(handler_parent.parent.name, "bugs")


if __name__ == "__main__":
    unittest.main()
