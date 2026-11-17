// Cecil Page Reader. Two jobs, both talking only to ClipAgent on this laptop:
//  1. Keep Cecil's copy of the active tab current: re-read it when Blake switches tab or window, when a
//     page finishes loading, and every 30 s (only sent when something changed). Toolbar button or
//     Alt+Shift+C forces a read. "Read pages automatically" in Options turns the automatic part off;
//     sites on the "Never read" list are never read.
//  2. Let Cecil act on that tab: a long-poll to /pagereader/next hands over one action at a time
//     (click / type / submit / scroll / navigate / back / capture); the result and a fresh capture go
//     back to /pagereader/result. "Let Cecil click" in Options turns this off.
importScripts("capture.js");

const DEFAULTS = { server: "http://127.0.0.1:8000", token: "", auto: true, act: true, blocked: "" };
const REFRESH_ALARM = "cecil-refresh";
let lastSent = "";        // fingerprint of the last capture sent, so unchanged pages are not re-sent
let pending = null;       // debounce timer for automatic reads
let polling = false;

async function settings() {
  const s = await chrome.storage.local.get(DEFAULTS);
  return { ...s, server: (s.server || DEFAULTS.server).replace(/\/+$/, "") };
}

function badge(tabId, text, color) {
  if (!tabId) return;
  chrome.action.setBadgeBackgroundColor({ tabId, color });
  chrome.action.setBadgeText({ tabId, text });
  setTimeout(() => chrome.action.setBadgeText({ tabId, text: "" }), 4000);
}

async function post(path, payload, signal) {
  const { server, token } = await settings();
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  const res = await fetch(server + path, { method: "POST", headers, body: JSON.stringify(payload), signal });
  if (!res.ok) throw new Error("ClipAgent answered " + res.status);
  return res.status === 204 ? null : res.json();
}

function readable(url) {
  return /^(https?|file):/i.test(url || "");
}

function blockedHost(url, blocked) {
  let host = "";
  try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return false; }
  return blocked.split(/[\s,]+/).map((b) => b.trim().toLowerCase().replace(/^\*\./, "")).filter(Boolean)
    .some((b) => host === b || host.endsWith("." + b));
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

// Read every frame we can reach; the top frame first, then the others in frame order.
async function readTab(tabId) {
  let results;
  try {
    results = await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, func: cecilPage, args: ["capture"] });
  } catch (e) {  // cross-origin frames may refuse; the top page alone is still useful
    results = await chrome.scripting.executeScript({ target: { tabId }, func: cecilPage, args: ["capture"] });
  }
  const pages = (results || []).sort((a, b) => a.frameId - b.frameId).map((r) => r.result).filter(Boolean);
  const top = pages.find((p) => p.top) || pages[0];
  if (!top) throw new Error("this page cannot be read (Chrome blocks extensions on its own pages)");
  const rest = pages.filter((p) => p !== top);
  const frames = rest.filter((p) => p.text && p.text.trim()).slice(0, 10).map((p) => ({ url: p.url, text: p.text }));
  const controls = top.controls.concat(...rest.map((p) => p.controls)).slice(0, 300);
  return { ...top, controls, frames };
}

// Capture the active tab and send it. force = Blake asked (button / shortcut) or Cecil acted.
async function captureActive({ force = false, tab = null } = {}) {
  const s = await settings();
  tab = tab || await activeTab();
  if (!tab || !tab.id || !readable(tab.url)) return null;
  if (blockedHost(tab.url, s.blocked)) {
    if (force) badge(tab.id, "OFF", "#666666");
    return null;
  }
  if (!force && !s.auto) return null;
  const page = await readTab(tab.id);
  const print = page.url + "\n" + page.title + "\n" + page.text.length + "\n" + page.text.slice(0, 4000)
    + "\n" + page.controls.map((c) => c.label).join("|");
  if (!force && print === lastSent) return page;
  await post("/pagereader/capture", { ...page, auto: !force });
  lastSent = print;
  return page;
}

function scheduleCapture(delay = 800) {
  clearTimeout(pending);
  pending = setTimeout(() => captureActive().catch((e) => console.debug("Cecil auto-read:", e.message)), delay);
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || !tab.id) return;
  badge(tab.id, "...", "#666666");
  try {
    const page = await captureActive({ force: true, tab });
    if (page) badge(tab.id, "OK", "#1a7f37");
  } catch (e) {
    console.warn("Cecil Page Reader:", e);
    badge(tab.id, "ERR", "#b42318");
  }
});

