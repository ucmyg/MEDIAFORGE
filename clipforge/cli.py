"""Typer CLI. Commands: add, run, review, publish, daemon, tick, auth, doctor, fixture, init-config."""
from __future__ import annotations

import typer

app = typer.Typer(name="clipforge", help="Long-form video -> captioned vertical clips -> YouTube Shorts / TikTok. Local and free.", no_args_is_help=True)


@app.callback()
def _main() -> None:
    pass


if __name__ == "__main__":
    app()
