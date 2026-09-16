"""Logging setup: rich console + rotating file under paths.logs. Every module uses get_logger(__name__)."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()
_configured = False


def setup_logging(log_dir: str | Path | None = None, level: int = logging.INFO) -> None:
    """Idempotent. Console handler via rich; file handler at <log_dir>/clipforge.log if log_dir is given.

    Only one file handler ever exists: calling this again with another `log_dir` (the UI reloading a saved config)
    closes the previous file instead of writing every line to both.
    """
    global _configured
    root = logging.getLogger("clipforge")
    if not _configured:
        root.setLevel(logging.DEBUG)
        root.propagate = False
        rh = RichHandler(console=console, show_path=False, rich_tracebacks=False, markup=False)
        rh.setLevel(level)
        root.addHandler(rh)
        _configured = True
    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        target = (log_dir / "clipforge.log").resolve()
        for h in list(root.handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                if Path(h.baseFilename).resolve() == target:
                    return
                root.removeHandler(h)
                h.close()
        fh = logging.handlers.RotatingFileHandler(target, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)


def console_level() -> int:
    """Level of the rich console handler (INFO until setup_logging ran); handed to render workers under spawn."""
    for h in logging.getLogger("clipforge").handlers:
        if isinstance(h, RichHandler):
            return h.level
    return logging.INFO


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("clipforge") else f"clipforge.{name}")
