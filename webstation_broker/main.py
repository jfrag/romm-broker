"""Command-line entry point that serves the broker with uvicorn."""

import uvicorn

from . import settings
from .app import create_app


def run() -> None:
    """Serve the broker with uvicorn on the configured host and port.

    The app is built here rather than handed to uvicorn as a factory string so
    the no-auth startup gate in `create_app` runs before the port is bound, and
    so there is one gate rather than two that can drift apart. Uvicorn's own
    logging configuration is switched off so the format set up in
    `webstation_broker.app` is the one that applies.

    Raises:
        SystemExit: When `BROKER_SECRET` is unset and dev mode is not explicitly
            enabled, since every session-lifecycle endpoint would otherwise be
            reachable with no auth.
    """
    uvicorn.run(
        create_app(),
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,
    )


if __name__ == "__main__":
    run()
