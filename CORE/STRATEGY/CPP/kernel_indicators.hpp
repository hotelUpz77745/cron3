// ==============================================================================
// FILE: CORE/STRATEGY/CPP/kernel_indicators.hpp
// ROLE: High-performance C++20 kernel & indicator math engine for HRON_3
// ==============================================================================

#pragma once

#include <vector>
#include <cmath>
#include <algorithm>
#include <numeric>
#include <cstdint>

namespace hron::kernel {

/**
 * @brief Computes precomputed weights for Rational Quadratic, Gaussian, or Epanechnikov kernel.
 */
inline std::vector<double> precompute_kernel_weights(
    int window_size,
    double bandwidth,
    int kernel_type = 0, // 0 = Rational Quadratic, 1 = Gaussian, 2 = Epanechnikov
    double alpha = 1.0
) {
    std::vector<double> w(window_size);
    double sum = 0.0;
    double h = std::max(1.0, bandwidth);

    for (int lag = 0; lag < window_size; ++lag) {
        double r = static_cast<double>(lag);
        if (kernel_type == 1) {
            // Gaussian: K(u) = exp(-0.5 * (u/h)^2)
            w[lag] = std::exp(-0.5 * std::pow(r / h, 2.0));
        } else if (kernel_type == 2) {
            // Epanechnikov: K(u) = 0.75 * (1 - u^2)
            double u = r / (h * std::sqrt(5.0));
            w[lag] = (std::abs(u) <= 1.0) ? (0.75 * (1.0 - u * u)) : 0.0;
        } else {
            // Rational Quadratic: K(r) = (1 + r^2 / (2 * alpha * h^2)) ^ (-alpha)
            w[lag] = std::pow(1.0 + (r * r) / (2.0 * alpha * h * h), -alpha);
        }
        sum += w[lag];
    }

    if (sum > 0.0) {
        for (int i = 0; i < window_size; ++i) {
            w[i] /= sum;
        }
    } else {
        std::fill(w.begin(), w.end(), 1.0 / window_size);
    }
    return w;
}

/**
 * @brief Fast non-repainting Nadaraya-Watson estimate at the current bar.
 */
inline double estimate_at_bar(
    const double* prices,
    size_t length,
    const double* weights,
    size_t window_size
) {
    if (length < window_size) {
        if (length == 0) return 0.0;
        double sum = 0.0;
        for (size_t i = 0; i < length; ++i) sum += prices[i];
        return sum / static_cast<double>(length);
    }

    double acc = 0.0;
    for (size_t i = 0; i < window_size; ++i) {
        acc += weights[i] * prices[length - 1 - i];
    }
    return acc;
}

/**
 * @brief Exponential Moving Average series calculation.
 */
inline std::vector<double> compute_ema(const std::vector<double>& prices, int period) {
    size_t n = prices.size();
    if (n == 0) return {};
    std::vector<double> ema(n);
    ema[0] = prices[0];
    double alpha = 2.0 / (period + 1.0);
    for (size_t i = 1; i < n; ++i) {
        ema[i] = alpha * prices[i] + (1.0 - alpha) * ema[i - 1];
    }
    return ema;
}

/**
 * @brief Average True Range series calculation.
 */
inline std::vector<double> compute_atr(
    const std::vector<double>& highs,
    const std::vector<double>& lows,
    const std::vector<double>& closes,
    int period
) {
    size_t n = closes.size();
    if (n == 0) return {};
    std::vector<double> atr(n);
    atr[0] = highs[0] - lows[0];
    for (size_t i = 1; i < n; ++i) {
        double tr1 = highs[i] - lows[i];
        double tr2 = std::abs(highs[i] - closes[i - 1]);
        double tr3 = std::abs(lows[i] - closes[i - 1]);
        double tr = std::max({tr1, tr2, tr3});
        atr[i] = (atr[i - 1] * (period - 1) + tr) / period;
    }
    return atr;
}

} // namespace hron::kernel

extern "C" {
    inline double c_estimate_nadaraya_watson(
        const double* prices,
        int length,
        const double* weights,
        int window_size
    ) {
        return hron::kernel::estimate_at_bar(prices, length, weights, window_size);
    }

    inline int c_compute_ema(
        const double* prices,
        int length,
        int period,
        double* out_ema
    ) {
        if (!prices || !out_ema || length <= 0 || period <= 0) return 0;
        out_ema[0] = prices[0];
        double alpha = 2.0 / (static_cast<double>(period) + 1.0);
        double ema = out_ema[0];
        for (int i = 1; i < length; ++i) {
            ema = alpha * prices[i] + (1.0 - alpha) * ema;
            out_ema[i] = ema;
        }
        return length;
    }

    inline int c_compute_ema_sma_seeded(
        const double* prices,
        int length,
        int period,
        double* out_ema
    ) {
        if (!prices || !out_ema || length <= 0 || period <= 0 || length < period) return 0;
        double sum = 0.0;
        for (int i = 0; i < period; ++i) sum += prices[i];
        double ema = sum / static_cast<double>(period);
        out_ema[0] = ema;
        double alpha = 2.0 / (static_cast<double>(period) + 1.0);
        int out_idx = 1;
        for (int i = period; i < length; ++i) {
            ema = prices[i] * alpha + ema * (1.0 - alpha);
            out_ema[out_idx++] = ema;
        }
        return out_idx;
    }

    inline int c_compute_atr(
        const double* highs,
        const double* lows,
        const double* closes,
        int length,
        int period,
        double* out_atr
    ) {
        if (!highs || !lows || !closes || !out_atr || length <= 0 || period <= 0) return 0;
        double prev_atr = highs[0] - lows[0];
        out_atr[0] = prev_atr;
        for (int i = 1; i < length; ++i) {
            double tr1 = highs[i] - lows[i];
            double tr2 = std::abs(highs[i] - closes[i - 1]);
            double tr3 = std::abs(lows[i] - closes[i - 1]);
            double tr = std::max({tr1, tr2, tr3});
            prev_atr = (prev_atr * (period - 1) + tr) / static_cast<double>(period);
            out_atr[i] = prev_atr;
        }
        return length;
    }

    inline double c_compute_atr_pct_sma(
        const double* highs,
        const double* lows,
        const double* closes,
        int length,
        int period
    ) {
        if (!highs || !lows || !closes || length <= 1 || period <= 0) return -1.0;
        std::vector<double> trs;
        trs.reserve(static_cast<size_t>(length));
        for (int i = 1; i < length; ++i) {
            double h = highs[i];
            double l = lows[i];
            double pc = closes[i - 1];
            if (h <= 0.0 || l <= 0.0 || pc <= 0.0) continue;
            double tr = std::max({h - l, std::abs(h - pc), std::abs(l - pc)});
            trs.push_back(tr);
        }
        if (static_cast<int>(trs.size()) < period) return -1.0;
        double tr_sum = 0.0;
        size_t start = trs.size() - static_cast<size_t>(period);
        for (size_t i = start; i < trs.size(); ++i) tr_sum += trs[i];
        double cur_close = closes[length - 1];
        if (cur_close <= 0.0) return -1.0;
        double atr_val = tr_sum / static_cast<double>(period);
        return (atr_val / cur_close) * 100.0;
    }
}
