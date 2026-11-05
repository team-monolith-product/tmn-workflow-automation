import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


def get_applications(namespace: str) -> dict[str, Any]:
    credentials = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ["KUBERNETES_SERVICE_PORT_HTTPS"]
    response = requests.get(
        f"https://{host}:{port}/apis/argoproj.io/v1alpha1/namespaces/{quote(namespace, safe='')}/applications",
        headers={
            "Authorization": f"Bearer {(credentials / 'token').read_text().strip()}"
        },
        verify=str(credentials / "ca.crt"),
        timeout=(3, 15),
    )
    response.raise_for_status()
    return response.json()
