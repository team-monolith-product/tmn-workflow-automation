"""
도달 결과 계층입니다. 뿌리오 웹 발송결과 페이지를 읽어 result_code 를 채웁니다.

뿌리오 v1 에는 결과 조회 API 가 없습니다(token·message·cancel 뿐). 접수 응답의
code 1000 은 "받았다"이지 "도달했다"가 아니고, 우리는 수신자 N 명을 한 번에
보내므로 접수 응답 하나로는 누가 못 받았는지 알 수 없습니다. 개별 결과가 남는
곳은 웹 페이지뿐이라 브라우저로 읽습니다.

비즈뿌리오로 옮기면 POST /v1/result/request 폴링이 이 파일을 대체합니다.
계정이 달라 지금은 쓸 수 없습니다(3006 invalid account in bizppurio).

환경 변수:
- PPURIO_WEB_ID / PPURIO_WEB_PASSWORD: 웹 로그인 계정
- PPURIO_WEB_LOGIN_URL / PPURIO_WEB_RESULT_URL: 로그인·발송결과 페이지 주소
- PPURIO_WEB_ID_SELECTOR / PPURIO_WEB_PW_SELECTOR: 로그인 폼 셀렉터

주소·셀렉터 기본값은 추정치입니다. 실제 계정으로 한 번 보정하세요:
    python -m service.sms.result --dump
"""

import asyncio
import datetime
import os
import re
from bs4 import BeautifulSoup

from service.sms import ledger

LOGIN_URL = os.environ.get("PPURIO_WEB_LOGIN_URL", "https://www.ppurio.com/login")
RESULT_URL = os.environ.get(
    "PPURIO_WEB_RESULT_URL", "https://www.ppurio.com/send/result"
)
ID_SELECTOR = os.environ.get("PPURIO_WEB_ID_SELECTOR", "input[name='userId']")
PW_SELECTOR = os.environ.get("PPURIO_WEB_PW_SELECTOR", "input[type='password']")

PAGE_TIMEOUT_MS = 30000

# 컨테이너 구동 조건. 둘 다 없으면 로컬에서는 되고 서버에서만 죽습니다.
# --no-sandbox: 이미지가 root 로 돌아 Chromium 샌드박스를 못 씁니다
# --disable-dev-shm-usage: 쿠버네티스 기본 /dev/shm 이 64MB 라 탭이 죽습니다
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

# 발송이력 시트의 '결과' 열에 그대로 들어가는 값입니다. 재발송 판정이
# 결과 == FAILED 정확 일치라, 이 둘 말고 다른 값을 내보내면 도달도 실패도
# 아닌 상태가 되어 재발송에서 조용히 빠집니다. parse_results 는 반드시
# 이 둘 중 하나만 돌려줍니다.
DELIVERED = "0000"
FAILED = "FAIL"

# 발송결과 표기가 조금씩 달라 계열 단위로 잡습니다.
_SUCCESS_WORDS = ("성공", "수신", "도착", "완료")
_FAILURE_WORDS = ("실패", "거부", "차단", "오류")
_PENDING_WORDS = ("대기", "전송중", "발송중", "접수")

# 표의 헤더 이름 후보. 어느 열을 읽을지 여기서 정합니다.
_PHONE_HEADERS = ("수신번호", "수신자", "휴대폰", "연락처", "번호")
_STATUS_HEADERS = ("결과", "상태")
_TIME_HEADERS = ("일시", "일자", "시간")

_TIME_PATTERN = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})\D+(\d{1,2}):(\d{2})")


class ResultPageChanged(RuntimeError):
    """발송결과 페이지에서 필요한 열을 찾지 못했을 때 발생합니다.

    조용히 빈 결과를 돌려주면 "벤더가 아직 결과를 안 올렸다"와 구분되지 않아,
    도달 확인이 통째로 죽어 있어도 영원히 들키지 않습니다.
    """


