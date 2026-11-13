// Setup wizard: fill the time zone from this browser (first run only, when the field still holds the default).
(function () {
  "use strict";
  var el = document.querySelector("[data-tz-auto]");
  if (!el) return;
  try {
    var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz && tz.indexOf("/") > 0 && el.value === "America/New_York") el.value = tz;
  } catch (e) { /* old browser: keep the default */ }
})();
// The brain: follow the Claude sign-in (install -> browser sign-in -> test) without reloading.
(function () {
  "use strict";
  var live = document.querySelector("[data-claude-live]");
  if (!live) return;
  var ACTIVE = ["starting", "installing", "signing_in", "testing"];
  var started = ACTIVE.indexOf(live.getAttribute("data-state")) >= 0;
  var startForm = document.querySelector("[data-claude-start]");
  function show(sel, on) { var el = live.querySelector(sel); if (el) el.hidden = !on; }
  function render(s) {
    var active = ACTIVE.indexOf(s.state) >= 0;
    live.hidden = s.state === "idle";
    live.setAttribute("data-state", s.state);
    live.classList.toggle("bad", s.state === "error");
    var msg = live.querySelector("[data-claude-msg]");
    if (msg) msg.textContent = s.message || "";
    show(".s-spin", active);
    var link = live.querySelector("[data-claude-link]");
    if (link && s.url && /^https:\/\//.test(s.url)) link.href = s.url;
    show("[data-claude-url]", active && !!s.url);
    show("[data-claude-code]", s.state === "signing_in");
    show("[data-claude-cancel]", active);
    show("[data-claude-error]", s.state === "error");
    if (startForm) startForm.hidden = active;
    return active;
  }
  function poll() {
    fetch("/setup/claude/status", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        if (!s) { setTimeout(poll, 4000); return; }
        var active = render(s);
        if (active) { started = true; setTimeout(poll, 1500); }
        else if (started) { window.location.reload(); }
      })
      .catch(function () { setTimeout(poll, 4000); });
  }
  if (started) poll();
})();
