"""
reconcile_live.py — AXE 3 : le bot fait-il VRAIMENT ce que la strategie dicte ?

Pour chaque bot live, compare l'etat REEL (position ouverte ou flat) a l'etat
ATTENDU par la strategie (prix live vs SMA50 des clotures daily, avec la bande
d'hysteresis de la config). Les ecarts legitimes (filtre de regime BTC baissier,
bot en pause, kill switch) sont EXPLIQUES ; seuls les vrais decalages sont marques
"!!". C'est un snapshot de FIDELITE, pas une mesure de P&L (impossible sur si peu
de trades). A relancer regulierement pour construire une confiance dans le systeme.

Prerequis : le bot tourne, dashboard sur http://localhost:8080.
Usage : python reconcile_live.py
"""
from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import asyncio
import time

import aiohttp

DASH = "http://localhost:8080"


async def jget(s, url, **kw):
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=10), **kw) as r:
        return await r.json()


async def fetch_daily(s, sym: str, n: int) -> list[float]:
    end = int(time.time())
    start = end - n * 86400
    url = f"https://api.exchange.coinbase.com/products/{sym}/candles"
    out, cur = [], end
    while cur > start:
        cs = max(start, cur - 300 * 86400)
        try:
            async with s.get(url, params={"granularity": 86400, "start": cs, "end": cur},
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    break
                d = await r.json()
        except Exception:
            break
        if not d:
            break
        out.extend(d)
        cur = cs
        await asyncio.sleep(0.25)
    out.sort(key=lambda c: c[0])
    return [float(c[4]) for c in out]


def expected_state(px: float, sma: float, entry_b: float, exit_b: float) -> str:
    if sma <= 0:
        return "?"
    if px > sma * (1 + entry_b / 100):
        return "LONG"
    if px < sma * (1 - exit_b / 100):
        return "FLAT"
    return "NEUTRE"


async def main() -> None:
    async with aiohttp.ClientSession(headers={"User-Agent": "reconcile"}) as s:
        try:
            cfg = await jget(s, f"{DASH}/api/config")
            swarm = await jget(s, f"{DASH}/api/swarm")
        except Exception as exc:
            print(f"Dashboard injoignable ({exc}). Le bot tourne-t-il ? (http://localhost:8080)")
            return

        # Boite noire d'execution (Axe 2) : fidelite bot<->broker. Tolerant a une
        # version de dashboard sans /api/health.exec (reconcile reste utilisable).
        exec_state = None
        try:
            _health = await jget(s, f"{DASH}/api/health")
            exec_state = _health.get("exec")
        except Exception:
            pass

        sma_p = int(cfg.get("sma_period", 50))
        exit_b = float(cfg.get("exit_buffer", 0.0))
        entry_b = 0.0   # non expose par /api/config -> defaut historique
        regime_on = bool(cfg.get("regime", False))

        # Regime : BTC est-il au-dessus de sa propre SMA ? (conditionne les entrees alts)
        btc_closes = await fetch_daily(s, "BTC-USD", sma_p + 80)
        btc_sma = sum(btc_closes[-sma_p:]) / sma_p if len(btc_closes) >= sma_p else 0.0
        btc_px = next((b.get("current_price") for b in swarm if b.get("symbol", "").startswith("BTC")), None) \
            or (btc_closes[-1] if btc_closes else 0.0)
        regime_bull = btc_px > btc_sma if btc_sma else True

        print("=" * 78)
        print(f"  RECONCILIATION LIVE vs STRATEGIE  -  SMA{sma_p}, bande sortie {exit_b:.1f}%, "
              f"regime {'ON' if regime_on else 'off'}")
        print(f"  Regime marche : BTC {btc_px:.0f} vs SMA {btc_sma:.0f}  ->  "
              f"{'HAUSSIER (alts autorises)' if regime_bull else 'BAISSIER (entrees alts bloquees)'}")
        print("=" * 78)

        anomalies = []
        for b in swarm:
            sym = b.get("symbol", "?")
            px = b.get("current_price") or 0.0
            closes = await fetch_daily(s, sym.replace("USDC", "USD"), sma_p + 80)
            if len(closes) < sma_p:
                print(f"  ?? {sym:<11} pas assez d'historique ({len(closes)}j)")
                continue
            sma = sum(closes[-sma_p:]) / sma_p
            dist = (px - sma) / sma * 100 if sma > 0 else 0.0
            exp = expected_state(px, sma, entry_b, exit_b)
            pos = b.get("position") or {}
            actual = "LONG" if (pos.get("qty") or 0) > 0 else "FLAT"
            sig = (b.get("signal_streak") or {}).get("action", "?")
            paused = bool(b.get("paused"))
            is_btc = sym.startswith("BTC")

            ok, note = True, ""
            if paused:
                note = "bot en PAUSE (n'agit pas)"
            elif exp == "NEUTRE":
                note = "dans la bande neutre -> garde son etat (conforme)"
            elif exp == actual:
                note = "conforme a la strategie"
            elif exp == "LONG" and actual == "FLAT":
                if regime_on and regime_bull is False and not is_btc:
                    note = "attendu LONG, mais entree bloquee par le FILTRE REGIME (BTC baissier) -> normal"
                else:
                    ok = False
                    note = "ATTENDU LONG mais FLAT -> a investiguer (cap expo ? kill switch ? lag d'entree ?)"
            elif exp == "FLAT" and actual == "LONG":
                ok = False
                note = f"ATTENDU FLAT (prix sous SMA-{exit_b:.0f}%) mais encore LONG -> devrait sortir"

            mark = "OK" if ok else "!!"
            print(f"  {mark} {sym:<11} prix/SMA {dist:>+6.1f}%   signal={sig:<5} "
                  f"attendu={exp:<7} reel={actual:<5}  {note}")
            if not ok:
                anomalies.append(sym)

        print("=" * 78)
        if anomalies:
            print(f"  !! {len(anomalies)} anomalie(s) a investiguer : {', '.join(anomalies)}")
        else:
            print("  Aucune anomalie : chaque bot est dans l'etat que la strategie dicte "
                  "(ecarts expliques inclus).")
        print()

        # ── FIDELITE BROKER (Axe 2) : etat INTERNE du bot vs realite Coinbase ──
        # Complementaire de la fidelite STRATEGIE ci-dessus : ici on regarde si le
        # suivi local du bot colle aux vrais soldes (source des faux fills / double-vente).
        print("=" * 78)
        if not exec_state:
            print("  FIDELITE BROKER : indisponible (dashboard sans boite noire ?).")
        else:
            c   = exec_state.get("counters", {}) or {}
            div = exec_state.get("divergences", 0)
            est = exec_state.get("fills_estimated", 0)
            age = exec_state.get("cycle_age_s")
            print(f"  FIDELITE BROKER (bot <-> Coinbase)  -  depuis {exec_state.get('since', '?')}")
            print(f"    fills={c.get('fill', 0)} (dont estimes {est})   "
                  f"divergences={div}   purges_fantome={c.get('phantom_purge', 0)}   "
                  f"snap_degrades={c.get('snapshot_degraded', 0)}   retries_REST={c.get('rest_retry', 0)}")
            if age is not None:
                print(f"    dernier cycle d'execution il y a {age:.0f}s")
            last_div = exec_state.get("last_divergence")
            if last_div:
                print(f"    !! derniere divergence : {last_div.get('symbol')}  "
                      f"local={last_div.get('local_qty')}  reel={last_div.get('real_qty')}  "
                      f"(delta {last_div.get('delta')})")
            verdict = ("OK — le suivi local reflete la realite Coinbase" if div == 0
                       else f"!! {div} divergence(s) — auditer la synchro du suivi local")
            print(f"    -> {verdict}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
