"""Run the supplied simulator's non-LLM scenarios against a fresh local server."""
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import judge_simulator as judge


class NoScoringProvider(judge.LLMProvider):
    def name(self):
        return "operational-only (no score)"

    def complete(self, prompt, system=None):
        raise RuntimeError("Scored judge requires a real LLM provider")


def main():
    with tempfile.TemporaryDirectory() as tmp, socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = dict(os.environ, STATE_DB=str(pathlib.Path(tmp) / "judge.sqlite3"), PORT=str(port))
        server = subprocess.Popen([sys.executable, "bot.py"], cwd=ROOT, env=env)
        try:
            judge.BOT_URL = f"http://127.0.0.1:{port}"
            for _ in range(50):
                try:
                    urllib.request.urlopen(judge.BOT_URL + "/v1/healthz", timeout=1).close()
                    break
                except OSError:
                    time.sleep(.05)
            else:
                raise RuntimeError("Local API failed to start")
            # Calls the official JudgeSimulator unchanged after loading it. Its `all`
            # scenario checks warmup, auto-reply, commitment, and hostile routing;
            # it does not invoke the LLM scorer.
            if not judge.JudgeSimulator(NoScoringProvider()).run("all"):
                raise SystemExit(1)
        finally:
            server.terminate()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
