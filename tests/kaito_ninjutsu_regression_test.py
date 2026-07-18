from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)


class KaitoNinjutsuRegressionTest(unittest.TestCase):
    @staticmethod
    def _clear_hand(game_state, player):
        for card_id in list(player["hand"]):
            assert game_state.move_card(
                card_id, player, "hand", player, "library")

    def test_battlefield_kaito_has_no_generic_ninjutsu_activation(self):
        game_state = fresh(271801)
        controller = game_state.p1
        game_state.turn = 1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        game_state.stack.clear()

        kaito = inject_real_card(
            game_state, controller, "Kaito, Bane of Nightmares",
            "battlefield")
        battlefield_index = controller["battlefield"].index(kaito)
        handler = get_env().action_handler

        mask = handler.generate_valid_actions()
        exposed = []
        for action, allowed in enumerate(mask):
            if not allowed:
                continue
            action_type, _ = handler.get_action_info(action)
            metadata = handler.action_reasons_with_context.get(action, {})
            context = metadata.get("context", {}) or {}
            if (action_type == "ACTIVATE_ABILITY"
                    and context.get("battlefield_idx") == battlefield_index):
                exposed.append((action, context.get("ability_idx")))

        self.assertEqual(
            exposed, [],
            "Kaito exposed Ninjutsu as a generic battlefield activation "
            f"outside combat: {exposed}")

    def test_real_kaito_ninjutsu_public_combat_path(self):
        game_state = fresh(271802)
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = 1
        game_state.agent_is_p1 = True
        game_state.stack.clear()
        game_state.priority_pass_count = 0
        self._clear_hand(game_state, controller)

        attacker = inject_into_zone(game_state, controller, {
            "name": "Kaito Return Probe",
            "mana_cost": "{1}{U}",
            "type_line": "Creature - Ninja",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
        }, "battlefield")
        controller["entered_battlefield_this_turn"].discard(attacker)
        kaito = inject_real_card(
            game_state, controller, "Kaito, Bane of Nightmares", "hand")
        handler = get_env().action_handler

        controller["mana_pool"] = {
            "W": 0, "U": 1, "B": 1, "R": 0, "G": 0, "C": 1,
        }
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {}
        game_state.blocked_attackers_this_combat = set()

        # Ninjutsu is unavailable before the post-blockers combat window.
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        self.assertFalse(
            handler.generate_valid_actions()[437],
            "Kaito exposed Ninjutsu outside its combat timing window")

        # It is also unavailable to the attacking policy while the opponent
        # owns priority in the otherwise-correct combat window.
        game_state.phase = game_state.PHASE_COMBAT_DAMAGE
        game_state.priority_player = opponent
        self.assertFalse(
            handler.generate_valid_actions()[437],
            "Kaito exposed Ninjutsu without its controller having priority")

        game_state.priority_player = controller
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[437], "real Kaito's legal Ninjutsu was absent")
        context = handler.action_reasons_with_context[437]["context"]
        self.assertEqual(controller["hand"][context["ninja_identifier"]], kaito)
        self.assertEqual(
            controller["battlefield"][context["attacker_identifier"]],
            attacker)

        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(437)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertFalse(info.get("critical_error", False), info)

        # Returning the attacker and paying {1}{U}{B} are activation costs.
        # Kaito itself stays in hand while the Ninjutsu ability is on the
        # stack so that both players receive a response window.
        self.assertIn(attacker, controller["hand"])
        self.assertIn(kaito, controller["hand"])
        self.assertNotIn(kaito, controller["battlefield"])
        self.assertEqual(
            controller["mana_pool"],
            {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0})
        self.assertEqual(game_state.current_attackers, [])
        self.assertNotIn(kaito, controller["tapped_permanents"])
        self.assertTrue(game_state.stack, "Ninjutsu skipped the stack")
        item_type, source_id, stack_controller, _ = game_state.stack[-1]
        self.assertEqual(item_type, "ABILITY")
        self.assertEqual(source_id, kaito)
        self.assertIs(stack_controller, controller)

        # The activator passes first. Kaito must still be in hand while the
        # opponent has priority and may respond to the Ninjutsu ability.
        game_state.agent_is_p1 = True
        first_pass_mask = handler.generate_valid_actions()
        self.assertTrue(first_pass_mask[11])
        handler.current_valid_actions = first_pass_mask
        _, done, truncated, info = handler.apply_action(11)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertIs(game_state.priority_player, opponent)
        self.assertIn(kaito, controller["hand"])
        self.assertTrue(game_state.stack)

        # Only the second pass resolves the ability and performs the
        # tapped-and-attacking placement.
        game_state.agent_is_p1 = False
        second_pass_mask = handler.generate_valid_actions()
        self.assertTrue(second_pass_mask[11])
        handler.current_valid_actions = second_pass_mask
        _, done, truncated, info = handler.apply_action(11)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)

        self.assertNotIn(kaito, controller["hand"])
        self.assertIn(kaito, controller["battlefield"])
        self.assertIn(kaito, controller["tapped_permanents"])
        self.assertEqual(game_state.current_attackers, [kaito])
        self.assertFalse(game_state.stack)
        self.assertEqual(controller["loyalty_counters"].get(kaito), 4)
        live_kaito = game_state._safe_get_card(kaito)
        self.assertEqual((live_kaito.power, live_kaito.toughness), (3, 4))

    def test_ninjutsu_does_not_follow_kaito_to_a_new_hand_incarnation(self):
        game_state = fresh(271804)
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = 1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_COMBAT_DAMAGE
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack.clear()
        self._clear_hand(game_state, controller)

        attacker = inject_into_zone(game_state, controller, {
            "name": "Kaito Identity Return Probe",
            "mana_cost": "{1}{U}",
            "type_line": "Creature - Ninja",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
        }, "battlefield")
        controller["entered_battlefield_this_turn"].discard(attacker)
        kaito = inject_real_card(
            game_state, controller, "Kaito, Bane of Nightmares", "hand")
        controller["mana_pool"] = {
            "W": 0, "U": 1, "B": 1, "R": 0, "G": 0, "C": 1,
        }
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {}
        game_state.blocked_attackers_this_combat = set()
        handler = get_env().action_handler

        activation_mask = handler.generate_valid_actions()
        self.assertTrue(activation_mask[437])
        handler.current_valid_actions = activation_mask
        _, done, truncated, info = handler.apply_action(437)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertIn(kaito, controller["hand"])
        self.assertTrue(game_state.stack)

        live_kaito = game_state._safe_get_card(kaito)
        activation_generation = live_kaito._zone_change_generation
        self.assertTrue(game_state.move_card(
            kaito, controller, "hand", controller, "graveyard",
            cause="response_discard"))
        self.assertTrue(game_state.move_card(
            kaito, controller, "graveyard", controller, "hand",
            cause="response_return"))
        self.assertGreater(
            live_kaito._zone_change_generation, activation_generation)

        game_state.agent_is_p1 = controller is game_state.p1
        first_pass_mask = handler.generate_valid_actions()
        self.assertTrue(first_pass_mask[11])
        handler.current_valid_actions = first_pass_mask
        _, done, truncated, info = handler.apply_action(11)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertIs(game_state.priority_player, opponent)
        self.assertIn(kaito, controller["hand"])
        self.assertTrue(game_state.stack)

        game_state.agent_is_p1 = opponent is game_state.p1
        second_pass_mask = handler.generate_valid_actions()
        self.assertTrue(second_pass_mask[11])
        handler.current_valid_actions = second_pass_mask
        _, done, truncated, info = handler.apply_action(11)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)

        self.assertIn(
            kaito, controller["hand"],
            "the old Ninjutsu ability followed Kaito's new hand incarnation")
        self.assertNotIn(kaito, controller["battlefield"])
        self.assertNotIn(kaito, controller["tapped_permanents"])
        self.assertNotIn(kaito, game_state.current_attackers)
        self.assertNotIn(kaito, controller["loyalty_counters"])
        self.assertFalse(game_state.stack)


if __name__ == "__main__":
    unittest.main()
