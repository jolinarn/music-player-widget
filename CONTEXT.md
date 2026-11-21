# Music Player Widget — Context for AI Assistant

**Project**: Cyberpunk-style "Now Playing" desktop widget for Brave/YouTube Music.
**Location**: `/home/jolinarn/music-player-widget/` — `player.py` (backend) + `player.html` (UI).
**Run**: `python3 player.py`

## Stack

PyQt6 `QWebEngineView` renders `player.html` (HTML/CSS/JS card) as a frameless, transparent,
always-below-other-windows widget. Python exposes a `MediaBridge` (media control) and
`WindowBridge` (window dragging) over `QWebChannel`. Backend talks to `playerctl` (MPRIS) for
playback and `pactl` (PipeWire) for real volume/mute control.

## Non-obvious gotchas baked into the current code — don't undo these without knowing why

1. **Two competing MPRIS sources** exist for Brave (`plasma-browser-integration` and
   `brave.instance...`) with independent state. Fetching fields via separate `playerctl` calls
   let them resolve to *different* players per-call, causing backward-jumping progress bars and
   mismatched title/art. Fix: one atomic
   `playerctl metadata --format "{{title}}\x1f{{artist}}\x1f..."` call per poll (see
   `MediaBridge.get_metadata`).

2. **MPRIS `Volume`/`Shuffle` don't work reliably.** Volume is controlled via
   `pactl set-sink-input-volume` on Brave's real PipeWire stream (found by `application.name`
   containing "brave", see `_find_brave_sink_input`). Shuffle has no MPRIS property at all (not
   in the Media Session API spec) — its UI toggle is cosmetic/local-only, driven by JS state, not
   real playback behavior.

3. **`xesam:url` lags ~1 poll behind `title`** after a track change, so the
   YouTube-thumbnail-fallback (derived from the video ID in that URL) can briefly show the
   *previous* track's art. Fixed by tracking `_last_confirmed_video_id`/`_last_seen_title` on
   `MediaBridge` and suppressing a candidate ID that matches the old one right after a title
   change.

4. **Chromium throttles occluded/backgrounded windows** (this widget is always pinned below
   others via `WindowStaysOnBottomHint` → almost always "occluded"), which freezes JS
   timers/rendering. Fixed via `QTWEBENGINE_CHROMIUM_FLAGS` env var set *before* `QApplication`
   is constructed (top of `player.py`).

5. **`QWebEngineView` doesn't reliably deliver raw mouse events to Python widget overrides.**
   Window dragging is implemented in JS (tracks `mousemove` on `.player-border`) calling
   `windowBridge.drag(dx, dy, grabDx, grabDy)`, not via Qt's `mousePressEvent`/`mouseMoveEvent`.


6. **CSS `[hidden]` is beaten by any author `display` rule** of equal/higher specificity. The
   menu popover's `display: flex` had to be scoped to `.menu-popover:not([hidden])` — otherwise
   toggling `.hidden` in JS had zero visual effect (this caused a real bug: the menu appeared
   "stuck open" forever).

7. **No CDN scripts.** Lucide icons originally failed to load because QtWebEngine blocks
   `file://` pages from fetching remote resources by default. Icons were switched to inline SVG
   to avoid the dependency entirely. `LocalContentCanAccessRemoteUrls` is force-enabled (see
   `main()`) only so remote YouTube thumbnail *images* can load.

8. **Volume-drag is throttled** (max one `pactl` call per 80ms during a drag, final precise
   value sent on mouseup) — unthrottled `mousemove`-triggered subprocess spawns were backing up
   the main thread enough to delay metadata polling and make cover art appear stuck.

9. **Wayland** (`wayland.py`): `move()` and `WindowStaysOnBottomHint` are ignored there, so the
   widget becomes a layer-shell surface on the *bottom* layer, anchored top-left and positioned
   by margins (starts at `WAYLAND_START_POS`). Two traps:
   - Loading the page makes QtWebEngine swap the native window for an OpenGL one, dropping any
     layer-shell setup done earlier -- so setup runs *after* `web.load()` + `processEvents()`.
   - The app can't know its window position, and moving the window mid-drag shifts the
     window-local pointer coords under it, so live dragging overshoots (badly on fast drags).
     Instead the window stays put while a click-through full-screen `DragOutline` shows where
     it will land, and `drag_end` moves it once. X11 still drags live via `dx/dy`.

## Current features

- Play/pause/next/prev, seek-on-click progress bar (client-side interpolated for smoothness
  between polls)
- Draggable + lockable window position
- Click-and-drag volume slider with mute toggle (real PipeWire control)
- Repeat (real, via `playerctl loop`) + shuffle (cosmetic, MPRIS doesn't support it) buttons
- Three-dot menu: lock-position toggle (green/white dot, not a checkbox), background color
  picker, "Reset to default" button
- Settings (lock state, background color) persisted via `localStorage`, so they survive restarts

## Dependencies

`PyQt6`, `PyQt6-WebEngine`, `playerctl`, `pactl` (PipeWire), and the KDE Plasma Browser
Integration extension installed in Brave.
