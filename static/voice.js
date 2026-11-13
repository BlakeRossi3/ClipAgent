// Voice: home mic pill + mute toggle, Settings "test my mic" meter and microphone list (voice/web.py).
(function () {
  "use strict";
  var LABELS = { idle: "Listening", listening: "Listening to you", followup: "Listening", thinking: "Thinking",
    speaking: "Speaking", muted: "Muted", no_mic: "No microphone", starting: "Starting", offline: "Voice off", stopped: "Voice off" };
  var pill = document.querySelector("[data-voice-pill]");
  function poll() {
    fetch("/voice/api/status", { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.json(); }).then(function (st) {
        if (!st.enabled && st.state === "offline") { pill.hidden = true; return; }
        pill.hidden = false;
        pill.dataset.state = st.state;
        pill.querySelector("[data-voice-label]").textContent = "Mic: " + (LABELS[st.state] || st.state);
        var b = pill.querySelector("[data-voice-mute]");
        b.textContent = st.muted ? "Unmute" : "Mute";
        b.dataset.muted = st.muted ? "1" : "0";
      }).catch(function () {});
  }
  if (pill) {
    pill.querySelector("[data-voice-mute]").addEventListener("click", function (e) {
      var body = new FormData();
      body.append("csrf", pill.dataset.csrf);
      body.append("muted", e.target.dataset.muted === "1" ? "0" : "1");
      fetch("/voice/api/mute", { method: "POST", body: body, credentials: "same-origin",
        headers: { Accept: "application/json" } }).then(poll);
    });
    poll();
    setInterval(poll, 3000);
  }
  var test = document.querySelector("[data-voice-test]");
  if (test) {
    var meter = document.querySelector("[data-voice-meter]"), note = document.querySelector("[data-voice-test-note]");
    var input = document.querySelector("[data-voice-devices]");
    fetch("/voice/api/devices", { headers: { Accept: "application/json" } }).then(function (r) { return r.json(); })
      .then(function (d) {
        var list = document.getElementById("voice-devs");
        (d.devices || []).forEach(function (x) { var o = document.createElement("option"); o.value = x.name; list.appendChild(o); });
        note.textContent = d.error ? d.error : (d.chosen ? "Automatic choice: " + d.chosen : "");
      }).catch(function () {});
    test.addEventListener("click", function () {
      var n = 0;
      (function once() {
        fetch("/voice/api/level?device=" + encodeURIComponent(input.value || ""), { headers: { Accept: "application/json" } })
          .then(function (r) { return r.json(); }).then(function (d) {
            meter.value = d.level || 0;
            note.textContent = d.error ? d.error : "Speak now - " + d.device;
            if (!d.error && ++n < 16) once(); else if (!d.error) note.textContent = "Done - " + d.device;
          });
      })();
    });
  }
})();
