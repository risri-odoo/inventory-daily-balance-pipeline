"""Allows `python -m inventory_pipeline <command>`."""

import sys

from inventory_pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())
