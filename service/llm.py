"""봇들이 공유하는 LLM 기본 모델.

봇마다 모델 이름을 박아두면 올릴 때마다 파일을 전부 훑어야 하므로 한 곳에 둡니다.
추론 강도는 작업마다 다르므로 각 호출부에 남깁니다.
"""

DEFAULT_MODEL = "gpt-5.6-terra"

# 도구를 붙인 ChatOpenAI는 output_version에 이 값을 줘야 합니다. 주지 않으면
# chat/completions로 나가는데, 그 경로는 추론 모델에 함수 도구를 붙이는 것을
# 400으로 막습니다. 도구 없이 부르는 곳에는 필요 없습니다.
RESPONSES_OUTPUT_VERSION = "responses/v1"
