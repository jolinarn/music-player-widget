import ctypes
import os

from PyQt6 import sip
from PyQt6.QtWidgets import QApplication

# Wayland ignores move() and WindowStaysOnBottomHint, so on Wayland the widget
# becomes a layer-shell surface on the "bottom" layer (above the wallpaper,
# below normal windows), positioned by top/left margins. Same ctypes approach
# as ~/discord-overlay/discord_overlay/wayland.py.
_lib = None
_ANCHOR_TOP = 1
_ANCHOR_BOTTOM = 2
_ANCHOR_LEFT = 4
_ANCHOR_RIGHT = 8
_LAYER_BOTTOM = 1
_LAYER_OVERLAY = 3
_KB_NONE = 0
_KB_ON_DEMAND = 2


class _CQMargins(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_int),
        ("top", ctypes.c_int),
        ("right", ctypes.c_int),
        ("bottom", ctypes.c_int),
    ]


def _load_lib():
    global _lib
    if _lib is not None:
        return _lib
    try:
        _lib = ctypes.cdll.LoadLibrary("libLayerShellQtInterface.so.6")
    except OSError:
        _lib = False
    return _lib


def _get_layer_window(widget):
    lib = _load_lib()
    if not lib:
        return None, None
    win = widget.windowHandle()
    if not win:
        return None, None
    get_fn = lib._ZN12LayerShellQt6Window3getEP7QWindow
    get_fn.restype = ctypes.c_void_p
    get_fn.argtypes = [ctypes.c_void_p]
    lsw = get_fn(sip.unwrapinstance(win))
    if not lsw:
        return None, None
    return lib, lsw


def is_wayland() -> bool:
    app = QApplication.instance()
    if app:
        return app.platformName() == "wayland"
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def setup_layer_shell(widget, x: int, y: int, screen=None) -> bool:
    """Make the widget a bottom-layer surface at (x, y) on `screen` (default:
    primary). Must run before first show()."""
    widget.winId()
    lib, lsw = _get_layer_window(widget)
    if not lsw:
        return False

    fn = lib._ZN12LayerShellQt6Window8setLayerENS0_5LayerE
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn(lsw, _LAYER_BOTTOM)

    fn = lib._ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn(lsw, _ANCHOR_TOP | _ANCHOR_LEFT)

    fn = lib._ZN12LayerShellQt6Window16setExclusiveZoneEi
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn(lsw, 0)

    # on-demand so the page still gets keys if it ever needs them, without grabbing focus
    fn = lib._ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn(lsw, _KB_ON_DEMAND)

    fn = lib._ZN12LayerShellQt6Window17setActivateOnShowEb
    fn.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    fn(lsw, False)

    _set_screen(widget, lib, lsw, screen or QApplication.primaryScreen())

    return set_position(widget, x, y)


def _set_screen(widget, lib, lsw, screen):
    if not screen:
        return
    widget.windowHandle().setScreen(screen)
    fn = lib._ZN12LayerShellQt6Window9setScreenEP7QScreen
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    fn(lsw, sip.unwrapinstance(screen))


def move_to_screen(widget, screen, x: int, y: int) -> bool:
    """Put an already-shown layer surface on another monitor at (x, y).
    A layer surface's output is fixed once it's mapped, so unmap it, switch
    the output and remap."""
    lib, lsw = _get_layer_window(widget)
    if not lsw:
        return False
    widget.hide()
    _set_screen(widget, lib, lsw, screen)
    set_position(widget, x, y)
    widget.show()
    return True


def set_position(widget, x: int, y: int) -> bool:
    lib, lsw = _get_layer_window(widget)
    if not lsw:
        return False
    margins = _CQMargins(x, y, 0, 0)
    fn = lib._ZN12LayerShellQt6Window10setMarginsERK8QMargins
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    fn(lsw, ctypes.byref(margins))
    # margin changes only reach the compositor on the next commit
    widget.windowHandle().requestUpdate()
    return True


def setup_fill_overlay(widget, screen=None) -> bool:
    """Full-screen, click-through overlay-layer surface on `screen` (default:
    primary). Must run before first show()."""
    widget.winId()
    lib, lsw = _get_layer_window(widget)
    if not lsw:
        return False

    fn = lib._ZN12LayerShellQt6Window8setLayerENS0_5LayerE
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn(lsw, _LAYER_OVERLAY)

    # all four anchors + exclusive zone 0 -> same usable area as the widget's margins
    fn = lib._ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn(lsw, _ANCHOR_TOP | _ANCHOR_BOTTOM | _ANCHOR_LEFT | _ANCHOR_RIGHT)

    fn = lib._ZN12LayerShellQt6Window16setExclusiveZoneEi
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn(lsw, 0)

    fn = lib._ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn(lsw, _KB_NONE)

    fn = lib._ZN12LayerShellQt6Window17setActivateOnShowEb
    fn.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    fn(lsw, False)

    _set_screen(widget, lib, lsw, screen or QApplication.primaryScreen())
    return True
