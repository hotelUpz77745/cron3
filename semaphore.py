# ============================================================
# File: semaphore.py
# Role: Клиентский модуль (Семафор) для торговых нод
# ============================================================
"""
Легковесный клиент, который встраивается в торговых ботов.
Отправляет heartbeats (пульс) на Арбитр и слушает широковещательные
сообщения для понимания своего статуса (Активен/Спит).
"""
import time
import zmq
import threading

class NodeSemaphore:
    def __init__(self, bot_name: str, server_name: str, arbiter_ip: str, pull_port: int = 5555, pub_port: int = 5556):
        """
        Инициализация клиента семафора для связи с Арбитром.

        :param bot_name: Имя бота (например, "hron"). Должно совпадать с ключом в config.json Арбитра.
        :param server_name: Маркер текущего сервера (например, "A" или "B").
        :param arbiter_ip: IP-адрес или доменное имя Арбитра. 
        
            ДЛЯ ПРОДАКШЕНА (ДИНАМИЧЕСКИЙ IP АРБИТРА):
            Если Арбитр запускается на сервере с динамическим IP, который периодически меняется, 
            обычный IP-адрес указывать нельзя, иначе связь разорвется.
            
            Гайд по настройке:
            1. Зарегистрируйте бесплатный DDNS-домен (например, DuckDNS, No-IP: "my-arbiter.duckdns.org").
            2. Настройте скрипт или роутер на стороне Арбитра для авто-обновления IP этого домена.
            3. При инициализации нод передавайте именно домен: `arbiter_ip="my-arbiter.duckdns.org"`.
            
            ZeroMQ нативно поддерживает доменные имена. Если IP Арбитра сменится, DDNS обновит DNS-запись, 
            и ваши ноды автоматически, без перезапуска, переподключатся по новому адресу.
            
        :param pull_port: Порт, на который Арбитр принимает пульс (heartbeats). По умолчанию 5555.
        :param pub_port: Порт, с которого Арбитр вещает статусы (pub/sub). По умолчанию 5556.
        """
        self.bot_name = bot_name
        self.server_name = server_name
        self.ctx = zmq.Context()
        
        self.push_sock = self.ctx.socket(zmq.PUSH)
        self.push_sock.connect(f"tcp://{arbiter_ip}:{pull_port}")
        
        self.sub_sock = self.ctx.socket(zmq.SUB)
        self.sub_sock.connect(f"tcp://{arbiter_ip}:{pub_port}")
        self.sub_sock.setsockopt_string(zmq.SUBSCRIBE, "")
        self.sub_sock.setsockopt(zmq.CONFLATE, 1) # Только самое свежее сообщение
        
        self._active_server_for_me = None
        self._start_listener()

    def _start_listener(self):
        def listen():
            while True:
                try:
                    msg = self.sub_sock.recv_json()
                    active_nodes = msg.get("active_nodes", {})
                    self._active_server_for_me = active_nodes.get(self.bot_name)
                except KeyboardInterrupt:
                    break
                except Exception:
                    time.sleep(0.1)
        threading.Thread(target=listen, daemon=True, name="ZmqSubscriber").start()

    @property
    def is_active(self) -> bool:
        return self._active_server_for_me == self.server_name

    def tick(self, status: str):
        """status: 'start' | 'end'"""
        try:
            self.push_sock.send_json({
                "bot_name": self.bot_name,
                "server_name": self.server_name,
                "status": status,
                "timestamp": time.time()
            })
        except Exception:
            pass