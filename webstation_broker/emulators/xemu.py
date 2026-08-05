"""xemu launcher (original Xbox): QMP-driven save states on the qcow2 HDD.

xemu is QEMU underneath, so the broker talks to it over a QMP unix socket.
Save data and save states both live inside the one hard-disk image the user's
xemu.toml points at: games write their saves to the emulated Xbox HDD, and
states are QEMU internal snapshots written into the same qcow2 (there is no
way to export a snapshot on its own). The save archive is therefore the whole
image, shipped by the standard dump because its mtime changes during play.
Raise SAVE_FILE_MAX_BYTES in the environment accordingly — disc images run
well past the broker's default archive cap.

Control plane, over the QMP socket:

  launch: the disc goes in the drive at power-on via -dvd_path. Injecting it
          later needs a system_reset, and a reset landing while the guest is
          still inside the MCPX bootrom wedges the machine.
  states: snapshot-save/snapshot-load as QMP jobs against the HDD block node.
          snapshot-save refuses a tag already on the image, so each save goes
          to a fresh "broker-slot-<slot>.<seq>" tag and the older sequences
          are deleted only once the new one landed.
  resume: snapshot-load right after QMP comes up; the snapshot restores a
          machine that was already running this disc.
  restore: the emulator assumes xemu is preconfigured; prepare_restore parks
          the live image aside so the archived one always lands — the
          skip-newer guard would otherwise reject it whenever the container
          seeded a fresh stock image.
"""

import json
import logging
import os
import re
import socket as _socket
import subprocess
import time
import tomllib
from pathlib import Path
from threading import Thread

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


def _hdd_image_path() -> Path:
    """The qcow2 xemu will actually mount, read from the user's own config.

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
    # A relative hdd_path or one sitting in / gives the save dump no sane
    # directory to scope to.
    if not p.is_absolute() or not p.parent.name:
        log.error("xemu.toml hdd_path %r is unusable; assuming hdd at %s",
                  raw, FALLBACK_HDD_IMAGE)
        return FALLBACK_HDD_IMAGE
    return p

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")
QMP_SOCKET = Path(XDG_RUNTIME_DIR) / "xemu-qmp.sock"

QMP_TIMEOUT = float(os.environ.get("XEMU_QMP_TIMEOUT", "2.0"))
# Whole-reply and job-conclusion budget. A snapshot job carries the full VM
# state, so it wants far more room than a plain command round trip.
QMP_WAIT = float(os.environ.get("XEMU_QMP_WAIT", "30.0"))
# QMP answers about a second after the process starts; the long tail is a
# slow cold boot on a busy host.
QMP_BOOT_WAIT = float(os.environ.get("XEMU_BOOT_WAIT", "60.0"))

# Only XISO, which is always named .iso, including the .xiso.iso double
# extension some dumps use.
ROM_EXTENSIONS = (".iso",)
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)

# A state tag is "broker-slot-<slot>" optionally followed by ".<sequence>".
# An unsuffixed tag counts as sequence 0, so it sorts oldest.
_STATE_TAG_RE = re.compile(r"^broker-slot-(\d+)(?:\.(\d+))?$")


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


# ── QMP ──────────────────────────────────────────────────────────────────────


class _QmpSession:
    """One QMP connection: greeting and capabilities done, commands capped by
    a whole-reply budget so a peer streaming async events just under the
    per-recv timeout can never pin the calling thread."""

    def __init__(self):
        self._sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self._buf = b""
        self._sock.settimeout(QMP_TIMEOUT)
        self._sock.connect(str(QMP_SOCKET))
        self.recv_msg()  # greeting
        self._sock.settimeout(QMP_WAIT)
        self.command("qmp_capabilities")

    def recv_msg(self, timeout: float | None = None) -> dict:
        if timeout is not None:
            self._sock.settimeout(timeout)
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("QMP socket closed")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return json.loads(line)

    def command(self, execute: str, args: dict | None = None) -> dict:
        payload: dict = {"execute": execute}
        if args:
            payload["arguments"] = args
        self._sock.sendall(json.dumps(payload).encode() + b"\n")
        deadline = time.monotonic() + QMP_WAIT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"QMP {execute} did not return within {QMP_WAIT:.0f}s")
            msg = self.recv_msg(timeout=remaining)
            if "return" in msg:
                return msg
            if "error" in msg:
                raise ValueError(msg["error"].get("desc", str(msg["error"])))
            # async event — keep draining

    def settimeout(self, timeout: float) -> None:
        self._sock.settimeout(timeout)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _qmp_command(cmd: str, args: dict | None = None) -> dict | None:
    try:
        with _QmpSession() as qmp:
            return qmp.command(cmd, args)
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("QMP %s failed: %s", cmd, exc)
        return None


def _qmp_available() -> bool:
    return _qmp_command("query-status") is not None


def _qmp_wait_ready(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _qmp_available():
            return True
        time.sleep(1.0)
    return False


def _qmp_hdd_query() -> tuple[str, set[str]] | None:
    """(block node name, snapshot tags) for ide0-hd0, or None on failure.

    Failure is never reported as an empty tag set: that would read as "this
    slot holds no state" and skip a resume that should have happened."""
    r = _qmp_command("query-block")
    if r is None:
        return None
    for dev in r.get("return", []):
        if dev.get("device") != "ide0-hd0":
            continue
        inserted = dev.get("inserted", {})
        node = inserted.get("node-name")
        if not node:
            break
        tags = {s.get("name", "") for s in inserted.get("image", {}).get("snapshots", [])}
        return node, tags
    log.error("QMP: query-block returned no usable ide0-hd0 device")
    return None


