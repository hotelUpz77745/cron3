# ANALYTICS/strategy_toxic_defense.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.strategy_toxic_defense
ROLE: Агрегация метрик защитных механизмов (Subgrid Peeling & Cross Recycling) из леджеров
==============================================================================
"""

import csv
from pathlib import Path
from typing import Dict, Any, Tuple


class StrategyToxicDefenseAggregator:
    """
    Агрегатор статистики защитных механизмов токсичной защиты:
    - Читает peels_ledger_{strat_id}.txt и recycling_ledger_{strat_id}.txt
    - Агрегирует события, объемы и поглощенные убытки
    - Обогащает структуры strat_per_coin и формирует блок toxic_defense
    """

    @staticmethod
    def aggregate_global_defense(peels_ledger: Path, recycling_ledger: Path) -> Dict[str, Any]:
        """Агрегирует глобальные показатели токсичной защиты из портфельных леджеров."""
        g_peel_cnt, g_peel_vol, g_peel_pnl = 0, 0.0, 0.0
        if peels_ledger.exists():
            try:
                with open(peels_ledger, "r", encoding="utf-8") as pf:
                    r = csv.reader(pf, delimiter=';')
                    next(r, None)
                    for row in r:
                        if len(row) >= 12:
                            g_peel_cnt += 1
                            g_peel_vol += float(row[10])
                            g_peel_pnl += float(row[11])
            except Exception:
                pass

        g_rec_cnt, g_rec_vol, g_rec_spent, g_rec_absorbed = 0, 0.0, 0.0, 0.0
        if recycling_ledger.exists():
            try:
                with open(recycling_ledger, "r", encoding="utf-8") as rf:
                    r = csv.reader(rf, delimiter=';')
                    next(r, None)
                    for row in r:
                        if len(row) >= 13:
                            g_rec_cnt += 1
                            g_rec_vol += float(row[10])
                            g_rec_absorbed += float(row[11])
                            g_rec_spent += float(row[12])
            except Exception:
                pass

        return {
            "subgrid_peeling": {
                "events_count": g_peel_cnt,
                "total_peeled_usdt": round(g_peel_vol, 4),
                "locked_micro_pnl_usdt": round(g_peel_pnl, 4),
            },
            "cross_recycling": {
                "events_count": g_rec_cnt,
                "burned_volume_usdt": round(g_rec_vol, 4),
                "profit_spent_usdt": round(g_rec_spent, 4),
                "loss_absorbed_usdt": round(g_rec_absorbed, 4),
            },
        }

    @staticmethod
    def aggregate_strategy_defense(
        ledgers_dir: Path,
        strat_id: str,
        strat_per_coin: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Агрегирует показатели токсичной защиты для отдельной стратегии и обновляет per_coin."""
        strat_peel_txt = ledgers_dir / f"peels_ledger_{strat_id}.txt"
        strat_rec_txt = ledgers_dir / f"recycling_ledger_{strat_id}.txt"

        strat_peel_cnt, strat_peel_vol, strat_peel_pnl, strat_peel_loss = 0, 0.0, 0.0, 0.0
        c_peels, c_recs = {}, {}

        if strat_peel_txt.exists():
            try:
                with open(strat_peel_txt, "r", encoding="utf-8") as pf:
                    r = csv.reader(pf, delimiter=';')
                    next(r, None)
                    for row in r:
                        if len(row) >= 12:
                            s, v, m = row[3], float(row[10]), float(row[11])
                            l = float(row[13]) if len(row) >= 14 else m
                            strat_peel_cnt += 1
                            strat_peel_vol += v
                            strat_peel_pnl += m
                            strat_peel_loss += l
                            cp = c_peels.setdefault(s, {
                                "events_count": 0, "total_peeled_usdt": 0.0,
                                "locked_micro_pnl_usdt": 0.0, "peel_loss_usdt": 0.0
                            })
                            cp["events_count"] += 1
                            cp["total_peeled_usdt"] = round(cp["total_peeled_usdt"] + v, 4)
                            cp["locked_micro_pnl_usdt"] = round(cp["locked_micro_pnl_usdt"] + m, 4)
                            cp["peel_loss_usdt"] = round(cp["peel_loss_usdt"] + l, 4)
            except Exception:
                pass

        if strat_peel_cnt == 0:
            strat_peel_cnt = sum(c.get("peeling", {}).get("events_count", 0) for c in strat_per_coin.values())
            strat_peel_vol = sum(c.get("peeling", {}).get("total_peeled_usdt", 0.0) for c in strat_per_coin.values())
            strat_peel_pnl = sum(c.get("peeling", {}).get("locked_micro_pnl_usdt", 0.0) for c in strat_per_coin.values())
            strat_peel_loss = sum(c.get("peeling", {}).get("peel_loss_usdt", 0.0) for c in strat_per_coin.values())

        strat_rec_cnt, strat_rec_vol, strat_rec_spent, strat_rec_absorbed = 0, 0.0, 0.0, 0.0
        if strat_rec_txt.exists():
            try:
                with open(strat_rec_txt, "r", encoding="utf-8") as rf:
                    r = csv.reader(rf, delimiter=';')
                    next(r, None)
                    for row in r:
                        if len(row) >= 13:
                            s, v, l, sp = row[3], float(row[10]), float(row[11]), float(row[12])
                            strat_rec_cnt += 1
                            strat_rec_vol += v
                            strat_rec_absorbed += l
                            strat_rec_spent += sp
                            cr = c_recs.setdefault(s, {
                                "events_count": 0, "burned_volume_usdt": 0.0,
                                "profit_spent_usdt": 0.0, "loss_absorbed_usdt": 0.0
                            })
                            cr["events_count"] += 1
                            cr["burned_volume_usdt"] = round(cr["burned_volume_usdt"] + v, 4)
                            cr["loss_absorbed_usdt"] = round(cr["loss_absorbed_usdt"] + l, 4)
                            cr["profit_spent_usdt"] = round(cr["profit_spent_usdt"] + sp, 4)
            except Exception:
                pass

        if strat_rec_cnt == 0:
            strat_rec_cnt = sum(c.get("cross_recycling", {}).get("events_count", 0) for c in strat_per_coin.values())
            strat_rec_vol = sum(c.get("cross_recycling", {}).get("burned_volume_usdt", 0.0) for c in strat_per_coin.values())
            strat_rec_spent = sum(c.get("cross_recycling", {}).get("profit_spent_usdt", 0.0) for c in strat_per_coin.values())
            strat_rec_absorbed = sum(c.get("cross_recycling", {}).get("loss_absorbed_usdt", 0.0) for c in strat_per_coin.values())

        for sym, cd in strat_per_coin.items():
            if sym in c_peels:
                cd["peeling"] = c_peels[sym]
            if sym in c_recs:
                cd["cross_recycling"] = c_recs[sym]
            pl = abs(min(0.0, float(cd.get("peeling", {}).get("peel_loss_usdt", 0.0))))
            rl = abs(float(cd.get("cross_recycling", {}).get("loss_absorbed_usdt", cd.get("cross_recycling", {}).get("profit_spent_usdt", 0.0))))
            r_net = float(cd.get("realized_pnl_net_usdt", cd.get("realized_pnl_usdt", 0.0)))
            cd["gross_realized_usdt"] = round(
                r_net + pl + rl + abs(float(cd.get("commission_usdt", 0.0))) - float(cd.get("funding_usdt", 0.0)),
                4
            )

        return {
            "subgrid_peeling": {
                "events_count": strat_peel_cnt,
                "total_peeled_usdt": round(strat_peel_vol, 4),
                "peel_loss_usdt": round(strat_peel_loss, 4),
                "locked_micro_pnl_usdt": round(strat_peel_pnl, 4)
            },
            "cross_recycling": {
                "events_count": strat_rec_cnt,
                "burned_volume_usdt": round(strat_rec_vol, 4),
                "profit_spent_usdt": round(strat_rec_spent, 4),
                "loss_absorbed_usdt": round(strat_rec_absorbed, 4)
            },
        }
