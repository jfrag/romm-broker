"""RPCS3 (PlayStation 3) launcher: config.yml/ipc.yml patching, PKG install
hook, PINE-driven boot verification, and save states via the emulator's own
exit-on-save hotkey.

Boot formats: decrypted .iso images and disc folder rips
(PS3_GAME/USRDIR/EBOOT.BIN) boot directly. A .pkg is an installer, not an
image: the first activation installs it via `--headless --installpkg` into
`dev_hdd0/game/<TITLEID>/` and later activations boot the installed
EBOOT.BIN. .rap/.edat licenses are plain files copied into
`dev_hdd0/home/00000001/exdata/`.

Save states: unlike PCSX2, RPCS3 has no live save/load. Pressing the
save-state hotkey always terminates the process once the state is written,
and loading one means booting RPCS3 pointed straight at the .SAVESTAT file
(auto-detected by magic bytes, not extension) instead of a normal boot
target, functionally the same as booting a ROM. So the broker follows
DuckStation's model rather than PCSX2's: no live supports_states, a resume
loads by choosing what to boot before the process starts, and a save
happens by triggering the hotkey and waiting for the process to exit rather
than by keeping it running. States are per-title, at
DATA_DIR/savestates/<title_id>/, which is a sibling of dev_hdd0 rather than
something under it; a symlink into dev_hdd0/savestates is what lets the
existing save archive (keyed to dev_hdd0-relative paths) carry it without
moving save_root out from under the subtrees already archived.

PINE: RPCS3's IPC is PINE-compatible but only implements the protocol's
generic opcodes (memory access, version/title/status queries) - there is no
save/load-state opcode the way PCSX2's variant adds one. The broker uses it
for nothing but boot verification (MsgStatus) and, for boots (a bare .iso)
whose title id the on-disk layout cannot supply, a title id lookup (MsgID)
once the game is confirmed running.

RPCS3 installs no signal handler, so a plain kill (no state requested, or
the hotkey/wait having failed) is a safe stop: whatever the game already
wrote to its own save data is on disk the moment it's written. cellSaveData
saves under `dev_hdd0/home/00000001/savedata/`, cellGameData saves under
`dev_hdd0/game/`.

The emulator is expected to be brought to a working state in desktop mode
(firmware, GPU settings, controllers) before automated launching.
"""

import hashlib
import logging
import os
import shutil
import socket as _socket
import struct
import subprocess
import time
import zipfile
from pathlib import Path
from threading import Thread

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))


