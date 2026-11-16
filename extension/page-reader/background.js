// Cecil Page Reader: only when Blake clicks the toolbar button (or presses Alt+Shift+C) is the active
// tab read and sent to ClipAgent on this laptop. activeTab means no site is readable until clicked.
importScripts("capture.js");

const DEFAULTS = { server: "http://127.0.0.1:8000", token: "" };

async function settings() {
  const s = await chrome.storage.local.get(DEFAULTS);
  return { server: (s.server || DEFAULTS.server).replace(/\/+$/, ""), token: s.token || "" };
}

function badge(tabId, text, color) {
  chrome.action.setBadgeBackgroundColor({ tabId, color });
  chrome.action.setBadgeText({ tabId, text });
  setTimeout(() => chrome.action.setBadgeText({ tabId, text: "" }), 4000);
}

async function send(payload) {
  const { server, token } = await settings();
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  const res = await fetch(server + "/pagereader/capture", { method: "POST", headers, body: JSON.stringify(payload) });
  if (!res.ok) throw new Error("ClipAgent answered " + res.status);
  return res.json();
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || !tab.id) return;
  badge(tab.id, "...", "#666666");
  try {
    let results;
    try {
      results = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: cecilCapturePage });
    } catch (e) {  // cross-origin frames may refuse; the top page alone is still useful
      results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: cecilCapturePage });
    }
    const pages = (results || []).map((r) => r.result).filter(Boolean);
    const top = pages.find((p) => p.top) || pages[0];
    if (!top) throw new Error("this page cannot be read (Chrome blocks extensions on its own pages)");
    const frames = pages.filter((p) => p !== top && p.text && p.text.trim())
      .slice(0, 10).map((p) => ({ url: p.url, text: p.text }));
    const controls = top.controls.concat(...pages.filter((p) => p !== top).map((p) => p.controls)).slice(0, 300);
    await send({ ...top, controls, frames });
    badge(tab.id, "OK", "#1a7f37");
  } catch (e) {
    console.warn("Cecil Page Reader:", e);
    badge(tab.id, "ERR", "#b42318");
  }
});
