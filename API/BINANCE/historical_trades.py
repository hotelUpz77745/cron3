# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\API\BINANCE\historical_trades.py
# Role: historical_trades.py module

# ==============================================================================
# Path: API/BINANCE/historical_trades.py
# Role: Высокопроизводительный парсер и потоковый итератор архивов реальных сделок (aggTrades) Binance Vision
# ==============================================================================

import csv
import heapq
import io
import os
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator, List, Dict, Tuple, Optional, Any

from c_log import UnifiedLogger

logger = UnifiedLogger("HistoricalTrades")


class BinanceVisionTradeParser:
    """
    Высокопроизводительный парсер тиковых данных Binance Vision (aggTrades).
    Поддерживает:
    1. Потоковое чтение напрямую из .zip без распаковки на диск.
    2. Хронологическую итерацию по диапазону дат YYYY-MM-DD.
    3. Синхронное слияние (Heap Merge) тиковых потоков нескольких монет в единый упорядоченный поток по времени.
    """

    @staticmethod
    def parse_zip_file(zip_path: Path | str) -> Generator[Tuple[int, float, float, bool], None, None]:
        """
        Потоково читает один .zip архив aggTrades и отдает тики:
        (timestamp_ms, price, quantity, is_buyer_maker)
        """
        if hasattr(zip_path, "read"):
            zf = zipfile.ZipFile(zip_path, "r")
            should_close = False
        else:
            path = Path(zip_path)
            if not path.exists():
                logger.warning(f"[HistoricalTrades] Zip archive not found: {path}")
                return
            zf = zipfile.ZipFile(path, "r")
            should_close = True

        try:
            namelist = zf.namelist()
            csv_names = [n for n in namelist if n.endswith(".csv")]
            if not csv_names:
                return

            with zf.open(csv_names[0], "r") as raw_file:
                # TextIOWrapper для быстрого построчного чтения
                text_file = io.TextIOWrapper(raw_file, encoding="utf-8", newline="")
                reader = csv.reader(text_file)
                
                # Читаем заголовок
                header = next(reader, None)
                has_header = False
                if header and "transact_time" in header:
                    has_header = True
                    t_idx = header.index("transact_time")
                    p_idx = header.index("price")
                    q_idx = header.index("quantity")
                    m_idx = header.index("is_buyer_maker") if "is_buyer_maker" in header else 6
                else:
                    t_idx, p_idx, q_idx, m_idx = 5, 1, 2, 6
                    if header:
                        try:
                            # Проверяем, не строка ли это данных
                            ts_val = int(header[t_idx])
                            p_val = float(header[p_idx])
                            q_val = float(header[q_idx])
                            m_val = header[m_idx].lower() == "true"
                            yield (ts_val, p_val, q_val, m_val)
                        except (ValueError, IndexError):
                            pass

                for row in reader:
                    if not row or len(row) <= max(t_idx, p_idx, q_idx):
                        continue
                    try:
                        ts = int(row[t_idx])
                        price = float(row[p_idx])
                        qty = float(row[q_idx])
                        is_maker = row[m_idx].lower() == "true" if len(row) > m_idx else False
                        yield (ts, price, qty, is_maker)
                    except (ValueError, IndexError):
                        continue
        finally:
            if should_close:
                zf.close()

    @classmethod
    def get_symbol_daily_zips(
        cls,
        symbol_dir: Path | str,
        symbol: str,
        start_date: str,
        end_date: str
    ) -> List[Path]:
        """
        Возвращает отсортированный список файлов архивов за даты от start_date до end_date (включительно).
        Формат дат: 'YYYY-MM-DD'.
        """
        s_dir = Path(symbol_dir)
        if not s_dir.exists():
            return []

        dt_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        dt_end = datetime.strptime(end_date, "%Y-%m-%d").date()

        found_zips = []
        cur_dt = dt_start
        sym_clean = symbol.upper().replace("/", "")

        while cur_dt <= dt_end:
            date_str = cur_dt.strftime("%Y-%m-%d")
            # Возможные паттерны имен: SYMBOL-aggTrades-YYYY-MM-DD.zip
            cand1 = s_dir / f"{sym_clean}-aggTrades-{date_str}.zip"
            cand2 = s_dir / date_str / f"{sym_clean}-aggTrades-{date_str}.zip"
            cand3 = s_dir.parent / date_str / f"{sym_clean}-aggTrades-{date_str}.zip"
            if cand1.exists():
                found_zips.append(cand1)
            elif cand2.exists():
                found_zips.append(cand2)
            elif cand3.exists():
                found_zips.append(cand3)
            else:
                # Поиск без учета регистра
                for f in s_dir.glob(f"*{date_str}*.zip"):
                    found_zips.append(f)
                    break
            cur_dt += timedelta(days=1)

        return sorted(found_zips)

    @classmethod
    def iterate_symbol_trades(
        cls,
        symbol_dir: Path | str,
        symbol: str,
        start_date: str,
        end_date: str
    ) -> Generator[Tuple[int, float, float, bool], None, None]:
        """
        Последовательно отдает все тики по одной монете за указанный период дат.
        """
        zips = cls.get_symbol_daily_zips(symbol_dir, symbol, start_date, end_date)
        for zp in zips:
            yield from cls.parse_zip_file(zp)

    @classmethod
    def merge_multi_symbol_streams(
        cls,
        symbols_dirs: Dict[str, Path | str],
        start_date: str,
        end_date: str
    ) -> Generator[Tuple[str, int, float, float, bool], None, None]:
        """
        Многопоточный K-Way Merge тиков нескольких монет.
        Отдает тики строго в порядке возрастания timestamp_ms:
        (symbol, timestamp_ms, price, quantity, is_buyer_maker)
        """
        # Инициализируем генераторы для каждого символа
        heap = []
        iterators = {}

        for sym, s_dir in symbols_dirs.items():
            it = cls.iterate_symbol_trades(s_dir, sym, start_date, end_date)
            iterators[sym] = it
            try:
                first_tick = next(it)
                # Элемент кучи: (timestamp_ms, tie_breaker_index, symbol, tick)
                heapq.heappush(heap, (first_tick[0], id(it), sym, first_tick))
            except StopIteration:
                pass

        while heap:
            ts, _, sym, tick = heapq.heappop(heap)
            # tick = (ts, price, qty, is_maker)
            yield (sym, tick[0], tick[1], tick[2], tick[3])

            it = iterators[sym]
            try:
                next_tick = next(it)
                heapq.heappush(heap, (next_tick[0], id(it), sym, next_tick))
            except StopIteration:
                pass
