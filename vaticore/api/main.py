"""FastAPI application skeleton.

Kept intentionally thin. The forecasting core stays cleanly separated from the
API so models can evolve without touching this layer. Install the service extra
to run it: uv sync --extra service, then uv run uvicorn vaticore.api.main:app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app() -> FastAPI:
    """Application factory. Endpoints are added as the engine matures."""
    from fastapi import FastAPI

    from vaticore import __version__

    app = FastAPI(title="Vaticore", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # TODO: POST /forecast and /advisory once the engine and decision layer are wired.
    return app
