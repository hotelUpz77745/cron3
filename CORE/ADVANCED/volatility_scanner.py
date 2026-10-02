# ==============================================================================
# Path: CORE/ADVANCED/volatility_scanner.py
# Role: Мультифакторный сканер волатильности и рыночных фильтров для фьючерсов Binance
# ==============================================================================

import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from c_log import UnifiedLogger
from consts import DATA_DIR
from c_utils import Utils
from API.BINANCE.public import BinancePublic
from API.BINANCE.client_prod import BinanceClient

logger = UnifiedLogger("VolatilityScanner")


class VolatilityScanner:
    """
    Каскадный сканер монет для сеточного бота:
      1. Базовый фильтр ликвидности (24h volume USDT).
      2. ATR Volatility Filter (1w свечная волатильность).
      3. RSI Filter (4h перекупленность/перепроданность по Уайлдеру).
      4. Bollinger Width Filter (4h чистая волатильность относительно Lower Band).
      5. Center Proximity Filter (отклонение от средней линии middleBB: SMA или EMA).
    Все формулы реализованы вручную на чистом Python без сторонних библиотек.
    """

    def __init__(self, client=None):
        self.config_path = DATA_DIR / "app.json"
        self.output_path = DATA_DIR / "volatile_symbols.json"
        self.output_txt_path = DATA_DIR / "volatile_symbols.txt"
        self.client = client
        self.semaphore = asyncio.Semaphore(10)

    def _get_config(self) -> Dict[str, Any]:
        """Загружает и нормализует конфигурацию сканера из app.json с поддержкой legacy-формата."""
        default_cfg: Dict[str, Any] = {
            "enabled": True,
            "strict_window": True,
            "min_dayly_vol_usdt": 3000000,
            "atr_volatility_filter": {
                "enabled": True,
                "timeframe": "1w",
                "window": 12,
                "min_volatility_pct": 25.0,
                "max_volatility_pct": 40.0
            },
            "rsi_filter": {
                "enabled": True,
                "timeframe": "4h",
                "period": 14,
                "min_rsi": 35.0,
                "max_rsi": 65.0
            },
            "bollinger_width_filter": {
                "enabled": True,
                "timeframe": "4h",
                "MA_type": "SMA",
                "period": 20,
                "multiplier": 2.0,
                "min_width_pct": 8.0,
                "max_width_pct": 13.0,
                "center_proximity_filter": {
                    "enabled": True,
                    "max_deviation_from_center_pct": 75.0
                }
            }
        }

        if self.config_path.exists():
            try:
                data = Utils.read_json_file(self.config_path)
                if data and "volatility_scanner" in data:
                    raw_cfg = data["volatility_scanner"]
                    # Поддержка нового структурированного формата
                    if "atr_volatility_filter" in raw_cfg or "rsi_filter" in raw_cfg or "bollinger_width_filter" in raw_cfg:
                        default_cfg["enabled"] = raw_cfg.get("enabled", True)
                        default_cfg["strict_window"] = raw_cfg.get("strict_window", True)
                        if "min_dayly_vol_usdt" in raw_cfg:
                            default_cfg["min_dayly_vol_usdt"] = raw_cfg["min_dayly_vol_usdt"]

                        if "atr_volatility_filter" in raw_cfg and isinstance(raw_cfg["atr_volatility_filter"], dict):
                            default_cfg["atr_volatility_filter"].update(raw_cfg["atr_volatility_filter"])

                        if "rsi_filter" in raw_cfg and isinstance(raw_cfg["rsi_filter"], dict):
                            default_cfg["rsi_filter"].update(raw_cfg["rsi_filter"])

                        if "bollinger_width_filter" in raw_cfg and isinstance(raw_cfg["bollinger_width_filter"], dict):
                            bb_raw = raw_cfg["bollinger_width_filter"]
                            center_raw = bb_raw.get("center_proximity_filter", {})
                            default_cfg["bollinger_width_filter"].update(bb_raw)
                            if center_raw and isinstance(center_raw, dict):
                                default_cfg["bollinger_width_filter"]["center_proximity_filter"].update(center_raw)
                    else:
                        # Обратная совместимость с плоским legacy-форматом
                        default_cfg["strict_window"] = raw_cfg.get("strict_window", True)
                        default_cfg["min_dayly_vol_usdt"] = raw_cfg.get("min_dayly_vol_usdt", 3000000)
                        default_cfg["atr_volatility_filter"]["timeframe"] = raw_cfg.get("timeframe", "1w")
                        default_cfg["atr_volatility_filter"]["window"] = int(raw_cfg.get("window", 12))
                        default_cfg["atr_volatility_filter"]["min_volatility_pct"] = raw_cfg.get("min_volatility_pct", 25.0)
                        default_cfg["atr_volatility_filter"]["max_volatility_pct"] = raw_cfg.get("max_volatility_pct", 40.0)
            except Exception as e:
                logger.error(f"[Scanner] Error reading config: {e}")

        return default_cfg

    # ==========================================================================
    # ЧИСТЫЕ МАТЕМАТИЧЕСКИЕ ИНДИКАТОРЫ (БЕЗ СТОРОННИХ БИБЛИОТЕК)
    # ==========================================================================

    @staticmethod
    def compute_atr_volatility(
        klines: List[Dict[str, Any]],
        window: int = 12,
        strict_window: bool = True
    ) -> Tuple[bool, Optional[float], int]:
        """
        Расчет средней свечной волатильности: ((High / Low) - 1) * 100%.
        Возвращает (passed_strict, avg_volatility, candles_count).
        """
        if not klines:
            return False, None, 0

        if strict_window and len(klines) < window:
            return False, None, len(klines)

        target_klines = klines[-window:] if len(klines) >= window else klines
        total_vol = 0.0
        count = 0
        for k in target_klines:
            high = float(k.get("high", 0.0))
            low = float(k.get("low", 0.0))
            if low > 0:
                vol = ((high / low) - 1.0) * 100.0
                total_vol += vol
                count += 1

        if count == 0:
            return False, None, 0

        avg_vol = total_vol / count
        return True, round(avg_vol, 2), count

    @staticmethod
    def compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
        """
        Стандартный расчет индекса относительной силы (RSI) с экспоненциальным сглаживанием Уайлдера.
        """
        if len(closes) < period + 1:
            return None

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]

        # Первое среднее: простое среднее за первые `period` изменений
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # Последующие сглаживания по формуле Уайлдера
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0.0 else 50.0
        if avg_gain == 0.0:
            return 0.0

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return round(rsi, 2)

    @staticmethod
    def compute_ma(closes: List[float], period: int = 20, ma_type: str = "SMA") -> Optional[float]:
        """
        Расчет скользящей средней (SMA или EMA).
        """
        if len(closes) < period:
            return None

        ma_type_upper = (ma_type or "SMA").upper().strip()
        if ma_type_upper == "EMA":
            alpha = 2.0 / (period + 1.0)
            ema = sum(closes[:period]) / period
            for price in closes[period:]:
                ema = price * alpha + ema * (1.0 - alpha)
            return ema
        else:
            return sum(closes[-period:]) / period

    @staticmethod
    def compute_bollinger_bands(
        closes: List[float],
        period: int = 20,
        multiplier: float = 2.0,
        ma_type: str = "SMA"
    ) -> Optional[Dict[str, float]]:
        """
        Расчет полос Боллинджера и ширины канала относительно нижней границы:
          BB Width = ((Upper Band / Lower Band) - 1) * 100%
        """
        if len(closes) < period:
            return None

        middle_bb = VolatilityScanner.compute_ma(closes, period, ma_type)
        if middle_bb is None or middle_bb <= 0:
            return None

        window = closes[-period:]
        variance = sum((x - middle_bb) ** 2 for x in window) / period
        std_dev = math.sqrt(variance)

        upper_bb = middle_bb + multiplier * std_dev
        lower_bb = middle_bb - multiplier * std_dev

        if lower_bb <= 0:
            return None

        bb_width_pct = ((upper_bb / lower_bb) - 1.0) * 100.0

        return {
            "middle": middle_bb,
            "upper": upper_bb,
            "lower": lower_bb,
            "std_dev": std_dev,
            "width_pct": round(bb_width_pct, 2)
        }

    @staticmethod
    def compute_center_proximity(
        current_price: float,
        middle_bb: float,
        upper_bb: float,
        lower_bb: float
    ) -> float:
        """
        Расчет отклонения цены от middleBB в процентах от общей ширины канала:
          Dev Pct = (|current_price - middleBB| / (upper_bb - lower_bb)) * 100%
        50% = цена на границе канала, 75% = цена не дальше 1.5x полуширины от центра.
        """
        band_span = upper_bb - lower_bb
        if band_span <= 0:
            return 0.0

        dev_abs = abs(current_price - middle_bb)
        dev_pct = (dev_abs / band_span) * 100.0
        return round(dev_pct, 2)

    # ==========================================================================
    # КАСКАДНАЯ ОЦЕНКА ФИЛЬТРОВ МОНЕТЫ
    # ==========================================================================

    def _evaluate_symbol_filters(
        self,
        symbol: str,
        klines_1w: Optional[List[Dict[str, Any]]],
        klines_4h: Optional[List[Dict[str, Any]]],
        cfg: Dict[str, Any]
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Последовательно проверяет монету через все включенные (enabled: true) фильтры.
        Возвращает (passed, details_dict).
        """
        strict_window = cfg.get("strict_window", True)
        atr_cfg = cfg.get("atr_volatility_filter", {})
        rsi_cfg = cfg.get("rsi_filter", {})
        bb_cfg = cfg.get("bollinger_width_filter", {})

        result: Dict[str, Any] = {
            "symbol": symbol,
            "volatility": 0.0,
            "candles": 0,
            "rsi": None,
            "bb_width_pct": None,
            "bb_center_dev_pct": None,
            "middle_bb": None,
            "current_price": None
        }

        # ── 1. ATR Volatility Filter ──────────────────────────────────────────
        if atr_cfg.get("enabled", True):
            if not klines_1w:
                return False, None

            window = int(atr_cfg.get("window", 12))
            passed_strict, avg_vol, count = self.compute_atr_volatility(
                klines_1w, window=window, strict_window=strict_window
            )
            if not passed_strict or avg_vol is None:
                return False, None

            min_vol = atr_cfg.get("min_volatility_pct")
            max_vol = atr_cfg.get("max_volatility_pct")
            if min_vol is not None and avg_vol < float(min_vol):
                return False, None
            if max_vol is not None and avg_vol > float(max_vol):
                return False, None

            result["volatility"] = avg_vol
            result["candles"] = count

        # ── 2. RSI & Bollinger Bands Filters (по 4h свечам) ───────────────────
        need_4h = rsi_cfg.get("enabled", True) or bb_cfg.get("enabled", True)
        if need_4h:
            if not klines_4h:
                return False, None

            closes = [float(k["close"]) for k in klines_4h if "close" in k]
            if not closes:
                return False, None

            current_price = closes[-1]
            result["current_price"] = round(current_price, 6)

            # RSI Filter
            if rsi_cfg.get("enabled", True):
                period_rsi = int(rsi_cfg.get("period", 14))
                rsi_val = self.compute_rsi(closes, period=period_rsi)
                if rsi_val is None:
                    return False, None

                min_rsi = float(rsi_cfg.get("min_rsi", 35.0))
                max_rsi = float(rsi_cfg.get("max_rsi", 65.0))
                if rsi_val < min_rsi or rsi_val > max_rsi:
                    return False, None

                result["rsi"] = rsi_val

            # Bollinger Width & Center Proximity Filter
            if bb_cfg.get("enabled", True):
                period_bb = int(bb_cfg.get("period", 20))
                mult_bb = float(bb_cfg.get("multiplier", 2.0))
                ma_type = str(bb_cfg.get("MA_type", "SMA"))

                bb_data = self.compute_bollinger_bands(
                    closes, period=period_bb, multiplier=mult_bb, ma_type=ma_type
                )
                if not bb_data:
                    return False, None

                bb_width = bb_data["width_pct"]
                min_w = float(bb_cfg.get("min_width_pct", 8.0))
                max_w = float(bb_cfg.get("max_width_pct", 13.0))

                if bb_width < min_w or bb_width > max_w:
                    return False, None

                result["bb_width_pct"] = bb_width
                result["middle_bb"] = round(bb_data["middle"], 6)

                # Подфильтр близости к центру
                center_cfg = bb_cfg.get("center_proximity_filter", {})
                if center_cfg.get("enabled", True):
                    max_dev = float(center_cfg.get("max_deviation_from_center_pct", 75.0))
                    dev_pct = self.compute_center_proximity(
                        current_price, bb_data["middle"], bb_data["upper"], bb_data["lower"]
                    )
                    if dev_pct > max_dev:
                        return False, None

                    result["bb_center_dev_pct"] = dev_pct

        return True, result

    async def _fetch_klines(self, symbol: str, interval: str, limit: int) -> Optional[List[Dict[str, Any]]]:
        """Асинхронная загрузка свечей с fallback на клиент биржи."""
        try:
            klines = await BinancePublic.get_klines(symbol, interval, limit)
            if not klines and self.client:
                klines = await self.client.get_klines(symbol, interval, limit)
            return klines
        except Exception as e:
            logger.debug(f"[Scanner] Error fetching klines for {symbol} ({interval}): {e}")
            return None

    async def _process_symbol(self, symbol: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Обрабатывает одну монету с сетевым ограничением semaphore."""
        async with self.semaphore:
            try:
                atr_cfg = cfg.get("atr_volatility_filter", {})
                rsi_cfg = cfg.get("rsi_filter", {})
                bb_cfg = cfg.get("bollinger_width_filter", {})

                # 1. Загрузка 1w свечей для ATR (только если ATR включен)
                klines_1w = None
                if atr_cfg.get("enabled", True):
                    tf_1w = atr_cfg.get("timeframe", "1w")
                    win_1w = int(atr_cfg.get("window", 12))
                    klines_1w = await self._fetch_klines(symbol, tf_1w, win_1w)
                    if not klines_1w:
                        return None

                # 2. Загрузка 4h свечей (один запрос для RSI и Bollinger)
                klines_4h = None
                need_4h = rsi_cfg.get("enabled", True) or bb_cfg.get("enabled", True)
                if need_4h:
                    tf_4h = rsi_cfg.get("timeframe", "4h") if rsi_cfg.get("enabled", True) else bb_cfg.get("timeframe", "4h")
                    p_rsi = int(rsi_cfg.get("period", 14)) if rsi_cfg.get("enabled", True) else 14
                    p_bb = int(bb_cfg.get("period", 20)) if bb_cfg.get("enabled", True) else 20
                    limit_4h = max(50, p_rsi * 3, p_bb + 10)
                    klines_4h = await self._fetch_klines(symbol, tf_4h, limit_4h)
                    if not klines_4h:
                        return None

                # 3. Каскадная оценка фильтров
                passed, res = self._evaluate_symbol_filters(symbol, klines_1w, klines_4h, cfg)
                if passed:
                    return res

            except Exception as e:
                logger.error(f"[Scanner] Error processing {symbol}: {e}")

        return None

    async def scan(self) -> Optional[Path]:
        """Основной цикл сканирования рынка."""
        cfg = self._get_config()
        if not cfg.get("enabled", True):
            logger.warning("[Scanner] Volatility scanner is disabled in config.")
            return None

        atr_cfg = cfg.get("atr_volatility_filter", {})
        rsi_cfg = cfg.get("rsi_filter", {})
        bb_cfg = cfg.get("bollinger_width_filter", {})

        logger.info(
            f"[Scanner] Starting scan. Filters: "
            f"ATR({atr_cfg.get('enabled')}, {atr_cfg.get('timeframe')}, {atr_cfg.get('window')}w: "
            f"{atr_cfg.get('min_volatility_pct')}..{atr_cfg.get('max_volatility_pct')}%), "
            f"RSI({rsi_cfg.get('enabled')}, {rsi_cfg.get('timeframe')}, p={rsi_cfg.get('period')}: "
            f"{rsi_cfg.get('min_rsi')}..{rsi_cfg.get('max_rsi')}), "
            f"BB({bb_cfg.get('enabled')}, {bb_cfg.get('MA_type')}, p={bb_cfg.get('period')}: "
            f"{bb_cfg.get('min_width_pct')}..{bb_cfg.get('max_width_pct')}%)"
        )

        symbols = await BinancePublic.get_perp_symbols()
        if not symbols:
            logger.error("[Scanner] Failed to fetch perpetual symbols from Binance.")
            return None

        logger.info(f"[Scanner] Found {len(symbols)} USDT-M Perpetual symbols.")

        # ── Шаг 1: Базовый фильтр ликвидности ─────────────────────────────────
        min_dayly_vol = cfg.get("min_dayly_vol_usdt")
        if min_dayly_vol is not None and float(min_dayly_vol) > 0:
            logger.info(f"[Scanner] Filtering symbols by min_dayly_vol_usdt >= {float(min_dayly_vol):,.0f} USDT")
            tickers = await BinancePublic.get_24h_ticker()
            if tickers:
                vol_map = {t["symbol"]: float(t.get("quoteVolume", 0.0)) for t in tickers}
                symbols = [s for s in symbols if vol_map.get(s, 0.0) >= float(min_dayly_vol)]
                logger.info(f"[Scanner] {len(symbols)} symbols passed liquidity filter.")
            else:
                logger.warning("[Scanner] Failed to fetch 24h tickers, skipping volume filter.")

        # ── Шаг 2-4: Каскадная параллельная обработка ─────────────────────────
        tasks = [self._process_symbol(sym, cfg) for sym in symbols]
        results = await asyncio.gather(*tasks)

        matching_symbols = [r for r in results if r is not None]

        # Сортировка по волатильности (убывание)
        matching_symbols.sort(key=lambda x: x.get("volatility", 0.0), reverse=True)

        # Сохранение JSON
        try:
            with open(self.output_path, "w", encoding="utf-8") as f:
                json.dump(matching_symbols, f, indent=4, ensure_ascii=False)
            logger.info(f"[Scanner] Saved {len(matching_symbols)} matching symbols to {self.output_path}")
        except Exception as e:
            logger.error(f"[Scanner] Error saving to {self.output_path}: {e}")

        # Сохранение TXT-отчета с детализацией по всем фильтрам
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            symbols_list = [item["symbol"] for item in matching_symbols]
            json_symbols_inline = json.dumps(symbols_list, ensure_ascii=False)
            json_symbols_multiline = json.dumps(symbols_list, indent=4, ensure_ascii=False)

            lines = [
                f"Дата: {now_str}",
                f"Количество монет: {len(matching_symbols)}",
                "",
                "JSON Symbols List (для app.json):",
                json_symbols_inline,
                "",
                "JSON Symbols List (развернутый):",
                json_symbols_multiline,
                "",
                "Детализация:",
                "_____"
            ]
            for item in matching_symbols:
                lines.append(f"Символ: {item['symbol']}")
                lines.append(f"Волатильность (1w ATR): {item.get('volatility', 0.0)}% (свечей: {item.get('candles', 0)})")
                if item.get("rsi") is not None:
                    lines.append(f"RSI (4h): {item['rsi']}")
                if item.get("bb_width_pct") is not None:
                    lines.append(f"BB Width (4h): {item['bb_width_pct']}%")
                if item.get("bb_center_dev_pct") is not None:
                    lines.append(f"BB Center Dev: {item['bb_center_dev_pct']}% (Middle: {item.get('middle_bb')}, Price: {item.get('current_price')})")
                lines.append("_____")

            with open(self.output_txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logger.info(f"[Scanner] Saved TXT report to {self.output_txt_path}")
        except Exception as e:
            logger.error(f"[Scanner] Error saving TXT: {e}")

        return self.output_path


async def run_scanner():
    scanner = VolatilityScanner()
    try:
        path = await scanner.scan()
        return path
    finally:
        if scanner.client:
            await scanner.client.shutdown()


if __name__ == "__main__":
    asyncio.run(run_scanner())
