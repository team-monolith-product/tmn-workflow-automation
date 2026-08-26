"""시트 카탈로그 정규화 테스트 — 셀 값이 새어 들어가면 안 된다."""

from service.sheets import catalog

FILE = {
    "id": "1AbC",
    "name": "[기업연계] 부산 2기 만족도 조사 응답",
    "modifiedTime": "2026-08-15T09:30:00.000Z",
    "webViewLink": "https://docs.google.com/spreadsheets/d/1AbC/edit",
}
TABS = [
    {"id": 0, "title": "설문지 응답 시트1", "header": ["타임스탬프", "트랙", "출석"]},
    {"id": 77, "title": "공문신청", "header": ["성함", "학교"]},
]


def test_이름과_탭과_머리행이_검색_대상이_된다():
    # "출석 컬럼 있는 시트" 를 찾는 것이 이 카탈로그의 존재 이유다.
    text = catalog.build_raw_text(FILE["name"], TABS)

    assert "부산 2기 만족도" in text
    assert "공문신청" in text
    assert "출석" in text


def test_셀_값은_들어가지_않는다():
    # 응답이 계속 쌓이므로 값을 적재하면 곧 낡는다. 낡은 숫자를 진실로 믿는 것이
    # 값을 모르는 것보다 나쁘다.
    row = catalog.build_row(1, FILE, TABS)

    assert "이진선" not in row["raw_text"]
    assert row["raw_text"].count("\n") == len(TABS)


def test_셀이_바뀌어도_해시가_그대로다():
    # 해시가 흔들리면 응답이 들어올 때마다 재적재가 돈다. 머리행만 보므로
    # 300건이 쌓여도 카탈로그는 조용하다.
    before = catalog.build_row(1, FILE, TABS)
    after = catalog.build_row(
        1, {**FILE, "modifiedTime": "2026-08-21T11:00:00.000Z"}, TABS
    )

    assert before["content_hash"] == after["content_hash"]


def test_열이_늘면_해시가_바뀐다():
    before = catalog.build_row(1, FILE, TABS)
    grown = [{**TABS[0], "header": TABS[0]["header"] + ["공문"]}, TABS[1]]

    after = catalog.build_row(1, FILE, grown)

    assert before["content_hash"] != after["content_hash"]


def test_탭별_gid와_열을_남긴다():
    # 시트를 찾은 뒤 execute_python 에 넘길 값이다. 없으면 탭을 다시 물어봐야 한다.
    import json

    meta = json.loads(catalog.build_row(1, FILE, TABS)["metadata"])

    assert meta["spreadsheet_id"] == "1AbC"
    assert [tab["gid"] for tab in meta["tabs"]] == [0, 77]
    assert meta["tabs"][0]["columns"] == ["타임스탬프", "트랙", "출석"]


def test_정제를_돌리지_않는다():
    # raw_text 가 이미 머리행이라 LLM 이 더 붙일 것이 없다.
    assert catalog.build_row(1, FILE, TABS)["distill_state"] == "skipped"


def test_링크가_없으면_만들어_준다():
    row = catalog.build_row(1, {**FILE, "webViewLink": None}, TABS)

    assert row["url"].endswith("/1AbC/edit")
