"""Command-line entry point that serves the broker with uvicorn."""

import uvicorn

from . import settings


def run() -> None:
    """Serve the application factory with uvicorn on the configured host and port.

    Uvicorn's own logging configuration is switched off so the format set up in
    `webstation_broker.app` is the one that applies.
    """
    uvicorn.run(
        "webstation_broker.app:create_app",
        factory=True,
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,
    )


if __name__ == "__main__":
    run()
