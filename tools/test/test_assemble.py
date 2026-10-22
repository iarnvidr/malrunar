"""tools/assemble-image.sh with a fake docker (standard library only)."""
import unittest
from pathlib import Path

from fakes import FakeDockerCase

SCRIPT = Path(__file__).resolve().parents[1] / "assemble-image.sh"


class AssembleTests(FakeDockerCase):
    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name)
        self.dockerfile = base / "Dockerfile.assemble"
        self.dockerfile.write_text("FROM scratch\n")
        self.checks = base / "checks.sh"
        self.checks.write_text("true\n")
        self.store.write_text("ex/base:1\nex/a:1\n")

    def assemble(self, *args, **extra):
        return self.run_cmd(["bash", str(SCRIPT), *args], **extra)

    def test_usage_without_arguments(self):
        result = self.assemble()
        self.assertEqual(result.returncode, 2)
        self.assertIn("--dockerfile", result.stderr)

    def test_a_missing_component_image_is_reported_before_any_build(self):
        result = self.assemble("--dockerfile", str(self.dockerfile), "--tag", "ex/final:latest", "BASE=ex/base:1", "C_B=ex/b:1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ex/b:1 is not in this Docker store", result.stderr)
        self.assertNotIn("docker build", self.logged())

    def test_bad_argument_is_rejected(self):
        result = self.assemble("--dockerfile", str(self.dockerfile), "--tag", "ex/final:latest", "lower=case")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad ARG=image-ref", result.stderr)

    def test_success_builds_with_the_refs_checks_and_tags_every_name(self):
        result = self.assemble("--dockerfile", str(self.dockerfile), "--tag", "ex/final:latest", "--tag", "ex/final:2",
                               "--checks", str(self.checks), "BASE=ex/base:1", "C_A=ex/a:1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        log = self.logged()
        self.assertIn("--build-arg BASE=ex/base:1", log)
        self.assertIn("--build-arg ASSEMBLE_COMPONENTS=BASE=ex/base:1,C_A=ex/a:1", log)
        self.assertIn("--target final", log)
        self.assertRegex(log, r"timeout -k 10 900 docker run --rm -i --user root --entrypoint bash \S+ -s")
        stored = self.lines(self.store)
        self.assertIn("ex/final:latest", stored)
        self.assertIn("ex/final:2", stored)
        self.assertFalse([s for s in stored if "candidate" in s], "the candidate tag is removed")

    def test_failing_checks_leave_the_tag_unmoved(self):
        result = self.assemble("--dockerfile", str(self.dockerfile), "--tag", "ex/final:latest", "--checks", str(self.checks),
                               "BASE=ex/base:1", FAKE_RUN_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the checks failed", result.stderr)
        self.assertNotIn("ex/final:latest", self.lines(self.store))
        self.assertNotIn("docker tag", self.logged())

    def test_a_failed_build_tags_nothing(self):
        result = self.assemble("--dockerfile", str(self.dockerfile), "--tag", "ex/final:latest", "BASE=ex/base:1",
                               FAKE_BUILD_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ex/final:latest", self.lines(self.store))


if __name__ == "__main__":
    unittest.main()
