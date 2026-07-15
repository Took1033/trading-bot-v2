"""
autoclose.py — "close reglable" par bot : take-profit fixe OU trailing, activable.

Etat partage EN MEMOIRE (dashboard et bots tournent dans le meme process asyncio)
+ persistance JSON (survit au redemarrage). OFF par defaut.

⚠️ Le backtest (run_backtest_trailing.py) montre qu'en AUTOMATIQUE PERMANENT, un
take-profit / trailing DEGRADE le rendement d'un trend-follower. C'est donc un
outil PONCTUEL / discretionnaire (« assurer un close a +X% », « securiser une
montee »), pas un reglage a laisser tourner en continu.

Modes :
  - "take_profit" : ferme si le gain depuis l'entree atteint +threshold_pct %
  - "trailing"    : ferme si le prix recule de threshold_pct % depuis le plus
                    haut atteint depuis l'entree
Apres declenchement, le bot RESTE ACTIF (il peut racheter au tick suivant si la
tendance tient) — choix valide le 2026-07-12.
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog

log = structlog.get_logger()

_PATH = Path(__file__).parent.parent / "config" / "autoclose.json"
_state: dict[str, dict] = {}
_loaded = False

DEFAULT = {"active": False, "mode": "trailing", "threshold_pct": 5.0}


def _load() -> None:
    global _loaded
    if _loaded:
        return
    try:
        if _PATH.exists():
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _state.update(data)
    except Exception as exc:
        log.warning("autoclose_load_failed", error=str(exc))
    _loaded = True


def _save() -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_state, indent=2), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception as exc:
        log.warning("autoclose_save_failed", error=str(exc))


def get(bot_id: str) -> dict:
    """Config du bot (copie), DEFAULT si absente."""
    _load()
    return dict(_state.get(bot_id, DEFAULT))


def all_configs() -> dict:
    _load()
    return {k: dict(v) for k, v in _state.items()}


def set_config(bot_id: str, active: bool, mode: str, threshold_pct: float) -> dict:
    """Met a jour et persiste la config d'un bot. Valide/borne les entrees."""
    _load()
    mode = mode if mode in ("take_profit", "trailing") else "trailing"
    try:
        thr = float(threshold_pct)
    except (TypeError, ValueError):
        thr = 5.0
    thr = max(0.5, min(90.0, thr))
    cfg = {"active": bool(active), "mode": mode, "threshold_pct": round(thr, 2)}
    _state[bot_id.lower()] = cfg
    _save()
    log.info("autoclose_set", bot_id=bot_id, **cfg)
    return cfg
