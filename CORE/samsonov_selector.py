# ============================================================
# FILE: CORE/samsonov_selector.py
# ROLE: Multi-factor quant coin selector (Samsonov Score & Exhaustion Alpha bridge)
# ============================================================

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from consts import BASE_DIR, DATA_DIR, PARENT_DIR, _CFG
from c_log import UnifiedLogger

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = UnifiedLogger("SamsonovSelector")



class SamsonovSelector:
    """
    Квантовый селектор монет Тим Лида Самсонова Николая.
    Связующий мост («Штурман») между исследовательской Фабрикой Звёзд (cron3PapperUniverses)
    и боевым торговым ядром (cron3).
    
    Реализует две фундаментальные теории:
    1. Теория 2: Квантовый Скор Самсонова (Resilience^w_r * Realized^w_m * Velocity^w_v).
    2. Теория 1: Exhaustion Alpha (Индекс истощения продавцов на донных просадках).
    """

    def __init__(self, radar_stats_dir: Optional[Path] = None):
        if radar_stats_dir:
            self.radar_stats_dir = Path(radar_stats_dir)
        else:
            # Читаем из app.json -> paths -> radar_dir или берем дефолтный соседний каталог
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
            logger.warning(f"Radar directory not found: {self.radar_stats_dir}")
            return None
        
        file_path = self.radar_stats_dir / f"analytics_{universe_id}.json"
        if not file_path.exists():
            logger.warning(f"Universe stats file not found: {file_path}")
            return None
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read universe stats {file_path}: {e}")
            return None

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

            # 1. Множитель асимметрии риска (Historical Resilience)
            dd_denom = max(max_dd, 0.5)
            peak_clamped = max(peak_net, 0.0)
            resilience = peak_clamped / dd_denom

            # 2. Множитель монетизации
            realized_pos = max(realized_net, 0.0)

            # 3. Множитель частоты оборота (Trade Velocity Ratio)
            velocity_ratio = trades / avg_trades if avg_trades > 0 else 1.0

            # --- СКОР САМСОНОВА ---
            # Full (с весами и Velocity)
            samsonov_full = (resilience ** w_resilience) * (realized_pos ** w_realized) * (velocity_ratio ** w_velocity)
            # Base (без Velocity)
            samsonov_base = (resilience ** w_resilience) * (realized_pos ** w_realized)

            # --- EXHAUSTION ALPHA (ТЕОРИЯ 1) ---
            pain_usdt = abs(floating) if floating < 0 else 0.0
            pain_ratio = pain_usdt / dd_denom

            # Exhaustion Index (по умолчанию веса 1.0)
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

    def run_samsonov_funnel(
        self,
        universe_id: str = "u0_fat_swing123",
        target_count: int = 4,
    ) -> Dict[str, Any]:
        """
        3-ступенчатый квантовый алгоритм фильтрации (Воронка Самсонова):
        Шаг 1: Ранжируем все монеты по Realized PnL (net fee) и берем срез ТОП-50%.
        Шаг 2: Оставшийся срез ранжируем по Peak Net / Max DD и берем срез ТОП-50%.
        Шаг 3: Оставшийся срез ранжируем по Trade Velocity (trades) и берем target_count монет.
        """
        records = self.calculate_coin_metrics(universe_id=universe_id)
        if not records:
            return {"step1_pool": [], "step2_pool": [], "final_targets": []}

        total_input = len(records)

        # ШАГ 1: Топ-50% по Realized PnL (монетизация в кассу)
        step1_sorted = sorted(records, key=lambda x: x["realized_net"], reverse=True)
        half1 = len(step1_sorted) // 2
        step1_pool = step1_sorted[:half1]

        # ШАГ 2: Топ-50% по Resilience (Peak Net / Max DD - асимметрия риска)
        step2_sorted = sorted(step1_pool, key=lambda x: x["resilience"], reverse=True)
        half2 = len(step2_sorted) // 2
        step2_pool = step2_sorted[:half2]

        # ШАГ 3: Ранжирование по Trade Velocity (скорость сделок) -> целевой список
        step3_sorted = sorted(step2_pool, key=lambda x: x["trades"], reverse=True)
        final_targets = step3_sorted[:target_count]

        return {
            "total_input": total_input,
            "step1_pool": step1_pool,
            "step2_pool": step2_pool,
            "step3_pool": step3_sorted,
            "final_targets": final_targets,
        }


