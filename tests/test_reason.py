"""Suite minimale, sans réseau : un faux LLM scripté. `python -m unittest -v`"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reason import core, server  # noqa: E402
from reason.core import Engine, Rules, validate  # noqa: E402
from reason.lessons import Playbook  # noqa: E402
from reason.llm import LLMError  # noqa: E402


class FakeLLM:
    """Renvoie les réponses scriptées dans l'ordre ; enregistre les prompts reçus."""
    def __init__(self, name, answers):
        self.name, self.answers, self.calls, self.model = name, list(answers), [], f"fake-{name}"
    def chat(self, messages, schema=None, temperature=0.0, max_tokens=None, retries=1):
        self.calls.append(messages)
        if not self.answers:
            raise LLMError(f"{self.name}: plus de réponses scriptées")
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a, {"total_tokens": 10}


def req(**over):
    base = {"namespace": "acme/invoices", "run_id": "run-1", "decision_point": "before_pay",
            "event": {"type": "invoice"}, "state": {"facts": {"amount": 900, "vendor_known": False}},
            "candidate_actions": [{"name": "approve", "schema": {"type": "object", "required": ["limit"],
                                                                  "properties": {"limit": {"type": "number"}},
                                                                  "additionalProperties": False},
                                   "rollback": {"compensating_action": "void"}},
                                  {"name": "escalate", "schema": {"type": "object", "additionalProperties": False}}],
            "idempotency_key": None}
    base.update(over)
    return base


def typed(action, conf=0.9, params=None, used=None):
    params = params if params is not None else ({"limit": 900} if action == "approve" else {})
    return {"action": action, "params_json": json.dumps(params), "confidence": conf, "rationale": "test",
            "lessons_used": used or []}


def engine(tmp, decider=None, reasoner=None, executor=None, thresholds=None):
    cfg = {"data_dir": tmp, "models": {}, "roles": {}, "thresholds": thresholds or {}}
    eng = Engine(cfg)
    for role, fake in (("decider", decider), ("reasoner", reasoner), ("executor", executor)):
        if fake is not None:
            eng.models[fake.name] = fake
            eng.roles[role] = fake.name
    return eng


