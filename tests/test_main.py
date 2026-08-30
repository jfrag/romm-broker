"""Process startup: the auth gate, and what the app says about its static mount.

The gate lives in `create_app` rather than in `run` alone, so pointing uvicorn
or gunicorn straight at the factory is covered by the same check the console
script goes through.
"""

import logging
from pathlib import Path

import pytest

from webstation_broker import main, settings
from webstation_broker.app import create_app


@pytest.fixture(autouse=True)
def no_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    """Stand-in for the real server: reaching run() here is a gate bug, not a test that should listen."""
    calls = []
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    return calls


def test_refuses_to_start_with_no_secret_and_no_dev_mode(
    monkeypatch: pytest.MonkeyPatch, no_uvicorn: list[tuple[tuple, dict]]
) -> None:
    """Startup refuses to run with no secret and no dev mode."""
    monkeypatch.setattr(settings, "BROKER_SECRET", "")
    monkeypatch.setattr(settings, "DEV_MODE", False)

    with pytest.raises(SystemExit):
        main.run()

    assert no_uvicorn == []


def test_starts_when_a_secret_is_set(
    monkeypatch: pytest.MonkeyPatch, no_uvicorn: list[tuple[tuple, dict]]
) -> None:
    """Startup runs when a secret is set."""
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    monkeypatch.setattr(settings, "DEV_MODE", False)

    main.run()

    assert len(no_uvicorn) == 1


def test_starts_with_no_secret_when_dev_mode_is_explicitly_on(
    monkeypatch: pytest.MonkeyPatch, no_uvicorn: list[tuple[tuple, dict]]
) -> None:
    """Startup runs with no secret when dev mode is explicitly on."""
    monkeypatch.setattr(settings, "BROKER_SECRET", "")
    monkeypatch.setattr(settings, "DEV_MODE", True)

    main.run()

    assert len(no_uvicorn) == 1


def test_building_the_app_directly_is_gated_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_app refuses to build with no secret and no dev mode.

    A server pointed at the factory never goes through run(), so a gate that
    lived only there would let the broker come up unauthenticated.
    """
    monkeypatch.setattr(settings, "BROKER_SECRET", "")
    monkeypatch.setattr(settings, "DEV_MODE", False)

    with pytest.raises(SystemExit):
        create_app()


def test_building_the_app_directly_is_allowed_with_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_app builds when a secret is set."""
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    monkeypatch.setattr(settings, "DEV_MODE", False)

    assert create_app() is not None


def test_a_missing_frontend_dist_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A frontend dist that is not there is logged, rather than silently skipping the mount.

    Without the log the only symptom is every page under the prefix answering
    404, which reads like a routing bug rather than a missing build.
    """
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    monkeypatch.setattr(settings, "DEV_MODE", False)
    monkeypatch.setattr(settings, "FRONTEND_DIST", tmp_path / "never-built")

    with caplog.at_level(logging.ERROR, logger="webstation_broker.app"):
        create_app()

    assert any("never-built" in r.getMessage() for r in caplog.records)


def test_a_present_frontend_dist_is_mounted_without_complaint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A frontend dist that is there is mounted, and nothing is logged as an error."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    monkeypatch.setattr(settings, "DEV_MODE", False)
    monkeypatch.setattr(settings, "FRONTEND_DIST", dist)

    with caplog.at_level(logging.INFO, logger="webstation_broker.app"):
        create_app()

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert any("serving the frontend" in r.getMessage() for r in caplog.records)
