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
   every delivery (currently 9/9, 12/12, and 255/255, plus 10/10 fixture-
   harvest tests, 5/5 production-protocol tests, 6/6 fuzz/replay tests, and
   the deterministic 8-seed / 8,000-action default fuzz profile, and the
   strict 32-seed / 320,000-action long profile).
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

- Tier 0 (stats plumbing): ✅ complete.
- Tier 1 (rules correctness): ✅ complete — all seven items plus the P1
  placeholder triage delivered; see appendix for the bug catalog.
- Tier 2 (card coverage): ◐ the audited eight-deck sample has no known
  high-risk partials; format-wide quantified coverage remains manifest-driven.
- Tier 3 (training/environment): ◐ policy plumbing and audit work are complete;
  a trained checkpoint still needs to beat scripted play before Harvest is
  promoted to policy-vs-policy.
- Tier 4 (verification/calibration): ◐ invariant and long-fuzz gates are green;
  the matchup calibration study remains open.
- Tier 5 (operations/integration): ◐ Harvest orchestration is complete; strength
  qualification, production throughput profiling, and deck-builder integration
  remain open.
- Test gates: smoke 9/9, training 12/12, scenarios 255/255 (grown from 12),
  fixture harvest 10/10, production Harvest protocol 5/5, fuzz/replay
  configuration 6/6, deterministic default fuzz 8 seeds x 1,000 valid
  actions, and strict long fuzz 32 seeds x 10,000 valid actions.
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

## Tier 4 — Verification & calibration

1. ✅ Golden scenario harness — 255 scenarios and growing; scenario-first is a
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

**Current execution order:** freeze a reproducible engine/baseline snapshot;
train a longer strength candidate; run paired-seat promotion; complete the
3–5-pair calibration study; profile production-size Harvest throughput; then
implement the deck-builder contract and automatic feedback loop. New fidelity
failures discovered along that path pre-empt strength and integration work.

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

- Role support currently covers the Monster and Wicked Role definitions used by
  the sample decks. Cursed, Royal, Sorcerer, Young Hero, and Virtuous Roles still
  need definitions when a target deck requires them.
- Emblem execution currently recognizes the Kaito Ninja anthem and Wrenn
  graveyard-permission texts used by the sample decks. Other emblem text is
  retained as a command-zone record but needs an effect implementation before
  its card can be considered supported.
- Delayed-trigger pronouns referring to objects created earlier in the same
  resolution mis-bind to the source (token-maker riders) and no-op safely.
- Specific `remove_ability` is not an existence dependency in layer sorting;
  CR 305.7 (Blood Moon ability loss) not modeled; applicability-set
  dependencies out of scope while `affected_ids` is a static snapshot.
- Day/night v1 covers designation establishment from daybound/nightbound
  permanents, turn-start spell-count transitions, synchronized transforms, and
  entry faces. Explicit spell/ability instructions that say it becomes day or
  night still need a dedicated parsed effect.
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
  redesign before those cards are suitable for strength comparisons.
- Legacy singleton shortcuts for Investigate, Amass, Venture, Explore, Adapt,
  and Goad execute their mechanic directly instead of committing the source's
  parsed activated-ability cost through the generic transaction. Their exposed
  masks now carry executable contexts, but cards relying on those activated
  forms must remain out of strength harvests until the shortcuts delegate to
  `activate_ability()` (or are removed in favor of the generic action).
- Target-selection actions and their masks use one canonical paged ordering,
  but the legacy `targetable_*` observation fields are category summaries rather
  than an identity-preserving encoding of those exact ten action choices. Card
  choice phases have aligned `choice_cards`; ordinary targeting still needs the
  same explicit page-aligned representation for best learning quality.
- `CompletelyFixedMTGExtractor` labels a length-one, zero-initialized LSTM as
  sequential processing. Its gates are trainable, but hidden state is not
  carried across policy steps, so it provides no temporal memory. Use a truly
  recurrent mask-aware policy or rename/replace this layer before claiming a
  recurrent agent.
- Repeated card IDs still model copies coarsely; last-moved location tracking
  and duplicate-preserving list zones repair the known first-touch failures,
  but true per-copy object identity remains a deeper future cleanup. Linked
  exile therefore cannot distinguish which one of two simultaneous same-ID
  sources owns a particular link.
- Clones/MCTS copies start with an empty delayed-trigger registry.
- Opponent trigger ordering routes through the installed policy; blocker damage
  ordering still uses the scripted/automatic path pending the next choice audit.
- Snow payment can double-charge (fidelity-counted); `_evaluate_condition`
  vocabulary is thin (life totals, card counts,
  "you control X" only).
- Reflexive-trigger v1 recognizes exact "When you do" / "When that player
  does" riders when the prerequisite itself parses; generic optional sacrifice
  now exposes both the permanent choice and decline path.
- Counter divisions are policy-selected when the effect resolves. Full CR
  601.2d fidelity would announce and lock those divisions during casting or
  activation. Dig selects the kept card explicitly, but any permitted ordering
  of multiple remainder cards still follows deterministic library order.
