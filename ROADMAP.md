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
2. **Layer dependency ordering (CR 613.8)** — ✅ core delivered (July 2026).
   `_sort_layer_effects` now does a real within-layer dependency pass on top
   of timestamp order: an effect that strips another effect's source applies
   first (topological sort; dependency loops fall back to timestamp order per
   613.8c). The piece that actually changes outcomes is existence tracking:
   once `remove_all_abilities` applies to a source, that source's
   not-yet-applied effects in layer 6 and all of layer 7 no longer exist and
   are skipped — effects already applied in earlier layers correctly continue
   (CR 613.6). The Humility shape (grantor stripped => its grants vanish) and
   the two-strip loop now evaluate correctly; three scenarios guard it.
   **Remaining (v1 limitations):** specific `remove_ability` is not treated
   as an existence dependency (the engine can't yet tell which ability
   generates which effect); layer-4 `set_type`/`lose_all_subtypes` edges are
   ordered but basic-land-typing ability removal (CR 305.7, the Blood
   Moon/Urborg shape) is not modeled; dependencies that change an effect's
   *applicability set* rather than its existence are out of scope while
   `affected_ids` is a static snapshot.
3. **Intervening "if" (603.4)** — ✅ delivered (July 2026), and the scenario
   work exposed two silent bugs bigger than the feature: (a) the trigger
   parser's separator was optional, so every text-parsed trigger condition
   was mangled ("when t...") and **no text-parsed triggered ability ever
   fired** — both live trigger paths (ability_handler.check_abilities and
   the stack) route through can_trigger, whose patterns could never match;
   (b) can_trigger called `_extract_condition_clause`, a method that did not
   exist anywhere — a latent AttributeError masked only by bug (a). Both
   fixed. The intervening "if" is now extracted at parse time
   (`self.intervening_if`), checked at trigger time in can_trigger and
   re-checked at resolution in resolve/resolve_with_targets (fizzle
   convention if false). Condition evaluation fallback also fixed: matched
   patterns now return their actual boolean instead of falling through to
   "assume True" on failure — without this, no intervening "if" could ever
   evaluate false. Three scenarios guard it. Trigger stats before this fix
   are suspect for any deck relying on text-parsed triggers.
   **Remaining:** reflexive triggers (603.12); richer condition vocabulary
   in `_evaluate_condition` (counters, tapped state, card types in play).
4. **Trigger ordering (603.3b) as an agent choice** — ✅ delivered (July 2026).
   process_triggered_abilities now stacks in strict APNAP batches; when the
   agent's player has 2+ simultaneous triggers, an 'order_triggers'
   choice_context opens in PHASE_CHOOSE (same pattern as scry/surveil) and
   each pick maps to the shared 353-362 action indices — **no action-space
   size change**, so saved MaskablePPO checkpoints stay loadable. Each action
   puts one pending trigger onto the stack next; a final single trigger is
   auto-stacked, and 0/1-trigger batches bypass the choice entirely, leaving
   training dynamics untouched in the common case. After ordering, the game
   enters PHASE_PRIORITY per CR 117.3c with the interrupted phase preserved.
   Mechanics live in AbilityHandler (order_trigger_chosen) so scenarios can
   drive them without the env. Two scenarios guard it.
   **Deliberate scope cut:** the scripted opponent auto-orders in queue order
   (an interactive opponent choice would deadlock the single-agent loop);
   route it through a policy when self-play lands (Tier 3).

4b. **Fixture-deck trigger coverage** — ✅ (July 2026). Both fixture decks now
   run two trigger creatures each (ETB + dies: Ember Herald, Cinder Martyr /
   Grove Chronicler, Verdant Reclaimer; 11 spells x4 + 16 lands = 60), so
   every smoke/training episode exercises the full trigger pipeline that was
   silently dead until the 603.1 parse fix. An end-to-end scenario
   (move_card -> event -> queue -> stack -> resolution -> draw) guards the
   wiring itself.
5. **Copy fidelity (CR 707) + cost-modification ordering (601.2f)** — ✅
   delivered (July 2026), and the scenario work exposed the worst engine bug
   since the SBA int/str one: **the layer system's base fed back on itself.**
   card_db returns the same shared object the end-of-pass write-back mutates,
   and base_chars read live attributes — so every recalculation (every phase
   change) compounded static P/T effects (+1/+1 became +N/+N over a game),
   inflating combat math, suppressing deaths, and corrupting every
   P/T-derived stat wherever static pump effects existed. Fix: Card now
   snapshots printed characteristics at construction (snapshot_printed /
   printed()); the layer pass starts from the snapshot, and the live card is
   an output, never an input. An idempotence scenario guards it.
   On that foundation: both copy sites (create_token_copy and the layer-1
   copy calc) now copy PRINTED characteristics per CR 707.2 instead of live
   pumped/granted values; a token's snapshot of those values becomes its own
   printed identity, giving correct copy-of-copy semantics. The layer-1 copy
   path also crashed unconditionally on a nonexistent gs._build_type_line —
   the first 707.2 scenario ever to exercise it found the phantom call; it
   now copies the printed type line and refreshes inherent abilities from
   the copied text. 601.2f: apply_cost_modifiers applied reductions before
   increases (backwards); a reduction bottoming out at zero before a tax
   over-priced spells by the clipped amount. Increases now apply first.
   Four scenarios guard the slice. P/T and cost stats predating this bundle
   are suspect for static-pump and cost-reduction decks respectively.
   **Remaining (v1 limits):** transform/DFC printed-identity re-snapshot not
   wired (call snapshot_printed on transform); copy effects that re-snapshot
   through layer-1 copies of copies within one pass; convoke/delve colored
   reductions still deferred.
