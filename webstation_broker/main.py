"""Command-line entry point that serves the broker with uvicorn."""

import logging

import uvicorn

from . import settings

log = logging.getLogger(__name__)

_NO_AUTH_BANNER = (
    "\n"
    + "!" * 72
    + "\n"
    "!! BROKER_SECRET IS NOT SET - REFUSING TO START\n"
    "!!\n"
    "!! Every session-lifecycle endpoint (activate, save/load state,\n"
    "!! swap-disc, state-file, memory-card, exports, imports) would be\n"
    "!! reachable by ANY caller that can reach this port, with no auth.\n"
    "!!\n"
    "!! Set BROKER_SECRET to a strong random value before starting the\n"
    "!! broker, or set BROKER_DEV_MODE=true to explicitly run without\n"
    "!! authentication (local development only).\n" + "!" * 72
)


def run() -> None:
    """Serve the application factory with uvicorn on the configured host and port.

    Refuses to start when `BROKER_SECRET` is unset and dev mode is not
    explicitly enabled, since every session-lifecycle endpoint would otherwise
    be reachable with no auth. Uvicorn's own logging configuration is switched
    off so the format set up in `webstation_broker.app` is the one that
    applies.
    """
    if not settings.BROKER_SECRET and not settings.DEV_MODE:
        log.critical(_NO_AUTH_BANNER)
        raise SystemExit(1)
    if not settings.BROKER_SECRET:
        log.warning(
            "BROKER_DEV_MODE is set, so the broker is starting with BROKER_SECRET "
            "unset: every session-lifecycle endpoint is unauthenticated. Do not "
            "expose this broker beyond local development."
        )
    uvicorn.run(
        "webstation_broker.app:create_app",
        factory=True,
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,
    )


if __name__ == "__main__":
    run()
