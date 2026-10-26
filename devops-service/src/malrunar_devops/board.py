"""Azure Boards operations with explicit scope, policy and token callbacks."""
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit
from .cache import cached_read
from .errors import BoardError
from .policy import AzurePolicy



class AzureBoards:
    def __init__(self, policy=None, *, token_provider=None, session=None, cache=None, force=False):
        if not isinstance(policy, AzurePolicy) or not callable(token_provider):
            raise ValueError("Explicit Azure policy and token provider are required.")
        policy.require("connect", {"organization": policy.organization, "project": policy.project})
        self.policy, self.cache, self.force = policy, cache, force
        if session is None:
            import requests
            session = requests.Session()
            session.trust_env = False  # No ambient netrc credentials or implicit account selection.
        self.requests = session
        token = token_provider()
        if not isinstance(token, str) or not token.strip():
            raise BoardError("The configured provider credential is unavailable.")
        self.headers = {"Authorization": "Bearer " + token}
        self.deadline = time.monotonic() + 45

    def request(self, method, path, body=None, patch=False, params=None):
        url = self.policy.route("wit", path)
        self.policy.require("request", {"area": "wit", "method": method, "path": path})
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BoardError("DevOps refresh timed out.")
        headers = dict(self.headers)
        if patch:
            headers["Content-Type"] = "application/json-patch+json"
        response = self.requests.request(method, url, params={"api-version": "7.1", **(params or {})},
                                                headers=headers, json=body, timeout=min(15, remaining), allow_redirects=False)
        if response.status_code in (401, 403):
            raise BoardError("DevOps access denied. Check the configured Azure login and work item permissions.")
        if not response.ok:
            if response.status_code in (400, 409, 412):
                raise BoardError("DevOps rejected the change. Refresh the item and check its workflow or required fields.")
            raise BoardError(f"DevOps request failed (HTTP {response.status_code}).")
        return response.json()

    def normalize(self, item):
        f = item.get("fields", {})
        if f.get("System.TeamProject") != self.policy.project:
            raise BoardError("This work item is outside the configured project.")
        self.policy.require("item", {"id": item["id"], "project": f.get("System.TeamProject")})
        assigned = f.get("System.AssignedTo", {})
        return {"id": item["id"], "revision": item["rev"], "title": f.get("System.Title", ""),
                "state": f.get("System.State", ""), "type": f.get("System.WorkItemType", ""),
                "assignedTo": assigned.get("displayName", "") if isinstance(assigned, dict) else str(assigned),
                "url": f"{self.policy.web_base}/_workitems/edit/{item['id']}"}

    def item(self, ident):
        item = self.request("GET", f"workitems/{int(ident)}")
        result = self.normalize(item)
        text = item.get("fields", {}).get("System.Description", "")
        result["description"] = html.unescape(re.sub(r"<[^>]+>", " ", text))[:12000]
        return result

    def types(self):
        if getattr(self, "cache", None):
            result = self.cache.fetch("workflow", lambda: {"types": self.load_types()}, ttl=900, force=self.force, workflow=True)
            self.workflow_warning = result["cache"].get("warning")
            return result["types"]
        return self.load_types()

    def load_types(self):
        types = self.request("GET", "workitemtypes").get("value", [])
        def states(t):
            return {"name": t["name"], "states": [{"name": s["name"], "category": s.get("category", "Unknown")}
                    for s in self.request("GET", f"workitemtypes/{quote(t['name'], safe='')}/states").get("value", [])]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(states, [t for t in types if not t.get("isDisabled")]))

    def board(self, payload):
        types = self.types()
        selected = payload.get("type")
        if selected and selected not in {t["name"] for t in types}:
            raise BoardError("Unknown work item type.")
        scope = payload["scope"]
        next_cursor = None
        if scope == "linked":
            ids = payload.get("linkedIds", [])
        else:
            literal = lambda s: "'" + s.replace("'", "''") + "'"
            conditions = ["[System.TeamProject] = @project"]
            if selected:
                conditions.append("[System.WorkItemType] = " + literal(selected))
            if scope == "mine":
                conditions.append("[System.AssignedTo] = @Me")
            elif scope != "all-open":
                raise BoardError("Unknown board scope.")
            # State names come from each actual type, including customized workflows.
            open_types = []
            for t in types:
                closed = [s["name"] for s in t["states"] if s["category"] in ("Completed", "Removed")]
                clause = "[System.WorkItemType] = " + literal(t["name"])
                if closed:
                    clause += " AND [System.State] NOT IN (" + ",".join(map(literal, closed)) + ")"
                open_types.append("(" + clause + ")")
            if open_types:
                conditions.append("(" + " OR ".join(open_types) + ")")
            if payload.get("cursor"):
                conditions.append("[System.Id] < " + str(int(payload["cursor"])))
            query = "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(conditions) + " ORDER BY [System.Id] DESC"
            found = self.request("POST", "wiql", {"query": query}, params={"$top": 201}).get("workItems", [])
            ids = [w["id"] for w in found[:200]]
            if len(found) > 200:
                next_cursor = ids[-1]
        items = []
        for start in range(0, len(ids), 200):
            batch = self.request("GET", "workitems", params={"ids": ",".join(map(str, ids[start:start + 200])),
                "fields": "System.Id,System.Title,System.State,System.WorkItemType,System.AssignedTo,System.TeamProject"}).get("value", [])
            items.extend(self.normalize(w) for w in batch)
        if selected:
            items = [w for w in items if w["type"] == selected]
        return {"items": items, "types": types, "nextCursor": next_cursor,
                "warnings": [self.workflow_warning] if getattr(self, "workflow_warning", None) else []}

    def transition(self, payload):
        item = self.item(payload["id"])
        if item["revision"] != payload["revision"]:
            raise BoardError("This item changed in DevOps. Refresh before moving it.")
        states = self.request("GET", f"workitemtypes/{quote(item['type'], safe='')}/states").get("value", [])
        if payload["state"] not in {s["name"] for s in states}:
            raise BoardError("That state is not part of this item's workflow.")
        changed = self.request("PATCH", f"workitems/{item['id']}", [
            {"op": "test", "path": "/rev", "value": payload["revision"]},
            {"op": "add", "path": "/fields/System.State", "value": payload["state"]},
        ], patch=True)
        return self.normalize(changed)

    def create(self, payload):
        if payload["type"] not in {t["name"] for t in self.types()}:
            raise BoardError("Unknown work item type.")
        # The user reviews plain text; encode it before storing the HTML field.
        item = self.request("POST", "workitems/$" + quote(payload["type"], safe=""), [
            {"op": "add", "path": "/fields/System.Title", "value": payload["title"]},
            {"op": "add", "path": "/fields/System.Description", "value": html.escape(payload["description"]).replace("\n", "<br>")},
        ], patch=True)
        return self.normalize(item)

    def ensure_link(self, payload):
        url = payload["url"]
        self.policy.require("link", {"id": payload["id"], "url": url})
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or not re.fullmatch(r"/h/[\w-]+/workspace/[\w-]+", parsed.path):
            raise BoardError("Invalid Paseo workspace link.")
        item = self.request("GET", f"workitems/{int(payload['id'])}", params={"$expand": "relations"})
        self.normalize(item)
        if any(r.get("rel") == "Hyperlink" and r.get("url") == url for r in item.get("relations", [])):
            return {"added": False, "url": url}
        self.request("PATCH", f"workitems/{item['id']}", [
            {"op": "test", "path": "/rev", "value": item["rev"]},
            {"op": "add", "path": "/relations/-", "value": {"rel": "Hyperlink", "url": url,
                "attributes": {"comment": self.policy.link_comment}}},
        ], patch=True)
        return {"added": True, "url": url}


def dispatch(action, payload, cache, *, factory=None, wiki_factory=None):
    if not callable(factory):
        raise ValueError("An explicit scoped provider factory is required.")
    if action.startswith("wiki-"):
        from .wiki import dispatch_wiki
        return dispatch_wiki(action, payload, cache, factory=wiki_factory)
    if action in ("board", "item"):
        force = bool(payload.get("force"))
        key = {"action": action, **{k: v for k, v in payload.items() if k != "force"}}
        def load():
            client = factory(cache=cache, force=force)
            return client.board(payload) if action == "board" else client.item(payload["id"])
        return cached_read(cache, key, load, ttl=300, force=force)
    if action not in ("transition", "create", "ensure-link"):
        raise BoardError("Unsupported DevOps action.")
    client = factory()  # Writes and their validation always use live Azure data.
    result = client.ensure_link(payload) if action == "ensure-link" else client.transition(payload) if action == "transition" else client.create(payload)
    if cache:
        try:
            cache.invalidate()
        except OSError:
            # A confirmed write remains a success; clients must show this warning.
            result["warning"] = "Saved in DevOps, but local cache invalidation failed. Use Refresh before continuing."
    return result
