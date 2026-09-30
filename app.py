#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
네이버 금융 실시간 시세 연동 및 실시간 자동분석(스크리너) 서버
- 멀티스레드 비동기 소켓 처리
- 정규장 시간(09:00~15:30) 전일 완성봉 / 장마감 후 금일 완성봉 자동 판별
- 일봉/주봉/월봉 타점 조건 부합 종목 거래량 순 TOP 20 실시간 스캔
"""

import os
import sys
import json
import socket
import datetime
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor
import webbrowser
import threading

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from naver_connector import NaverFinanceConnector
from algorithm import analyze_signals

PORT = 8080
naver_conn = NaverFinanceConnector()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class StockScreener:
    def __init__(self, connector, analyze_func):
        self.connector = connector
        self.analyze_signals = analyze_func
        self.cache = {}
        self.cache_time = {}
        self.cache_ttl = 300 # 5분 캐시

    def is_market_hours(self):
        """한국 정규장 시간(09:00 ~ 15:30, 월~금) 여부 판별"""
        now = datetime.datetime.now()
        if now.weekday() >= 5: # 토요일(5), 일요일(6)
            return False
        current_minutes = now.hour * 60 + now.minute
        return 9 * 60 <= current_minutes < 15 * 60 + 30

    def get_candidate_universe(self):
        """
        코스피/코스닥 대표 우량주, 고유동성 ETF 및 거래량 상위 종목 풀
        (풍부한 거래량 기반으로 작전세력 리스크 최소화)
        """
        core_list = [
            ('005930', '삼성전자'), ('000660', 'SK하이닉스'), ('005380', '현대차'),
            ('000270', '기아'), ('035720', '카카오'), ('035420', 'NAVER'),
            ('105560', 'KB금융'), ('055550', '신한지주'), ('316140', '우리금융지주'),
            ('086790', '하나금융지주'), ('122630', 'KODEX 레버리지'), ('252670', 'KODEX 200선물인버스2X'),
            ('102110', 'TIGER 200'), ('229200', 'KODEX 코스닥150'), ('233740', 'KODEX 코스닥150레버리지'),
            ('068270', '셀트리온'), ('005490', 'POSCO홀딩스'), ('028260', '삼성물산'),
            ('012330', '현대모비스'), ('441640', '미국배당다우존스'), ('373220', 'LG에너지솔루션'),
            ('207940', '삼성바이오로직스'), ('066570', 'LG전자'), ('096770', 'SK이노베이션'),
            ('015760', '한국전력'), ('033780', 'KT&G'), ('003670', '포스코퓨처엠'),
            ('247540', '에코프로비엠'), ('086520', '에코프로'), ('196170', '알테오젠'),
            ('069500', 'KODEX 200'), ('360750', 'TIGER 미국S&P500'), ('133690', 'TIGER 미국나스닥100'),
            ('009150', '삼성전기'), ('018260', '삼성에스디에스'), ('003550', 'LG'),
            ('032830', '삼성생명'), ('138040', '메리츠금융지주'), ('010130', '고려아연')
        ]
        
        try:
            vol_stocks = self.connector.get_top_volume_stocks(limit=10)
        except Exception:
            vol_stocks = []
            
        seen = set()
        universe = []
        for item in vol_stocks:
            c, n = item['code'], item['name']
            if c not in seen:
                seen.add(c)
                universe.append((c, n))
        for c, n in core_list:
            if c not in seen:
                seen.add(c)
                universe.append((c, n))
        return universe

    def screen(self, timeframe='D', limit=20, force_refresh=False):
        now_ts = time.time()
        if not force_refresh and timeframe in self.cache:
            if now_ts - self.cache_time.get(timeframe, 0) < self.cache_ttl:
                return self.cache[timeframe]

        universe = self.get_candidate_universe()
        market_hours = self.is_market_hours()
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        
        def evaluate_one(item):
            code, name = item
            try:
                raw = self.connector.get_chart_data(code, timeframe=timeframe, count=150)
                candles = raw['candles']
                if not candles or len(candles) < 25:
                    return None
                    
                # 핵심 요구사항: 장중에는 전일까지 캔들바로, 장마감 후에는 금일기준 캔들바로 평가
                if market_hours and candles[-1]['date'] == today_str and len(candles) > 30:
                    eval_candles = candles[:-1] # 전일 완성봉 기준
                    eval_mode = "장중 (전일 종가 완성봉 기준)"
                else:
                    eval_candles = candles # 금일 완성봉 기준
                    eval_mode = "장마감 (금일 종가 완성봉 기준)" if not market_hours else "장중 (완성봉 기준)"
                    
                analysis = self.analyze_signals(eval_candles, timeframe=timeframe)
                if not analysis:
                    return None
                    
                target = analysis[-1]
                return {
                    'code': code,
                    'name': raw['name'],
                    'price': target['close'],
                    'volume': target['volume'],
                    'score': target['score'],
                    'signal': target['signal'],
                    'reasons': target['reasons_buy'],
                    'eval_date': target['date'],
                    'eval_mode': eval_mode,
                    'timeframe': timeframe
                }
            except Exception:
                return None
                
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = [r for r in ex.map(evaluate_one, universe) if r is not None]
            
        # 순위 정렬: 타점 조건 만족 종목(score >= 2 우선, 그 다음 score == 1)을 거래량(volume) 순으로 정렬
        tier1 = [r for r in results if r['score'] >= 2]
        tier2 = [r for r in results if r['score'] == 1]
        tier3 = [r for r in results if r['score'] == 0]
        
        tier1.sort(key=lambda x: x['volume'], reverse=True)
        tier2.sort(key=lambda x: x['volume'], reverse=True)
        tier3.sort(key=lambda x: x['volume'], reverse=True)
        
        final_list = tier1 + tier2
        if len(final_list) < limit:
            final_list += tier3
            
        final_list = final_list[:limit]
        for i, item in enumerate(final_list):
            item['rank'] = i + 1
            
        eval_mode_desc = "장중 분석 모드 (전일 종가 완성봉 기준 / 09:00~15:30)" if market_hours else "장마감 확정 모드 (금일 종가 완성봉 기준)"
        
        output = {
            'status': 'success',
            'timeframe': timeframe,
            'market_hours': market_hours,
            'eval_mode': eval_mode_desc,
            'total_screened': len(universe),
            'updated_at': datetime.datetime.now().strftime('%H:%M:%S'),
            'items': final_list
        }
        
        self.cache[timeframe] = output
        self.cache_time[timeframe] = now_ts
        return output

screener_agent = StockScreener(naver_conn, analyze_signals)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class NaverAppHandler(BaseHTTPRequestHandler):
    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            
            # 1. 헬스체크
            if parsed.path == '/api/health':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{"status":"ok","connected":true}')
                return
                
            # 2. UI 서빙
            if parsed.path in ('/', '/index.html', '/chart.html'):
                html_file = os.path.join(BASE_DIR, 'chart.html')
                with open(html_file, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(content)
                return
                
            # 3. 당일 최고 거래량 3개 종목
            elif parsed.path == '/api/top_volume':
                top_stocks = naver_conn.get_top_volume_stocks(limit=3)
                payload = {'status': 'success', 'items': top_stocks}
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(body)
                return

            # 4. 실시간 자동분석 (타점 포착 TOP 20 스크리너 API)
            elif parsed.path == '/api/screener':
                params = urllib.parse.parse_qs(parsed.query)
                tf = params.get('tf', ['D'])[0]
                refresh = params.get('refresh', ['false'])[0].lower() == 'true'
                limit = int(params.get('limit', ['20'])[0])
                
                screen_res = screener_agent.screen(timeframe=tf, limit=limit, force_refresh=refresh)
                body = json.dumps(screen_res, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(body)
                return

            # 5. 차트 시세 및 정밀 알고리즘 분석 API
            elif parsed.path == '/api/chart':
                params = urllib.parse.parse_qs(parsed.query)
                code_query = params.get('code', ['005930'])[0]
                tf = params.get('tf', ['M'])[0]
                
                try:
                    raw = naver_conn.get_chart_data(code_query, timeframe=tf, count=150)
                    analysis = analyze_signals(raw['candles'], timeframe=tf)
                    payload = {
                        'status': 'success',
                        'meta': {
                            'code': raw['code'],
                            'name': raw['name'],
                            'timeframe': raw['timeframe'],
                            'source': raw['source'],
                        },
                        'analysis': analysis
                    }
                except Exception as e:
                    payload = {'status': 'error', 'message': str(e)}
                    
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(body)
                return
                
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as err:
            try:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(err)}).encode('utf-8'))
            except Exception:
                pass

    def log_message(self, format, *args):
        return

def get_free_port(start_port=8080):
    for p in [start_port, 8081, 8082, 8888, 5000]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('', p))
            s.close()
            return p
        except OSError:
            continue
    return start_port

def main():
    env_port = os.environ.get('PORT')
    if env_port:
        port = int(env_port)
    else:
        port = get_free_port()
    server = ThreadedHTTPServer(('', port), NaverAppHandler)
    url = f"http://localhost:{port}"
    print("==================================================================")
    print(f"  [네이버 금융 실시간 연동] 자동분석 & 트리플 타점 시스템 실행 완료")
    print(f"  접속 주소: {url}")
    print("  * 장중(09:00~15:30) 전일 완성봉 / 장마감 후 금일 완성봉 자동 분석")
    print("  * 일봉/주봉/월봉 타점 부합 종목 거래량 순 TOP 20 스캔 지원")
    print("==================================================================")
    
    try:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
        
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("서버를 종료합니다.")
        server.server_close()

if __name__ == '__main__':
    main()
