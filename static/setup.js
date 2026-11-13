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