def _default_data_dir() -> str:
    """RPCS3's Linux data root: $XDG_CONFIG_HOME/rpcs3 when set, otherwise
    ~/.config/rpcs3. Config, dev_flash and dev_hdd0 all live under it."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "rpcs3")
    return os.path.join(os.environ.get("HOME", "/config"), ".config/rpcs3")


DATA_DIR = Path(os.environ.get("RPCS3_DATA_DIR", _default_data_dir()))
CONFIG_PATH = DATA_DIR / "config.yml"
IPC_PATH = DATA_DIR / "ipc.yml"
DEV_HDD0 = DATA_DIR / "dev_hdd0"
USER_HOME = DEV_HDD0 / "home" / "00000001"
EXDATA_DIR = USER_HOME / "exdata"
GAME_DIR = DEV_HDD0 / "game"
# RPCS3 always writes states here, never under dev_hdd0; see _ensure_sstate_link.
SSTATE_ROOT = DATA_DIR / "savestates"
_SSTATE_LINK = DEV_HDD0 / "savestates"
RPCS3_LOG_PATH = Path(os.environ.get("RPCS3_LOG_PATH", "/config/rpcs3.log"))
# PKG decryption and archive extraction can both run minutes for multi-GB
# dumps, so this timeout is shared between them.
INSTALL_TIMEOUT = float(os.environ.get("RPCS3_INSTALL_TIMEOUT", "1800"))

# Boot formats, best first: decrypted ISO and PKG installer beat a bare
# EBOOT so a folder holding both the rip and its installer picks the image.
# Archives sort last since booting one costs an extraction first.
ROM_EXTENSIONS = (".iso", ".pkg", ".bin", ".self", ".elf", ".7z", ".zip", ".rar")
# EBOOT.BIN sits three levels down in a disc rip (PS3_GAME/USRDIR/EBOOT.BIN).
_ROM_SEARCH_GLOBS = ("*", "*/*", "*/*/*")
# Only executables named EBOOT.* are bootable; other .bin/.self files in a
# rip (licenses, sdata) are not.
_EBOOT_EXTS = (".bin", ".self", ".elf")
_ARCHIVE_EXTS = (".7z", ".zip", ".rar")


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


# Decrypted PS3 dumps run 2-25GB. With the cache enabled, an archive is
# extracted once into CACHE_DIR and reused on relaunch; with it disabled
# (the default), the extraction is removed again once the game stops, so
# an archived title never leaves a permanent decompressed copy behind.
CACHE_DIR = Path(os.environ.get("RPCS3_CACHE_DIR", str(DATA_DIR / "extracted")))
# Off by default: local disk headroom is limited on a typical host, and an
# unattended cache would otherwise grow until an operator notices. 30GB
# comfortably fits one large title's decompressed size for anyone who opts
# in via RPCS3_CACHE_ENABLED.
CACHE_ENABLED = _truthy(os.environ.get("RPCS3_CACHE_ENABLED", "false"))
CACHE_MAX_GB = float(os.environ.get("RPCS3_CACHE_MAX_GB", "30"))
_LAST_ACCESSED_MARKER = ".last_accessed"

# config.yml values forced before every launch. RPCS3 fills missing keys
# with defaults, so a partial file is a valid config. Keyed ("section", key);
# section "" is a flat top-level key, for ipc.yml which has no sections.
_CONFIG_PATCHES: dict[tuple[str, str], str] = {
    ("Miscellaneous", "Automatically start games after boot"): "true",
    # Game quit (or XMB exit) ends the process, so alive() tracks the game.
    ("Miscellaneous", "Exit RPCS3 when process finishes"): "true",
    # The labwc session gives no focus guarantees; a focus-loss pause would
    # freeze the game invisibly.
    ("Miscellaneous", "Pause emulation on RPCS3 focus loss"): "false",
}
_IPC_PATCHES: dict[tuple[str, str], str] = {
    # Off by default; the broker only ever reads status/title over it, so the
    # port is left at whatever it already is (Linux uses the fixed
    # $XDG_RUNTIME_DIR/rpcs3.sock path below regardless of the configured port).
    ("", "IPC Server enabled"): "true",
}

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")
PINE_SOCKET = Path(XDG_RUNTIME_DIR) / "rpcs3.sock"

# RPCS3's generic PINE opcodes (rpcs3/3rdparty/pine/pine_server.h). No
# save/load-state opcode exists in this set, unlike PCSX2's PINE variant.
_PINE_MSG_ID = 0x0C
_PINE_MSG_STATUS = 0x0F

BOOT_WAIT = float(os.environ.get("RPCS3_BOOT_WAIT", "90.0"))
# A full state write (compressed PS3 RAM + VRAM) is a heavier write than the
# other cores' states, so this is generous.
STATE_WAIT = float(os.environ.get("RPCS3_STATE_WAIT", "60.0"))

_XDOTOOL = os.environ.get("XDOTOOL_BIN", "xdotool")
# StartupWMClass in rpcs3/rpcs3.desktop. --no-gui suppresses the Qt main
# window entirely, so the render window is the only one carrying this class
# once a game is running - no title filtering needed the way PPSSPP's shared
# menu/game class needs.
_WINDOW_CLASS = "rpcs3"
# Default binding for gw_savestate_1 (rpcs3qt/shortcut_settings.cpp). RPCS3
# has four such shortcuts, but every one just writes a new auto-numbered
# state (rpcs3/Emu/savestate_utils.cpp), so slot 1 is as good as any other.
# Configurable because it's a GUI-rebindable shortcut, unlike the rest of
# this module's constants.
_SAVE_STATE_KEY = os.environ.get("RPCS3_SAVE_STATE_KEY", "ctrl+alt+1")


def _rpcs3_bin() -> str:
    return os.environ.get("RPCS3_BIN", "/opt/rpcs3/AppRun")


def _launch_env() -> dict[str, str]:
    env = base_launch_env()
    # The AppImage's desktop entry pins xcb; the Qt wayland platform is not
    # bundled.
    env["QT_QPA_PLATFORM"] = "xcb"
    return env


def _pick_rom_file(candidates, base: Path) -> Path | None:
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
            continue
        if ext in _EBOOT_EXTS and not p.name.upper().startswith("EBOOT"):
            continue
        try:
            if not p.is_file():
                continue
            real = p.resolve()
            rel = p.relative_to(base)
        except (OSError, ValueError):
            continue
        if not real.is_relative_to(ROM_ROOT):
            continue
        ranked.append((ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real))
    if not ranked:
        return None
    return min(ranked)[3]


def _patch_yaml_file(path: Path, patches: dict[tuple[str, str], str]) -> None:
    """Force ("section", "key") -> value into a two-level YAML file
    (unindented "Section:" headers over 2-space "key: value" lines) before
    every launch, or a flat "key: value" when section is "". RPCS3 fills
    missing keys with defaults, so a partial file is a valid config."""
    try:
        if not path.exists():
            log.info("%s not found, seeding one", path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        lines = path.read_text().splitlines()
        section = ""
        applied: set[tuple[str, str]] = set()
        new_lines: list[str] = []
        for line in lines:
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                section = line.strip()[:-1]
                new_lines.append(line)
                continue
            stripped = line.strip()
            matched = False
            for (sec, key), val in patches.items():
                if section == sec and stripped.startswith(f"{key}:"):
                    prefix = "  " if sec else ""
                    new_lines.append(f"{prefix}{key}: {val}")
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        missing = [(s, k, v) for (s, k), v in patches.items() if (s, k) not in applied]
        if missing:
            present = {
                ln.strip()[:-1]
                for ln in new_lines
                if ln and not ln[0].isspace() and ln.rstrip().endswith(":")
            }
            for sec, key, val in missing:
                if not sec:
                    new_lines.append(f"{key}: {val}")
                elif sec in present:
                    out: list[str] = []
                    inserted = False
                    for ln in new_lines:
                        out.append(ln)
                        if not inserted and ln.strip() == f"{sec}:":
                            out.append(f"  {key}: {val}")
                            inserted = True
                    new_lines = out
                else:
                    new_lines.extend([f"{sec}:", f"  {key}: {val}"])
                    present.add(sec)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(path)
    except Exception:
        log.exception("%s patch failed, broker settings NOT applied", path.name)


def _patch_config() -> None:
    _patch_yaml_file(CONFIG_PATH, _CONFIG_PATCHES)


def _patch_ipc() -> None:
    _patch_yaml_file(IPC_PATH, _IPC_PATCHES)


def _ensure_sstate_link() -> None:
    """Make dev_hdd0/savestates resolve to DATA_DIR/savestates.

    RPCS3 always writes states to DATA_DIR/savestates, a sibling of
    dev_hdd0 rather than something under it, but save_root has to stay
    dev_hdd0 so the archive subtrees already in use (home/00000001/savedata,
    game/) keep resolving as they always have. A symlink is what lets
    "savestates" join save_subtrees without moving save_root."""
    try:
        SSTATE_ROOT.mkdir(parents=True, exist_ok=True)
        DEV_HDD0.mkdir(parents=True, exist_ok=True)
        if _SSTATE_LINK.is_symlink():
            if _SSTATE_LINK.resolve() != SSTATE_ROOT.resolve():
                _SSTATE_LINK.unlink()
                _SSTATE_LINK.symlink_to(SSTATE_ROOT, target_is_directory=True)
        elif not _SSTATE_LINK.exists():
            _SSTATE_LINK.symlink_to(SSTATE_ROOT, target_is_directory=True)
        # Anything else is a real directory already sitting there; leave it
        # alone rather than deleting whatever put it there.
    except OSError as exc:
        log.warning("could not link savestates dir: %s", exc)


def _run_headless(args: list[str], what: str) -> None:
    """Run a one-shot `rpcs3 --headless` operation (installer CLI); the
    process exits when the operation completes. Exit code is always 0, so
    callers verify success by checking the expected files afterwards."""
    cmd = [_rpcs3_bin(), "--headless", *args]
    log.info("rpcs3 %s: %s", what, " ".join(cmd))
    try:
        log_fh = open(RPCS3_LOG_PATH, "ab", buffering=0)
        log_fh.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {what} ({' '.join(cmd)}) ===\n".encode()
        )
    except OSError:
        log_fh = None
    try:
        subprocess.run(
            cmd,
            env=_launch_env(),
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            timeout=INSTALL_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"rpcs3 {what} did not finish within {INSTALL_TIMEOUT:.0f}s")
    finally:
        if log_fh:
            log_fh.close()


def _gamedata_dirs() -> list[Path]:
    """cellGameData save dirs under game/: everything except installed
    titles (which have a bootable EBOOT.BIN) and RPCS3's ＄locks dir."""
    dirs = []
    if GAME_DIR.is_dir():
        for d in sorted(GAME_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith(("$", "＄")):
                continue
            if (d / "USRDIR" / "EBOOT.BIN").is_file():
                continue
            dirs.append(d)
    return dirs


def _sfo_title_id(sfo: Path) -> str | None:
    """TITLE_ID string from a PARAM.SFO (key/data table pairs indexed from
    a fixed-size header)."""
    try:
        data = sfo.read_bytes()
    except OSError:
        return None
    if len(data) < 0x14 or data[:4] != b"\x00PSF":
        return None
    key_start = int.from_bytes(data[0x08:0x0C], "little")
    data_start = int.from_bytes(data[0x0C:0x10], "little")
    entries = int.from_bytes(data[0x10:0x14], "little")
    for i in range(entries):
        off = 0x14 + i * 16
        entry = data[off : off + 16]
        if len(entry) < 16:
            return None
        key_off = int.from_bytes(entry[0:2], "little")
        data_len = int.from_bytes(entry[4:8], "little")
        data_off = int.from_bytes(entry[12:16], "little")
        key = data[key_start + key_off : key_start + key_off + 16].split(b"\0", 1)[0]
        if key == b"TITLE_ID":
            value = data[data_start + data_off : data_start + data_off + data_len]
            return value.split(b"\0", 1)[0].decode("ascii", "replace") or None
    return None


def _archive_dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.name == _LAST_ACCESSED_MARKER:
            continue
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _cache_size_bytes() -> int:
    if not CACHE_DIR.is_dir():
        return 0
    return sum(_archive_dir_size(d) for d in CACHE_DIR.iterdir() if d.is_dir())


def _touch_last_accessed(game_dir: Path) -> None:
    try:
        (game_dir / _LAST_ACCESSED_MARKER).write_text(str(time.time()))
    except OSError as exc:
        log.warning("rpcs3 cache: could not update last-accessed marker for %s: %s", game_dir, exc)


def _evict_lru(needed_bytes: int, keep: str) -> None:
    """Evict least-recently-used extracted games until needed_bytes fits
    within CACHE_MAX_GB. keep is the cache key currently being (re-)extracted,
    so a stale entry for it already removed by the caller is never chosen."""
    if not CACHE_ENABLED or not CACHE_DIR.is_dir():
        return
    max_bytes = int(CACHE_MAX_GB * 1024**3)
    current = _cache_size_bytes()
    while current + needed_bytes > max_bytes:
        candidates = []
        for game_dir in CACHE_DIR.iterdir():
            if not game_dir.is_dir() or game_dir.name == keep:
                continue
            marker = game_dir / _LAST_ACCESSED_MARKER
            try:
                mtime = marker.stat().st_mtime if marker.exists() else 0.0
            except OSError:
                mtime = 0.0
            candidates.append((mtime, game_dir))
        if not candidates:
            log.warning(
                "rpcs3 cache: nothing left to evict, proceeding over the %.0f GB cap", CACHE_MAX_GB
            )
            return
        candidates.sort(key=lambda c: c[0])
        victim = candidates[0][1]
        victim_size = _archive_dir_size(victim)
        log.info("rpcs3 cache: evicting %s (least recently used)", victim.name)
        try:
            shutil.rmtree(victim)
        except OSError as exc:
            log.warning("rpcs3 cache: could not evict %s: %s", victim, exc)
            return
        current -= victim_size


def _is_safe_boot_candidate(candidate: Path, root_real: Path) -> bool:
    """False for anything but a regular file resolving inside root_real, so a
    symlink planted by the archive cannot point rpcs3 at a path elsewhere on
    the host."""
    try:
        return candidate.is_file() and candidate.resolve().is_relative_to(root_real)
    except OSError:
        return False


def _archive_boot_target(root: Path) -> Path | None:
    """Best boot target inside an extracted archive: an EBOOT.BIN anywhere
    (disc rip or PKG-installed layout), or failing that a bare decrypted
    .iso an archive may hold instead of an unpacked JB folder."""
    root_real = root.resolve()
    for eboot in root.rglob("EBOOT.BIN"):
        if _is_safe_boot_candidate(eboot, root_real):
            return eboot
    for iso in root.rglob("*.iso"):
        if _is_safe_boot_candidate(iso, root_real):
            return iso
    return None


def _run_extractor(cmd: list[str], what: str) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{what} failed to run: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{what} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _reject_unsafe_members(dest: Path, members: list[str]) -> None:
    """A `../` (or absolute) member path can escape dest on extraction (Zip
    Slip); reject any member whose resolved path would land outside dest
    before anything is written."""
    dest_real = dest.resolve()
    for member in members:
        target = (dest / member).resolve()
        if target != dest_real and dest_real not in target.parents:
            raise RuntimeError(f"archive member escapes extraction dir: {member}")


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """zf.extractall() writes a member's path verbatim, so a `../` name in
    the archive (Zip Slip) can escape dest; reject any member that would."""
    _reject_unsafe_members(dest, zf.namelist())
    zf.extractall(dest)


def _rar_member_paths(archive: Path) -> list[str]:
    """Bare member paths from `unrar lb`: one path per line, no header or
    column formatting to parse around."""
    listing = _run_extractor(["unrar", "lb", "-y", str(archive)], f"unrar list ({archive.name})")
    return [line for line in listing.splitlines() if line]


def _7z_member_paths(archive: Path) -> list[str]:
    """Member paths from `7z l -slt`, the only 7z listing mode that gives a
    full untruncated path per entry. Everything before the `----------`
    separator describes the archive itself, not its contents."""
    listing = _run_extractor(["7z", "l", "-slt", str(archive)], f"7z list ({archive.name})")
    _, _, body = listing.partition("----------\n")
    return [line[len("Path = ") :] for line in body.splitlines() if line.startswith("Path = ")]


def _extract_archive(archive: Path, dest: Path) -> None:
    ext = archive.suffix.lower()
    log.info("rpcs3: extracting %s (%s)", archive.name, ext)
    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive) as zf:
                _safe_extract_zip(zf, dest)
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError(f"zip extraction of {archive.name} failed: {exc}") from exc
    elif ext == ".rar":
        _reject_unsafe_members(dest, _rar_member_paths(archive))
        _run_extractor(["unrar", "x", "-y", str(archive), f"{dest}/"], f"unrar ({archive.name})")
    else:
        # .7z, plus a fallback attempt for any other archive format 7z can
        # identify (RAR5, tar-in-7z JB dumps, etc).
        _reject_unsafe_members(dest, _7z_member_paths(archive))
        _run_extractor(["7z", "x", "-y", str(archive), f"-o{dest}"], f"7z ({archive.name})")


