# Playersim roadmap

Current as of July 23, 2026.

## Goal

Train a two-player Magic policy strong and reliable enough to generate
trustworthy, format-scoped statistics for a deck-building AI.

The project is not production-ready until:

1. engine and telemetry integrity are fail-closed;
2. a checkpoint passes held-out paired-seat strength qualification;
3. matchup calibration agrees with external or expert expectations;
4. statistics come from qualified policy or league play;
5. a format-aware builder consumes those statistics and returns legal
   candidates to evaluation automatically.

Permanent non-goals are multiplayer, Commander, and Planechase.

## Current snapshot

| Area | State |
| --- | --- |
| Rules engine | Operational; no known unresolved Priority 0 integrity defect |
| Observation | v6, exact-own strategy profile |
| Policy | MaskablePPO with bounded strategy FiLM |
| Active run | `round-8.00-obs-v6-film-v1` |
| Last completed run | Round 7.99, 2,007,040 actual timesteps |
| Qualified checkpoint | None |
| Standard corpus | Eight reviewed decks |
| Standard card support | 0/4,702 semantically verified |
| Harvest | Strict fixture and parallel protocol operational |
| Deck builder | Statistics contract exists; automated consumer not connected |
| Other formats | Modern and Pioneer not started as representative programs |

Round 7.99 proved that checkpoint self-play alone did not solve the
archetype-specific piloting wall. In comparable fixed evaluation slices,
control remained 0-24, and accumulated results were effectively zero for 4c
Control and Jeskai Lessons and near-zero for Dimir Excruciator.

Round 8.00 tests the next structural hypothesis: one shared policy needs an
explicit representation of its own deck's plan.

## Active run: Round 8.00

`round-8.00-obs-v6-film-v1` is running from a clean source revision
`2a0e2a0`.

Changed from Round 7.99:

- Observation v5 becomes v6.
- The observer receives its own exact 54-value deck-strategy profile.
- All eight Standard decks carry reviewed strategy profiles.
- A dedicated 54-to-64 encoder drives bounded FiLM scale and shift after the
  shared-state projection.
- The centralized taxonomy/classifier now feeds ingestion, analytics, the
  own-deck planner, run lineage, and the policy observation.

Held fixed:

| Setting | Value |
| --- | --- |
| Requested timesteps | 2,000,000 |
| Learning rate / rollout / batch | 2e-4 / 1,024 / 256 |
| Environments | 8 |
| Reward | `tempo-graded-potential-v1` |
| Curriculum | `combat-v7` |
| Training / evaluation seed | `20260715` / `21260715` |
| Evaluation | same 64 paired-seat cases every 100k |
| Checkpoint league | enabled |
| Pool cadence / size / probability | 100k / 4 / 0.5 |
| Permanent checkpoints | every 500k |
| Matchup weighting | off |
| Card registry / feature schema | unchanged |

The run was launched with explicit frozen flags rather than a registered
`--canary-config`, because named configurations currently end at the v5-pinned
Round 7.99 contract. Its resolved manifest was compared against 7.99 and the
controlled settings above match exactly. Before another run, add a v6
preflight/named contract so this comparison is enforced rather than manual.

Authoritative live state:

`models/ALPHA_ZERO_MTG_V3.00_20260723_012347_round-8.00-obs-v6-film-v1/training_run.json`

Do not change policy, reward, curriculum, corpus, opponent mix, or evaluation
settings during this run.

### Run-stopping conditions

Stop and diagnose immediately if any of these occur:

- a fidelity, unparsed, crash, execution-failure, or invalid-action counter;
- a degraded observation or fallback reset;
- an incompatible or rejected checkpoint-pool lease;
- non-finite policy, critic, reward, or gradient telemetry;
- a manifest, evaluation, reload, or final-artifact identity mismatch;
- a repeat of a known stats-corrupting or hidden-information defect.

### Required final analysis

When the run ends:

1. verify exact final checkpoint/evaluation/reload hashes and timesteps;
2. compare decisive wins, timeouts, score, and 95% lower bounds with 7.99;
3. compare every deck, especially Jeskai Lessons, 4c Control, and Dimir
   Excruciator;
4. measure whether profile-conditioned behavior actually differs by archetype;
5. inspect post-warmup self-play share, evaluator duration, steps/minute, RAM,
   and rollout stragglers;
6. treat any fidelity contamination as invalidating strength conclusions.

## Verified delivery baseline

| Gate | Result |
| --- | --- |
| Discovered unit tests | 873/873 |
| Golden scenarios | 409/409 |
| Runtime smoke | 9/9 |
| Training smoke | 14/14 |
| Fixture Harvest | 24/24 |
| Production Harvest protocol | 17/17 |
| Card registry | 19/19 |
| Deck ingestion | 13/13 |
| Default invariant fuzz | 8 seeds x 1,000 actions plus phase-boundary check |

The 32-seed x 10,000-action long-fuzz result is historical until rerun.

Current hard identities:

