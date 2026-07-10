#actions.py

import logging
import re
import numpy as np
from collections import defaultdict
from .card import Card
from .enhanced_combat import ExtendedCombatResolver
from .combat_integration import integrate_combat_actions
from .debug import debug_log_valid_actions 
from .enhanced_card_evaluator import EnhancedCardEvaluator

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

    # Plot hand slots 0-2. Remaining hand slots use 309-313.
    **{296 + i: ("PLOT_CARD", i) for i in range(3)},

    # Library/Card Movement (299-308) = 10 actions
    # Param = Choice index 0-4 for specific library search
    **{299 + i: ("SEARCH_LIBRARY", i) for i in range(5)}, # 299-303
    304: ("NO_OP_SEARCH_FAIL", None), # Unused action? Handler returns success even on fail. NO_OP.
    305: ("PUT_TO_GRAVEYARD", None), # Surveil Choice - Contextual
    306: ("PUT_ON_TOP", None), # Scry/Surveil Choice - Contextual
    307: ("PUT_ON_BOTTOM", None), # Scry Choice - Contextual
    308: ("DREDGE", None), # Param = GY index 0-5? Or Contextual? Use context.

    # Plot hand slots 3-7.
    **{309 + i: ("PLOT_CARD", i + 3) for i in range(5)},

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
    426: ("MUTATE", None), # Casting cost. Target and position use later choice contexts.
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

    # Level up a leveler creature (467-471) = 5 actions (CR 711)
    # Param = battlefield index 0-4 of the leveler. Distinct from LEVEL_UP_CLASS
    # (Class enchantments, 253-257); levelers pay a repeatable "Level up {cost}".
    **{467 + i: ("LEVEL_UP_CREATURE", i) for i in range(5)}, # 467-471

    # Wrenn emblem: play/cast graveyard permanent by relative index 0-5.
    **{472 + i: ("PLAY_FROM_GRAVEYARD", i) for i in range(6)}, # 472-477
    478: ("SADDLE", None), # Context={'battlefield_idx': X}
    479: ("TARGET_PAGE_NEXT", None)
}
# Ensure size is correct after updates
if len(ACTION_MEANINGS) != 480:
    raise ValueError(f"ACTION_MEANINGS size is WRONG after update: {len(ACTION_MEANINGS)} expected 480")

from .actions_space import ActionSpaceMixin
from .actions_turn import TurnPhaseHandlersMixin
from .actions_cast import CastingHandlersMixin
from .actions_combat import CombatHandlersMixin
from .actions_choices import ChoiceHandlersMixin
from .actions_mechanics import MechanicsHandlersMixin


class ActionHandler(
    ActionSpaceMixin,
    TurnPhaseHandlersMixin,
    CastingHandlersMixin,
    CombatHandlersMixin,
    ChoiceHandlersMixin,
    MechanicsHandlersMixin,
):
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
                "PLOT_CARD": self._handle_plot_card,
                "PLAY_FROM_GRAVEYARD": self._handle_play_from_graveyard,
                # Combat
                "ATTACK": self._handle_attack,
                "BLOCK": self._handle_block,
                # Delegated Combat Actions (Remapped indices based on review)
                "DECLARE_ATTACKERS_DONE": self._handle_declare_attackers_done, # Index 438
                "DECLARE_BLOCKERS_DONE": self._handle_declare_blockers_done, # Index 439
                "ATTACK_PLANESWALKER": self._handle_attack_planeswalker, # Indices 378-382
                "ASSIGN_MULTIPLE_BLOCKERS": self._handle_assign_multiple_blockers, # Indices 383-392
                "FIRST_STRIKE_ORDER": self._handle_first_strike_order, # Index 435
                "ASSIGN_COMBAT_DAMAGE": self._handle_assign_combat_damage, # Index 436
                "PROTECT_PLANESWALKER": self._handle_protect_planeswalker, # Index 444
                "ATTACK_BATTLE": self._handle_attack_battle, # Indices 462-466
                "DEFEND_BATTLE": self._handle_defend_battle, # Index 204
                "NINJUTSU": self._handle_ninjutsu, # Index 437
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
                "SADDLE": self._handle_saddle, # Index 478
                "TARGET_PAGE_NEXT": self._handle_target_page_next, # Index 479
                # Room/Class
                "UNLOCK_DOOR": self._handle_unlock_door, # Indices 248-252
                "LEVEL_UP_CLASS": self._handle_level_up_class, # Indices 253-257
                "LEVEL_UP_CREATURE": self._handle_level_up_creature, # Indices 467-471
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
                         if hasattr(gs, '_empty_mana_pools'):
                              gs._empty_mana_pools()
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

        generated_context = {}
        if hasattr(self, "action_reasons_with_context"):
            generated_context = dict(
                self.action_reasons_with_context.get(action_idx, {}).get(
                    "context", {}) or {})
        action_context = generated_context
        action_context.update(kwargs.get('context', {}) or {})
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

                # A spell/ability can pause mid-resolution for a policy
                # decision. Do not run SBAs, stack triggers, or resolve lower
                # stack items until that decision (and its continuation) ends.
                if (getattr(gs, 'targeting_context', None)
                        or getattr(gs, 'sacrifice_context', None)
                        or getattr(gs, 'choice_context', None)):
                    logging.debug(
                        "Game loop paused for an outstanding target/sacrifice/choice context.")
                    break

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
    
    
    
    

    
    
    
        # --- Specific Handler Implementations ---



     

     


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
    
    
        
        
                                








                        

        
                    
            

                    