chrome.tabs.onActivated.addListener(() => scheduleCapture(500));
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (tab.active && (info.status === "complete" || info.url)) scheduleCapture(info.status === "complete" ? 600 : 1500);
});
chrome.windows.onFocusChanged.addListener((id) => { if (id !== chrome.windows.WINDOW_ID_NONE) scheduleCapture(500); });

// --- acting for Cecil --------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Find which frame holds the labelled control (top frame first), then act in that frame only.
async function actInPage(tabId, spec) {
  let found;
  try {
    found = await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, func: cecilPage, args: ["find", spec] });
  } catch (e) {
    found = await chrome.scripting.executeScript({ target: { tabId }, func: cecilPage, args: ["find", spec] });
  }
  found = (found || []).sort((a, b) => a.frameId - b.frameId);
  let skip = spec.occurrence || 0;
  let frameId = null;
  for (const r of found) {
    const n = (r.result && r.result.count) || 0;
    if (skip < n) { frameId = r.frameId; break; }
    skip -= n;
  }
  if (frameId === null) {  // occurrence past the end: fall back to the first match anywhere
    const first = found.find((r) => r.result && r.result.count);
    if (!first) return { ok: false, detail: "nothing labelled \"" + (spec.label || "") + "\" on the page" };
    frameId = first.frameId;
    skip = 0;
  }
  const [res] = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] }, func: cecilPage, args: ["do", { ...spec, occurrence: skip }] });
  return (res && res.result) || { ok: false, detail: "the page did not answer" };
}

async function waitForLoad(tabId, ms = 8000) {
  const end = Date.now() + ms;
  await sleep(400);
  while (Date.now() < end) {
    const t = await chrome.tabs.get(tabId).catch(() => null);
    if (!t || t.status === "complete") return;
    await sleep(250);
  }
}

async function perform(action) {
  const tab = await activeTab();
  if (!tab || !tab.id) return { ok: false, detail: "no Chrome tab is active" };
  const s = await settings();
  if (!readable(tab.url) && !["navigate"].includes(action.kind)) return { ok: false, detail: "Chrome does not let extensions touch this page" };
  if (readable(tab.url) && blockedHost(tab.url, s.blocked)) return { ok: false, detail: "this site is on the Never read list" };
  let res;
  if (action.kind === "navigate") {
    if (!/^https?:\/\//i.test(action.url || "")) return { ok: false, detail: "only http(s) addresses" };
    if (blockedHost(action.url, s.blocked)) return { ok: false, detail: "that site is on the Never read list" };
    await chrome.tabs.update(tab.id, { url: action.url });
    res = { ok: true, detail: "opened " + action.url };
  } else if (action.kind === "back") {
    await chrome.tabs.goBack(tab.id);
    res = { ok: true, detail: "went back" };
  } else if (action.kind === "capture") {
    res = { ok: true, detail: "read the page" };
  } else {
    res = await actInPage(tab.id, action);
  }
  if (action.kind !== "capture") await waitForLoad(tab.id);
  try {
    res.capture = await readTab(tab.id);
    lastSent = "";  // the server stores this capture; the next automatic read is a fresh comparison
  } catch (e) {
    res.capture_error = e.message;
  }
  badge(tab.id, res.ok ? "OK" : "ERR", res.ok ? "#1a7f37" : "#b42318");
  return res;
}

// One long-poll at a time. Each round also calls an extension API, which keeps this worker awake;
// the alarm below restarts the loop if Chrome put the worker to sleep anyway.
async function pollLoop() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      const s = await settings();
      if (!s.act) break;
      await chrome.runtime.getPlatformInfo();
      let action = null;
      try {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), 28000);
        action = await post("/pagereader/next", { wait: 20 }, ctl.signal);
        clearTimeout(timer);
      } catch (e) {
        await sleep(5000);  // ClipAgent not running or restarting
        continue;
      }
      if (!action || !action.id) continue;
      let res;
      try {
        res = await perform(action);
      } catch (e) {
        res = { ok: false, detail: String(e && e.message || e) };
      }
      await post("/pagereader/result", { id: action.id, ...res }).catch((e) => console.warn("Cecil result:", e));
    }
  } finally {
    polling = false;
  }
}

function start() {
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: 0.5 });
  pollLoop();
  scheduleCapture(1000);
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== REFRESH_ALARM) return;
  pollLoop();
  scheduleCapture(0);
});
chrome.runtime.onStartup.addListener(start);
chrome.runtime.onInstalled.addListener(start);
chrome.storage.onChanged.addListener(() => { lastSent = ""; pollLoop(); });
start();
