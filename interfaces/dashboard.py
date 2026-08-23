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

# Symboles hors-strategie (residus ramasses sur le compte Coinbase, ex: FIGHT-USDC)
# a exclure du P&L latent affiche. Modifiable via DASHBOARD_EXCLUDED_SYMBOLS.
EXCLUDED_SYMBOLS = {s.strip().upper() for s in
                    os.getenv("DASHBOARD_EXCLUDED_SYMBOLS", "FIGHT-USDC").split(",") if s.strip()}


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
  h1 small { color: #6a7789; font-size: 0.65em; font-weight: 400; margin-left: 8px; }

  .badge { padding: 4px 10px; border-radius: 4px; font-size: 0.8em; font-weight: 600; }
  .badge.paper  { background: #3b3a1f; color: #e3c050; }
  .badge.live   { background: #5a2020; color: #e88080; }
  .badge.active { background: #1f3b1f; color: #3fd08a; }
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
  .node-sub     { fill: #98a6ba; font-size: 11px; text-anchor: middle; pointer-events: none; }
  .edge { stroke: #2a3a4a; stroke-width: 1.5; fill: none; }
  .edge.active { stroke: #3fd08a; stroke-width: 2; }
  .edge.kill   { stroke: #ff5050; stroke-width: 2; animation: dash 1s linear infinite; }
  @keyframes dash { to { stroke-dashoffset: -20; } }

  /* Grille de cartes bots */
  .bot-grid { display: grid; gap: 14px;
              grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
              margin-bottom: 18px; }
  .bot-card { background: #131820; border: 1px solid #1f2530;
              border-radius: 8px; padding: 16px; transition: border-color 0.3s; }
  .bot-card.has-position { border-color: #3fd08a; }
  .bot-card.paused { opacity: 0.5; border-color: #5a3a1f; }
  .bot-card h3 { font-size: 1em; color: #e8e8e8; margin-bottom: 4px; }
  .bot-card .symbol { color: #6a7789; font-size: 0.85em; margin-bottom: 12px; }
  .bot-stat { display: flex; justify-content: space-between;
              padding: 5px 0; font-size: 0.88em; border-bottom: 1px solid #1f2530; }
  .bot-stat:last-child { border-bottom: 0; }
  .bot-stat .label { color: #98a6ba; }
  .bot-stat .value { color: #f0f0f0; font-family: "Consolas", monospace; }
  .pos-up   { color: #3fd08a !important; }
  .pos-down { color: #ff6d7d !important; }
  /* Hierarchie gain/perte forte : accent lateral colore + P&L proeminent */
  .bot-card.profit { box-shadow: inset 4px 0 0 #22e07a; border-color: #2c4a3a; }
  .bot-card.loss   { box-shadow: inset 4px 0 0 #ff4d5e; border-color: #4a2c33; }
  .bot-pnl { font-size: 1.5em; font-weight: 800; font-family: "Consolas", monospace;
             letter-spacing: -.02em; margin: 2px 0 10px; }
  .bot-pnl.pos-up   { color: #22e07a !important; }
  .bot-pnl.pos-down { color: #ff4d5e !important; }

  /* Cartes principales */
  .top-grid { display: grid; gap: 14px;
              grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
              margin-bottom: 18px; }
  .card { background: #131820; border: 1px solid #1f2530;
          border-radius: 8px; padding: 16px; }
  .card h2 { font-size: 0.78em; color: #98a6ba; text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 10px; }
  .metric { font-size: 1.6em; font-weight: 700; color: #f0f0f0;
            font-family: "Consolas", monospace; }
  .submetric { font-size: 0.85em; color: #98a6ba; margin-top: 4px; }

  /* Decisions */
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 8px; font-size: 0.85em;
           border-bottom: 1px solid #1f2530; }
  th { color: #98a6ba; font-weight: 600; font-size: 0.75em; text-transform: uppercase; }
  td.num { text-align: right; font-family: "Consolas", monospace; }

  footer { text-align: center; margin-top: 20px; font-size: 0.78em; color: #6a7789; }
  .pulse { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
           background: #3fd08a; margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  /* Sparkline charts */
  canvas.sparkline { display: block; width: 100%; height: 44px;
                     margin-top: 8px; border-top: 1px solid #1f2530; padding-top: 4px; }

  /* Signal Diagnostic */
  .signal-diag { display: grid; gap: 14px; grid-template-columns: 1fr 1fr;
                 margin-bottom: 18px; }
  .signal-diag .card { padding: 14px 16px; }
  .vote-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .vote-label { width: 120px; font-size: 0.82em; color: #98a6ba; flex-shrink: 0; }
  .vote-bar-bg { flex: 1; background: #1a2030; border-radius: 3px; height: 10px; overflow: hidden; }
  .vote-bar { height: 10px; border-radius: 3px; transition: width 0.6s ease; min-width: 2px; }
  .vote-bar.buy  { background: linear-gradient(90deg, #1f5a2a, #3fd08a); }
  .vote-bar.sell { background: linear-gradient(90deg, #5a1f1f, #ff6d7d); }
  .vote-bar.threshold { background: #f0a030; }
  .vote-score { font-family: "Consolas", monospace; font-size: 0.82em;
                color: #f0f0f0; width: 36px; text-align: right; flex-shrink: 0; }
  .voters-list { font-size: 0.78em; color: #3fd08a; margin-top: 4px; min-height: 16px; }
  .voters-list.sell { color: #ff6d7d; }
  .diag-threshold { font-size: 0.78em; color: #f0a030; margin-top: 8px;
                    padding-top: 8px; border-top: 1px solid #1f2530; }
  .ind-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 6px; }
  .ind-chip { background: #0d121a; border: 1px solid #1f2530; border-radius: 5px;
              padding: 5px 8px; font-size: 0.78em; }
  .ind-chip .lbl { color: #6a7789; display: block; font-size: 0.85em; }
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
  .phase-tag.current { background: #1f4a2a; color: #3fd08a; }
  .phase-tag.done    { background: #1f3a2a; color: #50a350; }
  .phase-tag.future  { background: #1a2033; color: #6a7789; }
  .phase-item { font-size: 0.8em; padding: 3px 0; display: flex; align-items: flex-start;
                gap: 6px; color: #98a6ba; }
  .phase-item.done { color: #70c070; }
  .phase-item.todo { color: #a0a0b0; }
  .phase-item .chk { flex-shrink: 0; margin-top: 1px; }

  /* Tabs */
  .tabs { display: flex; gap: 0; border-bottom: 1px solid #1f2530; margin-bottom: 14px; }
  .tab { padding: 7px 16px; font-size: 0.82em; font-weight: 600; color: #6a7789;
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
  .btn-small   { background: #1a2333; color: #98a6ba; padding: 3px 10px;
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
  .ac-input { background: #0d121a; border: 1px solid #2a3142; color: #d4d4d4;
              border-radius: 4px; padding: 3px 6px; font-size: 11px;
              font-family: "Consolas", monospace; }
  .ac-input:focus { outline: none; border-color: #88b8ff; }
  .add-bot-card { margin-bottom: 18px; }
  .add-bot-form { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .add-bot-form input { background: #0d121a; border: 1px solid #2a3142; color: #d4d4d4;
                        border-radius: 5px; padding: 7px 10px; font-size: 0.85em;
                        font-family: "Consolas", monospace; }
  .add-bot-form input:focus { outline: none; border-color: #88b8ff; }
  .add-bot-form #new-bot-weight { width: 80px; }

  /* ==== Re-theme GRIS MOYEN + gros bloc (deploiement 15/07 v3, reversible) ==== */
  body { background:#79828f; color:#161e28; }
  .card, .bot-card { background:#a8b0bb; border-color:#838d9a; }
  .mindmap, .ind-chip, .phase-card { background:#9ca4b0; border-color:#838d9a; }
  .bot-stat, td, th, .sparkline, .tabs, .diag-threshold, .pair-ctrl { border-color:#838d9a; }
  .vote-bar-bg { background:#929aa6; }
  .pair-ctrl input, .ac-input, .add-bot-form input { background:#b6bdc7; border-color:#838d9a; color:#161e28; }
  .metric, h1, .bot-stat .value, .ind-chip .val, .bot-card h3, .vote-score, .node-label, .phase-title { color:#0c121b; }
  .card h2, .bot-stat .label, .submetric, th, .vote-label, .diag-threshold, .phase-item { color:#353e4a; }
  h1 small, .bot-card .symbol, .ind-chip .lbl, footer, .tab { color:#4e5764; }
  .pos-up { color:#076b3c !important; } .pos-down { color:#a81e31 !important; }
  .badge.paper { background:#d8cf9f; color:#5a4406; } .badge.live { background:#e0b3ba; color:#8a1f2c; }
  .badge.active { background:#a6d6bd; color:#075e37; } .badge.kill { background:#e6acac; color:#8a1414; }
  .no-trade-badge { background:#d8cf9f; border-color:#bda86a; color:#5a4406; }
  .tab:hover { color:#161e28; } .tab.active { color:#1652c8; border-bottom-color:#1652c8; }
  .btn-kill { background:#e6acac; color:#8a1414; } .btn-release { background:#a6d6bd; color:#075e37; }
  .btn-pause { background:#d8cf9f; color:#5a4406; } .btn-resume { background:#a6d6bd; color:#076b3c; }
  .btn-close { background:#e0b3ba; color:#8a1f2c; } .btn-open { background:#bcd0f2; color:#1652c8; }
  .btn-small { background:#9ca4b0; color:#353e4a; border-color:#838d9a; }
  .btn-small:hover { background:#929aa6; color:#161e28; }
  .node-bg { stroke:#838d9a; } .node-sub { fill:#353e4a; }
  .node-director { fill:#d8cf9f; } .node-active { fill:#a6d6bd; } .node-paused { fill:#e3d3b0; }
  .node-kill { fill:#e6acac; } .node-cold { fill:#9ca4b0; } .edge { stroke:#838d9a; }
  .top-grid { display:none; } /* remplacee par le gros bloc #hero */
  /* ---- GROS BLOC : hero valeur + jauges live ---- */
  #hero { display:grid; grid-template-columns:1.05fr 1.95fr; gap:14px; margin-bottom:18px; }
  #hero .hcard { background:#a8b0bb; border:1px solid #838d9a; border-radius:10px; padding:16px 18px; }
  #hero .hlabel { font-size:.66rem; letter-spacing:.13em; text-transform:uppercase; color:#353e4a; font-weight:700; }
  #hero .hval { font-family:"Consolas",monospace; font-size:2.5rem; font-weight:700; color:#0c121b; line-height:1.05; margin:8px 0 4px; }
  #hero .hdelta { display:inline-block; font-family:"Consolas",monospace; font-size:.82rem; padding:3px 10px; border-radius:999px; background:#929aa6; }
  #hero .hfoot { display:flex; gap:14px; flex-wrap:wrap; margin-top:12px; font-size:.74rem; color:#4e5764; }
  #hero .hfoot b { color:#161e28; }
  #hero .gauges { display:flex; flex-direction:column; gap:13px; justify-content:center; height:100%; }
  #hero .g .gt { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:5px; }
  #hero .g .gl { font-size:.8rem; color:#353e4a; font-weight:600; }
  #hero .g .gv { font-family:"Consolas",monospace; font-size:.86rem; font-weight:700; color:#0c121b; }
  #hero .gtrack { position:relative; height:11px; background:#929aa6; border:1px solid #838d9a; border-radius:6px; overflow:hidden; }
  #hero .gtrack.fg { overflow:visible; background:linear-gradient(90deg,#c0293c,#c68a1e 48%,#0c8a52); }
  #hero .gfill { position:absolute; left:0; top:0; bottom:0; width:0; border-radius:6px; transition:width 1s cubic-bezier(.22,1,.36,1); }
  #hero .gmk { position:absolute; top:-4px; bottom:-4px; width:3px; background:#0c121b; border-radius:2px; box-shadow:0 0 0 2px #a8b0bb; }
  #hero .gcap { display:flex; justify-content:space-between; margin-top:4px; font-size:.62rem; color:#4e5764; }
  @media(max-width:820px){ #hero{ grid-template-columns:1fr; } }
  /* ---- finition pro : ombres + declutter ---- */
  .card, .bot-card, #hero .hcard { box-shadow:0 1px 2px rgba(18,28,46,.10), 0 8px 22px rgba(18,28,46,.07); }
  .roadmap { display:none; }
  .card h2 { font-weight:700; letter-spacing:.06em; }
  /* ---- bandeau stats ---- */
  #statstrip { display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:18px; }
  #statstrip .ss { background:#a8b0bb; border:1px solid #838d9a; border-radius:10px; padding:11px 13px; box-shadow:0 1px 2px rgba(18,28,46,.10), 0 8px 22px rgba(18,28,46,.07); }
  #statstrip .ssl { font-size:.6rem; letter-spacing:.07em; text-transform:uppercase; color:#4e5764; font-weight:700; }
  #statstrip .ssv { font-family:"Consolas",monospace; font-size:1.12rem; font-weight:700; color:#0c121b; margin-top:5px; }
  @media(max-width:900px){ #statstrip{ grid-template-columns:repeat(3,1fr);} }
  @media(max-width:520px){ #statstrip{ grid-template-columns:repeat(2,1fr);} }
  /* ---- panneau parametres & seuils ---- */
  #params .params-head { display:flex; align-items:baseline; justify-content:space-between; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
  #params .params-head h2 { font-size:.78rem; color:#353e4a; text-transform:uppercase; letter-spacing:.06em; margin:0; font-weight:700; }
  #params .params-src { font-size:.68rem; color:#4e5764; font-family:"Consolas",monospace; }
  #params .params-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(172px,1fr)); gap:8px; }
  #params .pp { display:flex; align-items:center; justify-content:space-between; gap:8px; background:#9ca4b0; border:1px solid #838d9a; border-radius:9px; padding:8px 11px; }
  #params .pp .ppl { font-size:.74rem; color:#353e4a; }
  #params .pp .ppv { font-family:"Consolas",monospace; font-size:.85rem; font-weight:700; color:#0c121b; white-space:nowrap; }
  #params .pp .ppv.warn { color:#8a5e08; }

  /* ==== v5 DARK PREMIUM (theme unique actif — palette via tokens ci-dessous) ====
     Source de vurite des couleurs. Pour retoucher le theme : ne changer QUE ces valeurs.
     bg=fond  surf=carte  surf2=creux  bd=bordure  txt=texte  txt2=label  mut=discret
     acc=accent bleu  pos=vert  neg=rouge  warn=ambre                                */
  :root{
    --bg:#0b1017; --surf:#151d29; --surf2:#0f1722; --surf-hi:#1c2635;
    --bd:#25303f; --bd-hi:#33425a;
    --txt:#eef3fa; --txt2:#98a6ba; --mut:#6a7789;
    --acc:#5c9dff; --acc-soft:#16273f;
    --pos:#3fd08a; --neg:#ff6d7d; --warn:#e8b552;
  }
  body { background:var(--bg) !important; color:var(--txt) !important; }
  .card, .bot-card { background:var(--surf) !important; border-color:var(--bd) !important; }
  .mindmap, .ind-chip, .phase-card { background:var(--surf2) !important; border-color:var(--bd) !important; }
  .bot-stat, td, th, .sparkline, .tabs, .diag-threshold, .pair-ctrl { border-color:var(--bd) !important; }
  .vote-bar-bg { background:var(--surf2) !important; }
  .pair-ctrl input, .ac-input, .add-bot-form input { background:var(--surf2) !important; border-color:var(--bd) !important; color:var(--txt) !important; }
  .pair-ctrl input:focus, .ac-input:focus, .add-bot-form input:focus { border-color:var(--acc) !important; box-shadow:0 0 0 3px var(--acc-soft) !important; }
  .metric, h1, .bot-stat .value, .ind-chip .val, .bot-card h3, .vote-score, .phase-title { color:var(--txt) !important; }
  .card h2, .bot-stat .label, .submetric, th, .vote-label, .diag-threshold, .phase-item { color:var(--txt2) !important; }
  h1 small, .bot-card .symbol, .ind-chip .lbl, footer, .tab { color:var(--mut) !important; }
  .pos-up { color:var(--pos) !important; } .pos-down { color:var(--neg) !important; }
  .badge.paper { background:#33321c !important; color:var(--warn) !important; } .badge.live { background:#3a1c22 !important; color:var(--neg) !important; }
  .badge.active { background:#123321 !important; color:var(--pos) !important; } .badge.kill { background:#3a1720 !important; color:#ff5a6a !important; }
  .no-trade-badge { background:#2a2616 !important; border-color:#4a3f1f !important; color:var(--warn) !important; }
  .btn-kill { background:#5a1e24 !important; color:#ffb3ba !important; } .btn-release { background:#12402a !important; color:#8ff0bf !important; }
  .btn-pause { background:#3a3418 !important; color:var(--warn) !important; } .btn-resume { background:#123a2c !important; color:#5fe3ad !important; }
  .btn-close { background:#4a2030 !important; color:#ffb0c0 !important; } .btn-open { background:var(--acc-soft) !important; color:var(--acc) !important; }
  .btn-small { background:var(--surf-hi) !important; color:var(--txt2) !important; border-color:var(--bd) !important; }
  .tab:hover { color:var(--txt) !important; } .tab.active { color:var(--acc) !important; border-bottom-color:var(--acc) !important; }
  .node-bg { stroke:var(--bd) !important; } .node-label { fill:var(--txt) !important; } .node-sub { fill:var(--txt2) !important; }
  .node-director { fill:#4a3a1f !important; } .node-active { fill:#12402a !important; } .node-paused { fill:#4a2a1f !important; }
  .node-kill { fill:#4a1a22 !important; } .node-cold { fill:#1a2636 !important; } .edge { stroke:var(--bd-hi) !important; }
  /* blocs custom -> theme premium */
  #hero .hcard, #statstrip .ss, #params.card { background:var(--surf) !important; border-color:var(--bd) !important; }
  #hero .hlabel, #hero .g .gl, #statstrip .ssl, #params .params-head h2, #params .params-src, #hero .hfoot { color:var(--txt2) !important; }
  #hero .hval, #hero .g .gv, #statstrip .ssv, #params .pp .ppv, #hero .hfoot b { color:var(--txt) !important; }
  #hero .hval { color:#ffffff !important; }
  #params .pp { background:var(--surf2) !important; border-color:var(--bd) !important; }
  #params .pp .ppl { color:var(--txt2) !important; }
  #hero .gtrack { background:var(--surf2) !important; border-color:var(--bd) !important; }
  #hero .gtrack.fg { background:linear-gradient(90deg,#e5484d,#e8b552 48%,#3fd08a) !important; }
  #hero .gmk { background:var(--txt) !important; box-shadow:0 0 0 2px var(--surf) !important; }
  #hero .hdelta { background:var(--surf2) !important; }
  #params .pp .ppv.warn { color:var(--warn) !important; }
  /* accents fins */
  .bot-card.has-position { border-color:var(--pos) !important; }
  .tab.active { text-shadow:0 0 12px rgba(92,157,255,.35); }
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

  <!-- GROS BLOC : valeur + jauges (deploiement 15/07) -->
  <div id="hero">
    <div class="hcard">
      <div class="hlabel">Valeur du portefeuille</div>
      <div class="hval" id="hero-val">—</div>
      <span class="hdelta" id="hero-delta">—</span>
      <div class="hfoot">
        <span>réalisé <b id="hero-real">—</b></span>
        <span>latent <b id="hero-lat">—</b></span>
        <span>initial <b id="hero-init">—</b></span>
      </div>
    </div>
    <div class="hcard">
      <div class="hlabel">Instruments</div>
      <div class="gauges" style="margin-top:8px">
        <div class="g">
          <div class="gt"><span class="gl">Drawdown</span><span class="gv" id="g-dd-v">—</span></div>
          <div class="gtrack"><i class="gfill" id="g-dd" style="background:var(--pos)"></i></div>
          <div class="gcap"><span>0%</span><span>seuil watchdog 6%</span></div>
        </div>
        <div class="g">
          <div class="gt"><span class="gl">Exposition combinée</span><span class="gv" id="g-exp-v">—</span></div>
          <div class="gtrack"><i class="gfill" id="g-exp" style="background:var(--acc)"></i></div>
          <div class="gcap"><span>0%</span><span>cap 40%</span></div>
        </div>
        <div class="g">
          <div class="gt"><span class="gl">Fear &amp; Greed</span><span class="gv" id="g-fg-v">—</span></div>
          <div class="gtrack fg"><span class="gmk" id="g-fg" style="left:0%"></span></div>
          <div class="gcap"><span>Peur extrême</span><span>Avidité extrême</span></div>
        </div>
      </div>
    </div>
  </div>
  <script>
  (function(){
    var RM = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function fmt(n){ n = n||0; return (n<0?'-$':'$') + Math.abs(n).toFixed(2); }
    function pct(n){ n = n||0; return (n>=0?'+':'') + n.toFixed(2) + '%'; }
    function setW(id,w){ var el=document.getElementById(id); if(!el) return; var go=function(){ el.style.width=w+'%'; }; if(RM){ go(); } else { setTimeout(go,120); } }
    async function refresh(){
      try{
        var pf = await (await fetch('/api/portfolio')).json();
        var dr = await (await fetch('/api/director')).json();
        var sw = await (await fetch('/api/swarm')).json();
        var val = pf.total||0, init = pf.initial||0;
        document.getElementById('hero-val').textContent = fmt(val);
        var dEl = document.getElementById('hero-delta'), dp = pf.pnl_pct||0;
        dEl.textContent = (dp>=0?'▲ ':'▼ ')+pct(dp)+' · '+fmt(val-init);
        dEl.style.background = dp>=0 ? 'rgba(63,208,138,.15)' : 'rgba(255,109,125,.15)';
        dEl.style.color = dp>=0 ? '#3fd08a' : '#ff6d7d';
        document.getElementById('hero-real').textContent = fmt(pf.pnl_realized);
        document.getElementById('hero-lat').textContent = fmt(pf.pnl_latent);
        document.getElementById('hero-init').textContent = fmt(init);
        var dd = pf.drawdown_pct||0;
        document.getElementById('g-dd-v').textContent = dd.toFixed(2)+'%';
        setW('g-dd', Math.min(100, dd/6*100));
        var exp=0; (sw||[]).forEach(function(b){ if(b.position && b.position.qty && b.current_price){ exp += b.position.qty*b.current_price; } });
        var expPct = val>0 ? exp/val*100 : 0;
        document.getElementById('g-exp-v').textContent = '~'+expPct.toFixed(0)+'% / 40%';
        setW('g-exp', Math.min(100, expPct/40*100));
        var fg = (dr.fear_greed!=null) ? dr.fear_greed : 50;
        document.getElementById('g-fg-v').textContent = fg+' · '+(dr.fear_greed_label||'');
        document.getElementById('g-fg').style.left = Math.max(0,Math.min(100,fg))+'%';
      }catch(e){ /* silencieux : ne casse jamais le reste du dashboard */ }
    }
    refresh(); setInterval(refresh, 5000);
  })();
  </script>

  <!-- BANDEAU STATS (deploiement 15/07) -->
  <div id="statstrip">
    <div class="ss"><div class="ssl">Trades clôturés</div><div class="ssv" id="ss-closed">—</div></div>
    <div class="ss"><div class="ssl">Win rate</div><div class="ssv" id="ss-wr">—</div></div>
    <div class="ss"><div class="ssl">Meilleur symbole</div><div class="ssv" id="ss-best">—</div></div>
    <div class="ss"><div class="ssl">Positions ouvertes</div><div class="ssv" id="ss-pos">—</div></div>
    <div class="ss"><div class="ssl">Exposition</div><div class="ssv" id="ss-exp">—</div></div>
    <div class="ss"><div class="ssl">Décisions</div><div class="ssv" id="ss-dec">—</div></div>
  </div>
  <script>
  (function(){
    async function r(){
      try{
        var ts = await (await fetch('/api/trade_stats')).json();
        var pf = await (await fetch('/api/portfolio')).json();
        var sw = await (await fetch('/api/swarm')).json();
        var nClosed=0,nWins=0,best=null,bestV=-1e9;
        Object.keys(ts||{}).forEach(function(k){ var s=ts[k]||{}; nClosed+=s.n_closed||0; nWins+=s.wins||0; if((s.net_pnl_usdc||0)>bestV){ bestV=s.net_pnl_usdc||0; best=k; } });
        document.getElementById('ss-closed').textContent = nClosed;
        document.getElementById('ss-wr').textContent = nClosed ? Math.round(nWins/nClosed*100)+'%' : '—';
        document.getElementById('ss-best').textContent = best ? best.replace('-USDC','')+' '+(bestV>=0?'+':'')+bestV.toFixed(2)+'$' : '—';
        var nPos=0,exp=0; (sw||[]).forEach(function(b){ if(b.position && b.position.qty){ nPos++; if(b.current_price) exp+=b.position.qty*b.current_price; } });
        document.getElementById('ss-pos').textContent = nPos+' / '+(sw?sw.length:0);
        document.getElementById('ss-exp').textContent = '$'+exp.toFixed(0);
        document.getElementById('ss-dec').textContent = (pf.n_decisions||0).toLocaleString('fr-FR');
      }catch(e){}
    }
    r(); setInterval(r, 8000);
  })();
  </script>

  <!-- PARAMETRES & SEUILS (deploiement 15/07) -->
  <div id="params" class="card">
    <div class="params-head">
      <h2>Paramètres &amp; seuils actifs</h2>
      <span class="params-src">source : .env — lecture seule ici</span>
    </div>
    <div class="params-grid">
      <div class="pp"><span class="ppl">Taille / trade</span><span class="ppv" id="cfg-pos">—</span></div>
      <div class="pp"><span class="ppl">Exposition max</span><span class="ppv" id="cfg-exp">—</span></div>
      <div class="pp"><span class="ppl">SMA tendance</span><span class="ppv" id="cfg-sma">—</span></div>
      <div class="pp"><span class="ppl">Bande de sortie</span><span class="ppv" id="cfg-buf">—</span></div>
      <div class="pp"><span class="ppl">Stop catastrophe</span><span class="ppv" id="cfg-stop">—</span></div>
      <div class="pp"><span class="ppl">Fréquence check</span><span class="ppv" id="cfg-check">—</span></div>
      <div class="pp"><span class="ppl">Frais taker</span><span class="ppv" id="cfg-fee">—</span></div>
      <div class="pp"><span class="ppl">Entrées maker</span><span class="ppv" id="cfg-maker">—</span></div>
      <div class="pp"><span class="ppl">Filtre régime</span><span class="ppv" id="cfg-regime">—</span></div>
      <div class="pp"><span class="ppl">Ordre min</span><span class="ppv" id="cfg-minord">—</span></div>
      <div class="pp"><span class="ppl">Spread max</span><span class="ppv" id="cfg-spread">—</span></div>
    </div>
  </div>

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
      <h2>Valeur portefeuille</h2>
      <div class="metric" id="portfolio">—</div>
      <div class="submetric" id="pnl-total">—</div>
    </div>
    <div class="card">
      <h2>P&amp;L réalisé (net)</h2>
      <div class="metric" id="pnl-realized">—</div>
      <div class="submetric">trades clôturés, net de frais</div>
    </div>
    <div class="card">
      <h2>P&amp;L latent</h2>
      <div class="metric" id="pnl-latent">—</div>
      <div class="submetric" id="latent-note">positions ouvertes</div>
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
        <span style="display:flex;align-items:center;gap:8px;">Diagnostic tendance
          <select id="diag-bot" onchange="loadSignalDebug()"
                  style="background:var(--surf2);border:1px solid var(--bd);color:var(--txt);border-radius:6px;padding:2px 6px;font-size:12px;"></select>
        </span>
        <span id="no-trade-since" style="font-size:11px;font-weight:400;color:#e3c050;"></span>
      </h2>
      <div id="vote-buy-row" class="vote-row">
        <span class="vote-label">Au-dessus SMA</span>
        <div class="vote-bar-bg"><div class="vote-bar buy" id="vote-buy-bar" style="width:0%"></div></div>
        <span class="vote-score" id="vote-buy-score">0.00</span>
      </div>
      <div class="voters-list" id="vote-buy-voters">aucun votant</div>
      <div id="vote-sell-row" class="vote-row" style="margin-top:10px;">
        <span class="vote-label">Sous la SMA</span>
        <div class="vote-bar-bg"><div class="vote-bar sell" id="vote-sell-bar" style="width:0%"></div></div>
        <span class="vote-score" id="vote-sell-score">0.00</span>
      </div>
      <div class="voters-list sell" id="vote-sell-voters">aucun votant</div>
      <div class="diag-threshold">
        <strong id="vote-threshold">Signal : franchissement de la SMA</strong>
        &nbsp;|&nbsp; <span id="vote-status">en attente de données</span>
      </div>
    </div>

    <div class="card">
      <h2>Indicateurs temps réel (dernier signal)</h2>
      <div class="ind-grid" id="ind-grid">
        <div class="ind-chip"><span class="lbl">Prix</span><span class="val" id="ind-price">—</span></div>
        <div class="ind-chip"><span class="lbl">SMA</span><span class="val" id="ind-sma">—</span></div>
        <div class="ind-chip"><span class="lbl">Distance</span><span class="val" id="ind-dist">—</span></div>
        <div class="ind-chip"><span class="lbl">État</span><span class="val" id="ind-state">—</span></div>
        <div class="ind-chip"><span class="lbl">Période SMA</span><span class="val" id="ind-period">—</span></div>
        <div class="ind-chip"><span class="lbl">Confiance</span><span class="val" id="ind-conf">—</span></div>
        <div class="ind-chip"><span class="lbl">Pente SMA</span><span class="val" id="ind-slope">—</span></div>
        <div class="ind-chip"><span class="lbl">Âge tendance</span><span class="val" id="ind-age">—</span></div>
        <div class="ind-chip"><span class="lbl">SMA courte</span><span class="val" id="ind-smashort">—</span></div>
        <div class="ind-chip"><span class="lbl">Régime</span><span class="val" id="ind-regime">—</span></div>
        <div class="ind-chip"><span class="lbl">Volatilité</span><span class="val" id="ind-vol">—</span></div>
      </div>
      <div style="font-size:0.75em;color:#6a7789;margin-top:8px;" id="ind-symbol-ts">—</div>
    </div>
  </div>

  <!-- Courbe P&L portfolio -->
  <div class="card" style="margin-bottom:14px;">
    <h2 style="display:flex;justify-content:space-between;align-items:center;">
      <span>Courbe P&amp;L portfolio</span>
      <span style="font-size:11px;font-weight:400;color:#6a7789;">
        <button onclick="loadPnlCurve(7)" class="btn-small">7j</button>
        <button onclick="loadPnlCurve(30)" class="btn-small">30j</button>
        <button onclick="loadPnlCurve(90)" class="btn-small">90j</button>
      </span>
    </h2>
    <div style="position:relative; height:280px; padding-top:8px;">
      <canvas id="pnl-chart"></canvas>
    </div>
    <div style="font-size:12px;color:#98a6ba;margin-top:6px;text-align:center;" id="pnl-stats">—</div>
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
    <div id="add-bot-msg" style="font-size:0.8em;color:#98a6ba;margin-top:8px;"></div>
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
          <div class="phase-item done"><span class="chk">✅</span> Walk-forward validation</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Rapport P&amp;L / Sharpe / Max DD</div>
        </div>

        <div class="phase-card current">
          <div class="phase-title">Phase 4 — Live &amp; Sécurité <span class="phase-tag current">🔴 LIVE actif</span></div>
          <div class="phase-item done"><span class="chk">✅</span> Passage live Coinbase (capital réel)</div>
          <div class="phase-item done"><span class="chk">✅</span> Kill switch par bot + global</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Audit sécurité complet</div>
          <div class="phase-item todo"><span class="chk">⬜</span> Rate limiting &amp; circuit breakers</div>
          <div class="phase-item done"><span class="chk">✅</span> Alertes Telegram avancées (P&amp;L, drawdown)</div>
          <div class="phase-item done"><span class="chk">✅</span> Monitoring externe (uptime, alertes)</div>
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
  ctx.strokeStyle = up ? '#3fd08a' : '#ff6d7d';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // last price dot
  const lastX = w;
  const lastY = h - 2 - ((prices[prices.length-1] - min) / range) * (h - 6);
  ctx.beginPath();
  ctx.arc(lastX - 1, lastY, 3, 0, Math.PI * 2);
  ctx.fillStyle = up ? '#3fd08a' : '#ff6d7d';
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

async function renderParams() {
  const c = await fetch('/api/config').then(r => r.json()).catch(() => null);
  if (!c) return;
  const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  set('cfg-pos',    c.position_pct.toFixed(0) + '%');
  set('cfg-exp',    c.max_exposure.toFixed(0) + '%');
  set('cfg-sma',    'SMA' + c.sma_period);
  set('cfg-buf',    c.exit_buffer.toFixed(1) + '%');
  set('cfg-stop',   c.stop_loss > 0 ? '-' + c.stop_loss.toFixed(0) + '%' : 'OFF');
  set('cfg-check',  c.check_min.toFixed(0) + ' min');
  set('cfg-fee',    c.taker_fee.toFixed(2) + '%');
  set('cfg-maker',  c.maker ? 'ON' : 'OFF');
  set('cfg-regime', c.regime ? 'ON' : 'OFF');
  set('cfg-minord', '$' + c.min_order.toFixed(2));
  set('cfg-spread', c.max_spread.toFixed(2) + '%');
}

async function refresh() {
  renderParams();
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
    pnlEl.style.color = p.pnl_pct >= 0 ? '#3fd08a' : '#ff6d7d';

    const realizedEl = document.getElementById('pnl-realized');
    if (realizedEl && p.pnl_realized != null) {
      realizedEl.textContent = (p.pnl_realized >= 0 ? '+$' : '-$') + fmt(Math.abs(p.pnl_realized));
      realizedEl.style.color = p.pnl_realized >= 0 ? '#3fd08a' : '#ff6d7d';
    }
    const latentEl = document.getElementById('pnl-latent');
    if (latentEl && p.pnl_latent != null) {
      latentEl.textContent = (p.pnl_latent >= 0 ? '+$' : '-$') + fmt(Math.abs(p.pnl_latent));
      latentEl.style.color = p.pnl_latent >= 0 ? '#3fd08a' : '#ff6d7d';
    }
    const noteEl = document.getElementById('latent-note');
    if (noteEl && p.excluded && p.excluded.length) {
      noteEl.textContent = 'positions ouvertes (hors ' + p.excluded.join(', ') + ')';
    }

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
      const cls = 'bot-card'
        + (hasPos ? (pnlPct != null && pnlPct >= 0 ? ' profit' : ' loss') : '')
        + (b.paused ? ' paused' : '');
      // Infos dynamiques (Bot Dynamique)
      let dynInfo = '';
      if (b.bot_id === 'dynamique' && b.dynamic_perfs && Object.keys(b.dynamic_perfs).length) {
        const perfs = Object.entries(b.dynamic_perfs)
          .sort((a,b) => b[1]-a[1])
          .map(([s,v]) => `${s.split('-')[0]} ${v>=0?'+':''}${v.toFixed(1)}%`)
          .join(' | ');
        dynInfo = `<div class="bot-stat"><span class="label">24h</span><span class="value" style="font-size:0.8em;color:#98a6ba;">${perfs}</span></div>`;
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

      // Contrôle "close réglable" (présent seulement pour les TrendBots)
      let acCtrl = '';
      if (b.autoclose) {
        const ac = b.autoclose;
        const stateTxt = ac.active
          ? '<span style="color:#3fd08a;">● ON</span>'
          : '<span style="color:#6a7789;">○ OFF</span>';
        acCtrl = `
        <div class="pair-ctrl" style="flex-wrap:wrap;">
          <span style="font-size:11px;color:#98a6ba;width:100%;">Close réglable ${stateTxt}</span>
          <select id="ac-active-${b.bot_id}" class="ac-input">
            <option value="1" ${ac.active ? 'selected' : ''}>Actif</option>
            <option value="0" ${!ac.active ? 'selected' : ''}>Inactif</option>
          </select>
          <select id="ac-mode-${b.bot_id}" class="ac-input">
            <option value="trailing" ${ac.mode === 'trailing' ? 'selected' : ''}>Trailing</option>
            <option value="take_profit" ${ac.mode === 'take_profit' ? 'selected' : ''}>Take-profit</option>
          </select>
          <input type="number" id="ac-thr-${b.bot_id}" class="ac-input" style="width:52px;" step="0.5" min="0.5" value="${ac.threshold_pct}" />
          <span style="font-size:11px;color:#98a6ba;">%</span>
          <button class="btn-small" onclick="doAutoclose('${b.bot_id}')">💾 OK</button>
        </div>`;
      }

      grid.innerHTML += `<div class="${cls}">
        <h3>${b.name}
          <span style="color:#6a7789;font-size:0.75em;">${(b.weight*100).toFixed(0)}%</span>
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
        <div class="bot-pnl ${pnlPct!=null&&pnlPct>=0?'pos-up':'pos-down'}">${pnlPct!=null&&pnlPct>=0?'▲':'▼'} ${fmtPct(pnlPct)} <span style="font-size:.5em;font-weight:600;opacity:.65;letter-spacing:0;">P&L live</span></div>
        ` : ''}
        ${dynInfo}
        <canvas class="sparkline" data-botid="${b.bot_id}" width="260" height="44"></canvas>
        <div class="bot-actions">${pauseBtn}${closeBtn}</div>
        ${pairCtrl}${acCtrl}
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
    dc.innerHTML = '<div style="padding:14px;text-align:center;color:#6a7789;font-style:italic;">Aucune décision</div>';
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
        <td style="color:#98a6ba;font-size:0.85em;">${(d.reasoning || '').substring(0, 50)}</td>
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

async function doAutoclose(botId) {
  const active = document.getElementById('ac-active-' + botId).value === '1';
  const mode   = document.getElementById('ac-mode-' + botId).value;
  const thr    = parseFloat(document.getElementById('ac-thr-' + botId).value || '5');
  const res = await postJson('/api/bot/' + botId + '/autoclose',
                             {active, mode, threshold_pct: thr});
  if (res.ok) refresh();
  else alert('Close réglable refusé : ' + (res.error || 'erreur'));
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
    msg.style.color = '#3fd08a';
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
    `<span style="color:${pnlPct>=0?'#3fd08a':'#ff6d7d'};">P&L ${days}j: ${pnlPct>=0?'+':''}${pnlPct.toFixed(2)}%</span> | ` +
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
          borderColor: pnlPct >= 0 ? '#3fd08a' : '#ff6d7d',
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
          backgroundColor: '#151d29',
          borderColor: '#2a3142',
          borderWidth: 1,
          callbacks: {
            label: (ctx) => `${ctx.parsed.y.toFixed(2)} USDC (drawdown: ${drawdowns[ctx.dataIndex]}%)`,
          },
        },
      },
      scales: {
        x: { ticks: { color: '#6a7789', maxRotation: 0, autoSkipPadding: 30 },
             grid: { color: 'rgba(255,255,255,0.04)' } },
        y: { ticks: { color: '#6a7789', callback: v => '$' + v.toFixed(0) },
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

  const meta   = d.metadata || {};
  const dist   = meta.dist_pct   != null ? parseFloat(meta.dist_pct)   : null;
  const sma    = meta.sma        != null ? parseFloat(meta.sma)        : null;
  const price  = meta.live_price != null ? parseFloat(meta.live_price) : null;
  const period = meta.sma_period != null ? meta.sma_period : 50;
  const isLong = d.action === 'buy';

  // Barres : distance a la SMA (au-dessus = vert, sous = rouge). Echelle 20% = plein.
  const scale = 20;
  const above = (dist != null && dist > 0) ? Math.min(100, (dist / scale) * 100) : 0;
  const below = (dist != null && dist < 0) ? Math.min(100, (-dist / scale) * 100) : 0;
  document.getElementById('vote-buy-bar').style.width  = above + '%';
  document.getElementById('vote-sell-bar').style.width = below + '%';
  document.getElementById('vote-buy-score').textContent  = (dist != null && dist > 0) ? '+' + dist.toFixed(1) + '%' : '—';
  document.getElementById('vote-sell-score').textContent = (dist != null && dist < 0) ? dist.toFixed(1) + '%' : '—';
  document.getElementById('vote-buy-voters').textContent  = isLong  ? '▲ prix au-dessus de la SMA' + period : '—';
  document.getElementById('vote-sell-voters').textContent = !isLong ? '▼ prix sous la SMA' + period : '—';

  // Statut tendance
  const statusEl = document.getElementById('vote-status');
  if (dist == null) { statusEl.textContent = 'en attente de données'; statusEl.style.color = '#98a6ba'; }
  else if (isLong)  { statusEl.textContent = '🟢 LONG (tendance haussière)'; statusEl.style.color = '#3fd08a'; }
  else              { statusEl.textContent = '⚪ FLAT (sous la SMA' + period + ')'; statusEl.style.color = '#e3c050'; }

  // Depuis le dernier trade
  if (d.last_trade_ts) {
    const mins = Math.round((Date.now() - new Date(d.last_trade_ts)) / 60000);
    const h = Math.floor(mins / 60), m = mins % 60;
    document.getElementById('no-trade-since').textContent =
      `Sans trade : ${h ? h + 'h ' : ''}${m}min`;
  } else {
    document.getElementById('no-trade-since').textContent = 'Aucun trade enregistré';
  }

  // Indicateurs tendance
  const setChip = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  const fmtPrice = v => v == null ? '—' : v.toLocaleString('fr-FR', {maximumFractionDigits: v < 10 ? 4 : 2});
  setChip('ind-price', fmtPrice(price));
  setChip('ind-sma',   fmtPrice(sma));
  const distEl = document.getElementById('ind-dist');
  if (distEl) {
    distEl.textContent = dist != null ? (dist >= 0 ? '+' : '') + dist.toFixed(2) + '%' : '—';
    distEl.style.color = dist == null ? '' : (dist >= 0 ? '#3fd08a' : '#ff6d7d');
  }
  setChip('ind-state',  dist == null ? '—' : (isLong ? '📈 long' : '⚪ flat'));
  setChip('ind-period', period + ' j');
  setChip('ind-conf',   d.confidence != null ? Math.round(d.confidence * 100) + '%' : '—');

  // ── Indicateurs de tendance (affichage seul, calcules par trend_daily) ──
  const num = v => (v == null || isNaN(parseFloat(v))) ? null : parseFloat(v);
  const slope = num(meta.sma_slope_pct);
  const slopeEl = document.getElementById('ind-slope');
  if (slopeEl) {
    slopeEl.textContent = slope == null ? '—' : (slope >= 0 ? '↑ +' : '↓ ') + slope.toFixed(2) + ' %/j';
    slopeEl.style.color = slope == null ? '' : (slope >= 0 ? '#3fd08a' : '#ff6d7d');
  }
  const age = num(meta.trend_age_days);
  setChip('ind-age', age == null ? '—'
    : (meta.trend_age_side === 'up' ? '↑ ' : '↓ ') + age + (meta.trend_age_capped ? '+ j' : ' j'));
  const smaS = num(meta.sma_short), spread = num(meta.sma_spread_pct);
  setChip('ind-smashort', smaS == null ? '—'
    : fmtPrice(smaS) + (meta.sma_short_period ? ' (' + meta.sma_short_period + 'j)' : '')
      + (spread == null ? '' : ' · ' + (spread >= 0 ? '+' : '') + spread.toFixed(1) + '%'));
  const r2 = num(meta.trend_r2);
  const regEl = document.getElementById('ind-regime');
  if (regEl) {
    if (meta.trend_regime == null) { regEl.textContent = '—'; regEl.style.color = ''; }
    else {
      const isTrend = meta.trend_regime === 'trend';
      regEl.textContent = (isTrend ? '📈 tendance' : '↔ range') + (r2 != null ? ' (R² ' + r2.toFixed(2) + ')' : '');
      regEl.style.color = isTrend ? '#3fd08a' : '#e8b552';
    }
  }
  const vol = num(meta.volatility_pct);
  setChip('ind-vol', vol == null ? '—' : vol.toFixed(2) + ' %/j');

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
    tc.innerHTML = '<div style="padding:20px;text-align:center;color:#6a7789;font-style:italic;">Aucun trade exécuté pour l\'instant — le bot attend un signal fort (score ≥ seuil).</div>';
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
      <td style="color:#98a6ba;font-size:0.85em;">${(d.reasoning||'').substring(0,60)}</td>
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
  h1 small { color: #6a7789; font-size: 0.55em; font-weight: 400; display: block; }
  .price-big { font-size: 2.4em; font-weight: 800; color: #f0f0f0;
               font-family: "Consolas", monospace; margin: 20px 0 4px; }
  .price-change { font-size: 1em; margin-bottom: 24px; }
  .up   { color: #3fd08a; }  .down { color: #ff6d7d; }
  .card { background: #131820; border: 1px solid #1f2530;
          border-radius: 8px; padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 0.75em; color: #98a6ba; text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 14px; }
  .stat { display: flex; justify-content: space-between; padding: 7px 0;
          font-size: 0.9em; border-bottom: 1px solid #1a2030; }
  .stat:last-child { border-bottom: 0; }
  .stat .label { color: #98a6ba; }
  .stat .value { font-family: "Consolas", monospace; color: #f0f0f0; }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
  .btn { padding: 10px 20px; border-radius: 6px; border: none; cursor: pointer;
         font-size: 0.9em; font-weight: 700; transition: opacity 0.15s; }
  .btn:hover { opacity: 0.8; }
  .btn-pause   { background: #3b3a1f; color: #e3c050; }
  .btn-resume  { background: #1f4a1f; color: #3fd08a; }
  .btn-kill    { background: #7a1f1f; color: #ffaaaa; }
  .btn-release { background: #1f4a1f; color: #aaffaa; }
  .btn-close   { background: #5a2030; color: #ffb0b0; }
  canvas.chart { display: block; width: 100%; height: 120px; }
  .status-badge { display: inline-block; padding: 4px 12px; border-radius: 4px;
                  font-size: 0.85em; font-weight: 700; }
  .status-active { background: #1f4a2a; color: #3fd08a; }
  .status-paused { background: #4a2a1f; color: #e3a050; }
  .status-kill   { background: #5a1f1f; color: #ff5050; }
  .status-cold   { background: #1f2a3a; color: #98a6ba; }
  footer { text-align: center; margin-top: 30px; font-size: 0.75em; color: #6a7789; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 8px; font-size: 0.83em;
           border-bottom: 1px solid #1f2530; }
  th { color: #98a6ba; font-weight: 600; font-size: 0.75em; text-transform: uppercase; }
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
  const col = up ? '#3fd08a' : '#ff6d7d';

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
      el.style.color = pnlPct >= 0 ? '#3fd08a' : '#ff6d7d';
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
  tsPnl.style.color = ts.n_closed === 0 ? '#9aa4b2' : (ts.net_pnl_usdc >= 0 ? '#3fd08a' : '#ff6d7d');

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
      '<div style="padding:12px;color:#6a7789;font-style:italic">Aucune décision pour ce bot</div>';
  } else {
    let html = '<table><thead><tr><th>Heure</th><th>Type</th><th>Action</th><th>Conf.</th><th>Raison</th></tr></thead><tbody>';
    dec.forEach(d => {
      const conf = d.confidence != null ? Math.round(d.confidence*100)+'%' : '—';
      const cls  = d.action==='buy' ? 'style="color:#3fd08a"' : d.action==='sell' ? 'style="color:#ff6d7d"' : '';
      html += `<tr><td>${d.timestamp.substring(11,16)}</td><td>${d.task_type}</td>
               <td ${cls}>${d.action||'—'}</td><td>${conf}</td>
               <td style="color:#98a6ba">${(d.reasoning||'').substring(0,60)}</td></tr>`;
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

    total  = None
    latent = None
    if swarm:
        try:
            snap      = await swarm.get_portfolio_snapshot()
            total     = snap.get("total_usdc")
            positions = snap.get("positions", {}) or {}
            # P&L latent des positions STRATEGIE (exclut les residus hors-strategie)
            latent = round(sum((p.get("pnl_usdc") or 0.0)
                               for s, p in positions.items()
                               if s.upper() not in EXCLUDED_SYMBOLS), 2)
        except Exception as exc:
            log.warning("portfolio_snapshot_failed", error=str(exc))

    realized = None
    with _db() as conn:
        n_decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        last_dec    = conn.execute(
            "SELECT timestamp FROM decisions ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        # P&L realise net cumule (trades fermes, tous roles executants)
        try:
            realized = round(sum(v["net_pnl_usdc"] for v in _realized_stats(conn).values()), 2)
        except Exception:
            realized = None
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
        "pnl_realized":     realized,
        "pnl_latent":       latent,
        "excluded":         sorted(EXCLUDED_SYMBOLS),
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
    # roles executants : orchestrator (scalpeur historique) + trend_bot (bots
    # actuels) + user (cloture manuelle) — sinon P&L realise faux pour les trend.
    rows = conn.execute(
        "SELECT symbol, action, metadata FROM decisions "
        "WHERE task_type='order' AND role IN ('orchestrator','trend_bot','user') "
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


async def handle_autoclose(request: web.Request) -> web.Response:
    """POST /api/bot/<id>/autoclose {active, mode, threshold_pct} — close réglable."""
    from agents import autoclose
    bot_id = request.match_info["bot_id"].lower()
    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = autoclose.set_config(
        bot_id,
        active=bool(body.get("active", False)),
        mode=str(body.get("mode", "trailing")),
        threshold_pct=body.get("threshold_pct", 5.0),
    )
    return web.json_response({"ok": True, "bot_id": bot_id, **cfg})


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


async def handle_config(request: web.Request) -> web.Response:
    """Config live (relit le .env a chaque appel) pour la carte 'Parametres & seuils'."""
    def fnum(k: str, d: str) -> float:
        try:
            return float(os.getenv(k, d))
        except Exception:
            return float(d)
    def fbool(k: str) -> bool:
        return os.getenv(k, "false").lower() in ("true", "1", "yes")
    return web.json_response({
        "position_pct": fnum("TREND_POSITION_PCT", "0.03") * 100,
        "max_exposure": fnum("RISK_MAX_COMBINED_EXPOSURE_PCT", "0.40") * 100,
        "sma_period":   int(fnum("TREND_SMA_PERIOD", "50")),
        "exit_buffer":  fnum("TREND_EXIT_BUFFER_PCT", "0"),
        "stop_loss":    fnum("TREND_STOP_LOSS_PCT", "0"),
        "check_min":    fnum("TREND_CHECK_S", "300") / 60,
        "taker_fee":    fnum("COINBASE_TAKER_FEE_PCT", "0.0075") * 100,
        "maker":        fbool("LIVE_USE_MAKER_ENTRIES"),
        "regime":       fbool("REGIME_FILTER_ENABLED"),
        "min_order":    fnum("MIN_ORDER_USDC", "1.0"),
        "max_spread":   fnum("LIVE_MAX_SPREAD_PCT", "0.15"),
    })


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
    app.router.add_get("/api/config",              handle_config)
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
    app.router.add_post("/api/bot/{bot_id}/autoclose", handle_autoclose)
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
