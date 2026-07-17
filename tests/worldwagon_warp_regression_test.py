import unittest

from Playersim.ability_utils import EffectFactory
from tests.scenario_test import (
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)


def creature(name, power):
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": "Creature - Test",
        "oracle_text": "",
        "power": power,
        "toughness": power,
    }


class WorldwagonCrewRegressionTest(unittest.TestCase):
    def test_real_card_requires_four_power_for_crew(self):
        game_state = fresh(99401)
        player = game_state.p1
        game_state.agent_is_p1 = True
        one_power = inject_into_zone(
            game_state, player, creature("One Power Crew", 1), "battlefield")
        three_power = inject_into_zone(
            game_state, player, creature("Three Power Crew", 3), "battlefield")
        vehicle = inject_real_card(
            game_state, player, "Lumbering Worldwagon", "battlefield")

        abilities = game_state.ability_handler.get_activated_abilities(vehicle)
        crew_index = next(
            index for index, ability in enumerate(abilities)
            if getattr(ability, "keyword", "") == "crew")
        crew = abilities[crew_index]
        self.assertEqual(crew.crew_power, 4)
        parsed_effect = EffectFactory.create_effects(
            crew.effect, source_name="Lumbering Worldwagon")[0]
        self.assertEqual(parsed_effect.power, 4)

        player.setdefault("tapped_permanents", set()).add(three_power)
        self.assertFalse(game_state.ability_handler.can_activate_ability(
            vehicle, crew_index, player))
        player["tapped_permanents"].remove(three_power)
        self.assertTrue(game_state.ability_handler.can_activate_ability(
            vehicle, crew_index, player))

        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = player
        handler = get_env().action_handler
        _, activated = handler._handle_activate_ability(None, {
            "battlefield_idx": player["battlefield"].index(vehicle),
            "ability_idx": crew_index,
            "controller_id": "p1",
        })
        self.assertTrue(activated)
        self.assertEqual(game_state.choice_context["required_power"], 4)

        one_index = game_state.choice_context["options"].index(one_power)
        self.assertTrue(handler._handle_choose_mode(one_index, {})[1])
        self.assertFalse(handler._handle_pass_priority(None)[1])
        three_index = game_state.choice_context["options"].index(three_power)
        self.assertTrue(handler._handle_choose_mode(three_index, {})[1])
        self.assertTrue(handler._handle_pass_priority(None)[1])
        self.assertIn(one_power, player["tapped_permanents"])
        self.assertIn(three_power, player["tapped_permanents"])
        self.assertTrue(game_state.stack[-1][3].get("crew_cost_paid"))


class WarpOverflowRegressionTest(unittest.TestCase):
    def test_warp_is_catalogued_beyond_fixed_hand_slots(self):
        for target_index in (8, 10):
            with self.subTest(hand_index=target_index):
                game_state = fresh(99410 + target_index)
                player = game_state._get_active_player()
                game_state.agent_is_p1 = player is game_state.p1
                player["hand"] = []
                for index in range(target_index):
                    inject_into_zone(
                        game_state, player,
                        {
                            **creature(f"Warp Filler {target_index}-{index}", 1),
                            "mana_cost": "{9}", "cmc": 9,
                        },
                        "hand")
                warp_card = inject_real_card(
                    game_state, player, "Mightform Harmonizer", "hand")
                self.assertEqual(player["hand"].index(warp_card), target_index)

                player["mana_pool"] = {
                    "W": 0, "U": 0, "B": 0, "R": 0, "G": 1, "C": 2,
                }
                game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
                game_state.priority_player = player
                game_state.priority_pass_count = 0
                handler = get_env().action_handler

                mask = handler.generate_valid_actions()
                self.assertTrue(mask[479], "Warp overflow catalog was absent")
                open_context = handler.action_reasons_with_context[479]["context"]
                warp_entries = [
                    entry for entry in open_context.get("options", [])
                    if entry.get("handler") == "warp_cast"
                    and entry.get("action_context", {}).get("card_id") == warp_card
                ]
                self.assertEqual(len(warp_entries), 1)
                self.assertEqual(
                    warp_entries[0]["action_context"]["hand_idx"], target_index)

                handler.current_valid_actions = mask
                _, _, _, info = handler.apply_action(479)
                self.assertFalse(info.get("execution_failed", False), info)
                catalog = game_state.choice_context
                self.assertEqual(catalog.get("type"), "action_catalog")
                option_index = next(
                    index for index, entry in enumerate(catalog["options"])
                    if entry.get("handler") == "warp_cast"
                    and entry.get("action_context", {}).get("card_id") == warp_card)
                self.assertLess(option_index, 10)
                self.assertTrue(handler._handle_choose_mode(option_index, {})[1])
                self.assertNotIn(warp_card, player["hand"])
                self.assertEqual(game_state.stack[-1][1], warp_card)
                self.assertTrue(game_state.stack[-1][3].get("warp_cast"))


if __name__ == "__main__":
    unittest.main()
