# Playersim roadmap — current as of July 23, 2026

## Mission and scope

Train a strong two-player Magic policy whose games produce trustworthy
per-card, per-deck, and matchup statistics for a downstream deck-construction
AI. Work is prioritized by one question: **will this improve the reliability
of the builder's decisions?**

Target formats are Standard first, Modern second, and Pioneer third. Each
format gets an isolated card pool, deck corpus, support ledger, policy lineage,
checkpoint league, and statistics namespace.

Permanently out of scope: multiplayer, Commander, Planechase/planar dice, and
per-printing set/collector distinctions. The runtime is best-of-one; best-of-
three is a possible later addition only if a target format requires it.

Status legend: ✅ complete, ◐ active/partially complete, ▢ not started.

---

## Executive status

The engine, policy boundary, replay/fuzz infrastructure, format lineage, and
Harvest orchestration are operational. The representative Standard corpus does
not currently carry a severe support-ledger entry, but Round 7.98 proved that
static `observed_clean` evidence does not cover cross-card copy interactions;
strict recovery counters must remain zero in every fresh run. The project is
**not production-ready** because
no trained checkpoint has passed the paired-seat strength gate, matchup
calibration has not run, format-wide card support remains incomplete, and the
deck-builder feedback loop is not connected.

### Current verified delivery baseline

The current Observation-v6/FiLM working tree is green at the following gates:

| Gate | Result |
| --- | --- |
| Golden scenarios | 409/409 |
| Runtime smoke | 9/9 |
| Training smoke | 14/14 |
| Discovered unit tests | 873/873 |
| Default invariant fuzz | 8/8 seeds × 1,000 valid actions, plus phase-boundary check |
| Observation schema | v6 / `6521db9c0c70c919a63c34e9c99463a3b801e25ae91149fd518a34054989e790` |

The default invariant run covers 8,000 valid actions total plus the controlled
phase-boundary check. The full v6 delivery gate is complete. No v6 canary,
canary name, or launch command has yet been created.

Standing broader gates last recorded green: fixture Harvest 24/24, production
Harvest protocol 17/17, card registry 19/19, deck ingestion 13/13, and
fuzz/replay configuration 6/6. The strict long-fuzz result (32 seeds × 10,000
valid actions) is historical until that scheduled/manual gate is rerun.

The final pre-run audit aligned target discovery, action masks, selection, and
resolution around the exact active instruction; unknown target grammar fails
closed. Card and deck analytics now preserve canonical card identity,
player-relative turns, draw-aware rates, and atomic per-file persistence.
Evaluator caches retain only static characteristics, while live state and
perspective are recomputed for each decision.

### Round 7.98 result and Round 7.99 repaired boundary — July 22, 2026

`round-7.98-self-play-v1` launched the structural self-play lever and failed
strictly at 705,456 of 2,000,000 timesteps (35.27%). The failure was not a
checkpoint-pool or observation-boundary fault: Superior Spider-Man copied
Colorstorm Stallion, retained the name `Superior Spider-Man`, and later fired
the copied Opus trigger. Its otherwise supported effect was gated on the
printed source name, so resolution produced one `UnsupportedEffect` and the
strict fidelity callback stopped training with
`unparsed_cards=["Superior Spider-Man"]`. No checkpoint qualified for
publication; the best periodic qualification point score was 0.203 and the
best 95% lower bound was 0.0832, both far below the 0.550 gate.

The checkpoint-league implementation publishes a hashed learner snapshot every
100k timesteps into a four-checkpoint FIFO disk pool. Each worker eagerly
verifies SHA-256 and exact
observation/action-space compatibility, retains only one frozen CPU policy in
steady state, and samples that resident checkpoint versus scripted play with
probability 0.5 per episode. Lease replacement is staged synchronously and
commits only on reset; a bad checkpoint or worker rejection aborts training,
records partial attempts, and best-effort rolls every worker back to scripted
play. Fixed evaluation never receives the pool callback. Pool and run manifests
together record full lineage, actual checkpoint bytes and hashes, deterministic
lease seeds, assignment history, bounded-resource estimates, cadence crossings,
rollback details, and final run state.

Opponent inference now builds an explicit observer-private observation and
legal mask, restores environment, ActionHandler, diagnostics, planner analysis,
and archetype caches on success or failure, and installs the opponent mask and
contexts only for the atomic opponent action. Learned-seat hand/library changes
cannot affect the opponent view. Degraded fallback masks, incompatible models,
and mask-invalid predictions fail closed; direct checkpoint opponents used by
Harvest remain persistent across resets. Action/reward histories follow logical
roles when seats alternate, each physical seat gets a private planner profile
derived only from its own deck, and emergency fallback rebuilds the agent layer
and clears episode histories. Public threat scores cannot depend on identities
in the other seat's hidden hand.

The repair replaces the Colorstorm and Deceit printed-name gates with bounded,
complete executable-Oracle templates, so copied rules text keeps its semantics
when the object's name changes. Permanent runtime scenarios now cover
Superior-copy-Colorstorm through a five-mana Opus buff/token-copy resolution
and Superior-copy-Deceit through trigger ordering, opponent targeting, nonland
hand selection, discard, and bound exile; both assert unchanged fidelity
counters. The source-hashed three-card replay at
`probe_runs/standard-superior-copy-repair-2026-07-22-v1/card_probe_report.json`
reports 0 failures and 3 explicit coverage gaps, correctly leaving every card
semantically unverified. Its report SHA-256 is
`7ccd221cfa1fcdba6c37c323612da72d508d0fed73d78766bb2fcc6f968a2ed6`
canonical /
`0d9d3127ec61239e12d7853f65c7748f30d32d13ec68a311f12e30590f7ff561`
physical.

Round 7.99 is the fresh repaired canary. It inherits the complete 7.98
self-play, Observation v5, reward, curriculum, evaluation, seed, and
matchup-weighting-off contract, changing only permanent recovery checkpoints
from every 50k to every 500k timesteps. The independently bounded self-play
pool still publishes every 100k and retains four snapshots. The global
non-canary recovery default is also 500k; historical 7.98 remains immutable.
Named canaries reject resume, and curriculum scheduler state is not
checkpointed, so the failed run cannot be continued scientifically.

Observed post-activation checkpoint opponents accounted for 52.67% of sampled
episodes (41.1% over the measured run including the scripted-only warmup), which
matches the intended 0.5 split. The cost is material: post-100k throughput fell
to roughly 1,576 steps/min from the prior run's 3,027, and the 100k evaluation
took 29.51 minutes versus 15.06. Eight resident CPU policies, the evaluation
process, synchronous rollout stragglers, and roughly 12 GiB of process working
set explain the slowdown; checkpoint file writes took only seconds. Monitor the
same resource signals in 7.99 before changing another training lever.

Storage housekeeping recycled 2,004 disposable or superseded log files
(19.396 MiB) while retaining the failed 7.98 training log, unique warning,
completed baselines, cited probes, and fuzz replays. The failed run's model
directory is no longer retained; only `models/baselines/` remains under
`models/`.

### Final Room/Exhaust evidence refresh — July 21, 2026

The final-source 43-card replay lives at
`probe_runs/standard-room-exhaust-evidence-2026-07-21-v4/card_probe_report.json`
and is pinned to engine SHA-256
`15e3006bc51e200b94b042139ca532b690a8eef4560c2994470809e65824c931`.
It records 41 coverage gaps, 2 failures (Charred Foyer // Warped Space and Pit
Automaton), and 0 bounded mechanical passes. Exact resume reused all 43
artifacts. Report SHA-256 is
`e933b32699548d39e933d503888e3a5e78c8e4e1058d8841a0b13fdb58a2e062`
canonical /
`f32b845e84237f0cea58bbc2a3e49fdffa8cda661bd3e8ad3442fc0d79094bd2`
physical; run SHA-256 is
`df52c3a8fe6bd34917352d4e4308bce01e39d8df72f1eefe883bf824ebb29aa6`
canonical /
`354083d37220af88f524ad23fa64cfba83c08cac41968562bd1cf2d7bbce6164`
physical.

The residual three-card replay at
`probe_runs/standard-room-exhaust-expanded-2026-07-21-v4/card_probe_report.json`
fails Charred Foyer, Fell Gravship, and Pit Automaton closed. Charred's `{0}`
exile alternative cost creates five identical warnings and one manifest issue
recorded five times; Fell Gravship creates 24 warnings across four unique
Station/layer diagnostics; Pit is rejected by static evidence for source-coupled
copying. Exact resume reused all three artifacts. Report SHA-256 is
`d4a7218dc326e8b5b52e6749590f40a8a1ef041d518412ee2e237574c2a5953d`
canonical /
`8487ee985b15f07e1b92ec265bc455ee6551c441b380cdb116c6bb4dd81bf04d`
physical; run SHA-256 is
`e17b7e5e042f032062aa078f50b7e3d85c3bb70fcb4997882c2c4b2546fcab2b`
canonical /
`b959f0eec2a12c51831151012c5dd5ae37aee4424f80894d5a7c3a5ac316b2f2`
physical. Independent audit returned `AUDIT_OK=true` across all 46 v4 card
artifacts; every card remains semantically unverified. These probe directories
are local and Git-ignored.

### Batched levers — cast-path hardening, opt-in matchup weighting, Observation v5 — July 20, 2026

Three changes land together on top of the v4 work, in response to the round-7.96
diagnosis (below) that the reactive-deck collapse is a piloting problem and to
the recurring dormant mask/execution divergences that keep stopping runs.

- **Cast-path legality hardening.** Two successive runs died on dormant
  mask/execution divergences (a Tiered spell flashed back with no affordable
  mode; a counterspell cast from exile with an empty stack). Root cause: the
  alternative cast paths offered a spell on affordability alone, without the
  target and modal-mode legality the hand path enforces. A single shared
  `_cast_is_legal_for_mask` predicate (targets available plus a selectable
  Tiered/Spree mode) now gates every cast generator — hand, graveyard
  (flashback, Harmonize), exile, and the hand alternative-cost mechanics
  (Jump-start, Escape, Madness, Emerge, Delve, Warp). Overload is exempt
  because it replaces "target" with "each". Pinned by
  `tests/alt_cast_target_mask_regression_test.py` plus the flashback/exile
  regressions.
- **Opt-in matchup weighting.** `--matchup-weighting` (off by default) biases
  training deck selection to oversample the decks the agent is losing with
  (adaptive inverse-win-rate weighting fed by decisive-win outcomes), the lever
  the diagnosis points to for the reactive-deck collapse. Without the flag the
  scheduler keeps its even round-robin; fixed evaluation is unaffected. It is
  deliberately excluded from the named-canary contract. Pinned by
  `tests/curriculum_test.py::MatchupWeightingTest`.
- **Observation v5: producible mana by color.** The policy saw each card's cost
  broken out by color but only a color-blind `total_available_mana` scalar for
  what it could produce — a real gap for reactive decks deciding whether to hold
  up a colored answer. `my_producible_mana` / `opp_producible_mana` add per-color
  (WUBRG) access from visible untapped lands plus floating mana; own is exact,
  the opponent's is the public estimate from its face-up lands (no hidden-info
  leak). Schema hash
  `cc7d2e002af3338ee1192f3b85cc16d0913f1a4b4ee763b6b9ba7750d6c50a16`; the
  `round-7.97` canary re-pins the unchanged reward/curriculum over v5. Pinned by
  `tests/producible_mana_observation_test.py`.

The observation change was a hard lineage boundary; the cast-path and matchup
changes were not. That candidate launched with `--canary-config round-7.97` and
`--matchup-weighting` to enable the scheduling lever. Because two levers landed
in one round, its result requires careful attribution: matchup weighting was
expected to move the reactive decks, while Observation v5 was the secondary
information upgrade.

### Round 7.95 verdict, plateau diagnosis, and the Observation v4 response — July 20, 2026

The `round-7.95-combat-v7-v1` canary completed all 2M steps. Its finer 0.10
scripted ratchet did **not** break the plateau: across 20 evaluations the
qualification score oscillated between 0.125 and 0.328 (peak 0.328 at 300k,
final 0.266), never approaching the 0.55 gate, while the scripted handicap
ping-ponged exactly as in 7.94. This confirms the round hypothesis was wrong —
the plateau is not ratchet granularity — and that four consecutive
curriculum-ratchet canaries (7.92–7.95) have exhausted that lever.

