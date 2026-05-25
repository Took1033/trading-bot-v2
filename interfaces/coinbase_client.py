from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import structlog
from dotenv import load_dotenv

load_dotenv()

log = structlog.get_logger()

MODE: Literal["paper", "live"] = os.getenv("COINBASE_MODE", "paper")  # type: ignore


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
class Order:
    symbol:    str
    side:      Literal["buy", "sell"]
    qty:       float
    price:     float
    timestamp: str  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    order_id:  str  = ""
    status:    str  = "filled"   # paper orders sont immediatement filled


class CoinbaseClient:
    """
    Client Coinbase avec switch paper/live.

    En mode paper :
      - Simule les ordres localement sans appel API reel.
      - Prix reels recuperes via l'API publique Coinbase (sans auth).
      - Session aiohttp persistante pour reduire la latence.

    En mode live :
      - Utilise coinbase-advanced-py (cles requises dans .env).
    """

    def __init__(self) -> None:
        self.mode    = MODE
        self._paper  = PaperPortfolio()
        self._real_client = None
        self._session     = None      # session aiohttp persistante

        if self.mode == "live":
            self._init_live()
        else:
            log.info("coinbase_client_init", mode="paper", balance=self._paper.usdc_balance)

    def _init_live(self) -> None:
        try:
            from coinbase.rest import RESTClient  # type: ignore
            self._real_client = RESTClient(
                api_key=os.getenv("COINBASE_API_KEY", ""),
                api_secret=os.getenv("COINBASE_API_SECRET", ""),
            )
            log.info("coinbase_client_init", mode="live")
        except ImportError:
            raise RuntimeError(
                "coinbase-advanced-py non installe. Lance : pip install coinbase-advanced-py"
            )

    # ------------------------------------------------------------------ #
    #  Session HTTP persistante                                           #
    # ------------------------------------------------------------------ #

    async def _get_session(self):
        """Retourne la session aiohttp, en la creant si necessaire."""
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(limit=5),
            )
        return self._session

    async def close(self) -> None:
        """Ferme proprement la session HTTP."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ #
    #  Prix                                                               #
    # ------------------------------------------------------------------ #

    async def get_price(self, symbol: str = "BTC-USDC") -> float:
        """Retourne le dernier prix mid pour le symbole donne."""
        if self.mode == "paper":
            return await self._paper_price(symbol)
        return await self._live_price(symbol)

    async def _paper_price(self, symbol: str) -> float:
        """Recupere le prix reel via API publique (pas d'auth requise)."""
        url     = f"https://api.coinbase.com/v2/prices/{symbol}/spot"
        session = await self._get_session()
        async with session.get(url) as resp:
            data  = await resp.json()
            price = float(data["data"]["amount"])
            log.debug("paper_price_fetched", symbol=symbol, price=price)
            return price

    async def _live_price(self, symbol: str) -> float:
        product = self._real_client.get_best_bid_ask(product_ids=[symbol])
        bid = float(product.pricebooks[0].bids[0].price)
        ask = float(product.pricebooks[0].asks[0].price)
        return (bid + ask) / 2

    # ------------------------------------------------------------------ #
    #  Ordres                                                             #
    # ------------------------------------------------------------------ #

    async def place_order(
        self,
        symbol: str,
        side:   Literal["buy", "sell"],
        qty:    float,
    ) -> Order:
        if self.mode == "live":
            raise RuntimeError("Mode LIVE non autorise sans confirmation explicite.")

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

        log.info("paper_order_filled", symbol=symbol, side=side, qty=qty, price=price)
        return order

    # ------------------------------------------------------------------ #
    #  Portefeuille                                                       #
    # ------------------------------------------------------------------ #

    def get_position(self, symbol: str) -> dict | None:
        """Retourne la position ouverte pour un symbole, ou None."""
        pos = self._paper.positions.get(symbol)
        return dict(pos) if pos and pos.get("qty", 0) > 1e-8 else None

    async def get_portfolio_snapshot(self) -> dict:
        prices: dict[str, float] = {}
        for symbol in list(self._paper.positions.keys()):
            prices[symbol] = await self.get_price(symbol)

        return {
            "total_usdc":    self._paper.value(prices),
            "usdc_balance":  self._paper.usdc_balance,
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


if __name__ == "__main__":
    import asyncio

    async def demo() -> None:
        client = CoinbaseClient()
        price  = await client.get_price("BTC-USDC")
        print(f"BTC-USDC : {price:,.2f} USDC")

        order = await client.place_order("BTC-USDC", "buy", qty=0.001)
        print(f"Ordre: {order.side} {order.qty} BTC @ {order.price:,.2f}")

        snap = await client.get_portfolio_snapshot()
        print(f"Portefeuille: {snap['total_usdc']:.2f} USDC | P&L: {snap['pnl_pct']:+.3f}%")

        await client.close()

    asyncio.run(demo())
