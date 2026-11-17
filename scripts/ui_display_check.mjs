// Feed real production responses through the real web/app.js and compare
// every number the route cards and detail panel display with the route JSON.
//
//   node scripts/ui_display_check.mjs output/deployed-validation [--source path/to/app.js]
//
// Reads captures/<phase>/*.json and places.json written by
// scripts/validate_deployed.py and writes ui-check.json beside them. The DOM is
// a small shim in the style of tests/web_flow.mjs; this is not a visual
// browser test. It loads the deployed app.js copy when one was fetched.
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const out = process.argv[2] || "output/deployed-validation";
const sourceArg = process.argv.indexOf("--source");
const repoSource = new URL("../web/app.js", import.meta.url);
const deployedPath = path.join(out, "deployed-app.js");
const sourcePath = sourceArg > 0 ? process.argv[sourceArg + 1] : (fs.existsSync(deployedPath) ? deployedPath : repoSource.pathname);
const source = fs.readFileSync(sourcePath, "utf8");
const places = fs.existsSync(path.join(out, "places.json")) ? JSON.parse(fs.readFileSync(path.join(out, "places.json"), "utf8")) : [];

function matches(element, selector) {
  if (selector.startsWith(".")) return element.classList.contains(selector.slice(1));
  if (selector === "[data-distance]") return element.dataset.distance !== undefined;
  if (selector === "li[role='option']") return element.tagName === "LI" && element.getAttribute("role") === "option";
  if (selector === "input, button") return ["INPUT", "BUTTON"].includes(element.tagName);
  return element.tagName.toLowerCase() === selector;
}

function element(tagName = "div") {
  const node = {
    tagName: tagName.toUpperCase(), children: [], attributes: {}, dataset: {}, listeners: new Map(),
    className: "", textContent: "", hidden: false, disabled: false, value: "",
    style: { values: {}, setProperty(name, value) { this.values[name] = value; } },
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    removeAttribute(name) { delete this.attributes[name]; },
    addEventListener(type, callback) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(callback);
    },
    async fire(type, event = {}) {
      for (const callback of this.listeners.get(type) || []) await callback({ target: this, preventDefault() {}, ...event });
    },
    click() { return this.fire("click"); },
    closest(selector) { return matches(this, selector) ? this : null; },
    querySelectorAll(selector) {
      return this.children.flatMap(child => typeof child === "object"
        ? [...(matches(child, selector) ? [child] : []), ...child.querySelectorAll(selector)] : []);
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    scrollIntoView() {}, focus() {}, remove() { this.removed = true; },
  };
  node.classList = {
    contains(name) { return node.className.split(/\s+/).includes(name); },
    add(...names) { node.className = [...new Set([...node.className.split(/\s+/).filter(Boolean), ...names])].join(" "); },
    remove(...names) { node.className = node.className.split(/\s+/).filter(name => !names.includes(name)).join(" "); },
    toggle(name, force) { const on = force ?? !this.contains(name); if (on) this.add(name); else this.remove(name); return on; },
  };
  return node;
}