- Spell-copy retargeting can keep the complete inherited target set or replace
  the complete set. Changing only some targets of a multi-target spell needs a
  future slot-aware target-choice context; ordinary one-target copies are fully
  exposed now.
- X choices are currently capped at 10 by the fixed action range.
- Discard choices expose only the first 10 hand slots. Simultaneous
  each-player discards are committed sequentially, and the scripted opponent
  selects its first available hand slot until policy-vs-policy self-play lands.
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
- Domain now drives Leyline Binding's casting cost. Domain effect values such as
  Herd Migration's token count remain unsupported until an effect can consume
  the same distinct-basic-land-type count.
- Meld v1 requires the meld-result card object to be present in `card_db`; the
  deck loader does not fetch a missing `all_parts` URI. Blink/return handling
  for both component cards and clone-isolated meld identity remain deeper work.
- Numeric die v1 supports ordinary result tables and emits die-roll events.
  Roll modifiers, rerolls/ignored rolls, and tableless "equal to the result"
  clauses remain future work. The nonnumeric planar die is intentionally out
  of scope with Planechase.
- Specialize v1 requires all five `all_parts` variant card objects in `card_db`;
  missing families are fidelity-marked `unparsed`. Repeated card IDs and the
  shared `card_db` also make simultaneous copies and lookahead specialization
  coarser than true per-object perpetual identity.
- Mutate v1 uses battlefield control as the engine's ownership approximation.
  Ordered components, top/bottom identity, triggers, illegal-target fallback,
  and same-destination separation are covered; per-component replacement
  choices, library ordering, token-on-top status, commander routing, and
  clone-isolated merged identity remain deeper object-model work.
- `prevent_damage`/`redirect_damage` in game_state_damage are a dead API
  (no callers) — remove or wire deliberately.
- Ward target-tax v1 snapshots obligations when targets are committed and
  supports parsed mana costs and simple "pay N life" costs by auto-paying when
  possible. Sacrifice/discard costs and letting the agent deliberately decline
  payment remain future choice-exposure work.
- Attack triggers fire at declare-attackers-done through one ATTACKS dispatch:
  the attacker's own abilities plus "whenever a/another <type> [you control]
  attacks" and "attacks you" watchers on other permanents, scoped by controller
  and printed type (July 2026). Scope adjectives outside the card's
  type/subtype/supertype vocabulary (e.g. "nontoken") conservatively suppress
  the watcher; "equipped/enchanted creature attacks" wordings still fire
  ungated for ANY attacker (pre-existing over-fire, now documented); and
  defender-side gating assumes two-player "attacks you".
- Additional combat v1 inserts combat phases only. Wordings that add "an
  additional main phase" after the combat (Aggravated Assault style) get the
  combat but not the extra main phase.
- Opening-hand placement v1: PASS declines ALL of that player's remaining
  begin-game cards at once rather than per card, and the scripted opponent
  always places every eligible card. Leyline of Resonance's second line (the
  copy trigger on single-friendly-target spells) is NOT yet verified; only the
  begin-game line is covered by scenarios.
- Screaming Nemesis v1: the reflected damage picks the first committed target,
  and the rest-of-game restriction is a player flag consulted by gain_life and
  lifelink. Effects that add life directly without those entry points would
  bypass it (the same pre-existing caveat as all life-gain replacements).
- ConditionalExileEffect (Anoint) is single-target v1 and reads the corrupted
  threshold from the target controller's poison_counters at resolution.
- Forced sacrifice (Obliterator) exposes only the first 10 battlefield slots
  per pick, and when the damage source left play before resolution the payer
  falls back to the opponent of the trigger's controller.
- Cavern of Souls v1 stores the chosen type per card ID per player (repeated
  deck IDs share one choice), offers the top-10 creature subtypes from the
  controller's own cards as options, and applies the uncounterable rider when
  any of its restricted mana was spent on the cast. Counterspells can still
  TARGET the spell; they fizzle at resolution.
- Treasure tokens carry their printed sacrifice-for-mana text; Beza's token is
  scenario-verified through registration, activation costs, color choice,
  mana production, and the CR 605 no-stack path.
- Restless-land animation registers end-of-turn layer effects; reversion
  rides on the existing duration cleanup rather than a per-land scenario
  assertion.
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
- creatures_died_this_turn attributes a death to the player whose
  battlefield the creature left (control at death approximated by zone).
- ENTERS_BATTLEFIELD-registered replacements are now applied through the
  ENTER_BATTLEFIELD alias merge (July 2026). The revived generic "as enters"
  path sets `as_enters_choice_needed` context flags whose downstream
  consumers are not yet scenario-verified; Cavern of Souls keeps its
  dedicated Round 7.20 implementation.
- Text-derived replacement registration is idempotent per card per game and
  cleared by remove_effects_by_source (phasing rebuild); a pure CONTROL
  change without unregistration does not re-register, keeping the original
  registration's controller_id.
- strategy_memory persistence is per-env (under the env's storage
  directory). Cross-env sharing within one training run no longer happens
  implicitly; save_memory also has a 20% random "enhancement" pass, so
  memory file contents are not bit-deterministic (game RNG unaffected).

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
