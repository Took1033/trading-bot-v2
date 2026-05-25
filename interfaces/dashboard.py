"""
Dashboard web local - vue d'ensemble du bot dans le navigateur.

Sert sur http://localhost:8080 avec :
  - Page HTML autonome (CSS + JS inline, zero dependance front)
  - Auto-refresh toutes les 10 secondes
  - 4 endpoints JSON pour les donnees temps reel

Routes :
  GET /              -> page HTML
  GET /api/status    -> etat global + prix live
  GET /api/positions -> positions ouvertes avec P&L
  GET /api/decisions -> 20 dernieres decisions
  GET /api/snapshots -> historique du portefeuille (90 derniers)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import structlog
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

DB_PATH = os.getenv("DB_PATH", "memory/trading.db")
PORT    = int(os.getenv("DASHBOARD_PORT", "8080"))
MODE    = os.getenv("COINBASE_MODE", "paper")
SYMBOL  = os.getenv("TRADING_SYMBOL", "BTC-USDC")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


async def _fetch_price() -> float | None:
    """Prix BTC live via API publique Coinbase."""
    try:
        import aiohttp
        url = f"https://api.coinbase.com/v2/prices/{SYMBOL}/spot"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                return float(data["data"]["amount"])
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Page HTML
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Trading Bot v2 - Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    background: #0f1419; color: #d4d4d4; padding: 20px;
  }
  .container { max-width: 1200px; margin: 0 auto; }
  header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 24px; padding-bottom: 16px;
    border-bottom: 1px solid #2a2f3a;
  }
  h1 { font-size: 1.5em; color: #e8e8e8; }
  .badge {
    padding: 4px 10px; border-radius: 4px; font-size: 0.85em;
    font-weight: 600;
  }
  .badge.paper { background: #3b3a1f; color: #e3c050; }
  .badge.live  { background: #5a2020; color: #e88080; }
  .badge.active { background: #1f3b1f; color: #50e350; }
  .badge.paused { background: #3b1f1f; color: #e35050; }

  .grid {
    display: grid; gap: 16px;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    margin-bottom: 20px;
  }
  .card {
    background: #1a1f2a; border: 1px solid #2a2f3a;
    border-radius: 8px; padding: 20px;
  }
  .card h2 {
    font-size: 0.9em; color: #8b95a7; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 12px;
  }
  .metric { font-size: 1.8em; font-weight: 700; color: #f0f0f0; }
  .metric.up   { color: #50e350; }
  .metric.down { color: #e35050; }
  .submetric { font-size: 0.9em; color: #8b95a7; margin-top: 6px; }

  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  th, td {
    text-align: left; padding: 8px 10px; font-size: 0.9em;
    border-bottom: 1px solid #2a2f3a;
  }
  th { color: #8b95a7; font-weight: 600; font-size: 0.8em; text-transform: uppercase; }
  td.num { text-align: right; font-family: "Consolas", monospace; }
  td.pos-up   { color: #50e350; }
  td.pos-down { color: #e35050; }

  .empty {
    text-align: center; padding: 30px; color: #6b7585; font-style: italic;
  }
  footer {
    text-align: center; margin-top: 30px; font-size: 0.8em; color: #6b7585;
  }
  .pulse {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: #50e350; margin-right: 6px;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; } 50% { opacity: 0.4; }
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Trading Bot v2 <span style="color:#8b95a7;font-size:0.7em;font-weight:400;">/ <span id="symbol">BTC-USDC</span></span></h1>
    <div>
      <span class="badge" id="mode-badge">…</span>
      <span class="badge" id="status-badge">…</span>
    </div>
  </header>

  <!-- Cartes principales -->
  <div class="grid">
    <div class="card">
      <h2>Prix actuel</h2>
      <div class="metric" id="price">…</div>
      <div class="submetric" id="price-update">…</div>
    </div>
    <div class="card">
      <h2>Portefeuille</h2>
      <div class="metric" id="portfolio">…</div>
      <div class="submetric" id="pnl-total">…</div>
    </div>
    <div class="card">
      <h2>Dernier signal</h2>
      <div class="metric" id="last-signal">…</div>
      <div class="submetric" id="last-signal-meta">…</div>
    </div>
    <div class="card">
      <h2>Activité</h2>
      <div class="metric" id="n-decisions">…</div>
      <div class="submetric">décisions enregistrées</div>
    </div>
  </div>

  <!-- Positions -->
  <div class="card">
    <h2>Positions ouvertes</h2>
    <div id="positions-container">…</div>
  </div>

  <!-- Décisions récentes -->
  <div class="card" style="margin-top:16px;">
    <h2>20 dernières décisions</h2>
    <div id="decisions-container">…</div>
  </div>

  <footer>
    <span class="pulse"></span>Auto-refresh toutes les 10s — Dashboard local sur port <span id="port">8080</span>
  </footer>
</div>

<script>
async function fetchJson(url) {
  try { const r = await fetch(url); return await r.json(); }
  catch (e) { return null; }
}

function fmt(n, d=2) { return n != null ? n.toLocaleString('fr-FR', {minimumFractionDigits: d, maximumFractionDigits: d}) : '—'; }
function fmtPct(n, d=2) { return n != null ? (n>=0?'+':'') + n.toFixed(d) + '%' : '—'; }

async function refresh() {
  // STATUS
  const s = await fetchJson('/api/status');
  if (s) {
    document.getElementById('symbol').textContent = s.symbol;
    document.getElementById('mode-badge').textContent = s.mode.toUpperCase();
    document.getElementById('mode-badge').className = 'badge ' + s.mode;
    document.getElementById('status-badge').textContent = s.paused ? 'PAUSÉ' : 'ACTIF';
    document.getElementById('status-badge').className = 'badge ' + (s.paused ? 'paused' : 'active');

    document.getElementById('price').textContent = s.price ? '$' + fmt(s.price) : '—';
    document.getElementById('price-update').textContent = s.price ? 'mis à jour ' + new Date().toLocaleTimeString('fr-FR') : 'indisponible';

    document.getElementById('portfolio').textContent = s.portfolio_total != null ? '$' + fmt(s.portfolio_total) : '—';
    const pnl = s.pnl_pct;
    if (pnl != null) {
      const el = document.getElementById('pnl-total');
      el.textContent = 'P&L: ' + fmtPct(pnl);
      el.style.color = pnl >= 0 ? '#50e350' : '#e35050';
    }

    if (s.last_signal) {
      document.getElementById('last-signal').textContent = s.last_signal.action.toUpperCase();
      document.getElementById('last-signal').className = 'metric ' + (s.last_signal.action === 'buy' ? 'up' : s.last_signal.action === 'sell' ? 'down' : '');
      const meta = [];
      if (s.last_signal.confidence != null) meta.push('conf ' + Math.round(s.last_signal.confidence*100) + '%');
      if (s.last_signal.rsi != null) meta.push('RSI ' + s.last_signal.rsi);
      meta.push(s.last_signal.timestamp.substring(0, 16));
      document.getElementById('last-signal-meta').textContent = meta.join(' · ');
    }

    document.getElementById('n-decisions').textContent = s.n_decisions || 0;
  }

  // POSITIONS
  const pos = await fetchJson('/api/positions');
  const pc = document.getElementById('positions-container');
  if (!pos || pos.length === 0) {
    pc.innerHTML = '<div class="empty">Aucune position ouverte</div>';
  } else {
    let html = '<table><thead><tr><th>Symbole</th><th class="num">Quantité</th><th class="num">Entrée</th><th class="num">Actuel</th><th class="num">P&L USDC</th><th class="num">P&L %</th></tr></thead><tbody>';
    pos.forEach(p => {
      const cls = p.pnl_usdc >= 0 ? 'pos-up' : 'pos-down';
      html += `<tr>
        <td><strong>${p.symbol}</strong></td>
        <td class="num">${fmt(p.qty, 6)}</td>
        <td class="num">$${fmt(p.entry)}</td>
        <td class="num">$${fmt(p.current)}</td>
        <td class="num ${cls}">${p.pnl_usdc>=0?'+':''}$${fmt(p.pnl_usdc)}</td>
        <td class="num ${cls}">${fmtPct(p.pnl_pct)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    pc.innerHTML = html;
  }

  // DECISIONS
  const dec = await fetchJson('/api/decisions');
  const dc = document.getElementById('decisions-container');
  if (!dec || dec.length === 0) {
    dc.innerHTML = '<div class="empty">Aucune décision</div>';
  } else {
    let html = '<table><thead><tr><th>Heure</th><th>Rôle</th><th>Type</th><th>Action</th><th class="num">Conf.</th><th>Raison</th></tr></thead><tbody>';
    dec.forEach(d => {
      const conf = d.confidence != null ? Math.round(d.confidence*100) + '%' : '—';
      const action = d.action || '—';
      const cls = action === 'buy' ? 'pos-up' : action === 'sell' ? 'pos-down' : '';
      html += `<tr>
        <td>${d.timestamp.substring(11, 16)}</td>
        <td>${d.role}</td>
        <td>${d.task_type}</td>
        <td class="${cls}">${action}</td>
        <td class="num">${conf}</td>
        <td style="color:#8b95a7;font-size:0.85em;">${(d.reasoning || '').substring(0, 60)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    dc.innerHTML = html;
  }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    html = HTML.replace("8080", str(PORT))
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def handle_status(request: web.Request) -> web.Response:
    from agents import trading_state

    price = await _fetch_price()

    with _db() as conn:
        snap = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        last_signal = conn.execute(
            "SELECT action, confidence, timestamp, metadata FROM decisions "
            "WHERE task_type='signal' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    last_sig_dict = None
    if last_signal:
        rsi_match = re.search(r"'rsi':\s*([\d.]+)", last_signal["metadata"] or "")
        last_sig_dict = {
            "action":     last_signal["action"],
            "confidence": last_signal["confidence"],
            "timestamp":  last_signal["timestamp"],
            "rsi":        float(rsi_match.group(1)) if rsi_match else None,
        }

    return web.json_response({
        "symbol":          SYMBOL,
        "mode":            MODE,
        "paused":          trading_state.is_paused(),
        "price":           price,
        "portfolio_total": snap["total_usdc"] if snap else None,
        "pnl_pct":         snap["pnl_pct"] if snap else None,
        "last_signal":     last_sig_dict,
        "n_decisions":     n,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    })


async def handle_positions(request: web.Request) -> web.Response:
    with _db() as conn:
        snap = conn.execute(
            "SELECT positions FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

    if not snap:
        return web.json_response([])

    try:
        positions = json.loads(snap["positions"])
    except Exception:
        return web.json_response([])

    if not positions:
        return web.json_response([])

    current = await _fetch_price()
    out = []
    for sym, pos in positions.items():
        entry = pos["avg_price"]
        qty   = pos["qty"]
        cur   = current if (current and "BTC" in sym) else entry
        out.append({
            "symbol":   sym,
            "qty":      qty,
            "entry":    entry,
            "current":  cur,
            "pnl_usdc": qty * (cur - entry),
            "pnl_pct":  (cur - entry) / entry * 100 if entry > 0 else 0.0,
        })
    return web.json_response(out)


async def handle_decisions(request: web.Request) -> web.Response:
    with _db() as conn:
        rows = conn.execute(
            "SELECT timestamp, role, task_type, action, confidence, reasoning "
            "FROM decisions ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
    return web.json_response([dict(r) for r in rows])


async def handle_snapshots(request: web.Request) -> web.Response:
    with _db() as conn:
        rows = conn.execute(
            "SELECT timestamp, total_usdc, pnl_pct FROM portfolio_snapshots "
            "ORDER BY timestamp DESC LIMIT 90"
        ).fetchall()
    return web.json_response([dict(r) for r in reversed(rows)])


# ─────────────────────────────────────────────────────────────────────────────
# Lancement
# ─────────────────────────────────────────────────────────────────────────────

def build_app() -> web.Application:
    """Construit l'app aiohttp avec les routes du dashboard."""
    app = web.Application()
    app.router.add_get("/",                handle_index)
    app.router.add_get("/api/status",      handle_status)
    app.router.add_get("/api/positions",   handle_positions)
    app.router.add_get("/api/decisions",   handle_decisions)
    app.router.add_get("/api/snapshots",   handle_snapshots)
    return app


async def run_dashboard() -> None:
    """Lance le dashboard sur PORT (a appeler depuis main.py)."""
    app    = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("dashboard_started", url=f"http://localhost:{PORT}")

    # Garde la coroutine en vie
    import asyncio
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_dashboard())
