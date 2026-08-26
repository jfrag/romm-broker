"""FastAPI application factory.

The app is mounted under `settings.PREFIX` (default `/streaming`) so the same
paths work behind nginx, a reverse proxy, or uvicorn directly. Outside dev
mode the built frontend is served as static files at the prefix root; in dev
mode vite serves the frontend instead.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from . import api, room, settings
from .emulators.base import reap_orphan
from .emulators.shadps4 import sweep_stale_extractions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [broker] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Reap an emulator orphaned by a previous broker process before serving.

    A fresh broker holds no session, so an emulator recorded by the process
    that came before is playing to nobody. Killing it here rather than at the
    next activate is what keeps it killable at all: exit answers 409 without a
    session, so otherwise the only way out is launching another game.

    Also sweeps shadPS4 archive-extraction scratch dirs left behind by a
    crashed broker process, before any new extraction can be in flight.

    Args:
        _app: The application being started; unused.

    Yields:
        Nothing; control passes to the running application once the orphan is reaped.
    """
    await anyio.to_thread.run_sync(reap_orphan)
    await anyio.to_thread.run_sync(sweep_stale_extractions)
    yield


def create_app() -> FastAPI:
    """Build the broker application, mounted under the configured prefix.

    The lifespan belongs to whichever app is actually served. Starlette never
    hands the lifespan scope to a mounted sub-app, so a startup hook on the
    inner app would silently never run behind a prefix.

    Returns:
        The application to serve: a bare root app with the broker mounted at
        `settings.PREFIX` when a prefix is set, otherwise the broker app itself.
    """
    prefixed = bool(settings.PREFIX)
    inner = FastAPI(
        title="webstation-broker", lifespan=None if prefixed else _lifespan
    )
    inner.include_router(api.router)
    inner.include_router(room.router)

    @inner.middleware("http")
    async def _no_referrer(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Session/viewer tokens travel as ?token= query params (WS auth has no
        # header alternative, and the room is iframe-embedded); a leaked
        # Referer header would hand a live token to whatever the page links
        # out to.
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    if not settings.DEV_MODE and settings.FRONTEND_DIST.is_dir():
        inner.mount(
            "/",
            StaticFiles(directory=settings.FRONTEND_DIST, html=True),
            name="frontend",
        )

    if prefixed:
        root = FastAPI(lifespan=_lifespan)
        root.mount(settings.PREFIX, inner)
        return root
    return inner
