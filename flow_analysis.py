"""Transparent heuristic scores. Never treat missing flow as zero or a buy signal."""
import datetime as dt
from toss_connector import now_kst, KST

def age_seconds(timestamp, now=None):
    if not timestamp:
        return None
    try:
        stamp = dt.datetime.fromisoformat(timestamp)
        if stamp.tzinfo is None:
            return None
        elapsed = ((now or now_kst()) - stamp).total_seconds()
        return max(0, elapsed) if elapsed >= -5 else None
    except (ValueError, TypeError):
        return None

def flow_score(records, candles, technical_score, as_of=None):
    rows = sorted([r for r in records if not as_of or r['date'] <= as_of],
                  key=lambda r: r['date'], reverse=True)
    if not rows:
        return {'available': False, 'score': None, 'combined': None, 'reason': '수급 데이터 없음'}
    latest = rows[0]
    def net(row, actor):
        value = row.get(actor)
        return int(value['netBuyVolume']) if value and value.get('netBuyVolume') is not None else None
    f, i = net(latest, 'foreigner'), net(latest, 'institution')
    if f is None or i is None:
        return {'available': False, 'score': None, 'combined': None,
                'date': latest['date'], 'reason': '외국인 또는 기관 수급 미제공'}
    sample = rows[:5]
    complete = len(sample) == 5 and all(net(r, a) is not None for r in sample for a in ('foreigner','institution'))
    net5 = sum(net(r, 'foreigner') + net(r, 'institution') for r in sample) if complete else None
    bars = {c['date']: c for c in candles}
    volume = bars.get(latest['date'], {}).get('volume')
    ratio = (f + i) / volume * 100 if volume and volume > 0 else None
    # Flow 60%, technical setup 40%. Components add up to 100.
    score = (25 if f > 0 else 0) + (25 if i > 0 else 0)
    score += 25 if net5 is not None and net5 > 0 else 0
    score += 25 if ratio is not None and ratio >= 1 else 0
    enough = complete and ratio is not None
    combined = round(.6 * score + .4 * technical_score / 3 * 100) if enough else None
    return {'available': True, 'score': score, 'combined': combined, 'date': latest['date'],
            'updated_at': latest.get('updatedAt'), 'foreigner': f, 'institution': i,
            'net5': net5, 'net_volume_pct': round(ratio, 2) if ratio is not None else None,
            'provisional': latest.get('individual') is None,
            'reason': '수급 60% + 기술적 매수조건 40% (검증 전 휴리스틱)' if enough else '이력/거래량 부족 · 종합점수 보류',
            'warning': '순매수는 주식 수 기준입니다. 종합점수는 수익 확률이 아닙니다.'}

def scalp_analysis(candles, book, trade, connected, now=None):
    now = now or now_kst()
    today = now.date().isoformat()
    bars = [c for c in candles if c['date'] == today]
    # A forming minute bar may carry a future END timestamp. Use only finished bars for freshness/score.
    usable = [b for b in bars if dt.datetime.fromisoformat(b['timestamp']) <= now]
    trade_age = age_seconds(trade.get('timestamp') if trade else None, now)
    book_age = age_seconds(book.get('timestamp') if book else None, now)
    candles_age = age_seconds(usable[-1]['timestamp'] if usable else None, now)
    fresh = (connected and trade_age is not None and trade_age <= 30
             and book_age is not None and book_age <= 30
             and candles_age is not None and candles_age <= 150)
    recent = usable[-20:]
    vol = sum(b['volume'] for b in recent)
    vwap = sum((b['high'] + b['low'] + b['close']) / 3 * b['volume'] for b in recent) / vol if vol else None
    last_price = float(trade['price']) if trade else (bars[-1]['close'] if bars else None)
    bids = sorted((book or {}).get('bids', []), key=lambda x: float(x['price']), reverse=True)[:3]
    asks = sorted((book or {}).get('asks', []), key=lambda x: float(x['price']))[:3]
    bid_vol = sum(float(x['volume']) for x in bids)
    ask_vol = sum(float(x['volume']) for x in asks)
    imbalance = bid_vol / (bid_vol + ask_vol) if bid_vol + ask_vol else None
    spread = (float(asks[0]['price']) - float(bids[0]['price'])) / last_price * 100 if asks and bids and last_price else None
    previous = usable[-11:-1]
    baseline = sum(b['volume'] for b in previous) / len(previous) if previous else 0
    volume_ratio = usable[-1]['volume'] / baseline if usable and baseline else None
    enough = len(usable) >= 20 and vwap is not None and imbalance is not None and spread is not None
    breakout = enough and last_price > max(b['high'] for b in usable[-6:-1])
    checks = [
        ('최근 20개 완료 분봉의 근사 VWAP 상회', bool(vwap and last_price and last_price > vwap)),
        ('최근 완료 분봉 거래량이 직전 10봉 평균의 1.5배 이상', bool(volume_ratio and volume_ratio >= 1.5)),
        ('직전 5개 완료 분봉 고점 돌파', bool(breakout)),
        ('상위 3호가 매수잔량 비중 55% 이상', bool(imbalance is not None and imbalance >= .55))]
    raw_score = sum(passed for _, passed in checks) * 25
    eligible = fresh and enough and 0 <= spread <= .2
    if not fresh:
        signal = '실시간 데이터 대기 / 지연 · 판단 보류'
    elif not enough:
        signal = '당일 완료 분봉 부족 · 판단 보류'
    elif spread < 0 or spread > .2:
        signal = '호가 스프레드 확대 · 진입 보류'
    elif raw_score >= 75:
        signal = '상승 타점 후보'
    elif vwap and last_price < vwap and imbalance < .45:
        signal = '가격·호가 약세 관찰'
    else:
        signal = '관망'
    return {'signal': signal, 'score': raw_score if eligible else None,
            'price': last_price, 'vwap20': round(vwap, 2) if vwap else None,
            'volume_ratio': round(volume_ratio, 2) if volume_ratio is not None else None,
            'bid_share': round(imbalance * 100, 1) if imbalance is not None else None,
            'spread_pct': round(spread, 3) if spread is not None else None,
            'trade_at': trade.get('timestamp') if trade else None,
            'book_at': book.get('timestamp') if book else None,
            'candle_at': usable[-1]['timestamp'] if usable else None,
            'trade_age': trade_age, 'book_age': book_age, 'fresh': fresh,
            'checks': [{'label': label, 'passed': passed if fresh and enough else None} for label, passed in checks],
            'bars': bars[-60:], 'received_at': now.isoformat(),
            'note': '체결 API에는 매수/매도 구분이 없어 체결강도를 계산하지 않습니다. 호가잔량은 실제 체결 수급과 다릅니다.'}
