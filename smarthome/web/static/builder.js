"use strict";
// Визуальный конструктор сценариев. Работает поверх того же JSON, что и сервер.
// Использует общие функции из app.js: S, $, esc, api, toast, nm, shortType, EVENTS, UNITS, SCENES,
// hsvToHex, hexToHsv, closeSheet, loadScenarios, render, sw.

const B = { sc: null, orig: null, json: false, open: false };

const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const TRIGGER_TYPES = { state: "Устройство изменилось", time: "В указанное время", interval: "Периодически" };
const CONDITION_TYPES = { state: "Состояние устройства", time: "Время суток" };
const ACTION_TYPES = {
  device: "Управлять устройством", speak: "Сказать на колонке", command: "Команда колонке",
  delay: "Подождать", yandex_scenario: "Сценарий Яндекса", scenario: "Мой сценарий", log: "Запись в журнал",
};
const MODES = { single: "Не запускать повторно", restart: "Перезапустить", parallel: "Запустить параллельно" };
const OPS = { eq: "равно", ne: "не равно", gt: "больше", lt: "меньше", ge: "не меньше", le: "не больше" };

// ---------------------------------------------------------------- устройства и их ключи
function findDev(ref) {
  if (!ref) return null;
  if (S.devices[ref]) return S.devices[ref];
  const n = String(ref).toLowerCase();
  return Object.values(S.devices).find((d) => d.name.toLowerCase() === n) || null;
}

function houseName(id) {
  return (S.households || []).find((h) => h.id === id)?.name || "";
}

function deviceOptions(selected, onlySpeakers = false) {
  const devs = Object.values(S.devices).filter((d) => !onlySpeakers || d.is_speaker);
  const multiHouse = (S.households || []).length > 1;
  const groups = {};
  for (const d of devs) {
    const g = (multiHouse ? houseName(d.household_id) + " · " : "") + (d.room_name || "Без комнаты");
    (groups[g] ||= []).push(d);
  }
  const cur = findDev(selected);
  let html = `<option value="">— выберите —</option>`;
  if (selected && !cur) html += `<option value="${esc(selected)}" selected>${esc(selected)} (не найдено)</option>`;
  for (const g of Object.keys(groups).sort((a, b) => a.localeCompare(b, "ru"))) {
    html += `<optgroup label="${esc(g)}">` + groups[g].sort((a, b) => a.name.localeCompare(b.name, "ru")).map((d) =>
      `<option value="${esc(d.id)}" ${cur && cur.id === d.id ? "selected" : ""}>${esc(d.name)}</option>`).join("") + `</optgroup>`;
  }
  return html;
}

/** Все ключи устройства с описанием: что это, какие значения допустимы, можно ли менять. */
function deviceKeys(d) {
  if (!d) return [];
  const out = [];
  for (const c of d.capabilities) {
    const t = shortType(c.type), p = c.parameters || {};
    if (t === "on_off") out.push({ key: "on_off.on", label: "Питание", kind: "bool", settable: true });
    else if (t === "range") {
      const r = p.range || {};
      out.push({ key: `range.${p.instance}`, label: nm(p.instance), kind: "number", settable: true,
        min: r.min, max: r.max, step: r.precision || 1, unit: UNITS[p.unit] || "", relative: true });
    } else if (t === "color_setting") {
      if (p.temperature_k) out.push({ key: "color_setting.temperature_k", label: "Цветовая температура", kind: "number",
        settable: true, min: p.temperature_k.min, max: p.temperature_k.max, step: 100, unit: "K" });
      if (p.color_model) out.push({ key: `color_setting.${p.color_model === "rgb" ? "rgb" : "hsv"}`, label: "Цвет",
        kind: p.color_model === "rgb" ? "rgb" : "hsv", settable: true });
      const scenes = p.color_scene?.scenes || [];
      if (scenes.length) out.push({ key: "color_setting.scene", label: "Сцена", kind: "enum", settable: true,
        options: scenes.map((s) => ({ v: s.id, l: SCENES[s.id] || s.id })) });
    } else if (t === "mode") {
      out.push({ key: `mode.${p.instance}`, label: nm(p.instance), kind: "enum", settable: true,
        options: (p.modes || []).map((m) => ({ v: m.value, l: nm(m.value) })) });
    } else if (t === "toggle") out.push({ key: `toggle.${p.instance}`, label: nm(p.instance), kind: "bool", settable: true });
  }
  for (const p of d.properties) {
    const t = shortType(p.type), inst = p.parameters?.instance;
    if (t === "float") out.push({ key: `float.${inst}`, label: nm(inst), kind: "number", unit: UNITS[p.parameters.unit] || "" });
    else if (t === "event") out.push({ key: `event.${inst}`, label: nm(inst), kind: "enum",
      options: (p.parameters.events || []).map((e) => ({ v: e.value, l: EVENTS[e.value] || e.value })) });
  }
  for (const f of d.z2m_settable || []) {
    out.push({ key: `z2m.${f.property}`, label: (f.label || f.property) + " (Zigbee)", settable: true,
      kind: f.type === "binary" ? "enumraw" : f.type === "enum" ? "enum" : "number",
      options: f.type === "binary" ? [{ v: f.value_on, l: String(f.value_on) }, { v: f.value_off, l: String(f.value_off) }]
        : (f.values || []).map((v) => ({ v, l: String(v) })), min: f.value_min, max: f.value_max });
  }
  return out;
}

