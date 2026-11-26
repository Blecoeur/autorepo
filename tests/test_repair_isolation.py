import json
import os
import subprocess
import unittest
from unittest.mock import patch

from repair.propose import run_vibe

IMAGE = os.environ.get("AUTOREPO_TEST_VIBE_IMAGE")
FAKE_KEY = "verification-only-host-credential"
MODEL = "offline-test"


class Completion:
    status = 200

    def __init__(self, streaming):
        text = "isolated-repair-probe"
        usage = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
        common = {"id": "fixture", "model": MODEL, "created": 1}
        if streaming:
            self.kind = "text/event-stream"
            def event(choices, **extra):
                return "data: " + json.dumps({**common, "object": "chat.completion.chunk",
                                             "choices": choices, **extra}) + "\n\n"
            self.chunks = iter([
                event([{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]),
                event([{"index": 0, "delta": {}, "finish_reason": "stop"}]),
                event([], usage=usage),
                "data: [DONE]\n\n",
            ])
        else:
            self.kind = "application/json"
            self.chunks = iter([json.dumps({
                **common, "object": "chat.completion", "usage": usage,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
            })])

    def getheader(self, name, default):
        return self.kind

    def read1(self, size):
        return next(self.chunks, "").encode()


@unittest.skipUnless(IMAGE, "Requires the Linux Docker repair image")
class RepairIsolationTest(unittest.TestCase):
    def test_offline_vibe_reaches_only_the_broker(self):
        seen = []

        class MockUpstream:
            def __init__(self, host, timeout):
                seen.append(host)

            def request(self, method, endpoint, data, headers):
                seen.extend((method, endpoint, headers["Authorization"]))
                self.body = json.loads(data)

            def getresponse(self):
                self.assert_model()
                return Completion(self.body.get("stream", False))

            def assert_model(self):
                assert self.body["model"] == MODEL
                assert not self.body.get("tools")
                assert self.body["max_tokens"] <= 400

            def close(self):
                pass

        policy = {"model": MODEL, "max_turns": 1, "max_tokens": 400,
                  "budget_cents": 10, "timeout_seconds": 45}
        options = []
        def inspect_command(args, **kwargs):
            if args[:2] == ["docker", "run"]:
                options.extend(args)
                self.assertNotIn(FAKE_KEY, repr(args) + repr(kwargs.get("env")))
            result = real_run(args, **kwargs)
            if args[:2] == ["docker", "run"] and result.returncode:
                self.fail(result.stderr.decode(errors="replace").replace(FAKE_KEY, "[redacted]"))
            return result

        real_run = subprocess.run
        with patch("repair.broker.HTTPSConnection", MockUpstream), patch(
                "repair.propose.subprocess.run", side_effect=inspect_command):
            result, usage = run_vibe("Return the isolated-repair-probe marker.", policy, IMAGE, FAKE_KEY)
        self.assertIn("isolated-repair-probe", result)
        self.assertEqual(usage, {"requests": 1, "prompt_tokens": 12, "completion_tokens": 5})
        self.assertEqual(seen, ["api.mistral.ai", "POST", "/v1/chat/completions", "Bearer " + FAKE_KEY])
        self.assertEqual(options[options.index("--network") + 1], "none")
        self.assertIn("--read-only", options)
        self.assertFalse(any("docker.sock" in item for item in options))
        self.assertNotIn(FAKE_KEY, result)
