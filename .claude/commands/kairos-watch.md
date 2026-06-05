---
description: Bilan de santé Kairos avec jugement (dashboard + logs + état) — check à la demande
---

Tu fais un check de santé de Kairos Alpha (bot de trading LIVE, capital réel). Objectif : un diagnostic court **avec jugement**, pas un dump de logs.

Étapes :
1. État live du portefeuille : `curl -s http://localhost:8080/api/portfolio` puis `curl -s http://localhost:8080/api/swarm`.
2. Fin du log `logs/trading.log` (dernières lignes) — repère : erreurs (`"level": "error"`), état du kill switch (`kill_switch`, `fear_greed_fetched`), et la **fraîcheur** (timestamp de la dernière ligne vs maintenant).
3. Si pertinent, `git log --oneline -3`.

Puis donne un bilan en **4-6 lignes max** :
- Bot vivant ? (dashboard répond + log frais)
- Capital, drawdown, kill switch ON/OFF + raison (Fear & Greed ?), nb de positions ouvertes.
- Toute **anomalie qui mérite l'attention** : erreurs en hausse, position bloquée par le kill switch, log figé, dashboard muet…
- Si tout est nominal, dis-le en une ligne.

Priorise ce qui est **inhabituel**. Sois bref et factuel. Ne modifie rien (lecture seule).
