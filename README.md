# Playersim

A reinforcement-learning engine and training pipeline for two-player *Magic: The
Gathering*.

**Mission.** Train an agent to play two-player Magic well enough that its games
yield *trustworthy* per-card and per-deck statistics, which feed a downstream
deck-construction AI searching for the best deck per format. Every design choice
is ranked by one question: *does it make the statistics more trustworthy for the
deck builder?*

Out of scope permanently: multiplayer, Commander, Planechase, and match play
(best-of-three is a possible late add only if a target format demands it).

The agent is trained with mask-aware PPO (Stable-Baselines3 + SB3-Contrib). The
current `combat-v7` training default progresses from passive goldfish through
novice/scripted mixtures. Round 7.98 adds an opt-in, resource-bounded
checkpoint league for training while fixed evaluation remains scripted. The
Harvest protocol supports checkpoint-vs-checkpoint evaluation and promotion
once a checkpoint beats scripted play.

---

## Status

The rules engine, statistics pipeline, format/lineage plumbing, and training and
Harvest paths are operational and gated by a large regression suite. Rules and
card coverage are still expanding, and **no checkpoint has yet been shown to beat
scripted play**, so the statistics are not yet strength-grade.

[ROADMAP.md](ROADMAP.md) is the authoritative status and next-work list;
[STATS_SCHEMA.md](STATS_SCHEMA.md) is the contract for anything that consumes the
output statistics; [ARCHETYPE_SCHEMA.md](ARCHETYPE_SCHEMA.md) defines the
versioned deck-strategy taxonomy and its hidden-information boundary.
`DeckStats_Viewer/` provides a dependency-free local
workbench for run provenance, checkpoint trends, every evaluation case and
debug replay, DeckStats scopes, canonical-ID CardMemory comparisons, safe
StrategyMemory diagnostics, game logs, fidelity, and Harvest artifacts. New
evaluation sidecars also expose bounded, explicitly non-causal Enhanced Card
Evaluator activity beside both players' atomic actions.
Launch it with
`.\MTGenv\Scripts\python.exe .\DeckStats_Viewer\MTG_Statistics_Viewer.py`.

> **Statistics collected before July 2026 are unusable** (wrong player, wrong
> winner, fabricated play turns, and several now-fixed stats-corrupting bugs).
> Wipe and re-harvest with the current engine.

---

## How it fits together

```mermaid
flowchart LR
    subgraph Engine["Rules engine — Playersim/"]
        GS["GameState<br/>layers · stack · combat · SBAs"]
        ENV["Gym environment<br/>action mask + observation"]
        GS --- ENV
    end
    subgraph Data["Format namespace — formats/&lt;fmt&gt;/"]
        REG["canonical card registry<br/>(stable card IDs)"]
        SCH["frozen feature schema"]
        DECKS["deck corpus<br/>(metagame + imported)"]
        LED["support ledger"]
    end
    subgraph Learn["Training — main.py"]
        POL["MaskablePPO policy<br/>FixedWindow extractor"]
    end
    subgraph Harvest["Harvest — harvest_*.py"]
        HV["parallel shards<br/>+ paired-seat promotion"]
    end
    subgraph Stats["Statistics"]
        LOG["game log +<br/>tracker aggregates"]
        MAN["card support manifest"]
    end
    REG --> ENV
    SCH --> ENV
    DECKS --> ENV
    ENV <--> POL
    ENV --> HV
    POL --> HV
    HV --> LOG
    HV --> MAN
    LOG --> BUILD["deck builder<br/>(STATS_SCHEMA.md contract)"]
    MAN --> BUILD
    LED -. excludes gaps .-> BUILD
    BUILD -. candidate decks .-> DECKS
```

- **Rules engine** (`Playersim/`) simulates phases, the stack, the layer system,
  combat, replacement effects, and state-based actions, and exposes the game as a
  masked Gym environment so only legal actions are ever selectable.
- **Format namespace** (`formats/<format>/`) pins the inputs a run depends on: a
  canonical card registry (stable, append-only card IDs keyed by name + Scryfall
  `oracle_id`), a frozen feature schema (fixed observation width), the deck
  corpus, and a static support ledger. Each artifact is versioned and self-hashed
  so adding cards cannot silently change model input width or invalidate a
  checkpoint.
- **Training** (`main.py`) runs mask-aware PPO, alternating the learned policy
  between seats, and writes a full provenance manifest per run.
- **Harvest** (`harvest_fixtures.py`, `harvest_protocol.py`) plays games to
  produce statistics; the parallel protocol also scores checkpoint promotions.
- **Statistics** are the product: an append-only game log, tracker aggregates,
  card-memory records, and a card-support manifest — all consumed by the
  downstream deck builder through the `STATS_SCHEMA.md` contract.

---

## Repository layout

```
main.py                     Training entry point (PPO, callbacks, provenance)
harvest_fixtures.py         Deterministic single-process Harvest + strict artifact validation
harvest_protocol.py         Parallel sharded Harvest and paired-seat promotion
ROADMAP.md                  Authoritative status and next-work list
STATS_SCHEMA.md             Output-statistics contract for the deck builder
ARCHETYPE_SCHEMA.md         Versioned deck-strategy profile contract

Playersim/                  The engine + tooling package
  card.py, card_registry.py     Card model; canonical registry + frozen feature schema
  game_state*.py, layer_system.py, combat*.py, replacement_effects.py, targeting.py
  environment.py                Masked Gym environment
  actions*.py, ability_*.py     Action space, casting, choices, combat, mechanics
  deck_corpus.py, deck_ingest.py, deck_legality.py   Corpus hydration, import, legality
  support_preflight.py, card_support.py              Static coverage ledger + manifest
  card_probe.py                                      Fail-closed dynamic full-pool probes
  deck_stats_tracker.py, card_memory.py              Statistics aggregation
  strategic_planner*.py, enhanced_*.py, strategy_memory.py   Heuristic evaluation/planning

formats/standard/           Frozen Standard namespace (registry, schema, ledger, decks/)
Format Card Lists/          Pinned per-format card-pool snapshots (<format>.jsonl)
Mtg_Cards/                  Scryfall bulk card data
tests/                      Regression suites (see Verification)
MTGenv/                     Checked-in virtual environment (Windows)
```

