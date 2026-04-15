// UC Artemis review SPA controller.
//
// Architecture:
//   - PDF.js renders the current page into a <canvas> at scale 1.0 (the canvas
//     size IS the page size in points). All zoom and pan happens via a single
//     CSS transform on a wrapper div that contains BOTH the canvas and the SVG
//     overlay, so the two stay perfectly aligned at any zoom level.
//   - The SVG overlay uses a viewBox that matches the page in PDF user space
//     (points). All callout coordinates are stored and transmitted in PDF user
//     space; SVG handles the rendering math via the viewBox.
//   - Wheel zoom is anchored at the cursor (the world point under the cursor
//     stays under the cursor). Drag-pan moves the wrapper. Drag on a callout
//     box repositions that box.
//   - Existing callouts (already placed in earlier rows) load via
//     /api/page/<n>/callouts and render in gray. The current draft renders in
//     red. Both are draggable; on drag-release, existing callouts auto-save
//     via /api/update_callout, and the current draft just updates client-side
//     state (the user clicks Accept to commit).

const cfg = window.UC_ARTEMIS;
const SVG_NS = "http://www.w3.org/2000/svg";

// PDF.js global setup
pdfjsLib.GlobalWorkerOptions.workerSrc = cfg.pdfWorker;

// ---------- Element refs ----------

const canvasArea = document.getElementById("canvas-area");
const canvas = document.getElementById("pdf-canvas");
const overlay = document.getElementById("overlay");
const ctx = canvas.getContext("2d");

const openingName = document.getElementById("opening-name");
const bodyText = document.getElementById("body-text");
const matchInfoText = document.getElementById("match-info-text");
const zeroMatchCard = document.getElementById("zero-match-card");
const zeroOpening = document.getElementById("zero-opening");
const loadingCard = document.getElementById("loading-card");

const acceptBtn = document.getElementById("accept-btn");
const nextBtn = document.getElementById("next-btn");
const skipBtn = document.getElementById("skip-btn");

const progPlaced = document.getElementById("prog-placed");
const progSkipped = document.getElementById("prog-skipped");
const progUnmatched = document.getElementById("prog-unmatched");
const progTotal = document.getElementById("prog-total");

// ---------- App state ----------

let pdfDoc = null;          // pdfjs PDFDocumentProxy
let currentPage = null;     // pdfjs PDFPageProxy
let currentPageIndex = -1;  // -1 = nothing rendered
let pageWidth = 0;          // in PDF points
let pageHeight = 0;

let viewState = {
  scale: 1.0,    // CSS transform scale; 1.0 = fit-to-area
  tx: 0,         // CSS transform translate-x in screen pixels
  ty: 0,
};

let currentDraftBox = null;   // {x0,y0,x1,y1} in PDF points; null until match loaded
let currentDraftAnchor = null;
let existingCallouts = [];    // array of {xref, body_text, box:[..], anchor:[..]}

let serverState = null;       // last /api/state response

// Drag tracking
let dragKind = null;          // 'pan' | 'draft' | 'existing' | null
let dragData = null;          // payload depending on dragKind
const DRAG_THRESHOLD_PX = 3;

// ---------- Coordinate helpers ----------

// Convert a screen pixel point (relative to canvasArea) to PDF user space.
function screenToPdf(sx, sy) {
  // The wrapper transform is: translate(tx,ty) scale(scale).
  // Inverse: pdfX = (sx - tx) / scale * (pageWidth / canvas.width) ... but we set
  // canvas.width == pageWidth so no extra factor.
  return {
    x: (sx - viewState.tx) / viewState.scale,
    y: (sy - viewState.ty) / viewState.scale,
  };
}

function pointInsideBox(pt, box) {
  return pt.x >= box[0] && pt.x <= box[2] && pt.y >= box[1] && pt.y <= box[3];
}

// ---------- Transform application ----------

function applyTransform() {
  const t = `translate(${viewState.tx}px, ${viewState.ty}px) scale(${viewState.scale})`;
  canvas.style.transform = t;
  overlay.style.transform = t;
}

