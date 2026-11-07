# ==============================================================================
# Path: CORE/SERVICES/trailing_ratchet_analytics.py
# Role: Сервис сбора, анализа и визуализации распределения уровней срабатывания
#       трейлинг автозакрытия (Trailing Ratchet Fibonacci Staircase).
# ==============================================================================

from collections import defaultdict
from typing import Dict, List, Any, Optional
import copy


class TrailingRatchetAnalytics:
    """
    Институциональный сервис трекинга и аналитики срабатываний Trailing Ratchet.
    
    Фиксирует:
    - Индекс и наименование уровня Фибо-лестницы (L0, L1, L2, L3, L4, L5...)
    - Долю (%) каждого уровня в общем пуле автозакрытий купола
    - Чистый Net PnL, гарантированный пол (floor), пиковый Net (highest_net)
    - Время жизни итерации до закрытия (iter_age_hours)
    - Форматирование сводной ASCII/Markdown таблицы для бэктестов и Telegram
    """

    def __init__(self, strategy_id: str = ""):
        self.strategy_id = strategy_id
        self.events: List[Dict[str, Any]] = []
        self.level_counts: Dict[str, int] = defaultdict(int)
        self.level_net_sums: Dict[str, float] = defaultdict(float)
        self.level_net_mins: Dict[str, float] = {}
        self.level_net_maxs: Dict[str, float] = {}
        self.level_ages: Dict[str, List[float]] = defaultdict(list)
        self.level_descriptions: Dict[str, str] = {}

    def record_close(
        self,
        level_idx: int,
        level_desc: str,
        armed_floor: float,
        iter_net: float,
        highest_net: float,
        iter_age_hours: float = 0.0,
        strategy_id: Optional[str] = None,
        info: str = "",
        timestamp: float = 0.0,
    ) -> Dict[str, Any]:
        """Регистрирует единичное событие автозакрытия купола по трейлингу."""
        lvl_key = f"L{int(level_idx)}"
        clean_desc = level_desc.strip() if level_desc else lvl_key

        event = {
            "strategy_id": strategy_id or self.strategy_id,
            "level_idx": int(level_idx),
            "level_key": lvl_key,
            "level_desc": clean_desc,
            "armed_floor": round(float(armed_floor), 2),
            "iter_net": round(float(iter_net), 2),
            "highest_net": round(float(highest_net), 2),
            "iter_age_hours": round(float(iter_age_hours), 2),
            "info": info,
            "timestamp": timestamp,
        }
        self.events.append(event)

        # Обновление счетчиков
        self.level_counts[lvl_key] += 1
        self.level_net_sums[lvl_key] += float(iter_net)
        self.level_descriptions[lvl_key] = clean_desc
        self.level_ages[lvl_key].append(float(iter_age_hours))

        if lvl_key not in self.level_net_mins or iter_net < self.level_net_mins[lvl_key]:
            self.level_net_mins[lvl_key] = round(float(iter_net), 2)
        if lvl_key not in self.level_net_maxs or iter_net > self.level_net_maxs[lvl_key]:
            self.level_net_maxs[lvl_key] = round(float(iter_net), 2)

        return event

    def get_summary(self) -> Dict[str, Any]:
        """Возвращает агрегированную структуру распределения уровней."""
        total_closes = len(self.events)
        levels_sorted = sorted(
            self.level_counts.keys(),
            key=lambda k: int(k[1:]) if k.startswith("L") and k[1:].isdigit() else 999
        )

        distribution: Dict[str, Dict[str, Any]] = {}
        for lvl in levels_sorted:
            cnt = self.level_counts[lvl]
            pct = (cnt / total_closes * 100.0) if total_closes > 0 else 0.0
            net_sum = self.level_net_sums[lvl]
            avg_net = round(net_sum / cnt, 2) if cnt > 0 else 0.0
            ages = self.level_ages[lvl]
            avg_age = round(sum(ages) / len(ages), 2) if ages else 0.0

            distribution[lvl] = {
                "count": cnt,
                "percentage": round(pct, 1),
                "desc": self.level_descriptions.get(lvl, lvl),
                "avg_net": avg_net,
                "min_net": self.level_net_mins.get(lvl, 0.0),
                "max_net": self.level_net_maxs.get(lvl, 0.0),
                "avg_age_hours": avg_age,
            }

        most_frequent = None
        if distribution:
            most_frequent = max(distribution.keys(), key=lambda k: distribution[k]["count"])

        return {
            "strategy_id": self.strategy_id,
            "total_closes": total_closes,
            "most_frequent_level": most_frequent,
            "distribution": distribution,
            "raw_events_count": len(self.events),
        }

    def merge(self, other: Any) -> "TrailingRatchetAnalytics":
        """Сливает статистику из другого экземпляра или словаря снапшота."""
        if isinstance(other, dict):
            # Слияние из сериализованного словаря
            dist = other.get("distribution", {})
            for lvl_key, data in dist.items():
                cnt = data.get("count", 0)
                if cnt <= 0:
                    continue
                self.level_counts[lvl_key] += cnt
                avg_net = data.get("avg_net", 0.0)
                self.level_net_sums[lvl_key] += avg_net * cnt
                self.level_descriptions[lvl_key] = data.get("desc", lvl_key)
                
                min_n = data.get("min_net", 0.0)
                max_n = data.get("max_net", 0.0)
                if lvl_key not in self.level_net_mins or min_n < self.level_net_mins[lvl_key]:
                    self.level_net_mins[lvl_key] = min_n
                if lvl_key not in self.level_net_maxs or max_n > self.level_net_maxs[lvl_key]:
                    self.level_net_maxs[lvl_key] = max_n

                avg_age = data.get("avg_age_hours", 0.0)
                self.level_ages[lvl_key].extend([avg_age] * cnt)

            # Если есть сырые события, добавляем их
            for ev in other.get("events", []):
                self.events.append(ev)
            if not self.events and other.get("total_closes", 0) > 0:
                # Фиктивные заглушки для консистентности len(events)
                for _ in range(other.get("total_closes", 0)):
                    self.events.append({})
        elif isinstance(other, TrailingRatchetAnalytics):
            for ev in other.events:
                self.events.append(ev)
            for lvl_key, cnt in other.level_counts.items():
                self.level_counts[lvl_key] += cnt
                self.level_net_sums[lvl_key] += other.level_net_sums[lvl_key]
                self.level_descriptions[lvl_key] = other.level_descriptions.get(lvl_key, lvl_key)
                self.level_ages[lvl_key].extend(other.level_ages[lvl_key])

                other_min = other.level_net_mins.get(lvl_key, 0.0)
                other_max = other.level_net_maxs.get(lvl_key, 0.0)
                if lvl_key not in self.level_net_mins or other_min < self.level_net_mins[lvl_key]:
                    self.level_net_mins[lvl_key] = other_min
                if lvl_key not in self.level_net_maxs or other_max > self.level_net_maxs[lvl_key]:
                    self.level_net_maxs[lvl_key] = other_max

        return self

    def format_distribution_table(self, title: str = "РАСПРЕДЕЛЕНИЕ УРОВНЕЙ ТРЕЙЛИНГ АВТОЗАКРЫТИЯ") -> str:
        """Форматирует визуальную ASCII-таблицу распределения уровней."""
        summary = self.get_summary()
        dist = summary["distribution"]
        total = summary["total_closes"]

        if total == 0:
            return f"\n[{title}]: Автозакрытия купола не зафиксированы (0 срабатываний)\n"

        headers = ["Level", "Описание уровня", "Срабатываний", "Доля %", "Ср. Net$", "Мин/Макс Net$", "Ср. возраст"]
        rows = []

        for lvl, data in dist.items():
            cnt = data["count"]
            pct = data["percentage"]
            desc = data["desc"]
            avg_n = data["avg_net"]
            min_n = data["min_net"]
            max_n = data["max_net"]
            avg_age = data["avg_age_hours"]

            rows.append([
                lvl,
                desc,
                str(cnt),
                f"{pct:5.1f}%",
                f"{avg_n:+.2f}$",
                f"{min_n:+.2f}$ / {max_n:+.2f}$",
                f"{avg_age:4.1f} ч"
            ])

        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(val))

        def make_row(cols):
            return " | ".join(val.rjust(col_widths[i]) if i in (0, 2, 3, 4, 5, 6) else val.ljust(col_widths[i]) for i, val in enumerate(cols))

        sep = "-+-".join("-" * w for w in col_widths)
        banner = "=" * len(make_row(headers))
        title_line = f"🎯 {title.upper()} (ВСЕГО СРАБАТЫВАНИЙ: {total})"

        most_freq_str = f"Самый частый уровень: {summary['most_frequent_level']} ({dist.get(summary['most_frequent_level'], {}).get('percentage', 0.0)}%)" if summary['most_frequent_level'] else ""
        footer = f"ИТОГО: {total} автозакрытий | {most_freq_str}"

        table_lines = [
            banner,
            title_line,
            banner,
            make_row(headers),
            sep,
        ] + [make_row(r) for r in rows] + [
            sep,
            footer,
            banner
        ]
        return "\n" + "\n".join(table_lines) + "\n"