- Observation v6:
  `6521db9c0c70c919a63c34e9c99463a3b801e25ae91149fd518a34054989e790`
- Extractor architecture:
  `179b31ea6925d112e0b527cd1f03aa15dae6a36a061d50c3f66c671c1028d9ab`

All earlier checkpoints are incompatible with this boundary.

## Card support and probe baseline

Every card in the current Standard support ledger remains unverified. Static
status and dynamic probe status are triage dimensions, not semantic support:

| Evidence | Current result |
| --- | --- |
| Semantic ledger | 0 verified / 4,702 unverified |
| Static ledger | 119 observed-clean, 3,420 unseen, 833 partial, 330 unparsed |
| Latest complete full-pool schema-v3 probe | 48 execution-passed, 2,764 coverage gaps, 1,890 failed |
| July 21 Room/Exhaust targeted probe | 41 coverage gaps, 2 failed |
| July 22 copied-card targeted probe | 3 coverage gaps, 0 failed |

The latest complete full-pool report is:

`probe_runs/standard-full-schema3-repair1-2026-07-18-v3/card_probe_report.json`

Its evidence predates the current source revision, so completion work requires
a fresh full-pool probe against the current engine. Even a fresh
`execution_passed` result remains bounded mechanical evidence. A card becomes
supported only after every discovered and printed surface is mapped to
assertion-bearing exact-state scenarios, including relevant negative cases.

## Ordered execution plan

### 1. Finish and analyze Round 8.00

- Preserve the controlled experiment.
- Monitor fidelity, self-play share, throughput, evaluation lag, and resources.
- Perform the required final analysis above.
- Decide from evidence whether strategy conditioning improved control and
  combo piloting.

Exit: a complete, attributable final artifact and an evidence-backed verdict.

### 2. Qualify a checkpoint independently

Use a held-out seed and paired-seat cases not used for training or periodic
evaluation.

Required pass:

- point score at least 55%;
- pair-aware 95% lower confidence bound at least 55%;
- timeout life leads worth zero;
- zero fidelity counters;
- no `unparsed` or `crash` support entries;
- exact checkpoint and lineage provenance.

The current Harvest CLI enforces the point threshold but not the full interval
gate. Add interval persistence/enforcement before treating a CLI pass as final
qualification.

Exit: one frozen checkpoint that has beaten scripted play under the complete
held-out contract.

### 3. Complete Standard card support

Finish support for all 4,702 cards in the Standard ledger:

1. rerun the complete schema-v3 pool probe against the current source;
2. triage failures first, then coverage gaps, ordered by severity, corpus
   copies, builder relevance, and observed frequency;
3. repair shared mechanics before card-specific fallbacks;
4. add production-path scenarios for every ability, mode, target, choice,
   trigger, conditional branch, and relevant negative case;
5. require real payment, mask exposure, resolution, and exact post-state
   assertions;
6. eliminate `unparsed`, `partial`, and `unseen` support debt;
7. implement the machine-verifiable surface-to-test mapping needed to unlock
   semantic promotion;
8. rerun affected-card probes immediately and full-pool probes at milestones.

Exit: 4,702/4,702 cards supported under the semantic evidence contract, with
zero unresolved failures or coverage gaps.

### 4. Calibrate Standard and produce the first trusted harvest

- Run 3-5 known matchup pairs at meaningful sample size.
- Compare simulated win rates and play patterns with published or expert
  expectations.
- Investigate discrepancies as policy, rules, support, or sample-quality
  problems rather than tuning blindly.
- Use only qualified checkpoint or league play.
- Keep worker outputs isolated and lineage-compatible.
- Reject fidelity-contaminated games.
- Publish only after every shard and cross-file invariant passes.

Exit: documented calibration tolerances pass and a format-isolated,
strength-grade Standard statistics set is published.

### 5. Connect the deck builder

Implement the automated consumer of [STATS_SCHEMA.md](STATS_SCHEMA.md):

1. select only lineage-compatible, qualified evidence;
2. enforce format legality and the append-only card registry;
3. exclude `crash`, `unparsed`, and `excluded` cards;
4. down-weight `partial`, sparse, weak-policy, and uncertain evidence;
5. generate a legal candidate with a separate aspirational target profile;
6. infer the candidate's actual strategy profile from its cardlist;
7. route the candidate through support preflight, paired evaluation, and
   Harvest;
8. feed promotion or rejection back without manual file editing.

Exit: a closed, auditable candidate-generation loop.

### 6. Expand formats

After Standard qualification, calibration, Harvest, and builder integration:

1. build a representative Modern corpus and namespace;
2. repeat support triage, training/qualification, calibration, and Harvest;
3. repeat for Pioneer.

## Parallel work

These tasks may proceed without changing the active experiment:

- fix newly observed fidelity defects, scenario first;
- advance the explicit Standard support-completion step in descending impact
  order;
- profile production training and Harvest workloads;
- land only behavior-neutral performance changes with equivalence tests;
- design the builder consumer and uncertainty model;
- add a v6 named-canary/preflight contract for the next run;
- rerun long fuzz on its scheduled/manual gate.

