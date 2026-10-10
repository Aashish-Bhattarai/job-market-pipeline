// Browser-side extraction/filling. Every exported function here talks to a
// live `tab` (see chrome_connect.mjs) via `tab.run`. Nothing in this file is
// unit-testable offline — see field_rules.mjs for the pure logic and
// test_extraction.mjs for fixture-based (linkedom) extraction checks that
// exercise the pure `extractFieldsFromDocument` walker below without a
// browser.

// ---------------------------------------------------------------------
// Pure DOM walker: takes a `document`-like object (real DOM in the browser,
// or a linkedom document in tests) and returns field descriptors. No
// Puppeteer-specific calls happen in here so it can run identically inside
// `page.evaluate` and inside a fixture test.
// ---------------------------------------------------------------------
export function extractFieldsFromDocument(doc, root) {
  root = root || doc;
  const results = [];
  let counter = 0;

  function isVisible(el) {
    // The `hidden` attribute render-hides by definition in every
    // environment -- a linkedom fixture has no layout to probe and a real
    // browser may hide it further with CSS -- so honour it before any
    // layout-based check. (Observed: Ashby's yes/no widgets keep a hidden
    // backing <input type=checkbox> next to the visible button pair.)
    if (el && el.closest && el.closest("[hidden]")) return false;
    if (!el || typeof el.getClientRects !== "function") return true; // linkedom has no layout; assume visible
    const rects = el.getClientRects();
    if (doc.defaultView && rects.length === 0) return false;
    if (doc.defaultView) {
      const style = doc.defaultView.getComputedStyle(el);
      if (style && (style.visibility === "hidden" || style.display === "none")) return false;
    }
    return true;
  }

  // Custom-styled radio/checkbox widgets (observed: Rippling) frequently
  // keep the native <input> itself hidden (opacity:0/clip/absolute) while
  // rendering a fully visible label or container in its place. Testing the
  // native input's own visibility for these throws away every option; test
  // the associated label/container instead, falling back to the input only
  // when neither exists.
  function isOptionVisible(el) {
    const wrap = el.closest && el.closest("label");
    if (wrap) return isVisible(wrap);
    if (el.id) {
      const esc = el.id.replace(/(["\\])/g, "\\$1");
      const lab = root.querySelector(`label[for="${esc}"]`);
      if (lab) return isVisible(lab);
    }
    const container = el.closest && el.closest(QUESTION_CONTAINER_SEL);
    if (container) return isVisible(container);
    return isVisible(el);
  }

  function textOf(el) {
    return (el && el.textContent || "").replace(/SVGs not supported by this browser\.?/gi, "")
      .replace(/No location found\. Try entering a different location\s*(Loading)?/gi, "").replace(/\s+/g, " ").trim();
  }
  // A role widget's own label is often a SIBLING referenced by aria-labelledby ("<group>_label <option>_label", Workable): the
  // option-specific id is listed last. textOf(widget) alone is empty there.
  function ariaLabelledText(el) {
    const ids = String((el && el.getAttribute && el.getAttribute("aria-labelledby")) || "").split(/\s+/).filter(Boolean);
    for (let i = ids.length - 1; i >= 0; i--) {
      const node = (doc.getElementById && doc.getElementById(ids[i])) || null;
      const t = node ? textOf(node) : "";
      if (t) return t;
    }
    return "";
  }
  // A native input that is aria-hidden inside/next to a role widget is that widget's backing input (the role pass reads it).
  function isBackingInput(el) {
    return el.getAttribute("aria-hidden") === "true"
      && !!(el.closest("[role=radio], [role=checkbox]") || (el.parentElement && el.parentElement.querySelector("[role=radio], [role=checkbox]")));
  }
  const QUESTION_CONTAINER_SEL =
    "fieldset, [role='group'], [role='radiogroup'], li.application-question, .application-question, .custom-question, .form-question, .field-group, .form-group, .ashby-application-form-field-entry, [class*='question']";

  function findContainerLabel(el) {
    const container = el.closest && el.closest(QUESTION_CONTAINER_SEL);
    if (!container) return null;
    const legend = container.querySelector(":scope > legend");
    if (legend && textOf(legend)) return textOf(legend);
    const labelledby = container.getAttribute && container.getAttribute("aria-labelledby");
    if (labelledby) {
      const txt = labelledby
        .split(/\s+/)
        .map((id) => textOf(doc.getElementById(id)))
        .join(" ")
        .trim();
      if (txt) return txt;
    }
    const labelEl = container.querySelector(
      ".application-label .text, .application-label, .field-label .text, .field-label, .question-text, .form-label, .text, label, legend"
    );
    // 1000, not 200: Thought Machine's required "...We require a minimum of 3 days a week in the office... Does this working
    // model align with your expectations?" is ~290 characters and came back as an empty label (an unanswerable question).
    if (labelEl && textOf(labelEl) && textOf(labelEl).length <= 1000 && !labelEl.contains(el)) return textOf(labelEl);
    const ariaLabel = container.getAttribute && container.getAttribute("aria-label");
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    return null;
  }

  // Rippling-style opaque field identifiers: `labelFor` falling back to a
  // raw `name`/`placeholder` like "customQuestions.<hash>.<uniqueKey>" is
  // useless to both classifyField and a human reviewer. Rippling's own
  // page embeds the real question list (React's __NEXT_DATA__ payload) --
  // search it generically for an object whose id/uniqueKey matches a
  // segment of the opaque string, alongside a human title/label. The exact
  // prop path can shift between Rippling releases, so this walks the whole
  // payload rather than hardcoding `props.pageProps.activeJobApplication`.
  const OPAQUE_ID_RE = /[0-9a-f]{6,}-?[0-9a-f]{0,}|customQuestions\.|^[a-f0-9]{20,}$/i;
  function findEmbeddedQuestionTitle(idOrName) {
    if (!idOrName) return null;
    const script = doc.getElementById && doc.getElementById("__NEXT_DATA__");
    if (!script) return null;
    let data;
    try {
      data = JSON.parse(script.textContent || "");
    } catch {
      return null;
    }
    const parts = String(idOrName).split(/[.\s]+/).filter((p) => p.length > 3);
    if (!parts.length) return null;
    let found = null;
    const seen = new Set();
    function walk(node) {
      if (found || !node || typeof node !== "object" || seen.has(node)) return;
      seen.add(node);
      const key = node.uniqueKey || node.id || node.key;
      const title = node.title || node.label || node.question || node.questionText;
      if (key && title && parts.some((p) => String(key).includes(p))) {
        found = String(title).trim();
        return;
      }
      for (const v of Object.values(node)) {
        if (found) return;
        if (Array.isArray(v)) v.forEach(walk);
        else if (v && typeof v === "object") walk(v);
      }
    }
    walk(data);
    return found;
  }


  function labelFor(el) {
    // Accessible-name priority matches the real browser algorithm:
    // aria-labelledby > aria-label > native associated <label> > container
    // heuristics > placeholder/name. A decorative icon `<label for=...>`
    // (e.g. an upload-icon SVG whose only text is its "not supported by
    // this browser" fallback) must never win over a real aria-labelledby.
    const labelledby = el.getAttribute && el.getAttribute("aria-labelledby");
    if (labelledby) {
      const txt = labelledby
        .split(/\s+/)
        .map((id) => textOf(doc.getElementById(id)))
        .join(" ")
        .trim();
      if (txt) return txt;
    }
    const ariaLabel = el.getAttribute && el.getAttribute("aria-label");
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    // For file inputs, a deliberate `data-testid`/`data-qa` hook (e.g.
    // `input-resume`) is far more trustworthy than a generic associated
    // <label> or wrapping-<label> caption ("Attach", "Drop or select
    // (.doc / .docx / .pdf)") that every upload slot on the page reuses
    // verbatim -- it must be checked BEFORE those generic label lookups
    // below, not after, or it never gets a chance to win.
    if (el.type === "file") {
      const fileTestId =
        el.getAttribute &&
        (el.getAttribute("data-testid") || el.getAttribute("data-qa") || el.getAttribute("data-cy") || el.getAttribute("data-test"));
      if (fileTestId) return fileTestId.replace(/^input[-_]?/i, "").replace(/[-_]+/g, " ").trim();
    }
    if (el.id) {
      const esc = el.id.replace(/(["\\])/g, "\\$1");
      const lab = root.querySelector(`label[for="${esc}"]`);
      if (lab && textOf(lab)) return textOf(lab);
    }
    const wrap = el.closest && el.closest("label");
    if (wrap && textOf(wrap)) {
      const wrapText = textOf(wrap).replace(textOf(el), "").trim() || textOf(wrap);
      if (wrapText.length <= 200) return wrapText;
    }
    const testId =
      el.getAttribute &&
      (el.getAttribute("data-testid") || el.getAttribute("data-qa") || el.getAttribute("data-cy") || el.getAttribute("data-test"));
    const containerLabel = findContainerLabel(el);
    if (containerLabel && containerLabel.length <= 1000) return containerLabel;
    let container = el.closest && el.closest("fieldset, [role='group'], [role='radiogroup'], div, li, section");
    let depth = 0;
    while (container && depth < 5) {
      const legend = container.querySelector(":scope > legend") || container.querySelector(":scope > label");
      if (legend && textOf(legend) && textOf(legend).length <= 1000) return textOf(legend);
      const heading = container.querySelector(":scope > .field-label, :scope > .label, :scope > [class*='label']");
      if (heading && textOf(heading) && textOf(heading).length <= 1000) return textOf(heading);
      container = container.parentElement
        ? container.parentElement.closest("fieldset, div, li, section")
        : null;
      depth++;
    }
    // Modern SPA-built ATS forms frequently carry a semantic QA hook even
    // when there is no real associated label or usable container text --
    // better than falling all the way through to a placeholder/raw `name`.
    // Rippling, however, stores its OWN opaque `customQuestions.<hash>.
    // <uniqueKey>` id in `data-testid` itself (not `name`/`placeholder`),
    // so the same embedded-form-data resolution must run here too, before
    // trusting testId verbatim.
    if (testId && OPAQUE_ID_RE.test(testId)) {
      const resolved = findEmbeddedQuestionTitle(testId);
      if (resolved) return resolved;
    }
    if (testId) return testId.replace(/^input[-_]?/i, "").replace(/[-_]+/g, " ").trim();
    // A placeholder or raw `name` that's actually an opaque generated id
    // (Rippling's "customQuestions.<hash>.<uniqueKey>") must never be
    // reported as the label verbatim -- try to resolve the real question
    // title from the page's own embedded form data first.
    const rawFallback = el.placeholder || el.name || "";
    if (rawFallback && OPAQUE_ID_RE.test(rawFallback)) {
      const resolved = findEmbeddedQuestionTitle(rawFallback);
      if (resolved) return resolved;
    }
    if (el.placeholder) return el.placeholder.trim();
    if (el.name) return el.name.replace(/[_-]+/g, " ").trim();
    return "";
  }

  function containerLooksRequired(container) {
    if (!container) return false;
    const cls = container.className || "";
    if (typeof cls === "string" && /\brequired\b/i.test(cls)) return true;
    const labelEl = container.querySelector && container.querySelector(
      ".application-label, .field-label, .question-text, .form-label, label, legend"
    );
    if (labelEl) {
      const labelCls = labelEl.className || "";
      if (typeof labelCls === "string" && /\brequired\b/i.test(labelCls)) return true;
      if (/\*/.test(textOf(labelEl))) return true;
    }
    const markers = container.querySelectorAll ? container.querySelectorAll(".required, [class*='required']") : [];
    for (const m of markers) {
      if (/\*/.test(textOf(m)) || /required/i.test(m.className || "")) return true;
    }
    return false;
  }

  function isRequired(el, label) {
    if (el.required || (el.hasAttribute && el.hasAttribute("required")) || el.getAttribute("aria-required") === "true") return true;
    if (/\*|\(required\)/i.test(label)) return true;  // any asterisk: Greenhouse puts it mid-label before footnote link text
    // The page says in so many words that this control is optional (Greenhouse's React job-boards set
    // aria-required="false"): the wider-container heuristics below would then call it required whenever ANY sibling
    // field is (Figma: "Additional Information", "Other Website", "LinkedIn Profile" all read required and held the
    // application on questions that never needed an answer).
    if (el.getAttribute && el.getAttribute("aria-required") === "false") return false;
    const container = el.closest && el.closest("fieldset, div, li");
    if (container && containerLooksRequired(container)) return true;
    // Grouped/combobox questions (Ashby radio/checkbox/combobox groups)
    // often carry their required marker on the wider question-container
    // element, not the immediate div wrapping one radio/checkbox/option --
    // the nearest div above is frequently just a small per-option wrapper
    // with no required styling of its own, so also check the broader
    // QUESTION_CONTAINER_SEL ancestor that findContainerLabel resolves.
    const questionContainer = el.closest && el.closest(QUESTION_CONTAINER_SEL);
    if (questionContainer && questionContainer !== container && containerLooksRequired(questionContainer)) return true;
    return false;
  }

  function tag(el, kind) {
    const idx = counter++;
    el.setAttribute("data-omp-field", String(idx));
    el.setAttribute("data-omp-kind", kind);
    return idx;
  }

  const seen = new Set();

  // ---- aria-pressed yes/no toggle-button pairs (no native <input>, no
  // role attributes; observed: Ashby's "yesno" widget renders the two
  // choices as <button data-option="yes|no" aria-pressed="false">Yes</button>
  // pairs inside the question container) -- native radio/checkbox passes and
  // the role=radio pass never see these controls, so the whole question
  // would silently vanish from the extraction and the application could
  // never pass the ATS's own required-field validation.
  // Runs FIRST and claims every native checkbox/radio input inside the
  // question container: those are the widget's backing state holders
  // (already fully represented by the visible buttons) and must never
  // double-report as separate lonely checkbox fields.
  const pressedGroups = new Map();
  root.querySelectorAll("button[aria-pressed][data-option]").forEach((el) => {
    if (seen.has(el) || !isVisible(el)) return;
    const opt = String(el.getAttribute("data-option") || "").toLowerCase();
    if (opt !== "yes" && opt !== "no") return; // only yes/no question widgets
    const container = (el.closest && el.closest(QUESTION_CONTAINER_SEL)) || el.parentElement || el;
    if (!pressedGroups.has(container)) pressedGroups.set(container, []);
    pressedGroups.get(container).push(el);
  });
  for (const [, els] of pressedGroups) {
    if (els.length < 2) continue; // a lone toggle button is not a yes/no question
    const label = groupLabel(els);
    const groupIdx = tag(els[0], "radio-group-anchor");
    const optionDescs = els.map((el) => {
      seen.add(el);
      const oIdx = counter++;
      el.setAttribute("data-omp-field", String(groupIdx));
      el.setAttribute("data-omp-opt", String(oIdx));
      return { optIdx: oIdx, text: textOf(el) };
    });
    const qContainer = els[0].closest && els[0].closest(QUESTION_CONTAINER_SEL);
    if (qContainer) {
      qContainer.querySelectorAll("input[type=checkbox], input[type=radio]").forEach((inp) => seen.add(inp));
    }
    results.push({
      idx: groupIdx,
      type: "radio",
      label,
      required: els.some((el) => isRequired(el, label)),
      options: optionDescs.map((o) => o.text),
      optionIdx: optionDescs.map((o) => o.optIdx),
    });
  }

  // ---- native selects --------------------------------------------------
  root.querySelectorAll("select").forEach((el) => {
    if (!isVisible(el) || el.disabled) return;
    seen.add(el);
    const label = labelFor(el);
    const idx = tag(el, "select");
    const options = Array.from(el.querySelectorAll("option"))
      .map((o) => textOf(o))
      .filter((t) => t.length > 0);
    results.push({ idx, type: "select", label, required: isRequired(el, label), options });
  });

  // ---- textareas ---------------------------------------------------------
  root.querySelectorAll("textarea").forEach((el) => {
    if (!isVisible(el) || el.disabled) return;
    seen.add(el);
    const label = labelFor(el);
    const idx = tag(el, "textarea");
    results.push({ idx, type: "textarea", label, required: isRequired(el, label) });
  });

  // ---- radios & checkboxes (grouped) -------------------------------------
  const radioGroups = new Map();
  const checkboxGroups = new Map();
  const lonelyCheckboxes = [];

  root.querySelectorAll("input[type=radio]").forEach((el) => {
    if (seen.has(el)) return; // already claimed (e.g. backing input of an aria-pressed button pair)
    if (el.closest && el.closest("[hidden]")) return; // backing input of a custom widget, never itself visible
    if (isBackingInput(el)) return; // Workable: the div[role=radio] pass below reads it with its aria-labelledby text
    if (!isOptionVisible(el) || el.disabled) return;
    seen.add(el);
    const key = el.name || `__unnamed_radio_${labelFor(el.closest("fieldset") || el)}`;
    if (!radioGroups.has(key)) radioGroups.set(key, []);
    radioGroups.get(key).push(el);
  });

  root.querySelectorAll("input[type=checkbox]").forEach((el) => {
    if (seen.has(el)) return; // already claimed (e.g. backing input of an aria-pressed button pair)
    // A checkbox hidden with the `hidden` attribute (observed: the backing
    // input of Ashby's yes/no button pair) is never itself the visible
    // widget -- isOptionVisible deliberately looks at the surrounding
    // container for CSS-hidden Rippling-style inputs, so the attribute
    // must be checked directly here or the widget double-reports.
    if (el.closest && el.closest("[hidden]")) return;
    if (!isOptionVisible(el) || el.disabled) return;
    seen.add(el);
    const name = el.name || "";
    if (name.endsWith("[]") || name.endsWith("[")) {
      const key = name;
      if (!checkboxGroups.has(key)) checkboxGroups.set(key, []);
      checkboxGroups.get(key).push(el);
    } else {
      // Could still be a checkbox-group if siblings share a name or a
      // question container.
      lonelyCheckboxes.push(el);
    }
  });

  // Before falling back to per-checkbox lonely handling, group checkboxes
  // that share a nearest fieldset/[role='group']/question-container
  // ancestor into a single multi-value question (e.g. Ashby's
  // MultiValueSelect questions render as N independently-named/id'd
  // checkboxes with no shared `name`, but they all sit inside the same
  // question wrapper).
  const byContainer = new Map();
  const noContainer = [];
  for (const el of lonelyCheckboxes) {
    const container = el.closest && el.closest(QUESTION_CONTAINER_SEL);
    if (container) {
      if (!byContainer.has(container)) byContainer.set(container, []);
      byContainer.get(container).push(el);
    } else {
      noContainer.push(el);
    }
  }
  for (const [container, els] of byContainer) {
    if (els.length > 1) checkboxGroups.set(container, els);
    else noContainer.push(els[0]);
  }
  // Group whatever's left by shared `name` (non-empty) too, since some ATS
  // repeat the same `name` for a set of checkbox options with no common
  // question-container wrapper.
  const byName = new Map();
  const trueLonely = [];
  for (const el of noContainer) {
    if (el.name) {
      if (!byName.has(el.name)) byName.set(el.name, []);
      byName.get(el.name).push(el);
    } else {
      trueLonely.push(el);
    }
  }
  for (const [name, els] of byName) {
    if (els.length > 1) checkboxGroups.set(name, els);
    else trueLonely.push(els[0]);
  }

  // Last structural resort for a group with no fieldset/legend/aria name (observed: Helsing's
  // Greenhouse embed -- Tailwind markup where the question is a bare text node above the option rows,
  // so every group was labelled by its first OPTION: "No", "No", "Yes"). Take the text that precedes
  // the first option inside the lowest ancestor that holds this group and NO other radio/checkbox
  // group -- so a shared parent can never lend one group another group's question.
  function structuralGroupLabel(els) {
    const optionLabels = new Set(els.map((e) => e.closest && e.closest("label")).filter(Boolean));
    let node = els[0].parentElement;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      if (!els.every((e) => node.contains(e))) continue;
      const others = Array.from(node.querySelectorAll("input[type=radio], input[type=checkbox]")).filter((i) => !els.includes(i));
      if (others.length) return null;
      const out = [];
      let stop = false;
      const walk = (n) => {
        if (stop) return;
        if (n.nodeType === 3) {
          const t = (n.nodeValue || "").replace(/\s+/g, " ").trim();
          if (t) out.push(t);
          return;
        }
        if (n.nodeType !== 1) return;
        if (optionLabels.has(n) || els.includes(n)) { stop = true; return; }
        if (n.tagName === "SCRIPT" || n.tagName === "STYLE") return;
        Array.from(n.childNodes).forEach(walk);
      };
      walk(node);
      const text = out.join(" ").trim();
      // 1000, not 600: Helsing's interview-recording paragraph is ~650 characters and fell back to its option text "No"
      if (text && text.length <= 1000) return text;
    }
    return null;
  }

  function groupLabel(els) {
    const first = els[0];
    const containerLabel = findContainerLabel(first);
    if (containerLabel) return containerLabel;
    const fieldset = first.closest && first.closest("fieldset, [role='group'], [role='radiogroup']");
    if (fieldset) {
      const ariaLabel = fieldset.getAttribute && fieldset.getAttribute("aria-label");
      if (ariaLabel) return ariaLabel.trim();
    }
    const own = labelFor(first);
    // labelFor falls back to the first option's own text; only then is the structural resort useful.
    if (own && els.some((e) => (optionLabel(e) || "").trim() === own.trim())) {
      const structural = structuralGroupLabel(els);
      if (structural) return structural;
    }
    return own;
  }

  function optionLabel(el) {
    if (el.id) {
      const esc = el.id.replace(/(["\\])/g, "\\$1");
      const lab = root.querySelector(`label[for="${esc}"]`);
      if (lab && textOf(lab)) return textOf(lab);
    }
    const wrap = el.closest && el.closest("label");
    if (wrap) return textOf(wrap);
    const ariaLabel = el.getAttribute && el.getAttribute("aria-label");
    if (ariaLabel) return ariaLabel.trim();
    if (el.value) return el.value;
    return "";
  }

  for (const [, els] of radioGroups) {
    const label = groupLabel(els);
    const groupIdx = tag(els[0], "radio-group-anchor");
    const optionDescs = els.map((el) => {
      const oIdx = counter++;
      el.setAttribute("data-omp-field", String(groupIdx));
      el.setAttribute("data-omp-opt", String(oIdx));
      return { optIdx: oIdx, text: optionLabel(el) };
    });
    results.push({
      idx: groupIdx,
      type: "radio",
      label,
      required: els.some((el) => isRequired(el, label)),
      options: optionDescs.map((o) => o.text),
      optionIdx: optionDescs.map((o) => o.optIdx),
    });
  }

  for (const [, els] of checkboxGroups) {
    const label = groupLabel(els);
    const groupIdx = tag(els[0], "checkbox-group-anchor");
    const optionDescs = els.map((el) => {
      const oIdx = counter++;
      el.setAttribute("data-omp-field", String(groupIdx));
      el.setAttribute("data-omp-opt", String(oIdx));
      return { optIdx: oIdx, text: optionLabel(el) };
    });
    results.push({
      idx: groupIdx,
      type: "checkbox-group",
      label,
      required: els.some((el) => isRequired(el, label)),
      options: optionDescs.map((o) => o.text),
      optionIdx: optionDescs.map((o) => o.optIdx),
    });
  }

  for (const el of trueLonely) {
    let label = optionLabel(el) || labelFor(el);
    // A lone checkbox whose own text is just "Yes"/"on" (Helsing: the privacy acknowledgement is a
    // bare paragraph above a "Yes" box) is named by the text that precedes it.
    if (/^(yes|no|on|true|agree|i agree|accept)$/i.test((label || "").trim())) {
      label = structuralGroupLabel([el]) || label;
    }
    const idx = tag(el, "checkbox");
    results.push({ idx, type: "checkbox", label, required: isRequired(el, label) });
  }

  // ---- role=radiogroup / role=radio widgets (no native <input> at all,
  // e.g. Rippling's custom question radios) -------------------------------
  root.querySelectorAll("[role=radiogroup]").forEach((group) => {
    if (seen.has(group) || !isVisible(group)) return;
    const radios = Array.from(group.querySelectorAll("[role=radio]")).filter((r) => !seen.has(r));
    if (!radios.length) return;
    seen.add(group);
    radios.forEach((r) => seen.add(r));
    const label = findContainerLabel(group) || (group.getAttribute && group.getAttribute("aria-label")) || labelFor(group);
    const groupIdx = tag(radios[0], "radio-group-anchor");
    const optionDescs = radios.map((el) => {
      const oIdx = counter++;
      el.setAttribute("data-omp-field", String(groupIdx));
      el.setAttribute("data-omp-opt", String(oIdx));
      return { optIdx: oIdx, text: textOf(el) || ariaLabelledText(el) || (el.getAttribute && el.getAttribute("aria-label")) || "" };
    });
    results.push({
      idx: groupIdx,
      type: "radio",
      label,
      required: isRequired(group, label) || radios.some((el) => isRequired(el, label)),
      options: optionDescs.map((o) => o.text),
      optionIdx: optionDescs.map((o) => o.optIdx),
    });
  });

  // Standalone [role=radio] elements with no [role=radiogroup] wrapper --
  // group them by nearest shared question container instead.
  const roleRadioGroups = new Map();
  root.querySelectorAll("[role=radio]").forEach((el) => {
    if (seen.has(el) || !isVisible(el)) return;
    const container = (el.closest && el.closest(QUESTION_CONTAINER_SEL)) || el.parentElement || el;
    if (!roleRadioGroups.has(container)) roleRadioGroups.set(container, []);
    roleRadioGroups.get(container).push(el);
  });
  for (const [, els] of roleRadioGroups) {
    const label = findContainerLabel(els[0]) || labelFor(els[0]);
    const groupIdx = tag(els[0], "radio-group-anchor");
    const optionDescs = els.map((el) => {
      seen.add(el);
      const oIdx = counter++;
      el.setAttribute("data-omp-field", String(groupIdx));
      el.setAttribute("data-omp-opt", String(oIdx));
      return { optIdx: oIdx, text: textOf(el) || ariaLabelledText(el) || (el.getAttribute && el.getAttribute("aria-label")) || "" };
    });
    results.push({
      idx: groupIdx,
      type: "radio",
      label,
      required: els.some((el) => isRequired(el, label)),
      options: optionDescs.map((o) => o.text),
      optionIdx: optionDescs.map((o) => o.optIdx),
    });
  }

  // ---- role=combobox / role=listbox custom widgets -----------------------
  root.querySelectorAll("[role=combobox]").forEach((el) => {
    if (seen.has(el) || !isVisible(el)) return;
    seen.add(el);
    const label = labelFor(el);
    const idx = tag(el, "combobox");
    results.push({ idx, type: "combobox", label, required: isRequired(el, label), options: [] });
  });

  // ---- file inputs --------------------------------------------------------
  // A generic button/label caption like "Attach" or "Choose file" is
  // frequently reused verbatim across every upload slot on the page (e.g.
  // Greenhouse's embedded form labels both the resume and cover-letter
  // inputs "Attach" via a visually-hidden <label>). Blindly trusting that
  // text would make the CV and cover-letter uploads indistinguishable to
  // apply.mjs's label-based file matcher, so a generic caption must defer
  // to the input's own id/name (e.g. id="resume" / id="cover_letter"),
  // which uniquely identifies the slot, before falling back further.
  const GENERIC_FILE_LABEL_RE = /^(attach(ed)?|choose file|browse|upload|select( a)? file|drop( or select)?( a)? file)s?\.{0,3}$/i;
  root.querySelectorAll("input[type=file]").forEach((el) => {
    seen.add(el);
    let label = labelFor(el);
    if ((!label || GENERIC_FILE_LABEL_RE.test(label.trim())) && (el.id || el.name)) {
      const idOrName = (el.id || el.name).replace(/[-_]+/g, " ").trim();
      if (idOrName) label = idOrName;
    }
    // Recruitee labels its CV input "fileInputField" and keeps the slot in the id/name ("candidate.cv"): when the label does not
    // itself say CV / resume / cover, the slot name decides which file goes in.
    const slot = `${el.id || ""} ${el.name || ""}`.replace(/[-_.]+/g, " ").trim();
    if (!/resume|cv|cover|letter|portfolio/i.test(label || "") && /resume|\bcv\b|cover/i.test(slot)) label = slot;
    label = label || el.name || "File upload";
    const idx = tag(el, "file");
    results.push({ idx, type: "file", label, required: isRequired(el, label) });
  });

  // ---- remaining plain text-like inputs -----------------------------------
  const textTypes = ["text", "email", "tel", "url", "number", "date", "search", ""];
  root.querySelectorAll("input").forEach((el) => {
    if (seen.has(el)) return;
    const type = (el.getAttribute("type") || "text").toLowerCase();
    if (!textTypes.includes(type) || el.disabled) return;
    if (!isVisible(el)) return;
    // aria-hidden + tabindex=-1 is a custom widget's backing input (Workable's name="CA_38431" beside its visible combobox) or a
    // honeypot: never a question, and never to be filled.
    if (el.getAttribute("aria-hidden") === "true" && el.getAttribute("tabindex") === "-1") return;
    const label = labelFor(el);
    // Date widgets are their own kind: react-datepicker inputs (Ashby's "Pick date...") take a calendar
    // pick, not free text -- a sentence typed into one is held by the form as "missing entry".
    const isDate = type === "date" || !!(el.closest && el.closest(".react-datepicker-wrapper, .react-datepicker__input-container"));
    const kind = isDate ? "date" : type === "email" ? "email" : type === "tel" ? "tel" : type === "number" ? "number" : "text";
    const idx = tag(el, kind);
    // A calling-code selector beside the box (Recruitee's PhoneInput: "Select country calling code: United Kingdom") means the box
    // wants the number WITH the country code: "This phone number is invalid ... including the country calling code".
    const intl = kind === "tel" && !!(el.closest(".PhoneInput, .iti, .react-tel-input")
      || (el.parentElement && el.parentElement.querySelector('[aria-label^="Select country calling code"]')));
    results.push({ idx, type: kind, label, required: isRequired(el, label), ...(intl ? { international: true } : {}) });
  });

  // Some ATS forms keep decoy/duplicate fields in the DOM (honeypots, or
  // stale template copies) that are EXACT duplicates -- same kind, same
  // label, same options -- of a real field, distinguished only by being at
  // opacity:0 while the real one is fully opaque. Drop the opacity:0
  // duplicates ONLY when a fully-opaque sibling with the identical
  // signature exists; a field that's opacity:0 with no opaque sibling is
  // assumed to be a legitimate not-yet-scrolled-into-view section (a
  // common fade-in-on-scroll pattern) and is kept, never discarded.
  if (doc.defaultView) {
    const groups = new Map();
    for (const r of results) {
      const key = r.label.replace(/[\s*✱]+$/, "").trim().toLowerCase();
      if (key.length < 2) continue; // never merge blank/near-blank labels
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    }
    const toDrop = new Set();
    for (const group of groups.values()) {
      if (group.length < 2) continue;
      const withOpacity = group.map((r) => {
        const el = root.querySelector(`[data-omp-field="${r.idx}"]`);
        const style = el && doc.defaultView.getComputedStyle(el);
        return { r, opacity: style ? parseFloat(style.opacity) : 1 };
      });
      const hasReal = withOpacity.some((w) => w.opacity !== 0);
      if (!hasReal) continue;
      for (const w of withOpacity) {
        if (w.opacity === 0) toDrop.add(w.r);
      }
    }
    if (toDrop.size) return results.filter((r) => !toDrop.has(r));
  }

  // ---- Headless UI listbox buttons (Helsing's own application form) ----
  // "Which office would you be applying to?" renders as
  // <button aria-haspopup="listbox" class="form-like-select ..."><span>Please select</span></button>
  // with no <select>, no input, and no options in the DOM until the button is
  // clicked -- every earlier pass and the server's own required-field
  // validation ("Please select") never meet. Emit the button itself as a
  // combobox: openCombobox already opens listboxes and reads [role=option].
  root.querySelectorAll("button[aria-haspopup='listbox']").forEach((el) => {
    if (seen.has(el)) return;
    // A phone input's own country selector (Recruitee: "Select country calling code: United Kingdom", inside .PhoneInput) is part
    // of the tel field, not a question: left in, its unfilled "United KingdomUnited Kingdom" text blocked causaLens at the gate.
    if (/^select country calling code/i.test(el.getAttribute("aria-label") || "") || el.closest(".PhoneInput, .iti, .react-tel-input")) {
      seen.add(el);
      return;
    }
    const btnText = (el.textContent || "").replace(/\s+/g, " ").trim();
    let label = "";
    for (let wrap = el.parentElement, i = 0; wrap && wrap !== root && i < 5; i++, wrap = wrap.parentElement) {
      // climb past the widget's own chrome until a level contributes real question text
      const t = (wrap.innerText || wrap.textContent || "").replace(btnText, " ").replace(/\s+/g, " ").trim();
      if (t.length >= 8 && /[a-z]{3}/i.test(t)) { label = t; break; }
    }
    if (!label) label = btnText.slice(0, 40);
    results.push({ idx: tag(el, "combobox"), type: "combobox", label, required: isRequired(el, label), options: [] });
    seen.add(el);
  });

  return results;
}

// ---------------------------------------------------------------------
// Live-browser wrappers (Puppeteer `page` via tab.run).
// ---------------------------------------------------------------------

const EXTRACT_SRC = extractFieldsFromDocument.toString();

export async function extractFields(tab, frameUrl) {
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      return await target.evaluate((src) => {
        const fn = new Function(`return (${src})`)();
        return fn(document, document.body);
      }, args.src);
    },
    { args: [{ src: EXTRACT_SRC, frameUrl: frameUrl || null }] }
  );
}

// Detects a same-origin-embeddable ATS application form living inside an
// iframe on the current top-level page (e.g. a Greenhouse "custom domain"
// job board that redirects back to the company's own careers page with the
// real form embedded via `/embed/job_app`). Only returns a frame when it
// has strictly MORE form controls than the top-level page itself -- a
// stray newsletter-signup or search box on the host page must never win
// over the substantially larger real application form embedded in an
// iframe. Returns the embedded frame's `url()` (a stable identifier
// reusable across calls) or null if the top-level page already has the
// larger (or only) form. Only ever inspects frame content to count form
// controls -- never fills/reads anything here.
export async function findEmbeddedFormFrame(tab) {
  return await tab.run(async ({ page }) => {
    const mainCount = await page.evaluate(() => document.querySelectorAll("input, select, textarea").length);
    const frames = page.frames().filter((f) => f !== page.mainFrame());
    let bestUrl = null;
    let bestCount = mainCount;
    for (const f of frames) {
      try {
        const count = await f.evaluate(() => document.querySelectorAll("input, select, textarea").length);
        if (count > bestCount) {
          bestCount = count;
          bestUrl = f.url();
        }
      } catch {
        // cross-origin frame that errored, or already navigated away; skip.
      }
    }
    return bestUrl;
  });
}

// ---- Opening the application form from a job-description page ------------
// Many landing pages (Gradcracker's /out targets, SmartRecruiters, careers sites) show the posting
// and an "Apply" button but no form until it is clicked. Pure decision functions below are exported
// for offline tests and injected verbatim into the page via toString() (same technique as
// EXTRACT_SRC); the browser part only clicks what they select.

// An UNAMBIGUOUS apply control. Third-party sign-in shortcuts and non-apply buttons never qualify.
export function isApplyCtaText(text) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  if (!t || t.length > 40) return false;
  if (/linkedin|indeed|google|facebook|apple|seek|save|share|alert|sign ?in|log ?in|register|create|refer/i.test(t)) return false;
  return /^(apply( now| online| online now| here| today| for (this )?(job|role|position|vacancy))?|start (your )?application|i'?m interested|apply with (email|resume|cv))\s*[.!»>›]*$/i.test(t);
}

// Does the extracted control list look like an application form (as opposed to a job page whose only
// controls are a site search box, cookie toggles or a newsletter field)?
export function looksLikeApplicationForm(descriptors) {
  return (descriptors || []).some((d) =>
    d.type === "file" || /e-?mail|first name|last name|full name|^name\b|phone|mobile|resume|\bcv\b/i.test(d.label || ""));
}

const CTA_SRC = `(() => {
  const isCta = ${isApplyCtaText.toString()};
  const els = [...document.querySelectorAll('a[href], button, [role="button"], input[type="button"], input[type="submit"]')].filter((el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none" && !el.disabled;
  });
  for (const el of els) {
    const t = el.innerText || el.value || el.getAttribute("aria-label") || el.textContent || "";
    if (!isCta(t)) continue;
    el.setAttribute("data-omp-cta", "1");
    const a = el.closest("a[href]");
    const href = a ? a.getAttribute("href") : null;
    return { text: t.trim(), href: href && /^https?:|^\\//i.test(href) ? a.href : null };
  }
  return null;
})()`;

// SPA application forms (SmartRecruiters OneClick, Workday-style shells) render well after the network
// goes idle; poll until the page, or any frame in it, shows real controls.
async function waitForControls(page, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    let n = 0;
    for (const f of page.frames()) {
      n += await f.evaluate(() => document.querySelectorAll("input, select, textarea").length).catch(() => 0);
    }
    if (n >= 3) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

export async function pageText(tab) {
  return await tab.run(async ({ page }) => page.evaluate(() => document.body ? document.body.innerText : ""));
}

// Pure: why a posting cannot be applied to, when the page says so ("No longer accepting applications"
// on Arcadis's careers page, which has no Apply control at all). null when nothing says it is closed.
export function postingClosedReason(text) {
  // Phrases seen on live ATS pages for a closed/removed posting: Arcadis "No longer accepting
  // applications"; Greenhouse "The job you are looking for is no longer open" / "The job board you
  // were viewing is no longer active"; Lever "The job posting you're looking for might have closed,
  // or it has been removed"; Ashby "Job not found".
  const m = String(text || "").match(/no longer accepting applications|no longer (?:open|active|available)|job not found|page not found|couldn'?t find anything here|might have closed|(?:has|have) been removed|(?:job|position|role|vacancy|posting)\s+(?:is |has been |was |has )?(?:now )?(?:closed|filled|expired)\b|applications? (?:for this \w+ )?(?:are |is |have |has )?(?:now )?closed/i);
  return m ? `Posting is closed: "${m[0]}"` : null;
}

// Returns {text, via, url} when an apply control was used, null when none exists. Always works in
// the SAME page object so the rest of the fill pipeline keeps its handle: an anchor is followed in
// place, a button is clicked with the real mouse, and a popup/new tab it spawns is loaded in the
// original page instead.
export async function openApplicationForm(tab) {
  return await tab.run(async ({ page }) => {
    // A page that is still mid-navigation throws "detached frame"; retry rather than give up.
    let cta = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        cta = await page.evaluate(CTA_SRC);
        break;
      } catch {
        await new Promise((r) => setTimeout(r, 1500));
      }
    }
    if (!cta) return null;
    if (cta.href) {
      await page.goto(cta.href, { waitUntil: "networkidle2", timeout: 45000 }).catch(() => null);
      await waitForControls(page);
      return { text: cta.text, via: "anchor", url: page.url() };
    }
    const browser = page.browser();
    let popup = null;
    const onTarget = (t) => { if (t.type() === "page" && !popup) popup = t; };
    browser.on("targetcreated", onTarget);
    try {
      const box = await page.evaluate(`(() => {
        const el = document.querySelector('[data-omp-cta]');
        el.scrollIntoView({ block: "center", behavior: "instant" });
        const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      })()`);
      await page.mouse.click(box.x, box.y);
      await new Promise((r) => setTimeout(r, 3000));
    } finally {
      browser.off("targetcreated", onTarget);
    }
    let via = "click";
    if (popup) {
      const p = await popup.page().catch(() => null);
      if (p) {
        await new Promise((r) => setTimeout(r, 2000));
        const url = p.url();
        await p.close().catch(() => null);
        if (url && url !== "about:blank") {
          await page.goto(url, { waitUntil: "networkidle2", timeout: 45000 }).catch(() => null);
          via = "popup";
        }
      }
    }
    await waitForControls(page);
    return { text: cta.text, via, url: page.url() };
  });
}

// Pure classifier: given an iframe's `src`, decides whether it's a CAPTCHA
// widget and whether that widget is currently presenting something a human
// must solve. Invisible/score-based/managed-non-interactive widgets run
// passively and never block; only a rendered checkbox/image-grid/interactive
// challenge blocks. Exported for offline unit tests and reused verbatim
// inside the live page.evaluate call via toString() injection (same
// technique as EXTRACT_SRC / extractFieldsFromDocument).
export function classifyCaptchaFrame(src) {
  const s = (src || "").toLowerCase();
  if (!s) return null;

  if (s.includes("recaptcha")) {
    // A rendered challenge (image grid) frame is inherently visible,
    // whatever size param it carries.
    if (s.includes("/bframe")) return { type: "reCAPTCHA", visible: true };
    const sizeMatch = s.match(/[?&]size=([a-z]+)/);
    if (sizeMatch) return { type: "reCAPTCHA", visible: sizeMatch[1] !== "invisible" };
    if (s.includes("/anchor")) return { type: "reCAPTCHA", visible: true };
    return null; // some other recaptcha-domain resource, not the widget itself
  }

  if (s.includes("hcaptcha")) {
    if (s.includes("checkbox")) return { type: "hCaptcha", visible: true };
    if (s.includes("challenge")) return { type: "hCaptcha", visible: true };
    return { type: "hCaptcha", visible: false }; // enclave / other background service frames
  }

  // DataDome (SmartRecruiters' OneClick form serves it to automated browsers: "You have been
  // blocked -- unusual activity"): a human-verification wall, never something to work around.
  if (s.includes("captcha-delivery.com") || s.includes("datadome")) {
    return { type: "DataDome", visible: true };
  }

  if (s.includes("turnstile") || s.includes("challenges.cloudflare")) {
    if (/appearance=always|size=(normal|compact|flexible)\b|interactive/.test(s)) {
      return { type: "Cloudflare Turnstile", visible: true };
    }
    return { type: "Cloudflare Turnstile", visible: false }; // managed / non-interactive by default
  }

  return null;
}

const CLASSIFY_CAPTCHA_SRC = classifyCaptchaFrame.toString();

export async function detectBlockers(tab) {
  return await tab.run(
    async ({ page }, args) => {
      return await page.evaluate((src) => {
        const classifyCaptchaFrame = new Function(`return (${src})`)();
        const reasons = [];
        let captcha = null;
        const captchaSeen = [];
        const iframes = Array.from(document.querySelectorAll("iframe"));
        for (const f of iframes) {
          const classified = classifyCaptchaFrame(f.src || "");
          if (!classified) continue;
          captchaSeen.push(classified);
          if (classified.visible && !captcha) captcha = `${classified.type} (visible challenge)`;
        }
        const password = !!document.querySelector("input[type=password]");
        const bodyText = document.body.innerText || "";
        const createAccount = password || /create\s+(an\s+)?account|sign\s*up\s+for\s+an\s+account|set\s+a\s+password|choose\s+a\s+password/i.test(bodyText);
        const idRe = /national insurance( number)?|\bni number\b|passport number|social security number|\bssn\b|driving licen[cs]e number|driver'?s licen[cs]e number|tax identification number|\bnino\b/i;
        const idNumbers = [];
        document.querySelectorAll("label, [aria-label]").forEach((el) => {
          const t = (el.textContent || el.getAttribute("aria-label") || "").trim();
          if (t && idRe.test(t)) idNumbers.push(t.replace(/\s+/g, " ").slice(0, 120));
        });
        return { captcha, captchaSeen, password, createAccount, idNumbers, reasons };
      }, args);
    },
    { args: [CLASSIFY_CAPTCHA_SRC] }
  );
}

// ---- Ashby keeps a server-side draft of the form ----------------------------------------------------
// Every edit is persisted with an `ApiSetFormValue` GraphQL mutation and submit validation reads that
// server copy. Mutations for one field can be in flight twice at once (an empty write on blur/commit and
// the typed value); if the server handles them out of order the empty one wins. Pace the fill so the
// previous write has landed before the next edit. Only Ashby is affected, so it is the only host waited on.
const _saveTrackers = new WeakMap();

function trackSaves(page) {
  let t = _saveTrackers.get(page);
  if (t) return t;
  t = { inflight: 0, last: Date.now(), seen: false };
  const isSave = (r) => r.method() === "POST" && /non-user-graphql\?op=ApiSetFormValue/.test(r.url());
  page.on("request", (r) => { if (isSave(r)) { t.seen = true; t.inflight++; t.last = Date.now(); } });
  const done = (r) => { if (isSave(r)) { t.inflight = Math.max(0, t.inflight - 1); t.last = Date.now(); } };
  page.on("requestfinished", done);
  page.on("requestfailed", done);
  _saveTrackers.set(page, t);
  return t;
}

export function isAshbyUrl(url) {
  try { return /(^|\.)ashbyhq\.com$/i.test(new URL(url).hostname); } catch { return false; }
}

// Resolves when no Ashby save is in flight and none has started for `quietMs` (or after `maxMs`).
export async function settleSaves(page, { quietMs = 700, maxMs = 6000 } = {}) {
  const t = trackSaves(page);   // always installed: an Ashby form embedded in a company site has a non-Ashby URL
  if (!t.seen && !isAshbyUrl(page.url())) return false;
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    if (t.inflight === 0 && Date.now() - t.last >= quietMs) return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  return false;
}

export async function settleFormSaves(tab, opts) {
  return await tab.run(async ({ page }) => settleSaves(page, opts));
}

// Which of a typeahead's suggestions to click: the first that starts with what was typed AND is in the UK (a same-named
// place abroad, e.g. "Springfield, VT, USA" vs "Springfield, England, GBR", must not win), else the first that starts with it, else the first.
export function pickLocationSuggestion(texts, wanted) {
  const w = String(wanted || "").trim().toLowerCase();
  const starts = (t) => String(t).toLowerCase().startsWith(w);
  const uk = (t) => /\b(GBR|England|Scotland|Wales|Northern Ireland|United Kingdom|UK)\b/i.test(t);
  const i = texts.findIndex((t) => starts(t) && uk(t));
  if (i >= 0) return i;
  const j = texts.findIndex(starts);
  return j >= 0 ? j : texts.length ? 0 : -1;
}
const PICK_LOCATION_SRC = pickLocationSuggestion.toString();

export async function setValue(tab, idx, value, frameUrl) {
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      const prep = await target.evaluate((a) => {
        const el = document.querySelector(`[data-omp-field="${a.idx}"]`);
        if (!el) return { ok: false, reason: "not found" };
        const kind = el.getAttribute("data-omp-kind");
        // Lever's "Current location": searches only on real keystrokes (one insertText shows "No location found") and
        // accepts only a CLICKED suggestion, which fills the hidden #selected-location the form validates.
        if (kind === "text" && el.id === "location-input" && document.querySelector("#selected-location")) {
          el.scrollIntoView && el.scrollIntoView({ block: "center" });
          el.focus();
          try { el.select(); } catch { /* the Backspace below clears it anyway */ }
          return { ok: "lever-location" };
        }
        if (kind === "text" || kind === "email" || kind === "tel" || kind === "number" || kind === "textarea") {
          // Real keyboard input, not synthetic value+Event dispatch: some
          // ATS form stores (observed: Ashby) never register a value that
          // arrived via programmatically-dispatched input/change events --
          // the DOM shows the value but their validation still reports the
          // required field as missing. el.focus() + real key events
          // (isTrusted, delivered through the browser's input pipeline) are
          // indistinguishable from a human typist. No hit-testing click is
          // involved: focus() is direct, so the embedded-iframe click-hang
          // risk openCombobox works around never applies here. The typing
          // itself happens below via the Puppeteer keyboard, which lands on
          // the focused element.
          el.scrollIntoView && el.scrollIntoView({ block: "center" });
          el.focus();
          // select() works on every text-entry input INCLUDING email/number, where setSelectionRange
          // throws -- the old catch left the field unselected, so insertText APPENDED to whatever the
          // ATS had prefilled (Spotify/Lever, 2026-10-02: email became "a@b.coma@b.com").
          try {
            el.select();
          } catch {
            try {
              if (el.setSelectionRange) el.setSelectionRange(0, el.value ? el.value.length : 0);
            } catch {
              // neither selection API available: the value is cleared below instead
            }
          }
          if (el.value && el.selectionStart !== el.selectionEnd) {
            // selection confirmed; insertText will replace it
          } else if (el.value) {
            // selection could not be made on this input type: clear it so the typed value stands alone
            const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(proto, "value").set.call(el, "");
            el.dispatchEvent(new Event("input", { bubbles: true }));
          }
          return { ok: "type", kind };
        }
        if (kind === "select") {
          el.focus && el.focus();
          const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
          const opt = Array.from(el.options).find((o) => o.textContent.trim() === a.value);
          setter.call(el, opt ? opt.value : a.value);
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return { ok: true };
        }
        return { ok: false, reason: "unsupported kind for setValue: " + kind };
      }, { idx: args.idx, value: args.value });

      if (prep && prep.ok === "lever-location") {
        const pick = new Function(`return (${args.pickSrc})`)();
        await page.keyboard.press("Backspace");
        await page.keyboard.type(String(args.value), { delay: 40 });
        const sel = ".dropdown-results > .dropdown-location";
        await target.waitForSelector(sel, { timeout: 5000 }).catch(() => null);
        const texts = await target.$$eval(sel, (els) => els.map((e) => e.textContent.trim()));
        const i = pick(texts, args.value);
        if (i < 0) return { ok: false, reason: "no location suggestion for " + args.value };
        const items = await target.$$(sel);
        await items[i].click();
        await new Promise((r) => setTimeout(r, 500));
        const registered = await target.evaluate(() => (document.querySelector("#selected-location") || {}).value || "");
        return registered ? { ok: true, picked: texts[i] } : { ok: false, reason: "the suggestion did not register" };
      }
      if (prep && prep.ok === "type") {
        // Replace any existing content and insert the wanted value via CDP
        // Input.insertText (keyboard.sendCharacter): trusted, IME-style
        // insertion. Regular keydown/keypress events are demonstrably
        // swallowed on some Ashby forms (observed 2026-09-27: every ASCII
        // keystroke silently dropped -- only characters Puppeteer routes
        // through insertText, like "£", ever landed), while insertText
        // reaches the field and fires the input pipeline normally. The
        // select-all in the evaluate above makes this a full replace.
        await page.keyboard.sendCharacter(String(args.value));
        // Ashby saves every field to its server as it is edited and validates that server copy at
        // submit. Typing fields back to back fired a burst of parallel saves, and a stale/empty write
        // for a field could land after its real value ("Missing entry for required field" for a random
        // subset of fields, ~4 runs in 6, 2026-10-02). Let this field's save land BEFORE the blur
        // (which can emit its own write) and again before the next field is touched. No-op elsewhere.
        await settleSaves(page);
        await target.evaluate((a) => {
          const el = document.querySelector(`[data-omp-field="${a.idx}"]`);
          if (el && el.blur) el.blur();
        }, { idx: args.idx });
        await settleSaves(page);
        return { ok: true };
      }
      return prep;
    },
    { args: [{ idx, value, frameUrl: frameUrl || null, pickSrc: PICK_LOCATION_SRC }] }
  );
}

// Sets a date field from an ISO date (YYYY-MM-DD). Native <input type=date> takes the ISO value through the
// native setter; react-datepicker (Ashby's "Pick date...") is driven like a person would: a real click opens
// the calendar, month navigation, then a real click on the day (the first selectable day on/after the
// wanted one -- weekends/holidays can be disabled). Typed text is NOT enough for those widgets.
export async function setDate(tab, idx, iso, frameUrl) {
  const [Y, M, D] = String(iso).split("-").map(Number);
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const sel = `[data-omp-field="${args.idx}"]`;
      const info = await target.evaluate(`(() => { const el = document.querySelector('${sel}'); if (!el) return null;
        return { native: el.type === "date", picker: !!el.closest(".react-datepicker-wrapper, .react-datepicker__input-container") }; })()`);
      if (!info) return { ok: false, reason: "not found" };
      if (info.native) {
        await target.evaluate(`(() => { const el = document.querySelector('${sel}');
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, "${args.iso}");
          el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); })()`);
        return { ok: true };
      }
      if (!info.picker) return { ok: false, reason: "unsupported date widget" };
      const centre = (js) => target.evaluate(`(() => { const e = ${js}; if (!e) return null; e.scrollIntoView({ block: "center", behavior: "instant" });
        const r = e.getBoundingClientRect(); return r.width > 0 ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : null; })()`);
      const input = await centre(`document.querySelector('${sel}')`);
      if (!input) return { ok: false, reason: "date input not visible" };
      await sleep(250);
      await page.mouse.click(input.x, input.y);
      await sleep(700);
      const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
      for (let i = 0; i < 14; i++) {
        // Month/year label: stock react-datepicker uses __current-month; Ashby's custom header renders an
        // <h4><span>October 2026</span></h4> instead -- read whatever the header says.
        const head = await target.evaluate(`((document.querySelector(".react-datepicker__header") || document.querySelector(".react-datepicker") || {}).innerText || "")`);
        const m = head.match(/([A-Za-z]+)\s+(\d{4})/);
        if (!m) return { ok: false, reason: "calendar did not open" };
        const cur = Number(m[2]) * 12 + MONTHS.indexOf(m[1]);
        const want = args.Y * 12 + (args.M - 1);
        if (cur === want) break;
        const dir = cur < want ? "next" : "previous";
        const nav = await centre(`document.querySelector('.react-datepicker button[data-direction="${dir}"], .react-datepicker__navigation--${dir}')`);
        if (!nav) return { ok: false, reason: "no month navigation" };
        await page.mouse.click(nav.x, nav.y);
        await sleep(250);
      }
      const day = await centre(`[...document.querySelectorAll(".react-datepicker__day:not(.react-datepicker__day--outside-month):not(.react-datepicker__day--disabled)")]
        .find((e) => Number(e.innerText.trim()) >= ${args.D})`);
      if (!day) return { ok: false, reason: "no selectable day on or after the wanted date" };
      await page.mouse.click(day.x, day.y);
      await sleep(500);
      return { ok: true };
    },
    { args: [{ idx, iso, Y, M, D, frameUrl: frameUrl || null }] }
  );
}

export async function setCheckbox(tab, idx, check, frameUrl) {
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      const read = () => target.evaluate(`(() => { const el = document.querySelector('[data-omp-field="${args.idx}"]'); return el ? !!el.checked : null; })()`);
      const cur = await read();
      if (cur === null) return { ok: false, reason: "not found" };
      if (cur === !!args.check) return { ok: true };
      // Same lesson as radios (Ashby, 2026-10-02): framework-controlled inputs can ignore a DOM-level
      // .click() while the DOM shows it checked. A real mouse click on the visible box reaches the
      // framework; boxes with no visible area (custom-styled checkboxes) fall back to .click().
      const box = await target.evaluate(`(() => {
        const el = document.querySelector('[data-omp-field="${args.idx}"]');
        el.scrollIntoView({ block: "center", behavior: "instant" });
        const r = el.getBoundingClientRect();
        return r.width > 2 && r.height > 2 ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : null;
      })()`).catch(() => null);
      if (box) {
        await new Promise((r) => setTimeout(r, 200));
        await page.mouse.click(box.x, box.y).catch(() => null);
        await new Promise((r) => setTimeout(r, 400));
        if ((await read()) === !!args.check) return { ok: true };
      }
      await target.evaluate(`(() => { const el = document.querySelector('[data-omp-field="${args.idx}"]'); if (el && !!el.checked !== ${!!args.check}) el.click(); })()`);
      return { ok: true };
    },
    { args: [{ idx, check, frameUrl: frameUrl || null }] }
  );
}

