# Reprise — ergonomie dashboard (à finir dans CE dossier)

> Note de passation écrite depuis le QG `pilotage/`. À lire en premier en rouvrant `bots/`.
> Checkpoint : **2026-07-15**.

## ⚙️ Bot : comment il tourne maintenant (MAJ 15/07 ~19h)
- Le bot tourne via une **tâche planifiée Windows `KairosBot`** (créée avec l'accord de Brice).
  Il est **détaché de Claude** : survit à la fermeture/redémarrage de Claude Desktop, et
  **redémarre seul au logon Windows**. Fini les « butos de session ».
- Lanceur : `pythonw run_with_restart.py` (auto-restart si crash). Instance unique garantie
  (`MultipleInstances=IgnoreNew` + garde conflit Telegram interne à `main.py`).
- **⚠️ PIÈGE À CONNAÎTRE** : `Get-Process pythonw` affiche **4 process pour UN SEUL bot**
  (2 shims du venv + wrapper + `main.py`). **NE PAS les tuer en croyant qu'il y a plusieurs bots.**
  Vrai test « 1 seul bot » = **un seul `director_initial_capital`** dans le log **+** un seul
  process sur le **port 8080**.
- Gérer le bot (PowerShell) :
  - État : http://localhost:8080  ou  `Get-ScheduledTaskInfo -TaskName KairosBot`
  - Arrêter : `Stop-ScheduledTask -TaskName KairosBot`
  - (Re)démarrer : `Start-ScheduledTask -TaskName KairosBot`
  - Désinstaller la tâche : `Unregister-ScheduledTask -TaskName KairosBot -Confirm:$false`
- Capital au 15/07 ~19h : ~209,5 USDC (mode **live**).

## Où on en est
- Tout le working tree a été **figé par un commit checkpoint** : `9d113fc`
  (« chore: checkpoint 1 mois de travail non commite »).
  Branche filet de secours : **`save/checkpoint-2026-07-15`**.
- **Cause racine des pertes** : rien n'était committé depuis le 18/06 (`6ca3443`).
  ~1 mois de travail vivait uniquement dans le working tree, sans point de restauration
  → une session qui réécrit `dashboard.py` écrasait le rendu précédent sans trace.
  C'est ça qui faisait « on est revenu comme avant » sur les couleurs.

## Le sujet à finir
Fichier : **`interfaces/dashboard.py`** (~108 Ko).

1. **Rendu couleur (le « bof »)** : la palette actuellement dans le fichier est terne —
   body `#79828f` (gris-bleu), cartes `#a8b0bb`. Il reste aussi des restes de thème
   sombre (`#0d121a`, `#88b8ff`) → superposition de deux directions.
   → **Trancher UNE direction** (sombre propre OU clair contrasté/chaleureux), pas empiler.
2. **Indicateurs de tendance** : chantier en cours sur le dashboard — confirmer avec Brice
   ce qui reste à afficher / brancher.

## Garde-fou anti « session butée » (à appliquer ici)
- [ ] **Committer à la fin de CHAQUE session** (au minimum un checkpoint). Non négociable.
- [ ] Ajouter `memory/.watchdog_state.json` au `.gitignore` (état runtime, bruit inutile).
- [ ] Nettoyer les worktrees orphelins verrouillés dans `.git/worktrees/`
      (`lucid-grothendieck-f05894`, `quizzical-stonebraker-19aeeb` — `git worktree prune`
      une fois les process/OneDrive libérés).
- [ ] Le dossier est dans **OneDrive** → une resync peut ramener une version antérieure
      d'un fichier ouvert. Le commit protège ; un `git push` (34 commits d'avance non poussés)
      donnerait une sauvegarde hors-machine.

## Rappel stratégie (QG pilotage)
Kairos est **parqué / pause de dev** (zéro feature). L'ergo est un confort ponctuel,
pas la priorité — la piste principale reste la **boutique**. À garder en tête sur le temps investi.
