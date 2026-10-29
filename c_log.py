# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\c_log.py
# Role: c_log.py module

# ==============================================================================
# Path: c_log.py
# Role: Единая система логирования
# ==============================================================================

from __future__ import annotations

import pytz
from datetime import datetime, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pprint import pformat
from typing import Any, Optional
import os
import sys
import logging
import traceback
from pathlib import Path

import threading
import time

from consts import (
    LOG_TO_CONSOLE,
    LOG_TO_FILE,
    LOG_DIR,
    LOG_DEBUG,
    LOG_INFO,
    LOG_WARNING,
    LOG_ERROR,
    MAX_LOG_LINES,
    TIME_ZONE,
)

DEFAULT_LOG_DIR = str(LOG_DIR)
class UnlockedRotatingFileHandler(RotatingFileHandler):
    """
    Кастомный RotatingFileHandler, который не держит файл постоянно открытым.
    Каждая запись (emit) открывает, пишет и закрывает файл.
    Это снимает жесткую блокировку Windows, позволяя удалять/переименовывать логи.
    Поддерживает строгий лимит строк (max_lines): при превышении лимита формирует новый файл,
    сдвигая архивы (all.log.1, all.log.2, ...).
    """
    _line_counts: dict[str, int] = {}
    _lock = threading.Lock()

    def __init__(self, filename, mode='a', maxBytes=0, max_lines=MAX_LOG_LINES, backupCount=5, encoding=None):
        super().__init__(filename, mode, maxBytes, backupCount, encoding, delay=True)
        self.stream = None  # Принудительно отключаем постоянный стрим
        self.max_lines = max_lines

    def _open(self):  # type: ignore[override]
        return None  # type: ignore[return-value]

    @classmethod
    def _get_line_count(cls, filename: str) -> int:
        with cls._lock:
            if filename not in cls._line_counts:
                count = 0
                if os.path.exists(filename):
                    try:
                        with open(filename, "r", encoding="utf-8", errors="replace") as f:
                            count = sum(1 for _ in f)
                    except Exception:
                        count = 0
                cls._line_counts[filename] = count
            return cls._line_counts[filename]

    def shouldRollover(self, record):
        # 1. Первоочередная проверка по лимиту строк
        if self.max_lines > 0:
            if self._get_line_count(self.baseFilename) >= self.max_lines:
                return 1
        # 2. Резервная проверка по размеру файла в байтах
        if self.maxBytes > 0:
            msg = "%s\n" % self.format(record)
            try:
                size = os.path.getsize(self.baseFilename)
            except OSError:
                size = 0
            if size + len(msg.encode(self.encoding or 'utf-8')) >= self.maxBytes:
                return 1
        return 0

    def doRollover(self):
        """Безопасная ротация: формирует новый файл, сдвигая предыдущие в .1, .2, ..."""
        with self._lock:
            if self.stream:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None

            for i in range(self.backupCount - 1, 0, -1):
                sfn = f"{self.baseFilename}.{i}"
                dfn = f"{self.baseFilename}.{i + 1}"
                if os.path.exists(sfn):
                    try:
                        if os.path.exists(dfn):
                            os.remove(dfn)
                        os.replace(sfn, dfn)
                    except Exception:
                        pass

            dfn = f"{self.baseFilename}.1"
            if os.path.exists(self.baseFilename):
                try:
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    os.replace(self.baseFilename, dfn)
                except Exception:
                    try:
                        import shutil
                        shutil.copy2(self.baseFilename, dfn)
                        with open(self.baseFilename, "w", encoding=self.encoding or "utf-8") as f:
                            pass
                    except Exception:
                        pass

            self._line_counts[self.baseFilename] = 0

    def emit(self, record):
        try:
            if self.shouldRollover(record):
                self.doRollover()
                
            msg = self.format(record)
            lines_added = msg.count('\n') + 1
            os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
            with open(self.baseFilename, self.mode, encoding=self.encoding) as f:
                f.write(msg + self.terminator)
            with self._lock:
                self._line_counts[self.baseFilename] = self._line_counts.get(self.baseFilename, 0) + lines_added
        except Exception:
            self.handleError(record)


# ============================================================
# TIME
# ============================================================

import inspect

def log_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# HELPERS
# ============================================================

def estimate_average_line_length(path: str, sample: int = 200) -> int:
    if not os.path.exists(path):
        return 300
    try:
        lines = []
        with open(path, "r", encoding="utf-8") as f:
            for _ in range(sample):
                line = next(f, None)
                if line is None:
                    break
                lines.append(len(line))
        return sum(lines) // len(lines) if lines else 300
    except Exception:
        return 300


def calc_max_bytes(avg_len: int, lines: int) -> int:
    return avg_len * lines