---

## Setup

- **Python 3.11+** (developed and tested on 3.14).
- Dependencies: `pip install -r requirements.txt`
  (PyTorch, Stable-Baselines3 `[extra]`, SB3-Contrib, Gymnasium, Optuna,
  TensorBoard, NumPy, Matplotlib, psutil, GPUtil).

**GPU note.** The PyTorch build must match your GPU. The checked-in environment
uses a CUDA `cu130` wheel (`torch 2.12.1+cu130`) for an RTX 5060 (`sm_120`);
older CUDA wheels will not run that card. Pass `--cpu-only` to force CPU. When
more than one training environment is used, rollouts run in `SubprocVecEnv`
worker processes (Windows `spawn`).

On Windows the checked-in interpreter can be used directly as
`.\MTGenv\Scripts\python.exe` in place of `python`.

---

## Verification

Run these from the repository root before training or changing engine rules. The
current gate counts are tracked in [ROADMAP.md](ROADMAP.md).

The canonical delivery gate is:

```bash
python -m unittest discover -s tests -p "*_test.py"
python tests/scenario_test.py
python tests/smoke_test.py
python tests/train_smoke_test.py
python tests/invariant_fuzz_test.py --profile default
```

For the current working tree, those gates are green at 848/848 discovered unit
tests, 409/409 scenarios, 9/9 runtime smoke, 13/13 training smoke, and all 8
default fuzz seeds x 1,000 valid actions plus the phase-boundary check. The
previously recorded long-fuzz result remains historical until that
scheduled/manual gate is rerun.

```bash
python tests/smoke_test.py                    # engine end-to-end (no training stack)
python tests/scenario_test.py                 # golden rules scenarios
python tests/layer_system_test.py             # continuous-effect/CDA regressions
python tests/multi_instruction_target_test.py # independent spell target slots
python tests/gift_target_parity_test.py        # conditional target/mask parity
python tests/action_catalog_test.py           # overflow response dispatch
python tests/mana_payment_test.py              # hybrid/snow/Phyrexian payment
python tests/mana_auto_tap_test.py             # restricted auto-tap parity
python tests/optional_discard_test.py          # optional discard continuations
python tests/deck_stats_numeric_test.py        # symbolic-stat analytics
python tests/strategic_planner_numeric_test.py # finite planner estimates
python tests/choice_context_test.py            # nested trigger/choice continuation
python tests/modok_warning_regression_test.py  # M.O.D.O.K. activation + warning paths
python tests/evoke_casting_test.py              # Evoke action/cost exposure
python tests/deceit_real_card_test.py           # colored ETBs + Evoke sequencing
python tests/landfall_runtime_test.py           # real Landfall gates/effects
python tests/leatherhead_colorstorm_test.py     # reflexive/Opus real-card paths
python tests/quantum_riddler_test.py             # batch draw replacements
python tests/superior_spider_man_test.py         # Mind Swap copy/exile lifecycle
python tests/prepared_test.py                    # Prepared copy/payment lifecycle
python tests/log_rules_runtime_test.py           # canary cost/land/cast-lock rules
python tests/aura_warning_regression_test.py     # Aura targets + warning no-ops
python tests/target_lifecycle_regression_test.py # legal-target/fizzle boundary
python tests/hearth_elemental_test.py             # graveyard-union cost reduction
python tests/doomsday_excruciator_test.py         # cast-only hidden exile lifecycle
python tests/momo_cost_reduction_test.py           # first eligible flying spell cost
python tests/bushwhack_fight_test.py               # role-aware two-creature fight
python tests/canary_effect_binding_test.py     # production-card effect binding
python tests/esper_origins_test.py             # Flashback-to-Saga real-card path
python tests/stack_integrity_test.py           # spell lifecycle/finalization
python tests/train_smoke_test.py              # PPO / SB3 integration
python tests/card_registry_test.py            # canonical registry + feature schema
python tests/deck_corpus_test.py              # corpus hydration
python tests/deck_ingest_test.py              # deck import + legality
python tests/support_preflight_test.py        # full-pool coverage ledger
python tests/harvest_fixtures_test.py         # single-process Harvest contract
python tests/harvest_protocol_test.py         # parallel Harvest + promotion
python tests/invariant_fuzz_config_test.py    # invariant harness config
python tests/invariant_fuzz_test.py --profile default   # 8 seeds x 1,000 actions
```

**Working agreement:** every engine change ships with a failing scenario written
*first*. Untested subsystems are assumed broken — this practice has repeatedly
surfaced phantom methods and dead/overfiring subsystems. A card is never called
clean from parsing or a bounded probe alone. Semantic promotion is currently
hard-locked; permanent exact-state evidence must first gain a machine-verifiable
scenario-to-surface mapping (see the schema-v2 contract below and the ROADMAP
appendix bug catalog).

---

## Formats, decks, and lineage

A **format namespace** under `formats/<format>/` pins everything a run depends
on. Freeze one from a deck corpus:

```bash
python -m Playersim.card_registry freeze --decks <corpus_dir> --format standard \
  --output formats/standard
```

This writes `card_registry.json` (canonical card IDs) and `feature_schema.json`
(frozen card-vector layout), both versioned and self-hashed. Use `--extend` to
append new cards without renumbering existing IDs; a card that would widen the
frozen subtype vocabulary is rejected (that requires a new schema version, and
therefore a new checkpoint lineage).

The separate global policy-input contract is Observation v5, documented in
[OBSERVATION_SCHEMA.md](OBSERVATION_SCHEMA.md) and self-hashed by
`Playersim/observation_schema.py`.

