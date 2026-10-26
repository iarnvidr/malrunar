"""Synthetic contracts: no server, provider, credential or daemon is contacted."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from malrunar_devops import Backend, BoardError, DiskCache, create_server, validate
from malrunar_devops.run import load_backend, read_token


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = DiskCache(self.tmp.name, "synthetic-account")
        self.calls = []
        self.topics = {"topics": [{"id": "public", "workItemIds": [42],
                                   "url": "https://example.invalid/h/host/workspace/work"}]}
        self.backend = self.make_backend()

    def make_backend(self, **overrides):
        callbacks = dict(runner=lambda a, p, c: self.calls.append((a, p)) or {"id": 42},
                         cache_factory=lambda: self.cache, topic_reader=lambda: self.topics,
                         paseo_runner=lambda a, p: self.calls.append((a, p)) or {"ok": True},
                         linked_reader=lambda ids: {"items": ids}, authorize=lambda a, p: True)
        return Backend(**{**callbacks, **overrides})

    def test_absent_policy_or_provider_cannot_construct(self):
        with self.assertRaises(ValueError):
            Backend()
        for name in ("authorize", "runner", "cache_factory", "topic_reader", "paseo_runner", "linked_reader"):
            with self.subTest(callback=name), self.assertRaises(ValueError):
                self.make_backend(**{name: None})

    def test_denied_policy_precedes_provider_cache_and_topic_reads(self):
        def forbidden(*args):
            self.fail("Denied policy invoked a provider or state callback")
        backend = self.make_backend(authorize=lambda a, p: False, runner=forbidden,
                                    cache_factory=forbidden, topic_reader=forbidden,
                                    paseo_runner=forbidden, linked_reader=forbidden)
        for action, payload in (("topics", {}), ("status", {}), ("item", {"id": 42}),
                                ("paseo-overview", {}), ("linked-report", {"ids": [42]})):
            with self.subTest(action=action), self.assertRaises(BoardError):
                backend.execute(action, payload)

    def test_truthy_policy_is_not_explicit_authorization(self):
        with self.assertRaises(BoardError):
            self.make_backend(authorize=lambda a, p: "yes").execute("status", {})

    def test_unknown_action_and_fields_never_reach_policy(self):
        def forbidden(*args):
            self.fail("Invalid schema reached the policy")
        backend = self.make_backend(authorize=forbidden)
        for action, payload in (("shell", {}), ("item", {"id": 42, "command": "ignored"})):
            with self.assertRaises(ValueError):
                backend.execute(action, payload)

    def test_create_replay_is_account_local_and_does_not_repeat_write(self):
        payload = {"type": "Task", "title": "Reviewed", "description": ""}
        result = self.backend.execute("create", payload, "operation-one")
        self.assertEqual(self.backend.execute("create", payload, "operation-one"), result)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(BoardError):
            self.backend.execute("create", {**payload, "title": "Changed"}, "operation-one")
        other = DiskCache(self.tmp.name, "other-account")
        self.make_backend(cache_factory=lambda: other).execute("create", payload, "operation-one")
        self.assertEqual(len(self.calls), 2)

    def test_uncertain_write_remains_pending_and_cannot_repeat(self):
        def uncertain(*args):
            raise RuntimeError("synthetic uncertain write")
        backend = self.make_backend(runner=uncertain)
        payload = {"type": "Task", "title": "Reviewed", "description": ""}
        with self.assertRaises(RuntimeError):
            backend.execute("create", payload, "operation-two")
        backend.runner = lambda *args: self.fail("An uncertain write was repeated")
        with self.assertRaises(BoardError):
            backend.execute("create", payload, "operation-two")

    def test_revisions_etags_and_paseo_operation_ids_reach_provider_unchanged(self):
        transition = {"id": 42, "revision": 7, "state": "Active"}
        self.backend.execute("transition", transition)
        self.assertEqual(self.calls[-1], ("transition", transition))
        wiki = {"wikiId": "11111111-1111-1111-1111-111111111111", "path": "/Reviewed",
                "content": "Reviewed", "etag": '"7"'}
        self.backend.execute("wiki-publish", wiki, "operation-wiki")
        self.assertEqual(self.calls[-1], ("wiki-publish", wiki))
        topic = {"topicId": "public", "expectedRevision": "a" * 64,
                 "type": "Task", "title": "Reviewed", "description": ""}
        self.backend.execute("paseo-create", topic, "operation-topic")
        self.assertEqual(self.calls[-1], ("paseo-create", {**topic, "operationId": "operation-topic"}))
        with self.assertRaises(ValueError):
            self.backend.execute("paseo-create", topic)

    def test_backlink_requires_membership_in_consumer_public_topics(self):
        for topic, ident in (("private", 42), ("public", 99)):
            with self.assertRaises(ValueError):
                self.backend.execute("backlink", {"topicId": topic, "id": ident})
        self.assertEqual(self.calls, [])
        self.backend.execute("backlink", {"topicId": "public", "id": 42})
        self.assertEqual(self.calls[0], ("ensure-link", {"id": 42, "url": self.topics["topics"][0]["url"]}))

    def test_validation_retains_revision_and_scope_constraints(self):
        self.assertEqual(validate("board", {"scope": "mine", "linkedIds": [42]}), {"scope": "mine"})
        for action, payload in (("item", {"id": True}), ("transition", {"id": 42, "revision": 0, "state": "Active"}),
                                ("linked-report", {"ids": [-1]})):
            with self.assertRaises(ValueError):
                validate(action, payload)

    def test_http_bearer_boundary_without_starting_server(self):
        token = "x" * 48
        with patch("malrunar_devops.service.ThreadingHTTPServer") as factory:
            create_server(("127.0.0.1", 0), token, self.backend)
            handler_type = factory.call_args.args[1]
        for auth, expected in (("", 401), ("Bearer " + token + "extra", 401), ("Bearer " + token, 200)):
            handler = object.__new__(handler_type)
            body = json.dumps({"action": "status"}).encode()
            handler.headers = {"Authorization": auth, "Content-Length": str(len(body))}
            handler.path, handler.rfile = "/rpc", io.BytesIO(body)
            replies = []
            handler.reply = lambda status, value: replies.append((status, value))
            handler.do_POST()
            self.assertEqual(replies[0][0], expected)
        with self.assertRaises(ValueError):
            create_server(("127.0.0.1", 0), "short", self.backend)

    def test_read_failure_retains_snapshot_but_invalidates_freshness(self):
        clock = [1000]
        cache = DiskCache(self.tmp.name, "cache-test", clock=lambda: clock[0])
        self.assertFalse(cache.fetch("key", lambda: {"items": [42]})["cache"]["stale"])
        clock[0] += 70
        def unavailable():
            raise RuntimeError("provider unavailable")
        stale = cache.fetch("key", unavailable)
        self.assertEqual(stale["items"], [42])
        self.assertTrue(stale["cache"]["stale"])
        cache.invalidate()
        self.assertEqual(cache.fetch("key", lambda: {"items": [43]})["items"], [43])

    def test_no_token_or_adapter_default_and_token_file_is_owner_only(self):
        with self.assertRaises(ValueError):
            load_backend(None)
        with self.assertRaises(ValueError):
            read_token(None)
        path = Path(self.tmp.name) / "synthetic-token"
        path.write_text("t" * 48)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_token(str(path))
        path.chmod(0o600)
        self.assertEqual(read_token(str(path)), "t" * 48)


if __name__ == "__main__":
    unittest.main()
