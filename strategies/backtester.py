"""
Backtester — rejoue une stratégie sur des données historiques.

Simule le même comportement que l'orchestrateur paper :
  - Mêmes paramètres de risque (MAX_POSITION_PCT, MIN_CONFIDENCE)
  - Pas de shortcircuit — positions long seulement
  - Liquidation finale à la clôture si position ouverte

Usage rapide :
    import asyncio
    from strategies.backtester import Backtester, fetch_prices_coinbase
    from strategies.simple_ma import analyze

    async def main():
        prices = await fetch_prices_coinbase("BTC-USD", granularity=86400, limit=300)
        bt     = Backtester(strategy=analyze)
        result = await bt.run("BTC-USD", prices)
        print(result.summary())

    asyncio.run(main())
"""
from __future__ import annotations

import asyncio
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

import structlog

log = structlog.get_logger()

# Mêmes paramètres que risk_agent pour cohérence
MAX_POSITION_PCT = 0.02    # 2 % du capital par trade
MIN_CONFIDENCE   = 0.55    # 55 %
MIN_USDC_TRADE   = 10.0    # 10 USDC minimum
EMA_WARMUP       = 22      # points nécessaires pour les EMAs


# ──────────────────────────────────────────────────────────────────────────────
# Métriques standard (fonctions PURES : testables sans réseau ni stratégie)
# Ce sont les chiffres qui "prouvent" un edge de façon comparable entre stratégies.
# ──────────────────────────────────────────────────────────────────────────────

