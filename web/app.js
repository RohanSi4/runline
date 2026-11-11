const state = {
  surface: "mixed",
  elevation: "balanced",
  drive: 0,
  routes: [],
  layers: [],
  casing: null,
  selected: 0,
  startMarker: null,
  request: null,
  loading: false,
  mode: "loop",
  mapTarget: "start",
  finishMarker: null,
};
let runSession = null;
const runIsActive = () => Boolean(runSession?.isActive());

const routeColors = ["#d9f977", "#78b8ff", "#ff9f85", "#c3a6ff", "#7fe1cb"];
// Open on the prefilled start rather than a hardcoded point, so the map always
// shows the area the app actually covers.
const startLatitude = Number(document.getElementById("latitude").value) || 38.0293;
const startLongitude = Number(document.getElementById("longitude").value) || -78.4767;
const map = L.map("map", { zoomControl: false }).setView(
  [startLatitude, startLongitude],
  13,
);
L.control.zoom({ position: "topright" }).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const form = document.getElementById("route-form");
const address = document.getElementById("address");
const latitude = document.getElementById("latitude");
const longitude = document.getElementById("longitude");
const distanceInput = document.getElementById("distance");
const status = document.getElementById("form-status");
const generateButton = form.querySelector(".generate");
const suggestionList = document.getElementById("address-suggestions");
const destinationAddress = document.getElementById("destination-address");
const destinationLatitude = document.getElementById("destination-latitude");
const destinationLongitude = document.getElementById("destination-longitude");

// Autocomplete is served from the shipped map rather than a geocoder:
// Nominatim's usage policy forbids per-keystroke lookups. Picking a suggestion
// carries its own coordinates, so the request skips geocoding entirely.
const MAX_SUGGESTIONS = 8;
let places = [];
let matches = [];
let activeIndex = -1;

function closeSuggestions() {
  suggestionList.hidden = true;
  suggestionList.replaceChildren();
  address.setAttribute("aria-expanded", "false");
  matches = [];
  activeIndex = -1;
  address.removeAttribute("aria-activedescendant");
}

function findMatches(query) {
  const needle = query.trim().toLowerCase();
  if (needle.length < 2 || !places.length) return [];
  const prefix = [];
  const inner = [];
  for (const place of places) {
    const position = place.name.toLowerCase().indexOf(needle);
    if (position === 0) prefix.push(place);
    else if (position > 0 && inner.length < MAX_SUGGESTIONS) inner.push(place);
    if (prefix.length >= MAX_SUGGESTIONS) break;
  }
  return prefix.concat(inner).slice(0, MAX_SUGGESTIONS);
}

function highlight(index) {
  activeIndex = index;
  if (index >= 0) address.setAttribute("aria-activedescendant", `place-option-${index}`);
  else address.removeAttribute("aria-activedescendant");
  suggestionList.querySelectorAll("li[role='option']").forEach((item, itemIndex) => {
    const selected = itemIndex === index;
    item.setAttribute("aria-selected", selected ? "true" : "false");
    if (selected) item.scrollIntoView({ block: "nearest" });
  });
}

function chooseSuggestion(index) {
  if (runIsActive()) return;
  const place = matches[index];
  if (!place) return;
  closeSuggestions();
  invalidateRoutes();
  setStart(place.latitude, place.longitude, place.name);
  document.getElementById("map-tip").hidden = true;
}

function renderSuggestions(query) {
  matches = findMatches(query);
  suggestionList.replaceChildren();
  if (!matches.length) {
    closeSuggestions();
    return;
  }
  matches.forEach((place, index) => {
    const item = document.createElement("li");
    item.id = `place-option-${index}`;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", "false");
    const name = document.createElement("span");
    name.textContent = place.name;
    const kind = document.createElement("span");
    kind.className = "place-kind";
    kind.textContent = place.kind === "street" ? "" : place.kind;
    item.append(name, kind);
    // mousedown, not click: blur would tear the list down before click fires.
    item.addEventListener("mousedown", (event) => {
      event.preventDefault();
      chooseSuggestion(index);
    });
    suggestionList.appendChild(item);
  });
  suggestionList.hidden = false;
  address.setAttribute("aria-expanded", "true");
  highlight(-1);
}

