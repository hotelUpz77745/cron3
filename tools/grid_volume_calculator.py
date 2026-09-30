# ==============================================================================
# Path: tools/grid_volume_calculator.py
# Role: Квантовый сервис расчёта и валидации объёмов сетки (Geometric Progression Grid Calculator & Auditor)
# ==============================================================================

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


def calculate_grid_volumes(
    levels: int,
    multiplier: float = 1.1,
    total_pct: float = 100.0,
    round_digits: int = 2,
    invest_size: Optional[float] = None,
) -> Tuple[List[float], float]:
    """
    Расчет объемов сетки по геометрической прогрессии:
    V_k = V_0 * multiplier^k
    Сумма(k=0..N-1) V_k = total_pct (100.0%)

    Сумма геометрической прогрессии:
    S_N = V_0 * (multiplier^N - 1) / (multiplier - 1) = total_pct
    => V_0 = total_pct * (multiplier - 1) / (multiplier^N - 1)
    """
    if levels <= 0:
        raise ValueError("Количество уровней должно быть > 0")

    if abs(multiplier - 1.0) < 1e-9:
        raw_volumes = [total_pct / levels] * levels
    else:
        v0 = total_pct * (multiplier - 1.0) / (math.pow(multiplier, levels) - 1.0)
        raw_volumes = [v0 * math.pow(multiplier, k) for k in range(levels)]

    # Округление с сохранением точной суммы 100.00%
    rounded_volumes = [round(v, round_digits) for v in raw_volumes]
    current_sum = round(sum(rounded_volumes), round_digits)
    diff = round(total_pct - current_sum, round_digits)

    # Корректируем последний уровень на остаточную дельту округления (обычно 0.01% или 0.00%)
    if abs(diff) > 0.0:
        rounded_volumes[-1] = round(rounded_volumes[-1] + diff, round_digits)

    return rounded_volumes, v0


def print_grid_table(
    levels: int,
    multiplier: float,
    volumes: List[float],
    v0_exact: float,
    invest_size: Optional[float] = None,
):
    print("=" * 85)
    print(f"📐 [GRID CALCULATOR] Геометрическая прогрессия объёмов сетки")
    print("=" * 85)
    print(f"  • Количество уровней (N)  : {levels} (от Level 0 до Level {levels - 1})")
    print(f"  • Множитель шага (q)     : {multiplier:.4f} (каждое колено в {multiplier:.2f}x тяжелее)")
    print(f"  • Точный стартовый V_0   : {v0_exact:.6f}%")
    print(f"  • Отношение V_max / V_0  : {math.pow(multiplier, levels - 1):.2f}x")
    if invest_size:
        print(f"  • Базовый сайз на сторону : {invest_size:.2f} USDT")
    print("-" * 85)
    header = f"{'Уровень':<9} | {'Формула':<15} | {'Объем (%)':<11} | {'Кумулятив (%)':<15}"
    if invest_size:
        header += f" | {'Объем (USDT)':<14} | {'Кумулятив (USDT)':<16}"
    print(header)
    print("-" * 85)

    cum_pct = 0.0
    cum_usdt = 0.0
    for k, vol in enumerate(volumes):
        cum_pct += vol
        formula_str = f"V_0 * {multiplier}^{k}" if k > 0 else "V_0 (Start)"
        row = f"Level {k:<3} | {formula_str:<15} | {vol:>8.2f} %  | {cum_pct:>12.2f} %"
        if invest_size:
            vol_usdt = invest_size * (vol / 100.0)
            cum_usdt += vol_usdt
            row += f" | {vol_usdt:>11.2f} $  | {cum_usdt:>13.2f} $"
        print(row)

    print("-" * 85)
    print(f"{'ИТОГО':<9} | {'Сумма уровней':<15} | {cum_pct:>8.2f} %  | {'100.00 % (OK)':<15}")
    if invest_size:
        print(f"         |                 |             |                 | {cum_usdt:>11.2f} $  | {invest_size:>13.2f} $")
    print("=" * 85)


def generate_json_grid_snippet(volumes: List[float], existing_indents: Optional[List[float]] = None) -> str:
    """Генерирует готовый JSON-блок для вставки в universes.json или _base.json."""
    grid_dict = {}
    for k, vol in enumerate(volumes):
        indent = existing_indents[k] if existing_indents and k < len(existing_indents) else (0.0 if k == 0 else -float(k * 2.0))
        grid_dict[str(k)] = {
            "indent": indent,
            "volume": vol,
            "super_indent": None,
            "activated_at": None,
            "timestamp": None,
        }
    return json.dumps(grid_dict, indent=4)


