// ZeroDTE Terminal — production component app (Preact + htm, no build).
// Motion is purposeful only: count-ups, sparkline draw, staggered reveal — all
// disabled under prefers-reduced-motion. Live mode talks to the backend API;
// static mode (GitHub Pages) reads the published snapshot. Preact escapes all
// interpolated text by default → the old XSS render path is gone by construction.
import { h, render } from 'https://esm.sh/preact@10.19.3';
import { useState, useEffect, useRef, useCallback } from 'https://esm.sh/preact@10.19.3/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import gsap from 'https://esm.sh/gsap@3.12.5';

const html = htm.bind(h);

// ── Environment ─────────────────────────────────────────────────────────────
const STATIC = /\.github\.io$/i.test(location.hostname);
const SNAPSHOT_URL = 'https://raw.githubusercontent.com/xynkro/ZeroDTE/data/monitor.json';
const API = location.origin;          // backend serves this app same-origin in live mode
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const BT_MAX_DD = 1581;               // validated-backtest max drawdown anchor

// ── Format helpers ──────────────────────────────────────────────────────────
const money = (v, d = 0) => { const n = v || 0; return (n < 0 ? '−$' : '$') + Math.abs(n).toFixed(d); };
const signMoney = (v, d = 0) => { const n = v || 0; return (n >= 0 ? '+$' : '−$') + Math.abs(n).toFixed(d); };
const fmt = (v, d = 0) => (v == null ? '—' : (+v).toFixed(d));
const clsx = (...a) => a.filter(Boolean).join(' ');

// ── Data layer ──────────────────────────────────────────────────────────────
async function loadMonitor() {
  if (STATIC) {
    const s = await fetch(`${SNAPSHOT_URL}?t=${Date.now()}`, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('snapshot HTTP ' + r.status); return r.json(); });
    return {
      mode: 'static', generatedAt: s.generated_at,
      stats: s.stats || {}, debrief: s.debrief || {}, trades: s.trades || [], alpaca: s.alpaca || null,
    };
  }
  const [stats, debrief, trades, alpaca] = await Promise.all([
    fetch(`${API}/api/monitor/stats`).then(r => r.json()),
    fetch(`${API}/api/debrief`).then(r => r.json()).catch(() => ({})),
    fetch(`${API}/api/paper_trades`).then(r => r.json()).catch(() => []),
    fetch(`${API}/api/alpaca/status`).then(r => r.json()).catch(() => null),
  ]);
  return {
    mode: 'live', generatedAt: null,
    stats: stats || {}, debrief: debrief || {},
    trades: (trades || []).filter(t => t.strategy === 'directional_spread'), alpaca,
  };
}

// useResource — the loading/error/data state machine every view shares.
function useResource(loader, pollMs) {
  const [state, setState] = useState({ status: 'loading', data: null, error: null });
  const run = useCallback(() => {
    setState(s => ({ ...s, status: s.data ? 'refreshing' : 'loading' }));
    loader().then(
      data => setState({ status: 'ready', data, error: null }),
      err => setState(s => ({ status: s.data ? 'ready' : 'error', data: s.data, error: err.message || String(err) })),
    );
  }, [loader]);
  useEffect(() => {
    run();
    if (!pollMs) return;
    const id = setInterval(run, pollMs);
    return () => clearInterval(id);
  }, [run, pollMs]);
  return { ...state, reload: run };
}

// ── Motion primitives ───────────────────────────────────────────────────────
function Num({ value, format = (v) => fmt(v, 0), cls = '' }) {
  const ref = useRef(null);
  const prev = useRef(0);
  useEffect(() => {
    const el = ref.current; if (!el) return;
    const to = value || 0, from = prev.current; prev.current = to;
    if (REDUCED || from === to) { el.textContent = format(to); return; }
    const o = { v: from };
    const tw = gsap.to(o, { v: to, duration: 0.9, ease: 'power2.out', onUpdate() { el.textContent = format(o.v); } });
    return () => tw.kill();
  }, [value]);
  return html`<span ref=${ref} class=${clsx('num', cls)}>${format(value || 0)}</span>`;
}

function useStagger(dep) {
  const ref = useRef(null);
  useEffect(() => {
    if (REDUCED || !ref.current) return;
    const kids = ref.current.querySelectorAll('[data-stagger]');
    if (!kids.length) return;
    const tw = gsap.fromTo(kids, { opacity: 0, y: 10 },
      { opacity: 1, y: 0, duration: 0.5, ease: 'power2.out', stagger: 0.05, clearProps: 'opacity,transform' });
    return () => tw.kill();
  }, [dep]);
  return ref;
}

