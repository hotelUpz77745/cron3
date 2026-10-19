# ============================================================
# FILE: API/IPC/streamer_hub.py
# ROLE: Master-Elected Cross-Process Market Data Streamer (Shared WebSocket over IPC)
# ============================================================

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

from API.BINANCE.price_stream import BinanceHotPriceStream, HotPriceTick
from API.BINANCE.public import BinancePublic

from c_log import UnifiedLogger
logger = UnifiedLogger("StreamerIPC")

IPC_DIR = Path(__file__).resolve().parent
LOCK_FILE = IPC_DIR / "streamer.lock"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48123
LEASE_TIMEOUT_SEC = 10.0
HEARTBEAT_INTERVAL_SEC = 2.0


def _is_pid_alive(pid: int) -> bool:
    """Проверяет существование процесса в ОС."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_port_in_use(host: str, port: int) -> bool:
    """Проверяет, слушает ли уже какой-либо процесс этот порт."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def discover_cluster_symbols(extra_symbols: Optional[List[str]] = None) -> List[str]:
    """
    Собирает и склеивает списки монет ото всех трех ботов:
    - cron3
    - cron3Papper
    - cron3PapperUniverses
    Возвращает объединенный отсортированный список уникальных символов.
    """
    collected: Set[str] = set()
    if extra_symbols:
        for s in extra_symbols:
            if isinstance(s, str) and s.strip():
                collected.add(s.strip().upper())

    # Корень проекта HRON_3 (parents[2] от API/IPC/streamer_hub.py)
    root_dir = Path(__file__).resolve().parents[2]
    bot_cfg_paths = [
        root_dir / "cron3" / "CFG" / "app.json",
        root_dir / "cron3PapperUniverses" / "CFG" / "app.json",
    ]

    for cfg_path in bot_cfg_paths:
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                syms = data.get("symbols", [])
                if isinstance(syms, list):
                    for s in syms:
                        if isinstance(s, str) and s.strip():
                            collected.add(s.strip().upper())
                elif isinstance(syms, dict):
                    for s in syms.keys():
                        if isinstance(s, str) and s.strip():
                            collected.add(s.strip().upper())

                # Поддержка секции active_strategies (для боевого cron3)
                active_strats = data.get("active_strategies", {})
                if isinstance(active_strats, dict):
                    for strat_id, s_cfg in active_strats.items():
                        if isinstance(s_cfg, dict) and s_cfg.get("enabled", False):
                            for s in s_cfg.get("symbols", []):
                                if isinstance(s, str) and s.strip():
                                    collected.add(s.strip().upper())
            except Exception as e:
                logger.warning(f"[IPC] Failed to read symbols from {cfg_path}: {e}")

    # BNBUSDT всегда необходим для расчета комиссионного дисконта
    collected.add("BNBUSDT")
    res = sorted(collected)
    logger.info(f"[IPC] Cluster discovered {len(res)} combined symbols across cluster bots.")
    return res