Every run-level manifest (`training_run.json`, `harvest_run.json`,
`harvest_protocol.json`, `promotion.json`) stamps a `lineage` object recording
the format, pool-snapshot hash, corpus hash, card registry, card feature schema,
and observation schema versions + hashes. **Never merge statistics whose
lineage hashes differ** — they may disagree on card identity or on what the
policy observed. See
[STATS_SCHEMA.md](STATS_SCHEMA.md) → "Format namespaces and run lineage".

### Deck pool

The default training and Harvest pool is `formats/standard/decks/`, loaded
recursively. The pinned representative metagame lives under `metagame/`;
user-supplied decks live separately under `imported/`, so regenerating the
metagame can never overwrite an import. Harvest needs at least two decks in the
selected pool.

Regenerate the simulator-ready metagame files from the reviewable compact corpus
and the pinned card snapshot:

```bash
python -m Playersim.deck_corpus --replace
```

### Importing a deck list

Supply an Arena/simple-text list (`4 Card Name`, with optional `Deck`,
`Sideboard`, `Maybeboard` headings) or a compact JSON list:

```bash
python -m Playersim.deck_ingest path/to/my_deck.txt --dry-run   # validate only
python -m Playersim.deck_ingest path/to/my_deck.txt             # import
```

The importer resolves cards against the pinned snapshots; enforces 60-card
constructed legality, sideboard and copy limits, and a 1,000-card sanity cap;
and reports every matching format. Without `--format` it picks the narrowest
supported match in `Standard → Pioneer → Modern` order. A successful import
writes a hydrated deck to `formats/<format>/decks/imported/`, where training and
Harvest discover it through the recursive loader. `--strict-support` rejects
main-deck cards whose ledger status is `unparsed`, `crash`, or `excluded`;
`--replace` updates an existing named import. Sideboards are validated and
retained but not played by the best-of-one runtime; Maybeboards are ignored.

### Support ledger (coverage)

Before widening a format corpus, regenerate its support ledger. Schema v2 keeps
`static_status` (observed-clean, unseen-clean, `partial`, `unparsed`, or
`crash`) separate from `semantic_status`. Static cleanliness is only a triage
signal; it is never rules proof:

```bash
python -m Playersim.support_preflight \
  --snapshot "Format Card Lists/standard.jsonl" \
  --registry formats/standard/card_registry.json \
  --decks formats/standard/metagame_corpus_2026-07-11.json \
  --corpus-label representative-meta-2026-07-11 \
  --overrides formats/standard/support_overrides.json --format standard \
  --output formats/standard/support_ledger.json
```

The reserved `verified` state requires a nonempty candidate record pinned to the
card's Oracle ID and rules hash, an independently generated Oracle-text plus
dynamic-probe surface inventory, exact surface-set equality, and real
assertion-bearing unittest nodes. The validator rejects skipped, failing,
assertionless, stale, non-discoverable, or static-issue evidence and executes
each named node exactly once. Because a passing Python test still cannot prove
that its assertions establish every declared surface, actual ledger promotion
is hard-disabled and any nonempty `verified` override is rejected. The 96
former name-only claims are retained as `legacy_verified_claims` for audit
only and cannot change card status.

The current ledger, audited July 21, has 4,702 cards: 0 semantically verified,
4,702 unverified;
static status is 119 observed-clean, 3,420 unseen-clean, 833 partial, and 330
unparsed (75.3% static-clean). Its canonical SHA-256 is
`4ed2dce764032d9cfdfa7e2f9721bc0514ac6bef0b514daaa15a58f1db5b7e14`
(physical SHA-256
`17a74dae77ba533a8d3ef745790ceeb217f3f6777470142b58e5d17db0dea7f7`).

### Dynamic card-pool probe

The static ledger is not rules proof. Run every frozen-pool card through the
public action pipeline with deterministic fixtures, real mask-valid payment,
resolution, paired priority and trigger non-event checks, independent compound
trigger arms, lexical printed-trigger reconciliation, warning/fidelity gates,
and per-card atomic evidence:

```bash
python -m Playersim.card_probe \
  --snapshot "Format Card Lists/standard.jsonl" \
  --registry formats/standard/card_registry.json \
  --ledger formats/standard/support_ledger.json --format standard \
  --output probe_runs/standard-full
```

Use `--resume` after an interrupted run; artifacts are reused only when their
oracle row, limits, inputs, probe source, and complete `Playersim/**/*.py`
source hash still match. `failed` means an execution, warning, fidelity,
manifest, invariant, or known static-preflight failure. `coverage_gap` means a
discovered surface or branch was not independently exercised. Even
`execution_passed` is bounded mechanical evidence only: every card remains
`semantic_status=unverified` until permanent scenarios assert its exact rules
outcomes across all abilities, modes, and choices.

The July 22 copied-source repair replay is
`probe_runs/standard-superior-copy-repair-2026-07-22-v1/card_probe_report.json`.
Its source-hashed per-card run reports 0 failures and 3 explicit
`coverage_gap` results for Superior Spider-Man, Colorstorm Stallion, and
Deceit; it does not claim that isolated probes cover their composition. The
composed runtime paths live in `tests/superior_spider_man_test.py`: one casts
a five-mana spell through copied Colorstorm's Opus, and one resolves copied
Deceit's black ETB through target choice and nonland discard. Both assert that
the fidelity counters remain unchanged. The report SHA-256 is
`7ccd221cfa1fcdba6c37c323612da72d508d0fed73d78766bb2fcc6f968a2ed6`
canonical and
`0d9d3127ec61239e12d7853f65c7748f30d32d13ec68a311f12e30590f7ff561`
physical.

