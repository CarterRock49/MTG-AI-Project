# actions.py

import logging
import re
import numpy as np
import random
from collections import defaultdict
from .card import Card
from .enhanced_combat import ExtendedCombatResolver
from .combat_integration import integrate_combat_actions, apply_combat_action
from .debug import DEBUG_MODE
from .enhanced_card_evaluator import EnhancedCardEvaluator
from .combat_actions import CombatActionHandler


# ACTION_MEANINGS dictionary - Corrected and verified for size 480 (Indices 0-479)
ACTION_MEANINGS = {
    # Basic game flow (0-12) = 13 actions
    0: ("END_TURN", None), 1: ("UNTAP_NEXT", None), 2: ("DRAW_NEXT", None), 3: ("MAIN_PHASE_END", None),
    4: ("COMBAT_DAMAGE", None), 5: ("END_PHASE", None), 6: ("MULLIGAN", None), 7: ("UPKEEP_PASS", None),
    8: ("BEGIN_COMBAT_END", None), 9: ("END_COMBAT", None), 10: ("END_STEP", None), 11: ("PASS_PRIORITY", None),
    12: ("CONCEDE", None),

    # Play land (13-19) = 7 actions (param=hand index 0-6)
    **{i: ("PLAY_LAND", i-13) for i in range(13, 20)},

    # Play spell (20-27) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_SPELL", i-20) for i in range(20, 28)},

    # Attack (28-47) = 20 actions (param=battlefield index 0-19)
    **{i: ("ATTACK", i-28) for i in range(28, 48)},

    # Block (48-67) = 20 actions (param=battlefield index 0-19)
    **{i: ("BLOCK", i-48) for i in range(48, 68)},

    # Tap land for mana (68-87) = 20 actions (param=battlefield index 0-19)
    **{i: ("TAP_LAND_FOR_MANA", i-68) for i in range(68, 88)},

    # Tap land for effect (88-99) = 12 actions (param=battlefield index 0-11)
    **{i: ("TAP_LAND_FOR_EFFECT", i-88) for i in range(88, 100)},

    # Ability activation (100-159) = 60 actions (param=(battlefield index 0-19, ability index 0-2))
    **{100 + (i * 3) + j: ("ACTIVATE_ABILITY", (i, j)) for i in range(20) for j in range(3)},

    # Transform (160-179) = 20 actions (param=battlefield index 0-19)
    **{160 + i: ("TRANSFORM", i) for i in range(20)},

    # MDFC Land Back (180-187) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_MDFC_LAND_BACK", i-180) for i in range(180, 188)},

    # MDFC Spell Back (188-195) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_MDFC_BACK", i-188) for i in range(188, 196)},

    # Adventure (196-203) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_ADVENTURE", i-196) for i in range(196, 204)},

    # Defend Battle (204-223) = 20 actions (param=(battle index 0-4, creature index 0-3) - NEEDS CONTEXT)
    **{204 + (i * 4) + j: ("DEFEND_BATTLE", (i, j)) for i in range(5) for j in range(4)},

    # NO_OP (224)
    224: ("NO_OP", None),

    # Mulligan (225-229) = 5 actions
    225: ("KEEP_HAND", None),
    **{226 + i: ("BOTTOM_CARD", i) for i in range(4)},  # param=card index 0-3

    # Cast from Exile (230-237) = 8 actions (param=exile index 0-7)
    **{i: ("CAST_FROM_EXILE", i-230) for i in range(230, 238)},

    # Discard (238-247) = 10 actions (param=hand index 0-9)
    **{238 + i: ("DISCARD_CARD", i) for i in range(10)},

    # Room/Class (248-257) = 10 actions
    **{248 + i: ("UNLOCK_DOOR", i) for i in range(5)}, # param=battlefield index 0-4
    **{253 + i: ("LEVEL_UP_CLASS", i) for i in range(5)}, # param=battlefield index 0-4

    # Spree Mode (258-273) = 16 actions (param=(hand index 0-7, mode index 0-1) - NEEDS CONTEXT)
    **{258 + (i * 2) + j: ("SELECT_SPREE_MODE", (i, j)) for i in range(8) for j in range(2)},

    # Targeting (274-293) = 20 actions
    **{274 + i: ("SELECT_TARGET", i) for i in range(10)}, # param=valid target index 0-9
    **{284 + i: ("SACRIFICE_PERMANENT", i) for i in range(10)}, # param=valid sacrifice index 0-9

    # Gaps filled with NO_OP (294-298) = 5 actions
    **{i: ("NO_OP", None) for i in range(294, 299)},

    # Library/Card Movement (299-308) = 10 actions
    **{299 + i: ("SEARCH_LIBRARY", i) for i in range(5)}, # param=search type 0-4
    304: ("NO_OP_SEARCH_FAIL", None),
    305: ("PUT_TO_GRAVEYARD", None), # param=choice index (usually 0)
    306: ("PUT_ON_TOP", None),       # param=choice index (usually 0)
    307: ("PUT_ON_BOTTOM", None),    # param=choice index (usually 0)
    308: ("DREDGE", None),           # param=graveyard index 0-? (Needs Context)

    # Counter Management (309-329) = 21 actions
    **{309 + i: ("ADD_COUNTER", i) for i in range(10)}, # param=permanent index 0-9 (Needs Context for type/count)
    **{319 + i: ("REMOVE_COUNTER", i) for i in range(10)}, # param=permanent index 0-9 (Needs Context for type/count)
    329: ("PROLIFERATE", None),

    # Zone Movement (330-347) = 18 actions
    **{330 + i: ("RETURN_FROM_GRAVEYARD", i) for i in range(6)}, # param=graveyard index 0-5
    **{336 + i: ("REANIMATE", i) for i in range(6)}, # param=graveyard index 0-5
    **{342 + i: ("RETURN_FROM_EXILE", i) for i in range(6)}, # param=exile index 0-5

    # Modal/Choice (348-372) = 25 actions
    **{348 + i: ("CHOOSE_MODE", i) for i in range(10)}, # param=mode index 0-9
    **{358 + i: ("CHOOSE_X_VALUE", i+1) for i in range(10)}, # param=X value 1-10
    **{368 + i: ("CHOOSE_COLOR", i) for i in range(5)}, # param=WUBRG index 0-4

    # Advanced Combat (373-377, 383-392) = 15 actions
    **{373 + i: ("ATTACK_PLANESWALKER", i) for i in range(5)}, # param=opponent PW index 0-4
    # Gap filled with NO_OP (378-382) = 5 actions
    **{i: ("NO_OP", None) for i in range(378, 383)},
    **{383 + i: ("ASSIGN_MULTIPLE_BLOCKERS", i) for i in range(10)}, # param=attacker index 0-9

    # Alternative Casting (393-404) = 12 actions
    393: ("CAST_WITH_FLASHBACK", None), # param=graveyard index (Needs Context)
    394: ("CAST_WITH_JUMP_START", None), # param=graveyard index (Needs Context for discard)
    395: ("CAST_WITH_ESCAPE", None), # param=graveyard index (Needs Context for exile choice)
    396: ("CAST_FOR_MADNESS", None), # param=exile index (Needs Context)
    397: ("CAST_WITH_OVERLOAD", None), # param=hand index
    398: ("CAST_FOR_EMERGE", None), # param=(hand_idx, sac_idx) (Needs Context)
    399: ("CAST_FOR_DELVE", None), # param=(hand_idx, List[GY_idx]) (Needs Context)
    400: ("PAY_KICKER", True), # Informational, modifies context for PLAY_SPELL
    401: ("PAY_KICKER", False),# Informational, modifies context for PLAY_SPELL
    402: ("PAY_ADDITIONAL_COST", True), # Informational, modifies context for PLAY_SPELL
    403: ("PAY_ADDITIONAL_COST", False),# Informational, modifies context for PLAY_SPELL
    404: ("PAY_ESCALATE", None), # param=num_extra_modes (Needs Context)

    # Token/Copy (405-412) = 8 actions
    **{405 + i: ("CREATE_TOKEN", i) for i in range(5)}, # param=predefined token type index 0-4
    410: ("COPY_PERMANENT", None), # param=target permanent index (Needs Context)
    411: ("COPY_SPELL", None), # param=target spell stack index (Needs Context)
    412: ("POPULATE", None), # param=target token index (Needs Context)

    # Specific Mechanics (413-424) = 12 actions
    413: ("INVESTIGATE", None), 414: ("FORETELL", None), # param=hand index
    415: ("AMASS", None), # param=amount (Needs Context)
    416: ("LEARN", None),
    417: ("VENTURE", None), 418: ("EXERT", None), # param=creature index
    419: ("EXPLORE", None), # param=creature index
    420: ("ADAPT", None), # param=creature index (Needs Context for amount)
    421: ("MUTATE", None), # param=(hand_idx, target_idx) (Needs Context)
    422: ("CYCLING", None), # param=hand index
    423: ("GOAD", None), # param=target creature index
    424: ("BOAST", None), # param=creature index

    # Response Actions (425-429) = 5 actions
    425: ("COUNTER_SPELL", None), # param=hand_idx (Needs Context for target)
    426: ("COUNTER_ABILITY", None), # param=hand_idx (Needs Context for target)
    427: ("PREVENT_DAMAGE", None), # param=hand_idx (Needs Context for amount/target)
    428: ("REDIRECT_DAMAGE", None), # param=hand_idx (Needs Context for details)
    429: ("STIFLE_TRIGGER", None), # param=hand_idx (Needs Context for target)

    # Combat Actions (430-439) = 10 actions
    430: ("FIRST_STRIKE_ORDER", None), # param=assignments (Needs Context)
    431: ("ASSIGN_COMBAT_DAMAGE", None), # param=assignments (Needs Context)
    432: ("NINJUTSU", None), # param=(ninja_hand_idx, attacker_idx)
    433: ("DECLARE_ATTACKERS_DONE", None), 434: ("DECLARE_BLOCKERS_DONE", None),
    435: ("LOYALTY_ABILITY_PLUS", None), # param=battlefield index
    436: ("LOYALTY_ABILITY_ZERO", None), # param=battlefield index
    437: ("LOYALTY_ABILITY_MINUS", None),# param=battlefield index
    438: ("ULTIMATE_ABILITY", None),     # param=battlefield index
    439: ("PROTECT_PLANESWALKER", None), # param=(pw_idx, defender_idx) (Needs Context)

    # Card Type Specific (440-453, 455, 457-459) = 17 actions (14 + 1 + 3 NO_OPs)
    440: ("CAST_LEFT_HALF", None), # param = hand index
    441: ("CAST_RIGHT_HALF", None), # param = hand index
    442: ("CAST_FUSE", None), # param = hand index
    443: ("AFTERMATH_CAST", None), # param = GY index
    444: ("FLIP_CARD", None), # param = battlefield index
    445: ("EQUIP", None), # param = (equip_idx, creature_idx) (Needs Context)
    446: ("UNEQUIP", None), # param = equip index
    447: ("ATTACH_AURA", None), # param = (aura_idx, target_idx) (Needs Context)
    448: ("FORTIFY", None), # param = (fort_idx, land_idx) (Needs Context)
    449: ("RECONFIGURE", None), # param = battlefield index
    450: ("MORPH", None), # param = battlefield index
    451: ("MANIFEST", None), # param = battlefield index
    452: ("CLASH", None),
    453: ("CONSPIRE", None), # param = (spell_stack_idx, creature1_idx, creature2_idx) (Needs Context)
    454: ("NO_OP", None), # Replaced CONVOKE
    455: ("GRANDEUR", None), # param = hand index
    456: ("NO_OP", None), # Replaced HELLBENT

    # Gap filled with NO_OP (457-459) = 3 actions
    **{i: ("NO_OP", None) for i in range(457, 460)},

    # Actions 460-464: Target Battle index 0-4
    **{460 + i: ("ATTACK_BATTLE", i) for i in range(5)}, # param = relative battle index 0-4
    # Fill the remaining space (465-479) with No-Ops
    **{i: ("NO_OP", None) for i in range(465, 480)}
}

# Verify final size after changes
required_size = 480
if len(ACTION_MEANINGS) != required_size:
    logging.warning(f"ACTION_MEANINGS size incorrect after update: {len(ACTION_MEANINGS)}, expected {required_size}. Re-adjusting...")
    max_idx = max(ACTION_MEANINGS.keys()) if ACTION_MEANINGS else -1
    for i in range(required_size):
        if i not in ACTION_MEANINGS:
            ACTION_MEANINGS[i] = ("NO_OP", None)
    if len(ACTION_MEANINGS) > required_size:
        keys_to_remove = [k for k in ACTION_MEANINGS if k >= required_size]
        for k in keys_to_remove: del ACTION_MEANINGS[k]
    logging.info(f"ACTION_MEANINGS size corrected to {len(ACTION_MEANINGS)}")

