# ==============================================================================
# Path: CORE/STRATEGY/CPP/native_indicators.py
# Role: Нативный Python-бридж к kernel_indicators.hpp (C++ DLL через ctypes + Numba LLVM JIT + NumPy fallback)
# ==============================================================================

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

try:
    from numba import njit
    _HAS_NUMBA = True
except Exception:  # pragma: no cover
    njit = None
    _HAS_NUMBA = False

_CPP_DIR = Path(__file__).resolve().parent


def _try_load_cpp_lib() -> Optional[ctypes.CDLL]:
    for name in ("kernel_indicators.dll", "libkernel_indicators.so", "libkernel_indicators.dylib"):
        lib_path = _CPP_DIR / name
        if lib_path.exists():
            try:
                lib = ctypes.CDLL(str(lib_path))
                c_dp = ctypes.POINTER(ctypes.c_double)
                lib.hron_compute_ema.argtypes = [c_dp, ctypes.c_int, ctypes.c_int, c_dp]
                lib.hron_compute_ema.restype = ctypes.c_int
                lib.hron_compute_ema_sma_seeded.argtypes = [c_dp, ctypes.c_int, ctypes.c_int, c_dp]
                lib.hron_compute_ema_sma_seeded.restype = ctypes.c_int
                lib.hron_compute_atr.argtypes = [c_dp, c_dp, c_dp, ctypes.c_int, ctypes.c_int, c_dp]
                lib.hron_compute_atr.restype = ctypes.c_int
                lib.hron_compute_atr_pct_sma.argtypes = [c_dp, c_dp, c_dp, ctypes.c_int, ctypes.c_int]
                lib.hron_compute_atr_pct_sma.restype = ctypes.c_double
                return lib
            except Exception:
                return None
    return None


