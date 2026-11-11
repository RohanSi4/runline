import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../web/run.js", import.meta.url), "utf8");

function library(window = {}) {
  vm.runInNewContext(source, { window });
  return window.RunlineRun;
}

function fix(timestamp, longitude = 0, accuracy = 5, latitude = 0) {
  return { timestamp, coords: { latitude, longitude, accuracy } };
}

test("distance comes from measured GPS positions and elapsed time from the clock", () => {
  let clock = 100000;
  const recorder = library().createRecorder({ now: () => clock });
  recorder.prepare();
  clock += 10000;
  assert.equal(recorder.snapshot().elapsedMs, 0, "permission wait is not active run time");
  recorder.accept(fix(clock));
  clock += 5000;
  recorder.accept(fix(clock, 0.0001));
  clock += 5000;
  recorder.accept(fix(clock, 0.0002));
  assert.ok(Math.abs(recorder.snapshot().distanceMeters - 22.239) < 0.01);
  clock += 12000; // No timer callbacks are needed for time to remain correct.
  assert.equal(recorder.snapshot().elapsedMs, 22000);
});

test("manual pause excludes time and never bridges movement during the pause", () => {
  let clock = 100000;
  const recorder = library().createRecorder({ now: () => clock });
  recorder.prepare(); recorder.accept(fix(clock));
  clock += 5000; recorder.accept(fix(clock, 0.0001));
  recorder.pause();
  clock += 20000;
  assert.equal(recorder.snapshot().elapsedMs, 5000);
  assert.equal(recorder.accept(fix(clock, 0.1)).reason, "inactive");
  recorder.resume(); recorder.accept(fix(clock, 0.1));
  clock += 5000; recorder.accept(fix(clock, 0.1001));
  assert.equal(recorder.snapshot().elapsedMs, 10000);
  assert.equal(recorder.snapshot().segmentCount, 2);
  assert.ok(Math.abs(recorder.snapshot().distanceMeters - 22.239) < 0.01);
});

test("stationary jitter adds no distance", () => {
  let clock = 100000;
  const recorder = library().createRecorder({ now: () => clock });
  recorder.prepare(); recorder.accept(fix(clock));
  for (let i = 0; i < 10; i += 1) {
    clock += 1000;
    assert.equal(recorder.accept(fix(clock, i % 2 ? 0.00001 : -0.00001)).reason, "jitter");
  }
  assert.equal(recorder.snapshot().distanceMeters, 0);
  assert.equal(recorder.snapshot().elapsedMs, 10000);
});

test("bad accuracy, old fixes, invalid coordinates and impossible jumps are excluded", () => {
  let clock = 100000;
  const recorder = library().createRecorder({ now: () => clock });
  recorder.prepare();
  assert.equal(recorder.accept(fix(clock, 0, 100)).reason, "accuracy");
  assert.equal(recorder.snapshot().phase, "locating");
  clock += 1000; recorder.accept(fix(clock));
  assert.equal(recorder.accept(fix(clock - 1, 0.0001)).reason, "out-of-order");
  assert.equal(recorder.accept(fix(clock + 1, 0, 5, 91)).reason, "invalid");
  clock += 1000;
  assert.equal(recorder.accept(fix(clock, 1)).reason, "jump");
  assert.equal(recorder.snapshot().position.longitude, 0, "rejected jump must not move the current location");
  assert.equal(recorder.snapshot().distanceMeters, 0);
});

test("long and low-accuracy gaps start new segments without invented distance", () => {
  let clock = 100000;
  const recorder = library().createRecorder({ now: () => clock });
  recorder.prepare(); recorder.accept(fix(clock));
  clock += 40000; recorder.accept(fix(clock, 0.1));
  clock += 1000; recorder.accept(fix(clock, 0.1001, 100));
  clock += 1000; recorder.accept(fix(clock, 0.2));
  assert.equal(recorder.snapshot().distanceMeters, 0);
  assert.equal(recorder.snapshot().segmentCount, 3);
});

test("a rejected future timestamp cannot poison later valid GPS fixes", () => {
  let clock = 100000;
  const recorder = library().createRecorder({ now: () => clock });
  recorder.prepare(); recorder.accept(fix(clock));
  assert.equal(recorder.accept(fix(clock + 60000, 0.1)).reason, "stale");
  clock += 1000;
  assert.equal(recorder.accept(fix(clock, 0.0001)).accepted, true);
  clock += 5000;
  assert.equal(recorder.accept(fix(clock, 0.0002)).accepted, true);
  assert.ok(recorder.snapshot().distanceMeters > 11);
});

