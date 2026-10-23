# SPDX-License-Identifier: Apache-2.0
"""Forward-auth identity, capabilities and the route classification of the HTTP API."""
import unittest

import helpers  # noqa: F401
from helpers import make_config
from malrunar_obs.config import ConfigError, load_config
from test_api import ApiCase

PROXY = {"X-Forwarded-User": "ann"}
SECRET_HEADER = "X-Proxy-Secret"
SECRET_A = "secret-aaaaaaaaaaaaaaaa"
SECRET_B = "secret-bbbbbbbbbbbbbbbb"


def headers(user="ann", groups="", **extra):
    out = {"Remote-User": user, "Remote-Groups": groups, "Remote-Name": "Ann Example", "Remote-Email": "ann@example.invalid"}
    out.update(extra)
    return {k: v for k, v in out.items() if v}


class RouteClassificationTests(ApiCase):
    async def test_every_registered_route_is_classified(self):
        await self.start()
        self.assertEqual(self.controller.unclassified_routes(), [])

    async def test_an_unclassified_route_is_refused_when_enforcing(self):
        client = await self.start(access={"trusted_proxies": ["127.0.0.0/8"]})
        self.assertEqual((await client.get("/api/scenes", headers=headers())).status, 200)
        del self.controller.route_capabilities[("GET", "/api/scenes")]  # as a plugin route nobody classified
        self.assertEqual((await client.get("/api/scenes", headers=headers())).status, 403)
        self.assertEqual(self.controller.unclassified_routes(), [("GET", "/api/scenes")])
        self.controller.classify_route("GET", "/api/scenes", "viewer")
        self.assertEqual((await client.get("/api/scenes", headers=headers())).status, 200)


class DefaultsTests(ApiCase):
    async def test_nothing_configured_enforces_nothing(self):
        client = await self.start()
        self.assertFalse(self.controller.enforcing)
        for method, path in (("get", "/api/clips"), ("post", "/api/stop-output"), ("get", "/api/whoami")):
            self.assertEqual((await getattr(client, method)(path)).status, 200)
        body = await (await client.get("/api/whoami", headers=headers())).json()
        self.assertEqual(body, {"user": None, "name": None, "email": None, "groups": [], "capabilities": ["operator", "viewer"]})

    async def test_startup_warning_is_logged(self):
        with self.assertLogs("malrunar_obs", "WARNING") as logs:
            await self.start()
        self.assertIn("no identity", logs.output[0])
        self.assertIn("enforces nothing", logs.output[0])


class TrustTests(ApiCase):
    async def test_spoofed_headers_from_an_untrusted_address_are_ignored(self):
        client = await self.start(access={"trusted_proxies": ["203.0.113.0/24"]})
        response = await client.get("/api/whoami", headers=headers("mallory", "ops"))
        self.assertEqual(response.status, 403)
        self.assertEqual((await client.get("/api/clips", headers=headers("mallory"))).status, 403)

    async def test_x_forwarded_for_is_not_the_peer(self):
        client = await self.start(access={"trusted_proxies": ["203.0.113.0/24"]})
        response = await client.get("/api/whoami", headers=headers(**{"X-Forwarded-For": "203.0.113.9"}))
        self.assertEqual(response.status, 403)

    async def test_trusted_peer_default_is_operator(self):
        client = await self.start(access={"trusted_proxies": ["127.0.0.0/8"]})
        self.assertEqual((await client.get("/api/clips", headers=headers())).status, 200)
        body = await (await client.get("/api/whoami", headers=headers(groups="a, b"))).json()
        self.assertEqual(
            body,
            {"user": "ann", "name": "Ann Example", "email": "ann@example.invalid", "groups": ["a", "b"], "capabilities": ["operator", "viewer"]},
        )
        self.assertEqual((await client.get("/api/clips")).status, 403, "anonymous: refused when trust is configured")

    async def test_health_and_ready_stay_open(self):
        client = await self.start(access={"trusted_proxies": ["203.0.113.0/24"]})
        self.assertNotEqual((await client.get("/health")).status, 403)  # 200, or 503 while the fake OBS is not ready
        self.assertNotEqual((await client.get("/ready")).status, 403)

    async def test_secret_and_rotation(self):
        secret_file = self.tmp / "secrets"
        secret_file.write_text(f"{SECRET_A}\n", encoding="utf-8")
        access = {"proxy_secret_header": SECRET_HEADER, "proxy_secret_file": str(secret_file)}
        client = await self.start(access=access)
        ok = await client.get("/api/clips", headers=headers(**{SECRET_HEADER: SECRET_A}))
        self.assertEqual(ok.status, 200)
        self.assertEqual((await client.get("/api/clips", headers=headers())).status, 403, "missing secret")
        self.assertEqual((await client.get("/api/clips", headers=headers(**{SECRET_HEADER: "wrong-wrong-wrong-x"}))).status, 403)
        # rotation: the file holds the old and the new secret
        secret_file.write_text(f"{SECRET_A}\n{SECRET_B}\n", encoding="utf-8")
        for secret in (SECRET_A, SECRET_B):
            self.assertEqual((await client.get("/api/clips", headers=headers(**{SECRET_HEADER: secret}))).status, 200)

    async def test_secret_and_proxy_must_both_pass(self):
        secret_file = self.tmp / "secrets"
        secret_file.write_text(SECRET_A, encoding="utf-8")
        client = await self.start(
            access={"proxy_secret_header": SECRET_HEADER, "proxy_secret_file": str(secret_file), "trusted_proxies": ["203.0.113.0/24"]}
        )
        self.assertEqual((await client.get("/api/clips", headers=headers(**{SECRET_HEADER: SECRET_A}))).status, 403)

    async def test_custom_header_names(self):
        client = await self.start(
            access={"identity_header": "X-U", "groups_header": "X-G", "name_header": "X-N", "email_header": "X-E", "trusted_proxies": ["127.0.0.0/8"]}
        )
        body = await (await client.get("/api/whoami", headers={"X-U": "bob", "X-G": "g1", "X-N": "Bob", "X-E": "b@example.invalid"})).json()
        self.assertEqual((body["user"], body["groups"], body["name"], body["email"]), ("bob", ["g1"], "Bob", "b@example.invalid"))
        self.assertEqual((await client.get("/api/whoami", headers=headers())).status, 403, "Remote-User is not the configured header")