The current local, Git-ignored Room/Exhaust refresh is
`probe_runs/standard-room-exhaust-evidence-2026-07-21-v4/card_probe_report.json`,
pinned to the final 49-file `Playersim/**/*.py` engine SHA-256
`15e3006bc51e200b94b042139ca532b690a8eef4560c2994470809e65824c931`.
Across the exact 43-card selector it reports 41 `coverage_gap`, 2 `failed`
(Charred Foyer // Warped Space and Pit Automaton), and 0
`execution_passed`; an exact `--resume` reused all 43 artifacts. Independent
audit validated the complete artifact set, every self-hash and report/card
link, and current report/run/input identities. Report SHA-256 is
`e933b32699548d39e933d503888e3a5e78c8e4e1058d8841a0b13fdb58a2e062`
canonical and
`f32b845e84237f0cea58bbc2a3e49fdffa8cda661bd3e8ad3442fc0d79094bd2`
physical; run SHA-256 is
`df52c3a8fe6bd34917352d4e4308bce01e39d8df72f1eefe883bf824ebb29aa6`
canonical and
`354083d37220af88f524ad23fa64cfba83c08cac41968562bd1cf2d7bbce6164`
physical.

The scoped residual replay is
`probe_runs/standard-room-exhaust-expanded-2026-07-21-v4/card_probe_report.json`.
Charred Foyer, Fell Gravship, and Pit Automaton all fail closed; its exact
resume reused all 3 artifacts and the same independent audit passed. Charred
Foyer emits 5 identical warnings and one manifest issue recorded 5 times for
its unsupported `{0}` exile alternative cost; Fell Gravship emits 24 warnings
across four unique Station and `8+ | Flying, lifelink` layer diagnostics; Pit
Automaton is rejected by ledger evidence for its unsupported source-coupled
copy instruction. Report
SHA-256 is
`d4a7218dc326e8b5b52e6749590f40a8a1ef041d518412ee2e237574c2a5953d`
canonical and
`8487ee985b15f07e1b92ec265bc455ee6551c441b380cdb116c6bb4dd81bf04d`
physical; run SHA-256 is
`e17b7e5e042f032062aa078f50b7e3d85c3bb70fcb4997882c2c4b2546fcab2b`
canonical and
`b959f0eec2a12c51831151012c5dd5ae37aee4424f80894d5a7c3a5ac316b2f2`
physical. All 46 card artifacts remain `semantic_status=unverified`; the
expected fresh/resume exit code is 2 because the probe deliberately surfaces
failures.

The first schema-v2 affected replay is
`probe_runs/standard-affected-evidence-2026-07-19-v1/card_probe_report.json`.
Afterburner Expert, Optimistic Scavenger, Roaring Furnace // Steaming Sauna, and
Underwater Tunnel // Slimy Aquarium all finished as explicit `coverage_gap`
with zero failures and zero diagnostics; an identical `--resume` accepted all
four artifacts. Its physical SHA-256 is
`f74e355275d311ec2210a66bda1f20383ab4cb3e875f01f25447208fd5dbc9f6`
(canonical
`eac24b1f92bce0363fcbd17e8a2fc2c742869ca5e34cee99fd433813f69a865b`).
The permanent pilot tests cover Afterburner's cast/payment, Exhaust masks and
trigger negatives, plus Room source/door identity and Roaring Furnace's live
hand-size damage; uncovered alternate Room faces and missing generic trigger
fixtures remain gaps rather than promotions.

The repair-affected replay is
`probe_runs/standard-repair1-expanded5-2026-07-18-v1/card_probe_report.json`.
It completed all 770 selected cards with 437 `failed`, 314 `coverage_gap`, and
19 `execution_passed`. An independent artifact audit reported zero errors and
zero warnings; an identical `--resume` pass accepted all 770 artifacts without
changing the card manifest or report. The physical report SHA-256 is
`f9b4d4f9a3aba6cc4eb49b4ac3802822a3551a158de68ef6f2e3d06592d28f10`
(canonical report SHA-256
`fa15cc8b6b8a685a67d242e458028fce739c61049d5bd72c8b7c376c3466135e`).
All 770 cards remain `semantic_status=unverified`.

The fresh full-pool repair replay is
`probe_runs/standard-full-schema3-repair1-2026-07-18-v3/card_probe_report.json`.
It completed all 4,702 frozen Standard cards with 1,890 `failed`, 2,764
`coverage_gap`, and 48 `execution_passed`. Independent pre- and post-resume
audits each reported zero errors and zero warnings; the identical resume pass
accepted all 4,702 artifacts without changing the card manifest or report.
The physical report SHA-256 is
`c118a76b8601f356b9f88e868378fe3bcc6055b38207559b18dafdefd9f83dfa`
(canonical report SHA-256
`e9dc842ad77dbbb7a92ee1a139b6633861d948b45ff650536340098040cbda38`;
canonical card-manifest SHA-256
`4b0dc370c570f877a3c72e3273a44caa6ad6a4b6ce5d273af3d3ff83961f1c51`).
The 159 prior `verified`/`observed_clean` ledger cards contain zero failures,
111 explicit gaps, and 48 bounded mechanical passes. All 4,702 cards remain
`semantic_status=unverified`.

For comparison, the historical pre-repair full-pool schema-v3 baseline is
`probe_runs/standard-full-schema3-2026-07-18-v1/card_probe_report.json`: all
4,702 card artifacts were accepted by an identical resume pass, and independent
recomputation validated their identities, hashes, links, schema, terminal
statuses, and aggregate summary. It reports 1,930 `failed`, 2,725
`coverage_gap`, and 47 `execution_passed`; the physical report SHA-256 is
`2165f074334614706bac9e941eb90c43a90adac16f438f4ad500131482ff8091`.
All 4,702 cards remain semantically unverified.

That v1 baseline inventories 3,124 printed trigger clauses, matching 2,599 to runtime
triggers and retaining 525 unmatched clauses as explicit gaps. It independently
exercised 1,462 of 2,672 positive trigger arms, 1,574 of 2,611 close non-events,
and 3,287 of 3,480 public choice branches. The report also fails closed on
diagnostics and degraded support: 1,587 cards emitted diagnostics and 897
reported through the support manifest, and every affected card is `failed`.
The previously trusted ledger classes still contain zero runtime failures, but
only 47 of those 159 cards mechanically pass this stricter evidence contract.

The follow-up trusted-card replay is
`probe_runs/standard-trusted-branch-replay-2026-07-18-v3/card_probe_report.json`.
Across all 159 `verified`/`observed_clean` ledger cards it reports 0 `failed`,
98 `coverage_gap`, and 61 bounded mechanical passes. Public choice alternatives
are replayed as independent edges from fresh fixtures: all 133 discovered
choice-branch obligations were exercised without Cartesian path expansion.
Every one of these cards still has `semantic_status=unverified`; the mechanical
result never restores the old notion that a card is "clean."

The stricter trigger/printed-surface replay is
`probe_runs/standard-trusted-trigger-replay-2026-07-18-v1/card_probe_report.json`.
Across the same 159 cards it reports 0 `failed`, 112 `coverage_gap`, and 47
bounded mechanical passes. It independently exercised 55 of 77 trigger event
arms, 55 of 71 matched trigger non-events, and all 161 discovered public choice
branches. Its independent lexical inventory matched 68 of 81 printed triggers
to runtime registrations and retained the other 13 as explicit gaps; 66 of 72
printed conditional outcomes likewise remain gaps. The lower pass count is a
deliberate fail-closed reclassification, not a semantic regression, and all 159
cards remain `semantic_status=unverified`.

---

## Training

Round 7.99 is complete; do not relaunch or resume it. The next training command
will be published only after the Observation-v6 strategy encoding and
archetype-conditioning delivery gate passes. That canary must start fresh while
holding the 7.99 reward, curriculum, checkpoint-league, evaluation, and seed
contracts fixed.

No format or deck flags are required for the pinned Standard default. Custom
corpora are available through `--decks`, `--format`, and `--format-dir`.
The completed named canary failed closed on its enumerated CLI and complete PPO setting
tree, reward contract version/scalars, the full resolved curriculum hash,
Observation/registry/feature/corpus identities, feature-output width, CUDA
device class, evaluation-schedule hash, and the complete checkpoint-pool
contract. Round 7.99 checks two million timesteps,
learning rate `2e-4`, batch 256, rollout 1024, eight training environments,
100k evaluation cadence with 64 games (32 seat-swapped pairs), 500k permanent
recovery checkpoints, `combat-v7`, training seed `20260715`, and independent evaluation
seed `21260715`. It also pins matchup weighting off and checkpoint self-play on:
every 100k steps the learner atomically publishes a hashed snapshot into a
four-checkpoint FIFO disk pool, each worker eagerly validates and holds one
frozen CPU policy, and each episode chooses that resident checkpoint or the
scripted curriculum with probability 0.5. A lease changes only at a pool
refresh and commits only on reset; fixed evaluation receives no pool policy.
Opponent action/reward histories follow the learned and frozen roles even when
physical seats alternate. Each physical seat receives a private planner profile
derived only from its own deck, and emergency fallback resets rebuild the agent
layer and clear episode histories instead of retaining stale state. Public
threat scoring cannot use identities from the other seat's hidden hand.
Outside the named canary the league is off by default. Git/working-tree
provenance, runtime libraries, and the specific GPU model are recorded in
`training_run.json` for comparison; the canary selector does not constrain
those audit variables or claim to hash every implementation detail.

**Throughput.** Training is CPU-bound (the GPU learner needs seconds per
multi-minute rollout). Since Round 7.76 periodic evaluation runs
**asynchronously in a dedicated process**: the trainer saves a policy snapshot
at each `--eval-freq` boundary and keeps rolling while the evaluator scores it
(the pre-7.76 synchronous evaluator idled every worker — measured at **73% of
wall time** at the old defaults). Results land in TensorBoard on arrival;
`eval/evaluated_at_timesteps` records each score's true step. Use `--n-envs 8`
on a 6-core/12-thread machine (`--n-envs 0` auto-selects only 6; worker count
is RAM-bounded at ~0.3 MB per rollout-buffer step). If the evaluation cadence
outruns the evaluator, boundaries are skipped with a warning rather than
queueing stale snapshots. Skipped/cancelled boundaries are retained in
`evaluations.json`. A cadence boundary reached on the terminal rollout is
not scored before the last PPO update; it is replaced by one mandatory
post-update final-model evaluation at the model's actual timestep. Pending
snapshots are removed on interruption, while exactly one content-addressed
best-candidate snapshot is retained as bounded evaluation evidence. Training
workers batch compressed statistics for ten games; close still forces a final
flush. Deeper per-step optimizations are tracked as the ROADMAP Tier 3
throughput program.

Every periodic evaluation uses the same paired deck/seat/seed cases, generated
from `--eval-seed` independently of training RNG. The 64-game Standard schedule
contains 32 seat-swapped pairs, has no mirrors, and balances learned decks,
opponent decks, and physical seats.
Promotion is ordered by decisive wins, decisive win-minus-loss score, fewer
turn limits, then shaped return. A candidate is only promoted to
`best_model.zip` after the pair-aware 95% lower confidence bound for its
decisive-win plus half non-timeout-draw score reaches 55%; the point estimate
alone cannot qualify it. The exact evaluated best-so-far bytes are retained
separately under `best_candidate/`; this evidence artifact never bypasses the
qualification gate and may therefore be newer than `best_model.zip`.
The bound uses a conservative envelope of the episode-level Wilson interval
and the paired-case t interval. The interval and exact cases,
per-game outcomes, checkpoint SHA-256, and promotion decisions are published
atomically to schema-v4
`logs/<run>/evaluation/evaluations.json`. The mandatory final record is
role-tagged `final_model`; its evaluated archive is moved, not re-saved, into
`final_model.zip`, and validation requires the evaluation, reload, and final
artifact SHA-256 plus timestep to agree.

Strict training/evaluation fidelity also rejects nonzero effect-continuation
failure or lost-spell-recovery counters and retains their diagnostic contexts;
these recovery paths are not treated as clean merely because play continued.

Training and evaluation use separate statistics directories and alternate the
learned policy between P1 and P2 on successive episodes. Each run writes a
`training_run.json` provenance manifest under its model directory — seed, Git
revision and dirty state, CLI and resolved configuration, device and dependency
inventory, deck/lineage provenance, lifecycle result, and artifact paths. A
dirty run also stores a hashed `source_worktree.patch` beside the manifest,
including both tracked changes and untracked files.
Terminal manifest publication happens only after environments close, the final
completed/failed/interrupted line is flushed, and the per-run handler is
detached; the recorded runtime-log size and SHA-256 therefore cover the entire
immutable run log.

Training workers continue to record card and deck outcomes, but adaptive
history is analytics-only by default: it does not feed evaluator advice back
into live training decisions. Evaluation uses the same history-free behavior,
so identical public states do not acquire worker-local meanings over time.
Recorded history uses canonical archetype labels and each seat's own turn
count, rather than deck names or the engine's alternating global turn.

> **Checkpoint boundary (Round 7.89 / Observation v3).** The full Standard namespace widened card
> observations to 436 fields (259 subtype fields plus MDFC fields), signed live
> power/toughness, and count/stat bounds large enough for legal boards above 20
> permanents. Round 7.62 also widened the declared choice-count, allocation, and
> X-range bounds to remove the old X ceiling. Stable-Baselines validates the
> complete observation space. Round 7.72 replaced the overlapping/dead
> shaping paths with one discounted state-potential reward and reduced the
> procedural action-reward scale. Round 7.73 rebalanced terminal rewards
> (turn-limit timeouts now pay win +2 / draw −4 / loss −8), cut the action
> reward scale to 0.02, and added symlog compression to every continuous
> extractor input. Round 7.76 doubled the network (1024-dim extractor,
> 512/256/128 heads). Round 7.80 reweighted the state potential toward
> offense with a convex damage ramp (`discounted-state-potential-v3`) and
> populated the previously-zero `potential_combat_damage` observation. Round
> 7.82 made planner observations state/perspective-fresh, repaired exact target
> and stack indexing, routed combat summaries through canonical legality/search,
> and added the previously-omitted rank-3 ability-recommendation extractor.
> Round 7.83 introduced Observation v2: categorical canonical-card embeddings,
> symmetric public zones and mana, library/player/permanent state, exact
> attachment/combat mappings, richer stack objects, observer-relative indices,
> and removal of exact/dead v1 duplicates. The online strategy-memory hint was
> also removed from policy input: the optional replacement is deterministic,
> isolated per environment, and disabled during training/evaluation. The
> observation hash is now part of run lineage. Round 7.84 completes the
> pre-training planner audit: observation reads no longer consume RNG,
> face-down cards cannot shape archetype inference, fake exact opponent-hand
> estimates and disabled action recommendations are removed, and the strategic
> metric vector contains only its seven live values. Round 7.85 keeps the same
> Observation v2 hash while repairing combat-lookahead state isolation, stale
> combat participants, Room unlock mask/payment parity, and Ba Sing Se's
> mandatory Earthbend target commitment. The failed July 14 `reward-v7` run is
> diagnostic-only. Round 7.86 keeps the Observation v2 hash but makes public
> combat damage mandatory, separates and snapshots first-strike/regular steps,
> preserves blocked status and end-of-combat participants, emits canonical
> all-target damage events, unifies printed/granted lifelink, opens overflow
> combat actions, and changes the reward lineage to
> `discounted-state-potential-v4` (all turn-limit outcomes pay `-6`). The
> interrupted `round-7.85-reward-v8` run skipped combat damage and is also
> diagnostic-only.
> The subsequent `round-7.86-combat-v4` run proved combat was reachable but
> exposed a terminal-perspective reward bug and a timeout-heavy
> training/evaluation objective. Round 7.87 introduces
> `discounted-state-potential-v5`: opponent-ended results are translated back
> to the learned seat, all timeouts and safety truncations pay `-10`, and
> handler-local action reward is diagnostic-only. Training now uses the
> deterministic, mastery-gated `combat-v2` opponent curriculum (passive
> goldfish, gradual passive/novice/scripted mixtures, then the full scripted
> pool), while evaluation remains a
> fixed 64-game scripted suite arranged as 32 seat-swapped pairs. PPO defaults
> are 2e-4 learning rate,
> 1,024 rollout steps, batch 256, gamma 0.999, lambda 0.98, value coefficient
> 0.25, and five epochs. Observation v2 and its hash are unchanged for that
> historical lineage.
> Round 7.91 introduced `discounted-state-potential-v6` and made `combat-v4`
> that round's fresh-run default: a life lead at the turn limit pays `-8` (all
> other limit outcomes stay `-10`), the convex damage-progress potential weight
> doubles to `0.80`, race/bridge open against epsilon-handicapped opponents
> that anneal to full strength on demonstrated win rate, and goldfish/race/
> bridge play to 20/20/25 turns. Mastery floors count only full-strength
> episodes. Evaluation remained fixed within each run, but Round 7.91 used a
> different training/evaluation seed, schedule, and worker count from Rounds
> 7.89/7.90, so its absolute scores are diagnostic rather than a controlled
> cross-round comparison.
> Round 7.94 replaces the discounted-state-potential family with
> `tempo-graded-potential-v1` after rounds 7.88-7.93 all converged on
> timeout-dominant play: decisive wins earn a bounded speed premium
> (`+10` up to `+14` by unused engine-turn budget), turn-limit stalls grade
> continuously on opponent damage (`-10` up to `-7`, result labels ignored),
> real draws pay `-3`, every step costs `0.005` reward, and the potential
> drops the hand-size term while making the convex damage ramp dominant
> (life-diff `0.10`, board `0.15`, damage weight `1.0`). Shaping remains
> strictly potential-based; resume and canary validation fail closed across
> the contract boundary.
> Round 7.95 introduces `combat-v7`, which halves the scripted handicap
> ratchet step to 0.10: round-7.94-tempo-v1 ping-ponged the scripted
> epsilon between 0.40 (~38% decisive wins, tightens) and 0.20 (~12%,
> relaxes), so the 0.20 step spanned the entire measured skill cliff. The
> novice ramp keeps its 0.25 step, and the `round-7.95` canary doubles the
> training horizon to 2M timesteps so each finer rung can earn its
> 48-episode window.
> Historically, Round 7.89 introduced `combat-v3`. Race mastery
> requires a novice win-rate floor, bridge requires separate novice and
> scripted floors, and passive opponents are absent from bridge. Explicit
> stage deadlines distinguish forced progression from mastery and guarantee
> entry into `full_pool` by approximately 375k timesteps. `combat-v2` remains
> available only to reproduce the earlier schedule.
> At that time, Round 7.92 made `combat-v5` the fresh-run default. Goldfish
> received 25 turns, and the full-pool scripted profile annealed from epsilon
> `0.40` to full strength. Its qualifying scripted outcomes use their own rolling
> window, so the pool's interleaved novice games cannot starve the 24-sample
> ratchet. The pool is 80% scripted and 20% novice; both profiles were 100%
> active full-strength before this new scripted handicap. Round 7.92 also
> adds the enumerated named-canary checks, separates training/evaluation seeds,
> extends strict fidelity to failed continuations and lost-spell recoveries,
> and uses a pair-aware 95% lower-bound qualification gate.
> Stage selection applies on each worker's future reset; activation
> acknowledgements and stale-stage episode counts prevent old in-flight games
> from satisfying the next stage's minimum exposure clock.
> Observation v3 starts a fresh checkpoint lineage for Round 7.89: snow mana
> provenance, available-mana totals, land development, strategic resource
> magnitudes, win-condition viability, and default evaluator history isolation
> now have corrected semantics. Targetable vectors also follow the exact live
> target instruction, and adaptive turn analytics are player-relative.
> **Do not resume any pre-Round 7.89 checkpoint into this Observation v3
> lineage, including the failed `reward-v7`, `round-7.85-reward-v8`, or
> `round-7.86-combat-v4` artifacts.** Start fresh without `--resume`.

