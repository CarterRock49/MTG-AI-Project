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

This closes the stun mechanic, not every ability on those cards. Kaito still
depends on emblem and type-changing support, while Floodpits Drowner's second
ability still needs its stun-counter target filter and two-object shuffle.

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

This closes these mechanics, not every line on the cards. Heartfire Hero's
power-based death damage still needs last-known-power support. The Witch's
Vanity still inherits the separate Food activated-ability gap listed below.

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

## Confirmed Gaps

These should make affected card statistics ineligible for harvest until the
listed behavior is implemented and guarded by scenarios.

| Mechanic or rule object | Copies | Affected cards | Missing behavior |
| --- | ---: | --- | --- |
| Map tokens | 13 | Get Lost (3), Restless Anchorage (2), Sentinel of the Nameless City (4), Spyglass Siren (4) | Token's activated sacrifice and agent-controlled explore |
| Emblems and Kaito's changing card type | 11 | Kaito, Bane of Nightmares (9), Wrenn and Realmbreaker (2) | Command-zone emblem effects and Kaito's turn/loyalty layer changes |
| Stun-linked shuffle | 8 | Floodpits Drowner (8) | Require a creature with a stun counter, then shuffle source and target into their owners' libraries |
| Death return with type change | 6 | Enduring Curiosity (6) | Return only after dying as a creature, then remove creature type |
| Bounded copy-as-enters replacement | 6 | Mockingbird (6) | Agent target choice, mana-spent bound, copy exception, added type/keyword |
| Domain effect value | 1 | Herd Migration (1) | Use the distinct basic-land-type count for its token effect |
| Food token activated ability | 5 | Restless Cottage (4), The Witch's Vanity (1) | Food can be created, but its pay/tap/sacrifice life ability is absent |
| Plot | 4 | Slickshot Show-Off (4) | Sorcery-speed exile cost and later free-cast permission |
| Bargain | 4 | Torch the Tower (4) | Optional sacrifice cost and bargained resolution branch |
| Manifest dread | 4 | Turn Inside Out (4) | Two-card look, agent selection, graveyard move, and face-down permanent |
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
| Power-based death damage | 8 | Heartfire Hero (8): use last-known power after the source has left the battlefield |
| Delirium | 8 | Patchwork Beastie (4), Fear of Missing Out (4): four-card-type threshold and conditional effects |
| Eerie | 4 | Optimistic Scavenger (4): enchantment entry and fully-unlocked Room event |
| Damage reflexive/optional branches | 8 | Screaming Nemesis (4), Cacophony Scamp (4) |
| Hand-information choices | 8 | Duress (4), Oildeep Gearhulk (4): chooser, visibility, and legal-card filtering |
| Restless-land animation and attack riders | 16 | Anchorage (2), Cottage (4), Reef (7), Ridgeline (3) |
| Map/explore decisions | 13 | Explore exists, but the keep-or-graveyard decision is currently strategic auto-resolution |

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

1. Map tokens and agent-controlled explore, because 13 slots otherwise gain
   incomplete card advantage and hide a player decision.
2. Floodpits Drowner's stun-linked shuffle, to finish the remaining unsupported
   text on the audit's highest-copy stun card.
3. Heartfire Hero's power-based death damage, to complete the remaining text on
   the highest-copy card touched in Round 7.14.
4. Emblems and Kaito's changing type, because 11 slots still cross a missing
   command-zone and continuous-type boundary.
5. Enduring Curiosity's dies-as-a-creature return, because 6 slots still depend
   on a zone-change trigger followed by a type-changing battlefield return.