async function loadPlaces() {
  try {
    const response = await fetch("/api/places");
    if (!response.ok) return;
    places = (await response.json()).places || [];
  } catch (_error) {
    // Typing a full address still works; it is geocoded server-side.
  }
}

function setStart(latitudeValue, longitudeValue, label = "Pinned location") {
  latitude.value = latitudeValue;
  longitude.value = longitudeValue;
  address.value = label;
  if (state.startMarker) state.startMarker.remove();
  state.startMarker = L.marker([latitudeValue, longitudeValue], {
    icon: L.divIcon({ className: "", html: state.mode === "point_to_point" ? '<div class="start-pin">A</div>' : '<div class="start-pin"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }),
  }).addTo(map);
  map.panTo([latitudeValue, longitudeValue]);
}

map.on("click", (event) => {
  if (runIsActive()) return;
  invalidateRoutes();
  if (state.mode === "point_to_point" && state.mapTarget === "finish") {
    setFinish(event.latlng.lat.toFixed(6), event.latlng.lng.toFixed(6));
  } else {
    setStart(event.latlng.lat.toFixed(6), event.latlng.lng.toFixed(6));
  }
  document.getElementById("map-tip").hidden = true;
});

address.addEventListener("input", () => {
  invalidateRoutes();
  latitude.value = "";
  longitude.value = "";
  renderSuggestions(address.value);
});

address.addEventListener("keydown", (event) => {
  if (suggestionList.hidden || !matches.length) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    highlight((activeIndex + 1) % matches.length);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    highlight(activeIndex <= 0 ? matches.length - 1 : activeIndex - 1);
  } else if (event.key === "Enter" && activeIndex >= 0) {
    // Take the highlighted suggestion rather than submitting the form.
    event.preventDefault();
    chooseSuggestion(activeIndex);
  } else if (event.key === "Escape") {
    closeSuggestions();
  }
});

address.addEventListener("blur", closeSuggestions);
address.addEventListener("focus", () => {
  if (address.value.trim().length >= 2) renderSuggestions(address.value);
});

document.getElementById("locate").addEventListener("click", () => {
  if (runIsActive()) return;
  if (!navigator.geolocation) {
    showStatus("Location is not available in this browser.", true);
    return;
  }
  showStatus("Finding your location…");
  navigator.geolocation.getCurrentPosition(
    (position) => {
      invalidateRoutes();
      setStart(position.coords.latitude.toFixed(6), position.coords.longitude.toFixed(6), "My location");
      showStatus("");
    },
    () => showStatus("Could not access your location.", true),
    { enableHighAccuracy: true, timeout: 8000 },
  );
});

document.querySelectorAll(".segmented").forEach((group) => {
  group.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || runIsActive()) return;
    group.querySelectorAll("button").forEach((item) => setPressed(item, item === button));
    const value = group.dataset.control === "drive" ? Number(button.dataset.value) : button.dataset.value;
    if (state[group.dataset.control] !== value) invalidateRoutes();
    state[group.dataset.control] = value;
  });
});

document.querySelectorAll("[data-distance]").forEach((button) => {
  button.addEventListener("click", () => {
    if (runIsActive()) return;
    if (distanceInput.value !== button.dataset.distance) invalidateRoutes();
    distanceInput.value = button.dataset.distance;
    document.querySelectorAll("[data-distance]").forEach((item) => setPressed(item, item === button));
  });
});

distanceInput.addEventListener("input", () => {
  invalidateRoutes();
  document.querySelectorAll("[data-distance]").forEach((button) => setPressed(button, button.dataset.distance === distanceInput.value));
});

function setPressed(button, pressed) {
  button.classList.toggle("active", pressed);
  button.setAttribute("aria-pressed", pressed ? "true" : "false");
}

function showStatus(message, isError = false) {
  status.textContent = message;
  status.classList.toggle("error", isError);
}

