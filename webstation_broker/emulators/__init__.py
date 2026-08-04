from .base import Emulator
from .desktop import Desktop
from .eden import Eden
from .pcsx2 import Pcsx2
from .retroarch import Retroarch
from .shadps4 import Shadps4

REGISTRY: dict[str, type[Emulator]] = {
    "pcsx2": Pcsx2,
    "eden": Eden,
    "shadps4": Shadps4,
    "retroarch": Retroarch,
    "desktop": Desktop,
}


def get_emulator(name: str) -> Emulator | None:
    cls = REGISTRY.get(name)
    return cls() if cls else None
