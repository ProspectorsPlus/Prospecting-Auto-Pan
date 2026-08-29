#!/usr/bin/env python3
"""Treasure macro launcher.

    python3 treasure.py              launch the GUI
    python3 treasure.py --calibrate  hand-derive DIG_TRIGGER_PIXEL /
                                      CAP_FULL_PIXEL for a new layout
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)

if __name__ == "__main__":
    if "--calibrate" in sys.argv[1:]:
        from prospector_engine.engine import calibrate
        calibrate()
    else:
        from treasure_gui import main
        main()
