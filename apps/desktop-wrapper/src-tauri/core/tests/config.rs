use desktop_wrapper_core::{config::AppConfig, policy::Policy};

/// What `scripts/configure.py --check` prints for `wrapper.config.example.json`
/// (scripts/test_configure.py checks that it still does).
const FIXTURE: &str = include_str!("fixtures/app.json");

#[test]
fn the_generated_configuration_parses() {
    let config = AppConfig::parse(FIXTURE).unwrap();
    assert_eq!(config.name, "Example App");
    assert_eq!(config.identifier, "io.github.iarnvidr.example-app");
    assert_eq!(config.target().unwrap().as_str(), "https://app.example.com/");
    let policy = Policy::new(&config.target().unwrap(), &config.internal_hosts);
    assert!(policy.is_internal(&"https://login.example.com/".parse().unwrap()));
    assert!(policy.is_internal(&"https://t.idp.example.net/".parse().unwrap()));
    assert!(!policy.is_internal(&"https://elsewhere.example.org/".parse().unwrap()));
}

#[test]
fn unknown_keys_are_refused() {
    let text = FIXTURE.replacen('{', "{\"surprise\": 1,", 1);
    assert!(AppConfig::parse(&text).unwrap_err().contains("surprise"));
}

#[test]
fn unsafe_targets_are_refused() {
    for url in ["http://app.example.com/", "file:///etc/passwd", "javascript:1", "https://user:pw@app.example.com/", "not a url"] {
        let text = FIXTURE.replace("https://app.example.com/", url);
        assert!(AppConfig::parse(&text).is_err(), "{url}");
    }
    let loopback = FIXTURE.replace("https://app.example.com/", "http://127.0.0.1:8080/");
    assert!(AppConfig::parse(&loopback).is_ok());
}
