/* Photomation grading page.
   Previews are small graded copies rendered by the server and cached; Export
   writes full-resolution JPEGs. */

(() => {
  "use strict";

  const el = (id) => document.getElementById(id);
  const ui = {
    select: el("lut-select"),
    size: el("lut-size"),
    showBefore: el("show-before"),
    pairs: el("pairs"),
    previewCount: el("preview-count"),
    badPanel: el("bad-luts-panel"),
    badList: el("bad-luts"),
    noLuts: el("no-luts"),
    noKeepers: el("no-keepers"),
    body: el("grade-body"),
    reload: el("reload"),
    export: el("export"),
    progress: el("export-progress"),
    label: el("export-label"),
    count: el("export-count"),
    fill: el("export-fill"),
    toast: el("toast"),
  };

  const state = { luts: [], photos: [], exporting: false };

  async function loadLuts() {
    const res = await fetch("/api/luts");
    const data = await res.json();
    state.luts = data.luts;

    ui.select.replaceChildren();
    for (const l of data.luts) {
      const option = document.createElement("option");
      option.value = l.name;
      option.textContent = l.title ? `${l.title} (${l.name})` : l.name;
      ui.select.append(option);
    }

    ui.badList.replaceChildren();
    for (const b of data.unreadable) {
      const row = document.createElement("div");
      row.className = "skipped-item";
      const name = document.createElement("span");
      name.className = "label";
      name.textContent = b.name;
      const why = document.createElement("span");
      why.textContent = b.reason;
      row.append(name, why);
      ui.badList.append(row);
    }
    ui.badPanel.classList.toggle("hidden", data.unreadable.length === 0);

    const have = data.luts.length > 0;
    ui.noLuts.classList.toggle("hidden", have || window.PHOTOMATION.keepCount === 0);
    ui.body.classList.toggle("hidden", !have || window.PHOTOMATION.keepCount === 0);
    ui.export.disabled = !have;
    if (have) onLutChange();
  }

  function currentLut() {
    return state.luts.find((l) => l.name === ui.select.value);
  }

  function onLutChange() {
    const l = currentLut();
    ui.size.textContent = l ? `${l.size}³`.replace("3", "³") : "—";
    if (l) ui.size.textContent = `${l.size}³`;
    paint();
  }

  function paint() {
    const lutName = ui.select.value;
    if (!lutName) return;
    const before = ui.showBefore.checked;
    ui.pairs.replaceChildren();
    for (const name of state.photos) {
      const pair = document.createElement("figure");
      pair.className = "pair" + (before ? "" : " pair-single");

      if (before) {
        pair.append(shot(`/api/thumb/${encodeURIComponent(name)}`, name, "Before"));
      }
      pair.append(shot(
        `/api/preview/${encodeURIComponent(name)}?lut=${encodeURIComponent(lutName)}`,
        name, "After"));

      const caption = document.createElement("figcaption");
      caption.className = "photo-name";
      caption.textContent = name;
      pair.append(caption);
      ui.pairs.append(pair);
    }
    ui.previewCount.textContent = state.photos.length;
  }

  function shot(src, alt, label) {
    const wrap = document.createElement("div");
    wrap.className = "shot";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = src;
    img.alt = `${alt} ${label}`;
    const tag = document.createElement("span");
    tag.className = "shot-tag";
    tag.textContent = label;
    wrap.append(img, tag);
    return wrap;
  }

  function exportAll() {
    if (state.exporting || !ui.select.value) return;
    state.exporting = true;
    ui.export.disabled = true;
    ui.progress.classList.remove("hidden");
    ui.label.textContent = "Grading…";
    ui.fill.style.width = "0%";

    let total = 0;
    const source = new EventSource(`/api/grade?lut=${encodeURIComponent(ui.select.value)}`);
    let failed = 0;

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
        toast(failed
          ? `Graded ${total - failed} of ${total}; ${failed} failed`
          : `Graded ${total} photo${total === 1 ? "" : "s"} to output/graded/`);
        setTimeout(() => ui.progress.classList.add("hidden"), 1500);
      }
    };
    source.onerror = () => {
      source.close();
      state.exporting = false;
      ui.export.disabled = false;
      ui.label.textContent = "Grading failed — is the server still running?";
    };
  }

  function toast(message) {
    ui.toast.textContent = message;
    ui.toast.classList.add("visible");
    setTimeout(() => ui.toast.classList.remove("visible"), 3200);
  }

  ui.select.addEventListener("change", onLutChange);
  ui.showBefore.addEventListener("change", paint);
  ui.reload.addEventListener("click", () => loadLuts());
  ui.export.addEventListener("click", exportAll);

  // Photo names come from the server-rendered count plus a listing call.
  fetch("/api/keepers")
    .then((r) => r.json())
    .then((d) => { state.photos = d.photos; return loadLuts(); })
    .catch(() => { state.photos = []; return loadLuts(); });
})();
