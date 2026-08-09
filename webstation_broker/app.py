"""FastAPI application factory.

The app is mounted under settings.PREFIX (default /streaming) so the same
paths work behind nginx, a reverse proxy, or uvicorn directly. Outside dev
mode the built frontend is served as static files at the prefix root; in dev
mode vite serves the frontend instead.
"""

import logging
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, room, settings
from .emulators.base import reap_orphan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [broker] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # A fresh broker holds no session, so an emulator recorded by the process
    # that came before is playing to nobody. Killing it here rather than at the
    # next activate is what keeps it killable at all: exit answers 409 without a
    # session, so otherwise the only way out is launching another game.
    await anyio.to_thread.run_sync(reap_orphan)
    yield


def create_app() -> FastAPI:
    inner = FastAPI(title="webstation-broker", lifespan=_lifespan)
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