> **Current boundary (Rounds 7.97-7.98 / Observation v5).** Observation v4
> added the observer's own decklist and remaining-library composition;
> Observation v5 added producible mana by color. Its schema SHA-256 is
> `cc7d2e002af3338ee1192f3b85cc16d0913f1a4b4ee763b6b9ba7750d6c50a16`.
> Round 7.98 keeps that observation, reward, and `combat-v7` lineage but starts
> a fresh experiment for checkpoint-league attribution; its named canary
> rejects `--resume` and `--matchup-weighting`.

### Hyperparameter optimization

```bash
python main.py --optimize-hp
```

Automatically selects 10, 25, or 50 Optuna trials based on logical CPU count.

### Resuming / continuing a run

```bash
python main.py --resume models/<run>/final_model --timesteps 10000 \
  --curriculum none
```

(Only for manifest-verified, lineage-compatible checkpoints — see the boundary
note above. This command is only valid when the source run also recorded no
curriculum. Curriculum resume is rejected because per-worker matchup counters
are not checkpointed; the named Round 7.98 Observation v5 experiment must
start fresh.)

---

## Harvesting statistics

### Sample-deck fixture harvest

Rotates through the pinned decks, requires a fresh output directory, and rejects
reset fallbacks, degraded/out-of-space observations, mask-valid execution
failures, mask-invalid checkpoint choices, aborts, corrupt compressed data, and
cross-file count mismatches before writing `harvest_run.json` as its success
marker:

