#!/usr/bin/env python3
"""Prospector Lite engine launcher.

[Phase 04 C6] The engine implementation moved to prospector_engine/engine.py
(the one shared runtime for Prospector Lite and Prospector
Studio). This launcher keeps every historical entrypoint working unchanged:

    python3 prospecting_old.py [calibrate|calibrate-text|log|monitor]
    python3 prospecting_old.py --ipc [--home DIR] [--host NAME]
                               [--protocol N] [--sim SCENARIO]

and the frozen re-exec path (runpy.run_path(..., run_name="__main__")).
Importing this module yields the engine module itself. The engine treats this
file's directory as its home (prospecting_config.json lives next to it), which
the package move must not change.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PPENGINE_HOME", _HERE)
for _p in (_HERE, os.path.dirname(_HERE)):
    if _p not in sys.path:
        sys.path.append(_p)

import prospector_engine.engine as _engine

if __name__ == "__main__":
    _engine._cli_main()
else:
    sys.modules[__name__] = _engine
