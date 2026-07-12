# Playersim — Project Plan (overhauled July 2026)

**Mission:** train an AI to play two-player Magic well enough that its games
yield trustworthy per-card and per-deck statistics, which feed a downstream
deck-construction AI searching for the best deck per format. Everything in
this plan is ranked by one question: *does it make the statistics more
trustworthy for the deck-builder?*

Out of scope permanently: multiplayer, Commander, Planechase/planar dice,
and match-play (Bo3 is a possible late add only if target formats demand it).

---

## Definition of done ("complete project")

The project is complete when all of the following hold:

1. **Green gates, always.** Smoke, training, and scenario suites pass on
   every delivery (currently 9/9, 13/13, and 326/326, plus 15/15 fixture-
   harvest tests, 7/7 production-protocol tests, 19/19 card-registry tests,
   1/1 support-preflight tests, 2/2 deck-corpus tests, 13/13 deck-ingest tests,
   6/6 fuzz/replay tests, and the deterministic 8-seed / 8,000-action
   default fuzz profile, and the strict 32-seed / 320,000-action long
   profile).
2. **Zero known stats-corrupting bugs.** The silent-bug catalog (appendix)
   is closed; every fixed bug has a permanent guard scenario.
3. **Quantified card coverage.** For each target format's card pool, the
   card support manifest reports what fraction of the pool simulates
   faithfully; the milestone per format is: every card in the pool is either
   fully supported or explicitly excluded by the deck builder.
4. **Choices are the agent's.** No rules decision that affects card value is
   silently auto-resolved for the agent (trigger order ✅, damage assignment
   order ✅, targeting ▢, modal choice audit ✅, X choice audit ✅).
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

- Tier 0 (stats plumbing): ✅ complete; Round 7.37 production-tested opening
  hands, canonical draw history, real play turns, and terminal-cause telemetry
  across all six training workers.
- Tier 1 (rules correctness): ✅ complete — all seven items plus the P1
  placeholder triage delivered; see appendix for the bug catalog.
- Tier 2 (card coverage): ◐ the audited eight-deck sample has no unknown
  high-risk partials. The generic Spree casting transaction and exact Three
  Steps Ahead effects are supported; other Spree cards remain subject to their
  own effect-parser, scenario, and manifest evidence. Format-wide quantified
  coverage remains manifest-driven. Round 7.50 statically preflighted all
  4,702 current Standard cards and now separates verified, corpus-clean,
  unseen, partial, unparsed, crash, and explicitly excluded evidence. The
  current July 12 ledger contains 68 verified, 89 observed-clean, 3,565
  unseen-clean, 809 partial, and 171 unparsed cards: 79.1578051893% is
  static-clean and 3.3390046789% is evidence-qualified. The representative
  metagame has no unparsed or crash cards and retains two acknowledged partial
  multi-face entries: Emeritus of Ideation and Esper Origins. Round 7.55 added
  generic full-pool coverage for eight recurring mechanic families without
  regressing any previously clean card.
- Tier 3 (training/environment): ◐ policy plumbing and audit work are complete;
  a trained checkpoint still needs to beat scripted play before Harvest is
  promoted to policy-vs-policy.
- Tier 4 (verification/calibration): ◐ invariant and long-fuzz gates are green;
  the matchup calibration study remains open.
- Tier 5 (operations/integration): ◐ Harvest orchestration is complete and,
  since Round 7.46, format/corpus-configurable with full run lineage; strength
  qualification, production throughput profiling, and deck-builder integration
  remain open.
- Target-format program: ◐ milestone 1 (format foundation and lineage) is
  complete — frozen canonical registry + feature schema in
  `formats/standard/`, explicit `--format`/`--decks` configuration, and
  lineage-stamped manifests. User-supplied decks now route into isolated format
  pools automatically; policy qualification and builder feedback remain open.