A diagnosis over the run's own artifacts (1,280 eval games, 6,608 training
games, and the gzip traces) located the cause. It is **not** diffuse weakness,
sampling, or a globally broken reward. Win rate splits cleanly by the deck the
policy pilots: proactive decks play well (Izzet Prowess 52.5%, Selesnya 45.0%,
Izzet Spellementals 38.8%) while reactive/control decks collapse (Jeskai 0.6%,
Dimir 0.6%, 4c Control 0.0% across 160 eval games each). Sampling is ruled out:
every deck received ~650 balanced full_pool training games and 4c still won
only 10.6% in training. The mechanism is a self-inflicted mulligan
death-spiral: the policy mulligans to an average of 3–4 cards on exactly the
decks it cannot pilot (Dimir 4.12, 4c/Jeskai/Momo ~3.0) while keeping seven on
the decks it can (aggro 0.05–0.33), and the scripted opponent never mulligans,
so the agent enters control games down 3–4 cards against a full grip. The
eval timeout tail (173 turn-limit games, ~47% ahead / 49% behind) is
secondary and concentrated in the go-wide decks (Azorius Momo, Izzet
Spellementals) stalling near parity.

Root cause: the policy could not see its own deck. The library was count-only,
`deck_composition_estimate` summarized only already-revealed cards, and there
was no remaining-draw signal — so the mulligan/keep decision was uninformed at
the most basic level (the policy did not even know its own land count), and
reactive decks, which require deck knowledge to pilot, were unplayable.

**Observation v4** (schema hash
`15783924c36af23cf9dffb2700894f21d4c15343d0dc1fb353d351eae2f5d19f`) closes
this gap. It exposes the observer's own full starting decklist as canonical
identities (`my_deck_card_identity`, an order-free multiset — the cards you
own, never your hidden draw order), the remaining-library composition
(`my_library_composition`: type counts, mana-curve buckets, color counts, and
remaining total — the live "what's left to draw" signal), and changes
`deck_composition_estimate` to summarize the full starting deck. Every
decklist-derived feature is observer-own only; the opponent's decklist and
library are never exposed (`opponent_archetype` remains an estimate from
observed cards). An observation audit found the decklist family was the
primary gap; every other fact a player legitimately knows (zones, counts,
mana, counters, combat, stack) was already present. Per the working agreement,
the mulligan decision is left to the policy rather than capped — v4 gives it
the information to learn a good keep, instead of legislating one.

This is a hard lineage boundary. The `round-7.96` canary re-pins the unchanged
`tempo-graded-potential-v1` reward and `combat-v7` curriculum over Observation
v4; the v3-pinned canaries (7.92–7.95) fail closed against v4 runtime by
design, and v2/v3 checkpoints cannot resume into it. The next training run is a
fresh `round-7.96` candidate from the v4 boundary; the mulligan death-spiral
and reactive-deck win rates are its primary success signals.

The first `round-7.96-obs-v4-v1` run died at ~400k on a strict fidelity
failure — a pre-existing dormant mask/execution divergence the v4 policy
reached because it plays differently, not a v4 or card-fidelity regression.
The graveyard flashback mask offered Thunder Magic (a Tiered instant granted
flashback for {R}) because the base cost was affordable and the coarse
`_targets_available` check passed, but `cast_spell` rejected it: the only
affordable tier had no legal creature target and the higher tiers were
unaffordable. The graveyard-cast mask now shares `cast_spell`'s CR 601.2b
modal gate (`tiered_mode_is_selectable`/`spree_mode_is_selectable`) through a
`_graveyard_modal_cast_castable` predicate, so a modal graveyard cast is
offered only when a mode is actually castable
(`tests/flashback_tiered_mask_regression_test.py`, verified against the
retained 227-action failure replay).

The relaunched `round-7.96-obs-v4-v2` run reached 700k and died on a second,
distinct instance of the same class: the exile-cast mask offered No More Lies
(a counterspell exiled with an ordinary cast permission) because it was
affordable, but the stack was empty so `cast_spell` fizzled it for lack of a
target. Root pattern: the alternative cast paths (graveyard flashback,
Harmonize, exile) were built without the full hand-cast legality contract, so
they offer casts the engine then rejects. The exile-cast and both Harmonize
graveyard branches now apply the same `_targets_available` and modal-mode gate
the hand path uses (`tests/exile_cast_target_mask_regression_test.py`, verified
against the retained evaluation replay); a proactive audit closed the latent
Harmonize instance before it could stop a run.

The delivery gate is green at 812 discovered unit tests, 9/9 runtime smoke,
13/13 training smoke, and all eight default invariant-fuzz seeds plus the
phase-boundary check. Relaunch a fresh `round-7.96` candidate from the v4
boundary.

The v2 run's early evidence updated the diagnosis: the mulligan behavior was
unstable rather than fixed (it oscillated between keeping seven and
mulliganing to zero across evals), and the reactive decks (4c/Dimir/Jeskai)
stayed at ~0% regardless of mulligan depth while the proactive decks climbed
to ~48%. Mulligan behavior and reactive win rate are therefore decoupled: the
reactive collapse is a piloting problem (one policy collapsing onto the aggro
strategy across eight archetypes), not a mulligan or information deficit, so
the next lever after this run is matchup-weighted scheduling rather than
another observation or reward change.

### Room/Exhaust evidence replay — July 19, 2026

The follow-up replay to the affected-evidence run exercised its four retained
gap families across 43 cards: every Standard Room plus the Exhaust and
unlock-adjacent creatures. The report is
`probe_runs/standard-room-exhaust-evidence-2026-07-19-v2/card_probe_report.json`
with canonical SHA-256
`107e99c8495f36105a58005a7ceafdf800848afb70645242fee866f575b17adb` and
physical SHA-256
`a1da84abd642fa8bac97aeda9fe4fc14eb4618861999b88450d81bcde22c33be`.

Result: 0 mechanical passes, 36 coverage gaps, and 7 failed. Room-face
provenance was fully exercised (38/38), as were most spell, choice-branch, and
negative-mask obligations; the dominant remaining gap families are static
preflight evidence (38), alternate-face registration (23), split-layout paths
(23), and static surfaces (18). The retained diagnostics triage the seven
failures into six root causes, none of which touch the eight-deck training
corpus:

- Charred Foyer // Warped Space: the cast-from-exile alternative-cost clause
  ("Once each turn, you may pay {0} rather than pay the mana cost for a spell
  you cast from exile") cannot be classified; the primary cast, trigger, and
  matched negative event all fail.
- Ghostly Dancers and Greenhouse // Rickety Gazebo: a mandatory
  `ReturnToHandEffect` resolved with an empty target set without a validated
  post-commit invalidation context.
- Mirror Room // Fractured Realm: the trigger-doubling static ("that ability
  triggers an additional time") is misrouted to the layer system, which has no
  layer for it.
- Victor, Valgavoth's Seneschal: the graveyard-to-battlefield trigger resolves
  into an unimplemented `AbilityEffect`, and the effect continuation finishes
  with a failed instruction.
- Walk-In Closet // Forgotten Cellar: both halves of its graveyard
  replacement ("if a card would be put into your graveyard from anywhere this
  turn" / "exile it instead") are unimplemented `AbilityEffect`s.
- Pit Automaton: fails only its declared-partial static preflight evidence;
  no runtime diagnostic.

Every card remains `semantic_status=unverified`. The repair order is the two
shared-family defects first (empty-target return, trigger-doubling routing),
then the three unimplemented-effect cards, each scenario-first through the
production mask/cast/trigger pipeline.

**First repair batch — July 20, 2026.** The empty-target return family is
repaired at the parser level, scenario-first:

- Non-targeted graveyard recovery ("return a/an/one/up to one `<type>` card
  from your graveyard to your hand") now resolves as a
  `return_from_graveyard` resolution choice instead of a manufactured
  mandatory targeted bounce. Unknown quantifiers or type words fail closed;
  targeted returns and "return this card" self-returns keep their existing
  paths.
- Ghostly Dancers' trigger is the first member of a bounded inline-or modal
  family: "return … to your hand or unlock a locked door of a Room you
  control" now chooses its mode as the trigger is put on the stack (CR
  603.3c) through the existing `trigger_mode` flow. The unlock arm chooses
  among the controller's locked doors and routes through
  `complete_door_unlock`, extracted from the paid public unlock so free and
  paid unlocks share one production pipeline (layers, unlock events, chapter
  advancement).
- The linked-mill recognizer accepts "mill N cards, then return up to K
  `<type>` cards from among them to your hand" with multi-select bound to
  the physically milled cards (Rickety Gazebo), alongside the existing
  "you may put a … from among the milled cards" phrasing. Mandatory
  "then return two …" phrasings still fail closed.

Four permanent scenarios in
`tests/nontargeted_return_choice_regression_test.py` pin the family through
the production mask/apply/trigger pipeline, including the mandatory
no-decline mask, the exact-door unlock, and the empty-graveyard quiet
resolution. The delivery gate is green at 800 discovered unit tests and all
eight default invariant-fuzz seeds plus the controlled phase-boundary check.
A scoped production re-probe
(`probe_runs/room-exhaust-repair1-2026-07-20-v1`) moved Ghostly Dancers and
Greenhouse // Rickety Gazebo from `failed` to `coverage_gap` with zero
diagnostics; both remain `semantic_status=unverified`. Still failed from
this probe family: Charred Foyer // Warped Space (cast-from-exile
alternative cost), Mirror Room // Fractured Realm (trigger doubling),
Victor (reanimation effect), Walk-In Closet // Forgotten Cellar (graveyard
replacement), and Pit Automaton (declared-partial ledger evidence).

**Second repair batch — July 20, 2026.** The remaining runtime failures from
the first batch were repaired scenario-first, plus a subtype-aware extension
found during ledger regeneration:

- Fractured Realm trigger doubling: "if a triggered ability of a permanent
  you control triggers, that ability triggers an additional time" is now
  recognized as an event modification, not a CR 613 layer effect (it was
  misrouted to the layer system, which warned and dropped it). A flagged
  `StaticAbility.is_trigger_doubling` is read live at trigger-queue time with
  Room-door gating; each live source queues one extra copy of every
  permanent trigger the controller controls. Pinned by
  `tests/trigger_doubling_regression_test.py` (doubled with the Fractured
  Realm door unlocked, single with it locked).
- Victor's resolution-count ladder: "surveil 2 if this is the first time
  this ability has resolved this turn. If it's the second time … If it's the
  third time …" now runs exactly the arm matching a per-turn resolution
  count stamped at resolution (CR 608), and the third-time reanimation is a
  real non-targeted put-from-any-graveyard choice. A fourth+ resolution does
  nothing. Pinned by `tests/victor_resolution_ladder_test.py`.
- Forgotten Cellar's compound unlock: a turn-scoped "you may cast spells
  from your graveyard this turn" permission plus a turn-scoped "if a card
  would be put into your graveyard from anywhere this turn, exile it
  instead" replacement, scoped to the controller's graveyard only. Pinned by
  `tests/forgotten_cellar_regression_test.py`.
- Subtype-aware non-targeted graveyard return: the first-batch return parser
  handled only card types, so "return a creature or Vehicle/Spacecraft card
  from your graveyard" fell through. It now recognizes the Vehicle and
  Spacecraft subtypes and `non-<subtype>` exclusions (Carrion Cruiser, Fell
  Gravship, Overlord of the Balemurk). Pinned by a Carrion Cruiser scenario
  in `tests/nontargeted_return_choice_regression_test.py`.

Charred Foyer // Warped Space and Pit Automaton were confirmed correctly
fail-closed rather than repaired: their failures are the cast-from-exile
alternative cost and the source-coupled exhaust-copy trigger respectively,
both genuinely bounded mechanics the ledger already flags (`unparsed` and
`partial`), not false-clean bugs.

The support ledger was regenerated so it reflects the improved parser (same
corpus, label, and overrides; only Victor and Walk-In Closet changed,
`partial` → `unseen`, zero regressions across all 4,702 cards). Its new
declared SHA-256 is
`4ed2dce764032d9cfdfa7e2f9721bc0514ac6bef0b514daaa15a58f1db5b7e14` and its
physical file SHA-256 is
`17a74dae77ba533a8d3ef745790ceeb217f3f6777470142b58e5d17db0dea7f7`.

The delivery gate is green at 806 discovered unit tests and all eight
default invariant-fuzz seeds plus the controlled phase-boundary check. The
re-probe at `probe_runs/room-exhaust-repair2-2026-07-20-v1` (canonical
SHA-256
`9dbe252579fb27637213e8d26f4696265a1f9d7c21dfed05a300792d0bea7c58`) moved
seven of the eight affected cards to `coverage_gap` with zero diagnostics.
Fell Gravship remains `failed` on its Station and "8+" level layering — a
documented bounded mechanic (Station layering) unrelated to the repaired
return clause. Every affected card remains `semantic_status=unverified`.

