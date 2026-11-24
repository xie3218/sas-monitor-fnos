"use strict";
const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("zh-CN");
const missing = "—";
let disks = [];
let selection = null;
let selected = null;
let collectedAt = null;
let busy = false;
let manualPending = false;

const labels = {passed: "健康", attention: "需关注", failed: "SMART 故障", unknown: "未知"};
const reasonLabels = {
  smart_failed: "SMART 总体状态失败",
  smart_unknown: "SMART 总体状态未知",
  grown_defects: "G-list 有增长缺陷",
  uncorrected: "存在不可纠正错误",
  self_test: "自检日志有失败记录",
  non_medium_increased: "过去 24 小时观测到非介质错误增加",
};
const messages = {
  waiting: "等待首次采集。请在 NAS 上安装采集服务，安装后稍等片刻。",
  unavailable: "无法读取采集结果。请检查采集服务和应用用户权限。",
  missing_tool: "找不到 smartctl。请在 NAS 上安装 smartmontools。",
  scan_failed: "硬盘扫描失败。请检查 smartctl 和 HBA 或 RAID 控制器。",
  scan_timeout: "硬盘扫描超时。请检查控制器和硬盘连接。",
};

function setupTheme() {
  const selector = $("theme");
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const storageKey = "sas-monitor:theme";
  let preference = "system";
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved === "light" || saved === "dark") preference = saved;
  } catch {
    // Storage may be disabled in an embedded window.
  }

  function apply() {
    document.documentElement.dataset.theme = preference === "system"
      ? (system.matches ? "dark" : "light")
      : preference;
    selector.value = preference;
  }

  selector.addEventListener("change", () => {
    preference = selector.value;
    apply();
    try {
      window.localStorage.setItem(storageKey, preference);
    } catch {
      // The choice still applies until this window closes.
    }
  });
  system.addEventListener("change", apply);
  apply();
}

function node(tag, value, className) {
  const element = document.createElement(tag);
  element.textContent = value;
  if (className) element.className = className;
  return element;
}

function count(value, suffix = "") {
  return value == null ? missing : number.format(value) + suffix;
}

function capacity(value) {
  if (value == null) return missing;
  return value >= 1e12 ? (value / 1e12).toFixed(2) + " TB" : (value / 1e9).toFixed(1) + " GB";
}

function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function deviceKey(disk) {
  return disk.device + "|" + disk.controllerType;
}

function hardwareKey(disk) {
  if (disk.wwn) return "wwn:" + disk.wwn;
  return disk.serial ? "serial:" + disk.model + "|" + disk.serial : null;
}

function preserveSelection(previous, next, disk) {
  if (!disk) return null;
  const hardware = hardwareKey(disk);
  if (hardware && previous.filter((entry) => hardwareKey(entry) === hardware).length === 1) {
    const matches = next.filter((entry) => hardwareKey(entry) === hardware);
    if (matches.length === 1) return matches[0];
  }
  return next.find((entry) => {
    const current = hardwareKey(entry);
    return deviceKey(entry) === deviceKey(disk) && (!hardware || !current || hardware === current);
  }) || null;
}

function highlightSelection() {
  for (const row of $("rows").children) {
    const button = row.querySelector("button");
    if (!button) continue;
    const active = selected !== null && row.dataset.disk === deviceKey(selected);
    row.classList.toggle("selected", active);
    button.setAttribute("aria-pressed", String(active));
  }
  $("disk-picker").value = selected ? deviceKey(selected) : "";
}

function selectDisk(disk, reveal) {
  if (selected !== disk) $("raw").open = false;
  selected = disk;
  selection = disk;
  highlightSelection();
  showDetail();
  if (reveal) {
    $("disk-name").focus({preventScroll: true});
    $("disk-name").scrollIntoView({block: "nearest"});
  }
}

