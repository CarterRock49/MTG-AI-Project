# Playersim Stats Schema — Contract for the Deck-Builder AI (v2)

Everything the downstream deck-construction AI needs to consume Playersim output.
All files are written relative to the training process's working directory.

The append-only game-log contract is schema version `2`. Tracker aggregate
files use an independent `STATS_VERSION`, currently `3.2.0`.

## Directory layout

```
deck_stats/
  game_log.jsonl          # authoritative per-game records (this contract's core)
  fidelity_report.json    # cumulative simulation-fidelity summary
  harvest_run.json        # fixture-harvest-only success marker and seeded schedule
  decks/*.gz              # DeckStatsTracker aggregates, one gzip-JSON per deck
  cards/*.gz              # DeckStatsTracker aggregates, one gzip-JSON per card
  meta/*.gz               # tracker metadata (archetype fingerprints, mappings)
card_memory/
  all_cards.json.gz       # CardMemory: per-card lifetime performance records
  strategy_memory.pkl     # Optional runtime-only StrategyMemory store
  strategy_memory.json.gz # Safe, bounded StrategyMemory viewer diagnostics
```

Tracker/memory files are gzip-compressed JSON (`use_compression=True` default);
uncompressed variants have the same name without `.gz`.

## game_log.jsonl — one JSON object per recorded game

The primary join table. Append-only; each line:

| field           | type   | meaning |
|-----------------|--------|---------|
| `schema_version`| int    | currently `2`; v1 remains readable, reject higher versions |
| `ts`            | float  | unix timestamp at record time |
| `result`        | str    | **agent-relative**: `win`, `loss`, `draw`, `draw_both_loss`, `error`, `invalid_limit` |
| `terminal_reason` | str  | stable cause category such as `life_total`, `decking`, `poison`, `concession`, `turn_limit`, or `alternate_win` |
| `turn_count`    | int    | final turn number |
| `p1_deck` / `p2_deck` | str | deck names as loaded from the deck JSONs |
| `agent_is_p1`   | bool   | which seat the learning agent occupied |
| `agent_version` | str    | run id / checkpoint tag (see caveats) |
| `curriculum_stage` / `curriculum_stage_index` | str/null, int/null | training curriculum stage; null for fixed evaluation and non-curriculum games |
| `opponent_profile` | str | `passive`, `novice`, or `scripted` |
| `opponent_handicap` | float | pass-probability the annealed opponent played under; `0.0` means full strength (always `0.0` for fixed evaluation) |
| `max_turns` | int | the turn limit this game was played under (curriculum stages may shorten it below the engine default) |
| `agent_deck` / `opponent_deck` | str | semantic deck roles independent of physical P1/P2 seat |
| `matchup_episode_index` | int/null | deterministic stage/schedule episode index |
| `fidelity`      | object | per-game fidelity counters (below) |

Safety terminations are recorded with an `error_*`/`invalid_*` result and
stable terminal reason for diagnosis, but are never strength evidence.
`harvest_fixtures.py` is stricter: any reset fallback,
degraded/out-of-space observation, mask-valid execution failure, mask-invalid
checkpoint choice, error, repeated wait state, or step-cap abort fails the run
and therefore never writes its `harvest_run.json` success marker.

### fidelity object

| key | meaning |
|-----|---------|
| `unimplemented_action` | count of actions routed to the unimplemented handler |
| `unimplemented_action_types` | sorted list of the action-type names involved |
| `unparsed_mana` / `unparsed_modal` / `unparsed_effects` | oracle-text clauses the parser failed on |
| `unparsed_cards` | sorted list of card names whose text failed to parse this game |

**Weighting guidance:** treat a game with nonzero counters as lower-confidence;
treat per-card stats for any name appearing in `unparsed_cards` as unreliable
regardless of sample size.

## fidelity_report.json — cumulative summary

`games_recorded`, cumulative counter totals, `unparsed_cards` as
`{card_name: games_affected}`, `agent_version`, `generated_at`. Rewritten
atomically after every recorded game and on `env.close()`. The
`{card_name: count}` map, sorted by count, is the engine's card-coverage
work queue.

## Tracker aggregates (deck_stats/decks, /cards)

Validated fields per deck record: `name`, `card_list`, `archetype`, `games`,
`wins`, `losses`, `draws` (invariant: wins+losses+draws == games),
`avg_game_length`, plus per-stage breakdowns. Card records carry `games`,
`wins`, `losses`, `draws`, `win_rate`, `usage_count`. Deck identity is a
fingerprint of the card list; name mappings live under `meta/`.

