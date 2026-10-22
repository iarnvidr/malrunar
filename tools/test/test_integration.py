"""The tools against a real Docker daemon (skipped when there is none): build the example graph,
assemble and check it, run it again to see nothing is rebuilt, and describe and compare the result.
Every container run is bounded by `timeout`."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "tools/example/graph.toml"
sys.path.insert(0, str(REPO / "scripts"))
import malrunar_image as mi  # noqa: E402


def docker_works():
    try:
        return subprocess.run(["timeout", "20", "docker", "info"], capture_output=True).returncode == 0
    except OSError:
        return False


@unittest.skipUnless(docker_works(), "no Docker daemon")
class RealDockerTests(unittest.TestCase):
    tag = "malrunar-tools-test/assembled:latest"

    def graph(self, *args):
        return subprocess.run([sys.executable, str(REPO / "tools/malrunar_graph.py"), str(MANIFEST), "--jobs", "2",
                               "--tag", self.tag, *args], capture_output=True, text=True, timeout=1500)

    @classmethod
    def tearDownClass(cls):
        graph = mi.Graph(MANIFEST)
        refs = [cls.tag] + [graph.ref(n) for n in graph.nodes]
        subprocess.run(["docker", "rmi", "-f", *refs], capture_output=True)

    def test_build_assemble_rebuild_and_compare(self):
        first = self.graph()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("checks passed", first.stdout)
        graph = mi.Graph(MANIFEST)
        for name in graph.nodes:
            self.assertEqual(subprocess.run(["docker", "image", "inspect", graph.ref(name)], capture_output=True).returncode, 0, name)
        labels = subprocess.run(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", self.tag],
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(sorted(json.loads(labels)["example.components"].split(",")),
                         sorted(a for a in graph.assemble_args()))
        # Run the assembled image: the base, three components and the final ENV.
        out = subprocess.run(["timeout", "-k", "10", "120", "docker", "run", "--rm", "--entrypoint", "bash", self.tag, "-c",
                              "cat /etc/example-base; ls /opt/example; echo $EXAMPLE_HOME"],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.split(), ["example", "base", "alpha", "beta", "gamma", "/opt/example"])

        # Nothing changed: no node is built again.
        second = self.graph("--components-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        for name in graph.nodes:
            self.assertIn(f"{name}: local ", second.stdout)
        self.assertNotIn(": built", second.stdout)

        # An image compared with itself is equivalent, and a manifest is reproducible.
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "one.json", Path(tmp) / "two.json"
            for target in (one, two):
                made = subprocess.run([sys.executable, str(REPO / "tools/compare_images.py"), "manifest", self.tag, str(target)],
                                      capture_output=True, text=True, timeout=600)
                self.assertEqual(made.returncode, 0, made.stderr)
            compared = subprocess.run([sys.executable, str(REPO / "tools/compare_images.py"), "diff", str(one), str(two)],
                                      capture_output=True, text=True)
            self.assertEqual(compared.returncode, 0, compared.stdout)
            self.assertIn("unexplained: 0", compared.stdout)
            data = json.loads(one.read_text())
            self.assertIn("opt/example/beta/bin/beta", data["entries"])
            self.assertEqual(data["entries"]["opt/example/beta/bin/beta"]["mode"], 0o755)


if __name__ == "__main__":
    unittest.main()
