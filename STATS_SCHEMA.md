# Playersim Stats Schema — Contract for the Deck-Builder AI (v1)

Everything the downstream deck-construction AI needs to consume Playersim output.
All files are written relative to the training process's working directory.

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
```

Tracker/memory files are gzip-compressed JSON (`use_compression=True` default);
uncompressed variants have the same name without `.gz`.

## game_log.jsonl — one JSON object per recorded game

The primary join table. Append-only; each line:

| field           | type   | meaning |
|-----------------|--------|---------|
| `schema_version`| int    | currently `1`; reject records with a higher version |
| `ts`            | float  | unix timestamp at record time |
| `result`        | str    | **agent-relative**: `win`, `loss`, `draw`, `draw_both_loss`, `error`, `invalid_limit` |
| `terminal_reason` | str  | stable cause category such as `life_total`, `decking`, `poison`, `concession`, `turn_limit`, or `alternate_win` |
| `turn_count`    | int    | final turn number |
| `p1_deck` / `p2_deck` | str | deck names as loaded from the deck JSONs |
| `agent_is_p1`   | bool   | which seat the learning agent occupied |
| `agent_version` | str    | run id / checkpoint tag (see caveats) |
| `fidelity`      | object | per-game fidelity counters (below) |

Games with no adjudicated result (aborted simulations, opponent-loop truncations)
are deliberately **not** recorded — absence of a line means the game produced no
trustworthy signal. `harvest_fixtures.py` is stricter: any reset fallback,
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

Draw/opening/play telemetry is sourced directly from GameState as of Round
7.37: `games_drawn` and `draw_performance_by_turn` use completed draws,
`games_in_opening_hand` uses the final post-mulligan hand, and play-turn maps
use the turn on which the card was actually played. Outputs produced before
Round 7.37 have zero or inferred values in these fields and must not be mixed
with current card-performance aggregates.

## Caveats the deck-builder MUST respect

1. **Agent strength**: `result` reflects the *policy that played*, not intrinsic
   card quality. Filter or weight by `agent_version`; discard or down-weight
   games from early/weak checkpoints as training progresses.
2. **Fidelity**: prefer `fidelity`-clean games; exclude `unparsed_cards` names
   from ranking until the engine covers them.
3. **`error` / `invalid_limit` results** are recorded as draws in the tracker
   aggregates; filter them out via `game_log.jsonl` when computing win rates.
4. **Versioning**: bump handling when `schema_version` > 1 appears.

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

## Fixture harvest protocol

`harvest_fixtures.py` is the reproducible plumbing/support check for the audited
sample decks. It requires an empty output directory, sorts deck files before ID
assignment, rotates all eight decks through a seeded schedule, stamps one agent
version, accepts only completed `win`/`loss`/`draw`/`draw_both_loss` records, and
cross-checks the game log, cumulative fidelity totals, tracker aggregates, card
memory, support manifest, and scheduled deck labels. `harvest_run.json` is
written only after those checks pass.

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
