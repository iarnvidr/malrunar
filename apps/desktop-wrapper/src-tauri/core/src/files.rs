//! File name handling for downloads.

use std::path::{Path, PathBuf};

/// A path in `dir` for `name` that does not overwrite an existing file: `name`, then `stem (1).ext`,
/// `stem (2).ext`, and so on. Only the last component of `name` is used, so a name chosen by a remote
/// site cannot point outside `dir`.
pub fn unique_path(dir: &Path, name: &str) -> PathBuf {
    let name = Path::new(name).file_name().and_then(|n| n.to_str()).filter(|n| !n.is_empty() && *n != "..").unwrap_or("download");
    let candidate = dir.join(name);
    if !candidate.exists() {
        return candidate;
    }
    let stem = Path::new(name).file_stem().and_then(|s| s.to_str()).unwrap_or("download");
    let ext = Path::new(name).extension().and_then(|s| s.to_str());
    (1..)
        .map(|n| match ext {
            Some(ext) => dir.join(format!("{stem} ({n}).{ext}")),
            None => dir.join(format!("{stem} ({n})")),
        })
        .find(|path| !path.exists())
        .expect("unbounded range")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("desktop-wrapper-core-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn existing_files_are_never_overwritten() {
        let dir = scratch("unique");
        assert_eq!(unique_path(&dir, "a.pdf"), dir.join("a.pdf"));
        fs::write(dir.join("a.pdf"), b"x").unwrap();
        assert_eq!(unique_path(&dir, "a.pdf"), dir.join("a (1).pdf"));
        fs::write(dir.join("a (1).pdf"), b"x").unwrap();
        assert_eq!(unique_path(&dir, "a.pdf"), dir.join("a (2).pdf"));
        fs::write(dir.join("noext"), b"x").unwrap();
        assert_eq!(unique_path(&dir, "noext"), dir.join("noext (1)"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_name_cannot_leave_the_directory() {
        let dir = scratch("escape");
        assert_eq!(unique_path(&dir, "../../etc/passwd"), dir.join("passwd"));
        assert_eq!(unique_path(&dir, ".."), dir.join("download"));
        assert_eq!(unique_path(&dir, ""), dir.join("download"));
        fs::remove_dir_all(&dir).unwrap();
    }
}