```bash
python harvest_fixtures.py --seed 20260710 --output harvest_runs/seed_20260710
```

The default policy is random-valid vs the scripted opponent. **These records
prove execution and telemetry coverage; their win rates are not card- or
deck-strength evidence.** Statistical harvest begins only after a trained
checkpoint beats scripted play.

### Checkpoint qualification, parallel harvest, and promotion

Production Harvest uses isolated worker directories and publishes
`harvest_protocol.json` only after every shard passes the strict fixture
contract. Checkpoints are stamped by filename, size, and SHA-256.

```bash
python harvest_protocol.py qualify --games 64 --workers 4 --seed 21260716 \
  --candidate models/candidate.zip --minimum-score 0.55 \
  --output harvest_runs/qualification_001

python harvest_protocol.py harvest --games 256 --workers 4 \
  --agent-model models/candidate.zip --opponent-model models/champion.zip \
  --output harvest_runs/candidate

python harvest_protocol.py promote --games 64 --workers 4 \
  --candidate models/candidate.zip --baseline models/champion.zip \
  --minimum-score 0.55 --output harvest_runs/promotion_001
```

For the paired qualification and promotion commands, `--games 64` means 64
total games: 32 with the candidate in each physical seat.

Qualification pairs the candidate against the scripted policy from both seats.
It writes an atomic `qualification.json` after both strict legs validate, counts
non-timeout draws as half a point, gives turn-limit life leads zero points, and
passes its current protocol gate at the default 55% point-score threshold with
zero fidelity counters and no `unparsed`/`crash` support entries. A completed
failed gate is recorded for audit and returns a nonzero command status; an
invalid or incomplete protocol never publishes the qualification manifest.
Every persisted game seat and worker-stamped checkpoint identity must agree,
and the checkpoint is re-hashed before publication so a changed candidate fails
closed.
The command exits `0` for a pass, `2` for a valid completed rejection, and `1`
for an invalid or incomplete protocol.

