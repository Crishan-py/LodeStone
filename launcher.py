"""LodeStone entry point.

The launcher itself lives in the `LodeStone` package next to this file --
this module exists so `python launcher.py` and a PyInstaller onefile build keep
working unchanged.
"""

import sys

from LodeStone import serve

if __name__ == "__main__":
    sys.exit(serve())