test("finishing freezes time and ignores later GPS samples", () => {
  let clock = 100000;
  const recorder = library().createRecorder({ now: () => clock });
  recorder.prepare(); recorder.accept(fix(clock));
  clock += 5000; recorder.accept(fix(clock, 0.0001));
  recorder.finish();
  const measured = recorder.snapshot().distanceMeters;
  clock += 60000;
  assert.equal(recorder.accept(fix(clock, 0.1)).reason, "inactive");
  assert.equal(recorder.snapshot().distanceMeters, measured);
  assert.equal(recorder.snapshot().elapsedMs, 5000);
});

function node() {
  return {
    hidden: false, disabled: false, textContent: "", attributes: {}, listeners: new Map(),
    setAttribute(name, value) { this.attributes[name] = String(value); },
    addEventListener(name, callback) { this.listeners.set(name, callback); },
    click() { return this.listeners.get("click")?.(); },
    focus() { this.focused = true; },
  };
}

function harness({ supported = true, wake = false, deferredWake = false, confirm = true, synchronousError = false } = {}) {
  let clock = 100000;
  let counter = 0;
  const ids = new Map();
  const get = (id) => { if (!ids.has(id)) ids.set(id, node()); return ids.get(id); };
  const watches = new Map(), cleared = [], intervals = new Map(), mapEvents = new Map();
  const documentEvents = new Map(), windowEvents = new Map(), layers = [], activeStates = [];
  const geolocation = {
    watchPosition(success, error, options) {
      const id = ++counter;
      watches.set(id, { success, error, options });
      if (synchronousError) error({ code: 1 });
      return id;
    },
    clearWatch(id) { cleared.push(id); },
  };
  function layer(points, options) {
    const item = {
      points, options, removed: false,
      addTo() { return this; }, remove() { this.removed = true; },
      getBounds() { return { pad() { return this; } }; },
      setLatLng(value) { this.position = value; }, setRadius(value) { this.radius = value; },
      addLatLng(value) { this.points.push(value); },
    };
    layers.push(item); return item;
  }
  const map = { invalidations: 0, pans: [], fits: [],
    on(name, callback) { mapEvents.set(name, callback); },
    panTo(point) { this.pans.push(point); }, fitBounds(bounds) { this.fits.push(bounds); },
    invalidateSize() { this.invalidations += 1; },
  };
  const doc = { body: { dataset: {} }, visibilityState: "visible", getElementById: get,
    addEventListener(name, callback) { documentEvents.set(name, callback); } };
  const locks = [];
  const wakeGrants = [];
  const window = {
    document: doc, isSecureContext: true,
    navigator: { ...(supported ? { geolocation } : {}), ...(wake ? { wakeLock: {
      async request() {
        const lock = { released: false, release() { this.released = true; }, addEventListener() {} };
        locks.push(lock);
        return deferredWake ? new Promise((resolve) => wakeGrants.push(() => resolve(lock))) : lock;
      },
    } } : {}) },
    setInterval(callback) { const id = ++counter; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); },
    addEventListener(name, callback) { windowEvents.set(name, callback); },
    confirm() { return confirm; },
  };
  const selected = { feature: { geometry: { type: "LineString", coordinates: [[0, 0], [0.001, 0], [0, 0]] },
    properties: { distance_miles: 5 } }, gpx: "<gpx/>" };
  const run = library(window).create({ map, L: { polyline: layer, circleMarker: layer, circle: layer },
    getSelectedRoute: () => selected, onActiveChange(value) { activeStates.push(value); }, now: () => clock });
  return { run, get, doc, map, layers, watches, cleared, intervals, activeStates, locks, selected,
    grantWake() { wakeGrants.splice(0).forEach((grant) => grant()); },
    advance(ms) { clock += ms; },
    position(longitude = 0, accuracy = 5, id = [...watches.keys()].at(-1)) { watches.get(id).success(fix(clock, longitude, accuracy)); },
    error(code, id = [...watches.keys()].at(-1)) { watches.get(id).error({ code }); },
    visibility(value) { doc.visibilityState = value; documentEvents.get("visibilitychange")(); },
    pagehide() { windowEvents.get("pagehide")(); },
    drag() { mapEvents.get("dragstart")(); },
  };
}

