"""Fail-closed regressions for coupled spell-copy instructions."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_into_zone, inject_real_card  # noqa: E402
from Playersim.ability_types import (  # noqa: E402
    CopySpellEffect,
    TriggeredAbility,
    UnsupportedEffect,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402


logging.disable(logging.CRITICAL)


class SpellCopyGuardRegressionTest(unittest.TestCase):
    @staticmethod
    def _copy_trigger(game_state, source_id):
        triggers = [
            ability for ability
            in game_state.ability_handler.registered_abilities.get(
                source_id, [])
            if (isinstance(ability, TriggeredAbility)
                and "copy" in ability.effect.casefold())
        ]
        if len(triggers) != 1:
            raise AssertionError(
                f"expected one registered copy trigger, got {triggers!r}")
        return triggers[0]

    def test_real_coupled_spell_copy_shapes_are_single_unsupported_effects(self):
        card_names = (
            "Alania, Divergent Storm",
            "Aziza, Mage Tower Captain",
            "Breeches, the Blastmaker",
            "Jackal, Genius Geneticist",
            "Mica, Reader of Ruins",
        )
        for index, card_name in enumerate(card_names):
            with self.subTest(card=card_name):
                game_state = fresh(38530 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                card = game_state._safe_get_card(source_id)
                trigger = self._copy_trigger(game_state, source_id)

                for surface_name, surface in (
                        ("registered trigger", trigger.effect),
                        ("full oracle", card.oracle_text)):
                    with self.subTest(
                            card=card_name, surface=surface_name):
                        effects = EffectFactory.create_effects(
                            surface, source_name=card.name)
                        self.assertEqual(len(effects), 1)
                        self.assertIsInstance(
                            effects[0], UnsupportedEffect)
                        self.assertFalse(any(
                            isinstance(effect, CopySpellEffect)
                            for effect in effects))

    def test_aziza_trigger_without_three_creatures_never_copies_silently(self):
        game_state = fresh(38536)
        controller = game_state.p1
        aziza_id = inject_real_card(
            game_state, controller,
            "Aziza, Mage Tower Captain", "battlefield")
        trigger = self._copy_trigger(game_state, aziza_id)
        self.assertIn(
            "you may tap three untapped creatures you control",
            trigger.effect.casefold())

        spell_id = inject_into_zone(game_state, controller, {
            "name": "Aziza Probe Instant",
            "mana_cost": "{U}",
            "cmc": 1,
            "type_line": "Instant",
            "oracle_text": "Draw a card.",
            "color_identity": ["U"],
        }, "hand")
        controller["hand"].remove(spell_id)
        game_state.add_to_stack(
            "SPELL", spell_id, controller,
            {"source_zone": "hand", "requires_target": False})
        game_state.ability_handler.active_triggers.clear()

        self.assertTrue(game_state.trigger_ability(None, "CAST_SPELL", {
            "cast_card_id": spell_id,
            "casting_player": controller,
            "targets": {},
            "cast_card_types": ["instant"],
        }))
        self.assertEqual(
            [queued[0] for queued
             in game_state.ability_handler.active_triggers],
            [trigger])

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(
            [(item[0], item[1]) for item in game_state.stack],
            [("SPELL", spell_id), ("TRIGGER", aziza_id)])
        fidelity_before = game_state.fidelity_counters[
            "unparsed_effects"]

        game_state.resolve_top_of_stack()

        spell_rows = [
            item for item in game_state.stack
            if item[0] == "SPELL" and item[1] == spell_id]
        self.assertEqual(len(spell_rows), 1)
        self.assertFalse(any(
            item[3].get("is_copy") for item in spell_rows))
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before + 1)
        self.assertIn(
            "Aziza, Mage Tower Captain",
            game_state.fidelity_counters["unparsed_cards"])

    def test_audited_source_coupled_copy_inventory_is_single_unsupported(self):
        card_names = (
            "Choreographed Sparks",
            "Cursed Recording",
            "Double Down",
            "Ether",
            "Fin Fang Foom",
            "Fire Lord Azula",
            "Jeong Jeong, the Deserter",
            "Kaervek, the Punisher",
            "Kaya, Spirits' Justice",
            "Kitsa, Otterball Elite",
            "Loki Laufeyson",
            "Pyromancer's Goggles",
            "Ral, Crackling Wit",
            "Return the Favor",
            "Rimefire Torque",
            "Roving Actuator",
            "Shiko, Paragon of the Way",
            "Silverquill, the Disputant",
            "Slick Imitator",
            "Taigam, Master Opportunist",
        )
        for index, card_name in enumerate(card_names):
            with self.subTest(card=card_name):
                game_state = fresh(38540 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                card = game_state._safe_get_card(source_id)
                effects = EffectFactory.create_effects(
                    card.oracle_text, source_name=card.name)
                self.assertEqual(len(effects), 1)
                self.assertIsInstance(effects[0], UnsupportedEffect)
                self.assertFalse(any(
                    isinstance(effect, CopySpellEffect)
                    for effect in effects))

    def test_wrong_gate_triggers_never_copy_or_partially_mutate(self):
        card_names = (
            "Double Down",
            "Fire Lord Azula",
            "Fin Fang Foom",
        )
        for index, card_name in enumerate(card_names):
            with self.subTest(card=card_name):
                game_state = fresh(38570 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                source = game_state._safe_get_card(source_id)
                trigger = self._copy_trigger(game_state, source_id)

                spell_id = inject_into_zone(game_state, controller, {
                    "name": f"{card_name} Wrong-Gate Instant",
                    "mana_cost": "{U}",
                    "cmc": 1,
                    "type_line": "Instant",
                    "oracle_text": "Draw a card.",
                    "color_identity": ["U"],
                }, "hand")
                spell = game_state._safe_get_card(spell_id)
                controller["hand"].remove(spell_id)
                game_state.add_to_stack(
                    "SPELL", spell_id, controller,
                    {"source_zone": "hand", "requires_target": False})
                game_state.ability_handler.active_triggers.clear()
                event = {
                    "cast_card_id": spell_id,
                    "casting_player": controller,
                    "targets": {},
                    "cast_card_types": list(spell.card_types),
                    "cast_card_subtypes": list(spell.subtypes),
                }

                # These are deliberately invalid printed gates: the spell is
                # not an Outlaw, Azula is not attacking, and it targets no
                # artifact or land. The current trigger matcher may queue the
                # trigger, but resolution must fail closed before any effect.
                self.assertTrue(game_state.trigger_ability(
                    None, "CAST_SPELL", event))
                self.assertEqual(
                    [queued[0] for queued
                     in game_state.ability_handler.active_triggers],
                    [trigger])
                game_state.ability_handler.process_triggered_abilities()
                counters_before = dict(source.counters)
                battlefield_before = list(controller["battlefield"])
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]

                game_state.resolve_top_of_stack()

                spell_rows = [
                    item for item in game_state.stack
                    if item[0] == "SPELL" and item[1] == spell_id]
                self.assertEqual(len(spell_rows), 1)
                self.assertFalse(spell_rows[0][3].get("is_copy"))
                self.assertEqual(
                    list(controller["battlefield"]), battlefield_before)
                self.assertEqual(dict(source.counters), counters_before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before + 1)
                self.assertIn(
                    card_name,
                    game_state.fidelity_counters["unparsed_cards"])

    def test_unsupported_copy_activations_are_masked_before_costs(self):
        card_names = (
            "Cursed Recording",
            "Ether",
            "Jeong Jeong, the Deserter",
            "Kitsa, Otterball Elite",
            "Loki Laufeyson",
            "Pyromancer's Goggles",
            "Rimefire Torque",
            "Slick Imitator",
        )
        for index, card_name in enumerate(card_names):
            with self.subTest(card=card_name):
                game_state = fresh(38580 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                source = game_state._safe_get_card(source_id)
                entered = controller.get(
                    "entered_battlefield_this_turn", set())
                if hasattr(entered, "discard"):
                    entered.discard(source_id)
                elif source_id in entered:
                    entered.remove(source_id)
                controller["mana_pool"].update({
                    "W": 10, "U": 10, "B": 10,
                    "R": 10, "G": 10, "C": 10,
                })
                if card_name == "Rimefire Torque":
                    game_state.add_counter(source_id, "charge", 3)

                abilities = (
                    game_state.ability_handler.get_activated_abilities(
                        source_id))
                matches = [
                    (ability_index, ability)
                    for ability_index, ability in enumerate(abilities)
                    if EffectFactory.is_unsupported_source_coupled_copy(
                        source.name,
                        " ".join((
                            str(getattr(ability, "effect_text", "") or ""),
                            str(getattr(ability, "effect", "") or ""))))
                ]
                self.assertEqual(len(matches), 1)
                ability_index, ability = matches[0]
                if card_name == "Kitsa, Otterball Elite":
                    self.assertEqual(int(source.power), 1)

                state_before = (
                    list(controller["battlefield"]),
                    list(controller["graveyard"]),
                    set(controller["tapped_permanents"]),
                    dict(controller["mana_pool"]),
                    dict(source.counters),
                )
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]
                self.assertFalse(ability.can_pay_cost(
                    game_state, controller))
                self.assertFalse(
                    game_state.ability_handler.can_activate_ability(
                        source_id, ability_index, controller))
                self.assertFalse(
                    game_state.ability_handler.activate_ability(
                        source_id, ability_index, controller))
                self.assertEqual(
                    (
                        list(controller["battlefield"]),
                        list(controller["graveyard"]),
                        set(controller["tapped_permanents"]),
                        dict(controller["mana_pool"]),
                        dict(source.counters),
                    ),
                    state_before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before)

    def test_sword_preserves_treasure_then_diagnoses_delayed_copy(self):
        game_state = fresh(38590)
        controller = game_state.p1
        sword_id = inject_real_card(
            game_state, controller,
            "Sword of Wealth and Power", "battlefield")
        sword = game_state._safe_get_card(sword_id)
        effects = EffectFactory.create_effects(
            sword.oracle_text, source_name=sword.name)
        self.assertEqual(
            [type(effect).__name__ for effect in effects],
            ["CreateTreasureEffect", "UnsupportedEffect"])

        before = set(controller.get("tokens", []))
        fidelity_before = game_state.fidelity_counters[
            "unparsed_effects"]
        results = [
            effect.apply(
                game_state, sword_id, controller, {}, context={})
            for effect in effects]
        self.assertEqual(results, [True, False])
        created = [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]
        self.assertEqual(len(created), 1)
        treasure = game_state._safe_get_card(created[0])
        self.assertEqual(treasure.name, "Treasure")
        self.assertEqual(treasure.card_types, ["artifact"])
        self.assertEqual(treasure.subtypes, ["treasure"])
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before + 1)
        self.assertIn(
            "Sword of Wealth and Power",
            game_state.fidelity_counters["unparsed_cards"])

    def test_sword_ignores_combat_damage_from_unequipped_creature(self):
        game_state = fresh(38591)
        controller = game_state.p1
        opponent = game_state.p2
        inject_real_card(
            game_state, controller,
            "Sword of Wealth and Power", "battlefield")
        creature_id = inject_into_zone(game_state, controller, {
            "name": "Unequipped Sword Damage Probe",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Bear",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["G"],
        }, "battlefield")
        game_state.ability_handler.active_triggers.clear()

        self.assertFalse(game_state._emit_source_damage_event(
            creature_id, "p2", 2,
            target_player=opponent, is_combat_damage=True))
        self.assertEqual(
            game_state.ability_handler.active_triggers, [])
        self.assertEqual(controller.get("tokens", []), [])

    def test_sword_hears_its_equipped_creatures_combat_damage(self):
        game_state = fresh(38592)
        controller = game_state.p1
        opponent = game_state.p2
        sword_id = inject_real_card(
            game_state, controller,
            "Sword of Wealth and Power", "battlefield")
        creature_id = inject_into_zone(game_state, controller, {
            "name": "Equipped Sword Damage Probe",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Bear",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["G"],
        }, "battlefield")
        self.assertTrue(game_state.equip_permanent(
            controller, sword_id, creature_id))
        game_state.ability_handler.active_triggers.clear()

        self.assertTrue(game_state._emit_source_damage_event(
            creature_id, "p2", 2,
            target_player=opponent, is_combat_damage=True))
        queued = game_state.ability_handler.active_triggers
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0].card_id, sword_id)

        game_state.ability_handler.process_triggered_abilities()
        before = set(controller.get("tokens", []))
        fidelity_before = game_state.fidelity_counters[
            "unparsed_effects"]
        self.assertFalse(game_state.resolve_top_of_stack())
        created = [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]
        self.assertEqual(len(created), 1)
        treasure = game_state._safe_get_card(created[0])
        self.assertEqual(treasure.name, "Treasure")
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before + 1)
        self.assertIn(
            "Sword of Wealth and Power",
            game_state.fidelity_counters["unparsed_cards"])

    def test_neighboring_equipment_uses_same_damage_attachment_scope(self):
        game_state = fresh(38593)
        controller = game_state.p1
        opponent = game_state.p2
        pick_id = inject_real_card(
            game_state, controller, "Goldvein Pick", "battlefield")
        equipped_id = inject_into_zone(game_state, controller, {
            "name": "Goldvein Equipped Probe",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Bear",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["G"],
        }, "battlefield")
        other_id = inject_into_zone(game_state, controller, {
            "name": "Goldvein Unequipped Probe",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Bear",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["G"],
        }, "battlefield")
        self.assertTrue(game_state.equip_permanent(
            controller, pick_id, equipped_id))
        game_state.ability_handler.active_triggers.clear()

        self.assertFalse(game_state._emit_source_damage_event(
            other_id, "p2", 2,
            target_player=opponent, is_combat_damage=True))
        self.assertEqual(
            game_state.ability_handler.active_triggers, [])

        self.assertTrue(game_state._emit_source_damage_event(
            equipped_id, "p2", 2,
            target_player=opponent, is_combat_damage=True))
        queued = game_state.ability_handler.active_triggers
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0].card_id, pick_id)

    def test_plain_leyline_and_sage_copy_instructions_remain_supported(self):
        for source_name, instruction in (
                (
                    "Leyline of Resonance",
                    "Copy that spell. You may choose new targets for the copy.",
                ),
                (
                    "Sage of the Skies",
                    "Copy this spell. (The copy becomes a token.)",
                )):
            with self.subTest(card=source_name):
                effects = EffectFactory.create_effects(
                    instruction, source_name=source_name)
                self.assertFalse(any(
                    isinstance(effect, UnsupportedEffect)
                    for effect in effects))
                self.assertEqual(
                    sum(isinstance(effect, CopySpellEffect)
                        for effect in effects),
                    1)


if __name__ == "__main__":
    unittest.main()
