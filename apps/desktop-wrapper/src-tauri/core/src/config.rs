//! The configuration `scripts/configure.py` generates (`generated/app.json`). The script is the
//! one validator; this side parses the result strictly (unknown keys are an error, so the two cannot
//! drift apart unnoticed) and re-checks what a wrong value would make dangerous.

use serde::Deserialize;
use url::Url;

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WindowConfig {
    pub width: f64,
    pub height: f64,
    pub min_width: f64,
    pub min_height: f64,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppConfig {
    pub name: String,
    pub id: String,
    pub identifier: String,
    pub version: String,
    pub url: String,
    pub internal_hosts: Vec<String>,
    pub window: WindowConfig,
    pub notifications: bool,
    pub hide_on_close: bool,
}

impl AppConfig {
    pub fn parse(text: &str) -> Result<Self, String> {
        let config: Self = serde_json::from_str(text).map_err(|error| format!("invalid app config: {error}"))?;
        config.target()?;
        if config.name.trim().is_empty() {
            return Err("invalid app config: empty name".into());
        }
        Ok(config)
    }

    /// The address the window loads: https, or http on a loopback host.
    pub fn target(&self) -> Result<Url, String> {
        let url = Url::parse(&self.url).map_err(|error| format!("invalid url {:?}: {error}", self.url))?;
        let host = url.host_str().ok_or_else(|| format!("url {:?} has no host", self.url))?;
        let loopback = matches!(host, "localhost" | "127.0.0.1" | "[::1]");
        match url.scheme() {
            "https" => {}
            "http" if loopback => {}
            other => return Err(format!("url scheme {other:?} is not allowed (https, or http on a loopback host)")),
        }
        if !url.username().is_empty() || url.password().is_some() {
            return Err("the url must not contain credentials".into());
        }
        Ok(url)
    }
}
