"""FastAPI application factory.

The app is mounted under settings.PREFIX (default /streaming) so the same
paths work behind nginx, a reverse proxy, or uvicorn directly. Outside dev
mode the built frontend is served as static files at the prefix root; in dev
mode vite serves the frontend instead.
"""

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, room, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [broker] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def create_app() -> FastAPI:
    inner = FastAPI(title="webstation-broker")
    inner.include_router(api.router)
    inner.include_router(room.router)

    if not settings.DEV_MODE and settings.FRONTEND_DIST.is_dir():
        inner.mount(
            "/",
            StaticFiles(directory=settings.FRONTEND_DIST, html=True),
            name="frontend",
        )

    if settings.PREFIX:
        root = FastAPI()
        root.mount(settings.PREFIX, inner)
        return root
    return inner
