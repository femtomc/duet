//! Duet CLI - Command-line interface for the Duet runtime
//!
//! Provides subcommands for initializing, controlling, and inspecting
//! the Duet runtime.

use clap::{Parser, Subcommand};
use duet::runtime::{Runtime, RuntimeConfig, Result};
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "duet")]
#[command(about = "Causally consistent, time-travelable Syndicated Actor runtime", long_about = None)]
struct Cli {
    /// Root directory for runtime storage
    #[arg(short, long, default_value = ".duet")]
    root: PathBuf,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Initialize a new runtime
    Init {
        /// Number of turns between snapshots
        #[arg(long, default_value = "50")]
        snapshot_interval: u64,

        /// Flow control credit limit
        #[arg(long, default_value = "1000")]
        flow_control_limit: u64,
    },

    /// Show runtime status
    Status,

    /// Show turn history
    History {
        /// Number of recent turns to show
        #[arg(short, long, default_value = "10")]
        limit: usize,
    },

    /// Execute N turns
    Step {
        /// Number of turns to execute
        #[arg(short, long, default_value = "1")]
        count: usize,
    },

    /// Step backward N turns
    Back {
        /// Number of turns to rewind
        #[arg(short, long, default_value = "1")]
        count: usize,
    },

    /// Send a message to an actor
    Send {
        /// Actor ID (UUID)
        #[arg(long)]
        actor: String,

        /// Facet ID (UUID)
        #[arg(long)]
        facet: String,

        /// Message payload (preserves text format)
        #[arg(long)]
        payload: String,
    },

    /// Fork a new branch
    Fork {
        /// New branch name
        name: String,

        /// Fork from specific turn (default: current head)
        #[arg(long)]
        from: Option<String>,
    },

    /// Switch to a different branch
    Checkout {
        /// Branch name
        name: String,
    },

    /// List all branches
    Branches,
}

fn main() -> Result<()> {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(tracing::Level::INFO.into()),
        )
        .init();

    let cli = Cli::parse();

    match cli.command {
        Commands::Init {
            snapshot_interval,
            flow_control_limit,
        } => {
            let root = cli.root.clone();
            let config = RuntimeConfig {
                root: cli.root,
                snapshot_interval,
                flow_control_limit,
                debug: false,
            };

            Runtime::init(config)?;
            println!("Initialized Duet runtime at {:?}", root);
        }

        Commands::Status => {
            let runtime = Runtime::load(cli.root)?;
            println!("Active branch: {}", runtime.current_branch());
            println!("Storage root: {:?}", runtime.storage().root());
        }

        Commands::History { limit } => {
            let _runtime = Runtime::load(cli.root)?;
            // TODO: Implement history listing
            println!("Showing last {} turns (not yet implemented)", limit);
        }

        Commands::Step { count } => {
            let mut runtime = Runtime::load(cli.root)?;
            let records = runtime.step_n(count)?;
            println!("Executed {} turns", records.len());

            for record in records {
                println!(
                    "  Turn {}: actor {}, {} inputs, {} outputs",
                    record.turn_id,
                    record.actor,
                    record.inputs.len(),
                    record.outputs.len()
                );
            }
        }

        Commands::Back { count } => {
            let mut runtime = Runtime::load(cli.root)?;
            runtime.back(count)?;
            println!("Rewound {} turns", count);
        }

        Commands::Send {
            actor,
            facet,
            payload,
        } => {
            use duet::runtime::turn::{ActorId, FacetId};

            let mut runtime = Runtime::load(cli.root)?;

            let actor_id = ActorId::new_from_string(actor);
            let facet_id = FacetId::new_from_string(facet);

            // Parse payload as a preserves string
            let payload_value = preserves::IOValue::new(payload);

            runtime.send_message(actor_id, facet_id, payload_value);
            println!("Message enqueued");
        }

        Commands::Fork { name, from } => {
            let mut runtime = Runtime::load(cli.root)?;

            let from_turn = from.map(|s| duet::runtime::turn::TurnId::new(s));
            let new_branch = runtime.fork(name.clone(), from_turn)?;

            println!("Created branch: {}", new_branch);
        }

        Commands::Checkout { name } => {
            use duet::runtime::turn::BranchId;

            let mut runtime = Runtime::load(cli.root)?;
            let branch = BranchId::new(name.clone());

            runtime.switch_branch(branch)?;
            println!("Switched to branch: {}", name);
        }

        Commands::Branches => {
            let mut runtime = Runtime::load(cli.root)?;
            let current = runtime.current_branch().clone();
            let branches: Vec<_> = runtime.branch_manager_mut()
                .list_branches()
                .into_iter()
                .cloned()
                .collect();

            println!("Branches:");
            for branch in branches {
                let active = if branch.id == current {
                    "* "
                } else {
                    "  "
                };
                println!("{}  {}", active, branch.id);
            }
        }
    }

    Ok(())
}
