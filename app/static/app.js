/* 变电站质检系统前端：原生 JS，无构建步骤。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const resp = await fetch(path, {
    headers: isForm ? undefined : { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  const text = await resp.text();
  if (text) {
    try { body = JSON.parse(text); } catch (_e) { body = { error: text }; }
  }
  if (!resp.ok) {
    const msg = body && body.error ? body.error : `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.body = body;
    throw err;
  }
  return body;
}

function toast(message, kind = "info") {
  const el = $("#toast");
  el.className = `toast ${kind}`;
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 6000);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const ROLE_LABEL = { incoming: "进线", outgoing: "出线" };
const STATUS_LABEL = {
  complete: "完整", incomplete: "不完整", anomaly: "异常", normal: "正常",
};

/* ---------------- Tabs ---------------- */
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".tab-panel").forEach((p) =>
      p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
  });
});

/* ---------------- 配置 ---------------- */
function addConfigRow(point = "", role = "incoming") {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><input class="cp-point" value="${esc(point)}" placeholder="如 M-IN-01" /></td>
    <td><select class="cp-role">
      <option value="incoming">进线 incoming</option>
      <option value="outgoing">出线 outgoing</option>
    </select></td>
    <td><button type="button" class="danger cp-del">删除</button></td>`;
  tr.querySelector(".cp-role").value = role;
  tr.querySelector(".cp-del").addEventListener("click", () => tr.remove());
  $("#config-rows").appendChild(tr);
}
$("#config-add").addEventListener("click", () => addConfigRow());
addConfigRow("M-IN-01", "incoming");
addConfigRow("M-OUT-01", "outgoing");

$("#config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const points = $$("#config-rows tr").map((tr) => ({
    point_id: tr.querySelector(".cp-point").value.trim(),
    role: tr.querySelector(".cp-role").value,
  })).filter((p) => p.point_id);
  const payload = {
    effective_hour: e.target.effective_hour.value.trim(),
    points,
  };
  try {
    const res = await api("/api/configs", { method: "POST", body: JSON.stringify(payload) });
    toast(
      `配置已提交（版本 #${res.version_id}，${res.status === "created" ? "新建" : "同文幂等忽略"}）` +
      (res.invalidated_reports ? `；已失效旧日报 ${res.invalidated_reports} 份` : ""),
      "success");
    await refreshConfigs();
  } catch (err) {
    toast(`配置被拒收：${err.message}`, "error");
  }
});

async function refreshConfigs() {
  const versions = await api("/api/configs");
  $("#config-list").innerHTML = versions.length ? "" : `<p class="hint">暂无配置。</p>`;
  for (const v of versions) {
    const card = document.createElement("div");
    card.className = "card";
    const points = v.points.map((p) =>
      `<span class="badge role-${p.role}">${esc(p.point_id)} · ${ROLE_LABEL[p.role]}</span>`
    ).join(" ");
    card.innerHTML = `
      <div><strong>版本 #${v.version_id}</strong> 生效小时 <code>${esc(v.effective_hour)}</code></div>
      <div class="meta">提交于 ${esc(v.created_at)}</div>
      <div class="pill-row">${points}</div>`;
    $("#config-list").appendChild(card);
  }
}

