#!/usr/bin/env bash
# Bootstrap this checkout only; Docker/Linux are explicit system prerequisites.
set -euo pipefail

select_eval_venv() {
  local repository="$1" kernel_release="$2" python_command="$3"
  if [[ -n "${HARNESS_EVAL_VENV:-}" ]]; then
    printf '%s\n' "$HARNESS_EVAL_VENV"
  elif [[ "$repository" == /mnt/[a-zA-Z]/* && "${kernel_release,,}" == *microsoft* ]]; then
    local cache_root="${XDG_CACHE_HOME:-$HOME/.cache}"
    local checkout_hash
    checkout_hash="$("$python_command" -c \
      'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:20])' \
      "$repository")"
    printf '%s\n' "$cache_root/coding-harness/eval-venvs/$checkout_hash"
  else
    printf '%s\n' "$repository/.harness/eval-venv"
  fi
}

bootstrap() {
  local repo python_bin venv kernel_release
  repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$repo"
  python_bin="${PYTHON:-python3}"
  if ! command -v "$python_bin" >/dev/null 2>&1; then
    echo "Python 3.11+ is required. Install python3 and python3-venv first." >&2
    exit 1
  fi
  "$python_bin" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
  kernel_release="$(uname -r)"
  venv="$(select_eval_venv "$repo" "$kernel_release" "$python_bin")"
  echo "Evaluation Python environment: $venv"
  if [[ ! -x "$venv/bin/python" ]]; then
    "$python_bin" -m venv "$venv" || {
      echo "Cannot create venv. On Debian/Ubuntu: sudo apt install python3-venv" >&2
      exit 1
    }
  fi
  # Constraints pin the provider, patch implementation, and official grader for this profile.
  "$venv/bin/python" -m pip install --disable-pip-version-check \
    -c "$repo/scripts/evaluation-constraints.txt" -e "$repo[eval]"
  export PYTHONNOUSERSITE=1
  export PYTHONPATH="$repo/src"
  export OPENAI_AGENTS_DISABLE_TRACING=1
  exec "$venv/bin/python" "$repo/scripts/evaluate.py" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  bootstrap "$@"
fi

