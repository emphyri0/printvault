/* Printvault — live status from /api/live (the MQTT listener).
   Home page: hero per printer (camera image, progress, temperatures, AMS) and live camera images on
   the cards of running prints. Detail page: live box for the print that is running now. */
(function () {
  "use strict";
  const {el, icon, dur, deg, fmt} = PV;
  PV.ownsLive = true;
  const SNAP_MS = 30000;
  const hero = document.getElementById("live");
  const hstats = document.getElementById("hstats");
  const pageLive = document.getElementById("pagelive");
  const snapUrl = id => `/printers/${id}/snapshot.jpg?t=${Date.now()}`;
  const isActive = p => PV.ACTIVE.includes(p.gcode_state);

  // ---------- cards: running prints without an own cover show the camera image (new every 30 s) ----------
  // When the live status says the print is no longer running: one last image (label "end"), then stop.
  function snapFailed(img) {   // no camera image: back to the thumbnail, or "No photo"
    const tag = img.parentElement.querySelector(".snaptag");
    if (tag) tag.remove();
    if (img.dataset.fallback) {
      const thumb = el("img");
      thumb.alt = "";
      thumb.src = img.dataset.fallback;
      img.replaceWith(thumb);
    } else {
      const box = el("div", "noimg");
      box.append(icon("image"), el("span", null, "No photo"));
      img.replaceWith(box);
    }
  }
  function refreshSnap(img, last) {
    const next = new Image();
    next.onload = () => {
      img.src = next.src;
      if (last) {
        img.dataset.done = "1";
        const tag = img.parentElement.querySelector(".snaptag");
        if (tag) { tag.classList.add("end"); tag.querySelector(".t").textContent = "End"; }
      }
    };
    next.src = snapUrl(img.dataset.printer);
  }
  for (const img of document.querySelectorAll("img.livesnap")) {
    img.addEventListener("error", () => snapFailed(img), {once: true});
    if (img.complete && !img.naturalWidth) snapFailed(img);   // already failed before this script ran
  }
  let activePrints = null;   // print ids that are running according to the listener (null = unknown)
  setInterval(() => {
    if (document.hidden) return;
    for (const img of document.querySelectorAll("img.livesnap:not([data-done])")) {
      const ended = activePrints !== null && !activePrints.has(Number(img.dataset.print));
      refreshSnap(img, ended);
    }
  }, SNAP_MS);

  // ---------- pieces ----------
  function tempTile(ic, label, t) {
    const tile = el("div", "temp");
    tile.append(icon(ic), el("span", "tl", label), el("span", "tv num", deg(t && t.cur)));
    if (t && t.target) tile.append(el("span", "tar num", `/ ${deg(t.target)}`));
    return tile;
  }
  function temps(p) {
    const t = p.temps || {};
    if (!p.connected || !(t.nozzle || t.bed)) return null;
    const row = el("div", "temps");
    const dual = Array.isArray(p.nozzles) && p.nozzles.length > 1;
    if (dual) {
      // dual-nozzle printers (H2D, H2C): both nozzles, the active one marked with a word, not colour alone
      row.classList.add("dual");
      for (const n of p.nozzles) {
        const tile = tempTile("nozzle", `${n.name} nozzle`, n);
        if (n.active) {
          tile.classList.add("on");
          const tag = el("span", "act");
          tag.append(icon("check", "bico"), "active");
          tile.append(tag);
        }
        row.append(tile);
      }
      row.append(tempTile("bed", "Bed", t.bed));
    } else {
      row.append(tempTile("nozzle", "Nozzle", t.nozzle), tempTile("bed", "Bed", t.bed));
    }
    if (t.chamber && t.chamber.cur != null) row.append(tempTile("box", "Chamber", t.chamber));
    return row;
  }
  function progress(pct, label) {
    const bar = el("div", "progress"), fill = el("span");
    const v = Math.max(0, Math.min(100, pct || 0));
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-valuenow", String(v));
    bar.setAttribute("aria-label", label || "Print progress");
    fill.style.width = v + "%";
    bar.append(fill);
    return bar;
  }
  function stat(value, label) {
    const d = el("div");
    d.append(el("span", "sv num", value), el("span", "sl", label));
    return d;
  }
  function trayDot(tray) {
    const dot = el("i", "sw");
    if (tray.color) dot.style.background = "#" + tray.color;
    else dot.style.background = "transparent";
    return dot;
  }
  function infoRow(p) {
    const row = el("div", "inforow");
    if (p.speed) {
      const s = el("span");
      s.append(icon("speed"), el("span", "num", `${p.speed.name}${p.speed.percent ? ` ${p.speed.percent}%` : ""}`));
      s.title = "Speed profile";
      row.append(s);
    }
    const fans = p.fans || {};
    const fanText = ["part", "aux", "chamber", "heatbreak"].filter(k => fans[k] != null).map(k => `${k} ${fans[k]}%`).join(" · ");
    if (fanText) {
      const s = el("span");
      s.append(icon("fan"), el("span", "num", fanText));
      s.title = "Fans";
      row.append(s);
    }
    if (p.tray) {
      const s = el("span");
      s.append(trayDot(p.tray), [p.tray.label, p.tray.name || p.tray.type].filter(Boolean).join(" · "));
      s.title = "Active filament";
      row.append(s);
    }
    return row.childElementCount ? row : null;
  }
  function spark(values, title) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "spark");
    svg.setAttribute("viewBox", "0 0 100 24");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    const label = title + (values.length ? `: ${fmt(Math.min(...values))}–${fmt(Math.max(...values))}` : ": no readings yet");
    svg.setAttribute("aria-label", label);
    const t = document.createElementNS(NS, "title");
    t.textContent = label;
    svg.append(t);
    if (values.length > 1) {
      const lo = Math.min(...values), hi = Math.max(...values), span = (hi - lo) || 1;
      const pts = values.map((v, i) => `${(i / (values.length - 1) * 100).toFixed(1)},${(22 - (v - lo) / span * 20).toFixed(1)}`);
      const line = document.createElementNS(NS, "polyline");
      line.setAttribute("points", pts.join(" "));
      line.setAttribute("vector-effect", "non-scaling-stroke");
      svg.append(line);
    }
    return svg;
  }
  function amsName(id) {
    const n = Number(id);
    return Number.isInteger(n) && n >= 0 && n < 4 ? "AMS " + "ABCD"[n] : "AMS " + id;
  }
  function amsList(p) {
    const ams = p.ams || [];
    if (!ams.length) return null;
    const list = el("div", "amslist");
    for (const u of ams) {
      const row = el("div", "amsrow");
      row.append(el("span", "amsname", amsName(u.id)));
      const hum = u.humidity_pct != null ? `${fmt(u.humidity_pct)}%` : (u.humidity_level != null ? `level ${u.humidity_level}` : "–");
      const h = el("span", "num");
      h.append(icon("drop"), el("span", "sr", "Humidity "), hum);
      const t = el("span", "num");
      t.append(icon("thermo"), el("span", "sr", "Temperature "), u.temp != null ? `${fmt(u.temp)}°C` : "–");
      const hist = ((p.ams_history || {})[u.id] || []);
      const useLevel = u.humidity_pct == null;
      row.append(h, t, spark(hist.map(r => useLevel ? r[2] : r[1]).filter(v => v != null),
                             `Humidity, last 24 hours (${useLevel ? "level" : "%"})`));
      list.append(row);
    }
    return list;
  }
  function hmsList(p) {
    const lines = [];
    if (p.gcode_state === "FAILED" && (p.error_text || p.error)) {
      lines.push([p.error_text || "Print error", p.error, null]);
    }
    for (const h of (p.hms || [])) lines.push([h.text || h.module || "Printer message", h.code, h.url]);
    if (!lines.length) return null;
    const box = el("div", "hms");
    box.setAttribute("role", "status");
    for (const [text, code, url] of lines) {
      const row = el("div"), body = el("span");
      body.append(text);
      if (code) {
        body.append(" · ");
        if (url) {
          const a = el("a", "link num", code);
          a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
          body.append(a);
        } else body.append(el("span", "num", code));
      }
      row.append(icon("alert"), body);
      box.append(row);
    }
    return box;
  }

  // ---------- camera / image box of an active printer (persists between polls) ----------
  function mediaBox(printerId, hasCamera) {
    const box = el("div", "cam"), img = el("img"), tag = el("span", "livetag");
    const ph = el("div", "noimg");
    ph.append(icon("image"), el("span", null, "No image"));
    const ctl = el("div", "camctl"), msg = el("div", "cammsg");
    msg.hidden = true;
    msg.setAttribute("role", "status");
    img.alt = "";
    img.hidden = true;
    tag.hidden = true;
    box.append(ph, img, tag, ctl, msg);
    let want = null, timer = null, fallback = null, video = false;
    const vbtn = el("button", "btn small glass");
    vbtn.type = "button";
    vbtn.setAttribute("aria-pressed", "false");
    vbtn.append(icon("video"), el("span", null, "Live video"));
    const tab = el("a", "btn small glass icon");
    tab.href = `/printers/${printerId}/camera.mjpg`;
    tab.target = "_blank";
    tab.rel = "noopener";
    tab.title = "Open the live video in a new tab";
    tab.setAttribute("aria-label", "Open the live video in a new tab");
    tab.append(icon("external"));
    if (hasCamera) ctl.append(vbtn, tab);

    function setTag(kind) {
      tag.hidden = !(kind === "snap" || kind === "video");
      tag.replaceChildren(icon(kind === "video" ? "video" : "camera", "bico"), kind === "video" ? "Live video" : "Live");
    }
    function show(url, alt) {
      img.alt = alt;
      img.hidden = false;
      ph.hidden = true;
      img.src = url;
    }
    img.addEventListener("error", () => {
      if (!img.getAttribute("src")) return;
      if (video) {
        stopVideo();
        msg.textContent = "No live video. Is LAN live view enabled on the printer?";
        msg.hidden = false;
        return;
      }
      if (want && want.kind === "snap" && fallback && img.src.indexOf(fallback) === -1) {
        tag.hidden = true;
        show(fallback, "");
      } else {
        img.hidden = true;
        ph.hidden = false;
        tag.hidden = true;
      }
    });
    img.addEventListener("load", () => { if (video) msg.hidden = true; });

    function apply() {
      clearInterval(timer);
      if (video) return;
      setTag(want && want.kind);
      if (!want) { img.removeAttribute("src"); img.hidden = true; ph.hidden = false; return; }
      if (want.kind === "snap") {
        show(snapUrl(want.url), "Camera image of the printer");
        timer = setInterval(() => {
          if (document.hidden || video) return;
          const next = new Image();   // load first, then swap: no flicker
          next.onload = () => { if (!video) img.src = next.src; };
          next.src = snapUrl(want.url);
        }, SNAP_MS);
      } else {
        show(want.url, "Cover of the print");
      }
    }
    function startVideo() {
      video = true;
      clearInterval(timer);
      vbtn.setAttribute("aria-pressed", "true");
      setTag("video");
      msg.textContent = "Loading live video…";
      msg.hidden = false;
      show(`/printers/${printerId}/camera.mjpg?t=${Date.now()}`, "Live video of the printer");
    }
    function stopVideo() {
      video = false;
      vbtn.setAttribute("aria-pressed", "false");
      msg.hidden = true;
      img.removeAttribute("src");
      apply();
    }
    vbtn.addEventListener("click", () => { video ? stopVideo() : startVideo(); });
    // no stream to a tab nobody is looking at
    let resume = false;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && video) { resume = true; stopVideo(); }
      else if (!document.hidden && resume) { resume = false; startVideo(); }
    });
    return {
      el: box,
      set(kind, url, fb) {   // kind: "snap" (url = printer id), "img" or null
        fallback = fb || null;
        const key = kind && `${kind}:${url}`;
        if ((want && want.key) === key) return;
        want = kind ? {kind, url, key} : null;
        apply();
      },
      stop() { clearInterval(timer); video = false; img.removeAttribute("src"); },
    };
  }
  function chooseMedia(p, media) {
    const active = isActive(p);
    if (active && p.camera && !p.own_cover) media.set("snap", p.printer_id, p.cover || p.printer_photo);
    else if (p.cover) media.set("img", p.cover);
    else if (active && p.camera) media.set("snap", p.printer_id, p.printer_photo);
    else if (p.printer_photo) media.set("img", p.printer_photo);
    else media.set(null);
  }

  // ---------- hero ----------
  function overlay(p) {
    const st = PV.state(p);
    const ov = el("div", "overlay");
    const head = el("div", "hhead"), pr = el("span", "printer");
    pr.append(icon("printer"), p.printer || "Printer");
    head.append(pr, PV.badge(st));
    const h = el("h1", "htitle");
    if (p.print_id) {
      const a = el("a", null, p.name || "Print");
      a.href = "/print/" + p.print_id;
      h.append(a);
    } else h.textContent = p.name || "Print";
    ov.append(head, h);
    const sub = [];
    if (p.plate_cnt > 1 && p.plate_idx) sub.push(`plate ${p.plate_idx} of ${p.plate_cnt}`);
    if (p.gcode_state === "PAUSE") sub.push("paused");
    if (sub.length) ov.append(el("p", "hsub", sub.join(" · ")));
    ov.append(progress(p.percent));
    const stats = el("div", "stats");
    stats.append(stat(`${p.percent ?? "–"}%`, "done"),
                 stat(`${p.layer ?? "–"}/${p.total_layers ?? "–"}`, "layer"),
                 stat(dur(p.remaining_min), "left"),
                 stat(PV.endClock(p.remaining_min) || "–", "done at"));
    ov.append(stats);
    return ov;
  }
  function statsBlock(p, withName) {
    const box = el("div", "hstats");
    if (withName) {
      const h = el("h3");
      h.append(icon("printer", "bico"), p.printer);
      box.append(h);
    }
    for (const part of [temps(p), infoRow(p), hmsList(p), amsList(p)]) if (part) box.append(part);
    return box;
  }
  function idleCard(p, item) {
    const card = el("article", "hidle");
    card.setAttribute("aria-label", p.printer || "Printer");
    const pic = item.pic || el("div", "pic");
    const url = p.printer_photo || null;
    if (pic.dataset.url !== (url || "")) {
      pic.replaceChildren(url ? Object.assign(el("img"), {src: url, alt: ""}) : icon("printer"));
      pic.dataset.url = url || "";
    }
    item.pic = pic;
    const who = el("div", "who");
    who.append(el("strong", null, p.printer || "Printer"), PV.badge(PV.state(p)));
    if ((p.gcode_state === "FINISH" || p.gcode_state === "FAILED") && p.name) {
      const last = el("span", "meta");
      if (p.print_id) {
        const a = el("a", "link", p.name);
        a.href = "/print/" + p.print_id;
        last.append("Last print: ", a);
      } else last.append(`Last print: ${p.name}`);
      who.append(last);
    }
    card.append(pic, who);
    const t = temps(p);
    if (t) card.append(t);
    for (const part of [hmsList(p), amsList(p)]) if (part) card.append(part);
    return card;
  }

  const items = new Map();   // printer id → {kind, root, media, slot}
  function renderHero(data) {
    const printers = data.printers || [];
    hero.hidden = !printers.length;
    const seen = new Set();
    const activeOnes = printers.filter(isActive);
    for (const p of printers) {
      const kind = isActive(p) ? "active" : "idle";
      let item = items.get(p.printer_id);
      if (item && item.kind !== kind) {
        if (item.media) item.media.stop();
        item.root.remove();
        item = null;
      }
      if (!item) {
        item = {kind};
        if (kind === "active") {
          item.root = el("article", "hmain");
          item.media = mediaBox(p.printer_id, p.camera);
          item.root.append(item.media.el);
        }
        items.set(p.printer_id, item);
      }
      if (kind === "active") {
        item.root.setAttribute("aria-label", `${p.printer}: ${p.name || "print"}`);
        chooseMedia(p, item.media);
        const ov = overlay(p);
        if (item.slot) item.slot.replaceWith(ov); else item.root.append(ov);
        item.slot = ov;
      } else {
        const card = idleCard(p, item);
        if (item.root) item.root.replaceWith(card);
        item.root = card;
      }
      hero.append(item.root);   // follow the order; moving does not break the stream
      seen.add(p.printer_id);
    }
    for (const [id, item] of items) {
      if (!seen.has(id)) {
        if (item.media) item.media.stop();
        item.root.remove();
        items.delete(id);
      }
    }
    if (hstats) {
      hstats.replaceChildren(...activeOnes.map(p => statsBlock(p, activeOnes.length > 1)));
      hstats.hidden = !activeOnes.length;
    }
  }

  // ---------- detail page: live box for this print ----------
  function renderPage(data) {
    const id = Number(pageLive.dataset.print);
    const p = (data.printers || []).find(x => x.print_id === id && isActive(x));
    pageLive.hidden = !p;
    if (!p) return;
    const head = el("div", "hhead"), pr = el("span", "printer");
    pr.append(icon("printer"), p.printer || "Printer");
    head.append(pr, PV.badge(PV.state(p), "flat"));
    const parts = [head, progress(p.percent)];
    const line = [`${p.percent ?? "–"}%`, `layer ${p.layer ?? "–"}/${p.total_layers ?? "–"}`, `${dur(p.remaining_min)} left`];
    const clock = PV.endClock(p.remaining_min);
    if (clock) line.push(`done at ${clock}`);
    parts.push(el("p", "lb num", line.join(" · ")));
    for (const part of [temps(p), infoRow(p), hmsList(p)]) if (part) parts.push(part);
    pageLive.replaceChildren(...parts);
  }

  async function poll() {
    try {
      const data = await PV.fetchLive();
      if (data.available) {
        activePrints = new Set((data.printers || []).filter(p => p.print_id && isActive(p)).map(p => p.print_id));
      }
      PV.sideStatus(data);
      if (hero) renderHero(data);
      if (pageLive) renderPage(data);
    } catch (e) { /* quiet: try again later */ }
    setTimeout(poll, document.hidden ? 30000 : (pageLive && !hero ? 10000 : 5000));
  }
  poll();
})();
