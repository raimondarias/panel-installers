#!/usr/bin/env python3
"""Compatibility entry point. Implementation lives in the ptero package."""

from ptero.core import *  # Re-export the public API for existing integrations.
from ptero.cli import main

if __name__ == "__main__":
    sys.exit(main())
