# vendored copy - edit tools/identity, run scripts/sync-identity.sh
"""Forward-auth identity: who is the user, according to a trusted reverse proxy (standard library only).

A reverse proxy that authenticates users (a forward-auth gateway) tells the service behind it who the
user is in request headers. Those headers are only worth anything if the request really came from that
proxy, so this module trusts them only when the request passes the configured trust check, and treats
every other request as anonymous. The header names default to the common Remote-* convention and are all
configurable; nothing here names a product.

Settings (all read with the component's own prefix, e.g. MALRUNAR_GUAC_TUNNEL_):
  <PFX>USER_HEADER          header with the user name, default Remote-User
                            (<PFX>IDENTITY_HEADER is accepted as the older name of this setting)
  <PFX>GROUPS_HEADER        comma-separated groups, default Remote-Groups
  <PFX>NAME_HEADER          display name, default Remote-Name
  <PFX>EMAIL_HEADER         mail address, default Remote-Email
  <PFX>TRUSTED_PROXIES      comma-separated CIDRs (IPv4 and IPv6) the peer address must be in
  <PFX>PROXY_SECRET_HEADER  header in which the proxy sends a shared secret
  <PFX>PROXY_SECRET_FILE    file with the secret: one per line, at most two (the old and the new one
                            during a rotation); blank lines and lines starting with # are ignored. It
                            is read at startup and read again when its modification time changes.

Trust rule:
  * neither TRUSTED_PROXIES nor a secret configured: no identity header is trusted, everyone is anonymous
    (startup_warning() returns the text a component should log);
  * only one of the two configured: that one must pass;
  * BOTH configured: BOTH must pass (peer in a trusted CIDR AND a valid secret).
An untrusted request yields Identity(None, None, None, (), False): its identity headers are ignored.
Secrets are compared in constant time. scrub_headers() removes the secret header and the identity
headers from a header set before it is forwarded or logged.
"""
import hashlib
import hmac
import ipaddress
import os
import threading
from dataclasses import dataclass

DEFAULT_USER_HEADER = 'Remote-User'
DEFAULT_GROUPS_HEADER = 'Remote-Groups'
DEFAULT_NAME_HEADER = 'Remote-Name'
DEFAULT_EMAIL_HEADER = 'Remote-Email'
MAX_VALUE_LENGTH = 256
MAX_GROUPS_LENGTH = 4096
MAX_GROUPS = 128
MIN_SECRET_LENGTH = 16
MAX_SECRETS = 2


class ConfigError(ValueError):
    """A forward-auth setting is wrong; the message names the setting."""


@dataclass(frozen=True)
class Identity:
    user: str | None = None
    name: str | None = None
    email: str | None = None
    groups: tuple = ()
    trusted: bool = False

    @property
    def anonymous(self):
        return self.user is None


ANONYMOUS = Identity()


