"""Synthetic provider contracts: injected sessions only; no SDK login or network."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from malrunar_devops.board import AzureBoards, dispatch
from malrunar_devops.errors import BoardError
from malrunar_devops.linked import read_items
from malrunar_devops.policy import AzurePolicy
from malrunar_devops.wiki import AzureWiki, dispatch_wiki, page_path

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
REPOSITORY_ID = "22222222-2222-2222-2222-222222222222"


def policy(authorize=lambda operation, resource: True, **changes):
    return AzurePolicy(**{**dict(organization="https://dev.azure.com/example", project="Synthetic",
                                project_id=PROJECT_ID, authorize=authorize), **changes})


def item(revision=7, project="Synthetic", **fields):
    return {"id": 42, "rev": revision, "fields": {"System.TeamProject": project, "System.Title": "Synthetic",
            "System.State": "Active", "System.WorkItemType": "Task", **fields}}


class Response:
    def __init__(self, value=None, status=200, headers=None, chunks=()):
        self.value, self.status_code = value or {}, status
        self.ok = 200 <= status < 300
        self.headers, self.chunks = headers or {}, chunks

    def json(self):
        return self.value

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("Synthetic HTTP failure")

    def iter_content(self, size):
        return iter(self.chunks)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Session:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda method, url, kw: Response())

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class PolicyTests(unittest.TestCase):
    def test_missing_policy_and_auth_callback_fail_before_credentials(self):
        token = Mock(side_effect=AssertionError("credential callback must not execute"))
        with self.assertRaises(ValueError):
            AzureBoards(token_provider=token, session=Session())
        with self.assertRaises(ValueError):
            policy(authorize=None)
        with self.assertRaises(BoardError):
            AzureBoards(policy(lambda a, p: False), token_provider=token, session=Session())
        token.assert_not_called()
        with self.assertRaises(ValueError):
            dispatch("item", {"id": 42}, None)
        with self.assertRaises(ValueError):
            dispatch_wiki("wiki-list", {}, None)
        with self.assertRaises(ValueError):
            read_items([42])

    def test_unknown_and_truthy_policies_fail_closed(self):
        for configured in (policy(), policy(lambda a, p: "allow")):
            with self.assertRaises(BoardError):
                configured.require("future-operation", {})
        with self.assertRaises(BoardError):
            policy(lambda a, p: "allow").require("connect", {})

    def test_scope_cannot_be_an_implicit_or_credential_bearing_endpoint(self):
        credential_url = "https://{credentials}@{host}/example".format(credentials="user:pass", host="dev.azure.com")
        for organization in ("", "http://dev.azure.com/example", credential_url,
                             "https://example.invalid/example", "https://dev.azure.com/example?token=synthetic"):
            with self.subTest(organization=organization), self.assertRaises(ValueError):
                policy(organization=organization)
        with self.assertRaises(ValueError):
            policy(project="")
        with self.assertRaises(ValueError):
            policy(project_id="")

    def test_routes_remain_project_bound_and_cannot_redirect_credentials(self):
        session = Session(lambda *args: Response(status=302))
        client = AzureBoards(policy(), token_provider=lambda: "synthetic-token", session=session)
        for path in ("../outside", "%2e%2e/outside", "https://example.invalid/", "//example.invalid/", "workitems?secret=x", "a\\b"):
            with self.assertRaises(BoardError):
                client.request("GET", path)
        self.assertEqual(session.calls, [])
        with self.assertRaises(BoardError):
            client.request("GET", "workitems/42")
        self.assertFalse(session.calls[-1][2]["allow_redirects"])
        self.assertTrue(session.calls[-1][1].startswith(client.policy.web_base + "/_apis/wit/"))

    def test_request_denial_and_http_errors_never_return_response_body(self):
        session = Session(lambda *args: Response({"error": "synthetic confidential detail"}, status=403))
        client = AzureBoards(policy(), token_provider=lambda: "synthetic-token", session=session)
        with self.assertRaises(BoardError) as denied:
            client.request("GET", "workitems/42")
        self.assertNotIn("confidential", str(denied.exception))
        blocked = AzureBoards(policy(lambda a, p: a == "connect"), token_provider=lambda: "synthetic-token", session=session)
        before = len(session.calls)
        with self.assertRaises(BoardError):
            blocked.request("GET", "workitems/42")
        self.assertEqual(len(session.calls), before)


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.client = AzureBoards(policy(), token_provider=lambda: "synthetic-token", session=Session())
        self.client.item = Mock(return_value={"id": 42, "revision": 7, "type": "Task"})
        self.client.request = Mock(side_effect=lambda method, path, *a, **kw:
            {"value": [{"name": "Active"}, {"name": "Complete"}]} if method == "GET" else item(revision=8))

    def test_transition_checks_revision_and_patches_only_state(self):
        result = self.client.transition({"id": 42, "revision": 7, "state": "Complete"})
        self.assertEqual(result["revision"], 8)
        args, kwargs = self.client.request.call_args
        self.assertEqual(args[:2], ("PATCH", "workitems/42"))
        self.assertEqual(args[2], [{"op": "test", "path": "/rev", "value": 7},
                                  {"op": "add", "path": "/fields/System.State", "value": "Complete"}])
        self.assertTrue(kwargs["patch"])

    def test_stale_revision_or_unknown_state_never_writes(self):
        for payload in ({"id": 42, "revision": 6, "state": "Complete"}, {"id": 42, "revision": 7, "state": "Unknown"}):
            self.client.request.reset_mock()
            with self.assertRaises(BoardError):
                self.client.transition(payload)
            self.assertFalse(any(call.args[0] == "PATCH" for call in self.client.request.call_args_list))

    def test_create_escapes_reviewed_plain_text(self):
        self.client.types = lambda: [{"name": "Task"}]
        self.client.create({"type": "Task", "title": "Reviewed", "description": "<script>\n&"})
        body = self.client.request.call_args.args[2]
        self.assertEqual(body[1]["value"], "&lt;script&gt;<br>&amp;")

    def test_foreign_project_cannot_be_normalized(self):
        with self.assertRaises(BoardError):
            self.client.normalize(item(project="Other"))

    def test_open_query_uses_actual_completed_states_and_project_scope(self):
        self.client.types = lambda: [{"name": "Task", "states": [{"name": "Custom Finished", "category": "Completed"}]}]
        self.client.request = Mock(return_value={"workItems": []})
        self.client.board({"scope": "all-open"})
        query = self.client.request.call_args.args[2]["query"]
        self.assertIn("[System.TeamProject] = @project", query)
        self.assertIn("Custom Finished", query)

    def test_backlink_is_revision_guarded_idempotent_and_uses_policy_comment(self):
        url = "https://example.invalid/h/host/workspace/work"
        self.client.request = Mock(side_effect=[item(), item(revision=8)])
        self.assertTrue(self.client.ensure_link({"id": 42, "url": url})["added"])
        body = self.client.request.call_args.args[2]
        self.assertEqual(body[0], {"op": "test", "path": "/rev", "value": 7})
        self.assertEqual(body[1]["value"]["attributes"]["comment"], self.client.policy.link_comment)
        self.client.request = Mock(return_value={**item(), "relations": [{"rel": "Hyperlink", "url": url}]})
        self.assertFalse(self.client.ensure_link({"id": 42, "url": url})["added"])
        self.assertEqual(self.client.request.call_count, 1)

    def test_backlink_rejects_credentials_nonworkspace_and_fragments(self):
        for url in ("https://user:pass@example.invalid/h/host/workspace/work", "https://example.invalid/other",
                    "https://example.invalid/h/host/workspace/work?token=synthetic"):
            with self.assertRaises(BoardError):
                self.client.ensure_link({"id": 42, "url": url})
        self.client.request.assert_not_called()

    def test_confirmed_write_survives_cache_invalidation_failure(self):
        cache = Mock()
        cache.invalidate.side_effect = OSError("synthetic failure")
        client = Mock()
        client.create.return_value = {"id": 42}
        result = dispatch("create", {}, cache, factory=lambda: client)
        self.assertEqual(result["id"], 42)
        self.assertIn("warning", result)


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.client = AzureWiki(policy(), token_provider=lambda: "synthetic-token", session=Session())
        self.wiki = {"id": PROJECT_ID, "type": "projectWiki", "url": "https://example.invalid/wiki"}
        self.client.wikis = lambda: {"wikis": [self.wiki]}
        self.payload = {"wikiId": PROJECT_ID, "path": "/Reviewed", "content": "# Reviewed", "etag": '"7"'}

    def test_edit_uses_live_etag_and_only_reviewed_markdown(self):
        self.client.wiki_request = Mock(side_effect=[{"data": {}, "etag": '"7"'}, {"data": {}, "etag": '"8"'}])
        result = self.client.publish(self.payload)
        call = self.client.wiki_request.call_args
        self.assertEqual(call.args[0], "PUT")
        self.assertEqual(call.args[2], {"content": "# Reviewed"})
        self.assertEqual(call.kwargs["etag"], '"7"')
        self.assertEqual(result["etag"], '"8"')

    def test_stale_missing_or_deleted_etag_never_writes(self):
        for current, etag in (({"data": {}, "etag": '"8"'}, '"7"'), ({"data": {}, "etag": '"7"'}, ""), (None, '"7"')):
            self.client.wiki_request = Mock(return_value=current)
            with self.assertRaises(BoardError):
                self.client.publish({**self.payload, "etag": etag})
            self.assertEqual(self.client.wiki_request.call_count, 1)

    def test_new_page_checks_absence_before_creation(self):
        self.client.wiki_request = Mock(side_effect=[None, {"data": {}, "etag": '"1"'}])
        self.client.publish({**self.payload, "etag": ""})
        self.assertTrue(self.client.wiki_request.call_args_list[0].kwargs["missing"])
        self.assertEqual(self.client.wiki_request.call_args.kwargs["params"]["comment"], self.client.policy.wiki_create_comment)

    def test_foreign_wiki_is_rejected_before_any_page_request(self):
        self.client.wiki_request = Mock()
        with self.assertRaises(BoardError):
            self.client.page({**self.payload, "wikiId": REPOSITORY_ID})
        self.client.wiki_request.assert_not_called()

    def test_list_filters_project_before_exposing_wiki_metadata(self):
        del self.client.wikis
        self.client.wiki_request = Mock(return_value={"data": {"value": [
            {**self.wiki, "name": "Allowed", "projectId": PROJECT_ID},
            {**self.wiki, "id": REPOSITORY_ID, "name": "Hidden", "projectId": REPOSITORY_ID}]}})
        self.assertEqual([w["name"] for w in self.client.wikis()["wikis"]], ["Allowed"])

    def test_wiki_transport_forwards_if_match_and_rejects_conflicts(self):
        session = Session(lambda *a: Response({"content": "Synthetic"}, headers={"ETag": '"8"'}))
        client = AzureWiki(policy(), token_provider=lambda: "synthetic-token", session=session)
        client.wiki_request("PUT", "wikis/synthetic/pages", {"content": "Reviewed"}, etag='"7"')
        self.assertEqual(session.calls[-1][2]["headers"]["If-Match"], '"7"')
        session.handler = lambda *args: Response(status=412)
        with self.assertRaises(BoardError):
            client.wiki_request("PUT", "wikis/synthetic/pages")

    def test_tree_preserves_wiki_order_and_hierarchy(self):
        self.client.wiki_request = Mock(return_value={"data": {"path": "/", "subPages": [
            {"path": "/Second", "order": 2}, {"path": "/First", "order": 1, "subPages": [{"path": "/First/Child"}]}]}})
        self.assertEqual([(p["path"], p["depth"]) for p in self.client.tree(self.payload)["pages"]],
                         [("/First", 0), ("/First/Child", 1), ("/Second", 0)])

    def test_wiki_paths_and_attachments_cannot_escape(self):
        for path in ("relative", "/../outside", "/a/./b", "/a\\b", "/a\n"):
            with self.assertRaises(BoardError):
                page_path(path)
        self.client.wiki_request = Mock()
        with self.assertRaises(BoardError):
            self.client.attachment({"wikiId": PROJECT_ID, "name": "../outside"})
        self.client.wiki_request.assert_not_called()

    def test_attachment_is_bound_to_validated_project_and_repository(self):
        metadata = {"repositoryId": REPOSITORY_ID, "projectId": PROJECT_ID, "versions": [{"version": "main"}]}
        self.client.wiki_request = Mock(return_value={"data": metadata})
        self.client.requests.handler = lambda *args: Response(chunks=[b"synthetic"])
        result = self.client.attachment({"wikiId": PROJECT_ID, "name": "synthetic.png"})
        self.assertEqual(result["contentType"], "image/png")
        self.assertIn("/repositories/" + REPOSITORY_ID + "/items", self.client.requests.calls[-1][1])
        self.assertEqual(self.client.requests.calls[-1][2]["params"]["path"], "/.attachments/synthetic.png")
        self.client.wiki_request = Mock(return_value={"data": {**metadata, "projectId": REPOSITORY_ID}})
        previous = len(self.client.requests.calls)
        with self.assertRaises(BoardError):
            self.client.attachment({"wikiId": PROJECT_ID, "name": "synthetic.png"})
        self.assertEqual(len(self.client.requests.calls), previous)


class LinkedTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy()
        self.session = Session(self.response)
        self.client = AzureBoards(self.policy, token_provider=lambda: "synthetic-token", session=self.session)
        self.foreign_pr = False

    def response(self, method, url, kwargs):
        if url.endswith("/workitems"):
            return Response({"value": [{**item(), "relations": [{"url": "vstfs:///Git/PullRequestId/synthetic/12"}]}]})
        return Response({"title": "Hidden outside title" if self.foreign_pr else "Synthetic PR", "status": "active",
                         "repository": {"id": REPOSITORY_ID, "project": {"id": REPOSITORY_ID if self.foreign_pr else PROJECT_ID}}})

    def test_reads_exact_ids_and_only_project_bound_pr_metadata(self):
        result = read_items([42], policy=self.policy, factory=lambda: self.client)
        self.assertEqual(result["items"][0]["id"], 42)
        self.assertEqual(result["pullRequests"][0]["title"], "Synthetic PR")
        self.assertEqual(self.session.calls[0][2]["params"]["ids"], "42")
        self.assertTrue(all(not c[2]["allow_redirects"] for c in self.session.calls))

    def test_foreign_pr_metadata_never_escapes_as_a_linked_result(self):
        self.foreign_pr = True
        result = read_items([42], policy=self.policy, factory=lambda: self.client)
        self.assertEqual(result["pullRequests"][0]["status"], "unavailable")
        self.assertNotIn("Hidden outside title", json.dumps(result))
        self.assertTrue(result["warnings"])

    def test_invalid_ids_and_factory_scope_mismatch_fail_before_read(self):
        for ids in ([True], [-1], list(range(1, 102))):
            with self.assertRaises(ValueError):
                read_items(ids, policy=self.policy, factory=lambda: self.client)
        other = AzureBoards(policy(project="Other"), token_provider=lambda: "synthetic-token", session=self.session)
        with self.assertRaises(BoardError):
            read_items([42], policy=self.policy, factory=lambda: other)
        self.assertEqual(self.session.calls, [])

    def test_policy_denial_precedes_factory_and_topic_data_is_never_an_input(self):
        factory = Mock(side_effect=AssertionError("factory must not execute"))
        with self.assertRaises(BoardError):
            read_items([42], policy=policy(lambda a, p: False), factory=factory)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
