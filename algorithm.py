import math

def calculate_sma(prices, period):
    smas = [None] * len(prices)
    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1 : i + 1]
        smas[i] = round(sum(window) / period, 1)
    return smas

def calculate_bollinger_bands(prices, period=20, num_std=2):
    upper = [None] * len(prices)
    lower = [None] * len(prices)
    mid = calculate_sma(prices, period)
    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1 : i + 1]
        m = mid[i]
        variance = sum((p - m) ** 2 for p in window) / period
        std = math.sqrt(variance)
        upper[i] = round(m + num_std * std, 1)
        lower[i] = round(m - num_std * std, 1)
    return upper, mid, lower

def calculate_rsi(prices, period=14):
    rsi = [None] * len(prices)
    if len(prices) <= period:
        return rsi
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = round(100.0 - (100.0 / (1.0 + rs)), 1)
    for i in range(period + 1, len(prices)):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = round(100.0 - (100.0 / (1.0 + rs)), 1)
    return rsi

def calculate_disparity(prices, sma_prices):
    disp = [None] * len(prices)
    for i in range(len(prices)):
        if sma_prices[i] and sma_prices[i] > 0:
            disp[i] = round((prices[i] / sma_prices[i]) * 100.0, 1)
    return disp

def analyze_signals(candles, timeframe="M"):
    if len(candles) < 25:
        return []
    closes = [c['close'] for c in candles]
    opens = [c['open'] for c in candles]
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    
    ma20 = calculate_sma(closes, 20)
    ma60 = calculate_sma(closes, 60)
    ma120 = calculate_sma(closes, 120)
    ma200 = calculate_sma(closes, 200)
    rsi14 = calculate_rsi(closes, 14)
    bb_upper, bb_mid, bb_lower = calculate_bollinger_bands(closes, 20, 2)
    disp20 = calculate_disparity(closes, ma20)
    
    if timeframe == 'D':
        disparity = calculate_disparity(closes, ma200)
    elif timeframe == 'W':
        disparity = calculate_disparity(closes, ma120)
    else:
        disparity = calculate_disparity(closes, ma20)
        
    results = []
    for i in range(len(candles)):
        c_close = closes[i]
        c_open = opens[i]
        c_low = lows[i]
        c_high = highs[i]
        c_rsi = rsi14[i]
        p_rsi = rsi14[i-1] if i > 0 and rsi14[i-1] is not None else c_rsi
        c_bbl = bb_lower[i]
        c_bbu = bb_upper[i]
        c_disp = disparity[i]
        c_disp20 = disp20[i]
        
        # 1. 캔들 확인 필터 (Confirm Candle): 양봉이거나 아래꼬리 반발 매수세 형성
        body = abs(c_close - c_open)
        lower_wick = min(c_close, c_open) - c_low
        is_bullish_confirm = (c_close >= c_open) or (lower_wick >= body * 1.1)
        
        # 2. 떨어지는 칼날 필터 (Falling Knife): 20선 급락 각도 & 밴드 하단 이탈 음봉
        is_falling_knife = False
        if i >= 5 and ma20[i] and ma20[i-5] and ma20[i] < ma20[i-5] * 0.975 and c_close < c_open:
            if c_bbl and c_close < c_bbl:
                is_falling_knife = True
        
        # ================= [ 정밀 매수 타점 분석 ] =================
        r1_support = False
        r2_rsi = False
        r3_bb_disp = False
        reasons_buy = []
        
        if timeframe == 'M':
            m20 = ma20[i]
            m60 = ma60[i]
            if m20 and (abs(c_low - m20) / m20 <= 0.03 or (c_low <= m20 and c_close >= m20 * 0.98)) and is_bullish_confirm:
                r1_support = True
                reasons_buy.append("20월선 지지확인")
            elif m60 and (abs(c_low - m60) / m60 <= 0.035 or (c_low <= m60 and c_close >= m60 * 0.98)) and is_bullish_confirm:
                r1_support = True
                reasons_buy.append("60월 경기선 지지확인")
            if c_rsi is not None and 42 <= c_rsi <= 56 and c_rsi >= p_rsi and is_bullish_confirm:
                r2_rsi = True
                reasons_buy.append(f"월봉 RSI({c_rsi}) 반등")
            if c_bbl and c_low <= c_bbl * 1.02 and c_close >= c_bbl * 0.98 and is_bullish_confirm:
                r3_bb_disp = True
                reasons_buy.append("월봉 볼린저 하단 지지")
            elif c_disp and c_disp <= 92:
                r3_bb_disp = True
                reasons_buy.append(f"월봉 이격도 과매도({c_disp}%)")
        elif timeframe == 'W':
            m50 = ma60[i]
            m120 = ma120[i]
            if m50 and (abs(c_low - m50) / m50 <= 0.02 or (c_low <= m50 and c_close >= m50 * 0.98)) and is_bullish_confirm:
                r1_support = True
                reasons_buy.append("50주선 지지확인")
            elif m120 and (abs(c_low - m120) / m120 <= 0.025 or (c_low <= m120 and c_close >= m120 * 0.98)) and is_bullish_confirm:
                r1_support = True
                reasons_buy.append("120주선 지지확인")
            if c_rsi is not None and 38 <= c_rsi <= 52 and c_rsi >= p_rsi and is_bullish_confirm:
                r2_rsi = True
                reasons_buy.append(f"주봉 RSI({c_rsi}) 반등")
            if c_bbl and c_low <= c_bbl * 1.015 and c_close >= c_bbl * 0.98 and is_bullish_confirm:
                r3_bb_disp = True
                reasons_buy.append("주봉 볼린저 하단 반등")
            elif c_disp and c_disp <= 94:
                r3_bb_disp = True
                reasons_buy.append(f"주봉 이격도({c_disp}%)")
        else: # 일봉 (D)
            m200 = ma200[i]
            m120 = ma120[i]
            if m200 and (abs(c_low - m200) / m200 <= 0.018 or (c_low <= m200 and c_close >= m200 * 0.99)) and is_bullish_confirm:
                r1_support = True
                reasons_buy.append("200일선 리테스트 지지")
            elif m120 and (abs(c_low - m120) / m120 <= 0.018 or (c_low <= m120 and c_close >= m120 * 0.99)) and is_bullish_confirm:
                r1_support = True
                reasons_buy.append("120일선 지지확인")
            if c_rsi is not None and 32 <= c_rsi <= 49 and c_rsi >= p_rsi and is_bullish_confirm:
                r2_rsi = True
                reasons_buy.append(f"일봉 RSI({c_rsi}) 반등")
            if c_bbl and c_low <= c_bbl * 1.01 and c_close >= c_bbl * 0.99 and is_bullish_confirm:
                r3_bb_disp = True
                reasons_buy.append("일봉 볼린저 하단 지지")
            elif c_disp and c_disp <= 95:
                r3_bb_disp = True
                reasons_buy.append(f"200일 이격도({c_disp}%)")

        score = sum([1 for r in [r1_support, r2_rsi, r3_bb_disp] if r])
        # 떨어지는 칼날 구간에서는 매수 점수 감점
        if is_falling_knife and score > 0:
            score = max(0, score - 1)
            
        buy_signal = "HOLD"
        if score == 3: buy_signal = "TRIPLE_BUY"
        elif score == 2: buy_signal = "STRONG_BUY"
        elif score == 1: buy_signal = "WEAK_BUY"

        # ================= [ 정밀 매도 / 익절 / 손절 분석 ] =================
        s1_bb_upper = False
        s2_rsi_overbought = False
        s3_disp_over = False
        reasons_sell = []
        
        # 1. 볼린저 상단 저항
        if c_bbu and c_high >= c_bbu * 0.995:
            s1_bb_upper = True
            reasons_sell.append("볼린저 상단 과열 도달")
            
        # 2. RSI 과열권 꺾임
        over_rsi = 68 if timeframe == 'D' else 65
        if c_rsi is not None and c_rsi >= over_rsi and c_rsi < p_rsi:
            s2_rsi_overbought = True
            reasons_sell.append(f"RSI({c_rsi}) 과열권 하향 꺾임")
            
        # 3. 20선 이격 과열
        if c_disp20 is not None and c_disp20 >= 108.0:
            s3_disp_over = True
            reasons_sell.append(f"20선 이격도 과열({c_disp20}%)")
            
        # 4. 손절/추세 이탈 경고
        is_stop_loss = False
        if timeframe == 'D' and ma200[i] and c_close < ma200[i] * 0.98 and opens[i] >= ma200[i] * 0.98:
            is_stop_loss = True
            reasons_sell.append("200일선 종가 이탈(손절/비중축소)")
        elif timeframe == 'W' and ma60[i] and c_close < ma60[i] * 0.975 and opens[i] >= ma60[i] * 0.975:
            is_stop_loss = True
            reasons_sell.append("60주선 종가 이탈(중기 손절)")
            
        sell_score = sum([1 for s in [s1_bb_upper, s2_rsi_overbought, s3_disp_over] if s])
        sell_signal = "HOLD"
        if is_stop_loss:
            sell_signal = "STOP_LOSS"
        elif sell_score >= 2:
            sell_signal = "TAKE_PROFIT"
        elif sell_score == 1:
            sell_signal = "SELL_WARN"
        
        results.append({
            'date': candles[i]['date'],
            'open': c_open,
            'high': c_high,
            'low': c_low,
            'close': c_close,
            'volume': candles[i]['volume'],
            'ma20': ma20[i],
            'ma60': ma60[i],
            'ma120': ma120[i],
            'ma200': ma200[i],
            'bb_upper': c_bbu,
            'bb_lower': c_bbl,
            'rsi': c_rsi,
            'disparity': c_disp,
            'score': score,
            'signal': buy_signal,
            'reasons_buy': reasons_buy,
            'r1_support': r1_support,
            'r2_rsi': r2_rsi,
            'r3_bb_disp': r3_bb_disp,
            # 매도/익절/손절 신호
            'sell_score': sell_score,
            'sell_signal': sell_signal,
            'reasons_sell': reasons_sell,
            'is_stop_loss': is_stop_loss
        })
    return results
