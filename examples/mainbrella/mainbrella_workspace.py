"""External RemoteWorkspace prototype; no Canvas or Agent Server API changes."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import httpx
from mainbrella import Mainbrella, MainbrellaError, Sandbox
from openhands.sdk.workspace import RemoteWorkspace
from pydantic import Field, PrivateAttr

VERSION = "1.53.0"
PORT = 18000
ROOT = "/tmp/openhands-mainbrella"
WORKING_DIR = "/workspace"
Record = Callable[[dict], None]


@dataclass(frozen=True)
class RuntimeHandle:
    """Store this in a protected file; host and api_key are bearer credentials."""

    id: str
    created_at: str
    host: str = field(repr=False)
    api_key: str = field(repr=False)
    preview_id: str
    preview_expires_at: int


def bootstrap(sandbox: Sandbox, session_key: str, record: Record) -> None:
    """Install a pinned Agent Server and launch it detached, bounded by its lease."""
    install = sandbox.commands.start(
        f"python -m venv {ROOT}/venv && "
        f"{ROOT}/venv/bin/pip install --quiet --progress-bar off --disable-pip-version-check "
        f"openhands-agent-server=={VERSION} openhands-sdk=={VERSION} "
        f"openhands-tools=={VERSION}",
        timeout_ms=900_000,
    )
    record({"install_job_id": install.id})
    result = install.wait(timeout=910)
    if result.get("status") != "succeeded" or result.get("exitCode") != 0:
        raise MainbrellaError("agent_server_install_failed")
    # Only runtime credentials go into the guest. The account key stays in Mainbrella.
    env = {
        "OH_SESSION_API_KEYS_0": session_key,
        "OH_SECRET_KEY": secrets.token_hex(32),
        "OH_ENABLE_VSCODE": "false",
        "OH_PRELOAD_TOOLS": "false",
        "OH_PERSISTENCE_DIR": f"{ROOT}/state",
        "OH_CONVERSATIONS_PATH": f"{ROOT}/state/conversations",
        "OH_BASH_EVENTS_DIR": f"{ROOT}/state/bash_events",
        "OPENHANDS_SUPPRESS_BANNER": "1",
    }
    sandbox.files.write(f"{ROOT}/env.json", json.dumps(env).encode())
    script = f"""import json, os
with open('{ROOT}/env.json') as f:
    env = json.load(f)
os.environ.update(env)
os.makedirs('{WORKING_DIR}', exist_ok=True)
os.chdir('{WORKING_DIR}')
os.execv('{ROOT}/venv/bin/python', ['{ROOT}/venv/bin/python', '-m', 'openhands.agent_server',
                                 '--host', '0.0.0.0', '--port', '{PORT}'])
