#!/usr/bin/env python3
"""Print the CHANGELOG section of one version, for the GitHub Release notes."""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def notes(version):
    text = (ROOT / "CHANGELOG.md").read_text()
    match = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match or not match[1].strip():
        raise SystemExit(f"CHANGELOG.md has no section for {version}.")
    return match[1].strip() + "\n"


if __name__ == "__main__":
    sys.stdout.write(notes(sys.argv[1]))
