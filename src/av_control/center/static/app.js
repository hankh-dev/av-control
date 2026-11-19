"use strict";

// 관제 화면. 서버가 WebSocket으로 0.3초마다 보내는 현재 상태(snapshot)를 받아 그린다.
// 좌표는 SUMO 도로망 좌표(m)이고, Leaflet의 CRS.Simple에 [y, x]로 넣는다.

const COMMAND_LABEL = { stop: "정지", resume: "재출발", reroute: "목적지 변경", speed_limit: "속도 제한" };
const ALERT_STATE_LABEL = { open: "새 알림", acknowledged: "확인함" };
const ROAD_STYLE = { color: "#3b4757", weight: 5, opacity: 1 };
const CONGESTED_STYLE = { color: "#f85149", weight: 7, opacity: 1 };
const ICON_HTML = '<svg viewBox="-12 -12 24 24" aria-hidden="true"><path d="M0 -10 L7 8 L0 4 L-7 8 Z"/></svg>';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const toLatLng = ([x, y]) => [y, x];

function fmtTime(t) {
  if (t === null || t === undefined) return "-";
  const s = Math.floor(t);
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

const map = L.map("map", { crs: L.CRS.Simple, minZoom: -3, maxZoom: 3, zoomSnap: 0.25, attributionControl: false });
map.on("zoomstart", () => $("map").classList.add("zooming"));
map.on("zoomend", () => $("map").classList.remove("zooming"));

const roadLines = new Map(); // 도로 ID → [L.Polyline] (차로마다 하나)
const markers = new Map(); // 차량 ID → L.Marker
const routeLayer = L.layerGroup().addTo(map);

let snap = null; // 마지막으로 받은 상태
let selected = null; // 선택한 차량 ID
let picking = false; // 목적지 도로를 고르는 중
let congested = new Set();
let alertsHovered = false; // 알림 목록 위에 마우스가 있으면 목록을 움직이지 않는다
let routeCursor = { key: "", index: 0 }; // 선택한 차량이 경로의 몇 번째 도로에 있는지
const lastKeys = { route: "", alerts: "", fleet: "", commands: "" };

// --- 지도 ---

async function loadMap() {
  const m = await (await fetch("/api/map")).json();
  for (const j of m.junctions) {
    L.polygon(j.shape.map(toLatLng), {
      color: j.traffic_light ? "#9e7a1f" : ROAD_STYLE.color,
      weight: 1,
      fillColor: "#273140",
      fillOpacity: 1,
      interactive: false,
    }).addTo(map);
  }
  for (const road of m.roads) {
    const lines = road.lanes.map((lane) =>
      L.polyline(lane.map(toLatLng), ROAD_STYLE)
        .bindTooltip(road.id, { sticky: true, direction: "top" })
        .on("click", () => onRoadClick(road.id))
        .addTo(map),
    );
    roadLines.set(road.id, lines);
  }
  const [x0, y0, x1, y1] = m.bounds;
  map.fitBounds([[y0, x0], [y1, x1]], { padding: [24, 24] });
}

function newMarker(v) {
  const icon = L.divIcon({ className: "vehicle-marker", html: ICON_HTML, iconSize: [24, 24], iconAnchor: [12, 12] });
  const marker = L.marker(toLatLng([v.x, v.y]), { icon, keyboard: false, riseOnHover: true });
  marker.bindTooltip(v.id, { direction: "top", offset: [0, -12] });
  marker.on("click", () => select(v.id));
  return marker.addTo(map);
}

// --- 상태 그리기 ---

function vehicleClass(v, alerting) {
  if (v.state === "offline") return "lost";
  if (alerting.has(v.id)) return "alerting";
  if (v.held) return "held";
  return "ok";
}

function render(s) {
  snap = s;
  // 조건이 아직 풀리지 않은 알림이 있는 차량
  const alerting = new Set(s.alerts.filter((a) => a.t_cleared === null && a.type !== "congestion").map((a) => a.subject));

  const seen = new Set();
  for (const v of s.vehicles) {
    seen.add(v.id);
    let marker = markers.get(v.id);
    if (!marker) {
      marker = newMarker(v);
      markers.set(v.id, marker);
    } else {
      marker.setLatLng(toLatLng([v.x, v.y]));
    }
    const el = marker.getElement();
    if (el) {
      el.firstChild.style.transform = `rotate(${v.heading}deg)`; // SUMO 방향: 북쪽 0, 시계 방향
      const cls = vehicleClass(v, alerting);
      for (const name of ["lost", "alerting", "held"]) el.classList.toggle(name, cls === name);
      el.classList.toggle("selected", v.id === selected);
    }
  }
  for (const [id, marker] of markers) {
    if (!seen.has(id)) {
      marker.remove();
      markers.delete(id);
    }
  }
  if (selected && !seen.has(selected)) {
    selected = null; // 운행을 마친 차량
    stopPicking();
  }

  renderTopbar(s, alerting);
  renderCongestion(s);
  renderRoute();
  renderVehicle(alerting);
  renderAlerts(s);
  renderFleet(s, alerting);
  renderCommands(s);
}

function renderTopbar(s, alerting) {
  const classes = s.vehicles.map((v) => vehicleClass(v, alerting));
  $("clock").textContent = fmtTime(s.t);
  $("stat-online").textContent = classes.filter((c) => c !== "lost").length;
  $("stat-held").textContent = s.vehicles.filter((v) => v.held).length;
  $("stat-lost").textContent = classes.filter((c) => c === "lost").length;
  $("stat-alerts").textContent = s.alerts.length;
}

function renderCongestion(s) {
  const now = new Set(s.alerts.filter((a) => a.type === "congestion" && a.t_cleared === null).map((a) => a.subject));
  for (const id of congested) if (!now.has(id)) roadLines.get(id)?.forEach((line) => line.setStyle(ROAD_STYLE));
  for (const id of now) roadLines.get(id)?.forEach((line) => line.setStyle(CONGESTED_STYLE));
  congested = now;
}

function selectedVehicle() {
  return snap?.vehicles.find((v) => v.id === selected) ?? null;
}

// 경로에는 지나온 도로도 들어 있으므로, 지금 도로부터 남은 길만 고른다.
// 교차로 안(':'로 시작)에 있을 때는 직전 위치를 그대로 쓴다.
function remainingRoute(v) {
  const routeKey = `${v.id}:${v.route.join(",")}`;
  if (routeCursor.key !== routeKey) routeCursor = { key: routeKey, index: 0 };
  if (!v.road.startsWith(":")) {
    const index = v.route.indexOf(v.road, routeCursor.index);
    if (index >= 0) routeCursor.index = index;
  }
  return v.route.slice(routeCursor.index);
}

function renderRoute() {
  const v = selectedVehicle();
  const remaining = v ? remainingRoute(v) : [];
  const key = v ? `${v.id}:${remaining.join(",")}` : "";
  if (key === lastKeys.route) return;
  lastKeys.route = key;
  routeLayer.clearLayers();
  if (!v) return;
  for (const edge of remaining) {
    const isDestination = edge === v.destination;
    for (const line of roadLines.get(edge) ?? []) {
      L.polyline(line.getLatLngs(), {
        color: isDestination ? "#3fb950" : "#4c9aff",
        weight: isDestination ? 8 : 6,
        opacity: 0.8,
        interactive: false,
      }).addTo(routeLayer);
    }
  }
}

function renderVehicle(alerting) {
  const v = selectedVehicle();
  $("vehicle-panel").hidden = !v;
  if (!v) return;
  const cls = vehicleClass(v, alerting);
  const stateText = { lost: "연결 끊김", alerting: "알림", held: "관제 정지", ok: "운행 중" }[cls];
  $("v-id").textContent = v.id;
  $("v-state").className = `chip ${cls}`;
  $("v-state").textContent = stateText;
  $("v-speed").textContent = `${(v.speed * 3.6).toFixed(0)} km/h`;
  $("v-accel").textContent = `${v.accel.toFixed(1)} m/s²`;
  $("v-road").textContent = v.road.startsWith(":") ? "교차로 안" : v.road;
  $("v-dest").textContent = v.destination;
  $("v-alerts").innerHTML = snap.alerts
    .filter((a) => a.subject === v.id && a.t_cleared === null)
    .map((a) => `<li>${esc(a.label)} · ${esc(a.detail)}</li>`)
    .join("");
}

function renderAlerts(s) {
  $("alert-count").textContent = s.alerts.length || "";
  const key = JSON.stringify(s.alerts);
  if (key === lastKeys.alerts) return; // 바뀌지 않았으면 다시 그리지 않는다
  // 누르려던 버튼이 새 알림에 밀려 다른 알림을 처리하지 않도록, 마우스가 목록 위에 있는 동안은 미룬다
  $("alerts-paused").hidden = !alertsHovered;
  if (alertsHovered) return;
  lastKeys.alerts = key;
  $("alerts-empty").hidden = s.alerts.length > 0;
  $("alerts").innerHTML = s.alerts
    .map(
      (a) => `
    <li class="alert type-${esc(a.type)} ${esc(a.state)}" data-type="${esc(a.type)}" data-subject="${esc(a.subject)}">
      <div class="alert-top">
        <span class="badge">${esc(a.label)}</span>
        <span class="subject">${esc(a.subject)}</span>
        <time>${fmtTime(a.t_raised)}</time>
      </div>
      <div class="detail">${esc(a.detail)}</div>
      <div class="alert-bottom">
        <span class="chip ${esc(a.state)}">${ALERT_STATE_LABEL[a.state] ?? esc(a.state)}</span>
        ${a.t_cleared !== null ? `<span class="chip cleared">해제됨 ${fmtTime(a.t_cleared)}</span>` : ""}
        <span class="actions">
          ${a.state === "open" ? `<button data-action="acknowledge" data-id="${a.id}">확인</button>` : ""}
          <button data-action="resolve" data-id="${a.id}">처리 완료</button>
        </span>
      </div>
    </li>`,
    )
    .join("");
}

function renderFleet(s, alerting) {
  const items = s.vehicles.map((v) => [v.id, vehicleClass(v, alerting)]);
  const key = JSON.stringify(items) + selected;
  if (key === lastKeys.fleet) return;
  lastKeys.fleet = key;
  $("fleet-count").textContent = items.length ? `${items.length}대` : "";
  $("fleet").innerHTML = items
    .map(([id, cls]) => `<button class="vchip ${cls}${id === selected ? " selected" : ""}" data-id="${esc(id)}">${esc(id)}</button>`)
    .join("");
}

function commandText(c) {
  if (c.type === "speed_limit") return c.value === null ? "속도 제한 해제" : `속도 제한 ${Math.round(c.value * 3.6)} km/h`;
  if (c.type === "reroute") return `목적지 변경 → ${esc(c.value)}`;
  return COMMAND_LABEL[c.type] ?? esc(c.type);
}

function renderCommands(s) {
  const key = JSON.stringify(s.commands);
  if (key === lastKeys.commands) return;
  lastKeys.commands = key;
  $("commands-empty").hidden = s.commands.length > 0;
  $("commands").innerHTML = s.commands
    .map((c) => {
      const result =
        c.ok === null ? '<span class="chip pending">대기</span>' : c.ok ? '<span class="chip ok">성공</span>' : '<span class="chip fail">실패</span>';
      const reason = c.ok === false && c.reason ? `<span class="reason">${esc(c.reason)}</span>` : "";
      return `<li><time>${fmtTime(c.t_sent)}</time><span class="subject">${esc(c.vehicle_id)}</span><span>${commandText(c)}</span>${result}${reason}</li>`;
    })
    .join("");
}

// --- 선택과 명령 ---

function select(id, pan = false) {
  selected = id;
  stopPicking();
  if (snap) render(snap);
  if (pan && id && markers.has(id)) map.panTo(markers.get(id).getLatLng());
}

function focusRoad(id) {
  const lines = roadLines.get(id);
  if (lines) map.fitBounds(L.featureGroup(lines).getBounds(), { maxZoom: 1, padding: [80, 80] });
}

let toastTimer = null;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2500);
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.ok) return true;
  let message = `요청이 거부됐습니다 (${res.status})`;
  try {
    const data = await res.json();
    if (typeof data.detail === "string") message = data.detail;
  } catch {}
  toast(message);
  return false;
}