def _clean(value, limit=MAX_VALUE_LENGTH):
    """The stripped value, or None if it is empty, too long or has control characters."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit:
        return None
    if any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f or c in '\u2028\u2029' for c in value):
        return None
    return value


def _groups(value):
    value = _clean(value, MAX_GROUPS_LENGTH)
    if value is None:
        return ()
    seen = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            continue
        if len(part) > MAX_VALUE_LENGTH:
            return ()
        if part not in seen:
            seen.append(part)
    return tuple(seen) if len(seen) <= MAX_GROUPS else ()


def _header_name(name, setting):
    name = (name or '').strip()
    if not name or not all(c.isascii() and (c.isalnum() or c == '-') for c in name):
        raise ConfigError(f'{setting} must be a header name (letters, digits, hyphen)')
    return name


def _peer(peer_ip):
    if not peer_ip or not isinstance(peer_ip, str):
        return None
    try:
        address = ipaddress.ip_address(peer_ip.strip().split('%')[0])
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).digest()


def _read_secrets(path, setting):
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f'{setting}: cannot read {path}: {error.__class__.__name__}') from None
    secrets = [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
    if not secrets:
        raise ConfigError(f'{setting}: {path} holds no secret')
    if len(secrets) > MAX_SECRETS:
        raise ConfigError(f'{setting}: {path} holds more than {MAX_SECRETS} secrets')
    if any(len(secret) < MIN_SECRET_LENGTH for secret in secrets):
        raise ConfigError(f'{setting}: a secret is shorter than {MIN_SECRET_LENGTH} characters')
    return tuple(_digest(secret) for secret in secrets)


class IdentityConfig:
    """Trust settings and header names; resolve() turns a request into an Identity."""

    def __init__(self, user_header=DEFAULT_USER_HEADER, groups_header=DEFAULT_GROUPS_HEADER,
                 name_header=DEFAULT_NAME_HEADER, email_header=DEFAULT_EMAIL_HEADER,
                 trusted_proxies=(), secret_header=None, secret_file=None, prefix=''):
        self.user_header = _header_name(user_header, f'{prefix}USER_HEADER')
        self.groups_header = _header_name(groups_header, f'{prefix}GROUPS_HEADER')
        self.name_header = _header_name(name_header, f'{prefix}NAME_HEADER')
        self.email_header = _header_name(email_header, f'{prefix}EMAIL_HEADER')
        networks = []
        for item in trusted_proxies:
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                raise ConfigError(f'{prefix}TRUSTED_PROXIES: {item!r} is not an address or CIDR') from None
        self.trusted_proxies = tuple(networks)
        self.secret_header = _header_name(secret_header, f'{prefix}PROXY_SECRET_HEADER') if secret_header else None
        self.secret_file = secret_file or None
        if bool(self.secret_header) != bool(self.secret_file):
            raise ConfigError(f'{prefix}PROXY_SECRET_HEADER and {prefix}PROXY_SECRET_FILE go together: set both or neither')
        self._setting = f'{prefix}PROXY_SECRET_FILE'
        self._lock = threading.Lock()
        self._digests = ()
        self._stamp = None
        if self.secret_file:
            self._digests = _read_secrets(self.secret_file, self._setting)
            self._stamp = self._stat()

    def __repr__(self):  # never shows secrets
        return (f'IdentityConfig(mode={self.mode!r}, user_header={self.user_header!r}, '
                f'proxies={len(self.trusted_proxies)}, secret_header={self.secret_header!r})')

    @classmethod
    def from_mapping(cls, values, prefix=''):
        """Build from unprefixed settings: {'USER_HEADER': ..., 'TRUSTED_PROXIES': 'a, b', ...}."""
        def get(name):
            value = values.get(name)
            if value is None:
                return ''
            return ','.join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value).strip()
        proxies = [item.strip() for item in get('TRUSTED_PROXIES').split(',') if item.strip()]
        return cls(
            user_header=get('USER_HEADER') or get('IDENTITY_HEADER') or DEFAULT_USER_HEADER,
            groups_header=get('GROUPS_HEADER') or DEFAULT_GROUPS_HEADER,
            name_header=get('NAME_HEADER') or DEFAULT_NAME_HEADER,
            email_header=get('EMAIL_HEADER') or DEFAULT_EMAIL_HEADER,
            trusted_proxies=proxies,
            secret_header=get('PROXY_SECRET_HEADER') or None,
            secret_file=get('PROXY_SECRET_FILE') or None,
            prefix=prefix,
        )

    @classmethod
    def from_env(cls, env, prefix):
        keys = ('USER_HEADER', 'IDENTITY_HEADER', 'GROUPS_HEADER', 'NAME_HEADER', 'EMAIL_HEADER',
                'TRUSTED_PROXIES', 'PROXY_SECRET_HEADER', 'PROXY_SECRET_FILE')
        return cls.from_mapping({key: env[prefix + key] for key in keys if prefix + key in env}, prefix)

    @property
    def mode(self):
        """'none' (nothing trusted), 'proxies', 'secret' or 'both' (both must pass)."""
        if self.trusted_proxies and self.secret_header:
            return 'both'
        if self.trusted_proxies:
            return 'proxies'
        if self.secret_header:
            return 'secret'
        return 'none'

    def startup_warning(self):
        if self.mode != 'none':
            return None
        return ('forward-auth: neither trusted proxies nor a proxy secret is configured, so no identity '
                f'header ({self.user_header}, {self.groups_header}, {self.name_header}, {self.email_header}) '
                'is trusted and every request is anonymous')

    def _stat(self):
        try:
            info = os.stat(self.secret_file)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _secrets(self):
        if not self.secret_file:
            return ()
        stamp = self._stat()
        if stamp is not None and stamp != self._stamp:
            with self._lock:
                try:
                    self._digests = _read_secrets(self.secret_file, self._setting)
                except ConfigError:
                    pass  # keep the last good secrets while the file is being rewritten
                self._stamp = stamp
        return self._digests

    def sensitive_headers(self):
        """Lower-case names of the headers a proxy must never see from a client or a log."""
        names = {self.user_header, self.groups_header, self.name_header, self.email_header}
        if self.secret_header:
            names.add(self.secret_header)
        return frozenset(name.lower() for name in names)

    def scrub_headers(self, headers):
        """A copy of headers (name -> value) without the secret and the identity headers."""
        sensitive = self.sensitive_headers()
        return {name: value for name, value in headers.items() if name.lower() not in sensitive}

    def _trusted(self, peer_ip, lookup):
        if self.mode == 'none':
            return False
        if self.trusted_proxies:
            address = _peer(peer_ip)
            proxy_ok = address is not None and any(
                address.version == net.version and address in net for net in self.trusted_proxies)
        else:
            proxy_ok = True
        if self.secret_header:
            supplied = lookup.get(self.secret_header.lower())
            secret_ok = False
            if isinstance(supplied, str) and supplied.strip():
                given = _digest(supplied.strip())
                for known in self._secrets():  # no early exit: every secret is compared
                    secret_ok |= hmac.compare_digest(given, known)
        else:
            secret_ok = True
        return proxy_ok and secret_ok

    def resolve(self, peer_ip, headers):
        """The Identity of a request from peer_ip with headers (any mapping; names are case-insensitive)."""
        lookup = {str(name).lower(): value for name, value in (headers or {}).items()}
        if not self._trusted(peer_ip, lookup):
            return ANONYMOUS
        return Identity(
            user=_clean(lookup.get(self.user_header.lower())),
            name=_clean(lookup.get(self.name_header.lower())),
            email=_clean(lookup.get(self.email_header.lower())),
            groups=_groups(lookup.get(self.groups_header.lower())),
            trusted=True,
        )
