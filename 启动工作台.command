#!/bin/bash
# Run with: bash ./启动工作台.command
set -e

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON=""
for candidate in "$SCRIPT_DIR/.venv/bin/python" python3.14 python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  printf '%s\n' '需要 Python 3.12 或以上。安装后重新运行此脚本，或在项目中创建 .venv。' >&2
  exit 1
fi

exec "$PYTHON" -m auroradb --demo --web --port 0 --db "$SCRIPT_DIR/data/mac-demo" "$@"