function fitPageToArea() {
  const areaW = canvasArea.clientWidth;
  const areaH = canvasArea.clientHeight;
  if (!pageWidth || !pageHeight) return;
  const fit = Math.min(areaW / pageWidth, areaH / pageHeight) * 0.95;
  viewState.scale = fit;
  viewState.tx = (areaW - pageWidth * fit) / 2;
  viewState.ty = (areaH - pageHeight * fit) / 2;
  applyTransform();
}

function centerOnPdfPoint(px, py, targetScale) {
  const areaW = canvasArea.clientWidth;
  const areaH = canvasArea.clientHeight;
  if (targetScale != null) viewState.scale = targetScale;
  viewState.tx = areaW / 2 - px * viewState.scale;
  viewState.ty = areaH / 2 - py * viewState.scale;
  applyTransform();
}

// ---------- PDF.js page rendering ----------

async function renderPage(pageIndex) {
  if (currentPageIndex === pageIndex) return;
  currentPage = await pdfDoc.getPage(pageIndex + 1); // pdfjs uses 1-based
  currentPageIndex = pageIndex;

  // Render at scale 1.0 so canvas pixels ≈ PDF points. We use devicePixelRatio
  // for crispness on hi-DPI displays without changing the logical size.
  const baseViewport = currentPage.getViewport({ scale: 1.0 });
  pageWidth = baseViewport.width;
  pageHeight = baseViewport.height;

  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const renderViewport = currentPage.getViewport({ scale: dpr });
  canvas.width = renderViewport.width;
  canvas.height = renderViewport.height;
  canvas.style.width = pageWidth + "px";
  canvas.style.height = pageHeight + "px";

  await currentPage.render({
    canvasContext: ctx,
    viewport: renderViewport,
    annotationMode: pdfjsLib.AnnotationMode.DISABLE,
  }).promise;

  overlay.setAttribute("width", pageWidth);
  overlay.setAttribute("height", pageHeight);
  overlay.setAttribute("viewBox", `0 0 ${pageWidth} ${pageHeight}`);
  overlay.style.width = pageWidth + "px";
  overlay.style.height = pageHeight + "px";
}

// ---------- SVG overlay rendering ----------

function clearOverlay() {
  while (overlay.firstChild) overlay.removeChild(overlay.firstChild);
}

function makeCallout(box, anchor, label, kind, xref) {
  // kind: 'current' (red, draggable, current draft) or 'existing' (gray, draggable)
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", `callout callout-${kind}`);
  if (xref != null) g.dataset.xref = String(xref);

  // Leader line: anchor -> nearest edge of box
  const knee = nearestEdgePoint(box, anchor);
  const line = document.createElementNS(SVG_NS, "line");
  line.setAttribute("x1", anchor[0]);
  line.setAttribute("y1", anchor[1]);
  line.setAttribute("x2", knee.x);
  line.setAttribute("y2", knee.y);
  line.setAttribute("class", "leader");
  g.appendChild(line);

  // Anchor dot
  const dot = document.createElementNS(SVG_NS, "circle");
  dot.setAttribute("cx", anchor[0]);
  dot.setAttribute("cy", anchor[1]);
  dot.setAttribute("r", 4);
  dot.setAttribute("class", "anchor-dot");
  g.appendChild(dot);

  // Box
  const rect = document.createElementNS(SVG_NS, "rect");
  rect.setAttribute("x", box[0]);
  rect.setAttribute("y", box[1]);
  rect.setAttribute("width", box[2] - box[0]);
  rect.setAttribute("height", box[3] - box[1]);
  rect.setAttribute("class", "box");
  g.appendChild(rect);

  // Text. Wrap at the box width using <tspan>s — a simple word wrap.
  const padding = 6;
  const lineHeight = 12;
  const text = document.createElementNS(SVG_NS, "text");
  text.setAttribute("x", box[0] + padding);
  text.setAttribute("y", box[1] + padding + lineHeight);
  text.setAttribute("class", "label");
  const lines = wordWrap(label, (box[2] - box[0]) - padding * 2, 6.5);
  for (let i = 0; i < lines.length; i++) {
    const tspan = document.createElementNS(SVG_NS, "tspan");
    tspan.setAttribute("x", box[0] + padding);
    if (i > 0) tspan.setAttribute("dy", lineHeight);
    tspan.textContent = lines[i];
    text.appendChild(tspan);
  }
  g.appendChild(text);

  return g;
}

