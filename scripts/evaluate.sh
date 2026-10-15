#!/usr/bin/env bash
# Bootstrap this checkout only; Docker/Linux are explicit system prerequisites.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
python_bin="${PYTHON:-python3}"
venv="$repo/.harness/eval-venv"
if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "Python 3.11+ is required. Install python3 and python3-venv first." >&2
  exit 1
fi
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
if [[ ! -x "$venv/bin/python" ]]; then
  "$python_bin" -m venv "$venv" || {
    echo "Cannot create venv. On Debian/Ubuntu: sudo apt install python3-venv" >&2
    exit 1
  }
fi
# Constraints pin the provider, patch implementation, and official grader used by this profile.
"$venv/bin/python" -m pip install --disable-pip-version-check \
  -c "$repo/scripts/evaluation-constraints.txt" -e "$repo[eval]"
export PYTHONNOUSERSITE=1
export PYTHONPATH="$repo/src"
export OPENAI_AGENTS_DISABLE_TRACING=1
exec "$venv/bin/python" "$repo/scripts/evaluate.py" "$@"