// ── UI primitives ───────────────────────────────────────────────────────────
const Card = ({ title, accent = 'var(--accent)', actions, children, cls }) => html`
  <section class=${clsx('card', cls)} data-stagger>
    ${title && html`<div class="card-h">
      <span class="accent-tab" style=${{ background: accent }}></span>
      <h2>${title}</h2><div class="spacer" style="flex:1"></div>${actions}
    </div>`}
    <div class="card-b">${children}</div>
  </section>`;

const Badge = ({ kind = 'neutral', children }) => html`<span class=${clsx('badge', kind)}>${children}</span>`;

const StatTile = ({ k, value, format, tone, hl }) => html`
  <div class=${clsx('stat', hl && 'hl')} style=${hl ? { '--accent': tone } : null}>
    <div class="k">${k}</div>
    <div class="v ${tone === 'var(--red)' ? 'neg' : tone === 'var(--green)' ? 'pos' : ''}"
         style=${tone && !hl ? { color: tone } : null}>
      <${Num} value=${value} format=${format} />
    </div>
  </div>`;

const Skeleton = ({ w = '100%', h = 14, r = 6, style }) =>
  html`<div class="sk" style=${{ width: w, height: typeof h === 'number' ? h + 'px' : h, borderRadius: r + 'px', ...style }}></div>`;

const EmptyState = ({ glyph = '○', title, hint }) => html`
  <div class="empty" role="status"><div class="glyph">${glyph}</div>
    <p><strong>${title}</strong></p>${hint && html`<p class="faint">${hint}</p>`}</div>`;

const ErrorState = ({ message, onRetry }) => html`
  <div class="errbox" role="alert"><div class="glyph">⚠</div>
    <p>${message || 'Something went wrong.'}</p>
    ${onRetry && html`<div class="retry"><button class="btn" onClick=${onRetry}>Retry</button></div>`}</div>`;

// ── Equity sparkline (SVG + GSAP draw) ──────────────────────────────────────
function Sparkline({ curve = [], height = 132 }) {
  const ref = useRef(null);
  const wrapRef = useRef(null);
  const [w, setW] = useState(640);
  useEffect(() => {
    const el = wrapRef.current; if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth || 640));
    ro.observe(el); setW(el.clientWidth || 640);
    return () => ro.disconnect();
  }, []);
  const pad = 8;
  const vals = curve.map(c => c.cum);
  if (!vals.length) return html`<div ref=${wrapRef}><${EmptyState} glyph="—" title="No closed trades yet" hint="The equity curve draws once trades resolve." /></div>`;
  const min = Math.min(0, ...vals), max = Math.max(0, ...vals), range = (max - min) || 1;
  const x = i => pad + (i * (w - 2 * pad)) / Math.max(vals.length - 1, 1);
  const y = v => height - pad - ((v - min) / range) * (height - 2 * pad);
  const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const zeroY = y(0);
  const last = vals[vals.length - 1];
  const stroke = last >= 0 ? 'var(--green)' : 'var(--red)';
  useEffect(() => {
    const p = ref.current; if (!p || REDUCED) return;
    const len = p.getTotalLength();
    const tw = gsap.fromTo(p, { strokeDasharray: len, strokeDashoffset: len },
      { strokeDashoffset: 0, duration: 1.1, ease: 'power2.out' });
    return () => tw.kill();
  }, [pts, w]);
  return html`
    <div ref=${wrapRef}>
      <svg width=${w} height=${height} viewBox=${`0 0 ${w} ${height}`} role="img" aria-label="Equity curve">
        <line x1=${pad} y1=${zeroY} x2=${w - pad} y2=${zeroY} stroke="var(--line)" stroke-dasharray="3 4" />
        <polygon points=${`${pad},${zeroY} ${pts} ${x(vals.length - 1)},${zeroY}`} fill=${last >= 0 ? 'var(--green-bg)' : 'var(--red-bg)'} />
        <polyline ref=${ref} points=${pts} fill="none" stroke=${stroke} stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round" />
      </svg>
    </div>`;
}

