# CLAUDE.md — Trading Bot v2

## Contexte du projet
Bot de trading crypto multi-agents (« Kairos »). **Statut : LIVE — capital réel engagé sur Coinbase**
(bascule paper→live assumée, `COINBASE_MODE=live`). Swarm de **5 TrendBots** (BTC/ETH/SOL/XRP/DOGE).
Tourne en continu via une tâche planifiée Windows (`KairosBot`, auto-restart), détaché de Claude Desktop.
Objectif commercial à terme : stratégies reproductibles, auditables, déployables.

## Architecture

```
bots/
├── agents/
│   ├── orchestrator.py     ← chef d'orchestre MCP
│   ├── market_agent.py     ← analyse de marché + signaux
│   ├── risk_agent.py       ← contrôle du risque (position size, stop-loss)
│   └── memory_agent.py     ← lecture/écriture SQLite, résumés structurels
├── interfaces/
│   ├── telegram_bot.py     ← notifications + commandes utilisateur
│   └── coinbase_client.py  ← API Coinbase (paper/live switch)
├── strategies/             ← logiques alpha (1 fichier par stratégie)
├── memory/
│   ├── schema.py           ← init DB + UUID déterministes SHA256
│   └── trading.db          ← base SQLite (gitignored)
├── logs/                   ← logs structurés JSON (gitignored)
├── .env                    ← secrets (gitignored)
├── .env.example            ← template public
└── requirements.txt
```

## Règles de développement

### Sécurité
- Jamais de clé API en dur dans le code. Toujours via `os.getenv()`.
- **Le bot tourne en `live` (capital réel).** Ne JAMAIS élargir l'exposition (taille de position,
  nombre de bots, univers d'actifs) ni assouplir les paramètres de risque / stop sans confirmation
  explicite de Brice. Repli sécurité : `COINBASE_MODE=paper`.
- Les tests automatisés ne touchent JAMAIS le compte réel : `run_tests.py` force `paper`, neutralise
  les notifs et exclut `test_live.py` (seul script à interroger la vraie API + le solde réel).
- La base SQLite ne stocke jamais de prix bruts historiques ni de P&L détaillé — uniquement des résumés structurels (leakage prevention).

### Mémoire persistante (UUID déterministes)
- Chaque décision est identifiée par `SHA256(role:task_type:timestamp_iso)`.
- Les enregistrements sont **immuables** : pas de UPDATE sur `decisions`. On insère un nouveau record.
- Le `memory_agent` est le seul à écrire en base. Les autres agents passent par lui.

### Protocole MCP entre agents
Les agents communiquent via des messages JSON typés :
```json
{
  "node_type": "control | artifact | error",
  "sender": "orchestrator",
  "receiver": "market_agent",
  "payload": { ... },
  "timeout_ms": 5000,
  "retry_budget": 2
}
```
L'orchestrateur envoie des `control`, les agents répondent avec des `artifact` ou `error`.

### Calibrage de l'effort Claude API
| Tâche | Modèle | Effort |
|-------|--------|--------|
| Rapports journaliers, logs | Haiku 4.5 | low |
| Analyse de signaux, résumés | Sonnet 5 | medium |
| Conception alpha, debug cross-agent | Opus 4.8 | xhigh |
| Scaffolding / boilerplate | Sonnet 5 | low |

### Style de code
- Python 3.11+, typage strict (`from __future__ import annotations`).
- Async partout (`asyncio`), pas de `time.sleep()` bloquant.
- Logs via `structlog` en JSON, jamais `print()` en production.
- Une stratégie = un fichier dans `strategies/`, interface standardisée : `async def analyze(symbol, data) -> Signal`.

## Commandes prioritaires pour démarrer

```bash
# 1. Environnement
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

# 2. Init base de données
python memory/schema.py

# 3. Lancer la suite de tests (hermétique, paper forcé) — AVANT tout commit/push
python run_tests.py

# 4. Le bot en prod tourne via la tâche planifiée Windows `KairosBot`
#    (lanceur : pythonw run_with_restart.py, auto-restart, instance unique).
#    État : http://localhost:8080   |   Start/Stop-ScheduledTask -TaskName KairosBot
```

> **Filet anti-régression** : `run_tests.py` est la source de vérité, appelée à l'identique
> par le hook `.githooks/pre-push` (activer : `git config core.hooksPath .githooks`) et par
> GitHub Actions (`.github/workflows/ci.yml`). Un push est bloqué si un test échoue.

## Objectifs par phase

- **Phases 1–2 (faites)** : scaffolding, paper trading, signal automatique + notification Telegram.
- **Phase 3 (faite)** : backtesting historique — 11 harnais `run_backtest_*.py`. Reste à **consolider**
  en un rapport comparatif unique (Sharpe / drawdown / walk-forward) → « carte d'identité » de la strato.
- **Live (en cours)** : swarm de 5 TrendBots en capital réel, app mobile (PWA), fiabilisation de
  l'exécution (throttle 429, fill réel, verrou d'ordre) et **observabilité** (réconciliation live).
- **Suite** : preuve d'edge reproductible + page publique `/track`.

## Ce que Claude NE doit PAS faire
- **Élargir l'exposition ou assouplir le risque en live sans demande explicite** (le bot trade déjà réel).
- Faire tourner `test_live.py` en automatisé (il touche la vraie API + le solde réel).
- Stocker des prix OHLCV bruts en base (utiliser uniquement des indicateurs/résumés).
- Créer des abstractions anticipatoires (YAGNI).
- Utiliser Opus pour du scaffolding ou des tâches répétitives (voir tableau de calibrage).
