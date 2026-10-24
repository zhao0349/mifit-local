/* Loopback-only controls. Portable copies remove this script and every local-only node. */
(() => {
  "use strict";
  const token = __LOCAL_TOKEN__;
  const $ = id => document.getElementById(id);
  const dashboard = window.MiFitDashboard;
  const names = {running:"正在执行", ok:"已完成", partial:"部分完成，请查看各项结果", error:"未完成", cancelled:"已取消"};
  const typeNames = {daily_activity:"日常活动", sleep:"睡眠", workouts:"运动记录", body_measurements:"体重", heart_rate:"心率", spo2:"血氧", stress:"压力", abnormal_heart_beat:"异常心搏", workout_detail:"运动明细"};
  const categories = ["daily_activity", "heart_rate", "sleep", "body_measurements", "spo2", "stress", "workouts"];
  let connected = false, busy = false, pending = false, job = null, timer = null;
  let polling = false, generation = 0, explicitlyClosed = false;
  let loadingCache = false, loadedCacheAccount = null, account = null;
  let inventory = null, inventoryPending = false;
  let inventoryNeeded = true, manuallyImported = false, cacheRequest = 0;
  let confirmDeadline = 0, confirmTimer = null;
  let repairFailureAccount = null;
  const accountCookie = "mifit_view_account";
  $("dashboard-account").replaceChildren(new Option("读取账号列表…", ""));

  function rememberedAccount() {
    const cookie = document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith(accountCookie + "="));
    try { return cookie ? decodeURIComponent(cookie.slice(accountCookie.length + 1)) : null; }
    catch { return null; }
  }
  function rememberAccount(key) {
    document.cookie = `${accountCookie}=${encodeURIComponent(key)}; Path=/; Max-Age=31536000; SameSite=Strict`;
  }
  function openBackend() {
    const panel = connected && !explicitlyClosed ? $("local-panel") : $("backend-help");
    panel.hidden = false;
    if (!panel.open) panel.showModal();
  }
  window.MiFitLocalControl = {open:openBackend};

  function notify(message) {
    $("local-feedback").textContent = message;
    $("local-feedback").hidden = false;
    dashboard.notify(message);
  }
  function setRecent(days) {
    const today = new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
    const first = new Date(today + "T00:00:00Z");
    first.setUTCDate(first.getUTCDate() - days + 1);
    $("local-start").value = first.toISOString().slice(0,10);
    $("local-end").value = today;
    ["local-start", "local-end"].forEach(id => $(id).dispatchEvent(new Event("change", {bubbles:true})));
  }
  setRecent(30);
  function setIndicator(state) {
    const labels = {connected:"后端已连接", disconnected:"后端未连接", closed:"后端已关闭", syncing:"后端正在同步"};
    $("backend-indicator").dataset.state = state;
    $("backend-toggle").dataset.state = state;
    $("backend-label").textContent = labels[state];
  }
  function selectedAccount() {
    return inventory?.accounts.find(item => item.account_key === $("db-account").value);
  }
  function controls() {
    const blocked = !connected || busy || pending || inventoryPending || dashboard.isImporting();
    ["local-load", "local-sync", "local-sync-export", "local-export", "local-snapshot", "local-login",
      "local-start", "local-end", "local-read-scope", "local-replace", "local-last-month", "local-last-year",
      "local-save-credentials", "local-user-id", "local-pass-token", "local-selftest", "local-open-exports"
    ].forEach(id => $(id).disabled = blocked);
    $("local-cancel").disabled = !connected || pending || job?.status !== "running" ||
      !(job.cancellable ?? ["sync", "sync_export", "login"].includes(job.kind));
    $("local-shutdown").disabled = !connected || pending;
    $("dashboard-account").disabled = blocked || !inventory?.accounts.some(item => !item.error);
    $("backend-start").disabled = pending;
    $("db-refresh").disabled = blocked || inventoryPending;
    $("db-account").disabled = blocked || inventoryPending || !inventory?.accounts.length;
    const hasAccount = !!selectedAccount() && !selectedAccount().error;
    $("db-repair").disabled = blocked || !selectedAccount() || selectedAccount().repairable === false;
    ["db-open-location", "db-damaged-delete"].forEach(id => $(id).disabled = blocked || !repairFailureAccount);
    $("db-filters").disabled = blocked || inventoryPending || !hasAccount;
    ["db-delete", "db-delete-account"].forEach(id => $(id).disabled = blocked || inventoryPending || !hasAccount);
    if ($("local-confirm").open) updateConfirmation();
  }
  async function request(path, body) {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), ["/api/cache", "/api/accounts", "/api/deletion-preview", "/api/damaged-deletion-preview", "/api/delete", "/api/repair"].includes(path) ? 120000 : 10000);
    try {
      const response = await fetch(path, {
        method:body === undefined ? "GET" : "POST", cache:"no-store", credentials:"omit",
        headers:{"X-MiFit-Token":token, ...(body === undefined ? {} : {"Content-Type":"application/json"})},
        body:body === undefined ? undefined : JSON.stringify(body), signal:abort.signal,
      });
      const value = await response.json();
      if (!response.ok) {
        const error = new Error(value.error || "本机请求未完成。");
        error.status = response.status;
        error.diagnostics = value.diagnostics;
        throw error;
      }
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
    inventoryNeeded = true;
    setIndicator(explicitlyClosed ? "closed" : "disconnected");
    $("local-status").textContent = explicitlyClosed
      ? "后端已关闭。当前网页仍可手动导入与查看；点击左下角入口可重新开启。"
      : "本地连接已断开。当前档案已保留；运行启动程序后重新连接，或刷新页面。";
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
      if (state.closed) { explicitlyClosed = true; disconnected(); return; }
      if (!connected || (job?.status === "running" && state.job?.status !== "running") ||
          (state.account && account && state.account.scope !== account.scope)) inventoryNeeded = true;
      connected = true; explicitlyClosed = false; busy = state.busy; job = state.job;
      if (state.account) account = state.account;
      setIndicator(job?.status === "running" && ["sync", "sync_export"].includes(job.kind) ? "syncing" : "connected");
      $("local-status").textContent = busy ? "本机任务正在运行…" : "已连接本地后端 · 可读取缓存或发起同步";
      if (state.account) $("local-account").textContent = state.account.configured
        ? "已配置账号 " + state.account.account_masked
        : state.account.keyring_available ? "尚未登录，请先完成小米官网登录。" : "Windows 凭据管理器不可用，请检查本机运行环境。";
      const lines = job ? [names[job.status] || "任务状态未知", ...job.messages] : ["尚未开始任务。"];
      if (job?.result?.types) for (const [name,value] of Object.entries(job.result.types)) {
        lines.push(`${typeNames[name] || name}: ${names[value.status] || value.status}，新增 ${value.added}，更新 ${value.updated}，失败分块 ${value.failed_chunks}`);
      }
      if (job?.result?.message) lines.push(job.result.message);
      const exportDirectory = job?.result?.directory || job?.result?.export?.directory;
      if (exportDirectory) lines.push("导出位置：" + exportDirectory);
      $("local-progress").textContent = lines.join("\n");
      controls();
      if (inventoryNeeded && !busy && !pending) await refreshInventory({automatic:true});
    } catch { if (revision === generation) disconnected(); }
    finally {
      polling = false;
      clearTimeout(timer);
      if (!explicitlyClosed) timer = setTimeout(poll, connected ? 1500 : 5000);
    }
  }
  async function act(action) {
    if (pending) return;
    pending = true; generation++; controls();
    try { return await action(); }
    catch (error) { notify(error.message || "操作未完成，当前档案已保留。"); }
    finally { pending = false; generation++; controls(); if (!explicitlyClosed) await poll(); }
  }
  function updateConfirmation() {
    const remaining = Math.max(0, Math.ceil((confirmDeadline - performance.now()) / 1000));
    const button = $("local-confirm-accept");
    button.disabled = remaining > 0 || !connected || busy || pending;
    button.textContent = button.dataset.label + (remaining ? `（${remaining} 秒）` : "");
  }
  function confirmAction({title, message, label="确认删除", warning="此操作无法撤销，请核对账号与范围。", seconds=0}) {
    const dialog = $("local-confirm");
    if (dialog.open) return Promise.resolve(false);
    $("local-confirm-title").textContent = title;
    $("local-confirm-message").textContent = message;
    $("local-confirm-warning").textContent = warning;
    $("local-confirm-error").hidden = true;
    $("local-confirm-accept").dataset.label = label;
    confirmDeadline = performance.now() + seconds * 1000;
    updateConfirmation();
    dialog.returnValue = "cancel";
    dialog.showModal();
    $("local-confirm-cancel").focus();
    confirmTimer = setInterval(updateConfirmation, 100);
    return new Promise(resolve => dialog.addEventListener("close", () => {
      clearInterval(confirmTimer);
      resolve(dialog.returnValue === "accept");
    }, {once:true}));
  }
  $("local-confirm-accept").addEventListener("click", () => {
    updateConfirmation();
    if (!$("local-confirm-accept").disabled) $("local-confirm").close("accept");
  });
  $("local-confirm-cancel").addEventListener("click", () => $("local-confirm").close("cancel"));
  $("local-close").addEventListener("click", () => $("local-panel").close());
  $("local-last-month").addEventListener("click", () => setRecent(30));
  $("local-last-year").addEventListener("click", () => setRecent(365));

  async function loadAccount(key, {scope="all", remember=false, quiet=false} = {}) {
    const revision = dashboard.getRevision();
    const serial = ++cacheRequest;
    const selected = inventory?.accounts.find(item => item.account_key === key);
    const range = dates();
    const identity = key ? {account_key:key} : {account_scope:account?.scope};
    const summary = await request("/api/cache", {...range, scope, ...identity});
    if (serial !== cacheRequest || dashboard.isImporting() || revision !== dashboard.getRevision()) {
      throw new Error("已保留手动导入的档案；需要时请再次读取缓存。");
    }
    loadingCache = true;
    try {
      dashboard.adoptSummary(summary);
      loadedCacheAccount = key || account?.scope;
      manuallyImported = false;
      $("dashboard-account").value = key || "";
      if (remember && key) rememberAccount(key);
    }
    finally { loadingCache = false; }
    $("local-source").textContent = "当前档案：" + (selected?.label || "账号 " + (account?.account_masked || "")) + " 的本地缓存（" + (scope === "all" ? "全部已缓存历史" : range.start + " 至 " + range.end) + "）。";
    if (!quiet) notify(summary.days.length ? "本地缓存已载入。" : scope === "all" ? "当前账号没有缓存记录，请先同步。" : "所选日期没有缓存记录；可修改日期或同步。");
  }
  $("local-load").addEventListener("click", () => act(() => {
    const key = $("dashboard-account").value ||
      (inventory?.accounts.some(item => item.account_key === loadedCacheAccount) ? loadedCacheAccount : null);
    return loadAccount(key, {scope:$("local-read-scope").value});
  }));
  $("dashboard-account").addEventListener("change", () => {
    const key = $("dashboard-account").value;
    if (!key) return;
    act(async () => {
      try { await loadAccount(key, {remember:true}); }
      finally { $("dashboard-account").value = manuallyImported ? "" : loadedCacheAccount || ""; }
    });
  });
  async function startSync(exportAfter) {
    try {
      const payload = dates();
      if (exportAfter) payload.export = true;
      if ($("local-replace").checked) {
        if (!account?.scope) throw new Error("请先连接并配置当前账号。");
        payload.mode = "replace";
        payload.account_scope = account.scope;
        const accepted = await confirmAction({title:"确认覆盖当前账号历史", label:"确认覆盖", message:`账号 ${account.account_masked}\n全部同步成功后，仅保留 ${payload.start} 至 ${payload.end} 的新记录。`, warning:"失败或取消保留旧历史；其他账号和已另存快照不受影响。"});
        if (!accepted) return;
        $("local-replace").checked = false;
      }
      await act(async () => { job = await request("/api/sync", payload); busy = true; });
    } catch (error) { notify(error.message); }
  }
  $("local-sync").addEventListener("click", () => startSync(false));
  $("local-sync-export").addEventListener("click", () => startSync(true));
  for (const [id, mode] of [["local-export", "cumulative"], ["local-snapshot", "snapshot"]]) {
    $(id).addEventListener("click", () => act(async () => {
      job = await request("/api/export", {...dates(), mode, account_scope:account?.scope}); busy = true;
    }));
  }
  $("local-login").addEventListener("click", () => act(async () => { job = await request("/api/login", {}); busy = true; }));
  $("local-credentials").addEventListener("submit", event => {
    event.preventDefault();
    const user_id = $("local-user-id").value, pass_token = $("local-pass-token").value;
    $("local-user-id").value = ""; $("local-pass-token").value = "";
    act(async () => { await request("/api/credentials", {user_id, pass_token}); notify("凭据已保存到 Windows 凭据管理器。"); });
  });
  $("local-selftest").addEventListener("click", () => act(async () => { job = await request("/api/selftest", {}); busy = true; }));
  $("local-open-exports").addEventListener("click", () => act(async () => { await request("/api/open-exports", {}); notify("已打开本机导出文件夹。"); }));
  $("local-cancel").addEventListener("click", () => act(async () => { if (job) await request("/api/cancel", {id:job.id}); }));
  $("local-reconnect").addEventListener("click", poll);
  $("backend-start").addEventListener("click", () => act(async () => {
    try { await request("/api/start", {}); }
    catch (error) {
      const message = error.status ? error.message : "本机启动器尚未运行，请先运行“启动小米数据导出.cmd”，再点击开启。";
      $("backend-start-message").textContent = message;
      throw new Error(message);
    }
    explicitlyClosed = false;
    inventoryNeeded = true;
    $("backend-start-message").textContent = "开启后可读取本机账号数据、同步和管理数据库。手动导入与离线展示可继续使用。";
    $("backend-help").close();
  }));
  $("local-shutdown").addEventListener("click", async () => {
    // Closing also cancels a running sync. Do not make this confirmation depend on busy=false.
    if (!window.confirm("关闭这台电脑上的后端服务？正在执行的任务将取消或完成收尾，当前网页仍可独立使用。")) return;
    await act(async () => {
      await request("/api/shutdown", {});
      explicitlyClosed = true; generation++; clearTimeout(timer); disconnected();
      $("local-panel").close();
    });
  });

  function bytes(value) {
    value = Number(value) || 0;
    if (value < 1024) return value.toLocaleString("zh-CN") + " B";
    if (value < 1024 ** 2) return (value / 1024).toFixed(1) + " KiB";
    if (value < 1024 ** 3) return (value / 1024 ** 2).toFixed(2) + " MiB";
    return (value / 1024 ** 3).toFixed(2) + " GiB";
  }
  function dateRange(start, end) { return start && end ? start + " 至 " + end : "无记录"; }
  function clearFilters() {
    $("db-start").value = ""; $("db-end").value = "";
    $("db-categories").querySelectorAll("input").forEach(input => input.checked = false);
    ["db-start", "db-end"].forEach(id => $(id).dispatchEvent(new Event("change", {bubbles:true})));
  }
  for (const key of categories) {
    const label = document.createElement("label"), input = document.createElement("input");
    input.type = "checkbox"; input.value = key; input.name = "db-category";
    label.append(input, document.createTextNode(typeNames[key])); $("db-categories").append(label);
  }
  function renderAccount() {
    const item = selectedAccount();
    $("db-account-details").hidden = !item;
    $("db-category-summary").replaceChildren();
    if (!item) { $("db-account-summary").textContent = "请选择要查看或管理的账号。"; controls(); return; }
    $("db-account-summary").textContent = item.error || `${item.total_rows.toLocaleString("zh-CN")} 条记录 · ${dateRange(item.start,item.end)} · 磁盘占用 ${bytes(item.database_bytes)}（数据库 ${bytes(item.db_bytes)}，日志与共享内存 ${bytes((item.wal_bytes || 0) + (item.shm_bytes || 0))}）`;
    for (const entry of item.categories || []) {
      const row = document.createElement("tr");
      for (const value of [entry.label, entry.count.toLocaleString("zh-CN"), dateRange(entry.start, entry.end)]) {
        const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
      }
      $("db-category-summary").append(row);
    }
    controls();
  }
  async function refreshInventory({automatic=false} = {}) {
    if (inventoryPending || !connected || busy) return;
    inventoryPending = true; controls();
    try {
      const value = await request("/api/accounts");
      const previous = $("db-account").value;
      inventory = value;
      inventoryNeeded = false;
      $("db-account").replaceChildren(new Option("请选择一个账号", ""));
      for (const item of value.accounts) {
        $("db-account").append(new Option(`${item.label}${item.current ? " · 当前账号" : ""} · ${bytes(item.database_bytes)} · ${dateRange(item.start,item.end)}`, item.account_key));
      }
      $("db-account").value = value.accounts.some(item => item.account_key === previous) ? previous : "";
      $("db-total").textContent = `${value.accounts.length} 个账号数据库 · 总占用 ${bytes(value.total_bytes)}（含 WAL / SHM）`;
      renderAccount();
      const readable = value.accounts.filter(item => !item.error);
      const view = $("dashboard-account");
      view.replaceChildren(new Option(manuallyImported ? "手动导入的档案" : "请选择账号", ""));
      for (const item of readable) view.append(new Option(item.label, item.account_key));
      view.value = manuallyImported ? "" : loadedCacheAccount || "";
      $("account-picker").hidden = false;
      if (automatic && readable.length && !manuallyImported && !dashboard.isImporting()) {
        const preferred = loadedCacheAccount || rememberedAccount();
        const initial = readable.find(item => item.account_key === preferred) || readable[0];
        try { await loadAccount(initial.account_key, {quiet:true}); }
        catch (error) { notify(error.message); view.value = manuallyImported ? "" : loadedCacheAccount || ""; }
      }
    } catch (error) {
      inventory = null; $("db-account").replaceChildren(new Option("读取未完成，请刷新", ""));
      $("db-total").textContent = "未能读取数据库列表。"; renderAccount(); notify(error.message);
    } finally { inventoryPending = false; controls(); }
  }
  $("local-database").addEventListener("toggle", () => { if ($("local-database").open) refreshInventory(); });
  $("db-refresh").addEventListener("click", refreshInventory);
  $("db-account").addEventListener("change", () => { clearFilters(); $("db-repair-feedback").hidden = true; renderAccount(); });
  $("db-clear-filters").addEventListener("click", clearFilters);
  function showRepairFailure(item, error) {
    repairFailureAccount = {account_key:item.account_key, label:item.label, backupComplete:error.diagnostics?.backup_complete === true};
    $("db-repair-failure-summary").textContent = `账号：${item.label} · ${error.message}`;
    const diagnostics = error.diagnostics || {message:error.message};
    $("db-repair-diagnostics").replaceChildren();
    for (const key of ["stage", "exception_type", "message", "sqlite_errorcode", "sqlite_errorname", "database_path", "backup_directory", "backup_complete"]) {
      const row = document.createElement("p"), label = document.createElement("strong"), value = document.createElement("span");
      row.style.overflowWrap = "anywhere";
      label.textContent = key;
      value.textContent = diagnostics[key] == null ? "—" : String(diagnostics[key]);
      value.style.whiteSpace = "pre-wrap";
      row.append(label, document.createElement("br"), value);
      $("db-repair-diagnostics").append(row);
    }
    $("db-repair-failure-feedback").hidden = true;
    if (!$("db-repair-failure").open) $("db-repair-failure").showModal();
    controls();
  }
  function failureFeedback(message) {
    $("db-repair-failure-feedback").textContent = message;
    $("db-repair-failure-feedback").hidden = false;
  }
  function failureAction(action) {
    if (!connected || busy || pending || !repairFailureAccount) return;
    return act(async () => {
      try { return await action(); }
      catch (error) { failureFeedback(error.message); throw error; }
    });
  }
  $("db-repair-failure-close").addEventListener("click", () => $("db-repair-failure").close());
  $("db-open-location").addEventListener("click", () => failureAction(async () => {
    await request("/api/open-database", {account_key:repairFailureAccount.account_key});
    failureFeedback("已打开数据库所在文件夹。");
  }));
  $("db-damaged-delete").addEventListener("click", async () => {
    const item = repairFailureAccount;
    if (!item) return;
    const preview = await failureAction(() => request("/api/damaged-deletion-preview", {account_key:item.account_key}));
    if (!preview) return;
    $("db-repair-failure").close();
    const accepted = await confirmAction({
      title:"确认完全删除损坏的数据库", label:"完全删除数据库",
      message:`账号：${preview.account_label}${preview.database_path ? "\n数据库：" + preview.database_path : ""}\n无法完整读取此数据库，因此不能准确统计记录数。\n将删除该账号的数据库文件及其事务日志。`,
      seconds:Math.max(5, preview.wait_seconds || 0),
      warning:(item.backupComplete ? "此操作无法撤销。" : "尚未建立完整的修复备份，删除后本机数据将无法恢复。") + "若已创建修复备份将予以保留；已导出文件、离线网页和小米云端数据保留。",
    });
    if (!accepted) { $("db-repair-failure").showModal(); controls(); return; }
    await act(async () => {
      try { await request("/api/delete", {token:preview.token}); }
      catch (error) { $("db-repair-failure").showModal(); failureFeedback(error.message); throw error; }
      repairFailureAccount = null;
      const message = "所选账号的损坏数据库已删除；已创建的修复备份、导出文件与当前网页副本保留。";
      $("db-repair-feedback").textContent = message;
      $("db-repair-feedback").hidden = false;
      if (loadedCacheAccount === item.account_key) $("local-source").textContent = "当前展示的是删除前载入的缓存副本。";
      notify(message);
      busy = false;
      await refreshInventory();
    });
  });
  $("db-repair").addEventListener("click", () => {
    const item = selectedAccount();
    if (!item) return;
    act(async () => {
      const feedback = $("db-repair-feedback");
      feedback.hidden = false;
      feedback.textContent = "正在检查并修复所选账号数据库，请稍候…";
      try {
        const result = await request("/api/repair", {account_key:item.account_key});
        feedback.textContent = result.message + (result.backup_directory ? " 备份位置：" + result.backup_directory : "");
        // A status poll may have seen the repair lock; the response means it is released.
        busy = false;
        await refreshInventory();
      } catch (error) {
        feedback.textContent = error.message || "修复未完成，请稍后重试。";
        showRepairFailure(item, error);
        throw error;
      }
    });
  });
  async function deleteSelection(entireAccount) {
    const item = selectedAccount();
    if (!item) { notify("请先选择一个账号。"); return; }
    let start = entireAccount ? null : $("db-start").value || null;
    let end = entireAccount ? null : $("db-end").value || null;
    if (!entireAccount && ((!$("db-start").checkValidity() || !$("db-end").checkValidity()) || start && end && start > end)) {
      notify("请选择有效日期，开始日期不能晚于结束日期。"); return;
    }
    const chosen = entireAccount ? [] : [...$("db-categories").querySelectorAll("input:checked")].map(input => input.value);
    const preview = await act(() => request("/api/deletion-preview", {account_key:item.account_key, start, end, categories:chosen}));
    if (!preview) return;
    const range = preview.start || preview.end ? `${preview.start || "最早"} 至 ${preview.end || "最新"}` : "全部时间";
    const accepted = await confirmAction({
      title:preview.all_data ? "确认删除此账号的全部数据" : "确认删除筛选范围内的数据",
      message:`账号：${preview.account_label}\n时间：${range}\n类型：${preview.categories.map(entry => entry.label).join("、")}\n将删除 ${preview.total_rows.toLocaleString("zh-CN")} 条记录${preview.all_data ? "，并移除此账号数据库" : ""}。`,
      seconds:preview.all_data ? Math.max(3, preview.wait_seconds || 0) : preview.wait_seconds || 0,
      warning:"此操作无法撤销。已导出文件与离线网页中的副本不会随之删除，小米云端不受影响。",
    });
    if (!accepted) return;
    await act(async () => {
      await request("/api/delete", {token:preview.token});
      if (loadedCacheAccount === preview.account_key) {
        $("local-source").textContent = "当前展示的是删除前载入的缓存副本；请重新读取缓存以更新图表。";
      }
      notify("所选账号的数据已删除。已导出的文件与当前网页中的副本保留。请刷新列表查看磁盘占用。");
    });
    await refreshInventory();
  }
  $("db-delete").addEventListener("click", () => deleteSelection(false));
  $("db-delete-account").addEventListener("click", () => deleteSelection(true));
  document.addEventListener("mifit:import-state", () => {
    if (dashboard.isImporting()) manuallyImported = true;
    controls();
  });
  document.addEventListener("mifit:data-updated", () => {
    if (!loadingCache) {
      manuallyImported = true;
      loadedCacheAccount = null;
      $("dashboard-account").value = "";
      $("dashboard-account").options[0].textContent = "手动导入的档案";
      $("local-source").textContent = "当前档案：手动导入；尚未用本地缓存替换。";
    }
  });
  controls(); poll();
})();