def _column(cells: list[str], names: tuple[str, ...]) -> int | None:
    """헤더 행에서 그 이름을 담은 열의 위치를 찾습니다.

    Args:
        cells: 헤더 행의 셀 텍스트
        names: 찾을 이름 후보

    Returns:
        int | None: 열 위치. 없으면 None
    """
    for index, cell in enumerate(cells):
        if any(name in cell for name in names):
            return index
    return None


def _cells(row) -> list[str]:
    """행의 셀 텍스트를 순서대로 뽑습니다."""
    return [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]


def _row_time(text: str) -> datetime.datetime | None:
    """셀 텍스트에서 일시를 읽습니다. 형식이 다르면 None."""
    found = _TIME_PATTERN.search(text)
    if not found:
        return None
    year, month, day, hour, minute = (int(group) for group in found.groups())
    return datetime.datetime(year, month, day, hour, minute)


def parse_results(
    html: str, sent_after: datetime.datetime | None = None
) -> dict[str, str]:
    """발송결과 페이지 HTML 에서 {번호: 결과코드} 매핑을 뽑습니다.

    헤더에서 열 위치를 찾아 그 셀만 읽습니다. 행 전체 텍스트를 훑으면 문안
    본문에 박힌 문의 전화번호가 수신번호로, 본문의 '실패'·'오류' 같은 낱말이
    상태로 잡힙니다. 실제 discord 문안에 담당자 번호가 들어 있어 이건 가정이
    아니라 확정된 오분류였습니다.

    발송결과 페이지는 누적 이력입니다. sent_after 를 주면 그 시각 이후 행만
    채택합니다. 주지 않으면 같은 번호에 보낸 지난 발송의 결과를 이번 발송의
    결과로 읽습니다.

    Args:
        html: 발송결과 페이지 HTML
        sent_after: 이 시각 이후에 발송된 행만 본다

    Returns:
        dict[str, str]: 번호 -> DELIVERED/FAILED. 아직 결과가 없는 행은 담지 않는다

    Raises:
        ResultPageChanged: 수신번호·결과 열을 찾지 못했을 때. sent_after 를
            줬는데 일시 열이 없을 때도 같다
    """
    soup = BeautifulSoup(html, "lxml")

    phone_at = status_at = time_at = None
    rows = soup.find_all("tr")
    for index, row in enumerate(rows):
        cells = _cells(row)
        phone_at = _column(cells, _PHONE_HEADERS)
        status_at = _column(cells, _STATUS_HEADERS)
        if phone_at is not None and status_at is not None:
            time_at = _column(cells, _TIME_HEADERS)
            rows = rows[index + 1 :]
            break
    else:
        raise ResultPageChanged(
            "발송결과 표에서 수신번호·결과 열을 찾지 못했습니다. "
            "페이지가 바뀌었는지 `python -m service.sms.result --dump` 로 확인하세요."
        )

    if sent_after is not None and time_at is None:
        raise ResultPageChanged(
            "발송결과 표에 일시 열이 없어 이번 발송분만 골라낼 수 없습니다. "
            "지난 발송 결과를 이번 것으로 읽으면 도달한 사람에게 또 보냅니다."
        )

    results: dict[str, str] = {}
    for row in rows:
        cells = _cells(row)
        if max(phone_at, status_at) >= len(cells):
            continue
        phone = re.sub(r"\D", "", cells[phone_at])
        status = cells[status_at]
        if not phone or not status:
            continue
        if sent_after is not None:
            at = _row_time(cells[time_at])
            if at is None or at < sent_after:
                continue
        # 실패를 성공보다 먼저 본다. '수신실패'는 성공어 '수신'을 품고 있어
        # 순서를 뒤집으면 실패가 도달로 보고되고, 그 사람은 재발송 대상에서
        # 빠진다. 미확정은 둘 다보다 먼저 본다('수신대기').
        if any(word in status for word in _PENDING_WORDS):
            continue
        if any(word in status for word in _FAILURE_WORDS):
            code = FAILED
        elif any(word in status for word in _SUCCESS_WORDS):
            code = DELIVERED
        else:
            continue
        # 같은 번호가 여러 행에 있으면 최신(위쪽) 행을 남깁니다
        results.setdefault(phone, code)

    return results


