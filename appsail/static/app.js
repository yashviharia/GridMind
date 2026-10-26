/* GridMind dashboard. Vanilla JS + Chart.js; talks to the same-origin AppSail API. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const fmt = (n, d = 1) => (n == null || Number.isNaN(n) ? "—" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const hh = (ts) => ts.slice(11, 16);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  const C = {
    teal: css("--teal"), ren: css("--ren"), grid: css("--grid"), crit: css("--crit"), warn: css("--warn"),
    solar: css("--solar"), base: css("--base"), muted: css("--muted"), line: css("--line"),
  };

  const state = {
    result: null,
    scenarios: [],
    scenarioId: "heatwave",
    lastPayload: { scenario: "heatwave" },
    cursor: 0,
    timer: null,
    charts: {},
    status: null,
  };

  // ───────── API ─────────
  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    let body;
    try { body = await res.json(); } catch { body = { error: `HTTP ${res.status}` }; }
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    return body;
  }

  function batterySettings() {
    const out = {};
    $$("[data-bat]").forEach((el) => { if (el.value !== "") out[el.dataset.bat] = Number(el.value); });
    return out;
  }

  function offset() { return Number($("#tempOffset").value); }

  async function runForecast(payload) {
    state.lastPayload = payload;
    stopReplay();
    document.body.classList.add("loading");
    try {
      let result;
      if (payload.file) {
        const fd = new FormData();
        fd.append("file", payload.file);
        fd.append("battery", JSON.stringify(batterySettings()));
        fd.append("temp_offset_c", String(offset()));
        result = await api("/api/forecast", { method: "POST", body: fd });
      } else {
        result = await api("/api/forecast", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...payload, battery: batterySettings(), temp_offset_c: offset() }),
        });
      }
      state.result = result;
      state.cursor = defaultCursor(result);
      render();
      refreshLog();
    } catch (e) {
      showBanners([{ level: "error", title: "Forecast failed", detail: e.message }]);
    } finally {
      document.body.classList.remove("loading");
    }
  }

  function defaultCursor(result) {
    // Start on the hour with the highest demand: the most interesting moment.
    let best = 0;
    result.hours.forEach((h, i) => { if (h.demand_kw > result.hours[best].demand_kw) best = i; });
    return best;
  }

  // ───────── rendering ─────────
  function render() {
    const r = state.result;
    showBanners(r.alerts);
    renderModelBadge(r.model);
    renderKpis(r);
    renderLoadChart(r);
    renderTimeline(r);
    renderBatteryChart(r);
    renderSchedule(r);
    renderQuality(r.data_quality);
    $("#cursor").max = r.hours.length - 1;
    setCursor(state.cursor, { explain: true });
    $("#runId").textContent = r.run_id || "—";
    const s = r.summary;
    $("#tableNote").textContent = `${s.window_start} → ${s.window_end}${s.temp_offset_c ? ` · what-if ${s.temp_offset_c > 0 ? "+" : ""}${s.temp_offset_c} °C` : ""} · ${r.elapsed_ms} ms`;
  }

  function showBanners(alerts) {
    const icons = { critical: "⚡", warning: "▲", ok: "✓", error: "✕" };
    $("#banners").innerHTML = alerts.map((a) => `
      <div class="banner banner-${a.level}" role="${a.level === "critical" || a.level === "error" ? "alert" : "status"}">
        <span class="ico">${icons[a.level] || "•"}</span>
        <div><b>${esc(a.title)}</b>${esc(a.detail)}</div>
      </div>`).join("");
  }

  function renderModelBadge(model) {
    const b = $("#modelBadge");
    if (model.source === "quickml") {
      b.className = "pill pill-ok"; b.textContent = "● QuickML live"; b.title = "Forecasts served by the published QuickML endpoint";
    } else {
      b.className = "pill pill-warn"; b.textContent = "● local mirror model"; b.title = `QuickML not used: ${model.error || "unavailable"}`;
    }
  }

  function renderKpis(r) {
    const s = r.summary, b = s.battery;
    const kpis = [
      { k: "Peak demand", v: `${fmt(s.peak_demand_kw)} kW`, s: `at ${hh(s.peak_at)}`, cls: s.peak_demand_kw >= 200 ? "crit" : "" },
      { k: "Storage-mode hours", v: `${s.renewable_hours} / ${s.n_hours}`, s: `${r.transitions.length - 1} switch${r.transitions.length - 1 === 1 ? "" : "es"}` },
      { k: "Overload hours", v: s.overload_hours, s: s.at_risk_hours ? `+${s.at_risk_hours} at risk` : "none at risk", cls: s.overload_hours ? "crit" : s.at_risk_hours ? "warn" : "ok" },
      { k: "Battery low point", v: `${fmt(b.min_soc_pct, 0)}%`, s: b.shortfall_hours.length ? `${b.shortfall_hours.length} h on grid backup` : "covers all storage hours", cls: b.shortfall_hours.length ? "warn" : "ok" },
      { k: "Cost saved", v: `₹${fmt(b.cost_saving, 0)}`, s: `vs grid-only ₹${fmt(b.baseline_cost, 0)}`, cls: "ok" },
      { k: "CO₂ avoided", v: `${fmt(b.co2_saving_kg / 1000, 2)} t`, s: `${fmt(b.solar_kwh, 0)} kWh solar used`, cls: "ok" },
    ];
    $("#kpis").innerHTML = kpis.map((x) => `
      <div class="kpi ${x.cls || ""}"><div class="k">${x.k}</div><div class="v">${x.v}</div><div class="s">${x.s}</div></div>`).join("");
  }

  // Chart.js plugins: shade the 18:00-22:00 window and draw the replay cursor.
  const overlayPlugin = {
    id: "overlay",
    beforeDatasetsDraw(chart) {
      const hours = chart.config.options.plugins.overlay?.hours;
      if (!hours) return;
      const { ctx, chartArea: a, scales: { x } } = chart;
      const idx = hours.map((h, i) => (h.hour >= 18 && h.hour <= 22 ? i : -1)).filter((i) => i >= 0);
      ctx.save();
      ctx.fillStyle = "rgba(251,191,36,0.08)";
      // Group consecutive peak-window indices into spans.
      let start = null;
      idx.forEach((i, k) => {
        if (start === null) start = i;
        if (idx[k + 1] !== i + 1) {
          const half = (x.getPixelForValue(1) - x.getPixelForValue(0)) / 2 || 10;
          ctx.fillRect(x.getPixelForValue(start) - half, a.top, x.getPixelForValue(i) - x.getPixelForValue(start) + 2 * half, a.bottom - a.top);
          start = null;
        }
      });
      ctx.restore();
    },
    afterDatasetsDraw(chart) {
      const cur = chart.config.options.plugins.overlay?.cursor;
      if (cur == null) return;
      const { ctx, chartArea: a, scales: { x } } = chart;
      const px = x.getPixelForValue(cur);
      ctx.save();
      ctx.strokeStyle = "rgba(255,255,255,0.55)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
      ctx.restore();
    },
  };

  const baseOptions = () => ({
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 350 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: { backgroundColor: "#0c1322", borderColor: C.line, borderWidth: 1, titleColor: "#e6edf7", bodyColor: "#c7d2e4", padding: 10 },
    },
    scales: {
      x: { grid: { color: "rgba(30,44,71,.5)" }, ticks: { color: C.muted, font: { family: "JetBrains Mono", size: 10 }, maxRotation: 0, autoSkipPadding: 8 } },
      y: { grid: { color: "rgba(30,44,71,.5)" }, ticks: { color: C.muted, font: { family: "JetBrains Mono", size: 10 } } },
    },
    onClick: (evt, els, chart) => {
      const pts = chart.getElementsAtEventForMode(evt, "index", { intersect: false }, false);
      if (pts.length) { stopReplay(); setCursor(pts[0].index, { explain: true }); }
    },
  });

  function renderLoadChart(r) {
    const labels = r.hours.map((h) => hh(h.timestamp));
    const flat = (v) => r.hours.map(() => v);
    const data = {
      labels,
      datasets: [
        { label: "90% upper", data: r.hours.map((h) => h.demand_hi_kw), borderWidth: 0, pointRadius: 0, fill: "+1", backgroundColor: "rgba(45,212,191,0.14)" },
        { label: "90% lower", data: r.hours.map((h) => h.demand_lo_kw), borderWidth: 0, pointRadius: 0, fill: false },
        {
          label: "Forecast", data: r.hours.map((h) => h.demand_kw), borderColor: C.teal, borderWidth: 2.5, tension: 0.3,
          pointRadius: r.hours.map((h) => (h.overload_alert ? 4 : 2)),
          pointBackgroundColor: r.hours.map((h) => (h.overload_alert ? C.crit : C.teal)), pointBorderWidth: 0,
        },
        { label: "Base load", data: r.hours.map((h) => h.base_load_kw), borderColor: C.base, borderWidth: 1.5, borderDash: [2, 3], pointRadius: 0, tension: 0.3 },
        { label: "160 kW switch", data: flat(160), borderColor: C.grid, borderWidth: 1, borderDash: [6, 4], pointRadius: 0 },
        { label: "200 kW overload", data: flat(200), borderColor: C.crit, borderWidth: 1, borderDash: [6, 4], pointRadius: 0 },
      ],
    };
    const opts = baseOptions();
    opts.plugins.overlay = { hours: r.hours, cursor: state.cursor };
    opts.scales.y.title = { display: true, text: "kW", color: C.muted };
    opts.scales.y.suggestedMin = 0;
    opts.plugins.tooltip.filter = (item) => !item.dataset.label.startsWith("90%");
    opts.plugins.tooltip.callbacks = {
      label: (item) => `${item.dataset.label}: ${fmt(item.parsed.y)} kW`,
      afterBody: (items) => {
        const h = r.hours[items[0].dataIndex];
        return [`90% range: ${fmt(h.demand_lo_kw)}–${fmt(h.demand_hi_kw)} kW`, `P(≥200 kW): ${Math.round(h.p_overload * 100)}%`, `Mode: ${h.grid_mode === "RENEWABLE_STORAGE" ? "Renewable storage" : "Main grid"}`];
      },
    };
    upsertChart("loadChart", "line", data, opts);
  }

  function renderBatteryChart(r) {
    const labels = r.hours.map((h) => hh(h.timestamp));
    const data = {
      labels,
      datasets: [
        { type: "line", label: "State of charge %", data: r.hours.map((h) => h.soc_pct), borderColor: C.ren, backgroundColor: "rgba(52,211,153,.12)", fill: true, yAxisID: "soc", tension: 0.3, pointRadius: 0, borderWidth: 2 },
        { type: "bar", label: "Battery discharge kW", data: r.hours.map((h) => h.battery_kw), backgroundColor: "rgba(52,211,153,.55)", stack: "s", yAxisID: "kw" },
        { type: "bar", label: "Solar kW", data: r.hours.map((h) => Math.min(h.solar_kw, h.demand_kw)), backgroundColor: "rgba(250,204,21,.6)", stack: "s", yAxisID: "kw" },
        { type: "bar", label: "Grid import kW", data: r.hours.map((h) => h.grid_kw), backgroundColor: r.hours.map((h) => (h.battery_shortfall ? "rgba(248,113,113,.65)" : "rgba(96,165,250,.45)")), stack: "s", yAxisID: "kw" },
      ],
    };
    const opts = baseOptions();
    opts.plugins.overlay = { hours: r.hours, cursor: state.cursor };
    opts.plugins.legend = { display: true, labels: { color: C.muted, boxWidth: 10, font: { size: 11 } } };
    opts.scales = {
      x: { ...opts.scales.x, stacked: true },
      kw: { position: "left", stacked: true, grid: { color: "rgba(30,44,71,.5)" }, ticks: { color: C.muted, font: { size: 10 } }, title: { display: true, text: "kW", color: C.muted } },
      soc: { position: "right", min: 0, max: 100, grid: { display: false }, ticks: { color: C.ren, font: { size: 10 }, callback: (v) => `${v}%` } },
    };
    upsertChart("batteryChart", "bar", data, opts);
    const b = r.summary.battery;
    $("#batteryNote").textContent = b.recommended_capacity_kwh
      ? (b.shortfall_hours.length ? `needs ≈${fmt(b.recommended_capacity_kwh, 0)} kWh to cover all storage hours` : `smallest battery that works: ≈${fmt(b.recommended_capacity_kwh, 0)} kWh`)
      : "";
  }

  function upsertChart(id, type, data, options) {
    const existing = state.charts[id];
    if (existing) {
      existing.data = data;
      existing.options = options;
      existing.update();
      return existing;
    }
    state.charts[id] = new Chart($(`#${id}`), { type, data, options, plugins: [overlayPlugin] });
    return state.charts[id];
  }

  function renderTimeline(r) {
    const switches = new Set(r.transitions.slice(1).map((t) => t.timestamp));
    $("#timeline").innerHTML = r.hours.map((h, i) => `
      <div class="tl-cell ${h.grid_mode === "RENEWABLE_STORAGE" ? "ren" : "grid"} ${h.overload_alert ? "over" : ""} ${switches.has(h.timestamp) ? "switch" : ""}"
           data-i="${i}" title="${esc(h.timestamp)} · ${fmt(h.demand_kw)} kW · ${h.grid_mode === "RENEWABLE_STORAGE" ? "Renewable storage" : "Main grid"}${h.overload_alert ? " · OVERLOAD" : ""}">${hh(h.timestamp).slice(0, 2)}</div>`).join("");
  }

  function renderSchedule(r) {
    $("#schedule tbody").innerHTML = r.hours.map((h, i) => {
      const ren = h.grid_mode === "RENEWABLE_STORAGE";
      const risk = Math.round(h.p_overload * 100);
      const alert = h.overload_alert ? '<span class="tag tag-crit">CRITICAL</span>' : risk >= 20 ? '<span class="tag tag-warn">at risk</span>' : "";
      return `<tr data-i="${i}">
        <td class="num">${esc(h.timestamp.slice(5, 16))}</td>
        <td class="num">${fmt(h.ambient_temp_c)}°</td>
        <td class="num">${fmt(h.solar_irradiance_wm2, 0)}</td>
        <td class="num"><b>${fmt(h.demand_kw)}</b></td>
        <td class="num muted">${fmt(h.demand_lo_kw, 0)}–${fmt(h.demand_hi_kw, 0)}</td>
        <td class="num"><span class="riskbar"><i style="width:${risk}%"></i></span>${risk}%</td>
        <td><span class="tag ${ren ? "tag-ren" : "tag-grid"}">${ren ? "RENEWABLE" : "MAIN GRID"}</span></td>
        <td>${alert}</td>
        <td class="num">${fmt(h.soc_pct, 0)}%${h.battery_shortfall ? ' <span class="tag tag-warn">backup</span>' : ""}</td>
        <td class="reason">${esc(h.reason)}</td>
      </tr>`;
    }).join("");
  }

  function renderQuality(dq) {
    const issues = dq.issues;
    $("#dqNote").textContent = `${dq.n_input_rows} rows in → ${dq.n_clean_rows} clean`;
    $("#dqList").innerHTML = issues.length
      ? issues.map((i) => `<li><span class="act">${esc(i.action)}</span><span>${i.row ? `row ${i.row} · ` : ""}<b>${esc(i.field)}</b> ${esc(i.detail)}</span></li>`).join("")
      : '<li><span class="act" style="color:var(--ren)">clean</span><span>All rows valid: no missing values, gaps, duplicates or out-of-range readings.</span></li>';
  }

  // ───────── live cursor / replay ─────────
  function setCursor(i, { explain = false } = {}) {
    const r = state.result;
    if (!r) return;
    i = Math.max(0, Math.min(r.hours.length - 1, i));
    state.cursor = i;
    const h = r.hours[i];
    const ren = h.grid_mode === "RENEWABLE_STORAGE";
    $("#cursor").value = i;
    $("#liveTime").textContent = h.timestamp.slice(0, 16);
    const badge = $("#liveMode");
    badge.className = `mode-badge ${ren ? "mode-ren" : "mode-grid"} ${h.overload_alert ? "overload" : ""}`;
    badge.textContent = ren ? "RENEWABLE STORAGE" : "MAIN GRID";
    $("#liveReason").textContent = (h.overload_alert ? "⚠ Critical overload · " : "") + h.reason;
    $("#liveDemand").textContent = `${fmt(h.demand_kw)} kW`;
    $("#liveRange").textContent = `${fmt(h.demand_lo_kw, 0)}–${fmt(h.demand_hi_kw, 0)} kW`;
    const risk = Math.round(h.p_overload * 100);
    $("#liveRisk").textContent = `${risk}%`;
    $("#liveRisk").style.color = risk >= 50 ? C.crit : risk >= 20 ? C.warn : C.ren;
    const solarUsed = Math.min(h.solar_kw, h.demand_kw);
    $("#liveMix").textContent = `☀ ${fmt(solarUsed, 0)} · 🔋 ${fmt(h.battery_kw, 0)} · ⚡grid ${fmt(h.grid_kw, 0)} kW`;
    const arc = $("#socArc");
    arc.style.strokeDashoffset = String(314.16 * (1 - h.soc_pct / 100));
    arc.style.stroke = h.soc_pct < 20 ? C.crit : h.soc_pct < 40 ? C.warn : C.ren;
    $("#socValue").textContent = `${fmt(h.soc_pct, 0)}%`;

    $$(".tl-cell").forEach((el) => el.classList.toggle("cursor", Number(el.dataset.i) === i));
    $$("#schedule tbody tr").forEach((el) => el.classList.toggle("cursor", Number(el.dataset.i) === i));
    for (const id of ["loadChart", "batteryChart"]) {
      const ch = state.charts[id];
      if (ch) { ch.options.plugins.overlay.cursor = i; ch.update("none"); }
    }
    if (explain) explainHour(h);
  }

  function startReplay() {
    if (!state.result) return;
    stopReplay();
    if (state.cursor >= state.result.hours.length - 1) setCursor(0);
    $("#playBtn").textContent = "❚❚ Pause";
    state.timer = setInterval(() => {
      if (state.cursor >= state.result.hours.length - 1) { stopReplay(); return; }
      setCursor(state.cursor + 1);
    }, 900);
  }

  function stopReplay() {
    clearInterval(state.timer);
    state.timer = null;
    $("#playBtn").textContent = "▶ Replay day";
  }

  // ───────── explain ─────────
  let explainSeq = 0;
  async function explainHour(h) {
    const seq = ++explainSeq;
    const total = h.demand_kw;
    const base = Math.max(0, h.base_load_kw);
    const weather = Math.max(0, total - base);
    const pct = (v) => (total > 0 ? (100 * v) / total : 0);
    const box = $("#explain");
    box.innerHTML = `
      <div class="headline">${esc(h.timestamp.slice(0, 16))}: <b>${fmt(total)} kW</b> ${h.overload_alert ? '<span class="tag tag-crit">overload</span>' : ""}</div>
      <div class="stack">
        <div style="width:${pct(base)}%;background:${C.base}">base ${fmt(base, 0)}</div>
        <div style="width:${pct(weather)}%;background:${C.warn}">${weather > 8 ? `weather ${fmt(weather, 0)}` : ""}</div>
      </div>
      <div class="muted small">${weather > 1
        ? `${fmt(h.ambient_temp_c)} °C is ${fmt(h.ambient_temp_c - 24)} °C above the 24 °C cooling threshold, adding about ${fmt(weather, 0)} kW of air-conditioning load.`
        : `${fmt(h.ambient_temp_c)} °C is below the 24 °C cooling threshold, so this is almost entirely routine ${h.hour >= 18 && h.hour <= 22 ? "evening-peak " : ""}demand for ${hh(h.timestamp)}.`}</div>
      <div class="contrib" id="contrib"><span class="muted small">loading feature contributions…</span></div>`;
    try {
      const ex = await api("/api/explain", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timestamp: h.timestamp, ambient_temp_c: h.ambient_temp_c, humidity_pct: h.humidity_pct, solar_irradiance_wm2: h.solar_irradiance_wm2 }),
      });
      if (seq !== explainSeq) return;
      let rows = ex.linear_contributions.filter((c) => Math.abs(c.contribution_kw) >= 0.5);
      let title = "Feature contributions (linear model, kW)";
      const shap = ex.quickml?.explanation?.data;
      if (Array.isArray(shap) && shap.length) {
        rows = shap.map(([feature, value, contribution]) => ({ feature, value, contribution_kw: Number(contribution) })).sort((a, b) => Math.abs(b.contribution_kw) - Math.abs(a.contribution_kw));
        title = "QuickML SHAP attributions";
      }
      const max = Math.max(...rows.map((c) => Math.abs(c.contribution_kw)), 1);
      $("#contrib").innerHTML = `<div class="muted small">${title}</div>` + rows.slice(0, 7).map((c) => {
        const w = (50 * Math.abs(c.contribution_kw)) / max;
        const pos = c.contribution_kw >= 0;
        return `<div class="contrib-row"><span class="name" title="${esc(c.feature)} = ${esc(c.value)}">${esc(c.feature)}</span>
          <span class="bar"><i style="left:${pos ? 50 : 50 - w}%;width:${w}%;background:${pos ? C.warn : C.grid}"></i></span>
          <span class="val">${pos ? "+" : ""}${fmt(c.contribution_kw)}</span></div>`;
      }).join("");
    } catch (e) {
      if (seq === explainSeq) $("#contrib").innerHTML = `<span class="muted small">Explanation unavailable: ${esc(e.message)}</span>`;
    }
  }

  // ───────── log & model card ─────────
  async function refreshLog() {
    try {
      const h = await api("/api/history");
      $("#logList").innerHTML = h.events.slice(0, 60).map((e) => `
        <li><span class="t">${esc(new Date(e.time).toLocaleTimeString("en-IN", { hour12: false }))}</span><span class="lvl lvl-${esc(e.level)}">${esc(e.level)}</span><span>${esc(e.message)}</span></li>`).join("") || '<li class="muted">No events yet.</li>';
    } catch { /* log is non-critical */ }
  }

  function renderModelCard(status) {
    const rep = status.model_report;
    // The starter dummy (~46 kW) would flatten the chart; it is quoted in the text instead.
    const names = Object.keys(rep.cv_rmse).filter((n) => !n.startsWith("starter"));
    const vals = names.map((n) => rep.cv_rmse[n].mean);
    const best = Math.min(...vals);
    upsertChart("modelChart", "bar", {
      labels: names,
      datasets: [{ label: "CV RMSE (kW)", data: vals, backgroundColor: vals.map((v) => (v === best ? C.teal : "rgba(135,150,176,.35)")), borderRadius: 6 }],
    }, {
      ...baseOptions(),
      onClick: null,
      indexAxis: "y",
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (i) => `RMSE ${fmt(i.parsed.x, 2)} kW` } } },
      scales: {
        x: { min: 0, grid: { color: "rgba(30,44,71,.5)" }, ticks: { color: C.muted }, title: { display: true, text: "RMSE (kW), lower is better", color: C.muted } },
        y: { grid: { display: false }, ticks: { color: "#c7d2e4", font: { size: 11 }, autoSkip: false } },
      },
    });
    const naive = rep.cv_rmse["default boosting (raw 4 cols)"]?.mean;
    const dummy = rep.cv_rmse["starter-kit dummy formula"]?.mean;
    $("#modelText").innerHTML = `
      <div><b>Linear regression on ${rep.features.length} physics-informed features</b> beats tree ensembles on held-out future data: <b>${fmt(best, 2)} kW</b> RMSE${naive ? `, ${Math.round((100 * (naive - best)) / naive)}% lower than out-of-the-box boosting (${fmt(naive, 2)} kW)` : ""}${dummy ? ` and ${Math.round((100 * (dummy - best)) / dummy)}% lower than the starter kit's baseline formula (${fmt(dummy, 1)} kW)` : ""}.</div>
      <div>Demand = hourly base profile + cooling load that switches on above <b>24 °C</b>, with a separate slope for night, morning, daytime and evening. Linear terms extrapolate cleanly into hotter weather, where trees flatten out.</div>
      <div>Uncertainty: 90% of held-out errors fall in <b>${fmt(rep.interval_90[0])} to +${fmt(rep.interval_90[1])} kW</b>; the same error distribution gives each hour's overload probability.</div>
      <div>Features: ${rep.features.map((f) => `<code>${esc(f)}</code>`).join(" ")}</div>`;
  }

  // ───────── inputs ─────────
  function renderScenarios() {
    $("#scenarioList").innerHTML = state.scenarios.map((s) => `
      <button class="scenario ${s.id === state.scenarioId ? "active" : ""}" data-id="${esc(s.id)}">
        <span>${esc(s.label)}<small>${esc(s.date)} · ${s.n_rows} h</small></span>
        <span class="temp">${fmt(s.max_temp_c)}°C max</span>
      </button>`).join("");
  }

  function buildManualTable(rows) {
    const start = rows?.[0]?.timestamp ? rows[0].timestamp.replace(" ", "T").slice(0, 16) : $("#manualStart").value || "2015-07-01T00:00";
    $("#manualStart").value = start;
    const t0 = new Date(start + ":00Z");
    $("#manualTable tbody").innerHTML = Array.from({ length: 24 }, (_, i) => {
      const r = rows?.[i] || {};
      const t = new Date(t0.getTime() + i * 3600e3).toISOString();
      return `<tr><td>${t.slice(11, 16)}</td>
        <td><input data-col="ambient_temp_c" inputmode="decimal" value="${esc(r.ambient_temp_c ?? "")}" aria-label="temperature ${t.slice(11, 16)}"></td>
        <td><input data-col="humidity_pct" inputmode="decimal" value="${esc(r.humidity_pct ?? "")}" aria-label="humidity ${t.slice(11, 16)}"></td>
        <td><input data-col="solar_irradiance_wm2" inputmode="decimal" value="${esc(r.solar_irradiance_wm2 ?? "")}" aria-label="solar ${t.slice(11, 16)}"></td></tr>`;
    }).join("");
  }

  function manualRows() {
    const t0 = new Date($("#manualStart").value + ":00Z");
    return $$("#manualTable tbody tr").map((tr, i) => {
      const t = new Date(t0.getTime() + i * 3600e3).toISOString();
      const row = { timestamp: `${t.slice(0, 10)} ${t.slice(11, 19)}` };
      $$("input", tr).forEach((inp) => { row[inp.dataset.col] = inp.value; });
      return row;
    });
  }

  async function prefillManual() {
    const s = await api(`/api/scenarios/${state.scenarioId}`);
    buildManualTable(s.rows);
  }

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 2600);
  }

  function bind() {
    $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $$(".tabpane").forEach((p) => { p.hidden = p.dataset.pane !== tab.dataset.tab; });
      if (tab.dataset.tab === "manual" && !$("#manualTable tbody").children.length) prefillManual();
    }));

    $("#scenarioList").addEventListener("click", (e) => {
      const btn = e.target.closest(".scenario");
      if (!btn) return;
      state.scenarioId = btn.dataset.id;
      renderScenarios();
      runForecast({ scenario: state.scenarioId });
    });

    const dz = $("#dropzone"), fi = $("#fileInput");
    fi.addEventListener("change", () => { if (fi.files[0]) { runForecast({ file: fi.files[0] }); toast(`Uploaded ${fi.files[0].name}`); } });
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) { runForecast({ file: f }); toast(`Uploaded ${f.name}`); } });

    $("#manualFill").addEventListener("click", prefillManual);
    $("#manualStart").addEventListener("change", () => buildManualTable($$("#manualTable tbody tr").map((tr) => Object.fromEntries($$("input", tr).map((i) => [i.dataset.col, i.value])))));
    $("#manualRun").addEventListener("click", () => runForecast({ rows: manualRows() }));

    let debounce;
    $("#tempOffset").addEventListener("input", () => {
      const v = offset();
      $("#offsetLabel").textContent = `${v >= 0 ? "+" : ""}${v.toFixed(1)} °C`;
      clearTimeout(debounce);
      if (state.lastPayload.file) return; // re-uploading on every tick would spam storage
      debounce = setTimeout(() => runForecast(state.lastPayload), 300);
    });
    $("#tempOffset").addEventListener("change", () => { if (state.lastPayload.file) runForecast(state.lastPayload); });
    $("#applyBattery").addEventListener("click", () => runForecast(state.lastPayload));

    $("#playBtn").addEventListener("click", () => (state.timer ? stopReplay() : startReplay()));
    $("#cursor").addEventListener("input", (e) => { stopReplay(); setCursor(Number(e.target.value)); });
    $("#cursor").addEventListener("change", (e) => setCursor(Number(e.target.value), { explain: true }));
    $("#timeline").addEventListener("click", (e) => { const c = e.target.closest(".tl-cell"); if (c) { stopReplay(); setCursor(Number(c.dataset.i), { explain: true }); } });
    $("#schedule tbody").addEventListener("click", (e) => { const tr = e.target.closest("tr"); if (tr) { stopReplay(); setCursor(Number(tr.dataset.i), { explain: true }); } });
    $("#refreshLog").addEventListener("click", refreshLog);
    document.addEventListener("keydown", (e) => {
      if (!state.result || e.target.closest("input, textarea")) return;
      if (e.key === "ArrowRight") { stopReplay(); setCursor(state.cursor + 1, { explain: true }); }
      if (e.key === "ArrowLeft") { stopReplay(); setCursor(state.cursor - 1, { explain: true }); }
      if (e.key === " ") { e.preventDefault(); state.timer ? stopReplay() : startReplay(); }
    });
  }

  function tickClock() {
    $("#clock").textContent = new Date().toLocaleString("en-IN", { hour12: false, dateStyle: "medium", timeStyle: "medium" });
  }

  async function init() {
    bind();
    tickClock(); setInterval(tickClock, 1000);
    try {
      const [status, scenarios] = await Promise.all([api("/api/status"), api("/api/scenarios")]);
      state.status = status;
      state.scenarios = scenarios;
      $("#storageBadge").textContent = `storage: ${status.storage.split(" ")[0]}`;
      renderScenarios();
      renderModelCard(status);
      runForecast({ scenario: state.scenarioId });
    } catch (e) {
      showBanners([{ level: "error", title: "Backend unreachable", detail: e.message }]);
    }
  }

  init();
})();
