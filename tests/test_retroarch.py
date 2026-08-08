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


def test_extensions_and_save_subtrees_survive_the_load_as_tuples():
    """The launcher treats extensions as an ordered preference list and the
    save logic iterates the subtrees, so neither may come back as a raw list."""
    for slug, info in retroarch.PLATFORMS.items():
        assert isinstance(info["extensions"], tuple), slug
        if "save_subtrees" in info:
            assert isinstance(info["save_subtrees"], tuple), slug