if _HAS_NUMBA:
    @njit(nogil=True, cache=True)
    def _njit_compute_ema(arr: np.ndarray, period: int) -> np.ndarray:
        n = arr.shape[0]
        out = np.empty(n, dtype=np.float64)
        if n == 0 or period <= 0:
            return out[:0]
        out[0] = arr[0]
        alpha = 2.0 / (float(period) + 1.0)
        ema = out[0]
        for i in range(1, n):
            ema = alpha * arr[i] + (1.0 - alpha) * ema
            out[i] = ema
        return out

    @njit(nogil=True, cache=True)
    def _njit_compute_ema_sma_seeded(arr: np.ndarray, length: int) -> np.ndarray:
        n = arr.shape[0]
        if n < length or length <= 0:
            return np.empty(0, dtype=np.float64)
        out_len = n - length + 1
        out = np.empty(out_len, dtype=np.float64)
        total = 0.0
        for i in range(length):
            total += arr[i]
        ema = total / float(length)
        out[0] = ema
        alpha = 2.0 / (float(length) + 1.0)
        for i in range(length, n):
            ema = arr[i] * alpha + ema * (1.0 - alpha)
            out[i - length + 1] = ema
        return out

    @njit(nogil=True, cache=True)
    def _njit_compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
        n = closes.shape[0]
        out = np.empty(n, dtype=np.float64)
        if n == 0 or period <= 0:
            return out[:0]
        prev_atr = highs[0] - lows[0]
        out[0] = prev_atr
        for i in range(1, n):
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i - 1])
            tr3 = abs(lows[i] - closes[i - 1])
            tr = tr1 if tr1 >= tr2 else tr2
            if tr3 > tr:
                tr = tr3
            prev_atr = (prev_atr * (period - 1) + tr) / float(period)
            out[i] = prev_atr
        return out

    @njit(nogil=True, cache=True)
    def _njit_atr_pct_sma(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
        n = closes.shape[0]
        if n <= 1 or period <= 0:
            return -1.0
        cur_close = closes[n - 1]
        if cur_close <= 0.0:
            return -1.0
        trs = np.empty(n - 1, dtype=np.float64)
        cnt = 0
        for i in range(1, n):
            h = highs[i]
            l = lows[i]
            pc = closes[i - 1]
            if h <= 0.0 or l <= 0.0 or pc <= 0.0:
                continue
            tr1 = h - l
            tr2 = abs(h - pc)
            tr3 = abs(l - pc)
            tr = tr1 if tr1 >= tr2 else tr2
            if tr3 > tr:
                tr = tr3
            trs[cnt] = tr
            cnt += 1
        if cnt < period:
            return -1.0
        s = 0.0
        for i in range(cnt - period, cnt):
            s += trs[i]
        atr_val = s / float(period)
        return (atr_val / cur_close) * 100.0


class NativeIndicatorsBackend:
    """
    Высокопроизводительный вычислительный бэкенд индикаторов из CPP/kernel_indicators.hpp:
    1) При наличии compiled shared library -> вызов C ABI через ctypes;
    2) На Windows/Linux без GCC -> нативный машинный код через Numba @njit(cache=True);
    3) В качестве резерва -> чистый NumPy.
    """

    def __init__(self) -> None:
        self._cpp_lib: Optional[ctypes.CDLL] = _try_load_cpp_lib()
        if self._cpp_lib is not None:
            self.backend_name = "cpp_dll"
        elif _HAS_NUMBA:
            self.backend_name = "numba_jit"
        else:
            self.backend_name = "numpy"

    @staticmethod
    def _as_f64(seq: Sequence[float] | np.ndarray) -> np.ndarray:
        if isinstance(seq, np.ndarray):
            return np.ascontiguousarray(seq, dtype=np.float64)
        return np.ascontiguousarray(seq, dtype=np.float64)

    def fast_ema(self, prices: Sequence[float] | np.ndarray, length: int) -> List[float]:
        """SMA-seeded EMA (100% паритет с NativeMath.fast_ema)."""
        if length <= 0:
            return []
        arr = self._as_f64(prices)
        n = len(arr)
        if n < length:
            return []

        if self._cpp_lib is not None:
            out = np.empty(n - length + 1, dtype=np.float64)
            c_dp = ctypes.POINTER(ctypes.c_double)
            written = self._cpp_lib.hron_compute_ema_sma_seeded(
                arr.ctypes.data_as(c_dp),
                ctypes.c_int(n),
                ctypes.c_int(length),
                out.ctypes.data_as(c_dp),
            )
            return out[:written].tolist()

        if _HAS_NUMBA:
            return _njit_compute_ema_sma_seeded(arr, int(length)).tolist()

        k = 2.0 / (length + 1.0)
        out_py = [float(np.mean(arr[:length]))]
        ema = out_py[0]
        for p in arr[length:]:
            ema = p * k + ema * (1.0 - k)
            out_py.append(ema)
        return out_py

    def compute_ema(self, prices: Sequence[float] | np.ndarray, period: int) -> List[float]:
        """Прямая реализация hron::kernel::compute_ema."""
        if period <= 0:
            return []
        arr = self._as_f64(prices)
        n = len(arr)
        if n == 0:
            return []
        if self._cpp_lib is not None:
            out = np.empty(n, dtype=np.float64)
            c_dp = ctypes.POINTER(ctypes.c_double)
            written = self._cpp_lib.hron_compute_ema(
                arr.ctypes.data_as(c_dp),
                ctypes.c_int(n),
                ctypes.c_int(period),
                out.ctypes.data_as(c_dp),
            )
            return out[:written].tolist()
        if _HAS_NUMBA:
            return _njit_compute_ema(arr, int(period)).tolist()
        out_py = [float(arr[0])]
        alpha = 2.0 / (period + 1.0)
        ema = out_py[0]
        for p in arr[1:]:
            ema = alpha * float(p) + (1.0 - alpha) * ema
            out_py.append(ema)
        return out_py

    def compute_atr(
        self,
        highs: Sequence[float] | np.ndarray,
        lows: Sequence[float] | np.ndarray,
        closes: Sequence[float] | np.ndarray,
        period: int,
    ) -> List[float]:
        """Прямая реализация hron::kernel::compute_atr (Уайлдер)."""
        if period <= 0:
            return []
        h_arr = self._as_f64(highs)
        l_arr = self._as_f64(lows)
        c_arr = self._as_f64(closes)
        n = min(len(h_arr), len(l_arr), len(c_arr))
        if n == 0:
            return []
        if self._cpp_lib is not None:
            out = np.empty(n, dtype=np.float64)
            c_dp = ctypes.POINTER(ctypes.c_double)
            written = self._cpp_lib.hron_compute_atr(
                h_arr.ctypes.data_as(c_dp),
                l_arr.ctypes.data_as(c_dp),
                c_arr.ctypes.data_as(c_dp),
                ctypes.c_int(n),
                ctypes.c_int(period),
                out.ctypes.data_as(c_dp),
            )
            return out[:written].tolist()
        if _HAS_NUMBA:
            return _njit_compute_atr(h_arr[:n], l_arr[:n], c_arr[:n], int(period)).tolist()
        out_py = [float(h_arr[0] - l_arr[0])]
        prev = out_py[0]
        for i in range(1, n):
            tr = max(
                float(h_arr[i] - l_arr[i]),
                abs(float(h_arr[i] - c_arr[i - 1])),
                abs(float(l_arr[i] - c_arr[i - 1])),
            )
            prev = (prev * (period - 1) + tr) / float(period)
            out_py.append(prev)
        return out_py

    def atr_pct_sma(
        self,
        highs: Sequence[float] | np.ndarray,
        lows: Sequence[float] | np.ndarray,
        closes: Sequence[float] | np.ndarray,
        period: int,
    ) -> Optional[float]:
        """ATR% на основе SMA последних period TR (100% паритет с ATRProvider.get_atr_pct)."""
        if period <= 0:
            return None
        h_arr = self._as_f64(highs)
        l_arr = self._as_f64(lows)
        c_arr = self._as_f64(closes)
        n = min(len(h_arr), len(l_arr), len(c_arr))
        if n <= 1:
            return None

        if self._cpp_lib is not None:
            c_dp = ctypes.POINTER(ctypes.c_double)
            val = float(
                self._cpp_lib.hron_compute_atr_pct_sma(
                    h_arr.ctypes.data_as(c_dp),
                    l_arr.ctypes.data_as(c_dp),
                    c_arr.ctypes.data_as(c_dp),
                    ctypes.c_int(n),
                    ctypes.c_int(period),
                )
            )
            return val if val >= 0.0 else None

        if _HAS_NUMBA:
            val = float(_njit_atr_pct_sma(h_arr[:n], l_arr[:n], c_arr[:n], int(period)))
            return val if val >= 0.0 else None

        trs = []
        for i in range(1, n):
            h, l, pc = float(h_arr[i]), float(l_arr[i]), float(c_arr[i - 1])
            if h <= 0.0 or l <= 0.0 or pc <= 0.0:
                continue
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < period:
            return None
        cur_close = float(c_arr[n - 1])
        if cur_close <= 0.0:
            return None
        return (sum(trs[-period:]) / float(period) / cur_close) * 100.0


NATIVE_INDICATORS = NativeIndicatorsBackend()

