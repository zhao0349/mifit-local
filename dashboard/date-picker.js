/* Framework-free date input enhancement, including inputs in nested dialogs. */
(() => {
  "use strict";
  if (window.MiFitDatePicker) return;
  // A saved offline page may have been cloned while the picker was open.
  document.querySelectorAll("[data-date-picker-transient]").forEach(node => node.remove());
  const enhanced = new WeakSet();
  const mobile = matchMedia("(max-width:690px)");
  const ROW_HEIGHT = 44;
  const MIN_DATE = "0001-01-01", MAX_DATE = "9999-12-31";
  let input = null, draft = null, viewYear = 0, viewMonth = 0;
  let lower = MIN_DATE, upper = MAX_DATE;
  let wheelTimers = new Map();
  const wheelValues = new Map();
  const dialog = document.createElement("dialog");
  dialog.id = "date-picker-dialog";
  dialog.dataset.datePickerTransient = "";
  dialog.setAttribute("aria-labelledby", "date-picker-title");
  dialog.innerHTML = `
    <div class="date-picker-handle" aria-hidden="true"></div>
    <div class="date-picker-heading"><h2 id="date-picker-title">选择日期</h2><button id="date-picker-close" type="button" aria-label="取消选择日期">×</button></div>
    <div id="date-picker-calendar">
      <div class="date-picker-nav"><button id="date-picker-prev" type="button" aria-label="上个月">‹</button>
        <div class="date-picker-month-label"><input id="date-picker-year" type="number" min="1" max="9999" aria-label="年份"><span>年</span><select id="date-picker-month" aria-label="月份"></select></div>
        <button id="date-picker-next" type="button" aria-label="下个月">›</button></div>
      <div class="date-picker-weekdays" aria-hidden="true"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
      <div class="date-picker-grid" id="date-picker-grid" role="grid" aria-label="日期"></div>
    </div>
    <div id="date-picker-mobile" hidden>
      <div class="date-picker-wheel-labels" aria-hidden="true"><span>年</span><span>月</span><span>日</span></div>
      <div class="date-picker-wheels">
        <div class="date-picker-wheel" id="date-picker-wheel-year" role="spinbutton" tabindex="0" aria-label="年" aria-orientation="vertical"></div>
        <div class="date-picker-wheel" id="date-picker-wheel-month" role="spinbutton" tabindex="0" aria-label="月" aria-orientation="vertical"></div>
        <div class="date-picker-wheel" id="date-picker-wheel-day" role="spinbutton" tabindex="0" aria-label="日" aria-orientation="vertical"></div>
      </div><p class="date-picker-wheel-hint">上下滑动选择，确认后生效</p>
    </div>
    <p class="date-picker-preview" id="date-picker-preview" aria-live="polite"></p>
    <div class="date-picker-actions"><button id="date-picker-today" type="button">今天</button><button id="date-picker-clear" type="button">清除</button><button id="date-picker-cancel" type="button">取消</button><button id="date-picker-confirm" type="button">确认</button></div>`;
  document.body.append(dialog);
  const $ = id => document.getElementById(id);
  const parts = ["year", "month", "day"];
  const wheels = parts.map(part => $("date-picker-wheel-" + part));
  for (let month = 1; month <= 12; month++) {
    const option = document.createElement("option");
    option.value = String(month);
    option.textContent = month + "月";
    $("date-picker-month").append(option);
  }

  function daysInMonth(year, month) {
    return [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28,
      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  }
  function parse(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return null;
    const [year, month, day] = value.split("-").map(Number);
    return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth(year, month)
      ? {year, month, day} : null;
  }
  function iso(value) {
    return String(value.year).padStart(4, "0") + "-" + String(value.month).padStart(2, "0") + "-" + String(value.day).padStart(2, "0");
  }
  function today() {
    return new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit"}).format(new Date());
  }
  function clamp(value) {
    const stamp = iso(value);
    return parse(stamp < lower ? lower : stamp > upper ? upper : stamp);
  }
  function utc(value) {
    // Date.UTC treats years 0..99 as 1900..1999; setUTCFullYear preserves them.
    const date = new Date(0);
    date.setUTCFullYear(value.year, value.month - 1, value.day);
    date.setUTCHours(0, 0, 0, 0);
    return date;
  }
  function preview() {
    $("date-picker-preview").textContent = `${draft.year} 年 ${draft.month} 月 ${draft.day} 日`;
    $("date-picker-confirm").disabled = lower > upper;
    $("date-picker-today").disabled = today() < lower || today() > upper;
  }
  function position() {
    if (!dialog.open || !input) return;
    if (mobile.matches) {
      dialog.style.removeProperty("left");
      dialog.style.removeProperty("top");
      return;
    }
    const rect = input.getBoundingClientRect();
    const width = dialog.offsetWidth, height = dialog.offsetHeight;
    const available = window.visualViewport?.height || innerHeight;
    const top = rect.bottom + height + 8 <= available - 12 ? rect.bottom + 8 : rect.top - height - 8;
    dialog.style.left = Math.max(12, Math.min(rect.left, innerWidth - width - 12)) + "px";
    dialog.style.top = Math.max(12, Math.min(top, available - height - 12)) + "px";
  }

  function renderCalendar(focusDate = null) {
    $("date-picker-year").value = viewYear;
    $("date-picker-month").value = viewMonth;
    const monthStart = iso({year:viewYear, month:viewMonth, day:1});
    const monthEnd = iso({year:viewYear, month:viewMonth, day:daysInMonth(viewYear, viewMonth)});
    $("date-picker-prev").disabled = monthStart <= lower;
    $("date-picker-next").disabled = monthEnd >= upper;
    const grid = $("date-picker-grid");
    grid.replaceChildren();
    grid.setAttribute("aria-label", `${viewYear} 年 ${viewMonth} 月`);
    const padding = (utc({year:viewYear, month:viewMonth, day:1}).getUTCDay() + 6) % 7;
    for (let blank = 0; blank < padding; blank++) {
      const cell = document.createElement("span");
      cell.setAttribute("aria-hidden", "true");
      grid.append(cell);
    }
    let firstEnabled = null, selected = null;
    const currentDay = today();
    for (let day = 1; day <= daysInMonth(viewYear, viewMonth); day++) {
      const stamp = iso({year:viewYear, month:viewMonth, day});
      const button = document.createElement("button");
      button.type = "button";
      button.className = "date-picker-day";
      button.dataset.date = stamp;
      button.dataset.today = String(stamp === currentDay);
      button.setAttribute("role", "gridcell");
      button.setAttribute("aria-label", `${viewYear} 年 ${viewMonth} 月 ${day} 日`);
      button.setAttribute("aria-selected", String(stamp === iso(draft)));
      button.tabIndex = -1;
      button.textContent = day;
      button.disabled = stamp < lower || stamp > upper;
      if (!button.disabled && !firstEnabled) firstEnabled = button;
      if (!button.disabled && stamp === (focusDate || iso(draft))) selected = button;
      grid.append(button);
    }
    const target = selected || firstEnabled;
    if (target) target.tabIndex = 0;
    if (focusDate) target?.focus({preventScroll:true});
    preview();
    position();
  }
  function moveMonth(amount) {
    let month = viewMonth + amount, year = viewYear;
    if (month < 1) { month = 12; year--; }
    if (month > 12) { month = 1; year++; }
    if (year < 1 || year > 9999) return;
    viewYear = year; viewMonth = month;
    renderCalendar();
  }

  function valuesFor(part) {
    const min = parse(lower), max = parse(upper);
    let from = 1, to = 0;
    if (part === "year") { from = min.year; to = max.year; }
    if (part === "month") {
      from = draft.year === min.year ? min.month : 1;
      to = draft.year === max.year ? max.month : 12;
    }
    if (part === "day") {
      from = draft.year === min.year && draft.month === min.month ? min.day : 1;
      to = Math.min(daysInMonth(draft.year, draft.month), draft.year === max.year && draft.month === max.month ? max.day : 31);
    }
    return Array.from({length:Math.max(0, to - from + 1)}, (_, i) => from + i);
  }
  function markWheel(wheel, value) {
    wheel.querySelector('[data-selected="true"]')?.removeAttribute("data-selected");
    wheel.querySelector(`[data-value="${value}"]`)?.setAttribute("data-selected", "true");
    wheel.setAttribute("aria-valuenow", String(value));
    wheel.setAttribute("aria-valuetext", value + ({year:" 年", month:" 月", day:" 日"})[wheel.dataset.part]);
  }
  function renderWheels() {
    for (const [index, wheel] of wheels.entries()) {
      const part = parts[index], values = valuesFor(part);
      const old = wheelValues.get(wheel);
      if (!old || old[0] !== values[0] || old.at(-1) !== values.at(-1)) {
        const fragment = document.createDocumentFragment();
        for (const value of values) {
          const item = document.createElement("div");
          item.className = "date-picker-wheel-item";
          item.dataset.value = String(value);
          item.setAttribute("aria-hidden", "true");
          item.textContent = value;
          fragment.append(item);
        }
        wheel.replaceChildren(fragment);
        wheelValues.set(wheel, values);
      }
      wheel.dataset.part = part;
      wheel.setAttribute("aria-valuemin", values[0]);
      wheel.setAttribute("aria-valuemax", values.at(-1));
      clearTimeout(wheelTimers.get(wheel));
      markWheel(wheel, draft[part]);
      wheel.scrollTop = (draft[part] - values[0]) * ROW_HEIGHT;
    }
    preview();
  }
  function selectPart(part, value) {
    draft[part] = value;
    draft.day = Math.min(draft.day, daysInMonth(draft.year, draft.month));
    draft = clamp(draft);
    renderWheels();
  }
  function settleWheel(wheel) {
    if (!dialog.open || !mobile.matches) return;
    const values = wheelValues.get(wheel);
    const index = Math.max(0, Math.min(values.length - 1, Math.round(wheel.scrollTop / ROW_HEIGHT)));
    if (draft[wheel.dataset.part] !== values[index]) selectPart(wheel.dataset.part, values[index]);
  }
  for (const wheel of wheels) {
    wheel.addEventListener("scroll", () => {
      clearTimeout(wheelTimers.get(wheel));
      wheelTimers.set(wheel, setTimeout(() => settleWheel(wheel), 120));
    }, {passive:true});
    wheel.addEventListener("scrollend", () => settleWheel(wheel));
    wheel.addEventListener("click", event => {
      const item = event.target.closest(".date-picker-wheel-item");
      if (item) selectPart(wheel.dataset.part, Number(item.dataset.value));
    });
    wheel.addEventListener("keydown", event => {
      const values = wheelValues.get(wheel), value = draft[wheel.dataset.part];
      const changes = {ArrowUp:-1, ArrowDown:1, PageUp:-10, PageDown:10};
      let next;
      if (event.key in changes) next = value + changes[event.key];
      else if (event.key === "Home") next = values[0];
      else if (event.key === "End") next = values.at(-1);
      else return;
      event.preventDefault();
      selectPart(wheel.dataset.part, Math.max(values[0], Math.min(values.at(-1), next)));
    });
  }

  function renderMode() {
    dialog.dataset.mode = mobile.matches ? "wheel" : "calendar";
    $("date-picker-calendar").hidden = mobile.matches;
    $("date-picker-mobile").hidden = !mobile.matches;
    if (mobile.matches) renderWheels();
    else {
      viewYear = draft.year; viewMonth = draft.month;
      renderCalendar();
    }
    position();
  }
  function open(target) {
    if (!(target instanceof HTMLInputElement) || target.type !== "date" || target.disabled || target.readOnly) return;
    if (dialog.open) close();
    input = target;
    lower = parse(input.min) ? input.min : MIN_DATE;
    upper = parse(input.max) ? input.max : MAX_DATE;
    if (lower > upper) { input = null; return; }
    draft = clamp(parse(input.value) || parse(today()));
    const label = input.getAttribute("aria-label") || input.labels?.[0]?.textContent.trim() || "选择日期";
    $("date-picker-title").textContent = label.slice(0, 40);
    $("date-picker-clear").hidden = input.required;
    input.setAttribute("aria-expanded", "true");
    renderMode();
    dialog.showModal();
    // Layout must exist before setting a wheel's scroll position.
    if (mobile.matches) renderWheels();
    position();
    if (mobile.matches) wheels[0].focus({preventScroll:true});
    else $("date-picker-grid").querySelector('[tabindex="0"]')?.focus({preventScroll:true});
  }
  function close() {
    for (const timer of wheelTimers.values()) clearTimeout(timer);
    wheelTimers = new Map();
    const previous = input;
    input = null;
    if (dialog.open) dialog.close();
    previous?.setAttribute("aria-expanded", "false");
    if (previous?.isConnected && !previous.disabled) previous.focus({preventScroll:true});
  }
  function commit(value) {
    const target = input;
    if (!target || target.disabled || target.readOnly) { close(); return; }
    // Bounds may have changed while the modal was open (e.g. account/filter update).
    if (value && ((parse(target.min) && value < target.min) || (parse(target.max) && value > target.max))) {
      $("date-picker-preview").textContent = "可选日期范围已变化，请取消后重新选择。";
      return;
    }
    if (!value && target.required) return;
    const changed = target.value !== value;
    target.value = value;
    close();
    if (changed) {
      target.dispatchEvent(new Event("input", {bubbles:true}));
      target.dispatchEvent(new Event("change", {bubbles:true}));
    }
  }

  $("date-picker-grid").addEventListener("click", event => {
    const button = event.target.closest("button[data-date]");
    if (!button || button.disabled) return;
    draft = parse(button.dataset.date);
    renderCalendar(iso(draft));
  });
  $("date-picker-grid").addEventListener("keydown", event => {
    const button = event.target.closest("button[data-date]");
    if (!button) return;
    const value = parse(button.dataset.date), date = utc(value);
    const deltas = {ArrowLeft:-1, ArrowRight:1, ArrowUp:-7, ArrowDown:7};
    if (event.key in deltas) date.setUTCDate(date.getUTCDate() + deltas[event.key]);
    else if (event.key === "Home") date.setUTCDate(date.getUTCDate() - (date.getUTCDay() + 6) % 7);
    else if (event.key === "End") date.setUTCDate(date.getUTCDate() + 6 - (date.getUTCDay() + 6) % 7);
    else if (event.key === "PageUp" || event.key === "PageDown") {
      const offset = event.key === "PageUp" ? -1 : 1;
      date.setUTCDate(1);
      if (event.shiftKey) date.setUTCFullYear(date.getUTCFullYear() + offset);
      else date.setUTCMonth(date.getUTCMonth() + offset);
      date.setUTCDate(Math.min(value.day, daysInMonth(date.getUTCFullYear(), date.getUTCMonth() + 1)));
    } else return;
    event.preventDefault();
    const next = {year:date.getUTCFullYear(), month:date.getUTCMonth() + 1, day:date.getUTCDate()};
    draft = next.year < 1 ? parse(lower) : next.year > 9999 ? parse(upper) : clamp(next);
    viewYear = draft.year; viewMonth = draft.month;
    renderCalendar(iso(draft));
  });
  $("date-picker-prev").addEventListener("click", () => moveMonth(-1));
  $("date-picker-next").addEventListener("click", () => moveMonth(1));
  $("date-picker-year").addEventListener("change", event => {
    viewYear = Math.max(parse(lower).year, Math.min(parse(upper).year, Number(event.target.value) || draft.year));
    renderCalendar();
  });
  $("date-picker-month").addEventListener("change", event => { viewMonth = Number(event.target.value); renderCalendar(); });
  $("date-picker-today").addEventListener("click", () => { draft = clamp(parse(today())); renderMode(); });
  $("date-picker-clear").addEventListener("click", () => commit(""));
  $("date-picker-confirm").addEventListener("click", () => {
    // Flush pending touch/scroll movement before committing the selected rows.
    if (mobile.matches) {
      for (const wheel of wheels) {
        const values = wheelValues.get(wheel);
        const index = Math.max(0, Math.min(values.length - 1, Math.round(wheel.scrollTop / ROW_HEIGHT)));
        draft[wheel.dataset.part] = values[index];
      }
      draft.day = Math.min(draft.day, daysInMonth(draft.year, draft.month));
      draft = clamp(draft);
    }
    commit(iso(draft));
  });
  for (const id of ["date-picker-cancel", "date-picker-close"]) $(id).addEventListener("click", close);
  dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
  let backdropPress = false;
  function outside(event) {
    const rect = dialog.getBoundingClientRect();
    return event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom);
  }
  dialog.addEventListener("pointerdown", event => { backdropPress = outside(event); });
  dialog.addEventListener("click", event => { if (backdropPress && outside(event)) close(); backdropPress = false; });
  dialog.addEventListener("keydown", event => {
    if (event.key !== "Tab") return;
    const focusable = Array.from(dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),[tabindex="0"]')).filter(node => node.getClientRects().length);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  function enhance(root = document) {
    const candidates = root.matches?.('input[type="date"]') ? [root] : [];
    candidates.push(...root.querySelectorAll?.('input[type="date"]') || []);
    for (const target of candidates) {
      if (enhanced.has(target)) continue;
      enhanced.add(target);
      target.classList.add("mifit-date-input");
      target.setAttribute("aria-haspopup", "dialog");
      target.setAttribute("aria-controls", dialog.id);
      target.setAttribute("aria-expanded", "false");
      target.setAttribute("aria-keyshortcuts", "Alt+ArrowDown Space");
      target.setAttribute("inputmode", "none");
      target.addEventListener("pointerdown", event => {
        if (!target.disabled && !target.readOnly && event.button === 0) event.preventDefault();
      });
      target.addEventListener("click", event => { event.preventDefault(); open(target); });
      target.addEventListener("keydown", event => {
        if (event.key === " " || (event.altKey && event.key === "ArrowDown")) {
          event.preventDefault(); event.stopPropagation(); open(target);
        }
      });
    }
  }
  enhance();
  new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) {
      if (node.nodeType === Node.ELEMENT_NODE && !dialog.contains(node)) enhance(node);
    }
    if (input && !input.isConnected) close();
  }).observe(document.body, {childList:true, subtree:true});
  mobile.addEventListener("change", () => { if (dialog.open) renderMode(); });
  window.addEventListener("resize", position);
  window.visualViewport?.addEventListener("resize", position);
  window.MiFitDatePicker = {enhance, open, close};
})();
