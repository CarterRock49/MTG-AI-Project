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
