# Sample Deck Support Audit

Audit date: July 2026

## Scope

- 8 historical bootstrap decks, now archived locally under
  `archive/legacy_bootstrap_decks/`
- 480 total deck slots
- 110 unique cards
- Card text, layouts, keywords, costs, choices, tokens, linked effects, and
  mana abilities compared against reachable engine paths

Copy counts below are summed across all eight decks. Categories can overlap.
"Confirmed gap" means there is no complete engine path for the relevant
rules behavior. "Needs card scenario" means related code exists, but the
actual sample card has not yet proved that path end to end.

This document is retained as the historical audit record for that bootstrap
corpus. Current training and Harvest load `formats/standard/decks/` recursively:
the pinned representative corpus is hydrated into `metagame/`, while validated
user-supplied lists are routed into the format's separate `imported/` pool.

## Closed In Round 7.12

The audit found that the most common bad behavior was in the mana bases.
These 114 deck slots now use corrected entry and activation behavior:

| Family | Copies | Cards |
| --- | ---: | --- |
| Fast lands | 32 | Blooming Marsh (4), Concealed Courtyard (4), Copperline Gorge (8), Darkslick Shores (12), Seachrome Coast (4) |
| Pain lands | 28 | Adarkar Wastes (2), Caves of Koilos (3), Karplusan Forest (8), Llanowar Wastes (4), Underground River (11) |
| Verge lands | 28 | Floodfarm Verge (2), Gloomlake Verge (8), Hushwood Verge (4), Thornspire Verge (8), Wastewood Verge (6) |
| Always-tapped current wording | 26 | Commercial District (1), Hedge Maze (2), Lush Portico (4), Meticulous Archive (1), Restless Anchorage (2), Restless Cottage (4), Restless Reef (7), Restless Ridgeline (3), Shadowy Backstreet (2) |

The agent now chooses among a land's legal mana abilities. Pain-land damage
is tied only to the colored ability, Verge colors check controlled basic land
types, fast lands count other lands, and both "enters tapped" wordings work.

## Closed In Round 7.13

Stun-counter placement and rules semantics are now supported for all 17
affected slots: Kaito, Bane of Nightmares (9) and Floodpits Drowner (8).
Each untap attempt removes exactly one stun counter instead of untapping,
including the untap step, spell/ability effects, and untap costs. A replaced
untap does not fire "becomes untapped" triggers. Kaito's -2 sequence and
Floodpits Drowner's targeted ETB both preserve the selected target when adding
their counters.

This closed the stun mechanic, not every ability on those cards. Kaito's emblem
and type change and Floodpits Drowner's second ability were deferred to Round
7.17.

## Closed In Round 7.14

Valiant is now supported for all 16 affected slots: Heartfire Hero (8) and
Emberheart Challenger (8). Finalized spell and ability targets, changed copy
targets, and copied spells that keep inherited targets all use the same event.
The trigger checks the targeting controller, fires only for the first friendly
target each turn, resolves above the targeting object, and becomes available
again on the next turn or after the permanent leaves and returns. Heartfire's
counter and Emberheart's impulse-draw effects both have exact-card scenarios.

Monster and Wicked Role behavior is now supported for all 9 Role-producing
slots: Monstrous Rage (8) and The Witch's Vanity (1). Roles enter attached as
colorless Aura tokens, apply their printed bonuses, allow Roles controlled by
different players to coexist, and put all but the newest same-controller Role
into the graveyard as a state-based action. Wicked's graveyard trigger survives
the token ceasing to exist. Monstrous Rage has a full cast/target/Valiant/Role
scenario; The Witch's Vanity's chapter III wording has a focused parser and
resolution scenario.

This closed these mechanics, not every line on the cards. Heartfire Hero's
power-based death damage was deferred to Round 7.17. The Witch's Vanity's Food
token was completed in Round 7.18.

## Closed In Round 7.15

Linked temporary exile is now supported for all 8 affected slots:
Deep-Cavern Bat (4) and Leyline Binding (4). Deep-Cavern Bat targets an
opponent, exposes the optional nonland-card hand choice to the acting policy,
and returns that card to its owner's hand. Leyline Binding enforces its
opponent/nonland target restrictions and returns its linked permanent to the
battlefield. Both effects do nothing if their source left before the enters
ability resolved, and a linked card that is no longer in exile is not returned.

