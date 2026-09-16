"""`python -m clipforge.ui [--host H] [--port P] [--no-browser] [--config clipforge.yaml]`."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..config import CONFIG_ENV
from . import DEFAULT_HOST, DEFAULT_PORT, main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m clipforge.ui", description="ClipForge local web UI.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--config", type=Path, default=None, help="clipforge.yaml to use (default ./clipforge.yaml or $CLIPFORGE_CONFIG)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    if args.config is not None:
        if not args.config.is_file():
            sys.exit(f"config file not found: {args.config}")
        os.environ[CONFIG_ENV] = str(args.config)
    main(host=args.host, port=args.port, open_browser=not args.no_browser)