class GroupTests(ApiCase):
    ACCESS = {"trusted_proxies": ["127.0.0.0/8"], "groups": {"operator": ["ops"], "viewer": ["watchers"]}}

    async def test_groups_map_to_capabilities(self):
        client = await self.start(access=self.ACCESS)
        operator = headers("op", "x, ops")
        viewer = headers("vi", "watchers")
        nobody = headers("no", "other")
        who = lambda r: r.json()
        self.assertEqual((await who(await client.get("/api/whoami", headers=operator)))["capabilities"], ["operator", "viewer"])
        self.assertEqual((await who(await client.get("/api/whoami", headers=viewer)))["capabilities"], ["viewer"])
        self.assertEqual((await client.get("/api/whoami", headers=nobody)).status, 403, "no mapped capability")
        self.assertEqual((await client.get("/api/whoami")).status, 403, "anonymous")

    async def test_viewer_cannot_use_operator_routes(self):
        client = await self.start(access=self.ACCESS)
        viewer = headers("vi", "watchers")
        for path in ("/api/scenes", "/api/recording/status", "/api/input-levels", "/api/daily-fallback/status", "/"):
            self.assertNotEqual((await client.get(path, headers=viewer)).status, 403, path)
        for method, path in (
            ("get", "/api/clips"), ("post", "/api/scene/live"), ("post", "/api/stop-output"), ("post", "/api/recording/start"),
            ("post", "/api/sender-heartbeat"), ("delete", "/api/clips/x"), ("get", "/api/daily-fallback/preview"),
            ("post", "/api/daily-fallback/ensure"),
        ):
            self.assertEqual((await getattr(client, method)(path, headers=viewer)).status, 403, path)
        self.assertEqual((await client.post("/api/stop-output", headers=headers("op", "ops"))).status, 200)

    async def test_operator_alone_implies_viewer(self):
        client = await self.start(access={"trusted_proxies": ["127.0.0.0/8"], "groups": {"operator": ["ops"]}})
        self.assertEqual((await client.get("/api/scenes", headers=headers("op", "ops"))).status, 200)

    async def test_health_is_open_even_for_anonymous(self):
        client = await self.start(access=self.ACCESS)
        self.assertNotEqual((await client.get("/health")).status, 403)


