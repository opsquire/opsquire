#!/usr/bin/env bash
# Every check this repository runs, in one place: the release workflow calls it, and any other machine that checks a
# commit runs the same. Steps: lint, tests, e2e, package; none runs them all, in that order.
# Needs Python 3.13+ with `pip install -e ".[lint,test]"`, shellcheck, and for package: build, twine and uv.
set -euo pipefail
cd "$(dirname "$0")/.."
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-ci}" GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-ci@example.invalid}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-ci}" GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-ci@example.invalid}"

lint() {
  ruff check . && ruff format --check .
  mypy src tests scripts
  vulture
  (cd src/opsquire/data/helpers && mypy --config-file ../mypy.ini opsquire_helpers.py)
  (cd src/opsquire/data/templates && MYPYPATH=../helpers mypy --config-file ../mypy.ini main.py verify.py read.py)
  shellcheck -x -P src/opsquire/data/helpers src/opsquire/data/helpers/opsquire.sh src/opsquire/data/templates/*.sh \
    scripts/ci.sh
}

tests() {
  python -m pytest -q -n auto -p no:cacheprovider -W error::DeprecationWarning tests
}

e2e() {
  python scripts/full_power.py </dev/null
}

package() {
  rm -rf dist
  python -m build
  twine check dist/*
  local tmp
  tmp=$(mktemp -d)
  UV_TOOL_DIR="$tmp/tools" UV_TOOL_BIN_DIR="$tmp/bin" \
    uv tool install --quiet --python 3.13 --with ruff --with mypy dist/*.whl
  "$tmp/bin/opsquire" --version
  "$tmp/tools/opsquire/bin/python" - <<'PY'
from importlib import resources
data = resources.files("opsquire").joinpath("data")
missing = [f for f in ("ui/index.html", "ui/css/tokens.css", "ui/js/boot.js", "AGENTS.md", "retinue.json",
                       "helpers/opsquire.sh", "templates/main.py", "templates/verify.sh",
                       "skills/opsquire/SKILL.md") if not data.joinpath(f).is_file()]
assert not missing, f"wheel is missing package data: {missing}"
PY
  (
    unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
    export HOME="$tmp/stranger" PATH="$tmp/bin:$PATH"
    mkdir -p "$HOME"
    opsquire init "$HOME/ops-home" --budget 15 | tee "$tmp/init.log"
    task=$(sed -n 's/^first task: created and ready (\([^)]*\)).*/\1/p' "$tmp/init.log")
    test -n "$task"
    opsquire run "$task" </dev/null
  )
  rm -rf "$tmp"
}

steps=("$@")
[ ${#steps[@]} -gt 0 ] || steps=(lint tests e2e package)
for step in "${steps[@]}"; do
  case "$step" in
    lint | tests | e2e | package) printf '\n== %s\n' "$step" && "$step" ;;
    *) echo "unknown step: $step (lint, tests, e2e, package)" >&2 && exit 2 ;;
  esac
done
printf '\n== passed: %s\n' "${steps[*]}"
