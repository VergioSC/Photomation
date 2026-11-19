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
        state.photos.push({ name: msg.name, score: msg.score, decode: msg.decode,
                            shot: msg.shot || "", iso: msg.iso || 0 });
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

  /* ---------------- bursts and exposure ---------------- */

  /* Two frames count as the same shot when at most this many of the 64 bits
     differ. Measured on a real shoot: five frames of one scene sat at 2-8
     bits apart, and every unrelated frame was 23 or more. Anywhere in that
     gap works; this sits in the middle of it. */
  const BURST_BITS = 10;

  /* More than this much of the frame crushed to black, or blown to white,
     and the photo is a mistake rather than a dark or bright photograph. */
  const TOO_BLACK = 0.55;
  const TOO_BLOWN = 0.25;

  const POPCOUNT = Array.from({ length: 256 }, (_, i) =>
    (i & 1) + ((i >> 1) & 1) + ((i >> 2) & 1) + ((i >> 3) & 1) +
    ((i >> 4) & 1) + ((i >> 5) & 1) + ((i >> 6) & 1) + ((i >> 7) & 1));

  /* How many of the 64 bits differ. The hashes arrive as hex because a
     JavaScript number cannot hold 64 bits of integer, so they are compared a
     byte at a time. */
  function bitsApart(a, b) {
    if (!a || !b) return 64;
    let n = 0;
    for (let i = 0; i < 16; i += 2) {
      n += POPCOUNT[parseInt(a.slice(i, i + 2), 16) ^ parseInt(b.slice(i, i + 2), 16)];
    }
    return n;
  }

  /* Group runs of consecutive photos that look like the same shot.

     Consecutive on purpose: two similar frames from opposite ends of a shoot
     are two photographs, not a burst, and chaining them would hide one of
     them behind the other. Each group records the sharpest frame, which is
     the one worth keeping.

     Sets photo.burst = {id, index, size, pick} on every member. */
  function findBursts() {
    const groups = [];
    let run = [];
    const close = () => {
      if (run.length > 1) groups.push(run);
      run = [];
    };
    for (const photo of state.photos) {
      delete photo.burst;
      if (!run.length) { run = [photo]; continue; }
      if (bitsApart(run[run.length - 1].signature, photo.signature) <= BURST_BITS) run.push(photo);
      else { close(); run = [photo]; }
    }
    close();

    groups.forEach((group, id) => {
      const pick = group.reduce((best, p) => (p.score > best.score ? p : best), group[0]);
      group.forEach((photo, index) => {
        photo.burst = { id, index: index + 1, size: group.length, pick: photo === pick };
      });
    });
    return groups;
  }

  const badExposure = (photo) =>
    photo.black > TOO_BLACK ? "mostly black"
      : photo.blown > TOO_BLOWN ? "blown out" : "";

  /* The line above the grids: what was found, and the one click that acts. */
  function renderFindings() {
    const groups = findBursts();
    const dud = state.photos.filter((p) => badExposure(p));
    const box = el("findings");
    box.replaceChildren();
    box.classList.toggle("hidden", !groups.length && !dud.length);

    if (groups.length) {
      const extra = groups.reduce((n, g) => n + g.length - 1, 0);
      box.append(finding(
        `${groups.length} burst${groups.length === 1 ? "" : "s"}: ` +
        `${extra} near-identical frame${extra === 1 ? "" : "s"} beyond the sharpest of each.`,
        "Bin all but the sharpest",
        () => {
          for (const photo of state.photos) {
            if (photo.burst && !photo.burst.pick) state.overrides.set(photo.name, "reject");
          }
          render();
        }));
    }
    if (dud.length) {
      box.append(finding(
        `${dud.length} photo${dud.length === 1 ? " is" : "s are"} mostly black or blown out. ` +
        `Sharpness does not catch these: a clipped edge is still a hard edge.`,
        "Bin them",
        () => {
          for (const photo of dud) state.overrides.set(photo.name, "reject");
          render();
        }));
    }
  }

  function finding(text, action, onClick) {
    const row = document.createElement("div");
    row.className = "finding";
    const words = document.createElement("span");
    words.textContent = text;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-secondary btn-small";
    button.textContent = action;
    button.addEventListener("click", onClick);
    row.append(words, button);
    return row;
  }

  function render() {
    renderFindings();
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
      const shot = document.createElement("div");
      shot.className = "photo-exif";
      meta.append(score, name, shot);

      // Opening the photo must not also flip its verdict, so the viewer gets
      // its own button rather than sharing the card's click.
      const open = document.createElement("button");
      open.type = "button";
      open.className = "photo-open";
      open.textContent = "\u2922";
      open.title = "View large";
      open.addEventListener("click", (e) => { e.stopPropagation(); openViewer(photo); });

      card.append(img, badge, open, meta);
      card.addEventListener("click", () => cycleOverride(photo.name));
      cardCache.set(photo.name, card);
    }
    const forced = state.overrides.get(photo.name);
    card.querySelector(".photo-score").textContent = photo.score.toFixed(1);
    const name = card.querySelector(".photo-name");
    name.textContent = photo.name;
    card.querySelector(".photo-exif").textContent = photo.shot || "";
    const badge = card.querySelector(".photo-badge");
    const exposure = badExposure(photo);
    badge.textContent =
      forced !== undefined ? (forced === "keep" ? "kept by you" : "binned by you")
        : exposure ? exposure
          : photo.burst ? `burst ${photo.burst.index}/${photo.burst.size}` +
                          (photo.burst.pick ? " \u00b7 sharpest" : "")
            : "";
    card.classList.toggle("photo-pick", Boolean(photo.burst && photo.burst.pick));
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
      // Clamping both ends to the same minimum stacked "0" and the maximum on
      // top of each other at the left edge. Place each tick at its own spot.
      const raw = xOf(value);
      const x = t === 0 ? pad.left + 2
              : t === 4 ? width - pad.right - 2
              : Math.max(pad.left + 14, Math.min(width - pad.right - 14, raw));
      const text = make("text", {
        x,
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

  /* ---------------- the viewer ---------------- */

  const viewer = el("viewer");
  const viewerImg = el("viewer-img");
  const viewerStage = el("viewer-stage");

  /* Culling is a judgement made once per photo, dozens of times in a row. The
     viewer is where the judgement is actually possible - a grid thumbnail
     cannot show you whether a frame is sharp - so the verdict is taken here,
     with the keyboard, and the next photo is already on screen. */
  let viewingAt = -1;

  function openViewer(photo) {
    showAt(state.photos.indexOf(photo));
    viewer.classList.remove("hidden");
  }

  function showAt(index) {
    if (index < 0 || index >= state.photos.length) return;
    viewingAt = index;
    const photo = state.photos[index];
    el("viewer-name").textContent = photo.name;
    el("viewer-shot").textContent = photo.shot || "";
    el("viewer-score").textContent = `blur score ${photo.score.toFixed(1)}`;
    el("viewer-position").textContent = `${index + 1} / ${state.photos.length}`;
    // A big render, because the point is to judge whether it is actually sharp.
    viewerImg.src = `/api/preview/${encodeURIComponent(photo.name)}?from=input&size=2400&lut=none`;
    viewerStage.classList.remove("zoomed");
    paintVerdict();
  }

  function paintVerdict() {
    const photo = state.photos[viewingAt];
    const node = el("viewer-verdict");
    if (!photo) { node.textContent = ""; return; }
    const forced = state.overrides.get(photo.name);
    const bucket = bucketOf(photo);
    node.textContent = (forced !== undefined ? "you: " : "threshold: ") + bucket;
    node.className = "viewer-verdict " + (bucket === "keep" ? "is-keep" : "is-reject");
  }

  /* Decide, then move on. Stopping on the last photo rather than wrapping:
     wrapping round to the start silently re-judges work already done. */
  function judge(bucket) {
    const photo = state.photos[viewingAt];
    if (!photo) return;
    if (bucket === null) state.overrides.delete(photo.name);
    else state.overrides.set(photo.name, bucket);
    render();
    if (viewingAt + 1 < state.photos.length) showAt(viewingAt + 1);
    else { paintVerdict(); toast("Last photo. Apply & copy writes the sort to disk."); }
  }

  function closeViewer() {
    viewer.classList.add("hidden");
    viewerImg.src = "";
    viewingAt = -1;
  }

  viewerStage.addEventListener("click", () => viewerStage.classList.toggle("zoomed"));
  el("viewer-close").addEventListener("click", closeViewer);
  document.addEventListener("keydown", (e) => {
    if (viewer.classList.contains("hidden")) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const key = e.key.toLowerCase();
    const action = {
      escape: closeViewer,
      arrowright: () => showAt(viewingAt + 1),
      arrowleft: () => showAt(viewingAt - 1),
      k: () => judge("keep"),
      x: () => judge("reject"),
      u: () => judge(null),
    }[key];
    if (!action) return;
    e.preventDefault();
    action();
  });

  /* ---------------- uploading ---------------- */

  const dropzone = el("dropzone");
  const fileInput = el("file-input");
  const uploadTrack = el("upload-track");
  const uploadFill = el("upload-fill");
  const uploadList = el("upload-list");

  async function upload(files) {
    const chosen = [...files];
    if (!chosen.length) return;

    // Only worth asking when there is something to replace. On an empty
    // folder - which is how the server starts - this never appears.
    let replace = false;
    if (state.photos.length) {
      const answer = await window.ask(
        "New shoot, or more of this one?",
        `There ${state.photos.length === 1 ? "is" : "are"} already ` +
        `${state.photos.length} photo${state.photos.length === 1 ? "" : "s"} ` +
        `in this batch. Starting a new one moves them, and the keep/reject ` +
        `sort and any export, to output/.trash - nothing is deleted.`,
        ["Start a new batch", "Add to this batch"]);
      if (answer === null) return;                   // dismissed: do nothing
      replace = answer === "Start a new batch";
    }

    const form = new FormData();
    for (const f of chosen) form.append("photos", f);
    if (replace) form.append("replace", "1");

    uploadTrack.classList.remove("hidden");
    uploadFill.style.width = "0%";
    uploadList.replaceChildren();

    // XMLHttpRequest rather than fetch: it reports upload progress, and a
    // shoot of RAW files is big enough that a silent wait looks broken.
    const request = new XMLHttpRequest();
    request.open("POST", "/api/upload");
    request.upload.onprogress = (e) => {
      if (e.lengthComputable) uploadFill.style.width = `${(e.loaded / e.total) * 100}%`;
    };
    request.onload = () => {
      let data = {};
      try { data = JSON.parse(request.responseText); } catch { /* reported below */ }
      if (request.status !== 200) {
        note(data.error || `upload failed (${request.status})`, true);
        uploadTrack.classList.add("hidden");
        return;
      }
      if (data.cleared) note(`Moved ${data.cleared} file(s) from the last batch to output/.trash.`);
      if (data.saved.length) note(`Added ${data.saved.length} photo${data.saved.length === 1 ? "" : "s"}.`);
      for (const r of data.rejected || []) note(`${r.name}: ${r.reason}`, true);
      uploadTrack.classList.add("hidden");
      if (data.saved.length) scan();
    };
    request.onerror = () => {
      note("upload failed: no reply from the server", true);
      uploadTrack.classList.add("hidden");
    };
    request.send(form);
  }

  function note(text, bad) {
    const line = document.createElement("div");
    line.className = "upload-item" + (bad ? " bad" : "");
    line.textContent = text;
    uploadList.append(line);
  }

  el("new-batch").addEventListener("click", async () => {
    const answer = await window.ask(
      "Start a new batch?",
      "The photos in this batch, the keep/reject sort and any export are " +
      "moved to output/.trash. Nothing is deleted, so this can be undone by " +
      "hand, but the dashboard starts empty.",
      ["Start a new batch", "Cancel"]);
    if (answer !== "Start a new batch") return;
    const reply = await fetch("/api/new-batch", { method: "POST" });
    const data = await reply.json();
    note(`Moved ${data.moved} file(s) to ${data.trash}.`);
    scan();
  });

  /* Everything destructive in this app moves files to output/.trash instead
     of deleting them, which means that folder only ever grows. Emptying it is
     the one genuinely unrecoverable thing here, so it says how much it will
     destroy and asks first. */
  async function showTrash() {
    const button = el("empty-trash");
    try {
      const data = await (await fetch("/api/trash")).json();
      button.classList.toggle("hidden", !data.files);
      button.dataset.files = data.files;
      button.textContent = `Empty trash (${(data.bytes / 1e6).toFixed(0)} MB)`;
      button.title = `${data.files} file(s) in output/.trash`;
    } catch { button.classList.add("hidden"); }
  }

  el("empty-trash").addEventListener("click", async () => {
    const button = el("empty-trash");
    const answer = await window.ask(
      "Empty the trash?",
      `${button.dataset.files} file(s) in output/.trash are deleted permanently. ` +
      `This is the only thing in Photomation that cannot be undone - every ` +
      `other action moves files there rather than destroying them.`,
      ["Delete them permanently", "Cancel"]);
    if (answer !== "Delete them permanently") return;
    const data = await (await fetch("/api/empty-trash", { method: "POST" })).json();
    toast(`Deleted ${data.removed} file(s), freeing ${(data.freed_bytes / 1e6).toFixed(0)} MB.`);
    showTrash();
  });

  showTrash();

  el("pick").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => { upload(fileInput.files); fileInput.value = ""; });

  for (const event of ["dragenter", "dragover"]) {
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      dropzone.classList.add("over");
    });
  }
  for (const event of ["dragleave", "drop"]) {
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      if (event === "dragleave" && dropzone.contains(e.relatedTarget)) return;
      dropzone.classList.remove("over");
    });
  }
  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.files) upload(e.dataTransfer.files);
  });

  setThreshold(state.threshold);
  if (window.PHOTOMATION.photoCount > 0) scan();
})();