- Test gates: smoke 9/9, training 13/13, scenarios 346/346 (grown from 12),
  fixture harvest 15/15, production Harvest protocol 7/7, card registry
  19/19, deck ingestion 13/13, fuzz/replay configuration 6/6, deterministic
  default fuzz 8 seeds x 1,000 valid actions, and strict long fuzz 32 seeds x
  10,000 valid actions.
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
- ✅ **Per-card override registry**: card name → hand-written effect callable,
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
  * **"distribute N +1/+1 counters"** parsed. The original single-target v1
    was replaced by policy-selected multi-target allocation in Round 7.24.
  Regression-checked: previously-working clauses still route correctly. Four
  scenarios guard the new coverage.
  **Round 2 (July 2026)** closed 7 more gaps (13/20 no-op in the next sample,
  now 0/20 for the targeted clauses):
  * **Sacrifice / edict** — "sacrifice a <type>", "target player sacrifices",
    "each player/opponent sacrifices" all hit the no-op fallback. Added
    SacrificeEffect; its original heuristic pick was replaced by the affected
    player's sequential policy choice in Round 7.24.
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
    → added DigEffect. Round 7.24 replaced its highest-CMC heuristic with a
    chooser-only, card-visible policy decision.
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
  **Round 7.3 (July 2026)** preserved attachments through phasing:
  * **Indirect phasing groups** — Auras, Equipment, and other tracked
    attachments phase out and in with the permanent they are attached to.
  * **Cross-controller restoration** — an opponent-controlled Aura returns on
    the enchanted permanent's phase-in rather than waiting for its controller.
  * **Continuous effects** — attachment effects unregister while phased out
    and rebuild on phase-in without severing the attachment relationship.
  * **Clone-safe state** — phased groups store controller keys and their state
    is copied into cloned game states used by lookahead.
  Regression-checked 112/112 scenarios after the round.
  **Round 7.4 (July 2026)** implemented reflexive-trigger sequencing:
  * **Reflexive parser** — exact "When you do" and "When that player does"
    riders stay paired with their prerequisite instead of ordinary clause
    splitting resolving them unconditionally.
  * **Separate response window** — a successful prerequisite queues a real
    trigger through the APNAP pipeline; its effect no longer resolves inline.
  * **Failure gating** — if the prerequisite action cannot happen, no reflexive
    trigger is created.
  * **Trigger-zone correctness** — battlefield death triggers no longer fire
    from unrelated copies sitting in hand or graveyard.
  Regression-checked 115/115 scenarios after the round.
  **Round 7.5 (July 2026)** made spell copies rules-faithful and targetable:
  * **Copied decisions** - modes, X, color, kicker/additional-cost state, and
    original targets are deep-copied instead of erased.
  * **Optional retargeting** - the action mask offers both legal new targets
    and an explicit keep-original-targets path; selecting a target updates only
    the identified copy stack item.
  * **Shared copy path** - parsed copy effects, the legacy copy action, and
    conspire now use one deterministic stack-copy implementation.
  * **Target restrictions** - parsed "instant or sorcery" copy effects reject
    creature and other ineligible spells while retaining the new-target rider.
  * **Physical-card safety** - resolving or countering a copy never moves the
    original card; the original spell retains and resolves against its targets.
  Regression-checked 119/119 scenarios after the round.
  **Round 7.6 (July 2026)** exposed modal and X decisions to the agent:
  * **Rules-order casting** - modes and X are chosen while the card remains in
    hand and before mana is paid; the normal cast path resumes afterward.
  * **Modal finalization** - choose-one and optional choose-one-or-both spells
    preserve selected modes, paid costs, priority state, and card movement.
  * **Mode-aware targeting** - only selected modal text determines whether and
    what kind of target the spell requests.
  * **X payment and resolution** - affordable values 0-10 are action-masked,
    the chosen value is paid once, copied into effect context, and X=0 remains
    zero instead of falling back to a placeholder one.
  * **Fixed-number isolation** - choosing X no longer overwrites unrelated
    printed effect amounts; targeted destroy effects retain target semantics.
  Regression-checked 124/124 scenarios after the round.
  **Round 7.7 (July 2026)** exposed discard decisions to the affected player:
  * **Card-level choices** - nonrandom discard effects enter `PHASE_CHOOSE` and
    expose hand indices 0-9 through the existing `DISCARD_CARD` action range.
  * **Queued counts and players** - multi-card and each-player effects remain
    pending until every required card has been selected; the scripted opponent
    now makes its own discard action instead of stalling the choice phase.
  * **One discard pipeline** - effect and cleanup discards share replacement,
    zone-movement, and Madness handling; random discard remains immediate.
  * **Cleanup correctness** - maximum-hand-size cleanup pauses for the active
    player's choice and no longer incorrectly makes the nonactive player
    discard down to the active player's maximum hand size.
  Regression-checked 130/130 scenarios after the round.
  **Round 7.8 (July 2026)** completed ability targeting exposure and added meld:
  * **Triggered targets** - targeted triggers are stacked with pending target
    decisions, pause for `SELECT_TARGET`, and never enter the stack when no
    legal required target exists.
  * **Activated targets** - activated abilities choose targets before costs are
    paid, then resume payment and stack placement with the selected targets.
  * **Opponent target policy** - scripted opponents select their first legal
    target action instead of stalling targeted abilities.
  * **Effect-aware legality** - targeting validates the specific stack effect
    text instead of depending only on the source card's full oracle text.
  * **Meld** - Scryfall-style `all_parts` metadata identifies the partner and
    result; both components exile, one combined permanent returns with the
    result identity, and both physical cards separate when it leaves.
  Regression-checked 135/135 scenarios after the round.
  **Round 7.9 (July 2026)** completed direct and optional target choices:
  * **Direct-effect targets** - invoking a targeted `AbilityEffect` without a
    stack item now pauses for `SELECT_TARGET` and resumes only after the
    controller chooses; the strategic auto-target fallback is gone.
  * **Optional target counts** - "up to N targets" remains open after its
    minimum is met, then commits on Pass or automatically at the maximum.
  * **Shared finalization** - spells, copied spells, activated abilities, and
    resumable direct effects use one bounded target-commit path.
  * **Plural target parsing** - recognized plural target nouns normalize to
    their singular rules types, so phrases such as "target creatures" expose
    legal creature choices.
  Regression-checked 137/137 scenarios after the round.
  **Round 7.10 (July 2026)** first-touched numeric dice and Specialize:
  * **Die-result tables** - multiline numeric result rows stay attached to the
    spell or ability that rolls; exactly the row covering the rolled value is
    parsed and resolved.
  * **Roll state and triggers** - numeric rolls record side count, result,
    roller, source, and turn in clone-safe history, then fire `DIE_ROLLED` for
    cards watching die rolls.
  * **Specialize choices** - the action mask exposes Specialize at sorcery
    speed, asks which card to discard, and asks for a color when that card is
    multicolored before paying either cost.
  * **Linked perpetual identity** - the permanent keeps its physical ID while
    adopting the selected `all_parts` variant's characteristics and abilities;
    `SPECIALIZES` triggers fire, the identity persists across zones, and the
    shared card object restores before the next game.
  * **Fidelity boundary** - all five linked variants must exist in `card_db`;
    incomplete families are marked `unparsed` instead of partially simulating.
  Regression-checked 139/139 scenarios after the round.
  **Round 7.11 (July 2026)** first-touched day/night and Mutate end to end:
  * **Turn-start day/night** - the prior active player's spell count is
    preserved through cleanup, checked after phasing and before untapping, and
    applies the correct zero-spell day-to-night / two-spell night-to-day rules.
  * **Daybound entry and faces** - the first daybound permanent establishes
    day, permanents entering at night enter nightbound, synchronized transforms
    refresh types/abilities/layers, and shared DFC identity resets next game.
  * **Mutating creature spells** - action 426 now casts through the ordinary
    alternative-cost and stack paths, exposes only owned non-Human creature
    targets, and remains answerable before resolution.
  * **Over/under and triggers** - resolution exposes the controller's position
    choice, tracks ordered physical components, combines the top card's
    characteristics with every component's abilities, and fires each parsed
    whenever-this-creature-mutates ability.
  * **Resolution and separation** - an illegal target makes the mutating spell
    enter as an ordinary creature; a merged permanent carries counters/status
    and sends every component to the same destination when it leaves.
  Regression-checked 142/142 scenarios after the round.
  **Round 7.12 (July 2026)** audited all eight sample decks and repaired their
  shared mana-base behavior:
  * **Sample-deck audit** - `SAMPLE_DECK_SUPPORT_AUDIT.md` inventories all 480
    deck slots / 110 unique cards, separates confirmed gaps from unverified
    paths, records affected copy counts, and ranks the next work by stats risk.
  * **Land entry** - current "enters tapped" wording works alongside the older
    template, and fast lands count other controlled lands at entry.
  * **Agent mana choices** - multicolor lands expose their legal outputs in
    `PHASE_CHOOSE` instead of silently producing the first printed color.
  * **Pain and Verge lands** - colored pain-land modes deal their printed
    damage; Verge secondary colors appear only when a required basic land type
    is controlled. Restricted outputs remain in conditional mana pools.
  This corrects land behavior across 114 of the 480 sample-deck slots.
  Regression-checked 146/146 scenarios after the round.
  **Round 7.13 (July 2026)** implemented stun counters across all untap paths:
  * **Central replacement** - an untap attempt removes one stun counter and
    leaves the permanent tapped; multiple counters require multiple attempts,
    and replaced untaps do not emit `UNTAPPED`.
  * **Every untap route** - turn-based untapping, resolving untap effects, and
    untap-symbol costs all use the same replacement-aware permanent API.
  * **Sequenced target effects** - sentence-separated and conjunction-separated
    counter clauses retain the target selected by the preceding effect.
  * **Sample cards** - Kaito's -2 taps and adds two counters; Floodpits Drowner's
    ETB asks for a target, taps it, and adds one counter. Their unrelated
    emblem/type-changing and stun-linked shuffle gaps were deferred to Round
    7.17.
  This closes stun semantics for 17 sample-deck slots.
  Regression-checked 150/150 scenarios after the round.
  **Round 7.14 (July 2026)** implemented Valiant and the sample decks' Role
  tokens end to end:
  * **Target events** - committed spell, activated-ability, triggered-ability,
    direct-effect, retargeted-copy, and inherited-copy targets emit one shared
    event. Per-controller turn tracking enforces Valiant's first friendly target
    and resets when a turn starts or the object leaves the battlefield.
  * **Sample Valiant cards** - Heartfire Hero receives its +1/+1 counter above
    the targeting spell, while Emberheart Challenger exiles the library top and
    grants the printed end-of-turn play permission.
  * **Monster and Wicked Roles** - colorless Aura Role tokens enter already
    attached, grant their printed P/T and trample effects, coexist across
    controllers, and apply the newest-same-controller Role state-based action.
    A displaced Wicked Role still queues and resolves its life-loss trigger.
  * **Shared repairs found by the scenarios** - signed positive pump values now
    parse, targeted combat tricks keep their targets, timed spell layers survive
    the source card entering the graveyard, and layer hashes accept mixed deck
    and token ID types.
  This closes Valiant for 16 sample-deck slots and Role behavior for 9 slots.
  Heartfire Hero's remaining rider was deferred to Round 7.17; The Witch's
  Vanity's Food gap remains in the sample-deck audit. Regression-checked 155/155
  scenarios after the round.
  **Round 7.15 (July 2026)** implemented linked temporary exile and Nowhere to
  Run's targeting exceptions:
  * **Linked one-shot effects** - `Deep-Cavern Bat` exposes its optional filtered
    opponent-hand choice, and `Leyline Binding` targets a nonland opposing
    permanent. The chosen object is linked to its source and returns immediately
    to hand or battlefield when that source leaves. If the source left before
    the ability resolved, nothing is exiled.
  * **Nowhere to Run** - its two static lines are live rules exceptions rather
    than layer-6 ability removal. Its own ETB can target opposing creatures with
    hexproof, ward does not trigger while it is present, and resolution-time
    legality correctly reverts if it leaves.
  * **Ward timing** - ward obligations are now captured when targets are
    committed. Removing Nowhere later cannot create a retroactive ward trigger,
    while ordinary legacy stack entries retain the dynamic compatibility path.
  * **Shared parser repairs** - `target opponent`, `target nonland permanent`,
    and player target categorization now reach their intended target classes;
    targeting consults the current ability handler instead of a stale subsystem
    reference.
  This closes the linked-exile behavior for 8 sample slots and Nowhere to Run's
  protection behavior for 8 slots. Leyline Binding's separate Domain cost
  reduction remained open until Round 7.16. Regression-checked 162/162 scenarios
  after the round.
  **Round 7.16 (July 2026)** implemented the sample decks' outstanding casting
  costs and conditional reductions:
  * **Target-priced spells** - `Ride's End` and `This Town Ain't Big Enough`
    choose targets before affordability or payment. Their `{3}` reduction reads
    the committed tapped/friendly target, the action mask exposes casts that are
    affordable only after that reduction, and no mana or zone movement occurs
    while the target choice is pending. Ride's End also recognizes and exiles a
    noncreature Vehicle.
  * **Domain cost** - `Leyline Binding` reduces only generic mana for each
    distinct Plains, Island, Swamp, Mountain, and Forest type among lands its
    caster controls. Typed nonbasic lands contribute all their basic land types;
    duplicate types do not count twice.
  * **Nonmana casting costs** - `Fear of Isolation` exposes a mandatory
    non-target permanent choice and returns it to its owner's hand before the
    spell is stacked. `Analyze the Pollen` exposes sequential graveyard choices,
    verifies total mana value 8, exiles exactly the selected cards, and may be
    declined without moving them.
  * **Evidence-dependent resolution** - Analyze searches only for a basic land
    when evidence was declined and for a creature or land after evidence was
    collected. Search movement no longer duplicates a card already moved to
    hand by the library chooser.
  * **Shared cost repair** - a precomputed final cost dictionary is paid as-is;
    modifiers are no longer applied a second time inside `pay_mana_cost`.
  This closes nonmana additional costs for 10 sample slots and conditional cost
  reductions for 13 slots. Herd Migration's separate Domain effect value remains
  open. Regression-checked 168/168 scenarios after the round.
  **Round 7.17 (July 2026)** completed the five highest-priority support-audit
  items in their recommended order:
  * **Map and explore** - Map tokens carry their printed artifact subtype and
    activated ability. Target selection precedes mana, tap, and sacrifice
    payment; the ability survives its token source ceasing to exist. Explore
    handles land, nonland, and empty-library outcomes, with the nonland
    top-or-graveyard decision exposed to the agent. Get Lost preserves the
    destroyed permanent's controller for its two Map tokens.
  * **Floodpits Drowner** - its second ability requires a creature with a stun
    counter and shuffles source and target into their respective owners'
    libraries. Losing the stun counter before resolution makes the target
    illegal and leaves Drowner in place.
  * **Last-known information** - battlefield exits snapshot controller, owner,
    types, power/toughness, and token status before resetting the object.
    Heartfire Hero now deals its last-known power to each opponent when it dies.
  * **Emblems and Kaito** - persistent command-zone emblem records drive
    Kaito's cumulative Ninja anthem and Wrenn's graveyard land/permanent-spell
    permissions. Kaito's turn-and-loyalty condition changes it into only a 3/4
    Ninja creature with hexproof while retaining access to loyalty activation.
  * **Enduring Curiosity** - the dies trigger checks its creature and token
    snapshot, then returns the physical card under its owner as an enchantment
    with creature types removed.
  The integration work also repaired post-resolution sorcery timing, synthetic
  owner fallback, player-ID damage routing, and conditional keyword parsing.
  Regression-checked 178/178 scenarios after the round.
  **Round 7.18 (July 2026)** completed the next five support-audit items in one
  scenario-first pass:
  * **Mockingbird** - resolution exposes an optional bounded creature choice
    using total mana actually spent. The selected object contributes printed
    copyable values; counters and continuous effects are excluded, while Bird
    and flying are added before enters abilities trigger.
  * **Food** - exact colorless artifact Food tokens carry `{2}, {T}, Sacrifice
    this token: You gain 3 life.` The atomic activation transaction pays all
    costs and leaves the ability on the stack after the token ceases.
  * **Plot** - eight hand-index actions pay the parsed Plot cost as a
    sorcery-speed special action. A tracked permission blocks same-turn use,
    exposes a later free cast from exile, and is consumed on casting.
  * **Bargain and Torch the Tower** - casting exposes eligible artifacts,
    enchantments, and tokens or a decline action. Torch's atomic effect chooses
    2 versus 3 damage, conditionally scries, and registers its same-turn
    damage-linked death-to-exile replacement.
  * **Manifest dread and Turn Inside Out** - top-two selection, graveyard
    movement, one/zero-card edge cases, anonymous colorless 2/2
    characteristics, paid face-up restoration, and the exact target's
    same-turn delayed death trigger are represented end to end.
  Shared repairs found by these scenarios make generated action context reach
  handlers, constrain named self-ETB triggers to their own object, and let
  replacement conditions inspect live zone events without deep-copying thread
  locks. Regression-checked 187/187 scenarios after the round.
  **Round 7.19 (July 2026)** completed the audit's five recommended items in
  one scenario-first pass:
  * **Herd Migration's Domain value** - "for each basic land type among lands
    you control" resolves through a shared count_dynamic_quantity branch that
    counts DISTINCT basic land types (duals contribute each printed type), and
    CreateTokenEffect gained the same count_expr path variable draw/life/pump
    already use. The scenario also exposed that resolving any spell executed
    its printed activated-ability lines: Herd Migration's "{1}{G}, Discard
    this card: Search..." line discarded a card and gained 3 life on cast.
    Activated-ability lines are now stripped from spell resolution (CR 608.2).
  * **Fear of Missing Out's additional combat** - attack triggers now actually
    fire: handle_attack_triggers existed but had NO callers, and
    gs.attackers_this_turn was initialized and read (Boast legality, the
    dead-attacker observations) but never written. Declaring attackers done
    now fires each attacker's triggers with a first-attack-this-turn flag,
    "this creature attacks" gates to the attacker itself, "for the first time
    each turn" gates on the flag, and the Delirium intervening-if counts
    distinct card types in the graveyard. "After this phase, there is an
    additional combat phase" registers an extra combat that _advance_phase
    consumes instead of the postcombat main (CR 505.5a).
  * **Leyline of Resonance's opening hand** - after the last mulligan
    decision, each player with a "begin the game with it on the battlefield"
    card gets a real begin-game choice (starting player first, CR 103.6c),
    exposed through PHASE_CHOOSE actions 353-362 with PASS declining; the
    first turn is deferred until every placement resolves. The scripted
    opponent places its cards (first-legal-action policy).
  * **Screaming Nemesis's life restriction** - a DAMAGED trigger event class
    now exists ("is dealt damage" text never matched any event before, so
    enrage-style triggers were dead). The reflected damage reads "that much"
    from the trigger context, "any other target" excludes the source from
    legal targets, and a player dealt damage this way gets a rest-of-game
    cant_gain_life flag enforced by both gain_life and lifelink.
  * **Anoint with Affliction's Corrupted branch** - a ConditionalExileEffect
    checks "mana value 3 or less" at RESOLUTION (any creature is targetable),
    overridden when the target's controller has three or more poison
    counters. Both sentences stay one atomic effect.
  Shared repairs found by these scenarios: token subtypes outside the loaded
  pool's feature vocabulary were silently dropped (a "Beast token" had no
  Beast subtype unless some loaded card was a Beast), and mixed
  player/permanent target sets crashed SELECT_TARGET's plain sorted() with
  int-vs-str TypeError (latent for every "any target" burn spell).
  Regression-checked 194/194 scenarios after the round.
  **Round 7.20 (July 2026)** completed the next five support-audit items:
  * **Phyrexian Obliterator** - a "a source deals damage to this creature"
    trigger class routes through DAMAGED events (gated to the damaged object,
    excluded from source-side DEALS_DAMAGE matching), and the damage source's
    controller picks each sacrificed permanent through a new mandatory
    forced_sacrifice choice (actions 353-362, immediate per-pick sacrifice).
    The old PHASE_SACRIFICE staging machinery remains producer-less.
  * **Restless lands** - "this land becomes a N/N ... creature" self-animation
    parses as one atomic effect (P/T, colors, creature subtype, granted
    keywords, end-of-turn duration, still-a-land) through layers 4/5/6/7b, and
    all four sample riders work: Map token (Anchorage), Food plus up-to-one
    graveyard exile (Cottage), targeted four-card mill (Reef), and pump plus
    untap of another target attacking creature (Ridgeline).
  * **Sunfall / Incubate** - "Exile all creatures. Incubate X" is one atomic
    effect: every creature is exiled, and an Incubator token (a transforming
    DFC) enters with that many +1/+1 counters. Paying {2} transforms it into
    the 0/0 Phyrexian artifact creature whose counters carry over.
  * **Cavern of Souls** - "As this land enters, choose a creature type" opens
    a mandatory agent choice (options: the controller's own creature subtypes
    by frequency), the chosen type is substituted into the restricted mana's
    conditional-pool key so only creature spells of that type can spend it,
    and a cast paid with that mana is marked uncounterable on its stack item,
    which CounterSpellEffect now respects.
  * **Beza, the Bounding Spring** - its four independent opponent-comparison
    branches (lands/Treasure, life/4 life, creatures/two Fish, hand/draw)
    evaluate individually at resolution; named self-entry gating now also
    recognizes legendary short names ("When Beza enters" on the full card
    name), so the trigger no longer fires for every other creature entering.
  Silent bugs found by these scenarios, each now guarded: generic activated
  abilities were stacked with an EMPTY context, so every
  ability_handler.activate_ability resolution did nothing (Boast was doubly
  dead); generic creature tokens had NO card type unless their parsed name
  contained the word "creature" (Beast/Fish/Soldier tokens could never
  attack, block, or be creatures) and colored tokens were always colorless
  (Card reads WUBRG letters from "color_identity", not the "colors" vector);
  cast_spell's affordability/payment context omitted the card, so
  conditional "spend this mana only..." pools were unusable for every cast;
  and the targeting parser captured state adjectives as the target TYPE
  ("target attacking creature" parsed as type "attacking" with zero legal
  targets). Regression-checked 202/202 scenarios after the round.
  **Coverage status:** rounds 1-7.20 closed ~90 effect/mechanic classes across
  removal, bounce, counters, tokens, keywords, sacrifice, reanimation,
  control, mana, library manipulation, variable-count effects, prevention,
  animation, levelers, Adventure, and duplicate-ID zone semantics. Miss rate
  fell 6→13→14→9→10→3 across parser samples before the first-touch sweep moved
  into mechanic subsystems. Rounds 7.21-7.31 then closed the audited sample's
  remaining high-risk partials and used real-card warning probes to revive dead
  trigger, replacement, choice, and mechanic-entry paths. The current
  eight-deck sample has no known high-risk partial. New support work is ordered
  by real manifest counts and format-pool coverage, not speculative subsystem
  ordering.
- ◐ **First-touch coverage sweep**: one scenario for every subsystem that has
  never had one. This practice has repeatedly found phantom methods and dead or
  overfiring subsystems, so untested corners remain suspect. Next candidates
  come from real manifest counts, format-pool coverage, and the consolidated v1
  limitations below; there is no remaining sample-deck high-risk verification.
  **Round 7.21 (July 2026)** closed the requested seven-part sample-deck batch:
  Saddle, Duress/Oildeep hand choices, Cacophony Scamp's optional sacrifice,
  Leyline's single-friendly-target cast condition, Patchwork Beastie Delirium,
  Optimistic Scavenger Eerie events, the remaining real-card mechanic-entry
  sweep, and the exact-name per-card override registry. The sweep also fixed
  multi-symbol Ninjutsu cost truncation and unreachable Impending/Offspring
  cost parsing. Regression-checked 211/211 scenarios.
  **Round 7.22 (July 2026)** verified Beza's Treasure end to end and repaired
  the shared permanent mana-ability path: mana-producing activated text is
  promoted to ManaAbility, tap/sacrifice costs are paid atomically, variable
  color is policy-selected, and mana abilities resolve without the stack.
  Regression-checked 211/211 scenarios.

## Tier 3 — Training & environment quality

1. ✅ **Choice exposure audit**: spell, activated-ability,
   triggered-ability, and direct-effect targets are agent choices. Independent
   modal target slots, paged target lists, opponent trigger ordering,
   multi-target counter allocation, generic SacrificeEffect selection, Dig
   selection, and generic activated-ability sacrifice costs are complete.
   Non-self activation costs stage explicit, paginated permanent IDs before the
   shared cost transaction commits; self-sacrificing mana/token abilities keep
   their deterministic fast path.
2. ◐ **Opponent policy**: checkpoint/self-play policies install through
   `set_opponent_policy()`, receive their own observation and legal mask, and
   fall back safely when predicting an illegal action. Remaining: train a
   checkpoint that beats scripted play, then promote harvest to policy-vs-policy.
3. ✅ **Hidden-information audit**: `observation_for()` enforces a player
   perspective; changing unseen opponent hand identities and library order
   leaves every observation field unchanged. Face-down masking is also guarded.
4. ✅ **Replay logs**: seeded resets record actions, contexts, and deck names;
   `export_replay()` writes JSON and `replay()` verifies the selected decks
   before reproducing the episode.
5. ✅ **Deck legality validation**: `Playersim/deck_legality.py` validates
   minimum size, copy/basic-land rules, bans, restrictions, and format status;
   strict deck loading raises validation failures.

**Round 7.23 (July 2026):** delivered the requested Tier 3 items 3-7 batch.
Regression-checked 217/217 scenarios.

