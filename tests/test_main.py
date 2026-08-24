"""Startup auth gate: run() must refuse to serve traffic with no secret and no explicit opt-out."""

import pytest

from webstation_broker import main, settings


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
