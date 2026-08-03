import uvicorn

from . import settings


def run() -> None:
    uvicorn.run(
        "webstation_broker.app:create_app",
        factory=True,
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,
    )


if __name__ == "__main__":
    run()
