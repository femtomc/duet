# Interpreter-Driven Entity Spawning

## Goals

* Let entities (including the interpreter) create new actors/entities during a turn,
  mirroring the Syndicate `spawn`/`spawn-link` behavior.
* Keep time-travel guarantees: the journal and snapshots must replay the same spawn
  operations deterministically.
* Preserve capability discipline — spawning should be gated by an explicit capability.
* Record enough metadata that hydration and branch replay rebuild the actor tree.

## Proposed Pipeline

1. **Activation API**
   * Extend `Activation` with a `spawn_entity` (and future `spawn_link`) helper.
   * Each call creates a `PendingSpawn` record stored on the activation and emits a
     `TurnOutput::EntitySpawned`.
   * IDs (actor, root facet, entity ID) must be deterministic. Use a fixed UUID
     namespace and derive IDs via `Uuid::new_v5(namespace, data)` where `data`
     includes the parent actor/facet and a sequence counter.
   * Optional link metadata can be captured for future lifecycle coupling, but the
     initial implementation can ignore it (`link = false`).

2. **Turn Output**
   * Add `TurnOutput::EntitySpawned { parent_actor, parent_facet, child_actor,
     child_root_facet, entity_id, entity_type, config, link }`.
   * (Future) `TurnOutput::LinkEstablished` for tracking link edges if/when we add
     link semantics.

3. **Runtime Commit Path**
   * `Runtime::dispatch_turn_outputs` handles the new variant:
     * Create a new `Actor` using `Actor::with_root(child_actor, child_root_facet)`.
     * Instantiate the entity via the registry, attach it to the child actor, and
       register metadata in `EntityManager`.
     * Persist `entities.json` so hydration sees the new actor.
     * If `link == true`, record a link edge (storage TBD).
   * Ensure the operations run both during live execution and when replaying
     historical turns (reading from the journal).

4. **State Delta / Snapshots**
   * No new CRDT component is required — actor creation is driven by the new turn
     output. `StateDelta` remains unchanged.
   * Snapshots already gather entity state per actor/facet. Once the spawn output has
     been applied, the new actor/entity will appear in the snapshot automatically.
   * Hydration order should be: actors → facets → entities → assertions →
     capabilities. A new helper that accepts externally provided actors will make
     this explicit (`Actor::with_root`).

5. **Hydration**
   * When loading a snapshot or replaying from the journal, apply `EntitySpawned`
     outputs *before* restoring entity private state.
   * Existing `hydrate_entities` logic can be reused, but it must create the actor
     with the predetermined root facet (using the same helper as dispatch).

6. **Capability Gate**
   * Introduce a capability kind (e.g. `entity/spawn`). Only holders can call
     `Activation::spawn_entity`; the interpreter acquires this capability via the
     service when wiring the agent manager.
   * The activation helper checks `self.current_entity` and errors if the issuer
     lacks the capability (mirrors how other privileged actions will work later).

7. **Teardown Contract**
   * Document that entities must reset external resources they touch in `stop` /
     `exit_hook`. Time-travel semantics are “pure modulo entity side effects.”
   * Ensure the spawn path still calls `Entity::stop`/`exit_hook` when the runtime
     terminates actors during rewinds/branch switches.

8. **Links (Future Work)**
   * Track link edges in a runtime-managed map.
   * Emit a `LinkEstablished` output so replay constructs the same links.
   * When a parent facet stops, enqueue a termination turn for linked child actors.
     When a child actor exits, issue a facet stop.

## Testing Plan

* Unit test deterministic ID derivation (`spawn_entity` produces identical IDs across
  identical turns).
* Integration tests:
  * Interpreter program spawns an entity, posts assertions, rewind → entity count
    matches.
  * Snapshot + hydrate after spawn restores the actor/entity with the same IDs.
  * Capability gate: spawning without the capability fails; granting it succeeds.
* Mock entity that records `stop` / `exit_hook` invocations to validate teardown during
  rewinds.

## Open Questions / Tasks

* Define the UUID namespace constant (include in code with a comment).
* Decide where to persist link metadata once we implement linking.
* Audit existing CLI/service commands to surface the new spawn capability (e.g.,
  listing entities should include spawn-created ones automatically).