class IdentityStateTests(ApiCase):
    ACCESS = {"trusted_proxies": ["127.0.0.0/8"]}

    async def test_per_identity_broll_has_an_owner(self):
        client = await self.start(access=self.ACCESS, features={"program_control": True})
        self.assertEqual((await client.get("/api/daily-fallback/status")).status, 403)
        path = self.controller.broll.path(user="bob")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        bob = await (await client.get("/api/daily-fallback/status", headers=headers("bob"))).json()
        ann = await (await client.get("/api/daily-fallback/status", headers=headers("ann"))).json()
        self.assertTrue(bob["ready"])
        self.assertFalse(ann["ready"])
        self.assertEqual((await client.get("/api/daily-fallback/preview", headers=headers("bob"))).status, 200)
        self.assertEqual((await client.get("/api/daily-fallback/preview", headers=headers("ann"))).status, 404)

    async def test_untrusted_user_header_does_not_choose_the_owner(self):
        client = await self.start(access={"trusted_proxies": ["203.0.113.0/24"]}, features={"program_control": True})
        self.assertEqual((await client.get("/api/daily-fallback/status", headers=headers("bob"))).status, 403)

    async def test_heartbeat_user_comes_from_the_trusted_header(self):
        client = await self.start(access=self.ACCESS, features={"program_control": True})
        response = await client.post("/api/sender-heartbeat", json={"healthy": True, "user": "mallory"}, headers=headers("ann"))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.controller.sender_user, "ann")

    async def test_heartbeat_without_identity_is_forbidden_when_identities_are_in_use(self):
        # identity_header alone does not trust anything: the sender is anonymous.
        client = await self.start(access={"identity_header": "X-U"}, features={"program_control": True})
        response = await client.post("/api/sender-heartbeat", json={"healthy": True}, headers={"X-U": "ann"})
        self.assertEqual(response.status, 403)
        self.assertIsNone(self.controller.sender_user)

    async def test_no_identity_configuration_keeps_the_old_heartbeat(self):
        client = await self.start(features={"program_control": True})
        self.assertEqual((await client.post("/api/sender-heartbeat", json={"healthy": True}, headers=headers("ann"))).status, 200)
        self.assertIsNone(self.controller.sender_user)


class ConfigTests(helpers.TempDir, unittest.TestCase):
    def test_defaults_do_not_trust_anything(self):
        config = make_config(self.tmp)
        self.assertFalse(config.trust_configured)
        self.assertFalse(config.identity_active)
        self.assertEqual(config.access.groups, {})
        self.assertEqual(config.identity_config().mode, "none")

    def test_environment_overrides(self):
        env = {
            "MALRUNAR_OBS_ACCESS_TRUSTED_PROXIES": "203.0.113.0/24, 192.0.2.1",
            "MALRUNAR_OBS_ACCESS_GROUPS_HEADER": "X-G",
            "MALRUNAR_OBS_ACCESS_GROUPS_OPERATOR": "ops, staff",
            "MALRUNAR_OBS_ACCESS_GROUPS_VIEWER": "watchers",
        }
        config = make_config(self.tmp, env=env)
        self.assertEqual(config.access.trusted_proxies, ["203.0.113.0/24", "192.0.2.1"])
        self.assertEqual(config.access.groups, {"operator": ["ops", "staff"], "viewer": ["watchers"]})
        self.assertEqual(config.identity_config().groups_header, "X-G")
        self.assertTrue(config.identity_active)

    def test_secret_settings_come_in_pairs(self):
        with self.assertRaisesRegex(ConfigError, "go together"):
            make_config(self.tmp, access={"proxy_secret_header": "X-S"})

    def test_invalid_values_are_rejected(self):
        for access, text in (
            ({"trusted_proxies": ["not-a-cidr"]}, "not an address"),
            ({"identity_header": "bad header"}, "header name"),
            ({"trusted_proxies": "203.0.113.0/24"}, "list of strings"),
            ({"groups": {"admin": ["x"]}, "trusted_proxies": ["203.0.113.0/24"]}, "unknown capability"),
            ({"groups": {"operator": "ops"}, "trusted_proxies": ["203.0.113.0/24"]}, "list of group names"),
            ({"groups": {"operator": ["ops"]}}, "needs trust"),
            ({"nonsense": 1}, "unknown setting"),
        ):
            with self.subTest(access=access), self.assertRaisesRegex(ConfigError, text):
                make_config(self.tmp, access=access)

    def test_a_missing_secret_file_fails_at_startup(self):
        config = make_config(self.tmp, access={"proxy_secret_header": "X-S", "proxy_secret_file": str(self.tmp / "nope")})
        with self.assertRaisesRegex(ConfigError, "cannot read"):
            config.identity_config()


if __name__ == "__main__":
    unittest.main()
