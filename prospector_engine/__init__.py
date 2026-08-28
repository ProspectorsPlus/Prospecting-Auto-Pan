"""Treasure Navigator engine package.

The public surface stays deliberately small: importing this package must not
pull in Tk, OpenCV, or any OS-specific module, so both platform test suites and
the release-only deadman helper can import from it freely (bug B1).

Feature-level exports are added only once the corresponding gate passes
(plan 13.3); until then callers import the module they actually need.
"""

__version__ = "0.5.0"
ENGINE_VERSION = __version__

__all__ = ["ENGINE_VERSION", "__version__"]
