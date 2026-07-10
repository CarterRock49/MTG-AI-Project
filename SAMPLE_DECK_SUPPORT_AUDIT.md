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
then sacrifices the selected permanent as a casting cost. Torch deals 2 when
declined or 3 and scries 1 when bargained. A creature it actually damaged is
exiled instead if it would die later that turn.

Manifest dread and Turn Inside Out are supported for all 4 copies. The policy
chooses one of the top two cards to put onto the battlefield face down and the
other moves to the graveyard; one-card and empty libraries are handled without
phantom choices. A face-down object exposes only colorless 2/2 creature
characteristics, and a creature card can turn face up for its mana cost without
entering again. Turn Inside Out creates a one-shot, same-turn death trigger for
only its chosen creature.

## Confirmed Gaps

These should make affected card statistics ineligible for harvest until the
listed behavior is implemented and guarded by scenarios.

| Mechanic or rule object | Copies | Affected cards | Missing behavior |
| --- | ---: | --- | --- |
| Domain effect value | 1 | Herd Migration (1) | Use the distinct basic-land-type count for its token effect |
| Additional combat | 4 | Fear of Missing Out (4) | Untap chosen creature and insert one additional combat phase |
| Opening-hand replacement | 4 | Leyline of Resonance (4) | Begin-game battlefield choice before normal turn play |
| Rest-of-game life restriction | 4 | Screaming Nemesis (4) | Persistent player effect preventing future life gain |
| Corrupted | 4 | Anoint with Affliction (4) | Opponent poison threshold and conditional exile targeting |
| Forced permanent sacrifice count | 4 | Phyrexian Obliterator (4) | Damage-source controller chooses and sacrifices N permanents |
| Incubate | 2 | Sunfall (2) | Incubator token, X counters, and paid transform ability |
| Saddle | 1 | Caustic Bronco (1) | Creature-tapping cost, saddled state, and attack rider |
| Multi-condition ETB | 1 | Beza, the Bounding Spring (1) | Four independent comparisons and conditional effects |
| Chosen creature type mana | 3 | Cavern of Souls (3) | As-enters type choice, type-restricted spend, uncounterable rider |

## High-Risk Partial Support

These cards reach generic parser paths, but at least one value-changing part
is likely incomplete. They need focused scenarios before their statistics are
trusted.

| Mechanic or behavior | Copies | Cards or concern |
| --- | ---: | --- |
| Delirium | 8 | Patchwork Beastie (4), Fear of Missing Out (4): four-card-type threshold and conditional effects |
| Eerie | 4 | Optimistic Scavenger (4): enchantment entry and fully-unlocked Room event |
| Damage reflexive/optional branches | 8 | Screaming Nemesis (4), Cacophony Scamp (4) |
| Hand-information choices | 8 | Duress (4), Oildeep Gearhulk (4): chooser, visibility, and legal-card filtering |
| Restless-land animation and attack riders | 16 | Anchorage (2), Cottage (4), Reef (7), Ridgeline (3) |

## Implemented Paths To Verify With Real Cards

The engine has dedicated paths, and some have generic scenario coverage, but
each sample card's complete text still needs a card-specific scenario:

- Ninjutsu: Kaito, Bane of Nightmares (9)
- Exhaust: Afterburner Expert (4), Draconautics Engineer (4)
- Impending: Overlord of the Hauntwoods (4), Overlord of the Mistmoors (3)
- Offspring: Manifold Mouse (4), Pawpatch Recruit (2)
- Kicker: Burst Lightning (4)
- Proliferate: Cacophony Scamp (4)
- Cycling: Pest Control (2)
- Spree: Three Steps Ahead (1)
- Fight: Bushwhack (4)

## Recommended Order

1. Herd Migration's Domain effect value, because the shared distinct-basic-land-
   type counter already exists and only its token effect still needs to consume it.
2. Fear of Missing Out's additional combat, because all 4 copies need both the
   chosen-creature untap and a real inserted combat phase.
3. Leyline of Resonance's opening-hand replacement, because all 4 copies need a
   pre-turn battlefield choice rather than being evaluated as ordinary casts.
4. Screaming Nemesis's rest-of-game life restriction, because all 4 copies can
   otherwise produce seriously misleading damage and recovery statistics.
5. Anoint with Affliction's Corrupted branch, because all 4 copies need the
   opponent-poison threshold and its broader conditional exile targeting.
