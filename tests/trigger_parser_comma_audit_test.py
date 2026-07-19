"""Full-pool regressions for comma-heavy triggered-ability boundaries."""

import unittest

from Playersim.ability_types import TriggeredAbility


CASES = [
    (
        "Ashling enters or transforms",
        "Whenever this creature enters or transforms into Ashling, Rekindled, "
        "you may discard a card. If you do, draw a card",
        "Whenever this creature enters or transforms into Ashling, Rekindled",
        "you may discard a card. If you do, draw a card",
    ),
    (
        "Ashling transforms and main phase",
        "Whenever this creature transforms into Ashling, Rimebound and at the "
        "beginning of your first main phase, add two mana of any one color. "
        "Spend this mana only to cast spells with mana value 4 or greater",
        "Whenever this creature transforms into Ashling, Rimebound and at the "
        "beginning of your first main phase",
        "add two mana of any one color. Spend this mana only to cast spells "
        "with mana value 4 or greater",
    ),
    (
        "Avatar Aang bend list",
        "Whenever you waterbend, earthbend, firebend, or airbend, draw a card. "
        "Then if you've done all four this turn, transform Avatar Aang",
        "Whenever you waterbend, earthbend, firebend, or airbend",
        "draw a card. Then if you've done all four this turn, transform Avatar Aang",
    ),
    (
        "Blood Spatter Analysis singular die",
        "Whenever one or more creatures die, mill a card and put a bloodstain "
        "counter on this enchantment. Then sacrifice it if it has five or more "
        "bloodstain counters on it. When you do, return target creature card "
        "from your graveyard to your hand",
        "Whenever one or more creatures die",
        "mill a card and put a bloodstain counter on this enchantment. Then "
        "sacrifice it if it has five or more bloodstain counters on it. When "
        "you do, return target creature card from your graveyard to your hand",
    ),
    (
        "Brigid transform name",
        "Whenever this creature enters or transforms into Brigid, Clachan's "
        "Heart, create a 1/1 green and white Kithkin creature token",
        "Whenever this creature enters or transforms into Brigid, Clachan's Heart",
        "create a 1/1 green and white Kithkin creature token",
    ),
    (
        "Byway Barterer expend",
        "Whenever you expend 4, you may discard your hand. If you do, draw two cards",
        "Whenever you expend 4",
        "you may discard your hand. If you do, draw two cards",
    ),
    (
        "Fugitive Codebreaker turned face up",
        "When this creature is turned face up, discard your hand, then draw three cards",
        "When this creature is turned face up",
        "discard your hand, then draw three cards",
    ),
    (
        "Grub transform name",
        "Whenever this creature enters or transforms into Grub, Storied "
        "Matriarch, return up to one target Goblin card from your graveyard to "
        "your hand",
        "Whenever this creature enters or transforms into Grub, Storied Matriarch",
        "return up to one target Goblin card from your graveyard to your hand",
    ),
    (
        "Guru Pathik spell-type list",
        "Whenever you cast a Lesson, Saga, or Shrine spell, put a +1/+1 counter "
        "on another target creature you control",
        "Whenever you cast a Lesson, Saga, or Shrine spell",
        "put a +1/+1 counter on another target creature you control",
    ),
    (
        "Mazemind Tome counter threshold",
        "When there are four or more page counters on this artifact, exile it. "
        "If you do, you gain 4 life",
        "When there are four or more page counters on this artifact",
        "exile it. If you do, you gain 4 life",
    ),
    (
        "Mistway Spy nested duration trigger",
        "When this creature is turned face up, until end of turn, whenever a "
        "creature you control deals combat damage to a player, investigate",
        "When this creature is turned face up",
        "until end of turn, whenever a creature you control deals combat "
        "damage to a player, investigate",
    ),
    (
        "Muerra expend",
        "Whenever you expend 8, exile the top two cards of your library. Until "
        "the end of your next turn, you may play those cards",
        "Whenever you expend 8",
        "exile the top two cards of your library. Until the end of your next "
        "turn, you may play those cards",
    ),
    (
        "Party Dude passive attacked",
        "Whenever one or more of your opponents are attacked, up to one target "
        "attacking creature gets +X/+X until end of turn, where X is the number "
        "of cards in your hand",
        "Whenever one or more of your opponents are attacked",
        "up to one target attacking creature gets +X/+X until end of turn, "
        "where X is the number of cards in your hand",
    ),
    (
        "Questing Druid color list",
        "Whenever you cast a spell that's white, blue, black, or red, put a "
        "+1/+1 counter on this creature",
        "Whenever you cast a spell that's white, blue, black, or red",
        "put a +1/+1 counter on this creature",
    ),
    (
        "Sygg Wanderwine transform name",
        "Whenever this creature enters or transforms into Sygg, Wanderwine "
        "Wisdom, target creature gains \"Whenever this creature deals combat "
        "damage to a player or planeswalker, draw a card\" until end of turn",
        "Whenever this creature enters or transforms into Sygg, Wanderwine Wisdom",
        "target creature gains \"Whenever this creature deals combat damage "
        "to a player or planeswalker, draw a card\" until end of turn",
    ),
    (
        "Sygg Wanderbrine transform name",
        "Whenever this creature transforms into Sygg, Wanderbrine Shield, "
        "target creature you control gains protection from each color until "
        "your next turn",
        "Whenever this creature transforms into Sygg, Wanderbrine Shield",
        "target creature you control gains protection from each color until "
        "your next turn",
    ),
    (
        "Talion characteristic list",
        "Whenever an opponent casts a spell with mana value, power, or "
        "toughness equal to the chosen number, that player loses 2 life and "
        "you draw a card",
        "Whenever an opponent casts a spell with mana value, power, or "
        "toughness equal to the chosen number",
        "that player loses 2 life and you draw a card",
    ),
    (
        "Millennium Calendar thousands separators",
        "When there are 1,000 or more time counters on The Millennium Calendar, "
        "sacrifice it and each opponent loses 1,000 life",
        "When there are 1,000 or more time counters on The Millennium Calendar",
        "sacrifice it and each opponent loses 1,000 life",
    ),
    (
        "Trystan Cultivator transform name",
        "Whenever this creature enters or transforms into Trystan, Callous "
        "Cultivator, mill three cards. Then if there is an Elf card in your "
        "graveyard, you gain 2 life",
        "Whenever this creature enters or transforms into Trystan, Callous Cultivator",
        "mill three cards. Then if there is an Elf card in your graveyard, you gain 2 life",
    ),
    (
        "Trystan Culler transform name",
        "Whenever this creature transforms into Trystan, Penitent Culler, mill "
        "three cards, then you may exile an Elf card from your graveyard. If "
        "you do, each opponent loses 2 life",
        "Whenever this creature transforms into Trystan, Penitent Culler",
        "mill three cards, then you may exile an Elf card from your graveyard. "
        "If you do, each opponent loses 2 life",
    ),
    (
        "Unyielding Gatekeeper turned face up",
        "When this creature is turned face up, exile another target nonland "
        "permanent. If you controlled it, return it to the battlefield tapped. "
        "Otherwise, its controller creates a 2/2 white and blue Detective "
        "creature token",
        "When this creature is turned face up",
        "exile another target nonland permanent. If you controlled it, return "
        "it to the battlefield tapped. Otherwise, its controller creates a "
        "2/2 white and blue Detective creature token",
    ),
]


