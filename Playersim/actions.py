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

    # Targeting (274-293) = 20 actions
    # Param is index (0-9) into list of valid targets. Handler uses param + targeting_context.
    **{274 + i: ("SELECT_TARGET", i) for i in range(10)},
    # Param is index (0-9) into list of valid sacrifice targets. Handler uses param + sacrifice_context.
    **{284 + i: ("SACRIFICE_PERMANENT", i) for i in range(10)},

    # Gaps filled with NO_OP (294-298) = 5 actions
    **{i: ("NO_OP", None) for i in range(294, 299)},

    # Library/Card Movement (299-308) = 10 actions
    # Param=search type index 0-4
    **{299 + i: ("SEARCH_LIBRARY", i) for i in range(5)},
    304: ("NO_OP_SEARCH_FAIL", None),
    # Param implies choice index, Handler needs choice_context.
    305: ("PUT_TO_GRAVEYARD", None), # Choice index 0 -> Surveil GY
    306: ("PUT_ON_TOP", None),       # Choice index 0 -> Scry/Surveil Top
    307: ("PUT_ON_BOTTOM", None),    # Choice index 0 -> Scry Bottom
    # Param is graveyard index 0-5. Handler needs dredge_pending context.
    **{i: ("DREDGE", i-308) for i in range(308, 314)}, # Corrected Dredge index to match space (6 slots)
    # Gap from 314 to 318 needs filling if Dredge was only 1 slot previously (it was index 308). Revert to single DREDGE?
    # Let's revert to a single DREDGE action and use context.
    308: ("DREDGE", None), # Handler expects {'gy_idx': int} in context
    # Gap filling (309-313)
    **{i: ("NO_OP", None) for i in range(309, 314)}, # 5 NO_OPs

    # Counter Management (314-333 -> WAS 309-329 before fixing Dredge) = 20 actions + Prolif
    # Param is index (0-9) into valid targets. Handler needs {'counter_type': str, 'count': int} in context.
    **{314 + i: ("ADD_COUNTER", i) for i in range(10)},
    # Param is index (0-9) into valid targets. Handler needs {'counter_type': str, 'count': int} in context.
    **{324 + i: ("REMOVE_COUNTER", i) for i in range(10)},
    334: ("PROLIFERATE", None), # Param=None. Context optional: {'proliferate_targets': [id,...]}

    # Zone Movement (335-352 -> WAS 330-347) = 18 actions
    # Param is graveyard index 0-5. Handler uses param to find card ID.
    **{335 + i: ("RETURN_FROM_GRAVEYARD", i) for i in range(6)},
    # Param is graveyard index 0-5. Handler uses param to find card ID.
    **{341 + i: ("REANIMATE", i) for i in range(6)},
    # Param is exile index 0-5. Handler uses param to find card ID.
    **{347 + i: ("RETURN_FROM_EXILE", i) for i in range(6)},

    # Modal/Choice (353-377 -> WAS 348-372) = 25 actions
    # Param is mode index 0-9. Handler uses param + choice_context.
    **{353 + i: ("CHOOSE_MODE", i) for i in range(10)},
    # Param is X value 1-10. Handler uses param + choice_context.
    **{363 + i: ("CHOOSE_X_VALUE", i+1) for i in range(10)},
    # Param is WUBRG index 0-4. Handler uses param + choice_context.
    **{373 + i: ("CHOOSE_COLOR", i) for i in range(5)},

    # Advanced Combat (378-382, 388-397 -> WAS 373-377, 383-392) = 15 actions
    # Param=relative PW index 0-4. Handler uses param to identify target PW. Last declared attacker implicit.
    **{378 + i: ("ATTACK_PLANESWALKER", i) for i in range(5)},
    # Gap filled with NO_OP (383-387 -> WAS 378-382) = 5 actions
    **{i: ("NO_OP", None) for i in range(383, 388)},
    # Param=attacker index 0-9. Handler expects {'blocker_identifiers': [id_or_idx,...]} in context.
    **{388 + i: ("ASSIGN_MULTIPLE_BLOCKERS", i) for i in range(10)},

    # Alternative Casting (398-409 -> WAS 393-404) = 12 actions
    # Handlers expect necessary identifiers (gy_idx, hand_idx, sacrifice_idx, etc.) in context. Param often unused.
    398: ("CAST_WITH_FLASHBACK", None), # Context={'gy_idx': int}
    399: ("CAST_WITH_JUMP_START", None), # Context={'gy_idx': int, 'discard_idx': int}
    400: ("CAST_WITH_ESCAPE", None), # Context={'gy_idx': int, 'gy_indices_escape': [int,...]}
    401: ("CAST_FOR_MADNESS", None), # Context={'card_id': str, 'exile_idx': int}
    402: ("CAST_WITH_OVERLOAD", None), # Context={'hand_idx': int}
    403: ("CAST_FOR_EMERGE", None), # Context={'hand_idx': int, 'sacrifice_idx': int}
    404: ("CAST_FOR_DELVE", None), # Context={'hand_idx': int, 'gy_indices': [int,...]}
    # Informational Flags
    405: ("PAY_KICKER", True), # Param indicates choice
    406: ("PAY_KICKER", False),# Param indicates choice
    407: ("PAY_ADDITIONAL_COST", True), # Param indicates choice
    408: ("PAY_ADDITIONAL_COST", False),# Param indicates choice
    # Param=None. Handler expects {'num_extra_modes': int} in context.
    409: ("PAY_ESCALATE", None),

    # Token/Copy (410-417 -> WAS 405-412) = 8 actions
    # Param=predefined token type index 0-4
    **{410 + i: ("CREATE_TOKEN", i) for i in range(5)},
    # Param=None. Handler expects {'target_permanent_identifier': id_or_idx} in context.
    415: ("COPY_PERMANENT", None),
    # Param=None. Handler expects {'target_stack_identifier': id_or_idx} in context.
    416: ("COPY_SPELL", None),
    # Param=None. Handler expects {'target_token_identifier': id_or_idx} in context.
    417: ("POPULATE", None),

    # Specific Mechanics (418-429 -> WAS 413-424) = 12 actions (Handlers mostly rely on context)
    418: ("INVESTIGATE", None), # Context implies source
    419: ("FORETELL", None), # Context={'hand_idx': int}
    420: ("AMASS", None), # Context={'amount': int}
    421: ("LEARN", None), # Context defines source
    422: ("VENTURE", None), # Context defines source
    423: ("EXERT", None), # Context={'creature_idx': int}
    424: ("EXPLORE", None), # Context={'creature_idx': int}
    425: ("ADAPT", None), # Context={'creature_idx': int, 'amount': int}
    426: ("MUTATE", None), # Context={'hand_idx': int, 'target_idx': int}
    427: ("CYCLING", None), # Context={'hand_idx': int}
    428: ("GOAD", None), # Context={'target_creature_identifier': id_or_idx}
    429: ("BOAST", None), # Context={'creature_idx': int}

    # Response Actions (430-434 -> WAS 425-429) = 5 actions (Handlers rely on context for targets/sources)
    430: ("COUNTER_SPELL", None), # Context={'hand_idx': int, 'target_stack_identifier': id_or_idx}
    431: ("COUNTER_ABILITY", None), # Context={'hand_idx': int, 'target_stack_identifier': id_or_idx}
    432: ("PREVENT_DAMAGE", None), # Context={'hand_idx': int, ...}
    433: ("REDIRECT_DAMAGE", None), # Context={'hand_idx': int, ...}
    434: ("STIFLE_TRIGGER", None), # Context={'hand_idx': int, 'target_stack_identifier': id_or_idx}

    # Combat Actions (435-444 -> WAS 430-439) = 10 actions (Handlers rely on context)
    435: ("FIRST_STRIKE_ORDER", None), # Context={'assignments': {atk_id: [blk_id,...]}}
    436: ("ASSIGN_COMBAT_DAMAGE", None), # Context=Optional({'assignments': {atk_id: {tgt_id: dmg,...}}})
    437: ("NINJUTSU", None), # Context={'ninja_identifier': id_or_idx, 'attacker_identifier': id_or_idx}
    438: ("DECLARE_ATTACKERS_DONE", None),
    439: ("DECLARE_BLOCKERS_DONE", None),
    # Param=battlefield index. Handler expects context={'battlefield_idx': int}. Action determines ability type.
    440: ("LOYALTY_ABILITY_PLUS", None),
    441: ("LOYALTY_ABILITY_ZERO", None),
    442: ("LOYALTY_ABILITY_MINUS", None),
    443: ("ULTIMATE_ABILITY", None),
    # Param=None. Handler expects {'pw_identifier': id_or_idx, 'defender_identifier': id_or_idx} in context.
    444: ("PROTECT_PLANESWALKER", None),

    # Card Type Specific (445-459 -> WAS 440-456) = 15 actions (plus NO_OPs)
    # Param=None. Handler expects {'hand_idx': int} in context.
    445: ("CAST_LEFT_HALF", None),
    446: ("CAST_RIGHT_HALF", None),
    447: ("CAST_FUSE", None),
    # Param=None. Handler expects {'gy_idx': int} in context.
    448: ("AFTERMATH_CAST", None),
    # Param=None. Handler expects {'battlefield_idx': int} in context.
    449: ("FLIP_CARD", None),
    # Param=None. Handler expects {'equipment_identifier': id_or_idx, 'target_identifier': id_or_idx} in context.
    450: ("EQUIP", None),
    451: ("NO_OP", None), # Was UNEQUIP
    452: ("NO_OP", None), # Was ATTACH_AURA
    # Param=None. Handler expects {'fort_identifier': id_or_idx, 'target_identifier': id_or_idx} in context.
    453: ("FORTIFY", None),
    # Param=None. Handler expects {'card_identifier': id_or_idx, Optional 'target_identifier': id_or_idx} in context.
    454: ("RECONFIGURE", None),
    # Param=None. Handler expects {'battlefield_idx': int} in context.
    455: ("MORPH", None),
    456: ("MANIFEST", None), # Context implies source, usually top of library
    457: ("CLASH", None), # Context defines source
    # Param=None. Handler expects {'spell_stack_idx': int, 'creature1_identifier': id_or_idx, 'creature2_identifier': id_or_idx} in context.
    458: ("CONSPIRE", None),
    459: ("NO_OP", None), # Was CONVOKE
    # Param=None. Handler expects {'hand_idx': int} in context.
    460: ("GRANDEUR", None),
    461: ("NO_OP", None), # Was HELLBENT

    # Attack Battle (462-466 -> WAS 460-464) = 5 actions
    # Param=relative battle index 0-4. Handler uses param to identify target battle. Last declared attacker implicit.
    **{462 + i: ("ATTACK_BATTLE", i) for i in range(5)},

    # Fill the remaining space (467-479 -> WAS 465-479) with No-Ops
    **{i: ("NO_OP", None) for i in range(467, 480)} # Fill up to 479
}

