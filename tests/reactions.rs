use duet::runtime::control::Control;
use duet::runtime::error::ActorResult;
use duet::runtime::reaction::{ReactionDefinition, ReactionEffect, ReactionValue};
use duet::runtime::turn::{ActorId, FacetId};
use duet::runtime::{RuntimeConfig, pattern::Pattern};
use preserves::IOValue;
use std::sync::Once;
use tempfile::TempDir;
use uuid::Uuid;

use duet::runtime::actor::{Activation, Entity};
use duet::runtime::registry::EntityCatalog;

struct MirrorEntity;

impl Entity for MirrorEntity {
    fn on_message(&self, activation: &mut Activation, payload: &IOValue) -> ActorResult<()> {
        activation.assert(duet::runtime::turn::Handle::new(), payload.clone());
        Ok(())
    }
}

static REGISTER_MIRROR: Once = Once::new();

fn ensure_mirror_registered() {
    REGISTER_MIRROR.call_once(|| {
        EntityCatalog::global().register("mirror-entity", |_config| Ok(Box::new(MirrorEntity)));
    });
}

#[test]
fn reactions_persist_and_rehydrate() {
    ensure_mirror_registered();

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    let actor = ActorId::new();

    let mut control = Control::init(config.clone()).unwrap();

    // Register an entity to ensure the facet exists.
    let entity_config = IOValue::symbol("mirror-config");
    let facet_seed = FacetId::new();
    control
        .register_entity(
            actor.clone(),
            facet_seed,
            "mirror-entity".to_string(),
            entity_config,
        )
        .unwrap();

    let facet = control.list_entities().first().unwrap().facet.clone();

    let pattern = Pattern {
        id: Uuid::new_v4(),
        pattern: IOValue::record(IOValue::symbol("mirror"), vec![IOValue::symbol("<_>")]),
        facet: facet.clone(),
    };

    let effect = ReactionEffect::Assert {
        value: ReactionValue::MatchIndex { index: 0 },
        target_facet: None,
    };

    let definition = ReactionDefinition::new(pattern, effect);
    let reaction_id = control
        .register_reaction(actor.clone(), definition)
        .unwrap();

    let reactions = control.list_reactions();
    assert_eq!(reactions.len(), 1);
    assert_eq!(reactions[0].reaction_id, reaction_id);

    drop(control);

    // Rehydrate the runtime from disk and ensure the reaction is still present.
    let mut control = Control::init(config.clone()).unwrap();
    let facet = control.list_entities().first().unwrap().facet.clone();
    let reactions = control.list_reactions();
    assert_eq!(reactions.len(), 1);
    assert_eq!(reactions[0].reaction_id, reaction_id);

    // Send a message to trigger the entity and reaction.
    let payload = IOValue::record(
        IOValue::symbol("mirror"),
        vec![IOValue::new("hello".to_string())],
    );
    control
        .send_message(actor.clone(), facet.clone(), payload.clone())
        .unwrap();

    let assertions = control.runtime().assertions_for_actor(&actor).unwrap();
    let has_reaction = assertions.iter().any(|(_, value)| {
        value
            .as_string()
            .map(|s| s.as_ref() == "hello")
            .unwrap_or(false)
    });
    assert!(has_reaction, "reaction assertion not found");
}
