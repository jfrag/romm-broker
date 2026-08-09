"""The registry and the contract every emulator in it has to hold up."""

import json
import signal
import subprocess
import time
from pathlib import Path

import pytest

from webstation_broker import emulators
from webstation_broker.emulators import base


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


def _child_of(pid: int) -> int | None:
    """The first process reporting `pid` as its parent, or None.

    Read out of PPid rather than /proc/<pid>/task/<pid>/children, which needs a
    kernel built with CONFIG_PROC_CHILDREN and is missing on some of them."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("PPid:"):
                if int(line.split()[1]) == pid:
                    return int(entry.name)
                break
    return None


def test_reaping_kills_an_emulator_an_earlier_broker_left_running(
    pid_record, sleeper
):
    """A restarted broker has no handle on the emulator that outlived it, so
    the recorded pid is the only way it ever gets killed."""
    proc = sleeper()
    base._record_pid("fake", proc.pid, ["/usr/bin/sleep", "60"])

    killed = base.reap_orphan()

    assert killed["pid"] == proc.pid
    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert not pid_record.exists()


def test_reaping_leaves_a_recycled_pid_alone(pid_record, sleeper):
    """The pid may belong to something else entirely by now, so a record that
    does not match what is running is dropped rather than acted on."""
    proc = sleeper()
    base._record_pid("fake", proc.pid, ["/usr/bin/some-other-emulator"])

    assert base.reap_orphan() is None
    assert proc.poll() is None
    assert not pid_record.exists()


def test_reaping_leaves_a_pid_that_leads_no_process_group_alone(pid_record):
    """Emulators are spawned into their own session, so a recorded pid that is
    not a group leader is not the emulator, and killing its group would take
    down whatever unrelated process tree it belongs to."""
    parent = subprocess.Popen(["/bin/sh", "-c", "sleep 60; true"], start_new_session=True)
    try:
        deadline = time.monotonic() + 5.0
        child = None
        while child is None and time.monotonic() < deadline:
            child = _child_of(parent.pid)
            if child is None:
                time.sleep(0.1)
        assert child is not None
        base._record_pid("fake", child, base._cmdline(child))

        assert base.reap_orphan() is None
        assert parent.poll() is None
        assert not pid_record.exists()
    finally:
        parent.kill()
        parent.wait()


def test_reaping_a_record_that_names_no_command_does_nothing(pid_record, sleeper):
    """An empty cmd matches the empty cmdline every dead pid reports, so a
    record that cannot identify its process must not be acted on."""
    proc = sleeper()
    pid_record.write_text(json.dumps({"name": "fake", "pid": proc.pid}))

    assert base.reap_orphan() is None
    assert proc.poll() is None
    assert not pid_record.exists()


def test_reaping_with_nothing_recorded_does_nothing(pid_record):
    assert base.reap_orphan() is None


def test_a_graceful_exit_clears_the_record_the_same_as_a_kill(pid_record):
    """The emulators that quit over their own control channel never reach the
    kill path, so they have to drop the record themselves."""
    emu = emulators.get_emulator("shadps4")
    cmd = ["/bin/sh", "-c", "read line"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, start_new_session=True)
    emu._proc = proc
    base._record_pid("shadps4", proc.pid, cmd)

    emu.stop()

    assert proc.wait(timeout=10) == 0
    assert not pid_record.exists()


def test_stopping_clears_the_record_so_the_next_launch_reaps_nothing(
    pid_record, sleeper
):
    """A clean stop has to take the record with it: leaving it behind would
    have the next activate hunting a pid nobody owns."""
    emu = emulators.Emulator()
    emu._proc = sleeper()
    base._record_pid("fake", emu._proc.pid, ["/usr/bin/sleep", "60"])

    emu.stop()

    assert not pid_record.exists()
