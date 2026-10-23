//! The parts of the desktop wrapper that need no GUI: the generated configuration, the rule that
//! decides which pages stay in the app window, and the limits applied to notifications. They live in
//! their own crate so their tests run anywhere, without the platform web view libraries.

pub mod config;
pub mod files;
pub mod notify;
pub mod policy;