def compute_sharpe(equity: list[float], periods_per_year: float | None = None) -> float:
    """Sharpe des rendements pas-à-pas de la courbe d'équité. Annualisé si
    `periods_per_year` est fourni (365 daily, 24*365 horaire). Taux sans risque
    supposé nul (crypto, horizon court). 0.0 si trop peu de points ou variance nulle."""
    rets: list[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev > 0:
            rets.append((equity[i] - prev) / prev)
    if len(rets) < 2:
        return 0.0
    # Ecart-type d'ECHANTILLON (stdev, /N-1), pas de population (pstdev, /N) : un
    # rapport de preuve ne doit pas biaiser le Sharpe a la hausse de sqrt(N/(N-1)).
    sd = statistics.stdev(rets)
    if sd <= 0:
        return 0.0
    sharpe = statistics.fmean(rets) / sd
    if periods_per_year:
        sharpe *= periods_per_year ** 0.5
    return round(sharpe, 3)


def compute_profit_factor(pnls: list[float]) -> float | None:
    """Somme des gains / |somme des pertes|. None si aucune perte (edge sans perte,
    ratio indéfini) ; 0.0 si aucun gain."""
    gross_win  = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss > 0:
        return round(gross_win / gross_loss, 2)
    return None if gross_win > 0 else 0.0


def compute_exposure(n_in_position: int, n_steps: int) -> float:
    """% du temps passé en position — le risque de marché réellement encouru."""
    return round(n_in_position / n_steps * 100, 1) if n_steps > 0 else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    side:      str     # "buy" | "sell"
    price:     float
    qty:       float
    timestamp: str
    pnl_usdc:  float = 0.0    # renseigné à la clôture (sell uniquement)


@dataclass
class BacktestResult:
    symbol:       str
    initial_usdc: float
    final_usdc:   float
    total_return: float        # %
    n_trades:     int          # nombre de trades fermés (sell)
    n_wins:       int          # trades profitables
    win_rate:     float        # %
    max_drawdown: float        # %
    sharpe:        float = 0.0          # Sharpe (annualisé si periods_per_year fourni)
    profit_factor: float | None = None  # gains/pertes (None = aucune perte)
    exposure:      float = 0.0          # % du temps en position
    avg_win:       float = 0.0          # P&L moyen des trades gagnants (USDC)
    avg_loss:      float = 0.0          # P&L moyen des trades perdants (USDC)
    trades:       list[Trade] = field(default_factory=list)

    def summary(self) -> str:
        sep = "=" * 38
        lines = [
            sep,
            f"  Backtest | {self.symbol}",
            sep,
            f"  Capital initial : {self.initial_usdc:>12,.2f} USDC",
            f"  Capital final   : {self.final_usdc:>12,.2f} USDC",
            f"  Rendement total : {self.total_return:>+11.2f} %",
            f"  Trades fermés   : {self.n_trades:>12}",
            f"  Gagnants        : {self.n_wins:>12} ({self.win_rate:.1f}%)",
            f"  Max drawdown    : {self.max_drawdown:>11.2f} %",
            f"  Sharpe          : {self.sharpe:>12.3f}",
            f"  Profit factor   : {('∞ (aucune perte)' if self.profit_factor is None else f'{self.profit_factor:>12.2f}')}",
            f"  Exposition      : {self.exposure:>11.1f} %",
            f"  Gain/perte moy. : {self.avg_win:>+8.2f} / {self.avg_loss:>+8.2f} USDC",
            sep,
        ]
        return "\n".join(lines)

    def trades_summary(self, max_display: int = 20) -> str:
        """Retourne un résumé textuel des trades (buy + sell groupés)."""
        lines = ["Trades :"]
        for t in self.trades[:max_display]:
            if t.side == "buy":
                lines.append(f"  BUY  {t.qty:.6f} @ {t.price:>10,.2f}  [{t.timestamp[:10]}]")
            else:
                tag = "[+]" if t.pnl_usdc > 0 else ("[-]" if t.pnl_usdc < 0 else "[=]")
                lines.append(
                    f"  SELL {t.qty:.6f} @ {t.price:>10,.2f}  "
                    f"P&L {t.pnl_usdc:>+9.2f} USDC  {tag}  [{t.timestamp[:10]}]"
                )
        if len(self.trades) > max_display:
            lines.append(f"  … et {len(self.trades) - max_display} autres trades")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Backtester
# ──────────────────────────────────────────────────────────────────────────────

class Backtester:
    """
    Simulation paper d'une stratégie async sur données historiques.

    Args:
        strategy:     callable async(symbol, prices) → Signal
        initial_usdc: capital de départ en USDC
        warmup:       nombre de prix requis avant le premier signal
    """

    def __init__(
        self,
        strategy: Callable[[str, list[float]], Awaitable],
        initial_usdc: float = 10_000.0,
        warmup: int = EMA_WARMUP,
    ) -> None:
        self.strategy     = strategy
        self.initial_usdc = initial_usdc
        self.warmup       = warmup

    async def run(self, symbol: str, prices: list[float],
                  periods_per_year: float | None = None) -> BacktestResult:
        """
        Rejoue la stratégie sur `prices` (ordre chronologique, + ancien → + récent).
        Retourne un BacktestResult avec toutes les métriques.

        `periods_per_year` annualise le Sharpe selon la granularité des `prices`
        (365 pour du daily, 24*365 pour de l'horaire) ; None => Sharpe par pas.
        """
        if len(prices) < self.warmup + 1:
            raise ValueError(
                f"Besoin d'au moins {self.warmup + 1} prix, {len(prices)} fournis."
            )

        usdc:             float       = self.initial_usdc
        qty_held:         float       = 0.0
        avg_buy:          float       = 0.0
        trades:           list[Trade] = []
        portfolio_values: list[float] = []
        peak:             float       = self.initial_usdc

        window: deque[float] = deque(maxlen=self.warmup + 50)
        n_steps:  int = 0   # pas "tradables" (post-warmup)
        n_in_pos: int = 0   # pas passés en position -> exposition

        for price in prices:
            window.append(price)

            # Valeur courante et peak pour le drawdown
            current_value = usdc + qty_held * price
            portfolio_values.append(current_value)
            if current_value > peak:
                peak = current_value

            # Pré-chauffe
            if len(window) < self.warmup:
                continue

            # Exposition : ce pas est tradable ; on est en position si qty_held > 0.
            n_steps += 1
            if qty_held > 0:
                n_in_pos += 1

            signal = await self.strategy(symbol, list(window))
            ts = datetime.now(timezone.utc).isoformat()

            # ── BUY ──────────────────────────────────────────────────────────
            if signal.action == "buy" and qty_held == 0:
                if signal.confidence < MIN_CONFIDENCE:
                    continue
                usdc_to_spend = usdc * MAX_POSITION_PCT
                if usdc_to_spend < MIN_USDC_TRADE:
                    continue
                qty      = usdc_to_spend / price
                usdc    -= usdc_to_spend
                qty_held = qty
                avg_buy  = price
                trades.append(Trade(side="buy", price=price, qty=qty, timestamp=ts))

            # ── SELL ─────────────────────────────────────────────────────────
            elif signal.action == "sell" and qty_held > 0:
                if signal.confidence < MIN_CONFIDENCE:
                    continue
                pnl   = qty_held * (price - avg_buy)
                usdc += qty_held * price
                trades.append(
                    Trade(side="sell", price=price, qty=qty_held, timestamp=ts, pnl_usdc=pnl)
                )
                qty_held = 0.0
                avg_buy  = 0.0

        # ── Liquidation finale si position ouverte ────────────────────────────
        if qty_held > 0:
            last_price = prices[-1]
            pnl        = qty_held * (last_price - avg_buy)
            usdc      += qty_held * last_price
            trades.append(
                Trade(
                    side="sell",
                    price=last_price,
                    qty=qty_held,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    pnl_usdc=pnl,
                )
            )

        # ── Métriques finales ─────────────────────────────────────────────────
        sell_trades  = [t for t in trades if t.side == "sell"]
        n_wins       = sum(1 for t in sell_trades if t.pnl_usdc > 0)
        total_return = (usdc - self.initial_usdc) / self.initial_usdc * 100

        # Max drawdown
        max_dd  = 0.0
        peak_dd = portfolio_values[0] if portfolio_values else self.initial_usdc
        for v in portfolio_values:
            if v > peak_dd:
                peak_dd = v
            dd = (peak_dd - v) / peak_dd * 100
            if dd > max_dd:
                max_dd = dd

        # Métriques standard (Sharpe / profit factor / exposition / moyennes)
        pnls   = [t.pnl_usdc for t in sell_trades]
        gains  = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        result = BacktestResult(
            symbol        = symbol,
            initial_usdc  = self.initial_usdc,
            final_usdc    = round(usdc, 2),
            total_return  = round(total_return, 2),
            n_trades      = len(sell_trades),
            n_wins        = n_wins,
            win_rate      = round(n_wins / len(sell_trades) * 100, 1) if sell_trades else 0.0,
            max_drawdown  = round(max_dd, 2),
            sharpe        = compute_sharpe(portfolio_values, periods_per_year),
            profit_factor = compute_profit_factor(pnls),
            exposure      = compute_exposure(n_in_pos, n_steps),
            avg_win       = round(sum(gains) / len(gains), 2) if gains else 0.0,
            avg_loss      = round(sum(losses) / len(losses), 2) if losses else 0.0,
            trades        = trades,
        )

        log.info(
            "backtest_complete",
            symbol       = symbol,
            n_prices     = len(prices),
            total_return = result.total_return,
            n_trades     = result.n_trades,
            win_rate     = result.win_rate,
            max_drawdown = result.max_drawdown,
        )

        return result


# ──────────────────────────────────────────────────────────────────────────────
# Utilitaire : récupération de prix historiques (API publique Coinbase Exchange)
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_prices_coinbase(
    symbol:      str = "BTC-USD",
    granularity: int = 86400,
    limit:       int = 300,
) -> list[float]:
    """
    Récupère des prix de clôture depuis l'API publique Coinbase Exchange.
    Aucune authentification requise.

    Args:
        symbol:      ex. "BTC-USD" (tiret obligatoire pour cette API)
        granularity: en secondes — valeurs acceptées : 60, 300, 900, 3600, 21600, 86400
        limit:       nombre de chandelles (max ~300 par requête)

    Returns:
        Liste de prix de clôture en ordre chronologique (+ ancien → + récent)

    Raises:
        RuntimeError si l'API retourne une erreur HTTP.
    """
    import aiohttp
    import time

    _VALID_GRAN = {60, 300, 900, 3600, 21600, 86400}
    if granularity not in _VALID_GRAN:
        raise ValueError(f"granularity doit être dans {_VALID_GRAN}")

    end   = int(time.time())
    start = end - granularity * limit

    url = (
        f"https://api.exchange.coinbase.com/products/{symbol}/candles"
        f"?start={start}&end={end}&granularity={granularity}"
    )
    headers = {"User-Agent": "TradingBot/2.0 (paper)"}

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Coinbase Exchange API {resp.status}: {body[:200]}")
            candles = await resp.json()

    if not candles:
        raise RuntimeError(f"Aucune chandelle retournée pour {symbol}")

    # Format : [timestamp, low, high, open, close, volume]
    # L'API retourne les chandelles du plus récent au plus ancien → inverser
    prices = [float(c[4]) for c in reversed(candles)]
    log.info(
        "prices_fetched",
        symbol      = symbol,
        granularity = granularity,
        n           = len(prices),
        first       = round(prices[0], 2),
        last        = round(prices[-1], 2),
    )
    return prices


# ──────────────────────────────────────────────────────────────────────────────
# Script standalone
# ──────────────────────────────────────────────────────────────────────────────

async def _demo() -> None:
    from strategies.simple_ma import analyze

    print("📥 Récupération des prix BTC-USD (300 jours, données journalières)…")
    try:
        prices = await fetch_prices_coinbase("BTC-USD", granularity=86400, limit=300)
    except RuntimeError as e:
        print(f"❌ {e}")
        return

    print(f"   {len(prices)} prix récupérés.")
    print(f"   Premier : {prices[0]:,.2f} USD  |  Dernier : {prices[-1]:,.2f} USD\n")

    bt     = Backtester(strategy=analyze, initial_usdc=10_000.0)
    result = await bt.run("BTC-USD", prices)

    print(result.summary())
    if result.trades:
        print()
        print(result.trades_summary())


if __name__ == "__main__":
    asyncio.run(_demo())
