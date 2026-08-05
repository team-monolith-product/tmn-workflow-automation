"""
뿌리오 웹 발송결과 조회 Service Layer (Playwright)

뿌리오 API에는 발송결과 조회 엔드포인트가 없다(/v1/token, /v1/message, /v1/cancel 뿐).
발송 응답의 code 1000은 '접수 성공'일 뿐이고, 최종 도달 여부는 웹 발송결과 페이지에만
남으므로 여기서 브라우저로 로그인해 표를 읽는다.

환경 변수:
- PPURIO_WEB_ID / PPURIO_WEB_PASSWORD: 뿌리오 웹 로그인 계정
- PPURIO_WEB_LOGIN_URL / PPURIO_WEB_RESULT_URL: 로그인·발송결과 페이지 주소
- PPURIO_WEB_ID_SELECTOR / PPURIO_WEB_PW_SELECTOR: 로그인 폼 셀렉터

주소·셀렉터 기본값은 추정치다. 실제 계정으로 한 번 보정할 것:
    python -m service.ppurio_result --dump
로그인 후 페이지 HTML·스크린샷을 tmp/ 에 떨궈 준다.
"""

import asyncio
import os
import re

from bs4 import BeautifulSoup

LOGIN_URL = os.environ.get("PPURIO_WEB_LOGIN_URL", "https://www.ppurio.com/login")
RESULT_URL = os.environ.get(
    "PPURIO_WEB_RESULT_URL", "https://www.ppurio.com/send/result"
)
ID_SELECTOR = os.environ.get("PPURIO_WEB_ID_SELECTOR", "input[name='userId']")
PW_SELECTOR = os.environ.get("PPURIO_WEB_PW_SELECTOR", "input[type='password']")

PAGE_TIMEOUT_MS = 30000

# 컨테이너 구동 조건. 둘 다 없으면 로컬에서는 되고 서버에서만 죽는다.
# --no-sandbox: 이미지가 root 로 돌아 Chromium 샌드박스를 못 쓴다(securityContext 비어 있음)
# --disable-dev-shm-usage: 쿠버네티스 기본 /dev/shm 이 64MB 라 탭이 공유메모리 부족으로 죽는다
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

_PHONE_PATTERN = re.compile(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}")
# 뿌리오 발송결과 표기: 성공/실패/대기 계열. 표기가 조금씩 달라 계열 단위로 잡는다.
_STATUS_PATTERN = re.compile(
    r"성공|수신|도착|완료|실패|거부|차단|오류|대기|전송중|발송중|접수"
)
_SUCCESS_WORDS = ("성공", "수신", "도착", "완료")
_FAILURE_WORDS = ("실패", "거부", "차단", "오류")

DELIVERED = "성공"
FAILED = "실패"
PENDING = "대기"


def normalize_status(word: str) -> str:
    """뿌리오 상태 문구를 성공/실패/대기 셋 중 하나로 정규화합니다.

    Args:
        word: 결과 표에서 뽑은 상태 문구

    Returns:
        str: DELIVERED / FAILED / PENDING 중 하나
    """
    if word in _SUCCESS_WORDS:
        return DELIVERED
    if word in _FAILURE_WORDS:
        return FAILED
    return PENDING


def parse_results(html: str) -> dict[str, str]:
    """발송결과 페이지 HTML에서 {번호: 상태} 매핑을 뽑습니다.

    CSS 셀렉터를 박지 않고 '한 행에 수신번호와 상태 문구가 함께 있다'는 구조만 가정한다.
    뿌리오 웹은 개편이 잦아 셀렉터를 고정하면 조용히 빈 결과를 반환하게 된다.

    Args:
        html: 발송결과 페이지 HTML

    Returns:
        dict[str, str]: 숫자만 남긴 수신번호 -> 성공/실패/대기
    """
    soup = BeautifulSoup(html, "lxml")
    results: dict[str, str] = {}

    for row in soup.find_all("tr"):
        text = row.get_text(" ", strip=True)
        phone_match = _PHONE_PATTERN.search(text)
        status_match = _STATUS_PATTERN.search(text)
        if not phone_match or not status_match:
            continue
        phone = re.sub(r"\D", "", phone_match.group())
        status = normalize_status(status_match.group())
        # 같은 번호가 여러 행에 있으면 최신(위쪽) 행을 남긴다
        results.setdefault(phone, status)

    return results


async def _login_and_get_result_html(page) -> str:
    """로그인 후 발송결과 페이지 HTML을 반환합니다."""
    await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT_MS)
    await page.fill(ID_SELECTOR, os.environ["PPURIO_WEB_ID"])
    await page.fill(PW_SELECTOR, os.environ["PPURIO_WEB_PASSWORD"])
    await page.press(PW_SELECTOR, "Enter")
    await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)

    await page.goto(RESULT_URL, timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    return await page.content()


async def fetch_results(phones: list[str]) -> dict[str, str]:
    """뿌리오 웹 발송결과 페이지에서 번호별 발송 상태를 조회합니다.

    Args:
        phones: 조회할 수신번호 목록 (숫자만)

    Returns:
        dict[str, str]: 번호 -> 성공/실패/대기. 표에 없는 번호는 대기로 본다.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=LAUNCH_ARGS)
        page = await browser.new_page()
        html = await _login_and_get_result_html(page)
        await browser.close()

    # ponytail: 결과 페이지 첫 장만 읽는다. 한 번에 100건 넘게 보내면 페이지네이션이 필요하다.
    parsed = parse_results(html)
    return {phone: parsed.get(phone, PENDING) for phone in phones}


async def _dump() -> None:
    """셀렉터 보정용 — 로그인 후 페이지 HTML과 스크린샷을 tmp/ 에 저장합니다."""
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