"""
    sandbox.files.write(f"{ROOT}/start.py", script.encode())
    result = sandbox.commands.run(
        f"setsid nohup {ROOT}/venv/bin/python {ROOT}/start.py "
        f">{ROOT}/server.log 2>&1 </dev/null &",
        timeout_ms=30_000,
    )
    if result.get("exitCode") != 0:
        raise MainbrellaError("agent_server_start_failed")


def wait_until_healthy(workspace: RemoteWorkspace, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = workspace.client.get("/server_info", timeout=5)
            if response.is_success:
                if response.json().get("version") != VERSION:
                    raise MainbrellaError("agent_server_version_mismatch")
                return
            if response.status_code in (401, 403):
                raise MainbrellaError("agent_server_authentication_failed")
        except (httpx.TransportError, ValueError):
            pass
        time.sleep(1)
    raise MainbrellaError("agent_server_not_ready")


class MainbrellaWorkspace(RemoteWorkspace):
    """One machine per workspace; borrowed handles are never stopped on exit.

    Provisioning is explicit rather than a Pydantic deserialization side effect.
    This intentionally remains outside the SDK's REST workspace union.
    """

    host: str = Field(exclude=True, repr=False)
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    _sandbox: Sandbox = PrivateAttr()
    _handle: RuntimeHandle = PrivateAttr()
    _owned: bool = PrivateAttr(default=False)
    _stopped: bool = PrivateAttr(default=False)
    _record: Record = PrivateAttr(default_factory=lambda: lambda state: None)

    @property
    def handle(self) -> RuntimeHandle:
        return self._handle

    @classmethod
    def create(
        cls,
        client: Mainbrella,
        *,
        creation_key: str,
        size: str = "small",
        preview_ttl: int = 3600,
        record: Record = lambda state: None,
    ) -> MainbrellaWorkspace:
        caps = client.capabilities()
        if not (
            caps.get("previews", {}).get("supported")
            and caps.get("execution", {}).get("background")
            and caps.get("files", {}).get("write")
        ):
            raise MainbrellaError("required_capabilities_unavailable")
        if "python" not in {
            entry["id"] for entry in client.list().get("imageCatalog", [])
        }:
            raise MainbrellaError("python_image_unavailable")
        if (
            size not in {"small", "medium", "large", "xl"}
            or not 60 <= preview_ttl <= 3600
        ):
            raise MainbrellaError("invalid_runtime_options")
        # Persist before POST so an ambiguous creation can be reconciled with this key.
        record({"creation_key": creation_key, "catalog_id": "python", "size": size})
        sandbox = client.create(
            catalog_id="python", size=size, idempotency_key=creation_key
        )
        identity = {
            "creation_key": creation_key,
            "id": sandbox.id,
            "created_at": sandbox.created_at,
        }
        workspace = None
        try:
            record(identity)
            session_key = secrets.token_hex(32)
            bootstrap(sandbox, session_key, record)
            preview = sandbox.previews.create(PORT, ttl_seconds=preview_ttl)
            handle = RuntimeHandle(
                sandbox.id,
                sandbox.created_at,
                preview["url"].rstrip("/"),
                session_key,
                preview["id"],
                preview["expiresAt"],
            )
            workspace = cls._from_handle(sandbox, handle)
            workspace._owned = True
            workspace._record = record
            record({**identity, **asdict(handle)})
            wait_until_healthy(workspace, 120)
            return workspace
        except Exception as error:
            if workspace is not None:
                workspace.reset_client()
            cleanup = "completed"
            try:
                sandbox.kill()
            except Exception:
                cleanup = "failed"
            failure_code = (
                error.code
                if isinstance(error, MainbrellaError)
                else type(error).__name__
            )
            record({**identity, "cleanup": cleanup, "failure_code": failure_code})
            # Never expose HTTP errors containing bearer preview URLs or guest output.
            raise MainbrellaError("runtime_bootstrap_failed") from None

    @classmethod
    def _from_handle(
        cls, sandbox: Sandbox, handle: RuntimeHandle
    ) -> MainbrellaWorkspace:
        parsed = urlsplit(handle.host)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or (
                parsed.scheme != "https"
                and not (
                    parsed.scheme == "http"
                    and (parsed.hostname or "").endswith(".localhost")
                )
            )
        ):
            raise MainbrellaError("invalid_runtime_host")
        workspace = cls(
            host=handle.host, api_key=handle.api_key, working_dir=WORKING_DIR
        )
        workspace._sandbox = sandbox
        workspace._handle = handle
        return workspace

    @classmethod
    def attach(cls, client: Mainbrella, handle: RuntimeHandle) -> MainbrellaWorkspace:
        sandbox = client.connect(handle.id, handle.created_at)
        if not any(
            c["id"] == handle.id
            and c["createdAt"] == handle.created_at
            and c["status"] == "running"
            for c in client.list()["containers"]
        ):
            raise MainbrellaError("stale_runtime_generation")
        return cls._from_handle(sandbox, handle)

    def cleanup(self) -> None:
        self.reset_client()
        if self._owned and not self._stopped:
            try:
                self._sandbox.kill()
            except Exception:
                self._record(
                    {
                        "id": self.handle.id,
                        "created_at": self.handle.created_at,
                        "cleanup": "failed",
                    }
                )
                raise
            self._stopped = True
            self._record(
                {
                    "id": self.handle.id,
                    "created_at": self.handle.created_at,
                    "cleanup": "completed",
                }
            )

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.cleanup()
