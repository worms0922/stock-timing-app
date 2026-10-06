"""Legacy connector retired; the application now uses toss_connector.TossConnector."""
class NaverFinanceConnector:
    def __init__(self, *args, **kwargs):
        raise RuntimeError('네이버 가상 시세 대체 기능은 제거되었습니다. TossConnector를 사용하세요.')
