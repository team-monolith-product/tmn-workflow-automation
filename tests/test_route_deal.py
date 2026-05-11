from app.route_deal import (
    get_fillable_properties,
    build_schema_description,
    format_sample_rows,
    build_ai_json_schema,
    build_notion_properties,
)

SAMPLE_DATA_SOURCE = {
    "properties": {
        "Name": {"type": "title", "title": {}},
        "견적금액": {"type": "number", "number": {"format": "won"}},
        "단계": {
            "type": "status",
            "status": {
                "options": [
                    {"name": "홈페이지견적요청"},
                    {"name": "전화상담"},
                    {"name": "정산 완료"},
                ]
            },
        },
        "모델": {
            "type": "multi_select",
            "multi_select": {
                "options": [
                    {"name": "Pro"},
                    {"name": "AI"},
                    {"name": "해커톤"},
                ]
            },
        },
        "인원": {"type": "number", "number": {"format": "number"}},
        "사용학기": {
            "type": "select",
            "select": {
                "options": [
                    {"name": "2026-1학기"},
                    {"name": "2026-2학기"},
                ]
            },
        },
        # 제외 대상: non-fillable types
        "담당자": {"type": "people", "people": {}},
        "견적서": {"type": "files", "files": {}},
        "프로모션": {"type": "formula", "formula": {}},
        "학교급": {"type": "rollup", "rollup": {}},
        "🏫 학교": {"type": "relation", "relation": {}},
        # 제외 대상: deprecated
        "담당-추후제거": {"type": "rich_text", "rich_text": {}},
        "코들계정-추후제거": {"type": "email", "email": {}},
    }
}

SAMPLE_ROWS = [
    {
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "군산중학교 - 김철수"}],
            },
            "견적금액": {"type": "number", "number": 100000},
            "단계": {"type": "status", "status": {"name": "정산 완료"}},
            "모델": {
                "type": "multi_select",
                "multi_select": [{"name": "Pro"}],
            },
            "인원": {"type": "number", "number": 30},
            "사용학기": {"type": "select", "select": {"name": "2026-1학기"}},
        }
    },
    {
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "서울고등학교 - 박영희"}],
            },
            "견적금액": {"type": "number", "number": 500000},
            "단계": {"type": "status", "status": {"name": "전화상담"}},
            "모델": {
                "type": "multi_select",
                "multi_select": [{"name": "AI"}, {"name": "Pro"}],
            },
            "인원": {"type": "number", "number": 60},
            "사용학기": {"type": "select", "select": None},
        }
    },
]


def test_get_fillable_properties():
    result = get_fillable_properties(SAMPLE_DATA_SOURCE)
    # Should include fillable types
    assert "Name" in result
    assert "견적금액" in result
    assert "단계" in result
    assert "모델" in result
    assert "인원" in result
    assert "사용학기" in result
    # Should exclude non-fillable types
    assert "담당자" not in result  # people
    assert "견적서" not in result  # files
    assert "프로모션" not in result  # formula
    assert "학교급" not in result  # rollup
    assert "🏫 학교" not in result  # relation
    # Should exclude deprecated
    assert "담당-추후제거" not in result
    assert "코들계정-추후제거" not in result


def test_build_schema_description():
    props = get_fillable_properties(SAMPLE_DATA_SOURCE)
    desc = build_schema_description(props)
    assert "Name" in desc
    assert "견적금액" in desc
    assert "Pro" in desc  # multi_select option
    assert "홈페이지견적요청" in desc  # status option
    assert "2026-1학기" in desc  # select option
    assert "won" in desc  # number format


def test_format_sample_rows():
    props = get_fillable_properties(SAMPLE_DATA_SOURCE)
    text = format_sample_rows(SAMPLE_ROWS, props)
    assert "군산중학교 - 김철수" in text
    assert "서울고등학교 - 박영희" in text
    assert "100000" in text
    assert "Pro" in text
    assert "예시 1" in text
    assert "예시 2" in text


