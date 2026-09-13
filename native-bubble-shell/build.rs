fn main() {
    // `tauri_build` embeds frontendDist in the executable, but Cargo otherwise
    // only sees Rust/build-script inputs.  Track every owned shell asset so an
    // offline `cargo build` cannot ship an older WebView after a JS/CSS edit.
    let paths = [
        "frontend/app.js",
        "frontend/index.html",
        "frontend/style.css",
        "frontend/text-render.js",
        "frontend/thought-cloud.svg",
        "tauri.conf.json",
    ];
    let mut revision: u64 = 0xcbf29ce484222325;
    for path in paths {
        println!("cargo:rerun-if-changed={path}");
        // Changing an embedded asset must also relink the application.  The
        // Tauri resource library alone is not a Rust source dependency, so a
        // content fingerprint is passed to rustc to invalidate the binary.
        for byte in std::fs::read(path).expect("owned native asset").iter() {
            revision ^= u64::from(*byte);
            revision = revision.wrapping_mul(0x100000001b3);
        }
    }
    println!("cargo:rustc-env=NATIVE_BUBBLE_FRONTEND_REV={revision:016x}");
    tauri_build::build()
}