**Round 7.24 (July 2026):** closed the three named heuristic effect choices.
Counter distributions collect a legal allocation before committing counters;
SacrificeEffect routes each affected player's picks in sequence, supports
optional decline, and preserves reflexive-trigger gating; Dig exposes looked-at
identities only to its chooser. The three choices expose aligned card identity,
kind, remaining-pick, and staged-allocation observations, paginate beyond ten,
and pause compound effect resolution until each decision finishes.
Regression-checked 217/217 scenarios.

**Round 7.25 (July 2026):** closed the last Tier 3 choice-audit item and opened
the verification/harvest layer. Generic activated abilities now expose non-self
sacrifice costs to both policies, preserve target-before-cost ordering, preflight
composite costs without mutation, and route public action-mask activations and
legacy programmatic activations through the same payment implementation. The
new `harvest_fixtures.py` runs the audited eight-deck rotation only into a fresh
directory, rejects reset fallbacks/aborts, validates the stats contract, and
writes `harvest_run.json` only after gzip/JSON deck, card, meta, CardMemory,
game-log, and fidelity totals reconcile exactly. The strict run also exposed and
closed compounding deck aggregates, first-game/loser CardMemory omissions,
stale meta rates, transient-priority land actions, single-block dispatch, and
Adventure/MDFC mask confusion. Seed 20260710 completed 8/8 games with zero
fidelity counters and a clean support manifest (a plumbing/support baseline,
not a strength study). `tests/invariant_fuzz_test.py` checks 3 deterministic seeds
x 100 mask-valid actions for observation/mask validity, exact physical-card
conservation, SBA/layer idempotence, and mana-boundary clearing. It exposed and
closed phase-boundary mana retention and mask-valid combat-done dispatch bugs.
Regression-checked 225/225 scenarios, 10/10 harvest tests, and 3/3 fuzz seeds.

**Round 7.26 (July 2026):** audited the complete policy boundary across
`main.py`, the 480-action map, action-mask generation, dispatch contexts, and
the observation API. The audit repaired an all-zero production observation
path; strict observation-space bounds/dtypes and degradation reporting;
ten-card ordinary-spell/land accessibility; instant Adventure/MDFC timing;
non-active-player action ownership; context loss and unsafe TypeError retries;
alternative-cost indices and payment context; card-ID-zero handling (including
pending-cast and dredge choices); exact
post-action history; stack-card conservation across resolution continuations;
mask-valid combat, paging, loyalty, prevention, and mana actions; and
non-card mana-choice observations. Mask
generation is now non-mutating for paged choices, training/evaluation use
separate environments and statistics, callbacks/evaluation are mask-aware,
failures cannot be saved as final models, and training alternates both player
seats. Mask construction refuses to expose an unhandled action, and checkpoint
opponents can no longer fall back to scripted play after an illegal prediction.

The same round added failure-only atomic fuzz artifacts, exact action and
generated-context replay validation, deterministic state summaries,
short/default/long profiles, and a weekly/manual CI job.
It also delivered `harvest_protocol.py`: isolated parallel shards, global
deterministic schedule offsets, checkpoint SHA-256 identity, checkpoint-vs-
checkpoint play, aggregate throughput/fidelity manifests, paired-seat
promotion scoring, and fidelity/severe-manifest promotion gates. A final strict
two-worker plumbing run completed 2/2 games at 0.296 games/second with zero
fidelity counters; this tiny run validates orchestration, not throughput or
strength. Regression-checked 9/9 smoke stages, 10/10 training stages, 228/228
scenarios, 10/10 fixture-harvest tests, 5/5 protocol tests, and 6/6 fuzz/replay
tests. The default 8,000-action fuzz profile is green.
The final post-audit long profile is also green at 320,000/320,000 actions
across all 32 checked-in seeds, with no failure artifact.

**Round 7.27 (July 2026):** scoped attack-watcher triggers and made training
use the host machine. A probe showed the roadmap's "CREATURE_ATTACKS watchers
never fire" claim was stale in the worse direction: because check_abilities
scans every registered ability, watcher triggers fired through the single
ATTACKS dispatch with NO gating — the opponent's "whenever a creature you
control attacks" gained life off the wrong player's attacks, and "whenever a
Knight you control attacks" fired for a Bear. can_trigger's ATTACKS block now
scopes "a/another <type> [you control] attacks" watchers by attacker
controller, printed type/subtype/supertype, and self-exclusion for "another",
and routes "attacks you" wordings to the defending player only. The dead
per-permanent CREATURE_ATTACKS / CREATURE_ATTACKS_OPPONENT dispatch loops
were removed. Three guard scenarios. Regression-checked 231/231 scenarios,
9/9 smoke, 10/10 training, 10/10 + 5/5 harvest, 6/6 fuzz config, default fuzz
profile green. The same round replaced the CPU-only torch wheel with
2.12.1+cu130 (RTX 5060 / sm_120 verified with a live GPU op) and switched
multi-env training from DummyVecEnv to SubprocVecEnv with a learner
intra-op thread cap, so rollout collection parallelizes across cores instead
of serializing on one.

**Round 7.28 (July 2026):** qualified the reproducible Windows/CUDA training
pipeline and closed every engine failure found by its canaries. `main.py` now
accepts one root seed and records an atomic per-run `training_run.json` with
the CLI request, resolved configuration, train/evaluation worker seeds, device
and CUDA details, dependency inventory, deck/source hashes, Git revision and
dirty paths, a restorable working-tree patch, timings, evaluation history,
artifact hashes, and failure traceback when applicable. Multi-environment
rollouts use real Windows `spawn` `SubprocVecEnv` workers; evaluation remains
isolated. Incomplete runs write only `failed_model.zip`; successful runs first
write a pending model, reload it, perform a 256-step mask-aware validation with
period-1-through-4 cycle detection, then atomically publish `final_model.zip`.
Training and evaluation fail fast on engine-fidelity flags, mask-valid
execution failures, episode step limits, and repeated public-state/action
cycles. The training smoke suite includes a real two-worker spawned reset,
mask, legal-step, and close integration test.

The canary sequence exposed and closed nested-choice phase loss, reversible
attack/block selection loops, missing episode bounds/replay diagnostics,
process-unsafe failure contexts, deferred-target affordability/index drift,
and the final Torch the Tower loop. The Torch root cause was CR 601 ordering:
ordinary targeted spells paid costs before choosing targets, so Bargain could
sacrifice the only legal target and strand `TARGETING` on fallback `NO_OP`.
Every targeted spell now commits targets before mana, sacrifices, or stack
movement; permanent ETB targets are not mistaken for spell targets; Mutate
uses the same pre-cost path; and a focused public-mask regression preserves a
now-illegal bargained target so Torch fizzles cleanly. Patchwork Beastie's
"you may mill" is parsed as milling its controller rather than a phantom
target player. Policy-boundary diagnostics retain process-safe choice,
target, stack, and action context for any future long-run failure.

The final qualification run
`ALPHA_ZERO_MTG_V3.00_20260710_212719` completed 20,480/20,480 transitions
with two spawned training workers on the RTX 5060. It wrote checkpoints at
5,120, 10,240, 15,360, and 20,480; completed two strict four-episode
evaluations (mean rewards -21.62 and -30.79); reached 42.63 transitions/second
including evaluation/validation; peaked at 351,822,848 CUDA bytes allocated;
and passed final reload, finite-reward, nonempty-mask, mask-valid-prediction,
public-progress, and short-cycle validation. A downstream two-worker Harvest
smoke loaded the final model by SHA-256 and completed 2/2 games with zero
fidelity counters and a clean support manifest at 0.114 games/second. This is
pipeline qualification, not evidence of playing strength. Regression gates:
237/237 scenarios, 9/9 smoke stages, and 11/11 training stages; the post-fix
default fuzz profile also passed all 8,000 mask-valid actions.

**Round 7.29 (July 2026):** converted the final canary's highest-signal
warnings into generalized rules fixes and exact regressions. Compound player
instructions now retain an explicit shared subject, and discard choices pause
the shared effect sequencer without losing the underlying turn phase; this
restores Hopeless Nightmare's opponent life loss. `Sacrifice this <type>`
resolves against the source object only, so Hopeless Nightmare's activated
ability can no longer become a generic no-op or sacrifice a substitute.

`Mill N cards. You may put ... from among the milled cards` is now one atomic,
policy-visible effect. It mills the controller, offers only eligible cards
physically moved by that resolution, supports decline/pagination/cloned
continuations, and resumes suffixes such as Seed of Hope's life gain. Dredger's
Insight and Wrenn and Realmbreaker use the same path. A new graveyard-leave
event also makes Dredger's artifact/creature watcher function. Nurturing
Pixie's restricted optional bounce and result-dependent counter remain one
effect, with controller, nonland, and excluded-subtype targeting enforced.
Hyphens inside `non-Faerie`, `non-outlaw`, and P/T modifiers are no longer
mistaken for activated-ability separators.

The same warning pass removed duplicate Exhaust marking while retaining the
activation event, computes `avg_game_length` before first-save validation, and
suppresses only absent optional layer attributes whose calculated value is
`None`. A warning-enabled eight-deck reset now emits zero strict-separator,
legacy life-loss/mill/self-sacrifice, and missing-attribute warnings; six
repeated sample-card classification clauses remain the next fidelity audit.
Regression gates: 241/241 scenarios, 9/9 smoke, 11/11 training, and all 8,000
default-profile mask-valid fuzz actions.

**Round 7.30 (July 2026):** closed all six remaining sample-card
classification clauses; a warning-enabled eight-deck reset now emits zero
"could not classify" warnings. The shared root cause for four of them
(Overlord of the Mistmoors, Overlord of the Hauntwoods, Emberheart
Challenger, Manifold Mouse) was reminder-text stripping that consumed the
following newline, welding keyword-cost lines onto the next ability line;
stripping is now newline-preserving and Impending/Offspring cost lines are
recognized as handled. Guard scenarios exercise the REAL loaded cards (full
oracle text, faces, reminder text) rather than cleaned synthetic text.

Dead subsystems found live while wiring the remaining behavior, each of the
silent-bug-catalog pattern:
* **BEGINNING_OF_COMBAT had no can_trigger mapping** — dispatched every
  combat, never fired. Mapped, with "on your turn" gated to the ability
  controller's turns. **Every phase-beginning event was also dispatched
  twice**, queuing exact duplicates of each matching trigger; single
  dispatch now.
* **"target <subtype> you control" parsed as target type "target"** — zero
  legal targets, so such triggers silently vanished at stack time. Subtypes
  now pass through to the targeting system.
* **ETB-counter replacements were registered under ENTERS_BATTLEFIELD**, an
  event name move_card never applies — every "enters with ... counter"
  replacement was dead, and the modern wording (without "the battlefield")
  did not parse at all. Now registered under the live ENTER_BATTLEFIELD
  event with modern/symbolic counter parsing and a "for each <expr>"
  dynamic count. Making them live exposed two more latent bugs, both fixed:
  per-entry re-registration stacked duplicate effects (counter registration
  is now idempotent per source), and move_card extended the enter_counters
  list with itself, doubling every entry.

Card behavior delivered: Manifold Mouse's begin-combat trigger targets a
Mouse and pauses in PHASE_CHOOSE for the printed double strike/trample pick
(new `keyword_grant` choice, actions 353+); Obstinate Baloth's
opponent-caused discard puts it onto the battlefield through the shared
discard pipeline while self-caused and cleanup discards stay discards;
Callous Sell-Sword reads new per-player creatures-died-this-turn tracking
(turn-reset, setup-initialized) through count_dynamic_quantity, with face
text reachable for split/aftermath replacement scanning. Regression gates:
246/246 scenarios, 9/9 smoke, 11/11 training, 10/10 + 5/5 harvest, 6/6 fuzz
config, and the full default fuzz profile.

**Round 7.31 (July 2026):** the dead-subsystem sweep plus a strategic
planner/memory health check. Five more dead-or-overfiring paths, each of the
silent-bug-catalog pattern, now fixed and scenario-guarded:
* **Phase-trigger ownership** — "at the beginning of YOUR upkeep" fired on
  BOTH players' upkeeps (probe-confirmed, 2x rate inflation). Owner gating
  now covers your-upkeep/end-step/draw/precombat-main and routes
  "an opponent's upkeep" wordings to opponents' turns only.
* **End-step phase triggers never fired at all** — their patterns lived
  under END_OF_TURN, an event name nothing dispatches; the dispatcher's
  actual BEGINNING_OF_END_STEP event had no mapping.