/* ---------------- 读数 ---------------- */
function addReadingRow(point = "", hour = "", revision = 1, energy = "") {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><input class="r-point" value="${esc(point)}" placeholder="计量点编号" /></td>
    <td><input class="r-hour" value="${esc(hour)}" placeholder="2026-11-15T08:00:00Z" /></td>
    <td><input class="r-rev" type="number" min="1" value="${revision}" style="width:90px" /></td>
    <td><input class="r-energy" type="number" min="0" step="any" value="${esc(energy)}" /></td>
    <td><button type="button" class="danger r-del">删除</button></td>`;
  tr.querySelector(".r-del").addEventListener("click", () => tr.remove());
  $("#reading-rows").appendChild(tr);
}
$("#reading-add").addEventListener("click", () => addReadingRow());
addReadingRow();

$("#reading-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const readings = $$("#reading-rows tr").map((tr) => ({
    point_id: tr.querySelector(".r-point").value.trim(),
    hour: tr.querySelector(".r-hour").value.trim(),
    revision: Number(tr.querySelector(".r-rev").value),
    energy: Number(tr.querySelector(".r-energy").value),
  })).filter((r) => r.point_id && r.hour);
  try {
    const res = await api("/api/readings", { method: "POST", body: JSON.stringify({ readings }) });
    const okN = res.accepted.length, dupN = res.duplicates.length;
    toast(
      `读数处理完成：新收录 ${okN} 条，同文幂等 ${dupN} 条` +
      (res.invalidated_reports ? `；已失效旧日报 ${res.invalidated_reports} 份` : ""),
      "success");
    await refreshReadings();
  } catch (err) {
    toast(`读数被拒收（整批回滚，无部分写入）：${err.message}`, "error");
  }
});

async function refreshReadings() {
  const params = new URLSearchParams();
  const p = $("#reading-filter-point").value.trim();
  const h = $("#reading-filter-hour").value.trim();
  if (p) params.set("point_id", p);
  if (h) params.set("hour", h);
  const rows = await api(`/api/readings?${params.toString()}`);
  $("#reading-list").innerHTML = rows.length ? "" : `<p class="hint">暂无读数。</p>`;
  const card = document.createElement("div");
  card.className = "card";
  if (!rows.length) {
    $("#reading-list").innerHTML = `<p class="hint">暂无读数。</p>`;
    return;
  }
  card.innerHTML = `<table class="detail"><thead><tr>
    <th>计量点</th><th>UTC 小时</th><th>修订号</th><th>电量</th><th>来源</th><th>提交时刻 / 依据</th>
    </tr></thead><tbody>
    ${rows.map((r) => `<tr>
      <td>${esc(r.point_id)}</td><td><code>${esc(r.hour)}</code></td>
      <td>${r.revision}</td><td>${r.energy}</td>
      <td>${r.source === "derived"
        ? `<span class="badge role-outgoing">CSV 派生</span>`
        : `<span class="badge role-incoming">人工</span>`}</td>
      <td>${esc(r.created_at)}
        ${r.source === "derived" ? `<div class="meta trace">
          端点 ${r.start_cumulative_kwh}（批次 #${r.start_batch_id} 行 ${r.start_line_no}）
          → ${r.end_cumulative_kwh}（批次 #${r.end_batch_id} 行 ${r.end_line_no}）</div>` : ""}
      </td>
    </tr>`).join("")}</tbody></table>`;
  $("#reading-list").innerHTML = "";
  $("#reading-list").appendChild(card);
}
$("#reading-refresh").addEventListener("click", () =>
  refreshReadings().catch((e) => toast(e.message, "error")));

/* ---------------- 累计抄码导入 ---------------- */
const SAMPLE_CSV = [
  "point_id,hour,cumulative_kwh",
  "M-IN-01,2026-11-15T00:00:00Z,10000",
  "M-IN-01,2026-11-15T01:00:00Z,10120.5",
  "M-IN-01,2026-11-15T02:00:00Z,10245",
  "M-OUT-01,2026-11-15T00:00:00Z,5000",
  "M-OUT-01,2026-11-15T01:00:00Z,5118",
  "M-OUT-01,2026-11-15T02:00:00Z,5239",
].join("\n") + "\n";

$("#import-sample").addEventListener("click", () => {
  const blob = new Blob([SAMPLE_CSV], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "cumulative-readings-sample.csv";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
});

$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = e.target.file.files[0];
  if (!file) { toast("请选择 CSV 文件", "error"); return; }
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await api("/api/imports", { method: "POST", body: fd });
  const kind = res.status === "created" ? "新导入" : "同文件重传（幂等）";
    $("#import-result").innerHTML = `
      <div class="card import-summary">
        <div><strong>批次 #${res.batch_id}</strong>
          <span class="badge ${res.status === "created" ? "complete" : "invalid"}">${kind}</span>
        </div>
        <div class="meta">原件 <code>${esc(res.original_name)}</code>
          · ${res.byte_size} 字节 · ${res.row_count} 数据行 · SHA-256
          <code>${esc(res.sha256)}</code></div>
        <div class="meta">新示数端点 ${res.new_endpoints} · 已存在同值端点 ${res.duplicate_endpoints}
          · 新派生小时电量 ${res.new_derived_readings} 条
          · 已存在派生 ${res.duplicate_derived_readings} 条
          · 失效旧日报 ${res.invalidated_reports} 份</div>
        ${res.derived && res.derived.length ? `<details><summary>本次派生明细</summary>
          <table class="detail"><thead><tr>
            <th>计量点</th><th>小时</th><th>修订</th><th>电量</th>
            <th>起始示数（批次/行）</th><th>结束示数（批次/行）</th>
          </tr></thead><tbody>${res.derived.map((d) => `<tr>
            <td>${esc(d.point_id)}</td><td><code>${esc(d.hour)}</code></td>
            <td>rev${d.revision}</td><td>${d.energy}</td>
            <td>${d.start.cumulative_kwh}（#${d.start.batch_id}/${d.start.line_no}）</td>
            <td>${d.end.cumulative_kwh}（#${d.end.batch_id}/${d.end.line_no}）</td>
          </tr>`).join("")}</tbody></table></details>` : ""}
        <div class="row-actions">
          <a class="button-link" href="/api/imports/${res.batch_id}/download">下载原件（SHA-256 核验）</a>
          <button class="link" data-batch="${res.batch_id}">查看批次与追溯</button>
        </div>
      </div>`;
    bindBatchLinks($("#import-result"));
    toast(
      res.status === "created"
        ? `批次 #${res.batch_id} 已导入：新派生 ${res.new_derived_readings} 条` +
          (res.invalidated_reports ? `，失效旧日报 ${res.invalidated_reports} 份` : "")
        : `同一文件已导入过（批次 #${res.batch_id}），保留首次来源，未产生新数据`,
      "success");
    await refreshImports();
    await refreshReadings();
    await refreshReports();
  } catch (err) {
    $("#import-result").innerHTML =
      `<div class="invalid-banner error-banner">导入被拒收（整批回滚，无部分写入）：${esc(err.message)}</div>`;
    toast(`导入被拒收（整批回滚）：${err.message}`, "error");
  }
});

