#!/usr/bin/env bash
# Apply the build-time patches to a built msteams-mcp checkout and prove that each one landed.
#   apply.sh <package dir>        (the directory that holds dist/)
# Fails (non-zero) when a patch no longer applies to the pinned upstream commit, when a patch changed
# nothing, or when an assumption the component relies on is no longer true.
set -euo pipefail
pkg="${1:?usage: apply.sh <package dir>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dist="$pkg/dist"
[ -d "$dist" ] || { echo "msteams-mcp patches: $dist does not exist (build the package first)" >&2; exit 1; }

# The order matters only for readability: each patch touches its own statements.
patches=(browser-firefox login-timeout substrate-token refresh-origin refresh-expired)
tree_hash() { (cd "$dist" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1); }

for name in "${patches[@]}"; do
    before="$(tree_hash)"
    node "$here/$name.cjs" "$pkg" >/dev/null
    after="$(tree_hash)"
    if [ "$before" = "$after" ]; then
        echo "msteams-mcp patches: $name.cjs changed nothing; it does not apply to this upstream build" >&2
        exit 1
    fi
    echo "applied $name"
done

fail() { echo "msteams-mcp patches: $*" >&2; exit 1; }
has() { grep -qF -- "$2" "$dist/$1" || fail "expected '$2' in dist/$1 after patching"; }

has browser/context.js "import { firefox } from 'playwright';"
has browser/context.js "firefox.launchPersistentContext(BROWSER_PROFILE_DIR"
has browser/context.js "PLAYWRIGHT_FIREFOX_EXECUTABLE_PATH"
has tools/auth-tools.js "if (!input.forceNew) {"
has cli.js "MSTEAMS_LOGIN_TIMEOUT_MS"
has browser/auth.js "async function primeSubstrateToken(page)"
has browser/auth.js "MSTEAMS_TEAMS_URL"
has auth/token-extractor.js "newestTeamsCookie("
has auth/token-refresh-http.js "clientId === TEAMS_WEB_SPA_CLIENT_ID"
has auth/token-refresh.js "// patched: no unexpired token -> still try the HTTP refresh"

# Assumptions of the component (not patches): fail the build if upstream changes them.
# `info` and `debug` log to STDOUT, the stream the stdio MCP transport speaks on, and `info` is the
# default level: the launcher therefore runs the server with LOG_LEVEL=warn (README, "LOG_LEVEL").
grep -rqF 'console.log(`[${context}] ${message}`)' "$dist/utils/logger.js" \
    || fail 'expected info-level logging to go to stdout in dist/utils/logger.js; re-verify LOG_LEVEL handling in bin/malrunar-msteams-mcp'
grep -rq 'teams-mcp-server' "$dist/auth/session-store.js" || fail "expected the state directory name 'teams-mcp-server' in dist/auth/session-store.js"
grep -rq 'os.userInfo().username' "$dist/auth/crypto.js" || fail 'expected the hostname:username key derivation in dist/auth/crypto.js; re-verify the README section on the token cache key'
grep -rq 'StdioServerTransport' "$dist/server.js" || fail 'expected StdioServerTransport in dist/server.js'
if grep -rlq 'SSEServerTransport\|StreamableHTTPServerTransport' "$dist"; then
    fail 'an HTTP/SSE transport appeared in dist/; re-review the threat model before shipping it (the server is stdio only)'
fi
# What the optional modules import from the package.
grep -q '^export async function createBrowserContext' "$dist/browser/context.js" || fail 'createBrowserContext export is gone (libexec/browser-login.mjs)'
grep -q '^export async function closeBrowser' "$dist/browser/context.js" || fail 'closeBrowser export is gone (libexec/browser-login.mjs)'
grep -q '^export function writeSessionState' "$dist/auth/session-store.js" || fail 'writeSessionState export is gone (libexec/device-login.mjs)'
grep -q '^export function readSessionState' "$dist/auth/session-store.js" || fail 'readSessionState export is gone (libexec/sync-browser-session.mjs, device-login.mjs)'
grep -q '^export async function refreshTokensViaHttp' "$dist/auth/token-refresh-http.js" || fail 'refreshTokensViaHttp export is gone (libexec/device-login.mjs)'
echo 'msteams-mcp patches: all applied and verified'