* **The ENTERS_BATTLEFIELD registration alias was entirely dead** ("as
  enters" effects, printed ETB-tapped replacements). apply_replacements now
  merges the alias into the live ENTER_BATTLEFIELD application, and
  text-derived registration is idempotent per card per game — cleared again
  by remove_effects_by_source so phase-in rebuilds (the pre-existing 702.26
  phasing scenario caught exactly this interaction).
* **Impending was triple-broken**: the layer-4 "isn't a creature" effect
  removed 'Creature' from lowercase card_types (case-mismatch no-op); the
  end-step tick exists only in stripped reminder text on the Overlords (a
  synthesized trigger now registers for is_impending cards); and callable
  additional_condition functions CRASHED can_trigger, where the per-ability
  exception handler silently dropped the trigger — the same path Offspring's
  cost-paid condition rides. An impending-cast Overlord now enters as a
  4-time-counter non-creature, ticks only on its controller's end steps, and
  becomes a creature at zero.
* **strategy_memory.pkl was a hidden global input** — written to the process
  CWD by every game (tests included), loaded by every env construction, not
  gitignored, and feeding the rec/mem observation features: a silent
  reproducibility leak across seeded runs and a SubprocVecEnv file race. The
  memory file is now scoped under each env's storage directory; the stray
  root file is deleted and gitignored. The planner/memory functional probe
  (extract/update/suggest/save/load/analysis) passes; the construction-time
  "players not ready" warning is downgraded to expected-init debug noise.
Regression gates: 249/249 scenarios, 9/9 smoke, 11/11 training, 10/10 + 5/5
harvest, 6/6 fuzz config, and the full default fuzz profile.

**Round 7.32 (July 2026):** converted every signal from the first post-audit
strength-training attempt into a fix or an explicitly classified interruption.
Bug-log retention now closes each process's handlers and prunes again at exit,
so Windows spawn workers cannot leave more than five debug, warning, or error
files per family; rotating backups share the same five-file ceiling.

The run's engine findings are scenario-guarded: discard evaluation builds stable
cache keys across integer database IDs and string token IDs; game-level draw
sentinels no longer enter card lookup; optional `up to two` bounce resolves
successfully with zero targets; and Leyline of Resonance's `copy that spell`
copies the triggering stack object instead of falling back to an inert generic
effect. Runtime Card/GameState references are deliberately omitted from copied
stack context without warning. `Enchant`, opening-hand permissions, and printed
enters-tapped declarations are recognized as rules/replacement text rather than
dead continuous layer abilities. A user `KeyboardInterrupt` now preserves the
incomplete checkpoint and exit code 130 without reporting a blank training
error; the known SB3 wrapper-type false-positive warning is narrowly suppressed.

The real two-worker CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_022032` completed
128/128 training transitions and passed its 256-step reload/mask/progress/cycle
validation. Its newly written bug logs contain zero warnings and zero errors,
and the folder contains exactly five files per family. Regression gates:
253/253 scenarios, 9/9 smoke, 11/11 training, 10/10 + 5/5 harvest, 6/6 fuzz
config, and all 8,000 default-profile mask-valid fuzz actions.

**Round 7.33 (July 2026):** repaired and hardened the first longer strength run.
The run stopped after 11 completed worker-4 games when mask-valid action 19
(`PLAY_LAND` for hand slot 6) was rejected. Land masks now pin the observed
card ID, hand slot, and controller through execution, and the handler refuses a
stale/rebound slot instead of attempting a different card. Any mask-valid
execution failure now includes the card/seat/phase preconditions, compact
policy state, and an atomic replay artifact; opponent simulation stops at that
boundary so it cannot mutate the failed state before capture.

The same run exposed two operational faults. An evaluation environment closed
before its first reset no longer emits a false missing-player game error, and
network architecture output now lives under
`models/<run_id>/architecture/` instead of creating a second
`models/<run_id>_architecture/` sibling. The artifact manifest reads that same
contained path, and a training-stack regression asserts that architecture
recording leaves exactly one top-level directory for the run.

Warning triage is also closed: Plot, Saddle, and Mockingbird's copy-as-enters
declarations are handled by their dedicated mechanics and no longer register
as dead static/layer abilities. Ceased tokens retain last-known card
characteristics outside `card_db`, allowing their already-stacked abilities and
death triggers to resolve without false `Failed to find card` warnings. The
action-19 regression plays all 33 real lands in the audited pool from slot 6,
including Cavern, Restless lands, fast lands, pain lands, typed tapped lands,
and Verges.

The real two-worker CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_025239`
completed 128/128 transitions and passed its 256-step checkpoint reload,
mask-valid prediction, progress, finite-reward, and short-cycle validation.
It exercised six real land plays (including hand slot 7) with zero new warning
or error records. `models/` contains only `baselines/` and that single canary
run directory; its architecture summary is inside the run. Gates: 255/255
scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest, 6/6 fuzz/replay
configuration tests, and all 8,000 default-profile fuzz actions.

**Round 7.34 (July 2026):** the next six-worker strength attempt supplied the
missing worker-local evidence for action 19. Restless Anchorage was in hand
slot 6, its controller was the active player with priority, the stack was
empty, and no land had been played—but the engine was in transient
`PHASE_PRIORITY` over an underlying main phase. Mask generation correctly used
the canonical sorcery-speed predicate and exposed the play; `play_land()` used
a narrower literal-phase check and rejected it. Land execution now uses the
same canonical timing predicate as the mask while retaining its priority check.
The exact transient-priority case is part of the action-19 regression.

The failure also proved the new replay path could be interrupted by NumPy
`int64` values in action history. Policy diagnostics now normalize action IDs,
replay export recursively preserves its full structure while converting NumPy
scalars/arrays, and a failed write removes its temporary file. The forced
execution-failure regression includes a NumPy action and verifies the complete
JSON replay, diagnostic, and final action.

The deterministic six-worker CUDA rerun
`ALPHA_ZERO_MTG_V3.00_20260711_031143` used the failed run's seed and completed
the full first rollout: 12,288/12,288 transitions at 127 FPS, followed by the
256-step checkpoint reload/mask/progress/cycle validation. It passed the
original worker-4/action-19 point and wrote no new warning or error records.
Gates: 255/255 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest,
6/6 fuzz/replay configuration, and 8,000/8,000 default-fuzz actions.

**Round 7.35 (July 2026):** repaired the next strict-evaluation failure from
run `ALPHA_ZERO_MTG_V3.00_20260711_031708`. Action 21 exposed Hopeless
Nightmare from hand slot 1 while player 2 had priority, the stack was empty,
and `PHASE_PRIORITY` wrapped `MAIN_PRECOMBAT`; mask generation correctly
allowed the permanent spell, but `_can_cast_now()` still required a literal
main phase. Spell execution now shares the canonical sorcery-speed predicate
with mask generation. The check is also face-aware for modal double-faced and
Adventure cards, so timing follows the face actually being cast.

Ordinary `PLAY_SPELL` actions now pin hand slot, card ID, and controller through
execution, reject stale slots, and report full card/phase/seat context if a
future mask-valid cast fails. Replay schema v2 records the agent seat and
restores the exact named P1/P2 decks independently of deck-list ordering. The
historical 48-action failure replay now restores GolgariMidrange vs DimirSelf,
player 2 as the agent, and executes the formerly failing Hopeless Nightmare.

The exact failed checkpoint was resumed in six-worker CUDA canary
`ALPHA_ZERO_MTG_V3.00_20260711_032854`. It completed a 12,288-transition
rollout, one four-episode periodic evaluation, and the 256-step checkpoint
reload/mask/progress/cycle validation. The manifest reports both evaluation
and final validation passed, and the run created no warning or error records.
Gates: 256/256 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest,
6/6 fuzz/replay configuration, and 8,000/8,000 default-fuzz actions.

**Round 7.36 (July 2026):** analyzed the first strength run to reach three
periodic evaluations, `ALPHA_ZERO_MTG_V3.00_20260711_033606`. It completed
86,016 learner transitions, 332 training games, 12 evaluation games, and three
checkpoints before environment 2 stopped in a mandatory scripted-opponent scry
choice. The mask correctly exposed `PUT_ON_TOP`/`PUT_ON_BOTTOM`, but the
scripted CHOOSE fallback only attempted Pass, which is not legal during scry.
The baseline now conservatively keeps scry/surveil/explore cards on top (with
legal destination fallbacks). The exact learned-P2/scripted-P1 scry ownership
case is scenario-guarded.

The run is useful diagnostic data, not harvest-quality strength data. Of 332
training games, 331 reached the turn-21 cap; only 47 were wins, 116 losses, and
169 draws/draw flags. Periodic mean reward fell from +20.22 at 24,996 steps to
-34.38 at 49,992 and -46.95 at 74,988, even while rollout reward improved from
-52.26 to -31.01 and explained variance rose from -1.97 to 0.856. The learner
therefore fit the shaped training return without demonstrating stronger
evaluation play. Seat exposure was exactly balanced (166 games each), and all
recorded fidelity counters were zero.

The persisted data also reopened Tier 0. Across all seven train/evaluation
scopes, 1,057 deck-card rows report zero drawn/opening-hand observations, and
all 770 CardMemory rows have zero `times_drawn` and `in_opening_hand`; 363 rows
record plays while every `turn_played` map remains empty. The environment reads
`GameState.opening_hands` and `draw_history`, but those histories are never
populated; CardMemory also attempts to infer play turn from draw history instead
of using the already-recorded play history. Do not use drawn-vs-not-drawn,
opening-hand, turn-played, or derived card-effectiveness fields until this
telemetry is implemented and production-artifact tested. The authoritative
per-game outcomes, deck identities, clean fidelity counters, and existing play
histories remain useful for diagnostics.

Gates after the scry repair: 257/257 scenarios, 9/9 smoke, and 12/12 training
smoke. Next: repair and fixture-test draw/opening telemetry, then resume the
best checkpoint rather than the degraded failed checkpoint.

**Round 7.37 (July 2026):** closed the Tier 0 telemetry regression and rebuilt
the training signal exposed by Round 7.36. GameState now captures the exact
post-mulligan opening hands, routes every normal draw through one replacement-
aware draw method, records draw turns without double-counting Miracle, and
persists the already-authoritative play history rather than inventing play
turns from draws. Terminal cause is explicit in every game record. The real
six-worker CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_042819` persisted 206
opening-hand observations, 289 CardMemory draws, 414 deck-stat draw
observations, and 158 plays spanning turns 1–20 across 18 completed games.

Training reward is now a change in bounded strategic potential plus one
perspective-correct terminal reward; the old absolute board reward and duplicate
terminal/state-change additions no longer reward the same board repeatedly.
Turn-limit outcomes receive smaller rewards than natural wins/losses and draws
are mildly negative. The scripted opponent now plays lands, casts affordable
spells, and attacks/blocks instead of normally passing every priority. Reward
components and terminal reasons are exported to TensorBoard.

The resource monitor samples in a background thread through PPO learner bursts
and exports process-tree CPU/RAM, GPU utilization/memory/temperature, and CUDA
allocation. Per-game full-memory rewrites were removed or batched. The canary
completed 6,144 transitions plus the 256-step reload/mask/progress/cycle check
with no errors at 111 rollout FPS; GPU use reached 28% and CUDA reservation
reached 0.83 GiB. All 18 short-run games still reached the turn limit, so this
checkpoint remains diagnostic only: natural-terminal rate is the first strength
metric for the next run. Generic Ward's internal layer form and canonical draw
labels were cleaned up after this canary so they no longer create warning or
non-schema result categories.

Post-cleanup micro-canary `ALPHA_ZERO_MTG_V3.00_20260711_043821` completed 128
CUDA transitions and final validation with no warning/error file. The corrected
spawned-worker sampler reported 37.9% mean and 83.2% peak child-process CPU,
confirming that the previous all-zero worker chart was instrumentation failure,
not idle workers.

Gates: 262/262 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest,
6/6 fuzz/replay configuration, and 8,000/8,000 default-fuzz actions. Because
the reward function and baseline policy changed materially, start the next
strength candidate fresh rather than resuming a pre-7.37 checkpoint.

**Round 7.38 (July 2026):** the first eight-worker strength run on the new
reward, `ALPHA_ZERO_MTG_V3.00_20260711_110933`, completed 32,768 learner
transitions, 134 training games, and its first 16-game evaluation before strict
fidelity stopped an impossible Bushwhack choice. Mode masks exposed both
printed modes even though the fight mode required two differently controlled
creatures and only one legal target existed. Modal choices now validate the
combined selected-mode target text both while generating the mask and again at
execution. The exact 270-action failure replay now reaches the same state and
classifies action 354 as mask-invalid instead of failing execution.

The same run surfaced two value-changing warning paths. Caustic Bronco's attack
trigger had split into generic no-op fragments; it now moves the revealed top
card to hand and makes the correct player lose that card's mana value depending
on whether Bronco is saddled. Subjectless spell instructions such as Three
Steps Ahead's `discard a card` now bind to the spell controller instead of
requesting a nonexistent target-player ID. All three repairs have exact-card
scenarios. The run remained diagnostic—133/134 training games and all 16
evaluation games reached the turn limit—though it produced the first natural
life-total ending under the new baseline.

The eight-worker CUDA resume canary `ALPHA_ZERO_MTG_V3.00_20260711_112859`
loaded that exact best checkpoint, advanced it from 32,768 to 40,960 total
timesteps in one 8,192-transition rollout at 123 FPS, and passed the 256-step
checkpoint reload/mask/reward/progress/cycle validation. It recorded 25 games
(24 turn-limit, one natural life-total ending) and created no warning or error
file. Gates: 265/265 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest,
6/6 fuzz/replay configuration, and 8,000/8,000 default-fuzz actions.

**Round 7.39 (July 2026):** strength continuation
`ALPHA_ZERO_MTG_V3.00_20260711_113539` found one strict failure and two warning
paths. In an EsperSelf mirror, both physical permanents shared numeric ID 57;
Fear of Isolation's mask correctly offered Player 2's occurrence, but execution
used a global Player-1-first controller lookup and rejected it. Mandatory return
costs now treat membership in the choosing controller's battlefield as the
authoritative occurrence and route an ambiguous mirror-owned object back to
that controller. The exact 121-action replay now casts Fear successfully and
records no mask-valid execution failure.

Deferred casting choices now preserve both the transient `PRIORITY` phase and
its underlying main phase. This prevents Mockingbird from passing its initial
sorcery-speed timing check and then failing that same check after choosing X.
Real triggered-ability resolution now also supplies the source card name to the
effect factory, so exact-card overrides such as Caustic Bronco's linked attack
effect are used in matches, not merely in direct parser tests.

CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_115902` resumed the failed run's
best checkpoint and completed 8,192 transitions at 130 rollout FPS, including
one natural life-total ending. Checkpoint reload and the 256-step
mask/reward/progress/cycle validation passed, and the run created no warning or
error file. Gates: 267/267 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5
Harvest, 6/6 fuzz/replay configuration, the exact failure replay, and
8,000/8,000 default-fuzz actions.

**Round 7.40 (July 2026):** strength run
`ALPHA_ZERO_MTG_V3.00_20260711_120752` exposed a recursive Pawpatch Recruit
target trigger. The engine enforced neither side of its controller relationship,
so the friendly target selected for Recruit's own trigger was incorrectly
treated as a new opponent-controlled targeting event. This accumulated 1,345
triggers, overflowed the public `stack_count` observation, and eventually hit
the strict period-1 cycle guard. Target events now require the watched creature
to belong to Recruit's controller and the targeting object to belong to an
opponent. The trigger also excludes the original targeted creature from its
own mandatory target choice.

The warning sweep closed three adjacent paths. Bushwhack's search mode is now
one atomic search rather than a valid search plus an unimplemented `put it into
your hand` fragment. Parameterized Ward's internal `ward <cost>` form registers
as a layer-6 keyword without a parser warning. Drawing from an empty library is
logged as the ordinary rules-defined decking result rather than an engine
warning. Observation-bound warnings are emitted once per episode, and failure
diagnostics cap stack detail at 32 entries while retaining the true stack size,
preventing a runaway state from producing another multi-megabyte traceback.

CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_123455` resumed the failed run's
best checkpoint and completed 8,192 transitions at 154 rollout FPS. Checkpoint
reload and the 256-step mask/reward/progress/cycle validation passed, with no
new warning or error file. Gates: 269/269 scenarios, 9/9 smoke, 12/12 training,
10/10 + 5/5 Harvest, 6/6 fuzz/replay configuration, and 8,000/8,000
default-fuzz actions.

**Round 7.41 (July 2026):** strength run
`ALPHA_ZERO_MTG_V3.00_20260711_124331` found the remaining deferred-cast timing
path. Duress began legally during transient `PRIORITY` over Player 2's main
phase, but staging its opponent target overwrote `previous_priority_phase`.
After the target was selected, the resumed sorcery saw bare priority with no
underlying main phase and rejected its mask-valid action 274.

Targeted casts now snapshot and restore both the visible phase and underlying
priority phase, matching the casting-choice repair from Round 7.39. The exact
122-action EsperSelf-vs-GolgariMidrange replay now completes the target action,
puts Duress on the stack, and records no execution failure. CUDA canary
`ALPHA_ZERO_MTG_V3.00_20260711_125649` resumed the failed run's best checkpoint
and completed 8,192 transitions at 139 rollout FPS. Checkpoint reload and the
256-step mask/reward/progress/cycle validation passed with no new warning or
error file. Gates: 270/270 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5
Harvest, 6/6 fuzz/replay configuration, the exact failure replay, and
8,000/8,000 default-fuzz actions.

**Round 7.42 (July 2026):** strength run
`ALPHA_ZERO_MTG_V3.00_20260711_125937` exposed Floodpits Drowner's shuffle
ability as mask-valid with no creature bearing a stun counter. Execution already
enforced the required target and rejected action 118; the shared mask predicate
checked only whether the mana/tap costs were payable. `can_activate_ability`
now also requires at least one legal target for every targeted activated
ability whose minimum target count is nonzero.

The exact 212-action DimirSelf mirror replay now masks action 118; forcing the
recorded action is handled as an ordinary invalid action rather than a fidelity
failure. The existing exact-card Drowner scenario proves both mask directions:
the action is absent without a stun target and appears as soon as one exists.
CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_131724` resumed the failed run's
65,536-step best checkpoint, advanced it to 73,728 steps at 147 rollout FPS,
and passed checkpoint reload plus the 256-step mask/reward/progress/cycle
validation with no new warning or error file. Gates remain 270/270 scenarios,
9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest, 6/6 fuzz/replay configuration,
the exact failure replay, and 8,000/8,000 default-fuzz actions.

**Round 7.43 (July 2026):** run analysis — games never concluded
Analysis of `ALPHA_ZERO_MTG_V3.00_20260711_132035` (131,072 steps, clean
telemetry) showed the stats were structurally worthless: 98.5% of the 532
recorded games ended at the turn limit, 43% were draws, only 4 games in the
whole run ended by life total, GruulProwess went 0-for-50, and every Domain
game was a turn-limit draw. Probes reproduced whole 20-turn games in which
casting a spell was never once mask-legal. Four root causes, all fixed:

1. *Cast-time targeting over-blocked permanents.* `_targets_available`
   demanded a live target whenever a card's oracle text contained the word
   "target" anywhere — including triggered abilities and reminder text, which
   never gate casting (CR 601.2c). On an empty board this made most creatures
   uncastable and shut GruulProwess (Valiant text on its whole curve) out of
   the game. The mask now enforces cast-time targets only for instants,
   sorceries, and Auras.
2. *The scripted opponent could never cast.* Affordability is checked against
   floating mana and the opponent's priority list never tapped a land, so it
   passed every game action forever (hand pinned at 7, discarding each turn
   cycle). It now taps lands in its own main phases and resolves the
   `choose_mode`/`land_mana` choices duals raise.
3. *Casting needed a learned tap-then-cast dance.* Mask affordability and
   every cast_spell gate now count the player's untapped lands
   (`can_pay_mana_cost_with_lands`), and payment auto-taps the planned lands
   (exact augmenting-path matching for colored/hybrid pips, damage-free
   options preferred, restricted outputs skipped, duplicate-id fixture copies
   planned once — the last found by the default fuzz profile before delivery).
   A policy-contract scenario proves an empty pool casts from untapped lands.
4. *"Exile it instead" riders were dead outside Torch the Tower.* Obliterating
   Bolt and Elspeth's Smite fell through to the no-op base effect (the run's
   single warning). A generalized `DamageWithExileReplacementEffect` registers
   the end-of-turn DIES→exile replacement, planeswalker-aware per the rider's
   wording, with immediate-kill and delayed-death guard scenarios.

