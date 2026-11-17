const DEFAULTS = { server: "http://127.0.0.1:8000", token: "", auto: true, act: true, blocked: "" };
const $ = (id) => document.getElementById(id);
const say = (t) => { $("msg").textContent = t; };

chrome.storage.local.get(DEFAULTS).then((s) => {
  $("server").value = s.server;
  $("token").value = s.token;
  $("auto").checked = !!s.auto;
  $("act").checked = !!s.act;
  $("blocked").value = s.blocked;
});

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    server: $("server").value.trim() || DEFAULTS.server,
    token: $("token").value.trim(),
    auto: $("auto").checked,
    act: $("act").checked,
    blocked: $("blocked").value.trim(),
  });
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
