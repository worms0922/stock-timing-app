"""Read-only Toss Securities adapter. No account or order endpoints are used."""
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

KST = dt.timezone(dt.timedelta(hours=9))

def now_kst():
    return dt.datetime.now(KST)

class DataError(RuntimeError):
    pass

class AuthError(DataError):
    pass

class TossConnector:
    def __init__(self):
        self.client_id = os.getenv('TOSS_CLIENT_ID', '').strip()
        self.secret = os.getenv('TOSS_CLIENT_SECRET', '').strip()
        self._token = None
        self._expiry = 0
        self._token_lock = threading.Lock()
        self._lock = threading.RLock()
        self._cache = {}
        self._next_request = 0

    @property
    def configured(self):
        return bool(self.client_id and self.secret)

    def _http(self, path, params=None, data=None, token=None):
        url = 'https://openapi.tossinvest.com' + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        headers = {'Accept': 'application/json', 'User-Agent': 'PersonalTimingApp/2.0'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not expose response bodies, credentials, or request headers.
            if exc.code == 403:
                raise DataError('토스 API 허용 IP 등록을 확인하세요 (403).') from None
            if exc.code == 401 and path != '/oauth2/token':
                raise AuthError('토스 인증 토큰을 갱신해야 합니다 (401).') from None
            if exc.code == 429:
                try:
                    wait = min(60, max(1, float(exc.headers.get('Retry-After', 2))))
                except ValueError:
                    wait = 2
                self._next_request = max(self._next_request, time.monotonic() + wait)
                raise DataError('토스 호출 한도에 도달했습니다. 잠시 후 갱신합니다 (429).') from None
            raise DataError(f'토스 API 요청 실패 ({exc.code}). 인증·서비스 상태를 확인하세요.') from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise DataError('토스 API 응답을 받지 못했습니다. 가상 데이터는 사용하지 않습니다.') from None

    def token(self):
        if not self.configured:
            raise DataError('서버에 TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET을 설정하세요.')
        with self._token_lock:
            if self._token and time.monotonic() < self._expiry:
                return self._token
            result = self._http('/oauth2/token', data={
                'grant_type': 'client_credentials', 'client_id': self.client_id,
                'client_secret': self.secret})
            self._token = result['access_token']
            self._expiry = time.monotonic() + max(1, int(result['expires_in']) - 120)
            return self._token

    def get(self, path, params=None, ttl=0):
        key = (path, tuple(sorted((params or {}).items())))
        # Serialize cache misses: single server instance, at most ~4 REST requests/s.
        with self._lock:
            old = self._cache.get(key)
            if old and time.monotonic() - old[0] < ttl:
                return old[1]
            token = self.token()
            time.sleep(max(0, self._next_request - time.monotonic()))
            self._next_request = time.monotonic() + .27
            try:
                response = self._http(path, params=params, token=token)
            except AuthError:
                self.invalidate_token()
                response = self._http(path, params=params, token=self.token())
            if 'result' not in response:
                raise DataError('토스 응답 형식이 예상과 다릅니다.')
            result = response['result']
            if ttl:
                if len(self._cache) > 1000:
                    self._cache.clear()
                self._cache[key] = (time.monotonic(), result)
            return result

    def invalidate_token(self):
        with self._token_lock:
            self._token = None
            self._expiry = 0

    def resolve_code(self, query):
        names = {'삼성전자': '005930', 'SK하이닉스': '000660', '현대차': '005380',
                 '기아': '000270', '카카오': '035720', '네이버': '035420', 'NAVER': '035420'}
        code = names.get(query.strip(), query.strip())
        if code.startswith('A'):
            code = code[1:]
        if not re.fullmatch(r'\d{6}', code):
            raise DataError('국내 종목코드 6자리를 입력하세요. 일부 주요 종목은 이름 검색을 지원합니다.')
        return code

    def candles(self, code, interval='1d', count=250, adjusted=True):
        rows = {}
        before = None
        pages = (count + 199) // 200
        for page in range(pages):
            params = {'symbol': code, 'interval': interval, 'count': 200, 'adjusted': 'true' if adjusted else 'false'}
            if before:
                params['before'] = before
            result = self.get('/api/v1/candles', params, ttl=20 if page == 0 else 3600)
            old_len = len(rows)
            for candle in result.get('candles', []):
                rows[candle['timestamp']] = candle
            next_before = result.get('nextBefore')
            if not next_before or next_before == before or len(rows) == old_len:
                break
            before = next_before
        result = []
        for row in sorted(rows.values(), key=lambda x: x['timestamp']):
            stamp = dt.datetime.fromisoformat(row['timestamp']).astimezone(KST)
            result.append({'date': stamp.date().isoformat(), 'timestamp': stamp.isoformat(),
                           'open': float(row['openPrice']), 'high': float(row['highPrice']),
                           'low': float(row['lowPrice']), 'close': float(row['closePrice']),
                           'volume': int(row['volume'])})
        if not result:
            raise DataError('조회 가능한 캔들이 없습니다.')
        return result[-count:]

    def get_chart_data(self, stock_code, timeframe='M', count=150):
        code = self.resolve_code(stock_code)
        if timeframe not in ('D', 'W', 'M'):
            raise DataError('지원하지 않는 봉 주기입니다.')
        # Aggregate weekly/monthly bars from adjusted daily bars, retaining partial current bars.
        history = min(4500, count * {'D': 1, 'W': 6, 'M': 24}[timeframe] + 30)
        candles = aggregate(self.candles(code, count=history), timeframe)[-count:]
        info = self.get('/api/v1/stocks', {'symbols': code}, ttl=3600)
        return {'code': code, 'name': info[0]['name'] if info else code,
                'timeframe': timeframe, 'candles': candles, 'source': '토스증권 API · KRX/NXT',
                'received_at': now_kst().isoformat()}

    def flow(self, code):
        return self.get(f'/api/v1/stocks/{code}/investor-trading', {'count': 20}, ttl=60).get('records', [])

    def get_top_volume_stocks(self, limit=3):
        # Fixed watch universe; do not mislabel as market-wide volume ranking.
        return [{'code': c, 'name': n} for c, n in [
            ('005930', '삼성전자'), ('000660', 'SK하이닉스'), ('005380', '현대차')]][:limit]

def aggregate(candles, timeframe):
    if timeframe == 'D':
        return candles
    groups = {}
    for candle in candles:
        date = dt.date.fromisoformat(candle['date'])
        key = date.strftime('%Y-%m') if timeframe == 'M' else date - dt.timedelta(days=date.weekday())
        if key not in groups:
            groups[key] = dict(candle)
        else:
            bar = groups[key]
            bar.update(high=max(bar['high'], candle['high']), low=min(bar['low'], candle['low']),
                       close=candle['close'], volume=bar['volume'] + candle['volume'],
                       timestamp=candle['timestamp'])
    return list(groups.values())