`max_turns` also rose 20 → 30: with both seats actually playing, aggro
pressure reached lethal around turn 25, past the old cap. Scripted-vs-scripted
probes now end by life total or decking instead of turn-limit adjudication.
Gates: 273/273 scenarios (three new), 9/9 smoke, 12/12 training, 10/10 + 5/5
Harvest, 6/6 fuzz/replay configuration, and 8,000/8,000 default-fuzz actions.

**Round 7.44 (July 2026):** strength run
`ALPHA_ZERO_MTG_V3.00_20260711_145919` exposed Three Steps Ahead as an
ordinary mask-valid cast even though the engine does not yet implement Spree's
per-mode additional costs, target collection, and combined resolution. The
real card now parses all three printed modes, but Spree spells are deliberately
mask-excluded and recorded once per card object as `unparsed` support gaps until
that complete casting flow exists. This converts a fatal policy-contract breach
into explicit deck-builder exclusion rather than pretending the card is safe.

The same run's warning families led to six shared repairs. Targeted triggered
and loyalty abilities now collect committed targets before resolution and
fizzle cleanly when a mandatory target disappears, while targetless triggers
do not enter that path. Combined keyword clauses such as Oildeep Gearhulk's
`lifelink, ward {1}` no longer register duplicate static/layer effects. Draw
effects treat physical card id `0` as valid and regard an empty-library draw as
a successfully executed rules event. Exact scalar observations now retain
legal boards above the 20-slot detail tensor instead of degrading or warning on
them, and the first observation error remains sticky for the episode. Finally,
a failure inside the scripted-opponent loop writes a deterministic replay for
the agent action that entered that loop.

The fresh CUDA canaries then exercised paths that the synthetic fixtures had
not reached. Card observations now use the complete immutable 225-field schema
for this pool (including all 48 subtype fields and MDFC fields), preserve legal
signed live power/toughness, and validate component-specific bounds instead of
silently truncating vectors. Zur's exact static ability grants deathtouch,
lifelink, and hexproof only to its controller's enchantment creatures, including
later entrants, and removes those grants when Zur leaves. Tracker format 3.2
normalizes card/archetype prevalence by the two deck seats in every match;
TensorBoard uses policy timesteps as its single x-axis and exports cumulative
terminal counts alongside per-timestep rates.

Mosswood Dreadknight now recognizes its real two-face Adventure data. Its dies
trigger grants the printed graveyard Adventure permission through the end of
the controller's next turn, the cast uses Dread Whispers' cost and effect, and
successful resolution exiles the card with the normal creature-side recast
permission. Finally, the exact turn-16 replay from failed canary
`ALPHA_ZERO_MTG_V3.00_20260711_160213` proved that an ordinary BLOCK action
could strand one blocker on Harvester of Misery. Menace now starts blocking
through the atomic multi-block action, ordinary block masks bind the exact
attacker they validated, incomplete legacy declarations expose an undo, and
action 439 is present exactly when its completion handler can execute.

Failed canary `ALPHA_ZERO_MTG_V3.00_20260711_163807` then exposed action 294:
Overlord of the Hauntwoods' Impending `{1}{G}{G}` replacing cost was not parsed,
and its sparse cost mapping reached a `KeyError: 'W'`. Cost boundaries now
normalize the complete mana schema, and both the mask and handler price the
actual alternative cost with taxes, reductions, and land auto-tapping.
Convoke, Delve, and Improvise reductions are produced exactly once instead of
being applied again during affordability checks.

The corrected observation shape and scalar bounds change the declared Gym
observation space. Stable-Baselines checkpoint compatibility includes both, so
checkpoints created before Round 7.44 must not be resumed. Fresh eight-worker
CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_165824` completed 8,192/8,192
transitions at 99 rollout FPS and recorded 14 terminal games (3 decking, 3 life
total, 8 turn limit). Final validation passed with no warning or error file.
Gates: 287/287 scenarios, 9/9 smoke, 12/12 training, 11/11 + 5/5 Harvest, 6/6
fuzz/replay configuration, the exact failure-state regressions, and 8,000/8,000
default-fuzz actions.

**Round 7.45 (July 2026):** Spree is now a real casting transaction instead of
a blanket exclusion. The policy must announce one or more distinct modes; the
mask prices each next choice against the cumulative base cost plus every
chosen mode's additional cost and requires its mandatory targets to exist.
Final casting applies taxes and reductions once, auto-taps eligible lands,
pays one combined cost, and commits an independent target slot for each
targeted mode. Forged, duplicate, unaffordable, and zero-mode announcements are
rejected without moving the card or spending mana.

Resolution retains the selected modes in printed order, revalidates their
targets independently, skips a targeted mode whose targets all became illegal,
and makes the whole spell fail to resolve only when every target of the spell
is illegal.
Choice-producing effects can pause and resume the remaining mode sequence.
Three Steps Ahead is covered exactly across all seven non-empty mode
combinations: counter target spell; create a printed-value token copy of a
controlled artifact or creature; and draw two, then make its controller discard
one. Its tenth-hand-slot and third-mode actions are addressable, and it no
longer creates an `unparsed` support-manifest entry. The same targeting audit
now keeps creature spells on the stack out of creature-permanent target lists,
guarded through an Anoint with Affliction zone regression.

This closes the generic Spree announcement/payment/targeting/resolution
transaction and Three Steps Ahead's exact effects. It does **not** certify the
effect semantics of all 21 Spree cards: each other card remains eligible only
as its chosen modes parse faithfully and pass card-specific scenario/manifest
evidence. Malformed Spree mode text remains an explicit support gap.

Gates: 295/295 scenarios, 9/9 smoke, 12/12 training, 11/11 + 5/5 Harvest, 6/6
fuzz/replay configuration, and 8,000/8,000 default-fuzz actions. Exact-source
CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_174419` completed 8,192/8,192
transitions at 105 rollout FPS with 16 terminals (2 decking, 4 life total, 10
turn limit). Final checkpoint reload validation passed all 256 steps, including
mask-valid prediction, finite rewards, public progress, and four short-cycle
checks; the run emitted a debug log only, with no warning or error file.

**Round 7.46 (July 2026):** telemetry hardening
`ALPHA_ZERO_MTG_V3.00_20260711_205313` died 69 seconds in when its
system-metrics TensorBoard event file disappeared mid-run (the
`tensorboard_logs` directory was cleaned while the run was writing).
`ResourceMonitorCallback._on_step` was the only telemetry path that could
take a training run down: the one-second background sampler already swallows
its own failures. The per-step monitor body now does the same — a lost or
rotated event file costs metrics, never the run. Exact-seed replay
`ALPHA_ZERO_MTG_V3.00_20260711_205741` (seed 20260715, 8 workers) then
completed 8,192/8,192 transitions with validation passed, 14 recorded games
(9W/5L, no draws; 1 life-total, 2 decking, 11 turn-limit terminals), and a
debug log only. Gates: 295/295 scenarios, 9/9 smoke, and the 8-seed default
fuzz profile.

## Tier 4 — Verification & calibration

1. ✅ Golden scenario harness — 315 scenarios and growing; scenario-first is a
   working agreement, not a suggestion.
2. ✅ **Property/invariant harness**: exact non-token zone/stack conservation,
   SBA fixed points, mask-valid action execution/handler coverage, declared
   observation bounds and degradation checks, observation/info mask agreement,
   repeated mask purity, finite rewards, phase-boundary mana clearing, and
   repeated layer idempotence run under fixed seeds in
   `tests/invariant_fuzz_test.py`.
3. ✅ **Long-game fuzzing**: short (3 x 100), default (8 x 1,000), and long
   (32 x 10,000) profiles exist. Failures are written atomically with the seed,
   exact action/context history, state summary, and a one-command `--replay`
   path; successes leave no artifact directory. Weekly/manual CI runs the long
   profile and retains failure artifacts for 14 days. The final local run on
   the post-audit engine snapshot passed all 320,000 actions with no artifact.
4. ▢ **Calibration study**: 3–5 deck pairs with well-known matchup winrates;
   run at harvest scale; compare. This is the acceptance test for the whole
   pipeline and gates "harvest at scale."

## Tier 5 — Harvest operations & deck-builder integration

1. ◐ **Throughput**: isolated parallel workers and aggregate games/second
   telemetry are implemented. The trained-checkpoint 2-game/two-worker smoke
   measured 0.114 games/second; this tiny run validates loading and orchestration,
   not capacity. Profile a production-size checkpoint harvest, then optimize
   measured hot paths.
2. ✅ **Harvest protocol**: `harvest_fixtures.py` supplies strict deterministic
   shards and `harvest_protocol.py` supplies parallel operation, checkpoint
   loading/identity, aggregate success-only manifests, paired-seat candidate
   scoring, and promotion gates. The protocol is complete and regression-
   tested. The 20,480-transition CUDA canary now proves checkpoint loading from
   training through Harvest, but it is intentionally only a pipeline candidate.
   Operational next step: train a longer strength candidate, freeze a baseline
   checkpoint, then run the first paired-seat promotion and calibration study.
3. ▢ **Deck-builder contract**: `STATS_SCHEMA.md` + support manifest are the
   full interface; the builder's exclusion logic and confidence weighting
   consume them directly.
4. ▢ **Feedback loop**: builder-proposed decks auto-enter the harvest queue;
   their novel cards populate the manifest; support work is prioritized by
   what the builder actually wants to play.

### Target-format program — Standard, Modern, Pioneer

The narrowed constructed scope is **Standard first, Modern second, and Pioneer
third**. A "format agent" means one format-specialist policy and checkpoint
league trained across a representative corpus of decks for that format, not a
policy tied to one deck. Each policy may use multiple rollout or Harvest
workers, but its deck corpus, checkpoints, statistics, support observations,
and promotion/calibration artifacts remain in an isolated format namespace.
Statistics from different formats must never be merged merely because a card
or deck name appears in both.

All three format pipelines will share one versioned canonical-card registry
(stable Oracle identity rather than run-local integer IDs), one frozen
observation/action feature schema, and one format-parameterized deck builder.
The registry, format card-pool snapshot, deck-corpus snapshot, and feature
schema each need a recorded version/hash so adding a builder candidate cannot
silently change model input width or invalidate a checkpoint.

What exists today is useful foundation, not three completed format agents:
the three format card-list snapshots exist; deck loading has format-legality
hooks; the current eight-deck sample can bootstrap Standard; training can use
multiple environment workers; and Harvest can run isolated shards and
paired-seat checkpoint promotion. As of Round 7.46, training and Harvest
accept explicit format/corpus configuration, run-level manifests carry
format/pool/corpus/registry/schema lineage, and production Harvest is no
longer hard-coded to the audited eight decks. Round 7.52 added user-deck
ingress: one supplied list is legality-checked against the pinned snapshots,
its matching format(s) are detected, and it is added to that format's isolated
recursive deck pool. This is an input path, not the automatic deck-builder
feedback queue. Still open: the scripted opponent remains the training
baseline, no representative Modern or Pioneer training corpus exists, and
builder candidates do not yet enqueue themselves. A clean failure manifest
also does not prove that an unseen format card was simulated faithfully;
coverage must distinguish unseen, observed-clean, verified, partial, and
excluded cards.

Phased milestones:

