# ==============================================================================
# Path: CORE/SERVICES/profit_transfer_service.py
# Role: Сервис автоматического перевода профита итерации с Futures на Spot при срабатывании trailing_ratchet
# ==============================================================================

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

from API.BINANCE.transfer_manager import TransferManager, DEFAULT_TRANSFER_ASSET
from c_log import UnifiedLogger

logger = UnifiedLogger("ProfitTransferService")

DEFAULT_TRANSFER_PROFIT_PCT: float = 100.0
DEFAULT_MIN_TRANSFER_USDT: float = 1.0
HUNDRED_PCT: float = 100.0


class ProfitTransferService:
    """
    Инкапсулирует бизнес-логику автоматического вывода чистого профита закрытой итерации
    с фьючерсного счета (USDⓈ-M) на спотовый кошелек (Spot) при срабатывании храповика trailing_ratchet.
    """

    @staticmethod
    def extract_transfer_config(strat_cfg: Optional[Any]) -> Dict[str, Any]:
        """
        Извлекает и нормализует конфигурацию transfer_profit_to_spot из паспорта стратегии или словаря auto_closing.
        Поддерживает как полный словарь в auto_closing.positive.trailing_ratchet.transfer_profit_to_spot,
        так и быстрые переопределения в active_strategies (CFG/app.json).
        """
        result: Dict[str, Any] = {
            "enabled": False,
            "transfer_profit_pct": DEFAULT_TRANSFER_PROFIT_PCT,
            "min_transfer_usdt": DEFAULT_MIN_TRANSFER_USDT,
            "asset": DEFAULT_TRANSFER_ASSET,
        }
        if strat_cfg is None:
            return result

        candidates = []

        if isinstance(strat_cfg, dict):
            # Передан напрямую словарь auto_closing или словарь стратегии
            pos = strat_cfg.get("positive") or (strat_cfg.get("auto_closing") or {}).get("positive") or {}
            tr = pos.get("trailing_ratchet") or {}
            if "transfer_profit_to_spot" in tr:
                candidates.append(tr["transfer_profit_to_spot"])
            if "transfer_profit_to_spot" in pos:
                candidates.append(pos["transfer_profit_to_spot"])
            if "transfer_profit_to_spot" in strat_cfg:
                candidates.append(strat_cfg["transfer_profit_to_spot"])
        else:
            # Объект StrategyConfig: собираем по иерархии от базового шаблона к активным оверрайдам
            raw_meta = getattr(strat_cfg, "raw_meta", None) or {}
            overrides = getattr(strat_cfg, "overrides", None) or {}
            auto_closing_cfg = getattr(strat_cfg, "auto_closing_cfg", None) or {}
            active_settings = getattr(strat_cfg, "active_settings", None) or {}

            for ac_src in (
                raw_meta.get("auto_closing"),
                overrides.get("auto_closing"),
                auto_closing_cfg,
                active_settings.get("auto_closing"),
            ):
                if isinstance(ac_src, dict):
                    pos = ac_src.get("positive") or {}
                    if isinstance(pos, dict):
                        tr = pos.get("trailing_ratchet") or {}
                        if isinstance(tr, dict) and "transfer_profit_to_spot" in tr:
                            candidates.append(tr["transfer_profit_to_spot"])
                        if "transfer_profit_to_spot" in pos:
                            candidates.append(pos["transfer_profit_to_spot"])

            for direct_src in (raw_meta, overrides, active_settings):
                if isinstance(direct_src, dict) and "transfer_profit_to_spot" in direct_src:
                    candidates.append(direct_src["transfer_profit_to_spot"])

        for cand in candidates:
            if isinstance(cand, bool):
                result["enabled"] = cand
            elif isinstance(cand, dict):
                if "enabled" in cand and cand["enabled"] is not None:
                    result["enabled"] = bool(cand["enabled"])
                if "transfer_profit_pct" in cand and cand["transfer_profit_pct"] is not None:
                    result["transfer_profit_pct"] = float(cand["transfer_profit_pct"])
                if "min_transfer_usdt" in cand and cand["min_transfer_usdt"] is not None:
                    result["min_transfer_usdt"] = float(cand["min_transfer_usdt"])
                if "asset" in cand and cand["asset"]:
                    result["asset"] = str(cand["asset"]).upper().strip()

        return result

    @staticmethod
    def calculate_transfer_amount(iter_net_usdt: float, transfer_cfg: Dict[str, Any]) -> float:
        """
        Вычисляет сумму перевода в USDT строго по проценту transfer_profit_pct (суффикс _pct: 0..100).
        Округляет вниз до 2 знаков после запятой (ROUND_DOWN), чтобы гарантированно не превысить реализованный профит.
        """
        if not transfer_cfg or not transfer_cfg.get("enabled", False):
            return 0.0

        net_val = float(iter_net_usdt or 0.0)
        if net_val <= 0.0:
            return 0.0

        pct = float(transfer_cfg.get("transfer_profit_pct", DEFAULT_TRANSFER_PROFIT_PCT))
        if pct <= 0.0:
            return 0.0
        pct_clamped = min(HUNDRED_PCT, pct)

        raw_target = Decimal(str(net_val)) * (Decimal(str(pct_clamped)) / Decimal("100"))
        rounded_amount = float(raw_target.quantize(Decimal("0.01"), rounding=ROUND_DOWN))

        min_usdt = float(transfer_cfg.get("min_transfer_usdt", DEFAULT_MIN_TRANSFER_USDT))
        if rounded_amount < min_usdt:
            return 0.0

        return rounded_amount

    @classmethod
    async def execute_on_ratchet_close(
        cls,
        bot_core: Any,
        strat_id: str,
        strat_cfg: Optional[Any],
        iter_net_usdt: float,
        close_type: str = "TRAILING RATCHET",
    ) -> Optional[Dict[str, Any]]:
        """
        Исполняет перевод профита на Spot после закрытия всех позиций купола по храповику trailing_ratchet.
        Безопасен к сбоям сети/API: не блокирует закрытие итерации и возвращает статус для уведомления в Telegram.
        """
        c_type_upper = str(close_type or "").upper().strip()
        if "TRAILING RATCHET" not in c_type_upper and c_type_upper not in (
            "TRAILING_RATCHET",
            "CLASSIC",
            "ITER_NET_CLASSIC",
        ):
            return None

        cfg_source = strat_cfg
        if cfg_source is None and hasattr(bot_core, "auto_closing_cfg"):
            cfg_source = getattr(bot_core, "auto_closing_cfg")

        transfer_cfg = cls.extract_transfer_config(cfg_source)
        if not transfer_cfg.get("enabled", False):
            return None

        pct = float(transfer_cfg.get("transfer_profit_pct", DEFAULT_TRANSFER_PROFIT_PCT))
        asset = str(transfer_cfg.get("asset", DEFAULT_TRANSFER_ASSET)).upper()
        amount_usdt = cls.calculate_transfer_amount(iter_net_usdt, transfer_cfg)

        if amount_usdt <= 0.0:
            logger.info(
                f"[PROFIT_TRANSFER][{strat_id}] Перевод пропущен: расчетная сумма {amount_usdt:.2f} {asset} "
                f"меньше порога min_transfer_usdt={transfer_cfg.get('min_transfer_usdt')} "
                f"(iter_net={iter_net_usdt:+.2f} USDT, pct={pct:g}%)."
            )
            return {
                "executed": False,
                "reason": "below_min_threshold",
                "amount_usdt": 0.0,
                "transfer_profit_pct": pct,
                "asset": asset,
                "alert_line": "",
            }

        client = None
        if hasattr(bot_core, "get_client_for_strategy") and callable(bot_core.get_client_for_strategy):
            client = bot_core.get_client_for_strategy(strat_id)
        if client is None:
            client = getattr(bot_core, "client", None)

        if client is None or not hasattr(client, "transfer_futures_to_spot"):
            logger.warning(
                f"[PROFIT_TRANSFER][{strat_id}] Клиент биржи не поддерживает transfer_futures_to_spot. Перевод пропущен."
            )
            return {
                "executed": False,
                "reason": "client_not_supported",
                "amount_usdt": amount_usdt,
                "transfer_profit_pct": pct,
                "asset": asset,
                "alert_line": "",
            }

        try:
            res = await client.transfer_futures_to_spot(amount=amount_usdt, asset=asset)
            if getattr(res, "success", False):
                actual_amt = amount_usdt
                if isinstance(getattr(res, "data", None), dict) and "amount" in res.data:
                    try:
                        actual_amt = float(res.data["amount"])
                    except (TypeError, ValueError):
                        actual_amt = amount_usdt
                tran_id = res.data.get("tranId") if isinstance(getattr(res, "data", None), dict) else None
                logger.info(
                    f"💸 [PROFIT_TRANSFER][{strat_id}] Успешно переведено на Spot: "
                    f"+{actual_amt:.2f} {asset} ({pct:g}% от профита итерации {iter_net_usdt:+.2f} USDT, tranId={tran_id})"
                )
                alert_line = (
                    f"💸 Перевод профита на Spot: <b>+{actual_amt:.2f} {asset}</b> "
                    f"(<b>{pct:g}%</b> от профита итерации)"
                )
                return {
                    "executed": True,
                    "amount_usdt": actual_amt,
                    "transfer_profit_pct": pct,
                    "asset": asset,
                    "tran_id": tran_id,
                    "alert_line": alert_line,
                }
            else:
                err_msg = getattr(res, "error_msg", "Unknown error")
                err_code = getattr(res, "error_code", None)
                logger.warning(
                    f"⚠️ [PROFIT_TRANSFER][{strat_id}] Не удалось перевести {amount_usdt:.2f} {asset} на Spot: "
                    f"code={err_code}, msg={err_msg}"
                )
                alert_line = (
                    f"⚠️ Авто-перевод на Spot (<b>{amount_usdt:.2f} {asset}</b>) не выполнен: "
                    f"<code>{err_msg}</code>"
                )
                return {
                    "executed": False,
                    "reason": "api_error",
                    "error_code": err_code,
                    "error_msg": err_msg,
                    "amount_usdt": amount_usdt,
                    "transfer_profit_pct": pct,
                    "asset": asset,
                    "alert_line": alert_line,
                }
        except Exception as exc:
            logger.error(f"[PROFIT_TRANSFER][{strat_id}] Исключение при переводе профита на Spot: {exc}", exc_info=True)
            return {
                "executed": False,
                "reason": "exception",
                "error_msg": str(exc),
                "amount_usdt": amount_usdt,
                "transfer_profit_pct": pct,
                "asset": asset,
                "alert_line": f"⚠️ Ошибка авто-перевода на Spot: <code>{exc}</code>",
            }
