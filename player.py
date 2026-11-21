#!/usr/bin/env python3
import sys
import os

# Chromium throttles rendering and JS timers on windows it considers occluded
# (covered by other windows), to save power. Since this widget is pinned
# below every other window (WindowStaysOnBottomHint), it's almost always
# occluded from Chromium's point of view, which made the UI appear frozen
# (stale cover art, etc.) whenever it wasn't the topmost visible window.
# Must be set before QApplication/the WebEngine profile initializes.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling",
)

import re
import subprocess
import base64
import fcntl
import json
import traceback
from PyQt6.QtCore import QUrl, QObject, pyqtSlot, Qt, QFile, QIODevice
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEngineScript
from PyQt6.QtWebChannel import QWebChannel

import wayland


PLAYERS = "plasma-browser-integration,brave"
WAYLAND_START_POS = (40, 40)  # no window manager placement on Wayland, so pick a spot


# matches --card-clip in player.html
CARD_PATH = [(20, 0), (262, 0), ("q", 280, 0, 280, 18), (280, 300), (260, 320),
             (18, 320), ("q", 0, 320, 0, 302), (0, 20)]
OUTLINE_COLOR = QColor(0, 240, 255, 200)


class DragOutline(QWidget):
    # Wayland-only stand-in for live dragging: a click-through, full-screen
    # overlay that draws the card's outline where it will land. The real
    # window stays put until the drag ends (see WindowBridge).
    def __init__(self):
        super().__init__()
        self._pos = None
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setup_ok = wayland.setup_fill_overlay(self)

    def show_at(self, x, y):
        self._pos = (x, y)
        if not self.isVisible():
            self.show()
        self.update()

    def paintEvent(self, _):
        if self._pos is None:
            return
        path = QPainterPath()
        x0, y0 = self._pos
        path.moveTo(x0 + CARD_PATH[0][0], y0 + CARD_PATH[0][1])
        for pt in CARD_PATH[1:]:
            if pt[0] == "q":
                path.quadTo(x0 + pt[1], y0 + pt[2], x0 + pt[3], y0 + pt[4])
            else:
                path.lineTo(x0 + pt[0], y0 + pt[1])
        path.closeSubpath()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillPath(path, QColor(0, 240, 255, 20))
        pen = QPen(OUTLINE_COLOR, 2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawPath(path)


class WindowBridge(QObject):
    # QWebEngineView's rendered content is backed by Chromium's own render
    # surface, which captures raw mouse events before they reach the outer
    # QWidget's mousePressEvent/mouseMoveEvent overrides -- so window dragging
    # can't be implemented at the Python widget level. Instead, JS in the page
    # (which reliably gets every mouse event) tracks the drag and calls this
    # bridge to move the actual OS window.
    #
    # On Wayland the app can't know where its window is (screen coords are
    # really window-local), and moving the window mid-drag shifts those coords
    # under the pointer, so live moves overshoot. Instead the window stays put,
    # an outline follows the pointer drift from the grab point, and the window
    # jumps there on release.
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.wayland_pos = None  # [x, y] layer-shell margins, None on X11
        self.outline = None
        self._target = None

    @pyqtSlot(int, int, int, int)
    def drag(self, dx, dy, grab_dx, grab_dy):
        if self.wayland_pos is None:
            self.window.move(self.window.x() + dx, self.window.y() + dy)
            return
        screen = self.window.screen().geometry()
        x = max(0, min(self.wayland_pos[0] + grab_dx, screen.width() - self.window.width()))
        y = max(0, min(self.wayland_pos[1] + grab_dy, screen.height() - self.window.height()))
        self._target = [x, y]
        if self.outline is not None:
            self.outline.show_at(x, y)

    @pyqtSlot()
    def drag_end(self):
        if self.outline is not None:
            self.outline.hide()
        if self._target is not None and self._target != self.wayland_pos:
            self.wayland_pos = self._target
            wayland.set_position(self.window, *self._target)
        self._target = None


def _find_brave_sink_input():
    # MPRIS Volume on both plasma-browser-integration and brave is unreliable here:
    # plasma-browser-integration's write gets overwritten by the extension re-syncing
    # to the page's real volume within ~1s, and brave's MPRIS Volume write is a no-op.
    # Control the real PipeWire stream volume directly instead.
    try:
        data = json.loads(subprocess.check_output(["pactl", "-f", "json", "list", "sink-inputs"], text=True))
    except Exception:
        return None

    for stream in data:
        app_name = stream.get("properties", {}).get("application.name", "").lower()
        if "brave" in app_name:
            return stream
    return None


def _get_brave_volume():
    stream = _find_brave_sink_input()
    if not stream:
        return 0.5
    try:
        pct = stream["volume"]["front-left"]["value_percent"].rstrip("%")
        return max(0.0, min(1.0, float(pct) / 100.0))
    except Exception:
        return 0.5


def _get_brave_muted():
    stream = _find_brave_sink_input()
    return bool(stream.get("mute", False)) if stream else False


class MediaBridge(QObject):
    def __init__(self):
        super().__init__()
        # xesam:url (used for the thumbnail fallback below) lags ~1 poll
        # cycle behind title after a track change -- for a moment it still
        # points at the previous track's video id. Tracking what we last
        # confirmed lets us detect that staleness instead of briefly showing
        # the wrong track's art.
        self._last_seen_title = None
        self._last_confirmed_video_id = None
        # mpris:length is unstable for some content (e.g. YouTube Music radio
        # streams with no per-track URL just keep growing it like a live
        # stream's elapsed duration), not just right after a track change.
        # Only trust a length once it's been reported twice in a row.
        self._pending_length = None
        self._stable_length = 0
        self._art_cache_key = None
        self._art_cache_value = ""

    def _inline_local_art(self, file_url):
        if file_url == self._art_cache_key:
            return self._art_cache_value
        value = ""
        try:
            path = QUrl(file_url).toLocalFile()
            with open(path, "rb") as f:
                data = f.read()
            mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            value = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        except OSError:
            pass
        self._art_cache_key, self._art_cache_value = file_url, value
        return value

    @pyqtSlot(result=str)
    def get_metadata(self):
        try:
            # Fetch title/artist/art/url/length/position/status in a single
            # playerctl call. With two competing MPRIS sources
            # (plasma-browser-integration and brave), separate calls could
            # each independently resolve to a different one of the two,
            # pairing e.g. one player's position with the other's length --
            # causing the progress bar to jump, or title/art to mismatch.
            # One call guarantees every field comes from the same player at
            # the same instant.
            sep = "\x1f"
            fmt = sep.join([
                "{{title}}", "{{artist}}", "{{mpris:artUrl}}", "{{xesam:url}}",
                "{{mpris:length}}", "{{position}}", "{{status}}",
            ])
            raw = subprocess.check_output(
                ["playerctl", "-p", PLAYERS, "metadata", "--format", fmt], text=True
            ).strip()
            parts = (raw.split(sep) + [""] * 7)[:7]
            title, artist, art_url, track_url, length_us, position_us, status = parts

            try:
                length = float(length_us) / 1_000_000.0 if length_us else 0
            except ValueError:
                length = 0
            try:
                position = float(position_us) / 1_000_000.0 if position_us else 0
            except ValueError:
                position = 0

            # Properties don't update atomically -- title can change one poll
            # before mpris:length/position catch up, which would otherwise
            # briefly show the previous track's duration/position (e.g. a
            # stale 4-minute length paired with a fresh track that's only
            # 2 minutes long). Track title changes once, up front, and use
            # it to guard both this and the art-url staleness check below.
            title_just_changed = title != self._last_seen_title
            self._last_seen_title = title
            if title_just_changed:
                position = 0
                self._pending_length = None
                self._stable_length = 0

            # Only trust a length reading once it repeats on the very next
            # poll -- filters out both the one-poll transition lag above and
            # ongoing per-poll drift (growing/live-style content), which
            # would otherwise make the progress bar jump or show a wrong total.
            if length == self._pending_length:
                self._stable_length = length
            else:
                self._pending_length = length
            length = self._stable_length

            # Fall back to YouTube's predictable thumbnail URL when the track's page URL still has a video id.
            if not art_url:
                candidate_id = None
                match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", track_url)
                if match:
                    candidate_id = match.group(1)

                if title_just_changed and candidate_id == self._last_confirmed_video_id:
                    # xesam:url hasn't caught up to this track change yet --
                    # serving it now would show the previous track's art.
                    candidate_id = None

                if candidate_id:
                    art_url = f"https://i.ytimg.com/vi/{candidate_id}/hqdefault.jpg"
                    self._last_confirmed_video_id = candidate_id

            # plasma-browser-integration hands out art as a local /tmp file,
            # which the page can't load -- inline it as a data: URI instead
            # (cached, so the file is only read when the art changes).
            if art_url.startswith("file://"):
                art_url = self._inline_local_art(art_url)

            volume = _get_brave_volume()
            muted = _get_brave_muted()

            try:
                shuffle = subprocess.check_output(["playerctl", "-p", PLAYERS, "shuffle"], text=True, stderr=subprocess.DEVNULL).strip()
            except Exception:
                shuffle = "Off"

            try:
                loop = subprocess.check_output(["playerctl", "-p", PLAYERS, "loop"], text=True, stderr=subprocess.DEVNULL).strip()
            except Exception:
                loop = "None"

            return json.dumps({
                "title": title if title else "Nothing Playing",
                "artist": artist if artist else "Brave",
                "art_url": art_url,
                "status": status if status else "Stopped",
                "position": position,
                "length": length,
                "volume": volume,
                "muted": muted,
                "shuffle": shuffle,
                "loop": loop
            })
        except Exception:
            traceback.print_exc()
            return json.dumps({
                "title": "Nothing Playing",
                "artist": "Brave",
                "art_url": "",
                "status": "Stopped",
                "position": 0,
                "length": 0,
                "volume": 0.5,
                "muted": False,
                "shuffle": "Off",
                "loop": "None"
            })

    @pyqtSlot()
    def play_pause(self):
        subprocess.run(["playerctl", "-p", PLAYERS, "play-pause"])

    @pyqtSlot()
    def next_track(self):
        subprocess.run(["playerctl", "-p", PLAYERS, "next"])

    @pyqtSlot()
    def prev_track(self):
        subprocess.run(["playerctl", "-p", PLAYERS, "previous"])

    @pyqtSlot(float)
    def set_position(self, seconds):
        subprocess.run(["playerctl", "-p", PLAYERS, "position", str(seconds)])

    @pyqtSlot(float)
    def set_volume(self, level):
        # level between 0.0 and 1.0; controls Brave's real PipeWire stream volume
        level = max(0.0, min(1.0, level))
        stream = _find_brave_sink_input()
        if stream:
            subprocess.run(["pactl", "set-sink-input-volume", str(stream["index"]), f"{round(level * 100)}%"])

    @pyqtSlot()
    def toggle_mute(self):
        stream = _find_brave_sink_input()
        if stream:
            subprocess.run(["pactl", "set-sink-input-mute", str(stream["index"]), "toggle"])

    @pyqtSlot()
    def toggle_shuffle(self):
        try:
            current = subprocess.check_output(["playerctl", "-p", PLAYERS, "shuffle"], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            current = "Off"
        subprocess.run(["playerctl", "-p", PLAYERS, "shuffle", "Off" if current == "On" else "On"])

    @pyqtSlot()
    def toggle_repeat(self):
        try:
            current = subprocess.check_output(["playerctl", "-p", PLAYERS, "loop"], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            current = "None"
        order = ["None", "Track", "Playlist"]
        next_state = order[(order.index(current) + 1) % len(order)] if current in order else "None"
        subprocess.run(["playerctl", "-p", PLAYERS, "loop", next_state])


def _acquire_single_instance_lock():
    # A second launch (menu clicked twice, restart without killing the old
    # one) would stack an identical widget exactly on top of the first --
    # dragging then looks like the widget "copied itself". Hold an flock for
    # the process lifetime; if it's taken, another instance is running.
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    lock_file = open(os.path.join(runtime_dir, "music-player-widget.lock"), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("music-player-widget is already running", file=sys.stderr)
        sys.exit(0)
    return lock_file


def main():
    _instance_lock = _acquire_single_instance_lock()  # noqa: F841 -- keep fd open
    app = QApplication(sys.argv)
    web = QWebEngineView()

    web.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    web.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnBottomHint)
    web.page().setBackgroundColor(Qt.GlobalColor.transparent)

    # Album art URLs from playerctl are remote; QWebEngine blocks file:// pages from fetching remote content by default.
    web.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

    web.resize(280, 320)

    channel = QWebChannel()
    bridge = MediaBridge()
    window_bridge = WindowBridge(web)
    channel.registerObject('bridge', bridge)
    channel.registerObject('windowBridge', window_bridge)
    web.page().setWebChannel(channel)

    # Newer QtWebEngine refuses <script src="qrc:///..."> from a file:// page,
    # leaving QWebChannel undefined and the widget stuck on "Nothing Playing".
    # Inject Qt's bundled qwebchannel.js straight into the page instead.
    js_file = QFile(":/qtwebchannel/qwebchannel.js")
    if js_file.open(QIODevice.OpenModeFlag.ReadOnly):
        script = QWebEngineScript()
        script.setName("qwebchannel")
        script.setSourceCode(bytes(js_file.readAll()).decode("utf-8"))
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        web.page().scripts().insert(script)
        js_file.close()

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player.html")
    web.load(QUrl.fromLocalFile(html_path))

    if wayland.is_wayland():
        # Loading the page makes QtWebEngine swap the native window for an
        # OpenGL one, which drops any layer-shell setup done before -- so let
        # that happen first, then configure the window that will actually show.
        app.processEvents()
        if wayland.setup_layer_shell(web, *WAYLAND_START_POS):
            window_bridge.wayland_pos = list(WAYLAND_START_POS)
            outline = DragOutline()
            # without it, Wayland drags still work, just with no preview
            if outline.setup_ok:
                window_bridge.outline = outline
        else:
            print("layer-shell-qt unavailable, widget position is up to the compositor",
                  file=sys.stderr)

    web.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
