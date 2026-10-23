/* Offline intraday views. Raw readings stay in the archive; only the chart is reduced. */
(function () {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";
  const DAY = 86400000, HOUR = 3600000, PAGE = 20;
  const WINDOWS = {
    all: ["全天", 0, DAY], early: ["凌晨", 0, 6 * HOUR],
    morning: ["早晨", 6 * HOUR, 12 * HOUR],
    afternoon: ["午后", 12 * HOUR, 18 * HOUR], night: ["夜间", 18 * HOUR, DAY]
  };
  const STAGES = {
    awake: ["清醒", "var(--intraday-awake)"], rem: ["REM", "var(--intraday-rem)"],
    light: ["浅睡", "var(--intraday-light)"], deep: ["深睡", "var(--intraday-deep)"],
    unknown: ["未知", "var(--intraday-unknown)"]
  };
  const preferences = { heart: { window: "all", filter: "all", reading: null }, sleep: { session: 0 } };
  let archive = null;
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function svg(tag, attrs = {}, text) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function button(text, id, action, cls = "detail-button") {
    const node = el("button", cls, text);
    node.type = "button";
    if (id) node.id = id;
    node.addEventListener("click", action);
    return node;
  }
  function num(value, digits = 0) {
    return Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
  }
  function duration(value) {
    if (!Number.isFinite(value)) return "—";
    if (value > 0 && value < 1) return num(value * 60, 3) + " 秒";
    const whole = Math.floor(value / 60), rest = value % 60;
    return whole ? whole + " 小时" + (rest ? " " + num(rest, 1) + " 分" : "") : num(rest, 1) + " 分";
  }
  function clock(ms, seconds = false) {
    if (ms === DAY) return "24:00";
    const h = Math.floor(ms / HOUR), m = Math.floor(ms % HOUR / 60000);
    const sec = Math.floor(ms % 60000 / 1000), fraction = Math.round(ms % 1000);
    return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") +
      (seconds ? ":" + String(sec).padStart(2, "0") + (fraction ? "." + String(fraction).padStart(3, "0") : "") : "");
  }
  function beijing(ms, full = false) {
    const date = new Date(ms + 8 * HOUR);
    if (!Number.isFinite(date.getTime())) return "—";
    const text = date.toISOString();
    return full ? text.slice(0, 10) + " " + text.slice(11, 16) : text.slice(11, 16);
  }
  function dateShift(date, n) {
    const time = Date.parse(date + "T00:00:00Z");
    return Number.isFinite(time) ? new Date(time + n * DAY).toISOString().slice(0, 10) : "";
  }
  function validDate(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      Number.isFinite(Date.parse(value)) && new Date(value + "T00:00:00Z").toISOString().slice(0, 10) === value;
  }
  function stageName(stage) { return (STAGES[stage] || STAGES.unknown)[0]; }
  function stageColor(stage) { return (STAGES[stage] || STAGES.unknown)[1]; }
  function width() {
    const scroll = document.querySelector(".detail-scroll");
    const style = scroll ? getComputedStyle(scroll) : null;
    const available = scroll ? scroll.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) :
      Math.min(1100, window.innerWidth - 32);
    return Math.max(220, Math.min(1100, available - (window.innerWidth <= 690 ? 32 : 44)));
  }
  function empty(parent, text) {
    const box = el("div", "intraday-empty", text);
    box.id = "intraday-empty";
    parent.append(box);
  }
  function hasSamples(data) {
    return Object.keys(data.intraday?.heart || {}).length > 0 || Object.keys(data.intraday?.sleep || {}).length > 0;
  }
  function stat(label, value, unit, key) {
    const node = el("div", "intraday-stat");
    node.dataset.intradayStat = key;
    node.append(el("span", "", label));
    const strong = el("strong", "", num(value, key === "mean" ? 1 : key === "count" ? 0 : 2));
    strong.dataset.value = Number.isFinite(value) ? String(value) : "";
    node.append(strong, el("small", "", unit));
    return node;
  }
  function downloadCSV(filename, rows) {
    const quote = value => '"' + String(value ?? "").replaceAll('"', '""') + '"';
    const blob = new Blob(["\ufeff" + rows.map(row => row.map(quote).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" });
    const link = el("a");
    const url = URL.createObjectURL(blob);
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function nearest(samples, time) {
    let lo = 0, hi = samples.length - 1;
    while (lo < hi) {
      const mid = Math.floor((lo + hi) / 2);
      if (samples[mid][0] < time) lo = mid + 1;
      else hi = mid;
    }
    return lo > 0 && Math.abs(samples[lo - 1][0] - time) <= Math.abs(samples[lo][0] - time) ? lo - 1 : lo;
  }
  function chartSamples(samples, lo, hi) {
    const grouped = [];
    let group = 0;
    for (let index = 0; index < samples.length; index++) {
      if (index && samples[index][0] - samples[index - 1][0] > 15 * 60000) group++;
      grouped.push({ sample: samples[index], index, group });
    }
    if (grouped.length <= 2000) return grouped;
    const buckets = new Map();
    for (const point of grouped) {
      const bin = Math.min(499, Math.floor((point.sample[0] - lo) / Math.max(1, hi - lo) * 500));
      let bucket = buckets.get(bin);
      if (!bucket) {
        bucket = { first: point, min: point, max: point, last: point };
        buckets.set(bin, bucket);
      }
      if (point.sample[1] < bucket.min.sample[1]) bucket.min = point;
      if (point.sample[1] > bucket.max.sample[1]) bucket.max = point;
      bucket.last = point;
    }
    const unique = new Map();
    for (const bucket of buckets.values()) {
      for (const point of [bucket.first, bucket.min, bucket.max, bucket.last]) unique.set(point.index, point);
    }
    return [...unique.values()].sort((a, b) => a.index - b.index);
  }
  function dateControls(opts) {
    const dates = el("div", "intraday-date-controls");
    const min = opts.start || opts.data.meta?.range?.start || "", max = opts.end || opts.data.meta?.range?.end || "";
    const move = value => {
      if (!validDate(value) || min && value < min || max && value > max) return;
      opts.onDateChange?.(value);
    };
    const previous = button("‹", "intraday-prev-day", () => move(dateShift(opts.date, -1)), "detail-button intraday-day-step");
    previous.setAttribute("aria-label", "查看前一天的分时数据");
    previous.disabled = !validDate(opts.date) || Boolean(min && opts.date <= min);
    const label = el("label", "intraday-date-label");
    const caption = el("span", "sr-only", "分时详情日期");
    const input = el("input");
    input.type = "date";
    input.id = "intraday-date";
    input.value = opts.date || "";
    if (min) input.min = min;
    if (max) input.max = max;
    input.addEventListener("change", () => {
      if (!validDate(input.value) || !input.checkValidity()) { input.value = opts.date; return; }
      move(input.value);
    });
    label.append(caption, input);
    const next = button("›", "intraday-next-day", () => move(dateShift(opts.date, 1)), "detail-button intraday-day-step");
    next.setAttribute("aria-label", "查看后一天的分时数据");
    next.disabled = !validDate(opts.date) || Boolean(max && opts.date >= max);
    dates.append(previous, label, next);
    return dates;
  }
  function heart(parent, opts) {
    const pref = preferences.heart;
    const all = opts.data.intraday?.heart?.[opts.date] || [];
    const body = el("div", "intraday-heart");
    body.id = "intraday-heart";
    parent.append(body);
    const toolbar = el("div", "intraday-toolbar");
    const tabs = el("div", "intraday-windows");
    tabs.setAttribute("role", "group");
    tabs.setAttribute("aria-label", "心率查看时段");
    const refresh = () => {
      const focused = document.activeElement?.id || "";
      body.remove();
      heart(parent, opts);
      if (focused) document.getElementById(focused)?.focus({ preventScroll: true });
    };
    for (const [key, value] of Object.entries(WINDOWS)) {
      const item = button(value[0], "intraday-window-" + key, () => {
        pref.window = key; pref.reading = null; refresh();
      }, "");
      item.dataset.intradayWindow = key;
      item.setAttribute("aria-pressed", String(pref.window === key));
      tabs.append(item);
    }
    const filterLabel = el("label", "intraday-select-label");
    filterLabel.append(el("span", "", "样本类型"));
    const filter = el("select");
    filter.id = "intraday-heart-filter";
    for (const [value, label] of [["all", "全部样本"], ["regular", "非静息标记"], ["resting", "静息标记"]]) {
      const option = el("option", "", label);
      option.value = value;
      filter.append(option);
    }
    filter.value = pref.filter;
    filter.addEventListener("change", () => { pref.filter = filter.value; pref.reading = null; refresh(); });
    filterLabel.append(filter);
    toolbar.append(tabs, filterLabel);
    body.append(toolbar);
    if (!all.length) {
      empty(body, hasSamples(opts.data) ? "这一天没有逐次心率样本。可切换日期；日汇总不会生成分时读数。" :
        "当前文件仅有每日汇总。重新导入原始 JSON，即可加载逐次心率并保存为单文件离线网页。");
      return;
    }
    const window = WINDOWS[pref.window] || WINDOWS.all;
    const samples = all.filter(item => item[0] >= window[1] && item[0] < window[2] &&
      (pref.filter === "all" || (pref.filter === "resting" ? item[2] === 1 : item[2] !== 1)));
    if (!samples.length) { empty(body, "这个时段与样本类型下没有记录。试试“全天”或“全部样本”。"); return; }
    const total = samples.reduce((sum, sample) => sum + sample[1], 0);
    const min = samples.reduce((value, sample) => Math.min(value, sample[1]), Infinity);
    const max = samples.reduce((value, sample) => Math.max(value, sample[1]), -Infinity);
    const stats = el("div", "intraday-stats");
    stats.append(stat("原始样本", samples.length, "条", "count"), stat("样本均值", total / samples.length, "次/分", "mean"),
      stat("最低读数", min, "次/分", "min"), stat("最高读数", max, "次/分", "max"));
    body.append(stats);
    const drawn = chartSamples(samples, window[1], window[2]);
    const w = width(), h = 238, margin = { left: 38, right: 13, top: 21, bottom: 31 };
    const plotWidth = w - margin.left - margin.right, plotHeight = h - margin.top - margin.bottom;
    const pad = Math.max(5, (max - min) * 0.15), lower = Math.max(0, Math.floor((min - pad) / 5) * 5);
    const upper = Math.ceil((max + pad) / 5) * 5;
    const x = time => margin.left + (time - window[1]) / (window[2] - window[1]) * plotWidth;
    const y = bpm => margin.top + (upper - bpm) / (upper - lower) * plotHeight;
    const chart = svg("svg", { viewBox: `0 0 ${w} ${h}`, role: "img", "aria-labelledby": "intraday-heart-chart-title",
      "data-window-start": window[1], "data-window-end": window[2], "data-samples": samples.length,
      "data-drawn-samples": drawn.length,
      "data-drawn-min": drawn.reduce((value, point) => Math.min(value, point.sample[1]), Infinity),
      "data-drawn-max": drawn.reduce((value, point) => Math.max(value, point.sample[1]), -Infinity) });
    chart.id = "intraday-heart-chart";
    chart.append(svg("title", { id: "intraday-heart-chart-title" }, opts.date + " 心率原始样本，横轴为北京时间，纵轴为每分钟心跳次数。读数可使用下方滑条逐条查看。"));
    for (let i = 0; i <= 4; i++) {
      const value = lower + (upper - lower) / 4 * i;
      chart.append(svg("line", { x1: margin.left, x2: w - margin.right, y1: y(value), y2: y(value), class: "intraday-gridline" }));
      chart.append(svg("text", { x: margin.left - 8, y: y(value) + 3, "text-anchor": "end" }, num(value)));
    }
    const ticks = pref.window === "all" ? 4 : 3;
    for (let i = 0; i <= ticks; i++) {
      const time = window[1] + (window[2] - window[1]) * i / ticks;
      chart.append(svg("text", { x: x(time), y: h - 8, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle" }, clock(time)));
    }
    let path = "", previous = null;
    const segments = new Set();
    drawn.forEach(point => {
      const start = previous === null || previous.group !== point.group;
      path += (start ? "M" : "L") + x(point.sample[0]).toFixed(2) + " " + y(point.sample[1]).toFixed(2) + " ";
      segments.add(point.group);
      previous = point;
    });
    chart.dataset.segments = String(segments.size);
    chart.append(svg("path", { d: path, class: "intraday-heart-trace" }));
    drawn.forEach((point, i) => {
      if (drawn.length <= 100 || drawn[i - 1]?.group !== point.group && drawn[i + 1]?.group !== point.group) {
        chart.append(svg("circle", { cx: x(point.sample[0]), cy: y(point.sample[1]), r: 2.6, class: "intraday-heart-dot" }));
      }
    });
    const cursor = svg("line", { y1: margin.top, y2: h - margin.bottom, class: "intraday-cursor" });
    const selectedPoint = svg("circle", { r: 4.5, class: "intraday-selected-dot" });
    chart.append(cursor, selectedPoint);
    const chartWrap = el("div", "intraday-chart-wrap");
    chartWrap.append(chart);
    body.append(chartWrap);
    const hint = el("p", "intraday-chart-hint", "点按曲线或拖动下方滑条查看原始读数；相邻样本超过 15 分钟时留空。");
    body.append(hint);
    const inspector = el("div", "intraday-inspector");
    const reading = el("div", "intraday-reading");
    reading.id = "intraday-reading";
    reading.setAttribute("role", "status");
    reading.setAttribute("aria-live", "polite");
    const readingTime = el("span", "intraday-reading-time");
    const readingValue = el("strong");
    const readingType = el("small");
    reading.append(readingTime, readingValue, readingType);
    const rangeLabel = el("label", "intraday-slider-label");
    rangeLabel.append(el("span", "sr-only", "逐条查看原始心率样本"));
    const range = el("input", "intraday-sample-slider");
    range.id = "intraday-sample-range";
    range.type = "range";
    range.min = "0"; range.max = String(samples.length - 1); range.step = "1";
    rangeLabel.append(range);
    inspector.append(reading, rangeLabel);
    body.append(inspector);
    const selectionKey = opts.date + "|" + pref.window + "|" + pref.filter;
    let selected = pref.selectionKey === selectionKey && Number.isInteger(pref.readingIndex) &&
      samples[pref.readingIndex]?.[0] === pref.reading ? pref.readingIndex :
      pref.date === opts.date && Number.isFinite(pref.reading) ? nearest(samples, pref.reading) : samples.length - 1;
    const select = index => {
      selected = Math.max(0, Math.min(samples.length - 1, Math.round(index)));
      const sample = samples[selected];
      pref.date = opts.date; pref.reading = sample[0];
      pref.selectionKey = selectionKey; pref.readingIndex = selected;
      range.value = String(selected);
      range.setAttribute("aria-valuetext", `${clock(sample[0], true)}，${num(sample[1], 2)} 次/分，${sample[2] === 1 ? "静息标记" : "非静息标记"}`);
      reading.dataset.time = String(sample[0]); reading.dataset.value = String(sample[1]); reading.dataset.index = String(selected);
      readingTime.textContent = clock(sample[0], true) + " · 北京时间";
      readingValue.textContent = num(sample[1], 2) + " 次/分";
      readingType.textContent = (sample[2] === 1 ? "静息标记" : "非静息标记") + " · 第 " + num(selected + 1) + " / " + num(samples.length) + " 条";
      cursor.setAttribute("x1", x(sample[0])); cursor.setAttribute("x2", x(sample[0]));
      selectedPoint.setAttribute("cx", x(sample[0])); selectedPoint.setAttribute("cy", y(sample[1]));
    };
    range.addEventListener("input", () => select(Number(range.value)));
    const pointAt = event => {
      const rect = chart.getBoundingClientRect();
      const at = (event.clientX - rect.left) / rect.width * w;
      const time = window[1] + (at - margin.left) / plotWidth * (window[2] - window[1]);
      select(nearest(samples, time));
    };
    chart.addEventListener("pointerdown", event => {
      pointAt(event);
      if (event.pointerType !== "mouse") chart.setPointerCapture?.(event.pointerId);
    });
    chart.addEventListener("pointermove", event => { if (event.buttons || event.pointerType === "mouse") pointAt(event); });
    select(selected);
    const note = el("p", "intraday-note", "均值按样本计算，未按时间加权。静息标记来自原始记录；未标记的样本不等同于运动心率。");
    body.append(note);
    if (drawn.length < samples.length) {
      const reduced = el("p", "intraday-note", `为保持流畅，曲线按时间分组保留首尾及高低点，绘制 ${num(drawn.length)} / ${num(samples.length)} 条。读数、统计和导出仍使用全部原值。`);
      reduced.id = "intraday-heart-reduction";
      body.append(reduced);
    }
    const records = el("details", "intraday-records");
    records.id = "intraday-heart-records";
    const summary = el("summary", "", "逐条读数与导出");
    records.append(summary);
    const controls = el("div", "intraday-record-controls");
    const exportButton = button("导出当前样本 CSV", "intraday-heart-export", () => {
      downloadCSV("心率-" + opts.date + "-" + pref.window + "-" + pref.filter + ".csv",
        [["日期（北京时间）", "时间（北京时间）", "心率（次/分）", "样本类型"],
          ...samples.map(sample => [opts.date, clock(sample[0], true), sample[1], sample[2] === 1 ? "静息标记" : "非静息标记"])]);
    });
    controls.append(el("span", "", `当前时段和类型共 ${num(samples.length)} 条，导出包含全部原始读数。`), exportButton);
    records.append(controls);
    const table = el("table", "intraday-table");
    const head = el("thead"), headRow = el("tr");
    for (const title of ["北京时间", "心率 · 次/分", "样本类型"]) headRow.append(el("th", "", title));
    head.append(headRow); table.append(head);
    const tbody = el("tbody"); table.append(tbody);
    records.append(table);
    const footer = el("div", "intraday-pagination");
    let page = 0;
    const label = el("span");
    const prev = button("上一页", "intraday-record-prev", () => { page--; updateTable(); });
    const next = button("下一页", "intraday-record-next", () => { page++; updateTable(); });
    footer.append(label, prev, next); records.append(footer);
    function updateTable() {
      const rows = document.createDocumentFragment();
      samples.slice(page * PAGE, (page + 1) * PAGE).forEach((sample, offset) => {
        const row = el("tr");
        const timeCell = el("td");
        const link = button(clock(sample[0], true), "", () => select(page * PAGE + offset), "intraday-record-link");
        link.setAttribute("aria-label", `${clock(sample[0], true)}，${sample[1]} 次/分，查看曲线位置`);
        timeCell.append(link);
        row.append(timeCell, el("td", "", num(sample[1], 2)), el("td", "", sample[2] === 1 ? "静息标记" : "非静息标记"));
        rows.append(row);
      });
      tbody.replaceChildren(rows);
      label.textContent = (page + 1) + " / " + Math.ceil(samples.length / PAGE) + " 页";
      prev.disabled = page === 0; next.disabled = (page + 1) * PAGE >= samples.length;
    }
    updateTable(); body.append(records);
  }
  function sleep(parent, opts) {
    const pref = preferences.sleep;
    const source = opts.data.intraday?.sleep?.[opts.date] || [];
    const sessions = source.map((item, index) => ({ ...item, sourceIndex: index })).sort((a, b) => {
      if (a.nap !== b.nap) return a.nap ? 1 : -1;
      return (b.end - b.start) - (a.end - a.start) || b.end - a.end;
    });
    const body = el("div", "intraday-sleep"); body.id = "intraday-sleep"; parent.append(body);
    if (!sessions.length) {
      empty(body, hasSamples(opts.data) ? "这一天没有睡眠阶段记录。睡眠按醒来日期归档，跨午夜的记录请查看次日。" :
        "当前文件仅有每日汇总。重新导入原始 JSON，即可查看睡眠阶段并保存为单文件离线网页。");
      return;
    }
    if (pref.date !== opts.date || pref.session >= sessions.length) { pref.date = opts.date; pref.session = 0; }
    const label = el("label", "intraday-session-label");
    label.append(el("span", "", "按醒来日期选择记录"));
    const choose = el("select"); choose.id = "intraday-sleep-session";
    sessions.forEach((session, index) => {
      const text = (session.nap ? "小睡" : index === 0 ? "最长睡眠记录" : "其他睡眠记录") +
        " · " + beijing(session.start) + "–" + beijing(session.end) + " · " + duration((session.end - session.start) / 60000);
      const option = el("option", "", text); option.value = String(index); choose.append(option);
    });
    choose.value = String(pref.session);
    choose.addEventListener("change", () => {
      pref.session = Number(choose.value);
      body.remove(); sleep(parent, opts);
      document.getElementById("intraday-sleep-session")?.focus({ preventScroll: true });
    });
    label.append(choose); body.append(label);
    const session = sessions[pref.session];
    const timeline = window.MiFitIntradayModel?.sleepTimeline(session) ||
      { valid: false, segments: [], unplacedMinutes: null, reason: "missing" };
    const span = (session.end - session.start) / 60000;
    const stages = session.stages || [];
    const knownTotal = stages.reduce((sum, stage) => sum + stage[1], 0);
    const interval = el("p", "intraday-sleep-interval");
    interval.append(el("span", "", beijing(session.start, true)), el("span", "intraday-interval-arrow", "→"), el("span", "", beijing(session.end, true)));
    body.append(interval, el("p", "intraday-note intraday-timezone", "北京时间 · " + (session.nap ? "小睡" : "睡眠") +
      "记录区间 " + duration(span) + " · 阶段累计 " + duration(knownTotal)));
    const note = el("div", "intraday-sleep-note");
    note.id = "intraday-sleep-note";
    note.dataset.mode = timeline.valid ? "approximate" : stages.length ? "sequence" : "empty";
    note.append(el("strong", "", timeline.valid ? "近似阶段时间轴" : stages.length ? "阶段顺序" : "暂无阶段明细"));
    note.append(el("p", "", timeline.valid ?
      "根据阶段顺序和时长重建的近似时间轴。原始导出没有各阶段的起止时间；以下以记录起点连续排列，阶段边界为估算。" :
      stages.length ? (session.sequenceValid === false ? "部分来源阶段的时长无效，后续阶段无法定位。" :
        "阶段累计时长与记录区间不一致，无法可靠放置到钟点。") +
        "以下只展示可读阶段的原始顺序与时长，没有拉伸或补齐。" :
        "这条睡眠记录没有可用的阶段明细，保留起止时间与区间时长。"));
    body.append(note);
    if (Number.isFinite(timeline.unplacedMinutes) && timeline.unplacedMinutes > 0) {
      const unplaced = el("p", "intraday-unplaced", `另有 ${duration(timeline.unplacedMinutes)} 未定位：缺少阶段时间信息，无法确定发生位置，不计作清醒或其他阶段。`);
      unplaced.id = "intraday-sleep-unplaced"; unplaced.dataset.minutes = String(timeline.unplacedMinutes);
      body.append(unplaced);
    }
    if (stages.length) {
      const visibleStages = ["awake", "rem", "light", "deep"].concat(stages.some(item => item[0] === "unknown") ? ["unknown"] : []);
      const legend = el("div", "intraday-stage-legend");
      for (const key of visibleStages) {
        const item = el("span"); const dot = el("i"); dot.style.background = stageColor(key);
        const sum = stages.filter(stage => stage[0] === key).reduce((value, stage) => value + stage[1], 0);
        item.append(dot, el("span", "", stageName(key) + " " + duration(sum))); legend.append(item);
      }
      body.append(legend);
      if (timeline.valid) sleepChart(body, session, timeline, visibleStages);
      else sleepSequence(body, stages);
      sleepRecords(body, stages, timeline);
    }
    if (sessions.length > 1) body.append(el("p", "intraday-note", "默认查看区间最长的非小睡记录。其他记录可能存在时间重叠，可通过上方选择器分别核对。"));
  }
  function sleepChart(body, session, timeline, visibleStages) {
    const w = width(), left = 39, right = 13, top = 14, row = 36, bottom = 37;
    const h = top + visibleStages.length * row + bottom;
    const plotWidth = w - left - right;
    const x = time => left + (time - session.start) / (session.end - session.start) * plotWidth;
    const chart = svg("svg", { viewBox: `0 0 ${w} ${h}`, role: "group", "aria-labelledby": "intraday-sleep-chart-title",
      "data-window-minutes": (session.end - session.start) / 60000 });
    chart.id = "intraday-sleep-chart";
    chart.append(svg("title", { id: "intraday-sleep-chart-title" }, "睡眠阶段近似时间轴。阶段顺序与分钟数来自原记录，钟点为估算。"));
    visibleStages.forEach((stage, index) => {
      const y = top + index * row + row / 2;
      chart.append(svg("text", { x: left - 8, y: y + 3, "text-anchor": "end" }, stageName(stage)));
      chart.append(svg("line", { x1: left, x2: w - right, y1: y, y2: y, class: "intraday-gridline" }));
    });
    for (let i = 0; i <= 3; i++) {
      const time = session.start + (session.end - session.start) * i / 3;
      chart.append(svg("text", { x: x(time), y: h - 11, "text-anchor": i === 0 ? "start" : i === 3 ? "end" : "middle" }, beijing(time)));
    }
    const reading = el("p", "intraday-sleep-reading");
    reading.id = "intraday-sleep-reading";
    reading.setAttribute("role", "status");
    reading.textContent = "点按阶段条查看估算时间；下方列表保留各阶段的原始分钟数。";
    timeline.segments.forEach((segment, index) => {
      const y = top + visibleStages.indexOf(segment.stage) * row + 7;
      const minutes = (segment.end - segment.start) / 60000;
      const description = stageName(segment.stage) + " · " + duration(minutes) + " · 估算 " +
        beijing(segment.start, true) + " → " + beijing(segment.end, true);
      const rect = svg("rect", {
        x: x(segment.start), y, width: Math.max(0, x(segment.end) - x(segment.start)), height: 22, rx: 3,
        fill: stageColor(segment.stage), tabindex: 0, role: "button", "aria-label": description,
        "data-intraday-stage": segment.stage, "data-duration-minutes": minutes,
        "data-start": segment.start, "data-end": segment.end, "data-index": index,
        class: "intraday-sleep-segment"
      });
      rect.append(svg("title", {}, description));
      const select = () => {
        reading.textContent = description;
        reading.dataset.stage = segment.stage; reading.dataset.start = segment.start; reading.dataset.end = segment.end;
        chart.querySelectorAll(".is-selected").forEach(node => node.classList.remove("is-selected"));
        rect.classList.add("is-selected");
      };
      rect.addEventListener("click", select);
      rect.addEventListener("focus", select);
      rect.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
      });
      chart.append(rect);
    });
    const wrap = el("div", "intraday-chart-wrap intraday-sleep-chart-wrap");
    wrap.append(chart); body.append(wrap, reading);
  }
  function sleepSequence(body, stages) {
    const container = el("div", "intraday-stage-sequence");
    container.id = "intraday-sleep-chart";
    container.dataset.mode = "sequence";
    container.setAttribute("aria-label", "按原始顺序排列的阶段，没有推算钟点");
    stages.forEach((stage, index) => {
      const item = el("div", "intraday-sequence-item");
      item.dataset.intradayStage = stage[0]; item.dataset.durationMinutes = String(stage[1]);
      const dot = el("i"); dot.style.background = stageColor(stage[0]);
      item.append(el("small", "", String(index + 1).padStart(2, "0")), dot,
        el("span", "", stageName(stage[0])), el("strong", "", duration(stage[1])));
      container.append(item);
    });
    body.append(container);
  }
  function sleepRecords(body, stages, timeline) {
    const records = el("details", "intraday-records");
    records.id = "intraday-sleep-records";
    records.append(el("summary", "", `查看 ${num(stages.length)} 段原始阶段时长`));
    const table = el("table", "intraday-table intraday-sleep-table");
    const head = el("thead"), row = el("tr");
    for (const title of ["顺序", "阶段", "原始时长", timeline.valid ? "估算钟点" : "钟点"]) row.append(el("th", "", title));
    head.append(row); table.append(head);
    const tbody = el("tbody");
    stages.forEach((stage, index) => {
      const line = el("tr"), segment = timeline.segments[index];
      line.append(el("td", "", index + 1), el("td", "", stageName(stage[0])), el("td", "", duration(stage[1])),
        el("td", "", segment ? beijing(segment.start) + "–" + beijing(segment.end) : "无法定位"));
      tbody.append(line);
    });
    table.append(tbody); records.append(table); body.append(records);
  }
  function render(opts) {
    if (!["heart", "sleep"].includes(opts.category)) return null;
    if (archive !== opts.data.intraday) {
      archive = opts.data.intraday;
      preferences.heart.reading = null;
      preferences.heart.readingIndex = null;
      preferences.sleep.session = 0;
    }
    const section = el("section", "detail-card intraday-panel");
    section.id = "intraday-panel"; section.dataset.category = opts.category;
    section.setAttribute("aria-labelledby", "intraday-title");
    const header = el("div", "intraday-heading");
    const title = el("div");
    title.append(el("p", "intraday-eyebrow", "一天中的细节"));
    const heading = el("h3", "", opts.category === "heart" ? "日内心率曲线" : "睡眠阶段时间轴");
    heading.id = "intraday-title"; title.append(heading);
    header.append(title, dateControls(opts)); section.append(header);
    if (opts.category === "heart") heart(section, opts);
    else sleep(section, opts);
    return section;
  }
  window.MiFitIntradayView = Object.freeze({ render });
})();
