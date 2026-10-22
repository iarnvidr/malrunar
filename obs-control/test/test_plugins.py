# SPDX-License-Identifier: Apache-2.0
import json
import sys
import textwrap
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import helpers  # noqa: F401  (puts the package on sys.path)
from fake_obs import FakeObs
from helpers import TempDir, make_config
from malrunar_obs.controller import Controller, ProgramHolder
from malrunar_obs.plugins import PluginError, load_plugins
from malrunar_obs.scenes import generate

PLUGIN = textwrap.dedent(
    '''
    from aiohttp import web
    from malrunar_obs.controller import ProgramHolder

    STATE = {"held": False}

    def extend_collection(builder, config):
        builder.add_source("Plugin Frame", "color_source_v3", {"color": 4278190080, "width": 10, "height": 10})
        builder.add_scene("Plugin Scene", ["Plugin Frame"])

    def register(controller):
        async def hello(request):
            (version,) = await controller.obs(("GetVersion", {}))
            return web.json_response({"hello": controller.config.plugin["demo"]["greeting"], "obs": version["obsVersion"]})

        async def arm(request):
            STATE["held"] = True
            return web.json_response({"ok": True})

        async def release(neutral):
            STATE["held"] = False

        controller.app.router.add_get("/api/demo/hello", hello)
        controller.app.router.add_post("/api/demo/arm", arm)
        controller.extra_live_scenes.add("Plugin Scene")
        controller.add_program_holder(ProgramHolder("demo", lambda: STATE["held"], release, "Demo has the program"))
        controller.add_ready_info(lambda: {"demo": True})
    '''
)


class LoadingTests(TempDir, unittest.TestCase):
    def test_plugins_dir_loads_py_files_in_order_and_skips_underscore_files(self):
        folder = self.tmp / "plugins"
        folder.mkdir()
        (folder / "b_second.py").write_text("ORDER = 2\n")
        (folder / "a_first.py").write_text("ORDER = 1\n")
        (folder / "_helper.py").write_text("raise RuntimeError('must not load')\n")
        config = make_config(self.tmp, plugins={"dir": str(folder)})
        self.assertEqual([plugin.ORDER for plugin in load_plugins(config)], [1, 2])
        self.assertIn(str(folder), sys.path)
        sys.path.remove(str(folder))

    def test_module_names_and_file_paths(self):
        path = self.tmp / "one.py"
        path.write_text("NAME = 'one'\n")
        config = make_config(self.tmp, plugins={"modules": [str(path), "json"]})
        modules = load_plugins(config)
        self.assertEqual(modules[0].NAME, "one")
        self.assertIs(modules[1], json)

    def test_failures_name_the_plugin(self):
        bad = self.tmp / "bad.py"
        bad.write_text("raise RuntimeError('boom')\n")
        with self.assertRaisesRegex(PluginError, "bad.py.*boom"):
            load_plugins(make_config(self.tmp, plugins={"modules": [str(bad)]}))
        with self.assertRaisesRegex(PluginError, "does not exist"):
            load_plugins(make_config(self.tmp, plugins={"modules": [str(self.tmp / "none.py")]}))
        with self.assertRaisesRegex(PluginError, "failed to import"):
            load_plugins(make_config(self.tmp, plugins={"modules": ["no_such_module_xyz"]}))
        with self.assertRaisesRegex(PluginError, "does not exist"):
            load_plugins(make_config(self.tmp, plugins={"dir": str(self.tmp / "nodir")}))


class PluginFeatureTests(TempDir, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / "demo.py").write_text(PLUGIN)
        self.config = make_config(
            self.tmp,
            features={"program_control": True},
            plugins={"modules": [str(self.tmp / "demo.py")]},
            plugin={"demo": {"greeting": "hi"}},
        )

    def test_plugin_scene_is_generated_with_the_collection(self):
        generate(self.config, load_plugins(self.config))
        collection = json.loads((self.tmp / "obs-config/basic/scenes/malrunar.json").read_text())
        self.assertIn({"name": "Plugin Scene"}, collection["scene_order"])

    async def test_register_adds_routes_holders_and_ready_info(self):
        obs = await FakeObs().start()
        self.addAsyncCleanup(obs.stop)
        controller = Controller(self.config, load_plugins(self.config))
        controller.obs_client.url = obs.url
        client = TestClient(TestServer(controller.app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        await controller.startup_task
        hello = await client.get("/api/demo/hello")
        self.assertEqual(await hello.json(), {"hello": "hi", "obs": "32.2.0"})
        self.assertTrue((await (await client.get("/ready")).json())["demo"])
        controller.sender_last_seen = 1e12
        self.assertEqual((await client.post("/api/call/live")).status, 200)
        await client.post("/api/demo/arm")
        refused = await client.post("/api/call/live")
        self.assertEqual((refused.status, (await refused.json())["error"]), (409, "Demo has the program"))
        self.assertEqual((await client.post("/api/call/stop")).status, 200)
        self.assertFalse(controller.held_by())

    def test_program_holder_defaults(self):
        holder = ProgramHolder("x", lambda: False)
        self.assertIsNone(holder.release)
        self.assertTrue(holder.message)
        self.assertIsNotNone(web)


if __name__ == "__main__":
    unittest.main()