1. ✅ **Format foundation and lineage** (Round 7.46, July 2026): training
   (`main.py`), fixture Harvest, and the parallel Harvest/promotion protocol
   all accept explicit `--format`, `--decks`, and `--format-dir`
   configuration with strict legality; `Playersim/card_registry.py` provides
   the canonical card registry (stable, append-only integer indices keyed by
   name + Scryfall oracle_id) and the frozen, versioned feature schema, both
   self-hashed and created by `python -m Playersim.card_registry freeze`;
   every run-level manifest (`training_run.json`, `harvest_run.json`,
   `harvest_protocol.json`, `promotion.json`) stamps a `lineage` object with
   format, pool-snapshot hash, corpus hash, and registry/schema
   version+hash, alongside the existing git/policy/checkpoint identities;
   production Harvest is generalized beyond the hard-coded sample fixture
   while the no-argument fixture remains the regression gate. See
   `STATS_SCHEMA.md` "Format namespaces and run lineage" for the consumer
   contract. `formats/standard/` now covers every one of the 4,702 legal
   English cards in the pinned Standard snapshot plus 28 retained bootstrap
   identities (4,730 registry entries total). The v2 feature schema has 259
   subtypes and feature_dim 436; the original 110 indices remain unchanged.
2. ◐ **Standard end to end**: the representative corpus is pinned, hydrated,
   and can be extended with validated imports. Continue closing impact-ranked
   support gaps, qualify the Standard policy against scripted play, promote it
   into a checkpoint league, calibrate known matchups, and produce the first
   format-isolated, fidelity-clean strength harvest.
3. ▢ **Modern end to end**: assemble a separate strictly legal Modern corpus,
   triage its observed support gaps, then repeat qualification, league
   promotion, calibration, and production harvest in the Modern namespace.
   Do not assume every current sample deck is Modern legal.
4. ▢ **Pioneer end to end**: assemble a separate strictly legal Pioneer
   corpus and pass the same support, strength, calibration, and Harvest gates
   in the Pioneer namespace.
5. ▢ **Unified builder feedback**: make one builder accept a format as an
   explicit input and consume only that format's legal pool, version-matched
   support ledger, fidelity-clean qualified-policy statistics, matchup data,
   and uncertainty/confidence. Builder candidates enter the affected format's
   support preflight and paired-seat evaluation queue; accepted candidates feed
   its training/Harvest corpus without contaminating held-out promotion or
   calibration results.

**Current execution order:** the format foundation, full Standard namespace,
representative metagame corpus, recursive pool layout, and validated user-deck
ingress now exist. Continue the impact-ranked Standard support sweep, then
qualify the Standard policy against scripted play, promote it into a checkpoint
league, and calibrate known matchups. Imported lists can widen the working pool
without overwriting the pinned metagame, but builder-driven queueing remains a
later milestone. Reuse the qualified pipeline for Modern and then Pioneer, and
finally enable the unified automatic builder feedback loop. Production-size
throughput profiling and calibration occur as gates in each format rather than
as one mixed-format exercise. New fidelity failures discovered along that path
pre-empt strength and integration work in the affected format.

**Round 7.46 (July 2026)** delivered the format foundation end to end,
scenario-first (18 new `tests/card_registry_test.py` tests plus new harvest,
protocol, and training-smoke coverage):
* **Canonical card registry** — `Playersim/card_registry.py` builds a
  name-ordered, append-only registry keyed by card name + Scryfall
  `oracle_id` (Card now retains `oracle_id`); the loader's optional
  `card_registry` parameter makes those indices the engine `card_id`s, and a
  corpus card missing from the registry (or with a conflicting oracle_id) is
  always a fatal `CanonicalRegistryError` — never a skipped deck or backup
  fallback.
* **Frozen feature schema** — the exact base/cost/keyword/color/subtype/MDFC
  layout with `feature_dim`, self-hashed and versioned. Loading under it
  installs the frozen subtype vocabulary instead of the pool-derived one and
  rejects out-of-vocabulary cards loudly, so adding a deck can no longer
  silently change model input width. An engine-compat guard rejects schemas
  whose keyword list differs from `Card.ALL_KEYWORDS`.
* **Freeze CLI** — `python -m Playersim.card_registry freeze` writes
  `formats/<format>/card_registry.json` + `feature_schema.json` from a
  strictly loaded corpus; `--extend` appends new cards without renumbering
  and refuses cards that would require a schema width change.
* **Lineage everywhere** — `training_run.json`, `harvest_run.json`,
  `harvest_protocol.json`, and `promotion.json` all carry
  format/pool/corpus/registry/schema identities; parallel Harvest fails
  before publishing if shards disagree on lineage.
* **Generalized Harvest** — `harvest_fixtures.py` and `harvest_protocol.py`
  accept `--decks`/`--format`/`--format-dir`; artifact validation checks
  deck labels against the actual corpus. Verified live: a 1-game
  `--format standard` harvest completed with zero fidelity counters, a clean
  manifest, canonical IDs in every tracker artifact, and correct lineage;
  the no-argument fixture run remains byte-identical in behavior and now
  also records (null-format) lineage.
Gates: 295/295 scenarios, 9/9 smoke, 13/13 training (new format-lineage
stage), 15/15 fixture-harvest + 7/7 protocol tests, 18/18 registry tests,
6/6 fuzz/replay configuration, and 8,000/8,000 default-fuzz actions.

**Round 7.47 (July 2026)** swept the highest-value contained v1 limitations,
scenario-first:
* **All seven Roles** — Cursed, Monster, Royal, Sorcerer, Young Hero,
  Virtuous, and Wicked now parse and create exact attached Aura tokens. Their
  base/set/variable P/T, ward cost, attack-trigger scope, scry, counter, and
  graveyard riders are guarded end to end.
* **Explicit day/night** — "it becomes day/night" now has a dedicated parsed
  effect and synchronizes every daybound/nightbound permanent immediately.
* **Clone-safe text delays** — oracle-text delayed triggers use structured
  payloads, survive lookahead cloning, fire against the cloned players/zones,
  and neither consume nor mutate the source game's pending trigger.
* **Attachment attack scope** — "equipped/enchanted creature attacks" triggers
  now fire only for the object actually attached to their source.
* **Stale limitations retired** — Herd Migration's Domain value already had an
  exact distinct-basic-land-type guard, and Restless Anchorage cleanup now
  explicitly asserts removal of animated type, subtype, P/T, and flying. The
  unused duplicate `GameState.prevent_damage` / `redirect_damage` API was
  removed; parsed prevention and redirection continue through the active
  replacement-effect pipeline.
Gates: 299/299 scenarios, 9/9 smoke, 13/13 training, 15/15 fixture-harvest,
7/7 protocol, 18/18 registry, 6/6 fuzz/replay configuration, and the
8-seed / 8,000-action deterministic default fuzz profile.

**Round 7.48 (July 2026)** continued the contained limitation sweep:
* **Created-object delayed riders** — sequenced resolutions share explicit
  created-object identities, so "exile that token at the beginning of the next
  end step" binds the token made earlier in the same resolution instead of the
  spell source. The structured payload remains clone-safe and the token ceases
  after changing zones as required.
* **Snow payment** — unrestricted mana produced by snow permanents retains
  snow provenance. Floated snow mana and atomically activated snow sources pay
  `{S}` exactly once, consume the mana, leave no phantom mana behind, and no
  longer increment fidelity telemetry. Failed transactions untap snow sources.
* **Attack-scope adjectives** — token/nontoken watcher scopes now distinguish
  real cards from tokens instead of conservatively suppressing both.
* **Combat plus main insertion** — "an additional combat phase followed by an
  additional main phase" now inserts both phases immediately after the current
  phase, supports repeated pairs, then resumes the original turn sequence.
* **Honest policy naming** — the active feature extractor is now
  `FixedWindowMTGExtractor`; its length-one gated layer is explicitly described
  as non-recurrent. `CompletelyFixedMTGExtractor` remains only as a compatibility
  alias so older checkpoint imports and state-dict keys keep loading.
Gates: 303/303 scenarios, 9/9 smoke, 13/13 training, 15/15 fixture-harvest,
7/7 protocol, 18/18 registry, 6/6 fuzz/replay configuration, and the
8-seed / 8,000-action deterministic default fuzz profile.

**Round 7.49 (July 2026)** closed the remaining contained policy-identity
limitations and widened the frozen Standard namespace:
* **Full Standard identity pool** — `freeze-pool` reads the pinned JSONL card
  list, filters it to English cards legal in the requested format, preserves
  existing indices, appends missing identities, verifies complete coverage,
  and rebuilds a versioned feature schema for the union. Standard now contains
  all 4,702 snapshot cards plus 28 historical bootstrap cards (4,730 total,
  feature_dim 436) without renumbering the original 110.
* **Target identity observations** — actions 274–283 now have exact page-aligned
  card features, canonical IDs, target kinds, controllers, and zone positions.
  Legacy category summaries remain available for old consumers.
* **Unbounded direct choices** — discard and forced-sacrifice decisions page
  through hands/battlefields in groups of ten, and the scripted opponent uses
  the same page action.
* **Canonical mechanic activation** — Investigate, Amass, Venture, Explore,
  Adapt, and Goad compatibility slots locate the parsed activated ability and
  use its ordinary cost/target/stack transaction. Their effects resolve through
  the shared effect factory instead of bypassing printed costs.
Gates: 305/305 scenarios, 9/9 smoke stages, 13/13 training stages, and
19/19 registry tests.

**Round 7.50 (July 2026)** delivered the first full-format support preflight:
* **Every Standard card audited** — all 4,702 legal English snapshot cards are
  constructed, every face passes through ability and replacement registration,
  and spell, loyalty, activated, and triggered effect text is probed through
  the shared effect factory. The run completed without audit crashes.
* **Versioned evidence ledger** — `formats/standard/support_ledger.json` is
  schema-versioned, self-hashed, and tied to the pool snapshot, canonical
  registry, evidence overrides, and configured corpus. It distinguishes 52
  scenario-verified cards, 75 metagame-corpus/static-clean cards, 3,259 unseen
  static-clean cards, 889 partial cards, and 427 unparsed cards. No card is
  called fully supported merely because it has never produced telemetry.
* **Impact-ranked gaps** — unsupported cards and mechanic families are sorted
  by configured deck copies before pool prevalence/severity. The pinned July
  11 representative sample contains eight exact 60-card lists covering 73.8%
  of the MTGGoldfish metagame snapshot. Its leading named gaps are Earthbend
  (12 slots), Flashback (10), transform layouts (4), prepare layouts (2), and
  Airbend (2), plus 71 slots of card-specific/unclassified Oracle text.
* **Reproducible corpus boundary** — the corpus records its capture date,
  archetype shares, source URLs, and exact lists. The ledger hashes that file
  and labels the ranking `representative-meta-2026-07-11`; future snapshots can
  reprioritize gaps without changing the evidence contract.

**Round 7.51 (July 2026)** made the representative Standard corpus executable
and closed its first two impact-ranked mechanic gaps:
* **Deterministic corpus hydration** — `Playersim.deck_corpus` joins the compact
  eight-list metagame corpus against the pinned Standard JSONL snapshot, checks
  exact 60-card counts and missing identities, and atomically writes eight
  full-record deck files under `formats/standard/decks/`. Two tests guard
  deterministic output and fail-closed missing-card behavior.
* **Standard is the strict default** — training, hyperparameter optimization,
  fixture Harvest, scenario real-card discovery, and deck-stat discovery now
  use the hydrated corpus and frozen Standard namespace. The empty
  `DeckLists/` staging directory was removed; the old bootstrap decks were
  archived and their 28 rotated identities were extracted into the explicit
  `historical_bootstrap_cards.json` scenario fixture.
* **Earthbend** — fixed-N Earthbend selects a controlled land, makes it a 0/0
  land creature with haste, adds the printed +1/+1 counters, and returns it
  tapped under the Earthbend controller after death or exile. The previous
  12-slot named Earthbend gap is gone from the regenerated ledger.
* **Flashback** — printed and until-end-of-turn granted costs are payable from
  the graveyard, successful casts exile after resolution, multi-symbol costs
  parse intact, and the first six graveyard objects receive distinct executable
  actions rather than overwriting one singleton slot. The previous 10-slot
  named Flashback gap is gone from the regenerated ledger.
* **Measured change** — the static ledger moved from 3,386 clean/verified cards
  to 3,425 and from 427 unparsed cards to 413. Its current status split is 52
  verified, 78 corpus-clean, 3,295 unseen-clean, 864 partial, and 413 unparsed.
  Gates: 307/307 scenarios, 9/9 smoke, 13/13 training, 15/15 fixture Harvest,
  7/7 protocol, 19/19 registry, 1/1 support-preflight, and 2/2 corpus tests.

**Round 7.52 (July 2026)** added format-aware deck ingress and another exact-
card coverage slice:
* **Automatic format routing** — `python -m Playersim.deck_ingest <list>` reads
  Arena/simple text or JSON, resolves canonical names against the pinned card
  lists, enforces main-deck, sideboard, copy-limit, registry, schema, and format
  legality, reports every match, and selects Standard, then Pioneer, then
  Modern unless `--format` requires one. Dry-run, strict-support, explicit
  replacement, and fail-closed namespace controls are available.
* **Separated recursive pools** — generated metagame decks live under
  `formats/<format>/decks/metagame/`; validated user lists live under
  `formats/<format>/decks/imported/`. Training, Harvest, corpus identity,
  preflight frequency, provenance, and deck statistics discover both through
  stable recursive traversal, while metagame regeneration cannot overwrite an
  import.
* **Eight exact cards gained scenario-backed v1 verification** — Flow State,
  Accumulate Wisdom, and Consult the Star Charts cover conditional Dig counts
  and printed remainder ordering;
  Badgermole Cub covers its creature-mana replacement; Eddymurk Crab covers
  graveyard reduction, off-turn Flash entry, and zero-to-two targets; Spider
  Manifestation covers restricted mana choice and cast-trigger gating; Fabled
  Passage covers its single-shuffle atomic tapped search and four-land untap
  rider; and Beifong's Bounty Hunters covers last-known-power Earthbend X
  within the documented Earthbend v1 semantics.
* **Contained v1 limitations closed** — Dig remainder order now distinguishes
  preserve, policy-selected, and random instructions; begin-game cards receive
  independent accept/decline decisions; and Beifong's dynamic Earthbend value
  comes from the dying creature's last-known power. Printed once-per-turn
  triggers now enforce and reset their shared turn gate. Earthbend's
  choice-free delayed return still resolves immediately instead of using the
  stack.
* **Measured and conservative coverage** — the regenerated 4,702-card ledger
  now records 60 verified, 79 observed-clean, 3,310 unseen-clean, 843 partial,
  and 410 unparsed cards. Versus Round 7.51, static-clean/verified coverage rose
  from 3,425 to 3,449 cards, evidence-qualified coverage rose from 130 to 139,
  and unparsed fell from 413 to 410. Ten formerly clean cards were deliberately
  moved to partial after the audit exposed unimplemented Harmonize/source-
  duration semantics; the ledger no longer hides those gaps.

Gates added for this round: 315/315 scenarios and 13/13 deck-ingest tests.

