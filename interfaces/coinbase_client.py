"""
CoinbaseClient — client unifie paper / live.

Paper mode  : simule les ordres localement, prix reels via API publique.
Live mode   : utilise coinbase-advanced-py (cles CDP requises dans .env).
              Toutes les methodes async utilisent run_in_executor pour
              ne pas bloquer la boucle asyncio.

Cles requises dans .env pour le live :
    COINBASE_API_KEY    ex. "organizations/xxx/apiKeys/yyy"  (ou simple key name)
    COINBASE_API_SECRET ex. "-----BEGIN EC PRIVATE KEY-----\\n...\\n-----END..."
    COINBASE_MODE       "live"
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import structlog
from dotenv import load_dotenv

load_dotenv()

log = structlog.get_logger()

MODE: Literal["paper", "live"] = os.getenv("COINBASE_MODE", "paper")  # type: ignore

# Taille minimum de trade en USDC (Coinbase rejette les ordres < 1 USDC)
MIN_ORDER_USDC = float(os.getenv("MIN_ORDER_USDC", "1.0"))

# Frais round-trip Coinbase (taker x2). Le P&L par position est net de ces frais.
ROUND_TRIP_FEE_PCT = 2 * float(os.getenv("COINBASE_TAKER_FEE_PCT", "0.006"))

# Valeur min (USDC) d'une position pour qu'elle soit suivie par le bot en live.
# Les "dust" en dessous sont ignorees (pas vendues, pas comptabilisees).
LIVE_MIN_TRACK_USDC = float(os.getenv("LIVE_MIN_TRACK_USDC", "5.0"))

# Spread max accepte avant ordre (en %). Si bid/ask trop large = marche illiquide.
MAX_SPREAD_PCT = float(os.getenv("LIVE_MAX_SPREAD_PCT", "0.15"))


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PaperPortfolio:
    """Portefeuille simule pour le paper trading."""
    usdc_balance: float = 10_000.0
    positions: dict[str, dict] = field(default_factory=dict)

    def value(self, prices: dict[str, float]) -> float:
        total = self.usdc_balance
        for symbol, pos in self.positions.items():
            price = prices.get(symbol, pos["avg_price"])
            total += pos["qty"] * price
        return total

    def pnl_pct(self, prices: dict[str, float], initial: float = 10_000.0) -> float:
        return (self.value(prices) - initial) / initial * 100


@dataclass
class LivePortfolio:
    """
    Suivi local des positions en mode live.
    La quantite reelle vient de Coinbase (account balance).
    Le prix moyen d'entree est suivi localement (Coinbase ne le fournit pas).
    """
    positions: dict[str, dict] = field(default_factory=dict)  # symbol -> {qty, avg_price}

    def update_buy(self, symbol: str, qty: float, price: float) -> None:
        pos     = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
        new_qty = pos["qty"] + qty
        if new_qty > 0:
            pos["avg_price"] = (pos["qty"] * pos["avg_price"] + qty * price) / new_qty
        pos["qty"] = new_qty
        self.positions[symbol] = pos

    def update_sell(self, symbol: str, qty: float) -> None:
        pos = self.positions.get(symbol)
        if pos:
            pos["qty"] = max(0.0, pos["qty"] - qty)
            if pos["qty"] < 1e-8:
                del self.positions[symbol]

    def set_from_balance(self, symbol: str, qty: float, avg_price: float = 0.0) -> None:
        """Initialise depuis le vrai solde Coinbase (au demarrage)."""
        if qty > 1e-8:
            self.positions[symbol] = {"qty": qty, "avg_price": avg_price}
        elif symbol in self.positions:
            del self.positions[symbol]


def _extract_balance(obj) -> float:
    """Extrait un montant depuis un Money / dict / objet SDK (robuste)."""
    if obj is None:
        return 0.0
    # Attribut .value (objet)
    val = getattr(obj, "value", None)
    # Fallback : indexation type dict
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


@dataclass
class Order:
    symbol:    str
    side:      Literal["buy", "sell"]
    qty:       float
    price:     float
    timestamp: str  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    order_id:  str  = ""
    status:    str  = "filled"


# ─────────────────────────────────────────────────────────────────────────────
# Client principal
# ─────────────────────────────────────────────────────────────────────────────

class CoinbaseClient:
    """
    Client Coinbase avec switch paper/live.

    Paper : ordres simules, prix live sans auth.
    Live  : ordres reels via coinbase-advanced-py SDK (appels dans run_in_executor).
    """

    def __init__(self) -> None:
        self.mode        = MODE
        self._paper      = PaperPortfolio()
        self._live_port  = LivePortfolio()    # suivi positions live
        self._real_client = None
        self._session    = None               # session aiohttp persistante
        self._loop       = None               # boucle asyncio courante

        if self.mode == "live":
            self._init_live()
        else:
            log.info("coinbase_client_init", mode="paper", balance=self._paper.usdc_balance)

    # ── Init live ────────────────────────────────────────────────────────────

    def _init_live(self) -> None:
        """Initialise le client Coinbase Advanced Trade."""
        api_key    = os.getenv("COINBASE_API_KEY", "")
        api_secret = os.getenv("COINBASE_API_SECRET", "")

        if not api_key or not api_secret:
            raise RuntimeError(
                "COINBASE_API_KEY et COINBASE_API_SECRET requis dans .env pour le mode live.\n"
                "Cree tes cles sur : https://www.coinbase.com/settings/api\n"
                "Permissions requises : View + Trade (jamais Transfer)"
            )

        try:
            from coinbase.rest import RESTClient  # type: ignore
            self._real_client = RESTClient(
                api_key=api_key,
                api_secret=api_secret,
            )
            log.info("coinbase_client_init", mode="live", key_prefix=api_key[:20] + "...")
        except ImportError:
            raise RuntimeError(
                "coinbase-advanced-py non installe.\n"
                "Lance : pip install coinbase-advanced-py"
            )

    # ── Session HTTP persistante (paper mode) ────────────────────────────────

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(limit=10),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── Helper : run sync SDK dans executor ─────────────────────────────────

    async def _run_sync(self, fn, *args, **kwargs):
        """
        Execute une fonction synchrone du SDK dans un thread pool,
        avec retry exponentiel (1s, 2s, 4s) sur erreurs reseau / 5xx.
        Ne retry PAS sur les erreurs metier (400, validation, balance...).
        """
        loop = asyncio.get_event_loop()
        last_exc = None
        for attempt in range(3):
            try:
                return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
            except Exception as exc:
                msg = str(exc).lower()
                # Retry uniquement sur erreurs transitoires
                retryable = any(s in msg for s in (
                    "timeout", "connection", "503", "502", "504", "521", "522",
                    "temporarily unavailable", "rate limit", "429",
                ))
                if not retryable or attempt == 2:
                    raise
                last_exc = exc
                wait = 2 ** attempt   # 1s, 2s, 4s
                log.warning("coinbase_retry",
                            attempt=attempt + 1, wait_s=wait,
                            error=str(exc)[:100])
                await asyncio.sleep(wait)
        raise last_exc or RuntimeError("All retries exhausted")

    # ─────────────────────────────────────────────────────────────────────────
    # Prix
    # ─────────────────────────────────────────────────────────────────────────

    async def get_price(self, symbol: str = "BTC-USDC") -> float:
        if self.mode == "live":
            return await self._live_price(symbol)
        return await self._paper_price(symbol)

    async def _paper_price(self, symbol: str) -> float:
        """Prix reel via API publique Coinbase (sans auth)."""
        url     = f"https://api.coinbase.com/v2/prices/{symbol}/spot"
        session = await self._get_session()
        async with session.get(url) as resp:
            data  = await resp.json()
            price = float(data["data"]["amount"])
            log.debug("paper_price_fetched", symbol=symbol, price=price)
            return price

    async def _live_price(self, symbol: str) -> float:
        """Prix via Advanced Trade API (mid bid/ask), appel async-safe."""
        try:
            result = await self._run_sync(
                self._real_client.get_best_bid_ask,
                product_ids=[symbol],
            )
            pricebook = result.pricebooks[0]
            bid = float(pricebook.bids[0].price)
            ask = float(pricebook.asks[0].price)
            mid = (bid + ask) / 2
            log.debug("live_price_fetched", symbol=symbol, bid=bid, ask=ask, mid=mid)
            return mid
        except Exception as exc:
            log.warning("live_price_fallback", symbol=symbol, error=str(exc))
            # Fallback sur l'API publique si le SDK echoue
            return await self._paper_price(symbol)

    # ─────────────────────────────────────────────────────────────────────────
    # Ordres
    # ─────────────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side:   Literal["buy", "sell"],
        qty:    float,
        force:  bool = False,
    ) -> Order:
        """
        Place un ordre. `force=True` pour les sorties de protection (stop-loss,
        trailing, take-profit) : le garde anti-spread ne bloque PAS l'execution,
        car proteger la position prime sur l'illiquidite ponctuelle.
        """
        if self.mode == "live":
            return await self._live_order(symbol, side, qty, force=force)
        return await self._paper_order(symbol, side, qty)

    async def _paper_order(self, symbol: str, side: Literal["buy", "sell"], qty: float) -> Order:
        price = await self.get_price(symbol)
        order = Order(symbol=symbol, side=side, qty=qty, price=price)

        if side == "buy":
            cost = qty * price
            if cost > self._paper.usdc_balance:
                raise ValueError(
                    f"Solde insuffisant : {self._paper.usdc_balance:.2f} USDC < {cost:.2f}"
                )
            self._paper.usdc_balance -= cost
            pos     = self._paper.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
            new_qty = pos["qty"] + qty
            pos["avg_price"] = (pos["qty"] * pos["avg_price"] + qty * price) / new_qty
            pos["qty"]       = new_qty
            self._paper.positions[symbol] = pos

        elif side == "sell":
            pos = self._paper.positions.get(symbol)
            if not pos or pos["qty"] < qty:
                raise ValueError(f"Position insuffisante pour vendre {qty} {symbol}")
            pos["qty"] -= qty
            self._paper.usdc_balance += qty * price
            if pos["qty"] < 1e-8:
                del self._paper.positions[symbol]

        log.info("paper_order_filled", symbol=symbol, side=side,
                 qty=round(qty, 6), price=round(price, 2))
        return order

    async def _live_order(self, symbol: str, side: Literal["buy", "sell"], qty: float,
                          force: bool = False) -> Order:
        """Place un ordre market reel via Coinbase Advanced Trade API."""
        # ── Order book check : refus si spread trop large (marche illiquide) ──
        # `force=True` (stop-loss/trailing/TP) : on n'annule jamais une sortie de
        # protection a cause du spread, on logue seulement un avertissement.
        try:
            bb = await self._run_sync(self._real_client.get_best_bid_ask, product_ids=[symbol])
            pb = bb.pricebooks[0]
            bid = float(pb.bids[0].price)
            ask = float(pb.asks[0].price)
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100 if mid > 0 else 0
            if spread_pct > MAX_SPREAD_PCT:
                if not force:
                    raise ValueError(
                        f"Spread trop large : {spread_pct:.3f}% > max {MAX_SPREAD_PCT}% "
                        f"(bid={bid:.2f}, ask={ask:.2f})"
                    )
                log.warning("forced_sell_wide_spread", symbol=symbol,
                            spread_pct=round(spread_pct, 3), bid=bid, ask=ask)
            price = mid
        except ValueError:
            raise
        except Exception as exc:
            log.warning("order_book_check_failed", symbol=symbol, error=str(exc))
            price = await self.get_price(symbol)

        client_order_id = str(uuid.uuid4())

        if side == "buy":
            # On achete en USDC (quote_size), arrondi a 2 decimales
            usdc_amount = round(qty * price, 2)
            if usdc_amount < MIN_ORDER_USDC:
                raise ValueError(
                    f"Ordre trop petit : {usdc_amount:.2f} USDC < minimum {MIN_ORDER_USDC} USDC"
                )
            order_config = {
                "market_market_ioc": {
                    "quote_size": str(usdc_amount)
                }
            }
            log.info("live_order_buy", symbol=symbol, usdc=usdc_amount, price=price)

        else:  # sell
            # On vend en base currency (base_size = quantite crypto), arrondi a 8 decimales
            base_size = round(qty, 8)
            order_config = {
                "market_market_ioc": {
                    "base_size": str(base_size)
                }
            }
            log.info("live_order_sell", symbol=symbol, qty=base_size, price=price)

        # Execution de l'ordre (SDK synchrone → executor)
        result = await self._run_sync(
            self._real_client.create_order,
            client_order_id=client_order_id,
            product_id=symbol,
            side=side.upper(),
            order_configuration=order_config,
        )

        # Verification du resultat
        if not result.success:
            err = getattr(result, "error_response", {})
            raise RuntimeError(
                f"Ordre {side} {symbol} rejete par Coinbase : {err}"
            )

        order_id = result.order_id if hasattr(result, "order_id") else client_order_id

        # Mise a jour du suivi local des positions
        if side == "buy":
            # Recalcul de la vraie quantite achetee (Coinbase peut ajuster)
            real_qty = usdc_amount / price
            self._live_port.update_buy(symbol, real_qty, price)
        else:
            self._live_port.update_sell(symbol, qty)

        log.info("live_order_filled",
                 symbol=symbol, side=side,
                 qty=round(qty, 8), price=round(price, 2),
                 order_id=order_id)

        return Order(
            symbol=symbol, side=side, qty=qty,
            price=price, order_id=order_id, status="filled",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Portefeuille
    # ─────────────────────────────────────────────────────────────────────────

    def get_position(self, symbol: str) -> dict | None:
        """Retourne la position ouverte pour un symbole, ou None."""
        if self.mode == "live":
            pos = self._live_port.positions.get(symbol)
        else:
            pos = self._paper.positions.get(symbol)
        return dict(pos) if pos and pos.get("qty", 0) > 1e-8 else None

    async def get_portfolio_snapshot(self) -> dict:
        if self.mode == "live":
            return await self._live_snapshot()
        return await self._paper_snapshot()

    async def _paper_snapshot(self) -> dict:
        prices: dict[str, float] = {}
        for symbol in list(self._paper.positions.keys()):
            prices[symbol] = await self.get_price(symbol)

        return {
            "total_usdc":   self._paper.value(prices),
            "usdc_balance": self._paper.usdc_balance,
            "positions": {
                s: {
                    "qty":           p["qty"],
                    "avg_price":     p["avg_price"],
                    "current_price": prices.get(s, p["avg_price"]),
                    "pnl_usdc":      p["qty"] * (prices.get(s, p["avg_price"]) - p["avg_price"]),
                }
                for s, p in self._paper.positions.items()
            },
            "pnl_pct":  self._paper.pnl_pct(prices),
            "mode":     self.mode,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def _live_snapshot(self) -> dict:
        """
        Snapshot reel : solde USDC + positions depuis Coinbase.
        Synchronise aussi le suivi local avec les vrais soldes.
        """
        try:
            accounts_resp = await self._run_sync(self._real_client.get_accounts)
            accounts      = accounts_resp.accounts
        except Exception as exc:
            log.error("live_snapshot_failed", error=str(exc))
            # Fallback : retourne les donnees locales
            return self._live_snapshot_local()

        # Parser les soldes par currency (parsing robuste : Money obj OU dict)
        balances: dict[str, float] = {}
        for acct in accounts:
            try:
                currency = getattr(acct, "currency", None)
                if not currency:
                    continue
                avail = _extract_balance(getattr(acct, "available_balance", None))
                if avail > 1e-8:
                    balances[currency] = balances.get(currency, 0.0) + avail
            except Exception as exc:
                log.debug("acct_parse_skip", error=str(exc))
                continue

        usdc_balance = balances.get("USDC", 0.0)

        # Construire les positions depuis les soldes reels
        positions_out: dict[str, dict] = {}
        total_usdc = usdc_balance

        for symbol, local_pos in list(self._live_port.positions.items()):
            base_currency = symbol.split("-")[0]   # "BTC" depuis "BTC-USDC"
            real_qty      = balances.get(base_currency, 0.0)

            if real_qty < 1e-8:
                # Position fermee cote Coinbase — nettoyage local
                if symbol in self._live_port.positions:
                    del self._live_port.positions[symbol]
                continue

            # Sync quantite reelle (Coinbase fait foi)
            local_pos["qty"] = real_qty
            current_price    = await self.get_price(symbol)
            value_usdc       = real_qty * current_price
            cost_usdc        = real_qty * local_pos.get("avg_price", current_price)
            # Net de l'aller-retour : ce que tu garderais si tu liquidais maintenant.
            pnl_usdc         = value_usdc - cost_usdc - ROUND_TRIP_FEE_PCT * cost_usdc
            total_usdc       += value_usdc

            positions_out[symbol] = {
                "qty":           real_qty,
                "avg_price":     local_pos.get("avg_price", current_price),
                "current_price": current_price,
                "pnl_usdc":      pnl_usdc,
            }

        # Aussi recuperer les positions de crypto non suivies localement
        # FILTRE : on ignore les dust positions < LIVE_MIN_TRACK_USDC
        ignored_dust: list[tuple[str, float]] = []
        for currency, qty in balances.items():
            if currency == "USDC":
                continue
            symbol = f"{currency}-USDC"
            if symbol not in positions_out and qty > 1e-8:
                try:
                    current_price = await self.get_price(symbol)
                    value_usdc    = qty * current_price

                    # Dust : on calcule la valeur mais on n'ajoute PAS aux positions trackees
                    if value_usdc < LIVE_MIN_TRACK_USDC:
                        total_usdc += value_usdc
                        ignored_dust.append((symbol, value_usdc))
                        continue

                    total_usdc   += value_usdc
                    positions_out[symbol] = {
                        "qty":           qty,
                        "avg_price":     current_price,  # inconnu, on prend le prix actuel
                        "current_price": current_price,
                        "pnl_usdc":      0.0,
                    }
                    # Enregistre dans le suivi local si pas encore present
                    if symbol not in self._live_port.positions:
                        self._live_port.set_from_balance(symbol, qty, current_price)
                except Exception:
                    pass

        if ignored_dust:
            log.info("live_dust_ignored",
                     count=len(ignored_dust),
                     min_usdc=LIVE_MIN_TRACK_USDC,
                     dust=[f"{s}={v:.2f}" for s, v in ignored_dust])

        initial = float(os.getenv("LIVE_INITIAL_USDC", str(usdc_balance)))
        pnl_pct = (total_usdc - initial) / initial * 100 if initial > 0 else 0.0

        return {
            "total_usdc":   total_usdc,
            "usdc_balance": usdc_balance,
            "positions":    positions_out,
            "pnl_pct":      pnl_pct,
            "mode":         self.mode,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }

    def _live_snapshot_local(self) -> dict:
        """Snapshot de secours depuis les donnees locales (si API indisponible)."""
        return {
            "total_usdc":   0.0,
            "usdc_balance": 0.0,
            "positions":    {
                s: {"qty": p["qty"], "avg_price": p["avg_price"],
                    "current_price": p["avg_price"], "pnl_usdc": 0.0}
                for s, p in self._live_port.positions.items()
            },
            "pnl_pct":    0.0,
            "mode":       self.mode,
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        }

    async def sync_live_positions(self) -> None:
        """
        Synchronise le suivi local avec les vrais soldes Coinbase.
        A appeler au demarrage en mode live.
        """
        if self.mode != "live":
            return
        try:
            snap = await self._live_snapshot()
            n    = len(snap["positions"])
            log.info("live_positions_synced",
                     usdc_balance=round(snap["usdc_balance"], 2),
                     positions=n)
        except Exception as exc:
            log.warning("live_positions_sync_failed", error=str(exc))


if __name__ == "__main__":
    import asyncio

    async def demo() -> None:
        client = CoinbaseClient()
        price  = await client.get_price("BTC-USDC")
        print(f"BTC-USDC : {price:,.2f} USDC")
        snap = await client.get_portfolio_snapshot()
        print(f"Mode : {snap['mode']} | Total : {snap['total_usdc']:.2f} USDC")
        await client.close()

    asyncio.run(demo())