## Workstreams

| Workstream | State | Next exit |
| --- | --- | --- |
| Policy strength | Active | Round 8.00 verdict, then held-out qualification |
| Card fidelity | 0/4,702 semantically verified | Complete all card surfaces and exact-state evidence |
| Verification | Strong infrastructure | Maintain gates and refresh long fuzz |
| Throughput | Measured, still costly | Profile before any semantic change |
| Harvest | Operational | Add final interval enforcement and use a qualified policy |
| Deck builder | Contract only | Automated legal candidate feedback loop |
| Format program | Standard only | Complete Standard before Modern/Pioneer |

## Known limitations

| Area | Boundary |
| --- | --- |
| Policy strength | No checkpoint has passed production qualification |
| Card support | All 4,702 Standard cards remain semantically unverified |
| Combat | Multi-blocker damage assignment and rare attack/block requirements remain bounded |
| Generic parsing | Rare replacement, trigger, permission, and linked-search wording remains card-specific |
| Special mechanics | Mutate, Meld, Specialize, some DFC/adventure paths, and uncommon dice families are incomplete |
| Format play | Best-of-one only; sideboards are retained but not played |
| Action space | Fixed at 480 with overflow paging |
| Deck builder | Automatic statistics-to-candidate consumer is not connected |

Unknown templates must fail closed or create fidelity evidence. They must never
silently count as supported.

## Definition of done

The project is complete only when:

1. all delivery gates remain green;
2. no known stats-corrupting defect remains;
3. every builder-pool card is evidence-qualified or explicitly
   excluded/down-weighted;
4. all value-relevant player decisions are exposed to the policy;
5. a checkpoint passes held-out paired-seat strength qualification;
6. known-matchup calibration passes;
7. production statistics come from qualified play;
8. the format-aware builder consumes versioned evidence and automatically
   returns legal candidates to evaluation and Harvest.

## Compatibility boundaries

| Boundary | Incompatible change |
| --- | --- |
| 7.83 | Observation v2, stable categorical identity, expanded public state |
| 7.89 | Observation v3 semantic corrections and renewed lineage |
| 7.94 | `tempo-graded-potential-v1` reward |
| 7.95 | `combat-v7` and 2M horizon |
| 7.96 | Observation v4 own decklist/library composition |
| 7.97 | Observation v5 producible mana by color |
| 7.98 | Resource-bounded checkpoint self-play |
| 7.99 | Copied-text repair and 500k permanent checkpoints |
| 8.00 | Observation v6 exact-own strategy profile and bounded FiLM |

Changing observation fields, bounds, visibility, identity capacity, feature
layout, extractor routing, reward contract, or incompatible curriculum state
starts a new checkpoint lineage. Run manifests, not filenames, are
authoritative.

Resume and checkpoint-backed Harvest require:

- an approved ZIP artifact pointer in the nearest source manifest;
- exact SHA-256 and size;
- matching observation and extractor architecture;
- matching card registry and feature schema;
- the applicable corpus and strategy-profile lineage.

## Compact history

| Period | Durable result |
| --- | --- |
| 1-7.36 | Built the rules/parser foundation, masked policy boundary, replay/fuzz, and Harvest scaffolding |
| 7.37-7.50 | Rebuilt reward and statistics, added scripted play, format namespaces, registry, feature schema, and support ledger |
| 7.51-7.82 | Expanded mechanics, fixed identity/mana/choice/combat/targeting failures, and made evaluation asynchronous |
| 7.83-7.90 | Froze Observation v2/v3, removed unstable/dead inputs, repaired perspective, and added deterministic lineage |
| 7.91-7.95 | Built annealed curricula and graded reward; repeated runs exposed a persistent piloting plateau |
| 7.96-7.97 | Added own-deck/library and producible-mana observations; parameter-only retries still failed qualification |
| 7.98 | Added checkpoint self-play; run stopped on copied-card printed-name semantics |
| 7.99 | Repaired copied effects and completed 2M; self-play did not solve control piloting |
| 8.00 | Added centralized strategy profiles, Observation v6, and FiLM conditioning |

Detailed historical evidence belongs in run manifests, evaluation histories,
logs, probe reports, tests, and Git history rather than this roadmap.

## Working agreements

- Write the failing scenario before the fix.
- Treat untested subsystems as suspect.
- Parsing or bounded-probe success is not semantic proof.
- Keep mask, displayed choice, execution, and resolution on one legality
  contract.
- Treat warnings, degraded observations, swallowed exceptions, and fidelity
  counters as failures until classified.
- Make player perspective, card identity, and hidden-information boundaries
  explicit.
- Never merge statistics or checkpoints across incompatible lineage.
- Keep performance-only changes behaviorally identical and prove equivalence.
- Run focused tests plus scenario, smoke, training smoke, and default fuzz
  before delivery.
- Put current decisions and exits here; put per-run debugging in manifests and
  logs.