function optionalText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function setOptionalHidden(id, hidden) {
  const element = document.getElementById(id);
  if (element) element.hidden = hidden;
}

function clearRoutes() {
  state.layers.forEach((layer) => layer.remove());
  state.layers = [];
  state.routes = [];
  if (state.casing) state.casing.remove();
  state.casing = null;
}

function invalidateRoutes() {
  clearRoutes();
  state.request = null;
  document.body.classList.remove("has-routes");
  ["results", "selected-summary"].forEach((id) => setOptionalHidden(id, true));
  optionalText("route-count", "Routes");
  optionalText("request-summary", "");
  if (!state.loading) showStatus("");
}

function routeMetrics(route) {
  return route.feature.properties;
}

function requestSummary(request) {
  if (!request) return "Choose a route. Make it yours.";
  const surface = { road: "Road", mixed: "Mixed", trail: "Trail" }[request.surface] || "Mixed";
  const elevation = { flat: "Flat", balanced: "Balanced", hilly: "Hilly" }[request.elevation] || "Balanced";
  if (request.mode === "point_to_point") return `Start → finish · ${surface}`;
  return `${request.distance_miles} mi · ${surface} · ${elevation}`;
}

function selectRoute(index, fit = true) {
  if (runIsActive()) return;
  if (!state.routes[index]) return;
  state.selected = index;
  state.layers.forEach((layer, layerIndex) => {
    layer.setStyle({ opacity: layerIndex === index ? 1 : 0.5, weight: layerIndex === index ? 6 : 4 });
  });
  // A dark casing keeps lime, blue and coral visible on both basemap styles.
  if (state.casing) state.casing.remove();
  state.casing = L.polyline(state.layers[index].getLatLngs(), {
    color: "#071810", weight: 11, opacity: 0.9, className: "route-casing", interactive: false,
  }).addTo(map);
  state.layers[index].bringToFront();
  document.querySelectorAll(".route-card").forEach((card, cardIndex) => {
    const selected = cardIndex === index;
    card.classList.toggle("active", selected);
    card.setAttribute("aria-pressed", String(selected));
  });
  const metrics = routeMetrics(state.routes[index]);
  optionalText("summary-distance", `${metrics.distance_miles.toFixed(2)} mi`);
  optionalText("summary-start", metrics.start?.label || "Start here");
  optionalText("summary-gain", metrics.elevation_coverage >= 0.95 ? `${Math.round(metrics.elevation_gain_feet)} ft` : "—");
  optionalText("summary-signals", String(metrics.traffic_signal_events));
  setOptionalHidden("selected-summary", false);
  if (fit) map.fitBounds(state.layers[index].getBounds().pad(0.12));
}

function validateRoutes(payload, request = null) {
  if (!payload || !Array.isArray(payload.routes) || !payload.routes.length) {
    throw new Error("No routes came back. Try another distance or starting point.");
  }
  if (payload.origin && (!Number.isFinite(payload.origin.latitude) || !Number.isFinite(payload.origin.longitude))) {
    throw new Error("The route start could not be read. Please try again.");
  }
  const direct = request?.mode === "point_to_point" || payload.mode === "point_to_point";
  if (direct && (!payload.destination || !Number.isFinite(payload.destination.latitude) || !Number.isFinite(payload.destination.longitude))) {
    throw new Error("The finish location could not be read. Please try again.");
  }
  for (const route of payload.routes) {
    const geometry = route?.feature?.geometry;
    const points = geometry?.coordinates;
    const metrics = route?.feature?.properties;
    if (geometry?.type !== "LineString" || !Array.isArray(points) || points.length < (direct ? 2 : 4) ||
        !points.every((point) => Array.isArray(point) && point.length >= 2 && Number.isFinite(point[0]) && Number.isFinite(point[1]) && Math.abs(point[0]) <= 180 && Math.abs(point[1]) <= 90) ||
        (!direct && (points[0][0] !== points.at(-1)[0] || points[0][1] !== points.at(-1)[1])) ||
        (direct && (points[0][0] === points.at(-1)[0] && points[0][1] === points.at(-1)[1])) ||
        (direct && (points.at(-1)[0] !== payload.destination.longitude || points.at(-1)[1] !== payload.destination.latitude)) ||
        !metrics || !Number.isFinite(metrics.distance_miles) || metrics.distance_miles <= 0 ||
        !["traffic_signal_events", "major_crossing_events", "elevation_gain_feet"].every((key) => Number.isFinite(metrics[key]) && metrics[key] >= 0) ||
        !["trail_fraction", "repeated_fraction", "elevation_coverage"].every((key) => Number.isFinite(metrics[key]) && metrics[key] >= 0 && metrics[key] <= 1) ||
        typeof route?.gpx !== "string" || !route.gpx.trim()) {
      throw new Error("The route data could not be read. Please generate routes again.");
    }
  }
}

