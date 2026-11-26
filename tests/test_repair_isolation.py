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

    def __init__(self, streaming, tool_call=None):
        message = {"role": "assistant", "content": "isolated-repair-probe"}
        finish = "stop"
        if tool_call:
            message = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
            finish = "tool_calls"
        usage = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
        common = {"id": "fixture", "model": MODEL, "created": 1}
        if streaming:
            self.kind = "text/event-stream"
            def event(choices, **extra):
                return "data: " + json.dumps({**common, "object": "chat.completion.chunk",
                                             "choices": choices, **extra}) + "\n\n"
            self.chunks = iter([
                event([{"index": 0, "delta": message, "finish_reason": None}]),
                event([{"index": 0, "delta": {}, "finish_reason": finish}]),
                event([], usage=usage),
                "data: [DONE]\n\n",
            ])
        else:
            self.kind = "application/json"
            self.chunks = iter([json.dumps({
                **common, "object": "chat.completion", "usage": usage,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            })])

    def getheader(self, name, default):
        return self.kind

    def read1(self, size):
        return next(self.chunks, "").encode()


@unittest.skipUnless(IMAGE, "Requires the Linux Docker repair image")
class RepairIsolationTest(unittest.TestCase):
    def test_offline_vibe_reaches_only_the_broker(self):
        self.run_probe(allow_bash=False)

    def test_offline_vibe_can_run_bash_and_return_its_output(self):
        self.run_probe(allow_bash=True)

    def run_probe(self, allow_bash):
        seen = []
        requests = []

        class MockUpstream:
            def __init__(self, host, timeout):
                seen.append(host)

            def request(self, method, endpoint, data, headers):
                seen.extend((method, endpoint, headers["Authorization"]))
                self.body = json.loads(data)
                requests.append(self.body)

            def getresponse(self):
                assert self.body["model"] == MODEL
                assert self.body["max_tokens"] <= 400
                streaming = self.body.get("stream", False)
                names = [tool["function"]["name"] for tool in self.body.get("tools", [])]
                assert names == (["bash"] if allow_bash else [])
                if allow_bash and len(requests) == 1:
                    tool_call = {"id": "shelltest", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": "printf '%s%s' 4 2"}),
                    }}
                    if streaming:
                        tool_call["index"] = 0
                    return Completion(streaming, tool_call)
                return Completion(streaming)

            def close(self):
                pass

        count = 2 if allow_bash else 1
        policy = {"model": MODEL, "allow_bash": allow_bash, "max_turns": count, "max_tokens": 400,
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
        self.assertEqual(usage, {"requests": count, "prompt_tokens": 12 * count, "completion_tokens": 5 * count})
        self.assertEqual(seen, ["api.mistral.ai", "POST", "/v1/chat/completions", "Bearer " + FAKE_KEY] * count)
        if allow_bash:
            tool_outputs = [message["content"] for message in requests[-1]["messages"] if message["role"] == "tool"]
            self.assertTrue(any("42" in content for content in tool_outputs), tool_outputs)
        self.assertEqual(options[options.index("--network") + 1], "none")
        self.assertIn("--read-only", options)
        self.assertFalse(any("docker.sock" in item for item in options))
        self.assertNotIn(FAKE_KEY, result)
