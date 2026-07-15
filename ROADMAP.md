# Playersim roadmap — current as of July 15, 2026

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
Harvest orchestration are operational. The representative Standard corpus has
no known severe fidelity entry. The project is **not production-ready** because
no trained checkpoint has passed the paired-seat strength gate, matchup
calibration has not run, format-wide card support remains incomplete, and the
deck-builder feedback loop is not connected.

### Round 7.89 / Observation v3 pre-run boundary

The corrected v3 observation contract, active-gated curriculum, balanced
evaluation schedule, and combat audit are green:

| Gate | Result |
| --- | --- |
| Golden scenarios | 403/403 |
| Runtime smoke | 9/9 |
| Training smoke | 13/13 |
| Discovered unit tests | 243/243 |
| Focused combat regressions | 17/17 |
| Strategic numeric regressions | 11/11 |
| Default invariant fuzz | 8/8 seeds × 1,000 valid actions, plus phase-boundary check |
| Failed-run replay | exact 117-action trace reaches and executes Room action 250 cleanly |
| Diff/whitespace check | clean |
| Observation schema | v3 / `401f929f7e9cb21bceb2ba328a67f8f165f51c1eafe83afcdb73fd5c0561bb95` |

Standing broader gates last recorded green: 108/108 focused regressions,
fixture Harvest 16/16, production Harvest protocol 17/17, card registry 19/19,
deck ingestion 13/13, fuzz/replay configuration 6/6, and strict long fuzz 32
seeds × 10,000 valid actions.

### Non-negotiable lineage rules

- **Start every new policy from Round 7.89.** Observation v3 corrects learned
  mana, land-development, resource-advantage, and strategic-viability
  semantics; do not resume an Observation v2 checkpoint into this lineage.
- Resume now verifies the companion manifest's reward contract and Observation
  version/hash. Curriculum continuation is intentionally rejected until its
  per-worker scheduler counters can be checkpointed; launch Round 7.89 fresh.
- Do not mix pre-7.46 statistics with format-namespace statistics.
- Statistics collected before July 2026 are unusable. They were affected by
  perspective, winner attribution, fabricated play-turn, first-strike, layer,
  and replacement-system defects and must be re-harvested.
- Any future policy-observation change creates another explicit schema and
  checkpoint boundary.

---

## Latest run finding — `round-7.87-curriculum-v5` is diagnostic-only; Round 7.88 repaired

`ALPHA_ZERO_MTG_V3.00_20260715_002725_round-7.87-curriculum-v5` was manually
interrupted at 107,200 steps. It was fidelity-clean, but the fixed curriculum
advanced much faster than demonstrated ability: goldfish finished 33 decisive
wins, no losses, and 49 timeouts; race then fell to 13 wins and 265 losses;
bridge recorded 5 wins and 169 losses. The 25k fixed evaluation consumed about
937 seconds and returned 1 win, 50 losses, and 13 timeouts, yet that first weak
checkpoint was still published as `best_model`. The 100k boundary was skipped
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
  `best_model.zip` is published; best-so-far candidates remain observable.
- Evaluation game logs identify the exact checkpoint timestep and SHA-256.
  Backlog skips and interruption cancellations are durable, pending snapshots
  are cleaned up, and user interruption has its own manifest status and
  `interrupted_model` artifact.
- Every run has `logs/<run>/training.log`. Training statistics batch compressed
  aggregate writes for ten games and flush on shutdown. Critic scale telemetry
  now warns on repeated value/reward divergence.

Round 7.89 preserves `combat-v2` for reproducibility and makes `combat-v3` the
fresh-run default. Its mastery window retains opponent profile, race requires a
novice floor, and bridge requires separate novice and scripted floors. Stage
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
- Periodic evaluation uses one immutable 64-case paired deck/seat/seed suite.
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
`401f929f7e9cb21bceb2ba328a67f8f165f51c1eafe83afcdb73fd5c0561bb95`.
Changing a field, bound, identity capacity, visibility rule, or extractor route
starts a new schema/checkpoint lineage. The next training run must record actual
v3 throughput and memory alongside behavior telemetry.

---

## Current execution plan

