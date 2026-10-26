# DevOps service foundation

**UNPUBLISHED.** This is an opt-in source/API and dependency foundation, not a released image.
No consumer lock should reference a guessed digest or substitute a mutable tag.

The image packages the authenticated JSON RPC engine, durable account cache primitives,
Azure CLI/Python dependencies and the Paseo client SDK once. It contains no organization,
tenant, provider endpoint, Azure login, topic-index loader, application policy or credentials.
Paseo itself remains a separate upstream application. Boards, linked-item and wiki transport
are reusable modules; the consumer supplies explicit scope, policy, credential and topic hooks.

## API and security boundary

`Backend` requires keyword callbacks `runner(action, payload, cache)`, `cache_factory()`,
`topic_reader()`, `paseo_runner(action, payload)`, `linked_reader(ids)` and
`authorize(action, validated_payload)`. Missing callbacks fail construction. Authorization
must return exactly `True` for each operation; unknown actions/fields fail validation first.
The consumer supplies tenant/account selection in its credential callback and omits private
topics before returning topics. The core never reads an Azure profile or local topic index.
`error_type` identifies the adapter's sanitized exception class; never use unsanitized SDK
exceptions as owner-facing errors. `refresh_seconds` defaults to 300 (minimum 60).
`service_name` and `cache_description` are nonsecret status labels.

The API retains work-item revision checks and wiki ETag handling in the common providers,
operation-ID replay/uncertain-write refusal in the core, cache generations,
stale-read warnings and backlink membership checks. Read snapshots never authorize writes.
The process-local write journal lock does not support multiple writer processes sharing a
journal; run one service instance per account/cache. Provider plugins remain responsible for
their own revision and uncertain-write contract, including Paseo plugin operations.

`GET /health` reports process readiness only. `POST /rpc` requires an exact bearer token,
accepts `{action, payload, requestId}` JSON, limits request bodies to 1 MiB, emits no CORS
headers and never logs requests or provider exceptions. It is a backend for an owner-only
proxy, not a public browser endpoint. The consumer's owner/session/CSRF checks remain in
that proxy. Do not expose a token to browser code or forward private topic text to providers.

`DiskCache(root, namespace)` and `cached_read` are independent of account discovery.
Consumers must derive a stable account-specific namespace and explicit private root.
Operation results and caches are private state; mount only the owning account's directory.

## Boards, wiki and linked-item API

Construct `AzurePolicy` from `malrunar_devops.policy` with explicit `organization` (canonical
HTTPS Azure DevOps organization URL), `project` (exact name), `project_id` (UUID), and
`authorize(operation, resource)`. There are no organization, project, tenant or authorization
defaults. The hook must return exactly `True`. Supported operations are `connect`, `request`,
`item`, `wiki`, `link`, `linked-report`, and `pull-request`; unknown operations always fail.
The resource contains only the relevant organization/project, request method/area/path,
item ID/project, wiki ID/project ID, link ID/URL or linked IDs. It never contains credentials.
Optional `link_comment`, `wiki_update_comment`, and `wiki_create_comment` are consumer labels.

`AzureBoards(policy, token_provider=..., session=..., cache=..., force=...)` and
`AzureWiki(policy, token_provider=..., session=...)` use the explicit credential callback only
after connect authorization. The callback returns an access-token string; account/tenant
selection remains private. An injected HTTP session supports synthetic tests. The default
requests session disables ambient netrc/proxy authentication, and redirects are disabled.
Every request is rooted at the configured project; absolute/traversal routes are rejected.
Work items must have that exact TeamProject; wikis must have the configured project UUID.
Attachment repositories and linked PR results must also identify that UUID before exposure.
Responses missing those project claims fail closed. No provider error body is returned.

`board.dispatch(action, payload, cache, factory=..., wiki_factory=...)` accepts explicit scoped
client factories. `wiki.dispatch_wiki(..., factory=...)` has no default client. Writes always
use live state, revision/ETag checks and cache invalidation; a failed invalidation warns without
turning a confirmed write into a failure. `linked.read_items(ids, policy=..., factory=...)`
reads only up to 100 exact IDs and at most 12 related PR identifiers; it accepts no topic text.
The factory must return a client with the same policy. Private/excluded topics are filtered by
the consumer before deriving IDs or publishing workspace associations.