def _qmp_snapshot(cmd: str, tag: str, node: str) -> bool:
    """Run snapshot-save/-load/-delete as an async job; wait for conclusion."""
    try:
        with _QmpSession() as qmp:
            try:
                qmp.command("job-dismiss", {"id": tag})  # clear a stuck twin
            except (ValueError, TimeoutError):
                pass
            args = {"job-id": tag, "tag": tag, "devices": [node]}
            if cmd != "snapshot-delete":
                args["vmstate"] = node
            qmp.command(cmd, args)

            deadline = time.monotonic() + QMP_WAIT
            concluded = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    msg = qmp.recv_msg(timeout=remaining)
                except TimeoutError:
                    break
                if (msg.get("event") == "JOB_STATUS_CHANGE"
                        and msg.get("data", {}).get("id") == tag
                        and msg.get("data", {}).get("status") == "concluded"):
                    concluded = True
                    break

            # The wait loop leaves a near-zero timeout behind; the teardown
            # commands below need the full budget again.
            qmp.settimeout(QMP_WAIT)
            if not concluded:
                try:
                    qmp.command("job-cancel", {"id": tag})
                except (ValueError, TimeoutError):
                    pass
                raise OSError("snapshot job timed out")

            error = None
            for job in qmp.command("query-jobs").get("return", []):
                if job.get("id") == tag:
                    error = job.get("error")
                    break
            try:
                qmp.command("job-dismiss", {"id": tag})
            except (ValueError, TimeoutError):
                pass
            if error:
                raise ValueError(error)
            return True
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        log.error("QMP: %s %s failed: %s", cmd, tag, exc)
        return False


def _state_tag_seq(tag: str, slot: int) -> int | None:
    m = _STATE_TAG_RE.match(tag)
    if m is None or int(m.group(1)) != slot:
        return None
    return int(m.group(2) or 0)


def _state_tags_for(tags, slot: int) -> list[str]:
    """Every tag naming a state for `slot`, oldest first."""
    owned = [(seq, t) for t in tags if (seq := _state_tag_seq(t, slot)) is not None]
    return [t for _, t in sorted(owned)]


def _qmp_save_state(slot: int) -> bool:
    """Write slot `slot`, keeping the previous state until the new one exists.

    snapshot-save refuses a tag already on the image, so writing a slot twice
    under one name would mean deleting the old state first — and a save that
    then fails leaves the player with nothing. Each save instead goes to a
    fresh sequence and the older ones are dropped only once it landed."""
    queried = _qmp_hdd_query()
    if queried is None:
        return False
    node, tags = queried
    owned = _state_tags_for(tags, slot)
    seq = 0 if not owned else _state_tag_seq(owned[-1], slot) + 1
    tag = f"broker-slot-{slot}.{seq}"
    if not _qmp_snapshot("snapshot-save", tag, node):
        # A failed job can still leave a partial snapshot behind, and that one
        # would outrank the good state it was meant to replace.
        _qmp_snapshot("snapshot-delete", tag, node)
        return False
    log.info("QMP: snapshot saved %s", tag)
    for stale in owned:
        _qmp_snapshot("snapshot-delete", stale, node)  # superseded; ignore failure
    return True


def _qmp_load_state(slot: int) -> bool:
    queried = _qmp_hdd_query()
    if queried is None:
        return False
    node, tags = queried
    owned = _state_tags_for(tags, slot)
    if not owned:
        log.warning("QMP: slot %d holds no state on the mounted image", slot)
        return False
    return _qmp_snapshot("snapshot-load", owned[-1], node)


# ── Provider ─────────────────────────────────────────────────────────────────


