# SPDX-License-Identifier: Apache-2.0
"""docs/obs-control-api.md must document every setting, environment variable and route."""
import re
import unittest

import helpers
from malrunar_obs.config import CAMERA_DEFAULTS, DEFAULTS, ENV_ALIASES, env_name

DOC = helpers.ROOT.parent / "docs" / "obs-control-api.md"
ROUTES = helpers.ROOT / "malrunar_obs" / "controller.py"


@unittest.skipUnless(DOC.is_file(), "the docs are not part of the image")
class DocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOC.read_text(encoding="utf-8")

    def test_every_setting_and_variable_is_documented(self):
        missing = []
        for section, values in DEFAULTS.items():
            if section in ("cameras", "profiles", "plugin"):
                continue
            for key in values:
                if (section, key) == ("access", "groups"):
                    for capability in ("viewer", "operator"):
                        if f"`MALRUNAR_OBS_ACCESS_GROUPS_{capability.upper()}`" not in self.text:
                            missing.append(f"MALRUNAR_OBS_ACCESS_GROUPS_{capability.upper()}")
                    continue
                name = env_name(section, key)
                if f"`{name}`" not in self.text:
                    missing.append(name)
        for key in CAMERA_DEFAULTS:
            name = f"MALRUNAR_OBS_CAMERA_{key.upper()}"
            if f"`{name}`" not in self.text:
                missing.append(name)
        for alias in ENV_ALIASES:
            if f"`{alias}`" not in self.text:
                missing.append(alias)
        self.assertEqual(missing, [])

    def test_every_route_is_in_the_compatibility_table(self):
        source = ROUTES.read_text(encoding="utf-8")
        routes = re.findall(r'r\.add_(get|post|delete)\("([^"]+)"', source)
        self.assertGreater(len(routes), 30)
        missing = []
        for method, path in routes:
            doc_path = path.replace("{clip_id}", "<id>").replace("{key}", "<key>")
            if f"| `{method.upper()} {doc_path}`" not in self.text:
                missing.append((method, doc_path))
        self.assertEqual(missing, [])

    def test_the_table_has_no_stale_rows(self):
        section = self.text.split("## HTTP API and compatibility table")[1].split("## Identity and security")[0]
        in_table = set(re.findall(r"^\| `(GET|POST|DELETE) ([^`]+)` \|", section, re.M))
        source = ROUTES.read_text(encoding="utf-8")
        served = {
            (m.upper(), p.replace("{clip_id}", "<id>").replace("{key}", "<key>"))
            for m, p in re.findall(r'r\.add_(get|post|delete)\("([^"]+)"', source)
        }
        served.add(("GET", "/take-studio/takes/<id>.mp4"))
        stale = {row for row in in_table if row not in served and row[1] != "<id>"}
        self.assertEqual(stale, set())

    def test_the_example_values_in_the_docs_parse(self):
        import tomllib

        blocks = re.findall(r"```toml\n(.*?)```", self.text, re.S)
        self.assertTrue(blocks)
        for block in blocks:
            tomllib.loads(block)


if __name__ == "__main__":
    unittest.main()
