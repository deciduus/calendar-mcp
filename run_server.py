import uvicorn
import os
import sys
import logging
import logging.config # Import logging config
import threading
from dotenv import load_dotenv

# --- Centralized Logging Configuration --- 
# Get project directory for absolute log path
project_dir_for_log = os.path.dirname(os.path.abspath(__file__))
log_file_path = os.path.join(project_dir_for_log, 'calendar_mcp.log')

# Define the logging configuration dictionary
LOGGING_CONFIG = { 
    'version': 1,
    'disable_existing_loggers': False, # Let FastAPI/Uvicorn use this config
    'formatters': { 
        'default': { 
            'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        },
    },
    'handlers': { 
        'default': { # Console handler (used when not in MCP mode)
            'formatter': 'default',
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stderr',
        },
        'file': { # File handler (always used)
            'formatter': 'default',
            'class': 'logging.FileHandler',
            'filename': log_file_path, # Use absolute path
            'mode': 'a', # Append mode
        },
    },
    'loggers': { 
        '': { # Root logger
            'handlers': ['default', 'file'], # Start with both
            'level': 'INFO',
        },
        'uvicorn.error': { 
            'level': 'INFO', # Capture uvicorn errors
            'handlers': ['default', 'file'],
            'propagate': False,
        },
        'uvicorn.access': { 
            'level': 'WARNING', # Reduce access log noise if desired
            'handlers': ['default', 'file'],
            'propagate': False,
        },
    }
} 

# --- Force Reset Existing Handlers --- 
# Get the root logger
root_logger_for_reset = logging.getLogger()
# Remove all existing handlers
if root_logger_for_reset.hasHandlers():
    for handler in root_logger_for_reset.handlers[:]: # Iterate over a copy
        root_logger_for_reset.removeHandler(handler)
        handler.close() # Close the handler properly
    # stderr, not stdout: stdout is the MCP protocol channel in stdio mode.
    print("Log Reset: Removed existing handlers.", file=sys.stderr) # Use print as logger not configured yet
# --- End Force Reset ---

# Apply the configuration
logging.config.dictConfig(LOGGING_CONFIG)

# Get the root logger AFTER configuration
logger = logging.getLogger(__name__)
logger.info(f"Logging configured. Log file path: {log_file_path}") # Log the path
# --- End Logging Configuration ---

# Function to run the MCP server in a separate thread
def run_mcp_server():
    # Remove ONLY the console/stream handler for the MCP thread to keep stdio clean
    root_logger = logging.getLogger()
    handler_to_remove = None
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler_to_remove = handler
            break # Found the console handler, stop looking

    if handler_to_remove:
        logger.info(f"MCP Mode: Removing console handler {handler_to_remove}")
        root_logger.removeHandler(handler_to_remove)
    else:
        logger.warning("MCP Mode: Console handler (StreamHandler) not found to remove.")
        
    # Import and run MCP server
    from src.mcp_bridge import create_mcp_server
    mcp = create_mcp_server()
    logger.info("Starting MCP server with stdio transport")
    # os._exit skips atexit/handler flushing, so shut logging down explicitly first.
    try:
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"MCP server thread failed: {e}", exc_info=True)
        logging.shutdown()
        os._exit(1)

    # mcp.run() returns when the client closes stdin; uvicorn.run() is still
    # blocking the main thread, so exit the whole process rather than linger.
    logger.info("MCP server stopped (stdin closed); exiting process")
    logging.shutdown()
    os._exit(0)

if __name__ == "__main__":
    # Add the current directory to the Python path
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
        # Logger might not be fully configured here yet if run as main script initially
        # print(f"Added {project_dir} to Python path") 
    
    # Force PYTHONPATH for reloader (remains important)
    os.environ["PYTHONPATH"] = f"{project_dir}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
    logger.info(f"Set PYTHONPATH to include {project_dir}")
    
    # Load environment variables
    load_dotenv()

    # Start MCP server thread if stdin is not a TTY
    if not os.isatty(0):
        logger.info("MCP client detected via stdin: Starting MCP server thread")
        mcp_thread = threading.Thread(target=run_mcp_server, daemon=True)
        mcp_thread.start()
        logger.info("MCP server thread launched")
    else:
        logger.info("Running in HTTP-only mode (stdin is a TTY)")
        # Ensure console handler is present if we started in HTTP mode
        root_logger = logging.getLogger()
        has_console = any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers)
        if not has_console:
            logger.warning("Console handler missing in HTTP mode, adding default.")
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(logging.Formatter(LOGGING_CONFIG['formatters']['default']['format']))
            root_logger.addHandler(console_handler)

    # FastAPI/Uvicorn settings
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8000))
    # Default off: uvicorn's reloader spawns a subprocess, which breaks stdio
    # MCP mode and is undesirable on shared infrastructure.
    reload = os.getenv("RELOAD", "false").lower() == "true"

    logger.info(f"Starting FastAPI server on {host}:{port}...")
    logger.info(f"Reload mode: {'Enabled' if reload else 'Disabled'}")

    # Run Uvicorn, telling it to use our logging config
    try:
        uvicorn.run(
            "src.server:app", 
            host=host, 
            port=port, 
            reload=reload, 
            log_config=LOGGING_CONFIG # Pass our config dict
        )
    except Exception as e:
        logger.error(f"Error starting FastAPI server: {e}", exc_info=True)
        sys.exit(1) 