def _cache_key(archive: Path) -> str:
    """Cache dir name for archive: its stem plus a short hash of the file's
    own name, size, and mtime. A bare stem collides two archives that share
    a name but differ in extension, and survives a same-named re-upload
    with different content, either of which would otherwise serve up
    whatever is sitting in the old cache dir as if it were the new ROM."""
    try:
        st = archive.stat()
        fingerprint = f"{archive.name}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        fingerprint = archive.name
    digest = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    return f"{archive.stem}-{digest}"


def _extract_and_cache(archive: Path) -> Path:
    """Extract archive into CACHE_DIR, reusing a prior extraction keyed by
    _cache_key when one already holds a bootable target. Raises on failure
    so launch() aborts before ever starting rpcs3 on nothing bootable."""
    key = _cache_key(archive)
    game_dir = CACHE_DIR / key

    if game_dir.is_dir():
        boot = _archive_boot_target(game_dir)
        if boot is not None:
            log.info("rpcs3 cache hit: %s (boot target: %s)", archive.name, boot.name)
            _touch_last_accessed(game_dir)
            return boot
        log.warning("rpcs3 cache: %s has no boot target, re-extracting", archive.name)
        shutil.rmtree(game_dir, ignore_errors=True)

    try:
        needed = int(archive.stat().st_size * 1.1)
    except OSError:
        needed = 0
    _evict_lru(needed, key)

    game_dir.mkdir(parents=True, exist_ok=True)
    try:
        _extract_archive(archive, game_dir)
    except RuntimeError:
        shutil.rmtree(game_dir, ignore_errors=True)
        raise

    boot = _archive_boot_target(game_dir)
    if boot is None:
        shutil.rmtree(game_dir, ignore_errors=True)
        raise RuntimeError(f"{archive.name} extracted but held no EBOOT.BIN or decrypted .iso")
    _touch_last_accessed(game_dir)
    log.info("rpcs3: extracted %s, booting %s", archive.name, boot)
    return boot


