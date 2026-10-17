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
}
