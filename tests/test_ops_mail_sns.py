"""ops@ 메일 SNS 서명 검증과 웹훅 테스트"""

import base64
import datetime
import json

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import ops_mail_sns
from service.ops_mail import sns

TOPIC = "arn:aws:sns:ap-northeast-2:000000000000:tmn-topic-ops-mail"


def _signed_message(key, extra: dict | None = None) -> dict:
    message = {
        "Type": "Notification",
        "MessageId": "id-1",
        "TopicArn": TOPIC,
        "Message": "{}",
        "Timestamp": "2026-11-03T00:00:00.000Z",
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.ap-northeast-2.amazonaws.com/cert.pem",
    }
    signature = key.sign(
        sns.string_to_sign(message), padding.PKCS1v15(), hashes.SHA256()
    )
    message["Signature"] = base64.b64encode(signature).decode()
    message.update(extra or {})
    return message


def _cert(key) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def test_SNS_서명이_맞으면_통과하고_본문이_바뀌면_막는다():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # When : 서명한 뒤 Message 를 바꾸면
    signed = _signed_message(key)
    tampered = _signed_message(key, {"Message": '{"x":1}'})

    # Then : 원본은 통과하고 바뀐 것은 막힌다
    assert sns.verify(signed, _cert(key))
    assert not sns.verify(tampered, _cert(key))


def test_인증서_URL이_AWS가_아니면_막는다():
    good = "https://sns.ap-northeast-2.amazonaws.com/SimpleNotificationService-a.pem"

    # When / Then : sns.<region>.amazonaws.com 의 https .pem 만 받고, 받아올 URL 은
    #               검사한 호스트와 경로로만 다시 만든다
    assert sns.aws_cert_url(good, TOPIC) == good
    for bad in (
        "https://sns.ap-northeast-2.amazonaws.com.evil.kr/a.pem",
        "http://sns.ap-northeast-2.amazonaws.com/a.pem",
        # urlparse 는 AWS 로, requests 는 evil.kr 로 읽는다
        "https://evil.kr\\@sns.ap-northeast-2.amazonaws.com/a.pem",
        "https://u@sns.ap-northeast-2.amazonaws.com/a.pem",
        "https://sns.ap-northeast-2.amazonaws.com:8443/a.pem",
        # 남의 S3 버킷 "sns" 의 주소들
        "https://sns.s3.amazonaws.com/a.pem",
        "https://sns.s3-us-west-2.amazonaws.com/a.pem",
        # 토픽과 다른 리전
        "https://sns.us-east-1.amazonaws.com/a.pem",
    ):
        assert sns.aws_cert_url(bad, TOPIC) is None, bad


def test_SNS_서명_대상은_AWS_문서의_모양이다():
    message = {
        "Type": "Notification",
        "MessageId": "id-1",
        "TopicArn": TOPIC,
        "Message": "{}",
        "Timestamp": "2026-11-03T00:00:00.000Z",
    }

    # When : Subject 없는 알림의 서명 대상을 만들면
    signed = sns.string_to_sign(message)

    # Then : 정해진 키 순서로 "키\n값\n" 을 잇고, 없는 Subject 는 키째 빠진다
    #        (https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html)
    assert (
        signed
        == (
            "Message\n{}\nMessageId\nid-1\n"
            "Timestamp\n2026-11-03T00:00:00.000Z\n"
            f"TopicArn\n{TOPIC}\nType\nNotification\n"
        ).encode()
    )


def test_SNS_검증은_검사한_URL_에서만_인증서를_받는다(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fetched = []

    class _Response:
        content = _cert(key)

        def raise_for_status(self):
            pass

    def get(url, timeout, allow_redirects):
        # 다른 곳으로 넘겨 주면 따라가지 않는다
        assert allow_redirects is False
        fetched.append(url)
        return _Response()

    monkeypatch.setattr(sns.requests, "get", get)
    sns._fetch_cert.cache_clear()
    cert_url = "https://sns.ap-northeast-2.amazonaws.com/a.pem"

    # When : 서명이 맞는 메시지와, 인증서 URL 만 공격자 쪽으로 바꾼 메시지를 확인하면
    good = sns.is_authentic(_signed_message(key, {"SigningCertURL": cert_url}))
    forged = sns.is_authentic(
        _signed_message(
            key,
            {
                "SigningCertURL": "https://evil.kr\\@sns.ap-northeast-2.amazonaws.com/a.pem"
            },
        )
    )

    # Then : 맞는 것만 통과하고, 인증서는 AWS 호스트에서만 받는다
    assert good and not forged
    assert fetched == [cert_url]


def _client(monkeypatch, authentic: bool = True):
    monkeypatch.setenv("OPS_MAIL_SNS_TOPIC_ARN", TOPIC)
    monkeypatch.setattr(sns, "is_authentic", lambda message: authentic)
    app = FastAPI()
    app.include_router(ops_mail_sns.router)
    return TestClient(app)


def test_다른_토픽이나_서명이_틀린_요청은_막는다(monkeypatch):
    # When : 다른 토픽에서 왔거나 서명이 틀리면
    other_topic = _client(monkeypatch).post(
        "/ops-mail/sns",
        content=json.dumps({"TopicArn": "arn:other", "Type": "Notification"}),
    )
    bad_signature = _client(monkeypatch, authentic=False).post(
        "/ops-mail/sns", content=json.dumps({"TopicArn": TOPIC, "Type": "Notification"})
    )

    # Then : 각각 403, 401 로 막는다
    assert other_topic.status_code == 403
    assert bad_signature.status_code == 401


def test_수신_알림은_응답_뒤에_처리한다(monkeypatch):
    handled = []
    monkeypatch.setattr(
        ops_mail_sns, "process_object", lambda bucket, key: handled.append(key)
    )
    notification = {
        "notificationType": "Received",
        "receipt": {"action": {"bucketName": "bucket", "objectKey": "ops/abc"}},
    }

    # When : SES 수신 알림이 오면
    response = _client(monkeypatch).post(
        "/ops-mail/sns",
        content=json.dumps(
            {
                "TopicArn": TOPIC,
                "Type": "Notification",
                "Message": json.dumps(notification),
            }
        ),
    )

    # Then : 받았다고 답하고 그 원본을 처리한다
    assert response.json() == {"status": "accepted"}
    assert handled == ["ops/abc"]
