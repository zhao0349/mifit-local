/* Shared offline intraday whitelist. No storage, network, or identifiers. */
(function (root) {
  "use strict";
  const DAY = 86400000, OFFSET = 8 * 60 * 60000;
  const STAGES = new Set(["deep", "light", "rem", "awake", "unknown"]);
  const limits = Object.freeze({
    maxDays: 40000, maxHeartPoints: 2000000, maxHeartPerDay: 100000,
    maxSleepSessions: 100000, maxSleepPerDay: 1000,
    maxStageItems: 1000000, maxStagesPerSession: 10000,
    maxSleepSpanMs: 7 * DAY
  });
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const nonnegative = value => finite(value) && value >= 0 ? value : null;
  function validDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value < "0001-01-01") return false;
    const stamp = Date.parse(value + "T00:00:00Z");
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === value;
  }
  function localDate(stamp) {
    if (!Number.isSafeInteger(stamp)) return null;
    const date = new Date(stamp + OFFSET);
    if (!Number.isFinite(date.getTime())) return null;
    const value = date.toISOString().slice(0, 10);
    return validDate(value) ? value : null;
  }
  function limit(condition) {
    if (condition) throw new RangeError("日内记录规模过大，请缩小导出范围后重试。");
  }
  function session(raw) {
    if (!object(raw) || !Number.isSafeInteger(raw.start) || !Number.isSafeInteger(raw.end) ||
        raw.end <= raw.start || raw.end - raw.start > limits.maxSleepSpanMs ||
        !localDate(raw.start) || !localDate(raw.end) || raw.timing !== "sequence" ||
        !Array.isArray(raw.stages)) return null;
    limit(raw.stages.length > limits.maxStagesPerSession);
    const stages = [];
    let sequenceValid = raw.sequenceValid !== false;
    for (const item of raw.stages) {
      if (!Array.isArray(item) || item.length < 2 || typeof item[0] !== "string" ||
          !finite(item[1]) || item[1] < 0) { sequenceValid = false; continue; }
      // Keep the duration of an unrecognised stage without importing arbitrary
      // source labels or silently moving subsequent stages earlier.
      stages.push([STAGES.has(item[0]) ? item[0] : "unknown", item[1]]);
    }
    const asleep = nonnegative(raw.asleep), score = nonnegative(raw.score);
    const result = {
      start: raw.start, end: raw.end, nap: raw.nap === true,
      asleep, score: score !== null && score <= 100 ? score : null,
      stages, timing: "sequence"
    };
    if (!sequenceValid) result.sequenceValid = false;
    return result;
  }
  function normalize(value) {
    const output = { version: 1, heart: {}, sleep: {} };
    if (value === undefined || value === null) return output;
    if (!object(value) || value.version !== 1 ||
        (value.heart !== undefined && !object(value.heart)) ||
        (value.sleep !== undefined && !object(value.sleep))) {
      throw new TypeError("日内数据格式无效。");
    }
    let pointCount = 0, sleepCount = 0, stageCount = 0;
    const heartDays = Object.entries(value.heart || {}), sleepDays = Object.entries(value.sleep || {});
    limit(heartDays.length > limits.maxDays || sleepDays.length > limits.maxDays);
    for (const [day, rows] of heartDays) {
      if (!validDate(day) || !Array.isArray(rows)) continue;
      pointCount += rows.length;
      limit(rows.length > limits.maxHeartPerDay || pointCount > limits.maxHeartPoints);
      const points = [];
      for (const row of rows) {
        if (!Array.isArray(row) || row.length < 2 || !Number.isSafeInteger(row[0]) ||
            row[0] < 0 || row[0] >= DAY || !finite(row[1]) || row[1] <= 0) continue;
        points.push([row[0], row[1], row[2] === 1 ? 1 : 0]);
      }
      // Stable sort preserves multiple simultaneous measurements. Do not
      // average, resample or deduplicate heart points.
      points.sort((a, b) => a[0] - b[0]);
      if (points.length) output.heart[day] = points;
    }
    for (const [day, rows] of sleepDays) {
      if (!validDate(day) || !Array.isArray(rows)) continue;
      sleepCount += rows.length;
      limit(rows.length > limits.maxSleepPerDay || sleepCount > limits.maxSleepSessions);
      const sessions = [], seen = new Set();
      for (const raw of rows) {
        if (Array.isArray(raw?.stages)) {
          stageCount += raw.stages.length;
          limit(stageCount > limits.maxStageItems);
        }
        const cleaned = session(raw);
        if (!cleaned || localDate(cleaned.end) !== day) continue;
        const key = JSON.stringify(cleaned);
        if (seen.has(key)) continue;
        seen.add(key);
        sessions.push(cleaned);
      }
      sessions.sort((a, b) => (b.end - b.start) - (a.end - a.start));
      if (sessions.length) output.sleep[day] = sessions;
    }
    return output;
  }
  function sleepTimeline(raw) {
    let cleaned;
    try { cleaned = session(raw); }
    catch (_) { cleaned = null; }
    if (!cleaned) return {
      valid: false, segments: [], unplacedMinutes: null, reason: "睡眠会话时间或阶段数据无效。"
    };
    const span = (cleaned.end - cleaned.start) / 60000;
    if (cleaned.sequenceValid === false) return {
      valid: false, segments: [], unplacedMinutes: null,
      reason: "部分来源阶段的时长无效，不能定位后续阶段；仅展示可读阶段的原顺序。"
    };
    if (!cleaned.stages.length) return {
      valid: false, segments: [], unplacedMinutes: span, reason: "来源没有提供可用的阶段顺序。"
    };
    const total = cleaned.stages.reduce((sum, item) => sum + item[1], 0);
    if (!Number.isFinite(total) || total > span) return {
      valid: false, segments: [], unplacedMinutes: null,
      reason: "阶段时长合计超过会话跨度，不能按时间定位；请查看原顺序及各段时长。"
    };
    let cursor = cleaned.start;
    const segments = cleaned.stages.map(([stage, minutes]) => {
      const start = cursor;
      cursor += minutes * 60000;
      return { stage, start, end: cursor };
    });
    return {
      valid: true, segments, unplacedMinutes: span - total,
      reason: "仅按来源顺序和时长近似排列；阶段的真实起止时间及未定位时长的位置未知。"
    };
  }
  root.MiFitIntradayModel = Object.freeze({ normalize, sleepTimeline, limits });
})(typeof globalThis !== "undefined" ? globalThis : window);
