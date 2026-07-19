"""Regressions for target/scope warnings found by the 181929 canary."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.ability_utils import EffectFactory  # noqa: E402
from Playersim.card import Card  # noqa: E402
from Playersim.game_state import GameState  # noqa: E402


logging.disable(logging.CRITICAL)


def card(name, type_line, text="", power=None, toughness=None):
    data = {
        "name": name, "type_line": type_line, "oracle_text": text,
        "mana_cost": "", "cmc": 0, "color_identity": [],
    }
    if power is not None:
        data.update({"power": power, "toughness": toughness})
    return Card(data)


class CanaryEffectBindingTest(unittest.TestCase):
    def _state(self, cards):
        game_state = GameState(cards)
        game_state.reset(list(cards), list(cards), seed=181929)
        for player in (game_state.p1, game_state.p2):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
        return game_state

    def test_each_opposing_creature_damage_is_not_targeted(self):
        game_state = self._state({
            0: card("Iroh's Demonstration", "Sorcery"),
            1: card("Friendly", "Creature - Soldier", power=2, toughness=2),
            2: card("Opponent One", "Creature - Bear", power=3, toughness=3),
            3: card("Opponent Two", "Creature - Bear", power=4, toughness=4),
        })
        game_state.p1["battlefield"] = [1]
        game_state.p2["battlefield"] = [2, 3]
        effect = EffectFactory.create_effects(
            "Iroh's Demonstration deals 1 damage to each creature your "
            "opponents control.")[0]

        self.assertEqual(effect.target_type,
                         "each creature your opponents control")
        self.assertFalse(effect.requires_target)
        self.assertTrue(effect.apply(game_state, 0, game_state.p1, {}))
        self.assertNotIn(1, game_state.p1.get("damage_counters", {}))
        self.assertEqual(game_state.p2["damage_counters"], {2: 1, 3: 1})

    def test_source_power_counters_apply_to_each_controlled_creature(self):
        text = ("Put X +1/+1 counters on each creature you control, where X "
                "is this creature's power.")
        game_state = self._state({
            0: card("Ouroboroid", "Creature - Snake", text,
                    power=3, toughness=3),
            1: card("Ally", "Creature - Elf", power=1, toughness=1),
            2: card("Enemy", "Creature - Bear", power=2, toughness=2),
        })
        game_state.p1["battlefield"] = [0, 1]
        game_state.p2["battlefield"] = [2]
        effect = EffectFactory.create_effects(text)[0]

        self.assertEqual(effect.target_type, "each creature you control")
        self.assertEqual(effect.base_count, "source_power")
        self.assertFalse(effect.requires_target)
        self.assertTrue(effect.apply(game_state, 0, game_state.p1, {}))
        self.assertEqual(game_state._safe_get_card(0).counters["+1/+1"], 3)
        self.assertEqual(game_state._safe_get_card(1).counters["+1/+1"], 3)
        self.assertNotIn("+1/+1", game_state._safe_get_card(2).counters)

    def test_nonland_permanent_bounce_keeps_mixed_target_categories(self):
        game_state = self._state({
            0: card("This Town Ain't Big Enough", "Instant"),
            1: card("Friendly Enchantment", "Enchantment"),
            2: card("Opposing Creature", "Creature - Bear",
                    power=2, toughness=2),
        })
        game_state.p1["battlefield"] = [1]
        game_state.p2["battlefield"] = [2]
        effect = EffectFactory.create_effects(
            "Return up to two target nonland permanents to their owners' "
            "hands.")[0]

        self.assertEqual(effect.target_type, "permanent")
        self.assertEqual((effect.min_targets, effect.max_targets), (0, 2))
        self.assertTrue(effect.apply(
            game_state, 0, game_state.p1,
            {"enchantments": [1], "creatures": [2]}))
        self.assertIn(1, game_state.p1["hand"])
        self.assertIn(2, game_state.p2["hand"])

    def test_up_to_two_counter_instruction_accepts_zero_one_or_two_targets(self):
        clause = (
            "Put a +1/+1 counter on each of up to two target creatures.")
        game_state = self._state({
            0: card("Elegant Rotunda", "Enchantment - Room"),
            1: card("First Counter Target", "Creature - Scout",
                    power=1, toughness=1),
            2: card("Second Counter Target", "Creature - Scout",
                    power=2, toughness=2),
        })
        game_state.p1["battlefield"] = [0, 1, 2]
        effect = EffectFactory.create_effects(clause)[0]

        self.assertEqual(type(effect).__name__, "AddCountersEffect")
        self.assertTrue(effect.requires_target)
        self.assertEqual((effect.min_targets, effect.max_targets), (0, 2))
        self.assertTrue(effect.apply(game_state, 0, game_state.p1, {}))
        self.assertNotIn("+1/+1", game_state._safe_get_card(1).counters)
        self.assertNotIn("+1/+1", game_state._safe_get_card(2).counters)

        self.assertTrue(effect.apply(
            game_state, 0, game_state.p1, {"creatures": [1]}))
        self.assertEqual(
            game_state._safe_get_card(1).counters["+1/+1"], 1)
        self.assertNotIn("+1/+1", game_state._safe_get_card(2).counters)

        self.assertTrue(effect.apply(
            game_state, 0, game_state.p1, {"creatures": [1, 2]}))
        self.assertEqual(
            game_state._safe_get_card(1).counters["+1/+1"], 2)
        self.assertEqual(
            game_state._safe_get_card(2).counters["+1/+1"], 1)

    def test_revelation_binds_spell_bounce_and_damage_separately(self):
        oracle_text = (
            "Return target spell or permanent to its owner's hand. Jeskai "
            "Revelation deals 4 damage to any target. Create two 1/1 white "
            "Monk creature tokens with prowess. Draw two cards. You gain 4 "
            "life.")
        game_state = self._state({
            0: card("Jeskai Revelation", "Instant", oracle_text),
            1: card("Stack Target", "Instant"),
            2: card("Permanent Target", "Artifact"),
        })
        game_state.card_instance_owners[1] = "p2"
        game_state.p2["battlefield"] = [2]
        game_state.stack = [("SPELL", 1, game_state.p2, {})]
        slots = game_state._ordinary_target_slots(oracle_text)
        self.assertEqual(
            [slot["required_type"] for slot in slots],
            ["spell_or_permanent", "any"])
        valid_union = game_state.targeting_system.get_valid_targets(
            0, game_state.p1, "spell_or_permanent",
            effect_text=slots[0]["effect_text"])
        self.assertEqual(set(valid_union["spell_or_permanent"]), {1, 2})

        effects, parsed_all = game_state._ordinary_instruction_effects(
            game_state._safe_get_card(0), oracle_text, {
                "instruction_target_slots": slots,
                "targets_by_slot": [[1], ["p2"]],
            })
        self.assertTrue(parsed_all)
        bounce = next(effect for effect in effects
                      if type(effect).__name__ == "ReturnToHandEffect")
        damage = next(effect for effect in effects
                      if type(effect).__name__ == "DamageEffect")
        self.assertEqual(bounce.target_type, "spell or permanent")
        self.assertEqual(bounce._bound_targets, {"spells": [1]})
        self.assertEqual(damage._bound_targets, {"players": ["p2"]})

        life_before = game_state.p2["life"]
        self.assertTrue(bounce.apply(
            game_state, 0, game_state.p1, bounce._bound_targets))
        self.assertEqual(game_state.stack, [])
        self.assertIn(1, game_state.p2["hand"])
        self.assertTrue(damage.apply(
            game_state, 0, game_state.p1, damage._bound_targets))
        self.assertEqual(game_state.p2["life"], life_before - 4)


if __name__ == "__main__":
    unittest.main()