export async function setOptionState(tab, groupIdx, optionText, check, frameUrl) {
  const MATCH_SRC = `function (el, optionText) {
    let label = "";
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) label = lab.textContent.replace(/SVGs not supported by this browser\.?/gi, "").trim();
    }
    if (!label) {
      const wrap = el.closest("label");
      if (wrap) label = wrap.textContent.replace(/SVGs not supported by this browser\.?/gi, "").trim();
    }
    if (!label) label = el.getAttribute("aria-label") || el.value || el.textContent.trim() || "";
    return label.trim() === optionText;
  }`;
  const SET_SRC = `async (a) => {
    const opts = Array.from(document.querySelectorAll('[data-omp-field="' + a.groupIdx + '"][data-omp-opt]'));
    const match = ${MATCH_SRC};
    const el2 = opts.find((el) => match(el, a.optionText));
    if (!el2) return { ok: false, reason: "option not found" };
    const isRoleWidget = el2.getAttribute && (el2.getAttribute("role") === "radio" || el2.getAttribute("role") === "checkbox");
    const isPressedToggle = el2.hasAttribute && el2.hasAttribute("aria-pressed");
    const currentlyChecked = isRoleWidget
      ? el2.getAttribute("aria-checked") === "true"
      : isPressedToggle
        ? el2.getAttribute("aria-pressed") === "true"
        : !!el2.checked;
    if (isPressedToggle) {
      // Toggle buttons (Ashby's Yes/No pairs): the state shown by aria-pressed after a synthetic
      // click is not what Ashby's form store holds (2026-10-02: submit reported "Missing entry for
      // required field" for toggles the DOM showed as pressed). Like native radios, leave the click
      // to the real-mouse pass below -- unless the target is already pressed.
    } else if (el2.tagName === "INPUT" && el2.type === "radio") {
      // Native radios (observed: Ashby, 2026-10-02): a synthetic click reads
      // back as checked while the framework store misses it, and the real
      // click that follows then cancels it. Leave it to the real-mouse pass.
    } else if (currentlyChecked !== !!a.check) {
      el2.click();
    }
    return { ok: true };
  }`;
  const BOX_SRC = `async (a) => {
    const opts = Array.from(document.querySelectorAll('[data-omp-field="' + a.groupIdx + '"][data-omp-opt]'));
    const match = ${MATCH_SRC};
    const el = opts.find((el) => match(el, a.optionText));
    if (!el) return null;
    // A custom-styled radio/checkbox (Jane Street: a display:none input plus a styled <label for>) has no box of its
    // own, so a click at its centre lands on nothing (0,0): click the label that stands in for it.
    let clickTarget = el;
    const own = el.getBoundingClientRect();
    if (own.width === 0 || own.height === 0) {
      const lab = (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest("label");
      if (lab) clickTarget = lab;
    }
    clickTarget.scrollIntoView({ block: "center", behavior: "instant" });
    const r = clickTarget.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }`;
  const args = { groupIdx, optionText, check, frameUrl: frameUrl || null };
  const res = await tab.run(
    async ({ page }, a) => {
      const target = a.frameUrl ? page.frames().find((f) => f.url() === a.frameUrl) || page : page;
      return await target.evaluate(`(${SET_SRC})(${JSON.stringify(a)})`);
    },
    { args: [args] }
  );
  // A DOM-level .click() can leave a framework-controlled radio visibly
  // checked while the framework's own store misses it (observed: Ashby,
  // 2026-10-01 -- the site's submit validation rejected a radio the DOM
  // reported as checked). A real mouse click on the option's centre goes
  // through the browser's full input pipeline and always reaches the
  // framework; only used when the cheap in-page pass did not stick.
  const after = await readOptionState(tab, groupIdx, optionText, frameUrl).catch(() => null);
  if (!res || res.ok === false) return res;
  if (after === !!check) return res;
  const box = await tab.run(
    async ({ page }, a) => {
      const target = a.frameUrl ? page.frames().find((f) => f.url() === a.frameUrl) || page : page;
      const center = await target.evaluate(`(${BOX_SRC})(${JSON.stringify(a)})`).catch(() => null);
      if (center && Number.isFinite(center.x) && Number.isFinite(center.y)) {
        // Let any scroll/layout shift finish, then re-measure: coordinates
        // taken mid-scroll land the click on the wrong element.
        await new Promise((r) => setTimeout(r, 250));
        const settled = await target.evaluate(`(${BOX_SRC})(${JSON.stringify(a)})`).catch(() => null);
        const pt = settled && Number.isFinite(settled.x) ? settled : center;
        await page.mouse.click(pt.x, pt.y).catch(() => null);
        // Frameworks re-render asynchronously after a real click; an
        // immediate read-back races the state write (observed on Ashby).
        await new Promise((r) => setTimeout(r, 500));
      }
      return center;
    },
    { args: [args] }
  ).catch(() => null);
  if (!box) {
    return { ok: false, reason: "real-mouse fallback unavailable; option left as the in-page click set it" };
  }
  return res;
}

