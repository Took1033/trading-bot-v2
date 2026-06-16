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

# ── Ordres MAKER (limit post-only) sur les entrees BUY ───────────────────────
# Le maker paie ~moitie du taker -> divise ~par 2 le cout aller-retour et double
# le R:R net. Mais le fill n'est PAS garanti : on pose un limit post-only au bid,
# on attend MAKER_FILL_TIMEOUT_S, et si non rempli on annule + retombe sur le
# market (taker) — donc au pire on garde le comportement actuel.
# Desactive par defaut : a valider en live (poll/cancel SDK) avant activation.
USE_MAKER_ENTRIES   = os.getenv("LIVE_USE_MAKER_ENTRIES", "false").lower() in ("true", "1", "yes")
MAKER_FILL_TIMEOUT_S = float(os.getenv("MAKER_FILL_TIMEOUT_S", "45"))   # attente max de fill
MAKER_POLL_S         = float(os.getenv("MAKER_POLL_S", "3"))           # intervalle de polling
MAKER_FALLBACK_MARKET = os.getenv("MAKER_FALLBACK_MARKET", "true").lower() in ("true", "1", "yes")


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
        # Optionnel : resolver(symbol)->prix d'entree connu (ou None). Injecte par
        # BotSwarm depuis la DB, pour restaurer l'avg_price d'une position
        # re-adoptee apres reboot au lieu de l'ecraser avec le prix courant.
        self.entry_price_resolver = None

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
        bid = ask = None            # bruts du carnet (pour le maker) ; None si fetch KO
        bid_str = None              # chaine d'origine -> precision de prix exacte
        try:
            bb = await self._run_sync(self._real_client.get_best_bid_ask, product_ids=[symbol])
            pb = bb.pricebooks[0]
            bid_str = str(pb.bids[0].price)
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

            # ── Tentative MAKER (limit post-only au bid) sur les entrees ──────
            # Jamais pour une sortie de protection (force=True). Si le carnet
            # n'a pas ete recupere (bid None), on saute direct au market.
            if USE_MAKER_ENTRIES and not force and bid is not None:
                maker_order = await self._live_maker_buy(
                    symbol, usdc_amount, bid, bid_str
                )
                if maker_order is not None:
                    return maker_order
                # maker non rempli / KO -> fallback market ci-dessous (si autorise)
                if not MAKER_FALLBACK_MARKET:
                    raise RuntimeError(
                        f"Maker non rempli pour {symbol} et fallback market desactive"
                    )
                log.info("maker_fallback_to_market", symbol=symbol, usdc=usdc_amount)

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
    # Ordres MAKER (limit post-only) — entrees BUY uniquement
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _decimals_of(num_str: str | None) -> int:
        """Nombre de decimales d'une chaine de prix SDK (respect du tick de prix)."""
        if not num_str or "." not in num_str:
            return 2
        return len(num_str.split(".")[1])

    async def _get_order_status(self, order_id: str) -> tuple[str, float | None, float | None]:
        """(status, filled_qty, avg_price). Robuste aux variations du SDK."""
        try:
            resp = await self._run_sync(self._real_client.get_order, order_id=order_id)
            o = getattr(resp, "order", resp)
            status = str(getattr(o, "status", "") or "").upper()
            fq = getattr(o, "filled_size", None)
            ap = getattr(o, "average_filled_price", None)
            fq = float(fq) if fq not in (None, "") else None
            ap = float(ap) if ap not in (None, "") else None
            return status, fq, ap
        except Exception as exc:
            log.warning("get_order_status_error", order_id=order_id, error=str(exc))
            return "UNKNOWN", None, None

    async def _cancel_order(self, order_id: str) -> None:
        try:
            await self._run_sync(self._real_client.cancel_orders, order_ids=[order_id])
            log.info("maker_order_cancelled", order_id=order_id)
        except Exception as exc:
            log.warning("cancel_order_error", order_id=order_id, error=str(exc))

    async def _live_maker_buy(self, symbol: str, usdc_amount: float,
                              bid: float, bid_str: str | None) -> Order | None:
        """
        Pose un BUY limit post-only au bid et attend le fill (jusqu'a timeout).
        Retourne un Order rempli, ou None si non rempli / erreur (=> fallback market).
        Toute exception est avalee -> None : on ne casse jamais l'execution.
        """
        try:
            decimals    = self._decimals_of(bid_str)
            limit_price = round(bid, decimals)
            if limit_price <= 0:
                return None
            base_size   = round(usdc_amount / limit_price, 8)
            order_id_cli = str(uuid.uuid4())
            order_config = {
                "limit_limit_gtc": {
                    "base_size":   str(base_size),
                    "limit_price": f"{limit_price:.{decimals}f}",
                    "post_only":   True,
                }
            }
            log.info("maker_buy_placing", symbol=symbol, limit_price=limit_price,
                     base_size=base_size, usdc=usdc_amount, timeout_s=MAKER_FILL_TIMEOUT_S)

            result = await self._run_sync(
                self._real_client.create_order,
                client_order_id=order_id_cli, product_id=symbol,
                side="BUY", order_configuration=order_config,
            )
            if not getattr(result, "success", False):
                log.warning("maker_buy_rejected", symbol=symbol,
                            error=str(getattr(result, "error_response", {})))
                return None
            order_id = result.order_id if hasattr(result, "order_id") else order_id_cli

            # Polling jusqu'au fill ou timeout
            waited = 0.0
            while waited < MAKER_FILL_TIMEOUT_S:
                await asyncio.sleep(MAKER_POLL_S)
                waited += MAKER_POLL_S
                status, fq, ap = await self._get_order_status(order_id)
                if status == "FILLED":
                    qty_f, px_f = (fq or base_size), (ap or limit_price)
                    self._live_port.update_buy(symbol, qty_f, px_f)
                    log.info("maker_buy_filled", symbol=symbol, qty=round(qty_f, 8),
                             price=round(px_f, 6), order_id=order_id, waited_s=waited)
                    return Order(symbol=symbol, side="buy", qty=qty_f, price=px_f,
                                 order_id=order_id, status="filled")
                if status in ("CANCELLED", "EXPIRED", "FAILED", "REJECTED"):
                    log.info("maker_buy_terminated", symbol=symbol, status=status)
                    return None

            # Timeout -> annuler, puis re-verifier (anti double-achat sur race)
            await self._cancel_order(order_id)
            status, fq, ap = await self._get_order_status(order_id)
            if status == "FILLED":
                qty_f, px_f = (fq or base_size), (ap or limit_price)
                self._live_port.update_buy(symbol, qty_f, px_f)
                log.info("maker_buy_filled_late", symbol=symbol, order_id=order_id)
                return Order(symbol=symbol, side="buy", qty=qty_f, price=px_f,
                             order_id=order_id, status="filled")
            log.info("maker_buy_unfilled", symbol=symbol, order_id=order_id, waited_s=waited)
            return None

        except Exception as exc:
            log.warning("maker_buy_error", symbol=symbol, error=str(exc))
            return None

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

                    # Prix d'entree : on tente de le restaurer depuis l'historique
                    # (dernier achat connu en DB) au lieu d'ecraser avec le prix
                    # courant — sinon le P&L d'une position re-adoptee apres reboot
                    # est faux. Inconnu (depot hors-bot) -> on garde le prix courant.
                    entry_price = None
                    if self.entry_price_resolver is not None:
                        try:
                            recovered = self.entry_price_resolver(symbol)
                            if recovered and float(recovered) > 0:
                                entry_price = float(recovered)
                        except Exception as exc:
                            log.debug("entry_price_resolve_failed", symbol=symbol, error=str(exc))

                    if entry_price is not None:
                        cost_usdc = qty * entry_price
                        pnl_usdc  = value_usdc - cost_usdc - ROUND_TRIP_FEE_PCT * cost_usdc
                        avg_price = entry_price
                    else:
                        avg_price = current_price   # entree inconnue : pas de P&L fiable
                        pnl_usdc  = 0.0

                    total_usdc   += value_usdc
                    positions_out[symbol] = {
                        "qty":           qty,
                        "avg_price":     avg_price,
                        "current_price": current_price,
                        "pnl_usdc":      pnl_usdc,
                    }
                    # Enregistre dans le suivi local si pas encore present
                    if symbol not in self._live_port.positions:
                        self._live_port.set_from_balance(symbol, qty, avg_price)
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
