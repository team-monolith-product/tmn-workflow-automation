import os
from typing import Any

import requests
from github import Auth


def request(method: str, path: str, token: str, **kwargs: Any) -> requests.Response:
    response = requests.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        timeout=(2, 4),
        **kwargs,
    )
    response.raise_for_status()
    return response


def get_app_token() -> str:
    return Auth.AppAuth(
        os.environ["DEPLOY_APP_CLIENT_ID"],
        os.environ["DEPLOY_APP_PRIVATE_KEY"],
    ).token


def get_installation_token(
    installation_id: int, repositories: list[str], permissions: dict[str, str]
) -> str:
    return request(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        get_app_token(),
        json={"repositories": repositories, "permissions": permissions},
    ).json()["token"]


def get_pages(path: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        response = request("GET", path, token, params={"per_page": 100, "page": page})
        items.extend(response.json())
        if "next" not in response.links:
            return items
        page += 1