def _pkg_title_id(pkg: Path) -> str | None:
    """Title ID from the PKG header: the content id at offset 0x30 embeds it
    as chars 7-15 (`UP0001-BLUS30443_00-...` -> BLUS30443)."""
    try:
        with open(pkg, "rb") as f:
            header = f.read(0x60)
    except OSError:
        return None
    if len(header) < 0x60 or header[:4] != b"\x7fPKG":
        return None
    content_id = header[0x30:0x60].split(b"\0", 1)[0].decode("ascii", "replace")
    if len(content_id) >= 16 and content_id[6] == "-":
        return content_id[7:16]
    return None


def _install_pkgs(rom: Path) -> Path:
    """Install-if-needed hook for .pkg roms; returns the installed EBOOT to
    boot. When the rom has its own folder, every .pkg in it is installed
    (base + update + DLC) and every .rap/.edat license is copied into
    exdata. Already-installed titles skip straight to the boot path."""
    folder = rom.parent if rom.parent.resolve() != ROM_ROOT.resolve() else None
    siblings = sorted(folder.iterdir()) if folder else [rom]
    pkgs = [p for p in siblings if p.is_file() and p.suffix.lower() == ".pkg"] or [rom]
    licenses = [p for p in siblings if p.is_file() and p.suffix.lower() in (".rap", ".edat")]

    if licenses:
        EXDATA_DIR.mkdir(parents=True, exist_ok=True)
        for lic in licenses:
            dest = EXDATA_DIR / lic.name
            if not dest.exists():
                shutil.copy2(lic, dest)
                log.info("rpcs3: installed license %s", lic.name)

    title_id = _pkg_title_id(rom) or next(
        (t for t in map(_pkg_title_id, pkgs) if t), None
    )
    if title_id:
        eboot = GAME_DIR / title_id / "USRDIR" / "EBOOT.BIN"
        if eboot.is_file():
            log.info("rpcs3: %s already installed, booting %s", title_id, eboot)
            return eboot

    before = {p.name for p in GAME_DIR.iterdir()} if GAME_DIR.is_dir() else set()
    for pkg in pkgs:
        _run_headless(["--installpkg", str(pkg)], f"pkg install ({pkg.name})")

    if title_id is None:
        # Header parse failed; the install itself tells us the title dir.
        after = {p.name for p in GAME_DIR.iterdir()} if GAME_DIR.is_dir() else set()
        new = sorted(after - before)
        if new:
            title_id = new[0]
    if title_id is None:
        raise RuntimeError(f"could not determine title id for {rom.name}, see {RPCS3_LOG_PATH}")
    eboot = GAME_DIR / title_id / "USRDIR" / "EBOOT.BIN"
    if not eboot.is_file():
        raise RuntimeError(
            f"pkg install of {rom.name} produced no {eboot}, see {RPCS3_LOG_PATH}"
        )
    log.info("rpcs3: installed %s, booting %s", title_id, eboot)
    return eboot


