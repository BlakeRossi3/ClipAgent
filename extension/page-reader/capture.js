// Runs inside the page (chrome.scripting.executeScript serialises this one function, so it must be
// self-contained). Returns what Cecil may see: url, title, selection, headings, visible text and the
// labels of buttons, links and fields. Never the values typed into fields; password fields are skipped.
function cecilCapturePage() {
  const clip = (s, n) => (s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none";
  };
  const labelOf = (el) => {
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return l.innerText;
    }
    const wrap = el.closest("label");
    return el.getAttribute("aria-label") || (wrap && wrap.innerText) || el.getAttribute("placeholder")
      || el.getAttribute("title") || el.getAttribute("name") || el.innerText || el.getAttribute("value") || "";
  };
  const controls = [];
  const sel = "a[href], button, input, select, textarea, [role=button], [role=link], [role=tab], [role=menuitem]";
  for (const el of document.querySelectorAll(sel)) {
    if (controls.length >= 300) break;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "password" || type === "hidden" || !visible(el)) continue;
    let kind = el.getAttribute("role") || tag;
    if (tag === "input") kind = ["submit", "button", "reset"].includes(type) ? "button" : "field";
    if (tag === "select" || tag === "textarea") kind = "field";
    if (tag === "a") kind = "link";
    // a submit button's value is its caption, not user input; every other field's value is left out
    const label = clip(kind === "button" && tag === "input" ? el.value : labelOf(el), 160);
    if (!label) continue;
    const c = { kind, label };
    if (tag === "a") c.href = el.href;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") c.disabled = true;
    controls.push(c);
  }
  const headings = [...document.querySelectorAll("h1, h2, h3")].filter(visible)
    .map((h) => clip(h.innerText, 200)).filter(Boolean).slice(0, 80);
  return {
    top: window === window.top,
    url: location.href,
    title: document.title,
    selection: String(window.getSelection ? window.getSelection() : "").slice(0, 10000),
    headings,
    controls,
    text: (document.body ? document.body.innerText : "").slice(0, 200000),
  };
}
