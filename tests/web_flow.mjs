import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");

function matches(element, selector) {
  if (selector.startsWith(".")) return element.classList.contains(selector.slice(1));
  if (selector === "[data-distance]") return element.dataset.distance !== undefined;
  if (selector === "li[role='option']") return element.tagName === "LI" && element.getAttribute("role") === "option";
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
      for (const callback of this.listeners.get(type) || []) {
        await callback({ target: this, preventDefault() {}, ...event });
      }
    },
    click() { this.clicked = true; return this.fire("click"); },
    closest(selector) { return matches(this, selector) ? this : null; },
    querySelectorAll(selector) {
      return this.children.flatMap(child => typeof child === "object"
        ? [...(matches(child, selector) ? [child] : []), ...child.querySelectorAll(selector)] : []);
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    scrollIntoView(options) { this.scrolled = options; },
    focus() { this.focused = true; },
    remove() { this.removed = true; },
  };
  node.classList = {
    contains(name) { return node.className.split(/\s+/).includes(name); },
    add(...names) { node.className = [...new Set([...node.className.split(/\s+/).filter(Boolean), ...names])].join(" "); },
    remove(...names) { node.className = node.className.split(/\s+/).filter(name => !names.includes(name)).join(" "); },
    toggle(name, force) {
      const on = force ?? !this.contains(name);
      if (on) this.add(name); else this.remove(name);
      return on;
    },
  };
  return node;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function route(distance, signals, gain, trail, gpx = "<gpx/>") {
  return { gpx, feature: { type: "Feature", geometry: {
    type: "LineString", coordinates: [[20, 10], [20.01, 10.01], [20.02, 10.01], [20, 10]],
  }, properties: { distance_miles: distance, traffic_signal_events: signals,
    major_crossing_events: 1, elevation_gain_feet: gain, elevation_coverage: 1,
    trail_fraction: trail, repeated_fraction: 0.01, compactness: 0.5 } } };
}

function payload() {
  return { origin: { latitude: 10, longitude: 20 }, source: "pool", warnings: [],
    routes: [route(5.01, 0, 100, 0.2, "<gpx>first</gpx>"), route(4.98, 2, 250, 0.8, "<gpx>second</gpx>")] };
}

function app({ runFactory } = {}) {
  const ids = new Map();
  const get = id => {
    if (!ids.has(id)) ids.set(id, element());
    return ids.get(id);
  };
  get("address").value = "Typed place";
  get("latitude").value = "1"; get("longitude").value = "2";
  get("distance").value = "5";
  const generate = element("button"); generate.className = "generate";
  generate.append(element("span"));
  get("route-form").append(generate);
  const groups = ["surface", "elevation", "drive"].map(control => {
    const group = element(); group.className = "segmented"; group.dataset.control = control;
    const values = { surface: ["road", "mixed", "trail"], elevation: ["flat", "balanced", "hilly"], drive: ["0", "1", "3", "5"] }[control];
    for (const value of values) { const button = element("button"); button.dataset.value = value; group.append(button); }
    return group;
  });
  const distances = [3, 5, 8, 12].map(distance => {
    const button = element("button"); button.dataset.distance = String(distance); return button;
  });
  const body = element("body"), mapEvents = new Map(), createdLayers = [], markers = [], requests = [], blobs = [];
  const bounds = () => ({ points: [], extend(value) { this.points.push(value); return this; }, pad() { return this; } });
  const map = { fits: [], pans: [], setView(point, zoom) { this.home = { point, zoom }; return this; },
    on(type, callback) { mapEvents.set(type, callback); }, panTo(point) { this.pans.push(point); },
    fitBounds(value) { this.fits.push(value); }, invalidateSize() {} };
  function layer(points = []) {
    const instance = { points, styles: [], events: new Map(), removed: false,
      addTo() { return this; }, on(type, callback) { this.events.set(type, callback); return this; },
      setStyle(value) { this.styles.push(value); return this; }, getLatLngs() { return this.points; },
      getBounds() { const value = bounds(); value.extend(this.points); return value; },
      bringToFront() {}, remove() { this.removed = true; },
    };
    createdLayers.push(instance); return instance;
  }
  const context = vm.createContext({
    console, Blob, setTimeout, clearTimeout,
    requestAnimationFrame(callback) { callback(); },
    window: { matchMedia() { return { matches: false }; }, addEventListener() {},
      ...(runFactory ? { RunlineRun: { create: runFactory } } : {}) },
    matchMedia() { return { matches: false }; },
    navigator: {},
    document: { body, documentElement: element("html"), getElementById: get,
      createElement(tag) { return element(tag); }, createElementNS(_namespace, tag) { return element(tag); },
      querySelectorAll(selector) {
        if (selector === ".segmented") return groups;
        if (selector === "[data-distance]") return distances;
        return [...ids.values()].flatMap(node => node.querySelectorAll(selector));
      },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    },
    L: { map() { return map; }, control: { zoom() { return { addTo() {} }; } },
      tileLayer() { return { addTo() {} }; }, divIcon() {}, latLngBounds: bounds,
      polyline: layer, marker(point) { const marker = layer(point); markers.push(marker); return marker; } },
    fetch(url, options) {
      if (url === "/api/routes") { const response = deferred(); requests.push({ options, ...response }); return response.promise; }
      return Promise.resolve({ ok: true, json: async () => url === "/api/places" ? { places: [] } : { areas: ["Synthetic region"] } });
    },
    URL: { createObjectURL(blob) { blobs.push(blob); return "blob:synthetic"; }, revokeObjectURL() {} },
  });
  vm.runInContext(source, context);
  return { get, body, generate, map, markers, requests, blobs,
    state() { return vm.runInContext("state", context); },
    submit() { return get("route-form").fire("submit"); },
    respond(data = payload(), { ok = true, status = 200, jsonError } = {}) {
      requests.at(-1).resolve({ ok, status, json: async () => { if (jsonError) throw jsonError; return data; } });
    },
    async choose(control, value) {
      const group = groups.find(item => item.dataset.control === control);
      const button = group.children.find(item => item.dataset.value === String(value));
      await group.fire("click", { target: button });
    },
    async mode(value) {
      const radio = get(value === "loop" ? "mode-loop" : "mode-point-to-point");
      radio.checked = true;
      await radio.fire("change");
    },
    mapClick(latitude, longitude) { mapEvents.get("click")({ latlng: { lat: latitude, lng: longitude } }); },
  };
}

