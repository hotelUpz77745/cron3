# ============================================================
# FILE: CORE/coin_selectors.py
# ROLE: Dual quant coin selectors (Agent Market Scanner & Samsonov 3-Step Funnel)
# ============================================================

from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from consts import BASE_DIR, DATA_DIR, PARENT_DIR, _CFG
from c_log import UnifiedLogger
from API.BINANCE.public import BinancePublic

logger = UnifiedLogger("CoinSelectors")


# ==============================================================================
# КЛАСС 1: АЛГОРИТМ АГЕНТА (Протокол 1: Рыночный сканер волатильности и ликвидности)
# ==============================================================================

class AgentMarketSelector:
    """
    Алгоритм Агента (Протокол 1):
    Сканирует весь живой рынок Binance Futures (USDT-M), отбирая активы с наивысшей
    суточной волатильностью (24h Price Range) и институциональной ликвидностью стакана.
    
    Именно этот алгоритм подобрал первый боевой пул: GRASS, PUMP, 0G.
    """

    def __init__(
        self,
        min_range_pct: float = 20.0,
        min_volume_usdt: float = 90_000_000.0,
        exclude_stablecoins: bool = True,
    ):
        self.min_range_pct = min_range_pct
        self.min_volume_usdt = min_volume_usdt
        self.exclude_stablecoins = exclude_stablecoins
        self.stablecoin_patterns = ("USDC", "FDUSD", "TUSD", "BUSD", "EUR", "DAI")

    async def select_top_coins(
        self,
        target_count: int = 4,
        universe_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Асинхронный отбор ТОП-N монет по рыночному размаху и ликвидности.
        Если передан universe_filter, фильтрует только среди монет этого списка.
        """
        try:
            tickers = await BinancePublic.get_24h_ticker()
        except Exception as e:
            logger.error(f"[AgentSelector] Failed to fetch 24h tickers from Binance: {e}")
            return []

        if not tickers:
            logger.warning("[AgentSelector] No ticker data returned by Binance public API.")
            return []

        candidates: List[Dict[str, Any]] = []

        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue

            if self.exclude_stablecoins and any(p in sym for p in self.stablecoin_patterns):
                continue

            if universe_filter and sym not in universe_filter:
                continue

            try:
                high = float(t.get("highPrice", 0.0))
                low = float(t.get("lowPrice", 0.0))
                vol_usdt = float(t.get("quoteVolume", 0.0))
                chg_pct = float(t.get("priceChangePercent", 0.0))
                last_price = float(t.get("lastPrice", 0.0))

                if low <= 0 or high <= 0:
                    continue

                range_pct = ((high - low) / low) * 100.0

                if range_pct < self.min_range_pct:
                    continue

                if vol_usdt < self.min_volume_usdt:
                    continue

                candidates.append({
                    "symbol": sym,
                    "range_pct": round(range_pct, 2),
                    "price_change_pct": round(chg_pct, 2),
                    "volume_usdt": round(vol_usdt, 2),
                    "volume_m": round(vol_usdt / 1_000_000.0, 2),
                    "high_price": high,
                    "low_price": low,
                    "last_price": last_price,
                })
            except (ValueError, TypeError):
                continue

        # Ранжирование по суточному размаху волатильности (первичный критерий)
        # с вторичным учетом объема торгов
        candidates.sort(key=lambda x: (x["range_pct"], x["volume_usdt"]), reverse=True)
        return candidates[:target_count]

    def select_top_coins_sync(
        self,
        target_count: int = 4,
        universe_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Синхронный фасад для мгновенного вызова без async boilerplate."""
        try:
            return asyncio.run(self.select_top_coins(target_count=target_count, universe_filter=universe_filter))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self.select_top_coins(target_count=target_count, universe_filter=universe_filter))


# ==============================================================================
# КЛАСС 2: АЛГОРИТМ САМСОНОВА (Протокол 2: 3-ступенчатая Квантовая Воронка)
# ==============================================================================