**Round 7.53 (July 2026)** closed the representative metagame's remaining
severe support gaps and delivered a high-impact fidelity slice:
* **Zero severe representative cards** — Escape Tunnel, Aang, Swift Savior,
  and Cosmogrand Zenith moved out of `unparsed`; the regenerated representative
  corpus now contains no `unparsed` or `crash` entry.
* **Exact high-impact effects** — Escape Tunnel grants power-limited temporary
  unblockability; Aang airbends creatures or spells and grants the owner's
  `{2}` exile cast; Cosmogrand gates on the second spell and exposes its mode;
  Combustion Technique counts Lessons and installs its exile replacement;
  Daydream blinks with a counter; and Sage of the Skies copies its own creature
  spell into a token.
* **Policy-visible searches** — Brightglass Gearhulk and Starfield Shepherd
  now expose their exact restricted library candidates. Optional/up-to search
  can be declined. Starfield remains honestly partial because Warp itself is
  not implemented.
* **Harmonize is executable** — printed Harmonize cards are castable from the
  graveyard, may tap one chosen creature to reduce generic mana by its power,
  and exile after resolving. Winternight Stories also implements its one-
  creature-or-two-cards discard decision.
* **Measured coverage** — the 4,702-card ledger now records 68 verified, 76
  observed-clean, 3,337 unseen-clean, 857 partial, and 364 unparsed cards.
  Static-clean coverage is 74.0323266695%; evidence-qualified coverage is
  3.0625265844%. Warp's 31-card family is explicitly partial, replacing a
  false-clean classification.

Gates for this round: 322/322 scenarios, 9/9 smoke, 19/19 registry,
13/13 deck-ingest, 2/2 deck-corpus, and 1/1 support-preflight tests.

**Round 7.54 (July 2026)** closed every remaining partial in the pinned
representative Standard corpus as one shared-primitives release:
* **Warp end to end** — hand-indexed Warp alternative casts pay the printed
  cost, resolve normally, exile at the next end step through clone-safe delayed
  payloads, and grant a later ordinary cast from exile without changing the
  frozen action or feature dimensions.
* **Linked choices and transactions** — Erode and Lumbering Worldwagon expose
  optional tapped-land searches; Archdruid's Charm chooses a creature or land
  and applies its linked destination; No More Lies exposes payment and exiles
  only a spell countered its way; Deadly Cover-Up purges the chosen name across
  graveyard, hand, and library and replaces exiled hand cards; Strategic
  Betrayal gives the affected opponent its creature choice before exiling the
  graveyard; North Wind Avatar consumes an optional outside-game choice when
  that pool exists.
* **Temporary and zone rules** — Mistrise Village marks and consumes the next
  spell's uncounterability; Day of Black Sun snapshots the X-bounded set,
  removes abilities in layer 6, then destroys that set; finality counters exile
  dying creatures; Esper Origins can resolve from a graveyard through exile to
  its transformed Saga face with a finality counter. Multi-face Card objects
  now initialize from their front-face Scryfall fields instead of empty
  top-level fields.
* **Vehicles** — Crew reuses the power-threshold tapping chooser, taps the
  committed creatures, and registers the Vehicle's layer-4 animation. Lumbering
  Worldwagon also receives its land-count power CDA while crewed.
* **Measured closure** — the regenerated 4,702-card ledger records 68 verified,
  89 observed-clean, 3,360 unseen-clean, 821 partial, and 364 unparsed cards.
  Static-clean coverage is 74.7979583156%; evidence-qualified coverage is
  3.3390046789%. The representative corpus has zero unexplained partial,
  unparsed, or crash cards.

Gates for this round: 326/326 scenarios; the remaining repository gates are
listed in the status snapshot above.

**Round 7.55 (July 2026)** made a broad generic-mechanics pass over the frozen
Standard pool instead of closing cards one at a time:
* **Equipment and Vehicles** — ordinary Equip abilities now resolve through a
  reusable attach effect after the existing target-and-cost transaction, while
  generic Crew text shares the power-threshold creature chooser and layer-4
  Vehicle animation path. The implementation covers both registry ability
  records and generated Crew instructions.
* **Policy-visible keyword actions** — fixed-value Discover reveals through the
  first eligible nonland card, randomizes the rest onto the bottom, and exposes
  cast-without-paying versus hand. Connive draws, exposes the discard, adds the
  nonland counter, supports optional wording, and enforces printed once-per-turn
  gates. Suspect applies menace/can't-block state, supports clearing and an
  optional transfer choice, and cleans state on zone changes.
* **Broader shared effects** — Explore and Investigate recognize ordinary
  pronoun/controller templates, and Airbend accepts the supported range of
  nonland permanent targets while preserving owner-based exile-cast permission.
  Complex mixed clauses remain partial instead of being falsely claimed by a
  broad keyword match.
* **Measured full-pool gain** — 192 cards moved from partial/unparsed to
  unseen-clean with zero clean-card regressions. The 4,702-card ledger now
  records 68 verified, 89 observed-clean, 3,552 unseen-clean, 810 partial, and
  183 unparsed cards. Static-clean coverage rose from 74.7979583156% to
  78.8813270949%; evidence-qualified coverage remains 3.3390046789% until the
  newly generic cards appear in a runtime corpus.

Gates for this round: 329/329 scenarios and 63/63 repository unit tests.

**Round 7.56 (July 2026)** addressed the dynamic keyword-action limitations
left deliberately open by the preceding family sweep:
* **Dynamic Discover** — X can come from a targeted or triggering spell's mana
  value, including X actually paid. Completing the action emits a controller-
  scoped Discover event, so Curator of Sun's Creation repeats the same value and
  its printed once-per-turn gate prevents recursion.
* **Repeated Explore and Investigate** — Explore X preserves its remaining
  iterations through a nonland top/graveyard policy choice. Investigate now
  supports fixed repeats, the two-player hand-size comparison, and counts the
  creatures controlled by selected players.
* **Endure** — fixed and counter-derived values expose the required choice
  between +1/+1 counters and an X/X white Spirit. Nontoken/another creature
  trigger filters now reject token and self entries, and nontoken death watchers
  use last-known token status.
* **Conservative prerequisites** — Descendant of Storms and Krumar Initiate move
  only from unparsed to partial: their Endure result is understood, but optional
  mana payment and activated `{X}` plus X-life payment are not falsely claimed.
  Brass's Tunnel-Grinder likewise improves to partial while its unrelated front-
  face clauses remain open.
* **Measured gain** — ten cards moved to unseen-clean with zero clean-card
  regressions. The ledger now records 68 verified, 89 observed-clean, 3,562
  unseen-clean, 811 partial, and 172 unparsed cards. Static-clean coverage rose
  from 78.8813270949% to 79.0940025521%; evidence-qualified coverage remains
  3.3390046789%.

Gates for this round: 331/331 scenarios and 63/63 repository unit tests.

**Round 7.57 (July 2026)** continued the limitations work with shared payment
and action-exposure infrastructure:
* **Optional resolution payments** — exact “you may pay `{cost}`; if you do”
  instructions now expose pay and decline through the ordinary resolution-
  choice policy. Payment uses the auto-tap planner, and the paid follow-up plus
  any remaining resolving instructions retain their continuation/finalizer.
  Descendant of Storms and Subway Train become fully statically clean through
  this path.
* **Large X pagination** — spell X affordability is no longer truncated at ten.
  All affordable values are retained in the casting choice and exposed ten per
  page through the existing shared page action, preserving the frozen 480-slot
  action schema. X=0 remains the Pass alias, while page-local actions carry the
  exact absolute X value into payment and resolution.
* **Activated X transactions** — activated abilities now stage the same
  paginated announcement before paying anything. The chosen value is shared by
  `{X}` mana and “Pay X life,” committed with tap and other costs, and retained
  in the stack context for the resolving effect. Krumar Initiate is covered end
  to end through Endure X.
* **Conservative improvement** — Digsite Conservator moves from unparsed to
  partial because its optional `{4}` into Discover transaction now works, while
  its independent four-card graveyard targeting remains explicitly open.
* **Measured result** — three cards moved to unseen-clean and one moved from
  unparsed to partial with zero clean-card regressions. The ledger records 68
  verified, 89 observed-clean, 3,565 unseen-clean, 809 partial, and 171 unparsed
  cards. Static-clean coverage is 79.1578051893%; evidence-qualified coverage
  remains 3.3390046789%.

Gates for this round: 333/333 scenarios and 63/63 repository unit tests.

**Round 7.58 (July 2026)** began the severity-ranked Priority 0 integrity pass:
* **Controller-safe permanent transfer** — temporary control now moves the
  permanent's controller-scoped state, returns an explicit success result,
  preserves its original controller for cleanup, and cannot move an object back
  from a non-battlefield zone after it dies.
* **Control-dependent effect rebinding** — live static and text-derived
  replacement effects are rebuilt for the new controller on both the initial
  control change and the end-of-turn return. Additional-mana replacements are
  scenario-verified to stop applying to the old controller immediately.
* **Last-known death attribution** — a creature that dies while stolen is
  credited to its actual controller at last existence. This closes the separate
  control-at-death limitation; only the broader repeated-ID object-model issue
  remains.
* **Generic as-enters transaction** — first-entry parsing now handles creature
  type, color, card type, opponent, and counter choices before deferred ETB,
  Landfall, and Saga events fire. Choices are retained in both typed stores and
  a generic per-permanent record, and all variants use the ordinary action mask
  and scripted-opponent path. Arbitrary card-specific consumers remain bounded
  coverage work rather than a simulation-integrity gap.

Gates for this round: 335/335 scenarios, 63/63 repository unit tests, 9/9 smoke
stages, and 13/13 training stages.

**Round 7.59 (July 2026)** continued the Priority 0 layer-integrity pass:
* **Specific ability dependencies** — every parsed static layer effect now
  carries source-ability identity. A specific `remove_ability` dependency can
  suppress the matching generated effect without erasing unrelated abilities
  from the same source; exact-text inference preserves older registrations.
* **CR 305.7 basic land types** — setting a basic land type now removes old land
  subtypes and rules-text abilities, supplies the intrinsic basic mana output,
  suppresses registered activations and triggers, and restores printed state
  when the effect ends.
* **Blood Moon/Urborg ordering** — basic-land-type setting participates in
  within-layer dependency sorting, so it can remove the source ability of an
  earlier type-changing effect before that effect applies.
* **Dynamic nonbasic-land scope** — global nonbasic-land effects recompute their
  battlefield membership each pass, including lands that enter after the
  effect began. Arbitrary changing applicability sets outside the structured
  dynamic-scope vocabulary remain the narrowed Priority 0 limitation.

Gates for this round: 337/337 scenarios, 63/63 repository unit tests, 9/9 smoke
stages, and 13/13 training stages.

**Round 7.60 (July 2026)** completed the broad Priority 0 closure sweep:
* **True runtime card identity** — canonical registry IDs remain the stable
  deck/statistics namespace, while reset materializes every repeated physical
  card as a distinct runtime ID, mutable `Card` object, and explicit owner.
  Targeting, counters, linked exile, control changes, and lookahead now address
  copies independently; telemetry canonicalizes runtime plays at its boundary.
* **Clone-safe delayed execution** — accepted structured triggers and legacy
  function/method callbacks now survive lookahead cloning with captured game,
  player, subsystem, closure, and default-argument references rebound to the
  branch. Opaque callable objects are rejected at registration instead of
  being accepted and silently dropped.
* **Announcement-time counter divisions** — spell and activated-ability
  divisions are chosen after targets but before costs are paid or the object
  reaches the stack. The locked allocation survives in stack context; an
  illegal target loses its share rather than redistributing it at resolution.
* **Snow provenance and condition integrity** — ordinary, conditional, and
  phase-restricted pools preserve and consume snow provenance. Common turn,
  attack, death, life-change, control, and hand-comparison conditions are
  explicit; unknown conditions now fail closed and raise fidelity telemetry.
* **Live arbitrary layer scopes** — every layer handler resolves affected sets
  from current calculated characteristics. Clone-safe declarative boolean
  predicates (`all`/`any`/`not` plus characteristic comparisons) can gain or
  lose members after an earlier effect in the same layer.
* **Branch-local mutable identity** — every reachable card object and merged,
  melded, or specialized identity ledger is isolated before clone
  construction, preventing clone initialization and layer write-back from
  mutating the source branch.
* **Merged-object ownership and blink** — Mutate records each physical
  component's owner and separates into the correct private zones. Meld rejects
  non-owned components, and blinking a melded permanent returns both front
  faces separately through the same transaction.

Gates for this round: 346/346 scenarios, 63/63 repository unit tests, 9/9 smoke
stages, and 13/13 training stages.

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

The order below is the canonical implementation priority. Severity is based on
the risk of corrupting rules outcomes or training statistics first, then missing
player decisions, bounded card/mechanic coverage, and operational constraints.
The detailed notes afterward are retained in their historical source order and
do not override this ranking.

### Priority 0 — simulation integrity

✅ No known Priority 0 simulation-integrity limitations remain.

Round 7.60 retired true per-copy runtime identity, dynamic layer applicability,
delayed-callback cloning, announcement-time counter divisions, restricted snow
provenance, condition fail-closed behavior, Mutate ownership/clone isolation,
Meld blink/clone isolation, and Specialize clone isolation. Missing Meld or
Specialize family data and bounded condition/mechanic vocabularies are honestly
classified as Priority 2 coverage; they fail closed rather than silently
diverge.

### Priority 1 — decision and action completeness

2. The fixed 480-action layout omits some activated abilities, hand objects,
    graveyard objects, and simultaneous source/target contexts.
3. Blocker damage ordering remains scripted even though opponent trigger
    ordering is policy-driven.
4. Simultaneous each-player discards are staged sequentially and use scripted
    opponent choices.
5. Ward auto-pays supported costs and cannot expose deliberate decline;
    sacrifice and discard ward costs are unsupported.
6. Multi-target spell copies cannot retarget only a subset of inherited
    targets.
7. Sacrifice choices support a bounded Oracle-characteristic vocabulary, and
    direct non-policy callers retain a deterministic fallback.
8. Nonland mana abilities lack structured choices for multi-symbol packages,
    colorless alternatives, and independent per-mana selections.
9. X selection retains a defensive ceiling and does not cover every nonmana
    X-cost expression.
10. Level-up exposes the legal action but has only basic policy choice around
    whether and when to spend mana.
11. The scripted opponent always accepts eligible opening-hand placement
    effects.
12. Keyword-grant choices support exactly two printed options and a bounded
    subtype-target template.

### Priority 2 — bounded mechanic and card fidelity

13. Generic `as enters` transactions are verified for creature type, color,
    card type, opponent, counters, and deferred ETB events; arbitrary effects
    consuming those chosen values remain card-specific.
14. Emblem execution is implemented only for the currently recognized Kaito
    and Wrenn texts.
15. MDFC support still lacks direct nonland back-face entry and complete
    back-face targeting text.
