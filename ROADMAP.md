# Playersim — Roadmap (rescoped July 2026)

**Mission:** train an AI to play Magic in order to harvest card- and deck-performance
statistics, which feed a downstream deck-construction AI that searches for the best
deck per format. Two-player only. Multiplayer, Commander, and match-play tiers from
the earlier draft are dropped.

Everything below is prioritized by one question: *does this make the statistics more
trustworthy for the deck-builder?*

---

## Tier 0 — Stats pipeline integrity ✅ (mostly done, July 2026)

Fixed in the latest pass — listed so the remaining items make sense:
- Game results now actually record: `ensure_game_result_recorded` is wired into
  `step()` on every ending (it previously was never called — the pipeline was empty).
- Turn-limit games (the majority at `max_turns=20`) record their life-adjudicated
  result instead of being silently dropped.
- Stats persist to disk immediately after each recorded game and on `env.close()`,
  so crashes can't lose data. `DeckStatsTracker` now receives the card database.
- Fidelity telemetry (`gs.fidelity_counters`) counts unimplemented actions and
  unparsed mana/modal/effect text, and is exposed in the final `info` dict.
- Guarded by a permanent test stage ("stats pipeline records and persists games").

**Completed (July 2026):** all four remaining items are done. Every recorded game
appends a schema-versioned record to `deck_stats/game_log.jsonl` with per-game
fidelity and the agent version (stamped via `env_method("set_agent_version", ...)`
in main.py); `deck_stats/fidelity_report.json` accumulates per-card unparseable
counts (the card-coverage work queue); and `STATS_SCHEMA.md` documents the full
on-disk contract for the deck-builder, caveats included. Guarded by the
"stats pipeline records and persists games" test stage.

## Tier 1 — Rules correctness that most distorts statistics

Same engine gaps as before, re-justified: each one systematically mis-scores whole
card categories, which poisons the deck-builder's inputs.

