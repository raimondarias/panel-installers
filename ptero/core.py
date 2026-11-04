#!/usr/bin/env python3
"""Pterodactyl Panel v2 and Wings installer facade. Standard library only.

The implementation lives in focused modules; this module composes the Installer
and re-exports the historical public API (``from ptero.core import ...``).
"""
# These module imports are part of the historical namespace: ``installer.py``
# star-imports it and integrations patch names such as ``ptero.core.time.sleep``.
import argparse
import contextlib
import datetime as dt
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .common import (PANEL_VERSION, PROJECT, SCHEMA, WINGS_VERSION, Cancelled, InstallerError, NoRedirect,
                     atomic_write, node_configuration, read_json, secure_extract, valid_domain, valid_email,
                     valid_host, valid_url, valid_version, version_key, write_json)
from .ui import UI
from .runtime import Paths, Runner
from .host import HostMixin
from .panel_ops import PanelMixin
from .wings_ops import WingsMixin
from .maintenance import MaintenanceMixin


class Installer(HostMixin, PanelMixin, WingsMixin, MaintenanceMixin):
    def __init__(self, paths=None, ui=None, runner=None):
        self.p = paths or Paths()
        self.ui = ui or UI()
        self.run = runner or Runner(self.ui, self.p.log)

    def load(self, path):
        state = read_json(path)
        if not isinstance(state, dict) or state.get("schema") != SCHEMA or state.get("owner") != PROJECT:
            raise InstallerError("This installation or backup does not belong to this installer.")
        if Path(path) == self.p.wings_state and self.p.config.is_file():
            text = self.p.config.read_text()
            if text.lstrip().startswith("{"):
                config = json.loads(text)
                self.run.secrets.update(str(config.get(key, "")) for key in ("token", "token_id"))
            else:
                for match in re.finditer(r"(?m)^token(?:_id)?:\s*[\"']?([A-Za-z0-9_-]+)", text):
                    self.run.secrets.add(match[1])
        return state