### Evidence-ledger reset and first trusted-card repair — July 19, 2026

Support ledger/override schema v2 removed every name-only semantic
certification. All 96 former names remain as `legacy_verified_claims` audit
metadata, while all 4,702 cards are `semantic_status=unverified`. A future
`verified` record must have no static issues and must pin Oracle identity and
rules, an independent Oracle-text plus dynamic-probe surface inventory, exact
surface coverage declarations, test-file/node hashes, and
`exact_state_v1`. The validator executes each named assertion-bearing
unittest exactly once and rejects stale, skipped, failing, assertionless, fake,
or incomplete candidate evidence. A passing Python test cannot itself prove
that every declared surface was semantically established, so promotion is
hard-disabled until that mapping has a machine-verifiable artifact; nonempty
`verified` overrides currently fail. The regenerated ledger's canonical SHA-256 is
`ab98966df91afa68995a3f1dc08101cd955b2197920b8e4969f20e53f9401b70`.

The first trusted-card pilot added permanent public-pipeline evidence for
Afterburner Expert and Room unlock triggers. It found and repaired two concrete
Roaring Furnace false-clean bugs: the trigger heard another Room or the wrong
door, and variable damage silently defaulted to 1 instead of the live hand
size. A duplicate-condition review then caught and repaired the corresponding
false-negative risk for Rooms whose two halves both say “When you unlock this
door.” Dynamic “damage equal to the number of” expressions now retain their
quantity through resolution; supported counts resolve strictly and unsupported
counts fail closed instead of dealing 1.

The affected replay at
`probe_runs/standard-affected-evidence-2026-07-19-v1/card_probe_report.json`
contains four resumable artifacts: 0 failed, 4 coverage gaps, and 0 mechanical
passes. The gaps are retained deliberately: generic Exhaust/Room trigger
fixtures, full-Room unlock coverage, alternate Room-face registration, and
split-layout paths are not yet complete. The report's canonical SHA-256 is
`eac24b1f92bce0363fcbd17e8a2fc2c742869ca5e34cee99fd433813f69a865b`.

### Repair-affected schema-v3 replay — July 18, 2026

The repair-surface inventory selected 770 unique cards: 555 broad-parser0
matches, 90 cards across 91 fidelity surfaces, 8 token-count surfaces, and 167
cards across 174 copy/token lines, with overlaps deduplicated. Its alphabetical
selection SHA-256 is
`7fb5d7f4feda734028d5196614d3638ce597137cd5be09b5e3f7b731cb41dba8`;
its registry-order SHA-256 is
`5e88ae9467be4fe2f33a037b0b809c19844cf5423a6f9f3a3c4874ff5fa881e2`.
The report is
`probe_runs/standard-repair1-expanded5-2026-07-18-v1/card_probe_report.json`.

| Prior ledger status | Failed | Coverage gap | Mechanical pass |
| --- | ---: | ---: | ---: |
| `verified` | 0 | 23 | 9 |
| `observed_clean` | 0 | 13 | 10 |
| `unseen` | 101 | 278 | 0 |
| `partial` | 161 | 0 | 0 |
| `unparsed` | 175 | 0 | 0 |
| **Total** | **437** | **314** | **19** |

Independent pre- and post-resume audits found zero errors, zero warnings, and
zero violations among all 22 grouped-zone-change clauses. The identical resume
accepted all 770 artifacts without changing the report or card manifest. The
physical report SHA-256 is
`f9b4d4f9a3aba6cc4eb49b4ac3802822a3551a158de68ef6f2e3d06592d28f10`;
the canonical report SHA-256 is
`fa15cc8b6b8a685a67d242e458028fce739c61049d5bd72c8b7c376c3466135e`;
the canonical card-manifest SHA-256 is
`377146e758479bd26e612064a3ec0a643206af3678acf2986fea5ac4e06c2303`.
Every selected card remains `semantic_status=unverified`.

### Fresh full-pool schema-v3 repair replay — July 18, 2026

The fresh replay completed all 4,702 frozen Standard cards. Its report is
`probe_runs/standard-full-schema3-repair1-2026-07-18-v3/card_probe_report.json`.

| Prior ledger status | Failed | Coverage gap | Mechanical pass |
| --- | ---: | ---: | ---: |
| `verified` | 0 | 72 | 24 |
| `observed_clean` | 0 | 39 | 24 |
| `unseen` | 695 | 2,653 | 0 |
| `partial` | 745 | 0 | 0 |
| `unparsed` | 450 | 0 | 0 |
| **Total** | **1,890** | **2,764** | **48** |

Independent pre- and post-resume audits each found zero errors and zero
warnings, 4,704 of 4,704 expected files, all 4,702 cards loaded and reported,
and zero violations among all 22 grouped-zone-change clauses. The identical
resume accepted all 4,702 artifacts without recomputation or hash drift. The
physical report SHA-256 is
`c118a76b8601f356b9f88e868378fe3bcc6055b38207559b18dafdefd9f83dfa`;
the canonical report SHA-256 is
`e9dc842ad77dbbb7a92ee1a139b6633861d948b45ff650536340098040cbda38`;
the canonical card-manifest SHA-256 is
`4b0dc370c570f877a3c72e3273a44caa6ad6a4b6ce5d273af3d3ff83961f1c51`.

Relative to the historical schema-v3 run, 81 cards moved from `failed` to
`coverage_gap`, 41 previously unseen cards moved from `coverage_gap` to an
explicit fail-closed `failed`, and Manifold Mouse moved from `coverage_gap` to
`execution_passed`. The 159 prior `verified`/`observed_clean` cards have zero
failures, 111 gaps, and 48 bounded mechanical passes. Their largest remaining
gap-obligation families are printed conditionals (66), static surfaces (44),
modes (22), triggers (22), and optional branches (18). Diagnostics occur on
1,531 cards and support-manifest reports on 818. Every card remains
`semantic_status=unverified`; no probe result is rules certification.

### Historical full-pool dynamic-probe repair baseline — July 18, 2026

The fail-closed production-pipeline reconnaissance completed all 4,702 frozen
Standard cards with hash-valid terminal artifacts: 1,774 `failed`, 2,910
`coverage_gap`, and 18 `execution_passed`. All 4,702 remain
`semantic_status=unverified`; a bounded execution pass is not a rules-support
certification. The report is
`probe_runs/standard-full-2026-07-18-v2/card_probe_report.json` with SHA-256
`5a020931c2969c2859653e46504c211e07747acbf157fd9b3a77d52e3699d002`.

| Prior ledger status | Failed | Coverage gap | Mechanical pass |
| --- | ---: | ---: | ---: |
| `verified` | 0 | 86 | 10 |
| `observed_clean` | 0 | 55 | 8 |
| `unseen` | 579 | 2,769 | 0 |
| `partial` | 745 | 0 | 0 |
| `unparsed` | 450 | 0 | 0 |

Compared with the first full-pool run, 168 cards moved from `failed` to
`coverage_gap`, reducing failures from 1,942 to 1,774. All 14 failures among
previously `verified`/`observed_clean` cards were repaired; those cards now
remain coverage gaps rather than being overclaimed as semantic passes. Pest
Control moved from `execution_passed` to `coverage_gap` because rule-keyword
metadata is now tracked as an explicit unexercised obligation. There are 1,380
cards with runtime diagnostics. The largest explicit coverage obligations are
static effects (2,786), choice branches (2,717), triggers (2,611), and optional
branches (1,207). These counts are the repair/coverage baseline; no ledger
promotion follows without scenario-backed exact-state assertions for every
distinct path.

### Trusted-card independent-branch replay — July 18, 2026

The follow-up run exercised all 159 cards in the prior `verified` and
`observed_clean` ledger classes using fresh-fixture replay for each public
choice edge. The final report is
`probe_runs/standard-trusted-branch-replay-2026-07-18-v3/card_probe_report.json`
with SHA-256
`0d219ad2ea8da7085e030ac81d00664e21cdba8c87ab244c4fdc957c11c75cfe`.

| Prior ledger status | Failed | Coverage gap | Mechanical pass |
| --- | ---: | ---: | ---: |
| `verified` | 0 | 71 | 25 |
| `observed_clean` | 0 | 27 | 36 |
| **Total** | **0** | **98** | **61** |

Relative to the repaired full-pool baseline's trusted slice (141 gaps and 18
mechanical passes), 43 cards moved from `coverage_gap` to
`execution_passed`. All 133 discovered `choice_branch` obligations were
exercised as independent edges; the remaining largest explicit gaps are 71
triggered, 44 static, 22 mode, 16 optional-branch, and 10 layout obligations.
Every card remains `semantic_status=unverified`: this run is runtime
reconnaissance, not permission to call the old trusted classes clean.

### Trusted trigger and printed-surface replay — July 18, 2026

The schema-v3 replay re-audited the same 159 prior `verified` and
`observed_clean` cards with independent printed-trigger discovery, fresh
fixtures for every recognized compound trigger arm, exact queue/stack identity,
and matched non-event proofs. The resume-validated report is
`probe_runs/standard-trusted-trigger-replay-2026-07-18-v1/card_probe_report.json`
with file SHA-256
`d46b8427b0af0b0a3be6a919eac4998b014a92977a86f49b61409626de47b1de`.

| Prior ledger status | Failed | Coverage gap | Mechanical pass |
| --- | ---: | ---: | ---: |
| `verified` | 0 | 73 | 23 |
| `observed_clean` | 0 | 39 | 24 |
| **Total** | **0** | **112** | **47** |

All 71 registered trigger obligations were split into 77 independently replayed
event arms: 55 arms completed and 22 remain explicit gaps. Matched close
non-events exercised 55 of 71 triggers with zero false deliveries; all 161
discovered public choice branches completed. An independent lexical inventory
found 81 printed trigger clauses, matched 68 to registered runtime abilities,
and kept 13 unmatched clauses as gaps instead of allowing parser omissions to
disappear. Printed conditions add 72 obligations, of which 6 are exercised and
66 remain gaps.

The stricter evidence contract deliberately moved 26 former mechanical passes
back to gaps (principally unexercised conditional outcomes and unmatched printed
triggers), while 12 prior gaps became passes after authentic trigger fixtures
and rules repairs. No card failed, emitted diagnostics, or reported degraded
support. Every card remains `semantic_status=unverified`; lower pass counts here
represent more honest discovery, not reduced runtime correctness.

### Historical full-pool schema-v3 trigger and printed-surface baseline — July 18, 2026

The strengthened probe completed all 4,702 frozen Standard cards and then
accepted all 4,702 artifacts on an identical `--resume` pass without
recomputation. Independent audit found no missing or extra artifacts, identity
or hash mismatches, duplicate obligations, bad report links, schema drift, or
summary discrepancies. The report is
`probe_runs/standard-full-schema3-2026-07-18-v1/card_probe_report.json` with
physical file SHA-256
`2165f074334614706bac9e941eb90c43a90adac16f438f4ad500131482ff8091`
and embedded canonical SHA-256
`e44b4fc2d860fbaa8559a5dc4e295613198531f38b5475a689195c2e6437153e`.

| Prior ledger status | Failed | Coverage gap | Mechanical pass |
| --- | ---: | ---: | ---: |
| `verified` | 0 | 73 | 23 |
| `observed_clean` | 0 | 39 | 24 |
| `unseen` | 735 | 2,613 | 0 |
| `partial` | 745 | 0 | 0 |
| `unparsed` | 450 | 0 | 0 |
| **Total** | **1,930** | **2,725** | **47** |

Independent printed-text discovery found 3,124 trigger clauses: 2,599 matched a
runtime trigger and 525 remain unmatched gaps. Of 2,672 positive trigger arms,
1,462 were exercised, 1,064 remain gaps, and 146 failed. Matched close
non-events exercised 1,574 of 2,611 triggers, with 848 gaps and 189 failures.
Public choice discovery exercised 3,287 of 3,480 branches; 1,220 of 1,231
printed conditional branches, 575 of 586 optional branches, 474 modes, 98 mode
combinations, and 95 variable choices remain explicit gaps.