function nearestEdgePoint(box, anchor) {
  const cx = (box[0] + box[2]) / 2;
  const cy = (box[1] + box[3]) / 2;
  const dx = anchor[0] - cx;
  const dy = anchor[1] - cy;
  if (Math.abs(dx) >= Math.abs(dy)) {
    return { x: dx > 0 ? box[2] : box[0], y: cy };
  }
  return { x: cx, y: dy > 0 ? box[3] : box[1] };
}

function wordWrap(text, maxWidth, charWidth) {
  // Crude monospace approximation. Good enough for short callout text.
  const words = String(text || "").split(/\s+/);
  const lines = [];
  let current = "";
  const maxChars = Math.max(1, Math.floor(maxWidth / charWidth));
  for (const w of words) {
    if (!current) {
      current = w;
    } else if ((current + " " + w).length <= maxChars) {
      current += " " + w;
    } else {
      lines.push(current);
      current = w;
    }
    if (lines.length >= 3) break; // cap at 3 lines
  }
  if (current && lines.length < 3) lines.push(current);
  return lines.length ? lines : [""];
}

function renderCalloutsForCurrentMatch() {
  clearOverlay();

  // Existing callouts (gray)
  for (const c of existingCallouts) {
    overlay.appendChild(makeCallout(c.box, c.anchor, c.body_text, "existing", c.xref));
  }

  // Current draft (red)
  if (currentDraftBox && currentDraftAnchor) {
    const label = serverState ? serverState.body_text : "";
    overlay.appendChild(
      makeCallout(currentDraftBox, currentDraftAnchor, label, "current", null)
    );
  }
}

function findCalloutAtPdfPoint(pt) {
  // Hit-test current draft first (it's "on top"), then existing.
  if (currentDraftBox && pointInsideBox(pt, currentDraftBox)) {
    return { kind: "current" };
  }
  for (const c of existingCallouts) {
    if (pointInsideBox(pt, c.box)) {
      return { kind: "existing", xref: c.xref };
    }
  }
  return null;
}

// ---------- Drag handling ----------

function onPointerDown(ev) {
  if (ev.button !== 0) return;
  const rect = canvasArea.getBoundingClientRect();
  const sx = ev.clientX - rect.left;
  const sy = ev.clientY - rect.top;
  const pdfPt = screenToPdf(sx, sy);

  const hit = findCalloutAtPdfPoint(pdfPt);
  if (hit) {
    let target;
    if (hit.kind === "current") {
      target = { box: currentDraftBox.slice() };
    } else {
      const c = existingCallouts.find((c) => c.xref === hit.xref);
      if (!c) return;
      target = { box: c.box.slice(), xref: c.xref };
    }
    dragKind = hit.kind;
    dragData = {
      startScreen: { x: sx, y: sy },
      startPdf: pdfPt,
      origBox: target.box,
      xref: target.xref,
      moved: false,
    };
  } else {
    dragKind = "pan";
    dragData = {
      startScreen: { x: sx, y: sy },
      origTx: viewState.tx,
      origTy: viewState.ty,
      moved: false,
    };
  }
  ev.preventDefault();
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
}

