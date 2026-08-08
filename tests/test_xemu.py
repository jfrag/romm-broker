"""xemu disc parsing, HDD image handling, and FATX save sync.

The FATX tests run against a real image built by pyfatx rather than a stub:
libfatx compares path names byte for byte, and that detail is exactly what the
inject and extract hooks have to get right.
"""

import struct
import tomllib
from pathlib import Path

import pytest
from pyfatx import Fatx

from webstation_broker.emulators import xemu

SECTOR = 2048


# ── Disc images ──────────────────────────────────────────────────────────────


def _xiso(path: Path, title_id: int = 0x4D530064, *, base: int = 0,
          xbe_name: bytes = b"default.xbe") -> Path:
    """A minimal XISO: volume descriptor, a one-entry root directory table,
    and an XBE whose certificate carries `title_id`."""
    root_sector, xbe_sector, xbe_size = 33, 34, 0x200
    image = bytearray((base + (xbe_sector + 1) * SECTOR))

    vd = bytearray(SECTOR)
    vd[: len(xemu._XISO_MAGIC)] = xemu._XISO_MAGIC
    vd[20:24] = struct.pack("<I", root_sector)
    vd[24:28] = struct.pack("<I", SECTOR)
    image[base + 32 * SECTOR : base + 33 * SECTOR] = vd

    entry = bytearray(SECTOR)
    entry[0:2] = struct.pack("<H", 0)  # left
    entry[2:4] = struct.pack("<H", 0)  # right
    entry[4:8] = struct.pack("<I", xbe_sector)
    entry[8:12] = struct.pack("<I", xbe_size)
    entry[13] = len(xbe_name)
    entry[14 : 14 + len(xbe_name)] = xbe_name
    image[base + root_sector * SECTOR : base + (root_sector + 1) * SECTOR] = entry

    xbe = bytearray(xbe_size)
    xbe[0:4] = b"XBEH"
    xbe[0x104:0x108] = struct.pack("<I", 0x10000)
    xbe[0x118:0x11C] = struct.pack("<I", 0x10100)
    xbe[0x108:0x10C] = struct.pack("<I", title_id)  # certificate title id
    off = base + xbe_sector * SECTOR
    image[off : off + xbe_size] = xbe

    path.write_bytes(bytes(image))
    return path


def test_title_id_is_read_from_the_xbe_certificate(tmp_path):
    assert xemu._disc_title_id(_xiso(tmp_path / "g.iso")) == "4D530064"


def test_title_id_is_uppercase_like_the_dashboard_writes_it(tmp_path):
    """The dashboard creates E:/UDATA/<id> in uppercase, and a directory this
    code creates has to be named the same way."""
    assert xemu._disc_title_id(_xiso(tmp_path / "g.iso", 0x0000ABCD)) == "0000ABCD"


def test_title_id_reads_a_disc_with_a_video_partition_up_front(tmp_path):
    disc = _xiso(tmp_path / "g.iso", base=0x18300000)
    assert xemu._disc_title_id(disc) == "4D530064"


def test_title_id_is_none_when_the_disc_has_no_volume_descriptor(tmp_path):
    junk = tmp_path / "junk.iso"
    junk.write_bytes(b"\x00" * (40 * SECTOR))
    assert xemu._disc_title_id(junk) is None


def test_title_id_is_none_when_the_root_holds_no_default_xbe(tmp_path):
    disc = _xiso(tmp_path / "g.iso", xbe_name=b"nothere.xbe")
    assert xemu._disc_title_id(disc) is None


def test_title_id_is_none_for_a_missing_file(tmp_path):
    assert xemu._disc_title_id(tmp_path / "gone.iso") is None


# ── ROM resolution ───────────────────────────────────────────────────────────


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(xemu, "ROM_ROOT", root)
    return root


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Game.iso", 1),
        ("Game (Disc 2).iso", 2),
        ("Game.disk3.iso", 3),
        ("Game_cd-4.iso", 4),
    ],
)
def test_disc_number_is_read_from_the_name(name, expected):
    assert xemu._disc_number(Path(name)) == expected


def test_a_disc_set_boots_disc_one(rom_root):
    folder = rom_root / "Game"
    folder.mkdir()
    for name in ("Game (Disc 2).iso", "Game (Disc 1).iso"):
        (folder / name).write_bytes(b"x")
    assert xemu.Xemu.resolve_rom_file(None, folder).name == "Game (Disc 1).iso"


def test_a_file_path_resolves_to_itself(rom_root):
    disc = rom_root / "Game.iso"
    disc.write_bytes(b"x")
    assert xemu.Xemu.resolve_rom_file(None, disc) == disc


