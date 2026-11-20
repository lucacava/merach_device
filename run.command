#!/bin/zsh
# Double-click (or `open run.command`) to start the connector in macOS Terminal, which holds the Bluetooth
# permission. Extra arguments are passed to merach_connector.py.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
( sleep 2; open "http://localhost:8765" ) &
exec .venv/bin/python merach_connector.py "$@"