function onPointerMove(ev) {
  if (!dragKind) return;
  const rect = canvasArea.getBoundingClientRect();
  const sx = ev.clientX - rect.left;
  const sy = ev.clientY - rect.top;
  const dxScreen = sx - dragData.startScreen.x;
  const dyScreen = sy - dragData.startScreen.y;
  if (
    !dragData.moved &&
    Math.abs(dxScreen) < DRAG_THRESHOLD_PX &&
    Math.abs(dyScreen) < DRAG_THRESHOLD_PX
  ) {
    return;
  }
  dragData.moved = true;

  if (dragKind === "pan") {
    viewState.tx = dragData.origTx + dxScreen;
    viewState.ty = dragData.origTy + dyScreen;
    applyTransform();
    return;
  }

  // Callout drag — convert screen delta to PDF points via current scale.
  const dxPdf = dxScreen / viewState.scale;
  const dyPdf = dyScreen / viewState.scale;
  const newBox = [
    dragData.origBox[0] + dxPdf,
    dragData.origBox[1] + dyPdf,
    dragData.origBox[2] + dxPdf,
    dragData.origBox[3] + dyPdf,
  ];

  if (dragKind === "current") {
    currentDraftBox = newBox;
  } else if (dragKind === "existing") {
    const c = existingCallouts.find((c) => c.xref === dragData.xref);
    if (c) c.box = newBox;
  }
  renderCalloutsForCurrentMatch();
}

async function onPointerUp(ev) {
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  if (!dragKind) return;
  const wasMoved = dragData.moved;
  const kind = dragKind;
  const data = dragData;
  dragKind = null;
  dragData = null;

  // For existing callouts that actually moved, persist via API.
  if (kind === "existing" && wasMoved) {
    const c = existingCallouts.find((c) => c.xref === data.xref);
    if (c) {
      try {
        await fetch(cfg.urls.update_callout, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ xref: c.xref, box: c.box }),
        });
      } catch (e) {
        console.error("Failed to save callout move", e);
      }
    }
  }
}

// ---------- Wheel zoom (cursor-anchored) ----------

function onWheel(ev) {
  ev.preventDefault();
  const rect = canvasArea.getBoundingClientRect();
  const sx = ev.clientX - rect.left;
  const sy = ev.clientY - rect.top;

  // World point under cursor BEFORE zoom
  const before = screenToPdf(sx, sy);

  const delta = -ev.deltaY;
  const factor = delta > 0 ? 1.1 : 1 / 1.1;
  const newScale = Math.min(8, Math.max(0.1, viewState.scale * factor));
  viewState.scale = newScale;

  // Translate so the same world point ends up under the cursor AFTER zoom.
  viewState.tx = sx - before.x * newScale;
  viewState.ty = sy - before.y * newScale;
  applyTransform();
}

// ---------- State fetch / advance ----------

async function fetchState() {
  const r = await fetch(cfg.urls.state);
  return await r.json();
}

async function pollStateUntilReady() {
  while (true) {
    const s = await fetchState();
    if (s.done) {
      window.location.href = s.next || cfg.urls.done;
      return null;
    }
    if (s.waiting) {
      loadingCard.classList.remove("hidden");
      await new Promise((res) => setTimeout(res, 500));
      continue;
    }
    loadingCard.classList.add("hidden");
    return s;
  }
}

async function loadState(s) {
  serverState = s;

  // Update header
  openingName.textContent = s.opening;
  bodyText.textContent = s.body_text || "";

  // Progress
  const p = s.progress || {};
  progPlaced.textContent = p.placed || 0;
  progSkipped.textContent = p.skipped || 0;
  progUnmatched.textContent = p.unmatched || 0;
  progTotal.textContent = p.total || 0;

  if (s.zero_match) {
    matchInfoText.textContent = "no matches";
    zeroMatchCard.classList.remove("hidden");
    zeroOpening.textContent = s.opening;
    canvas.style.visibility = "hidden";
    overlay.style.visibility = "hidden";
    acceptBtn.disabled = true;
    nextBtn.disabled = true;
    return;
  }

  zeroMatchCard.classList.add("hidden");
  canvas.style.visibility = "visible";
  overlay.style.visibility = "visible";

  matchInfoText.textContent = `Match ${s.match_index + 1} of ${s.match_count} \u2014 page ${s.match.page_label}`;

  await renderPage(s.match.page_index);

  // Load existing callouts on this page
  const url = cfg.urls.page_callouts.replace("__P__", String(s.match.page_index));
  const r = await fetch(url);
  const data = await r.json();
  existingCallouts = (data.callouts || []).map((c) => ({
    xref: c.xref,
    body_text: c.body_text,
    box: c.box.slice(),
    anchor: c.anchor.slice(),
  }));

  currentDraftBox = s.match.auto_box_pdf.slice();
  currentDraftAnchor = s.match.anchor_pdf.slice();
  renderCalloutsForCurrentMatch();

  // Auto-zoom to the anchor on every match change so the user immediately
  // sees both the matched text and the prospective callout box without panning.
  centerOnPdfPoint(
    (currentDraftAnchor[0] + (currentDraftBox[0] + currentDraftBox[2]) / 2) / 2,
    (currentDraftAnchor[1] + (currentDraftBox[1] + currentDraftBox[3]) / 2) / 2,
    2.0
  );

  acceptBtn.disabled = false;
  nextBtn.disabled = s.match_count <= 1;
}