Nowhere to Run's protection exceptions are supported for all 8 copies. Its
static text is modeled as live targeting rules rather than as ability removal,
so its own trigger can select an opposing hexproof creature, that target becomes
illegal if Nowhere leaves before resolution, and opposing ward abilities do not
trigger while it remains. Ward obligations are captured when targets are
committed, preventing a later departure from creating a retroactive ward tax.

This closed these behavior families, not every line on Leyline Binding. Domain
and its resulting conditional cost reduction were deferred to Round 7.16.

## Closed In Round 7.16

Nonmana additional casting costs are now supported for all 10 affected slots.
Fear of Isolation (8) exposes a mandatory non-target choice among permanents its
caster controls and returns the selected object to its owner's hand before mana
is paid or the spell enters the stack. Analyze the Pollen (2) exposes optional,
sequential graveyard choices; it accepts payment only at total mana value 8 or
greater, exiles exactly those cards, and leaves the graveyard unchanged when
declined. Its resolution searches for a basic land without evidence and a
creature or land when evidence was collected.

Conditional casting-cost reductions are now supported for all 13 affected
slots. Leyline Binding (4) counts distinct basic land types, including multiple
types on a nonbasic land. Ride's End (1) and This Town Ain't Big Enough (8)
choose targets before determining affordability or paying mana, then apply their
discount only from the targets actually committed. Their reduced casts and all
additional-cost selections are visible in the policy action mask. Ride's End's
`creature or Vehicle` target class is also covered end to end.

This closes Leyline Binding's Domain cost line, not every Domain effect. Herd
Migration's basic-land-type-scaled token count remains listed below.

## Closed In Round 7.17

Map tokens and explore are now supported for all 13 affected slots. Map is a
colorless artifact token with its printed pay, tap, sacrifice, target, and
sorcery-timing restrictions. The target is selected before costs are paid, and
the ability remains on the stack after its token source ceases to exist.
Explore moves a revealed land to hand, gives a +1/+1 counter for a nonland or
empty library, and exposes the nonland top-or-graveyard decision to the acting
policy. Spyglass Siren and Get Lost have exact scenarios, including Get Lost
giving both Maps to the destroyed permanent's controller.

Floodpits Drowner's remaining activated ability is supported for all 8 copies.
Only a creature with a stun counter is legal; resolution shuffles Drowner and
that creature into their owners' libraries. If the target loses its last stun
counter before resolution, the whole targeted ability fizzles and Drowner
stays on the battlefield.

Heartfire Hero's remaining death rider is supported for all 8 copies. A
battlefield-leave snapshot preserves its power before counters and continuous
effects reset, and its dies trigger deals that last-known amount to each
opponent.

Command-zone emblems and Kaito's conditional animation are supported for all
11 affected Kaito and Wrenn slots. Kaito becomes only a 3/4 Ninja creature with
hexproof during its controller's turn while it has loyalty, can still activate
loyalty abilities in that form, and reverts when either condition ends. Its
Ninja anthem emblem applies cumulatively. Wrenn's emblem exposes legal land
plays and permanent spells from its controller's graveyard through the policy
action space.

Enduring Curiosity's death return is supported for all 6 copies. It returns
under its owner's control only when its death snapshot says it was a creature,
returns as an enchantment without its creature subtypes, and does not return if
the dying object was a token.

## Closed In Round 7.18

Mockingbird's bounded copy-as-enters replacement is supported for all 6 copies.
The policy may choose any battlefield creature whose mana value is no greater
than the total mana actually spent on Mockingbird, or decline to copy. It uses
copyable printed characteristics rather than counters or continuous effects,
adds Bird without removing copied subtypes, grants flying, and has copied
enters abilities before it enters.

Food tokens are supported for all 5 Restless Cottage and The Witch's Vanity
slots. Food is a colorless artifact token with the Food subtype and its printed
`{2}`, tap, sacrifice ability. The activation pays every cost atomically,
survives its token source ceasing to exist, and gains exactly 3 life.

