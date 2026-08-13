from .azahar import Azahar
from .base import Emulator
from .cemu import Cemu
from .desktop import Desktop
from .dolphin import Dolphin
from .duckstation import Duckstation
from .eden import Eden
from .pcsx2 import Pcsx2
from .retroarch import Retroarch
from .rpcs3 import Rpcs3
from .shadps4 import Shadps4
from .xemu import Xemu

REGISTRY: dict[str, type[Emulator]] = {
    "pcsx2": Pcsx2,
    "duckstation": Duckstation,
    "dolphin": Dolphin,
    "cemu": Cemu,
    "azahar": Azahar,
    "eden": Eden,
    "shadps4": Shadps4,
    "retroarch": Retroarch,
    "rpcs3": Rpcs3,
    "xemu": Xemu,
    "desktop": Desktop,
}


def get_emulator(name: str) -> Emulator | None:
    cls = REGISTRY.get(name)
    return cls() if cls else None
