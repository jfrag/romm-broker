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
  emulators/             emulator launchers (pcsx2, retroarch, eden, shadps4, desktop)
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
  (pcsx2: `memcards/`, `sstates/`; retroarch: `states/`, `saves/`; eden:
  `nand/user/save/` `nand/system/save/8000000000000010/`; shadps4:
  `home/1000/savedata/`).
  Activate restores an archive without
  rolling back newer files; exit zips everything modified since launch and
  uploads it to the callback origin. In dev mode nothing is uploaded the
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
| `BROKER_IMPORT_DIR` | `/config/broker-imports` | where uploaded save archives land, ready to pass to activate as `save.archive` |
| `BROKER_SAVE_UPLOAD_PATH` | `/api/webstation/saves` | path appended to the callback base URL when exit POSTs the save archive |
| `BROKER_SAVE_UPLOAD_TIMEOUT` | `30` | seconds allowed for the exit save upload |
| `BROKER_STATE_FILE_MAX_BYTES` | `268435456` | ceiling on one state file over the state-file routes; RomM caps the same transfer |
| `BROKER_STATE_SCREENSHOT_MAX_BYTES` | `16777216` | ceiling on the frame served with a state; RomM caps the same transfer |
| `PCSX2_STATE_SLOT` | `10` | the slot PCSX2 works in; 10 is its own autosave slot, which the per-emulator broker also used |
| `RETROARCH_STATE_SLOT` | `0` | the slot RetroArch works in; 0 is its default, so the player's own load hotkey reaches the same file |
| `BROKER_DEV_MODE` | unset | run from mounted source with uvicorn/vite hot reload; also disables the exit save upload (report-only) |

## Deployment

The container is meant to be served from a subfolder of the parent's origin, so
the parent's player can see pointer events inside the stream iframe. Point a
reverse proxy rule at the container's web port and set `SUBFOLDER` to the same
path it is mounted at. The proxy must pass the prefix through rather than strip
it, and must forward websocket upgrades.

See [docs/reverse-proxy.md](docs/reverse-proxy.md) for the RomM config keys,
recipes for nginx, Caddy and Traefik, why Zoraxy needs one of them behind it,
how to verify a mount, and what running more than one container takes.

## Dev mode

```
git clone https://github.com/romm-streaming/romm-broker.git
cd romm-broker
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

`emulator: "eden"` launches the Eden Switch emulator the same way. Eden has
no save states: `resume_slot` is ignored, and exit performs a graceful
shutdown followed by a save delta dump the game's own save data under the emulated NAND is the
persistence. `EDEN_STOP_WAIT` (default 15 s) controls how long the graceful
stop gets before SIGKILL.

`emulator: "retroarch"` is the general purpose launcher: one class covering
every libretro core, with `rom.platform` picking the core (ngc and wii both
map to `dolphin`, plus snes, n64, dc, saturn, psp, nds, 3ds, arcade and
genesis). Missing cores are downloaded from the libretro buildbot into the
user's own `libretro_directory`, which is where RetroArch also keeps the
matching `.info` files: a core loaded from anywhere else has no core info and
`GET_STATUS` then segfaults RetroArch mid-session. The user's config is never
edited, only layered with `--appendconfig`.

Save states are real, but RetroArch has no "save to slot n" command, so the
broker parks the slot instead: it walks down to the -1 floor and back up to
`RETROARCH_STATE_SLOT` (default 0). That runs once per launch and again only
when a save lands elsewhere, which means the player moved the slot with their
own hotkeys. The first save of a session costs about 3 s, the rest under 1 s.
Loads use `LOAD_STATE_SLOT`, which is already absolute. Tunables:
`RETROARCH_SLOT_STEP_DELAY` (0.1 s between presses) and
`RETROARCH_SLOT_HOME_STEPS` (24 presses to reach the floor).

`emulator: "shadps4"` launches the shadPS4 PlayStation 4 emulator. shadPS4
has no save states either (`resume_slot` is ignored); the game's save data
under `home/1000/savedata/` is the persistence. The binary is picked from
`$HOME/.local/share/shadPS4QtLauncher/versions`: the `Pre-release/` build if
present (it always trumps releases), otherwise the newest semver release
folder (`vX.Y.Z - ... - <date>/`, each holding `Shadps4-sdl.AppImage`).
Override the search with `SHADPS4_BIN` (explicit path), `SHADPS4_VERSIONS_DIR`,
`SHADPS4_BIN_NAME`, or `SHADPS4_DATA_DIR`. The broker drives shadPS4 through
its stdin IPC (`SHADPS4_ENABLE_IPC=true`): RUN/START boot the game headlessly,
and exit sends STOP (the SDL quit event) for a graceful teardown before the
save delta is dumped. shadPS4 has no SIGTERM handler, so SIGTERM is only the
fallback if STOP doesn't finish in `SHADPS4_STOP_WAIT` (default 20 s).

### Launch a game with save data

Add a `save` object: `archive` is a zip restored before launch, `resume_slot`
loads a state once the VM is up. Both are optional and independent. Like the
state routes, `resume_slot` resolves to the emulator's working slot rather than
being honoured literally, so any value loads whatever that slot holds.

PCSX2 empties its working slot at the start of activate, before the archive is
restored. A `.p2s` is named for the disc it was taken from and the serial only
comes off the running disc, so a file left over from an earlier session cannot
be told apart from the current game's; RomM already holds those states, and
clearing them is what stops the last player's save being served as this one's.
Both the restore and the later resume push land afterwards, so neither is
touched. The other emulators name a state after the loaded content and can tell
a stale one apart on sight, so they need no equivalent.

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

### Save and load state mid-session

```
curl -k -X POST https://localhost:3001/streaming/api/session/save-state \
  -H 'X-Broker-Secret: <shared secret>' \
  -H 'Content-Type: application/json' \
  -d '{ "slot": 3 }'
