"""Fail-closed regressions for formerly trusted spell mechanics.

These tests intentionally use real Standard card records and route every
casting, mode, target, and priority decision through ActionHandler's public
mask/dispatch boundary.  Mana starts in lands, not in a pre-filled pool.

Run from the repository root with::

    .\\MTGenv\\Scripts\\python.exe tests\\trusted_spell_mechanics_regression_test.py
"""

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
    inject_card,
    inject_into_zone,
    inject_real_card,
)


class TrustedSpellMechanicsRegressionTest(unittest.TestCase):
    def _state(self, seed: int):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.sacrifice_context = None
        game_state.pending_spell_context = None
        game_state.delayed_triggers = []
        game_state.ability_handler.active_triggers.clear()
        for player in (controller, opponent):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
            player["damage_counters"] = {}
            player["deathtouch_damage"] = {}
            player["mana_pool"] = {
                "W": 0, "U": 0, "B": 0,
                "R": 0, "G": 0, "C": 0,
            }
        game_state._last_card_locations = {}
        handler = get_env().action_handler
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    def _mask(self, handler):
        game_state = handler.game_state
        priority = game_state.priority_player or game_state.p1
        game_state.priority_player = priority
        game_state.agent_is_p1 = priority is game_state.p1
        return handler.generate_valid_actions()

    def _public(self, handler, action: int, message: str):
        mask = self._mask(handler)
        self.assertTrue(
            mask[action],
            f"{message}: action {action} absent; valid="
            f"{[index for index, allowed in enumerate(mask) if allowed]}",
        )
        handler.current_valid_actions = mask
        reward, done, truncated, info = handler.apply_action(action)
        self.assertFalse(info.get("execution_failed"), (message, reward, info))
        self.assertFalse(info.get("critical_error"), (message, reward, info))
        self.assertFalse(info.get("invalid_action"), (message, reward, info))
        return reward, done, truncated, info

    def _add_real_lands(self, game_state, controller, name: str, count: int):
        return [
            inject_real_card(game_state, controller, name, "battlefield")
            for _ in range(count)
        ]

    def _cast_from_first_hand_slot(self, handler):
        self._public(handler, 20, "cast real spell from the first hand slot")

    def _select_target(self, handler, controller, target_id):
        game_state = handler.game_state
        self.assertIsNotNone(game_state.targeting_context)
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(target_id, candidates)
        absolute_index = candidates.index(target_id)
        for _ in range(absolute_index // 10):
            self._public(handler, 479, "page target candidates")
        self._public(
            handler, 274 + absolute_index % 10,
            f"select target {target_id}")

    def _resolve_top_with_priority(self, handler):
        game_state = handler.game_state
        self.assertTrue(game_state.stack, "spell never reached the stack")
        self._public(handler, 11, "controller passes priority")
        self._public(handler, 11, "opponent passes priority")

    @staticmethod
    def _durable_creature(name: str):
        return {
            "name": name,
            "mana_cost": "{2}",
            "cmc": 2,
            "type_line": "Creature - Test",
            "oracle_text": "",
            "power": 2,
            "toughness": 20,
        }

    def test_real_kicker_is_a_public_choice_and_changes_payment_and_outcome(self):
        cases = (
            (31001, "Burst Lightning", "Mountain", 5, 4),
            (31002, "Firebending Lesson", "Mountain", 5, 5),
            (31003, "Consult the Star Charts", "Island", 4, None),
        )
        for seed, card_name, land_name, land_count, kicked_damage in cases:
            with self.subTest(card=card_name):
                game_state, handler, controller, opponent = self._state(seed)
                spell_id = inject_real_card(
                    game_state, controller, card_name, "hand")
                lands = self._add_real_lands(
                    game_state, controller, land_name, land_count)
                target_id = None
                if kicked_damage is not None:
                    target_id = inject_into_zone(
                        game_state, opponent,
                        self._durable_creature(f"{card_name} target"),
                        "battlefield")
                else:
                    # Consult looks at one card per controlled land.  Give it
                    # four known cards so the kicked branch must offer two
                    # independent public selections.
                    known_top = []
                    for index in range(4):
                        card_id = inject_card(game_state, {
                            "name": f"Consult known card {index}",
                            "mana_cost": "{1}",
                            "type_line": "Sorcery",
                            "oracle_text": "",
                        })
                        controller["library"].append(card_id)
                        game_state._last_card_locations[card_id] = (
                            controller, "library")
                        known_top.append(card_id)

                self._cast_from_first_hand_slot(handler)
                self.assertEqual(
                    (game_state.phase,
                     (game_state.choice_context or {}).get("type")),
                    (game_state.PHASE_CHOOSE, "pay_kicker"),
                    f"{card_name} did not announce Kicker before targets/payment",
                )
                kicker_mask = self._mask(handler)
                self.assertTrue(kicker_mask[405], "affordable Kicker was hidden")
                self.assertTrue(kicker_mask[406], "decline-Kicker branch was hidden")
                self._public(handler, 405, f"pay {card_name} Kicker")

                if target_id is not None:
                    self._select_target(handler, controller, target_id)

                self.assertTrue(game_state.stack)
                stack_context = game_state.stack[-1][3]
                paid = stack_context.get("final_paid_cost", {})
                expected_paid = (
                    (4, 1) if land_name == "Mountain" else (2, 2))
                color_symbol = {
                    "Mountain": "R", "Island": "U",
                }[land_name]
                self.assertEqual(
                    (paid.get("generic", 0), paid.get(color_symbol, 0)),
                    expected_paid,
                    f"{card_name} did not add its Kicker cost exactly once",
                )
                self.assertEqual(
                    len(set(lands).intersection(controller["tapped_permanents"])),
                    land_count,
                    f"{card_name} did not pay with the staged real lands",
                )

                self._resolve_top_with_priority(handler)
                if target_id is not None:
                    self.assertEqual(
                        opponent["damage_counters"].get(target_id),
                        kicked_damage,
                        f"{card_name} did not replace its base damage when kicked",
                    )
                else:
                    self.assertEqual(
                        (game_state.choice_context or {}).get("type"),
                        "dig_select")
                    self.assertEqual(
                        game_state.choice_context.get("remaining"), 2,
                        "kicked Consult did not take two cards")
                    self._public(handler, 353, "choose first Consult card")
                    self._public(handler, 353, "choose second Consult card")
                    self.assertTrue(
                        all(card_id in controller["hand"]
                            for card_id in known_top[:2]),
                        "kicked Consult did not retain both public choices",
                    )
                self.assertNotIn(spell_id, controller["hand"])

    def test_declined_or_unaffordable_kicker_deals_only_base_damage(self):
        cases = (
            (31011, "Burst Lightning", 2),
            (31012, "Firebending Lesson", 2),
        )
        for seed, card_name, expected_damage in cases:
            with self.subTest(card=card_name):
                game_state, handler, controller, opponent = self._state(seed)
                inject_real_card(game_state, controller, card_name, "hand")
                land_id = self._add_real_lands(
                    game_state, controller, "Mountain", 1)[0]
                target_id = inject_into_zone(
                    game_state, opponent,
                    self._durable_creature(f"unkicked {card_name} target"),
                    "battlefield")

                self._cast_from_first_hand_slot(handler)
                # Once the announcement bug is fixed, this branch proves the
                # unaffordable positive choice is hidden.  Before that fix the
                # cast skips directly to targeting, allowing the independent
                # resolution assertion below to expose the 2+N damage bug.
                if ((game_state.choice_context or {}).get("type")
                        == "pay_kicker"):
                    kicker_mask = self._mask(handler)
                    self.assertFalse(
                        kicker_mask[405],
                        "Kicker was offered without enough real mana")
                    self.assertTrue(kicker_mask[406])
                    self._public(handler, 406, f"decline {card_name} Kicker")

                self._select_target(handler, controller, target_id)
                paid = game_state.stack[-1][3].get("final_paid_cost", {})
                self._resolve_top_with_priority(handler)
                self.assertEqual(
                    (paid.get("generic", 0), paid.get("R", 0),
                     opponent["damage_counters"].get(target_id),
                     land_id in controller["tapped_permanents"]),
                    (0, 1, expected_damage, True),
                    f"{card_name} stacked its conditional damage instead of replacing it",
                )

    def test_parting_gust_publicly_offers_and_honors_its_gift(self):
        game_state, handler, controller, opponent = self._state(31021)
        gust_id = inject_real_card(
            game_state, controller, "Parting Gust", "hand")
        lands = self._add_real_lands(
            game_state, controller, "Plains", 2)
        target_id = inject_into_zone(
            game_state, opponent,
            self._durable_creature("Parting Gust nontoken target"),
            "battlefield")
        token_id = inject_into_zone(game_state, opponent, {
            **self._durable_creature("Parting Gust token decoy"),
        }, "battlefield")
        token = game_state._safe_get_card(token_id)
        token.is_token = True
        opponent.setdefault("tokens", []).append(token_id)

        self._cast_from_first_hand_slot(handler)
        self.assertEqual(
            (game_state.phase,
             (game_state.choice_context or {}).get("type")),
            (game_state.PHASE_CHOOSE, "gift"),
            "Parting Gust skipped its Gift announcement",
        )
        gift_mask = self._mask(handler)
        self.assertTrue(gift_mask[353], "promise-Gift branch was hidden")
        self.assertTrue(gift_mask[11], "decline-Gift branch was hidden")
        self._public(handler, 353, "promise Parting Gust's Gift")

        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(target_id, candidates)
        self.assertNotIn(token_id, candidates)
        self._select_target(handler, controller, target_id)
        stack_context = game_state.stack[-1][3]
        self.assertTrue(stack_context.get("gift_promised"))
        self.assertEqual(
            stack_context.get("final_paid_cost", {}).get("W"), 2)
        self.assertEqual(
            len(set(lands).intersection(controller["tapped_permanents"])), 2)

        self._resolve_top_with_priority(handler)
        self.assertIn(target_id, opponent["exile"])
        self.assertNotIn(target_id, opponent["battlefield"])
        self.assertFalse(
            any(trigger.get("source_id") == gust_id
                for trigger in game_state.delayed_triggers),
            "promised Parting Gust incorrectly scheduled the ungifted return",
        )
        fish = [
            card_id for card_id in opponent["battlefield"]
            if getattr(game_state._safe_get_card(card_id), "name", "")
            == "Fish Token"]
        self.assertEqual(len(fish), 1)
        self.assertIn(fish[0], opponent["tapped_permanents"])

    def test_thunder_magic_tiers_add_cost_once_and_deal_exact_damage(self):
        cases = (
            (31031, 0, 1, 0, 1, 2),
            (31032, 1, 4, 3, 1, 4),
            (31033, 2, 7, 5, 2, 8),
        )
        for seed, tier, land_count, generic, red, damage in cases:
            with self.subTest(tier=tier):
                game_state, handler, controller, opponent = self._state(seed)
                inject_real_card(
                    game_state, controller, "Thunder Magic", "hand")
                lands = self._add_real_lands(
                    game_state, controller, "Mountain", land_count)
                target_id = inject_into_zone(
                    game_state, opponent,
                    self._durable_creature(f"Thunder tier {tier} target"),
                    "battlefield")

                self._cast_from_first_hand_slot(handler)
                self.assertEqual(
                    (game_state.choice_context or {}).get("type"),
                    "choose_mode")
                self._public(handler, 353 + tier, f"choose Thunder tier {tier}")
                self._select_target(handler, controller, target_id)
                paid = game_state.stack[-1][3].get("final_paid_cost", {})
                self._resolve_top_with_priority(handler)
                self.assertEqual(
                    (paid.get("generic", 0), paid.get("R", 0),
                     opponent["damage_counters"].get(target_id),
                     len(set(lands).intersection(
                         controller["tapped_permanents"]))),
                    (generic, red, damage, land_count),
                    f"Thunder tier {tier} ignored its additional cost or damage",
                )

    def test_thunder_magic_hides_unaffordable_tiers(self):
        game_state, handler, controller, opponent = self._state(31041)
        inject_real_card(game_state, controller, "Thunder Magic", "hand")
        self._add_real_lands(game_state, controller, "Mountain", 1)
        inject_into_zone(
            game_state, opponent,
            self._durable_creature("Thunder affordability target"),
            "battlefield")

        self._cast_from_first_hand_slot(handler)
        tier_mask = self._mask(handler)
        self.assertEqual(
            [bool(tier_mask[353 + index]) for index in range(3)],
            [True, False, False],
            "Thunder exposed tiers whose additional costs could not be paid",
        )

    def test_thunder_tier_rows_never_become_battlefield_activations(self):
        game_state, handler, controller, opponent = self._state(31042)
        thunder_id = inject_real_card(
            game_state, controller, "Thunder Magic", "battlefield")
        self._add_real_lands(game_state, controller, "Mountain", 1)
        inject_into_zone(
            game_state, opponent,
            self._durable_creature("Thunder activation decoy"),
            "battlefield")
        game_state.ability_handler.register_card_abilities(
            thunder_id, controller)
        battlefield_index = controller["battlefield"].index(thunder_id)

        mask = self._mask(handler)
        exposed = []
        for action, allowed in enumerate(mask):
            if not allowed or handler.get_action_info(action)[0] \
                    != "ACTIVATE_ABILITY":
                continue
            metadata = handler.action_reasons_with_context.get(action, {})
            context = metadata.get("context", {}) or {}
            if context.get("battlefield_idx") == battlefield_index:
                exposed.append(action)
        self.assertEqual(
            exposed, [],
            "Tiered spell rows were registered as generic activated abilities",
        )


if __name__ == "__main__":
    unittest.main()
