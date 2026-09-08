"""Run Personal CIO from any directory, including through the installed symlink."""

import os
from pathlib import Path
import sys


if __name__ == "__main__":
    project = Path(__file__).resolve().parents[3]
    os.chdir(project)
    os.execv(sys.executable, [sys.executable, "-m", "cio", *sys.argv[1:]])
