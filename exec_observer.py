"""
exec_observer.py — boîte noire d'exécution (Axe 2 : observabilité).

Kairos trade en LIVE. `_live_snapshot` / `_live_order` détectent et corrigent
DÉJÀ, en silence, les écarts entre l'état interne du bot et la réalité Coinbase
(positions fantômes purgées, fills estimés faute de fill réel, snapshots dégradés,
429). Ce module rend ces événements VISIBLES et AUDITABLES sans changer aucune
décision de trading.

Deux surfaces alimentées par la même source :
  - un résumé en mémoire  -> exposé par /api/health (badge de fidélité)
  - un journal persistant -> logs/exec_journal.jsonl (audit a posteriori)

CONTRAT DE SÛRETÉ : rien ici ne doit jamais casser le chemin critique. Chaque
enregistrement est encapsulé dans un try/except qui avale tout. Une panne
d'observabilité est un angle mort, jamais un ordre manqué.
"""
from __future__ import annotations

import functools
import json
import os
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_MAX_EVENTS = 200          # ring buffer en mémoire (derniers évènements)
_ROTATE_BYTES = 5_000_000  # rotation simple du JSONL au-delà de ~5 Mo


def _safe(fn: Callable[..., None]) -> Callable[..., None]:
    """Contrat de sûreté : une fonction d'enregistrement ne propage JAMAIS d'exception,
    même sur une entrée malformée. Un angle mort d'observabilité, jamais un ordre manqué."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            pass
    return wrapper


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Observer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
        self._last: dict[str, dict[str, Any]] = {}
        self._last_cycle: datetime | None = None
        self._started = _now()

    # ── résolution paresseuse du chemin du journal (aucun effet de bord au import) ──
    def _journal_path(self) -> Path:
        env = os.getenv("EXEC_JOURNAL_PATH")
        if env:
            return Path(env)
        return Path("logs") / "exec_journal.jsonl"

    def _write(self, ev: dict[str, Any]) -> None:
        path = self._journal_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Rotation naïve : on garde un seul .1 pour borner l'espace disque.
            try:
                if path.exists() and path.stat().st_size > _ROTATE_BYTES:
                    path.replace(path.with_suffix(path.suffix + ".1"))
            except Exception:
                pass
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # jamais bloquant

    def emit(self, kind: str, **fields: Any) -> None:
        try:
            ev = {"ts": _now().isoformat(), "kind": kind, **fields}
            with self._lock:
                self._counters[kind] += 1
                self._events.append(ev)
                self._last[kind] = ev
            self._write(ev)
        except Exception:
            pass  # contrat de sûreté : l'observabilité ne casse jamais l'exécution

    def mark_cycle(self, source: str | None = None) -> None:
        try:
            with self._lock:
                self._last_cycle = _now()
                self._counters["cycle"] += 1
            self._write({"ts": _now().isoformat(), "kind": "cycle", "source": source})
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        """Résumé léger et sérialisable pour /api/health. Lecture seule."""
        try:
            with self._lock:
                cycle_age = None
                if self._last_cycle is not None:
                    cycle_age = round((_now() - self._last_cycle).total_seconds(), 1)
                return {
                    "counters": dict(self._counters),
                    "last_fill": self._last.get("fill"),
                    "last_divergence": self._last.get("divergence"),
                    "last_phantom_purge": self._last.get("phantom_purge"),
                    "last_cycle_ts": self._last_cycle.isoformat() if self._last_cycle else None,
                    "cycle_age_s": cycle_age,
                    "fills_estimated": self._counters.get("fill_estimated", 0),
                    "divergences": self._counters.get("divergence", 0),
                    "rest_retries": self._counters.get("rest_retry", 0),
                    "since": self._started.isoformat(),
                }
        except Exception:
            return {}

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        try:
            with self._lock:
                items = list(self._events)
            return items[-n:]
        except Exception:
            return []

    def reset(self) -> None:
        """Réinitialise l'état en mémoire (tests uniquement)."""
        with self._lock:
            self._counters.clear()
            self._events.clear()
            self._last.clear()
            self._last_cycle = None
            self._started = _now()


# Singleton process-wide (tous les CoinbaseClient partagent la même boîte noire).
_OBS = _Observer()


# ── API publique (délègue au singleton, tolérante aux entrées malformées) ─────

@_safe
def record_fill(symbol: str, side: str, qty: float, price: float, *,
                estimated: bool, order_id: str = "", status: str = "filled") -> None:
    """Un ordre a été exécuté. `estimated=True` => fill RÉEL non lu (repli sur l'estimé)."""
    _OBS.emit("fill", symbol=symbol, side=side, qty=qty, price=price,
              estimated=bool(estimated), order_id=order_id, status=status)
    if estimated:
        _OBS.emit("fill_estimated", symbol=symbol, side=side, order_id=order_id)
    if status == "partial":
        _OBS.emit("fill_partial", symbol=symbol, side=side, qty=qty, order_id=order_id)


@_safe
def record_divergence(symbol: str, local_qty: float, real_qty: float) -> None:
    """L'état interne du bot diffère de la réalité Coinbase (Coinbase fait foi)."""
    _OBS.emit("divergence", symbol=symbol,
              local_qty=local_qty, real_qty=real_qty,
              delta=(real_qty - local_qty))


@_safe
def record_phantom_purge(symbol: str) -> None:
    """Position purgée après 2 lectures nulles consécutives (solde réel tombé à 0)."""
    _OBS.emit("phantom_purge", symbol=symbol)


@_safe
def record_snapshot_degraded(n_balances: int | None = None) -> None:
    """Lecture de comptes clairement incomplète : repli sur le dernier snapshot sain."""
    _OBS.emit("snapshot_degraded", n_balances=n_balances)


@_safe
def record_retry(reason: str = "", attempt: int | None = None) -> None:
    """Un appel REST a été retenté (429 / 5xx / réseau transitoire)."""
    _OBS.emit("rest_retry", reason=str(reason)[:120], attempt=attempt)


@_safe
def mark_cycle(source: str | None = None) -> None:
    """Battement de cœur : un cycle d'exécution vient de s'exécuter."""
    _OBS.mark_cycle(source)


def snapshot() -> dict[str, Any]:
    return _OBS.snapshot()


def recent(n: int = 50) -> list[dict[str, Any]]:
    return _OBS.recent(n)


def reset() -> None:
    _OBS.reset()