function bindBatchLinks(root) {
  root.querySelectorAll("button[data-batch]").forEach((b) =>
    b.addEventListener("click", () => showImportDetail(Number(b.dataset.batch))));
}

async function refreshImports() {
  const batches = await api("/api/imports");
  const box = $("#import-list");
  box.innerHTML = batches.length ? "" : `<p class="hint">暂无导入批次。</p>`;
  for (const b of batches) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div><strong>批次 #${b.batch_id}</strong>
        <span class="badge role-outgoing">${b.row_count} 行</span>
      </div>
      <div class="meta">文件 <code>${esc(b.original_name)}</code> · ${b.byte_size} 字节
        · 导入于 ${esc(b.created_at)}</div>
      <div class="meta">SHA-256 <code class="sha">${esc(b.sha256)}</code></div>
      <div class="row-actions">
        <a class="button-link" href="/api/imports/${b.batch_id}/download">下载原件</a>
        <a class="button-link" href="/api/imports/${b.batch_id}/verify" target="_blank" rel="noopener">核验 SHA-256</a>
        <button class="link" data-batch="${b.batch_id}">查看行与派生追溯</button>
      </div>`;
    bindBatchLinks(card);
    box.appendChild(card);
  }
}

async function showImportDetail(batchId) {
  const b = await api(`/api/imports/${batchId}`);
  const rowsHtml = b.rows.map((r) => `<tr>
    <td>${r.line_no}</td><td>${esc(r.point_id)}</td>
    <td><code>${esc(r.hour)}</code></td><td>${r.cumulative_kwh}</td>
    <td>${r.first_in_file ? "首次" : "同值重复"}</td></tr>`).join("");
  const derivedHtml = b.derived_readings.map((d) => `<tr>
    <td>${esc(d.point_id)}</td><td><code>${esc(d.hour)}</code></td>
    <td>rev${d.revision}</td><td>${d.energy}</td>
    <td>${d.start_cumulative_kwh} <span class="meta">批次 #${d.start_batch_id} 行 ${d.start_line_no}</span></td>
    <td>${d.end_cumulative_kwh} <span class="meta">批次 #${d.end_batch_id} 行 ${d.end_line_no}</span></td>
  </tr>`).join("");
  $("#import-detail").innerHTML = `
    <h3>批次 #${b.batch_id} 详情</h3>
    <div class="card">
      <div class="meta">文件 <code>${esc(b.original_name)}</code> · SHA-256
        <code class="sha">${esc(b.sha256)}</code> · 导入于 ${esc(b.created_at)}</div>
      <h4>原文数据行（含行号）</h4>
      <table class="detail"><thead><tr>
        <th>CSV 行号</th><th>计量点</th><th>UTC 整点</th><th>累计示数</th><th>文件内</th>
      </tr></thead><tbody>${rowsHtml}</tbody></table>
      <h4>与本批次相关的派生读数</h4>
      ${derivedHtml ? `<table class="detail"><thead><tr>
        <th>计量点</th><th>小时</th><th>修订</th><th>电量</th>
        <th>起始示数（来源）</th><th>结束示数（来源）</th>
      </tr></thead><tbody>${derivedHtml}</tbody></table>` : `<p class="hint">暂无。</p>`}
    </div>`;
  $("#import-detail").scrollIntoView({ behavior: "smooth" });
}
$("#import-refresh").addEventListener("click", () =>
  refreshImports().catch((e) => toast(e.message, "error")));

/* ---------------- 日报 ---------------- */
$("#report-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    report_date: e.target.report_date.value,
    tolerance: Number(e.target.tolerance.value),
  };
  try {
    const res = await api("/api/reports/generate", {
      method: "POST", body: JSON.stringify(payload),
    });
    toast(
      `日报 #${res.id} ${res.generation === "created" ? "已生成" : "为现有有效日报，直接取回"}` +
      `（状态：${STATUS_LABEL[res.status]}）`,
      "success");
    await refreshReports();
    showReportDetail(res.id);
  } catch (err) {
    toast(`日报生成失败（已回滚，不留半份日报）：${err.message}`, "error");
  }
});