# ============================================================
# UNIFIED LOGGER
# ============================================================
class UnifiedLogger:
    """
    Универсальный логгер:
    - logging + RotatingFileHandler
    - decorator для методов
    - совместим с async / sync
    """

    def __init__(
        self,
        name: str,
        log_dir: Optional[str] = None,
        max_lines: int = MAX_LOG_LINES,
        context: Optional[dict] = None,
    ):
        if log_dir is None:
            log_dir = DEFAULT_LOG_DIR
        os.makedirs(log_dir, exist_ok=True)

        argv_str = " ".join(sys.argv).lower()
        is_backtesting = (
            "backtest" in argv_str or "study" in argv_str or "tournament" in argv_str or os.getenv("CRON3_BACKTEST") == "1"
        )
        is_testing = (
            any(k in argv_str for k in ("unittest", "pytest", "discover", "tests\\", "tests/"))
            or ("unittest" in sys.modules and any("test" in a.lower() for a in sys.argv))
            or "pytest" in sys.modules
        )

        if is_backtesting:
            log_filename = "backtest_errors.log"
        elif is_testing:
            log_filename = "tests.log"
        else:
            log_filename = "all.log"

        all_log_path = os.path.join(log_dir, log_filename)
        avg_len = estimate_average_line_length(all_log_path)
        max_bytes = calc_max_bytes(avg_len, max_lines)

        if is_backtesting:
            desired_level = logging.ERROR
        else:
            desired_level = logging.DEBUG if LOG_DEBUG else logging.INFO

        logger = logging.getLogger(name)
        logger.setLevel(desired_level)
        logger.propagate = False  # 🔒 важно: не всплывать в root

        # 🔑 КРИТИЧНО: handler добавляем ТОЛЬКО если его ещё нет
        if not logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)s | %(context)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            
            if LOG_TO_FILE:
                all_handler = UnlockedRotatingFileHandler(
                    all_log_path,
                    maxBytes=max_bytes,
                    max_lines=max_lines,
                    backupCount=5,
                    encoding="utf-8",
                )
                all_handler.setLevel(desired_level)
                all_handler.setFormatter(formatter)
                logger.addHandler(all_handler)

            # Console
            if LOG_TO_CONSOLE:
                stream = sys.stdout
                if hasattr(stream, "reconfigure"):
                    try:
                        stream.reconfigure(errors="replace")
                    except Exception:
                        pass
                console_handler = logging.StreamHandler(stream)
                console_handler.setLevel(desired_level)
                console_handler.setFormatter(formatter)
                logger.addHandler(console_handler)

        self._logger = logging.LoggerAdapter(
            logger,
            extra={"context": context or name},
        )
        self._last_log_times = {}

    def _should_throttle(self, msg: str, throttle_sec: int, throttle_key: Optional[str] = None) -> bool:
        if throttle_sec <= 0:
            return False
        current_time = time.time()
        key = throttle_key if throttle_key is not None else msg
        last_time = self._last_log_times.get(key, 0)
        if current_time - last_time < throttle_sec:
            return True
        self._last_log_times[key] = current_time
        return False

    def debug(self, msg: str, *args, throttle_sec: int = 0, throttle_key: Optional[str] = None, **kwargs):
        if LOG_DEBUG and not self._should_throttle(msg, throttle_sec, throttle_key):
            self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, throttle_sec: int = 0, throttle_key: Optional[str] = None, **kwargs):
        if LOG_INFO and not self._should_throttle(msg, throttle_sec, throttle_key):
            self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, throttle_sec: int = 0, throttle_key: Optional[str] = None, **kwargs):
        if LOG_WARNING and not self._should_throttle(msg, throttle_sec, throttle_key):
            self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, throttle_sec: int = 0, throttle_key: Optional[str] = None, **kwargs):
        if LOG_ERROR and not self._should_throttle(msg, throttle_sec, throttle_key):
            self._logger.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args, throttle_sec: int = 0, throttle_key: Optional[str] = None, **kwargs):
        if LOG_ERROR and not self._should_throttle(msg, throttle_sec, throttle_key):
            self._logger.critical(msg, *args, **kwargs)

    def exception(self, msg: str, *args, throttle_sec: int = 0, throttle_key: Optional[str] = None, exc: Optional[Exception] = None, **kwargs):
        if LOG_ERROR and not self._should_throttle(msg, throttle_sec, throttle_key):
            self._logger.exception(msg, *args, **kwargs)

    # ======================================================
    # DECORATOR
    # ======================================================
    def total_exception_decor(self, func, context: Optional[Any] = None):
        """
        Ловит ВСЕ исключения, логирует контекст,
        НЕ крашит приложение.
        """

        if getattr(func, "_is_wrapped", False):
            return func

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as ex:
                self._log_exception(func, ex, args, kwargs, context)
                return None

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as ex:
                self._log_exception(func, ex, args, kwargs, context)
                return None

        wrapper = (
            async_wrapper
            if inspect.iscoroutinefunction(func)
            else sync_wrapper
        )
        setattr(wrapper, "_is_wrapped", True)
        return wrapper

    def _log_exception(self, func, ex, args, kwargs, context: Optional[Any] = None):
        extra = {}
        if context is not None:
            extra["context"] = context

        self._logger.error(
            f"[EXCEPTION] {func.__qualname__} -> {ex}\n"
            f"Args:\n{pformat({'args': args, 'kwargs': kwargs})}\n"
            f"Stack:\n{traceback.format_exc()}",
            extra=extra or None,
        )