(function (global) {
  "use strict";

  const METERS_PER_MILE = 1609.344;
  const MAX_ACCURACY_METERS = 40;
  const MAX_GAP_MS = 30000;
  const MAX_SPEED_METERS_PER_SECOND = 15;

  function metersBetween(first, second) {
    const radians = (degrees) => degrees * Math.PI / 180;
    const latitude = radians(second.latitude - first.latitude);
    const longitude = radians(second.longitude - first.longitude);
    const a = Math.sin(latitude / 2) ** 2 + Math.cos(radians(first.latitude)) *
      Math.cos(radians(second.latitude)) * Math.sin(longitude / 2) ** 2;
    return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(Math.max(0, 1 - a)));
  }

  // Session math is independent of DOM, map rendering and the GPS provider.
  // Only accepted measured fixes contribute distance; planned miles never do.
  function createRecorder({ now = () => Date.now() } = {}) {
    let phase = "idle";
    let elapsed = 0;
    let activeSince = null;
    let distance = 0;
    let anchor = null;
    let position = null;
    let lastTimestamp = -Infinity;
    let segments = [];
    let hasFix = false;

    function snapshot({ includeTrack = true } = {}) {
      return {
        phase, distanceMeters: distance,
        elapsedMs: elapsed + (activeSince === null ? 0 : Math.max(0, now() - activeSince)),
        hasFix, position: position && { ...position }, segmentCount: segments.length,
        ...(includeTrack ? { segments: segments.map((segment) => segment.map((fix) => ({ ...fix }))) } : {}),
      };
    }

    function prepare() {
      phase = "locating";
      elapsed = 0;
      activeSince = null;
      distance = 0;
      anchor = null;
      position = null;
      lastTimestamp = -Infinity;
      segments = [];
      hasFix = false;
      return snapshot();
    }

    function breakSegment() { anchor = null; }

    function accept(raw) {
      if (phase !== "locating" && phase !== "running") return { accepted: false, reason: "inactive" };
      const fix = {
        latitude: raw?.coords?.latitude, longitude: raw?.coords?.longitude,
        accuracy: raw?.coords?.accuracy, timestamp: raw?.timestamp,
      };
      if (![fix.latitude, fix.longitude, fix.accuracy, fix.timestamp].every(Number.isFinite) ||
          Math.abs(fix.latitude) > 90 || Math.abs(fix.longitude) > 180 || fix.accuracy < 0) {
        return { accepted: false, reason: "invalid" };
      }
      if (fix.timestamp <= lastTimestamp) return { accepted: false, reason: "out-of-order" };
      if (now() - fix.timestamp > 15000 || fix.timestamp - now() > 10000) {
        breakSegment();
        return { accepted: false, reason: "stale" };
      }
      lastTimestamp = fix.timestamp;
      if (fix.accuracy > MAX_ACCURACY_METERS) {
        breakSegment();
        return { accepted: false, reason: "accuracy", accuracy: fix.accuracy };
      }
      if (phase === "locating") {
        phase = "running";
        activeSince = now();
      }
      let added = 0;
      if (anchor) {
        const gap = fix.timestamp - anchor.timestamp;
        if (gap > MAX_GAP_MS) anchor = null;
        else {
          added = metersBetween(anchor, fix);
          if (added / (gap / 1000) > MAX_SPEED_METERS_PER_SECOND) {
            return { accepted: false, reason: "jump" };
          }
          // Suppress small stationary GPS drift; keep the prior anchor so
          // genuine movement accumulates beyond this threshold.
          const noise = Math.max(3, Math.min(10, (anchor.accuracy + fix.accuracy) * 0.25));
          if (added < noise) {
            position = fix;
            return { accepted: false, reason: "jitter", fix };
          }
        }
      }
      if (!anchor) {
        segments.push([]);
        added = 0;
      }
      distance += added;
      position = fix;
      segments.at(-1).push(fix);
      anchor = fix;
      hasFix = true;
      return { accepted: true, fix, addedMeters: added, segmentIndex: segments.length - 1 };
    }

    function pause() {
      if (phase !== "running" && phase !== "locating") return snapshot();
      if (activeSince !== null) elapsed += Math.max(0, now() - activeSince);
      activeSince = null;
      anchor = null;
      phase = "paused";
      return snapshot();
    }

    function resume() {
      if (phase !== "paused") return snapshot();
      phase = "locating";
      anchor = null;
      return snapshot();
    }

    function finish() {
      if (phase === "idle" || phase === "finished") return snapshot();
      if (activeSince !== null) elapsed += Math.max(0, now() - activeSince);
      activeSince = null;
      anchor = null;
      phase = "finished";
      return snapshot();
    }

    function reset() {
      prepare();
      phase = "idle";
      return snapshot();
    }

    return { prepare, accept, pause, resume, finish, reset, breakSegment, snapshot };
  }

  function timeLabel(milliseconds) {
    const total = Math.max(0, Math.floor(milliseconds / 1000));
    const seconds = String(total % 60).padStart(2, "0");
    const minutes = Math.floor(total / 60);
    return minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${seconds}`
      : `${String(minutes).padStart(2, "0")}:${seconds}`;
  }

  function paceLabel(snapshot) {
    if (snapshot.distanceMeters < 20 || snapshot.elapsedMs <= 0) return "—";
    const seconds = Math.round(snapshot.elapsedMs / 1000 / (snapshot.distanceMeters / METERS_PER_MILE));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} /mi`;
  }

  function create({ map, L, getSelectedRoute, onActiveChange = () => {}, now = () => Date.now(),
    geolocation = global.navigator?.geolocation } = {}) {
    const doc = global.document;
    const recorder = createRecorder({ now });
    let plannedRoute = null;
    let watchId = null;
    let intervalId = null;
    let generation = 0;
    let wakeLock = null;
    let wakePending = null;
    let follow = true;
    let active = false;
    let plannedLayer = null;
    let positionLayer = null;
    let accuracyLayer = null;
    let trackLayers = [];
    let followingMap = false;

    const element = (id) => doc.getElementById(id);
    const text = (id, value) => { const node = element(id); if (node) node.textContent = value; };
    const hidden = (id, value) => { const node = element(id); if (node) node.hidden = value; };
    const status = (value) => text("run-status", value);

    function releaseWakeLock() {
      const previous = wakeLock;
      wakeLock = null;
      if (previous) Promise.resolve(previous.release()).catch(() => {});
    }

    async function acquireWakeLock() {
      if (!global.navigator?.wakeLock || wakeLock || wakePending !== null || doc.visibilityState === "hidden") return;
      const current = generation;
      wakePending = current;
      try {
        const lock = await global.navigator.wakeLock.request("screen");
        if (!active || current !== generation || recorder.snapshot({ includeTrack: false }).phase !== "running") {
          await lock.release();
          return;
        }
        wakeLock = lock;
        lock.addEventListener?.("release", () => { if (wakeLock === lock) wakeLock = null; });
      } catch (_error) {
        // Screen wake lock is optional and may be denied by the device.
        // The always-visible foreground note remains the recording contract.
      } finally { if (wakePending === current) wakePending = null; }
    }

    function stopResources() {
      generation += 1;
      if (watchId !== null) geolocation?.clearWatch(watchId);
      watchId = null;
      if (intervalId !== null) global.clearInterval(intervalId);
      intervalId = null;
      releaseWakeLock();
      wakePending = null;
    }

    function removeLayers() {
      [plannedLayer, positionLayer, accuracyLayer, ...trackLayers].forEach((layer) => layer?.remove());
      plannedLayer = positionLayer = accuracyLayer = null;
      trackLayers = [];
    }

    function render() {
      const current = recorder.snapshot({ includeTrack: false });
      doc.body.dataset.runState = current.phase;
      hidden("run-panel", !active);
      text("run-distance", `${(current.distanceMeters / METERS_PER_MILE).toFixed(2)} mi`);
      text("run-time", timeLabel(current.elapsedMs));
      text("run-pace", paceLabel(current));
      const pause = element("run-pause");
      if (pause) {
        pause.textContent = current.phase === "paused" ? "Resume run" : current.phase === "locating" ? "Waiting for GPS…" : "Pause run";
        pause.disabled = current.phase === "locating" || current.phase === "finished";
      }
      const finish = element("run-finish");
      if (finish) finish.disabled = !current.hasFix || current.phase === "finished";
      hidden("run-pause", current.phase === "finished");
      hidden("run-finish", current.phase === "finished");
      hidden("run-close", false);
      text("run-close", current.phase === "finished" ? "Back to routes" : "Exit run");
      const followButton = element("run-follow");
      followButton?.setAttribute("aria-pressed", String(follow));
      if (followButton) {
        followButton.disabled = current.phase === "finished";
        followButton.setAttribute("aria-label", follow ? "Stop following location" : "Follow location");
      }
      if (current.phase === "finished") {
        text("run-title", "Run finished");
        text("run-summary", `${(current.distanceMeters / METERS_PER_MILE).toFixed(2)} miles recorded in ${timeLabel(current.elapsedMs)} active time. This session is not saved after you leave.`);
        hidden("run-summary", false);
      }
    }

    function updateMap(fix, result) {
      const point = [fix.latitude, fix.longitude];
      if (!positionLayer) {
        positionLayer = L.circleMarker(point, { radius: 7, color: "#ffffff", weight: 3,
          fillColor: "#78b8ff", fillOpacity: 1, className: "run-position" }).addTo(map);
        accuracyLayer = L.circle(point, { radius: fix.accuracy, color: "#78b8ff", weight: 1,
          fillOpacity: 0.1, interactive: false, className: "run-accuracy-circle" }).addTo(map);
      } else {
        positionLayer.setLatLng(point);
        accuracyLayer.setLatLng(point);
        accuracyLayer.setRadius(fix.accuracy);
      }
      if (result.accepted) {
        if (!trackLayers[result.segmentIndex]) {
          trackLayers[result.segmentIndex] = L.polyline([], { color: "#78b8ff", weight: 6,
            opacity: 1, className: "run-track-line" }).addTo(map);
        }
        trackLayers[result.segmentIndex].addLatLng(point);
      }
      if (follow) {
        followingMap = true;
        map.panTo(point, { animate: false });
        followingMap = false;
      }
    }

    function handlePosition(raw, current) {
      if (!active || current !== generation) return;
      const result = recorder.accept(raw);
      if (result.accepted || result.reason === "jitter") {
        updateMap(result.fix, result);
        text("run-accuracy", `±${Math.round(result.fix.accuracy)} m`);
        status("Recording your run. Keep this screen open.");
        if (intervalId === null) intervalId = global.setInterval(render, 1000);
        acquireWakeLock();
      } else if (result.reason === "accuracy") {
        text("run-accuracy", `±${Math.round(result.accuracy)} m`);
        status("GPS accuracy is low. Distance recording resumes with a clearer signal.");
      } else if (result.reason === "jump") {
        status("An unusual GPS jump was ignored. Waiting for a reliable position.");
      } else if (result.reason === "stale") {
        status("Waiting for a fresh GPS position. Missing distance will not be guessed.");
      }
      render();
    }

    function handleError(error, current) {
      if (!active || current !== generation) return;
      recorder.breakSegment();
      if (error?.code === 1) {
        recorder.pause();
        stopResources();
        status("Location access was denied. Allow location in your browser settings, then tap Resume run, or exit.");
      } else {
        status(error?.code === 3 ? "GPS is taking longer than expected. Waiting for a reliable position."
          : "GPS is unavailable right now. Keep this screen open; missing distance will not be guessed.");
      }
      render();
    }

    function beginWatch() {
      const current = ++generation;
      try {
        const id = geolocation.watchPosition((position) => handlePosition(position, current),
          (error) => handleError(error, current), { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
        // Some providers invoke a synchronous error; clear the returned watch
        // too if that error already invalidated this generation.
        if (current === generation) watchId = id;
        else geolocation.clearWatch(id);
      } catch (_error) {
        recorder.pause();
        stopResources();
        status("Location could not be started. Check browser permissions, then Resume run or exit.");
        render();
      }
    }

    function start() {
      if (active) { element("run-title")?.focus(); return false; }
      const selected = getSelectedRoute?.();
      const coordinates = selected?.feature?.geometry?.coordinates;
      if (!Array.isArray(coordinates) || coordinates.length < 2) return false;
      plannedRoute = JSON.parse(JSON.stringify(selected));
      recorder.prepare();
      active = true;
      follow = true;
      text("run-title", "Your run");
      text("run-accuracy", "Waiting for GPS");
      hidden("run-summary", true);
      onActiveChange(true);
      plannedLayer = L.polyline(plannedRoute.feature.geometry.coordinates.map(([longitude, latitude]) => [latitude, longitude]),
        { color: "#d9f977", weight: 4, opacity: 0.8, className: "run-planned-line" }).addTo(map);
      render();
      map.invalidateSize();
      map.fitBounds(plannedLayer.getBounds().pad(0.12));
      element("run-title")?.focus();
      if (!geolocation || global.isSecureContext === false) {
        recorder.pause();
        status("Live recording needs location support and an HTTPS connection. You can exit and keep viewing your planned route.");
        render();
        return true;
      }
      if (doc.visibilityState === "hidden") {
        recorder.pause();
        status("Return to this screen and tap Resume run to start recording.");
        render();
        return true;
      }
      status("Finding your GPS position. Recording starts with the first reliable fix.");
      beginWatch();
      return true;
    }

    function pause(reason = "Run paused. Tap Resume run when you are ready.") {
      const phase = recorder.snapshot({ includeTrack: false }).phase;
      if (!active || (phase !== "running" && phase !== "locating")) return false;
      recorder.pause();
      stopResources();
      status(reason);
      render();
      return true;
    }

    function resume() {
      if (!active || recorder.snapshot({ includeTrack: false }).phase !== "paused") return false;
      if (!geolocation || global.isSecureContext === false) {
        status("Location recording is not available in this browser. Exit to return to your route.");
        return false;
      }
      if (doc.visibilityState === "hidden") return false;
      recorder.resume();
      status("Finding GPS again. Recording resumes with a reliable fix.");
      render();
      beginWatch();
      return true;
    }

    function finish() {
      const current = recorder.snapshot({ includeTrack: false });
      if (!active || !current.hasFix || current.phase === "finished") return false;
      recorder.finish();
      stopResources();
      status("Recording stopped. Your measured run is shown below.");
      render();
      element("run-title")?.focus();
      return true;
    }

    function close() {
      if (!active) return false;
      const current = recorder.snapshot({ includeTrack: false });
      if (current.hasFix && current.phase !== "finished" && global.confirm &&
          !global.confirm("Exit this run? This recording is not saved. Choose Cancel to keep recording or finish your run first.")) return false;
      stopResources();
      removeLayers();
      recorder.reset();
      plannedRoute = null;
      active = false;
      doc.body.dataset.runState = "idle";
      hidden("run-panel", true);
      onActiveChange(false);
      map.invalidateSize();
      element("start-run")?.focus();
      return true;
    }

    element("run-pause")?.addEventListener("click", () => recorder.snapshot({ includeTrack: false }).phase === "paused" ? resume() : pause());
    element("run-finish")?.addEventListener("click", finish);
    element("run-close")?.addEventListener("click", close);
    element("run-follow")?.addEventListener("click", () => {
      follow = !follow;
      const current = recorder.snapshot({ includeTrack: false });
      if (follow && current.position) updateMap(current.position, { accepted: false });
      render();
    });
    map.on("dragstart", () => {
      if (active && !followingMap) { follow = false; render(); }
    });
    doc.addEventListener("visibilitychange", () => {
      if (doc.visibilityState === "hidden") pause("Recording paused because this screen was hidden. Return here and tap Resume run; background distance is not recorded.");
    });
    global.addEventListener("pagehide", () => pause("Recording paused while this page was away. Tap Resume run to continue."));

    return { start, pause, resume, finish, close, isActive: () => active, snapshot: recorder.snapshot };
  }

  global.RunlineRun = { create, createRecorder, metersBetween };
})(window);
