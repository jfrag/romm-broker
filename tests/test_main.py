"""Startup auth gate: run() must refuse to serve traffic with no secret and
no explicit opt-out."""

import pytest

from webstation_broker import main, settings


@pytest.fixture(autouse=True)
def no_uvicorn(monkeypatch):
    """Stand-in for the real server: a run() that reaches this is a bug in
    the gate above it, not something a test should actually let listen."""
    calls = []
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    return calls


def test_refuses_to_start_with_no_secret_and_no_dev_mode(monkeypatch, no_uvicorn):
    monkeypatch.setattr(settings, "BROKER_SECRET", "")
    monkeypatch.setattr(settings, "DEV_MODE", False)

    with pytest.raises(SystemExit):
        main.run()

    assert no_uvicorn == []


def test_starts_when_a_secret_is_set(monkeypatch, no_uvicorn):
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    monkeypatch.setattr(settings, "DEV_MODE", False)

    main.run()

    assert len(no_uvicorn) == 1


def test_starts_with_no_secret_when_dev_mode_is_explicitly_on(monkeypatch, no_uvicorn):
    monkeypatch.setattr(settings, "BROKER_SECRET", "")
    monkeypatch.setattr(settings, "DEV_MODE", True)

    main.run()

    assert len(no_uvicorn) == 1
