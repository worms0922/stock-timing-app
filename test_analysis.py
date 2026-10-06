import datetime as dt
import json
import os
import unittest
import base64
import threading
import urllib.request
import urllib.error
from unittest.mock import patch
from toss_connector import KST, TossConnector, DataError, aggregate
from flow_analysis import flow_score, scalp_analysis
from live_stream import LiveStream

class AnalysisTests(unittest.TestCase):
    def test_private_http_endpoints_and_health(self):
        import app
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        url = 'http://127.0.0.1:' + str(server.server_port)
        def get(path, auth=None):
            headers = {'Authorization':auth} if auth else {}
            try:
                with urllib.request.urlopen(urllib.request.Request(url+path,headers=headers),timeout=3) as response:
                    return response.status,response.read().decode()
            except urllib.error.HTTPError as error:
                return error.code,error.read().decode()
        try:
            with patch.dict(os.environ,{'APP_USERNAME':'','APP_PASSWORD':''}):
                self.assertEqual(get('/')[0],503)
                self.assertEqual(json.loads(get('/api/health')[1]),{'status':'ok'})
            with patch.dict(os.environ,{'APP_USERNAME':'test','APP_PASSWORD':'test-private-password'}):
                self.assertEqual(get('/api/live')[0],401)
                self.assertEqual(get('/','Basic invalid')[0],401)
                auth='Basic '+base64.b64encode(b'test:test-private-password').decode()
                self.assertEqual(get('/',auth)[0],200)
                self.assertEqual(get('/.env',auth)[0],404)
        finally:
            server.shutdown()
            server.server_close()

    def test_flow_no_future_or_missing_promoted(self):
        rows = [{'date':f'2026-10-{day:02}', 'foreigner':{'netBuyVolume':'100'},
                 'institution':{'netBuyVolume':'50'}, 'individual':None} for day in range(1,7)]
        bars=[{'date':r['date'],'volume':1000} for r in rows]
        flow=flow_score(rows,bars,3,as_of='2026-10-05')
        self.assertEqual(flow['date'],'2026-10-05')
        self.assertEqual(flow['net5'],750)
        self.assertEqual(flow['combined'],100)
        self.assertTrue(flow['provisional'])
        rows[-2]['institution']=None
        self.assertIsNone(flow_score(rows,bars,3,as_of='2026-10-05')['combined'])
        self.assertIsNone(flow_score([],bars,3)['score'])

    def test_minute_end_time_and_stale_stream_block_signal(self):
        now=dt.datetime(2026,10,6,10,0,30,tzinfo=KST)
        bars=[]
        for n in range(22):
            stamp=dt.datetime(2026,10,6,9,40,tzinfo=KST)+dt.timedelta(minutes=n)
            bars.append({'date':'2026-10-06','timestamp':stamp.isoformat(),
                         'open':100,'high':100,'low':100,'close':100,'volume':100 if n<20 else 500})
        book={'timestamp':now.isoformat(),'bids':[{'price':'101','volume':'700'}],
              'asks':[{'price':'101.1','volume':'300'}]}
        trade={'timestamp':now.isoformat(),'price':'101'}
        result=scalp_analysis(bars,book,trade,True,now)
        self.assertEqual(result['volume_ratio'],5)
        self.assertEqual(result['score'],100)
        book['timestamp']=(now-dt.timedelta(seconds=31)).isoformat()
        result=scalp_analysis(bars,book,trade,True,now)
        self.assertIsNone(result['score'])
        self.assertFalse(result['fresh'])
        self.assertTrue(all(x['passed'] is None for x in result['checks']))

    def test_missing_book_and_wide_spread(self):
        now=dt.datetime(2026,10,6,10,0,tzinfo=KST)
        self.assertIsNone(scalp_analysis([],None,None,False,now)['score'])
        from flow_analysis import age_seconds
        self.assertIsNone(age_seconds((now+dt.timedelta(minutes=5)).isoformat(),now))

    def test_pagination_overlap_and_chronological_order(self):
        client=TossConnector()
        def bar(day):
            return {'timestamp':f'2026-10-{day:02}T00:00:00+09:00',
                    'openPrice':'10','highPrice':'12','lowPrice':'9','closePrice':str(day),'volume':'100'}
        pages=[{'candles':[bar(3),bar(2)],'nextBefore':bar(2)['timestamp']},
               {'candles':[bar(2),bar(1)],'nextBefore':None}]
        with patch.object(client,'get',side_effect=pages):
            candles=client.candles('005930',count=250)
        self.assertEqual([c['close'] for c in candles],[1,2,3])
        self.assertEqual(len(aggregate(candles,'W')),1)
        self.assertEqual(aggregate(candles,'M')[0]['volume'],300)

    def test_subscription_switch_and_disconnect(self):
        stream=LiveStream(TossConnector()); stream.code='005930'
        stream.handle({'type':'subscriptions','subscribed':['trade:kr:005930','orderbook:kr:005930']})
        self.assertTrue(stream.connected)
        stream.handle({'type':'message','topic':'trade:kr:000660','data':{'price':'1'}})
        self.assertIsNone(stream.trade)
        with self.assertRaises(RuntimeError):
            stream.handle({'type':'error'})
        self.assertFalse(stream.connected)

    def test_credentials_required_and_symbol_validation(self):
        with patch.dict(os.environ,{'TOSS_CLIENT_ID':'','TOSS_CLIENT_SECRET':''}):
            with self.assertRaises(DataError): TossConnector().token()
        with self.assertRaises(DataError): TossConnector().resolve_code('../secret')

if __name__ == '__main__':
    unittest.main()