export async function readValue(tab, idx, frameUrl) {
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      return await target.evaluate((a) => {
        const el = document.querySelector(`[data-omp-field="${a.idx}"]`);
        if (!el) return null;
        const kind = el.getAttribute("data-omp-kind");
        if (kind === "select") {
          const opt = el.options[el.selectedIndex];
          return opt ? opt.textContent.trim() : "";
        }
        if (kind === "checkbox") return el.checked;
        if (kind === "combobox") {
          // react-select (and similar widgets) clear the underlying
          // input's `.value` immediately after a pick and render the
          // choice into a separate "selected value" node instead --
          // reading `el.value` here would always see the pick as failed.
          // Scope to the widget's own "control" wrapper (react-select-
          // style BEM naming: "...__control"/"...-control") -- almost
          // every ancestor div in these component trees also contains
          // "select" in its class name, so `closest("[class*='select']")`
          // matches the input's own immediate wrapper and never reaches
          // the sibling node that actually renders the selected value.
          const control = el.closest("[class*='__control'], [class*='-control']") || el.parentElement || el;
          const displayEl =
            control.querySelector("[class*='single-value'], [class*='singleValue']") ||
            control.querySelector("[class*='selected-value'], [class*='selectedValue']");
          if (displayEl && displayEl.textContent.trim()) return displayEl.textContent.trim();
          // Multi-select variants (Graphcore's gender question, 2026-10-02) render each pick as a
          // "multi-value" chip with its own label node: the pick DID register but this read-back
          // saw no single-value node and reported it as failed.
          const chips = Array.from(control.querySelectorAll("[class*='multi-value__label'], [class*='multiValue__label']"))
            .map((n) => n.textContent.trim()).filter(Boolean);
          if (chips.length) return chips.join(", ");
          const shown = el.value || el.textContent.trim() || "";
          // A listbox button shows its placeholder ("Please select") as its
          // text content: that is an EMPTY control, not a value -- reading it
          // as one made the pre-submit gate flag an "unverifiable autofill".
          return /^(please )?select\b|^choose\b|^--$/i.test(shown) ? "" : shown;
        }
        return el.value;
      }, args);
    },
    { args: [{ idx, frameUrl: frameUrl || null }] }
  );
}