```

`/load-state` takes the same body. Both return
`{"status": "saved"|"loaded"|"failed", "slot": N, ...}` and both are
secret-only: the parent's backend is the caller, not the room UI.

RomM is the library of states, so each emulator works in exactly one slot and
resolves whatever `slot` is asked for to its own. The response echoes the slot
it actually used, which is also the `state_slot` in the status response. The
field is kept because the per-emulator brokers take it and because it is what
the parent needs to read back, not because the broker keeps ten of anything.

`400` means the running emulator has no save states at all (`desktop`, `eden`
and `shadps4` persist through the game's own save data instead), `409` means
there is no active session or the emulator process is gone. Which case applies
is readable ahead of time from `supports_states` in the status response, so the
parent does not need its own per-emulator table.

A `"failed"` status is a real answer, not an error: for PCSX2 it means PINE
never acked, and for either emulator it covers a load of a slot that holds no
state file, or a save whose slot never appeared on disk.

### Move a state file in or out

```
curl -k -X GET https://localhost:3001/streaming/api/session/state-file \
  -H 'X-Broker-Secret: <shared secret>' -o state.bin

curl -k -X PUT "https://localhost:3001/streaming/api/session/state-file?filename=NAME" \
  -H 'X-Broker-Secret: <shared secret>' --data-binary @state.bin
```

This is how RomM becomes the datastore rather than the container: the GET
follows a save so the state can be filed in the library, and the PUT sends any
stored state back so a load or a resume can reach it. The GET reports the name
in `X-State-Filename` and the slot in `X-State-Slot`.

The PUT takes a name back and asks only whether the running emulator could have
written it for the loaded game. The slot in the name is not part of that test:
the library holds states captured under whatever slot was in use at the time,
so the name is restamped into this broker's one working slot and the response
reports the name it landed under. What is checked is identity, the PCSX2 serial
or the RetroArch content basename, so a state for another game or another
directory is still a `400` rather than a stray file in the save tree.

Both are session-scoped and return `409` once the session is gone. After exit
the state has already left in the save archive the exit dumps, so there is
nothing left to pull. `413` on either side means the file is over
`BROKER_STATE_FILE_MAX_BYTES` (256 MiB); RomM caps the same transfer, so
raising one end alone only moves which end refuses.

### Fetch the frame a state was taken at

```
curl -k -X GET https://localhost:3001/streaming/api/session/state-screenshot \
  -H 'X-Broker-Secret: <shared secret>' -o state.png
```

This is what gives a stored state its thumbnail in RomM's resume picker. Only
emulators that write the frame as its own file answer it: RetroArch saves a
`<state file>.png` beside every state, so it serves that. PCSX2 embeds the frame
inside the `.p2s` and returns `404`, which is the caller's cue to read it out of
the state it already fetched rather than an error. Same session scoping as the
state-file routes, with `413` over `BROKER_STATE_SCREENSHOT_MAX_BYTES` (16 MiB).

### Exit

From the room UI's exit button (controller token) or directly:

```
curl -k -X POST 'https://localhost:3001/streaming/api/session/exit?slot=10' \
  -H 'X-Broker-Secret: <shared secret>'
```

Saves state (into the working slot, whatever `slot` says), stops the emulator,
dumps the save delta, and uploads
it to the callback origin as multipart form data:
`POST {base_url}{BROKER_SAVE_UPLOAD_PATH}` with an `archive` file part
(zip, `{session_id}-{timestamp}.zip`) plus `session_id`, `emulator`, and
when the rom carried them `rom_id` / `rom_name` form fields. If activate
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

### Save archive transfer

Activate's `save.archive` is a container path, but the parent is a separate
service holding bytes, so archives move over these endpoints in both
directions.

```
PUT    /streaming/api/session/imports/{name}.zip   body: raw zip
GET    /streaming/api/session/exports
GET    /streaming/api/session/exports/{name}.zip
DELETE /streaming/api/session/exports/{name}.zip
```

All four take `X-Broker-Secret`. The upload returns
`{"status": "stored", "name": ..., "path": ..., "size": ...}`; feed that
`path` back as `save.archive` on activate. Names must be a bare `.zip`
basename, the body must start with the zip magic, and the size ceiling is the
same 256 MB the dump uses.

Pulling covers the two cases the exit push cannot: dev mode, where the upload
is disabled and the archive only ever lands on disk, and a failed upload,
where the archive on disk is the only remaining copy of the save data. Delete
each archive once the parent has stored it.

### Status

`GET /streaming/api/session/status` for a session summary,
`GET /streaming/api/health` for a bare health check.
