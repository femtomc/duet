//! `codebased` – Codebase daemon built on the Duet runtime.

use duet::runtime::service::Service;
use duet::runtime::{Control, RuntimeConfig};
use std::env;
use std::io::{self, BufWriter};
use std::path::PathBuf;

fn main() -> io::Result<()> {
    let mut args = env::args().skip(1);
    let mut root: Option<PathBuf> = None;
    let mut init_storage = true;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--root" => {
                let path = args
                    .next()
                    .unwrap_or_else(|| panic!("--root requires a path argument"));
                root = Some(PathBuf::from(path));
            }
            "--no-init" => {
                init_storage = false;
            }
            "--stdio" => {
                // Stdio is the default transport; accept the flag for compatibility.
            }
            "--help" | "-h" => {
                print_usage();
                return Ok(());
            }
            other => {
                eprintln!("Unknown argument: {other}");
                print_usage();
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "invalid command-line argument",
                ));
            }
        }
    }

    let mut config = RuntimeConfig::default();
    if let Some(root_path) = root {
        config.root = root_path;
    }

    let control = if init_storage {
        Control::init(config.clone())
            .and_then(|_| Control::new(config))
            .map_err(to_io_error)?
    } else {
        Control::new(config).map_err(to_io_error)?
    };

    let stdin = io::stdin();
    let stdout = io::stdout();
    let reader = stdin.lock();
    let writer = BufWriter::new(stdout.lock());

    let mut service = Service::new(control, writer);
    service.run(reader)
}

fn print_usage() {
    eprintln!(
        "Usage: codebased [--root PATH] [--no-init] [--stdio]\n\
         \n\
         Options:\n\
           --root PATH   Use PATH as the runtime root (default: .duet)\n\
           --no-init     Skip storage initialization (assumes existing data)\n\
           --stdio       Communicate over stdin/stdout (default)\n"
    );
}

fn to_io_error(error: duet::runtime::error::RuntimeError) -> io::Error {
    io::Error::new(io::ErrorKind::Other, error.to_string())
}