export async function readOptionState(tab, groupIdx, optionText, frameUrl) {
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      return await target.evaluate((a) => {
        const opts = Array.from(document.querySelectorAll(`[data-omp-field="${a.groupIdx}"][data-omp-opt]`));
        const el = opts.find((el) => {
          let label = "";
          if (el.id) {
            const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (lab) label = lab.textContent.replace(/SVGs not supported by this browser\.?/gi, "").trim();
          }
          if (!label) {
            const wrap = el.closest("label");
            if (wrap) label = wrap.textContent.replace(/SVGs not supported by this browser\.?/gi, "").trim();
          }
          if (!label) label = el.getAttribute("aria-label") || el.value || el.textContent.trim() || "";
          return label.trim() === a.optionText;
        });
        if (!el) return null;
        const isRoleWidget = el.getAttribute && (el.getAttribute("role") === "radio" || el.getAttribute("role") === "checkbox");
        // Toggle-button option widgets (observed: Ashby's yes/no pairs)
        // carry their state in aria-pressed, not aria-checked or .checked.
        if (el.hasAttribute && el.hasAttribute("aria-pressed")) return el.getAttribute("aria-pressed") === "true";
        return isRoleWidget ? el.getAttribute("aria-checked") === "true" : !!el.checked;
      }, args);
    },
    { args: [{ groupIdx, optionText, frameUrl: frameUrl || null }] }
  );
}