Tracker metadata written by the current engine carries version `3.2.0`.
`meta.total_games` is a match count, while card/archetype `games` values count
deck-seat appearances. Popularity fields therefore use deck-seat share:

```
play_rate or meta_share = appearances / (2 * meta.total_games)
```

This is the probability that a randomly selected deck seat contains that card
or belongs to that archetype, and must remain in `[0, 1]`. Outputs produced by
older tracker versions divided appearances by matches and can exceed 1; reload
them through the current tracker/viewer normalization or rebuild them from the
game log before using prevalence in deck-builder features.

Draw/opening/play telemetry is sourced directly from GameState as of Round
7.37: `games_drawn` and `draw_performance_by_turn` use completed draws,
`games_in_opening_hand` uses the final post-mulligan hand, and play-turn maps
use the turn on which the card was actually played. Outputs produced before
Round 7.37 have zero or inferred values in these fields and must not be mixed
with current card-performance aggregates.

## CardMemory and StrategyMemory diagnostics

`card_memory/all_cards.json.gz` is a versioned, cumulative snapshot owned by
one environment worker. Its `cards` mapping is keyed by canonical card ID and
records W/L/D totals, drawn/played/opening-hand counts, player-relative play
turns, curve buckets, archetype splits, synergy-partner counters,
effectiveness, a bounded recent outcome trend, and optional meta position.
Consumers must join it to tracker aggregates by canonical ID, never by card
name. Worker snapshots and run lineages remain separate; the snapshot is not a
checkpoint-time reconstruction of what an evaluator knew during an older game.

CardMemory is recorded in normal production runs even when
`adaptive_decision_history_enabled` is false. A viewer must distinguish
"recorded analytics" from "used as a decision input" and preserve the
independent DeckStats and CardMemory persistence/freshness indicators. The
DeckStats Viewer derives the provenance labels `adaptive_input`,
`recorded_only`, or `unknown` from the selected run manifest; the presence of a
memory file alone is not evidence that the policy consumed it. Unsupported or
malformed CardMemory contracts are exposed only as raw diagnostics and must
not be interpreted or joined.

StrategyMemory is optional and disabled for standard training/evaluation. Its
runtime source of truth remains `strategy_memory.pkl`; consumers and web
viewers must treat that file as opaque and never unpickle it. Every successful
enabled-memory save makes a best-effort, deterministic, atomic gzip-JSON export
to adjacent `strategy_memory.json.gz` with:

- `kind: playersim.strategy_memory.diagnostics`, diagnostics schema version 1,
  and the source memory schema version;
- `source_pickle.size_bytes` and `source_pickle.sha256`, identifying the exact
  raw pickle generation summarized by the export without deserializing it;
- logical update and pattern/action/action-sequence counts;
- evidence-weighted mean shaped reward and positive-shaped-reward rate;
- fixed limits, explicit truncation metadata, and at most 100 top-pattern and
  250 top-action records.

The positive-reward rate means the fraction of recorded learned-agent shaped
transition rewards greater than zero. It is not a game win rate. The safe JSON
is diagnostics only and never participates in runtime action selection.
Exported patterns contain at most 64 fields; every value must be finite and
within `[-1e6, 1e6]`, and action IDs must be integers in `[0, 4095]`. A safe
export failure does not fail the runtime pickle save, but it does invalidate
any older adjacent safe JSON so a viewer cannot mistake stale diagnostics for
the just-saved memory. A viewer must also compare both `source_pickle` fields
with the opaque file's raw bytes; a mismatch catches a process interruption
between the pickle and JSON atomic replacements and makes the diagnostics
stale/raw-only. Viewers interpret only the supported diagnostics kind and
schema version 1; unsupported or malformed JSON remains available raw-only.

## Caveats the deck-builder MUST respect

1. **Agent strength**: `result` reflects the *policy that played*, not intrinsic
   card quality. Filter or weight by `agent_version`; discard or down-weight
   games from early/weak checkpoints as training progresses.
2. **Fidelity**: prefer `fidelity`-clean games; exclude `unparsed_cards` names
   from ranking until the engine covers them.
3. **`error` / `invalid_limit` results** are recorded as draws in the tracker
   aggregates; filter them out via `game_log.jsonl` when computing win rates.
