from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from api import argocd


def test_application_list_uses_rotating_service_account_credentials_and_tls(
    monkeypatch,
):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.100.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    read = Mock(side_effect=["first-token\n", "rotated-token\n"])
    monkeypatch.setattr(Path, "read_text", read)
    response = Mock()
    response.json.return_value = {"items": [{"metadata": {"name": "service"}}]}
    get = Mock(return_value=response)
    monkeypatch.setattr(argocd.requests, "get", get)

    for token in ["first-token", "rotated-token"]:
        assert argocd.get_applications("argocd-global") == response.json.return_value
        get.assert_called_with(
            "https://10.100.0.1:443/apis/argoproj.io/v1alpha1/namespaces/argocd-global/applications",
            headers={"Authorization": f"Bearer {token}"},
            verify=str(Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")),
            timeout=(3, 15),
        )


def test_application_list_permission_failure_propagates(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.100.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    monkeypatch.setattr(Path, "read_text", Mock(return_value="test-token"))
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("403 Forbidden")
    monkeypatch.setattr(argocd.requests, "get", Mock(return_value=response))

    with pytest.raises(requests.HTTPError, match="403"):
        argocd.get_applications("argocd-global")
    response.json.assert_not_called()