// Reads a control's CURRENT value for the pre-submit snapshot (every
// control on the form, not just ones apply.mjs filled). `readValue`'s
// single-element selector works for every kind except radio/checkbox-group,
// whose shared `idx` is the GROUP anchor -- resolve those by checking each
// offered option's own state instead. Radio resolves to the one checked
// option's text (or null); checkbox-group resolves to the list of checked
// option texts (possibly empty).
export async function getFieldValueForSnapshot(tab, d, frameUrl) {
  if (d.type === "radio" || d.type === "checkbox-group") {
    const selected = [];
    for (const opt of d.options || []) {
      const checked = await readOptionState(tab, d.idx, opt, frameUrl).catch(() => false);
      if (checked) selected.push(opt);
    }
    return d.type === "radio" ? selected[0] || null : selected;
  }
  if (d.type === "file") return null; // represented via apply.mjs's own uploads[] instead
  return await readValue(tab, d.idx, frameUrl);
}

// Pure classifier: given a `document`-like object, the selector the file
// input was originally tagged with, and the uploaded filename, decides
// whether the upload can be confirmed. Exported so it is directly
// unit-testable offline (with linkedom) as well as reused verbatim inside
// the live page.evaluate call below via toString() injection (same
// technique as EXTRACT_SRC / CLASSIFY_CAPTCHA_SRC / SUBMIT_CANDIDATE_SRC).
export function isFileUploadConfirmed(doc, selector, filename) {
  const el = doc.querySelector(selector);
  if (el) {
    if (el.files && el.files.length && el.files[0].name === filename) return true;
    let container = el.closest("form") || el.parentElement;
    let depth = 0;
    while (container && depth < 4) {
      if ((container.textContent || "").includes(filename)) return true;
      container = container.parentElement;
      depth++;
    }
  }
  // The tagged file input itself may have been unmounted by the page's own
  // JS right after a successful upload (e.g. Greenhouse's embedded widget
  // swaps the <input type=file> for a "file attached" display showing the
  // filename, with no element left at the old data-omp-field selector) --
  // a missing element must never be read as "upload failed" without first
  // checking whether the uploaded filename is now visible anywhere on the
  // page.
  return (doc.body.textContent || "").includes(filename);
}

