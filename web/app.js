const state = {
  surface: "mixed",
  elevation: "balanced",
  drive: 1,
  routes: [],
  layers: [],
  selected: 0,
  startMarker: null,
};

const routeColors = ["#e2552f", "#235f9f", "#7a4d96", "#2f785b", "#ad7628"];
const map = L.map("map", { zoomControl: false }).setView([38.9799, -77.5257], 13);
L.control.zoom({ position: "bottomright" }).addTo(map);
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

function setStart(latitudeValue, longitudeValue, label = "Pinned location") {
  latitude.value = latitudeValue;
  longitude.value = longitudeValue;
  address.value = label;
  if (state.startMarker) state.startMarker.remove();
  state.startMarker = L.marker([latitudeValue, longitudeValue], {
    icon: L.divIcon({ className: "", html: '<div class="start-pin"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }),
  }).addTo(map);
  map.panTo([latitudeValue, longitudeValue]);
}

map.on("click", (event) => {
  setStart(event.latlng.lat.toFixed(6), event.latlng.lng.toFixed(6));
  document.getElementById("map-tip").hidden = true;
});

address.addEventListener("input", () => {
  latitude.value = "";
  longitude.value = "";
});

document.getElementById("locate").addEventListener("click", () => {
  if (!navigator.geolocation) {
    showStatus("Location is not available in this browser.", true);
    return;
  }
  showStatus("Finding your location…");
  navigator.geolocation.getCurrentPosition(
    (position) => {
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
    if (!button) return;
    group.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    state[group.dataset.control] = group.dataset.control === "drive" ? Number(button.dataset.value) : button.dataset.value;
  });
});

document.querySelectorAll("[data-distance]").forEach((button) => {
  button.addEventListener("click", () => {
    distanceInput.value = button.dataset.distance;
    document.querySelectorAll("[data-distance]").forEach((item) => item.classList.toggle("active", item === button));
  });
});

distanceInput.addEventListener("input", () => {
  document.querySelectorAll("[data-distance]").forEach((button) => button.classList.toggle("active", button.dataset.distance === distanceInput.value));
});

function showStatus(message, isError = false) {
  status.textContent = message;
  status.classList.toggle("error", isError);
}

function clearRoutes() {
  state.layers.forEach((layer) => layer.remove());
  state.layers = [];
  state.routes = [];
}

function routeMetrics(route) {
  return route.feature.properties;
}

function selectRoute(index, fit = true) {
  state.selected = index;
  state.layers.forEach((layer, layerIndex) => {
    layer.setStyle({ opacity: layerIndex === index ? 1 : 0.25, weight: layerIndex === index ? 6 : 3 });
    if (layerIndex === index) layer.bringToFront();
  });
  document.querySelectorAll(".route-card").forEach((card, cardIndex) => card.classList.toggle("active", cardIndex === index));
  const route = state.routes[index];
  const metrics = routeMetrics(route);
  document.getElementById("summary-distance").textContent = `${metrics.distance_miles.toFixed(2)} mi`;
  document.getElementById("summary-start").textContent = metrics.start?.label || "Start here";
  document.getElementById("summary-gain").textContent = metrics.elevation_coverage >= 0.95 ? `${Math.round(metrics.elevation_gain_feet)} ft` : "—";
  document.getElementById("summary-signals").textContent = metrics.traffic_signal_events;
  document.getElementById("selected-summary").hidden = false;
  if (fit) map.fitBounds(state.layers[index].getBounds().pad(0.12));
}

function renderRoutes(payload) {
  clearRoutes();
  state.routes = payload.routes;
  const routeList = document.getElementById("route-list");
  routeList.replaceChildren();
  const allBounds = L.latLngBounds();

  state.routes.forEach((route, index) => {
    const coordinates = route.feature.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
    const layer = L.polyline(coordinates, { color: routeColors[index], weight: index === 0 ? 6 : 3, opacity: index === 0 ? 1 : 0.25 }).addTo(map);
    layer.on("click", () => selectRoute(index));
    state.layers.push(layer);
    allBounds.extend(layer.getBounds());

    const metrics = routeMetrics(route);
    const card = document.createElement("button");
    card.type = "button";
    card.className = `route-card${index === 0 ? " active" : ""}`;
    card.style.setProperty("--route-color", routeColors[index]);
    const swatch = document.createElement("span");
    swatch.className = "route-swatch";
    const main = document.createElement("span");
    main.className = "route-main";
    const title = document.createElement("strong");
    title.textContent = metrics.start?.label || `Option ${index + 1}`;
    const details = document.createElement("span");
    const gain = metrics.elevation_coverage >= 0.95 ? `${Math.round(metrics.elevation_gain_feet)} ft` : "elevation —";
    details.textContent = `${gain} · ${metrics.traffic_signal_events} signals · ${Math.round(metrics.trail_fraction * 100)}% trail`;
    main.append(title, details);
    const routeDistance = document.createElement("span");
    routeDistance.className = "route-distance";
    routeDistance.textContent = `${metrics.distance_miles.toFixed(2)} mi`;
    card.append(swatch, main, routeDistance);
    card.addEventListener("click", () => selectRoute(index));
    routeList.appendChild(card);
  });

  map.fitBounds(allBounds.pad(0.1));
  document.getElementById("results").hidden = false;
  document.getElementById("result-context").textContent = payload.discovered_starts ? `${payload.discovered_starts} nearby starts checked` : "Starting here";
  selectRoute(0, false);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  generateButton.disabled = true;
  generateButton.querySelector("span").textContent = "Exploring loops…";
  showStatus("Reading streets, trails, crossings, and elevation…");
  const body = {
    distance_miles: Number(distanceInput.value),
    drive_radius_miles: state.drive,
    surface: state.surface,
    elevation: state.elevation,
    result_count: 3,
  };
  if (latitude.value && longitude.value) {
    body.latitude = Number(latitude.value);
    body.longitude = Number(longitude.value);
  } else {
    body.address = address.value.trim();
  }

  try {
    const response = await fetch("/api/routes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Could not generate routes.");
    renderRoutes(payload);
    showStatus(payload.warnings?.[0] || "");
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    generateButton.disabled = false;
    generateButton.querySelector("span").textContent = "Generate routes";
  }
});

document.getElementById("download-gpx").addEventListener("click", () => {
  const route = state.routes[state.selected];
  if (!route) return;
  const blob = new Blob([route.gpx], { type: "application/gpx+xml" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `runline-option-${state.selected + 1}.gpx`;
  link.click();
  URL.revokeObjectURL(link.href);
});

async function loadCoverage() {
  try {
    const response = await fetch("/api/areas");
    if (!response.ok) return;
    const { areas } = await response.json();
    if (!areas || !areas.length) return;
    const hint = document.getElementById("coverage-hint");
    hint.textContent = `Map data available for ${areas.join(" · ")}`;
    hint.hidden = false;
  } catch (_error) {
    // The hint is decorative; failing to load it must not block the form.
  }
}

async function loadDemo() {
  try {
    const response = await fetch("/api/demo");
    if (!response.ok) return;
    renderRoutes(await response.json());
  } catch (_error) {
    // The planner remains fully usable when no precomputed demo is present.
  }
}

loadCoverage();
loadDemo();
