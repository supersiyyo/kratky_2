const review = JSON.parse(document.querySelector("#review-data").textContent);
const video = document.querySelector("#review-video");
const recordedTime = document.querySelector("#recorded-time");
const sensorTime = document.querySelector("#sensor-time");
const sensorStatus = document.querySelector("#sensor-status");
const samples = review.samples || [];
const firstFrameMs = Date.parse(review.first_frame_at);
const maximumAgeMs = (review.approximate ? 65 : 3) * 1000;

const fields = {
  environment: {
    air_temperature_f: ["Air temperature", "°F"],
    relative_humidity_percent: ["Humidity", "%"],
    co2_ppm: ["CO₂", "ppm"],
    light_lux: ["Light", "lx"],
  },
  water: {
    temperature_c: ["Water temperature", "°C"],
    ph: ["pH", ""],
    electrical_conductivity_us_cm: ["Conductivity", "µS/cm"],
    moisture_percent: ["Moisture", "%"],
    nitrogen_mg_kg: ["Nitrogen", "mg/kg"],
    phosphorus_mg_kg: ["Phosphorus", "mg/kg"],
    potassium_mg_kg: ["Potassium", "mg/kg"],
  },
};

const clockFormatter = new Intl.DateTimeFormat([], {
  timeZone: review.timezone,
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
});

function sampleAt(timestampMs) {
  let low = 0;
  let high = samples.length - 1;
  let match = null;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const sampleMs = Date.parse(samples[middle].timestamp);
    if (sampleMs <= timestampMs) {
      match = samples[middle];
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  if (!match || timestampMs - Date.parse(match.timestamp) > maximumAgeMs) return null;
  return match;
}

function renderSection(name, sample) {
  const root = document.querySelector(`#review-${name}`);
  const values = sample?.[name]?.values || {};
  root.innerHTML = Object.entries(fields[name]).map(([key, [label, unit]]) => {
    const value = values[key];
    const shown = typeof value === "number" ? Math.round(value * 10) / 10 : "Unavailable";
    return `<div class="sensor-value"><span>${label}</span><strong>${shown}${value == null || !unit ? "" : ` ${unit}`}</strong></div>`;
  }).join("");
}

function render() {
  const timestampMs = firstFrameMs + video.currentTime * 1000;
  recordedTime.textContent = clockFormatter.format(new Date(timestampMs));
  const sample = sampleAt(timestampMs);
  renderSection("environment", sample);
  renderSection("water", sample);
  if (!sample) {
    sensorTime.textContent = "No sensor reading is available for this moment.";
    sensorStatus.textContent = "UNAVAILABLE";
    sensorStatus.className = "status error";
    return;
  }
  const sampleDate = new Date(sample.timestamp);
  sensorTime.textContent = `Sensor sample: ${clockFormatter.format(sampleDate)}`;
  sensorStatus.textContent = sample.status || "OK";
  sensorStatus.className = sample.status === "OK" ? "status" : "status warning";
}

for (const event of ["loadedmetadata", "timeupdate", "seeked", "pause"]) {
  video.addEventListener(event, render);
}
render();
