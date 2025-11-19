#actions.py

import logging
import re
import numpy as np
import random
from collections import defaultdict
from .card import Card
from .enhanced_combat import ExtendedCombatResolver
from .combat_integration import integrate_combat_actions, apply_combat_action
from .debug import DEBUG_MODE, debug_log_valid_actions 
from .enhanced_card_evaluator import EnhancedCardEvaluator
from .combat_actions import CombatActionHandler

# ACTION_MEANINGS dictionary - Corrected and verified for size 480 (Indices 0-479)
# Added Context Required comments
ACTION_MEANINGS = {
    # Basic game flow (0-12) = 13 actions
    0: ("END_TURN", None), 1: ("UNTAP_NEXT", None), 2: ("DRAW_NEXT", None), 3: ("MAIN_PHASE_END", None),
    4: ("NO_OP", None), # COMBAT_DAMAGE (4) is now NO_OP, handled by 431
    5: ("END_PHASE", None), # Should be deprecated in favor of END_STEP (10) or pass priority. Keep as NO_OP? Let's keep definition but handler might ignore.
    6: ("MULLIGAN", None), 7: ("UPKEEP_PASS", None),
    8: ("BEGIN_COMBAT_END", None), 9: ("END_COMBAT", None), 10: ("END_STEP", None), # END_STEP means pass prio during end step
    11: ("PASS_PRIORITY", None),
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
    # ability_idx here is 0, 1, or 2 (relative index of activatable abilities on that permanent)
    **{100 + (i * 3) + j: ("ACTIVATE_ABILITY", None) for i in range(20) for j in range(3)},

    # Transform (160-179) = 20 actions (param=battlefield index 0-19)
    **{160 + i: ("TRANSFORM", i) for i in range(20)},

    # MDFC Land Back (180-187) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_MDFC_LAND_BACK", i-180) for i in range(180, 188)},

    # MDFC Spell Back (188-195) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_MDFC_BACK", i-188) for i in range(188, 196)},

    # Adventure (196-203) = 8 actions (param=hand index 0-7)
    **{i: ("PLAY_ADVENTURE", i-196) for i in range(196, 204)},

    # Defend Battle (204-223) = 20 actions - RETHINK MAPPING LATER IF NEEDED
    # Current map assumes Battle 0-4, Def 0-3. Simple approach: Just use 204.
    # Param=None. Handler expects {'battle_identifier': id_or_idx, 'defender_identifier': id_or_idx} in context.
    204: ("DEFEND_BATTLE", None), # Simplified: one action, needs context
    
    205: ("DISTURB_CAST", None),         # Cast from graveyard (Spirit/enchantment side)
    206: ("DASH_CAST", None),            # Cast with haste and return to hand EOT
    207: ("SPECTACLE_CAST", None),       # Alternative cost if opponent lost life
    208: ("BESTOW_CAST", None),          # Cast as Aura or creature
    209: ("BLITZ_CAST", None),           # Cast with haste, sacrifice & draw
    210: ("ETERNALIZE", None),           # Bring back as 4/4 black zombie
    211: ("EMBALM", None),               # Create token copy from graveyard
    212: ("REINFORCE", None),            # Discard to put counters
    213: ("CHANNEL", None),              # Discard for alternative effect
    214: ("TRANSMUTE", None),            # Discard to tutor same CMC
    215: ("FORECAST", None),             # Activate from hand during upkeep
    216: ("SUSPEND_CAST", None),         # Exile with time counters to cast later
    217: ("UNEARTH", None),              # Temporarily reanimate
    218: ("ENCORE", None),               # Create token attacks
    219: ("PARTNER_WITH", None),         # Tutor specific card
    220: ("COMPANION_CHECK", None),      # Check companion requirements
    221: ("EVOKE_CAST", None),           # Cast for less with sacrifice
    222: ("MIRACLE_CAST", None),         # Cast at reduced cost when drawn
    223: ("FORETELL_CAST", None),        # Cast from exile after foretelling

    # NO_OP (224)
    224: ("NO_OP", None),

    # Mulligan (225-229) = 5 actions
    225: ("KEEP_HAND", None),
    # Param is hand index (0-3) for card selection to bottom.
    **{226 + i: ("BOTTOM_CARD", i) for i in range(4)},

    # Cast from Exile (230-237) = 8 actions (param=relative index 0-7 into castable exile list)
    **{i: ("CAST_FROM_EXILE", i-230) for i in range(230, 238)},

    # Discard (238-247) = 10 actions (param=hand index 0-9) - Handled by DISCARD_CARD choice
    **{238 + i: ("DISCARD_CARD", i) for i in range(10)},

    # Room/Class (248-257) = 10 actions
    # Param=battlefield index 0-4
    **{248 + i: ("UNLOCK_DOOR", i) for i in range(5)},
    # Param=battlefield index 0-4
    **{253 + i: ("LEVEL_UP_CLASS", i) for i in range(5)},

    # Spree Mode (258-273) = 16 actions
    # Param=None. Handler expects {'hand_idx': int, 'mode_idx': int} in context.
    # Maps Hand Index 0-7, Mode Index 0-1 -> Action Index 258-273
    **{258 + (i * 2) + j: ("SELECT_SPREE_MODE", None) for i in range(8) for j in range(2)},

    # Targeting / Sacrifice Choices (274-293) = 20 actions
    # Param = Choice Index (0-9) relative to available valid options for the current context
    **{274 + i: ("SELECT_TARGET", i) for i in range(10)},
    **{284 + i: ("SACRIFICE_PERMANENT", i) for i in range(10)},

    # --- Offspring/Impending custom actions (indices 294, 295) ---
    294: ("CAST_FOR_IMPENDING", None),  # Context={'hand_idx': X}
    295: ("PAY_OFFSPRING_COST", None),  # Context implicit via gs.pending_spell_context

    # Gaps filled with NO_OP (296-298) = 3 actions
    **{i: ("NO_OP", None) for i in range(296, 299)},

    # Library/Card Movement (299-308) = 10 actions
    # Param = Choice index 0-4 for specific library search
    **{299 + i: ("SEARCH_LIBRARY", i) for i in range(5)}, # 299-303
    304: ("NO_OP_SEARCH_FAIL", None), # Unused action? Handler returns success even on fail. NO_OP.
    305: ("PUT_TO_GRAVEYARD", None), # Surveil Choice - Contextual
    306: ("PUT_ON_TOP", None), # Scry/Surveil Choice - Contextual
    307: ("PUT_ON_BOTTOM", None), # Scry Choice - Contextual
    308: ("DREDGE", None), # Param = GY index 0-5? Or Contextual? Use context.

    # Gaps filled with NO_OP (309-313) = 5 actions
    **{i: ("NO_OP", None) for i in range(309, 314)}, # 309-313

    # Counter Management (314-334) = 21 actions
    # Param = Target Index 0-9 (relative to valid targets?) Context needed: {'counter_type': X}
    **{314 + i: ("ADD_COUNTER", i) for i in range(10)},         # 314-323
    **{324 + i: ("REMOVE_COUNTER", i) for i in range(10)},      # 324-333
    334: ("PROLIFERATE", None), # Param = None, Context maybe for target selection? Handler implements simple.

    # Zone Movement (335-352) = 18 actions
    # Param = Zone Index 0-5
    **{335 + i: ("RETURN_FROM_GRAVEYARD", i) for i in range(6)},# 335-340
    **{341 + i: ("REANIMATE", i) for i in range(6)},            # 341-346
    **{347 + i: ("RETURN_FROM_EXILE", i) for i in range(6)},    # 347-352

    # Modal/Choice (353-377) = 25 actions
    # Param = Choice index 0-9
    **{353 + i: ("CHOOSE_MODE", i) for i in range(10)},         # 353-362
    # Param = X value (1-10)
    **{363 + i: ("CHOOSE_X_VALUE", i+1) for i in range(10)},    # 363-372
    # Param = Color index 0-4 (WUBRG)
    **{373 + i: ("CHOOSE_COLOR", i) for i in range(5)},         # 373-377

    # Advanced Combat (378-387 = 10 Actions -> MAPPING INCORRECT IN ORIGINAL?)
    # Original Docstring: 378-397 (20 actions)
    # Attack Planeswalker 0-4 (relative index)
    **{378 + i: ("ATTACK_PLANESWALKER", i) for i in range(5)},  # 378-382
    # Assign Multiple Blockers 0-9 (relative to current attackers)
    **{383 + i: ("ASSIGN_MULTIPLE_BLOCKERS", i) for i in range(10)}, # 383-392
    # Gaps filled (393-397) = 5 actions
    **{i: ("NO_OP", None) for i in range(393, 398)},            # 393-397

    # Alternative Casting (398-409) = 12 actions -> Some require context
    398: ("CAST_WITH_FLASHBACK", None), # Context={'gy_idx': X}
    399: ("CAST_WITH_JUMP_START", None), # Context={'gy_idx': X, 'discard_idx': Y}
    400: ("CAST_WITH_ESCAPE", None), # Context={'gy_idx': X, 'gy_indices_escape': [...]}
    401: ("CAST_FOR_MADNESS", None), # Context={'card_id': X, 'exile_idx': Y}
    402: ("CAST_WITH_OVERLOAD", None), # Context={'hand_idx': X}
    403: ("CAST_FOR_EMERGE", None), # Context={'hand_idx': X, 'sacrifice_idx': Y}
    404: ("CAST_FOR_DELVE", None), # Context={'hand_idx': X, 'gy_indices': [...]}
    405: ("PAY_KICKER", True), # Implicitly uses pending_spell_context
    406: ("PAY_KICKER", False),# Implicitly uses pending_spell_context
    407: ("PAY_ADDITIONAL_COST", True), # Implicitly uses pending_spell_context
    408: ("PAY_ADDITIONAL_COST", False),# Implicitly uses pending_spell_context
    409: ("PAY_ESCALATE", None), # Context={'num_extra_modes': X}, Implicitly uses pending_spell_context

    # Token/Copy (410-417) = 8 actions
    # Param = Predefined token type index (0-4)
    **{410 + i: ("CREATE_TOKEN", i) for i in range(5)}, # 410-414
    415: ("COPY_PERMANENT", None), # Context={'target_identifier':X}
    416: ("COPY_SPELL", None), # Context={'target_stack_identifier':X}
    417: ("POPULATE", None), # Context={'target_token_identifier':X}

    # Specific Mechanics (418-429) = 12 actions -> Mostly need context
    418: ("INVESTIGATE", None), # Usually result of effect, not direct action? Can be activated. Needs context.
    419: ("FORETELL", None), # Context={'hand_idx':X}
    420: ("AMASS", None), # Usually result of effect. Needs context if activated.
    421: ("LEARN", None), # Result of effect.
    422: ("VENTURE", None), # Can be activated. Needs context.
    423: ("EXERT", None), # Choice during attack declaration. Needs context {'creature_idx':X}.
    424: ("EXPLORE", None), # Usually result of effect. Needs context if activated.
    425: ("ADAPT", None), # Activated. Needs context {'creature_idx':X, 'amount':Y}
    426: ("MUTATE", None), # Casting cost. Needs context {'hand_idx':X, 'target_idx':Y}
    427: ("CYCLING", None), # Activated from hand. Needs context {'hand_idx':X}
    428: ("GOAD", None), # Usually result of effect. Needs context {'target_creature_identifier':X} if activated.
    429: ("BOAST", None), # Activated after attack. Needs context {'creature_idx':X}.

    # Response Actions (430-434) = 5 actions -> Need context
    430: ("COUNTER_SPELL", None), # Context={'hand_idx':X, 'target_spell_idx':Y}
    431: ("COUNTER_ABILITY", None), # Context={'hand_idx':X, 'target_ability_idx':Y}
    432: ("PREVENT_DAMAGE", None), # Needs context if casting spell.
    433: ("REDIRECT_DAMAGE", None),# Needs context if casting spell.
    434: ("STIFLE_TRIGGER", None), # Context={'hand_idx':X, 'target_trigger_idx':Y}

    # Combat Actions (435-444) = 10 actions -> Delegated, need context
    435: ("FIRST_STRIKE_ORDER", None), # Delegated to CombatActionHandler
    436: ("ASSIGN_COMBAT_DAMAGE", None), # Delegated to CombatActionHandler
    437: ("NINJUTSU", None), # Delegated to CombatActionHandler
    438: ("DECLARE_ATTACKERS_DONE", None),# Delegated to CombatActionHandler
    439: ("DECLARE_BLOCKERS_DONE", None), # Delegated to CombatActionHandler
    440: ("LOYALTY_ABILITY_PLUS", None), # Param=bf_idx. Needs context {'ability_idx':X} maybe? Handler finds correct ability.
    441: ("LOYALTY_ABILITY_ZERO", None), # Param=bf_idx.
    442: ("LOYALTY_ABILITY_MINUS", None),# Param=bf_idx.
    443: ("ULTIMATE_ABILITY", None), # Param=bf_idx.
    444: ("PROTECT_PLANESWALKER", None), # Delegated to CombatActionHandler

    # Card Type Specific (445-461) = 17 actions -> Mostly need context
    445: ("CAST_LEFT_HALF", None), # Param=hand_idx
    446: ("CAST_RIGHT_HALF", None), # Param=hand_idx
    447: ("CAST_FUSE", None), # Param=hand_idx
    448: ("AFTERMATH_CAST", None), # Context={'gy_idx':X}
    449: ("FLIP_CARD", None), # Context={'battlefield_idx':X}
    450: ("EQUIP", None), # Context={'equip_identifier':X, 'target_identifier':Y}
    451: ("NO_OP", None), # UNEQUIP removed (rarely a direct action)
    452: ("NO_OP", None), # ATTACH_AURA removed (happens on resolution)
    453: ("FORTIFY", None), # Context={'fort_identifier':X, 'target_identifier':Y}
    454: ("RECONFIGURE", None), # Context={'card_identifier':X, 'target_identifier':Y (optional)}
    455: ("MORPH", None), # Context={'battlefield_idx':X} (Turn face up)
    456: ("MANIFEST", None), # Context={'battlefield_idx':X} (Turn face up)
    457: ("CLASH", None),
    458: ("CONSPIRE", None), # Context={'spell_stack_idx':X, 'creature1_identifier':Y, 'creature2_identifier':Z}
    459: ("NO_OP", None), # CONVOKE removed (passive cost reduction)
    460: ("GRANDEUR", None), # Context={'hand_idx':X}
    461: ("NO_OP", None), # HELLBENT removed (passive check)

    # Attack Battle (462-466) = 5 actions
    # Param = Relative battle index (0-4). Handler uses context to know which attacker.
    **{462 + i: ("ATTACK_BATTLE", i) for i in range(5)}, # 462-466

    # Fill the remaining space (467-479) = 13 actions
    **{i: ("NO_OP", None) for i in range(467, 480)} # 467-479
}
# Ensure size is correct after updates
if len(ACTION_MEANINGS) != 480:
    raise ValueError(f"ACTION_MEANINGS size is WRONG after update: {len(ACTION_MEANINGS)} expected 480")

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
            """Maps action type strings to their handler methods. (Updated for new mapping)"""
            return {
                # Basic Flow
                "END_TURN": self._handle_end_turn, "UNTAP_NEXT": self._handle_untap_next,
                "DRAW_NEXT": self._handle_draw_next, "MAIN_PHASE_END": self._handle_main_phase_end,
                # "COMBAT_DAMAGE": self._handle_combat_damage, # Mapped to NO_OP(4) now
                "END_PHASE": self._handle_end_phase, # Keep handler but action may be unused
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
                # Combat
                "ATTACK": self._handle_attack,
                "BLOCK": self._handle_block,
                # Delegated Combat Actions (Remapped indices based on review)
                "DECLARE_ATTACKERS_DONE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DECLARE_ATTACKERS_DONE", p, context=context), # Index 438
                "DECLARE_BLOCKERS_DONE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DECLARE_BLOCKERS_DONE", p, context=context), # Index 439 
                "ATTACK_PLANESWALKER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ATTACK_PLANESWALKER", p, context=context), # Indices 378-382
                "ASSIGN_MULTIPLE_BLOCKERS": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ASSIGN_MULTIPLE_BLOCKERS", p, context=context), # Indices 383-392
                "FIRST_STRIKE_ORDER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "FIRST_STRIKE_ORDER", p, context=context), # Index 435
                "ASSIGN_COMBAT_DAMAGE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ASSIGN_COMBAT_DAMAGE", p, context=context), # Index 436
                "PROTECT_PLANESWALKER": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "PROTECT_PLANESWALKER", p, context=context), # Index 444
                "ATTACK_BATTLE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "ATTACK_BATTLE", p, context=context), # Indices 462-466
                "DEFEND_BATTLE": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "DEFEND_BATTLE", p, context=context), # Index 204
                "NINJUTSU": lambda p=None, context=None, **k: apply_combat_action(self.game_state, "NINJUTSU", p, context=context), # Index 437
                # Abilities & Mana
                "TAP_LAND_FOR_MANA": self._handle_tap_land_for_mana,
                "TAP_LAND_FOR_EFFECT": self._handle_tap_land_for_effect,
                "ACTIVATE_ABILITY": self._handle_activate_ability, # Indices 100-159
                "LOYALTY_ABILITY_PLUS": lambda p=None, context=None, **k: self._handle_loyalty_ability(p, context=context, action_type="LOYALTY_ABILITY_PLUS", **k), # Index 440
                "LOYALTY_ABILITY_ZERO": lambda p=None, context=None, **k: self._handle_loyalty_ability(p, context=context, action_type="LOYALTY_ABILITY_ZERO", **k), # Index 441
                "LOYALTY_ABILITY_MINUS": lambda p=None, context=None, **k: self._handle_loyalty_ability(p, context=context, action_type="LOYALTY_ABILITY_MINUS", **k),# Index 442
                "ULTIMATE_ABILITY": lambda p=None, context=None, **k: self._handle_loyalty_ability(p, context=context, action_type="ULTIMATE_ABILITY", **k), # Index 443
                # Targeting & Choices
                "SELECT_TARGET": self._handle_select_target, # Indices 274-283
                "SACRIFICE_PERMANENT": self._handle_sacrifice_permanent, # Indices 284-293
                "CHOOSE_MODE": self._handle_choose_mode, # Indices 353-362
                "CHOOSE_X_VALUE": self._handle_choose_x, # Indices 363-372
                "CHOOSE_COLOR": self._handle_choose_color, # Indices 373-377
                "PUT_TO_GRAVEYARD": self._handle_scry_surveil_choice, # Index 305
                "PUT_ON_TOP": self._handle_scry_surveil_choice, # Index 306
                "PUT_ON_BOTTOM": self._handle_scry_surveil_choice, # Index 307
                # Library/Card Movement
                "SEARCH_LIBRARY": self._handle_search_library, # Indices 299-303
                "DREDGE": self._handle_dredge, # Index 308
                # Counter Management
                "ADD_COUNTER": self._handle_add_counter, # Indices 314-323
                "REMOVE_COUNTER": self._handle_remove_counter, # Indices 324-333
                "PROLIFERATE": self._handle_proliferate, # Index 334
                # Zone Movement
                "RETURN_FROM_GRAVEYARD": self._handle_return_from_graveyard, # Indices 335-340
                "REANIMATE": self._handle_reanimate, # Indices 341-346
                "RETURN_FROM_EXILE": self._handle_return_from_exile, # Indices 347-352
                # Alternative Casting
                "CAST_WITH_FLASHBACK": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_WITH_FLASHBACK", context=context, **k), # Index 398
                "CAST_WITH_JUMP_START": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_WITH_JUMP_START", context=context, **k), # Index 399
                "CAST_WITH_ESCAPE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_WITH_ESCAPE", context=context, **k), # Index 400
                "CAST_FOR_MADNESS": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_FOR_MADNESS", context=context, **k), # Index 401
                "CAST_WITH_OVERLOAD": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_WITH_OVERLOAD", context=context, **k), # Index 402
                "CAST_FOR_EMERGE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_FOR_EMERGE", context=context, **k), # Index 403
                "CAST_FOR_DELVE": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="CAST_FOR_DELVE", context=context, **k), # Index 404
                "AFTERMATH_CAST": lambda p=None, context=None, **k: self._handle_alternative_casting(p, action_type="AFTERMATH_CAST", context=context, **k), # Index 448
                # Informational Flags
                "PAY_KICKER": self._handle_pay_kicker, # Index 405/406
                "PAY_ADDITIONAL_COST": self._handle_pay_additional_cost, # Index 407/408
                "PAY_ESCALATE": self._handle_pay_escalate, # Index 409
                # Token/Copy
                "CREATE_TOKEN": self._handle_create_token, # Indices 410-414
                "COPY_PERMANENT": self._handle_copy_permanent, # Index 415
                "COPY_SPELL": self._handle_copy_spell, # Index 416
                "POPULATE": self._handle_populate, # Index 417
                # Specific Mechanics
                "INVESTIGATE": self._handle_investigate, # Index 418
                "FORETELL": self._handle_foretell, # Index 419
                "AMASS": self._handle_amass, # Index 420
                "LEARN": self._handle_learn, # Index 421
                "VENTURE": self._handle_venture, # Index 422
                "EXERT": self._handle_exert, # Index 423
                "EXPLORE": self._handle_explore, # Index 424
                "ADAPT": self._handle_adapt, # Index 425
                "MUTATE": self._handle_mutate, # Index 426
                "CYCLING": self._handle_cycling, # Index 427
                "GOAD": self._handle_goad, # Index 428
                "BOAST": self._handle_boast, # Index 429
                # Response Actions
                "COUNTER_SPELL": self._handle_counter_spell, # Index 430
                "COUNTER_ABILITY": self._handle_counter_ability, # Index 431
                "PREVENT_DAMAGE": self._handle_prevent_damage, # Index 432
                "REDIRECT_DAMAGE": self._handle_redirect_damage, # Index 433
                "STIFLE_TRIGGER": self._handle_stifle_trigger, # Index 434
                # Card Type Specific
                "CAST_LEFT_HALF": lambda p=None, context=None, **k: self._handle_cast_split(p, context=context, action_type="CAST_LEFT_HALF", **k), # Index 445
                "CAST_RIGHT_HALF": lambda p=None, context=None, **k: self._handle_cast_split(p, context=context, action_type="CAST_RIGHT_HALF", **k),# Index 446
                "CAST_FUSE": lambda p=None, context=None, **k: self._handle_cast_split(p, context=context, action_type="CAST_FUSE", **k),# Index 447
                "FLIP_CARD": self._handle_flip_card, # Index 449
                "EQUIP": self._handle_equip, # Index 450
                "FORTIFY": self._handle_fortify, # Index 453
                "RECONFIGURE": self._handle_reconfigure, # Index 454
                "MORPH": self._handle_morph, # Index 455
                "MANIFEST": self._handle_manifest, # Index 456
                "CLASH": self._handle_clash, # Index 457
                "CONSPIRE": self._handle_conspire, # Index 458
                "GRANDEUR": self._handle_grandeur, # Index 460
                # Room/Class
                "UNLOCK_DOOR": self._handle_unlock_door, # Indices 248-252
                "LEVEL_UP_CLASS": self._handle_level_up_class, # Indices 253-257
                # Discard / Spree
                "DISCARD_CARD": self._handle_discard_card, # Indices 238-247
                "SELECT_SPREE_MODE": self._handle_select_spree_mode, # Indices 258-273
                # Transform
                "TRANSFORM": self._handle_transform, # Indices 160-179
                # NO_OP variants
                "NO_OP": self._handle_no_op, # Various indices
                # Offspring/Impending
                "CAST_FOR_IMPENDING": self._handle_cast_for_impending, # Index 294
                "PAY_OFFSPRING_COST": self._handle_pay_offspring_cost, # Index 295
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
            """
            Return the current action mask as boolean array with reasoning. 
            Includes CRITICAL STATE AUTO-CORRECTION to prevent infinite NO_OP loops.
            Handles all phases, sub-steps, and complex casting sequences.
            """
            gs = self.game_state
            try:
                valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                action_reasons = {} # Reset reasons for this generation

                def set_valid_action(index, reason="", context=None):
                    # Ensures CONCEDE (12) isn't added here, handled at the end.
                    if 0 <= index < self.ACTION_SPACE_SIZE and index != 12:
                        valid_actions[index] = True
                        action_reasons[index] = {"reason": reason, "context": context or {}}
                    elif index != 12:
                        logging.error(f"INVALID ACTION INDEX during generation: {index} bounds (0-{self.ACTION_SPACE_SIZE-1}) Reason: {reason}")

                # --- 1. Player Validation & Perspective ---
                perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2 # Player from whose view we generate
                if not gs.p1 or not gs.p2 or not perspective_player:
                    logging.error("Player object(s) missing or invalid. Defaulting to CONCEDE.")
                    valid_actions[12] = True; action_reasons[12] = {"reason": "Error: Players not initialized", "context": {}}
                    self.action_reasons_with_context = action_reasons; self.action_reasons = {k: v.get("reason","Err") for k, v in action_reasons.items()}
                    
                    debug_log_valid_actions(self.game_state, valid_actions, self.action_reasons_with_context, self.get_action_info)
                    return valid_actions

                current_turn_player = gs._get_active_player() 
                
                # --- 2. Mulligan Phase Logic ---
                if getattr(gs, 'mulligan_in_progress', False):
                    mulligan_player = getattr(gs, 'mulligan_player', None)
                    bottoming_player = getattr(gs, 'bottoming_player', None)

                    # Check if the perspective player needs to bottom cards
                    if bottoming_player == perspective_player:
                        hand_size = len(perspective_player.get("hand", []))
                        current_bottomed = getattr(gs, 'bottoming_count', 0)
                        total_needed = getattr(gs, 'cards_to_bottom', 0)
                        needed_now = max(0, total_needed - current_bottomed)

                        if needed_now > 0:
                            # Generate BOTTOM_CARD actions for valid hand indices
                            # Map indices 0-3 to actions 226-229
                            for i in range(min(hand_size, 4)): 
                                set_valid_action(226 + i, f"BOTTOM_CARD index {i}", context={'hand_idx': i})
                        else: 
                            set_valid_action(224, "NO_OP (Finished bottoming)")
                        return valid_actions

                    # Check if the perspective player needs to make a mulligan decision
                    elif mulligan_player == perspective_player:
                        set_valid_action(225, "KEEP_HAND")
                        # Can always mulligan if under limit
                        if gs.mulligan_count.get('p1' if perspective_player == gs.p1 else 'p2', 0) < 7:
                            set_valid_action(6, "MULLIGAN")
                        return valid_actions

                    # Check if perspective player is waiting for opponent
                    elif bottoming_player or mulligan_player:
                        set_valid_action(224, "NO_OP (Waiting for opponent)")
                        return valid_actions

                    # State Error: Mulligan in progress but no player assigned
                    # Allow NO_OP to cycle step and hopefully trigger external recovery
                    set_valid_action(224, "NO_OP (Mulligan Error Cycle)")
                    return valid_actions

                # --- 3. Special Choice Phase Logic (Targeting, Sacrifice, Choose) ---
                if gs.phase in [gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
                    special_phase_name = gs._PHASE_NAMES.get(gs.phase, "SPECIAL")
                    context = getattr(gs, 'targeting_context', None) or \
                            getattr(gs, 'sacrifice_context', None) or \
                            getattr(gs, 'choice_context', None)
                    
                    acting_player = context.get('controller') or context.get('player') if context else None

                    if acting_player == perspective_player:
                        if gs.phase == gs.PHASE_TARGETING: 
                            self._add_targeting_actions(perspective_player, valid_actions, set_valid_action)
                            # Allow passing if minimum requirements met
                            min_req = gs.targeting_context.get("min_targets", 1)
                            sel = len(gs.targeting_context.get("selected_targets", []))
                            max_targets = gs.targeting_context.get("max_targets", 1)
                            if sel >= min_req and (min_req < max_targets or sel == max_targets):
                                set_valid_action(11, "PASS_PRIORITY (Finish Targeting)")
                                
                        elif gs.phase == gs.PHASE_SACRIFICE: 
                            self._add_sacrifice_actions(perspective_player, valid_actions, set_valid_action)
                            
                        elif gs.phase == gs.PHASE_CHOOSE: 
                            self._add_special_choice_actions(perspective_player, valid_actions, set_valid_action)
                    else:
                        set_valid_action(224, f"NO_OP (Waiting for opponent in {special_phase_name})")

                    # If valid actions found (or NO_OP added), return
                    if np.sum(valid_actions) > 0:
                        self.action_reasons_with_context = action_reasons
                        return valid_actions
                    
                    # Fallback if no actions generated (prevent crash)
                    set_valid_action(224, f"NO_OP (Fallback {special_phase_name})")
                    return valid_actions

                # --- 4. Regular Game Play & State Integrity Check ---
                
                priority_player_obj = getattr(gs, 'priority_player', None)

                # Phases where SOMEONE must have priority (all except Untap/Cleanup)
                interactive_phases = [
                    gs.PHASE_UPKEEP, gs.PHASE_DRAW, gs.PHASE_MAIN_PRECOMBAT, 
                    gs.PHASE_BEGIN_COMBAT, gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, 
                    gs.PHASE_FIRST_STRIKE_DAMAGE, gs.PHASE_COMBAT_DAMAGE, gs.PHASE_END_OF_COMBAT, 
                    gs.PHASE_MAIN_POSTCOMBAT, gs.PHASE_END_STEP, gs.PHASE_PRIORITY
                ]

                if priority_player_obj is None and gs.phase in interactive_phases:
                    active_p = gs._get_active_player()
                    logging.warning(f"STATE RECOVERY: Priority was None in {gs._PHASE_NAMES.get(gs.phase)}. Auto-assigning to {active_p.get('name', 'AP')} inside mask generation.")
                    
                    # Direct GameState Mutation to fix the error immediately
                    gs.priority_player = active_p
                    gs.priority_pass_count = 0
                    
                    # Update local variable so the rest of this function runs correctly
                    priority_player_obj = active_p
                    
                    # If stack is empty and we're in a phase that should auto-progress, do it
                    if not gs.stack and gs.phase == gs.PHASE_UPKEEP:
                        logging.info("Auto-progressing from UPKEEP to DRAW phase after priority fix")
                        gs.phase = gs.PHASE_DRAW

                # --- Check Priority Match ---
                has_priority = (priority_player_obj == perspective_player)

                if not has_priority:
                    # --- Perspective Player Does NOT Have Priority ---
                    # Allow NO_OP when waiting.
                    set_valid_action(224, "NO_OP (Waiting for priority)")
                    
                    # Allow instant-speed MANA abilities (allowed without priority - Rule 605.3a)
                    self._add_mana_ability_actions(perspective_player, valid_actions, set_valid_action)

                else:
                    # --- Perspective Player HAS Priority (or was just auto-assigned it) ---
                    set_valid_action(11, "PASS_PRIORITY") # Always allowed

                    split_second_is_active = getattr(gs, 'split_second_active', False)
                    
                    if split_second_is_active:
                        # Only add mana abilities (and PASS already added)
                        logging.debug("Split Second active, only allowing Mana abilities and PASS.")
                        self._add_mana_ability_actions(perspective_player, valid_actions, set_valid_action)
                    else:
                        is_my_turn = (current_turn_player == perspective_player)
                        opponent_player = gs.p2 if perspective_player == gs.p1 else gs.p1
                        
                        # Check Timing
                        can_act_sorcery_speed = gs._can_act_at_sorcery_speed(perspective_player)
                        can_act_instant_speed = gs.phase not in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP]

                        # Sorcery Speed Actions
                        if can_act_sorcery_speed:
                            self._add_sorcery_speed_actions(perspective_player, opponent_player, valid_actions, set_valid_action)
                            self._add_basic_phase_actions(is_my_turn, valid_actions, set_valid_action)
                            # Include Split Cards logic which handles Fuse/Split casting
                            self._add_split_card_actions(perspective_player, valid_actions, set_valid_action)

                        # Instant Speed Actions
                        if can_act_instant_speed:
                            self._add_instant_speed_actions(perspective_player, opponent_player, valid_actions, set_valid_action)
                            self._add_damage_prevention_actions(perspective_player, valid_actions, set_valid_action)

                        # Combat Actions
                        if hasattr(self, 'combat_handler') and self.combat_handler:
                            active_p_gs = gs._get_active_player()
                            non_active_p_gs = gs._get_non_active_player()

                            if gs.phase == gs.PHASE_DECLARE_ATTACKERS and perspective_player == active_p_gs:
                                self.combat_handler._add_attack_declaration_actions(perspective_player, non_active_p_gs, valid_actions, set_valid_action)
                            elif gs.phase == gs.PHASE_DECLARE_BLOCKERS and perspective_player == non_active_p_gs and getattr(gs, 'current_attackers', []):
                                self.combat_handler._add_block_declaration_actions(perspective_player, valid_actions, set_valid_action)

                        # Stack Interactions
                        self._add_x_cost_actions(perspective_player, valid_actions, set_valid_action)
                        
                        # Pending Spell Contexts (Complex Casting)
                        pending_context = getattr(gs, 'pending_spell_context', None)
                        if pending_context and pending_context.get('card_id') and \
                        pending_context.get('controller') == perspective_player:
                            
                            card_id = pending_context['card_id']
                            card = gs._safe_get_card(card_id)
                            
                            # Kicker / Escalate / Additional Costs
                            self._add_kicker_options(perspective_player, valid_actions, set_valid_action)
                            
                            # Spree Modes
                            if getattr(card, 'is_spree', False):
                                self._add_spree_mode_actions(perspective_player, valid_actions, set_valid_action)

                            # Offspring Cost Payment
                            if card and getattr(card, 'is_offspring', False) and \
                            pending_context.get('pay_offspring') is None and \
                            pending_context.get('waiting_for_choice') == 'offspring_cost':
                                    cost_str = getattr(card, 'offspring_cost', None)
                                    if cost_str and self._can_afford_cost_string(perspective_player, cost_str, context=pending_context):
                                        offspring_context = {'action_source': 'offspring_payment_opportunity'}
                                        set_valid_action(295, f"Optional: PAY_OFFSPRING_COST for {getattr(card, 'name', card_id)}", context=offspring_context)

                # --- 5. Final Concede Check ---
                self.action_reasons_with_context = action_reasons.copy()
                self.action_reasons = {k: v.get("reason","Unknown") for k, v in action_reasons.items()}
                
                num_valid_non_concede = np.sum(valid_actions)

                if num_valid_non_concede == 0:
                    # Only add CONCEDE if *truly* no other action (not even NO_OP or PASS) is available.
                    valid_actions[12] = True
                    concede_reason = "CONCEDE (No other valid actions)"
                    if 12 not in action_reasons:
                        action_reasons[12] = {"reason": concede_reason, "context": {}}
                        self.action_reasons_with_context[12] = action_reasons[12]; self.action_reasons[12] = concede_reason
                    logging.warning("generate_valid_actions: No valid actions found, only CONCEDE is available.")

                debug_log_valid_actions(self.game_state, valid_actions, self.action_reasons_with_context, self.get_action_info)
                return valid_actions

            except Exception as e:
                # Critical Fallback
                logging.critical(f"CRITICAL error generating valid actions: {str(e)}", exc_info=True)
                fallback_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                fallback_actions[11] = True # Pass Priority
                fallback_actions[12] = True # Concede
                self.action_reasons = {11: "Crit Err - PASS", 12: "Crit Err - CONCEDE"}
                self.action_reasons_with_context = {11: {"reason":"Crit Err - PASS","context":{}}, 12: {"reason":"Crit Err - CONCEDE","context":{}}}
                debug_log_valid_actions(self.game_state, fallback_actions, self.action_reasons_with_context, self.get_action_info)
                return fallback_actions
    
    def _handle_no_op(self, param, context=None, **kwargs):
        """Handles NO_OP. Checks for stuck state and forces recovery."""
        gs = self.game_state
        logging.debug("Executed NO_OP action.")
        
        # Fix for stuck state: If NO_OP executed when priority is None in a playable phase, force recovery.
        if gs.priority_player is None and gs.phase not in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP, gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
            logging.warning("NO_OP executed while priority is None (Stuck State). Attempting recovery.")
            
            # Get the active player first
            active_player = gs._get_active_player()
            
            # 1. Try standard logic to assign priority (might fail if stack logic is strict)
            try:
                gs._pass_priority()
            except Exception as e:
                logging.error(f"Error during _pass_priority in NO_OP recovery: {e}")
            
            # 2. Force assignment if standard logic didn't resolve it
            if gs.priority_player is None:
                logging.warning(f"Recovery: Force assigning priority to Active Player ({active_player.get('name', 'Unknown')}).")
                gs.priority_player = active_player
                gs.priority_pass_count = 0
            
        return 0.0, True
        
    def check_mulligan_state(self):
            """
            Helper function to diagnose mulligan state inconsistencies.
            Fixed to target self.game_state attributes.
            """
            gs = self.game_state
            
            # Case 1: Both mulligan_player and bottoming_player are None but still in mulligan phase
            if gs.mulligan_in_progress and gs.mulligan_player is None and not gs.bottoming_in_progress:
                logging.error("Inconsistent state: In mulligan phase with no active mulligan player")
                unmade_decisions = 0
                for p, p_id in [(gs.p1, 'p1'), (gs.p2, 'p2')]:
                    if p and not p.get('_mulligan_decision_made', False):
                        unmade_decisions += 1
                        gs.mulligan_player = p
                        logging.info(f"Recovering mulligan state by assigning {p_id} as mulligan player")
                
                if unmade_decisions != 1:
                    logging.warning(f"Found {unmade_decisions} players with undecided mulligans. Forcing end of mulligan phase.")
                    self._end_mulligan_phase()
                    return False
                return True
            
            # Case 2: In bottoming phase but no bottoming player
            if gs.bottoming_in_progress and gs.bottoming_player is None:
                logging.error("Inconsistent state: In bottoming phase with no active bottoming player")
                needs_bottom_found = 0
                for p, p_id in [(gs.p1, 'p1'), (gs.p2, 'p2')]:
                    if p and p.get('_needs_to_bottom_next', False) and not p.get('_bottoming_complete', False):
                        needs_bottom_found += 1
                        gs.bottoming_player = p
                        gs.bottoming_count = 0
                        gs.cards_to_bottom = min(gs.mulligan_count.get(p_id, 0), len(p.get("hand", [])))
                        logging.info(f"Recovering bottoming state by assigning {p_id} as bottoming player")
                
                if needs_bottom_found != 1:
                    logging.warning(f"Found {needs_bottom_found} players needing to bottom. Forcing end of mulligan phase.")
                    self._end_mulligan_phase()
                    return False
                return True
            
            # Case 3: Neither mulligan nor bottoming in progress, but mulligan_in_progress flag is still set
            if gs.mulligan_in_progress and not gs.bottoming_in_progress and gs.mulligan_player is None:
                all_decided = True
                for p in [gs.p1, gs.p2]:
                    if p and not p.get('_mulligan_decision_made', False):
                        all_decided = False
                        break
                        
                if all_decided:
                    logging.info("All players have made mulligan decisions but phase not ended. Ending mulligan.")
                    self._end_mulligan_phase()
                    return False
                else:
                    logging.error("Inconsistent mulligan state: No bottoming, not all decided, but no mulligan_player")
                    self._end_mulligan_phase() 
                    return False
            
            # Case 4: Bottoming needed but stalled - check counters
            if gs.bottoming_in_progress and gs.bottoming_player:
                if gs.cards_to_bottom <= 0 or gs.bottoming_count >= gs.cards_to_bottom:
                    logging.error(f"Bottoming stalled: to_bottom={gs.cards_to_bottom}, count={gs.bottoming_count}")
                    gs.bottoming_player['_bottoming_complete'] = True
                    
                    other_player = gs.p2 if gs.bottoming_player == gs.p1 else gs.p1
                    if other_player and other_player.get('_needs_to_bottom_next', False) and not other_player.get('_bottoming_complete', False):
                        gs.bottoming_player = other_player
                        gs.bottoming_count = 0
                        other_id = 'p2' if other_player == gs.p2 else 'p1'
                        gs.cards_to_bottom = min(gs.mulligan_count.get(other_id, 0), len(other_player.get("hand", [])))
                        logging.info(f"Transitioning bottoming to next player: {other_player['name']}")
                    else:
                        logging.info("No more players need to bottom. Ending mulligan phase.")
                        self._end_mulligan_phase()
                        return False
            
            # Case 5: Final safety check - if in limbo, force end
            if (gs.mulligan_in_progress or gs.bottoming_in_progress) and gs.turn >= 1:
                logging.error("Critical inconsistency: In mulligan/bottoming but turn >= 1. Forcing end.")
                self._end_mulligan_phase()
                return False
            
            return True
         
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
                        if adv_data and ('sorcery' in adv_data.get('type','').lower() or 'instant' in adv_data.get('type','').lower()):
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
        self._add_specific_mechanics_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)
        
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
        self._add_specific_mechanics_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)
        
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
                card_name = getattr(card, 'name', card_id)
                set_valid_action(306, f"PUT_ON_TOP {card_name}") # Action for Top - Index 306 maps to PUT_ON_TOP
                if choice_type == "scry":
                    set_valid_action(307, f"PUT_ON_BOTTOM {card_name}") # Action for Bottom - Index 307 maps to PUT_ON_BOTTOM
                else: # Surveil
                    set_valid_action(305, f"PUT_TO_GRAVEYARD {card_name}") # Action for GY - Index 305 maps to PUT_TO_GRAVEYARD

            # Dredge (Replace Draw)
            elif choice_type == "dredge" and context.get("card_id"):
                card_id = context.get("card_id")
                dredge_val = context.get("value")
                if len(player["library"]) >= dredge_val:
                    # Find card index in graveyard
                    gy_idx = -1
                    for idx, gy_id in enumerate(player.get("graveyard", [])):
                        if gy_id == card_id and idx < 6: # GY Index 0-5 (Action space limited)
                            gy_idx = idx
                            break
                    if gy_idx != -1:
                        # Provide context for DREDGE action handler
                        dredge_action_context = {'gy_idx': gy_idx}
                        set_valid_action(308, f"DREDGE {gs._safe_get_card(card_id).name}", context=dredge_action_context)
                # Allow skipping the dredge replacement
                set_valid_action(11, "Skip Dredge") # PASS_PRIORITY effectively skips

            # Choose Mode
            elif choice_type == "choose_mode":
                num_choices = context.get("num_choices", 0)
                max_modes = context.get("max_required", 1)
                min_modes = context.get("min_required", 1)
                selected_count = len(context.get("selected_modes", []))

                # Allow choosing another mode if max not reached
                if selected_count < max_modes:
                    for i in range(min(num_choices, 10)): # Mode index 0-9
                        # Prevent selecting the same mode twice unless allowed
                        if i not in context.get("selected_modes", []):
                            # FIXED: Use correct index range 353-362 for CHOOSE_MODE
                            set_valid_action(353 + i, f"CHOOSE_MODE {i+1}")

                # Allow finalizing choice if minimum met (and min != max)
                if selected_count >= min_modes and min_modes != max_modes:
                    set_valid_action(11, "PASS_PRIORITY (Finish Mode Choice)")

            # Choose X
            elif choice_type == "choose_x":
                max_x = context.get("max_x", 0)
                min_x = context.get("min_x", 0)
                for i in range(min(max_x, 10)): # X value 1-10
                    x_val = i + 1
                    if x_val >= min_x:
                        # FIXED: Use correct index range 363-372 for CHOOSE_X_VALUE
                        set_valid_action(363 + i, f"CHOOSE_X_VALUE {x_val}")

            # Choose Color
            elif choice_type == "choose_color":
                for i in range(5): # Color index 0-4 (WUBRG)
                    # FIXED: Use correct index range 373-377 for CHOOSE_COLOR
                    set_valid_action(373 + i, f"CHOOSE_COLOR {['W','U','B','R','G'][i]}")

            # Kicker / Additional Cost / Escalate Choices (Using correct indices now)
            elif choice_type == "pay_kicker":
                set_valid_action(405, "PAY_KICKER") # Param=True
                set_valid_action(406, "DONT_PAY_KICKER") # Param=False
            elif choice_type == "pay_additional":
                set_valid_action(407, "PAY_ADDITIONAL_COST") # Param=True
                set_valid_action(408, "DONT_PAY_ADDITIONAL_COST") # Param=False
            elif choice_type == "pay_escalate":
                max_extra = context.get('max_modes', 1) - context.get('num_selected', 1)
                for i in range(min(max_extra, 3)): # Allow paying for 1, 2, or 3 extra modes max
                    num_extra = i + 1
                    # Check affordability of paying N times
                    escalate_cost = context.get('escalate_cost_each')
                    if escalate_cost and gs.mana_system.can_pay_mana_cost(player, f"{escalate_cost}*{num_extra}"):
                            escalate_action_context = {'num_extra_modes': num_extra}
                            set_valid_action(409, f"PAY_ESCALATE for {num_extra} extra mode(s)", context=escalate_action_context)
                set_valid_action(11, "PASS_PRIORITY (Finish Escalate/Don't pay)")

        else:
            # If no choice context is active during PHASE_CHOOSE, allow PASS
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
            if hasattr(gs, 'targeting_system') and gs.targeting_system:
                valid_targets_map = gs.targeting_system.get_valid_targets(source_id, player, target_type)
            else:
                # Fallback: Add basic logic here or assume it's handled by agent
                logging.warning("Targeting system not available, cannot generate specific targeting actions.")
                pass # Need a fallback

            # Flatten the valid targets map into a list
            valid_targets_list = []
            for category, targets in valid_targets_map.items():
                valid_targets_list.extend(targets)
                
            # Remove already selected targets to avoid duplicates
            already_selected = context.get('selected_targets', [])
            valid_targets_list = [target for target in valid_targets_list if target not in already_selected]

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

        # Counter Spell - Using correct action index 430
        if stack_has_opponent_spell:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "counter target spell" in card.oracle_text.lower():
                    if self._can_afford_card(player, card):
                        # Create context with hand_idx and any targets needed in handler
                        counter_context = {'hand_idx': i}
                        # Find a valid target spell to include in context
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and item[0] == "SPELL" and item[2] != player:
                                counter_context['target_spell_idx'] = stack_idx
                                break
                        set_valid_action(430, f"COUNTER_SPELL with {card.name}", context=counter_context)

        # Counter Ability - Using correct action index 431
        if stack_has_opponent_ability:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and ("counter target ability" in card.oracle_text.lower() or 
                                                            "counter target activated ability" in card.oracle_text.lower()):
                    if self._can_afford_card(player, card):
                        # Include necessary context for handler
                        counter_ability_context = {'hand_idx': i}
                        # Find a valid target ability to include in context
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and item[0] == "ABILITY" and item[2] != player:
                                counter_ability_context['target_ability_idx'] = stack_idx
                                break
                        set_valid_action(431, f"COUNTER_ABILITY with {card.name}", context=counter_ability_context)

        # Prevent Damage - Using correct action index 432
        # Check if a damage spell/ability is on stack or if combat damage is pending
        damage_pending = gs.phase in [gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE] or \
                        any(isinstance(item, tuple) and "damage" in getattr(gs._safe_get_card(item[1]), 'oracle_text', '').lower() for item in gs.stack)
        if damage_pending:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "prevent" in card.oracle_text.lower() and "damage" in card.oracle_text.lower():
                    if self._can_afford_card(player, card):
                        prevent_context = {'hand_idx': i}
                        # Find damage source if applicable
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and "damage" in getattr(gs._safe_get_card(item[1]), 'oracle_text', '').lower():
                                prevent_context['damage_source_idx'] = stack_idx
                                break
                        set_valid_action(432, f"PREVENT_DAMAGE with {card.name}", context=prevent_context)

        # Redirect Damage - Using correct action index 433
        # Similar to prevent damage, check for damage sources
        if damage_pending:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "redirect" in card.oracle_text.lower() and "damage" in card.oracle_text.lower():
                    if self._can_afford_card(player, card):
                        redirect_context = {'hand_idx': i}
                        set_valid_action(433, f"REDIRECT_DAMAGE with {card.name}", context=redirect_context)

        # Stifle Trigger - Using correct action index 434
        # For now, enable if a triggered ability is on stack
        stack_has_trigger = any(isinstance(item, tuple) and item[0] == "TRIGGER" for item in gs.stack)
        if stack_has_trigger:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "counter target triggered ability" in card.oracle_text.lower():
                    if self._can_afford_card(player, card):
                        stifle_context = {'hand_idx': i}
                        # Find a valid target trigger to include in context
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and item[0] == "TRIGGER":
                                stifle_context['target_trigger_idx'] = stack_idx
                                break
                        set_valid_action(434, f"STIFLE_TRIGGER with {card.name}", context=stifle_context)
        
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
                        # FIXED: Use correct action index 427 for CYCLING
                        cycling_context = {'hand_idx': i}
                        set_valid_action(427, f"CYCLING {card.name}", context=cycling_context)

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
        """Add actions for counter management."""
        gs = self.game_state
        
        # Only show counter actions if we're in a context that requires them
        if hasattr(gs, 'counter_context') and gs.counter_context:
            context = gs.counter_context
            counter_type = context.get('counter_type', '+1/+1')
            action_type = context.get('action_type', 'ADD_COUNTER')
            
            # ADD_COUNTER actions (indices 314-323)
            if action_type == "ADD_COUNTER":
                valid_targets = []
                # Target determination based on counter type
                if counter_type == '+1/+1':
                    # Creatures can get +1/+1 counters
                    for perm_id in player["battlefield"]:
                        perm_card = gs._safe_get_card(perm_id)
                        if perm_card and 'creature' in getattr(perm_card, 'card_types', []):
                            valid_targets.append(perm_id)
                elif counter_type == 'loyalty':
                    # Planeswalkers get loyalty counters
                    for perm_id in player["battlefield"]:
                        perm_card = gs._safe_get_card(perm_id)
                        if perm_card and 'planeswalker' in getattr(perm_card, 'card_types', []):
                            valid_targets.append(perm_id)
                # Generic case for other counter types
                else:
                    for perm_id in player["battlefield"]:
                        valid_targets.append(perm_id)
                
                # Generate ADD_COUNTER actions
                for i, perm_id in enumerate(valid_targets[:10]):  # Limit to 10 targets
                    perm_card = gs._safe_get_card(perm_id)
                    perm_name = getattr(perm_card, 'name', perm_id) if perm_card else str(perm_id)
                    counter_context = {'counter_type': counter_type, 'target_identifier': perm_id}
                    set_valid_action(314 + i, f"ADD {counter_type} COUNTER to {perm_name}", context=counter_context)
                    
            # REMOVE_COUNTER actions (indices 324-333)
            elif action_type == "REMOVE_COUNTER":
                valid_targets = []
                # Find permanents that have the specified counter type
                for perm_id in player["battlefield"]:
                    perm_card = gs._safe_get_card(perm_id)
                    if perm_card and hasattr(perm_card, 'counters') and perm_card.counters.get(counter_type, 0) > 0:
                        valid_targets.append(perm_id)
                
                # Generate REMOVE_COUNTER actions
                for i, perm_id in enumerate(valid_targets[:10]):  # Limit to 10 targets
                    perm_card = gs._safe_get_card(perm_id)
                    perm_name = getattr(perm_card, 'name', perm_id) if perm_card else str(perm_id)
                    counter_context = {'counter_type': counter_type, 'target_identifier': perm_id}
                    set_valid_action(324 + i, f"REMOVE {counter_type} COUNTER from {perm_name}", context=counter_context)
                    
            # PROLIFERATE action (index 334)
            elif action_type == "PROLIFERATE":
                # Check if there are any permanents with counters
                has_permanents_with_counters = False
                
                for perm_id in player["battlefield"]:
                    perm_card = gs._safe_get_card(perm_id)
                    if perm_card and hasattr(perm_card, 'counters') and any(count > 0 for count in perm_card.counters.values()):
                        has_permanents_with_counters = True
                        break
                
                if not has_permanents_with_counters:
                    # Check opponent's permanents
                    opponent = gs.p2 if player == gs.p1 else gs.p1
                    for perm_id in opponent["battlefield"]:
                        perm_card = gs._safe_get_card(perm_id)
                        if perm_card and hasattr(perm_card, 'counters') and any(count > 0 for count in perm_card.counters.values()):
                            has_permanents_with_counters = True
                            break
                
                if has_permanents_with_counters:
                    set_valid_action(334, "PROLIFERATE to add counters to all permanents with counters")
        
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
        
        # *** EARLY SAFETY CHECK FOR STUCK STATE ***
        if action_idx == 224:  # NO_OP action
            # Count consecutive NO_OPs
            if not hasattr(gs, '_consecutive_no_ops'):
                gs._consecutive_no_ops = 0
            gs._consecutive_no_ops += 1
            
            # Progressive Recovery Strategy
            if gs._consecutive_no_ops > 3:
                logging.warning(f"STUCK STATE DETECTED: {gs._consecutive_no_ops} consecutive NO_OPs!")
                
                # Level 1: Fix missing priority
                if gs.priority_player is None and gs.phase not in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP]:
                    active_player = gs._get_active_player()
                    logging.warning(f"Emergency fix L1: Assigning priority to {active_player.get('name', 'Active Player')}")
                    gs.priority_player = active_player
                    gs.priority_pass_count = 0
                
                # Level 2: Force Pass Priority (if stuck with priority or opponent stalled)
                # This usually happens if the env thinks agent acts, but agent thinks no priority
                elif gs._consecutive_no_ops > 6:
                    logging.warning("Emergency fix L2: Forcing priority pass/stack resolution.")
                    gs._pass_priority()
                
                # Level 3: Force Phase Advance (Hard Stuck)
                if gs._consecutive_no_ops > 12:
                    logging.error("Emergency fix L3: Hard stuck. Forcing phase advance.")
                    if hasattr(gs, '_advance_phase'):
                         gs._advance_phase()
                    else:
                         # Fallback manual advance
                         gs.phase = gs.phase + 1 if gs.phase < gs.PHASE_CLEANUP else gs.PHASE_UNTAP
                         gs.priority_player = gs._get_active_player()
                         gs.priority_pass_count = 0

        else:
            # Reset counter on non-NO_OP action
            gs._consecutive_no_ops = 0
        
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
        """
        Unified and improved handler for Scry/Surveil actions with better validation and outcomes tracking.
        
        Args:
            param: The action parameter (unused directly)
            context: Context including card index information
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        action_index = kwargs.get('action_index')

        # Determine action type based on action index
        if action_index == 305:  # PUT_TO_GRAVEYARD (Surveil only)
            destination = "graveyard"
            action_name = "PUT_TO_GRAVEYARD"
        elif action_index == 306:  # PUT_ON_TOP (Both Scry and Surveil)
            destination = "top"
            action_name = "PUT_ON_TOP"
        elif action_index == 307:  # PUT_ON_BOTTOM (Scry only)
            destination = "bottom"
            action_name = "PUT_ON_BOTTOM"
        else:
            logging.error(f"Invalid scry/surveil action index: {action_index}")
            return -0.2, False

        # Validate context existence and structure
        if not hasattr(gs, 'choice_context') or gs.choice_context is None:
            logging.warning(f"{action_name} called outside of CHOOSE context")
            return -0.2, False

        context = gs.choice_context
        current_choice_type = context.get("type")

        # Validate context type matches action
        if current_choice_type not in ["scry", "surveil"]:
            logging.warning(f"{action_name} called in incorrect context: {current_choice_type}")
            return -0.2, False
            
        # Validate player is the one making the choice
        if context.get("player") != player:
            logging.warning(f"{action_name} called for incorrect player")
            return -0.2, False
            
        # Validate cards available to process
        if not context.get("cards"):
            logging.warning(f"{action_name} called but no cards to process")
            gs.choice_context = None
            gs.phase = gs.PHASE_PRIORITY
            return -0.1, False

        # Validate action type against context type
        if (destination == "graveyard" and current_choice_type != "surveil") or \
        (destination == "bottom" and current_choice_type != "scry"):
            logging.warning(f"Invalid action {action_name} for {current_choice_type}")
            return -0.1, False

        # Process the card choice
        card_id = context["cards"].pop(0)
        card = gs._safe_get_card(card_id)
        card_name = getattr(card, 'name', card_id)
        
        # Evaluate card value in current context
        card_value = 0.0
        if self.card_evaluator and card:
            card_value = self.card_evaluator.evaluate_card(card_id, "general", 
                                                        context_details={"destination": destination,
                                                                        "action": current_choice_type})

        # Process the card based on destination
        if destination == "top":
            if current_choice_type == "scry":
                # Add to list of cards kept on top
                context.setdefault("kept_on_top", []).append(card_id)
                logging.debug(f"Scry: Keeping {card_name} on top (pending order)")
                reward = 0.05 + card_value * 0.05  # Higher reward for keeping good cards on top
            else:  # Surveil
                # Put directly back on top of library
                player["library"].insert(0, card_id)
                logging.debug(f"Surveil: Kept {card_name} on top")
                reward = 0.05 + card_value * 0.05
                
        elif destination == "bottom":  # Scry only
            context.setdefault("put_on_bottom", []).append(card_id)
            logging.debug(f"Scry: Putting {card_name} on bottom")
            reward = 0.05 - card_value * 0.05  # Lower reward for putting good cards on bottom
            
        elif destination == "graveyard":  # Surveil only
            success_move = gs.move_card(card_id, player, "library_implicit", player, "graveyard", cause="surveil")
            if not success_move:
                logging.error(f"Failed to move {card_name} to graveyard during surveil")
                gs.choice_context = None
                gs.phase = gs.PHASE_PRIORITY
                return -0.1, False
                
            logging.debug(f"Surveil: Put {card_name} into graveyard")
            # Higher reward for putting bad cards in graveyard, but also reward for
            # putting good recursion targets there
            has_recursion = False
            if card and any(x in getattr(card, 'oracle_text', '').lower() for x in 
                        ['from your graveyard', 'from a graveyard']):
                has_recursion = True
                
            reward = 0.05 + (0.05 if has_recursion else -0.05 * card_value)

        # Check if all cards have been processed
        if not context.get("cards"):
            logging.debug(f"{current_choice_type.capitalize()} finished")
            
            # For Scry, finalize the library order
            if current_choice_type == "scry":
                bottom_cards = context.get("put_on_bottom", [])
                top_cards = context.get("kept_on_top", [])
                
                # Apply strategic ordering for top cards (default: keep original order)
                ordered_top_cards = top_cards
                
                # Add cards back to library in the correct order
                player["library"] = ordered_top_cards + player["library"]  # Top cards first
                player["library"].extend(bottom_cards)  # Bottom cards last
                logging.debug(f"Scry final: {len(top_cards)} cards on top, {len(bottom_cards)} on bottom")

            # Clear context and return to previous phase
            previous_phase = getattr(gs, 'previous_priority_phase', gs.PHASE_PRIORITY)
            gs.choice_context = None
            gs.phase = previous_phase
            gs.priority_pass_count = 0
            gs.priority_player = gs._get_active_player()
            
            # Additional reward for completing the full scry/surveil
            reward += 0.05
        
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
    
    def _handle_no_op(self, param, context=None, **kwargs):
            """Handles NO_OP. Checks for stuck state and forces recovery."""
            gs = self.game_state
            logging.debug("Executed NO_OP action.")
            
            # Stuck State Recovery: 
            # If NO_OP executed when priority is None in a playable phase (not Untap/Cleanup),
            # it means the game state has lost track of the active actor.
            if gs.priority_player is None and gs.phase not in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP, gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
                logging.warning("NO_OP executed while priority is None. Attempting recovery.")
                
                # 1. Try standard logic to assign priority
                gs._pass_priority() 
                
                # 2. Force assignment if standard logic skipped it (e.g. due to non-empty stack check failure)
                if gs.priority_player is None:
                    logging.warning("Recovery: Force assigning priority to Active Player.")
                    gs.priority_player = gs._get_active_player()
                    gs.priority_pass_count = 0
                
            return 0.0, True

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
        """Handle transitioning from draw step."""
        gs = self.game_state
        # Actually draw a card for the active player
        active_player = gs._get_active_player()
        if active_player["library"]:
            gs._draw_card(active_player)
        # Transition to main phase
        result, success = self._handle_phase_transition(gs.PHASE_DRAW, gs.PHASE_MAIN_PRECOMBAT, **kwargs)
        return (0.05, success) if success else result, success
        
    def _handle_phase_transition(self, current_phase, target_phase, **kwargs):
        """
        Centralized handler for phase transitions with improved validation and logging.
        
        Args:
            current_phase: Current game phase
            target_phase: Desired phase to transition to
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        
        # Validate current phase
        if gs.phase != current_phase:
            logging.warning(f"Phase transition attempted from incorrect phase. Expected: {current_phase}, Actual: {gs.phase}")
            return -0.1, False
        
        # Check if this transition is valid in the flow of gameplay
        valid_transitions = {
            gs.PHASE_MAIN_PRECOMBAT: [gs.PHASE_BEGIN_COMBAT],
            gs.PHASE_BEGIN_COMBAT: [gs.PHASE_DECLARE_ATTACKERS],
            gs.PHASE_DECLARE_ATTACKERS: [gs.PHASE_DECLARE_BLOCKERS],
            gs.PHASE_DECLARE_BLOCKERS: [gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE],
            gs.PHASE_FIRST_STRIKE_DAMAGE: [gs.PHASE_COMBAT_DAMAGE],
            gs.PHASE_COMBAT_DAMAGE: [gs.PHASE_END_OF_COMBAT],
            gs.PHASE_END_OF_COMBAT: [gs.PHASE_MAIN_POSTCOMBAT],
            gs.PHASE_MAIN_POSTCOMBAT: [gs.PHASE_END_STEP],
            gs.PHASE_END_STEP: [gs.PHASE_CLEANUP],
            gs.PHASE_CLEANUP: [gs.PHASE_UNTAP],
            gs.PHASE_UNTAP: [gs.PHASE_UPKEEP],
            gs.PHASE_UPKEEP: [gs.PHASE_DRAW],
            gs.PHASE_DRAW: [gs.PHASE_MAIN_PRECOMBAT]
        }
        
        if target_phase not in valid_transitions.get(current_phase, []):
            logging.warning(f"Invalid phase transition: {current_phase} -> {target_phase}")
            return -0.05, False
        
        # Perform phase-specific checks
        if target_phase == gs.PHASE_DECLARE_ATTACKERS and current_phase == gs.PHASE_BEGIN_COMBAT:
            # Check if there are potential attackers
            active_player = gs._get_active_player()
            has_creatures = any('creature' in getattr(gs._safe_get_card(cid), 'card_types', []) 
                            for cid in active_player.get("battlefield", []))
            if not has_creatures:
                logging.debug("Skipping attack phase as there are no creatures to attack with")
                # Skip directly to end of combat
                gs.phase = gs.PHASE_END_OF_COMBAT
                return 0.01, True
        
        # Perform the transition
        gs.phase = target_phase
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        
        # Trigger any phase-specific effects
        if hasattr(gs, 'trigger_phase_change'):
            gs.trigger_phase_change(target_phase)
        
        logging.debug(f"Successfully transitioned from {current_phase} to {target_phase}")
        return 0.02, True

    def _handle_main_phase_end(self, param, **kwargs):
        """Handle transitioning from main phase."""
        gs = self.game_state
        if gs.phase == gs.PHASE_MAIN_PRECOMBAT:
            return self._handle_phase_transition(gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_BEGIN_COMBAT, **kwargs)
        elif gs.phase == gs.PHASE_MAIN_POSTCOMBAT:
            return self._handle_phase_transition(gs.PHASE_MAIN_POSTCOMBAT, gs.PHASE_END_STEP, **kwargs)
        else:
            logging.warning(f"MAIN_PHASE_END called during invalid phase: {gs.phase}")
            return -0.1, False

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
        """
        Improved handler for the MULLIGAN action with detailed tracking and error recovery.
        
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        # Use perspective player for the action, ensure they are the current mulligan player
        perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2
        current_mulligan_player = getattr(gs, 'mulligan_player', None)

        if current_mulligan_player != perspective_player:
             logging.warning(f"MULLIGAN action called by {perspective_player.get('name','?')}, but mulligan player is {getattr(current_mulligan_player,'name','None')}.")
             # Do not execute if it's not the correct player's turn to mulligan
             return -0.2, False # Invalid state/action timing

        player = current_mulligan_player # Use the validated player
        # ... (rest of validation logic from the original _handle_mulligan - checks count, hand size etc.) ...
        current_mulls = gs.mulligan_count.get('p1' if player == gs.p1 else 'p2', 0)
        hand_size = len(player.get("hand", []))
        if current_mulls >= 7: return -0.2, False
        if hand_size == 0: return -0.2, False

        result = None
        if hasattr(gs, 'perform_mulligan'):
            result = gs.perform_mulligan(player, keep_hand=False)
        else:
             logging.error("perform_mulligan missing from GameState.")
             return -0.2, False

        # Check result from perform_mulligan
        # perform_mulligan returns True if mulligan taken successfully
        if result is True:
            # Calculate penalty (remains the same)
            penalty = 0.05 * (current_mulls + 1)
            # Evaluate the quality of the hand just rejected
            # --- Hand evaluation needs to happen *before* the mulligan replaces the hand ---
            # This needs careful consideration: evaluate before calling perform_mulligan?
            # Let's skip hand evaluation for now to focus on fixing the flow.
            hand_quality = 0
            # Bigger penalty for mulliganing good hands
            adjusted_penalty = penalty # * (1 + hand_quality)
            logging.debug(f"Mulligan {current_mulls+1}: Penalty {-adjusted_penalty:.2f}")
            return -adjusted_penalty, True # Successful mulligan
        else:
            # If perform_mulligan returns False or None, it failed or was not allowed
            logging.warning(f"Mulligan action failed (perform_mulligan returned {result})")
            return -0.2, False

    def _handle_keep_hand(self, param, **kwargs):
        """
        Improved handler for the KEEP_HAND action with better validation and state transitions.
        
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2
        current_mulligan_player = getattr(gs, 'mulligan_player', None)

        if current_mulligan_player != perspective_player:
             logging.warning(f"KEEP_HAND action called by {perspective_player.get('name','?')}, but mulligan player is {getattr(current_mulligan_player,'name','None')}.")
             return -0.2, False # Invalid state/action timing

        player = current_mulligan_player # Use validated player

        result = None # perform_mulligan returns None for state transition, False for completion+no_draw
        if hasattr(gs, 'perform_mulligan'):
            result = gs.perform_mulligan(player, keep_hand=True)
        else:
            logging.error("perform_mulligan missing from GameState.")
            return -0.2, False

        # Process the result
        # Result = None: State transitioned (switched mulligan player or went to bottoming), requires new action mask. Success = True.
        # Result = False: Mulligan phase finished. Success = True.
        if result is None or result is False:
            # Evaluate hand quality (after keep decision)
            hand_quality = 0
            hand_size = len(player.get("hand", []))
            if self.card_evaluator and hand_size > 0:
                try: # Add try-except around evaluation
                     for card_id in player.get("hand", []):
                         hand_quality += self.card_evaluator.evaluate_card(card_id, "hand_strength") / max(1, hand_size)
                except Exception as e: logging.error(f"Error evaluating hand quality: {e}")

            reward = 0.05 + hand_quality * 0.1
            logging.debug(f"Keep hand: Reward {reward:.2f} (hand quality: {hand_quality:.2f})")
            return reward, True # Action processed successfully
        else: # Should not happen if perform_mulligan returns None/False correctly for keep
            logging.warning(f"Keep hand action failed (perform_mulligan returned unexpected {result})")
            return -0.1, False

    def _handle_bottom_card(self, param, context, **kwargs):
        """
        Improved handler for the BOTTOM_CARD action with better validation, evaluation, and tracking.
        
        Args:
            param: Hand index to bottom
            context: Additional context
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2
        current_bottoming_player = getattr(gs, 'bottoming_player', None)
        hand_idx_to_bottom = param # Param is already the index

        if current_bottoming_player != perspective_player:
            logging.warning(f"BOTTOM_CARD action called by {perspective_player.get('name','?')}, but bottoming player is {getattr(current_bottoming_player,'name','None')}.")
            return -0.2, False # Invalid state/action timing

        player = current_bottoming_player # Use validated player

        # --- Validation (moved from GameState method to Handler) ---
        if not getattr(gs, 'bottoming_in_progress', False):
            logging.warning("BOTTOM_CARD called but not in bottoming phase.")
            return -0.2, False
        if not isinstance(hand_idx_to_bottom, int) or hand_idx_to_bottom < 0:
             logging.warning(f"Invalid hand index for bottoming: {hand_idx_to_bottom}")
             return -0.15, False
        hand = player.get("hand", [])
        if hand_idx_to_bottom >= len(hand):
             logging.warning(f"Hand index {hand_idx_to_bottom} out of bounds (hand size: {len(hand)})")
             return -0.15, False
        # --- End Validation ---

        # Get card and evaluate before bottoming
        card_id = hand[hand_idx_to_bottom]
        card = gs._safe_get_card(card_id)
        card_value = 0
        if self.card_evaluator and card:
            try: # Add try-except around evaluation
                 eval_context = {"hand_size": len(hand), "mulligan_count": gs.mulligan_count.get('p1' if player == gs.p1 else 'p2', 0)}
                 card_value = self.card_evaluator.evaluate_card(card_id, "bottoming", context_details=eval_context)
            except Exception as e: logging.error(f"Error evaluating card for bottoming: {e}")

        # Attempt to bottom the card using game state
        success = False
        if hasattr(gs, 'bottom_card'):
            success = gs.bottom_card(player, hand_idx_to_bottom) # GS method handles the logic and state transitions
        else:
             logging.error("bottom_card method missing from GameState.")
             return -0.2, False

        # Calculate reward/penalty based on card value
        # More penalty for bottoming high-value cards
        reward_mod = -0.01 - (card_value * 0.05)
        # Apply reward based on success
        if success:
            # Check if bottoming is now complete (from GameState's perspective)
            if not gs.bottoming_in_progress and not gs.mulligan_in_progress:
                 final_reward = 0.05 + reward_mod
            elif not gs.bottoming_in_progress and gs.mulligan_in_progress: # Waiting for opponent bottoming
                 final_reward = 0.03 + reward_mod
            else: # More bottoming needed from this player
                 final_reward = 0.02 + reward_mod
            return final_reward, True
        else:
             logging.warning(f"Failed to bottom card at index {hand_idx_to_bottom} (GameState returned False).")
             return -0.05, False

    def _handle_upkeep_pass(self, param, **kwargs):
        """Handle transitioning from upkeep step."""
        gs = self.game_state
        
        # Ensure priority is assigned before transition
        if gs.priority_player is None:
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
            logging.warning("UPKEEP_PASS: Priority was None, assigned to active player")
        
        result, success = self._handle_phase_transition(gs.PHASE_UPKEEP, gs.PHASE_DRAW, **kwargs)
        
        # If transition succeeded, ensure priority is set for draw phase
        if success:
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
        
        return result, success

    def _handle_begin_combat_end(self, param, **kwargs):
        """Handle transitioning from begin combat step."""
        gs = self.game_state
        return self._handle_phase_transition(gs.PHASE_BEGIN_COMBAT, gs.PHASE_DECLARE_ATTACKERS, **kwargs)

    def _handle_end_combat(self, param, **kwargs):
        """Handle transitioning from end of combat step."""
        gs = self.game_state
        return self._handle_phase_transition(gs.PHASE_END_OF_COMBAT, gs.PHASE_MAIN_POSTCOMBAT, **kwargs)

    def _handle_end_step(self, param, **kwargs):
        """Handle transitioning from end step."""
        gs = self.game_state
        # Use phase transition for consistency
        result, success = self._handle_phase_transition(gs.PHASE_END_STEP, gs.PHASE_CLEANUP, **kwargs)
        # Apply end step effects if successful
        if success and hasattr(gs, '_end_step_effects'):
            gs._end_step_effects()
        return result, success

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
         
    def _handle_activate_ability(self, param, context, **kwargs):
        """
        Enhanced and more robust handler for ability activation with improved error handling,
        cost validation, and support for different ability types.
        
        Args:
            param: Not used directly (None from ACTION_MEANINGS)
            context: Must contain 'battlefield_idx' and 'ability_idx'
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        player = gs._get_active_player()
        
        # Get indices from context
        bf_idx = context.get('battlefield_idx')
        ability_idx = context.get('ability_idx')
        
        # Validate context contains needed indices
        if bf_idx is None or ability_idx is None:
            logging.error(f"ACTIVATE_ABILITY missing required indices in context: {context}")
            return -0.15, False
        
        # Validate indices are integers
        if not isinstance(bf_idx, int) or not isinstance(ability_idx, int):
            logging.error(f"ACTIVATE_ABILITY indices must be integers: {context}")
            return -0.15, False
        
        # Validate battlefield index
        if bf_idx >= len(player.get("battlefield", [])):
            logging.warning(f"ACTIVATE_ABILITY: Invalid battlefield index {bf_idx}")
            return -0.2, False
        
        # Get card and validate
        card_id = player["battlefield"][bf_idx]
        card = gs._safe_get_card(card_id)
        
        if not card:
            logging.warning(f"ACTIVATE_ABILITY: Card not found for ID {card_id}")
            return -0.2, False
        
        # Verify AbilityHandler exists
        if not hasattr(gs, 'ability_handler') or not gs.ability_handler:
            logging.error("Cannot activate ability: AbilityHandler not found")
            return -0.15, False
        
        # Get ability and validate index
        activated_abilities = gs.ability_handler.get_activated_abilities(card_id)
        
        if not activated_abilities:
            logging.warning(f"No activated abilities found for {getattr(card, 'name', card_id)}")
            return -0.15, False
        
        if ability_idx >= len(activated_abilities):
            logging.warning(f"Invalid ability index {ability_idx} for {getattr(card, 'name', card_id)}")
            return -0.15, False
        
        ability = activated_abilities[ability_idx]
        internal_idx = getattr(ability, 'activation_index', ability_idx)
        
        # Check if ability is exhausted
        is_exhaust = getattr(ability, 'is_exhaust', False)
        if is_exhaust and gs.check_exhaust_used(card_id, internal_idx):
            logging.debug(f"Cannot activate Exhaust ability for {card.name}: Already used")
            return -0.05, False
        
        # Check if card is tapped (for abilities requiring untapped state)
        is_tap_ability = False
        if hasattr(ability, 'cost') and '{T}' in ability.cost:
            is_tap_ability = True
            if card_id in player.get("tapped_permanents", set()):
                logging.debug(f"Cannot activate tap ability for {card.name}: Already tapped")
                return -0.05, False
        
        # Check for timing issues with sorcery-speed restrictions
        requires_sorcery = "activate only as a sorcery" in getattr(ability, 'effect_text', '').lower()
        if requires_sorcery and not gs._can_act_at_sorcery_speed(player):
            logging.debug(f"Cannot activate sorcery-speed ability now for {card.name}")
            return -0.05, False
        
        # Prepare cost context
        cost_context = {
            "card_id": card_id,
            "card": card,
            "ability": ability,
            "is_ability": True,
            "cause": "ability_activation"
        }
        cost_context.update(context)
        
        # Verify costs can be paid
        cost_str = getattr(ability, 'cost', None)
        if not cost_str:
            logging.error(f"Ability for {card.name} missing 'cost' attribute")
            return -0.15, False
        
        can_pay = gs.mana_system.can_pay_mana_cost(player, cost_str, cost_context) if gs.mana_system else True
        
        if not can_pay:
            logging.debug(f"Cannot afford cost {cost_str} for {card.name} ability {ability_idx}")
            return -0.05, False
        
        # Pay costs
        costs_paid = gs.mana_system.pay_mana_cost(player, cost_str, cost_context) if gs.mana_system else True
        
        if not costs_paid:
            logging.warning(f"Failed to pay cost for {card.name} ability {ability_idx}")
            return -0.05, False
        
        # Mark exhaust used (if applicable)
        if is_exhaust:
            gs.mark_exhaust_used(card_id, internal_idx)
            # Trigger exhaust event
            if gs.ability_handler:
                exhaust_context = {"activator": player, "source_card_id": card_id, "ability_index": internal_idx}
                gs.ability_handler.check_abilities(card_id, "EXHAUST_ABILITY_ACTIVATED", exhaust_context)
                gs.ability_handler.process_triggered_abilities()
        
        # Handle targeting if needed
        effect_text = getattr(ability, 'effect', getattr(ability, 'effect_text', 'Unknown Effect'))
        requires_target = "target" in effect_text.lower()
        
        if requires_target:
            # Set up targeting phase
            if gs.phase not in [gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
                gs.previous_priority_phase = gs.phase
            
            gs.phase = gs.PHASE_TARGETING
            gs.targeting_context = {
                "source_id": card_id,
                "controller": player,
                "effect_text": effect_text,
                "required_type": gs._get_target_type_from_text(effect_text),
                "required_count": 1,
                "min_targets": 1,
                "max_targets": 1,
                "selected_targets": [],
                "stack_info": {
                    "item_type": "ABILITY",
                    "source_id": card_id,
                    "controller": player,
                    "context": {
                        "ability_index": internal_idx,
                        "effect_text": effect_text,
                        "ability": ability,
                        "is_exhaust": is_exhaust,
                        "targets": {}
                    }
                }
            }
            
            # Set priority to choosing player
            gs.priority_player = player
            gs.priority_pass_count = 0
            logging.debug(f"Set up targeting for ability: {effect_text}")
        else:
            # Add directly to stack
            stack_context = {
                "ability_index": internal_idx,
                "effect_text": effect_text,
                "ability": ability,
                "is_exhaust": is_exhaust,
                "targets": {}
            }
            gs.add_to_stack("ABILITY", card_id, player, stack_context)
            logging.debug(f"Added non-targeting ability {internal_idx} for {card.name} to stack")
        
        # Evaluate strategic value of activation
        ability_value = 0
        if self.card_evaluator:
            try:
                ability_value, _ = self.evaluate_ability_activation(card_id, internal_idx)
            except Exception as e:
                logging.error(f"Error evaluating ability activation: {e}")
        
        # Return reward based on strategic value
        return 0.1 + ability_value * 0.4, True

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
        
    def _handle_special_choice_actions(self, param, context, **kwargs):
        """
        Handle special choice actions like mode selection, color selection, X value choice, etc.
        Delegates to specific handlers based on the context type.
        """
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        choice_type = None
        
        # Get choice context
        if not hasattr(gs, 'choice_context') or not gs.choice_context:
            logging.warning("Special choice action called, but no choice context found.")
            return -0.2, False
        
        choice_type = gs.choice_context.get("type")
        
        # Verify player has authority to make this choice
        if gs.choice_context.get("player") != player:
            logging.warning("Received choice action for non-active choice player.")
            return -0.2, False
        
        # Delegate to appropriate choice handler based on type
        if choice_type == "scry" or choice_type == "surveil":
            # Handle scry/surveil choices (PUT_ON_TOP, PUT_ON_BOTTOM, PUT_TO_GRAVEYARD)
            action_index = kwargs.get('action_index')
            
            if action_index == 306:  # PUT_ON_TOP
                return self._handle_scry_surveil_choice(param, context, action_index=306)
            elif action_index == 307:  # PUT_ON_BOTTOM
                if choice_type == "surveil":
                    logging.warning("Cannot PUT_ON_BOTTOM during Surveil choice.")
                    return -0.1, False
                return self._handle_scry_surveil_choice(param, context, action_index=307)
            elif action_index == 305:  # PUT_TO_GRAVEYARD
                if choice_type == "scry":
                    logging.warning("Cannot PUT_TO_GRAVEYARD during Scry choice.")
                    return -0.1, False
                return self._handle_scry_surveil_choice(param, context, action_index=305)
        
        elif choice_type == "dredge":
            # Use the specific dredge handler - Index 308
            return self._handle_dredge(param, context)
        
        elif choice_type == "choose_mode":
            # Handle choosing modes from options - Indices 353-362
            return self._handle_choose_mode(param, context)
        
        elif choice_type == "choose_x":
            # Handle choosing X value - Indices 363-372
            return self._handle_choose_x(param, context)
        
        elif choice_type == "choose_color":
            # Handle choosing color - Indices 373-377
            return self._handle_choose_color(param, context)
        
        elif choice_type == "pay_kicker":
            # Handle kicker payment choice - Indices 405-406
            action_index = kwargs.get('action_index')
            if action_index == 405:  # PAY_KICKER
                return self._handle_pay_kicker(True, context)
            elif action_index == 406:  # DONT_PAY_KICKER
                return self._handle_pay_kicker(False, context)
        
        elif choice_type == "pay_additional":
            # Handle additional cost payment choice - Indices 407-408
            action_index = kwargs.get('action_index')
            if action_index == 407:  # PAY_ADDITIONAL_COST
                return self._handle_pay_additional_cost(True, context)
            elif action_index == 408:  # DONT_PAY_ADDITIONAL_COST
                return self._handle_pay_additional_cost(False, context)
        
        elif choice_type == "pay_escalate":
            # Handle escalate payment - Index 409
            return self._handle_pay_escalate(param, context)
        
        # If we reach here, either the choice type is unrecognized or the action doesn't match the choice
        logging.warning(f"Unhandled special choice type: {choice_type} or mismatched action")
        return -0.1, False

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

        equip_identifier = context.get('equipment_identifier', context.get('equip_identifier'))
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
        # Get equip cost from card text
        equip_cost_str = None
        match = re.search(r"equip (\{[^\}]+\}|[0-9]+)", getattr(equip_card, 'oracle_text', '').lower())
        if match:
            equip_cost_str = match.group(1)
            if equip_cost_str.isdigit(): equip_cost_str = f"{{{equip_cost_str}}}" # Normalize cost

        if not equip_cost_str or not self._can_afford_cost_string(player, equip_cost_str):
            logging.debug(f"Cannot afford equip cost {equip_cost_str or 'N/A'} for {getattr(equip_card, 'name', equip_id)}")
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
        """Handle turning morphed card face up. Expects battlefield_idx in context."""
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
                # GS method checks if morphed, face down, and pays cost
                success = gs.turn_face_up(player, card_id, pay_morph_cost=True)
                return (0.3, success) if success else (-0.1, False)
            else: logging.warning(f"Morph index out of bounds: {card_idx}")
        else: logging.warning(f"Morph context missing 'battlefield_idx'")
        return (-0.15, False)

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
        """
        Improved handler for alternative casting methods with better organization.
        
        Args:
            param: Action parameter value
            action_type: The specific alternative casting action type
            context: Additional context for the casting
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if context is None: context = {}
        if kwargs.get('context'): context.update(kwargs['context'])
        
        # Common casting info for all alternative methods
        casting_info = {
            "CAST_WITH_FLASHBACK": {
                "source_zone": "graveyard",
                "index_key": "gy_idx",
                "cost_pattern": r"flashback (\{[^\}]+\})",
                "timing_check": lambda card: True  # Flashback follows the timing of the card
            },
            "CAST_WITH_JUMP_START": {
                "source_zone": "graveyard",
                "index_key": "gy_idx",
                "cost_pattern": None,  # Uses original cost
                "requires_discard": True,
                "timing_check": lambda card: True  # Jump-start follows the timing of the card
            },
            "CAST_WITH_ESCAPE": {
                "source_zone": "graveyard",
                "index_key": "gy_idx",
                "cost_pattern": r"escape—([^\,]+)",
                "additional_pattern": r"exile ([^\.]+)",
                "timing_check": lambda card: True  # Escape follows the timing of the card
            },
            "CAST_FOR_MADNESS": {
                "source_zone": "exile",
                "index_key": "exile_idx",
                "cost_pattern": r"madness (\{[^\}]+\}|[0-9]+)",
                "timing_check": lambda card: True  # Madness follows the timing of the card
            },
            "CAST_WITH_OVERLOAD": {
                "source_zone": "hand",
                "index_key": "hand_idx",
                "cost_pattern": r"overload (\{[^\}]+\})",
                "timing_check": lambda card: True  # Overload follows the timing of the card
            },
            "CAST_FOR_EMERGE": {
                "source_zone": "hand",
                "index_key": "hand_idx",
                "cost_pattern": r"emerge (\{[^\}]+\})",
                "requires_sacrifice": True,
                "timing_check": lambda card: 'sorcery' in getattr(card, 'card_types', []) or 
                                        not ('instant' in getattr(card, 'card_types', []))
            },
            "CAST_FOR_DELVE": {
                "source_zone": "hand",
                "index_key": "hand_idx",
                "cost_pattern": None,  # Uses original cost with delve reduction
                "timing_check": lambda card: True  # Delve follows the timing of the card
            },
            "AFTERMATH_CAST": {
                "source_zone": "graveyard",
                "index_key": "gy_idx",
                "cast_right_half": True,
                "timing_check": lambda card: True  # Aftermath follows the timing of the card's right half
            }
        }
        
        # Retrieve casting configuration
        if action_type not in casting_info:
            logging.error(f"Unsupported alternative casting type: {action_type}")
            return -0.2, False
        
        cast_config = casting_info[action_type]
        source_zone = cast_config["source_zone"]
        index_key = cast_config["index_key"]
        
        # Get card ID based on context or param
        card_id = None
        
        # Special case for madness which uses card_id directly from context
        if action_type == "CAST_FOR_MADNESS" and "card_id" in context:
            card_id = context["card_id"]
        else:
            # Otherwise get index from context or param
            idx = context.get(index_key)
            if idx is None:
                # If no index in context, try to use param as index
                if isinstance(param, int):
                    idx = param
                else:
                    logging.error(f"{action_type} missing required {index_key} in context: {context}")
                    return -0.15, False
            
            # Validate index
            if idx >= len(player.get(source_zone, [])):
                logging.error(f"{action_type}: Invalid {index_key} {idx} (max: {len(player.get(source_zone, []))-1})")
                return -0.1, False
            
            card_id = player[source_zone][idx]
            # Store index in context for downstream handlers
            context['source_idx'] = idx
        
        # Get card and validate
        card = gs._safe_get_card(card_id)
        if not card:
            logging.error(f"{action_type}: Card {card_id} not found")
            return -0.15, False
        
        # Set up context for alternative casting
        context["source_zone"] = source_zone
        context["use_alt_cost"] = action_type.replace('CAST_WITH_', '').replace('CAST_FOR_', '').replace('CAST_', '').replace('_', ' ').lower()
        
        # Handle special card-half logic
        if cast_config.get("cast_right_half"):
            context["cast_right_half"] = True
        
        # Handle additional costs and requirements
        if cast_config.get("requires_discard"):
            if "discard_idx" not in context:
                logging.error(f"{action_type} requires 'discard_idx' in context")
                return -0.1, False
            
            discard_idx = context["discard_idx"]
            if discard_idx >= len(player.get("hand", [])):
                logging.error(f"{action_type}: Invalid discard_idx {discard_idx}")
                return -0.1, False
            
            context["additional_cost_to_pay"] = {"type": "discard", "hand_idx": discard_idx}
        
        if cast_config.get("requires_sacrifice"):
            if "sacrifice_idx" not in context:
                logging.error(f"{action_type} requires 'sacrifice_idx' in context")
                return -0.1, False
            
            sac_idx = context["sacrifice_idx"]
            if sac_idx >= len(player.get("battlefield", [])):
                logging.error(f"{action_type}: Invalid sacrifice_idx {sac_idx}")
                return -0.1, False
            
            sac_id = player["battlefield"][sac_idx]
            sac_card = gs._safe_get_card(sac_id)
            if not sac_card or 'creature' not in getattr(sac_card, 'card_types', []):
                logging.error(f"{action_type} sacrifice target must be a creature")
                return -0.1, False
            
            context["additional_cost_to_pay"] = {"type": "sacrifice", "target_id": sac_id}
            context["sacrificed_cmc"] = getattr(sac_card, 'cmc', 0)
        
        # Handle escape exile requirements
        if action_type == "CAST_WITH_ESCAPE":
            if "gy_indices_escape" not in context or not isinstance(context["gy_indices_escape"], list):
                logging.error(f"{action_type} requires 'gy_indices_escape' list in context")
                return -0.1, False
            
            exile_req_str = None
            pattern = cast_config.get("additional_pattern")
            if pattern:
                match = re.search(pattern, getattr(card, 'oracle_text', '').lower())
                if match: exile_req_str = match.group(1).strip()
            
            required_exile_count = self._word_to_number(re.search(r"(\w+|\d+)", exile_req_str).group(1)) if exile_req_str else 0
            if required_exile_count <= 0: required_exile_count = 5  # Default if not specified
            
            actual_gy_indices = [idx for idx in context["gy_indices_escape"] if idx < len(player.get("graveyard", []))]
            if len(actual_gy_indices) < required_exile_count:
                logging.warning(f"{action_type}: Not enough valid graveyard cards to exile ({len(actual_gy_indices)}/{required_exile_count})")
                return -0.1, False
            
            context["additional_cost_to_pay"] = {"type": "escape_exile", "gy_indices": actual_gy_indices[:required_exile_count]}
        
        # Handle delve cost reduction
        if action_type == "CAST_FOR_DELVE":
            if "gy_indices" not in context or not isinstance(context["gy_indices"], list):
                logging.error(f"{action_type} requires 'gy_indices' list in context")
                return -0.1, False
            
            actual_gy_indices = [idx for idx in context["gy_indices"] if idx < len(player.get("graveyard", []))]
            context["additional_cost_to_pay"] = {"type": "delve", "gy_indices": actual_gy_indices}
            context["delve_count"] = len(actual_gy_indices)
        
        # Cast the spell using game state
        success = gs.cast_spell(card_id, player, context=context)
        
        if success:
            # Clear madness state after successful cast
            if action_type == "CAST_FOR_MADNESS" and hasattr(gs, 'madness_cast_available') and gs.madness_cast_available and gs.madness_cast_available.get('card_id') == card_id:
                gs.madness_cast_available = None
                logging.debug(f"Madness state cleared after successful cast of {card.name}")
            
            # Calculate reward based on card value
            card_value = 0.3  # Base value for successful alternative cast
            if self.card_evaluator:
                eval_context = {"situation": f"cast_{context['use_alt_cost']}"}
                eval_context.update(context)
                card_value += self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context) * 0.2
            
            return card_value, True
        else:
            logging.warning(f"{action_type} failed for {getattr(card, 'name', card_id)}. Handled by gs.cast_spell.")
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
         return ExtendedCombatResolver._check_block_restrictions(self, blocker_id, attacker_id)

    def _add_state_change_rewards(self, base_reward, previous_state, current_state):
        """Calculate rewards based on positive changes in game state with improved strategic weighting."""
        reward = base_reward
        
        # Life total rewards with scaling factors - more important at lower life totals
        my_life_change = current_state["my_life"] - previous_state["my_life"]
        opp_life_change = previous_state["opp_life"] - current_state["opp_life"]
        
        # Life change is more important when life is low
        my_life_factor = 0.03 * (1 + max(0, (20 - current_state["my_life"])) / 10)
        opp_life_factor = 0.05 * (1 + max(0, (current_state["opp_life"] - 1)) / 10)
        
        reward += my_life_change * my_life_factor
        reward += opp_life_change * opp_life_factor
        
        # Card advantage with higher weighting
        my_hand_change = current_state["my_hand"] - previous_state["my_hand"]
        opp_hand_change = current_state["opp_hand"] - previous_state["opp_hand"]
        card_adv_change = my_hand_change - opp_hand_change
        reward += card_adv_change * 0.15  # Card advantage is very important
        
        # Board presence - separate into creatures and non-creatures if possible
        my_board_change = current_state["my_board"] - previous_state["my_board"]
        opp_board_change = current_state["opp_board"] - previous_state["opp_board"]
        board_adv_change = my_board_change - opp_board_change
        reward += board_adv_change * 0.08
        
        # Power advantage with improved scaling
        my_power_change = current_state["my_power"] - previous_state["my_power"]
        opp_power_change = current_state["opp_power"] - previous_state["opp_power"]
        power_adv_change = my_power_change - opp_power_change
        
        # Power value scales with how far ahead/behind you are
        power_factor = 0.03
        if current_state["my_power"] > current_state["opp_power"]:
            # Increasing a winning board is good
            power_factor = 0.04
        elif current_state["my_power"] < current_state["opp_power"]:
            # Catching up from behind is very good
            power_factor = 0.05
        
        reward += power_adv_change * power_factor
        
        # Log detailed reward breakdown if significant change
        if abs(reward - base_reward) > 0.01:
            logging.debug(f"State Change Reward: Life: {(my_life_change * my_life_factor + opp_life_change * opp_life_factor):.2f}, "
                        f"Cards: {(card_adv_change * 0.15):.2f}, Board: {(board_adv_change * 0.08):.2f}, "
                        f"Power: {(power_adv_change * power_factor):.2f}")
        
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
        """Add actions for token creation and copying."""
        gs = self.game_state
        
        # Check for cards or effects that can create tokens
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                
                # CREATE_TOKEN actions (indices 410-414)
                if "create a token" in oracle_text and not card_id in player.get("tapped_permanents", set()):
                    # Check for activated ability that creates tokens
                    create_pattern = re.search(r"\{[^\}]+\}:.*?create a", oracle_text)
                    if create_pattern:
                        # Determine token type (up to 5 predefined types)
                        token_types = ["creature", "treasure", "clue", "food", "blood"]
                        for idx, token_type in enumerate(token_types):
                            if token_type in oracle_text:
                                token_context = {'battlefield_idx': i, 'token_type': idx}
                                set_valid_action(410 + idx, f"CREATE_{token_type.upper()}_TOKEN with {card.name}", context=token_context)
                                break
                
                # COPY_PERMANENT action (index 415)
                if "copy target" in oracle_text and "permanent" in oracle_text and not card_id in player.get("tapped_permanents", set()):
                    # Check for activated ability
                    copy_pattern = re.search(r"\{[^\}]+\}:.*?copy target", oracle_text)
                    if copy_pattern:
                        # Find valid targets to copy
                        for target_idx, target_id in enumerate(player["battlefield"]):
                            if target_id != card_id:  # Can't copy itself
                                target_card = gs._safe_get_card(target_id)
                                if target_card:
                                    copy_context = {'battlefield_idx': i, 'target_identifier': target_id}
                                    set_valid_action(415, f"COPY_PERMANENT {target_card.name}", context=copy_context)
                                    break  # Just one action is enough, context will specify target
                
                # COPY_SPELL action (index 416)
                if "copy target" in oracle_text and "spell" in oracle_text and not card_id in player.get("tapped_permanents", set()):
                    # Check for activated ability
                    copy_pattern = re.search(r"\{[^\}]+\}:.*?copy target", oracle_text)
                    if copy_pattern and gs.stack:
                        # Find valid spells on stack
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and item[0] == "SPELL" and item[2] != player:
                                spell_id = item[1]
                                spell = gs._safe_get_card(spell_id)
                                if spell:
                                    copy_context = {'battlefield_idx': i, 'target_stack_identifier': stack_idx}
                                    set_valid_action(416, f"COPY_SPELL {spell.name}", context=copy_context)
                                    break
                
                # POPULATE action (index 417)
                if "populate" in oracle_text and not card_id in player.get("tapped_permanents", set()):
                    # Check for activated ability
                    populate_pattern = re.search(r"\{[^\}]+\}:.*?populate", oracle_text)
                    if populate_pattern:
                        # Find valid token creatures to copy
                        has_token = False
                        for token_idx, token_id in enumerate(player["battlefield"]):
                            token_card = gs._safe_get_card(token_id)
                            if token_card and getattr(token_card, 'is_token', False) and 'creature' in getattr(token_card, 'card_types', []):
                                populate_context = {'battlefield_idx': i, 'target_token_identifier': token_id}
                                set_valid_action(417, f"POPULATE to copy {token_card.name}", context=populate_context)
                                has_token = True
                                break
                        
                        if not has_token:
                            # Can't populate without token creatures
                            continue

    def _add_specific_mechanics_actions(self, player, valid_actions, set_valid_action, is_sorcery_speed):
        """Add actions for specialized MTG mechanics, considering timing."""
        gs = self.game_state
        
        # --- Investigate (Action 418) ---
        # Check for cards with "Investigate" as an activated ability
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "investigate" in card.oracle_text.lower():
                # Check if the card has an activated ability that causes investigation
                investigate_pattern = re.search(r"\{[^\}]+\}:.*?investigate", getattr(card, 'oracle_text', '').lower())
                if investigate_pattern and not card_id in player.get("tapped_permanents", set()):
                    # Only add if we can pay the cost (simplified check)
                    investigate_context = {'battlefield_idx': i}
                    set_valid_action(418, f"INVESTIGATE with {card.name}", context=investigate_context)

        # --- Foretell (Action 419 - Sorcery speed only) ---
        if is_sorcery_speed:
            for i in range(min(len(player.get("hand",[])), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "foretell" in card.oracle_text.lower():
                    # Foretell cost is always {2} mana
                    if self._can_afford_cost_string(player, "{2}"):
                        context = {'hand_idx': i}
                        set_valid_action(419, f"FORETELL {card.name}", context=context)

        # --- Amass (Action 420) ---
        # Check for cards with "Amass" as an activated ability
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "amass" in card.oracle_text.lower():
                amass_pattern = re.search(r"\{[^\}]+\}:.*?amass (\d+)", getattr(card, 'oracle_text', '').lower())
                if amass_pattern and not card_id in player.get("tapped_permanents", set()):
                    amount = int(amass_pattern.group(1)) if amass_pattern.group(1).isdigit() else 1
                    amass_context = {'battlefield_idx': i, 'amount': amount}
                    set_valid_action(420, f"AMASS {amount} with {card.name}", context=amass_context)

        # --- Learn (Action 421) ---
        # Adding this if there's a "Learn" trigger waiting for resolution
        if hasattr(gs, 'learn_pending') and gs.learn_pending and gs.learn_pending.get('player') == player:
            set_valid_action(421, "LEARN (Draw and discard or get Lesson)")

        # --- Venture (Action 422) ---
        # Check for cards with "Venture into the dungeon" as an activated ability
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "venture into the dungeon" in card.oracle_text.lower():
                venture_pattern = re.search(r"\{[^\}]+\}:.*?venture", getattr(card, 'oracle_text', '').lower())
                if venture_pattern and not card_id in player.get("tapped_permanents", set()):
                    venture_context = {'battlefield_idx': i}
                    set_valid_action(422, f"VENTURE with {card.name}", context=venture_context)

        # --- Exert (Action 423) ---
        # Only available during combat for attackers
        if gs.phase == gs.PHASE_DECLARE_ATTACKERS:
            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "exert" in card.oracle_text.lower():
                    # Only for creatures that can attack and aren't already being exerted
                    if 'creature' in getattr(card, 'card_types', []) and card_id not in player.get("tapped_permanents", set()):
                        if not hasattr(gs, 'exerted_this_combat') or card_id not in gs.exerted_this_combat:
                            exert_context = {'creature_idx': i}
                            set_valid_action(423, f"EXERT {card.name}", context=exert_context)

        # --- Explore (Action 424) ---
        # Check for cards with "Explore" as an activated ability
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "explore" in card.oracle_text.lower():
                explore_pattern = re.search(r"\{[^\}]+\}:.*?explore", getattr(card, 'oracle_text', '').lower())
                if explore_pattern and not card_id in player.get("tapped_permanents", set()):
                    explore_context = {'creature_idx': i}
                    set_valid_action(424, f"EXPLORE with {card.name}", context=explore_context)

        # --- Adapt (Action 425 - Sorcery speed) ---
        if is_sorcery_speed:
            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "adapt " in card.oracle_text.lower():
                    match = re.search(r"\{[^\}]+\}:.*?adapt (\d+)", card.oracle_text.lower())
                    adapt_n = int(match.group(1)) if match and match.group(1).isdigit() else 1
                    
                    # Check if creature already has +1/+1 counters (can't adapt if it does)
                    has_counters = False
                    if hasattr(card, 'counters') and getattr(card, 'counters', {}).get('+1/+1', 0) > 0:
                        has_counters = True
                    
                    if not has_counters and self._can_afford_card(player, card):
                        adapt_context = {'creature_idx': i, 'amount': adapt_n}
                        set_valid_action(425, f"ADAPT {adapt_n} for {card.name}", context=adapt_context)

        # --- Mutate (Action 426 - Sorcery speed) ---
        if is_sorcery_speed:
            # Check for mutate cards in hand
            for hand_idx, card_id in enumerate(player["hand"][:8]):
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "mutate " in card.oracle_text.lower():
                    # Check for valid targets on the battlefield (non-Human creatures)
                    has_valid_target = False
                    for target_idx, target_id in enumerate(player["battlefield"]):
                        target_card = gs._safe_get_card(target_id)
                        if (target_card and 'creature' in getattr(target_card, 'card_types', []) and 
                                'human' not in getattr(target_card, 'subtypes', [])):
                            has_valid_target = True
                            break
                    
                    if has_valid_target:
                        # Extract mutate cost
                        match = re.search(r"mutate (\{[^\}]+\})", card.oracle_text.lower())
                        mutate_cost = match.group(1) if match else None
                        
                        if mutate_cost and self._can_afford_cost_string(player, mutate_cost):
                            mutate_context = {'hand_idx': hand_idx}
                            set_valid_action(426, f"MUTATE {card.name}", context=mutate_context)

        # --- Cycling (Action 427 - Instant speed) ---
        if not is_sorcery_speed:  # Only at instant speed
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "cycling" in card.oracle_text.lower():
                    cycling_match = re.search(r"cycling (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                    if cycling_match:
                        cost_str = cycling_match.group(1)
                        if cost_str.isdigit(): 
                            cost_str = f"{{{cost_str}}}"
                        
                        if self._can_afford_cost_string(player, cost_str):
                            cycling_context = {'hand_idx': i}
                            set_valid_action(427, f"CYCLING {card.name}", context=cycling_context)

        # --- Goad (Action 428) ---
        # Check for cards that can goad
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "goad" in card.oracle_text.lower():
                goad_pattern = re.search(r"\{[^\}]+\}:.*?goad", getattr(card, 'oracle_text', '').lower())
                if goad_pattern and not card_id in player.get("tapped_permanents", set()):
                    # Check if there are valid targets (opponent's creatures)
                    opponent = gs.p2 if player == gs.p1 else gs.p1
                    valid_targets = [
                        idx for idx, creature_id in enumerate(opponent.get("battlefield", []))
                        if 'creature' in getattr(gs._safe_get_card(creature_id), 'card_types', [])
                    ]
                    
                    if valid_targets:
                        goad_context = {'battlefield_idx': i}
                        set_valid_action(428, f"GOAD with {card.name}", context=goad_context)

        # --- Boast (Action 429 - Only after attacking) ---
        if gs.phase >= gs.PHASE_DECLARE_ATTACKERS:  # After declare attackers phase
            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                if card_id in getattr(gs, 'attackers_this_turn', set()):  # Check if it attacked
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'oracle_text') and "boast —" in card.oracle_text.lower():
                        # Check if already boasted this turn
                        if not hasattr(gs, 'boast_activated') or card_id not in gs.boast_activated:
                            boast_context = {'creature_idx': i}
                            set_valid_action(429, f"BOAST with {card.name}", context=boast_context)
        
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
        """Add actions for zone movement."""
        gs = self.game_state
        
        # RETURN_FROM_GRAVEYARD actions (indices 335-340)
        for i, card_id in enumerate(player.get("graveyard", [])[:6]):  # Limit to first 6
            card = gs._safe_get_card(card_id)
            if not card: continue
            
            # Check if a card in hand or on battlefield can return this card
            can_return = False
            return_source = None
            
            # Check hand for cards that can return from graveyard
            for hand_card_id in player.get("hand", []):
                hand_card = gs._safe_get_card(hand_card_id)
                if hand_card and hasattr(hand_card, 'oracle_text') and "return target" in hand_card.oracle_text.lower() and "from your graveyard" in hand_card.oracle_text.lower():
                    # Determine if this card is a valid target based on type
                    card_type_pattern = re.search(r"return target ([a-z]+) card from your graveyard", hand_card.oracle_text.lower())
                    if card_type_pattern:
                        required_type = card_type_pattern.group(1)
                        if required_type in getattr(card, 'card_types', []) or required_type in getattr(card, 'subtypes', []):
                            can_return = True
                            return_source = hand_card.name
                            break
                    else:
                        can_return = True  # No type restriction found
                        return_source = hand_card.name
                        break
            
            if can_return:
                context = {'gy_idx': i, 'source': return_source}
                set_valid_action(335 + i, f"RETURN_FROM_GRAVEYARD {card.name}", context=context)
        
        # REANIMATE actions (indices 341-346)
        for i, card_id in enumerate(player.get("graveyard", [])[:6]):  # Limit to first 6
            card = gs._safe_get_card(card_id)
            if not card or 'creature' not in getattr(card, 'card_types', []): continue
            
            # Check if a card in hand or on battlefield can reanimate this creature
            can_reanimate = False
            reanimate_source = None
            
            # Check hand for cards that can reanimate
            for hand_card_id in player.get("hand", []):
                hand_card = gs._safe_get_card(hand_card_id)
                if hand_card and hasattr(hand_card, 'oracle_text') and ("return target creature" in hand_card.oracle_text.lower() and "to the battlefield" in hand_card.oracle_text.lower()):
                    can_reanimate = True
                    reanimate_source = hand_card.name
                    break
            
            if can_reanimate:
                context = {'gy_idx': i, 'source': reanimate_source}
                set_valid_action(341 + i, f"REANIMATE {card.name}", context=context)
        
        # RETURN_FROM_EXILE actions (indices 347-352)
        for i, card_id in enumerate(player.get("exile", [])[:6]):  # Limit to first 6
            card = gs._safe_get_card(card_id)
            if not card: continue
            
            # Check if a card in hand or on battlefield can return this card from exile
            can_return_from_exile = False
            return_exile_source = None
            
            # Check hand for cards that can return from exile
            for hand_card_id in player.get("hand", []):
                hand_card = gs._safe_get_card(hand_card_id)
                if hand_card and hasattr(hand_card, 'oracle_text') and "return target" in hand_card.oracle_text.lower() and "from exile" in hand_card.oracle_text.lower():
                    can_return_from_exile = True
                    return_exile_source = hand_card.name
                    break
            
            if can_return_from_exile:
                context = {'exile_idx': i, 'source': return_exile_source}
                set_valid_action(347 + i, f"RETURN_FROM_EXILE {card.name}", context=context)
            
    def _add_alternative_casting_actions(self, player, valid_actions, set_valid_action, is_sorcery_speed):
        """Add actions for alternative casting costs."""
        gs = self.game_state
        # Flashback
        for i in range(min(len(player.get("graveyard",[])), 6)):
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "flashback" in card.oracle_text.lower():
                is_instant = 'instant' in getattr(card, 'card_types', [])
                if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"flashback (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                        # Context needs gy_idx
                        context = {'gy_idx': i}
                        # FIXED: Use correct action ID for CAST_WITH_FLASHBACK (398)
                        set_valid_action(398, f"CAST_WITH_FLASHBACK {card.name}", context=context)

        # Jump-start
        for i in range(min(len(player.get("graveyard",[])), 6)):
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "jump-start" in card.oracle_text.lower():
                is_instant = 'instant' in getattr(card, 'card_types', [])
                if is_sorcery_speed or is_instant: # Check timing
                    if len(player["hand"]) > 0 and self._can_afford_card(player, card):
                        # FIXED: Use correct action ID for CAST_WITH_JUMP_START (399)
                        context = {'gy_idx': i}
                        set_valid_action(399, f"CAST_WITH_JUMP_START {card.name}", context=context)

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
                            # FIXED: Use correct action ID for CAST_WITH_ESCAPE (400)
                            context = {'gy_idx': i}
                            set_valid_action(400, f"CAST_WITH_ESCAPE {card.name}", context=context)

        # Madness (Triggered when discarded, check if castable)
        if hasattr(gs, 'madness_cast_available') and gs.madness_cast_available:
            madness_info = gs.madness_cast_available
            if madness_info['player'] == player:
                card_id = madness_info['card_id']
                cost_str = madness_info['cost']
                card = gs._safe_get_card(card_id)

                # Find the card in exile
                exile_idx = -1
                for idx, exiled_id in enumerate(player.get("exile", [])):
                    if exiled_id == card_id:
                        exile_idx = idx
                        break

                # Check affordability
                if exile_idx != -1 and self._can_afford_cost_string(player, cost_str):
                    # FIXED: Use correct action ID for CAST_FOR_MADNESS (401)
                    context = {'exile_idx': exile_idx, 'card_id': card_id}
                    set_valid_action(401, f"CAST_FOR_MADNESS {card.name if card else card_id}", context=context)

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
        """Add options for paying kicker and related additional costs."""
        gs = self.game_state
        
        # Check for pending spell context that might need kicker decisions
        pending_context = getattr(gs, 'pending_spell_context', None)
        if pending_context and pending_context.get('card_id') and pending_context.get('controller') == player:
            card_id = pending_context['card_id']
            card = gs._safe_get_card(card_id)
            
            # Kicker options - Use correct indices 405 and 406
            if card and hasattr(card, 'oracle_text') and "kicker" in card.oracle_text.lower():
                kicker_match = re.search(r"kicker (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                if kicker_match:
                    cost_str = kicker_match.group(1)
                    if cost_str.isdigit(): 
                        cost_str = f"{{{cost_str}}}"
                    
                    # Only show PAY_KICKER if it's affordable
                    if self._can_afford_cost_string(player, cost_str, context=pending_context):
                        set_valid_action(405, f"PAY_KICKER for {card.name}")
                    
                    # Always allow NOT paying kicker (it's optional)
                    set_valid_action(406, f"DONT_PAY_KICKER for {card.name}")
            
            # Additional Cost options - Use correct indices 407 and 408
            if card and hasattr(card, 'oracle_text') and "additional cost" in card.oracle_text.lower():
                # Parse the additional cost to determine if it's optional or mandatory
                cost_info = self._get_additional_cost_info(card)
                is_optional = cost_info.get("optional", True) if cost_info else True
                
                # Only show PAY_ADDITIONAL_COST if it's payable
                if cost_info and self._can_pay_specific_additional_cost(player, cost_info, pending_context):
                    set_valid_action(407, f"PAY_ADDITIONAL_COST for {card.name}")
                
                # Only show DON'T_PAY option if the cost is optional
                if is_optional:
                    set_valid_action(408, f"DONT_PAY_ADDITIONAL_COST for {card.name}")
            
            # Escalate options - Use correct index 409
            if card and hasattr(card, 'oracle_text') and "escalate" in card.oracle_text.lower():
                escalate_match = re.search(r"escalate (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                if escalate_match:
                    cost_str = escalate_match.group(1)
                    if cost_str.isdigit(): 
                        cost_str = f"{{{cost_str}}}"
                    
                    # Extract info about available modes
                    num_modes = 0
                    if hasattr(card, 'modes'):
                        num_modes = len(card.modes)
                    elif "choose one" in card.oracle_text.lower():
                        num_modes = card.oracle_text.lower().count("•")
                    
                    # Only show PAY_ESCALATE if more than one mode exists and cost is affordable
                    if num_modes > 1 and self._can_afford_cost_string(player, cost_str, context=pending_context):
                        # For each possible extra mode (up to 3)
                        for extra_modes in range(1, min(num_modes, 4)):
                            # Check if we can afford the cost multiple times
                            if self._can_afford_cost_string(player, f"{cost_str}*{extra_modes}", context=pending_context):
                                escalate_context = {'num_extra_modes': extra_modes}
                                set_valid_action(409, f"PAY_ESCALATE for {extra_modes} extra mode(s)", context=escalate_context)
                                break  # Just add one action; context will specify how many modes
        
        # Check spells currently on the stack that belong to the player
        # This handles cases where the spell is already on stack but needs kicker decision
        for item in gs.stack:
            if isinstance(item, tuple) and len(item) >= 3:
                stack_type, card_id, controller = item[:3]
                if stack_type == "SPELL" and controller == player:
                    card = gs._safe_get_card(card_id)
                    context = item[3] if len(item) > 3 else {}
                    
                    # Check if this spell is waiting for kicker decision
                    if context.get('waiting_for_kicker_choice'):
                        # Similar logic as above, but for stack items
                        if card and hasattr(card, 'oracle_text') and "kicker" in card.oracle_text.lower():
                            kicker_match = re.search(r"kicker (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                            if kicker_match and self._can_afford_cost_string(player, kicker_match.group(1), context=context):
                                set_valid_action(405, f"PAY_KICKER for {card.name}")
                            set_valid_action(406, f"DONT_PAY_KICKER for {card.name}")

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
                
                # Left half casting - Use correct action index 445
                if has_left_half:
                    left_cost = card.left_half.get('mana_cost', card.mana_cost)
                    can_afford_left = self._can_afford_cost_string(player, left_cost)
                    
                    if can_afford_left:
                        context = {'hand_idx': idx}
                        set_valid_action(445, f"CAST_LEFT_HALF of {card.name}", context=context)
                
                # Right half casting - Use correct action index 446
                if has_right_half:
                    right_cost = card.right_half.get('mana_cost', card.mana_cost)
                    can_afford_right = self._can_afford_cost_string(player, right_cost)
                    
                    if can_afford_right:
                        context = {'hand_idx': idx}
                        set_valid_action(446, f"CAST_RIGHT_HALF of {card.name}", context=context)
                
                # Fuse (both halves) - Use correct action index 447
                if has_left_half and has_right_half and "fuse" in getattr(card, 'oracle_text', '').lower():
                    # Need to afford both costs
                    left_cost = card.left_half.get('mana_cost', card.mana_cost)
                    right_cost = card.right_half.get('mana_cost', card.mana_cost)
                    
                    # This is simplistic; a real implementation should combine costs correctly
                    total_cost = left_cost + right_cost  
                    can_afford_both = self._can_afford_cost_string(player, total_cost)
                    
                    if can_afford_both:
                        context = {'hand_idx': idx}
                        set_valid_action(447, f"CAST_FUSE of {card.name}", context=context)
            
            # Check if it's an aftermath card
            is_aftermath = False
            if card and hasattr(card, 'layout'):
                is_aftermath = card.layout == "aftermath"
            elif card and hasattr(card, 'oracle_text') and "aftermath" in card.oracle_text.lower():
                is_aftermath = True
            
            # Add aftermath actions for graveyard - Use correct action index 448
            if is_aftermath:
                for g_idx, g_card_id in enumerate(player["graveyard"][:6]):  # First 6 in graveyard
                    g_card = gs._safe_get_card(g_card_id)
                    if g_card and hasattr(g_card, 'layout') and g_card.layout == "aftermath":
                        # Check if it has a castable aftermath half
                        if hasattr(g_card, 'right_half'):
                            right_cost = g_card.right_half.get('mana_cost', g_card.mana_cost)
                            can_afford = self._can_afford_cost_string(player, right_cost)
                            
                            if can_afford:
                                context = {'gy_idx': g_idx}
                                set_valid_action(448, f"AFTERMATH_CAST of {g_card.name}", context=context)

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
    