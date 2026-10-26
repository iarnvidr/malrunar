"""Project-scoped wiki reads and reviewed, ETag-guarded page publication."""
import time
import base64
import mimetypes
from uuid import UUID
from urllib.parse import quote, urlencode
from .board import AzureBoards, BoardError


def page_path(value):
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 1000 or any(ord(c) < 32 for c in value):
        raise BoardError("Invalid wiki path.")
    if any(p in (".", "..") for p in value.split("/")) or "\\" in value:
        raise BoardError("Invalid wiki path.")
    return value.rstrip("/") or "/"


class AzureWiki(AzureBoards):
    def wiki_request(self, method, path, body=None, params=None, etag=None, missing=False):
        url = self.policy.route("wiki", path)
        self.policy.require("request", {"area": "wiki", "method": method, "path": path})
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BoardError("Wiki request timed out.")
        headers = {**self.headers, "Accept": "application/json"}
        if etag:
            headers["If-Match"] = etag
        response = self.requests.request(method, url, headers=headers,
            params={"api-version": "7.1", **(params or {})}, json=body, timeout=min(15, remaining), allow_redirects=False)
        if missing and response.status_code == 404:
            return None
        if response.status_code in (409, 412):
            raise BoardError("The wiki page has changed. Refresh it before publishing.")
        if response.status_code in (401, 403):
            raise BoardError("Wiki access denied. Check the configured login and permissions.")
        if not response.ok:
            raise BoardError(f"Wiki request failed (HTTP {response.status_code}).")
        return {"data": response.json(), "etag": response.headers.get("ETag", "")}

    def wikis(self):
        rows = self.wiki_request("GET", "wikis")["data"].get("value", [])
        return {"wikis": [{"id": w["id"], "name": w["name"], "type": w["type"],
            "url": f"{self.policy.web_base}/_wiki/wikis/{quote(w['id'], safe='')}"}
            for w in rows if w.get("projectId", "").lower() == self.policy.project_id
            and self._allow_wiki(w)]}

    def _allow_wiki(self, row):
        self.policy.require("wiki", {"id": row["id"], "projectId": row.get("projectId")})
        return True

    def require_wiki(self, ident):
        wiki = next((w for w in self.wikis()["wikis"] if w["id"] == ident), None)
        if not wiki:
            raise BoardError("This wiki is outside the configured project.")
        return wiki

    def tree(self, payload):
        wiki = self.require_wiki(payload["wikiId"])
        root = self.wiki_request("GET", f"wikis/{quote(wiki['id'], safe='')}/pages",
            params={"path": "/", "recursionLevel": "full", "includeContent": "false"})["data"]
        pages = []
        def walk(node, depth=0):
            if depth > 50 or len(pages) >= 10000:
                raise BoardError("The wiki tree is too large for this view.")
            path = page_path(node["path"])
            if path != "/":
                pages.append({"path": path, "depth": depth - 1, "title": path.rsplit("/", 1)[-1]})
            for child in sorted(node.get("subPages", []), key=lambda p: p.get("order", 0)):
                walk(child, depth + 1)
        walk(root)
        return {"pages": pages}

    def normalize_page(self, wiki, path, response):
        row = response["data"]
        content = row.get("content", "")
        if len(content) > 250000:
            raise BoardError("The page is too large. Open it in Azure DevOps.")
        return {"wikiId": wiki["id"], "path": path, "content": content, "etag": response["etag"],
            "url": wiki["url"] + "?" + urlencode({"pagePath": path})}

    def attachment(self, payload):
        wiki = self.require_wiki(payload['wikiId'])
        name = payload['name']
        if not name or len(name) > 500 or name in ('.', '..') or any(c in name for c in ('/', '\\')) or any(ord(c) < 32 for c in name):
            raise BoardError('Invalid wiki attachment name.')
        metadata = self.wiki_request('GET', f"wikis/{wiki['id']}")['data']
        if metadata.get('projectId', '').lower() != self.policy.project_id:
            raise BoardError('This attachment is outside the configured project.')
        repository = str(UUID(metadata['repositoryId']))
        version = metadata.get('versions', [{}])[0].get('version')
        if not version:
            raise BoardError('The wiki repository version is unavailable.')
        route = f'repositories/{repository}/items'
        url = self.policy.route('git', route)
        self.policy.require('request', {'area': 'git', 'method': 'GET', 'path': route})
        with self.requests.get(url, headers={**self.headers, 'Accept': 'application/octet-stream'},
                params={'api-version': '7.1', 'path': '/.attachments/' + name, 'download': 'true', 'versionDescriptor.version': version, 'versionDescriptor.versionType': 'branch'},
                stream=True, timeout=15, allow_redirects=False) as response:
            if not response.ok:
                raise BoardError('The wiki attachment is unavailable.')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024 or time.monotonic() > self.deadline:
                    raise BoardError('The attachment is too large. Open it in Azure DevOps.')
                chunks.append(chunk)
        return {'content': base64.b64encode(b''.join(chunks)).decode(), 'contentType': mimetypes.guess_type(name)[0] or 'application/octet-stream'}

    def page(self, payload):
        wiki = self.require_wiki(payload["wikiId"])
        path = page_path(payload["path"])
        response = self.wiki_request("GET", f"wikis/{quote(wiki['id'], safe='')}/pages", params={"path": path, "includeContent": "true"})
        return self.normalize_page(wiki, path, response)

    def publish(self, payload):
        wiki = self.require_wiki(payload["wikiId"])
        if wiki["type"] != "projectWiki":
            raise BoardError("Edit code wikis in their Git repository.")
        path = page_path(payload["path"])
        if path == "/" or not payload["content"].strip() or len(payload["content"]) > 250000:
            raise BoardError("Provide a page path and reviewed content.")
        route = f"wikis/{quote(wiki['id'], safe='')}/pages"
        # Always validate current server state; cached pages never authorize edits.
        current = self.wiki_request("GET", route, params={"path": path, "includeContent": "false"}, missing=True)
        expected = payload.get("etag", "")
        if current and (not expected or current["etag"] != expected):
            raise BoardError("The page already exists or has changed. Refresh it before publishing.")
        if not current and expected:
            raise BoardError("The page no longer exists. Refresh the wiki.")
        result = self.wiki_request("PUT", route, {"content": payload["content"]},
            params={"path": path, "comment": self.policy.wiki_update_comment if current else self.policy.wiki_create_comment}, etag=expected)
        result["data"]["content"] = payload["content"]
        return self.normalize_page(wiki, path, result)


def dispatch_wiki(action, payload, cache, *, factory=None):
    if not callable(factory):
        raise ValueError("An explicit scoped wiki factory is required.")
    if action == "wiki-publish":
        result = factory().publish(payload)
        if cache:
            try:
                cache.invalidate()
            except OSError:
                result["warning"] = "Published in DevOps, but the saved snapshot could not be invalidated. Use Refresh."
        return result
    if action == 'wiki-attachment':
        return factory().attachment(payload)
    handlers = {"wiki-list": "wikis", "wiki-tree": "tree", "wiki-page": "page"}
    if action not in handlers:
        raise BoardError("Unknown wiki action.")
    def load():
        client = factory()
        return client.wikis() if action == "wiki-list" else getattr(client, handlers[action])(payload)
    from .cache import cached_read
    return cached_read(cache, {"action": action, **{k: v for k, v in payload.items() if k != "force"}}, load,
        ttl=300 if action == "wiki-page" else 900, force=bool(payload.get("force")))