test("a resolved address moves the original pin and keeps the typed label", async () => {
  const ui = app(), original = ui.markers[0];
  ui.get("latitude").value = ""; ui.get("longitude").value = "";
  const pending = ui.submit(); ui.respond(); await pending;
  assert.equal(original.removed, true);
  assert.deepEqual(Array.from(ui.markers.at(-1).points), [10, 20]);
  assert.equal(ui.get("address").value, "Typed place");
  assert.equal(Number(ui.get("latitude").value), 10);
  assert.equal(Number(ui.get("longitude").value), 20);
  assert.equal(ui.generate.disabled, false);
});

test("a current response renders selectable alternatives and the selected GPX", async () => {
  const ui = app(), pending = ui.submit(); ui.respond(); await pending;
  const cards = ui.get("route-list").children;
  assert.equal(cards.length, 2);
  await cards[1].fire("click");
  assert.equal(ui.get("summary-distance").textContent, "4.98 mi");
  assert.equal(ui.get("summary-gain").textContent, "250 ft");
  assert.equal(Number(ui.get("summary-signals").textContent), 2);
  assert.equal(cards[1].getAttribute("aria-pressed"), "true");
  assert.equal(cards[0].getAttribute("aria-pressed"), "false");
  await ui.get("download-gpx").fire("click");
  assert.equal(await ui.blobs.at(-1).text(), "<gpx>second</gpx>");
});

test("a late response cannot overwrite a newly chosen start", async () => {
  const ui = app(), pending = ui.submit();
  ui.get("address").value = "New pin"; ui.get("latitude").value = "11"; ui.get("longitude").value = "21";
  ui.respond(); await pending;
  assert.equal(ui.state().routes.length, 0);
  assert.equal(ui.get("address").value, "New pin");
  assert.equal(ui.get("latitude").value, "11");
  assert.equal(ui.generate.disabled, false);
  assert.match(ui.get("form-status").textContent, /changed|updated|again/i);
});

for (const [control, value] of [["distance", 8], ["surface", "trail"], ["elevation", "hilly"], ["drive", 1]]) {
  test(`a late response cannot replace a new ${control} preference`, async () => {
    const ui = app(), pending = ui.submit();
    if (control === "distance") ui.get("distance").value = String(value);
    else await ui.choose(control, value);
    ui.respond(); await pending;
    assert.equal(ui.state().routes.length, 0);
    assert.equal(ui.generate.disabled, false);
    assert.match(ui.get("form-status").textContent, /changed|updated|again/i);
  });
}

