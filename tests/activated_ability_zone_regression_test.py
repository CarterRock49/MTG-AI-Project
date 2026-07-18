from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, get_env, inject_into_zone  # noqa: E402


class ActivatedAbilityZoneRegressionTest(unittest.TestCase):
    def test_hand_only_keyword_ability_is_not_exposed_on_battlefield(self):
        game_state = fresh(271803)
        controller = game_state.p1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        source_id = inject_into_zone(game_state, controller, {
            "name": "Cycling Zone Probe",
            "mana_cost": "{2}",
            "type_line": "Artifact",
            "oracle_text": "Cycling {2}",
        }, "battlefield")
        abilities = game_state.ability_handler.get_activated_abilities(
            source_id)
        self.assertTrue(
            any(getattr(ability, "zone", None) == "hand"
                for ability in abilities),
            "fixture did not register the hand-only Cycling declaration")

        battlefield_index = controller["battlefield"].index(source_id)
        handler = get_env().action_handler
        mask = handler.generate_valid_actions()
        exposed = []
        for action, allowed in enumerate(mask):
            if not allowed:
                continue
            action_type, _ = handler.get_action_info(action)
            context = handler.action_reasons_with_context.get(
                action, {}).get("context", {}) or {}
            if (action_type == "ACTIVATE_ABILITY"
                    and context.get("battlefield_idx") == battlefield_index):
                exposed.append(action)
        self.assertEqual(
            exposed, [],
            f"a hand-only Cycling ability leaked onto the battlefield: {exposed}")


if __name__ == "__main__":
    unittest.main()
