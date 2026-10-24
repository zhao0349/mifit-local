/* Included only by the loopback server; removed from portable HTML downloads. */
(() => {
  "use strict";
  const token = __LOCAL_TOKEN__;
  const $ = id => document.getElementById(id);
  const dashboard = window.MiFitDashboard;
  const names = {running:"正在执行", ok:"已完成", partial:"部分完成，请查看各项结果", error:"未完成", cancelled:"已取消"};
  const typeNames = {daily_activity:"日常活动", sleep:"睡眠", workouts:"运动记录", body_measurements:"身体测量", heart_rate:"心率", spo2:"血氧", stress:"压力", abnormal_heart_beat:"异常心跳", workout_detail:"运动明细"};
  let connected = false, busy = false, pending = false, job = null, timer = null;
  let polling = false, generation = 0;
  let loadingCache = false;
  let account = null;
  $("backend-help").hidden = true;
  const today = new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
  const first = new Date(today + "T00:00:00Z");
  first.setUTCDate(first.getUTCDate() - 29);
  $("local-start").value = first.toISOString().slice(0,10);
  $("local-end").value = today;

  function controls() {
    const blocked = !connected || busy || pending || dashboard.isImporting();
    ["local-load", "local-sync", "local-login", "local-start", "local-end", "local-read-scope", "local-replace"].forEach(id => $(id).disabled = blocked);
    $("local-cancel").disabled = !connected || pending || job?.status !== "running";
  }
  async function request(path, body) {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), path === "/api/cache" ? 120000 : 10000);
    try {
      const response = await fetch(path, {
        method: body === undefined ? "GET" : "POST", cache:"no-store", credentials:"omit",
        headers:{"X-MiFit-Token":token, ...(body === undefined ? {} : {"Content-Type":"application/json"})},
        body:body === undefined ? undefined : JSON.stringify(body), signal:abort.signal,
      });
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || "本机请求未完成。");
      return value;
    } finally { clearTimeout(timeout); }
  }
  function dates() {
    const start = $("local-start").value, end = $("local-end").value;
    if (!start || !end || start > end || !$("local-start").checkValidity() || !$("local-end").checkValidity()) {
      throw new Error("请选择有效日期，开始日期不能晚于结束日期。");
    }
    return {start,end};
  }
  function disconnected() {
    connected = false;
    $("local-status").textContent = "本地连接已断开。当前档案已保留；启动桌面工具后重新连接，或刷新页面。";
    controls();
  }
  async function poll() {
    if (polling) return;
    polling = true;
    const revision = generation;
    try {
      const state = await request("/api/status");
      if (revision !== generation) return;
      if (state.service !== "mifit-local" || state.version !== 1) throw new Error("服务不匹配");
      connected = true; busy = state.busy; job = state.job;
      if (state.account) account = state.account;
      $("local-status").textContent = busy ? "本机任务正在运行…" : "已连接本地后端 · 可读取缓存或发起同步";
      if (state.account) $("local-account").textContent = state.account.configured
        ? "已配置账号 " + state.account.account_masked
        : state.account.keyring_available ? "尚未登录，请先完成小米官网登录。" : "Windows 凭据管理器不可用，请检查桌面工具。";
      const lines = job ? [names[job.status] || "任务状态未知", ...job.messages] : [];
      if (job?.result?.types) for (const [name,value] of Object.entries(job.result.types)) {
        lines.push(`${typeNames[name] || name}: ${names[value.status] || value.status}，新增 ${value.added}，更新 ${value.updated}，失败分块 ${value.failed_chunks}`);
      }
      $("local-progress").textContent = lines.join("\n");
      controls();
    } catch { if (revision === generation) disconnected(); }
    finally {
      polling = false;
      clearTimeout(timer);
      // Even a lost start response can be recovered by polling the one active job.
      timer = setTimeout(poll, connected ? 1500 : 5000);
    }
  }
  async function act(action) {
    if (pending) return;
    pending = true; generation++; controls();
    try { await action(); }
    catch (error) { dashboard.notify(error.message || "操作未完成，当前档案已保留。"); }
    finally { pending = false; generation++; controls(); await poll(); }
  }
  $("local-load").addEventListener("click", () => act(async () => {
    const revision = dashboard.getRevision();
    const scope = $("local-read-scope").value;
    const loadedAccount = account;
    const summary = await request("/api/cache", {...dates(), scope, account_scope:loadedAccount?.scope});
    // A manual import started/completed while the response was in flight wins.
    if (dashboard.isImporting() || revision !== dashboard.getRevision()) {
      throw new Error("已保留手动导入的档案；需要时请再次读取缓存。");
    }
    loadingCache = true;
    try { dashboard.adoptSummary(summary); }
    finally { loadingCache = false; }
    $("local-source").textContent = "当前档案：账号 " + (loadedAccount?.account_masked || "") + " 的本地缓存（" + (scope === "all" ? "全部已缓存历史" : $("local-start").value + " 至 " + $("local-end").value) + "）。";
    dashboard.notify(summary.days.length ? "本地缓存已载入。" : scope === "all" ? "当前账号没有缓存记录，请先同步。" : "所选日期没有缓存记录；可修改日期或同步。");
  }));
  $("local-sync").addEventListener("click", () => act(async () => {
    const payload = dates();
    if ($("local-replace").checked) {
      if (!account?.scope) throw new Error("请先连接并配置当前账号。");
      if (!confirm(`确认覆盖账号 ${account.account_masked} 的历史？\n全部同步成功后，仅保留 ${payload.start} 至 ${payload.end} 的新记录。失败或取消保留旧历史；其他账号不变。`)) return;
      payload.mode = "replace";
      payload.account_scope = account.scope;
      $("local-replace").checked = false;
    }
    job = await request("/api/sync", payload); busy = true;
  }));
  $("local-login").addEventListener("click", () => act(async () => {
    job = await request("/api/login", {}); busy = true;
  }));
  $("local-cancel").addEventListener("click", () => act(async () => {
    if (job) await request("/api/cancel", {id:job.id});
  }));
  $("local-reconnect").addEventListener("click", poll);
  document.addEventListener("mifit:import-state", controls);
  document.addEventListener("mifit:data-updated", () => {
    if (!loadingCache) $("local-source").textContent = "当前档案：手动导入；尚未用本地缓存替换。";
  });
  poll();
})();
