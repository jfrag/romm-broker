# webstation-broker

Session broker and collaboration interface for the RomM webstation container.
The container hosts one play session at a time: a game is activated over REST,
the broker launches the emulator (restoring save data if provided) and returns
a token URL that lands on a collab room with the selkies stream iframed inside
it. Exiting saves state, stops the emulator, and archives the session's save
delta.

## How it works

```
webstation_broker/       FastAPI app (pip installable, console script webstation-broker)
  api.py                 activate / join / exit / status / context REST endpoints
  room.py                collab websocket (chat, webcam fanout, resolution, input passing)
  session.py             single-session state, room broadcast, gamepad/MK assignment
  selkies.py             token pushes to the selkies control plane (localhost:8083)
  saves.py               save archive restore on activate, delta dump on exit
  emulators/             emulator launchers (pcsx2, desktop)
frontend/                vite vanilla-JS room interface
```

* Everything is served under the `SUBFOLDER` prefix (default `/streaming/`).
  The same env var drives the nginx templating, the FastAPI mount, and the
  vite base.
* Each user gets a personal token. The broker pushes the full token map
  (`{token: {role, slot, mk_control}}`) to selkies, which enforces input
  routing per streaming connection. The room UI reassigns gamepads and
  mouse/keyboard by dragging icons onto users.
* Save data flows through zip archives scoped to the emulator's save subtrees
  (pcsx2: `memcards/`, `sstates/`). Activate restores an archive without
  rolling back newer files; exit zips everything modified since launch and
  uploads it to the callback origin. In dev mode nothing is uploaded — the
  archive is written under `/config/broker-exports/` and the exit report says
  what *would* have been sent. Outside dev mode the archive only lands on
  disk when the upload fails, so a dead callback never loses save data.
* The callback origin defaults to the parent: the broker is same-origined
  under the parent's `SUBFOLDER`, so activate derives it from the request
  itself (`X-Forwarded-Proto`/`X-Forwarded-Host`, else `Host`). A
  split-origin deployment can override it with `callback.base_url` in the
  activate payload.
* Emulators are added by subclassing `emulators.base.Emulator` and
  registering in `emulators/__init__.py`. The special `desktop` type launches
  the full webstation desktop for configuring emulators through the GUI.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `SUBFOLDER` | `/streaming/` | URL prefix for the whole app |
| `BROKER_SECRET` | unset | shared secret for the lifecycle endpoints (`X-Broker-Secret` header); unset disables auth |
| `ROM_ROOT` | `/romm` | activate rejects rom paths outside this root |
| `BROKER_EXPORT_DIR` | `/config/broker-exports` | where exit writes save archives (always in dev mode, otherwise only when the upload fails) |
| `BROKER_SAVE_UPLOAD_PATH` | `/api/webstation/saves` | path appended to the callback base URL when exit POSTs the save archive |
| `BROKER_SAVE_UPLOAD_TIMEOUT` | `30` | seconds allowed for the exit save upload |
| `BROKER_DEV_MODE` | unset | run from mounted source with uvicorn/vite hot reload; also disables the exit save upload (report-only) |

## Dev mode

```
git clone https://github.com/thelamer/romm-broker-dev.git
cd romm-broker-dev
docker run --rm -it \
  -e BROKER_DEV_MODE=true \
  -e PUID=1000 -e PGID=1000 \
  -v $(pwd):/broker \
  -v /path/to/roms:/romm \
  -v /path/to/config:/config \
  -p 3001:3001 \
  linuxserver/webstation:romm bash
```

With `BROKER_DEV_MODE=true` the mounted `/broker` is pip-installed editable,
uvicorn runs with `--reload` on 8000, and the vite dev server runs on 5173.
Without dev mode the installed package serves the built frontend itself.

## API

### Launch a game

```
curl -k -X POST https://localhost:3001/streaming/api/session/activate \
  -H 'Content-Type: application/json' \
  -d '{
    "user": { "id": 1, "username": "ryan", "display_name": "Ryan" },
    "emulator": "pcsx2",
    "rom": { "name": "Soul Calibre 2", "platform": "ps2", "path": "/romm/ps2/SC2.iso" }
  }'
```

Returns `{"status": "launching", "session_id": ..., "url": "/streaming/?token=<controller token>"}`.
Open `https://<host>:3001/streaming/?token=...` for the room with the game
streaming. Returns 409 if a session is already active.

`rom.path` may be a file or a game folder; the broker picks the best bootable
disc image (disc number first, then format ranking, chd > iso > ...).

### Launch a game with save data

Add a `save` object: `archive` is a zip restored before launch, `resume_slot`
loads that state slot once the VM is up. Both are optional and independent.

```
curl -k -X POST https://localhost:3001/streaming/api/session/activate \
  -H 'Content-Type: application/json' \
  -d '{
    "user": { "id": 1, "username": "ryan", "display_name": "Ryan" },
    "emulator": "pcsx2",
    "rom": { "name": "Soul Calibre 2", "platform": "ps2", "path": "/romm/ps2/SC2.iso" },
    "save": { "archive": "/config/incoming/4471.zip", "resume_slot": 10 }
  }'
```

### Launch the desktop

`emulator: "desktop"` streams the full webstation desktop so emulators can be
configured through the GUI. No `rom` or `save`; same room interface, same exit
teardown.

```
curl -k -X POST https://localhost:3001/streaming/api/session/activate \
  -H 'Content-Type: application/json' \
  -d '{ "emulator": "desktop", "user": { "id": 1, "username": "ryan", "display_name": "Ryan" } }'
```

### Add a user to the session

```
curl -k -X POST https://localhost:3001/streaming/api/session/join \
  -H 'Content-Type: application/json' \
  -d '{
    "user": { "id": 2, "username": "player2", "display_name": "Player Two" },
    "permission": "participant"
  }'
```

Returns `{"status": "joined", "url": "/streaming/?token=<personal token>", ...}`,
409 when no session is up. `permission: "readonly"` for spectators. Re-joining
the same user (by id, else username) replaces their old token.

### Exit

From the room UI's exit button (controller token) or directly:

```
curl -k -X POST 'https://localhost:3001/streaming/api/session/exit?slot=10' \
  -H 'X-Broker-Secret: <shared secret>'
```

Saves state to `slot`, stops the emulator, dumps the save delta, and uploads
it to the callback origin as multipart form data:
`POST {base_url}{BROKER_SAVE_UPLOAD_PATH}` with an `archive` file part
(zip, `{session_id}-{timestamp}.zip`) plus `session_id`, `emulator`, and —
when the rom carried them — `rom_id` / `rom_name` form fields. If activate
supplied `callback.token`, it is sent as `Authorization: Bearer <token>`.

The callback base URL is derived from the activate request (the parent
origin) unless the activate payload overrides it:

```json
"callback": { "base_url": "https://romm.example.com", "token": "<upload token>" }
```

In dev mode (`BROKER_DEV_MODE=true`) the upload is skipped: the archive is
written to `BROKER_EXPORT_DIR` and the exit report's `upload` object is
`mode: "report-only"` with what would have been sent. Outside dev mode a
failed upload also writes the archive there so nothing is lost. The summary
is posted to the room chat before the room is torn down.

### Status

`GET /streaming/api/session/status` for a session summary,
`GET /streaming/api/health` for a bare health check.