def _reap_strays() -> None:
    """SIGKILL any xemu the broker does not own. An orphan busy-loops CPU
    cores, holds the QMP socket path, and — worst — keeps the qcow2 open
    while a restore parks it. Matched by full command line so other AppImage
    emulators (duckstation is also comm "AppRun") are left alone."""
    if subprocess.run(["pkill", "-9", "-f", XEMU_BIN], capture_output=True).returncode == 0:
        log.info("reaped stray xemu process(es)")
        time.sleep(0.5)


class Xemu(Emulator):
    name = "xemu"
    display_name = "xemu"
    rom_extensions = ROM_EXTENSIONS
    log_path = XEMU_LOG_PATH
    # SIGTERM gives QEMU a clean shutdown that flushes the qcow2; give a
    # large image time to land before the SIGKILL escalation tears it.
    term_timeout = float(os.environ.get("XEMU_STOP_WAIT", "15"))

    def __init__(self):
        super().__init__()
        self._launch_seq = 0
        # Resolved once per session so the whole activate/exit round trip
        # sees one image, even if xemu rewrites its config mid-session.
        self.hdd_image = _hdd_image_path()
        # Dot-prefixed so a parked image can never ship in a save dump.
        self._hdd_parked = self.hdd_image.with_name(f".{self.hdd_image.name}.prev")
        self.save_root = self.hdd_image.parent.parent
        self.save_subtrees = (self.hdd_image.parent.name,)
        log.info("xemu hdd image: %s (save scope %s/%s)",
                 self.hdd_image, self.save_root, self.save_subtrees[0])

    def _unpark_image(self) -> None:
        """Put a parked image back when no restore replaced it: an activate
        whose archive held no qcow2 must not boot into xemu's first-run
        wizard."""
        if not self.hdd_image.exists() and self._hdd_parked.is_file():
            log.info("restore shipped no HDD image; putting the previous one back")
            try:
                os.replace(self._hdd_parked, self.hdd_image)
            except OSError as exc:
                log.error("could not unpark %s: %s", self._hdd_parked, exc)

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
        """Park the live HDD image so the archived one always lands.

        The dump/restore mtime guard exists so a restore never rolls back
        newer saves, but for xemu the "newer" file is routinely a stock image
        seeded at container init — letting it win would silently discard the
        player's entire disk. QEMU must not hold the image while it moves."""
        self.stop()
        _reap_strays()
        if not self.hdd_image.is_file():
            return
        try:
            os.replace(self.hdd_image, self._hdd_parked)
            log.info("parked %s for restore", self.hdd_image.name)
        except OSError as exc:
            log.error("could not park %s, restore may be skipped as older: %s",
                      self.hdd_image, exc)

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        _reap_strays()
        self._unpark_image()
        # QEMU fails to bind if a dead socket file is left behind.
        try:
            QMP_SOCKET.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove stale QMP socket %s: %s", QMP_SOCKET, exc)

        self._launch_seq += 1
        seq = self._launch_seq

        log.info("launching xemu (rom=%s, resume_slot=%s)", rom_path, resume_slot)
        self._spawn(
            [XEMU_BIN, "-dvd_path", str(rom_path),
             "-qmp", f"unix:{QMP_SOCKET},server,nowait"],
            base_launch_env(),
        )

        if resume_slot:
            Thread(
                target=self._deferred_load_state, args=(resume_slot, seq), daemon=True
            ).start()

    def _deferred_load_state(self, slot: int, seq: int) -> None:
        """snapshot-load `slot` once QMP answers. A snapshot load replaces
        machine state wholesale, so it is safe the moment QMP is up, even
        with the guest still in early boot; what it restores is a machine
        that was already running this disc."""
        deadline = time.monotonic() + QMP_BOOT_WAIT
        while time.monotonic() < deadline:
            if self._launch_seq != seq or not self.alive():
                log.info("resume: launch superseded, slot %d load abandoned", slot)
                return
            if _qmp_available():
                if self._launch_seq != seq:
                    return
                ok = _qmp_load_state(slot)
                log.info("resume: slot %d %s", slot, "loaded" if ok else "not loaded — booted fresh")
                return
            time.sleep(1.0)
        log.warning("resume: QMP never came up, slot %d not loaded", slot)

    def save_state(self, slot: int) -> bool:
        return _qmp_save_state(slot)

    def save_and_exit(self, slot: int) -> dict:
        saved = False
        state_file = None
        if self.alive():
            saved = self.save_state(slot)
        self.stop()
        if saved and self.hdd_image.is_file():
            st = self.hdd_image.stat()
            state_file = {"path": str(self.hdd_image), "size": st.st_size, "mtime": st.st_mtime}
        return {"state_saved": saved, "state_slot": slot, "state_file": state_file}

    def stop(self) -> None:
        # Invalidate any in-flight deferred state load before the kill.
        self._launch_seq += 1
        super().stop()
