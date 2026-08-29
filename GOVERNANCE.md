# GOUVERNANCE des changements de paramètres — Kairos

> **But.** Empêcher qu'un chiffre qui « a l'air mieux » sur l'historique devienne un
> changement en capital réel sans preuve. Ce document est un **protocole**, pas une
> opinion : une checklist à suivre avant de toucher un paramètre du bot live.
>
> **Cadre.** Ceci régit la **méthode**. Toute décision de trading et tout acte sur le
> compte réel restent à Brice. Conformément au `CLAUDE.md` : ne jamais élargir
> l'exposition ni assouplir le risque sans décision explicite.

---

## Pourquoi ce document existe (la leçon exit-1 %)

Le projet a construit un appareil rigoureux (walk-forward, sensibilité aux frais,
gate GO/NO-GO) **pour se prémunir de l'optimisation in-sample**… puis a failli
activer une bande de sortie « 1 % » choisie **par argmax sur les mêmes 5 ans**
(`run_backtest_hysteresis.py`, grille de 7 valeurs, agrégat non ajusté du risque).

Le walk-forward de la bande (`run_backtest_band_walkforward.py`) a tranché : la bande
**ne bat pas proprement le flip-strict hors échantillon** (12/25 fenêtres OOS). Le
« +22 pts » était un artefact concentré. **Sans ce test, on aurait déployé du
curve-fitting en capital réel.** Ce protocole rend ce test obligatoire.

---

## Les deux erreurs que ce protocole bloque

1. **Optimiser puis déployer.** Choisir une valeur qui maximise la performance
   *passée* (in-sample) et la traiter comme une découverte. Toute grille assez fine
   trouve un paramètre qui « améliore » l'historique — c'est de la sur-optimisation,
   pas un edge.
2. **Juger le mauvais objet.** Le rapport de preuve mesure l'alpha du **signal**,
   actif par actif, all-in. Le produit live est un **book** (swarm + cap 6 %,
   ~94 % en cash). Une décision qui reshape le book (couper des bots, changer le cap)
   doit être validée **au niveau book**, pas sur un backtest single-asset.

---

## Protocole — 7 conditions avant tout changement de paramètre live

Aucun paramètre du `.env` live (SMA, bandes d'hystérésis, sizing, cap, stop, filtre
de régime, univers de bots) ne change sans que **les 7** soient satisfaites :

- [ ] **1. Hypothèse pré-déclarée.** Écrire *avant* de regarder les résultats : quel
  est le **mécanisme** attendu (le *pourquoi*, indépendant du backtest), et quelle
  amélioration on prédit. Un changement sans mécanisme ex-ante est du data-mining.
- [ ] **2. Validation HORS échantillon.** Le bénéfice doit survivre à un test OOS :
  walk-forward avec **re-sélection** du paramètre sur le seul passé, ou split
  temporel train/test. **Un argmax in-sample ne compte pas comme preuve.** La
  robustesse du **signe/plateau** (le voisinage améliore aussi) prime sur la valeur
  exacte (un pic isolé = drapeau rouge d'overfit).
- [ ] **3. Bon objet.** Si le changement affecte le **book** (nombre de bots, sizing,
  cap, allocation), le valider sur `run_backtest_portfolio.py` (swarm + cap), pas
  seulement sur `edge_report.py` (single-asset). Mesurer ce qui tourne réellement.
- [ ] **4. Horizon et seuil déclarés.** Avant d'activer : sur **combien de temps** on
  évalue en live, et quel **seuil** déclenche « on garde » vs « on revert ». Sans
  critère d'échec pré-écrit, on rationalise toujours a posteriori.
- [ ] **5. Un seul changement à la fois.** Jamais deux leviers simultanés (ex.
  hystérésis **et** retrait d'un bot) — sinon l'effet est non attribuable.
- [ ] **6. Réversibilité + filet vert.** Le changement est un one-liner de config
  réversible instantanément ; `python run_tests.py` est **vert** ; la fidélité
  backtest↔live est re-vérifiée si le comportement d'exécution change.
- [ ] **7. Trace immuable.** Consigner la décision (hypothèse, date, valeur avant/après,
  test OOS de référence, horizon/seuil) — cohérent avec la philosophie
  `decisions` immuables. Une décision non journalisée n'a pas eu lieu.

> **Règle du one-way door.** Les changements de config (bande, activation d'un bot)
> sont réversibles → à traiter comme des **expériences monitorées**. Les actes
> irréversibles (couper définitivement, augmenter le capital, relever le cap) exigent
> en plus une justification explicite et écrite : ils cristallisent une décision.

---

## Application aux questions ouvertes (au 2026-08-29)

| Décision | Ce que dit la preuve actuelle | Ce qu'il manque avant d'agir |
|---|---|---|
| **Activer la bande 1 %** | Mécanisme sain (moins de whipsaw), mais **ne bat pas le flip-strict OOS** (12/25 fenêtres). Le « +22 pts » est in-sample. | Un plateau OOS clair (condition 2). En l'état : **flip-strict reste le défaut** ; la bande est un candidat, pas une amélioration prouvée. |
| **Couper les 4 alts non-prouvés** | « Non prouvé robuste » ≠ « perdant ». Au **niveau book** (cap 6 %), swarm vs BTC-concentré est un **vrai arbitrage** (swarm +rendt, BTC-conc +Sharpe/−DD/−complexité), aucun ne domine. | Le verdict est **instable** (ETH champion en juin, fragile en août). Couper = acte quasi-irréversible sur une mesure bruitée. Manque : plus de recul live + décision sur la **politique de risque par niveau de preuve** (sizing différencié, filtre de régime), pas sur le *nombre* de bots. |
| **Toucher au cap / sizing** | Non backtesté — c'est pourtant **la vraie variable de risque** du book. | Balayer le cap dans `run_backtest_portfolio.py` ; **confirmer d'abord les valeurs LIVE réelles** (`.env` gitignoré). |

---

## Ce que ce protocole n'est pas

- Ce n'est **pas** un frein à toute évolution : c'est un filtre qui distingue une
  amélioration prouvée d'un mirage rétrospectif.
- Ce n'est **pas** un conseil d'investissement : il n'indique aucun achat/vente/
  allocation. Il structure *comment décider*, pas *quoi trader*.
- Ce n'est **pas** figé : le protocole lui-même se révise, par écrit, avec la même
  discipline.

---

*Adopté le 2026-08-29, à la suite de la réflexion stratégique post-preuve-d'edge
(4 lentilles : quant, risque, commercial, red-team). Outils de référence :
`edge_report.py` (single-asset), `run_backtest_portfolio.py` (book),
`run_backtest_band_walkforward.py` (validation OOS d'un paramètre).*
