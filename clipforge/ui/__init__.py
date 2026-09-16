"""Local web UI (v2): a FastAPI backend (server.py) serving the vanilla HTML/JS frontend in static/.

`clipforge ui` and `python -m clipforge.ui` both end in main(): build Settings + DB, create_app, print the URL, open
the browser after a second and run uvicorn on 127.0.0.1:8765. Local tool: no authentication, so keep the bind
address on the loopback interface.
"""
from __future__ import annotations

import threading
import webbrowser

from ..config import Settings, get_settings
from ..db import DB
from ..log import console, get_logger, setup_logging
from .server import ANY_HOST, create_app

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "allowed_hosts_for", "create_app", "main", "url_for"]

log = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BROWSER_DELAY_S = 1.0  # give uvicorn a moment to bind before the browser knocks


def url_for(host: str, port: int) -> str:
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    if ":" in shown and not shown.startswith("["):  # bare IPv6 literal
        shown = f"[{shown}]"
    return f"http://{shown}:{port}/"


def allowed_hosts_for(host: str) -> set[str]:
    """Host names the request guard accepts besides the loopback ones: the bind address itself, or any host at all when
    the server is deliberately bound to every interface (users then reach it through whatever LAN name they have)."""
    return {ANY_HOST} if host in ("0.0.0.0", "::") else {host.strip("[]").lower()}


def open_browser_later(url: str, delay: float = BROWSER_DELAY_S) -> threading.Timer:
    """Daemon timer that opens `url` in the default browser; a missing browser is logged, never fatal."""

    def _open() -> None:
        try:
            if not webbrowser.open(url):
                log.warning("no browser opened %s; open it by hand", url)
        except Exception as err:  # broken $BROWSER entry
            log.warning("could not open a browser for %s (%s: %s)", url, type(err).__name__, err)

    timer = threading.Timer(delay, _open)
    timer.daemon = True
    return timer


def main(settings: Settings | None = None, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    """Serve the UI until Ctrl-C. Without `settings` the usual clipforge.yaml / $CLIPFORGE_CONFIG lookup applies."""
    import uvicorn

    if settings is None:
        settings = get_settings(reload=True)
        setup_logging(settings.logs_dir)
    db = DB(settings.db_path)
    app = create_app(settings, db, allowed_hosts=allowed_hosts_for(host))
    url = url_for(host, port)
    console.print(f"ClipForge UI: {url}  (Ctrl-C stops the server)", highlight=False)
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(f"[yellow]warning:[/] the UI has no login; binding {host} exposes it to the network", highlight=False)
    if open_browser:
        open_browser_later(url).start()
    log.info("ui: serving on %s (workspace %s)", url, settings.workspace_dir)
    uvicorn.run(app, host=host, port=port, log_level="warning")
