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

# 인증서 URL 이 AWS 가 아니면 공격자가 자기 인증서로 서명한 것이다.
CERT_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")

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


def is_aws_cert_url(url: str) -> bool:
    """인증서 URL 이 AWS SNS 것인지 봅니다.

    Args:
        url: SigningCertURL

    Returns:
        bool: https 이고 sns.<region>.amazonaws.com 의 .pem 이면 True
    """
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and CERT_HOST.match(parsed.hostname or "") is not None
        and parsed.path.endswith(".pem")
    )


def _fetch_cert(url: str) -> bytes:
    """인증서를 받아옵니다. 같은 인증서를 메시지마다 다시 받지 않습니다."""
    if url not in _certs:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        _certs[url] = response.content
    return _certs[url]


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
    url = message["SigningCertURL"]
    if not is_aws_cert_url(url):
        return False
    return verify(message, _fetch_cert(url))