Plot is supported for all 4 Slickshot Show-Off copies. Plot is a hand-indexed
sorcery-speed special action that pays `{1}{R}` and moves the card to exile
without using the stack. The card cannot be cast that turn; a later-turn
sorcery-speed action casts it from exile without paying its mana cost and
consumes exactly one Plot permission.

Bargain and Torch the Tower are supported for all 4 copies. Casting exposes an
optional policy choice among controlled artifacts, enchantments, and tokens,
then commits targets before sacrificing the selected permanent or paying mana.
The selected Bargain permanent may legally be Torch's target; that target is
retained on the stack and Torch fizzles cleanly after the sacrifice. Torch
deals 2 when Bargain is declined or 3 and scries 1 when bargained. A creature
it actually damaged is exiled instead if it would die later that turn.

Manifest dread and Turn Inside Out are supported for all 4 copies. The policy
chooses one of the top two cards to put onto the battlefield face down and the
other moves to the graveyard; one-card and empty libraries are handled without
phantom choices. A face-down object exposes only colorless 2/2 creature
characteristics, and a creature card can turn face up for its mana cost without
entering again. Turn Inside Out creates a one-shot, same-turn death trigger for
only its chosen creature.

## Closed In Round 7.19

The five recommended items are implemented and scenario-guarded:

Herd Migration (1) counts distinct basic land types for its token effect
through the shared dynamic-quantity counter; duals contribute each printed
type. The same scenario forced a general repair: printed activated-ability
lines no longer execute during spell resolution.

Fear of Missing Out (4) fires its attack trigger on the first attack each
turn, evaluates Delirium as distinct card types in its controller's
graveyard, pauses for its untap target, and inserts one additional combat
phase that the phase machinery consumes instead of the postcombat main. This
round also brought attack triggers to life engine-wide: they previously had
no caller, and per-turn attacker tracking was never written.

Leyline of Resonance (4) exposes a begin-game battlefield choice to each
player after mulligan decisions, starting player first; declining keeps the
card in hand, and the first turn is deferred until every placement resolves.
Its separate copy-trigger condition was completed in Round 7.21.

Screaming Nemesis (4) reflects exactly the damage it was dealt to any other
target (the source is excluded from legal choices), and a player damaged this
way can't gain life for the rest of the game through either the general
life-gain entry or lifelink.

Anoint with Affliction (4) targets any creature and checks its exile
condition at resolution: mana value 3 or less, or any mana value when the
target's controller has three or more poison counters.

## Closed In Round 7.20

Phyrexian Obliterator (4): a dealt-damage trigger class now exists for "a
source deals damage to this creature", and the damage source's controller
chooses each of the sacrificed permanents through a mandatory forced-sacrifice
choice, one immediate sacrifice per pick.

Restless Anchorage (2), Cottage (4), Reef (7), Ridgeline (3): the printed
self-animation activated abilities produce end-of-turn creature type, subtype,
colors, keywords, and P/T through the layer system; the animated land is a
legal attacker; and each land's "whenever this land attacks" rider is
scenario-verified (Map, Food plus optional graveyard exile, targeted mill,
pump-and-untap of another target attacking creature).

Sunfall (2): "Exile all creatures. Incubate X" exiles every creature
atomically and creates a transforming Incubator token with that many +1/+1
counters; paying {2} transforms it into the 0/0 Phyrexian artifact creature
that keeps the counters.

Cavern of Souls (3): entering opens a mandatory creature-type choice drawn
from the controller's own creature subtypes, the restricted "any color" output
is spendable only on creature spells of the chosen type, and a spell paid with
it is uncounterable at resolution.

Beza, the Bounding Spring (1): all four opponent comparisons (lands, life,
creatures, cards in hand) are evaluated independently at resolution, producing
exactly the Treasure, 4 life, two blue 1/1 Fish, and one draw that apply.

## Confirmed Gaps

These should make affected card statistics ineligible for harvest until the
listed behavior is implemented and guarded by scenarios.

None currently known in the audited eight-deck sample. Round 7.45 closes Three
Steps Ahead's former Spree gap. This is not format-wide evidence: other Spree
cards and newly introduced cards remain eligible only when their own effect
semantics are scenario- or manifest-verified.

