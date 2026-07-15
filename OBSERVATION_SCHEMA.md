# Playersim policy observation schema

Current contract: **Observation v3**, frozen July 15, 2026.

Current schema hash:
`6e29a94e3443881681afd794185f061133f24ff72350a7df27f48524f00d4137`.

The executable version and hash live in
`Playersim/observation_schema.py`. Training, fixture Harvest, and production
Harvest lineage record that identity independently from the card registry and
card feature schema.

## Global conventions

- Every player-dependent field is relative to the observer: `my`/controller
  code `0` means the policy's player; `opp`/controller code `1` means the other
  player. No learned field depends on absolute P1/P2 seat assignment.
- Fixed windows are padded. Standard defaults are hand `H=10`, battlefield
  `B=20` per player, public zone `Z=10`, stack `S=5`, action count `A=480`, and
  card feature width `F=436` under the current Standard feature schema.
- Card feature vectors use component-specific bounds: mana value and mana pips
  saturate at `1,000,000`, P/T at `±1,000,000`, and categorical columns at
  `0..1`. Exact zone counts remain separate from truncated detail windows.
- Unbounded game quantities deliberately saturate at their declared bounds.
  Structural categories, masks, phases, and indices are hard-contract values;
  exceeding their bounds records an observation degradation.
- Graveyard and exile detail windows are newest/top first. Stack slot zero is
  the top object.

## Extractor routes

| Route | Fields | Treatment |
| --- | --- | --- |
| Phase embedding | `phase` | Learned categorical embedding. |
| Semantic identity embedding | Every `*_card_identity` field | One shared 65,536-category, 32-wide embedding; never symlogged or treated as ordinal. |
| Continuous/categorical MLP | All other learned fields | Symlog first, then the rank-1/rank-2/rank-3 registered extractor. |
| External mask | `action_mask` | Consumed by MaskablePPO, not by the feature extractor. |
| Protocol metadata | `target_card_ids` | Runtime occurrence handles used to pin a target page through execution; never enters the policy network. |

Training smoke requires the union of these routes to cover every declared key.

## Stable semantic identity

All identity fields use the frozen canonical registry namespace:

- `0`: padded/no object.
- `1`: a visible object whose printed identity is unknown, hidden from this
  observer, generated, or absent from the frozen registry.
- `N + 2`: canonical registry index `N`.

The fixed encoded range is `0..65535`, so adding a card or deck within that
registry capacity does not resize the policy. The same embedding table is
shared across zones so one card has one learned identity representation.

Runtime occurrence IDs are never semantic input. A player can receive the
identity of a face-down permanent they control; its opponent receives category
`1`. Face-down exile remains hidden unless a future rules-specific visibility
permission is represented explicitly. Exile visibility masks distinguish a
known face-up object from an opaque face-down object.

Identity fields are:

`my_hand_card_identity`, `my_battlefield_card_identity`,
`opp_battlefield_card_identity`, `my_graveyard_card_identity`,
`opp_graveyard_card_identity`, `my_exile_card_identity`,
`opp_exile_card_identity`, `stack_card_identity`, `target_card_identity`, and
`choice_card_identity`.

## Field inventory

### Turn, life, and hand

| Key | Shape / range | Meaning |
| --- | --- | --- |
| `phase` | `(1)`, engine phase enum | Current rules/choice phase. |
| `turn` | `(1)`, `0..max_turns+1` | Current turn counter, including terminal adjudication step. |
| `is_my_turn` | `(1)`, boolean | Whether the observer is active player. |
| `my_life`, `opp_life` | `(1)`, `-10000..10000` | Live life totals. |
| `life_difference` | `(1)`, `-20000..20000` | `my_life - opp_life`. |
| `my_hand` | `(H,F)`, card bounds | Observer-visible hand cards in actionable order. |
| `my_hand_card_identity` | `(H)`, identity namespace | Canonical hand identities in the same slots. |
| `my_hand_count`, `opp_hand_count` | `(1)`, `0..1000` | Exact hand sizes; opponent identities remain hidden. |
| `hand_playable` | `(H)`, boolean | Current timing/affordability result per hand slot. |
| `hand_card_types` | `(H,5)`, boolean | Creature, instant, sorcery, land, other. |
| `hand_synergy_scores` | `(H)`, `0..1` | Planner synergy estimate. |
| `opportunity_assessment` | `(H)`, `0..10` | Planner opportunity score. |

### Battlefield and permanent state

