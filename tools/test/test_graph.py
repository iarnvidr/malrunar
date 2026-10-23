"""Graph-mode hashing of scripts/malrunar_image.py on the example manifest (standard library only)."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import malrunar_image as mi  # noqa: E402

EXAMPLE = REPO / "tools/example"


class GraphCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "example"
        shutil.copytree(EXAMPLE, self.root)

    def hashes(self, root=None):
        graph = mi.Graph((root or self.root) / "graph.toml")
        return {name: graph.hash(name) for name in graph.nodes}

    def changed(self, before):
        after = self.hashes()
        return {name for name in after if after[name] != before[name]}

    def edit(self, relative, old, new):
        path = self.root / relative
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))


class HashTests(GraphCase):
    def test_hashes_are_stable_and_independent_of_the_checkout_location(self):
        other = self.tmp / "elsewhere"
        shutil.copytree(EXAMPLE, other)
        self.assertEqual(self.hashes(), self.hashes())
        self.assertEqual(self.hashes(), self.hashes(other))
        for value in self.hashes().values():
            self.assertRegex(value, r"^[0-9a-f]{16}$")

    def test_a_component_file_changes_only_that_component(self):
        before = self.hashes()
        (self.root / "components/alpha/README").write_text("changed\n")
        self.assertEqual(self.changed(before), {"alpha"})

    def test_an_exec_bit_changes_the_hash(self):
        before = self.hashes()
        (self.root / "components/beta/bin/beta").chmod(0o644)
        self.assertEqual(self.changed(before), {"beta"})

    def test_a_new_file_in_a_copied_directory_changes_the_hash(self):
        before = self.hashes()
        (self.root / "components/alpha/extra").write_text("x")
        self.assertEqual(self.changed(before), {"alpha"})

    def test_the_hash_chains_through_the_parent(self):
        before = self.hashes()
        self.edit("Dockerfile", 'echo "example base"', 'echo "example base v2"')
        self.assertEqual(self.changed(before), {"base", "gamma"}, "gamma builds FROM base, alpha and beta do not")

    def test_a_pin_changes_only_the_nodes_that_reference_it(self):
        before = self.hashes()
        self.edit("Dockerfile", "ubuntu:26.04@sha256:c098", "ubuntu:26.04@sha256:c099")
        self.assertEqual(self.changed(before), {"base", "gamma"})

    def test_a_manifest_build_arg_overrides_the_default_and_is_hashed(self):
        before = self.hashes()
        manifest = self.root / "graph.toml"
        manifest.write_text(manifest.read_text().replace('[node.base]', '[build_args]\nEXAMPLE_UBUNTU = "ubuntu:26.04@sha256:' + "0" * 64 + '"\n\n[node.base]'))
        self.assertEqual(self.changed(before), {"base", "gamma"})

    def test_epoch_changes_everything(self):
        before = self.hashes()
        manifest = self.root / "graph.toml"
        manifest.write_text(manifest.read_text().replace('epoch = "1"', 'epoch = "2"'))
        self.assertEqual(self.changed(before), set(before))

    def test_unrelated_files_do_not_change_anything(self):
        before = self.hashes()
        (self.root / "unrelated.txt").write_text("not copied by any stage")
        (self.root / "components/alpha/__pycache__").mkdir()
        (self.root / "components/alpha/__pycache__/x.pyc").write_text("junk")
        self.assertEqual(self.changed(before), set())

    def test_comments_and_continuations_do_not_matter_but_instructions_do(self):
        before = self.hashes()
        self.edit("Dockerfile", "FROM scratch AS c-alpha\n", "# a comment\nFROM scratch AS c-alpha\n")
        self.assertEqual(self.changed(before), set())

    def test_a_stage_a_target_does_not_need_does_not_change_it(self):
        before = self.hashes()
        with (self.root / "Dockerfile").open("a") as handle:
            handle.write("\nFROM scratch AS c-unused\nCOPY src/gamma.sh /x\n")
        self.assertEqual(self.changed(before), set())

    def test_a_missing_copy_source_is_an_error(self):
        shutil.rmtree(self.root / "components/alpha")
        with self.assertRaises(SystemExit) as raised:
            self.hashes()
        self.assertIn("COPY source not found", str(raised.exception))


class ManifestTests(GraphCase):
    def write(self, text):
        (self.root / "graph.toml").write_text(text)

    def test_order_puts_parents_first(self):
        self.assertEqual(mi.Graph(self.root / "graph.toml").order().index("base"), 0)
        graph = mi.Graph(self.root / "graph.toml")
        self.assertLess(graph.order().index("base"), graph.order().index("gamma"))
        self.assertEqual(graph.parents("gamma"), ["base"])
        self.assertEqual(graph.parents("alpha"), [])

    def test_refs_and_build_args_use_the_hash_of_the_parent(self):
        graph = mi.Graph(self.root / "graph.toml")
        self.assertEqual(graph.ref("alpha"), f"example/alpha:{graph.hash('alpha')}")
        self.assertEqual(graph.node_build_args("gamma"), {"RUNTIME_BASE_IMAGE": graph.ref("base")})
        self.assertEqual(graph.node_build_args("alpha"), {})

    def test_unknown_schema_node_target_and_duplicate_arg_are_rejected(self):
        text = (EXAMPLE / "graph.toml").read_text()
        for bad, message in ((text.replace("schema = 1", "schema = 2"), "schema must be 1"),
                             (text.replace('target = "c-alpha"', 'target = "c-nope"'), "not found in the Dockerfile"),
                             (text.replace('arg = "C_BETA_IMAGE"', 'arg = "C_ALPHA_IMAGE"'), "used by"),
                             (text.replace('components = ["alpha"', 'components = ["zeta", "alpha"'), "unknown node"),
                             (text.replace('namespace = "example"', 'namespace = "Bad Name"'), "namespace")):
            self.write(bad)
            with self.assertRaises(SystemExit) as raised:
                mi.Graph(self.root / "graph.toml")
            self.assertIn(message, str(raised.exception))

    def test_a_parent_cycle_is_rejected(self):
        (self.root / "Dockerfile").write_text(
            "ARG X_IMAGE\nARG Y_IMAGE\nFROM scratch AS x\nCOPY --from=${Y_IMAGE} / /\nFROM scratch AS y\nCOPY --from=${X_IMAGE} / /\n")
        self.write('schema = 1\nnamespace = "cyc"\n[node.x]\ntarget = "x"\narg = "X_IMAGE"\n[node.y]\ntarget = "y"\narg = "Y_IMAGE"\n')
        with self.assertRaises(SystemExit) as raised:
            mi.Graph(self.root / "graph.toml")
        self.assertIn("cycle", str(raised.exception))

    def test_a_node_without_target_covers_the_whole_dockerfile_with_its_own_context(self):
        (self.root / "recipe").mkdir()
        (self.root / "recipe/Dockerfile").write_text("FROM scratch\nCOPY data /data\n")
        (self.root / "recipe/data").write_text("1")
        self.write('schema = 1\nnamespace = "r"\n[node.recipe]\ndockerfile = "recipe/Dockerfile"\ncontext = "recipe"\nship = false\n')
        first = mi.Graph(self.root / "graph.toml").hash("recipe")
        (self.root / "recipe/data").write_text("2")
        self.assertNotEqual(first, mi.Graph(self.root / "graph.toml").hash("recipe"))

    def test_assemble_dockerfile_and_args(self):
        graph = mi.Graph(self.root / "graph.toml")
        text = graph.assemble_dockerfile()
        self.assertRegex(text, r"FROM \$\{RUNTIME_BASE_IMAGE\} AS final")
        copies = re.findall(r"^COPY --link --from=use-(\w+) / /$", text, flags=re.M)
        self.assertEqual(copies, ["alpha", "beta", "gamma"], "manifest order")
        self.assertNotIn("RUN ", text)
        self.assertTrue(text.rstrip().endswith("ENV EXAMPLE_HOME=/opt/example"))
        self.assertEqual(graph.assemble_args(), [f"RUNTIME_BASE_IMAGE={graph.ref('base')}",
                                                 f"C_ALPHA_IMAGE={graph.ref('alpha')}",
                                                 f"C_BETA_IMAGE={graph.ref('beta')}",
                                                 f"C_GAMMA_IMAGE={graph.ref('gamma')}"])

    def test_only_the_listed_components_are_assembled(self):
        text = (EXAMPLE / "graph.toml").read_text().replace('["alpha", "beta", "gamma"]', '["beta"]')
        self.write(text)
        copies = re.findall(r"^COPY --link", mi.Graph(self.root / "graph.toml").assemble_dockerfile(), flags=re.M)
        self.assertEqual(len(copies), 1, "a component that is not listed is not in the image")


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(REPO / "scripts/malrunar_image.py"), "graph", str(EXAMPLE / "graph.toml"), *args],
                              capture_output=True, text=True)

    def test_plan_is_json_and_hash_matches(self):
        result = self.run_cli("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(list(plan), ["base", "alpha", "beta", "gamma"])
        self.assertEqual(self.run_cli("hash", "gamma").stdout.strip(), plan["gamma"]["hash"])
        self.assertEqual(self.run_cli("ref", "gamma").stdout.strip(), plan["gamma"]["ref"])
        self.assertEqual(plan["gamma"]["parents"], ["base"])

    def test_bad_usage_exits_2_and_the_directory_mode_still_works(self):
        self.assertEqual(self.run_cli("nope").returncode, 2)
        listing = subprocess.run([sys.executable, str(REPO / "scripts/malrunar_image.py"), "list"], capture_output=True, text=True)
        self.assertEqual(listing.returncode, 0)
        self.assertIn("desktop-base", listing.stdout)


if __name__ == "__main__":
    unittest.main()