## Closed In Round 7.45

- Spree now uses one generic casting transaction. The policy announces one or
  more distinct modes, cumulative affordability includes the printed base plus
  every selected mode's additional cost, taxes/reductions apply once, eligible
  lands can be auto-tapped, and the combined cost is paid once. Duplicate,
  forged, unaffordable, and zero-mode selections are rejected without moving
  the card or spending mana.
- Every targeted chosen mode receives its own target slot. Mandatory target
  availability gates mode selection; targets are committed before the spell
  reaches the stack, revalidated independently at resolution, and do not leak
  between modes. Modes resolve in printed order, including across a pending
  policy choice, while the whole spell fails to resolve only when all of its
  targets are illegal.
- Three Steps Ahead is exact across all seven non-empty combinations of its
  three modes. It counters the selected spell; copies a controlled artifact or
  creature using printed copiable values; and draws two before its controller
  chooses a discard. The tenth hand slot and third mode are publicly
  addressable, all mode costs are paid exactly once, partial target failure
  preserves legal and untargeted modes, and the supported card no longer enters
  the support-gap manifest.
- Target categories are zone-correct: a creature spell on the stack is a spell,
  not a creature permanent. An Anoint with Affliction regression proves that
  removal cannot select it while a legal battlefield creature remains visible.

This round closes the generic Spree announcement, payment, targeting, and
resolution transaction plus Three Steps Ahead's exact effect semantics. It
does **not** certify all 21 Spree cards. Each other card's chosen-mode effects
remain subject to the normal parser, card-specific scenarios, and support
manifest; malformed Spree mode text remains an explicit gap.

Gates: 295/295 scenarios, 9/9 smoke, 12/12 training, 11/11 + 5/5 Harvest, 6/6
fuzz/replay configuration, and 8,000/8,000 default-fuzz actions. Exact-source
CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_174419` completed 8,192/8,192
transitions at 105 rollout FPS with 16 terminals (2 decking, 4 life total, 10
turn limit). Final checkpoint reload validation passed all 256 steps, including
mask-valid prediction, finite rewards, public progress, and four short-cycle
checks; the run emitted a debug log only, with no warning or error file.

## Closed In Round 7.44

- Three Steps Ahead can no longer enter its incomplete Spree casting path as an
  ordinary `{U}` spell. The real-card parser preserves all three modes, the mask
  excludes the spell, and the support manifest reports the gap without repeated
  mask probes inflating its count.
- Mandatory targets are committed before triggered and loyalty abilities
  resolve. Missing targets fizzle cleanly, while targetless triggers retain
  their normal resolution path; Kaito's targeted loyalty ability is guarded end
  to end through target choice, stack placement, and resolution.
- Oildeep Gearhulk's combined `lifelink, ward {1}` clause registers each keyword
  once instead of duplicating a generic static layer effect.
- Draw effects preserve physical card id `0` and treat empty-library drawing as
  a completed rules event rather than an unimplemented instruction.
- Legal boards above 20 permanents keep exact scalar counts without observation
  degradation; the fixed-size per-card detail tensor remains capped at 20.
- Scripted-opponent execution failures now persist a deterministic replay for
  the agent action that entered the opponent loop.
- Card observations retain the full 225-field pool schema (all 48 subtype
  fields plus MDFC fields), legal signed live power/toughness, and exact
  component bounds instead of silently truncating the feature tail.
- Zur grants deathtouch, lifelink, and hexproof only to controlled enchantment
  creatures, updates later entrants, and removes the grants with its source.
- Card and archetype prevalence uses deck-seat share
  (`appearances / (2 * matches)`), keeping `play_rate` and `meta_share` within
  `[0, 1]`. TensorBoard terminal charts now expose cumulative counts and
  per-timestep rates on one policy-timestep axis.
- Mosswood Dreadknight's dies trigger grants a real, expiring graveyard cast of
  Dread Whispers; it uses the Adventure face's cost/effect and follows the
  normal Adventure exile/recast path.
- Sequential blocking can no longer strand one blocker on a menace attacker.
  The mask uses atomic multi-blocking to begin a menace block, binds ordinary
  blocks to their validated attacker, offers recovery for a stale partial
  declaration, and exposes action 439 only when it will execute.
- Overlord of the Hauntwoods' Impending cast now parses and pays its real
  `{1}{G}{G}` replacing cost. Sparse cost mappings are normalized, the mask and
  handler include taxes, reductions, and land auto-tapping, and Convoke, Delve,
  and Improvise reductions apply exactly once.

Run `ALPHA_ZERO_MTG_V3.00_20260711_145919` exposed the Spree mismatch and the
associated warning families; later fresh canaries exposed the observation,
Zur, stats, Mosswood, and menace paths. The exact combat failure state now has
permanent public-mask, duplicate-occurrence, and scripted-policy regressions,
and failed canary `ALPHA_ZERO_MTG_V3.00_20260711_163807` exposed the sparse
Impending replacing-cost failure. Exact-source CUDA canary
`ALPHA_ZERO_MTG_V3.00_20260711_165824` completed 8,192/8,192 transitions at 99
rollout FPS with 14 terminals (3 decking, 3 life total, 8 turn limit); final
validation passed with no warning or error file. Gates are 287/287 scenarios,
9/9 smoke, 12/12 training, 11/11 +
5/5 Harvest, 6/6 fuzz configuration, and 8,000/8,000 default-fuzz actions.
Correcting both the observation shape and declared bounds creates a checkpoint-
space boundary, so models saved before this round cannot be resumed.

## Closed In Round 7.42

- Targeted activated abilities are mask-valid only when at least one legal
  required target exists. Floodpits Drowner's shuffle action is absent without
  a creature bearing a stun counter and appears immediately when one exists.

Run `ALPHA_ZERO_MTG_V3.00_20260711_125937` found the mismatch through action
118 in a DimirSelf mirror. The exact 212-action replay now masks that action.
CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_131724` advanced the checkpoint from
65,536 to 73,728 steps and passed final validation without a new warning/error
file. Gates remain 270/270 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5
Harvest, 6/6 fuzz configuration, and 8,000/8,000 default-fuzz actions.