function haversine([lat1, lon1], [lat2, lon2]) {
  const r = Math.PI / 180, a = Math.sin((lat2 - lat1) * r / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin((lon2 - lon1) * r / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.sqrt(a));
}

function app() {
  const ids = new Map();
  const get = id => { if (!ids.has(id)) ids.set(id, element()); return ids.get(id); };
  get("address").value = "Start"; get("latitude").value = "1"; get("longitude").value = "2"; get("distance").value = "5";
  const generate = element("button"); generate.className = "generate"; generate.append(element("span"));
  get("route-form").append(generate);
  const groups = ["surface", "elevation", "drive"].map(control => {
    const group = element(); group.className = "segmented"; group.dataset.control = control;
    const values = { surface: ["road", "mixed", "trail"], elevation: ["flat", "balanced", "hilly"], drive: ["0", "1", "3", "5"] }[control];
    for (const value of values) { const button = element("button"); button.dataset.value = value; group.append(button); }
    return group;
  });
  const distances = [3, 5, 8, 12].map(d => { const b = element("button"); b.dataset.distance = String(d); return b; });
  const body = element("body"), mapEvents = new Map(), requests = [], fetches = [];
  const bounds = () => ({ points: [], extend(v) { this.points.push(v); return this; }, pad() { return this; } });
  const map = { setView() { return this; }, on(t, cb) { mapEvents.set(t, cb); }, panTo() {}, fitBounds() {}, invalidateSize() {},
    distance(a, b) { return haversine(a.map(Number), b.map(Number)); } };
  const layer = (points = []) => ({ points, addTo() { return this; }, on() { return this; }, setStyle() { return this; },
    getLatLngs() { return this.points; }, getBounds() { return bounds().extend(this.points); }, bringToFront() {}, remove() {} });
  const context = vm.createContext({
    console, Blob, setTimeout, clearTimeout, requestAnimationFrame(cb) { cb(); },
    window: { matchMedia() { return { matches: false }; }, addEventListener() {} },
    matchMedia() { return { matches: false }; }, navigator: {},
    document: { body, documentElement: element("html"), getElementById: get, createElement: tag => element(tag),
      createElementNS: (_ns, tag) => element(tag),
      querySelectorAll(selector) {
        if (selector === ".segmented") return groups;
        if (selector === "[data-distance]") return distances;
        return [...ids.values()].flatMap(node => node.querySelectorAll(selector));
      },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; } },
    L: { map() { return map; }, control: { zoom() { return { addTo() {} }; } }, tileLayer() { return { addTo() {} }; },
      divIcon() {}, latLngBounds: bounds, polyline: layer, marker: point => layer(point) },
    fetch(url, options) {
      fetches.push({ url, options });
      if (url === "/api/routes") return new Promise(resolve => requests.push({ options, resolve }));
      const data = url === "/api/places" ? { places } : url === "/api/capabilities" ? { modes: {} } : { areas: [] };
      return Promise.resolve({ ok: true, json: async () => data });
    },
    URL: { createObjectURL() { return "blob:x"; }, revokeObjectURL() {} },
  });
  vm.runInContext(source, context);
  return { get, groups, requests, fetches, mapEvents, context,
    async choose(control, value) {
      const group = groups.find(g => g.dataset.control === control);
      await group.fire("click", { target: group.children.find(b => b.dataset.value === String(value)) });
    } };
}

const tick = ms => new Promise(resolve => setTimeout(resolve, ms));
const textOf = node => typeof node !== "object" ? String(node) : [node.textContent, ...node.children.map(textOf)].filter(Boolean).join(" ");

// Every ranking component src/runline/scoring.py adds into a route's score.
const SCORER_COMPONENTS = ["distance", "traffic_signals", "major_crossings", "surface", "elevation", "repeated_segments", "shape", "driving"];
const display = { responses: 0, cards: 0, compared: 0, mismatches: [], fields: {} };
const drive = { cases: 0, alt_cards: 0, start_label_ok: 0, context_ok: 0, drive_cost_hits: 0, score_hits: 0 };
const allText = [];

function check(where, field, shown, expected, ok) {
  display.compared += 1;
  display.fields[field] = (display.fields[field] || 0) + 1;
  if (!ok) display.mismatches.push({ where, field, shown, expected });
}