SECOND_PASS_CASES = [
    (
        "Ashroot Animist effect starts with another",
        "Whenever this creature attacks, another target creature you control "
        "gains trample and gets +X/+X until end of turn, where X is this "
        "creature's power",
        "Whenever this creature attacks",
        "another target creature you control gains trample and gets +X/+X "
        "until end of turn, where X is this creature's power",
    ),
    (
        "Assimilation Aegis duration effect",
        "Whenever this Equipment becomes attached to a creature, for as long "
        "as this Equipment remains attached to it, that creature becomes a "
        "copy of a creature card exiled with this Equipment",
        "Whenever this Equipment becomes attached to a creature",
        "for as long as this Equipment remains attached to it, that creature "
        "becomes a copy of a creature card exiled with this Equipment",
    ),
    (
        "Beifong earthbend effect",
        "Whenever a nonland creature you control dies, earthbend X, where X is "
        "that creature's power",
        "Whenever a nonland creature you control dies",
        "earthbend X, where X is that creature's power",
    ),
    (
        "Tecutlan discover effect",
        "Whenever you cast a permanent spell using mana produced by Tecutlan, "
        "discover X, where X is that spell's mana value",
        "Whenever you cast a permanent spell using mana produced by Tecutlan",
        "discover X, where X is that spell's mana value",
    ),
    (
        "Caustic Bronco reveal effect",
        "Whenever this creature attacks, reveal the top card of your library "
        "and put it into your hand. You lose life equal to that card's mana "
        "value if this creature isn't saddled. Otherwise, each opponent loses "
        "that much life",
        "Whenever this creature attacks",
        "reveal the top card of your library and put it into your hand. You "
        "lose life equal to that card's mana value if this creature isn't "
        "saddled. Otherwise, each opponent loses that much life",
    ),
    (
        "Craterhoof creature-subject effect",
        "When this creature enters, creatures you control gain trample and get "
        "+X/+X until end of turn, where X is the number of creatures you control",
        "When this creature enters",
        "creatures you control gain trample and get +X/+X until end of turn, "
        "where X is the number of creatures you control",
    ),
    (
        "Earthbender Ascension earthbend effect",
        "When this enchantment enters, earthbend 2. Then search your library "
        "for a basic land card, put it onto the battlefield tapped, then shuffle",
        "When this enchantment enters",
        "earthbend 2. Then search your library for a basic land card, put it "
        "onto the battlefield tapped, then shuffle",
    ),
    (
        "Firebender Ascension caused trigger",
        "Whenever a creature you control attacking causes a triggered ability "
        "of that creature to trigger, put a quest counter on this enchantment. "
        "Then if it has four or more quest counters on it, you may copy that "
        "ability. You may choose new targets for the copy",
        "Whenever a creature you control attacking causes a triggered ability "
        "of that creature to trigger",
        "put a quest counter on this enchantment. Then if it has four or more "
        "quest counters on it, you may copy that ability. You may choose new "
        "targets for the copy",
    ),
    (
        "Fishing Pole remove effect",
        "Whenever equipped creature becomes untapped, remove a bait counter "
        "from this Equipment. If you do, create a 1/1 blue Fish creature token",
        "Whenever equipped creature becomes untapped",
        "remove a bait counter from this Equipment. If you do, create a 1/1 "
        "blue Fish creature token",
    ),
    (
        "Hemosymbic Mite effect starts with another",
        "Whenever this creature becomes tapped, another target creature you "
        "control gets +X/+X until end of turn, where X is this creature's power",
        "Whenever this creature becomes tapped",
        "another target creature you control gets +X/+X until end of turn, "
        "where X is this creature's power",
    ),
]


