"""
나라장터(G2B) 입찰공고정보서비스 API 모듈

조달청_나라장터 입찰공고정보서비스 (공공데이터포털 데이터 15129394)의
입찰공고목록 오퍼레이션을 호출하는 직접 래퍼.

- 엔드포인트: http://apis.data.go.kr/1230000/ad/BidPublicInfoService/<operation>
- 업무구분별 오퍼레이션을 따로 호출해야 정상 응답을 받는다 (용역/물품/공사/외자).
- 인증키는 공공데이터포털에서 이 서비스에 대해 별도 활용신청·승인이 필요하다.
  (공휴일 API 키 DATA_GO_KR_SPECIAL_DAY_KEY 와는 활용신청 대상이 다르다.)
"""

import os
import requests

BASE_URL = "http://apis.data.go.kr/1230000/ad/BidPublicInfoService"

# 업무구분(kind) → 입찰공고목록 오퍼레이션명
BID_LIST_OPERATIONS = {
    "servc": "getBidPblancListInfoServc",  # 용역
    "thng": "getBidPblancListInfoThng",  # 물품
    "cnstwk": "getBidPblancListInfoCnstwk",  # 공사
    "frgcpt": "getBidPblancListInfoFrgcpt",  # 외자
}

# 업무구분 한글 라벨 (보고용)
KIND_LABELS = {
    "servc": "용역",
    "thng": "물품",
    "cnstwk": "공사",
    "frgcpt": "외자",
}

# 조회구분(inqryDiv): 1=공고게시일시 기준, 2=개찰일시 기준
INQRY_DIV_NOTICE_DATE = "1"


def get_bid_pblanc_list(
    kind: str,
    inqry_bgn: str,
    inqry_end: str,
    page_no: int = 1,
    num_of_rows: int = 100,
    inqry_div: str = INQRY_DIV_NOTICE_DATE,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> dict:
    """입찰공고목록을 업무구분별로 1페이지 조회한다 (raw JSON 반환).

    Args:
        kind: 업무구분 ("servc"=용역, "thng"=물품, "cnstwk"=공사, "frgcpt"=외자)
        inqry_bgn: 조회 시작 일시 (YYYYMMDDHHMM)
        inqry_end: 조회 종료 일시 (YYYYMMDDHHMM)
        page_no: 페이지 번호
        num_of_rows: 페이지당 행 수
        inqry_div: 조회구분 (1=공고게시일시, 2=개찰일시)
        timeout: 요청 타임아웃(초)
        session: 재사용할 requests 세션 (선택)

    Returns:
        공공데이터포털 응답 JSON (response.body.items 구조)
    """
    if kind not in BID_LIST_OPERATIONS:
        raise ValueError(
            f"알 수 없는 업무구분 '{kind}'. 가능: {sorted(BID_LIST_OPERATIONS)}"
        )

    service_key = os.environ.get("DATA_GO_KR_BID_KEY")
    if not service_key:
        raise RuntimeError(
            "DATA_GO_KR_BID_KEY 환경변수가 없습니다. "
            "공공데이터포털에서 '조달청_나라장터 입찰공고정보서비스'(데이터 15129394) "
            "활용신청 후 발급된 일반 인증키(Decoding)를 설정하세요."
        )

    http = session or requests
    url = f"{BASE_URL}/{BID_LIST_OPERATIONS[kind]}"
    params = {
        "serviceKey": service_key,
        "inqryDiv": inqry_div,
        "inqryBgnDt": inqry_bgn,
        "inqryEndDt": inqry_end,
        "pageNo": str(page_no),
        "numOfRows": str(num_of_rows),
        "type": "json",
    }
    resp = http.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