// ── Debrief ─────────────────────────────────────────────────────────────────
function DebriefCard({ d }) {
  if (!d || !d.date) return html`<${Card} title="Session Debrief"><${EmptyState} glyph="\u{1F50D}" title="No closed trades to debrief yet." /></${Card}>`;
  const sp = d.session_pnl || 0;
  const tone = sp >= 0 ? 'var(--green)' : 'var(--red)';
  const ddPct = Math.min(100, d.dd_vs_backtest_pct || 0);
  const ddColor = ddPct >= 100 ? 'var(--red)' : ddPct >= 70 ? 'var(--amber)' : 'var(--blue)';
  const fl = d.flags || {};
  return html`
    <${Card} title=${`Debrief · ${d.date}`} accent=${tone}
      actions=${html`<span class="muted" style="font-size:12px">${(d.trades || []).length} trade(s) · ${d.wins}W/${d.losses}L · <b style=${{ color: tone }}>${signMoney(sp)}</b></span>`}
      cls="debrief">
      ${(d.trades || []).map(a => html`
        <div class="row" key=${a.trade_no}>
          <span class="ic">${a.icon}</span>
          <span class="side">#${a.trade_no} ${a.side}</span>
          <span class=${a.pnl >= 0 ? 'pos num' : 'neg num'} style="min-width:54px">${signMoney(a.pnl)}</span>
          <span class="muted" style="flex:1">${a.note}</span>
        </div>`)}
      ${fl.directional_skew && html`<div style="color:var(--amber);font-size:12px;margin-top:8px">⚠ ${fl.directional_skew}${fl.vol_context ? ' · vol: ' + fl.vol_context : ''}</div>`}
      <div class="verdict"><strong>Verdict:</strong> ${d.verdict}</div>
      <div style="margin-top:10px">
        <div style="display:flex;justify-content:space-between;font-size:10px" class="faint">
          <span>drawdown vs backtest max (${money(BT_MAX_DD)})</span><span>${ddPct.toFixed(0)}%</span>
        </div>
        <div class="gauge"><i style=${{ width: ddPct + '%', background: ddColor }}></i></div>
      </div>
      <div class="faint" style="font-size:11px;margin-top:8px">${d.discipline}</div>
    </${Card}>`;
}

// ── Trade log ───────────────────────────────────────────────────────────────
function TradeTable({ trades = [] }) {
  if (!trades.length) return html`<${EmptyState} glyph="\u{1F4C8}" title="No directional-spread trades." hint="Entries appear here as they fire and resolve." />`;
  const rows = [...trades].sort((a, b) => (b.fired_at || '').localeCompare(a.fired_at || ''));
  const brokerKind = b => (b === 'submitted' || b === 'filled' || b === 'closed') ? 'ok' : b === 'error' ? 'bad' : 'neutral';
  return html`
    <div style="overflow-x:auto">
    <table class="tbl"><thead><tr>
      <th>#</th><th>In</th><th>Out</th><th>Side</th><th class="r">Short</th><th class="r">Long</th>
      <th>Outcome</th><th class="r">P&L</th><th class="r">Kept</th><th>Broker</th>
    </tr></thead><tbody>
      ${rows.map(t => html`
        <tr key=${t.trade_no} class=${(t.pnl || 0) > 0 ? 'win' : (t.pnl || 0) < 0 ? 'lose' : ''}>
          <td>${t.trade_no}</td>
          <td>${(t.fired_at || '').slice(11, 16)}</td>
          <td>${t.closed_at ? t.closed_at.slice(11, 16) : '—'}</td>
          <td style=${{ color: t.side === 'sell_call_cs' ? 'var(--red)' : 'var(--green)', fontWeight: 600 }}>
            ${t.side === 'sell_call_cs' ? 'CALL' : 'PUT'}</td>
          <td class="r">${fmt(t.short_strike, 0)}</td>
          <td class="r">${fmt(t.long_strike, 0)}</td>
          <td class="muted">${(t.outcome || 'open').replace(/_/g, ' ')}</td>
          <td class=${clsx('r', (t.pnl || 0) >= 0 ? 'pos' : 'neg')} style="font-weight:600">${signMoney(t.pnl || 0)}</td>
          <td class="r">${fmt(t.peak_pct_kept, 0)}%</td>
          <td><${Badge} kind=${brokerKind(t.broker_status)}>${t.broker_status || '—'}</${Badge}></td>
        </tr>`)}
    </tbody></table></div>`;
}

// ── Chrome ──────────────────────────────────────────────────────────────────
function ConnPill({ res, mode, generatedAt }) {
  if (mode === 'static') {
    const when = generatedAt ? new Date(generatedAt).toLocaleString() : '—';
    return html`<span class="pill"><span class="dot"></span>snapshot · ${when}</span>`;
  }
  const map = { loading: ['', 'connecting…'], refreshing: ['live', 'live'], ready: ['live', 'live'], error: ['err', 'backend offline'] };
  const [d, label] = map[res.status] || ['', ''];
  return html`<span class="pill"><span class=${clsx('dot', d)}></span>${label}</span>`;
}