def test_non_iso_files_are_not_bootable(rom_root):
    folder = rom_root / "Game"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"x")
    assert xemu.Xemu.resolve_rom_file(None, folder) is None


def test_a_symlink_out_of_the_rom_root_is_rejected(rom_root, tmp_path):
    outside = tmp_path / "elsewhere.iso"
    outside.write_bytes(b"x")
    folder = rom_root / "Game"
    folder.mkdir()
    (folder / "Game.iso").symlink_to(outside)
    assert xemu.Xemu.resolve_rom_file(None, folder) is None


# ── HDD image location ───────────────────────────────────────────────────────


def _toml(tmp_path: Path, hdd_path: str) -> Path:
    cfg = tmp_path / "xemu.toml"
    cfg.write_text(f'[sys.files]\nhdd_path = "{hdd_path}"\n')
    return cfg


def test_the_hdd_path_comes_from_the_user_config(monkeypatch, tmp_path):
    monkeypatch.setattr(xemu, "XEMU_TOML", _toml(tmp_path, "/config/xemu/mine.qcow2"))
    assert xemu._hdd_image_path() == Path("/config/xemu/mine.qcow2")


@pytest.mark.parametrize("hdd_path", ["", "relative.qcow2", "/atroot.qcow2"])
def test_an_unusable_hdd_path_falls_back(monkeypatch, tmp_path, hdd_path):
    monkeypatch.setattr(xemu, "XEMU_TOML", _toml(tmp_path, hdd_path))
    assert xemu._hdd_image_path() == xemu.FALLBACK_HDD_IMAGE


def test_a_missing_config_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(xemu, "XEMU_TOML", tmp_path / "nope.toml")
    assert xemu._hdd_image_path() == xemu.FALLBACK_HDD_IMAGE


def test_an_unparseable_config_falls_back(monkeypatch, tmp_path):
    cfg = tmp_path / "xemu.toml"
    cfg.write_text("[sys.files\nhdd_path =")
    monkeypatch.setattr(xemu, "XEMU_TOML", cfg)
    assert xemu._hdd_image_path() == xemu.FALLBACK_HDD_IMAGE


# ── One-time raw conversion ──────────────────────────────────────────────────


def test_a_raw_image_is_left_alone(tmp_path):
    image = tmp_path / "hdd.qcow2"
    image.write_bytes(b"not a qcow2 header")
    assert xemu._ensure_raw_image(image) is True
    assert image.read_bytes() == b"not a qcow2 header"
    assert not image.with_name("hdd.qcow2.backup").exists()


def test_a_missing_image_is_not_convertible(tmp_path):
    assert xemu._ensure_raw_image(tmp_path / "gone.qcow2") is False


def test_a_qcow2_is_converted_in_place_and_backed_up(tmp_path, monkeypatch):
    image = tmp_path / "hdd.qcow2"
    image.write_bytes(xemu.QCOW2_MAGIC + b"rest of the qcow2")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"raw content")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(xemu.subprocess, "run", fake_run)
    assert xemu._ensure_raw_image(image) is True
    assert image.read_bytes() == b"raw content"
    assert image.with_name("hdd.qcow2.backup").read_bytes().startswith(xemu.QCOW2_MAGIC)


def test_a_failed_conversion_leaves_the_qcow2_playable(tmp_path, monkeypatch):
    """Losing save sync is survivable; losing the disk is not."""
    image = tmp_path / "hdd.qcow2"
    original = xemu.QCOW2_MAGIC + b"rest of the qcow2"
    image.write_bytes(original)

    def fake_run(cmd, **kwargs):
        raise xemu.subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(xemu.subprocess, "run", fake_run)
    assert xemu._ensure_raw_image(image) is False
    assert image.read_bytes() == original


# ── Renderer pin ─────────────────────────────────────────────────────────────


# A config shaped like the one xemu writes: comments, several tables, and a
# [display.quality] subtable right after the [display] body the pin edits.
FULL_TOML = """\
[general]
show_welcome = false

[display]
renderer = 'VULKAN'
# how the guest frame is fitted to the window
ui_scale = 2

[display.quality]
surface_scale = 1

[sys.files]
hdd_path = '/config/xemu/xbox_hdd.qcow2'
"""


@pytest.fixture
def pinned(monkeypatch, tmp_path):
    """Point the pin at a throwaway config and default it to OpenGL."""
    cfg = tmp_path / "xemu.toml"
    monkeypatch.setattr(xemu, "XEMU_TOML", cfg)
    monkeypatch.setattr(xemu, "XEMU_RENDERER", "OPENGL")
    return cfg