def _state_dir_for(serial: str | None) -> Path | None:
    return (SSTATE_ROOT / serial) if serial else None


def _state_snapshot(serial: str | None) -> dict:
    d = _state_dir_for(serial)
    if d is None or not d.is_dir():
        return {}
    snap = {}
    for pattern in ("*.SAVESTAT", "*.SAVESTAT.zst", "*.SAVESTAT.gz"):
        for p in d.glob(pattern):
            try:
                st = p.stat()
                snap[p] = (st.st_size, st.st_mtime)
            except OSError:
                pass
    return snap


def _newest_state(serial: str | None) -> Path | None:
    """Newest state file for `serial`. RPCS3 already isolates every title
    under its own savestates/<title_id>/ dir, so newest-by-mtime in there is
    always this title's own most recent capture."""
    best: tuple[float, Path] | None = None
    for p, (_size, mtime) in _state_snapshot(serial).items():
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best is not None else None


def _changed_state(serial: str | None, before: dict) -> Path | None:
    best: tuple[float, Path] | None = None
    for p, cur in _state_snapshot(serial).items():
        if before.get(p) != cur:
            mtime = cur[1]
            if best is None or mtime > best[0]:
                best = (mtime, p)
    return best[1] if best is not None else None


def _wait_for_state_write(serial: str | None, before: dict, deadline: float) -> Path | None:
    """Poll the title's savestate dir until a new/changed file's size holds
    steady for STABLE_SECS, or the deadline passes.

    RPCS3 exiting is not itself proof the write finished: the hotkey ends
    the process once the state is captured, but nothing here can tell
    whether compression to disk still trails that. Polling the file's own
    size is the only confirmation there is, the same as every other
    emulator's write-confirmation loop."""
    STABLE_SECS = 0.5
    POLL_SECS = 0.2
    target: Path | None = None
    last_size: int | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        p = _changed_state(serial, before)
        if p is None:
            time.sleep(POLL_SECS)
            continue
        try:
            size = p.stat().st_size
        except OSError:
            time.sleep(POLL_SECS)
            continue
        if target != p:
            target, last_size, stable_since = p, size, time.monotonic()
        elif size != last_size:
            last_size, stable_since = size, time.monotonic()
        elif stable_since is not None and time.monotonic() - stable_since >= STABLE_SECS:
            log.info("save state write complete: %s (%d bytes)", target.name, last_size)
            return target
        time.sleep(POLL_SECS)
    if target is not None:
        log.warning("save state write never stabilized within timeout: %s", target)
    return None