This is a deliberately noisy repair baseline. Diagnostics appeared on 1,587
cards and the support manifest reported 897 cards; their union is 1,596 cards,
and all are failed. The largest diagnostic families are Phyrexian `{P}` mana
tokens, Start Your Engines layering, and Station layering. Coverage work is led
by static/runtime surfaces, conditional fixtures, unsupported trigger fixtures,
trigger negative fixtures, optional branches, and unmatched printed triggers.
All 4,702 cards remain `semantic_status=unverified`; the 47 mechanical passes
are not rules certification.

The repair order recorded at that baseline was: first regress the Cerebral Download `Surveil x` hard
crash; then eliminate the 140 close-non-event false trigger deliveries through
shared source-identity and controller/type scoping. Next repair continuation
integrity (36 continuation failures, 17 identical-state loops, Jadelight
Spelunker's zone loss, and five mask-valid execution failures). After those
high-confidence defects, work down the shared parser families, reconcile the
525 unmatched printed triggers, add authentic fixtures for the 977 unsupported
trigger events, and finally expand conditional, optional, modal, variable,
replacement, static, layout, and alternate-face branch coverage. Re-run the
complete schema-v3 pool after each coherent repair batch; promote ledger status
only from independent exact-state scenarios.

### Non-negotiable lineage rules

- **Round 7.99 is complete. Observation v6 and its bounded
  archetype-conditioning path are implemented and the full delivery gate has
  passed. The next task is to define and launch a fresh named canary rather
  than resume any earlier checkpoint; no v6 canary or command exists yet.** Preserve
  `tempo-graded-potential-v1`, `combat-v7`, and the pinned checkpoint-league
  contract so the new strategy input and conditioning path remain the only
  experimental lever. Observation v5 added producible mana by color
  (`my_producible_mana`, `opp_producible_mana`) on top of the v4 decklist
  features (`my_deck_card_identity`, `my_library_composition`, full-deck
  `deck_composition_estimate`). Own producible mana is exact; the opponent's
  is the public estimate from its face-up untapped lands, and all
  decklist-derived features remain observer-own only. Observation v3's
  corrected mana, land-development, resource-advantage, and
  strategic-viability semantics carry forward. Adaptive card/deck history is
  recorded but excluded from live evaluator advice by default so worker-local
  histories cannot make the same public state nonstationary. Recorded
  archetypes are canonical and play-turn analytics are player-relative;
  targetable observations match the active target instruction. Observation v6
  adds the observer-own `my_exact_deck_strategy_profile` (`float32`, `(54,)`,
  bounds `0..1`) and consumes it only through dedicated bounded FiLM; exact
  opponent deck metadata remains forbidden. Its schema hash is
  `6521db9c0c70c919a63c34e9c99463a3b801e25ae91149fd518a34054989e790`, and
  the extractor architecture has its own required lineage identity. Every
  prior checkpoint and named canary, including Round 7.99, is incompatible.
  Round 7.99 remains an immutable v5 result with 500k permanent recovery and
  100k self-play publication cadence; Round 7.98 remains an immutable failed
  diagnostic boundary. Generic runs still leave both checkpoint self-play and
  matchup weighting off by default.
- Resume now verifies the companion manifest's reward contract and Observation
  version/hash. Curriculum continuation is intentionally rejected until its
  per-worker scheduler counters can be checkpointed; launch a fresh
  current-round run.
- Do not mix pre-7.46 statistics with format-namespace statistics.
- Statistics collected before July 2026 are unusable. They were affected by
  perspective, winner attribution, fabricated play-turn, first-strike, layer,
  and replacement-system defects and must be re-harvested.
- Any future policy-observation change creates another explicit schema and
  checkpoint boundary.

---

## Round 7.91 — climbable curriculum ramps and reward contract v6

Analysis of `round-7.90-memory-redundancy-v1` (stopped at ~188k steps)
confirmed the 7.89/7.90 canary behaved exactly as designed — goldfish mastered
at 47,488 in both runs, race fell to its deadline at 147,488 in both, and the
100k evaluations matched (9-44, qualification 0.141) — so the memory-redundancy
refactor is behavior-neutral. The agent, however, showed the same plateau as
Round 7.88's full million-step run:

- ~60% decisive wins against passive opponents collapsing to ~5% against
  novice, flat from 57k to 188k timesteps. There was no gradient between the
  two difficulty levels to climb.
- Decisive wins averaged turn 25 while losses averaged turn 17: the policy's
  clock was slower than every active opponent's, and ~250-step episodes meant
  only ~750 games in the whole run — starvation-level sample counts.
- The critic's explained variance collapsed (0.84 → 0.11) at each stage
  transition, and 6 of 15 evaluation "wins" were life-lead timeouts scored
  exactly like losses.

Round 7.91 lands four repairs (opponent-profile observation features were
deliberately rejected to keep the policy deck- and opponent-agnostic):

1. **`combat-v4` annealed handicap.** Race and bridge open with weakened
   active profiles: with probability epsilon the opponent takes the passive
   baseline for one priority decision. The trainer ratchets epsilon toward
   zero each time a rolling window of decisive wins at the current epsilon
   clears the stage target (race: novice 0.75 start, 0.25 step; bridge:
   scripted 0.60 start, 0.20 step). Mastery requires the anneal to finish and
   the profile floors are now satisfied only by full-strength episodes;
   `opponent_handicap` is recorded in every game log and mastery record.
2. **Stage turn limits.** Goldfish/race play to 20 turns and bridge to 25
   (full pool keeps the engine default), buying more terminal outcomes per
   timestep without moving the observation-space bound; the per-episode limit
   is recorded as `max_turns` in game logs.
3. **Reward contract v6** (`discounted-state-potential-v6`): a life lead at
   the turn limit now pays -8 instead of -10 (all other limit outcomes stay
   -10), so "almost won" is no longer worth exactly as much as losing while
   remaining worse than a decisive win (a decisive loss still pays -10). The convex
   damage-progress potential weight doubles (0.40 → 0.80). Resume remains
   version-gated, so v5 checkpoints cannot enter this lineage.
4. **Paired-seat observation audit.** The 7.90 evaluation's 5-27 (P1) versus
   10-20-2 (P2) split motivated a standing regression
   (`tests/seat_parity_test.py`) covering the audited `my_*`/`opp_*` fields on
   paired constructed states. It passes, so that tested extractor slice did
   not reproduce the split; it is not proof that every runtime seat asymmetry
   has been eliminated.

`combat-v4` was the Round 7.91 default and `combat-v3` remains resolvable for
reproducibility. At that time, Round 7.92 superseded both for fresh runs with
`combat-v5`.
Fixed evaluation continues to use full-strength scripted opponents and the
engine-default turn limit.

### Run 1 result (`round-7.91-annealed-ramp-v1`) and the choice-phase fix

The first v4 run validated the ramp design and died on a pre-existing engine
defect at ~223k steps. Goldfish fell to its 75k deadline (the 20-turn limit
converted the fresh policy's slow kills into ~71% timeouts — a stage-tuning
miss to revisit), but race then ratcheted 0.75 → 0 on demonstrated win rate
and **advanced via mastery at 169,332** — the first earned transition past
goldfish in any round, with ≥20% decisive wins against full-strength novices
(previous rounds plateaued near 5%). The 100k evaluation scored 0.188
(12-45), beating 7.88 (0.094), 7.89/7.90 (0.141), and matching 7.88's 300k
checkpoint. Bridge climbed 0.60 → 0.20 before the crash.

The crash: a combat-damage trigger fetched Multiversal Passage, its
as-enters choice opened mid-resolution, and `_finish_damage_step` overwrote
the CHOOSE phase with END_OF_COMBAT. Choice actions are phase-routed, so the
pending `as_enters_pay_life` decision became unreachable and strict training
correctly aborted on a period-1 PASS cycle. Fixed at both layers
(`tests/choice_phase_integrity_test.py`): the damage step now defers its
transition through `previous_priority_phase` when a decision context is
pending, and mask generation self-heals any future orphaned decision context
by restoring its phase and chooser instead of letting an episode brick. The
handicapped scripted opponents block far more than earlier rounds, which is
why this dormant path finally fired.

Run 2 (`round-7.91-annealed-ramp-v2`) reproduced run 1 deterministically,
annealed bridge to full strength at 244k, entered full_pool at 369k, and died
at ~375k on a second dormant defect: the COUNTER_SPELL mask matched
"counter target spell" without validating rider clauses, so a mask-valid
Spell Snare aimed at a mana-value-1 Daydream failed execution. The mask now
runs the same targeting-system validation as `cast_spell` and aims at a
spell that is actually a legal target (`tests/counter_spell_mask_test.py`).

The older Round 7.89/7.90 interrupted-run manifests can still report
`status: running`; that is a historical lifecycle-recording defect, not
evidence that either process remains active. Their checkpoint results remain
diagnostic only.

### Run 3 verdict (`round-7.91-annealed-ramp-v3`, interrupted at ~730k)

Run 3 cleared both fixed defects and produced the round's full result:
fastest climb in project history (0.281 qualification at 400k versus 7.88
needing 800k+ for 0.25), then an oscillating plateau — 0.188, 0.203, 0.109,
0.281, 0.188, 0.250, 0.109 across the seven evaluations. The old interval was
a descriptive Bernoulli estimate over repeated, paired cases and should not be
read as a precise independent-sample bound. The curriculum ramps raised the
*speed* to competence, but did not show a higher ceiling. The pooled 448-game
diagnostic suggests concentrated deck-skill holes rather than diffuse
weakness; because it reuses cases across correlated checkpoints, it does not
locate the ceiling precisely.

Those scores are not a controlled comparison with Rounds 7.89/7.90. Those
runs used eight environments, training/evaluation seeds `20260715`/`21260715`,
and schedule hash `f5aa…`; Round 7.91 used six environments, seeds
`42`/`1000042`, and schedule hash `bde3…`. Each suite was fixed within its own
run, but the cases and training configuration differed across rounds.

The pooled diagnostics were:

| Skill hole | Evidence (eval, all 7 checkpoints pooled) |
| --- | --- |
| Piloting reactive decks | 4c Control 0-43 (0%), Jeskai 12%, Dimir 11% as agent decks; training full_pool win rates 3-4% on the same decks — 37% of full_pool episodes produce near-pure loss signal |
| Beating token swarms | 1-54 vs Azorius Momo, 4-52 vs Selesnya as opponents |
| Closing against durdle | 30 and 34 of 56 games vs 4c Control / Jeskai hit the 31-turn limit |

Round 7.92 implemented the two stage-level responses: `combat-v5` (the
fresh-run default at that time) gave goldfish 25 turns — runs 1-3 all lost
goldfish to its deadline at ~71% timeouts under 20 turns — and extended the
annealed handicap
to the scripted portion of `full_pool` (epsilon 0.40 → 0 by ratchet). The
full-pool bag is 80% scripted and 20% novice, but before v5 both active
profiles began at full strength; it was therefore 100% active full-strength,
not 80% full-strength. The ratchet now keeps a separate window of qualifying
scripted outcomes, so interleaved novice games cannot prevent the required 24
scripted samples from ever accumulating.

The same fix batch makes fidelity enforcement cover failed effect
continuations and lost-spell recoveries (with diagnostic contexts), persists a
pair-aware 95% qualification interval, and gates promotion to `best_model.zip`
on its lower bound rather than the point estimate. These changes pass the
delivery gate.

The pilot-side skill holes need a different
lever and are deliberately **not** addressed by v5; candidates for the next
decision, in rough order of expected value:

1. Matchup-weighted scheduling: oversample the agent piloting its worst
   decks once a win-rate floor exists, or handicap opponents specifically
   when the agent pilots a reactive deck (per-matchup epsilon).
2. Larger held-out paired-seat qualification suites if the lower confidence
   bound remains too wide for a promotion decision.
3. Policy capacity/optimization (network width, rollout length, entropy
   schedule) — only after the curriculum-level levers are exhausted, and
   as a single attributable canary change.

The named `--canary-config round-7.92` contract checks its enumerated CLI and
complete PPO setting tree, reward-contract version/scalars, the full resolved
curriculum hash, feature-output width, CUDA device class, hashed lineage, and
evaluation schedule: one million timesteps, 100k periodic evaluation with 64
games (32 seat-swapped pairs), 50k checkpoints, eight training environments,
training seed `20260715`, independent evaluation seed `21260715`, Observation
v3, reward v6, and `combat-v5`. Mismatch in those checked fields fails before
training. Git/patch provenance, runtime libraries, and the specific GPU model
are recorded for audit rather than constrained by the canary selector.

### Run 1 result (`round-7.92-combat-v5-v1`) and the delayed-return fix

The first v5 run stopped at 393,256 timesteps when a mask-valid priority pass
advanced into the end step and Parting Gust returned Namor the Sub-Mariner
with a +1/+1 counter. Battlefield abilities were registered after enter
counters, so the counter's immediate layer pass encountered Namor's unresolved
`*` power before its characteristic-defining ability existed and raised a
`TypeError`. The pass action itself and its mask were correct.

Battlefield abilities are now registered before enter counters, and Layer 7c
leaves unresolved symbolic power/toughness alone until a characteristic-
defining effect resolves it. Replay also restores the recorded opponent
handicap and turn limit, making the retained 162-action production failure
artifact deterministic through the repaired transition. The exact replay,
430 unit tests, 404 golden scenarios, runtime and training smoke suites, and
the default invariant fuzz gate are green. Curriculum scheduler state is not
checkpointed, so the 350k checkpoint remains diagnostic only; launch a fresh
`round-7.92-combat-v5-v2` canary rather than resuming it.

---

## Earlier run finding — `round-7.87-curriculum-v5` is diagnostic-only; Round 7.88 repaired

`ALPHA_ZERO_MTG_V3.00_20260715_002725_round-7.87-curriculum-v5` was manually
interrupted at 107,200 steps. It was fidelity-clean, but the fixed curriculum
advanced much faster than demonstrated ability: goldfish finished 33 decisive
wins, no losses, and 49 timeouts; race then fell to 13 wins and 265 losses;
bridge recorded 5 wins and 169 losses. The 25k fixed evaluation consumed about
937 seconds and returned 1 win, 50 losses, and 13 timeouts, yet that first weak
checkpoint was still promoted to `best_model`. The 100k boundary was skipped
because the single evaluator was backlogged. Critic values also became much
larger than rollout rewards while fit quality was unstable, and per-card gzip
statistics produced avoidable small-file I/O.

Round 7.88 keeps reward v5 and Observation v2, but changes the training and
run-lifecycle controls:

- `combat-v2` advances only after a rolling mastery window meets decisive-win,
  loss, timeout, and minimum-stage-duration gates. Opponent strength ramps via
  mixtures instead of jumping from passive to 100% novice.
- Full evaluation defaults to every 100k steps. A checkpoint needs a 55%
  qualification score (decisive wins plus half non-timeout draws) before
  promotion to `best_model.zip`; best-so-far candidates remain observable.
- Evaluation game logs identify the exact checkpoint timestep and SHA-256.
  Backlog skips and interruption cancellations are durable, pending snapshots
  are cleaned up, and user interruption has its own manifest status and
  `interrupted_model` artifact.
- Every run has `logs/<run>/training.log`. Training statistics batch compressed
  aggregate writes for ten games and flush on shutdown. Critic scale telemetry
  now warns on repeated value/reward divergence.

Round 7.89 preserves `combat-v2` for reproducibility and made `combat-v3` that
round's fresh-run default. Its mastery window retains opponent profile; race
requires a novice floor, and bridge requires separate novice and scripted
floors. Stage
deadlines are reported as forced transitions and bound full-pool entry to about
375k timesteps, rather than allowing bridge to consume most of the run.

---

## Previous run finding — `round-7.86-combat-v4` is diagnostic-only; Round 7.87 repaired

`ALPHA_ZERO_MTG_V3.00_20260714_195653_round-7.86-combat-v4` was interrupted
after its 350k checkpoint. Combat was reachable and the engine recorded 1,303
training games, but the policy did not learn a reliable win condition: 309
games reached the turn limit, 99 of the 137 logged wins were only timeout life
leads, and just 38 games were decisive wins. The 125k checkpoint selected as
"best" later went 0–10 with six timeouts; the 250k checkpoint went 3–7 but was
not promoted. The critic was already fitting its targets (explained variance
about 0.874) while policy KL remained around 0.003, pointing to a weak policy
update and a bad objective/evaluator rather than an underfit value network.

The postmortem found one Priority 0 reward bug: when the scripted opponent's
action ended a game, its acting-seat `game_result` was discarded. PPO commonly
received the `-0.25` fallback instead of the learned seat's `+10`/`-10`
terminal outcome even though later statistics inferred the result correctly.
Round 7.87 establishes a new reward/training boundary:

- Reward contract v5 translates opponent-ended results back to the fixed
  learned seat, gives all turn limits and safety truncations `-10`, removes
  handler-local action reward from the optimized objective, and records a
  terminal-result sign diagnostic.
- Periodic evaluation uses one immutable 64-game suite of 32 seat-swapped
  deck/seat/seed pairs.
  Promotion is lexicographic: decisive wins, decisive win-minus-loss score,
  fewer timeouts, then shaped return. `evaluations.json` retains every case,
  resolved runtime identity, outcome, checkpoint SHA-256, and promotion
  decision. Adaptive card/deck history is disconnected from evaluator choices,
  so checkpoint order cannot change the fixed opponent.
- `combat-v1` provides deterministic, worker-isolated opponent progression:
  passive two-deck goldfish at 0, novice race at 30k, four-deck mixed bridge
  at 75k, and the full mostly-scripted pool at 125k. Fixed evaluation always
  overrides the curriculum and uses scripted play.
- PPO defaults move to learning rate `2e-4`, rollout `1024`, batch `256`, gamma
  `0.999`, GAE lambda `0.98`, value coefficient `0.25`, and five epochs. This
  is a measured canary configuration, not a claim that tuning is finished.
- Harvest qualification and promotion give timeout life leads zero points.
  Matchup/profile/stage identity is now present in run manifests, game logs,
  replays, and TensorBoard outcome telemetry.
- Reward-v5 telemetry now covers fail-closed early returns as well as ordinary
  terminal transitions. Dirty-run source patches include untracked files, and
  cloned deferred resolutions safely initialize missing turn-ledger buckets.

Observation v2 itself is unchanged at
`8b77a325816aec9fd6a8b7a8e924a2b936a092e163b81f2d0a22947387804ea8`.
Do not resume `round-7.86-combat-v4`; its policy optimized the broken reward
and its checkpoints were selected by the old random, return-based evaluator.

---

## Previous run finding — `round-7.85-reward-v8` invalid, Round 7.86 repaired

`ALPHA_ZERO_MTG_V3.00_20260714_172013_round-7.85-reward-v8` was interrupted
at roughly 275k steps and is diagnostic-only. Its persisted training logs
contain 600 games: 548 turn limits (91.3%), 41 decking losses, only 10
life-total endings (1.7%), and zero fidelity-counter failures. The low lethal
rate was not primarily a policy-strength result: the public damage phases
allowed both players to pass directly to end of combat without ever invoking
the combat resolver. A minimal unblocked 3-power attacker reproduced the
failure with both masks exposing only Pass and the defender remaining at 20.

Round 7.86 closes the live combat and reward defects exposed by that run:

- Empty-stack double-pass now performs mandatory combat damage and fails
  closed if the resolver does not mark the current damage step complete.
  Multi-blocker ordering follows the same failure contract.
- Attack declaration taps non-vigilance attackers, both seats use the active
  turn player for damage ownership, and combatants persist through end of
  combat before being cleared.
- First-strike and regular damage are separate priority windows. Eligibility
  is snapshotted, blocked status survives departed blockers, and double strike,
  trample, planeswalker/battle targets, ordering, and simultaneous damage use
  the preserved combat declaration.
- Canonical source-damage events now cover players, creatures, planeswalkers,
  and battles with correct source/controller/type filters. Printed and granted
  lifelink share one actual-damage path and state-based actions wait until the
  simultaneous batch, including lifelink, is complete.
- Attackers and blockers beyond the 20-card detail window use the paged action
  catalog; overflow blockers can also complete or withdraw sequential menace
  declarations beyond the ten atomic multi-block targets.
- Declaration completion has one public policy action instead of a duplicate
  Pass alias. The scripted baseline opens overflow combat choices before
  finishing declarations.
- Reward contract v4 gives every turn-limit result a flat `-6`; a small life
  lead can no longer earn `+2` by avoiding combat until timeout.

This is a new reward/gameplay checkpoint boundary but not an Observation v2
schema change. The observation hash remains
`8b77a325816aec9fd6a8b7a8e924a2b936a092e163b81f2d0a22947387804ea8`.
Do not resume the interrupted model, its best model, or its checkpoints.

## Previous run finding — `reward-v7` invalid, Round 7.85 repaired

The first full Observation v2 attempt,
`ALPHA_ZERO_MTG_V3.00_20260714_155433_reward-v7`, is not a usable policy
lineage. Strict fidelity correctly stopped it after a mask-valid Room unlock
failed in environment 2. Its 25k–100k evaluation trend is diagnostic only;
none of its best, checkpoint, or failed-model artifacts may be resumed,
qualified, or harvested.

Round 7.85 closes every issue found between the 7.84 freeze and that abort:

- Combat lookahead now restores life, poison, counters, P/T, and combat-trigger
  state after hypothetical damage, and damage accumulation no longer assumes a
  pre-existing dictionary entry.
- Stale attackers/blockers that already left the battlefield are excluded from
  combat simulation; star-P/T objects outside the battlefield safely use zero
  instead of raising on `None`.
- Room unlock masks and execution share one land-aware affordability/payment
  transaction. Generated actions pin controller, battlefield occurrence, Room
  ID, and door number; execution auto-taps the lands accepted by the mask, and
  full-unlock detection now reads the actual door dictionaries.
- Earthbend's reminder-defined target is reconstructed for activated-ability
  legality, target choice, stack validation, and resolution. Ba Sing Se now
  commits a controlled land before it taps or pays mana, eliminating the empty
  mandatory-target warning.

These are gameplay and policy-boundary repairs, not an observation-contract
change. Observation v2 remains at hash
`8b77a325816aec9fd6a8b7a8e924a2b936a092e163b81f2d0a22947387804ea8`.

---

## Latest finding — Observation v2 planner audit complete

Round 7.84 completes the audited, self-hashed Observation v2 policy contract
before its first training run:

- Canonical cards receive stable categorical identities: `0` is padding, `1`
  is visible unknown/off-registry, and canonical registry index `N` is `N+2`.
  All identity fields share one fixed 65,536-entry embedding. Per-game runtime
  IDs remain protocol metadata and never become learned semantic identity.
- Public state now includes both libraries; poison, energy, and experience;
  monarch and city's blessing; permanent counters, marked damage, attachments;
  exact attack targets and blocker assignments; richer top-first stack objects;
  both players' regular, snow, and restricted floating mana; and symmetric
  graveyard/exile windows with face-down visibility masks.
- Hand, battlefield, graveyard, exile, stack, target-page, and choice-page card
  windows carry categorical identity. Hidden opponent information remains
  zeroed; visible generated or off-registry objects use the unknown category.
- Public indices, controllers, battlefield counts, combat maps, targets, and
  zone windows are observer-relative. Perspective tests pin both seats.
- Dead or duplicated v1 fields were removed: absolute-seat life/battlefield
  copies, phase one-hot, duplicate battlefield counts/keywords/tapped/mana,
  `remaining_mana_sources`, `hand_performance`, and asymmetric key-card zones.
- `OBSERVATION_SCHEMA.md` records every field's shape, bounds, perspective,
  saturation, visibility, and extractor route. The schema version/hash is now
  mandatory lineage beside registry and feature-schema identity.
- Extractor coverage is exhaustive: phase uses its dedicated embedding,
  semantic identities use the shared categorical embedding, continuous fields
  use the symlog/MLP path, and masks/runtime IDs stay external.
- Strategy memory is no longer policy input. Its former empty-memory behavior
  injected a random legal action, reset-time reconstruction discarded unsaved
  evidence, and online per-worker state made evaluation non-comparable. The
  optional replacement is deterministic, action-specific, atomically saved,
  reused across resets, disabled by default, and isolated per environment.
- Planner-derived observations are now pure reads. Multi-turn mana development
  uses expected land draws instead of sampling, and wide-board attacker search
  uses a stable bounded combination order instead of the game RNG.
- Opponent archetype inference excludes face-down exile and face-down
  permanents. The fake `estimated_opponent_hand` tensor was removed because it
  ranked exact candidates from a runtime database containing hidden deck
  instances.
- Disabled `recommended_action` inputs were removed, and `strategic_metrics`
  was compacted from ten positions to its seven live values.

**Historical v2 verdict:** no high-priority correctness defect was known at
that freeze. Round 7.89's deeper audit nevertheless found semantic defects and
supersedes it with Observation v3 at
`6e29a94e3443881681afd794185f061133f24ff72350a7df27f48524f00d4137`.
Changing a field, bound, identity capacity, visibility rule, or extractor route
starts a new schema/checkpoint lineage. The next training run must record actual
v3 throughput and memory alongside behavior telemetry.

---

## Round 7.97 post-mortem — Observation v5 + matchup weighting did not break the piloting wall — July 21, 2026

`round-7.97-obs-v5-v1` completed a full 2M timesteps clean: zero reward-sign
failures, zero strict-fidelity counters, critic explained-variance 0.86–0.90,
no crash. It did **not** qualify. Periodic-eval qualification peaked at **0.297
@ 1.6M** and finished **0.281 @ 2.0M** against the fixed full-strength gauntlet
(eval `opponent_handicap = 0.0`, confirmed in `game_log.jsonl`), never reaching
the 0.55 promotion bar, so `best_model.zip` was never published. `final_model`
(the 2M policy) is retained.

**Not an improvement over v3 observation.** On the only cross-round-comparable
metrics — qualification and timeout rate against the fixed gauntlet — 7.97 is a
statistical dead heat with its predecessors and if anything trails: peak
qualification 7.95 **0.328** > 7.96-obs-v4-v2 **0.312** > 7.97 **0.297**, all
inside the ±0.14 interval of a 64-game eval. The v4 decklist and v5 color-mana
observations plus matchup weighting produced **no measurable gain over 7.95's
plain observation.** The ~0.30 ceiling is invariant across 7.94–7.97 regardless
of observation richness or reward (shared `tempo-graded-potential-v1`). An
earlier live-diagnosis call that 7.97 was "the strongest recent trajectory" was
wrong — it anchored on the one run being watched; the cross-round table
corrects it.

**The wall is per-deck, and it is total.** Filtering `game_log.jsonl` to the 2M
eval (64 games) gives the final policy's true skill by agent deck:

| Agent deck | Archetype | 2M W–L | Win% | Timeout% | Avg turn |
|---|---|---|---|---|---|
| Izzet Spellementals | tempo | 5–3 | 62 | 12 | 18.6 |
| Azorius Momo | reanimator | 4–4 | 50 | 25 | 23.2 |
| Selesnya Ouroboroid | aggro | 4–4 | 50 | 0 | 19.0 |
| Izzet Prowess | tempo | 3–5 | 38 | 0 | 16.5 |
| Mono-Green Landfall | combo | 3–5 | 38 | 0 | 13.6 |
| Jeskai Lessons | control | 0–8 | 0 | 0 | 15.8 |
| Dimir Excruciator | control | 0–8 | 0 | 0 | 15.0 |
| 4c Control | control | 0–8 | 0 | 0 | 15.8 |

The headline 30% is a competent aggro/tempo pilot (Izzet Spellementals 62%)
averaged with a control pilot that wins **nothing** — the three reactive decks
go **0–24**. Matchup weighting, the lever this round expected to lift the
reactive decks off the ~0% floor by oversampling them, **did not**; they remain
pinned at zero.

**It dies, it no longer durdles.** Every one of the 24 control-deck losses is
`by life_total` (killed), clustering turns 12–17, against every proactive
opponent — not timeouts. The mid-run durdle worry is largely resolved in the
final policy (2M overall timeout 5%, eval timeouts 3/64). The residual failure
is not stalling: the single policy learned exactly one gameplan — deploy threats
and race — and has no defensive plan, so on a control deck it is killed around
turn 14 before any control plan comes online.

**Mulligans compound the collapse.** Cumulative per-deck, mulliganing craters
win rate for every deck (keep-hand 0.28–0.85 vs mulligan-hand 0.00–0.32), and
the three control decks mulligan the most (avg 2.2–2.3, i.e. down to ~5 cards)
and win **0%** when they do — the user's replayed observation, quantified. Read
as compounding, not root cause: control decks win only 0.28–0.33 even on kept
sevens cumulatively and 0% at 2M, so the archetype deficit is primary and the
over-mulligan makes a losing position hopeless. The mulligan behavior is most
likely a symptom — the policy mulligans control hands searching for the
proactive gameplan it knows, and finds none keepable.

**Verdict.** The reactive-deck collapse is confirmed as a **single-policy
piloting ceiling**, not an information or reward-shaping problem. Observation and
reward levers are exhausted for this failure. Round 7.98 must change the training
signal structurally rather than add features; see the plan below.

---

## Round 7.99 post-mortem and provenance closeout — July 22, 2026

`round-7.99-self-play-v1` completed cleanly at **2,007,040 actual
timesteps**. The checkpoint league delivered the intended mixed training
opponents, but it did not solve the archetype-specific piloting failure:
control remained 0–24 in the comparable fixed-suite slices, and the accumulated
deck results remained effectively zero for 4c Control and Jeskai Lessons
(0–160 each) and near-zero for Dimir Excruciator (2–158). Self-play is
therefore evidence against “more of the same policy” as the missing signal,
not evidence that the control problem is fixed.

The audit found three provenance defects in an otherwise clean run:

1. the best-so-far 1.7M evaluation archive was deleted after scoring, leaving
   only metadata and non-byte-identical later serializations;
2. the fixed evaluator scored the 2.0M cadence snapshot, while the published
   final model contained three later PPO updates at 2,007,040;
3. `training.log` was hashed before the final 219 bytes of completion output.

All three are now closed for future runs. Evaluation history schema v4 retains
exactly one content-addressed best-candidate archive independently of the
qualified `best_model.zip` gate. A terminal cadence snapshot is deferred until
after the last PPO update, the exact scored archive is moved into
`final_model.zip`, and evaluation/reload/final hashes plus timesteps must
agree. Environments now close and the final status line is flushed before the
runtime-log handler is detached; only then are terminal artifact hashes and
the manifest published.

The prerequisite archetype overhaul, Observation v6, and bounded FiLM are now
implemented as one lineage boundary. The versioned strategy-profile contract
separates macro plans from secondary plans, closed multi-label gameplan tags,
and quantized strategic axes; the eight pinned Standard decks carry reviewed
profiles; hydration/runtime loading preserves and validates them; stats and
the own-deck planner consume the centralized classifier. Observation v6
exposes only the active observer's exact profile as
`my_exact_deck_strategy_profile`; opponent input remains public inference only.
The dedicated 54-to-64 FiLM path bounds both scale and shift to `0.25` and is
excluded from generic feature concatenation. The implementation is complete;
the full delivery gate is now complete as well. No canary has yet been defined
or launched.

---

## Current execution plan

### Now — define and launch a fresh named v6 canary

Round 7.99 is complete; do **not** relaunch it. Observation v6 and the
archetype-conditioned capacity lever are implemented, and the full delivery
gate is green. No v6 canary, canary name, or launch command has yet been
created. The current task is step 5:

1. **Completed prerequisite:** preserve reviewed exact-own strategy profiles
   through the Standard corpus, hydration, runtime loader, analytics, and
   planner. Macro plan, secondary plan, tags, axes, confidence/evidence, and
   hashes come from one contract rather than three drifting classifiers.
2. **Implemented: Observation v6 hard boundary.**
   `my_exact_deck_strategy_profile` is a `float32` `(54,)` vector bounded to
   `0..1`, with literal component order plus taxonomy/classifier identities
   pinned by schema hash
   `6521db9c0c70c919a63c34e9c99463a3b801e25ae91149fd518a34054989e790`.
   It follows the acting observer and never substitutes the other seat's
   curated full-deck profile for public opponent inference.
3. **Implemented: condition the policy.** A dedicated 54-to-64 encoder drives
   FiLM scale and shift after shared-state projection. `tanh` bounds each
   modulation to `0.25`, the profile bypasses generic concatenation, and the
   extractor architecture is separately lineage-hashed and resume-validated.
   The v6 input plus conditioning path is one declared capacity lever.
4. **Completed: delivery gate before a canary.** The gate is green at 873/873
   discovered unit tests, 409/409 scenarios, 9/9 runtime smoke, 14/14 training
   smoke, and 8/8 default fuzz seeds x 1,000 valid actions (8,000 total), plus
   the controlled phase-boundary check. Resume and Harvest checkpoint loading
   also bind exact ZIP bytes to an allowed source-manifest artifact and reject
   policy/schema/data-lineage drift before use.
5. **Next: define and launch a fresh named canary from scratch.** No canary
   name or command has been created yet. Keep checkpoint self-play and every
   other 7.99 training setting fixed so conditioning is the only changed
   experimental lever. Promotion still requires the pair-aware 95%
   qualification lower bound to reach 55% with zero fidelity counters.

Mulligan work remains downstream. Revisit it only if control decks still
collapse after they can express a distinct plan; sharpen the existing
`mulligan_recommendation` signal rather than imposing a decision cap.

### Next — qualify and calibrate Standard

1. Pass an independent held-out paired-seat scripted qualification: the 95%
   lower confidence bound for decisive wins plus half non-timeout draws must
   reach the configured 55% threshold, with timeout life leads worth zero,
   zero fidelity counters, and exact checkpoint/lineage provenance. Do not
   reuse the training seed or the periodic-evaluation schedule as final
   qualification evidence. The current Harvest CLI enforces its 55% point
   score only, so its pass remains necessary but not sufficient until the
   protocol persists and enforces the same interval.
2. Freeze the qualified checkpoint as the baseline and promote it into the
   checkpoint league.
3. Run 3–5 known-matchup deck pairs at Harvest scale and compare simulated
   winrates with published or expert expectations.
4. Only after strength and calibration pass, run the first format-isolated,
   fidelity-clean Standard production harvest.

### Then — close the loop and expand formats

1. Implement the deck-builder consumer for `STATS_SCHEMA.md`, support status,
   uncertainty, matchup data, and format legality.
2. Automatically route builder candidates through legality, support preflight,
   paired-seat evaluation, and the appropriate format's Harvest queue.
3. Build a representative Modern corpus and repeat support triage,
   qualification, calibration, and production Harvest.
4. Repeat for Pioneer.

### Work that can proceed alongside training

- Fix newly observed fidelity failures immediately, scenario first.
- Continue the Standard support sweep in descending manifest impact order.
- Watch v3 identity-category coverage and observation degradation telemetry;
  treat any schema mismatch or hidden-information leak as a run-stopper.
- Profile production-sized training and Harvest workloads; land optimizations
  as separately verified changes.
- During the next canary, track post-warmup self-play share, rollout stragglers, evaluator
  duration, resident-policy RAM, system free memory, and effective steps/min.
  Keep permanent checkpoints at 500k and the bounded opponent pool at 100k
  unless a new measured failure justifies another named experiment.
- Measure cast transaction cost in the pinned canary before changing rollback:
  a logging-disabled 120-card microbenchmark measured a 15.21 ms median
  constructor-free checkpoint versus 34.30 ms for the canonical clone (12
  timed samples). Ordinary casts take one checkpoint and nonmana-cost preflight
  currently takes about two; optimize only if end-to-end profiling identifies
  this as a material training bottleneck.

---

## Active workstreams

### 1. Policy strength and opponent quality — ◐

Checkpoint/self-play policies already receive their own perspective-correct
observation and legal mask. The paired-seat qualification command fails closed
on illegal predictions, fidelity failures, and provenance mismatches. What
remains begins with defining and launching the fresh v6 canary now that the
delivery gate is complete; then train a policy that passes, freeze it, and
replace the scripted Harvest baseline with qualified policy-vs-policy or league
play. No v6 canary or command has yet been created.

### 2. Card fidelity and coverage — ◐

The support manifest records `crash`, `unparsed`, and `partial` clauses and
persists them beside statistics. Worst severity sticks per card. The builder
must exclude crash/unparsed cards and distrust or down-weight partial-card
statistics.

Standard's pinned 4,702-card schema-v2 ledger, measured July 21, contains:

| Evidence class | Cards |
| --- | ---: |
| Semantic verified | 0 |
| Semantic unverified | 4,702 |
| Legacy name-only claims (overlapping audit metadata) | 96 |
| Static observed clean | 119 |
| Static unseen/clean | 3,420 |
| Static partial | 833 |
| Static unparsed | 330 |

That is 75.3% static-clean and 0% semantically verified. The ledger's canonical
SHA-256 is
`4ed2dce764032d9cfdfa7e2f9721bc0514ac6bef0b514daaa15a58f1db5b7e14`.
A clean manifest for
the representative corpus does not prove unseen cards are faithful, and the 96
legacy claims confer no qualification.

Workflow: harvest → rank failures by real frequency/impact → write a failing
scenario → implement the smallest reusable parser or exact-card fix → verify
the evidence contract before ledger promotion. Untested subsystems remain
suspect even when static classification is clean.

### 3. Verification and replay — ✅ infrastructure, ◐ calibration

- Golden scenarios cover known regressions and policy contracts.
- Deterministic invariant fuzz checks zone/stack conservation, SBA fixed
  points, mask-valid execution, bounds, observation degradation, mask purity,
  finite rewards, mana clearing, and layer idempotence.
- Failures retain seed, actions, context, state, and a replay command; clean
  seeds leave no artifact.
- Test bug logs stay quarantined in `bugs/tests/` so they never masquerade as
  production run failures. Detection (`Playersim/debug.py`) now covers three
  layers: explicit `PLAYERSIM_TEST_MODE`/`PYTEST_CURRENT_TEST`, launcher argv
  (`-m unittest`, `*_test.py`, and IDE runners — VS Code `unittestadapter`/
  `visualstudio_py_testlauncher`, PyCharm `_jb_*_runner`), and an import-stack
  fallback for any unenumerated launcher whose test module is live when
  `debug.py` first loads. IDE-launched tests previously leaked to top-level
  `bugs/` because their launcher argv matched none of the old patterns.
- Successful fixed-evaluation games now retain evaluation-only, both-actor
  action/state traces in atomic gzip sidecars. The dependency-free Stats
  Workbench joins every case to its game-log provenance and loads one selected
  trace on demand; historical evaluations are explicitly summary-only.
- The remaining acceptance gap is matchup calibration, not test
  infrastructure.

### 4. Throughput — ◐

Reference-machine baseline: Ryzen 5 5600, 32 GB RAM, RTX 5060. Before async
evaluation, effective training was 10.6 steps/s while pure rollout reached
roughly 40–55 steps/s; synchronous evaluation consumed about 73% of wall time.
Round 7.76 moved evaluation to a dedicated process and widened the network to
use the otherwise idle GPU.

Measured hot paths remain environment-side: observation construction, repeated
436-field card feature generation, text parsing/string normalization, and
repeated 480-action mask generation.

Safe optimization order:

1. Cache immutable per-card feature slices; invalidate mutable P/T, counter,
   type, controller, and zone-dependent data explicitly.
2. Cache planner analysis only by **state version plus observing player**.
   Turn-only caching is forbidden because it caused the 7.82 same-turn and
   cross-seat correctness failures.
3. Cache action masks only against an action-relevant state/choice version.
4. Require bit-identical observations and masks under scenarios and fuzz, then
   re-profile. These optimizations must not change schema or semantics.

### 5. Harvest and deck-builder integration — ◐

Parallel deterministic shards, checkpoint identity, aggregate success-only
manifests, candidate scoring, paired-seat qualification, and promotion gates
are implemented. Remaining: production-scale throughput measurement, a
qualified checkpoint, calibration, builder-side exclusion/confidence logic,
and automatic candidate feedback. The unified Stats Workbench now exposes run
lineage, evaluations, isolated DeckStats scopes, fidelity/support evidence,
and Harvest/promotion manifests, but it is an operator/debugger rather than the
missing automated builder consumer.

### 6. Format program — ◐

- ✅ Shared foundation: canonical append-only registry, frozen self-hashed
  feature schema, explicit format/deck configuration, legality checks, and
  lineage-stamped manifests.
- ◐ Standard: corpus, namespace, reviewed profiles, Observation v6, and
  bounded archetype conditioning exist and the v6 delivery gate is green;
  fresh-canary launch, qualification, calibration, and production Harvest remain.
- ▢ Modern: no representative training corpus yet.
- ▢ Pioneer: no representative training corpus yet.
- ▢ Unified builder: format-aware feedback queue not connected.

---

## Known limitations that still matter

The current fix batch has no known unresolved Priority 0 simulation-integrity
defect. Priority 1 agent choice exposure
is substantially complete, but combat still has bounded rules
families rather than complete coverage. The following behaviors fail closed or
remain fidelity-marked rather than being treated as fully supported.

| Area | Current boundary |
| --- | --- |
| Combat assignment | Multi-blocker damage uses an ordered automatic approximation and cannot express every legal split. |
| Combat requirements | Common evasion and restrictions work; lure effects, attack/block costs, and unusual must-attack/must-block combinations remain partial. |
| Combat objects | Planeswalker targeting is covered; battle protector/controller behavior remains incomplete. |
| Combat cleanup | Exert is recognized for attacking but does not yet enforce skipping the creature's next untap. |
| Damage modification | Core prevention, protection, indestructible, deathtouch, lifelink, and trample paths work; rare replacement/prevention wording remains parser-dependent. |
| `as enters` | Common creature type, color, card/basic-land type, opponent, counters, life-payment, and deferred-ETB choices work; arbitrary consumers remain card-specific. |
| Emblems | Kaito anthem and Wrenn graveyard permission are implemented; other emblem text is retained but not executed generically. |
| Double-faced/adventure cards | Back-face casting works; direct nonland back-face entry and complete back-face/adventure targeting remain incomplete or heuristic. |
| Generic mechanics | Discover, Explore, Investigate, Endure, Connive, Suspect, Airbend, Equip, Crew, Prepare, Warp, and Earthbend intentionally support bounded text/count families. |
| Trigger/condition parsing | Reflexive triggers, rare phase scopes, structured sacrifice, activation costs, target-conditioned pricing, and unusual attack adjectives use bounded vocabularies. Unknown templates fail closed. |
| Search/permissions | Uncommon linked optional searches and source-duration Warp permissions need exact transactions. |
| Dice | Ordinary result tables and roll events work; modifiers, rerolls, ignored rolls, and tableless result clauses do not. |
| Mutate/Meld/Specialize | Mutate lacks per-component replacement and library-order choices. Meld/Specialize require complete local family data and fail closed when absent. |
| Exact-card fallbacks | Screaming Nemesis, Anoint, Obliterator, Cavern of Souls, and Obstinate Baloth have documented conservative boundaries. |
| Hidden exile | Current runtime IDs are unique and hidden correctly. A legacy state sharing one ID between visible and hidden occurrences hides both. |
| Action tensor | Fixed at 480 actions. Action 479 pages overflow contexts through normal handlers; dormant indices 205–223 remain mask-invalid until their mechanics exist. |
| Format data | Registry/schema are lineage-bound and Bo1. Sideboards are retained and legality-checked but not played. Registry growth changes registry lineage; observation-field or identity-capacity changes require a new policy lineage. |

---

## Definition of done

The project is complete only when all of these are true:

1. Every delivery keeps the required scenario, smoke, training, invariant,
   replay, Harvest, registry, ingestion, and long-fuzz gates green.
2. There are no known stats-corrupting defects, and every fixed defect has a
   permanent guard.
3. Every card admitted to a format's builder pool is evidence-qualified or
   explicitly excluded/down-weighted according to its support status.
4. Every value-relevant player decision is exposed to the policy, including
   arbitrary legal multi-blocker damage distributions.
5. A trained policy passes independent held-out paired-seat qualification at
   the configured 95% lower-bound threshold, and statistics come from
   qualified policy/league play rather than the random or scripted baseline.
6. Known-matchup calibration passes within a documented tolerance.
7. The format-aware builder consumes version-matched statistics and support
   evidence, proposes legal candidates, and feeds them back into evaluation
   and Harvest without manual file editing.

---

## Checkpoint and schema boundaries

Historical boundaries are retained here so old artifacts cannot be resumed by
mistake. Round 7.99 is complete and remains an immutable Observation v5 result.
Observation v6 and bounded FiLM are implemented and their delivery gate has
passed. The practical rule remains: **do not relaunch or resume 7.99; define
and launch the next policy fresh.** No v6 canary, name, or command exists yet.
Every prior checkpoint and named canary is incompatible with schema hash
`6521db9c0c70c919a63c34e9c99463a3b801e25ae91149fd518a34054989e790` and the
separately pinned extractor architecture. Keep `tempo-graded-potential-v1`,
`combat-v7`, and the checkpoint-league contract fixed so strategy conditioning
is the single declared experimental lever.

| Minimum round | Incompatible change |
| --- | --- |
| 7.37 | Reward rebuild and playable scripted baseline |
| 7.44 | Declared observation-space change |
| 7.62 | X/count observation bounds |
| 7.72 | Discounted state-potential reward and critic baseline |
| 7.73 | Timeout terminal contract and symlog extractor inputs |
| 7.76 | Network width and async evaluation lineage |
| 7.80 | Offense-weighted reward v3 and live combat-damage observation |
| 7.82 | Exhaustive extractor routing and repaired observation semantics |
| 7.83 | Frozen Observation v2, categorical card identity, public-state expansion, v1 compaction, and deterministic strategy-memory boundary |
| 7.84 | Deterministic planner observations, hidden-information-safe inference, and removal of fake/dead planner inputs |
| 7.85 | First-v2-run fidelity boundary: isolated combat simulation, live participant filtering, Room mask/payment parity, and Earthbend target commitment |
| 7.86 | Mandatory public combat damage, split first-strike steps, preserved blocked status, canonical all-target damage/lifelink, overflow combat actions, and flat-timeout reward v4 |
| 7.87 | Correct opponent-terminal perspective, reward v5, fixed outcome evaluation, deterministic combat curriculum, and PPO canary defaults |
| 7.88 | Mastery-gated curriculum, asynchronous evaluation lifecycle, qualification-based promotion, and batched training statistics |
| 7.89 | Observation v3 semantic corrections, profile-specific mastery gates, bounded full-pool entry, balanced evaluation exposure, and the renewed combat audit |
| 7.90 | Analytics memory-redundancy refactor, validated behavior-neutral against the 7.89 canary trajectory |
| 7.91 | Annealed opponent handicap (`combat-v4`), stage turn limits, graded turn-limit reward v6, doubled damage-progress potential, and the paired-seat observation audit |
| 7.92 | `combat-v5` (goldfish 25 turns, full-pool handicap ramp), mixed-profile-safe terminal ratchet windows, strict recovery fidelity counters, pinned canary seeds/configuration, and pair-aware lower-bound qualification |
| 7.93 | `combat-v6`: two-way 95% Wilson-interval handicap ratchet with 48-episode windows |
| 7.94 | `tempo-graded-potential-v1` reward contract: graded win speed and stall penalties, negative draws, per-step time cost, offense-dominant potential |
| 7.95 | `combat-v7` halved scripted ratchet step (0.10 rungs) and the 2M-timestep canary horizon |
| 7.96 | Observation v4: the observer's own decklist (`my_deck_card_identity`) and remaining-library composition (`my_library_composition`), full-deck `deck_composition_estimate`; reward/curriculum carry over from 7.95 |
| 7.97 | Observation v5: producible mana by color (`my_producible_mana`, `opp_producible_mana`); reward/curriculum carry over from 7.96. Opt-in `--matchup-weighting` and cast-path legality hardening ride along (neither breaks lineage) |
| 7.98 | Resource-bounded checkpoint self-play: explicit opponent-private observation/mask boundary, one frozen CPU policy per worker, four-snapshot FIFO disk pool, reset-scoped deterministic leases, fail-closed staging/rollback provenance, and scripted-only fixed evaluation. Observation v5, reward, and curriculum remain unchanged |
| 7.99 | Fresh repair boundary after 7.98 failed at 705,456: copied Colorstorm and Deceit executable Oracle templates are name-independent; permanent recovery checkpoints move from 50k to 500k while the four-snapshot self-play pool remains at 100k. Observation v5, reward, curriculum, evaluation, seeds, and opponent mix remain unchanged |
| Post-7.99 next-policy boundary | Observation v6 `my_exact_deck_strategy_profile` (`float32`, `(54,)`, `0..1`, observer-own only), taxonomy/classifier-pinned schema, and dedicated bounded FiLM with separately hashed extractor architecture. Incompatible with every prior checkpoint; delivery gate complete, fresh canary not yet defined |

The canonical registry is append-only within the fixed identity capacity;
appends change registry lineage without changing observation width. Changing
feature vocabulary, observation fields, bounds, visibility semantics,
identity capacity, or extractor routing changes a schema hash and starts a new
policy lineage. Run manifests—not individual game-log rows—carry format, pool,
corpus, registry, both schema identities, policy, and checkpoint provenance.

---

## Delivered history — compact record

- **Rounds 1–7.22:** built the parser/effect foundation and corrected the
  earliest rules, targeting, token, layer, replacement, and statistics
  failures.
- **7.23–7.36:** completed the policy boundary, choice audits, replay and fuzz
  harnesses, parallel Harvest/promotion protocol, and reproducible CUDA worker
  pipeline.
- **7.37–7.45:** rebuilt reward/telemetry and the scripted opponent, repaired
  game-ending paths, changed the observation space, and implemented Spree as a
  real casting transaction.
- **7.46–7.50:** introduced format namespaces, canonical registry, frozen
  feature schema, full Standard snapshot, support preflight, and self-hashed
  lineage manifests.
- **7.51–7.62:** made the representative corpus clean, expanded reusable
  mechanics, established true per-copy runtime identity, and closed the known
  Priority 0/1 integrity and choice lists.
- **7.63–7.70:** hardened mana/choice/worker integrity, made coverage evidence
  honest, launched paired-seat qualification, and converted live training
  failures into guarded fixes.
- **7.71–7.79:** repaired choice paging and lifecycle traps, rebuilt the
  learning signal and critic input contract, moved evaluation asynchronous,
  widened the network, and aligned mask/selection/resolution targeting.
- **7.80–7.82:** introduced offense-weighted reward v3, audited observation
  liveness and semantics, revived combat search, completed extractor coverage,
  removed unstable IDs from learned inputs, repaired exact target/stack
  summaries and perspective freshness, and fixed the blocker lookup exposed by
  the real search path.
- **7.83:** froze Observation v2 with stable categorical card identity, filled
  missing public zones/resources/counters/attachments/combat/stack state,
  compacted dead v1 duplicates, removed online strategy memory from policy
  input, made the optional memory deterministic and disabled by default,
  documented and hashed the full contract, and made its identity part of every
  lineage manifest.
- **7.84:** completed the planner-observation audit: removed RNG consumption,
  excluded face-down identities from opponent inference, removed fake exact
  hand/action inputs, compacted live strategy metrics, and pinned the final v2
  hash with purity regressions.
- **7.85:** invalidated the first v2 training attempt after strict fidelity
  caught a Room unlock mismatch; made combat lookahead state-pure and resilient
  to stale/star-P/T participants, unified Room mask/payment execution, and
  restored mandatory Earthbend targeting without changing the v2 schema hash.
- **7.86:** invalidated the interrupted `round-7.85-reward-v8` lineage after
  proving public passes skipped combat damage; made damage mandatory and
  fail-closed, separated and snapshotted damage steps, preserved combat state,
  unified source events and lifelink, opened overflow combat choices, and
  removed the timeout life-lead incentive with reward contract v4.
- **7.87–7.90:** corrected terminal perspective with reward v5, introduced
  deterministic mastery gates and asynchronous fixed evaluation, moved to
  Observation v3 with semantic/profile-floor corrections, and validated the
  behavior-neutral analytics-memory refactor.
- **7.91:** introduced annealed active opponents, graded reward v6, explicit
  stage turn limits, and a scoped paired-seat observation audit; three runs
  demonstrated faster curriculum climbing but a concentrated matchup plateau.
- **7.92:** introduced `combat-v5`, repaired the full-pool qualifying-outcome
  ratchet, expanded strict recovery fidelity telemetry, pinned independent
  train/evaluation seeds in a named canary contract, and changed periodic
  checkpoint qualification from a point estimate to a pair-aware 95%
  lower-bound gate. The delivery suite is green at 430 unit tests, 404
  scenarios, 9/9 runtime smoke, 13/13 training smoke, and 8/8 default fuzz
  seeds plus the phase-boundary check. The held-out Harvest protocol still needs the same
  interval enforcement before its point-score pass is sufficient evidence.
- **7.93:** diagnosed the round-7.92 flatline (evaluation peaked at 0.281 at
  200k, then decayed once every stage fell to its deadline and 24-episode
  raw-rate windows ratcheted scripted play to full strength on noise-level
  evidence — 6/24 wins, exactly the stage floor — while the training win
  rate collapsed to ~8% with no path back). The handicap ratchet now gates
  both directions on the window's 95% Wilson interval and is reversible: a
  window confidently below target hands one rung back, never past the
  stage's configured start, including at epsilon zero. `combat-v6` widens
  the ratchet windows to 48 episodes so the interval can resolve either way,
  and the `round-7.93` canary pins the otherwise-unchanged 7.92 contract
  over it. The Three Steps Ahead modal continuation failure that ended the
  run was fixed separately. A low-play-rate card audit of ten suspects
  through the production mask/cast/trigger paths cleared eight as fully
  functional and fixed two real gaps: `_targets_available` now applies CR
  601.2b to modal spells through the shared `modal_mode_is_selectable`
  predicate (Bushwhack was uncastable whenever its fight mode lacked
  targets), and blight is now a supported optional additional casting cost
  with its paid/unpaid record gating "if this spell's additional cost was
  paid" riders at resolution (Requiting Hex previously skipped the cost and
  granted its life unconditionally). Three golden scenarios pin the fixes.