| Key | Shape / range | Meaning |
| --- | --- | --- |
| `my_battlefield`, `opp_battlefield` | `(B,F)`, card bounds | Public battlefield card features, observer-relative. |
| `my_battlefield_card_identity`, `opp_battlefield_card_identity` | `(B)`, identity namespace | Canonical identities for those slots. |
| `my_battlefield_flags`, `opp_battlefield_flags` | `(B,5)`, boolean | Tapped, summoning-sick, attacking, blocking, has-any-keyword. |
| `my_battlefield_count`, `opp_battlefield_count` | `(1)`, `0..1000` | Exact battlefield counts beyond the detail window. |
| `my_permanent_counters`, `opp_permanent_counters` | `(B,6)`, `0..1000000` | `+1/+1`, `-1/-1`, loyalty, defense, lore, and all other counters. |
| `my_damage_marked`, `opp_damage_marked` | `(B)`, `0..1000000` | Damage currently marked on each permanent. |
| `my_attachment_targets`, `opp_attachment_targets` | `(B)`, `-1..2B-1` | Combined relative battlefield index of the object this Aura/Equipment is attached to; `-1` means none/off-window. |
| `my_attachment_counts`, `opp_attachment_counts` | `(B)`, `0..1000` | Number of public attachments on each permanent. |
| `my_creature_count`, `opp_creature_count` | `(1)`, `0..1000` | Exact creature counts. |
| `my_total_power`, `my_total_toughness`, `opp_total_power`, `opp_total_toughness` | `(1)`, `±1000000` | Aggregate live creature stats. |
| `creature_advantage` | `(1)`, `±1000` | Relative creature-count difference. |
| `power_advantage`, `toughness_advantage` | `(1)`, `±1000000` | Relative aggregate-stat differences. |
| `threat_assessment` | `(B)`, `0..10` | Planner score for opposing battlefield slots. |
| `card_synergy_scores` | `(B,B)`, `-1..1` | Pairwise synergy across observer permanents. |

### Mana, libraries, and player state

Mana vectors use color order `W,U,B,R,G,C` and saturate at 100 per entry.

| Key | Shape / range | Meaning |
| --- | --- | --- |
| `my_mana_pool`, `opp_mana_pool` | `(6)`, `0..100` | Ordinary floating mana. |
| `my_snow_mana_pool`, `opp_snow_mana_pool` | `(6)`, `0..100` | Floating mana retaining snow provenance across ordinary, phase-restricted, and conditional pools. |
| `my_restricted_mana_pool`, `opp_restricted_mana_pool` | `(6)`, `0..100` | Aggregate conditional and phase-restricted floating mana by color. |
| `untapped_land_count` | `(1)`, `0..1000` | Observer's untapped lands. |
| `total_available_mana` | `(1)`, `0..100` | Observer ordinary/restricted floating mana plus simplified untapped-land availability. Snow provenance is a subset and is not counted twice. |
| `turn_vs_mana` | `(1)`, `0..1` | Land development relative to turns received by the observer, not the global alternating turn number. |
| `my_library_count`, `opp_library_count` | `(1)`, `0..1000` | Exact public library sizes; no library identity/order. |
| `my_player_counters`, `opp_player_counters` | `(3)`, `0..1000` | Poison, energy, experience. |
| `my_player_status`, `opp_player_status` | `(2)`, boolean | City's blessing, monarch. |

### Graveyard and exile

| Key | Shape / range | Meaning |
| --- | --- | --- |
| `my_graveyard_count`, `opp_graveyard_count` | `(1)`, `0..100` | Exact graveyard sizes within the declared saturation bound. |
| `my_exile_count`, `opp_exile_count` | `(1)`, `0..1000` | Exact exile sizes, including face-down objects. |
| `my_dead_creatures`, `opp_dead_creatures` | `(1)`, `0..100` | Current creature-card counts in graveyards. |
| `my_graveyard_cards`, `opp_graveyard_cards` | `(Z,F)`, card bounds | Public top/newest graveyard cards. |
| `my_graveyard_card_identity`, `opp_graveyard_card_identity` | `(Z)`, identity namespace | Their canonical identities. |
| `my_exile_cards`, `opp_exile_cards` | `(Z,F)`, card bounds | Public newest exile objects; hidden identities have zero vectors. |
| `my_exile_card_identity`, `opp_exile_card_identity` | `(Z)`, identity namespace | Canonical or unknown identities for those objects. |
| `my_exile_card_visibility`, `opp_exile_card_visibility` | `(Z)`, boolean | Whether printed identity is visible to the observer. |

### Stack and combat

