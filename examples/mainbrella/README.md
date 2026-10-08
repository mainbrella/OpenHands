# MainBrella per-conversation workspace prototype

This is a runnable response to [OpenHands #17833](https://github.com/OpenHands/OpenHands/issues/17833), not a proposed Canvas integration PR. It uses the published OpenHands SDK 1.53.0 and Mainbrella Python SDK 0.1.0. No backend changes, fork, custom Agent Server endpoints, or new Canvas backend kind are required.

Each `MainbrellaWorkspace.create()` provisions one Python machine, installs a pinned OpenHands Agent Server, and connects the SDK's existing `RemoteWorkspace` to it through a protected preview. Use one workspace per conversation. Commands, files, Git, conversation requests, and event transport continue to use OpenHands' existing protocol. This demonstrates the existing remote-workspace path; it does not add MainBrella to Canvas's local/Docker execution-runtime selector or implement the provider interface being discussed upstream.

## Run

Install [uv](https://docs.astral.sh/uv/) and Node 24+. From this directory:

```sh
uv sync --locked
uv run --locked pytest
```

Provision `MAINBRELLA_API_KEY` through your environment or secret manager. The API key requires an active plan/trial and enough allowance for two simultaneous Small machines. Optional `MAINBRELLA_API_URL` selects an HTTPS API origin or HTTP loopback. The verifier uses **two starts**, downloads its output, and stops only its own exact generations. Installation is intentionally slow in this prototype; a production provider should use a prepared image.

```sh
uv run --locked python verify.py
```

To use an existing ignored environment file, Node's loader keeps credentials out of arguments and shell history:

```sh
MAINBRELLA_PROTOTYPE_PYTHON="$PWD/.venv/bin/python" \
  node --env-file=/absolute/path/to/.env run.mjs
```

For the existing sibling Mainbrella local service, start `npm run dev:all` in `../backend` relative to the OpenHands repository root so the Python image is advertised. Apply its documented local database migrations, including `npx wrangler d1 migrations apply mainbrella-preview-routes --local`. Local accounts and plan access are separate from production. From this example directory:

```sh
MAINBRELLA_PROTOTYPE_PYTHON="$PWD/.venv/bin/python" \
  node --env-file=../../../backend/.env run.mjs --local
```

`--local` maps that file's `LOCAL_MAINBRELLA_API_KEY` to the child process's standard credential variable and pins the API origin to `http://localhost:8787`. It never falls back to production. You can instead set `MAINBRELLA_API_KEY` and `MAINBRELLA_API_URL` yourself and run `verify.py` directly.

The verifier checks authenticated server metadata, stdout/stderr/exit status, timeout with retained partial output, a 1 MiB binary round trip, reconnection to an exact generation, conversation creation plus a WebSocket user event, two-machine filesystem isolation, and cleanup. It makes no model requests. `send_message()` persists the event; it does not call `run()`. This is transport qualification, not an agent-quality evaluation.

The [October 8 local qualification report](qualification.json) records all seven transport checks passing, completed cleanup, and the separate SDK timeout-output gap. Seven unit tests also passed from the locked environment. The Canvas UI and production service were not exercised.

## Use with a conversation

```python
import os
import uuid

from mainbrella import Mainbrella
from openhands.sdk import Agent, Conversation, LLM
from mainbrella_workspace import MainbrellaWorkspace

client = Mainbrella(os.environ["MAINBRELLA_API_KEY"])
llm = LLM(model=os.environ["LLM_MODEL"],
          api_key=os.environ["LLM_API_KEY"], usage_id="agent")

# Import tools locally so their module names travel to the remote Agent Server.
# Add openhands-tools==1.53.0 to your application for the normal coding tools.
from openhands.sdk.tool import Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name),
                            Tool(name=FileEditorTool.name)])
with MainbrellaWorkspace.create(client, creation_key=str(uuid.uuid4())) as workspace:
    conversation = Conversation(agent=agent, workspace=workspace)
    try:
        conversation.send_message("Write and run a Python script in /workspace.")
        conversation.run()
    finally:
        conversation.close()
    workspace.file_download("/workspace/output.txt", "output.txt")
```

The account credential stays in the caller. Agent Server receives a separately generated runtime session key and encryption key. Model credentials follow the existing remote-conversation behavior and are sent to the remote Agent Server. Do not put the account key in guest files or agent context.

## Lifecycle and current limits

- Creation uses a caller-supplied idempotency key. Persist it **before** calling create. The optional `record` callback records that key before POST and the exact identity immediately after creation. Ambiguous creation must be reconciled with the same selection and key, not retried with a new key. The SDK retains the key on creation errors.
- `RuntimeHandle` contains `id` plus `created_at`, the endpoint, runtime key, preview ID, and actual preview expiration. `attach(client, handle)` checks that exact generation is still running. Borrowed handles are left running on exit; created workspaces own cleanup. Cleanup can be retried after failure.
- `verify.py` records recovery state in atomic 0600 files within a 0700 directory. Preview URLs and runtime keys are credentials. Keep this directory private and untracked. Normal Pydantic dumps/reprs exclude the endpoint and key; the explicit handle is sensitive. The verifier suppresses SDK transport logs because they may include bearer preview hosts. Applications must similarly suppress or redact transport logs and returned HTTP errors. After interruption, inspect these state files and stop only the recorded generation through `client.connect(id, created_at).kill()`.
- Previews expire after at most one hour, clipped to the machine's hard lease. This example has no automatic endpoint renewal. A long-lived provider needs endpoint refresh with client/WebSocket reconnection; otherwise expiration must surface as unavailable. Current preview URLs are bearer subdomains; they are not stable signed runtime endpoints.
- Ordinary machine stop discards unsaved filesystem state. Snapshot/export APIs are separate and capability-gated. Conversation history or a reconnect handle does not preserve a stopped guest. Memory/process pause-resume is not implemented here.
- Browser tooling and the embedded editor are not qualified. Application cookies are stripped by preview ingress; Agent Server's session-key HTTP/WebSocket paths are the target of this prototype. Canvas CORS, embedded mode, Cloud selection, ACP, delegated/planning conversations, and automations are not exercised by this example.
- Bootstrap jobs have a 15-minute maximum deadline and bounded output. The installer disables progress output. Workload execution through Agent Server remains bounded by the machine lease and OpenHands' own limits. The prototype does not automatically replay command or file mutations.
- The local check found an SDK 1.53.0 gap: `RemoteWorkspace.execute_command()` reports a timeout but can omit the final buffered stdout because its polling deadline coincides with the command deadline. The verifier records this separately and checks retained partial output through `start_command()` / `get_command_output()`. A provider qualification contract should distinguish process deadlines from transport/polling deadlines. The example does not patch the SDK to hide the gap.

## Interface proposal for #17833

Keep provider lifecycle in `software-agent-sdk` / Agent Server and keep Canvas a typed consumer. A useful first interface would cover:

1. `create_or_reconcile(spec, idempotency_key)` returning an opaque generation-qualified handle.
2. `inspect(handle)` returning lifecycle state, actual resource/lease limits, and capabilities.
3. `connect(handle)` returning an authenticated OpenHands Agent Server endpoint and its expiration.
4. `stop(handle)` scoped to the exact generation, with an explicit uncertain-cleanup outcome.
5. Optional endpoint renewal, snapshot/restore, pause/resume, networking, and prepared-image support, advertised as separate capabilities.

Distinguish control-plane credentials from per-runtime access. Unsupported operations should be explicit rather than silently emulated. Expose an instance default with optional per-conversation and per-automation overrides; bind the selected provider/profile to the created conversation so resuming it does not move execution to whichever backend is currently active. Delegated workers' machine-sharing policy should be explicit.

For marketplace trust, surface who operates the service, required credential scope, resource and billing limits, network policy, retention, and whether stopping preserves files. A provider-neutral qualification suite should cover creation ambiguity, stale handles, command/file results, event reconnect, lease/endpoint expiry, and failed cleanup.

MainBrella can be a concrete test implementation for that interface. We can report unsupported requirements and capability mismatches against the maintainers' contract rather than asking Canvas to adopt a provider-specific design.
