/* Self-contained detail UI; every runtime element lives inside the dialog. */
(function () {
  "use strict";
  const CATEGORIES = {
    activity: { label: "日常活动", short: "活动", metric: "steps", icon: "M3 12h4l3-8 4 16 3-8h4",
      description: "把每天的活动放在同一条时间线上，查看累计、变化与缺失记录。" },
    sleep: { label: "睡眠详情", short: "睡眠", metric: "sleep_minutes", icon: "M20 15.4A8.5 8.5 0 0 1 8.6 4a8.5 8.5 0 1 0 11.4 11.4Z",
      description: "按醒来日期查看主睡眠、小睡与评分；阶段分布只使用四个阶段均完整的样本。" },
    heart: { label: "心率详情", short: "心率", metric: "resting_hr", icon: "M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z",
      description: "区分每日平均、最小、最大和静息心率，保留没有样本的日期，不把缺失计作零。" },
    workouts: { label: "运动档案", short: "运动", metric: "workout_minutes", icon: "M4 8v8m4-11v14m8-14v14m4-11v8M8 12h8",
      description: "按类型查看运动记录、训练时长与距离。单次详情只展示档案中实际提供的指标。" },
    body: { label: "身体指标", short: "身体", metric: "weight_kg", icon: "M5 3h14v18H5zM8 7h8M12 7l2 3",
      description: "体重、体脂、血氧与压力分别查看。最新值来自所选范围内有记录的最后一天。" }
  };
  const STAGES = [
    ["deep", "深睡", "var(--sleep)"], ["light", "浅睡", "#9b91cc"],
    ["rem", "REM", "#c8a3bb"], ["awake", "清醒", "#93a9a0"]
  ];
  const PAGE_SIZE = 10;
  const SVG = "http://www.w3.org/2000/svg";
  let state = {}, analysis = null, opener = null, savedScroll = null;
  const oldDialog = document.getElementById("detail-dialog");
  if (oldDialog) oldDialog.remove();
  document.body.classList.remove("detail-open");

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function svg(tag, attrs = {}, text) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function button(text, id, action, className = "detail-button") {
    const node = el("button", className, text);
    node.type = "button";
    if (id) node.id = id;
    node.addEventListener("click", action);
    return node;
  }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function fmt(value, digits = 0) {
    return finite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
  }
  function meta(key = state.metric) { return window.MiFitDetailModel.metrics[key]; }
  function metricKeys() {
    return Object.keys(window.MiFitDetailModel.metrics).filter(key => meta(key).category === state.category);
  }
  function valueText(value, key = state.metric, withUnit = false) {
    if (!finite(value)) return "—";
    const definition = meta(key);
    const text = fmt(value, definition?.decimals ?? 1);
    return withUnit && definition?.unit ? text + " " + definition.unit : text;
  }
  function minutes(value) {
    if (!finite(value)) return "—";
    const rounded = Math.round(value);
    return rounded >= 60 ? Math.floor(rounded / 60) + " 小时 " + rounded % 60 + " 分" : rounded + " 分";
  }
  function pace(value) {
    if (!finite(value)) return "—";
    const rounded = Math.round(value);
    return Math.floor(rounded / 60) + "'" + String(rounded % 60).padStart(2, "0") + '"/km';
  }
  function dayLabel(date) { return date ? date.replaceAll("-", ".") : "—"; }
  function shiftDate(date, amount) {
    const parsed = new Date(date + "T00:00:00Z");
    parsed.setUTCDate(parsed.getUTCDate() + amount);
    return Number.isFinite(parsed.getTime()) ? parsed.toISOString().slice(0, 10) : "";
  }
  function notify(text) { window.MiFitDashboard?.notify?.(text); }
  function typeName(type) { return window.MiFitDashboard?.formatType?.(type) || String(type || "其他"); }
  function dataRange(data) {
    const dates = (data.days || []).map(item => item.date).concat(
      (data.workouts || []).map(item => item.date),
      Object.keys(data.intraday?.heart || {}), Object.keys(data.intraday?.sleep || {})
    ).filter(Boolean).sort();
    return { start: dates[0] || data.meta?.range?.start || "", end: dates.at(-1) || data.meta?.range?.end || "" };
  }
  function validRange(start, end) {
    const valid = value => typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      Number.isFinite(Date.parse(value + "T00:00:00Z")) &&
      new Date(value + "T00:00:00Z").toISOString().slice(0, 10) === value;
    if (!valid(start) || !valid(end) || start > end) return "请选择有效的日期范围，开始日期不能晚于结束日期。";
    if ((Date.parse(end) - Date.parse(start)) / 86400000 >= 3660) return "一次最多查看 10 年，请缩小日期范围。";
    return "";
  }
  function applyRange(start, end, preset = "") {
    const error = validRange(start, end);
    if (error) { state.error = error; render(); return; }
    Object.assign(state, { start, end, draftStart: start, draftEnd: end, preset, page: 0,
      error: "", workout: null, workoutOrigin: "" });
    render();
  }
  function revealPanel(panel) {
    if (!panel || !scroll.contains(panel)) return;
    panel.focus({ preventScroll: true });
    // scrollIntoView also scrolls overflow:hidden ancestors, including the
    // dialog. Move only the content pane so its header and bottom stay fixed.
    dialog.scrollTop = shell.scrollTop = 0;
    const viewport = scroll.getBoundingClientRect();
    const scale = viewport.height / scroll.offsetHeight || 1;
    const inset = parseFloat(getComputedStyle(scroll).paddingTop) || 0;
    const top = scroll.scrollTop + (panel.getBoundingClientRect().top - viewport.top) / scale
      - scroll.clientTop - inset;
    scroll.scrollTo({
      top: Math.max(0, Math.min(top, scroll.scrollHeight - scroll.clientHeight)),
      behavior: "instant"
    });
  }
  function revealWorkout() {
    revealPanel(document.getElementById("detail-single-workout"));
  }
  function changeMetric(key) {
    if (!meta(key) || meta(key).category !== state.category) return;
    state.metric = key;
    render();
  }
  function changeCategory(category) {
    state.category = category;
    state.metric = CATEGORIES[category].metric;
    state.page = 0;
    state.workout = null;
    render();
  }

  const dialog = el("dialog");
  dialog.id = "detail-dialog";
  dialog.setAttribute("aria-labelledby", "detail-title");
  const shell = el("div", "detail-shell");
  const header = el("header", "detail-header");
  const heading = el("div");
  heading.append(el("p", "detail-eyebrow", "YOUR HEALTH · 详细记录"));
  const title = el("h2");
  title.id = "detail-title";
  heading.append(title);
  const headerTools = el("div", "detail-header-tools");
  const save = button("保存离线副本", "detail-save-html", () => {
    document.getElementById("download-html")?.click();
  }, "detail-button detail-save");
  const closeButton = button("×", "detail-close", close, "detail-button detail-close");
  closeButton.setAttribute("aria-label", "关闭详情，返回总览");
  headerTools.append(save, closeButton);
  header.append(heading, headerTools);
  const nav = el("nav", "detail-nav");
  nav.setAttribute("aria-label", "健康详情分类");
  for (const [key, definition] of Object.entries(CATEGORIES)) {
    const item = button("", "detail-category-" + key, () => changeCategory(key), "");
    item.dataset.detailCategory = key;
    const icon = svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
    icon.append(svg("path", { d: definition.icon }));
    item.append(icon, el("span", "", definition.short));
    nav.append(item);
  }
  const scroll = el("div", "detail-scroll");
  shell.append(header, nav, scroll);
  dialog.append(shell);
  document.body.append(dialog);
  dialog.addEventListener("close", () => {
    document.body.classList.remove("detail-open");
    if (savedScroll) window.scrollTo({ left: savedScroll.x, top: savedScroll.y, behavior: "instant" });
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    opener = null;
    savedScroll = null;
  });
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom) close();
  });
  function close() { if (dialog.open) dialog.close(); }

  function renderMetrics() {
    const tabs = el("div", "detail-metrics");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "详情指标");
    const keys = metricKeys();
    keys.forEach((key, index) => {
      const tab = button(meta(key).label, "detail-metric-" + key, () => changeMetric(key), "");
      tab.dataset.detailMetric = key;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(key === state.metric));
      tab.tabIndex = key === state.metric ? 0 : -1;
      tab.addEventListener("keydown", event => {
        let next;
        if (event.key === "ArrowRight") next = (index + 1) % keys.length;
        if (event.key === "ArrowLeft") next = (index + keys.length - 1) % keys.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = keys.length - 1;
        if (next !== undefined) {
          event.preventDefault();
          state.metric = keys[next];
          render();
          document.getElementById("detail-metric-" + keys[next]).focus({ preventScroll: true });
        }
      });
      tabs.append(tab);
    });
    return tabs;
  }
  function renderFilters() {
    const fragment = document.createDocumentFragment();
    const toolbar = el("div", "detail-toolbar");
    const presets = el("div", "detail-presets");
    for (const [key, label] of [["7", "最近 7 天"], ["30", "30 天"], ["90", "90 天"], ["all", "全部"]]) {
      const item = button(label, "detail-range-" + key, () => {
        const all = dataRange(state.data);
        applyRange(key === "all" ? all.start :
          [all.start, shiftDate(all.end, -(Number(key) - 1))].filter(Boolean).sort().at(-1), all.end, key);
      }, "");
      item.dataset.detailRange = key;
      item.setAttribute("aria-pressed", String(state.preset === key));
      item.disabled = !dataRange(state.data).end;
      presets.append(item);
    }
    const range = el("div", "detail-date-range");
    for (const [id, label, property] of [["detail-start", "开始日期", "draftStart"], ["detail-end", "结束日期", "draftEnd"]]) {
      const wrapper = el("label", "", label);
      const input = el("input");
      input.type = "date";
      input.id = id;
      input.value = state[property] || "";
      input.addEventListener("input", () => { state[property] = input.value; });
      input.addEventListener("keydown", event => {
        if (event.key === "Enter") { event.preventDefault(); applyRange(state.draftStart, state.draftEnd); }
      });
      wrapper.append(input);
      range.append(wrapper);
    }
    range.append(button("应用", "detail-apply-range", () => {
      applyRange(state.draftStart, state.draftEnd);
    }));
    toolbar.append(presets, range);
    fragment.append(toolbar);
    const filterLine = el("div", "detail-filter-line");
    const grainLabel = el("label", "detail-select-label", "趋势粒度");
    const grain = el("select");
    grain.id = "detail-granularity";
    for (const [value, label] of [["day", "按日"], ["week", "按周"], ["month", "按月"]]) {
      const option = el("option", "", label);
      option.value = value;
      grain.append(option);
    }
    grain.value = state.granularity;
    grain.addEventListener("change", () => { state.granularity = grain.value; render(); });
    grainLabel.append(grain);
    filterLine.append(grainLabel);
    if (state.category === "workouts") {
      const typeLabel = el("label", "detail-select-label", "运动类型");
      const select = el("select");
      select.id = "detail-workout-type";
      const all = el("option", "", "全部运动");
      all.value = "all";
      select.append(all);
      const types = [...new Set((state.data.workouts || [])
        .filter(item => item.date >= state.start && item.date <= state.end).map(item => String(item.type || "other")))].sort();
      for (const type of types) {
        const option = el("option", "", typeName(type));
        option.value = type;
        select.append(option);
      }
      select.value = state.workoutType;
      select.addEventListener("change", () => {
        state.workoutType = select.value;
        state.page = 0;
        state.workout = null;
        render();
      });
      typeLabel.append(select);
      filterLine.append(typeLabel);
    }
    filterLine.append(el("span", "detail-range-label", dayLabel(state.start) + " — " + dayLabel(state.end) + " · 北京时间"));
    fragment.append(filterLine);
    if (state.error) {
      const error = el("p", "detail-error", state.error);
      error.setAttribute("role", "alert");
      fragment.append(error);
    }
    return fragment;
  }
  function statCard(key, label, value, unit, detail, digits) {
    const card = el("div", "detail-stat");
    card.dataset.detailStat = key;
    card.append(el("p", "detail-stat-label", label));
    const strong = el("strong", "", fmt(value, digits ?? meta().decimals ?? 1));
    strong.dataset.value = finite(value) ? String(value) : "";
    card.append(strong, el("div", "detail-stat-unit", unit || " "), el("small", "", detail || " "));
    return card;
  }
  function renderStats() {
    const grid = el("div", "detail-stat-grid");
    const stats = analysis.stats;
    const unit = meta().unit;
    const count = analysis.series.reduce((sum, point) => sum + (point.count || 0), 0);
    grid.append(statCard("mean", "有效日期平均", stats.mean, unit, `${count} 天有有效记录`));
    const cumulative = meta().aggregation === "sum";
    grid.append(statCard(cumulative ? "total" : "latest", cumulative ? "所选范围累计" : "范围内最新值",
      cumulative ? stats.total : stats.last, unit, cumulative ? "只累计已提供的数据" : "最后一个有效记录日"));
    grid.append(statCard("min", "最低日值", stats.min, unit, "所选范围内的每日记录"));
    grid.append(statCard("max", "最高日值", stats.max, unit, "所选范围内的每日记录"));
    grid.append(statCard("coverage", "记录覆盖", finite(stats.coverage) ? stats.coverage * 100 : null, "%",
      `${count} / ${stats.calendarDays || 0} 天有值`, 1));
    const delta = stats.delta;
    const previous = stats.previousMean;
    const percent = finite(delta) && finite(previous) && previous !== 0 ? delta / Math.abs(previous) * 100 : null;
    const detail = finite(previous) ? "前期日均 " + valueText(previous, state.metric, true) +
      (finite(percent) && unit !== "%" ? ` · ${percent >= 0 ? "+" : ""}${fmt(percent, 1)}%` : "") : "前期无有效样本，暂不比较";
    const change = statCard("change", "较等长前期日均", delta, unit === "%" ? "百分点" : unit, detail);
    const value = change.querySelector("strong");
    if (finite(delta) && delta > 0) value.textContent = "+" + value.textContent;
    grid.append(change);
    return grid;
  }
  function card(titleText, subtitle) {
    const panel = el("section", "detail-card");
    const head = el("div", "detail-card-head");
    const titleBox = el("div");
    titleBox.append(el("h3", "", titleText));
    if (subtitle) titleBox.append(el("p", "detail-caption", subtitle));
    head.append(titleBox);
    panel.append(head);
    return { panel, head };
  }
  function renderChart() {
    const aggregation = meta().aggregation === "sum" ? "累计" : "有效日均值";
    const grainName = { day: "每日", week: "每周", month: "每月" }[state.granularity];
    const { panel } = card(meta().label + "趋势", grainName + "显示" +
      (state.granularity === "day" ? "当日值" : aggregation) + "；缺失保持空缺，不连接没有记录的区间。");
    panel.classList.add("detail-chart-card");
    const plot = el("div", "detail-chart");
    plot.id = "detail-chart";
    const items = analysis.series, values = items.map(item => item.value).filter(finite);
    if (!values.length) {
      plot.append(el("div", "detail-empty", "这个范围还没有该指标的有效记录。\n可以换一个指标或扩大日期范围。"));
      panel.append(plot);
      return panel;
    }
    const dailyMap = new Map(analysis.days.map(item => [item.date, item]));
    const band = state.category === "heart" && state.metric === "heart_rate_avg" && state.granularity === "day";
    const extrema = values.slice();
    if (band) for (const item of items) {
      const day = dailyMap.get(item.date);
      if (finite(day?.heart_rate_min)) extrema.push(day.heart_rate_min);
      if (finite(day?.heart_rate_max)) extrema.push(day.heart_rate_max);
    }
    let low = Math.min(...extrema), high = Math.max(...extrema);
    if (meta().aggregation === "sum" || state.metric.includes("minutes")) low = 0;
    const pad = high > low ? (high - low) * .13 : Math.max(1, high * .05);
    low = Math.max(0, low - (low ? pad : 0));
    high += pad;
    if (high <= low) high = low + 1;
    const mobile = innerWidth <= 690;
    const scrollStyle = getComputedStyle(scroll);
    const scrollWidth = scroll.clientWidth || Math.min(1180, mobile ? innerWidth : innerWidth - 48);
    const width = Math.max(200, scrollWidth - (parseFloat(scrollStyle.paddingLeft) || 0) -
      (parseFloat(scrollStyle.paddingRight) || 0) - (mobile ? 32 : 44));
    const height = mobile ? 225 : 260, left = mobile ? 42 : 53, right = 14, top = 17, bottom = 33;
    const plotW = width - left - right, plotH = height - top - bottom;
    const x = index => left + (items.length <= 1 ? plotW / 2 : index / (items.length - 1) * plotW);
    const y = value => top + (high - value) / (high - low) * plotH;
    const canvas = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", tabindex: "0",
      "aria-label": meta().label + "趋势图。可用左右方向键选择日期；也可在下方日期选择框查看。",
      id: "detail-chart-svg" });
    for (let index = 0; index < 4; index++) {
      const value = low + (high - low) * index / 3;
      canvas.append(svg("line", { x1: left, x2: width - right, y1: y(value), y2: y(value), class: "detail-gridline" }));
      canvas.append(svg("text", { x: left - 10, y: y(value) + 4, "text-anchor": "end" }, fmt(value, high < 10 ? 1 : 0)));
    }
    if (band) {
      let section = [];
      const flush = () => {
        if (!section.length) return;
        const path = section.map((point, i) => `${i ? "L" : "M"}${x(point.i)},${y(point.max)}`).join(" ") +
          " " + section.slice().reverse().map(point => `L${x(point.i)},${y(point.min)}`).join(" ") + " Z";
        canvas.append(svg("path", { d: path, class: "detail-band" }));
        section = [];
      };
      items.forEach((item, i) => {
        const day = dailyMap.get(item.date);
        if (finite(item.value) && finite(day?.heart_rate_min) && finite(day?.heart_rate_max)) {
          section.push({ i, min: day.heart_rate_min, max: day.heart_rate_max });
        } else flush();
      });
      flush();
    }
    let trace = "", connected = false;
    items.forEach((item, index) => {
      if (!finite(item.value)) { connected = false; return; }
      trace += (connected ? " L" : " M") + x(index) + "," + y(item.value);
      connected = true;
    });
    canvas.append(svg("path", { d: trace, class: "detail-trace" }));
    const selectedIndex = Math.max(0, items.findIndex(item => state.date >= item.date && state.date <= (item.end || item.date)));
    if (items[selectedIndex]) {
      canvas.append(svg("line", { x1: x(selectedIndex), x2: x(selectedIndex), y1: top, y2: height - bottom, class: "detail-selected-line" }));
    }
    items.forEach((item, index) => {
      if (!finite(item.value) || (items.length > 160 && index !== selectedIndex)) return;
      const dot = svg("circle", { cx: x(index), cy: y(item.value), r: index === selectedIndex ? 4.4 : 2.4, class: "detail-point" });
      dot.append(svg("title", {}, (item.label || item.date) + " · " + valueText(item.value, state.metric, true)));
      canvas.append(dot);
    });
    const labels = Math.min(Math.max(2, Math.floor(plotW / 80)), 6, items.length);
    for (let index = 0; index < labels; index++) {
      const position = labels <= 1 ? 0 : Math.round(index / (labels - 1) * (items.length - 1));
      canvas.append(svg("text", { x: x(position), y: height - 7, "text-anchor": index === 0 ? "start" : index === labels - 1 ? "end" : "middle" },
        items[position].date.slice(5).replace("-", "/")));
    }
    function choose(index) {
      const point = items[index];
      if (!point) return;
      state.date = point.date < state.start ? state.start : point.date > state.end ? state.end : point.date;
      render();
    }
    canvas.addEventListener("click", event => {
      const rect = canvas.getBoundingClientRect();
      const location = (event.clientX - rect.left) / rect.width * width;
      const index = Math.round((location - left) / plotW * (items.length - 1));
      choose(Math.max(0, Math.min(items.length - 1, index)));
    });
    canvas.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 :
        Math.max(0, Math.min(items.length - 1, selectedIndex + (event.key === "ArrowLeft" ? -1 : 1)));
      choose(index);
    });
    plot.append(canvas);
    panel.append(plot);
    const caption = el("div", "detail-chart-footer");
    caption.append(el("span", "", "单位：" + (meta().unit || "数值") + (band ? " · 浅色范围为每日最小—最大心率" : "")),
      el("span", "", "点选图表查看当日 · 左右方向键移动"));
    panel.append(caption);
    return panel;
  }
  function renderStageBlock(stages, titleText, complete, countText) {
    const section = el("div", "detail-stage-section");
    section.append(el("h4", "", titleText));
    const total = STAGES.reduce((sum, [key]) => sum + (finite(stages?.[key]) ? stages[key] : 0), 0);
    if (complete && total > 0) {
      const bar = el("div", "detail-stage-bar");
      bar.setAttribute("aria-label", titleText);
      for (const [key, label, color] of STAGES) {
        const part = el("span");
        part.style.width = stages[key] / total * 100 + "%";
        part.style.background = color;
        part.title = label + " " + minutes(stages[key]);
        bar.append(part);
      }
      section.append(bar);
    }
    const legend = el("div", "detail-stage-legend");
    for (const [key, label, color] of STAGES) {
      const group = el("div");
      const name = el("span");
      const dot = el("i");
      dot.style.background = color;
      name.append(dot, document.createTextNode(label));
      group.append(name, el("strong", "", minutes(stages?.[key])));
      legend.append(group);
    }
    section.append(legend, el("p", "detail-caption", countText));
    return section;
  }
  function renderDay() {
    const { panel, head } = card("当日记录", "选择日期，查看同一天的多项指标。");
    panel.id = "detail-day-panel";
    const input = el("input");
    input.id = "detail-date";
    input.type = "date";
    input.setAttribute("aria-label", "查看当日详情");
    input.min = state.start || "";
    input.max = state.end || "";
    input.value = state.date || "";
    input.addEventListener("change", () => {
      if (input.value && input.value >= state.start && input.value <= state.end) {
        state.date = input.value;
        render();
      }
    });
    head.append(input);
    const current = analysis.days.find(item => item.date === state.date) || {};
    const grid = el("div", "detail-day-grid");
    for (const key of metricKeys()) {
      const item = button("", "detail-day-" + key, () => changeMetric(key), "detail-day-item");
      item.dataset.detailDayMetric = key;
      item.setAttribute("aria-pressed", String(key === state.metric));
      const value = el("strong", "", valueText(current[key], key));
      value.dataset.value = finite(current[key]) ? String(current[key]) : "";
      item.append(el("span", "", meta(key).label), value, el("small", "", meta(key).unit || " "));
      grid.append(item);
    }
    panel.append(grid);
    if (state.category === "sleep") {
      const complete = STAGES.every(([key]) => finite(current.sleep_stages?.[key]));
      panel.append(renderStageBlock(current.sleep_stages, "这一天的睡眠阶段", complete,
        complete ? "四个阶段均有记录；阶段总和包含清醒时间。" : "阶段数据不完整；已提供的阶段独立列出，不拼出完整占比。"));
    }
    if (state.category === "heart") panel.append(el("p", "detail-caption", "静息心率仅来自标记为 resting 的样本；日最小、最大是当天已有心率样本的范围。"));
    if (state.category === "workouts") panel.append(el("p", "detail-caption", "记录数为当前运动类型筛选下的档案条数，不代表当天实际运动的完整次数。"));
    return panel;
  }
  function renderDistribution() {
    if (state.category === "sleep") {
      const stages = analysis.sleepStages || {};
      const { panel } = card("睡眠阶段结构", "同一组完整样本的平均分钟数，避免把不同日期的阶段拼在一起。");
      panel.id = "detail-sleep-stages";
      panel.append(renderStageBlock(stages, "所选范围 · 完整阶段样本", stages.count > 0,
        stages.count ? `${stages.count} 天具备完整的深睡、浅睡、REM 和清醒记录。` : "0 天具备四个阶段的完整记录，阶段分布保持空缺。"));
      return panel;
    }
    const workouts = state.category === "workouts";
    const { panel } = card(workouts ? "运动类型分布" : "一周里的节奏",
      workouts ? "所选范围及筛选条件下的记录数和时长。" : "同一星期几的有效日均值；样本少的日期请结合记录数查看。");
    panel.id = "detail-distribution";
    const source = workouts ? analysis.workoutTypes : analysis.weekday;
    if (!source?.length) {
      panel.append(el("div", "detail-empty", "暂无可用记录"));
      return panel;
    }
    const maximum = Math.max(1, ...source.map(item => workouts ? item.count || 0 : finite(item.mean) ? item.mean : 0));
    const list = el("div", "detail-distribution");
    for (const item of source) {
      const value = workouts ? item.count : item.mean;
      const row = el("div", "detail-dist-row");
      const label = el("span", "detail-dist-label", workouts ? typeName(item.type) : item.label);
      label.title = label.textContent;
      const track = el("div", "detail-dist-track");
      const fill = el("span");
      fill.style.width = (finite(value) ? Math.max(0, value) / maximum * 100 : 0) + "%";
      track.append(fill);
      row.append(label, track, el("span", "detail-dist-value", workouts ?
        fmt(item.count) + " 条 · " + (finite(item.minutes) ? fmt(item.minutes) + " 分" : "—") :
        valueText(value) + " · " + fmt(item.count) + " 天"));
      list.append(row);
    }
    panel.append(list);
    return panel;
  }
  function csvColumns() {
    const definitions = {
      activity: [["date", "日期"], ["steps", "步数"], ["distance_km", "活动距离(km)"], ["active_kcal", "活动热量(kcal)"], ["active_minutes", "活动分钟"]],
      sleep: [["date", "日期"], ["sleep_minutes", "主睡眠分钟"], ["sleep_score", "睡眠评分"], ["nap_minutes", "小睡分钟"], ...STAGES.map(([key, label]) => ["stage_" + key, label + "分钟"])],
      heart: [["date", "日期"], ["resting_hr", "静息心率"], ["heart_rate_avg", "平均心率"], ["heart_rate_min", "最小心率"], ["heart_rate_max", "最大心率"]],
      body: [["date", "日期"], ["weight_kg", "体重(kg)"], ["body_fat_pct", "体脂率(%)"], ["spo2_avg", "血氧(%)"], ["stress_avg", "压力"]],
      workouts: [["date", "日期"], ["start", "开始时间"], ["type", "运动类型"], ["duration_minutes", "时长(分钟)"], ["distance_km", "距离(km)"], ["calories_kcal", "热量(kcal)"], ["avg_heart_rate_bpm", "平均心率"], ["avg_pace_sec_per_km", "配速(秒每公里)"]]
    };
    return definitions[state.category];
  }
  function fieldValue(row, key) {
    if (key.startsWith("stage_")) return row.sleep_stages?.[key.slice(6)];
    if (key === "type") return typeName(row.type);
    return row[key];
  }
  function exportCsv() {
    const columns = csvColumns();
    const rows = state.category === "workouts" ? analysis.workouts : analysis.days;
    const escape = value => {
      if (value === null || value === undefined || (typeof value === "number" && !finite(value))) return "";
      let text = String(value);
      if ((/^\s*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) && typeof value !== "number") text = "'" + text;
      return '"' + text.replaceAll('"', '""') + '"';
    };
    const lines = [columns.map(([, label]) => escape(label)).join(",")];
    for (const row of rows) lines.push(columns.map(([key]) => escape(fieldValue(row, key))).join(","));
    const url = URL.createObjectURL(new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" }));
    const link = el("a");
    link.href = url;
    link.download = `${CATEGORIES[state.category].label}_${state.start}_${state.end}.csv`;
    dialog.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    notify("已导出当前详情范围的 CSV，缺失值保留为空。");
  }
  function renderRecords() {
    const workouts = state.category === "workouts";
    const { panel, head } = card(workouts ? "完整运动记录" : "逐日记录",
      workouts ? "从近到远排列，点选一条查看单次详情。" : "包含没有记录的日期；数值为空表示未提供，不补成零。");
    panel.classList.add("detail-records");
    panel.id = "detail-records";
    head.append(button("导出本类 CSV", "detail-export-csv", exportCsv));
    const rows = workouts ? analysis.workouts : analysis.days;
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    state.page = Math.max(0, Math.min(state.page, pages - 1));
    if (!rows.length) panel.append(el("div", "detail-empty", "这个日期范围没有可展示的记录。"));
    else {
      const wrap = el("div", "detail-table-wrap");
      wrap.tabIndex = 0;
      wrap.setAttribute("aria-label", "记录表格；窄屏可横向滚动");
      const table = el("table", "detail-table");
      table.id = "detail-record-table";
      const thead = el("thead"), headingRow = el("tr");
      const columns = csvColumns().filter(([key]) => !(workouts && (key === "start" || key === "type")));
      for (const [, label] of columns) {
        const th = el("th", "", label);
        th.scope = "col";
        headingRow.append(th);
      }
      thead.append(headingRow);
      const tbody = el("tbody");
      rows.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE).forEach((row, offset) => {
        const tr = el("tr");
        if (!workouts && row.date === state.date) tr.className = "is-selected";
        columns.forEach(([key, label], index) => {
          const td = el("td");
          td.dataset.label = label;
          if (index === 0) {
            const recordId = workouts ? "detail-record-workout-" + (state.page * PAGE_SIZE + offset) : "detail-record-day-" + row.date;
            const record = button(dayLabel(row.date) + (workouts ? " " + (row.start || "") : ""), recordId, () => {
              if (workouts) { state.workout = row; state.workoutOrigin = recordId; }
              state.date = row.date;
              render();
              if (workouts) revealWorkout();
            }, "detail-record-link");
            if (workouts) record.dataset.detailWorkoutIndex = String(state.page * PAGE_SIZE + offset);
            else record.dataset.detailRowDate = row.date;
            td.append(record);
            if (workouts) td.append(el("span", "detail-type", typeName(row.type)));
          } else {
            const value = fieldValue(row, key);
            td.textContent = key === "avg_pace_sec_per_km" ? pace(value) : fmt(value, /weight|fat|distance|spo2/.test(key) ? 1 : 0);
          }
          tr.append(td);
        });
        tbody.append(tr);
      });
      table.append(thead, tbody);
      wrap.append(table);
      panel.append(wrap);
    }
    const footer = el("div", "detail-table-footer");
    footer.append(el("span", "", `共 ${rows.length} ${workouts ? "条运动记录" : "个日期"} · 每页 ${PAGE_SIZE} 条`));
    const pager = el("div", "detail-pagination");
    const previous = button("‹", "detail-record-prev", () => { state.page--; render(); }, "");
    previous.setAttribute("aria-label", "上一页记录");
    previous.disabled = state.page === 0;
    const next = button("›", "detail-record-next", () => { state.page++; render(); }, "");
    next.setAttribute("aria-label", "下一页记录");
    next.disabled = state.page >= pages - 1;
    const page = el("span", "", `${state.page + 1} / ${pages}`);
    page.id = "detail-record-page";
    pager.append(previous, page, next);
    footer.append(pager);
    panel.append(footer);
    return panel;
  }
  function renderWorkout() {
    if (state.category !== "workouts" || !state.workout) return null;
    const workout = state.workout;
    const { panel, head } = card("单次运动 · " + typeName(workout.type),
      dayLabel(workout.date) + " " + (workout.start || "") + " · 北京时间");
    panel.id = "detail-single-workout";
    panel.tabIndex = -1;
    panel.setAttribute("aria-label", "单次运动详情");
    panel.classList.add("detail-workout-card");
    head.append(button("收起", "detail-workout-close", () => {
      const origin = state.workoutOrigin;
      state.workout = null;
      render();
      const target = document.getElementById(origin);
      if (target) revealPanel(target);
      else closeButton.focus({ preventScroll: true });
    }));
    const grid = el("div", "detail-workout-values");
    for (const [key, label, format] of [
      ["duration_minutes", "运动时长", minutes],
      ["distance_km", "运动距离", value => fmt(value, 2) + " km"],
      ["calories_kcal", "消耗热量", value => fmt(value) + " kcal"],
      ["avg_heart_rate_bpm", "平均心率", value => fmt(value) + " 次/分"],
      ["avg_pace_sec_per_km", "平均配速", pace]
    ]) {
      if (!finite(workout[key])) continue;
      const item = el("div");
      item.dataset.detailWorkoutMetric = key;
      const strong = el("strong", "", format(workout[key]));
      strong.dataset.value = String(workout[key]);
      item.append(el("span", "", label), strong);
      grid.append(item);
    }
    panel.append(grid);
    if (!grid.children.length) panel.append(el("p", "detail-caption", "这条记录未提供额外的运动指标。"));
    panel.append(el("p", "detail-caption", "仅展示来源提供的运动指标，不包含 GPS 轨迹或运动配速分时数据。当日心率采样可在心率详情的单日曲线中查看。"));
    return panel;
  }
  function render() {
    const active = document.activeElement;
    const activeId = dialog.contains(active) ? active.id : "";
    const y = scroll.scrollTop;
    state.data = window.MiFitDashboard.getData();
    const availableTypes = new Set((state.data.workouts || [])
      .filter(item => item.date >= state.start && item.date <= state.end).map(item => String(item.type || "other")));
    if (state.workoutType !== "all" && !availableTypes.has(state.workoutType)) state.workoutType = "all";
    analysis = window.MiFitDetailModel.analyze(state.data, {
      category: state.category, metric: state.metric, start: state.start, end: state.end,
      granularity: state.granularity, workoutType: state.workoutType
    });
    if (!state.date || state.date < state.start || state.date > state.end) {
      state.date = analysis.days.at(-1)?.date || state.end || "";
    }
    if (state.workout && (state.workout.date !== state.date ||
        state.workout.date < state.start || state.workout.date > state.end ||
        (state.workoutType !== "all" && state.workout.type !== state.workoutType))) {
      state.workout = null;
      state.workoutOrigin = "";
    }
    dialog.dataset.category = state.category;
    title.textContent = CATEGORIES[state.category].label;
    for (const item of nav.querySelectorAll("[data-detail-category]")) {
      item.setAttribute("aria-pressed", String(item.dataset.detailCategory === state.category));
    }
    const body = document.createDocumentFragment();
    body.append(el("p", "detail-description", CATEGORIES[state.category].description));
    if (state.category === "heart" || state.category === "sleep") {
      body.append(button(state.category === "heart" ? "查看单日心率曲线 ↓" : "查看睡眠阶段时间轴 ↓",
        "detail-jump-intraday", () => {
          const panel = document.getElementById("intraday-panel");
          if (!panel) return;
          panel.tabIndex = -1;
          revealPanel(panel);
        }, "detail-button detail-intraday-jump"));
    }
    body.append(renderMetrics(), renderFilters(), renderStats(), renderChart());
    const intraday = window.MiFitIntradayView?.render({
      data: state.data, category: state.category, date: state.date,
      start: state.start, end: state.end,
      onDateChange(date) {
        if (validRange(date, date) || date < state.start || date > state.end) return;
        state.date = date;
        state.workout = null;
        render();
      }
    });
    if (intraday) body.append(intraday);
    const middle = el("div", "detail-middle-grid");
    middle.append(renderDay(), renderDistribution());
    body.append(middle);
    const workout = renderWorkout();
    if (workout) body.append(workout);
    body.append(renderRecords(), el("p", "detail-footer-note",
      "详情筛选独立于总览；关闭后总览保持原样。周、月趋势仅汇总所选范围内的数据。所有处理和导出都在本地浏览器完成。"));
    scroll.replaceChildren(body);
    scroll.scrollTop = y;
    if (activeId) (document.getElementById(activeId) || closeButton).focus({ preventScroll: true });
  }
  function open(category, metric, date, workout) {
    if (!CATEGORIES[category] || !window.MiFitDetailModel || !window.MiFitDashboard) return;
    const data = window.MiFitDashboard.getData();
    const inherited = window.MiFitDashboard.getRange() || dataRange(data);
    const all = dataRange(data);
    state = {
      data, category,
      metric: window.MiFitDetailModel.metrics[metric]?.category === category ? metric : CATEGORIES[category].metric,
      start: inherited.start || all.start, end: inherited.end || all.end,
      draftStart: inherited.start || all.start, draftEnd: inherited.end || all.end,
      granularity: "day", workoutType: "all", page: 0, preset: "", error: "",
      date: date || workout?.date || window.MiFitDashboard.getSelectedDate?.() || inherited.end || all.end,
      workout: workout || null
    };
    if (!dialog.open) {
      opener = document.activeElement;
      savedScroll = { x: window.scrollX, y: window.scrollY };
      document.body.classList.add("detail-open");
      dialog.showModal();
      render();
      closeButton.focus({ preventScroll: true });
      scroll.scrollTop = 0;
    } else render();
    if (workout) revealWorkout();
  }
  document.addEventListener("mifit:data-updated", () => {
    if (!dialog.open) return;
    const range = window.MiFitDashboard.getRange();
    state.start = state.draftStart = range.start;
    state.end = state.draftEnd = range.end;
    state.date = range.end;
    state.page = 0;
    state.workout = null;
    render();
  });
  window.MiFitDetails = { open, close };
  let resizeTimer;
  window.addEventListener("resize", () => {
    if (!dialog.open) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (dialog.open) render(); }, 100);
  });
  document.documentElement.dataset.detailsReady = "true";
}());
