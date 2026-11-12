// ==============================================================================
// FILE: CORE/STRATEGY/CPP/kernel_indicators_bridge.cpp
// ROLE: C ABI DLL/SO bridge exposing kernel_indicators.hpp symbols to Python ctypes
// ==============================================================================

#include "kernel_indicators.hpp"

#ifdef _WIN32
#define HRON_API __declspec(dllexport)
#else
#define HRON_API __attribute__((visibility("default")))
#endif

extern "C" {

HRON_API double hron_estimate_nadaraya_watson(
    const double* prices,
    int length,
    const double* weights,
    int window_size
) {
    return c_estimate_nadaraya_watson(prices, length, weights, window_size);
}

HRON_API int hron_compute_ema(
    const double* prices,
    int length,
    int period,
    double* out_ema
) {
    return c_compute_ema(prices, length, period, out_ema);
}

HRON_API int hron_compute_ema_sma_seeded(
    const double* prices,
    int length,
    int period,
    double* out_ema
) {
    return c_compute_ema_sma_seeded(prices, length, period, out_ema);
}

HRON_API int hron_compute_atr(
    const double* highs,
    const double* lows,
    const double* closes,
    int length,
    int period,
    double* out_atr
) {
    return c_compute_atr(highs, lows, closes, length, period, out_atr);
}

HRON_API double hron_compute_atr_pct_sma(
    const double* highs,
    const double* lows,
    const double* closes,
    int length,
    int period
) {
    return c_compute_atr_pct_sma(highs, lows, closes, length, period);
}

} // extern "C"