4. **Versioning**: v1 rows predate curriculum/matchup fields; treat those fields
   as null/`scripted`. Bump handling when `schema_version > 2` appears. Tracker
   consumers must separately recognize aggregate version
   `3.2.0`; `last_updated` is optional and may be null.

---

## card_support_manifest.json (added July 2026)

Written to the same directory as the deck statistics, merged (never
clobbered) on every persist, accumulating across games and process restarts.

```json
{
  "<card name>": {
    "reasons": {"unparsed clause: <text>": 3, "...": 1},
    "severity": "crash" | "unparsed" | "partial",
    "count": 4,
    "first_seen": "2026-07-07",
    "last_seen": "2026-07-07"
  }
}
```

Severity semantics (worst sticks per card):
- `crash`   — handling the card raised an exception.
- `unparsed`— an entire effect produced nothing the engine can run.
- `partial` — some clauses parsed; at least one fell back to a no-op.

**Deck-builder contract:** exclude `crash` and `unparsed` cards from
candidate pools entirely; treat statistics for `partial` cards as
lower-confidence (their recorded value is a floor, since some of their text
did nothing). Re-include cards when their entries stop accumulating after an
engine update (compare `last_seen` against the engine/agent version of the
current harvest run).

Loader: `Playersim.card_support.CardSupportManifest.load(directory)`.

### Static format support ledger (Round 7.50)

`formats/<format>/support_ledger.json` is the versioned, self-hashed preflight
companion to the runtime manifest. Generate it with:

```bash
python -m Playersim.support_preflight \
  --snapshot "Format Card Lists/standard.jsonl" \
  --registry formats/standard/card_registry.json \
  --decks formats/standard/metagame_corpus_2026-07-11.json \
  --corpus-label representative-meta-2026-07-11 \
  --overrides formats/standard/support_overrides.json \
  --format standard --output formats/standard/support_ledger.json
```

Every legal pool card is constructed, each face is sent through ability and
replacement registration, and runnable effect text is probed through the
shared effect factory. Card statuses are `verified` (explicit scenario-backed
override), `observed_clean` (configured corpus plus clean static preflight),
`unseen`, `partial`, `unparsed`, `crash`, or `excluded`. Static-clean is not a
rules proof: the builder may qualify only `verified` and `observed_clean`
cards, and should exclude `unparsed`, `crash`, and `excluded` cards.

`ranked_mechanics` and `ranked_cards` sort gaps first by copies in the
configured deck corpus, then by pool prevalence/severity. The ledger records
the pool, registry, override, and corpus identities needed to interpret that
ranking. The pinned representative corpus records its capture date, source
URLs, archetype shares, and exact 60-card lists.

## Format namespaces and run lineage (added July 2026)

A frozen format namespace under `formats/<format>/` pins two versioned,
self-hashed JSON artifacts. Freeze a deck-only namespace with
`python -m Playersim.card_registry freeze --decks <corpus> --format <format>
--output formats/<format>`, or cover the complete pinned pool while preserving
existing indices with `python -m Playersim.card_registry freeze-pool
--snapshot "Format Card Lists/<format>.jsonl" --decks <corpus>
--format <format> --output formats/<format>`:

- `card_registry.json` — canonical card identities. Each card keeps one
  stable integer index (used as the engine `card_id`) plus its Scryfall
  `oracle_id`. Extension is append-only: adding cards never renumbers
  existing entries. Canonical indices are name-sorted at first freeze and
  **differ from legacy insertion-order IDs**, so a format-namespace run is a
  new stats lineage.
- `feature_schema.json` — the frozen card feature-vector layout and
  `feature_dim`. Loading a corpus under it keeps model input width fixed; a
  card outside the frozen subtype vocabulary fails the load loudly.
- `Playersim/observation_schema.py` — the global, self-hashed policy-input
  contract. It versions field meanings, perspective, semantic identity
  encoding, extractor routing, additions, and removals. The human-readable
  inventory is `OBSERVATION_SCHEMA.md`.

Every run-level manifest (`training_run.json`, `harvest_run.json`,
`harvest_protocol.json`, `promotion.json`) now carries a `lineage` object:

| key | meaning |
|---|---|
| `format` | format name, or null for format-free legacy runs |
| `pool_snapshot` | `Format Card Lists/<format>.jsonl` identity (path, size, sha256), or null |
| `corpus` | deck-corpus directory name, per-file sha256 list, and one aggregate sha256 |
| `card_registry` | registry schema_version, card count, sha256, or null |
| `feature_schema` | schema_version, feature_dim, sha256, or null |
| `observation_schema` | global policy-observation kind, schema_version, and sha256 |

**Consumer rule:** never merge statistics whose `lineage.format`,
`lineage.card_registry.sha256`, `lineage.feature_schema.sha256`, or
`lineage.observation_schema.sha256` differ — they may disagree on card
identity or on what the policy observed. A run with `lineage: null` (or
missing) predates this contract; a lineage without `observation_schema`
predates Observation v2 and is checkpoint/statistics-incompatible with it.

## Fixed evaluation history and game debugging

Periodic evaluation history is written to
`logs/<run>/evaluation/evaluations.json`. Schema version 3 retains the immutable
schedule, checkpoint identity, qualification rule, every checkpoint summary,
and every individual case. Each `episodes[]` record includes its case index,
requested and resolved case, raw and canonical result, terminal reason, reward,
length, timeout/decisive flags, and (for newly generated evaluations) a
`debug_path` reference plus `debug_sha256`, compressed size, trace-event count,
and replay-action count.

The referenced debug schema version 1 payload is captured only in an environment
stamped with both an evaluation timestep and checkpoint SHA-256. Ordinary
training episodes pay none of its state-snapshot cost. The callback writes each
payload atomically as deterministic gzip JSON at
`logs/<run>/evaluation/games/<timestep>/case_NNN.json.gz`; the compact history is
not published if any required sidecar fails. A payload contains:

- `replay`: deterministic replay version 3, including the seed, physical deck
  seats, learned-policy seat/profile metadata, and every learned action/context.
  Extra human-readable action fields are ignored by the replay reader. Capture
  retains at most 4,096 events and 2 MiB of compact JSON, with a 64 KiB limit
  per entry.
- `trace`: one contiguous sequence of both learned and opponent atomic actions.
  Each event records sequence number, actor/seat, action index/type/parameter,
  concise label, selected context, and compact pre/post states. State snapshots
  include turn, phase, priority, life, poison, public and omniscient changing
  zones, stack summary, and library counts. Learned actions additionally carry
  the resulting reward components and diagnostics. Trace capture retains at
  most 8,192 events and 8 MiB of compact JSON, with a 512 KiB limit per entry.
  Newly captured events may also carry `evaluator` schema version 1: a bounded,
  explicitly non-causal window of EnhancedCardEvaluator activity observed
  before/during that atomic action. Each evaluator record preserves
  runtime/canonical card identity, context and perspective,
  base/context/history/DeckStats components, history source and evidence,
  multipliers, pre-clamp/final scores, repeat counts, and
  invalid/fallback/exception flags. Evaluator capture retains at most 256
  records per action window, 4,096 records per game, 64 KiB per record, and
  2 MiB of emitted compact JSON per game.
- `terminal`: result/reason, final reward, reward contract/components, policy
  diagnostic, fidelity counters, and final compact state. The debug root also
  summarizes per-game evaluator calls, captured/deduplicated/dropped events,
  fallbacks/exceptions, event budget, and cache-hit/miss deltas, plus any final
  unattached evaluator window.

The complete debug payload is limited to 12 MiB. Replay, trace, evaluator, and
terminal values pass through depth, node, container, key, and string bounds;
oversized or unserializable values cannot change the game result. Each bounded
section publishes limits, byte/event counts, omissions or drops, and capture
errors so consumers can distinguish complete diagnostics from degraded
telemetry.

The terminal case remains the strength-evidence row; replay and trace are debug
artifacts and must not alter qualification scoring. Histories written before
this contract contain complete terminal episode summaries but no successful-game
action trace. Consumers must report these as `trace unavailable` and must never
fabricate a timeline from the terminal row. Failure-only `failure_replay.json`
artifacts remain valid but are not successful evaluation traces.

Evaluator activity is explanatory telemetry, not proof that the PPO policy
selected an action because of a heuristic score. It can describe observation
features, legality/search helpers, or automatic subchoices. Historical
sidecars written before evaluator diagnostics cannot reconstruct those values,
and a viewer must not recompute them with newer evaluator code.

