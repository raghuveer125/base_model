// Trading_Plug&Play — minimal vanilla-JS UI.

(() => {
  const $ = (id) => document.getElementById(id);

  const state = {
    indices: [],
    current: null,
    ws: null,
    reconnectTimer: null,
    expiry: null,
    chain: {},   // { strike: { CE: {tick, greeks}, PE: {tick, greeks} } }
    atm: null,
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
    state.indices.forEach((idx) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = idx;
      btn.addEventListener("click", () => switchTab(idx));
      if (idx === state.current) btn.classList.add("active");
      tabs.appendChild(btn);
    });
  }

  async function switchTab(idx) {
    if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    state.current = idx;
    state.chain = {};
    $("chain-tbody").innerHTML = "";
    $("candles").innerHTML = "";
    $("spot").textContent = "—";
    $("last-seen").textContent = "—";
    renderTabs();
    await fetchState(idx);
    await loadChain();
    connectWs(idx);
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

  init();
})();
