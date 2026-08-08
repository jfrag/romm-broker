"""xemu launcher (original Xbox): FATX-level save sync on a raw HDD image.

xemu keeps game saves inside the one hard-disk image its xemu.toml points at,
and QEMU exposes that image only as an opaque block device — the previous
design therefore shipped the entire qcow2 as the save artifact. This version
syncs at the filesystem level instead: the image is kept in raw format so
pyfatx (userspace libfatx bindings — no FUSE, no NBD, container-safe) can
read and write the FATX E partition directly, and the save archive carries
only the launched title's save data.

  image:  the first launch after deploy finds the configured qcow2 and
          converts it in place with qemu-img — same filename, raw content,
          sparse on disk — keeping the original alongside as <name>.backup.
          A raw image cannot hold QEMU internal snapshots, so the save-state
          interface is gone; save data is the whole artifact now.
  launch: when the activate restored an archive, its files are written into
          E:/UDATA and E:/TDATA before xemu boots (pre-launch hook).
  exit:   once xemu has stopped and flushed, the launched title's UDATA and
          TDATA trees are extracted from the image into the staging dir
          (post-close hook), where the standard dump zips them.
  title:  the title id (the UDATA directory name) is read from the disc's
          default.xbe certificate; if the disc cannot be parsed, extraction
          falls back to every title on the disk rather than losing the
          session's saves.
"""

import logging
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

from pyfatx import Fatx

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

XEMU_BIN = os.environ.get("XEMU_BIN", "/opt/xemu/AppRun")
XEMU_LOG_PATH = Path(os.environ.get("XEMU_LOG_PATH", "/config/xemu.log"))