async function render(capture, name) {
  const ui = app(), request = capture.request, payload = capture.response;
  ui.get("address").value = request.address || "Pinned location";
  ui.get("latitude").value = request.latitude === undefined ? "" : String(request.latitude);
  ui.get("longitude").value = request.longitude === undefined ? "" : String(request.longitude);
  ui.get("distance").value = String(request.distance_miles);
  if (request.drive_radius_miles) await ui.choose("drive", request.drive_radius_miles);
  const pending = ui.get("route-form").fire("submit");
  const sent = JSON.parse(ui.requests.at(-1).options.body);
  ui.requests.at(-1).resolve({ ok: true, status: 200, json: async () => payload });
  await pending;
  const state = vm.runInContext("state", ui.context);
  const cards = ui.get("route-list").children;
  display.responses += 1;
  check(name, "card count", cards.length, payload.routes.length, cards.length === payload.routes.length && state.routes.length === payload.routes.length);
  check(name, "request body distance", sent.distance_miles, request.distance_miles, sent.distance_miles === request.distance_miles && sent.drive_radius_miles === (request.drive_radius_miles || 0));
  const header = ui.get("request-summary").textContent;
  check(name, "header target", header, `${request.distance_miles} mi`, header.startsWith(`${request.distance_miles} mi`));
  const context = ui.get("result-context").textContent;
  const expectedContext = payload.discovered_starts ? `${payload.discovered_starts} nearby starts checked` : "Starting here";
  check(name, "result context", context, expectedContext, context === expectedContext);
  if (request.drive_radius_miles) { drive.cases += 1; if (context === expectedContext) drive.context_ok += 1; }
  allText.push(header, context, ui.get("route-count").textContent, ui.get("form-status").textContent);
  for (const [index, card] of cards.entries()) {
    display.cards += 1;
    const m = payload.routes[index].feature.properties, where = `${name} card ${index + 1}`;
    const [, main, distanceNode, detail] = card.children;
    const title = main.children[1].textContent;
    const distanceText = distanceNode.children[0].textContent;
    check(where, "distance (card)", distanceText, m.distance_miles, distanceText === m.distance_miles.toFixed(2) && Math.abs(Number(distanceText) - m.distance_miles) <= 0.005 + 1e-12);
    const metricMap = Object.fromEntries(detail.children.map(item => [item.children[1].textContent, item.children[0].textContent]));
    check(where, "signals (card)", metricMap.signals, m.traffic_signal_events, Number(metricMap.signals) === m.traffic_signal_events);
    const climb = metricMap.climbing;
    check(where, "climbing (card)", climb, m.elevation_gain_feet,
      m.elevation_coverage >= 0.95 ? climb === `${Math.round(m.elevation_gain_feet)} ft` && Math.abs(parseFloat(climb) - m.elevation_gain_feet) <= 0.5 : climb === "—");
    check(where, "trail % (card)", metricMap.trail, m.trail_fraction, metricMap.trail === `${Math.round(m.trail_fraction * 100)}%` && Math.abs(parseFloat(metricMap.trail) / 100 - m.trail_fraction) <= 0.005 + 1e-12);
    const label = m.start?.label || "Starting here";
    check(where, "start label (card)", title, label, title === label);
    const aria = card.getAttribute("aria-label");
    check(where, "aria-label", aria, m, aria === `Route ${index + 1}, ${m.distance_miles.toFixed(2)} miles, ${m.traffic_signal_events} signals, ${Math.round(m.trail_fraction * 100)} percent trail`);
    await card.fire("click");
    const summary = { distance: ui.get("summary-distance").textContent, gain: ui.get("summary-gain").textContent,
      signals: ui.get("summary-signals").textContent, start: ui.get("summary-start").textContent };
    check(where, "distance (panel)", summary.distance, m.distance_miles, summary.distance === `${m.distance_miles.toFixed(2)} mi`);
    check(where, "elevation gain (panel)", summary.gain, m.elevation_gain_feet, summary.gain === (m.elevation_coverage >= 0.95 ? `${Math.round(m.elevation_gain_feet)} ft` : "—"));
    check(where, "signals (panel)", summary.signals, m.traffic_signal_events, Number(summary.signals) === m.traffic_signal_events);
    check(where, "start label (panel)", summary.start, m.start?.label || "Start here", summary.start === (m.start?.label || "Start here"));
    const shown = [textOf(card), aria, ...Object.values(summary)].join(" | ");
    allText.push(shown);
    if (request.drive_radius_miles && m.start && m.start.kind !== "origin") {
      drive.alt_cards += 1;
      if (title === m.start.label && summary.start === m.start.label) drive.start_label_ok += 1;
      const d = m.start.drive_distance_miles;
      if ([d.toFixed(1), d.toFixed(2)].some(v => shown.includes(`${v} mi`)) || /drive/i.test(shown)) drive.drive_cost_hits += 1;
      if (/score/i.test(shown) || shown.includes(m.score.toFixed(2))) drive.score_hits += 1;
    }
  }
}

const captureDirs = ["cville", "metros", "drive", "us8"].map(dir => path.join(out, "captures", dir)).filter(fs.existsSync);
for (const dir of captureDirs) {
  for (const file of fs.readdirSync(dir).filter(f => f.endsWith(".json")).sort()) {
    const capture = JSON.parse(fs.readFileSync(path.join(dir, file), "utf8"));
    if (capture.status === 200 && capture.response?.routes?.length) await render(capture, `${path.basename(dir)}/${file}`);
  }
}

// What the page can show at all: rendered card/panel/header text plus the
// deployed static HTML (labels, tooltips, hidden elements).
const html = fs.existsSync(path.join(out, "index.html")) ? fs.readFileSync(path.join(out, "index.html"), "utf8") : "";
const rendered = allText.join(" \n ");
const searchable = `${rendered}\n${html.replace(/<[^>]+(title|aria-label)="([^"]*)"[^>]*>/g, " $2 ").replace(/<[^>]*>/g, " ")}`;
const inventory = {
  "distance error / off-target": /(off target|distance error|±|\+\/-|miles? (short|long|over|under))/i,
  "major-road crossings": /crossing/i,
  "repeated fraction": /repeat/i,
  "shape / compactness": /(shape|compact|\broundness\b)/i,
  "route score or score breakdown": /score/i,
  "drive distance / driving cost": /mi drive|drive distance|driving/i,
};
const notShown = Object.entries(inventory).filter(([, re]) => !re.test(searchable)).map(([k]) => k);
const shownAnywhere = Object.entries(inventory).filter(([, re]) => re.test(searchable)).map(([k, re]) => `${k} (matched "${searchable.match(re)[0]}")`);
const sourceUsesCode = Object.fromEntries(["major_crossing_events", "repeated_fraction", "compactness", "score", "score_components", "drive_distance_miles"]
  .map(key => [key, new RegExp(`metrics\\.${key}\\b|\\.${key}\\b`).test(source.replace(/validateRoutes[\s\S]*?\n}\n/, ""))]));
