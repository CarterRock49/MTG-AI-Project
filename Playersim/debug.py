# debug.py

import logging
import traceback
import os
import sys
import atexit
import numpy as np
from datetime import datetime
from logging.handlers import RotatingFileHandler

def _looks_like_test_process(argv=None, environ=None):
    """Identify repository test runners without relying on one framework."""
    if argv is None:
        argv = list(sys.argv)
        invocation = list(getattr(sys, "orig_argv", ()) or ())
    else:
        argv = list(argv)
        invocation = list(argv)
    environ = os.environ if environ is None else environ
    explicit = str(environ.get("PLAYERSIM_TEST_MODE", "")).strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    if environ.get("PYTEST_CURRENT_TEST"):
        return True
    if not argv:
        return False
    lowered_invocation = [str(token).strip().lower()
                          for token in invocation]
    if any(token in {"pytest", "py.test", "unittest"}
           for token in lowered_invocation):
        return True
    # IDE test runners (VS Code, PyCharm) drive tests through a launcher whose
    # argv is neither ``-m unittest`` nor a ``*_test.py`` path, so match the
    # launcher scripts themselves.  The import-stack fallback below is the real
    # catch-all; these markers keep detection working before any test frame is
    # on the stack (e.g. a launcher that pre-imports the package).
    joined_invocation = " ".join(lowered_invocation)
    if any(marker in joined_invocation for marker in _IDE_TEST_LAUNCHER_MARKERS):
        return True
    executable = os.path.basename(str(argv[0])).lower()
    if executable.startswith("pytest") or executable in {
            "py.test", "unittest", "unittest.exe"}:
        return True
    normalized = str(argv[0]).replace("\\", "/").lower()
    if any(marker in normalized for marker in _IDE_TEST_LAUNCHER_MARKERS):
        return True
    filename = normalized.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{normalized.lstrip('/')}"
        or "/unittest/" in f"/{normalized.lstrip('/')}"
        or filename.endswith(("_test.py", "test.py"))
    )


# Launcher-script name fragments used by common IDE test runners.  ``unittest``
# alone is deliberately absent — it is handled above as an exact token so it
# does not match unrelated paths.
_IDE_TEST_LAUNCHER_MARKERS = (
    "unittestadapter",           # VS Code Python extension (modern)
    "visualstudio_py_test",      # VS Code Python extension (legacy)
    "testlauncher",              # generic VS Code / ptvsd launcher
    "_jb_unittest_runner",       # PyCharm unittest
    "_jb_pytest_runner",         # PyCharm pytest
    "_jb_nosetest_runner",       # PyCharm nose
    "pydev_runfiles",            # PyDev / Eclipse
)


def _looks_like_test_frame(filename):
    """True when a stack frame belongs to a test module or the tests package."""
    normalized = str(filename).replace("\\", "/").lower()
    base = normalized.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{normalized.lstrip('/')}"
        or base.endswith(("_test.py", "test.py"))
        or base.startswith("test_")
    )


def _test_context_in_stack(frames=None):
    """Detect a test run from the import stack, independent of the launcher.

    ``debug.py`` is only pulled in transitively (via ``environment`` etc.), so
    during a test run the frame that first imports it always traces back
    through a test module or the ``tests`` package.  A production ``main.py``
    run never has such a frame, so this stays false there.
    """
    if frames is None:
        frames = [frame.filename for frame in traceback.extract_stack()]
    return any(_looks_like_test_frame(name) for name in frames)


def _current_process_is_test(argv=None, environ=None):
    """Full test-context check: explicit env, launcher argv, then stack."""
    environ = os.environ if environ is None else environ
    explicit = str(environ.get("PLAYERSIM_TEST_MODE", "")).strip().lower()
    if explicit in {"0", "false", "no", "off"}:
        return False
    if _looks_like_test_process(argv=argv, environ=environ):
        return True
    return _test_context_in_stack()


