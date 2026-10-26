"""Read exact linked work items and PR metadata within an explicit project policy."""
import re
from concurrent.futures import ThreadPoolExecutor
from .errors import BoardError
from .policy import AzurePolicy
from uuid import UUID
from urllib.parse import unquote


def read_items(ids, *, policy=None, factory=None):
    if not isinstance(policy, AzurePolicy) or not callable(factory):
        raise ValueError("An explicit linked-item policy and provider factory are required.")
    policy.require("linked-report", {"ids": ids})
    if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError("Expected at most 100 positive work item IDs")
    if not ids:
        return {"items": [], "pullRequests": [], "warnings": []}
    client = factory()
    if client.policy != policy:
        raise BoardError("Linked-item factory returned another project policy.")
    # This endpoint only reads exact IDs; private topics are rejected before invoking this helper.
    policy.require('request', {'area': 'wit', 'method': 'GET', 'path': 'workitems'})
    response = client.requests.get(
        policy.route("wit", "workitems"),
        params={"ids": ",".join(map(str, ids)), "$expand": "relations", "api-version": "7.1"},
        headers=client.headers, timeout=20, allow_redirects=False,
    )
    response.raise_for_status()
    result = []
    pr_ids = set()
    for item in response.json().get("value", []):
        f = item.get("fields", {})
        normalized = client.normalize(item)
        result.append({key: normalized[key] for key in ("id", "title", "state", "assignedTo", "url")})
        # Parse references already stored in the linked public work item.
        # Workspace handoffs and private topic text are never sent to Azure.
        for relation in item.get("relations", []):
            url = unquote(relation.get("url", ""))
            if url.startswith("vstfs:///Git/PullRequestId/") and url.rsplit("/", 1)[-1].isdigit():
                pr_ids.add(int(url.rsplit("/", 1)[-1]))
        pr_ids.update(int(n) for n in re.findall(
            re.escape(policy.web_base) + r"/_git/[^\s\"<>]+/pullrequest/(\d+)", f.get("System.Description", ""), re.I))

    def pull_request(ident):
        try:
            policy.require("request", {"area": "git", "method": "GET", "path": f"pullrequests/{ident}"})
            r = client.requests.get(policy.route("git", f"pullrequests/{ident}"),
                                    params={"api-version": "7.1"}, headers=client.headers, timeout=8, allow_redirects=False)
            r.raise_for_status()
            p = r.json()
            if p.get('repository', {}).get('project', {}).get('id', '').lower() != policy.project_id:
                raise BoardError('Pull request is outside the configured project.')
            policy.require('pull-request', {'id': ident, 'projectId': policy.project_id})
            return {"id": ident, "title": p.get("title", ""), "status": p.get("status", "unknown"),
                    "url": f"{policy.web_base}/_git/{str(UUID(p['repository']['id']))}/pullrequest/{ident}"}
        except Exception:
            return {"id": ident, "title": "Pull request unavailable", "status": "unavailable", "url": ""}

    warnings = []
    if len(pr_ids) > 12:
        warnings.append("Showing the 12 most recent PR identifiers linked from these work items.")
    with ThreadPoolExecutor(max_workers=4) as pool:
        prs = list(pool.map(pull_request, sorted(pr_ids, reverse=True)[:12]))
    if any(p["status"] == "unavailable" for p in prs):
        warnings.append("Some linked PR states are unavailable; use the preserved work item references.")
    return {"items": result, "pullRequests": prs, "warnings": warnings}