function renderRoutes(payload, request = null) {
  validateRoutes(payload, request);
  clearRoutes();
  if (payload.origin) {
    setStart(payload.origin.latitude, payload.origin.longitude, address.value || "Start here");
  }
  if (payload.destination && state.mode === "point_to_point") {
    setFinish(payload.destination.latitude, payload.destination.longitude, destinationAddress.value || "Finish here");
  }
  state.routes = payload.routes;
  state.request = request;
  const routeList = document.getElementById("route-list");
  routeList.replaceChildren();
  const allBounds = L.latLngBounds();

  state.routes.forEach((route, index) => {
    const color = routeColors[index % routeColors.length];
    const coordinates = route.feature.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
    const layer = L.polyline(coordinates, { color, weight: index === 0 ? 6 : 4, opacity: index === 0 ? 1 : 0.5, className: "route-line" }).addTo(map);
    layer.on("click", () => selectRoute(index));
    state.layers.push(layer);
    allBounds.extend(layer.getBounds());

    const metrics = routeMetrics(route);
    const card = document.createElement("button");
    card.type = "button";
    card.className = `route-card${index === 0 ? " active" : ""}`;
    card.setAttribute("aria-pressed", String(index === 0));
    card.setAttribute("aria-label", `Route ${index + 1}, ${metrics.distance_miles.toFixed(2)} miles, ${metrics.traffic_signal_events} signals, ${Math.round(metrics.trail_fraction * 100)} percent trail`);
    card.style.setProperty("--route-color", color);
    card.style.setProperty("--i", index);

    const swatch = document.createElement("span");
    swatch.className = "route-swatch";
    swatch.setAttribute("aria-hidden", "true");
    const main = document.createElement("span");
    main.className = "route-main";
    const head = document.createElement("span");
    head.className = "route-card-head";
    const option = document.createElement("span");
    option.className = "route-option";
    option.textContent = `Route ${String(index + 1).padStart(2, "0")}`;
    head.append(option);
    if (index === 0) {
      const badge = document.createElement("span");
      badge.className = "route-badge";
      badge.textContent = "Top match";
      head.append(badge);
    }
    const title = document.createElement("strong");
    title.textContent = metrics.start?.label || "Starting here";
    main.append(head, title);
    const routeDistance = document.createElement("span");
    routeDistance.className = "route-distance";
    const distanceValue = document.createElement("strong");
    distanceValue.textContent = metrics.distance_miles.toFixed(2);
    const unit = document.createElement("small");
    unit.textContent = "mi";
    routeDistance.append(distanceValue, unit);
    const detail = document.createElement("span");
    detail.className = "route-metrics";
    const gain = metrics.elevation_coverage >= 0.95 ? `${Math.round(metrics.elevation_gain_feet)} ft` : "—";
    for (const [value, label] of [[String(metrics.traffic_signal_events), "signals"], [gain, "climbing"], [`${Math.round(metrics.trail_fraction * 100)}%`, "trail"]]) {
      const metric = document.createElement("span");
      metric.className = "route-metric";
      const metricValue = document.createElement("strong");
      metricValue.textContent = value;
      const metricLabel = document.createElement("span");
      metricLabel.className = "route-metric-label";
      metricLabel.textContent = label;
      metric.append(metricValue, metricLabel);
      detail.append(metric);
    }
    card.append(swatch, main, routeDistance, detail);
    card.addEventListener("click", () => selectRoute(index));
    routeList.appendChild(card);
  });

  document.body.classList.add("has-routes");
  map.fitBounds(allBounds.pad(0.1));
  setOptionalHidden("results", false);
  optionalText("result-context", payload.discovered_starts ? `${payload.discovered_starts} nearby starts checked` : "Starting here");
  optionalText("route-count", `${state.routes.length} route${state.routes.length === 1 ? "" : "s"}`);
  optionalText("request-summary", requestSummary(request));
  selectRoute(0, false);
  const results = document.getElementById("results");
  // Reveal the cards within the desktop planner without moving the mobile
  // viewport away from the map. A dedicated button takes mobile users there.
  if (window.matchMedia?.("(min-width: 781px)").matches) {
    results?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "nearest" });
  }
}

