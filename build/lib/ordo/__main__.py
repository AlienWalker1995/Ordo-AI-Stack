"""Enable `python -m ordo` as an alias for the CLI entrypoint."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
