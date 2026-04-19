// Trading_Plug&Play — minimal vanilla-JS UI.

(() => {
  const $ = (id) => document.getElementById(id);

  const METRICS_TAB = "__metrics__";
  const REPLAY_TAB = "__replay__";

  const state = {
    indices: [],
    current: null,
    ws: null,
    reconnectTimer: null,
    expiry: null,
    chain: {},   // { strike: { CE: {tick, greeks}, PE: {tick, greeks} } }
    atm: null,
    metricsTimer: null,
    replayCurrentId: null,
  };

  async function init() {
    try {
      const res = await fetch("/api/indices");
      state.indices = await res.json();
    } catch (e) {
      state.indices = ["NIFTY50", "BANKNIFTY", "SENSEX"];
    }
    renderTabs();
    $("expiry").value = defaultExpiryISO();
    $("load-chain").addEventListener("click", loadChain);
    $("replay-refresh").addEventListener("click", loadReplayList);
    if (state.indices.length) switchTab(state.indices[0]);
  }

  function defaultExpiryISO() {
    const d = new Date();
    const dayMs = 86_400_000;
    const delta = ((4 - d.getDay()) + 7) % 7 || 7;   // 4 = Thursday
    return new Date(d.getTime() + delta * dayMs).toISOString().slice(0, 10);
  }

  function renderTabs() {
    const tabs = $("tabs");
    tabs.innerHTML = "";
    const buttons = [
      ...state.indices.map((idx) => [idx, idx]),
      [METRICS_TAB, "Metrics"],
      [REPLAY_TAB, "Replay"],
    ];
    for (const [key, label] of buttons) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = label;
      btn.addEventListener("click", () => switchTab(key));
      if (key === state.current) btn.classList.add("active");
      tabs.appendChild(btn);
    }
  }

  async function switchTab(key) {
    // Tear down everything the previous tab owned.
    if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    if (state.metricsTimer) { clearInterval(state.metricsTimer); state.metricsTimer = null; }

    state.current = key;
    renderTabs();
    showView(key);

    if (key === METRICS_TAB) {
      startMetricsPolling();
      return;
    }
    if (key === REPLAY_TAB) {
      loadReplayList();
      return;
    }
    // index tab
    state.chain = {};
    $("chain-tbody").innerHTML = "";
    $("candles").innerHTML = "";
    $("signals").innerHTML = "";
    $("spot").textContent = "—";
    $("last-seen").textContent = "—";
    await fetchState(key);
    await loadChain();
    connectWs(key);
  }

  function showView(key) {
    const live = $("view-live");
    const metrics = $("view-metrics");
    const replay = $("view-replay");
    live.hidden = true; metrics.hidden = true; replay.hidden = true;
    if (key === METRICS_TAB) metrics.hidden = false;
    else if (key === REPLAY_TAB) replay.hidden = false;
    else live.hidden = false;
  }

  async function fetchState(idx) {
    try {
      const s = await fetch(`/api/state/${idx}`).then((r) => r.json());
      if (typeof s.spot === "number") $("spot").textContent = s.spot.toFixed(2);
      if (s.atm) state.atm = s.atm;
      if (s.last_seen_ms) $("last-seen").textContent = fmtTime(s.last_seen_ms);
    } catch (e) { /* redis may be cold */ }
  }

  async function loadChain() {
    const idx = state.current;
    const expiry = $("expiry").value;
    if (!idx || !expiry) return;
    state.expiry = expiry;
    try {
      const res = await fetch(`/api/chain/${idx}?expiry=${encodeURIComponent(expiry)}`);
      if (!res.ok) return;
      const body = await res.json();
      state.chain = {};
      for (const [strike, row] of Object.entries(body.strikes || {})) {
        state.chain[+strike] = { CE: row.CE || {}, PE: row.PE || {} };
      }
      renderChain();
    } catch (e) { /* nop */ }
  }

  function connectWs(idx) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/${idx}`);
    state.ws = ws;
    setWsStatus("connecting…", false);
    ws.onopen = () => setWsStatus("connected", true);
    ws.onclose = () => {
      setWsStatus("disconnected", false);
      if (state.current === idx) {
        state.reconnectTimer = setTimeout(() => connectWs(idx), 2000);
      }
    };
    ws.onerror = () => setWsStatus("error", false);
    ws.onmessage = (ev) => {
      let parsed;
      try { parsed = JSON.parse(ev.data); } catch (e) { return; }
      const { channel, data } = parsed;
      if (!channel) return;
      if (channel.startsWith("ticks.index.")) onIndexTick(data);
      else if (channel.startsWith("ticks.option.")) onOptionTick(data);
      else if (channel.startsWith("candles.")) onCandle(channel, data);
      else if (channel.startsWith("greeks.")) onGreeks(data);
      else if (channel.startsWith("signals.")) onSignal(data);
    };
  }

  function setWsStatus(text, connected) {
    const el = $("ws-status");
    el.textContent = text;
    el.classList.toggle("connected", connected);
    el.classList.toggle("disconnected", !connected);
  }

  function onIndexTick(tick) {
    if (!tick || typeof tick.ltp !== "number") return;
    $("spot").textContent = tick.ltp.toFixed(2);
    if (tick.ts_exchange) $("last-seen").textContent = fmtTime(tick.ts_exchange);
  }

  function onOptionTick(tick) {
    if (!tick || !tick.strike) return;
    const row = state.chain[tick.strike] ||= { CE: {}, PE: {} };
    row[tick.option_type] ||= {};
    row[tick.option_type].tick = tick;
    scheduleRender();
  }

  function onGreeks(g) {
    if (!g || !g.strike) return;
    const row = state.chain[g.strike] ||= { CE: {}, PE: {} };
    row[g.option_type] ||= {};
    row[g.option_type].greeks = g;
    scheduleRender();
  }

  function onCandle(channel, candle) {
    const tf = channel.split(".").pop();
    if (tf !== "1m") return;
    const ts = candle.close_ts ? fmtTime(candle.close_ts) : "";
    const li = document.createElement("li");
    li.textContent = `${ts}  O${candle.open}  H${candle.high}  L${candle.low}  C${candle.close}  (${candle.tick_count})`;
    $("candles").prepend(li);
    const MAX = 40;
    while ($("candles").children.length > MAX) $("candles").lastElementChild.remove();
  }

  function onSignal(sig) {
    const li = document.createElement("li");
    li.className = `sig ${sig.action}`;
    const t = sig.ts ? fmtTime(sig.ts) : "";
    li.textContent = `${t}  ${sig.strategy} · ${sig.action} ${sig.instrument} (c=${(sig.confidence ?? 0).toFixed(2)})  ${sig.reason ?? ""}`;
    $("signals").prepend(li);
    const MAX = 40;
    while ($("signals").children.length > MAX) $("signals").lastElementChild.remove();
  }

  let renderPending = false;
  function scheduleRender() {
    if (renderPending) return;
    renderPending = true;
    requestAnimationFrame(() => { renderPending = false; renderChain(); });
  }

  function renderChain() {
    const tbody = $("chain-tbody");
    const strikes = Object.keys(state.chain).map(Number).sort((a, b) => a - b);
    const rows = strikes.map((s) => {
      const r = state.chain[s];
      const ce = r.CE || {}, pe = r.PE || {};
      const ceT = ce.tick || {}, ceG = ce.greeks || {};
      const peT = pe.tick || {}, peG = pe.greeks || {};
      const isAtm = state.atm && Math.abs(s - state.atm) < 1;
      return `<tr${isAtm ? ' class="atm"' : ''}>
        <td>${fmt(ceT.ltp)}</td>
        <td>${fmt(ceG.delta, 3)}</td>
        <td>${fmt(ceG.gamma, 5)}</td>
        <td>${fmt(ceG.theta, 2)}</td>
        <td>${fmt(ceG.vega, 3)}</td>
        <td>${fmt(ceG.iv ?? ceT.iv, 3)}</td>
        <td class="strike">${s}</td>
        <td>${fmt(peG.iv ?? peT.iv, 3)}</td>
        <td>${fmt(peG.vega, 3)}</td>
        <td>${fmt(peG.theta, 2)}</td>
        <td>${fmt(peG.gamma, 5)}</td>
        <td>${fmt(peG.delta, 3)}</td>
        <td>${fmt(peT.ltp)}</td>
      </tr>`;
    });
    tbody.innerHTML = rows.join("");
  }

  function fmt(v, d = 2) {
    if (v === undefined || v === null || v === "") return "";
    const n = +v;
    if (!Number.isFinite(n)) return "";
    return n.toFixed(d);
  }

  function fmtTime(ms) {
    const n = +ms;
    if (!Number.isFinite(n)) return "—";
    return new Date(n).toLocaleTimeString([], { hour12: false });
  }

  // ----- metrics view -----

  async function refreshMetrics() {
    try {
      const m = await fetch("/api/metrics").then((r) => r.json());
      setMetric("m-tick-rate",   m.tick_rate_per_s);
      setMetric("m-lat-p50",     m.ingest_latency_p50_ms);
      setMetric("m-lat-p95",     m.ingest_latency_p95_ms);
      setMetric("m-lat-max",     m.ingest_latency_max_ms);
      setMetric("m-ticks-total", m.ticks_total);
      setMetric("m-gaps",        m.gap_count);
      setMetric("m-reconnects",  m.reconnect_count);
      setMetric("m-dedup",       m.dedup_drops);
      setMetric("m-wal",         m.wal_appends);
      setMetric("m-pg-flushes",  m.pg_flushes);
      const rows = m.pg_rows_flushed;
      $("m-pg-rows").textContent = (rows ?? 0) + " rows";
      setMetric("m-candles",     m.candles_closed);
      setMetric("m-greeks",      m.greeks_computed);
      setMetric("m-signals-ok",  m.signals_emitted);
      setMetric("m-signals-cd",  m.signals_suppressed_cooldown);
      setMetric("m-signals-risk", m.signals_suppressed_risk);
      const updated = m.updated_ms;
      $("metrics-updated").textContent = updated
        ? `updated ${fmtTime(updated)}`
        : "no metrics yet (is ingest running?)";
    } catch (e) {
      $("metrics-updated").textContent = "error loading /api/metrics";
    }
  }

  function setMetric(id, v) {
    const el = $(id);
    if (v === undefined || v === null) { el.textContent = "—"; return; }
    if (typeof v === "number") {
      el.textContent = Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2);
    } else {
      el.textContent = String(v);
    }
  }

  function startMetricsPolling() {
    refreshMetrics();
    state.metricsTimer = setInterval(refreshMetrics, 3000);
  }

  // ----- replay view -----

  async function loadReplayList() {
    const ul = $("replay-runs");
    ul.innerHTML = `<li class="muted">loading…</li>`;
    let runs;
    try {
      runs = await fetch("/api/replays").then((r) => r.json());
    } catch (e) {
      ul.innerHTML = `<li class="muted">error loading /api/replays</li>`;
      return;
    }
    if (!runs.length) {
      ul.innerHTML = `<li class="muted">no replay runs yet — see tpp-replay</li>`;
      return;
    }
    ul.innerHTML = "";
    for (const run of runs) {
      const li = document.createElement("li");
      if (run.run_id === state.replayCurrentId) li.classList.add("active");
      const mtime = fmtTime(run.mtime_ms);
      const sig = run.headline?.signals_emitted ?? 0;
      const strategies = (run.manifest?.strategies ?? []).join(",");
      li.innerHTML = `
        <span class="rid">${run.run_id}</span>
        <span class="meta">${mtime} · ${strategies} · ${sig} signals</span>
      `;
      li.addEventListener("click", () => selectReplay(run.run_id));
      ul.appendChild(li);
    }
  }

  async function selectReplay(runId) {
    state.replayCurrentId = runId;
    // re-render list for highlight
    loadReplayList();
    $("replay-run-id").textContent = runId;
    $("replay-manifest").textContent = "loading…";
    $("replay-signals").innerHTML = "";
    $("replay-signals-count").textContent = "";

    let summary, manifest, signals;
    try {
      [summary, manifest, signals] = await Promise.all([
        fetch(`/api/replays/${runId}/summary`).then((r) => r.json()),
        fetch(`/api/replays/${runId}/manifest`).then((r) => r.json()),
        fetch(`/api/replays/${runId}/signals?limit=500`).then((r) => r.json()),
      ]);
    } catch (e) {
      $("replay-manifest").textContent = "error loading run";
      return;
    }

    const bits = [];
    if (manifest.source) bits.push(`source: ${manifest.source}`);
    if (manifest.strategies) bits.push(`strategies: ${manifest.strategies.join(", ")}`);
    if (manifest.timeframes) bits.push(`tf: ${manifest.timeframes.join("/")}`);
    if (manifest.started_at) bits.push(`started: ${manifest.started_at.slice(0, 19)}`);
    $("replay-manifest").textContent = bits.join(" · ");

    const grid = $("replay-summary-grid");
    grid.innerHTML = "";
    for (const [label, value] of summaryCells(summary)) {
      const cell = document.createElement("div");
      cell.className = "cell";
      cell.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
      grid.appendChild(cell);
    }

    fillKv("replay-by-strategy", summary.signals_by_strategy || {});
    fillKv("replay-by-action",   summary.signals_by_action   || {});
    fillKv("replay-by-tf",       summary.candles_closed_by_tf || {});

    const feed = $("replay-signals");
    feed.innerHTML = "";
    $("replay-signals-count").textContent = `${signals.length} signals`;
    for (const sig of signals) {
      const li = document.createElement("li");
      li.className = `sig ${sig.action}`;
      const t = sig.ts ? fmtTime(sig.ts) : "";
      li.textContent = `${t}  ${sig.strategy} · ${sig.action} ${sig.instrument} (c=${(sig.confidence ?? 0).toFixed(2)})  ${sig.reason ?? ""}`;
      feed.appendChild(li);
    }
  }

  function summaryCells(s) {
    const cells = [
      ["records read",     (s.records_read ?? 0).toLocaleString()],
      ["ticks (index)",    (s.ticks_index ?? 0).toLocaleString()],
      ["ticks (option)",   (s.ticks_option ?? 0).toLocaleString()],
      ["candles from src", (s.candles_from_source ?? 0).toLocaleString()],
      ["candles synth",    (s.candles_synthesized ?? 0).toLocaleString()],
      ["greeks computed",  (s.greeks_computed ?? 0).toLocaleString()],
      ["signals emitted",  (s.signals_emitted ?? 0).toLocaleString()],
      ["supp. cooldown",   (s.signals_suppressed_cooldown ?? 0).toLocaleString()],
      ["supp. risk",       (s.signals_suppressed_risk ?? 0).toLocaleString()],
      ["wall seconds",     (s.wall_seconds ?? 0).toFixed ? (s.wall_seconds).toFixed(2) : s.wall_seconds],
    ];
    if (s.ts_range_ms && s.ts_range_ms[0]) {
      cells.push(["ts start", fmtTime(s.ts_range_ms[0])]);
      cells.push(["ts end",   fmtTime(s.ts_range_ms[1])]);
    }
    return cells;
  }

  function fillKv(id, obj) {
    const ul = $(id);
    ul.innerHTML = "";
    const entries = Object.entries(obj);
    if (!entries.length) {
      ul.innerHTML = `<li class="muted">—</li>`;
      return;
    }
    entries.sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0));
    for (const [k, v] of entries) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="k">${k}</span><span class="v">${v}</span>`;
      ul.appendChild(li);
    }
  }

  init();
})();