| Key | Shape / range | Meaning |
| --- | --- | --- |
| `stack_count` | `(1)`, `0..1000` | Exact stack depth. |
| `stack_controller` | `(S)`, `-1..1` | Empty, me, opponent; top first. |
| `stack_card_types` | `(S,5)`, boolean | Creature, instant, sorcery, ability, other. |
| `stack_cards` | `(S,F)`, card bounds | Full public source/spell features. |
| `stack_card_identity` | `(S)`, identity namespace | Stable source/spell identities. |
| `stack_object_kinds` | `(S)`, `0..4` | Empty, spell, activated ability, trigger, other. |
| `stack_target_counts` | `(S)`, `0..1000` | Number of committed target leaves in the stack context. |
| `stack_mode_counts` | `(S)`, `0..100` | Number of selected ordinary/Spree modes. |
| `attackers_count`, `blockers_count` | `(1)`, `0..1000` | Exact declared combatant totals. |
| `combat_attack_targets` | `(2B)`, `-2..2B` | Row is combined relative battlefield object: `-2` nonattacker, `-1` off-window defender, `0` defending player, `N+1` permanent index `N`. |
| `combat_blocker_assignments` | `(2B)`, `-1..2B-1` | Row is blocker; value is combined relative attacker index. |
| `potential_combat_damage` | `(1)`, `0..1000000` | Total power of observer's currently legal attackers. |

### Ability, history, and planner summaries

| Key | Shape / range | Meaning |
| --- | --- | --- |
| `ability_features` | `(B,5)`, `0..10` | Up to five public activated-ability summaries per observer permanent. |
| `ability_timing` | `(5)`, boolean | Current broad activation-timing context. |
| `planeswalker_activations` | `(B)`, boolean | Planeswalker activation availability. |
| `planeswalker_activation_counts` | `(B)`, `0..10` | Activations used/available as represented by the engine. |
| `previous_actions` | `(80)`, `-1..A` | Recent action history, padded with `-1`. |
| `previous_rewards` | `(80)`, `-1000..1000` | Recent reward history. |
| `phase_history` | `(5)`, `-1..phase_max` | Last observed phase transitions. |
| `optimal_attackers` | `(B)`, boolean | Deterministic bounded-combination recommendation during the live declare-attackers decision. |
| `attacker_values` | `(B)`, `-10..10` | Per-attacker evaluator score during the live declare-attackers decision. |
| `ability_recommendations` | `(B,5,2)`, `0..1` | Recommend/confidence pair per ability; rank-3 extractor route. |
| `strategic_metrics` | `(7)`, `-1..1` | Position, board, card, mana, life, tempo, and game-stage metrics. Card and mana advantages use magnitude-preserving `tanh(delta / 3)` normalization. |
| `position_advantage` | `(1)`, `-1..1` | Planner position score. |
| `deck_composition_estimate`, `opponent_archetype` | `(6)`, `0..1` | Public-information deck/archetype summaries with specialized tempo, ramp, and tribal profiles preserved. |
| `future_state_projections` | `(7)`, `-1..1` | Observer-antisymmetric planner projection; symmetric public states are exactly neutral. |
| `multi_turn_plan`, `win_condition_viability` | `(6)`, `0..1` | Deterministic expected-value plan and win-condition summary. Plans use live untapped lands and spendable floating mana, respect every remaining land-drop allowance, and do not invent a current-turn draw. Nonviable paths have zero viability; viable damage paths increase monotonically as their projected win approaches. |
| `win_condition_timings` | `(6)`, `0..max_turns+1` | Estimated turns to each win condition. |

Planner analysis is recomputed per observation. Constructing an observation is
RNG-neutral: unknown future draws use expected values, and wide-board attack
candidates use a deterministic bounded ordering. Opponent inference may inspect
only identities visible to the observer; face-down permanents and face-down
exile objects are excluded. A future performance cache must be keyed by both
state version and observing player; turn-only caching is forbidden because it
is stale within a turn and unsafe across seats.

### Mulligan, target, and choice protocol

