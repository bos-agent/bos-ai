#!/usr/bin/env bash
# Assert the base distribution installs as a library: the rings import, and no
# console script is produced. Extras govern which *dependencies* are installed,
# not which files ship, so this can only be verified by a real install — the
# ring-isolation guards check import direction and cannot catch a dependency
# landing in the wrong extra.
#
# Run locally before a release; run by .github/workflows/packaging.yml on every PR.
set -euo pipefail

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

uv build --wheel --out-dir "$workdir/dist"
uv venv --python 3.13 "$workdir/venv"
VIRTUAL_ENV="$workdir/venv" uv pip install --quiet "$workdir"/dist/*.whl

if [ -e "$workdir/venv/bin/boscli" ]; then
  echo "FAIL: base install produced a 'boscli' executable; [project.scripts] should be empty" >&2
  exit 1
fi

"$workdir/venv/bin/python" - <<'PY'
import bos.config  # noqa: F401
import bos.core  # noqa: F401
import bos.exts  # noqa: F401

print("base install: bos.core, bos.config, bos.exts all import")
PY

echo "PASS: base install is a library"
