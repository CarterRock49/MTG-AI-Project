# Playersim — Project Plan (overhauled July 2026)

**Mission:** train an AI to play two-player Magic well enough that its games
yield trustworthy per-card and per-deck statistics, which feed a downstream
deck-construction AI searching for the best deck per format. Everything in
this plan is ranked by one question: *does it make the statistics more
trustworthy for the deck-builder?*

Out of scope permanently: multiplayer, Commander, match-play (Bo3 is a
possible late add only if target formats demand it).

---

## Definition of done ("complete project")

The project is complete when all of the following hold:

1. **Green gates, always.** Smoke, training, and scenario suites pass on
   every delivery (currently 8/8, 6/6, 45/45).
2. **Zero known stats-corrupting bugs.** The silent-bug catalog (appendix)
   is closed; every fixed bug has a permanent guard scenario.
3. **Quantified card coverage.** For each target format's card pool, the
   card support manifest reports what fraction of the pool simulates
   faithfully; the milestone per format is: every card in the pool is either
   fully supported or explicitly excluded by the deck builder.
4. **Choices are the agent's.** No rules decision that affects card value is
   silently auto-resolved for the agent (trigger order ✅, damage assignment
   order ✅, targeting ▢, modal choice audit ▢, X choice audit ▢).
5. **Trained play beats scripted play** and stats are harvested under
   self-play or league play, not vs. the random opponent.
6. **Calibration passes.** Engine winrates for a small set of known-matchup
   decks fall within tolerance of published/human data — the end-to-end
   sanity check that play quality and rules fidelity are sufficient.
7. **The loop closes.** The deck builder consumes `STATS_SCHEMA.md` data +
   the support manifest, produces candidate decks, and those decks feed back
   into harvest runs without manual file surgery.

---

## Status snapshot (July 2026)

- Tier 0 (stats plumbing): ✅ complete.
- Tier 1 (rules correctness): ✅ complete — all seven items plus the P1
  placeholder triage delivered; see appendix for the bug catalog.
- Test gates: smoke 8/8 (fixture decks now exercise triggers every episode),
  training 6/6, scenarios 45/45 (grown from 12).
- **Stats collected before July 2026 are unusable** (wrong player, wrong
  winner, fictional play turns, cosmetic first strike, compounding P/T,
  dead replacement system). Wipe and re-harvest after the current engine
  is deployed.

---

## Tier 2 — Card coverage, driven by the support manifest

The card support manifest (`Playersim/card_support.py`, July 2026) is the
engine of this tier. Any card whose text the engine cannot faithfully run is
automatically recorded — with the failing clause, a severity, and running
counts — and persisted to `deck_stats/card_support_manifest.json` alongside
the game statistics.

**Severities:** `crash` (handling raised), `unparsed` (whole effect produced
nothing runnable), `partial` (some clauses fell back to no-ops). Worst
severity sticks per card.

**The workflow this enables:**
1. Run harvest games; the manifest fills itself, ranked by real play
   frequency (counts), never alphabetically.
2. Adding support = pick the highest-count entry, write a failing scenario
   for the card's clause, extend the parser (or the per-card override
   registry below), watch the entry stop accumulating.
3. The deck builder loads the manifest and **excludes `crash`/`unparsed`
   cards from candidate pools** and down-weights `partial` cards'
   statistics until support lands. This closes the loop Carter asked for:
   unsupported cards can't silently poison deck search.

Remaining Tier 2 work:
- ▢ **Crash-severity wiring**: wrap per-card resolution paths so exceptions
  attribute `crash` entries instead of only logging.
- ▢ **Per-card override registry**: card name → hand-written effect callable,
  consulted before the text parser, for cards regex can't express.
- ▢ **Coverage report**: script that joins a format's card pool against the
  manifest to print "N of M cards fully supported" — the format milestone.
- ▢ **Parser expansion, manifest-ranked**: grow clause coverage in
  `EffectFactory` in order of manifest counts from real runs.
- ▢ **First-touch coverage sweep**: one scenario for every subsystem that has
  never had one (this practice found four phantom methods and three dead
  subsystems; assume more remain in untested corners — candidates: search
  effects, library manipulation, discard/mill paths, auras/equipment
  attachment lifecycle, planeswalker loyalty, sagas).

## Tier 3 — Training & environment quality

1. ▢ **Choice exposure, remainder**: targeting as an agent choice (the
   `resolve_targeting` auto-fallback becomes the exception, not the rule);
   audit modal and X choices for silent auto-resolution; opponent ordering
   choices route through a policy under self-play.
2. ▢ **Opponent policy**: self-play (or league of checkpoints) as soon as a
   trained model beats the scripted opponent; all harvest stats after that
   point come from policy-vs-policy games.