# Final size verification (Important after re-indexing)
required_size = 480
if len(ACTION_MEANINGS) != required_size:
     logging.error(f"ACTION_MEANINGS size INCORRECT after fixing: {len(ACTION_MEANINGS)}, expected {required_size}.")
     # Manual correction if needed
     for i in range(required_size):
         if i not in ACTION_MEANINGS: ACTION_MEANINGS[i] = ("NO_OP", None)
     keys_to_remove = [k for k in ACTION_MEANINGS if k >= required_size]
     for k in keys_to_remove: del ACTION_MEANINGS[k]
     if len(ACTION_MEANINGS) == required_size: logging.info("ACTION_MEANINGS size corrected.")
     else: logging.critical("ACTION_MEANINGS size correction failed!")

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
            }

    def _handle_level_up_class(self, param, context, **kwargs):
        """Handle leveling up a class card."""
        gs = self.game_state
        player = gs._get_active_player() # Leveling up usually happens on your turn
        class_idx = param # Param is the battlefield index

        if not hasattr(gs, 'ability_handler') or not gs.ability_handler:
            logging.error("LEVEL_UP_CLASS failed: AbilityHandler not found.")
            return -0.15, False

        if class_idx is None or not isinstance(class_idx, int):
            logging.error(f"LEVEL_UP_CLASS failed: Invalid or missing index parameter '{class_idx}'.")
            return -0.15, False

        # Use AbilityHandler's method
        if gs.ability_handler.handle_class_level_up(class_idx):
            return 0.35, True # Reward leveling up
        else:
            logging.debug(f"Leveling up class at index {class_idx} failed (handled by ability_handler).")
            return -0.1, False

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
            """Return the current action mask as boolean array with reasoning. (Updated for New Phases and Delegation)"""
            gs = self.game_state
            try:
                valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                # Ensure player objects are valid
                p1_valid = hasattr(gs, 'p1') and gs.p1 is not None
                p2_valid = hasattr(gs, 'p2') and gs.p2 is not None
                if not p1_valid or not p2_valid:
                    logging.error("Player object(s) not initialized in GameState. Cannot generate actions.")
                    valid_actions[12] = True # Only Concede
                    self.action_reasons = {12: "Error: Players not initialized"}
                    return valid_actions

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
                # Mulligan/Bottoming take absolute precedence
                if getattr(gs, 'mulligan_in_progress', False):
                    if getattr(gs, 'mulligan_player', None) == current_player:
                        set_valid_action(6, "MULLIGAN")
                        set_valid_action(225, "KEEP_HAND")
                    else: # Waiting for opponent mulligan
                        set_valid_action(224, "NO_OP (Waiting for opponent mulligan)")
                    self.action_reasons = action_reasons
                    return valid_actions
                if getattr(gs, 'bottoming_in_progress', False):
                    if getattr(gs, 'bottoming_player', None) == current_player:
                        for i in range(len(current_player.get("hand", []))): # Use get for safety
                            if i < 4: # Limit based on action indices 226-229
                                set_valid_action(226 + i, f"BOTTOM_CARD index {i}")
                        # Always allow finishing bottoming once required number is chosen?
                        # Check if enough cards have been selected to allow completion.
                        required_to_bottom = gs.cards_to_bottom
                        bottomed_count = gs.bottoming_count
                        if bottomed_count >= required_to_bottom:
                            # Need a DONE_BOTTOMING action or automatic transition.
                            # For now, we implicitly allow passing priority to signal completion.
                            set_valid_action(11, "PASS_PRIORITY (Finish Bottoming)") # Overload Pass Priority?
                    else: # Waiting for opponent bottom
                        set_valid_action(224, "NO_OP (Waiting for opponent bottom)")
                    self.action_reasons = action_reasons
                    return valid_actions

                # Check for other special phases (Targeting, Sacrifice, Choice)
                special_phase = None
                if gs.phase == gs.PHASE_TARGETING: special_phase = "TARGETING"
                elif gs.phase == gs.PHASE_SACRIFICE: special_phase = "SACRIFICE"
                elif gs.phase == gs.PHASE_CHOOSE: special_phase = "CHOOSE"

                if special_phase:
                    logging.debug(f"Generating actions limited to {special_phase} phase.")
                    player_is_acting = False
                    if special_phase == "TARGETING" and hasattr(gs, 'targeting_context') and gs.targeting_context and gs.targeting_context.get("controller") == current_player:
                        self._add_targeting_actions(current_player, valid_actions, set_valid_action)
                        player_is_acting = True
                        # Allow pass only if minimum targets selected
                        min_targets = gs.targeting_context.get("min_targets", 1)
                        selected_count = len(gs.targeting_context.get("selected_targets", []))
                        if selected_count >= min_targets:
                             set_valid_action(11, f"PASS_PRIORITY (Finish {special_phase})")
                    elif special_phase == "SACRIFICE" and hasattr(gs, 'sacrifice_context') and gs.sacrifice_context and gs.sacrifice_context.get("controller") == current_player:
                        self._add_sacrifice_actions(current_player, valid_actions, set_valid_action)
                        player_is_acting = True
                        # Allow pass only if minimum targets selected
                        required_count = gs.sacrifice_context.get("required_count", 1)
                        selected_count = len(gs.sacrifice_context.get("selected_permanents", []))
                        if selected_count >= required_count:
                            set_valid_action(11, f"PASS_PRIORITY (Finish {special_phase})")
                    elif special_phase == "CHOOSE" and hasattr(gs, 'choice_context') and gs.choice_context and gs.choice_context.get("player") == current_player:
                        # Logic for specific choice types is already within _add_special_choice_actions
                        self._add_special_choice_actions(current_player, valid_actions, set_valid_action)
                        player_is_acting = True
                        # PASS logic is handled within _add_special_choice_actions based on choice type

                    # Always add Concede
                    set_valid_action(12, "CONCEDE")
                    # If it's not player's turn to act in special phase, only allow PASS/CONCEDE
                    if not player_is_acting:
                         set_valid_action(11, f"PASS_PRIORITY (Waiting Opponent {special_phase})")

                    # Final check: if no actions besides Concede, add Pass as fallback
                    self.action_reasons = action_reasons
                    if np.sum(valid_actions) == 1 and valid_actions[12]:
                         set_valid_action(11, f"FALLBACK PASS (Special Phase {special_phase})")
                    # --- Important: Return here, do not proceed to other action types ---
                    return valid_actions
                # --- End Special Phase Handling ---


                # --- Rest of the logic (Timing checks, standard actions) ---
                has_priority = (getattr(gs, 'priority_player', None) == current_player)
                can_act_sorcery_speed = False
                can_act_instant_speed = has_priority

                # --- Always Available ---
                if has_priority: # Only allow PASS if player has priority
                    set_valid_action(11, "PASS_PRIORITY")
                set_valid_action(12, "CONCEDE")

                if is_my_turn and gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and not gs.stack and has_priority:
                    can_act_sorcery_speed = True

                # Prevent actions during untap/cleanup (except maybe mana for Split Second)
                if gs.phase in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP]:
                    can_act_instant_speed = False # No actions here
                    can_act_sorcery_speed = False

                # Handle Split Second
                split_second_is_active = getattr(gs, 'split_second_active', False)
                if split_second_is_active:
                    logging.debug("Split Second active, limiting actions.")
                    # Only mana abilities are allowed. Keep PASS and CONCEDE? Assume yes for now.
                    # Create temporary filtered valid_actions
                    temp_valid_actions = np.zeros_like(valid_actions)
                    temp_reasons = {}
                    # Add Pass and Concede back if they were valid
                    if valid_actions[11]: temp_valid_actions[11] = True; temp_reasons[11] = action_reasons[11]
                    if valid_actions[12]: temp_valid_actions[12] = True; temp_reasons[12] = action_reasons[12]

                    def set_valid_mana_action(index, reason=""): # Helper for mana abilities
                        if 0 <= index < self.ACTION_SPACE_SIZE:
                            temp_valid_actions[index] = True
                            temp_reasons[index] = reason
                        return True

                    self._add_mana_ability_actions(current_player, temp_valid_actions, set_valid_mana_action) # Populate mana actions

                    # Replace original mask and reasons
                    valid_actions = temp_valid_actions
                    action_reasons = temp_reasons

                else: # Not Split Second - add normal actions based on timing
                    if can_act_sorcery_speed:
                        self._add_sorcery_speed_actions(current_player, opponent, valid_actions, set_valid_action)
                    if can_act_instant_speed:
                        self._add_instant_speed_actions(current_player, opponent, valid_actions, set_valid_action)

                    # --- Phase-Specific Actions ---
                    if has_priority and not gs.stack:
                        self._add_basic_phase_actions(is_my_turn, valid_actions, set_valid_action)

                    # --- Combat Phase Actions (Delegated) ---
                    # Check combat_handler exists before calling delegated methods
                    if has_priority and self.combat_handler:
                        if is_my_turn and gs.phase == gs.PHASE_DECLARE_ATTACKERS:
                            # Delegate to CombatActionHandler
                            self.combat_handler._add_attack_declaration_actions(current_player, opponent, valid_actions, set_valid_action)
                        elif not is_my_turn and gs.phase == gs.PHASE_DECLARE_BLOCKERS and getattr(gs, 'current_attackers', []):
                             # Delegate to CombatActionHandler
                             self.combat_handler._add_block_declaration_actions(current_player, valid_actions, set_valid_action)
                        elif is_my_turn and gs.phase in [gs.PHASE_FIRST_STRIKE_DAMAGE, gs.PHASE_COMBAT_DAMAGE] and not getattr(gs, 'combat_damage_dealt', False):
                             # Delegate to CombatActionHandler
                             self.combat_handler._add_combat_damage_actions(current_player, valid_actions, set_valid_action)
                    elif has_priority and not self.combat_handler:
                         logging.warning("Combat phase actions cannot be generated: CombatActionHandler missing.")

                # --- Final Checks ---
                self.action_reasons = action_reasons
                valid_count = np.sum(valid_actions)
                if valid_count == 0: # Should not happen normally except maybe forced concede
                    logging.warning("No valid actions found! Forcing CONCEDE.")
                    set_valid_action(12, "FALLBACK - CONCEDE")
                elif valid_count == 1 and valid_actions[12]: # Only concede is possible
                    pass # Okay state
                elif valid_count == 2 and valid_actions[11] and valid_actions[12] and has_priority: # Only pass/concede available to priority player
                     pass # Player must pass or concede

                return valid_actions

            except Exception as e:
                logging.error(f"CRITICAL error generating valid actions: {str(e)}", exc_info=True)
                fallback_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                fallback_actions[11] = True
                fallback_actions[12] = True
                self.action_reasons = {11: "Critical Error Fallback", 12: "Critical Error Fallback"}
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
      
    def apply_action(self, action_idx, **kwargs): # Add **kwargs to accept context from env
            """
            Execute the action and get the next observation, reward and done status.
            Overhauled for clarity, correctness, and better reward shaping.
            Includes an internal game loop for SBAs, Triggers, and Stack resolution.
            """
            gs = self.game_state
            # Define player/opponent early for state access
            me = None
            opp = None
            if hasattr(gs, 'p1') and gs.p1 and hasattr(gs, 'p2') and gs.p2:
                me = gs.p1 if gs.agent_is_p1 else gs.p2
                opp = gs.p2 if gs.agent_is_p1 else gs.p1
            else:
                logging.error("Players not initialized in apply_action. Aborting step.")
                obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {}
                info = {"action_mask": np.zeros(self.ACTION_SPACE_SIZE, dtype=bool), "critical_error": True}
                info["action_mask"][12] = True # Allow CONCEDE
                return obs, -5.0, True, False, info

            # --- Initialization for the step ---
            reward = 0.0
            done = False
            truncated = False # Gymnasium API requires truncated flag
            pre_action_pattern = None # Initialize here
            info = {"action_mask": None, "game_result": "undetermined", "critical_error": False} # Default info

            # Regenerate action mask if not available (e.g., start of step)
            if not hasattr(self, 'current_valid_actions') or self.current_valid_actions is None or np.sum(self.current_valid_actions) == 0:
                if hasattr(self, 'generate_valid_actions') and callable(self.generate_valid_actions):
                    self.current_valid_actions = self.generate_valid_actions()
                else:
                    logging.error("Action mask generation method not found!")
                    self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                    self.current_valid_actions[11] = True # Pass
                    self.current_valid_actions[12] = True # Concede

            # *** Get context from environment/pending state ***
            action_context = kwargs.get('context', {})
            # --- MERGE Game State Context for Special Phases ---
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
                    obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {}
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    info["error_message"] = f"Action index out of bounds: {action_idx}"
                    info["critical_error"] = True
                    return obs, -0.5, False, False, info # Penalty, don't end game

                # 2. Validate Against Action Mask
                if not self.current_valid_actions[action_idx]:
                    invalid_reason = self.action_reasons.get(action_idx, 'Not Valid / Unknown Reason')
                    valid_indices = np.where(self.current_valid_actions)[0]
                    logging.warning(f"Invalid action {action_idx} selected (Action Mask False). Reason: [{invalid_reason}]. Valid: {valid_indices}")
                    reward = -0.1 # Standard penalty
                    obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {}
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    info["invalid_action_reason"] = invalid_reason
                    return obs, reward, done, truncated, info

                # Reset invalid action counter if needed (handled by environment)
                # self.invalid_action_count = 0

                # 3. Get Action Info
                action_type, param = self.get_action_info(action_idx)
                logging.info(f"Applying action: {action_idx} -> {action_type}({param}) with context: {action_context}")

                # 4. Store Pre-Action State for Reward Shaping (check players exist)
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

                # 5. Execute Action - Delegate to specific handlers
                handler_func = self.action_handlers.get(action_type)
                action_reward = 0.0
                action_executed = False

                if handler_func:
                    try:
                        import inspect
                        sig = inspect.signature(handler_func)
                        handler_args = {}
                        if 'param' in sig.parameters: handler_args['param'] = param
                        if 'context' in sig.parameters: handler_args['context'] = action_context
                        if 'action_type' in sig.parameters: handler_args['action_type'] = action_type
                        if 'action_index' in sig.parameters: handler_args['action_index'] = action_idx

                        result = handler_func(**handler_args)

                        if isinstance(result, tuple) and len(result) == 2: action_reward, action_executed = result
                        elif isinstance(result, (float, int)): action_reward = float(result); action_executed = True
                        elif isinstance(result, bool): action_reward = 0.05 if result else -0.1; action_executed = result
                        else: action_reward = 0.0; action_executed = True
                        if action_reward is None: action_reward = 0.0

                    except TypeError as te:
                        if "unexpected keyword argument 'context'" in str(te) or "unexpected keyword argument 'action_type'" in str(te):
                            try:
                                sig_param = inspect.signature(handler_func)
                                args_fallback = {'param': param} if 'param' in sig_param.parameters else {}
                                result = handler_func(**args_fallback)
                                if isinstance(result, tuple) and len(result) == 2: action_reward, action_executed = result
                                elif isinstance(result, (float, int)): action_reward, action_executed = float(result), True
                                elif isinstance(result, bool): action_reward, action_executed = (0.05, True) if result else (-0.1, False)
                                else: action_reward, action_executed = 0.0, True
                                if action_reward is None: action_reward = 0.0
                            except Exception as handler_e:
                                logging.error(f"Error executing handler {action_type} (param-only fallback call): {handler_e}", exc_info=True)
                                action_reward, action_executed = -0.2, False
                        else:
                            logging.error(f"TypeError executing handler {action_type} with params {handler_args}: {te}", exc_info=True)
                            action_reward, action_executed = -0.2, False
                    except Exception as handler_e:
                            logging.error(f"Error executing handler {action_type} with params {handler_args}: {handler_e}", exc_info=True)
                            action_reward, action_executed = -0.2, False
                else:
                    logging.warning(f"No handler implemented for action type: {action_type}")
                    action_reward = -0.05 # Small penalty for unimplemented action
                    action_executed = False # Mark as not executed

                # Add action-specific reward to total step reward
                reward += action_reward
                info["action_reward"] = action_reward

                # Check if action failed to execute properly
                if not action_executed:
                    logging.warning(f"Action {action_type}({param}) failed to execute (Handler returned False or error occurred).")
                    obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {}
                    # Generate fresh mask if needed
                    self.current_valid_actions = self.generate_valid_actions() if hasattr(self, 'generate_valid_actions') else np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                    info["action_mask"] = self.current_valid_actions.astype(bool) # Return current mask
                    info["execution_failed"] = True
                    return obs, reward, done, truncated, info

                # --- BEGIN GAME LOOP ---
                resolution_attempts = 0
                max_resolution_attempts = 20 # Safety break
                while resolution_attempts < max_resolution_attempts:
                    resolution_attempts += 1

                    # a. Check State-Based Actions
                    sba_performed = False
                    if hasattr(gs, 'check_state_based_actions'):
                        sba_performed = gs.check_state_based_actions()
                        # Check game end immediately after SBAs (player loss)
                        if (me and me.get("lost_game", False)) or (opp and opp.get("lost_game", False)):
                            done = True; info["game_result"] = "loss" if me.get("lost_game", False) else "win"
                            logging.debug(f"Game ended due to SBA during loop.")
                            break # Exit loop if game ended via SBAs

                    # b. Process Triggered Abilities
                    triggers_queued = False
                    initial_stack_size = len(gs.stack)
                    if hasattr(gs, 'ability_handler') and gs.ability_handler:
                        if hasattr(gs.ability_handler, 'process_triggered_abilities'):
                             # Process triggers (which adds them to stack)
                             gs.ability_handler.process_triggered_abilities()
                             # Check if stack size increased
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
                           resolved = gs.resolve_top_of_stack() # Resolves ONE item and resets priority
                        else:
                           logging.error("GameState missing resolve_top_of_stack method!")

                        if resolved:
                            # Loop continues to check SBAs/Triggers again
                            continue
                        else:
                            # Resolution failed for some reason (e.g., error, fizzle handled as success by resolve_top_of_stack)
                            logging.error(f"Stack resolution failed for top item! Breaking loop.")
                            break

                    # d. Break Condition
                    # State is stable if no SBAs performed, no new triggers added/resolved, and stack doesn't need resolving
                    if not sba_performed and not triggers_queued and not needs_resolution:
                        logging.debug(f"Loop {resolution_attempts}: State stable, exiting game loop.")
                        break

                    # Log continuation if state changed
                    if resolution_attempts > 1 and (sba_performed or triggers_queued):
                        logging.debug(f"Loop {resolution_attempts}: State changed (SBAs/Triggers), re-evaluating.")

                # --- Check for loop limit ---
                if resolution_attempts >= max_resolution_attempts:
                    logging.error(f"Exceeded max game loop iterations ({max_resolution_attempts}) after action {action_type}. Potential loop or complex interaction.")
                    # Consider ending the game or forcing a state change here? For now, just log.
                    # Setting done=True here could prevent true infinite loops in training.
                    # done = True
                    # info["game_result"] = "error_loop"

                # --- END GAME LOOP ---


                # 7. Calculate State Change Reward (Only if players are valid and game not already ended)
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

                # 8. Check Game End Conditions (Re-check after loop)
                if not done: # Only check if game didn't end during loop
                    # Note: check_state_based_actions in the loop should have set loss flags already
                    if opp and opp.get("lost_game"):
                        done = True; reward += 10.0 + max(0, gs.max_turns - gs.turn) * 0.1; info["game_result"] = "win"
                    elif me and me.get("lost_game"):
                        done = True; reward -= 10.0; info["game_result"] = "loss"
                    elif (me and me.get("game_draw")) or (opp and opp.get("game_draw")):
                        done = True; reward += 0.0; info["game_result"] = "draw"
                    elif gs.turn > gs.max_turns:
                        # Ensure turn limit check wasn't already handled and flags set
                        if not getattr(gs, '_turn_limit_checked', False):
                            done, truncated = True, True
                            life_diff_reward = 0
                            if me and opp: life_diff_reward = (me.get("life",0) - opp.get("life",0)) * 0.1
                            reward += life_diff_reward
                            if me and opp:
                                info["game_result"] = "win" if (me.get("life",0) > opp.get("life",0)) else "loss" if (me.get("life",0) < opp.get("life",0)) else "draw"
                            else: info["game_result"] = "draw"
                            gs._turn_limit_checked = True # Mark as checked
                            logging.info(f"Turn limit ({gs.max_turns}) reached. Result: {info['game_result']}")


                # Record results if game ended
                if done and hasattr(self, 'ensure_game_result_recorded'):
                    self.ensure_game_result_recorded()

                # 9. Finalize Step
                obs = self._get_obs() if hasattr(self, '_get_obs') else {}
                self.current_valid_actions = None # Invalidate cache
                next_mask = self.generate_valid_actions().astype(bool)
                info["action_mask"] = next_mask

                # Update strategy memory if implemented
                if hasattr(gs, 'strategy_memory') and gs.strategy_memory and pre_action_pattern is not None:
                    try: gs.strategy_memory.update_strategy(pre_action_pattern, reward)
                    except Exception as strategy_e: logging.error(f"Error updating strategy memory: {strategy_e}")

                return obs, reward, done, truncated, info

            except Exception as e:
                logging.error(f"CRITICAL error in apply_action (Action {action_idx}): {e}", exc_info=True)
                obs = self._get_obs_safe() if hasattr(self, '_get_obs_safe') else {}
                mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); mask[11] = True; mask[12] = True
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
        """Handles BOTTOM_CARD action during mulligan. Param is the hand index (0-3 or more based on max hand size)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        hand_idx_to_bottom = param # Agent's choice index

        # Validate context
        if not gs.bottoming_in_progress or gs.bottoming_player != player:
            logging.warning("BOTTOM_CARD called but not in bottoming phase for this player.")
            return -0.2, False

        # Validate index
        if 0 <= hand_idx_to_bottom < len(player.get("hand", [])):
             card_id = player["hand"][hand_idx_to_bottom]
             card = gs._safe_get_card(card_id)
             card_name = getattr(card, 'name', card_id)

             # Use GameState method to handle bottoming
             if gs.bottom_card(player, hand_idx_to_bottom):
                 reward_mod = -0.01 # Small default penalty for bottoming
                 if self.card_evaluator:
                     # Penalize more for bottoming good cards
                     value = self.card_evaluator.evaluate_card(card_id, "bottoming")
                     reward_mod = (0.5 - value) * 0.1 # Higher penalty if value > 0.5

                 # Check if bottoming is now complete
                 if gs.bottoming_count >= gs.cards_to_bottom:
                     # Game should now proceed to the first turn automatically
                     logging.debug("Bottoming complete.")
                     return 0.05 + reward_mod, True # Success in completing bottoming
                 else:
                     # More cards needed, stay in bottoming phase
                     return 0.02 + reward_mod, True # Incremental success
             else: # Bottoming failed (e.g., index already bottomed - GameState.bottom_card should handle)
                 logging.warning(f"Failed to bottom card at index {hand_idx_to_bottom} (Handled by gs.bottom_card).")
                 return -0.05, False # Failed action
        else: # Invalid index
            logging.error(f"Invalid BOTTOM_CARD action parameter: {hand_idx_to_bottom}. Valid indices: 0-{len(player.get('hand', []))-1}")
            return -0.1, False

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
        gs._pass_priority() # Let GameState handle the logic (priority toggle, stack/phase advance)
        # Passing priority itself is generally neutral reward; consequences come later.
        return 0.0, True # Action execution succeeded

    def _handle_concede(self, param, **kwargs):
        # Handled directly in apply_action's main logic
        return -10.0 # Large penalty


    def _handle_play_land(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        hand_idx = param

        # Check index validity
        if hand_idx >= len(player.get("hand", [])):
            logging.warning(f"PLAY_LAND: Invalid hand index {hand_idx}")
            return -0.2, False # Significant penalty for invalid index

        card_id = player["hand"][hand_idx]

        # Call GameState method to handle play logic
        # GameState.play_land handles rules checks (is land, one per turn, timing) and triggers
        if gs.play_land(card_id, player):
            return 0.2, True # Positive reward for successful land play
        else:
            logging.debug(f"PLAY_LAND: Failed (handled by gs.play_land validation). Card: {card_id}")
            return -0.1, False # Penalty for attempting invalid play



    def _handle_play_spell(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        context = kwargs.get('context', {}) # Use context passed in
        hand_idx = param

        # Check index validity
        if hand_idx >= len(player.get("hand", [])):
            logging.warning(f"PLAY_SPELL: Invalid hand index {hand_idx}")
            return -0.2, False

        card_id = player["hand"][hand_idx]
        card = gs._safe_get_card(card_id)
        if not card: return -0.2, False # Card not found

        # Add hand index to context if needed by handlers/mana system
        if 'hand_idx' not in context: context['hand_idx'] = hand_idx

        # Evaluate card value BEFORE casting (use context for more accuracy)
        card_value = 0
        if self.card_evaluator:
             eval_context = {"situation": "casting", "current_phase": gs.phase, **context}
             card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context)

        # Attempt to cast using GameState method
        # GameState.cast_spell handles timing, cost, targeting checks, and adds to stack
        if gs.cast_spell(card_id, player, context=context):
            # Reward based on successful cast + estimated card value
            return 0.1 + card_value * 0.3, True
        else:
             logging.debug(f"PLAY_SPELL: Failed (handled by gs.cast_spell validation). Card: {card_id}")
             return -0.1, False # Penalty for attempting invalid cast
    
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
        player = gs._get_active_player() # Only active player attacks
        battlefield_idx = param

        # Check index validity
        if battlefield_idx >= len(player.get("battlefield", [])):
            logging.warning(f"ATTACK: Invalid battlefield index {battlefield_idx}")
            return -0.2, False

        card_id = player["battlefield"][battlefield_idx]
        card = gs._safe_get_card(card_id)
        if not card: return -0.2, False # Card not found

        # --- Combat Handler Integration ---
        # Use CombatActionHandler validation if available
        can_attack = False
        if self.combat_handler:
            can_attack = self.combat_handler.is_valid_attacker(card_id)
        else: # Fallback basic validation (less accurate)
            if 'creature' in getattr(card, 'card_types', []):
                 tapped_set = player.get("tapped_permanents", set())
                 entered_set = player.get("entered_battlefield_this_turn", set())
                 has_haste = self._has_keyword(card, "haste") # Uses centralized check
                 if card_id not in tapped_set and not (card_id in entered_set and not has_haste):
                      can_attack = True

        # Toggle Attacker Status
        # Use game state attributes directly
        if not hasattr(gs, 'current_attackers'): gs.current_attackers = []
        if not hasattr(gs, 'planeswalker_attack_targets'): gs.planeswalker_attack_targets = {}
        if not hasattr(gs, 'battle_attack_targets'): gs.battle_attack_targets = {}

        if card_id in gs.current_attackers:
            # If already attacking, deselect
            gs.current_attackers.remove(card_id)
            # Remove any target assignment for this creature
            gs.planeswalker_attack_targets.pop(card_id, None)
            gs.battle_attack_targets.pop(card_id, None)
            logging.debug(f"ATTACK: Deselected {card.name}")
            return -0.05, True # Small penalty for deselecting
        else:
            # If not attacking, declare attack if valid
            if can_attack:
                 gs.current_attackers.append(card_id)
                 logging.debug(f"ATTACK: Declared {card.name} as attacker.")
                 # Target assignment (player/PW/Battle) happens via SEPARATE actions (e.g., ATTACK_PLANESWALKER)
                 return 0.1, True # Small reward for declaring a valid attacker
            else:
                 logging.warning(f"ATTACK: {card.name} cannot attack now.")
                 return -0.1, False # Penalty for trying invalid attack

    def _handle_block(self, param, **kwargs):
        gs = self.game_state
        blocker_player = gs._get_non_active_player() # Only non-active player blocks
        battlefield_idx = param
        context = kwargs.get('context', {})

        # Check index validity
        if battlefield_idx >= len(blocker_player.get("battlefield", [])):
            logging.warning(f"BLOCK: Invalid battlefield index {battlefield_idx}")
            return -0.2, False

        blocker_id = blocker_player["battlefield"][battlefield_idx]
        blocker_card = gs._safe_get_card(blocker_id)
        if not blocker_card or 'creature' not in getattr(blocker_card, 'card_types', []):
             logging.warning(f"BLOCK: {blocker_id} is not a creature.")
             return -0.15, False # Not a creature

        # --- Determine Target Attacker ---
        # Find which attacker this blocker is currently assigned to (if any)
        if not hasattr(gs, 'current_block_assignments'): gs.current_block_assignments = {}
        currently_blocking_attacker = None
        for atk_id, blockers_list in gs.current_block_assignments.items():
            if blocker_id in blockers_list:
                currently_blocking_attacker = atk_id
                break

        # --- Toggle Blocking Assignment ---
        if currently_blocking_attacker:
            # If already blocking, unassign
            gs.current_block_assignments[currently_blocking_attacker].remove(blocker_id)
            # Clean up dict if list becomes empty
            if not gs.current_block_assignments[currently_blocking_attacker]:
                del gs.current_block_assignments[currently_blocking_attacker]
            logging.debug(f"BLOCK: Unassigned {blocker_card.name} from blocking {gs._safe_get_card(currently_blocking_attacker).name}")
            return -0.05, True # Deselected block
        else:
            # If not blocking, try to assign to an attacker
            target_attacker_id = None
            # 1. Agent specified target via context
            if 'target_attacker_id' in context:
                target_attacker_id = context['target_attacker_id']
                # Validate the provided target attacker ID exists
                if target_attacker_id not in getattr(gs, 'current_attackers', []):
                     logging.warning(f"BLOCK: Provided target attacker {target_attacker_id} not in current attackers.")
                     target_attacker_id = None # Reset if invalid
            # 2. AI chooses target (if not specified or invalid)
            if target_attacker_id is None:
                possible_targets = [atk_id for atk_id in getattr(gs, 'current_attackers', []) if self._can_block(blocker_id, atk_id)]
                if possible_targets:
                    # Basic AI: Block highest power attacker first
                    possible_targets.sort(key=lambda atk_id: getattr(gs._safe_get_card(atk_id),'power',0), reverse=True)
                    target_attacker_id = possible_targets[0]
                    logging.debug(f"BLOCK: AI chose attacker {gs._safe_get_card(target_attacker_id).name} for {blocker_card.name}")

            # Assign block if a valid target was found/chosen
            if target_attacker_id:
                 # Final check: can this blocker block the chosen attacker?
                 if self._can_block(blocker_id, target_attacker_id):
                      if target_attacker_id not in gs.current_block_assignments:
                           gs.current_block_assignments[target_attacker_id] = []
                      # Prevent duplicates if accidentally called twice
                      if blocker_id not in gs.current_block_assignments[target_attacker_id]:
                          gs.current_block_assignments[target_attacker_id].append(blocker_id)
                          logging.debug(f"BLOCK: Assigned {blocker_card.name} to block {gs._safe_get_card(target_attacker_id).name}")
                          return 0.1, True # Successfully assigned block
                      else:
                          logging.debug(f"BLOCK: {blocker_card.name} already assigned to block {gs._safe_get_card(target_attacker_id).name}")
                          return -0.01, False # Minor penalty for redundant action
                 else:
                      logging.warning(f"BLOCK: {blocker_card.name} cannot legally block {gs._safe_get_card(target_attacker_id).name}")
                      return -0.1, False # Invalid block assignment
            else:
                 logging.warning(f"BLOCK: No valid attacker found for {blocker_card.name} to block.")
                 return -0.1, False # No valid attacker to assign block to

    def _handle_tap_land_for_mana(self, param, **kwargs):
         gs = self.game_state
         player = gs._get_active_player()
         land_idx = param

         if land_idx >= len(player.get("battlefield", [])):
             logging.warning(f"TAP_LAND_FOR_MANA: Invalid land index {land_idx}")
             return -0.2, False

         card_id = player["battlefield"][land_idx]

         # Use ManaSystem to handle tapping and mana addition
         if gs.mana_system and gs.mana_system.tap_land_for_mana(player, card_id):
             return 0.05, True # Mana is useful
         else:
             card_name = getattr(gs._safe_get_card(card_id), 'name', card_id)
             logging.warning(f"TAP_LAND_FOR_MANA: Failed (handled by gs.mana_system). Card: {card_name}")
             return -0.1, False


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
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         # Param is PW index on battlefield
         # action_index needs to be passed in kwargs by the caller in apply_action
         action_idx = kwargs.get('action_index')
         if action_idx is None:
             logging.error(f"Action Index missing for loyalty ability: {action_type}")
             return -0.2, False

         # Param is derived from action_idx based on action mapping if needed, or passed directly
         # Let's assume param is the battlefield index (derived if needed or passed)
         if param is None:
             # Recalculate param (battlefield_idx) if not passed explicitly
             # This logic depends on how the agent calls apply_action for these actions
             # Assume for now param IS the battlefield index based on ACTION_MEANINGS mapping logic
             logging.error("Loyalty ability handler called without param (battlefield_idx).")
             return -0.2, False # Needs the index

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
                      # Use activate_planeswalker_ability which now handles stack/targeting
                      if gs.activate_planeswalker_ability(card_id, ability_idx, player):
                           # Evaluate ability value *after* successful activation (cost paid, on stack/target phase)
                           ability_value, _ = self.evaluate_ability_activation(card_id, ability_idx)
                           # Reward is less immediate now, mostly for paying the cost and adding to stack
                           # More reward comes from successful resolution later.
                           return 0.05 + ability_value * 0.1, True # Small base reward + strategic value
                      else:
                           logging.debug(f"Planeswalker ability activation failed for {card.name}, Index {ability_idx}")
                           return -0.1, False # Failed activation (cost check etc.)
                 else:
                      logging.warning(f"Could not find matching loyalty ability for action {action_type} on {card.name}")
                      return -0.15, False # Ability type not found for action
             else: # Not a planeswalker at this index
                 logging.warning(f"Card at index {param} ({getattr(card, 'name', 'N/A')}) is not a planeswalker.")
                 return -0.15, False
         else: # Invalid battlefield index
            logging.warning(f"Invalid battlefield index {param} for loyalty ability.")
            return -0.2, False
     
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
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2 # Should maybe be current_player or context['player']? Assumes agent player.
        hand_idx = param
        if hand_idx < len(player.get("hand", [])):
            card_id = player["hand"][hand_idx]
            card = gs._safe_get_card(card_id)
            card_name = getattr(card, 'name', card_id)
            value = self.card_evaluator.evaluate_card(card_id, "discard") if self.card_evaluator else 0

            # Check for Madness before moving to GY
            has_madness = "madness" in getattr(card,'oracle_text','').lower() if card else False
            target_zone = "exile" if has_madness else "graveyard" # Move to exile first if madness

            # Use move_card for the discard action
            success_move = gs.move_card(card_id, player, "hand", player, target_zone, cause="discard")

            if success_move and has_madness:
                # Set up madness trigger/context
                if not hasattr(gs, 'madness_trigger'): gs.madness_trigger = None # Use None or empty dict
                # Ensure madness_trigger isn't overwritten if multiple discards happen before resolution
                # Maybe queue triggers instead? Simple override for now.
                gs.madness_trigger = {'card_id': card_id, 'player': player, 'cost': self._get_madness_cost_str(card)}
                logging.debug(f"Discarded {card_name} with Madness, moved to exile.")
            elif success_move:
                logging.debug(f"Discarded {card_name} to graveyard.")

            return -0.05 + value * 0.2 if success_move else -0.15, success_move # Reward negative because losing card
        return -0.2, False
    
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
        # Param is battlefield index of Room card
        if hasattr(gs, 'ability_handler') and gs.ability_handler.handle_unlock_door(param):
             return 0.3, True
        return -0.1, False

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

    def _handle_dredge(self, param, context=None, **kwargs):
        """Handle DREDGE action. Param is the index (0-5) into the GY options."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        gy_choice_idx = param # Agent's choice index from available dredge options

        # Ensure dredge is actually pending for this player
        if not hasattr(gs, 'dredge_pending') or not gs.dredge_pending or gs.dredge_pending['player'] != player:
             logging.warning("DREDGE action called but no dredge pending for this player.")
             return -0.1, False

        # Regenerate the list of available dredge options NOW
        dredge_options_now = [] # List of (gy_index, card_id, dredge_value)
        for idx, card_id in enumerate(player.get("graveyard", [])):
            card = gs._safe_get_card(card_id)
            if card and "dredge" in getattr(card, 'oracle_text', '').lower():
                 dredge_match = re.search(r"dredge (\d+)", card.oracle_text.lower())
                 if dredge_match:
                     dredge_value = int(dredge_match.group(1))
                     if len(player.get("library", [])) >= dredge_value:
                          dredge_options_now.append((idx, card_id, dredge_value))
        # Limit options based on action space if needed (e.g., max 6 dredge actions)
        dredge_options_now = dredge_options_now[:6]

        # Validate the chosen index from the regenerated list
        if 0 <= gy_choice_idx < len(dredge_options_now):
             gy_idx, dredge_card_id, dredge_val = dredge_options_now[gy_choice_idx]

             # Verify the chosen card ID matches the originally pending one (sanity check)
             if dredge_card_id != gs.dredge_pending['card_id']:
                  logging.warning(f"DREDGE choice index {gy_choice_idx} points to {dredge_card_id}, but pending was {gs.dredge_pending['card_id']}. State mismatch?")
                  # Proceed with the agent's choice anyway? Or fail? Fail for safety.
                  return -0.1, False

             # Perform dredge via GameState method
             if hasattr(gs, 'perform_dredge') and gs.perform_dredge(player, dredge_card_id):
                 return 0.3, True
             else:
                 # perform_dredge should clear pending state on failure
                 logging.warning(f"Dredge failed (perform_dredge returned False).")
                 return -0.05, False
        else: # Invalid index 'param' provided by agent
            logging.error(f"Invalid DREDGE action parameter: {gy_choice_idx}. Valid indices: 0-{len(dredge_options_now)-1}")
            return -0.1, False
        
    def _handle_add_counter(self, param, context, **kwargs):
        """Adds a counter to a target. Param is target index, context supplies counter info."""
        gs = self.game_state
        # Param 0-9 is target permanent index (assume combined battlefield)
        target_idx = param
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if not target_id:
            logging.warning(f"ADD_COUNTER: Invalid target index {target_idx}.")
            return -0.1, False

        # Need context for counter type and count
        if context is None or 'counter_type' not in context:
            logging.error(f"ADD_COUNTER context missing 'counter_type' for target {target_id}.")
            return -0.15, False # Invalid action call without context

        counter_type = context['counter_type']
        count = context.get('count', 1)

        # Call GameState method
        success = gs.add_counter(target_id, counter_type, count)
        if success:
            # Basic reward, could be refined by counter type/value
            reward = 0.1 * count if '+1/+1' in counter_type else 0.05 * count
            return reward, True
        return -0.05, False # add_counter failed validation

    def _handle_remove_counter(self, param, context, **kwargs):
        """Removes a counter from a target. Param is target index, context supplies counter info."""
        gs = self.game_state
        target_idx = param
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if not target_id:
             logging.warning(f"REMOVE_COUNTER: Invalid target index {target_idx}.")
             return -0.1, False

        if context is None:
            logging.error(f"REMOVE_COUNTER context missing for target {target_id}.")
            return -0.15, False

        counter_type = context.get('counter_type') # Optional: If specified
        count = context.get('count', 1)
        target_card = gs._safe_get_card(target_id)

        # Infer counter type if not provided (try to remove -1/-1 first)
        if not counter_type:
             counters_on_card = getattr(target_card, 'counters', {}) if target_card else {}
             if '-1/-1' in counters_on_card and counters_on_card['-1/-1'] >= count:
                 counter_type = '-1/-1'
             elif '+1/+1' in counters_on_card and counters_on_card['+1/+1'] >= count:
                 counter_type = '+1/+1'
             elif counters_on_card: # Take first available if others not present/sufficient
                 counter_type = list(counters_on_card.keys())[0]
                 if counters_on_card[counter_type] < count:
                     logging.warning(f"Cannot remove {count} {counter_type}, only {counters_on_card[counter_type]} exist.")
                     return -0.05, False # Cannot meet count requirement
             else: # No counters found
                 logging.warning(f"REMOVE_COUNTER: No counters found on {target_id} to remove.")
                 return -0.1, False

        # Call GameState method with negative count
        success = gs.add_counter(target_id, counter_type, -count)
        if success:
            reward = 0.15 * count if '-1/-1' in counter_type else 0.05 * count
            return reward, True
        else:
            logging.warning(f"REMOVE_COUNTER: gs.add_counter failed for {target_id}")
            return -0.05, False # add_counter failed

    def _handle_proliferate(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Proliferate needs player choice context, but the action itself just triggers the process
        if hasattr(gs, 'proliferate') and callable(gs.proliferate):
            # Context might specify targets chosen by agent, otherwise proliferate does AI choice/affects all
            chosen_targets = context.get('proliferate_targets') if context else None
            success = gs.proliferate(player, targets=chosen_targets) # Pass chosen targets if provided
            return 0.3 if success else 0.0, True # Action taken, reward based on outcome
        else:
             logging.error("Proliferate function missing in GameState.")
             return -0.1, False

    def _handle_return_from_graveyard(self, param, context=None, **kwargs):
        """Moves card from GY to Hand. Param is GY index."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        gy_idx = param

        if gy_idx < len(player.get("graveyard", [])):
            card_id = player["graveyard"][gy_idx] # Index into current GY
            card_value = self.card_evaluator.evaluate_card(card_id, "return_from_gy") if self.card_evaluator else 0.0
            # GameState.move_card handles removals and triggers
            success = gs.move_card(card_id, player, "graveyard", player, "hand", cause="return_from_gy_action")
            return 0.2 + card_value * 0.2 if success else -0.1, success
        else:
             logging.warning(f"Invalid GY index {gy_idx} for RETURN_FROM_GRAVEYARD.")
             return -0.15, False

    def _handle_reanimate(self, param, context=None, **kwargs):
        """Moves card from GY to Battlefield. Param is GY index."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        gy_idx = param

        if gy_idx < len(player.get("graveyard", [])):
            card_id = player["graveyard"][gy_idx]
            card = gs._safe_get_card(card_id)
            # Check if target is a valid permanent type to reanimate
            valid_types = ["creature", "artifact", "enchantment", "planeswalker", "land", "battle"]
            if card and any(t in getattr(card, 'card_types', []) or t in getattr(card, 'type_line','').lower() for t in valid_types):
                card_value = self.card_evaluator.evaluate_card(card_id, "reanimate") if self.card_evaluator else 0.0
                success = gs.move_card(card_id, player, "graveyard", player, "battlefield", cause="reanimate_action")
                return 0.5 + card_value * 0.3 if success else -0.1, success
            else:
                 logging.warning(f"Cannot reanimate {getattr(card, 'name', card_id)}: Invalid type.")
                 return -0.1, False
        else:
             logging.warning(f"Invalid GY index {gy_idx} for REANIMATE.")
             return -0.15, False

    def _handle_return_from_exile(self, param, context=None, **kwargs):
        """Moves card from Exile to Hand. Param is Exile index."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        exile_idx = param

        if exile_idx < len(player.get("exile", [])):
            card_id = player["exile"][exile_idx]
            card_value = self.card_evaluator.evaluate_card(card_id, "return_from_exile") if self.card_evaluator else 0.0
            success = gs.move_card(card_id, player, "exile", player, "hand", cause="return_from_exile_action")
            return 0.3 + card_value * 0.1 if success else -0.1, success
        else:
            logging.warning(f"Invalid Exile index {exile_idx} for RETURN_FROM_EXILE.")
            return -0.15, False
    
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
        """Handle COPY_PERMANENT action. Expects context={'target_identifier':X}."""
        gs = self.game_state; player = gs._get_active_player()
        target_identifier = context.get('target_identifier', context.get('target_permanent_idx')) # Allow old key fallback

        if target_identifier is not None:
            target_id, target_owner = gs.get_permanent_by_identifier(target_identifier) # Use helper for index/ID
            if target_id:
                target_card = gs._safe_get_card(target_id)
                if target_card:
                    # GS method handles token creation/copying
                    token_id = gs.create_token_copy(target_card, player)
                    return 0.4 if token_id else -0.1, token_id is not None
                else: logging.warning(f"Target card not found for copy: {target_id}")
            else: logging.warning(f"Target identifier invalid for copy: {target_identifier}")
        else: logging.warning(f"Copy Permanent context missing 'target_identifier'")
        return -0.15, False

    def _handle_copy_spell(self, param, context, **kwargs):
        """Handle COPY_SPELL action. Expects context={'target_stack_identifier':X}."""
        gs = self.game_state; player = gs._get_active_player()
        target_identifier = context.get('target_stack_identifier', context.get('target_spell_idx')) # Stack Index or unique ID

        if target_identifier is not None:
            # Find stack item by identifier (index or ID)
            target_stack_item = None
            if isinstance(target_identifier, int): # Index
                 if 0 <= target_identifier < len(gs.stack):
                      if gs.stack[target_identifier][0] == "SPELL": # Check type
                           target_stack_item = gs.stack[target_identifier]
            else: # Assume ID (less common)
                for item in gs.stack:
                     if item[0] == "SPELL" and item[1] == target_identifier:
                          target_stack_item = item; break

            if target_stack_item:
                 item_type, card_id, original_controller, old_context = target_stack_item
                 card = gs._safe_get_card(card_id)
                 if card:
                      import copy
                      new_context = copy.deepcopy(old_context)
                      new_context["is_copy"] = True
                      new_context["needs_new_targets"] = True # Always allow new targets for copy
                      new_context.pop("targets", None) # Clear old targets

                      # Add copy to stack, controlled by the player taking the copy action
                      gs.add_to_stack("SPELL", card_id, player, new_context)
                      logging.debug(f"Successfully copied spell {card.name} onto stack.")
                      return 0.4, True
                 else: logging.warning(f"Spell card {card_id} not found for copy.")
            else: logging.warning(f"Target stack item not found or not a spell: {target_identifier}")
        else: logging.warning(f"Copy Spell context missing 'target_stack_identifier'")
        return -0.15, False

    def _handle_populate(self, param, context, **kwargs):
        """Handle POPULATE action. Expects context={'target_token_identifier':X}."""
        gs = self.game_state; player = gs._get_active_player()
        target_identifier = context.get('target_token_identifier', context.get('target_token_idx')) # Identifier (index or ID)

        if target_identifier is not None:
            token_to_copy_id = self.action_handler._find_permanent_id(player, target_identifier) # Find token ID
            if token_to_copy_id:
                 original_token = gs._safe_get_card(token_to_copy_id)
                 if original_token and hasattr(original_token,'is_token') and original_token.is_token and 'creature' in getattr(original_token, 'card_types', []):
                      new_token_id = gs.create_token_copy(original_token, player)
                      return 0.35 if new_token_id else -0.1, new_token_id is not None
                 else: logging.warning(f"Target for populate {token_to_copy_id} is not a valid creature token.")
            else: logging.warning(f"Target token identifier invalid for populate: {target_identifier}")
        else: logging.warning(f"Populate context missing 'target_token_identifier'")
        return -0.15, False
        
    def _handle_investigate(self, param, context=None, **kwargs):
            """Handle the INVESTIGATE action."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2 # Usually triggered by own effect

            token_data = gs.get_token_data_by_index(4) # Index 4 is Clue token in ACTION_MEANINGS example
            if not token_data:
                # Fallback Clue data if mapping missing
                token_data = {"name": "Clue", "type_line": "Token Artifact — Clue", "card_types": ["artifact"], "subtypes": ["Clue"], "oracle_text": "{2}, Sacrifice this artifact: Draw a card."}

            success = gs.create_token(player, token_data)
            return (0.25, success) if success else (-0.05, False) # Reward creating a clue
      
    def _handle_foretell(self, param, context, **kwargs):
            gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
            hand_idx = context.get('hand_idx') # Use context

            if hand_idx is not None:
                try: hand_idx = int(hand_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Foretell context has non-integer index: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]):
                    card_id = player["hand"][hand_idx]; card = gs._safe_get_card(card_id)
                    if card and "foretell" in getattr(card, 'oracle_text', '').lower():
                        # Standard foretell cost is {2}
                        cost = {"generic": 2}
                        if gs.mana_system.can_pay_mana_cost(player, cost):
                            if gs.mana_system.pay_mana_cost(player, cost):
                                # Move card from hand to exile (face down conceptually)
                                success_move = gs.move_card(card_id, player, "hand", player, "exile", cause="foretell")
                                if success_move:
                                    if not hasattr(gs, 'foretold_cards'): gs.foretold_cards = {}
                                    # Store original info and turn foretold
                                    gs.foretold_cards[card_id] = {
                                        'turn': gs.turn,
                                        'original': card.__dict__.copy() # Store original state if needed
                                    }
                                    logging.debug(f"Foretold {card.name}")
                                    return 0.2, True
                        return -0.05, False # Can't afford
                return -0.1, False # Card cannot be foretold
            else: logging.warning(f"Foretell context missing 'hand_idx'")
            return -0.15, False
    
    def _handle_amass(self, param, context, **kwargs):
            """Handle AMASS action. Expects amount in context."""
            gs = self.game_state;
            player = gs.p1 if gs.agent_is_p1 else gs.p2 # Player who amasses
            amount = context.get('amount', 1) # Get amount from context, default 1
            if not isinstance(amount, int) or amount <= 0: amount = 1

            success = False
            if hasattr(gs, 'amass') and callable(gs.amass):
                success = gs.amass(player, amount) # GS method handles logic
            else:
                logging.error("Amass function missing in GameState.")

            # Reward scales with amount, capped
            reward = min(0.4, 0.1 * amount) if success else -0.05
            return reward, success
    
      
    def _handle_learn(self, param, context, **kwargs):
            """Handle LEARN action. Simple Draw->Discard implementation."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2 # Player learning

            # TODO: Add sideboard interaction for a full implementation

            # Option 1: Draw, then discard
            drew_card = False
            discarded_card = False
            reward = 0.0

            # Draw
            if player["library"]:
                card_drawn_id = gs._draw_card(player) # Use GS draw method
                if card_drawn_id:
                    drew_card = True
                    card_name = getattr(gs._safe_get_card(card_drawn_id), 'name', card_drawn_id)
                    logging.debug(f"Learn: Drew {card_name}")
                    reward += 0.1 # Value for drawing
                else: # Draw failed?
                    pass
            else:
                logging.warning(f"Learn: Cannot draw, library empty for {player['name']}")

            # Discard (only if a card was successfully drawn, usually)
            if drew_card and player["hand"]:
                # AI needs to choose discard. Simple AI: Discard drawn card if CMC high, else highest CMC?
                # Simpler: Discard lowest value card in hand based on evaluator.
                chosen_discard_id = None
                if self.card_evaluator:
                    lowest_value = float('inf')
                    for card_id_in_hand in player["hand"]:
                        # Context for discard eval might be important (e.g., GY synergy)
                        val = self.card_evaluator.evaluate_card(card_id_in_hand, "discard")
                        if val < lowest_value:
                            lowest_value = val
                            chosen_discard_id = card_id_in_hand
                else: # Fallback: discard last drawn card
                    chosen_discard_id = card_drawn_id if card_drawn_id in player["hand"] else (player["hand"][0] if player["hand"] else None)

                if chosen_discard_id:
                    discard_success = gs.move_card(chosen_discard_id, player, "hand", player, "graveyard", cause="learn_discard")
                    if discard_success:
                        discarded_card = True
                        card_name = getattr(gs._safe_get_card(chosen_discard_id), 'name', chosen_discard_id)
                        logging.debug(f"Learn: Discarded {card_name}")
                        reward += 0.05 # Small reward for completing discard
                    else:
                        logging.warning("Learn: Failed to move card to graveyard for discard.")
                        reward -= 0.05 # Penalty for failed move

            # Trigger Learn ability completed
            gs.trigger_ability(None, "LEARNED", {"controller": player, "drew": drew_card, "discarded": discarded_card})

            return reward, drew_card or discarded_card # Return True if either happened

    def _handle_venture(self, param, context, **kwargs):
            """Handle VENTURE action."""
            gs = self.game_state;
            player = gs.p1 if gs.agent_is_p1 else gs.p2

            if hasattr(gs, 'venture') and callable(gs.venture):
                success = gs.venture(player) # GS handles dungeon logic
                # Reward might depend on room entered? Simple reward for now.
                return 0.15 if success else -0.05, success
            else:
                logging.warning("Venture called but GameState.venture method not implemented.")
                return -0.1, False # Cannot perform action

      
    def _handle_exert(self, param, context, **kwargs):
            gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
            creature_idx = context.get('creature_idx') # Battlefield index

            if creature_idx is not None:
                try: creature_idx = int(creature_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Exert context has non-integer index: {context}")
                    return (-0.15, False)

                if creature_idx < len(player["battlefield"]):
                    card_id = player["battlefield"][creature_idx]
                    # Exert typically happens *as* it attacks
                    # Check if creature is currently being declared as attacker
                    if card_id in gs.current_attackers:
                        if not hasattr(gs, 'exerted_this_combat'): gs.exerted_this_combat = set()
                        if card_id not in gs.exerted_this_combat:
                            gs.exerted_this_combat.add(card_id)
                            # Exert effect usually triggers automatically or provides static bonus
                            card = gs._safe_get_card(card_id)
                            logging.debug(f"Exerted {card.name}")
                            gs.trigger_ability(card_id, "EXERTED", {"controller": player}) # Trigger "when exerted" abilities
                            return 0.2, True
                        else: # Already exerted this combat
                            return -0.05, False
                    else: # Cannot exert if not attacking
                        return -0.1, False
                else: logging.warning(f"Exert index out of bounds: {creature_idx}")
            else: logging.warning(f"Exert context missing 'creature_idx'")
            return -0.15, False
      
    def _handle_explore(self, param, context, **kwargs):
            """Handle EXPLORE action. Expects creature_idx in context."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            creature_idx = context.get('creature_idx') # Battlefield index

            if creature_idx is not None:
                try: creature_idx = int(creature_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Explore context has non-integer index: {context}")
                    return (-0.15, False)

                if creature_idx < len(player["battlefield"]):
                    card_id = player["battlefield"][creature_idx]
                    if hasattr(gs, 'explore') and callable(gs.explore):
                        success = gs.explore(player, card_id) # GS handles logic
                        return 0.25 if success else -0.05, success
                    else:
                        logging.error("Explore function missing in GameState.")
                        return -0.1, False
                else: logging.warning(f"Explore index out of bounds: {creature_idx}")
            else: logging.warning(f"Explore context missing 'creature_idx'")
            return -0.15, False

    def _handle_adapt(self, param, context, **kwargs):
            gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
            creature_idx = context.get('creature_idx') # Creature index
            amount = context.get('amount', 1) # Get amount, default 1

            if creature_idx is not None:
                try: creature_idx, amount = int(creature_idx), int(amount)
                except (ValueError, TypeError):
                    logging.warning(f"Adapt context has non-integer index/amount: {context}")
                    return (-0.15, False)

                if creature_idx < len(player["battlefield"]):
                    card_id = player["battlefield"][creature_idx]
                    # Adapt needs cost payment usually, handled by activating the ability
                    # This action might just trigger the check, assuming cost was paid via ability.
                    success = gs.adapt(player, card_id, amount) # GS handles logic
                    return 0.1 * amount if success else -0.05, success
                else: logging.warning(f"Adapt index out of bounds: {creature_idx}")
            else: logging.warning(f"Adapt context missing 'creature_idx'")
            return -0.15, False

      

    def _handle_mutate(self, param, context, **kwargs):
            """Handle MUTATE action. Expects context={'hand_idx':X, 'target_idx':Y}."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            hand_idx = context.get('hand_idx')
            target_idx = context.get('target_idx') # Target battlefield index

            if hand_idx is not None and target_idx is not None:
                try: hand_idx, target_idx = int(hand_idx), int(target_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Mutate context has non-integer indices: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]) and target_idx < len(player["battlefield"]):
                    mutating_card_id = player["hand"][hand_idx]
                    target_id = player["battlefield"][target_idx]
                    mutating_card = gs._safe_get_card(mutating_card_id)
                    target_card = gs._safe_get_card(target_id)

                    if not mutating_card or not target_card: return -0.15, False

                    # --- Check Mutate Cost & Affordability ---
                    # Assumes mutate cost is same as mana cost? Rules check needed. Often different.
                    # Look for explicit mutate cost. Default to mana cost if not found.
                    mutate_cost_str = None
                    match = re.search(r"mutate (\{[^\}]+\})", getattr(mutating_card, 'oracle_text','').lower())
                    if match: mutate_cost_str = match.group(1)
                    else: mutate_cost_str = getattr(mutating_card, 'mana_cost', '')

                    if not self._can_afford_cost_string(player, mutate_cost_str):
                         logging.debug(f"Cannot afford mutate cost {mutate_cost_str} for {mutating_card_id}")
                         return -0.05, False

                    # --- Perform Mutate via GameState ---
                    # Cost payment happens *here* before calling gs.mutate
                    if not gs.mana_system.pay_mana_cost(player, mutate_cost_str):
                        logging.warning(f"Failed to pay mutate cost {mutate_cost_str}")
                        return -0.05, False

                    # GameState.mutate handles validation (non-human etc.) and merge
                    if hasattr(gs, 'mutate') and gs.mutate(player, mutating_card_id, target_id):
                        # Card moves from hand implicitly within mutate logic if successful?
                        # Let's assume gs.mutate handles the card movement from hand.
                        return 0.6, True
                    else:
                        logging.debug(f"Mutate validation/execution failed for {mutating_card_id} -> {target_id}")
                        # Refund mana? Assume cost wasted.
                        return -0.1, False
                else: logging.warning(f"Mutate indices out of bounds H:{hand_idx}, T:{target_idx}")
            else: logging.warning(f"Mutate context missing indices: {context}")
            return -0.15, False

        
    def _handle_goad(self, param, context, **kwargs):
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            target_idx = context.get('target_creature_idx') # Combined index

            if target_idx is not None:
                try: target_idx = int(target_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Goad context has non-integer index: {context}")
                    return (-0.15, False)

                target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
                opponent = gs.p2 if player == gs.p1 else gs.p1
                if target_id and target_owner == opponent:
                    success = gs.goad_creature(target_id) # GS method handles marking
                    return 0.25 if success else -0.05, success
            else: logging.warning(f"Goad context missing 'target_creature_idx'")
            return -0.15, False
      
    def _handle_boast(self, param, context, **kwargs):
            gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
            creature_idx = context.get('creature_idx') # BF index

            if creature_idx is not None:
                try: creature_idx = int(creature_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Boast context has non-integer index: {context}")
                    return (-0.15, False)

                if creature_idx < len(player["battlefield"]):
                    card_id = player["battlefield"][creature_idx]
                    # Boast condition check (attacked this turn and not already boasted)
                    # Moved actual activation logic here from the ability handler check.
                    if hasattr(gs, 'attackers_this_turn') and card_id in gs.attackers_this_turn \
                    and card_id not in getattr(gs, 'boast_activated', set()):

                        # Find boast ability (needs better lookup than index 1)
                        ability_idx_to_activate = -1
                        if hasattr(gs, 'ability_handler'):
                            abilities = gs.ability_handler.get_activated_abilities(card_id)
                            for idx, ab in enumerate(abilities):
                                if "boast" in getattr(ab, 'effect_text', '').lower():
                                    ability_idx_to_activate = idx
                                    break

                        if ability_idx_to_activate != -1:
                            if gs.ability_handler.can_activate_ability(card_id, ability_idx_to_activate, player):
                                success = gs.ability_handler.activate_ability(card_id, ability_idx_to_activate, player)
                                if success:
                                    if not hasattr(gs, 'boast_activated'): gs.boast_activated = set()
                                    gs.boast_activated.add(card_id)
                                    return 0.3, True
                                return -0.1, False # Activation failed
                            return -0.1, False # Cannot activate
                        else: # No boast ability found
                            return -0.1, False
                    else: # Condition not met (didn't attack or already boasted)
                        return -0.1, False
                else: logging.warning(f"Boast index out of bounds: {creature_idx}")
            else: logging.warning(f"Boast context missing 'creature_idx'")
            return -0.15, False
      
    def _handle_counter_spell(self, param, context, **kwargs):
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # Context needs: {'hand_idx': X, 'target_spell_idx': Y}
            hand_idx = context.get('hand_idx')
            target_stack_idx = context.get('target_spell_idx')

            if hand_idx is not None and target_stack_idx is not None:
                try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Counter Spell context has non-integer indices: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]):
                    card_id = player["hand"][hand_idx]
                    # Cast the counter spell targeting the specified stack index
                    if gs.cast_spell(card_id, player, context={"target_stack_index": target_stack_idx}):
                        return 0.6, True # Successful cast is good
                    else: return -0.1, False
            else: logging.warning(f"Counter Spell context missing indices: {context}")
            return -0.15, False
    
    def _handle_prevent_damage(self, param, context, **kwargs):
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # Context needs: {'hand_idx': X, 'amount': Y, 'target': Z}
            hand_idx = context.get('hand_idx')

            if hand_idx is not None:
                try: hand_idx = int(hand_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Prevent Damage context has non-integer index: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]):
                    card_id = player["hand"][hand_idx]
                    # Cast the prevention spell
                    # The GS/Ability system needs to resolve the spell and create the replacement effect
                    if gs.cast_spell(card_id, player, context=context):
                        # The effect is registered when the spell resolves
                        return 0.2, True
                    else: return -0.1, False
            else: logging.warning(f"Prevent Damage context missing 'hand_idx'")
            return -0.15, False

      
    def _handle_redirect_damage(self, param, context, **kwargs):
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # Context needs: {'hand_idx': X, 'new_target': Y}
            hand_idx = context.get('hand_idx')

            if hand_idx is not None:
                try: hand_idx = int(hand_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Redirect Damage context has non-integer index: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]):
                    card_id = player["hand"][hand_idx]
                    # Cast the redirection spell
                    if gs.cast_spell(card_id, player, context=context):
                        # Effect registered on resolution
                        return 0.3, True
                    else: return -0.1, False
            else: logging.warning(f"Redirect Damage context missing 'hand_idx'")
            return -0.15, False

      
    def _handle_stifle_trigger(self, param, context, **kwargs):
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # Context needs: {'hand_idx': X, 'target_trigger_idx': Y}
            hand_idx = context.get('hand_idx')
            target_stack_idx = context.get('target_trigger_idx')

            if hand_idx is not None and target_stack_idx is not None:
                try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Stifle context has non-integer indices: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]):
                    card_id = player["hand"][hand_idx]
                    # Cast the stifle spell
                    if gs.cast_spell(card_id, player, context={"target_stack_index": target_stack_idx}):
                        return 0.5, True
                    else: return -0.1, False
            else: logging.warning(f"Stifle context missing indices: {context}")
            return -0.15, False
        
    def _handle_counter_ability(self, param, context, **kwargs):
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # Context needs: {'hand_idx': X, 'target_ability_idx': Y}
            hand_idx = context.get('hand_idx')
            target_stack_idx = context.get('target_ability_idx')

            if hand_idx is not None and target_stack_idx is not None:
                try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Counter Ability context has non-integer indices: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]):
                    card_id = player["hand"][hand_idx]
                    # Cast the counter ability spell
                    if gs.cast_spell(card_id, player, context={"target_stack_index": target_stack_idx}):
                        return 0.5, True
                    else: return -0.1, False
            else: logging.warning(f"Counter Ability context missing indices: {context}")
            return -0.15, False
    
    def _handle_flip_card(self, param, context, **kwargs):
            """Handle flipping a flip card. Expects battlefield_idx in context."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            target_idx = context.get('battlefield_idx') # Get from context

            if target_idx is not None:
                try: target_idx = int(target_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Flip Card context has non-integer index: {context}")
                    return (-0.15, False)

                if target_idx < len(player["battlefield"]):
                    card_id = player["battlefield"][target_idx]
                    success = gs.flip_card(card_id) # GS method handles logic
                    return (0.2, success) if success else (-0.1, False)
                else: logging.warning(f"Flip Card index out of bounds: {target_idx}")
            else: logging.warning(f"Flip Card context missing 'battlefield_idx'")
            return (-0.15, False)
    

    def _handle_equip(self, param, context, **kwargs): # Param is None
        """Handle EQUIP action. Expects context={'equipment_identifier':X, 'target_identifier':Y}."""
        gs = self.game_state
        player = gs._get_active_player() # Equipping happens on your turn
        if context is None: context = {}

        # --- Get Identifiers from CONTEXT ---
        equip_identifier = context.get('equipment_identifier')
        target_identifier = context.get('target_identifier')

        if equip_identifier is None or target_identifier is None:
            logging.error(f"Equip context missing required identifiers: {context}")
            return -0.15, False
        # --- End Context Check ---

        equip_id = self._find_permanent_id(player, equip_identifier)
        target_id = self._find_permanent_id(player, target_identifier) # Target creature

        if not equip_id or not target_id:
             logging.warning(f"Equip failed: Invalid identifiers. Equip: '{equip_identifier}' -> {equip_id}, Target: '{target_identifier}' -> {target_id}")
             return -0.15, False

        # --- Check Equip Cost & Affordability ---
        equip_card = gs._safe_get_card(equip_id)
        equip_cost_str = self._get_equip_cost_str(equip_card) # Use internal helper

        if not equip_cost_str or not self._can_afford_cost_string(player, equip_cost_str):
            logging.debug(f"Cannot afford equip cost {equip_cost_str or 'N/A'} for {equip_id}")
            return -0.05, False

        # --- Perform Equip via GameState ---
        # Pay cost first
        if not hasattr(gs, 'mana_system') or not gs.mana_system or not gs.mana_system.pay_mana_cost(player, equip_cost_str):
            logging.warning(f"Failed to pay equip cost {equip_cost_str}")
            return -0.05, False

        # Attempt attachment
        if hasattr(gs, 'equip_permanent') and gs.equip_permanent(player, equip_id, target_id):
            return 0.25, True
        else:
            logging.debug(f"Equip action failed validation or execution for {equip_id} -> {target_id}")
            # Rollback mana? Assume cost wasted for now.
            return -0.1, False
        
    def _handle_unequip(self, param, context, **kwargs):
            """Handle unequip action. Expects equip_idx in context."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # context passed from apply_action
            equip_idx = context.get('equip_idx') # Get from context

            if equip_idx is not None:
                try: equip_idx = int(equip_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Unequip context has non-integer index: {context}")
                    return (-0.15, False)

                if equip_idx < len(player["battlefield"]):
                    equip_id = player["battlefield"][equip_idx]
                    success = gs.unequip_permanent(player, equip_id) # GameState method handles logic
                    return (0.1, success) if success else (-0.1, False)
                else: logging.warning(f"Unequip index out of bounds: {equip_idx}")
            else: logging.warning(f"Unequip context missing 'equip_idx'")
            return (-0.15, False)


    def _handle_attach_aura(self, param, context, **kwargs):
            """Handle attaching aura via an ability/effect. Expects context={'aura_id':X, 'target_id':Y}."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            aura_id = context.get('aura_id')
            target_id = context.get('target_id')

            if aura_id and target_id:
                # Find controller of the aura effect (might not be the owner of the aura card)
                # Assume player activating the ability is the one performing the attach
                controller_of_effect = player

                # GS method handles validation and attachment
                if hasattr(gs, 'attach_aura') and gs.attach_aura(controller_of_effect, aura_id, target_id):
                    return 0.25, True
                else:
                    logging.warning(f"Failed to attach aura {aura_id} to {target_id}")
                    return -0.1, False
            else:
                logging.warning(f"ATTACH_AURA context missing aura_id or target_id: {context}")
                return -0.15, False

    def _handle_fortify(self, param, context, **kwargs): # Param is None
        """Handle FORTIFY action. Expects context={'fort_identifier':X, 'target_identifier':Y}."""
        gs = self.game_state
        player = gs._get_active_player()
        if context is None: context = {}

        # --- Get Identifiers from CONTEXT ---
        fort_identifier = context.get('fort_identifier')
        target_identifier = context.get('target_identifier') # Target Land identifier

        if fort_identifier is None or target_identifier is None:
            logging.error(f"Fortify context missing required identifiers: {context}")
            return -0.15, False
        # --- End Context Check ---

        fort_id = self._find_permanent_id(player, fort_identifier)
        target_id = self._find_permanent_id(player, target_identifier) # Target land

        if not fort_id or not target_id:
             logging.warning(f"Fortify failed: Invalid identifiers. Fort: '{fort_identifier}' -> {fort_id}, Target: '{target_identifier}' -> {target_id}")
             return -0.15, False

        # --- Check Cost & Affordability ---
        fort_card = gs._safe_get_card(fort_id)
        fort_cost_str = self._get_fortify_cost_str(fort_card) # Use internal helper

        if not fort_cost_str or not self._can_afford_cost_string(player, fort_cost_str):
            logging.debug(f"Cannot afford fortify cost {fort_cost_str or 'N/A'} for {fort_id}")
            return -0.05, False

        # --- Perform Fortify via GameState ---
        # Pay cost
        if not hasattr(gs, 'mana_system') or not gs.mana_system or not gs.mana_system.pay_mana_cost(player, fort_cost_str):
            logging.warning(f"Failed to pay fortify cost {fort_cost_str}")
            return -0.05, False

        # Attach
        if hasattr(gs, 'fortify_land') and gs.fortify_land(player, fort_id, target_id):
            return 0.2, True
        else:
            logging.debug(f"Fortify action failed validation or execution for {fort_id} -> {target_id}")
            # Rollback cost?
            return -0.1, False
     
    def _handle_reconfigure(self, param, context, **kwargs):
        """Handle RECONFIGURE action. Expects context={'card_identifier':X}. Might need target context if attaching."""
        gs = self.game_state
        player = gs._get_active_player()
        card_identifier = context.get('card_identifier', context.get('battlefield_idx')) # Fallback

        if card_identifier is None:
             logging.error(f"Reconfigure context missing 'card_identifier': {context}")
             return -0.15, False

        card_id = self.action_handler._find_permanent_id(player, card_identifier)
        if not card_id:
             logging.warning(f"Reconfigure failed: Invalid identifier '{card_identifier}' -> {card_id}")
             return -0.15, False

        # --- Check Cost & Affordability ---
        card = gs._safe_get_card(card_id)
        reconf_cost_str = self.action_handler._get_reconfigure_cost_str(card) if hasattr(self.action_handler, '_get_reconfigure_cost_str') else None
        if reconf_cost_str is None and hasattr(self.combat_handler, '_get_reconfigure_cost_str'):
            reconf_cost_str = self.combat_handler._get_reconfigure_cost_str(card)

        if not reconf_cost_str or not self.action_handler._can_afford_cost_string(player, reconf_cost_str):
            logging.debug(f"Cannot afford reconfigure cost {reconf_cost_str} for {card_id}")
            return (-0.05, False)

        # --- Determine target if attaching ---
        target_id = None
        is_attached = card_id in player.get("attachments", {})
        if not is_attached: # Trying to attach
             # Target should come from context if agent made a choice
             target_identifier_ctx = context.get('target_identifier')
             if target_identifier_ctx:
                  target_id = self.action_handler._find_permanent_id(player, target_identifier_ctx)
                  if not target_id: logging.warning(f"Reconfigure attach target invalid: {target_identifier_ctx}")
             else:
                  # AI fallback: choose first valid creature? Or expect context? Expect context for now.
                  logging.error("Reconfigure attach requires target identifier in context.")
                  return -0.1, False # Require explicit target choice

        # --- Perform Reconfigure via GameState ---
        if not hasattr(gs, 'mana_system') or not gs.mana_system.pay_mana_cost(player, reconf_cost_str):
            logging.warning(f"Failed to pay reconfigure cost {reconf_cost_str}")
            return (-0.05, False)

        if hasattr(gs, 'reconfigure_permanent') and gs.reconfigure_permanent(player, card_id, target_id=target_id):
            return (0.2, True)
        else:
            logging.debug(f"Reconfigure failed for {card_id}")
            return (-0.1, False)


    def _handle_morph(self, param, context, **kwargs):
            """Handle turning a morph face up. Expects battlefield_idx in context."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # context passed from apply_action
            card_idx = context.get('battlefield_idx') # Get from context

            if card_idx is not None:
                try: card_idx = int(card_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Morph context has non-integer index: {context}")
                    return (-0.15, False)

                if card_idx < len(player["battlefield"]):
                    card_id = player["battlefield"][card_idx]
                    # GS method checks if it's morph, face down, and pays cost
                    success = gs.turn_face_up(player, card_id, pay_morph_cost=True)
                    return (0.3, success) if success else (-0.1, False)
                else: logging.warning(f"Morph index out of bounds: {card_idx}")
            else: logging.warning(f"Morph context missing 'battlefield_idx'")
            return (-0.15, False)
    

    def _handle_clash(self, param, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        opponent = gs.p2 if player == gs.p1 else gs.p1
        winner = gs.clash(player, opponent)
        return (0.1, True) if winner == player else (0.0, True)
      

    def _handle_conspire(self, param, context, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # --- ADDED Context Checks ---
        spell_stack_idx = context.get('spell_stack_idx')
        c1_identifier = context.get('creature1_identifier') # Can be index or ID
        c2_identifier = context.get('creature2_identifier') # Can be index or ID

        if spell_stack_idx is None or c1_identifier is None or c2_identifier is None:
             logging.error(f"Conspire context missing required indices: {context}")
             return -0.15, False
        # --- END Checks ---

        if spell_stack_idx is not None and c1_identifier is not None and c2_identifier is not None:
            try: spell_stack_idx = int(spell_stack_idx)
            except (ValueError, TypeError): return -0.15, False

            # Call GameState conspire method
            if hasattr(gs, 'conspire') and gs.conspire(player, spell_stack_idx, c1_identifier, c2_identifier):
                return 0.4, True
            else:
                logging.debug("Conspire action failed validation or execution.")
                return -0.1, False
        # else: error logged above
        return -0.15, False
      
    def _handle_grandeur(self, param, context, **kwargs):
            gs = self.game_state; player = gs.p1 if gs.agent_is_p1 else gs.p2
            # context passed from apply_action
            hand_idx = context.get('hand_idx') # Get from context

            if hand_idx is not None:
                try: hand_idx = int(hand_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Grandeur context has non-integer index: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]):
                    card_id_to_discard = player["hand"][hand_idx]
                    discard_card = gs._safe_get_card(card_id_to_discard)
                    if not discard_card: return -0.1, False
                    # Find grandeur card on battlefield
                    grandeur_id_on_bf = None
                    for bf_idx, bf_id in enumerate(player["battlefield"]): # Iterate with index
                        bf_card = gs._safe_get_card(bf_id)
                        if bf_card and bf_card.name == discard_card.name and "grandeur" in getattr(bf_card,'oracle_text','').lower():
                            grandeur_id_on_bf = bf_id
                            break
                    if grandeur_id_on_bf:
                        success_discard = gs.move_card(card_id_to_discard, player, "hand", player, "graveyard", cause="grandeur_cost")
                        if success_discard:
                            # Activate grandeur ability (assume index 0?)
                            # The ability activation should now happen via a separate ACTIVATE_ABILITY action
                            # This action just pays the cost. Need to adjust workflow or handler.
                            # Option 1: Grandeur action *both* discards and activates.
                            # Option 2: Grandeur action just discards, agent needs separate ACTIVATE action.
                            # Let's assume Option 1 for now.
                            success_ability = False
                            if hasattr(gs, 'ability_handler'):
                                success_ability = gs.ability_handler.activate_ability(grandeur_id_on_bf, 0, player) # Assumes ability 0

                            return (0.35, True) if success_ability else (0.0, True) # Reward successful activation more
                        return -0.05, False # Discard failed
                    else: logging.warning(f"No card named {discard_card.name} with Grandeur found on battlefield.")
                else: logging.warning(f"Grandeur hand index out of bounds: {hand_idx}")
            else: logging.warning(f"Grandeur context missing 'hand_idx'")
            return -0.15, False
    
    def _handle_select_spree_mode(self, param, context, **kwargs): # Param is None
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # --- Get Indices from CONTEXT ---
        hand_idx = context.get('hand_idx')
        mode_idx = context.get('mode_idx')

        if hand_idx is None or mode_idx is None:
            logging.error(f"SELECT_SPREE_MODE missing 'hand_idx' or 'mode_idx' in context: {context}")
            return -0.15, False
        if not isinstance(hand_idx, int) or not isinstance(mode_idx, int):
            logging.error(f"SELECT_SPREE_MODE context indices are not integers: {context}")
            return -0.15, False
        # --- End Context Check ---

        if hand_idx < len(player["hand"]):
            card_id = player["hand"][hand_idx]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_spree') and card.is_spree and mode_idx < len(getattr(card,'spree_modes',[])):
                # --- Logic for managing pending spree context ---
                if not hasattr(gs, 'pending_spell_context'): gs.pending_spell_context = {}
                if gs.pending_spell_context.get('card_id') != card_id:
                    gs.pending_spell_context = {
                        'card_id': card_id, 'hand_idx': hand_idx, # Store index
                        'selected_spree_modes': set(), 'spree_costs': {}, 'source_zone': 'hand'
                    }

                selected_modes = gs.pending_spell_context.get('selected_spree_modes', set())
                mode_cost_str = card.spree_modes[mode_idx].get('cost', '')
                if self._can_afford_cost_string(player, mode_cost_str, context=context):
                    if mode_idx not in selected_modes:
                        selected_modes.add(mode_idx)
                        gs.pending_spell_context['selected_spree_modes'] = selected_modes
                        gs.pending_spell_context['spree_costs'][mode_idx] = mode_cost_str
                        logging.debug(f"Added Spree mode {mode_idx} (Cost: {mode_cost_str}) to pending cast for {card.name}")
                        # Stay in priority to allow selecting more modes or casting
                        gs.phase = gs.PHASE_PRIORITY
                        return 0.05, True
                    else: # Mode already selected
                        logging.warning(f"Spree mode {mode_idx} already selected for {card.name}")
                        return -0.05, False
                else: # Cannot afford mode cost
                    logging.warning(f"Cannot afford Spree mode {mode_idx} cost {mode_cost_str} for {card.name}")
                    return -0.05, False
            else: # Invalid card or mode index
                logging.warning(f"Invalid card or mode index for Spree: Hand:{hand_idx}, Mode:{mode_idx}")
                return -0.1, False
        else: # Invalid hand index
             logging.warning(f"Invalid hand index {hand_idx} for SELECT_SPREE_MODE.")
             return -0.2, False
    
    def _handle_create_token(self, param, action_type=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # Param 0-4 is token type index
        token_data = gs.get_token_data_by_index(param)
        if token_data:
             success = gs.create_token(player, token_data)
             return 0.15 if success else -0.1, success
        return -0.15, False

      
    def _handle_cycling(self, param, context, **kwargs):
            """Handle CYCLING action. Expects context={'hand_idx':X}."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            hand_idx = context.get('hand_idx') # Use context

            if hand_idx is not None:
                try: hand_idx = int(hand_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Cycling context has non-integer index: {context}")
                    return (-0.15, False)

                if hand_idx < len(player["hand"]):
                    card_id = player["hand"][hand_idx]
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'oracle_text') and "cycling" in card.oracle_text.lower():
                        cost_match = re.search(r"cycling (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                        if cost_match:
                            cost_str = cost_match.group(1)
                            if cost_str.isdigit(): cost_str = f"{{{cost_str}}}"
                            # --- Pay Cycling Cost ---
                            if gs.mana_system.can_pay_mana_cost(player, cost_str):
                                if gs.mana_system.pay_mana_cost(player, cost_str):
                                    # --- Discard and Draw ---
                                    # Use move_card to discard (handles triggers)
                                    success_discard = gs.move_card(card_id, player, "hand", player, "graveyard", cause="cycling_discard")
                                    if success_discard:
                                        # Draw a card (handles triggers)
                                        gs._draw_phase(player)
                                        # Trigger cycling abilities
                                        gs.trigger_ability(card_id, "CYCLING", {"controller": player})
                                        return 0.1, True # Positive reward for cycling
                                    else:
                                        # Rollback cost? Complex. Assume cost paid.
                                        return -0.05, False # Discard failed
                                return -0.05, False # Cost payment failed
                            return -0.05, False # Cannot afford
                return -0.1, False # No cycling ability or invalid card
            else: logging.warning(f"Cycling context missing 'hand_idx'")
            return -0.15, False
        
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
    
    def _handle_no_op_search_fail(self, param, **kwargs): return self._handle_no_op(param, **kwargs)
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
        # Ensure ability_features is always present
        if "ability_features" in self.observation_space.spaces:
            space = self.observation_space.spaces["ability_features"]
            obs["ability_features"] = np.zeros(space.shape, dtype=space.dtype)
        return obs