const CONFIRM_UPLOAD_SRC = isFileUploadConfirmed.toString();

// Live wrapper around isFileUploadConfirmed: does the tagged input already
// carry (or does the page already show) this exact file? apply.mjs checks
// this BEFORE calling uploadFile so a retry run never re-fires the file
// input's change event -- re-uploading the same resume re-triggers the
// ATS's own asynchronous resume parsing (observed: Ashby's autofill
// re-render), which remounts/clears already-filled fields mid-fill.
export async function isUploadConfirmed(tab, idx, filePath, frameUrl) {
  const selector = `[data-omp-field="${idx}"]`;
  const filename = filePath.split(/[\\/]/).pop();
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      return await target.evaluate((a) => {
        const fn = new Function(`return (${a.src})`)();
        return fn(document, a.selector, a.filename);
      }, args);
    },
    { args: [{ selector, filename, frameUrl: frameUrl || null, src: CONFIRM_UPLOAD_SRC }] }
  );
}

export async function uploadFile(tab, idx, filePath, frameUrl) {
  const selector = `[data-omp-field="${idx}"]`;
  await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      const handle = await target.$(args.selector);
      if (!handle) throw new Error("file input not found: " + args.selector);
      await handle.uploadFile(args.filePath);
    },
    { args: [{ selector, filePath, frameUrl: frameUrl || null }] }
  );
  const filename = filePath.split(/[\\/]/).pop();
  // The page's own JS (change-event handler, React state update, upload
  // network request, etc.) that flips the input/DOM into a confirmable
  // "attached" state runs asynchronously after `handle.uploadFile()`
  // resolves -- a single immediate check races that update and can read a
  // genuinely successful upload as failed. Poll briefly instead of
  // checking exactly once.
  const deadline = Date.now() + 3000;
  let confirmed = false;
  while (Date.now() < deadline) {
    confirmed = await tab.run(
      async ({ page }, args) => {
        const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
        return await target.evaluate((a) => {
          const fn = new Function(`return (${a.src})`)();
          return fn(document, a.selector, a.filename);
        }, args);
      },
      { args: [{ selector, filename, frameUrl: frameUrl || null, src: CONFIRM_UPLOAD_SRC }] }
    );
    if (confirmed) break;
    await new Promise((r) => setTimeout(r, 150));
  }
  return confirmed;
}