class TestValidate(unittest.TestCase):
    def test_types_enum_required_additional(self):
        s = {"type": "object", "required": ["a"], "additionalProperties": False,
             "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"enum": ["x"]}}}
        self.assertEqual(validate({"a": 2}, s), [])
        self.assertTrue(validate({"a": True}, s))          # bool n'est pas integer
        self.assertTrue(validate({"a": 0}, s))             # minimum
        self.assertTrue(validate({"a": 1, "b": "y"}, s))   # enum
        self.assertTrue(validate({"a": 1, "c": 1}, s))     # additionalProperties
        self.assertTrue(validate({}, s))                   # required


class TestRules(unittest.TestCase):
    def test_decide_and_forbid(self):
        r = Rules([{"id": "cap", "if": [{"path": "state.facts.amount", "op": "gt", "value": 5000}], "forbid": ["approve"]},
                   {"id": "small", "if": [{"path": "state.facts.vendor_known", "op": "eq", "value": True},
                                          {"path": "state.facts.amount", "op": "lte", "value": 200}],
                    "decide": {"name": "approve", "params": {"limit": 200}}}])
        self.assertEqual(r.decide(req(state={"facts": {"amount": 100, "vendor_known": True}}))["name"], "approve")
        self.assertIsNone(r.decide(req()))
        self.assertIn("approve", r.forbidden(req(state={"facts": {"amount": 9000}})))
        self.assertEqual(r.forbidden(req()), {})
        self.assertFalse(core._cond({"a": None}, {"path": "a", "op": "gt", "value": 1}))  # None jamais > 1


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_rule_wins_without_calling_model(self):
        dec_llm = FakeLLM("d", [])
        eng = engine(self.tmp, decider=dec_llm)
        (Path(self.tmp) / "acme/invoices").mkdir(parents=True)
        (Path(self.tmp) / "acme/invoices/rules.json").write_text(json.dumps(
            [{"id": "small", "if": [{"path": "state.facts.amount", "op": "lte", "value": 1000}],
              "decide": {"name": "escalate"}}]))
        d = eng.decide(req())
        self.assertEqual((d["tier"], d["action"]["name"]), ("rules", "escalate"))
        self.assertEqual(dec_llm.calls, [])

    def test_typed_decision_ok_and_rollback_and_idempotency(self):
        eng = engine(self.tmp, decider=FakeLLM("d", [typed("approve")]))
        d = eng.decide(req(idempotency_key="k1"))
        self.assertEqual(d["action"], {"name": "approve", "params": {"limit": 900}})
        self.assertEqual(d["tier"], "decider")
        self.assertEqual(d["rollback"], {"compensating_action": "void"})
        self.assertTrue(eng.decide(req(idempotency_key="k1"))["replayed"])  # pas de second appel modèle

    def test_executor_decides_when_no_decider(self):
        ex = FakeLLM("x", [typed("escalate")])
        eng = engine(self.tmp, executor=ex)
        d = eng.decide(req())
        self.assertEqual((d["tier"], d["action"]["name"]), ("decider", "escalate"))
        self.assertEqual(len(ex.calls), 1)

    def test_per_request_model_override(self):
        a, b = FakeLLM("a", []), FakeLLM("b", [typed("escalate")])
        eng = engine(self.tmp, decider=a); eng.models["b"] = b
        d = eng.decide(req(models={"decider": "b"}))
        self.assertEqual(d["action"]["name"], "escalate"); self.assertEqual(a.calls, [])

    def test_low_confidence_escalates_to_reasoner_then_human(self):
        eng = engine(self.tmp, decider=FakeLLM("d", [typed("approve", 0.3)]),
                     reasoner=FakeLLM("r", [typed("abstain", 0.9)]))
        d = eng.decide(req())
        self.assertEqual(d["action"]["name"], "ask_human")
        self.assertTrue(d["needs_human"]["required"])
        self.assertIn("decider: approve confiance 0.30", d["rationale"])

    def test_forbidden_action_is_vetoed(self):
        eng = engine(self.tmp, decider=FakeLLM("d", [typed("approve")]))
        (Path(self.tmp) / "acme/invoices").mkdir(parents=True)
        (Path(self.tmp) / "acme/invoices/rules.json").write_text(json.dumps(
            [{"id": "cap", "if": [{"path": "state.facts.amount", "op": "gt", "value": 500}], "forbid": ["approve"]}]))
        d = eng.decide(req())
        self.assertEqual(d["action"]["name"], "ask_human"); self.assertIn("interdit", d["rationale"])

    def test_reasoner_concludes_after_decider_doubt(self):
        eng = engine(self.tmp, decider=FakeLLM("d", [typed("approve", 0.4)]), reasoner=FakeLLM("r", [typed("escalate", 0.8)]))
        d = eng.decide(req())
        self.assertEqual((d["tier"], d["action"]["name"]), ("reasoner", "escalate"))

    def test_idempotency_is_per_namespace_and_rule_decide_must_be_candidate(self):
        eng = engine(self.tmp, decider=FakeLLM("d", [typed("approve"), typed("escalate"), typed("escalate")]))
        a = eng.decide(req(idempotency_key="same", namespace="ns-a"))
        b = eng.decide(req(idempotency_key="same", namespace="ns-b"))
        self.assertEqual((a["namespace"], b["namespace"], "replayed" in b), ("ns-a", "ns-b", False))
        (Path(self.tmp) / "ns-c").mkdir(parents=True)
        (Path(self.tmp) / "ns-c/rules.json").write_text(json.dumps([{"id": "bad", "decide": {"name": "nuke"}}]))
        d = eng.decide(req(namespace="ns-c"))
        self.assertEqual(d["tier"], "decider"); self.assertEqual(d["action"]["name"], "escalate")

    def test_unsupported_action_schema_and_bad_model_override_are_400s(self):
        eng = engine(self.tmp, decider=FakeLLM("d", []))
        with self.assertRaises(ValueError):
            eng.decide(req(candidate_actions=[{"name": "x", "schema": {"anyOf": [{"type": "object"}]}}]))
        with self.assertRaises(ValueError):
            eng.decide(req(models={"decider": ["x"]}))

    def test_invalid_params_and_model_error_fall_through(self):
        eng = engine(self.tmp, decider=FakeLLM("d", [typed("approve", params={"limit": "beaucoup"})]),
                     reasoner=FakeLLM("r", [LLMError("panne")]))
        d0 = eng.decide(req(run_id="r0")); self.assertIn("paramètres invalides", d0["rationale"])
        eng = engine(self.tmp, decider=FakeLLM("d", [{"action": "approve", "params_json": "pas du json", "confidence": 0.9,
                                                        "rationale": "", "lessons_used": []}]),
                     reasoner=FakeLLM("r", [LLMError("panne")]))
        d = eng.decide(req())
        self.assertEqual(d["action"]["name"], "ask_human")
        self.assertIn("paramètres invalides", d["rationale"]); self.assertIn("panne", d["rationale"])

    def test_loop_guard(self):
        eng = engine(self.tmp, decider=FakeLLM("d", [typed("escalate") for _ in range(5)]), thresholds={"loop_max_repeats": 2})
        eng.decide(req()); eng.decide(req())
        d = eng.decide(req())
        self.assertEqual((d["tier"], d["action"]["name"]), ("loop", "ask_human"))

    def test_bad_request_and_namespace(self):
        eng = engine(self.tmp, decider=FakeLLM("d", []))
        with self.assertRaises(ValueError):
            eng.decide({"namespace": "x"})
        for bad in ("../etc", "a/../b", ".hidden", "a//b", "/abs"):
            with self.assertRaises(ValueError, msg=bad):
                eng.decide(req(namespace=bad))
        self.assertTrue(eng.ns_dir("org/auto.v2/prod").is_dir())

    def test_outcome_updates_lessons_dataset_and_proposals(self):
        dec_llm = FakeLLM("d", [typed("approve", used=["L-001"])])
        refl = FakeLLM("rf", [{"lessons": ["Vérifier l'IBAN avant d'approuver un fournisseur inconnu."]}])
        eng = engine(self.tmp, decider=dec_llm); eng.models["rf"] = refl; eng.roles["reflector"] = "rf"
        pb = eng.playbook("acme/invoices"); pb.add("Toujours vérifier le fournisseur.", source="manual")
        d = eng.decide(req())
        self.assertIn("[L-001]", dec_llm.calls[0][0]["content"])
        out = eng.outcome({"decision_id": d["decision_id"], "result": "failure",
                           "human_feedback": {"correct_action": "escalate"}})
        self.assertEqual(pb.data["lessons"][0]["harmful"], 1)
        dup = eng.outcome({"decision_id": d["decision_id"], "result": "failure"})
        self.assertEqual((dup["recorded"], dup.get("duplicate"), pb.data["lessons"][0]["harmful"]), (False, True, 1))
        with self.assertRaises(ValueError):
            eng.outcome({"decision_id": d["decision_id"], "result": "failure", "human_feedback": {"correct_action": "nuke"}})
        self.assertEqual(len(out["lesson_proposals"]), 1)
        self.assertEqual(len(pb.data["pending"]), 1)
        rows = (Path(self.tmp) / "acme/invoices/dataset.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(rows[0])["expected_action"], "escalate")
        # redémarrage : le journal est rechargé, l'outcome retrouve la décision
        eng2 = engine(self.tmp, decider=FakeLLM("d", []))
        self.assertTrue(eng2.outcome({"decision_id": d["decision_id"], "result": "success"}).get("duplicate"))  # déjà noté, rechargé du journal
        with self.assertRaises(KeyError):
            eng2.outcome({"decision_id": "d_nope", "result": "success"})

    def test_execute_uses_executor_and_schema(self):
        ex = FakeLLM("x", [{"total": 42}])
        eng = engine(self.tmp, executor=ex)
        out = eng.execute({"namespace": "acme/invoices", "task": "extrais le total", "input": "Total: 42 EUR",
                           "output_schema": {"type": "object", "required": ["total"], "properties": {"total": {"type": "number"}}}})
        self.assertEqual(out["output"], {"total": 42})
        ex.answers = [{"total": "quarante-deux"}]
        with self.assertRaises(LLMError):
            eng.execute({"namespace": "acme/invoices", "task": "t", "output_schema": {"type": "object", "properties": {"total": {"type": "number"}}}})


class TestJev(unittest.TestCase):
    def test_jev_adapter_maps_choice_and_escalates_on_params(self):
        from reason.jev import JevDecider
        seen = {}
        class FakeJev(JevDecider):
            def _ask(self, state, questions):
                seen["q"] = questions
                return {"action": {"type": "choice", "choice": "escalate", "confidence": 0.93,
                                   "probabilities": {"escalate": 0.95, "approve": 0.04, "abstain": 0.01}}}, {"input_tokens": 50}
        tmp = tempfile.mkdtemp()
        eng = Engine({"data_dir": tmp, "models": {}, "roles": {}}); eng.models["j"] = FakeJev(name="j"); eng.roles["decider"] = "j"
        d = eng.decide(req())
        self.assertEqual((d["tier"], d["action"]["name"], d["action"]["params"]), ("decider", "escalate", {}))
        self.assertEqual(list(seen["q"]["action"]["criteria"]), ["approve", "escalate", "abstain", "ask_human"])
        # Jev choisit approve (qui exige limit) : params vides invalides → pas de raisonneur → humain
        FakeJev._ask = lambda self, s, q: ({"action": {"type": "choice", "choice": "approve", "confidence": 0.99, "probabilities": {}}}, {})
        d = eng.decide(req(run_id="r2"))
        self.assertEqual(d["action"]["name"], "ask_human"); self.assertIn("paramètres invalides", d["rationale"])

    def test_make_model_provider(self):
        from reason.core import _make_model
        from reason.jev import JevDecider
        self.assertIsInstance(_make_model("j", {"provider": "jev", "api_key": "k"}), JevDecider)
        self.assertEqual(_make_model("o", {"base_url": "http://x/v1", "model": "m"}).model, "m")


class TestPlaybook(unittest.TestCase):
    def test_dedup_feedback_prune_render(self):
        pb = Playbook(Path(tempfile.mkdtemp()) / "p.json")
        a = pb.add("Attendre 30 secondes quand l'API renvoie 429 avant de réessayer.")
        b = pb.add("Attendre 30 secondes quand l'API renvoie un 429 avant de réessayer")  # doublon
        self.assertEqual(a["id"], b["id"]); self.assertEqual(b["helpful"], 2)
        pb.add("Autre leçon distincte sur les factures.")
        text, ids = pb.render(10_000)
        self.assertEqual(ids[0], a["id"]); self.assertIn("[L-001]", text)
        self.assertEqual(pb.render(10)[1], [])                     # budget trop petit : rien montré
        for _ in range(5):
            pb.feedback([a["id"]], helpful=False)
        self.assertTrue(pb.data["lessons"][0]["archived"]); self.assertNotIn(a["id"], pb.render()[1])
        ids = pb.propose(["Nouvelle leçon proposée.", "Nouvelle leçon proposée !"], source="d_1")
        self.assertEqual(len(ids), 1)
        self.assertTrue(pb.approve(ids[0])); self.assertFalse(pb.reject(ids[0]))
        self.assertIn(ids[0], pb.render()[1])
        # négation : pas un doublon ; id jamais réattribué après rejet ; rechargement si un autre processus écrit le fichier
        n = pb.add("Approuver les factures de ACME.")
        self.assertNotEqual(pb.add("Ne pas approuver les factures de ACME.")["id"], n["id"])
        rid = pb.propose(["Leçon à rejeter."], source="x")[0]; pb.reject(rid)
        self.assertNotEqual(pb.propose(["Leçon suivante."], source="x")[0], rid)
        other = Playbook(pb.path); other.add("Leçon ajoutée par la CLI pendant que le serveur tourne.")
        self.assertTrue(any("CLI" in l["text"] for l in pb.active()))


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dec = FakeLLM("d", [typed("approve") for _ in range(20)])
        server.Handler.engine = engine(cls.tmp, decider=cls.dec)
        server.Handler.token = "secret"
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, body=None, token="secret"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse(); data = r.read()
        return r.status, (json.loads(data) if data else None)

    def test_health_decide_outcome_auth(self):
        self.assertEqual(self.call("GET", "/health", token=None)[0], 200)
        self.assertEqual(self.call("POST", "/decide", req(), token=None)[0], 401)
        st, d = self.call("POST", "/decide", req(run_id="srv-1"))
        self.assertEqual((st, d["action"]["name"]), (200, "approve"))
        st, o = self.call("POST", "/outcome", {"decision_id": d["decision_id"], "result": "success"})
        self.assertEqual((st, o["recorded"]), (200, True))
        self.assertEqual(self.call("POST", "/decide", {"namespace": "x"})[0], 400)
        self.assertEqual(self.call("GET", "/lessons/acme/invoices")[0], 200)

    def test_mcp_initialize_list_call(self):
        st, r = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "reason")
        self.assertEqual(self.call("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        st, r = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual([t["name"] for t in r["result"]["tools"]], ["decide", "outcome", "execute"])
        st, r = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                            "params": {"name": "decide", "arguments": req(run_id="mcp-1")}})
        self.assertFalse(r["result"]["isError"]); self.assertEqual(r["result"]["structuredContent"]["action"]["name"], "approve")
        st, r = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}})
        self.assertIn("error", r)
        self.assertEqual(self.call("GET", "/mcp")[0], 405)
        self.assertEqual(self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 5})[1]["error"]["code"], -32600)
        self.assertEqual(self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": 3})[1]["error"]["code"], -32602)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/mcp", "{}", {"Authorization": "Bearer secret", "Origin": "https://evil.example"})
        self.assertEqual(c.getresponse().status, 403)

    def test_claude_hook_gate_and_context(self):
        # le faux décideur répond "allow" (dans l'enum du hook) : mappé sur permissionDecision allow
        self.dec.answers[:0] = [typed("allow", params={}) for _ in range(6)]
        hook = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/tmp/My Projet é", "tool_name": "Bash",
                "tool_input": {"command": "ls"}, "tool_use_id": "t1"}
        st, r = self.call("POST", "/hooks/claude", hook)
        self.assertEqual((st, r["hookSpecificOutput"]["permissionDecision"]), (200, "allow"))
        # 5 appels différents dans la même session ne déclenchent pas l'anti-boucle (empreinte de l'entrée dans le point)
        for i in range(5):
            st, r = self.call("POST", "/hooks/claude", {**hook, "tool_input": {"command": f"ls {i}"}, "tool_use_id": f"t{i+2}"})
            self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "allow", i)
        self.assertEqual(server.hook_namespace("/tmp/My Projet é"), "My_Projet__")
        self.assertEqual(server.hook_namespace("/"), "default")
        server.Handler.engine.playbook("claude/My_Projet__").add("Ne jamais lancer rm -rf sans confirmation.")
        st, r = self.call("POST", "/hooks/claude", {"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/My Projet é"})
        self.assertIn("rm -rf", r["hookSpecificOutput"]["additionalContext"])


