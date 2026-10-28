# ==============================================================================
# Path: ANALYTICS/equity_trajectory.py
# Role: Построитель траектории эквити, хранение прогресса локальных пиков/минимумов и живых сэмплов
# ==============================================================================

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Кэш последней записанной точки сэмпла эквити в памяти: {stream_key: (ts_ms, net_profit)}
_LAST_EQUITY_SAMPLE_CACHE: Dict[str, Tuple[int, float]] = {}


def resolve_equity_history_path(ledgers_dir: Path, universe_id: Optional[str] = None) -> Path:
    """Возвращает путь к CSV-файлу истории точек эквити для купола или портфеля."""
    if universe_id and universe_id != "default":
        return Path(ledgers_dir) / f"equity_history_{universe_id}.csv"
    return Path(ledgers_dir) / "equity_history.csv"


def update_local_extrema(
    data: dict,
    now_ms: int,
    net_profit: float,
    start_balance: float = 0.0,
    reversal_threshold_usdt: Optional[float] = None,
    max_pivots: int = 1500,
) -> bool:
    """
    Отслеживает и персистентно сохраняет хронологический прогресс ВСЕХ локальных максимумов (PEAK)
    и локальных минимумов просадки (TROUGH) по алгоритму ZigZag без затирания прошлых волн.
    Возвращает True, если был зафиксирован разворот (запечатан предыдущий экстремум).
    """
    if reversal_threshold_usdt is None:
        base_bal = abs(float(start_balance or data.get("start_balance_usdt") or 0.0))
        reversal_threshold_usdt = max(0.20, round(base_bal * 0.0015, 4))

    raw_extrema = data.get("local_extrema")
    extrema: List[dict] = []
    if isinstance(raw_extrema, list):
        for item in raw_extrema:
            if isinstance(item, dict) and "ts" in item and "np" in item and item.get("type") in ("PEAK", "TROUGH"):
                try:
                    extrema.append({
                        "ts": int(item["ts"]),
                        "np": round(float(item["np"]), 4),
                        "type": str(item["type"]),
                    })
                except (TypeError, ValueError):
                    continue

    # Bootstrap из глобальных полей аналитики, если массив local_extrema еще не создавался
    if not extrema:
        init_bal = float(start_balance or data.get("start_balance_usdt") or 0.0)
        seed_candidates: List[Tuple[int, float, str]] = []

        p_ts = data.get("peak_net_ts") or data.get("iteration_peak_ts")
        p_bal = data.get("peak_balance_usdt")
        p_np = data.get("peak_net_profit_usdt")
        if p_np is None and p_bal is not None:
            p_np = float(p_bal) - init_bal
        if p_ts and p_np is not None and float(p_np) >= reversal_threshold_usdt:
            seed_candidates.append((int(p_ts), round(float(p_np), 4), "PEAK"))

        m_ts = data.get("min_balance_ts")
        m_bal = data.get("min_balance_usdt")
        if m_ts and m_bal is not None:
            t_np = round(float(m_bal) - init_bal, 4)
            if t_np <= -reversal_threshold_usdt:
                seed_candidates.append((int(m_ts), t_np, "TROUGH"))

        seed_candidates.sort(key=lambda x: x[0])
        for s_ts, s_np, s_type in seed_candidates:
            if not extrema or extrema[-1]["type"] != s_type:
                extrema.append({"ts": s_ts, "np": s_np, "type": s_type})

    np_val = round(float(net_profit), 4)
    ts_val = int(now_ms)
    sealed_pivot = False

    if not extrema:
        if np_val >= reversal_threshold_usdt:
            extrema.append({"ts": ts_val, "np": np_val, "type": "PEAK"})
            sealed_pivot = True
        elif np_val <= -reversal_threshold_usdt:
            extrema.append({"ts": ts_val, "np": np_val, "type": "TROUGH"})
            sealed_pivot = True
    else:
        last = extrema[-1]
        safe_ts = max(int(last["ts"]), ts_val)
        if last["type"] == "PEAK":
            if np_val >= float(last["np"]):
                last["np"] = np_val
                last["ts"] = safe_ts
            elif (float(last["np"]) - np_val) >= reversal_threshold_usdt:
                extrema.append({
                    "ts": max(int(last["ts"]) + 1, ts_val),
                    "np": np_val,
                    "type": "TROUGH",
                })
                sealed_pivot = True
        else:  # last["type"] == "TROUGH"
            if np_val <= float(last["np"]):
                last["np"] = np_val
                last["ts"] = safe_ts
            elif (np_val - float(last["np"])) >= reversal_threshold_usdt:
                extrema.append({
                    "ts": max(int(last["ts"]) + 1, ts_val),
                    "np": np_val,
                    "type": "PEAK",
                })
                sealed_pivot = True

    if len(extrema) > max_pivots:
        extrema = extrema[-max_pivots:]

    data["local_extrema"] = extrema
    return sealed_pivot


