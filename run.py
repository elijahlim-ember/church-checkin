"""Start the app without installing it: python run.py serve  (or any other command, see --help)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checkin.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["serve"]))
