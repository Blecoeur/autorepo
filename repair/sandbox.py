"""Present the host's inference broker to Vibe over container loopback."""
import json
import os
from pathlib import Path
import selectors
import socket
import socketserver
import subprocess
import sys
import threading


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as broker, selectors.DefaultSelector() as ready:
            broker.connect("/broker/inference.sock")
            ready.register(self.request, selectors.EVENT_READ, broker)
            ready.register(broker, selectors.EVENT_READ, self.request)
            while True:
                for key, _ in ready.select(300):
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)


def main():
    model, turns, tokens, price, seconds, allow_bash = sys.argv[1:]
    home = Path("/tmp/vibe")
    home.mkdir()
    (home / "config.toml").write_text(f"""
active_model = "repair"
enable_update_checks = false
[[providers]]
name = "broker"
api_base = "http://127.0.0.1:8000/v1"
api_style = "openai"
api_key_env_var = "AUTOREPO_LOCAL_BROKER_TOKEN"
backend = "generic"
[[models]]
name = {json.dumps(model)}
provider = "broker"
alias = "repair"
max_context_length = 65536
""")
    tool_flags = ["--disabled-tools", "*"]
    if allow_bash == "true":
        with (home / "config.toml").open("a") as config:
            config.write('\n[tools.bash]\npermission = "always"\n')
        tool_flags = ["--enabled-tools", "bash"]
    with socketserver.ThreadingTCPServer(("127.0.0.1", 8000), Relay) as relay:
        relay.daemon_threads = True
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        result = subprocess.run(
            ["vibe", "--prompt-file", "/request/prompt.txt", "--output", "text",
             *tool_flags, "--max-turns", turns, "--max-tokens", tokens,
             "--max-price", price, "--time-limit", seconds],
            cwd="/tmp", env={"PATH": os.environ["PATH"], "HOME": "/tmp",
                            "VIBE_HOME": str(home), "VIBE_TELEMETRY_ENABLED": "false",
                            "AUTOREPO_LOCAL_BROKER_TOKEN": "local-loopback-placeholder"},
            check=False,
        )
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
