# Trading Bot v2

Bot de trading crypto multi-agents en **paper trading** (simulation).  
Architecture MCP, Python 3.11+, asyncio, SQLite, Telegram.

---

## Démarrage rapide

```bash
# 1. Activer l'environnement
.venv\Scripts\activate

# 2. Copier et configurer
copy .env.example .env
# → Éditer .env : mettre TELEGRAM_BOT_TOKEN

# 3. Initialiser la base de données
python memory/schema.py

# 4. Lancer le bot complet (orchestrateur + Telegram)
python main.py
```

Puis dans Telegram : `/register` pour activer les notifications automatiques.

---

## Architecture

```
bots/
├── main.py                  ← Entrypoint unifié (orchestrateur + Telegram)
├── agents/
│   ├── orchestrator.py      ← Chef d'orchestre : boucle tick → signal → ordre
│   ├── market_agent.py      ← Récupère prix + génère signaux via stratégie
│   ├── risk_agent.py        ← Valide position size, confiance, doublons
│   ├── memory_agent.py      ← Seul agent autorisé à écrire en SQLite
│   └── trading_state.py     ← État partagé pause/resume (même processus)
├── interfaces/
│   ├── coinbase_client.py   ← API Coinbase (paper/live switch)
│   ├── telegram_bot.py      ← Bot Telegram (commandes + build_app)
│   └── notifier.py          ← Envoi de notifications Telegram (silencieux)
├── strategies/
│   ├── simple_ma.py         ← Stratégie EMA 9/21 (golden/death cross)
│   └── backtester.py        ← Backtesteur + fetch_prices_coinbase
├── memory/
│   ├── schema.py            ← Init DB + UUID déterministes SHA256
│   ├── trading.db           ← SQLite (gitignored)
│   └── telegram_config.json ← Chat ID (créé par /register, gitignored)
├── logs/                    ← Logs JSON structlog (gitignored)
├── .env                     ← Secrets (gitignored)
└── .env.example             ← Template public
```

---

## Commandes Telegram

| Commande | Description |
|---|---|
| `/register` | Enregistre ce chat pour les notifications automatiques |
| `/status` | État du bot, mode, dernier snapshot |
| `/decisions [N]` | N dernières décisions (défaut : 5, max : 20) |
| `/price` | Prix actuel BTC-USDC |
| `/pnl` | P&L depuis le début du paper trading |
| `/mode` | Mode actuel (paper / live) |
| `/stop` | Met le trading en pause |
| `/resume` | Reprend le trading |

---

## Stratégie : EMA Crossover 9/21

- **BUY** → golden cross (EMA9 passe au-dessus d'EMA21)
- **SELL** → death cross (EMA9 passe en-dessous d'EMA21)
- **HOLD** → pas de croisement, ou historique insuffisant (< 22 points)
- Confiance calculée sur l'amplitude relative du croisement (55 % – 95 %)

---

## Backtester

```python
import asyncio
from strategies.backtester import Backtester, fetch_prices_coinbase
from strategies.simple_ma import analyze

async def main():
    # 300 jours de données journalières BTC
    prices = await fetch_prices_coinbase("BTC-USD", granularity=86400, limit=300)
    
    bt = Backtester(strategy=analyze, initial_usdc=10_000.0)
    result = await bt.run("BTC-USD", prices)
    
    print(result.summary())
    print(result.trades_summary())

asyncio.run(main())
```

Lancer directement :
```bash
python strategies/backtester.py
```

---

## Variables d'environnement clés

| Variable | Défaut | Description |
|---|---|---|
| `COINBASE_MODE` | `paper` | `paper` = simulation locale, `live` = API réelle |
| `TRADING_SYMBOL` | `BTC-USDC` | Paire tradée |
| `LOOP_INTERVAL_S` | `60` | Intervalle entre chaque tick (secondes) |
| `RISK_MAX_POSITION_PCT` | `0.02` | Taille max par trade (2 % du portefeuille) |
| `RISK_MIN_CONFIDENCE` | `0.55` | Confiance minimale pour déclencher un ordre |

---

## Phases

| Phase | Statut | Description |
|---|---|---|
| 1 | ✅ | Scaffolding + paper trading BTC-USDC |
| 2 | ✅ | Signaux automatiques + notifications Telegram |
| 3 | 🔄 | Backtesting sur données historiques |
| 4 | ⏳ | Revue de sécurité avant live |

---

## Règles importantes

- **`COINBASE_MODE=live` ne doit jamais être activé sans validation explicite** (Phase 4)
- La DB SQLite ne stocke jamais de prix OHLCV bruts — uniquement des résumés
- Les décisions sont **immuables** (INSERT OR IGNORE, jamais UPDATE)
- Seul `MemoryAgent` écrit en base — les autres agents passent par lui
