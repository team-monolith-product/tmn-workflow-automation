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
import os
import re
from typing import Any

from service.sms import ledger

from bs4 import BeautifulSoup

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

# sms_send.result_code 규약. 스키마의 실패 판정이 result_code <> '0000' 이라
# 성공만 0000 이고 나머지는 실패로 셉니다. 웹에는 벤더 숫자 코드가 안 보여
# 페이지 문구를 이 둘로 접습니다.
DELIVERED = "0000"
FAILED = "FAIL"

_PHONE_PATTERN = re.compile(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}")
# 발송결과 표기가 조금씩 달라 계열 단위로 잡습니다.
_SUCCESS_WORDS = ("성공", "수신", "도착", "완료")
_FAILURE_WORDS = ("실패", "거부", "차단", "오류")
_PENDING_WORDS = ("대기", "전송중", "발송중", "접수")
_STATUS_PATTERN = re.compile("|".join(_SUCCESS_WORDS + _FAILURE_WORDS + _PENDING_WORDS))


def parse_results(html: str) -> dict[str, str]:
    """발송결과 페이지 HTML 에서 {번호: 결과코드} 매핑을 뽑습니다.

    CSS 셀렉터를 박지 않고 '한 행에 수신번호와 상태 문구가 함께 있다'는 구조만
    가정합니다. 뿌리오 웹은 개편이 잦아 셀렉터를 고정하면 조용히 빈 결과를
    돌려주고, 그러면 전원 미확정으로 보여 아무도 눈치채지 못합니다.

    Args:
        html: 발송결과 페이지 HTML

    Returns:
        dict[str, str]: 번호 -> DELIVERED/FAILED. 아직 결과가 없는 행은 담지 않는다
    """
    soup = BeautifulSoup(html, "lxml")
    results: dict[str, str] = {}

    for row in soup.find_all("tr"):
        text = row.get_text(" ", strip=True)
        phone_match = _PHONE_PATTERN.search(text)
        status_match = _STATUS_PATTERN.search(text)
        if not phone_match or not status_match:
            continue
        word = status_match.group()
        if word in _PENDING_WORDS:
            continue
        phone = re.sub(r"\D", "", phone_match.group())
        # 같은 번호가 여러 행에 있으면 최신(위쪽) 행을 남깁니다
        results.setdefault(phone, DELIVERED if word in _SUCCESS_WORDS else FAILED)

    return results


async def _login_and_get_result_html(page) -> str:
    """로그인 후 발송결과 페이지 HTML 을 반환합니다."""
    await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT_MS)
    await page.fill(ID_SELECTOR, os.environ["PPURIO_WEB_ID"])
    await page.fill(PW_SELECTOR, os.environ["PPURIO_WEB_PASSWORD"])
    await page.press(PW_SELECTOR, "Enter")
    await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)

    await page.goto(RESULT_URL, timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    return await page.content()


async def fetch_results(phones: list[str]) -> dict[str, str]:
    """뿌리오 웹에서 번호별 도달 결과를 읽습니다.

    Args:
        phones: 조회할 수신번호 목록 (숫자만)

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
        phone: code for phone, code in parse_results(html).items() if phone in wanted
    }


def record(campaign: str, statuses: dict[str, str]) -> list[dict[str, str]]:
    """도달 결과를 이력 시트에 기록하고, 실패한 수신자를 돌려줍니다.

    이미 결과가 찬 행은 건드리지 않습니다. 폴링이 여러 번 돌아도 처음 확정된
    결과가 남습니다.

    Args:
        campaign: 발송 건 식별자
        statuses: fetch_results 결과

    Returns:
        list[dict[str, str]]: 재발송 대상 [{"to": 번호, "name": 이름}]
    """
    ws = ledger.open_ledger()
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