Final qualification is a separate held-out decision: use a seed and case suite
that were not used for training or periodic evaluation, and require the same
pair-aware 95% lower confidence bound to reach 55%. In this round the periodic
evaluator computes and persists that interval; `harvest_protocol.py qualify`
still enforces only its point-score threshold. A Harvest CLI pass is therefore
necessary but not sufficient final promotion evidence until that interval gate
is added to the protocol.

Promotion evaluates the candidate in both seats and requires both the score
threshold and a clean fidelity/severe-support manifest. `--decks`/`--format`/
`--format-dir` select the corpus. The protocol is ready; a real promotion needs
trained candidate and baseline checkpoints.

### Long-game invariant fuzzing

Deterministic profiles: `short` (300 actions), `default` (8,000), `long`
(320,000). A successful run leaves no artifact; a failure writes an atomic JSON
payload with the exact seed, actions, contexts, and state for one-command
replay.

```bash
python tests/invariant_fuzz_test.py --profile long --artifact-dir fuzz_failures
python tests/invariant_fuzz_test.py --replay fuzz_failures/invariant_fuzz_seed_1701.json
```

The long profile also runs weekly / on demand via
`.github/workflows/long-game-fuzz.yml`; failure replays are retained as CI
artifacts for 14 days.

---

## `main.py` command-line arguments

