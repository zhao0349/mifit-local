/* Paginated source records inside the existing detail dialog. */
(function (root) {
  "use strict";
  const model = root.MiFitRecordModel;
  const PAGE_SIZE = 10;
  const preferences = {};
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function text(value) {
    if (value === null || value === undefined) return "—";
    if (Array.isArray(value)) {
      const names = { deep: "深睡", light: "浅睡", rem: "REM", awake: "清醒", unknown: "未知阶段" };
      return value.map(item => (names[item.stage] || "未知阶段") + " " + (item.minutes ?? "—") + " 分钟").join(" → ") || "空列表";
    }
    if (typeof value === "boolean") return value ? "是" : "否";
    // Preserve source precision, especially GPS coordinates and scale readings.
    return String(value);
  }
  function csvCell(value) {
    if (value === null || value === undefined) return "";
    let result = Array.isArray(value) ? JSON.stringify(value) : String(value);
    if (typeof value !== "number" && /^\s*[=+\-@\t\r\n]/.test(result)) result = "'" + result;
    return '"' + result.replaceAll('"', '""') + '"';
  }
  function render({ data, category, start, end }) {
    const sources = Object.keys(model.schema).filter(key => model.schema[key].category === category);
    const preference = preferences[category] || (preferences[category] = { source: sources[0], invalid: false });
    const panel = el("section", "detail-card detail-source-records");
    panel.id = "detail-source-records";
    const head = el("div", "detail-panel-head");
    head.append(el("h3", "", "导出明细"));
    panel.append(head, el("p", "detail-caption",
      "逐条保留来源记录，不合并同日测量。时间字段保留原始时区；筛选按北京时间（睡眠按醒来日期）。空值表示未提供，来源数值原样展示，不作健康判定。"));
    const controls = el("div", "detail-source-controls");
    const label = el("label", "", "数据类型 ");
    const select = el("select");
    select.id = "detail-source-select";
    for (const key of sources) {
      const option = el("option", "", model.schema[key].label + " · " + (data.record_details?.datasets?.[key]?.rows.length || 0) + " 条");
      option.value = key;
      select.append(option);
    }
    select.value = preference.source;
    label.append(select);
    const invalidLabel = el("label");
    const invalid = el("input");
    invalid.type = "checkbox";
    invalid.id = "detail-source-invalid";
    invalid.checked = preference.invalid;
    invalidLabel.append(invalid, document.createTextNode("仅查看日期缺失或无效的记录"));
    controls.append(label, invalidLabel);
    panel.append(controls);
    const summary = el("p", "detail-caption");
    summary.id = "detail-source-count";
    const content = el("div", "detail-source-list");
    const footer = el("div", "detail-table-footer");
    panel.append(summary, content, footer);
    let page = 0, indices = [], dataset, fields, invalidCount = 0;
    function filter() {
      dataset = data.record_details?.datasets?.[preference.source];
      fields = dataset ? dataset.fields.map(key => model.schema[preference.source].fields.find(field => field[0] === key)) : [];
      indices = [];
      invalidCount = 0;
      if (dataset) dataset.rows.forEach((cells, index) => {
        const key = model.schema[preference.source].date;
        const date = model.day(preference.source, { [key]: cells[dataset.fields.indexOf(key)] });
        if (!date) invalidCount++;
        if (preference.invalid ? !date : date && (!start || date >= start) && (!end || date <= end)) indices.push(index);
      });
      page = 0;
      update();
    }
    function action(label, id, callback) {
      const node = el("button", "detail-button", label);
      node.type = "button"; node.id = id; node.addEventListener("click", callback);
      return node;
    }
    function update() {
      const count = dataset?.rows.length || 0;
      const pages = Math.max(1, Math.ceil(indices.length / PAGE_SIZE));
      summary.textContent = "当前筛选 " + indices.length + " / " + count + " 条 · 日期缺失或无效 " + invalidCount + " 条 · 每页 10 条";
      content.replaceChildren();
      if (!count) content.append(el("p", "detail-empty",
        "没有逐条明细。旧版日汇总无法恢复原始字段，请重新导入原始 JSON 导出文件。"));
      else if (!indices.length) content.append(el("p", "detail-empty", "当前筛选没有记录，可调整日期范围或切换日期无效记录。"));
      for (const index of indices.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) {
        const row = model.unpack(dataset, dataset.rows[index]);
        const entry = el("details", "detail-source-entry");
        entry.dataset.sourceRow = String(index);
        const heading = el("summary");
        const primary = preference.source === "body_measurements" ? " · 体重 " + text(row.weight_kg) + " kg" :
          preference.source === "workouts" ? " · " + text(row.activity_type) : "";
        heading.textContent = "#" + (index + 1) + " · " + text(row[model.schema[preference.source].date]) + primary;
        entry.append(heading);
        const grid = el("dl", "detail-source-values");
        fields.forEach(([key, label, unit], column) => {
          const cell = el("div");
          cell.dataset.sourceField = key;
          const name = el("dt", "", label + (unit ? " (" + unit + ")" : ""));
          const code = el("small", "", key);
          name.append(code);
          cell.append(name, el("dd", "", text(dataset.rows[index][column])));
          grid.append(cell);
        });
        entry.append(grid);
        content.append(entry);
      }
      footer.replaceChildren();
      const previous = action("上一页", "detail-source-prev", () => { page--; update(); });
      const next = action("下一页", "detail-source-next", () => { page++; update(); });
      previous.disabled = page === 0; next.disabled = page >= pages - 1;
      const download = action("导出筛选明细 CSV", "detail-source-export", () => {
        const lines = [fields.map(([key, label, unit]) => csvCell(label + (unit ? "(" + unit + ")" : "") + " [" + key + "]")).join(",")];
        for (const index of indices) lines.push(dataset.rows[index].map(csvCell).join(","));
        const url = URL.createObjectURL(new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" }));
        const link = el("a");
        link.href = url; link.download = preference.source + "_明细.csv";
        panel.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
      });
      download.disabled = !indices.length;
      footer.append(previous, el("span", "", (page + 1) + " / " + pages), next, download);
    }
    select.addEventListener("change", () => { preference.source = select.value; filter(); });
    invalid.addEventListener("change", () => { preference.invalid = invalid.checked; filter(); });
    filter();
    panel.append(el("p", "detail-caption",
      "包含已知导出健康字段、阶段与轨迹坐标；账号、设备、服务定位标识和未知扩展字段不带入。GPS 以坐标明细展示，不请求在线地图。"));
    return panel;
  }
  root.MiFitRecordView = Object.freeze({ render });
})(window);