test("a network failure unlocks the form and a retry succeeds", async () => {
  const ui = app(), failed = ui.submit();
  ui.requests.at(-1).reject(new Error("Network unavailable")); await failed;
  assert.equal(ui.generate.disabled, false);
  assert.equal(ui.generate.getAttribute("aria-busy"), null);
  assert.match(ui.get("form-status").textContent, /network/i);
  const retry = ui.submit(); ui.respond(); await retry;
  assert.equal(ui.state().routes.length, 2);
});

test("an HTML server error displays a readable error and permits retry", async () => {
  const ui = app(), pending = ui.submit();
  ui.respond(null, { ok: false, status: 502, jsonError: new SyntaxError("Unexpected token <") }); await pending;
  assert.equal(ui.generate.disabled, false);
  assert.match(ui.get("form-status").textContent, /502|server|try again/i);
  assert.doesNotMatch(ui.get("form-status").textContent, /Unexpected token/);
  assert.equal(ui.state().routes.length, 0);
});

test("validation errors are shown as readable messages", async () => {
  const ui = app(), pending = ui.submit();
  ui.respond({ detail: [{ loc: ["body", "distance_miles"], msg: "Choose a positive distance" }] }, { ok: false, status: 422 });
  await pending;
  assert.match(ui.get("form-status").textContent, /Choose a positive distance/);
  assert.equal(ui.generate.disabled, false);
});

test("map style toggles and reset view fits the current alternatives", async () => {
  const ui = app();
  await ui.get("map-style").fire("click");
  assert.equal(ui.body.classList.contains("light-map"), true);
  assert.equal(ui.get("map-style").getAttribute("aria-pressed"), "true");
  await ui.get("map-style").fire("click");
  assert.equal(ui.body.classList.contains("light-map"), false);
  const pending = ui.submit(); ui.respond(); await pending;
  const before = ui.map.fits.length;
  await ui.get("reset-view").fire("click");
  assert.equal(ui.map.fits.length, before + 1);
});

test("an incomplete new response preserves the last valid route choices", async () => {
  const ui = app(), initial = ui.submit(); ui.respond(); await initial;
  const original = ui.state().routes;
  const pending = ui.submit();
  ui.respond({ origin: { latitude: 10, longitude: 20 }, source: "pool", routes: [] });
  await pending;
  assert.equal(ui.state().routes, original);
  assert.equal(ui.get("route-list").children.length, 2);
  assert.equal(ui.generate.disabled, false);
  assert.ok(ui.get("form-status").textContent.length > 0);
});

test("missing elevation coverage is shown as unavailable rather than zero climbing", async () => {
  const ui = app(), data = payload();
  data.routes[0].feature.properties.elevation_coverage = 0;
  data.routes[0].feature.properties.elevation_gain_feet = 0;
  const pending = ui.submit(); ui.respond(data); await pending;
  assert.equal(ui.get("summary-gain").textContent, "—");
});

test("selected surface composition uses each route's measured fraction", async () => {
  const ui = app(), pending = ui.submit(); ui.respond(); await pending;
  assert.equal(ui.get("surface-trail-fill").style.width, "20%");
  assert.equal(ui.get("surface-trail-label").textContent, "20% trail");
  assert.equal(ui.get("surface-other-label").textContent, "80% other surfaces");
  await ui.get("route-list").children[1].fire("click");
  assert.equal(ui.get("surface-trail-fill").style.width, "80%");
  assert.equal(ui.get("surface-trail-label").textContent, "80% trail");
  assert.equal(ui.get("surface-other-label").textContent, "20% other surfaces");
  assert.match(ui.get("surface-panel").getAttribute("aria-label"), /80% trail/);
});

test("changing the start clears old route exports and measured details", async () => {
  const ui = app(), pending = ui.submit(); ui.respond(); await pending;
  ui.get("address").value = "A different synthetic place";
  await ui.get("address").fire("input");
  assert.equal(ui.state().routes.length, 0);
  assert.equal(ui.get("selected-summary").hidden, true);
  assert.equal(ui.get("surface-panel").hidden, true);
  await ui.get("download-gpx").fire("click");
  assert.equal(ui.blobs.length, 0);
});

