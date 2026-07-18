#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
PYTHON="$ROOT/.build-venv/bin/python"
AUDIT_LOCK="$ROOT/requirements-audit.txt"
BUILD_LOCK="$ROOT/requirements-build.txt"
AUDIT_HOME=$(mktemp -d "${TMPDIR:-/tmp}/openusage-audit.XXXXXX")
trap 'rm -rf "$AUDIT_HOME"' EXIT

[[ -x "$PYTHON" && -f "$AUDIT_LOCK" && -f "$BUILD_LOCK" ]] || {
  print -u2 "dependency_audit_unavailable"
  exit 2
}

"$PYTHON" -m venv "$AUDIT_HOME/venv"
"$AUDIT_HOME/venv/bin/python" -m pip install \
  --disable-pip-version-check --no-deps --require-hashes \
  --requirement "$AUDIT_LOCK"
"$AUDIT_HOME/venv/bin/python" -m pip_audit \
  --requirement "$BUILD_LOCK" --progress-spinner off --strict
print "dependency_audit_ok"