### Now — train the Round 7.89 active-gated canary

1. Freeze the Round 7.89 source and launch a fresh Standard candidate with
   reward v5, `combat-v3`, eight workers, 100k evaluation cadence, and the
   balanced fixed 64-case suite. Do not resume the Round 7.88 checkpoint.
2. Confirm `race` cannot master without its novice floor and `bridge` cannot
   master without both novice and scripted floors. A deadline transition must
   be reported as `deadline`, never as mastery.
3. Confirm stage-duration ceilings place the run in `full_pool` by roughly
   375k timesteps, leaving most of the one-million-step run for all eight decks.
4. At each 100k boundary, compare checkpoints only through `evaluations.json`.
   Require rising decisive wins, balanced deck exposure, zero reward-sign and
   fidelity failures, controlled policy KL, and a stable critic.
5. Keep Observation v3, the curriculum thresholds, evaluation cases, and
   PPO settings unchanged within this canary so the result is attributable.

### Next — qualify and calibrate Standard

1. Pass paired-seat scripted qualification: at least the configured 55%
   qualification score, timeout life leads worth zero, zero fidelity counters, and
   exact checkpoint/lineage provenance.
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

---

## Active workstreams

### 1. Policy strength and opponent quality — ◐

Checkpoint/self-play policies already receive their own perspective-correct
observation and legal mask. The paired-seat qualification command fails closed
on illegal predictions, fidelity failures, and provenance mismatches. What
remains is operational: train a policy that passes, freeze it, then replace the
scripted Harvest baseline with qualified policy-vs-policy or league play.

### 2. Card fidelity and coverage — ◐

The support manifest records `crash`, `unparsed`, and `partial` clauses and
persists them beside statistics. Worst severity sticks per card. The builder
must exclude crash/unparsed cards and distrust or down-weight partial-card
statistics.

Standard's pinned 4,702-card ledger, last measured July 12, contains:

| Evidence class | Cards |
| --- | ---: |
| Verified | 86 |
| Observed clean | 73 |
| Unseen/static clean | 3,327 |
| Partial | 788 |
| Unparsed | 428 |

That is 74.1% static-clean but only 3.4% evidence-qualified. A clean manifest
for the representative corpus does not prove unseen cards are faithful.

Workflow: harvest → rank failures by real frequency/impact → write a failing
scenario → implement the smallest reusable parser or exact-card fix → verify
the ledger promotion. Untested subsystems remain suspect even when static
classification is clean.

### 3. Verification and replay — ✅ infrastructure, ◐ calibration

- Golden scenarios cover known regressions and policy contracts.
- Deterministic invariant fuzz checks zone/stack conservation, SBA fixed
  points, mask-valid execution, bounds, observation degradation, mask purity,
  finite rewards, mana clearing, and layer idempotence.
- Failures retain seed, actions, context, state, and a replay command; clean
  seeds leave no artifact.
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
and automatic candidate feedback.

### 6. Format program — ◐

- ✅ Shared foundation: canonical append-only registry, frozen self-hashed
  feature schema, explicit format/deck configuration, legality checks, and
  lineage-stamped manifests.
- ◐ Standard: corpus, namespace, and Observation v3 exist; qualification,
  calibration, and production Harvest remain.
- ▢ Modern: no representative training corpus yet.
- ▢ Pioneer: no representative training corpus yet.
- ▢ Unified builder: format-aware feedback queue not connected.

---

## Known limitations that still matter

Priority 0 simulation integrity has no known open defect. Priority 1 agent
choice exposure is substantially complete, but combat still has bounded rules
families rathx er than complete coverage. The following behaviors fail closed or
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
5. A trained policy passes paired-seat qualification and statistics come from
   qualified policy/league play rather than the random or scripted baseline.
6. Known-matchup calibration passes within a documented tolerance.
7. The format-aware builder consumes version-matched statistics and support
   evidence, proposes legal candidates, and feeds them back into evaluation
   and Harvest without manual file editing.

---

## Checkpoint and schema boundaries

Historical boundaries are retained here so old artifacts cannot be resumed by
mistake. The practical rule is: **start this policy fresh from Round 7.89.**

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
