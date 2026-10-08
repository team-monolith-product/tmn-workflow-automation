import asyncio
import hashlib
import hmac
import json
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from service.deploy import deploy_request, execute

router = APIRouter()


@router.post("/github/deploy")
async def receive_deploy(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str = Header(""),
    x_hub_signature_256: str = Header(""),
) -> dict[str, str]:
    secret = os.environ.get("DEPLOY_WEBHOOK_SECRET")
    if not all(
        (
            secret,
            os.environ.get("DEPLOY_APP_CLIENT_ID"),
            os.environ.get("DEPLOY_APP_PRIVATE_KEY"),
        )
    ):
        raise HTTPException(503, "GitHub App이 설정되지 않았습니다.")
    body = await request.body()
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(401, "GitHub webhook 서명이 올바르지 않습니다.")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("객체가 필요합니다.")
        inputs = deploy_request(x_github_event, payload)
        installation_id = payload["installation"]["id"] if inputs else None
        if inputs and (type(installation_id) is not int or installation_id <= 0):
            raise ValueError("설치 번호가 올바르지 않습니다.")
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise HTTPException(400, "GitHub webhook 형식이 올바르지 않습니다.") from error
    if inputs is not None:
        background_tasks.add_task(asyncio.to_thread, execute, installation_id, inputs)
        return {"status": "accepted"}
    return {"status": "ok"}
