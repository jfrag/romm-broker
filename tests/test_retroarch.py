"""RetroArch's platform table: the map that decides which core a claim loads."""

import json
import logging
import threading

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
    buildbot .so does not carry. It reads the files straight out of PPSSPP/, so
    linking the tree one level deeper hides them from it."""
    assets = retroarch._platform_info("psp")["assets"]

    assert assets["PPSSPP"].endswith("/assets")


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


class TestBrokerConfig:
    """The per-launch overlay written on top of the user's own retroarch.cfg."""

    @pytest.fixture(autouse=True)
    def _dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(retroarch, "RA_DATA_DIR", tmp_path)
        monkeypatch.setattr(retroarch, "STATE_DIR", tmp_path / "states")
        monkeypatch.setattr(retroarch, "SAVE_DIR", tmp_path / "saves")
        monkeypatch.setattr(retroarch, "BROKER_CFG", tmp_path / "broker.cfg")

    def test_the_joypad_driver_is_pinned_off_udev(self):
        """The Selkies pads all look like one device to udev, so it registers
        none of them; linuxraw opens the js nodes the interposer hooks."""
        cfg = retroarch._write_broker_cfg().read_text()

        assert 'input_joypad_driver = "linuxraw"' in cfg

    def test_an_empty_driver_leaves_the_user_config_alone(self, monkeypatch):
        monkeypatch.setattr(retroarch, "JOYPAD_DRIVER", "")

        assert "input_joypad_driver" not in retroarch._write_broker_cfg().read_text()

    def test_the_driver_is_overridable(self, monkeypatch):
        monkeypatch.setattr(retroarch, "JOYPAD_DRIVER", "sdl2")

        assert 'input_joypad_driver = "sdl2"' in retroarch._write_broker_cfg().read_text()

    def test_the_stdin_channel_and_save_dirs_are_still_there(self):
        """The overlay is what makes the session controllable at all."""
        cfg = retroarch._write_broker_cfg().read_text()

        assert 'stdin_cmd_enable = "true"' in cfg
        assert f'savestate_directory = "{retroarch.STATE_DIR}"' in cfg
        assert f'savefile_directory = "{retroarch.SAVE_DIR}"' in cfg

    @pytest.mark.parametrize("thumbnail,expected", [(True, "true"), (False, "false")])
    def test_thumbnails_follow_the_platform(self, thumbnail, expected):
        cfg = retroarch._write_broker_cfg(thumbnail).read_text()

        assert f'savestate_thumbnail_enable = "{expected}"' in cfg


def test_extensions_and_save_subtrees_survive_the_load_as_tuples():
    """The launcher treats extensions as an ordered preference list and the
    save logic iterates the subtrees, so neither may come back as a raw list."""
    for slug, info in retroarch.PLATFORMS.items():
        assert isinstance(info["extensions"], tuple), slug
        if "save_subtrees" in info:
            assert isinstance(info["save_subtrees"], tuple), slug


class TestResumeGate:
    """Slot 0 is this broker's only working slot, so a launch asking to resume
    it must still start the deferred load."""

    @pytest.fixture(autouse=True)
    def _stub_launch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(retroarch, "_ensure_core", lambda name: tmp_path / f"{name}.so")
        monkeypatch.setattr(retroarch, "_ensure_core_assets", lambda assets: None)
        monkeypatch.setattr(retroarch, "_write_broker_cfg", lambda *a: tmp_path / "broker.cfg")
        monkeypatch.setattr(retroarch.shutil, "which", lambda binary: "/usr/bin/retroarch")
        monkeypatch.setattr(retroarch.Retroarch, "stop", lambda self: None)
        monkeypatch.setattr(retroarch.Retroarch, "_spawn_ra", lambda self, cmd, env: None)

        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=False):
                self.args = args

            def start(self):
                started.append(self.args)

        monkeypatch.setattr(retroarch.threading, "Thread", FakeThread)
        return started

    def _launch(self, tmp_path, resume_slot):
        emu = retroarch.Retroarch()
        emu.platform = "snes"
        emu.launch(tmp_path / "game.sfc", resume_slot)
        return emu

    def test_slot_zero_still_defers_a_load(self, tmp_path, _stub_launch):
        self._launch(tmp_path, 0)

        assert [args[0] for args in _stub_launch] == [0]

    def test_a_nonzero_slot_defers_a_load(self, tmp_path, _stub_launch):
        self._launch(tmp_path, 3)

        assert [args[0] for args in _stub_launch] == [3]

    def test_no_resume_request_defers_nothing(self, tmp_path, _stub_launch):
        self._launch(tmp_path, None)

        assert _stub_launch == []

    def test_launching_a_playlist_records_it_and_starts_on_disc_zero(self, tmp_path):
        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("Game (Disc 1).chd\n")

        emulator.launch(playlist, None)

        assert emulator._playlist == playlist
        assert emulator._disc_index == 0

    def test_launching_a_bare_disc_records_no_playlist(self, tmp_path):
        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        disc = tmp_path / "Game.chd"
        disc.write_bytes(b"x")

        emulator.launch(disc, None)

        assert emulator._playlist is None


