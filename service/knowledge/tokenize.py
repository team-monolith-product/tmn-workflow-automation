"""
IDF 계산용 토큰화 모듈입니다.

Postgres 쪽 어휘 검색은 pg_bigm이 2글자씩 잘라 처리하지만, IDF는 파이썬에서
계산합니다. 그래서 여기는 RDS 확장 제약을 받지 않습니다.

형태소 분석기를 쓰지 않는 이유는 슬랙봇 파드가 400Mi이고 슬랙 앱 넷과
프로세스를 공유하기 때문입니다. IDF의 용도가 "희귀한 토큰이 들어 있는가"
판정이고, 실제로 변별력을 주는 토큰은 대부분 식별자·에러코드·고유명사라
얕은 정규화로 충분합니다.

tokenizer 이름을 token_df에 함께 저장합니다. 규칙을 바꾸면 기존 df가 전부
무의미해지는데, 이름을 안 남기면 알아챌 방법이 없습니다.
"""

import re

TOKENIZER = "simple-v1"

# 식별자는 통째로 살린다. karafka-rdkafka, CVE-2026-40295, sg-09ddf59899f05a71a
# 같은 것들이 IDF에서 가장 변별력이 크다.
_ASCII = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_HANGUL = re.compile(r"[가-힣]+")

# 빈도 높은 조사와 복수 접미사만 길이 내림차순으로 둔다. 목록을 늘릴수록
# 실제 단어를 깎을 위험이 커진다.
_SUFFIXES = (
    "으로부터",
    "에게서",
    "께서는",
    "으로써",
    "으로서",
    "에서는",
    "에게는",
    "이라는",
    "께서",
    "라는",
    "에서",
    "에게",
    "부터",
    "까지",
    "으로",
    "처럼",
    "보다",
    "마다",
    "조차",
    "밖에",
    "이나",
    "이란",
    "이라",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "의",
    "도",
    "로",
    "와",
    "과",
    "만",
    "나",
    "라",
    "께",
    # 복수. "선생님들께" 처럼 조사를 뗀 뒤 한 번 더 남는다.
    "들",
)

# "코드로는"은 "는"을 떼면 "코드로"가 되고 한 번 더 떼야 "코드"가 된다.
# 반복 횟수를 막아두지 않으면 "바나나"가 "바"까지 깎인다.
_MAX_STRIP = 3

# 조사를 뗀 뒤 이보다 짧아지면 떼지 않는다. "의도"에서 "도"를 떼면 "의"가
# 남는 식의 파괴를 막는다.
_MIN_STEM = 2


def strip_josa(word: str) -> str:
    """한글 어절 끝의 조사와 복수 접미사를 뗍니다.

    Args:
        word: 한글로만 이루어진 어절

    Returns:
        str: 접미사를 뗀 어간. 뗄 수 없으면 원래 어절
    """
    for _ in range(_MAX_STRIP):
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
                word = word[: -len(suffix)]
                break
        else:
            break
    return word


def tokenize(text: str) -> set[str]:
    """문서 하나에서 IDF용 토큰 집합을 뽑습니다.

    문서빈도를 세는 용도이므로 같은 토큰이 여러 번 나와도 하나로 칩니다.

    Args:
        text: 원문 또는 정제문

    Returns:
        set[str]: 소문자로 정규화된 토큰 집합
    """
    tokens = {match.group().lower() for match in _ASCII.finditer(text)}
    tokens |= {strip_josa(match.group()) for match in _HANGUL.finditer(text)}
    return {token for token in tokens if len(token) >= _MIN_STEM}
