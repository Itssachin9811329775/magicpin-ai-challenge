"""End-to-end contract checks against the real HTTP server."""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


class APITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ, PORT=str(cls.port), STATE_DB=str(pathlib.Path(cls.tmp.name) / "state.sqlite3"))
        cls.server = subprocess.Popen([sys.executable, "bot.py"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                cls.call("GET", "/v1/healthz")
                break
            except urllib.error.URLError:
                time.sleep(.05)
        else:
            raise RuntimeError("server did not start: " + cls.server.stderr.read().decode())

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        self.call("POST", "/v1/teardown", {})

    @classmethod
    def call(cls, method, path, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(cls.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def push(self, scope, payload, version=1):
        field = {"category": "slug", "merchant": "merchant_id", "customer": "customer_id", "trigger": "id"}[scope]
        return self.call("POST", "/v1/context", {"scope": scope, "context_id": payload[field],
                          "version": version, "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})

    @staticmethod
    def seed(name, key):
        return json.loads((ROOT / "dataset" / name).read_text())[key]

    def load(self):
        categories = [json.loads(f.read_text()) for f in (ROOT / "dataset/categories").glob("*.json")]
        merchants = self.seed("merchants_seed.json", "merchants")
        customers = self.seed("customers_seed.json", "customers")
        triggers = self.seed("triggers_seed.json", "triggers")
        for scope, group in (("category", categories), ("merchant", merchants), ("customer", customers), ("trigger", triggers)):
            for item in group:
                self.assertEqual(self.push(scope, item)[0], 200)
        return categories, merchants, customers, triggers

    def test_health_metadata_and_full_warmup(self):
        self.assertEqual(self.call("GET", "/v1/healthz")[1]["contexts_loaded"], dict.fromkeys(("category", "merchant", "customer", "trigger"), 0))
        status, metadata = self.call("GET", "/v1/metadata")
        self.assertEqual(status, 200)
        self.assertIn("team_name", metadata)
        self.load()
        self.assertEqual(self.call("GET", "/v1/healthz")[1]["contexts_loaded"], {"category": 5, "merchant": 10, "customer": 15, "trigger": 25})

    def test_context_version_and_validation(self):
        merchant = self.seed("merchants_seed.json", "merchants")[0]
        self.assertEqual(self.push("merchant", merchant)[0], 200)
        self.assertEqual(self.push("merchant", merchant)[0], 409)
        updated = dict(merchant, performance=dict(merchant["performance"], views=9876))
        self.assertEqual(self.push("merchant", updated, 2)[0], 200)
        self.assertEqual(self.push("merchant", merchant)[1]["current_version"], 2)
        self.assertEqual(self.call("POST", "/v1/context", {"scope": "invalid", "context_id": "x", "version": 1, "payload": {}})[0], 400)
        self.assertEqual(self.call("POST", "/v1/context", {"scope": "merchant", "context_id": "wrong", "version": 1, "payload": merchant})[0], 400)
        self.assertEqual(self.call("POST", "/v1/context", {"scope": "merchant", "context_id": merchant["merchant_id"], "version": "3", "payload": merchant})[0], 400)

    def test_tick_grounding_dedup_and_conversations(self):
        _, merchants, _, triggers = self.load()
        tids = [t["id"] for t in triggers]
        code, data = self.call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": tids})
        self.assertEqual(code, 200)
        actions = data["actions"]
        self.assertGreater(len(actions), 5)
        self.assertLessEqual(len(actions), 20)
        self.assertEqual(len({a["conversation_id"] for a in actions}), len(actions))
        for action in actions:
            self.assertIn(action["merchant_id"], [m["merchant_id"] for m in merchants])
            self.assertIn(action["trigger_id"], tids)
            self.assertTrue(action["body"] and action["rationale"] and action["suppression_key"])
            self.assertTrue(action["template_name"] and action["template_params"])
            self.assertNotIn("http://", action["body"])
        emitted = [a["trigger_id"] for a in actions]
        self.assertEqual(self.call("POST", "/v1/tick", {"now": "2026-04-26T10:40:00Z", "available_triggers": emitted})[1]["actions"], [])
        self.assertTrue(any("JIDA" in a["body"] for a in actions if a["trigger_id"] == "trg_001_research_digest_dentists"))

    def test_missing_context_and_customer_consent(self):
        cat = json.loads((ROOT / "dataset/categories/dentists.json").read_text())
        merchant = self.seed("merchants_seed.json", "merchants")[0]
        customer = self.seed("customers_seed.json", "customers")[0]
        trigger = self.seed("triggers_seed.json", "triggers")[2]
        self.push("trigger", trigger)
        tick = {"now": "2026-04-26T11:00:00Z", "available_triggers": [trigger["id"]]}
        self.assertEqual(self.call("POST", "/v1/tick", tick)[1]["actions"], [])
        self.push("category", cat)
        self.push("merchant", merchant)
        self.assertEqual(self.call("POST", "/v1/tick", tick)[1]["actions"], [])
        self.push("customer", customer)
        action = self.call("POST", "/v1/tick", tick)[1]["actions"][0]
        self.assertEqual(action["send_as"], "merchant_on_behalf")
        self.assertEqual(action["customer_id"], customer["customer_id"])
        self.assertIn("2026-11-12", action["body"])
        self.assertIn("₹299", action["body"])

    def test_replies_and_opt_out(self):
        self.load()
        tid = "trg_001_research_digest_dentists"
        action = self.call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": [tid]})[1]["actions"][0]
        reply = {"conversation_id": action["conversation_id"], "merchant_id": action["merchant_id"], "from_role": "merchant", "turn_number": 2}
        self.assertEqual(self.call("POST", "/v1/reply", dict(reply, message="Thank you for contacting us. Our team will respond shortly."))[1]["action"], "wait")
        self.assertEqual(self.call("POST", "/v1/reply", dict(reply, message="Thank you for contacting us. Our team will respond shortly."))[1]["action"], "end")
        next_reply = dict(reply, conversation_id="new_session", message="Ok lets do it. Whats next?")
        response = self.call("POST", "/v1/reply", next_reply)[1]
        self.assertEqual(response["action"], "send")
        self.assertIn("draft", response["body"].lower())
        self.assertEqual(self.call("POST", "/v1/reply", dict(next_reply, message="Stop messaging me."))[1]["action"], "end")

    def test_expanded_warmup_and_adaptive_context(self):
        with tempfile.TemporaryDirectory() as out:
            subprocess.run([sys.executable, "dataset/generate_dataset.py", "--seed-dir", "dataset", "--out", out], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            for scope, folder in (("category", "categories"), ("merchant", "merchants"), ("customer", "customers"), ("trigger", "triggers")):
                for f in (pathlib.Path(out) / folder).glob("*.json"):
                    self.assertEqual(self.push(scope, json.loads(f.read_text()))[0], 200)
        self.assertEqual(self.call("GET", "/v1/healthz")[1]["contexts_loaded"], {"category": 5, "merchant": 50, "customer": 200, "trigger": 100})
        cat = json.loads((ROOT / "dataset/categories/dentists.json").read_text())
        merchant = self.seed("merchants_seed.json", "merchants")[0]
        revised = dict(cat, digest=[dict(cat["digest"][0], title="Fresh verified dentistry update from the judge")])
        revised_merchant = dict(merchant, customer_aggregate=dict(merchant["customer_aggregate"], high_risk_adult_count=211))
        self.assertEqual(self.push("category", revised, 2)[0], 200)
        self.assertEqual(self.push("merchant", revised_merchant, 2)[0], 200)
        action = self.call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": ["trg_001_research_digest_dentists"]})[1]["actions"][0]
        self.assertIn("Fresh verified dentistry update", action["body"])
        self.assertIn("211 high-risk adults", action["body"])

    def test_bad_json_and_bad_tick_reply(self):
        self.assertEqual(self.call("POST", "/v1/context", raw=b"{oops")[0], 400)
        self.assertEqual(self.call("POST", "/v1/tick", {"available_triggers": [123]})[0], 400)
        self.assertEqual(self.call("POST", "/v1/reply", {"conversation_id": "x", "from_role": "merchant", "message": ""})[0], 400)
        self.assertEqual(self.call("GET", "/missing")[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