const keyInfo = (d, key) => deviceKeys(d).find((k) => k.key === key) || { key, label: key, kind: "text" };

function defaultValue(info) {
  if (info.kind === "bool") return true;
  if (info.kind === "enum" || info.kind === "enumraw") return info.options?.[0]?.v ?? "";
  if (info.kind === "number") return info.min ?? 0;
  if (info.kind === "hsv") return { h: 30, s: 100, v: 100 };
  if (info.kind === "rgb") return "#ff8800";
  return "";
}

// ---------------------------------------------------------------- поля ввода
const P = (...parts) => parts.join("/");

function keySelect(d, path, selected, settableOnly) {
  const keys = deviceKeys(d).filter((k) => !settableOnly || k.settable);
  if (!keys.length) return `<select disabled><option>нет параметров</option></select>`;
  return `<select data-b="${path}">` + (selected && !keys.some((k) => k.key === selected)
    ? `<option value="${esc(selected)}" selected>${esc(selected)}</option>` : "") +
    keys.map((k) => `<option value="${esc(k.key)}" ${k.key === selected ? "selected" : ""}>${esc(k.label)}</option>`).join("") + `</select>`;
}

function valueInput(info, value, path) {
  if (info.kind === "bool") {
    return `<select data-b="${path}" data-t="bool">
      <option value="true" ${value === true ? "selected" : ""}>Вкл</option>
      <option value="false" ${value === false ? "selected" : ""}>Выкл</option></select>`;
  }
  if (info.kind === "enum" || info.kind === "enumraw") {
    const opts = info.options || [];
    const has = opts.some((o) => o.v === value);
    return `<select data-b="${path}" data-t="json">` +
      (!has && value !== undefined && value !== null && value !== "" ? `<option value='${esc(JSON.stringify(value))}' selected>${esc(value)}</option>` : "") +
      opts.map((o) => `<option value='${esc(JSON.stringify(o.v))}' ${o.v === value ? "selected" : ""}>${esc(o.l)}</option>`).join("") + `</select>`;
  }
  if (info.kind === "hsv") {
    const hex = value && typeof value === "object" ? hsvToHex(value) : "#ff8800";
    return `<input type="color" class="color-pick" data-b="${path}" data-t="hsv" value="${hex}">`;
  }
  if (info.kind === "rgb") {
    return `<input type="color" class="color-pick" data-b="${path}" data-t="str" value="${esc(value || "#ff8800")}">`;
  }
  if (info.kind === "number") {
    return `<input type="text" inputmode="decimal" data-b="${path}" data-t="numrel" value="${esc(value ?? "")}"
      placeholder="${info.relative ? "50, +5 или -5" : ""}${info.min !== undefined ? ` (${info.min}…${info.max})` : ""}">` +
      (info.unit ? `<span class="muted small" style="align-self:center">${esc(info.unit)}</span>` : "");
  }
  return `<input type="text" data-b="${path}" data-t="json" value='${esc(typeof value === "string" ? value : JSON.stringify(value ?? ""))}'>`;
}

