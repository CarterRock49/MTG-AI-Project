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
   every delivery (currently 8/8, 6/6, 110/110).
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
  training 6/6, scenarios 110/110 (grown from 12).
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
- ✅ **Crash-severity wiring**: per-card resolution exceptions now attribute
  `crash` entries to the support manifest instead of only logging.
- ▢ **Per-card override registry**: card name → hand-written effect callable,
  consulted before the text parser, for cards regex can't express.
- ✅ **Coverage report**: script joins a format's card pool against the
  manifest to print "N of M cards fully supported" — the format milestone.
- ◐ **Parser expansion** (started July 2026): a reusable diagnostic harness
  measures the factory's no-op fallback rate across common oracle clauses.
  Round 1 closed 6 gaps in a 40-clause sample (was 6/40 no-op, now 0/40):
  * **Bounce was broken for standard phrasing** — "return target creature to
    its owner's hand" embedded a regex pattern inside a plain substring `in`
    check, so it literally searched for the pattern string and never matched;
    only "to your hand" worked. High-impact: bounce is ubiquitous. Real regex now.
  * **"target player loses N life"** (drain/edict) → added LoseLifeEffect (CR
    118.4: life loss, not damage; unpreventable; fires LIFE_LOSS triggers).
  * **"gains <keyword> until end of turn"** — the most common combat trick —
    → added GainKeywordEffect (layer-6 add_ability, end-of-turn). Also fixes
    the trample half of "gets +1/+1 and gains trample", previously dropped.
  * **"distribute N +1/+1 counters"** parsed (single-target v1; the
    multi-target split is a Tier 3 agent-choice item).
  Regression-checked: previously-working clauses still route correctly. Four
  scenarios guard the new coverage.
  **Round 2 (July 2026)** closed 7 more gaps (13/20 no-op in the next sample,
  now 0/20 for the targeted clauses):
  * **Sacrifice / edict** — "sacrifice a <type>", "target player sacrifices",
    "each player/opponent sacrifices" all hit the no-op fallback. Added
    SacrificeEffect (v1 heuristic pick; player-choice is a Tier 3 item).
  * **Reanimation** — "return ... from graveyard to the battlefield" was only
    handled for the "to hand" phrasing; the battlefield destination no-opped.
    Added ReanimateEffect (enters under controller's control), disambiguated
    from bounce so "to hand" vs "to the battlefield" route correctly.
  * **"can't attack/block"** combat restrictions → granted cant_attack /
    cant_block abilities via the layer-6 path.
  * Found and fixed a genuine phantom method: **GameState._is_creature never
    existed** despite 6 call sites — including the environment's
    my_dead_creatures / opp_dead_creatures OBSERVATIONS, meaning the agent's
    dead-creature counts were ALWAYS ZERO in training, and BuffEffect/anthem
    target sets silently excluded every creature. Added the helper.
  **Round 3 (July 2026)** closed 4 more high-frequency clusters (14/20 no-op
  in the next sample; the genuinely-missing spell effects now route):
  * **Rituals / add-mana spell effects** — "Add {B}{B}{B}", "add N mana of any
    color" as a SPELL effect (Dark Ritual etc.) hit the no-op fallback and
    produced nothing. Added AddManaEffect. (Mana ACTIVATED abilities on
    permanents were already handled by ManaAbility; the diagnostic's raw
    "14/20" over-counts those since they aren't spell effects.)
  * **Gain control** — "gain control of target creature" → ControlEffect via
    apply_temporary_control (end-of-turn release already handled).
  * **Regenerate** — "regenerate target creature" → RegenerateEffect (adds a
    regeneration_shields entry; apply_regeneration consumes it).
  * **Mass tap** — "tap all creatures target player controls" → TapEffect with
    a new all_target_player scope.
  Regression-checked 0/18 including single-target tap vs mass tap.
  **Round 4 (July 2026)** closed 4 more clusters (9/20 no-op in the next
  sample — miss rate falling as coverage grows):
  * **Mass bounce** — "return all creatures to their owners' hands" → added an
    'all'/'all_yours' scope to ReturnToHandEffect.
  * **Untap-all** — "untap all lands you control" (ramp/combo staple) → added
    an 'all_yours' scope to UntapEffect.
  * **Dig** — "look at the top N, put one into your hand, rest on bottom/top"
    → added DigEffect (v1 keeps the highest-CMC card; the pick is a Tier 3
    agent-choice item).
  * **Tuck / put-on-library** — "put target creature on top of its owner's
    library" (tempo removal) → added PutOnLibraryEffect.
  Regression-checked 0/17 including single-target vs mass bounce/untap.
  **Round 5 (July 2026)** closed 4 more clusters (10/20 no-op in the next
  sample):
  * **Variable draw** — "draw cards equal to the number of X" → DrawCardEffect
    gained a count_expr, resolved via a new shared GameState helper
    count_dynamic_quantity (handles "creatures/lands/artifacts you control",
    "cards in your graveyard", basic-land subtypes, etc.).
  * **Variable life** — "gain life equal to the number of X" → GainLifeEffect
    gained the same count_expr path.
  * **Shuffle graveyard into library** (graveyard hate / recursion) → added
    ShuffleGraveyardEffect.
  * **Damage prevention (fog)** — "prevent all combat damage this turn",
    "prevent the next N damage" → added PreventDamageEffect, which registers a
    DAMAGE replacement (the replacement system was made functional earlier
    this campaign, so this composes cleanly). Correctly distinguishes combat
    vs non-combat damage and tracks "next N".
  Regression-checked 0/21 including fixed-count vs variable draw/life.
  **Round 6 (July 2026)** closed the last 3 gaps in the next sample (only
  3/20 no-op — the common-effect surface is now largely covered):
  * **Variable P/T pump** — "gets +X/+X ... where X is the number of Y" →
    BuffEffect gained a count_expr resolved via count_dynamic_quantity. The
    clause splitter severs the "where X is..." tail at the comma (same disease
    as delayed triggers/search), so the branch reads the count expression from
    the full effect_text, not just the clause.
  * **Animate land** — "target land becomes a 3/3 creature (still a land)" →
    added AnimateLandEffect (layer-4 add_type creature + layer-7b set_pt).
  * **Reveal hand** — "target player reveals their hand" → added
    RevealHandEffect (marks hand_revealed + fires a HAND_REVEALED trigger for
    downstream discard-selection effects to key off).
  Regression-checked 0/19 including fixed vs variable pump.
  **Round 7 (July 2026)** first-touched level-up creatures and Adventure cast
  paths:
  * **Level-up creatures** — Cards now parse `LEVEL N-M` / `LEVEL N+` bands,
    expose level-up costs, offer a `LEVEL_UP_CREATURE` action at sorcery speed,
    pay the cost, add level counters, and apply level-band P/T and abilities
    through layers. +1/+1 counters stack correctly on top of the band base.
  * **Adventure recast path** — Adventure spells exile on resolution, mark the
    creature side castable from exile, expose `CAST_FROM_EXILE`, consume that
    permission on cast, and resolve the creature side onto the battlefield.
  * **Repeated card-ID zone repair** — fixture decks represent multiple copies
    by repeating card IDs, so a hand/library copy could hide the battlefield
    permanent an effect targeted. Zone lookup now honors last movement and
    preserves duplicate IDs in list zones, repairing combat, edict, tuck, mass
    tap, and mass bounce guards.
  **Round 7.1 (July 2026)** first-touched combat keyword legality:
  * **Flying/reach** — blocking legality now rejects vanilla blockers for
    fliers and accepts reach.
  * **Menace** — the declare-blockers step no longer advances when a menace
    attacker has only one blocker assigned.
  * **Protection from red** — red creatures cannot block protected attackers,
    red spell targeting excludes protected permanents, and red damage is
    prevented before it is marked.
  * **Ward** — binary keyword data now falls back to oracle text so `Ward {2}`
    registers with the normalized `{2}` cost instead of `ward_generic`.
  * **Lifelink** — combat lifegain bookkeeping no longer crashes and lifegain
    is based on damage actually dealt.
  Regression-checked 105/105 scenarios after the round.
  **Round 7.2 (July 2026)** tightened targeting keyword resolution:
  * **Hexproof/shroud** — controller and opponent targeting are guarded, with
    shroud correctly prohibiting targeting by every player.
  * **Target action masks** — target-selection actions expose only legal
    targets and preserve the selected category for stack resolution.
  * **Ward target tax** — opposing targeted stack items now auto-pay parsed
    mana or simple life costs when possible; an unpaid ward cost counters the
    item before its effects apply.
  * **Target validation aliases** — singular/plural category keys and generic
    chosen-target contexts resolve consistently instead of spuriously fizzling.
  Regression-checked 110/110 scenarios after the round.
  **Coverage status:** seven rounds plus 7.2 have closed ~45
  effect/mechanic classes across
  removal, bounce, counters, tokens, keywords, sacrifice, reanimation,
  control, mana, library manipulation, variable-count effects, prevention,
  animation, levelers, Adventure, and duplicate-ID zone semantics. Miss rate
  fell 6→13→14→9→10→3 across parser samples before the first-touch sweep moved
  into mechanic subsystems. Remaining gaps are increasingly specialized
  (dice/planar, complex modal riders, "you choose a card" interactive discard,
  meld/specialize). Reorder by real manifest counts whenever harvest runs begin.
- ◐ **First-touch coverage sweep**: one scenario for every subsystem that has
  never had one (this practice found four phantom methods and three dead
  subsystems; assume more remain in untested corners — next candidates:
  phasing attachments, reflexive triggers, spell-copy retargeting, modal/X
  choice audits, and remaining special card-frame mechanics).

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

1. ✅ Golden scenario harness — 110 scenarios and growing; scenario-first is a
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
- MDFC back-face casting — v1 support added (July 2026): is_mdfc() no longer
  requires "//" in the text (two non-transform faces suffice), Card exposes
  get_face_cost/get_face_text/get_face_type_line per face, and cast_spell uses
  the back face's cost + text when cast_as_back_face is set (the spell path
  previously always used the front cost). Remaining: MDFC back-face for
  non-land permanents entering directly, and back-face targeting text.
- Level-up creatures — v1 support added (July 2026): Card parses the
  "LEVEL N-M / N+" band format (distinct from Class enchantments) into
  is_leveler / leveler_bands / level_up_cost, with get_leveler_pt(counters)
  and get_leveler_abilities(counters). The action space exposes
  LEVEL_UP_CREATURE, pays the printed cost, adds level counters, and the layer
  system applies current-band P/T and abilities. Remaining: richer player
  choice around when/whether to spend mana beyond the basic action mask.
- Adventure cards — v1 support added (July 2026): casting the Adventure half
  resolves to exile instead of graveyard, marks the creature side castable from
  exile, exposes CAST_FROM_EXILE, consumes that permission on cast, and resolves
  the creature side to the battlefield. Remaining: adventure-half parsing and
  targeting are still heuristic.
- Repeated card IDs still model copies coarsely; last-moved location tracking
  and duplicate-preserving list zones repair the known first-touch failures,
  but true per-copy object identity remains a deeper future cleanup.
- Clones/MCTS copies start with an empty delayed-trigger registry.
- Opponent auto-orders triggers and blockers (deadlock-safe scope cut until
  self-play).
- Phasing does not yet preserve attachments; snow payment can double-charge
  (fidelity-counted); reflexive triggers (603.12) unimplemented;
  `_evaluate_condition` vocabulary is thin (life totals, card counts,
  "you control X" only).
- `prevent_damage`/`redirect_damage` in game_state_damage are a dead API
  (no callers) — remove or wire deliberately.
- Ward target-tax v1 supports parsed mana costs and simple "pay N life" costs
  by auto-paying when possible. Sacrifice/discard costs and letting the agent
  deliberately decline payment remain future choice-exposure work.

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
