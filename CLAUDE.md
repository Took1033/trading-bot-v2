# CLAUDE.md — Trading Bot v2

## Contexte du projet
Bot de trading crypto multi-agents. Phase actuelle : **paper trading uniquement**.
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
- Mode `COINBASE_MODE=paper` par défaut. Tout passage en `live` nécessite confirmation explicite.
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
| Analyse de signaux, résumés | Sonnet 4.6 | medium |
| Conception alpha, debug cross-agent | Opus 4.7 | xhigh |
| Phase scaffolding / boilerplate | Sonnet 4.6 | low |

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

# 3. Lancer le bot Telegram (quand implémenté)
python interfaces/telegram_bot.py
```

## Objectifs par phase

- **Phase 1 (actuelle)** : Scaffolding + paper trading BTC-USD via Coinbase Sandbox
- **Phase 2** : Premier signal automatique + notification Telegram
- **Phase 3** : Backtesting sur données historiques, validation de la stratégie
- **Phase 4** : Revue de sécurité complète avant tout passage en live

## Ce que Claude NE doit PAS faire
- Passer `COINBASE_MODE=live` sans demande explicite
- Stocker des prix OHLCV bruts en base (utiliser uniquement des indicateurs/résumés)
- Créer des abstractions anticipatoires (YAGNI)
- Utiliser Opus 4.7 pour du scaffolding ou des tâches répétitives
