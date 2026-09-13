"""Entry point of the installed program (PyInstaller needs a plain script)."""

import sys

from crossing_count.app import main

if __name__ == "__main__":
    sys.exit(main())
