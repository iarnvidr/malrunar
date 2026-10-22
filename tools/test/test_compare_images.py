"""tools/compare_images.py diff on hand-made manifests (standard library only; no Docker)."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare_images as ci  # noqa: E402


def entry(sha="a", mode=0o644, **extra):
    return {"type": "file", "mode": mode, "uid": 0, "gid": 0, "size": 1, "sha256": sha, **extra}


def manifest(entries, config=None, packages=(), lists=None, loader=()):
    return {"image": "img", "id": "sha256:0", "layers": 1, "config": config or {"Env": ["A=1"]},
            "entries": entries, "dpkg_lists": lists or {}, "ldconfig": list(loader), "packages": list(packages)}


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def write(self, name, data):
        path = self.base / name
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return str(path)

    def diff(self, old, new, detail=None, rules=None, noise=None, renames=()):
        """rules: text of a rules file; noise: a manifest of a second build of NEW."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = ci.diff(self.write("old.json", old), self.write("new.json", new), detail,
                             self.write("rules.tsv", rules) if rules is not None else None,
                             self.write("noise.json", noise) if noise is not None else None, renames)
        return status, out.getvalue()

    def test_identical_images_are_equivalent(self):
        data = manifest({"etc/a": entry(), "opt": {"type": "dir", "mode": 0o755, "uid": 0, "gid": 0}})
        status, output = self.diff(data, data)
        self.assertEqual(status, 0)
        self.assertIn("unexplained: 0", output)

    def test_content_mode_and_missing_paths_are_unexplained(self):
        old = manifest({"etc/a": entry("a"), "etc/b": entry("b"), "etc/c": entry("c")})
        new = manifest({"etc/a": entry("changed"), "etc/b": entry("b", mode=0o755), "etc/d": entry("d")})
        status, output = self.diff(old, new)
        self.assertEqual(status, 1)
        self.assertIn("unexplained: 4", output)
        self.assertIn("UNEXPLAINED only-old", output)
        self.assertIn("UNEXPLAINED only-new", output)
        self.assertIn("UNEXPLAINED changed", output)

    def test_a_config_difference_fails_unless_a_rule_explains_it(self):
        old, new = manifest({}, {"Env": ["A=1"]}), manifest({}, {"Env": ["A=2"]})
        self.assertEqual(self.diff(old, new)[0], 1)
        status, output = self.diff(old, new, rules="^config:Env$\tthe value moved on purpose\n")
        self.assertEqual(status, 0)
        self.assertIn("the value moved on purpose", output)

    def test_rules_explain_paths(self):
        old, new = manifest({"var/cache/x": entry("1")}), manifest({"var/cache/x": entry("2")})
        status, output = self.diff(old, new, rules="^var/cache/\tcaches differ\n")
        self.assertEqual(status, 0)
        self.assertIn("caches differ", output)

    def test_a_changed_package_version_explains_the_files_it_owns(self):
        old = manifest({"usr/bin/tool": entry("1")}, packages=["tool 1.0"], lists={"tool": ["/usr/bin/tool"]})
        new = manifest({"usr/bin/tool": entry("2")}, packages=["tool 1.1"], lists={"tool": ["/usr/bin/tool"]})
        status, output = self.diff(old, new)
        self.assertEqual(status, 0, output)
        self.assertIn("differing packages 1", output)

    def test_noise_from_a_second_build_explains_nondeterminism(self):
        old, new = manifest({"etc/key": entry("1")}), manifest({"etc/key": entry("2")})
        second = manifest({"etc/key": entry("3")})
        self.assertEqual(self.diff(old, new)[0], 1)
        status, output = self.diff(old, new, noise=second)
        self.assertEqual(status, 0)
        self.assertIn("nondeterminism", output)

    def test_rename_maps_an_old_prefix_onto_the_new_one(self):
        old = manifest({"opt/old/bin/x": entry("1"), "usr/bin/x": entry("2", type="symlink", link="/opt/old/bin/x")},
                       {"Env": ["PATH=/opt/old/bin:/usr/bin"]})
        new = manifest({"opt/new/bin/x": entry("1"), "usr/bin/x": entry("2", type="symlink", link="/opt/new/bin/x")},
                       {"Env": ["PATH=/opt/new/bin:/usr/bin"]})
        self.assertEqual(self.diff(old, new)[0], 1)
        status, _ = self.diff(old, new, renames=[("/opt/old", "/opt/new")])
        self.assertEqual(status, 0)

    def test_detail_file_lists_every_difference(self):
        detail = self.base / "detail.txt"
        old, new = manifest({"etc/a": entry("1")}), manifest({"etc/a": entry("2")})
        self.diff(old, new, detail=str(detail))
        self.assertIn("etc/a", detail.read_text())
        self.assertIn("UNEXPLAINED", detail.read_text())


if __name__ == "__main__":
    unittest.main()