function Topbar({ res, data }) {
  return html`
    <header class="topbar">
      <div class="brand">
        <span class="logo">Zero<b>DTE</b></span>
        <span class="tag">terminal</span>
      </div>
      <div class="spacer"></div>
      <${ConnPill} res=${res} mode=${data?.mode || (STATIC ? 'static' : 'live')} generatedAt=${data?.generatedAt} />
      <button class="btn ghost" onClick=${res.reload} aria-label="Refresh" title="Refresh">↻</button>
    </header>`;
}

// ── Monitor view ────────────────────────────────────────────────────────────
function StatsRow({ s }) {
  const wr = s.wr_pct || 0, pnl = s.total_pnl || 0, pf = s.profit_factor || 0;
  return html`
    <div class="stats">
      <${StatTile} k="Total P&L" value=${pnl} tone=${pnl >= 0 ? 'var(--green)' : 'var(--red)'} hl=${true}
                   format=${v => signMoney(v)} />
      <${StatTile} k="Win Rate" value=${wr} tone=${wr >= 60 ? 'var(--green)' : 'var(--amber)'} format=${v => v.toFixed(0) + '%'} />
      <${StatTile} k="Trades" value=${s.total || 0} format=${v => v.toFixed(0)} />
      <${StatTile} k="Profit Factor" value=${pf >= 999 ? 0 : pf} format=${v => (pf >= 999 ? '∞' : v.toFixed(2))} />
      <${StatTile} k="Avg Win" value=${s.avg_win || 0} tone="var(--green)" format=${v => signMoney(v)} />
      <${StatTile} k="Avg Loss" value=${s.avg_loss || 0} tone="var(--red)" format=${v => signMoney(v)} />
      <${StatTile} k="Max Drawdown" value=${s.max_drawdown || 0} format=${v => money(v)} />
      <${StatTile} k="Wins / Losses" value=${s.wins || 0} format=${() => `${s.wins || 0} / ${s.losses || 0}`} />
    </div>`;
}

function MonitorSkeleton() {
  return html`<div class="grid" style="margin-top:16px">
    <div class="stats">${Array.from({ length: 8 }).map((_, i) => html`<div class="stat" key=${i}><${Skeleton} w="55%" h=10 /><div style="height:8px"></div><${Skeleton} w="80%" h=20 /></div>`)}</div>
    <div class="grid cols-2">
      <div class="card"><div class="card-b">${Array.from({ length: 5 }).map((_, i) => html`<div key=${i} style="margin:9px 0"><${Skeleton} w=${`${90 - i * 8}%`} h=13 /></div>`)}</div></div>
      <div class="card"><div class="card-b"><${Skeleton} h=132 r=8 /></div></div>
    </div>
    <div class="card"><div class="card-b">${Array.from({ length: 4 }).map((_, i) => html`<div key=${i} style="margin:10px 0"><${Skeleton} h=14 /></div>`)}</div></div>
  </div>`;
}

function MonitorView() {
  const res = useResource(loadMonitor, STATIC ? 60000 : 8000);
  const staggerRef = useStagger(res.status === 'ready' ? (res.data?.generatedAt || res.data?.stats?.total) : null);

  return html`
    <div class="app">
      <${Topbar} res=${res} data=${res.data} />
      ${res.status === 'loading' && html`<${MonitorSkeleton} />`}
      ${res.status === 'error' && html`<div style="margin-top:24px"><${ErrorState} message=${'Could not reach the backend. ' + (res.error || '')} onRetry=${res.reload} /></div>`}
      ${res.data && html`
        <div ref=${staggerRef} class="grid" style="margin-top:8px" aria-busy=${res.status === 'refreshing'}>
          ${res.data.mode === 'static' && html`<div class="banner" data-stagger>\u{1F4F8} <span><strong>Read-only snapshot</strong> — live trading runs on the backend.</span></div>`}
          <div data-stagger><${StatsRow} s=${res.data.stats || {}} /></div>
          <div class="grid cols-2">
            <${DebriefCard} d=${res.data.debrief} />
            <${Card} title="Equity Curve" accent="var(--blue)"><${Sparkline} curve=${(res.data.stats || {}).equity_curve || []} /></${Card}>
          </div>
          <${Card} title="Trade Log" accent="var(--violet)"
            actions=${res.data.alpaca && html`<${Badge} kind=${res.data.alpaca.enabled ? 'ok' : 'neutral'}>${res.data.alpaca.enabled ? 'broker active' : 'broker off'}</${Badge}>`}>
            <${TradeTable} trades=${res.data.trades} />
          </${Card}>
        </div>`}
    </div>`;
}

const root = document.getElementById('app');
root.removeAttribute('aria-busy');
render(html`<${MonitorView} />`, root);