async function refresh() {
  try {
    render(await (await fetch("/api/snapshot")).json());
  } catch {}
}

async function sendCommand(type, value = null) {
  if (!selected) return;
  const vid = selected;
  if (await post(`/api/vehicles/${encodeURIComponent(vid)}/commands`, { type, value })) {
    toast(`${vid}에 ${COMMAND_LABEL[type]} 명령을 보냈습니다`);
    refresh();
  }
}

function startPicking() {
  picking = true;
  $("map").classList.add("picking");
  $("c-reroute").classList.add("active");
  $("c-hint").textContent = "지도에서 목적지 도로를 클릭하세요 (Esc 취소)";
}

function stopPicking() {
  picking = false;
  $("map").classList.remove("picking");
  $("c-reroute").classList.remove("active");
  $("c-hint").textContent = "";
}

function onRoadClick(roadId) {
  if (!picking || !selected) return;
  stopPicking();
  sendCommand("reroute", roadId);
}

$("c-stop").addEventListener("click", () => sendCommand("stop"));
$("c-resume").addEventListener("click", () => sendCommand("resume"));
$("c-limit-set").addEventListener("click", () => {
  const kmh = Number($("c-limit").value);
  if (!(kmh > 0)) return toast("제한 속도를 km/h로 입력하세요");
  sendCommand("speed_limit", kmh / 3.6);
});
$("c-limit-clear").addEventListener("click", () => sendCommand("speed_limit", null));
$("c-reroute").addEventListener("click", () => (picking ? stopPicking() : startPicking()));
$("v-close").addEventListener("click", () => select(null));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (picking) stopPicking();
  else select(null);
});

