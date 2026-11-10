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
  };

  const state = {
    photos: [], current: null, values: {}, exporting: false,
    film: "", recipe: null, crop: null, aspect: "",
    /* One edit per photo, keyed by filename. An edit belongs to a photo:
       brightening a dark frame should not brighten the one next to it. */
    edits: new Map(),
    sizes: new Map(),        // filename -> original width/height ratio
    autoOnOpen: true,
  };

  function captureEdit() {
    if (!state.current) return;
    state.edits.set(state.current, {
      values: { ...state.values },
      film: state.film,
      crop: state.crop ? [...state.crop] : null,
      recipe: state.recipe,
      aspect: state.aspect,
      touched: true,
    });
  }

  function restoreEdit(name) {
    const saved = state.edits.get(name);
    if (!saved) return false;
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
    dehaze: "Dehaze", black_point: "Black point", white_point: "White point",
    noise_reduction: "Noise reduction",
    clarity: "Clarity", structure: "Structure", grain: "Grain",
    sharpen: "Sharpen", vignette: "Vignette",
    lut_strength: "Strength",
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
        clearRecipeSelection();
        captureEdit();
        schedulePreview();
      });
      // Double-click resets one slider, which is quicker than dragging back.
      input.addEventListener("dblclick", () => {
        input.value = String(def);
        state.values[key] = def;
        show();
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
    for (const [k, v] of Object.entries(state.values)) params.set(k, String(v));
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

    if (restoreEdit(name)) {
      renderPreview(fullPreviewPx());
      return;
    }
    // First time opening this photo: start from a clean slate, then let auto
    // have a go, which is what "auto adjust the photos" should mean.
    resetValuesOnly();
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
      img.src = `/api/thumb/${encodeURIComponent(name)}`;
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
    for (const r of data.recipes) {
      if (!groups.has(r.group)) groups.set(r.group, []);
      groups.get(r.group).push(r);
    }
    for (const [groupName, items] of groups) {
      const head = document.createElement("div");
      head.className = "recipe-group";
      head.textContent = groupName;
      ui.recipeList.append(head);
      for (const r of items) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "recipe-btn";
        b.textContent = r.name;
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
    setValues(recipe.values);
    ui.recipeNote.textContent = recipe.notes || "";
    // Some looks are inseparable from their format - Arbus is square, Avedon
    // is a tall portrait - so a recipe may bring its own crop shape.
    if (recipe.crop_aspect) setAspect(String(recipe.crop_aspect));
    captureEdit();
    for (const b of ui.recipeList.children) {
      b.classList.toggle("active", b.dataset.id === recipe.id);
    }
    schedulePreview();
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

  function clearRecipeSelection() {
    state.recipe = null;
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
      });
      if (data.crop) state.crop = data.crop;
      ui.autoNotes.replaceChildren();
      for (const note of data.notes) {
        const li = document.createElement("li");
        li.textContent = note;
        ui.autoNotes.append(li);
      }
      ui.autoNotesGroup.classList.toggle("hidden", !data.notes.length);
      if (data.shot) ui.shotLine.textContent = data.shot;
      captureEdit();
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

  async function exportAll() {
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
  ui.export.addEventListener("click", exportAll);
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
    schedulePreview();
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
    .then((d) => {
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
