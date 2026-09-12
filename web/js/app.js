/* 教保機構風險預警系統 — 前端應用邏輯（無框架，純標準瀏覽器 API）
 * 與 charts.js（KChart）搭配，透過 /api/* 端點取得後端已計算好的結果並渲染。
 */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const main = () => document.getElementById("main");

  const STATE = {
    meta: null,
    bandColor: {},
    bandLabel: {},
    cities: [],
    orgTypes: [],
    listQuery: { q: "", city: "", org_type: "", band: "", sort: "score", order: "desc" },
    scheduleCapacity: 40,
  };

  // ---------------------------------------------------------------- 工具
  function fmt(v, d) { return window.KChart.fmtNum(v, d); }
  function pct(v, d) { return v === null || v === undefined ? "-" : fmt(v * 100, d === undefined ? 1 : d) + "%"; }
  function money(v) { return v === null || v === undefined ? "-" : fmt(v, 0) + " 元"; }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function qs(obj) {
    const parts = [];
    for (const k in obj) {
      if (obj[k] === undefined || obj[k] === null || obj[k] === "") continue;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(obj[k]));
    }
    return parts.join("&");
  }
  // ---------------------------------------------------------------- 資料存取
  // 本前端支援兩種運行模式：
  //
  // 1. **本機程式模式**：由 app/server.py 提供 /api/* 動態端點，可調整權重、
  //    重新載入資料、呼叫 AI 代理人。
  // 2. **靜態網站模式**（GitHub Pages）：沒有後端可執行 Python，改讀取
  //    scripts/export_static.py 事先匯出的 JSON 檔。分析結果完全相同，
  //    但涉及重新計算或寫入的操作無法提供，會明確告知使用者而非靜默失敗。
  //
  // 模式由 web/static-mode.js 設定 window.KREWS_STATIC 決定；靜態匯出時
  // 會一併產生該檔案，本機執行時則不存在，因此預設為動態模式。
  const STATIC = !!window.KREWS_STATIC;
  const STATIC_BASE = (window.KREWS_STATIC_BASE || "api").replace(/\/$/, "");

  // 靜態模式下，帶查詢字串的端點改由前端自行處理：
  //   /institutions?...  → 讀取完整清單後在瀏覽器內篩選排序
  //   /schedule?capacity → 讀取預先匯出的排程結果
  //   /institution?id=   → 讀取該機構的獨立 JSON（避免單一巨大檔案）
  function staticUrl(path) {
    const [route, query] = path.split("?");
    const p = new URLSearchParams(query || "");
    if (route === "/institution") {
      return `${STATIC_BASE}/institution/${encodeURIComponent(p.get("id"))}.json`;
    }
    if (route === "/institutions") return `${STATIC_BASE}/institutions.json`;
    if (route === "/schedule") return `${STATIC_BASE}/schedule.json`;
    return `${STATIC_BASE}${route}.json`;
  }

  const WRITE_HINT = "此為線上展示版（靜態網站），無法變更設定或重新分析。" +
    "如需調整權重、重新採集資料或啟用 AI 深度偵查，請下載可執行檔在本機執行。";

  async function api(path, opts) {
    if (STATIC) {
      const method = ((opts && opts.method) || "GET").toUpperCase();
      if (method !== "GET") throw new Error(WRITE_HINT);
      const res = await fetch(staticUrl(path), { cache: "no-cache" });
      if (!res.ok) {
        throw new Error(res.status === 404
          ? "線上展示版未包含此項資料。" : res.status + " " + res.statusText);
      }
      const data = await res.json();
      return path.startsWith("/institutions?")
        ? filterInstitutions(data, path) : data;
    }
    const res = await fetch("/api" + path, opts);
    let data = {};
    try { data = await res.json(); } catch (e) { /* noop */ }
    if (!res.ok) throw new Error(data.error || (res.status + " " + res.statusText));
    return data;
  }

  // 靜態模式的機構清單篩選：複製後端 /api/institutions 的行為，
  // 讓搜尋、篩選與排序在瀏覽器端得到一致結果。
  function filterInstitutions(data, path) {
    const p = new URLSearchParams((path.split("?")[1]) || "");
    const q = (p.get("q") || "").trim().toLowerCase();
    const city = p.get("city") || "";
    const orgType = p.get("org_type") || "";
    const band = p.get("band") || "";
    const sort = p.get("sort") || "score";
    const order = (p.get("order") || "desc") === "asc" ? 1 : -1;
    const limit = parseInt(p.get("limit") || "400", 10);

    let rows = (data.rows || []).filter(r => {
      if (city && r.city !== city) return false;
      if (orgType && r.org_type !== orgType) return false;
      if (band && r.band !== band) return false;
      if (!q) return true;
      return [r.name, r.inst_id, r.district, r.org_type]
        .some(v => String(v || "").toLowerCase().includes(q));
    });
    rows.sort((a, b) => {
      const x = a[sort], y = b[sort];
      if (typeof x === "number" && typeof y === "number") return (x - y) * order;
      return String(x || "").localeCompare(String(y || ""), "zh-Hant") * order;
    });
    return { ...data, total: rows.length, rows: rows.slice(0, limit) };
  }
  function debounce(fn, ms) {
    let t = null;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), ms);
    };
  }
  let toastTimer = null;
  function toast(msg, isErr) {
    let t = $(".toast");
    if (!t) {
      t = document.createElement("div");
      t.className = "toast";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.toggle("err", !!isErr);
    t.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("on"), 3200);
  }
  function tag(band, label) {
    return `<span class="tag ${esc(band || "info")}">${esc(label || band || "-")}</span>`;
  }
  function scorePill(score, band) {
    const c = STATE.bandColor[band] || "#64748b";
    return `<span class="score-pill" style="background:${c}22;color:${c};border:1px solid ${c}55">${fmt(score, 1)}</span>`;
  }
  function miniBar(score, band) {
    const c = STATE.bandColor[band] || "#64748b";
    const w = Math.max(2, Math.min(100, score));
    return `<div class="mini-bar"><i style="width:${w}%;background:${c}"></i></div>`;
  }
  function linkInst(id, name) {
    return `<a href="#/institution/${encodeURIComponent(id)}">${esc(name)}</a>`;
  }
  function mapsUrl(query) {
    return "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(query);
  }
  function mapsLink(inst) {
    const q = (inst.address && inst.address.trim()) || inst.name;
    if (!q) return "";
    return `<a href="${mapsUrl(q)}" target="_blank" rel="noopener">在 Google 地圖開啟&nbsp;&#128205;</a>`;
  }
  function card(title, hint, bodyHtml, extra) {
    return `<div class="card">
      <h3>${title}</h3>
      ${hint ? `<div class="hint">${hint}</div>` : ""}
      ${bodyHtml}
    </div>`;
  }
  function kpi(label, value, unit, tone, desc) {
    return `<div class="kpi ${tone || ""}">
      <div class="k">${label}</div>
      <div class="v">${value}${unit ? `<span class="u">${unit}</span>` : ""}</div>
      ${desc ? `<div class="d">${desc}</div>` : ""}
    </div>`;
  }
  function emptyIf(list, html) {
    return (!list || !list.length) ? '<div class="empty">目前無資料</div>' : html;
  }

  // ---------------------------------------------------------------- 啟動
  const bootBar = document.getElementById("bootBar");
  const bootMsg = document.getElementById("bootMsg");
  const bootEl = document.getElementById("boot");

  async function pollStatus() {
    try {
      const s = await api("/status");
      bootBar.style.width = (s.progress || 0) + "%";
      bootMsg.textContent = `${s.status || "處理中"}…（${s.progress || 0}%）`;
      if (s.error) {
        bootEl.classList.add("err");
        bootMsg.textContent = "發生錯誤：" + s.error.split("\n")[0];
        return;
      }
      if (s.ready) {
        await boot();
        return;
      }
    } catch (e) {
      bootMsg.textContent = "等待服務啟動…";
    }
    setTimeout(pollStatus, 450);
  }

  async function boot() {
    const meta = await api("/meta");
    STATE.meta = meta;
    (meta.bands || []).forEach(b => { STATE.bandColor[b.key] = b.color; STATE.bandLabel[b.key] = b.label; });
    document.getElementById("brandName").textContent = meta.app.name;
    document.getElementById("brandSub").textContent = meta.app.subtitle;
    document.getElementById("brandVer").textContent = "v" + meta.app.version +
      "　資料基準日 " + meta.as_of;
    document.title = meta.app.name;
    bootEl.style.display = "none";
    document.getElementById("app").style.display = "";
    window.addEventListener("hashchange", nav);
    nav();
  }
  pollStatus();

  // ---------------------------------------------------------------- 路由
  const ROUTES = {
    dashboard: renderDashboard,
    institutions: renderInstitutions,
    institution: renderInstitutionDetail,
    forensics: renderForensics,
    sentiment: renderSentiment,
    schedule: renderSchedule,
    metrics: renderMetrics,
    settings: renderSettings,
  };

  const DATA_ROUTES = new Set(["dashboard", "institutions", "institution",
    "forensics", "sentiment", "schedule", "metrics"]);

  function noDataCard(meta) {
    const data = (meta && meta.data) || {};
    const warnings = data.warnings || [];
    return `
      ${pageHead("尚無資料", "本系統不會以模擬或虛構資料頂替，需放入真實資料後才會顯示分析結果。")}
      <div class="note warn">
        <b>尚未偵測到 institutions 資料表。</b><br>
        請將真實資料檔案（CSV／XLSX，欄位可參考 app/datastore.py 之欄位別名對照）
        放入執行檔同層的 <b>data/</b> 資料夾，或於「設定」頁指定 AWS 公開網址後按
        「重新載入資料」。
        ${warnings.length ? "<br><br><b>目前訊息：</b><br>" + warnings.map(esc).join("<br>") : ""}
      </div>
      <div class="card"><h3>前往設定</h3>
        <button class="btn" onclick="location.hash='#/settings'">開啟設定頁</button>
      </div>`;
  }

  async function nav() {
    const hash = (location.hash || "#/dashboard").replace(/^#\/?/, "");
    const parts = hash.split("/");
    const route = parts[0] || "dashboard";
    const param = parts[1];
    $$(".nav a").forEach(a => a.classList.toggle("on", a.dataset.key === route));
    main().innerHTML = '<div class="loading"><span class="spin"></span>&nbsp; 載入中…</div>';
    try {
      if (DATA_ROUTES.has(route)) {
        const s = await api("/summary");
        if (!s.n_institutions) {
          main().innerHTML = noDataCard(STATE.meta);
          return;
        }
      }
      const fn = ROUTES[route] || renderDashboard;
      await fn(param);
    } catch (e) {
      main().innerHTML = `<div class="card"><h3>載入失敗</h3><div class="hint">${esc(e.message)}</div></div>`;
    }
  }

  function renderAiReport(report, instId) {
    if (!report) {
      return `<button class="btn" id="btnAiRun">執行 AI 深度偵查</button>
        <div id="aiStatus" class="hint" style="margin-top:8px"></div>`;
    }
    if (report.error) {
      return `<div class="note warn">${esc(report.error)}</div>
        <button class="btn" id="btnAiRun" style="margin-top:8px">重新執行</button>
        <div id="aiStatus" class="hint" style="margin-top:8px"></div>`;
    }
    const bandKey = { "極高風險": "critical", "高風險": "high", "中風險": "medium",
                     "低風險": "low", "極低風險": "minimal" }[report.risk_level] || "info";
    return `
      <div class="grid g4" style="margin-bottom:12px">
        ${kpi("AI 研判分數", fmt(report.risk_score, 1), "", "", "")}
        ${kpi("AI 風險等級", tag(bandKey, report.risk_level), "")}
        ${kpi("信心程度", esc(report.confidence || "-"), "")}
        ${kpi("分析時間", esc(report._generated_at || "-"), "", "", esc(report._model || ""))}
      </div>
      <div class="reason ${bandKey}">
        <div class="t">研判摘要</div>
        <div class="x">${esc(report.summary || "")}</div>
      </div>
      <div class="grid g2" style="margin-top:12px">
        <div>
          <h3 style="font-size:14px">關鍵發現</h3>
          ${emptyIf(report.key_findings, (report.key_findings||[]).map(f =>
            `<div class="reason medium"><div class="x">${esc(f)}</div></div>`).join(""))}
        </div>
        <div>
          <h3 style="font-size:14px">建議稽查作為</h3>
          <div class="pill-row">${(report.recommended_actions||[]).map(a =>
            `<span class="pill">${esc(a)}</span>`).join("")}</div>
        </div>
      </div>
      <button class="btn ghost" id="btnAiRun" style="margin-top:14px">重新執行分析</button>
      <div id="aiStatus" class="hint" style="margin-top:8px"></div>
    `;
  }

  function pageHead(title, desc, actionsHtml) {
    return `<div class="page-head">
      <div><h2>${title}</h2>${desc ? `<div class="desc">${desc}</div>` : ""}</div>
      <div class="actions">${actionsHtml || ""}</div>
    </div>`;
  }

  // ================================================================ 總覽
  async function renderDashboard() {
    const s = await api("/summary");
    const kAuc = s.kpi || {};
    main().innerHTML = `
      ${pageHead("風險總覽", `資料基準日 ${s.as_of}　最近一次分析：${s.built_at || "-"}`,
        `<button class="btn ghost" id="btnReload">重新載入資料</button>
         <a class="btn" href="/api/export.xlsx">匯出總表 Excel</a>`)}
      <div class="grid g4">
        ${kpi("機構總數", fmt(s.n_institutions), "所")}
        ${kpi("高風險機構", fmt(s.n_high_risk), "所", "warn", `占比 ${fmt(s.high_risk_pct, 1)}%`)}
        ${kpi("極高風險（需立即處理）", fmt(s.n_critical), "所", "crit")}
        ${kpi("平均風險分數", fmt(s.avg_score, 1), "", "", `中位數 ${fmt(s.median_score, 1)}`)}
      </div>
      <div class="grid g4">
        ${kpi("模型 AUC", fmt(kAuc.auc, 3), "", "ok", "訓練期樣本外驗證")}
        ${kpi("前20%預警精準率", pct(kAuc.precision_at_20pct), "", "", "命中即後續被裁罰")}
        ${kpi("前20%預警召回率", pct(kAuc.recall_at_20pct), "")}
        ${kpi("平均提前預警天數", fmt(kAuc.lead_days, 0), "天", "", "相較裁罰發生時間")}
      </div>
      <div class="note">
        <b>資料來源</b>：模式＝${s.data && s.data.mode === "demo" ? "內建示範資料" : "外部資料"}；
        載入時間 ${s.data && s.data.loaded_at || "-"}。
        ${(s.data && s.data.warnings && s.data.warnings.length)
          ? `<br><b>提醒</b>：${s.data.warnings.map(esc).join("；")}` : ""}
        詳細資料來源與筆數請至「設定」頁查看。
      </div>
      <div class="grid g23">
        <div class="card"><h3>風險等級分布</h3>
          <div class="hint">依全體機構分數相對排序分級（極高風險 = 風險最高前 2%）。</div>
          <div id="cBands"></div>
          <div class="legend">${(s.bands || []).map(b =>
            `<span><i style="background:${b.color}"></i>${b.label} ${b.count} 所（${b.pct_range}）</span>`).join("")}</div>
        </div>
        <div class="card"><h3>五大構面平均風險</h3>
          <div class="hint">各構面 0～100 分平均值，虛線可對照目前權重設定。</div>
          <div id="cRadar"></div>
        </div>
      </div>
      <div class="grid g2">
        <div class="card"><h3>分數區間分布</h3><div id="cHist"></div></div>
        <div class="card"><h3>各設立類型平均風險</h3><div id="cOrg"></div></div>
      </div>
      <div class="card"><h3>縣市別平均風險（前 12）</h3><div id="cCity"></div></div>
      <div class="card"><h3>重點關注名單（Top 25）</h3>
        <div class="scroll">
        <table class="tbl">
          <thead><tr><th>排名</th><th>機構名稱</th><th>縣市</th><th>類型</th>
            <th class="num">分數</th><th>等級</th><th>主要風險原因</th></tr></thead>
          <tbody>${emptyIf(s.top, (s.top || []).map(r => `
            <tr class="clickable" onclick="location.hash='#/institution/${esc(r.inst_id)}'">
              <td>${r.rank}</td><td class="nm">${esc(r.name)}</td>
              <td>${esc(r.city)}${r.district ? "・" + esc(r.district) : ""}</td>
              <td>${esc(r.org_type)}</td>
              <td class="num">${scorePill(r.score, r.band)}</td>
              <td>${tag(r.band, r.band_label)}</td>
              <td>${(r.top_reasons || []).join("、")}</td>
            </tr>`).join(""))}</tbody>
        </table>
        </div>
      </div>
    `;
    window.KChart.donut("cBands", {
      data: (s.bands || []).map(b => ({ label: b.label, value: b.count, color: b.color })),
      centerLabel: "機構總數",
    });
    window.KChart.radar("cRadar", {
      axes: (s.dimension_avg || []).map(d => d.label),
      series: [{ name: "平均分數", values: (s.dimension_avg || []).map(d => d.avg), color: "#38bdf8" }],
    });
    window.KChart.bar("cHist", {
      data: (s.histogram || []).map(h => ({ label: h.bucket, value: h.count })),
      valueLabel: "機構數", showValue: true,
    });
    window.KChart.bar("cOrg", {
      data: (s.by_org_type || []).map(o => ({ label: o.org_type, value: o.avg,
        note: `高風險占比 ${fmt(o.high_pct, 1)}%（共 ${o.count} 所）` })),
      valueLabel: "平均分數", decimals: 1, showValue: true,
    });
    window.KChart.hbar("cCity", {
      data: (s.by_city || []).slice(0, 12).map(c => ({ label: c.city, value: c.avg,
        note: `高風險 ${c.high} 所／共 ${c.count} 所` })),
      valueLabel: "平均分數", decimals: 1,
    });
    $("#btnReload").addEventListener("click", async () => {
      toast("已送出重新載入請求，將於背景重新分析…");
      await api("/reload", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      location.hash = "#/dashboard";
      setTimeout(() => location.reload(), 600);
    });
  }

  // ================================================================ 機構清單
  async function renderInstitutions() {
    const meta = STATE.meta;
    main().innerHTML = `
      ${pageHead("機構清單", "點擊欄位可排序；點擊機構名稱查看詳細鑑識與風險說明。",
        `<a class="btn" href="/api/export.xlsx">匯出 Excel</a>`)}
      <div class="filters">
        <input type="text" id="fq" placeholder="搜尋機構名稱／代碼／行政區" value="${esc(STATE.listQuery.q)}">
        <select id="fCity"><option value="">全部縣市</option></select>
        <select id="fOrg"><option value="">全部類型</option></select>
        <select id="fBand"><option value="">全部等級</option></select>
        <label><span id="fCount"></span></label>
      </div>
      <div class="card">
        <div class="scroll">
        <table class="tbl" id="instTbl">
          <thead><tr>
            <th class="srt num" data-s="rank">排名</th>
            <th class="srt" data-s="name">機構名稱</th>
            <th class="srt" data-s="city">縣市／行政區</th>
            <th class="srt" data-s="org_type">類型</th>
            <th class="num">招收／核定</th>
            <th class="srt num" data-s="score">風險分數</th>
            <th>等級</th>
            <th class="srt num" data-s="pen_count_1y">近1年裁罰</th>
            <th class="srt num" data-s="senti_negative">負面聲量</th>
            <th>主要原因</th>
          </tr></thead>
          <tbody id="instBody"></tbody>
        </table>
        </div>
      </div>
    `;
    (meta.org_types || []).forEach(o => $("#fOrg").insertAdjacentHTML("beforeend",
      `<option value="${esc(o)}">${esc(o)}</option>`));
    (meta.bands || []).forEach(b => $("#fBand").insertAdjacentHTML("beforeend",
      `<option value="${esc(b.key)}">${esc(b.label)}</option>`));
    $("#fCity").value = STATE.listQuery.city;
    $("#fOrg").value = STATE.listQuery.org_type;
    $("#fBand").value = STATE.listQuery.band;

    async function load() {
      const q = STATE.listQuery;
      const data = await api("/institutions?" + qs({ ...q, limit: 400 }));
      if (!$("#fCity").children.length || $("#fCity").children.length <= 1) {
        (data.cities || []).forEach(c => $("#fCity").insertAdjacentHTML("beforeend",
          `<option value="${esc(c)}">${esc(c)}</option>`));
        $("#fCity").value = q.city;
      }
      $("#fCount").textContent = `共 ${data.total} 所，顯示 ${data.returned} 所`;
      $$("#instTbl th.srt").forEach(th => {
        th.textContent = th.textContent.replace(/ ▲| ▼/, "");
        if (th.dataset.s === q.sort) th.textContent += (q.order === "desc" ? " ▼" : " ▲");
      });
      $("#instBody").innerHTML = emptyIf(data.rows, data.rows.map(r => `
        <tr class="clickable" onclick="location.hash='#/institution/${esc(r.inst_id)}'">
          <td class="num">${r.rank}</td>
          <td class="nm">${esc(r.name)}
            <a href="${mapsUrl(r.name + " " + (r.city||""))}" target="_blank" rel="noopener"
               onclick="event.stopPropagation()" title="在 Google 地圖開啟">&#128205;</a></td>
          <td>${esc(r.city)}${r.district ? "・" + esc(r.district) : ""}</td>
          <td>${esc(r.org_type)}</td>
          <td class="num">${fmt(r.enrolled)}／${fmt(r.capacity)}</td>
          <td class="num">${scorePill(r.score, r.band)} ${miniBar(r.score, r.band)}</td>
          <td>${tag(r.band, r.band_label)}${r.burst ? ' <span class="tag critical">爆量</span>' : ""}</td>
          <td class="num">${fmt(r.pen_count_1y)}</td>
          <td class="num">${fmt(r.senti_negative)}</td>
          <td>${(r.top_reasons || []).slice(0, 2).join("、")}</td>
        </tr>`).join(""));
    }
    const reload = debounce(load, 300);
    $("#fq").addEventListener("input", e => { STATE.listQuery.q = e.target.value; reload(); });
    $("#fCity").addEventListener("change", e => { STATE.listQuery.city = e.target.value; load(); });
    $("#fOrg").addEventListener("change", e => { STATE.listQuery.org_type = e.target.value; load(); });
    $("#fBand").addEventListener("change", e => { STATE.listQuery.band = e.target.value; load(); });
    $$("#instTbl th.srt").forEach(th => th.addEventListener("click", () => {
      const s = th.dataset.s;
      if (STATE.listQuery.sort === s) {
        STATE.listQuery.order = STATE.listQuery.order === "desc" ? "asc" : "desc";
      } else {
        STATE.listQuery.sort = s;
        STATE.listQuery.order = (s === "name" || s === "city" || s === "org_type") ? "asc" : "desc";
      }
      load();
    }));
    await load();
  }

  // ================================================================ 機構詳情
  async function renderInstitutionDetail(id) {
    const d = await api("/institution?id=" + encodeURIComponent(id));
    const inst = d.inst, row = d.row || {}, ex = d.explain, act = d.action;
    const f = d.features, det = d.detail;
    const limit2 = STATE.meta.settings && 8; // 顯示用，實際門檻見下方文字
    main().innerHTML = `
      <div class="bcrumb"><a onclick="location.hash='#/institutions'">機構清單</a> / ${esc(inst.name)}</div>
      ${pageHead(esc(inst.name),
        `${esc(inst.city)}${inst.district ? "・" + esc(inst.district) : ""}　${esc(inst.org_type)}
         負責人：${esc(inst.principal || "-")}　設立年：${esc(inst.found_year || "-")}`,
        `<a class="btn ghost" href="${mapsUrl((inst.address && inst.address.trim()) || inst.name)}" target="_blank" rel="noopener">&#128205; 在地圖開啟</a>
         <a class="btn" href="/api/report?id=${encodeURIComponent(id)}">下載預警單</a>`)}

      <div class="grid g4">
        ${kpi("風險分數", fmt(row.score, 1), "", "", `排名第 ${row.rank} 名（前 ${fmt(row.percentile,1)}%）`)}
        ${kpi("風險等級", tag(row.band, row.band_label), "")}
        ${kpi("建議稽查時效", act.urgency, "", "warn", act.mode)}
        ${kpi("模型預測機率", pct(row.model_prob), "", "", `規則分 ${fmt(row.rule_score,1)}／模型分 ${fmt(row.model_score,1)}`)}
      </div>

      <div class="grid g32">
        <div class="card"><h3>風險因子說明</h3>
          <div class="hint">依嚴重程度排序；證據取自公開資料交叉比對與鑑識會計檢定。</div>
          ${emptyIf(ex.reasons, ex.reasons.map(r => `
            <div class="reason ${r.level}">
              <div class="t">${tag(r.level, r.level)} ${esc(r.title)}</div>
              <div class="x">${esc(r.text)}</div>
              ${r.evidence ? `<div class="e">${esc(r.evidence)}</div>` : ""}
            </div>`).join(""))}
        </div>
        <div class="card"><h3>五構面雷達圖</h3><div id="cRadar"></div>
          <div class="kv" style="margin-top:10px">
            ${ex.dimensions.map(dd => `<div class="k">${dd.label}</div><div class="v">${fmt(dd.score,1)} 分（權重 ${pct(dd.weight,0)}）</div>`).join("")}
          </div>
        </div>
      </div>

      <div class="card"><h3>建議稽查作為</h3>
        <div class="hint">聚焦構面：${act.focus}</div>
        <div class="pill-row">${(act.actions || []).map(a => `<span class="pill">${esc(a)}</span>`).join("")}</div>
        ${act.critical_flags && act.critical_flags.length ? `<div class="note warn" style="margin-top:10px"><b>重大示警</b>：${act.critical_flags.map(esc).join("；")}</div>` : ""}
      </div>

      <div class="card"><h3>基本與營運資料</h3>
        <div class="grid g3">
          <div class="kv">
            <div class="k">核定招收</div><div class="v">${fmt(inst.approved_capacity)} 人</div>
            <div class="k">實際招收</div><div class="v">${fmt(inst.enrolled)} 人（${pct(f.over_enroll)}）</div>
            <div class="k">班級數</div><div class="v">${fmt(inst.classes)} 班</div>
            <div class="k">教保人員數</div><div class="v">${fmt(inst.teacher_count)} 人</div>
            <div class="k">全園生師比（參考）</div><div class="v">${f.student_teacher != null ? fmt(f.student_teacher,1)+"：1" : "-"}</div>
          </div>
          <div class="kv">
            <div class="k">兩歲專班人數</div><div class="v">${fmt(inst.enrolled_age2 || 0)} 人</div>
            <div class="k">兩歲專班教保員</div><div class="v">${fmt(inst.teacher_count_age2 || 0)} 人</div>
            <div class="k">兩歲專班生師比</div><div class="v">${f.student_teacher_age2 != null ? fmt(f.student_teacher_age2,1)+"：1（法定上限 8：1）" : "無兩歲專班"}</div>
            <div class="k">三至五歲人數</div><div class="v">${fmt(inst.enrolled_age35 != null ? inst.enrolled_age35 : inst.enrolled)} 人</div>
            <div class="k">三至五歲生師比</div><div class="v">${f.student_teacher_age35 != null ? fmt(f.student_teacher_age35,1)+"：1（法定上限 15：1）" : "-"}</div>
          </div>
          <div class="kv">
            <div class="k">近一年人員異動</div><div class="v">${fmt(det.staff_changes_1y)} 人次</div>
            <div class="k">負責人跨園所數</div><div class="v">${fmt(f.principal_multi)}</div>
            <div class="k">地址</div><div class="v">${esc(inst.address || "-")}</div>
            <div class="k">位置</div><div class="v">${mapsLink(inst) || "-"}</div>
            <div class="k">電話</div><div class="v">${esc(inst.phone || "-")}</div>
          </div>
        </div>
      </div>

      <div class="grid g2">
        <div class="card"><h3>模型貢獻拆解（前10）</h3>
          <div class="hint">紅色使風險分數上升，綠色使其下降（相對於全體機構常態範圍的標準化貢獻）。</div>
          <div id="cContrib"></div>
        </div>
        <div class="card"><h3>財務比率同儕偏離</h3>
          <div class="hint">以同類型、同規模機構為同儕群組，穩健標準化 z 分數。</div>
          <div id="cPeer"></div>
        </div>
      </div>

      <div class="grid g2">
        <div class="card"><h3>決算收支趨勢</h3><div id="cFin"></div></div>
        <div class="card"><h3>決算科目首位數分布（同儕基準，參考指標）</h3>
          <div class="hint">${det.digit_conformity && det.digit_conformity.sufficient ? det.digit_conformity.basis : "樣本不足"}</div>
          <div id="cDigit"></div>
        </div>
      </div>

      <div class="grid g3">
        <div class="card"><h3>末兩位數均勻性檢定</h3>
          ${det.last_two && det.last_two.sufficient ? `
            <div class="kv">
              <div class="k">卡方值</div><div class="v">${fmt(det.last_two.chi2,2)}</div>
              <div class="k">p 值</div><div class="v">${det.last_two.p_value}</div>
              <div class="k">判定</div><div class="v">${det.last_two.level}</div>
            </div>
            <div class="hint" style="margin-top:8px">集中尾數：${(det.last_two.top_endings||[]).map(t=>`${t.ending}（${pct(t.share)}）`).join("、")}</div>
          ` : '<div class="empty">樣本不足</div>'}
        </div>
        <div class="card"><h3>金額整數偏誤</h3>
          ${det.round_bias && det.round_bias.sufficient ? `
            <div class="kv">
              <div class="k">千元整數占比</div><div class="v">${pct(det.round_bias.ratio_1000)}</div>
              <div class="k">判定</div><div class="v">${det.round_bias.level}</div>
            </div>` : '<div class="empty">樣本不足</div>'}
        </div>
        <div class="card"><h3>收費與決算交叉核對</h3>
          ${det.cross_check && det.cross_check.available ? `
            <div class="kv">
              <div class="k">推估收入</div><div class="v">${money(det.cross_check.estimated)}</div>
              <div class="k">決算收入</div><div class="v">${money(det.cross_check.reported)}</div>
              <div class="k">落差</div><div class="v">${pct(det.cross_check.gap_ratio)}</div>
            </div>
            <div class="hint" style="margin-top:8px">${det.cross_check.direction}</div>` : '<div class="empty">資料不足</div>'}
        </div>
      </div>

      <div class="grid g2">
        <div class="card"><h3>裁罰紀錄</h3>
          <div class="scroll sm"><table class="tbl">
            <thead><tr><th>日期</th><th>類別</th><th>法條</th><th class="num">罰鍰</th><th>處分</th></tr></thead>
            <tbody>${emptyIf(det.penalties, (det.penalties||[]).map(p => `
              <tr><td>${esc(p.penalty_date)}</td><td>${esc(p.category)}</td>
                <td>${esc(p.law_article)}</td><td class="num">${money(p.fine_amount)}</td>
                <td>${esc(p.disposition)}</td></tr>`).join(""))}</tbody>
          </table></div>
        </div>
        <div class="card"><h3>評鑑歷程</h3>
          <div class="scroll sm"><table class="tbl">
            <thead><tr><th>年度</th><th>結果</th><th class="num">待改善項目</th><th class="num">分數</th></tr></thead>
            <tbody>${emptyIf(det.evaluations, (det.evaluations||[]).map(e => `
              <tr><td>${esc(e.eval_year)}</td><td>${esc(e.result)}</td>
                <td class="num">${fmt(e.items_failed)}</td><td class="num">${fmt(e.score,1)}</td></tr>`).join(""))}</tbody>
          </table></div>
        </div>
      </div>

      <div class="card"><h3>社群輿情摘要</h3>
        <div class="kv">
          <div class="k">輿情風險分數</div><div class="v">${fmt(det.sentiment.score,1)}</div>
          <div class="k">負面貼文占比</div><div class="v">${pct(det.sentiment.neg_ratio)}（共 ${fmt(det.sentiment.n_posts)} 則）</div>
          <div class="k">爆量預警</div><div class="v">${det.sentiment.burst && det.sentiment.burst.burst ? "是（p=" + det.sentiment.burst.p_value + "）" : "否"}</div>
        </div>
        <div style="margin-top:10px">
          ${emptyIf(det.sentiment.recent_examples, (det.sentiment.recent_examples||[]).map(p => `
            <div class="post"><div class="h"><span>${esc(p.post_date)}</span><span>${esc(p.source)}</span>
              <span class="tag info">${esc(p.topic)}</span></div>
              <div class="c">${esc(p.content)}</div></div>`).join(""))}
        </div>
      </div>

      <div class="card"><h3>Claude AI agent 深度偵查</h3>
        <div class="hint">Claude 自主呼叫工具調閱本機構之財務、鑑識、生師比、裁罰與輿情資料後產出的鑑識研判，
          與上方統計／模型分數為互補的獨立意見。需於「設定」頁設定 API 金鑰並連線網路。</div>
        <div id="aiCard">${renderAiReport(d.ai_report, id)}</div>
      </div>
    `;

    window.KChart.radar("cRadar", {
      axes: ex.dimensions.map(x => x.label),
      series: [{ name: inst.name, values: ex.dimensions.map(x => x.score), color: "#38bdf8" }],
    });
    window.KChart.waterfall("cContrib", {
      items: (ex.model_contributions || []).map(c => ({ label: c.label, value: c.contribution })),
    });
    window.KChart.hbar("cPeer", {
      data: (det.peer_deviation || []).map(p => ({ label: p.label, value: p.score,
        note: `目前值 ${fmt(p.value,3)}／同儕中位數 ${fmt(p.peer_median,3)}（z=${p.z}，n=${p.peer_n}）` })),
      valueLabel: "偏離分數",
    });
    const fy = (det.ratio_series || []).map(r => r.fiscal_year);
    if (fy.length) {
      window.KChart.line("cFin", {
        series: [
          { name: "總收入", points: (det.ratio_series||[]).map(r => ({ x: r.fiscal_year, y: r.total_revenue || 0 })) },
          { name: "總支出", points: (det.ratio_series||[]).map(r => ({ x: r.fiscal_year, y: r.total_expense || 0 })) },
        ],
        xTicks: fy.map(y => ({ x: y, label: String(y) })),
        yFormat: v => fmt(v/10000,0) + "萬",
      });
    } else {
      $("#cFin").innerHTML = '<div class="empty">無決算資料</div>';
    }
    if (det.digit_conformity && det.digit_conformity.sufficient) {
      window.KChart.digitBars("cDigit", {
        observed: det.digit_conformity.observed_pct,
        expected: det.digit_conformity.expected_pct,
        labels: ["1","2","3","4","5","6","7","8","9"],
        obsLabel: "本機構", expLabel: "同儕基準",
      });
    } else {
      $("#cDigit").innerHTML = '<div class="empty">樣本不足</div>';
    }

    wireAiButton(id);
  }

  function wireAiButton(id) {
    const btn = $("#btnAiRun");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const statusEl = $("#aiStatus");
      if (statusEl) statusEl.innerHTML = '<span class="spin"></span>&nbsp;分析中，Claude 正在呼叫工具調閱資料，可能需 10~60 秒…';
      try {
        const r = await api("/ai/investigate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        });
        if (!r.ok) throw new Error(r.error || "分析失敗");
        $("#aiCard").innerHTML = renderAiReport(r.report, id);
        wireAiButton(id);
        toast("AI 深度偵查完成");
      } catch (e) {
        if (statusEl) statusEl.innerHTML = "";
        toast(e.message || "分析失敗", true);
        btn.disabled = false;
      }
    });
  }

  // ================================================================ 鑑識分析
  async function renderForensics() {
    const fo = await api("/forensics");
    main().innerHTML = `
      ${pageHead("鑑識會計分析", fo.methodology.why)}
      <div class="note"><b>整體層級</b>：${fo.methodology.portfolio_test}｜<b>機構層級</b>：${fo.methodology.institution_test}</div>
      <div class="grid g23">
        <div class="card"><h3>整體決算首位數分布（班佛定律）</h3>
          <div class="hint">樣本數 ${fo.overall_benford.n}，判定：${fo.overall_benford.level}（MAD=${fo.overall_benford.mad}）</div>
          <div id="cBenford"></div>
        </div>
        <div class="card"><h3>財務比率同儕分布（人事費率）</h3>
          <div class="hint">依設立類型分組之箱形圖，紅點為單一機構標記。</div>
          <div id="cBox"></div>
        </div>
      </div>
      <div class="card"><h3>人事費率 × 結餘率 散布圖</h3>
        <div class="hint">顏色深淺代表多維異常偵測分數（Isolation Forest + kNN 綜合）。</div>
        <div id="cScatter"></div>
      </div>
      <div class="grid g2">
        <div class="card"><h3>末兩位數群聚異常排行</h3>
          <div class="scroll sm"><table class="tbl">
            <thead><tr><th>機構</th><th>類型</th><th class="num">卡方</th><th class="num">最大集中度</th><th>判定</th></tr></thead>
            <tbody>${emptyIf(fo.last_two_offenders, fo.last_two_offenders.map(o => `
              <tr class="clickable" onclick="location.hash='#/institution/${esc(o.inst_id)}'">
                <td class="nm">${esc(o.name)}</td><td>${esc(o.org_type)}</td>
                <td class="num">${fmt(o.chi2,1)}</td><td class="num">${pct(o.max_share)}</td>
                <td>${esc(o.level)}</td></tr>`).join(""))}</tbody>
          </table></div>
        </div>
        <div class="card"><h3>收費與決算交叉核對落差排行</h3>
          <div id="cGaps"></div>
        </div>
      </div>
      <div class="grid g2">
        <div class="card"><h3>多維異常偵測排行</h3>
          <div class="scroll sm"><table class="tbl">
            <thead><tr><th>機構</th><th>類型</th><th class="num">綜合分數</th><th class="num">Isolation Forest</th><th class="num">kNN</th></tr></thead>
            <tbody>${emptyIf(fo.anomaly_top, fo.anomaly_top.map(o => `
              <tr class="clickable" onclick="location.hash='#/institution/${esc(o.inst_id)}'">
                <td class="nm">${esc(o.name)}</td><td>${esc(o.org_type)}</td>
                <td class="num">${fmt(o.combined,1)}</td><td class="num">${fmt(o.iforest_rank,1)}</td>
                <td class="num">${fmt(o.knn_rank,1)}</td></tr>`).join(""))}</tbody>
          </table></div>
        </div>
        <div class="card"><h3>首位數分布偏離同儕排行（參考指標）</h3>
          <div class="scroll sm"><table class="tbl">
            <thead><tr><th>機構</th><th class="num">MAD 倍數</th><th>判定</th></tr></thead>
            <tbody>${emptyIf(fo.first_digit_offenders, fo.first_digit_offenders.map(o => `
              <tr class="clickable" onclick="location.hash='#/institution/${esc(o.inst_id)}'">
                <td class="nm">${esc(o.name)}</td><td class="num">${fmt(o.adjusted_ratio,2)}</td>
                <td>${esc(o.level)}</td></tr>`).join(""))}</tbody>
          </table></div>
        </div>
      </div>
    `;
    window.KChart.digitBars("cBenford", {
      observed: fo.overall_benford.observed_pct, expected: fo.overall_benford.expected_pct,
      labels: ["1","2","3","4","5","6","7","8","9"],
    });
    const box = (fo.ratio_box || []).find(b => b.ratio === "personnel_ratio") || fo.ratio_box[0];
    if (box) {
      window.KChart.box("cBox", { groups: box.groups.map(g => ({ ...g, group: g.group })),
        yFormat: v => pct(v) });
    }
    window.KChart.scatter("cScatter", {
      points: fo.scatter.map(p => ({ x: p.x, y: p.y, label: p.name,
        color: p.anomaly > 60 ? "#e8590c" : "#38bdf8", opacity: 0.4 + Math.min(1, p.anomaly/100)*0.5 })),
      xLabel: "人事費率", yLabel: "結餘率", xFormat: pct, yFormat: pct,
    });
    window.KChart.hbar("cGaps", {
      data: (fo.cross_check_gaps || []).slice(0, 12).map(g => ({ label: g.name, value: (g.gap_ratio||0)*100,
        note: g.direction })), valueLabel: "落差", unit: "%", decimals: 1,
    });
  }

  // ================================================================ 輿情分析
  async function renderSentiment() {
    const so = await api("/sentiment");
    main().innerHTML = `
      ${pageHead("社群輿情分析", `共分析 ${so.n_analyzed} 則貼文，內建詞庫 ${so.lexicon_size} 詞；${so.bursts.length} 所機構出現爆量預警。`)}
      <div class="grid g2">
        <div class="card"><h3>風險主題分布</h3><div id="cTopics"></div></div>
        <div class="card"><h3>聲量時間趨勢</h3><div id="cTimeline"></div></div>
      </div>
      <div class="grid g23">
        <div class="card"><h3>關鍵詞</h3><div id="cWords"></div></div>
        <div class="card"><h3>來源可信度</h3>
          <table class="tbl"><thead><tr><th>來源</th><th class="num">總則數</th><th class="num">負面則數</th><th class="num">可信度加權</th></tr></thead>
          <tbody>${(so.sources||[]).map(s => `<tr><td>${esc(s.source)}</td><td class="num">${fmt(s.count)}</td>
            <td class="num">${fmt(s.negative)}</td><td class="num">${fmt(s.credibility,2)}</td></tr>`).join("")}</tbody></table>
        </div>
      </div>
      <div class="card"><h3>負面聲量最高機構</h3>
        <div class="scroll"><table class="tbl">
          <thead><tr><th>機構</th><th>縣市</th><th>類型</th><th class="num">輿情分數</th>
            <th class="num">負面則數</th><th>爆量</th><th>主要主題</th></tr></thead>
          <tbody>${emptyIf(so.top_negative, so.top_negative.map(t => `
            <tr class="clickable" onclick="location.hash='#/institution/${esc(t.inst_id)}'">
              <td class="nm">${esc(t.name)}</td><td>${esc(t.city)}</td><td>${esc(t.org_type)}</td>
              <td class="num">${fmt(t.score,1)}</td><td class="num">${fmt(t.n_negative)}</td>
              <td>${t.burst ? '<span class="tag critical">是</span>' : "否"}</td>
              <td>${(t.topics||[]).join("、")}</td></tr>`).join(""))}</tbody>
        </table></div>
      </div>
    `;
    window.KChart.bar("cTopics", {
      data: so.topics.map(t => ({ label: t.topic, value: t.count, note: `嚴重度 ${t.severity}` })),
      rotate: true, valueLabel: "則數",
    });
    window.KChart.line("cTimeline", {
      series: [
        { name: "總聲量", points: so.timeline.map((t,i) => ({ x: i, y: t.total, label: t.month, xLabel: t.month })) },
        { name: "負面聲量", points: so.timeline.map((t,i) => ({ x: i, y: t.negative, label: t.month, xLabel: t.month })) },
      ],
      xTicks: so.timeline.map((t,i) => i % 4 === 0 ? { x: i, label: t.month } : null).filter(Boolean),
      dots: false,
    });
    window.KChart.wordcloud("cWords", { words: so.keywords });
  }

  // ================================================================ 稽查排程
  async function renderSchedule() {
    async function load(cap) {
      const sc = await api("/schedule?capacity=" + cap);
      STATE.scheduleCapacity = cap;
      const e = sc.efficiency;
      main().innerHTML = `
        ${pageHead("稽查資源配置建議", "依風險分數排序並依地理位置併批，量化相較隨機／全面稽查的效益。",
          `<a class="btn" href="/api/export.xlsx">匯出 Excel</a>`)}
        <div class="filters">
          <label>稽查人力量能（可查核所數）</label>
          <input type="number" id="capInput" value="${cap}" min="1" style="width:100px">
          <button class="btn sm" id="capApply">重新計算</button>
        </div>
        <div class="grid g4">
          ${kpi("涵蓋率", pct(e.coverage_pct/100), "", "", `${e.capacity} / ${e.n_total} 所`)}
          <div class="kpi ok"><div class="k">命中效率倍數</div><div class="v">${fmt(e.multiplier,2)}×<span class="u">vs 隨機抽查</span></div>
            <div class="d">預估命中 ${e.model_hits} 所／隨機僅 ${e.random_hits} 所</div></div>
          ${kpi("預估總工時", fmt(e.estimated_hours,0), "小時", "", `全面稽查需 ${e.full_audit_hours} 小時`)}
          ${kpi("節省工時", pct(e.hours_saved_pct/100), "", "ok", `約省下 ${e.hours_saved} 小時；併批節省 ${e.trips_saved} 趟`)}
        </div>
        <div class="grid g2">
          ${sc.waves.map(w => `
            <div class="card"><h3>${w.name}</h3>
              <div class="hint">${w.note}｜期限 ${w.due}｜共 ${w.count} 所，預估 ${w.estimated_hours} 小時（${w.estimated_persondays} 人日）</div>
              <div class="scroll sm"><table class="tbl">
                <thead><tr><th>機構</th><th>縣市</th><th class="num">分數</th><th>模式</th></tr></thead>
                <tbody>${w.institutions.map(i => `
                  <tr class="clickable" onclick="location.hash='#/institution/${esc(i.inst_id)}'">
                    <td class="nm">${esc(i.name)}</td><td>${esc(i.city)}・${esc(i.district||"")}</td>
                    <td class="num">${fmt(i.score,1)}</td><td>${esc(i.mode)}</td></tr>`).join("")}</tbody>
              </table></div>
            </div>`).join("")}
        </div>
        <div class="card"><h3>地理併批建議（Top 30 行政區）</h3><div id="cRoutes"></div></div>
      `;
      window.KChart.hbar("cRoutes", {
        data: sc.routes.slice(0, 15).map(r => ({ label: `${r.city}${r.district}`, value: r.count,
          note: `平均分數 ${fmt(r.avg_score,1)}，可省 ${r.trips_saved} 趟` })),
        valueLabel: "機構數",
      });
      $("#capApply").addEventListener("click", () => {
        const v = Math.max(1, parseInt($("#capInput").value, 10) || 40);
        load(v);
      });
    }
    await load(STATE.scheduleCapacity);
  }

  // ================================================================ 模型驗證
  async function renderMetrics() {
    const m = await api("/metrics");
    const h = m.hybrid || {};
    const wc = m.weight_calibration || {};
    main().innerHTML = `
      ${pageHead("模型驗證與校準", `訓練基準日 ${m.train_as_of}，標籤觀察期 ${m.label_window}（共 ${m.n_train} 所，正例 ${h.positives} 所）`)}
      <div class="grid g4">
        ${kpi("混合模型 AUC", fmt(h.auc,3), "", "ok")}
        ${kpi("前20%精準率", pct(h.confusion && h.confusion.precision), "")}
        ${kpi("前20%召回率", pct(h.confusion && h.confusion.recall), "")}
        ${kpi("平均提前預警", fmt(m.lead_time && m.lead_time.mean_days,0), "天")}
      </div>
      <div class="grid g2">
        <div class="card"><h3>規則／模型混合比例敏感度</h3>
          <div class="hint">目前混合比例 ${m.current_blend}，建議比例 ${m.best_blend.blend}（AUC ${m.best_blend.auc}）</div>
          <div id="cBlend"></div>
        </div>
        <div class="card"><h3>各構面預測力（單構面 AUC）</h3><div id="cDimAuc"></div></div>
      </div>
      <div class="grid g23">
        <div class="card"><h3>權重校準建議</h3>
          <div class="hint">以歷史裁罰結果反推之建議權重；校準後樣本外 AUC ${wc.auc_oof}（目前規則 AUC ${wc.auc_current}）。</div>
          <div id="cWeights"></div>
          <button class="btn sm" id="btnApplyCal" style="margin-top:10px">套用校準權重</button>
        </div>
        <div class="card"><h3>三種評分方式比較</h3><div id="cCompare"></div></div>
      </div>
      <div class="card"><h3>特徵重要性（模型係數，前15）</h3><div id="cImportance"></div></div>
    `;
    window.KChart.line("cBlend", {
      series: [{ name: "AUC", points: m.blend_sweep.map(b => ({ x: b.blend, y: b.auc })) }],
      xTicks: m.blend_sweep.map(b => ({ x: b.blend, label: String(b.blend) })),
      decimals: 3,
    });
    window.KChart.bar("cDimAuc", {
      data: m.dimension_auc.map(d => ({ label: d.label, value: d.auc })), decimals: 3, showValue: true,
    });
    const dimKeys = Object.keys(wc.current_weights || {});
    window.KChart.bar("cWeights", {
      data: dimKeys.map(k => ({ label: wc.labels[k] || k, value: wc.weights[k],
        note: `目前 ${fmt(wc.current_weights[k],3)} → 建議 ${fmt(wc.weights[k],3)}` })),
      decimals: 3, showValue: true,
    });
    window.KChart.bar("cCompare", {
      data: [
        { label: "規則分數", value: (m.rule_only||{}).auc || 0 },
        { label: "模型分數", value: (m.model_only||{}).auc || 0 },
        { label: "混合分數", value: (m.hybrid||{}).auc || 0 },
      ], decimals: 3, showValue: true,
    });
    window.KChart.waterfall("cImportance", {
      items: (m.importance || []).slice(0, 15).map(it => ({ label: it.label, value: it.coef,
        note: `勝算比 ${it.odds_ratio}` })),
    });
    $("#btnApplyCal").addEventListener("click", async () => {
      await api("/apply-calibrated-weights", { method: "POST" });
      toast("已套用校準權重，正在重新計算分數…");
      location.hash = "#/settings";
    });
  }

  // ================================================================ 設定
  async function renderSettings() {
    const meta = await api("/meta");
    const s = meta.settings;
    const data = meta.data || {};
    main().innerHTML = `
      ${pageHead("系統設定", "調整權重與模型混合比例後即時重新計算分數；資料來源可切換本機檔案或 AWS 公開網址。")}
      <div class="grid g2">
        <div class="card"><h3>五大構面權重</h3>
          <div class="hint">數值愈高代表該構面對綜合分數影響愈大，總和不需為 1（系統會自動正規化）。</div>
          ${(meta.dimensions || []).map(d => `
            <div class="field">
              <label>${d.label}<span class="val" id="v_${d.key}">${fmt(s.weights[d.key],2)}</span></label>
              <input type="range" min="0" max="1" step="0.01" value="${s.weights[d.key]}" id="w_${d.key}">
              <div class="desc">${d.desc}</div>
            </div>`).join("")}
          <div class="field">
            <label>規則／模型混合比例<span class="val" id="v_blend">${fmt(s.model_blend,2)}</span></label>
            <input type="range" min="0" max="1" step="0.01" value="${s.model_blend}" id="w_blend">
            <div class="desc">0 = 完全採用規則分數；1 = 完全採用監督式模型分數。</div>
          </div>
          <div class="field">
            <label>稽查人力量能（所／期）</label>
            <input type="number" min="1" value="${s.inspection_capacity}" id="w_capacity" style="width:120px">
          </div>
          <button class="btn" id="btnSaveWeights">儲存並套用</button>
          <button class="btn ghost" id="btnResetWeights">重設為預設值</button>
        </div>
        <div class="card"><h3>資料來源</h3>
          <div class="hint">
            優先順序：<b>本機 data/ 資料夾</b> &gt; <b>AWS／遠端網址</b> &gt; 內建示範資料。
            檔名或欄位需與範本一致（可用下方「匯出目前資料為範本」取得標準欄位）。
          </div>
          <div class="field">
            <label>AWS 公開網址前綴（將讀取 &lt;網址&gt;/institutions.csv 等檔案；亦可在 data/manifest.json 個別指定各表網址）</label>
            <input type="text" id="w_aws" value="${esc(s.aws_base_url || "")}" placeholder="https://your-bucket.s3.ap-northeast-1.amazonaws.com/krews">
          </div>
          <button class="btn" id="btnReload">重新載入資料</button>
          <button class="btn ghost" id="btnExportTpl">匯出目前資料為 CSV 範本</button>
          <div id="tplResult" style="margin-top:10px"></div>
          <div class="kv" style="margin-top:16px">
            <div class="k">資料模式</div><div class="v">${data.mode === "demo" ? "內建示範資料" : "外部資料"}</div>
            <div class="k">載入時間</div><div class="v">${esc(data.loaded_at || "-")}</div>
          </div>
          <table class="tbl" style="margin-top:10px">
            <thead><tr><th>資料表</th><th>來源</th><th class="num">筆數</th></tr></thead>
            <tbody>${Object.keys(data.counts || {}).map(k => `
              <tr><td>${esc(k)}</td><td>${esc((data.sources||{})[k] || "-")}</td>
                <td class="num">${fmt((data.counts||{})[k])}</td></tr>`).join("")}</tbody>
          </table>
          ${(data.warnings && data.warnings.length) ? `<div class="note warn" style="margin-top:10px">${data.warnings.map(esc).join("<br>")}</div>` : ""}
        </div>
      </div>
      <div class="card"><h3>Claude AI agent（深度偵查，選用）</h3>
        <div class="hint">
          全體機構的排名／儀表板／稽查排程由左側統計與模型流程即時產生，<b>不需要</b>此處設定。
          此區塊是額外的深度偵查功能：針對單一機構，讓 Claude 自主呼叫工具調閱財務比率、
          同儕偏離、鑑識檢定、生師比、裁罰輿情等資料，產出自然語言鑑識報告。
          <b>需要網路連線與你自己的 Anthropic API 金鑰</b>，金鑰僅存於本機 settings.json，
          不會傳送至本應用程式以外的任何伺服器。
        </div>
        <div class="field">
          <label>Anthropic API 金鑰${meta.settings.anthropic_api_key_set ? '　<span class="tag low">已設定</span>' : '　<span class="tag info">尚未設定</span>'}</label>
          <input type="password" id="w_apikey" placeholder="sk-ant-…（留空並儲存代表不變更）" style="width:100%;max-width:420px">
        </div>
        <div class="field">
          <label>模型</label>
          <select id="w_model" style="width:220px">
            <option value="claude-sonnet-5">claude-sonnet-5（建議，品質較佳）</option>
            <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001（較快較省）</option>
          </select>
        </div>
        <button class="btn" id="btnSaveKey">儲存金鑰設定</button>
        <div style="margin-top:16px">
          <label>批次深度偵查</label>
          <div class="filters">
            <select id="w_ai_scope">
              <option value="top30">風險分數最高 30 所</option>
              <option value="top50">風險分數最高 50 所</option>
              <option value="all">全部機構（數量多時將耗時較久、費用較高）</option>
            </select>
            <button class="btn ghost" id="btnAiBatch">開始批次偵查</button>
          </div>
          <span class="hint">每所機構約需數次 API 呼叫；可隨時離開此頁，進度會持續在背景執行。</span>
          <div id="aiProgress" style="margin-top:10px"></div>
        </div>
      </div>
      <div class="note">
        <b>師生比法規依據</b>：依幼兒教育及照顧法第 16 條，兩歲以上未滿三歲之幼兒班級師生比上限為 8：1，
        三歲以上至入國民小學前之班級為 15：1，兩者須分別計算，不得以全園混合平均取代，
        以避免以其他班級人力稀釋兩歲專班之照顧比例。
      </div>
    `;
    (meta.dimensions || []).forEach(d => {
      $("#w_" + d.key).addEventListener("input", e => { $("#v_" + d.key).textContent = fmt(+e.target.value, 2); });
    });
    $("#w_blend").addEventListener("input", e => { $("#v_blend").textContent = fmt(+e.target.value, 2); });
    $("#btnSaveWeights").addEventListener("click", async () => {
      const weights = {};
      (meta.dimensions || []).forEach(d => { weights[d.key] = +$("#w_" + d.key).value; });
      await api("/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ weights, model_blend: +$("#w_blend").value,
          inspection_capacity: +$("#w_capacity").value }),
      });
      toast("設定已儲存並重新計算分數");
    });
    $("#btnResetWeights").addEventListener("click", async () => {
      await api("/reset-weights", { method: "POST" });
      toast("已重設為預設權重");
      location.hash = "#/settings"; location.reload();
    });
    $("#btnReload").addEventListener("click", async () => {
      await api("/reload", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ aws_base_url: $("#w_aws").value }),
      });
      toast("已送出重新載入請求，將於背景重新分析（可能需數秒到數十秒）…");
    });
    $("#btnExportTpl").addEventListener("click", async () => {
      const r = await api("/export-templates", { method: "POST" });
      if (r.ok) {
        $("#tplResult").innerHTML = `<div class="note">已匯出至 <b>${esc(r.dir)}</b>：${r.files.map(esc).join("、")}</div>`;
      } else {
        toast(r.error || "匯出失敗", true);
      }
    });
    $("#w_model").value = s.anthropic_model || "claude-sonnet-5";
    $("#btnSaveKey").addEventListener("click", async () => {
      const key = $("#w_apikey").value;
      const body = { anthropic_model: $("#w_model").value };
      if (key) body.anthropic_api_key = key;
      await api("/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      toast("已儲存 AI agent 設定");
      $("#w_apikey").value = "";
      location.hash = "#/settings"; location.reload();
    });
    $("#btnAiBatch").addEventListener("click", async () => {
      const scope = $("#w_ai_scope").value;
      const body = scope === "all" ? { ids: "all" } :
        { top_n: scope === "top50" ? 50 : 30 };
      const r = await api("/ai/batch", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      if (!r.ok) { toast(r.error || "無法啟動批次分析", true); return; }
      toast(`已啟動批次深度偵查（共 ${r.n} 所）`);
      pollAiProgress();
    });
    pollAiProgress();

    async function pollAiProgress() {
      const p = await api("/ai/status");
      const box = $("#aiProgress");
      if (!box) return;
      if (p.running) {
        const pct = p.total ? Math.round(p.done / p.total * 100) : 0;
        box.innerHTML = `<div class="mini-bar"><i style="width:${pct}%;background:#38bdf8"></i></div>
          <div class="hint" style="margin-top:6px">${esc(p.message || "")}（${p.done}/${p.total}）</div>`;
        setTimeout(pollAiProgress, 1500);
      } else if (p.total) {
        box.innerHTML = `<div class="hint">上次批次已完成（共 ${p.total} 所）${p.error ? "；部分失敗：" + esc(p.error) : ""}</div>`;
      } else {
        box.innerHTML = "";
      }
    }
  }

})();
