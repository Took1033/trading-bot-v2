"""
Dashboard web local - vue d'ensemble du swarm de bots.

Sert sur http://localhost:8080 :
  - Carte mentale des bots + Director (SVG anime)
  - Etat de chaque bot (BTC, ETH, SOL, Dynamique)
  - Kill switch / drawdown global
  - Decisions et positions live

Routes :
  GET /              -> page HTML autonome
  GET /api/swarm     -> etat des 4 bots
  GET /api/portfolio -> portefeuille global + drawdown
  GET /api/director  -> etat du Director Agent
  GET /api/decisions -> 20 dernieres decisions tous bots confondus
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
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


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_swarm():
    """Recupere le SWARM expose par main.py."""
    return getattr(sys.modules.get("__main__"), "SWARM", None)


def _get_director():
    return getattr(sys.modules.get("__main__"), "DIRECTOR", None)


async def _fetch_price(symbol: str) -> float | None:
    """Prix live via API publique Coinbase."""
    try:
        import aiohttp
        url = f"https://api.coinbase.com/v2/prices/{symbol}/spot"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                return float(data["data"]["amount"])
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Page HTML - Carte mentale + cartes bots
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Trading Bot v2 - Swarm Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
         background: #0a0e14; color: #d4d4d4; padding: 16px; }
  .container { max-width: 1400px; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: center;
           margin-bottom: 18px; padding-bottom: 14px;
           border-bottom: 1px solid #1f2530; }
  h1 { font-size: 1.4em; color: #f0f0f0; }
  h1 small { color: #6b7585; font-size: 0.65em; font-weight: 400; margin-left: 8px; }

  .badge { padding: 4px 10px; border-radius: 4px; font-size: 0.8em; font-weight: 600; }
  .badge.paper  { background: #3b3a1f; color: #e3c050; }
  .badge.live   { background: #5a2020; color: #e88080; }
  .badge.active { background: #1f3b1f; color: #50e350; }
  .badge.kill   { background: #5a1f1f; color: #ff5050; animation: blink 1s infinite; }
  @keyframes blink { 50% { opacity: 0.4; } }

  /* Carte mentale */
  .mindmap { background: #0d121a; border: 1px solid #1f2530; border-radius: 10px;
             padding: 24px; margin-bottom: 18px; overflow: hidden; }
  svg.mindmap-svg { width: 100%; height: 320px; display: block; }
  .node-bg { stroke: #2a2f3a; stroke-width: 1.5; }
  .node-director { fill: #5a3a1f; }
  .node-active   { fill: #1f4a2a; }
  .node-paused   { fill: #4a2a1f; }
  .node-kill     { fill: #5a1f1f; }
  .node-cold     { fill: #1f2a3a; }
  .node-label   { fill: #f0f0f0; font-size: 13px; font-weight: 600;
                   text-anchor: middle; pointer-events: none; }
  .node-sub     { fill: #8b95a7; font-size: 11px; text-anchor: middle; pointer-events: none; }
  .edge { stroke: #2a3a4a; stroke-width: 1.5; fill: none; }
  .edge.active { stroke: #50e350; stroke-width: 2; }
  .edge.kill   { stroke: #ff5050; stroke-width: 2; animation: dash 1s linear infinite; }
  @keyframes dash { to { stroke-dashoffset: -20; } }

  /* Grille de cartes bots */
  .bot-grid { display: grid; gap: 14px;
              grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
              margin-bottom: 18px; }
  .bot-card { background: #131820; border: 1px solid #1f2530;
              border-radius: 8px; padding: 16px; transition: border-color 0.3s; }
  .bot-card.has-position { border-color: #50e350; }
  .bot-card.paused { opacity: 0.5; border-color: #5a3a1f; }
  .bot-card h3 { font-size: 1em; color: #e8e8e8; margin-bottom: 4px; }
  .bot-card .symbol { color: #6b7585; font-size: 0.85em; margin-bottom: 12px; }
  .bot-stat { display: flex; justify-content: space-between;
              padding: 5px 0; font-size: 0.88em; border-bottom: 1px solid #1f2530; }
  .bot-stat:last-child { border-bottom: 0; }
  .bot-stat .label { color: #8b95a7; }
  .bot-stat .value { color: #f0f0f0; font-family: "Consolas", monospace; }
  .pos-up   { color: #50e350 !important; }
  .pos-down { color: #e35050 !important; }

  /* Cartes principales */
  .top-grid { display: grid; gap: 14px;
              grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
              margin-bottom: 18px; }
  .card { background: #131820; border: 1px solid #1f2530;
          border-radius: 8px; padding: 16px; }
  .card h2 { font-size: 0.78em; color: #8b95a7; text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 10px; }
  .metric { font-size: 1.6em; font-weight: 700; color: #f0f0f0;
            font-family: "Consolas", monospace; }
  .submetric { font-size: 0.85em; color: #8b95a7; margin-top: 4px; }

  /* Decisions */
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 8px; font-size: 0.85em;
           border-bottom: 1px solid #1f2530; }
  th { color: #8b95a7; font-weight: 600; font-size: 0.75em; text-transform: uppercase; }
  td.num { text-align: right; font-family: "Consolas", monospace; }

  footer { text-align: center; margin-top: 20px; font-size: 0.78em; color: #6b7585; }
  .pulse { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
           background: #50e350; margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Trading Bot v2 <small>Swarm Dashboard</small></h1>
    <div>
      <span class="badge" id="mode-badge">…</span>
      <span class="badge" id="kill-badge">…</span>
    </div>
  </header>

  <!-- Carte mentale -->
  <div class="mindmap">
    <svg class="mindmap-svg" viewBox="0 0 1000 320" xmlns="http://www.w3.org/2000/svg">
      <!-- Director au centre haut -->
      <g id="node-director">
        <rect class="node-bg node-director" x="430" y="20" width="140" height="60" rx="10"/>
        <text class="node-label" x="500" y="44">Director Agent</text>
        <text class="node-sub" x="500" y="62" id="director-status">…</text>
      </g>

      <!-- Edges Director -> Bots -->
      <path id="edge-btc"       class="edge" d="M 500,80 Q 500,170 150,200"/>
      <path id="edge-eth"       class="edge" d="M 500,80 Q 500,170 383,200"/>
      <path id="edge-sol"       class="edge" d="M 500,80 Q 500,170 617,200"/>
      <path id="edge-dynamique" class="edge" d="M 500,80 Q 500,170 850,200"/>

      <!-- 4 bots en bas -->
      <g id="node-btc">
        <rect class="node-bg" x="80" y="200" width="140" height="100" rx="10"/>
        <text class="node-label" x="150" y="226">BTC</text>
        <text class="node-sub"   x="150" y="248" id="bot-btc-symbol">BTC-USDC</text>
        <text class="node-sub"   x="150" y="266" id="bot-btc-state">…</text>
        <text class="node-sub"   x="150" y="284" id="bot-btc-pnl">P&amp;L: —</text>
      </g>
      <g id="node-eth">
        <rect class="node-bg" x="313" y="200" width="140" height="100" rx="10"/>
        <text class="node-label" x="383" y="226">ETH</text>
        <text class="node-sub"   x="383" y="248" id="bot-eth-symbol">ETH-USDC</text>
        <text class="node-sub"   x="383" y="266" id="bot-eth-state">…</text>
        <text class="node-sub"   x="383" y="284" id="bot-eth-pnl">P&amp;L: —</text>
      </g>
      <g id="node-sol">
        <rect class="node-bg" x="547" y="200" width="140" height="100" rx="10"/>
        <text class="node-label" x="617" y="226">SOL</text>
        <text class="node-sub"   x="617" y="248" id="bot-sol-symbol">SOL-USDC</text>
        <text class="node-sub"   x="617" y="266" id="bot-sol-state">…</text>
        <text class="node-sub"   x="617" y="284" id="bot-sol-pnl">P&amp;L: —</text>
      </g>
      <g id="node-dynamique">
        <rect class="node-bg" x="780" y="200" width="140" height="100" rx="10"/>
        <text class="node-label" x="850" y="226">Dynamique</text>
        <text class="node-sub"   x="850" y="248" id="bot-dynamique-symbol">—</text>
        <text class="node-sub"   x="850" y="266" id="bot-dynamique-state">…</text>
        <text class="node-sub"   x="850" y="284" id="bot-dynamique-pnl">P&amp;L: —</text>
      </g>
    </svg>
  </div>

  <!-- Stats top -->
  <div class="top-grid">
    <div class="card">
      <h2>Portefeuille total</h2>
      <div class="metric" id="portfolio">—</div>
      <div class="submetric" id="pnl-total">—</div>
    </div>
    <div class="card">
      <h2>Drawdown actuel</h2>
      <div class="metric" id="drawdown">—</div>
      <div class="submetric" id="peak">peak: —</div>
    </div>
    <div class="card">
      <h2>Bots actifs</h2>
      <div class="metric" id="n-active">—</div>
      <div class="submetric">sur 4 bots du swarm</div>
    </div>
    <div class="card">
      <h2>Décisions DB</h2>
      <div class="metric" id="n-decisions">—</div>
      <div class="submetric" id="last-decision">—</div>
    </div>
  </div>

  <!-- Cartes bots détaillées -->
  <div class="bot-grid" id="bot-grid">…</div>

  <!-- Décisions récentes -->
  <div class="card">
    <h2>20 dernières décisions (tous bots)</h2>
    <div id="decisions-container">…</div>
  </div>

  <footer><span class="pulse"></span>Auto-refresh 8s — http://localhost:<span id="port">8080</span></footer>
</div>

<script>
async function fetchJson(url) {
  try { const r = await fetch(url); return await r.json(); }
  catch (e) { return null; }
}

function fmt(n, d=2) { return n != null ? n.toLocaleString('fr-FR', {minimumFractionDigits: d, maximumFractionDigits: d}) : '—'; }
function fmtPct(n, d=2) { return n != null ? (n>=0?'+':'') + n.toFixed(d) + '%' : '—'; }

function setNode(botId, state, pnlPct, hasPos, symbol) {
  const rect = document.querySelector('#node-' + botId + ' rect');
  const edge = document.getElementById('edge-' + botId);
  if (!rect) return;

  rect.classList.remove('node-active', 'node-paused', 'node-cold', 'node-kill');
  edge.classList.remove('active', 'kill');

  if (state === 'kill')    { rect.classList.add('node-kill'); edge.classList.add('kill'); }
  else if (state === 'paused') rect.classList.add('node-paused');
  else if (hasPos)         { rect.classList.add('node-active'); edge.classList.add('active'); }
  else                     rect.classList.add('node-cold');

  document.getElementById('bot-' + botId + '-state').textContent =
    state === 'kill' ? '🚨 KILL' : state === 'paused' ? '⏸ pausé' : hasPos ? '📈 position' : '⚪ flat';
  document.getElementById('bot-' + botId + '-pnl').textContent =
    pnlPct != null ? 'P&L: ' + fmtPct(pnlPct) : 'P&L: —';
  if (symbol) document.getElementById('bot-' + botId + '-symbol').textContent = symbol;
}

async function refresh() {
  const port = await fetch('/api/director').then(r => r.json()).catch(() => null);
  document.getElementById('mode-badge').textContent = (port?.mode || 'paper').toUpperCase();
  document.getElementById('mode-badge').className = 'badge ' + (port?.mode || 'paper');

  const killActive = port?.kill_switch_active;
  const killBadge = document.getElementById('kill-badge');
  if (killActive) {
    killBadge.textContent = '🚨 KILL SWITCH';
    killBadge.className = 'badge kill';
  } else {
    killBadge.textContent = '▶️ ACTIF';
    killBadge.className = 'badge active';
  }

  document.getElementById('director-status').textContent =
    killActive ? '🚨 KILL ACTIF' : '🟢 monitoring';

  // PORTFOLIO
  const p = await fetchJson('/api/portfolio');
  if (p) {
    document.getElementById('portfolio').textContent = '$' + fmt(p.total);
    const pnlEl = document.getElementById('pnl-total');
    pnlEl.textContent = 'P&L: ' + fmtPct(p.pnl_pct);
    pnlEl.style.color = p.pnl_pct >= 0 ? '#50e350' : '#e35050';

    document.getElementById('drawdown').textContent = (p.drawdown_pct || 0).toFixed(2) + '%';
    document.getElementById('peak').textContent = 'peak: $' + fmt(p.peak || 0);

    document.getElementById('n-decisions').textContent = p.n_decisions || 0;
  }

  // SWARM
  const swarm = await fetchJson('/api/swarm');
  const grid = document.getElementById('bot-grid');
  let nActive = 0;
  if (swarm) {
    grid.innerHTML = '';
    swarm.forEach(b => {
      const hasPos = b.position && b.position.qty > 0;
      const pnlPct = hasPos && b.current_price ?
        ((b.current_price - b.position.avg_price) / b.position.avg_price) * 100 : null;
      const state = killActive ? 'kill' : (b.paused ? 'paused' : 'active');
      if (!b.paused && !killActive) nActive++;

      // Update mind map node
      setNode(b.bot_id, state, pnlPct, hasPos, b.symbol);

      // Card
      const cls = 'bot-card' + (hasPos ? ' has-position' : '') + (b.paused ? ' paused' : '');
      grid.innerHTML += `<div class="${cls}">
        <h3>${b.name} <span style="color:#6b7585;font-size:0.75em;float:right;">${(b.weight*100).toFixed(0)}%</span></h3>
        <div class="symbol">${b.symbol}</div>
        <div class="bot-stat"><span class="label">Statut</span><span class="value">${state === 'kill' ? '🚨 KILL' : b.paused ? '⏸ pausé' : '▶️ actif'}</span></div>
        <div class="bot-stat"><span class="label">Warm-up</span><span class="value">${b.warmed_up ? '✅' : (b.history_len + '/22')}</span></div>
        <div class="bot-stat"><span class="label">Position</span><span class="value">${hasPos ? fmt(b.position.qty, 6) : '—'}</span></div>
        ${hasPos ? `
        <div class="bot-stat"><span class="label">Entrée</span><span class="value">$${fmt(b.position.avg_price)}</span></div>
        <div class="bot-stat"><span class="label">P&L</span><span class="value ${pnlPct>=0?'pos-up':'pos-down'}">${fmtPct(pnlPct)}</span></div>
        ` : ''}
      </div>`;
    });
  }
  document.getElementById('n-active').textContent = nActive;

  // DECISIONS
  const dec = await fetchJson('/api/decisions');
  const dc = document.getElementById('decisions-container');
  if (!dec || dec.length === 0) {
    dc.innerHTML = '<div style="padding:14px;text-align:center;color:#6b7585;font-style:italic;">Aucune décision</div>';
  } else {
    let html = '<table><thead><tr><th>Heure</th><th>Symbole</th><th>Rôle</th><th>Type</th><th>Action</th><th class="num">Conf.</th><th>Raison</th></tr></thead><tbody>';
    dec.forEach(d => {
      const conf = d.confidence != null ? Math.round(d.confidence*100) + '%' : '—';
      const action = d.action || '—';
      const cls = action === 'buy' ? 'pos-up' : action === 'sell' ? 'pos-down' : '';
      html += `<tr>
        <td>${d.timestamp.substring(11, 16)}</td>
        <td><strong>${d.symbol || '—'}</strong></td>
        <td>${d.role}</td>
        <td>${d.task_type}</td>
        <td class="${cls}">${action}</td>
        <td class="num">${conf}</td>
        <td style="color:#8b95a7;font-size:0.85em;">${(d.reasoning || '').substring(0, 50)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    dc.innerHTML = html;
  }

  if (p && p.last_decision_ts) {
    document.getElementById('last-decision').textContent = 'dernière: ' + p.last_decision_ts.substring(11, 16);
  }
}

refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    html = HTML.replace("8080", str(PORT))
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def handle_swarm(request: web.Request) -> web.Response:
    """Retourne l'etat de chaque bot du swarm avec prix live."""
    swarm = _get_swarm()
    if not swarm:
        return web.json_response([])

    out = []
    for bot_info in swarm.get_status():
        current = await _fetch_price(bot_info["symbol"]) if bot_info["position"] else None
        bot_info["current_price"] = current
        out.append(bot_info)
    return web.json_response(out)


