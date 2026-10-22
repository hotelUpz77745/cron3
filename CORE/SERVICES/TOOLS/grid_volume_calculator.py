# CORE/SERVICES/TOOLS/grid_volume_calculator.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.TOOLS.grid_volume_calculator
ROLE: Квантовый сервис расчёта и валидации объёмов сетки (Geometric Progression Grid Calculator & Auditor)
==============================================================================
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Fix Windows console encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class GridVolumeCalculator:
    """
    Институциональный калькулятор и аудитор геометрической прогрессии объемов сеток.
    Рассчитывает веса уровней V_k = V_0 * multiplier^k с гарантированным сохранением суммы 100.00%.
    """

    def __init__(self, default_multiplier: float = 1.1, default_total_pct: float = 100.0, round_digits: int = 2):
        self.default_multiplier = default_multiplier
        self.default_total_pct = default_total_pct
        self.round_digits = round_digits

    def calculate(
        self,
        levels: int,
        multiplier: Optional[float] = None,
        total_pct: Optional[float] = None,
        round_digits: Optional[int] = None,
    ) -> Tuple[List[float], float]:
        """
        Расчет объемов сетки по геометрической прогрессии:
        V_k = V_0 * multiplier^k
        Сумма(k=0..N-1) V_k = total_pct (100.0%)

        Сумма геометрической прогрессии:
        S_N = V_0 * (multiplier^N - 1) / (multiplier - 1) = total_pct
        => V_0 = total_pct * (multiplier - 1) / (multiplier^N - 1)
        """
        mult = multiplier if multiplier is not None else self.default_multiplier
        tot = total_pct if total_pct is not None else self.default_total_pct
        r_digits = round_digits if round_digits is not None else self.round_digits

        if levels <= 0:
            raise ValueError("Количество уровней должно быть > 0")

        if abs(mult - 1.0) < 1e-9:
            v0 = tot / levels
            raw_volumes = [v0] * levels
        else:
            v0 = tot * (mult - 1.0) / (math.pow(mult, levels) - 1.0)
            raw_volumes = [v0 * math.pow(mult, k) for k in range(levels)]

        # Округление с сохранением точной суммы 100.00%
        rounded_volumes = [round(v, r_digits) for v in raw_volumes]
        current_sum = round(sum(rounded_volumes), r_digits)
        diff = round(tot - current_sum, r_digits)

        # Корректируем последний уровень на остаточную дельту округления (обычно 0.01% или 0.00%)
        if abs(diff) > 0.0:
            rounded_volumes[-1] = round(rounded_volumes[-1] + diff, r_digits)

        return rounded_volumes, v0

    def format_table(
        self,
        levels: int,
        multiplier: float,
        volumes: List[float],
        invest_size: Optional[float] = None,
    ) -> str:
        """Форматирует строковую таблицу распределения объемов и инвестиций."""
        lines = []
        lines.append("=" * 70)
        lines.append(f"РАСЧЁТ СЕТКИ ОБЪЁМОВ (Levels: {levels}, Multiplier: {multiplier})")
        lines.append("=" * 70)
        header = f"{'Level':<8} | {'Volume %':<12}"
        if invest_size is not None:
            header += f" | {'Invest USDT':<14} | {'Cumulative USDT':<16}"
        lines.append(header)
        lines.append("-" * len(header))

        cum_usdt = 0.0
        for i, vol in enumerate(volumes):
            row = f"L{i:<7} | {vol:>6.2f}%     "
            if invest_size is not None:
                amt = invest_size * (vol / 100.0)
                cum_usdt += amt
                row += f" | {amt:>10.2f} USDT | {cum_usdt:>12.2f} USDT"
            lines.append(row)

        lines.append("-" * len(header))
        summary = f"{'ИТОГО':<8} | {sum(volumes):>6.2f}%     "
        if invest_size is not None:
            summary += f" | {invest_size:>10.2f} USDT | {cum_usdt:>12.2f} USDT"
        lines.append(summary)
        lines.append("=" * 70)
        return "\n".join(lines)

    def patch_file(
        self,
        filepath: Path,
        levels: int,
        multiplier: float,
        target_side: Optional[str] = None,
    ) -> bool:
        """Обновляет объемы сетки в файле конфигурации вселенной."""
        volumes, _ = self.calculate(levels=levels, multiplier=multiplier)

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        sides_to_patch = [target_side.upper()] if target_side else ["LONG", "SHORT"]

        for side in sides_to_patch:
            if side not in data or "grid" not in data[side]:
                continue

            grid = data[side]["grid"]
            for i in range(min(levels, len(grid))):
                key = str(i)
                if key in grid:
                    grid[key]["volume"] = volumes[i]

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        print(f"[OK] Конфиг {filepath.name} успешно обновлен для сторон: {sides_to_patch}")
        return True


# Модульные функции для обратной совместимости
_default_calc = GridVolumeCalculator()


def calculate_grid_volumes(
    levels: int,
    multiplier: float = 1.1,
    total_pct: float = 100.0,
    round_digits: int = 2,
) -> Tuple[List[float], float]:
    return _default_calc.calculate(levels, multiplier, total_pct, round_digits)


def print_grid_table(
    levels: int,
    multiplier: float,
    volumes: List[float],
    invest_size: Optional[float] = None,
) -> None:
    table_str = _default_calc.format_table(levels, multiplier, volumes, invest_size)
    print(table_str)


def patch_universe_file(
    filepath: Path,
    levels: int,
    multiplier: float,
    target_side: Optional[str] = None,
) -> None:
    _default_calc.patch_file(filepath, levels, multiplier, target_side)


def main():
    parser = argparse.ArgumentParser(
        description="Квантовый калькулятор и аудитор геометрической прогрессии сеток HRON_3"
    )
    parser.add_argument("--levels", "-l", type=int, default=7, help="Количество уровней сетки (default: 7)")
    parser.add_argument("--mult", "-m", type=float, default=1.1, help="Множитель геометрической прогрессии (default: 1.1)")
    parser.add_argument("--invest", "-i", type=float, default=None, help="Базовый размер инвестиции в USDT для расчета сумм")
    parser.add_argument("--patch", "-p", type=str, default=None, help="Путь к файлу конфигурации вселенной (*.json) для патча")
    parser.add_argument("--side", "-s", type=str, default=None, help="Целевая сторона для патча: LONG или SHORT (default: обе)")

    args = parser.parse_args()

    vols, v0 = calculate_grid_volumes(levels=args.levels, multiplier=args.mult)
    print_grid_table(levels=args.levels, multiplier=args.mult, volumes=vols, invest_size=args.invest)

    if args.patch:
        path = Path(args.patch)
        if not path.exists():
            print(f"[ERR] Файл не найден: {path}")
            sys.exit(1)
        patch_universe_file(path, levels=args.levels, multiplier=args.mult, target_side=args.side)


if __name__ == "__main__":
    main()