## Closed In Round 7.41

- Targeted casts preserve the complete timing window while the policy chooses
  targets. Duress can begin during transient priority over a main phase, select
  its opponent, and resume the same legal sorcery cast without losing its
  underlying phase.

Run `ALPHA_ZERO_MTG_V3.00_20260711_124331` exposed this through a mask-valid
Duress target action. Its exact 122-action replay now places Duress on the
stack. CUDA canary `ALPHA_ZERO_MTG_V3.00_20260711_125649` completed 8,192
transitions and final checkpoint validation without a new warning/error file.
Gates: 270/270 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest, 6/6
fuzz configuration, and 8,000/8,000 default-fuzz actions.

## Closed In Round 7.40

- Pawpatch Recruit now triggers only when one of its controller's creatures is
  targeted by an opponent-controlled spell or ability. Its own friendly
  targeted trigger cannot recursively trigger itself, and “other than that
  creature” excludes the original event target from the policy choice.
- Bushwhack's basic-land mode resolves as one search/reveal/hand/shuffle unit
  instead of leaving an unimplemented `put it into your hand` fragment.
- Parameterized Ward's internal static form registers cleanly in layer 6.
  Decking is ordinary game telemetry rather than a warning.

Run `ALPHA_ZERO_MTG_V3.00_20260711_120752` found the Recruit loop after its
stack reached 1,345 entries. Diagnostics now log an observation-bound failure
once and cap serialized stack detail at 32 entries. CUDA canary
`ALPHA_ZERO_MTG_V3.00_20260711_123455` completed 8,192 transitions and final
checkpoint validation without a new warning/error file. Gates: 269/269
scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest, 6/6 fuzz
configuration, and 8,000/8,000 default-fuzz actions.

## Closed In Round 7.39

- Fear of Isolation's mandatory return cost now distinguishes Player 2's
  physical permanent in a mirror match even when Player 1 has the same numeric
  card ID on their battlefield. The choosing controller's zone occurrence is
  authoritative, and ambiguous mirror ownership returns it to that controller.
- Deferred casting choices preserve the complete timing window. Mockingbird can
  choose X from transient priority over a main phase and resume the same legal
  creature cast without losing its sorcery-speed provenance.
