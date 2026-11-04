"""
AWS SES API 래퍼 함수들

SES v2 API 를 씁니다. v1 SendRawEmail 은 한 통에 10MB 까지라 첨부가 든
메일을 보내기에 작고, v2 는 40MB 까지 받습니다. IAM 권한은 ses:SendEmail 입니다.
"""

import os
from functools import cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# SES 가 메일을 받지 않았다고 확실히 답하는 오류. 이때는 메일이 나가지 않았다.
# 타임아웃·5xx 는 여기 없다 -- SES 가 이미 받았을 수 있다.
REJECTED_CODES = frozenset(
    {
        "AccountSuspendedException",
        "BadRequestException",
        "LimitExceededException",
        "MailFromDomainNotVerifiedException",
        "MessageRejected",
        "NotFoundException",
        "SendingPausedException",
        "TooManyRequestsException",
    }
)


def send_mime(raw: bytes) -> dict:
    """
    MIME 원문 그대로 메일을 보냅니다. 받는 사람은 원문의 To 헤더에서 읽습니다.

    Args:
        raw: MIME 원문

    Returns:
        dict: SES 응답 (MessageId 포함)
    """
    return _client().send_email(Content={"Raw": {"Data": raw}})


@cache
def _client():
    """SES 클라이언트를 한 번만 만듭니다. 수백 명에게 보낼 때 통마다 만들지 않습니다.

    다시 보내지 않는다. 기본 설정은 타임아웃·5xx 에 같은 원문을 최대 5번 보내는데,
    SES 가 이미 받았는데 응답만 끊긴 것이면 같은 사람이 여러 번 받는다.
    boto3 기본 세션은 여러 스레드가 동시에 클라이언트를 만들면 깨질 수 있어 세션을
    따로 만든다. 만든 클라이언트는 여러 스레드가 함께 써도 된다.
    """
    return boto3.session.Session().client(
        "sesv2",
        region_name=os.environ.get("AWS_REGION", "ap-northeast-2"),
        config=Config(retries={"total_max_attempts": 1}),
    )


def rejected_code(error: BaseException) -> str | None:
    """SES 가 받지 않았다고 확실히 답한 오류면 그 코드를 돌려줍니다.

    Args:
        error: send_mime 이 던진 오류

    Returns:
        str | None: 오류 코드. 나갔는지 모르는 오류면 None
    """
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code")
        if code in REJECTED_CODES:
            return code
    return None
