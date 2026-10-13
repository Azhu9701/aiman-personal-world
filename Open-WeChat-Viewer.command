#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for candidate in "${AIMAN_PYTHON:-python3}" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    exec "$candidate" "$app_dir/personal_world.py" open "$@"
  fi
done
echo 'AIMAN Personal World requires Python 3.10 or later. Install it, or set AIMAN_PYTHON to its executable.' >&2
exit 2
