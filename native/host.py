#!/usr/bin/env python3
"""Native messaging host for the Music Player Widget Bridge extension.

Brave starts this process when the extension connects. It relays:
  extension -> widget: YouTube Music state ({"op": "state", ...})
  widget -> extension: commands ({"op": "toggleShuffle" | "cycleRepeat" | "seek"})

Brave side: Chromium native messaging over stdio (4-byte little-endian
length prefix + UTF-8 JSON). Widget side: newline-delimited JSON on a unix
socket in $XDG_RUNTIME_DIR, which this host serves -- the host lives as long
as Brave does, while the widget can come and go.

Never print to stdout: it would corrupt the native messaging framing.
"""
import json
import os
import socket
import struct
import sys
import threading
import time

SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
    "music-player-widget.sock",
)

stdout_lock = threading.Lock()
clients_lock = threading.Lock()
clients = []
latest_state = None


LOG_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "music-player-widget", "host.log",
)


def log(*args):
    # stderr goes to Brave's own stderr, which nobody sees -- also keep a
    # small file log for debugging the bridge.
    line = " ".join(str(a) for a in args)
    print("music-player-widget host:", line, file=sys.stderr, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 256_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        pass


MAX_MESSAGE = 1 << 20  # extension -> host messages are small state snapshots


def read_native():
    """Next message from Brave; None when the port closes. Malformed
    messages are skipped rather than killing the bridge."""
    while True:
        header = sys.stdin.buffer.read(4)
        if len(header) < 4:
            return None
        (length,) = struct.unpack("=I", header)  # native byte order, per Chromium
        if length > MAX_MESSAGE:
            log("oversized message, closing:", length)
            return None
        body = sys.stdin.buffer.read(length)
        if len(body) < length:
            return None
        try:
            msg = json.loads(body.decode("utf-8"))
        except ValueError:
            log("unparseable message skipped")
            continue
        if isinstance(msg, dict):
            return msg


def write_native(obj):
    data = json.dumps(obj).encode("utf-8")
    with stdout_lock:
        sys.stdout.buffer.write(struct.pack("=I", len(data)) + data)
        sys.stdout.buffer.flush()


# Don't hand a freshly connected widget a snapshot older than this --
# the YouTube Music tab may be long gone.
REPLAY_MAX_AGE = 2.0
latest_state_at = 0.0


def publish(obj):
    """Record the newest state and send it to every widget. Holding
    clients_lock for both the update and the sends keeps a new client's
    replay (serve_client) from landing after a newer broadcast."""
    global latest_state, latest_state_at
    line = (json.dumps(obj) + "\n").encode("utf-8")
    with clients_lock:
        latest_state = obj
        latest_state_at = time.monotonic()
        for conn in list(clients):
            try:
                conn.sendall(line)
            except OSError:  # includes the send timeout below
                clients.remove(conn)
                conn.close()


def serve_client(conn):
    # A widget that stops reading must not block the host (and with it
    # Brave's stdin pipe): give up on sends after one second.
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", 1, 0))
    with clients_lock:
        clients.append(conn)
        if latest_state is not None and time.monotonic() - latest_state_at < REPLAY_MAX_AGE:
            try:
                conn.sendall((json.dumps(latest_state) + "\n").encode("utf-8"))
            except OSError:
                pass
    buf = b""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_MESSAGE:  # no newline in sight; not our protocol
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("op") in ("toggleShuffle", "cycleRepeat", "seek"):
                    log("command", json.dumps(msg))
                    write_native(msg)
    except OSError:
        pass
    finally:
        with clients_lock:
            if conn in clients:
                clients.remove(conn)
        conn.close()


def bind_socket():
    # A second Brave profile/window could start another host. Only one may
    # serve: if the socket answers, someone else owns it.
    if os.path.exists(SOCKET_PATH):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(SOCKET_PATH)
            probe.close()
            return None
        except OSError:
            os.unlink(SOCKET_PATH)  # stale, left by a crashed host
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    server.listen(4)
    return server


def accept_loop(server):
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve_client, args=(conn,), daemon=True).start()


def main():
    server = bind_socket()
    if server is None:
        log("another host already serves", SOCKET_PATH)
        # Keep draining stdin so Brave doesn't see a crash-loop; drop messages.
        while read_native() is not None:
            pass
        return
    threading.Thread(target=accept_loop, args=(server,), daemon=True).start()
    log("started for", sys.argv[1:] or "?")
    seen_states = 0
    try:
        while True:
            msg = read_native()
            if msg is None:
                log("Brave closed the port")
                break
            if msg.get("op") == "state":
                seen_states += 1
                if seen_states == 1 or seen_states % 120 == 0:
                    log("state #%d" % seen_states, json.dumps(msg))
                publish(msg)
    finally:
        server.close()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
