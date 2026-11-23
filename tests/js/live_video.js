/* Runs static/app.js and static/live.js in Node with a minimal DOM, and checks the live video button.
   Started by tests/test_fixes.py. */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");

class Node_ {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {}; this.style = {};
    this.hidden = false; this.parent = null;
    this.classList = {add() {}, remove() {}};
  }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || new Set()).add(f); }
  removeEventListener(t, f) { if (this.listeners[t]) this.listeners[t].delete(f); }
  fire(t) { for (const f of [...(this.listeners[t] || [])]) f({}); }
  append(...c) { for (const x of c) if (x instanceof Node_) { x.remove(); x.parent = this; this.children.push(x); } }
  replaceChildren(...c) { this.children = []; this.append(...c); }
  replaceWith(n) { const p = this.parent; if (!p) return; n.remove(); n.parent = p; p.children[p.children.indexOf(this)] = n; this.parent = null; }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return k === "src" ? this.src : this.attrs[k]; }
  removeAttribute(k) { if (k === "src") this.src = undefined; delete this.attrs[k]; }
  get childElementCount() { return this.children.length; }
  querySelector() { return new Node_("x"); }
  all(tag) { return this.children.flatMap(c => [...(c.tag === tag ? [c] : []), ...c.all(tag)]); }
}
const hero = new Node_("section"), doc = new Node_("document");
Object.assign(doc, {
  hidden: false, createElement: t => new Node_(t), createElementNS: (_, t) => new Node_(t),
  getElementById: id => id === "live" ? hero : null, querySelectorAll: () => [],
});
let data, tick;
const printer = state => ({printer_id: 1, printer: "P", connected: true, gcode_state: state, camera: true, name: "x", ams: []});
const context = {
  document: doc, Image: class {}, navigator: {userAgent: ""}, console,
  fetch: async () => ({ok: true, json: async () => data}),
  setInterval: () => 1, clearInterval() {}, setTimeout: f => { tick = f; },
};
context.window = context;
vm.createContext(context);
const root = path.join(__dirname, "..", "..", "static");
const load = name => vm.runInContext(fs.readFileSync(path.join(root, name), "utf8"), context);
const settle = () => new Promise(r => setImmediate(r));
const poll = async state => { data = {available: true, printers: [printer(state)]}; if (tick) tick(); await settle(); await settle(); };
const visible = v => { doc.hidden = !v; doc.fire("visibilitychange"); };
const streaming = img => String(img.src || "").includes("camera.mjpg");

(async () => {
  load("app.js");
  data = {available: true, printers: [printer("RUNNING")]};
  load("live.js");
  await settle(); await settle();
  const img = hero.all("img")[0], button = hero.all("button")[0];
  button.fire("click");
  assert(streaming(img), "live video starts");
  visible(false);
  assert(!streaming(img), "no stream in a hidden tab");
  visible(true);
  assert(streaming(img), "resumes when the tab is visible again");

  visible(false);
  await poll("FINISH");          // the print ends while the tab is hidden: the box disappears
  visible(true);
  assert(!streaming(img), "a removed box must not start streaming again");
  assert.strictEqual((doc.listeners.visibilitychange || new Set()).size, 0, "listeners are cleaned up");

  await poll("RUNNING");         // a new print gives a new, working box
  const img2 = hero.all("img")[0];
  assert.notStrictEqual(img2, img);
  hero.all("button")[0].fire("click");
  assert(streaming(img2));
})().catch(e => { console.error(e.message); process.exit(1); });
