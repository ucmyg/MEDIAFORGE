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
    """Idempotent. Console handler via rich; file handler at <log_dir>/clipforge.log if log_dir is given."""
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
        target = log_dir / "clipforge.log"
        for h in root.handlers:
            if isinstance(h, logging.handlers.RotatingFileHandler) and Path(h.baseFilename) == target.resolve():
                return
        fh = logging.handlers.RotatingFileHandler(target, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("clipforge") else f"clipforge.{name}")