def print_cli_summary(universe_id: str = "u0_fat_swing123", target_count: int = 4):
    selector = SamsonovSelector()
    if not selector.is_radar_available():
        print(f"Error: Radar directory not found at {selector.radar_stats_dir}")
        return

    print("=" * 115)
    print(f"КВАНТОВЫЙ СЕЛЕКТОР САМСОНОВА — РЕЗУЛЬТАТЫ СРЕЗА [Вселенная: {universe_id}]")
    print("=" * 115)

    # 1. АЛГОРИТМ ВОРОНКИ САМСОНОВА (3 ШАГА) — ПЕРВЫЙ И ГЛАВНЫЙ МЕХАНИЗМ
    funnel = selector.run_samsonov_funnel(universe_id=universe_id, target_count=target_count)
    s1 = funnel["step1_pool"]
    s2 = funnel["step2_pool"]
    targets = funnel["final_targets"]

    print(f"\n>>> [АЛГОРИТМ ВОРОНКИ САМСОНОВА — 3 ЭТАПА ФИЛЬТРАЦИИ]:")
    print(f"  * Входной пул радара: {funnel['total_input']} монет")
    if s1:
        print(f"  * ШАГ 1 (Фильтр Монетизации / Realized PnL): {len(s1)} монет (порог: {s1[-1]['realized_net']:+.2f}$..{s1[0]['realized_net']:+.2f}$)")
    if s2:
        print(f"  * ШАГ 2 (Фильтр Качества / Peak Net/Max DD): {len(s2)} монет (порог коэф.: {s2[-1]['resilience']:.2f}..{s2[0]['resilience']:.2f})")
    print(f"  * ШАГ 3 (Фильтр Пульса / Trade Velocity): отобрано ТОП-{target_count} целевых монет:")
    
    print("-" * 115)
    print(f"{'#':<3} {'Символ':<12} {'Сделок':<8} {'Realized PnL':<15} {'Peak Net':<12} {'Max DD':<12} {'Peak/DD':<10} {'Net Profit':<12} {'Floating'}")
    print("-" * 115)
    for rank, c in enumerate(targets, 1):
        print(f"#{rank:<2} {c['symbol']:<12} {c['trades']:<8} {c['realized_net']:>+8.2f} $      {c['peak_net']:>+7.2f} $    {c['max_dd']:>6.2f} $     {c['resilience']:>5.2f}      {c['net_profit']:>+8.2f} $   {c['floating']:>+7.2f} $")
    print("-" * 115)

    # 2. TOP-10 Exhaustion Alpha (Полезная эвристическая идея контртренда)
    top_ex = selector.get_exhaustion_top(universe_id, top_n=10)
    print("\n--- [ЭВРИСТИКА EXHAUSTION ALPHA: Поиск донного истощения продавцов] ---")
    print(f"{'#':<3} {'Символ':<12} {'Боль USDT':<12} {'MaxDD':<8} {'Pain/DD':<10} {'Realized':<10} {'NetProfit':<10} {'Сделок'}")
    for i, r in enumerate(top_ex, 1):
        print(f"{i:<3} {r['symbol']:<12} {r['pain_usdt']:<12.2f} {r['max_dd']:<8.2f} {r['pain_ratio']:<10.2f} {r['realized_net']:<10.2f} {r['net_profit']:<10.2f} {r['trades']}")


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "u0_fat_swing123"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    print_cli_summary(u, n)