def _resolve_bug_log_directory(argv=None, environ=None):
    """Route synthetic test logs away from operator-facing run logs."""
    environ = os.environ if environ is None else environ
    override = str(environ.get("PLAYERSIM_BUG_LOG_DIR", "")).strip()
    if override:
        return os.path.normpath(override)
    return os.path.join(
        "bugs", "tests") if _looks_like_test_process(
            argv=argv, environ=environ) else "bugs"


TEST_LOG_CONTEXT = _current_process_is_test()
if TEST_LOG_CONTEXT:
    # Windows spawned workers import this module afresh, and an IDE launcher's
    # argv is invisible to them.  Mark the parent environment so every child --
    # and the directory resolution just below -- keeps the isolated destination.
    os.environ.setdefault("PLAYERSIM_TEST_MODE", "1")
BUG_LOG_DIRECTORY = _resolve_bug_log_directory()
os.makedirs(BUG_LOG_DIRECTORY, exist_ok=True)

# Keep only the most recent logs of each family so bugs/ stops growing
# without bound across runs.
KEEP_RECENT_LOGS = 5

def _prune_old_logs(directory="bugs", keep=KEEP_RECENT_LOGS):
    """Keep at most ``keep`` files for each bug-log family.

    Windows spawn workers can all pass an import-time prune before any of them
    creates its delayed log files. Shutdown pruning is therefore also required.
    """
    import glob

    def mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    for family in ("mtg_errors_", "mtg_warnings_", "mtg_debug_"):
        files = sorted((path for path in glob.glob(
                            os.path.join(directory, family + "*"))
                        if os.path.isfile(path)),
                       key=mtime, reverse=True)
        for stale in files[keep:]:
            try:
                os.remove(stale)
            except OSError:
                pass  # still open in another process; the next run gets it

_prune_old_logs(BUG_LOG_DIRECTORY)

# Configure logging based on debug mode
DEBUG_MODE = False
DEBUG_ENV_RESETS = False  # Track environment resets
DEBUG_ACTION_STEPS = False  # Track action steps

# Generate timestamp for the log files
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

# Set up rotating error file handler with 2GB size limit
MAX_LOG_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB in bytes
BACKUP_COUNT = KEEP_RECENT_LOGS - 1

