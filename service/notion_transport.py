"""
노션 API 호출을 한도 아래로 묶는 httpx 전송 계층입니다.

노션은 커넥션당 평균 초당 3회를 허용하고 넘으면 429를 돌려줍니다. 호출 지점에서
워커 수로 지키려 하면 어긋납니다. 페이지 하나를 마크다운으로 바꾸는 데도
자식이 있는 블록마다 blocks.children.list를 부르므로, "페이지 3개 동시"가
"초당 3회"가 아닙니다. 실측에서 표본 2,851행 중 1,616건이 429였고, 본문 조회
실패는 본문 0자로 취급되어 문서형 데이터베이스가 표로 잘못 판정됐습니다.

전송 계층에 두면 notion_to_md 내부 호출까지 한 계량기를 지납니다. 호출자는
동시성을 신경 쓰지 않아도 됩니다.
"""

import threading
import time

import httpx

# 초당 허용 호출 수. 한도가 평균 3회라 그 아래에 둡니다. 같은 토큰을 쓰는
# 프로세스가 슬랙봇과 FastAPI 둘이라 각자 여기까지 쓰면 합이 한도에 닿습니다.
REQUESTS_PER_SECOND = 2.5

# 429를 맞았을 때 다시 칠 횟수. 한도는 평균이라 짧은 초과는 기다리면 풀립니다.
MAX_RETRIES = 5

# Retry-After가 없을 때 기다릴 시간.
DEFAULT_RETRY_AFTER = 1.0


class ThrottledTransport(httpx.HTTPTransport):
    """호출 간격을 벌리고 429를 기다렸다 다시 칩니다.

    간격은 프로세스 전체에서 하나로 셉니다. 스레드풀로 동시에 불러도 실제
    호출은 여기서 줄을 섭니다.
    """

    def __init__(
        self,
        requests_per_second: float = REQUESTS_PER_SECOND,
        max_retries: int = MAX_RETRIES,
        **kwargs,
    ) -> None:
        """
        Args:
            requests_per_second: 초당 허용 호출 수
            max_retries: 429를 맞았을 때 다시 칠 횟수
            kwargs: httpx.HTTPTransport에 그대로 넘길 인자
        """
        super().__init__(**kwargs)
        self._interval = 1 / requests_per_second
        self._max_retries = max_retries
        self._lock = threading.Lock()
        self._next_at = 0.0

    def _wait_turn(self) -> None:
        """자기 차례가 올 때까지 기다립니다."""
        with self._lock:
            start = max(time.monotonic(), self._next_at)
            self._next_at = start + self._interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """
        Args:
            request: 보낼 요청

        Returns:
            httpx.Response: 응답. 재시도를 다 쓰면 마지막 429를 그대로 돌려줍니다
        """
        for attempt in range(self._max_retries + 1):
            self._wait_turn()
            response = super().handle_request(request)
            if response.status_code != 429 or attempt == self._max_retries:
                return response

            retry_after = response.headers.get("Retry-After")
            response.read()
            response.close()
            time.sleep(float(retry_after) if retry_after else DEFAULT_RETRY_AFTER)
        return response


def build_client(**kwargs) -> httpx.Client:
    """한도를 지키는 httpx 클라이언트를 만듭니다.

    notion_client.Client의 client 인자로 넘깁니다.

    Args:
        kwargs: ThrottledTransport에 넘길 인자

    Returns:
        httpx.Client: 모든 요청이 계량기를 지나는 클라이언트
    """
    return httpx.Client(transport=ThrottledTransport(**kwargs))