- **Post-7.93 low-play follow-up:** audited the eighteen reported cards against
  their opportunity-adjusted usage, production action masks, parsing, event
  routing, and resolution paths. The audit repaired modal Charm selection,
  stack-target and controller scoping for Surrak, nested intervening-if parsing
  for Earthbender Ascension, Crew-value/self-trigger recovery for Lumbering
  Worldwagon, Warp access beyond the first eight hand slots, optional and
  once-per-turn reflected damage for Jennifer Walters, layered/LKI source power
  for Ouroboroid, fail-to-find library search for Starfield Shepherd, and a
  false Badgermole replacement registration caused by reminder text. Exact
  per-game drawn/opening-hand counts are now retained in individual card stats,
  so the viewer can distinguish a card that was never seen from one declined
  despite being available. Icetill Explorer, Seam Rip, Spider Manifestation,
  Sage of the Skies, Deadly Cover-Up, Iroh's Demonstration, M.O.D.O.K., and Day
  of Judgment were cleared through their existing production paths. North Wind
  Avatar's wish resolution is functional when an outside-the-game pool is
  supplied; current deck data does not define such a pool, so no targets are
  invented. The delivery gate is green at 471 discovered unit tests and 408
  scenarios, plus all eight default invariant-fuzz seeds and the controlled
  phase-boundary check.