// Pure helper: matches a wanted value against a list of scraped combobox
// option texts, trimmed/lowercased, allowing either side to be a prefix of
// the other (react-select and similar widgets routinely render an option
// as "United Kingdom (+44)" when the wanted value is just "United
// Kingdom", or vice versa). Exported so it's directly unit-testable
// offline as well as reused by apply.mjs's fill loop.
export function matchComboboxOption(options, wanted) {
  if (!options || !options.length || !wanted) return -1;
  const w = String(wanted).trim().toLowerCase();
  let idx = options.findIndex((o) => String(o).trim().toLowerCase() === w);
  if (idx !== -1) return idx;
  idx = options.findIndex((o) => String(o).trim().toLowerCase().startsWith(w));
  if (idx !== -1) return idx;
  idx = options.findIndex((o) => w.startsWith(String(o).trim().toLowerCase()) && String(o).trim().length > 0);
  return idx;
}

// Opens a custom combobox widget (role=combobox, e.g. react-select) and
// collects its option texts. Greenhouse's react-select-style forms only
// set aria-controls once the menu has actually opened (an async render),
// so a fixed short wait plus "grab the first [role=listbox] anywhere on
// the page" races the render and can pick up an unrelated widget's menu.
// Instead: poll for up to ~2s, and scope option discovery to the
// combobox's own container/aria-controls target rather than the whole
// document. `typeText`, when given, is typed into the combobox first --
// useful for async/long option lists that only populate once filtered.
export async function openCombobox(tab, idx, frameUrl, typeText) {
  const selector = `[data-omp-field="${idx}"]`;
  await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      // A previous field's combobox menu may still be open/lingering in
      // the DOM (some widgets don't reliably close on Escape) -- force it
      // closed and strip any stale `data-omp-combo-opt` tags before
      // opening the next one, so pickComboboxOption can never click a
      // leftover option element from an earlier, unrelated field.
      await target.evaluate(() => {
        document.querySelectorAll("[data-omp-combo-opt]").forEach((el) => el.removeAttribute("data-omp-combo-opt"));
        if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      }).catch((e) => console.log("TYPE ERROR:", e));
      await page.keyboard.press("Escape").catch((e) => console.log("TYPE ERROR:", e));
      // Puppeteer's own `Frame#click` performs a visibility/intersection
      // check and a real hit-test click at the element's screen
      // coordinates -- observed to hang indefinitely for a combobox
      // nested inside a same-origin embedded iframe (Greenhouse's
      // `/embed/job_app`). Open the widget purely via the DOM instead:
      // focus it directly (no hit-testing) and press ArrowDown, which is
      // the standard accessible way react-select-style comboboxes expect
      // to be opened and never hangs.
      await target.evaluate((sel) => {
        const el = document.querySelector(sel);
        if (el) el.focus();
      }, args.selector);
      await page.keyboard.press("ArrowDown").catch((e) => console.log("TYPE ERROR:", e));
      // Some non-react-select comboboxes only open on an actual click --
      // dispatch one purely in JS (no Puppeteer hit-testing/hang risk). ONLY
      // when the ArrowDown did not already open a listbox: on a Headless UI
      // button the click is a TOGGLE and would close the just-opened menu
      // (Helsing's office dropdown rendered 7 options, then the mouse events
      // shut it before the read loop looked).
      await target.evaluate((sel) => {
        const el = document.querySelector(sel);
        if (!el) return;
        const visible = [...document.querySelectorAll("[role=option], [role=listbox]")]
          .some((o) => o.offsetParent || o.getClientRects().length);
        if (visible) return;
        for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
          el.dispatchEvent(type.startsWith("pointer")
            ? new PointerEvent(type, { bubbles: true, cancelable: true, pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0 })
            : new MouseEvent(type, { bubbles: true, cancelable: true, view: window, button: 0 }));
        }
      }, args.selector).catch((e) => console.log("TYPE ERROR:", e));
      if (args.typeText) {
        await target.type(args.selector, args.typeText, { delay: 20 }).catch((e) => console.log("TYPE ERROR:", e));
      }
    },
    { args: [{ selector, frameUrl: frameUrl || null, typeText: typeText || null }] }
  );
  const deadline = Date.now() + 2000;
  let options = [];
  while (Date.now() < deadline) {
    options = await tab.run(
      async ({ page }, args) => {
        const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
        return await target.evaluate((a) => {
          const el = document.querySelector(a.selector);
          if (!el) return [];
          let scope = null;
          const owns = el.getAttribute("aria-owns") || el.getAttribute("aria-controls");
          if (owns) {
            for (const id of owns.split(/\s+/)) {
              const found = document.getElementById(id);
              if (found) { scope = found; break; }
            }
          }
          if (!scope) {
            // Scope to the combobox's own visual container (react-select
            // renders the open menu as a sibling of the input, inside the
            // same "select__control"/"combobox" wrapper) before ever
            // falling back to the whole document. Search from the
            // PARENT, not the input itself -- the input's own class
            // (e.g. "select__input") frequently contains "select" too and
            // would otherwise match itself, which has no descendants to
            // search.
            const start = el.parentElement || el;
            const container =
              start.closest("[class*='select']") || start.closest("[class*='combobox']") || start.closest("[class*='dropdown']") || start;
            if (container) {
              scope = container.querySelector("[role=listbox], [class*='-menu'], [class*='__menu']") || container;
            }
          }
          const isVisible = (o) => !!(o.offsetParent || o.getClientRects().length);
          let opts = scope ? Array.from(scope.querySelectorAll("[role=option], [id*='-option-']")).filter(isVisible) : [];
          if (!opts.length) {
            // Global fallback: some widgets (intl-tel-input-style phone
            // country pickers) keep their option list permanently in the
            // DOM, just hidden via CSS, rather than removing it on close --
            // an invisible leftover list must never be mistaken for the
            // combobox that was actually just opened.
            opts = Array.from(document.querySelectorAll("[role=option], [id*='-option-']")).filter(isVisible);
          }
          return opts.map((o, i) => {
            o.setAttribute("data-omp-combo-opt", String(i));
            return o.textContent.replace(/SVGs not supported by this browser\.?/gi, "").trim();
          });
        }, args);
      },
      { args: [{ selector, frameUrl: frameUrl || null }] }
    );
    if (options.length) break;
    await new Promise((r) => setTimeout(r, 150));
  }
  return options;
}

export async function pickComboboxOption(tab, optionIndex, frameUrl) {
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      return await target.evaluate((a) => {
        const el = document.querySelector(`[data-omp-combo-opt="${a.optionIndex}"]`);
        if (!el) return false;
        // react-select-style widgets select an option on `mousedown` (to
        // beat the input's own blur handler), not `click` -- a bare
        // `el.click()` synthesizes a click event only and is silently
        // ignored by these widgets. Dispatch the full, real sequence.
        for (const type of ["mousedown", "mouseup", "click"]) {
          el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, button: 0 }));
        }
        return true;
      }, args);
    },
    { args: [{ optionIndex, frameUrl: frameUrl || null }] }
  );
}


export async function closeCombobox(tab) {
  await tab.run(async ({ page }) => {
    await page.keyboard.press("Escape");
  });
}

// Live-only: best-effort location/workplace-type hint scraped from the
// current page itself, to correct a job.json region that only ever saw the
// aggregator's bare country (see field_rules.mjs's correctRegion). Checks
// both any embedded __NEXT_DATA__ payload (Rippling) and the page's own
// visible text (Ashby/Greenhouse commonly show a "Hybrid — London, UK"
// style badge near the title).
export async function extractRegionHint(tab, frameUrl) {
  return await tab.run(
    async ({ page }, args) => {
      const target = args.frameUrl ? page.frames().find((f) => f.url() === args.frameUrl) || page : page;
      return await target.evaluate(() => {
        let text = "";
        const nextData = document.getElementById("__NEXT_DATA__");
        if (nextData) text += " " + (nextData.textContent || "");
        text += " " + (document.body.innerText || "").slice(0, 3000);
        return text;
      });
    },
    { args: [{ frameUrl: frameUrl || null }] }
  );
}

// Some ATS platforms (observed: Ashby) keep processing an already-confirmed
// upload asynchronously and reject a Submit click made while that lag is
// still happening -- entirely independently of any visible page text
// (waitForUploadsToSettle below cannot detect it at all). A flat, constant
// pause after the LAST confirmed upload and before clickSubmit is the only
// reliable mitigation observed.
export const POST_UPLOAD_SETTLE_MS = 5000;
export async function waitAfterUploads(ms = POST_UPLOAD_SETTLE_MS) {
  await new Promise((r) => setTimeout(r, ms));
}

// Pure predicate: given the page's visible text, decides whether an upload
// is still in flight (e.g. Ashby's own client-side JS shows "We're updating
// your application (e.g. uploading files), please try again when they're
// finished." and rejects a Submit click while it's up, even though
// `isFileUploadConfirmed` already saw the file attached in the DOM -- the
// widget's own async processing can lag behind the DOM attribute we check).
// Exported so it is directly unit-testable offline as well as reused
// verbatim inside the live page.evaluate call via toString() injection.
export function isUploadInProgressText(text) {
  return /uploading|please try again when.*finished|upload.*progress/i.test(text || "");
}

const UPLOAD_IN_PROGRESS_SRC = isUploadInProgressText.toString();