class TestPlaylistPreference:
    """A folder holding a playlist and its discs boots the playlist, because
    the disc-swap commands step through playlist entries."""

    @pytest.mark.parametrize(
        "platform", ["dc", "saturn", "segacd", "turbografx-cd", "dos"]
    )
    def test_m3u_is_the_first_choice_on_every_disc_platform(self, platform):
        info = retroarch._platform_info(platform)
        assert info is not None
        assert info["extensions"][0] == ".m3u"

    def test_a_folder_with_a_playlist_and_discs_picks_the_playlist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(retroarch, "ROM_ROOT", tmp_path)
        game = tmp_path / "Game"
        game.mkdir()
        (game / "Game.m3u").write_text("Game (Disc 1).chd\nGame (Disc 2).chd\n")
        (game / "Game (Disc 1).chd").write_bytes(b"1")
        (game / "Game (Disc 2).chd").write_bytes(b"2")

        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        assert emulator.resolve_rom_file(game) == (game / "Game.m3u").resolve()


class TestPlaylistHelpers:
    """Reading a .m3u the way RetroArch does: one relative path per line,
    comments and blanks skipped, order is the disc order."""

    def test_entries_resolve_against_the_playlist_directory(self, tmp_path):
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("Game (Disc 1).chd\nGame (Disc 2).chd\n")
        assert retroarch._m3u_entries(playlist) == [
            (tmp_path / "Game (Disc 1).chd").resolve(),
            (tmp_path / "Game (Disc 2).chd").resolve(),
        ]

    def test_comments_and_blank_lines_are_skipped(self, tmp_path):
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("# a comment\n\nGame (Disc 1).chd\n\n")
        assert retroarch._m3u_entries(playlist) == [
            (tmp_path / "Game (Disc 1).chd").resolve()
        ]

    def test_an_unreadable_playlist_yields_no_entries(self, tmp_path):
        assert retroarch._m3u_entries(tmp_path / "missing.m3u") == []

    def test_an_unreadable_playlist_logs_a_warning(self, tmp_path, caplog):
        missing = tmp_path / "missing.m3u"
        with caplog.at_level(logging.WARNING):
            retroarch._m3u_entries(missing)
        assert str(missing) in caplog.text

    def test_index_finds_the_matching_entry(self, tmp_path):
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("Game (Disc 1).chd\nGame (Disc 2).chd\n")
        target = tmp_path / "Game (Disc 2).chd"
        assert retroarch._m3u_index_for_path(playlist, target) == 1

    def test_index_is_none_for_a_disc_the_playlist_does_not_list(self, tmp_path):
        playlist = tmp_path / "Game.m3u"
        playlist.write_text("Game (Disc 1).chd\n")
        assert retroarch._m3u_index_for_path(playlist, tmp_path / "Other.chd") is None


