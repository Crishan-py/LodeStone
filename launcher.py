"""LOADSTONE entry point.

The launcher itself lives in the `LOADSTONE` package next to this file --
this module exists so `python launcher.py` and a PyInstaller onefile build keep
working unchanged.
"""

import sys

from LOADSTONE import serve

if __name__ == "__main__":
    sys.exit(serve())
