# Create a modified debug.py file with comprehensive logging and file rotation
import logging
import traceback
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Create bugs directory if it doesn't exist
os.makedirs("bugs", exist_ok=True)

# Configure logging based on debug mode
DEBUG_MODE = True
DEBUG_ENV_RESETS = True  # Track environment resets
DEBUG_ACTION_STEPS = True  # Track action steps

# Generate timestamp for the log files
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

# Set up rotating error file handler with 2GB size limit
MAX_LOG_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB in bytes
BACKUP_COUNT = 8  # Number of backup files to keep

# Set up error file handler with rotation
error_handler = RotatingFileHandler(
    os.path.join("bugs", f"mtg_errors_{timestamp}.log"),
    maxBytes=MAX_LOG_SIZE,
    backupCount=BACKUP_COUNT
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'))

# Set up warning file handler with rotation
warning_handler = RotatingFileHandler(
    os.path.join("bugs", f"mtg_warnings_{timestamp}.log"),
    maxBytes=MAX_LOG_SIZE,
    backupCount=BACKUP_COUNT
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
    backupCount=BACKUP_COUNT
)
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'))

# Console handler for regular logging (no rotation needed)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'))

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
root_logger.addHandler(error_handler)
root_logger.addHandler(warning_handler)
root_logger.addHandler(debug_handler)
root_logger.addHandler(console_handler)

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