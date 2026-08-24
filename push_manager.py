"""
push_manager.py — notifications push web (Web Push / VAPID) vers l'appli mobile.

- Genere + persiste une paire de clefs VAPID au 1er run (data/vapid.json) : aucune
  config .env manuelle.
- Stocke les abonnements push du navigateur (data/push_subs.json).
- send(title, body) : envoie a tous les abonnes. JAMAIS d'exception (le trading ne
  doit pas crasher si un push echoue). Purge les abonnes expires (404/410).

Necessite pywebpush (cf requirements.txt). Requiert HTTPS cote client (Tailscale Serve).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import structlog

log = structlog.get_logger()

_DATA       = Path(os.getenv("DB_PATH", "memory/trading.db")).parent
_VAPID_FILE = _DATA / "vapid.json"
_SUBS_FILE  = _DATA / "push_subs.json"
_SUBJECT    = os.getenv("PUSH_VAPID_SUBJECT", "mailto:brice.cuny@gmail.com")

_vapid_cache: dict | None = None


def _ensure_vapid() -> dict:
    """Charge, ou genere+persiste, la paire VAPID -> {private_pem, public_key}."""
    global _vapid_cache
    if _vapid_cache:
        return _vapid_cache
    if _VAPID_FILE.exists():
        try:
            _vapid_cache = json.loads(_VAPID_FILE.read_text(encoding="utf-8"))
            return _vapid_cache
        except Exception:
            pass
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    v = Vapid02()
    v.generate_keys()
    pub = v.public_key.public_bytes(serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint)
    pub_b64 = base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
    pem = v.private_pem()
    _vapid_cache = {"private_pem": pem.decode() if isinstance(pem, bytes) else pem,
                    "public_key": pub_b64}
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        _VAPID_FILE.write_text(json.dumps(_vapid_cache), encoding="utf-8")
        log.info("vapid_generated", file=str(_VAPID_FILE))
    except Exception as exc:
        log.warning("vapid_persist_failed", error=str(exc))
    return _vapid_cache


def public_key() -> str:
    """Clef publique (applicationServerKey) a passer a PushManager.subscribe."""
    return _ensure_vapid()["public_key"]


def _load_subs() -> list[dict]:
    if _SUBS_FILE.exists():
        try:
            return json.loads(_SUBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_subs(subs: list[dict]) -> None:
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        _SUBS_FILE.write_text(json.dumps(subs), encoding="utf-8")
    except Exception as exc:
        log.warning("push_subs_save_failed", error=str(exc))


def add_subscription(sub: dict) -> bool:
    """Ajoute un abonnement (dedup sur l'endpoint). True si nouveau."""
    if not isinstance(sub, dict) or not sub.get("endpoint"):
        return False
    subs = _load_subs()
    if any(s.get("endpoint") == sub["endpoint"] for s in subs):
        return False
    subs.append(sub)
    _save_subs(subs)
    log.info("push_subscription_added", n=len(subs))
    return True


def has_subscribers() -> bool:
    return bool(_load_subs())


def send(title: str, body: str = "", url: str = "/app") -> int:
    """Envoie une notif push a tous les abonnes. Renvoie le nb de succes.
    Jamais d'exception. Purge les abonnes expires (404/410). Synchrone (a lancer
    dans un thread depuis l'event loop asyncio)."""
    subs = _load_subs()
    if not subs:
        return 0
    try:
        from py_vapid import Vapid02
        from pywebpush import WebPushException, webpush
    except Exception as exc:
        log.warning("pywebpush_indisponible", error=str(exc))
        return 0
    vap = _ensure_vapid()
    vapid_key = Vapid02.from_pem(vap["private_pem"].encode())
    payload = json.dumps({"title": title, "body": body, "url": url})
    ok, dead = 0, []
    for s in subs:
        try:
            webpush(subscription_info=s, data=payload, vapid_private_key=vapid_key,
                    vapid_claims={"sub": _SUBJECT}, timeout=8)
            ok += 1
        except WebPushException as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):
                dead.append(s.get("endpoint"))
            else:
                log.warning("push_send_failed", status=code, error=str(exc)[:120])
        except Exception as exc:
            log.warning("push_send_error", error=str(exc)[:120])
    if dead:
        _save_subs([s for s in subs if s.get("endpoint") not in dead])
        log.info("push_subs_pruned", removed=len(dead))
    return ok