class TestSwapDisc:
    """Driving the tray over the command protocol."""

    @pytest.fixture
    def emulator(self, tmp_path, monkeypatch):
        """A Retroarch that looks alive and records commands instead of
        writing them to a process."""
        monkeypatch.setattr(retroarch, "DISC_TRAY_SETTLE", 0)
        monkeypatch.setattr(retroarch, "DISC_STEP_DELAY", 0)
        emulator = retroarch.Retroarch()
        emulator.platform = "dc"
        emulator.sent = []

        def fake_send(cmd, wait_prefix=None, timeout=5.0):
            emulator.sent.append(cmd)
            if cmd == "GET_STATUS":
                return "GET_STATUS PLAYING dc,Game,0"
            return None

        monkeypatch.setattr(emulator, "_send", fake_send)
        monkeypatch.setattr(emulator, "alive", lambda: True)

        playlist = tmp_path / "Game.m3u"
        playlist.write_text(
            "Game (Disc 1).chd\nGame (Disc 2).chd\nGame (Disc 3).chd\n"
        )
        emulator._playlist = playlist
        emulator._disc_index = 0
        emulator.tmp_path = tmp_path
        return emulator

    def test_swapping_forward_ejects_steps_and_closes(self, emulator):
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is True
        assert emulator.sent == [
            "GET_STATUS",
            "DISK_EJECT_TOGGLE",
            "DISK_NEXT",
            "DISK_EJECT_TOGGLE",
        ]
        assert emulator._disc_index == 1

    def test_swapping_backward_wraps_around_the_playlist(self, emulator):
        emulator._disc_index = 2
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is True
        # From index 2 to index 1 is two forward steps through a 3-disc list.
        assert emulator.sent.count("DISK_NEXT") == 2
        assert emulator._disc_index == 1

    def test_swapping_to_the_mounted_disc_leaves_the_tray_alone(self, emulator):
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 1).chd") is True
        assert "DISK_EJECT_TOGGLE" not in emulator.sent

    def test_a_disc_outside_the_playlist_is_refused(self, emulator):
        assert emulator.swap_disc(emulator.tmp_path / "Other.chd") is False
        assert "DISK_EJECT_TOGGLE" not in emulator.sent

    def test_a_session_with_no_playlist_cannot_swap(self, emulator):
        emulator._playlist = None
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False

    def test_a_core_that_never_reports_playing_is_refused(self, emulator, monkeypatch):
        monkeypatch.setattr(retroarch, "DISC_SWAP_WAIT", 0)
        monkeypatch.setattr(emulator, "_send", lambda *a, **k: None)
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False

    def test_a_core_that_dies_mid_swap_does_not_commit_the_index(self, emulator, monkeypatch):
        """The tray commands after PLAYING have no reply to confirm delivery,
        so a death partway through must not leave the tracked index pointing
        at a disc that was never actually mounted."""
        state = {"alive": True}
        monkeypatch.setattr(emulator, "alive", lambda: state["alive"])

        def fake_send(cmd, wait_prefix=None, timeout=5.0):
            emulator.sent.append(cmd)
            if cmd == "GET_STATUS":
                return "GET_STATUS PLAYING dc,Game,0"
            if cmd == "DISK_NEXT":
                state["alive"] = False
            return None

        monkeypatch.setattr(emulator, "_send", fake_send)
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        assert emulator._disc_index == 0

    def test_a_relaunch_during_the_wait_does_not_clobber_the_new_sessions_index(
        self, emulator, monkeypatch
    ):
        """A swap outliving the session it was issued for must not stomp the
        disc index a fresh launch() already reset, the same hazard
        _deferred_load_state guards against with _launch_seq."""

        def fake_send(cmd, wait_prefix=None, timeout=5.0):
            emulator.sent.append(cmd)
            if cmd == "GET_STATUS":
                # A relaunch races the wait and wins: a new session starts and
                # resets tracking exactly as Retroarch.launch() does.
                emulator._launch_seq += 1
                emulator._disc_index = 0
                return "GET_STATUS PLAYING dc,Game,0"
            return None

        monkeypatch.setattr(emulator, "_send", fake_send)
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        assert emulator._disc_index == 0

    def test_the_class_advertises_disc_swap(self):
        assert retroarch.Retroarch.supports_disc_swap is True

    def test_no_playlist_and_a_dead_core_are_reported_differently(
        self, emulator, monkeypatch, caplog
    ):
        """A dead core is a different failure than an unloaded playlist, and
        reporting "no playlist" for a dead core is actively misleading."""
        playlist = emulator._playlist

        with caplog.at_level(logging.WARNING):
            emulator._playlist = None
            assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        no_playlist_message = caplog.text
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            emulator._playlist = playlist
            monkeypatch.setattr(emulator, "alive", lambda: False)
            assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        dead_core_message = caplog.text

        assert no_playlist_message != dead_core_message
        assert "playlist" in no_playlist_message.lower()
        assert "playlist" not in dead_core_message.lower()

    def test_a_relaunch_during_the_tray_settle_aborts_before_the_next_command(
        self, emulator, monkeypatch
    ):
        """A relaunch landing right after the eject must not let the sequence
        keep sending commands (DISK_NEXT, the closing eject) to the new
        process's stdin, which reads self._proc live."""

        def fake_send(cmd, wait_prefix=None, timeout=5.0):
            emulator.sent.append(cmd)
            if cmd == "GET_STATUS":
                return "GET_STATUS PLAYING dc,Game,0"
            if cmd == "DISK_EJECT_TOGGLE":
                emulator._launch_seq += 1
            return None

        monkeypatch.setattr(emulator, "_send", fake_send)
        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        assert emulator.sent == ["GET_STATUS", "DISK_EJECT_TOGGLE"]
        assert emulator._disc_index == 0

    def test_a_second_concurrent_swap_is_refused(self, emulator):
        """A swap already holding the tray lock must make a second swap fail
        fast rather than queue up behind the multi-second sequence."""
        emulator._disc_lock.acquire()
        try:
            assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False
        finally:
            emulator._disc_lock.release()
        assert emulator.sent == []

    def test_concurrent_swaps_do_not_interleave_the_tray_sequence(
        self, emulator, monkeypatch
    ):
        """The loser of the race must fail before touching the tray, so the
        winner's EJECT / NEXT / EJECT sequence is never split up by another
        thread's commands landing in the middle of it."""
        entered_sequence = threading.Event()
        release_winner = threading.Event()

        def fake_send(cmd, wait_prefix=None, timeout=5.0):
            emulator.sent.append(cmd)
            if cmd == "GET_STATUS":
                return "GET_STATUS PLAYING dc,Game,0"
            if cmd == "DISK_EJECT_TOGGLE" and emulator.sent.count("DISK_EJECT_TOGGLE") == 1:
                # Mid-sequence: let the loser attempt its own swap here.
                entered_sequence.set()
                release_winner.wait(timeout=2)
            return None

        monkeypatch.setattr(emulator, "_send", fake_send)

        results = {}

        def winner():
            results["winner"] = emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd")

        t = threading.Thread(target=winner)
        t.start()
        assert entered_sequence.wait(timeout=2)

        results["loser"] = emulator.swap_disc(emulator.tmp_path / "Game (Disc 3).chd")
        release_winner.set()
        t.join(timeout=2)

        assert results["winner"] is True
        assert results["loser"] is False
        # The loser's refusal injected nothing: the recorded sequence is
        # exactly the winner's, uninterrupted.
        assert emulator.sent == [
            "GET_STATUS",
            "DISK_EJECT_TOGGLE",
            "DISK_NEXT",
            "DISK_EJECT_TOGGLE",
        ]
        assert emulator._disc_index == 1

    def test_a_swap_is_refused_while_a_deferred_resume_holds_the_lock(
        self, emulator, monkeypatch
    ):
        """The tray lock also excludes _deferred_load_state: a LOAD_STATE
        landing inside a swap's tray-settle window is the collision it
        guards against, and the same holds in reverse."""
        monkeypatch.setattr(retroarch, "RESUME_LOAD_SETTLE", 0)
        entered_lock = threading.Event()
        release_resume = threading.Event()

        def fake_wait_for_state(deadline):
            entered_lock.set()
            release_resume.wait(timeout=2)
            return True

        monkeypatch.setattr(emulator, "wait_for_state", fake_wait_for_state)
        monkeypatch.setattr(emulator, "load_state", lambda slot: True)

        t = threading.Thread(
            target=emulator._deferred_load_state, args=(0, emulator._launch_seq)
        )
        t.start()
        assert entered_lock.wait(timeout=2)

        assert emulator.swap_disc(emulator.tmp_path / "Game (Disc 2).chd") is False

        release_resume.set()
        t.join(timeout=2)