class SamsonovFunnelSelector:
    """
    Алгоритм Главного Тим Лида Самсонова Николая (Протокол 2):
    3-ступенчатая последовательная воронка квантовой фильтрации по телеметрии Радара:
      1. ШАГ 1: Ранжирование по Realized PnL (net fee) -> берем срез ТОП-50% (отсекаем спящие монеты).
      2. ШАГ 2: Ранжирование по Peak Net / Max DD -> берем срез ТОП-50% (отсекаем мины замедленного действия).
      3. ШАГ 3: Ранжирование по Trade Velocity (число сделок) -> берем ТОП-N монет (выбираем скоростные локомотивы).
    """

    def __init__(self, radar_stats_dir: Optional[Path] = None):
        if radar_stats_dir:
            self.radar_stats_dir = Path(radar_stats_dir)
        else:
            paths_cfg = _CFG.get("paths", {})
            radar_cfg = paths_cfg.get("radar_dir")
            if radar_cfg:
                p = Path(radar_cfg)
                if not p.is_absolute():
                    p = (BASE_DIR / p).resolve()
                self.radar_stats_dir = p / "ANALYTICS" / "STATS"
            else:
                self.radar_stats_dir = PARENT_DIR / "cron3PapperUniverses" / "ANALYTICS" / "STATS"

    def is_radar_available(self) -> bool:
        return self.radar_stats_dir.exists() and self.radar_stats_dir.is_dir()

    def load_universe_data(self, universe_id: str) -> Optional[Dict[str, Any]]:
        if not self.is_radar_available():
            logger.warning(f"[SamsonovSelector] Radar directory not found: {self.radar_stats_dir}")
            return None

        file_path = self.radar_stats_dir / f"analytics_{universe_id}.json"
        if not file_path.exists():
            logger.warning(f"[SamsonovSelector] Universe stats not found: {file_path}")
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[SamsonovSelector] Failed to read {file_path}: {e}")
            return None

    def get_funnel_stages(
        self,
        universe_id: str = "u0_fat_swing123",
        target_count: int = 4,
    ) -> Dict[str, Any]:
        """Возвращает детальную разбивку всех этапов воронки."""
        data = self.load_universe_data(universe_id)
        if not data or "per_coin" not in data:
            return {"total_input": 0, "step1_pool": [], "step2_pool": [], "step3_pool": [], "final_targets": []}

        per_coin: Dict[str, Dict[str, Any]] = data["per_coin"]
        records: List[Dict[str, Any]] = []

        for sym, stats in per_coin.items():
            realized_net = float(stats.get("realized_pnl_net_usdt", stats.get("realized_pnl_usdt", 0.0)))
            net_profit = float(stats.get("net_profit_usdt", 0.0))
            peak_net = float(stats.get("peak_net_profit_usdt", stats.get("max_net_profit", 0.0)))
            max_dd = abs(float(stats.get("max_drawdown_net_usdt", stats.get("max_drawdown", 0.0))))
            floating = float(stats.get("current_drawdown", 0.0))
            trades = int(stats.get("trades", 0))

            dd_denom = max(max_dd, 0.5)
            resilience = max(peak_net, 0.0) / dd_denom

            records.append({
                "symbol": sym,
                "trades": trades,
                "realized_net": round(realized_net, 2),
                "peak_net": round(peak_net, 2),
                "max_dd": round(max_dd, 2),
                "resilience": round(resilience, 2),
                "net_profit": round(net_profit, 2),
                "floating": round(floating, 2),
            })

        total_input = len(records)

        # ШАГ 1: Ранжирование по Realized PnL (net fee) -> срез ТОП-50%
        step1_sorted = sorted(records, key=lambda x: x["realized_net"], reverse=True)
        half1 = len(step1_sorted) // 2
        step1_pool = step1_sorted[:half1]

        # ШАГ 2: Ранжирование по коэффициенту Peak Net / Max DD -> срез ТОП-50%
        step2_sorted = sorted(step1_pool, key=lambda x: x["resilience"], reverse=True)
        half2 = len(step2_sorted) // 2
        step2_pool = step2_sorted[:half2]

        # ШАГ 3: Ранжирование по Trade Velocity (число сделок) -> целевой ТОП-N
        step3_sorted = sorted(step2_pool, key=lambda x: x["trades"], reverse=True)
        final_targets = step3_sorted[:target_count]

        return {
            "total_input": total_input,
            "step1_pool": step1_pool,
            "step2_pool": step2_pool,
            "step3_pool": step3_sorted,
            "final_targets": final_targets,
        }

    def run_samsonov_funnel(
        self,
        universe_id: str = "u0_fat_swing123",
        target_count: int = 4,
    ) -> Dict[str, Any]:
        """Алиас для get_funnel_stages."""
        return self.get_funnel_stages(universe_id=universe_id, target_count=target_count)

    def calculate_coin_metrics(
        self,
        universe_id: str = "u0_fat_swing123",
        w_resilience: float = 1.0,
        w_realized: float = 1.0,
        w_velocity: float = 1.0,
        w_pain_ratio: float = 1.0,
        w_pain_usdt: float = 1.0,
    ) -> List[Dict[str, Any]]:
        """
        Производит расчет Скора Самсонова и Индекса Истощения (Exhaustion Alpha)
        по всем монетам указанного универсума.
        """
        data = self.load_universe_data(universe_id)
        if not data or "per_coin" not in data:
            return []

        per_coin: Dict[str, Dict[str, Any]] = data["per_coin"]
        total_coins = max(len(per_coin), 1)
        total_trades = sum(s.get("trades", 0) for s in per_coin.values())
        avg_trades = total_trades / total_coins if total_coins > 0 else 1.0

        records: List[Dict[str, Any]] = []

        for sym, stats in per_coin.items():
            realized_net = float(stats.get("realized_pnl_net_usdt", stats.get("realized_pnl_usdt", 0.0)))
            net_profit = float(stats.get("net_profit_usdt", 0.0))
            peak_net = float(stats.get("peak_net_profit_usdt", stats.get("max_net_profit", 0.0)))
            max_dd = abs(float(stats.get("max_drawdown_net_usdt", stats.get("max_drawdown", 0.0))))
            floating = float(stats.get("current_drawdown", 0.0))
            trades = int(stats.get("trades", 0))

            dd_denom = max(max_dd, 0.5)
            peak_clamped = max(peak_net, 0.0)
            resilience = peak_clamped / dd_denom
            realized_pos = max(realized_net, 0.0)
            velocity_ratio = trades / avg_trades if avg_trades > 0 else 1.0

            samsonov_full = (resilience ** w_resilience) * (realized_pos ** w_realized) * (velocity_ratio ** w_velocity)
            samsonov_base = (resilience ** w_resilience) * (realized_pos ** w_realized)

            pain_usdt = abs(floating) if floating < 0 else 0.0
            pain_ratio = pain_usdt / dd_denom
            exhaustion_index = (pain_ratio ** w_pain_ratio) * ((pain_usdt / 10.0) ** w_pain_usdt) * (velocity_ratio ** 0.5)

            records.append({
                "symbol": sym,
                "net_profit": round(net_profit, 2),
                "realized_net": round(realized_net, 2),
                "peak_net": round(peak_net, 2),
                "max_dd": round(max_dd, 2),
                "floating": round(floating, 2),
                "resilience": round(resilience, 2),
                "trades": trades,
                "velocity_ratio": round(velocity_ratio, 2),
                "samsonov_score": round(samsonov_full, 2),
                "samsonov_base": round(samsonov_base, 2),
                "pain_usdt": round(pain_usdt, 2),
                "pain_ratio": round(pain_ratio, 2),
                "exhaustion_index": round(exhaustion_index, 2),
            })

        return records

    def get_samsonov_top(
        self,
        universe_id: str = "u0_fat_swing123",
        top_n: int = 20,
        w_resilience: float = 1.0,
        w_realized: float = 1.0,
        w_velocity: float = 1.0,
    ) -> List[Dict[str, Any]]:
        records = self.calculate_coin_metrics(
            universe_id=universe_id,
            w_resilience=w_resilience,
            w_realized=w_realized,
            w_velocity=w_velocity,
        )
        records.sort(key=lambda x: x["samsonov_score"], reverse=True)
        return records[:top_n]

    def get_exhaustion_top(
        self,
        universe_id: str = "u0_fat_swing123",
        top_n: int = 10,
    ) -> List[Dict[str, Any]]:
        records = self.calculate_coin_metrics(universe_id=universe_id)
        records.sort(key=lambda x: x["pain_usdt"], reverse=True)
        return records[:top_n]

    def select_top_coins(
        self,
        universe_id: str = "u0_fat_swing123",
        target_count: int = 4,
    ) -> List[Dict[str, Any]]:
        """Основной интерфейс: возвращает целевой список ТОП-N монет по Воронке Самсонова."""
        funnel = self.get_funnel_stages(universe_id=universe_id, target_count=target_count)
        return funnel["final_targets"]