async function advanceTo(s) {
  if (s.done) {
    window.location.href = s.next || cfg.urls.done;
    return;
  }
  if (s.waiting) {
    const ready = await pollStateUntilReady();
    if (ready) await loadState(ready);
    return;
  }
  await loadState(s);
}

// ---------- Action handlers ----------

async function doAccept() {
  if (acceptBtn.disabled) return;
  acceptBtn.disabled = true;
  nextBtn.disabled = true;
  skipBtn.disabled = true;
  try {
    const r = await fetch(cfg.urls.accept, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ box: currentDraftBox }),
    });
    const s = await r.json();
    await advanceTo(s);
  } finally {
    skipBtn.disabled = false;
  }
}

async function doNext() {
  if (nextBtn.disabled) return;
  const r = await fetch(cfg.urls.next, { method: "POST" });
  const s = await r.json();
  await loadState(s);
}

async function doSkip() {
  skipBtn.disabled = true;
  acceptBtn.disabled = true;
  nextBtn.disabled = true;
  try {
    const r = await fetch(cfg.urls.skip, { method: "POST" });
    const s = await r.json();
    await advanceTo(s);
  } finally {
    skipBtn.disabled = false;
  }
}

acceptBtn.addEventListener("click", doAccept);
nextBtn.addEventListener("click", doNext);
skipBtn.addEventListener("click", doSkip);

// ---------- Keyboard shortcuts ----------

document.addEventListener("keydown", (ev) => {
  if (ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA")) return;
  switch (ev.key.toLowerCase()) {
    case "y":
      ev.preventDefault();
      doAccept();
      break;
    case "n":
      ev.preventDefault();
      doNext();
      break;
    case "s":
      ev.preventDefault();
      doSkip();
      break;
    case "0":
      ev.preventDefault();
      fitPageToArea();
      break;
    case "+":
    case "=":
      ev.preventDefault();
      stepZoom(1.2);
      break;
    case "-":
      ev.preventDefault();
      stepZoom(1 / 1.2);
      break;
  }
});

function stepZoom(factor) {
  const cx = canvasArea.clientWidth / 2;
  const cy = canvasArea.clientHeight / 2;
  const before = screenToPdf(cx, cy);
  viewState.scale = Math.min(8, Math.max(0.1, viewState.scale * factor));
  viewState.tx = cx - before.x * viewState.scale;
  viewState.ty = cy - before.y * viewState.scale;
  applyTransform();
}

// ---------- Wire up canvas event handlers ----------

canvasArea.addEventListener("pointerdown", onPointerDown);
canvasArea.addEventListener("wheel", onWheel, { passive: false });
window.addEventListener("resize", () => {
  // On resize, just re-apply transform. We don't refit because the user may
  // have already adjusted the view.
  applyTransform();
});

// ---------- Bootstrap ----------

(async function bootstrap() {
  loadingCard.classList.remove("hidden");
  try {
    pdfDoc = await pdfjsLib.getDocument({ url: cfg.urls.pdf }).promise;
  } catch (e) {
    console.error("Failed to load PDF", e);
    loadingCard.innerHTML = "<p>Failed to load PDF: " + e.message + "</p>";
    return;
  }
  const s = await pollStateUntilReady();
  if (s) await loadState(s);
})();
