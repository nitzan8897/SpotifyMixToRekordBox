// Evaluated inside the Spotify web player page by phase1_discover.py.
// Read-only apart from tagging candidate elements with a data attribute so
// the script can click one on request. Returns a JSON-serialisable summary.
(args) => {
  const kwRe = new RegExp(args.pattern, "i");
  const MAX = 3000;
  const attrs = ["aria-label", "data-testid", "title", "id", "role", "aria-valuetext", "name", "placeholder"];
  const out = { url: location.href, title: document.title, matches: [], sliders: [], candidates: [] };

  const describe = (el) => {
    const r = el.getBoundingClientRect();
    const a = {};
    for (const n of attrs) { const v = el.getAttribute(n); if (v) a[n] = v; }
    const cls = typeof el.className === "string" ? el.className : "";
    return {
      tag: el.tagName.toLowerCase(),
      attrs: a,
      classes: cls.slice(0, 200),
      text: (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200),
      visible: r.width > 0 && r.height > 0,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      data: Object.fromEntries(Object.entries(el.dataset || {}).slice(0, 20)),
    };
  };

  // Sliders / range inputs: the most likely carriers of fade lengths and EQ values.
  for (const el of document.querySelectorAll('input[type=range], [role=slider], [aria-valuenow], progress, meter')) {
    const d = describe(el);
    d.value = el.value ?? null;
    d.min = el.getAttribute("min") ?? el.getAttribute("aria-valuemin");
    d.max = el.getAttribute("max") ?? el.getAttribute("aria-valuemax");
    d.now = el.getAttribute("aria-valuenow");
    // Label from an ancestor, e.g. "Low" / "Fade length".
    let p = el.parentElement, label = "";
    for (let i = 0; i < 4 && p && !label; i++, p = p.parentElement) {
      label = (p.getAttribute("aria-label") || "").trim();
    }
    d.ancestor_label = label;
    out.sliders.push(d);
  }

  // Every element whose attributes or own text mention a keyword.
  let n = 0;
  for (const el of document.querySelectorAll("body *")) {
    if (n >= MAX) break;
    const own = Array.from(el.childNodes).filter(c => c.nodeType === 3).map(c => c.textContent).join(" ").trim();
    const hay = attrs.map(a => el.getAttribute(a) || "").join(" ") + " " + (typeof el.className === "string" ? el.className : "") + " " + own;
    if (!kwRe.test(hay)) continue;
    out.matches.push(describe(el));
    n++;
  }

  // Clickable things that might open a mix / transition editor.
  const openRe = new RegExp(args.openPattern, "i");
  const unsafeRe = new RegExp(args.unsafePattern, "i");
  let idx = 0;
  document.querySelectorAll("[data-smtr-candidate]").forEach(el => el.removeAttribute("data-smtr-candidate"));
  for (const el of document.querySelectorAll('button, [role=button], a, [role=tab], [role=menuitem]')) {
    const label = [el.getAttribute("aria-label"), el.getAttribute("title"), el.getAttribute("data-testid"), (el.innerText || "").trim()].filter(Boolean).join(" | ");
    if (!openRe.test(label)) continue;
    const d = describe(el);
    d.label = label.slice(0, 200);
    d.safe = !unsafeRe.test(label);
    d.index = idx;
    el.setAttribute("data-smtr-candidate", String(idx));
    out.candidates.push(d);
    idx++;
  }
  return out;
}
