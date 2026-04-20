// Trading_Plug&Play — minimal vanilla-JS UI.

(() => {
  const $ = (id) => document.getElementById(id);

  const METRICS_TAB = "__metrics__";
  const REPLAY_TAB = "__replay__";
  const HEALTH_TAB = "__health__";
  const PAPER_TAB = "__paper__";

  const state = {
    indices: [],
    expiries: {},   // { INDEX: "YYYY-MM-DD" } — from /api/expiries (Fyers symbol master)
    current: null,
    ws: null,
    reconnectTimer: null,
    expiry: null,
    // Candle chart (TradingView Lightweight Charts)
    chart: null,
    candleSeries: null,
    candleTf: "1m",         // active timeframe
    lastCandleT: 0,         // guard against out-of-order WS updates
    candleResizeObs: null,
    chain: {},   // { strike: { CE: {tick, greeks}, PE: {tick, greeks} } }
    atm: null,
    metricsTimer: null,
    healthTimer: null,
    paperTimer: null,
    replayCurrentId: null,     // currently-inspected run on Replay tab
    mode: "live",              // "live" | "replay" for index tabs
    replayMode: {              // populated when mode === "replay"
      runId: null,
      signals: [],             // full signal array for the run
      summary: null,
      manifest: null,
    },
  };

  async function init() {
    try {
      const res = await fetch("/api/indices");
      state.indices = await res.json();
    } catch (e) {
      state.indices = ["NIFTY50", "BANKNIFTY", "SENSEX"];
    }
    try {
      const res = await fetch("/api/expiries");
      state.expiries = await res.json();
    } catch (e) {
      state.expiries = {};
    }
    renderTabs();
    initCandleChart();
    $("load-chain").addEventListener("click", loadChain);
    $("replay-refresh").addEventListener("click", loadReplayList);
    $("mode-live").addEventListener("click", () => setMode("live"));
    $("mode-replay").addEventListener("click", () => setMode("replay"));
    $("replay-picker").addEventListener("change", (e) => onReplayRunSelect(e.target.value));
    $("replay-banner-clear").addEventListener("click", () => setMode("live"));
    $("tf-select").addEventListener("change", (e) => {
      state.candleTf = e.target.value;
      if (state.current && state.indices.includes(state.current)) {
        loadCandles(state.current, state.candleTf);
      }
    });
    $("candles-max").addEventListener("click", toggleMaximizeCandles);
    const paperRefreshBtn = $("paper-refresh");
    if (paperRefreshBtn) paperRefreshBtn.addEventListener("click", loadPaperTrades);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && $("candles-section").classList.contains("maximized")) {
        toggleMaximizeCandles();
      }
    });
    window.addEventListener("popstate", () => applyUrlState());
    startPaperPolling();
    await applyUrlState({ first: true });
  }

  // ----- URL state -----

  function resolveIndexParam(raw) {
    if (!raw) return null;
    const up = String(raw).toUpperCase();
    if (state.indices.includes(up)) return up;
    // tolerant prefix match: "NIFTY" → "NIFTY50", "BANK" → "BANKNIFTY"
    const pref = state.indices.find((i) => i.startsWith(up));
    return pref || null;
  }

  function readUrlState() {
    const p = new URLSearchParams(location.search);
    const mode = p.get("mode") === "replay" ? "replay" : "live";
    const run_id = p.get("run_id") || null;
    const indexRaw = p.get("index");
    const index = resolveIndexParam(indexRaw);
    const tab = p.get("tab");
    return { mode, run_id, index, tab };
  }

  function writeUrlState() {
    const p = new URLSearchParams();
    if (state.mode === "replay") p.set("mode", "replay");
    if (state.mode === "replay" && state.replayMode.runId) {
      p.set("run_id", state.replayMode.runId);
    }
    if (state.current === METRICS_TAB) p.set("tab", "metrics");
    else if (state.current === HEALTH_TAB) p.set("tab", "health");
    else if (state.current === REPLAY_TAB) p.set("tab", "replay");
    else if (state.current === PAPER_TAB) p.set("tab", "paper");
    else if (state.current) p.set("index", state.current);
    const qs = p.toString();
    const next = qs ? `${location.pathname}?${qs}` : location.pathname;
    if (next !== location.pathname + location.search) {
      history.replaceState(null, "", next);
    }
  }

  async function applyUrlState({ first = false } = {}) {
    const { mode, run_id, index, tab } = readUrlState();

    // Mode first so replay artifacts are preloaded before rendering the tab.
    if (mode === "replay") {
      await setMode("replay", { skipUrl: true, skipTabRender: true });
      if (run_id) {
        await populateReplayPicker();
        const picker = $("replay-picker");
        if ([...picker.options].some((o) => o.value === run_id)) {
          picker.value = run_id;
        }
        await onReplayRunSelect(run_id, { skipUrl: true });
      }
    } else if (!first) {
      // popstate back to live
      await setMode("live", { skipUrl: true, skipTabRender: true });
    }

    let target;
    if (tab === "metrics") target = METRICS_TAB;
    else if (tab === "health") target = HEALTH_TAB;
    else if (tab === "replay") target = REPLAY_TAB;
    else if (tab === "paper") target = PAPER_TAB;
    else if (index) target = index;
    else target = state.indices[0];

    if (target) await switchTab(target, { skipUrl: true });

    writeUrlState();
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
      [HEALTH_TAB, "Health"],
      [PAPER_TAB, "Paper"],
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

  async function switchTab(key, opts = {}) {
    // Tear down everything the previous tab owned.
    if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    if (state.metricsTimer) { clearInterval(state.metricsTimer); state.metricsTimer = null; }
    if (state.healthTimer)  { clearInterval(state.healthTimer);  state.healthTimer  = null; }
    if (state.paperTimer)   { clearInterval(state.paperTimer);   state.paperTimer   = null; }

    state.current = key;
    renderTabs();
    showView(key);

    try {
      if (key === METRICS_TAB) {
        startMetricsPolling();
        return;
      }
      if (key === HEALTH_TAB) {
        startHealthPolling();
        return;
      }
      if (key === REPLAY_TAB) {
        loadReplayList();
        return;
      }
      if (key === PAPER_TAB) {
        loadPaperTrades();
        state.paperTimer = setInterval(loadPaperTrades, 5000);
        return;
      }
      // Index tab
      state.chain = {};
      $("chain-tbody").innerHTML = "";
      if (state.candleSeries) state.candleSeries.setData([]);
      state.lastCandleT = 0;
      $("signals").innerHTML = "";
      $("spot").textContent = "—";
      $("last-seen").textContent = "—";
      $("expiry").value = state.expiries[key] || defaultExpiryISO();

      if (state.mode === "replay") {
        renderIndexReplay(key);
        return;
      }
      // live
      await fetchState(key);
      await loadChain();
      await loadCandles(key, state.candleTf);
      connectWs(key);
    } finally {
      if (!opts.skipUrl) writeUrlState();
    }
  }

  function showView(key) {
    const live = $("view-live");
    const metrics = $("view-metrics");
    const replay = $("view-replay");
    const health = $("view-health");
    const paper = $("view-paper");
    live.hidden = true; metrics.hidden = true;
    replay.hidden = true; health.hidden = true;
    if (paper) paper.hidden = true;
    if (key === METRICS_TAB) metrics.hidden = false;
    else if (key === REPLAY_TAB) replay.hidden = false;
    else if (key === HEALTH_TAB) health.hidden = false;
    else if (key === PAPER_TAB && paper) paper.hidden = false;
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
      state.ltpHistory = {};   // reset per-strike momentum when reloading
      if (typeof body.spot === "number") state.spotValue = body.spot;
      for (const [strike, row] of Object.entries(body.strikes || {})) {
        state.chain[+strike] = {
          CE: row.CE || {},
          PE: row.PE || {},
        };
      }
      renderChain();
    } catch (e) { /* nop */ }
  }

  // Track last N LTPs per (strike,side) for tick-momentum tagging.
  const LTP_HISTORY_N = 5;
  function recordLtp(strike, ot, ltp) {
    if (typeof ltp !== "number") return;
    state.ltpHistory ||= {};
    const key = `${strike}:${ot}`;
    const arr = (state.ltpHistory[key] ||= []);
    if (arr.length && arr[arr.length - 1] === ltp) return;   // no change
    arr.push(ltp);
    if (arr.length > LTP_HISTORY_N) arr.shift();
  }

  function ltpMomentum(strike, ot) {
    const arr = state.ltpHistory?.[`${strike}:${ot}`];
    if (!arr || arr.length < 2) return "flat";
    const first = arr[0], last = arr[arr.length - 1];
    if (last > first) return "up";
    if (last < first) return "down";
    return "flat";
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
      else if (channel.startsWith("scalp.")) onScalpEvent(channel, data);
      else if (channel.startsWith("critical.regime.")) onRegimeEvent(channel, data);
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
    state.spotValue = tick.ltp;
    $("spot").textContent = tick.ltp.toFixed(2);
    if (tick.ts_exchange) $("last-seen").textContent = fmtTime(tick.ts_exchange);
  }

  function onOptionTick(tick) {
    if (!tick || !tick.strike) return;
    const row = state.chain[tick.strike] ||= { CE: {}, PE: {} };
    row[tick.option_type] ||= {};
    row[tick.option_type].tick = tick;
    // Client-side derived metrics (no backend round-trip per tick).
    row[tick.option_type].metrics = computeRowMetrics(
      tick.strike, tick.option_type, tick, row[tick.option_type].greeks,
    );
    recordLtp(tick.strike, tick.option_type, tick.ltp);
    scheduleRender();
  }

  function onGreeks(g) {
    if (!g || !g.strike) return;
    const row = state.chain[g.strike] ||= { CE: {}, PE: {} };
    row[g.option_type] ||= {};
    row[g.option_type].greeks = g;
    // itm_prob / intrinsic / TV depend on greeks + spot — recompute.
    if (row[g.option_type].tick) {
      row[g.option_type].metrics = computeRowMetrics(
        g.strike, g.option_type, row[g.option_type].tick, g,
      );
    }
    scheduleRender();
  }

  function onCandle(channel, candle) {
    // Only update the chart when the WS channel matches the active TF — the
    // server publishes all three concurrently. Candlestick `time` is unix
    // seconds in IST so bars align with the broker's clock.
    const tf = channel.split(".").pop();
    if (tf !== state.candleTf) return;
    if (!state.candleSeries || !candle || !candle.open_ts) return;
    const t = Math.floor(candle.open_ts / 1000);
    if (t < state.lastCandleT) return;    // ignore out-of-order
    state.lastCandleT = t;
    state.candleSeries.update({
      time: t,
      open: candle.open, high: candle.high,
      low: candle.low,  close: candle.close,
    });
  }

  // ---------- Candlestick chart (Lightweight Charts) ----------

  function initCandleChart() {
    const el = $("candle-chart");
    if (!el || !window.LightweightCharts) return;

    const chart = window.LightweightCharts.createChart(el, {
      layout: {
        background: { type: "solid", color: "#161b22" },
        textColor: "#d1d5db",
      },
      grid: {
        vertLines: { color: "#1f2937" },
        horzLines: { color: "#1f2937" },
      },
      rightPriceScale: { borderColor: "#30363d" },
      timeScale: {
        borderColor: "#30363d",
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: { mode: 0 },
      autoSize: false,
    });
    const series = chart.addCandlestickSeries({
      upColor: "#3fb950", downColor: "#f85149",
      borderUpColor: "#3fb950", borderDownColor: "#f85149",
      wickUpColor: "#3fb950", wickDownColor: "#f85149",
    });
    chart.applyOptions({
      width: el.clientWidth,
      height: el.clientHeight || 200,
    });
    state.chart = chart;
    state.candleSeries = series;

    // Keep the chart sized to its container — responds to maximize/restore
    // and to the viewport resizing. Coalesce into rAF so a flurry of resize
    // events doesn't thrash the canvas.
    let scheduled = false;
    const resize = () => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        if (!state.chart) return;
        state.chart.resize(el.clientWidth, el.clientHeight);
      });
    };
    state.candleResizeObs = new ResizeObserver(resize);
    state.candleResizeObs.observe(el);
  }

  async function loadCandles(idx, tf) {
    if (!state.candleSeries) return;
    try {
      const res = await fetch(
        `/api/candles/${idx}?timeframe=${encodeURIComponent(tf)}&limit=300`,
      );
      if (!res.ok) return;
      const rows = await res.json();
      const data = rows
        .map((c) => ({
          time: Math.floor(new Date(c.open_ts).getTime() / 1000),
          open: c.open, high: c.high, low: c.low, close: c.close,
        }))
        .filter((d) => Number.isFinite(d.time));
      state.candleSeries.setData(data);
      state.lastCandleT = data.length ? data[data.length - 1].time : 0;
      if (state.chart) state.chart.timeScale().fitContent();
    } catch (e) { /* server may be cold */ }
  }

  function toggleMaximizeCandles() {
    const sec = $("candles-section");
    const btn = $("candles-max");
    const nowMax = sec.classList.toggle("maximized");
    if (btn) {
      btn.textContent = nowMax ? "✕" : "⤢";
      btn.title = nowMax ? "Restore" : "Maximize";
    }
    // Let layout settle before reading the new container size, then push
    // the dimensions into Lightweight Charts so the canvas fills the new
    // viewport. ResizeObserver covers incidental resizes later.
    requestAnimationFrame(() => {
      const el = $("candle-chart");
      if (state.chart && el) {
        state.chart.resize(el.clientWidth, el.clientHeight);
        state.chart.timeScale().fitContent();
      }
    });
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

  // ---------- paper-trading feed from trading.critical ----------

  // `scalp.{INDEX}` events come from trading.critical.executor — entry /
  // exit / entry_rejected. Render compact coloured lines in the signals
  // feed so the live chain view shows the paper book's activity.
  function onScalpEvent(channel, ev) {
    if (!ev || typeof ev !== "object") return;
    const idx = channel.split(".", 2)[1] || "";
    const li = document.createElement("li");
    const t = ev.ts ? fmtTime(ev.ts) : "";
    const side = ev.side || "";
    const strike = ev.strike ?? "";
    const kind = ev.kind || "event";
    if (kind === "entry") {
      li.className = "sig entry BUY";
      li.textContent =
        `${t}  ENTRY  ${idx} ${strike}${side}  @${(ev.entry_ltp ?? 0).toFixed(2)}  ` +
        `stop=${(ev.stop ?? 0).toFixed(2)} target=${(ev.target ?? 0).toFixed(2)}  ` +
        `(c=${(ev.confidence ?? 0).toFixed(2)})  ${(ev.reasons || []).join(" · ")}`;
    } else if (kind === "exit") {
      const pnl = Number(ev.pnl ?? 0);
      li.className = `sig exit ${pnl >= 0 ? "win" : "loss"}`;
      const held = Math.round((ev.held_ms ?? 0) / 1000);
      li.textContent =
        `${t}  EXIT   ${idx} ${strike}${side}  ${pnl >= 0 ? "+" : ""}₹${pnl.toFixed(2)}  ` +
        `hold=${held}s  ${ev.reason ?? ""}`;
    } else if (kind === "entry_rejected") {
      li.className = "sig rejected";
      li.textContent = `${t}  REJECT ${idx} ${strike}${side}  ${ev.reason ?? ""}`;
    } else {
      li.textContent = `${t}  ${kind.toUpperCase()} ${idx}`;
    }
    $("signals").prepend(li);
    const MAX = 60;
    while ($("signals").children.length > MAX) $("signals").lastElementChild.remove();
  }

  function onRegimeEvent(channel, data) {
    // Lightweight: surface the regime only in the paper strip's tooltip.
    if (!data || typeof data !== "object") return;
    state.lastRegime ||= {};
    const idx = channel.split(".").pop();
    state.lastRegime[idx] = data;
  }

  // ---------- paper trading summary strip + poller ----------

  let paperTimer = null;

  function startPaperPolling() {
    stopPaperPolling();
    refreshPaper();
    paperTimer = setInterval(refreshPaper, 5000);
  }

  function stopPaperPolling() {
    if (paperTimer) { clearInterval(paperTimer); paperTimer = null; }
  }

  async function refreshPaper() {
    const el = $("paper-strip");
    if (!el) return;
    try {
      const r = await fetch("/api/critical/validation");
      if (!r.ok) { el.textContent = "paper: offline"; return; }
      const d = await r.json();
      if (!d.available) { el.textContent = "paper: idle"; return; }
      const o = d.overall || {};
      const pnlNum = Number(o.total_pnl ?? 0);
      const pnl = (pnlNum >= 0 ? "+" : "") + pnlNum.toFixed(0);
      const hit = Math.round((o.hit_rate ?? 0) * 100);
      el.innerHTML =
        `paper · entries <b>${o.entries ?? 0}</b> · ` +
        `W/L <b>${o.wins ?? 0}/${o.losses ?? 0}</b> · ` +
        `hit <b>${hit}%</b> · ` +
        `pnl <b class="${pnlNum >= 0 ? "pos" : "neg"}">₹${pnl}</b> · ` +
        `up ${d.uptime_s ?? 0}s`;
    } catch (e) {
      el.textContent = "paper: err";
    }
  }

  // ---------- Paper tab: full trade table + per-index cards ----------

  async function loadPaperTrades() {
    try {
      const [tradesRes, valRes] = await Promise.all([
        fetch("/api/critical/trades?limit=200"),
        fetch("/api/critical/validation"),
      ]);
      const trades = tradesRes.ok ? await tradesRes.json() : [];
      const val = valRes.ok ? await valRes.json() : {};
      renderPaperTrades(trades, val);
    } catch (e) {
      const body = $("paper-trades-body");
      if (body) body.innerHTML = `<tr><td colspan="14" class="muted">error loading trades</td></tr>`;
    }
  }

  function renderPaperTrades(trades, val) {
    // ---- overall header ----
    const o = (val && val.overall) || {};
    const pnl = Number(o.total_pnl ?? 0);
    $("paper-overall").innerHTML =
      `entries <b>${o.entries ?? 0}</b> · ` +
      `closed <b>${o.closed ?? 0}</b> · ` +
      `W/L <b>${o.wins ?? 0}/${o.losses ?? 0}</b> · ` +
      `hit <b>${Math.round((o.hit_rate ?? 0) * 100)}%</b> · ` +
      `pnl <b class="${pnl >= 0 ? "pos" : "neg"}">₹${(pnl >= 0 ? "+" : "") + pnl.toFixed(2)}</b>`;

    // ---- per-index cards ----
    const per = (val && val.per_index) || {};
    const cards = Object.entries(per)
      .filter(([, s]) => s.entries || s.exits)
      .map(([idx, s]) => {
        const p = Number(s.total_pnl ?? 0);
        const reasons = Object.entries(s.by_reason || {})
          .map(([k, v]) => `${k}:${v}`).join(" · ");
        return `<div class="paper-card">
          <h3>${idx}</h3>
          <div class="row"><span>Entries</span><b>${s.entries ?? 0}</b></div>
          <div class="row"><span>W / L</span><b>${s.wins ?? 0} / ${s.losses ?? 0}</b></div>
          <div class="row"><span>Hit</span><b>${Math.round((s.hit_rate ?? 0) * 100)}%</b></div>
          <div class="row"><span>PnL</span><b class="${p >= 0 ? "pos" : "neg"}">${(p >= 0 ? "+" : "") + p.toFixed(2)}</b></div>
          <div class="row"><span>Avg hold</span><b>${(s.avg_hold_s ?? 0).toFixed(0)} s</b></div>
          <div class="row reasons"><span>Exits</span><b>${reasons || "—"}</b></div>
        </div>`;
      })
      .join("");
    $("paper-cards").innerHTML = cards || `<div class="muted">no trades yet today</div>`;

    // ---- full trades table ----
    const body = $("paper-trades-body");
    const countEl = $("paper-trade-count");
    const rows = trades.map((t) => {
      const status = t.status || "?";
      const pnlN = Number(t.pnl ?? 0);
      const pnlCls = pnlN > 0 ? "pos" : (pnlN < 0 ? "neg" : "");
      const pnlTxt = t.pnl != null
        ? (pnlN >= 0 ? "+" : "") + pnlN.toFixed(2)
        : "—";
      const hold = t.held_ms != null ? Math.round(t.held_ms / 1000) : "—";
      const when = t.exit_ts
        ? fmtTime(t.exit_ts)
        : (t.entry_ts ? fmtTime(t.entry_ts) + " (open)" : "");
      const reasons = (t.reasons || []).join(" · ");
      const entryLtp = t.entry_ltp != null ? Number(t.entry_ltp).toFixed(2) : "—";
      const exitLtp = t.exit_ltp != null ? Number(t.exit_ltp).toFixed(2) : "—";
      const stop = t.stop != null ? Number(t.stop).toFixed(2) : "—";
      const target = t.target != null ? Number(t.target).toFixed(2) : "—";
      const lotsQty = t.lots != null
        ? `${t.lots}×${(t.qty ?? 0)}`
        : "—";
      const statusCls =
        status === "open" ? "status-open" :
        status === "rejected" ? "status-rejected" :
        (pnlN >= 0 ? "status-win" : "status-loss");
      return `<tr class="${statusCls}">
        <td class="num">${when}</td>
        <td>${t.index ?? ""}</td>
        <td class="side-${t.side ?? ""}">${t.side ?? ""}</td>
        <td class="num">${t.strike ?? ""}</td>
        <td class="num">${lotsQty}</td>
        <td class="num">${entryLtp}</td>
        <td class="num">${exitLtp}</td>
        <td class="num">${stop}</td>
        <td class="num">${target}</td>
        <td class="num ${pnlCls}"><b>${pnlTxt}</b></td>
        <td class="num">${hold}</td>
        <td class="reason">${t.reason ?? "—"}</td>
        <td class="status">${status}</td>
        <td class="reasons muted" title="${reasons}">${reasons.length > 60 ? reasons.slice(0, 57) + "…" : reasons}</td>
      </tr>`;
    }).join("");
    body.innerHTML = rows || `<tr><td colspan="14" class="muted">no trades yet today</td></tr>`;
    if (countEl) countEl.textContent = `${trades.length} rows`;
  }

  let renderPending = false;
  function scheduleRender() {
    if (renderPending) return;
    renderPending = true;
    requestAnimationFrame(() => { renderPending = false; renderChain(); });
  }

  // ---- derived metrics (client-side mirror of trading.derived.build_metrics) ----
  // Kept here so per-tick updates don't pay a network round-trip; the REST
  // endpoint computes the same values for initial page load.
  const LOW_LIQ_SPREAD_PCT = 4.0;
  const LOW_LIQ_MIN_VOLUME = 100;

  function computeRowMetrics(strike, ot, tick, greeks) {
    if (!tick) return {};
    const bid = tick.bid, ask = tick.ask;
    const bq = tick.bid_qty, aq = tick.ask_qty;
    const vol = tick.volume, oi = tick.oi;
    const ltp = tick.ltp;
    const spot = (greeks && greeks.spot) || state.spotValue || null;

    let spread_pct = null;
    if (Number.isFinite(bid) && Number.isFinite(ask) && ask >= bid && (bid + ask) > 0) {
      const mid = (bid + ask) / 2;
      spread_pct = mid > 0 ? ((ask - bid) / mid) * 100 : null;
    }
    let imbalance = null;
    if (Number.isFinite(bq) && Number.isFinite(aq) && (bq + aq) > 0) {
      imbalance = (bq - aq) / (bq + aq);
    }
    let vol_oi = null;
    if (Number.isFinite(vol) && Number.isFinite(oi) && oi > 0) {
      vol_oi = vol / oi;
    }
    let intrinsic = null, time_value = null;
    if (Number.isFinite(spot) && spot > 0 && Number.isFinite(ltp) && Number.isFinite(strike)) {
      intrinsic = ot === "CE"
        ? Math.max(spot - strike, 0)
        : Math.max(strike - spot, 0);
      time_value = Math.max(ltp - intrinsic, 0);
    }
    const itm_prob = (greeks && Number.isFinite(greeks.itm_prob)) ? greeks.itm_prob : null;
    const low_liq =
      (spread_pct !== null && spread_pct > LOW_LIQ_SPREAD_PCT) ||
      (Number.isFinite(vol) && vol < LOW_LIQ_MIN_VOLUME);

    return { spread_pct, imbalance, vol_oi, intrinsic, time_value, itm_prob, low_liq };
  }

  // ---- cell builders ----

  function fmtQty(n) {
    if (!Number.isFinite(n)) return "—";
    if (n >= 1e7) return (n / 1e7).toFixed(1) + "Cr";
    if (n >= 1e5) return (n / 1e5).toFixed(1) + "L";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(n);
  }

  function fmtPct(n) {
    if (!Number.isFinite(n)) return "—";
    return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
  }

  function badgesCell(m) {
    if (!m) return "";
    const tags = [];
    if (m.low_liq) tags.push(`<span class="badge liq">LIQ</span>`);
    if (Number.isFinite(m.imbalance)) {
      if (m.imbalance > 0.3) tags.push(`<span class="badge imb-up">IMB↑</span>`);
      else if (m.imbalance < -0.3) tags.push(`<span class="badge imb-down">IMB↓</span>`);
    }
    return tags.join("");
  }

  function spreadCell(pct) {
    if (!Number.isFinite(pct)) return `<td class="num">—</td>`;
    const cls = pct > LOW_LIQ_SPREAD_PCT ? "warn" : "";
    return `<td class="num ${cls}">${pct.toFixed(2)}</td>`;
  }

  function ltpCell(strike, ot, ltp, mom) {
    if (!Number.isFinite(ltp)) return `<td class="num ltp">—</td>`;
    return `<td class="num ltp mom-${mom}">${ltp.toFixed(2)}</td>`;
  }

  function chgCell(v, pct = false) {
    if (!Number.isFinite(v)) return `<td class="num">—</td>`;
    const cls = v > 0 ? "pos" : (v < 0 ? "neg" : "");
    return `<td class="num ${cls}">${pct ? fmtPct(v) : (v >= 0 ? "+" : "") + v.toFixed(2)}</td>`;
  }

  function bidAskCell(px, qty) {
    if (!Number.isFinite(px)) return `<td class="num">—</td>`;
    const q = Number.isFinite(qty) ? `<span class="q">×${fmtQty(qty)}</span>` : "";
    return `<td class="num">${px.toFixed(2)}${q}</td>`;
  }

  function itmCell(p) {
    if (!Number.isFinite(p)) return `<td class="num">—</td>`;
    return `<td class="num">${(p * 100).toFixed(1)}</td>`;
  }

  function itmCellClass(p, extra) {
    const txt = Number.isFinite(p) ? (p * 100).toFixed(1) : "—";
    return `<td class="num ${extra}">${txt}</td>`;
  }

  function chgCellClass(v, pct, extra) {
    if (!Number.isFinite(v)) return `<td class="num ${extra}">—</td>`;
    const cls = v > 0 ? "pos" : (v < 0 ? "neg" : "");
    const txt = pct ? fmtPct(v) : (v >= 0 ? "+" : "") + v.toFixed(2);
    return `<td class="num ${cls} ${extra}">${txt}</td>`;
  }

  function renderChain() {
    const tbody = $("chain-tbody");
    const strikes = Object.keys(state.chain).map(Number).sort((a, b) => a - b);
    const rows = strikes.map((s) => {
      const r = state.chain[s];
      const ce = r.CE || {}, pe = r.PE || {};
      const ceT = ce.tick || {}, ceG = ce.greeks || {}, ceM = ce.metrics || {};
      const peT = pe.tick || {}, peG = pe.greeks || {}, peM = pe.metrics || {};
      const ceMom = ltpMomentum(s, "CE");
      const peMom = ltpMomentum(s, "PE");
      const isAtm = state.atm && Math.abs(s - state.atm) < 1;
      const trCls = ["row", isAtm ? "atm" : ""].filter(Boolean).join(" ");

      const ceVoiTxt = Number.isFinite(ceM.vol_oi) ? ceM.vol_oi.toFixed(2) : "—";
      const peVoiTxt = Number.isFinite(peM.vol_oi) ? peM.vol_oi.toFixed(2) : "—";
      const ceTvTxt  = Number.isFinite(ceM.time_value) ? ceM.time_value.toFixed(2) : "—";
      const peTvTxt  = Number.isFinite(peM.time_value) ? peM.time_value.toFixed(2) : "—";
      // NB: hide-md / hide-sm classes must mirror the <th> layout in index.html
      // so column collapse under @media queries keeps rows aligned.
      return `<tr class="${trCls}">
        <td class="num">${fmtQty(ceT.oi)}</td>
        <td class="num hide-sm">${fmtQty(ceT.oi_change)}</td>
        <td class="num">${fmtQty(ceT.volume)}</td>
        <td class="num hide-sm">${ceVoiTxt}</td>
        <td class="num">${fmt(ceG.iv ?? ceT.iv, 3)}</td>
        <td class="num">${fmt(ceG.delta, 3)}</td>
        <td class="num hide-md">${fmt(ceG.gamma, 5)}</td>
        <td class="num hide-sm">${fmt(ceG.theta, 2)}</td>
        <td class="num hide-sm">${fmt(ceG.vega, 3)}</td>
        ${itmCellClass(ceM.itm_prob, "hide-md")}
        <td class="num hide-md">${ceTvTxt}</td>
        ${spreadCell(ceM.spread_pct)}
        ${bidAskCell(ceT.bid, ceT.bid_qty)}
        ${ltpCell(s, "CE", ceT.ltp, ceMom)}
        ${chgCellClass(ceT.change, false, "hide-sm")}
        ${chgCell(ceT.change_pct, true)}
        ${bidAskCell(ceT.ask, ceT.ask_qty)}

        <td class="strike">${s}<div class="badges">${badgesCell(ceM)}${badgesCell(peM)}</div></td>

        ${bidAskCell(peT.bid, peT.bid_qty)}
        ${ltpCell(s, "PE", peT.ltp, peMom)}
        ${chgCellClass(peT.change, false, "hide-sm")}
        ${chgCell(peT.change_pct, true)}
        ${bidAskCell(peT.ask, peT.ask_qty)}
        ${spreadCell(peM.spread_pct)}
        <td class="num hide-md">${peTvTxt}</td>
        ${itmCellClass(peM.itm_prob, "hide-md")}
        <td class="num hide-sm">${fmt(peG.vega, 3)}</td>
        <td class="num hide-sm">${fmt(peG.theta, 2)}</td>
        <td class="num hide-md">${fmt(peG.gamma, 5)}</td>
        <td class="num">${fmt(peG.delta, 3)}</td>
        <td class="num">${fmt(peG.iv ?? peT.iv, 3)}</td>
        <td class="num hide-sm">${peVoiTxt}</td>
        <td class="num">${fmtQty(peT.volume)}</td>
        <td class="num hide-sm">${fmtQty(peT.oi_change)}</td>
        <td class="num">${fmtQty(peT.oi)}</td>
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

  // ----- health view -----

  async function refreshHealth() {
    let h, hist;
    try {
      [h, hist] = await Promise.all([
        fetch("/api/health").then((r) => r.json()),
        fetch("/api/notifications/history?limit=50").then((r) => r.json()).catch(() => []),
      ]);
    } catch (e) {
      setHealthBanner("down");
      $("health-updated").textContent = "error loading /api/health";
      return;
    }
    renderHealthHistory(hist || []);
    setHealthBanner(h.status || "down");
    $("health-updated").textContent =
      `updated ${fmtTime(h.generated_ms)}` +
      (h.market_open ? " · market open" : " · market closed");

    const checks = $("health-checks");
    checks.innerHTML = "";
    for (const c of h.checks || []) {
      const li = document.createElement("li");
      li.className = c.status;
      const lat = c.latency_ms != null ? ` · ${c.latency_ms}ms` : "";
      li.innerHTML = `
        <span class="name">${c.name}</span>
        <span class="detail">${escapeHtml(c.detail || "")}${lat}</span>
        <span class="badge">${c.status}</span>
      `;
      checks.appendChild(li);
    }

    const alerts = $("health-alerts");
    alerts.innerHTML = "";
    if (!(h.alerts || []).length) {
      alerts.innerHTML = `<li class="muted">no active alerts</li>`;
    } else {
      for (const a of h.alerts) {
        const li = document.createElement("li");
        li.className = `alert ${a.severity}`;
        li.innerHTML = `
          <span class="rule">${a.rule}</span>
          <span>${escapeHtml(a.detail || "")}</span>
          <span class="when">${fmtTime(a.since_ms)}</span>
        `;
        alerts.appendChild(li);
      }
    }

    const idxUl = $("health-indices");
    idxUl.innerHTML = "";
    for (const idx of h.indices || []) {
      const li = document.createElement("li");
      li.className = idx.status;
      const age = (idx.staleness_s == null)
        ? "no ticks"
        : `${idx.staleness_s}s ago`;
      li.innerHTML = `
        <span class="idx">${idx.index}</span>
        <span class="age">${age}</span>
        <span class="badge">${idx.status}</span>
      `;
      idxUl.appendChild(li);
    }
  }

  function setHealthBanner(status) {
    const el = $("health-banner");
    el.className = "health-banner " + status;
    $("health-status-label").textContent = status;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function startHealthPolling() {
    refreshHealth();
    state.healthTimer = setInterval(refreshHealth, 5000);
  }

  function renderHealthHistory(entries) {
    const ul = $("health-history");
    ul.innerHTML = "";
    $("history-count").textContent = entries.length
      ? `${entries.length} entr${entries.length === 1 ? "y" : "ies"}`
      : "no history yet";
    if (!entries.length) {
      ul.innerHTML = `<li class="muted">no alerts recorded</li>`;
      return;
    }
    for (const e of entries) {
      const li = document.createElement("li");
      const cls = e.kind === "resolved"
        ? "resolved"
        : `fired ${e.severity || ""}`;
      li.className = cls;
      const when = e.ts_ms ? fmtTime(e.ts_ms) : "";
      const sev = (e.severity || "").toUpperCase();
      const delivery = e.delivered ? "sent" : "failed";
      li.innerHTML = `
        <span class="kind">${e.kind}${sev ? " · " + sev : ""}</span>
        <span class="rule">${escapeHtml(e.rule || "")}</span>
        <span class="detail">${escapeHtml(e.detail || "")}</span>
        <span class="delivery ${delivery}">${delivery}</span>
        <span class="when">${when}</span>
      `;
      ul.appendChild(li);
    }
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

  // ----- live / replay mode toggle -----

  async function setMode(mode, opts = {}) {
    if (mode === state.mode) {
      if (!opts.skipUrl) writeUrlState();
      return;
    }
    state.mode = mode;
    $("mode-live").classList.toggle("active", mode === "live");
    $("mode-replay").classList.toggle("active", mode === "replay");
    $("replay-picker").disabled = (mode !== "replay");

    if (mode === "replay") {
      await populateReplayPicker();
      applyReplayBanner();
      applyLiveOnlyPanelsDim(true);
      if (!opts.skipTabRender &&
          state.current && !state.current.startsWith("__") &&
          state.replayMode.runId) {
        renderIndexReplay(state.current);
      }
    } else {
      $("replay-banner").hidden = true;
      applyLiveOnlyPanelsDim(false);
      if (!opts.skipTabRender &&
          state.current && !state.current.startsWith("__")) {
        await switchTab(state.current, { skipUrl: true });
      }
    }
    if (!opts.skipUrl) writeUrlState();
  }

  function applyLiveOnlyPanelsDim(on) {
    for (const id of ["chain-section", "candles-section"]) {
      const el = $(id);
      if (!el) continue;
      el.classList.toggle("panel-dim", on);
    }
  }

  function applyReplayBanner() {
    const banner = $("replay-banner");
    const text = $("replay-banner-text");
    if (state.mode !== "replay") {
      banner.hidden = true;
      return;
    }
    if (!state.replayMode.runId) {
      banner.hidden = false;
      text.textContent = "Replay mode — select a run above to load data";
      return;
    }
    const m = state.replayMode.manifest || {};
    const s = state.replayMode.summary || {};
    const bits = [`Replay · ${state.replayMode.runId}`];
    if (m.source) bits.push(m.source);
    if (m.strategies) bits.push(`strategies: ${m.strategies.join(", ")}`);
    if (s.ts_range_ms && s.ts_range_ms[0]) {
      bits.push(`${fmtTime(s.ts_range_ms[0])} → ${fmtTime(s.ts_range_ms[1])}`);
    }
    text.textContent = bits.join(" · ");
    banner.hidden = false;
  }

  async function populateReplayPicker() {
    const picker = $("replay-picker");
    let runs;
    try {
      runs = await fetch("/api/replays").then((r) => r.json());
    } catch (e) {
      picker.innerHTML = `<option value="">(error loading runs)</option>`;
      return;
    }
    picker.innerHTML = "";
    if (!runs.length) {
      picker.innerHTML = `<option value="">no runs yet</option>`;
      return;
    }
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "— pick a run —";
    picker.appendChild(placeholder);
    for (const run of runs) {
      const opt = document.createElement("option");
      opt.value = run.run_id;
      const strategies = (run.manifest?.strategies ?? []).join(",");
      const n = run.headline?.signals_emitted ?? 0;
      opt.textContent = `${run.run_id} · ${strategies} · ${n} sig`;
      if (run.run_id === state.replayMode.runId) opt.selected = true;
      picker.appendChild(opt);
    }
  }

  async function onReplayRunSelect(runId, opts = {}) {
    if (!runId) {
      state.replayMode.runId = null;
      state.replayMode.signals = [];
      state.replayMode.summary = null;
      state.replayMode.manifest = null;
      applyReplayBanner();
      if (state.current && !state.current.startsWith("__")) {
        renderIndexReplay(state.current);
      }
      if (!opts.skipUrl) writeUrlState();
      return;
    }
    try {
      const [summary, manifest, signals] = await Promise.all([
        fetch(`/api/replays/${runId}/summary`).then((r) => r.json()),
        fetch(`/api/replays/${runId}/manifest`).then((r) => r.json()),
        fetch(`/api/replays/${runId}/signals?limit=5000`).then((r) => r.json()),
      ]);
      state.replayMode.runId = runId;
      state.replayMode.summary = summary;
      state.replayMode.manifest = manifest;
      state.replayMode.signals = signals;
    } catch (e) {
      state.replayMode.runId = null;
      state.replayMode.signals = [];
      state.replayMode.summary = null;
      state.replayMode.manifest = null;
    }
    applyReplayBanner();
    if (state.current && !state.current.startsWith("__")) {
      renderIndexReplay(state.current);
    }
    if (!opts.skipUrl) writeUrlState();
  }

  function renderIndexReplay(idx) {
    // live banner + dimmed panels
    applyReplayBanner();
    applyLiveOnlyPanelsDim(true);

    // header stats from summary ts_range
    const s = state.replayMode.summary;
    if (s && s.ts_range_ms && s.ts_range_ms[0]) {
      $("spot").textContent = "replay";
      $("last-seen").textContent =
        `${fmtTime(s.ts_range_ms[0])} → ${fmtTime(s.ts_range_ms[1])}`;
    } else {
      $("spot").textContent = "—";
      $("last-seen").textContent = "—";
    }
    const wsEl = $("ws-status");
    wsEl.textContent = "replay";
    wsEl.classList.remove("connected");
    wsEl.classList.remove("disconnected");

    // signals feed filtered to this index
    const feed = $("signals");
    feed.innerHTML = "";
    if (!state.replayMode.runId) {
      feed.innerHTML = `<li class="muted">pick a run from the dropdown to load signals</li>`;
      return;
    }
    const forIndex = state.replayMode.signals.filter((sg) => sg.index === idx);
    if (!forIndex.length) {
      feed.innerHTML = `<li class="muted">no signals for ${idx} in this run</li>`;
      return;
    }
    // Newest first for consistency with live feed
    forIndex.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0));
    const MAX = 500;
    const take = forIndex.slice(0, MAX);
    for (const sig of take) {
      const li = document.createElement("li");
      li.className = `sig ${sig.action}`;
      const t = sig.ts ? fmtTime(sig.ts) : "";
      li.textContent = `${t}  ${sig.strategy} · ${sig.action} ${sig.instrument} (c=${(sig.confidence ?? 0).toFixed(2)})  ${sig.reason ?? ""}`;
      feed.appendChild(li);
    }
  }

  init();
})();
