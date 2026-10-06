"""One read-only WebSocket connection; subscription follows the selected symbol."""
import copy
import json
import random
import socket
import threading
import time

class LiveStream:
    def __init__(self, connector):
        self.connector = connector
        self.lock = threading.RLock()
        self.code = None
        self.connected = False
        self.trade = None
        self.book = None
        self.error = '실시간 종목을 선택하세요.'
        self.thread = None
        self.revision = 0

    def select(self, code):
        with self.lock:
            if code != self.code:
                self.code = code
                self.trade = self.book = None
                self.connected = False
                self.error = '구독 준비 중'
                self.revision += 1
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, daemon=True)
                self.thread.start()

    def snapshot(self, code):
        with self.lock:
            if code != self.code:
                return None, None, False, '종목 변경 중'
            return copy.deepcopy(self.trade), copy.deepcopy(self.book), self.connected, self.error

    def handle(self, frame):
        with self.lock:
            if frame.get('type') == 'subscriptions':
                expected = {f'trade:kr:{self.code}', f'orderbook:kr:{self.code}'}
                self.connected = expected.issubset(set(frame.get('subscribed', [])))
                self.error = '' if self.connected else '체결·호가 구독 거부 또는 대기'
            elif frame.get('type') == 'error':
                self.connected = False
                self.error = '실시간 서비스 오류 · 재연결 중'
                raise RuntimeError('stream error')
            elif frame.get('type') == 'message':
                if frame.get('topic') == f'trade:kr:{self.code}':
                    self.trade = frame['data']
                elif frame.get('topic') == f'orderbook:kr:{self.code}':
                    self.book = frame['data']

    def run(self):
        delay = 1
        while True:
            ws = None
            try:
                from websockets.sync.client import connect
                token = self.connector.token()
                ws = connect('wss://openapi-ws.tossinvest.com/ws/v1',
                             additional_headers={'Authorization': 'Bearer ' + token},
                             open_timeout=12, ping_interval=None, max_queue=64)
                sent_revision = -1
                last_ping = time.monotonic()
                last_received = time.monotonic()
                while True:
                    with self.lock:
                        revision, code = self.revision, self.code
                    if revision != sent_revision:
                        # REST initial snapshot; next pushed updates supersede it.
                        book = self.connector.get('/api/v1/orderbook', {'symbol': code}, ttl=2)
                        trades = self.connector.get('/api/v1/trades', {'symbol': code, 'count': 1}, ttl=2)
                        with self.lock:
                            if self.code == code:
                                self.book = book
                                self.trade = trades[0] if trades else None
                        ws.send(json.dumps([{'type': 'trade:kr', 'codes': [code]},
                                            {'type': 'orderbook:kr', 'codes': [code]}]))
                        sent_revision = revision
                    if time.monotonic() - last_ping >= 60:
                        ws.send('PING')
                        last_ping = time.monotonic()
                    try:
                        frame = json.loads(ws.recv(timeout=1))
                    except TimeoutError:
                        if time.monotonic() - last_received > 130:
                            raise RuntimeError('heartbeat timeout')
                        continue
                    last_received = time.monotonic()
                    self.handle(frame)
                    delay = 1
            except ImportError:
                with self.lock:
                    self.connected = False
                    self.error = '서버에 requirements.txt의 websockets 설치가 필요합니다.'
                return
            except Exception as exc:
                if getattr(getattr(exc, 'response', None), 'status_code', None) == 401:
                    self.connector.invalidate_token()
                with self.lock:
                    self.connected = False
                    self.trade = self.book = None
                    self.error = ('토스 API 키 설정이 필요합니다.' if not self.connector.configured
                                  else '실시간 연결 대기 · 허용 IP/토큰/서비스 상태 확인')
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            time.sleep(delay + random.uniform(0, .5))
            delay = min(delay * 2, 30)
