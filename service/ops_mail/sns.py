"""
SNS 가 보낸 HTTP 요청이 진짜인지 확인합니다.

웹훅 주소는 공개되어 있어서 누구나 SNS 모양의 본문을 보낼 수 있습니다.
SNS 는 메시지마다 AWS 인증서로 서명하므로, 인증서를 받아 서명을 검증합니다.
https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
"""

import base64
import re
from typing import Any
from urllib.parse import urlparse

import requests
from cachetools import TTLCache
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

# 서명 대상에 들어가는 키와 순서. 메시지 종류마다 다르다.
NOTIFICATION_KEYS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
SUBSCRIPTION_KEYS = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)

# 토픽 ARN 의 리전 자리. 인증서 호스트는 이 리전의 sns.<region>.amazonaws.com 이어야 한다.
# "sns.<무엇이든>.amazonaws.com" 으로 열어 두면 sns.s3-us-west-2.amazonaws.com 처럼
# 남이 가진 S3 버킷 "sns" 의 주소가 통과한다.
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

_certs: TTLCache = TTLCache(maxsize=10, ttl=3600)


def string_to_sign(message: dict[str, Any]) -> bytes:
    """서명 대상 문자열을 만듭니다.

    Args:
        message: SNS 가 보낸 JSON

    Returns:
        bytes: "키\\n값\\n" 을 정해진 순서로 이어붙인 바이트
    """
    keys = NOTIFICATION_KEYS if message["Type"] == "Notification" else SUBSCRIPTION_KEYS
    # Subject 는 알림에 없을 수 있고, 없으면 키째 빠진다.
    return "".join(
        f"{key}\n{message[key]}\n" for key in keys if key in message
    ).encode()


def aws_cert_url(url: str, topic_arn: str) -> str | None:
    """인증서 URL 이 이 토픽 리전의 AWS SNS 것이면, 받아올 URL 을 검사한 부분만으로
    다시 만듭니다.

    받은 문자열을 그대로 requests 에 넘기면 안 된다. "https://공격자\\@sns.<리전>
    .amazonaws.com/x.pem" 을 urlparse 는 AWS 호스트로, requests 는 공격자 호스트로
    읽는다. SigningCertURL 은 서명 대상이 아니라서 서명 뒤에 바꿀 수 있다.

    Args:
        url: SigningCertURL
        topic_arn: 서명된 TopicArn. arn:aws:sns:<region>:<account>:<name>

    Returns:
        str | None: https://sns.<region>.amazonaws.com/....pem. 아니면 None
    """
    parts = topic_arn.split(":")
    region = parts[3] if len(parts) == 6 else ""
    parsed = urlparse(url)
    if (
        REGION.match(region) is None
        or parsed.scheme != "https"
        or parsed.username is not None
        or parsed.port is not None
        or parsed.hostname != f"sns.{region}.amazonaws.com"
        or not parsed.path.endswith(".pem")
        or "\\" in url
    ):
        return None
    return f"https://{parsed.hostname}{parsed.path}"


def _fetch_cert(url: str) -> bytes:
    """인증서를 받아옵니다. 같은 인증서를 메시지마다 다시 받지 않습니다."""
    # 여러 작업 스레드에서 부른다. 검사와 읽기 사이에 만료돼도 KeyError 가 안 나게 get.
    cert = _certs.get(url)
    if cert is None:
        # 다른 곳으로 넘겨 주면 따라가지 않는다. 검사한 호스트에서만 받는다.
        response = requests.get(url, timeout=10, allow_redirects=False)
        response.raise_for_status()
        cert = _certs[url] = response.content
    return cert


def verify(message: dict[str, Any], cert_pem: bytes) -> bool:
    """서명을 검증합니다.

    Args:
        message: SNS 가 보낸 JSON
        cert_pem: SigningCertURL 에서 받은 인증서

    Returns:
        bool: 서명이 맞으면 True
    """
    algorithm = hashes.SHA256() if message["SignatureVersion"] == "2" else hashes.SHA1()
    public_key = x509.load_pem_x509_certificate(cert_pem).public_key()
    try:
        public_key.verify(
            base64.b64decode(message["Signature"]),
            string_to_sign(message),
            padding.PKCS1v15(),
            algorithm,
        )
    except InvalidSignature:
        return False
    return True


def is_authentic(message: dict[str, Any]) -> bool:
    """인증서 URL 과 서명을 모두 확인합니다. 네트워크를 타므로 스레드에서 부릅니다.

    Args:
        message: SNS 가 보낸 JSON

    Returns:
        bool: AWS SNS 가 보낸 것이 맞으면 True
    """
    url = aws_cert_url(message["SigningCertURL"], message["TopicArn"])
    if url is None:
        return False
    return verify(message, _fetch_cert(url))