function daysChips(path, days) {
  const set = new Set(days || []);
  return `<div class="chips">` + DAYS.map((l, i) =>
    `<button class="chip ${set.has(i + 1) ? "active" : ""}" data-bday="${path}" data-day="${i + 1}">${l}</button>`).join("") +
    `<span class="muted small" style="align-self:center">${set.size ? "" : "каждый день"}</span></div>`;
}

function rowHead(list, i, typeSel) {
  const n = B.sc[list].length;
  return `<div class="brow-head">${typeSel}
    <button class="btn small" data-bact="up" data-list="${list}" data-i="${i}" ${i === 0 ? "disabled" : ""}>↑</button>
    <button class="btn small" data-bact="down" data-list="${list}" data-i="${i}" ${i === n - 1 ? "disabled" : ""}>↓</button>
    <button class="btn small danger" data-bact="del" data-list="${list}" data-i="${i}">✕</button></div>`;
}

function typeSelect(path, types, cur) {
  return `<select data-b="${path}" data-retype>` +
    Object.entries(types).map(([v, l]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${l}</option>`).join("") + `</select>`;
}

// ---------------------------------------------------------------- строки
function triggerRow(t, i) {
  const base = P("triggers", i);
  let body = "";
  if (t.type === "state") {
    const d = findDev(t.device);
    const info = keyInfo(d, t.key);
    const cmp = t.above != null ? "above" : t.below != null ? "below" : t.to !== undefined && t.to !== null ? "to" : "any";
    body = `<div class="bline"><span class="blabel">Устройство</span><select data-b="${P(base, "device")}" data-redev>${deviceOptions(t.device)}</select></div>
      <div class="bline"><span class="blabel">Параметр</span>${keySelect(d, P(base, "key"), t.key, false)}</div>
      <div class="bline"><span class="blabel">Когда</span><select data-bcmp="${base}">
        <option value="to" ${cmp === "to" ? "selected" : ""}>стало равно</option>
        ${info.kind === "number" ? `<option value="above" ${cmp === "above" ? "selected" : ""}>стало больше</option>
        <option value="below" ${cmp === "below" ? "selected" : ""}>стало меньше</option>` : ""}
        <option value="any" ${cmp === "any" ? "selected" : ""}>любое изменение</option></select></div>
      ${cmp === "any" ? "" : `<div class="bline"><span class="blabel">Значение</span>${
        cmp === "to" ? valueInput(info, t.to, P(base, "to"))
          : `<input type="text" inputmode="decimal" data-b="${P(base, cmp)}" data-t="num" value="${esc(t[cmp] ?? "")}">${info.unit ? `<span class="muted small" style="align-self:center">${esc(info.unit)}</span>` : ""}`}</div>`}`;
  } else if (t.type === "time") {
    body = `<div class="bline"><span class="blabel">Время</span><input type="time" data-b="${P(base, "at")}" data-t="str" value="${esc(t.at || "")}"></div>
      <div class="bline"><span class="blabel">Дни</span>${daysChips(P(base, "days"), t.days)}</div>`;
  } else if (t.type === "interval") {
    body = `<div class="bline"><span class="blabel">Каждые</span><input type="number" min="1" data-b="${P(base, "seconds")}" data-t="min"
      value="${t.seconds ? Math.round(t.seconds / 60 * 100) / 100 : ""}"><span class="muted small" style="align-self:center">мин</span></div>`;
  }
  return `<div class="brow">${rowHead("triggers", i, typeSelect(P(base, "type"), TRIGGER_TYPES, t.type))}${body}</div>`;
}

function conditionRow(c, i) {
  const base = P("conditions", i);
  let body = "";
  if (c.type === "state") {
    const d = findDev(c.device);
    const info = keyInfo(d, c.key);
    const ops = info.kind === "number" ? OPS : { eq: OPS.eq, ne: OPS.ne };
    body = `<div class="bline"><span class="blabel">Устройство</span><select data-b="${P(base, "device")}" data-redev>${deviceOptions(c.device)}</select></div>
      <div class="bline"><span class="blabel">Параметр</span>${keySelect(d, P(base, "key"), c.key, false)}</div>
      <div class="bline"><span class="blabel">Условие</span><select data-b="${P(base, "op")}" data-t="str">${Object.entries(ops).map(([v, l]) =>
        `<option value="${v}" ${v === (c.op || "eq") ? "selected" : ""}>${l}</option>`).join("")}</select>${valueInput(info, c.value, P(base, "value"))}</div>`;
  } else if (c.type === "time") {
    body = `<div class="bline"><span class="blabel">С</span><input type="time" data-b="${P(base, "after")}" data-t="str" value="${esc(c.after || "")}">
      <span class="blabel" style="min-width:auto">до</span><input type="time" data-b="${P(base, "before")}" data-t="str" value="${esc(c.before || "")}"></div>
      <div class="bline"><span class="blabel">Дни</span>${daysChips(P(base, "days"), c.days)}</div>`;
  } else {
    body = `<div class="muted small">Сложное условие (${esc(c.type)}) — редактируется в JSON.</div>`;
  }
  return `<div class="brow">${rowHead("conditions", i, typeSelect(P(base, "type"), CONDITION_TYPES, c.type))}${body}</div>`;
}

function actionRow(a, i) {
  const base = P("actions", i);
  let body = "";
  if (a.type === "device") {
    const d = findDev(a.device);
    const entries = Object.entries(a.set || {});
    body = `<div class="bline"><span class="blabel">Устройство</span><select data-b="${P(base, "device")}" data-redev>${deviceOptions(a.device)}</select></div>` +
      entries.map(([k, v], j) => {
        const info = keyInfo(d, k);
        return `<div class="bline"><select data-bsetkey="${base}" data-old="${esc(k)}">${deviceKeys(d).filter((x) => x.settable).map((x) =>
          `<option value="${esc(x.key)}" ${x.key === k ? "selected" : ""}>${esc(x.label)}</option>`).join("") ||
          `<option>${esc(k)}</option>`}</select>${valueInput(info, v, P(base, "set", k))}
          <button class="btn small danger" data-bact="unset" data-path="${base}" data-k="${esc(k)}">✕</button></div>`;
      }).join("") +
      (d ? `<button class="btn small" data-bact="addset" data-path="${base}">+ параметр</button>` : "");
  } else if (a.type === "speak" || a.type === "command") {
    const len = (a.text || "").length;
    body = `<div class="bline"><span class="blabel">Колонка</span><select data-b="${P(base, "device")}" data-t="str">${deviceOptions(a.device, true)}</select></div>
      <div class="bline"><span class="blabel">${a.type === "speak" ? "Текст" : "Команда"}</span><input type="text" maxlength="100"
        data-b="${P(base, "text")}" data-t="str" value="${esc(a.text || "")}" placeholder="${a.type === "speak" ? "Дверь открыта" : "включи радио"}"></div>
      <div class="muted small">${len}/100 символов${a.type === "command" ? " · выполняется как голосовая команда Алисе" : ""}</div>`;
  } else if (a.type === "delay") {
    const s = a.seconds || 0, useMin = s >= 60 && s % 60 === 0;
    body = `<div class="bline"><span class="blabel">Время</span><input type="number" min="0" data-b="${P(base, "seconds")}" data-t="${useMin ? "min" : "num"}"
      value="${useMin ? s / 60 : s}"><select class="unit" data-bdelayunit="${base}"><option value="s" ${useMin ? "" : "selected"}>сек</option>
      <option value="m" ${useMin ? "selected" : ""}>мин</option></select></div>`;
  } else if (a.type === "yandex_scenario") {
    body = `<div class="bline"><span class="blabel">Сценарий</span><select data-b="${P(base, "scenario")}" data-t="str"><option value="">— выберите —</option>${
      (S.yaScenarios || []).map((s) => `<option value="${esc(s.id)}" ${a.scenario === s.id || a.scenario === s.name ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>`;
  } else if (a.type === "scenario") {
    body = `<div class="bline"><span class="blabel">Сценарий</span><select data-b="${P(base, "scenario")}" data-t="str"><option value="">— выберите —</option>${
      (S.scenarios || []).filter((s) => s.id !== B.sc.id).map((s) => `<option value="${esc(s.id)}" ${a.scenario === s.id || a.scenario === s.name ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>`;
  } else if (a.type === "log") {
    body = `<div class="bline"><span class="blabel">Текст</span><input type="text" data-b="${P(base, "message")}" data-t="str" value="${esc(a.message || "")}"></div>`;
  }
  return `<div class="brow">${rowHead("actions", i, typeSelect(P(base, "type"), ACTION_TYPES, a.type))}${body}</div>`;
}

// ---------------------------------------------------------------- отрисовка
function renderBuilder() {
  const sc = B.sc;
  let html = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
      <div style="font-size:20px;font-weight:700;flex:1">${B.orig ? "Сценарий" : "Новый сценарий"}</div>
      <button class="chip ${B.json ? "" : "active"}" data-bact="visual">Конструктор</button>
      <button class="chip ${B.json ? "active" : ""}" data-bact="json">JSON</button></div>`;
  if (B.json) {
    html += `<textarea id="sc-json" rows="22" spellcheck="false" autocapitalize="off" autocorrect="off">${esc(JSON.stringify(clean(sc), null, 2))}</textarea>`;
  } else {
    html += `<div class="bline"><input type="text" data-b="name" data-t="str" value="${esc(sc.name || "")}" placeholder="Название сценария"></div>
      <div class="bline"><span class="blabel">Включён</span>${sw(sc.enabled !== false, `data-b="enabled" data-t="check"`)}</div>
      <div class="bline"><span class="blabel">Если уже идёт</span><select data-b="mode" data-t="str">${Object.entries(MODES).map(([v, l]) =>
        `<option value="${v}" ${v === (sc.mode || "single") ? "selected" : ""}>${l}</option>`).join("")}</select></div>

      <h2>Когда</h2>${sc.triggers.map(triggerRow).join("") || `<div class="muted small" style="margin:0 2px 8px">Без триггеров сценарий запускается только вручную.</div>`}
      <button class="btn small" data-bact="add" data-list="triggers">+ Триггер</button>

      <h2>Если <span class="muted small">(необязательно)</span></h2>${sc.conditions.map(conditionRow).join("")}
      <button class="btn small" data-bact="add" data-list="conditions">+ Условие</button>

      <h2>Сделать</h2>${sc.actions.map(actionRow).join("")}
      <button class="btn small" data-bact="add" data-list="actions">+ Действие</button>`;
  }
  html += `<div class="btns bsave">
      <button class="btn primary" data-bact="save">Сохранить</button>
      ${B.orig ? `<button class="btn" data-bact="run">▶ Запустить</button>
        <button class="btn danger" data-bact="delete">Удалить</button>` : ""}</div>`;
  const content = $("#sheet-content");
  const scroll = content.parentElement.scrollTop;
  content.innerHTML = html;
  content.parentElement.scrollTop = scroll;
}

function clean(sc) {
  const out = JSON.parse(JSON.stringify(sc));
  delete out.running; delete out.last_run;
  return out;
}

function openBuilder(id) {
  const sc = id ? S.scenarios.find((s) => s.id === id) : null;
  B.orig = sc ? clean(sc) : null;
  B.sc = sc ? clean(sc) : { name: "", enabled: true, mode: "single", triggers: [], conditions: [], actions: [] };
  for (const k of ["triggers", "conditions", "actions"]) B.sc[k] ||= [];
  B.json = false;
  B.open = true;
  S.openDevice = null;
  renderBuilder();
  $("#sheet").classList.remove("hidden");
}

// ---------------------------------------------------------------- изменение данных
function getAt(obj, path) {
  return path.split("/").reduce((o, k) => (o == null ? o : o[k]), obj);
}
function setAt(obj, path, value) {
  const parts = path.split("/"), last = parts.pop();
  const target = parts.reduce((o, k) => o[k], obj);
  if (value === undefined) delete target[last]; else target[last] = value;
}

function parseValue(el) {
  const t = el.dataset.t, v = el.value;
  if (t === "bool") return v === "true";
  if (t === "check") return el.checked;
  if (t === "str") return v;
  if (t === "num") return v === "" ? undefined : Number(String(v).replace(",", "."));
  if (t === "min") return v === "" ? undefined : Math.round(Number(String(v).replace(",", ".")) * 60);
  if (t === "numrel") {
    const s = String(v).trim().replace(",", ".");
    if (/^[+-]/.test(s)) return s;  // относительное изменение: "+5"
    return s === "" ? "" : Number(s);
  }
  if (t === "hsv") return hexToHsv(v);
  if (t === "json") { try { return JSON.parse(v); } catch (_) { return v; } }
  return v;
}

const NEW = {
  triggers: () => ({ type: "state", device: "", key: "" }),
  conditions: () => ({ type: "time", after: "18:00", before: "07:00" }),
  actions: () => ({ type: "device", device: "", set: {} }),
};
const RETYPE = {
  triggers: { state: NEW.triggers, time: () => ({ type: "time", at: "07:00" }), interval: () => ({ type: "interval", seconds: 600 }) },
  conditions: { state: () => ({ type: "state", device: "", key: "", op: "eq" }), time: NEW.conditions },
  actions: {
    device: NEW.actions, speak: () => ({ type: "speak", device: "", text: "" }), command: () => ({ type: "command", device: "", text: "" }),
    delay: () => ({ type: "delay", seconds: 60 }), yandex_scenario: () => ({ type: "yandex_scenario", scenario: "" }),
    scenario: () => ({ type: "scenario", scenario: "" }), log: () => ({ type: "log", message: "" }),
  },
};

function onBuilderChange(el) {
  const path = el.dataset.b;
  if (el.hasAttribute("data-retype")) {
    const [list, i] = path.split("/");
    B.sc[list][i] = RETYPE[list][el.value]();
  } else if (el.hasAttribute("data-redev")) {
    const row = getAt(B.sc, path.split("/").slice(0, 2).join("/"));
    row.device = el.value;
    const d = findDev(el.value);
    if ("key" in row) {
      const keys = deviceKeys(d);
      row.key = keys[0]?.key || "";
      delete row.to; delete row.above; delete row.below;
      if ("op" in row) row.value = defaultValue(keys[0] || {});      // условие
      else row.to = defaultValue(keys[0] || {});                    // триггер: «стало равно»
    }
    if (row.type === "device") {
      const first = deviceKeys(d).find((k) => k.settable);
      row.set = first ? { [first.key]: defaultValue(first) } : {};
    }
  } else if (/^(triggers|conditions)\/\d+\/key$/.test(path)) {
    const row = getAt(B.sc, path.replace(/\/key$/, ""));
    row.key = el.value;
    const info = keyInfo(findDev(row.device), row.key);
    if (row.type === "state" && path.startsWith("triggers")) {
      delete row.above; delete row.below;
      row.to = defaultValue(info);
    } else row.value = defaultValue(info);
  } else {
    setAt(B.sc, path, parseValue(el));
  }
}

document.addEventListener("input", (e) => {
  const el = e.target;
  if (!B.open || !el.dataset.b || el.tagName === "SELECT") return;
  if (el.type === "checkbox") return;
  setAt(B.sc, el.dataset.b, parseValue(el));
  // счётчик символов для текста колонки
  const counter = el.closest(".brow")?.querySelector(".muted.small:last-child");
  if (counter && el.maxLength === 100) counter.textContent = counter.textContent.replace(/^\d+/, el.value.length);
});

document.addEventListener("change", (e) => {
  const el = e.target;
  if (!B.open) return;
  if (el.dataset.b) {
    onBuilderChange(el);
    if (el.tagName === "SELECT" || el.type === "checkbox" || el.type === "color") renderBuilder();
  } else if (el.dataset.bcmp) {
    const row = getAt(B.sc, el.dataset.bcmp);
    const info = keyInfo(findDev(row.device), row.key);
    delete row.to; delete row.above; delete row.below;
    if (el.value === "to") row.to = defaultValue(info);
    else if (el.value === "above" || el.value === "below") row[el.value] = info.min ?? 0;
    renderBuilder();
  } else if (el.dataset.bsetkey) {
    const row = getAt(B.sc, el.dataset.bsetkey);
    const entries = Object.entries(row.set || {});
    const info = keyInfo(findDev(row.device), el.value);
    row.set = Object.fromEntries(entries.map(([k, v]) => k === el.dataset.old ? [el.value, defaultValue(info)] : [k, v]));
    renderBuilder();
  } else if (el.dataset.bdelayunit) {
    const row = getAt(B.sc, el.dataset.bdelayunit);
    const n = Number(el.closest(".bline").querySelector("input").value) || 0;
    row.seconds = el.value === "m" ? n * 60 : n;
    renderBuilder();
  }
});

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-bact],[data-bday]");
  if (!el || !B.open) return;
  e.preventDefault();
  const ds = el.dataset;
  if (ds.bday) {
    const row = getAt(B.sc, ds.bday.replace(/\/days$/, ""));
    const set = new Set(row.days || []);
    const d = Number(ds.day);
    set.has(d) ? set.delete(d) : set.add(d);
    row.days = [...set].sort();
    if (!row.days.length) delete row.days;
    return renderBuilder();
  }
  const list = ds.list, i = Number(ds.i);
  try {
    switch (ds.bact) {
      case "add": B.sc[list].push(NEW[list]()); break;
      case "del": B.sc[list].splice(i, 1); break;
      case "up": [B.sc[list][i - 1], B.sc[list][i]] = [B.sc[list][i], B.sc[list][i - 1]]; break;
      case "down": [B.sc[list][i + 1], B.sc[list][i]] = [B.sc[list][i], B.sc[list][i + 1]]; break;
      case "addset": {
        const row = getAt(B.sc, ds.path);
        const free = deviceKeys(findDev(row.device)).find((k) => k.settable && !(k.key in (row.set || {})));
        if (free) row.set = { ...(row.set || {}), [free.key]: defaultValue(free) };
        else toast("Все параметры уже добавлены");
        break;
      }
      case "unset": { const row = getAt(B.sc, ds.path); delete row.set[ds.k]; break; }
      case "json": B.json = true; break;
      case "visual": {
        if (B.json) {
          try { B.sc = JSON.parse($("#sc-json").value); }
          catch (err) { toast("Ошибка JSON: " + err.message, true); return; }
          for (const k of ["triggers", "conditions", "actions"]) B.sc[k] ||= [];
        }
        B.json = false; break;
      }
      case "save": {
        if (B.json) {
          try { B.sc = JSON.parse($("#sc-json").value); }
          catch (err) { toast("Ошибка JSON: " + err.message, true); return; }
        }
        if (!String(B.sc.name || "").trim()) { toast("Укажите название сценария", true); return; }
        await api("/api/scenarios", { body: clean(B.sc) });
        toast("Сохранено");
        B.open = false; closeSheet(); await loadScenarios(); render();
        return;
      }
      case "run": await api(`/api/scenarios/${encodeURIComponent(B.orig.id)}/run`, { body: {} }); toast("Запущено"); return;
      case "delete": {
        if (!confirm("Удалить сценарий?")) return;
        await api(`/api/scenarios/${encodeURIComponent(B.orig.id)}`, { method: "DELETE" });
        B.open = false; closeSheet(); await loadScenarios(); render();
        return;
      }
    }
  } catch (err) {
    toast(err.message, true);
    return;
  }
  renderBuilder();
});

// ---------------------------------------------------------------- описание для списка
function describeScenario(s) {
  const dn = (ref) => findDev(ref)?.name || ref || "?";
  const val = (d, key, v) => {
    const info = keyInfo(findDev(d), key);
    if (info.kind === "bool") return v ? "вкл" : "выкл";
    const opt = info.options?.find((o) => o.v === v);
    return opt ? opt.l : typeof v === "object" ? "цвет" : String(v);
  };
  const trig = (s.triggers || []).map((t) => {
    if (t.type === "time") return `в ${t.at}${t.days?.length ? " (" + t.days.map((d) => DAYS[d - 1]).join(",") + ")" : ""}`;
    if (t.type === "interval") return `каждые ${Math.round(t.seconds / 60)} мин`;
    const label = keyInfo(findDev(t.device), t.key).label;
    if (t.above != null) return `${dn(t.device)}: ${label} > ${t.above}`;
    if (t.below != null) return `${dn(t.device)}: ${label} < ${t.below}`;
    if (t.to !== undefined && t.to !== null) return `${dn(t.device)}: ${label} → ${val(t.device, t.key, t.to)}`;
    return `${dn(t.device)}: ${label}`;
  }).join(", ") || "только вручную";
  const acts = (s.actions || []).map((a) => a.type === "device" ? dn(a.device)
    : a.type === "speak" ? `🔊 «${a.text || ""}»` : a.type === "command" ? `🗣 ${a.text || ""}`
    : a.type === "delay" ? `⏱ ${a.seconds} с` : ACTION_TYPES[a.type] || a.type).join(" → ");
  return `${trig}${acts ? " ⇒ " + acts : ""}`;
}
