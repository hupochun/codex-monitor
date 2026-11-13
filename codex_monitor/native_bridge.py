"""Desktop App native-pipe adapter, isolated in a timeout-bounded helper.

This is a version-probed local App implementation detail, not a public stable API.
An App restart may invalidate its inherited pipe path. A successful submission
still requires a separate acknowledgement from the original conversation.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import uuid

MAX_FRAME = 64 * 1024 * 1024


def _result(state: str, detail: str) -> dict:
    return {"state": state, "detail": detail, "transport": "native"}


def _read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        data = stream.read(size - len(chunks))
        if not data:
            raise EOFError("Desktop App pipe closed")
        chunks.extend(data)
    return bytes(chunks)


def _rpc(stream, request_id, method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": request_id,
                          "method": method, "params": params}).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ValueError("Native request exceeds frame limit")
    frame = struct.pack("<I", len(payload)) + payload
    view = memoryview(frame)
    while view:
        written = stream.write(view)
        if not written:
            raise OSError("Native pipe write did not progress")
        view = view[written:]
    size = struct.unpack("<I", _read_exact(stream, 4))[0]
    if size > MAX_FRAME:
        raise ValueError("Native response exceeds frame limit")
    response = json.loads(_read_exact(stream, size))
    if response.get("id") != request_id or response.get("jsonrpc") != "2.0":
        raise ValueError("Unexpected native response identity")
    if "error" in response:
        raise RuntimeError("Desktop App returned an RPC error")
    return response["result"]


def _exchange(request, report_stage):
    """Only the helper calls this; report before the first mutating write."""
    sending = False
    try:
        with open(request["pipe"], "r+b", buffering=0) as stream:
            catalog = _rpc(stream, 1, "tools/list", {"threadStartKind": "all"})
            candidates = [tool for tool in catalog["tools"]
                          if tool.get("name") == "send_message_to_thread"]
            if len(candidates) != 1 or candidates[0].get("namespace") != "codex_app":
                return _result("blocked", "Desktop App does not expose the expected message tool.")
            schema = candidates[0].get("inputSchema", {})
            properties = schema.get("properties", {})
            if not {"threadId", "prompt"}.issubset(properties):
                return _result("blocked", "Desktop message tool schema is incompatible.")
            # Written and flushed to helper stdout before any mutating pipe write.
            # Parent timeouts after this boundary must never trigger a blind retry.
            report_stage("sending")
            sending = True
            result = _rpc(stream, 2, "tools/call", {
                "arguments": {"threadId": request["thread_id"], "prompt": request["prompt"]},
                "callerSource": "codex", "callId": request["call_id"],
                "namespace": "codex_app", "threadId": request["thread_id"],
                "tool": "send_message_to_thread", "turnId": "mcp-turn-" + request["call_id"],
            })
            if result.get("success") is True:
                return _result("submitted", "Desktop App accepted the message; waiting for original conversation acknowledgement.")
            return _result("uncertain", "Desktop App did not confirm submission; do not resend blindly.")
    except (OSError, EOFError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        state = "uncertain" if sending else "retryable"
        # Do not expose pipe paths, prompts, or arbitrary host error content.
        return _result(state, f"Native App bridge {type(exc).__name__} {'after send began' if sending else 'before message send'}.")


def _records(output):
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    records = []
    for line in (output or "").splitlines():
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
        except ValueError:
            pass
    return records


def send_message(thread_id: str, prompt: str, config: dict) -> dict:
    """Send an explicitly authorized follow-up to its original App conversation.

    Never fall back to CLI queue here: it can address a different runtime.
    ``callId`` correlates calls; no server-side idempotency is assumed.
    """
    try:
        uuid.UUID(thread_id)
        timeout = float(config.get("app_timeout", 30))
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("Invalid timeout")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Empty prompt")
    except (ValueError, TypeError, AttributeError):
        return _result("blocked", "Invalid thread, prompt, or app_timeout (must be within 0–300 seconds).")
    pipe = config.get("app_pipe") or os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
    if not pipe:
        return _result("retryable", "Desktop App tools pipe unavailable; launch the monitor from the App or supply app_pipe.")
    request = {"pipe": str(pipe), "thread_id": thread_id, "prompt": prompt,
               "call_id": "codex-monitor-" + str(uuid.uuid4())}
    # Absolute script path is independent of the training process cwd.
    command = [sys.executable, str(Path(__file__).resolve()), "--helper"]
    try:
        response = subprocess.run(
            command, input=json.dumps(request), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        sent = any(record.get("stage") == "sending" for record in _records(exc.stdout))
        return _result("uncertain" if sent else "retryable",
                       "Native helper timed out after send began; do not resend blindly." if sent
                       else "Native helper timed out before message send.")
    except OSError:
        return _result("retryable", "Could not launch native bridge helper.")
    records = _records(response.stdout)
    sent = any(record.get("stage") == "sending" for record in records)
    if response.returncode == 0:
        for record in reversed(records):
            if record.get("state") in {"submitted", "retryable", "uncertain", "blocked"}:
                return record
    return _result("uncertain" if sent else "retryable", "Native helper exited without a confirmed result.")


def _main():
    if sys.argv[1:] != ["--helper"]:
        raise SystemExit("Internal helper; use codex-monitor commands.")
    request = json.load(sys.stdin)
    result = _exchange(request, lambda stage: print(json.dumps({"stage": stage}), flush=True))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    _main()