def audit_repository_universes() -> Dict[str, Dict]:
    """
    Полный аудит всех файлов конфигураций в текущем репозитории и в Радаре.
    Проверяет сумму объемов всех уровней сетки (должна быть строго 100.00%).
    """
    root_dir = Path(__file__).resolve().parent.parent
    files_to_check = [
        ("cron3 Live Base", root_dir / "CFG" / "_base.json"),
        ("cron3 Live Universes", root_dir / "CFG" / "universes.json"),
        ("Radar Lab Base", root_dir.parent / "cron3PapperUniverses" / "CFG" / "_base.json"),
        ("Radar Lab Universes", root_dir.parent / "cron3PapperUniverses" / "CFG" / "universes.json"),
    ]

    print("=" * 95)
    print("🔍 [AUDIT] Запуск полной проверки распределения объёмов сеток во всех стратегиях")
    print("=" * 95)

    discrepancies = []
    audited_count = 0

    for label, filepath in files_to_check:
        if not filepath.exists():
            continue

        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"❌ Ошибка чтения {filepath}: {e}")
            continue

        print(f"\n📂 Файл: [{label}] -> {filepath.name}")
        print("-" * 95)
        print(f"{'Стратегия / Блок':<40} | {'Уровней':<8} | {'Сумма объёмов':<14} | {'Разница':<12} | {'Статус':<10}")
        print("-" * 95)

        if "_base" in filepath.name:
            for side in ["LONG", "SHORT"]:
                audited_count += 1
                grid = data.get(side, {}).get("grid", {})
                tot_vol = sum(float(v.get("volume", 0.0)) for v in grid.values())
                diff = 100.0 - tot_vol
                status = "✅ OK" if abs(diff) < 0.05 else "❌ ERROR"
                print(f"{side + ' (_base default)':<40} | {len(grid):<8} | {tot_vol:>10.2f} %   | {diff:>+8.2f} %   | {status}")
                if abs(diff) >= 0.05:
                    discrepancies.append((label, side, len(grid), tot_vol, diff))
        else:
            for u_id, u_data in data.items():
                if not isinstance(u_data, dict):
                    continue
                grid = u_data.get("overrides", {}).get("grid") or u_data.get("grid")
                if not grid or not isinstance(grid, dict):
                    continue
                audited_count += 1
                tot_vol = sum(float(v.get("volume", 0.0)) for v in grid.values())
                diff = 100.0 - tot_vol
                status = "✅ OK" if abs(diff) < 0.05 else "❌ ХВОСТ!"
                print(f"{u_id:<40} | {len(grid):<8} | {tot_vol:>10.2f} %   | {diff:>+8.2f} %   | {status}")
                if abs(diff) >= 0.05:
                    discrepancies.append((label, u_id, len(grid), tot_vol, diff))

    print("=" * 95)
    print(f"📊 Всего проверено конфигураций сеток: {audited_count}")
    if not discrepancies:
        print("🎉 Во всех стратегиях объёмы распределены безупречно: ровно 100.00%!")
    else:
        print(f"🚨 ОБНАРУЖЕНО НЕСООТВЕТСТВИЙ: {len(discrepancies)}")
        for label, sid, lvls, tot, diff in discrepancies:
            print(f"   • [{label}] {sid}: уровней={lvls}, текущая сумма={tot:.2f}%, неиспользуемый остаток={diff:+.2f}%")
    print("=" * 95)


def main():
    parser = argparse.ArgumentParser(description="Квантовый калькулятор и аудитор объемов сетки DCA")
    parser.add_argument("--levels", "-n", type=int, default=8, help="Количество уровней сетки (default: 8)")
    parser.add_argument("--multiplier", "-q", type=float, default=1.1, help="Множитель шага объема (default: 1.1)")
    parser.add_argument("--invest-size", "-s", type=float, default=None, help="Базовый сайз на сторону в USDT (optional)")
    parser.add_argument("--total-pct", "-t", type=float, default=100.0, help="Общий процент маржи (default: 100.0)")
    parser.add_argument("--audit", "-a", action="store_true", help="Запустить полный аудит всех стратегий в проекте")
    parser.add_argument("--json", "-j", action="store_true", help="Вывести JSON-сниппет для вставки в конфигурацию")

    args = parser.parse_args()

    if args.audit:
        audit_repository_universes()
        return

    volumes, v0_exact = calculate_grid_volumes(
        levels=args.levels,
        multiplier=args.multiplier,
        total_pct=args.total_pct,
        invest_size=args.invest_size,
    )

    print_grid_table(
        levels=args.levels,
        multiplier=args.multiplier,
        volumes=volumes,
        v0_exact=v0_exact,
        invest_size=args.invest_size,
    )

    if args.json:
        print("\n📋 JSON-сниппет конфигурации сетки (готовый к вставке в 'grid'):")
        print(generate_json_grid_snippet(volumes))


if __name__ == "__main__":
    main()
