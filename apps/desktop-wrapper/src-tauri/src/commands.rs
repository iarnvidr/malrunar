// The commands the page may call. build.rs turns each into an `allow-<name>` permission and main.rs
// grants those to the configured site's origin, and to nothing else.
pub const COMMANDS: &[&str] = &["wrapper_notify", "wrapper_badge"];