- Trigger resolution now passes the source card name through to exact-card
  effect overrides. Caustic Bronco therefore uses its atomic reveal/hand/life
  implementation during real games rather than splitting into generic no-ops.

These were discovered by continuation run
`ALPHA_ZERO_MTG_V3.00_20260711_113539`. Its exact 121-action mirror replay now
completes the failed action. CUDA canary
`ALPHA_ZERO_MTG_V3.00_20260711_115902` added 8,192 transitions, passed final
checkpoint validation, and created no warning/error file. Gates: 267/267
scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest, 6/6 fuzz
configuration, and 8,000/8,000 default-fuzz actions.

## Closed In Round 7.38

- Bushwhack's fight mode is no longer mask-valid unless its two mandatory
  creature targets exist. The original 270-action training replay reaches the
  same board and now masks the impossible choice.
- Caustic Bronco's attack trigger now moves the revealed top card to hand and
  applies its printed unsaddled/saddled mana-value life loss. Its former generic
  no-op fragments are gone.
- Bare spell instructions such as Three Steps Ahead's `discard a card` bind to
  their controller rather than looking for an unselected target player.

These were discovered by strength run `ALPHA_ZERO_MTG_V3.00_20260711_110933`
after 32,768 learner transitions and are guarded by three exact scenarios.
The exact best-checkpoint CUDA resume completed another 8,192 transitions and
final validation with no warning/error file. Gates: 265/265 scenarios, 9/9
smoke, 12/12 training, 10/10 + 5/5 Harvest, 6/6 fuzz configuration, and
8,000/8,000 default-fuzz actions.

## Closed In Round 7.37

- Opening hands, draw turns, and actual play turns now reach both CardMemory and
  deck aggregates. The six-worker production canary populated all three paths;
  pre-7.37 draw/opening/turn-derived statistics remain ineligible for harvest.
- Reward is potential-difference based and terminal rewards are centralized.
  Turn-limit outcomes are deliberately worth less than natural game endings.
- The scripted baseline plays lands, casts affordable spells, and participates
  in combat. TensorBoard now separates reward components, terminal causes,
  rollout/learner time, process-tree CPU/RAM, and physical GPU utilization.
- Persistence is batched instead of rewriting the complete CardMemory after
  every game. Generic Ward layer parsing no longer emits false warnings.

The CUDA canary completed 6,144 transitions and final checkpoint validation
with no errors. Its 18 games all hit the turn limit, so it is diagnostic rather
than harvest-quality; the next fresh strength run must improve natural-terminal
rate before paired-seat promotion. Gates: 262/262 scenarios, 9/9 smoke, 12/12
training, 10/10 + 5/5 Harvest, 6/6 fuzz configuration, and 8,000/8,000 default
fuzz actions.

## Closed In Round 7.35

- Hopeless Nightmare's mask-valid action 21 failure was another transient
  priority timing mismatch: mask generation accepted the empty-stack main-phase
  wrapper, while spell execution required a literal main phase. Both now use
  the canonical sorcery-speed predicate, including face-aware timing for modal
  double-faced and Adventure casts.
- Ordinary `PLAY_SPELL` actions pin the observed hand slot, card ID, and
  controller through execution, reject stale slots, and preserve detailed
  failure context.
- Replay schema v2 records the agent seat and restores exact named deck seats
  regardless of the caller's deck-list ordering. The original 48-action replay
  now reaches and executes the formerly failing cast.

The exact failed checkpoint completed a six-worker, 12,288-transition CUDA
canary with a four-episode periodic evaluation and 256-step final validation.
Both validations passed and no new warning/error records were created. Gates:
256/256 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5 Harvest, 6/6 fuzz
configuration, and 8,000/8,000 default fuzz.

## Closed In Round 7.34

- The mask-valid Restless Anchorage failure was a timing-contract mismatch:
  the mask recognized an empty-stack `PHASE_PRIORITY` wrapper over a main
  phase, while `play_land()` required the literal main-phase number. Both now
  use the canonical sorcery-speed predicate, with priority still required.
