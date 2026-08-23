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
