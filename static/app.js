/* Printvault — shared helpers and the printer status in the sidebar. Plain JS, no build step. */
(function () {
  "use strict";
  const PV = window.PV = {};
  const SVG = "http://www.w3.org/2000/svg";

  PV.el = function (tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  // Icons come from the sprite in base.html (icons.py)
  PV.icon = function (name, cls) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", cls || "ico");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS(SVG, "use");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
  };
  PV.dur = function (min) {
    if (min == null) return "–";
    const h = Math.floor(min / 60), m = min % 60;
    return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
  };
  PV.deg = v => v == null ? "–" : `${Math.round(v)}°`;
  PV.fmt = v => Number(v).toLocaleString("en-GB", {maximumFractionDigits: 1});
  PV.endClock = function (min) {
    if (min == null) return null;
    const end = new Date(Date.now() + min * 60000), now = new Date();
    const hm = end.toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit", hour12: false});
    const days = Math.round((new Date(end).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 864e5);
    return days === 0 ? hm : days === 1 ? `tomorrow ${hm}` : `${end.toLocaleDateString("en-GB", {weekday: "short"})} ${hm}`;
  };
  PV.ACTIVE = ["PREPARE", "RUNNING", "PAUSE"];
  // What a printer is doing, as badge class + icon + word (never colour alone)
  PV.state = function (p) {
    if (!p.connected) return {cls: "st-failed", icon: "wifi-off", text: "Not connected"};
    switch (p.gcode_state) {
      case "RUNNING": return {cls: "st-printing", icon: "play", text: p.phase || "Printing"};
      case "PREPARE": return {cls: "st-printing", icon: "clock", text: p.phase || "Preparing"};
      case "PAUSE": return {cls: "st-paused", icon: "pause", text: p.phase || "Paused"};
      case "FINISH": return {cls: "st-succeeded", icon: "check", text: "Finished"};
      case "FAILED": return {cls: "st-failed", icon: "x", text: "Failed"};
      default: return {cls: "st-idle", icon: "printer", text: p.gcode_state ? "Idle" : "Waiting for data…"};
    }
  };
  PV.badge = function (st, extraCls) {
    const b = PV.el("span", `badge ${st.cls}${extraCls ? " " + extraCls : ""}`);
    b.append(PV.icon(st.icon, "bico"), st.text);
    return b;
  };

  // Sidebar: one line with what the printer(s) are doing
  PV.sideStatus = function (data) {
    const box = document.getElementById("sidestatus");
    if (!box) return;
    const printers = (data && data.printers) || [];
    if (!data || !data.available || !printers.length) { box.hidden = true; return; }
    const dot = box.querySelector(".dot-live"), txt = box.querySelector(".txt");
    const active = printers.filter(p => p.connected && PV.ACTIVE.includes(p.gcode_state));
    const online = printers.filter(p => p.connected);
    dot.className = "dot-live" + (active.length ? " on" : online.length ? "" : " off");
    if (printers.length === 1) {
      const p = printers[0], st = PV.state(p);
      txt.textContent = active.length && p.percent != null ? `${st.text} · ${p.percent}%` : st.text;
      box.title = `${p.printer}: ${txt.textContent}`;
    } else {
      txt.textContent = active.length ? `${active.length} of ${printers.length} printing` : `${online.length} of ${printers.length} online`;
      box.title = txt.textContent;
    }
    box.hidden = false;
  };

  PV.fetchLive = async function () {
    const r = await fetch("/api/live", {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  };

  // Pages without their own live script still show the sidebar status (every 60 s)
  PV.boot = function () {
    if (PV.ownsLive) return;
    async function tick() {
      if (!document.hidden) {
        try { PV.sideStatus(await PV.fetchLive()); } catch (e) { /* quiet */ }
      }
      setTimeout(tick, 60000);
    }
    tick();
  };
})();
