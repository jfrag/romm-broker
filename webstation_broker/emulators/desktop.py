"""Administration session: launches the full webstation desktop
(selkies-desktop) so the user can configure emulators through the GUI.
Managed like any emulator session, just with no ROM and no save sync."""

import os
from pathlib import Path

from .base import Emulator, base_launch_env


class Desktop(Emulator):
    name = "desktop"
    display_name = "Webstation Desktop"
    requires_rom = False
    save_root = Path("/config")
    save_subtrees = ()
    log_path = Path("/config/selkies-desktop.log")

    def launch(self, rom_path: Path | None, resume_slot: int | None) -> None:
        self.stop()
        binary = os.environ.get("DESKTOP_BIN", "selkies-desktop")
        self._spawn([binary], base_launch_env())
