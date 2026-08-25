"""
app_auth.py — authentification de l'appli/dashboard Kairos.

L'appli est servie sur le tailnet (Tailscale HTTPS) et ses endpoints declenchent de
VRAIS ordres (kill switch, cloture, ajout de bot...). Sans auth, tout appareil du
tailnet peut piloter le capital reel. Ce module ajoute une barriere simple et robuste
pour un usage mono-utilisateur auto-heberge :

  - `APP_PASSWORD` (dans .env, gitignore comme les cles API) : le mot de passe.
    NON defini  -> auth DESACTIVEE (retro-compatible, mais on previent bruyamment).
    defini      -> les endpoints de donnees et d'action exigent un jeton valide.
  - Login : POST /app/login {password} -> jeton signe (HMAC-SHA256, cle = le mot de
    passe -> changer le mot de passe invalide les jetons) avec expiration.
  - Le jeton voyage dans l'entete `Authorization: Bearer <jeton>` (jamais dans l'URL).
  - Anti-bruteforce : fenetre glissante globale sur /app/login.

Stateless (aucun store serveur) : le jeton porte son expiration + sa signature.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

import structlog

log = structlog.get_logger()

_SESSION_DAYS   = float(os.getenv("APP_SESSION_DAYS", "30") or "30")
# Anti-bruteforce login : au plus N tentatives par fenetre (toutes IP confondues —
# usage mono-utilisateur, un plafond global suffit et evite les soucis d'IP derriere
# le reverse-proxy Tailscale).
_MAX_ATTEMPTS   = int(os.getenv("APP_LOGIN_MAX_ATTEMPTS", "10") or "10")
_WINDOW_S       = 300.0
_attempts: list[float] = []


def _password() -> str:
    return os.getenv("APP_PASSWORD", "") or ""


def is_enabled() -> bool:
    """True si un mot de passe est configure -> l'auth est appliquee."""
    return bool(_password())


def _key() -> bytes:
    # Cle de signature = mot de passe. Le changer invalide tous les jetons emis.
    return _password().encode("utf-8")


def _sign(msg: str) -> str:
    return hmac.new(_key(), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def check_password(candidate: str) -> bool:
    """Comparaison a temps constant contre APP_PASSWORD."""
    pw = _password()
    if not pw or not isinstance(candidate, str):
        return False
    return hmac.compare_digest(candidate, pw)


def issue_token() -> dict:
    """Emet un jeton signe {token, expires_at} valable APP_SESSION_DAYS jours."""
    exp = int(time.time() + _SESSION_DAYS * 86400)
    # nonce pour que deux jetons emis a la meme seconde different (tracabilite)
    nonce   = base64.urlsafe_b64encode(os.urandom(6)).rstrip(b"=").decode()
    payload = f"{exp}.{nonce}"
    token   = f"{payload}.{_sign(payload)}"
    return {"token": token, "expires_at": exp}


def verify_token(token: str | None) -> bool:
    """Valide la signature ET l'expiration d'un jeton. Comparaison a temps constant."""
    if not is_enabled() or not token or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    exp_s, nonce, sig = parts
    payload = f"{exp_s}.{nonce}"
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        return int(exp_s) > int(time.time())
    except (ValueError, TypeError):
        return False


def bearer_from_request(request) -> str | None:
    """Extrait le jeton de l'entete Authorization: Bearer <jeton>."""
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return None


def login_rate_ok() -> bool:
    """Fenetre glissante anti-bruteforce sur les tentatives de login. Enregistre la
    tentative. True si sous le seuil, False si le seuil est franchi."""
    now = time.time()
    global _attempts
    _attempts = [t for t in _attempts if now - t < _WINDOW_S]
    _attempts.append(now)
    return len(_attempts) <= _MAX_ATTEMPTS