def append_equity_sample(
    ledgers_dir: Path,
    universe_id: Optional[str],
    now_ms: int,
    realized_net: float,
    unrealized: float,
    net_profit: float,
    balance: float,
    min_delta_usdt: float = 0.10,
    heartbeat_sec: float = 300.0,
    force: bool = False,
    event: str = "SAMPLE",
) -> bool:
    """
    Дописывает реальную точку эквити в append-only CSV при значимом сдвиге баланса,
    закрытии сделки, развороте экстремума или по таймеру heartbeat.
    Исключает грубое сглаживание и навсегда сохраняет реальный ход эквити.
    """
    history_path = resolve_equity_history_path(ledgers_dir, universe_id)
    cache_key = str(history_path.resolve())

    if cache_key not in _LAST_EQUITY_SAMPLE_CACHE and history_path.exists():
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                last_line = ""
                for line in f:
                    if line.strip() and not line.startswith("Timestamp_MS"):
                        last_line = line.strip()
                if last_line:
                    parts = last_line.split(";")
                    if len(parts) >= 5:
                        _LAST_EQUITY_SAMPLE_CACHE[cache_key] = (int(parts[0]), float(parts[4]))
        except Exception:
            pass

    prev = _LAST_EQUITY_SAMPLE_CACHE.get(cache_key)
    ts_int = int(now_ms)
    np_round = round(float(net_profit), 4)

    should_write = False
    if force or prev is None:
        should_write = True
    else:
        prev_ts, prev_np = prev
        if ts_int > prev_ts:
            delta_np = abs(np_round - prev_np)
            elapsed_ms = ts_int - prev_ts
            if delta_np >= min_delta_usdt:
                should_write = True
            elif elapsed_ms >= int(heartbeat_sec * 1000) and (delta_np >= 0.01 or abs(unrealized) > 0.01):
                should_write = True

    if not should_write:
        return False

    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not history_path.exists() or history_path.stat().st_size == 0
        utc_str = datetime.fromtimestamp(ts_int / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(history_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            if write_header:
                writer.writerow([
                    "Timestamp_MS",
                    "UTC_Time",
                    "Realized_Net_USDT",
                    "Unrealized_USDT",
                    "Net_Profit_USDT",
                    "Balance_USDT",
                    "Event",
                ])
            writer.writerow([
                ts_int,
                utc_str,
                round(float(realized_net), 4),
                round(float(unrealized), 4),
                np_round,
                round(float(balance), 4),
                event,
            ])
        _LAST_EQUITY_SAMPLE_CACHE[cache_key] = (ts_int, np_round)
        return True
    except Exception:
        return False


def read_equity_samples(
    ledgers_dir: Path,
    universe_id: Optional[str] = None,
    start_dt: Optional[datetime] = None,
) -> List[Tuple[datetime, float]]:
    """Читает хронологический ряд реальных сэмплов (dt_utc_naive, net_profit_usdt) с диска."""
    history_path = resolve_equity_history_path(ledgers_dir, universe_id)
    if not history_path.exists():
        return []

    samples: List[Tuple[datetime, float]] = []
    try:
        with open(history_path, mode="r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=";")
            header = next(reader, None)
            if not header:
                return []
            for row in reader:
                if len(row) < 5:
                    continue
                try:
                    ts_ms = int(row[0])
                    np_val = float(row[4])
                    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
                    if start_dt and dt < start_dt:
                        continue
                    samples.append((dt, np_val))
                except (ValueError, TypeError):
                    continue
    except Exception:
        return []

    samples.sort(key=lambda x: x[0])
    return samples


class EquityTrajectoryBuilder:
    """
    Строит непрерывную хронологическую траекторию баланса и чистого Net Profit:
    1) Использует реальные записанные сэмплы эквити (equity_samples), если они накоплены.
    2) Гарантированно сохраняет прогресс ВСЕХ локальных максимумов (PEAK) и локальных минимумов
       просадки (TROUGH) из local_extrema — новый ATH или новое дно никогда не затирают старые волны.
    3) На исторических участках без сэмплов учитывает реальный знак закрытых сделок без линейного схлопывания.
    """

    def __init__(
        self,
        start_balance: float,
        net_profit_final: float,
        raw_trades: List[Tuple[datetime, float]],
        start_dt: Optional[datetime] = None,
        peak_np: Optional[float] = None,
        peak_dt: Optional[datetime] = None,
        trough_np: Optional[float] = None,
        trough_dt: Optional[datetime] = None,
        now_dt: Optional[datetime] = None,
        local_extrema: Optional[List[dict]] = None,
        equity_samples: Optional[List[Tuple[datetime, float]]] = None,
    ):
        self.start_balance: float = float(start_balance)
        self.net_profit_final: float = float(net_profit_final)
        self.raw_trades: List[Tuple[datetime, float]] = sorted(raw_trades, key=lambda x: x[0])
        self.start_dt: Optional[datetime] = start_dt
        self.peak_np: Optional[float] = float(peak_np) if peak_np is not None else None
        self.peak_dt: Optional[datetime] = peak_dt
        self.trough_np: Optional[float] = float(trough_np) if trough_np is not None else None
        self.trough_dt: Optional[datetime] = trough_dt
        self.now_dt: datetime = now_dt if now_dt else datetime.now(timezone.utc).replace(tzinfo=None)
        self.local_extrema: List[dict] = list(local_extrema) if isinstance(local_extrema, list) else []
        self.equity_samples: List[Tuple[datetime, float]] = (
            sorted(equity_samples, key=lambda x: x[0]) if equity_samples else []
        )

    def _collect_anchors(self, t_start: datetime, t_end: datetime, end_np: float) -> List[Tuple[datetime, float]]:
        """Собирает все локальные пики, локальные минимумы просадки и граничные точки без взаимного затирания."""
        anchors: List[Tuple[datetime, float]] = [(t_start, 0.0)]

        # 1. Все сохраненные локальные пики и локальные минимумы из ZigZag истории
        parsed_extrema: List[Tuple[datetime, float, str]] = []
        for ext in self.local_extrema:
            if not isinstance(ext, dict) or "ts" not in ext or "np" not in ext:
                continue
            try:
                dt_ext = datetime.fromtimestamp(int(ext["ts"]) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
                val_ext = float(ext["np"])
                etype = str(ext.get("type", ""))
                if t_start <= dt_ext <= t_end:
                    parsed_extrema.append((dt_ext, val_ext, etype))
            except (TypeError, ValueError, OSError):
                continue

        for dt_e, val_e, _ in parsed_extrema:
            anchors.append((dt_e, val_e))

        # 2. Глобальный пик и глобальное дно (если их еще нет среди local_extrema рядом по времени)
        has_peak = self.peak_np is not None and self.peak_np > 0.0001
        has_trough = self.trough_np is not None and self.trough_np < -0.0001

        t_peak = self.peak_dt
        if has_peak and not t_peak:
            if abs(self.net_profit_final - float(self.peak_np)) < 0.01:
                t_peak = self.now_dt
            elif self.raw_trades:
                cum = 0.0
                best_c = -1e9
                best_t = self.raw_trades[0][0]
                for dt_tr, pnl_tr in self.raw_trades:
                    cum += pnl_tr
                    if cum > best_c:
                        best_c = cum
                        best_t = dt_tr
                t_peak = best_t
            else:
                t_peak = t_start + (t_end - t_start) * 0.33

        t_trough = self.trough_dt
        if has_trough and not t_trough:
            if abs(self.net_profit_final - float(self.trough_np)) < 0.01:
                t_trough = self.now_dt
            elif self.raw_trades:
                cum = 0.0
                worst_c = 1e9
                worst_t = self.raw_trades[-1][0]
                for dt_tr, pnl_tr in self.raw_trades:
                    cum += pnl_tr
                    if cum < worst_c:
                        worst_c = cum
                        worst_t = dt_tr
                t_trough = worst_t
            else:
                t_trough = t_start + (t_end - t_start) * 0.67

        if has_peak and t_peak:
            clamped_p = max(t_start, min(self.now_dt, t_peak))
            if clamped_p <= t_end:
                already_covered = any(
                    et == "PEAK" and abs((clamped_p - dt_e).total_seconds()) <= 120 and abs(val_e - float(self.peak_np)) < 0.05
                    for dt_e, val_e, et in parsed_extrema
                )
                if not already_covered:
                    anchors.append((clamped_p, float(self.peak_np)))

        if has_trough and t_trough:
            clamped_t = max(t_start, min(self.now_dt, t_trough))
            if clamped_t <= t_end:
                already_covered = any(
                    et == "TROUGH" and abs((clamped_t - dt_e).total_seconds()) <= 120 and abs(val_e - float(self.trough_np)) < 0.05
                    for dt_e, val_e, et in parsed_extrema
                )
                if not already_covered:
                    anchors.append((clamped_t, float(self.trough_np)))

        anchors.sort(key=lambda x: x[0])

        # Разрешение микро-коллизий времени без потери ни одного экстремума
        distinct: List[Tuple[datetime, float]] = [anchors[0]]
        for dt_item, val_item in anchors[1:]:
            prev_dt, prev_val = distinct[-1]
            if dt_item > prev_dt:
                distinct.append((dt_item, val_item))
            elif abs(val_item - prev_val) > 0.001:
                shifted = prev_dt + timedelta(seconds=1)
                if shifted < t_end:
                    distinct.append((shifted, val_item))
                else:
                    distinct[-1] = (prev_dt - timedelta(seconds=1), prev_val)
                    distinct.append((dt_item, val_item))

        # Финальная точка диапазона
        last_dt, last_val = distinct[-1]
        if last_dt < t_end - timedelta(seconds=2):
            distinct.append((t_end, end_np))
        elif abs(last_val - end_np) > 0.001:
            distinct[-1] = (t_end - timedelta(seconds=5), last_val)
            distinct.append((t_end, end_np))
        else:
            distinct[-1] = (t_end, end_np)

        return distinct

    def _reconstruct_segment_points(
        self,
        anchors: List[Tuple[datetime, float]],
    ) -> List[Tuple[datetime, float]]:
        """
        Строит точки между опорными экстремумами с сохранением знака и амплитуды каждой закрытой сделки.
        Прибыльные сделки на входе в плавающую просадку дают реальный рост (не обнуляются в прямую линию).
        """
        pts: List[Tuple[datetime, float]] = []

        for idx in range(len(anchors) - 1):
            t_a, v_a = anchors[idx]
            t_b, v_b = anchors[idx + 1]

            if not pts:
                pts.append((t_a, round(v_a, 4)))

            seg_trades = [tr for tr in self.raw_trades if t_a < tr[0] < t_b]
            if not seg_trades:
                if t_b > pts[-1][0]:
                    pts.append((t_b, round(v_b, 4)))
                else:
                    pts[-1] = (pts[-1][0], round(v_b, 4))
                continue

            t_span = max(1.0, (t_b - t_a).total_seconds())
            total_trade_pnl = sum(pnl for _, pnl in seg_trades)
            target_delta = v_b - v_a
            unclosed_drift = target_delta - total_trade_pnl

            # Если сделки шли в плюс, а сегмент заканчивается в глубокой плавающей просадке —
            # плавающая просадка нарастает нелинейно во второй половине окна, не стирая утренний профит сделок.
            drift_power = 2.2 if (total_trade_pnl > 0.0 and target_delta < 0.0) else 1.0

            cum_pnl = 0.0
            for dt_tr, pnl_tr in seg_trades:
                cum_pnl += pnl_tr
                if dt_tr <= pts[-1][0]:
                    continue
                t_ratio = min(1.0, max(0.0, (dt_tr - t_a).total_seconds() / t_span))
                val = v_a + cum_pnl + unclosed_drift * (t_ratio ** drift_power)
                pts.append((dt_tr, round(val, 4)))

            if t_b > pts[-1][0]:
                pts.append((t_b, round(v_b, 4)))
            else:
                pts[-1] = (pts[-1][0], round(v_b, 4))

        return pts

    def build_trajectory(self) -> Tuple[List[datetime], List[float], List[float]]:
        """
        Формирует хронологические списки (times, net_profits, balances) для отрисовки графика эквити.
        """
        now = self.now_dt
        if self.start_dt:
            t_start = self.start_dt
        elif self.equity_samples:
            t_start = self.equity_samples[0][0] - timedelta(minutes=10)
        elif self.raw_trades:
            t_start = self.raw_trades[0][0] - timedelta(minutes=30)
        else:
            t_start = now - timedelta(hours=3)

        if t_start >= now:
            t_start = now - timedelta(minutes=10)

        valid_samples = [(dt, val) for dt, val in self.equity_samples if t_start <= dt <= now]

        if valid_samples:
            first_sample_dt, first_sample_np = valid_samples[0]
            combined: List[Tuple[datetime, float]] = []

            # Если до начала записи live-сэмплов уже была история сделок/экстремумов — бесшовно реконструируем префикс
            has_pre_history = (
                first_sample_dt > t_start + timedelta(minutes=5)
                and (
                    any(tr[0] < first_sample_dt for tr in self.raw_trades)
                    or abs(first_sample_np) > 0.05
                )
            )
            if has_pre_history:
                pre_anchors = self._collect_anchors(t_start, first_sample_dt, first_sample_np)
                combined.extend(self._reconstruct_segment_points(pre_anchors))
            elif first_sample_dt > t_start + timedelta(seconds=5):
                combined.append((t_start, 0.0))

            # Добавляем реальные измеренные точки эквити
            for dt_s, val_s in valid_samples:
                if not combined or dt_s > combined[-1][0]:
                    combined.append((dt_s, round(val_s, 4)))
                else:
                    combined[-1] = (combined[-1][0], round(val_s, 4))

            # Гарантируем присутствие всех запечатанных экстремумов (PEAK / TROUGH), если они попали между сэмплами
            post_anchors = self._collect_anchors(first_sample_dt, now, self.net_profit_final)
            for dt_a, val_a in post_anchors[1:-1]:
                near_existing = any(
                    abs((dt_a - dt_c).total_seconds()) <= 30 and abs(val_a - val_c) <= 0.05
                    for dt_c, val_c in combined
                )
                if not near_existing:
                    combined.append((dt_a, round(val_a, 4)))

            combined.sort(key=lambda x: x[0])

            # Дедупликация одинаковых меток времени
            deduped: List[Tuple[datetime, float]] = []
            for dt_p, val_p in combined:
                if not deduped or dt_p > deduped[-1][0]:
                    deduped.append((dt_p, val_p))
                elif abs(val_p) > abs(deduped[-1][1]):
                    deduped[-1] = (dt_p, val_p)

            # Точная фиксация текущей финальной точки
            if deduped[-1][0] < now - timedelta(seconds=2):
                deduped.append((now, round(self.net_profit_final, 4)))
            elif abs(deduped[-1][1] - self.net_profit_final) > 0.001:
                deduped[-1] = (now - timedelta(seconds=3), deduped[-1][1])
                deduped.append((now, round(self.net_profit_final, 4)))
            else:
                deduped[-1] = (now, round(self.net_profit_final, 4))

            times = [p[0] for p in deduped]
            net_profits = [p[1] for p in deduped]
        else:
            anchors = self._collect_anchors(t_start, now, self.net_profit_final)
            pts = self._reconstruct_segment_points(anchors)
            times = [p[0] for p in pts]
            net_profits = [p[1] for p in pts]

        balances = [round(self.start_balance + np_val, 4) for np_val in net_profits]
        return times, net_profits, balances
