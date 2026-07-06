# Playersim Stats Schema — Contract for the Deck-Builder AI (v1)

Everything the downstream deck-construction AI needs to consume Playersim output.
All files are written relative to the training process's working directory.

## Directory layout

```
deck_stats/
  game_log.jsonl          # authoritative per-game records (this contract's core)
  fidelity_report.json    # cumulative simulation-fidelity summary
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
| `turn_count`    | int    | final turn number |
| `p1_deck` / `p2_deck` | str | deck names as loaded from the deck JSONs |
| `agent_is_p1`   | bool   | which seat the learning agent occupied |
| `agent_version` | str    | run id / checkpoint tag (see caveats) |
| `fidelity`      | object | per-game fidelity counters (below) |

Games with no adjudicated result (aborted simulations, opponent-loop truncations)
are deliberately **not** recorded — absence of a line means the game produced no
trustworthy signal.

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

## Caveats the deck-builder MUST respect

1. **Agent strength**: `result` reflects the *policy that played*, not intrinsic
   card quality. Filter or weight by `agent_version`; discard or down-weight
   games from early/weak checkpoints as training progresses.
2. **Fidelity**: prefer `fidelity`-clean games; exclude `unparsed_cards` names
   from ranking until the engine covers them.
3. **`error` / `invalid_limit` results** are recorded as draws in the tracker
   aggregates; filter them out via `game_log.jsonl` when computing win rates.
4. **Versioning**: bump handling when `schema_version` > 1 appears.
