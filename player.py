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
import json
from PyQt6.QtCore import QUrl, QObject, pyqtSlot, Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebChannel import QWebChannel


PLAYERS = "plasma-browser-integration,brave"


class WindowBridge(QObject):
    # QWebEngineView's rendered content is backed by Chromium's own render
    # surface, which captures raw mouse events before they reach the outer
    # QWidget's mousePressEvent/mouseMoveEvent overrides -- so window dragging
    # can't be implemented at the Python widget level. Instead, JS in the page
    # (which reliably gets every mouse event) tracks the drag and calls this
    # bridge to move the actual OS window.
    def __init__(self, window):
        super().__init__()
        self.window = window

    @pyqtSlot(int, int)
    def move_by(self, dx, dy):
        self.window.move(self.window.x() + dx, self.window.y() + dy)


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

            volume = _get_brave_volume()
            muted = _get_brave_muted()

            try:
                shuffle = subprocess.check_output(["playerctl", "-p", PLAYERS, "shuffle"], text=True).strip()
            except Exception:
                shuffle = "Off"

            try:
                loop = subprocess.check_output(["playerctl", "-p", PLAYERS, "loop"], text=True).strip()
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
            current = subprocess.check_output(["playerctl", "-p", PLAYERS, "shuffle"], text=True).strip()
        except Exception:
            current = "Off"
        subprocess.run(["playerctl", "-p", PLAYERS, "shuffle", "Off" if current == "On" else "On"])

    @pyqtSlot()
    def toggle_repeat(self):
        try:
            current = subprocess.check_output(["playerctl", "-p", PLAYERS, "loop"], text=True).strip()
        except Exception:
            current = "None"
        order = ["None", "Track", "Playlist"]
        next_state = order[(order.index(current) + 1) % len(order)] if current in order else "None"
        subprocess.run(["playerctl", "-p", PLAYERS, "loop", next_state])


def main():
    app = QApplication(sys.argv)
    web = QWebEngineView()

    web.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    web.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnBottomHint)
    web.page().setBackgroundColor(Qt.GlobalColor.transparent)

    # Album art URLs from playerctl are remote; QWebEngine blocks file:// pages from fetching remote content by default.
    web.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

    channel = QWebChannel()
    bridge = MediaBridge()
    window_bridge = WindowBridge(web)
    channel.registerObject('bridge', bridge)
    channel.registerObject('windowBridge', window_bridge)
    web.page().setWebChannel(channel)

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player.html")
    web.load(QUrl.fromLocalFile(html_path))

    web.resize(280, 320)
    web.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
