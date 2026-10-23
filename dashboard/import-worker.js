/* Embedded, offline worker. No imports, network calls, or persistent storage. */
(function () {
  "use strict";

  const MAX_FILE = 1024 * 1024 * 1024;
  const SMALL_SUMMARY = 128 * 1024 * 1024;
  const CHUNK_BYTES = 256 * 1024;
  const MAX_RECORD = 16 * 1024 * 1024;
  const SOURCES = [
    "daily_activity", "sleep", "workouts", "body_measurements", "heart_rate",
    "spo2", "stress", "abnormal_heart_beat", "workout_detail", "workout_gps"
  ];
  const SOURCE_SET = new Set(SOURCES);
  const SKIPPED = new Set(["abnormal_heart_beat", "workout_detail", "workout_gps"]);
  const STAGES = ["deep", "light", "rem", "awake"];
  const INTRADAY = globalThis.MiFitIntradayModel;
  const FIELDS = [
    "steps", "distance_km", "active_kcal", "active_minutes", "sleep_minutes",
    "sleep_score", "nap_minutes", "heart_rate_avg", "heart_rate_min",
    "heart_rate_max", "resting_hr", "spo2_avg", "stress_avg", "weight_kg",
    "body_fat_pct", "workout_minutes"
  ];
  const NOTES = [
    "数据只在当前浏览器中读取和汇总，不上传网络，不保存原始账号或设备标识。",
    "时间戳按北京时间归日；无时区时间按北京时间解释。活动日期保留来源日期。",
    "同一天活动取步数最大的一条；同值优先较新的采集记录，其余活动指标取自同一条记录。",
    "主睡眠按醒来日期归日，只取最长的非小睡记录，睡眠分钟使用实际睡着时间。",
    "小睡按醒来日期归日；重叠时间段只保留最长记录的实际睡着时间，再合计不重叠的组。",
    "心率、静息心率、血氧、压力采用有效样本日均值；静息心率仅取 resting 样本。",
    "体重和体脂取当天最后一次测量，不用其他记录补齐缺失指标；缺失值保留为空。",
    "运动概览不合并不同来源记录；秒级运动明细、GPS 和异常心跳仅计数，不保留原始值。",
    "单日心率保留有效原始采样点，不做平均或抽样；同一时刻的多条记录分别保留。",
    "睡眠阶段保留来源顺序与时长；来源未保留各阶段起止时间，时间轴只作近似排列，剩余时长不定位。"
  ];
  const ERRORS = {
    file: "请选择一个有效的 JSON 文件。",
    size: "文件超过 1 GB，暂不支持在浏览器中导入。",
    format: "JSON 文件不完整、格式不正确或不是支持的运动健康导出文件。",
    summary: "摘要文件超过 128 MB，请使用原始运动健康 JSON 导出文件。",
    limit: "文件中的单条记录或汇总规模过大，请缩小导出日期范围后重试。",
    read: "无法读取本地文件，请重新选择文件后重试。",
    busy: "正在导入，请等待当前导入完成。"
  };

  class ImportFailure extends Error {
    constructor(code) {
      super(ERRORS[code] || ERRORS.format);
      this.code = code;
    }
  }
  function fail(code = "format") { throw new ImportFailure(code); }
  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }
  function number(value, positive = false, maximum = null, integer = false) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value === "string") {
      if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim())) return null;
      value = Number(value);
    }
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0 ||
        (positive && value <= 0) || (maximum !== null && value > maximum) ||
        (integer && !Number.isInteger(value))) return null;
    return value;
  }
  function calendarDate(value) {
    if (typeof value !== "string") return null;
    const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!parts) return null;
    const y = +parts[1], m = +parts[2], d = +parts[3];
    const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
    const lengths = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return y >= 1 && m >= 1 && m <= 12 && d >= 1 && d <= lengths[m - 1] ? value : null;
  }
  function localTime(value) {
    if (typeof value !== "string") return null;
    const p = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?(Z|[+-]\d{2}:\d{2})?)?$/.exec(value);
    if (!p || !calendarDate(p[1])) return null;
    const hour = +(p[2] || 0), minute = +(p[3] || 0), second = +(p[4] || 0);
    if (hour > 23 || minute > 59 || second > 59) return null;
    let offset = 480;
    if (p[6] === "Z") offset = 0;
    else if (p[6]) {
      const oh = +p[6].slice(1, 3), om = +p[6].slice(4, 6);
      if (oh > 23 || om > 59) return null;
      offset = (p[6][0] === "-" ? -1 : 1) * (oh * 60 + om);
    }
    const [year, month, day] = p[1].split("-").map(Number);
    const utc = new Date(0);
    utc.setUTCFullYear(year, month - 1, day);
    utc.setUTCHours(hour, minute, second, +((p[5] || "") + "000").slice(0, 3));
    const instant = utc.getTime() - offset * 60000;
    const local = new Date(instant + 480 * 60000);
    if (local.getUTCFullYear() < 1 || local.getUTCFullYear() > 9999) return null;
    const iso = local.toISOString();
    // Preserve microseconds for same-millisecond ranking, as Python datetime
    // does. This value remains internal and is never sent in the summary.
    const micros = +((p[5] || "") + "000000").slice(3, 6);
    return { instant, order: BigInt(instant) * 1000n + BigInt(micros),
      day: iso.slice(0, 10), time: iso.slice(11, 16) };
  }
  function blankDay(date) {
    const result = { date };
    for (const key of FIELDS) result[key] = null;
    result.sleep_stages = Object.fromEntries(STAGES.map(key => [key, null]));
    result.workout_count = 0;
    return result;
  }
  function greater(a, b) {
    if (!b) return true;
    for (let i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) return a[i] > b[i];
    }
    return false;
  }
  function meanAdd(stat, value) {
    if (value === null) return;
    stat.count += 1;
    stat.mean += (value - stat.mean) / stat.count;
    stat.minimum = stat.minimum === null ? value : Math.min(stat.minimum, value);
    stat.maximum = stat.maximum === null ? value : Math.max(stat.maximum, value);
  }
  function meanResult(stat) {
    if (!stat.count || !Number.isFinite(stat.mean)) return null;
    if (stat.mean >= 2 ** 46) return stat.mean;
    // Match Python round(value, 2): use the exact binary float, then nearest
    // hundredth with ties to even. Multiplying by 100 first introduces a
    // second rounding (for example 2.675), and Math.round uses different ties.
    const view = new DataView(new ArrayBuffer(8));
    view.setFloat64(0, stat.mean);
    const bits = view.getBigUint64(0);
    const exponentBits = Number((bits >> 52n) & 2047n);
    const mantissa = (bits & ((1n << 52n) - 1n)) | (exponentBits ? 1n << 52n : 0n);
    const exponent = exponentBits ? exponentBits - 1075 : -1074;
    let rounded = mantissa * 100n;
    if (exponent >= 0) rounded <<= BigInt(exponent);
    else {
      const denominator = 1n << BigInt(-exponent);
      const remainder = rounded % denominator;
      rounded /= denominator;
      if (remainder * 2n > denominator ||
          (remainder * 2n === denominator && rounded % 2n === 1n)) rounded++;
    }
    return Number(rounded) / 100;
  }
  function parseStages(raw) {
    const result = Object.fromEntries(STAGES.map(key => [key, null]));
    if (raw === null || raw === undefined) return { result, invalid: false };
    let stages;
    try { stages = typeof raw === "string" ? JSON.parse(raw) : raw; }
    catch (_) { return { result, invalid: true }; }
    if (!Array.isArray(stages)) return { result, invalid: true };
    let invalid = false;
    for (const stage of stages) {
      if (!isObject(stage) || !STAGES.includes(stage.stage)) { invalid = true; continue; }
      const minutes = number(stage.minutes);
      if (minutes === null) { invalid = true; continue; }
      result[stage.stage] = (result[stage.stage] || 0) + minutes;
    }
    return { result, invalid };
  }
  function workoutType(value) {
    return typeof value === "string" && value.trim() ? value.slice(0, 80) : "other";
  }
  function intradayStages(raw) {
    let stages;
    try { stages = typeof raw === "string" ? JSON.parse(raw) : raw; }
    catch (_) { return { stages: [], sequenceValid: false }; }
    if (raw === null || raw === undefined) return { stages: [], sequenceValid: true };
    if (!Array.isArray(stages)) return { stages: [], sequenceValid: false };
    if (stages.length > INTRADAY.limits.maxStagesPerSession) fail("limit");
    const result = [];
    let sequenceValid = true;
    for (const stage of stages) {
      if (!isObject(stage) || typeof stage.stage !== "string") { sequenceValid = false; continue; }
      const minutes = number(stage.minutes);
      if (minutes === null) { sequenceValid = false; continue; }
      result.push([STAGES.includes(stage.stage) ? stage.stage : "unknown", minutes]);
    }
    return { stages: result, sequenceValid };
  }
  function normalizedIntraday(value) {
    try { return INTRADAY.normalize(value); }
    catch (error) { fail(error instanceof RangeError ? "limit" : "format"); }
  }
  function summaryEnvelope(days, workouts, counts, notes, intraday) {
    // Even individually finite samples can overflow when durations are added.
    // Keep the public summary contract finite-number-or-null in that case.
    for (const day of days) {
      for (const key of FIELDS) if (!Number.isFinite(day[key])) day[key] = null;
      for (const key of STAGES) {
        if (!Number.isFinite(day.sleep_stages[key])) day.sleep_stages[key] = null;
      }
    }
    days.sort((a, b) => a.date.localeCompare(b.date));
    workouts.sort((a, b) => a.date.localeCompare(b.date) ||
      a.start.localeCompare(b.start) || a.type.localeCompare(b.type));
    return {
      schema_version: 1,
      meta: {
        generated_at: new Date(Date.now() + 480 * 60000).toISOString().replace("Z", "+08:00"),
        timezone: "Asia/Shanghai",
        range: { start: days.length ? days[0].date : null, end: days.length ? days[days.length - 1].date : null },
        source_counts: counts,
        notes,
        omitted: ["gps", "account_identifiers", "device_identifiers", "workout_detail_samples", "spo2_stress_raw_samples"]
      },
      days,
      workouts,
      intraday: normalizedIntraday(intraday)
    };
  }

  class Aggregator {
    constructor() {
      this.counts = Object.fromEntries(SOURCES.map(key => [key, 0]));
      this.days = new Map();
      this.activities = new Map();
      this.sleeps = new Map();
      this.naps = new Map();
      this.means = new Map();
      this.bodyTimes = new Map();
      this.workouts = [];
      this.badDates = 0;
      this.badStages = 0;
      this.intraday = { version: 1, heart: {}, sleep: {} };
      this.heartPoints = 0;
      this.sleepSessions = 0;
      this.stageItems = 0;
    }
    day(key) {
      if (!this.days.has(key)) {
        if (this.days.size >= 40000) fail("limit");
        this.days.set(key, blankDay(key));
      }
      return this.days.get(key);
    }
    count(source) {
      if (SOURCE_SET.has(source)) this.counts[source] += 1;
    }
    add(source, row) {
      if (!isObject(row)) fail();
      this.count(source);
      if (source === "daily_activity") this.activity(row);
      else if (source === "sleep") this.sleep(row);
      else if (source === "heart_rate" || source === "spo2" || source === "stress") this.metric(source, row);
      else if (source === "body_measurements") this.body(row);
      else if (source === "workouts") this.workout(row);
    }
    activity(row) {
      const key = calendarDate(row.date);
      if (!key) { this.badDates++; return; }
      const steps = number(row.steps, false, null, true);
      const collected = localTime(row.collected_at);
      const rank = [steps === null ? -1 : steps, collected ? collected.order : -Infinity];
      if (!greater(rank, this.activities.get(key))) return;
      this.activities.set(key, rank);
      const distance = number(row.distance_m);
      Object.assign(this.day(key), {
        steps, distance_km: distance === null ? null : distance / 1000,
        active_kcal: number(row.active_kcal), active_minutes: number(row.active_minutes)
      });
    }
    sleep(row) {
      const start = localTime(row.start_at), end = localTime(row.end_at);
      if (!start || !end || end.order < start.order) { this.badDates++; return; }
      const key = end.day, day = this.day(key);
      const duration = number(row.duration_minutes), asleep = number(row.time_asleep_minutes);
      const rank = [duration === null ? Number(end.order - start.order) / 60000000 : duration,
        asleep === null ? -1 : asleep, end.order];
      if (end.instant > start.instant && end.instant - start.instant <= INTRADAY.limits.maxSleepSpanMs) {
        const sessions = this.intraday.sleep[key] || (this.intraday.sleep[key] = []);
        const stages = intradayStages(row.stages);
        this.sleepSessions++;
        this.stageItems += stages.stages.length;
        if (this.sleepSessions > INTRADAY.limits.maxSleepSessions ||
            sessions.length >= INTRADAY.limits.maxSleepPerDay ||
            this.stageItems > INTRADAY.limits.maxStageItems) fail("limit");
        const session = {
          start: start.instant, end: end.instant,
          nap: row.is_nap === true || row.is_nap === 1 || row.is_nap === "1",
          asleep, score: number(row.sleep_score, false, 100),
          stages: stages.stages, timing: "sequence"
        };
        if (!stages.sequenceValid) session.sequenceValid = false;
        sessions.push(session);
      }
      if (row.is_nap === true || row.is_nap === 1 || row.is_nap === "1") {
        let left = start.order, right = end.order, bestRank = rank, bestAsleep = asleep;
        const remaining = [];
        for (const old of this.naps.get(key) || []) {
          if (left < old.end && old.start < right) {
            left = left < old.start ? left : old.start;
            right = right > old.end ? right : old.end;
            if (greater(old.rank, bestRank)) { bestRank = old.rank; bestAsleep = old.asleep; }
          } else remaining.push(old);
        }
        remaining.push({ start: left, end: right, rank: bestRank, asleep: bestAsleep });
        remaining.sort((a, b) => a.start < b.start ? -1 : a.start > b.start ? 1 : 0);
        this.naps.set(key, remaining);
        return;
      }
      if (!greater(rank, this.sleeps.get(key))) return;
      this.sleeps.set(key, rank);
      const stages = parseStages(row.stages);
      if (stages.invalid) this.badStages++;
      day.sleep_minutes = asleep;
      day.sleep_score = number(row.sleep_score, false, 100);
      day.sleep_stages = stages.result;
    }
    addMean(key, field, value) {
      const id = key + "|" + field;
      if (!this.means.has(id)) this.means.set(id, {
        key, field, count: 0, mean: 0, minimum: null, maximum: null
      });
      meanAdd(this.means.get(id), value);
    }
    metric(source, row) {
      const stamp = localTime(row.timestamp);
      if (!stamp) { this.badDates++; return; }
      const key = stamp.day;
      this.day(key);
      if (source === "heart_rate") {
        const value = number(row.bpm, true);
        this.addMean(key, "heart_rate_avg", value);
        if (row.sample_type === "resting") this.addMean(key, "resting_hr", value);
        if (value !== null) {
          const points = this.intraday.heart[key] || (this.intraday.heart[key] = []);
          this.heartPoints++;
          if (this.heartPoints > INTRADAY.limits.maxHeartPoints ||
              points.length >= INTRADAY.limits.maxHeartPerDay) fail("limit");
          const millis = ((stamp.instant + 480 * 60000) % 86400000 + 86400000) % 86400000;
          points.push([millis, value, row.sample_type === "resting" ? 1 : 0]);
        }
      } else if (source === "spo2") {
        this.addMean(key, "spo2_avg", number(row.spo2_pct, true, 100));
      } else {
        this.addMean(key, "stress_avg", number(row.stress_score, false, 100));
      }
    }
    body(row) {
      const stamp = localTime(row.timestamp);
      if (!stamp) { this.badDates++; return; }
      if (this.bodyTimes.has(stamp.day) && stamp.order <= this.bodyTimes.get(stamp.day)) return;
      this.bodyTimes.set(stamp.day, stamp.order);
      Object.assign(this.day(stamp.day), {
        weight_kg: number(row.weight_kg, true), body_fat_pct: number(row.body_fat_pct, false, 100)
      });
    }
    workout(row) {
      const stamp = localTime(row.start_at);
      if (!stamp) { this.badDates++; return; }
      if (this.workouts.length >= 100000) fail("limit");
      const duration = number(row.duration_minutes), distance = number(row.distance_m);
      this.workouts.push({
        date: stamp.day, start: stamp.time, type: workoutType(row.activity_type),
        duration_minutes: duration, distance_km: distance === null ? null : distance / 1000,
        calories_kcal: number(row.calories_kcal),
        avg_heart_rate_bpm: number(row.avg_heart_rate_bpm, true),
        avg_pace_sec_per_km: number(row.avg_pace_sec_per_km, true)
      });
      const day = this.day(stamp.day);
      day.workout_count++;
      if (duration !== null) day.workout_minutes = (day.workout_minutes || 0) + duration;
    }
    finish() {
      for (const [key, groups] of this.naps) {
        const values = groups.map(group => group.asleep).filter(value => value !== null);
        this.days.get(key).nap_minutes = values.length ? values.reduce((a, b) => a + b, 0) : null;
      }
      for (const stat of this.means.values()) {
        const day = this.days.get(stat.key);
        day[stat.field] = meanResult(stat);
        if (stat.field === "heart_rate_avg") {
          day.heart_rate_min = stat.minimum;
          day.heart_rate_max = stat.maximum;
        }
      }
      const notes = NOTES.slice();
      if (this.badDates) notes.push(`有 ${this.badDates} 条记录日期或时间无效，未纳入聚合，原始文件未改动。`);
      if (this.badStages) notes.push(`有 ${this.badStages} 条主睡眠阶段信息不完整，只保留可识别的有效阶段。`);
      return summaryEnvelope(Array.from(this.days.values()), this.workouts, this.counts, notes, this.intraday);
    }
  }

  /*
   * Incremental JSON grammar validator. Input is consumed synchronously within
   * each decoded chunk; no per-character promises and no complete-file parse.
   * Only one non-omitted dataset object is captured for JSON.parse at a time.
   * Strings/numbers in omitted objects are validated without retaining values.
   */
  class StreamParser {
    constructor(aggregate) {
      this.aggregate = aggregate;
      this.stack = [];
      this.lex = null;
      this.started = false;
      this.done = false;
      this.schemaVersion = undefined;
      this.hasRecords = false;
      this.rootKeys = new Set();
      this.capture = null;
      this.text = "";
    }
    parent() { return this.stack[this.stack.length - 1]; }
    appendCapture(end) {
      if (!this.capture) return;
      const part = this.text.slice(this.capture.from, end);
      this.capture.size += part.length;
      if (this.capture.size > MAX_RECORD) fail("limit");
      this.capture.parts.push(part);
      this.capture.from = end;
    }
    finishCapture(end, source) {
      this.appendCapture(end);
      const raw = this.capture.parts.join("");
      this.capture = null;
      let row;
      try { row = JSON.parse(raw); }
      catch (_) { fail(); }
      this.aggregate.add(source, row);
    }
    completeValue() {
      const parent = this.parent();
      if (!parent) this.done = true;
      else {
        if (parent.state !== "waiting") fail();
        parent.state = "comma";
      }
    }
    key(raw) {
      let key;
      try { key = JSON.parse(raw); } catch (_) { fail(); }
      const parent = this.parent();
      if (!parent || parent.kind !== "object") fail();
      if (parent.role === "root" && (key === "schema_version" || key === "records")) {
        if (this.rootKeys.has(key)) fail();
        this.rootKeys.add(key);
      }
      if (parent.role === "records" && SOURCE_SET.has(key)) {
        if (parent.seen.has(key)) fail();
        parent.seen.add(key);
      }
      parent.key = key;
      parent.state = "colon";
    }
    scalar(lex) {
      if (lex.schema) {
        try { this.schemaVersion = JSON.parse(lex.raw); } catch (_) { fail(); }
      }
      this.lex = null;
      this.completeValue();
    }
    begin(c, index) {
      const parent = this.parent();
      let role = "other", source = null;
      const schema = !!(parent && parent.role === "root" && parent.key === "schema_version");
      if (!parent) {
        if (this.started || c !== "{") fail();
        this.started = true;
        role = "root";
      } else {
        if (parent.role === "root" && parent.key === "records") {
          if (c !== "{") fail();
          role = "records";
          this.hasRecords = true;
        } else if (parent.role === "records") {
          if (c !== "[") fail();
          role = "dataset";
          source = parent.key;
        } else if (parent.role === "dataset") {
          if (c !== "{") fail();
          role = "row";
          source = parent.source;
          if (SOURCE_SET.has(source) && !SKIPPED.has(source)) {
            if (this.capture) fail();
            this.capture = { parts: [], size: 0, from: index };
          }
        }
        parent.state = "waiting";
      }
      if (c === "{" || c === "[") {
        if (this.stack.length >= 128) fail("limit");
        this.stack.push({
          kind: c === "{" ? "object" : "array", state: "first",
          role, source, key: null, schema, seen: role === "records" ? new Set() : null
        });
      } else if (c === '"') {
        this.lex = { kind: "string", key: false, escape: false, unicode: 0, schema, raw: schema ? '"' : "" };
      } else if (c === "-" || (c >= "0" && c <= "9")) {
        this.lex = { kind: "number", state: c === "-" ? "minus" : c === "0" ? "zero" : "int",
          schema, raw: schema ? c : "" };
      } else if (c === "t" || c === "f" || c === "n") {
        const expected = c === "t" ? "true" : c === "f" ? "false" : "null";
        this.lex = { kind: "literal", expected, position: 1, schema, raw: schema ? expected : "" };
      } else fail();
    }
    close(c, end) {
      const frame = this.stack.pop();
      if (!frame || (frame.kind === "object" ? c !== "}" : c !== "]")) fail();
      if (frame.schema) this.schemaVersion = null;
      if (frame.role === "row") {
        if (SOURCE_SET.has(frame.source) && !SKIPPED.has(frame.source)) this.finishCapture(end, frame.source);
        else this.aggregate.count(frame.source);
      }
      this.completeValue();
    }
    numberChar(c, lex) {
      const digit = c >= "0" && c <= "9";
      switch (lex.state) {
        case "minus":
          if (!digit) fail();
          lex.state = c === "0" ? "zero" : "int"; return true;
        case "zero":
          if (digit) fail();
          break;
        case "int":
          if (digit) return true;
          break;
        case "fractionStart":
          if (!digit) fail();
          lex.state = "fraction"; return true;
        case "fraction":
          if (digit) return true;
          if (c === "e" || c === "E") { lex.state = "exponentStart"; return true; }
          return false;
        case "exponentStart":
          if (c === "+" || c === "-") { lex.state = "exponentSign"; return true; }
          if (!digit) fail();
          lex.state = "exponent"; return true;
        case "exponentSign":
          if (!digit) fail();
          lex.state = "exponent"; return true;
        case "exponent":
          return digit;
        default: fail();
      }
      if (c === ".") { lex.state = "fractionStart"; return true; }
      if (c === "e" || c === "E") { lex.state = "exponentStart"; return true; }
      return false;
    }
    feed(text, final = false) {
      this.text = text;
      if (this.capture) this.capture.from = 0;
      for (let i = 0; i < text.length;) {
        const c = text[i], lex = this.lex;
        if (lex) {
          if (lex.kind === "string") {
            if (lex.key || lex.schema) {
              lex.raw += c;
              if (lex.raw.length > 65536) fail("limit");
            }
            if (lex.unicode) {
              if (!/[0-9a-fA-F]/.test(c)) fail();
              lex.unicode--;
            } else if (lex.escape) {
              lex.escape = false;
              if (c === "u") lex.unicode = 4;
              else if (!'"\\/bfnrt'.includes(c)) fail();
            } else if (c === "\\") lex.escape = true;
            else if (c === '"') {
              if (lex.key) { this.key(lex.raw); this.lex = null; }
              else this.scalar(lex);
            } else if (c.charCodeAt(0) < 32) fail();
            i++;
            continue;
          }
          if (lex.kind === "literal") {
            if (c !== lex.expected[lex.position]) fail();
            lex.position++;
            if (lex.position === lex.expected.length) this.scalar(lex);
            i++;
            continue;
          }
          if (this.numberChar(c, lex)) {
            if (lex.schema) {
              lex.raw += c;
              if (lex.raw.length > 128) fail();
            }
            i++;
            continue;
          }
          if (!" \t\r\n,]}".includes(c) ||
              !["zero", "int", "fraction", "exponent"].includes(lex.state)) fail();
          this.scalar(lex);
          continue; // The delimiter still belongs to the surrounding grammar.
        }
        if (" \t\r\n".includes(c)) { i++; continue; }
        if (this.done) fail();
        const frame = this.parent();
        if (!frame) { this.begin(c, i); i++; continue; }
        if (frame.kind === "object") {
          if (frame.state === "first" || frame.state === "key") {
            if (c === "}" && frame.state === "first") this.close(c, i + 1);
            else if (c === '"') this.lex = {
              kind: "string", key: true, escape: false, unicode: 0, schema: false, raw: '"'
            };
            else fail();
          } else if (frame.state === "colon") {
            if (c !== ":") fail();
            frame.state = "value";
          } else if (frame.state === "value") this.begin(c, i);
          else if (frame.state === "comma") {
            if (c === ",") frame.state = "key";
            else if (c === "}") this.close(c, i + 1);
            else fail();
          } else fail();
        } else {
          if (frame.state === "first" || frame.state === "value") {
            if (c === "]" && frame.state === "first") this.close(c, i + 1);
            else this.begin(c, i);
          } else if (frame.state === "comma") {
            if (c === ",") frame.state = "value";
            else if (c === "]") this.close(c, i + 1);
            else fail();
          } else fail();
        }
        i++;
      }
      if (this.capture) this.appendCapture(text.length);
      this.text = "";
      if (final) {
        if (this.lex && this.lex.kind === "number" &&
            ["zero", "int", "fraction", "exponent"].includes(this.lex.state)) this.scalar(this.lex);
        if (this.lex || this.stack.length || !this.done || this.capture) fail();
      }
    }
  }

  function normalizeSummary(input) {
    if (!isObject(input) || input.schema_version !== 1 ||
        !Array.isArray(input.days) || !Array.isArray(input.workouts)) fail();
    if (input.days.length > 40000 || input.workouts.length > 100000) fail("limit");
    const seen = new Set(), days = [];
    for (const item of input.days) {
      if (!isObject(item) || !calendarDate(item.date) || seen.has(item.date)) fail();
      seen.add(item.date);
      const day = blankDay(item.date);
      for (const field of FIELDS) {
        const positive = ["heart_rate_avg", "heart_rate_min", "heart_rate_max", "resting_hr", "spo2_avg", "weight_kg"].includes(field);
        const maximum = ["sleep_score", "spo2_avg", "stress_avg", "body_fat_pct"].includes(field) ? 100 : null;
        day[field] = number(item[field], positive, maximum, field === "steps");
      }
      day.workout_count = number(item.workout_count, false, null, true) || 0;
      if (isObject(item.sleep_stages)) {
        for (const stage of STAGES) day.sleep_stages[stage] = number(item.sleep_stages[stage]);
      }
      days.push(day);
    }
    const workouts = [];
    for (const item of input.workouts) {
      if (!isObject(item) || !calendarDate(item.date) ||
          typeof item.start !== "string" || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(item.start)) fail();
      workouts.push({
        date: item.date, start: item.start, type: workoutType(item.type),
        duration_minutes: number(item.duration_minutes), distance_km: number(item.distance_km),
        calories_kcal: number(item.calories_kcal),
        avg_heart_rate_bpm: number(item.avg_heart_rate_bpm, true),
        avg_pace_sec_per_km: number(item.avg_pace_sec_per_km, true)
      });
    }
    const supplied = isObject(input.meta) && isObject(input.meta.source_counts) ? input.meta.source_counts : {};
    const counts = Object.fromEntries(SOURCES.map(key => [key, number(supplied[key], false, null, true) || 0]));
    return summaryEnvelope(days, workouts, counts, [
      "已导入日汇总摘要，只保留仪表盘支持的字段；原文件中的额外标识和说明未带入。",
      ...NOTES
    ], input.intraday);
  }

  async function importFile(file, progress) {
    if (!file || typeof file.name !== "string" || !/\.json$/i.test(file.name) ||
        typeof file.slice !== "function" || !Number.isSafeInteger(file.size) || file.size <= 0) fail("file");
    if (file.size > MAX_FILE) fail("size");
    const aggregate = new Aggregator();
    const parser = new StreamParser(aggregate);
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let smallParts = file.size <= SMALL_SUMMARY ? [] : null;
    progress(0, "读取本地 JSON");
    for (let offset = 0; offset < file.size; offset += CHUNK_BYTES) {
      const end = Math.min(file.size, offset + CHUNK_BYTES);
      let buffer;
      try { buffer = await file.slice(offset, end).arrayBuffer(); }
      catch (_) { fail("read"); }
      let text;
      try { text = decoder.decode(buffer, { stream: true }); }
      catch (_) { fail(); }
      if (smallParts) smallParts.push(text);
      parser.feed(text);
      // Raw exports are streamed record by record; once their schema is known,
      // do not retain a second complete text copy even below the summary cap.
      if (parser.schemaVersion === "1.0") smallParts = null;
      if (parser.schemaVersion === 1 && !smallParts) fail("summary");
      progress(Math.min(99, Math.floor(end / file.size * 100)), "校验并汇总本地记录");
    }
    let tail;
    try { tail = decoder.decode(); } catch (_) { fail(); }
    if (smallParts) smallParts.push(tail);
    parser.feed(tail, true);
    progress(99, "整理仪表盘摘要");
    let result;
    if (parser.schemaVersion === "1.0" && parser.hasRecords) result = aggregate.finish();
    else if (parser.schemaVersion === 1 && smallParts) {
      let original;
      try { original = JSON.parse(smallParts.join("")); } catch (_) { fail(); }
      result = normalizeSummary(original);
    } else fail();
    progress(100, "导入完成");
    return result;
  }

  let busy = false;
  self.onmessage = async function (event) {
    if (busy) { self.postMessage({ type: "error", message: ERRORS.busy }); return; }
    busy = true;
    try {
      const data = await importFile(event && event.data && event.data.file, (percent, phase) => {
        self.postMessage({ type: "progress", percent, phase });
      });
      self.postMessage({ type: "complete", data });
    } catch (error) {
      self.postMessage({
        type: "error",
        message: error instanceof ImportFailure ? error.message : ERRORS.format
      });
    } finally {
      busy = false;
    }
  };
}());