$("alerts").addEventListener("pointerenter", () => (alertsHovered = true));
$("alerts").addEventListener("pointerleave", () => {
  alertsHovered = false;
  if (snap) renderAlerts(snap);
});

$("alerts").addEventListener("click", async (e) => {
  const button = e.target.closest("button[data-action]");
  if (button) {
    if (!(await post(`/api/alerts/${button.dataset.id}/${button.dataset.action}`))) return;
    // 목록 갱신을 미루는 중에도 누른 결과는 바로 보이게 그 항목만 고친다
    const item = button.closest("li.alert");
    if (button.dataset.action === "resolve") {
      item.remove();
    } else {
      item.classList.replace("open", "acknowledged");
      const chip = item.querySelector(".alert-bottom .chip");
      chip.className = "chip acknowledged";
      chip.textContent = ALERT_STATE_LABEL.acknowledged;
      button.remove();
    }
    lastKeys.alerts = ""; // 다음 갱신 때 서버 상태로 다시 그린다
    refresh();
    return;
  }
  const item = e.target.closest("li.alert");
  if (!item) return;
  if (item.dataset.type === "congestion") focusRoad(item.dataset.subject);
  else select(item.dataset.subject, true);
});

$("fleet").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-id]");
  if (button) select(button.dataset.id, true);
});

// --- 실시간 연결 ---

function setLink(up) {
  const el = $("link");
  el.className = `link ${up ? "up" : "down"}`;
  el.textContent = up ? "실시간 연결됨" : "연결 끊김, 다시 연결 중…";
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => setLink(true);
  ws.onmessage = (e) => render(JSON.parse(e.data));
  ws.onclose = () => {
    setLink(false);
    setTimeout(connect, 1000);
  };
}

loadMap().then(connect);
