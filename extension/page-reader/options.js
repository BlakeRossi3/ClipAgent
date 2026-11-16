const DEFAULTS = { server: "http://127.0.0.1:8000", token: "" };
const $ = (id) => document.getElementById(id);
const say = (t) => { $("msg").textContent = t; };

chrome.storage.local.get(DEFAULTS).then((s) => { $("server").value = s.server; $("token").value = s.token; });

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ server: $("server").value.trim() || DEFAULTS.server, token: $("token").value.trim() });
  say("Saved.");
});

$("test").addEventListener("click", async () => {
  const server = ($("server").value.trim() || DEFAULTS.server).replace(/\/+$/, "");
  const token = $("token").value.trim();
  try {
    const res = await fetch(server + "/pagereader/status", { headers: token ? { Authorization: "Bearer " + token } : {} });
    say(res.ok ? "Connected to ClipAgent." : "ClipAgent answered " + res.status + (res.status === 401 ? " - check the token." : "."));
  } catch (e) {
    say("Could not reach ClipAgent at " + server + ". Is it running?");
  }
});
