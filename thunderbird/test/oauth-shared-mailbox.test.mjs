import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const [modulePath] = process.argv.slice(2);
assert.ok(modulePath, "pass the patched Thunderbird OAuth2Module path");
const primaryEmail = "user@example.invalid";
const sharedEmail = "shared@example.invalid";
const smtpScope = "https://outlook.office.com/SMTP.Send";
const scopes = `IMAP.AccessAsUser.All ${smtpScope} offline_access`;
const login = { username: primaryEmail, httpRealm: scopes, password: "existing-refresh" };
const modifications = [];
if (!Set.prototype.isSupersetOf) {
  Object.defineProperty(Set.prototype, "isSupersetOf", {
    value(other) { return [...other].every(value => this.has(value)); },
  });
}

globalThis.console.createInstance = () => ({ debug() {}, info() {}, warn() {}, error() {} });
globalThis.ChromeUtils = { generateQI: () => () => {} };
globalThis.Ci = { nsIWritablePropertyBag: "bag", nsILoginInfo: "login", nsIMsgAsyncPrompter: "prompter" };
globalThis.Cc = new Proxy({}, {
  get(_target, name) {
    if (name === "@mozilla.org/hash-property-bag;1") {
      return { createInstance: () => ({ values: {}, setProperty(key, value) { this.values[key] = value; } }) };
    }
    if (name === "@mozilla.org/messenger/msgAsyncPrompter;1") {
      return { getService: () => ({ queueAsyncAuthPrompt(_key, _first, listener) {
        listener.onPromptAuthAvailable({ onAuthResult() {} });
      } }) };
    }
    throw new Error(`Unexpected XPCOM service ${name}`);
  },
});
globalThis.OAuth2 = class {
  constructor(scope, details) {
    this.scope = scope;
    this.authorizationEndpoint = details.authorizationEndpoint;
    this.accessToken = "test-access-token";
    this.refreshToken = "existing-refresh";
  }
  connect() { return Promise.resolve(); }
  static clearState() {}
};
globalThis.OAuth2CustomDetails = {
  fromIncomingServer: () => null,
  fromOutgoingServer: () => null,
};
globalThis.OAuth2Providers = {
  getHostnameDetails: (_hostname, type) => ({
    issuer: "login.microsoftonline.com",
    allScopes: scopes,
    requiredScopes: type === "smtp" ? smtpScope : "IMAP.AccessAsUser.All",
  }),
  getIssuerDetails: () => ({ clientId: "test-client", authorizationEndpoint: "https://login.microsoftonline.com/auth" }),
};
globalThis.enforcePrimaryPassword = () => true;
// Thunderbird 158 builds the XOAUTH2 string through MailStringUtils (UTF-8 to a byte string).
globalThis.MailStringUtils = { stringToByteString: text => Buffer.from(text, "utf8").toString("latin1") };
globalThis.Services = {
  prefs: { getBoolPref: () => false },
  logins: {
    async searchLoginsAsync() { return [login]; },
    async modifyLoginAsync(current, properties) { modifications.push({ current, properties: properties.values }); },
    async addLoginAsync() { throw new Error("A second refresh-token login must not be created"); },
    async removeLoginAsync() { throw new Error("The primary refresh-token login must not be removed"); },
  },
  tm: { spinEventLoopUntilOrQuit() {} },
  strings: { createBundle: () => ({ formatStringFromName: () => "OAuth" }) },
  obs: { addObserver() {}, removeObserver() {} },
};

let source = await readFile(modulePath, "utf8");
source = source.replace(/^import .*;\n/gm, "").replace("export function OAuth2Module() {}", "function OAuth2Module() {}");
source += "\nglobalThis.__OAuth2Module = OAuth2Module;\n";
vm.runInThisContext(source, { filename: modulePath });

const makeServer = (username, authUsername = "") => ({
  username,
  hostname: "outlook.office365.com",
  type: "imap",
  getStringValue(key) {
    assert.equal(key, "oauth2.username");
    return authUsername;
  },
});

const primary = new globalThis.__OAuth2Module();
assert.equal(primary.initFromMail(makeServer(primaryEmail)), true);
const shared = new globalThis.__OAuth2Module();
assert.equal(shared.initFromMail(makeServer(sharedEmail, primaryEmail)), true);
assert.equal(primary._oauthUsername, primaryEmail);
assert.equal(shared._oauthUsername, primaryEmail);
assert.equal(shared._username, sharedEmail, "XOAUTH2 must retain the shared mailbox username");
assert.equal(shared._oauth, primary._oauth, "both mailbox accounts must share one rotating OAuth object");
const smtp = new globalThis.__OAuth2Module();
assert.equal(smtp.initFromOutgoing({
  serverURI: { host: "outlook.office365.com" },
  username: primaryEmail,
  type: "smtp",
}), true);
assert.equal(smtp._username, primaryEmail, "outgoing OAuth must authenticate as the existing primary account");
assert.equal(smtp._oauth, primary._oauth, "SMTP.Send must reuse the existing account OAuth object and refresh token");

// Microsoft can return preferred_username after an interactive OAuth flow.
// The stored OAuth/login identity follows the credential owner while SASL
// continues to identify the mailbox being opened.
shared._setUsernamesFromAccessToken(primaryEmail);
assert.equal(shared._oauth.username, primaryEmail);
assert.equal(shared._oauthUsername, primaryEmail);
assert.equal(shared._username, sharedEmail, "a primary-account JWT must not replace the shared SASL username");
primary._setUsernamesFromAccessToken(primaryEmail);
assert.equal(primary._username, primaryEmail, "ordinary account semantics remain unchanged");

await shared.setRefreshToken("rotated-refresh");
assert.equal(modifications.length, 1);
assert.equal(modifications[0].current.username, primaryEmail, "rotated credentials must update the existing primary login");
assert.equal(modifications[0].properties.password, "rotated-refresh");

const sasl = await new Promise((resolve, reject) => {
  shared.connect(false, { onSuccess: resolve, onFailure: reject });
});
const decoded = Buffer.from(sasl, "base64").toString();
assert.equal(decoded, `user=${sharedEmail}\x01auth=Bearer test-access-token\x01\x01`);
console.log("Thunderbird shared OAuth identity and SASL mailbox mapping passed.");
