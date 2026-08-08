"""The registry and the contract every emulator in it has to hold up."""

import pytest

from webstation_broker import emulators


def test_an_unknown_name_resolves_to_nothing():
    assert emulators.get_emulator("gameboy") is None
    assert emulators.get_emulator("") is None


def test_each_name_builds_its_own_instance():
    first = emulators.get_emulator("pcsx2")
    second = emulators.get_emulator("pcsx2")

    assert isinstance(first, emulators.Pcsx2)
    assert first is not second


@pytest.mark.parametrize("name", sorted(emulators.REGISTRY))
def test_every_emulator_declares_what_the_routes_read_off_it(name):
    emu = emulators.get_emulator(name)

    assert emu.name and emu.display_name
    assert isinstance(emu.save_subtrees, tuple)
    assert isinstance(emu.rom_extensions, tuple)
    # A state slot of 0 means the routes have nowhere to put a state, so an
    # emulator that claims states has to name one.
    if emu.supports_states:
        # The state routes read the slot's file off the emulator and validate
        # a pushed name against it, so the base no-op stubs are not enough.
        assert type(emu).state_path is not emulators.Emulator.state_path
        assert type(emu).state_target is not emulators.Emulator.state_target
        assert emu.state_slot >= 0


@pytest.mark.parametrize("name", sorted(emulators.REGISTRY))
def test_a_memory_card_comes_with_everything_the_card_routes_need(name):
    emu = emulators.get_emulator(name)
    if emu.memory_card_subtree is None:
        assert emu.memory_card_path() is None
        return

    # The card travels on its own routes, so it has to be nameable, findable,
    # and removable from the save archive.
    assert emu.memory_card_marker
    assert emu.memory_card_path() is not None
    assert emu.memory_card_subtree in emu.save_subtrees


def test_the_desktop_launcher_needs_no_rom():
    assert emulators.get_emulator("desktop").requires_rom is False
