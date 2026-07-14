# Playersim roadmap — current as of July 14, 2026

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
calibration has not run, Observation v2 is not frozen, format-wide card support
remains incomplete, and the deck-builder feedback loop is not connected.

### Verified Round 7.82 baseline

The observation audit and its follow-up combat fix are green:

| Gate | Result |
| --- | --- |
| Golden scenarios | 376/376 |
| Runtime smoke | 9/9 |
| Training smoke | 13/13 |
| Default invariant fuzz | 8/8 seeds × 1,000 valid actions, plus phase-boundary check |
| Diff/whitespace check | clean |

Standing broader gates last recorded green: 108/108 focused regressions,
201/201 discovered unit tests, fixture Harvest 16/16, production Harvest
protocol 16/16, card registry 19/19, deck ingestion 13/13, fuzz/replay
configuration 6/6, and strict long fuzz 32 seeds × 10,000 valid actions.

### Non-negotiable lineage rules

- **Start every new policy from Round 7.82 or later.** The extractor and
  observation semantics changed; do not resume any earlier checkpoint.
- Do not mix pre-7.46 statistics with format-namespace statistics.
- Statistics collected before July 2026 are unusable. They were affected by
  perspective, winner attribution, fabricated play-turn, first-strike, layer,
  and replacement-system defects and must be re-harvested.
- Any future Observation v2 change creates another explicit schema and
  checkpoint boundary.

---

## Latest finding — observation contract

### What the 7.80–7.82 audit fixed

- `potential_combat_damage` now reports canonical attack-capable power rather
  than a constant zero.
- Previously dead target summaries and phase history now carry live signal.
- Combat simulation no longer silently collapses to all-zero evaluations.
- The rank-3 `ability_recommendations` grid is explicitly extracted; training
  smoke now requires coverage of every policy-consumed observation key.
- `phase` uses its dedicated embedding, the action mask remains an external
  MaskablePPO input, and unstable runtime target occurrence IDs are protocol
  metadata rather than learned features.
- Planner analysis refreshes at each observation boundary, preventing stale
  same-turn values and cross-seat perspective reuse.
- Target summaries preserve the actual flattened battlefield, graveyard,
  player, and stack indices. Stack summaries use the real top five objects.
- Combat-power and optimal-attacker fields use the canonical legality and
  combination-search paths instead of local approximations.
- Exercising the real attack search exposed a blocker-controller lookup on a
  nonexistent resolver helper; it now queries game state and has a real-blocker
  liveness scenario.

**Current verdict:** no additional high-priority correctness defect is known
inside the existing observation contract, and its verification gates are
green. That does not mean the representation is complete.

### Observation v2 — deliberate schema project

Observation v2 should be implemented as one versioned project, not as unrelated
field additions during a live training run.

1. ▢ **Stable semantic card identity.** Give the policy a frozen registry or
   Oracle-level identity signal. Current structural features can alias cards
   that have different rules text but similar visible characteristics. Never
   use per-game runtime occurrence IDs as semantic identity.
2. ▢ **Missing public state.** Represent library sizes, poison counters,
   permanent counters and attachments, combat assignments and attack targets,
   richer stack objects, and opponent floating mana where public.
3. ▢ **Compaction after evidence.** Measure and then consolidate absolute vs.
   seat-relative duplicates, the dead `hand_performance` proxy, duplicated
   tapped/keyword information, and derived features that add no policy value.
   Do not remove fields without ablation results.
4. ▢ **Schema migration.** Document every key's meaning, perspective, bounds,
   saturation behavior, and extractor route; version and hash the new schema;
   update checkpoint compatibility checks and lineage manifests.
5. ▢ **Acceptance gates.** Preserve hidden-information invariance, exact
   extractor coverage, finite/bounded values, liveness under seeded play,
   perspective correctness, and scenario/default-fuzz parity. Record memory
   and steps-per-second changes before adopting the schema.

Observation v2 is required before the final production policy lineage, but it
does not block a Round 7.82 diagnostic run whose purpose is to evaluate the
new offense-weighted reward and current behavior.

---

## Current execution plan

### Now — validate the Round 7.82 policy

1. Launch a fresh Standard candidate under
   `discounted-state-potential-v3` with `--n-envs 8`, `--eval-freq 25000`,
   and `--eval-episodes 10`.
2. Read the run at roughly 300k steps. The required direction is a rising
   `terminal/life_total_rate`, a falling `terminal/turn_limit_rate`, improving
   episode reward, a stable critic, and zero fidelity/provenance failures.
3. If the policy still stalls, investigate the scripted opponent's passivity
   before changing the reward again. Earlier runs reached about 0.93 critic
   explained variance while roughly 88% of episodes timed out; the model had
   learned the stalled objective rather than failed to learn it.
4. Keep schema changes and throughput code changes out of the live experiment
   so its result remains attributable.

### Next — freeze Observation v2, then qualify Standard

1. Use the 7.82 diagnostic as the behavioral baseline, then implement and
   freeze Observation v2 with a new schema hash and checkpoint boundary.
2. Train a fresh Standard candidate on the frozen v2 schema.
3. Pass paired-seat scripted qualification: at least the configured 55% score,
   zero fidelity counters, and exact checkpoint/lineage provenance.
4. Freeze the qualified checkpoint as the baseline and promote it into the
   checkpoint league.
5. Run 3–5 known-matchup deck pairs at Harvest scale and compare simulated
   winrates with published or expert expectations.
6. Only after strength and calibration pass, run the first format-isolated,
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
- Design Observation v2 and its migration without changing the live run's
  input schema.
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
- ◐ Standard: corpus and namespace exist; qualification, Observation v2,
  calibration, and production Harvest remain.
- ▢ Modern: no representative training corpus yet.
- ▢ Pioneer: no representative training corpus yet.
- ▢ Unified builder: format-aware feedback queue not connected.

---

## Known limitations that still matter

Priority 0 simulation integrity has no known open defect. Priority 1 agent
choice exposure is substantially complete; blocker-side combat damage ordering
is the remaining scripted decision path. The following bounded behaviors fail
closed or remain fidelity-marked rather than being treated as fully supported.

| Area | Current boundary |
| --- | --- |
| Choice exposure | Blocker-side damage ordering is automatic; attacker-side ordering is exposed. |
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
| Strategy memory | Per-environment; its optional enhancement pass is nondeterministic, though game RNG is unaffected. |
| Action tensor | Fixed at 480 actions. Action 479 pages overflow contexts through normal handlers; dormant indices 205–223 remain mask-invalid until their mechanics exist. |
| Format data | Registry/schema are lineage-bound and Bo1. Sideboards are retained and legality-checked but not played. Schema vocabulary growth requires a new policy lineage. |

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
   the remaining blocker-side damage-order choice.
5. A trained policy passes paired-seat qualification and statistics come from
   qualified policy/league play rather than the random or scripted baseline.
6. Known-matchup calibration passes within a documented tolerance.
7. The format-aware builder consumes version-matched statistics and support
   evidence, proposes legal candidates, and feeds them back into evaluation
   and Harvest without manual file editing.

---

## Checkpoint and schema boundaries

Historical boundaries are retained here so old artifacts cannot be resumed by
mistake. The practical rule remains: **use Round 7.82 or later.**

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

The canonical registry is append-only within a compatible width. Expanding the
feature vocabulary or Observation v2 changes the schema hash and starts a new
lineage. Run manifests—not individual game-log rows—carry format, pool,
corpus, registry, schema, policy, and checkpoint provenance.

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
