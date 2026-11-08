"""Paths and settings.

Every setting can be passed on the command line; when it is not, the value comes from an
environment variable, and when that is not set either, from a default below.
"""

import os
from pathlib import Path

DEFAULT_OUTPUT_DIR = "output"
DEFAULT_DB_FILENAME = "inventory.db"

ENV_DB_PATH = "INV_DB_PATH"
ENV_OUTPUT_DIR = "INV_OUTPUT_DIR"


def default_output_dir() -> Path:
    """Return the output directory from INV_OUTPUT_DIR, or `output/` if it is not set."""
    return Path(os.environ.get(ENV_OUTPUT_DIR, DEFAULT_OUTPUT_DIR))


def default_db_path() -> Path:
    """Return the database path from INV_DB_PATH, or `<output dir>/inventory.db` if it is not set."""
    env_value = os.environ.get(ENV_DB_PATH)
    if env_value:
        return Path(env_value)
    return default_output_dir() / DEFAULT_DB_FILENAME
