"""시트 카탈로그 정규화 테스트 — 검색에 걸릴 것만 들어가야 한다."""

import json

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


def test_머리행만_들어간다():
    # 카탈로그에 넣는 것은 이름·탭·머리행뿐이다. 행이 늘어도 raw_text 는
    # 탭 수만큼만 길어진다 -- 셀 값이 섞이면 이 줄 수가 흔들린다.
    row = catalog.build_row(1, FILE, TABS)

    assert row["raw_text"].count("\n") == len(TABS)
    assert row["raw_text"].startswith(FILE["name"])


def test_수정_시각만_바뀌면_해시가_그대로다():
    # 셀만 고친 시트도 modifiedTime 이 갱신돼 후보로 올라온다. 그때 머리행이
    # 같으면 해시도 같아야, 무엇이 실제로 바뀌었는지 구분할 수 있다.
    before = catalog.build_row(1, FILE, TABS)
    after = catalog.build_row(
        1, {**FILE, "modifiedTime": "2026-08-21T11:00:00.000Z"}, TABS
    )

    assert before["content_hash"] == after["content_hash"]
    # 시각 자체는 따라가야 한다. 안 그러면 언제 손댄 시트인지 알 수 없다.
    assert after["source_updated_at"] != before["source_updated_at"]


def test_열이_늘면_해시가_바뀐다():
    before = catalog.build_row(1, FILE, TABS)
    grown = [{**TABS[0], "header": TABS[0]["header"] + ["공문"]}, TABS[1]]

    after = catalog.build_row(1, FILE, grown)

    assert before["content_hash"] != after["content_hash"]


def test_탭별_gid와_열을_남긴다():
    # 시트를 찾은 뒤 execute_python 에 넘길 값이다. 없으면 탭을 다시 물어봐야 한다.
    meta = json.loads(catalog.build_row(1, FILE, TABS)["metadata"])

    assert meta["spreadsheet_id"] == "1AbC"
    assert [tab["gid"] for tab in meta["tabs"]] == [0, 77]
    assert meta["tabs"][0]["columns"] == ["타임스탬프", "트랙", "출석"]


def test_정제_큐에_들어가지_않는다():
    # item_distill_q 부분 인덱스가 distill_state='pending' 만 담는다.
    # 카탈로그는 raw_text 가 이미 머리행이라 LLM 이 더 붙일 것이 없다.
    row = catalog.build_row(1, FILE, TABS)

    assert row["distill_state"] != "pending"
    assert row["distill_after"] is None


def test_링크가_없으면_만들어_준다():
    row = catalog.build_row(1, {**FILE, "webViewLink": None}, TABS)

    assert row["url"].endswith("/1AbC/edit")


def test_탭이_늘면_해시가_바뀐다():
    # 탭을 추가하는 것도 "이 시트에 무엇이 있나" 를 바꾼다.
    before = catalog.build_row(1, FILE, TABS)

    after = catalog.build_row(
        1, FILE, TABS + [{"id": 9, "title": "정산", "header": ["항목", "금액"]}]
    )

    assert before["content_hash"] != after["content_hash"]
    assert "정산" in after["raw_text"]