| Key | Shape / range | Meaning |
| --- | --- | --- |
| `mulligan_in_progress` | `(1)`, boolean | Observer is in the mulligan transaction. |
| `mulligan_recommendation` | `(1)`, `0..1` | Planner keep/mulligan score. |
| `mulligan_reason_count` | `(1)`, `0..5` | Number of active reason flags. |
| `mulligan_reasons` | `(5)`, boolean | Mulligan reason flags. |
| `targetable_permanents` | `(2B)`, `-1..int32_max` | Valid permanent indices in `my battlefield + opp battlefield`. |
| `targetable_players` | `(2)`, `-1..1` | Valid player indices, `0=me`, `1=opponent`. |
| `targetable_spells_on_stack` | `(S)`, `-1..int32_max` | Real valid stack indices. |
| `targetable_cards_in_graveyards` | `(20)`, `-1..int32_max` | Valid indices in `my graveyard + opp graveyard`. |
| `target_cards` | `(10,F)`, card bounds | Exact current SELECT_TARGET page. |
| `target_card_identity` | `(10)`, identity namespace | Stable candidate identities. |
| `target_card_mask` | `(10)`, boolean | Slot contains a card object. |
| `target_card_ids` | `(10)`, runtime ID or `-1` | External transaction pin; excluded from extractor. |
| `target_kinds` | `(10)`, `0..6` | Padding, player, permanent, stack, graveyard, exile, other. |
| `target_controllers` | `(10)`, `-1..1` | Unknown/noncard, me, opponent. |
| `target_zone_indices` | `(10)`, `-1..1000000` | Exact index within the candidate owner's public zone/stack. |
| `sacrificeable_permanents` | `(B)`, `-1..B` | Observer battlefield indices for the active sacrifice transaction. |
| `selectable_modes` | `(10)`, `-1..10` | Active mode indices. |
| `selectable_colors` | `(5)`, `-1..4` | Active WUBRG choices. |
| `choice_cards` | `(10,F)`, card bounds | Exact current generic-choice page. |
| `choice_card_identity` | `(10)`, identity namespace | Stable identities for card options. |
| `choice_card_mask` | `(10)`, boolean | Slot contains a real card option. |
| `choice_kind` | `(1)`, `0..16` | Generic choice transaction category. |
| `choice_remaining` | `(1)`, `0..int32_max` | Required selections/allocations remaining. |
| `choice_allocation_counts` | `(10)`, `0..int32_max` | Current allocation per visible option. |
| `valid_x_range` | `(2)`, `-1..int32_max` | Inclusive min/max X; `-1` when inactive. |
| `bottomable_cards` | `(H)`, boolean | Hand slots available to London-bottom. |
| `dredgeable_cards_in_gy` | `(6)`, `-1..100` | Graveyard indices exposed by the current Dredge choice. |
| `action_mask` | `(A)`, boolean | Current legal action set, external to feature extraction. |

## Removed Observation v1 fields

The v2 migration intentionally removes only exact/dead redundancy:

- `phase_onehot` (duplicate of `phase` embedding).
- `p1_life`, `p2_life`, `p1_battlefield`, `p2_battlefield`, `p1_bf_count`,
  `p2_bf_count` (absolute-seat duplicates replaced by relative fields).
- `hand_performance` (constant initialization proxy).
- `my_battlefield_keywords` (keywords already occupy frozen card-vector
  columns).
- `my_tapped_permanents` (duplicate of battlefield flag column zero).
- `my_mana` (sum of the ordinary mana vector).
- `remaining_mana_sources` (duplicate of `untapped_land_count`).
- `graveyard_key_cards`, `exile_key_cards` (replaced by symmetric my/opp
  public-zone tensors and identities).
- `memory_suggested_action`, `suggestion_matches_recommendation` (the former
  online, per-environment memory injected a random legal action when it had no
  evidence and made otherwise equal observations depend on rollout history).
- `recommended_action`, `recommended_action_confidence` (constant in the
  default training configuration and stochastic/circular when enabled).
- `estimated_opponent_hand` (presented fake exact card vectors selected from a
  live runtime database containing hidden deck instances; opponent hand count
  and public archetype evidence remain represented without inventing cards).

Strategy memory remains available as an explicitly enabled, deterministic
advisory/diagnostic subsystem. It is disabled by default, uses isolated
per-environment versioned storage, and never enters the policy observation.

Card-memory and deck-statistics outcomes are still recorded by every worker,
but their adaptive evaluator inputs are disabled by default. This keeps equal
public states stationary across workers and keeps training aligned with the
history-free evaluation environment. Adaptive history remains an explicit
opt-in diagnostic mode and is not part of the default policy contract. Its
play and optimal-turn statistics use turns received by the relevant player,
not the engine's alternating global turn number.

The remaining derived planner and advantage fields are deterministic,
observer-information-only summaries. They remain until policy ablation gives
evidence that removing them is safe.

During target selection, every targetable observation vector is derived from
the active instruction's required type and effect text, including modal and
multi-instruction spells. It therefore describes the same candidates bound to
the current target-selection actions rather than all targets mentioned on the
printed card.

## Compatibility rule

Observation v3 is checkpoint-incompatible with every earlier model. Consumers
must reject or isolate runs when `lineage.observation_schema.sha256` differs.
The canonical registry hash determines the identity embedding namespace; the
card feature-schema hash determines `F`; both must match as well.
