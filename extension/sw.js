// Service worker: relays between the YouTube Music tab(s) and the native
// host (native/host.py), which in turn talks to the desktop widget.

const HOST = "com.jolinarn.musicplayerwidget";
const RETRY_MS = 5000;
let port = null;
let retryTimer = null;
// Commands go to the tab that most recently reported it was playing, so a
// second YT Music tab doesn't get its shuffle toggled too.
let activeTabId = null;

function scheduleReconnect() {
  if (retryTimer !== null) return;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, RETRY_MS);
}

function connect() {
  if (port) return;
  let p;
  try {
    p = chrome.runtime.connectNative(HOST);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  port = p;
  // Handlers are bound to this port instance: a late disconnect from an
  // old port must not clear a newer one.
  p.onMessage.addListener((msg) => {
    if (activeTabId === null) return;
    chrome.tabs.sendMessage(activeTabId, msg).catch(() => {
      activeTabId = null; // tab navigated away or its script is orphaned
    });
  });
  p.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError?.message;
    if (err) console.warn("native host disconnected:", err);
    if (port === p) port = null;
    scheduleReconnect();
  });
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg?.op !== "state" || !sender.tab) return;
  if (msg.playing || activeTabId === null) activeTabId = sender.tab.id;
  if (sender.tab.id !== activeTabId) return;
  // Incoming state wakes the worker after Chrome idles it; reconnect then
  // instead of waiting for the retry timer.
  if (!port) connect();
  try {
    port?.postMessage(msg);
  } catch (e) {
    port = null;
    scheduleReconnect();
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === activeTabId) activeTabId = null;
});

connect();
