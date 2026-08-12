const driveStatus = document.querySelector("#drive-status");
const connectForm = document.querySelector("#connect-form");
const authorization = document.querySelector("#authorization");
const connected = document.querySelector("#connected");
let authorizationTimer = null;

const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

const fmtBytes = value => {
  if (value == null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let unit = 0, amount = value;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
  return `${amount.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
};
const fmtDate = value => value ? new Date(value).toLocaleString() : "—";

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 4000);
}

function statusClass(status) {
  if (["ERROR", "PARTIAL"].includes(status)) return "status error";
  if (["UPLOADING", "PAUSED", "LOCAL"].includes(status)) return "status warning";
  if (["DRIVE_VERIFIED", "LOCAL_REMOVED", "IDLE", "CONNECTED"].includes(status)) return "status";
  return "status neutral";
}

function render(data) {
  document.querySelector("#not-configured").hidden = data.configured;
  connectForm.hidden = !data.configured || data.connected || data.authorization_pending;
  authorization.hidden = !data.authorization_pending;
  connected.hidden = !data.connected;
  driveStatus.textContent = !data.configured ? "NOT CONFIGURED" : data.connected ? "CONNECTED" : data.authorization_pending ? "WAITING" : "NOT CONNECTED";
  driveStatus.className = data.connected ? "status" : data.authorization_pending ? "status warning" : "status neutral";
  if (data.authorization_pending && data.authorization) {
    const pendingLink = document.querySelector("#verification-link");
    pendingLink.textContent = data.authorization.verification_url || "";
    pendingLink.href = data.authorization.verification_url || "#";
    document.querySelector("#user-code").textContent = data.authorization.user_code || "";
    if (!authorizationTimer) {
      authorizationTimer = setInterval(pollAuthorization, Math.max(5, data.authorization.interval || 5) * 1000);
    }
  }

  document.querySelector("#connected-project").textContent = data.project_name || "—";
  document.querySelector("#cleanup-mode").textContent = data.auto_cleanup ? "Enabled after full verification" : "Manual";
  const driveLink = document.querySelector("#drive-link");
  driveLink.hidden = !data.project_web_link;
  if (data.project_web_link) driveLink.href = data.project_web_link;
  const pauseButton = document.querySelector("#pause-offload");
  pauseButton.textContent = data.paused ? "Resume uploads" : "Pause uploads";
  pauseButton.dataset.paused = data.paused ? "true" : "false";

  const service = data.service || {};
  const serviceState = service.status || (data.connected ? "WAITING" : "NOT CONNECTED");
  const serviceStatus = document.querySelector("#service-status");
  serviceStatus.textContent = serviceState.replaceAll("_", " ");
  serviceStatus.className = statusClass(serviceState);
  const current = data.current || service.current;
  document.querySelector("#current-day").textContent = current?.day || "—";
  document.querySelector("#current-file").textContent = current?.relative_name || "—";
  document.querySelector("#current-progress").textContent = current?.size ? `${Math.round((current.upload_offset || 0) / current.size * 100)}%` : "—";
  document.querySelector("#offload-updated").textContent = fmtDate(service.updated_at);
  document.querySelector("#offload-error").textContent = service.error || current?.error || "";

  const days = data.days || service.days || [];
  document.querySelector("#offload-days").innerHTML = days.length ? days.map(day => `
    <article class="day-card">
      <div class="day-heading">
        <div><h2>${escapeHtml(day.day)}</h2><small>${fmtBytes(day.local_bytes)} registered</small></div>
        <span class="${statusClass(day.status)}">${escapeHtml(day.status.replaceAll("_", " "))}</span>
      </div>
      <div class="day-components">
        <div><span>Files verified</span><strong>${day.verified_files} of ${day.expected_files}</strong><small>Includes the daily manifest</small></div>
        <div><span>Google Drive</span><strong>${day.verified_at ? "Verified" : "Pending"}</strong><small>${fmtDate(day.verified_at)}</small></div>
        <div><span>Local video</span><strong>${day.cleanup_at ? "Removed" : "Preserved"}</strong><small>${day.cleanup_at ? fmtDate(day.cleanup_at) : "Waiting for verification"}</small></div>
      </div>
      ${day.error ? `<p class="day-warning">${escapeHtml(day.error)}</p>` : ""}
    </article>`).join("") : '<p class="empty">No days registered yet.</p>';
}

async function refresh() {
  try {
    const response = await fetch("/api/offload/status");
    if (!response.ok) throw new Error(`Status ${response.status}`);
    render(await response.json());
  } catch (error) {
    showToast(`Offload status failed: ${error.message}`);
  }
}

connectForm.addEventListener("submit", async event => {
  event.preventDefault();
  const response = await fetch("/api/offload/connect", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      project_name: document.querySelector("#project-name").value,
      auto_cleanup: document.querySelector("#auto-cleanup").checked,
    }),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) return showToast(result.error || `Status ${response.status}`);
  document.querySelector("#verification-link").textContent = result.verification_url;
  document.querySelector("#verification-link").href = result.verification_url;
  document.querySelector("#user-code").textContent = result.user_code;
  authorization.hidden = false;
  connectForm.hidden = true;
  clearInterval(authorizationTimer);
  authorizationTimer = setInterval(pollAuthorization, Math.max(5, result.interval || 5) * 1000);
});

async function pollAuthorization() {
  const response = await fetch("/api/offload/connect/status", {method: "POST"});
  const result = await response.json();
  if (response.status === 202) return;
  clearInterval(authorizationTimer);
  authorizationTimer = null;
  if (!response.ok || !result.ok) {
    document.querySelector("#authorization-status").textContent = result.error || "Authorization failed.";
    return;
  }
  showToast("Google Drive connected and project folders created");
  await refresh();
}

document.querySelector("#pause-offload").addEventListener("click", async event => {
  const paused = event.currentTarget.dataset.paused !== "true";
  const response = await fetch("/api/offload/pause", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({paused}),
  });
  if (response.ok) await refresh();
});

document.querySelector("#disconnect-drive").addEventListener("click", async () => {
  if (!confirm("Disconnect Google Drive? Previously uploaded files will remain in Drive.")) return;
  const response = await fetch("/api/offload/disconnect", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({confirm: "DISCONNECT"}),
  });
  if (response.ok) {
    showToast("Google Drive disconnected");
    await refresh();
  }
});

refresh();
setInterval(refresh, 5000);
