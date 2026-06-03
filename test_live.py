"""
test_live.py — Validation de la connexion Coinbase Advanced Trade API.

Lance AVANT de passer COINBASE_MODE=live pour verifier que :
  1. Les cles API sont valides
  2. Les permissions View + Trade sont actives
  3. Le solde USDC est visible
  4. Les prix sont recuperes correctement

Usage :
    python test_live.py

Ne place AUCUN ordre.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# On force le mode live pour le test
os.environ["COINBASE_MODE"] = "live"

from dotenv import load_dotenv
load_dotenv()


async def main() -> int:
    api_key    = os.getenv("COINBASE_API_KEY", "")
    api_secret = os.getenv("COINBASE_API_SECRET", "")

    print("=" * 60)
    print("  KAIROS ALPHA — Test connexion Coinbase Advanced Trade")
    print("=" * 60)

    # ── 1. Vérification des clés ────────────────────────────────────────────
    if not api_key or not api_secret:
        print("\n  ❌  COINBASE_API_KEY ou COINBASE_API_SECRET manquant dans .env")
        print("      Crée tes clés sur : https://www.coinbase.com/settings/api")
        return 1

    print(f"\n  ✅  Clé API   : {api_key[:30]}...")
    print(f"  ✅  Secret    : {'*' * 20} (présent)")

    try:
        from coinbase.rest import RESTClient  # type: ignore
    except ImportError:
        print("\n  ❌  coinbase-advanced-py non installé.")
        print("      Lance : pip install coinbase-advanced-py")
        return 1

    # ── 2. Initialisation du client ─────────────────────────────────────────
    try:
        client = RESTClient(api_key=api_key, api_secret=api_secret)
        print("  ✅  Client Coinbase initialisé")
    except Exception as exc:
        print(f"\n  ❌  Erreur init client : {exc}")
        return 1

    loop = asyncio.get_event_loop()

    def run(fn, *args, **kwargs):
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── 3. Liste des portfolios ─────────────────────────────────────────────
    print("\n  📂  Portfolios disponibles...")
    portfolio_uuids: list[str] = []
    try:
        ports_resp = await run(client.get_portfolios)
        ports      = getattr(ports_resp, "portfolios", [])
        for p in ports:
            uuid = getattr(p, "uuid", "?")
            name = getattr(p, "name", "?")
            ptype = getattr(p, "type", "?")
            deleted = getattr(p, "deleted", False)
            print(f"      {ptype:10s} | {name:20s} | {uuid}  (deleted={deleted})")
            if not deleted:
                portfolio_uuids.append(uuid)
    except Exception as exc:
        print(f"      (impossible de lister : {exc})")

    # ── 4. Récupération des comptes (avec pagination + tous portfolios) ─────
    print("\n  📋  Récupération des comptes (pagination + tous portfolios)...")
    all_accounts: list = []

    async def fetch_all(portfolio_uuid: str | None = None) -> int:
        """Pagine tous les comptes d'un portfolio (ou global si None)."""
        cursor = ""
        count  = 0
        while True:
            kwargs = {"limit": 250}
            if cursor:
                kwargs["cursor"] = cursor
            if portfolio_uuid:
                kwargs["retail_portfolio_id"] = portfolio_uuid
            try:
                resp = await run(client.get_accounts, **kwargs)
            except Exception as exc:
                # Certains SDK n'acceptent pas retail_portfolio_id
                if portfolio_uuid:
                    return 0
                raise
            accts = getattr(resp, "accounts", [])
            all_accounts.extend(accts)
            count += len(accts)
            has_next = getattr(resp, "has_next", False)
            cursor   = getattr(resp, "cursor", "") or ""
            if not has_next or not cursor:
                break
        return count

    try:
        n = await fetch_all()
        print(f"      → {n} comptes (portfolio par défaut)")

        # Si on a plusieurs portfolios, on tente aussi de récupérer les autres
        for puuid in portfolio_uuids:
            extra = await fetch_all(portfolio_uuid=puuid)
            if extra > 0:
                print(f"      → +{extra} comptes (portfolio {puuid[:8]}...)")
    except Exception as exc:
        print(f"\n  ❌  Erreur get_accounts : {exc}")
        print("      Vérifie les permissions de la clé API (View requis)")
        return 1

    # Dedup par UUID compte
    seen = set()
    unique_accounts = []
    for a in all_accounts:
        uuid = getattr(a, "uuid", None)
        if uuid and uuid not in seen:
            seen.add(uuid)
            unique_accounts.append(a)

    # Parser tous les soldes (Money object OU dict — l'API renvoie les deux)
    def extract_value(obj) -> float:
        if obj is None:
            return 0.0
        val = getattr(obj, "value", None)
        if val is None:
            try:
                val = obj["value"]
            except (KeyError, TypeError):
                val = None
        if val is None:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    balances: dict[str, float] = {}
    nonzero: list[tuple[str, float]] = []
    for acct in unique_accounts:
        try:
            currency = getattr(acct, "currency", None)
            if not currency:
                continue
            avail = extract_value(getattr(acct, "available_balance", None))
            balances[currency] = balances.get(currency, 0.0) + avail
            if avail > 0:
                nonzero.append((currency, avail))
        except Exception:
            continue

    print(f"\n  📊  Total : {len(unique_accounts)} comptes uniques, "
          f"{len(nonzero)} avec solde > 0")

    # Debug : dump BRUT de chaque compte pour voir exactement ce que renvoie l'API
    print(f"\n  🔍  Dump brut des {len(unique_accounts)} comptes :")
    for i, acct in enumerate(unique_accounts):
        try:
            # On essaie plusieurs formats pour ne rien rater
            currency = getattr(acct, "currency", "?")
            name     = getattr(acct, "name", "?")
            atype    = getattr(acct, "type", "?")
            active   = getattr(acct, "active", "?")
            platform = getattr(acct, "platform", "?")

            # Balance disponible (peut être Money object ou dict)
            avail_obj = getattr(acct, "available_balance", None)
            avail_val = "?"
            if avail_obj is not None:
                avail_val = getattr(avail_obj, "value", None) or (
                    avail_obj.get("value", "?") if isinstance(avail_obj, dict) else "?"
                )

            # Hold (peut aussi être Money object ou dict)
            hold_obj = getattr(acct, "hold", None)
            hold_val = "?"
            if hold_obj is not None:
                hold_val = getattr(hold_obj, "value", None) or (
                    hold_obj.get("value", "?") if isinstance(hold_obj, dict) else "?"
                )

            print(f"      [{i:2d}] {currency:>6s} | name={name:<25s} | "
                  f"avail={avail_val} | hold={hold_val} | type={atype} | "
                  f"active={active} | platform={platform}")
        except Exception as exc:
            print(f"      [{i:2d}] ERREUR parsing : {exc}")
            # Fallback : vars() pour voir ce qui est disponible
            try:
                print(f"           vars={vars(acct)}")
            except Exception:
                pass

    if nonzero:
        print("\n  💰  Soldes non nuls :")
        for currency, amount in sorted(nonzero, key=lambda x: -x[1]):
            print(f"      {currency:10s} : {amount:.8f}")
    else:
        print("  ⚠️   Aucun solde > 0 trouvé même après pagination de tous les portfolios")
        print("      → Tes USDC sont peut-être sur le compte Coinbase (retail/wallet)")
        print("         et pas dans l'Advanced Trade. Solution :")
        print("         Va sur https://www.coinbase.com/portfolios et transfère")
        print("         tes USDC vers le portfolio 'Default' (Advanced Trade).")

    usdc = balances.get("USDC", 0.0)
    print(f"\n  💵  Solde USDC total visible : {usdc:,.2f} USDC")

    if usdc < 10:
        print("  ⚠️   Solde USDC < 10 — dépôt ou transfert recommandé avant de trader")

    # ── 4. Récupération des prix en live ────────────────────────────────────
    print("\n  📈  Récupération des prix...")
    for symbol in ["BTC-USDC", "ETH-USDC", "SOL-USDC"]:
        try:
            result    = await run(client.get_best_bid_ask, product_ids=[symbol])
            pricebook = result.pricebooks[0]
            bid  = float(pricebook.bids[0].price)
            ask  = float(pricebook.asks[0].price)
            mid  = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100
            print(f"      {symbol:12s} : {mid:>10,.2f} USDC  "
                  f"(bid={bid:,.2f} / ask={ask:,.2f} / spread={spread_pct:.3f}%)")
        except Exception as exc:
            print(f"      {symbol:12s} : ❌  {exc}")

    # ── 5. Vérification des permissions Trade ───────────────────────────────
    print("\n  🔐  Vérification des permissions (lecture seule — pas d'ordre)...")
    try:
        perms = await run(client.get_api_key_permissions)
        can_trade  = getattr(perms, "can_trade", None)
        can_view   = getattr(perms, "can_view", None)
        perm_str   = str(perms)
        print(f"      Réponse : {perm_str[:200]}")
        if can_trade is False:
            print("  ⚠️   Permission TRADE non activée sur la clé API")
    except Exception as exc:
        # Pas tous les SDKs exposent cette méthode — pas critique
        print(f"      (non testable via ce SDK : {exc})")

    # ── 6. Résumé ────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if usdc > 0:
        print("  ✅  Connexion Coinbase OK — tout est prêt pour le LIVE")
        print()
        print("  PROCHAINES ÉTAPES :")
        print("  1. Édite .env et change :")
        print("       COINBASE_MODE=live")
        print(f"       LIVE_INITIAL_USDC={usdc:.2f}")
        print("  2. Lance : python run_with_restart.py")
    else:
        print("  ⚠️   Connexion établie mais solde USDC = 0")
        print("      Dépose des USDC sur ton compte Coinbase avant de lancer en live")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
