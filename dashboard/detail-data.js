/* Pure, offline models for detail views. Inputs contain daily summaries, not raw samples. */
(function (root) {
  "use strict";

  const DAY_MS = 86400000;
  const definitions = {
    steps: ["步数", "步", 0, "sum", "activity"],
    distance_km: ["活动距离", "km", 2, "sum", "activity"],
    active_kcal: ["活动消耗", "kcal", 0, "sum", "activity"],
    active_minutes: ["活动分钟", "分钟", 0, "sum", "activity"],
    sleep_minutes: ["主睡眠时长", "分钟", 0, "mean", "sleep"],
    sleep_score: ["睡眠评分", "分", 1, "mean", "sleep"],
    nap_minutes: ["小睡时长", "分钟", 0, "mean", "sleep"],
    resting_hr: ["静息心率", "次/分", 1, "mean", "heart"],
    heart_rate_avg: ["平均心率", "次/分", 1, "mean", "heart"],
    heart_rate_min: ["最低心率", "次/分", 0, "mean", "heart"],
    heart_rate_max: ["最高心率", "次/分", 0, "mean", "heart"],
    weight_kg: ["体重", "kg", 1, "mean", "body"],
    body_fat_pct: ["体脂率", "%", 1, "mean", "body"],
    bmi: ["BMI", "", 2, "mean", "body"],
    muscle_mass_kg: ["肌肉量", "kg", 2, "mean", "body"],
    water_pct: ["体水分率", "%", 2, "mean", "body"],
    bone_mass_kg: ["骨量", "kg", 2, "mean", "body"],
    visceral_fat_score: ["内脏脂肪等级", "", 0, "mean", "body"],
    basal_metabolism_kcal: ["基础代谢", "kcal", 0, "mean", "body"],
    metabolic_age: ["代谢年龄", "岁", 0, "mean", "body"],
    spo2_avg: ["平均血氧", "%", 1, "mean", "body"],
    stress_avg: ["平均压力", "分", 1, "mean", "body"],
    workout_count: ["运动记录数", "次", 0, "sum", "workouts"],
    workout_minutes: ["运动记录时长", "分钟", 0, "sum", "workouts"],
    workout_distance_km: ["运动记录距离", "km", 2, "sum", "workouts"],
    workout_kcal: ["运动记录消耗", "kcal", 0, "sum", "workouts"],
  };
  const metrics = Object.freeze(Object.fromEntries(
    Object.entries(definitions).map(([key, [label, unit, decimals, aggregation, category]]) =>
      [key, Object.freeze({ label, unit, decimals, aggregation, category })])
  ));
  const categories = Object.freeze([
    ["activity", "活动"], ["sleep", "睡眠"], ["heart", "心率"],
    ["workouts", "运动"], ["body", "身体"],
  ].map(([id, label]) => Object.freeze({
    id, label,
    metrics: Object.freeze(Object.keys(metrics).filter(key => metrics[key].category === id)),
  })));
  const stageKeys = ["deep", "light", "rem", "awake"];
  const weekdayLabels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const positiveKeys = new Set([
    "resting_hr", "heart_rate_avg", "heart_rate_min", "heart_rate_max", "weight_kg", "bmi", "spo2_avg",
  ]);
  const percentageKeys = new Set(["sleep_score", "body_fat_pct", "water_pct", "spo2_avg", "stress_avg"]);
  const numeric = value => typeof value === "number" && Number.isFinite(value);
  function metricValue(key, value) {
    if (!numeric(value) || value < 0 || (positiveKeys.has(key) && value <= 0)) return null;
    if (percentageKeys.has(key) && value > 100) return null;
    return value;
  }
  function validDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const timestamp = Date.parse(value + "T00:00:00Z");
    return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value;
  }
  const timestamp = value => Date.parse(value + "T00:00:00Z");
  const dateAt = value => new Date(value).toISOString().slice(0, 10);
  const shift = (value, days) => dateAt(timestamp(value) + days * DAY_MS);
  const weekdayIndex = value => (new Date(timestamp(value)).getUTCDay() + 6) % 7;
  const shortDate = value => value.slice(5).replace("-", "/");
  function mean(values) {
    if (!values.length) return null;
    let result = 0;
    values.forEach((value, index) => { result += (value - result) / (index + 1); });
    return numeric(result) ? result : null;
  }
  function total(values) {
    if (!values.length) return null;
    const result = values.reduce((sum, value) => sum + value, 0);
    return numeric(result) ? result : null;
  }
  const validValues = (rows, key) => rows.map(row => row[key]).filter(numeric);
  function emptyDay(date) {
    const result = { date };
    Object.keys(metrics).forEach(key => { result[key] = null; });
    result.sleep_stages = Object.fromEntries(stageKeys.map(key => [key, null]));
    return result;
  }
  function normalizeDays(rows) {
    const result = new Map();
    for (const raw of Array.isArray(rows) ? rows : []) {
      if (!raw || !validDate(raw.date)) continue;
      const day = emptyDay(raw.date);
      Object.keys(metrics).forEach(key => { day[key] = metricValue(key, raw[key]); });
      stageKeys.forEach(key => { day.sleep_stages[key] = metricValue(key, raw.sleep_stages?.[key]); });
      // Summary dates are unique. For malformed duplicates, match the main view's
      // last-row behavior without counting the same calendar date twice.
      result.set(day.date, day);
    }
    return result;
  }
  function normalizeWorkouts(rows) {
    const result = [];
    for (const raw of Array.isArray(rows) ? rows : []) {
      if (!raw || !validDate(raw.date)) continue;
      const start = typeof raw.start === "string" &&
        /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(raw.start) ? raw.start : "";
      result.push({
        date: raw.date, start,
        type: typeof raw.type === "string" && raw.type.trim() ? raw.type.slice(0, 120) : "other",
        duration_minutes: metricValue("duration_minutes", raw.duration_minutes),
        distance_km: metricValue("distance_km", raw.distance_km),
        calories_kcal: metricValue("calories_kcal", raw.calories_kcal),
        avg_heart_rate_bpm: numeric(raw.avg_heart_rate_bpm) && raw.avg_heart_rate_bpm > 0
          ? raw.avg_heart_rate_bpm : null,
        avg_pace_sec_per_km: numeric(raw.avg_pace_sec_per_km) && raw.avg_pace_sec_per_km > 0
          ? raw.avg_pace_sec_per_km : null,
      });
    }
    return result;
  }
  function newWorkoutGroup() {
    return {
      count: 0,
      minutes: { count: 0, value: 0 },
      distance_km: { count: 0, value: 0 },
      kcal: { count: 0, value: 0 },
    };
  }
  function addWorkout(group, workout) {
    group.count++;
    for (const [name, source] of [
      ["minutes", "duration_minutes"], ["distance_km", "distance_km"], ["kcal", "calories_kcal"],
    ]) {
      if (numeric(workout[source])) {
        group[name].count++;
        group[name].value += workout[source];
      }
    }
  }
  const groupTotal = (group, key) =>
    group[key].count && numeric(group[key].value) ? group[key].value : null;
  function groupedSeries(days, metric, granularity) {
    const buckets = new Map();
    for (const day of days) {
      let date = day.date, end = day.date, label = shortDate(day.date);
      if (granularity === "week") {
        date = shift(day.date, -weekdayIndex(day.date));
        end = shift(date, 6);
        label = shortDate(date) + "–" + shortDate(end);
      } else if (granularity === "month") {
        date = day.date.slice(0, 7) + "-01";
        const next = new Date(timestamp(date));
        next.setUTCMonth(next.getUTCMonth() + 1);
        end = dateAt(next.getTime() - DAY_MS);
        label = day.date.slice(0, 7).replace("-", ".");
      }
      if (!buckets.has(date)) buckets.set(date, { date, end, label, values: [] });
      if (numeric(day[metric])) buckets.get(date).values.push(day[metric]);
    }
    return [...buckets.values()].map(({ date, end, label, values }) => ({
      date, end, label,
      // Bounds label the calendar bucket; only dates inside the selected window
      // contribute. Empty days/buckets remain null so charts can draw a gap.
      value: metrics[metric].aggregation === "sum" ? total(values) : mean(values),
      count: values.length,
    }));
  }
  function analyze(data, options = {}) {
    data = data && typeof data === "object" ? data : {};
    options = options && typeof options === "object" ? options : {};
    const category = categories.find(item => item.id === options.category) || categories[0];
    const metric = category.metrics.includes(options.metric) ? options.metric : category.metrics[0];
    const granularity = ["day", "week", "month"].includes(options.granularity)
      ? options.granularity : "day";
    const workoutType = typeof options.workoutType === "string" ? options.workoutType : "all";
    const dayMap = normalizeDays(data.days);
    const allWorkouts = normalizeWorkouts(data.workouts);
    const archiveDates = new Set([...dayMap.keys(), ...allWorkouts.map(workout => workout.date)]);
    const availableDates = [...archiveDates].sort();
    const start = options.start ?? availableDates[0] ?? null;
    const end = options.end ?? availableDates.at(-1) ?? null;
    if ((start !== null || end !== null) && (!validDate(start) || !validDate(end) || start > end)) {
      throw new Error("请选择有效的日期范围，开始日期不能晚于结束日期。");
    }
    const calendarDays = start === null ? 0 : Math.round((timestamp(end) - timestamp(start)) / DAY_MS) + 1;
    // The main view is limited to ten years; this additional bound rejects
    // malformed century-spanning input instead of allocating millions of rows.
    if (calendarDays > 36600) throw new Error("日期范围过长，请缩小范围后查看。");
    const eligibleWorkouts = allWorkouts.filter(workout => workoutType === "all" || workout.type === workoutType);
    const dailyWorkouts = new Map();
    for (const workout of eligibleWorkouts) {
      if (!dailyWorkouts.has(workout.date)) dailyWorkouts.set(workout.date, newWorkoutGroup());
      addWorkout(dailyWorkouts.get(workout.date), workout);
    }
    function makeDay(date) {
      const source = dayMap.get(date);
      const day = source ? { ...source, sleep_stages: { ...source.sleep_stages } } : emptyDay(date);
      const workouts = dailyWorkouts.get(date);
      // Zero means the archive has this day but no matching workout record.
      // A completely absent archive day stays null, and absent duration/distance/
      // energy never turns into zero, even when a workout record exists.
      day.workout_count = archiveDates.has(date) ? (workouts?.count ?? 0) : null;
      day.workout_minutes = workouts ? groupTotal(workouts, "minutes") : null;
      day.workout_distance_km = workouts ? groupTotal(workouts, "distance_km") : null;
      day.workout_kcal = workouts ? groupTotal(workouts, "kcal") : null;
      return day;
    }
    const days = Array.from({ length: calendarDays }, (_, index) => makeDay(shift(start, index)));
    const workouts = eligibleWorkouts.filter(workout => start !== null && workout.date >= start && workout.date <= end)
      .sort((a, b) => b.date.localeCompare(a.date) || b.start.localeCompare(a.start) || a.type.localeCompare(b.type));
    const values = validValues(days, metric);
    const currentMean = mean(values);
    const previousValues = [];
    for (let index = calendarDays; index > 0; index--) {
      const value = makeDay(shift(start, -index))[metric];
      if (numeric(value)) previousValues.push(value);
    }
    const previousMean = mean(previousValues);
    const delta = numeric(currentMean) && numeric(previousMean) ? currentMean - previousMean : null;
    // These statistics always describe the original daily values, independently
    // of chart granularity. delta compares equal-length adjacent date windows'
    // valid-day means (absolute units, not a percentage). coverage is 0..1.
    const stats = {
      mean: currentMean,
      total: metrics[metric].aggregation === "sum" ? total(values) : null,
      min: values.length ? values.reduce((result, value) => Math.min(result, value)) : null,
      max: values.length ? values.reduce((result, value) => Math.max(result, value)) : null,
      first: values[0] ?? null,
      last: values.at(-1) ?? null,
      delta: numeric(delta) ? delta : null,
      previousMean,
      coverage: calendarDays ? values.length / calendarDays : 0,
      calendarDays,
    };
    const completeSleep = days.filter(day => stageKeys.every(key => numeric(day.sleep_stages[key])));
    const sleepStages = { count: completeSleep.length };
    stageKeys.forEach(key => { sleepStages[key] = mean(completeSleep.map(day => day.sleep_stages[key])); });
    const weekdayValues = weekdayLabels.map(() => []);
    days.forEach(day => {
      if (numeric(day[metric])) weekdayValues[weekdayIndex(day.date)].push(day[metric]);
    });
    const weekday = weekdayValues.map((items, index) => ({
      label: weekdayLabels[index], count: items.length, mean: mean(items),
    }));
    const typeGroups = new Map();
    for (const workout of workouts) {
      if (!typeGroups.has(workout.type)) typeGroups.set(workout.type, newWorkoutGroup());
      addWorkout(typeGroups.get(workout.type), workout);
    }
    const workoutTypes = [...typeGroups].map(([type, group]) => ({
      type, count: group.count,
      minutes: groupTotal(group, "minutes"),
      distance_km: groupTotal(group, "distance_km"),
      kcal: groupTotal(group, "kcal"),
    })).sort((a, b) => b.count - a.count || a.type.localeCompare(b.type));
    return {
      days, workouts, series: groupedSeries(days, metric, granularity),
      stats, sleepStages, weekday, workoutTypes,
    };
  }
  root.MiFitDetailModel = Object.freeze({ categories, metrics, analyze });
})(typeof window !== "undefined" ? window : globalThis);