- Failure replays convert NumPy runtime values without collapsing their
  structure, and failed atomic writes remove the temporary file. The regression
  verifies a complete replay containing a NumPy action history.

The same-seed six-worker CUDA rerun completed 12,288 transitions plus the
256-step final validation, passed the original failure point, and created no
new warning/error records. Gates: 255/255 scenarios, 9/9 smoke, 12/12 training,
10/10 + 5/5 Harvest, 6/6 fuzz configuration, and 8,000/8,000 default fuzz.

## Closed In Round 7.33

- `PLAY_LAND` actions now carry the observed card ID and controller as well as
  the encoded hand slot. Slot 6/action 19 is covered end to end across all 33
  real lands in the audited pool, stale slot rebinding is rejected, and future
  execution failures persist the exact policy state and replay instead of
  losing the worker-local cause.
- Plot, Saddle, and Mockingbird's copy-as-enters declaration lines no longer
  duplicate their dedicated mechanic paths as static layer effects.
- Ceased tokens keep last-known characteristics for their pending triggers and
  abilities without remaining in `card_db` or producing missing-card warnings.
- Unreset evaluation shutdown is quiet, and every model artifact—including the
  architecture summary—is contained under one `models/<run_id>/` directory.

The real two-worker CUDA canary completed 128 transitions and a 256-step final
validation with no new warning/error records and exactly one model directory
for the run. Gates: 255/255 scenarios, 9/9 smoke, 12/12 training, 10/10 + 5/5
Harvest, 6/6 fuzz configuration, and 8,000/8,000 default-fuzz actions.

## Closed In Round 7.32

- Leyline of Resonance's `copy that spell` instruction now copies the spell
  referenced by its CAST_SPELL trigger context and preserves the printed option
  to choose new targets.
- This Town Ain't Big Enough can legally resolve after its controller selects
  zero permanents; `up to two` is retained in the parsed bounce effect instead
  of becoming a mandatory generic target.
- Enchant restrictions, Leyline opening-hand permissions, and enters-tapped
  declarations are treated as already-handled rules/replacement text rather
  than registered as dead layer effects.
- Mixed integer card IDs and string token IDs no longer crash discard-policy
  synergy evaluation. Game-draw sentinels and non-copyable runtime stack
  references no longer produce false card/context warnings.

The real two-worker CUDA canary completed 128 transitions and a 256-step final
validation with zero newly written warning or error records. Gates: 253/253
scenarios, 9/9 smoke, 11/11 training, 10/10 + 5/5 Harvest, 6/6 fuzz config,
and 8,000/8,000 default-fuzz actions.

## Closed In Round 7.29

The final training canary exposed three value-changing gaps that construction-
time coverage had missed, plus several misleading warning sources.

- Hopeless Nightmare now makes each opponent finish the discard choice before
  losing 2 life. Its activated instruction sacrifices that exact source on
  resolution and cannot substitute another enchantment if the source left.
- Dredger's Insight mills its controller, offers only the artifact, creature,
  or land cards moved by that resolution, and permits decline. Its separate
  artifact/creature-leaves-your-graveyard trigger now gains life. Seed of Hope
  and Wrenn and Realmbreaker share the same permanent-selection path; Seed's
  life-gain suffix resumes after either choosing or declining.
- Nurturing Pixie's optional target keeps the non-Faerie, nonland, and
  controller restrictions. The source gets its counter only when the selected
  permanent actually reaches its owner's hand.
- Exhaust use is marked once by the cost transaction; the action handler no
  longer marks it again. A two-ability regression proves that using index 0
  masks only index 0 and that a direct retry cannot pay another cost.
- Initial deck aggregates calculate `avg_game_length` before validation, and
  optional `None` layer characteristics no longer flood Monster Role logs.
  Warnings remain intact for missing non-`None` characteristics.

The warning-enabled exact regressions contain none of the old life-loss,
mill-target, generic self-sacrifice, or fragmented Pixie signatures. An
eight-deck reset likewise produces zero strict-separator and Role optional-
attribute warnings. Gates: 241/241 scenarios, 9/9 smoke, 11/11 training, and
8/8 deterministic default-fuzz seeds (8,000 mask-valid actions).

## Closed In Round 7.21