display.shown = ["distance (2 dp, card + panel)", "traffic signals (card + panel)", "climbing ft (card + panel, hidden if elevation coverage < 95%)",
  "trail % (card)", "start label (card + panel)", "target distance in header", "nearby starts checked (header)"];
display.not_shown = notShown;
display.matched_in_page = shownAnywhere;
display.metric_fields_read_outside_validation = sourceUsesCode;
display.scorer_components = SCORER_COMPONENTS;
display.score_parts_shown = !notShown.includes("route score or score breakdown");
display.component_display = {
  distance: "partly: actual distance and target are shown, the error itself is not",
  traffic_signals: "shown as a count",
  major_crossings: notShown.includes("major-road crossings") ? "not shown" : "shown",
  surface: "partly: trail % on the card",
  elevation: "partly: climbing ft",
  repeated_segments: notShown.includes("repeated fraction") ? "not shown" : "shown",
  shape: notShown.includes("shape / compactness") ? "not shown" : "shown",
  driving: notShown.includes("drive distance / driving cost") ? "not shown (only the start label)" : "shown",
};

// US8: autocomplete from the shipped catalog and map clicks, through app.js.
const autocomplete = [];
for (const [query, expected] of [["lawn", "The Lawn"], ["mcintire", "McIntire Park"], ["court sq", "Court Square Park"], ["pen pa", "Pen Park"], ["ix art", "IX Art Park"]]) {
  const ui = app();
  await tick(5); // loadPlaces()
  ui.get("address").value = query;
  await ui.get("address").fire("input");
  const items = ui.get("address-suggestions").children;
  const names = items.map(item => item.children[0].textContent);
  const index = names.indexOf(expected);
  const place = places.find(p => p.name === expected && p.kind !== "street");
  const record = { query, expected, listed: index >= 0, suggestions: names.length, pin_set: false, warm_sent: false, body_coordinates: false };
  if (index >= 0) {
    await items[index].fire("mousedown");
    record.pin_set = Number(ui.get("latitude").value) === place.latitude && Number(ui.get("longitude").value) === place.longitude && ui.get("address").value === expected;
    await tick(450);
    record.warm_sent = ui.fetches.some(f => f.url === "/api/warm" && JSON.parse(f.options.body).latitude === place.latitude);
    ui.get("route-form").fire("submit");
    const sent = JSON.parse(ui.requests.at(-1).options.body);
    record.body_coordinates = sent.latitude === place.latitude && sent.longitude === place.longitude && sent.address === undefined;
  }
  autocomplete.push(record);
}
const clickUi = app();
const lawn = places.find(p => p.name === "The Lawn");
clickUi.mapEvents.get("click")({ latlng: { lat: lawn.latitude + 1.23456e-7, lng: lawn.longitude - 1.23456e-7 } });
clickUi.get("route-form").fire("submit");
const clickBody = JSON.parse(clickUi.requests.at(-1).options.body);
const mapClick = { passed: clickBody.latitude === Number((lawn.latitude + 1.23456e-7).toFixed(6)) &&
  clickBody.longitude === Number((lawn.longitude - 1.23456e-7).toFixed(6)) && clickBody.address === undefined &&
  clickUi.get("address").value === "Pinned location" };

const report = {
  source: path.relative(process.cwd(), sourcePath),
  source_matches_deployed: fs.existsSync(deployedPath) ? fs.readFileSync(deployedPath, "utf8") === fs.readFileSync(repoSource, "utf8") : null,
  display,
  drive: { ...drive, drive_cost_displayed: drive.alt_cards > 0 && drive.drive_cost_hits === drive.alt_cards,
    score_displayed: drive.alt_cards > 0 && drive.score_hits === drive.alt_cards,
    start_label_displayed: drive.alt_cards > 0 && drive.start_label_ok === drive.alt_cards },
  autocomplete, map_click: mapClick,
};
fs.writeFileSync(path.join(out, "ui-check.json"), JSON.stringify(report, null, 1) + "\n");
console.log(`rendered ${display.responses} responses, ${display.cards} cards, ${display.compared} values compared, ${display.mismatches.length} mismatches`);
console.log(`not shown anywhere: ${notShown.join(", ")}`);
console.log(`drive: ${JSON.stringify(report.drive)}`);
console.log(`autocomplete: ${autocomplete.map(a => `${a.query}:${a.listed && a.pin_set && a.body_coordinates}`).join(" ")}; map click: ${mapClick.passed}`);
process.exit(0);
