"""Entry point: ``python3 -m deddrop``."""
import sys

from .service import main

if __name__ == "__main__":
    sys.exit(main())
