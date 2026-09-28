"""Package executable entrypoint for python -m battlelab."""

import sys
from battlelab.cli import main

if __name__ == "__main__":
    sys.exit(main())
