import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_monitor.bridge import deliver, notification


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.result = Path(self.temp.name) / "jobs" / "abc" / "result.json"
        self.result.parent.mkdir(parents=True)
        self.result.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
        self.spec = {"thread_id": "11111111-1111-4111-8111-111111111111", "bridge": {"mode": "cli"}}

    def test_disabled_never_invokes_codex(self):
        with patch("subprocess.run") as run:
            result = deliver({"bridge": {"enabled": False}}, "missing", "event")
        self.assertEqual(result["state"], "disabled")
        run.assert_not_called()

    def test_invalid_thread_is_blocked(self):
        self.spec["thread_id"] = "--last"
        self.assertEqual(deliver(self.spec, str(self.result), "e")["state"], "blocked")

    @patch("shutil.which", return_value="codex.exe")
    @patch("subprocess.run")
    def test_resume_warning_is_not_retried_or_confirmed(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, "queued", "warning: resume failed")
        result = deliver(self.spec, str(self.result), "event")
        self.assertEqual(result["state"], "submitted")
        self.assertIn("resume failed", result["detail"])
        self.assertIn("--resume", run.call_args.args[0])

    @patch("shutil.which", return_value="codex.exe")
    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 30))
    def test_timeout_is_uncertain(self, run, which):
        self.assertEqual(deliver(self.spec, str(self.result), "e")["state"], "uncertain")

    @patch("shutil.which", return_value="codex.exe")
    @patch("subprocess.run")
    def test_nonzero_after_possible_enqueue_is_uncertain(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 1, "", "error")
        self.assertEqual(deliver(self.spec, str(self.result), "e")["state"], "uncertain")

    @patch("shutil.which", return_value=None)
    def test_missing_binary_is_safe_to_retry(self, which):
        self.assertEqual(deliver(self.spec, str(self.result), "e")["state"], "retryable")

    def test_ack_has_exact_job_thread_event_and_state_dir(self):
        message = notification(self.spec, str(self.result), "unique-event")
        argv = json.loads(next(line for line in message.splitlines() if line.startswith('["')))
        self.assertEqual(Path(argv[argv.index("--state-dir") + 1]), Path(self.temp.name).resolve())
        self.assertEqual(argv[argv.index("--event-id") + 1], "unique-event")
        self.assertEqual(argv[argv.index("--thread-id") + 1], self.spec["thread_id"])

    @patch.dict("os.environ", {}, clear=True)
    @patch("subprocess.run")
    def test_auto_does_not_silently_resume_another_runtime(self, run):
        self.spec["bridge"] = {"mode": "auto"}
        outcome = deliver(self.spec, str(self.result), "event")
        self.assertEqual(outcome["state"], "retryable")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
