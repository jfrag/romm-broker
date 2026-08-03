from .base import Emulator
from .desktop import Desktop
from .pcsx2 import Pcsx2

REGISTRY: dict[str, type[Emulator]] = {
    "pcsx2": Pcsx2,
    "desktop": Desktop,
}


def get_emulator(name: str) -> Emulator | None:
    cls = REGISTRY.get(name)
    return cls() if cls else None
