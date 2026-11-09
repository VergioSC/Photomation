/* Photomation dashboard.
   Scores arrive once over SSE; the threshold slider re-buckets them locally, so
   dragging it never triggers a rescan. */

(() => {
  "use strict";

  const state = {
    photos: [],        // { name, score, decode }
    skipped: [],       // { name, reason }
    threshold: window.PHOTOMATION.defaultThreshold,
    scanning: false,
    /* Manual decisions, by filename: "keep" or "reject". The whole-frame blur
       score gets portraits and big empty skies wrong, and moving the threshold
       to rescue one photo drags every other photo with it. An override pins
       one photo and ignores the slider for it. */
    overrides: new Map(),
  };

  /* Where a photo lands: a manual override if there is one, else the slider. */
  const bucketOf = (photo) =>
    state.overrides.get(photo.name) ??
    (photo.score >= state.threshold ? "keep" : "reject");

  const el = (id) => document.getElementById(id);
  const ui = {
    emptyState: el("empty-state"),
    scanPanel: el("scan-panel"),
    scanLabel: el("scan-label"),
    scanCount: el("scan-count"),
    progress: el("progress"),
    results: el("results"),
    statKeep: el("stat-keep"),
    statReject: el("stat-reject"),
    statTotal: el("stat-total"),
    statRange: el("stat-range"),
    slider: el("threshold-slider"),
    number: el("threshold-input"),
    display: el("threshold-display"),
    histogram: el("histogram"),
    keepGrid: el("keep-grid"),
    rejectGrid: el("reject-grid"),
    keepCount: el("keep-count"),
    rejectCount: el("reject-count"),
    skippedPanel: el("skipped-panel"),
    skippedItems: el("skipped-items"),
    apply: el("apply"),
    rescan: el("rescan"),
    toast: el("toast"),
  };

  /* ---------------- scanning ---------------- */

  function scan() {
    if (state.scanning) return;
    state.scanning = true;
    state.photos = [];
    state.skipped = [];
    cardCache.clear();
    state.overrides.clear();
    ui.apply.disabled = true;
    ui.emptyState.classList.add("hidden");
    ui.scanPanel.classList.remove("hidden");
    ui.scanLabel.textContent = "Scanning…";
    ui.progress.style.width = "0%";

    let total = 0;
    const source = new EventSource("/api/scan");

    source.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      if (msg.type === "start") {
        total = msg.total;
        if (total === 0) {
          source.close();
          state.scanning = false;
          ui.scanPanel.classList.add("hidden");
          ui.results.classList.add("hidden");
          ui.emptyState.classList.remove("hidden");
          return;
        }
        ui.results.classList.remove("hidden");
        ui.scanCount.textContent = `0 / ${total}`;
      } else if (msg.type === "photo") {
        state.photos.push({ name: msg.name, score: msg.score, decode: msg.decode });
        ui.scanCount.textContent = `${msg.index} / ${total}`;
        ui.progress.style.width = `${(msg.index / total) * 100}%`;
        scheduleRender();
      } else if (msg.type === "skipped") {
        state.skipped.push({ name: msg.name, reason: msg.reason });
        ui.scanCount.textContent = `${msg.index} / ${total}`;
        ui.progress.style.width = `${(msg.index / total) * 100}%`;
        renderSkipped();
      } else if (msg.type === "done") {
        source.close();
        state.scanning = false;
        ui.scanLabel.textContent = "Scan complete";
        ui.apply.disabled = state.photos.length === 0;
        fitSliderRange();
        render();
        setTimeout(() => ui.scanPanel.classList.add("hidden"), 1200);
      }
    };

    source.onerror = () => {
      source.close();
      state.scanning = false;
      ui.scanLabel.textContent = "Scan failed — is the server still running?";
    };
  }

  /* Slider must be able to reach the whole range of real scores. */
  function fitSliderRange() {
    if (!state.photos.length) return;
    const max = Math.max(...state.photos.map((p) => p.score));
    const ceiling = Math.max(200, Math.ceil(max * 1.05));
    ui.slider.max = String(ceiling);
    if (state.threshold > ceiling) setThreshold(ceiling);
  }

  /* ---------------- threshold ---------------- */

  function setThreshold(value) {
    const next = Math.max(0, Number(value) || 0);
    state.threshold = next;
    ui.slider.value = String(next);
    ui.number.value = String(Math.round(next));
    ui.display.textContent = String(Math.round(next));
    render();
  }

  ui.slider.addEventListener("input", (e) => setThreshold(e.target.value));
  ui.number.addEventListener("change", (e) => setThreshold(e.target.value));
  ui.rescan.addEventListener("click", scan);
  ui.apply.addEventListener("click", apply);

  /* ---------------- rendering ---------------- */

  /* Repainting the grids on every streamed photo would recreate every <img>
     element dozens of times, which flickers and re-requests thumbnails. Coalesce
     into one paint per animation frame instead. */
  let renderQueued = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      render();
    });
  }

  function render() {
    const keep = state.photos.filter((p) => bucketOf(p) === "keep");
    const reject = state.photos.filter((p) => bucketOf(p) !== "keep");

    ui.statKeep.textContent = keep.length;
    ui.statReject.textContent = reject.length;
    ui.statTotal.textContent = state.photos.length;
    ui.keepCount.textContent = keep.length;
    ui.rejectCount.textContent = reject.length;

    if (state.photos.length) {
      const scores = state.photos.map((p) => p.score);
      const lo = Math.min(...scores);
      const hi = Math.max(...scores);
      ui.statRange.textContent = `${Math.round(lo)}–${Math.round(hi)}`;
    } else {
      ui.statRange.textContent = "—";
    }

    // Sort sharpest-first in Keep, blurriest-first in Reject: the photos most
    // likely to be misclassified end up nearest the top of each column.
    paintGrid(ui.keepGrid, [...keep].sort((a, b) => a.score - b.score), true);
    paintGrid(ui.rejectGrid, [...reject].sort((a, b) => b.score - a.score), false);
    drawHistogram();
  }

  /* Cards are cached by filename so a repaint reorders existing nodes instead of
     rebuilding them - the browser then never restarts a thumbnail download. */
  const cardCache = new Map();

  /* Clicking a photo cycles: follow the slider -> force keep -> force reject. */
  function cycleOverride(name) {
    const current = state.overrides.get(name);
    if (current === undefined) state.overrides.set(name, "keep");
    else if (current === "keep") state.overrides.set(name, "reject");
    else state.overrides.delete(name);
    render();
  }

  function cardFor(photo) {
    let card = cardCache.get(photo.name);
    if (!card) {
      // A button, not a div, so it is reachable by keyboard.
      card = document.createElement("button");
      card.type = "button";
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = `/api/thumb/${encodeURIComponent(photo.name)}`;
      img.alt = photo.name;
      const badge = document.createElement("span");
      badge.className = "photo-badge";
      const meta = document.createElement("div");
      meta.className = "photo-meta";
      const score = document.createElement("div");
      score.className = "photo-score";
      const name = document.createElement("div");
      name.className = "photo-name";
      meta.append(score, name);
      card.append(img, badge, meta);
      card.addEventListener("click", () => cycleOverride(photo.name));
      cardCache.set(photo.name, card);
    }
    const forced = state.overrides.get(photo.name);
    card.querySelector(".photo-score").textContent = photo.score.toFixed(1);
    const name = card.querySelector(".photo-name");
    name.textContent = photo.name;
    const badge = card.querySelector(".photo-badge");
    badge.textContent = forced === undefined ? "" : forced === "keep" ? "kept by you" : "binned by you";
    card.classList.toggle("photo-forced", forced !== undefined);
    card.title = forced === undefined
      ? `${photo.name} \u2014 decoded via ${photo.decode}. Click to force keep.`
      : `${photo.name} \u2014 forced to ${forced}. Click to change.`;
    return card;
  }

  function paintGrid(container, photos, isKeep) {
    const nodes = photos.map((photo) => {
      const card = cardFor(photo);
      card.className = "photo" + (isKeep ? " photo-keep" : "");
      return card;
    });
    container.replaceChildren(...nodes);
  }


  function renderSkipped() {
    if (!state.skipped.length) {
      ui.skippedPanel.classList.add("hidden");
      return;
    }
    ui.skippedPanel.classList.remove("hidden");
    ui.skippedItems.replaceChildren();
    for (const item of state.skipped) {
      const row = document.createElement("div");
      row.className = "skipped-item";
      const name = document.createElement("span");
      name.className = "label";
      name.textContent = item.name;
      const reason = document.createElement("span");
      reason.textContent = item.reason;
      row.append(name, reason);
      ui.skippedItems.append(row);
    }
  }

  /* ---------------- histogram ---------------- */

  const SVG_NS = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    return node;
  };

  /* A bar with rounded top corners only, anchored flat to the baseline. */
  function barPath(x, y, w, h, r) {
    const radius = Math.min(r, w / 2, h);
    return `M${x},${y + h} L${x},${y + radius} Q${x},${y} ${x + radius},${y}` +
           ` L${x + w - radius},${y} Q${x + w},${y} ${x + w},${y + radius}` +
           ` L${x + w},${y + h} Z`;
  }

  function drawHistogram() {
    const svg = ui.histogram;
    svg.replaceChildren();
    if (!state.photos.length) return;

    const width = svg.clientWidth || 800;
    const height = 150;
    const pad = { top: 8, right: 4, bottom: 22, left: 4 };
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

    const scores = state.photos.map((p) => p.score);
    const maxScore = Math.max(...scores, 1);
    const binCount = Math.min(32, Math.max(8, Math.ceil(Math.sqrt(scores.length) * 3)));

    /* Sharpness scores are heavily right-skewed: one very sharp frame can score
       20x the median, which on a linear axis crushes every other photo into the
       first couple of bins - exactly where the threshold lives. Binning in
       sqrt-space spreads the low end out while still showing the outlier, with
       no truncation. Tick labels below stay real score values. */
    const toAxis = (score) => Math.sqrt(Math.max(0, score) / maxScore);
    const fromAxis = (unit) => unit * unit * maxScore;

    const bins = new Array(binCount).fill(0);
    for (const s of scores) {
      bins[Math.min(binCount - 1, Math.floor(toAxis(s) * binCount))] += 1;
    }
    const maxCount = Math.max(...bins, 1);

    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const slot = plotW / binCount;
    const gap = 2;                     // 2px surface gap between adjacent bars
    const barW = Math.max(1, slot - gap);
    const xOf = (score) => pad.left + toAxis(score) * plotW;

    bins.forEach((count, i) => {
      const binStart = fromAxis(i / binCount);
      const binEnd = fromAxis((i + 1) / binCount);
      const h = count === 0 ? 0 : Math.max(2, (count / maxCount) * plotH);
      if (h === 0) return;
      // A bin is "keep" when its whole range sits at or above the threshold.
      const isKeep = binStart >= state.threshold;
      const bar = make("path", {
        d: barPath(pad.left + i * slot + gap / 2, pad.top + plotH - h, barW, h, 4),
        class: "bar " + (isKeep ? "bar-keep" : "bar-reject"),
      });
      const label = make("title", {});
      label.textContent = `${count} photo${count === 1 ? "" : "s"} scoring ` +
                          `${Math.round(binStart)}–${Math.round(binEnd)}`;
      bar.append(label);
      svg.append(bar);
    });

    // Recessive baseline
    svg.append(make("line", {
      x1: pad.left, y1: pad.top + plotH, x2: width - pad.right, y2: pad.top + plotH,
      class: "axis-line",
    }));

    // A few tick labels, not one per bar. Values are real scores, spaced by the
    // sqrt axis, so the gaps between labels widen towards the right.
    for (let t = 0; t <= 4; t += 1) {
      const value = fromAxis(t / 4);
      const text = make("text", {
        x: Math.min(width - pad.right - 10, Math.max(pad.left + 2, xOf(value))),
        y: height - 6,
        class: "axis-text",
        "text-anchor": t === 0 ? "start" : t === 4 ? "end" : "middle",
      });
      text.textContent = Math.round(value);
      svg.append(text);
    }

    // Threshold marker
    if (state.threshold <= maxScore) {
      const x = xOf(state.threshold);
      svg.append(make("line", {
        x1: x, y1: pad.top, x2: x, y2: pad.top + plotH, class: "threshold-line",
      }));
    }
  }

  window.addEventListener("resize", () => drawHistogram());

  /* ---------------- apply ---------------- */

  async function apply() {
    if (!state.photos.length) return;
    ui.apply.disabled = true;
    const buckets = {};
    for (const p of state.photos) buckets[p.name] = bucketOf(p);

    try {
      const response = await fetch("/api/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ buckets }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "apply failed");
      toast(`Copied ${result.kept} to keep, ${result.rejected} to reject`);
    } catch (err) {
      toast(`Could not apply: ${err.message}`);
    } finally {
      ui.apply.disabled = false;
    }
  }

  function toast(message) {
    ui.toast.textContent = message;
    ui.toast.classList.add("visible");
    setTimeout(() => ui.toast.classList.remove("visible"), 3200);
  }

  /* ---------------- boot ---------------- */

  setThreshold(state.threshold);
  if (window.PHOTOMATION.photoCount > 0) scan();
})();
