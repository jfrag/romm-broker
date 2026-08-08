"""RetroArch's platform table: the map that decides which core a claim loads."""

import json

import pytest

from webstation_broker.emulators import retroarch


def test_the_table_is_the_one_on_disk():
    """The map used to be duplicated inline, and the copy silently shadowed the
    file, so every platform added to the file did nothing."""
    on_disk = json.loads(retroarch._PLATFORMS_FILE.read_text())

    assert set(retroarch.PLATFORMS) == set(on_disk)


@pytest.mark.parametrize("slug", ["psp", "nes", "gba", "n64", "snes", "genesis", "dc"])
def test_the_common_platforms_are_mapped(slug):
    info = retroarch._platform_info(slug)

    assert info is not None
    assert info["core"]
    assert info["extensions"]


def test_psp_boots_on_the_ppsspp_core():
    info = retroarch._platform_info("psp")

    assert info["core"] == "ppsspp"
    assert ".iso" in info["extensions"] and ".cso" in info["extensions"]


def test_a_platform_slug_is_matched_case_insensitively():
    assert retroarch._platform_info("PSP") == retroarch._platform_info("psp")


def test_an_unmapped_platform_has_no_core():
    assert retroarch._platform_info("ps2") is None
    assert retroarch._platform_info(None) is None


@pytest.mark.parametrize("slug", ["ngc", "wii"])
def test_the_dolphin_core_keeps_state_thumbnails_off(slug):
    """It renders on the GPU, and the framebuffer grab after a save deadlocks
    RetroArch's runloop, taking the command channel down with it."""
    assert retroarch._platform_info(slug)["thumbnail"] is False


def test_psp_declares_where_the_core_finds_its_assets():
    """The ppsspp core will not boot without PPSSPP's own asset tree, which the
    buildbot .so does not carry."""
    assets = retroarch._platform_info("psp")["assets"]

    assert assets["PPSSPP/assets"].endswith("/assets")


class TestCoreAssets:
    def test_a_declared_source_is_linked_into_the_system_dir(self, tmp_path, monkeypatch):
        source = tmp_path / "share" / "ppsspp" / "assets"
        source.mkdir(parents=True)
        (source / "ppge_atlas.zim").write_bytes(b"atlas")
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({"PPSSPP/assets": str(source)})

        linked = system / "PPSSPP" / "assets"
        assert linked.is_symlink()
        assert (linked / "ppge_atlas.zim").read_bytes() == b"atlas"

    def test_linking_twice_is_a_no_op(self, tmp_path, monkeypatch):
        source = tmp_path / "assets"
        source.mkdir()
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)
        assets = {"PPSSPP/assets": str(source)}

        retroarch._ensure_core_assets(assets)
        retroarch._ensure_core_assets(assets)

        assert (system / "PPSSPP" / "assets").readlink() == source

    def test_a_stale_link_is_repointed(self, tmp_path, monkeypatch):
        old = tmp_path / "old"
        old.mkdir()
        new = tmp_path / "new"
        new.mkdir()
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({"PPSSPP/assets": str(old)})
        retroarch._ensure_core_assets({"PPSSPP/assets": str(new)})

        assert (system / "PPSSPP" / "assets").readlink() == new

    def test_a_real_directory_already_there_is_left_alone(self, tmp_path, monkeypatch):
        """A user who installed the assets by hand keeps them."""
        source = tmp_path / "assets"
        source.mkdir()
        system = tmp_path / "system"
        theirs = system / "PPSSPP" / "assets"
        theirs.mkdir(parents=True)
        (theirs / "theirs.zim").write_bytes(b"mine")
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({"PPSSPP/assets": str(source)})

        assert not theirs.is_symlink()
        assert (theirs / "theirs.zim").exists()

    def test_a_missing_source_does_not_raise(self, tmp_path, monkeypatch):
        """The core's own complaint about the missing asset is the better error."""
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({"PPSSPP/assets": str(tmp_path / "nope")})

        assert not (system / "PPSSPP" / "assets").exists()

    def test_a_platform_with_no_assets_touches_nothing(self, tmp_path, monkeypatch):
        system = tmp_path / "system"
        monkeypatch.setattr(retroarch, "SYSTEM_DIR", system)

        retroarch._ensure_core_assets({})

        assert not system.exists()


def test_extensions_and_save_subtrees_survive_the_load_as_tuples():
    """The launcher treats extensions as an ordered preference list and the
    save logic iterates the subtrees, so neither may come back as a raw list."""
    for slug, info in retroarch.PLATFORMS.items():
        assert isinstance(info["extensions"], tuple), slug
        if "save_subtrees" in info:
            assert isinstance(info["save_subtrees"], tuple), slug
