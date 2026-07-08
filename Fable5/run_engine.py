#!/usr/bin/env python3
"""Engine process entry — launched by app.py (or run standalone for a
UI-less session controlled purely by hotkeys)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.runtime import main

if __name__ == "__main__":
    main()
