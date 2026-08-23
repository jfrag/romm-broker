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
  emulators/             emulator launchers (pcsx2, retroarch, shadps4, desktop)
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
  (pcsx2: `memcards/`, `sstates/`; retroarch: `states/`, `saves/`; shadps4:
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

## Migrating from a per-emulator broker

If you're running one of the per-emulator broker mods
(`pcsx2-romm-integration`, `dolphin-romm-integration`,
`xemu-romm-integration`, `rpcs3-romm-integration`, `eden-romm-integration`),
those are deprecated. This broker, running inside a single
[docker-webstation](https://github.com/linuxserver/docker-webstation)
container, replaces all of them: one container serves every platform instead
of one container per emulator.

In RomM's `config.yml`, each per-emulator container used a bare `platform:`
key. One `protocol: webstation` container replaces any number of those with
a `platforms:` map:

```yaml
# before: one container per platform
streaming:
  containers:
    - platform: ps2
      host: https://192.168.1.51:3001
      broker_host: http://192.168.1.51:8000
      label: PCSX2
      memory_card_sync: true
    - platform: ngc
      host: https://192.168.1.52:3001
      broker_host: http://192.168.1.52:8000
      label: Dolphin

# after: one webstation container serving both
streaming:
  containers:
    - host: https://192.168.1.56:3010
      protocol: webstation
      subfolder: /streaming
      library_path: /romm
      label: Emulation station
      platforms:
        ps2:
          emulator: pcsx2
          label: PCSX2
          memory_card_sync: true
        ngc:
          emulator: dolphin
          label: Dolphin
```

`subfolder` and `library_path` line up with this broker's own `SUBFOLDER` and
`ROM_ROOT` env vars (see Configuration below), so set them to match however
you deploy the container.

Stand up docker-webstation with this broker installed, mount it at the same
ROM library path your old per-emulator containers used (so save/state history
keyed to it doesn't reset), confirm streaming works for each platform, then
remove the old containers. See RomM's `docs/STREAMING_MIGRATION.md` for the
full guide:
https://github.com/rommapp/romm/blob/master/docs/STREAMING_MIGRATION.md

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `SUBFOLDER` | `/streaming/` | URL prefix for the whole app |
| `BROKER_SECRET` | unset | shared secret for the lifecycle endpoints (`X-Broker-Secret` header); **required** — the broker refuses to start without it unless `BROKER_DEV_MODE=true` is also set |
| `BROKER_DEV_MODE` | unset | `true` explicitly opts into running with no `BROKER_SECRET` (local development only; every lifecycle endpoint is unauthenticated) |
| `BROKER_SAVE_FILE_MAX_ENTRIES` | `10000` | ceiling on member count in a save/memory-card archive, independent of the byte-size cap |
| `ROM_ROOT` | `/romm` | activate rejects rom paths outside this root |
| `BROKER_EXPORT_DIR` | `/config/broker-exports` | where exit writes save archives (always in dev mode, otherwise only when the upload fails) |
| `BROKER_IMPORT_DIR` | `/config/broker-imports` | where uploaded save archives land, ready to pass to activate as `save.archive` |
| `BROKER_DISPLAY` | `:0` | X display emulators are launched onto |
| `BROKER_WAYLAND_DISPLAY` | `wayland-0` | Wayland display emulators are launched onto |
| `BROKER_PID_FILE` | `/config/broker-emulator.json` | where the running emulator's pid is recorded so a restarted broker can still kill it |
| `BROKER_SAVE_UPLOAD_PATH` | `/api/webstation/saves` | path appended to the callback base URL when exit POSTs the save archive |
| `BROKER_SAVE_UPLOAD_TIMEOUT` | `30` | seconds allowed for the exit save upload |
| `BROKER_STATE_FILE_MAX_BYTES` | `268435456` | ceiling on one state file over the state-file routes; RomM caps the same transfer |
| `BROKER_STATE_SCREENSHOT_MAX_BYTES` | `16777216` | ceiling on the frame served with a state; RomM caps the same transfer |
| `BROKER_DEV_MODE` | unset | run from mounted source with uvicorn/vite hot reload; also disables the exit save upload (report-only) |

### Emulator settings

Each launcher reads its own variables at import time. Paths default to where
the emulator itself keeps its data inside the container (`HOME` is `/config`,
and `XDG_CONFIG_HOME` / `XDG_DATA_HOME` are honoured where the emulator
honours them), so none of these need setting unless the image layout changes.
Timings are in seconds.

| Env var | Default | Purpose |
| --- | --- | --- |
| `XDOTOOL_BIN` | `xdotool` | the xdotool used to send save/load hotkeys to Dolphin and PPSSPP |
| `XDG_RUNTIME_DIR` | `/config/.XDG` | where PCSX2's PINE socket (`pcsx2.sock`) is looked for |
| `DESKTOP_BIN` | `selkies-desktop` | what the `desktop` type launches |
| `PCSX2_BIN` | `pcsx2-qt` | PCSX2 binary |
| `PCSX2_STATE_SLOT` | `10` | the slot PCSX2 works in; 10 is its own autosave slot, which the per-emulator broker also used |
| `PCSX2_SLOT1_CARD` | `romm-slot1` | name of the Slot 1 folder memory card the broker owns (a directory, no `.ps2` extension) |
| `PCSX2_LOG_PATH` | `/config/pcsx2-qt.log` | where PCSX2's stdout/stderr is captured |
| `SSTATE_DIR` | `/config/.config/PCSX2/sstates` | where PCSX2 writes save states |
| `PINE_WAIT` | `20.0` | how long a save state has to land on disk after the PINE save command |
| `RESUME_LOAD_WAIT` | `90.0` | how long a resume load waits for PCSX2 to report a running game |
| `RESUME_LOAD_SETTLE` | `3.0` | how long the game has to be running before the resume load fires |
| `DUCKSTATION_BIN` | `/opt/duckstation/AppRun` | DuckStation binary |
| `DUCKSTATION_DATA_DIR` | `/config/.local/share/duckstation` | DuckStation's data root (`settings.ini`, `savestates/`, memory cards); `$XDG_CONFIG_HOME/duckstation` when that is set |
| `DUCKSTATION_LOG_PATH` | `/config/duckstation.log` | where DuckStation's stdout/stderr is captured |
| `DUCKSTATION_STOP_WAIT` | `30` | SIGTERM grace before SIGKILL; the graceful shutdown serialises the resume state |
| `DOLPHIN_BIN` | `dolphin-emu` | Dolphin binary |
| `DOLPHIN_USER_DIR` | `/config/.local/share/dolphin-emu` | Dolphin's user directory (`Config/`, `StateSaves/`, `GC/`, `Wii/`) |
| `DOLPHIN_LOG_PATH` | `/config/dolphin.log` | where Dolphin's stdout/stderr is captured |
| `DOLPHIN_STATE_SLOT` | `1` | the slot Dolphin works in (Shift+F<n> saves it, F<n> loads it) |
| `DOLPHIN_STATE_WAIT` | `20.0` | how long a save state has to land on disk after the save hotkey |
| `DOLPHIN_RESUME_LOAD_WAIT` | `90.0` | how long a resume load waits for the render window |
| `DOLPHIN_RESUME_LOAD_SETTLE` | `5.0` | how long the window has to be up before the load hotkey is sent |
| `DOLPHIN_VIDEO_BACKEND` | `OGL` | video backend pinned at launch; OGL over Vulkan because RADV on the integrated AMD parts has been the less reliable one |
| `CEMU_BIN` | `Cemu` | Cemu binary |
| `CEMU_CONFIG_DIR` | `/config/.config/Cemu` | Cemu's config directory (`settings.xml`, `controllerProfiles/`) |
| `CEMU_DATA_DIR` | `/config/.local/share/Cemu` | Cemu's data directory |
| `CEMU_MLC_DIR` | `<CEMU_DATA_DIR>/mlc01` | the mlc01 tree saves are dumped from; only passed to Cemu as `-m` when set explicitly |
| `CEMU_LOG_PATH` | `/config/cemu.log` | where Cemu's stdout/stderr is captured |
| `CEMU_PAD_NAME` | `Microsoft X-Box 360 pad` | the selkies virtual pad's name as the kernel xpad driver presents it, used to derive the SDL GUIDs the controller profile binds |
| `CEMU_PAD_UUIDS` | unset | comma separated `<index>_<guid>` list to bind instead of the derived ones |
| `CEMU_STOP_WAIT` | `5` | SIGTERM grace before SIGKILL (Cemu has no SIGTERM handler, so this only covers process-group teardown) |
| `AZAHAR_BIN` | `/opt/azahar/AppRun` | Azahar binary |
| `AZAHAR_USER_DIR` | `/config/.local/share/azahar-emu` | Azahar's user data directory (`sdmc/`, `nand/`) |
| `AZAHAR_CONFIG_DIR` | `/config/.config/azahar-emu` | Azahar's config directory (`qt-config.ini`) |
| `AZAHAR_LOG_PATH` | `/config/azahar.log` | where Azahar's stdout/stderr is captured |
| `AZAHAR_STOP_WAIT` | `5` | SIGTERM grace before SIGKILL (no SIGTERM handler, process-group teardown only) |
| `EDEN_BIN` | `eden` | Eden binary |
| `EDEN_CONFIG_DIR` | `/config/.config/eden` | Eden's config directory (`qt-config.ini`) |
| `EDEN_DATA_DIR` | `/config/.local/share/eden` | Eden's data directory (`nand/`, `sdmc/`) |
| `EDEN_LOG_PATH` | `/config/eden.log` | where Eden's stdout/stderr is captured |
| `EDEN_STOP_WAIT` | `15` | SIGTERM grace before SIGKILL; a running game takes longer than the base 5 s to tear down |
| `SHADPS4_BIN` | unset | explicit shadPS4 binary; skips the version search |
| `SHADPS4_VERSIONS_DIR` | `/config/.local/share/shadPS4QtLauncher/versions` | where the Qt launcher downloads builds, one folder per release; `Pre-release/` trumps the newest semver folder |
| `SHADPS4_BIN_NAME` | `Shadps4-sdl.AppImage` | the binary looked for inside each version folder |
| `SHADPS4_DATA_DIR` | `/config/.local/share/shadPS4` | shadPS4's data directory (`home/1000/savedata/` is the save subtree) |
| `SHADPS4_LOG_PATH` | `/config/shadps4.log` | where shadPS4's stdout/stderr is captured |
| `SHADPS4_STOP_WAIT` | `20` | how long the IPC STOP gets to finish before SIGTERM |
| `RETROARCH_BIN` | `retroarch` | RetroArch binary |
| `RETROARCH_CONFIG_DIR` | `/config/.config/retroarch` | where the user's `retroarch.cfg` lives |
| `RETROARCH_CORES_DIR` | `libretro_directory` from `retroarch.cfg`, else `/config/.local/share/RetroArch/cores` | where cores are loaded from and downloaded into |
| `RETROARCH_SYSTEM_DIR` | `system_directory` from `retroarch.cfg`, else `/config/.local/share/RetroArch/system` | where cores look for BIOS files and firmware |
| `RETROARCH_CORES_BASE_URL` | `https://buildbot.libretro.com/nightly/linux/x86_64/latest` | where missing cores are downloaded from |
| `RETROARCH_CORE_URL_<CORE>` | unset | pins one core's download URL without editing `retroarch_platforms.json` |
| `GITHUB_API_BASE` | `https://api.github.com` | API base for cores whose platform entry names a GitHub release source |
| `GITHUB_TOKEN` | unset | bearer token for those GitHub release lookups |
| `RETROARCH_CORE_DOWNLOAD_TIMEOUT` | `180` | timeout on a core download |
| `RETROARCH_DATA_DIR` | `/config/.retroarch` | broker-managed `states/`, `saves/` and the `broker.cfg` layered on with `--appendconfig` |
| `RETROARCH_LOG_PATH` | `/config/retroarch.log` | where RetroArch's stdout/stderr is captured |
| `RETROARCH_JOYPAD_DRIVER` | `linuxraw` | joypad driver forced at launch; the udev driver sees the selkies pads as one device plugged eight times. Set empty to leave the user's own driver alone |
| `RETROARCH_STATE_SLOT` | `0` | the slot RetroArch works in; 0 is its default, so the player's own load hotkey reaches the same file |
| `RETROARCH_SLOT_STEP_DELAY` | `0.1` | pause between slot presses while parking the slot |
| `RETROARCH_SLOT_HOME_STEPS` | `24` | slot-down presses to reach the -1 floor from anywhere the player could have cycled to |
| `RETROARCH_STATE_CONFIRM_WAIT` | `10.0` | how long a save state has to land on disk after `SAVE_STATE_SLOT` |
| `RETROARCH_LOAD_ACK_WAIT` | `10.0` | how long `LOAD_STATE_SLOT` has to be acknowledged |
| `RETROARCH_RESUME_WAIT` | `90.0` | how long a resume load waits for the core to report a running game |
| `RETROARCH_RESUME_SETTLE` | `3.0` | how long the game has to be running before the resume load fires |
| `RETROARCH_SAVE_FILES_WAIT` | `10.0` | how long `SAVE_FILES` (flush SRAM to disk) has to answer at exit |
| `RETROARCH_QUIT_WAIT` | `10.0` | how long `QUIT` gets to finish before SIGTERM |
| `RETROARCH_QUIT_CONFIRM_GAP` | `0.1` | gap before the second `QUIT`, for configs that ask for confirmation |
| `RETROARCH_DISC_TRAY_SETTLE` | `1.5` | pause after opening the tray before changing the disc index; an index change during the open is silently dropped |
| `RETROARCH_DISC_STEP_DELAY` | `0.1` | pause between disc index presses |
| `RETROARCH_DISC_SWAP_WAIT` | `90.0` | how long a swap waits for the core to report a running game |
| `RETROARCH_STOP_WAIT` | `15` | SIGTERM grace before SIGKILL once `QUIT` has been tried |
| `RPCS3_BIN` | `/opt/rpcs3/AppRun` | RPCS3 binary |
| `RPCS3_DATA_DIR` | `/config/.config/rpcs3` | RPCS3's data root (`config.yml`, `dev_hdd0/`) |
| `RPCS3_LOG_PATH` | `/config/rpcs3.log` | where RPCS3's stdout/stderr is captured |
| `RPCS3_INSTALL_TIMEOUT` | `1800` | how long a PKG install may run; decryption of a multi-GB package takes minutes |
| `RPCS3_STOP_WAIT` | `2` | SIGTERM grace before SIGKILL (no SIGTERM handler, covers the AppImage wrapper teardown) |
| `XEMU_BIN` | `/opt/xemu/AppRun` | xemu binary |
| `XEMU_TOML` | `/config/.local/share/xemu/xemu/xemu.toml` | xemu's config, read for the HDD image path and pinned for the renderer |
| `XEMU_HDD_IMAGE` | `/config/xemu/xbox_hdd.qcow2` | HDD image used only when `xemu.toml` has no usable `hdd_path` |
| `XEMU_RENDERER` | `OPENGL` | renderer pinned in `xemu.toml` before each launch; `VULKAN` where the driver is known good, `KEEP` to leave the file alone |
| `XEMU_SOFTWARE_GL` | unset | truthy renders xemu on the CPU (`LIBGL_ALWAYS_SOFTWARE` for xemu only); slow, for hosts where both GPU paths abort |
| `XEMU_LOG_PATH` | `/config/xemu.log` | where xemu's stdout/stderr is captured |
| `XEMU_STOP_WAIT` | `15` | SIGTERM grace before SIGKILL; QEMU's clean shutdown flushes the HDD image the save extraction reads |
| `PPSSPP_BIN` | `PPSSPPQt` | PPSSPP binary |
| `PPSSPP_CONFIG_DIR` | `/config/.config/ppsspp` | PPSSPP's config root; `PSP/` under it holds `SYSTEM/ppsspp.ini`, `SAVEDATA/` and `PPSSPP_STATE/` |
| `PPSSPP_LOG_PATH` | `/config/ppsspp.log` | where PPSSPP's stdout/stderr is captured |
| `PPSSPP_STATE_SLOT` | `1` | the slot PPSSPP works in |
| `PPSSPP_STATE_WAIT` | `20.0` | how long a save state has to land on disk after the save hotkey |
| `PPSSPP_RESUME_LOAD_WAIT` | `90.0` | how long a resume load waits for the game window |
| `PPSSPP_RESUME_LOAD_SETTLE` | `5.0` | how long the window has to be up before the load hotkey is sent |
| `XENIA_BIN` | `/opt/xenia/AppRun` | Xenia Edge binary |
| `XENIA_DATA_DIR` | `/config/xenia` | Xenia's `--storage_root`: config, cache, the signed-in profile and the `content/` tree the save archive ships |
| `XENIA_LOG_PATH` | `/config/xenia.log` | where Xenia's stdout/stderr is captured |
| `FLYCAST_BIN` | `/opt/flycast/AppRun` | Flycast binary |
| `FLYCAST_DATA_DIR` | `/config/.local/share/flycast` | Flycast's data root (VMU saves, save state, BIOS); `$XDG_DATA_HOME/flycast` when that is set |
| `FLYCAST_LOG_PATH` | `/config/flycast.log` | where Flycast's stdout/stderr is captured |
| `FLYCAST_STOP_WAIT` | `20` | how long an Alt+F4 close request gets to reach `dc_exit()` (and write the resume state) before SIGTERM |

## Deployment

The container is meant to be served from a subfolder of the parent's origin, so
the parent's player can see pointer events inside the stream iframe. Point a
reverse proxy rule at the container's web port and set `SUBFOLDER` to the same
path it is mounted at. The proxy must pass the prefix through rather than strip
it, and must forward websocket upgrades.

See [docs/reverse-proxy.md](docs/reverse-proxy.md) for the RomM config keys,
recipes for nginx, Caddy and Traefik, why Zoraxy needs one of them behind it,
how to verify a mount, and what running more than one container takes.

## Releases

Versions are semver and cut by release-please off `master`. Conventional commit
subjects drive the bump: `feat:` takes the minor, `fix:` the patch. The action
opens a release pull request that carries the version into `pyproject.toml` and
writes `CHANGELOG.md`; merging it tags `vX.Y.Z` and publishes the release.

The image installs a release by source tarball, so nothing has to be attached to
it:

```
https://github.com/romm-streaming/romm-broker/archive/refs/tags/vX.Y.Z.tar.gz
```

While the version is below 1.0.0 a breaking change still takes the minor. Going
to 1.0.0 is a deliberate call, made with a `Release-As: 1.0.0` commit footer.

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

`emulator: "xenia"` launches the Xenia Edge Xbox 360 emulator. Xenia has no
save states (`resume_slot` is ignored) and no control interface of any kind
(no IPC, socket or stdin protocol), so the game's own save data is the
persistence and exit is a SIGTERM, which Xenia takes as a plain kill. That is
safe because guest save writes go straight through to host files, so nothing
is lost in a buffer. The save archive is the whole `content/` tree under
`XENIA_DATA_DIR`: Xbox 360 save paths embed the profile XUID and the profile
store lives in that same tree, so the two travel together and line up on a
restore into a fresh container. `rom.path` may be an XISO (`.iso`), a
bare `.xex`, an extracted dump (a folder holding `default.xex`), or an XBLA /
Games on Demand title folder: the broker walks the console's own layout
(`[Content/<XUID>/]<TITLE_ID>/000D0000|00007000/<package>`) and boots the STFS
package it finds there, checked by magic so DLC, title updates and the `.data`
payload beside a GoD package are passed over.

`emulator: "flycast"` launches the Flycast Dreamcast emulator; `emulator:
"retroarch"` with `rom.platform: "dc"` covers the same platform through its
own core for anyone who would rather manage Dreamcast alongside their other
RetroArch games. Standalone Flycast has neither a control socket nor a
SIGTERM handler, so like DuckStation there is no mid-session save or load:
resume is boot-time only, driven by transient `-config` overrides
(`Dreamcast.AutoLoadState`/`AutoSaveState`, both under the literal section
`config`, not `Dreamcast`) rather than an edited ini. The only graceful exit
path is an Alt+F4 keypress into the focused Flycast window (the container's
compositor treats that as a close request), which the broker sends through
xdotool; that is also the only point `Dreamcast.AutoSaveState` gets written,
so exit asks for the close and only falls back to SIGTERM if it doesn't land
within `FLYCAST_STOP_WAIT` (default 20 s). The save state itself is one
deterministic file, `<rom-basename>.state` in `FLYCAST_DATA_DIR`, so unlike
DuckStation the broker never has to guess which file in the directory is the
current game's at launch time; a restore still clears every leftover
`*.state` first, since the name is only unambiguous once the rom is known and
that happens after the clear, not before it. `rom.path` may be a disc image
(`.chd`, `.gdi`, `.cdi`, `.cue`) or a homebrew `.elf`; VMU saves and the save
state both live loose in `FLYCAST_DATA_DIR`, which ships as a single save
subtree.

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

`400` means the running emulator has no save states at all (`desktop` and
`shadps4` persist through the game's own save data instead), `409` means
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

The PUT needs a live session and returns `409` without one, since a state is
only pushed in so a running emulator can reach it. The GET outlives the session:
exit captures a state on its way out, and RomM can only come back for it once
the teardown has answered, so refusing there was what left every exit state
stranded in the container. The broker holds the exited session's state until the
next activate clears the working slot, and answers `409` before the first
session and `404` once the slot is empty. `413` on either side means the file is
over `BROKER_STATE_FILE_MAX_BYTES` (256 MiB); RomM caps the same transfer, so
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
the state it already fetched rather than an error. It stays readable after exit
on the same terms as the state itself, with `413` over
`BROKER_STATE_SCREENSHOT_MAX_BYTES` (16 MiB).

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

`GET /streaming/api/session/status` for a session summary (takes
`X-Broker-Secret` like the other RomM-facing routes; it returns usernames and
ROM details, not just liveness),
`GET /streaming/api/health` for a bare health check (no secret, no user data).
