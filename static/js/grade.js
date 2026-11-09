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
  };

  const state = { photos: [], current: null, values: {}, exporting: false };

  /* ---------------- sliders ---------------- */

  const LABELS = {
    exposure: "Exposure", contrast: "Contrast", highlights: "Highlights",
    shadows: "Shadows", temperature: "Temperature", tint: "Tint",
    vibrance: "Vibrance", saturation: "Saturation", sharpen: "Sharpen",
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
      show();
    }
  }

  function resetAll() {
    for (const row of document.querySelectorAll(".slider-row")) row._reset();
    schedulePreview();
  }

  /* ---------------- preview ---------------- */

  function query() {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(state.values)) params.set(k, String(v));
    params.set("lut", ui.select.value || "none");
    return params.toString();
  }

  /* One request in flight at a time: dragging a slider fires dozens of input
     events, and without this the browser queues a render for every one. */
  let pending = false;
  let queued = false;

  function schedulePreview() {
    if (pending) { queued = true; return; }
    renderPreview();
  }

  function renderPreview() {
    if (!state.current) return;
    pending = true;
    const url = `/api/preview/${encodeURIComponent(state.current)}?${query()}`;
    const probe = new Image();
    probe.onload = probe.onerror = () => {
      ui.after.src = probe.src;
      pending = false;
      if (queued) { queued = false; renderPreview(); }
    };
    probe.src = url;
  }

  function selectPhoto(name) {
    state.current = name;
    ui.currentName.textContent = name;
    ui.before.src = `/api/thumb/${encodeURIComponent(name)}?from=keep`;
    for (const t of ui.filmstrip.children) {
      t.classList.toggle("active", t.dataset.name === name);
    }
    renderPreview();
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

  /* ---------------- export ---------------- */

  function exportAll() {
    if (state.exporting) return;
    state.exporting = true;
    ui.export.disabled = true;
    ui.progress.classList.remove("hidden");
    ui.label.textContent = "Exporting…";
    ui.fill.style.width = "0%";

    let total = 0, failed = 0;
    const source = new EventSource(`/api/grade?${query()}`);
    source.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "start") {
        total = msg.total;
        ui.count.textContent = `0 / ${total}`;
      } else if (msg.type === "photo" || msg.type === "skipped") {
        if (msg.type === "skipped") failed += 1;
        ui.count.textContent = `${msg.index} / ${total}`;
        ui.fill.style.width = `${(msg.index / total) * 100}%`;
      } else if (msg.type === "done") {
        source.close();
        state.exporting = false;
        ui.export.disabled = false;
        ui.label.textContent = "Done";
        toast(failed ? `Exported ${total - failed} of ${total}; ${failed} failed`
                     : `Exported ${total} photo${total === 1 ? "" : "s"}. Use Download zip.`);
        setTimeout(() => ui.progress.classList.add("hidden"), 1600);
      }
    };
    source.onerror = () => {
      source.close();
      state.exporting = false;
      ui.export.disabled = false;
      ui.label.textContent = "Export failed — is the server still running?";
    };
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
  });

  buildSliders();
  fetch("/api/keepers")
    .then((r) => r.json())
    .then((d) => {
      state.photos = d.photos || [];
      if (!state.photos.length) return loadLuts();
      buildFilmstrip();
      ui.export.disabled = false;
      return loadLuts().then(() => selectPhoto(state.photos[0]));
    })
    .catch(() => loadLuts());
})();
