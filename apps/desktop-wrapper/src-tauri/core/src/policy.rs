//! Which pages load inside the app window. Everything else opens in the system browser. The window
//! is a wrapper around one site: that site, the sign-in hosts it names, and nothing more.

use url::Url;

/// The pages allowed in the app window.
#[derive(Debug, Clone)]
pub struct Policy {
    host: String,
    scheme: String,
    port: Option<u16>,
    /// Exact hosts (`login.example.com`) or subdomain patterns (`*.example.com`, which does not
    /// match `example.com` itself). Always https on the default port.
    extra_hosts: Vec<String>,
}

impl Policy {
    pub fn new(target: &Url, extra_hosts: &[String]) -> Self {
        Self {
            host: target.host_str().unwrap_or_default().to_ascii_lowercase(),
            scheme: target.scheme().to_owned(),
            port: target.port_or_known_default(),
            extra_hosts: extra_hosts.iter().map(|host| host.to_ascii_lowercase()).collect(),
        }
    }

    fn is_main_origin(&self, url: &Url) -> bool {
        url.scheme() == self.scheme
            && url.port_or_known_default() == self.port
            && url.host_str().map(str::to_ascii_lowercase).as_deref() == Some(self.host.as_str())
    }

    fn is_extra_host(&self, url: &Url) -> bool {
        if url.scheme() != "https" || url.port_or_known_default() != Some(443) {
            return false;
        }
        let Some(host) = url.host_str().map(str::to_ascii_lowercase) else { return false };
        self.extra_hosts.iter().any(|pattern| host_matches(pattern, &host))
    }

    /// May this URL load in the window (a navigation or a frame)? The local connection-check page
    /// (`tauri:`), `about:` and `blob:` documents are always part of the app.
    pub fn is_internal(&self, url: &Url) -> bool {
        match url.scheme() {
            "tauri" | "about" | "blob" => true,
            "http" | "https" => self.is_main_origin(url) || self.is_extra_host(url),
            _ => false,
        }
    }

    /// Should a link that opens a new window load in the main window? The same rule: other sites go
    /// to the system browser.
    pub fn is_app_window_url(&self, url: &Url) -> bool {
        self.is_internal(url)
    }
}

/// `pattern` is an exact host or `*.suffix`; both arguments are lower case.
pub fn host_matches(pattern: &str, host: &str) -> bool {
    match pattern.strip_prefix("*.") {
        Some(suffix) => host.len() > suffix.len() + 1 && host.ends_with(suffix) && host[..host.len() - suffix.len()].ends_with('.'),
        None => pattern == host,
    }
}

/// Only web, mail and phone links are handed to the operating system.
pub fn may_open_externally(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https" | "mailto" | "tel")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy(extra: &[&str]) -> Policy {
        let extra: Vec<String> = extra.iter().map(|host| host.to_string()).collect();
        Policy::new(&Url::parse("https://app.example.com/").unwrap(), &extra)
    }

    fn internal(policy: &Policy, url: &str) -> bool {
        policy.is_internal(&Url::parse(url).unwrap())
    }

    #[test]
    fn the_configured_site_stays_in_the_app() {
        let policy = policy(&[]);
        assert!(internal(&policy, "https://app.example.com/inbox?x=1#y"));
        assert!(internal(&policy, "https://APP.example.com:443/"));
        assert!(internal(&policy, "about:srcdoc"));
        assert!(internal(&policy, "blob:https://app.example.com/1234"));
        assert!(internal(&policy, "tauri://localhost/index.html"));
    }

    #[test]
    fn other_origins_and_look_alikes_do_not() {
        let policy = policy(&[]);
        assert!(!internal(&policy, "https://example.com/"));
        assert!(!internal(&policy, "https://app.example.com.evil.example/"));
        assert!(!internal(&policy, "https://evilapp.example.com/"));
        assert!(!internal(&policy, "https://sub.app.example.com/"));
        assert!(!internal(&policy, "http://app.example.com/"));
        assert!(!internal(&policy, "https://app.example.com:8443/"));
        assert!(!internal(&policy, "file:///etc/passwd"));
        assert!(!internal(&policy, "javascript:alert(1)"));
        assert!(!internal(&policy, "data:text/html,hi"));
    }

    #[test]
    fn named_sign_in_hosts_are_https_only() {
        let policy = policy(&["login.example.org", "*.idp.example.net"]);
        assert!(internal(&policy, "https://login.example.org/auth"));
        assert!(internal(&policy, "https://tenant.idp.example.net/"));
        assert!(internal(&policy, "https://a.b.idp.example.net/"));
        assert!(!internal(&policy, "https://idp.example.net/"));
        assert!(!internal(&policy, "https://xidp.example.net/"));
        assert!(!internal(&policy, "http://login.example.org/"));
        assert!(!internal(&policy, "https://login.example.org:8443/"));
        assert!(!internal(&policy, "https://login.example.org.evil.example/"));
    }

    #[test]
    fn loopback_http_target_keeps_its_port() {
        let policy = Policy::new(&Url::parse("http://127.0.0.1:8080/").unwrap(), &[]);
        assert!(internal(&policy, "http://127.0.0.1:8080/page"));
        assert!(!internal(&policy, "http://127.0.0.1:8081/"));
        assert!(!internal(&policy, "https://127.0.0.1:8080/"));
    }

    #[test]
    fn wildcard_matching() {
        assert!(host_matches("*.example.com", "a.example.com"));
        assert!(host_matches("*.example.com", "a.b.example.com"));
        assert!(!host_matches("*.example.com", "example.com"));
        assert!(!host_matches("*.example.com", ".example.com"));
        assert!(!host_matches("*.example.com", "badexample.com"));
        assert!(host_matches("example.com", "example.com"));
    }

    #[test]
    fn only_safe_schemes_leave_the_app() {
        for url in ["https://x.example/", "http://x.example/", "mailto:a@example.com", "tel:+4512345678"] {
            assert!(may_open_externally(&Url::parse(url).unwrap()), "{url}");
        }
        for url in ["file:///Applications", "javascript:1", "ssh://host/", "smb://host/share", "data:text/plain,x"] {
            assert!(!may_open_externally(&Url::parse(url).unwrap()), "{url}");
        }
    }
}