test("a Run session freezes the plan but reports measured distance, then cleans up", async () => {
  const app = harness({ wake: true });
  assert.equal(app.run.start(), true);
  assert.equal(app.run.isActive(), true);
  assert.equal(app.get("run-distance").textContent, "0.00 mi");
  assert.equal(app.get("run-finish").disabled, true);
  app.selected.feature.geometry.coordinates[0][0] = 1;
  assert.equal(app.layers[0].points[0][1], 0, "planned layer is independent of later route selection edits");
  app.position();
  app.advance(5000); app.position(0.0001);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(app.get("run-time").textContent, "00:05");
  assert.ok(app.run.snapshot().distanceMeters > 11 && app.run.snapshot().distanceMeters < 12);
  assert.equal(app.get("run-finish").disabled, false);
  assert.equal(app.locks.length, 1);
  assert.equal(app.run.finish(), true);
  assert.equal(app.intervals.size, 0);
  assert.equal(app.cleared.length, 1);
  assert.equal(app.locks[0].released, true);
  assert.equal(app.run.isActive(), true, "finished summary keeps the planner locked until close");
  assert.match(app.get("run-summary").textContent, /0\.01 miles recorded/);
  assert.doesNotMatch(app.get("run-summary").textContent, /5\.00/);
  app.advance(5000); app.position(0.1);
  assert.ok(app.run.snapshot().distanceMeters < 12, "late finished watch callback is ignored");
  app.run.close();
  assert.equal(app.run.isActive(), false);
  assert.equal(app.get("run-panel").hidden, true);
  assert.ok(app.layers.every((item) => item.removed));
  assert.deepEqual(app.activeStates, [true, false]);
});

test("permission denial can be retried, and no-fix sessions always permit exit", () => {
  const app = harness();
  app.run.start(); app.error(1);
  assert.equal(app.run.snapshot().phase, "paused");
  assert.match(app.get("run-status").textContent, /denied/);
  assert.equal(app.get("run-pause").disabled, false);
  assert.equal(app.get("run-close").hidden, false);
  app.get("run-pause").click();
  app.advance(1000); app.position();
  assert.equal(app.run.snapshot().phase, "running");
  const missing = harness({ supported: false });
  missing.run.start();
  assert.match(missing.get("run-status").textContent, /location support/);
  assert.equal(missing.run.close(), true);
});

test("a wake lock granted after finishing is immediately released", async () => {
  const app = harness({ wake: true, deferredWake: true });
  app.run.start(); app.position();
  assert.equal(app.locks.length, 1);
  app.run.finish();
  app.grantWake();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(app.locks[0].released, true);
  assert.equal(app.run.snapshot().phase, "finished");
  assert.equal(app.intervals.size, 0);
});

test("hidden/pagehide pauses recording and resumed watches reject older callbacks", () => {
  const app = harness();
  app.run.start(); app.position();
  const old = [...app.watches.keys()].at(-1);
  app.advance(5000); app.position(0.0001);
  app.visibility("hidden");
  assert.equal(app.run.snapshot().phase, "paused");
  assert.equal(app.intervals.size, 0);
  assert.match(app.get("run-status").textContent, /background distance is not recorded/);
  app.advance(60000); app.position(0.1, 5, old);
  assert.ok(app.run.snapshot().distanceMeters < 12);
  app.visibility("visible");
  assert.equal(app.run.snapshot().phase, "paused", "returning does not silently resume");
  app.run.resume(); app.position(0.1);
  app.advance(5000); app.position(0.1001);
  app.position(1, 5, old);
  assert.ok(Math.abs(app.run.snapshot().distanceMeters - 22.239) < 0.01);
  assert.equal(app.run.snapshot().elapsedMs, 10000);
  app.pagehide();
  assert.equal(app.run.snapshot().phase, "paused");
  assert.equal(app.intervals.size, 0);
});

test("low accuracy is reported honestly, transient GPS errors do not invent a bridge", () => {
  const app = harness();
  app.run.start(); app.position(0, 100);
  assert.equal(app.run.snapshot().phase, "locating");
  assert.match(app.get("run-status").textContent, /accuracy is low/);
  app.advance(1000); app.position();
  app.advance(5000); app.position(0.0001);
  app.error(2);
  app.advance(5000); app.position(0.1);
  assert.ok(app.run.snapshot().distanceMeters < 12);
  assert.equal(app.run.snapshot().segmentCount, 2);
});

test("follow can be disabled by panning and reenabled without adding distance", () => {
  const app = harness();
  app.run.start(); app.position();
  app.drag();
  assert.equal(app.get("run-follow").attributes["aria-pressed"], "false");
  const pans = app.map.pans.length;
  app.advance(5000); app.position(0.0001);
  assert.equal(app.map.pans.length, pans);
  const distance = app.run.snapshot().distanceMeters;
  app.get("run-follow").click();
  assert.equal(app.map.pans.length, pans + 1);
  assert.equal(app.run.snapshot().distanceMeters, distance);
});

test("synchronous permission errors clear their returned watch and exit does not discard on Cancel", () => {
  const denied = harness({ synchronousError: true });
  denied.run.start();
  assert.equal(denied.run.snapshot().phase, "paused");
  assert.equal(denied.cleared.length, 1);
  const app = harness({ confirm: false });
  app.run.start(); app.position();
  assert.equal(app.run.close(), false);
  assert.equal(app.run.isActive(), true);
  assert.equal(app.run.snapshot().phase, "running");
});
