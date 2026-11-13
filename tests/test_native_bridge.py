import io
import json
import struct
import subprocess
import unittest
from unittest.mock import patch

from codex_monitor.native_bridge import _exchange, _rpc, send_message


THREAD = "11111111-1111-4111-8111-111111111111"
TOOL = {"name": "send_message_to_thread", "namespace": "codex_app",
        "inputSchema": {"properties": {"threadId": {}, "prompt": {}}}}


def frame(request_id, result):
    value = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()
    return struct.pack("<I", len(value)) + value


class FakePipe:
    """Fragment reads/writes to exercise stream framing without an actual pipe."""
    def __init__(self, responses):
        self.input = io.BytesIO(responses)
        self.output = bytearray()

    def read(self, size):
        return self.input.read(min(size, 3))

    def write(self, value):
        size = min(len(value), 5)
        self.output.extend(value[:size])
        return size

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def requests(self):
        stream = io.BytesIO(self.output)
        values = []
        while header := stream.read(4):
            values.append(json.loads(stream.read(struct.unpack("<I", header)[0])))
        return values


class NativeBridgeTests(unittest.TestCase):
    def request(self):
        return {"pipe": "mock-pipe", "thread_id": THREAD, "prompt": "event payload",
                "call_id": "test-call"}

    def test_fragmented_transport_submits_to_same_caller_and_target(self):
        pipe = FakePipe(frame(1, {"tools": [TOOL]}) + frame(2, {"success": True}))
        stages = []

        def stage(value):
            self.assertEqual(len(pipe.requests()), 1)  # mutation has not started
            stages.append(value)

        with patch("builtins.open", return_value=pipe):
            result = _exchange(self.request(), stage)
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(stages, ["sending"])
        request = pipe.requests()[1]
        self.assertEqual(request["method"], "tools/call")
        self.assertEqual(request["params"]["threadId"], THREAD)
        self.assertEqual(request["params"]["arguments"]["threadId"], THREAD)
        self.assertEqual(request["params"]["arguments"]["prompt"], "event payload")

    def test_missing_tool_never_sends(self):
        pipe = FakePipe(frame(1, {"tools": []}))
        with patch("builtins.open", return_value=pipe):
            result = _exchange(self.request(), lambda _: self.fail("must not send"))
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(len(pipe.requests()), 1)

    def test_incompatible_schema_never_sends(self):
        pipe = FakePipe(frame(1, {"tools": [{**TOOL, "inputSchema": {}}]}))
        with patch("builtins.open", return_value=pipe):
            self.assertEqual(_exchange(self.request(), lambda _: self.fail())["state"], "blocked")

    def test_connection_failure_is_retryable_and_redacts_path(self):
        with patch("builtins.open", side_effect=OSError("private pipe value")):
            result = _exchange(self.request(), lambda _: self.fail())
        self.assertEqual(result["state"], "retryable")
        self.assertNotIn("private pipe value", result["detail"])

    def test_eof_after_mutating_write_is_uncertain(self):
        pipe = FakePipe(frame(1, {"tools": [TOOL]}))
        with patch("builtins.open", return_value=pipe):
            self.assertEqual(_exchange(self.request(), lambda _: None)["state"], "uncertain")

    def test_host_failure_after_send_is_uncertain(self):
        pipe = FakePipe(frame(1, {"tools": [TOOL]}) + frame(2, {"success": False}))
        with patch("builtins.open", return_value=pipe):
            self.assertEqual(_exchange(self.request(), lambda _: None)["state"], "uncertain")

    def test_rpc_rejects_wrong_response_id(self):
        with self.assertRaises(ValueError):
            _rpc(FakePipe(frame(9, {})), 1, "tools/list", {})

    @patch.dict("os.environ", {}, clear=True)
    @patch("subprocess.run")
    def test_missing_pipe_does_not_launch(self, run):
        self.assertEqual(send_message(THREAD, "message", {})["state"], "retryable")
        run.assert_not_called()

    @patch("subprocess.run")
    def test_invalid_input_does_not_launch(self, run):
        for thread, config in [("invalid", {}), (THREAD, {"app_timeout": 0}),
                               (THREAD, {"app_timeout": float("nan")})]:
            self.assertEqual(send_message(thread, "message", config)["state"], "blocked")
        run.assert_not_called()

    @patch("subprocess.run")
    def test_timeout_before_send_is_retryable(self, run):
        run.side_effect = subprocess.TimeoutExpired("helper", 30, output=b"")
        self.assertEqual(send_message(THREAD, "message", {"app_pipe": "mock"})["state"], "retryable")

    @patch("subprocess.run")
    def test_timeout_after_send_is_uncertain(self, run):
        run.side_effect = subprocess.TimeoutExpired("helper", 30, output=b'{"stage":"sending"}\n')
        self.assertEqual(send_message(THREAD, "message", {"app_pipe": "mock"})["state"], "uncertain")
        self.assertEqual(run.call_count, 1)

    @patch("subprocess.run")
    def test_result_remains_submitted_pending_ack(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '{"stage":"sending"}\n'
            '{"state":"submitted","detail":"waiting for ack","transport":"native"}\n', "")
        self.assertEqual(send_message(THREAD, "message", {"app_pipe": "mock"})["state"], "submitted")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 30)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(json.loads(kwargs["input"])["pipe"], "mock")

    @patch("subprocess.run")
    def test_child_crash_after_send_is_uncertain(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '{"stage":"sending"}\n', "")
        self.assertEqual(send_message(THREAD, "message", {"app_pipe": "mock"})["state"], "uncertain")

    @patch("subprocess.run", side_effect=OSError("launch failed"))
    def test_helper_launch_failure_is_retryable(self, run):
        self.assertEqual(send_message(THREAD, "message", {"app_pipe": "mock"})["state"], "retryable")


if __name__ == "__main__":
    unittest.main()
