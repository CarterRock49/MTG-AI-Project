# Playersim Stats Workbench

This is the local, dependency-free viewer for training manifests, evaluation
history, full evaluation-game records, DeckStats aggregates, CardMemory,
safe StrategyMemory diagnostics, authoritative game logs, fidelity/support
reports, and Harvest artifacts.

Start it from the project root:

```powershell
.\MTGenv\Scripts\python.exe .\DeckStats_Viewer\MTG_Statistics_Viewer.py
```

It opens `http://127.0.0.1:8050/` in the default browser. Useful options:

```powershell
# Serve without opening a browser
.\MTGenv\Scripts\python.exe .\DeckStats_Viewer\MTG_Statistics_Viewer.py --no-browser

# Inspect discovery counts without starting the server
.\MTGenv\Scripts\python.exe .\DeckStats_Viewer\MTG_Statistics_Viewer.py --check

# Point at another Playersim workspace or choose a different port
.\MTGenv\Scripts\python.exe .\DeckStats_Viewer\MTG_Statistics_Viewer.py `
  --root C:\path\to\Playersim --port 8060
```

The workbench automatically discovers:

- `models/<run>/training_run.json`
- `logs/<run>/evaluation/evaluations.json`
- `logs/<run>/environment_data/{train,eval}/env_*/deck_stats/`
- each scope's adjacent `card_memory/all_cards.json(.gz)`
- safe `card_memory/strategy_memory.json(.gz)` diagnostics; legacy
  `strategy_memory.pkl` is reported as opaque metadata and is never opened
- the root `deck_stats/` compatibility scope
- fixture and parallel Harvest manifests/shards
- promotion decisions

Everything is shown in one scrolling workspace. Select a run to inspect its
configuration, runtime, lineage, checkpoint trend, all evaluation cases, paired
seat mate, terminal record, replay/trace (when available), and raw JSON. Select
a statistics scope independently to inspect every deck/card aggregate, its exact
canonical-ID CardMemory match, meta, fidelity, support manifest, and paginated
`game_log.jsonl` row. DeckStats and CardMemory counters remain visibly separate.
Memory-only and aggregate-only IDs are retained and diagnosed; records are never
joined by card name.

The CardMemory panel reports its file, schema, update time, card and ambiguous-name
counts, exact-ID join health, counter mismatches, and complete raw envelope. Its
decision-use badge comes only from run-manifest provenance: `recorded analytics`
means the environment recorded outcomes but adaptive history was explicitly off;
`adaptive evaluator input` means the manifest explicitly enabled it; legacy or
standalone scopes remain `unknown`. Each worker is an independent scope, and the
viewer never silently merges worker memories. CardMemory is a latest cumulative
snapshot, so it must not be treated as the exact memory state for an older game
or checkpoint. Supported v1/v2 envelopes are structurally and numerically
validated before their entries are interpreted. Unknown schemas are displayed
as raw JSON only; malformed supported envelopes are also raw-only until repaired.
Neither can flow through current counter/rating semantics.

The StrategyMemory panel reads only the versioned
`playersim.strategy_memory.diagnostics` JSON export. It shows evidence counts,
evidence-weighted shaped rewards, truncation, top patterns/actions, and raw JSON.
Its `positive_reward_rate` is the fraction of shaped transition rewards greater
than zero; it is **not a game win rate**. A coexisting runtime pickle is expected
and is listed as not opened. The viewer hashes those opaque bytes without
deserializing them and requires the JSON's `source_pickle.size_bytes` and
`source_pickle.sha256` marker to match before interpreting the diagnostics as
current. A missing marker, missing pickle, or byte mismatch is raw-only and
marked for review. A pickle without the safe JSON export is shown as a
diagnostic gap rather than deserialized.
Only `playersim.strategy_memory.diagnostics` v1 is interpreted. Unknown
kind/schema combinations and malformed v1 counts, rates, rankings, or truncation
metadata are raw-only and marked for review rather than `clean`.

Historical successful evaluation artifacts only contain terminal summaries;
the UI labels these games **summary only**. Evaluations produced after the trace
contract was added carry deterministic replay/debug data in verified gzip
sidecars. The table loads only the selected game's sidecar, keeping large run
histories responsive. Before parsing, the viewer checks the compressed byte size
and SHA-256 recorded by the episode. Missing, corrupt, size-mismatched, or
hash-mismatched sidecars become visible structured artifact errors and are never
returned as path strings pretending to be debug data. Selection carries a stable
record ID and checkpoint SHA; an ambiguous timestep/case request is rejected.
An episode joins an authoritative game-log row only when every identity field
present on both sides agrees. A checkpoint conflict can never fall through to a
relaxed seed/deck/seat match and is retained as a structured join diagnostic.
Failure replays remain separate artifacts and are not misrepresented as
successful-game traces. Trace, replay, and terminal-only debug availability are
reported and filtered independently.

For a traced evaluation, **Watch replay** opens a full-screen Arena-style
battlefield driven exclusively by the verified `trace` pre/post snapshots. The
learned seat is placed at the bottom by default; perspective and the normally
concealed top hand can be toggled. The theater renders both players' life,
resources, hands, libraries, graveyards, exile, battlefield, tap state,
permanent counters, marked damage, stack, priority, turn, and phase. Newer
captures also identify the active player and combat attackers/blockers, and
carry immutable mana cost, rules text, and printed stats for card labels and
hover details. Playback supports play/pause, single-action stepping, scrubbing,
speed selection, an action feed, and Space/arrow/Home/End keyboard controls.
Autoplay coalesces routine priority-only passes while every retained action
remains reachable by stepping or scrubbing.

The visual player never substitutes the learned-policy `replay.actions` stream
for a full match trace: that stream omits opponent decisions and intermediate
states. Replay-only, terminal-only, truncated, and historical records remain
explicitly labeled, and missing snapshots are either tied to the next exact
capture or visibly held at the last exact state rather than reconstructed.

When a new sidecar contains EnhancedCardEvaluator diagnostics, the selected game
shows terminal totals and every evaluator event attached to an atomic action:
card identity, context/perspective, score components, history source/evidence,
adjustments, fallbacks, exceptions, cache counts, deduplication, drops, and raw
records. This section is deliberately labeled non-causal: evaluator calls can
feed observations or automatic subchoices and do not prove why PPO selected an
action. Evaluator records are paged in bounded groups, and per-event, terminal,
and complete-sidecar JSON is materialized only when its drawer is opened. This
keeps maximum-budget debug games responsive without hiding any persisted field.
The action timeline is likewise paged, with each complete action/context record
materialized lazily. Its complete raw drawer always retains both the trace and
replay payloads, even when the summarized action list is available.

The server binds to localhost by default, has no CDN or external network calls,
and accepts artifact IDs from its own catalog rather than arbitrary file paths.
It uses only the Python standard library; there is no Dash, Flask, React, npm,
or frontend build step.
