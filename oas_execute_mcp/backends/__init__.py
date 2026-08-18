"""Backend implementations. Use get_backend() — never import concrete classes directly."""

from .base import Backend
from .. import config


def get_backend() -> Backend:
    name = config.BACKEND_NAME
    if name == "stub":
        from .stub import StubBackend
        return StubBackend()
    if name == "mt5_demo":
        from .mt5_demo import MT5DemoBackend
        return MT5DemoBackend()
    if name == "ctrader":
        raise NotImplementedError("cTrader OpenAPI backend pending (only built if FTMO 1-step retake opens on cTrader)")
    if name == "mt5_vps":
        raise NotImplementedError("MT5 VPS backend pending — fallback if Mac-native bridge proves unreliable")
    raise ValueError(f"Unknown OAS_EXECUTE_BACKEND: {name!r}")
