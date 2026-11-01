"use strict";

// ---------------------------------------------------------------- состояние
const S = {
  tab: localStorage.getItem("tab") || "home",
  devices: {},          // id -> устройство
  rooms: {},            // id -> имя
  scenarios: [],
  yaScenarios: [],
  log: [],
  status: null,
  openDevice: null,     // id устройства в открытой карточке
  dragging: false,      // пользователь двигает слайдер — не перерисовываем
  ws: null,
};

const native = window.webkit?.messageHandlers?.app;
const isNative = !!native;

// ---------------------------------------------------------------- утилиты
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.method = opts.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) { showLogin(); throw new Error("Нужен вход"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `Ошибка ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), err ? 4000 : 1800);
}

// navigator.clipboard недоступен по HTTP, поэтому копируем через скрытое поле
function copyText(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;font-size:16px";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) {}
  ta.remove();
  if (!ok && navigator.clipboard) { navigator.clipboard.writeText(text).catch(() => {}); ok = true; }
  return ok;
}

function haptic() { try { native?.postMessage({ cmd: "haptic" }); } catch (_) {} }

function openExternal(url) {
  if (isNative) native.postMessage({ cmd: "open", url });
  else window.open(url, "_blank");
}

const fmtTime = (ts) => {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const t = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? t : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) + " " + t;
};

// ---------------------------------------------------------------- словари
const ICONS = {
  light: "💡", switch: "🎚️", socket: "🔌", "media_device.tv": "📺", media_device: "📺",
  "media_device.tv_box": "📺", "media_device.receiver": "📻", smart_speaker: "🔊",
  "sensor.open": "🚪", "sensor.climate": "🌡️", "sensor.motion": "🏃", "sensor.water_leak": "💧",
  "sensor.smoke": "🔥", "sensor.gas": "⚠️", "sensor.button": "🔘", "sensor.vibration": "📳",
  "sensor.illumination": "☀️", sensor: "📟", thermostat: "🌡️", "thermostat.ac": "❄️",
  humidifier: "💨", purifier: "🌬️", vacuum_cleaner: "🧹", openable: "🚪", "openable.curtain": "🪟",
  cooking: "🍳", "cooking.kettle": "🫖", "cooking.coffee_maker": "☕", washing_machine: "🫧",
  dishwasher: "🍽️", iron: "👕", camera: "📷", hub: "📡", other: "⚙️",
};
const deviceIcon = (d) => {
  const t = d.type.replace("devices.types.", "");
  return ICONS[t] || ICONS[t.split(".")[0]] || "⚙️";
};

const NAMES = {
  on: "Питание", brightness: "Яркость", temperature: "Температура", volume: "Громкость",
  channel: "Канал", humidity: "Влажность", open: "Открытие", battery_level: "Батарея",
  input_source: "Источник", mute: "Без звука", pause: "Пауза", backlight: "Подсветка",
  controls_locked: "Блокировка", oscillation: "Вращение", ionization: "Ионизация", keep_warm: "Подогрев",
  program: "Программа", thermostat: "Термостат", fan_speed: "Скорость", work_speed: "Скорость работы",
  cleanup_mode: "Режим уборки", swing: "Направление", heat: "Нагрев", coffee_mode: "Кофе", tea_mode: "Чай",
  illumination: "Освещённость", pressure: "Давление", power: "Мощность", amperage: "Ток",
  voltage: "Напряжение", co2_level: "CO₂", motion: "Движение", button: "Кнопка",
  water_leak: "Протечка", smoke: "Дым", gas: "Газ", vibration: "Вибрация",
  electricity_meter: "Электроэнергия", temperature_k: "Цветовая температура", hsv: "Цвет",
  rgb: "Цвет", scene: "Сцена", "pm2.5_density": "PM2.5", tvoc: "Летучие вещества", water_level: "Вода",
};
const EVENTS = {
  opened: "Открыто", closed: "Закрыто", detected: "Обнаружено", not_detected: "Нет",
  leak: "Протечка!", dry: "Сухо", click: "Нажатие", double_click: "Двойное нажатие",
  long_press: "Долгое нажатие", vibration: "Вибрация", tilt: "Наклон", fall: "Падение",
  low: "Низкий", normal: "Норма", high: "Высокий", empty: "Пусто", full: "Полный",
};
const UNITS = {
  "unit.percent": "%", "unit.temperature.celsius": "°C", "unit.temperature.kelvin": "K",
  "unit.pressure.mmhg": "мм рт.ст.", "unit.pressure.atm": "атм", "unit.pressure.bar": "бар",
  "unit.pressure.pascal": "Па", "unit.ppm": "ppm", "unit.watt": "Вт", "unit.volt": "В",
  "unit.ampere": "А", "unit.kilowatt_hour": "кВт·ч", "unit.illumination.lux": "лк",
  "unit.density.mcg_m3": "мкг/м³", "unit.time.seconds": "с",
};
const SCENES = {
  alarm: "Тревога", alice: "Алиса", candle: "Свеча", dinner: "Ужин", fantasy: "Фантазия",
  garland: "Гирлянда", jungle: "Джунгли", movie: "Кино", neon: "Неон", night: "Ночь",
  ocean: "Океан", party: "Вечеринка", reading: "Чтение", rest: "Отдых", romance: "Романтика",
  siren: "Сирена", sunrise: "Рассвет", sunset: "Закат",
};
const nm = (i) => NAMES[i] || i;
const shortType = (t) => t.replace("devices.capabilities.", "").replace("devices.properties.", "");

function capOf(d, type, instance) {
  return d.capabilities.find((c) => shortType(c.type) === type &&
    (instance === undefined || (c.parameters.instance || (type === "on_off" ? "on" : c.state?.instance)) === instance));
}
function propValue(p) {
  const v = p.state?.value;
  if (v === undefined || v === null) return "—";
  if (shortType(p.type) === "event") return EVENTS[v] || v;
  const u = UNITS[p.parameters.unit] || "";
  return `${typeof v === "number" ? Math.round(v * 10) / 10 : v}${u ? " " + u : ""}`;
}

function summary(d) {
  const parts = [];
  const onoff = capOf(d, "on_off");
  if (onoff) {
    const v = onoff.state?.value;
    parts.push(v === true ? "Вкл" : v === false ? "Выкл" : "—");
    const br = capOf(d, "range", "brightness");
    if (v && br?.state) parts.push(br.state.value + "%");
  }
  let alert = false;
  for (const p of d.properties) {
    const inst = p.parameters.instance;
    if (inst === "battery_level" && d.properties.length > 1) continue;
    if (shortType(p.type) === "event" && ["opened", "detected", "leak"].includes(p.state?.value)) alert = true;
    if (p.state) parts.push(propValue(p));
  }
  return { text: parts.join(" · ") || (d.capabilities.length ? "Управление" : "—"), alert };
}

// ---------------------------------------------------------------- загрузка
async function loadDevices() {
  const data = await api("/api/devices");
  S.rooms = data.rooms;
  S.households = data.households || [];
  S.devices = Object.fromEntries(data.devices.map((d) => [d.id, d]));
}
async function loadScenarios() {
  [S.scenarios, S.yaScenarios] = await Promise.all([api("/api/scenarios"), api("/api/yandex/scenarios")]);
}
async function loadStatus() { S.status = await api("/api/status"); }
async function loadLog() { S.log = await api("/api/log?limit=300"); }

// ---------------------------------------------------------------- WebSocket
let wsDelay = 1000;
function connectWS() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  S.ws = ws;
  ws.onopen = () => {
    wsDelay = 1000;
    $("#conn").classList.add("hidden");
    refreshAll();
  };
  ws.onmessage = (m) => onEvent(JSON.parse(m.data));
  ws.onclose = (e) => {
    if (e.code === 4401) { showLogin(); return; }
    $("#conn").classList.remove("hidden");
    setTimeout(connectWS, wsDelay);
    wsDelay = Math.min(wsDelay * 2, 15000);
  };
}

let devicesReload;
function onEvent(ev) {
  if (ev.type === "state") {
    const d = S.devices[ev.device_id];
    if (!d) return;
    applyState(d, ev.key, ev.value, ev.ts);
    updateCard(d);
    if (S.openDevice === d.id && !S.dragging) renderDevice(d.id);
  } else if (ev.type === "devices") {
    clearTimeout(devicesReload);
    devicesReload = setTimeout(() => loadDevices().then(render), 300);
  } else if (ev.type === "log") {
    S.log.unshift(ev);
    S.log.length = Math.min(S.log.length, 300);
    if (S.tab === "log") render();
  } else if (ev.type === "scenario") {
    loadScenarios().then(() => S.tab === "scenarios" && render());
  }
}

function applyState(d, key, value, ts) {
  d.states[key] = value;
  const [kind, inst] = [key.slice(0, key.indexOf(".")), key.slice(key.indexOf(".") + 1)];
  if (kind === "float" || kind === "event") {
    for (const p of d.properties) if (shortType(p.type) === kind && p.parameters.instance === inst) {
      p.state = { instance: inst, value }; p.last_updated = ts;
    }
  } else if (kind !== "z2m") {
    for (const c of d.capabilities) {
      if (shortType(c.type) !== kind) continue;
      const ci = c.parameters.instance || (kind === "on_off" ? "on" : null);
      if (kind === "color_setting" || ci === inst) { c.state = { instance: inst, value }; c.last_updated = ts; break; }
    }
  }
}

// ---------------------------------------------------------------- команды
async function act(id, set, quiet = false) {
  haptic();
  const d = S.devices[id];
  try {
    const res = await api(`/api/devices/${encodeURIComponent(id)}/action`, { body: { set } });
    S.devices[id] = res;
    updateCard(res);
    if (S.openDevice === id && !S.dragging) renderDevice(id);
  } catch (e) {
    toast(`${d?.name || ""}: ${e.message}`, true);
    if (!quiet) throw e;
  }
}

// ---------------------------------------------------------------- отрисовка
function render() {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === S.tab));
  const v = $("#view");
  if (S.tab === "home") v.innerHTML = renderHome();
  else if (S.tab === "scenarios") v.innerHTML = renderScenarios();
  else if (S.tab === "log") v.innerHTML = renderLog();
  else v.innerHTML = renderSettings();
}

function cardHTML(d) {
  const onoff = capOf(d, "on_off");
  const on = onoff?.state?.value === true;
  const s = summary(d);
  return `<div class="card ${on ? "on" : ""} ${s.alert ? "alert" : ""}" data-dev="${esc(d.id)}">
    <div class="icon">${deviceIcon(d)}</div>
    ${onoff ? `<button class="power" data-power="${esc(d.id)}">⏻</button>` : ""}
    <div><div class="name">${esc(d.name)}</div><div class="sum">${esc(s.text)}</div></div>
  </div>`;
}

function updateCard(d) {
  const el = document.querySelector(`.card[data-dev="${CSS.escape(d.id)}"]`);
  if (el) el.outerHTML = cardHTML(d);
}

function renderHome() {
  const devs = Object.values(S.devices);
  if (!devs.length) {
    const ya = S.status?.adapters?.yandex;
    return `<h1>Дом</h1><div class="list"><div class="row"><div class="grow">
      <div class="title">Устройств пока нет</div>
      <div class="sub">${ya && !ya.logged_in ? "Войдите в аккаунт Яндекса в «Настройках», и устройства появятся здесь."
        : ya?.last_error ? "Ошибка Яндекса: " + esc(ya.last_error) : "Загрузка…"}</div></div></div></div>`;
  }
  // выбор дома, если их несколько
  const houses = (S.households || []).filter((h) => devs.some((d) => d.household_id === h.id));
  let house = localStorage.getItem("house") || "";
  if (house && !houses.some((h) => h.id === house)) house = "";
  let head = `<h1>${esc(houses.length === 1 ? houses[0].name : house ? houses.find((h) => h.id === house).name : "Дом")}</h1>`;
  if (houses.length > 1) {
    head += `<div class="chips" style="margin:-4px 0 6px">
      <button class="chip ${house ? "" : "active"}" data-house="">Все дома</button>
      ${houses.map((h) => `<button class="chip ${house === h.id ? "active" : ""}" data-house="${esc(h.id)}">${esc(h.name)}</button>`).join("")}</div>`;
  }
  const shown = house ? devs.filter((d) => d.household_id === house || !d.household_id) : devs;
  const groups = {};
  for (const d of shown) {
    let room = d.room_name || (d.source === "zigbee" ? "Zigbee" : "Без комнаты");
    if (houses.length > 1 && !house && d.household_id) {
      room = (houses.find((h) => h.id === d.household_id)?.name || "") + " · " + room;
    }
    (groups[room] ||= []).push(d);
  }
  const names = Object.keys(groups).sort((a, b) => a.localeCompare(b, "ru"));
  return head + names.map((r) =>
    `<h2>${esc(r)}</h2><div class="grid">${groups[r].sort((a, b) => a.name.localeCompare(b.name, "ru")).map(cardHTML).join("")}</div>`
  ).join("");
}

// ---- карточка устройства
function openDevice(id) {
  S.openDevice = id;
  if (typeof B !== "undefined") B.open = false;
  renderDevice(id);
  $("#sheet").classList.remove("hidden");
  loadSparklines(id);
}
function closeSheet() {
  S.openDevice = null;
  if (typeof B !== "undefined") B.open = false;
  $("#sheet").classList.add("hidden");
  $("#sheet-content").innerHTML = "";
}

function sw(checked, attrs) {
  return `<label class="switch"><input type="checkbox" ${checked ? "checked" : ""} ${attrs}><span></span></label>`;
}

function hsvToHex({ h, s, v }) {
  s /= 100; v /= 100;
  const f = (n) => { const k = (n + h / 60) % 6; return v - v * s * Math.max(0, Math.min(k, 4 - k, 1)); };
  return "#" + [f(5), f(3), f(1)].map((x) => Math.round(x * 255).toString(16).padStart(2, "0")).join("");
}
function hexToHsv(hex) {
  const n = parseInt(hex.slice(1), 16), r = (n >> 16 & 255) / 255, g = (n >> 8 & 255) / 255, b = (n & 255) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d) h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return { h: Math.round((h * 60 + 360) % 360), s: Math.round(mx ? d / mx * 100 : 0), v: Math.round(mx * 100) };
}

function capControl(d, c) {
  const t = shortType(c.type), p = c.parameters, st = c.state;
  const id = esc(d.id);
  if (t === "on_off") {
    if (!c.retrievable) {
      return `<div class="ctl"><label>${nm("on")}</label><div class="btns">
        <button class="btn" data-set='{"on_off.on":true}' data-id="${id}">Включить</button>
        <button class="btn" data-set='{"on_off.on":false}' data-id="${id}">Выключить</button></div></div>`;
    }
    return `<div class="row" style="padding:12px 0"><div class="grow title">${nm("on")}</div>
      ${sw(st?.value === true, `data-key="on_off.on" data-id="${id}"`)}</div>`;
  }
  if (t === "range") {
    const inst = p.instance, r = p.range || { min: 0, max: 100, precision: 1 };
    const unit = UNITS[p.unit] || "";
    const key = `range.${inst}`;
    const step = r.precision || 1;
    let html = `<div class="ctl"><label><span>${nm(inst)}</span><span>${st ? esc(st.value) + " " + unit : ""}</span></label>`;
    if (p.random_access !== false) {
      html += `<input type="range" min="${r.min}" max="${r.max}" step="${step}" value="${st?.value ?? r.min}"
        data-range="${key}" data-id="${id}">`;
    }
    html += `<div class="btns" style="margin-top:6px">
      <button class="btn small" data-set='${JSON.stringify({ [key]: "-" + step })}' data-id="${id}">−</button>
      <button class="btn small" data-set='${JSON.stringify({ [key]: "+" + step })}' data-id="${id}">+</button></div></div>`;
    return html;
  }
  if (t === "color_setting") {
    let html = "";
    if (p.temperature_k) {
      const v = st?.instance === "temperature_k" ? st.value : Math.round((p.temperature_k.min + p.temperature_k.max) / 2);
      html += `<div class="ctl"><label><span>${nm("temperature_k")}</span><span>${st?.instance === "temperature_k" ? v + " K" : ""}</span></label>
        <input class="temp" type="range" min="${p.temperature_k.min}" max="${p.temperature_k.max}" step="100" value="${v}"
          data-range="color_setting.temperature_k" data-id="${id}"></div>`;
    }
    if (p.color_model) {
      const cur = st?.instance === "hsv" ? hsvToHex(st.value) : st?.instance === "rgb" ? "#" + Number(st.value).toString(16).padStart(6, "0") : "#ffffff";
      const presets = ["#ff0000", "#ff8800", "#ffee00", "#00ff44", "#00ccff", "#0044ff", "#aa00ff", "#ff00aa"];
      html += `<div class="ctl"><label><span>${nm("hsv")}</span></label><div class="chips">
        ${presets.map((h) => `<button class="swatch" style="background:${h}" data-color="${h}" data-id="${id}" data-model="${p.color_model}"></button>`).join("")}
        <input type="color" value="${cur}" data-color-input data-id="${id}" data-model="${p.color_model}" class="color-pick" title="Свой цвет">
      </div></div>`;
    }
    const scenes = p.color_scene?.scenes || [];
    if (scenes.length) {
      html += `<div class="ctl"><label>${nm("scene")}</label><div class="chips">${scenes.map((s) =>
        `<button class="chip ${st?.instance === "scene" && st.value === s.id ? "active" : ""}"
          data-set='${JSON.stringify({ "color_setting.scene": s.id })}' data-id="${id}">${esc(SCENES[s.id] || s.id)}</button>`).join("")}</div></div>`;
    }
    return html;
  }
  if (t === "mode") {
    const key = `mode.${p.instance}`;
    return `<div class="ctl"><label>${nm(p.instance)}</label><div class="chips">${(p.modes || []).map((m) =>
      `<button class="chip ${st?.value === m.value ? "active" : ""}" data-set='${JSON.stringify({ [key]: m.value })}'
        data-id="${id}">${esc(nm(m.value))}</button>`).join("")}</div></div>`;
  }
  if (t === "toggle") {
    return `<div class="row" style="padding:12px 0"><div class="grow title">${nm(p.instance)}</div>
      ${sw(st?.value === true, `data-key="toggle.${esc(p.instance)}" data-id="${id}"`)}</div>`;
  }
  return `<div class="ctl muted small">${esc(t)}: ${esc(JSON.stringify(st?.value ?? null))}</div>`;
}

function z2mControl(d, f) {
  const id = esc(d.id), key = "z2m." + f.property, cur = d.states[key];
  const label = esc(f.label || f.property) + (f.unit ? `, ${esc(f.unit)}` : "");
  if (f.type === "binary") {
    return `<div class="row" style="padding:10px 0"><div class="grow">${label}</div>
      ${sw(cur === f.value_on, `data-z2m-bool="${esc(f.property)}" data-on='${esc(JSON.stringify(f.value_on))}' data-off='${esc(JSON.stringify(f.value_off))}' data-id="${id}"`)}</div>`;
  }
  if (f.type === "enum") {
    return `<div class="ctl"><label>${label}</label><div class="chips">${(f.values || []).map((v) =>
      `<button class="chip ${cur === v ? "active" : ""}" data-set='${esc(JSON.stringify({ [key]: v }))}' data-id="${id}">${esc(v)}</button>`).join("")}</div></div>`;
  }
  return `<div class="ctl"><label><span>${label}</span><span>${esc(cur ?? "")}</span></label>
    <input type="number" value="${esc(cur ?? "")}" min="${f.value_min ?? ""}" max="${f.value_max ?? ""}" step="${f.value_step ?? "any"}"
      data-z2m-num="${esc(f.property)}" data-id="${id}"></div>`;
}

function renderDevice(id) {
  const d = S.devices[id];
  if (!d) { closeSheet(); return; }
  const room = d.room_name ? esc(d.room_name) + " · " : "";
  const info = d.device_info ? [d.device_info.manufacturer, d.device_info.model].filter(Boolean).join(" ") : "";
  let html = `<div style="display:flex;align-items:center;gap:12px">
      <div style="font-size:36px">${deviceIcon(d)}</div>
      <div class="grow"><div style="font-size:20px;font-weight:700">${esc(d.name)}</div>
      <div class="muted small">${room}${esc(d.type.replace("devices.types.", ""))} · ${d.source === "zigbee" ? "Zigbee (локально)" : "Яндекс"}${info ? " · " + esc(info) : ""}</div></div>
    </div>`;
  const ORDER = ["on_off", "range", "color_setting", "mode", "toggle"];
  const rank = (c) => { const i = ORDER.indexOf(shortType(c.type)); return i < 0 ? 99 : i; };
  html += [...d.capabilities].sort((a, b) => rank(a) - rank(b)).map((c) => capControl(d, c)).join("");
  if (d.is_speaker) {
    const QUICK = ["Стоп", "Громче", "Тише", "Включи музыку", "Какая погода", "Который час"];
    html += `<h2>Колонка</h2>
      <div class="ctl"><input id="speak-text" type="text" maxlength="100" placeholder="Текст или команда (до 100 символов)">
      <div class="btns" style="margin-top:8px">
        <button class="btn primary" data-speak="${esc(d.id)}" data-mode="tts">🔊 Сказать</button>
        <button class="btn" data-speak="${esc(d.id)}" data-mode="command">🗣 Выполнить как команду</button></div>
      <div class="chips" style="margin-top:8px">${QUICK.map((q) =>
        `<button class="chip" data-speak="${esc(d.id)}" data-mode="command" data-text="${esc(q)}">${esc(q)}</button>`).join("")}</div>
      <div class="muted small" style="margin-top:6px">«Сказать» — колонка произнесёт текст. «Команда» — как будто вы сказали это Алисе.</div></div>`;
  }
  if (d.properties.length) {
    html += `<h2>Показания</h2>` + d.properties.map((p) => {
      const inst = p.parameters.instance;
      return `<div class="prop"><span>${esc(nm(inst))}<br><span class="muted small">${fmtTime(p.last_updated)}</span></span>
        <b>${esc(propValue(p))}</b></div>
        ${shortType(p.type) === "float" ? `<svg class="spark" data-spark="float.${esc(inst)}"></svg>` : ""}`;
    }).join("");
  }
  if (d.z2m_settable?.length) {
    html += `<details><summary>Дополнительные настройки Zigbee</summary>${d.z2m_settable.map((f) => z2mControl(d, f)).join("")}</details>`;
  }
  html += `<details><summary>Ключи для сценариев</summary><div class="small muted" style="margin-bottom:6px">Нажмите, чтобы скопировать</div>
    <div class="chips">${Object.keys(d.states).filter((k) => !k.startsWith("z2m.")).map((k) =>
      `<code class="key" data-copy="${esc(k)}">${esc(k)}</code>`).join("") || "<span class='muted small'>нет</span>"}</div>
    <div class="small muted" style="margin-top:8px">id: <code class="key" data-copy="${esc(d.id)}">${esc(d.id)}</code></div></details>
    <details><summary>Все данные устройства</summary><pre>${esc(JSON.stringify(d, null, 2))}</pre></details>`;
  const content = $("#sheet-content");
  const scroll = content.parentElement.scrollTop;
  content.innerHTML = html;
  content.parentElement.scrollTop = scroll;
  drawSparklines(id);
}

const sparkCache = {};
async function loadSparklines(id) {
  try {
    const hist = await api(`/api/devices/${encodeURIComponent(id)}/history?hours=24`);
    sparkCache[id] = hist;
    drawSparklines(id);
  } catch (_) {}
}
function drawSparklines(id) {
  const hist = sparkCache[id];
  if (!hist) return;
  document.querySelectorAll("svg[data-spark]").forEach((svg) => {
    const pts = hist.filter((h) => h.key === svg.dataset.spark && typeof h.value === "number");
    if (pts.length < 2) { svg.outerHTML = ""; return; }
    const W = 300, H = 50, t0 = pts[0].ts, t1 = pts[pts.length - 1].ts || t0 + 1;
    const vs = pts.map((p) => p.value), lo = Math.min(...vs), hi = Math.max(...vs), span = hi - lo || 1;
    const path = pts.map((p, i) => `${i ? "L" : "M"}${((p.ts - t0) / (t1 - t0 || 1) * W).toFixed(1)},${(H - 4 - (p.value - lo) / span * (H - 8)).toFixed(1)}`).join("");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.innerHTML = `<path d="${path}" fill="none" stroke="#5b8cff" stroke-width="2" vector-effect="non-scaling-stroke"/>
      <text x="2" y="10" fill="#8b90a0" font-size="9">${hi}</text><text x="2" y="${H - 1}" fill="#8b90a0" font-size="9">${lo}</text>`;
  });
}

// ---- сценарии
function renderScenarios() {
  let html = `<h1>Сценарии</h1><div class="btns" style="margin-bottom:12px">
    <button class="btn primary" data-sc-new>+ Новый сценарий</button></div>`;
  html += `<div class="list">` + (S.scenarios.length ? S.scenarios.map((s) => {
    const lr = s.last_run;
    const trig = describeScenario(s);
    return `<div class="row"><div class="grow" data-sc-edit="${esc(s.id)}" style="cursor:pointer">
      <div class="title">${esc(s.name)} ${s.running ? '<span class="badge ok">выполняется</span>' : ""}</div>
      <div class="sub">${esc(trig)}${lr ? ` · ${fmtTime(lr.ts)} ${lr.ok === false ? "⚠️" : ""}` : ""}</div></div>
      <button class="btn small" data-sc-run="${esc(s.id)}">▶</button>
      ${sw(s.enabled, `data-sc-toggle="${esc(s.id)}"`)}</div>`;
  }).join("") : `<div class="row"><div class="grow muted">Своих сценариев пока нет</div></div>`) + `</div>`;
  if (S.yaScenarios.length) {
    html += `<h2>Сценарии Яндекса</h2><div class="list">${S.yaScenarios.map((s) =>
      `<div class="row"><div class="grow title">${esc(s.name)}</div>
       <button class="btn small" data-ya-run="${esc(s.id)}">▶</button></div>`).join("")}</div>`;
  }
  return html;
}

// ---- журнал
function renderLog() {
  return `<h1>Журнал</h1><div class="list">${S.log.map((l) =>
    `<div class="log-row ${esc(l.level)}"><time>${fmtTime(l.ts)}</time><span class="badge">${esc(l.source)}</span> ${esc(l.message)}</div>`
  ).join("") || '<div class="log-row muted">Пусто</div>'}</div>`;
}

// ---- настройки
function renderSettings() {
  const st = S.status || {}, ya = st.adapters?.yandex, zb = st.adapters?.zigbee;
  let html = `<h1>Настройки</h1><h2>Аккаунт Яндекса</h2><div class="list">`;
  if (ya) {
    const how = ya.method === "qr" ? " (по QR-коду)" : ya.method === "oauth" ? " (OAuth)" : "";
    html += `<div class="row"><div class="grow"><div class="title">${ya.logged_in ? "Вход выполнен" + how : "Вход не выполнен"}</div>
      <div class="sub">${ya.last_ok ? "Последний опрос: " + fmtTime(ya.last_ok) : ""}
      ${ya.last_error ? `<br><span style="color:var(--danger)">${esc(ya.last_error)}</span>` : ""}
      ${ya.token_expires_at ? "<br>Токен до " + new Date(ya.token_expires_at * 1000).toLocaleDateString("ru-RU") : ""}</div></div>
      ${ya.token_source !== "env" ? `<button class="btn small danger" data-ya-logout>${ya.logged_in ? "Выйти" : "Сбросить"}</button>` : ""}</div>`;
    if (!ya.logged_in || ya.last_error) {
      html += `<div class="row"><div class="grow">
        <div class="title" style="margin-bottom:6px">Вход по QR-коду</div>
        <div class="sub" style="margin-bottom:8px">Своё OAuth-приложение не нужно. Подтвердите вход в приложении Яндекса.</div>
        <div id="qr-box">${qrBoxHTML()}</div></div></div>`;
      html += ya.can_login ? `<div class="row"><div class="grow"><details><summary>Вход через своё OAuth-приложение</summary>
        <div class="sub" style="margin:8px 0">Нажмите кнопку, подтвердите доступ, вставьте код или токен со страницы Яндекса.</div>
        <div class="btns" style="margin-bottom:8px">
          <a class="btn" href="${esc(ya.login_url)}" target="_blank" rel="noopener">Войти через Яндекс</a>
          <a class="btn" href="${esc(ya.token_url)}" target="_blank" rel="noopener">Получить токен</a></div>
        <div class="sub" style="margin-bottom:8px">ClientID на сервере: <b>${esc((ya.client_id || "—").slice(0, 8))}…</b>
          <button class="btn small" data-ya-check style="margin-left:6px">Проверить ключи</button></div>
        <input id="ya-code" placeholder="Код или токен" autocapitalize="off" autocorrect="off" spellcheck="false">
        <div class="btns" style="margin-top:8px"><button class="btn" data-ya-code>Отправить</button></div></details></div></div>` : "";
    }
  }
  html += `</div><h2>Zigbee</h2><div class="list">`;
  html += zb ? `<div class="row"><div class="grow"><div class="title">${zb.connected ? "MQTT подключён" : "MQTT не подключён"}</div>
      <div class="sub">${esc(zb.host)} · мост: ${esc(zb.bridge_state || "?")} · устройств: ${zb.devices}
      ${zb.last_error ? `<br><span style="color:var(--danger)">${esc(zb.last_error)}</span>` : ""}</div></div>
      <button class="btn small" data-permit ${zb.connected ? "" : "disabled"}>Добавить устройство</button></div>`
    : `<div class="row"><div class="grow sub">Не настроен. Укажите MQTT_HOST на сервере, когда появится Zigbee-стик.</div></div>`;
  html += `</div><h2>Сервер</h2><div class="list">
    <div class="row"><div class="grow"><div class="title">${esc(location.host)}</div>
      <div class="sub">Версия ${esc(st.version || "?")} · устройств: ${st.devices ?? "?"} · ${esc(st.timezone || "")}</div></div></div>
    ${isNative ? `<div class="row"><div class="grow">Адрес сервера в приложении</div><button class="btn small" data-native-settings>Изменить</button></div>` : ""}
    <div class="row"><div class="grow">Обновить интерфейс</div><button class="btn small" data-reload>Обновить</button></div>
  </div>`;
  return html;
}

// ---- вход в Яндекс по QR-коду
const QR = { link: null, ts: 0, timer: null, state: "idle" };

function qrBoxHTML() {
  if (QR.state === "idle" || !QR.link) {
    return `<button class="btn primary" data-qr-start>${QR.state === "loading" ? "Получаю QR-код…" : "Показать QR-код"}</button>`;
  }
  if (QR.state === "expired") {
    return `<div class="sub" style="margin-bottom:8px">Время вышло.</div><button class="btn primary" data-qr-start>Новый QR-код</button>`;
  }
  return `<div style="background:#fff;border-radius:16px;padding:10px;width:240px;max-width:100%">
      <img src="/api/yandex/qr.svg?t=${QR.ts}" alt="QR" style="width:100%;display:block"></div>
    <div class="sub" style="margin:10px 0">
      <b>Вариант 1.</b> Откройте этот экран на другом устройстве (например, ${esc(location.host)} в браузере компьютера)
      и отсканируйте QR-код камерой телефона, где вы вошли в Яндекс.<br>
      <b>Вариант 2.</b> На этом телефоне нажмите кнопку ниже и подтвердите вход в Яндексе.</div>
    <div class="btns"><a class="btn primary" href="${esc(QR.link)}" target="_blank" rel="noopener">Подтвердить на этом телефоне</a>
      <span class="key" data-copy="${esc(QR.link)}" style="align-self:center;color:var(--accent2);cursor:pointer">скопировать ссылку</span></div>
    <div class="sub" style="margin-top:8px">Жду подтверждения… QR-код действует 5 минут.</div>`;
}

function renderQR() {
  const box = $("#qr-box");
  if (box) box.innerHTML = qrBoxHTML();
}

async function qrStart() {
  QR.state = "loading"; renderQR();
  try {
    const r = await api("/api/yandex/qr/start", { body: {} });
    Object.assign(QR, { link: r.link, ts: Date.now(), state: "waiting" });
  } catch (e) {
    QR.state = "idle"; toast(e.message, true);
  }
  renderQR();
  clearInterval(QR.timer);
  if (QR.state === "waiting") QR.timer = setInterval(qrPoll, 2500);
}

async function qrPoll() {
  try {
    const r = await api("/api/yandex/qr/check", { body: {} });
    if (r.done) {
      clearInterval(QR.timer);
      Object.assign(QR, { state: "idle", link: null });
      toast("Вход выполнен, загружаю устройства…");
      setTimeout(refreshAll, 2000);
    } else if (r.expired) {
      clearInterval(QR.timer);
      QR.state = "expired"; renderQR();
    }
  } catch (e) {
    clearInterval(QR.timer);
    QR.state = "idle"; renderQR();
    toast(e.message, true);
  }
}

// ---- вход
function showLogin() {
  if (S.ws) { S.ws.onclose = null; S.ws.close(); }
  $("#tabs").classList.add("hidden");
  $("#view").innerHTML = `<div class="login"><div style="font-size:52px">🏠</div><h1>Умный дом</h1>
    <input id="pw" type="password" placeholder="Пароль" autocomplete="current-password">
    <button class="btn primary" style="width:100%" data-login>Войти</button></div>`;
}

// ---------------------------------------------------------------- обработчики
const debounce = {};
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-power],[data-dev],[data-set],[data-close],[data-tab],[data-color],[data-copy]," +
    "[data-sc-new],[data-sc-edit],[data-house],[data-speak],[data-sc-run],[data-ya-run]," +
    "[data-ya-code],[data-ya-logout],[data-ya-check],[data-qr-start],[data-permit],[data-reload],[data-native-settings],[data-login]");
  if (!t) return;
  const ds = t.dataset;
  try {
    if (ds.power) {
      e.stopPropagation();
      const d = S.devices[ds.power];
      await act(d.id, { "on_off.on": capOf(d, "on_off")?.state?.value !== true });
    } else if (ds.dev) openDevice(ds.dev);
    else if (ds.set) await act(ds.id, JSON.parse(ds.set));
    else if (ds.close !== undefined) closeSheet();
    else if (ds.tab) { S.tab = ds.tab; localStorage.setItem("tab", S.tab); render(); refreshTab(); }
    else if (ds.color) {
      const set = ds.model === "rgb" ? { "color_setting.rgb": ds.color } : { "color_setting.hsv": hexToHsv(ds.color) };
      await act(ds.id, set);
    } else if (ds.copy) {
      toast(copyText(ds.copy) ? "Скопировано" : "Не удалось скопировать", false);
    } else if (ds.scNew !== undefined) openBuilder(null);
    else if (ds.scEdit) openBuilder(ds.scEdit);
    else if (ds.house !== undefined) { localStorage.setItem("house", ds.house); render(); }
    else if (ds.speak) {
      const text = ds.text || $("#speak-text")?.value || "";
      if (!text.trim()) { toast("Введите текст", true); return; }
      await api(`/api/devices/${encodeURIComponent(ds.speak)}/speak`, { body: { text, mode: ds.mode } });
      toast(ds.mode === "command" ? "Команда отправлена" : "Отправлено на колонку");
    }
    else if (ds.scRun) { await api(`/api/scenarios/${encodeURIComponent(ds.scRun)}/run`, { body: {} }); toast("Запущено"); }
    else if (ds.yaRun) { await api(`/api/yandex/scenarios/${encodeURIComponent(ds.yaRun)}/run`, { body: {} }); toast("Запущено"); }
    else if (ds.yaCode !== undefined) {
      await api("/api/yandex/code", { body: { code: $("#ya-code").value } });
      toast("Вход выполнен"); setTimeout(refreshAll, 1500);
    } else if (ds.qrStart !== undefined) {
      await qrStart();
    } else if (ds.yaCheck !== undefined) {
      const r = await api("/api/yandex/check");
      toast(r.message, !r.ok);
    } else if (ds.yaLogout !== undefined) {
      if (!confirm("Сбросить вход в аккаунт Яндекса?")) return;
      await api("/api/yandex/logout", { body: {} }); await refreshAll();
    } else if (ds.permit !== undefined) { await api("/api/zigbee/permit-join", { body: {} }); toast("Сопряжение открыто на 4 минуты"); }
    else if (ds.reload !== undefined) location.reload();
    else if (ds.nativeSettings !== undefined) native.postMessage({ cmd: "settings" });
    else if (ds.login !== undefined) {
      await api("/api/login", { body: { password: $("#pw").value } });
      $("#tabs").classList.remove("hidden"); connectWS();
    }
  } catch (err) {
    if (err.message !== "Нужен вход") toast(err.message, true);
  }
});

document.addEventListener("change", (e) => {
  const t = e.target, ds = t.dataset;
  if (ds.key) act(ds.id, { [ds.key]: t.checked }, true);
  else if (ds.scToggle) {
    const sc = S.scenarios.find((s) => s.id === ds.scToggle);
    const data = { ...sc, enabled: t.checked }; delete data.running; delete data.last_run;
    api("/api/scenarios", { body: data }).then(loadScenarios).catch((err) => toast(err.message, true));
  } else if (ds.colorInput !== undefined) {
    act(ds.id, ds.model === "rgb" ? { "color_setting.rgb": t.value } : { "color_setting.hsv": hexToHsv(t.value) }, true);
  } else if (ds.z2mBool) act(ds.id, { ["z2m." + ds.z2mBool]: JSON.parse(t.checked ? ds.on : ds.off) }, true);
  else if (ds.z2mNum) act(ds.id, { ["z2m." + ds.z2mNum]: Number(t.value) }, true);
  else if (ds.range) {
    S.dragging = false;
    act(ds.id, { [ds.range]: Number(t.value) }, true);
  }
});
document.addEventListener("input", (e) => {
  const ds = e.target.dataset;
  if (ds.range) {
    S.dragging = true;
    const lbl = e.target.closest(".ctl")?.querySelector("label span:last-child");
    if (lbl) lbl.textContent = e.target.value;
    clearTimeout(debounce[ds.range]);
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "pw") $("[data-login]").click();
  if (e.key === "Escape") closeSheet();
});

// ---------------------------------------------------------------- запуск
async function refreshTab() {
  try {
    if (S.tab === "scenarios") await loadScenarios();
    else if (S.tab === "log") await loadLog();
    else if (S.tab === "settings") await loadStatus();
    render();
  } catch (_) {}
}
async function refreshAll() {
  try {
    await Promise.all([loadDevices(), loadStatus()]);
    await refreshTab();
    if (S.openDevice) renderDevice(S.openDevice);
  } catch (err) {
    if (err.message !== "Нужен вход") toast(err.message, true);
  }
}
// приложение вернулось из фона — обновляем данные
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
window.smarthomeRefresh = refreshAll;  // вызывается из iOS-приложения (pull-to-refresh)

render();
connectWS();
