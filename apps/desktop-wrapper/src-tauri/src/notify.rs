//! Native notifications and the Dock badge: the commands the injected `Notification` and
//! `navigator.setAppBadge` call. Text is limited and the rate capped (see `desktop_wrapper_core::notify`).
//!
//! macOS uses mac-notification-sys directly because it can wait for the click on a banner; Tauri's
//! notification plugin cannot. Other systems show a plain notification (clicking it only focuses the
//! app through the system).

use std::{sync::Mutex, time::Instant};

use desktop_wrapper_core::notify::{sanitize, RateLimiter};
use tauri::{AppHandle, Manager, State, WebviewWindow};

use crate::MAIN_WINDOW;

pub struct Notifier {
    app_name: String,
    limiter: Mutex<RateLimiter>,
}

impl Notifier {
    pub fn new(app_name: String) -> Self {
        Self { app_name, limiter: Mutex::new(RateLimiter::default()) }
    }
}

/// Call once at startup, before the first notification.
pub fn init(identifier: &str) {
    // Without this the banner would be attributed to Finder.
    #[cfg(target_os = "macos")]
    if let Err(error) = mac_notification_sys::set_application(identifier) {
        eprintln!("could not set notification identity: {error}");
    }
    #[cfg(not(target_os = "macos"))]
    let _ = identifier;
}

/// Brings the window back and tells the page which notification was clicked.
fn clicked(app: &AppHandle, id: &str) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
        // `id` is a number the shim made; it is re-encoded as a JSON string anyway.
        let id = serde_json::Value::String(id.to_owned());
        let _ = window.eval(format!("window.__wrapperNotificationClick && window.__wrapperNotificationClick({id})"));
    }
}

#[cfg(target_os = "macos")]
fn show(app: &AppHandle, id: String, title: String, body: String) {
    use mac_notification_sys::{Notification, NotificationResponse};

    let app = app.clone();
    // send() blocks until the banner is clicked or goes away, so it gets its own thread.
    std::thread::spawn(move || {
        let response = Notification::new().title(&title).message(&body).default_sound().wait_for_click(true).send();
        match response {
            Ok(NotificationResponse::Click) => clicked(&app, &id),
            Ok(_) => {}
            Err(error) => eprintln!("could not show notification: {error}"),
        }
    });
}

#[cfg(not(target_os = "macos"))]
fn show(_app: &AppHandle, _id: String, title: String, body: String) {
    if let Err(error) = notify_rust::Notification::new().summary(&title).body(&body).show() {
        eprintln!("could not show notification: {error}");
    }
}

#[tauri::command]
pub fn wrapper_notify(app: AppHandle, notifier: State<'_, Notifier>, id: String, title: String, body: String) -> Result<(), String> {
    if id.len() > 20 {
        return Err("invalid notification id".into());
    }
    if !notifier.limiter.lock().map_err(|_| "notification state poisoned")?.allow(Instant::now()) {
        return Err("too many notifications".into());
    }
    let (title, body) = sanitize(&notifier.app_name, &title, &body);
    show(&app, id, title, body);
    Ok(())
}

/// Sets the Dock badge text, or clears it with `None`. Only macOS has a badge.
#[tauri::command]
pub fn wrapper_badge(window: WebviewWindow, label: Option<String>) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let label = label.map(|text| text.chars().filter(|c| !c.is_control()).take(8).collect::<String>());
        window.set_badge_label(label).map_err(|error| error.to_string())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (window, label);
        Ok(())
    }
}