test("repeated submit while planning sends only one route request", async () => {
  const ui = app(), pending = ui.submit();
  await ui.submit();
  assert.equal(ui.requests.length, 1);
  ui.respond(); await pending;
  assert.equal(ui.generate.disabled, false);
  assert.equal(ui.state().routes.length, 2);
});

test("an error for an old start does not become the new start's error", async () => {
  const ui = app(), pending = ui.submit();
  ui.get("address").value = "A newly selected synthetic start";
  ui.get("latitude").value = "11";
  ui.respond({ detail: "The old start is outside coverage" }, { ok: false, status: 422 });
  await pending;
  assert.match(ui.get("form-status").textContent, /Start changed/);
  assert.doesNotMatch(ui.get("form-status").textContent, /outside coverage/);
  assert.equal(ui.generate.disabled, false);
});

function directPayload() {
  const data = payload();
  data.mode = "point_to_point";
  data.source = "live";
  data.destination = { latitude: 10.01, longitude: 20.02 };
  data.routes = [data.routes[0]];
  data.routes[0].feature.geometry.coordinates = [[20, 10], [20.02, 10.01]];
  return data;
}

test("start-to-finish sends a destination without loop-only preferences and renders an open path", async () => {
  const ui = app();
  await ui.choose("elevation", "hilly"); await ui.choose("drive", 1);
  await ui.mode("point_to_point");
  ui.get("destination-address").value = "Synthetic finish";
  ui.get("destination-latitude").value = "10.01";
  ui.get("destination-longitude").value = "20.02";
  const pending = ui.submit();
  const body = JSON.parse(ui.requests.at(-1).options.body);
  assert.equal(body.mode, "point_to_point");
  assert.equal(body.distance_miles, undefined);
  assert.equal(body.drive_radius_miles, 0);
  assert.equal(body.elevation, "balanced");
  assert.deepEqual(body.destination, { latitude: 10.01, longitude: 20.02 });
  ui.respond(directPayload()); await pending;
  assert.equal(ui.state().routes.length, 1);
  assert.deepEqual(Array.from(ui.state().finishMarker.points), [10.01, 20.02]);
  assert.match(ui.get("request-summary").textContent, /Start → finish/);
  assert.equal(ui.get("distance").disabled, true);
  await ui.mode("loop");
  assert.equal(ui.get("distance").disabled, false);
  assert.equal(ui.get("destination-field").hidden, true);
});

test("a closed loop from a legacy backend is rejected for a direct request", async () => {
  const ui = app(); await ui.mode("point_to_point");
  ui.get("destination-address").value = "Synthetic finish";
  const pending = ui.submit();
  const invalid = directPayload(); invalid.routes[0].feature.geometry.coordinates.push([20, 10]);
  ui.respond(invalid); await pending;
  assert.equal(ui.state().routes.length, 0);
  assert.match(ui.get("form-status").textContent, /could not be read/i);
});

test("a destination edited during planning discards the old path", async () => {
  const ui = app(); await ui.mode("point_to_point");
  ui.get("destination-address").value = "Synthetic finish";
  const pending = ui.submit();
  ui.get("destination-address").value = "A different finish";
  ui.respond(directPayload()); await pending;
  assert.equal(ui.state().routes.length, 0);
  assert.match(ui.get("form-status").textContent, /changed/i);
});

test("map endpoint controls select finish independently from start", async () => {
  const ui = app(); await ui.mode("point_to_point");
  ui.mapClick(11, 21);
  assert.equal(ui.get("destination-latitude").value, "11.000000");
  assert.equal(Number(ui.get("latitude").value), 1);
  await ui.get("pick-start").fire("click"); ui.mapClick(12, 22);
  assert.equal(ui.get("latitude").value, "12.000000");
  assert.equal(ui.get("destination-latitude").value, "11.000000");
});

test("Run uses the selected route and prevents planner edits while the session is active", async () => {
  let selected, active = false;
  const ui = app({ runFactory({ getSelectedRoute, onActiveChange }) {
    return { isActive: () => active, start() { selected = getSelectedRoute(); active = true; onActiveChange(true); } };
  } });
  const pending = ui.submit(); ui.respond(); await pending;
  await ui.get("route-list").children[1].fire("click");
  await ui.get("start-run").fire("click");
  assert.equal(selected.gpx, "<gpx>second</gpx>");
  ui.mapClick(11, 21);
  assert.equal(Number(ui.get("latitude").value), 10);
  await ui.submit();
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.state().routes.length, 2);
});