def _renderer_of(cfg: Path) -> str:
    return tomllib.loads(cfg.read_text())["display"]["renderer"]


def test_a_vulkan_config_is_pinned_back_to_opengl(pinned):
    pinned.write_text(FULL_TOML)
    xemu._pin_renderer()
    assert _renderer_of(pinned) == "OPENGL"


def test_pinning_leaves_the_rest_of_the_config_alone(pinned):
    """The file belongs to xemu; the pin owns exactly one key in it."""
    pinned.write_text(FULL_TOML)
    xemu._pin_renderer()
    after = pinned.read_text()
    assert after == FULL_TOML.replace("renderer = 'VULKAN'", "renderer = 'OPENGL'")
    assert "# how the guest frame is fitted to the window" in after
    assert tomllib.loads(after)["display"]["quality"]["surface_scale"] == 1


def test_a_display_section_without_a_renderer_gains_one(pinned):
    pinned.write_text("[display]\nui_scale = 2\n\n[sys]\nmem = 64\n")
    xemu._pin_renderer()
    assert _renderer_of(pinned) == "OPENGL"
    assert tomllib.loads(pinned.read_text())["sys"]["mem"] == 64


def test_a_config_with_no_display_section_gains_one(pinned):
    pinned.write_text("[general]\nshow_welcome = false\n")
    xemu._pin_renderer()
    assert _renderer_of(pinned) == "OPENGL"


def test_the_renderer_is_configurable_for_hardware_where_vulkan_works(pinned, monkeypatch):
    monkeypatch.setattr(xemu, "XEMU_RENDERER", "VULKAN")
    pinned.write_text("[display]\nrenderer = 'OPENGL'\n")
    xemu._pin_renderer()
    assert _renderer_of(pinned) == "VULKAN"


@pytest.mark.parametrize("setting", ["KEEP", ""])
def test_the_pin_can_be_turned_off(pinned, monkeypatch, setting):
    monkeypatch.setattr(xemu, "XEMU_RENDERER", setting)
    pinned.write_text(FULL_TOML)
    xemu._pin_renderer()
    assert pinned.read_text() == FULL_TOML


def test_a_missing_config_is_not_created(pinned):
    """xemu writes the file itself, and its own default is already OpenGL."""
    xemu._pin_renderer()
    assert not pinned.exists()


def test_an_unwritable_config_does_not_stop_the_launch(pinned, monkeypatch):
    pinned.write_text(FULL_TOML)

    def fail(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", fail)
    xemu._pin_renderer()
    assert _renderer_of(pinned) == "VULKAN"


def test_launch_pins_the_renderer_before_spawning(emulator, monkeypatch, tmp_path):
    """The pin is worthless if a launch can get past it."""
    cfg = xemu.XEMU_TOML
    cfg.write_text(cfg.read_text() + "\n[display]\nrenderer = 'VULKAN'\n")
    monkeypatch.setattr(xemu, "XEMU_RENDERER", "OPENGL")
    monkeypatch.setattr(xemu, "_reap_strays", lambda: None)
    monkeypatch.setattr(xemu.Xemu, "stop", lambda self: None)

    spawned: list[list[str]] = []
    monkeypatch.setattr(xemu.Xemu, "_spawn",
                        lambda self, cmd, env: spawned.append(cmd))

    emulator.launch(_xiso(tmp_path / "g.iso"), None)
    assert spawned, "launch did not spawn xemu"
    assert _renderer_of(cfg) == "OPENGL"


# ── FATX save sync ───────────────────────────────────────────────────────────


@pytest.fixture
def emulator(monkeypatch, tmp_path):
    """An Xemu whose HDD image is a real, freshly formatted FATX disk."""
    image = tmp_path / "xbox_hdd.qcow2"
    Fatx.create(str(image))
    monkeypatch.setattr(xemu, "XEMU_TOML", _toml(tmp_path, str(image)))
    em = xemu.Xemu()
    em.staging_dir.mkdir(parents=True, exist_ok=True)
    return em


def _fatx(image: Path) -> Fatx:
    return Fatx(str(image), drive="e")


def _seed(image: Path, path: str, data: bytes) -> None:
    fs = _fatx(image)
    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        d = "/" + "/".join(parts[:i])
        if not xemu._fatx_isdir(fs, d):
            fs.mkdir(d)
    fs.write("/" + "/".join(parts), data)
    del fs


def _stage(em, rel: str, data: bytes) -> None:
    p = em.staging_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_extract_pulls_only_the_launched_title(emulator):
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"mine")
    _seed(emulator.hdd_image, "/UDATA/DEADBEEF/saved.dat", b"someone else")
    emulator._title_id = "4D530064"

    assert emulator._extract_saves() == 1
    assert (emulator.staging_dir / "UDATA/4D530064/saved.dat").read_bytes() == b"mine"
    assert not (emulator.staging_dir / "UDATA/DEADBEEF").exists()


