# debug.py

import logging
import traceback
import os
import atexit
import numpy as np
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Create bugs directory if it doesn't exist
os.makedirs("bugs", exist_ok=True)

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

_prune_old_logs()

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
    os.path.join("bugs", f"mtg_errors_{timestamp}.log"),
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
    os.path.join("bugs", f"mtg_warnings_{timestamp}.log"),
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
    os.path.join("bugs", f"mtg_debug_{timestamp}.log"),
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
    _prune_old_logs()


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