# Алиас для обратной совместимости
SamsonovSelector = SamsonovFunnelSelector


# ==============================================================================
# CLI И СРАВНИТЕЛЬНАЯ ДЕМОНСТРАЦИЯ ОБОИХ АЛГОРИТМОВ
# ==============================================================================

def print_comparison_cli(universe_id: str = "u0_fat_swing123", target_count: int = 4):
    print("=" * 115)
    print(f"КВАНТОВЫЙ БАТТЛ СЕЛЕКЦИИ: АЛГОРИТМ АГЕНТА VS АЛГОРИТМ САМСОНОВА (ЦЕЛЬ: {target_count} МОНЕТЫ)")
    print("=" * 115)

    samsonov_selector = SamsonovFunnelSelector()
    radar_data = samsonov_selector.load_universe_data(universe_id)
    radar_coins = list(radar_data["per_coin"].keys()) if (radar_data and "per_coin" in radar_data) else []

    # 1. АЛГОРИТМ АГЕНТА (Весь рынок)
    agent_selector = AgentMarketSelector(min_range_pct=20.0, min_volume_usdt=90_000_000.0)
    agent_all = agent_selector.select_top_coins_sync(target_count=target_count)

    print(f"\n>>> 1. АЛГОРИТМ АГЕНТА (Рыночный сканер Binance Futures: Весь рынок, Размах >=20%, Объем >=$90M):")
    print(f"{'#':<3} {'Символ':<12} {'Размах 24h':<14} {'Сут. Изм %':<14} {'Сут. Объем USDT':<20} {'Last Price'}")
    print("-" * 115)
    if agent_all:
        for i, c in enumerate(agent_all, 1):
            print(f"#{i:<2} {c['symbol']:<12} {c['range_pct']:>6.2f} %       {c['price_change_pct']:>+6.2f} %       ${c['volume_m']:>8.1f} M        {c['last_price']}")
    else:
        print("  [!] Не удалось получить данные от биржи Binance.")
    print("-" * 115)

    # 1.2 АЛГОРИТМ АГЕНТА (Внутри пула 76 монет Радара)
    if radar_coins:
        agent_radar = agent_selector.select_top_coins_sync(target_count=target_count, universe_filter=radar_coins)
        print(f"\n>>> 1.2 АЛГОРИТМ АГЕНТА (Тот же сканер волатильности, примененный к пулу 76 монет Радара):")
        print(f"{'#':<3} {'Символ':<12} {'Размах 24h':<14} {'Сут. Изм %':<14} {'Сут. Объем USDT':<20} {'Last Price'}")
        print("-" * 115)
        for i, c in enumerate(agent_radar, 1):
            print(f"#{i:<2} {c['symbol']:<12} {c['range_pct']:>6.2f} %       {c['price_change_pct']:>+6.2f} %       ${c['volume_m']:>8.1f} M        {c['last_price']}")
        print("-" * 115)

    # 2. АЛГОРИТМ САМСОНОВА
    funnel = samsonov_selector.get_funnel_stages(universe_id=universe_id, target_count=target_count)
    s_coins = funnel["final_targets"]

    print(f"\n>>> 2. АЛГОРИТМ САМСОНОВА (3-ступенчатая Воронка Радара на Вселенной [{universe_id}]):")
    print(f"  * Шаг 1: Realized PnL 50% -> Шаг 2: Peak/DD 50% -> Шаг 3: Trade Velocity Top-{target_count}")
    print(f"{'#':<3} {'Символ':<12} {'Сделок':<8} {'Realized PnL':<15} {'Peak Net':<12} {'Max DD':<12} {'Peak/DD':<10} {'Net Profit':<12} {'Floating'}")
    print("-" * 115)
    for i, c in enumerate(s_coins, 1):
        print(f"#{i:<2} {c['symbol']:<12} {c['trades']:<8} {c['realized_net']:>+8.2f} $      {c['peak_net']:>+7.2f} $    {c['max_dd']:>6.2f} $     {c['resilience']:>5.2f}      {c['net_profit']:>+8.2f} $   {c['floating']:>+7.2f} $")
    print("-" * 115)


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "u0_fat_swing123"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    print_comparison_cli(u, n)