class CrossProcessHotPriceStream:
    """
    Единый межпроцессный стример котировок Binance.
    Реализует динамический выбор Master-процесса (Election):
    - Первый запущенный процесс захватывает блокировку streamer.lock и держит 1 WebSocket к бирже
      для ОБЪЕДИНЕННОГО списка монет всех ботов кластера.
    - Все остальные процессы подключаются как Slave к локальному сокету без сетевых коннектов к Binance.
    - При подключении Slave Master мгновенно передает ему полный снапшот текущих цен.
    - При падении Master один из Slave бесшовно перехватывает роль Master (Failover).
    """

    def __init__(
        self,
        symbols: List[str],
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        lock_name: Optional[str] = None,
        auto_cluster_symbols: bool = True,
    ):
        if auto_cluster_symbols:
            self.symbols = discover_cluster_symbols(symbols)
        else:
            self.symbols = [s.upper().strip() for s in symbols if isinstance(s, str) and s.strip()]

        self.local_symbols = set(s.upper().strip() for s in symbols if isinstance(s, str) and s.strip())
        self.host = host
        self.port = port
        self.pid = os.getpid()
        self.lock_file = IPC_DIR / (lock_name if lock_name else "streamer.lock")

        self._stop = asyncio.Event()
        self.ready = asyncio.Event()
        self.snapshot_received = asyncio.Event()
        self._is_master = False
        self._clients: Set[asyncio.StreamWriter] = set()
        self._latest_prices: Dict[str, Tuple[float, int]] = {}

    @property
    def is_master(self) -> bool:
        return self._is_master

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self._stop.set()
        self._release_lock()
        for writer in list(self._clients):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._clients.clear()

    def _try_acquire_lock(self) -> bool:
        """Попытка захватить роль Master."""
        if _is_port_in_use(self.host, self.port):
            return False

        now = time.time()
        lease_until = now + LEASE_TIMEOUT_SEC

        try:
            payload = {"pid": self.pid, "lease_until": lease_until, "port": self.port}
            self.lock_file.write_text(json.dumps(payload), encoding="utf-8")
            return True
        except Exception:
            return False

    def _renew_lease(self) -> None:
        """Продление лизинга роли Master."""
        if not self._is_master:
            return
        try:
            payload = {
                "pid": self.pid,
                "lease_until": time.time() + LEASE_TIMEOUT_SEC,
                "port": self.port,
            }
            self.lock_file.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass

    def _release_lock(self) -> None:
        """Освобождение роли Master."""
        if self._is_master and self.lock_file.exists():
            try:
                text = self.lock_file.read_text(encoding="utf-8")
                if text.strip():
                    content = json.loads(text)
                    if int(content.get("pid", 0)) == self.pid:
                        self.lock_file.unlink(missing_ok=True)
            except Exception:
                pass
            self._is_master = False

    async def _heartbeat_loop(self) -> None:
        """Фоновый цикл продления блокировки Master."""
        while not self._stop.is_set() and self._is_master:
            self._renew_lease()
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)

    async def _handle_slave_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Обработка подключившегося Slave процесса: отправка снапшота и регистрация на рассылку тиков."""
        peer = writer.get_extra_info("peername")
        self._clients.add(writer)
        logger.info(f"[IPC] New Slave connected from {peer}. Total active slaves: {len(self._clients)}")

        # 1. Мгновенно передаем Slave процессу полный снапшот цен актуальных символов кластера
        try:
            valid_prices = {
                sym: val for sym, val in self._latest_prices.items()
                if sym in self.symbols
            } if self._latest_prices else {}
            if valid_prices:
                snapshot_lines = [
                    f"{sym} {p} {ts}\n"
                    for sym, (p, ts) in valid_prices.items()
                ]
                writer.write("".join(snapshot_lines).encode("utf-8"))
            writer.write(f"SNAPSHOT_DONE 0 {int(time.time() * 1000)}\n".encode("utf-8"))
            await writer.drain()
            logger.info(f"[IPC] Sent price snapshot ({len(valid_prices)} symbols) to Slave {peer}")
        except Exception as e:
            logger.warning(f"[IPC] Failed to transmit price snapshot to slave {peer}: {e}")

        try:
            while not self._stop.is_set():
                data = await reader.readline()
                if not data:
                    break
        except Exception:
            pass
        finally:
            self._clients.discard(writer)
            logger.info(f"[IPC] Slave disconnected from {peer}. Remaining slaves: {len(self._clients)}")
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _broadcast_tick(self, tick: HotPriceTick) -> None:
        """Рассылка тика всем подключенным Slave процессам."""
        if not self._clients:
            return

        payload = f"{tick.symbol} {tick.price} {tick.event_time_ms}\n".encode("utf-8")
        dead_clients = []
        for writer in self._clients:
            try:
                writer.write(payload)
                await writer.drain()
            except Exception:
                dead_clients.append(writer)

        for dw in dead_clients:
            self._clients.discard(dw)

    async def _run_master(self, on_tick: Callable[[HotPriceTick], Awaitable[None]]) -> None:
        """Работа в роли Master: предзагрузка цен, старт TCP сервера, WebSocket к Binance."""
        server = None
        bn_stream = None
        hb_task = None

        try:
            # 1. Запуск локального TCP сервера для Slave процессов
            try:
                server = await asyncio.start_server(self._handle_slave_client, self.host, self.port)
            except OSError as e:
                logger.warning(f"[IPC] Cannot bind {self.host}:{self.port} ({e}). Yielding Master role.")
                self._release_lock()
                return

            self._is_master = True
            logger.info(f"[IPC] Process PID={self.pid} ACQUIRED MASTER role! Starting IPC TCP server on {self.host}:{self.port}...")
            hb_task = asyncio.create_task(self._heartbeat_loop())
            logger.info(f"[IPC] Master IPC TCP server listening on {self.host}:{self.port}.")

            # 2. Предзагрузка начальных котировок по всем символам кластера одним оптовым REST-запросом
            try:
                bulk_init = await BinancePublic.get_prices_bulk(self.symbols)
                now_ms = int(time.time() * 1000)
                # Очищаем кэш цен от фантомных символов прошлых сессий
                self._latest_prices = {sym: (p, now_ms) for sym, p in bulk_init.items()}
                for sym, p in bulk_init.items():
                    await on_tick(HotPriceTick(symbol=sym, price=p, event_time_ms=now_ms))
                logger.info(f"[IPC] Master preloaded initial prices for {len(bulk_init)}/{len(self.symbols)} cluster symbols.")
            except Exception as e:
                logger.warning(f"[IPC] Master initial bulk prefetch failed: {e}")

            # 3. Подключение внешнего WebSocket к бирже
            async def master_tick_wrapper(tick: HotPriceTick):
                self._latest_prices[tick.symbol] = (tick.price, tick.event_time_ms)
                await on_tick(tick)
                await self._broadcast_tick(tick)

            bn_stream = BinanceHotPriceStream(self.symbols)
            stream_task = asyncio.create_task(bn_stream.run(master_tick_wrapper))

            await bn_stream.ready.wait()
            logger.info(f"[IPC] Master PID={self.pid} Binance WebSocket connected & ready. Streaming {len(self.symbols)} symbols.")
            self.snapshot_received.set()
            self.ready.set()

            # Ждем завершения либо падения
            await self._stop.wait()

        finally:
            if self._is_master:
                logger.info(f"[IPC] Master PID={self.pid} shutting down and releasing master lock.")
            if hb_task:
                hb_task.cancel()
            if bn_stream:
                await bn_stream.aclose()
            if server:
                server.close()
                await server.wait_closed()
            self._release_lock()
            self.ready.clear()
            self.snapshot_received.clear()

    async def _run_slave(self, on_tick: Callable[[HotPriceTick], Awaitable[None]]) -> None:
        """Работа в роли Slave: читает тики и снапшот из сокета Master процесса."""
        self._is_master = False
        logger.info(f"[IPC] Process PID={self.pid} connecting as SLAVE to Master at {self.host}:{self.port} (0 Binance WS connections)...")
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.ready.set()
        logger.info(f"[IPC] Slave PID={self.pid} connected to Master successfully! Receiving shared tick stream.")
        try:
            while not self._stop.is_set():
                line = await reader.readline()
                if not line:
                    logger.warning(f"[IPC] Slave PID={self.pid} lost connection to Master (EOF). Initiating failover...")
                    break
                decoded = line.decode("utf-8").strip()
                if not decoded:
                    continue
                parts = decoded.split()
                if len(parts) >= 3:
                    sym, price_str, ts_str = parts[0], parts[1], parts[2]
                    if sym == "SNAPSHOT_DONE":
                        self.snapshot_received.set()
                        logger.info(f"[IPC] Slave PID={self.pid} received full price snapshot from Master.")
                        continue
                    try:
                        price_val = float(price_str)
                        ts_val = int(ts_str)
                    except ValueError:
                        continue
                    tick = HotPriceTick(symbol=sym, price=price_val, event_time_ms=ts_val)
                    self._latest_prices[sym] = (price_val, ts_val)
                    await on_tick(tick)
        finally:
            self.ready.clear()
            self.snapshot_received.clear()
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def run(self, on_tick: Callable[[HotPriceTick], Awaitable[None]]) -> None:
        """Главный цикл работы стримера с поддержкой Failover."""
        slave_retry_count = 0
        while not self._stop.is_set():
            try:
                if self._try_acquire_lock():
                    # Мы стали Master
                    try:
                        await self._run_master(on_tick)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        if not self._stop.is_set():
                            logger.warning(f"[IPC] Master run error: {e}")
                            await asyncio.sleep(1.0)
                else:
                    # Мы работаем как Slave
                    try:
                        await self._run_slave(on_tick)
                        slave_retry_count = 0
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        slave_retry_count += 1
                        if not _is_port_in_use(self.host, self.port):
                            self._release_lock()
                        if not self._stop.is_set() and (slave_retry_count <= 2 or slave_retry_count % 30 == 0):
                            logger.warning(f"[IPC] Slave connection failed: {e} (attempt {slave_retry_count}). Retrying in 1s...")
                        if not self._stop.is_set():
                            await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break

