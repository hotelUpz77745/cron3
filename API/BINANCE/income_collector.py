# API/BINANCE/income_collector.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: API.BINANCE.income_collector
ROLE: Сборщик исторического PnL, комиссий, фандинга и свечей (REST)
==============================================================================
"""

import os
import time
import asyncio
from typing import Optional, List, Dict, Any
from consts import REQ_TIMEOUT_SEC
from c_log import UnifiedLogger

logger = UnifiedLogger("INCOME_COLLECTOR")


class IncomeCollector:
    """Сборщик реализованного PnL, комиссий и Funding Fee."""

    @staticmethod
    async def get_income_pnl(
        client: Any,
        symbol: str,
        side: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Optional[Dict[str, float]]:
        if client.is_mock_mode and client._mock_provider:
            if client._mock_provider.simulate_analytics_network_error or os.getenv("MOCK_ANALYTICS_NETWORK_ERROR") == "1":
                return None
            inc_res = await client._mock_provider.fetch_income(
                symbol=symbol,
                start_time=start_time,
                end_time=end_time,
            )
            if not inc_res.success:
                return None
            records = inc_res.data or []
            gross = sum(float(r.get("income", 0.0)) for r in records if r.get("incomeType") == "REALIZED_PNL")
            comm = sum(float(r.get("income", 0.0)) for r in records if r.get("incomeType") == "COMMISSION")
            fund = sum(float(r.get("income", 0.0)) for r in records if r.get("incomeType") == "FUNDING_FEE")
            return {
                "gross_pnl": gross,
                "commission": comm,
                "funding_fee": fund,
                "net_pnl": gross + comm + fund,
            }

        trade_params = {
            "symbol": symbol,
            "recvWindow": 20000,
            "limit": 1000,
        }
        if start_time:
            trade_params["startTime"] = start_time
        if end_time:
            trade_params["endTime"] = end_time

        trade_res = await client._request(
            "GET",
            client.user_trades_url,
            params=trade_params,
            signed=True,
        )

        gross_pnl = 0.0
        commission = 0.0
        bnb_price = 0.0

        if trade_res.success and isinstance(trade_res.data, list):
            for t in trade_res.data:
                if t.get("positionSide") != side:
                    continue

                ts = int(t.get("time", 0))
                if start_time and ts < start_time:
                    continue

                gross_pnl += float(t.get("realizedPnl", 0.0))
                commission_val = float(t.get("commission", 0.0))
                asset = t.get("commissionAsset", "")

                if asset == "BNB":
                    if bnb_price == 0.0:
                        try:
                            from API.BINANCE.public import BinancePublic
                            bulk_p = await BinancePublic.get_prices_bulk(["BNBUSDT"])
                            if "BNBUSDT" in bulk_p:
                                bnb_price = float(bulk_p["BNBUSDT"])
                        except Exception:
                            pass
                    commission -= (commission_val * bnb_price) if bnb_price > 0 else commission_val
                else:
                    commission -= commission_val

        inc_params = {
            "symbol": symbol,
            "incomeType": "FUNDING_FEE",
            "recvWindow": 20000,
            "limit": 1000,
        }
        if start_time:
            inc_params["startTime"] = start_time
        if end_time:
            inc_params["endTime"] = end_time

        inc_res = await client._request(
            "GET",
            client.income_url,
            params=inc_params,
            signed=True,
        )

        funding_fee = 0.0
        if inc_res.success and isinstance(inc_res.data, list):
            for r in inc_res.data:
                ts = int(r.get("time", 0))
                if start_time and ts < start_time:
                    continue
                funding_fee += float(r.get("income", 0.0))

        net_pnl = gross_pnl + commission + funding_fee

        return {
            "gross_pnl": gross_pnl,
            "commission": commission,
            "funding_fee": funding_fee,
            "net_pnl": net_pnl,
        }


class KlineFetcher:
    """Загрузчик свечных данных (klines) с защитой от rate limit."""

    @staticmethod
    async def get_klines(
        client: Any,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> Optional[List[dict]]:
        limit = min(limit, 1500)
        limit_sec = 0.5
        async with client._kline_lock:
            elapsed = time.monotonic() - client._kline_last_send
            if elapsed < limit_sec:
                await asyncio.sleep(limit_sec - elapsed)
            client._kline_last_send = time.monotonic()

        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }

        try:
            async with client._session_lock:
                session = await client._get_session()

            async with session.get(
                url="https://fapi.binance.com/fapi/v1/klines",
                params=params,
                timeout=REQ_TIMEOUT_SEC
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[{symbol}] get_klines error HTTP {resp.status}")
                    return None

                data = await resp.json()
                if not isinstance(data, list):
                    return None

                data.sort(key=lambda x: int(x[0]))

                result = []
                for k in data:
                    result.append({
                        "open_time": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": int(k[6])
                    })
                return result

        except Exception as e:
            logger.error(f"[{symbol}] Exception in get_klines: {e}")
            return None
