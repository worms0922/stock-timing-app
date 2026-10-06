"""Private, read-only market analysis server. Configure secrets in server environment."""
import base64
import hmac
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from algorithm import analyze_signals
from toss_connector import TossConnector, DataError, now_kst
from flow_analysis import flow_score, scalp_analysis
from live_stream import LiveStream

BASE_DIR = Path(__file__).resolve().parent
connector = TossConnector()
live = LiveStream(connector)
WATCH = [('005930','삼성전자'),('000660','SK하이닉스'),('005380','현대차'),
         ('000270','기아'),('035420','NAVER'),('035720','카카오'),('105560','KB금융'),
         ('055550','신한지주'),('068270','셀트리온'),('005490','POSCO홀딩스'),
         ('012330','현대모비스'),('373220','LG에너지솔루션'),('207940','삼성바이오로직스'),
         ('066570','LG전자'),('015760','한국전력'),('033780','KT&G'),
         ('003670','포스코퓨처엠'),('247540','에코프로비엠'),('086520','에코프로'),('069500','KODEX 200')]
screen_lock = threading.Lock()
screen_states = {}

def chart(code, tf):
    raw = connector.get_chart_data(code, tf, count=250 if tf == 'D' else 150)
    analysis = analyze_signals(raw['candles'], tf)
    if not analysis:
        raise DataError('분석에 필요한 25개 이상의 봉이 없습니다.')
    latest = analysis[-1]
    try:
        # Use unadjusted daily volume: investor net quantities are actual shares.
        daily = connector.candles(raw['code'], count=30, adjusted=False)
        flow = flow_score(connector.flow(raw['code']), daily, latest['score'],
                          as_of=raw['candles'][-1]['timestamp'][:10])
        if flow.get('date') and flow['date'] != raw['candles'][-1]['timestamp'][:10]:
            flow['combined'] = None
            flow['reason'] = '최신 캔들과 수급 기준일이 다름 · 종합판정 보류'
    except DataError as exc:
        flow = {'available': False, 'combined': None, 'reason': str(exc)}
    missing = [p for p in (60,120,200) if len(analysis) < p]
    return {'status': 'success', 'meta': {k: raw[k] for k in ('code','name','timeframe','source')},
            'analysis': analysis, 'flow': flow, 'received_at': raw['received_at'],
            'history_note': ('시세 이력 부족: ' + ', '.join(str(p)+'봉 이평선' for p in missing)) if missing else '',
            'bar_note': '최신봉은 진행 중일 수 있습니다. 과거 차트 화살표는 기존 기술적 지표만 사용합니다.'}

def scan_worker(tf):
    items, failed = [], 0
    for code, _ in WATCH:
        try:
            data = chart(code, tf)
            last = data['analysis'][-1]
            flow = data['flow']
            items.append({'code': code, 'name': data['meta']['name'], 'price': last['close'],
                          'volume': last['volume'], 'score': last['score'], 'signal': last['signal'],
                          'reasons': last['reasons_buy'], 'eval_date': last['date'],
                          'combined': flow.get('combined'), 'flow_score': flow.get('score'),
                          'flow_date': flow.get('date'), 'provisional': flow.get('provisional')})
        except Exception:
            failed += 1
    # Missing flow never gets promoted above a complete composite score.
    items.sort(key=lambda x: (x['combined'] is not None, x['combined'] or 0, x['score'], x['volume']), reverse=True)
    for n, item in enumerate(items, 1):
        item['rank'] = n
    with screen_lock:
        screen_states[tf] = {'status': 'success' if items else 'error',
                            'message': '시세·수급 조회 실패. 키와 허용 IP를 확인하세요.' if not items else '',
                            'items': items, 'eval_mode': '최신봉 + 최신 수급 · 당일 잠정치 포함',
                            'updated_at': now_kst().isoformat(), 'total_screened': len(WATCH),
                            'failed_count': failed, 'ts': time.monotonic()}

def screen(tf, refresh=False):
    with screen_lock:
        current = screen_states.get(tf)
        if current and current['status'] == 'loading':
            return {'status': 'loading'}
        if current and not refresh and time.monotonic() - current['ts'] < 60:
            return {k:v for k,v in current.items() if k != 'ts'}
        screen_states[tf] = {'status': 'loading'}
        threading.Thread(target=scan_worker, args=(tf,), daemon=True).start()
        return {'status': 'loading'}

class Handler(BaseHTTPRequestHandler):
    def headers_common(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')

    def json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.headers_common()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        user = os.getenv('APP_USERNAME', '')
        password = os.getenv('APP_PASSWORD', '')
        if not user or len(password) < 12:
            self.json({'status':'error', 'message':'서버에 개인용 로그인 APP_USERNAME과 12자 이상 APP_PASSWORD를 설정하세요.'}, 503)
            return False
        try:
            kind, encoded = self.headers.get('Authorization', '').split(' ', 1)
            if kind.lower() != 'basic':
                raise ValueError()
            provided = base64.b64decode(encoded, validate=True)
            expected = (user + ':' + password).encode('utf-8')
            valid = hmac.compare_digest(provided, expected)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            self.send_response(401)
            self.headers_common()
            self.send_header('WWW-Authenticate', 'Basic realm="Private market analysis", charset="UTF-8"')
            self.send_header('Content-Length', '0')
            self.end_headers()
        return valid

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/health':
            self.json({'status':'ok'})
            return
        if not self.authenticated():
            return
        try:
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get('code', ['005930'])[0]
            tf = params.get('tf', ['M'])[0]
            if tf not in ('D','W','M'):
                raise DataError('지원하지 않는 봉 주기입니다.')
            if parsed.path in ('/','/index.html','/chart.html'):
                body = (BASE_DIR/'chart.html').read_bytes()
                self.send_response(200)
                self.headers_common()
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == '/api/chart':
                self.json(chart(code,tf))
            elif parsed.path == '/api/screener':
                self.json(screen(tf, params.get('refresh',['false'])[0] == 'true'))
            elif parsed.path == '/api/top_volume':
                self.json({'status':'success','items':connector.get_top_volume_stocks(3)})
            elif parsed.path == '/api/live':
                code = connector.resolve_code(code)
                if not connector.configured:
                    raise DataError('서버에 토스 API 키를 설정하세요.')
                live.select(code)
                candles = connector.candles(code,'1m',200, adjusted=False)
                trade, book, connected, error = live.snapshot(code)
                result = scalp_analysis(candles,book,trade,connected)
                result.update(stream_connected=connected, stream_message=error, code=code)
                self.json({'status':'success','live':result})
            else:
                self.json({'status':'error','message':'찾을 수 없는 경로'},404)
        except DataError as exc:
            self.json({'status':'error','message':str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.json({'status':'error','message':'데이터 처리 오류. 데이터 형식과 서버 상태를 확인하세요.'},500)

    def log_message(self, *args):
        pass

if __name__ == '__main__':
    port = int(os.getenv('PORT','8080'))
    print(f'Private timing app listening on port {port}; secrets are environment-only.')
    ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