- **7.94:** overhauled the reward system after the stopped
  `round-7.93-cardfix-v1` run confirmed the pattern across 7.88-7.93: with
  flat +-10 terminals the policy converged on timeout-dominant play (82%
  goldfish / 76% race timeouts with zero decisive losses — it never lost,
  it just never finished). `tempo-graded-potential-v1` replaces the
  discounted-state-potential family: decisive wins earn a bounded speed
  premium (+10 up to +14 by unused engine-turn budget, stationary across
  stage turn limits), turn-limit stalls grade continuously on opponent
  damage (-10 untouched up to -7 near lethal, result labels ignored so
  lifegain cannot cushion the penalty, always below a real draw), draws pay
  -3 instead of the near-neutral -0.25, every step costs 0.005 reward so
  stalling bleeds and conceding hopeless games recycles samples, and the
  potential drops the hand-hoarding term while making the convex damage
  ramp dominant (life 0.10, board 0.15, damage 1.0 with slope 0.35x-1.65x).
  Shaping remains strictly potential-based and terminal-zeroed. The
  `round-7.94` canary pins the new contract over the unchanged combat-v6
  inputs and is validated against the live training config; the v6-era
  canaries and resume paths fail closed across the contract boundary.
- **7.95:** halved the scripted handicap ratchet step after run
  `round-7.94-tempo-v1` (1M steps, completed clean) validated the two-way
  interval ratchet but exposed its granularity: eval qualification rose
  0.047 -> 0.250 (best final of any run, still far under the 0.550 publish
  gate) while the back half of the run ping-ponged the scripted epsilon
  0.40 <-> 0.20 three times — ~38% decisive wins at 0.40 tightens, ~12% at
  0.20 relaxes, so the whole skill cliff sat inside one 0.20 rung.
  `combat-v7` sets the bridge and full_pool scripted steps to 0.10 (rungs
  at 0.30 and 0.10); the novice ramp keeps 0.25 — race never oscillated.
  Because each finer rung needs a fresh 48-episode window, the `round-7.95`
  canary doubles the horizon to 2M timesteps; every other input carries
  over from round 7.94 unchanged. Rewards are deliberately untouched this
  round: tempo-graded-potential-v1 has exactly one clean 1M-step run of
  evidence and the eval timeout tail (10-15%, scored as losses by the
  qualification gate) is its follow-up target if the finer ladder also
  plateaus. Two fixes from the round-7.94 stats audit ride along before
  launch. First, the hand "playable" observation flag computed
  affordability from floating mana only while the action mask used the
  land-aware check, so effectively every spell observed as unplayable;
  the environment now delegates to the action handler's affordability
  check and scenario 601.2f guards mask/observation agreement (the
  audit's affordability probe cleared the engine itself: masks, negative
  cases, conditional Verge activation, and tap payments were all
  correct). Second, record_game never received game_state, so every
  deck/card ahead/behind bucket sat at the tracker's parity default (0
  of 960 card files after 7.94); the environment now snapshots life
  totals per turn and classifies the winner's mid-game position (ahead,
  parity, or behind at the middle turn, 5-life margin), making
  snowball-versus-comeback analytics real.