function fitRoutes() {
  if (state.layers.length) {
    const bounds = L.latLngBounds();
    state.layers.forEach((layer) => bounds.extend(layer.getBounds()));
    map.fitBounds(bounds.pad(0.12));
  } else {
    const currentLatitude = Number(latitude.value);
    const currentLongitude = Number(longitude.value);
    const hasStart = latitude.value !== "" && longitude.value !== "" && Number.isFinite(currentLatitude) && Number.isFinite(currentLongitude);
    map.setView(hasStart ? [currentLatitude, currentLongitude] : [startLatitude, startLongitude], 13);
  }
}

function toggleMapStyle() {
  const light = document.body.classList.toggle("light-map");
  const button = document.getElementById("map-style");
  button?.setAttribute("aria-pressed", String(light));
  button?.setAttribute("aria-label", light ? "Use dark map" : "Use light map");
  button?.setAttribute("title", light ? "Use dark map" : "Use light map");
}

function jumpToMap() {
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  document.getElementById("map")?.scrollIntoView({ behavior: reduced ? "instant" : "smooth", block: "start" });
  map.invalidateSize();
}

// FastAPI sends a string for errors the API raises, but a list of
// {loc, msg, type} objects when the request fails validation.
function errorMessage(detail) {
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => (item && item.msg) || String(item)).filter(Boolean);
    if (messages.length) return messages.join(" ");
  } else if (typeof detail === "string" && detail) {
    return detail;
  }
  return "Could not generate routes.";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.loading || runIsActive()) return;
  state.loading = true;
  document.body.classList.add("is-generating");
  generateButton.disabled = true;
  generateButton.setAttribute("aria-busy", "true");
  generateButton.querySelector("span").textContent = "Exploring loops…";
  showStatus("Reading streets, trails, crossings, and elevation…");
  const submittedStart = [address.value, latitude.value, longitude.value];
  const submittedPreferences = requestIntent();
  const body = {
    mode: state.mode,
    ...(state.mode === "loop" ? { distance_miles: Number(distanceInput.value) } : {}),
    drive_radius_miles: state.mode === "loop" ? state.drive : 0,
    surface: state.surface,
    elevation: state.mode === "loop" ? state.elevation : "balanced",
    result_count: 3,
  };
  if (latitude.value && longitude.value) {
    body.latitude = Number(latitude.value);
    body.longitude = Number(longitude.value);
  } else {
    body.address = address.value.trim();
  }
  if (state.mode === "point_to_point") {
    body.destination = destinationLatitude.value && destinationLongitude.value
      ? { latitude: Number(destinationLatitude.value), longitude: Number(destinationLongitude.value) }
      : { address: destinationAddress.value.trim() };
  }

  try {
    const response = await fetch("/api/routes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // A platform timeout or 502 returns HTML, not the API's JSON error body.
    const payload = await response.json().catch(() => ({
      detail: `Server error (${response.status}). Try again shortly.`,
    }));
    const currentStart = [address.value, latitude.value, longitude.value];
    if (submittedStart.some((value, index) => value !== currentStart[index])) {
      showStatus("Start changed. Generate routes for the new location.");
      return;
    }
    const currentPreferences = requestIntent();
    if (submittedPreferences.some((value, index) => value !== currentPreferences[index])) {
      showStatus("Preferences changed. Generate routes for the new settings.");
      return;
    }
    if (!response.ok) throw new Error(errorMessage(payload?.detail));
    renderRoutes(payload, body);
    showStatus(Array.isArray(payload.warnings) ? payload.warnings.join(" ") : "");
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    state.loading = false;
    document.body.classList.remove("is-generating");
    generateButton.disabled = false;
    generateButton.removeAttribute("aria-busy");
    generateButton.querySelector("span").textContent = "Generate routes";
  }
});

