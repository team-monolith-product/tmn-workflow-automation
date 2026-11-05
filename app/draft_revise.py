"""문자·메일 초안 [수정] 창이 함께 쓰는 "적은 글은 무엇인가" 선택입니다.

수정 창에는 고칠 점("더 짧게")을 적기도, 보낼 문장 자체를 적기도 합니다. 에이전트가
글만 보고 둘을 가리면 "좀 더 정중하게, 회의 언제 끝나요?" 처럼 섞인 글을 잘못
읽습니다. 그래서 적은 사람이 고르게 합니다.
"""

MODE_BLOCK = "draft_revise_mode"
MODE_INPUT = "draft_revise_mode_input"
FIX = "fix"
VERBATIM = "verbatim"

_OPTIONS = {
    FIX: "고칠 점을 적었어요",
    VERBATIM: "보낼 문장을 적었어요 — 이 글 그대로 보내 주세요",
}


def _option(value: str) -> dict:
    return {"text": {"type": "plain_text", "text": _OPTIONS[value]}, "value": value}


def mode_block() -> dict:
    """수정 창에 넣을 선택 블록을 만듭니다. 기본은 "고칠 점" 입니다.

    Returns:
        dict: 슬랙 input 블록(radio_buttons)
    """
    return {
        "type": "input",
        "block_id": MODE_BLOCK,
        "label": {"type": "plain_text", "text": "위에 적은 글은"},
        "element": {
            "type": "radio_buttons",
            "action_id": MODE_INPUT,
            "options": [_option(FIX), _option(VERBATIM)],
            "initial_option": _option(FIX),
        },
    }


def is_verbatim(values: dict) -> bool:
    """제출된 수정 창에서 "이 글 그대로" 를 골랐으면 True 입니다.

    Args:
        values: view_submission 의 body["view"]["state"]["values"]

    Returns:
        bool: 보낼 문장을 적었다고 골랐으면 True
    """
    return values[MODE_BLOCK][MODE_INPUT]["selected_option"]["value"] == VERBATIM