class TestLLMClient(unittest.TestCase):
    """Le client urllib contre un faux fournisseur OpenAI local : 429 puis 200, contenu vide, troncature, json_object."""
    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        cls.seen, cls.script = [], []
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.seen.append((self.headers.get("Authorization"), body))
                code, payload = cls.script.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    @staticmethod
    def ok(content, finish="stop"):
        return 200, {"choices": [{"message": {"content": content}, "finish_reason": finish}], "usage": {"total_tokens": 3}}

    def test_retry_json_modes_and_errors(self):
        from reason.llm import LLM
        llm = LLM(self.url, "m", api_key="k", json_schema=False, temperature=None, max_tokens_key="max_completion_tokens",
                  extra={"reasoning": {"effort": "low"}})
        self.script[:] = [(429, {"error": "slow down"}), self.ok('{"a": 1}')]
        out, usage = llm.chat([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}], schema={"type": "object"})
        self.assertEqual(out, {"a": 1})
        auth, body = self.seen[-1]
        self.assertEqual(auth, "Bearer k"); self.assertNotIn("temperature", body); self.assertIn("max_completion_tokens", body)
        self.assertEqual(body["response_format"], {"type": "json_object"}); self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["messages"][0]["role"], "system"); self.assertIn("schéma JSON", body["messages"][0]["content"])
        self.assertEqual(len(body["messages"]), 2)
        strict = LLM(self.url, "m", json_schema=True)
        self.script[:] = [self.ok('```json\n{"b": 2}\n```')]
        self.assertEqual(strict.chat([{"role": "user", "content": "U"}], schema={"type": "object"})[0], {"b": 2})
        self.assertEqual(self.seen[-1][1]["response_format"]["json_schema"]["strict"], True)
        self.script[:] = [self.ok("", "stop"), self.ok("", "stop")]
        with self.assertRaisesRegex(LLMError, "contenu vide"):
            strict.chat([{"role": "user", "content": "U"}], schema={"type": "object"})
        self.script[:] = [self.ok('{"c":', "length")]
        with self.assertRaisesRegex(LLMError, "tronquée"):
            strict.chat([{"role": "user", "content": "U"}], schema={"type": "object"})
        self.script[:] = [(400, {"error": "bad"})]
        with self.assertRaisesRegex(LLMError, "HTTP 400"):
            strict.chat([{"role": "user", "content": "U"}])
        self.assertEqual(len(self.script), 0)   # un 400 n'est pas retenté
        with self.assertRaises(ValueError):
            LLM.from_config("x", {"base_url": self.url, "model": "m", "temprature": 0})


class TestCheckDiff(unittest.TestCase):
    def test_detects_test_tampering(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "check_diff.py"
        diff = ("diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
                "@@ -1 +1 @@\n-    assert x == 1\n+    pass  # pytest.mark.skip\n"
                "diff --git a/src/ok.py b/src/ok.py\n--- a/src/ok.py\n+++ b/src/ok.py\n@@ -1 +1 @@\n-a=1\n+a=2\n")
        p = subprocess.run([sys.executable, str(script)], input=diff, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1); self.assertIn("tests/test_x.py", p.stdout)
        clean = "diff --git a/src/ok.py b/src/ok.py\n--- a/src/ok.py\n+++ b/src/ok.py\n@@ -1 +1 @@\n-a=1\n+a=2\n"
        p = subprocess.run([sys.executable, str(script)], input=clean, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