document.getElementById("download-gpx").addEventListener("click", () => {
  if (runIsActive()) return;
  const route = state.routes[state.selected];
  if (!route) return;
  const blob = new Blob([route.gpx], { type: "application/gpx+xml" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `runline-option-${state.selected + 1}.gpx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoke after the browser has started the download, not in the same turn.
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});

async function loadCoverage() {
  try {
    const response = await fetch("/api/areas");
    if (!response.ok) return;
    const { areas } = await response.json();
    if (!areas || !areas.length) return;
    const hint = document.getElementById("coverage-hint");
    hint.textContent = areas.join(" · ");
    hint.hidden = false;
  } catch (_error) {
    // The hint is decorative; failing to load it must not block the form.
  }
}

document.getElementById("reset-view")?.addEventListener("click", fitRoutes);
document.getElementById("map-style")?.addEventListener("click", toggleMapStyle);
document.getElementById("mobile-map")?.addEventListener("click", jumpToMap);

function requestIntent() {
  return [state.mode, distanceInput.value, state.surface, state.elevation, state.drive,
    ...(state.mode === "point_to_point" ? [destinationAddress.value, destinationLatitude.value, destinationLongitude.value] : [])];
}

function setFinish(lat, lon, label = "Finish on map") {
  destinationLatitude.value = lat;
  destinationLongitude.value = lon;
  destinationAddress.value = label;
  if (state.finishMarker) state.finishMarker.remove();
  state.finishMarker = L.marker([lat, lon], {
    icon: L.divIcon({ className: "", html: '<div class="finish-pin">B</div>', iconSize: [24, 24], iconAnchor: [12, 12] }),
  }).addTo(map);
  map.panTo([lat, lon]);
}

function pickEndpoint(target) {
  if (runIsActive()) return;
  state.mapTarget = target;
  document.getElementById("pick-start")?.setAttribute("aria-pressed", String(target === "start"));
  document.getElementById("pick-finish")?.setAttribute("aria-pressed", String(target === "finish"));
  showStatus(`Click the map to set your ${target}.`);
  const tip = document.getElementById("map-tip");
  if (tip) { tip.textContent = `Click map to set ${target}`; tip.hidden = false; }
  if (window.matchMedia?.("(max-width: 780px)").matches) jumpToMap();
}

function setRouteMode(mode) {
  if (runIsActive() || state.mode === mode) return;
  state.mode = mode;
  const direct = mode === "point_to_point";
  invalidateRoutes();
  ["destination-field", "map-pick-controls"].forEach((id) => setOptionalHidden(id, !direct));
  ["distance-controls", "elevation-controls", "drive-controls"].forEach((id) => setOptionalHidden(id, direct));
  distanceInput.disabled = direct;
  distanceInput.required = !direct;
  destinationAddress.required = direct;
  setOptionalHidden("mode-hint", !direct);
  document.body.classList.toggle("point-to-point", direct);
  if (state.finishMarker) state.finishMarker.remove();
  state.finishMarker = null;
  if (direct && destinationLatitude.value && destinationLongitude.value) {
    setFinish(destinationLatitude.value, destinationLongitude.value, destinationAddress.value);
  }
  if (latitude.value && longitude.value) setStart(latitude.value, longitude.value, address.value);
  if (direct) pickEndpoint("finish");
}

document.getElementById("mode-loop")?.addEventListener("change", (event) => {
  if (event.target.checked) setRouteMode("loop");
});
document.getElementById("mode-point-to-point")?.addEventListener("change", (event) => {
  if (event.target.checked) setRouteMode("point_to_point");
});
document.getElementById("pick-start")?.addEventListener("click", () => pickEndpoint("start"));
document.getElementById("pick-finish")?.addEventListener("click", () => pickEndpoint("finish"));

// A second independent combobox shares the shipped place catalog, never a
// per-keystroke geocoder. Choosing a place carries its map coordinates.
if (destinationAddress) {
  const list = document.getElementById("destination-suggestions");
  let choices = [], selected = -1;
  const close = () => {
    list.hidden = true;
    list.replaceChildren();
    destinationAddress.setAttribute("aria-expanded", "false");
    destinationAddress.removeAttribute("aria-activedescendant");
    choices = []; selected = -1;
  };
  const choose = (index) => {
    if (!choices[index] || runIsActive()) return;
    const place = choices[index];
    close(); invalidateRoutes();
    setFinish(place.latitude, place.longitude, place.name);
  };
  const render = () => {
    choices = findMatches(destinationAddress.value);
    list.replaceChildren(); selected = -1;
    if (!choices.length) { close(); return; }
    choices.forEach((place, index) => {
      const item = document.createElement("li");
      item.id = `finish-option-${index}`;
      item.setAttribute("role", "option"); item.setAttribute("aria-selected", "false");
      item.textContent = place.name;
      item.addEventListener("mousedown", (event) => { event.preventDefault(); choose(index); });
      list.appendChild(item);
    });
    list.hidden = false;
    destinationAddress.setAttribute("aria-expanded", "true");
  };
  destinationAddress.addEventListener("input", () => {
    if (runIsActive()) return;
    invalidateRoutes();
    destinationLatitude.value = ""; destinationLongitude.value = "";
    if (state.finishMarker) state.finishMarker.remove();
    state.finishMarker = null;
    render();
  });
  destinationAddress.addEventListener("focus", render);
  destinationAddress.addEventListener("blur", close);
  destinationAddress.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { close(); return; }
    if (list.hidden || !choices.length) return;
    if (event.key === "Enter" && selected >= 0) { event.preventDefault(); choose(selected); return; }
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    selected = event.key === "ArrowDown" ? (selected + 1) % choices.length : selected <= 0 ? choices.length - 1 : selected - 1;
    destinationAddress.setAttribute("aria-activedescendant", `finish-option-${selected}`);
    list.querySelectorAll("li[role='option']").forEach((item, index) => {
      item.setAttribute("aria-selected", String(index === selected));
      if (index === selected) item.scrollIntoView({ block: "nearest" });
    });
  });
}

async function loadCapabilities() {
  try {
    const response = await fetch("/api/capabilities");
    if (!response.ok) return;
    const labels = (await response.json()).modes?.point_to_point?.areas;
    if (Array.isArray(labels) && labels.length) optionalText("mode-hint", `Start → finish: ${labels.join(" · ")}`);
  } catch (_error) { /* Existing coverage and loop planning remain usable. */ }
}

if (window.RunlineRun) {
  const disabledBeforeRun = new Map();
  runSession = window.RunlineRun.create({ map, L,
    getSelectedRoute: () => state.routes[state.selected],
    onActiveChange(active) {
      form.querySelectorAll("input, button").forEach((control) => {
        if (active) { disabledBeforeRun.set(control, control.disabled); control.disabled = true; }
        else if (disabledBeforeRun.has(control)) control.disabled = disabledBeforeRun.get(control);
      });
      if (!active) disabledBeforeRun.clear();
      requestAnimationFrame(() => map.invalidateSize());
    },
  });
  document.getElementById("start-run")?.addEventListener("click", () => {
    if (state.loading) { showStatus("Wait for route planning to finish before starting your run."); return; }
    runSession.start();
  });
}

// Show the prefilled start so the map opens with a visible origin.
setStart(startLatitude, startLongitude, address.value);

loadCoverage();
loadPlaces();
loadCapabilities();
