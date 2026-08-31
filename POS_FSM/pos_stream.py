# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\POS_FSM\pos_stream.py
# Role: pos_stream.py module

# ==============================================================================
# Path: FSM/pos_stream.py
# Role: Подключение к User Data Stream и менеджер Listen Key
# ==============================================================================

import asyncio
import aiohttp
import json
from typing import Optional, Callable, Set
from POS_FSM.pos_stream_monitor import PositionMonitor
from c_utils import Utils
from c_log import UnifiedLogger

logger = UnifiedLogger("FSM_Stream")
IS_SHOW_SIGNAL = False

class BinanceListenKeyManager:
    """
    Менеджер User Data Stream Listen Key.
    Единственное место, где используется API KEY.
    WS сюда не лезет.
    """
    KEEPALIVE_INTERVAL = 25 * 60  # Binance рекомендует < 30 мин

    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.session = session
        self.listen_key: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    async def create(self) -> str:
        async with self.session.post(
            "https://fapi.binance.com/fapi/v1/listenKey",
            headers={"X-MBX-APIKEY": self.api_key},
        ) as r:
            data = await r.json()
            if "listenKey" not in data:
                error_msg = data.get('msg', str(data))
                logger.error(f"[BINANCE] API-Key rejected: {error_msg}")
                raise ValueError(f"Binance API Error: {error_msg}")
            self.listen_key = data["listenKey"]

        await self.session.put(
            "https://fapi.binance.com/fapi/v1/listenKey",
            headers={"X-MBX-APIKEY": self.api_key},
        )

        logger.info(f"[BINANCE] listenKey created & activated: {self.listen_key[:5]}... (length: {len(self.listen_key)})")
        return self.listen_key

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(self.KEEPALIVE_INTERVAL)
            try:
                await self.session.put(
                    "https://fapi.binance.com/fapi/v1/listenKey",
                    headers={"X-MBX-APIKEY": self.api_key},
                )
                # logger.debug("[BINANCE] listenKey keepalive")
            except Exception as e:
                logger.warning(f"[BINANCE] listenKey keepalive failed: {e}")

    def start_keepalive(self):
        if not self._task:
            self._task = asyncio.create_task(self._keepalive_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

class PositionStream:
    """
    BINANCE FUTURES USER DATA STREAM
    
    Стримом парсим только ендпоинт открытых позиций (ACCOUNT_UPDATE).
    """
    def __init__(
        self,
        *,
        api_key: str,
        stop_flag: Callable[[], bool],
        monitor,
        target_symbols: Optional[Set[str]] = None,
        client = None,
    ):
        self.api_key = api_key
        self.stop_flag = stop_flag
        self.monitor = monitor
        self.target_symbols = {s.upper() for s in target_symbols} if target_symbols else set()
        self.client = client

        self.session: Optional[aiohttp.ClientSession] = None
        self.websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self.listen_mgr: Optional[BinanceListenKeyManager] = None

        self.ws_url: Optional[str] = None
        self.ready = False
        self.is_connected = False
        self._external_stop = False
        
        self.callbacks = []

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def stop(self):
        self._external_stop = True
        self.ready = False
        logger.info("PositionStream: stop requested")

    async def _create_session(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=None)
        # logger.info("[MASTER WS] direct connection - creating session with specific headers")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive"
        }
        return aiohttp.ClientSession(timeout=timeout, trust_env=False, headers=headers)

    async def _connect(self) -> bool:
        try:
            # logger.info(f"[MASTER WS] connecting to {self.ws_url}...")
            self.websocket = await self.session.ws_connect(
                self.ws_url,
                autoping=True,
                max_msg_size=0,
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Origin": "https://fapi.binance.com",
                    "Host": "fstream.binance.com",
                    "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits"
                }
            )
            self.is_connected = True
            # logger.info(f"[MASTER WS] connected successfully. Status: {self.websocket.closed}")
            return True
        except aiohttp.WSServerHandshakeError as e:
            logger.error(f"[MASTER WS] handshake failed: status={e.status}, message={e.message}, headers={e.headers}")
            return False
        except Exception as e:
            logger.error(f"[MASTER WS] connect failed: {type(e).__name__} - {e}", exc_info=True)
            return False

    async def _disconnect(self):
        self.is_connected = False
        self.ready = False

        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
            self.websocket = None

        if self.listen_mgr:
            await self.listen_mgr.stop()
            self.listen_mgr = None

        logger.info("PositionStream: WS disconnected")



    async def _handle_messages(self):
        while not self._external_stop and not self.stop_flag():
            try:
                msg = await asyncio.wait_for(
                    self.websocket.receive(),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                # logger.debug("[MASTER WS] 5s timeout - waiting for messages...", throttle_sec=60)
                continue

            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                logger.warning(f"[MASTER WS] socket closed: {msg.type}")
                raise RuntimeError("ws_closed")

            if msg.type != aiohttp.WSMsgType.TEXT:
                logger.info(f"[MASTER WS] non-text msg type: {msg.type} data: {msg.data}")
                continue

            # logger.info(f"[MASTER WS RAW] {msg.data}")

            try:
                data = json.loads(msg.data)
                # print(f"RAW WS MSG: {data}")
            except Exception as e:
                logger.warning(f"[MASTER WS] invalid JSON: {e}")
                continue

            etype = data.get("e")
            # logger.debug(f"[MASTER WS] RAW EVENT RECEIVED: {etype}")
            if not etype:
                continue

            if IS_SHOW_SIGNAL:
                logger.debug(f"[MASTER WS] EVENT {etype}", throttle_sec=60, throttle_key=f"ws_event_{etype}")

            for cb in self.callbacks:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(data))
                else:
                    cb(data)

    async def start(self):
        self._external_stop = False
        try:
            while not self._external_stop and not self.stop_flag():
                self.session = await self._create_session()
                try:
                    self.listen_mgr = BinanceListenKeyManager(
                        api_key=self.api_key,
                        session=self.session,
                    )
                    listen_key = await self.listen_mgr.create()
                    self.listen_mgr.start_keepalive()

                    self.ws_url = f"wss://fstream.binance.com/private/ws/{listen_key}"

                    if not await self._connect():
                        raise RuntimeError("ws_connect_failed")

                    # Обязательная синхронизация по REST при любом подключении/переподключении стрима
                    if self.client and self.target_symbols:
                        try:
                            # logger.info("[MASTER WS] Connected. Running REST sync to catch up on missed updates...")
                            await self.monitor.sync_from_rest(self.client, list(self.target_symbols))
                        except Exception as e:
                            logger.error(f"[MASTER WS] REST sync failed on connect: {e}")

                    self.ready = True
                    await self._handle_messages()

                except asyncio.CancelledError:
                    raise
                except RuntimeError as e:
                    if str(e) == "ws_closed":
                        logger.info(f"[MASTER WS] Stream disconnected, initiating automatic reconnect...")
                    else:
                        logger.error(f"[MASTER WS] cycle failed: {type(e).__name__} - {e}")
                except ValueError as e:
                    logger.error(f"[MASTER WS] {e}")
                except Exception as e:
                    logger.error(f"[MASTER WS] cycle failed: {type(e).__name__} - {e}", exc_info=True)

                finally:
                    self.ready = False
                    await self._disconnect()

                    if self.session:
                        try:
                            await self.session.close()
                        except Exception:
                            pass
                        self.session = None

                    if not self._external_stop:
                        await asyncio.sleep(1.0)
        finally:
            self.ready = False
