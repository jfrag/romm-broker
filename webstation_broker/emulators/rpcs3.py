"""RPCS3 (PlayStation 3) launcher: config.yml patching, PKG install hook,
and SIGTERM shutdown.

RPCS3 has no runtime control channel and installs no signal handler; the
whole lifecycle is CLI + config file. Save states exist only as GUI actions,
so persistence is the game's own save data, which the emulator writes to
plain host files the moment the game saves: cellSaveData saves under
`dev_hdd0/home/00000001/savedata/`, cellGameData saves under
`dev_hdd0/game/`. Nothing needs flushing at exit, which makes SIGTERM (a
hard kill) a safe stop.

Boot formats: decrypted .iso images and disc folder rips
(PS3_GAME/USRDIR/EBOOT.BIN) boot directly. A .pkg is an installer, not an
image: the first activation installs it via `--headless --installpkg` into
`dev_hdd0/game/<TITLEID>/` and later activations boot the installed
EBOOT.BIN. .rap/.edat licenses are plain files copied into
`dev_hdd0/home/00000001/exdata/`.

The emulator is expected to be brought to a working state in desktop mode
(firmware, GPU settings, controllers) before automated launching.
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

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
DEV_HDD0 = DATA_DIR / "dev_hdd0"
USER_HOME = DEV_HDD0 / "home" / "00000001"
EXDATA_DIR = USER_HOME / "exdata"
GAME_DIR = DEV_HDD0 / "game"
RPCS3_LOG_PATH = Path(os.environ.get("RPCS3_LOG_PATH", "/config/rpcs3.log"))
# PKG decryption/extraction can run minutes for multi-GB packages.
INSTALL_TIMEOUT = float(os.environ.get("RPCS3_INSTALL_TIMEOUT", "1800"))

# Boot formats, best first: decrypted ISO and PKG installer beat a bare
# EBOOT so a folder holding both the rip and its installer picks the image.
ROM_EXTENSIONS = (".iso", ".pkg", ".bin", ".self", ".elf")
# EBOOT.BIN sits three levels down in a disc rip (PS3_GAME/USRDIR/EBOOT.BIN).
_ROM_SEARCH_GLOBS = ("*", "*/*", "*/*/*")
# Only executables named EBOOT.* are bootable; other .bin/.self files in a
# rip (licenses, sdata) are not.
_EBOOT_EXTS = (".bin", ".self", ".elf")

# config.yml values forced before every launch. RPCS3 fills missing keys
# with defaults, so a partial file is a valid config.
_CONFIG_PATCHES: dict[tuple[str, str], str] = {
    ("Miscellaneous", "Automatically start games after boot"): "true",
    # Game quit (or XMB exit) ends the process, so alive() tracks the game.
    ("Miscellaneous", "Exit RPCS3 when process finishes"): "true",
    # The labwc session gives no focus guarantees; a focus-loss pause would
    # freeze the game invisibly.
    ("Miscellaneous", "Pause emulation on RPCS3 focus loss"): "false",
}


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


def _patch_config() -> None:
    """Force broker-required config.yml values before every launch.

    config.yml is two-level YAML: unindented `Section:` headers over
    2-space-indented `Key: value` lines. Patched line-wise so every other
    setting the user tuned through the GUI survives untouched."""
    try:
        if not CONFIG_PATH.exists():
            log.info("config.yml not found at %s, seeding one", CONFIG_PATH)
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text("")
        lines = CONFIG_PATH.read_text().splitlines()
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
            for (sec, key), val in _CONFIG_PATCHES.items():
                if section == sec and stripped.startswith(f"{key}:"):
                    new_lines.append(f"  {key}: {val}")
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        missing = [(s, k, v) for (s, k), v in _CONFIG_PATCHES.items() if (s, k) not in applied]
        if missing:
            present = {
                ln.strip()[:-1]
                for ln in new_lines
                if ln and not ln[0].isspace() and ln.rstrip().endswith(":")
            }
            for sec, key, val in missing:
                if sec in present:
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
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(CONFIG_PATH)
    except Exception:
        log.exception("config.yml patch failed, broker settings NOT applied")


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

    @property
    def save_subtrees(self) -> tuple[str, ...]:
        """cellSaveData saves plus the cellGameData save dirs under game/.

        game/ mixes save data with installed PKG titles and RPCS3's ＄locks
        dir, so the dump enumerates only the dirs without a bootable
        EBOOT.BIN. A restore inverts the problem: the archive holds nothing
        but previously dumped save dirs, and those dirs don't exist on disk
        yet, so the whole game/ prefix is declared to let them through."""
        if self._restoring:
            return ("home/00000001/savedata", "game")
        subtrees = ["home/00000001/savedata"]
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

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        self._restoring = False
        if resume_slot is not None:
            log.info(
                "rpcs3 save states are unsupported, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        _patch_config()
        boot = _install_pkgs(rom_path) if rom_path.suffix.lower() == ".pkg" else rom_path

        # Save dirs are named with the title serial as prefix; remember it so
        # the exit dump can be scoped to this title. Installed titles boot
        # from game/<serial>/USRDIR/EBOOT.BIN, disc rips carry a PARAM.SFO
        # next to USRDIR. An .iso has no readable serial, so those sessions
        # fall back to shipping the save dirs written while the game ran.
        if boot.name.upper().startswith("EBOOT"):
            if boot.is_relative_to(GAME_DIR):
                self._session_serial = boot.parent.parent.name
            else:
                self._session_serial = _sfo_title_id(boot.parent.parent / "PARAM.SFO")
        else:
            self._session_serial = None
        self._session_start = time.time()

        log.info(
            "launching rpcs3 (rom=%s, boot=%s, serial=%s)",
            rom_path, boot, self._session_serial,
        )
        self._spawn([_rpcs3_bin(), "--no-gui", "--fullscreen", str(boot)], _launch_env())

    def save_and_exit(self, slot: int) -> dict:
        self.stop()
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
                    except OSError:
                        pass
        return {"state_saved": None, "state_slot": None, "state_file": None}

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
