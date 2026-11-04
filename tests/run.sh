#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PROJECT_DIR"
bash -n install.sh scripts/run_local.sh tests/run.sh
python3 -m compileall -q installer.py ptero tests scripts
if python3 -m ruff --version >/dev/null 2>&1; then
    python3 -m ruff check .
    python3 -m ruff format --check .
else
    printf '%s\n' 'Ruff unavailable; install it with: python3 -m pip install ruff==0.8.6'
fi
python3 scripts/check_translations.py
python3 -m unittest discover -s tests -v
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck install.sh scripts/run_local.sh tests/run.sh
else
    printf '%s\n' 'ShellCheck unavailable; Bash syntax validation passed.'
fi
if [[ "${1:-}" == "--live" ]]; then
    python3 tests/live_check.py
fi
