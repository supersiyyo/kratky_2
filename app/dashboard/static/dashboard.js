const labels = {
  air_temperature_f: ["Air temperature", "°F"],
  relative_humidity_percent: ["Humidity", "%"],
  co2_ppm: ["CO₂", "ppm"],
  light_lux: ["Light", "lx"],
  temperature_c: ["Water temperature", "°C"],
  ph: ["pH", ""],
  electrical_conductivity_us_cm: ["Conductivity", "µS/cm"],
  moisture_percent: ["Moisture", "%"],
  nitrogen_mg_kg: ["Nitrogen", "mg/kg"],
  phosphorus_mg_kg: ["Phosphorus", "mg/kg"],
  potassium_mg_kg: ["Potassium", "mg/kg"],
};

const fmtBytes = value => {
  if (value == null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let unit = 0, amount = value;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
  return `${amount.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
};
const fmtDate = value => value ? new Date(value).toLocaleString() : "—";
const basename = value => value ? value.split(/[\\/]/).pop() : "—";

function statusClass(value) {
  if (["ERROR", "STALE"].includes(value)) return "status error";
  if (["STARTING", "RECONNECTING", "PAUSED"].includes(value)) return "status warning";
  if (value === "PLANNED") return "status neutral";
  return "status";
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 3500);
}

function renderSensors(camera, sensors) {
  const section = camera === "water" ? sensors.water : sensors.environment;
  const root = document.querySelector(`[data-sensors="${camera}"]`);
  if (!root || !section?.values) return;
  root.innerHTML = Object.entries(section.values)
    .filter(([key, value]) => labels[key] && value != null)
    .map(([key, value]) => {
      const [label, unit] = labels[key];
      const shown = typeof value === "number" ? Math.round(value * 10) / 10 : value;
      return `<div class="sensor-value"><span>${label}</span><strong>${shown} ${unit}</strong></div>`;
    }).join("") || `<p>Sensor data ${section?.status?.toLowerCase() || "unavailable"}.</p>`;
}

function render(data) {
  document.querySelector("#version").textContent = `Version ${data.capture.version || "unknown"}`;
  Object.entries(data.capture.cameras || {}).forEach(([name, camera]) => {
    const card = document.querySelector(`[data-camera-card="${name}"]`);
    if (!card) return;
    for (const field of ["status", "last_frame_at", "current_recording", "reconnects", "last_gap_seconds", "last_error"]) {
      const node = card.querySelector(`[data-field="${field}"]`);
      if (!node) continue;
      const value = camera[field];
      node.textContent = field === "last_frame_at" ? fmtDate(value) :
        field === "current_recording" ? basename(value) :
        field === "last_gap_seconds" ? (value == null ? "—" : `${value.toFixed(1)} seconds`) :
        value ?? (field === "reconnects" ? "0" : "");
      if (field === "status") node.className = statusClass(value);
    }
    renderSensors(name, data.sensors);
  });

  const storage = data.capture.storage || {};
  document.querySelector("#storage").innerHTML = [
    ["Free space", fmtBytes(storage.free_bytes)],
    ["Safety reserve", fmtBytes(storage.reserve_bytes)],
    ["Recent daily write", fmtBytes(storage.recent_daily_bytes)],
    ["Measured retention", storage.estimated_retention_days == null ? "Measuring" : `${storage.estimated_retention_days.toFixed(1)} days`],
    ["Two-camera projection", storage.provisional_two_camera_days == null ? "Measuring" : `${storage.provisional_two_camera_days.toFixed(1)} days`],
  ].map(([key, value]) => `<div><dt>${key}</dt><dd>${value}</dd></div>`).join("");
  const health = document.querySelector("#storage-health");
  health.textContent = storage.reserve_reached ? "RESERVE REACHED" : storage.capacity_warning ? "CAPACITY WARNING" : storage.free_bytes ? "HEALTHY" : "MEASURING";
  health.className = storage.reserve_reached ? "status error" : storage.capacity_warning ? "status warning" : "status";

  const events = [...(data.capture.events || [])].reverse().slice(0, 12);
  document.querySelector("#events").innerHTML = events.length ? events.map(event =>
    `<li><strong>${event.camera} · ${event.kind}</strong><br>${event.detail}<br><small>${fmtDate(event.timestamp)}</small></li>`
  ).join("") : "<li>No reconnects or gaps reported.</li>";
}

async function refresh() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error(`Status ${response.status}`);
    render(await response.json());
    document.querySelectorAll("img[data-preview]").forEach(image => {
      image.src = `/preview/${image.dataset.preview}.jpg?t=${Date.now()}`;
      image.onload = () => image.nextElementSibling?.remove();
    });
  } catch (error) {
    showToast(`Dashboard update failed: ${error.message}`);
  }
}

document.querySelectorAll("button[data-action]").forEach(button => {
  button.addEventListener("click", async () => {
    const { action, camera } = button.dataset;
    if (!confirm(`${action[0].toUpperCase() + action.slice(1)} ${camera === "all" ? "all cameras" : camera + " camera"}?`)) return;
    button.disabled = true;
    try {
      const response = await fetch("/api/control", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action, camera}),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || `Status ${response.status}`);
      showToast(`${action} accepted for ${camera}`);
      await refresh();
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });
});

refresh();
setInterval(refresh, 1000);