1. **Delayed triggered abilities (CR 603.7)** — ✅ core delivered (July 2026).
   `gs.register_delayed_trigger(effect, phase=...)` with firing hooks at every
   phase entry and at state-based checks (asap class); triggers fire exactly
   once and expire. The two pre-existing producers (damage redirection,
   deferred lifelink gain) were crashing on the missing `delayed_triggers`
   slot and now work. Clones start with an empty registry (closures reference
   the original state; documented v1 limitation for MCTS).
   **Oracle-text wiring ✅ (July 2026):** `EffectFactory.create_effects` now
   carves out "at the beginning of the next <phase>" sentences BEFORE clause
   splitting (the comma split used to sever the timing phrase from its
   effect) and emits `DelayedTriggerEffect` objects. Applying one registers
   the inner effect with the registry instead of executing it — covering all
   resolution paths (spells, abilities, modal modes) through the single
   factory hook. Both templating forms parse: leading ("At the beginning of
   the next end step, you gain 2 life.") and trailing ("Exile it at the
   beginning of the next end step.", unearth-style). Simple pronoun riders
   (exile/sacrifice/destroy/return "it") bind to the single explicit target
   if present, else the source card, and no-op safely if the object has left
   the battlefield; other inner clauses re-enter the factory at fire time.
   Recurring wordings ("at the beginning of your upkeep / each end step")
   deliberately do NOT match — those are permanents' triggered abilities.
   Six scenarios now guard 603.7. **v1 limitations (documented):** a pronoun
   referring to an object created by an earlier sentence in the same
   resolution (token-maker riders) mis-binds to the source and no-ops;
   unmapped phases and failed inner effects increment `unparsed_effects`
   fidelity telemetry.
   Bonus bug found by the new scenarios: Card objects are shared across games
   via card_db, and counters written onto them leaked into later games —
   every game after the first started with the previous game's +1/+1 counters.
   Fixed by clearing transient card state at game start.
2. **Layer dependency ordering (CR 613.8)** — `layer_system.py` admits dependency
   sorting is inactive; static-effect cards mis-evaluate in interaction-heavy decks.
3. **Intervening "if" (603.4) / reflexive triggers (603.12)** — absent; affected
   cards fire wrongly or not at all.
4. **Trigger ordering (603.3b)** as an agent choice — engine-default ordering hides
   real card value in trigger-dense decks.
5. **Copy fidelity (CR 707)** and **cost-modification ordering (601.2f)** — copy
   decks and cost-reduction decks get skewed stats.
6. **Combat damage edge math** — deathtouch/trample/first-strike interactions;
   combat is where most game value moves, so errors here touch everything.
7. Triage the 82 self-admitted `simplified`/`placeholder` sites, densest in
   `ability_types.py`, `ability_handler.py`, `layer_system.py`.

## Tier 2 — Card coverage, driven by telemetry

- The fidelity counters (Tier 0) turn real training games into a ranked list of the
  most-played unparseable cards. Fix coverage in that order — never alphabetically.
- Add a **per-card override registry** (card name → hand-written effect) consulted
  before the text parser, for cards regex can't express.
- Scope pools per target format (matching the deck-builder's formats). "100% of pool
  X simulates faithfully" is the milestone that makes format-level deck search valid.

## Tier 3 — Training & environment quality (stats are only as good as the play)

1. **Opponent policy**: stats gathered against a scripted/random opponent measure
   "value vs. bad play." Route the opponent through the trained policy (self-play)
   as early as feasible; consider a league of past checkpoints.
2. **Hidden-information audit**: verify observations never leak opponent hand,
   library order, or face-down identities (`estimated_opponent_hand` exists — audit
   the rest), or the learned values won't transfer to real play.
3. **Choice exposure**: rules choices the engine auto-resolves (trigger order, damage
   assignment) hide card value; surface them as actions over time.
4. **Replay logs**: seeded resets exist; add full action-log replays so any stat
   anomaly can be reproduced and inspected.
5. **Deck legality validation** at load time per target format (copy limits,
   banlists), so the deck-builder searches only legal space. Sideboards/Bo3 are
   optional later — only if the target formats are best-of-three.

## Tier 4 — Verification

1. **Golden scenario harness** ✅ (delivered July 2026): `tests/scenario_test.py`,
   12 scenarios tagged by CR section, all passing. Supports `known_bug=True`
   (XFAIL/XPASS) so failing scenarios can be committed ahead of their fixes.
   Its first run exposed and led to fixes for ten deep engine bugs, including
   the two most consequential of the whole project: state-based actions never
   applied to cards (int ids failed an isinstance-str check — creatures never
   died to damage), and every spell resolution crashed at target validation
   and silently deleted the card from the game. Also fixed: SBA re-entrancy
   wiping counters, annihilation double-implementation, the layer engine
   skipping all computation when no effects were registered (counters never
   changed P/T), a stale layer cache, stdlib `copy` shadowed by numpy in two
   files, a nonexistent-method crash on every cleanup step, the legend rule
   crashing on an unhashable key, and non-reproducible seeded resets.
   Grow this suite with every rules fix: write the scenario first.
2. **Property tests**: zone-count conservation per action, SBA idempotence, action
   mask never permits an illegal action (fuzz), mana pools empty at phase ends.
3. **Long-game fuzzing** across many seeds with invariant checks.

---

## Suggested order of attack

1. Finish Tier 0 (items 1–4): the schema + fidelity-weighting work is small and
   makes every game played from now on more valuable.
2. ~~Tier 4's scenario harness next~~ — delivered; extend it with each Tier 1 fix.
3. Tier 1 items 1–3 (delayed triggers, layer dependencies, intervening-if): the
   deepest systematic stat distortions.
4. Run telemetry-driven Tier 2 coverage continuously alongside training.
5. Tier 3 self-play as soon as a trained checkpoint beats the scripted opponent.
