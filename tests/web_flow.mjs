import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
function input(value = "") {
  let text = String(value);
  return { get value() { return text; }, set value(next) { text = String(next); } };
}
function element() {
  return { children: [], style: { setProperty() {} }, append(...items) { this.children.push(...items); },
    appendChild(item) { this.children.push(item); }, replaceChildren() { this.children = []; }, addEventListener() {} };
}

test("an address result moves the existing pin and keeps its label", () => {
  const elements = new Map();
  const address = input("Typed place");
  const latitude = input(1), longitude = input(2);
  let removed = false, markerPoint;
  const bounds = { extend() {}, pad() { return this; } };
  const layer = () => ({ addTo() { return this; }, on() {}, getBounds() { return bounds; } });
  const context = vm.createContext({
    address, latitude, longitude,
    state: { layers: [], startMarker: { remove() { removed = true; } } },
    map: { panTo() {}, fitBounds() {} }, routeColors: ["red"],
    L: { divIcon() {}, marker(point) { markerPoint = point; return layer(); }, polyline: layer, latLngBounds() { return bounds; } },
    document: { createElement: element, getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); } },
    clearRoutes() {}, selectRoute() {}, routeMetrics(route) { return route.feature.properties; },
  });
  const start = source.slice(source.indexOf("function setStart("), source.indexOf('\nmap.on("click"'));
  const render = source.slice(source.indexOf("function renderRoutes("), source.indexOf("\n// FastAPI sends"));
  vm.runInContext(start + "\n" + render, context);
  context.renderRoutes({ origin: { latitude: 10, longitude: 20 }, routes: [{ feature: {
    geometry: { coordinates: [[20, 10], [20.01, 10.01], [20, 10]] },
    properties: { distance_miles: 3, elevation_coverage: 1, elevation_gain_feet: 100, traffic_signal_events: 0, trail_fraction: 0.2 },
  } }] });
  assert.equal(removed, true);
  assert.equal(JSON.stringify(markerPoint), "[10,20]");
  assert.equal(latitude.value, "10");
  assert.equal(longitude.value, "20");
  assert.equal(address.value, "Typed place");
});

function submission() {
  let handler, respond;
  const response = new Promise(resolve => { respond = resolve; });
  const address = input("Typed place"), latitude = input(), longitude = input();
  const rendered = [], statuses = [];
  const generateButton = { disabled: false, setAttribute() {}, removeAttribute() {}, querySelector() { return {}; } };
  const context = vm.createContext({ address, latitude, longitude, generateButton,
    distanceInput: input(5), state: { surface: "mixed", elevation: "balanced", drive: 0 },
    form: { addEventListener(_event, callback) { handler = callback; } },
    fetch() { return response; }, renderRoutes(payload) { rendered.push(payload); },
    showStatus(message) { statuses.push(message); }, errorMessage() { return "error"; },
  });
  const code = source.slice(source.indexOf('form.addEventListener("submit"'), source.indexOf('\ndocument.getElementById("download-gpx")'));
  vm.runInContext(code, context);
  return { address, latitude, longitude, generateButton, rendered, statuses,
    submit() { return handler({ preventDefault() {} }); },
    respond() { respond({ ok: true, json: async () => ({ origin: { latitude: 10, longitude: 20 }, warnings: [] }) }); },
  };
}

test("a current address response still renders and unlocks the form", async () => {
  const form = submission();
  const pending = form.submit();
  form.respond(); await pending;
  assert.equal(form.rendered.length, 1);
  assert.equal(form.generateButton.disabled, false);
});

test("a late address response cannot replace a newly selected start", async () => {
  const form = submission();
  const pending = form.submit();
  form.address.value = "New pin";
  form.latitude.value = 11; form.longitude.value = 21;
  form.respond(); await pending;
  assert.equal(form.rendered.length, 0);
  assert.equal(form.address.value, "New pin");
  assert.equal(form.latitude.value, "11");
  assert.equal(form.longitude.value, "21");
  assert.equal(form.generateButton.disabled, false);
  assert.match(form.statuses.at(-1), /Start changed/);
});
