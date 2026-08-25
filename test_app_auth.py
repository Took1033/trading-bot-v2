"""
test_app_auth.py — tests de l'authentification de l'appli (axe : durcir l'appli).

Deux niveaux :
  [1] logique pure app_auth (mot de passe, jeton signe, expiration, rate-limit)
  [2] INTEGRATION du vrai middleware aiohttp : endpoint protege -> 401 sans jeton,
      login -> jeton, endpoint protege -> 200 avec jeton, jeton altere -> 401,
      route publique -> ouverte.

Lancer : python test_app_auth.py   (exit 0 = tout passe)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Env AVANT import : un mot de passe -> auth ACTIVE.
os.environ["DB_PATH"]     = os.path.join(tempfile.mkdtemp(prefix="kairos_auth_test_"), "trading.db")
os.environ["APP_PASSWORD"] = "s3cret-de-test-long"
os.environ["COINBASE_MODE"] = "paper"

import app_auth

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def test_pure() -> None:
    print("\n[1] app_auth — logique pure")
    check("is_enabled avec APP_PASSWORD", app_auth.is_enabled() is True)
    check("bon mot de passe accepte", app_auth.check_password("s3cret-de-test-long") is True)
    check("mauvais mot de passe refuse", app_auth.check_password("nope") is False)
    check("mot de passe vide refuse", app_auth.check_password("") is False)

    tok = app_auth.issue_token()["token"]
    check("jeton emis valide", app_auth.verify_token(tok) is True)
    check("jeton None invalide", app_auth.verify_token(None) is False)
    check("jeton bidon invalide", app_auth.verify_token("x.y.z") is False)
    check("jeton altere (signature) invalide", app_auth.verify_token(tok + "ff") is False)

    # jeton expire : forge une charge expiree signee -> doit etre refuse (expiration)
    exp = int(time.time()) - 10
    payload = f"{exp}.nonce"
    forged = f"{payload}.{app_auth._sign(payload)}"
    check("jeton expire refuse", app_auth.verify_token(forged) is False)

    # rate limit : au-dela du seuil -> False
    app_auth._attempts.clear()
    allowed = sum(1 for _ in range(app_auth._MAX_ATTEMPTS + 3) if app_auth.login_rate_ok())
    check("rate-limit login plafonne", allowed == app_auth._MAX_ATTEMPTS)

    # auth desactivee si pas de mot de passe
    os.environ["APP_PASSWORD"] = ""
    check("is_enabled False sans mot de passe", app_auth.is_enabled() is False)
    check("verify_token False si auth desactivee", app_auth.verify_token(tok) is False)
    os.environ["APP_PASSWORD"] = "s3cret-de-test-long"


async def _integration() -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from interfaces import dashboard

    app_auth._attempts.clear()   # repart d'un compteur anti-bruteforce vide
    app = web.Application(middlewares=[dashboard.auth_middleware])
    async def protected(_): return web.json_response({"ok": True})
    async def shell(_):     return web.Response(text="shell")
    app.router.add_get("/api/portfolio", protected)
    app.router.add_get("/app", shell)
    app.router.add_post("/app/login", dashboard.handle_login)

    async with TestClient(TestServer(app)) as c:
        r = await c.get("/app");                       check("route publique /app ouverte", r.status == 200)
        r = await c.get("/api/portfolio");             check("protege sans jeton -> 401", r.status == 401)
        r = await c.post("/app/login", json={"password": "faux"}); check("login mauvais mdp -> 401", r.status == 401)
        r = await c.post("/app/login", json={"password": "s3cret-de-test-long"})
        j = await r.json(); token = j.get("token")
        check("login bon mdp -> jeton", r.status == 200 and bool(token))
        r = await c.get("/api/portfolio", headers={"Authorization": "Bearer " + token})
        check("protege avec jeton -> 200", r.status == 200)
        r = await c.get("/api/portfolio", headers={"Authorization": "Bearer " + token + "zz"})
        check("protege jeton altere -> 401", r.status == 401)


def test_integration() -> None:
    print("\n[2] middleware aiohttp — barriere reelle")
    try:
        asyncio.run(_integration())
    except Exception as exc:
        check(f"integration sans exception ({exc})", False)


if __name__ == "__main__":
    print("=== App auth — tests ===")
    test_pure()
    test_integration()
    print(f"\n{'=' * 42}")
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"   - {f}")
        raise SystemExit(1)
    print("  Tous les tests passent.")
    raise SystemExit(0)