## Activation

Extend the eventual immutable service image, copy only your private adapter/configuration files,
and set `MALRUNAR_DEVOPS_ADAPTER` to an absolute trusted Python file defining
`create_backend() -> Backend`. Put that adapter directory on `PYTHONPATH` in addition to
`/opt/malrunar/devops-service/src`. Never use an untrusted writable adapter directory.
Set `MALRUNAR_DEVOPS_TOKEN_FILE` to an absolute owner-only regular file owned by the
runtime UID. The file must contain 32–4096 characters. No token or adapter default exists;
invalid configuration exits 78 without printing exception details.
`MALRUNAR_DEVOPS_LISTEN` defaults to `127.0.0.1`, `MALRUNAR_DEVOPS_PORT` to `18392`.
The default user is 10001:10001 and home is `/state`; a consumer can explicitly select its
existing UID and private state mounts. No Azure or Paseo credential is copied into the image.
Private JavaScript bridges can resolve the packaged SDK through an explicit `node_modules`
symlink to `/opt/malrunar/devops-service/node_modules`; the bridge remains consumer-owned.

## Dependencies and native boundary

The image uses the repository's pinned Ubuntu 26.04 base and apt snapshot, its native Python,
libffi/OpenSSL and the digest-pinned external Node component. The Python venv is built and
run on that same base; the image is self-contained, not a scratch payload to overlay on an
arbitrary distro. Do not copy `/usr/lib`, the venv or its native wheels across Python/distro
ABIs. A consumer needing an in-process composition must prove its own ABI contract first.

Direct pins are inherited unchanged: Azure CLI 2.91.0, azure-identity 1.26.0, requests 2.34.2;
Paseo client 0.11.1 and ws 8.22.0. npm transitive versions/integrities are locked and lifecycle
scripts/optional native addons are disabled. `requirements.lock` pins all 151 Python packages
and their official release SHA-256 hashes for CPython 3.14/Linux amd64. The Dockerfile rejects
another Python version/architecture and installs with `--require-hashes --only-binary=:all:`
then runs `pip check`. No source distribution or unlocked transitive dependency is built.
All 151 compatible wheels were downloaded in memory, hash-verified and their actual METADATA
audited for a complete target-marker dependency closure; none was installed or executed.
PyPI project JSON alone is insufficient for older packages whose wheel metadata differs.
The lock uses a manylinux 2.39 compatibility floor; Ubuntu 26.04 must supply a compatible glibc.
Real image imports, native linking and process behavior still require the image smoke test.

`lock-requirements.sh` regenerates the lock using already installed uv 0.13.5, public PyPI only,
no user config, netrc/keyring, Python download, package install or source build. It prints the
requirements to stdout; save/review the output as `requirements.lock`. Regeneration may
select newer transitive versions and requires review. Installing the checked-in hash lock
does not reselect those versions. The apt CA bootstrap follows the existing repository helper;
native packages otherwise come from the pinned Ubuntu snapshot, not from the Python lock.

Registry hashing, publication and PR smoke routing use the existing GitHub Actions tooling.
Publication remains subject to review. Local synthetic checks:

```sh
python3 -m unittest discover -s devops-service/test -p 'test_*.py'
```

Image smoke checks are defined but not run here.

## Build and publication

After reviewing/committing this source, run the existing hosted GitHub Actions path:

```sh
scripts/ci-try.sh devops-service --level smoke
```

That command pushes the committed revision to a temporary CI branch and dispatches `pr.yml`;
it never publishes. Its hosted build group runs `scripts/build.sh devops-service --load`, then
`scripts/smoke.sh devops-service`. Prerequisites are repository push/Actions access and the
existing hosted runner's public Ubuntu/PyPI/npm/GHCR connectivity. No provider login is needed.

Merging reviewed source to main triggers `publish.yml`, which builds with SBOM/max provenance,
signs the actual resulting digest and verifies the existing workflow identity. To explicitly
dispatch that same main-only workflow after review:

```sh
gh workflow run publish.yml --ref main --repo iarnvidr/malrunar
```

The current workflow matrix checks the other registered images too, skipping already published
content hashes. Record the actual new digest/signature and platform chain from that run before
changing any consumer lock. There is no published digest to consume at this source checkpoint.