def _pine_recv_exact(sock: _socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _pine_request(opcode: int, payload: bytes = b"", timeout: float = 5.0) -> bytes | None:
    """One PINE request. Wire format (LE): u32 total size, u8 opcode, payload;
    reply is u32 size, u8 result (0 = OK), payload. Same wire format PCSX2's
    PINE variant uses; RPCS3 just implements a smaller opcode set (no
    save/load-state) on top of it."""
    packet = struct.pack("<IB", 5 + len(payload), opcode) + payload
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(PINE_SOCKET))
            sock.sendall(packet)
            header = _pine_recv_exact(sock, 5)
            if header is None:
                return None
            size, result = struct.unpack("<IB", header)
            body = b""
            if size > 5:
                body = _pine_recv_exact(sock, size - 5) or b""
            if result != 0:
                log.warning("PINE opcode 0x%02X rejected (result %d)", opcode, result)
                return None
            return body
    except OSError as exc:
        log.warning("PINE request failed on %s (opcode 0x%02X): %s", PINE_SOCKET, opcode, exc)
        return None


def _pine_status() -> int | None:
    """0 running, 1 paused, 2 shutdown, None if IPC is down or disabled."""
    body = _pine_request(_PINE_MSG_STATUS, timeout=2.0)
    if body is None or len(body) < 4:
        return None
    return struct.unpack("<I", body[:4])[0]


def _pine_title_id() -> str | None:
    body = _pine_request(_PINE_MSG_ID, timeout=2.0)
    if not body:
        return None
    return body.split(b"\0", 1)[0].decode("ascii", "replace") or None


