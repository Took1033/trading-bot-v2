"""
Etat global partage entre orchestrateurs et bot Telegram.

Supporte la pause individuelle par bot_id + une pause globale (kill switch).
Pas de dependances externes - module pur Python.
"""
from __future__ import annotations

_paused: dict[str, bool] = {}
_kill_switch: bool       = False
_kill_reason: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Pause individuelle (par bot_id)
# ─────────────────────────────────────────────────────────────────────────────

def is_paused(bot_id: str = "main") -> bool:
    """True si le bot specifie est en pause OU si le kill switch est actif."""
    return _kill_switch or _paused.get(bot_id, False)


def pause(bot_id: str = "main") -> None:
    """Met un bot specifique en pause."""
    _paused[bot_id] = True


def resume(bot_id: str = "main") -> None:
    """Reprend un bot specifique."""
    _paused[bot_id] = False


# ─────────────────────────────────────────────────────────────────────────────
# Kill switch global (Director Agent)
# ─────────────────────────────────────────────────────────────────────────────

def kill_switch(reason: str) -> None:
    """Active le kill switch global : tous les bots sont mis en pause."""
    global _kill_switch, _kill_reason
    _kill_switch = True
    _kill_reason = reason


def release_kill_switch() -> None:
    """Desactive le kill switch global."""
    global _kill_switch, _kill_reason
    _kill_switch = False
    _kill_reason = None


def is_kill_switch_active() -> bool:
    return _kill_switch


def get_kill_reason() -> str | None:
    return _kill_reason


def get_all_paused() -> dict[str, bool]:
    """Retourne l'etat de pause de tous les bots."""
    return dict(_paused)
