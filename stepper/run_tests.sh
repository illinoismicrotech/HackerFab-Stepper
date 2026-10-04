#!/usr/bin/env bash
# Run the test suite. On Linux the GUI tests use an invisible virtual screen (Xvfb)
# when it is installed, so no windows pop up on your desktop.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
PYTHON="${PYTHON:-$(ls -d .venv-3.1[0-3]/bin/python 2>/dev/null | head -1)}"
PYTHON="${PYTHON:-python3}"
export PYTHONPATH=src
if [[ "$(uname)" == "Linux" ]] && command -v xvfb-run >/dev/null; then
    exec xvfb-run -a -s "-screen 0 1920x1080x24" "$PYTHON" -m unittest discover -s tests "$@"
fi
exec "$PYTHON" -m unittest discover -s tests "$@"