The dependency-free viewer in `DeckStats_Viewer/` joins evaluation episodes to
their authoritative evaluation `game_log.jsonl` records using run, evaluation
timestep, checkpoint SHA-256, and case/matchup index. It preserves each raw
episode alongside normalized seat, pair, and semantic deck fields. Game lists
remain summary-only over HTTP. A stable record ID plus checkpoint SHA-256
disambiguates repeated cases, and ambiguous legacy selections are rejected.
The selected sidecar is loaded on demand only after its compressed size and
SHA-256 match the evaluation-history reference. Replay, action-trace, and
terminal-debug availability are reported independently. Unsupported or
malformed CardMemory/StrategyMemory is raw-only, while large evaluator event
lists and raw payloads use paginated or lazy detail rendering so the complete
captured information remains inspectable without blocking initial page load.

## Fixture harvest protocol

`harvest_fixtures.py` is the reproducible plumbing/support check for the audited
sample decks. It requires an empty output directory, sorts deck files before ID
assignment, rotates all eight decks through a seeded schedule, stamps one agent
version, accepts only completed `win`/`loss`/`draw`/`draw_both_loss` records, and
cross-checks the game log, cumulative fidelity totals, tracker aggregates, card
memory, support manifest, and scheduled deck labels. `harvest_run.json` is
written only after those checks pass.

The same runner also harvests any other strictly loaded corpus:
`--decks <dir>` selects the corpus (decks ordered by name for the seeded
schedule), `--format <format>` additionally enforces strict format legality
and applies the frozen `formats/<format>` registry and feature schema
(`--format-dir` overrides the namespace location). The no-argument fixture
form remains the regression gate.

The fixture policy is random-valid against the scripted opponent. These records
prove execution and telemetry coverage; they are not suitable for card/deck
strength estimates. Statistical harvest begins only after a trained checkpoint
beats scripted play and the schedule is promoted to checkpoint/self-play.

## Production parallel harvest

`harvest_protocol.py harvest` partitions one deterministic global game schedule
into isolated `shard_NNN/` directories. Each shard must independently satisfy
the fixture contract above. The root publishes files only after every shard
succeeds:

- `harvest_protocol.json` — success marker and aggregate run metadata.
- `card_support_manifest.json` — count/severity/reason merge across shards.
- `shard_NNN/harvest_run.json` and normal stats artifacts — auditable source
  records; shard tracker databases are never concurrently shared.

`harvest_protocol.json` schema version 1 fields:

| key | meaning |
|---|---|
| `status` | Always `complete`; absence means the run is incomplete/invalid. |
| `protocol_version` | Harvest orchestrator behavior version. |
| `seed`, `games`, `workers`, `max_steps` | Deterministic schedule and safety-cap inputs. |
| `elapsed_seconds`, `games_per_second` | Whole-run wall-clock throughput. |
| `agent_policy`, `opponent_policy` | Policy identity. A checkpoint identity includes filename, byte size, and SHA-256; non-checkpoint fixtures use `kind`. |
| `lineage` | Corpus/format lineage object (see "Format namespaces and run lineage"). All shards must agree or the run fails before publishing. |
| `decks` | Ordered deck names in the harvested corpus. |
| `results` | Aggregate completed result counts. |
| `fidelity` | Sum of all fidelity counters across shards. |
| `manifest_entries` | Number of merged card-support entries. |
| `shards` | Ordered shard number, global game offset/count, directory, agent version, and result counts. |

Use the root manifest for run-level filtering and the append-only shard game
logs for individual outcomes. Never merge shard aggregate gzip files by adding
already-cumulative snapshots; consume each shard as its own stats scope or
rebuild downstream aggregates from `game_log.jsonl`.

## Checkpoint promotion decision

`harvest_protocol.py promote` evaluates a candidate against a baseline in two
equal seeded halves, swapping the candidate between P1 and P2. It writes
`promotion.json` only after both parallel harvests validate. Draws score 0.5.
A candidate is promoted only when its score reaches `minimum_score`, every
fidelity counter is zero, and the merged support manifest has no `unparsed` or
`crash` card.

Important `promotion.json` fields are the SHA-256 candidate/baseline identities,
`games`, `games_per_seat`, `candidate_points`, `candidate_score`,
`minimum_score`, merged `fidelity`, `severe_manifest_cards`, and the final
boolean `promote` / string `decision`. The protocol records the decision; model
file copying or champion alias updates are deliberately left to the caller.
