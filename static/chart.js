/* Printvault — temperature and stage chart on the print detail page (data in #samples-data). */
(function () {
  "use strict";
  if (!document.getElementById("tempchart")) return;
  const S = JSON.parse(document.getElementById("samples-data").textContent);   // [time, nozzle, nozzle_target, bed, bed_target, chamber, stage, stage text, stage group, %, layer]
  const root = document.getElementById("tempchart"), plot = root.querySelector(".viz-plot"), tip = root.querySelector(".viz-tip");
  const NS = "http://www.w3.org/2000/svg";
  const pts = S.map(r => ({t: new Date(r[0]).getTime(), n: r[1], nt: r[2], b: r[3], bt: r[4], c: r[5],
                           stage: r[6], phase: r[7], group: r[8], pct: r[9], layer: r[10]}));
  const SERIES = [["n", "nt", "s1", "Nozzle"], ["b", "bt", "s2", "Bed"], ["c", null, "s3", "Chamber"]];
  const GAP = 5 * 60 * 1000;   // longer without a data point (listener offline): break the line
  const fmtTime = t => new Date(t).toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit", hour12: false});
  const fmtT = v => v == null ? "–" : `${Math.round(v)}°`;
  function mk(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    if (parent) parent.append(e);
    return e;
  }
  function niceMax(v) { const step = v > 150 ? 50 : 25; return Math.max(step, Math.ceil(v / step) * step); }
  function draw() {
    plot.replaceChildren();
    if (pts.length < 2) { plot.textContent = "Not enough data points for a chart yet."; return; }
    const W = Math.max(300, plot.clientWidth), H = 240, BAND = 26;
    const m = {l: 40, r: 92, t: 10, b: 24 + BAND + 8};
    const svg = mk("svg", {viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
                           "aria-label": "Nozzle, bed and chamber temperature over time, with the stages of the print"}, plot);
    const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    const all = pts.flatMap(p => [p.n, p.nt, p.b, p.bt, p.c]).filter(v => v != null);
    const yMax = niceMax(Math.max(...all, 50));
    const x = t => m.l + (t - t0) / Math.max(1, t1 - t0) * (W - m.l - m.r);
    const yb = H - m.b, y = v => yb - v / yMax * (yb - m.t);
    // grid and axes
    const step = yMax > 150 ? 50 : 25;
    for (let v = 0; v <= yMax; v += step) {
      mk("line", {x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: v ? "gridline" : "base"}, svg);
      mk("text", {x: m.l - 6, y: y(v) + 4, "text-anchor": "end", class: "axis"}, svg).textContent = v + "°";
    }
    const span = t1 - t0, minutes = [5, 10, 15, 30, 60, 120, 240].find(mi => span / (mi * 60000) <= 6) || 480;
    const tickMs = minutes * 60000;
    for (let t = Math.ceil(t0 / tickMs) * tickMs; t <= t1; t += tickMs) {
      mk("text", {x: x(t), y: yb + 16, "text-anchor": "middle", class: "axis"}, svg).textContent = fmtTime(t);
    }
    // lines (target dashed, as steps: a target jumps)
    function path(key, stepwise) {
      let d = "", prev = null;
      for (const p of pts) {
        const v = p[key];
        if (v == null) { prev = null; continue; }
        const X = x(p.t), Y = y(v);
        if (!prev || p.t - prev.t > GAP) d += `M${X},${Y}`;
        else d += stepwise ? `H${X}V${Y}` : `L${X},${Y}`;
        prev = p;
      }
      return d;
    }
    const labels = [];
    for (const [key, target, cls, name] of SERIES) {
      if (target && pts.some(p => p[target])) mk("path", {d: path(target, true), class: `line target ${cls}`}, svg);
      if (!pts.some(p => p[key] != null)) continue;
      mk("path", {d: path(key, false), class: `line ${cls}`}, svg);
      const last = [...pts].reverse().find(p => p[key] != null);
      labels.push({y: y(last[key]), text: `${name} ${fmtT(last[key])}`, cls});
    }
    // direct labels on the right, pushed apart
    labels.sort((a, b) => a.y - b.y);
    for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 14);
    for (const l of labels) {
      mk("circle", {cx: W - m.r + 8, cy: l.y, r: 4, class: `dot ${l.cls}`}, svg);
      mk("text", {x: W - m.r + 16, y: l.y + 4, class: "dlabel"}, svg).textContent = l.text;
    }
    // stage band: text carries the stage, colour only for pauses (with ⏸)
    const by = H - BAND - 4;
    let segStart = 0, alt = 0;
    for (let i = 1; i <= pts.length; i++) {
      if (i < pts.length && pts[i].stage === pts[segStart].stage) continue;
      const a = pts[segStart], b = pts[Math.min(i, pts.length - 1)];
      const x1 = x(a.t), x2 = Math.max(x1 + 2, x(b.t) - 2);
      const pause = a.group === "paused";
      const g = mk("g", {class: "seg"}, svg);
      mk("rect", {x: x1, y: by, width: x2 - x1, height: BAND, rx: 4, class: pause ? "band pause" : `band alt${alt}`}, g);
      const label = (pause ? "⏸ " : "") + (a.phase || "End");
      if (x2 - x1 > label.length * 6.5 + 8) {
        mk("text", {x: x1 + 6, y: by + BAND / 2 + 4, class: "bandlabel"}, g).textContent = label;
      }
      mk("title", {}, g).textContent = `${a.phase || "End"}: ${fmtTime(a.t)}–${fmtTime(b.t)}`;
      alt = 1 - alt;
      segStart = i;
    }
    // crosshair and tooltip
    const cross = mk("line", {y1: m.t, y2: yb, class: "cross", visibility: "hidden"}, svg);
    const hit = mk("rect", {x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent"}, svg);
    function move(ev) {
      const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width;
      const t = t0 + (px - m.l) / (W - m.l - m.r) * (t1 - t0);
      const p = pts.reduce((best, q) => Math.abs(q.t - t) < Math.abs(best.t - t) ? q : best, pts[0]);
      cross.setAttribute("x1", x(p.t)); cross.setAttribute("x2", x(p.t)); cross.setAttribute("visibility", "visible");
      tip.replaceChildren();
      const head = document.createElement("strong");
      head.textContent = `${fmtTime(p.t)} · ${p.phase || "End"}`;
      tip.append(head);
      for (const [label, cur, tar, cls] of [["Nozzle", p.n, p.nt, "s1"], ["Bed", p.b, p.bt, "s2"], ["Chamber", p.c, null, "s3"]]) {
        const row = document.createElement("div"), sw = document.createElement("i");
        sw.className = `sw ${cls}`;
        row.append(sw, `${label} ${fmtT(cur)}${tar ? ` → ${fmtT(tar)}` : ""}`);
        tip.append(row);
      }
      if (p.pct != null || p.layer != null) {
        const row = document.createElement("div");
        row.className = "muted";
        row.textContent = `${p.pct ?? "–"}% · layer ${p.layer ?? "–"}`;
        tip.append(row);
      }
      tip.hidden = false;
      const left = x(p.t) / W * r.width;
      tip.style.left = Math.min(Math.max(0, left + 12), r.width - tip.offsetWidth) + "px";
    }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  }
  draw();
  let w = plot.clientWidth;
  new ResizeObserver(() => { if (plot.clientWidth !== w) { w = plot.clientWidth; draw(); } }).observe(plot);
})();
