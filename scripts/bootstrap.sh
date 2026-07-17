#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
VENV="$ROOT/.build-venv"
REQUIREMENTS="$ROOT/requirements-build.txt"
PYTHON_BIN=${PYTHON_BIN:-${commands[python3]:-}}

[[ $(uname -s) == Darwin ]] || {
  print -u2 "OpenUsage Bar builds only on macOS"
  exit 1
}
[[ $(uname -m) == arm64 ]] || {
  print -u2 "OpenUsage Bar 0.2 supports Apple Silicon only"
  exit 1
}
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || {
  print -u2 "Python 3.11 or later is required"
  exit 1
}
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or later is required")
PY
xcode-select -p >/dev/null 2>&1 || {
  print -u2 "Xcode command-line tools are required"
  exit 1
}
command -v swift >/dev/null 2>&1 || {
  print -u2 "Swift 6.2 or later is required"
  exit 1
}

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip==26.1.2
"$VENV/bin/python" -m pip install \
  --disable-pip-version-check --no-deps --requirement "$REQUIREMENTS"
"$VENV/bin/python" -m pip check
cd "$ROOT"
"$VENV/bin/python" scripts/release_secret_scan.py
print "bootstrap_ready python=$($VENV/bin/python -c 'import platform; print(platform.python_version())')"
