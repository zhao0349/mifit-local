/* Shared, bounded health-record archive. Schema is supplied by the HTML builder. */
(function (root) {
  "use strict";
  const schema = root.MiFitRecordSchema;
  const fieldTypes = Object.fromEntries(Object.entries(schema).map(([source, definition]) =>
    [source, Object.fromEntries(definition.fields.map(([key, , , type]) => [key, type]))]));
  const rangeCache = new WeakMap();
  const MAX_ROWS = 2000000;
  const MAX_CELLS = 40000000;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  function value(raw, type) {
    if (raw === null || raw === undefined) return null;
    if (type === "text") {
      if (typeof raw !== "string") return null;
      if (raw.length > 1024) throw new RangeError("record text limit");
      return raw;
    }
    if (type === "boolean") return [true, 1, "1"].includes(raw) ? true :
      [false, 0, "0"].includes(raw) ? false : null;
    if (type === "stages") {
      if (typeof raw === "string") { try { raw = JSON.parse(raw); } catch (_) { return null; } }
      if (!Array.isArray(raw)) return null;
      if (raw.length > 10000) throw new RangeError("stage limit");
      return raw.filter(object).map(item => ({
        stage: ["deep", "light", "rem", "awake"].includes(item.stage) ? item.stage : "unknown",
        minutes: value(item.minutes, "number")
      }));
    }
    if (typeof raw === "string" && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw.trim())) raw = Number(raw);
    return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
  }
  function day(source, row) {
    const raw = row[schema[source].date];
    if (typeof raw !== "string") return null;
    const match = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?(Z|[+-]\d{2}:\d{2})?)?$/.exec(raw);
    if (!match) return null;
    const date = match[1], parsed = new Date(date + "T00:00:00Z");
    if (Number(date.slice(0, 4)) < 1 || !Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date ||
        Number(match[2] || 0) > 23 || Number(match[3] || 0) > 59 || Number(match[4] || 0) > 59) return null;
    if (source === "daily_activity") return raw === date ? date : null;
    let offset = 480;
    if (match[5] === "Z") offset = 0;
    else if (match[5]) {
      const hours = Number(match[5].slice(1, 3)), minutes = Number(match[5].slice(4, 6));
      if (hours > 23 || minutes > 59) return null;
      offset = (match[5][0] === "-" ? -1 : 1) * (hours * 60 + minutes);
    }
    parsed.setUTCMinutes(Number(match[2] || 0) * 60 + Number(match[3] || 0) + 480 - offset);
    const result = parsed.toISOString().slice(0, 10);
    return /^\d{4}-\d{2}-\d{2}$/.test(result) ? result : null;
  }
  class Builder {
    constructor() { this.archive = { version: 1, datasets: {} }; this.rows = 0; this.cells = 0; }
    add(source, row) {
      if (!Object.hasOwn(schema, source) || !object(row)) throw new TypeError("record source");
      // Additive known columns are retained only when present; old exports do not
      // acquire fabricated values. Rows remain in source order, including duplicates.
      let dataset = this.archive.datasets[source];
      if (!dataset) dataset = this.archive.datasets[source] = { fields: [], rows: [] };
      for (const [key] of schema[source].fields) {
        if (Object.hasOwn(row, key) && !dataset.fields.includes(key)) {
          dataset.fields.push(key);
          this.cells += dataset.rows.length;
          for (const previous of dataset.rows) previous.push(null);
        }
      }
      this.rows++;
      this.cells += dataset.fields.length;
      if (this.rows > MAX_ROWS || this.cells > MAX_CELLS) throw new RangeError("record limit");
      const cells = dataset.fields.map(key => value(row[key], fieldTypes[source][key]));
      this.cells += cells.reduce((sum, cell) => sum + (Array.isArray(cell) ? cell.length * 2 : 0), 0);
      if (this.cells > MAX_CELLS) throw new RangeError("record limit");
      dataset.rows.push(cells);
    }
  }
  function normalize(input) {
    const archive = { version: 1, datasets: {} };
    if (input === null || input === undefined) return archive;
    if (!object(input) || input.version !== 1 || !object(input.datasets)) throw new TypeError("record archive");
    let rowCount = 0, cellCount = 0;
    for (const [source, dataset] of Object.entries(input.datasets)) {
      if (!Object.hasOwn(schema, source)) continue;
      if (!object(dataset) || !Array.isArray(dataset.fields) || !Array.isArray(dataset.rows) ||
          dataset.fields.length > 256 || new Set(dataset.fields).size !== dataset.fields.length) throw new TypeError("record columns");
      const columns = dataset.fields.map((key, index) => ({ key, index }))
        .filter(({ key }) => Object.hasOwn(fieldTypes[source], key));
      rowCount += dataset.rows.length;
      cellCount += dataset.rows.length * columns.length;
      if (rowCount > MAX_ROWS || cellCount > MAX_CELLS) throw new RangeError("record limit");
      const rows = dataset.rows.map(cells => {
        if (!Array.isArray(cells) || cells.length !== dataset.fields.length) throw new TypeError("record row");
        return columns.map(({ key, index }) => {
          const result = value(cells[index], fieldTypes[source][key]);
          if (Array.isArray(result)) cellCount += result.length * 2;
          if (cellCount > MAX_CELLS) throw new RangeError("record limit");
          return result;
        });
      });
      archive.datasets[source] = { fields: columns.map(({ key }) => key), rows };
    }
    return archive;
  }
  function unpack(dataset, cells) {
    return Object.fromEntries(dataset.fields.map((key, index) => [key, cells[index]]));
  }
  function range(archive) {
    if (archive && rangeCache.has(archive)) return rangeCache.get(archive);
    let start = null, end = null;
    for (const [source, dataset] of Object.entries(archive?.datasets || {})) {
      const key = schema[source].date, index = dataset.fields.indexOf(key);
      for (const cells of dataset.rows) {
        const date = day(source, { [key]: cells[index] });
        if (date) { if (!start || date < start) start = date; if (!end || date > end) end = date; }
      }
    }
    const result = Object.freeze({ start, end });
    if (archive) rangeCache.set(archive, result);
    return result;
  }
  root.MiFitRecordModel = Object.freeze({ schema, Builder, normalize, unpack, day, range });
})(typeof window !== "undefined" ? window : globalThis);
