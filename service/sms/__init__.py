"""문자 발송 계층.

컨테이너에 TZ 가 설정돼 있지 않아 datetime.now() 는 UTC 입니다. 반면 사람이
읽는 것(이력 시트의 일시)과 벤더가 해석하는 것(예약 sendTime)은 전부 KST 라,
벽시계가 필요한 곳은 이 상수를 거칩니다. 그러지 않으면 시트가 9시간 어긋나고
"3분 뒤" 검사가 이미 지난 시각을 통과시킵니다.
"""

from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
