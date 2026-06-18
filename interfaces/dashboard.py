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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

DB_PATH = os.getenv("DB_PATH", "memory/trading.db")
PORT    = int(os.getenv("DASHBOARD_PORT", "8080"))
MODE    = os.getenv("COINBASE_MODE", "paper")

# Capital live initial (.env). Sert de base au P&L en live au lieu des snapshots paper.
LIVE_INITIAL_USDC = float(os.getenv("LIVE_INITIAL_USDC", "0") or "0")
# Seuil de separation paper/live : les snapshots paper tournent autour de 10000,
# les snapshots live autour de LIVE_INITIAL_USDC (~170). Tout snapshot sous ce
# seuil est considere comme "live" -> debut de la courbe live.
PAPER_LIVE_SPLIT = float(os.getenv("PAPER_LIVE_SPLIT_USDC", "1000"))

# Frais round-trip Coinbase (taker x2). Soustrait du P&L par bot pour afficher
# le net reel si la position etait liquidee maintenant.
ROUND_TRIP_FEE_PCT = 2 * float(os.getenv("COINBASE_TAKER_FEE_PCT", "0.0075"))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _live_start_ts(conn: sqlite3.Connection) -> str | None:
    """Timestamp du 1er snapshot live (1er passage sous PAPER_LIVE_SPLIT). None en paper."""
    if MODE != "live":
        return None
    row = conn.execute(
        "SELECT timestamp FROM portfolio_snapshots "
        "WHERE total_usdc < ? ORDER BY timestamp ASC LIMIT 1",
        (PAPER_LIVE_SPLIT,),
    ).fetchone()
    return row["timestamp"] if row else None


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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
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

  /* Signal Diagnostic */
  .signal-diag { display: grid; gap: 14px; grid-template-columns: 1fr 1fr;
                 margin-bottom: 18px; }
  .signal-diag .card { padding: 14px 16px; }
  .vote-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .vote-label { width: 120px; font-size: 0.82em; color: #8b95a7; flex-shrink: 0; }
  .vote-bar-bg { flex: 1; background: #1a2030; border-radius: 3px; height: 10px; overflow: hidden; }
  .vote-bar { height: 10px; border-radius: 3px; transition: width 0.6s ease; min-width: 2px; }
  .vote-bar.buy  { background: linear-gradient(90deg, #1f5a2a, #50e350); }
  .vote-bar.sell { background: linear-gradient(90deg, #5a1f1f, #e35050); }
  .vote-bar.threshold { background: #f0a030; }
  .vote-score { font-family: "Consolas", monospace; font-size: 0.82em;
                color: #f0f0f0; width: 36px; text-align: right; flex-shrink: 0; }
  .voters-list { font-size: 0.78em; color: #50e350; margin-top: 4px; min-height: 16px; }
  .voters-list.sell { color: #e35050; }
  .diag-threshold { font-size: 0.78em; color: #f0a030; margin-top: 8px;
                    padding-top: 8px; border-top: 1px solid #1f2530; }
  .ind-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 6px; }
  .ind-chip { background: #0d121a; border: 1px solid #1f2530; border-radius: 5px;
              padding: 5px 8px; font-size: 0.78em; }
  .ind-chip .lbl { color: #6b7585; display: block; font-size: 0.85em; }
  .ind-chip .val { color: #f0f0f0; font-family: "Consolas", monospace; font-weight: 600; }
  .ind-chip.warn  { border-color: #4a3a1f; }
  .ind-chip.signal-buy  { border-color: #1f4a2a; }
  .ind-chip.signal-sell { border-color: #4a1f1f; }
  .no-trade-badge { display: inline-flex; align-items: center; gap: 6px;
                    background: #2a2a1a; border: 1px solid #4a3a1f; border-radius: 6px;
                    padding: 6px 12px; font-size: 0.82em; color: #e3c050; margin-bottom: 10px; }

  /* Roadmap */
  .roadmap { margin-bottom: 18px; }
  .roadmap-grid { display: grid; gap: 12px;
                  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
                  margin-top: 12px; }
  .phase-card { background: #0d121a; border: 1px solid #1f2530; border-radius: 8px;
                padding: 14px 16px; }
  .phase-card.current { border-color: #3a5a2a; background: #0d1a10; }
  .phase-card.done    { border-color: #1f3a2a; opacity: 0.75; }
  .phase-card.future  { opacity: 0.5; }
  .phase-title { font-size: 0.88em; font-weight: 700; color: #e8e8e8; margin-bottom: 8px;
                 display: flex; align-items: center; gap: 8px; }
  .phase-tag { font-size: 0.7em; padding: 2px 8px; border-radius: 3px; font-weight: 600; }
  .phase-tag.current { background: #1f4a2a; color: #50e350; }
  .phase-tag.done    { background: #1f3a2a; color: #50a350; }
  .phase-tag.future  { background: #1a2033; color: #6b7585; }
  .phase-item { font-size: 0.8em; padding: 3px 0; display: flex; align-items: flex-start;
                gap: 6px; color: #8b95a7; }
  .phase-item.done { color: #70c070; }
  .phase-item.todo { color: #a0a0b0; }
  .phase-item .chk { flex-shrink: 0; margin-top: 1px; }

  /* Tabs */
  .tabs { display: flex; gap: 0; border-bottom: 1px solid #1f2530; margin-bottom: 14px; }
  .tab { padding: 7px 16px; font-size: 0.82em; font-weight: 600; color: #6b7585;
         cursor: pointer; border-bottom: 2px solid transparent; transition: color 0.2s;
         user-select: none; }
  .tab:hover { color: #d4d4d4; }
  .tab.active { color: #88b8ff; border-bottom-color: #88b8ff; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

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
  .btn-close   { background: #5a2030; color: #ffb0b0; }
  .btn-small   { background: #1a2333; color: #8b9eb3; padding: 3px 10px;
                 border-radius: 4px; border: 1px solid #2a3142;
                 cursor: pointer; font-size: 11px; font-weight: 500;
                 margin-left: 4px; }
  .btn-small:hover { background: #2a3142; color: #d4d4d4; }
  .btn-open    { background: #1f2a3a; color: #88b8ff; font-size: 0.75em; float: right; }
  .bot-actions { display: flex; gap: 6px; margin-top: 10px; }
  .bot-card { cursor: default; }

  /* Contrôles paire / roster */
  .btn-danger { color: #e88080; border-color: #4a2a2a; }
  .btn-danger:hover { background: #3a2020; }
  .pair-ctrl { display: flex; gap: 4px; margin-top: 8px; flex-wrap: wrap;
               padding-top: 8px; border-top: 1px solid #1f2530; align-items: center; }
  .pair-ctrl input { background: #0d121a; border: 1px solid #2a3142; color: #d4d4d4;
                     border-radius: 4px; padding: 4px 8px; font-size: 11px; width: 110px;
                     font-family: "Consolas", monospace; }
  .pair-ctrl input:focus { outline: none; border-color: #88b8ff; }
  .add-bot-card { margin-bottom: 18px; }
  .add-bot-form { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .add-bot-form input { background: #0d121a; border: 1px solid #2a3142; color: #d4d4d4;
                        border-radius: 5px; padding: 7px 10px; font-size: 0.85em;
                        font-family: "Consolas", monospace; }
  .add-bot-form input:focus { outline: none; border-color: #88b8ff; }
  .add-bot-form #new-bot-weight { width: 80px; }
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

      <!-- Bots + edges générés dynamiquement depuis /api/swarm (renderMindmap) -->
      <g id="mindmap-bots"></g>
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
      <div class="submetric">sur <span id="n-total">—</span> bots du swarm</div>
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

  <!-- Signal Diagnostic -->
  <div class="signal-diag">
    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
        <span style="display:flex;align-items:center;gap:8px;">Diagnostic signal
          <select id="diag-bot" onchange="loadSignalDebug()"
                  style="background:#0d121a;border:1px solid #2a3142;color:#d4d4d4;border-radius:6px;padding:2px 6px;font-size:12px;"></select>
        </span>
        <span id="no-trade-since" style="font-size:11px;font-weight:400;color:#e3c050;"></span>
      </h2>
      <div id="vote-buy-row" class="vote-row">
        <span class="vote-label">BUY score</span>
        <div class="vote-bar-bg"><div class="vote-bar buy" id="vote-buy-bar" style="width:0%"></div></div>
        <span class="vote-score" id="vote-buy-score">0.00</span>
      </div>
      <div class="voters-list" id="vote-buy-voters">aucun votant</div>
      <div id="vote-sell-row" class="vote-row" style="margin-top:10px;">
        <span class="vote-label">SELL score</span>
        <div class="vote-bar-bg"><div class="vote-bar sell" id="vote-sell-bar" style="width:0%"></div></div>
        <span class="vote-score" id="vote-sell-score">0.00</span>
      </div>
      <div class="voters-list sell" id="vote-sell-voters">aucun votant</div>
      <div class="diag-threshold">
        Seuil de déclenchement : <strong id="vote-threshold">1.20</strong>
        &nbsp;|&nbsp; <span id="vote-status">En attente de signal fort</span>
      </div>
    </div>

    <div class="card">
      <h2>Indicateurs temps réel (dernier signal)</h2>
      <div class="ind-grid" id="ind-grid">
        <div class="ind-chip"><span class="lbl">RSI</span><span class="val" id="ind-rsi">—</span></div>
        <div class="ind-chip"><span class="lbl">MACD hist</span><span class="val" id="ind-macd">—</span></div>
        <div class="ind-chip"><span class="lbl">EMA fast</span><span class="val" id="ind-ema-fast">—</span></div>
        <div class="ind-chip"><span class="lbl">EMA slow</span><span class="val" id="ind-ema-slow">—</span></div>
        <div class="ind-chip"><span class="lbl">BB lower</span><span class="val" id="ind-bb-low">—</span></div>
        <div class="ind-chip"><span class="lbl">BB upper</span><span class="val" id="ind-bb-high">—</span></div>
        <div class="ind-chip"><span class="lbl">Vol ratio</span><span class="val" id="ind-vol">—</span></div>
        <div class="ind-chip"><span class="lbl">ATR %</span><span class="val" id="ind-atr">—</span></div>
        <div class="ind-chip"><span class="lbl">Prix</span><span class="val" id="ind-price">—</span></div>
      </div>
      <div style="font-size:0.75em;color:#6b7585;margin-top:8px;" id="ind-symbol-ts">—</div>
    </div>
  </div>

  <!-- Courbe P&L portfolio -->
  <div class="card" style="margin-bottom:14px;">
    <h2 style="display:flex;justify-content:space-between;align-items:center;">
      <span>Courbe P&amp;L portfolio</span>
      <span style="font-size:11px;font-weight:400;color:#6b7585;">
        <button onclick="loadPnlCurve(7)" class="btn-small">7j</button>
        <button onclick="loadPnlCurve(30)" class="btn-small">30j</button>
        <button onclick="loadPnlCurve(90)" class="btn-small">90j</button>
      </span>
    </h2>
    <div style="position:relative; height:280px; padding-top:8px;">
      <canvas id="pnl-chart"></canvas>
    </div>
    <div style="font-size:12px;color:#8b9eb3;margin-top:6px;text-align:center;" id="pnl-stats">—</div>
  </div>

  <!-- Cartes bots détaillées -->
  <div class="bot-grid" id="bot-grid">…</div>

  <!-- Ajouter un bot -->
  <div class="card add-bot-card">
    <h2>Ajouter un bot</h2>
    <div class="add-bot-form">
      <input type="text" id="new-bot-id"     placeholder="id (ex: ada)" />
      <input type="text" id="new-bot-symbol" placeholder="paire (ex: ADA-USDC)" />
      <input type="number" id="new-bot-weight" placeholder="poids" step="0.05" min="0" max="1" value="0.10" />
      <button class="btn btn-resume" onclick="doAddBot()">➕ Ajouter</button>
    </div>
    <div id="add-bot-msg" style="font-size:0.8em;color:#8b95a7;margin-top:8px;"></div>
  </div>

  <!-- Onglets : Décisions / Trades / Roadmap -->
  <div class="card">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('tab-decisions', this)">20 dernières décisions</div>
      <div class="tab" onclick="switchTab('tab-trades', this)">Trades exécutés</div>
      <div class="tab" onclick="switchTab('tab-roadmap', this)">Roadmap projet</div>
    </div>

    <div id="tab-decisions" class="tab-panel active">
      <div id="decisions-container">…</div>
    </div>

    <div id="tab-trades" class="tab-panel">
      <div id="trades-container">…</div>
    </div>

    <div id="tab-roadmap" class="tab-panel">
      <div class="roadmap-grid">
        <div class="phase-card done">
          <div class="phase-title">Phase 1 — Scaffolding <span class="phase-tag done">✓ Fait</span></div>
          <div class="phase-item done"><span class="chk">✅</span> Architecture multi-bots Swarm</div>
          <div class="phase-item done"><span class="chk">✅</span> Director Agent + kill switch</div>
          <div class="phase-item done"><span class="chk">✅</span> 3 stratégies : MA, Multi-ind., Mean Rev.</div>
          <div class="phase-item done"><span class="chk">✅</span> Ensemble vote pondéré</div>
          <div class="phase-item done"><span class="chk">✅</span> Mémoire SQLite + UUID déterministes</div>
          <div class="phase-item done"><span class="chk">✅</span> Dashboard Swarm (ce dashboard)</div>
          <div class="phase-item done"><span class="chk">✅</span> Fear &amp; Greed + sparklines</div>
          <div class="phase-item done"><span class="chk">✅</span> Bot Dynamique (rotation symbol)</div>
          <div class="phase-item done"><span class="chk">✅</span> Telegram commandes Swarm</div>
        </div>

        <div class="phase-card done">
          <div class="phase-title">Phase 2 — Signaux &amp; agents <span class="phase-tag done">✓ Fait</span></div>
          <div class="phase-item done"><span class="chk">✅</span> Notification Telegram signal</div>
          <div class="phase-item done"><span class="chk">✅</span> Risk Agent (position size, SL/TP)</div>
          <div class="phase-item done"><span class="chk">✅</span> Journal de trades complet (CSV)</div>
          <div class="phase-item done"><span class="chk">✅</span> Param tuner automatique</div>
          <div class="phase-item done"><span class="chk">✅</span> News sentiment agent</div>
          <div class="phase-item done"><span class="chk">✅</span> Weekly stats report</div>
          <div class="phase-item done"><span class="chk">✅</span> Daily summary + milestones + backups</div>
        </div>

        <div class="phase-card current">
          <div class="phase-title">Phase 3 — Calibrage &amp; trades <span class="phase-tag current">▶ En cours</span></div>
          <div class="phase-item done"><span class="chk">✅</span> Moteur de backtest (run_backtest.py)</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Backtest intégré au dashboard</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Calibrage seuils ensemble sur données réelles</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Optimisation paramètres (grid search)</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Walk-forward validation</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Rapport P&amp;L / Sharpe / Max DD</div>
        </div>

        <div class="phase-card current">
          <div class="phase-title">Phase 4 — Live &amp; Sécurité <span class="phase-tag current">🔴 LIVE actif</span></div>
          <div class="phase-item done"><span class="chk">✅</span> Passage live Coinbase (capital réel)</div>
          <div class="phase-item done"><span class="chk">✅</span> Kill switch par bot + global</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Audit sécurité complet</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Rate limiting &amp; circuit breakers</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Alertes Telegram avancées (P&amp;L, drawdown)</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Monitoring externe (uptime, alertes)</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Infra cloud déployable</div>
        </div>
      </div>
    </div>
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

// Frais round-trip Coinbase (maj depuis /api/director). Soustrait du P&L par bot.
let FEE_RT = 0.012;
function netPnl(cur, avg) {
  if (cur == null || avg == null || avg <= 0) return null;
  return ((cur - avg) / avg - FEE_RT) * 100;
}

const SVGNS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

// Génère dynamiquement les nœuds + arêtes de la carte mentale depuis le swarm.
// Reflète en temps réel l'ajout / la suppression de bots.
function renderMindmap(swarm, killActive) {
  const g = document.getElementById('mindmap-bots');
  if (!g) return;
  g.innerHTML = '';

  const n = swarm.length;
  if (!n) return;
  const W = 1000, nodeW = Math.min(140, (W - 40) / n - 10);
  const slot = W / n;

  swarm.forEach((b, i) => {
    const cx = slot * (i + 0.5);
    const x  = cx - nodeW / 2;
    const hasPos = b.position && b.position.qty > 0;
    const pnlPct = hasPos && b.current_price ?
      netPnl(b.current_price, b.position.avg_price) : null;
    const state = killActive ? 'kill' : (b.paused ? 'paused' : 'active');

    // Arête Director -> bot
    const edge = svgEl('path', {d: `M 500,80 Q 500,170 ${cx.toFixed(0)},200`, class: 'edge'});
    if (state === 'kill') edge.classList.add('kill');
    else if (state === 'active' && hasPos) edge.classList.add('active');
    g.appendChild(edge);

    // Boîte du bot
    const rect = svgEl('rect', {x: x.toFixed(0), y: 200, width: nodeW.toFixed(0),
                                height: 100, rx: 10, class: 'node-bg'});
    if (state === 'kill')        rect.classList.add('node-kill');
    else if (state === 'paused') rect.classList.add('node-paused');
    else if (hasPos)             rect.classList.add('node-active');
    else                         rect.classList.add('node-cold');
    g.appendChild(rect);

    const label = (b.name || b.bot_id).toUpperCase().substring(0, 12);
    const stateTxt = state === 'kill' ? '🚨 KILL' : state === 'paused' ? '⏸ pausé'
                   : hasPos ? '📈 position' : '⚪ flat';
    const pnlTxt = pnlPct != null ? 'P&L: ' + fmtPct(pnlPct) : 'P&L: —';

    g.appendChild(Object.assign(svgEl('text', {x: cx.toFixed(0), y: 226, class: 'node-label'}),
                                {textContent: label}));
    g.appendChild(Object.assign(svgEl('text', {x: cx.toFixed(0), y: 248, class: 'node-sub'}),
                                {textContent: b.symbol || '—'}));
    g.appendChild(Object.assign(svgEl('text', {x: cx.toFixed(0), y: 266, class: 'node-sub'}),
                                {textContent: stateTxt}));
    g.appendChild(Object.assign(svgEl('text', {x: cx.toFixed(0), y: 284, class: 'node-sub'}),
                                {textContent: pnlTxt}));
  });
}

async function refresh() {
  const port = await fetch('/api/director').then(r => r.json()).catch(() => null);
  if (port?.round_trip_fee_pct != null) FEE_RT = port.round_trip_fee_pct;
  // Pas de fallback 'paper' : afficher faussement PAPER en live est dangereux.
  // Si le mode est inconnu (API injoignable), on garde le dernier badge connu.
  if (port?.mode) {
    document.getElementById('mode-badge').textContent = port.mode.toUpperCase();
    document.getElementById('mode-badge').className = 'badge ' + port.mode;
  }

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
    renderMindmap(swarm, killActive);
    populateDiagSelect(swarm);
    swarm.forEach(b => {
      const hasPos = b.position && b.position.qty > 0;
      const pnlPct = hasPos && b.current_price ?
        netPnl(b.current_price, b.position.avg_price) : null;
      const state = killActive ? 'kill' : (b.paused ? 'paused' : 'active');
      if (!b.paused && !killActive) nActive++;

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
      // Bouton "Clôturer" : seulement si position ouverte (verrouille le P&L affiché).
      const closeBtn = hasPos
        ? `<button class="btn btn-close" onclick="doClose('${b.bot_id}', ${pnlPct != null ? pnlPct.toFixed(2) : 'null'})">✖ Clôturer</button>`
        : '';

      // Contrôles paire/roster (sauf bot dynamique qui choisit auto)
      const isDyn = b.bot_id === 'dynamique';
      const pairCtrl = isDyn ? '' : `
        <div class="pair-ctrl">
          <input type="text" id="pair-${b.bot_id}" placeholder="${b.symbol}" />
          <button class="btn-small" onclick="doSetPair('${b.bot_id}')">↔ Changer paire</button>
          <button class="btn-small btn-danger" onclick="doRemoveBot('${b.bot_id}')">🗑 Retirer</button>
        </div>`;

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
        <div class="bot-stat"><span class="label">Engagé</span><span class="value">$${fmt(b.position.qty * b.position.avg_price)}</span></div>
        <div class="bot-stat"><span class="label">Valeur actuelle</span><span class="value">${b.current_price ? '$' + fmt(b.position.qty * b.current_price) : '—'}</span></div>
        <div class="bot-stat"><span class="label">P&L live</span><span class="value ${pnlPct!=null&&pnlPct>=0?'pos-up':'pos-down'}">${fmtPct(pnlPct)}</span></div>
        ` : ''}
        ${dynInfo}
        <canvas class="sparkline" data-botid="${b.bot_id}" width="260" height="44"></canvas>
        <div class="bot-actions">${pauseBtn}${closeBtn}</div>
        ${pairCtrl}
      </div>`;
    });
  }
  document.getElementById('n-active').textContent = nActive;
  if (swarm) document.getElementById('n-total').textContent = swarm.length;

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
async function doClose(botId, pnlPct) {
  const pnlTxt = (pnlPct != null && !isNaN(pnlPct))
    ? ` (P&L actuel ${pnlPct >= 0 ? '+' : ''}${pnlPct}%)` : '';
  if (!confirm(`Clôturer la position de ${botId.toUpperCase()}${pnlTxt} ?\n\n` +
               `→ Vente au marché immédiate (capital réel)\n` +
               `→ Le bot est ensuite mis en PAUSE (pas de rachat).`)) return;
  const res = await postJson('/api/bot/' + botId + '/close', {});
  if (res.ok) {
    const pnl = res.pnl_pct != null ? `${res.pnl_pct >= 0 ? '+' : ''}${res.pnl_pct.toFixed(2)}%` : '—';
    alert(`✅ Clôturé : ${(res.qty ?? 0).toFixed(6)} ${res.symbol} @ $${(res.price ?? 0).toFixed(4)}\n` +
          `P&L : ${pnl}\nBot mis en pause.`);
  } else {
    alert('Clôture refusée : ' + (res.error || 'erreur'));
  }
  refresh();
}

// ─── Config paires / roster ───────────────────────────────────────────────────
async function postJson(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  } catch (e) { return {ok: false, error: 'réseau'}; }
}

async function doSetPair(botId) {
  const input = document.getElementById('pair-' + botId);
  const symbol = (input.value || '').trim().toUpperCase();
  if (!symbol) { input.focus(); return; }
  const res = await postJson('/api/bot/' + botId + '/setpair', {symbol});
  if (res.ok) { input.value = ''; refresh(); }
  else alert('Changement de paire refusé : ' + (res.error || 'erreur'));
}

async function doRemoveBot(botId) {
  if (!confirm('Retirer le bot ' + botId.toUpperCase() + ' du swarm ?')) return;
  const res = await postJson('/api/bot/' + botId + '/remove', {});
  if (res.ok) refresh();
  else alert('Suppression refusée : ' + (res.error || 'erreur'));
}

async function doAddBot() {
  const bot_id = (document.getElementById('new-bot-id').value || '').trim();
  const symbol = (document.getElementById('new-bot-symbol').value || '').trim().toUpperCase();
  const weight = parseFloat(document.getElementById('new-bot-weight').value || '0.1');
  const msg = document.getElementById('add-bot-msg');
  if (!bot_id || !symbol) { msg.textContent = 'id et paire requis'; return; }
  msg.textContent = 'Ajout en cours…';
  const res = await postJson('/api/bots/add', {bot_id, symbol, weight});
  if (res.ok) {
    msg.style.color = '#50e350';
    msg.textContent = `Bot ${res.bot_id.toUpperCase()} ajouté sur ${res.symbol}.`;
    document.getElementById('new-bot-id').value = '';
    document.getElementById('new-bot-symbol').value = '';
    refresh();
  } else {
    msg.style.color = '#e88080';
    msg.textContent = 'Refusé : ' + (res.error || 'erreur');
  }
}

// Afficher/masquer les boutons kill/release selon l'etat
function updateKillButtons(killActive) {
  document.getElementById('btn-kill').style.display    = killActive ? 'none'         : 'inline-block';
  document.getElementById('btn-release').style.display = killActive ? 'inline-block' : 'none';
}

// ─── P&L Curve (Chart.js) ────────────────────────────────────────────────────
let pnlChart = null;

async function loadPnlCurve(days) {
  const data = await fetchJson('/api/pnl_curve?days=' + days);
  if (!data || !data.points || data.points.length < 2) {
    document.getElementById('pnl-stats').textContent = `Pas assez de données (${data?.n_points || 0} points sur ${days}j)`;
    return;
  }

  const labels  = data.points.map(p => new Date(p.t).toLocaleString('fr-FR', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}));
  const values  = data.points.map(p => p.v);
  const pnls    = data.points.map(p => p.p);
  const drawdowns = data.drawdowns;

  // Stats
  const last     = values[values.length-1];
  const first    = values[0];
  const pnlPct   = first > 0 ? ((last - first) / first * 100) : 0;
  const maxDd    = Math.max(...drawdowns);
  document.getElementById('pnl-stats').innerHTML =
    `<span style="color:${pnlPct>=0?'#22c55e':'#ef4444'};">P&L ${days}j: ${pnlPct>=0?'+':''}${pnlPct.toFixed(2)}%</span> | ` +
    `Min: $${Math.min(...values).toFixed(2)} | Max: $${Math.max(...values).toFixed(2)} | ` +
    `Max drawdown: ${maxDd.toFixed(2)}%`;

  // Construit ou met a jour le chart
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  if (pnlChart) pnlChart.destroy();

  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Portfolio (USDC)',
          data: values,
          borderColor: pnlPct >= 0 ? '#22c55e' : '#ef4444',
          backgroundColor: pnlPct >= 0 ? 'rgba(34,197,94,0.08)' : 'rgba(239,68,68,0.08)',
          tension: 0.3,
          fill: true,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1f2e',
          borderColor: '#2a3142',
          borderWidth: 1,
          callbacks: {
            label: (ctx) => `${ctx.parsed.y.toFixed(2)} USDC (drawdown: ${drawdowns[ctx.dataIndex]}%)`,
          },
        },
      },
      scales: {
        x: { ticks: { color: '#6b7585', maxRotation: 0, autoSkipPadding: 30 },
             grid: { color: 'rgba(255,255,255,0.04)' } },
        y: { ticks: { color: '#6b7585', callback: v => '$' + v.toFixed(0) },
             grid: { color: 'rgba(255,255,255,0.04)' } },
      },
    },
  });
}

// ─── Tabs ────────────────────────────────────────────────────────────────────
function switchTab(panelId, tabEl) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(panelId).classList.add('active');
  tabEl.classList.add('active');
}

// ─── Signal Diagnostic ───────────────────────────────────────────────────────
// Remplit le sélecteur de bot du panneau diagnostic (préserve la sélection).
function populateDiagSelect(swarm) {
  const sel = document.getElementById('diag-bot');
  if (!sel) return;
  const ids = swarm.map(b => b.bot_id).join(',');
  if (sel.dataset.ids === ids) return;   // pas de changement de roster
  const prev = sel.value;
  sel.innerHTML = swarm.map(b =>
    `<option value="${b.bot_id}">${(b.name || b.bot_id).toUpperCase()} — ${b.symbol}</option>`
  ).join('');
  sel.value = swarm.some(b => b.bot_id === prev) ? prev : (swarm[0] ? swarm[0].bot_id : '');
  sel.dataset.ids = ids;
  if (sel.value !== prev) loadSignalDebug();
}

async function loadSignalDebug() {
  const sel = document.getElementById('diag-bot');
  const botId = sel && sel.value ? sel.value : '';
  const d = await fetchJson('/api/signal_debug' + (botId ? '?bot_id=' + botId : ''));
  if (!d) return;

  const maxScore = 3.5;  // poids total théorique max (1.5 + 1.0 + 1.0)
  const thresh   = d.threshold || 1.2;

  // Barres de vote
  const buyPct  = Math.min(100, (d.buy_score  / maxScore) * 100);
  const sellPct = Math.min(100, (d.sell_score / maxScore) * 100);
  document.getElementById('vote-buy-bar').style.width  = buyPct  + '%';
  document.getElementById('vote-sell-bar').style.width = sellPct + '%';
  document.getElementById('vote-buy-score').textContent  = d.buy_score?.toFixed(2)  || '0.00';
  document.getElementById('vote-sell-score').textContent = d.sell_score?.toFixed(2) || '0.00';
  document.getElementById('vote-threshold').textContent  = thresh.toFixed(2);

  // Votants
  const buyV  = d.voters_buy  || [];
  const sellV = d.voters_sell || [];
  document.getElementById('vote-buy-voters').textContent  = buyV.length  ? '▲ ' + buyV.join(', ')  : 'aucun votant';
  document.getElementById('vote-sell-voters').textContent = sellV.length ? '▼ ' + sellV.join(', ') : 'aucun votant';

  // Status
  const maxS = Math.max(d.buy_score || 0, d.sell_score || 0);
  let statusText = 'En attente de signal fort';
  let statusColor = '#8b95a7';
  if (maxS >= thresh) {
    statusText  = (d.buy_score > d.sell_score ? '🟢 BUY déclenché' : '🔴 SELL déclenché');
    statusColor = d.buy_score > d.sell_score ? '#50e350' : '#e35050';
  } else if (maxS >= thresh * 0.7) {
    statusText = '🟡 Signal faible (proche du seuil)';
    statusColor = '#e3c050';
  }
  const statusEl = document.getElementById('vote-status');
  statusEl.textContent = statusText;
  statusEl.style.color = statusColor;

  // Streak no-trade
  if (d.last_trade_ts) {
    const mins = Math.round((Date.now() - new Date(d.last_trade_ts)) / 60000);
    const h = Math.floor(mins / 60), m = mins % 60;
    document.getElementById('no-trade-since').textContent =
      `Sans trade : ${h ? h + 'h ' : ''}${m}min`;
  } else {
    document.getElementById('no-trade-since').textContent = 'Aucun trade enregistré';
  }

  // Indicateurs
  const meta = d.metadata || {};
  const fmt2 = v => v != null ? parseFloat(v).toFixed(2) : '—';
  const fmt4 = v => v != null ? parseFloat(v).toFixed(4) : '—';
  document.getElementById('ind-rsi').textContent     = fmt2(meta.rsi);
  document.getElementById('ind-macd').textContent    = fmt4(meta.macd_hist);
  document.getElementById('ind-ema-fast').textContent= fmt2(meta.ema_fast);
  document.getElementById('ind-ema-slow').textContent= fmt2(meta.ema_slow);
  document.getElementById('ind-bb-low').textContent  = fmt2(meta.bb_lower);
  document.getElementById('ind-bb-high').textContent = fmt2(meta.bb_upper);
  document.getElementById('ind-vol').textContent     = fmt2(meta.vol_ratio);
  document.getElementById('ind-atr').textContent     = meta.atr_pct != null ? (meta.atr_pct * 100).toFixed(3) + '%' : '—';
  document.getElementById('ind-price').textContent   = fmt2(meta.price);

  // Colorer RSI
  const rsiChip = document.getElementById('ind-rsi').closest('.ind-chip');
  rsiChip.className = 'ind-chip';
  if (meta.rsi != null) {
    if (meta.rsi <= 32)      rsiChip.classList.add('signal-buy');
    else if (meta.rsi >= 68) rsiChip.classList.add('signal-sell');
    else if (meta.rsi < 40 || meta.rsi > 60) rsiChip.classList.add('warn');
  }

  if (d.symbol && d.timestamp) {
    document.getElementById('ind-symbol-ts').textContent =
      d.symbol + ' — ' + d.timestamp.substring(0, 19).replace('T', ' ') + ' UTC';
  }
}

// ─── Trades exécutés ─────────────────────────────────────────────────────────
async function loadTrades() {
  const data = await fetchJson('/api/trades');
  const tc = document.getElementById('trades-container');
  if (!data || data.length === 0) {
    tc.innerHTML = '<div style="padding:20px;text-align:center;color:#6b7585;font-style:italic;">Aucun trade exécuté pour l\'instant — le bot attend un signal fort (score ≥ seuil).</div>';
    return;
  }
  let html = '<table><thead><tr><th>Heure</th><th>Symbole</th><th>Action</th><th class="num">Conf.</th><th>Détail</th></tr></thead><tbody>';
  data.forEach(d => {
    const cls = d.action === 'buy' ? 'pos-up' : d.action === 'sell' ? 'pos-down' : '';
    html += `<tr>
      <td>${d.timestamp.substring(0, 19).replace('T', ' ')}</td>
      <td><strong>${d.symbol || '—'}</strong></td>
      <td class="${cls}">${d.action}</td>
      <td class="num">${d.confidence != null ? Math.round(d.confidence*100) + '%' : '—'}</td>
      <td style="color:#8b95a7;font-size:0.85em;">${(d.reasoning||'').substring(0,60)}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  tc.innerHTML = html;
}

refresh();
loadPnlCurve(30);
loadSignalDebug();
loadTrades();
setInterval(refresh, 8000);
setInterval(loadSignalDebug, 15000);
setInterval(loadTrades, 30000);
setInterval(() => loadPnlCurve(30), 60000);
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
  .btn-close   { background: #5a2030; color: #ffb0b0; }
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
    <button class="btn btn-close"   id="btn-close"   onclick="doCloseBot()" style="display:none">✖ Clôturer la position</button>
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
    <h2>Trades clôturés (net de frais)</h2>
    <div class="stat"><span class="label">Trades clôturés</span><span class="value" id="ts-closed">—</span></div>
    <div class="stat"><span class="label">Gagnants</span><span class="value" id="ts-wins">—</span></div>
    <div class="stat"><span class="label">Taux de réussite</span><span class="value" id="ts-winrate">—</span></div>
    <div class="stat"><span class="label">P&L net réalisé</span><span class="value" id="ts-pnl">—</span></div>
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
  document.getElementById('btn-close').style.display   =
    (bot.position && bot.position.qty > 0 && !ks) ? 'inline-block' : 'none';

  // Position
  const pos = bot.position;
  if (pos && pos.qty > 0) {
    document.getElementById('pos-qty').textContent = fmt(pos.qty, 6) + ' ' + bot.symbol.split('-')[0];
    document.getElementById('pos-entry').textContent = '$' + fmt(pos.avg_price);
    document.getElementById('pos-current').textContent = bot.current_price ? '$' + fmt(bot.current_price) : '—';
    if (bot.current_price) {
      const feeRt  = dir?.round_trip_fee_pct ?? 0.012;
      const pnlPct = ((bot.current_price - pos.avg_price) / pos.avg_price - feeRt) * 100;
      const pnlUsd = pos.qty * (bot.current_price - pos.avg_price) - feeRt * pos.qty * pos.avg_price;
      const el = document.getElementById('pos-pnl');
      el.textContent = fmtPct(pnlPct) + ' ($' + fmt(pnlUsd) + ')';
      el.style.color = pnlPct >= 0 ? '#50e350' : '#e35050';
    }
  } else {
    document.getElementById('pos-qty').textContent = 'Aucune position';
    document.getElementById('pos-entry').textContent = '—';
    document.getElementById('pos-current').textContent = '—';
    document.getElementById('pos-pnl').textContent = '—';
  }

  // Trades clôturés (net de frais)
  const ts = bot.trade_stats || {n_closed:0, wins:0, win_rate:null, net_pnl_usdc:0};
  document.getElementById('ts-closed').textContent = ts.n_closed;
  document.getElementById('ts-wins').textContent   = ts.n_closed ? (ts.wins + ' / ' + ts.n_closed) : '—';
  document.getElementById('ts-winrate').textContent = ts.win_rate != null ? Math.round(ts.win_rate*100)+'%' : '—';
  const tsPnl = document.getElementById('ts-pnl');
  tsPnl.textContent = (ts.net_pnl_usdc>=0?'+':'') + '$' + fmt(ts.net_pnl_usdc);
  tsPnl.style.color = ts.n_closed === 0 ? '#9aa4b2' : (ts.net_pnl_usdc >= 0 ? '#50e350' : '#e35050');

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
async function doCloseBot() {
  if (!confirm('Clôturer la position de ' + BOT_ID.toUpperCase() + ' ?\n\n' +
               '→ Vente au marché immédiate (capital réel)\n→ Bot ensuite mis en PAUSE.')) return;
  try {
    const r   = await fetch('/api/bot/' + BOT_ID + '/close', {method:'POST'});
    const res = await r.json();
    if (res.ok) {
      const pnl = res.pnl_pct != null ? `${res.pnl_pct>=0?'+':''}${res.pnl_pct.toFixed(2)}%` : '—';
      alert(`✅ Clôturé : ${(res.qty??0).toFixed(6)} ${res.symbol} @ $${(res.price??0).toFixed(4)}\nP&L : ${pnl}`);
    } else {
      alert('Clôture refusée : ' + (res.error || 'erreur'));
    }
  } catch (e) { alert('Erreur réseau'); }
  refresh();
}

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
        live_ts     = _live_start_ts(conn)
        if live_ts:
            # En live : base = 1er snapshot live (ce matin), pas les snapshots paper.
            first_snap = conn.execute(
                "SELECT total_usdc FROM portfolio_snapshots "
                "WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
                (live_ts,),
            ).fetchone()
        else:
            first_snap = conn.execute(
                "SELECT total_usdc FROM portfolio_snapshots ORDER BY timestamp ASC LIMIT 1"
            ).fetchone()

    if MODE == "live":
        initial = LIVE_INITIAL_USDC or (first_snap["total_usdc"] if first_snap else None) or 0.0
    else:
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
        "round_trip_fee_pct": ROUND_TRIP_FEE_PCT,
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


async def handle_pnl_curve(request: web.Request) -> web.Response:
    """
    Retourne la courbe P&L : serie chronologique des snapshots portfolio.
    Format : [{t: timestamp_iso, v: total_usdc, p: pnl_pct}, ...]
    Limite : 500 points (downsample si plus).
    """
    days = int(request.query.get("days", "30"))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _db() as conn:
        # En live : on tronque avant le passage en live pour ne pas tracer le paper (~10000).
        live_ts = _live_start_ts(conn)
        if live_ts and live_ts > since:
            since = live_ts
        rows = conn.execute(
            "SELECT timestamp, total_usdc, pnl_pct FROM portfolio_snapshots "
            "WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since,),
        ).fetchall()

    points = [dict(r) for r in rows]

    # Downsample si > 500 points (1 sur N pour atteindre ~500)
    if len(points) > 500:
        step = len(points) // 500 + 1
        points = points[::step]

    # Calcule le drawdown courant
    max_val      = 0.0
    drawdowns    = []
    for p in points:
        v = p.get("total_usdc", 0) or 0
        if v > max_val:
            max_val = v
        dd = (max_val - v) / max_val * 100 if max_val > 0 else 0
        drawdowns.append(round(dd, 2))

    return web.json_response({
        "points":    [
            {"t": p["timestamp"], "v": p["total_usdc"], "p": p.get("pnl_pct", 0)}
            for p in points
        ],
        "drawdowns": drawdowns,
        "max_value": max_val,
        "min_value": min((p["total_usdc"] for p in points), default=0),
        "n_points":  len(points),
    })


async def handle_signal_debug(request: web.Request) -> web.Response:
    """Dernier signal d'un bot donné (?bot_id=) avec vote breakdown + indicateurs.

    Sans bot_id : dernier signal tous bots confondus (compat ascendante).
    """
    threshold = float(os.getenv("ENSEMBLE_MIN_SCORE", "1.2"))

    # Résolution bot_id -> symbole via le swarm (pour filtrer le bon bot)
    symbol = None
    bot_id = (request.query.get("bot_id") or "").lower().strip()
    if bot_id:
        swarm = _get_swarm()
        if swarm:
            bot = next((b for b in swarm.bots if b.bot_id == bot_id), None)
            if bot:
                symbol = bot.symbol

    with _db() as conn:
        if symbol:
            row = conn.execute(
                "SELECT timestamp, symbol, action, confidence, reasoning, metadata "
                "FROM decisions WHERE task_type='signal' AND symbol=? "
                "ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            last_trade = conn.execute(
                "SELECT timestamp FROM decisions WHERE task_type='order' "
                "AND action IN ('buy','sell') AND symbol=? "
                "ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT timestamp, symbol, action, confidence, reasoning, metadata "
                "FROM decisions WHERE task_type='signal' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            last_trade = conn.execute(
                "SELECT timestamp FROM decisions WHERE task_type='order' AND action IN ('buy','sell') "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()

    if not row:
        return web.json_response({"threshold": threshold, "symbol": symbol})

    meta: dict = {}
    try:
        raw = row["metadata"]
        if raw:
            import ast
            meta = ast.literal_eval(raw) if isinstance(raw, str) else raw
    except Exception:
        pass

    return web.json_response({
        "timestamp":     row["timestamp"],
        "symbol":        row["symbol"],
        "action":        row["action"],
        "confidence":    row["confidence"],
        "buy_score":     meta.get("ensemble_buy",  0.0),
        "sell_score":    meta.get("ensemble_sell", 0.0),
        "voters_buy":    meta.get("ensemble_voters_buy",  []),
        "voters_sell":   meta.get("ensemble_voters_sell", []),
        "threshold":     threshold,
        "metadata":      {k: v for k, v in meta.items()
                          if k not in ("ensemble_buy", "ensemble_sell",
                                       "ensemble_voters_buy", "ensemble_voters_sell")},
        "last_trade_ts": last_trade["timestamp"] if last_trade else None,
    })


def _realized_stats(conn) -> dict[str, dict]:
    """Apparie les BUY/SELL (FIFO) par symbole et calcule le P&L net realise.

    Source : decisions.metadata (qty/price), idem que le journal CSV. Le DB ne
    stocke pas de prix bruts hors metadata d'ordre, donc rien de nouveau n'est
    persiste ici : lecture seule, zero impact sur les ordres.

    Retourne {symbol: {n_closed, wins, win_rate, net_pnl_usdc}}.
    """
    rows = conn.execute(
        "SELECT symbol, action, metadata FROM decisions "
        "WHERE task_type='order' AND role='orchestrator' "
        "AND action IN ('buy','sell') ORDER BY timestamp ASC"
    ).fetchall()

    lots: dict[str, list[list[float]]] = {}   # symbol -> [[qty, price], ...]
    stats: dict[str, dict] = {}

    for row in rows:
        sym = row["symbol"]
        try:
            meta  = json.loads(row["metadata"]) if row["metadata"] else {}
            qty   = float(meta.get("qty", 0))
            price = float(meta.get("price", 0))
        except Exception:
            continue
        if qty <= 0 or price <= 0:
            continue

        lots.setdefault(sym, [])
        st = stats.setdefault(sym, {"n_closed": 0, "wins": 0, "net_pnl_usdc": 0.0})

        if row["action"] == "buy":
            lots[sym].append([qty, price])
            continue

        # SELL : apparie FIFO contre les lots d'achat
        remaining = qty
        net = 0.0
        matched = False
        while remaining > 1e-12 and lots[sym]:
            lot = lots[sym][0]
            take = min(remaining, lot[0])
            cost = take * lot[1]
            net += take * (price - lot[1]) - ROUND_TRIP_FEE_PCT * cost
            remaining -= take
            lot[0]    -= take
            matched = True
            if lot[0] <= 1e-12:
                lots[sym].pop(0)
        if matched:
            st["n_closed"]    += 1
            st["net_pnl_usdc"] += net
            if net > 0:
                st["wins"] += 1

    for sym, st in stats.items():
        st["net_pnl_usdc"] = round(st["net_pnl_usdc"], 4)
        st["win_rate"]     = round(st["wins"] / st["n_closed"], 3) if st["n_closed"] else None
    return stats


async def handle_trade_stats(request: web.Request) -> web.Response:
    """Stats de trades clotures (apparies FIFO) par symbole — lecture seule."""
    with _db() as conn:
        return web.json_response(_realized_stats(conn))


async def handle_trades(request: web.Request) -> web.Response:
    """Retourne les ordres executés (buy/sell réels, pas les signaux hold)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT timestamp, symbol, role, action, confidence, reasoning "
            "FROM decisions WHERE task_type='order' AND action IN ('buy','sell','rejected') "
            "ORDER BY timestamp DESC LIMIT 50"
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

    # Stats de trades clotures (apparies FIFO) pour ce symbole
    try:
        with _db() as conn:
            bot["trade_stats"] = _realized_stats(conn).get(
                bot["symbol"],
                {"n_closed": 0, "wins": 0, "win_rate": None, "net_pnl_usdc": 0.0},
            )
    except Exception:
        bot["trade_stats"] = {"n_closed": 0, "wins": 0, "win_rate": None, "net_pnl_usdc": 0.0}

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


async def handle_setpair(request: web.Request) -> web.Response:
    """POST /api/bot/<id>/setpair {symbol} — change la paire d'un bot."""
    swarm = _get_swarm()
    if not swarm:
        return web.json_response({"ok": False, "error": "swarm non disponible"}, status=503)
    bot_id = request.match_info["bot_id"].lower()
    try:
        body = await request.json()
    except Exception:
        body = {}
    symbol = (body.get("symbol") or "").strip()
    if not symbol:
        return web.json_response({"ok": False, "error": "symbole manquant"}, status=400)
    res = await swarm.set_pair(bot_id, symbol)
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def handle_addbot(request: web.Request) -> web.Response:
    """POST /api/bots/add {bot_id, symbol, weight} — ajoute un bot."""
    swarm = _get_swarm()
    if not swarm:
        return web.json_response({"ok": False, "error": "swarm non disponible"}, status=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    bot_id = (body.get("bot_id") or "").strip()
    symbol = (body.get("symbol") or "").strip()
    try:
        weight = float(body.get("weight", 0.1))
    except (TypeError, ValueError):
        weight = 0.1
    if not bot_id or not symbol:
        return web.json_response({"ok": False, "error": "bot_id et symbol requis"}, status=400)
    res = await swarm.add_bot(bot_id, symbol, weight=weight, name=bot_id.upper())
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def handle_removebot(request: web.Request) -> web.Response:
    """POST /api/bot/<id>/remove — retire un bot du swarm."""
    swarm = _get_swarm()
    if not swarm:
        return web.json_response({"ok": False, "error": "swarm non disponible"}, status=503)
    bot_id = request.match_info["bot_id"].lower()
    res = await swarm.remove_bot(bot_id)
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def handle_bot_close(request: web.Request) -> web.Response:
    """POST /api/bot/<id>/close — clôture la position du bot (vente marché) + pause."""
    swarm = _get_swarm()
    if not swarm:
        return web.json_response({"ok": False, "error": "swarm non disponible"}, status=503)
    bot_id = request.match_info["bot_id"].lower()
    res = await swarm.force_close(bot_id)
    log.info("bot_close_via_dashboard", bot_id=bot_id, ok=res.get("ok"))
    return web.json_response(res, status=200 if res.get("ok") else 400)


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
    app.router.add_get("/api/pnl_curve",            handle_pnl_curve)
    app.router.add_get("/api/signal_debug",        handle_signal_debug)
    app.router.add_get("/api/trades",              handle_trades)
    app.router.add_get("/api/trade_stats",         handle_trade_stats)
    app.router.add_get("/api/bot/{bot_id}",        handle_bot_api)
    # API controles
    app.router.add_post("/api/bot/{bot_id}/pause",  handle_bot_pause)
    app.router.add_post("/api/bot/{bot_id}/resume", handle_bot_resume)
    app.router.add_post("/api/bot/{bot_id}/setpair", handle_setpair)
    app.router.add_post("/api/bot/{bot_id}/close",  handle_bot_close)
    app.router.add_post("/api/bot/{bot_id}/remove", handle_removebot)
    app.router.add_post("/api/bots/add",            handle_addbot)
    app.router.add_post("/api/kill",               handle_kill)
    app.router.add_post("/api/release",            handle_release)
    return app


async def run_dashboard() -> None:
    import asyncio
    app    = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    try:
        await site.start()
    except OSError as exc:
        if exc.errno in (10048, 98):   # Windows: 10048 / Linux: 98 = port already in use
            log.warning("dashboard_port_busy",
                        port=PORT,
                        hint=f"Port {PORT} deja utilise. "
                             "Ferme l'ancienne instance ou change DASHBOARD_PORT dans .env")
            await runner.cleanup()
            return
        raise
    log.info("dashboard_started", url=f"http://localhost:{PORT}")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
