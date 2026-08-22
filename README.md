# webstation-broker

Session broker and collaboration interface for the RomM webstation container.
The container hosts one play session at a time: a game is activated over REST,
the broker launches the emulator (restoring save data if provided) and returns
a token URL that lands on a collab room with the selkies stream iframed inside
it. Exiting saves state, stops the emulator, and archives the session's save
delta.

**Documentation: https://romm-streaming.github.io/romm-broker/**

| | |
| --- | --- |
| [Overview](https://romm-streaming.github.io/romm-broker/docs) | what the broker is and how a session flows |
| [Configuration](https://romm-streaming.github.io/romm-broker/docs/configuration) | every environment variable, for the broker and for each emulator |
| [Reverse proxy](https://romm-streaming.github.io/romm-broker/docs/deployment/reverse-proxy) | serving the container from RomM's origin |
| [Emulators](https://romm-streaming.github.io/romm-broker/docs/emulators) | what each launcher supports and its one-time desktop setup |
| [REST API](https://romm-streaming.github.io/romm-broker/docs/api) | activate, join, save states, exit, save archives, status |
| [Developer guide](https://romm-streaming.github.io/romm-broker/docs/developer) | layout, conventions, adding an emulator, the generated Python reference |

## Layout

```
webstation_broker/       FastAPI app (pip installable, console script webstation-broker)
  api.py                 activate / join / exit / status / state and archive REST endpoints
  room.py                collab websocket (chat, webcam fanout, resolution, input passing)
  session.py             single-session state, room broadcast, gamepad/MK assignment
  selkies.py             token pushes to the selkies control plane
  saves.py               save archive restore on activate, delta dump on exit
  emulators/             one launcher per emulator, all subclassing emulators.base.Emulator
frontend/                vite vanilla-JS room interface
tests/                   pytest suite
docs/                    the documentation site (Fumadocs, deployed to GitHub Pages)
```

## Development

```bash
uv venv && uv pip install -e . pytest "ruff==0.16.1"
.venv/bin/pytest -q
.venv/bin/ruff check webstation_broker tests
```

Every module, class and function carries a Google-style docstring and full
type hints; ruff enforces both and the developer reference on the docs site is
generated from them. To run the broker from source inside the container, set
`BROKER_DEV_MODE=true` and mount the checkout at `/broker`; see
[Dev mode](https://romm-streaming.github.io/romm-broker/docs/container/dev-mode).

Building the docs locally:

```bash
cd docs && npm ci && cd ..
uv pip install ./docs/node_modules/fumadocs-python
PYTHONPATH=. .venv/bin/fumapy-generate webstation_broker --dir docs
cd docs && npm run generate && npm run dev
```

## Releases

Versions are semver, cut by release-please off `master` from conventional
commit subjects (`feat:` bumps the minor, `fix:` the patch). Each release is
consumable as a source tarball at
`https://github.com/romm-streaming/romm-broker/archive/refs/tags/vX.Y.Z.tar.gz`.
See [Releases](https://romm-streaming.github.io/romm-broker/docs/releases) and
[CHANGELOG.md](CHANGELOG.md).