def _default_toml_path() -> Path:
    """xemu.toml lives in SDL's pref dir: $XDG_DATA_HOME/xemu/xemu, or
    ~/.local/share/xemu/xemu when XDG_DATA_HOME is unset."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        base = Path(os.environ.get("HOME", "/config")) / ".local/share"
    return base / "xemu" / "xemu" / "xemu.toml"


XEMU_TOML = Path(os.environ.get("XEMU_TOML", str(_default_toml_path())))
# Only when xemu.toml cannot tell us: fresh container where xemu has never
# run, or a config with no usable hdd_path.
FALLBACK_HDD_IMAGE = Path(
    os.environ.get("XEMU_HDD_IMAGE", "/config/xemu/xbox_hdd.qcow2")
)

# Staging directory next to the HDD image; the generic dump/restore reads and
# writes host files here, and the launch/exit hooks move them in and out of
# the FATX filesystem.
SAVE_STAGING_DIRNAME = "saves"

QCOW2_MAGIC = b"QFI\xfb"

# Only XISO, which is always named .iso, including the .xiso.iso double
# extension some dumps use.
ROM_EXTENSIONS = (".iso",)
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


def _hdd_image_path() -> Path:
    """The disk image xemu will actually mount, read from the user's config.

    The user configures xemu themselves, so their xemu.toml [sys.files]
    hdd_path is the authority; a broker-side path would drift from it the
    moment they repoint one of them."""
    try:
        with XEMU_TOML.open("rb") as fh:
            cfg = tomllib.load(fh)
        raw = str(cfg.get("sys", {}).get("files", {}).get("hdd_path", "") or "")
    except OSError as exc:
        log.warning("could not read %s (%s); assuming hdd at %s",
                    XEMU_TOML, exc, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    except tomllib.TOMLDecodeError as exc:
        log.error("could not parse %s (%s); assuming hdd at %s",
                  XEMU_TOML, exc, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    if not raw:
        log.warning("%s has no sys.files.hdd_path; assuming hdd at %s",
                    XEMU_TOML, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    p = Path(raw).expanduser()
    # A relative hdd_path or one sitting in / gives the save staging dir no
    # sane directory to live in.
    if not p.is_absolute() or not p.parent.name:
        log.error("xemu.toml hdd_path %r is unusable; assuming hdd at %s",
                  raw, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    return p


def _disc_number(rel: Path) -> int:
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _pick_rom_file(candidates, base: Path) -> Path | None:
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
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
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


# ── One-time image conversion ────────────────────────────────────────────────


def _ensure_raw_image(image: Path) -> bool:
    """Make sure `image` holds raw disk content; True when FATX access is
    possible afterwards.

    Detection is the qcow2 magic, so this runs as a cheap no-op on every
    launch and converts exactly once. The raw content replaces the file under
    its original name — xemu format-probes the content, so the .qcow2 name
    keeps working and the user's xemu.toml never needs to change. The
    original qcow2 (including any internal snapshots) is kept alongside as a
    backup. On any failure the qcow2 is left in place so the session can
    still play; only save sync is lost."""
    try:
        with image.open("rb") as fh:
            magic = fh.read(len(QCOW2_MAGIC))
    except OSError as exc:
        log.error("could not read HDD image %s: %s", image, exc)
        return False
    if magic != QCOW2_MAGIC:
        return True

    backup = image.with_name(image.name + ".backup")
    tmp = image.with_name(f".{image.name}.raw-tmp")
    log.info("one-time HDD conversion: %s qcow2 -> raw", image)
    try:
        subprocess.run(
            ["qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(image), str(tmp)],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        log.error("qemu-img not found; cannot convert %s", image)
        return False
    except subprocess.CalledProcessError as exc:
        log.error("qemu-img convert failed for %s: %s", image, exc.stderr.strip())
        tmp.unlink(missing_ok=True)
        return False
    try:
        if backup.exists():
            log.warning("overwriting previous HDD backup %s", backup)
        os.replace(image, backup)
        os.replace(tmp, image)
    except OSError as exc:
        log.error("could not swap converted HDD image into place: %s", exc)
        if not image.exists() and backup.exists():
            os.replace(backup, image)
        tmp.unlink(missing_ok=True)
        return False
    log.info("HDD image now raw; original qcow2 kept at %s", backup)
    return True


# ── Title id from the disc image ─────────────────────────────────────────────

_XISO_SECTOR = 2048
_XISO_MAGIC = b"MICROSOFT*XBOX*MEDIA"
# Game-partition offsets to probe: 0 for an extracted XISO, 0x18300000 for a
# full dump that still carries the video partition up front.
_XISO_BASES = (0, 0x18300000)


def _xiso_root_dir(fh) -> tuple[int, int, int] | None:
    """(partition base, root dir file offset, root dir size) of the disc."""
    for base in _XISO_BASES:
        fh.seek(base + 32 * _XISO_SECTOR)
        vd = fh.read(_XISO_SECTOR)
        if len(vd) == _XISO_SECTOR and vd[: len(_XISO_MAGIC)] == _XISO_MAGIC:
            sector = int.from_bytes(vd[20:24], "little")
            size = int.from_bytes(vd[24:28], "little")
            return base, base + sector * _XISO_SECTOR, size
    return None


def _xiso_find_entry(table: bytes, name: bytes) -> tuple[int, int] | None:
    """(start sector, file size) of `name` in one XISO directory table.

    Entries form a binary tree; left/right links are dword offsets into the
    table. The visited set and bounds checks make a corrupt or 0xFF-padded
    table terminate instead of looping."""
    stack = [0]
    seen: set[int] = set()
    while stack:
        off = stack.pop() * 4
        if off in seen or off + 14 > len(table):
            continue
        seen.add(off)
        left = int.from_bytes(table[off:off + 2], "little")
        right = int.from_bytes(table[off + 2:off + 4], "little")
        name_len = table[off + 13]
        entry_name = table[off + 14:off + 14 + name_len]
        if 0 < name_len <= 42 and len(entry_name) == name_len:
            if entry_name.lower() == name:
                start = int.from_bytes(table[off + 4:off + 8], "little")
                size = int.from_bytes(table[off + 8:off + 12], "little")
                return start, size
            for branch in (left, right):
                if branch not in (0, 0xFFFF):
                    stack.append(branch)
    return None


def _disc_title_id(rom_path: Path) -> str | None:
    """Title id from the disc's default.xbe certificate, formatted the way
    the Xbox names save dirs (E:/UDATA/<id>), or None when unreadable."""
    try:
        with rom_path.open("rb") as fh:
            root = _xiso_root_dir(fh)
            if root is None:
                log.warning("%s: no XISO volume descriptor found", rom_path)
                return None
            base, dir_off, dir_size = root
            fh.seek(dir_off)
            entry = _xiso_find_entry(fh.read(dir_size), b"default.xbe")
            if entry is None:
                log.warning("%s: no default.xbe in the disc root", rom_path)
                return None
            xbe_off, xbe_size = entry
            xbe_off = base + xbe_off * _XISO_SECTOR
            fh.seek(xbe_off)
            header = fh.read(0x11C)
            if len(header) < 0x11C or header[:4] != b"XBEH":
                log.warning("%s: default.xbe has no XBE header", rom_path)
                return None
            base_addr = int.from_bytes(header[0x104:0x108], "little")
            cert_addr = int.from_bytes(header[0x118:0x11C], "little")
            cert_off = cert_addr - base_addr
            if cert_off < 0 or cert_off + 12 > xbe_size:
                log.warning("%s: XBE certificate out of bounds", rom_path)
                return None
            fh.seek(xbe_off + cert_off)
            cert = fh.read(12)
            if len(cert) < 12:
                return None
            # Uppercase because that is how the dashboard names the directory
            # it creates, and a directory this code creates has to match.
            return f"{int.from_bytes(cert[8:12], 'little'):08X}"
    except OSError as exc:
        log.warning("could not read %s for a title id: %s", rom_path, exc)
        return None


# ── FATX access (pyfatx surfaces libfatx errors as bare AssertionError) ──────


def _open_fatx_e(image: Path) -> Fatx | None:
    try:
        return Fatx(str(image), drive="e")
    except (AssertionError, OSError):
        log.error("could not open the FATX E partition on %s", image)
        return None


def _fatx_isdir(fs: Fatx, path: str) -> bool:
    try:
        return bool(fs.get_attr(path).is_directory)
    except AssertionError:
        return False


def _fatx_find_dir(fs: Fatx, parent: str, name: str) -> str | None:
    """Path of `parent`'s child directory matching `name` whatever its case.

    libfatx compares names byte for byte, so a lookup has to use the case the
    disk actually holds. Titles vary in how they case their save directories,
    and the miss would be silent: nothing resolves, nothing is extracted, and
    the session's saves stay behind in the image."""
    wanted = name.lower()
    try:
        entries = list(fs.listdir(parent))
    except (AssertionError, OSError):
        return None
    for attr in entries:
        if attr.is_directory and attr.filename.lower() == wanted:
            return f"{parent.rstrip('/')}/{attr.filename}"
    return None


