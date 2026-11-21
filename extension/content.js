// Runs inside music.youtube.com. Reports YouTube Music's own view of the
// current track (per-track time, shuffle, repeat) and presses its real
// shuffle/repeat buttons on request.
//
// Why not MPRIS: YouTube Music plays the queue as one continuous media
// stream, so <video>.currentTime/duration (and therefore MPRIS position and
// length) keep counting across tracks. The player bar's progress slider is
// the only per-track time source in the page.

const BAR = "ytmusic-player-bar, ytmusic-player-controls";
const REPEAT_BTN = [
  "#button-shape-repeat button",
  "ytmusic-player-bar .repeat",
  "ytmusic-player-controls .repeat",
];
const SHUFFLE_BTN = [
  "#button-shape-shuffle button",
  "ytmusic-player-bar .shuffle",
  "ytmusic-player-controls .shuffle",
];
const PROGRESS = [
  "ytmusic-player-bar #progress-bar",
  "ytmusic-player-controls #progress-bar",
  "#progress-bar",
];
const TITLE = [
  "ytmusic-player-bar .content-info-wrapper .title",
  "ytmusic-player-bar .title",
  "ytmusic-player-controls .title",
];

const ART = [
  "ytmusic-player-bar .thumbnail-image-wrapper img",
  "ytmusic-player-bar img.image",
  "ytmusic-player-bar img",
  "ytmusic-player-controls img",
];

function first(selectors) {
  for (const s of selectors) {
    const el = document.querySelector(s);
    if (el) return el;
  }
  return null;
}

function repeatState() {
  const bar = document.querySelector(BAR);
  if (bar?.hasAttribute("repeat-mode-one")) return "ONE";
  if (bar?.hasAttribute("repeat-mode-all")) return "ALL";
  const btn = first(REPEAT_BTN);
  const label = (btn?.getAttribute("aria-label") || btn?.title || "").toLowerCase();
  if (label.includes("one")) return "ONE";
  if (label.includes("all")) return "ALL";
  if (bar?.getAttribute("repeat-mode")) return bar.getAttribute("repeat-mode").toUpperCase();
  return "NONE";
}

function shuffleOn() {
  const btn = first(SHUFFLE_BTN);
  if (!btn) return false;
  const pressed = btn.getAttribute("aria-pressed");
  if (pressed === "true") return true;
  if (pressed === "false") return false;
  const label = (btn.getAttribute("aria-label") || btn.title || "").toLowerCase();
  return label.includes("off"); // "Turn shuffle off" => currently on
}

function parseClock(text) {
  // "1:02" or "1:02:03" -> seconds
  const parts = text.trim().split(":").map(Number);
  if (!parts.length || parts.some(isNaN)) return null;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}

function trackTime() {
  const slider = first(PROGRESS);
  if (slider) {
    const now = parseFloat(slider.getAttribute("value") ?? slider.getAttribute("aria-valuenow"));
    const max = parseFloat(slider.getAttribute("aria-valuemax"));
    if (isFinite(now) && isFinite(max) && max > 0) return { position: now, length: max };
  }
  // Fallback: the "1:02 / 3:09" text next to the controls.
  const info = document.querySelector("ytmusic-player-bar .time-info, .time-info");
  const m = info?.textContent.match(/([\d:]+)\s*\/\s*([\d:]+)/);
  if (m) {
    const position = parseClock(m[1]);
    const length = parseClock(m[2]);
    if (position !== null && length) return { position, length };
  }
  return { position: 0, length: 0 };
}

// The slider may only move in whole seconds. Pair it with the media
// element's smooth clock: remember where this track sits on the <video>
// timeline and only re-anchor when the two disagree by more than the
// slider's granularity (track change, seek, buffering hiccup).
let videoOffset = null;
let offsetTrack = null;

function preciseTime() {
  const video = document.querySelector("video");
  const { position, length } = trackTime();
  // A new track (or a replaced <video>) invalidates the anchor even if the
  // clocks happen to agree within the tolerance below.
  const track = (first(TITLE)?.textContent || "") + "|" + length;
  if (track !== offsetTrack) {
    offsetTrack = track;
    videoOffset = null;
  }
  if (!video || !length) {
    videoOffset = null;
    return { position, length };
  }
  const smooth = videoOffset === null ? null : video.currentTime - videoOffset;
  if (smooth === null || Math.abs(smooth - position) > 1.5) {
    videoOffset = video.currentTime - position;
    return { position, length };
  }
  return { position: Math.max(0, Math.min(smooth, length)), length };
}

// The player-bar thumbnail is what YT Music itself shows for the current
// track, so it changes exactly when the track does. It's served tiny
// (=w60-h60...); ask Google's image server for a large square instead.
function artUrl() {
  // (navigator.mediaSession artwork would be ideal, but the page's
  // MediaMetadata isn't visible from an extension's isolated world.)
  // Skip <img>s that exist but haven't loaded a picture yet.
  let src = "";
  for (const sel of ART) {
    const img = [...document.querySelectorAll(sel)].find((el) => el.src?.startsWith("http"));
    if (img) {
      src = img.src;
      break;
    }
  }
  if (!src) return "";
  if (src.includes("googleusercontent.com")) {
    return src.replace(/=w\d+-h\d+[^/]*$/, "=w544-h544-l90-rj");
  }
  return src; // i.ytimg.com video thumbnails are already a usable size
}

function snapshot() {
  const video = document.querySelector("video");
  const { position, length } = preciseTime();
  return {
    op: "state",
    title: first(TITLE)?.textContent?.trim() || "",
    art: artUrl(),
    position,
    length,
    playing: video ? !video.paused : false,
    shuffle: shuffleOn(),
    repeat: repeatState(),
    found: {
      art: !!artUrl(),
      shuffle: !!first(SHUFFLE_BTN),
      repeat: !!first(REPEAT_BTN),
      progress: !!first(PROGRESS),
    },
  };
}

let pushTimer = null;

function push() {
  // After the extension is reloaded/updated, this copy of the script is
  // orphaned: chrome.runtime.id goes away and every send throws. Stop the
  // timer; reloading the YouTube Music page injects the new copy.
  if (!chrome.runtime?.id) {
    clearInterval(pushTimer);
    return;
  }
  try {
    chrome.runtime.sendMessage(snapshot()).catch(() => {});
  } catch (e) {
    clearInterval(pushTimer);
  }
}

// Absolute seek within the current track. The <video> timeline is
// continuous across tracks, so offset the target by where this track
// started on it.
function seekTo(seconds) {
  const video = document.querySelector("video");
  if (!video) return;
  if (!Number.isFinite(seconds)) return;
  const { position, length } = preciseTime();
  if (!length) return;
  const target = Math.max(0, Math.min(seconds, length - 0.5));
  video.currentTime = video.currentTime - position + target;
  videoOffset = null; // re-anchor on the next report
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.op === "toggleShuffle") first(SHUFFLE_BTN)?.click();
  else if (msg.op === "cycleRepeat") first(REPEAT_BTN)?.click();
  else if (msg.op === "seek") seekTo(Number(msg.position));
  // Report the result as soon as YT Music has re-rendered.
  setTimeout(push, 150);
});

// Twice a second is plenty: the widget interpolates between reports.
pushTimer = setInterval(push, 500);
push();
