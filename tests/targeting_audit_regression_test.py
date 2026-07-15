"""Focused regressions for target legality, parsing, and mask parity."""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from Playersim.actions import ActionHandler
from Playersim.card import Card
from Playersim.game_state import GameState


def card(name, type_line, oracle_text="", **values):
    data = {
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "mana_cost": values.pop("mana_cost", ""),
        "cmc": values.pop("cmc", 0),
        "power": values.pop("power", 0),
        "toughness": values.pop("toughness", 0),
        "color_identity": values.pop("color_identity", []),
    }
    data.update(values)
    return Card(data)


def state(cards, p1_battlefield=(), p2_battlefield=()):
    game_state = GameState(cards)
    ids = list(cards) or ["filler"]
    game_state.p1 = game_state._init_player(ids, 1)
    game_state.p2 = game_state._init_player(ids, 2)
    for player in (game_state.p1, game_state.p2):
        player["library"] = []
        player["hand"] = []
        player["battlefield"] = []
        player["graveyard"] = []
        player["exile"] = []
        player["tapped_permanents"] = set()
    game_state.p1["battlefield"] = list(p1_battlefield)
    game_state.p2["battlefield"] = list(p2_battlefield)
    game_state.stack = []
    game_state.phased_out = set()
    game_state._last_card_locations = {
        target_id: (player, "battlefield")
        for player in (game_state.p1, game_state.p2)
        for target_id in player["battlefield"]
    }
    game_state.layer_system.invalidate_cache()
    game_state.layer_system.apply_all_effects()
    return game_state


def flattened(valid_map):
    return {
        target_id
        for target_ids in valid_map.values()
        for target_id in target_ids
    }


