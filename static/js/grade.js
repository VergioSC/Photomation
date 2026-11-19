/* Photomation editor.
   Sliders drive a live preview of one photo; Export applies the same edit to
   every kept photo at full resolution. */

(() => {
  "use strict";

  const el = (id) => document.getElementById(id);
  const ui = {
    editor: el("editor"), noKeepers: el("no-keepers"),
    after: el("after"), before: el("before"), beforeWrap: el("before-wrap"),
    compare: el("compare"), currentName: el("current-name"),
    filmstrip: el("filmstrip"),
    select: el("lut-select"), badPanel: el("bad-luts-panel"), badList: el("bad-luts"),
    reset: el("reset"), export: el("export"), download: el("download"),
    progress: el("export-progress"), label: el("export-label"),
    count: el("export-count"), fill: el("export-fill"),
    toast: el("toast"),
    recipeList: el("recipe-list"), recipeNote: el("recipe-note"),
    badRecipes: el("bad-recipes"), badRecipesPanel: el("bad-recipes-panel"),
    filmSelect: el("film-select"), resizeSelect: el("resize-select"),
    auto: el("auto"), autoCrop: el("auto-crop"), clearCrop: el("clear-crop"),
    autoNotes: el("auto-notes"), autoNotesGroup: el("auto-notes-group"),
    shotLine: el("shot-line"), autoOpen: el("auto-open"), copyAll: el("copy-all"),
    zoom: el("zoom"), undo: el("undo"), redo: el("redo"),
  };

  // Lives on the sliders for the UI's sake, but it is a control over the
  // recipe, not a property of the photo, so it never goes to the server.
  const UI_ONLY = new Set(["recipe_strength"]);

  const state = {
    photos: [], current: null, values: {}, exporting: false,
    film: "", recipe: null, crop: null, aspect: "",
    recipeBase: null, recipeTarget: null,
    /* One edit per photo, keyed by filename. An edit belongs to a photo:
       brightening a dark frame should not brighten the one next to it. */
    edits: new Map(),
    sizes: new Map(),        // filename -> original width/height ratio
    autoOnOpen: true,
    /* False as soon as an edit is made, true again once Export all has
       written every photo out. Leaving the page with it false loses work. */
    exported: true,
    /* Undo stack for the photo on screen. Snapshots are pushed when a change
       is COMMITTED (a slider released, a preset clicked), not on every pixel
       of a drag, or one slider sweep would bury the stack. */
    history: [],
    historyAt: -1,
    restoring: false,
    zoomed: false,
  };

  function snapshot() {
    return {
      values: { ...state.values }, film: state.film,
      crop: state.crop ? [...state.crop] : null,
      recipe: state.recipe, aspect: state.aspect,
    };
  }

  function pushHistory() {
    if (state.restoring) return;
    // Anything undone is dropped once a new change is made, as everywhere else.
    state.history.length = state.historyAt + 1;
    state.history.push(snapshot());
    if (state.history.length > 60) state.history.shift();
    state.historyAt = state.history.length - 1;
    syncHistoryButtons();
  }

  function restore(snap) {
    state.restoring = true;
    setValues({ ...snap.values, film: snap.film });
    state.crop = snap.crop ? [...snap.crop] : null;
    state.aspect = snap.aspect || "";
    state.recipe = snap.recipe;
    for (const b of ui.aspectRow.children) {
      b.classList.toggle("active", b.dataset.aspect === state.aspect);
    }
    for (const b of ui.recipeList.children) {
      if (b.dataset.id) b.classList.toggle("active", b.dataset.id === state.recipe);
    }
    state.restoring = false;
    captureEdit();
    schedulePreview();
  }

  function undo() {
    if (state.historyAt <= 0) return;
    state.historyAt -= 1;
    restore(state.history[state.historyAt]);
    syncHistoryButtons();
  }

  function redo() {
    if (state.historyAt >= state.history.length - 1) return;
    state.historyAt += 1;
    restore(state.history[state.historyAt]);
    syncHistoryButtons();
  }

  function syncHistoryButtons() {
    ui.undo.disabled = state.historyAt <= 0;
    ui.redo.disabled = state.historyAt >= state.history.length - 1;
  }

  function resetHistory() {
    state.history = [snapshot()];
    state.historyAt = 0;
    syncHistoryButtons();
  }

  /* Edits are saved to output/edits.json beside the photos they describe.
     They used to live only in this page, so a reload - or closing the laptop
     - threw away every slider on every photo. Debounced, because a slider
     drag fires this on every pixel. */
  let saveTimer = null;

  function saveEdits() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      fetch("/api/edits", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ edits: Object.fromEntries(state.edits) }),
      }).catch(() => toast("Could not save your edits to disk."));
    }, 700);
  }

  async function loadEdits() {
    try {
      const data = await (await fetch("/api/edits")).json();
      for (const [name, edit] of Object.entries(data.edits || {})) {
        state.edits.set(name, edit);
      }
      const n = state.edits.size;
      if (n) {
        state.exported = false;
        toast(`Picked up where you left off: ${n} photo${n === 1 ? "" : "s"} already edited.`);
      }
    } catch { /* no saved edits is the normal case */ }
  }

  function captureEdit() {
    if (!state.current) return;
    const values = { ...state.values };
    for (const k of UI_ONLY) delete values[k];
    state.exported = false;          // this edit has not been written out yet
    state.edits.set(state.current, {
      values,
      film: state.film,
      crop: state.crop ? [...state.crop] : null,
      recipe: state.recipe,
      aspect: state.aspect,
      touched: true,
    });
    saveEdits();
  }

  function restoreEdit(name) {
    const saved = state.edits.get(name);
    if (!saved) return false;
    // The strength the recipe was mixed at is already baked into these
    // values. The control itself resets, because mixing again needs the
    // slider positions from before the recipe, and those belong to the
    // moment it was clicked, not to the photo.
    state.recipeTarget = null;
    setStrengthRow(100, false);
    setValues({ ...saved.values, film: saved.film });
    state.crop = saved.crop ? [...saved.crop] : null;
    state.aspect = saved.aspect || "";
    state.recipe = saved.recipe;
    for (const b of ui.aspectRow.children) {
      b.classList.toggle("active", b.dataset.aspect === state.aspect);
    }
    for (const b of ui.recipeList.children) {
      b.classList.toggle("active", b.dataset.id === state.recipe);
    }
    return true;
  }

  /* ---------------- sliders ---------------- */

  const LABELS = {
    exposure: "Exposure", contrast: "Contrast", highlights: "Highlights",
    shadows: "Shadows", temperature: "Temperature", tint: "Tint",
    vibrance: "Vibrance", saturation: "Saturation",
    sky_exposure: "Sky brightness", foreground_exposure: "Ground brightness",
    sky_contrast: "Sky contrast",
    dehaze: "Dehaze", black_point: "Black point", white_point: "White point",
    noise_reduction: "Noise reduction",
    clarity: "Clarity", structure: "Structure", grain: "Grain",
    sharpen: "Sharpen", vignette: "Vignette",
    lut_strength: "LUT strength", film_strength: "Film strength",
    recipe_strength: "Recipe strength",
  };

  function buildSliders() {
    for (const row of document.querySelectorAll(".slider-row")) {
      const key = row.dataset.key;
      const def = Number(row.dataset.default);
      state.values[key] = def;

      const label = document.createElement("span");
      label.className = "field-label";
      label.textContent = LABELS[key] || key;

      const value = document.createElement("span");
      value.className = "field-value mono";

      const input = document.createElement("input");
      input.type = "range";
      input.min = row.dataset.min;
      input.max = row.dataset.max;
      input.step = row.dataset.step;
      input.value = String(def);
      input.setAttribute("aria-label", LABELS[key] || key);

      const show = () => {
        const n = Number(input.value);
        const unit = row.dataset.unit || "";
        value.textContent = (n > 0 && unit !== "" ? "+" : "") +
          (row.dataset.step === "0.05" ? n.toFixed(2) : String(n)) + unit;
        row.classList.toggle("changed", n !== def);
      };

      input.addEventListener("input", () => {
        state.values[key] = Number(input.value);
        show();
        if (key === "recipe_strength") {
          // This one dilutes the recipe rather than being an edit of its own,
          // so it must not clear the selection it is adjusting.
          applyRecipeStrength(Number(input.value));
          return;
        }
        clearRecipeSelection();
        markNotesStale();
        captureEdit();
        schedulePreview();
      });
      input.addEventListener("change", pushHistory);
      // Double-click resets one slider, which is quicker than dragging back.
      input.addEventListener("dblclick", () => {
        input.value = String(def);
        state.values[key] = def;
        show();
        pushHistory();
        schedulePreview();
      });

      const head = document.createElement("div");
      head.className = "field-head";
      head.append(label, value);
      row.append(head, input);
      row._reset = () => { input.value = String(def); state.values[key] = def; show(); };
      row._set = (n) => {
        const clamped = Math.max(Number(input.min), Math.min(Number(input.max), n));
        input.value = String(clamped);
        state.values[key] = clamped;
        show();
      };
      show();
    }
  }

  function resetAll() {
    for (const row of document.querySelectorAll(".slider-row")) row._reset();
    state.film = "";
    state.crop = null;
    ui.filmSelect.value = "";
    ui.recipeNote.textContent = "";
    ui.autoNotesGroup.classList.add("hidden");
    setAspect("");
    clearRecipeSelection();
    if (state.current) state.edits.delete(state.current);
    for (const t of ui.filmstrip.children) {
      t.classList.toggle("edited", state.edits.has(t.dataset.name));
    }
    schedulePreview();
  }

  /* ---------------- preview ---------------- */

  const DRAFT_PX = 700;

  /* Ask for the size the stage will really show. CSS pixels are not screen
     pixels on a Retina display, so without devicePixelRatio the preview is
     upscaled and looks pixellated. */
  function fullPreviewPx() {
    const stage = el("stage");
    let boxW = Math.max(stage ? stage.clientWidth : 800, 480);
    if (ui.compare && ui.compare.checked) boxW /= 2;
    // object-fit: contain fits the photo inside the box, so its long edge can
    // reach whichever box side is larger. A portrait shot is limited by height,
    // so asking by width alone under-requests and the result is soft.
    const boxH = window.innerHeight * 0.74;      // matches .stage img max-height
    const css = Math.max(boxW, boxH);
    return Math.min(2400, Math.round(css * (window.devicePixelRatio || 1)));
  }

  function query(sizePx) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(state.values)) {
      if (!UI_ONLY.has(k)) params.set(k, String(v));
    }
    params.set("lut", ui.select.value || "none");
    params.set("film", state.film || "");
    params.set("size", String(sizePx));
    if (state.crop) {
      const [x, y, w, h] = state.crop;
      params.set("crop_x", x); params.set("crop_y", y);
      params.set("crop_w", w); params.set("crop_h", h);
    }
    return params.toString();
  }

  /* One request in flight at a time: dragging a slider fires dozens of input
     events, and without this the browser queues a render for every one. */
  let pending = false;
  let queued = false;
  let settleTimer = null;

  /* While a slider moves, render small and fast. When it stops, render at the
     real size so the picture the user judges is sharp. */
  function schedulePreview() {
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => renderPreview(fullPreviewPx()), 260);
    if (pending) { queued = true; return; }
    renderPreview(DRAFT_PX);
  }

  function renderPreview(sizePx) {
    if (!state.current) return;
    pending = true;
    const probe = new Image();
    probe.onload = probe.onerror = () => {
      ui.after.src = probe.src;
      pending = false;
      if (queued) { queued = false; renderPreview(DRAFT_PX); }
    };
    probe.src = `/api/preview/${encodeURIComponent(state.current)}?${query(sizePx)}`;
  }

  async function selectPhoto(name) {
    captureEdit();                       // keep what was done to the last one
    state.current = name;
    ui.currentName.textContent = name;
    ui.before.src = `/api/preview/${encodeURIComponent(name)}?size=${fullPreviewPx()}&lut=none`;
    for (const t of ui.filmstrip.children) {
      t.classList.toggle("active", t.dataset.name === name);
    }
    for (const t of ui.filmstrip.children) {
      t.classList.toggle("edited", state.edits.has(t.dataset.name));
    }
    refreshRecipeSheet();

    if (restoreEdit(name)) {
      resetHistory();
      renderPreview(fullPreviewPx());
      return;
    }
    // First time opening this photo: start from a clean slate, then let auto
    // have a go, which is what "auto adjust the photos" should mean.
    resetValuesOnly();
    resetHistory();
    renderPreview(DRAFT_PX);
    if (state.autoOnOpen) await runAuto(false, true);
    else renderPreview(fullPreviewPx());
  }

  function resetValuesOnly() {
    for (const row of document.querySelectorAll(".slider-row")) row._reset();
    state.film = "";
    state.crop = null;
    state.aspect = "";
    ui.filmSelect.value = "";
    clearRecipeSelection();
    for (const b of ui.aspectRow.children) b.classList.toggle("active", b.dataset.aspect === "");
  }

  function buildFilmstrip() {
    ui.filmstrip.replaceChildren();
    for (const name of state.photos) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "frame";
      button.dataset.name = name;
      button.title = name;
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = `/api/thumb/${encodeURIComponent(name)}?from=keep`;
      img.alt = name;
      button.append(img);
      button.addEventListener("click", () => selectPhoto(name));
      ui.filmstrip.append(button);
    }
  }

  /* ---------------- LUTs ---------------- */

  async function loadLuts() {
    const data = await (await fetch("/api/luts")).json();
    ui.select.replaceChildren();

    const none = document.createElement("option");
    none.value = "none";
    none.textContent = "None";
    ui.select.append(none);

    for (const l of data.luts) {
      const option = document.createElement("option");
      option.value = l.name;
      option.textContent = l.title ? `${l.title} (${l.size}³)` : l.name;
      ui.select.append(option);
    }

    ui.badList.replaceChildren();
    for (const b of data.unreadable) {
      const row = document.createElement("div");
      row.className = "skipped-item";
      const n = document.createElement("span");
      n.className = "label";
      n.textContent = b.name;
      const why = document.createElement("span");
      why.textContent = b.reason;
      row.append(n, why);
      ui.badList.append(row);
    }
    ui.badPanel.classList.toggle("hidden", data.unreadable.length === 0);
    syncStrengthRow();
  }

  function syncStrengthRow() {
    el("row-lut_strength").classList.toggle("hidden", ui.select.value === "none");
  }

  /* ---------------- recipes and film looks ---------------- */

  /* How tall one tile is on screen. The sheet is sent at 128px a tile, so
     this has room to spare on a Retina display. */
  const RECIPE_CHIP_PX = 34;

  /* Point every preset button at the current photo, so the list shows what
     each look does to THIS photo rather than to nothing. */
  function refreshRecipeSheet() {
    if (!state.current) return;
    const url = `/api/recipe-sheet/${encodeURIComponent(state.current)}`;
    for (const chip of ui.recipeList.querySelectorAll(".recipe-chip")) {
      chip.style.backgroundImage = `url("${url}")`;
    }
  }


  async function loadFilms() {
    const data = await (await fetch("/api/films")).json();
    ui.filmSelect.replaceChildren();
    const none = document.createElement("option");
    none.value = ""; none.textContent = "None";
    ui.filmSelect.append(none);
    for (const f of data.films) {
      const o = document.createElement("option");
      o.value = f.id; o.textContent = f.label;
      ui.filmSelect.append(o);
    }
  }

  async function loadRecipes() {
    const data = await (await fetch("/api/recipes")).json();
    state.recipes = data.recipes;
    ui.recipeList.replaceChildren();
    const groups = new Map();
    data.recipes.forEach((r, index) => {
      // Its row in the contact sheet. The groups below reorder the buttons,
      // so the position in the sheet has to be remembered here.
      r.sheetRow = index;
      if (!groups.has(r.group)) groups.set(r.group, []);
      groups.get(r.group).push(r);
    });
    for (const [groupName, items] of groups) {
      const head = document.createElement("div");
      head.className = "recipe-group";
      head.textContent = groupName;
      ui.recipeList.append(head);
      for (const r of items) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "recipe-btn";
        const chip = document.createElement("span");
        chip.className = "recipe-chip";
        chip.style.backgroundPosition = `0 -${r.sheetRow * RECIPE_CHIP_PX}px`;
        const text = document.createElement("span");
        text.textContent = r.name;
        b.append(chip, text);
        b.title = r.notes || r.name;
        b.dataset.id = r.id;
        b.addEventListener("click", () => applyRecipe(r));
        ui.recipeList.append(b);
      }
    }
    ui.badRecipes.replaceChildren();
    for (const bad of data.problems || []) {
      const row = document.createElement("div");
      row.className = "skipped-item";
      const n = document.createElement("span");
      n.className = "label"; n.textContent = bad.name;
      const why = document.createElement("span"); why.textContent = bad.reason;
      row.append(n, why);
      ui.badRecipes.append(row);
    }
    ui.badRecipesPanel.classList.toggle("hidden", !(data.problems || []).length);
  }

  /* A recipe only fills the sliders in. Everything stays adjustable after. */
  function applyRecipe(recipe) {
    state.recipe = recipe.id;
    // Only the fields the recipe actually declares. It describes a look, and
    // knows nothing about this photo's exposure, haze or noise - so applying
    // one must not wipe the corrections auto worked out for it.
    const look = {};
    for (const key of recipe.sets || Object.keys(recipe.values)) {
      look[key] = recipe.values[key];
    }
    // Remember what the sliders held before, so the strength control can mix
    // between that and the recipe instead of only switching the look on.
    state.recipeBase = {};
    for (const key of Object.keys(look)) state.recipeBase[key] = state.values[key];
    state.recipeTarget = look;
    setStrengthRow(100, true);
    setValues(look);
    ui.recipeNote.textContent = recipe.notes || "";
    markNotesStale();
    // Some looks are inseparable from their format - Arbus is square, Avedon
    // is a tall portrait - so a recipe may bring its own crop shape.
    if (recipe.crop_aspect) setAspect(String(recipe.crop_aspect));
    captureEdit();
    pushHistory();
    for (const b of ui.recipeList.children) {
      b.classList.toggle("active", b.dataset.id === recipe.id);
    }
    schedulePreview();
  }

  /* Mix the selected recipe with whatever the sliders held before it.

     At 100 it is the preset as written; at 40 it is a suggestion. Numbers
     move proportionally, and the film look is diluted by its own strength,
     because a channel mixer and a tone curve cannot be scaled any other way. */
  function applyRecipeStrength(percent) {
    if (!state.recipeTarget) return;
    const mix = percent / 100;
    const values = {};
    for (const [key, target] of Object.entries(state.recipeTarget)) {
      if (key === "film") { values.film = target; continue; }
      const base = Number(state.recipeBase[key] ?? 0);
      values[key] = base + (Number(target) - base) * mix;
    }
    if ("film" in state.recipeTarget) values.film_strength = percent;
    setValues(values);
    markNotesStale();
    captureEdit();
    schedulePreview();
  }

  /* Show the strength control only while a recipe is selected. */
  function setStrengthRow(percent, visible) {
    const row = el("row-recipe-strength");
    if (!row) return;
    row.hidden = !visible;
    if (visible && typeof row._set === "function") row._set(percent);
  }

  /* Push a set of values into the sliders, film picker and state. */
  function setValues(values) {
    for (const row of document.querySelectorAll(".slider-row")) {
      const key = row.dataset.key;
      if (!(key in values)) continue;
      row._set(Number(values[key]));
    }
    if ("film" in values) {
      state.film = values.film || "";
      ui.filmSelect.value = state.film;
    }
  }

  /* The auto panel explains values. Once they change it is describing a photo
     that no longer exists, so it says so rather than quietly lying. */
  function markNotesStale() {
    if (ui.autoNotesGroup.classList.contains("hidden")) return;
    ui.autoNotesGroup.classList.add("stale");
  }

  function clearRecipeSelection() {
    state.recipe = null;
    state.recipeTarget = null;
    setStrengthRow(100, false);
    for (const b of ui.recipeList.children) b.classList.remove("active");
  }

  /* ---------------- auto ---------------- */

  async function runAuto(withCrop, quiet) {
    if (!state.current) return;
    const params = new URLSearchParams({ crop: withCrop ? "1" : "0" });
    if (withCrop && state.aspect) params.set("aspect", state.aspect);
    ui.auto.disabled = true;
    ui.autoCrop.disabled = true;
    try {
      const res = await fetch(`/api/auto/${encodeURIComponent(state.current)}?${params}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "auto failed");
      // Only the tonal values: a film look the user picked is left alone.
      const v = data.values;
      setValues({
        exposure: v.exposure, contrast: v.contrast, highlights: v.highlights,
        shadows: v.shadows, temperature: v.temperature, tint: v.tint,
        noise_reduction: v.noise_reduction, vibrance: v.vibrance,
        sharpen: v.sharpen, dehaze: v.dehaze,
        black_point: v.black_point, white_point: v.white_point,
        sky_exposure: v.sky_exposure,
        foreground_exposure: v.foreground_exposure,
        sky_contrast: v.sky_contrast,
      });
      if (data.crop) state.crop = data.crop;
      ui.autoNotes.replaceChildren();
      for (const note of data.notes) {
        const li = document.createElement("li");
        li.textContent = note;
        ui.autoNotes.append(li);
      }
      ui.autoNotesGroup.classList.toggle("hidden", !data.notes.length);
      ui.autoNotesGroup.classList.remove("stale");
      if (data.shot) ui.shotLine.textContent = data.shot;
      captureEdit();
      pushHistory();
      renderPreview(fullPreviewPx());
      if (!quiet) toast("Auto adjusted. Every slider is still yours to change.");
    } catch (err) {
      toast(`Auto failed: ${err.message}`);
    } finally {
      ui.auto.disabled = false;
      ui.autoCrop.disabled = false;
    }
  }

  /* ---------------- crop ---------------- */

  function setAspect(value) {
    state.aspect = value;
    for (const b of ui.aspectRow.children) b.classList.toggle("active", b.dataset.aspect === value);
    if (!value) { state.crop = null; schedulePreview(); return; }
    // Centre a crop of the requested shape inside the ORIGINAL frame. Measuring
    // the preview instead would compound, since it may already be cropped.
    const srcAspect = state.sizes.get(state.current) || 1.5;
    const target = Number(value);
    let w = 1, h = 1;
    if (target > srcAspect) { h = srcAspect / target; } else { w = target / srcAspect; }
    state.crop = [(1 - w) / 2, (1 - h) / 2, w, h];
    captureEdit();
    pushHistory();
    schedulePreview();
  }

  /* ---------------- export ---------------- */

  function currentEditPayload() {
    captureEdit();
    const out = {};
    for (const [name, e] of state.edits) {
      const entry = { ...e.values, film: e.film || "" };
      if (e.crop) {
        entry.crop_x = e.crop[0]; entry.crop_y = e.crop[1];
        entry.crop_w = e.crop[2]; entry.crop_h = e.crop[3];
      }
      out[name] = entry;
    }
    return out;
  }

  async function exportAll(only) {
    if (state.exporting) return;
    state.exporting = true;
    ui.export.disabled = true;
    ui.progress.classList.remove("hidden");
    ui.label.textContent = "Exporting\u2026";
    ui.fill.style.width = "0%";

    let total = 0, failed = 0, autoed = 0;
    try {
      // EventSource can only GET, and the edits are too big for a URL, so the
      // stream is read off a POST instead.
      const response = await fetch("/api/grade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          edits: currentEditPayload(),
          lut: ui.select.value || "none",
          resize: Number(ui.resizeSelect.value) || 0,
          auto_fill: state.autoOnOpen,
          // Present only when exporting one photo, so the server knows not to
          // clear the other fifty-five already in output/graded.
          ...(only ? { only } : {}),
        }),
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.error || `export failed (${response.status})`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const msg = JSON.parse(line.slice(6));
          if (msg.type === "start") {
            total = msg.total;
            ui.count.textContent = `0 / ${total}`;
          } else if (msg.type === "photo" || msg.type === "skipped") {
            if (msg.type === "skipped") failed += 1;
            if (msg.edit === "auto") autoed += 1;
            ui.count.textContent = `${msg.index} / ${total}`;
            ui.fill.style.width = `${(msg.index / total) * 100}%`;
          } else if (msg.type === "done") {
            ui.label.textContent = "Done";
          }
        }
      }
      const extra = autoed ? `, ${autoed} auto-adjusted for you` : "";
      // One photo is not the whole set, so the rest are still unexported.
      if (!failed && !only) state.exported = true;
      toast(failed ? `Exported ${total - failed} of ${total}; ${failed} failed`
                   : `Exported ${total} photo${total === 1 ? "" : "s"}${extra}.`);
    } catch (err) {
      ui.label.textContent = "Export failed";
      toast(`Export failed: ${err.message}`);
    } finally {
      state.exporting = false;
      ui.export.disabled = false;
      setTimeout(() => ui.progress.classList.add("hidden"), 1600);
    }
  }

  /* Give every photo the edit that is on screen now. */
  function copyToAll() {
    captureEdit();
    const mine = state.edits.get(state.current);
    if (!mine) return;
    for (const name of state.photos) {
      state.edits.set(name, { ...mine, crop: mine.crop ? [...mine.crop] : null });
    }
    for (const t of ui.filmstrip.children) t.classList.add("edited");
    toast(`That edit now applies to all ${state.photos.length} photos.`);
  }

  function toast(message) {
    ui.toast.textContent = message;
    ui.toast.classList.add("visible");
    setTimeout(() => ui.toast.classList.remove("visible"), 3600);
  }

  /* ---------------- boot ---------------- */

  ui.select.addEventListener("change", () => { syncStrengthRow(); schedulePreview(); });
  ui.reset.addEventListener("click", resetAll);
  ui.export.addEventListener("click", () => exportAll());

  el("export-one").addEventListener("click", () => {
    if (!state.current) return;
    captureEdit();
    exportAll([state.current]);
  });

  /* Spotting a bad frame while editing used to mean going back to Cull and
     starting the sort again. */
  el("bin-photo").addEventListener("click", async () => {
    const name = state.current;
    if (!name) return;
    const answer = await window.ask(
      "Bin this photo?",
      `${name} moves from the keepers to output/reject and leaves the editor. ` +
      `Nothing is deleted, and Cull can put it back.`,
      ["Bin it", "Cancel"]);
    if (answer !== "Bin it") return;

    const reply = await fetch(`/api/reject/${encodeURIComponent(name)}`, { method: "POST" });
    if (!reply.ok) { toast("Could not move that photo."); return; }
    state.edits.delete(name);
    saveEdits();
    const at = state.photos.indexOf(name);          // a list of names, not objects
    state.photos.splice(at, 1);
    state.current = null;                            // or captureEdit re-adds it
    buildFilmstrip();
    if (state.photos.length) selectPhoto(state.photos[Math.min(at, state.photos.length - 1)]);
    else window.location.href = "/dashboard";
    toast(`${name} moved to the rejects.`);
  });

  /* ---------------- leaving with work unsaved ---------------- */

  /* Edits live in this page and nowhere else. Navigating away throws them
     away silently, which is a bad way to find out how the tool works. */
  function unsavedCount() {
    if (state.exported || state.exporting) return 0;
    let n = 0;
    for (const e of state.edits.values()) if (e.touched) n += 1;
    return n;
  }

  /* The in-app links are ours, so they get the real question with the real
     way out: export first, and then go. */
  for (const link of document.querySelectorAll(".nav-links a, .brand")) {
    link.addEventListener("click", async (event) => {
      const n = unsavedCount();
      if (!n) return;
      event.preventDefault();
      const answer = await window.ask(
        "Your edits are not exported",
        `${n} photo${n === 1 ? " has" : "s have"} edits that exist only on ` +
        `this page. Leaving resets every slider to its default. Exporting ` +
        `writes them to output/graded first.`,
        ["Export, then leave", "Leave without exporting", "Stay here"]);
      if (answer === "Export, then leave") {
        await exportAll();
        if (!state.exported) return;        // export failed: stay put
      } else if (answer !== "Leave without exporting") {
        return;
      }
      window.location.href = link.href;
    });
  }

  /* Closing the tab or reloading cannot be intercepted with our own dialog:
     browsers only allow their own, and ignore any text we pass. It is still
     worth having, because it is the one that catches an accidental Cmd+W. */
  window.addEventListener("beforeunload", (event) => {
    if (!unsavedCount()) return;
    event.preventDefault();
    event.returnValue = "";
  });
  ui.compare.addEventListener("change", () => {
    ui.beforeWrap.classList.toggle("hidden", !ui.compare.checked);
    el("after-tag").classList.toggle("hidden", !ui.compare.checked);
    el("stage").classList.toggle("split", ui.compare.checked);
    renderPreview(fullPreviewPx());
  });

  ui.aspectRow = el("aspect-row");
  for (const b of ui.aspectRow.children) {
    b.addEventListener("click", () => setAspect(b.dataset.aspect));
  }
  ui.filmSelect.addEventListener("change", () => {
    state.film = ui.filmSelect.value;
    clearRecipeSelection();
    captureEdit();
    pushHistory();
    schedulePreview();
  });
  /* Zoom the stage to real pixels. Judging sharpening, noise or a dehaze
     without seeing 100% is guesswork, and the stage is fitted to the window. */
  function toggleZoom() {
    state.zoomed = !state.zoomed;
    el("stage").classList.toggle("zoomed", state.zoomed);
    ui.zoom.textContent = state.zoomed ? "Fit" : "100%";
    renderPreview(state.zoomed ? 2400 : fullPreviewPx());
  }

  ui.zoom.addEventListener("click", toggleZoom);
  ui.undo.addEventListener("click", undo);
  ui.redo.addEventListener("click", redo);

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea")) return;
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === "z") {
      e.preventDefault();
      e.shiftKey ? redo() : undo();
    } else if (!mod && e.key === "z") {
      e.preventDefault();
      toggleZoom();
    }
  });

  ui.auto.addEventListener("click", () => runAuto(false));
  ui.copyAll.addEventListener("click", copyToAll);
  ui.autoOpen.addEventListener("change", () => { state.autoOnOpen = ui.autoOpen.checked; });
  ui.autoCrop.addEventListener("click", () => runAuto(true, false));
  ui.clearCrop.addEventListener("click", () => setAspect(""));

  buildSliders();
  Promise.all([loadFilms(), loadRecipes()]).catch(() => {});
  fetch("/api/keepers")
    .then((r) => r.json())
    .then(async (d) => {
      // Before anything is opened, so a photo with a saved edit is restored
      // rather than auto-adjusted from scratch.
      await loadEdits();
      state.photos = d.photos || [];
      for (const s of d.sizes || []) {
        if (s.width && s.height) state.sizes.set(s.name, s.width / s.height);
      }
      if (!state.photos.length) return loadLuts();
      buildFilmstrip();
      ui.export.disabled = false;
      return loadLuts().then(() => selectPhoto(state.photos[0]));
    })
    .catch(() => loadLuts());
})();