class ActionHandler:
    """Handles action validation and execution"""


    ACTION_SPACE_SIZE = 480 # Define constant for action space size

    def __init__(self, game_state):
        self.game_state = game_state
        self.action_reasons = {} # For debugging valid actions
        try:
            if not hasattr(game_state, 'card_evaluator') or game_state.card_evaluator is None:
                self.card_evaluator = EnhancedCardEvaluator(game_state,
                getattr(game_state, 'stats_tracker', None),
                getattr(game_state, 'card_memory', None))
                game_state.card_evaluator = self.card_evaluator
            else:
                self.card_evaluator = game_state.card_evaluator
        except Exception as e:
            logging.error(f"Error initializing EnhancedCardEvaluator: {e}")
            self.card_evaluator = None
            if hasattr(game_state, 'card_evaluator'): game_state.card_evaluator = None

        self.combat_handler = integrate_combat_actions(self.game_state)

        if self.combat_handler:
            self.combat_handler.setup_combat_systems()
        else:
            logging.error("CombatActionHandler could not be initialized!")
            if not hasattr(self.game_state, 'current_attackers'):
                self.game_state.current_attackers = []
            if not hasattr(self.game_state, 'current_block_assignments'):
                self.game_state.current_block_assignments = {}

        self.action_handlers = self._get_action_handlers() # Initialize handlers
        
    def _get_action_handlers(self):
        """Maps action type strings to their handler methods."""
        # --- Updated Handler Map ---
        # Removes CONVOKE and HELLBENT handlers, ensures others exist.
        return {
            # Basic Flow
            "END_TURN": self._handle_end_turn, "UNTAP_NEXT": self._handle_untap_next,
            "DRAW_NEXT": self._handle_draw_next, "MAIN_PHASE_END": self._handle_main_phase_end,
            "COMBAT_DAMAGE": self._handle_combat_damage, "END_PHASE": self._handle_end_phase,
            "MULLIGAN": self._handle_mulligan, "KEEP_HAND": self._handle_keep_hand,
            "BOTTOM_CARD": self._handle_bottom_card, "UPKEEP_PASS": self._handle_upkeep_pass,
            "BEGIN_COMBAT_END": self._handle_begin_combat_end, "END_COMBAT": self._handle_end_combat,
            "END_STEP": self._handle_end_step, "PASS_PRIORITY": self._handle_pass_priority,
            "CONCEDE": self._handle_concede,
            # Play Cards
            "PLAY_LAND": self._handle_play_land, "PLAY_SPELL": self._handle_play_spell,
            "PLAY_MDFC_LAND_BACK": self._handle_play_mdfc_land_back,
            "PLAY_MDFC_BACK": self._handle_play_mdfc_back,
            "PLAY_ADVENTURE": self._handle_play_adventure,
            "CAST_FROM_EXILE": self._handle_cast_from_exile,
            # Simple Combat
            "ATTACK": self._handle_attack,
            "BLOCK": self._handle_block,
            # Delegated Combat Actions
            "DECLARE_ATTACKERS_DONE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DECLARE_ATTACKERS_DONE", p, context=context),
            "DECLARE_BLOCKERS_DONE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DECLARE_BLOCKERS_DONE", p, context=context),
            "ATTACK_PLANESWALKER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ATTACK_PLANESWALKER", p, context=context),
            "ASSIGN_MULTIPLE_BLOCKERS": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ASSIGN_MULTIPLE_BLOCKERS", p, context),
            "FIRST_STRIKE_ORDER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "FIRST_STRIKE_ORDER", p, context),
            "ASSIGN_COMBAT_DAMAGE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ASSIGN_COMBAT_DAMAGE", p, context),
            "PROTECT_PLANESWALKER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "PROTECT_PLANESWALKER", None, context),
            "ATTACK_BATTLE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ATTACK_BATTLE", p, context=context),
            "DEFEND_BATTLE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DEFEND_BATTLE", None, context),
            "NINJUTSU": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "NINJUTSU", None, context),
            # Abilities & Mana
            "TAP_LAND_FOR_MANA": self._handle_tap_land_for_mana,
            "TAP_LAND_FOR_EFFECT": self._handle_tap_land_for_effect,
            "ACTIVATE_ABILITY": self._handle_activate_ability,
            "LOYALTY_ABILITY_PLUS": self._handle_loyalty_ability,
            "LOYALTY_ABILITY_ZERO": self._handle_loyalty_ability,
            "LOYALTY_ABILITY_MINUS": self._handle_loyalty_ability,
            "ULTIMATE_ABILITY": self._handle_loyalty_ability,
            # Targeting & Choices
            "SELECT_TARGET": self._handle_select_target,
            "SACRIFICE_PERMANENT": self._handle_sacrifice_permanent,
            "CHOOSE_MODE": self._handle_choose_mode,
            "CHOOSE_X_VALUE": self._handle_choose_x,
            "CHOOSE_COLOR": self._handle_choose_color,
            "PUT_TO_GRAVEYARD": self._handle_surveil_choice, # Linked to choice handler
            "PUT_ON_TOP": self._handle_scry_surveil_choice,  # Linked to choice handler
            "PUT_ON_BOTTOM": self._handle_scry_choice,       # Linked to choice handler
            # Library/Card Movement
            "SEARCH_LIBRARY": self._handle_search_library,
            "DREDGE": self._handle_dredge,
            # Counter Management
            "ADD_COUNTER": self._handle_add_counter,
            "REMOVE_COUNTER": self._handle_remove_counter,
            "PROLIFERATE": self._handle_proliferate,
            # Zone Movement
            "RETURN_FROM_GRAVEYARD": self._handle_return_from_graveyard,
            "REANIMATE": self._handle_reanimate,
            "RETURN_FROM_EXILE": self._handle_return_from_exile,
            # Alternative Casting
            "CAST_WITH_FLASHBACK": lambda p=None, context=None, **k: self._handle_alternative_casting(p, "CAST_WITH_FLASHBACK", context=context, **k),
            "CAST_WITH_JUMP_START": lambda p=None, context=None, **k: self._handle_alternative_casting(p, "CAST_WITH_JUMP_START", context=context, **k),
            "CAST_WITH_ESCAPE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, "CAST_WITH_ESCAPE", context=context, **k),
            "CAST_FOR_MADNESS": lambda p=None, context=None, **k: self._handle_alternative_casting(p, "CAST_FOR_MADNESS", context=context, **k),
            "CAST_WITH_OVERLOAD": lambda p=None, context=None, **k: self._handle_alternative_casting(p, "CAST_WITH_OVERLOAD", context=context, **k),
            "CAST_FOR_EMERGE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, "CAST_FOR_EMERGE", context=context, **k),
            "CAST_FOR_DELVE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, "CAST_FOR_DELVE", context=context, **k),
            "AFTERMATH_CAST": lambda p=None, context=None, **k: self._handle_alternative_casting(p, "AFTERMATH_CAST", context=context, **k),
            # Informational Flags
            "PAY_KICKER": self._handle_pay_kicker,
            "PAY_ADDITIONAL_COST": self._handle_pay_additional_cost,
            "PAY_ESCALATE": self._handle_pay_escalate,
            # Token/Copy
            "CREATE_TOKEN": self._handle_create_token,
            "COPY_PERMANENT": self._handle_copy_permanent,
            "COPY_SPELL": self._handle_copy_spell,
            "POPULATE": self._handle_populate,
            # Specific Mechanics
            "INVESTIGATE": self._handle_investigate,
            "FORETELL": self._handle_foretell,
            "AMASS": self._handle_amass,
            "LEARN": self._handle_learn,
            "VENTURE": self._handle_venture,
            "EXERT": self._handle_exert,
            "EXPLORE": self._handle_explore,
            "ADAPT": self._handle_adapt,
            "MUTATE": self._handle_mutate,
            "CYCLING": self._handle_cycling,
            "GOAD": self._handle_goad,
            "BOAST": self._handle_boast,
            # Response Actions
            "COUNTER_SPELL": self._handle_counter_spell,
            "COUNTER_ABILITY": self._handle_counter_ability,
            "PREVENT_DAMAGE": self._handle_prevent_damage,
            "REDIRECT_DAMAGE": self._handle_redirect_damage,
            "STIFLE_TRIGGER": self._handle_stifle_trigger,
            # Card Type Specific
            "CAST_LEFT_HALF": lambda p=None, context=None, **k: self._handle_cast_split(p, "CAST_LEFT_HALF", context=context, **k),
            "CAST_RIGHT_HALF": lambda p=None, context=None, **k: self._handle_cast_split(p, "CAST_RIGHT_HALF", context=context, **k),
            "CAST_FUSE": lambda p=None, context=None, **k: self._handle_cast_split(p, "CAST_FUSE", context=context, **k),
            "FLIP_CARD": self._handle_flip_card,
            "EQUIP": self._handle_equip,
            "UNEQUIP": self._handle_unequip,
            "ATTACH_AURA": self._handle_attach_aura,
            "FORTIFY": self._handle_fortify,
            "RECONFIGURE": self._handle_reconfigure,
            "MORPH": self._handle_morph,
            "MANIFEST": self._handle_manifest,
            "CLASH": self._handle_clash,
            "CONSPIRE": self._handle_conspire,
            "GRANDEUR": self._handle_grandeur,
            # Room/Class
            "UNLOCK_DOOR": self._handle_unlock_door,
            "LEVEL_UP_CLASS": self._handle_level_up_class,
            "DISCARD_CARD": self._handle_discard_card,
            "SELECT_SPREE_MODE": self._handle_select_spree_mode,
            # NO_OP
            "NO_OP": self._handle_no_op,
            "NO_OP_SEARCH_FAIL": self._handle_no_op,
            # TRANSFORM
            "TRANSFORM": self._handle_transform
            # CONVOKE and HELLBENT handlers are intentionally removed.
        }

    def _add_battle_attack_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_battle_attack_actions"""
        if self.combat_handler:
            self.combat_handler._add_battle_attack_actions(player, valid_actions, set_valid_action)

    def is_valid_attacker(self, card_id):
        """Delegate to CombatActionHandler.is_valid_attacker"""
        if self.combat_handler:
            return self.combat_handler.is_valid_attacker(card_id)
        # --- Fallback logic (kept for reference, but delegation is preferred) ---
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        if not card or 'creature' not in getattr(card, 'card_types', []): return False
        if card_id in me.get("tapped_permanents", set()): return False
        # Use GameState's _has_haste method which likely checks LayerSystem
        if card_id in me.get("entered_battlefield_this_turn", set()) and not self._has_haste(card_id): return False
        # Check defender keyword via centralized check
        if self._has_keyword(card, "defender"): return False
        return True
    
    def _has_keyword(self, card, keyword):
        """Checks if a card has a keyword using the central checker."""
        gs = self.game_state
        card_id = getattr(card, 'card_id', None)
        if not card_id: return False

        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            # Use AbilityHandler's check method if available
            if hasattr(gs.ability_handler, 'check_keyword'):
                 return gs.ability_handler.check_keyword(card_id, keyword)

        # Fallback: Check the card's own keyword array
        logging.warning(f"Using basic card keyword fallback check for {keyword} on {getattr(card, 'name', 'Unknown')}")
        if hasattr(card, 'has_keyword'):
             return card.has_keyword(keyword) # Assumes card object has checker
        return False

    def find_optimal_attack(self):
        """Delegate to CombatActionHandler.find_optimal_attack"""
        if self.combat_handler:
            return self.combat_handler.find_optimal_attack()
        return [] # _handle_search_library

    def setup_combat_systems(self):
        """Delegate to CombatActionHandler.setup_combat_systems"""
        if self.combat_handler:
            self.combat_handler.setup_combat_systems()

    def _has_first_strike(self, card):
        """Delegate to CombatActionHandler._has_first_strike"""
        if self.combat_handler:
            return self.combat_handler._has_first_strike(card)
        # _handle_search_library
        if not card: return False
        if hasattr(card, 'oracle_text') and "first strike" in card.oracle_text.lower(): return True
        if hasattr(card, 'keywords') and len(card.keywords) > 5 and card.keywords[5] == 1: return True
        return False

    def _add_multiple_blocker_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_multiple_blocker_actions"""
        if self.combat_handler:
            self.combat_handler._add_multiple_blocker_actions(player, valid_actions, set_valid_action)

    def _add_ninjutsu_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_ninjutsu_actions"""
        if self.combat_handler:
            self.combat_handler._add_ninjutsu_actions(player, valid_actions, set_valid_action)

    def _add_equipment_aura_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_equipment_aura_actions"""
        if self.combat_handler:
            self.combat_handler._add_equipment_aura_actions(player, valid_actions, set_valid_action)

    def _add_planeswalker_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_planeswalker_actions"""
        if self.combat_handler:
            self.combat_handler._add_planeswalker_actions(player, valid_actions, set_valid_action)
    
    def should_hold_priority(self, player):
        """
        Determine if the player should hold priority based on game state.
        (Simplified version, can be expanded)
        """
        gs = self.game_state

        # Hold priority if stack is not empty and player has potential responses
        if gs.stack:
            # Check for instants/flash in hand
            if hasattr(gs, 'mana_system'):
                for card_id in player["hand"]:
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'card_types') and ('instant' in card.card_types or self._has_flash(card_id)):
                        if gs.mana_system.can_pay_mana_cost(player, getattr(card, 'mana_cost', "")):
                            return True

            # Check for activatable abilities
            if hasattr(gs, 'ability_handler'):
                for card_id in player["battlefield"]:
                    abilities = gs.ability_handler.get_activated_abilities(card_id)
                    for i in range(len(abilities)):
                        if gs.ability_handler.can_activate_ability(card_id, i, player):
                            return True
            return True # Hold priority if stack isn't empty, even without obvious responses for now

        # Hold priority during opponent's turn in certain phases (end step, combat)
        is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
        if not is_my_turn and gs.phase in [gs.PHASE_END_STEP, gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS]:
             return True # Simplified: always consider holding priority on opponent's turn end/combat

        return False
    
    def recommend_ability_activation(self, card_id, ability_idx):
        """
        Determine if now is a good time to activate an ability.
        Uses Strategic Planner if available, otherwise basic heuristics.
        """
        gs = self.game_state
        if hasattr(gs, 'strategic_planner') and gs.strategic_planner:
            try:
                return gs.strategic_planner.recommend_ability_activation(card_id, ability_idx)
            except Exception as e:
                logging.warning(f"Error using strategic planner for ability recommendation: {e}")
        # Fallback heuristic
        return True, 0.6 # Default to recommend with medium confidence
 
    def generate_valid_actions(self):
        """Return the current action mask as boolean array with reasoning. (Updated for New Phases)"""
        gs = self.game_state
        try:
            valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
            current_player = gs.p1 if gs.agent_is_p1 else gs.p2
            opponent = gs.p2 if gs.agent_is_p1 else gs.p1
            is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1

            action_reasons = {}

            def set_valid_action(index, reason=""):
                """Helper to set action and reason, with bounds check."""
                if 0 <= index < self.ACTION_SPACE_SIZE:
                    if not valid_actions[index]:
                        valid_actions[index] = True
                        action_reasons[index] = reason
                else:
                    logging.error(f"INVALID ACTION INDEX: {index} bounds (0-{self.ACTION_SPACE_SIZE-1}) Reason: {reason}")
                return True

            # --- Check Special Phases FIRST ---
            if hasattr(gs, 'mulligan_in_progress') and gs.mulligan_in_progress:
                 if gs.mulligan_player == current_player:
                     set_valid_action(6, "MULLIGAN")
                     set_valid_action(225, "KEEP_HAND")
                 else: # Waiting for opponent mulligan
                     set_valid_action(224, "NO_OP (Waiting for opponent mulligan)")
                 self.action_reasons = action_reasons
                 return valid_actions
            if hasattr(gs, 'bottoming_in_progress') and gs.bottoming_in_progress:
                 if gs.bottoming_player == current_player:
                     for i in range(len(current_player["hand"])):
                          if i < 4: # Limit based on action indices 226-229
                               set_valid_action(226 + i, f"BOTTOM_CARD index {i}")
                 else: # Waiting for opponent bottom
                     set_valid_action(224, "NO_OP (Waiting for opponent bottom)")
                 self.action_reasons = action_reasons
                 return valid_actions
            # --- NEW: Handle Targeting/Sacrifice/Choice Phases ---
            if gs.phase == gs.PHASE_TARGETING:
                 if gs.targeting_context and gs.targeting_context.get("controller") == current_player:
                     self._add_targeting_actions(current_player, valid_actions, set_valid_action)
                 else: # Waiting for opponent target choice
                     set_valid_action(224, "NO_OP (Waiting for opponent targeting)")
                 self.action_reasons = action_reasons
                 return valid_actions
            if gs.phase == gs.PHASE_SACRIFICE:
                if gs.sacrifice_context and gs.sacrifice_context.get("controller") == current_player:
                    self._add_sacrifice_actions(current_player, valid_actions, set_valid_action)
                else: # Waiting for opponent sacrifice choice
                    set_valid_action(224, "NO_OP (Waiting for opponent sacrifice)")
                self.action_reasons = action_reasons
                return valid_actions
            if gs.phase == gs.PHASE_CHOOSE:
                 if gs.choice_context and gs.choice_context.get("player") == current_player:
                      self._add_special_choice_actions(current_player, valid_actions, set_valid_action)
                 else: # Waiting for opponent choice
                      set_valid_action(224, "NO_OP (Waiting for opponent choice)")
                 self.action_reasons = action_reasons
                 return valid_actions

            # --- Rest of the logic (assuming it was mostly correct before) ---
            # --- Always Available ---
            set_valid_action(11, "PASS_PRIORITY is always possible")
            set_valid_action(12, "CONCEDE is always possible")

            # --- Timing-Based Actions ---
            can_act_sorcery_speed = False
            # Check priority player directly from GameState
            has_priority = (gs.priority_player == current_player)
            can_act_instant_speed = has_priority # Base check

            if is_my_turn and gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and not gs.stack and has_priority:
                can_act_sorcery_speed = True
            # Prevent actions during untap/cleanup (except triggered?)
            if gs.phase in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP]:
                can_act_instant_speed = False # Unless responding to triggered abilities in cleanup

            # Prevent actions if Split Second is active
            if getattr(gs, 'split_second_active', False):
                 can_act_instant_speed = False
                 can_act_sorcery_speed = False
                 logging.debug("Split Second active, limiting actions.")


            if can_act_sorcery_speed:
                self._add_sorcery_speed_actions(current_player, opponent, valid_actions, set_valid_action)
            if can_act_instant_speed:
                self._add_instant_speed_actions(current_player, opponent, valid_actions, set_valid_action)
            # --- Phase-Specific Actions ---
            if has_priority: # Only add phase actions if player has priority
                # Add basic phase progression actions if priority allows
                self._add_basic_phase_actions(is_my_turn, valid_actions, set_valid_action)

                if is_my_turn and gs.phase == gs.PHASE_DECLARE_ATTACKERS:
                    self._add_attack_declaration_actions(current_player, opponent, valid_actions, set_valid_action)
                # Blocker (non-active) only gets priority *after* attackers declared
                elif not is_my_turn and gs.phase == gs.PHASE_DECLARE_BLOCKERS and gs.current_attackers:
                    self._add_block_declaration_actions(current_player, valid_actions, set_valid_action)
                # Add damage assignment actions
                elif gs.phase in [gs.PHASE_FIRST_STRIKE_DAMAGE, gs.PHASE_COMBAT_DAMAGE] and not gs.combat_damage_dealt:
                    self._add_combat_damage_actions(current_player, valid_actions, set_valid_action)

            # --- Final ---
            self.action_reasons = action_reasons
            valid_count = np.sum(valid_actions)
            if valid_count == 0: # Should only happen if only concede is possible
                 set_valid_action(12, "FALLBACK - CONCEDE")
            elif valid_count == 1 and valid_actions[12]: # Only concede is possible
                 pass # Okay state
            elif valid_count == 2 and valid_actions[11] and valid_actions[12]: # Only pass/concede
                 # Automatically pass if stack empty and is my turn main phase (already done sorcery actions)
                 if not gs.stack and is_my_turn and gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]:
                      set_valid_action(3, "Auto Pass Main Phase") # Add phase end action
                 elif not gs.stack: # Auto pass if nothing to do
                      pass # Agent should pass anyway

            return valid_actions

        except Exception as e:
            # ... (Error handling assumed correct) ...
            fallback_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
            fallback_actions[11] = True
            fallback_actions[12] = True
            self.action_reasons = {11: "Critical Error Fallback", 12: "Critical Error Fallback"}
            return fallback_actions
    
    def _add_combat_damage_actions(self, player, valid_actions, set_valid_action):
         """Adds actions for assigning combat damage order if needed."""
         gs = self.game_state
         # Check if damage assignment order is needed (multiple blockers)
         needs_order_assignment = False
         for attacker_id, blockers in gs.current_block_assignments.items():
             if len(blockers) > 1:
                 attacker_card = gs._safe_get_card(attacker_id)
                 if attacker_card and hasattr(attacker_card, 'power') and attacker_card.power > 0:
                     needs_order_assignment = True
                     break
         if needs_order_assignment:
              # Allow action to confirm the damage assignment order (FIRST_STRIKE_ORDER)
              # This action (430) currently requires context for the assignments.
              # Agent needs to build this context.
              set_valid_action(430, "Assign Combat Damage Order")

         # Allow action to finalize damage resolution (ASSIGN_COMBAT_DAMAGE)
         # This action (431) allows manual override or triggers auto-resolve.
         set_valid_action(431, "Resolve Combat Damage")
        
    def _add_sorcery_speed_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions performable only at sorcery speed."""
        gs = self.game_state
        # Play Land
        if not player.get("land_played", False): # Use .get for safety
            for i in range(min(len(player["hand"]), 7)): # Hand index 0-6
                try:
                    card_id = player["hand"][i]
                    card = gs._safe_get_card(card_id)
                    if card and 'land' in getattr(card, 'type_line', '').lower():
                        set_valid_action(13 + i, f"PLAY_LAND {card.name}")
                        # MDFC Land Back
                        # Check back_face safely
                        back_face_data = getattr(card, 'back_face', None)
                        if hasattr(card, 'is_mdfc') and card.is_mdfc() and back_face_data and 'land' in back_face_data.get('type_line','').lower():
                            set_valid_action(180 + i, f"PLAY_MDFC_LAND_BACK {back_face_data.get('name', 'Unknown')}")
                except IndexError:
                    logging.warning(f"IndexError accessing hand for PLAY_LAND at index {i}")
                    break # Stop if index is out of bounds

        # Play Sorcery-speed Spells (Sorceries, Creatures, Artifacts, Enchantments, Planeswalkers)
        for i in range(min(len(player["hand"]), 8)): # Hand index 0-7
            try:
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                # Additional safety checks for card attributes
                if not card or not hasattr(card, 'type_line') or not hasattr(card, 'card_types'):
                    continue

                type_line_lower = getattr(card, 'type_line', '').lower()
                card_types_list = getattr(card, 'card_types', [])

                if 'land' not in type_line_lower and not ('instant' in card_types_list or self._has_flash(card_id)):
                    # Prepare context for can_afford and target checks
                    context_check = {'kicked': False, 'pay_additional': False} # Minimal context for check
                    if self._can_afford_card(player, card, context=context_check):
                        if self._targets_available(card, player, opponent): # Check if required targets exist
                            set_valid_action(20 + i, f"PLAY_SPELL {card.name}")

                            # MDFC Spell Back (Sorcery)
                            back_face_data = getattr(card, 'back_face', None)
                            if hasattr(card, 'is_mdfc') and card.is_mdfc() and back_face_data:
                                back_type_line = back_face_data.get('type_line','').lower()
                                back_types = back_face_data.get('card_types', [])
                                if 'land' not in back_type_line and 'instant' not in back_types:
                                    if self._can_afford_card(player, back_face_data, is_back_face=True, context=context_check):
                                         # Check back face targets availability too
                                        if self._targets_available_from_data(back_face_data, player, opponent):
                                            set_valid_action(188 + i, f"PLAY_MDFC_BACK {back_face_data.get('name', 'Unknown')}")

                            # Adventure (Sorcery)
                            if hasattr(card, 'has_adventure') and card.has_adventure():
                                adv_data = card.get_adventure_data()
                                if adv_data and 'sorcery' in adv_data.get('type','').lower():
                                    if self._can_afford_cost_string(player, adv_data.get('cost',''), context=context_check):
                                        # Check adventure targets
                                        if self._targets_available_from_text(adv_data.get('effect',''), player, opponent):
                                             set_valid_action(196 + i, f"PLAY_ADVENTURE {adv_data.get('name', 'Unknown')}")
            except IndexError:
                 logging.warning(f"IndexError accessing hand for PLAY_SPELL at index {i}")
                 break # Stop if index is out of bounds

        # Activate Sorcery-speed Abilities
        self._add_ability_activation_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)
        # Note: PW abilities are sorcery speed by default, handled by _add_planeswalker_actions
        if hasattr(self, 'combat_handler') and self.combat_handler:
            self.combat_handler._add_planeswalker_actions(player, valid_actions, set_valid_action)

        # Other Sorcery-speed Actions
        self._add_level_up_actions(player, valid_actions, set_valid_action)
        self._add_unlock_door_actions(player, valid_actions, set_valid_action)
        self._add_equip_aura_actions(player, valid_actions, set_valid_action) # Includes Equip, Fortify, Reconfigure
        self._add_morph_actions(player, valid_actions, set_valid_action)
        self._add_alternative_casting_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)
        self._add_special_mechanics_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True) # For Foretell, Suspend activation etc.
        
    def _add_instant_speed_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions performable at instant speed."""
        gs = self.game_state
        # Play Instant/Flash Spells
        for i in range(min(len(player["hand"]), 8)): # Hand index 0-7
            try:
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'type_line') or not hasattr(card, 'card_types'):
                     continue

                # Minimal context for check
                context_check = {'kicked': False, 'pay_additional': False}
                card_types_list = getattr(card, 'card_types', [])

                if 'instant' in card_types_list or self._has_flash(card_id):
                    if 'land' not in getattr(card, 'type_line', '').lower(): # Exclude lands with flash (handled elsewhere)
                        if self._can_afford_card(player, card, context=context_check):
                            if self._targets_available(card, player, opponent): # Check if required targets exist
                                set_valid_action(20 + i, f"PLAY_SPELL (Instant) {card.name}")

                                # MDFC Spell Back (Instant)
                                back_face_data = getattr(card, 'back_face', None)
                                if hasattr(card, 'is_mdfc') and card.is_mdfc() and back_face_data:
                                    back_type_line = back_face_data.get('type_line','').lower()
                                    back_types = back_face_data.get('card_types', [])
                                    # Check flash text on back face too
                                    has_back_flash = self._has_flash_text(back_face_data.get('oracle_text',''))
                                    if 'land' not in back_type_line and ('instant' in back_types or has_back_flash):
                                        if self._can_afford_card(player, back_face_data, is_back_face=True, context=context_check):
                                            if self._targets_available_from_data(back_face_data, player, opponent):
                                                set_valid_action(188 + i, f"PLAY_MDFC_BACK (Instant) {back_face_data.get('name', 'Unknown')}")

                                # Adventure (Instant)
                                if hasattr(card, 'has_adventure') and card.has_adventure():
                                    adv_data = card.get_adventure_data()
                                    if adv_data and 'instant' in adv_data.get('type','').lower():
                                        if self._can_afford_cost_string(player, adv_data.get('cost',''), context=context_check):
                                            if self._targets_available_from_text(adv_data.get('effect',''), player, opponent):
                                                set_valid_action(196 + i, f"PLAY_ADVENTURE (Instant) {adv_data.get('name', 'Unknown')}")
            except IndexError:
                 logging.warning(f"IndexError accessing hand for Instant/Flash spell at index {i}")
                 break # Stop if index is out of bounds

        # Activate Instant-speed Abilities
        self._add_ability_activation_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)

        # Tap Lands for Mana
        self._add_land_tapping_actions(player, valid_actions, set_valid_action)

        # Alternative Casting (Instant Speed)
        self._add_alternative_casting_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)

        # Cycling
        self._add_cycling_actions(player, valid_actions, set_valid_action)

        # Response Actions if stack is not empty
        if gs.stack:
            self._add_response_actions(player, valid_actions, set_valid_action)

        # Other instant-speed mechanics
        self._add_special_mechanics_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False) # For Boast, Unearth activation etc.
        
    def _targets_available_from_data(self, card_data, caster, opponent):
        """Check target availability from card data dict."""
        gs = self.game_state
        oracle_text = card_data.get('oracle_text', '').lower()
        if 'target' not in oracle_text: return True
        card_id = card_data.get('id') # Need ID
        if not card_id: return True # Cannot check without ID

        if hasattr(gs, 'targeting_system') and gs.targeting_system:
            try:
                valid_targets = gs.targeting_system.get_valid_targets(card_id, caster)
                return any(targets for targets in valid_targets.values())
            except Exception as e: return True
        else: return True
        
    def _targets_available_from_text(self, effect_text, caster, opponent):
        """Check target availability just from effect text (less precise)."""
        gs = self.game_state
        if 'target' not in effect_text.lower(): return True
        # Basic heuristic: check if *any* creatures or players exist
        if 'target creature' in effect_text.lower():
             if len(caster.get("battlefield",[])) > 0 or len(opponent.get("battlefield",[])) > 0: return True
        if 'target player' in effect_text.lower(): return True
        if 'target opponent' in effect_text.lower(): return True
        # ... add more basic checks
        return True # Assume available if check is simple

    def _add_attack_declaration_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions specific to the Declare Attackers step."""
        gs = self.game_state
        # Declare Attackers
        possible_attackers = []
        for i in range(min(len(player["battlefield"]), 20)):
            try:
                card_id = player["battlefield"][i]
                if self.is_valid_attacker(card_id):
                     card = gs._safe_get_card(card_id)
                     set_valid_action(28 + i, f"ATTACK with {getattr(card, 'name', 'Unknown')}")
                     possible_attackers.append((i, card_id)) # Store index and ID
            except IndexError:
                 logging.warning(f"IndexError accessing battlefield for ATTACK at index {i}")
                 break # Stop if index is out of bounds

        # Declare targets for attackers (if applicable)
        if possible_attackers:
             self._add_attack_target_actions(player, opponent, valid_actions, set_valid_action, possible_attackers)

        # Always allow finishing declaration
        set_valid_action(433, "Finish Declaring Attackers")


    def _add_block_declaration_actions(self, player, valid_actions, set_valid_action):
         """Adds actions specific to the Declare Blockers step."""
         gs = self.game_state
         if gs.current_attackers: # Only allow blocking if there are attackers
            # Declare Blockers
            possible_blockers = []
            for i in range(min(len(player["battlefield"]), 20)): # 'player' is the blocker now
                try:
                    card_id = player["battlefield"][i]
                    card = gs._safe_get_card(card_id)
                    if not card: continue

                    # Basic creature & untap check
                    if 'creature' not in getattr(card, 'card_types', []) or card_id in player.get("tapped_permanents", set()):
                        continue

                    # Check if this creature can block any current attacker
                    can_block_anything = False
                    for attacker_id in gs.current_attackers:
                        if self._can_block(card_id, attacker_id):
                            can_block_anything = True
                            break
                    if can_block_anything:
                        # Check current block assignment - allows toggling
                        is_currently_blocking = any(card_id in blockers for blockers in gs.current_block_assignments.values())
                        action_text = "BLOCK" if not is_currently_blocking else "UNASSIGN BLOCK"
                        set_valid_action(48 + i, f"{action_text} with {card.name}")
                        possible_blockers.append((i, card_id)) # Store index and ID
                except IndexError:
                     logging.warning(f"IndexError accessing battlefield for BLOCK at index {i}")
                     break # Stop if index is out of bounds

            # Assign multiple blockers
            # Enable this only if we have at least 2 possible blockers overall
            if len(possible_blockers) >= 2:
                 # Enable ASSIGN_MULTIPLE_BLOCKERS for each attacker (up to 10)
                 for atk_idx, attacker_id in enumerate(gs.current_attackers[:10]):
                     attacker_card = gs._safe_get_card(attacker_id)
                     attacker_name = attacker_card.name if attacker_card else f"Attacker {atk_idx}"
                     # Check if at least 2 possible blockers *can* block *this* attacker
                     valid_multi_blockers_for_attacker = [b_id for _, b_id in possible_blockers if self._can_block(b_id, attacker_id)]
                     if len(valid_multi_blockers_for_attacker) >= 2:
                         set_valid_action(383 + atk_idx, f"ASSIGN_MULTIPLE_BLOCKERS to {attacker_name}")

            # Always allow finishing block declaration
            set_valid_action(434, "Finish Declaring Blockers")
                     
    
    def _add_special_choice_actions(self, player, valid_actions, set_valid_action):
        """Add actions for Scry, Surveil, Dredge, Choose Mode, Choose X, Choose Color."""
        gs = self.game_state
        # Scry
        if hasattr(gs, 'scry_in_progress') and gs.scry_in_progress and gs.scrying_player == player:
            if gs.scrying_cards:
                card_id = gs.scrying_cards[0]
                card = gs._safe_get_card(card_id)
                card_name = card.name if card else card_id
                set_valid_action(306, f"PUT_ON_TOP {card_name}")
                set_valid_action(307, f"PUT_ON_BOTTOM {card_name}")
        # Surveil
        elif hasattr(gs, 'surveil_in_progress') and gs.surveil_in_progress and gs.surveiling_player == player:
             if gs.cards_being_surveiled:
                 card_id = gs.cards_being_surveiled[0]
                 card = gs._safe_get_card(card_id)
                 card_name = card.name if card else card_id
                 set_valid_action(305, f"PUT_TO_GRAVEYARD {card_name}")
                 set_valid_action(306, f"PUT_ON_TOP {card_name}")
        # Dredge (Needs integration with Draw replacement)
        # If a draw is being replaced by dredge, allow dredge action
        if hasattr(gs, 'dredge_pending') and gs.dredge_pending['player'] == player:
            card_id = gs.dredge_pending['card_id']
            dredge_val = gs.dredge_pending['value']
            if len(player["library"]) >= dredge_val:
                 # Find card index in graveyard
                 gy_idx = -1
                 for idx, gy_id in enumerate(player["graveyard"]):
                      if gy_id == card_id and idx < 6: # GY Index 0-5
                           gy_idx = idx
                           break
                 if gy_idx != -1:
                     set_valid_action(308, f"DREDGE {gs._safe_get_card(card_id).name}") # Param = gy_idx

        # Choose Mode/X/Color for spell/ability on stack
        if gs.stack:
            top_item = gs.stack[-1]
            if isinstance(top_item, tuple) and len(top_item) >= 3 and top_item[2] == player:
                 stack_type, card_id, controller = top_item[:3]
                 card = gs._safe_get_card(card_id)
                 context = top_item[3] if len(top_item) > 3 else {}
                 if card and hasattr(card, 'oracle_text'):
                     text = card.oracle_text.lower()
                     # Choose Mode
                     if "choose one" in text or "choose two" in text or "choose up to" in text:
                          num_modes = len(re.findall(r'[•\-−–—]', text))
                          for i in range(min(num_modes, 10)): # Mode index 0-9
                               set_valid_action(348 + i, f"CHOOSE_MODE {i+1}")
                     # Choose X
                     if hasattr(card, 'mana_cost') and 'X' in card.mana_cost and "X" not in context:
                         available_mana = sum(player["mana_pool"].values())
                         for i in range(min(available_mana, 10)): # X value 1-10
                              set_valid_action(358 + i, f"CHOOSE_X_VALUE {i+1}")
                     # Choose Color
                     if "choose a color" in text:
                          for i in range(5): # Color index 0-4 (WUBRG)
                               set_valid_action(368 + i, f"CHOOSE_COLOR {['W','U','B','R','G'][i]}")
        
    def _add_sacrifice_actions(self, player, valid_actions, set_valid_action):
         """Add SACRIFICE_PERMANENT actions when in the sacrifice phase."""
         gs = self.game_state
         if hasattr(gs, 'sacrifice_context') and gs.sacrifice_context:
             context = gs.sacrifice_context
             source_id = context.get('source_id')
             source_card = gs._safe_get_card(source_id)
             source_name = source_card.name if source_card and hasattr(source_card, 'name') else source_id
             required_count = context.get('required_count', 1)
             selected_count = len(context.get('selected_permanents', []))

             # Determine valid permanents to sacrifice based on context (e.g., 'creature', 'artifact')
             permanent_type_req = context.get('required_type')
             valid_permanents = []
             for i, perm_id in enumerate(player["battlefield"]):
                  perm_card = gs._safe_get_card(perm_id)
                  if not perm_card: continue
                  if not permanent_type_req or permanent_type_req in getattr(perm_card, 'card_types', []):
                       valid_permanents.append(perm_id)

             # Generate SACRIFICE_PERMANENT actions
             if selected_count < required_count:
                 for i, perm_id in enumerate(valid_permanents):
                     if i >= 10: break # Limit to action space indices 284-293
                     perm_card = gs._safe_get_card(perm_id)
                     perm_name = perm_card.name if perm_card and hasattr(perm_card, 'name') else perm_id
                     set_valid_action(284 + i, f"SACRIFICE ({i}): {perm_name} for {source_name}")
             else:
                  set_valid_action(11, "PASS_PRIORITY (Sacrifices selected)")

    
    def _add_targeting_actions(self, player, valid_actions, set_valid_action):
        """Add SELECT_TARGET actions when in the targeting phase."""
        gs = self.game_state
        if hasattr(gs, 'targeting_context') and gs.targeting_context:
            context = gs.targeting_context
            source_id = context.get('source_id')
            source_card = gs._safe_get_card(source_id)
            source_name = source_card.name if source_card and hasattr(source_card, 'name') else source_id
            target_type = context.get('required_type', 'target') # e.g., 'creature', 'player'
            required_count = context.get('required_count', 1)
            selected_count = len(context.get('selected_targets', []))

            # Get valid targets using TargetingSystem if possible
            valid_targets_map = {}
            if gs.targeting_system:
                valid_targets_map = gs.targeting_system.get_valid_targets(source_id, player, target_type)
            else:
                # Fallback: Add basic logic here or assume it's handled by agent
                logging.warning("Targeting system not available, cannot generate specific targeting actions.")
                pass # Need a fallback

            # Flatten the valid targets map into a list
            valid_targets_list = []
            for category, targets in valid_targets_map.items():
                valid_targets_list.extend(targets)

            # Generate SELECT_TARGET actions for available targets
            if selected_count < required_count:
                for i, target_id in enumerate(valid_targets_list):
                    if i >= 10: break # Limit to action space indices 274-283
                    target_card = gs._safe_get_card(target_id)
                    target_name = target_card.name if target_card and hasattr(target_card, 'name') else target_id
                    if isinstance(target_id, str) and target_id in ["p1", "p2"]: # Handle player targets
                         target_name = "Player 1" if target_id == "p1" else "Player 2"
                    set_valid_action(274 + i, f"SELECT_TARGET ({i}): {target_name} for {source_name}")
            else:
                # If enough targets are selected, allow passing priority
                set_valid_action(11, "PASS_PRIORITY (Targets selected)")
        
    def _add_level_up_actions(self, player, valid_actions, set_valid_action):
        """Add actions for leveling up Class cards."""
        gs = self.game_state
        for i in range(min(len(player["battlefield"]), 5)): # Class index 0-4
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_class') and card.is_class and hasattr(card, 'can_level_up') and card.can_level_up():
                 next_level = card.current_level + 1
                 cost = card.get_level_cost(next_level)
                 if self._can_afford_cost_string(player, cost):
                     set_valid_action(253 + i, f"LEVEL_UP_CLASS {card.name} to {next_level}")
                     
    def _add_unlock_door_actions(self, player, valid_actions, set_valid_action):
        """Add actions for unlocking Room doors."""
        gs = self.game_state
        for i in range(min(len(player["battlefield"]), 5)): # Room index 0-4
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_room') and card.is_room:
                 if hasattr(card, 'door2') and not card.door2.get('unlocked', False):
                     cost = card.door2.get('mana_cost', '')
                     if self._can_afford_cost_string(player, cost):
                         set_valid_action(248 + i, f"UNLOCK_DOOR {card.name}")
                         
    def _add_equip_actions(self, player, valid_actions, set_valid_action):
        gs = self.game_state
        # Identify creatures and equipment indices on player's battlefield
        creature_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                             if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])]
        equipment_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                              if gs._safe_get_card(cid) and 'equipment' in getattr(gs._safe_get_card(cid), 'subtypes', [])]

        action_map = {} # Store unique (type, param) to action index

        for eq_idx, equip_id in equipment_indices:
            if eq_idx >= 10: continue # Action space limit for source? Maybe rethink this index mapping.
            equip_card = gs._safe_get_card(equip_id)
            cost_match = re.search(r"equip (\{[^\}]+\}|[0-9]+)", getattr(equip_card, 'oracle_text', '').lower())
            if cost_match:
                 cost_str = cost_match.group(1)
                 if cost_str.isdigit(): cost_str = f"{{{cost_str}}}" # Normalize cost
                 if self._can_afford_cost_string(player, cost_str):
                      # Allow equipping to each creature
                      for c_idx, creature_id in creature_indices:
                           # Map (eq_idx, c_idx) to a unique action index if needed, or use tuple param directly.
                           # Let's assume EQUIP action uses tuple param (equip_idx, creature_idx)
                           param_tuple = (eq_idx, c_idx)
                           # Need a way to map this tuple back to a *single* action index like 445.
                           # Current ACTION_MEANINGS for 445 doesn't support this complex param well.
                           # Compromise: Use action 445, but the handler expects a tuple passed via context.
                           # Agent needs to provide this context. Set action as valid, assuming agent handles context.
                           set_valid_action(445, f"EQUIP {equip_card.name} to {gs._safe_get_card(creature_id).name}")

            # Reconfigure
            if "reconfigure" in getattr(equip_card, 'oracle_text', '').lower():
                cost_match = re.search(r"reconfigure (\{[^\}]+\}|[0-9]+)", getattr(equip_card, 'oracle_text', '').lower())
                if cost_match:
                    cost_str = cost_match.group(1)
                    if cost_str.isdigit(): cost_str = f"{{{cost_str}}}"
                    if self._can_afford_cost_string(player, cost_str):
                         # Reconfigure needs the equip_idx. Action 449 assumes this.
                         set_valid_action(449, f"RECONFIGURE {equip_card.name}") # Param = eq_idx

        # Unequip
        if hasattr(player, "attachments"):
            for equip_id, target_id in player["attachments"].items():
                equip_card = gs._safe_get_card(equip_id)
                if equip_card and 'equipment' in getattr(equip_card, 'subtypes', []):
                    # Find index of equipment on battlefield
                    eq_idx = -1
                    for i, cid in enumerate(player["battlefield"]):
                        if cid == equip_id:
                            eq_idx = i
                            break
                    if eq_idx != -1:
                         set_valid_action(446, f"UNEQUIP {equip_card.name}") # Param = eq_idx


    def _add_morph_actions(self, player, valid_actions, set_valid_action):
         """Add actions for turning Morph/Manifest cards face up."""
         gs = self.game_state
         for i in range(min(len(player["battlefield"]), 20)):
             card_id = player["battlefield"][i]
             card = gs._safe_get_card(card_id)
             # Check Morph
             if card and hasattr(card, 'oracle_text') and "morph" in card.oracle_text.lower() and getattr(gs.morphed_cards.get(card_id, {}), 'face_down', False):
                 cost_match = re.search(r"morph (\{[^\}]+\})", card.oracle_text.lower())
                 if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                     set_valid_action(450, f"MORPH {gs.morphed_cards[card_id]['original']['name']}") # Param = battlefield index i
             # Check Manifest
             elif card and hasattr(gs, 'manifested_cards') and card_id in gs.manifested_cards:
                 original_card = gs.manifested_cards[card_id]['original']
                 if hasattr(original_card, 'mana_cost') and self._can_afford_card(player, original_card):
                     set_valid_action(451, f"MANIFEST {original_card.name}") # Param = battlefield index i
        
    def _add_attack_target_actions(self, player, opponent, valid_actions, set_valid_action, possible_attackers):
        """Add actions for choosing targets for attackers (Planeswalkers, Battles)."""
        gs = self.game_state
        # Attacker ID needs to be associated with the target choice.
        # Current approach assumes the *last declared attacker* is the one choosing target.

        # Planeswalkers
        opponent_planeswalkers = [(idx, card_id) for idx, card_id in enumerate(opponent["battlefield"])
                                   if gs._safe_get_card(card_id) and 'planeswalker' in getattr(gs._safe_get_card(card_id), 'card_types', [])]
        for i in range(min(len(opponent_planeswalkers), 5)): # PW index 0-4
            pw_idx, pw_id = opponent_planeswalkers[i]
            pw_card = gs._safe_get_card(pw_id)
            # Action 373-377 assume param is the PW index (0-4)
            set_valid_action(373 + i, f"ATTACK_PLANESWALKER {pw_card.name}") # Param = i

        # Battles
        opponent_battles = [(idx, card_id) for idx, card_id in enumerate(opponent["battlefield"])
                             if gs._safe_get_card(card_id) and 'battle' in getattr(gs._safe_get_card(card_id), 'type_line', '')]
        for battle_idx_rel, (abs_idx, battle_id) in enumerate(opponent_battles):
            if battle_idx_rel >= 5: break # Battle index 0-4 relative to available battles
            battle_card = gs._safe_get_card(battle_id)
            # ACTION_MEANINGS has a complex mapping (battle_idx * 4 + creature_idx)
            # This needs rework. Simplify: Use actions 460-464 to target battle 0-4.
            # The handler needs to associate the *last declared attacker* with this battle target.
            set_valid_action(460 + battle_idx_rel, f"ATTACK_BATTLE {battle_card.name}") # Param = battle_idx_rel
        
    def _add_response_actions(self, player, valid_actions, set_valid_action):
        """Add actions for responding to stack (counters, etc.)."""
        gs = self.game_state
        if not gs.stack: return

        stack_has_opponent_spell = any(isinstance(item, tuple) and item[0] == "SPELL" and item[2] != player for item in gs.stack)
        stack_has_opponent_ability = any(isinstance(item, tuple) and item[0] == "ABILITY" and item[2] != player for item in gs.stack)

        # Counter Spell
        if stack_has_opponent_spell:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "counter target spell" in card.oracle_text.lower():
                     if self._can_afford_card(player, card):
                         set_valid_action(425, f"COUNTER_SPELL with {card.name}") # Param = (hand_idx, stack_idx)

        # Counter Ability
        if stack_has_opponent_ability:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and ("counter target ability" in card.oracle_text.lower() or "counter target activated ability" in card.oracle_text.lower()):
                     if self._can_afford_card(player, card):
                         set_valid_action(426, f"COUNTER_ABILITY with {card.name}") # Param = (hand_idx, stack_idx)

        # Prevent Damage
        # Check if a damage spell/ability is on stack or if combat damage is pending
        damage_pending = gs.phase in [gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE] or \
                         any(isinstance(item, tuple) and "damage" in getattr(gs._safe_get_card(item[1]), 'oracle_text', '').lower() for item in gs.stack)
        if damage_pending:
             for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "prevent" in card.oracle_text.lower() and "damage" in card.oracle_text.lower():
                     if self._can_afford_card(player, card):
                         set_valid_action(427, f"PREVENT_DAMAGE with {card.name}") # Param = (hand_idx, source_idx?)

        # Stifle Trigger (More complex - needs trigger stack)
        # For now, enable if a triggered ability is on stack
        stack_has_trigger = any(isinstance(item, tuple) and item[0] == "TRIGGER" for item in gs.stack)
        if stack_has_trigger:
             for i in range(min(len(player["hand"]), 8)):
                 card_id = player["hand"][i]
                 card = gs._safe_get_card(card_id)
                 if card and hasattr(card, 'oracle_text') and "counter target triggered ability" in card.oracle_text.lower():
                      if self._can_afford_card(player, card):
                          set_valid_action(429, f"STIFLE_TRIGGER with {card.name}") # Param = (hand_idx, stack_idx)
        
    def _add_cycling_actions(self, player, valid_actions, set_valid_action):
        """Add cycling actions."""
        gs = self.game_state
        for i in range(min(len(player["hand"]), 8)): # Hand index 0-7
            card_id = player["hand"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "cycling" in card.oracle_text.lower():
                cost_match = re.search(r"cycling (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                if cost_match:
                     cost_str = cost_match.group(1)
                     # Normalize cost string if it's just a number
                     if cost_str.isdigit(): cost_str = f"{{{cost_str}}}"
                     if self._can_afford_cost_string(player, cost_str):
                          set_valid_action(422, f"CYCLING {card.name}") # Param needs hand index `i`

    def _add_room_door_actions(self, player, valid_actions, set_valid_action, is_sorcery_timing):
        """Add actions for Room doors, only at sorcery speed."""
        # Rooms can only be accessed at sorcery speed
        if not is_sorcery_timing:
            return
            
        gs = self.game_state
        for idx, card_id in enumerate(player["battlefield"]):
            if idx >= 5:  # Limit to first 5 Room cards
                break
                
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'is_room') or not card.is_room:
                continue
                
            # Check if door2 is locked (door1 is automatically unlocked when played)
            if hasattr(card, 'door2') and not card.door2.get('unlocked', False):
                door_cost = card.door2.get('mana_cost', '')
                
                # Check if we can afford the door cost
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, door_cost)
                else:
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                if can_afford:
                    set_valid_action(248 + idx, f"UNLOCK_DOOR for {card.name}")

    def _add_class_level_actions(self, player, valid_actions, set_valid_action, is_sorcery_timing):
        """Add actions for Class level-ups, strictly at sorcery speed."""
        # Class level-ups can only be done at sorcery speed
        if not is_sorcery_timing:
            return
            
        gs = self.game_state
        for idx, card_id in enumerate(player["battlefield"]):
            if idx >= 5:  # Limit to 5 Class cards
                break
                
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'is_class') or not card.is_class:
                continue
                
            # Check if class can level up further
            if not hasattr(card, 'can_level_up') or not card.can_level_up():
                continue
                
            # Get cost for next level
            next_level = card.current_level + 1
            level_cost = card.get_level_cost(next_level)
            
            # Only allow leveling if current level supports it
            if next_level == 2 or (next_level == 3 and card.current_level == 2):
                # Check if we can afford the level cost
                can_afford = False
                if hasattr(gs, 'mana_system') and level_cost:
                    can_afford = gs.mana_system.can_pay_mana_cost(player, level_cost)
                else:
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                if can_afford:
                    set_valid_action(253 + idx, f"LEVEL_UP_CLASS to level {next_level} for {card.name}")
                    
    def _add_counter_management_actions(self, player, valid_actions, set_valid_action):
        """Add actions for counter management based on current game context."""
        gs = self.game_state
        
        # Only show counter management actions if we're in an appropriate context
        context_requires_counter_action = False
        counter_type = None
        action_type = None
        
        # Check if there's a spell or ability on the stack that requires counter placement
        if gs.stack and len(gs.stack) > 0:
            stack_item = gs.stack[-1]
            
            if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                stack_type, card_id, controller = stack_item[:3]
                if controller == player:  # Only process if it's this player's spell/ability
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'oracle_text'):
                        oracle_text = card.oracle_text.lower()
                        
                        # Check for various counter patterns
                        if "put a +1/+1 counter" in oracle_text or "place a +1/+1 counter" in oracle_text:
                            context_requires_counter_action = True
                            counter_type = "+1/+1"
                            action_type = "ADD_COUNTER"
                        elif "put a -1/-1 counter" in oracle_text or "place a -1/-1 counter" in oracle_text:
                            context_requires_counter_action = True
                            counter_type = "-1/-1"
                            action_type = "ADD_COUNTER"
                        elif "remove a counter" in oracle_text:
                            context_requires_counter_action = True
                            action_type = "REMOVE_COUNTER"
                        elif "proliferate" in oracle_text:
                            context_requires_counter_action = True
                            action_type = "PROLIFERATE"
        
        # If we need to show counter actions, add them based on the context
        if context_requires_counter_action:
            if action_type == "ADD_COUNTER":
                # Add actions for adding counters to permanents
                target_text = ""
                targets = []
                
                # Determine valid targets based on counter type
                if counter_type == "+1/+1":
                    # +1/+1 counters typically go on creatures
                    target_text = "creature"
                    targets = [cid for cid in player["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            'creature' in gs._safe_get_card(cid).card_types]
                elif counter_type == "-1/-1":
                    # -1/-1 counters typically go on opponent's creatures
                    opponent = gs.p2 if player == gs.p1 else gs.p1
                    target_text = "opponent's creature"
                    targets = [cid for cid in opponent["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            'creature' in gs._safe_get_card(cid).card_types]
                
                # Add action for each valid target
                for idx, target_id in enumerate(targets[:10]):  # Limit to 10 targets
                    card = gs._safe_get_card(target_id)
                    if card:
                        set_valid_action(309 + idx, 
                                        f"ADD_{counter_type}_COUNTER to {card.name}")
            
            elif action_type == "REMOVE_COUNTER":
                # Add actions for removing counters from permanents
                # First check player's permanents
                for idx, perm_id in enumerate(player["battlefield"][:10]):  # Limit to 10
                    card = gs._safe_get_card(perm_id)
                    if card and hasattr(card, 'counters') and card.counters:
                        # Show an action for each counter type on this permanent
                        for counter_type, count in card.counters.items():
                            if count > 0:
                                set_valid_action(319 + idx, 
                                            f"REMOVE_{counter_type}_COUNTER from {card.name}")
                                break  # Just one action per permanent for simplicity
            
            elif action_type == "PROLIFERATE":
                # Check if there are any permanents with counters to proliferate
                has_permanents_with_counters = False
                
                # Check player's permanents
                for perm_id in player["battlefield"]:
                    card = gs._safe_get_card(perm_id)
                    if card and hasattr(card, 'counters') and any(count > 0 for count in card.counters.values()):
                        has_permanents_with_counters = True
                        break
                
                # Check opponent's permanents
                if not has_permanents_with_counters:
                    opponent = gs.p2 if player == gs.p1 else gs.p1
                    for perm_id in opponent["battlefield"]:
                        card = gs._safe_get_card(perm_id)
                        if card and hasattr(card, 'counters') and any(count > 0 for count in card.counters.values()):
                            has_permanents_with_counters = True
                            break
                
                # Add proliferate action if there are targets
                if has_permanents_with_counters:
                    set_valid_action(329, "PROLIFERATE to add counters to all permanents with counters")
        
    def _check_valid_targets_exist(self, card, current_player, opponent):
        """
        Check if valid targets exist for a card requiring targets using the TargetingSystem.
        
        Args:
            card: The card being cast
            current_player: The player casting the spell
            opponent: The opponent player
            
        Returns:
            bool: Whether valid targets exist for the card
        """
        gs = self.game_state
        
        # Use TargetingSystem if available
        if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'targeting_system'):
            targeting_system = gs.ability_handler.targeting_system
            targets = targeting_system.resolve_targeting_for_spell(card.card_id, current_player)
            return targets is not None and any(targets.values())
        
        # Fallback to simple target existence check if no targeting system
        card_text = card.oracle_text.lower() if hasattr(card, 'oracle_text') else ""
        
        # Check for target creatures
        if 'target creature' in card_text:
            # Check for controller restrictions
            your_creatures_only = 'target creature you control' in card_text
            opponent_creatures_only = 'target creature an opponent controls' in card_text or 'target creature you don\'t control' in card_text
            
            for p in [current_player, opponent]:
                # Skip if targeting restrictions don't allow this player's creatures
                if your_creatures_only and p != current_player:
                    continue
                if opponent_creatures_only and p == current_player:
                    continue
                    
                for c_id in p["battlefield"]:
                    c = gs._safe_get_card(c_id)
                    if c and hasattr(c, 'card_types') and 'creature' in c.card_types:
                        # Check for protection, hexproof, shroud
                        if p != current_player and self._check_for_protection(c, card):
                            continue
                        return True
        
        # Check for target players
        elif 'target player' in card_text or 'target opponent' in card_text:
            return True
            
        # Check for target permanent with more specific type checking
        elif 'target permanent' in card_text:
            your_permanents_only = 'target permanent you control' in card_text
            opponent_permanents_only = 'target permanent an opponent controls' in card_text
            
            for p in [current_player, opponent]:
                if your_permanents_only and p != current_player:
                    continue
                if opponent_permanents_only and p == current_player:
                    continue
                    
                if p["battlefield"]:
                    for perm_id in p["battlefield"]:
                        perm = gs._safe_get_card(perm_id)
                        if perm and not self._check_for_protection(perm, card):
                            return True
        
        # Check for specific permanent types
        elif any(f'target {ptype}' in card_text for ptype in ['artifact', 'enchantment', 'land', 'planeswalker']):
            for ptype in ['artifact', 'enchantment', 'land', 'planeswalker']:
                if f'target {ptype}' in card_text:
                    for p in [current_player, opponent]:
                        for perm_id in p["battlefield"]:
                            perm = gs._safe_get_card(perm_id)
                            if perm and hasattr(perm, 'card_types') and ptype in perm.card_types:
                                # Check for controller restrictions
                                if f'target {ptype} you control' in card_text and p != current_player:
                                    continue
                                if f'target {ptype} an opponent controls' in card_text and p == current_player:
                                    continue
                                if not self._check_for_protection(perm, card):
                                    return True
        
        # Default to true if targeting requirements cannot be determined
        return 'target' not in card_text
    
    def _check_for_protection(self, target_card, source_card):
        """Check if target has protection, hexproof, or shroud with enhanced handling."""
        gs = self.game_state
        
        # Use targeting_system if available
        if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'targeting_system'):
            targeting_system = gs.ability_handler.targeting_system
            
            # Find controllers for context
            target_controller = None
            source_controller = None
            for player in [gs.p1, gs.p2]:
                for zone in ["battlefield", "hand", "graveyard", "exile"]:
                    if zone in player and target_card.card_id in player[zone]:
                        target_controller = player
                    if source_card and hasattr(source_card, 'card_id') and zone in player and source_card.card_id in player[zone]:
                        source_controller = player
            
            # Default if controllers can't be determined
            if not target_controller:
                target_controller = gs.p1
            if not source_controller:
                source_controller = gs.p2
                
            # Use comprehensive protection check
            if targeting_system._has_protection_from(target_card, source_card, target_controller, source_controller):
                return True
                
            # Check hexproof against opposing source
            if target_controller != source_controller and targeting_system._has_hexproof(target_card):
                return True
                
            # Check shroud against any source
            if targeting_system._has_shroud(target_card):
                return True
                
            return False
        
        # Fallback to basic check if targeting system not available
        # Protection
        if hasattr(target_card, 'oracle_text') and "protection from" in target_card.oracle_text.lower():
            # Check color protection
            if hasattr(source_card, 'colors'):
                for i, color in enumerate(['white', 'blue', 'black', 'red', 'green']):
                    if f"protection from {color}" in target_card.oracle_text.lower() and source_card.colors[i]:
                        return True
            # Check for protection from all
            if "protection from everything" in target_card.oracle_text.lower():
                return True
        
        # Hexproof
        if hasattr(target_card, 'oracle_text') and "hexproof" in target_card.oracle_text.lower():
            # Check if source is controlled by target controller (hexproof only affects opponents)
            return True  # For simplicity assume opponent is casting
        
        # Shroud
        if hasattr(target_card, 'oracle_text') and "shroud" in target_card.oracle_text.lower():
            return True
            
        return False
        
    def get_action_info(self, action_idx):
        """Get action type and parameter from action index."""
        if 0 <= action_idx < self.ACTION_SPACE_SIZE:
            return ACTION_MEANINGS.get(action_idx, ("INVALID", None))
        logging.error(f"Action index {action_idx} out of bounds (0-{self.ACTION_SPACE_SIZE-1})")
        return "INVALID", None
    

    def apply_action(self, action_idx, **kwargs): # Add **kwargs to accept context from env
        """
        Execute the action and get the next observation, reward and done status.
        Overhauled for clarity, correctness, and better reward shaping.
        """
        gs = self.game_state
        # Define player/opponent early for state access
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1

        # --- Initialization for the step ---
        reward = 0.0
        done = False
        truncated = False # Gymnasium API requires truncated flag
        pre_action_pattern = None # Initialize here
        info = {"action_mask": None, "game_result": "undetermined", "critical_error": False} # Default info

        # Regenerate action mask if not available (e.g., start of step)
        if not hasattr(self, 'current_valid_actions') or self.current_valid_actions is None or np.sum(self.current_valid_actions) == 0:
             # Ensure action_mask method exists and call it
             if hasattr(self, 'generate_valid_actions') and callable(self.generate_valid_actions):
                 self.current_valid_actions = self.generate_valid_actions()
             else:
                  # Fallback if action_mask generation fails
                  logging.error("Action mask generation method not found!")
                  self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                  self.current_valid_actions[11] = True # Pass
                  self.current_valid_actions[12] = True # Concede


        # *** Get context from environment/pending state ***
        action_context = kwargs.get('context', {})
        # Merge game state context if in a special phase
        if gs.phase == gs.PHASE_TARGETING and hasattr(gs, 'targeting_context'): action_context.update(gs.targeting_context or {})
        if gs.phase == gs.PHASE_SACRIFICE and hasattr(gs, 'sacrifice_context'): action_context.update(gs.sacrifice_context or {})
        if gs.phase == gs.PHASE_CHOOSE and hasattr(gs, 'choice_context'): action_context.update(gs.choice_context or {})

        # --- Main Action Application Logic ---
        try:
            # 1. Validate Action Index
            if not (0 <= action_idx < self.ACTION_SPACE_SIZE):
                logging.error(f"Action index {action_idx} is out of bounds (0-{self.ACTION_SPACE_SIZE-1}).")
                # Use safe observation getter
                obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {}
                info["action_mask"] = self.current_valid_actions.astype(bool) # Return current mask
                info["error_message"] = f"Action index out of bounds: {action_idx}"
                info["critical_error"] = True # Indicate a critical failure
                return obs, -0.5, False, False, info # Heavy penalty for invalid index

            # 2. Validate Against Action Mask
            if not self.current_valid_actions[action_idx]:
                invalid_reason = self.action_reasons.get(action_idx, 'Not Valid / Unknown Reason')
                valid_indices = np.where(self.current_valid_actions)[0]
                logging.warning(f"Invalid action {action_idx} selected (Action Mask False). Reason: [{invalid_reason}]. Valid: {valid_indices}")
                # --- Invalid Action Limit Handling (Assuming these attributes exist on self/env) ---
                # Simplified handling for now: penalize and continue if possible
                reward = -0.1 # Standard penalty
                obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {}
                info["action_mask"] = self.current_valid_actions.astype(bool)
                info["invalid_action_reason"] = invalid_reason
                return obs, reward, done, truncated, info

            # Reset invalid action counter if needed
            # self.invalid_action_count = 0

            # 3. Get Action Info
            action_type, param = self.get_action_info(action_idx)
            logging.info(f"Applying action: {action_idx} -> {action_type}({param}) with context: {action_context}")
            # Record action if tracking is implemented
            # self.current_episode_actions.append(action_idx)

            # 4. Store Pre-Action State for Reward Shaping
            prev_state = {
                "my_life": me["life"], "opp_life": opp["life"],
                "my_hand": len(me.get("hand", [])), "opp_hand": len(opp.get("hand", [])),
                "my_board": len(me.get("battlefield", [])), "opp_board": len(opp.get("battlefield", [])),
                "my_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in me.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                "opp_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in opp.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                # Add other relevant state like mana, counters etc. if needed
            }
            # Extract pre-action pattern if needed for strategy memory
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory:
                try:
                    pre_action_pattern = gs.strategy_memory.extract_strategy_pattern(gs)
                except Exception as e:
                    logging.error(f"Error extracting pre-action strategy pattern: {e}")

            # 5. Execute Action - Delegate to specific handlers
            handler_func = self.action_handlers.get(action_type)
            action_reward = 0.0
            action_executed = False

            if handler_func:
                try:
                    # Pass param, context, and action_type to the handler
                    result = handler_func(param=param, context=action_context, action_type=action_type)

                    # Process the result from the handler
                    if isinstance(result, tuple) and len(result) == 2: # (reward, success_flag)
                        action_reward, action_executed = result
                    elif isinstance(result, (float, int)): # Only reward (assume success)
                        action_reward = float(result); action_executed = True
                    elif isinstance(result, bool): # Only success flag
                        action_reward = 0.05 if result else -0.1; action_executed = result
                    else: # Assume success if handler returns None or other type
                        action_reward = 0.0; action_executed = True
                    if action_reward is None: action_reward = 0.0 # Ensure float

                # --- Error Handling during Handler Execution ---
                except TypeError as te:
                    # Try calling without context/action_type if TypeError suggests it
                    if "unexpected keyword argument 'context'" in str(te) or "unexpected keyword argument 'action_type'" in str(te):
                         try:
                            result = handler_func(param=param)
                            # Process result same as above...
                            if isinstance(result, tuple) and len(result) == 2: action_reward, action_executed = result
                            elif isinstance(result, (float, int)): action_reward, action_executed = float(result), True
                            elif isinstance(result, bool): action_reward, action_executed = (0.05, True) if result else (-0.1, False)
                            else: action_reward, action_executed = 0.0, True
                            if action_reward is None: action_reward = 0.0
                         except Exception as handler_e:
                             logging.error(f"Error executing handler {action_type} (param-only fallback call): {handler_e}", exc_info=True)
                             action_reward, action_executed = -0.2, False
                    else: # Other TypeError
                         logging.error(f"TypeError executing handler {action_type} with param {param} and context {action_context}: {te}", exc_info=True)
                         action_reward, action_executed = -0.2, False
                except Exception as handler_e:
                        logging.error(f"Error executing handler {action_type} with param {param} and context {action_context}: {handler_e}", exc_info=True)
                        action_reward, action_executed = -0.2, False
            else:
                logging.warning(f"No handler implemented for action type: {action_type}")
                action_reward = -0.05 # Small penalty for unimplemented action
                action_executed = False # Mark as not executed

            # Add action-specific reward to total step reward
            reward += action_reward

            # Check if action failed to execute properly
            if not action_executed:
                logging.warning(f"Action {action_type}({param}) failed to execute (Handler returned False).")
                obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {}
                info["action_mask"] = self.current_valid_actions.astype(bool) # Return current mask
                info["execution_failed"] = True
                return obs, reward, done, truncated, info # Return immediately on failure

            # 6. Process State-Based Actions and Stack Resolution
            if hasattr(gs, 'check_state_based_actions'):
                gs.check_state_based_actions()
            # Process triggered abilities *after* SBAs resulting from the action
            if hasattr(gs, 'ability_handler') and gs.ability_handler:
                 triggered = gs.ability_handler.process_triggered_abilities() # Process triggers that queued up

            # Loop to resolve stack if priority passes and stack isn't empty
            # and split second isn't active
            resolution_attempts = 0
            max_resolution_attempts = 20 # Safety break for stack loops
            while (not getattr(gs, 'split_second_active', False) and
                   gs.priority_pass_count >= 2 and gs.stack and
                   resolution_attempts < max_resolution_attempts):
                resolution_attempts += 1
                resolved = False
                if hasattr(gs, 'resolve_top_of_stack'):
                     resolved = gs.resolve_top_of_stack() # This also resets priority_pass_count

                if resolved:
                     if hasattr(gs, 'check_state_based_actions'):
                         gs.check_state_based_actions()
                     if hasattr(gs, 'ability_handler') and gs.ability_handler:
                          # Re-process triggers that might have occurred due to resolution
                          gs.ability_handler.process_triggered_abilities()
                     # Note: resolve_top_of_stack should now reset priority_pass_count and priority_player
                else:
                    logging.warning("Stack resolution failed for top item, breaking resolution loop.")
                    break # Avoid infinite loop if resolution fails

            if resolution_attempts >= max_resolution_attempts:
                 logging.error(f"Exceeded max stack resolution attempts ({max_resolution_attempts}). Potential loop.")
                 # Consider ending the game or forcing state change


            # Apply continuous effects after state changes
            if hasattr(gs, 'layer_system'):
                 gs.layer_system.apply_all_effects()
            # Final SBA check after layers and stack resolution
            if hasattr(gs, 'check_state_based_actions'):
                 gs.check_state_based_actions()

            # 7. Calculate State Change Reward
            current_state = {
                "my_life": me["life"], "opp_life": opp["life"],
                "my_hand": len(me.get("hand", [])), "opp_hand": len(opp.get("hand", [])),
                "my_board": len(me.get("battlefield", [])), "opp_board": len(opp.get("battlefield", [])),
                "my_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in me.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                "opp_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in opp.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
            }
            # Ensure _add_state_change_rewards exists
            if hasattr(self, '_add_state_change_rewards'):
                state_change_reward = self._add_state_change_rewards(0.0, prev_state, current_state)
                reward += state_change_reward

            # 8. Check Game End Conditions
            # Ensure player dictionaries have win/loss/draw flags set by SBAs
            if opp.get("lost_game"):
                done = True; reward += 10.0 + max(0, gs.max_turns - gs.turn) * 0.1; info["game_result"] = "win"
            elif me.get("lost_game"):
                done = True; reward -= 10.0; info["game_result"] = "loss"
            elif me.get("game_draw") or opp.get("game_draw"): # Check draw flags
                done = True; reward += 0.0; info["game_result"] = "draw"
            elif gs.turn > gs.max_turns:
                done, truncated = True, True
                life_diff_reward = (me["life"] - opp["life"]) * 0.1
                reward += life_diff_reward
                # Set result based on life comparison if turn limit reached
                info["game_result"] = "win" if (me["life"] > opp["life"]) else "loss" if (me["life"] < opp["life"]) else "draw"
                logging.info(f"Turn limit ({gs.max_turns}) reached. Result: {info['game_result']}")
            # Check for max episode steps if applicable (assuming self.current_step exists)
            # elif hasattr(self, 'current_step') and hasattr(self, 'max_episode_steps') and self.current_step >= self.max_episode_steps:
            #     done, truncated = True, True
            #     reward -= 0.5 # Small penalty for truncation
            #     info["game_result"] = "truncated"
            #     logging.info("Max episode steps reached.")

            # Record results if game ended
            # if done and hasattr(self, 'ensure_game_result_recorded'):
            #     self.ensure_game_result_recorded()

            # 9. Finalize Step
            # Record reward if tracking
            # self.episode_rewards.append(reward)

            # Get observation and next action mask
            obs = self._get_obs() if hasattr(self, '_get_obs') else {} # Use actual observation method
            # Invalidate current mask cache so it's regenerated next time needed
            self.current_valid_actions = None
            # Regenerate for the info dict return value
            next_mask = self.generate_valid_actions().astype(bool)
            info["action_mask"] = next_mask

            # Update action/reward history if tracking
            # if hasattr(self, 'last_n_actions') and hasattr(self, 'last_n_rewards'):
            #      self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
            #      self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = reward

            # Update strategy memory if implemented
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory and pre_action_pattern is not None:
                try: gs.strategy_memory.update_strategy(pre_action_pattern, reward)
                except Exception as strategy_e: logging.error(f"Error updating strategy memory: {strategy_e}")

            return obs, reward, done, truncated, info

        except Exception as e:
            # --- Critical Error Handling ---
            logging.error(f"CRITICAL error in apply_action (Action {action_idx}): {e}", exc_info=True)
            obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {} # Use safe version
            mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); mask[11] = True; mask[12] = True # Pass/Concede
            info["action_mask"] = mask
            info["critical_error"] = True
            info["error_message"] = str(e)
            return obs, -5.0, True, False, info # End episode on critical error
        
    # --- Individual Action Handlers ---
    # These methods will be called by apply_action based on action_type
    
    def _handle_surveil_choice(self, param, **kwargs):
        """Handle Surveil choice: PUT_TO_GRAVEYARD"""
        gs = self.game_state
        if hasattr(gs, 'choice_context') and gs.choice_context and gs.choice_context.get("type") == "surveil":
            context = gs.choice_context
            player = context["player"]
            if not context.get("cards"):
                logging.warning("Surveil choice made but no cards left to process.")
                gs.choice_context = None # Clear context
                gs.phase = gs.PHASE_PRIORITY # Return to priority
                return 0.0, True

            card_id = context["cards"].pop(0)
            card = gs._safe_get_card(card_id)
            card_name = card.name if card else card_id
            gs.move_card(card_id, player, "library_top_temp", player, "graveyard") # Assume temp zone
            logging.debug(f"Surveil: Put {card_name} into graveyard.")

            # If done surveiling, clear context and return to priority
            if not context.get("cards"):
                logging.debug("Surveil finished.")
                gs.choice_context = None
                gs.phase = gs.PHASE_PRIORITY
                gs.priority_pass_count = 0 # Priority back to active
                gs.priority_player = gs._get_active_player()
            return 0.05, True
        logging.warning("PUT_TO_GRAVEYARD called outside of Surveil context.")
        return -0.1, False

    def _handle_scry_surveil_choice(self, param, **kwargs):
        """Handle Scry/Surveil choice: PUT_ON_TOP"""
        gs = self.game_state
        if hasattr(gs, 'choice_context') and gs.choice_context:
            context = gs.choice_context
            player = context["player"]
            choice_type = context.get("type")

            if choice_type not in ["scry", "surveil"] or not context.get("cards"):
                logging.warning("PUT_ON_TOP choice made but no cards/context.")
                gs.choice_context = None
                gs.phase = gs.PHASE_PRIORITY
                return -0.1, False

            card_id = context["cards"].pop(0)
            card = gs._safe_get_card(card_id)
            card_name = card.name if card else card_id

            if choice_type == "scry":
                if "kept_on_top" not in context: context["kept_on_top"] = []
                context["kept_on_top"].append(card_id)
                logging.debug(f"Scry: Keeping {card_name} on top.")
            else: # Surveil
                 # Conceptually stays on top, just removed from choice list
                logging.debug(f"Surveil: Keeping {card_name} on top.")

            # If done, finalize and return to priority
            if not context.get("cards"):
                if choice_type == "scry":
                     # Need AI to order the kept cards
                     # Simple: Keep current order
                     player["library"] = context["kept_on_top"] + player["library"]
                     logging.debug("Scry finished.")
                else: # Surveil
                     logging.debug("Surveil finished.")

                gs.choice_context = None
                gs.phase = gs.PHASE_PRIORITY
                gs.priority_pass_count = 0
                gs.priority_player = gs._get_active_player()

            return 0.05, True
        logging.warning("PUT_ON_TOP called outside of Scry/Surveil context.")
        return -0.1, False


    def _handle_scry_choice(self, param, **kwargs):
        """Handle Scry choice: PUT_ON_BOTTOM"""
        gs = self.game_state
        if hasattr(gs, 'choice_context') and gs.choice_context and gs.choice_context.get("type") == "scry":
            context = gs.choice_context
            player = context["player"]
            if not context.get("cards"):
                logging.warning("PUT_ON_BOTTOM choice made but no cards left.")
                gs.choice_context = None
                gs.phase = gs.PHASE_PRIORITY
                return -0.1, False

            card_id = context["cards"].pop(0)
            card = gs._safe_get_card(card_id)
            card_name = card.name if card else card_id

            if "put_on_bottom" not in context: context["put_on_bottom"] = []
            context["put_on_bottom"].append(card_id)
            logging.debug(f"Scry: Putting {card_name} on bottom.")

            # If done, finalize and return to priority
            if not context.get("cards"):
                 # Put kept cards on top, bottomed cards on bottom
                 player["library"] = context.get("kept_on_top", []) + player["library"] + context.get("put_on_bottom", [])
                 logging.debug("Scry finished.")
                 gs.choice_context = None
                 gs.phase = gs.PHASE_PRIORITY
                 gs.priority_pass_count = 0
                 gs.priority_player = gs._get_active_player()
            return 0.05, True
        logging.warning("PUT_ON_BOTTOM called outside of Scry context.")
        return -0.1, False

    def _handle_no_op(self, param, **kwargs):
        logging.debug("Executed NO_OP action.")
        return 0.0

    def _handle_end_turn(self, param, **kwargs):
        gs = self.game_state
        # Advance phase until end step, then let the next pass handle cleanup->next turn
        if gs.phase < gs.PHASE_END_STEP:
            gs.phase = gs.PHASE_END_STEP
            gs.priority_pass_count = 0 # Reset priority for end step
            gs.priority_player = gs._get_active_player()
            logging.debug("Fast-forwarding to End Step.")
        elif gs.phase == gs.PHASE_END_STEP:
            # If already in end step, pass priority to trigger cleanup eventually
             gs._pass_priority()
        return 0.0

    def _handle_untap_next(self, param, **kwargs):
        gs = self.game_state
        gs._untap_phase(gs._get_active_player())
        gs.phase = gs.PHASE_UPKEEP
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01 # Small reward for progressing

    def _handle_draw_next(self, param, **kwargs):
        gs = self.game_state
        gs._draw_phase(gs._get_active_player())
        gs.phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.05 # Draw is good

    def _handle_main_phase_end(self, param, **kwargs):
        gs = self.game_state
        if gs.phase == gs.PHASE_MAIN_PRECOMBAT:
            gs.phase = gs.PHASE_BEGIN_COMBAT
        elif gs.phase == gs.PHASE_MAIN_POSTCOMBAT:
            gs.phase = gs.PHASE_END_STEP
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01

    def _handle_combat_damage(self, param, **kwargs):
        gs = self.game_state
        if gs.combat_resolver:
            damage_dealt = gs.combat_resolver.resolve_combat()
            gs.phase = gs.PHASE_END_COMBAT # Move to end of combat
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
            # Reward is calculated based on damage in apply_action
            return 0.0 # Base reward handled later
        return -0.1 # Penalty if no resolver

    def _handle_end_phase(self, param, **kwargs):
        gs = self.game_state
        gs._advance_phase() # Let advance phase handle logic
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01

    def _handle_mulligan(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if gs.perform_mulligan(player, keep_hand=False):
            return -0.1 # Small penalty for mulligan
        return -0.2 # Failed mulligan

    def _handle_keep_hand(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if gs.perform_mulligan(player, keep_hand=True):
             return 0.1 # Small reward for keeping
        return -0.1 # Error keeping

    def _handle_bottom_card(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if gs.bottom_card(player, param):
            return 0.05 # Small reward
        return -0.1 # Failed

    def _handle_upkeep_pass(self, param, **kwargs):
        gs = self.game_state
        gs.phase = gs.PHASE_DRAW
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01

    def _handle_begin_combat_end(self, param, **kwargs):
        gs = self.game_state
        gs.phase = gs.PHASE_DECLARE_ATTACKERS
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01

    def _handle_end_combat(self, param, **kwargs):
        gs = self.game_state
        gs.phase = gs.PHASE_MAIN_POSTCOMBAT
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01

    def _handle_end_step(self, param, **kwargs):
        gs = self.game_state
        gs.phase = gs.PHASE_CLEANUP
        # Cleanup happens automatically, then turn advances
        return 0.01

    def _handle_pass_priority(self, param, **kwargs):
        gs = self.game_state
        gs._pass_priority() # Let GameState handle the logic
        # Reward is neutral for passing priority itself; consequences come later.
        return 0.0, True

    def _handle_concede(self, param, **kwargs):
        # Handled directly in apply_action's main logic
        return -10.0 # Large penalty

    def _handle_play_land(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if param < len(player["hand"]):
            card_id = player["hand"][param]
            if gs.play_land(card_id, player):
                return 0.2 # Good reward for successful land play
            else:
                return -0.1 # Penalty for trying invalid land play
        return -0.2 # Invalid index


    def _handle_play_spell(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        context = kwargs.get('context', {}) # Use context passed in
        hand_idx = param

        if hand_idx < len(player["hand"]):
            card_id = player["hand"][hand_idx]
            card = gs._safe_get_card(card_id)
            if not card: return -0.2, False # Card not found

            # Add hand index to context if needed by handlers/mana system
            if 'hand_idx' not in context: context['hand_idx'] = hand_idx

            # Use CardEvaluator to estimate value BEFORE casting
            card_value = 0
            if self.card_evaluator:
                 eval_context = {"situation": "casting", **context} # Merge context
                 card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context)

            # Attempt to cast
            if gs.cast_spell(card_id, player, context=context):
                # Reward based on card value and successful cast
                return 0.1 + card_value * 0.3, True
            else:
                 # Penalty for trying invalid cast (failed affordability or targeting inside cast_spell)
                 return -0.1, False
        return -0.2, False # Invalid hand index
    def _handle_play_mdfc_land_back(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if param < len(player["hand"]):
            card_id = player["hand"][param]
            # Logic similar to play_land but specifying back face
            if gs.play_land(card_id, player, play_back_face=True):
                 return 0.18 # Slightly less than normal land? Or more for flexibility?
            else:
                 return -0.1
        return -0.2

    def _handle_play_mdfc_back(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if param < len(player["hand"]):
            card_id = player["hand"][param]
            card = gs._safe_get_card(card_id)
            card_value = 0
            if self.card_evaluator and card:
                card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details={"is_back_face": True})

            context = {"cast_back_face": True}
            if gs.cast_spell(card_id, player, context=context):
                return 0.1 + card_value * 0.3
            else:
                return -0.1
        return -0.2

    def _handle_play_adventure(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if param < len(player["hand"]):
            card_id = player["hand"][param]
            card = gs._safe_get_card(card_id)
            # Simplified value
            card_value = 0.5
            if self.card_evaluator and card:
                # Evaluate the adventure part specifically if possible
                 card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details={"is_adventure": True})

            context = {"cast_as_adventure": True}
            if gs.cast_spell(card_id, player, context=context):
                 return 0.1 + card_value * 0.25
            else:
                 return -0.1
        return -0.2

    def _handle_cast_from_exile(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        castable_cards = list(getattr(gs, 'cards_castable_from_exile', set()))
        if param < len(castable_cards):
             card_id = castable_cards[param]
             card_value = 0
             if self.card_evaluator:
                  card_value = self.card_evaluator.evaluate_card(card_id, "play")
             if gs.cast_spell(card_id, player): # Assumes cast_spell handles exile source
                  return 0.2 + card_value * 0.3 # Bonus for casting from exile
             else:
                  return -0.1
        return -0.2

    def _handle_attack(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        battlefield_idx = param
        if battlefield_idx < len(player["battlefield"]):
            card_id = player["battlefield"][battlefield_idx]
            card = gs._safe_get_card(card_id)
            if not card: return -0.2, False # Card not found

            # Use CombatActionHandler's validation method if available
            can_attack = False
            if self.combat_handler:
                can_attack = self.combat_handler.is_valid_attacker(card_id)
            else: # Fallback validation
                 if 'creature' in getattr(card, 'card_types', []) and \
                    card_id not in player.get("tapped_permanents", set()) and \
                    not (card_id in player.get("entered_battlefield_this_turn", set()) and not self._has_haste(card_id)):
                     can_attack = True


            if card_id in gs.current_attackers:
                # If already attacking, deselect
                gs.current_attackers.remove(card_id)
                # Remove targeting info if applicable
                if hasattr(gs, 'planeswalker_attack_targets') and card_id in gs.planeswalker_attack_targets: del gs.planeswalker_attack_targets[card_id]
                if hasattr(gs, 'battle_attack_targets') and card_id in gs.battle_attack_targets: del gs.battle_attack_targets[card_id]
                return -0.05, True # Small penalty for cancelling attack declaration
            else:
                # If not attacking, declare attack if valid
                if can_attack:
                     gs.current_attackers.append(card_id)
                     # Reset targeting (target needs separate action now)
                     if hasattr(gs, 'planeswalker_attack_targets') and card_id in gs.planeswalker_attack_targets: del gs.planeswalker_attack_targets[card_id]
                     if hasattr(gs, 'battle_attack_targets') and card_id in gs.battle_attack_targets: del gs.battle_attack_targets[card_id]
                     return 0.1, True # Small reward for declaring attacker
                else:
                     return -0.1, False # Invalid attacker selected
        return -0.2, False # Invalid battlefield index


    def _handle_block(self, param, **kwargs):
        gs = self.game_state
        blocker_player = gs.p1 if gs.agent_is_p1 else gs.p2 # Blocker is 'me'
        battlefield_idx = param
        if battlefield_idx < len(blocker_player["battlefield"]):
            blocker_id = blocker_player["battlefield"][battlefield_idx]
            blocker_card = gs._safe_get_card(blocker_id)
            if not blocker_card or 'creature' not in getattr(blocker_card, 'card_types', []): return -0.15, False # Not a creature

            # Determine which attacker to block (needs context or agent decision)
            # Simple heuristic: Block the attacker this creature is already blocking if possible, else find a new one
            current_block_target = None
            for atk_id, blockers in gs.current_block_assignments.items():
                if blocker_id in blockers:
                    current_block_target = atk_id
                    break

            # If already blocking an attacker, deselect block
            if current_block_target:
                gs.current_block_assignments[current_block_target].remove(blocker_id)
                if not gs.current_block_assignments[current_block_target]: # Remove empty list
                    del gs.current_block_assignments[current_block_target]
                return -0.05, True # Deselected block

            # If not blocking, find an attacker to block
            else:
                target_attacker_id = None
                if 'target_attacker_id' in kwargs.get('context', {}): # Agent specified target
                    target_attacker_id = kwargs['context']['target_attacker_id']
                else: # AI chooses target
                    possible_targets = [atk_id for atk_id in gs.current_attackers if self._can_block(blocker_id, atk_id)]
                    if possible_targets:
                        # Simple heuristic: Block highest power attacker
                        possible_targets.sort(key=lambda atk_id: getattr(gs._safe_get_card(atk_id),'power',0), reverse=True)
                        target_attacker_id = possible_targets[0]

                # Assign block if target found
                if target_attacker_id and self._can_block(blocker_id, target_attacker_id): # Double check block validity
                     if target_attacker_id not in gs.current_block_assignments: gs.current_block_assignments[target_attacker_id] = []
                     # Check menace constraint only when finalizing blocks? Or check here? Let's check basic validity here.
                     attacker_card = gs._safe_get_card(target_attacker_id)
                     if self._has_keyword(attacker_card, "menace") and len(gs.current_block_assignments.get(target_attacker_id, [])) == 0:
                          # Need another blocker - don't assign yet, maybe signal multi-block needed?
                          # Or use ASSIGN_MULTIPLE_BLOCKERS action instead?
                          # For now, allow assignment, let validation happen later.
                          logging.debug(f"Assigning first blocker to Menace attacker {attacker_card.name}")
                          pass

                     gs.current_block_assignments[target_attacker_id].append(blocker_id)
                     return 0.1, True
                else:
                    return -0.1, False # No valid attacker found or can't block

        return -0.2, False # Invalid battlefield index

    def _handle_tap_land_for_mana(self, param, **kwargs):
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         if param < len(player["battlefield"]):
             card_id = player["battlefield"][param]
             if gs.tap_for_mana(card_id, player): # Assumes tap_for_mana exists
                  return 0.05 # Mana is useful
             else:
                  return -0.1 # Failed tap
         return -0.2

    def _handle_tap_land_for_effect(self, param, **kwargs):
         # Similar to activate ability, but specific to land effects
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         if param < len(player["battlefield"]):
             card_id = player["battlefield"][param]
             # Assuming ability index 0 is the non-mana tap ability
             if hasattr(gs, 'ability_handler') and gs.ability_handler.activate_ability(card_id, 0, player):
                  return 0.15 # Land effects can be good
             else:
                  return -0.1
         return -0.2

    def _handle_activate_ability(self, param, **kwargs):
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         if isinstance(param, tuple) and len(param) == 2:
             card_idx, ability_idx = param
             if card_idx < len(player["battlefield"]):
                 card_id = player["battlefield"][card_idx]
                 # Get ability value before activating
                 ability_value = 0
                 if self.card_evaluator:
                     # Pass GameState directly to evaluator method
                      ability_value, _ = self.evaluate_ability_activation(card_id, ability_idx)

                 # Use GameState's ability handler
                 if hasattr(gs, 'ability_handler') and gs.ability_handler.activate_ability(card_id, ability_idx, player):
                      # Reward based on ability value
                      return 0.1 + ability_value * 0.4, True
                 else:
                      return -0.1, False # Failed activation
             else: # card_idx out of bounds
                  return -0.2, False
         return -0.2, False # Invalid param format

    def _handle_loyalty_ability(self, param, action_type, **kwargs):
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         # Param should be PW index on battlefield
         if param < len(player["battlefield"]):
             card_id = player["battlefield"][param]
             card = gs._safe_get_card(card_id)
             if card and 'planeswalker' in getattr(card, 'card_types', []):
                 # Find appropriate ability index based on action type
                 ability_idx = -1
                 if hasattr(card, 'loyalty_abilities'):
                      for idx, ability in enumerate(card.loyalty_abilities):
                           cost = ability.get('cost', 0)
                           is_ultimate = ability.get('is_ultimate', False)
                           if action_type == "LOYALTY_ABILITY_PLUS" and cost > 0: ability_idx = idx; break
                           if action_type == "LOYALTY_ABILITY_ZERO" and cost == 0: ability_idx = idx; break
                           if action_type == "LOYALTY_ABILITY_MINUS" and cost < 0 and not is_ultimate: ability_idx = idx; break
                           if action_type == "ULTIMATE_ABILITY" and is_ultimate: ability_idx = idx; break

                 if ability_idx != -1:
                      # Use activate_planeswalker_ability
                      if gs.activate_planeswalker_ability(card_id, ability_idx, player):
                           # Evaluate effect
                           ability_value, _ = self.evaluate_ability_activation(card_id, ability_idx)
                           return 0.15 + ability_value * 0.5
                      else:
                           return -0.1 # Failed activation
                 else:
                      return -0.15 # Ability type not found
         return -0.2 # Invalid index or not a planeswalker
     
    def evaluate_ability_activation(self, card_id, ability_idx):
        """Evaluate strategic value of activating an ability."""
        if hasattr(self.game_state, 'strategic_planner') and self.game_state.strategic_planner:
            return self.game_state.strategic_planner.evaluate_ability_activation(card_id, ability_idx)
        return 0.5, "Default ability value" # Fallback

    def _handle_transform(self, param, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param is battlefield index
        if param < len(player["battlefield"]):
             card_id = player["battlefield"][param]; card = gs._safe_get_card(card_id)
             if card and hasattr(card, 'transform') and card.can_transform(gs): # Check if possible
                 card.transform() # Card method handles its state change
                 gs.trigger_ability(card_id, "TRANSFORMED", {"controller": player})
                 return 0.1, True
             return -0.1, False # Not transformable or cannot transform now
        return -0.2, False # Invalid index

    def _handle_discard_card(self, param, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        hand_idx = param
        if hand_idx < len(player["hand"]):
            card_id = player["hand"][hand_idx]
            value = self.card_evaluator.evaluate_card(card_id, "discard") if self.card_evaluator else 0
            # Check for Madness before moving to GY
            card = gs._safe_get_card(card_id)
            has_madness = "madness" in getattr(card,'oracle_text','').lower()
            target_zone = "exile" if has_madness else "graveyard" # Move to exile first if madness
            success_move = gs.move_card(card_id, player, "hand", player, target_zone, cause="discard")
            if success_move and has_madness:
                # Set up madness trigger/context
                if not hasattr(gs, 'madness_trigger'): gs.madness_trigger = []
                gs.madness_trigger.append({'card_id': card_id, 'player': player})
                logging.debug(f"Discarded {card.name} with Madness, moved to exile.")
            elif success_move:
                logging.debug(f"Discarded {card.name} to graveyard.")
            return -0.05 + value * 0.2 if success_move else -0.15, success_move
        return -0.2, False

    def _handle_unlock_door(self, param, context, **kwargs):
        gs = self.game_state
        # Param is battlefield index of Room card
        if hasattr(gs, 'ability_handler') and gs.ability_handler.handle_unlock_door(param):
             return 0.3, True
        return -0.1, False

    def _handle_level_up_class(self, param, context, **kwargs):
        gs = self.game_state
        # Param is battlefield index of Class card
        if hasattr(gs, 'ability_handler'):
            success = gs.ability_handler.handle_class_level_up(param)
            if success:
                 # Calculate reward based on new level maybe
                 player = gs._get_active_player()
                 card = gs._safe_get_card(player["battlefield"][param])
                 level = getattr(card, 'current_level', 1)
                 return 0.2 * level, True # Higher reward for higher levels
            else:
                 return -0.1, False
        return -0.15, False # No ability handler

    def _handle_select_target(self, param, context, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if not gs.targeting_context or gs.targeting_context.get("controller") != player:
            logging.warning("SELECT_TARGET called but not in targeting phase for this player.")
            return -0.2, False

        ctx = gs.targeting_context
        required_count = ctx.get('required_count', 1)
        selected_targets = ctx.get('selected_targets', [])

        # Get valid targets for the current selection step
        valid_targets_list = []
        if gs.targeting_system:
             valid_map = gs.targeting_system.get_valid_targets(ctx["source_id"], player, ctx["required_type"])
             for targets in valid_map.values(): valid_targets_list.extend(targets)
        else: return -0.15, False # Cannot select target without system

        if param < len(valid_targets_list):
            target_id = valid_targets_list[param]
            if target_id not in selected_targets: # Avoid duplicates unless allowed
                 selected_targets.append(target_id)
                 ctx["selected_targets"] = selected_targets
                 logging.debug(f"Selected target {len(selected_targets)}/{required_count}: {target_id}")

                 # If enough targets are now selected, finalize targeting
                 if len(selected_targets) >= required_count:
                      # Update the stack item context
                      found_stack_item = False
                      for i in range(len(gs.stack) - 1, -1, -1): # Check from top down
                           item = gs.stack[i]
                           if isinstance(item, tuple) and item[1] == ctx["source_id"]:
                                new_stack_context = item[3] if len(item) > 3 else {}
                                # Structure targets based on type? Simple list for now.
                                new_stack_context['targets'] = {"chosen": selected_targets} # Example structure
                                gs.stack[i] = item[:3] + (new_stack_context,)
                                found_stack_item = True
                                logging.debug(f"Updated stack item {i} with targets: {selected_targets}")
                                break
                      if not found_stack_item:
                           logging.error("Targeting context active but couldn't find matching stack item!")
                           # Reset state anyway?
                           gs.targeting_context = None
                           gs.phase = gs.PHASE_PRIORITY
                           return -0.2, False

                      # Clear targeting context and return to priority phase
                      gs.targeting_context = None
                      gs.phase = gs.PHASE_PRIORITY
                      gs.priority_pass_count = 0
                      gs.priority_player = gs._get_active_player()
                      logging.debug("Targeting complete, returning to priority.")
                      return 0.05, True # Success
                 else:
                      # More targets needed, stay in targeting phase
                      return 0.02, True # Incremental success
            else: # Invalid index 'param'
                 logging.warning(f"Invalid target index selected: {param}")
                 return -0.1, False
        else: # Not in targeting phase
             return -0.2, False
    

    def _handle_sacrifice_permanent(self, param, context, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if not gs.sacrifice_context or gs.sacrifice_context.get("controller") != player:
            logging.warning("SACRIFICE_PERMANENT called but not in sacrifice phase for this player.")
            return -0.2, False

        ctx = gs.sacrifice_context
        required_count = ctx.get('required_count', 1)
        selected_perms = ctx.get('selected_permanents', [])

        # Get valid permanents to sacrifice
        valid_perms = []
        perm_type_req = ctx.get('required_type')
        for i, perm_id in enumerate(player["battlefield"]):
             perm_card = gs._safe_get_card(perm_id)
             if not perm_card: continue
             # Check type if required
             if not perm_type_req or perm_type_req == "permanent" or perm_type_req in getattr(perm_card, 'card_types', []):
                 valid_perms.append(perm_id)

        if param < len(valid_perms):
            sac_id = valid_perms[param]
            if sac_id not in selected_perms: # Avoid duplicates
                selected_perms.append(sac_id)
                ctx["selected_permanents"] = selected_perms
                logging.debug(f"Selected sacrifice {len(selected_perms)}/{required_count}: {gs._safe_get_card(sac_id).name}")

                # If enough sacrifices selected, finalize
                if len(selected_perms) >= required_count:
                     # Perform sacrifices and update stack item
                     # ... (logic moved from _add_sacrifice_actions) ...
                    sac_reward_mod = 0
                    for sacrifice_id in selected_perms:
                        sac_card = gs._safe_get_card(sacrifice_id)
                        if self.card_evaluator and sac_card:
                            sac_reward_mod -= self.card_evaluator.evaluate_card(sacrifice_id, "general") * 0.2
                        # Actual move handled by the ability resolution usually
                        # We store the chosen IDs for the ability resolver.

                    # Update the context of the ability on the stack
                    found_stack_item = False
                    for i in range(len(gs.stack) -1, -1, -1):
                         item = gs.stack[i]
                         if isinstance(item, tuple) and item[1] == ctx["source_id"]:
                              new_stack_context = item[3] if len(item) > 3 else {}
                              new_stack_context['sacrificed_permanents'] = selected_perms
                              gs.stack[i] = item[:3] + (new_stack_context,)
                              found_stack_item = True
                              logging.debug(f"Updated stack item {i} with sacrifices: {selected_perms}")
                              break
                    if not found_stack_item: logging.error("Sacrifice context active but couldn't find stack item!")

                    gs.sacrifice_context = None
                    gs.phase = gs.PHASE_PRIORITY
                    gs.priority_pass_count = 0
                    gs.priority_player = gs._get_active_player()
                    logging.debug("Sacrifice choice complete, returning to priority.")
                    return 0.1 + sac_reward_mod, True # Reward for completing sacrifice choice
                else:
                     # More sacrifices needed
                     return 0.02, True # Incremental success
            else: # Invalid index
                 logging.warning(f"Invalid sacrifice index selected: {param}")
                 return -0.1, False
        else: # Not in sacrifice phase
             return -0.2, False
    
    def _handle_special_choice_actions(self, player, valid_actions, set_valid_action):
        """Add actions for Scry, Surveil, Dredge, Choose Mode, Choose X, Choose Color."""
        gs = self.game_state
        if gs.phase != gs.PHASE_CHOOSE: # Only generate these during the dedicated CHOICE phase
            return

        if hasattr(gs, 'choice_context') and gs.choice_context:
            context = gs.choice_context
            choice_type = context.get("type")
            source_id = context.get("source_id")
            choice_player = context.get("player")

            if choice_player != player: # Not this player's choice
                set_valid_action(11, "PASS_PRIORITY (Waiting for opponent choice)")
                return

            # Scry / Surveil
            if choice_type in ["scry", "surveil"] and context.get("cards"):
                card_id = context["cards"][0] # Process one card at a time
                card = gs._safe_get_card(card_id)
                card_name = card.name if card else card_id
                set_valid_action(306, f"PUT_ON_TOP {card_name}") # Put on Top
                if choice_type == "scry":
                    set_valid_action(307, f"PUT_ON_BOTTOM {card_name}") # Put on Bottom (Scry only)
                else: # Surveil
                     set_valid_action(305, f"PUT_TO_GRAVEYARD {card_name}") # Put to GY (Surveil only)

            # Dredge (Replace Draw)
            elif choice_type == "dredge" and context.get("card_id"):
                 card_id = context["card_id"]
                 dredge_val = context.get("value")
                 if len(player["library"]) >= dredge_val:
                     # Find card index in graveyard
                     gy_idx = -1
                     for idx, gy_id in enumerate(player["graveyard"]):
                          if gy_id == card_id and idx < 6: # GY Index 0-5 ? Action space limited
                               gy_idx = idx
                               break
                     if gy_idx != -1:
                         # Param for DREDGE needs to be the graveyard index.
                         # This needs adjustment in ACTION_MEANINGS or handler.
                         # Assuming DREDGE action takes GY index via context for now.
                         set_valid_action(308, f"DREDGE {gs._safe_get_card(card_id).name}")
                 set_valid_action(11, "Skip Dredge") # Option to not dredge

            # Choose Mode
            elif choice_type == "choose_mode" and context.get("num_choices") and context.get("max_modes"):
                num_choices = context.get("num_choices")
                max_modes = context.get("max_modes")
                selected_count = len(context.get("selected_modes", []))
                if selected_count < max_modes:
                     for i in range(min(num_choices, 10)): # Mode index 0-9
                          # Prevent selecting the same mode twice unless allowed
                          if i not in context.get("selected_modes", []):
                               set_valid_action(348 + i, f"CHOOSE_MODE {i+1}")
                set_valid_action(11, "PASS_PRIORITY (Finish Mode Choice)") # Finish choosing

            # Choose X
            elif choice_type == "choose_x" and context.get("max_x") is not None:
                 max_x = context.get("max_x")
                 for i in range(min(max_x, 10)): # X value 1-10
                      set_valid_action(358 + i, f"CHOOSE_X_VALUE {i+1}")
                 if context.get("min_x", 0) == 0: # Allow X=0 if minimum is 0
                      pass # Need an action for X=0 or handle via PASS?
                 # set_valid_action(11, "PASS_PRIORITY (X selected)") # Assume choosing X transitions automatically

            # Choose Color
            elif choice_type == "choose_color":
                 for i in range(5): # Color index 0-4 (WUBRG)
                      set_valid_action(368 + i, f"CHOOSE_COLOR {['W','U','B','R','G'][i]}")
                 # set_valid_action(11, "PASS_PRIORITY (Color selected)") # Assume choosing transitions

            # Kicker / Additional Cost / Escalate Choices
            elif choice_type == "pay_kicker":
                set_valid_action(400, "PAY_KICKER") # Param = True
                set_valid_action(401, "DONT_PAY_KICKER") # Param = False
            elif choice_type == "pay_additional":
                 set_valid_action(402, "PAY_ADDITIONAL_COST") # Param = True
                 set_valid_action(403, "DONT_PAY_ADDITIONAL_COST") # Param = False
            elif choice_type == "pay_escalate" and context.get("num_modes") and context.get("num_selected"):
                 max_extra = context.get("num_modes") - 1
                 selected = context.get("num_selected")
                 # Allow paying for more modes if affordable and available
                 for i in range(max_extra):
                      num_to_pay = i + 1
                      if selected + num_to_pay <= context.get("num_modes"):
                           set_valid_action(404, f"PAY_ESCALATE for {num_to_pay} extra modes") # Param = num_extra_modes
                 set_valid_action(11, "PASS_PRIORITY (Finish Escalate)") # Don't pay escalate

            # Spree Mode Selection (handled separately in _add_spree_mode_actions, or move here?)

        else:
             # If no choice context, just allow passing priority
             set_valid_action(11, "PASS_PRIORITY (No choices pending)")
             
    

    def _add_spree_mode_actions(self, player, valid_actions, set_valid_action):
        """Add actions for Spree mode selection during casting."""
        gs = self.game_state
        # Check if a Spree spell is being prepared (e.g., via a 'PREPARE_SPREE' phase/context)
        if hasattr(gs, 'spree_context') and gs.spree_context:
            context = gs.spree_context
            card_id = context.get('card_id')
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'spree_modes'):
                 selected_modes = context.get("selected_modes", set())
                 base_cost_paid = context.get("base_cost_paid", False)

                 # Base cost must be paid first (conceptually)
                 if not base_cost_paid:
                     # Maybe add an action "PAY_BASE_SPREE_COST"? Or handle implicitly.
                     pass

                 # Allow selecting additional modes if base cost is handled
                 if base_cost_paid:
                     for mode_idx, mode_data in enumerate(card.spree_modes):
                          # Action space mapping needs adjustment: (card_idx, mode_idx)
                          # Example mapping: card 0-7, mode 0-1 -> indices 258-273
                          # Need the hand_idx of the spree card. Assume it's stored in context.
                          hand_idx = context.get("hand_idx")
                          if hand_idx is not None and hand_idx < 8 and mode_idx < 2:
                               action_index = 258 + (hand_idx * 2) + mode_idx
                               mode_cost = mode_data.get('cost', '')
                               if self._can_afford_cost_string(player, mode_cost):
                                    # Prevent re-selecting the same mode
                                    if mode_idx not in selected_modes:
                                        set_valid_action(action_index, f"SELECT_SPREE_MODE {mode_idx} for {card.name}")
                     # Add action to finalize spree casting? Or use PLAY_SPELL?
                     set_valid_action(20 + hand_idx, f"CAST_SPREE {card.name} with selected modes")    

    def _handle_choose_mode(self, param, context, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if not gs.choice_context or gs.choice_context.get("type") != "choose_mode" or gs.choice_context.get("player") != player:
             logging.warning("CHOOSE_MODE called out of context.")
             return -0.2, False

        ctx = gs.choice_context
        num_choices = ctx.get("num_choices", 0)
        max_modes = ctx.get("max_modes", 1)
        selected_modes = ctx.get("selected_modes", [])

        if param < num_choices:
             if len(selected_modes) < max_modes:
                  if param not in selected_modes: # Prevent duplicates unless allowed by max_modes > 1? Logic needed.
                       selected_modes.append(param)
                       ctx["selected_modes"] = selected_modes
                       logging.debug(f"Selected mode {len(selected_modes)}/{max_modes}: Mode Index {param}")
                       # If final choice, finalize
                       if len(selected_modes) >= max_modes:
                            # Update stack item context
                            found_stack_item = False
                            for i in range(len(gs.stack) - 1, -1, -1):
                                item = gs.stack[i]
                                if isinstance(item, tuple) and item[1] == ctx["source_id"]:
                                    new_stack_context = item[3] if len(item) > 3 else {}
                                    new_stack_context['selected_modes'] = selected_modes
                                    gs.stack[i] = item[:3] + (new_stack_context,)
                                    found_stack_item = True
                                    logging.debug(f"Updated stack item {i} with modes: {selected_modes}")
                                    break
                            if not found_stack_item: logging.error("Mode choice context active but couldn't find stack item!")

                            gs.choice_context = None
                            gs.phase = gs.PHASE_PRIORITY
                            gs.priority_pass_count = 0
                            gs.priority_player = gs._get_active_player()
                            logging.debug("Mode choice complete.")
                            return 0.05, True
                       else: return 0.02, True # Incremental success
                  else: # Mode already selected
                       return -0.05, False
             else: # Tried to select too many modes
                 return -0.1, False
        else: # Invalid mode index
             return -0.1, False


    def _handle_choose_x(self, param, context, **kwargs):
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         if not gs.choice_context or gs.choice_context.get("type") != "choose_x" or gs.choice_context.get("player") != player:
              logging.warning("CHOOSE_X called out of context.")
              return -0.2, False

         ctx = gs.choice_context
         x_value = param # Action param 1-10 maps directly to X
         max_x = ctx.get("max_x", 10) # Get max X allowed by affordability/context

         if 1 <= x_value <= max_x:
              ctx["chosen_x"] = x_value # Store chosen value
              # Update stack item context
              found_stack_item = False
              for i in range(len(gs.stack) - 1, -1, -1):
                   item = gs.stack[i]
                   if isinstance(item, tuple) and item[1] == ctx["source_id"]:
                       new_stack_context = item[3] if len(item) > 3 else {}
                       new_stack_context['X'] = x_value
                       gs.stack[i] = item[:3] + (new_stack_context,)
                       found_stack_item = True
                       logging.debug(f"Updated stack item {i} with X={x_value}")
                       break
              if not found_stack_item: logging.error("X choice context active but couldn't find stack item!")

              # Pay the X cost (mana system needs update?)
              if gs.mana_system:
                  gs.mana_system.pay_mana_cost(player, {'generic': x_value}) # Assume generic mana for X

              gs.choice_context = None
              gs.phase = gs.PHASE_PRIORITY
              gs.priority_pass_count = 0
              gs.priority_player = gs._get_active_player()
              logging.debug(f"Chose X={x_value}")
              return 0.05, True
         else:
              logging.warning(f"Invalid X value chosen: {x_value} (Max: {max_x})")
              return -0.1, False

    def _handle_choose_color(self, param, context, **kwargs):
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         if not gs.choice_context or gs.choice_context.get("type") != "choose_color" or gs.choice_context.get("player") != player:
              logging.warning("CHOOSE_COLOR called out of context.")
              return -0.2, False

         ctx = gs.choice_context
         chosen_color = ['W','U','B','R','G'][param]
         ctx["chosen_color"] = chosen_color

         # Update stack item context
         found_stack_item = False
         for i in range(len(gs.stack) - 1, -1, -1):
             item = gs.stack[i]
             if isinstance(item, tuple) and item[1] == ctx["source_id"]:
                  new_stack_context = item[3] if len(item) > 3 else {}
                  new_stack_context['chosen_color'] = chosen_color
                  gs.stack[i] = item[:3] + (new_stack_context,)
                  found_stack_item = True
                  logging.debug(f"Updated stack item {i} with color={chosen_color}")
                  break
         if not found_stack_item: logging.error("Color choice context active but couldn't find stack item!")

         gs.choice_context = None
         gs.phase = gs.PHASE_PRIORITY
         gs.priority_pass_count = 0
         gs.priority_player = gs._get_active_player()
         logging.debug(f"Chose color {chosen_color}")
         return 0.05, True
    
        # --- Placeholder Handlers for unimplemented actions ---
    def _handle_unimplemented(self, param, action_type, **kwargs):
        logging.warning(f"Action handler for {action_type} not implemented.")
        return -0.05 # Small penalty for trying unimplemented action

    def _handle_search_library(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param 0-4 maps to criteria
        search_map = {0: "basic land", 1: "creature", 2: "instant", 3: "sorcery", 4: "artifact"}
        criteria = search_map.get(param)
        if not criteria: return -0.1, False # Invalid search param

        if hasattr(gs, 'search_library_and_choose'):
            # AI chooses based on criteria. Assume gs.search_library handles move/shuffle.
            found_id = gs.search_library_and_choose(player, criteria, ai_choice_context={"goal": criteria}) # Provide simple goal context
            if found_id:
                 return 0.4, True # Successful search + find
            else: # Search failed, still shuffle
                 gs.shuffle_library(player)
                 # Action 304 is NO_OP_SEARCH_FAIL - but we don't change the action here.
                 # The reward reflects the outcome.
                 return 0.0, True # Search performed but nothing found
        return -0.15, False # Missing search function

        
    def _card_matches_criteria(self, card, criteria):
        """Basic check if card matches simple criteria."""
        if not card: return False
        types = getattr(card, 'card_types', [])
        subtypes = getattr(card, 'subtypes', [])
        type_line = getattr(card, 'type_line', '').lower()

        if criteria == "any": return True
        if criteria == "basic land" and 'basic' in type_line and 'land' in type_line: return True
        if criteria == "land" and 'land' in type_line: return True
        if criteria in types: return True
        if criteria in subtypes: return True
        # Add more specific checks if needed
        return False
         
    def _get_search_criteria_from_param(self, param):
        """Helper to map param index (e.g., 299-303) to search criteria."""
        search_map = {0: "basic land", 1: "creature", 2: "instant", 3: "sorcery", 4: "artifact"}
        return search_map.get(param, None) # param would be 0-4 if derived from 299-303


    def _handle_dredge(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param should be GY index 0-5, representing the card to dredge
        gy_idx = param
        if not hasattr(gs, 'dredge_pending') or not gs.dredge_pending or gs.dredge_pending['player'] != player:
             logging.warning("DREDGE action called but no dredge pending.")
             return -0.1, False # No valid dredge state

        if gy_idx >= len(player["graveyard"]):
            logging.warning(f"DREDGE invalid GY index {gy_idx}")
            return -0.1, False # Invalid index

        dredge_card_id = player["graveyard"][gy_idx]
        if dredge_card_id != gs.dredge_pending['card_id']:
            logging.warning(f"DREDGE selected card {dredge_card_id} does not match pending dredge card {gs.dredge_pending['card_id']}")
            return -0.1, False # Wrong card selected

        # Delegate to GameState's dredge handler which confirms the choice
        if hasattr(gs, 'perform_dredge') and gs.perform_dredge(player, dredge_card_id):
             return 0.3, True # Successful dredge is good value
        else:
             # Perform dredge failed (e.g., not enough cards to mill)
             gs.dredge_pending = None # Clear pending state on failure
             return -0.05, False


    def _handle_add_counter(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param 0-9 is target permanent index on combined battlefield? Or just player's?
        # Let's assume combined battlefield for now.
        target_idx = param
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if not target_id: return -0.1, False

        # Need context for counter type and count
        context = kwargs.get('context', {})
        counter_type = context.get('counter_type', '+1/+1')
        count = context.get('count', 1)

        success = gs.add_counter(target_id, counter_type, count)
        if success:
            reward = 0.1 * count if counter_type == '+1/+1' else 0.05 * count
            return reward, True
        return -0.05, False

    def _handle_remove_counter(self, param, action_type=None, **kwargs):
        gs = self.game_state
        # Param 0-9 target index
        target_idx = param
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if not target_id: return -0.1, False

        # Need context for counter type and count
        context = kwargs.get('context', {})
        counter_type = context.get('counter_type') # Try to infer if None?
        count = context.get('count', 1)

        target_card = gs._safe_get_card(target_id)
        if not counter_type: # Simple inference
            if hasattr(target_card, 'counters') and target_card.counters:
                 counter_type = list(target_card.counters.keys())[0]
            else: return -0.1, False # No counters to remove

        success = gs.add_counter(target_id, counter_type, -count) # Use negative count
        if success:
            reward = 0.15 * count if counter_type == '-1/-1' else 0.05 * count # Removing bad counters is good
            return reward, True
        return -0.05, False

    def _handle_proliferate(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        success = gs.proliferate(player) # Target choice needs AI input/context
        return 0.3 if success else 0.0, True

    def _handle_return_from_graveyard(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        gy_idx = param # Param 0-5 is GY index
        if gy_idx < len(player["graveyard"]):
            card_id = player["graveyard"][gy_idx]
            success = gs.move_card(card_id, player, "graveyard", player, "hand") # Default to hand
            card_value = self.card_evaluator.evaluate_card(card_id, "return_from_gy") if self.card_evaluator else 0
            return 0.2 + card_value*0.2 if success else -0.1, success
        return -0.15, False

    def _handle_reanimate(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        gy_idx = param # Param 0-5 is GY index
        if gy_idx < len(player["graveyard"]):
             card_id = player["graveyard"][gy_idx]
             card = gs._safe_get_card(card_id)
             if card and any(t in getattr(card, 'card_types', []) for t in ["creature", "artifact", "enchantment", "planeswalker"]):
                 success = gs.move_card(card_id, player, "graveyard", player, "battlefield")
                 card_value = self.card_evaluator.evaluate_card(card_id, "reanimate") if self.card_evaluator else 0
                 return 0.5 + card_value*0.3 if success else -0.1, success
        return -0.15, False


    def _handle_return_from_exile(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        exile_idx = param # Param 0-5 is exile index
        if exile_idx < len(player["exile"]):
            card_id = player["exile"][exile_idx]
            success = gs.move_card(card_id, player, "exile", player, "hand") # Default to hand
            card_value = self.card_evaluator.evaluate_card(card_id, "return_from_exile") if self.card_evaluator else 0
            return 0.3 + card_value*0.1 if success else -0.1, success
        return -0.15, False
    
    def _handle_pay_kicker(self, param, **kwargs):
        """Flag intent to pay kicker. param=True/False"""
        context = kwargs.get('context', {}) # Get context
        # Check if there's a spell being prepared to cast
        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if pending_context and 'card_id' in pending_context:
             card_id = pending_context['card_id']
             card = self.game_state._safe_get_card(card_id)
             if card and "kicker" in getattr(card, 'oracle_text', '').lower():
                  kicker_cost_str = self._get_kicker_cost_str(card)
                  player = self.game_state._get_active_player() # Get player from GS
                  # If trying to pay, check affordability now
                  if param is True:
                      if kicker_cost_str and self._can_afford_cost_string(player, kicker_cost_str, context=pending_context):
                           pending_context['kicked'] = True
                           logging.debug(f"Kicker context flag set to True for pending {card.name}")
                           return 0.01, True
                      else:
                           logging.warning(f"Cannot afford kicker cost {kicker_cost_str} for {card.name}")
                           return -0.05, False # Cannot set kicker=True if unaffordable
                  else: # param is False
                      pending_context['kicked'] = False
                      logging.debug(f"Kicker context flag set to False for pending {card.name}")
                      return 0.01, True
             else: # Card not found or no kicker
                 return -0.05, False
        return -0.1, False # No spell context pending
    
    def _get_kicker_cost_str(self, card):
        """Helper to extract kicker cost string."""
        if card and hasattr(card, 'oracle_text'):
            match = re.search(r"kicker (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
            if match:
                 cost_str = match.group(1)
                 if cost_str.isdigit(): return f"{{{cost_str}}}"
                 return cost_str
        return None


    def _handle_pay_additional_cost(self, param, **kwargs):
        """Flag intent to pay additional costs. param=True/False"""
        context = kwargs.get('context', {})
        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if pending_context and 'card_id' in pending_context:
             card_id = pending_context['card_id']
             card = self.game_state._safe_get_card(card_id)
             cost_info = self._get_additional_cost_info(card) if card else None
             if cost_info:
                 player = self.game_state._get_active_player()
                 # If trying to pay, check if possible
                 if param is True:
                     if self._can_pay_specific_additional_cost(player, cost_info, pending_context):
                         pending_context['pay_additional'] = True
                         # Specific non-mana cost details need to be added to context by agent?
                         # e.g., which creature to sacrifice, which card to discard
                         # This action just flags intent. Agent needs follow-up if choice required.
                         pending_context['additional_cost_info'] = cost_info # Store info for payment step
                         logging.debug(f"Additional Cost context flag set to True for pending {card.name}")
                         return 0.01, True
                     else:
                          logging.warning(f"Cannot meet additional cost requirement for {card.name}")
                          return -0.05, False
                 else: # param is False
                      if cost_info.get("optional", True): # Can only choose not to pay if optional
                           pending_context['pay_additional'] = False
                           logging.debug(f"Additional Cost context flag set to False for pending {card.name}")
                           return 0.01, True
                      else:
                           logging.warning("Cannot skip non-optional additional cost.")
                           return -0.05, False
             else: # No additional cost found
                  return -0.05, False # Action inappropriate
        return -0.1, False # No spell context pending
         
    
    def _can_pay_specific_additional_cost(self, player, cost_info, context):
        cost_type = cost_info.get("type")
        if cost_type == "sacrifice":
            target_type = cost_info.get("target")
            return any(target_type in getattr(self.game_state._safe_get_card(cid), 'card_types', [])
                       for cid in player["battlefield"])
        elif cost_type == "discard":
             return len(player["hand"]) >= cost_info.get("count", 1)
        # Add checks for mana, life etc.
        return False # Default false
         
    # Placeholder helpers for additional costs (need detailed implementation)
    def _get_additional_cost_info(self, card):
        if card and hasattr(card, 'oracle_text'):
             text = card.oracle_text.lower()
             if "as an additional cost to cast this spell, sacrifice a creature" in text:
                 return {"type": "sacrifice", "target": "creature", "optional": False}
             if "as an additional cost to cast this spell, discard a card" in text:
                 return {"type": "discard", "count": 1, "optional": False}
             # Add more patterns for mana, life etc.
        return None

    def _handle_pay_escalate(self, param, **kwargs):
        """Set number of extra modes chosen via escalate. param=count (e.g., 1 or 2)."""
        context = kwargs.get('context', {})
        num_extra_modes = param if isinstance(param, int) and param >= 0 else 0

        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if pending_context and 'card_id' in pending_context:
             card_id = pending_context['card_id']
             card = self.game_state._safe_get_card(card_id)
             if card and "escalate" in getattr(card, 'oracle_text', '').lower():
                 escalate_cost_str = self._get_escalate_cost_str(card) # Needs helper
                 player = self.game_state._get_active_player()
                 # Check affordability for *each* extra mode
                 if escalate_cost_str:
                      cost_per_mode = self.game_state.mana_system.parse_mana_cost(escalate_cost_str)
                      total_escalate_cost = {k: v * num_extra_modes for k,v in cost_per_mode.items()}
                      # Need to check mana affordability here relative to base cost? Complex.
                      # Assume can_pay_mana_cost for *total* spell cost handles this.
                      # For now, just store the number of extra modes.
                      pending_context['escalate_count'] = num_extra_modes
                      logging.debug(f"Escalate context flag set to {num_extra_modes} for pending {card.name}")
                      return 0.01, True
                 else:
                      logging.warning(f"Cannot parse escalate cost for {card.name}")
                      return -0.05, False
             else: # Not an escalate card
                 return -0.05, False
        return -0.1, False # No spell context pending


    def _handle_copy_permanent(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param needs to be target permanent index (combined battlefield)
        target_idx = param
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if target_id:
             target_card = gs._safe_get_card(target_id)
             if target_card:
                  token_id = gs.create_token_copy(target_card, player)
                  return 0.4 if token_id else -0.1, token_id is not None
        return -0.15, False

    def _handle_copy_spell(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param needs to be target spell stack index
        target_stack_idx = param
        if 0 <= target_stack_idx < len(gs.stack):
            item_type, card_id, original_controller, old_context = gs.stack[target_stack_idx]
            if item_type == "SPELL":
                card = gs._safe_get_card(card_id)
                if card:
                    new_context = old_context.copy()
                    new_context["is_copy"] = True
                    # TODO: Allow changing targets for copy
                    gs.add_to_stack("SPELL", card_id, player, new_context) # Copy controlled by caster
                    return 0.4, True
        return -0.15, False


    def _handle_populate(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param needs to be index of token to copy on player's battlefield
        target_token_idx = param
        tokens_on_bf = [cid for cid in player["battlefield"] if cid in player.get("tokens", [])]
        if target_token_idx < len(tokens_on_bf):
            token_to_copy_id = tokens_on_bf[target_token_idx]
            original_token = gs._safe_get_card(token_to_copy_id)
            if original_token:
                new_token_id = gs.create_token_copy(original_token, player)
                return 0.35 if new_token_id else -0.1, new_token_id is not None
        return -0.15, False
    
    def _handle_investigate(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        token_data = {"name":"Clue", "type_line":"Artifact - Clue", "card_types":["artifact"],"subtypes":["Clue"],"oracle_text":"{2}, Sacrifice this artifact: Draw a card."}
        success = gs.create_token(player, token_data)
        return (0.25, success) if success else (-0.05, False)
    

    def _handle_foretell(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        hand_idx = param
        if hand_idx < len(player["hand"]):
             card_id = player["hand"][hand_idx]; card = gs._safe_get_card(card_id)
             if card and "foretell" in getattr(card, 'oracle_text', '').lower():
                 cost = {"generic": 2} # Standard foretell cost
                 if gs.mana_system.can_pay_mana_cost(player, cost):
                     if gs.mana_system.pay_mana_cost(player, cost):
                         gs.move_card(card_id, player, "hand", player, "exile")
                         if not hasattr(gs, 'foretold_cards'): gs.foretold_cards = {}
                         gs.foretold_cards[card_id] = gs.turn # Store turn foretold
                         logging.debug(f"Foretold {card.name}")
                         return 0.2, True
                 return -0.05, False # Can't afford
        return -0.1, False

    def _handle_amass(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        amount = param if isinstance(param, int) and param > 0 else 1 # Assume context provides amount
        success = gs.amass(player, amount)
        return (0.1 * amount, success) if success else (-0.05, False)
    
    def _handle_learn(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Simple: Draw a card
        if player["library"]:
             gs.move_card(player["library"][0], player, "library", player, "hand")
             return 0.25, True
        return 0.0, True # Can't draw, but action succeeded

    def _handle_venture(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        success = gs.venture(player) # Assumes venture logic in GS
        return 0.15 if success else -0.05, success

    def _handle_exert(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        creature_idx = param
        if creature_idx < len(player["battlefield"]):
             card_id = player["battlefield"][creature_idx]
             if card_id in gs.current_attackers: # Must be attacking
                 if not hasattr(gs, 'exerted_this_combat'): gs.exerted_this_combat = set()
                 if card_id not in gs.exerted_this_combat:
                     gs.exerted_this_combat.add(card_id)
                     # Find exert bonus in oracle text or trigger ability
                     card = gs._safe_get_card(card_id)
                     logging.debug(f"Exerted {card.name}")
                     gs.trigger_ability(card_id, "EXERTED", {"controller": player})
                     return 0.2, True
        return -0.1, False

    def _handle_explore(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        creature_idx = param # Index of exploring creature
        if creature_idx < len(player["battlefield"]):
            card_id = player["battlefield"][creature_idx]
            success = gs.explore(player, card_id)
            return 0.25 if success else -0.05, success
        return -0.1, False

    def _handle_adapt(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param is creature index. Amount comes from card text.
        creature_idx = param
        if creature_idx < len(player["battlefield"]):
            card_id = player["battlefield"][creature_idx]
            card = gs._safe_get_card(card_id)
            if card and "adapt" in getattr(card,'oracle_text','').lower():
                 match = re.search(r"adapt (\d+)", card.oracle_text.lower())
                 amount = int(match.group(1)) if match else 1
                 success = gs.adapt(player, card_id, amount)
                 return 0.1 * amount if success else -0.05, success
        return -0.1, False

    def _handle_mutate(self, param, **kwargs):
        """Handle mutate. Param is IGNORED, context needs (hand_idx, target_idx)."""
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        context = kwargs.get('context', {}) # Use context passed in

        # *** CHANGED: Get indices from context ***
        hand_idx = context.get('hand_idx')
        target_idx = context.get('target_idx') # Target on battlefield

        if hand_idx is not None and target_idx is not None:
             try: hand_idx, target_idx = int(hand_idx), int(target_idx)
             except (ValueError, TypeError):
                  logging.warning(f"Mutate context has non-integer indices: {context}")
                  return (-0.15, False)

             if hand_idx < len(player["hand"]) and target_idx < len(player["battlefield"]):
                 mutating_card_id = player["hand"][hand_idx]
                 target_id = player["battlefield"][target_idx]
                 # Mutate cost paid implicitly via alternate casting method
                 # The 'mutate' function should perform validation and merge
                 if hasattr(gs, 'mutate') and gs.mutate(player, mutating_card_id, target_id):
                     # If mutate is successful, the card is no longer in hand
                     # GameState.mutate or the casting logic should handle this zone change.
                     return 0.6, True
                 else:
                      logging.debug(f"Mutate validation/execution failed for {mutating_card_id} -> {target_id}")
                      return -0.1, False # Mutate validation failed
        else: logging.warning(f"Mutate context missing indices: {context}")
        return -0.15, False
    

    def _handle_goad(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param = combined target index
        target_idx = param
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        opponent = gs.p2 if player == gs.p1 else gs.p1
        if target_id and target_owner == opponent:
            success = gs.goad_creature(target_id)
            return 0.25 if success else -0.05, success
        return -0.1, False

    def _handle_boast(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        creature_idx = param
        if creature_idx < len(player["battlefield"]):
            card_id = player["battlefield"][creature_idx]
            # Boast requires attacking. Activate is triggered.
            # Find boast ability (assume index 1 for simplicity?)
            if card_id in gs.attackers_this_turn and card_id not in getattr(gs, 'boast_activated', set()):
                 if hasattr(gs, 'ability_handler'):
                     # Find the ability index that has 'boast' in its text? Needs better lookup.
                     # Assume index 1 for now.
                     success = gs.ability_handler.activate_ability(card_id, 1, player) # Pass ability index
                     if success:
                         if not hasattr(gs, 'boast_activated'): gs.boast_activated = set()
                         gs.boast_activated.add(card_id)
                         return 0.3, True
                     return -0.1, False # Activation failed
        return -0.15, False
    
    def _handle_counter_spell(self, param, action_type=None, **kwargs):
        gs = self.game_state
        # Param should be stack index (0=top)
        stack_idx = param if isinstance(param, int) else 0 # Default to top if no index
        success = gs.counter_spell(stack_idx)
        return 0.6 if success else -0.1, success

    def _handle_prevent_damage(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param needs to specify amount, maybe source/target filter?
        # Simple: Prevent next 2 damage to self
        amount = 2
        success = gs.prevent_damage(target=player, amount=amount) # Use player dict as target
        return 0.1 * amount if success else -0.05, success

    def _handle_redirect_damage(self, param, action_type=None, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param needs new target index (combined)
        new_target_idx = param
        new_target_id, _ = gs.get_permanent_by_combined_index(new_target_idx)
        if new_target_id:
             success = gs.redirect_damage(source_filter="any", original_target=player, new_target=new_target_id)
             return 0.4 if success else -0.05, success
        return -0.1, False

    def _handle_stifle_trigger(self, param, action_type=None, **kwargs):
        return self._handle_counter_ability(param, **kwargs) # Assume same handler works
    
    def _handle_flip_card(self, param, **kwargs):
         """Handle flipping a flip card."""
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         target_idx = param
         if target_idx < len(player["battlefield"]):
             card_id = player["battlefield"][target_idx]
             success = gs.flip_card(card_id)
             return (0.2, success) if success else (-0.1, False)
         return (-0.15, False)
    
    def _handle_equip(self, param, **kwargs):
        """Handle equip action. Param is IGNORED, context needs (equip_idx, target_idx)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        context = kwargs.get('context', {}) # Use context passed in

        # *** CHANGED: Get indices from context, not param ***
        equip_idx = context.get('equip_idx')
        target_idx = context.get('target_idx')

        if equip_idx is not None and target_idx is not None:
             # Ensure indices are integers
             try: equip_idx, target_idx = int(equip_idx), int(target_idx)
             except (ValueError, TypeError):
                  logging.warning(f"Equip context has non-integer indices: {context}")
                  return (-0.15, False)

             if 0 <= equip_idx < len(player["battlefield"]) and 0 <= target_idx < len(player["battlefield"]):
                  equip_id = player["battlefield"][equip_idx]
                  target_id = player["battlefield"][target_idx]
                  # Assume equip_permanent uses GameState methods which include cost check/payment
                  if hasattr(gs, 'equip_permanent') and gs.equip_permanent(player, equip_id, target_id):
                      # Equip cost handled within equip_permanent (it should check mana/timing)
                      return (0.25, True)
                  else:
                       logging.debug(f"Equip action failed validation or execution for {equip_id} -> {target_id}")
                       return (-0.1, False) # Equip validation/execution failed
             else: logging.warning(f"Equip indices out of bounds: E:{equip_idx}, T:{target_idx}")
        else: logging.warning(f"Equip context missing indices: {context}")
        return (-0.15, False)

    def _handle_unequip(self, param, **kwargs):
        """Handle unequip action. Param = equip_idx."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        equip_idx = param
        if equip_idx < len(player["battlefield"]):
             equip_id = player["battlefield"][equip_idx]
             success = gs.unequip_permanent(player, equip_id)
             return (0.1, success) if success else (-0.1, False)
        return (-0.15, False)


    def _handle_attach_aura(self, param, **kwargs):
        """Handle attaching aura. Param = (aura_idx, target_idx)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param structure depends on how agent selects aura & target
        # Assume param = (aura_hand_or_bf_idx, target_bf_idx_combined)
        if isinstance(param, tuple) and len(param) == 2:
             aura_idx, target_combined_idx = param
             # TODO: Need logic to find aura_id based on index (could be hand or battlefield for move)
             # TODO: Need logic to find target_id based on combined index
             aura_id = None # Placeholder
             target_id = None # Placeholder
             if aura_id and target_id:
                  success = gs.attach_aura(player, aura_id, target_id)
                  return (0.25, success) if success else (-0.1, False)
        return (-0.15, False)

    def _handle_fortify(self, param, **kwargs):
        """Handle fortify action. Param = (fort_idx, land_idx)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if isinstance(param, tuple) and len(param) == 2:
             fort_idx, land_idx = param
             if fort_idx < len(player["battlefield"]) and land_idx < len(player["battlefield"]):
                  fort_id = player["battlefield"][fort_idx]
                  target_id = player["battlefield"][land_idx]
                  success = gs.fortify_land(player, fort_id, target_id)
                  return (0.2, success) if success else (-0.1, False)
        return (-0.15, False)
     
    def _handle_reconfigure(self, param, **kwargs):
        """Handle reconfigure action. Param = battlefield index."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        card_idx = param
        if card_idx < len(player["battlefield"]):
             card_id = player["battlefield"][card_idx]
             success = gs.reconfigure_permanent(player, card_id)
             return (0.2, success) if success else (-0.1, False)
        return (-0.15, False)


    def _handle_morph(self, param, **kwargs):
        """Handle turning a morph face up. Param = battlefield index."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        card_idx = param
        if card_idx < len(player["battlefield"]):
             card_id = player["battlefield"][card_idx]
             success = gs.turn_face_up(player, card_id, pay_morph_cost=True)
             return (0.3, success) if success else (-0.1, False)
        return (-0.15, False)
    

    def _handle_clash(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        opponent = gs.p2 if player == gs.p1 else gs.p1
        winner = gs.clash(player, opponent)
        return (0.1, True) if winner == player else (0.0, True)


    def _handle_conspire(self, param, **kwargs):
        """Handle conspire. Param is IGNORED, context needs (spell_stack_idx, c1_idx, c2_idx)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        context = kwargs.get('context', {}) # Use context passed in

        # *** CHANGED: Get indices from context ***
        spell_stack_idx = context.get('spell_stack_idx')
        c1_identifier = context.get('creature1_identifier') # Can be index or ID
        c2_identifier = context.get('creature2_identifier') # Can be index or ID

        if spell_stack_idx is not None and c1_identifier is not None and c2_identifier is not None:
             try: spell_stack_idx = int(spell_stack_idx)
             except (ValueError, TypeError):
                 logging.warning(f"Conspire context has non-integer spell_stack_idx: {context}")
                 return -0.15, False

             # GameState.conspire performs validation (spell target, creatures valid/untapped/color) and taps
             if hasattr(gs, 'conspire') and gs.conspire(player, spell_stack_idx, c1_identifier, c2_identifier):
                 return 0.4, True
             else:
                 logging.debug("Conspire action failed validation or execution.")
                 return -0.1, False
        else: logging.warning(f"Conspire context missing required indices: {context}")
        return -0.15, False
    
    def _handle_grandeur(self, param, **kwargs):
        gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param = hand index of card with same name
        hand_idx = param
        if hand_idx < len(player["hand"]):
             card_id_to_discard = player["hand"][hand_idx]
             discard_card = gs._safe_get_card(card_id_to_discard)
             if not discard_card: return -0.1, False
             # Find grandeur card on battlefield
             grandeur_id_on_bf = None
             for bf_id in player["battlefield"]:
                  bf_card = gs._safe_get_card(bf_id)
                  if bf_card and bf_card.name == discard_card.name and "grandeur" in getattr(bf_card,'oracle_text','').lower():
                       grandeur_id_on_bf = bf_id
                       break
             if grandeur_id_on_bf:
                  success_discard = gs.move_card(card_id_to_discard, player, "hand", player, "graveyard")
                  if success_discard:
                      # Activate grandeur ability (assume index 0?)
                      success_ability = gs.ability_handler.activate_ability(grandeur_id_on_bf, 0, player) if hasattr(gs, 'ability_handler') else False
                      return (0.35, True) if success_ability else (0.0, True) # Allow action even if ability fizzles
                  return -0.05, False # Discard failed
        return -0.1, False
    
    def _handle_select_spree_mode(self, param, context, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param = (card_hand_idx, mode_idx)
        if isinstance(param, tuple) and len(param) == 2:
            card_idx, mode_idx = param
            if card_idx < len(player["hand"]):
                 card_id = player["hand"][card_idx]
                 card = gs._safe_get_card(card_id)
                 if card and hasattr(card, 'is_spree') and card.is_spree and mode_idx < len(getattr(card,'spree_modes',[])):
                     # Use a consistent pending context on GameState
                     if not hasattr(gs, 'pending_spell_context'): gs.pending_spell_context = {}
                     # Check if we are preparing the same card or starting new
                     if gs.pending_spell_context.get('card_id') != card_id:
                          gs.pending_spell_context = {
                               'card_id': card_id,
                               'hand_idx': card_idx, # Store hand index
                               'selected_spree_modes': set(),
                               'spree_costs': {} # Store costs paid per mode
                          }

                     # Add selected mode if not already chosen
                     selected_modes = gs.pending_spell_context.get('selected_spree_modes', set())
                     if mode_idx not in selected_modes:
                          selected_modes.add(mode_idx)
                          gs.pending_spell_context['selected_spree_modes'] = selected_modes
                          logging.debug(f"Added Spree mode {mode_idx} to pending cast for {card.name}")
                          return 0.05, True # Success in selecting mode
                     else: # Mode already selected
                          return -0.05, False
                 else: # Invalid card or mode index
                     return -0.1, False
            else: # Invalid hand index
                 return -0.1, False
        return -0.2, False # Invalid param
    
    def _handle_create_token(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param 0-4 is token type index
        token_data = gs.get_token_data_by_index(param)
        if token_data:
             success = gs.create_token(player, token_data)
             return 0.15 if success else -0.1, success
        return -0.15, False

    def _handle_cycling(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        hand_idx = param # Hand index 0-7
        if hand_idx < len(player["hand"]):
            card_id = player["hand"][hand_idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "cycling" in card.oracle_text.lower():
                cost_match = re.search(r"cycling (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                if cost_match:
                     cost_str = cost_match.group(1)
                     if cost_str.isdigit(): cost_str = f"{{{cost_str}}}"
                     if gs.mana_system.can_pay_mana_cost(player, cost_str):
                          if gs.mana_system.pay_mana_cost(player, cost_str):
                              gs.move_card(card_id, player, "hand", player, "graveyard")
                              gs._draw_phase(player) # Draw a card
                              # Trigger cycling abilities
                              gs.trigger_ability(card_id, "CYCLING", {"controller": player})
                              return 0.1, True
                     return -0.05, False # Cannot afford
        return -0.1, False
    
    def evaluate_play_card_action(self, card_id_or_hand_idx, context=None):
        """Evaluate strategic value of playing a card."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        card_id = None
        if isinstance(card_id_or_hand_idx, int) and card_id_or_hand_idx < len(player["hand"]):
             card_id = player["hand"][card_id_or_hand_idx]
        elif isinstance(card_id_or_hand_idx, str):
             card_id = card_id_or_hand_idx

        if not card_id: return 0.0

        if hasattr(self.game_state, 'strategic_planner') and self.game_state.strategic_planner:
             # Ensure context is a dict
             eval_context = context if isinstance(context, dict) else {}
             return self.game_state.strategic_planner.evaluate_play_card_action(card_id, context=eval_context)
        return 0.5 # Fallback
    
    def _handle_no_op_search_fail(self, param, **kwargs): return self._handle_no_op(param, **kwargs)
    def _handle_put_to_graveyard(self, param, **kwargs): return self._handle_unimplemented(param, "PUT_TO_GRAVEYARD", **kwargs) # Need Scry/Surveil state
    def _handle_put_on_top(self, param, **kwargs): return self._handle_unimplemented(param, "PUT_ON_TOP", **kwargs) # Need Scry/Surveil state
    def _handle_put_on_bottom(self, param, **kwargs): return self._handle_unimplemented(param, "PUT_ON_BOTTOM", **kwargs) # Need Scry state
    def _handle_cast_with_flashback(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_WITH_FLASHBACK", **kwargs)
    def _handle_cast_with_jump_start(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_WITH_JUMP_START", **kwargs)
    def _handle_cast_with_escape(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_WITH_ESCAPE", **kwargs)
    def _handle_cast_for_madness(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_FOR_MADNESS", **kwargs)
    def _handle_cast_with_overload(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_WITH_OVERLOAD", **kwargs)
    def _handle_cast_for_emerge(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_FOR_EMERGE", **kwargs)
    def _handle_cast_for_delve(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_FOR_DELVE", **kwargs)
    def _handle_cast_left_half(self, param, **kwargs): return self._handle_cast_split(param, action_type="CAST_LEFT_HALF", **kwargs)
    def _handle_cast_right_half(self, param, **kwargs): return self._handle_cast_split(param, action_type="CAST_RIGHT_HALF", **kwargs)
    def _handle_cast_fuse(self, param, **kwargs): return self._handle_cast_split(param, action_type="CAST_FUSE", **kwargs)
    def _handle_aftermath_cast(self, param, **kwargs): return self._handle_alternative_casting(param, "AFTERMATH_CAST", **kwargs)
    

    def _handle_manifest(self, param, **kwargs):
        """Handle turning a manifested card face up. Param = battlefield index."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        card_idx = param
        if card_idx < len(player["battlefield"]):
             card_id = player["battlefield"][card_idx]
             success = gs.turn_face_up(player, card_id, pay_manifest_cost=True)
             return (0.25, success) if success else (-0.1, False)
        return (-0.15, False)
    
    def _handle_alternative_casting(self, param, action_type, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        source_zone = "graveyard"
        hand_idx_param = None # Param might be GY index or Hand index (Madness)
        context = {"use_alt_cost": action_type.replace('CAST_', '').replace('_', ' ').lower()}

        if action_type == "CAST_FOR_MADNESS":
            source_zone = "exile" # Correct: Madness casts from exile after discard
            # Assume param is index in exile of the card just discarded
            exile_idx = param
            if exile_idx < len(player["exile"]):
                 card_id = player["exile"][exile_idx]
            else: return -0.1, False # Invalid exile index
        elif action_type == "CAST_FOR_EMERGE":
             # Param needs (Hand index, Sacrifice index)
             if isinstance(param, tuple) and len(param) == 2:
                  hand_idx_param, sac_idx = param
                  source_zone = "hand"
                  if hand_idx_param < len(player["hand"]):
                       card_id = player["hand"][hand_idx_param]
                       # Validate and perform sacrifice
                       if sac_idx < len(player["battlefield"]):
                            sac_id = player["battlefield"][sac_idx]
                            sac_card = gs._safe_get_card(sac_id)
                            if sac_card and 'creature' in getattr(sac_card, 'card_types', []):
                                gs.move_card(sac_id, player, "battlefield", player, "graveyard")
                                context["sacrificed_cmc"] = getattr(sac_card, 'cmc', 0) # Pass sacrificed CMC to cost calculation
                            else: return -0.1, False # Invalid sacrifice
                       else: return -0.1, False # Invalid sacrifice index
                  else: return -0.1, False # Invalid hand index
             else: return -0.2, False # Invalid param format
        elif action_type == "CAST_FOR_DELVE":
             # Param needs (Hand index, List[GY indices])
             if isinstance(param, tuple) and len(param) == 2:
                  hand_idx_param, gy_indices = param
                  source_zone = "hand"
                  if hand_idx_param < len(player["hand"]):
                       card_id = player["hand"][hand_idx_param]
                       # Validate and perform exile from GY
                       actual_gy_indices = [idx for idx in gy_indices if idx < len(player["graveyard"])]
                       if len(actual_gy_indices) > 0:
                           for gy_idx in sorted(actual_gy_indices, reverse=True): # Remove from end first
                                exile_id = player["graveyard"].pop(gy_idx)
                                player["exile"].append(exile_id)
                           context["delve_count"] = len(actual_gy_indices)
                       else: context["delve_count"] = 0
                  else: return -0.1, False # Invalid hand index
             else: return -0.2, False # Invalid param format
        else: # Flashback, Jump-Start, Escape, Aftermath
             gy_idx = param
             if gy_idx < len(player[source_zone]):
                  card_id = player[source_zone][gy_idx]
             else: return -0.1, False # Invalid index in source zone

        # Need special handling for Jump-Start discard
        if action_type == "CAST_WITH_JUMP_START":
            if len(player["hand"]) > 0:
                 # Auto-discard first card for now
                 discard_id = player["hand"].pop(0)
                 player["graveyard"].append(discard_id)
            else: return -0.1, False # No card to discard

        card = gs._safe_get_card(card_id)
        if not card: return -0.15, False

        # Remove card from source zone before adding to stack
        if source_zone == "hand" and hand_idx_param is not None:
             player["hand"].pop(hand_idx_param)
        elif source_zone == "graveyard":
             player["graveyard"].remove(card_id)
        elif source_zone == "exile" and action_type == "CAST_FOR_MADNESS":
             player["exile"].remove(card_id)

        success = gs.cast_spell(card_id, player, context=context)
        if success:
            return 0.25, True # Reward for successful alt cast
        else:
             # Return card to source zone if cast failed
             if source_zone == "hand": player["hand"].insert(hand_idx_param, card_id)
             elif source_zone == "graveyard": player["graveyard"].append(card_id)
             elif source_zone == "exile": player["exile"].append(card_id)
             # Reverse sacrifice/discard if applicable
             return -0.1, False
    
    def _handle_cast_split(self, param, action_type, **kwargs):
        """Handler for casting split cards."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if param < len(player["hand"]):
             card_id = player["hand"][param]
             context = {}
             if action_type == "CAST_LEFT_HALF": context["cast_left_half"] = True
             elif action_type == "CAST_RIGHT_HALF": context["cast_right_half"] = True
             elif action_type == "CAST_FUSE": context["fuse"] = True

             if gs.cast_spell(card_id, player, context=context):
                  return 0.15 # Reward for casting split
             else: return -0.1
        return -0.2
    
        # --- Specific Handler Implementations ---

    def _handle_alternative_casting(self, param, action_type, context=None, **kwargs):
        """Generic handler for alternative casting methods. (Updated)"""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Context passed from kwargs overrides base context
        if context is None: context = {}
        source_zone = "graveyard" # Default
        card_id = None
        hand_idx_param = None # Track hand index if source is hand

        alt_cost_name = action_type.replace('CAST_WITH_', '').replace('CAST_FOR_', '').replace('CAST_', '').replace('_',' ').lower()
        context["use_alt_cost"] = alt_cost_name

        # --- Determine card_id and source_zone ---
        if action_type == "CAST_FOR_MADNESS":
            source_zone = "exile"
            # Madness requires the card ID from the *game state's* madness trigger context
            if not hasattr(gs, 'madness_trigger') or not gs.madness_trigger or gs.madness_trigger.get('player') != player:
                 logging.warning("Madness cast action taken but no valid madness trigger found.")
                 return -0.1, False
            card_id = gs.madness_trigger.get("card_id")
            if not card_id or card_id not in player[source_zone]:
                 logging.warning("Madness trigger card not found in exile.")
                 return -0.1, False
        elif action_type in ["CAST_FOR_EMERGE", "CAST_FOR_DELVE"]:
            source_zone = "hand"
            # Requires hand_idx from context, not param
            if "hand_idx" not in context: return -0.1, False # Agent needs to provide this via context
            hand_idx_param = context["hand_idx"]
            if hand_idx_param >= len(player[source_zone]): return -0.1, False
            card_id = player[source_zone][hand_idx_param]

            # Handle additional costs for Emerge/Delve
            if action_type == "CAST_FOR_EMERGE":
                if "sacrifice_idx" not in context: return -0.1, False
                sac_idx = context["sacrifice_idx"]
                if sac_idx >= len(player["battlefield"]): return -0.1, False
                sac_id = player["battlefield"][sac_idx]
                sac_card = gs._safe_get_card(sac_id)
                if not sac_card or 'creature' not in getattr(sac_card, 'card_types', []): return -0.1, False
                gs.move_card(sac_id, player, "battlefield", player, "graveyard")
                context["sacrificed_cmc"] = getattr(sac_card, 'cmc', 0)
            elif action_type == "CAST_FOR_DELVE":
                if "gy_indices" not in context: return -0.1, False
                gy_indices = context["gy_indices"]
                actual_gy_indices = [idx for idx in gy_indices if idx < len(player["graveyard"])]
                if len(actual_gy_indices) == 0 and len(gy_indices) > 0:
                     logging.warning("No valid GY cards provided for Delve cost.")
                     # Don't necessarily fail, might pay full cost. Delve count affects mana cost reduction.
                     context["delve_count"] = 0
                else:
                     context["delve_cards"] = []
                     for gy_idx in sorted(actual_gy_indices, reverse=True):
                          exile_id = player["graveyard"].pop(gy_idx)
                          player["exile"].append(exile_id)
                          context["delve_cards"].append(exile_id)
                     context["delve_count"] = len(context["delve_cards"])

        else: # Flashback, Jump-Start, Escape, Aftermath
             source_zone_idx = param # Param is the index in the source zone (GY)
             if source_zone_idx is None or source_zone_idx >= len(player[source_zone]): return -0.1, False
             card_id = player[source_zone][source_zone_idx]

             if action_type == "CAST_WITH_JUMP_START":
                  if "discard_idx" not in context: return -0.1, False # Context needs discard choice
                  discard_idx = context["discard_idx"]
                  if discard_idx >= len(player["hand"]): return -0.1, False
                  discard_id = player["hand"].pop(discard_idx)
                  player["graveyard"].append(discard_id)
             elif action_type == "CAST_WITH_ESCAPE":
                  if "gy_indices_escape" not in context: return -0.1, False
                  gy_indices_escape = context["gy_indices_escape"]
                  # Escape cost needs parsing to know required exile count
                  card = gs._safe_get_card(card_id)
                  required_exile_count = 0 # Placeholder - requires parsing card.escape_cost
                  match = re.search(r"escape[^\n]*, exile (\w+|\d+)", getattr(card, 'oracle_text','').lower())
                  if match:
                       count_str = match.group(1)
                       if count_str.isdigit(): required_exile_count = int(count_str)
                       elif count_str == "one": required_exile_count = 1
                       elif count_str == "two": required_exile_count = 2
                       # ... add more word numbers
                       else: required_exile_count = 1 # Default? Or fail?

                  actual_gy_indices = [idx for idx in gy_indices_escape if idx < len(player["graveyard"])]
                  if len(actual_gy_indices) < required_exile_count:
                       logging.warning(f"Not enough GY cards selected for Escape ({len(actual_gy_indices)}/{required_exile_count})")
                       # Rollback Jump-Start discard if applicable
                       if action_type == "CAST_WITH_JUMP_START" and discard_id:
                           player["graveyard"].remove(discard_id)
                           player["hand"].append(discard_id)
                       return -0.1, False

                  context["escape_cards"] = []
                  for gy_idx in sorted(actual_gy_indices, reverse=True):
                      exile_id = player["graveyard"].pop(gy_idx)
                      player["exile"].append(exile_id)
                      context["escape_cards"].append(exile_id)

        if not card_id: return -0.2, False

        # --- Prepare for casting (Remove from source) ---
        # The cast_spell method handles this now if source_zone is provided in context
        context["source_zone"] = source_zone

        # --- Cast the spell ---
        success = gs.cast_spell(card_id, player, context=context)

        if success:
            # Clear madness trigger state if successful
            if action_type == "CAST_FOR_MADNESS": gs.madness_trigger = None
            return 0.25, True
        else:
            # Rollback zone change and costs
            # Note: cast_spell should ideally handle rollback on failure
            logging.warning(f"Alternative cast failed for {action_type} {card_id}")
            # Minimal rollback attempt: put card back if cast_spell didn't
            if not gs.find_card_location(card_id):
                if source_zone == "hand" and hand_idx_param is not None: player["hand"].insert(hand_idx_param, card_id)
                elif source_zone in player: player[source_zone].append(card_id)
            # TODO: Rollback sacrifice/discard/exile more robustly
            return -0.1, False

    def _handle_cast_split(self, param, action_type, **kwargs):
        """Handler for casting split cards. (Updated Context)"""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        hand_idx = param # Param is hand index

        if hand_idx < len(player["hand"]):
            card_id = player["hand"][hand_idx]
            card = gs._safe_get_card(card_id)
            if not card: return -0.2, False

            context = {"source_zone": "hand"} # Provide source zone
            if action_type == "CAST_LEFT_HALF": context["cast_left_half"] = True
            elif action_type == "CAST_RIGHT_HALF": context["cast_right_half"] = True
            elif action_type == "CAST_FUSE": context["fuse"] = True

            # Use CardEvaluator to estimate value
            eval_context = {"situation": "casting", **context} # Merge context
            card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context) if self.card_evaluator else 0.0

            if gs.cast_spell(card_id, player, context=context):
                return 0.15 + card_value * 0.2, True # Base reward + value mod
            else:
                return -0.1, False
        return -0.2, False

    # --- Combat Handler Wrappers ---
    def _handle_declare_attackers_done(self, param, **kwargs):
         return 0.05 if apply_combat_action(self.game_state, "DECLARE_ATTACKERS_DONE", param) else -0.1
    def _handle_declare_blockers_done(self, param, **kwargs):
         return 0.05 if apply_combat_action(self.game_state, "DECLARE_BLOCKERS_DONE", param) else -0.1
    def _handle_attack_planeswalker(self, param, **kwargs):
         return 0.1 if apply_combat_action(self.game_state, "ATTACK_PLANESWALKER", param) else -0.1
    def _handle_assign_multiple_blockers(self, param, **kwargs):
         return 0.1 if apply_combat_action(self.game_state, "ASSIGN_MULTIPLE_BLOCKERS", param) else -0.1
    def _handle_first_strike_order(self, param, **kwargs):
         return 0.05 if apply_combat_action(self.game_state, "FIRST_STRIKE_ORDER", param) else -0.1
    def _handle_assign_combat_damage(self, param, **kwargs):
         # Param might be manual assignments, or None for auto
         return 0.05 if apply_combat_action(self.game_state, "ASSIGN_COMBAT_DAMAGE", param) else -0.1
    def _handle_protect_planeswalker(self, param, **kwargs):
         return 0.15 if apply_combat_action(self.game_state, "PROTECT_PLANESWALKER", param) else -0.1
     
    def _handle_attack_battle(self, param, **kwargs):
         # Param needs to be (attacker_idx, battle_idx)
         # The ACTION_MEANING needs fixing.
         # We need to select an attacker.
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         # Select first valid attacker? This needs better logic.
         attacker_idx = -1
         for idx, cid in enumerate(player["battlefield"]):
             if self.is_valid_attacker(cid):
                 attacker_idx = idx
                 break
         if attacker_idx != -1 and param is not None:
             # Store mapping for combat handler
             gs._battle_attack_creatures = getattr(gs, '_battle_attack_creatures', {})
             gs._battle_attack_creatures[param] = attacker_idx # Map battle_idx to creature_idx
             return 0.1 if apply_combat_action(gs, "ATTACK_BATTLE", param) else -0.1
         return -0.15 # No valid attacker or battle index

    def _handle_defend_battle(self, param, **kwargs):
         return 0.1 if apply_combat_action(self.game_state, "DEFEND_BATTLE", param) else -0.1
     
    def _handle_ninjutsu(self, param, **kwargs):
         # Param needs (ninja_hand_idx, attacker_idx)
         # Simple version: assume first ninja, first unblocked attacker
         ninja_idx = -1
         attacker_id = None
         # Find ninja
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         for idx, cid in enumerate(player["hand"]):
              card = gs._safe_get_card(cid)
              if card and "ninjutsu" in getattr(card, 'oracle_text', '').lower():
                   ninja_idx = idx
                   break
         # Find unblocked attacker
         unblocked = [aid for aid in gs.current_attackers if aid not in gs.current_block_assignments or not gs.current_block_assignments[aid]]
         if unblocked: attacker_id = unblocked[0]

         if ninja_idx != -1 and attacker_id is not None:
             return 0.3 if apply_combat_action(gs, "NINJUTSU", ninja_idx, attacker_id) else -0.1 # Pass both params
         return -0.15

    # --- Helper method to check blocking capability ---
    def _can_block(self, blocker_id, attacker_id):
         """Check if blocker_id can legally block attacker_id."""
         gs = self.game_state
         if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_check_block_restrictions'):
              return gs.combat_resolver._check_block_restrictions(attacker_id, blocker_id)
         # Basic fallback
         blocker = gs._safe_get_card(blocker_id)
         attacker = gs._safe_get_card(attacker_id)
         if not blocker or not attacker: return False
         if 'creature' not in getattr(blocker, 'card_types', []): return False
         # Check flying/reach vs flying
         has_flying_attacker = 'flying' in getattr(attacker, 'oracle_text', '').lower()
         if has_flying_attacker:
              has_flying_blocker = 'flying' in getattr(blocker, 'oracle_text', '').lower()
              has_reach_blocker = 'reach' in getattr(blocker, 'oracle_text', '').lower()
              if not has_flying_blocker and not has_reach_blocker:
                   return False
         # Check other evasion later (menace handled in multi-block)
         return True

    def _add_state_change_rewards(self, base_reward, previous_state, current_state):
        """Calculate rewards based on positive changes in game state."""
        reward = base_reward
        # Life total swing
        my_life_change = current_state["my_life"] - previous_state["my_life"]
        opp_life_change = previous_state["opp_life"] - current_state["opp_life"] # Positive if opponent lost life
        reward += my_life_change * 0.03 + opp_life_change * 0.05 # Weight opponent life loss slightly higher

        # Card advantage
        card_adv_change = (current_state["my_hand"] - previous_state["my_hand"]) - (current_state["opp_hand"] - previous_state["opp_hand"])
        reward += card_adv_change * 0.1

        # Board presence
        board_adv_change = (current_state["my_board"] - previous_state["my_board"]) - (current_state["opp_board"] - previous_state["opp_board"])
        reward += board_adv_change * 0.05

        # Power advantage
        power_adv_change = (current_state["my_power"] - previous_state["my_power"]) - (current_state["opp_power"] - previous_state["opp_power"])
        reward += power_adv_change * 0.02

        # Log detailed reward breakdown if significant change
        if abs(reward - base_reward) > 0.01:
             logging.debug(f"State Change Reward: Life: {(my_life_change * 0.03 + opp_life_change * 0.05):.2f}, "
                           f"Cards: {(card_adv_change * 0.1):.2f}, Board: {(board_adv_change * 0.05):.2f}, "
                           f"Power: {(power_adv_change * 0.02):.2f}")

        return reward
    
    def _get_escalate_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"escalate\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
        return None
    
    def _has_haste(self, card_id):
        """Centralized haste check using AbilityHandler."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card: return False
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
             return gs.ability_handler.check_keyword(card_id, "haste")
        return 'haste' in getattr(card,'oracle_text','').lower() # Fallback
        
    def _handle_play_card(self, player, hand_idx, is_land=False):
        """Handle playing a card from hand, considering land/spell rules."""
        gs = self.game_state
        try:
            card_id = player["hand"][hand_idx]
            card = gs.card_db[card_id]
            
            if is_land:
                if 'land' not in card.type_line:
                    logging.debug(f"Invalid action: {card.name} is not a land")
                    return 0  # Invalid action: not a land
                if player["land_played"]:
                    logging.debug(f"Invalid action: already played a land this turn")
                    return 0  # Already played a land this turn
                
                player["battlefield"].append(card_id)
                player["hand"].pop(hand_idx)
                player["land_played"] = True
                for idx, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                    player["mana_production"][color] += card.colors[idx]
                return 0.1  # Reduced reward for playing a land
                
            else:
                if 'land' in card.type_line:
                    logging.debug(f"Invalid action: can't cast {card.name} as a spell")
                    return 0  # Can't cast a land as a spell
                    
                # Check if can afford using mana_system if available
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                else:
                    # Simple check - at least some mana available  
                    can_afford = sum(player["mana_pool"].values()) > 0
                    
                if not can_afford:
                    logging.debug(f"Invalid action: can't afford {card.name}")
                    return 0  # Not enough mana
                
                if 'creature' in card.card_types:
                    # Mark creatures as having summoning sickness
                    if not hasattr(gs, 'summoning_sick'):
                        gs.summoning_sick = set()
                    gs.summoning_sick.add(card_id)
                
                # Add to stack instead of directly to battlefield
                gs.stack.append(("SPELL", card_id, player))
                player["hand"].pop(hand_idx)
                
                # Use mana_system to pay cost if available
                if hasattr(gs, 'mana_system'):
                    gs.mana_system.pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                else:
                    # Simple deduction - use all available mana
                    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                
                return 0.25  # Reduced reward for casting a spell
                
        except IndexError:
            logging.warning(f"Attempted to play a card at invalid hand index {hand_idx}.")
            return 
        
    def _add_instant_casting_actions(self, player, valid_actions, set_valid_action):
        """Add actions for casting instants and flash spells (only those valid at instant speed)."""
        gs = self.game_state
        
        for i in range(20, 28):
            hand_idx = i - 20
            if hand_idx < len(player["hand"]):
                card_id = player["hand"][hand_idx]
                card = gs._safe_get_card(card_id)
                
                if not card or not hasattr(card, 'card_types'):
                    continue
                    
                # Use mana_system if available, otherwise fall back to simpler check
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                else:
                    # Simple check - at least some mana available
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                # Check if card can be cast at instant speed
                is_instant_speed = 'instant' in card.card_types or (hasattr(card, 'oracle_text') and 'flash' in card.oracle_text.lower())
                
                if is_instant_speed and can_afford:
                    # Check if the spell requires targets
                    requires_target = False
                    valid_targets_exist = True
                    
                    if hasattr(card, 'oracle_text'):
                        requires_target = 'target' in card.oracle_text.lower()
                        
                        # If it requires targets, check if any are available
                        if requires_target:
                            valid_targets_exist = self._check_valid_targets_exist(card, player, 
                                                        gs.p2 if player == gs.p1 else gs.p1)
                    
                    # Only allow casting if valid targets exist (if required)
                    if requires_target and not valid_targets_exist:
                        continue
                        
                    # Check for Spree instant 
                    if hasattr(card, 'is_spree') and card.is_spree:
                        set_valid_action(i, f"PLAY_SPREE_INSTANT for {card.name}")
                    else:
                        # Regular instant playing
                        set_valid_action(i, f"PLAY_INSTANT for {card.name}")
                
                    # Check for Adventure with instant type
                    if hasattr(card, 'has_adventure') and card.has_adventure():
                        adventure_data = card.get_adventure_data()
                        if adventure_data and 'instant' in adventure_data.get('type', '').lower():
                            adventure_cost = adventure_data.get('cost', '')
                            if hasattr(gs, 'mana_system'):
                                can_afford_adventure = gs.mana_system.can_pay_mana_cost(player, adventure_cost)
                            else:
                                can_afford_adventure = sum(player["mana_pool"].values()) > 0
                            
                            if can_afford_adventure:
                                set_valid_action(196 + hand_idx, f"PLAY_ADVENTURE for {adventure_data.get('name', 'Unknown')}")
                                
    
                                
    def _can_afford_card(self, player, card_or_data, is_back_face=False, context=None):
        """Check affordability using ManaSystem, handling dict or Card object."""
        gs = self.game_state
        if context is None: context = {}
        if not hasattr(gs, 'mana_system') or not gs.mana_system:
            return sum(player.get("mana_pool", {}).values()) > 0 # Basic check

        if isinstance(card_or_data, dict): # E.g., back face data
            cost_str = card_or_data.get('mana_cost', '')
            card_id = card_or_data.get('id') # Need ID for context
        elif isinstance(card_or_data, Card):
            cost_str = getattr(card_or_data, 'mana_cost', '')
            card_id = getattr(card_or_data, 'card_id', None)
        else:
            return False # Invalid input

        if not cost_str and not context.get('use_alt_cost'): return True # Free spell (unless alt cost used)

        try:
            parsed_cost = gs.mana_system.parse_mana_cost(cost_str)
            # Apply cost modifiers based on context (Kicker, Additional, Alternative)
            final_cost = gs.mana_system.apply_cost_modifiers(player, parsed_cost, card_id, context)
            return gs.mana_system.can_pay_mana_cost(player, final_cost, context)
        except Exception as e:
            card_name = getattr(card_or_data, 'name', 'Unknown') if isinstance(card_or_data, Card) else card_or_data.get('name', 'Unknown')
            logging.warning(f"Error checking mana cost for '{card_name}': {e}")
            return False

    def _can_afford_cost_string(self, player, cost_string, context=None):
        """Check affordability directly from a cost string using ManaSystem."""
        gs = self.game_state
        if context is None: context = {}
        if not hasattr(gs, 'mana_system') or not gs.mana_system:
            return sum(player.get("mana_pool", {}).values()) > 0 # Basic check
        if not cost_string: return True

        try:
            parsed_cost = gs.mana_system.parse_mana_cost(cost_string)
            # No cost modifiers applied here, assumes string is the final cost
            return gs.mana_system.can_pay_mana_cost(player, parsed_cost, context)
        except Exception as e:
            logging.warning(f"Error checking mana cost string '{cost_string}': {e}")
            return False

    def _has_flash(self, card_id):
        """Check if card has flash keyword."""
        card = self.game_state._safe_get_card(card_id)
        return self._has_flash_text(getattr(card, 'oracle_text', ''))

    def _has_flash_text(self, oracle_text):
        """Check if oracle text contains flash keyword."""
        return oracle_text and 'flash' in oracle_text.lower()

    def _targets_available(self, card, caster, opponent):
        """Check target availability using TargetingSystem."""
        gs = self.game_state
        card_id = getattr(card, 'card_id', None)
        if not card_id or not hasattr(card, 'oracle_text') or 'target' not in card.oracle_text.lower():
            return True # No target needed or cannot check

        if hasattr(gs, 'targeting_system') and gs.targeting_system:
            try:
                valid_targets = gs.targeting_system.get_valid_targets(card_id, caster)
                return any(targets for targets in valid_targets.values())
            except Exception as e:
                 logging.warning(f"Error checking targets with TargetingSystem for {card.name}: {e}")
                 return True # Assume targets exist on error
        else:
            return True # Assume targets exist if no system


    def _add_ability_activation_actions(self, player, valid_actions, set_valid_action, is_sorcery_speed):
        """Add actions for activating abilities."""
        gs = self.game_state
        if not hasattr(gs, 'ability_handler'): return

        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if not card: continue

            abilities = gs.ability_handler.get_activated_abilities(card_id)
            for j, ability in enumerate(abilities):
                if j >= 3: break # Limit abilities per card

                # Check timing restriction
                requires_sorcery = "activate only as a sorcery" in getattr(ability, 'effect_text', '').lower()
                if requires_sorcery and not is_sorcery_speed: continue
                if not requires_sorcery and is_sorcery_speed: continue # If checking only sorcery speed

                if gs.ability_handler.can_activate_ability(card_id, j, player):
                    # Check activation limit
                    activation_count = sum(1 for act_id, act_idx in getattr(gs, 'abilities_activated_this_turn', [])
                                            if act_id == card_id and act_idx == j)
                    if activation_count < 3: # Limit activation
                       set_valid_action(100 + (i * 3) + j, f"ACTIVATE {card.name} ability {j}")

    def _add_land_tapping_actions(self, player, valid_actions, set_valid_action):
        """Add actions for tapping lands for mana or effects."""
        gs = self.game_state
        for i in range(min(len(player["battlefield"]), 20)): # Tap land indices 0-19
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and 'land' in getattr(card, 'type_line', '') and card_id not in player.get("tapped_permanents", set()):
                # Check for mana abilities
                if hasattr(card, 'oracle_text') and "add {" in card.oracle_text.lower():
                     set_valid_action(68 + i, f"TAP_LAND_FOR_MANA {card.name}")
                # Check for other tap abilities
                if hasattr(card, 'oracle_text') and "{t}:" in card.oracle_text.lower() and "add {" not in card.oracle_text.lower():
                     if i < 12: # Tap land for effect indices 0-11
                          set_valid_action(88 + i, f"TAP_LAND_FOR_EFFECT {card.name}")

    def _add_exile_casting_actions(self, player, valid_actions, set_valid_action):
        """Add actions for casting spells from exile."""
        gs = self.game_state
        
        if hasattr(gs, 'cards_castable_from_exile'):
            for i, card_id in enumerate(gs.cards_castable_from_exile):
                if card_id in player["exile"] and i < 8:  # Limit to 8 exile castable cards
                    card = gs._safe_get_card(card_id)
                    if not card:
                        continue
                    
                    # Check if we can afford to cast it
                    if hasattr(gs, 'mana_system'):
                        can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                    else:
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
                        set_valid_action(230 + i, f"CAST_FROM_EXILE {card.name}")
                        
    def _add_token_copy_actions(self, player, valid_actions, set_valid_action):
        """Add actions for token creation and copying based on game context."""
        gs = self.game_state
        
        # Only show token/copy actions if we're in an appropriate context
        context_requires_token_action = False
        action_type = None
        
        # Check if there's a spell or ability on the stack that requires token/copy creation
        if gs.stack and len(gs.stack) > 0:
            stack_item = gs.stack[-1]
            
            if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                stack_type, card_id, controller = stack_item[:3]
                if controller == player:  # Only process if it's this player's spell/ability
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'oracle_text'):
                        oracle_text = card.oracle_text.lower()
                        
                        # Check for various token/copy patterns
                        if "create a" in oracle_text and "token" in oracle_text:
                            context_requires_token_action = True
                            action_type = "CREATE_TOKEN"
                        elif "copy target" in oracle_text and "permanent" in oracle_text:
                            context_requires_token_action = True
                            action_type = "COPY_PERMANENT"
                        elif "copy target" in oracle_text and "spell" in oracle_text:
                            context_requires_token_action = True
                            action_type = "COPY_SPELL"
                        elif "populate" in oracle_text:
                            context_requires_token_action = True
                            action_type = "POPULATE"
        
        # If we need to show token/copy actions, add them based on the context
        if context_requires_token_action:
            if action_type == "CREATE_TOKEN":
                # Auto-create token based on the card text - no action needed
                # This is typically automatic, but we could add options for token types if needed
                set_valid_action(405, "CREATE_TOKEN as specified by the spell/ability")
            
            elif action_type == "COPY_PERMANENT":
                # Add actions for copying permanents on the battlefield
                for idx, perm_id in enumerate(player["battlefield"][:5]):  # Limit to 5
                    card = gs._safe_get_card(perm_id)
                    if card:
                        set_valid_action(405 + idx, f"COPY_PERMANENT {card.name}")
            
            elif action_type == "COPY_SPELL":
                # Add actions for copying spells on the stack
                spells_on_stack = []
                for i, item in enumerate(gs.stack):
                    if item != stack_item and isinstance(item, tuple) and item[0] == "SPELL":
                        spell_id = item[1]
                        spell = gs._safe_get_card(spell_id)
                        if spell:
                            spells_on_stack.append((i, spell_id, spell))
                
                # Add action for each copyable spell
                if spells_on_stack:
                    for idx, (stack_idx, spell_id, spell) in enumerate(spells_on_stack[:5]):  # Limit to 5
                        set_valid_action(411, f"COPY_SPELL {spell.name} on the stack")
                
            elif action_type == "POPULATE":
                # Check if there are any token creatures to copy
                token_creatures = []
                for perm_id in player["battlefield"]:
                    card = gs._safe_get_card(perm_id)
                    if card and hasattr(card, 'is_token') and card.is_token and hasattr(card, 'card_types') and 'creature' in card.card_types:
                        token_creatures.append((perm_id, card))
                
                # Add populate action if there are token creatures
                if token_creatures:
                    set_valid_action(412, "POPULATE to create a copy of a creature token you control")

    def _add_specific_mechanics_actions(self, player, valid_actions, set_valid_action):
        """Add actions for specialized MTG mechanics."""
        gs = self.game_state
        
        # Investigate - when checking battlefield
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "investigate" in card.oracle_text.lower():
                set_valid_action(413, f"INVESTIGATE with {card.name}")

        # Foretell - when checking hand
        for i in range(20, 28):
            hand_idx = i - 20
            if hand_idx < len(player["hand"]):
                card_id = player["hand"][hand_idx]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "foretell" in card.oracle_text.lower():
                    set_valid_action(414, f"FORETELL {card.name}")

        # Adapt - when checking battlefield creatures
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "adapt" in card.oracle_text.lower():
                set_valid_action(420, f"ADAPT {card.name}")

        # Mutate - when checking battlefield creatures
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "mutate" in card.oracle_text.lower():
                set_valid_action(421, f"MUTATE {card.name}")

                
        # Boast - when checking battlefield creatures
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "boast" in card.oracle_text.lower():
                # Only allow boast if the creature attacked this turn
                if hasattr(gs, 'ability_handler'):
                    can_boast = gs.ability_handler._apply_boast(card_id, "ACTIVATE", {"controller": player})
                    if can_boast:
                        set_valid_action(424, f"BOAST with {card.name}")
                        
        
        # Amass - check for amass cards
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "amass" in card.oracle_text.lower():
                set_valid_action(415, f"AMASS with {card.name}")
        
        # Learn - check for learn cards
        for idx in range(min(len(player["hand"]), 8)):
            card_id = player["hand"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "learn" in card.oracle_text.lower():
                set_valid_action(416, f"LEARN with {card.name}")
        
        # Venture - check for venture cards
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "venture into the dungeon" in card.oracle_text.lower():
                set_valid_action(417, f"VENTURE with {card.name}")
        
        # Explore - check for explore cards
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "explore" in card.oracle_text.lower():
                set_valid_action(419, f"EXPLORE with {card.name}")
        
        # Morph - check for face-down cards that can be morphed
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_face_down') and card.is_face_down:
                set_valid_action(455, f"MORPH {card.name}")
        
        # Manifest - check for manifested cards
        for idx in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_manifested') and card.is_manifested:
                set_valid_action(456, f"MANIFEST {card.name}")
        
    def _tap_land_for_effect(self, player, land_id):
        """Tap a land to activate abilities (excluding mana production)."""
        gs = self.game_state
        
        # Get the land card
        card = gs._safe_get_card(land_id)
        if not card or 'land' not in card.type_line or land_id in player["tapped_permanents"]:
            return False
        
        # Mark the land as tapped
        player["tapped_permanents"].add(land_id)
        
        # Check for tap effects if ability handler exists
        if hasattr(gs, 'ability_handler'):
            gs.ability_handler.handle_tap_effects(land_id, player)
        
        # Trigger any "when this land becomes tapped" abilities
        if hasattr(gs, 'trigger_ability'):
            gs.trigger_ability(land_id, "TAPPED", {"controller": player})
        
        logging.debug(f"Tapped {card.name} for effect")
        return True
                    
    def _add_zone_movement_actions(self, player, valid_actions, set_valid_action):
        """Add actions for zone movement based on current game context."""
        gs = self.game_state
        
        # Only show zone movement actions if we're in an appropriate context
        context_requires_zone_action = False
        target_zone = None
        source_zone = None
        action_type = None
        
        # Check if there's a spell or ability on the stack that requires zone movement
        if gs.stack and len(gs.stack) > 0:
            stack_item = gs.stack[-1]
            
            if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                stack_type, card_id, controller = stack_item[:3]
                if controller == player:  # Only process if it's this player's spell/ability
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'oracle_text'):
                        oracle_text = card.oracle_text.lower()
                        
                        # Check for various zone movement patterns
                        if "return target" in oracle_text and "from your graveyard" in oracle_text:
                            context_requires_zone_action = True
                            target_zone = "hand"
                            source_zone = "graveyard"
                            action_type = "RETURN_FROM_GRAVEYARD"
                        elif "return target" in oracle_text and "from exile" in oracle_text:
                            context_requires_zone_action = True
                            target_zone = "hand"
                            source_zone = "exile"
                            action_type = "RETURN_FROM_EXILE"
                        elif "return" in oracle_text and "to the battlefield" in oracle_text and "from your graveyard" in oracle_text:
                            context_requires_zone_action = True
                            target_zone = "battlefield"
                            source_zone = "graveyard"
                            action_type = "REANIMATE"
        
        # Also check if we're in a specific phase that requires zone choices (like discard during cleanup)
        if gs.phase == gs.PHASE_CLEANUP and len(player["hand"]) > 7:
            context_requires_zone_action = True
            action_type = "DISCARD_CARD"
        
        # If we need to show zone movement actions, add them based on the context
        if context_requires_zone_action:
            if action_type == "RETURN_FROM_GRAVEYARD":
                # Add actions for selecting cards from graveyard
                for idx, card_id in enumerate(player["graveyard"][:6]):  # Limit to first 6 in graveyard
                    card = gs._safe_get_card(card_id)
                    if card:
                        set_valid_action(330 + idx, f"RETURN_FROM_GRAVEYARD {card.name} to hand")
            
            elif action_type == "RETURN_FROM_EXILE":
                # Add actions for selecting cards from exile
                for idx, card_id in enumerate(player["exile"][:6]):  # Limit to first 6 in exile
                    card = gs._safe_get_card(card_id)
                    if card:
                        set_valid_action(342 + idx, f"RETURN_FROM_EXILE {card.name} to hand")
            
            elif action_type == "REANIMATE":
                # Add actions for reanimating creatures from graveyard
                for idx, card_id in enumerate(player["graveyard"][:6]):  # Limit to first 6 in graveyard
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                        set_valid_action(336 + idx, f"REANIMATE {card.name} to battlefield")
            
            elif action_type == "DISCARD_CARD":
                # Add actions for discarding cards during cleanup
                for idx, card_id in enumerate(player["hand"][:10]):  # Support up to 10 hand positions
                    card = gs._safe_get_card(card_id)
                    if card:
                        set_valid_action(238 + idx, f"DISCARD_CARD {card.name}")
            
    def _add_alternative_casting_actions(self, player, valid_actions, set_valid_action, is_sorcery_speed):
        """Add actions for alternative casting costs."""
        gs = self.game_state
        # Flashback
        for i in range(min(len(player["graveyard"]), 6)): # GY index 0-5
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "flashback" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    cost_str = re.search(r"flashback (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_str and self._can_afford_cost_string(player, cost_str.group(1)):
                         set_valid_action(393, f"CAST_WITH_FLASHBACK {card.name}") # Param needs to be GY index `i`

        # Jump-start
        for i in range(min(len(player["graveyard"]), 6)): # GY index 0-5
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "jump-start" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    if len(player["hand"]) > 0 and self._can_afford_card(player, card):
                        set_valid_action(394, f"CAST_WITH_JUMP_START {card.name}") # Param needs to be GY index `i`

        # Escape
        for i in range(min(len(player["graveyard"]), 6)): # GY index 0-5
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "escape" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"escape—([^\,]+), exile ([^\.]+)", card.oracle_text.lower())
                    if cost_match:
                        cost_str = cost_match.group(1).strip()
                        exile_req_str = cost_match.group(2).strip()
                        exile_count_match = re.search(r"(\d+)", exile_req_str)
                        exile_count = int(exile_count_match.group(1)) if exile_count_match else 1
                        if len(player["graveyard"]) > exile_count and self._can_afford_cost_string(player, cost_str):
                             set_valid_action(395, f"CAST_WITH_ESCAPE {card.name}") # Param needs to be GY index `i`

        # Madness (Triggered when discarded, check if castable)
        # Need a state for "waiting_for_madness_cast"
        if hasattr(gs, 'madness_trigger') and gs.madness_trigger:
             card_id = gs.madness_trigger['card_id']
             card = gs._safe_get_card(card_id)
             if card and hasattr(card, 'oracle_text') and "madness" in card.oracle_text.lower():
                  cost_match = re.search(r"madness (\{[^\}]+\})", card.oracle_text.lower())
                  if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                      # Find card in exile (where it goes temporarily)
                      exile_idx = -1
                      for idx, ex_id in enumerate(player["exile"]):
                           if ex_id == card_id and idx < 8: # Exile index 0-7
                                exile_idx = idx
                                break
                      if exile_idx != -1:
                           set_valid_action(396, f"CAST_FOR_MADNESS {card.name}") # Param needs to be exile index

        # Overload
        for i in range(min(len(player["hand"]), 8)): # Hand index 0-7
            card_id = player["hand"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "overload" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"overload (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                        set_valid_action(397, f"CAST_WITH_OVERLOAD {card.name}") # Param needs to be hand index `i`

        # Emerge (Sorcery speed only)
        if is_sorcery_speed:
            for i in range(min(len(player["hand"]), 8)): # Hand index 0-7
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "emerge" in card.oracle_text.lower():
                    cost_match = re.search(r"emerge (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_match:
                        # Check if there's a creature to sacrifice
                        can_sac = any('creature' in getattr(gs._safe_get_card(cid), 'card_types', []) for cid in player["battlefield"])
                        if can_sac and self._can_afford_cost_string(player, cost_match.group(1)): # Simplified cost check
                             set_valid_action(398, f"CAST_FOR_EMERGE {card.name}") # Param needs (hand_idx, sac_idx)

        # Delve (Sorcery speed only)
        if is_sorcery_speed:
            for i in range(min(len(player["hand"]), 8)): # Hand index 0-7
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "delve" in card.oracle_text.lower():
                    if len(player["graveyard"]) > 0 and self._can_afford_card(player, card): # Simplified check
                        set_valid_action(399, f"CAST_FOR_DELVE {card.name}") # Param needs (hand_idx, List[GY_idx])

                    
    def _add_x_cost_actions(self, player, valid_actions, set_valid_action):
        """Add actions for X cost spells on the stack."""
        gs = self.game_state
        
        # Check if there is a spell with X in its cost on the stack
        if gs.stack and len(gs.stack) > 0:
            stack_item = gs.stack[-1]
            
            if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                stack_type, card_id, controller = stack_item[:3]
                
                # Only process if this is the player's spell
                if controller == player and stack_type == "SPELL":
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'mana_cost') and 'X' in card.mana_cost:
                        # Calculate available mana for X
                        available_mana = sum(player["mana_pool"].values())
                        
                        # Allow X values up to available mana (max 10)
                        max_x = min(available_mana, 10)
                        for i in range(max_x):
                            x_value = i + 1  # X values start at 1
                            set_valid_action(358 + i, f"CHOOSE_X_VALUE {x_value} for {card.name}")

    def _add_kicker_options(self, player, valid_actions, set_valid_action):
         """Add options for paying kicker."""
         gs = self.game_state
         # Check spells currently on the stack that belong to the player
         for item in gs.stack:
             if isinstance(item, tuple) and len(item) >= 3:
                 spell_type, card_id, controller = item[:3]
                 if spell_type == "SPELL" and controller == player:
                     card = gs._safe_get_card(card_id)
                     if card and hasattr(card, 'oracle_text') and "kicker" in card.oracle_text.lower():
                         # Check if kicker cost can be paid
                         cost_match = re.search(r"kicker (\{[^\}]+\})", card.oracle_text.lower())
                         if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                             set_valid_action(400, f"PAY_KICKER for {card.name}")
                         # Always allow not paying kicker if kicker is optional
                         set_valid_action(401, f"DON'T_PAY_KICKER for {card.name}")
                     # Check for additional costs similarly
                     if card and hasattr(card, 'oracle_text') and "additional cost" in card.oracle_text.lower():
                         # Simplified check for now
                         set_valid_action(402, f"PAY_ADDITIONAL_COST for {card.name}")
                         set_valid_action(403, f"DON'T_PAY_ADDITIONAL_COST for {card.name}")
                     # Check for escalate
                     if card and hasattr(card, 'oracle_text') and "escalate" in card.oracle_text.lower():
                          cost_match = re.search(r"escalate (\{[^\}]+\})", card.oracle_text.lower())
                          if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                              set_valid_action(404, f"PAY_ESCALATE for {card.name}")

    def _add_split_card_actions(self, player, valid_actions, set_valid_action):
        """Add actions for split cards."""
        gs = self.game_state
        
        for idx, card_id in enumerate(player["hand"][:8]):  # Limit to first 8
            card = gs._safe_get_card(card_id)
            
            # Check if it's a split card
            is_split = False
            if card and hasattr(card, 'layout'):
                is_split = card.layout == "split"
            elif card and hasattr(card, 'oracle_text') and "//" in card.oracle_text:
                is_split = True
            
            if is_split:
                # Extract information about both halves
                has_left_half = hasattr(card, 'left_half')
                has_right_half = hasattr(card, 'right_half')
                
                # Left half casting
                if has_left_half:
                    left_cost = card.left_half.get('mana_cost', card.mana_cost)
                    can_afford_left = False
                    if hasattr(gs, 'mana_system'):
                        can_afford_left = gs.mana_system.can_pay_mana_cost(player, left_cost)
                    else:
                        can_afford_left = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford_left:
                        set_valid_action(440, f"CAST_LEFT_HALF of {card.name}")
                
                # Right half casting
                if has_right_half:
                    right_cost = card.right_half.get('mana_cost', card.mana_cost)
                    can_afford_right = False
                    if hasattr(gs, 'mana_system'):
                        can_afford_right = gs.mana_system.can_pay_mana_cost(player, right_cost)
                    else:
                        can_afford_right = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford_right:
                        set_valid_action(441, f"CAST_RIGHT_HALF of {card.name}")
                
                # Fuse (both halves)
                if has_left_half and has_right_half and "fuse" in card.oracle_text.lower():
                    # Need to afford both costs
                    left_cost = card.left_half.get('mana_cost', card.mana_cost)
                    right_cost = card.right_half.get('mana_cost', card.mana_cost)
                    
                    total_cost = left_cost + right_cost  # This is a simplification
                    can_afford_both = False
                    if hasattr(gs, 'mana_system'):
                        can_afford_both = gs.mana_system.can_pay_mana_cost(player, total_cost)
                    else:
                        can_afford_both = sum(player["mana_pool"].values()) > 1  # At least 2 mana
                    
                    if can_afford_both:
                        set_valid_action(442, f"CAST_FUSE of {card.name}")
            
            # Check if it's an aftermath card
            is_aftermath = False
            if card and hasattr(card, 'layout'):
                is_aftermath = card.layout == "aftermath"
            elif card and hasattr(card, 'oracle_text') and "aftermath" in card.oracle_text.lower():
                is_aftermath = True
            
            # Add aftermath actions for graveyard
            if is_aftermath:
                for g_idx, g_card_id in enumerate(player["graveyard"][:6]):  # First 6 in graveyard
                    g_card = gs._safe_get_card(g_card_id)
                    if g_card and hasattr(g_card, 'layout') and g_card.layout == "aftermath":
                        # Check if it has a castable aftermath half
                        if hasattr(g_card, 'right_half'):
                            right_cost = g_card.right_half.get('mana_cost', g_card.mana_cost)
                            can_afford = False
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(player, right_cost)
                            else:
                                can_afford = sum(player["mana_pool"].values()) > 0
                            
                            if can_afford:
                                set_valid_action(443, f"AFTERMATH_CAST of {g_card.name}")

    def _add_counter_actions(self, player, valid_actions, set_valid_action):
        """Add actions for countering spells and abilities."""
        gs = self.game_state
        
        # Check if there are spells on the stack
        if gs.stack:
            # Check for counter spells in hand
            for idx, card_id in enumerate(player["hand"][:8]):  # Limit to first 8
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "counter target spell" in card.oracle_text.lower():
                    # Check if we can afford it
                    can_afford = False
                    if hasattr(gs, 'mana_system'):
                        can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                    else:
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
                        set_valid_action(425, f"COUNTER_SPELL with {card.name}")
                
                # Check for ability counters
                if card and hasattr(card, 'oracle_text') and "counter target activated ability" in card.oracle_text.lower():
                    # Check if we can afford it
                    can_afford = False
                    if hasattr(gs, 'mana_system'):
                        can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                    else:
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
                        set_valid_action(426, f"COUNTER_ABILITY with {card.name}")
                        
                # Check for stifle effects
                if card and hasattr(card, 'oracle_text') and "counter target triggered ability" in card.oracle_text.lower():
                    # Check if we can afford it
                    can_afford = False
                    if hasattr(gs, 'mana_system'):
                        can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                    else:
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
                        set_valid_action(429, f"STIFLE_TRIGGER with {card.name}")

    def _add_damage_prevention_actions(self, player, valid_actions, set_valid_action):
        """Add actions for preventing or redirecting damage."""
        gs = self.game_state
        
        # Check if damage is being dealt (only relevant in combat or with damage effects on stack)
        is_damage_being_dealt = gs.phase in [gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE]
        
        if is_damage_being_dealt or gs.stack:
            # Check for prevention effects in hand
            for idx, card_id in enumerate(player["hand"][:8]):  # Limit to first 8
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text'):
                    if "prevent" in card.oracle_text.lower() and "damage" in card.oracle_text.lower():
                        # Check if we can afford it
                        can_afford = False
                        if hasattr(gs, 'mana_system'):
                            can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                        else:
                            can_afford = sum(player["mana_pool"].values()) > 0
                        
                        if can_afford:
                            set_valid_action(427, f"PREVENT_DAMAGE with {card.name}")
                    
                    if "redirect" in card.oracle_text.lower() and "damage" in card.oracle_text.lower():
                        # Check if we can afford it
                        can_afford = False
                        if hasattr(gs, 'mana_system'):
                            can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                        else:
                            can_afford = sum(player["mana_pool"].values()) > 0
                        
                        if can_afford:
                            set_valid_action(428, f"REDIRECT_DAMAGE with {card.name}")


    def resolve_stack_item(self):
        """
        Resolve the top item on the stack if priority has been passed appropriately.
        
        Returns:
            bool: Whether an item was resolved
        """
        gs = self.game_state
        
        # Check if both players have passed priority
        if gs.priority_pass_count >= 2 and gs.stack:
            # Process any triggered abilities first
            if hasattr(gs, 'ability_handler') and gs.ability_handler:
                gs.ability_handler.process_triggered_abilities()
                
            # Resolve top of stack
            gs.resolve_top_of_stack()  # Changed from gs._resolve_top_of_stack()
            
            # Reset priority
            gs.priority_pass_count = 0
            gs.priority_player = gs._get_active_player()
            return True
            
        return False 
    
    def _get_obs_safe(self):
        """Return a minimal, safe observation dictionary in case of errors."""
        gs = self.game_state
        obs = {k: np.zeros(space.shape, dtype=space.dtype)
               for k, space in self.observation_space.spaces.items()}
        # Fill minimal necessary fields
        obs["phase"] = gs.phase if hasattr(gs, 'phase') else 0
        obs["turn"] = np.array([gs.turn if hasattr(gs, 'turn') else 1], dtype=np.int32)
        obs["my_life"] = np.array([gs.p1["life"] if gs.agent_is_p1 else gs.p2["life"]], dtype=np.int32)
        obs["opp_life"] = np.array([gs.p2["life"] if gs.agent_is_p1 else gs.p1["life"]], dtype=np.int32)
        obs["action_mask"] = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
        obs["action_mask"][11] = True # Pass priority
        obs["action_mask"][12] = True # Concede
        return obs