function showRows() {
  const query = $("search").value.trim().toLowerCase();
  const status = $("status-filter").value;
  const filtered = disks.filter((disk) => (!status || disk.health === status) &&
    [disk.model, disk.serial, disk.device, disk.controllerType, disk.wwn]
      .some((value) => String(value ?? "").toLowerCase().includes(query)));
  const rows = filtered.map((disk) => {
    const row = node("tr", "");
    row.setAttribute("role", "row");
    row.dataset.disk = deviceKey(disk);
    row.addEventListener("click", () => selectDisk(disk, true));
    const drive = node("td", "", "disk-cell");
    drive.setAttribute("role", "cell");
    const button = node("button", disk.model || "SAS 硬盘", "disk-button");
    button.type = "button";
    button.append(node("small", disk.device + " · " + disk.controllerType + (disk.serial ? " · " + disk.serial : "")));
    drive.append(button);
    const badge = node("td", "", "status-cell");
    badge.setAttribute("role", "cell");
    badge.append(node("span", labels[disk.health] || labels.unknown, "badge " + disk.health));
    row.append(drive, badge);
    const metrics = [
      ["容量", capacity(disk.bytes)], ["温度", count(disk.temperature, " ℃")],
      ["通电时间", count(disk.hours, " 小时")], ["G-list", count(disk.grownDefects)],
    ];
    for (const [label, value] of metrics) {
      const cell = node("td", "", "metric-cell");
      cell.setAttribute("role", "cell");
      const caption = node("span", label, "metric-caption");
      caption.setAttribute("aria-hidden", "true");
      cell.append(caption, node("span", value));
      row.append(cell);
    }
    return row;
  });
  if (disks.length && !rows.length) {
    const row = node("tr", "");
    row.className = "empty-row";
    row.setAttribute("role", "row");
    const cell = node("td", "没有匹配的硬盘。");
    cell.setAttribute("role", "cell");
    cell.colSpan = 6;
    rows.push(row);
    row.append(cell);
  }
  $("rows").replaceChildren(...rows);
  $("list").hidden = !disks.length;
  highlightSelection();
}

function updatePicker() {
  $("disk-picker").replaceChildren(...disks.map((disk) => {
    const option = node("option", disk.device + " · " + disk.controllerType +
      " · " + (disk.model || "SAS 硬盘") + (disk.serial ? " · " + disk.serial : ""));
    option.value = deviceKey(disk);
    return option;
  }));
}

function healthReasons(disk) {
  return (disk.healthReasons ?? []).map((reason) => reasonLabels[reason]).filter(Boolean).join("、");
}

function showDetail() {
  $("detail").hidden = !selected;
  selectScanDisk(selected);
  if (!selected) return;
  const disk = selected;
  $("disk-name").textContent = disk.model || "SAS 硬盘";
  $("disk-path").textContent = disk.device + " · " + disk.controllerType +
    (disk.serial ? " · " + disk.serial : "") + " · " + (labels[disk.health] || labels.unknown);
  const reasons = healthReasons(disk);
  $("health-reasons").textContent = reasons;
  $("health-reasons").hidden = !reasons;
  const lastIncrease = disk.nonMediumIncreasedAt && new Date(disk.nonMediumIncreasedAt);
  const fields = {
    "容量": capacity(disk.bytes), "温度": count(disk.temperature, " ℃"),
    "通电时间": count(disk.hours, " 小时"), "G-list": count(disk.grownDefects),
    "序列号": disk.serial, "WWN": disk.wwn, "固件": disk.firmware,
    "逻辑扇区": count(disk.logicalBlock, " B"), "物理扇区": count(disk.physicalBlock, " B"),
    "非介质错误累计": count(disk.nonMedium),
    "本次新增非介质错误": disk.nonMediumDelta == null
      ? (disk.nonMedium == null ? missing : "暂无可比记录")
      : count(disk.nonMediumDelta),
    "最近观测到新增": lastIncrease ? lastIncrease.toLocaleString("zh-CN", {hour12: false}) : missing,
    "SMART 总体判定": disk.smartPassed === true ? "通过" : disk.smartPassed === false ? "失败" : "未知",
  };
  $("meta").replaceChildren(...Object.entries(fields).map(([label, value]) => {
    const group = node("div", "");
    group.append(node("dt", label), node("dd", value || missing));
    return group;
  }));
  const operations = {read: "读取", write: "写入", verify: "校验"};
  $("errors").replaceChildren(...Object.entries(operations).map(([key, label]) => {
    const row = node("tr", "");
    const values = disk.errors?.[key] || {};
    row.append(node("td", label), node("td", count(values.corrected)), node("td", count(values.uncorrected)));
    return row;
  }));
  $("selftests").replaceChildren(...(disk.selfTests?.length ? disk.selfTests : ["硬盘未返回自检记录。"])
    .map((entry) => node("li", entry)));
  $("report").textContent = disk.report || "未返回报告";
  $("export").disabled = !disk.report;
}