16. Adventure-half parsing and targeting remain heuristic.
17. Generic Discover, Explore, Investigate, Endure, Connive, Suspect, Airbend,
    Equip, and Crew support intentionally covers bounded text families.
18. Reflexive triggers recognize a bounded set of exact rider templates.
19. Earthbend has bounded dynamic-X parsing and resolves its choice-free delayed
    return immediately after the initial zone move.
20. Uncommon linked optional-search templates still need exact transaction
    handlers.
21. Warp source-duration permissions remain conservatively partial.
22. Target-conditioned pricing recognizes only the implemented condition
    vocabulary.
23. Numeric die support omits modifiers, rerolls, ignored rolls, and generic
    result-value clauses.
24. Attack-trigger adjective scopes and defender gating remain bounded to the
    supported two-player vocabulary.
25. Rare phase-beginning scopes can pass ungated.
26. Screaming Nemesis relies on standard life-gain entry points and a single
    committed reflected-damage target.
27. ConditionalExileEffect is a single-target implementation.
28. Obliterator uses a conservative payer fallback when the damage source has
    left play.
29. Cavern of Souls offers only the top ten locally derived creature subtypes.
30. Obstinate Baloth conservatively leaves an undetermined-cause discard in the
    graveyard.
31. Meld requires its result printing to be present in the local card database;
    missing `all_parts` data fails closed instead of fetching at runtime.
32. Specialize requires all five local variant printings; incomplete families
    are fidelity-marked unparsed and excluded from supported play.
33. Trigger-condition parsing now fails closed but does not yet express every
    Oracle condition template.
34. Mutate still lacks per-component replacement choices and library-order
    choice; commander-specific routing is outside the current formats.

### Priority 3 — operational and lineage constraints

35. Strategy memory is per environment and its optional enhancement pass makes
    saved memory content nondeterministic.
36. Format registries and schemas are intentionally lineage-bound, name plus
    Oracle-ID based, and best-of-one only; schema growth can require a new
    policy lineage.
37. Treasure/Beza support is scenario-verified; its retained note documents the
    exact supported path rather than an active correctness gap.

### Detailed notes (historical source order)

- Emblem execution currently recognizes the Kaito Ninja anthem and Wrenn
  graveyard-permission texts used by the sample decks. Other emblem text is
  retained as a command-zone record but needs an effect implementation before
  its card can be considered supported.
- Specific `remove_ability` existence dependencies and CR 305.7 basic-land-type
  ability loss are scenario-verified, including Blood Moon/Urborg ordering,
  late-entering nonbasic lands, intrinsic mana, and state restoration (Round
  7.59). Round 7.60 routes every layer handler through live membership and adds
  clone-safe boolean characteristic predicates, including applicability changes
  caused by an earlier effect in the same layer. Oracle text still needs to be
  parsed into that declarative vocabulary as bounded card coverage.
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
- The fixed 480-action layout can expose only the first three activated
  abilities on each permanent. Specialized hand actions such as MDFC,
  Adventure, Plot, cycling, and alternate costs still inspect only the first
  eight of the ten observed hand slots. Indices 205–223 retain dormant labels
  for mechanics that have no handler and are deliberately never mask-valid.
  Several singleton mechanic slots (notably
  Equipment/Fortification and loyalty families) also retain only one generated
  source/target context when several equivalent pairs are legal. Every exposed
  slot is executable after the Round 7.26 audit, but full choice coverage needs
  a staged/paginated generic source-target chooser or a versioned action-space
  redesign before those cards are suitable for strength comparisons. Round
  7.51 removed the Flashback collision for the first six graveyard objects;
  graveyard pagination beyond six remains open.
- Canonical registry IDs are printing/statistics identities; deck entries
  materialize as distinct runtime IDs with separate mutable
  `Card` objects and explicit owners (Round 7.60). Linked exile and all live
  object state key by runtime ID, while play telemetry canonicalizes back to the
  registry ID before persistence.
- Structured oracle-text delayed triggers and legacy function/method callbacks
  are clone-safe. Captured game/player/subsystem references, closure cells, and
  default arguments rebind to the branch. Unsupported opaque callable objects
  are rejected by `register_delayed_trigger` rather than accepted and lost.
- Opponent trigger ordering routes through the installed policy; blocker damage
  ordering still uses the scripted/automatic path pending the next choice audit.
- Snow provenance is tracked and consumed for ordinary, restricted,
  phase-restricted, and atomic-source payment. `_evaluate_condition` covers the
  common turn, attack, death, life-change, hand-comparison, and control
  predicates; unknown Oracle conditions fail closed and raise fidelity
  telemetry until their bounded parser coverage is added.
- Reflexive-trigger v1 recognizes exact "When you do" / "When that player
  does" riders when the prerequisite itself parses; generic optional sacrifice
  now exposes both the permanent choice and decline path.
- Counter divisions are policy-selected after targets and before costs during
  casting or activation, then locked in stack context under CR 601.2d. Illegal
  targets lose their announced shares at resolution. Dig selects the kept card
  explicitly and follows each parsed instruction's preserve, policy-selected,
  or random remainder order.
- Earthbend v1 supports fixed numeric values, Beifong's last-known-power X
  expression, and the correct death/exile return destination. The choice-free
  delayed return currently resolves immediately after the initial zone move
  rather than entering the stack; dynamic X expressions beyond the supported
  last-known-power pattern still need dedicated parsing.
- Nonland mana abilities expose simple one-symbol colored alternatives such as
  `{R} or {G}`. Multi-symbol packages, colorless alternatives, and independent
  per-mana choices still need a structured production-choice model.
- Optional "its controller may search" land-search riders preserve the
  pre-removal battlefield controller and expose decline when supported by the
  linked effect. Other uncommon linked-search templates still require exact
  transaction handlers.
- Warp's cast, next-end-step exile, and later exile-cast transaction is
  implemented. Source-duration wording such as "for as long as you control" is
  still
  conservatively partial where the permission or restriction outlives the
  resolving instruction.
- Round 7.55's generic family support is deliberately bounded. Round 7.56 added
  spell-mana-value and repeated-same-value Discover, Explore X, hand-comparison
  and selected-player creature-count Investigate, plus fixed/counter-derived
  Endure. Connive covers the ordinary one-card action and simple
  optional/once-per-turn templates; Suspect covers direct, clear-all, attached,
  and transfer forms; and Airbend covers nonland permanents plus the existing
  creature/spell path. Other dynamic count expressions, compound keyword
  clauses whose
  other instructions do not parse, unusual multi-object Suspect wording, and
  broader exile-cast cost modifiers remain conservatively partial. Generic
  Equip and Crew are executable, but cards with additional unsupported text
  remain partial on that independent text.
- Spell-copy retargeting can keep the complete inherited target set or replace
  the complete set. Changing only some targets of a multi-target spell needs a
  future slot-aware target-choice context; ordinary one-target copies are fully
  exposed now.
- X spell choices paginate beyond ten within the fixed action range. The
  monotonic affordability walk has a defensive ceiling of 1,000. Activated
  abilities share that staged chooser when X appears in their mana cost or a
  “Pay X life” component; other X-dependent nonmana cost expressions remain
  open.
- Simultaneous each-player discards are committed sequentially, and the
  scripted opponent selects its first available legal slot/page until
  policy-vs-policy self-play lands.
- Target, Dig, counter-distribution, SacrificeEffect, and activated-cost
  sacrifice choices paginate beyond ten. Direct programmatic ability callers
  that omit explicit non-self sacrifice IDs retain a deterministic fallback;
  policy-facing actions never use it. Sacrifice requirements involving Oracle
  characteristics beyond the supported type/subtype, token/nontoken, nonland,
  tapped/untapped, `another`, and type-disjunction vocabulary still need a
  dedicated cost parser before those cards are harvest-eligible.
- Round 7.16 target-conditioned pricing recognizes the sample cards' two exact
  conditions: a tapped permanent and a permanent you control. Arbitrary Oracle
  conditions that refer to target characteristics still need dedicated parsers.
- Meld v1 requires the meld-result card object to be present in `card_db`; the
  deck loader does not fetch a missing `all_parts` URI, so absent result data
  fails closed. Ownership is validated, branch identity is isolated, and blink
  returns both front-face components as separate objects (Round 7.60).
- Numeric die v1 supports ordinary result tables and emits die-roll events.
  Roll modifiers, rerolls/ignored rolls, and tableless "equal to the result"
  clauses remain future work. The nonnumeric planar die is intentionally out
  of scope with Planechase.
- Specialize v1 requires all five `all_parts` variant card objects in `card_db`;
  missing families are fidelity-marked `unparsed`. Lookahead specialization is
  branch-isolated and simultaneous copies specialize independently.
- Mutate records an explicit owner for every physical component and routes each
  one to its owner's private zone on separation. Ordered components, top/bottom
  identity, triggers, illegal-target fallback, token cessation, and clone
  isolation are covered. Per-component replacement choices, library ordering,
  and commander routing remain bounded mechanic/decision work.
- Ward target-tax v1 snapshots obligations when targets are committed and
  supports parsed mana costs and simple "pay N life" costs by auto-paying when
  possible. Sacrifice/discard costs and letting the agent deliberately decline
  payment remain future choice-exposure work.
- Attack triggers fire at declare-attackers-done through one ATTACKS dispatch:
  the attacker's own abilities plus "whenever a/another <type> [you control]
  attacks" and "attacks you" watchers on other permanents, scoped by controller
  and printed type (July 2026). Token/nontoken scopes are supported; other
  adjectives outside the card's type/subtype/supertype vocabulary remain
  conservative. Defender-side gating assumes two-player "attacks you".
- Opening-hand placement v1 now gives each eligible card an independent accept
  or decline decision; the scripted opponent still always places every eligible
  card as its policy. Both Leyline of Resonance's begin-game line and its copy
  trigger on a spell targeting exactly one friendly creature are scenario-
  verified.
- Screaming Nemesis v1: the reflected damage picks the first committed target,
  and the rest-of-game restriction is a player flag consulted by gain_life and
  lifelink. Effects that add life directly without those entry points would
  bypass it (the same pre-existing caveat as all life-gain replacements).
- ConditionalExileEffect (Anoint) is single-target v1 and reads the corrupted
  threshold from the target controller's poison_counters at resolution.
- When an Obliterator damage source left play before resolution, the forced-
  sacrifice payer falls back to the opponent of the trigger's controller.
- Cavern of Souls v1 stores the chosen type per runtime object per player,
  offers the top-10 creature subtypes from the controller's own cards as
  options, and applies the uncounterable rider when
  any of its restricted mana was spent on the cast. Counterspells can still
  TARGET the spell; they fizzle at resolution.
- Treasure tokens carry their printed sacrifice-for-mana text; Beza's token is
  scenario-verified through registration, activation costs, color choice,
  mana production, and the CR 605 no-stack path.
- Phase-beginning trigger owner gating (July 2026) covers "on your turn"
  (combat), your-upkeep/end-step/draw/precombat-main, and "an opponent's
  upkeep/end step" wordings. Rarer phase scopes ("each player's upkeep on
  their turn", named-player phases) still pass ungated.
- Obstinate Baloth v1 identifies an opponent-caused discard by finding the
  causing source on the stack (falling back to the source's current zone
  controller); an undeterminable cause conservatively keeps the graveyard
  destination.
- The keyword_grant choice v1 supports exactly two printed options and is
  made by the effect's controller. Subtype target pass-through applies only
  to "target <subtype> you control" wordings.
- `creatures_died_this_turn` now attributes deaths from the battlefield
  object's last-known controller, including a permanent under temporary
  control (Round 7.58). Round 7.60's runtime IDs remove the former repeated-copy
  ambiguity.
- ENTERS_BATTLEFIELD-registered replacements are now applied through the
  ENTER_BATTLEFIELD alias merge (July 2026). The revived generic "as enters"
  transaction is scenario-verified for creature-type, color, card-type,
  opponent, counter, and deferred-trigger paths (Round 7.58). Arbitrary
  card-specific effects that consume those chosen values remain bounded
  coverage work; Cavern of Souls keeps its dedicated mana consumer.
- Text-derived replacement registration is idempotent per card per game and
  cleared by `remove_effects_by_source` for phasing rebuilds. Round 7.58 also
  rebuilds controller-bound static and replacement effects after pure control
  changes and their end-of-turn reversion.
- strategy_memory persistence is per-env (under the env's storage
  directory). Cross-env sharing within one training run no longer happens
  implicitly; save_memory also has a 20% random "enhancement" pass, so
  memory file contents are not bit-deterministic (game RNG unaffected).
- Format-foundation v2 (Round 7.49): the frozen `formats/standard/` registry
  covers all 4,702 legal English cards in the pinned Standard pool snapshot
  plus 28 historical bootstrap identities. `freeze-pool` grows the registry
  append-only and rebuilds the feature schema for a new policy lineage;
  the original 110 indices remain stable. Canonical indices are name-sorted
  at first freeze and
  differ from legacy insertion-order IDs, so format-namespace runs are a
  new stats lineage and must not be mixed with pre-7.46 artifacts. Lineage
  lives in run-level manifests, not per-game `game_log.jsonl` lines.
  Registry identity matches by card name + oracle_id; per-printing
  distinctions (set/collector number) are deliberately out of scope. A
  frozen-schema subtype vocabulary that must grow requires a new schema
  version and therefore a new policy lineage; ordinary `freeze --extend`
  still accepts only width-preserving additions.
  Round 7.52's deck importer can bootstrap a wholly missing Standard, Pioneer,
  or Modern namespace from its pinned snapshot, but refuses a half-present
  registry/schema pair. Imported decks are hydrated into the detected format's
  isolated recursive pool; sideboards are retained and legality-checked but are
  not played by the current best-of-one runtime.

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
abilities permanently, force-untapped); attack triggers
(handle_attack_triggers had no callers and attackers_this_turn was never
written — Boast could never activate and the agent's attacked-this-turn
observations were always zero); dealt-damage triggers ("is dealt damage"
matched no event class — every enrage-style trigger was dead).

Stats-corrupting, found in Round 7.19: resolving a spell executed its printed
activated-ability lines (Herd Migration discarded and gained 3 life on cast);
token subtypes outside the loaded pool's feature vocabulary were dropped
(tokens missed tribal/anthem interactions); "any target" spells crashed
target selection when players and permanents shared the valid set
(int-vs-str sort).

Found in Round 7.20: generic activated abilities stacked with an empty
context resolved to nothing (every ability_handler.activate_ability call was
a no-op — Boast was dead twice over); generic creature tokens had no card
type unless the parsed name contained the literal word "creature", and
colored tokens were always colorless (Card reads "color_identity" letters,
not the "colors" vector); cast_spell's context omitted the card, making all
conditional "spend only" mana unusable for casts; the targeting parser
captured state adjectives as the target type ("target attacking creature"
had zero legal targets).

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
