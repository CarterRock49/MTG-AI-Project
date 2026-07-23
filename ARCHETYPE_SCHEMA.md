# Deck strategy taxonomy

`Playersim/archetypes.py` owns the versioned deck-strategy contract used by
deck manifests, analytics classification, and own-deck planner initialization.
It does not replace the public opponent inference vector in Observation v5.

## Version 1 profile

A reviewed manifest profile has one primary macro plan, an optional distinct
secondary plan, sorted game-plan tags, and ten integer axes in the inclusive
range 0–100. The closed primary order is:

1. `aggro`
2. `tempo`
3. `midrange`
4. `control`
5. `combo`
6. `ramp`
7. `hybrid`
8. `unknown`

`hybrid` and `unknown` are inference results; reviewed manifests must select a
concrete primary. Tags such as `reanimator`, `landfall`, `prowess`, `tokens`,
and `spellslinger` describe how a deck executes its plan rather than competing
with the macro labels.

The fixed axis order is `speed`, `threat_density`, `interaction`,
`card_advantage`, `mana_acceleration`, `synergy_dependency`,
`combo_dependency`, `graveyard_dependency`, `board_width`, and
`instant_speed`. Vocabulary order is part of the taxonomy hash and must not be
changed without a new taxonomy version.

Every reviewed profile includes a review status, date, and basis. The active
Standard corpus carries reviewed profiles for all eight decks. Hydration
normalizes those profiles and records a deterministic profile hash; the runtime
deck loader verifies that hash before exposing the metadata. Governed
schema-v2 corpus decks fail closed when the reviewed profile is absent.
Explicit `kind=imported_deck` user imports remain eligible for deterministic
rule inference until a reviewed declaration is supplied.

## Classification API

- `classify_full_deck(card_counts, card_db, declared=None)` is a pure,
  deterministic full-deck classifier. It returns a `DeckStrategyProfile` with
  confidence in basis points, rule IDs, numeric evidence, a feature hash, and a
  complete profile hash.
- `validate_declared_profile()` and `normalize_declared_profile()` fail closed
  on unknown labels/tags, incomplete axes, invalid ranges, or an unsupported
  taxonomy version.
- `compatibility_primary()` maps historic specialized strings onto the closed
  macro vocabulary.
- `encode_profile()` defines a stable vector API for a future observation
  version. It is intentionally unused by Observation v5.

Classification traverses canonical card counts in a stable order, weights
copies explicitly, recognizes every card type independently (including
multi-type cards), and uses integer scoring/quantized axes. A low-evidence deck
becomes `unknown`; a close top-two score becomes `hybrid`.

## Information boundary and compatibility

An observer may use its own exact full-deck profile. It must never receive the
opponent seat's reviewed profile or exact deck metadata. Opponent inference
continues to use only public battlefield, graveyard, and visible exile evidence
under the existing six-value Observation-v5 contract.

`DeckStatsTracker.identify_archetype()` retains its lowercase string return
type, now sourced from the centralized profile. This avoids silently changing
the existing statistics file schema. Historical buckets are not relabeled or
merged. Any future profile vectors, wider opponent beliefs, or FiLM conditioning
must ship as Observation v6 with new schema, model, and lineage identities.

Run lineage records the taxonomy hash, classifier hash, every reviewed
per-deck profile hash, and their aggregate. A resume is rejected unless the
source manifest contains that contract and the freshly loaded corpus matches
it exactly. Named-canary validation exposes the same taxonomy, classifier,
profile-count, and reviewed-profile aggregate identities for the next canary
to pin.
