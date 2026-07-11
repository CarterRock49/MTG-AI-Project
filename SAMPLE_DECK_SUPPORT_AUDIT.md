# Sample Deck Support Audit

Audit date: July 2026

## Scope

- 8 sample decks in `Decks/`
- 480 total deck slots
- 110 unique cards
- Card text, layouts, keywords, costs, choices, tokens, linked effects, and
  mana abilities compared against reachable engine paths

Copy counts below are summed across all eight decks. Categories can overlap.
"Confirmed gap" means there is no complete engine path for the relevant
rules behavior. "Needs card scenario" means related code exists, but the
actual sample card has not yet proved that path end to end.

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

None currently confirmed in the audited eight-deck sample. Rounds 7.30–7.34
closed the remaining classification groups and the first strength-run warning
and error signatures with exact scenarios.

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
3. **Next:** train and benchmark a checkpoint against scripted play before
   promoting harvest runs to policy-vs-policy. Treat the random-valid fixture
   results as plumbing/support evidence only, never as card-strength statistics.