def test_extract_matches_a_save_directory_whatever_its_case(emulator):
    """Regression: the title id used to be formatted lowercase and looked up
    literally, so nothing resolved and every session's saves were lost with
    no error."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"mine")
    emulator._title_id = "4d530064"

    assert emulator._extract_saves() == 1
    assert (emulator.staging_dir / "UDATA/4D530064/saved.dat").read_bytes() == b"mine"


def test_extract_covers_both_udata_and_tdata(emulator):
    _seed(emulator.hdd_image, "/UDATA/4D530064/a.dat", b"u")
    _seed(emulator.hdd_image, "/TDATA/4D530064/b.dat", b"t")
    emulator._title_id = "4D530064"

    assert emulator._extract_saves() == 2


def test_extract_without_a_title_id_takes_every_title(emulator):
    _seed(emulator.hdd_image, "/UDATA/4D530064/a.dat", b"one")
    _seed(emulator.hdd_image, "/UDATA/DEADBEEF/b.dat", b"two")
    emulator._title_id = None

    assert emulator._extract_saves() == 2


def test_extract_is_empty_when_the_title_never_saved(emulator):
    _seed(emulator.hdd_image, "/UDATA/DEADBEEF/a.dat", b"two")
    emulator._title_id = "4D530064"

    assert emulator._extract_saves() == 0


def test_inject_writes_staged_files_into_the_image(emulator):
    _stage(emulator, "UDATA/4D530064/saved.dat", b"restored")

    assert emulator._inject_saves() == 1
    fs = _fatx(emulator.hdd_image)
    assert bytes(fs.read("/UDATA/4D530064/saved.dat")) == b"restored"


def test_inject_lands_in_the_existing_directory_whatever_its_case(emulator):
    """An archive captured elsewhere can carry a different case than the disk
    holds; creating a twin directory beside the real one would hide the save
    from the game."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/old.dat", b"old")
    _stage(emulator, "udata/4d530064/saved.dat", b"restored")

    assert emulator._inject_saves() == 1
    fs = _fatx(emulator.hdd_image)
    assert bytes(fs.read("/UDATA/4D530064/saved.dat")) == b"restored"
    assert [a.filename for a in fs.listdir("/")] == ["UDATA"]


def test_inject_truncates_a_file_that_shrank(emulator):
    """pyfatx write() never shortens an existing file, so a smaller save has
    to be truncated or it lands with the old tail still attached."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"a much longer save")
    _stage(emulator, "UDATA/4D530064/saved.dat", b"short")

    assert emulator._inject_saves() == 1
    fs = _fatx(emulator.hdd_image)
    assert bytes(fs.read("/UDATA/4D530064/saved.dat")) == b"short"


def test_inject_skips_dotfiles(emulator):
    _stage(emulator, "UDATA/4D530064/.DS_Store", b"junk")
    assert emulator._inject_saves() == 0


def test_inject_is_a_no_op_with_nothing_staged(emulator):
    assert emulator._inject_saves() == 0


def test_a_save_round_trips_through_the_image(emulator):
    """The whole point: what exit extracts is what the next activate injects."""
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"progress")
    emulator._title_id = "4D530064"
    assert emulator._extract_saves() == 1

    fs = _fatx(emulator.hdd_image)
    fs.write("/UDATA/4D530064/saved.dat", b"WIPED!!!")
    del fs

    assert emulator._inject_saves() == 1
    fs = _fatx(emulator.hdd_image)
    assert bytes(fs.read("/UDATA/4D530064/saved.dat")) == b"progress"


# ── Session contract ─────────────────────────────────────────────────────────


def test_xemu_reports_no_save_state_support():
    """A raw image cannot hold QEMU internal snapshots, so the parent has to
    read the absence off the status response rather than assume it."""
    assert xemu.Xemu.supports_states is False


def test_save_and_exit_reports_saves_rather_than_a_state(emulator, monkeypatch):
    _seed(emulator.hdd_image, "/UDATA/4D530064/saved.dat", b"progress")
    emulator._title_id = "4D530064"
    monkeypatch.setattr(xemu, "_reap_strays", lambda: None)
    monkeypatch.setattr(emulator, "stop", lambda: None)

    result = emulator.save_and_exit(1)
    assert result["state_saved"] is None
    assert result["state_slot"] is None
    assert result["title_id"] == "4D530064"
    assert result["saves_extracted"] == 1