6. **Combat damage edge math** — ✅ delivered (July 2026). Two real bugs:
   (a) **First strike was cosmetic.** Both damage steps run inside one
   resolve_combat call, and the promised "game loop checks SBAs later" never
   happened between them -- creatures dealt lethal first-strike damage stayed
   on the battlefield and struck back (the regular step only skips combatants
   that have LEFT it). check_state_based_actions now runs between the steps
   per CR 510.4. Every first-strike creature's stats prior to this fix are
   worthless -- it never actually pre-empted anything.
   (b) **Cross-game leakage, layer edition.** The layer write-back mutates
   shared card_db objects (name, P/T, keywords, colors, types, oracle_text)
   and nothing restored them at game start, so game N's pumps and copy
   effects leaked into game N+1's live card state -- surfaced when a 707.2
   copy scenario permanently renamed a shared fixture card. Card.reset_to_printed()
   now restores the printed snapshot in the same game-start loop that clears
   counters. A leak-guard scenario spans two games over the shared db.
   Trample assignment (lethal-then-excess) and deathtouch math (1 = lethal
   for both assignment and the 704.5h SBA, including deathtouch+trample) were
   verified CORRECT by the new scenarios -- four scenarios guard combat now.
   **Remaining:** protection/menace interactions unaudited.
   **Assignment order ✅ (July 2026):** damage-assignment order among 2+
   blockers is now an agent choice (CR 510.1c) — an 'order_blockers'
   choice_context on the shared 353-362 indices, opened by
   handle_assign_combat_damage before resolution and finalized by
   blocker_order_chosen, which re-invokes the deferred combat resolution and
   advances to end of combat. Single-blocker attackers bypass the choice;
   the scripted opponent keeps the toughness-ascending default (same scope
   cut as 603.3b). This also retired action 435's placeholder, which ignored
   agent input entirely. Two scenarios guard it.
7. **Triage of self-admitted `simplified`/`placeholder` sites** — ✅ triaged
   (July 2026). 49 sites remain of the original 82 (earlier slices retired a
   third). The stats-critical tier was FIXED this turn — four compounding
   bugs meant per-card statistics were thoroughly scrambled end to end:
   (a) **Play turns were fabricated**: the tracker recorded play turn = CMC
   (every 6-drop "played on turn 6"). gs.play_history now records real turns
   in track_card_played and flows through record_game -> _update_card_stats;
   the CMC estimate survives only as a fallback for history-less callers.
   (b) **Winner/loser attribution swapped in every p2 win**: raw p1/p2-keyed
   cards_played fed a consumer that reads index 0 as the winner. Both
   environment record sites now remap via _stats_result_mapped.
   (c) **All plays credited to p2**: track_card_played re-mapped its argument
   by comparing an int index to the player DICT (always False -> index 1),
   so p1's plays were counted as p2's in every game ever recorded.
   (d) GameState.__slots__ gained 'play_history'.
   Per-card stats collected before this bundle are unusable: wrong player,
   wrong winner, fictional curve. Wipe and re-harvest.
   **Remaining 45 sites by priority:** [P1 rules-outcome, next slices]
   replacement_effects x4 (mana doubling, token-copy replacement, X-cost
   placeholder, target simplification), game_state_damage:594 (placeholder
   source/duration on redirection), enhanced_mana_system:1907 (snow payment
   assumed after can_pay), combat.py:2340 (approximation in legacy resolver
   path), targeting:655, game_state_turn:64 (phasing state simplified).
   [P2 unsupported-mechanic stubs, count in fidelity telemetry rather than
   fix] game_state_zones:618 (venture). [P3 agent-quality heuristics, not
   correctness] strategic_planner_search, strategy_memory, environment
   observation sites, actions_space x3, ability_handler x7, ability_types
   x6, ability_utils x2, actions_turn x2, actions_mechanics,
   actions_choices, actions.py — these shape play strength, not rules
   outcomes; revisit after Tier 2. [P4 cosmetic] docstrings/log wording
   (e.g. deck_stats_tracker:3954).

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
