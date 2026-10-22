# CORE/STRATEGY/auto_closing_builder.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY.auto_closing_builder
ROLE: Построение нормализованной конфигурации режимов автозакрытия (Classic, Toxic, Fixed, Off)
==============================================================================
"""

import copy
from typing import Dict, Any, Optional
from consts import _CFG


class AutoClosingConfigBuilder:
    """Формирует конфигурацию auto_closing строго для целевого режима купола."""

    @staticmethod
    def build_mode_auto_closing(mode: str, base_ac: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Формирует конфигурацию auto_closing строго для целевого режима из переданного base_ac (или app.json)."""
        mode = mode.lower().strip()
        if base_ac is None:
            base_ac = _CFG.get("auto_closing", {})
        if not base_ac or not isinstance(base_ac, dict):
            base_ac = {"positive": {}, "negative": {}}

        neg_cfg = copy.deepcopy(base_ac.get("negative") or {})
        pos_base = copy.deepcopy(base_ac.get("positive") or {})
        raw_scale_val = pos_base.get("scale_money")
        scale_money = float(raw_scale_val) if raw_scale_val is not None else None
        desc_scale_money = pos_base.get(
            "_desc_scale_money",
            "Масштаб 1.0 рассчитан на матожидание системы — 400 USDT на сторону."
        )

        auto_scale_money = pos_base.get("auto_scale_money", False)
        margin_relative = copy.deepcopy(pos_base.get("margin_relative") or {})
        mode_scale = pos_base.get("mode_scale", "inverted_notional")
        inverted_notional = copy.deepcopy(pos_base.get("inverted_notional") or {})

        if mode == "off":
            return {
                "positive": {},
                "negative": neg_cfg
            }
        elif mode == "fixed":
            fixed_net = copy.deepcopy(pos_base.get("fixed_net") or {"target_net_usdt": 15.0})
            if "target_net_usdt" not in fixed_net:
                fixed_net["target_net_usdt"] = 15.0
            pos_cfg = {
                "scale_money": scale_money,
                "_desc_scale_money": desc_scale_money,
                "auto_scale_money": auto_scale_money,
                "mode_scale": mode_scale,
                "inverted_notional": inverted_notional,
                "margin_relative": margin_relative,
                "fixed_net": fixed_net
            }
            return {
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        elif mode == "toxic":
            toxicity_cfg = copy.deepcopy(pos_base.get("toxicity_adaptive") or {
                "panic_toxicity_threshold": 0.70,
                "relaxed_toxicity_threshold": 0.35,
                "emergency_exit_floor_usdt": 3.0,
                "moderate_exit_floor_usdt": 8.0,
                "relaxed_exit_floor_usdt": 15.0,
                "min_panic_recovery": 0.40,
                "min_moderate_recovery": 0.20,
            })
            pos_cfg = {
                "scale_money": scale_money,
                "_desc_scale_money": desc_scale_money,
                "auto_scale_money": auto_scale_money,
                "mode_scale": mode_scale,
                "inverted_notional": inverted_notional,
                "margin_relative": margin_relative,
                "toxicity_adaptive": toxicity_cfg
            }
            return {
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        elif mode in ("trailing_ratchet", "classic", "iter_net_classic"):
            ratchet_cfg = pos_base.get("trailing_ratchet")
            if not ratchet_cfg or not isinstance(ratchet_cfg, dict):
                ratchet_cfg = {
                    "levels": [
                        {"activation": 13.0, "floor": 8.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L0: F7->F6 ($13 -> $8, Golden Pocket 61.8%)"},
                        {"activation": 21.0, "floor": 13.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L1: F8->F7 ($21 -> $13, Golden Pocket 61.8%)"},
                        {"activation": 34.0, "floor": 21.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L2: F9->F8 ($34 -> $21, Golden Pocket 61.8%)"},
                        {"activation": 55.0, "floor": 34.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L3: F10->F9 ($55 -> $34, Golden Pocket 61.8%)"},
                        {"activation": 89.0, "floor": 55.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L4: F11->F10 ($89 -> $55, Golden Pocket 61.8%)"},
                    ]
                }
            pos_cfg = {
                "scale_money": scale_money,
                "_desc_scale_money": desc_scale_money,
                "auto_scale_money": auto_scale_money,
                "mode_scale": mode_scale,
                "inverted_notional": inverted_notional,
                "margin_relative": margin_relative,
                "trailing_ratchet": copy.deepcopy(ratchet_cfg)
            }
            return {
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        else:
            raise ValueError(f"[AutoClosingConfigBuilder] Неизвестный режим автозакрытия '{mode}'! Допустимые режимы: 'trailing_ratchet', 'toxic', 'fixed', 'off'.")
