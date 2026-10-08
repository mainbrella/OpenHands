"""Live SDK transport qualification. Uses two starts; no model requests."""

import argparse
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
os.environ["LITELLM_LOG"] = "ERROR"

from mainbrella import Mainbrella, MainbrellaError
from mainbrella_workspace import MainbrellaWorkspace
from openhands.sdk import LLM, Agent, Conversation
from openhands.sdk.event import MessageEvent


def recorder(path):
    state = {}

    def record(update):
        state.update(update)
        # A preview URL and runtime key are credentials. Use an atomic 0600 file.
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            json.dump(state, f)
            os.chmod(f.name, 0o600)
        os.replace(f.name, path)

    return record


def check(condition, label):
    if not condition:
        raise MainbrellaError(label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument("--size", default="small")
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.state_dir, 0o700)
    logging.disable(logging.CRITICAL)  # SDK HTTP logs include bearer preview URLs.
    client = Mainbrella(
        os.environ["MAINBRELLA_API_KEY"],
        base_url=os.getenv("MAINBRELLA_API_URL", "https://api.mainbrella.com"),
    )
    baseline = {
        (c["id"], c["createdAt"])
        for c in client.list()["containers"]
        if c["status"] == "running"
    }
    report = {
        "api_origin": client.base_url,
        "checks": [],
        "gaps": [],
        "cleanup": "pending",
    }
    run_id = str(uuid.uuid4())

    try:
        print(json.dumps({"phase": "provisioning", "starts": 2}), flush=True)
        with MainbrellaWorkspace.create(
            client,
            creation_key=f"openhands-{run_id}-a",
            size=args.size,
            record=recorder(args.state_dir / f"{run_id}-a.json"),
        ) as first:
            report["checks"].append("authenticated_server_info")
            result = first.execute_command("printf hello; printf problem >&2; exit 7")
            check(
                result.stdout.strip() == "hello"
                and "problem" in result.stderr
                and result.exit_code == 7,
                "command_result",
            )
            report["checks"].append("stdout_stderr_exit_code")
            result = first.execute_command("printf 'partial\\n'; sleep 10", timeout=2)
            report["timeout_result"] = {
                "exit_code": result.exit_code,
                "timeout_occurred": result.timeout_occurred,
                "partial_output_received": "partial" in result.stdout,
            }
            check(result.timeout_occurred, "timeout_result")
            if "partial" not in result.stdout:
                report["gaps"].append(
                    "SDK execute_command timeout omits final buffered output"
                )
            command_id = first.start_command("printf partial; sleep 10", timeout=1)
            deadline = time.monotonic() + 10
            retained = None
            while time.monotonic() < deadline:
                retained = first.get_command_output(command_id)
                if retained and retained.get("exit_code") is not None:
                    break
                time.sleep(0.1)
            check(
                retained
                and retained.get("exit_code") == -1
                and "partial" in (retained.get("stdout") or ""),
                "retained_timeout_output",
            )
            report["checks"].append("timeout_partial_output")

            payload = bytes(range(256)) * 4096
            upload = first.file_upload(payload, "/workspace/probe.bin")
            check(upload.success, "binary_upload")
            download = args.state_dir / "roundtrip.bin"
            check(
                first.file_download("/workspace/probe.bin", download).success
                and download.read_bytes() == payload,
                "binary_roundtrip",
            )
            report["checks"].append("one_mib_binary_roundtrip")
            first.execute_command("printf first > /workspace/isolation.txt")
            with MainbrellaWorkspace.attach(client, first.handle) as attached:
                check(
                    attached.execute_command(
                        "cat /workspace/isolation.txt"
                    ).stdout.strip()
                    == "first",
                    "attach_to_existing_generation",
                )
            report["checks"].append("generation_qualified_reconnect")

            # The actual SDK creates the conversation and streams its user event.
            # send_message does not run the model. The placeholder key is never used.
            arrived = threading.Event()

            def on_event(event):
                if isinstance(event, MessageEvent):
                    arrived.set()

            agent = Agent(
                llm=LLM(
                    model="openai/gpt-4o", api_key="unused", usage_id="qualification"
                ),
                tools=[],
            )
            conversation = Conversation(
                agent=agent, workspace=first, callbacks=[on_event], visualizer=None
            )
            try:
                conversation.send_message("Mainbrella SDK transport probe")
                check(arrived.wait(30), "conversation_websocket_event")
                report["checks"].append("conversation_create_and_websocket_event")
            finally:
                conversation.close()

            print(json.dumps({"phase": "testing_second_workspace"}), flush=True)
            with MainbrellaWorkspace.create(
                client,
                creation_key=f"openhands-{run_id}-b",
                size=args.size,
                record=recorder(args.state_dir / f"{run_id}-b.json"),
            ) as second:
                check(
                    (second.handle.id, second.handle.created_at)
                    != (first.handle.id, first.handle.created_at),
                    "distinct_identity",
                )
                check(
                    second.execute_command(
                        "test ! -e /workspace/isolation.txt"
                    ).exit_code
                    == 0,
                    "filesystem_isolation",
                )
                second.execute_command("printf second > /workspace/isolation.txt")
                check(
                    first.execute_command("cat /workspace/isolation.txt").stdout.strip()
                    == "first",
                    "independent_filesystems",
                )
                report["checks"].append("two_workspace_filesystem_isolation")
        report["ok"] = True
    except Exception as error:
        report["ok"] = False
        report["error"] = getattr(error, "code", type(error).__name__)
    finally:
        current = {
            (c["id"], c["createdAt"])
            for c in client.list()["containers"]
            if c["status"] == "running"
        }
        report["cleanup"] = "completed" if current == baseline else "check_state_files"
        report["ok"] = report.get("ok", False) and report["cleanup"] == "completed"
        (args.state_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
