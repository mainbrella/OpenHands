"""Provider lifecycle tests using the real Mainbrella HTTP client and SDK."""

import json
import os
import subprocess
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import mainbrella_workspace
import pytest
from mainbrella import Mainbrella, MainbrellaError
from mainbrella_workspace import MainbrellaWorkspace, RuntimeHandle
from openhands.sdk.workspace import RemoteWorkspace

KEY = "mb_" + "a" * 64
GENERATION = "2026-10-08T09:25:17.059Z"
HOST = "https://" + "b" * 48 + ".mainbrella.dev"
PREVIEW_ID = "c" * 32


class API:
    def __init__(self):
        self.calls = []
        self.generation = GENERATION
        self.previews = True
        self.kill_fails = False

    def transport(self, url, method, headers, body, timeout):
        assert headers["Authorization"] == "Bearer " + KEY
        self.calls.append((method, url, body))
        if url.endswith("/capabilities"):
            data = {
                "previews": {"supported": self.previews},
                "execution": {"background": True},
                "files": {"write": True},
            }
        elif method == "POST" and url.endswith("/containers"):
            data = {
                "creation": {
                    "id": "creation-1",
                    "status": "running",
                    "containerId": "small",
                    "createdAt": GENERATION,
                },
                "containers": [
                    {"id": "small", "createdAt": GENERATION, "status": "running"}
                ],
            }
        elif method == "GET" and url.endswith("/containers"):
            data = {
                "containers": [
                    {"id": "small", "createdAt": self.generation, "status": "running"}
                ],
                "imageCatalog": [{"id": "python"}],
            }
        elif method == "DELETE":
            if self.kill_fails:
                return 503, b'{"error":"runtime_unavailable"}'
            assert "createdAt=2026-10-08T09%3A25%3A17.059Z" in url
            data = {"containers": []}
        elif "/containers/previews" in url:
            data = {"id": PREVIEW_ID, "url": HOST + "/", "expiresAt": 1791460000000}
        else:
            raise AssertionError(f"Unexpected provider call: {method} {url}")
        return 200, json.dumps(data).encode()

    def client(self):
        return Mainbrella(KEY, transport=self.transport)


def handle():
    return RuntimeHandle(
        "small", GENERATION, HOST, "runtime-secret", PREVIEW_ID, 1791460000000
    )


def healthy(workspace, timeout):
    workspace._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"version": "1.53.0"}, request=request
            )
        ),
        base_url=workspace.host,
    )


def test_create_is_a_remote_workspace_and_cleans_only_its_generation():
    api = API()
    states = []
    with (
        patch("mainbrella_workspace.bootstrap"),
        patch("mainbrella_workspace.wait_until_healthy", healthy),
    ):
        with MainbrellaWorkspace.create(
            api.client(), creation_key="stable-key", record=states.append
        ) as workspace:
            assert isinstance(workspace, RemoteWorkspace)
            assert workspace.get_server_info()["version"] == "1.53.0"
            assert workspace.handle.created_at == GENERATION
            assert KEY not in repr(workspace)
            assert workspace.handle.api_key not in repr(workspace)
            assert HOST not in workspace.model_dump_json()
    assert states[0]["creation_key"] == "stable-key"
    assert states[1]["id"] == "small"
    assert any(method == "DELETE" for method, _, _ in api.calls)


def test_attach_validates_generation_and_never_stops_borrowed_machine():
    api = API()
    with MainbrellaWorkspace.attach(api.client(), handle()) as workspace:
        assert workspace.handle.id == "small"
    assert all(method == "GET" for method, _, _ in api.calls)
    api.generation = "2026-10-08T10:25:17.059Z"
    with pytest.raises(MainbrellaError, match="stale_runtime_generation"):
        MainbrellaWorkspace.attach(api.client(), handle())


def test_missing_capabilities_fail_before_consuming_a_start():
    api = API()
    api.previews = False
    with pytest.raises(MainbrellaError, match="required_capabilities_unavailable"):
        MainbrellaWorkspace.create(api.client(), creation_key="stable-key")
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_bootstrap_records_identity_and_cleanup_outcome(cleanup_fails):
    api = API()
    api.kill_fails = cleanup_fails
    states = []
    with patch("mainbrella_workspace.bootstrap", side_effect=RuntimeError("secret")):
        with pytest.raises(MainbrellaError, match="runtime_bootstrap_failed"):
            MainbrellaWorkspace.create(
                api.client(), creation_key="stable-key", record=states.append
            )
    assert states[-1]["cleanup"] == ("failed" if cleanup_fails else "completed")
    assert states[-1]["created_at"] == GENERATION
    assert "secret" not in json.dumps(states)


def test_cleanup_failure_is_recorded_and_can_be_retried():
    api = API()
    states = []
    with (
        patch("mainbrella_workspace.bootstrap"),
        patch("mainbrella_workspace.wait_until_healthy", healthy),
    ):
        workspace = MainbrellaWorkspace.create(
            api.client(), creation_key="stable-key", record=states.append
        )
    api.kill_fails = True
    with pytest.raises(MainbrellaError):
        workspace.cleanup()
    assert states[-1]["cleanup"] == "failed"
    api.kill_fails = False
    workspace.cleanup()
    before = len(api.calls)
    workspace.cleanup()
    assert len(api.calls) == before
    assert states[-1]["cleanup"] == "completed"


def test_bootstrap_executes_inside_venv_without_account_credential(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(mainbrella_workspace, "ROOT", str(tmp_path))
    monkeypatch.setattr(mainbrella_workspace, "WORKING_DIR", str(tmp_path / "work"))
    venv_path = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_path)
    module = next((venv_path / "lib").glob("python*/site-packages")) / "openhands"
    module.mkdir()
    (module / "agent_server.py").write_text(
        "import json, os, sys\n"
        "with open(os.path.join(os.getcwd(), 'started.json'), 'w') as f:\n"
        "    json.dump({'prefix': sys.prefix, 'session': os.environ['OH_SESSION_API_KEYS_0'],"
        " 'account': os.environ.get('MAINBRELLA_API_KEY')}, f)\n"
    )

    def write(path, data):
        Path(path).write_bytes(data)

    def launch(command, timeout_ms):
        process = subprocess.run(
            [str(venv_path / "bin/python"), str(tmp_path / "start.py")],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            timeout=5,
        )
        return {"exitCode": process.returncode}

    install = SimpleNamespace(
        id="test-install", wait=lambda **kwargs: {"status": "succeeded", "exitCode": 0}
    )
    sandbox = SimpleNamespace(
        commands=SimpleNamespace(start=lambda *args, **kwargs: install, run=launch),
        files=SimpleNamespace(write=write),
    )
    mainbrella_workspace.bootstrap(sandbox, "runtime-only", lambda state: None)
    result = json.loads((tmp_path / "work/started.json").read_text())
    assert Path(result["prefix"]).resolve() == venv_path.resolve()
    assert result["session"] == "runtime-only"
    assert result["account"] is None
