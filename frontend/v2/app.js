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
// '2026-06-09' → 'Tue 9 Jun' (ISO YYYY-MM-DD reads ambiguous; all dates are ET days)
const fmtDay = (iso) => {
  try {
    const d = new Date((iso || '').slice(0, 10) + 'T12:00:00');
    if (isNaN(d)) return iso || '—';
    return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
  } catch (e) { return iso || '—'; }
};

// ── Data layer ──────────────────────────────────────────────────────────────
async function loadMonitor() {
  if (STATIC) {
    const s = await fetch(`${SNAPSHOT_URL}?t=${Date.now()}`, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('snapshot HTTP ' + r.status); return r.json(); });
    return {
      mode: 'static', generatedAt: s.generated_at,
      stats: s.stats || {}, debrief: s.debrief || {}, trades: s.trades || [], alpaca: s.alpaca || null,
      today: (s.signals || {}).today || null, meicHistory: s.meic_history || null,
    };
  }
  const [stats, debrief, trades, alpaca, today, meicHistory] = await Promise.all([
    fetch(`${API}/api/monitor/stats`).then(r => r.json()),
    fetch(`${API}/api/debrief`).then(r => r.json()).catch(() => ({})),
    fetch(`${API}/api/paper_trades`).then(r => r.json()).catch(() => []),
    fetch(`${API}/api/alpaca/status`).then(r => r.json()).catch(() => null),
    fetch(`${API}/api/signals`).then(r => r.json()).then(d => d.today).catch(() => null),
    fetch(`${API}/api/meic/history`).then(r => r.json()).catch(() => null),
  ]);
  return {
    mode: 'live', generatedAt: null,
    stats: stats || {}, debrief: debrief || {},
    trades: (trades || []).filter(t => t.strategy === 'directional_spread'), alpaca, today, meicHistory,
  };
}

async function loadSignals() {
  if (STATIC) {
    const s = await fetch(`${SNAPSHOT_URL}?t=${Date.now()}`, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('snapshot HTTP ' + r.status); return r.json(); });
    return { mode: 'static', _snapAt: s.generated_at, ...(s.signals || {}) };
  }
  const d = await fetch(`${API}/api/signals`).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
  return { mode: 'live', ...d };
}

async function loadPlaybook() {
  if (STATIC) {
    const s = await fetch(`${SNAPSHOT_URL}?t=${Date.now()}`, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('snapshot HTTP ' + r.status); return r.json(); });
    return s.playbook || {};
  }
  return fetch(`${API}/api/playbook`).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
}