The remaining seven-part Tier 2 batch is implemented and guarded.

- Caustic Bronco exposes Saddle at sorcery speed, lets the policy select any
  number of other untapped creatures, requires total power 3, taps the chosen
  creatures together, and clears the saddled designation during cleanup.
- Duress exposes only noncreature, nonland cards from the targeted opponent's
  revealed hand. Oildeep Gearhulk exposes every card, permits declining, and
  performs the chosen discard followed by the replacement draw.
- Cacophony Scamp exposes its sacrifice as an optional policy decision and
  proliferates only after the sacrifice succeeds.
- Leyline of Resonance recognizes CAST_SPELL events only for an instant or
  sorcery with exactly one target, where that target is a creature controlled
  by the caster. Optimistic Scavenger now recognizes both friendly enchantment
  entry and ROOM_FULLY_UNLOCKED events.
- Patchwork Beastie's Delirium restriction is enforced by attack and block
  legality using distinct graveyard card types.
- The real-card path sweep covers Kaito's full Ninjutsu cost, both Exhaust
  cards, both Impending Overlords, both Offspring cards, Burst Lightning's
  Kicker, Pest Control's Cycling, Three Steps Ahead's Spree modes, and
  Bushwhack's Fight route. It found and fixed truncated multi-symbol Ninjutsu
  costs and impossible Offspring/Impending cost regex boundaries.
- `EffectFactory.register_card_override()` provides the exact-name,
  hand-written-effect escape hatch before generic parsing.

## High-Risk Partial Support

None currently known in the audited eight-deck sample. Round 7.30 added exact
real-card scenarios for both Overlords, Obstinate Baloth, Callous Sell-Sword,
Manifold Mouse, and Emberheart Challenger, and removed their classification
warnings. This is sample coverage, not format-wide proof; new work remains
ordered by real Harvest manifest counts.

## Closed In Round 7.22

Beza's Treasure is verified end to end. Its printed ability registers as a
mana ability, pays tap and self-sacrifice costs atomically, exposes W/U/B/R/G
to the policy, adds exactly one mana of the selected color, and resolves
without using the stack. The scenario uncovered two shared defects: the early
activated-ability parser did not promote mana-producing text to ManaAbility,
and EnhancedManaSystem lacked the `add_mana` entry point already used by
ManaAbility. Both are repaired for all parsed permanent mana abilities.

## Implemented Paths To Verify With Real Cards

The engine has dedicated paths, and some have generic scenario coverage, but
each sample card's complete text still needs a card-specific scenario:

Round 7.21 added an exact-name mechanic-entry contract for every card formerly
listed here. Future deeper scenarios should continue to expand resolution
branches, but these cards no longer rely on wholly unproved routing.

## Recommended Order

1. ✅ Run harvest fixtures and rank any new manifest entries by observed count.
   `harvest_fixtures.py` now performs a strict seeded rotation across all eight
   decks, rejects incomplete/error games, and writes a success-only run manifest.
   The seed-20260710 baseline completed 8/8 games with zero fidelity counters
   and no support-manifest entries. The parallel checkpoint-aware Harvest
   protocol is also complete; its final two-worker plumbing run completed 2/2
   games at 0.296 games/second with no fidelity or support-manifest issue.
2. ✅ Begin Tier 4 property tests and invariant fuzzing. The deterministic
   harness is green through the strict 32-seed x 10,000-action long profile
   (320,000/320,000). It guards card conservation, mask/handler execution,
   observation bounds and non-degradation, mask purity, SBA/layer fixed points,
   finite rewards, exact replay contexts, and phase-boundary mana clearing.
3. ✅ The shared target-format foundation now supplies stable canonical card
   IDs, frozen versioned feature schemas, explicit format/corpus lineage, and
   generalized production Harvest. Standard also has a pinned representative
   metagame and format-aware ingestion for user-supplied deck lists.
4. **Next:** continue the impact-ranked Standard coverage sweep, then qualify
   the Standard policy, promote it into a checkpoint league, and calibrate
   known matchups. Deliver Modern and Pioneer afterward, before enabling the
   automatic deck-builder feedback loop. Treat random-valid fixture results as
   plumbing/support evidence only, never as card-strength statistics.