- **7.96–7.97:** completed the Observation v4/v5 and training-contract
  transitions, repaired cast-legality and matchup-weighting boundaries, and
  used the qualification/postmortem evidence to reject another parameter-only
  retry: the policy still had no publishable strength result and required a
  structural opponent-distribution change.
- **7.98–7.99:** activated the checkpoint self-play league. Round 7.98 stopped
  strictly at 705,456 steps when Superior Spider-Man's copied Colorstorm Opus
  text exposed a printed-name parser gate. The repair made the complete
  Colorstorm and Deceit executable templates name-independent, added exact
  runtime regressions and a source-hashed three-card probe, and defined 7.99 as
  a fresh lineage with permanent recovery checkpoints every 500k timesteps
  while preserving the self-play pool's independent 100k publication cadence.

### Institutional lessons retained from the silent-bug catalog

- An untested subsystem is assumed suspect; first-touch scenarios repeatedly
  found phantom methods, swallowed exceptions, and code paths that never ran.
- Mask, displayed choice, execution, and resolution must share one legality
  contract. Drift between them caused no-ops, target fizzles, and deterministic
  paging loops.
- Card identity, controller, and player perspective must be explicit at every
  boundary. Earlier ambiguity corrupted both gameplay and statistics.
- Observation shape/bounds are insufficient: every field needs liveness,
  semantics, perspective, extractor-routing, and degradation tests.