class TargetingAuditRegressionTest(unittest.TestCase):
    def test_numeric_zero_target_and_modal_effect_text(self):
        cards = {
            0: card("Zero Bear", "Creature - Bear", power=2, toughness=2),
            1: card(
                "Modal Source", "Instant",
                "Destroy target creature. Destroy target permanent."),
            2: card("Relic", "Artifact"),
        }
        game_state = state(cards, p2_battlefield=(0, 2))

        creature_targets = game_state.targeting_system.get_valid_targets(
            1, game_state.p1, "creature",
            effect_text="Destroy target creature.")
        self.assertEqual(flattened(creature_targets), {0})
        self.assertEqual(
            game_state.targeting_system.resolve_targeting(
                1, game_state.p1, "Destroy target creature."),
            {"creatures": [0]})

        selected = game_state.targeting_system.resolve_targeting_for_spell(
            1, game_state.p1, "Destroy target artifact.")
        self.assertEqual(selected, {"artifacts": [2]})

    def test_player_restrictions_use_seat_identity(self):
        cards = {1: card("Coercion", "Sorcery", "Target opponent discards.")}
        game_state = state(cards)
        game_state.p2 = copy.deepcopy(game_state.p1)
        self.assertEqual(game_state.p1, game_state.p2)
        self.assertIsNot(game_state.p1, game_state.p2)

        valid = game_state.targeting_system.get_valid_targets(
            1, game_state.p1, "player",
            effect_text="Target opponent discards a card.")
        self.assertEqual(valid, {"player": ["p2"]})
        self.assertEqual(
            game_state.targeting_system.resolve_targeting(
                1, game_state.p1, "Target opponent discards a card."),
            {"players": ["p2"]})

        # Equal-value player dictionaries still represent different seats.
        game_state.p1["keywords"] = {"hexproof"}
        game_state.p2["keywords"] = {"hexproof"}
        self.assertEqual(game_state.p1, game_state.p2)
        self.assertEqual(game_state.targeting_system.get_valid_targets(
            1, game_state.p1, "player",
            effect_text="Target opponent discards a card."), {})

    def test_common_adjectives_and_layered_types(self):
        cards = {
            1: card("Selector", "Instant", "Target creature."),
            2: card(
                "Artifact Bear", "Artifact Creature - Bear",
                power=2, toughness=2),
            3: card(
                "Goblin", "Creature - Goblin", power=2, toughness=2),
            4: card(
                "Legend", "Legendary Creature - Human",
                color_identity=["W", "U"], power=3, toughness=3),
            5: card("Token", "Creature - Soldier", power=1, toughness=1),
            6: card("Animated Relic", "Artifact"),
        }
        cards[5].is_token = True
        game_state = state(cards, p2_battlefield=(2, 3, 4, 5, 6))
        game_state.layer_system.register_effect({
            "source_id": 6, "layer": 4, "affected_ids": [6],
            "effect_type": "add_type", "effect_value": "creature",
            "duration": "permanent",
        })
        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()

        def targets(text):
            return flattened(game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "creature", effect_text=text))

        self.assertEqual(targets("Destroy target Goblin creature."), {3})
        self.assertNotIn(2, targets("Destroy target nonartifact creature."))
        self.assertNotIn(5, targets("Exile target nontoken creature."))
        self.assertEqual(targets("Tap target multicolored creature."), {4})
        self.assertEqual(targets("Tap target legendary creature."), {4})
        self.assertIn(6, targets("Tap target creature."))
        self.assertEqual(flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p2, "goblin",
                effect_text="Target Goblin you control gains haste.")), {3})

    def test_stack_spell_restrictions_and_uncounterable_target(self):
        cards = {
            1: card("Counter", "Instant", "Counter target spell."),
            2: card(
                "Uncounterable Lesson", "Instant",
                "This spell can't be countered.", cmc=2),
            3: card("Two Drop", "Creature", cmc=2, power=2, toughness=2),
            4: card("Expensive Sorcery", "Sorcery", cmc=3),
        }
        game_state = state(cards)
        game_state.stack = [
            ("SPELL", 2, game_state.p2, {}),
            ("SPELL", 3, game_state.p2, {}),
            ("SPELL", 4, game_state.p2, {}),
        ]

        noncreature = flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "spell",
                effect_text="Counter target noncreature spell."))
        self.assertEqual(noncreature, {2, 4})
        mana_value_two = flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "spell",
                effect_text="Counter target spell with mana value 2."))
        self.assertEqual(mana_value_two, {2, 3})
        self.assertIn(2, mana_value_two)
        instant_or_sorcery = flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "spell",
                effect_text="Copy target instant or sorcery spell."))
        self.assertEqual(instant_or_sorcery, {2, 4})

    def test_typed_card_targets_keep_zone_owner_and_card_type(self):
        cards = {
            1: card("Helping Hand", "Sorcery"),
            2: card("Small Creature", "Creature", cmc=2),
            3: card("Large Creature", "Creature", cmc=4),
            4: card("Graveyard Relic", "Artifact", cmc=1),
            5: card("Graveyard Lesson", "Instant", cmc=1),
            6: card("Opponent Creature", "Creature", cmc=2),
            7: card("Battlefield Creature", "Creature", cmc=2),
            8: card("Owned Exile Relic", "Artifact", cmc=2),
            9: card("Opponent Exile Relic", "Artifact", cmc=2),
            10: card("Battlefield Relic", "Artifact", cmc=2),
            11: card("Graveyard Land", "Land"),
            12: card("Battlefield Land", "Land"),
        }
        game_state = state(cards, p2_battlefield=(7, 10, 12))
        game_state.p1["graveyard"] = [2, 3, 4, 5, 11]
        game_state.p2["graveyard"] = [6]
        game_state.p1["exile"] = [8]
        game_state.p2["exile"] = [9]

        helping_hand = (
            "Return target creature card with mana value 2 or less from "
            "your graveyard to the battlefield tapped.")
        self.assertEqual(flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "creature",
                effect_text=helping_hand)), {2})
        self.assertEqual(flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "permanent",
                effect_text=(
                    "Return target permanent card from your graveyard to "
                    "your hand."))), {2, 3, 4, 11})
        self.assertEqual(flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "artifact",
                effect_text=(
                    "Put target artifact card you own in exile into your "
                    "hand."))), {8})
        self.assertEqual(flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "creature",
                effect_text=(
                    "Return up to two target creature cards from your "
                    "graveyard to your hand."))), {2, 3})
        self.assertEqual(flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "land",
                effect_text=(
                    "Return target land card from your graveyard to your "
                    "hand."))), {11})
        self.assertEqual(flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "permanent",
                effect_text=(
                    "Return target nonland permanent card from your "
                    "graveyard to your hand."))), {2, 3, 4})
        self.assertEqual(flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "permanent",
                effect_text=(
                    "Return target artifact or creature card from your "
                    "graveyard to your hand."))), {2, 3, 4})

    def test_nonland_permanent_keeps_trailing_mana_value_restriction(self):
        cards = {
            1: card("Removal", "Sorcery"),
            2: card("Cheap Relic", "Artifact", cmc=2),
            3: card("Expensive Relic", "Artifact", cmc=4),
            4: card("Land", "Land", cmc=0),
            5: card("Friendly Relic", "Artifact", cmc=1),
        }
        game_state = state(
            cards, p1_battlefield=(5,), p2_battlefield=(2, 3, 4))
        targets = flattened(game_state.targeting_system.get_valid_targets(
            1, game_state.p1, "permanent",
            effect_text=(
                "Destroy target nonland permanent an opponent controls with "
                "mana value 2 or less.")))
        self.assertEqual(targets, {2})

    def test_battle_is_a_permanent_and_any_target_but_phasing_hides_it(self):
        cards = {
            1: card("Damage", "Instant", "Damage any target."),
            2: card("Siege", "Battle - Siege"),
            3: card("Relic", "Artifact"),
        }
        game_state = state(cards, p2_battlefield=(2, 3))
        any_targets = flattened(game_state.targeting_system.get_valid_targets(
            1, game_state.p1, "any", effect_text="Deal 1 damage to any target."))
        self.assertIn(2, any_targets)
        self.assertNotIn(3, any_targets)
        permanent_targets = flattened(
            game_state.targeting_system.get_valid_targets(
                1, game_state.p1, "permanent",
                effect_text="Return target permanent."))
        self.assertEqual(permanent_targets, {2, 3})
        self.assertTrue(game_state.targeting_system.validate_targets(
            1, {"battles": [2]}, game_state.p1,
            effect_text="Return target permanent."))

        game_state.phased_out.add(2)
        phased_targets = flattened(game_state.targeting_system.get_valid_targets(
            1, game_state.p1, "permanent",
            effect_text="Return target permanent."))
        self.assertNotIn(2, phased_targets)

    def test_protection_hexproof_shroud_and_ward_direction(self):
        cards = {
            0: card(
                "Protected Zero", "Creature", "Protection from red",
                power=2, toughness=2),
            1: card(
                "Red Source", "Instant", "Deal 1 damage to target creature.",
                color_identity=["R"]),
            2: card("Hexproof Foe", "Creature", "Hexproof", power=2, toughness=2),
            3: card("Shrouded Foe", "Creature", "Shroud", power=2, toughness=2),
            4: card("Warded Foe", "Creature", "Ward {2}", power=2, toughness=2),
            5: card("Friendly Hexproof", "Creature", "Hexproof", power=2, toughness=2),
        }
        game_state = state(cards, p1_battlefield=(5,), p2_battlefield=(0, 2, 3, 4))
        for target_id in (0, 2, 3, 4, 5):
            game_state.ability_handler._parse_and_register_abilities(
                target_id, game_state._safe_get_card(target_id))
        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()

        before_stack = list(game_state.stack)
        before_targeted = set(game_state.p1["targeted_permanents_this_turn"])
        targets = flattened(game_state.targeting_system.get_valid_targets(
            1, game_state.p1, "creature",
            effect_text="Deal 1 damage to target creature."))
        self.assertNotIn(0, targets)
        self.assertNotIn(2, targets)
        self.assertNotIn(3, targets)
        self.assertIn(4, targets)
        self.assertIn(5, targets)
        self.assertEqual(game_state.stack, before_stack)
        self.assertEqual(
            game_state.p1["targeted_permanents_this_turn"], before_targeted)

    def test_block_and_cast_probes_fail_closed(self):
        cards = {
            1: card("Flyer", "Creature", "Flying", power=2, toughness=2),
            2: card("Ground Blocker", "Creature", "", power=2, toughness=2),
            3: card("Relic", "Artifact"),
            4: card("Removal", "Instant", "Destroy target creature."),
        }
        game_state = state(cards, p1_battlefield=(1,), p2_battlefield=(2, 3))
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        self.assertFalse(game_state.targeting_system.check_can_be_blocked(1, 2))
        self.assertFalse(game_state.targeting_system.check_can_be_blocked(1, 3))
        game_state.phased_out.add(2)
        self.assertFalse(game_state.targeting_system.check_can_be_blocked(1, 2))
        target_text = "Destroy target creature an opponent controls."
        self.assertFalse(handler._targets_available_from_text(
            target_text, game_state.p1, game_state.p2, source_id=4))
        self.assertFalse(handler._targets_available_from_data(
            {"oracle_text": target_text, "type_line": "Instant"},
            game_state.p1, game_state.p2,
            source_id=4))
        self.assertFalse(handler._targets_available_from_text(
            target_text, game_state.p1, game_state.p2))
        self.assertTrue(handler._targets_available_from_text(
            "Tap up to one target creature an opponent controls.",
            game_state.p1, game_state.p2, source_id=4))

        with patch.object(
                game_state.targeting_system, "check_can_be_blocked",
                side_effect=RuntimeError("probe")):
            self.assertFalse(handler._can_block(2, 1))
        with patch.object(
                game_state.targeting_system, "get_valid_targets",
                side_effect=RuntimeError("probe")):
            self.assertFalse(handler._targets_available(
                cards[4], game_state.p1, game_state.p2))
            self.assertFalse(handler._targets_available_from_text(
                target_text, game_state.p1, game_state.p2, source_id=4))

    def test_unknown_target_grammar_fails_closed(self):
        cards = {
            1: card("Unknown", "Instant", "Exile target contraption."),
            2: card("Relic", "Artifact"),
        }
        game_state = state(cards, p2_battlefield=(2,))
        self.assertEqual(game_state.targeting_system.get_valid_targets(
            1, game_state.p1,
            effect_text="Exile target contraption."), {})

    def test_target_observation_uses_the_active_instruction_context(self):
        from Playersim.environment import AlphaZeroMTGEnv

        cards = {
            0: card("Bear", "Creature - Bear", power=2, toughness=2),
            1: card(
                "Modal Source", "Instant",
                "Destroy target creature. Destroy target artifact."),
            2: card("Relic", "Artifact"),
        }
        game_state = state(cards, p2_battlefield=(0, 2))
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_TARGETING
        game_state.targeting_context = {
            "source_id": 1,
            "controller": game_state.p1,
            "required_type": "creature",
            "effect_text": "Destroy target creature.",
            "required_count": 1,
            "max_targets": 1,
            "selected_targets": [],
        }
        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        env.game_state = game_state
        env.action_handler = handler
        env.observation_space = {
            "targetable_permanents": SimpleNamespace(shape=(4,)),
            "targetable_spells_on_stack": SimpleNamespace(shape=(5,)),
            "targetable_cards_in_graveyards": SimpleNamespace(shape=(20,)),
        }

        candidates = handler._get_target_selection_candidates(
            game_state.p1, game_state.targeting_context)
        observed = env._get_potential_targets_vector("permanent")
        self.assertEqual(candidates, [0])
        self.assertEqual(observed.tolist(), [0, -1, -1, -1])


if __name__ == "__main__":
    unittest.main()
