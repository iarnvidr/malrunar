// Desktop wrapper: a native window around one website.
//
// Nothing of the site is bundled. The only local content is `ui/index.html`, a connection check that
// shows a Retry page instead of a blank window when the site cannot be reached. What the site and
// the app name are comes from `generated/app.json`, written at build time by `scripts/configure.py`.
//
//   desktop-wrapper-core   configuration, navigation policy, notification limits (no GUI, tested anywhere)
//   notify.rs              native notifications and the Dock badge, the commands the page calls
//   scripts/*.js           what is injected into the page: links, and Notification / setAppBadge
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod notify;

use std::{collections::HashMap, path::PathBuf, sync::Mutex};

use desktop_wrapper_core::{
    config::AppConfig,
    files::unique_path,
    policy::{may_open_externally, Policy},
};
use tauri::{
    utils::config::BackgroundThrottlingPolicy,
    webview::{DownloadEvent, NewWindowResponse},
    AppHandle, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_opener::OpenerExt;
use url::Url;

pub const MAIN_WINDOW: &str = "main";
const APP_JSON: &str = include_str!("../generated/app.json");
const LINKS_JS: &str = include_str!("../scripts/links.js");
const NOTIFICATIONS_JS: &str = include_str!("../scripts/notifications.js");

/// Wraps an injected script so it sees only its `cfg` argument, and no names leak into the page.
fn injected(script: &str, cfg: &serde_json::Value) -> String {
    format!("(function (cfg) {{\n{script}\n}})({cfg});")
}

/// Lets the configured site's origin, and only that, call the commands in `commands.rs`.
fn grant_commands_to(app: &AppHandle, target: &Url) -> tauri::Result<()> {
    let permissions: Vec<String> =
        commands::COMMANDS.iter().map(|name| format!("allow-{}", name.replace('_', "-"))).collect();
    app.add_capability(
        serde_json::json!({
            "identifier": "site-origin",
            "windows": [MAIN_WINDOW],
            "local": false,
            "remote": { "urls": [format!("{}/*", target.origin().ascii_serialization())] },
            "permissions": permissions,
        })
        .to_string(),
    )
}

fn open_externally(app: &AppHandle, url: &Url) {
    if may_open_externally(url) {
        if let Err(error) = app.opener().open_url(url.as_str(), None::<&str>) {
            eprintln!("could not open {url} externally: {error}");
        }
    }
}

fn main() {
    let config = AppConfig::parse(APP_JSON).expect("generated/app.json is invalid: run scripts/configure.py");
    let target = config.target().expect("validated by AppConfig::parse");
    let policy = Policy::new(&target, &config.internal_hosts);

    let app = tauri::Builder::default()
        // Must be registered first. A second launch just focuses the first window.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_opener::init())
        .manage(notify::Notifier::new(config.name.clone()))
        .invoke_handler(tauri::generate_handler![notify::wrapper_notify, notify::wrapper_badge])
        .setup({
            let config = config.clone();
            move |app| {
                grant_commands_to(app.handle(), &target)?;
                notify::init(&config.identifier);

                // Chosen download destinations, so the finished file can be revealed even on macOS
                // where the finish event carries no path.
                let downloads: Mutex<HashMap<String, PathBuf>> = Mutex::new(HashMap::new());
                let downloads_dir = app
                    .path()
                    .download_dir()
                    .or_else(|_| app.path().home_dir().map(|home| home.join("Downloads")))
                    .ok();

                let nav_policy = policy.clone();
                let popup_policy = policy.clone();
                let popup_handle = app.handle().clone();
                let download_handle = app.handle().clone();

                // The connection-check page receives the real target as a parameter and redirects
                // there once the site answers.
                let mut start = Url::parse("tauri://localhost/index.html")?;
                start.query_pairs_mut().append_pair("target", target.as_str()).append_pair("name", &config.name);
                let local = format!("index.html?{}", start.query().unwrap_or_default());

                let mut hosts = vec![target.host_str().unwrap_or_default().to_ascii_lowercase()];
                if let Some(port) = target.port() {
                    hosts[0] = format!("{}:{port}", hosts[0]);
                }
                hosts.extend(config.internal_hosts.iter().map(|host| host.to_ascii_lowercase()));
                let page_config = serde_json::json!({ "hosts": hosts });

                let mut builder = WebviewWindowBuilder::new(app, MAIN_WINDOW, WebviewUrl::App(local.into()))
                    .title(config.name.as_str())
                    .inner_size(config.window.width, config.window.height)
                    .min_inner_size(config.window.min_width, config.window.min_height)
                    .initialization_script(injected(LINKS_JS, &page_config));
                if config.notifications {
                    builder = builder.initialization_script(injected(NOTIFICATIONS_JS, &page_config));
                }
                builder
                    // A call or a notification-bearing page keeps running while the window is hidden.
                    .background_throttling(BackgroundThrottlingPolicy::Disabled)
                    // Also called for frame loads and redirects, so anything not allowed is blocked
                    // here, never opened in the browser (link clicks reach the browser through
                    // links.js and on_new_window).
                    .on_navigation(move |url| {
                        let allowed = nav_policy.is_internal(url);
                        if !allowed {
                            eprintln!("blocked in-app navigation to {url}");
                        }
                        allowed
                    })
                    // target=_blank and window.open: the site loads in the main window, everything
                    // else in the system browser. No extra app windows.
                    .on_new_window(move |url, _features| {
                        if popup_policy.is_app_window_url(&url) {
                            if let Some(window) = popup_handle.get_webview_window(MAIN_WINDOW) {
                                let _ = window.navigate(url);
                            }
                        } else {
                            open_externally(&popup_handle, &url);
                        }
                        NewWindowResponse::Deny
                    })
                    // Webviews do not download on their own. Save to the Downloads folder and show
                    // the file when done.
                    .on_download(move |_webview, event| {
                        match event {
                            DownloadEvent::Requested { url, destination } => {
                                let name = destination.file_name().and_then(|n| n.to_str()).unwrap_or("download").to_owned();
                                if let Some(dir) = &downloads_dir {
                                    *destination = unique_path(dir, &name);
                                }
                                downloads.lock().unwrap().insert(url.to_string(), destination.clone());
                            }
                            DownloadEvent::Finished { url, path, success } => {
                                let stored = downloads.lock().unwrap().remove(url.as_str());
                                if success {
                                    if let Some(path) = path.or(stored) {
                                        let _ = download_handle.opener().reveal_item_in_dir(path);
                                    }
                                }
                            }
                            _ => {}
                        }
                        true
                    })
                    .build()?;
                Ok(())
            }
        })
        // On macOS, closing the window leaves the app running in the Dock, as Mac apps do, so
        // notifications keep arriving. Quit with Cmd+Q.
        .on_window_event({
            let hide_on_close = config.hide_on_close;
            move |window, event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    if cfg!(target_os = "macos") && hide_on_close {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the app");

    app.run(|app, event| {
        // Clicking the Dock icon while the window is hidden brings it back.
        #[cfg(target_os = "macos")]
        if let tauri::RunEvent::Reopen { has_visible_windows: false, .. } = event {
            if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
        #[cfg(not(target_os = "macos"))]
        let _ = (app, event);
    });
}
