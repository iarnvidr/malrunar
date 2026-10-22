include!("src/commands.rs");

fn main() {
    println!("cargo:rerun-if-changed=generated/app.json");
    println!("cargo:rerun-if-changed=src/commands.rs");
    if !std::path::Path::new("generated/app.json").is_file() {
        panic!("generated/app.json is missing: run `python3 scripts/configure.py` in apps/desktop-wrapper first (npm run build does)");
    }
    let manifest = tauri_build::AppManifest::new().commands(COMMANDS);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest))
        .expect("failed to run tauri-build");
}