class Rpcs3(Emulator):
    name = "rpcs3"
    display_name = "RPCS3"
    save_root = DEV_HDD0
    rom_extensions = ROM_EXTENSIONS
    _restoring = False
    _session_serial: str | None = None
    _session_start = 0.0
    log_path = RPCS3_LOG_PATH
    # No SIGTERM handler: the default action ends the process at once, saves
    # are already on disk. The grace window only covers process-group
    # teardown of the AppImage wrapper.
    term_timeout = float(os.environ.get("RPCS3_STOP_WAIT", "2"))

    def __init__(self):
        super().__init__()
        self._launch_seq = 0
        # Only set for an archive boot; stop() removes it when the cache is
        # disabled so nothing outlives the session it was extracted for.
        self._extracted_dir: Path | None = None

    def clear_working_slot(self) -> None:
        """RPCS3 has no fixed slot to clear, but activate() calls this before
        it ever reads save_subtrees to restore an archive, which makes it the
        one place that is safe to create the savestates symlink: it runs on
        every activate, but never during a bare construction (registry
        sweeps build every emulator against real, unredirected paths).

        It also wipes every title's savestates dir and flips _restoring on
        early. Wiping first means a leftover local state from a previous
        session never outranks (by mtime) whatever this session's archive
        restores, and it caps SSTATE_ROOT at whatever the current session
        itself produces rather than growing across every activate. Flipping
        _restoring here, not just in prepare_restore(), matters because
        api.py reads save_subtrees for the restore extract before it ever
        calls prepare_restore()."""
        _ensure_sstate_link()
        self._restoring = True
        if SSTATE_ROOT.is_dir():
            for child in SSTATE_ROOT.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except OSError as exc:
                    log.warning("could not clear stale savestate %s: %s", child, exc)

    @property
    def save_subtrees(self) -> tuple[str, ...]:
        """cellSaveData saves, save states, plus the cellGameData save dirs
        under game/.

        game/ mixes save data with installed PKG titles and RPCS3's ＄locks
        dir, so the dump enumerates only the dirs without a bootable
        EBOOT.BIN. A restore inverts the problem: the archive holds nothing
        but previously dumped save dirs, and those dirs don't exist on disk
        yet, so the whole game/ prefix is declared to let them through."""
        if self._restoring:
            return ("home/00000001/savedata", "game", "savestates")
        subtrees = ["home/00000001/savedata", "savestates"]
        subtrees += [f"game/{d.name}" for d in _gamedata_dirs()]
        return tuple(subtrees)

    def prepare_restore(self) -> None:
        self.stop()
        self._restoring = True

    def resolve_rom_file(self, path: Path) -> Path | None:
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError:
                return None
        return _pick_rom_file(candidates, path)

    def _xdotool(self, *args: str) -> str | None:
        """Run xdotool, returning its stdout, or None if it failed."""
        try:
            result = subprocess.run(
                [_XDOTOOL, *args],
                env=base_launch_env(),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("xdotool %s failed: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            log.warning("xdotool %s: %s", " ".join(args), result.stderr.strip())
            return None
        return result.stdout

    def _game_window(self) -> str | None:
        out = self._xdotool("search", "--class", _WINDOW_CLASS)
        if not out:
            log.warning("no rpcs3 window found")
            return None
        return out.split()[0]

    def _send_key(self, key: str) -> bool:
        """Focus the render window and send `key` through XTEST.

        Activating first is what makes this survive the player clicking back
        into the page: XTEST delivers to whatever holds focus, so a key sent
        at an unfocused RPCS3 goes to the desktop instead."""
        win_id = self._game_window()
        if win_id is None:
            return False
        if self._xdotool("windowactivate", "--sync", win_id) is None:
            return False
        return self._xdotool("key", "--clearmodifiers", key) is not None

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        self._restoring = False
        self.boot_failed = False
        self._launch_seq += 1
        seq = self._launch_seq

        _patch_config()
        _patch_ipc()
        ext = rom_path.suffix.lower()
        if ext == ".pkg":
            boot = _install_pkgs(rom_path)
        elif ext in _ARCHIVE_EXTS:
            boot = _extract_and_cache(rom_path)
            self._extracted_dir = CACHE_DIR / _cache_key(rom_path)
        else:
            boot = rom_path

        # Save dirs (and now states) are named with the title serial as
        # prefix; remember it so the exit dump can be scoped to this title.
        # Installed titles boot from game/<serial>/USRDIR/EBOOT.BIN, disc
        # rips carry a PARAM.SFO next to USRDIR. An .iso has no readable
        # serial from the path alone; the boot watchdog fills it in from
        # PINE once the game is confirmed running.
        if boot.name.upper().startswith("EBOOT"):
            if boot.is_relative_to(GAME_DIR):
                self._session_serial = boot.parent.parent.name
            else:
                self._session_serial = _sfo_title_id(boot.parent.parent / "PARAM.SFO")
        else:
            self._session_serial = None

        # RPCS3 has no boot-time state-load flag of its own, but it detects a
        # savestate by magic bytes regardless of what is passed as the boot
        # target, so pointing it at the state file IS the boot-time load.
        target = boot
        if resume_slot is not None:
            if self._session_serial is None:
                log.warning(
                    "resume requested but %s has no known title id, booting fresh",
                    rom_path.name,
                )
            else:
                state = _newest_state(self._session_serial)
                if state is None:
                    log.warning(
                        "resume requested but no savestate in %s",
                        SSTATE_ROOT / self._session_serial,
                    )
                else:
                    target = state

        self._session_start = time.time()
        log.info(
            "launching rpcs3 (rom=%s, boot=%s, serial=%s)",
            rom_path, target, self._session_serial,
        )
        self._spawn([_rpcs3_bin(), "--no-gui", "--fullscreen", str(target)], _launch_env())
        Thread(target=self._boot_watchdog, args=(seq,), daemon=True).start()

    def _boot_watchdog(self, seq: int) -> None:
        """Confirm the freshly launched game reaches a running state via
        PINE, and opportunistically resolve the title id via MsgID for boots
        that had no serial from the file path alone (a bare .iso).

        A process still alive when the deadline passes without ever
        reporting running is the boot-error-dialog case: RPCS3 does not exit
        on its own, so nothing else in the broker would ever notice."""
        deadline = time.monotonic() + BOOT_WAIT
        while time.monotonic() < deadline:
            if self._launch_seq != seq:
                log.info("boot watchdog: launch superseded, abandoning")
                return
            if _pine_status() == 0:
                if not self._session_serial:
                    title = _pine_title_id()
                    if self._launch_seq != seq:
                        log.info(
                            "boot watchdog: launch superseded during title lookup, "
                            "discarding %s",
                            title,
                        )
                        return
                    if title:
                        self._session_serial = title
                        log.info("boot watchdog: resolved title id %s via PINE", title)
                return
            time.sleep(1.0)

        if self._launch_seq != seq:
            return
        if self.alive():
            self.boot_failed = True
            log.warning(
                "boot watchdog: rpcs3 never reported a running state and is "
                "still alive, treating as a boot failure"
            )
        else:
            log.warning("boot watchdog: rpcs3 exited before ever reporting running")

    def save_and_exit(self, slot: int | None) -> dict:
        saved = False
        state_file = None
        if slot is not None and self.alive():
            if not self._session_serial:
                log.warning("save-and-exit: no known title id, cannot locate a state to save")
            else:
                before = _state_snapshot(self._session_serial)
                if not self._send_key(_SAVE_STATE_KEY):
                    log.warning("save-and-exit: could not send the save-state hotkey")
                else:
                    p = _wait_for_state_write(
                        self._session_serial, before, time.monotonic() + STATE_WAIT
                    )
                    if p is None:
                        log.warning("save-and-exit: no new state file appeared")
                    else:
                        try:
                            st = p.stat()
                        except OSError as exc:
                            log.warning("save-and-exit: could not stat saved state %s: %s", p, exc)
                        else:
                            saved = True
                            state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}

        # The dump ships files newer than the session baseline. A save is a
        # directory tree the game rewrites only partially, and sibling dirs
        # belong to other titles, so refresh every mtime in this title's
        # save dirs: they ship whole, the rest stay filtered out.
        now = time.time()
        for d in self._session_save_dirs():
            for p in d.rglob("*"):
                if p.is_file():
                    try:
                        os.utime(p, (now, now))
                    except OSError as exc:
                        log.warning("could not restamp %s, may be dropped from the dump: %s", p, exc)

        self.stop()
        return {"state_saved": saved, "state_slot": slot, "state_file": state_file}

    def stop(self) -> None:
        # Invalidate any in-flight boot watchdog before the kill.
        self._launch_seq += 1
        super().stop()
        if not CACHE_ENABLED and self._extracted_dir is not None:
            log.info("rpcs3 cache disabled: removing extracted copy %s", self._extracted_dir.name)
            shutil.rmtree(self._extracted_dir, ignore_errors=True)
            self._extracted_dir = None

    def _session_save_dirs(self) -> list[Path]:
        """This title's save dirs: name prefixed with the session serial, or
        containing a file written while the session ran."""
        savedata = USER_HOME / "savedata"
        candidates = sorted(d for d in savedata.iterdir() if d.is_dir()) if savedata.is_dir() else []
        candidates += _gamedata_dirs()
        selected = []
        for d in candidates:
            if self._session_serial and d.name.startswith(self._session_serial):
                selected.append(d)
                continue
            try:
                if any(
                    p.is_file() and p.stat().st_mtime >= self._session_start
                    for p in d.rglob("*")
                ):
                    selected.append(d)
            except OSError:
                continue
        return selected