// Polls the page's visible text for up to `timeoutMs` (checking every
// `intervalMs`) until no upload-in-progress warning is showing. Must run
// after every upload has already been confirmed via `isFileUploadConfirmed`
// and right before `clickSubmit` -- some ATS platforms (observed: Ashby)
// keep processing an attached file asynchronously after the DOM already
// reflects it as attached, and silently reject a Submit click made during
// that window instead of surfacing a normal validation error.
export async function waitForUploadsToSettle(tab, { timeoutMs = 10000, intervalMs = 300 } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const stillUploading = await tab.run(
      async ({ page }, args) => {
        return await page.evaluate((src) => {
          const isUploadInProgressText = new Function(`return (${src})`)();
          return isUploadInProgressText(document.body.innerText);
        }, args);
      },
      { args: [UPLOAD_IN_PROGRESS_SRC] }
    );
    if (!stillUploading) return true;
    if (Date.now() >= deadline) return false;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
// Pure classifier: given a button/link's accessible text, decides whether it
// is the real "submit this application" control. Exported so it is directly
// unit-testable offline (see test_rules.mjs) as well as reused verbatim
// inside the live page.evaluate call below via toString() injection (same
// technique as EXTRACT_SRC / extractFieldsFromDocument).
export function isSubmitButtonCandidate(text) {
  const good = /submit application|submit your application|submit|apply now|send application|send your application|^send$/i;
  const bad = /save|draft|back|previous|cancel|upload|browse|remove|delete|apply with linkedin|apply with indeed|autofill|auto-fill|import (from|resume)|indeed|linkedin/i;
  if (!text) return false;
  if (bad.test(text)) return false;
  return good.test(text);
}

const SUBMIT_CANDIDATE_SRC = isSubmitButtonCandidate.toString();

// Which candidate is the real submit button. Wheely's Greenhouse page has an "Apply Now" button at the TOP (it only scrolls to the
// form and focuses First Name): the old "first match in DOM order" clicked it twice and every attempt failed silently. Strong
// wording inside the form wins, then strong wording anywhere, then any candidate inside the form, then the LAST candidate (a submit
// button sits at the bottom; a page-top "Apply" button does not). items: [{ text, inForm }] in DOM order -> index or -1.
export function pickSubmitCandidate(items) {
  const strong = (t) => /submit (your )?application|send (your )?application|complete (your )?application|^submit$|^apply$|^send$/i.test(String(t).trim());
  const tiers = [
    (x) => x.inForm && strong(x.text),
    (x) => strong(x.text),
    (x) => x.inForm,
    () => true,
  ];
  for (const ok of tiers) {
    for (let i = items.length - 1; i >= 0; i--) if (ok(items[i])) return i;
  }
  return -1;
}
const PICK_SUBMIT_SRC = pickSubmitCandidate.toString();

// Finds the real submit control. When the application form lives in an
// embedded iframe (frameUrl from findEmbeddedFormFrame), the candidate
// search runs INSIDE that frame only and never falls back to the top-level
// page: host pages around embedded ATS forms routinely carry their own
// "Apply Now" button/link that isSubmitButtonCandidate also matches, and
// clicking it navigates the host page (observed: NewRocket/Greenhouse
// embed, first submit attempt clicked the parent's Apply Now and landed
// back on the job description while the iframe form was never submitted).
export async function findSubmitControl(tab, frameUrl) {
  return await tab.run(
    async ({ page }, args) => {
      let target = page;
      if (args.frameUrl) {
        const frame = page.frames().find((f) => f.url() === args.frameUrl);
        if (!frame) return { found: false, text: null, reason: "form frame no longer present" };
        target = frame;
      }
      return await target.evaluate((src, pickSrc) => {
        const isSubmitButtonCandidate = new Function(`return (${src})`)();
        const pickSubmitCandidate = new Function(`return (${pickSrc})`)();
        const tagged = document.querySelector("[data-omp-field]");
        const form = tagged && tagged.closest("form");
        const found = [];
        for (const el of document.querySelectorAll("button, input[type=submit], [role=button]")) {
          const text = (el.textContent || el.value || el.getAttribute("aria-label") || "").trim();
          if (text && isSubmitButtonCandidate(text)) found.push({ el, text, inForm: !!(form && form.contains(el)) });
        }
        const i = pickSubmitCandidate(found.map((f) => ({ text: f.text, inForm: f.inForm })));
        if (i < 0) return { found: false, text: null };
        found[i].el.setAttribute("data-omp-submit", "1");
        return { found: true, text: found[i].text };
      }, args.src, args.pickSrc);
    },
    { args: [{ frameUrl: frameUrl || null, src: SUBMIT_CANDIDATE_SRC, pickSrc: PICK_SUBMIT_SRC }] }
  );
}

// Pure helper: extracts the verbatim Greenhouse email-verification
// interstitial prompt ("A verification code was sent to ... enter the
// 8-character code to confirm you're a human.") from page text, or null.
// Observed on the NewRocket/Greenhouse embed: this prompt appears right
// after clicking Submit, disables the submit button, and can auto-resolve
// to the confirmation page within ~40s WITHOUT the code being entered --
// so its presence mid-poll is not a failure, but one still showing when
// polling exhausts is a hard blocker worth surfacing verbatim.
export function verificationPromptText(text) {
  const m = String(text || "").match(/[^\n]*verification code was sent[^\n]*/i);
  return m ? m[0].trim() : null;
}

// Clicks the tagged submit control -- inside the embedded form frame when
// one is in play (frameUrl), never on the host page -- then polls the SAME
// target every second for up to `timeoutMs` for a terminal state:
// success (URL/text confirmation), validation errors, or a visible
// captcha. The long poll matters because embedded Greenhouse forms can
// sit on the email-verification interstitial for tens of seconds before
// the frame navigates to /confirmation; a single early read sees only the
// interstitial and false-negatives a submission that actually went
// through. Returns the same shape detectConfirmation consumes, with the
// frame's own URL as `url` (and the frame's pre-click URL as `before`, so
// detectConfirmation's urlChanged comparison stays meaningful).
// Text of every frame of the page, the submit frame first. A confirmation can render in a frame the poller was not
// watching while the top document is just site chrome: Stripe's embedded Greenhouse form confirmed both applications
// (the inbox got "Thanks for applying to Stripe!") while the runner read only the host page and called them failed.
export async function allFramesText(page, target) {
  const grab = (f) => f.evaluate(() => (document.body ? document.body.innerText : "").slice(0, 20000)).then(stripBlobs).catch(() => "");
  const parts = [await grab(target)];
  for (const f of page.frames()) {
    // hCaptcha's own frame renders its script source as visible text: it buried Spotify's real cause under JavaScript.
    if (f === target || classifyCaptchaFrame(f.url())) continue;
    const t = await grab(f);
    if (t) parts.push(t);
  }
  return parts.join("\n");
}

// True when the submit's own traffic shows a captcha challenge and nothing else was accepted: Lever answered Submit with an hCaptcha
// challenge and the application POST never happened (Spotify, 2026-10-03). A bot wall -- reported, never solved, never retried.
export function captchaBlockedSubmit(network) {
  const list = Array.isArray(network) ? network : [];
  // any host: Recruitee serves hCaptcha through its own CDN (captcha-base.recruiteecdn.com/getcaptcha/...)
  const isCaptcha = (n) => /\/(getcaptcha|checkcaptcha)\b|hcaptcha/i.test(n.url);
  const challenged = list.some(isCaptcha);
  const accepted = list.some((n) => !isCaptcha(n) && n.status < 400);
  return challenged && !accepted;
}
const CAPTCHA_WALL_MESSAGE = "A captcha challenge was raised after Submit and the application was not sent: the site wants a human check (bot wall), not retried";

export async function clickSubmit(tab, frameUrl, { timeoutMs = 45000, intervalMs = 1000 } = {}) {
  const before = await tab.run(async ({ page }, args) => {
    if (!args.frameUrl) return page.url();
    const frame = page.frames().find((f) => f.url() === args.frameUrl);
    return frame ? frame.url() : page.url();
  }, { args: [{ frameUrl: frameUrl || null }] });

  // Evidence of what the site received and answered: every non-analytics POST made while submitting, with its status (and body
  // when it failed). A silent refusal, or a success page the text matcher does not know, is otherwise a guess.
  const network = [];
  const onResponse = async (res) => {
    try {
      if (res.request().method() !== "POST") return;
      const u = res.url();
      if (/google|gstatic|doubleclick|facebook|segment|sentry|datadog|hotjar|clarity|newrelic|intercom|cloudflare|recaptcha|analytics|launchdarkly|mixpanel|amplitude|posthog|fullstory/i.test(u)) return;
      const entry = { url: u.slice(0, 140), status: res.status() };
      if (res.status() >= 400) entry.body = (await res.text().catch(() => "")).replace(/\s+/g, " ").slice(0, 300);
      network.push(entry);
      if (network.length > 12) network.shift();
    } catch { /* a diagnostic must never break a submit */ }
  };
  await tab.run(async ({ page }) => { page.on("response", onResponse); });

  await tab.run(async ({ page }, args) => {
    const target = args.frameUrl
      ? page.frames().find((f) => f.url() === args.frameUrl) || page
      : page;
    await target.evaluate(() => {
      const el = document.querySelector("[data-omp-submit]");
      if (el) el.click();
    });
  }, { args: [{ frameUrl: frameUrl || null }] });

  const readState = async ({ page }, args) => {
    let target = page;
    if (args.frameUrl) {
      // The frame may legitimately navigate (e.g. to .../confirmation)
      // after the click -- re-find it by prefix, not exact URL.
      target = page.frames().find((f) => f.url().startsWith(args.frameUrl.slice(0, 60))) || page.frames().find((f) => f.url() === args.frameUrl) || page;
    }
    return {
      url: target.url(),
      text: await allFramesText(page, target),
      errors: await target.evaluate(() => {
        const errEls = Array.from(
          document.querySelectorAll("[role=alert], .error, .field_error, [aria-invalid=true], .validation-error")
        );
        return errEls
          .map((e) => (e.textContent || "").trim())
          .filter((t) => t.length > 0)
          .slice(0, 20);
      }),
      // Never interact with a challenge if one appears -- only detect it
      // so detectConfirmation can report submit_failed instead of a false
      // "submitted".
      visibleCaptcha: await page.evaluate((src) => {
        const classifyCaptchaFrame = new Function(`return (${src})`)();
        const iframes = Array.from(document.querySelectorAll("iframe"));
        for (const f of iframes) {
          const classified = classifyCaptchaFrame(f.src || "");
          if (classified && classified.visible) return classified.type;
        }
        return null;
      }, args.src),
    };
  };

  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, intervalMs));
    // A navigation mid-read ("Execution context was destroyed") is usually the confirmation page loading: keep polling instead of
    // crashing, because a crash AFTER the click leaves no attempt record and the next retry could send a duplicate.
    const state = await tab.run(readState, { args: [{ frameUrl: frameUrl || null, src: CLASSIFY_CAPTCHA_SRC }] }).catch(() => null);
    if (!state) continue;
    last = state;
    const verdict = detectConfirmation({ before, ...last });
    if (verdict.success || (last.errors && last.errors.length) || last.visibleCaptcha) break;
    // Keep polling through the email-verification interstitial -- it can
    // auto-resolve into the confirmation page without any code entry.
  }
  const final = last || (await tab.run(readState, { args: [{ frameUrl: frameUrl || null, src: CLASSIFY_CAPTCHA_SRC }] })
    .catch(() => ({ url: before, text: "", errors: [], visibleCaptcha: null })));
  const verification = verificationPromptText(final.text);
  await tab.run(async ({ page }) => { page.off("response", onResponse); }).catch(() => null);
  return {
    before,
    ...final,
    // a captcha puzzle is the cause; whatever the page behind it says ("All fields marked with * are required") is not
    errors: captchaBlockedSubmit(network) ? [CAPTCHA_WALL_MESSAGE]
      : final.errors && final.errors.length ? final.errors
      : verification ? [verification]
      : final.errors,
    network,
  };
}

// Unbroken tokens of 120+ characters are never prose: a hidden reCAPTCHA / session field (base64) can fill the page's text and
// buried both Writer's confirmation and ElevenLabs' refusal on 2026-10-02.
export const stripBlobs = (s) => String(s == null ? "" : s).replace(/\S{120,}/g, " ");

export function detectConfirmation({ before, url, text: rawText, errors, visibleCaptcha }) {
  const text = stripBlobs(rawText);
  const successRe = /thank you for (applying|your application|submitting)|application (has been |was )?(received|submitted|successful)|we('| ha)ve received your application|your application (has been|was) (successfully )?submitted|was successfully submitted|application confirmation/i;
  const urlRe = /thank|confirm|success|submitted|complete/i;
  const urlChanged = url !== before;
  const urlLooksLikeSuccess = urlChanged && urlRe.test(url);
  const textLooksLikeSuccess = successRe.test(text || "");
  const hasErrors = errors && errors.length > 0;
  const hasVisibleCaptcha = !!visibleCaptcha;
  const allErrors = hasVisibleCaptcha
    ? [...(errors || []), `Visible ${visibleCaptcha} challenge appeared after submit -- never interacted with it`]
    : errors || [];
  return {
    success: (urlLooksLikeSuccess || textLooksLikeSuccess) && !hasErrors && !hasVisibleCaptcha,
    confirmation_text: textLooksLikeSuccess ? (text.match(successRe) || [])[0] || null : null,
    confirmation_url: urlChanged ? url : null,
    errors: allErrors,
  };
}