async def handle_portfolio(request: web.Request) -> web.Response:
    """Etat global du portefeuille + drawdown depuis le Director."""
    swarm    = _get_swarm()
    director = _get_director()

    total = None
    if swarm:
        total = await swarm.get_portfolio_total()

    with _db() as conn:
        n_decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        last_dec    = conn.execute(
            "SELECT timestamp FROM decisions ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        first_snap  = conn.execute(
            "SELECT total_usdc FROM portfolio_snapshots ORDER BY timestamp ASC LIMIT 1"
        ).fetchone()

    initial = (first_snap["total_usdc"] if first_snap else None) or 10_000.0
    peak    = director._peak_value if director else None
    pnl_pct = ((total - initial) / initial * 100) if total and initial > 0 else None

    drawdown_pct = 0.0
    if peak and total and peak > 0:
        drawdown_pct = max(0.0, (peak - total) / peak * 100)

    return web.json_response({
        "total":            total,
        "initial":          initial,
        "pnl_pct":          pnl_pct,
        "peak":             peak,
        "drawdown_pct":     drawdown_pct,
        "n_decisions":      n_decisions,
        "last_decision_ts": last_dec["timestamp"] if last_dec else None,
    })


async def handle_director(request: web.Request) -> web.Response:
    """Etat du Director Agent."""
    from agents import trading_state
    director = _get_director()
    return web.json_response({
        "mode":                 MODE,
        "kill_switch_active":   trading_state.is_kill_switch_active(),
        "kill_reason":          trading_state.get_kill_reason(),
        "peak_value":           director._peak_value  if director else None,
        "initial_value":        director._initial_value if director else None,
    })


async def handle_decisions(request: web.Request) -> web.Response:
    with _db() as conn:
        rows = conn.execute(
            "SELECT timestamp, symbol, role, task_type, action, confidence, reasoning "
            "FROM decisions ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
    return web.json_response([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────────────────────
# Lancement
# ─────────────────────────────────────────────────────────────────────────────

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/",              handle_index)
    app.router.add_get("/api/swarm",     handle_swarm)
    app.router.add_get("/api/portfolio", handle_portfolio)
    app.router.add_get("/api/director",  handle_director)
    app.router.add_get("/api/decisions", handle_decisions)
    return app


async def run_dashboard() -> None:
    app    = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("dashboard_started", url=f"http://localhost:{PORT}")
    import asyncio
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
