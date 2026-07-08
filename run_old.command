#!/bin/bash
# Double-click to launch the OLD v1.0.0 macro on macOS (isolated from the latest).
cd "$(dirname "$0")/Prospectors_Old_v1.0.0" || exit 1
exec python3 "prospecting_app.py"