def test_format_sample_rows_empty():
    props = get_fillable_properties(SAMPLE_DATA_SOURCE)
    text = format_sample_rows([], props)
    assert text == ""


def test_build_ai_json_schema():
    props = get_fillable_properties(SAMPLE_DATA_SOURCE)
    schema = build_ai_json_schema(props)
    assert schema["type"] == "object"
    assert "Name" in schema["properties"]
    assert "견적금액" in schema["properties"]
    assert "단계" in schema["properties"]
    assert "모델" in schema["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())

    # title: nullable string
    assert schema["properties"]["Name"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]
    # number: nullable number
    assert schema["properties"]["견적금액"]["anyOf"] == [
        {"type": "number"},
        {"type": "null"},
    ]
    # status: nullable enum
    status_schema = schema["properties"]["단계"]["anyOf"]
    assert {"type": "null"} in status_schema
    enum_option = next(s for s in status_schema if s.get("type") == "string")
    assert "홈페이지견적요청" in enum_option["enum"]

    # multi_select: array of enums
    model_schema = schema["properties"]["모델"]
    assert model_schema["type"] == "array"
    assert "Pro" in model_schema["items"]["enum"]

    # select: nullable enum
    semester_schema = schema["properties"]["사용학기"]["anyOf"]
    enum_option = next(s for s in semester_schema if s.get("type") == "string")
    assert "2026-1학기" in enum_option["enum"]


def test_build_notion_properties():
    ai_result = {
        "Name": "테스트학교 - 홍길동",
        "견적금액": 300000,
        "단계": "홈페이지견적요청",
        "모델": ["Pro", "AI"],
        "인원": 30,
        "사용학기": None,
    }
    props = get_fillable_properties(SAMPLE_DATA_SOURCE)
    result = build_notion_properties(ai_result, props, "bot-user-id")

    assert result["Name"]["title"][0]["text"]["content"] == "테스트학교 - 홍길동"
    assert result["견적금액"]["number"] == 300000
    assert result["단계"]["status"]["name"] == "홈페이지견적요청"
    assert len(result["모델"]["multi_select"]) == 2
    assert result["모델"]["multi_select"][0]["name"] == "Pro"
    assert result["모델"]["multi_select"][1]["name"] == "AI"
    assert result["인원"]["number"] == 30
    assert "사용학기" not in result  # None should be skipped
    assert result["담당자"]["people"][0]["id"] == "bot-user-id"


def test_build_notion_properties_all_null():
    ai_result = {
        "Name": None,
        "견적금액": None,
        "단계": None,
        "모델": [],
        "인원": None,
        "사용학기": None,
    }
    props = get_fillable_properties(SAMPLE_DATA_SOURCE)
    result = build_notion_properties(ai_result, props, "bot-user-id")

    # Only 담당자 should be set (always set to bot user)
    assert "담당자" in result
    assert "Name" not in result
    assert "견적금액" not in result
    assert "단계" not in result
    assert "모델" not in result
    assert "인원" not in result


def test_build_notion_properties_partial():
    ai_result = {
        "Name": "서울중학교 - 김영수",
        "견적금액": None,
        "단계": "홈페이지견적요청",
        "모델": ["AI"],
        "인원": 15,
        "사용학기": "2026-1학기",
    }
    props = get_fillable_properties(SAMPLE_DATA_SOURCE)
    result = build_notion_properties(ai_result, props, "bot-user-id")

    assert "Name" in result
    assert "견적금액" not in result  # None → skipped
    assert result["단계"]["status"]["name"] == "홈페이지견적요청"
    assert result["모델"]["multi_select"] == [{"name": "AI"}]
    assert result["인원"]["number"] == 15
    assert result["사용학기"]["select"]["name"] == "2026-1학기"
    assert result["담당자"]["people"][0]["id"] == "bot-user-id"