| Flag | Meaning | Default |
|---|---|---|
| `--timesteps` | Total training timesteps | `1000000` |
| `--seed` | Training seed (Python, NumPy, Torch, workers) | `20260715` |
| `--eval-seed` | Independent fixed-evaluation schedule seed | `21260715` |
| `--canary-config` | Validate a named, enumerated experiment contract (current: Round 7.99) | none |
| `--resume` | Path to a lineage-compatible checkpoint to continue | — |
| `--learning-rate` | Initial learning rate | `2e-4` |
| `--batch-size` | Batch size | `256` |
| `--n-steps` | Rollout steps before an update | `1024` |
| `--n-envs` | Parallel training environments (`0` = auto when explicitly supplied) | `8` |
| `--eval-freq` / `--eval-episodes` | Periodic cadence / games in fixed seat-swapped pairs | `100000` / `64` (32 pairs) |
| `--checkpoint-freq` | Permanent recovery-checkpoint cadence (timesteps) | `500000` |
| `--checkpoint-pool-self-play` | Opt in to frozen-checkpoint opponents for training only | off |
| `--checkpoint-pool-snapshot-freq` | Learner cadence for checkpoint-pool refresh | `100000` |
| `--checkpoint-pool-size` | Maximum frozen checkpoints retained on disk | `4` |
| `--checkpoint-pool-probability` | Per-episode resident-checkpoint probability | `0.5` |
| `--curriculum` | Training opponent schedule (`combat-v7`, older reproducibility versions, or `none`) | `combat-v7` |
| `--format` / `--decks` / `--format-dir` | Format legality + corpus / deck dir / frozen namespace | pinned Standard |
| `--optimize-hp` | Run Optuna hyperparameter search | off |
| `--record-network` / `--record-freq` | Record network parameters / cadence | off / `5000` |
| `--run-name` | Short label folded into the run id and TensorBoard run name | none |
| `--cpu-only` | Force CPU even if a GPU is available | off |
| `--debug` | Extra debugging output | off |

---

## Monitoring

```bash
tensorboard --logdir=tensorboard_logs
```

Each training run groups its streams under one folder named
`MMDD-HHMMSS[_label]` (label from `--run-name`), containing `train` (policy
metrics), `system` (resource usage), and `network` (parameter recording when
enabled). The distinct part leads the name so runs stay tellable-apart in
TensorBoard's sidebar even when truncated.

Logged metrics include signed/absolute/nonzero reward components, raw action
and state-potential diagnostics, rollout critic target/value scales, decisive
outcomes, timeouts, valid-action counts, action distributions,
network-parameter changes, and CPU/GPU/
memory usage. All
time-series use policy timesteps as their x-axis. Terminal telemetry is reported
as both transition-normalized `terminal/*` metrics and episode-normalized
`terminal_episode/*`/`outcome/*` metrics. Terminal reward/result sign
mismatches have their own diagnostic counter; decisive and timeout rates are
also split under `opponent_profile/*` and `curriculum_stage/*`.

---

## Architecture notes and honest caveats

- **FixedWindowMTGExtractor** — a custom feature extractor over the heterogeneous
  observation (battlefield, hand, phase, life totals, resources).
  `CompletelyFixedMTGExtractor` remains only as a load-compatibility alias.
- **FixedDimensionMaskableActorCriticPolicy** — applies the legal-action mask so
  the policy can never select an illegal action.
- **Not recurrent.** The extractor's gated block applies an LSTM-shaped transform
  to a length-one input; its parameters train, but no hidden state is carried
  across policy calls. This is not yet a recurrent policy.
- **Curriculum opponent by default.** Training progresses from passive through
  novice to mostly scripted play; fixed evaluation always uses the scripted
  profile. Self-play / league play is gated on first beating scripted play.
- **Heuristic planning is opt-in.** Strategic-planner projections are available in
  the observation, but training does not inject a planner-selected action by
  default, and these features provide no cross-step memory.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE). You may use, modify, and distribute
this software; retain the copyright notice and license, document significant
changes, and attribute the project.

## Acknowledgments

- [Stable-Baselines3](https://github.com/DLR-RM/stable-baselines3)
- [SB3-Contrib](https://github.com/Stable-Baselines-Team/stable-baselines3-contrib)
- [Gymnasium](https://github.com/Farama-Foundation/Gymnasium)
- Card data from [Scryfall](https://scryfall.com/).

## Contact

For questions or contributions, please open an issue on the GitHub repository.