async function refreshReports() {
  const params = new URLSearchParams();
  const d = $("#report-filter-date").value.trim();
  if (d) params.set("report_date", d);
  params.set("include_invalid", $("#report-include-invalid").checked ? "true" : "false");
  const reports = await api(`/api/reports?${params.toString()}`);
  $("#report-list").innerHTML = reports.length ? "" : `<p class="hint">暂无日报。</p>`;
  for (const r of reports) {
    const card = document.createElement("div");
    card.className = "card";
    const invalidBadge = r.valid ? "" :
      ` <span class="badge invalid">已失效</span>`;
    card.innerHTML = `
      <div><strong>日报 #${r.id}</strong>
        <span class="badge ${r.status}">${STATUS_LABEL[r.status]}</span>${invalidBadge}
      </div>
      <div class="meta">日期 <code>${esc(r.report_date)}</code> · 容差 ${r.tolerance}
        · 生成于 ${esc(r.created_at)}</div>
      ${r.valid ? "" : `<div class="meta">失效原因：${esc(r.invalidated_reason || "-")}
        （${esc(r.invalidated_at || "-")}）</div>`}
      <div class="row-actions"><button class="link" data-id="${r.id}">查看详情与依据</button></div>`;
    card.querySelector("button").addEventListener("click", () => showReportDetail(r.id));
    $("#report-list").appendChild(card);
  }
}
$("#report-refresh").addEventListener("click", () =>
  refreshReports().catch((e) => toast(e.message, "error")));
$("#report-include-invalid").addEventListener("change", () =>
  refreshReports().catch((e) => toast(e.message, "error")));

async function showReportDetail(id) {
  const r = await api(`/api/reports/${id}`);
  const box = $("#report-detail");
  const rowsHtml = r.detail.map((h) => {
    const readings = (h.readings || []).map((x) => {
      if (x.missing) {
        return `<span class="badge incomplete">${esc(x.point_id)}(${ROLE_LABEL[x.role]}) 缺读数</span>`;
      }
      const src = x.source === "derived" ? " · CSV派生" : "";
      return `<span class="badge role-${x.role}">${esc(x.point_id)}(${ROLE_LABEL[x.role]})` +
        `=${esc(String(x.energy))} rev${x.revision}${src}</span>`;
    }).join(" ");
    const basis = (h.readings || []).filter((x) => !x.missing)
      .map((x) => `<div class="meta trace">${esc(x.basis || "")}</div>`).join("");
    return `<tr>
      <td><code>${esc(h.hour)}</code></td>
      <td><span class="badge ${h.status}">${STATUS_LABEL[h.status] || esc(h.status)}</span></td>
      <td>${h.config_version_id
        ? `版本 #${h.config_version_id}（生效 <code>${esc(h.config_effective_hour)}</code>）`
        : "—"}</td>
      <td>${readings}${basis}<div class="meta">${esc(h.reason || "")}</div></td>
      <td>${h.status === "anomaly" || h.status === "normal"
        ? `进线 ${h.incoming_sum} / 出线 ${h.outgoing_sum}<br/>差 ${h.difference}` +
          `（绝对差 ${h.abs_difference}，容差 ${h.tolerance}）`
        : "—"}</td>
    </tr>`;
  }).join("");
  box.innerHTML = `
    <h3>日报 #${r.id} 详情（依据为生成时快照）
      <span class="badge ${r.status}">${STATUS_LABEL[r.status]}</span>
      ${r.valid ? "" : `<span class="badge invalid">已失效</span>`}
    </h3>
    ${r.valid ? "" : `<div class="invalid-banner">本日报已失效：${esc(r.invalidated_reason || "-")}
      （${esc(r.invalidated_at || "-")}）。下列内容为历史快照，仅供追溯。</div>`}
    <table class="detail"><thead><tr>
      <th>小时 (UTC)</th><th>结论</th><th>当时配置依据</th><th>计量点读数与修订依据</th><th>差值核算</th>
    </tr></thead><tbody>${rowsHtml}</tbody></table>`;
  box.scrollIntoView({ behavior: "smooth" });
}

/* ---------------- 初始化 ---------------- */
refreshConfigs().catch((e) => toast(e.message, "error"));
refreshReadings().catch((e) => toast(e.message, "error"));
refreshImports().catch((e) => toast(e.message, "error"));
refreshReports().catch((e) => toast(e.message, "error"));