# Set up error file handler with rotation. delay=True defers creating the
# file until a record is actually written: importing this module used to
# leave three empty timestamped files behind on every run.
error_handler = RotatingFileHandler(
    os.path.join(BUG_LOG_DIRECTORY, f"mtg_errors_{timestamp}.log"),
    maxBytes=MAX_LOG_SIZE,
    backupCount=BACKUP_COUNT,
    encoding="utf-8",
    errors="replace",
    delay=True,
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'))

# Set up warning file handler with rotation
warning_handler = RotatingFileHandler(
    os.path.join(BUG_LOG_DIRECTORY, f"mtg_warnings_{timestamp}.log"),
    maxBytes=MAX_LOG_SIZE,
    backupCount=BACKUP_COUNT,
    encoding="utf-8",
    errors="replace",
    delay=True,
)
warning_handler.setLevel(logging.WARNING)
warning_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'))

# Create filter to only capture warnings (not errors)
class WarningFilter(logging.Filter):
    def filter(self, record):
        return record.levelno == logging.WARNING

warning_handler.addFilter(WarningFilter())

# Set up debug file handler with rotation
debug_handler = RotatingFileHandler(
    os.path.join(BUG_LOG_DIRECTORY, f"mtg_debug_{timestamp}.log"),
    maxBytes=MAX_LOG_SIZE,
    backupCount=BACKUP_COUNT,
    encoding="utf-8",
    errors="replace",
    delay=True,
)
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'))

# Console handler for regular logging (no rotation needed)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'))

# Configure root logger
root_logger = logging.getLogger()
# Force level to DEBUG if mode is on, regardless of other libraries
root_logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
root_logger.addHandler(error_handler)
root_logger.addHandler(warning_handler)
root_logger.addHandler(debug_handler)
root_logger.addHandler(console_handler)


def _shutdown_bug_logging():
    """Close this process's bug handlers, then enforce family retention."""
    for handler in (error_handler, warning_handler, debug_handler):
        try:
            root_logger.removeHandler(handler)
            handler.flush()
            handler.close()
        except (OSError, ValueError):
            pass
    _prune_old_logs(BUG_LOG_DIRECTORY)


atexit.register(_shutdown_bug_logging)

# Helper function to track environment resets
def log_reset(env_id):
    try:
        # Log basic reset information
        logging.debug(f"Environment {env_id} reset called from: {__name__}")
        
        # Optional: Capture minimal stack trace context
        stack = traceback.extract_stack()
        if len(stack) > 2:
            caller = stack[-2]
            logging.debug(f"Reset called from {caller.filename}:{caller.lineno}")
    except Exception as e:
        print(f"Error in log_reset: {e}")

# Helper function to log exceptions to file
def log_exception(exception, additional_info=""):
    """Log an exception with stack trace to file"""
    error_msg = f"{additional_info}\nException: {str(exception)}\n"
    error_msg += traceback.format_exc()
    logging.error(error_msg)

def debug_log_valid_actions(game_state, valid_actions, action_reasons, action_lookup_func):
    """
    Helper to log all available actions in a readable format for debugging.
    Robust version using standard logging module.
    """
    # Only spend compute time if debug logging is enabled
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        try:
            # Ensure valid_actions is a numpy array
            if not isinstance(valid_actions, np.ndarray):
                valid_actions = np.array(valid_actions)
                
            valid_indices = np.where(valid_actions)[0]
            count = len(valid_indices)
            
            # Identify player
            player_name = "Unknown"
            if hasattr(game_state, 'agent_is_p1'):
                p = game_state.p1 if game_state.agent_is_p1 else game_state.p2
                if p: player_name = p.get('name', 'Player')
            
            # Identify phase
            phase_name = "Unknown Phase"
            if hasattr(game_state, 'phase'):
                # Try to lookup phase name, fallback to int
                phase_name = getattr(game_state, '_PHASE_NAMES', {}).get(game_state.phase, f"PHASE_{game_state.phase}")
            
            # Header
            log_lines = [f"\n=== AI ACTIONS ({count}): {player_name} [{phase_name}] ==="]
            
            if count == 0:
                log_lines.append("  (No valid actions found)")
            else:
                for idx in valid_indices:
                    # 1. Get Action Name
                    act_name = "Unknown"
                    if action_lookup_func:
                        try:
                            info = action_lookup_func(idx)
                            if isinstance(info, tuple):
                                act_name = f"{info[0]}({info[1]})"
                            else:
                                act_name = str(info)
                        except Exception:
                            act_name = f"Action_{idx}"
                    
                    # 2. Get Reason/Context
                    details = ""
                    entry = action_reasons.get(idx)
                    if isinstance(entry, dict):
                        reason = entry.get("reason", "")
                        ctx = entry.get("context", {})
                        # Format context concisely
                        ctx_str = str(ctx) if ctx else ""
                        if ctx_str == "{}": ctx_str = ""
                        
                        details = f" | {reason}"
                        if ctx_str: details += f" | {ctx_str}"
                    elif entry:
                        details = f" | {str(entry)}"
                        
                    log_lines.append(f"  [{idx:03d}] {act_name:<30}{details}")

            log_lines.append("==========================================================")
            
            # Log as a single block to prevent interleaving
            logging.debug("\n".join(log_lines))
            
        except Exception as e:
            # Fallback if formatting fails
            logging.error(f"Failed to log valid actions: {e}")
            # print(f"DEBUG FAIL: {e}") # Uncomment for extreme debugging
