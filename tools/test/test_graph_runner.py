"""tools/malrunar_graph.py with a fake docker and a fake ssh that keep a local and a remote store."""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeDockerCase

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "scripts"))
import malrunar_graph as mg  # noqa: E402
import malrunar_image as mi  # noqa: E402

MANIFEST = REPO / "tools/example/graph.toml"


class RunnerTests(FakeDockerCase):
    def setUp(self):
        super().setUp()
        self.graph = mi.Graph(MANIFEST)
        self.ref = self.graph.ref

    def run_main(self, *args):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env(), clear=True), contextlib.redirect_stdout(out):
            mg.main([str(MANIFEST), *args])
        return out.getvalue()

    def builds(self):
        """References of the nodes built (the assembly candidate is not a node)."""
        found = re.findall(r"^docker build .*-t (\S+) ", self.logged(), flags=re.M)
        return [ref for ref in found if not ref.startswith("malrunar-assemble:")]

    def test_print_plan_needs_no_docker(self):
        out = self.run_main("--print-plan")
        self.assertEqual(json.loads(out)["gamma"]["ref"], self.ref("gamma"))
        self.assertEqual(self.logged(), "")

    def test_local_mode_builds_everything_parents_first_then_assembles_and_checks(self):
        self.run_main("--jobs", "1")
        built = self.builds()
        self.assertEqual(sorted(built), sorted(self.ref(n) for n in self.graph.nodes))
        for name in ("base", "alpha", "beta", "gamma"):
            self.assertIn(self.ref(name), built)
        self.assertLess(built.index(self.ref("base")), built.index(self.ref("gamma")))
        log = self.logged()
        self.assertIn("--target c-gamma", log)
        self.assertIn(f"--build-arg RUNTIME_BASE_IMAGE={self.ref('base')}", log)
        self.assertIn("example/assembled:latest", self.lines(self.store))
        self.assertIn("docker run --rm -i --user root --entrypoint bash", log)
        self.assertEqual(self.lines(self.remote_store), [], "nothing is sent in local mode")

    def test_a_second_local_run_builds_nothing(self):
        self.run_main("--jobs", "1")
        self.log.write_text("")
        self.run_main("--jobs", "1", "--components-only")
        self.assertEqual(self.builds(), [])

    def test_remote_mode_builds_and_sends_only_what_the_remote_lacks(self):
        self.remote_store.write_text(f"{self.ref('alpha')}\n{self.ref('beta')}\n")
        summary = Path(self.tmp.name) / "summary.json"
        self.run_main("--remote", "user@host", "--method", "save-load", "--jobs", "1", "--summary", str(summary))
        self.assertEqual(sorted(self.builds()), sorted([self.ref("base"), self.ref("gamma")]))
        remote = self.lines(self.remote_store)
        for name in ("base", "gamma"):
            self.assertIn(self.ref(name), remote, f"{name} was sent")
        self.assertIn("example/assembled:latest", remote, "assembled and tagged on the remote")
        self.assertNotIn("example/assembled:latest", self.lines(self.store), "not assembled here")
        report = json.loads(summary.read_text())
        self.assertEqual(report["remote_held"], ["alpha", "beta"])
        self.assertEqual([c["name"] for c in report["components"] if c.get("shipped")], ["base", "gamma"])
        self.assertEqual(report["final"], {"tags": ["example/assembled:latest"], "location": "user@host"})

    def test_remote_holding_everything_sends_and_builds_nothing_but_still_assembles(self):
        self.remote_store.write_text("".join(self.ref(n) + "\n" for n in self.graph.nodes))
        self.run_main("--remote", "user@host", "--method", "save-load")
        self.assertEqual(self.builds(), [])
        self.assertNotIn("docker save", self.logged())
        self.assertIn("example/assembled:latest", self.lines(self.remote_store))

    def test_a_parent_the_remote_holds_is_copied_not_rebuilt(self):
        self.remote_store.write_text("".join(self.ref(n) + "\n" for n in ("base", "alpha", "beta")))
        self.run_main("--remote", "user@host", "--method", "save-load")
        self.assertEqual(self.builds(), [self.ref("gamma")])
        self.assertIn(self.ref("base"), self.lines(self.store), "copied from the remote to build gamma on it")

    def test_a_failing_build_stops_the_run(self):
        env = self.env(FAKE_BUILD_FAILS="1")
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                mg.main([str(MANIFEST), "--jobs", "1"])
        self.assertIn("build failed", str(raised.exception))
        self.assertNotIn("example/assembled:latest", self.lines(self.store))

    def test_explicit_tags_replace_the_manifest_tag(self):
        self.run_main("--tag", "mine/final:1", "--tag", "mine/final:2")
        stored = self.lines(self.store)
        self.assertIn("mine/final:1", stored)
        self.assertIn("mine/final:2", stored)
        self.assertNotIn("example/assembled:latest", stored)


if __name__ == "__main__":
    unittest.main()
