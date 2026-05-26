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
<title>Kairos Alpha — Swarm</title>
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

  /* Sparkline charts */
  canvas.sparkline { display: block; width: 100%; height: 44px;
                     margin-top: 8px; border-top: 1px solid #1f2530; padding-top: 4px; }

  /* Fear & Greed color overrides */
  .fg-extreme-fear { color: #ff4444 !important; }
  .fg-fear         { color: #ff8844 !important; }
  .fg-neutral      { color: #f0f0f0 !important; }
  .fg-greed        { color: #88ee44 !important; }
  .fg-extreme-greed{ color: #44ff44 !important; }

  /* Boutons de controle */
  .btn { display: inline-block; padding: 5px 12px; border-radius: 5px; border: none;
         cursor: pointer; font-size: 0.82em; font-weight: 600; text-decoration: none;
         transition: opacity 0.15s; }
  .btn:hover { opacity: 0.8; }
  .btn-kill    { background: #7a1f1f; color: #ffaaaa; }
  .btn-release { background: #1f4a1f; color: #aaffaa; }
  .btn-pause   { background: #3b3a1f; color: #e3c050; }
  .btn-resume  { background: #1f3b2f; color: #50e3a0; }
  .btn-open    { background: #1f2a3a; color: #88b8ff; font-size: 0.75em; float: right; }
  .bot-actions { display: flex; gap: 6px; margin-top: 10px; }
  .bot-card { cursor: default; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Kairos Alpha <small>Swarm Dashboard</small></h1>
    <div style="display:flex;gap:10px;align-items:center;">
      <span class="badge" id="mode-badge">…</span>
      <span class="badge" id="kill-badge">…</span>
      <button class="btn btn-kill"    id="btn-kill"    onclick="doKill()"   style="display:none">🚨 Kill Switch</button>
      <button class="btn btn-release" id="btn-release" onclick="doRelease()" style="display:none">✅ Relâcher</button>
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
    <div class="card">
      <h2>Fear &amp; Greed</h2>
      <div class="metric" id="fg-value">—</div>
      <div class="submetric" id="fg-label">—</div>
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

// ─── Sparklines ───────────────────────────────────────────────────────────────
function drawSparkline(canvas, prices) {
  if (!prices || prices.length < 2) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.offsetWidth || canvas.width;
  const h = canvas.height;
  canvas.width = w;  // rescale to actual pixel width
  ctx.clearRect(0, 0, w, h);

  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const range = (max - min) || 1;
  const up = prices[prices.length - 1] >= prices[0];

  // gradient fill
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, up ? 'rgba(80,227,80,0.25)' : 'rgba(227,80,80,0.25)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');

  ctx.beginPath();
  prices.forEach((p, i) => {
    const x = (i / (prices.length - 1)) * w;
    const y = h - 2 - ((p - min) / range) * (h - 6);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  // close fill path
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // stroke line
  ctx.beginPath();
  prices.forEach((p, i) => {
    const x = (i / (prices.length - 1)) * w;
    const y = h - 2 - ((p - min) / range) * (h - 6);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = up ? '#50e350' : '#e35050';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // last price dot
  const lastX = w;
  const lastY = h - 2 - ((prices[prices.length-1] - min) / range) * (h - 6);
  ctx.beginPath();
  ctx.arc(lastX - 1, lastY, 3, 0, Math.PI * 2);
  ctx.fillStyle = up ? '#50e350' : '#e35050';
  ctx.fill();
}

function applyCharts(history) {
  if (!history) return;
  document.querySelectorAll('canvas.sparkline').forEach(canvas => {
    const botId = canvas.dataset.botid;
    const prices = history[botId];
    if (prices && prices.length > 1) drawSparkline(canvas, prices);
  });
}

// ─── Fear & Greed helpers ─────────────────────────────────────────────────────
function fgClass(v) {
  if (v == null) return '';
  if (v < 20) return 'fg-extreme-fear';
  if (v < 40) return 'fg-fear';
  if (v < 60) return 'fg-neutral';
  if (v < 80) return 'fg-greed';
  return 'fg-extreme-greed';
}

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
  updateKillButtons(killActive);

  // FEAR & GREED
  const fgEl  = document.getElementById('fg-value');
  const fgLbl = document.getElementById('fg-label');
  if (port?.fear_greed != null) {
    fgEl.textContent = port.fear_greed;
    fgEl.className   = 'metric ' + fgClass(port.fear_greed);
    fgLbl.textContent = port.fear_greed_label || '—';
  }

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
      // Infos dynamiques (Bot Dynamique)
      let dynInfo = '';
      if (b.bot_id === 'dynamique' && b.dynamic_perfs && Object.keys(b.dynamic_perfs).length) {
        const perfs = Object.entries(b.dynamic_perfs)
          .sort((a,b) => b[1]-a[1])
          .map(([s,v]) => `${s.split('-')[0]} ${v>=0?'+':''}${v.toFixed(1)}%`)
          .join(' | ');
        dynInfo = `<div class="bot-stat"><span class="label">24h</span><span class="value" style="font-size:0.8em;color:#8b95a7;">${perfs}</span></div>`;
      }

      const pauseBtn = b.paused
        ? `<button class="btn btn-resume" onclick="doResume('${b.bot_id}')">▶️ Reprendre</button>`
        : `<button class="btn btn-pause"  onclick="doPause('${b.bot_id}')">⏸ Pause</button>`;

      grid.innerHTML += `<div class="${cls}">
        <h3>${b.name}
          <span style="color:#6b7585;font-size:0.75em;">${(b.weight*100).toFixed(0)}%</span>
          <a class="btn btn-open" href="/bot/${b.bot_id}" target="_blank">🔍 Ouvrir</a>
        </h3>
        <div class="symbol">${b.symbol}</div>
        <div class="bot-stat"><span class="label">Statut</span><span class="value">${state === 'kill' ? '🚨 KILL' : b.paused ? '⏸ pausé' : '▶️ actif'}</span></div>
        <div class="bot-stat"><span class="label">Warm-up</span><span class="value">${b.warmed_up ? '✅ prêt' : ('🔄 ' + b.history_len + '/51')}</span></div>
        <div class="bot-stat"><span class="label">Position</span><span class="value">${hasPos ? fmt(b.position.qty, 6) : '—'}</span></div>
        ${hasPos ? `
        <div class="bot-stat"><span class="label">Entrée</span><span class="value">$${fmt(b.position.avg_price)}</span></div>
        <div class="bot-stat"><span class="label">P&L live</span><span class="value ${pnlPct!=null&&pnlPct>=0?'pos-up':'pos-down'}">${fmtPct(pnlPct)}</span></div>
        ` : ''}
        ${dynInfo}
        <canvas class="sparkline" data-botid="${b.bot_id}" width="260" height="44"></canvas>
        <div class="bot-actions">${pauseBtn}</div>
      </div>`;
    });
  }
  document.getElementById('n-active').textContent = nActive;

  // SPARKLINE CHARTS (apres build du grid pour que les canvas existent dans le DOM)
  const history = await fetchJson('/api/history');
  applyCharts(history);

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

// ─── Controles ────────────────────────────────────────────────────────────────
async function doKill() {
  if (!confirm('Activer le Kill Switch ? Tous les bots seront mis en pause.')) return;
  await fetch('/api/kill', {method:'POST'});
  refresh();
}
async function doRelease() {
  await fetch('/api/release', {method:'POST'});
  refresh();
}
async function doPause(botId) {
  await fetch('/api/bot/' + botId + '/pause', {method:'POST'});
  refresh();
}
async function doResume(botId) {
  await fetch('/api/bot/' + botId + '/resume', {method:'POST'});
  refresh();
}

// Afficher/masquer les boutons kill/release selon l'etat
function updateKillButtons(killActive) {
  document.getElementById('btn-kill').style.display    = killActive ? 'none'         : 'inline-block';
  document.getElementById('btn-release').style.display = killActive ? 'inline-block' : 'none';
}

refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Page individuelle par bot (comme les ports 3000-3003 de Kairos Alpha)
# ─────────────────────────────────────────────────────────────────────────────

BOT_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Kairos Alpha — {BOT_NAME}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
         background: #0a0e14; color: #d4d4d4; padding: 20px; }
  .container { max-width: 800px; margin: 0 auto; }
  header { display: flex; align-items: center; gap: 14px; margin-bottom: 24px;
           padding-bottom: 16px; border-bottom: 1px solid #1f2530; }
  .back { color: #88b8ff; text-decoration: none; font-size: 0.9em; }
  .back:hover { text-decoration: underline; }
  h1 { font-size: 1.6em; color: #f0f0f0; flex: 1; }
  h1 small { color: #6b7585; font-size: 0.55em; font-weight: 400; display: block; }
  .price-big { font-size: 2.4em; font-weight: 800; color: #f0f0f0;
               font-family: "Consolas", monospace; margin: 20px 0 4px; }
  .price-change { font-size: 1em; margin-bottom: 24px; }
  .up   { color: #50e350; }  .down { color: #e35050; }
  .card { background: #131820; border: 1px solid #1f2530;
          border-radius: 8px; padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 0.75em; color: #8b95a7; text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 14px; }
  .stat { display: flex; justify-content: space-between; padding: 7px 0;
          font-size: 0.9em; border-bottom: 1px solid #1a2030; }
  .stat:last-child { border-bottom: 0; }
  .stat .label { color: #8b95a7; }
  .stat .value { font-family: "Consolas", monospace; color: #f0f0f0; }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
  .btn { padding: 10px 20px; border-radius: 6px; border: none; cursor: pointer;
         font-size: 0.9em; font-weight: 700; transition: opacity 0.15s; }
  .btn:hover { opacity: 0.8; }
  .btn-pause   { background: #3b3a1f; color: #e3c050; }
  .btn-resume  { background: #1f4a1f; color: #50e350; }
  .btn-kill    { background: #7a1f1f; color: #ffaaaa; }
  .btn-release { background: #1f4a1f; color: #aaffaa; }
  canvas.chart { display: block; width: 100%; height: 120px; }
  .status-badge { display: inline-block; padding: 4px 12px; border-radius: 4px;
                  font-size: 0.85em; font-weight: 700; }
  .status-active { background: #1f4a2a; color: #50e350; }
  .status-paused { background: #4a2a1f; color: #e3a050; }
  .status-kill   { background: #5a1f1f; color: #ff5050; }
  .status-cold   { background: #1f2a3a; color: #8b95a7; }
  footer { text-align: center; margin-top: 30px; font-size: 0.75em; color: #6b7585; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 8px; font-size: 0.83em;
           border-bottom: 1px solid #1f2530; }
  th { color: #8b95a7; font-weight: 600; font-size: 0.75em; text-transform: uppercase; }
</style>
</head>
<body>
<div class="container">
  <header>
    <a class="back" href="/">← Swarm</a>
    <h1 id="bot-title">Bot {BOT_ID_UPPER}
      <small id="bot-symbol">{BOT_SYMBOL}</small>
    </h1>
    <div id="status-badge" class="status-badge status-cold">…</div>
  </header>

  <div id="price-big" class="price-big">—</div>
  <div id="price-change" class="price-change">—</div>

  <div class="controls" id="controls">
    <button class="btn btn-pause"   id="btn-pause"   onclick="doPause()">⏸ Mettre en pause</button>
    <button class="btn btn-resume"  id="btn-resume"  onclick="doResume()" style="display:none">▶️ Reprendre</button>
    <button class="btn btn-kill"    id="btn-kill"    onclick="doKillGlobal()">🚨 Kill Switch Global</button>
    <button class="btn btn-release" id="btn-release" onclick="doReleaseGlobal()" style="display:none">✅ Relâcher Kill Switch</button>
  </div>

  <div class="card">
    <h2>Graphique de prix (historique en mémoire)</h2>
    <canvas class="chart" id="price-chart" height="120"></canvas>
  </div>

  <div class="card">
    <h2>Position ouverte</h2>
    <div id="position-info">
      <div class="stat"><span class="label">Position</span><span class="value" id="pos-qty">—</span></div>
      <div class="stat"><span class="label">Prix entrée</span><span class="value" id="pos-entry">—</span></div>
      <div class="stat"><span class="label">Prix actuel</span><span class="value" id="pos-current">—</span></div>
      <div class="stat"><span class="label">P&L live</span><span class="value" id="pos-pnl">—</span></div>
    </div>
  </div>

  <div class="card">
    <h2>Indicateurs</h2>
    <div class="stat"><span class="label">Warm-up</span><span class="value" id="ind-warm">—</span></div>
    <div class="stat"><span class="label">Historique prix</span><span class="value" id="ind-hist">—</span></div>
    <div class="stat"><span class="label">Dernier trade</span><span class="value" id="ind-last">—</span></div>
    <div class="stat"><span class="label">Signal streak</span><span class="value" id="ind-streak">—</span></div>
    <div class="stat"><span class="label">Allocation poids</span><span class="value" id="ind-weight">—</span></div>
  </div>

  <div class="card">
    <h2>10 dernières décisions de ce bot</h2>
    <div id="decisions">…</div>
  </div>

  <footer>Auto-refresh 5s — Kairos Alpha</footer>
</div>

<script>
const BOT_ID = '{BOT_ID}';

function fmt(n, d=2) { return n != null ? n.toLocaleString('fr-FR', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—'; }
function fmtPct(n, d=2) { return n != null ? (n>=0?'+':'')+n.toFixed(d)+'%' : '—'; }

async function fetchJson(url) {
  try { const r = await fetch(url); return await r.json(); }
  catch { return null; }
}

function drawChart(canvas, prices) {
  if (!prices || prices.length < 2) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.offsetWidth || canvas.width;
  const h = canvas.height;
  canvas.width = w;
  ctx.clearRect(0, 0, w, h);
  const min = Math.min(...prices), max = Math.max(...prices);
  const range = (max - min) || 1;
  const up = prices[prices.length-1] >= prices[0];
  const col = up ? '#50e350' : '#e35050';

  const grad = ctx.createLinearGradient(0,0,0,h);
  grad.addColorStop(0, up ? 'rgba(80,227,80,0.3)' : 'rgba(227,80,80,0.3)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');

  ctx.beginPath();
  prices.forEach((p,i) => {
    const x=(i/(prices.length-1))*w, y=h-4-((p-min)/range)*(h-10);
    i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
  });
  ctx.lineTo(w,h); ctx.lineTo(0,h); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();

  ctx.beginPath();
  prices.forEach((p,i) => {
    const x=(i/(prices.length-1))*w, y=h-4-((p-min)/range)*(h-10);
    i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
  });
  ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.stroke();

  // last price label
  const lastY = h-4-((prices[prices.length-1]-min)/range)*(h-10);
  ctx.fillStyle = col; ctx.font = 'bold 12px Consolas';
  ctx.fillText('$'+fmt(prices[prices.length-1],0), w-90, Math.max(16, lastY-6));
}

async function refresh() {
  const bot  = await fetchJson('/api/bot/' + BOT_ID);
  const dir  = await fetchJson('/api/director');
  const hist = await fetchJson('/api/history');

  if (!bot) return;

  // Titre + symbole
  document.getElementById('bot-title').firstChild.nodeValue = bot.name + ' ';
  document.getElementById('bot-symbol').textContent = bot.symbol;

  // Prix
  if (bot.current_price) {
    document.getElementById('price-big').textContent = '$' + fmt(bot.current_price);
  }
  if (bot.price_history && bot.price_history.length >= 2) {
    const prices = bot.price_history;
    const chg = (prices[prices.length-1] - prices[0]) / prices[0] * 100;
    const el = document.getElementById('price-change');
    el.textContent = (chg>=0?'+':'')+chg.toFixed(2)+'% depuis le debut de session';
    el.className = 'price-change ' + (chg>=0?'up':'down');
  }

  // Status badge
  const ks = dir?.kill_switch_active;
  const badge = document.getElementById('status-badge');
  if (ks) {
    badge.textContent = '🚨 KILL SWITCH'; badge.className = 'status-badge status-kill';
  } else if (bot.paused) {
    badge.textContent = '⏸ EN PAUSE'; badge.className = 'status-badge status-paused';
  } else if (!bot.warmed_up) {
    badge.textContent = '🔄 CHAUFFE'; badge.className = 'status-badge status-cold';
  } else {
    badge.textContent = '▶️ ACTIF'; badge.className = 'status-badge status-active';
  }

  // Boutons controle
  document.getElementById('btn-pause').style.display   = bot.paused ? 'none' : 'inline-block';
  document.getElementById('btn-resume').style.display  = bot.paused ? 'inline-block' : 'none';
  document.getElementById('btn-kill').style.display    = ks ? 'none' : 'inline-block';
  document.getElementById('btn-release').style.display = ks ? 'inline-block' : 'none';

  // Position
  const pos = bot.position;
  if (pos && pos.qty > 0) {
    document.getElementById('pos-qty').textContent = fmt(pos.qty, 6) + ' ' + bot.symbol.split('-')[0];
    document.getElementById('pos-entry').textContent = '$' + fmt(pos.avg_price);
    document.getElementById('pos-current').textContent = bot.current_price ? '$' + fmt(bot.current_price) : '—';
    if (bot.current_price) {
      const pnlPct = (bot.current_price - pos.avg_price) / pos.avg_price * 100;
      const el = document.getElementById('pos-pnl');
      el.textContent = fmtPct(pnlPct) + ' ($' + fmt(pos.qty*(bot.current_price-pos.avg_price)) + ')';
      el.style.color = pnlPct >= 0 ? '#50e350' : '#e35050';
    }
  } else {
    document.getElementById('pos-qty').textContent = 'Aucune position';
    document.getElementById('pos-entry').textContent = '—';
    document.getElementById('pos-current').textContent = '—';
    document.getElementById('pos-pnl').textContent = '—';
  }

  // Indicateurs
  document.getElementById('ind-warm').textContent   = bot.warmed_up ? '✅ Prêt (51 prix chargés)' : ('🔄 ' + bot.history_len + '/51 prix');
  document.getElementById('ind-hist').textContent   = (bot.price_history?.length || 0) + ' prix en mémoire';
  document.getElementById('ind-last').textContent   = bot.last_trade > 0 ? new Date(bot.last_trade*1000).toLocaleTimeString() : 'Aucun';
  const streak = bot.signal_streak;
  document.getElementById('ind-streak').textContent = streak?.action ? (streak.action + ' x' + streak.count) : 'Aucun';
  document.getElementById('ind-weight').textContent = (bot.weight * 100).toFixed(0) + '%';

  // Chart
  const canvas = document.getElementById('price-chart');
  if (hist && hist[BOT_ID]) drawChart(canvas, hist[BOT_ID]);

  // Decisions
  const dec = bot.decisions || [];
  if (dec.length === 0) {
    document.getElementById('decisions').innerHTML =
      '<div style="padding:12px;color:#6b7585;font-style:italic">Aucune décision pour ce bot</div>';
  } else {
    let html = '<table><thead><tr><th>Heure</th><th>Type</th><th>Action</th><th>Conf.</th><th>Raison</th></tr></thead><tbody>';
    dec.forEach(d => {
      const conf = d.confidence != null ? Math.round(d.confidence*100)+'%' : '—';
      const cls  = d.action==='buy' ? 'style="color:#50e350"' : d.action==='sell' ? 'style="color:#e35050"' : '';
      html += `<tr><td>${d.timestamp.substring(11,16)}</td><td>${d.task_type}</td>
               <td ${cls}>${d.action||'—'}</td><td>${conf}</td>
               <td style="color:#8b95a7">${(d.reasoning||'').substring(0,60)}</td></tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('decisions').innerHTML = html;
  }
}

async function doPause()         { await fetch('/api/bot/'+BOT_ID+'/pause',   {method:'POST'}); refresh(); }
async function doResume()        { await fetch('/api/bot/'+BOT_ID+'/resume',  {method:'POST'}); refresh(); }
async function doKillGlobal()    { if(confirm('Kill Switch global ?')) { await fetch('/api/kill', {method:'POST'}); refresh(); } }
async function doReleaseGlobal() { await fetch('/api/release', {method:'POST'}); refresh(); }

refresh();
setInterval(refresh, 5000);
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
    """Etat du Director Agent + Fear & Greed."""
    from agents import trading_state
    director = _get_director()

    fg_val   = director._fg_value if director else None
    fg_label = director._fg_label if director else "—"

    return web.json_response({
        "mode":               MODE,
        "kill_switch_active": trading_state.is_kill_switch_active(),
        "kill_reason":        trading_state.get_kill_reason(),
        "peak_value":         director._peak_value   if director else None,
        "initial_value":      director._initial_value if director else None,
        "fear_greed":         fg_val,
        "fear_greed_label":   fg_label,
    })


async def handle_history(request: web.Request) -> web.Response:
    """Retourne l'historique de prix en memoire pour chaque bot (sparklines)."""
    swarm = _get_swarm()
    if not swarm:
        return web.json_response({})

    out: dict[str, list[float]] = {}
    for bot in swarm.bots:
        try:
            if bot._market and bot._market.price_history:
                out[bot.bot_id] = list(bot._market.price_history)
        except Exception:
            pass
    return web.json_response(out)


async def handle_decisions(request: web.Request) -> web.Response:
    with _db() as conn:
        rows = conn.execute(
            "SELECT timestamp, symbol, role, task_type, action, confidence, reasoning "
            "FROM decisions ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
    return web.json_response([dict(r) for r in rows])


async def handle_bot_page(request: web.Request) -> web.Response:
    """Page individuelle d'un bot (comme les ports 3000-3003 de Kairos Alpha)."""
    bot_id = request.match_info["bot_id"].lower()
    swarm  = _get_swarm()

    bot_name   = bot_id.upper()
    bot_symbol = "—"
    if swarm:
        for b in swarm.bots:
            if b.bot_id == bot_id:
                bot_name   = getattr(b, "display_name", bot_id.upper())
                bot_symbol = b.symbol
                break

    html = (BOT_HTML
            .replace("{BOT_ID}",      bot_id)
            .replace("{BOT_ID_UPPER}", bot_id.upper())
            .replace("{BOT_NAME}",    bot_name)
            .replace("{BOT_SYMBOL}",  bot_symbol))
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def handle_bot_api(request: web.Request) -> web.Response:
    """Etat detaille d'un bot unique + historique + decisions recentes."""
    bot_id = request.match_info["bot_id"].lower()
    swarm  = _get_swarm()
    if not swarm:
        return web.json_response({"error": "swarm non disponible"}, status=503)

    statuses = swarm.get_status()
    bot      = next((s for s in statuses if s["bot_id"] == bot_id), None)
    if not bot:
        return web.json_response({"error": "bot inconnu"}, status=404)

    # Prix live
    bot["current_price"] = await _fetch_price(bot["symbol"])

    # Historique en memoire
    for b in swarm.bots:
        if b.bot_id == bot_id:
            bot["price_history"] = list(b._market.price_history)
            break

    # 10 dernieres decisions pour ce bot
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT timestamp, task_type, action, confidence, reasoning "
                "FROM decisions WHERE symbol=? ORDER BY timestamp DESC LIMIT 10",
                (bot["symbol"],),
            ).fetchall()
        bot["decisions"] = [dict(r) for r in rows]
    except Exception:
        bot["decisions"] = []

    return web.json_response(bot)


async def handle_bot_pause(request: web.Request) -> web.Response:
    """POST /api/bot/<id>/pause — met le bot en pause."""
    from agents import trading_state
    bot_id = request.match_info["bot_id"].lower()
    trading_state.pause(bot_id)
    log.info("bot_paused_via_dashboard", bot_id=bot_id)
    return web.json_response({"ok": True, "bot_id": bot_id, "action": "paused"})


async def handle_bot_resume(request: web.Request) -> web.Response:
    """POST /api/bot/<id>/resume — reprend le bot."""
    from agents import trading_state
    bot_id = request.match_info["bot_id"].lower()
    trading_state.resume(bot_id)
    log.info("bot_resumed_via_dashboard", bot_id=bot_id)
    return web.json_response({"ok": True, "bot_id": bot_id, "action": "resumed"})


async def handle_kill(request: web.Request) -> web.Response:
    """POST /api/kill — kill switch global via dashboard."""
    from agents import trading_state
    trading_state.kill_switch("Kill Switch manuel via Dashboard")
    log.info("kill_switch_dashboard")
    return web.json_response({"ok": True, "action": "kill"})


async def handle_release(request: web.Request) -> web.Response:
    """POST /api/release — relache le kill switch via dashboard."""
    from agents import trading_state
    trading_state.release_kill_switch()
    log.info("kill_switch_released_dashboard")
    return web.json_response({"ok": True, "action": "released"})


# ─────────────────────────────────────────────────────────────────────────────
# Lancement
# ─────────────────────────────────────────────────────────────────────────────

def build_app() -> web.Application:
    app = web.Application()
    # Pages HTML
    app.router.add_get("/",                        handle_index)
    app.router.add_get("/bot/{bot_id}",            handle_bot_page)
    # API lecture
    app.router.add_get("/api/swarm",               handle_swarm)
    app.router.add_get("/api/portfolio",           handle_portfolio)
    app.router.add_get("/api/director",            handle_director)
    app.router.add_get("/api/decisions",           handle_decisions)
    app.router.add_get("/api/history",             handle_history)
    app.router.add_get("/api/bot/{bot_id}",        handle_bot_api)
    # API controles
    app.router.add_post("/api/bot/{bot_id}/pause",  handle_bot_pause)
    app.router.add_post("/api/bot/{bot_id}/resume", handle_bot_resume)
    app.router.add_post("/api/kill",               handle_kill)
    app.router.add_post("/api/release",            handle_release)
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
