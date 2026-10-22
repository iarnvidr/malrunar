//! Limits on what a page may show as a native notification. A page is remote content: it must not be
//! able to flood the desktop or send unbounded text to the notification centre.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

pub const MAX_TITLE_CHARS: usize = 120;
pub const MAX_BODY_CHARS: usize = 500;
/// At most this many notifications in any window of `RATE_WINDOW`.
pub const RATE_LIMIT: usize = 10;
pub const RATE_WINDOW: Duration = Duration::from_secs(10);

/// What is shown: the title falls back to the app name, control characters are dropped and long text
/// is cut (on a character boundary, with an ellipsis).
pub fn sanitize(app_name: &str, title: &str, body: &str) -> (String, String) {
    let title = clean(title, MAX_TITLE_CHARS);
    let title = if title.is_empty() { clean(app_name, MAX_TITLE_CHARS) } else { title };
    (title, clean(body, MAX_BODY_CHARS))
}

fn clean(text: &str, max_chars: usize) -> String {
    let text: String = text
        .chars()
        .map(|c| if c == '\n' || c == '\t' { ' ' } else { c })
        .filter(|c| !c.is_control())
        .collect();
    let text = text.trim();
    if text.chars().count() <= max_chars {
        return text.to_owned();
    }
    let mut cut: String = text.chars().take(max_chars - 1).collect();
    cut.push('\u{2026}');
    cut
}

/// A sliding-window limit, with the clock passed in so tests need no sleeping.
#[derive(Debug, Default)]
pub struct RateLimiter {
    recent: VecDeque<Instant>,
}

impl RateLimiter {
    pub fn allow(&mut self, now: Instant) -> bool {
        while let Some(first) = self.recent.front() {
            if now.saturating_duration_since(*first) >= RATE_WINDOW {
                self.recent.pop_front();
            } else {
                break;
            }
        }
        if self.recent.len() >= RATE_LIMIT {
            return false;
        }
        self.recent.push_back(now);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_title_falls_back_to_the_app_name() {
        assert_eq!(sanitize("Example", "  ", "hello"), ("Example".into(), "hello".into()));
        assert_eq!(sanitize("Example", "Mail", ""), ("Mail".into(), String::new()));
    }

    #[test]
    fn control_characters_are_removed_and_newlines_flattened() {
        let (title, body) = sanitize("Example", "a\u{0}b\u{7}c", "line1\nline2\u{1b}[31m");
        assert_eq!(title, "abc");
        assert_eq!(body, "line1 line2[31m");
    }

    #[test]
    fn long_text_is_cut_on_a_character_boundary() {
        let long = "\u{e5}".repeat(1000);
        let (title, body) = sanitize("Example", &long, &long);
        assert_eq!(title.chars().count(), MAX_TITLE_CHARS);
        assert!(title.ends_with('\u{2026}'));
        assert_eq!(body.chars().count(), MAX_BODY_CHARS);
    }

    #[test]
    fn a_burst_is_limited_and_recovers() {
        let start = Instant::now();
        let mut limiter = RateLimiter::default();
        for _ in 0..RATE_LIMIT {
            assert!(limiter.allow(start));
        }
        assert!(!limiter.allow(start + Duration::from_secs(5)));
        assert!(limiter.allow(start + RATE_WINDOW));
    }
}