# ── Provider ─────────────────────────────────────────────────────────────────


def _reap_strays() -> None:
    """SIGKILL any xemu the broker does not own. An orphan busy-loops CPU
    cores and — worst — keeps writing the HDD image the save hooks are about
    to touch. Matched by full command line so other AppImage emulators
    (duckstation is also comm "AppRun") are left alone."""
    if subprocess.run(["pkill", "-9", "-f", XEMU_BIN], capture_output=True).returncode == 0:
        log.info("reaped stray xemu process(es)")
        time.sleep(0.5)


class Xemu(Emulator):
    name = "xemu"
    display_name = "xemu"
    rom_extensions = ROM_EXTENSIONS
    log_path = XEMU_LOG_PATH
    # SIGTERM gives QEMU a clean shutdown that flushes the HDD image the
    # post-close extraction is about to read; give the flush time to land
    # before the SIGKILL escalation tears it.
    term_timeout = float(os.environ.get("XEMU_STOP_WAIT", "15"))

    def __init__(self):
        super().__init__()
        # Resolved once per session so the whole activate/exit round trip
        # sees one image, even if xemu rewrites its config mid-session.
        self.hdd_image = _hdd_image_path()
        self.staging_dir = self.hdd_image.parent / SAVE_STAGING_DIRNAME
        self.save_root = self.hdd_image.parent
        self.save_subtrees = (SAVE_STAGING_DIRNAME,)
        self._restore_pending = False
        self._title_id: str | None = None
        parked = self.hdd_image.with_name(f".{self.hdd_image.name}.prev")
        if not self.hdd_image.exists() and parked.is_file():
            log.info("recovering HDD image parked by a previous version")
            try:
                os.replace(parked, self.hdd_image)
            except OSError as exc:
                log.error("could not recover parked image %s: %s", parked, exc)
        log.info("xemu hdd image: %s (save staging %s)", self.hdd_image, self.staging_dir)

    def _clear_staging(self) -> None:
        if self.staging_dir.is_dir():
            try:
                shutil.rmtree(self.staging_dir)
            except OSError as exc:
                log.warning("could not fully clear %s: %s", self.staging_dir, exc)

    def _inject_saves(self) -> int:
        """Pre-launch hook: write every staged file into the FATX E
        partition. Returns the number of files that landed."""
        if not self.staging_dir.is_dir():
            return 0
        files = [
            p for p in sorted(self.staging_dir.rglob("*"))
            if p.is_file() and not p.is_symlink()
            and not any(part.startswith(".") for part in p.relative_to(self.staging_dir).parts)
        ]
        if not files:
            return 0
        fs = _open_fatx_e(self.hdd_image)
        if fs is None:
            return 0
        written = 0
        # Directory paths as they exist on the image, keyed by the staged
        # (case-bearing) components, so an archive whose case differs from the
        # disk's lands in the existing directory instead of a twin beside it.
        resolved: dict[tuple[str, ...], str] = {(): ""}
        for p in files:
            parts = p.relative_to(self.staging_dir).parts
            try:
                parent = ""
                for i in range(1, len(parts)):
                    key = parts[:i]
                    if key in resolved:
                        parent = resolved[key]
                        continue
                    found = _fatx_find_dir(fs, parent or "/", parts[i - 1])
                    if found is None:
                        found = f"{parent}/{parts[i - 1]}"
                        fs.mkdir(found)
                    resolved[key] = found
                    parent = found
                fatx_path = f"{parent}/{parts[-1]}"
                data = p.read_bytes()
                fs.write(fatx_path, data)
                # pyfatx write() never shrinks an existing file; drop the old
                # tail or a shorter save lands corrupt.
                if fs.get_attr(fatx_path).file_size != len(data):
                    fs.truncate(fatx_path, len(data))
                written += 1
            except (AssertionError, OSError) as exc:
                log.warning("could not inject %s into the HDD image: %s",
                            "/".join(parts), exc)
        del fs
        return written

    def _extract_saves(self) -> int:
        """Post-close hook: copy the title's save data out of the FATX E
        partition into the (already cleared) staging dir. The files carry
        fresh mtimes, so the dump's launch-baseline filter ships them all —
        the archive is the title's complete save set, not a delta."""
        fs = _open_fatx_e(self.hdd_image)
        if fs is None:
            return 0
        roots = []
        for top in ("UDATA", "TDATA"):
            if not _fatx_isdir(fs, f"/{top}"):
                continue
            if not self._title_id:
                roots.append(f"/{top}")
                continue
            src = _fatx_find_dir(fs, f"/{top}", self._title_id)
            if src is None:
                log.info("no /%s/%s on the HDD image", top, self._title_id)
            else:
                roots.append(src)
        if not roots:
            log.warning("no save directories found on %s for title %s; the "
                        "archive will be empty", self.hdd_image, self._title_id or "<all>")
        extracted = 0
        for src in roots:
            for root, _dirs, filenames in fs.walk(src):
                for name in filenames:
                    fatx_path = root.rstrip("/") + "/" + name
                    try:
                        data = bytes(fs.read(fatx_path))
                    except (AssertionError, OSError) as exc:
                        log.warning("could not read %s from the HDD image: %s",
                                    fatx_path, exc)
                        continue
                    dest = self.staging_dir / fatx_path.lstrip("/")
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                    except OSError as exc:
                        log.warning("could not stage %s: %s", dest, exc)
                        continue
                    extracted += 1
        del fs
        return extracted

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

    def prepare_restore(self) -> None:
        """Before the archive is extracted: stop anything holding the image,
        make sure it is raw, and empty the staging dir — leftovers from the
        previous session's dump would otherwise mix into the injection, and
        the newer-file guard could skip archive members over them."""
        self.stop()
        _reap_strays()
        _ensure_raw_image(self.hdd_image)
        self._clear_staging()
        self._restore_pending = True

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        _reap_strays()
        raw_ok = _ensure_raw_image(self.hdd_image)

        self._title_id = _disc_title_id(rom_path)
        if self._title_id:
            log.info("disc title id: %s", self._title_id)
        else:
            log.warning("no title id for %s; the exit dump will carry every "
                        "title on the disk", rom_path)

        if self._restore_pending:
            self._restore_pending = False
            if raw_ok:
                injected = self._inject_saves()
                log.info("pre-launch: injected %d save file(s) into the HDD image", injected)
            else:
                log.error("pre-launch: HDD image is not raw; restored saves were NOT injected")

        if resume_slot:
            log.warning("resume: save states are not supported on a raw HDD "
                        "image; slot %d ignored, booting fresh", resume_slot)

        log.info("launching xemu (rom=%s)", rom_path)
        self._spawn([XEMU_BIN, "-dvd_path", str(rom_path)], base_launch_env())

    def save_and_exit(self, slot: int) -> dict:
        self.stop()
        _reap_strays()
        extracted = 0
        if self.hdd_image.is_file():
            self._clear_staging()
            extracted = self._extract_saves()
            log.info("post-close: staged %d save file(s) for title %s",
                     extracted, self._title_id or "<all>")
        else:
            log.error("post-close: HDD image %s is missing; nothing to extract",
                      self.hdd_image)
        return {
            "state_saved": None,
            "state_slot": None,
            "state_file": None,
            "title_id": self._title_id,
            "saves_extracted": extracted,
        }
