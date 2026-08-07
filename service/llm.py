"""봇들이 공유하는 LLM 기본 모델.

봇마다 모델 이름을 박아두면 올릴 때마다 파일을 전부 훑어야 하므로 한 곳에 둡니다.
추론 강도는 작업마다 다르므로 각 호출부에 남깁니다.
"""

DEFAULT_MODEL = "gpt-5.6-terra"

# 도구를 붙인 ChatOpenAI는 output_version에 이 값을 줘야 합니다. 주지 않으면
# chat/completions로 나가는데, 그 경로는 추론 모델에 함수 도구를 붙이는 것을
# 400으로 막습니다. 도구 없이 부르는 곳에는 필요 없습니다.
RESPONSES_OUTPUT_VERSION = "responses/v1"


def extract_text(content: str | list) -> str:
    """AIMessage.content에서 사람이 읽을 본문만 꺼냅니다.

    RESPONSES_OUTPUT_VERSION 을 준 모델은 content 를 블록 리스트로 돌려줍니다.
    reasoning 블록이 섞여 있어서 그대로 슬랙에 보내면 invalid_blocks 로 거부됩니다.

    Args:
        content: AIMessage.content

    Returns:
        str: type이 text인 블록을 이어붙인 문자열
    """
    if isinstance(content, str):
        return content
    return "".join(block["text"] for block in content if block.get("type") == "text")
