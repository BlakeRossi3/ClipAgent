// Runs inside the page. chrome.scripting.executeScript serialises this ONE function, so it must be
// self-contained (no helpers outside it). Three operations share the same list of controls, so the
// labels Cecil sees in a capture are exactly the ones he can click:
//   cecilPage("capture")      -> url, title, selection, headings, visible text, control labels.
//                                Never the values typed into fields; password fields are skipped.
//   cecilPage("find", spec)   -> how many controls in this frame match spec.label (exact, then partial).
//   cecilPage("do", spec)     -> perform spec.kind (click | type | submit | scroll) on the match.
function cecilPage(op, spec) {
  const clip = (s, n) => (s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const norm = (s) => clip(s, 400).toLowerCase();
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
  const sel = "a[href], button, input, select, textarea, [contenteditable=true], [contenteditable=''], "
    + "[role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [role=option], [role=textbox]";
  for (const el of document.querySelectorAll(sel)) {
    if (controls.length >= 300) break;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "password" || type === "hidden" || !visible(el)) continue;
    let kind = el.getAttribute("role") || tag;
    if (tag === "input") kind = ["submit", "button", "reset"].includes(type) ? "button"
      : (["checkbox", "radio"].includes(type) ? type : "field");
    if (tag === "select" || tag === "textarea" || el.isContentEditable || kind === "textbox") kind = "field";
    if (tag === "a") kind = "link";
    // a submit button's value is its caption, not user input; every other field's value is left out
    const label = clip(tag === "input" && kind === "button" ? el.value : labelOf(el), 160);
    if (!label) continue;
    const c = { kind, label };
    if (tag === "a") c.href = el.href;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") c.disabled = true;
    if ((kind === "checkbox" || kind === "radio") && el.checked) c.checked = true;
    controls.push({ c, el });
  }

  if (op === "capture") {
    const headings = [...document.querySelectorAll("h1, h2, h3")].filter(visible)
      .map((h) => clip(h.innerText, 200)).filter(Boolean).slice(0, 80);
    return {
      top: window === window.top,
      url: location.href,
      title: document.title,
      selection: String(window.getSelection ? window.getSelection() : "").slice(0, 10000),
      headings,
      controls: controls.map((x) => x.c),
      text: (document.body ? document.body.innerText : "").slice(0, 200000),
    };
  }

  spec = spec || {};
  if (spec.kind === "scroll") {
    if (op === "find") return { count: window === window.top ? 1 : 0 };
    const dy = Math.round(window.innerHeight * 0.8) * (spec.direction === "up" ? -1 : 1);
    if (spec.direction === "top") window.scrollTo(0, 0); else window.scrollBy(0, dy);
    return { ok: true, detail: "scrolled " + (spec.direction || "down") };
  }

  // Candidates: exact control label, else controls containing it, else any visible element whose own
  // text is exactly the label (for sites that make plain divs clickable).
  const want = norm(spec.label);
  if (!want) return op === "find" ? { count: 0 } : { ok: false, detail: "no label given" };
  let found = controls.filter((x) => norm(x.c.label) === want);
  if (!found.length) found = controls.filter((x) => norm(x.c.label).includes(want));
  if (!found.length && spec.kind === "click") {
    const leaves = [];
    for (const el of document.body ? document.body.querySelectorAll("*") : []) {
      if (leaves.length >= 20) break;
      if (norm(el.innerText) !== want || !visible(el)) continue;
      if ([...el.children].some((ch) => norm(ch.innerText) === want)) continue;  // keep the deepest
      leaves.push({ c: { kind: "text", label: clip(el.innerText, 160) }, el });
    }
    found = leaves;
  }
  if (op === "find") return { count: found.length };

  const pick = found[Math.max(0, Math.min(spec.occurrence || 0, found.length - 1))];
  if (!pick) return { ok: false, detail: "nothing labelled \"" + spec.label + "\" on the page" };
  const el = pick.el;
  const tag = el.tagName.toLowerCase();
  if ((el.getAttribute("type") || "").toLowerCase() === "password") return { ok: false, detail: "password fields are off limits" };
  if (pick.c.disabled) return { ok: false, detail: "\"" + pick.c.label + "\" is disabled" };
  el.scrollIntoView({ block: "center", inline: "center" });

  if (spec.kind === "click") {
    const r = el.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    for (const t of ["pointerover", "pointerenter", "mouseover", "pointerdown", "mousedown"]) {
      el.dispatchEvent(t.startsWith("pointer") ? new PointerEvent(t, at) : new MouseEvent(t, at));
    }
    if (typeof el.focus === "function") el.focus();
    for (const t of ["pointerup", "mouseup"]) {
      el.dispatchEvent(t.startsWith("pointer") ? new PointerEvent(t, at) : new MouseEvent(t, at));
    }
    el.click();
    return { ok: true, detail: "clicked " + pick.c.kind + " \"" + pick.c.label + "\"" };
  }

  if (spec.kind === "type") {
    const text = String(spec.text == null ? "" : spec.text);
    if (typeof el.focus === "function") el.focus();
    if (tag === "select") {
      const opt = [...el.options].find((o) => norm(o.text) === norm(text))
        || [...el.options].find((o) => norm(o.text).includes(norm(text)));
      if (!opt) return { ok: false, detail: "no option \"" + text + "\" in \"" + pick.c.label + "\"" };
      el.value = opt.value;
    } else if (tag === "input" || tag === "textarea") {
      // React and friends watch the native setter, so set the value through it
      const proto = tag === "input" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
    } else if (el.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand("insertText", false, text);
    } else {
      return { ok: false, detail: "\"" + pick.c.label + "\" is not a field" };
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, detail: "typed into \"" + pick.c.label + "\"" };
  }

  if (spec.kind === "submit") {
    if (typeof el.focus === "function") el.focus();
    const key = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
    const go = el.dispatchEvent(new KeyboardEvent("keydown", key));
    el.dispatchEvent(new KeyboardEvent("keypress", key));
    el.dispatchEvent(new KeyboardEvent("keyup", key));
    if (go && el.form) {
      if (typeof el.form.requestSubmit === "function") el.form.requestSubmit(); else el.form.submit();
    }
    return { ok: true, detail: "pressed Enter in \"" + pick.c.label + "\"" };
  }
  return { ok: false, detail: "unknown action " + spec.kind };
}