3. ▢ **Hidden-information audit**: observations must not leak opponent hand,
   library order, or face-down identities.
4. ▢ **Replay logs**: full action-log replays from seeded resets so any stat
   anomaly is reproducible.
5. ▢ **Deck legality validation** at load per target format (copy limits,
   banlists) so the deck builder searches only legal space.

## Tier 4 — Verification & calibration

1. ✅ Golden scenario harness — 45 scenarios and growing; scenario-first is a
   working agreement, not a suggestion.
2. ▢ **Property tests**: zone-count conservation per action; SBA idempotence;
   the action mask never permits an illegal action (fuzz); mana pools empty
   at phase boundaries; layer recalculation idempotence under repetition
   (one such scenario exists; generalize).
3. ▢ **Long-game fuzzing** across many seeds with invariant checks.
4. ▢ **Calibration study**: 3–5 deck pairs with well-known matchup winrates;
   run at harvest scale; compare. This is the acceptance test for the whole
   pipeline and gates "harvest at scale."

## Tier 5 — Harvest operations & deck-builder integration

1. ▢ **Throughput**: profile games/hour; parallel envs; identify the hot
   paths (layer recalcs and text parsing are the likely suspects — consider
   caching parsed abilities per card name).
2. ▢ **Harvest protocol**: versioned runs (agent version stamping exists),
   wipe-and-reharvest procedure documented, per-run manifests.
3. ▢ **Deck-builder contract**: `STATS_SCHEMA.md` + support manifest are the
   full interface; the builder's exclusion logic and confidence weighting
   consume them directly.
4. ▢ **Feedback loop**: builder-proposed decks auto-enter the harvest queue;
   their novel cards populate the manifest; support work is prioritized by
   what the builder actually wants to play.

---

## Working agreements

- **Scenario-first**: failing scenario before implementation, every slice.
- **Turn-start drift check**: diff workspace vs `/mnt/project/` before work;
  classify any drift before building on it.
- **Delivery**: one zip per turn containing every file differing from the
  project — complete drop-in files, `Playersim/` and `tests/` paths.
- **Three-suite gate** before every delivery; roadmap updated with every
  slice, including v1 limitations, honestly stated.

## Known v1 limitations (consolidated)

- Delayed-trigger pronouns referring to objects created earlier in the same
  resolution mis-bind to the source (token-maker riders) and no-op safely.
- Specific `remove_ability` is not an existence dependency in layer sorting;
  CR 305.7 (Blood Moon ability loss) not modeled; applicability-set
  dependencies out of scope while `affected_ids` is a static snapshot.
- Transform/DFC printed-identity re-snapshot not wired (`snapshot_printed`
  exists; call it on transform).
- Clones/MCTS copies start with an empty delayed-trigger registry.
- Opponent auto-orders triggers and blockers (deadlock-safe scope cut until
  self-play).
- Phasing does not yet preserve attachments; snow payment can double-charge
  (fidelity-counted); reflexive triggers (603.12) unimplemented;
  `_evaluate_condition` vocabulary is thin (life totals, card counts,
  "you control X" only).
- `prevent_damage`/`redirect_damage` in game_state_damage are a dead API
  (no callers) — remove or wire deliberately.
- Protection/menace combat interactions unaudited.

---

## Appendix — the silent-bug catalog (institutional knowledge)

Every one of these shipped silently and was found by a first-ever scenario.
The pattern to internalize: **untested subsystem ⇒ assume broken.**

Phantom methods (calls to functions that never existed anywhere):
`_extract_condition_clause` (trigger conditions), `gs._build_type_line`
(crashed every layer-1 copy), `_is_effect_expired` (crashed the entire
replacement system when any effect registered), plus a Card-object `.get()`
in replacement ordering.

Dead-on-arrival subsystems: text-parsed triggered abilities (optional regex
separator mangled every condition — no parsed trigger ever fired);
replacement effects (two latent crashes, exceptions swallowed); mana
doubling (listened for an event nothing fired); dies-copy tokens (set a flag
nothing read); phasing (permanents oscillated out of existence, lost their
abilities permanently, force-untapped).

Stats-corrupting: layer base fed back on itself (+1/+1 compounded every
phase); layer write-back leaked across games via shared card_db objects;
first strike was cosmetic (no SBA between damage steps); play turns
fabricated from CMC; winner/loser card attribution swapped on p2 wins; all
plays credited to p2 (dict-vs-index comparison); cost reductions applied
before increases (601.2f inverted); SBAs never applied to cards
(isinstance-str vs int ids — creatures never died); spell resolution crashed
at target validation and deleted the card; layer engine skipped computation
with no effects registered; stdlib `copy` shadowed by numpy; prevention
'creature' class shielded players.

Each has a permanent guard scenario. Keep writing them first.