- Broad parsers must fail closed. A visible partial/unparsed ledger entry is
  safer than silently claiming support.

---

## Working agreements

- Write the failing scenario before implementing a defect fix.
- A card is never "clean" or "fully implemented" on parse evidence alone.
  The support ledger's `observed_clean` audits parsing/registration, and
  cards with that status have repeatedly produced runtime bugs (modal
  continuation, cost choices, trigger wiring — the Three Steps Ahead
  failure killed a 850k-step run). A card qualifies as fully implemented
  only when each of its distinct abilities, modes, and choice paths has
  been driven through the production pipeline — mask exposure including
  at least one negative case, cast/activation with real payment,
  resolution, triggers via `process_triggered_abilities`, and any
  choice/target flows — with post-resolution state asserted (zones,
  counters, life, tapped state), not merely the absence of
  `execution_failed`. When such a probe exercises a mechanic with no
  existing scenario coverage, promote it to a permanent scenario even if
  it found nothing.
- Treat every probe result, including `execution_passed`, as bounded mechanical
  evidence; semantic status remains unverified until exact-state scenarios cover
  every distinct rules path.
- Never copy a historical card name into `verified`. Candidate evidence
  requires fresh Oracle/surface hashes, exact coverage equality, and passing
  assertion-bearing test nodes, but promotion remains hard-locked until the
  test-to-surface mapping is machine-verifiable. `legacy_verified_claims` is
  audit metadata only.
- Treat any warning, degraded observation, swallowed exception, or fidelity
  counter as a correctness failure until classified.
- Keep masks and handlers on shared legality predicates and pin paged choices
  through execution.
- Run scenario, smoke, training smoke, and default invariant fuzz before a
  delivery; run focused suites for the changed subsystem and long fuzz on the
  scheduled/manual gate.
- Update the roadmap only with current decisions, measurable exits, new schema
  boundaries, or durable limitations; keep per-run debugging in logs.
- Never mix statistics or checkpoints across incompatible lineage hashes.
- Keep performance-only work semantically bit-identical and verify it before
  trusting benchmark improvements.