async def _login_and_get_result_html(page) -> str:
    """로그인 후 발송결과 페이지 HTML 을 반환합니다.

    Raises:
        ResultPageChanged: 로그인 뒤에도 로그인 페이지에 머물러 있을 때
    """
    await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT_MS)
    await page.fill(ID_SELECTOR, os.environ["PPURIO_WEB_ID"])
    await page.fill(PW_SELECTOR, os.environ["PPURIO_WEB_PASSWORD"])
    await page.press(PW_SELECTOR, "Enter")
    await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)

    await page.goto(RESULT_URL, timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    if page.url.rstrip("/") == LOGIN_URL.rstrip("/"):
        # 로그인 실패면 로그인 페이지 HTML 이 돌아오고, 파싱은 빈 결과를 낸다.
        # 그건 "아직 결과가 안 올라왔다"와 구분되지 않아 영원히 안 들킨다.
        raise ResultPageChanged(
            "로그인 후에도 로그인 페이지입니다. 계정이나 셀렉터를 확인하세요."
        )
    return await page.content()


async def fetch_results(
    phones: list[str], sent_after: datetime.datetime | None = None
) -> dict[str, str]:
    """뿌리오 웹에서 번호별 도달 결과를 읽습니다.

    Args:
        phones: 조회할 수신번호 목록 (숫자만)
        sent_after: 이 시각 이후 발송분만 본다. 발송결과 페이지는 누적이라,
            주지 않으면 같은 번호에 보낸 지난 발송의 결과를 읽는다

    Returns:
        dict[str, str]: 결과가 확정된 번호만 담은 {번호: 결과코드}
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=LAUNCH_ARGS)
        page = await browser.new_page()
        html = await _login_and_get_result_html(page)
        await browser.close()

    # ponytail: 결과 페이지 첫 장만 읽습니다. 한 번에 100건을 넘기면 페이지네이션이 필요합니다.
    wanted = set(phones)
    return {
        phone: code
        for phone, code in parse_results(html, sent_after).items()
        if phone in wanted
    }


def record(
    spreadsheet_id: str, campaign: str, statuses: dict[str, str]
) -> list[dict[str, str]]:
    """도달 결과를 이력 시트에 기록하고, 실패한 수신자를 돌려줍니다.

    이미 결과가 찬 행은 건드리지 않습니다. 폴링이 여러 번 돌아도 처음 확정된
    결과가 남습니다.

    Args:
        spreadsheet_id: 참가자 스프레드시트
        campaign: 발송 건 식별자
        statuses: fetch_results 결과

    Returns:
        list[dict[str, str]]: 재발송 대상 [{"to": 번호, "name": 이름}]
    """
    ws = ledger.open_ledger(spreadsheet_id)
    ledger.record_results(ws, campaign, statuses)
    return ledger.failed_targets(ledger.read_rows(ws), campaign, FAILED)


async def _dump() -> None:
    """셀렉터 보정용 — 로그인 후 페이지 HTML 과 스크린샷을 tmp/ 에 저장합니다."""
    from playwright.async_api import async_playwright

    os.makedirs("tmp", exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=LAUNCH_ARGS)
        page = await browser.new_page()

        await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT_MS)
        with open("tmp/ppurio_login.html", "w", encoding="utf-8") as file:
            file.write(await page.content())
        await page.screenshot(path="tmp/ppurio_login.png", full_page=True)

        html = await _login_and_get_result_html(page)
        with open("tmp/ppurio_result.html", "w", encoding="utf-8") as file:
            file.write(html)
        await page.screenshot(path="tmp/ppurio_result.png", full_page=True)
        await browser.close()

    print(f"현재 URL 설정: 로그인 {LOGIN_URL} / 결과 {RESULT_URL}")
    print(f"파싱된 결과 {len(parse_results(html))}건 — tmp/ppurio_*.html, *.png 확인")


if __name__ == "__main__":
    asyncio.run(_dump())