SECOND_PASS_CASES += [
    (
        "Hollow Marauder any-number effect",
        "When this creature enters, any number of target opponents each discard "
        "a card. For each of those opponents who didn't discard a card with "
        "mana value 4 or greater, draw a card",
        "When this creature enters",
        "any number of target opponents each discard a card. For each of those "
        "opponents who didn't discard a card with mana value 4 or greater, "
        "draw a card",
    ),
    (
        "Kellan reveal effect",
        "Whenever Kellan attacks, reveal the top card of your library. If it's "
        "a creature card with mana value 3 or less, put it into your hand. "
        "Otherwise, you may put it into your graveyard",
        "Whenever Kellan attacks",
        "reveal the top card of your library. If it's a creature card with "
        "mana value 3 or less, put it into your hand. Otherwise, you may put "
        "it into your graveyard",
    ),
    (
        "Moonshaker creature-subject effect",
        "When this creature enters, creatures you control gain flying and get "
        "+X/+X until end of turn, where X is the number of creatures you control",
        "When this creature enters",
        "creatures you control gain flying and get +X/+X until end of turn, "
        "where X is the number of creatures you control",
    ),
    (
        "Severance Priest definite-subject effect",
        "When this creature leaves the battlefield, the exiled card's owner "
        "creates an X/X white Spirit creature token, where X is the mana value "
        "of the exiled card",
        "When this creature leaves the battlefield",
        "the exiled card's owner creates an X/X white Spirit creature token, "
        "where X is the mana value of the exiled card",
    ),
    (
        "Shantotto named-subject effect",
        "Whenever you cast a noncreature spell, Shantotto gets +X/+0 until end "
        "of turn, where X is the amount of mana spent to cast that spell. If "
        "X is 4 or more, draw a card",
        "Whenever you cast a noncreature spell",
        "Shantotto gets +X/+0 until end of turn, where X is the amount of mana "
        "spent to cast that spell. If X is 4 or more, draw a card",
    ),
    (
        "Tishana counter effect",
        "When this creature enters, counter up to one target activated or "
        "triggered ability. If an ability of an artifact, creature, or "
        "planeswalker is countered this way, that permanent loses all abilities "
        "for as long as this creature remains on the battlefield",
        "When this creature enters",
        "counter up to one target activated or triggered ability. If an ability "
        "of an artifact, creature, or planeswalker is countered this way, that "
        "permanent loses all abilities for as long as this creature remains on "
        "the battlefield",
    ),
    (
        "The Boulder earthbend effect",
        "Whenever The Boulder attacks, earthbend X, where X is the number of "
        "creatures you control with power 4 or greater",
        "Whenever The Boulder attacks",
        "earthbend X, where X is the number of creatures you control with power "
        "4 or greater",
    ),
    (
        "Toph earthbend effect",
        "Whenever you cast a spell, earthbend 1. If that spell is a Lesson, put "
        "an additional +1/+1 counter on that land",
        "Whenever you cast a spell",
        "earthbend 1. If that spell is a Lesson, put an additional +1/+1 "
        "counter on that land",
    ),
    (
        "War Machine effect starts with another",
        "At the beginning of combat on your turn, another target creature you "
        "control gets +X/+0 until end of turn, where X is War Machine's power",
        "At the beginning of combat on your turn",
        "another target creature you control gets +X/+0 until end of turn, "
        "where X is War Machine's power",
    ),
    (
        "Wildwood Mentor effect starts with another",
        "Whenever this creature attacks, another target attacking creature gets "
        "+X/+X until end of turn, where X is this creature's power",
        "Whenever this creature attacks",
        "another target attacking creature gets +X/+X until end of turn, "
        "where X is this creature's power",
    ),
]


class TriggerParserCommaAuditTest(unittest.TestCase):
    def test_full_pool_comma_heavy_boundaries(self):
        for label, text, expected_condition, expected_effect in CASES:
            with self.subTest(label=label):
                self.assertEqual(
                    TriggeredAbility._parse_condition_effect(None, text),
                    (expected_condition, expected_effect),
                )

    def test_second_pass_effect_starter_boundaries(self):
        for label, text, expected_condition, expected_effect in SECOND_PASS_CASES:
            with self.subTest(label=label):
                self.assertEqual(
                    TriggeredAbility._parse_condition_effect(None, text),
                    (expected_condition, expected_effect),
                )


if __name__ == "__main__":
    unittest.main()
