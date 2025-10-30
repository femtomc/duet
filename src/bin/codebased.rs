//! `codebased` – Codebase daemon built on the Duet runtime.

use duet::codebase;
use duet::runtime::service::Service;
use duet::runtime::{Control, RuntimeConfig};
use std::env;
use std::io::{self, BufReader, BufWriter};
use std::net::TcpListener;
use std::path::PathBuf;

fn main() -> io::Result<()> {
    let mut args = env::args().skip(1);
    let mut root: Option<PathBuf> = None;
    let mut init_storage = true;
    let mut listen_addr: Option<String> = None;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--root" => {
                let path = match args.next() {
                    Some(path) => path,
                    None => {
                        eprintln!("--root requires a path argument");
                        print_usage();
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidInput,
                            "missing value for --root",
                        ));
                    }
                };
                root = Some(PathBuf::from(path));
            }
            "--no-init" => {
                init_storage = false;
            }
            "--stdio" => {
                // Stdio is the default transport; accept the flag for compatibility.
            }
            "--listen" => {
                let addr = match args.next() {
                    Some(addr) => addr,
                    None => {
                        eprintln!("--listen requires an address argument");
                        print_usage();
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidInput,
                            "missing value for --listen",
                        ));
                    }
                };
                listen_addr = Some(addr);
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

    let workspace_root = config.root.clone();

    let mut control = if init_storage {
        Control::init(config.clone())
            .and_then(|_| Control::new(config))
            .map_err(to_io_error)?
    } else {
        Control::new(config).map_err(to_io_error)?
    };

    if let Err(err) = codebase::ensure_workspace_entity(&mut control, &workspace_root) {
        eprintln!("Failed to ensure workspace entity: {err}");
    }

    if let Err(err) = codebase::ensure_claude_agent(&mut control) {
        eprintln!("Failed to ensure Claude agent: {err}");
    }

    if let Err(err) = codebase::ensure_codex_agent(&mut control) {
        eprintln!("Failed to ensure Codex agent: {err}");
    }

    if let Err(err) = codebase::ensure_harness_agent(&mut control) {
        eprintln!("Failed to ensure harness agent: {err}");
    }

    if let Some(addr) = listen_addr {
        return run_tcp(control, &addr);
    }

    run_stdio(control)
}

fn run_stdio(control: Control) -> io::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let reader = stdin.lock();
    let writer = BufWriter::new(stdout.lock());

    let mut service = Service::new(control);
    service.handle(reader, writer)
}

fn run_tcp(control: Control, addr: &str) -> io::Result<()> {
    let listener = TcpListener::bind(addr)?;
    let actual = listener.local_addr()?;
    eprintln!("codebased listening on {}", actual);

    let mut service = Service::new(control);
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                let peer = stream.peer_addr().ok();
                let reader = BufReader::new(stream.try_clone()?);
                let writer = BufWriter::new(stream);
                if let Err(err) = service.handle(reader, writer) {
                    eprintln!("connection error from {:?}: {}", peer, err);
                }
            }
            Err(err) => {
                eprintln!("failed to accept connection: {err}");
            }
        }
    }

    Ok(())
}

fn print_usage() {
    eprintln!(
        "Usage: codebased [--root PATH] [--no-init] [--stdio] [--listen ADDR]\n\
         \n\
         Options:\n\
           --root PATH   Runtime root directory (default: nearest .duet folder)\n\
           --no-init     Skip storage initialization (assumes existing data)\n\
           --stdio       Communicate over stdin/stdout (default)\n\
           --listen ADDR Listen on TCP ADDR instead of stdio\n"
    );
}

fn to_io_error(error: duet::runtime::error::RuntimeError) -> io::Error {
    io::Error::new(io::ErrorKind::Other, error.to_string())
}
