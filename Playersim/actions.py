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
# Added Context Required comments
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
    # Context Required (Optional): {'target_attacker_id': id_or_idx} (If agent specifies blocker target)
    **{i: ("BLOCK", i-48) for i in range(48, 68)},

    # Tap land for mana (68-87) = 20 actions (param=battlefield index 0-19)
    **{i: ("TAP_LAND_FOR_MANA", i-68) for i in range(68, 88)},

    # Tap land for effect (88-99) = 12 actions (param=battlefield index 0-11)
    # Context Required: {'ability_idx': int} (Typically 0 for land effect, handled by context if ambiguous)
    **{i: ("TAP_LAND_FOR_EFFECT", i-88) for i in range(88, 100)},

    # Activate Ability (100-159) = 60 actions
    # Param=None. Handler expects {'battlefield_idx': int, 'ability_idx': int} in context.
    **{100 + (i * 3) + j: ("ACTIVATE_ABILITY", None) for i in range(20) for j in range(3)},

    # Transform (160-179) = 20 actions (param=battlefield index 0-19)
    **{160 + i: ("TRANSFORM", i) for i in range(20)},

    # MDFC Land Back (180-187) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_MDFC_LAND_BACK", i-180) for i in range(180, 188)},

    # MDFC Spell Back (188-195) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_MDFC_BACK", i-188) for i in range(188, 196)},

    # Adventure (196-203) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_ADVENTURE", i-196) for i in range(196, 204)},

    # Defend Battle (204-223) = 20 actions
    # Param=None. Handler expects {'battle_identifier': id_or_idx, 'defender_identifier': id_or_idx} in context.
    **{204 + (i * 4) + j: ("DEFEND_BATTLE", None) for i in range(5) for j in range(4)},

    # NO_OP (224)
    224: ("NO_OP", None),

    # Mulligan (225-229) = 5 actions
    225: ("KEEP_HAND", None),
    # Param is hand index (0-3) for card selection to bottom.
    **{226 + i: ("BOTTOM_CARD", i) for i in range(4)},

    # Cast from Exile (230-237) = 8 actions (param=relative index 0-7 into castable exile list)
    **{i: ("CAST_FROM_EXILE", i-230) for i in range(230, 238)},

    # Discard (238-247) = 10 actions (param=hand index 0-9)
    **{238 + i: ("DISCARD_CARD", i) for i in range(10)},

    # Room/Class (248-257) = 10 actions
    # Param=battlefield index 0-4
    **{248 + i: ("UNLOCK_DOOR", i) for i in range(5)},
    # Param=battlefield index 0-4
    **{253 + i: ("LEVEL_UP_CLASS", i) for i in range(5)},

    # Spree Mode (258-273) = 16 actions
    # Param=None. Handler expects {'hand_idx': int, 'mode_idx': int} in context.
    **{258 + (i * 2) + j: ("SELECT_SPREE_MODE", None) for i in range(8) for j in range(2)},

    **{274 + i: ("SELECT_TARGET", i) for i in range(10)},
    **{284 + i: ("SACRIFICE_PERMANENT", i) for i in range(10)},

    # --- Offspring/Impending custom actions (indices 294, 295) ---
    294: ("CAST_FOR_IMPENDING", None),  # Player chooses to cast a card for its Impending cost (Context={'hand_idx': X})
    295: ("PAY_OFFSPRING_COST", None),  # Player chooses to pay Offspring cost for the pending spell (Context implicit via gs.pending_spell_context)

    # Gaps filled with NO_OP (296-298) = 3 actions
    **{i: ("NO_OP", None) for i in range(296, 299)},

    # Library/Card Movement (299-308) -> Corrected (Now starts at 299)
    **{299 + i: ("SEARCH_LIBRARY", i) for i in range(5)}, # 299-303
    304: ("NO_OP_SEARCH_FAIL", None),                   # 304
    305: ("PUT_TO_GRAVEYARD", None),                    # 305 (Surveil GY)
    306: ("PUT_ON_TOP", None),                          # 306 (Scry/Surveil Top)
    307: ("PUT_ON_BOTTOM", None),                       # 307 (Scry Bottom)
    308: ("DREDGE", None), # Handler expects {'gy_idx': int} in context # 308

    # Gap filling (309-313 -> previously 314-318)
    **{i: ("NO_OP", None) for i in range(309, 314)}, # 5 NO_OPs # 309-313

    # Counter Management (314-334 -> previously 309-329+prolif = 314-334)
    **{314 + i: ("ADD_COUNTER", i) for i in range(10)},         # 314-323
    **{324 + i: ("REMOVE_COUNTER", i) for i in range(10)},      # 324-333
    334: ("PROLIFERATE", None),                                 # 334

    # Zone Movement (335-352 -> previously 330-347)
    **{335 + i: ("RETURN_FROM_GRAVEYARD", i) for i in range(6)},# 335-340
    **{341 + i: ("REANIMATE", i) for i in range(6)},            # 341-346
    **{347 + i: ("RETURN_FROM_EXILE", i) for i in range(6)},    # 347-352

    # Modal/Choice (353-377 -> previously 348-372)
    **{353 + i: ("CHOOSE_MODE", i) for i in range(10)},         # 353-362
    **{363 + i: ("CHOOSE_X_VALUE", i+1) for i in range(10)},    # 363-372
    **{373 + i: ("CHOOSE_COLOR", i) for i in range(5)},         # 373-377

    # Advanced Combat (378-397 -> previously 373-377, 383-392)
    **{378 + i: ("ATTACK_PLANESWALKER", i) for i in range(5)},  # 378-382
    **{i: ("NO_OP", None) for i in range(383, 388)}, # 5 NO_OPs # 383-387
    **{388 + i: ("ASSIGN_MULTIPLE_BLOCKERS", i) for i in range(10)}, # 388-397

    # Alternative Casting (398-409 -> previously 393-404)
    398: ("CAST_WITH_FLASHBACK", None),
    399: ("CAST_WITH_JUMP_START", None),
    400: ("CAST_WITH_ESCAPE", None),
    401: ("CAST_FOR_MADNESS", None),
    402: ("CAST_WITH_OVERLOAD", None),
    403: ("CAST_FOR_EMERGE", None),
    404: ("CAST_FOR_DELVE", None),
    405: ("PAY_KICKER", True),
    406: ("PAY_KICKER", False),
    407: ("PAY_ADDITIONAL_COST", True),
    408: ("PAY_ADDITIONAL_COST", False),
    409: ("PAY_ESCALATE", None),

    # Token/Copy (410-417 -> previously 405-412)
    **{410 + i: ("CREATE_TOKEN", i) for i in range(5)}, # 410-414
    415: ("COPY_PERMANENT", None),
    416: ("COPY_SPELL", None),
    417: ("POPULATE", None),

    # Specific Mechanics (418-429 -> previously 413-424)
    418: ("INVESTIGATE", None),
    419: ("FORETELL", None),
    420: ("AMASS", None),
    421: ("LEARN", None),
    422: ("VENTURE", None),
    423: ("EXERT", None),
    424: ("EXPLORE", None),
    425: ("ADAPT", None),
    426: ("MUTATE", None),
    427: ("CYCLING", None),
    428: ("GOAD", None),
    429: ("BOAST", None),

    # Response Actions (430-434 -> previously 425-429)
    430: ("COUNTER_SPELL", None),
    431: ("COUNTER_ABILITY", None),
    432: ("PREVENT_DAMAGE", None),
    433: ("REDIRECT_DAMAGE", None),
    434: ("STIFLE_TRIGGER", None),

    # Combat Actions (435-444 -> previously 430-439)
    435: ("FIRST_STRIKE_ORDER", None),
    436: ("ASSIGN_COMBAT_DAMAGE", None),
    437: ("NINJUTSU", None),
    438: ("DECLARE_ATTACKERS_DONE", None),
    439: ("DECLARE_BLOCKERS_DONE", None),
    440: ("LOYALTY_ABILITY_PLUS", None),
    441: ("LOYALTY_ABILITY_ZERO", None),
    442: ("LOYALTY_ABILITY_MINUS", None),
    443: ("ULTIMATE_ABILITY", None),
    444: ("PROTECT_PLANESWALKER", None),

    # Card Type Specific (445-461 -> previously 440-459)
    445: ("CAST_LEFT_HALF", None),
    446: ("CAST_RIGHT_HALF", None),
    447: ("CAST_FUSE", None),
    448: ("AFTERMATH_CAST", None),
    449: ("FLIP_CARD", None),
    450: ("EQUIP", None),
    451: ("UNEQUIP", None), # Restore UNEQUIP? Needs logic. Let's keep NO_OP for now.
    452: ("ATTACH_AURA", None), # Restore? Needs logic. NO_OP.
    453: ("FORTIFY", None),
    454: ("RECONFIGURE", None),
    455: ("MORPH", None),
    456: ("MANIFEST", None),
    457: ("CLASH", None),
    458: ("CONSPIRE", None),
    459: ("CONVOKE", None), # Restore CONVOKE? Needs logic. NO_OP.
    460: ("GRANDEUR", None),
    461: ("HELLBENT", None), # Restore HELLBENT? Needs logic. NO_OP.

    # Attack Battle (462-466 -> previously 460-464)
    **{462 + i: ("ATTACK_BATTLE", i) for i in range(5)}, # 462-466

    # Fill the remaining space (467-479)
    **{i: ("NO_OP", None) for i in range(467, 480)}
}
# Ensure size again after changes
if len(ACTION_MEANINGS) != 480:
    raise ValueError(f"ACTION_MEANINGS size IS STILL INCORRECT: {len(ACTION_MEANINGS)}")

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
            """Maps action type strings to their handler methods. (Updated)"""
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
                # Delegated Combat Actions (Passed through apply_combat_action)
                "DECLARE_ATTACKERS_DONE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DECLARE_ATTACKERS_DONE", p, context=context),
                "DECLARE_BLOCKERS_DONE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DECLARE_BLOCKERS_DONE", p, context=context),
                "ATTACK_PLANESWALKER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ATTACK_PLANESWALKER", p, context=context),
                "ASSIGN_MULTIPLE_BLOCKERS": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ASSIGN_MULTIPLE_BLOCKERS", p, context=context),
                "FIRST_STRIKE_ORDER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "FIRST_STRIKE_ORDER", p, context=context),
                "ASSIGN_COMBAT_DAMAGE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ASSIGN_COMBAT_DAMAGE", p, context=context),
                "PROTECT_PLANESWALKER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "PROTECT_PLANESWALKER", p, context=context),
                "ATTACK_BATTLE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ATTACK_BATTLE", p, context=context),
                "DEFEND_BATTLE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DEFEND_BATTLE", p, context=context),
                "NINJUTSU": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "NINJUTSU", p, context=context),
                # Abilities & Mana
                "TAP_LAND_FOR_MANA": self._handle_tap_land_for_mana,
                "TAP_LAND_FOR_EFFECT": self._handle_tap_land_for_effect,
                "ACTIVATE_ABILITY": self._handle_activate_ability, # Now expects action_index in kwargs
                "LOYALTY_ABILITY_PLUS": lambda p=None, context=None, **k: self._handle_loyalty_ability(p, context=context, action_type="LOYALTY_ABILITY_PLUS", **k),
                "LOYALTY_ABILITY_ZERO": lambda p=None, context=None, **k: self._handle_loyalty_ability(p, context=context, action_type="LOYALTY_ABILITY_ZERO", **k),
                "LOYALTY_ABILITY_MINUS": lambda p=None, context=None, **k: self._handle_loyalty_ability(p, context=context, action_type="LOYALTY_ABILITY_MINUS", **k),
                "ULTIMATE_ABILITY": lambda p=None, context=None, **k: self._handle_loyalty_ability(p, context=context, action_type="ULTIMATE_ABILITY", **k),
                # Targeting & Choices
                "SELECT_TARGET": self._handle_select_target, # Param is index into valid choices
                "SACRIFICE_PERMANENT": self._handle_sacrifice_permanent, # Param is index into valid choices
                "CHOOSE_MODE": self._handle_choose_mode, # Param is mode index
                "CHOOSE_X_VALUE": self._handle_choose_x, # Param is X value
                "CHOOSE_COLOR": self._handle_choose_color, # Param is color index 0-4
                "PUT_TO_GRAVEYARD": self._handle_scry_surveil_choice, # Updated mapping for surveil GY choice
                "PUT_ON_TOP": self._handle_scry_surveil_choice, # Updated mapping
                "PUT_ON_BOTTOM": self._handle_scry_surveil_choice, # Updated mapping
                # Library/Card Movement
                "SEARCH_LIBRARY": self._handle_search_library, # Param is search type 0-4
                "DREDGE": self._handle_dredge, # Handler expects {'gy_idx': int} in context
                # Counter Management
                "ADD_COUNTER": self._handle_add_counter, # Param is target index 0-9, context needed
                "REMOVE_COUNTER": self._handle_remove_counter, # Param is target index 0-9, context needed
                "PROLIFERATE": self._handle_proliferate,
                # Zone Movement
                "RETURN_FROM_GRAVEYARD": self._handle_return_from_graveyard, # Param is GY index 0-5
                "REANIMATE": self._handle_reanimate, # Param is GY index 0-5
                "RETURN_FROM_EXILE": self._handle_return_from_exile, # Param is Exile index 0-5
                # Alternative Casting
                "CAST_WITH_FLASHBACK": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_WITH_FLASHBACK", context=context, **k),
                "CAST_WITH_JUMP_START": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_WITH_JUMP_START", context=context, **k),
                "CAST_WITH_ESCAPE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_WITH_ESCAPE", context=context, **k),
                "CAST_FOR_MADNESS": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_FOR_MADNESS", context=context, **k),
                "CAST_WITH_OVERLOAD": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_WITH_OVERLOAD", context=context, **k),
                "CAST_FOR_EMERGE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_FOR_EMERGE", context=context, **k),
                "CAST_FOR_DELVE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_FOR_DELVE", context=context, **k),
                "AFTERMATH_CAST": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="AFTERMATH_CAST", context=context, **k),
                # Informational Flags
                "PAY_KICKER": self._handle_pay_kicker, # Param=True/False
                "PAY_ADDITIONAL_COST": self._handle_pay_additional_cost, # Param=True/False
                "PAY_ESCALATE": self._handle_pay_escalate, # Param=count
                # Token/Copy
                "CREATE_TOKEN": self._handle_create_token, # Param is predefined token index 0-4
                "COPY_PERMANENT": self._handle_copy_permanent, # Param=None, Context={'target_permanent_identifier':X}
                "COPY_SPELL": self._handle_copy_spell, # Param=None, Context={'target_stack_identifier':X}
                "POPULATE": self._handle_populate, # Param=None, Context={'target_token_identifier':X}
                # Specific Mechanics
                "INVESTIGATE": self._handle_investigate,
                "FORETELL": self._handle_foretell, # Param=None, Context={'hand_idx':X}
                "AMASS": self._handle_amass, # Param=None, Context={'amount':X}
                "LEARN": self._handle_learn,
                "VENTURE": self._handle_venture,
                "EXERT": self._handle_exert, # Param=None, Context={'creature_idx':X}
                "EXPLORE": self._handle_explore, # Param=None, Context={'creature_idx':X}
                "ADAPT": self._handle_adapt, # Param=None, Context={'creature_idx':X, 'amount':Y}
                "MUTATE": self._handle_mutate, # Param=None, Context={'hand_idx':X, 'target_idx':Y}
                "CYCLING": self._handle_cycling, # Param=None, Context={'hand_idx':X}
                "GOAD": self._handle_goad, # Param=None, Context={'target_creature_identifier':X}
                "BOAST": self._handle_boast, # Param=None, Context={'creature_idx':X}
                # Response Actions
                "COUNTER_SPELL": self._handle_counter_spell, # Param=None, Context={'hand_idx':X, 'target_spell_idx':Y}
                "COUNTER_ABILITY": self._handle_counter_ability, # Param=None, Context={'hand_idx':X, 'target_ability_idx':Y}
                "PREVENT_DAMAGE": self._handle_prevent_damage, # Param=None, Context={'hand_idx':X, ...}
                "REDIRECT_DAMAGE": self._handle_redirect_damage, # Param=None, Context={'hand_idx':X, ...}
                "STIFLE_TRIGGER": self._handle_stifle_trigger, # Param=None, Context={'hand_idx':X, 'target_trigger_idx':Y}
                # Card Type Specific
                "CAST_LEFT_HALF": lambda p=None, context=None, **k: self._handle_cast_split(p, context=context, action_type="CAST_LEFT_HALF", **k), # Param is hand_idx
                "CAST_RIGHT_HALF": lambda p=None, context=None, **k: self._handle_cast_split(p, context=context, action_type="CAST_RIGHT_HALF", **k),# Param is hand_idx
                "CAST_FUSE": lambda p=None, context=None, **k: self._handle_cast_split(p, context=context, action_type="CAST_FUSE", **k),# Param is hand_idx
                "FLIP_CARD": self._handle_flip_card, # Param=None, Context={'battlefield_idx':X}
                "EQUIP": self._handle_equip, # Param=None, Context={'equip_identifier':X, 'target_identifier':Y}
                "FORTIFY": self._handle_fortify, # Param=None, Context={'fort_identifier':X, 'target_identifier':Y}
                "RECONFIGURE": self._handle_reconfigure, # Param=None, Context={'card_identifier':X}
                "MORPH": self._handle_morph, # Param=None, Context={'battlefield_idx':X}
                "MANIFEST": self._handle_manifest, # Param=None, Context={'battlefield_idx':X}
                "CLASH": self._handle_clash,
                "CONSPIRE": self._handle_conspire, # Param=None, Context={'spell_stack_idx':X, 'creature1_identifier':Y, 'creature2_identifier':Z}
                "GRANDEUR": self._handle_grandeur, # Param=None, Context={'hand_idx':X}
                # Room/Class (Delegated to _handle_ methods)
                "UNLOCK_DOOR": self._handle_unlock_door, # Param is room battlefield_idx
                "LEVEL_UP_CLASS": self._handle_level_up_class, # Param is class battlefield_idx
                # Discard / Spree
                "DISCARD_CARD": self._handle_discard_card, # Param is hand_idx
                "SELECT_SPREE_MODE": self._handle_select_spree_mode, # Param=None, Context={'hand_idx':X, 'mode_idx':Y}
                # Transform
                "TRANSFORM": self._handle_transform, # Param is battlefield index
                # NO_OP variants
                "NO_OP": self._handle_no_op,
                "NO_OP_SEARCH_FAIL": self._handle_no_op,
                # Actions removed or repurposed to NO_OP
                "UNEQUIP": self._handle_no_op,
                "ATTACH_AURA": self._handle_no_op,
                "CAST_FOR_IMPENDING": self._handle_cast_for_impending,
                "PAY_OFFSPRING_COST": self._handle_pay_offspring_cost,
            }

    def _handle_level_up_class(self, param, context, **kwargs):
        """Handle leveling up a class card."""
        gs = self.game_state
        player = gs._get_active_player()
        class_idx = param

        if not hasattr(gs, 'ability_handler') or not gs.ability_handler:
            logging.error("LEVEL_UP_CLASS failed: AbilityHandler not found.")
            return -0.15, False # Failure

        if class_idx is None or not isinstance(class_idx, int):
            logging.error(f"LEVEL_UP_CLASS failed: Invalid or missing index parameter '{class_idx}'.")
            return -0.15, False # Failure

        if gs.ability_handler.handle_class_level_up(class_idx):
            return 0.35, True # Success
        else:
            logging.debug(f"Leveling up class at index {class_idx} failed (handled by ability_handler).")
            return -0.1, False # Failure

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
        """Return the current action mask as boolean array with reasoning. CONCEDE is now a last resort. (Revised Mulligan/Priority/Logging/Waiting v4 - Added Recovery)"""
        gs = self.game_state
        try:
            valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
            action_reasons = {} # Reset reasons for this generation

            def set_valid_action(index, reason="", context=None):
                # Ensures CONCEDE (12) isn't added here, handled at the end.
                if 0 <= index < self.ACTION_SPACE_SIZE and index != 12:
                    valid_actions[index] = True
                    action_reasons[index] = {"reason": reason, "context": context or {}}
                    # logging.debug(f"  Set action {index} VALID: {reason}") # Optional detailed logging
                elif index != 12: # Log invalid indices *except* CONCEDE
                    logging.error(f"INVALID ACTION INDEX during generation: {index} bounds (0-{self.ACTION_SPACE_SIZE-1}) Reason: {reason}")

            # --- Player Validation & Perspective ---
            perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2 # Player from whose view we generate
            if not gs.p1 or not gs.p2 or not perspective_player:
                logging.error("Player object(s) missing or invalid. Defaulting to CONCEDE.")
                valid_actions[12] = True; action_reasons[12] = {"reason": "Error: Players not initialized", "context": {}}
                self.action_reasons_with_context = action_reasons; self.action_reasons = {k: v.get("reason","Err") for k, v in action_reasons.items()}; return valid_actions

            current_turn_player = gs._get_active_player() # Player whose turn it is
            priority_player_obj = getattr(gs, 'priority_player', None) # Player who currently holds priority

            # --- Mulligan Phase Logic ---
            if getattr(gs, 'mulligan_in_progress', False):
                mulligan_decision_player = getattr(gs, 'mulligan_player', None)
                bottoming_active_player = getattr(gs, 'bottoming_player', None)
                perspective_player_name = perspective_player.get('name', 'Unknown')
                mulligan_target_name = getattr(mulligan_decision_player, 'name', 'None') if mulligan_decision_player else 'None' # Added safe check
                bottoming_target_name = getattr(bottoming_active_player, 'name', 'None') if bottoming_active_player else 'None' # Added safe check
                # Use debug level for this potentially frequent log
                logging.debug(f"Mulligan Gen: Perspective={perspective_player_name}, Mulligan Player={mulligan_target_name}, Bottoming Player={bottoming_target_name}")

                action_found_in_mulligan = False

                # Check if the perspective player needs to bottom cards
                if bottoming_active_player == perspective_player:
                    hand_size = len(perspective_player.get("hand", []))
                    # Use getattr safely for potentially missing attrs during state issues
                    needed = max(0, getattr(gs, 'cards_to_bottom', 0) - getattr(gs, 'bottoming_count', 0))
                    logging.debug(f"Bottoming Phase (Perspective): Needs to bottom {needed} cards.")
                    if needed > 0:
                        for i in range(min(hand_size, 4)): # BOTTOM_CARD actions 226-229 for hand indices 0-3
                            set_valid_action(226 + i, f"BOTTOM_CARD index {i}", context={'hand_idx': i})
                        action_found_in_mulligan = True
                    else: # Needed is 0, but player still assigned (should transition via apply_action/bottom_card)
                        logging.warning("Mulligan Stuck? Current player assigned bottoming, but no cards needed. Allowing NO_OP.")
                        set_valid_action(224, "NO_OP (Bottoming Stuck?)")
                        action_found_in_mulligan = True

                # Check if the perspective player needs to make a mulligan decision
                elif mulligan_decision_player == perspective_player:
                    logging.debug(f"Mulligan Phase (Perspective): Deciding Keep/Mull.")
                    set_valid_action(225, "KEEP_HAND")
                    mulls_taken = gs.mulligan_count.get('p1' if perspective_player == gs.p1 else 'p2', 0)
                    # Can always mulligan first time (0 mulls taken), down to 0 cards eventually. Max 7 normal mulligans.
                    if mulls_taken < 7:
                        set_valid_action(6, "MULLIGAN")
                    action_found_in_mulligan = True

                # *** ENHANCED CHECK FOR STALLED MULLIGAN & ATTEMPT RECOVERY ***
                elif bottoming_active_player is None and mulligan_decision_player is None:
                    # We are in mulligan_in_progress=True, but no player assigned. This IS the error state seen in logs.
                    logging.error("MULLIGAN STATE ERROR: In Progress, but mulligan_player AND bottoming_player are None! Attempting ENHANCED recovery.")
                    
                    # First try checking mulligan state - this will try to find undecided players or force end
                    try:
                        fixed = False
                        if hasattr(gs, 'check_mulligan_state'):
                            # Check returns True if valid, False if forced recovery
                            valid_state = gs.check_mulligan_state()
                            if not valid_state:
                                # Recovery was forced - return temp actions for this call, next call will have updated state
                                logging.info("Mulligan state fixed via check_mulligan_state(). Re-generating actions.")
                                fixed = True
                            elif gs.mulligan_player == perspective_player:
                                # If check assigned current player, let this function continue
                                logging.info("Recovery: mulligan_player is now assigned to perspective player.")
                                # Re-run decision logic with new assignment
                                set_valid_action(225, "KEEP_HAND")
                                mulls_taken = gs.mulligan_count.get('p1' if perspective_player == gs.p1 else 'p2', 0)
                                if mulls_taken < 7:
                                    set_valid_action(6, "MULLIGAN")
                                action_found_in_mulligan = True
                                fixed = True
                        
                        # If check failed to fix, try direct end
                        if not fixed:
                            logging.warning("check_mulligan_state() didn't resolve issue, trying direct _end_mulligan_phase()")
                            if hasattr(gs, '_end_mulligan_phase'):
                                gs._end_mulligan_phase()
                                logging.info("Mulligan phase force-ended via direct call. Re-generating actions.")
                                fixed = True
                        
                        # If any recovery happened, return temporary actions
                        if fixed:
                            temp_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                            temp_actions[224] = True # Allow NO_OP
                            return temp_actions
                            
                    except Exception as recovery_e:
                        logging.critical(f"CRITICAL: Enhanced recovery failed: {recovery_e}", exc_info=True)
                    
                    # If all recovery attempts fail, allow NO_OP as a last resort
                    set_valid_action(224, "NO_OP (Mulligan State Error - After recovery attempts)")
                    action_found_in_mulligan = True
                # *** END OF ENHANCED CHECK ***

                # Check if perspective player is waiting for opponent
                elif bottoming_active_player: # Opponent is bottoming
                    set_valid_action(224, f"NO_OP (Waiting for {bottoming_target_name} bottoming)")
                    action_found_in_mulligan = True
                elif mulligan_decision_player: # Opponent is deciding mulligan
                    set_valid_action(224, f"NO_OP (Waiting for {mulligan_target_name} mulligan)")
                    action_found_in_mulligan = True


                # --- Final check/return for mulligan phase (if recovery wasn't attempted/failed) ---
                self.action_reasons_with_context = action_reasons.copy()
                self.action_reasons = {k: v.get("reason","Mull") for k, v in action_reasons.items()}
                if not action_found_in_mulligan: # If NO actions were set inside mulligan logic (including waiting)
                    # This path should be less likely now due to the error recovery attempt
                    logging.warning("No specific mulligan/bottom/wait/error actions generated despite being in mulligan phase.")
                    valid_actions[224] = True # Failsafe NO_OP
                    action_reasons[224] = {"reason": "NO_OP (Fallback Mulligan)", "context": {}}
                    self.action_reasons_with_context[224] = action_reasons[224]; self.action_reasons[224] = "NO_OP (Fallback Mulligan)"

                # --- Add CONCEDE as last resort for mulligan phase ---
                if np.sum(valid_actions) == 0: # Check if ONLY CONCEDE would be valid
                    valid_actions[12] = True
                    action_reasons[12] = {"reason": "CONCEDE (Mulligan - Final)", "context": {}}
                    self.action_reasons_with_context[12] = action_reasons[12]; self.action_reasons[12] = action_reasons[12]["reason"]
                # CONCEDE(12) is implicitly false otherwise due to set_valid_action logic

                return valid_actions # Return immediately for mulligan phase

            # --- Special Choice Phase Logic (Targeting, Sacrifice, Choose) ---
            special_phase = None
            acting_player = None # Player required to make the choice
            if gs.phase == gs.PHASE_TARGETING and hasattr(gs, 'targeting_context') and gs.targeting_context:
                special_phase = "TARGETING"; acting_player = gs.targeting_context.get("controller")
            elif gs.phase == gs.PHASE_SACRIFICE and hasattr(gs, 'sacrifice_context') and gs.sacrifice_context:
                special_phase = "SACRIFICE"; acting_player = gs.sacrifice_context.get("controller")
            elif gs.phase == gs.PHASE_CHOOSE and hasattr(gs, 'choice_context') and gs.choice_context:
                special_phase = "CHOOSE"; acting_player = gs.choice_context.get("player")

            if special_phase:
                action_found_in_special = False
                if acting_player == perspective_player:
                    logging.debug(f"Generating actions limited to {special_phase} phase for player {perspective_player.get('name')}.")
                    if special_phase == "TARGETING":
                        self._add_targeting_actions(perspective_player, valid_actions, set_valid_action)
                        min_req = gs.targeting_context.get("min_targets", 1); sel = len(gs.targeting_context.get("selected_targets", []))
                        max_targets = gs.targeting_context.get("max_targets", 1)
                        # Allow passing if minimum met AND (minimum is less than max OR maximum reached)
                        if sel >= min_req and (min_req < max_targets or sel == max_targets):
                            set_valid_action(11, f"PASS_PRIORITY (Finish {special_phase})") # Pass signifies completion here
                    elif special_phase == "SACRIFICE":
                        self._add_sacrifice_actions(perspective_player, valid_actions, set_valid_action)
                        min_req = gs.sacrifice_context.get("required_count", 1); sel = len(gs.sacrifice_context.get("selected_permanents", []))
                        if sel >= min_req: set_valid_action(11, f"PASS_PRIORITY (Finish {special_phase})") # Pass signifies completion
                    elif special_phase == "CHOOSE":
                        self._add_special_choice_actions(perspective_player, valid_actions, set_valid_action)
                        # PASS logic might be embedded within special choice actions where needed (e.g., mode choice)
                    action_found_in_special = np.sum(valid_actions) > 0 # Check if any actions were set
                else:
                    # Not this player's turn to act in special phase, allow NO_OP
                    set_valid_action(224, f"NO_OP (Waiting for opponent {special_phase})")
                    action_found_in_special = True

                # Final check for special phase actions
                self.action_reasons_with_context = action_reasons.copy(); self.action_reasons = {k: v.get("reason","Choice") for k, v in action_reasons.items()}
                if not action_found_in_special: # If no actions (even PASS/NO_OP) were set
                    logging.warning(f"No actions generated during special phase {special_phase} for acting player {getattr(acting_player, 'name', 'None')}.")
                    valid_actions[224] = True # Failsafe NO_OP
                    action_reasons[224] = {"reason": f"NO_OP (Fallback {special_phase})", "context": {}}
                    self.action_reasons_with_context[224] = action_reasons[224]; self.action_reasons[224] = action_reasons[224]["reason"]

                # Add CONCEDE as last resort for special phases
                if np.sum(valid_actions) == 0:
                    valid_actions[12] = True
                    action_reasons[12] = {"reason": f"CONCEDE ({special_phase} - Final)", "context": {}}
                    self.action_reasons_with_context[12] = action_reasons[12]; self.action_reasons[12] = action_reasons[12]["reason"]
                # CONCEDE(12) is implicitly false otherwise

                return valid_actions # Return immediately for special phases

            # --- Regular Game Play ---
            has_priority = (priority_player_obj == perspective_player)

            if not has_priority:
                # --- Perspective Player Does NOT Have Priority ---
                logging.debug(f"generate_valid_actions called for {perspective_player.get('name')}, but priority is with {getattr(priority_player_obj, 'name', 'None')}. Allowing NO_OP/Mana.")
                # Allow NO_OP when waiting.
                set_valid_action(224, "NO_OP (Waiting for priority)")
                # Add instant-speed MANA abilities (allowed without priority - Rule 605.3a)
                self._add_mana_ability_actions(perspective_player, valid_actions, set_valid_action)
                # Other instant-speed actions (like casting instants) require priority.

            else:
                # --- Perspective Player HAS Priority ---
                split_second_is_active = getattr(gs, 'split_second_active', False)
                # Passing is almost always possible if you have priority (unless winning/losing effect forces action)
                set_valid_action(11, "PASS_PRIORITY")

                if split_second_is_active:
                    # Only add mana abilities (and PASS already added)
                    logging.debug("Split Second active, only allowing Mana abilities and PASS.")
                    self._add_mana_ability_actions(perspective_player, valid_actions, set_valid_action)
                else:
                    # Add regular actions based on timing
                    is_my_turn = (current_turn_player == perspective_player)
                    # Use GameState helper to check sorcery speed timing
                    can_act_sorcery_speed = False
                    if hasattr(gs, '_can_act_at_sorcery_speed'):
                        can_act_sorcery_speed = gs._can_act_at_sorcery_speed(perspective_player)

                    # Can generally act at instant speed unless in Untap/Cleanup
                    # Targeting/Sacrifice/Choose handled above, so don't need to check here
                    can_act_instant_speed = gs.phase not in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP, gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]

                    opponent_player = gs.p2 if perspective_player == gs.p1 else gs.p1

                    # Sorcery Speed Actions (Main phase, own turn, empty stack)
                    if can_act_sorcery_speed:
                        self._add_sorcery_speed_actions(perspective_player, opponent_player, valid_actions, set_valid_action)
                        self._add_basic_phase_actions(is_my_turn, valid_actions, set_valid_action) # Phase transitions only at sorcery speed

                    # Instant Speed Actions (Any phase except Untap/Cleanup, needs priority)
                    if can_act_instant_speed:
                        self._add_instant_speed_actions(perspective_player, opponent_player, valid_actions, set_valid_action)

                    # Add Combat Actions (Declaration phase specific logic)
                    if hasattr(self, 'combat_handler') and self.combat_handler:
                        active_player_gs = gs._get_active_player() # Use GS helper
                        non_active_player_gs = gs._get_non_active_player() # Use GS helper

                        if gs.phase == gs.PHASE_DECLARE_ATTACKERS and perspective_player == active_player_gs:
                            self.combat_handler._add_attack_declaration_actions(perspective_player, non_active_player_gs, valid_actions, set_valid_action)
                        elif gs.phase == gs.PHASE_DECLARE_BLOCKERS and perspective_player == non_active_player_gs and getattr(gs, 'current_attackers', []): # Must be non-active player and attackers declared
                            self.combat_handler._add_block_declaration_actions(perspective_player, valid_actions, set_valid_action)
                        # Add other combat step actions if needed (e.g., damage order assignment, First Strike Order choice)
                        # These would likely be triggered by the combat handler's state machine logic.

                    # Check optional Offspring cost payment for a PENDING spell
                    pending_spell_context = getattr(gs, 'pending_spell_context', None)
                    if pending_spell_context and pending_spell_context.get('card_id') and \
                    pending_spell_context.get('controller') == perspective_player:
                        card_id = pending_spell_context['card_id']
                        card = gs._safe_get_card(card_id)
                        # Check if the spell is waiting for this specific decision (offspring cost)
                        # Assumes pending_spell_context signals waiting for choices (like Kicker/Offspring)
                        if card and getattr(card, 'is_offspring', False) and \
                        not pending_spell_context.get('pay_offspring', None) and \
                        pending_spell_context.get('waiting_for_choice') == 'offspring_cost': # Add specific wait flag
                            cost_str = getattr(card, 'offspring_cost', None)
                            # Use pending_context for affordability check
                            if cost_str and self._can_afford_cost_string(player=perspective_player, cost_string=cost_str, context=pending_spell_context):
                                offspring_context = {'action_source': 'offspring_payment_opportunity'}
                                set_valid_action(295, f"Optional: PAY_OFFSPRING_COST for {card.name}", context=offspring_context)


            # --- Final CONCEDE Logic ---
            self.action_reasons_with_context = action_reasons.copy()
            self.action_reasons = {k: v.get("reason","Unknown") for k, v in action_reasons.items()}
            num_valid_non_concede_actions = np.sum(valid_actions) # Count valid actions excluding CONCEDE

            if num_valid_non_concede_actions == 0:
                # Only add CONCEDE if *truly* no other action (not even NO_OP or PASS) is available.
                valid_actions[12] = True
                concede_reason = "CONCEDE (No other valid actions)"
                if 12 not in action_reasons: # Avoid overwriting critical errors
                    action_reasons[12] = {"reason": concede_reason, "context": {}}
                    self.action_reasons_with_context[12] = action_reasons[12]; self.action_reasons[12] = concede_reason
                logging.warning("generate_valid_actions: No valid actions found, only CONCEDE is available.")
            # CONCEDE(12) remains implicitly False otherwise

            return valid_actions

        except Exception as e:
            # --- Critical Error Fallback ---
            logging.critical(f"CRITICAL error generating valid actions: {str(e)}", exc_info=True)
            fallback_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
            fallback_actions[11] = True # Pass Priority
            fallback_actions[12] = True # Concede
            self.action_reasons = {11: "Crit Err - PASS", 12: "Crit Err - CONCEDE"}
            self.action_reasons_with_context = {11: {"reason":"Crit Err - PASS","context":{}}, 12: {"reason":"Crit Err - CONCEDE","context":{}}}
            return fallback_actions
            
    def _add_basic_phase_actions(self, is_my_turn, valid_actions, set_valid_action):
        """Adds basic actions available based on the current phase, assuming priority and no stack."""
        gs = self.game_state

        # MAIN_PHASE_END (Action 3)
        if is_my_turn and gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]:
            set_valid_action(3, f"End Main Phase {gs._PHASE_NAMES.get(gs.phase)}")

        # BEGIN_COMBAT_END (Action 8)
        if is_my_turn and gs.phase == gs.PHASE_BEGIN_COMBAT:
            set_valid_action(8, "End Begin Combat Step")

        # COMBAT_DAMAGE (Action 4) - Only if combat occurred and damage assignment is next
        # This action is less of a player choice and more a system transition.
        # Might be better handled by the combat handler logic triggering the phase change.
        # Let's *not* add it here, assuming the declare blockers done action transitions to damage steps.

        # END_COMBAT (Action 9)
        if is_my_turn and gs.phase == gs.PHASE_END_OF_COMBAT:
             set_valid_action(9, "End Combat Phase")

        # END_STEP (Action 10) - Renamed from END_PHASE to match ACTION_MEANINGS
        if is_my_turn and gs.phase == gs.PHASE_END_STEP:
            # Passing priority in End Step handles moving to Cleanup
            pass # Let PASS_PRIORITY handle this transition

        # UPKEEP_PASS (Action 7)
        if is_my_turn and gs.phase == gs.PHASE_UPKEEP:
            set_valid_action(7, "End Upkeep Step")
            
    def _add_mana_ability_actions(self, player, valid_actions, set_valid_action):
            """Add actions only for mana abilities (used during Split Second)."""
            gs = self.game_state
            if not hasattr(gs, 'ability_handler'): return

            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                card = gs._safe_get_card(card_id)
                if not card: continue

                abilities = gs.ability_handler.registered_abilities.get(card_id, [])
                for j, ability in enumerate(abilities):
                    if j >= 3: break # Limit abilities per card

                    # Check if it's a Mana Ability specifically
                    is_mana_ability = False
                    if hasattr(gs.ability_handler, 'is_mana_ability') and callable(gs.ability_handler.is_mana_ability):
                        is_mana_ability = gs.ability_handler.is_mana_ability(ability)
                    elif isinstance(ability, gs.ability_handler.ManaAbility): # Fallback check if is_mana_ability doesn't exist
                        is_mana_ability = True

                    if is_mana_ability:
                        if gs.ability_handler.can_activate_ability(card_id, j, player):
                            # Map (battlefield_idx, ability_idx) to action index
                            action_idx = 100 + (i * 3) + j
                            if action_idx < 160: # Ensure it's within ACTIVATE_ABILITY range
                                set_valid_action(action_idx, f"MANA_ABILITY {card.name} ability {j}")

            # Add tapping basic lands for mana (simplification)
            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                card = gs._safe_get_card(card_id)
                if card and 'land' in getattr(card, 'type_line', '') and card_id not in player.get("tapped_permanents", set()):
                    if hasattr(card, 'oracle_text') and "add {" in card.oracle_text.lower():
                        # Check if it's JUST a mana ability (no targets, no loyalty cost)
                        if ":" not in card.oracle_text.lower() or "{t}: add" in card.oracle_text.lower():
                            action_idx = 68 + i
                            if action_idx < 88: # Check it's within TAP_LAND_FOR_MANA range
                                set_valid_action(action_idx, f"TAP_LAND_FOR_MANA {card.name}")
        
    def _add_sorcery_speed_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions performable only at sorcery speed. (Updated for Offspring/Impending)"""
        gs = self.game_state
        # --- Play Land ---
        if not player.get("land_played", False): # Use .get for safety
            for i in range(min(len(player["hand"]), 7)): # Hand index 0-6 -> Land Actions 13-19
                try:
                    card_id = player["hand"][i]
                    card = gs._safe_get_card(card_id)
                    if card and 'land' in getattr(card, 'type_line', '').lower():
                        # Context needed: hand_idx for the land card itself
                        play_land_context = {'hand_idx': i}
                        set_valid_action(13 + i, f"PLAY_LAND {card.name}", context=play_land_context)

                        # MDFC Land Back - Hand index 0-7 -> Actions 180-187
                        back_face_data = getattr(card, 'back_face', None)
                        if hasattr(card, 'is_mdfc') and card.is_mdfc() and back_face_data and 'land' in back_face_data.get('type_line','').lower():
                            mdfc_land_context = {'hand_idx': i, 'play_back_face': True}
                            set_valid_action(180 + i, f"PLAY_MDFC_LAND_BACK {back_face_data.get('name', 'Unknown')}", context=mdfc_land_context)
                except IndexError:
                    logging.warning(f"IndexError accessing hand for PLAY_LAND at index {i}")
                    break # Stop if index is out of bounds

        # --- Play Sorcery-speed Spells ---
        for i in range(min(len(player["hand"]), 8)): # Hand index 0-7 -> Spell Actions 20-27
            try:
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'type_line') or not hasattr(card, 'card_types'): continue

                # Determine if card is typically sorcery speed
                is_sorcery_speed_type = 'land' not in card.type_line.lower() and not ('instant' in card.card_types or self._has_flash(card_id))

                if is_sorcery_speed_type:
                    # Check base cost affordability FIRST for the standard PLAY_SPELL action
                    if self._can_afford_card(player, card, context={}):
                        if self._targets_available(card, player, opponent):
                            # --- STANDARD PLAY_SPELL ACTION ---
                            # Provide context: hand_idx
                            play_context = {'hand_idx': i}
                            set_valid_action(20 + i, f"PLAY_SPELL {card.name}", context=play_context)
                            # Offer Offspring PAYMENT option *after* PLAY_SPELL is deemed valid
                            if getattr(card, 'is_offspring', False) and getattr(card, 'offspring_cost', None):
                                 if self._can_afford_cost_string(player, card.offspring_cost):
                                      # No extra context needed, applies to pending spell
                                      set_valid_action(295, f"Optional: PAY_OFFSPRING_COST for {card.name}")

                            # Offer Kicker / Additional Cost Payment (If applicable) - Should check affordability
                            # ... Add checks for PAY_KICKER (405/406), PAY_ADDITIONAL_COST (407/408) based on card text ...

                    # --- OFFER ALTERNATIVE CASTING MODES (Impending) ---
                    if getattr(card, 'is_impending', False) and getattr(card, 'impending_cost', None):
                         if self._can_afford_cost_string(player, card.impending_cost):
                              # Provide context: hand_idx
                              impending_context = {'hand_idx': i}
                              set_valid_action(294, f"Alt: CAST_FOR_IMPENDING {card.name}", context=impending_context)

                    # --- Other alternative/related actions (MDFC back, Adventure) ---
                    # Offer MDFC Spell Back (Sorcery) - Hand index 0-7 -> Actions 188-195
                    back_face_data = getattr(card, 'back_face', None)
                    if hasattr(card, 'is_mdfc') and card.is_mdfc() and back_face_data:
                        back_type_line = back_face_data.get('type_line','').lower()
                        back_types = back_face_data.get('card_types', [])
                        back_has_flash = self._has_flash_text(back_face_data.get('oracle_text',''))
                        if 'land' not in back_type_line and not ('instant' in back_types or back_has_flash):
                            if self._can_afford_card(player, back_face_data, is_back_face=True, context={}):
                                 if self._targets_available_from_data(back_face_data, player, opponent):
                                     mdfc_back_context = {'hand_idx': i, 'play_back_face': True}
                                     set_valid_action(188 + i, f"PLAY_MDFC_BACK {back_face_data.get('name', 'Unknown')}", context=mdfc_back_context)

                    # Offer Adventure (Sorcery) - Hand index 0-7 -> Actions 196-203
                    if hasattr(card, 'has_adventure') and card.has_adventure():
                        adv_data = card.get_adventure_data()
                        if adv_data and ('sorcery' in adv_data.get('type','').lower() or 'instant' in adv_data.get('type','').lower()): # Offer Adventure even if Instant speed on main phase
                            if self._can_afford_cost_string(player, adv_data.get('cost',''), context={}):
                                if self._targets_available_from_text(adv_data.get('effect',''), player, opponent):
                                     adventure_context = {'hand_idx': i, 'play_adventure': True}
                                     set_valid_action(196 + i, f"PLAY_ADVENTURE {adv_data.get('name', 'Unknown')}", context=adventure_context)

            except IndexError:
                 logging.warning(f"IndexError accessing hand for PLAY_SPELL at index {i}"); break

        # --- Other Sorcery-speed Actions ---
        self._add_ability_activation_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)
        # PW abilities handled by _add_planeswalker_actions
        if hasattr(self, 'combat_handler') and self.combat_handler:
            self.combat_handler._add_planeswalker_actions(player, valid_actions, set_valid_action)

        self._add_level_up_actions(player, valid_actions, set_valid_action)
        self._add_unlock_door_actions(player, valid_actions, set_valid_action)
        # Renamed _add_equip_actions to _add_equipment_aura_actions (assuming it handles fortify/reconfigure too)
        if hasattr(self, '_add_equipment_aura_actions') and callable(self._add_equipment_aura_actions):
             self._add_equipment_aura_actions(player, valid_actions, set_valid_action)
        else: # Fallback if rename not done
            if hasattr(self, '_add_equip_actions') and callable(self._add_equip_actions): self._add_equip_actions(player, valid_actions, set_valid_action)

        self._add_morph_actions(player, valid_actions, set_valid_action)
        self._add_alternative_casting_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)
        self._add_special_mechanics_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)
        
    def _add_instant_speed_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions performable at instant speed. (Updated for Offspring/Impending)"""
        gs = self.game_state
        # --- Play Instant/Flash Spells (Modified) ---
        for i in range(min(len(player["hand"]), 8)): # Spell Actions 20-27
            try:
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'type_line') or not hasattr(card, 'card_types'): continue

                is_instant_speed = 'instant' in card.card_types or self._has_flash(card_id)

                if is_instant_speed and 'land' not in card.type_line.lower():
                     if self._can_afford_card(player, card, context={}):
                         if self._targets_available(card, player, opponent):
                            # --- MAIN PLAY ACTION ---
                            play_context = {'hand_idx': i}
                            set_valid_action(20 + i, f"PLAY_SPELL (Instant) {card.name}", context=play_context)

                            # --- OFFER ADDITIONAL/ALTERNATIVE ACTIONS ---
                            # Offer Offspring Payment (Optional Additional)
                            if getattr(card, 'is_offspring', False) and getattr(card, 'offspring_cost', None):
                                 if self._can_afford_cost_string(player, card.offspring_cost):
                                      set_valid_action(295, f"Optional: PAY_OFFSPRING_COST for {card.name}")
                                      
                            # Offer MDFC Back (Instant/Flash)
                            back_face_data = getattr(card, 'back_face', None)
                            if hasattr(card, 'is_mdfc') and card.is_mdfc() and back_face_data:
                                # ... (existing logic for instant back face, using hand_idx i) ...
                                pass

                            # Offer Adventure (Instant)
                            if hasattr(card, 'has_adventure') and card.has_adventure():
                                # ... (existing logic for instant adventure, using hand_idx i) ...
                                pass

            except IndexError:
                 logging.warning(f"IndexError accessing hand for Instant/Flash spell at index {i}"); break

        # --- Other instant speed actions (no changes needed) ---
        self._add_ability_activation_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)
        self._add_land_tapping_actions(player, valid_actions, set_valid_action)
        self._add_alternative_casting_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)
        self._add_cycling_actions(player, valid_actions, set_valid_action)
        if gs.stack: self._add_response_actions(player, valid_actions, set_valid_action)
        self._add_special_mechanics_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)
        
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
                     

    def _add_special_choice_actions(self, player, valid_actions, set_valid_action):
        """Add actions for Scry, Surveil, Dredge, Choose Mode, Choose X, Choose Color."""
        gs = self.game_state
        # Only generate these during the dedicated CHOICE phase
        if gs.phase != gs.PHASE_CHOOSE: return

        if hasattr(gs, 'choice_context') and gs.choice_context:
            context = gs.choice_context
            choice_type = context.get("type")
            source_id = context.get("source_id")
            choice_player = context.get("player")

            if choice_player != player: # Not this player's choice
                set_valid_action(11, "PASS_PRIORITY (Waiting for opponent choice)")
                return

            # --- Scry / Surveil ---
            if choice_type in ["scry", "surveil"] and context.get("cards"):
                card_id = context["cards"][0] # Process one card at a time
                card = gs._safe_get_card(card_id)
                card_name = getattr(card, 'name', card_id)
                set_valid_action(306, f"PUT_ON_TOP {card_name}") # Action for Top
                if choice_type == "scry":
                    set_valid_action(307, f"PUT_ON_BOTTOM {card_name}") # Action for Bottom (Scry only)
                else: # Surveil
                     set_valid_action(305, f"PUT_TO_GRAVEYARD {card_name}") # Action for Graveyard (Surveil only)

            # --- Dredge (Replace Draw) ---
            elif choice_type == "dredge" and context.get("card_id"):
                 # ... (keep existing Dredge logic for action 308) ...
                 card_id = context.get("card_id")
                 dredge_val = context.get("value")
                 if len(player["library"]) >= dredge_val:
                     # Validate card is still in GY? Probably not needed, context source is reliable
                     gy_idx = -1
                     for idx, gy_id in enumerate(player.get("graveyard", [])):
                         if gy_id == card_id: gy_idx = idx; break
                     if gy_idx != -1:
                         # We need context for the DREDGE action handler now
                         dredge_action_context = {'gy_idx': gy_idx}
                         set_valid_action(308, f"DREDGE {gs._safe_get_card(card_id).name}", context=dredge_action_context)
                 # Always allow skipping the dredge replacement
                 set_valid_action(11, "Skip Dredge") # PASS_PRIORITY effectively skips

            # --- Choose Mode ---
            elif choice_type == "choose_mode":
                 num_choices = context.get("num_choices", 0)
                 max_modes = context.get("max_required", 1)
                 min_modes = context.get("min_required", 1)
                 selected_count = len(context.get("selected_modes", []))

                 # Allow choosing another mode if max not reached
                 if selected_count < max_modes:
                     for i in range(min(num_choices, 10)): # Mode index 0-9 (Action 353-362)
                          # Prevent selecting the same mode twice if only choosing 1 or 2 unless allowed
                          if i not in context.get("selected_modes", []): # Basic duplicate check
                               # Action needs context: { 'battlefield_idx': ?, 'ability_idx': ?} NO - context is in gs.choice_context
                               set_valid_action(353 + i, f"CHOOSE_MODE {i+1}")

                 # Allow finalizing choice if minimum met (and min != max)
                 if selected_count >= min_modes and min_modes != max_modes:
                      set_valid_action(11, "PASS_PRIORITY (Finish Mode Choice)") # Allow passing to finalize if optional modes remain

            # --- Choose X ---
            elif choice_type == "choose_x":
                 max_x = context.get("max_x", 0)
                 min_x = context.get("min_x", 0)
                 for i in range(min(max_x, 10)): # X value 1-10 (Actions 363-372)
                      x_val = i + 1
                      if x_val >= min_x: # Only allow valid X choices based on min
                           # Action needs context: { 'X_Value': x_val } - This is embedded in param
                           set_valid_action(363 + i, f"CHOOSE_X_VALUE {x_val}")
                 # No PASS needed, choosing X implicitly finalizes

            # --- Choose Color ---
            elif choice_type == "choose_color":
                 for i in range(5): # Color index 0-4 (WUBRG -> Actions 373-377)
                      # Action needs context: { 'color_index': i } - Embedded in param
                      set_valid_action(373 + i, f"CHOOSE_COLOR {['W','U','B','R','G'][i]}")
                 # No PASS needed, choosing color implicitly finalizes

            # --- Kicker / Additional Cost / Escalate Choices ---
            elif choice_type == "pay_kicker":
                set_valid_action(405, "PAY_KICKER") # Param=True
                set_valid_action(406, "DONT_PAY_KICKER") # Param=False
            elif choice_type == "pay_additional":
                 set_valid_action(407, "PAY_ADDITIONAL_COST") # Param=True
                 set_valid_action(408, "DONT_PAY_ADDITIONAL_COST") # Param=False
            elif choice_type == "pay_escalate":
                 # Context needed: num_modes, num_selected, max_allowed_extra
                 # This logic is complex for generating distinct actions.
                 # Simplified: Offer PAY_ESCALATE action, handler decides how many based on cost/context.
                 # Param = number of *extra* modes to pay for (1 or more)
                 max_extra = context.get('max_modes', 1) - context.get('num_selected', 1)
                 for i in range(min(max_extra, 3)): # Allow paying for 1, 2, or 3 extra modes max (adjust as needed)
                      num_extra = i + 1
                      # Check affordability of paying N times (simplistic check)
                      escalate_cost = context.get('escalate_cost_each')
                      if escalate_cost and gs.mana_system.can_pay_mana_cost(player, f"{escalate_cost}*{num_extra}"):
                            escalate_action_context = {'num_extra_modes': num_extra}
                            # Needs dedicated actions if param doesn't support count.
                            # Reuse action 409, use context.
                            set_valid_action(409, f"PAY_ESCALATE for {num_extra} extra mode(s)", context=escalate_action_context)
                 set_valid_action(11, "PASS_PRIORITY (Finish Escalate/Don't pay)") # Finish Escalate choice

            # Add more choice types (Distribute counters, choose order, etc.) here

        else:
            # If no choice context is active during PHASE_CHOOSE (shouldn't happen), allow PASS
            set_valid_action(11, "PASS_PRIORITY (No choices pending?)")
        
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
      
    def apply_action(self, action_idx, **kwargs):
        """
        Execute the action and get the next observation, reward and done status.
        Overhauled for clarity, correctness, and better reward shaping.
        Includes an internal game loop for SBAs, Triggers, and Stack resolution.
        NOW returns (reward, done, truncated, info) as expected by Environment.
        """
        gs = self.game_state
        me = None
        opp = None
        if hasattr(gs, 'p1') and gs.p1 and hasattr(gs, 'p2') and gs.p2:
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1
        else:
            logging.error("Players not initialized in apply_action. Aborting step.")
            # Return default failure values matching the expected 4-tuple
            info = {"action_mask": np.zeros(self.ACTION_SPACE_SIZE, dtype=bool), "critical_error": True, "game_result":"error"}
            info["action_mask"][12] = True # Allow CONCEDE
            return -5.0, True, False, info # reward, done, truncated, info

        # --- Initialization for the step ---
        reward = 0.0
        done = False
        truncated = False # Gymnasium API requires truncated flag
        pre_action_pattern = None # Initialize here
        # Start with a clean info dict for this step
        info = {"action_mask": None, "game_result": "undetermined", "critical_error": False}

        # --- Regenerate mask (remains the same) ---
        if not hasattr(self, 'current_valid_actions') or self.current_valid_actions is None or np.sum(self.current_valid_actions) == 0:
            if hasattr(self, 'generate_valid_actions') and callable(self.generate_valid_actions):
                self.current_valid_actions = self.generate_valid_actions()
            else:
                logging.error("Action mask generation method not found!")
                self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                self.current_valid_actions[11] = True # Pass
                self.current_valid_actions[12] = True # Concede

        action_context = kwargs.get('context', {})
        # --- Merge context (remains the same) ---
        if hasattr(gs, 'phase'): # Check phase exists first
                if gs.phase == gs.PHASE_TARGETING and hasattr(gs, 'targeting_context') and gs.targeting_context:
                    action_context.update(gs.targeting_context)
                    if 'selected_targets' not in action_context:
                        action_context['selected_targets'] = gs.targeting_context.get('selected_targets', [])
                elif gs.phase == gs.PHASE_SACRIFICE and hasattr(gs, 'sacrifice_context') and gs.sacrifice_context:
                    action_context.update(gs.sacrifice_context)
                    if 'selected_permanents' not in action_context:
                        action_context['selected_permanents'] = gs.sacrifice_context.get('selected_permanents', [])
                elif gs.phase == gs.PHASE_CHOOSE and hasattr(gs, 'choice_context') and gs.choice_context:
                    action_context.update(gs.choice_context)
                    if gs.choice_context.get("type") == "scry" and "scrying_cards" not in action_context:
                        action_context['cards'] = gs.choice_context.get('cards', [])
                        action_context['kept_on_top'] = gs.choice_context.get('kept_on_top', [])
                        action_context['put_on_bottom'] = gs.choice_context.get('put_on_bottom', [])
                    elif gs.choice_context.get("type") == "surveil" and "cards_being_surveiled" not in action_context:
                        action_context['cards'] = gs.choice_context.get('cards', [])


        # --- Main Action Application Logic ---
        try:
            # 1. Validate Action Index (Check moved here)
            if not (0 <= action_idx < self.ACTION_SPACE_SIZE):
                logging.error(f"Action index {action_idx} is out of bounds (0-{self.ACTION_SPACE_SIZE-1}).")
                info["action_mask"] = self.current_valid_actions.astype(bool)
                info["error_message"] = f"Action index out of bounds: {action_idx}"
                info["critical_error"] = True
                return -0.5, False, False, info # Penalty, don't end game, return 4-tuple

            # 2. Validate Against Action Mask
            if not self.current_valid_actions[action_idx]:
                invalid_reason = self.action_reasons.get(action_idx, 'Not Valid / Unknown Reason')
                valid_indices = np.where(self.current_valid_actions)[0]
                logging.warning(f"Invalid action {action_idx} selected (Mask False). Reason: [{invalid_reason}]. Valid: {valid_indices}")
                reward = -0.1 # Standard penalty
                info["action_mask"] = self.current_valid_actions.astype(bool)
                info["invalid_action_reason"] = invalid_reason
                return reward, done, truncated, info # Return 4-tuple

            # Reset invalid action counter if needed (handled by environment)

            # 3. Get Action Info (remains the same)
            action_type, param = self.get_action_info(action_idx)
            logging.info(f"Applying action: {action_idx} -> {action_type}({param}) with context: {action_context}")

            # 4. Store Pre-Action State for Reward Shaping (remains the same)
            prev_state = {}
            if me and opp:
                prev_state = {
                    "my_life": me.get("life", 0), "opp_life": opp.get("life", 0),
                    "my_hand": len(me.get("hand", [])), "opp_hand": len(opp.get("hand", [])),
                    "my_board": len(me.get("battlefield", [])), "opp_board": len(opp.get("battlefield", [])),
                    "my_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) or 0 for cid in me.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                    "opp_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) or 0 for cid in opp.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                }
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory:
                try: pre_action_pattern = gs.strategy_memory.extract_strategy_pattern(gs)
                except Exception as e: logging.error(f"Error extracting pre-action strategy pattern: {e}")

            # --- 5. Execute Action - Delegate to specific handlers ---
            # --- MODIFIED: Ensure handlers return (reward, success) and process this ---
            handler_func = self.action_handlers.get(action_type)
            action_reward = 0.0
            action_executed = False # Flag to track if the handler logic ran successfully

            if handler_func:
                try:
                    import inspect
                    sig = inspect.signature(handler_func)
                    handler_args = {}
                    # Prepare arguments based on handler signature
                    if 'param' in sig.parameters: handler_args['param'] = param
                    if 'context' in sig.parameters: handler_args['context'] = action_context
                    if 'action_type' in sig.parameters: handler_args['action_type'] = action_type
                    if 'action_index' in sig.parameters: handler_args['action_index'] = action_idx # Include if needed

                    # Call the handler
                    result = handler_func(**handler_args)

                    # Process the result (EXPECTING 2-tuple)
                    if isinstance(result, tuple) and len(result) == 2:
                        action_reward, action_executed = result
                        if not isinstance(action_executed, bool): # Validate second element is bool
                            logging.warning(f"Handler {action_type} returned tuple, but 2nd element not bool: {result}. Assuming False execution.")
                            action_executed = False
                        if not isinstance(action_reward, (float, int)): # Validate first element is numeric
                             logging.warning(f"Handler {action_type} returned tuple, but 1st element not numeric reward: {result}. Using 0.0 reward.")
                             action_reward = 0.0

                    # --- DEPRECATED Handling for other return types - enforce 2-tuple ---
                    # elif isinstance(result, (float, int)): # Old handler returning only reward
                    #     logging.warning(f"Handler {action_type} returned only reward ({result}). Standardize to (reward, success). Assuming success=True.")
                    #     action_reward, action_executed = float(result), True
                    # elif isinstance(result, bool): # Old handler returning only success flag
                    #      logging.warning(f"Handler {action_type} returned only success flag ({result}). Standardize to (reward, success). Assigning default reward.")
                    #      action_reward, action_executed = (0.05, True) if result else (-0.1, False)
                    # ---------------------------------------------------------------------
                    else: # Handler returned something unexpected
                        logging.error(f"Handler {action_type} returned unexpected type: {type(result)}. Result: {result}. Standardize to (reward, success). Assuming failure.")
                        action_reward, action_executed = -0.2, False # Penalize failure

                except TypeError as te: # Handle signature mismatches more gracefully
                    # --- Reworked TypeError Handling ---
                    handler_args_used = {k: v for k, v in handler_args.items() if k in sig.parameters} # Use only valid args
                    logging.warning(f"TypeError calling {action_type} with args {handler_args} (Signature: {sig}). Attempting fallback with args {handler_args_used}. Error: {te}")
                    try:
                        result = handler_func(**handler_args_used) # Retry with filtered args
                        # Process result again, expecting 2-tuple
                        if isinstance(result, tuple) and len(result) == 2:
                            action_reward, action_executed = result
                            if not isinstance(action_executed, bool): action_executed = False
                            if not isinstance(action_reward, (float, int)): action_reward = 0.0
                        else:
                            logging.error(f"Handler {action_type} fallback call returned unexpected type: {type(result)}. Assuming failure.")
                            action_reward, action_executed = -0.2, False
                    except Exception as handler_e:
                        logging.error(f"Error executing handler {action_type} (fallback call): {handler_e}", exc_info=True)
                        action_reward, action_executed = -0.2, False # Fail on inner error
                except Exception as handler_e:
                    logging.error(f"Error executing handler {action_type} with params {handler_args}: {handler_e}", exc_info=True)
                    action_reward, action_executed = -0.2, False # Fail on general error
            else:
                logging.warning(f"No handler implemented for action type: {action_type}")
                action_reward = -0.05 # Small penalty for unimplemented action
                action_executed = False # Mark as not executed

            # Add action-specific reward to total step reward
            reward += action_reward
            info["action_reward"] = action_reward # Store just the action's direct reward

            # Check if action failed to execute properly (HANDLER failed)
            if not action_executed:
                logging.warning(f"Action {action_type}({param}) failed execution (Handler returned False or error occurred).")
                self.current_valid_actions = self.generate_valid_actions() if hasattr(self, 'generate_valid_actions') else np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                info["action_mask"] = self.current_valid_actions.astype(bool) # Return current mask
                info["execution_failed"] = True
                # Don't end the game here, return the state and let agent retry
                return reward, done, truncated, info # Return 4-tuple


            # --- BEGIN GAME LOOP (remains the same logic) ---
            resolution_attempts = 0
            max_resolution_attempts = 20 # Safety break
            while resolution_attempts < max_resolution_attempts:
                resolution_attempts += 1

                # a. Check State-Based Actions
                sba_performed = False
                if hasattr(gs, 'check_state_based_actions'):
                    sba_performed = gs.check_state_based_actions()
                    if (me and me.get("lost_game", False)) or (opp and opp.get("lost_game", False)):
                        done = True; info["game_result"] = "loss" if me.get("lost_game", False) else "win"
                        logging.debug(f"Game ended due to SBA during loop.")
                        break # Exit loop if game ended via SBAs

                # b. Process Triggered Abilities
                triggers_queued = False
                initial_stack_size = len(gs.stack)
                if hasattr(gs, 'ability_handler') and gs.ability_handler:
                    if hasattr(gs.ability_handler, 'process_triggered_abilities'):
                         gs.ability_handler.process_triggered_abilities()
                         if len(gs.stack) > initial_stack_size:
                              triggers_queued = True
                              gs.priority_pass_count = 0 # Reset priority passes
                              gs.priority_player = gs._get_active_player() # Priority goes to AP
                              gs.last_stack_size = len(gs.stack) # Update last stack size
                              logging.debug(f"Loop {resolution_attempts}: Triggers added to stack, priority reset to AP.")

                # c. Check if Stack Needs Resolution
                needs_resolution = (gs.priority_pass_count >= 2 and
                                    gs.stack and
                                    not getattr(gs, 'split_second_active', False))

                if needs_resolution:
                    logging.debug(f"Loop {resolution_attempts}: Resolving stack (Passes: {gs.priority_pass_count}, Stack: {len(gs.stack)} items)")
                    resolved = False
                    if hasattr(gs, 'resolve_top_of_stack'):
                       resolved = gs.resolve_top_of_stack()
                    else: logging.error("GameState missing resolve_top_of_stack method!")
                    if resolved: continue # Loop continues
                    else: logging.error(f"Stack resolution failed for top item! Breaking loop."); break

                # d. Break Condition
                if not sba_performed and not triggers_queued and not needs_resolution:
                    logging.debug(f"Loop {resolution_attempts}: State stable, exiting game loop.")
                    break
                if resolution_attempts > 1 and (sba_performed or triggers_queued):
                    logging.debug(f"Loop {resolution_attempts}: State changed (SBAs/Triggers), re-evaluating.")

            # --- Check for loop limit ---
            if resolution_attempts >= max_resolution_attempts:
                logging.error(f"Exceeded max game loop iterations ({max_resolution_attempts}) after action {action_type}. Potential loop or complex interaction.")
                # Setting done=True here could prevent true infinite loops in training.
                # done = True; truncated = True
                # info["game_result"] = "error_loop"
            # --- END GAME LOOP ---


            # --- 7. Calculate State Change Reward (remains the same) ---
            if not done and me and opp:
                current_state = {
                    "my_life": me.get("life", 0), "opp_life": opp.get("life", 0),
                    "my_hand": len(me.get("hand", [])), "opp_hand": len(opp.get("hand", [])),
                    "my_board": len(me.get("battlefield", [])), "opp_board": len(opp.get("battlefield", [])),
                    "my_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) or 0 for cid in me.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                    "opp_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) or 0 for cid in opp.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                }
                if hasattr(self, '_add_state_change_rewards') and callable(self._add_state_change_rewards):
                    state_change_reward = self._add_state_change_rewards(0.0, prev_state, current_state)
                    reward += state_change_reward
                    info["state_change_reward"] = state_change_reward

            # --- 8. Check Game End Conditions (remains the same) ---
            if not done:
                if opp and opp.get("lost_game"):
                    done = True; reward += 10.0 + max(0, gs.max_turns - gs.turn) * 0.1; info["game_result"] = "win"
                elif me and me.get("lost_game"):
                    done = True; reward -= 10.0; info["game_result"] = "loss"
                elif (me and me.get("game_draw")) or (opp and opp.get("game_draw")):
                    done = True; reward += 0.0; info["game_result"] = "draw"
                elif gs.turn > gs.max_turns:
                    if not getattr(gs, '_turn_limit_checked', False):
                        done, truncated = True, True
                        life_diff_reward = 0
                        if me and opp: life_diff_reward = (me.get("life",0) - opp.get("life",0)) * 0.1
                        reward += life_diff_reward
                        if me and opp: info["game_result"] = "win" if (me.get("life",0) > opp.get("life",0)) else "loss" if (me.get("life",0) < opp.get("life",0)) else "draw"
                        else: info["game_result"] = "draw"
                        gs._turn_limit_checked = True
                        logging.info(f"Turn limit ({gs.max_turns}) reached. Result: {info['game_result']}")

            # Record results if game ended
            if done and hasattr(gs.action_handler, 'ensure_game_result_recorded') and callable(gs.action_handler.ensure_game_result_recorded):
                 gs.action_handler.ensure_game_result_recorded() # Ensure called

            # --- 9. Finalize Step ---
            # Generate observation using Env's method AFTER state is fully updated
            # Obs generation is handled by the Env step function. This function now only returns the results.
            self.current_valid_actions = None # Invalidate mask cache in handler
            if hasattr(self, 'generate_valid_actions'): # Ensure method exists
                next_mask = self.generate_valid_actions().astype(bool)
            else: # Fallback if generate_valid_actions is missing on self
                next_mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); next_mask[11]=True; next_mask[12]=True
            info["action_mask"] = next_mask # Add next mask to info

            # Update strategy memory (remains the same)
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory and pre_action_pattern is not None:
                try: gs.strategy_memory.update_strategy(pre_action_pattern, reward)
                except Exception as strategy_e: logging.error(f"Error updating strategy memory: {strategy_e}")

            # *** IMPORTANT: Fix final return statement to return 4 values ***
            return reward, done, truncated, info # Return values for Env

        except Exception as e:
            logging.error(f"CRITICAL error in apply_action (Action {action_idx}): {e}", exc_info=True)
            mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); mask[11] = True; mask[12] = True
            # Update info dict directly
            info["action_mask"] = mask
            info["critical_error"] = True
            info["error_message"] = str(e)
            info["game_result"] = "error"
            # *** IMPORTANT: Fix final return statement to return 4 values ***
            return -5.0, True, False, info # Return values for Env
        
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

    def _handle_put_to_graveyard(self, param, context, **kwargs):
        """Handle Surveil choice: PUT_TO_GRAVEYARD. Relies on context from _add_special_choice_actions."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if not hasattr(gs, 'choice_context') or gs.choice_context is None or gs.choice_context.get("type") != "surveil":
            logging.warning("PUT_TO_GRAVEYARD called outside of Surveil context.")
            return -0.2, False
        if gs.choice_context.get("player") != player:
            logging.warning("Received PUT_TO_GRAVEYARD choice for non-active choice player.")
            return -0.2, False # Wrong player

        context = gs.choice_context
        if not context.get("cards"):
            logging.warning("Surveil choice PUT_TO_GRAVEYARD made but no cards left to process.")
            gs.choice_context = None # Clear context
            gs.phase = gs.PHASE_PRIORITY # Return to priority
            return -0.1, False # Minor error, but invalid state

        card_id = context["cards"].pop(0) # Process first card in the list
        card = gs._safe_get_card(card_id)
        card_name = getattr(card, 'name', card_id)

        # Use move_card to handle replacements/triggers
        success_move = gs.move_card(card_id, player, "library_implicit", player, "graveyard", cause="surveil")
        if not success_move:
            logging.error(f"Failed to move {card_name} to graveyard during surveil.")
            # Put card back? State is potentially inconsistent. End choice phase.
            gs.choice_context = None
            gs.phase = gs.PHASE_PRIORITY
            return -0.1, False

        logging.debug(f"Surveil: Put {card_name} into graveyard.")

        # If done surveiling, clear context and return to previous phase
        if not context.get("cards"):
            logging.debug("Surveil finished.")
            gs.choice_context = None
            if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                 gs.phase = gs.previous_priority_phase
                 gs.previous_priority_phase = None
            else:
                 gs.phase = gs.PHASE_PRIORITY # Fallback
            gs.priority_pass_count = 0 # Reset priority
            gs.priority_player = gs._get_active_player()
        # Else, stay in CHOICE phase for next card

        # Positive reward for making a valid choice
        card_eval_score = 0
        if self.card_evaluator and card:
             # Evaluate card being put in GY (might be good for recursion)
             card_eval_score = self.card_evaluator.evaluate_card(card_id, "general", context_details={"destination":"graveyard"})
        # Reward higher if putting low-value card in GY
        return 0.05 - card_eval_score * 0.05, True

    def _handle_scry_surveil_choice(self, param, context, **kwargs):
        """Unified handler for Scry/Surveil PUT_ON_TOP action."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        action_index = kwargs.get('action_index')
        # Determine destination based on action index
        destination = "top" # Default for PUT_ON_TOP (306)

        if action_index == 305: # PUT_TO_GRAVEYARD (Surveil specific)
            destination = "graveyard"
        elif action_index == 307: # PUT_ON_BOTTOM (Scry specific)
             destination = "bottom"

        # Get current context
        if not hasattr(gs, 'choice_context') or gs.choice_context is None:
            logging.warning(f"Scry/Surveil choice ({destination}) called outside of CHOOSE context.")
            return -0.2, False

        context = gs.choice_context
        current_choice_type = context.get("type")

        # Validate context type
        if current_choice_type not in ["scry", "surveil"]:
            logging.warning(f"Choice ({destination}) called during wrong context type: {current_choice_type}")
            return -0.2, False
        # Validate player
        if context.get("player") != player:
            logging.warning("Received Scry/Surveil choice for non-active choice player.")
            return -0.2, False
        # Validate card availability
        if not context.get("cards"):
            logging.warning(f"Choice ({destination}) made but no cards left to process.")
            gs.choice_context = None
            gs.phase = gs.PHASE_PRIORITY
            return -0.1, False # Invalid state

        # Validate action matches context type (e.g., cannot PUT_TO_GRAVEYARD during scry)
        if current_choice_type == "scry" and destination == "graveyard":
             logging.warning("Invalid action: Cannot PUT_TO_GRAVEYARD during Scry.")
             return -0.1, False
        if current_choice_type == "surveil" and destination == "bottom":
             logging.warning("Invalid action: Cannot PUT_ON_BOTTOM during Surveil.")
             return -0.1, False


        # --- Process the Choice ---
        card_id = context["cards"].pop(0) # Process first card
        card = gs._safe_get_card(card_id)
        card_name = getattr(card, 'name', card_id)

        reward = 0.05 # Base reward for valid choice
        card_eval_score = 0
        if self.card_evaluator and card:
             card_eval_score = self.card_evaluator.evaluate_card(card_id, "general", context_details={"destination": destination})


        if destination == "top":
             if current_choice_type == "scry":
                  # Add to list to be ordered later (by AI/rules)
                  context.setdefault("kept_on_top", []).append(card_id)
                  logging.debug(f"Scry: Keeping {card_name} on top (pending order).")
                  reward += card_eval_score * 0.05 # Reward keeping good cards
             else: # Surveil
                  # Put back onto library directly (no reordering for Surveil top choice)
                  player["library"].insert(0, card_id)
                  logging.debug(f"Surveil: Kept {card_name} on top.")
                  reward += card_eval_score * 0.05
        elif destination == "bottom": # Scry only
             context.setdefault("put_on_bottom", []).append(card_id)
             logging.debug(f"Scry: Putting {card_name} on bottom.")
             reward -= card_eval_score * 0.05 # Penalize bottoming good cards
        elif destination == "graveyard": # Surveil only
             success_move = gs.move_card(card_id, player, "library_implicit", player, "graveyard", cause="surveil")
             if not success_move:
                 logging.error(f"Failed to move {card_name} to graveyard during surveil.")
                 gs.choice_context = None; gs.phase = gs.PHASE_PRIORITY; return -0.1, False
             logging.debug(f"Surveil: Put {card_name} into graveyard.")
             reward -= card_eval_score * 0.03 # Smaller penalty for GY vs bottom? Depends.


        # --- Check if Choice Phase Ends ---
        if not context.get("cards"):
            logging.debug(f"{current_choice_type.capitalize()} finished.")

            # Finalize Scry: Put bottom cards, then ordered top cards back
            if current_choice_type == "scry":
                 bottom_cards = context.get("put_on_bottom", [])
                 top_cards = context.get("kept_on_top", [])
                 # AI needs to choose order for top_cards
                 # Simple: keep original relative order
                 ordered_top_cards = top_cards # Placeholder for ordering logic
                 # Add cards back to library
                 player["library"] = ordered_top_cards + player["library"] # Top first
                 player["library"].extend(bottom_cards) # Then bottom
                 logging.debug(f"Scry final: Top=[{','.join(ordered_top_cards)}], Bottom=[{','.join(bottom_cards)}]")

            # Clear context and return to previous phase
            gs.choice_context = None
            if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                 gs.phase = gs.previous_priority_phase
                 gs.previous_priority_phase = None
            else:
                 gs.phase = gs.PHASE_PRIORITY # Fallback
            gs.priority_pass_count = 0 # Reset priority
            gs.priority_player = gs._get_active_player()

        return reward, True


    def _handle_scry_choice(self, param, **kwargs):
        """Handle Scry choice: PUT_ON_BOTTOM"""
        gs = self.game_state
        # ... (existing checks and logic) ...
        if hasattr(gs, 'choice_context') and gs.choice_context and gs.choice_context.get("type") == "scry":
            context = gs.choice_context
            player = context["player"]
            if not context.get("cards"):
                # ... (handle error) ...
                gs.choice_context = None # --- ADD: Clear context ---
                gs.phase = gs.PHASE_PRIORITY # --- ADD: Set Phase ---
                return -0.1, False

            card_id = context["cards"].pop(0)
            # ... (rest of logic for putting on bottom) ...

            # If done, finalize and return to priority
            if not context.get("cards"):
                 # ... (logic to put cards back on library) ...
                 logging.debug("Scry finished.")
                 # --- Phase Transition ---
                 gs.choice_context = None
                 if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                      gs.phase = gs.previous_priority_phase
                      gs.previous_priority_phase = None
                 else:
                      gs.phase = gs.PHASE_PRIORITY
                 gs.priority_player = gs._get_active_player()
                 gs.priority_pass_count = 0
                 # --- End Phase Transition ---
            return 0.05, True
        logging.warning("PUT_ON_BOTTOM called outside of Scry context.")
        return -0.1, False

    def _handle_no_op(self, param, **kwargs):
        logging.debug("Executed NO_OP action.")
        return 0.0, True # Return (reward, success_flag)


    def _handle_pay_offspring_cost(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        pending_context = getattr(gs, 'pending_spell_context', None)

        if not pending_context or 'card_id' not in pending_context:
            logging.warning("PAY_OFFSPRING_COST called but no spell context is pending.")
            return -0.1, False

        card_id = pending_context['card_id']
        card = gs._safe_get_card(card_id)
        if not card or not getattr(card, 'is_offspring', False):
            logging.warning(f"Cannot PAY_OFFSPRING_COST: Card {card_id} not found or has no Offspring.")
            return -0.05, False

        offspring_cost_str = getattr(card, 'offspring_cost', None)
        if not offspring_cost_str:
            logging.warning(f"Offspring cost not found on card {card_id}.")
            return -0.05, False

        # Pass existing pending_context to affordability check
        if not self._can_afford_cost_string(player, offspring_cost_str, context=pending_context):
            logging.debug(f"Cannot afford Offspring cost {offspring_cost_str} for {card.name}")
            return -0.05, False

        pending_context['pay_offspring'] = True
        pending_context['offspring_cost_to_pay'] = offspring_cost_str
        logging.debug(f"Offspring cost context flag set for pending {card.name}")
        return 0.01, True # Successful flag setting

    def _handle_cast_for_impending(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        if context is None: context = {}
        if kwargs.get('context'): context.update(kwargs['context'])

        hand_idx = context.get('hand_idx')
        if hand_idx is None or not isinstance(hand_idx, int) or hand_idx >= len(player.get("hand", [])):
            logging.error(f"CAST_FOR_IMPENDING missing or invalid 'hand_idx' in context: {context}")
            return -0.15, False

        card_id = player["hand"][hand_idx]
        card = gs._safe_get_card(card_id)
        if not card or not getattr(card, 'is_impending', False):
            logging.warning(f"Card {card_id} at index {hand_idx} does not have Impending.")
            return -0.05, False

        impending_cost_str = getattr(card, 'impending_cost', None)
        if not impending_cost_str:
             logging.warning(f"Impending cost not found for card {card_id} at index {hand_idx}.")
             return -0.05, False

        # Use full context for affordability check
        if not self._can_afford_cost_string(player, impending_cost_str, context=context):
            logging.debug(f"Cannot afford Impending cost {impending_cost_str} for {card.name}")
            return -0.05, False

        # Create context for casting
        cast_context = context.copy()
        cast_context['use_alt_cost'] = 'impending'
        cast_context['hand_idx'] = hand_idx
        cast_context['source_zone'] = 'hand'
        # --- Flag needed for move_card/ETB ---
        cast_context['cast_for_impending'] = True

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.25 if success else -0.1
        return reward, success

    def _handle_end_turn(self, param, **kwargs):
        gs = self.game_state
        if gs.phase < gs.PHASE_END_STEP:
            gs.phase = gs.PHASE_END_STEP
            gs.priority_pass_count = 0 # Reset priority for end step
            gs.priority_player = gs._get_active_player()
            logging.debug("Fast-forwarding to End Step.")
        elif gs.phase == gs.PHASE_END_STEP:
            gs._pass_priority()
        return 0.0, True # Action logic succeeded

    def _handle_untap_next(self, param, **kwargs):
        gs = self.game_state
        gs._untap_phase(gs._get_active_player())
        gs.phase = gs.PHASE_UPKEEP
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01, True # Small reward, successful

    def _handle_draw_next(self, param, **kwargs):
        gs = self.game_state
        gs._draw_phase(gs._get_active_player())
        gs.phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.05, True # Draw is good, successful

    def _handle_main_phase_end(self, param, **kwargs):
        gs = self.game_state
        if gs.phase == gs.PHASE_MAIN_PRECOMBAT:
            gs.phase = gs.PHASE_BEGIN_COMBAT
        elif gs.phase == gs.PHASE_MAIN_POSTCOMBAT:
            gs.phase = gs.PHASE_END_STEP
        else: # Should not happen if mask is correct
            logging.warning(f"MAIN_PHASE_END called during invalid phase: {gs.phase}")
            return -0.1, False # Failed state change
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01, True # Success

    def _handle_combat_damage(self, param, **kwargs):
        # This is often less of a choice and more a state transition.
        # If it's mapped to an action, it should just signify moving past the damage step.
        gs = self.game_state
        if hasattr(gs,'combat_resolver') and gs.combat_resolver:
            # Resolve might happen implicitly based on priority passes or combat actions.
            # This action might just confirm damage step is done?
            # Let's assume success means proceeding. Actual damage reward is separate.
            # If manual damage assignment is needed, that's a different action.
            logging.debug("COMBAT_DAMAGE action acknowledged (resolution likely handled elsewhere).")
            return 0.0, True # Proceeding is success
        logging.warning("COMBAT_DAMAGE action called but no combat resolver found.")
        return -0.1, False # Cannot proceed

    def _handle_end_phase(self, param, **kwargs):
        # Deprecated - Use specific phase end actions
        logging.warning("Generic END_PHASE action called (likely deprecated).")
        # Maybe map to PASS_PRIORITY?
        gs = self.game_state
        gs._pass_priority()
        return 0.0, True # Pass priority logic is successful

    def _handle_mulligan(self, param, **kwargs):
        """Handle MULLIGAN action."""
        gs = self.game_state
        player = gs.mulligan_player
        if not player:
            logging.warning("MULLIGAN action called but gs.mulligan_player is None.")
            return -0.2, False # Failure

        result = gs.perform_mulligan(player, keep_hand=False)
        if result is True: # Successfully took a mulligan
            mull_count = gs.mulligan_count.get('p1' if player == gs.p1 else 'p2', 1)
            return -0.05 * mull_count, True # Successful mulligan taken (negative reward)
        else: # Result is False (e.g., cannot mulligan further?)
            logging.warning(f"MULLIGAN action failed (perform_mulligan returned False).")
            return -0.2, False # Failed state change

    def _handle_keep_hand(self, param, **kwargs):
        gs = self.game_state
        player = gs.mulligan_player
        if not player:
            logging.warning("KEEP_HAND action called but gs.mulligan_player is None.")
            return -0.2, False # Failure

        # perform_mulligan handles state transition (to opponent or bottoming or game start)
        # It returns True if the 'keep' decision itself was processed, False/None if invalid state/action
        result = gs.perform_mulligan(player, keep_hand=True)
        if result in [True, None]: # True/None indicates decision processed, state advanced
            return 0.05, True # Small reward for progressing mulligan
        else: # Result was False
            logging.warning(f"KEEP_HAND failed (likely invalid state or error in perform_mulligan).")
            return -0.2, False # Failed state change

    def _handle_bottom_card(self, param, context, **kwargs):
        """Handles BOTTOM_CARD action during mulligan. Param is the hand index."""
        gs = self.game_state
        player = gs.bottoming_player # Act on the player who needs to bottom
        hand_idx_to_bottom = param # Agent's choice index from action

        if not player:
            logging.warning("BOTTOM_CARD action called but gs.bottoming_player is None.")
            return -0.2, False

        # Validate context and state
        if not gs.bottoming_in_progress or gs.bottoming_player != player:
            logging.warning("BOTTOM_CARD called but not in bottoming phase for this player.")
            return -0.2, False

        # Check index exists before accessing hand
        if not (0 <= hand_idx_to_bottom < len(player.get("hand", []))):
             logging.warning(f"Invalid hand index {hand_idx_to_bottom} for bottoming.")
             return -0.15, False

        card_id = player["hand"][hand_idx_to_bottom]
        card = gs._safe_get_card(card_id)
        card_name = getattr(card, 'name', card_id)

        # Use GameState method to handle bottoming & state transitions
        success = gs.bottom_card(player, hand_idx_to_bottom)

        if success:
            reward_mod = -0.01 # Small default penalty for bottoming
            if self.card_evaluator:
                try:
                     value = self.card_evaluator.evaluate_card(card_id, "bottoming")
                     reward_mod -= value * 0.05 # More penalty for high value card
                except Exception as e:
                    logging.warning(f"Error evaluating card {card_id} during bottoming: {e}")

            # Check if bottoming is now complete GLOBALLY (gs.bottom_card updates state)
            if not gs.bottoming_in_progress and not gs.mulligan_in_progress:
                 logging.debug("Bottoming action completed the entire mulligan phase.")
                 return 0.05 + reward_mod, True # Finished bottoming process
            elif not gs.bottoming_in_progress and gs.mulligan_in_progress:
                 logging.debug("Bottoming finished for this player, mulligan continues for opponent/next state.")
                 return 0.03 + reward_mod, True # Finished player's part
            else:
                 # More cards needed, stay in bottoming phase for this player
                 logging.debug("Bottoming action chosen, more cards needed.")
                 return 0.02 + reward_mod, True # Incremental success
        else: # Bottoming failed internally
            logging.warning(f"Failed to bottom card index {hand_idx_to_bottom} (gs.bottom_card returned False).")
            return -0.05, False


    def _handle_upkeep_pass(self, param, **kwargs):
        gs = self.game_state
        if gs.phase == gs.PHASE_UPKEEP:
             gs.phase = gs.PHASE_DRAW
             gs.priority_player = gs._get_active_player()
             gs.priority_pass_count = 0
             return 0.01, True # Success
        return -0.1, False # Invalid timing

    def _handle_begin_combat_end(self, param, **kwargs):
        gs = self.game_state
        if gs.phase == gs.PHASE_BEGIN_COMBAT:
            gs.phase = gs.PHASE_DECLARE_ATTACKERS
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
            return 0.01, True # Success
        return -0.1, False # Invalid timing

    def _handle_end_combat(self, param, **kwargs):
        gs = self.game_state
        if gs.phase == gs.PHASE_END_OF_COMBAT:
            gs.phase = gs.PHASE_MAIN_POSTCOMBAT
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
            return 0.01, True # Success
        return -0.1, False # Invalid timing

    def _handle_end_step(self, param, **kwargs):
        # Usually handled by passing priority during end step.
        # If it's an explicit action, it should likely pass priority.
        logging.debug("Handling END_STEP action - passing priority.")
        gs = self.game_state
        gs._pass_priority()
        return 0.01, True # Action performed successfully

    def _handle_pass_priority(self, param, **kwargs):
        gs = self.game_state
        gs._pass_priority() # Let GameState handle the logic
        return 0.0, True # Action execution succeeded

    def _handle_concede(self, param, **kwargs):
        # Actual logic handled in apply_action check before handler call
        # This handler shouldn't technically be reached, but if it is:
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        if me: me['lost_game'] = True
        logging.info("Player conceded.")
        return -10.0, True # Large penalty, action succeeded


    def _handle_play_land(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        hand_idx = param
        context = kwargs.get('context', {})

        if hand_idx >= len(player.get("hand", [])):
            logging.warning(f"PLAY_LAND: Invalid hand index {hand_idx}")
            return -0.2, False

        card_id = player["hand"][hand_idx]
        success = gs.play_land(card_id, player, play_back_face=context.get('play_back_face', False))
        if success:
            return 0.2, True # Success
        else:
            logging.debug(f"PLAY_LAND: Failed (handled by gs.play_land). Card: {card_id}, Back: {context.get('play_back_face', False)}")
            return -0.1, False # Failure

    def _handle_play_spell(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        context = kwargs.get('context', {})
        hand_idx = param

        if hand_idx >= len(player.get("hand", [])):
            logging.warning(f"PLAY_SPELL: Invalid hand index {hand_idx}")
            return -0.2, False

        card_id = player["hand"][hand_idx]
        card = gs._safe_get_card(card_id)
        if not card: return -0.2, False

        if 'hand_idx' not in context: context['hand_idx'] = hand_idx
        if 'source_zone' not in context: context['source_zone'] = 'hand'

        card_value = 0
        if self.card_evaluator:
            eval_context = {"situation": "casting", "current_phase": gs.phase, **context}
            card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context)

        success = gs.cast_spell(card_id, player, context=context)
        if success:
            return 0.1 + card_value * 0.3, True # Success
        else:
            logging.debug(f"PLAY_SPELL: Failed (handled by gs.cast_spell). Card: {card_id}")
            return -0.1, False # Failure
    
    def _handle_play_mdfc_land_back(self, param, **kwargs):
        context = kwargs.get('context', {})
        context['play_back_face'] = True # Ensure flag is set
        # Use standard play_land handler with modified context
        return self._handle_play_land(param, context=context) # Returns (reward, success)

    def _handle_play_mdfc_back(self, param, **kwargs):
        context = kwargs.get('context', {})
        context['cast_back_face'] = True # Ensure flag is set
        # Use standard play_spell handler with modified context
        return self._handle_play_spell(param, context=context) # Returns (reward, success)

    def _handle_play_adventure(self, param, **kwargs):
        context = kwargs.get('context', {})
        context['cast_as_adventure'] = True # Ensure flag is set
        # Use standard play_spell handler with modified context
        return self._handle_play_spell(param, context=context) # Returns (reward, success)

    def _handle_cast_from_exile(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        context = kwargs.get('context', {})
        castable_cards = list(getattr(gs, 'cards_castable_from_exile', set())) # Ensure it's a list for indexing

        if param >= len(castable_cards):
             logging.warning(f"CAST_FROM_EXILE: Invalid index {param}, only {len(castable_cards)} available.")
             return -0.2, False

        card_id = castable_cards[param]
        # Verify card still exists in player's exile (might have moved)
        if card_id not in player.get("exile",[]):
             logging.warning(f"CAST_FROM_EXILE: Card {card_id} no longer in {player['name']}'s exile.")
             return -0.15, False

        context['source_zone'] = 'exile'
        context['source_idx'] = player['exile'].index(card_id) # Find index in exile list for cast_spell

        card_value = 0
        if self.card_evaluator:
            card_value = self.card_evaluator.evaluate_card(card_id, "play")

        success = gs.cast_spell(card_id, player, context=context)
        if success:
            return 0.2 + card_value * 0.3, True # Success
        else:
            logging.debug(f"CAST_FROM_EXILE: Failed (handled by gs.cast_spell). Card: {card_id}")
            return -0.1, False # Failure

    def _handle_attack(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        battlefield_idx = param

        if battlefield_idx >= len(player.get("battlefield", [])):
            logging.warning(f"ATTACK: Invalid battlefield index {battlefield_idx}")
            return -0.2, False

        card_id = player["battlefield"][battlefield_idx]
        card = gs._safe_get_card(card_id)
        if not card: return -0.2, False

        can_attack = False
        if self.combat_handler:
            can_attack = self.combat_handler.is_valid_attacker(card_id)
        else: # Fallback
            if 'creature' in getattr(card, 'card_types', []):
                 tapped_set = player.get("tapped_permanents", set())
                 entered_set = player.get("entered_battlefield_this_turn", set())
                 has_haste = self._has_keyword(card, "haste")
                 if card_id not in tapped_set and not (card_id in entered_set and not has_haste):
                      can_attack = True

        if not hasattr(gs, 'current_attackers'): gs.current_attackers = []
        if not hasattr(gs, 'planeswalker_attack_targets'): gs.planeswalker_attack_targets = {}
        if not hasattr(gs, 'battle_attack_targets'): gs.battle_attack_targets = {}

        if card_id in gs.current_attackers:
            gs.current_attackers.remove(card_id)
            gs.planeswalker_attack_targets.pop(card_id, None)
            gs.battle_attack_targets.pop(card_id, None)
            logging.debug(f"ATTACK: Deselected {card.name}")
            return -0.05, True # Deselection successful
        else:
            if can_attack:
                 gs.current_attackers.append(card_id)
                 logging.debug(f"ATTACK: Declared {card.name} as attacker.")
                 return 0.1, True # Declaration successful
            else:
                 logging.warning(f"ATTACK: {card.name} cannot attack now.")
                 return -0.1, False # Cannot attack (failure)

    def _handle_block(self, param, **kwargs):
        gs = self.game_state
        blocker_player = gs._get_non_active_player()
        battlefield_idx = param
        context = kwargs.get('context', {})

        if battlefield_idx >= len(blocker_player.get("battlefield", [])):
            logging.warning(f"BLOCK: Invalid battlefield index {battlefield_idx}")
            return -0.2, False

        blocker_id = blocker_player["battlefield"][battlefield_idx]
        blocker_card = gs._safe_get_card(blocker_id)
        if not blocker_card or 'creature' not in getattr(blocker_card, 'card_types', []):
             logging.warning(f"BLOCK: {blocker_id} is not a creature.")
             return -0.15, False

        if not hasattr(gs, 'current_block_assignments'): gs.current_block_assignments = {}
        currently_blocking_attacker = None
        for atk_id, blockers_list in gs.current_block_assignments.items():
            if blocker_id in blockers_list:
                currently_blocking_attacker = atk_id; break

        if currently_blocking_attacker:
            gs.current_block_assignments[currently_blocking_attacker].remove(blocker_id)
            if not gs.current_block_assignments[currently_blocking_attacker]:
                del gs.current_block_assignments[currently_blocking_attacker]
            logging.debug(f"BLOCK: Unassigned {blocker_card.name} from blocking {gs._safe_get_card(currently_blocking_attacker).name}")
            return -0.05, True # Deselection successful
        else:
            target_attacker_id = context.get('target_attacker_id')
            if target_attacker_id is None:
                possible_targets = [atk_id for atk_id in getattr(gs, 'current_attackers', []) if self._can_block(blocker_id, atk_id)]
                if possible_targets:
                    possible_targets.sort(key=lambda atk_id: getattr(gs._safe_get_card(atk_id),'power',0), reverse=True)
                    target_attacker_id = possible_targets[0]
                    logging.debug(f"BLOCK: AI chose attacker {gs._safe_get_card(target_attacker_id).name} for {blocker_card.name}")
                else:
                     logging.warning(f"BLOCK: No valid attacker found for {blocker_card.name} to block.")
                     return -0.1, False # No valid attacker to assign

            # Validate chosen/found target
            if target_attacker_id not in getattr(gs, 'current_attackers', []) or not self._can_block(blocker_id, target_attacker_id):
                 logging.warning(f"BLOCK: Cannot legally block chosen attacker {target_attacker_id}")
                 return -0.1, False # Invalid block target

            if target_attacker_id not in gs.current_block_assignments:
                gs.current_block_assignments[target_attacker_id] = []
            if blocker_id not in gs.current_block_assignments[target_attacker_id]:
                gs.current_block_assignments[target_attacker_id].append(blocker_id)
                logging.debug(f"BLOCK: Assigned {blocker_card.name} to block {gs._safe_get_card(target_attacker_id).name}")
                return 0.1, True # Assignment successful
            else: # Should not happen if selection/deselection logic is right
                 logging.debug(f"BLOCK: Redundant block assignment ignored for {blocker_card.name}")
                 return -0.01, False # Redundant action failed

    def _handle_tap_land_for_mana(self, param, **kwargs):
         gs = self.game_state
         player = gs._get_active_player()
         land_idx = param

         if land_idx >= len(player.get("battlefield", [])):
             logging.warning(f"TAP_LAND_FOR_MANA: Invalid land index {land_idx}")
             return -0.2, False

         card_id = player["battlefield"][land_idx]
         success = False
         if gs.mana_system and hasattr(gs.mana_system, 'tap_land_for_mana'):
             success = gs.mana_system.tap_land_for_mana(player, card_id)
         else:
             logging.warning("TAP_LAND_FOR_MANA: ManaSystem not available or missing method.")

         if success:
             return 0.05, True # Success
         else:
             card_name = getattr(gs._safe_get_card(card_id), 'name', card_id)
             logging.warning(f"TAP_LAND_FOR_MANA: Failed (handled by gs.mana_system). Card: {card_name}")
             return -0.1, False # Failure


    def _handle_tap_land_for_effect(self, param, **kwargs):
         gs = self.game_state
         player = gs._get_active_player() # Context might specify controller? Assume active for now.
         land_idx = param
         # Assume ability index 0 for non-mana tap ability from context
         context = kwargs.get('context', {})
         ability_idx = context.get('ability_idx', 0)

         if land_idx >= len(player.get("battlefield", [])):
             logging.warning(f"TAP_LAND_FOR_EFFECT: Invalid land index {land_idx}")
             return -0.2, False

         card_id = player["battlefield"][land_idx]
         card = gs._safe_get_card(card_id)
         if not card or 'land' not in getattr(card,'type_line',''):
             logging.warning(f"TAP_LAND_FOR_EFFECT: Card {card_id} not a land.")
             return -0.15, False

         if not hasattr(gs, 'ability_handler'):
             logging.error("TAP_LAND_FOR_EFFECT: AbilityHandler not found.")
             return -0.15, False

         # Use the generic activate ability handler now
         success = gs.ability_handler.activate_ability(card_id, ability_idx, player)
         if success:
             return 0.15, True # Land effects can be good
         else:
             logging.debug(f"TAP_LAND_FOR_EFFECT failed for {card.name}, ability {ability_idx} (handled by activate_ability).")
             return -0.1, False # Failure
         
    def _handle_activate_ability(self, param, context, **kwargs): # Param (derived from ACTION_MEANINGS) is None here
        """
        Handles the ACTIVATE_ABILITY action.
        Expects 'battlefield_idx' and 'ability_idx' in context.
        Includes checks and handling for the Exhaust mechanic.
        """
        gs = self.game_state
        player = gs._get_active_player() # Ability activation usually on your turn/priority
        # --- Get Indices from CONTEXT ---
        bf_idx = context.get('battlefield_idx')
        # The ability_idx provided in the context is the *index within the list of activated abilities*
        # generated for the action mask (0-2 typically).
        activated_ability_list_idx = context.get('ability_idx')

        if bf_idx is None or activated_ability_list_idx is None:
             logging.error(f"ACTIVATE_ABILITY missing 'battlefield_idx' or 'ability_idx' in context: {context}")
             return -0.15, False

        if not isinstance(bf_idx, int) or not isinstance(activated_ability_list_idx, int):
             logging.error(f"ACTIVATE_ABILITY context indices are not integers: {context}")
             return -0.15, False
        # --- End Context Check ---

        if bf_idx >= len(player.get("battlefield", [])):
            logging.warning(f"ACTIVATE_ABILITY: Invalid battlefield index {bf_idx}")
            return -0.2, False

        card_id = player["battlefield"][bf_idx]
        card = gs._safe_get_card(card_id)
        if not card: return -0.2, False # Card not found

        # Use AbilityHandler to get the ability instance
        if not gs.ability_handler:
            logging.error("Cannot activate ability: AbilityHandler not found.")
            return -0.15, False
        activated_abilities = gs.ability_handler.get_activated_abilities(card_id)

        # Validate the list index provided by the context/action
        if not (0 <= activated_ability_list_idx < len(activated_abilities)):
            logging.warning(f"Invalid ability list index {activated_ability_list_idx} provided for {card.name}. Available: {len(activated_abilities)}")
            return -0.15, False

        ability_to_activate = activated_abilities[activated_ability_list_idx]
        # Get the *internal* activation index stored on the ability object (used for tracking)
        activation_idx_on_card = getattr(ability_to_activate, 'activation_index', -1)

        if activation_idx_on_card == -1:
             logging.error(f"Internal activation_index missing for ability {activated_ability_list_idx} on {card.name}! Cannot track exhaust.")
             # Fail activation if it's supposed to be exhaust
             if getattr(ability_to_activate, 'is_exhaust', False): return -0.15, False

        # --- EXHAUST CHECK ---
        is_exhaust = getattr(ability_to_activate, 'is_exhaust', False)

        if is_exhaust:
             if gs.check_exhaust_used(card_id, activation_idx_on_card): # Use the internal index
                  logging.debug(f"Cannot activate Exhaust ability index {activation_idx_on_card} for {card.name}: Already used.")
                  return -0.05, False # Penalty for trying used exhaust
        # --- END EXHAUST CHECK ---

        # --- PERMISSION CHECK (Timing, Priority - already done by action mask gen) ---
        # We can assume the action is valid timing/priority-wise if it passed the mask.

        # --- PAY COSTS ---
        # Merge game state context with action context
        cost_context = {
            "card_id": card_id, "card": card,
            "ability": ability_to_activate, # Pass the ability object
            "is_ability": True,
            "cause": "ability_activation",
            **context # Include context from the action (e.g., choices for costs)
        }

        costs_paid = False
        if gs.mana_system:
            # Ensure cost string exists
            cost_str = getattr(ability_to_activate, 'cost', None)
            if cost_str is not None:
                 costs_paid = gs.mana_system.pay_mana_cost(player, cost_str, cost_context)
            else:
                 logging.error(f"Ability object for {card.name} missing 'cost' attribute.")
        else: # Fallback if no mana system
            logging.warning("Mana system missing, cannot properly handle cost payment.")
            costs_paid = True # Assume costs paid if no system to check (risky)

        if costs_paid:
            # --- MARK EXHAUST USED (AFTER paying cost, before adding to stack) ---
            if is_exhaust:
                if not gs.mark_exhaust_used(card_id, activation_idx_on_card): # Use internal index
                     logging.error(f"Failed to mark exhaust used for {card.name} index {activation_idx_on_card} despite successful payment!")
                     # Rollback costs? Complex. Fail activation for safety.
                     # gs.mana_system._refund_payment(player, cost_context.get('payment_details')) # Requires payment details stored
                     return -0.2, False
            # --- END MARK EXHAUST USED ---

            # --- TRIGGER EXHAUST ACTIVATED EVENT ---
            if is_exhaust:
                 exhaust_context = {"activator": player, "source_card_id": card_id, "ability_index": activation_idx_on_card}
                 # Make sure ability_handler exists before triggering
                 if gs.ability_handler:
                    gs.ability_handler.check_abilities(card_id, "EXHAUST_ABILITY_ACTIVATED", exhaust_context)
                    # Immediately process triggers resulting from activation
                    gs.ability_handler.process_triggered_abilities()
                 else:
                     logging.warning("Cannot trigger EXHAUST_ABILITY_ACTIVATED: AbilityHandler missing.")
            # --- END TRIGGER EXHAUST ACTIVATED EVENT ---

            # --- ADD TO STACK (Handle Targeting) ---
            effect_text_for_stack = getattr(ability_to_activate, 'effect', getattr(ability_to_activate, 'effect_text', 'Unknown Effect'))
            requires_target = "target" in effect_text_for_stack.lower()

            if requires_target:
                 # Ability needs targets, set up targeting phase
                 logging.debug(f"Activated ability requires target. Entering TARGETING phase.")
                 # Store previous phase unless already in a special phase
                 if gs.phase not in [gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
                    gs.previous_priority_phase = gs.phase
                 else:
                    gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT # Fallback if already special

                 gs.phase = gs.PHASE_TARGETING
                 gs.targeting_context = {
                      "source_id": card_id,
                      "controller": player,
                      "effect_text": effect_text_for_stack,
                      "required_type": gs._get_target_type_from_text(effect_text_for_stack), # Use helper
                      "required_count": 1, # Default, need better parsing for >1 target
                      "min_targets": 1, # Assume required if "target" present
                      "max_targets": 1,
                      "selected_targets": [],
                      # Store info to put the *actual ability instance* on stack AFTER targeting
                      "stack_info": {
                           "item_type": "ABILITY",
                           "source_id": card_id,
                           "controller": player,
                           "context": {
                                "ability_index": activation_idx_on_card, # Use internal index
                                "effect_text": effect_text_for_stack,
                                "ability": ability_to_activate, # <<< Pass the ability object itself
                                "is_exhaust": is_exhaust, # Pass exhaust status if needed for resolution
                                "targets": {} # To be filled
                           }
                      }
                 }
                 # Set priority to the choosing player
                 gs.priority_player = player
                 gs.priority_pass_count = 0
                 logging.debug(f"Set up targeting for ability: {effect_text_for_stack}")

            else: # No targets needed, add directly to stack
                 stack_context = {
                     "ability_index": activation_idx_on_card, # Use internal index
                     "effect_text": effect_text_for_stack,
                     "ability": ability_to_activate, # <<< Pass the ability object itself
                     "is_exhaust": is_exhaust, # Pass exhaust status
                     "targets": {}
                 }
                 gs.add_to_stack("ABILITY", card_id, player, stack_context)
                 logging.debug(f"Added non-targeting ability index {activated_ability_list_idx} ({activation_idx_on_card}) for {card.name} to stack{' (Exhaust)' if is_exhaust else ''}.")

            # --- Calculate Reward ---
            ability_value = 0
            if self.card_evaluator:
                try:
                     # Pass correct ability index (internal activation_idx_on_card)
                     ability_value, _ = self.evaluate_ability_activation(card_id, activation_idx_on_card)
                except Exception as eval_e:
                    logging.error(f"Error evaluating ability activation {card_id}, {activation_idx_on_card}: {eval_e}")

            # Activation success reward + strategic value
            return 0.1 + ability_value * 0.4, True
        else:
            logging.debug(f"Failed to pay cost for ability index {activated_ability_list_idx} ({activation_idx_on_card}) on {card.name}")
            # Cost payment failure might need rollback logic in pay_cost (should be handled by ManaSystem)
            return -0.1, False

    def _handle_loyalty_ability(self, param, action_type, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Needs priority to activate PW ability
        bf_idx = param # Param IS the battlefield index from action mapping
        context = kwargs.get('context',{})

        if bf_idx is None or not isinstance(bf_idx, int):
            logging.error(f"Loyalty ability handler called without valid param (battlefield_idx): {bf_idx}.")
            return -0.2, False

        if bf_idx >= len(player.get("battlefield", [])):
             logging.warning(f"Invalid battlefield index {bf_idx} for loyalty ability.")
             return -0.2, False

        card_id = player["battlefield"][bf_idx]
        card = gs._safe_get_card(card_id)
        if not card or 'planeswalker' not in getattr(card, 'card_types', []):
            logging.warning(f"Card at index {bf_idx} ({getattr(card, 'name', 'N/A')}) is not a planeswalker.")
            return -0.15, False

        # Find appropriate ability index
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
            success = gs.activate_planeswalker_ability(card_id, ability_idx, player)
            if success:
                ability_value, _ = self.evaluate_ability_activation(card_id, ability_idx)
                return 0.05 + ability_value * 0.1, True # Success
            else:
                logging.debug(f"Planeswalker ability activation failed for {card.name}, Index {ability_idx}")
                return -0.1, False # Failure
        else:
            logging.warning(f"Could not find matching loyalty ability for action {action_type} on {card.name}")
            return -0.15, False # Failure
     
    def evaluate_ability_activation(self, card_id, ability_idx):
        """Evaluate strategic value of activating an ability."""
        if hasattr(self.game_state, 'strategic_planner') and self.game_state.strategic_planner:
            return self.game_state.strategic_planner.evaluate_ability_activation(card_id, ability_idx)
        return 0.5, "Default ability value" # Fallback

    def _handle_transform(self, param, **kwargs):
        gs = self.game_state; player = gs._get_active_player() # Transforming usually happens on owner's turn/initiative
        bf_idx = param
        if bf_idx >= len(player.get("battlefield", [])): return -0.2, False

        card_id = player["battlefield"][bf_idx]; card = gs._safe_get_card(card_id)
        if card and hasattr(card, 'transform') and hasattr(card, 'can_transform') and card.can_transform(gs): # Check if possible
            card.transform() # Card method handles its state change
            gs.trigger_ability(card_id, "TRANSFORMED", {"controller": player})
            return 0.1, True # Success
        logging.debug(f"TRANSFORM failed for {getattr(card, 'name', card_id)} (cannot transform).")
        return -0.1, False # Not transformable or cannot transform now (Failure)


    def _handle_discard_card(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Assume player with priority discards, context could override
        hand_idx = param
        if hand_idx >= len(player.get("hand", [])):
             logging.warning(f"DISCARD_CARD: Invalid hand index {hand_idx}")
             return -0.2, False

        card_id = player["hand"][hand_idx] # Do NOT pop yet
        card = gs._safe_get_card(card_id)
        card_name = getattr(card, 'name', card_id)
        value = self.card_evaluator.evaluate_card(card_id, "discard") if self.card_evaluator else 0

        has_madness = False
        madness_cost = None
        if card and "madness" in getattr(card,'oracle_text','').lower():
            has_madness = True
            madness_cost = self._get_madness_cost_str(card)

        target_zone = "exile" if has_madness else "graveyard"

        success_move = gs.move_card(card_id, player, "hand", player, target_zone, cause="discard")

        if success_move and has_madness:
            # Set up madness trigger/context
            if madness_cost: # Only if we found a cost
                gs.madness_cast_available = {'card_id': card_id, 'player': player, 'cost': madness_cost}
                logging.debug(f"Discarded {card_name} with Madness, moved to exile. Cost: {madness_cost}")
            else:
                logging.warning(f"Discarded {card_name} with Madness, but couldn't parse cost. Moving to exile.")
                # Should it still go to exile? Rules say yes. Opportunity to cast just fails.
        elif success_move:
            logging.debug(f"Discarded {card_name} to graveyard.")

        reward = -0.05 - value * 0.2 # Penalty for losing card, worse if high value
        return reward, success_move # Return success of move
    
    def _get_madness_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
            match = re.search(r"madness (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
            if match:
                 cost_str = match.group(1)
                 if cost_str.isdigit(): return f"{{{cost_str}}}"
                 return cost_str
        return None

    def _handle_unlock_door(self, param, context, **kwargs):
        gs = self.game_state
        bf_idx = param
        if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler,'handle_unlock_door'):
             success = gs.ability_handler.handle_unlock_door(bf_idx)
             return 0.3, success # Reward successful unlock
        logging.error("UNLOCK_DOOR: AbilityHandler or method missing.")
        return -0.15, False # Failure if handler missing

    def _handle_select_target(self, param, context, **kwargs):
        """Handles the SELECT_TARGET action. Param is the index (0-9) into the list of currently valid targets."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        target_choice_index = param # Param is the index chosen by the agent

        # Validate context
        if not gs.targeting_context or gs.targeting_context.get("controller") != player:
            logging.warning("SELECT_TARGET called but not in targeting phase for this player.")
            return -0.2, False

        ctx = gs.targeting_context
        required_count = ctx.get('required_count', 1)
        selected_targets = ctx.get('selected_targets', [])

        # Regenerate the list of valid targets the agent could have chosen from NOW
        # This ensures the index maps correctly even if valid targets changed slightly
        valid_targets_list = []
        if gs.targeting_system:
            valid_map = gs.targeting_system.get_valid_targets(ctx["source_id"], player, ctx["required_type"])
            # Flatten map consistently (e.g., sorted by category then ID)
            for category in sorted(valid_map.keys()):
                valid_targets_list.extend(sorted(valid_map[category]))
            # Ensure list uniqueness if needed (should be handled by get_valid_targets ideally)
            valid_targets_list = sorted(list(set(valid_targets_list))) # Simple unique sort
        else:
            logging.error("Targeting system not available during target selection.")
            return -0.15, False # Cannot select target without system

        # Validate the chosen index
        if 0 <= target_choice_index < len(valid_targets_list):
            target_id = valid_targets_list[target_choice_index] # Get the ID using the agent's chosen index

            if target_id not in selected_targets: # Avoid duplicates unless context allows
                selected_targets.append(target_id)
                ctx["selected_targets"] = selected_targets
                logging.debug(f"Selected target {len(selected_targets)}/{required_count}: {target_id} (Choice Index {target_choice_index})")

                # If enough targets are now selected, finalize targeting
                min_targets = ctx.get('min_targets', required_count) # Use min_targets
                if len(selected_targets) >= min_targets: # Met minimum requirement
                    # Max targets check handled here or implicitly by required_count? Check max too.
                    max_targets = ctx.get('max_targets', required_count)
                    if len(selected_targets) > max_targets:
                         logging.error("Selected more targets than allowed!") # Should not happen if mask is correct
                         return -0.15, False # Error state

                    # Proceed to finalize (put targets into stack item context)
                    found_stack_item = False
                    source_id = ctx.get("source_id")
                    copy_instance_id = ctx.get("copy_instance_id") # Handle copies needing targets
                    if source_id:
                        for i in range(len(gs.stack) - 1, -1, -1):
                            item = gs.stack[i]
                            # Match by source ID and potentially copy ID if available
                            item_matches = (isinstance(item, tuple) and item[1] == source_id)
                            if copy_instance_id: # If targeting a copy, match its specific ID
                                item_matches &= (item[3].get('copy_instance_id') == copy_instance_id)

                            if item_matches:
                                new_stack_context = item[3] if len(item) > 3 else {}
                                # Structure targets (e.g., categorized or flat list)
                                # Categorization needed if resolution logic depends on type
                                categorized_targets = defaultdict(list)
                                for tid in selected_targets:
                                     # Determine category (simple example)
                                     cat = gs._determine_target_category(tid) # Need helper method
                                     categorized_targets[cat].append(tid)
                                new_stack_context['targets'] = dict(categorized_targets) # Store categorized dict
                                gs.stack[i] = item[:3] + (new_stack_context,)
                                found_stack_item = True
                                logging.debug(f"Updated stack item {i} (Source: {source_id}) with targets: {new_stack_context['targets']}")
                                break
                    if not found_stack_item:
                         # Check if targeting context was for an ability *not* on stack (e.g. ETB choice?)
                         # This requires a different update mechanism if choice isn't for stack item.
                         logging.error(f"Targeting context active (Source: {source_id}) but couldn't find matching stack item!")
                         gs.targeting_context = None # Clear potentially invalid context
                         gs.phase = gs.PHASE_PRIORITY
                         return -0.2, False

                    # Clear targeting context and return to previous phase
                    gs.targeting_context = None
                    if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                         gs.phase = gs.previous_priority_phase
                         gs.previous_priority_phase = None
                    else: gs.phase = gs.PHASE_PRIORITY # Fallback
                    gs.priority_pass_count = 0
                    gs.priority_player = gs._get_active_player()
                    logging.debug("Targeting complete, returning to priority phase.")
                    return 0.05, True # Success
                else:
                    # More targets needed, stay in targeting phase
                    # Let agent choose next target index.
                    return 0.02, True # Incremental success
            else: # Target already selected
                 logging.warning(f"Target {target_id} (Index {target_choice_index}) already selected.")
                 return -0.05, False # Redundant selection
        else: # Invalid index 'param' provided by agent
             logging.error(f"Invalid SELECT_TARGET action parameter: {target_choice_index}. Valid indices: 0-{len(valid_targets_list)-1}")
             return -0.1, False # Invalid choice

    def _handle_sacrifice_permanent(self, param, context, **kwargs):
        """Handles the SACRIFICE_PERMANENT action. Param is the index (0-9) into the list of currently valid sacrifices."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        sacrifice_choice_index = param # Agent's choice index

        # Validate context
        if not hasattr(gs, 'sacrifice_context') or not gs.sacrifice_context or gs.sacrifice_context.get("controller") != player:
            logging.warning("SACRIFICE_PERMANENT called but not in sacrifice phase for this player.")
            return -0.2, False

        ctx = gs.sacrifice_context
        required_count = ctx.get('required_count', 1)
        selected_perms = ctx.get('selected_permanents', [])

        # Regenerate the list of valid permanents the agent could have chosen from NOW
        valid_perms = []
        perm_type_req = ctx.get('required_type')
        for i, perm_id in enumerate(player.get("battlefield", [])): # Use get for safety
            perm_card = gs._safe_get_card(perm_id)
            if not perm_card: continue
            is_valid_type = False
            if not perm_type_req or perm_type_req == "permanent": is_valid_type = True
            elif hasattr(perm_card, 'card_types') and perm_type_req in perm_card.card_types: is_valid_type = True
            elif hasattr(perm_card, 'subtypes') and perm_type_req in perm_card.subtypes: is_valid_type = True

            if is_valid_type:
                 # Check additional conditions from context if needed (e.g., "non-token")
                 valid_perms.append(perm_id)

        # Validate the chosen index
        if 0 <= sacrifice_choice_index < len(valid_perms):
            sac_id = valid_perms[sacrifice_choice_index] # <<< Use agent's chosen index

            if sac_id not in selected_perms: # Avoid duplicates
                selected_perms.append(sac_id)
                ctx["selected_permanents"] = selected_perms # Update the context
                sac_card = gs._safe_get_card(sac_id)
                logging.debug(f"Selected sacrifice {len(selected_perms)}/{required_count}: {getattr(sac_card, 'name', sac_id)} (Choice Index {sacrifice_choice_index})")

                # If enough sacrifices selected, finalize
                if len(selected_perms) >= required_count:
                    sac_reward_mod = 0
                    # Find stack item requiring the sacrifice and update its context
                    found_stack_item = False
                    stack_source_id = ctx.get("source_id")
                    if stack_source_id:
                        for i in range(len(gs.stack) - 1, -1, -1):
                            item = gs.stack[i]
                            if isinstance(item, tuple) and item[1] == stack_source_id:
                                new_stack_context = item[3] if len(item) > 3 else {}
                                new_stack_context['sacrificed_permanents'] = selected_perms
                                gs.stack[i] = item[:3] + (new_stack_context,)
                                found_stack_item = True
                                logging.debug(f"Updated stack item {i} (Source: {stack_source_id}) with sacrifices: {selected_perms}")
                                break
                    # Handle cases where sacrifice isn't for stack (e.g., cost payment)
                    # Need context to know how to proceed if not stack related. Assume stack for now.
                    if not found_stack_item and stack_source_id:
                        logging.error(f"Sacrifice context active (Source: {stack_source_id}), but couldn't find matching stack item!")

                    # Calculate reward based on value of sacrificed cards
                    if hasattr(self, 'card_evaluator'):
                        for sacrifice_id in selected_perms:
                            sac_reward_mod -= self.card_evaluator.evaluate_card(sacrifice_id, "sacrifice") * 0.2

                    # Clear context and return to previous phase
                    gs.sacrifice_context = None
                    if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                         gs.phase = gs.previous_priority_phase
                         gs.previous_priority_phase = None
                    else: gs.phase = gs.PHASE_PRIORITY
                    gs.priority_pass_count = 0
                    gs.priority_player = gs._get_active_player()
                    logging.debug("Sacrifice choice complete, returning to priority phase.")
                    return 0.1 + sac_reward_mod, True
                else:
                    # More sacrifices needed, stay in SACRIFICE phase
                    return 0.02, True # Incremental success
            else: # Card already selected
                 logging.warning(f"Sacrifice choice index {sacrifice_choice_index} points to already selected permanent {sac_id}.")
                 return -0.05, False
        else: # Invalid index 'param' provided by agent
            logging.error(f"Invalid SACRIFICE_PERMANENT action parameter: {sacrifice_choice_index}. Valid indices: 0-{len(valid_perms)-1}")
            return -0.1, False
    
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
        """Handles the CHOOSE_MODE action. Param is the chosen mode index (0-9). Finalizes choice if criteria met."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        chosen_mode_idx = param # Agent's choice index from action

        # Validate context
        if not gs.choice_context or gs.choice_context.get("type") != "choose_mode" or gs.choice_context.get("player") != player:
             logging.warning("CHOOSE_MODE called out of context.")
             return -0.2, False

        ctx = gs.choice_context
        num_choices = ctx.get("num_choices", 0)
        min_required = ctx.get("min_required", 1)
        max_required = ctx.get("max_required", 1)
        selected_modes = ctx.get("selected_modes", [])
        available_modes_text = ctx.get("available_modes", [])

        # Validate chosen mode index
        if 0 <= chosen_mode_idx < num_choices:
            # Check if maximum choices already reached
            if len(selected_modes) >= max_required:
                logging.warning(f"Attempted to select more modes than allowed ({max_required}) for {ctx.get('card_id')}.")
                return -0.1, False

            # Check if mode already selected (disallow unless specific rule allows - rare)
            if chosen_mode_idx in selected_modes:
                logging.warning(f"Mode index {chosen_mode_idx} already selected for {ctx.get('card_id')}.")
                return -0.05, False # Penalty for redundant choice

            # --- Valid Choice Made ---
            selected_modes.append(chosen_mode_idx)
            ctx["selected_modes"] = selected_modes # Update context immediately
            chosen_mode_text = available_modes_text[chosen_mode_idx] if chosen_mode_idx < len(available_modes_text) else f"Mode {chosen_mode_idx}"
            logging.debug(f"Selected mode {len(selected_modes)}/{max_required}: Mode Index {chosen_mode_idx} ('{chosen_mode_text[:30]}...')")

            # Check if the choice is now complete
            # Complete if max required reached OR min required reached and player passes/finalizes
            finalize_choice = False
            if len(selected_modes) >= max_required:
                 finalize_choice = True
                 logging.debug("Maximum modes selected.")
            # Note: Need a way for player to signal completion if min < max.
            # Could use PASS_PRIORITY action when in PHASE_CHOOSE, or a dedicated FINISH_CHOICE action.
            # For now, assume finalize only when max is reached.

            if finalize_choice:
                 # --- Finalizing the Choice ---
                 card_id = ctx.get("card_id")
                 cast_controller = ctx.get("controller") # Get original caster
                 original_cast_context = ctx.get("original_cast_context", {})
                 final_paid_cost = ctx.get("final_paid_cost", {})

                 if not card_id or not cast_controller:
                      logging.error("CRITICAL: Choice context missing card_id or controller during finalization.")
                      # Clear broken context
                      gs.choice_context = None
                      gs.phase = gs.PHASE_PRIORITY
                      return -0.5, False # Indicate major state error

                 # Prepare the final context for the stack item
                 final_stack_context = original_cast_context.copy() # Start with original cast context
                 final_stack_context['selected_modes'] = selected_modes # Embed the chosen modes
                 final_stack_context['final_paid_cost'] = final_paid_cost # Include cost paid for copies etc.
                 # Clear temporary choice flags from context if any
                 final_stack_context.pop('available_modes', None)
                 final_stack_context.pop('min_required', None)
                 final_stack_context.pop('max_required', None)

                 # Add the spell WITH CHOSEN MODES to the stack
                 gs.add_to_stack("SPELL", card_id, cast_controller, final_stack_context)
                 # add_to_stack handles phase transition back to PRIORITY and resets priority

                 logging.debug(f"Finalized mode choice for {card_id}. Spell added to stack.")

                 # Clear choice context AFTER adding to stack
                 gs.choice_context = None

                 return 0.1, True # Successful choice and finalization
            else:
                 # More modes can/must be chosen, stay in PHASE_CHOOSE
                 return 0.05, True # Incremental success
        else:
            # Invalid mode index chosen by agent
            logging.error(f"Invalid CHOOSE_MODE action parameter: {chosen_mode_idx}. Valid indices: 0-{num_choices-1}")
            return -0.1, False

    def _handle_choose_x(self, param, context, **kwargs):
        """Handles CHOOSE_X action. Param is the chosen X value (1-10)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        x_value = param # Use agent's chosen value directly

        # Validate context
        if not gs.choice_context or gs.choice_context.get("type") != "choose_x" or gs.choice_context.get("player") != player:
            logging.warning("CHOOSE_X called out of context.")
            return -0.2, False

        ctx = gs.choice_context
        max_x = ctx.get("max_x", 0) # Get max allowed based on affordability check done earlier
        min_x = ctx.get("min_x", 0)

        # Validate chosen X value
        if min_x <= x_value <= max_x:
            ctx["chosen_x"] = x_value # Store chosen value

            # --- FINALIZING LOGIC (Update Stack, Pay X Cost, Change Phase) ---
            found_stack_item = False
            source_id = ctx.get("source_id")
            copy_instance_id = ctx.get("copy_instance_id")
            if source_id:
                for i in range(len(gs.stack) - 1, -1, -1):
                    item = gs.stack[i]
                    item_matches = (isinstance(item, tuple) and item[1] == source_id)
                    if copy_instance_id: item_matches &= (item[3].get('copy_instance_id') == copy_instance_id)

                    if item_matches:
                        new_stack_context = item[3] if len(item) > 3 else {}
                        new_stack_context['X'] = x_value # Store chosen X
                        # Store final cost including X component? Or assume ManaSystem tracks pending?
                        # Store chosen X is usually enough for resolution logic.
                        gs.stack[i] = item[:3] + (new_stack_context,)
                        found_stack_item = True
                        logging.debug(f"Updated stack item {i} (Source: {source_id}) with X={x_value}")
                        break
            if not found_stack_item: logging.error("X choice context active but couldn't find stack item!")

            # Pay the X cost (ManaSystem needed)
            # NOTE: Cost payment for X was originally planned during cast_spell setup,
            # but rules state costs paid on resolution *after* choices.
            # For simplicity, assume cost was checked during CHOOSE_X action generation,
            # and PAY it now. More accurate would be to require agent PAY_X action, or pay during resolution.
            # Pay now for simplification.
            x_cost_paid = False
            if gs.mana_system:
                paid_details = gs.mana_system.pay_mana_cost_get_details(player, {'generic': x_value})
                if paid_details: x_cost_paid = True
                else: logging.error(f"Failed to pay X={x_value} mana cost!")
            if not x_cost_paid:
                logging.error("Aborting CHOOSE_X: Failed to pay the required mana for X.")
                gs.choice_context = None # Clear invalid state
                gs.phase = gs.PHASE_PRIORITY # Return to priority
                # Need to handle stack potentially? Rollback?
                return -0.2, False # Failed cost payment

            # Clear choice context and return to previous phase
            gs.choice_context = None
            if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                 gs.phase = gs.previous_priority_phase
                 gs.previous_priority_phase = None
            else: gs.phase = gs.PHASE_PRIORITY
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
            logging.debug(f"Chose X={x_value} and paid cost.")
            return 0.05, True
        else: # Invalid X value
             logging.error(f"Invalid CHOOSE_X action parameter: {x_value}. Valid range: [{min_x}-{max_x}]")
             return -0.1, False


    def _handle_choose_color(self, param, context, **kwargs):
        """Handles CHOOSE_COLOR action. Param is the color index (0-4)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        color_idx = param # Use agent's choice

        # Validate context
        if not gs.choice_context or gs.choice_context.get("type") != "choose_color" or gs.choice_context.get("player") != player:
            logging.warning("CHOOSE_COLOR called out of context.")
            return -0.2, False

        ctx = gs.choice_context
        # Validate chosen color index
        if 0 <= color_idx <= 4:
             chosen_color = ['W','U','B','R','G'][color_idx]
             ctx["chosen_color"] = chosen_color

             # --- FINALIZING LOGIC (Update Stack, Change Phase) ---
             found_stack_item = False
             source_id = ctx.get("source_id")
             copy_instance_id = ctx.get("copy_instance_id")
             if source_id:
                 for i in range(len(gs.stack) - 1, -1, -1):
                     item = gs.stack[i]
                     item_matches = (isinstance(item, tuple) and item[1] == source_id)
                     if copy_instance_id: item_matches &= (item[3].get('copy_instance_id') == copy_instance_id)

                     if item_matches:
                         new_stack_context = item[3] if len(item) > 3 else {}
                         new_stack_context['chosen_color'] = chosen_color
                         gs.stack[i] = item[:3] + (new_stack_context,)
                         found_stack_item = True
                         logging.debug(f"Updated stack item {i} (Source: {source_id}) with chosen color={chosen_color}")
                         break
             if not found_stack_item: logging.error("Color choice context active but couldn't find stack item!")

             # Clear choice context and return to previous phase
             gs.choice_context = None
             if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                  gs.phase = gs.previous_priority_phase
                  gs.previous_priority_phase = None
             else: gs.phase = gs.PHASE_PRIORITY
             gs.priority_player = gs._get_active_player()
             gs.priority_pass_count = 0
             logging.debug(f"Chose color {chosen_color}")
             return 0.05, True
        else: # Invalid color index
            logging.error(f"Invalid CHOOSE_COLOR action parameter: {color_idx}. Valid indices: 0-4")
            return -0.1, False
    
        # --- Placeholder Handlers for unimplemented actions ---
    def _handle_unimplemented(self, param, action_type, **kwargs):
        logging.warning(f"Action handler for {action_type} not implemented.")
        return -0.05 # Small penalty for trying unimplemented action

    def _handle_search_library(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Assumes active player searches
        criteria = self._get_search_criteria_from_param(param)
        if not criteria: return -0.1, False # Invalid search param

        if hasattr(gs, 'search_library_and_choose'):
            # Use context provided by agent/env if available
            ai_choice_context = kwargs.get('context', {}) # Get full context
            ai_choice_context['goal'] = criteria # Add goal if not present

            found_id = gs.search_library_and_choose(player, criteria, ai_choice_context=ai_choice_context)
            if found_id:
                 return 0.4, True # Successful search + find
            else: # Search failed, still shuffle (which is considered success of the *action*)
                 # gs.shuffle_library(player) handled inside search_library_and_choose
                 return 0.0, True # Search performed but nothing found
        logging.error("SEARCH_LIBRARY: GameState missing search_library_and_choose method.")
        return -0.15, False # Failure (cannot perform search)
        
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

    def _handle_dredge(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2 # Who has dredge choice? Needs context. Assume active.
        player = gs._get_active_player()
        gy_choice_idx = param # Agent chose which dredge option (index 0-5)

        # Need context to know which card/value this corresponds to
        # Dredge action generation (_add_special_choice_actions) should provide context.
        dredge_context = context or {} # Get context from kwargs
        if 'gy_idx' not in dredge_context: # OLD dredge pending check
            if not hasattr(gs, 'dredge_pending') or not gs.dredge_pending or gs.dredge_pending['player'] != player:
                 logging.warning("DREDGE action called but no dredge pending or invalid context.")
                 return -0.1, False
            dredge_info = gs.dredge_pending
            dredge_card_id = dredge_info['card_id'] # Use ID from pending state
        else: # NEW context-based approach (prefer this)
            gy_idx = dredge_context['gy_idx']
            if gy_idx >= len(player.get("graveyard",[])): return -0.15, False
            dredge_card_id = player["graveyard"][gy_idx]
            card = gs._safe_get_card(dredge_card_id)
            if not card or "dredge" not in getattr(card,'oracle_text','').lower(): return -0.1, False # Card invalid


        if hasattr(gs, 'perform_dredge') and gs.perform_dredge(player, dredge_card_id):
            # perform_dredge returns True on successful dredge execution
            return 0.3, True
        else:
            logging.warning(f"Dredge failed (perform_dredge returned False for {dredge_card_id}).")
            # Dredge was chosen but failed execution (e.g., not enough cards)
            # Clear pending state if gs.perform_dredge didn't
            if hasattr(gs,'dredge_pending') and gs.dredge_pending: gs.dredge_pending = None
            return -0.05, False
        
    def _handle_add_counter(self, param, context, **kwargs):
        gs = self.game_state
        target_idx = param # Combined battlefield index
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if not target_id:
            logging.warning(f"ADD_COUNTER: Invalid target index {target_idx}.")
            return -0.1, False

        if context is None or 'counter_type' not in context:
            logging.error(f"ADD_COUNTER context missing 'counter_type' for target {target_id}.")
            return -0.15, False

        counter_type = context['counter_type']
        count = context.get('count', 1)

        success = gs.add_counter(target_id, counter_type, count)
        if success:
            reward = 0.1 * count if '+1/+1' in counter_type else 0.05 * count
            return reward, True # Success
        else:
            logging.debug(f"ADD_COUNTER failed for {target_id} (handled by gs.add_counter).")
            return -0.05, False # Failure

    def _handle_remove_counter(self, param, context, **kwargs):
        gs = self.game_state
        target_idx = param
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if not target_id:
             logging.warning(f"REMOVE_COUNTER: Invalid target index {target_idx}.")
             return -0.1, False

        if context is None: # Requires context
            logging.error(f"REMOVE_COUNTER context missing for target {target_id}.")
            return -0.15, False

        counter_type = context.get('counter_type') # Should be provided in context
        count = context.get('count', 1)

        if not counter_type: # Ensure type is present
             logging.error(f"REMOVE_COUNTER context missing 'counter_type' for target {target_id}.")
             return -0.15, False

        success = gs.add_counter(target_id, counter_type, -count) # Use negative count
        if success:
            reward = 0.15 * count if '-1/-1' in counter_type else 0.05 * count
            return reward, True # Success
        else:
            logging.warning(f"REMOVE_COUNTER: gs.add_counter failed for {target_id}")
            return -0.05, False # Failure

    def _handle_proliferate(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player choosing proliferate targets
        if hasattr(gs, 'proliferate') and callable(gs.proliferate):
            chosen_targets = context.get('proliferate_targets') if context else None
            # gs.proliferate returns True if *any* counter was added
            proliferated_something = gs.proliferate(player, targets=chosen_targets)
            # Action succeeds if proliferate logic runs, reward based on outcome
            return (0.3 if proliferated_something else 0.0), True
        else:
             logging.error("Proliferate function missing in GameState.")
             return -0.1, False # Failure (cannot perform)


    def _handle_return_from_graveyard(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Assume player with priority/activating returns
        gy_idx = param

        if gy_idx >= len(player.get("graveyard", [])):
             logging.warning(f"Invalid GY index {gy_idx} for RETURN_FROM_GRAVEYARD.")
             return -0.15, False

        card_id = player["graveyard"][gy_idx] # Do NOT pop yet, move_card handles it
        card_value = self.card_evaluator.evaluate_card(card_id, "return_from_gy") if self.card_evaluator else 0.0
        success = gs.move_card(card_id, player, "graveyard", player, "hand", cause="return_from_gy_action")
        reward = 0.2 + card_value * 0.2
        return reward, success # Return success flag from move_card

    def _handle_reanimate(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        gy_idx = param

        if gy_idx >= len(player.get("graveyard", [])):
             logging.warning(f"Invalid GY index {gy_idx} for REANIMATE.")
             return -0.15, False

        card_id = player["graveyard"][gy_idx] # Do NOT pop yet
        card = gs._safe_get_card(card_id)
        valid_types = ["creature", "artifact", "enchantment", "planeswalker", "land", "battle"]
        if card and any(t in getattr(card, 'card_types', []) or t in getattr(card, 'type_line','').lower() for t in valid_types):
            card_value = self.card_evaluator.evaluate_card(card_id, "reanimate") if self.card_evaluator else 0.0
            success = gs.move_card(card_id, player, "graveyard", player, "battlefield", cause="reanimate_action")
            reward = 0.5 + card_value * 0.3
            return reward, success # Return success flag from move_card
        else:
             logging.warning(f"Cannot reanimate {getattr(card, 'name', card_id)}: Invalid type.")
             return -0.1, False # Failure (invalid type)

    def _handle_return_from_exile(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        exile_idx = param

        if exile_idx >= len(player.get("exile", [])):
            logging.warning(f"Invalid Exile index {exile_idx} for RETURN_FROM_EXILE.")
            return -0.15, False

        card_id = player["exile"][exile_idx] # Do NOT pop yet
        card_value = self.card_evaluator.evaluate_card(card_id, "return_from_exile") if self.card_evaluator else 0.0
        success = gs.move_card(card_id, player, "exile", player, "hand", cause="return_from_exile_action")
        reward = 0.3 + card_value * 0.1
        return reward, success # Return success flag from move_card
    
    def _handle_pay_kicker(self, param, context, **kwargs):
        """Flag intent to pay kicker. param=True/False. Checks affordability."""
        if context is None: context = {}
        context.update(kwargs.get('context', {})) # Merge from kwargs

        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if not pending_context or 'card_id' not in pending_context:
            logging.warning("PAY_KICKER called but no spell context is pending.")
            return -0.1, False # No spell context pending

        card_id = pending_context['card_id']
        card = self.game_state._safe_get_card(card_id)
        if not card or "kicker" not in getattr(card, 'oracle_text', '').lower():
             logging.warning(f"Cannot set kicker flag: Card {card_id} not found or has no kicker.")
             return -0.05, False

        kicker_cost_str = self._get_kicker_cost_str(card) # Use helper
        player = self.game_state._get_active_player() # Get player from GS

        # If trying to pay, check affordability now
        if param is True:
            if kicker_cost_str and self._can_afford_cost_string(player, kicker_cost_str, context=pending_context):
                pending_context['kicked'] = True
                # Store the cost string itself for ManaSystem to use later
                pending_context['kicker_cost_to_pay'] = kicker_cost_str
                logging.debug(f"Kicker context flag set to True for pending {card.name} (Cost: {kicker_cost_str})")
                return 0.01, True
            else:
                logging.warning(f"Cannot afford kicker cost {kicker_cost_str or 'N/A'} for {card.name}")
                return -0.05, False # Cannot set kicker=True if unaffordable or no cost
        else: # param is False
            pending_context['kicked'] = False
            pending_context.pop('kicker_cost_to_pay', None) # Remove cost if not paying
            logging.debug(f"Kicker context flag set to False for pending {card.name}")
            return 0.01, True
    
    def _get_kicker_cost_str(self, card):
        """Helper to extract kicker cost string."""
        if card and hasattr(card, 'oracle_text'):
            # Prioritize kicker cost directly after the word 'kicker'
            direct_match = re.search(r"\bkicker\s*(\{.*?\})\b", card.oracle_text.lower())
            if direct_match: return direct_match.group(1)
            # Fallback for kicker costs later in the text (less common format)
            later_match = re.search(r"kicker (?:\{[^\}]+\}|[0-9]+)", card.oracle_text.lower()) # Original pattern as fallback
            if later_match:
                cost_str = later_match.group(1)
                if cost_str.isdigit(): return f"{{{cost_str}}}"
                return cost_str
        return None

    def _handle_pay_additional_cost(self, param, context, **kwargs):
        """Flag intent to pay additional costs. param=True/False"""
        if context is None: context = {}
        context.update(kwargs.get('context', {})) # Merge from kwargs

        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if not pending_context or 'card_id' not in pending_context:
             logging.warning("PAY_ADDITIONAL_COST called but no spell context is pending.")
             return -0.1, False

        card_id = pending_context['card_id']
        card = self.game_state._safe_get_card(card_id)
        cost_info = self._get_additional_cost_info(card) if card else None # Use helper

        if not cost_info:
             logging.warning(f"PAY_ADDITIONAL_COST called, but no additional cost found on {card.name}")
             return -0.05, False # Action inappropriate if no cost

        player = self.game_state._get_active_player()

        # If trying to pay, check if the non-mana part can be met (mana checked later)
        if param is True:
            # Pass pending context to check helper for costs like Escape
            if self._can_pay_specific_additional_cost(player, cost_info, pending_context):
                pending_context['pay_additional'] = True
                # Store cost info for cast_spell/pay_mana_cost to handle actual payment/action
                pending_context['additional_cost_info'] = cost_info
                logging.debug(f"Additional Cost context flag set to True for pending {card.name}")
                # Agent might need follow-up actions (e.g., SACRIFICE_PERMANENT) if choice is required
                return 0.01, True
            else:
                 logging.warning(f"Cannot meet non-mana part of additional cost for {card.name}")
                 return -0.05, False
        else: # param is False
             if cost_info.get("optional", True): # Can only choose not to pay if optional (Rule 601.2b)
                 # Need to parse if cost is optional from text? Assume mandatory if pattern matched.
                 logging.warning("Skipping mandatory additional cost is usually not allowed.")
                 return -0.05, False # Cannot skip mandatory cost
                 # If optional costs are added later, this needs refinement.
             else:
                  # Cost is mandatory, player *must* choose param=True if able
                  logging.warning("Cannot choose not to pay a mandatory additional cost.")
                  return -0.05, False

    def _can_pay_specific_additional_cost(self, player, cost_info, context):
        """Check if the non-mana part of an additional cost can be met."""
        gs = self.game_state
        cost_type = cost_info.get("type")

        if cost_type == "sacrifice":
            target_type = cost_info.get("target")
            # Check if there's *at least one* valid permanent to sacrifice
            return any(target_type == "permanent" or target_type in getattr(gs._safe_get_card(cid), 'card_types', [])
                       for cid in player.get("battlefield", []))
        elif cost_type == "discard":
            return len(player.get("hand", [])) >= cost_info.get("count", 1)
        elif cost_type == "pay_life":
             return player.get("life", 0) >= cost_info.get("amount", 0)
        elif cost_type == "tap_permanents":
            count_needed = cost_info.get("count", 1)
            target_type = cost_info.get("target_type")
            untapped_matching = 0
            tapped_set = player.get("tapped_permanents", set())
            for cid in player.get("battlefield", []):
                 if cid not in tapped_set:
                      card = gs._safe_get_card(cid)
                      if card and (target_type == "permanent" or target_type in getattr(card, 'card_types', []) or target_type in getattr(card, 'subtypes',[])):
                           untapped_matching += 1
            return untapped_matching >= count_needed
        elif cost_type == "escape_exile": # Check if enough cards in GY are provided
            gy_indices = context.get("gy_indices_escape", []) # indices provided by agent
            valid_indices = [idx for idx in gy_indices if idx < len(player.get("graveyard",[]))]
            # Need the required count from card text (assumed already parsed into context/cost_info?)
            # Let's retrieve it again or assume it's implicitly checked by caller providing enough indices.
            required_count = cost_info.get("count", 0) # Assume count is stored here if needed
            if required_count == 0: # Re-parse if missing
                match = re.search(r"exile (\w+|\d+) other cards?", cost_info.get('description','').lower())
                if match: required_count = self._word_to_number(match.group(1))
            return len(valid_indices) >= required_count
        elif cost_type == "delve": # Just need *some* cards in GY
            return len(player.get("graveyard",[])) > 0


        # Assume true if type unknown or check not implemented, cast_spell will fail later if needed
        logging.warning(f"Cannot validate non-mana additional cost type: {cost_type}")
        return True
         
    def _get_additional_cost_info(self, card):
        """Helper to identify additional costs (sacrifice, discard, pay life etc.)."""
        if card and hasattr(card, 'oracle_text'):
            text = card.oracle_text.lower()
            # Pattern for "As an additional cost to cast..., [ACTION]"
            # Handles variations like "to cast this spell" or "to cast ~"
            match = re.search(r"as an additional cost to cast (?:this spell|.*?),\s+(.+?)(?:\.|$|,)", text)
            if match:
                cost_desc = match.group(1).strip()
                # Sacrifice Creature
                sac_match = re.search(r"sacrifice (a|an|\d*)?\s*(\w+)", cost_desc)
                if sac_match and sac_match.group(2) in ["creature", "artifact", "enchantment", "land", "permanent", "planeswalker"]:
                    return {"type": "sacrifice", "target": sac_match.group(2), "optional": False, "description": cost_desc}
                # Discard Card
                disc_match = re.search(r"discard (\w+|\d*) cards?", cost_desc)
                if disc_match:
                    count = self._word_to_number(disc_match.group(1))
                    return {"type": "discard", "count": count, "optional": False, "description": cost_desc}
                # Pay Life
                life_match = re.search(r"pay (\d+) life", cost_desc)
                if life_match:
                    amount = int(life_match.group(1))
                    return {"type": "pay_life", "amount": amount, "optional": False, "description": cost_desc}
                # Tap Permanents
                tap_match = re.search(r"tap (\w+|\d*) untapped ([\w\s]+?) you control", cost_desc)
                if tap_match:
                     count = self._word_to_number(tap_match.group(1))
                     target_type = tap_match.group(2).strip().replace('s','') # Singularize
                     return {"type": "tap_permanents", "count": count, "target_type": target_type, "optional": False, "description": cost_desc}

                # Unrecognized additional cost type within the pattern
                logging.debug(f"Unrecognized additional cost pattern: {cost_desc}")
                return {"type": "unknown", "optional": False, "description": cost_desc} # Mark as unknown cost
        return None
    
    def _word_to_number(self, word):
        """Convert word representation of number to int."""
        if isinstance(word, int): return word
        if isinstance(word, str) and word.isdigit(): return int(word)
        mapping = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                   "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        return mapping.get(str(word).lower(), 1) # Default to 1 if word not found

    def _handle_pay_escalate(self, param, context, **kwargs):
        """Set number of extra modes chosen via escalate. param=count (e.g., 1 or 2). Checks affordability."""
        if context is None: context = {}
        context.update(kwargs.get('context', {})) # Merge from kwargs

        num_extra_modes = param if isinstance(param, int) and param >= 0 else 0

        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if not pending_context or 'card_id' not in pending_context:
             logging.warning("PAY_ESCALATE called but no spell context is pending.")
             return -0.1, False

        card_id = pending_context['card_id']
        card = self.game_state._safe_get_card(card_id)
        if not card or "escalate" not in getattr(card, 'oracle_text', '').lower():
             logging.warning(f"PAY_ESCALATE called, but card {card_id} not found or has no Escalate.")
             return -0.05, False

        escalate_cost_str = self._get_escalate_cost_str(card) # Use helper
        player = self.game_state._get_active_player()

        # Check affordability for *each* extra mode
        if not escalate_cost_str:
             logging.warning(f"Cannot parse escalate cost for {card.name}")
             return -0.05, False

        # Check if *total* mana for escalate can be paid (relative to base cost affordability)
        # This is complex. ManaSystem needs to verify combined cost later.
        # Simple check: can afford escalate cost N times *in addition* to base? Hard to isolate.
        # We will just store the intent here, and let ManaSystem check during payment.
        # Basic affordability check of just the escalate cost:
        if num_extra_modes > 0 and not self._can_afford_cost_string(player, escalate_cost_str, context=pending_context):
             logging.warning(f"Cannot afford *one* instance of escalate cost {escalate_cost_str} for {card.name}")
             # Note: This doesn't guarantee affordability for N instances + base cost.
             # Maybe don't even check here and let cast_spell fail? Less informative.
             # Let's allow setting intent, fail at cast if overall cost too high.

        # TODO: Add check against number of modes available on the card vs. extra modes chosen.
        # Needs mode parsing first.

        pending_context['escalate_count'] = num_extra_modes
        pending_context['escalate_cost_each'] = escalate_cost_str # Store cost per mode for ManaSystem
        logging.debug(f"Escalate context flag set to {num_extra_modes} for pending {card.name}")
        return 0.01, True
      
    def _handle_copy_permanent(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player()
        target_identifier = context.get('target_identifier', context.get('target_permanent_idx'))

        if target_identifier is None:
             logging.warning(f"Copy Permanent context missing 'target_identifier'")
             return -0.15, False

        target_id, target_owner = gs.get_permanent_by_identifier(target_identifier)
        if not target_id:
             logging.warning(f"Target identifier invalid for copy: {target_identifier}")
             return -0.15, False

        target_card = gs._safe_get_card(target_id)
        if not target_card:
             logging.warning(f"Target card not found for copy: {target_id}")
             return -0.15, False

        token_id = gs.create_token_copy(target_card, player)
        success = token_id is not None
        reward = 0.4 if success else -0.1
        return reward, success # Success based on token creation

    def _handle_copy_spell(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player()
        target_identifier = context.get('target_stack_identifier', context.get('target_spell_idx'))

        if target_identifier is None:
             logging.warning(f"Copy Spell context missing 'target_stack_identifier'")
             return -0.15, False

        target_stack_item = None
        if isinstance(target_identifier, int):
             if 0 <= target_identifier < len(gs.stack) and gs.stack[target_identifier][0] == "SPELL":
                  target_stack_item = gs.stack[target_identifier]
        else:
            for item in gs.stack:
                 if item[0] == "SPELL" and item[1] == target_identifier: target_stack_item = item; break

        if not target_stack_item:
             logging.warning(f"Target stack item not found or not a spell: {target_identifier}")
             return -0.15, False

        item_type, card_id, original_controller, old_context = target_stack_item
        card = gs._safe_get_card(card_id)
        if not card:
            logging.warning(f"Spell card {card_id} not found for copy.")
            return -0.15, False

        import copy
        new_context = copy.deepcopy(old_context)
        new_context["is_copy"] = True
        new_context["needs_new_targets"] = True
        new_context.pop("targets", None)

        gs.add_to_stack("SPELL", card_id, player, new_context)
        logging.debug(f"Successfully copied spell {card.name} onto stack.")
        return 0.4, True # Success
    
    def _find_permanent_id(self, player, identifier):
        """Finds a permanent ID on the player's battlefield using index or ID string."""
        # This might already exist in GameState, just ensuring it's callable
        if isinstance(identifier, int):
             if 0 <= identifier < len(player.get("battlefield", [])):
                  return player["battlefield"][identifier]
        elif isinstance(identifier, str):
             if identifier in player.get("battlefield", []):
                  return identifier
        return None

    def _handle_populate(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player()
        target_identifier = context.get('target_token_identifier', context.get('target_token_idx'))

        if target_identifier is None:
             logging.warning(f"Populate context missing 'target_token_identifier'")
             return -0.15, False

        token_to_copy_id = self._find_permanent_id(player, target_identifier) # Helper finds ID from index/ID
        if not token_to_copy_id:
             logging.warning(f"Target token identifier invalid for populate: {target_identifier}")
             return -0.15, False

        original_token = gs._safe_get_card(token_to_copy_id)
        if not (original_token and getattr(original_token,'is_token', False) and 'creature' in getattr(original_token, 'card_types', [])):
            logging.warning(f"Target for populate {token_to_copy_id} is not a valid creature token.")
            return -0.15, False

        new_token_id = gs.create_token_copy(original_token, player)
        success = new_token_id is not None
        reward = 0.35 if success else -0.1
        return reward, success # Success based on token creation
        
    def _handle_investigate(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Assume player whose effect triggered investigates

        # Use game state helper to get token data
        token_data = gs.get_token_data_by_index(4) # Clue index from ACTION_MEANINGS
        if not token_data:
            logging.warning("Clue token data not found, using fallback for Investigate.")
            token_data = {"name": "Clue", "type_line": "Token Artifact — Clue", "card_types": ["artifact"], "subtypes": ["Clue"], "oracle_text": "{2}, Sacrifice this artifact: Draw a card."}

        success = gs.create_token(player, token_data)
        reward = 0.25 if success else -0.05
        return reward, success # Return success flag
      
    def _handle_foretell(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player() # Player choosing to foretell
        hand_idx = context.get('hand_idx')

        if hand_idx is None:
             logging.warning(f"Foretell context missing 'hand_idx'")
             return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError):
            logging.warning(f"Foretell context has non-integer index: {context}")
            return -0.15, False

        if hand_idx >= len(player["hand"]):
            logging.warning(f"Foretell hand index out of bounds: {hand_idx}")
            return -0.1, False

        card_id = player["hand"][hand_idx]; card = gs._safe_get_card(card_id)
        if not card or "foretell" not in getattr(card, 'oracle_text', '').lower():
            logging.warning(f"Foretell card {card_id} invalid or has no Foretell.")
            return -0.1, False

        cost_str = "{2}" # Standard foretell cost
        if not gs.mana_system.can_pay_mana_cost(player, cost_str):
            logging.debug(f"Foretell failed: Cannot afford cost {cost_str} for {card.name}")
            return -0.05, False

        if not gs.mana_system.pay_mana_cost(player, cost_str):
            logging.warning(f"Foretell failed: Error paying cost for {card.name}")
            return -0.05, False

        success_move = gs.move_card(card_id, player, "hand", player, "exile", cause="foretell")
        if success_move:
            if not hasattr(gs, 'foretold_cards'): gs.foretold_cards = {}
            gs.foretold_cards[card_id] = { 'turn': gs.turn, 'original': card.__dict__.copy() }
            logging.debug(f"Foretold {card.name}")
            return 0.2, True # Success
        else: # Move failed
            # Mana was spent, need rollback? Assume lost for now.
            logging.error(f"Foretell move failed for {card.name}")
            return -0.1, False

    def _handle_amass(self, param, context, **kwargs):
        gs = self.game_state;
        player = gs._get_active_player() # Player whose effect triggered Amass
        amount = context.get('amount', 1)
        if not isinstance(amount, int) or amount <= 0: amount = 1

        success = False
        if hasattr(gs, 'amass') and callable(gs.amass):
            success = gs.amass(player, amount)
        else: logging.error("Amass function missing in GameState.")

        reward = min(0.4, 0.1 * amount) if success else -0.05
        return reward, success # Return success flag

    def _handle_learn(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player Learning
        drew_card = False; discarded_card = False
        reward = 0.0; overall_success = False

        # Option 1: Draw, then discard
        card_drawn_id = None
        if player["library"]:
            card_drawn_id = gs._draw_card(player)
            if card_drawn_id:
                drew_card = True; overall_success = True # Action led to something
                card_name = getattr(gs._safe_get_card(card_drawn_id), 'name', card_drawn_id)
                logging.debug(f"Learn: Drew {card_name}")
                reward += 0.1
            else: pass # Draw failed handled internally
        else: logging.warning(f"Learn: Cannot draw, library empty for {player['name']}")

        if drew_card and player["hand"]:
            chosen_discard_id = None
            # ... (AI discard choice logic) ...
            if self.card_evaluator: lowest_value=float('inf') ; [ (val < lowest_value and (lowest_value:=val, chosen_discard_id:=cid)) for cid in player["hand"] if (val := self.card_evaluator.evaluate_card(cid, "discard")) ]
            else: chosen_discard_id = card_drawn_id if card_drawn_id in player["hand"] else (player["hand"][0] if player["hand"] else None)

            if chosen_discard_id:
                discard_success = gs.move_card(chosen_discard_id, player, "hand", player, "graveyard", cause="learn_discard")
                if discard_success:
                    discarded_card = True; overall_success = True
                    card_name = getattr(gs._safe_get_card(chosen_discard_id), 'name', chosen_discard_id)
                    logging.debug(f"Learn: Discarded {card_name}")
                    reward += 0.05
                else:
                    logging.warning("Learn: Failed to move card to graveyard for discard.")
                    reward -= 0.05
            else: pass # No card chosen to discard (e.g., empty hand after draw?)

        # Option 2: Sideboard interaction (not implemented)

        if overall_success: gs.trigger_ability(None, "LEARNED", {"controller": player, "drew": drew_card, "discarded": discarded_card})
        return reward, overall_success # True if draw or discard happened


    def _handle_venture(self, param, context, **kwargs):
        gs = self.game_state;
        player = gs._get_active_player() # Player venturing
        success = False
        if hasattr(gs, 'venture') and callable(gs.venture):
            success = gs.venture(player)
        else: logging.warning("Venture called but GameState.venture method not implemented.")

        reward = 0.15 if success else -0.05
        return reward, success # Return success flag

    def _handle_exert(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player() # Player declaring attacker
        creature_idx = context.get('creature_idx')

        if creature_idx is None: logging.warning(f"Exert context missing 'creature_idx'"); return -0.15, False
        try: creature_idx = int(creature_idx)
        except (ValueError, TypeError): logging.warning(f"Exert context has non-integer index: {context}"); return -0.15, False

        if creature_idx >= len(player["battlefield"]): logging.warning(f"Exert index out of bounds: {creature_idx}"); return -0.15, False

        card_id = player["battlefield"][creature_idx]
        # Exert choice typically made when declaring attackers.
        # Check if card IS attacking. Combat handler integration needed.
        # Assume for now: if card is in `current_attackers`, can exert.
        if card_id in getattr(gs, 'current_attackers', []):
            if not hasattr(gs, 'exerted_this_combat'): gs.exerted_this_combat = set()
            if card_id not in gs.exerted_this_combat:
                gs.exerted_this_combat.add(card_id)
                card = gs._safe_get_card(card_id)
                logging.debug(f"Exerted {card.name}")
                gs.trigger_ability(card_id, "EXERTED", {"controller": player})
                return 0.2, True # Success
            else: # Already exerted this combat
                logging.debug(f"Cannot Exert: {card_id} already exerted.")
                return -0.05, False # Cannot exert again
        else: # Cannot exert if not attacking
             logging.debug(f"Cannot Exert: {card_id} not currently attacking.")
             return -0.1, False
      

    def _handle_explore(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player whose creature explores
        creature_idx = context.get('creature_idx')

        if creature_idx is None: logging.warning(f"Explore context missing 'creature_idx'"); return -0.15, False
        try: creature_idx = int(creature_idx)
        except (ValueError, TypeError): logging.warning(f"Explore context has non-integer index: {context}"); return -0.15, False

        if creature_idx >= len(player["battlefield"]): logging.warning(f"Explore index out of bounds: {creature_idx}"); return -0.15, False

        card_id = player["battlefield"][creature_idx]
        success = False
        if hasattr(gs, 'explore') and callable(gs.explore):
            success = gs.explore(player, card_id)
        else: logging.error("Explore function missing in GameState.")

        reward = 0.25 if success else -0.05
        return reward, success # Return success flag

    def _handle_adapt(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player() # Player activating adapt
        creature_idx = context.get('creature_idx')
        amount = context.get('amount', 1)

        if creature_idx is None: logging.warning(f"Adapt context missing 'creature_idx'"); return -0.15, False
        try: creature_idx, amount = int(creature_idx), int(amount)
        except (ValueError, TypeError): logging.warning(f"Adapt context has non-integer index/amount: {context}"); return -0.15, False

        if creature_idx >= len(player["battlefield"]): logging.warning(f"Adapt index out of bounds: {creature_idx}"); return -0.15, False

        card_id = player["battlefield"][creature_idx]
        success = False
        if hasattr(gs, 'adapt') and callable(gs.adapt):
            success = gs.adapt(player, card_id, amount) # GS handles cost check + logic
        else: logging.error("Adapt function missing in GameState.")

        reward = 0.1 * amount if success else -0.05
        return reward, success # Return success flag

    def _handle_mutate(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player casting mutate spell
        hand_idx = context.get('hand_idx')
        target_idx = context.get('target_idx')

        if hand_idx is None or target_idx is None: logging.warning(f"Mutate context missing indices: {context}"); return -0.15, False
        try: hand_idx, target_idx = int(hand_idx), int(target_idx)
        except (ValueError, TypeError): logging.warning(f"Mutate context has non-integer indices: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]) or target_idx >= len(player["battlefield"]):
            logging.warning(f"Mutate indices out of bounds H:{hand_idx}, T:{target_idx}")
            return -0.15, False

        mutating_card_id = player["hand"][hand_idx]
        target_id = player["battlefield"][target_idx]
        mutating_card = gs._safe_get_card(mutating_card_id)
        if not mutating_card: return -0.15, False

        mutate_cost_str = None
        match = re.search(r"mutate (\{[^\}]+\})", getattr(mutating_card, 'oracle_text','').lower())
        if match: mutate_cost_str = match.group(1)
        else: # If no explicit cost, cannot mutate via casting
             logging.warning(f"Cannot mutate {mutating_card.name}: No explicit mutate cost found.")
             return -0.05, False

        if not self._can_afford_cost_string(player, mutate_cost_str):
            logging.debug(f"Cannot afford mutate cost {mutate_cost_str} for {mutating_card_id}")
            return -0.05, False

        if not gs.mana_system.pay_mana_cost(player, mutate_cost_str):
            logging.warning(f"Failed to pay mutate cost {mutate_cost_str}")
            return -0.05, False

        # GameState.mutate handles validation (non-human etc.) and merge
        success = False
        if hasattr(gs, 'mutate') and gs.mutate(player, mutating_card_id, target_id):
            # Remove card from hand AFTER successful mutation
            if mutating_card_id in player["hand"]:
                player["hand"].remove(mutating_card_id) # This assumes index might have changed, remove by ID
            else: # Card already moved/gone?
                 logging.warning(f"Mutating card {mutating_card_id} not in hand after successful mutate call.")
            success = True
        else:
            logging.debug(f"Mutate validation/execution failed for {mutating_card_id} -> {target_id}")
            # Rollback cost? Assume wasted.
            success = False

        return (0.6 if success else -0.1), success # Return success flag

        
    def _handle_goad(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player applying goad
        target_idx = context.get('target_creature_idx') # Combined index

        if target_idx is None: logging.warning(f"Goad context missing 'target_creature_idx'"); return -0.15, False
        try: target_idx = int(target_idx)
        except (ValueError, TypeError): logging.warning(f"Goad context has non-integer index: {context}"); return -0.15, False

        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        opponent = gs._get_non_active_player() # Get current opponent
        if target_id and target_owner == opponent: # Can only goad opponent's creatures
            success = False
            if hasattr(gs, 'goad_creature'): success = gs.goad_creature(target_id)
            else: logging.error("goad_creature method missing in GameState.")
            return (0.25 if success else -0.05), success # Return success flag
        else: # Target not opponent's or invalid index
            logging.warning(f"Goad target invalid or not opponent's: {target_id}")
            return -0.1, False
      
    def _handle_boast(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player() # Player activating boast
        creature_idx = context.get('creature_idx')

        if creature_idx is None: logging.warning(f"Boast context missing 'creature_idx'"); return -0.15, False
        try: creature_idx = int(creature_idx)
        except (ValueError, TypeError): logging.warning(f"Boast context has non-integer index: {context}"); return -0.15, False

        if creature_idx >= len(player["battlefield"]): logging.warning(f"Boast index out of bounds: {creature_idx}"); return -0.15, False

        card_id = player["battlefield"][creature_idx]
        card = gs._safe_get_card(card_id)
        if not card or "boast" not in getattr(card, 'oracle_text', '').lower():
             logging.warning(f"Boast card {card_id} invalid or has no Boast."); return -0.1, False

        # Check condition: Attacked this turn?
        if not hasattr(gs, 'attackers_this_turn') or card_id not in gs.attackers_this_turn:
             logging.debug(f"Cannot Boast: {card.name} did not attack this turn."); return -0.1, False
        # Check condition: Already boasted this turn?
        if card_id in getattr(gs, 'boast_activated', set()):
             logging.debug(f"Cannot Boast: {card.name} already boasted this turn."); return -0.1, False

        # Find boast ability (better than assuming index 0 or 1)
        ability_idx_to_activate = -1
        if hasattr(gs, 'ability_handler'):
            abilities = gs.ability_handler.get_activated_abilities(card_id)
            for idx, ab in enumerate(abilities):
                if "boast —" in getattr(ab, 'effect_text', '').lower(): # Check for "Boast —" marker
                    ability_idx_to_activate = idx; break

        if ability_idx_to_activate != -1:
            if gs.ability_handler.can_activate_ability(card_id, ability_idx_to_activate, player):
                # Use generic activate_ability which handles costs and stack
                success = gs.ability_handler.activate_ability(card_id, ability_idx_to_activate, player)
                if success:
                    if not hasattr(gs, 'boast_activated'): gs.boast_activated = set()
                    gs.boast_activated.add(card_id) # Mark after successful activation
                    return 0.3, True # Success
                else: return -0.1, False # Activation failed (e.g. cost)
            else: return -0.1, False # Cannot activate currently
        else:
             logging.warning(f"No ability with 'Boast —' marker found for {card.name}"); return -0.1, False

    def _handle_counter_spell(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player casting counter
        hand_idx = context.get('hand_idx')
        target_stack_idx = context.get('target_spell_idx') # Index on stack

        if hand_idx is None or target_stack_idx is None: logging.warning(f"Counter Spell context missing indices: {context}"); return -0.15, False
        try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
        except (ValueError, TypeError): logging.warning(f"Counter Spell context has non-integer indices: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Counter Spell hand index out of bounds: {hand_idx}"); return -0.1, False
        if target_stack_idx < 0 or target_stack_idx >= len(gs.stack) or gs.stack[target_stack_idx][0] != "SPELL":
             logging.warning(f"Counter Spell target stack index invalid or not a spell: {target_stack_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        # Add targeting info to cast context
        cast_context = {'target_stack_index': target_stack_idx, 'source_zone':'hand', 'hand_idx':hand_idx}
        cast_context.update(context) # Merge other context

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.6 if success else -0.1
        return reward, success # Success flag from cast_spell
    
    def _handle_prevent_damage(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        hand_idx = context.get('hand_idx')

        if hand_idx is None: logging.warning(f"Prevent Damage context missing 'hand_idx'"); return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError): logging.warning(f"Prevent Damage context non-integer index: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Prevent Damage hand index OOB: {hand_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        cast_context = context.copy() # Use provided context for targets etc.
        cast_context['source_zone'] = 'hand'; cast_context['hand_idx'] = hand_idx

        success = gs.cast_spell(card_id, player, context=cast_context)
        # The effect registers on resolution, cast success is rewarded here
        reward = 0.2 if success else -0.1
        return reward, success

    def _handle_redirect_damage(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        hand_idx = context.get('hand_idx')

        if hand_idx is None: logging.warning(f"Redirect Damage context missing 'hand_idx'"); return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError): logging.warning(f"Redirect Damage context non-integer index: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Redirect Damage hand index OOB: {hand_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        cast_context = context.copy()
        cast_context['source_zone'] = 'hand'; cast_context['hand_idx'] = hand_idx

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.3 if success else -0.1
        return reward, success

    def _handle_stifle_trigger(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        hand_idx = context.get('hand_idx')
        target_stack_idx = context.get('target_trigger_idx') # Use specific key

        if hand_idx is None or target_stack_idx is None: logging.warning(f"Stifle context missing indices: {context}"); return -0.15, False
        try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
        except (ValueError, TypeError): logging.warning(f"Stifle context non-integer indices: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Stifle hand index OOB: {hand_idx}"); return -0.1, False
        if target_stack_idx < 0 or target_stack_idx >= len(gs.stack) or gs.stack[target_stack_idx][0] != "TRIGGER":
             logging.warning(f"Stifle target index invalid/not trigger: {target_stack_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        cast_context = {'target_stack_index': target_stack_idx, 'source_zone':'hand', 'hand_idx':hand_idx}
        cast_context.update(context)

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.5 if success else -0.1
        return reward, success
        
    def _handle_counter_ability(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        hand_idx = context.get('hand_idx')
        target_stack_idx = context.get('target_ability_idx') # Use specific key

        if hand_idx is None or target_stack_idx is None: logging.warning(f"Counter Ability context missing indices: {context}"); return -0.15, False
        try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
        except (ValueError, TypeError): logging.warning(f"Counter Ability context non-integer indices: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Counter Ability hand index OOB: {hand_idx}"); return -0.1, False
        if target_stack_idx < 0 or target_stack_idx >= len(gs.stack) or gs.stack[target_stack_idx][0] not in ["ABILITY", "TRIGGER"]:
             logging.warning(f"Counter Ability target index invalid/not ability: {target_stack_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        cast_context = {'target_stack_index': target_stack_idx, 'source_zone':'hand', 'hand_idx':hand_idx}
        cast_context.update(context)

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.5 if success else -0.1
        return reward, success
    
    def _handle_flip_card(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Assume player activating controls the card
        target_idx = context.get('battlefield_idx')

        if target_idx is None: logging.warning(f"Flip Card context missing 'battlefield_idx'"); return -0.15, False
        try: target_idx = int(target_idx)
        except (ValueError, TypeError): logging.warning(f"Flip Card context non-integer index: {context}"); return -0.15, False

        if target_idx >= len(player["battlefield"]): logging.warning(f"Flip Card index out of bounds: {target_idx}"); return -0.15, False

        card_id = player["battlefield"][target_idx]
        success = False
        if hasattr(gs, 'flip_card') and callable(gs.flip_card):
             success = gs.flip_card(card_id)
        else: logging.error("flip_card method missing in GameState.")

        reward = 0.2 if success else -0.1
        return reward, success

    def _handle_equip(self, param, context, **kwargs): # Param is None
        gs = self.game_state
        player = gs._get_active_player()
        if context is None: context = {}

        equip_identifier = context.get('equipment_identifier')
        target_identifier = context.get('target_identifier')

        if equip_identifier is None or target_identifier is None:
            logging.error(f"Equip context missing required identifiers: {context}")
            return -0.15, False

        equip_id = self._find_permanent_id(player, equip_identifier)
        target_id = self._find_permanent_id(player, target_identifier)

        if not equip_id or not target_id:
             logging.warning(f"Equip failed: Invalid identifiers. Equip:'{equip_identifier}', Target:'{target_identifier}'")
             return -0.15, False

        equip_card = gs._safe_get_card(equip_id)
        equip_cost_str = self._get_equip_cost_str(equip_card)

        if not equip_cost_str or not self._can_afford_cost_string(player, equip_cost_str):
            logging.debug(f"Cannot afford equip cost {equip_cost_str or 'N/A'} for {equip_id}")
            return -0.05, False

        if not gs.mana_system.pay_mana_cost(player, equip_cost_str):
            logging.warning(f"Failed to pay equip cost {equip_cost_str}")
            return -0.05, False

        success = False
        if hasattr(gs, 'equip_permanent') and gs.equip_permanent(player, equip_id, target_id):
            success = True
        else:
            logging.debug(f"Equip action failed validation or execution for {equip_id} -> {target_id}")
            # Rollback mana? Assume cost wasted.

        return (0.25 if success else -0.1), success

    def _handle_unequip(self, param, context, **kwargs):
        # Usually UNEQUIP is not a player action, but happens via Equip/Destroy/SBA
        # If mapped to NO_OP, this handler shouldn't be called.
        # If kept as a potential (non-standard) action:
        gs = self.game_state
        player = gs._get_active_player()
        equip_idx = context.get('equip_idx') # Context needed

        if equip_idx is None: logging.warning("Unequip context missing 'equip_idx'"); return -0.15, False
        try: equip_idx = int(equip_idx)
        except (ValueError, TypeError): logging.warning(f"Unequip context has non-integer index: {context}"); return -0.15, False

        if equip_idx >= len(player["battlefield"]): logging.warning(f"Unequip index out of bounds: {equip_idx}"); return -0.15, False

        equip_id = player["battlefield"][equip_idx]
        success = False
        if hasattr(gs, 'unequip_permanent'): success = gs.unequip_permanent(player, equip_id)
        else: logging.error("unequip_permanent method missing in GameState.")

        return (0.1 if success else -0.1), success

    def _handle_attach_aura(self, param, context, **kwargs):
        # Attach usually happens on spell resolution, not as a separate action.
        # This might be for effects that say "Attach target Aura..."
        gs = self.game_state
        player = gs._get_active_player() # Assume active player controls effect
        aura_id = context.get('aura_id')
        target_id = context.get('target_id')

        if not aura_id or not target_id:
            logging.warning(f"ATTACH_AURA context missing aura_id or target_id: {context}")
            return -0.15, False

        success = False
        if hasattr(gs, 'attach_aura'):
            success = gs.attach_aura(player, aura_id, target_id) # GS handles validation
        else: logging.error("attach_aura method missing in GameState.")

        if not success: logging.warning(f"Failed to attach aura {aura_id} to {target_id}")
        return (0.25 if success else -0.1), success

    def _handle_fortify(self, param, context, **kwargs): # Param is None
        gs = self.game_state
        player = gs._get_active_player()
        if context is None: context = {}

        fort_identifier = context.get('fort_identifier')
        target_identifier = context.get('target_identifier')

        if fort_identifier is None or target_identifier is None:
            logging.error(f"Fortify context missing required identifiers: {context}")
            return -0.15, False

        fort_id = self._find_permanent_id(player, fort_identifier)
        target_id = self._find_permanent_id(player, target_identifier)

        if not fort_id or not target_id:
             logging.warning(f"Fortify failed: Invalid identifiers. Fort:'{fort_identifier}', Target:'{target_identifier}'")
             return -0.15, False

        fort_card = gs._safe_get_card(fort_id)
        fort_cost_str = self._get_fortify_cost_str(fort_card)

        if not fort_cost_str or not self._can_afford_cost_string(player, fort_cost_str):
            logging.debug(f"Cannot afford fortify cost {fort_cost_str or 'N/A'} for {fort_id}")
            return -0.05, False

        if not gs.mana_system.pay_mana_cost(player, fort_cost_str):
            logging.warning(f"Failed to pay fortify cost {fort_cost_str}")
            return -0.05, False

        success = False
        if hasattr(gs, 'fortify_land') and gs.fortify_land(player, fort_id, target_id):
            success = True
        else:
            logging.debug(f"Fortify action failed validation or execution for {fort_id} -> {target_id}")
            # Rollback cost?

        return (0.2 if success else -0.1), success

    def _handle_reconfigure(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        card_identifier = context.get('card_identifier', context.get('battlefield_idx')) # Use context, fallback index

        if card_identifier is None:
             logging.error(f"Reconfigure context missing 'card_identifier': {context}")
             return -0.15, False

        card_id = self._find_permanent_id(player, card_identifier)
        if not card_id:
             logging.warning(f"Reconfigure failed: Invalid identifier '{card_identifier}' -> {card_id}")
             return -0.15, False

        card = gs._safe_get_card(card_id)
        # --- Use GameState helpers for cost string retrieval ---
        reconf_cost_str = None
        if hasattr(gs, '_get_reconfigure_cost_str'):
             reconf_cost_str = gs._get_reconfigure_cost_str(card)

        if not reconf_cost_str or not self._can_afford_cost_string(player, reconf_cost_str):
            logging.debug(f"Cannot afford reconfigure cost {reconf_cost_str or 'N/A'} for {card_id}")
            return -0.05, False

        target_id = None # Determine target if attaching (needs logic or context)
        is_attached = hasattr(player, 'attachments') and card_id in player["attachments"]
        if not is_attached: # Trying to attach
             target_identifier_ctx = context.get('target_identifier')
             if not target_identifier_ctx:
                  logging.error("Reconfigure attach requires target identifier in context.")
                  return -0.1, False # Expect agent choice via context
             target_id = self._find_permanent_id(player, target_identifier_ctx)
             if not target_id:
                  logging.warning(f"Reconfigure attach target invalid: {target_identifier_ctx}")
                  return -0.1, False

        if not gs.mana_system.pay_mana_cost(player, reconf_cost_str):
            logging.warning(f"Failed to pay reconfigure cost {reconf_cost_str}")
            return -0.05, False

        success = False
        if hasattr(gs, 'reconfigure_permanent') and gs.reconfigure_permanent(player, card_id, target_id=target_id):
            success = True
        else: logging.debug(f"Reconfigure failed for {card_id}")

        return (0.2 if success else -0.1), success

    def _handle_morph(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player controlling face-down card
        card_idx = context.get('battlefield_idx') # Use context

        if card_idx is None: logging.warning(f"Morph context missing 'battlefield_idx'"); return -0.15, False
        try: card_idx = int(card_idx)
        except (ValueError, TypeError): logging.warning(f"Morph context has non-integer index: {context}"); return -0.15, False

        if card_idx >= len(player["battlefield"]): logging.warning(f"Morph index out of bounds: {card_idx}"); return -0.15, False

        card_id = player["battlefield"][card_idx]
        success = False
        if hasattr(gs, 'turn_face_up'):
            # GS method handles checks, cost payment, state change
            success = gs.turn_face_up(player, card_id, pay_morph_cost=True)
        else: logging.error("turn_face_up method missing in GameState.")

        return (0.3 if success else -0.1), success

    def _handle_manifest(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        card_idx = context.get('battlefield_idx')

        if card_idx is None: logging.warning(f"Manifest context missing 'battlefield_idx'"); return -0.15, False
        try: card_idx = int(card_idx)
        except (ValueError, TypeError): logging.warning(f"Manifest context has non-integer index: {context}"); return -0.15, False

        if card_idx >= len(player["battlefield"]): logging.warning(f"Manifest index out of bounds: {card_idx}"); return -0.15, False

        card_id = player["battlefield"][card_idx]
        success = False
        if hasattr(gs, 'turn_face_up'):
            success = gs.turn_face_up(player, card_id, pay_manifest_cost=True)
        else: logging.error("turn_face_up method missing in GameState.")

        return (0.25 if success else -0.1), success

    def _handle_clash(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player initiating clash
        opponent = gs._get_non_active_player()

        winner = None
        if hasattr(gs, 'clash') and callable(gs.clash):
            winner = gs.clash(player, opponent)
            # Clash itself is successful regardless of win/loss
            reward = 0.1 if winner == player else (0.0 if winner is None else -0.05)
            return reward, True # Action performed successfully
        else:
             logging.error("Clash method missing in GameState.")
             return -0.1, False # Cannot perform action

    def _handle_conspire(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player conspiring
        spell_stack_idx = context.get('spell_stack_idx')
        c1_identifier = context.get('creature1_identifier')
        c2_identifier = context.get('creature2_identifier')

        if spell_stack_idx is None or c1_identifier is None or c2_identifier is None:
             logging.error(f"Conspire context missing required indices: {context}")
             return -0.15, False
        try: spell_stack_idx = int(spell_stack_idx)
        except (ValueError, TypeError): return -0.15, False

        success = False
        if hasattr(gs, 'conspire'):
            success = gs.conspire(player, spell_stack_idx, c1_identifier, c2_identifier)
        else: logging.error("Conspire method missing in GameState.")

        if not success: logging.debug("Conspire action failed validation or execution.")
        return (0.4 if success else -0.1), success

    def _handle_grandeur(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player() # Player activating grandeur
        hand_idx = context.get('hand_idx')

        if hand_idx is None: logging.warning(f"Grandeur context missing 'hand_idx'"); return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError): logging.warning(f"Grandeur context has non-integer index: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Grandeur hand index out of bounds: {hand_idx}"); return -0.1, False

        card_id_to_discard = player["hand"][hand_idx] # Do NOT pop yet
        discard_card = gs._safe_get_card(card_id_to_discard)
        if not discard_card: return -0.1, False

        grandeur_id_on_bf = None; grandeur_bf_idx = -1
        for bf_idx, bf_id in enumerate(player["battlefield"]):
            bf_card = gs._safe_get_card(bf_id)
            if bf_card and bf_card.name == discard_card.name and "grandeur" in getattr(bf_card,'oracle_text','').lower():
                grandeur_id_on_bf = bf_id; grandeur_bf_idx = bf_idx; break

        if not grandeur_id_on_bf:
            logging.warning(f"No card named {discard_card.name} with Grandeur found on battlefield.")
            return -0.1, False

        # Discard the card (pay cost)
        success_discard = gs.move_card(card_id_to_discard, player, "hand", player, "graveyard", cause="grandeur_cost")
        if not success_discard: logging.warning(f"Grandeur failed: Could not discard {discard_card.name}."); return -0.05, False

        # Activate the ability (assume index 0 or find specific grandeur ability)
        # --- Needs specific context/logic to find the correct ability index ---
        grandeur_ability_idx = 0 # Placeholder - find actual index

        success_ability = False
        if hasattr(gs, 'ability_handler'):
            success_ability = gs.ability_handler.activate_ability(grandeur_id_on_bf, grandeur_ability_idx, player)
        else: logging.error("Cannot activate Grandeur: AbilityHandler missing.")

        # Reward success, but action successful even if ability fizzles/fails after cost paid
        reward = 0.35 if success_ability else 0.0 # Base reward for performing discard+activation attempt
        return reward, True # Cost paid, activation attempted = successful action

    def _handle_select_spree_mode(self, param, context, **kwargs): # Param is None
        gs = self.game_state
        player = gs._get_active_player() # Assume player choosing mode has priority
        hand_idx = context.get('hand_idx')
        mode_idx = context.get('mode_idx')

        if hand_idx is None or mode_idx is None: logging.error(f"SELECT_SPREE_MODE context missing indices: {context}"); return -0.15, False
        if not isinstance(hand_idx, int) or not isinstance(mode_idx, int): logging.error(f"SELECT_SPREE_MODE context indices non-integer: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Invalid hand index {hand_idx} for SELECT_SPREE_MODE."); return -0.2, False

        card_id = player["hand"][hand_idx]
        card = gs._safe_get_card(card_id)
        if not card or not getattr(card, 'is_spree', False) or mode_idx >= len(getattr(card,'spree_modes',[])):
            logging.warning(f"Invalid card or mode index for Spree: Hand:{hand_idx}, Mode:{mode_idx}"); return -0.1, False

        # Manage pending context
        if not hasattr(gs, 'pending_spell_context') or gs.pending_spell_context.get('card_id') != card_id:
            gs.pending_spell_context = {'card_id': card_id, 'hand_idx': hand_idx, 'selected_spree_modes': set(), 'spree_costs': {}, 'source_zone': 'hand'}

        selected_modes = gs.pending_spell_context.setdefault('selected_spree_modes', set())
        mode_cost_str = card.spree_modes[mode_idx].get('cost', '')

        if not self._can_afford_cost_string(player, mode_cost_str, context=context):
            logging.warning(f"Cannot afford Spree mode {mode_idx} cost {mode_cost_str} for {card.name}")
            return -0.05, False

        if mode_idx in selected_modes:
            logging.warning(f"Spree mode {mode_idx} already selected for {card.name}")
            # Deselect? Or just fail? Let's fail redundant selection.
            return -0.05, False

        selected_modes.add(mode_idx)
        gs.pending_spell_context['spree_costs'][mode_idx] = mode_cost_str
        logging.debug(f"Added Spree mode {mode_idx} (Cost: {mode_cost_str}) to pending cast for {card.name}")
        gs.phase = gs.PHASE_PRIORITY # Stay in priority
        return 0.05, True # Successful mode selection
    
    def _handle_create_token(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player whose effect creates token
        token_data = gs.get_token_data_by_index(param) # Index 0-4
        if not token_data:
            logging.error(f"CREATE_TOKEN failed: No data found for index {param}.")
            return -0.15, False

        success = gs.create_token(player, token_data)
        reward = 0.15 if success else -0.1
        return reward, success

    def _handle_cycling(self, param, context, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Player cycling
        hand_idx = context.get('hand_idx')

        if hand_idx is None: logging.warning(f"Cycling context missing 'hand_idx'"); return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError): logging.warning(f"Cycling context has non-integer index: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Cycling index out of bounds: {hand_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        card = gs._safe_get_card(card_id)
        if not card or "cycling" not in getattr(card, 'oracle_text', '').lower():
            logging.warning(f"Cycling card {card_id} invalid or has no Cycling."); return -0.1, False

        cost_match = re.search(r"cycling (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
        if not cost_match: logging.warning(f"Cycling cost parse failed for {card.name}"); return -0.1, False

        cost_str = cost_match.group(1)
        if cost_str.isdigit(): cost_str = f"{{{cost_str}}}"

        if not gs.mana_system.can_pay_mana_cost(player, cost_str): logging.debug(f"Cycling failed: Cannot afford cost {cost_str}"); return -0.05, False
        if not gs.mana_system.pay_mana_cost(player, cost_str): logging.warning(f"Cycling cost payment failed for {card.name}"); return -0.05, False

        success_discard = gs.move_card(card_id, player, "hand", player, "graveyard", cause="cycling_discard")
        if success_discard:
            gs._draw_phase(player) # GS handles empty library etc.
            gs.trigger_ability(card_id, "CYCLING", {"controller": player})
            return 0.1, True # Success
        else: # Discard failed
            # Mana cost rollback? Assume wasted.
            logging.error(f"Cycling move failed for {card.name}")
            return -0.05, False
        
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
    
    def _handle_put_to_graveyard(self, param, context, **kwargs):
        """Handle Surveil choice: PUT_TO_GRAVEYARD. Relies on context from _add_special_choice_actions."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if not hasattr(gs, 'choice_context') or gs.choice_context is None or gs.choice_context.get("type") != "surveil":
            logging.warning("PUT_TO_GRAVEYARD called outside of Surveil context.")
            return -0.2, False
        if gs.choice_context.get("player") != player:
            logging.warning("Received PUT_TO_GRAVEYARD choice for non-active choice player.")
            return -0.2, False # Wrong player

        context = gs.choice_context
        if not context.get("cards"):
            logging.warning("Surveil choice PUT_TO_GRAVEYARD made but no cards left to process.")
            gs.choice_context = None # Clear context
            gs.phase = gs.PHASE_PRIORITY # Return to priority
            return -0.1, False # Minor error, but invalid state

        card_id = context["cards"].pop(0) # Process first card in the list
        card = gs._safe_get_card(card_id)
        card_name = getattr(card, 'name', card_id)

        # Use move_card to handle replacements/triggers
        success_move = gs.move_card(card_id, player, "library_implicit", player, "graveyard", cause="surveil")
        if not success_move:
            logging.error(f"Failed to move {card_name} to graveyard during surveil.")
            # Put card back? State is potentially inconsistent. End choice phase.
            gs.choice_context = None
            gs.phase = gs.PHASE_PRIORITY
            return -0.1, False

        logging.debug(f"Surveil: Put {card_name} into graveyard.")

        # If done surveiling, clear context and return to previous phase
        if not context.get("cards"):
            logging.debug("Surveil finished.")
            gs.choice_context = None
            if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                 gs.phase = gs.previous_priority_phase
                 gs.previous_priority_phase = None
            else:
                 gs.phase = gs.PHASE_PRIORITY # Fallback
            gs.priority_pass_count = 0 # Reset priority
            gs.priority_player = gs._get_active_player()
        # Else, stay in CHOICE phase for next card

        # Positive reward for making a valid choice
        card_eval_score = 0
        if self.card_evaluator and card:
             # Evaluate card being put in GY (might be good for recursion)
             card_eval_score = self.card_evaluator.evaluate_card(card_id, "general", context_details={"destination":"graveyard"})
        # Reward higher if putting low-value card in GY
        return 0.05 - card_eval_score * 0.05, True
    
    def _handle_no_op_search_fail(self, param, **kwargs):
        """Handles the dedicated NO_OP action when a search fails."""
        logging.debug("Executed NO_OP_SEARCH_FAIL action.")
        return 0.0, True # Action itself is always successful
    
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
    

    def _handle_manifest(self, param, context, **kwargs):
            """Handle turning a manifested card face up. Expects battlefield_idx in context."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # context passed from apply_action
            card_idx = context.get('battlefield_idx') # Get from context

            if card_idx is not None:
                try: card_idx = int(card_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Manifest context has non-integer index: {context}")
                    return (-0.15, False)

                if card_idx < len(player["battlefield"]):
                    card_id = player["battlefield"][card_idx]
                    # GS method checks if manifested, face down, is creature, and pays cost
                    success = gs.turn_face_up(player, card_id, pay_manifest_cost=True)
                    return (0.25, success) if success else (-0.1, False)
                else: logging.warning(f"Manifest index out of bounds: {card_idx}")
            else: logging.warning(f"Manifest context missing 'battlefield_idx'")
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
        """Generic handler for alternative casting methods. (Updated Context Handling & Madness)"""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if context is None: context = {} # Ensure context is a dict

        # Merge environment/agent context with existing
        if kwargs.get('context'): context.update(kwargs['context'])

        card_id = None
        source_zone = "graveyard" # Default for flashback/escape/aftermath/jumpstart
        validation_ok = True

        alt_cost_name = action_type.replace('CAST_WITH_', '').replace('CAST_FOR_', '').replace('CAST_', '').replace('_',' ').lower()
        context["use_alt_cost"] = alt_cost_name # Flag for mana/cost system

        # --- Identify Source Card and Zone based on Action Type & CONTEXT ---
        if action_type == "CAST_FOR_MADNESS":
            source_zone = "exile"
            # Card ID *MUST* come from context now (provided during action generation)
            card_id = context.get('card_id')
            if not card_id:
                logging.error(f"Madness cast action, but context missing 'card_id': {context}")
                validation_ok = False
            # Verify card is actually in exile (context provided exile_idx)
            elif 'exile_idx' not in context or context['exile_idx'] >= len(player.get(source_zone, [])) or player[source_zone][context['exile_idx']] != card_id:
                logging.error(f"Madness cast context mismatch: Card {card_id} not found at exile_idx {context.get('exile_idx')}")
                validation_ok = False
            # Check against the gs.madness_cast_available state as well for consistency
            elif not hasattr(gs, 'madness_cast_available') or not gs.madness_cast_available or \
                 gs.madness_cast_available.get('card_id') != card_id or \
                 gs.madness_cast_available.get('player') != player:
                 logging.warning(f"Madness cast attempted but does not match current gs.madness_cast_available state. Context: {context}, State: {gs.madness_cast_available}")
                 # Allow attempt anyway? Or fail strictly? Fail strictly for now.
                 validation_ok = False

            # Store exile_idx if needed downstream
            if validation_ok: context['source_idx'] = context['exile_idx']

        # --- Other alt cast types (remain largely the same logic using context indices) ---
        elif action_type in ["CAST_FOR_EMERGE", "CAST_FOR_DELVE", "CAST_WITH_OVERLOAD"]: # Originates from hand
             source_zone = "hand"
             if "hand_idx" not in context:
                 logging.error(f"{action_type} requires 'hand_idx' in context.")
                 validation_ok = False
             else:
                 hand_idx = context["hand_idx"]
                 if hand_idx >= len(player.get(source_zone, [])):
                     logging.error(f"Invalid hand_idx {hand_idx} in context for {action_type}.")
                     validation_ok = False
                 else:
                     card_id = player[source_zone][hand_idx]
                     context['source_idx'] = hand_idx # Store index
        elif action_type in ["CAST_WITH_FLASHBACK", "CAST_WITH_JUMP_START", "CAST_WITH_ESCAPE", "AFTERMATH_CAST"]:
             source_zone = "graveyard"
             if "gy_idx" not in context:
                 logging.error(f"{action_type} requires 'gy_idx' in context. Param={param} is unused if context is missing.")
                 validation_ok = False
             else:
                 gy_idx = context['gy_idx']
                 if gy_idx >= len(player.get(source_zone, [])):
                     logging.error(f"Invalid gy_idx {gy_idx} in context for {action_type}.")
                     validation_ok = False
                 else:
                     card_id = player[source_zone][gy_idx]
                     context['source_idx'] = gy_idx # Store index

        if not card_id or not validation_ok:
            logging.error(f"Cannot determine valid card ID for {action_type} with context: {context}")
            return -0.2, False

        context["source_zone"] = source_zone

        # --- Prepare Additional Cost Info (remains the same logic using context indices) ---
        if action_type == "CAST_WITH_JUMP_START":
            if "discard_idx" not in context or context["discard_idx"] >= len(player.get("hand", [])):
                logging.error(f"Jump-Start requires valid 'discard_idx' in context: {context}")
                return -0.1, False
            context["additional_cost_to_pay"] = {"type": "discard", "hand_idx": context["discard_idx"]}
        elif action_type == "CAST_FOR_EMERGE":
            if "sacrifice_idx" not in context or context["sacrifice_idx"] >= len(player.get("battlefield", [])):
                logging.error(f"Emerge requires valid 'sacrifice_idx' in context: {context}")
                return -0.1, False
            sac_idx = context["sacrifice_idx"]
            sac_id = player["battlefield"][sac_idx]
            sac_card = gs._safe_get_card(sac_id)
            if not sac_card or 'creature' not in getattr(sac_card, 'card_types', []):
                logging.error(f"Emerge sacrifice target index {sac_idx} is not a creature.")
                return -0.1, False
            context["additional_cost_to_pay"] = {"type": "sacrifice", "target_id": sac_id}
            context["sacrificed_cmc"] = getattr(sac_card, 'cmc', 0)
        elif action_type == "CAST_WITH_ESCAPE":
            if "gy_indices_escape" not in context or not isinstance(context["gy_indices_escape"], list):
                logging.error("Escape requires 'gy_indices_escape' list in context.")
                return -0.1, False
            card = gs._safe_get_card(card_id)
            required_exile_count = 0
            match = re.search(r"exile (\w+|\d+) other cards?", getattr(card, 'oracle_text','').lower())
            if match: required_exile_count = self._word_to_number(match.group(1))
            if required_exile_count <= 0: required_exile_count = 1
            actual_gy_indices = [idx for idx in context["gy_indices_escape"] if idx < len(player.get("graveyard",[]))]
            if len(actual_gy_indices) < required_exile_count:
                logging.warning(f"Not enough valid GY indices provided for Escape ({len(actual_gy_indices)}/{required_exile_count})")
                return -0.1, False
            context["additional_cost_to_pay"] = {"type": "escape_exile", "gy_indices": actual_gy_indices[:required_exile_count]}
        elif action_type == "CAST_FOR_DELVE":
             if "gy_indices" not in context or not isinstance(context["gy_indices"], list):
                 logging.error("Delve requires 'gy_indices' list in context.")
                 return -0.1, False
             actual_gy_indices = [idx for idx in context["gy_indices"] if idx < len(player.get("graveyard",[]))]
             context["additional_cost_to_pay"] = {"type": "delve", "gy_indices": actual_gy_indices}
             context["delve_count"] = len(actual_gy_indices)
        if action_type == "AFTERMATH_CAST": context["cast_right_half"] = True

        # --- Cast the spell using GameState ---
        success = gs.cast_spell(card_id, player, context=context)

        if success:
            # --- CLEAR MADNESS STATE ---
            if action_type == "CAST_FOR_MADNESS":
                # Verify it was the correct card before clearing
                if hasattr(gs, 'madness_cast_available') and gs.madness_cast_available and \
                   gs.madness_cast_available.get('card_id') == card_id:
                    gs.madness_cast_available = None # Clear state *after* successful cast
                    logging.debug("Madness state cleared after successful cast.")
            # --- END CLEAR MADNESS STATE ---

            # Calculate reward... (remains the same)
            card_value = 0.25
            if self.card_evaluator:
                 eval_context = {"situation": f"cast_{alt_cost_name}"}; eval_context.update(context)
                 card_value += self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context) * 0.2
            return card_value, True
        else:
            logging.warning(f"Alternative cast failed for {action_type} {card_id}. Handled by gs.cast_spell.")
            # Rollback handled by cast_spell if payment failed partially
            return -0.1, False

    def _handle_cast_split(self, param, action_type, **kwargs):
        """Handler for casting split cards. (Updated Context)"""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        hand_idx = param # Param is hand index
        context = kwargs.get('context', {}) # Use context passed in

        if hand_idx < len(player["hand"]):
            card_id = player["hand"][hand_idx]
            card = gs._safe_get_card(card_id)
            if not card: return -0.2, False

            # Update context based on action_type
            context["source_zone"] = "hand"
            context["hand_idx"] = hand_idx # Add hand_idx for clarity
            if action_type == "CAST_LEFT_HALF": context["cast_left_half"] = True
            elif action_type == "CAST_RIGHT_HALF": context["cast_right_half"] = True
            elif action_type == "CAST_FUSE": context["fuse"] = True

            # Use CardEvaluator to estimate value
            eval_context = {"situation": "casting", **context} # Merge context
            card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context) if self.card_evaluator else 0.0

            if gs.cast_spell(card_id, player, context=context):
                return 0.15 + card_value * 0.2, True # Base reward + value mod
            else:
                 logging.warning(f"Cast split failed ({action_type}) for {card_id}. Handled by gs.cast_spell.")
                 return -0.1, False
        return -0.2, False # Invalid hand index

    # --- Combat Handler Wrappers ---
    def _handle_declare_attackers_done(self, param, context, **kwargs):
        success = apply_combat_action(self.game_state, "DECLARE_ATTACKERS_DONE", param, context=context)
        return (0.05 if success else -0.1), success
    def _handle_declare_blockers_done(self, param, context, **kwargs):
        success = apply_combat_action(self.game_state, "DECLARE_BLOCKERS_DONE", param, context=context)
        return (0.05 if success else -0.1), success
    def _handle_attack_planeswalker(self, param, context, **kwargs):
        # Param = relative PW index (0-4)
        # Context needs attacker ID
        success = apply_combat_action(self.game_state, "ATTACK_PLANESWALKER", param, context=context)
        return (0.1 if success else -0.1), success
    def _handle_assign_multiple_blockers(self, param, context, **kwargs):
        # Context needs {attacker_id: ..., blocker_ids: [...], order: [...]}
        success = apply_combat_action(self.game_state, "ASSIGN_MULTIPLE_BLOCKERS", param, context=context)
        return (0.1 if success else -0.1), success
    def _handle_first_strike_order(self, param, context, **kwargs):
        # Context needs {attacker_id: ..., order: [...]}
        success = apply_combat_action(self.game_state, "FIRST_STRIKE_ORDER", param, context=context)
        return (0.05 if success else -0.1), success
    def _handle_assign_combat_damage(self, param, context, **kwargs):
        # Context might have manual assignments, or None for auto
        success = apply_combat_action(self.game_state, "ASSIGN_COMBAT_DAMAGE", param, context=context)
        return (0.05 if success else -0.1), success
    def _handle_protect_planeswalker(self, param, context, **kwargs):
        # Context needs {blocker_id: ..., planeswalker_id: ...}
        success = apply_combat_action(self.game_state, "PROTECT_PLANESWALKER", param, context=context)
        return (0.15 if success else -0.1), success
    def _handle_attack_battle(self, param, context, **kwargs):
        # Param = relative battle index (0-4)
        # Context needs attacker_id
        success = apply_combat_action(self.game_state, "ATTACK_BATTLE", param, context=context)
        return (0.1 if success else -0.1), success
    def _handle_defend_battle(self, param, context, **kwargs):
        # Context needs {'battle_identifier': X, 'defender_identifier': Y}
        success = apply_combat_action(self.game_state, "DEFEND_BATTLE", param, context=context)
        return (0.1 if success else -0.1), success
    def _handle_ninjutsu(self, param, context, **kwargs):
        # Context needs {ninja_hand_idx: X, attacker_id: Y}
        success = apply_combat_action(self.game_state, "NINJUTSU", param, context=context)
        return (0.3 if success else -0.1), success
     
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
        """Helper to extract escalate cost string."""
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
        for i in range(min(len(player.get("graveyard",[])), 6)): # Use get for safety
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "flashback" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"flashback (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                         # Context needs gy_idx
                         context = {'gy_idx': i}
                         set_valid_action(393, f"CAST_WITH_FLASHBACK {card.name}", context=context)

        # Jump-start
        for i in range(min(len(player.get("graveyard",[])), 6)):
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "jump-start" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    if len(player["hand"]) > 0 and self._can_afford_card(player, card):
                        # Context needs gy_idx and choice of card to discard
                        # For now, enable action assuming agent will provide discard target later
                        context = {'gy_idx': i}
                        set_valid_action(394, f"CAST_WITH_JUMP_START {card.name}", context=context)

        # Escape
        for i in range(min(len(player.get("graveyard",[])), 6)):
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "escape" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"escape—([^\,]+), exile ([^\.]+)", card.oracle_text.lower())
                    if cost_match:
                        cost_str = cost_match.group(1).strip()
                        exile_req_str = cost_match.group(2).strip()
                        exile_count = self._word_to_number(re.search(r"(\w+|\d+)", exile_req_str).group(1)) if re.search(r"(\w+|\d+)", exile_req_str) else 1

                        # Check if enough *other* cards exist in GY
                        if len(player["graveyard"]) > exile_count and self._can_afford_cost_string(player, cost_str):
                             # Agent needs to provide list of GY indices to exile later
                             context = {'gy_idx': i}
                             set_valid_action(395, f"CAST_WITH_ESCAPE {card.name}", context=context)

        # Madness (Triggered when discarded, check if castable)
        if hasattr(gs, 'madness_cast_available') and gs.madness_cast_available:
            madness_info = gs.madness_cast_available
            # Only make action available if the player matches and it's *their* turn/priority?
            # Rule 702.34a: Casts it as the triggered ability resolves. Let's allow if player matches.
            if madness_info['player'] == player:
                card_id = madness_info['card_id']
                cost_str = madness_info['cost']
                card = gs._safe_get_card(card_id)

                # Find the card in exile to provide context for handler
                exile_idx = -1
                for idx, exiled_id in enumerate(player.get("exile", [])):
                    if exiled_id == card_id:
                         exile_idx = idx
                         break

                # Check affordability
                if exile_idx != -1 and self._can_afford_cost_string(player, cost_str):
                    context = {'exile_idx': exile_idx, 'card_id': card_id} # Pass required context
                    set_valid_action(396, f"CAST_FOR_MADNESS {card.name if card else card_id}", context=context)

        # Overload
        for i in range(min(len(player["hand"]), 8)):
            card_id = player["hand"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "overload" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"overload (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                        context = {'hand_idx': i}
                        set_valid_action(397, f"CAST_WITH_OVERLOAD {card.name}", context=context)

        # Emerge (Sorcery speed only)
        if is_sorcery_speed:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "emerge" in card.oracle_text.lower():
                    cost_match = re.search(r"emerge (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_match:
                        # Check if there's a creature to sacrifice
                        can_sac = any('creature' in getattr(gs._safe_get_card(cid), 'card_types', []) for cid in player.get("battlefield",[]))
                        # Simplified cost check - full check happens later
                        if can_sac and self._can_afford_cost_string(player, cost_match.group(1)):
                             context = {'hand_idx': i}
                             set_valid_action(398, f"CAST_FOR_EMERGE {card.name}", context=context)

        # Delve (Sorcery speed only)
        if is_sorcery_speed:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "delve" in card.oracle_text.lower():
                    if len(player.get("graveyard",[])) > 0 and self._can_afford_card(player, card): # Simplified check
                        context = {'hand_idx': i}
                        set_valid_action(399, f"CAST_FOR_DELVE {card.name}", context=context)

                    
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