async function loadWave() {
  if (STATIC) {
    const s = await fetch(`${SNAPSHOT_URL}?t=${Date.now()}`, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('snapshot HTTP ' + r.status); return r.json(); });
    return {
      mode: 'static', generatedAt: s.generated_at,
      history: s.wave_history || null, debrief: s.debrief || {},
      trades: (s.trades || []).filter(t => t.strategy === 'directional_spread'),
    };
  }
  const [history, debrief, trades] = await Promise.all([
    fetch(`${API}/api/wave/history`).then(r => r.json()).catch(() => null),
    fetch(`${API}/api/debrief`).then(r => r.json()).catch(() => ({})),
    fetch(`${API}/api/paper_trades`).then(r => r.json()).catch(() => []),
  ]);
  return {
    mode: 'live', generatedAt: null, history, debrief: debrief || {},
    trades: (trades || []).filter(t => t.strategy === 'directional_spread'),
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

// ── Write layer ─────────────────────────────────────────────────────────────
// Live mode: POST to the backend with the injected write token. The dashboard
// is served by the backend, which injects window.__ZDT for the operator only;
// any other caller is 401'd.
const API_TOKEN = (typeof window !== 'undefined' && window.__ZDT) || '';
async function apiWrite(path, body) {
  const r = await fetch(API + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-ZeroDTE-Token': API_TOKEN },
    body: body != null ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) throw new Error('unauthorized — open this from the backend (localhost / Tailscale), not Pages');
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

// Static (Pages) mode: writes go through the GitOps control branch using the
// operator's own GitHub token, held in sessionStorage (clears on close) — NOT
// localStorage. The XSS path that could exfiltrate it is gone (Preact escapes).
const GH_API = 'https://api.github.com/repos/xynkro/ZeroDTE';
function ghToken(force) {
  let t = sessionStorage.getItem('zerodte_gh_token');
  if (!t || force) {
    t = prompt('GitHub fine-grained token (repo: xynkro/ZeroDTE · Contents: Read & write).\nStored for THIS SESSION only.\n\nCreate: github.com/settings/tokens?type=beta');
    if (t) { t = t.trim(); sessionStorage.setItem('zerodte_gh_token', t); }
  }
  return t;
}
const _b64e = s => btoa(unescape(encodeURIComponent(s)));
const _b64d = s => decodeURIComponent(escape(atob((s || '').replace(/\n/g, ''))));
async function ghGetControl(t) {
  const r = await fetch(`${GH_API}/contents/control.json?ref=control&t=${Date.now()}`,
    { headers: { Authorization: 'token ' + t, Accept: 'application/vnd.github+json' }, cache: 'no-store' });
  if (r.status === 404) return { sha: null, json: { v: 0 } };
  if (!r.ok) { const e = new Error('GitHub GET ' + r.status); e.status = r.status; throw e; }
  const d = await r.json();
  let j = { v: 0 }; try { j = JSON.parse(_b64d(d.content)); } catch (e) {}
  return { sha: d.sha, json: j };
}
async function ghPutControl(t, sha, obj, msg) {
  const body = { message: msg, branch: 'control', content: _b64e(JSON.stringify(obj, null, 2)) };
  if (sha) body.sha = sha;
  const r = await fetch(`${GH_API}/contents/control.json`, {
    method: 'PUT', headers: { Authorization: 'token ' + t, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) { const x = await r.text(); const e = new Error('GitHub PUT ' + r.status + ' ' + x.slice(0, 90)); e.status = r.status; throw e; }
  return r.json();
}
async function savePrefs(prefs) {
  if (!STATIC) { await apiWrite('/api/telegram/prefs', prefs); return { via: 'api' }; }
  const t = ghToken(false);
  if (!t) throw new Error('cancelled (no token)');
  try {
    const cur = await ghGetControl(t);
    const obj = Object.assign({}, cur.json, { v: (cur.json.v || 0) + 1, telegram_prefs: prefs });
    await ghPutControl(t, cur.sha, obj, 'control: telegram prefs v' + obj.v);
    return { via: 'gitops' };
  } catch (e) { if (e.status === 401 || e.status === 403) sessionStorage.removeItem('zerodte_gh_token'); throw e; }
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

const Toggle = ({ checked, onChange, label }) => html`
  <label class="tog"><input type="checkbox" checked=${!!checked} onChange=${e => onChange(e.target.checked)} />
    <span class="sw" aria-hidden="true"></span><span>${label}</span></label>`;

function Modal({ title, onClose, children, footer }) {
  const ref = useRef(null);
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    const prev = document.activeElement;
    ref.current?.querySelector('button,input,select,[tabindex]')?.focus();
    return () => { document.removeEventListener('keydown', onKey); prev?.focus?.(); };
  }, []);
  return html`
    <div class="scrim" onMouseDown=${e => { if (e.target.classList.contains('scrim')) onClose(); }}>
      <div class="sheet" ref=${ref} role="dialog" aria-modal="true" aria-label=${title}>
        <div class="sheet-h"><h2>${title}</h2><button class="x" aria-label="Close" onClick=${onClose}>×</button></div>
        <div class="sheet-b">${children}</div>
        ${footer && html`<div class="sheet-f">${footer}</div>`}
      </div>
    </div>`;
}

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
      <svg viewBox=${`0 0 ${w} ${height}`} preserveAspectRatio="none" role="img" aria-label="Equity curve"
           style=${{ width: '100%', height: height + 'px', display: 'block' }}>
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
    <${Card} title=${`Last trade · ${fmtDay(d.date)}`} accent=${tone}
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

// ── Telegram settings sheet (dual write: API live, GitOps on Pages) ─────────
function SettingsSheet({ onClose }) {
  const [prefs, setPrefs] = useState(null);
  const [types, setTypes] = useState([]);
  const [status, setStatus] = useState('loading');   // loading | ready | error | saving
  const [msg, setMsg] = useState('');
  useEffect(() => { (async () => {
    try {
      let p, t;
      if (STATIC) { const s = await fetch(`${SNAPSHOT_URL}?t=${Date.now()}`, { cache: 'no-store' }).then(r => r.json()); p = s.telegram_prefs || {}; t = s.telegram_types || []; }
      else { const d = await fetch(`${API}/api/telegram/prefs`).then(r => r.json()); p = d.prefs || {}; t = d.message_types || []; }
      setPrefs(p); setTypes(t); setStatus('ready');
    } catch (e) { setStatus('error'); setMsg(e.message); }
  })(); }, []);
  const upd = (fn) => setPrefs(prev => { const n = structuredClone(prev); fn(n); return n; });
  const save = async () => {
    setStatus('saving'); setMsg('');
    try { const r = await savePrefs(prefs); setStatus('ready'); setMsg(r.via === 'gitops' ? '✓ saved — backend applies within ~1 min' : '✓ saved — applies on next alert'); }
    catch (e) { setStatus('ready'); setMsg('save failed: ' + e.message); }
  };
  const link = prefs?.link || {}, detail = prefs?.detail || {}, tmap = prefs?.types || {};
  const footer = html`
    <button class="btn primary" onClick=${save} disabled=${status === 'saving'}>${status === 'saving' ? 'Saving…' : 'Save'}</button>
    <span class="muted" style="font-size:11.5px;flex:1">${msg}</span>`;
  return html`
    <${Modal} title="Telegram settings" onClose=${onClose} footer=${prefs ? footer : null}>
      ${status === 'loading' && html`<${Skeleton} h=160 r=8 />`}
      ${status === 'error' && html`<${ErrorState} message=${msg} />`}
      ${prefs && html`<div>
        ${STATIC && html`<div class="note-blue">\u{1F4F1} Phone mode — saves go via GitHub; your backend applies them within ~1 min. First save asks for a GitHub token (this session only).</div>`}
        <div class="field"><label>Push these alerts</label>
          <div class="toggle-grid">
            ${types.map(t => html`<${Toggle} key=${t.key} label=${t.label} checked=${tmap[t.key] !== false}
              onChange=${v => upd(n => { (n.types = n.types || {})[t.key] = v; })} />`)}
          </div>
        </div>
        <div class="field"><label>Prefix — top of every alert</label>
          <input class="inp" value=${prefs.prefix || ''} placeholder="(none)" onInput=${e => upd(n => n.prefix = e.target.value)} /></div>
        <div class="field"><label>Footer — bottom of every alert</label>
          <input class="inp" value=${prefs.footer || ''} placeholder="(none)" onInput=${e => upd(n => n.footer = e.target.value)} /></div>
        <div class="field"><label>Dashboard link</label>
          <${Toggle} label="Include the dashboard link" checked=${link.enabled !== false} onChange=${v => upd(n => { (n.link = n.link || {}).enabled = v; })} />
          <div class="seg" style="margin-top:9px">
            ${[['plain', 'plain URL'], ['button', 'tappable button']].map(([s, lbl]) => html`
              <button key=${s} class=${(link.style || 'plain') === s ? 'on' : ''} onClick=${() => upd(n => { (n.link = n.link || {}).style = s; })}>${lbl}</button>`)}
          </div>
        </div>
        <div class="field"><label>Entry detail lines</label>
          <div class="toggle-grid">
            ${['factors', 'plan', 'sizing'].map(k => html`<${Toggle} key=${k} label=${k} checked=${detail[k] !== false}
              onChange=${v => upd(n => { (n.detail = n.detail || {})[k] = v; })} />`)}
          </div>
        </div>
      </div>`}
    </${Modal}>`;
}

// ── Kill switch — live only (backend-served); guarded; never on Pages ───────
function KillButton() {
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState('');
  const kill = async () => {
    if (!confirm('KILL SWITCH\n\nHalt the strategy, disable the broker, and flatten ALL open positions. Continue?')) return;
    setBusy(true); setRes('');
    try { const r = await apiWrite('/api/alpaca/kill'); setRes(r.ok ? 'halted' : ('err: ' + (r.error || ''))); }
    catch (e) { setRes('failed'); console.error(e); }
    setBusy(false);
  };
  return html`<button class="btn danger" onClick=${kill} disabled=${busy} title="Emergency kill — halt + flatten">
    ${busy ? '…' : '■ Kill'}${res ? ' · ' + res : ''}</button>`;
}

// ── Chrome ──────────────────────────────────────────────────────────────────
// Per-book tab sets. The two books (MEIC condors, Wave directional spreads) share
// one backend/account/Telegram but get fully separate UIs via the top-level switch.
const TABS = {
  meic: [['dashboard', 'Dashboard'], ['signals', 'Signals'], ['backtest', 'Backtest'], ['playbook', 'Playbook'], ['macro', 'Macro']],
  wave: [['dashboard', 'Dashboard'], ['signals', 'Signals'], ['backtest', 'Backtest'], ['playbook', 'Playbook'], ['macro', 'Macro']],
};

const BookSwitch = ({ book, setBook }) => html`
  <div class="seg bookseg" role="tablist" aria-label="Strategy book">
    ${[['meic', '🦅 MEIC'], ['wave', '🌊 Wave']].map(([k, label]) => html`<button key=${k}
      class=${book === k ? 'on' : ''} role="tab" aria-selected=${book === k}
      onClick=${() => setBook(k)}>${label}</button>`)}
  </div>`;

const Nav = ({ book, view, setView }) => html`
  <nav class="nav" aria-label="Views">
    ${TABS[book].map(([k, label]) => html`<button key=${k} class=${view === k ? 'on' : ''}
      aria-current=${view === k ? 'page' : null} onClick=${() => setView(k)}>${label}</button>`)}
  </nav>`;

const ModePill = () => STATIC
  ? html`<span class="pill"><span class="dot"></span>snapshot</span>`
  : html`<span class="pill"><span class="dot live"></span>live</span>`;

function Topbar({ book, setBook, view, setView, onSettings }) {
  return html`
    <header class="topbar">
      <div class="brand"><img class="wordmark" src="./wordmark.png" alt="ZeroDTE" /><span class="tag">terminal</span></div>
      <${BookSwitch} book=${book} setBook=${setBook} />
      <div class="spacer"></div>
      <${Nav} book=${book} view=${view} setView=${setView} />
      <${ModePill} />
      ${!STATIC && html`<${KillButton} />`}
      <button class="btn ghost icon-btn" onClick=${onSettings} aria-label="Telegram settings" title="Telegram settings">⚙</button>
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

// MEIC condor track record — the cumulative real-P&L picture the app was missing.
// Shows EVERY night (green + red) so the loud red Telegram pings aren't the only
// thing the operator sees. Judge the cumulative, not any single night.
function MeicTrackRecord({ h }) {
  const nights = (h && h.nights) || [];
  const sm = (h && h.summary) || {};
  if (!nights.length) return html`<${Card} title="MEIC Track Record" accent="var(--amber)">
    <${EmptyState} glyph="\u{1F985}" title="No condor nights recorded yet"
      hint="Each night's real Alpaca-fill P&L lands here after the close." /></${Card}>`;
  const total = sm.total || 0;
  const tone = total >= 0 ? 'var(--green)' : 'var(--red)';
  const curve = nights.map(n => ({ cum: n.cumulative }));
  return html`<${Card} title="MEIC Track Record" accent=${tone}
    actions=${html`<span class="muted" style="font-size:12px"><b style=${{ color: total >= 0 ? 'var(--green)' : 'var(--red)' }}>${signMoney(total)}</b> · ${sm.green}/${sm.n} green nights</span>`}>
    <div class="stats" style="margin-bottom:14px">
      <${StatTile} k="Net (real $)" value=${total} tone=${tone} hl=${true} format=${v => signMoney(v)} />
      <${StatTile} k="Green / Nights" value=${sm.green || 0} format=${() => `${sm.green || 0} / ${sm.n || 0}`} />
      <${StatTile} k="Best night" value=${sm.best || 0} tone="var(--green)" format=${v => signMoney(v)} />
      <${StatTile} k="Worst night" value=${sm.worst || 0} tone="var(--red)" format=${v => signMoney(v)} />
    </div>
    <${Sparkline} curve=${curve} height=${96} />
    <div style="overflow-x:auto;margin-top:10px">
    <table class="tbl"><thead><tr>
      <th>Night</th><th class="r">Fired</th><th class="r">Stop%</th><th class="r">P&L</th><th class="r">Cumul.</th><th class="r">Peak/Dip</th><th class="r">Slip</th>
    </tr></thead><tbody>
      ${[...nights].reverse().map(n => html`<tr key=${n.date} class=${n.real_net > 0 ? 'win' : n.real_net < 0 ? 'lose' : ''}>
        <td>${fmtDay(n.date)}</td>
        <td class="r">${n.executed}</td>
        <td class="r muted">${n.stop_rate != null ? Math.round(n.stop_rate) + '%' : '—'}</td>
        <td class=${clsx('r', n.real_net >= 0 ? 'pos' : 'neg')} style="font-weight:600">${signMoney(n.real_net)}</td>
        <td class=${clsx('r', n.cumulative >= 0 ? 'pos' : 'neg')}>${signMoney(n.cumulative)}</td>
        <td class="r faint">${n.mfe_med_pct != null ? html`<span class="pos">+${Math.round(n.mfe_med_pct)}%</span>/<span class="neg">${Math.round(n.mae_med_pct)}%</span>` : '—'}</td>
        <td class="r faint">${n.slippage_pct != null ? (n.slippage_pct > 0 ? '+' : '') + Math.round(n.slippage_pct) + '%' : '—'}</td>
      </tr>`)}
    </tbody></table></div>
    <div class="faint" style="font-size:11px;margin-top:9px;line-height:1.5">Real Alpaca-fill P&L · SPY-scale @ 1ct/slot. Many small reds + occasional bigger greens = the condor asymmetry working as designed — judge the cumulative, not one night.<br/><b>Peak/Dip</b> = median MFE/MAE per night (% of credit): how much profit the book reached vs how deep it dipped toward breach. Healthy = peak near +100%, dip shallow — winners capture full decay and the breakeven stop stays clear of recoverable trades.</div>
  </${Card}>`;
}

function WaveTrackRecord({ h }) {
  const nights = (h && h.nights) || [];
  const sm = (h && h.summary) || {};
  if (!nights.length) return html`<${Card} title="Wave Track Record" accent="var(--blue)">
    <${EmptyState} glyph="\u{1F30A}" title="No directional nights recorded yet"
      hint="Each night's Wave P&L lands here after the close." /></${Card}>`;
  const total = sm.total || 0;
  const tone = total >= 0 ? 'var(--green)' : 'var(--red)';
  const curve = nights.map(n => ({ cum: n.cumulative }));
  return html`<${Card} title="Wave Track Record" accent=${tone}
    actions=${html`<span class="muted" style="font-size:12px"><b style=${{ color: total >= 0 ? 'var(--green)' : 'var(--red)' }}>${signMoney(total)}</b> est · ${sm.green}/${sm.n} green nights</span>`}>
    <div class="banner" style="margin-bottom:12px">\u{26A0}️ <span><strong>P&L is model-estimated</strong>, not real Alpaca fills yet — entries execute on the broker but exit P&L is computed by the model. Real-fill capture is the next fix; treat these as directional, not realized.</span></div>
    <div class="stats" style="margin-bottom:14px">
      <${StatTile} k="Net (est $)" value=${total} tone=${tone} hl=${true} format=${v => signMoney(v)} />
      <${StatTile} k="Green / Nights" value=${sm.green || 0} format=${() => `${sm.green || 0} / ${sm.n || 0}`} />
      <${StatTile} k="Win rate" value=${sm.win_rate || 0} format=${() => sm.win_rate != null ? `${sm.win_rate}% (${sm.wins}/${sm.trades})` : '—'} />
      <${StatTile} k="Best / Worst" value=${sm.best || 0} tone=${tone} format=${() => `${signMoney(sm.best)} / ${signMoney(sm.worst)}`} />
    </div>
    <${Sparkline} curve=${curve} height=${96} />
    <div style="overflow-x:auto;margin-top:10px">
    <table class="tbl"><thead><tr>
      <th>Night</th><th class="r">Trades</th><th class="r">Wins</th><th class="r">P&L (est)</th><th class="r">Cumul.</th>
    </tr></thead><tbody>
      ${[...nights].reverse().map(n => html`<tr key=${n.date} class=${n.pnl > 0 ? 'win' : n.pnl < 0 ? 'lose' : ''}>
        <td>${fmtDay(n.date)}</td>
        <td class="r">${n.n}</td>
        <td class="r muted">${n.wins != null ? n.wins : '—'}</td>
        <td class=${clsx('r', n.pnl >= 0 ? 'pos' : 'neg')} style="font-weight:600">${signMoney(n.pnl)}</td>
        <td class=${clsx('r', n.cumulative >= 0 ? 'pos' : 'neg')}>${signMoney(n.cumulative)}</td>
      </tr>`)}
    </tbody></table></div>
    <div class="faint" style="font-size:11px;margin-top:9px;line-height:1.5">Directional credit spreads (SPY-scale). Edge validated ~96% on PUTS; CALL trades are the unproven side. Judge the cumulative, not one night — N is still small.</div>
  </${Card}>`;
}

function _waveSide(s) { return s === 'sell_call_cs' ? 'CALL' : s === 'sell_put_cs' ? 'PUT' : (s || '—'); }

function WaveTodayTrades({ trades }) {
  const ts = [...(trades || [])].sort((a, b) => (b.fired_at || '').localeCompare(a.fired_at || ''));
  if (!ts.length) return null;
  return html`<${Card} title="Wave Trades (recent)" accent="var(--violet)">
    <div style="overflow-x:auto">
    <table class="tbl"><thead><tr>
      <th>Time</th><th>Side</th><th class="r">Ctr</th><th class="r">Credit (real)</th><th class="r">P&L</th><th class="r">Basis</th><th>Outcome</th>
    </tr></thead><tbody>
      ${ts.slice(0, 12).map((t, i) => {
        const real = t.broker_realized_credit;
        const rpnl = t.broker_realized_pnl;            // real-fill P&L when captured
        const verified = rpnl != null;
        const closeErr = t.broker_status === 'close_error';
        const pnl = verified ? rpnl : t.pnl;            // prefer real, fall back to model
        return html`<tr key=${t.fired_at + i} class=${(pnl || 0) > 0 ? 'win' : (pnl || 0) < 0 ? 'lose' : ''}>
          <td class="muted">${(t.fired_at || '').slice(5, 16).replace('T', ' ')}</td>
          <td>${_waveSide(t.side)}</td>
          <td class="r muted">${t.contracts || '—'}</td>
          <td class="r">${real != null ? signMoney(real) : '—'}</td>
          <td class=${clsx('r', (pnl || 0) >= 0 ? 'pos' : 'neg')} style="font-weight:600">${pnl != null ? signMoney(pnl) : '—'}</td>
          <td class="r" style="font-size:10px">${verified ? html`<span class="pos">real</span>` : closeErr ? html`<span class="neg">close_err</span>` : html`<span class="faint">model</span>`}</td>
          <td class="faint" style="font-size:11px">${t.exit_reason || t.outcome || (t.closed ? 'closed' : 'open')}</td>
        </tr>`;
      })}
    </tbody></table></div>
    <div class="faint" style="font-size:11px;margin-top:9px;line-height:1.5"><b>P&L</b> shows the <b class="pos">real</b> Alpaca entry+exit fill when captured; <b class="faint">model</b> = estimate (no real exit yet); <b class="neg">close_err</b> = the broker close FAILED, so the real P&L is unverified and the position rode on. Trust the <b class="pos">real</b> rows.</div>
  </${Card}>`;
}

function WaveView() {
  const res = useResource(loadWave, STATIC ? 60000 : 8000);
  const staggerRef = useStagger(res.status === 'ready' ? (res.data?.generatedAt || (res.data?.history?.summary?.n)) : null);
  if (res.status === 'loading') return html`<${MonitorSkeleton} />`;
  if (res.status === 'error') return html`<div style="margin-top:24px"><${ErrorState} message=${'Could not reach the backend. ' + (res.error || '')} onRetry=${res.reload} /></div>`;
  const d = res.data;
  return html`
    <div ref=${staggerRef} class="grid" style="margin-top:8px" aria-busy=${res.status === 'refreshing'}>
      ${d.mode === 'static' && html`<div class="banner" data-stagger>\u{1F4F8} <span><strong>Read-only snapshot</strong>${d.generatedAt ? ' · ' + new Date(d.generatedAt).toLocaleString() : ''} — live trading runs on the backend.</span></div>`}
      <div data-stagger><${WaveTrackRecord} h=${d.history} /></div>
      <div data-stagger><${WaveTodayTrades} trades=${d.trades} /></div>
      <div data-stagger><${DebriefCard} d=${d.debrief} /></div>
    </div>`;
}

// MEIC dashboard — the condor book only (Wave moved to its own book).
function MeicDashboard() {
  const res = useResource(loadMonitor, STATIC ? 60000 : 8000);
  const staggerRef = useStagger(res.status === 'ready' ? (res.data?.generatedAt || res.data?.meicHistory?.summary?.total) : null);
  if (res.status === 'loading') return html`<${MonitorSkeleton} />`;
  if (res.status === 'error') return html`<div style="margin-top:24px"><${ErrorState} message=${'Could not reach the backend. ' + (res.error || '')} onRetry=${res.reload} /></div>`;
  const d = res.data;
  return html`
    <div ref=${staggerRef} class="grid" style="margin-top:8px" aria-busy=${res.status === 'refreshing'}>
      ${d.mode === 'static' && html`<div class="banner" data-stagger>\u{1F4F8} <span><strong>Read-only snapshot</strong>${d.generatedAt ? ' · ' + new Date(d.generatedAt).toLocaleString() : ''} — live trading runs on the backend.</span></div>`}
      ${d.today && html`<div data-stagger><${TodayCard} t=${d.today} /></div>`}
      <div data-stagger><${MeicTrackRecord} h=${d.meicHistory} /></div>
    </div>`;
}

// ── Macro view (live-only — needs the backend feed) ─────────────────────────
const liveOnly = (glyph, title) => html`<div style="margin-top:16px"><${Card} title=${title}>
  <${EmptyState} glyph=${glyph} title=${title + ' is live-only'}
    hint="Open the backend dashboard (Mac / Tailscale) — this needs the live feed, not the phone snapshot." /></${Card}></div>`;

function MacroView() {
  if (STATIC) return liveOnly('\u{1F4F0}', 'Macro');
  const load = useCallback(async () => {
    const [n, c] = await Promise.all([
      fetch(`${API}/api/macro/news`).then(r => r.json()).then(x => x.news || []),
      fetch(`${API}/api/macro/calendar`).then(r => r.json()).catch(() => ({})),
    ]);
    return { news: n, calendar: c.calendar || [], calStatus: c.status || null };
  }, []);
  const res = useResource(load, 120000);
  if (res.status === 'loading') return html`<div class="grid cols-2" style="margin-top:16px">
    ${[0, 1].map(i => html`<div class="card" key=${i}><div class="card-b">${Array.from({ length: 6 }).map((_, j) => html`<div key=${j} style="margin:10px 0"><${Skeleton} w=${`${85 - j * 6}%`} h=13 /></div>`)}</div></div>`)}</div>`;
  if (res.status === 'error') return html`<div style="margin-top:16px"><${ErrorState} message=${res.error} onRetry=${res.reload} /></div>`;
  const { news = [], calendar = [], calStatus = null } = res.data || {};
  return html`<div class="grid cols-2" style="margin-top:16px">
    <${Card} title="Market News" accent="var(--blue)">
      ${!news.length ? html`<${EmptyState} glyph="\u{1F4F0}" title="No recent headlines." />`
        : news.slice(0, 30).map((n, i) => html`<div class="news-item" key=${i}>
            <div class="news-meta"><span>${n.datetime ? new Date(n.datetime).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}</span><span>${n.source || ''}</span></div>
            <div class="news-head">${n.url ? html`<a href=${n.url} target="_blank" rel="noopener noreferrer">${n.headline}</a>` : n.headline}</div>
            ${n.summary && html`<div class="news-sum">${n.summary.slice(0, 220)}</div>`}
          </div>`)}
    </${Card}>
    <${Card} title="Economic Calendar" accent="var(--amber)">
      ${(calStatus && !calStatus.available)
        ? html`<${EmptyState} glyph="\u{1F512}" title="Calendar unavailable" hint=${calStatus.reason || 'the economic-calendar source is not accessible'} />`
        : !calendar.length ? html`<${EmptyState} glyph="\u{1F5D3}" title="No upcoming US events." />`
        : html`<div><div class="cal-h"><span>When</span><span>Impact</span><span>Event</span><span class="r">Est</span><span class="r">Prev</span></div>
          ${calendar.slice(0, 40).map((e, i) => html`<div class=${clsx('cal-row', e.impact || 'low')} key=${i}>
            <span class="muted">${(e.time || '').slice(5, 16)}</span>
            <span class="imp">${e.impact || 'low'}</span>
            <span>${e.event}</span>
            <span class="num r">${e.estimate ?? '—'}</span>
            <span class="num r">${e.prev ?? '—'}</span>
          </div>`)}</div>`}
    </${Card}>
  </div>`;
}

// ── Backtest view (live-only — needs the backend + historical data) ─────────
function BacktestView() {
  if (STATIC) return liveOnly('\u{1F9EA}', 'Backtest');
  const [p, setP] = useState({ target_delta: 30, final_tp_target: 90, use_dynamic_stops: false, data_window: '3y' });
  const [res, setRes] = useState({ status: 'idle', data: null, error: null });
  const run = async () => {
    setRes({ status: 'loading', data: null, error: null });
    try {
      const q = new URLSearchParams({ target_delta: p.target_delta, final_tp_target: p.final_tp_target, use_dynamic_stops: p.use_dynamic_stops, data_window: p.data_window });
      const d = await fetch(`${API}/api/backtest/honest?${q}`).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
      if (d.error) throw new Error(d.error);
      setRes({ status: 'ready', data: d, error: null });
    } catch (e) { setRes({ status: 'error', data: null, error: e.message }); }
  };
  useEffect(() => { run(); }, []);
  const upd = (k, v) => setP(prev => ({ ...prev, [k]: v }));
  const s = res.data?.summary || {};
  let cum = 0; const curve = (res.data?.trades || []).map(t => ({ cum: (cum += t.pnl || 0) }));
  return html`<div class="grid" style="margin-top:16px">
    <${Card} title="Honest backtest — Black-Scholes (the validated engine)" accent="var(--green)">
      <div class="bt-form">
        <div class="field"><label>Short Δ</label><input class="inp num" type="number" value=${p.target_delta} onInput=${e => upd('target_delta', +e.target.value)} /></div>
        <div class="field"><label>TP %</label><input class="inp num" type="number" value=${p.final_tp_target} onInput=${e => upd('final_tp_target', +e.target.value)} /></div>
        <div class="field"><label>Stop ladder</label><${Toggle} label=${p.use_dynamic_stops ? 'on' : 'off'} checked=${p.use_dynamic_stops} onChange=${v => upd('use_dynamic_stops', v)} /></div>
        <div class="field"><label>Window</label>
          <select class="inp" value=${p.data_window} onChange=${e => upd('data_window', e.target.value)}>
            <option value="60d">60 days</option><option value="1y">1 year</option><option value="3y">3 years</option>
          </select></div>
        <button class="btn primary" onClick=${run} disabled=${res.status === 'loading'}>${res.status === 'loading' ? 'Running…' : 'Run'}</button>
      </div>
    </${Card}>
    ${res.status === 'loading' && html`<${Card}><div class="bt-stats">${Array.from({ length: 4 }).map((_, i) => html`<div class="stat" key=${i}><${Skeleton} w="55%" h=10 /><div style="height:8px"></div><${Skeleton} w="80%" h=20 /></div>`)}</div></${Card}>`}
    ${res.status === 'error' && html`<${ErrorState} message=${res.error} onRetry=${run} />`}
    ${res.status === 'ready' && html`
      <${Card} title="Result" accent=${(s.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)'}>
        <div class="bt-stats">
          <${StatTile} k="Total P&L" value=${s.total_pnl || 0} tone=${(s.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)'} hl=${true} format=${v => signMoney(v)} />
          <${StatTile} k="Win Rate" value=${s.win_rate_pct || 0} format=${v => v.toFixed(1) + '%'} />
          <${StatTile} k="Trades" value=${s.n_trades || 0} format=${v => v.toFixed(0)} />
          <${StatTile} k="Max Drawdown" value=${s.max_drawdown || 0} tone="var(--red)" format=${v => money(v)} />
        </div>
        <div style="margin-top:16px"><${Sparkline} curve=${curve} /></div>
      </${Card}>
      <${Card} title="By year" accent="var(--blue)">
        <table class="tbl"><thead><tr><th>Year</th><th class="r">Trades</th><th class="r">P&L</th><th class="r">Win %</th></tr></thead>
          <tbody>${(res.data.yearly || []).map(y => html`<tr key=${y.year}>
            <td>${y.year}</td><td class="r">${y.n}</td>
            <td class=${clsx('r', (y.pnl || 0) >= 0 ? 'pos' : 'neg')} style="font-weight:600">${signMoney(y.pnl)}</td>
            <td class="r">${(y.wr || 0).toFixed(0)}%</td></tr>`)}</tbody></table>
      </${Card}>`}
  </div>`;
}

// MEIC backtest — the honest engine (scripts/meic_backtest.py) is CLI-only; the
// HTTP /api/backtest/iron_condor is the DISCREDITED legacy proxy (per CLAUDE.md),
// so we surface the validated anchors here instead of wiring the bad endpoint.
function MeicBacktestView() {
  const A = [
    ['Net / day', '+$464', 'SPX-scale, 1ct/slot'],
    ['Green nights', '73%', 'ladder 11/12/13/14 ET'],
    ['t-stat', '21.9', 'per-day, full history'],
    ['Stop rate', '58%', 'per-entry breakeven'],
  ];
  return html`<div class="grid" style="margin-top:16px">
    <${Card} title="MEIC honest backtest — validated anchors" accent="var(--green)">
      <div class="banner" style="margin-bottom:14px">\u{1F9EA} <span>The MEIC honest engine runs offline (<span class="mono">scripts/meic_backtest.py</span> / <span class="mono">meic_excursion.py</span>, Black-Scholes repricing). The live HTTP iron-condor backtest is the legacy proxy that inflated win-rate — deliberately not shown here.</span></div>
      <div class="stats">
        ${A.map(([k, v, sub]) => html`<div class="stat" key=${k}><div class="k">${k}</div><div class="v">${v}</div><div class="faint" style="font-size:10px;margin-top:2px">${sub}</div></div>`)}
      </div>
      <div class="faint" style="font-size:11.5px;margin-top:13px;line-height:1.55">Config: 16Δ short, $25 SPX wings, skew (puts 1.15× / calls 0.90×), per-condor breakeven stop, ride-to-expiry. To re-run or sweep, use the CLI: <span class="mono">PYTHONPATH=. .venv/bin/python scripts/meic_backtest.py</span>. Excursion (MAE/MFE) + stop-anchor experiments live in the sibling scripts.</div>
    </${Card}>
  </div>`;
}

// ── App ─────────────────────────────────────────────────────────────────────
// ── Signals cockpit (the 'brain') ───────────────────────────────────────────
const SIDE = {
  sell_call_cs: { label: 'SELL CALL SPREAD', short: 'CALL', kind: 'call' },
  sell_put_cs: { label: 'SELL PUT SPREAD', short: 'PUT', kind: 'put' },
};
const sideOf = s => SIDE[s] || { label: s || '—', short: '?', kind: '' };
const pctFrom = (a, b) => (a != null && b ? (a - b) / b * 100 : null);
const sideAccent = kind => kind === 'call' ? 'var(--red)' : 'var(--green)';

function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(id); }, [ms]);
  return now;
}

function Countdown({ to, now }) {
  if (!to) return html`<span class="muted">—</span>`;
  const ms = new Date(to).getTime() - now;
  if (isNaN(ms)) return html`<span class="muted">—</span>`;
  if (ms <= 0) return html`<span class="countdown urgent">passed</span>`;
  const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const pad = n => String(n).padStart(2, '0');
  const txt = h > 0 ? `${h}h ${pad(m)}m` : `${m}m ${pad(ss)}s`;
  const cls = ms < 5 * 60000 ? 'urgent' : ms < 20 * 60000 ? 'warn' : '';
  return html`<span class=${clsx('countdown', cls)}>${txt}</span>`;
}

function PositionCockpit({ p, underlying, timeStopAt, now }) {
  const s = sideOf(p.side);
  const tp = p.tp_underlying_target, stop = p.stop_underlying_target;
  let bar = null;
  if (tp != null && stop != null && underlying != null) {
    const lo = Math.min(tp, stop, underlying), hi = Math.max(tp, stop, underlying), span = (hi - lo) || 1;
    const at = v => `${Math.max(0, Math.min(100, (v - lo) / span * 100))}%`;
    bar = html`<div>
      <div class="distbar">
        <div class="tick tp" style=${{ left: at(tp) }}></div>
        <div class="tick stop" style=${{ left: at(stop) }}></div>
        <div class="tick now" style=${{ left: at(underlying) }}></div>
      </div>
      <div class="distcap"><span class="pos">TP ${fmt(tp, 0)}</span><span>now ${fmt(underlying, 0)}</span><span class="neg">STOP ${fmt(stop, 0)}</span></div>
    </div>`;
  }
  const pnl = p.pnl;
  return html`<${Card} title=${'Open position · #' + p.trade_no} accent=${sideAccent(s.kind)}
    actions=${p.broker_status && html`<${Badge} kind=${p.broker_status === 'closed' ? 'neutral' : 'ok'}>${p.broker_status}</${Badge}>`}>
    <div class=${clsx('sig', s.kind)}>
      <div class="sig-side">${s.label}<span class="dirpill">${p.instrument || 'SPY'} · live</span></div>
      <div class="sig-legs">
        <span><span class="big">${fmt(p.short_strike, 0)}</span> <span class="lbl">short</span></span>
        <span class="faint">/</span>
        <span><span class="big">${fmt(p.long_strike, 0)}</span> <span class="lbl">long</span></span>
      </div>
      <div class="kvs">
        <div class="kv"><div class="k">Credit</div><div class="v">${money(p.credit, 0)}</div></div>
        <div class="kv"><div class="k">Live P&L</div><div class=${clsx('v', pnl == null ? null : pnl >= 0 ? 'pos' : 'neg')}>${pnl == null ? '—' : signMoney(pnl, 0)}</div></div>
        <div class="kv"><div class="k">Peak kept</div><div class="v">${fmt(p.peak_pct_kept, 0)}%</div></div>
        <div class="kv"><div class="k">Time-stop</div><div class="v"><${Countdown} to=${timeStopAt} now=${now} /></div></div>
      </div>
      ${bar}
    </div>
  </${Card}>`;
}

function SignalCard({ sig }) {
  const s = sideOf(sig.side);
  const when = sig.triggered_at ? new Date(sig.triggered_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
  return html`<${Card} title="Latest signal" accent=${sideAccent(s.kind)}>
    <div class=${clsx('sig', s.kind)}>
      <div class="sig-side">${s.label}<span class="dirpill">${sig.instrument || 'signal'}</span></div>
      <div class="sig-legs">
        <span><span class="big">${fmt(sig.short_strike, 0)}</span> <span class="lbl">short</span></span>
        <span class="faint">/</span>
        <span><span class="big">${fmt(sig.long_strike, 0)}</span> <span class="lbl">long</span></span>
      </div>
      <div class="kvs">
        <div class="kv"><div class="k">Confluence</div><div class="v">${sig.confluence_score ?? '—'}</div></div>
        ${sig.credit != null && html`<div class="kv"><div class="k">Credit</div><div class="v">${money(sig.credit, 0)}</div></div>`}
        ${sig.roi_pct != null && html`<div class="kv"><div class="k">ROI</div><div class="v">${fmt(sig.roi_pct, 0)}%</div></div>`}
        <div class="kv"><div class="k">Fired</div><div class="v" style="font-size:13px">${when}</div></div>
      </div>
    </div>
  </${Card}>`;
}

function ZoneLadder({ d }) {
  const reg = d.regime || {}, u = d.underlying, ch = reg.proj_high, cl = reg.proj_low;
  const dist = z => (z != null && u != null) ? `${pctFrom(z, u) >= 0 ? '+' : ''}${fmt(pctFrom(z, u), 2)}%` : '';
  const regimeLabel = { non_volatile: 'Non-volatile', volatile: 'Volatile', pre_obs: 'Pre-open-range' }[reg.regime] || reg.regime || '—';
  return html`<${Card} title="Tonight's strategy — sell zones" accent="var(--amber)"
    actions=${html`<${Badge} kind=${reg.classified ? 'ok' : 'neutral'}>${reg.classified ? regimeLabel : 'awaiting 9:45 ET'}</${Badge}>`}>
    ${(ch == null && cl == null)
      ? html`<${EmptyState} glyph="\u{1F4CF}" title="Zones set after the opening range"
          hint="Projected sell zones publish once the 9:30–9:45 ET observation window closes." />`
      : html`<div>
        <div class="zone callz"><span class="zlbl">Call-spread sell zone <span class="faint">(short above)</span></span><span class="zval">${fmt(ch, 0)}</span><span class="zdist">${dist(ch)}</span></div>
        <div class="zone now"><span class="zlbl">Spot</span><span class="zval">${fmt(u, 0)}</span><span class="zdist">${d.feed && d.feed !== 'none' ? d.feed : 'last'}</span></div>
        <div class="zone putz"><span class="zlbl">Put-spread sell zone <span class="faint">(short below)</span></span><span class="zval">${fmt(cl, 0)}</span><span class="zdist">${dist(cl)}</span></div>
      </div>`}
  </${Card}>`;
}

function RecentSignals({ list }) {
  if (!list || !list.length) return null;
  return html`<${Card} title="Recent signals" accent="var(--violet)">
    ${list.map((sig, i) => {
      const s = sideOf(sig.side);
      const when = sig.triggered_at ? new Date(sig.triggered_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
      return html`<div class="sigrow" key=${i}>
        <span class=${clsx('s-side', s.kind)}>${s.short}</span>
        <span class="s-meta">${sig.short_strike != null ? `${fmt(sig.short_strike, 0)}/${fmt(sig.long_strike, 0)}` : ''} · spot ${fmt(sig.underlying_price, 0)}${sig.confluence_score != null ? ` · conf ${sig.confluence_score}` : ''}</span>
        <span class="s-meta">${when}</span>
      </div>`;
    })}
  </${Card}>`;
}

function TodayCard({ t }) {
  if (!t) return null;
  const gated = Object.entries(t.gated || {});
  const alive = !!t.last_bar_et;
  const fired = t.fired > 0;
  return html`<${Card} title=${'Today · ' + fmtDay(t.date) + ' · ET'} accent=${fired ? 'var(--green)' : 'var(--blue)'}
    actions=${html`<span class="pill"><span class=${clsx('dot', alive && t.market_open ? 'live' : alive ? 'warn' : 'err')}></span>${t.last_bar_et ? 'last bar ' + t.last_bar_et : 'no feed'}</span>`}>
    <div class="today-status">${t.status}</div>
    ${gated.length ? html`<div class="today-gates">
      ${gated.map(([k, n]) => html`<span class="gate-chip" key=${k}>${k} <b>×${n}</b></span>`)}
    </div>` : ''}
    ${(t.evaluated > 0) ? html`<div class="today-meta">${t.evaluated} signal${t.evaluated !== 1 ? 's' : ''} evaluated · ${t.fired} fired${t.market_open ? '' : ' · session closed'}</div>`
      : html`<div class="today-meta">${t.market_open ? 'market open — watching for a setup' : t.weekday ? 'market closed for the day' : 'weekend — market closed'}</div>`}
  </${Card}>`;
}

const MEIC_STATUS = {
  pending: ['neutral', 'pending'], window: ['warn', 'in window'], missed: ['neutral', 'missed'],
  submitted: ['ok', 'LIVE'], closed_stop: ['bad', 'stopped'], alert_only: ['warn', 'alert only'],
  error: ['bad', 'error'], shadow: ['neutral', 'shadow'], skipped: ['neutral', 'skipped'],
};

// Per-night roll-up of the CBOE-mid limit SHADOW across whatever rungs have a
// measured fill. Mirrors debrief._limit_shadow_summary so the live ladder header
// agrees with the nightly debrief. Returns null until at least one fill lands.
function shadowSummary(rungs = []) {
  const m = rungs.map(r => r.shadow).filter(Boolean);
  if (!m.length) return null;
  const wf = m.filter(s => s.decision === 'would_fill').length;
  const totalImprove = m.reduce((a, s) => a + (s.improve_per_share || 0), 0) * 100;
  return { n: m.length, wf, totalImprove };
}

// One rung's shadow read-out: would_fill ✓ / would_not_fill ✗ + limit→fill mids
// and the $/share the market beat (or missed) the marketable limit by.
function ShadowLine({ s }) {
  if (!s) return null;
  const fill = s.decision === 'would_fill';
  const imp = s.improve_per_share || 0;
  return html`<div class=${clsx('shadow-line', fill ? 'fill' : 'nofill')}>
    <span class="sh-mark">${fill ? '✓' : '✗'}</span>
    <span class="sh-txt">limit-shadow · ${fill ? 'would fill' : 'would NOT fill'}</span>
    <span class="sh-nums num">${s.limit.toFixed(2)}→${s.real.toFixed(2)}/sh
      <span class=${imp >= 0 ? 'pos' : 'neg'}>${imp >= 0 ? '+' : ''}${imp.toFixed(2)}</span></span>
  </div>`;
}

function MeicLadder({ m }) {
  if (!m || !(m.rungs || []).length) return null;
  const sh = shadowSummary(m.rungs);
  const header = html`<div style="display:flex;align-items:center;gap:8px">
    ${sh && html`<span class="pill sh-pill" title="CBOE-mid marketable-limit shadow — measured vs real fills">
      <span class="dot ${sh.wf === sh.n ? 'live' : 'warn'}"></span>limit ${sh.wf}/${sh.n} · <span class=${sh.totalImprove >= 0 ? 'pos' : 'neg'}>${signMoney(sh.totalImprove)}</span></span>`}
    <${Badge} kind=${m.enabled ? 'ok' : 'neutral'}>${m.enabled ? `executing ×${m.contracts}` : 'alert-only'}</${Badge}>
  </div>`;
  return html`<${Card} title="MEIC ladder — iron condors" accent="var(--amber)" actions=${header}>
    ${(m.rungs || []).map((r, i) => {
      const [kind, label] = MEIC_STATUS[r.status] || ['neutral', r.status || '—'];
      const legs = (r.call_short != null)
        ? `C ${fmt(r.call_short, 0)}/${fmt(r.call_long, 0)} · P ${fmt(r.put_short, 0)}/${fmt(r.put_long, 0)}`
        : 'awaiting build';
      return html`<div class=${clsx('mrung', r.shadow && 'has-shadow')} key=${i}>
        <div class="sigrow">
          <span class="s-side" style="color:var(--ink)">${r.slot}</span>
          <span class="s-meta">${legs}${r.credit != null ? ` · credit ${money(r.credit, 0)}` : ''}</span>
          <${Badge} kind=${kind}>${label}</${Badge}>
        </div>
        <${ShadowLine} s=${r.shadow} />
      </div>`;
    })}
  </${Card}>`;
}

// MEIC signals — the condor ladder (each slot's strikes + CBOE-mid limit shadow).
function MeicSignals() {
  const res = useResource(loadSignals, STATIC ? 60000 : 5000);
  const staggerRef = useStagger(res.status === 'ready' ? (res.data?.meic?.builds?.length || 0) : null);
  if (res.status === 'loading') return html`<${MonitorSkeleton} />`;
  if (res.status === 'error') return html`<div style="margin-top:24px"><${ErrorState} message=${'Could not load signals. ' + (res.error || '')} onRetry=${res.reload} /></div>`;
  const d = res.data || {};
  const tvUrl = d.tv_chart_url || 'https://www.tradingview.com/chart/?symbol=SP%3ASPX&interval=5';
  return html`
    <div ref=${staggerRef} class="grid" style="margin-top:16px" aria-busy=${res.status === 'refreshing'}>
      ${d.mode === 'static' && html`<div class="banner" data-stagger>\u{1F4F8} <span><strong>Snapshot</strong>${d._snapAt ? ' · ' + new Date(d._snapAt).toLocaleString() : ''} — live updates on the backend terminal.</span></div>`}
      ${d.today && html`<div data-stagger><${TodayCard} t=${d.today} /></div>`}
      ${d.meic
        ? html`<div data-stagger><${MeicLadder} m=${d.meic} /></div>`
        : html`<div data-stagger><${Card} title="MEIC Ladder"><${EmptyState} glyph="\u{1F985}" title="No condor ladder yet"
            hint="The 11/12/13/14 ET ladder rungs appear here as each slot builds." /></${Card}></div>`}
      <div data-stagger style="display:flex;justify-content:flex-end">
        <a class="btn" href=${tvUrl} target="_blank" rel="noopener noreferrer">\u{1F4C8} Open chart on TradingView</a>
      </div>
    </div>`;
}

// Wave signals — the directional cockpit (live signal, open position, sell zones).
function WaveSignals() {
  const res = useResource(loadSignals, STATIC ? 60000 : 5000);
  const now = useNow(1000);
  const staggerKey = res.status === 'ready'
    ? ((res.data?.open_positions || []).length + '|' + (res.data?.latest_signal?.triggered_at || '')) : null;
  const staggerRef = useStagger(staggerKey);
  if (res.status === 'loading') return html`<${MonitorSkeleton} />`;
  if (res.status === 'error') return html`<div style="margin-top:24px"><${ErrorState} message=${'Could not load signals. ' + (res.error || '')} onRetry=${res.reload} /></div>`;
  const d = res.data || {};
  const open = d.open_positions || [];
  const sig = d.latest_signal;
  const tvUrl = d.tv_chart_url || 'https://www.tradingview.com/chart/?symbol=SP%3ASPX&interval=5';
  return html`
    <div ref=${staggerRef} class="grid" style="margin-top:16px" aria-busy=${res.status === 'refreshing'}>
      ${d.mode === 'static' && html`<div class="banner" data-stagger>\u{1F4F8} <span><strong>Snapshot</strong>${d._snapAt ? ' · ' + new Date(d._snapAt).toLocaleString() : ''} — live countdown / P&L update on the backend terminal.</span></div>`}
      ${d.today && html`<div data-stagger><${TodayCard} t=${d.today} /></div>`}
      <div data-stagger style="display:flex;justify-content:flex-end">
        <a class="btn" href=${tvUrl} target="_blank" rel="noopener noreferrer">\u{1F4C8} Open chart on TradingView</a>
      </div>
      ${open.length
        ? open.map((p, i) => html`<div data-stagger key=${p.trade_no || i}><${PositionCockpit} p=${p} underlying=${d.underlying} timeStopAt=${d.time_stop_at} now=${now} /></div>`)
        : (sig
          ? html`<div data-stagger><${SignalCard} sig=${sig} /></div>`
          : html`<div data-stagger><${Card} title="Signal"><${EmptyState} glyph="\u{1F9E0}" title="No open position"
              hint="No live signal right now. Tonight's sell zones are below; entries fire to Telegram + here in real time." /></${Card}></div>`)}
      <div data-stagger><${ZoneLadder} d=${d} /></div>
      <div data-stagger><${RecentSignals} list=${d.recent_signals} /></div>
    </div>`;
}

// ── Playbook (strategy explainer, fed live from /api/playbook) ───────────────
const ParamRow = ({ k, v }) => html`<div class="pb-row"><span class="pb-k">${k}</span><span class="pb-v">${v}</span></div>`;

function PlaybookView() {
  const res = useResource(loadPlaybook, 0);
  if (res.status === 'loading') return html`<${MonitorSkeleton} />`;
  if (res.status === 'error') return html`<div style="margin-top:24px"><${ErrorState} message=${'Could not load the playbook. ' + (res.error || '')} onRetry=${res.reload} /></div>`;
  const d = res.data || {};
  const m = d.meic || {}, w = d.wave || {};
  const dlt = v => v == null ? '—' : ((v < 1 ? Math.round(v * 100) : Math.round(v)) + 'Δ');
  const slotList = (m.slots || '').split(',').map(s => s.trim()).filter(Boolean);
  const slots = slotList.join(' · ');
  const slotCount = slotList.length || 4;
  const riskBudget = (w.account && w.risk_pct) ? Math.round(w.account * w.risk_pct / 100) : null;
  const cutoff = w.cutoff_et || '15:30';
  return html`<div class="grid" style="margin-top:16px">
    <div class="banner">\u{1F4D6} <span>Exactly what the engine is running — pulled live from its config, so it never drifts from reality.</span></div>

    <${Card} title="MEIC — Multiple-Entry Iron Condor" accent="var(--amber)"
      actions=${html`<${Badge} kind=${m.enabled ? 'ok' : 'neutral'}>${m.enabled ? 'live' : 'off'}</${Badge}>`}>
      <p class="pb-lead">Sells premium at <b>${slotCount} fixed times</b> across the day — an <b>iron condor when both sides pay</b>, a single put/call <b>credit spread when only one side does</b>. Each entry profits if SPX simply <b>stays range-bound</b> into the 4:00 PM expiry. Laddering spreads timing risk instead of betting the whole day on one moment.</p>
      <div class="pb-params">
        <${ParamRow} k="Entries" v=${(slots || '11:00 · 12:00 · 13:00 · 14:00') + ' ET'} />
        <${ParamRow} k="Size" v=${`${m.contracts_per_slot ?? 1} contract / slot · SPX built, SPY-executed (1/10)`} />
        <${ParamRow} k="Pricing" v=${m.entry_plane === 'alpaca_nbbo' ? 'live NBBO + per-strike IV (executable quotes)' : 'CBOE delayed mids'} />
        <${ParamRow} k="Shorts" v=${dlt(m.short_delta) + ' call spread + ' + dlt(m.short_delta) + ' put spread'} />
        <${ParamRow} k="Wings" v=${`SPX ${m.wing_spx ?? 25}pt → SPY $${m.wing_spy ?? 2.5}`} />
        <${ParamRow} k="Side floor" v=${`each side must pay ≥ $${m.min_side_credit_usd ?? 100}${m.one_sided_enabled ? ' · paying side trades alone' : ' (else skip)'}`} />
        <${ParamRow} k="Min credit" v=${(m.min_credit_pct ?? 10) + '% of wing (else skip the slot)'} />
        <${ParamRow} k="Stop" v=${`breakeven — buy-back ≥ ${m.stop_buffer ?? 1.05}× the REAL fill credit, marked on ${m.stop_mark === 'alpaca_nbbo' ? 'live NBBO' : 'CBOE mids'} · else 16:00 expiry`} />
        <${ParamRow} k="Assign guard" v=${m.assignment_guard && m.assignment_guard.enabled ? `final ${m.assignment_guard.window_min_before_close ?? 15} min: force-close any short within $${m.assignment_guard.buffer_spy ?? 0.5} of spot` : 'off'} />
      </div>
      <p class="pb-note"><b>No early take-profit</b> — the backtest showed it caps the full-credit winners that ARE the edge. Expect many small scratched reds + occasional bigger green nights (the asymmetry). ≈ <b>$250–300/day defined risk</b> at 1 contract/slot.</p>
    </${Card}>

    <${Card} title="WAVE — Directional Credit Spread" accent="var(--green)"
      actions=${html`<${Badge} kind=${w.enabled ? 'ok' : 'neutral'}>${w.enabled ? 'live' : 'off'}</${Badge}>`}>
      <p class="pb-lead">After the morning, when price <b>over-extends</b>, it fades the move — selling a credit spread <b>against</b> the stretch (price up → sell a call spread, price down → sell a put spread). A mean-reversion bet that the move exhausts before your short strike.</p>
      <div class="pb-params">
        <${ParamRow} k="Shorts" v=${dlt(w.short_delta)} />
        <${ParamRow} k="Wings" v=${`SPX $${w.wing_spx ?? 10} → SPY $${w.wing_spy ?? 1}`} />
        <${ParamRow} k="Take-profit" v=${(w.tp_target ?? 95) + '% of credit (≈ ride to near-expiry)'} />
        <${ParamRow} k="Sizing" v=${`risk-based: ${w.risk_pct ?? 4.5}% of $${(w.account || 10000).toLocaleString()}${riskBudget ? ` ($${riskBudget})` : ''}, cap $${w.size_cap ?? 500} → ~5–6 SPY contracts`} />
        <${ParamRow} k="Confluence" v=${`≥${w.min_confluence ?? 2} to fire · full size at ${w.full_size_confluence ?? 4}`} />
        <${ParamRow} k="Time stop" v=${(w.time_stop_min ?? 15) + ' min before the close'} />
        <${ParamRow} k="Max / day" v=${`${w.max_per_day ?? 3} entries`} />
      </div>
      <p class="pb-note">Fires only when confluence ≥${w.min_confluence ?? 2}, VIX below ${w.vix_standaside ?? 30}${w.vix_up_only ? ', and VIX is UP at the open' : ''}, outside macro-event blackouts, and before ${cutoff} ET. <b>Picky by design</b> — most days it stands aside, which is why the book is often flat.</p>
    </${Card}>
  </div>`;
}

function App() {
  // book = which strategy (meic | wave); view = tab within that book. Both persist.
  const [book, setBookRaw] = useState(() => localStorage.getItem('zdt_book') || 'meic');
  const [view, setViewRaw] = useState(() => localStorage.getItem('zdt_view') || 'dashboard');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const setBook = (b) => { setBookRaw(b); localStorage.setItem('zdt_book', b);
    // reset to dashboard when switching books unless the tab exists in both
    if (!TABS[b].some(([k]) => k === view)) { setViewRaw('dashboard'); localStorage.setItem('zdt_view', 'dashboard'); } };
  const setView = (v) => { setViewRaw(v); localStorage.setItem('zdt_view', v); };
  return html`
    <div class="app">
      ${settingsOpen && html`<${SettingsSheet} onClose=${() => setSettingsOpen(false)} />`}
      <${Topbar} book=${book} setBook=${setBook} view=${view} setView=${setView} onSettings=${() => setSettingsOpen(true)} />
      ${book === 'meic' && view === 'dashboard' && html`<${MeicDashboard} />`}
      ${book === 'meic' && view === 'signals' && html`<${MeicSignals} />`}
      ${book === 'meic' && view === 'backtest' && html`<${MeicBacktestView} />`}
      ${book === 'wave' && view === 'dashboard' && html`<${WaveView} />`}
      ${book === 'wave' && view === 'signals' && html`<${WaveSignals} />`}
      ${book === 'wave' && view === 'backtest' && html`<${BacktestView} />`}
      ${view === 'playbook' && html`<${PlaybookView} />`}
      ${view === 'macro' && html`<${MacroView} />`}
    </div>`;
}

const root = document.getElementById('app');
root.removeAttribute('aria-busy');
render(html`<${App} />`, root);