async function refresh() {
  if (busy) return;
  busy = true;
  $("refresh").disabled = true;
  try {
    const response = await fetch("/app/sas-monitor/api/snapshot", {
      cache: "no-store", signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) throw new Error(String(response.status));
    const snapshot = await response.json();
    const dataChanged = collectedAt !== snapshot.collectedAt || snapshot.disks.length !== disks.length;
    if (dataChanged) {
      const next = snapshot.disks;
      const match = preserveSelection(disks, next, selection);
      if (!match) $("raw").open = false;
      disks = next;
      selected = match || disks[0] || null;
      if (selected) selection = selected;
      collectedAt = snapshot.collectedAt;
      updatePicker();
      showRows();
      showDetail();
    }
    $("export-all").disabled = !disks.length;
    const time = snapshot.collectedAt && new Date(snapshot.collectedAt);
    const stale = time && Date.now() - time.getTime() > 15 * 60 * 1000;
    $("updated").textContent = time ? "采集于 " + time.toLocaleString("zh-CN", {hour12: false}) : "尚无采集结果";
    $("count").textContent = snapshot.state === "ok" ? disks.length : missing;
    $("healthy").textContent = snapshot.state === "ok" ? disks.filter((disk) => disk.health === "passed").length : missing;
    $("alerts").textContent = snapshot.state === "ok" ? disks.filter((disk) => ["failed", "attention"].includes(disk.health)).length : missing;
    $("unknown").textContent = snapshot.state === "ok" ? disks.filter((disk) => disk.health === "unknown").length : missing;

    const issues = snapshot.issues || [];
    $("probe").hidden = issues.length === 0;
    $("probe-title").textContent = issues.length + " 个候选设备读取失败";
    $("probe-issues").replaceChildren(...issues.map((issue) => node("li", issue.device + " · " + issue.type + " · " + issue.message)));
    const empty = snapshot.candidates ? "没有确认到 SAS 盘。请检查控制器是否透传物理盘，或硬盘是否报告 SAS 协议。"
      : "没有发现可查询的 SCSI 物理盘。请检查 HBA 或 RAID 控制器是否向飞牛暴露硬盘。";
    notice([stale ? "采集结果已超过 15 分钟，请检查后台采集服务。" : "",
      messages[snapshot.state] || (!disks.length ? empty : ""),
      snapshot.baselineUnavailable ? "非介质错误基线暂未保存，请检查采集服务日志。" : "",
    ].filter(Boolean).join(" "));
    return snapshot;
  } catch (error) {
    notice(error.message === "403" ? "请使用飞牛管理员账号打开应用。"
      : "当前无法获取采集结果，请稍后重试。");
  } finally {
    $("refresh").disabled = manualPending;
    busy = false;
  }
}

function showRefreshStatus(message) {
  $("refresh-status").textContent = message;
  $("refresh-status").hidden = !message;
}

async function requestCollection() {
  if (manualPending) return;
  manualPending = true;
  const button = $("refresh");
  button.disabled = true;
  button.textContent = "采集中…";
  showRefreshStatus("已发送采集请求，等待 NAS 读取硬盘 SMART 数据。");
  try {
    const response = await fetch("/app/sas-monitor/api/refresh", {
      method: "POST", headers: {"X-SAS-Refresh": "1"}, signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const errors = {
        collector_upgrade: "请先运行新版采集服务的安装脚本，再点「立即采集」。",
        collector_unavailable: "无法提交采集请求。请检查 NAS 上采集服务的权限和运行状态。",
        cooldown: "刚有采集请求提交，请稍等 10 秒后再试。",
      };
      showRefreshStatus(errors[payload.state] || (response.status === 403
        ? "这次采集请求被拒绝。请从飞牛管理页面重新打开应用。"
        : "无法启动采集，请稍后重试。"));
      return;
    }

    const requestedAt = Date.parse((await response.json()).requestedAt);
    if (!Number.isFinite(requestedAt)) throw new Error("Missing request timestamp");
    const deadline = Date.now() + 255000;
    while (Date.now() < deadline) {
      const snapshot = await refresh();
      if (Date.parse(snapshot?.collectedAt) > requestedAt) {
        showRefreshStatus(snapshot.state === "ok" ? "采集完成，列表已更新。"
          : "本次采集未成功，请查看下方诊断信息。");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    showRefreshStatus("等待采集结果超时。请检查 NAS 上的采集服务日志。");
  } catch (error) {
    showRefreshStatus("采集请求没有收到确认，请稍后重试。");
  } finally {
    manualPending = false;
    button.textContent = "立即采集";
    button.disabled = busy;
  }
}

const scanText = {
  starting: "核对硬盘中", running: "扫描中", stopping: "等待当前读取结束",
  stopped: "已停止", interrupted: "扫描中断", limited: "已达到错误上限",
  raid: "此盘通过 RAID 控制器访问。逐扇区扫描需要直通的 SAS 物理盘。",
  path: "系统未提供可核实的 SAS 物理块设备路径。请检查 HBA 直通和硬盘映射。",
  identity: "缺少 WWN 或序列号，无法核对扫描目标。",
  upgrade: "请在 NAS 的新版 collector 目录运行 sudo bash ./install.sh，然后点击「立即采集」。",
  service: "暂时无法连接扫描服务，请检查 sas-monitor-scan.socket。",
  disk_changed: "硬盘身份与清单不符。请点「立即采集」后重新选盘。",
  diskseq: "当前内核无法核实块设备编号是否已被复用，无法扫描此盘。",
  geometry: "无法核对容量或逻辑扇区大小，已中断扫描。",
  offline: "硬盘离线、路径已变化或控制器未就绪。",
  short_read: "设备返回了不完整的扇区，已中断扫描。",
  io: "系统读取或保存状态失败，详情见扫描服务日志。",
  restart: "服务或 NAS 曾重启，进度停在最近保存的检查点。",
  snapshot: "采集清单暂不可用，请先点「立即采集」。",
  busy: "已有一项扫描正在运行。", job_changed: "该扫描任务已经结束或变化。",
  forbidden: "请以飞牛管理员身份重新打开应用。",
};
let scanDiskKey = null;
let scanTimer = null;
let scanRevision = 0;
let scanData = null;
let scanOnline = false;
let scanPending = false;
let scanConfirm = false;
let scanNote = "";

function renderScan() {
  if (!selected) return;
  const report = scanData?.result;
  const active = scanData?.active;
  const own = active && active.disk === selected.scanId;
  const other = active && !own;
  const support = selected.scanSupport || "upgrade";
  const state = report?.state;
  const completed = state === "complete";
  const warn = report?.unreadable > 0 || state === "limited" || state === "interrupted";
  const badge = $("scan-state");
  badge.textContent = completed ? (report.unreadable ? "完成 · 有读失败" : "完成 · 全盘可读")
    : scanText[state] || (support !== "available" ? "不可扫描" : scanOnline ? "尚未扫描" : "等待服务");
  badge.className = "badge" + (warn ? " attention" : completed ? " passed" : "");
  $("scan-hint").textContent = scanText[support] ||
    "从第一个逻辑扇区读到盘尾，扫描期间可能影响存储池读写速度。";
  $("scan-result").hidden = !report;
  if (report) {
    const ratio = report.totalSectors ? report.checkedSectors / report.totalSectors : 0;
    const pct = Math.floor(ratio * 1000) / 10;
    $("scan-progress").value = ratio * 100;
    $("scan-counts").textContent = report.totalSectors
      ? pct.toFixed(1) + "% · 已检查 " + count(report.checkedSectors) + " / " +
        count(report.totalSectors, " 个扇区") + " · 读失败 " + count(report.unreadable, " 个")
      : "尚未读取扇区";
    const when = (value) => new Date(value * 1000).toLocaleString("zh-CN", {hour12: false});
    $("scan-time").textContent = "开始于 " + when(report.startedAt) +
      (report.endedAt ? " · 结束于 " + when(report.endedAt) : "");
    const ranges = report.ranges || [];
    $("scan-ranges").hidden = !ranges.length;
    $("scan-lbas").replaceChildren(...ranges.map(([first, last]) =>
      node("li", first === last ? String(first) : first + " – " + last)));
  }
  const start = $("scan-start");
  start.hidden = !!own || support !== "available";
  start.disabled = !scanOnline || scanPending || !!active;
  start.textContent = scanConfirm ? "确认扫描此盘" : report ? "重新扫描" : "开始扫描";
  $("scan-back").hidden = !scanConfirm;
  $("scan-stop").hidden = !own;
  $("scan-stop").disabled = !scanOnline || scanPending || state === "stopping";
  const lines = [];
  if (scanConfirm) lines.push("将扫描 " + (selected.model || "SAS 硬盘") + " · " +
    (selected.serial || selected.wwn) + "（" + selected.device + "）。关闭窗口后扫描会继续。");
  if (other) lines.push("正在扫描另一块硬盘：" + active.label);
  if (state === "stopping") lines.push("已请求停止，正在等待硬盘返回当前读取。");
  if (state === "limited") lines.push("已发现 64 个不可读扇区，提前结束。其余区域尚未检查。");
  if (state === "interrupted") lines.push(scanText[report.error] || "扫描未完成。");
  if (report?.saveFailed) lines.push("结果只在本次服务运行期间可用，写入历史记录失败。");
  if (scanNote) lines.push(scanNote);
  $("scan-message").textContent = lines.join(" ");
  $("scan-message").hidden = !lines.length;
}

async function scanRequest(action = "") {
  const key = selected?.scanId;
  if (!key) return;
  if (scanTimer !== null) clearTimeout(scanTimer);
  const revision = ++scanRevision;
  scanPending = !!action;
  if (action) { scanConfirm = false; scanNote = ""; renderScan(); }
  try {
    const options = {cache: "no-store", signal: AbortSignal.timeout(6500)};
    if (action) Object.assign(options, {method: "POST", headers: {"X-SAS-Scan": "1"}});
    const response = await fetch("/app/sas-monitor/api/scan/" + key + action, options);
    const data = await response.json().catch(() => ({}));
    if (revision !== scanRevision) return;
    if (!response.ok) {
      const code = data.error || (response.status === 403 ? "forbidden" : "service");
      throw new Error(code);
    }
    scanOnline = true;
    scanData = data;
    scanNote = "";
    if (data.active) scanConfirm = false;
  } catch (error) {
    if (revision !== scanRevision) return;
    scanOnline = false;
    scanNote = scanText[error.message] || scanText.service;
  } finally {
    if (revision === scanRevision) {
      scanPending = false;
      renderScan();
      scanTimer = setTimeout(() => scanRequest(), !scanOnline || scanData?.active ? 3000 : 15000);
    }
  }
}

function selectScanDisk(disk) {
  const key = disk && (disk.scanId || deviceKey(disk)) + "/" + (disk.scanSupport || "upgrade");
  if (key === scanDiskKey) return;
  scanDiskKey = key;
  ++scanRevision;
  if (scanTimer !== null) clearTimeout(scanTimer);
  scanTimer = null;
  scanData = null;
  scanOnline = scanPending = scanConfirm = false;
  scanNote = "";
  $("scan-ranges").open = false;
  renderScan();
  if (disk?.scanId) scanRequest();
}

$("scan-start").addEventListener("click", () => {
  if (!selected?.scanId || !scanOnline || scanData?.active || scanPending) return;
  if (scanConfirm) { scanRequest("/start"); return; }
  scanConfirm = true;
  renderScan();
});
$("scan-back").addEventListener("click", () => { scanConfirm = false; renderScan(); });
$("scan-stop").addEventListener("click", () => {
  const active = scanData?.active;
  if (active && active.disk === selected?.scanId && !scanPending) scanRequest("/stop/" + active.job);
});

function download(filename, contents, contentType) {
  const url = URL.createObjectURL(new Blob([contents], {type: contentType}));
  const link = node("a", "");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csvCell(value) {
  let cell = String(value ?? "");
  // Drive firmware supplies text fields. Keep spreadsheet software from treating them as formulas.
  if (typeof value === "string" && /^[\s]*[=+@-]|^[\t\r\n]/.test(cell)) cell = "'" + cell;
  return '"' + cell.replace(/"/g, '""') + '"';
}

$("search").addEventListener("input", showRows);
$("status-filter").addEventListener("change", showRows);
$("disk-picker").addEventListener("change", () => {
  const disk = disks.find((entry) => deviceKey(entry) === $("disk-picker").value);
  if (disk) selectDisk(disk, false);
});
$("refresh").addEventListener("click", requestCollection);
$("export").addEventListener("click", () => {
  if (!selected?.report) return;
  const filename = "smart-" + selected.device.split("/").pop() + "-" +
    selected.controllerType.replace(/[^a-z0-9]+/gi, "-") + ".txt";
  download(filename, selected.report + "\n", "text/plain;charset=utf-8");
});
$("export-all").addEventListener("click", () => {
  const columns = ["采集时间", "设备", "类型", "型号", "序列号", "WWN", "固件", "状态", "容量 (B)",
    "逻辑扇区 (B)", "物理扇区 (B)", "温度 (℃)", "通电时间 (h)", "G-list", "非介质错误",
    "读取不可纠正", "写入不可纠正", "校验不可纠正", "本次新增非介质错误", "最近观测到新增", "判定原因"];
  const rows = disks.map((disk) => [collectedAt, disk.device, disk.controllerType, disk.model, disk.serial,
    disk.wwn, disk.firmware, labels[disk.health] || labels.unknown, disk.bytes, disk.logicalBlock,
    disk.physicalBlock, disk.temperature, disk.hours, disk.grownDefects, disk.nonMedium,
    disk.errors?.read?.uncorrected, disk.errors?.write?.uncorrected, disk.errors?.verify?.uncorrected,
    disk.nonMediumDelta, disk.nonMediumIncreasedAt, healthReasons(disk)]);
  const csv = [columns, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
  download("sas-disk-inventory.csv", "\uFEFF" + csv + "\r\n", "text/csv;charset=utf-8");
});
setupTheme();
refresh();
setInterval(() => { if (!manualPending) return refresh(); }, 30000);
