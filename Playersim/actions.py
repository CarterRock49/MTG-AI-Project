# actions.py

import logging
import re
import numpy as np
from .card import Card
from .enhanced_combat import ExtendedCombatResolver
from .combat_integration import integrate_combat_actions, apply_combat_action
from .debug import DEBUG_MODE
from .enhanced_card_evaluator import EnhancedCardEvaluator
from .combat_actions import CombatActionHandler

ACTION_MEANINGS = {
    # Basic game flow actions (0-12)
    0: ("END_TURN", None),            # Force-advance to next turn
    1: ("UNTAP_NEXT", None),          # Complete untap phase
    2: ("DRAW_NEXT", None),           # Complete draw phase
    3: ("MAIN_PHASE_END", None),      # End current main phase
    4: ("COMBAT_DAMAGE", None),       # Process combat damage
    5: ("END_PHASE", None),           # End current phase
    6: ("MULLIGAN", None),            # Take a mulligan
    7: ("UPKEEP_PASS", None),         # Complete upkeep phase
    8: ("BEGIN_COMBAT_END", None),    # Complete begin combat
    9: ("END_COMBAT", None),          # Complete end combat
    10: ("END_STEP", None),           # Complete end step
    11: ("PASS_PRIORITY", None),      # Pass priority
    12: ("CONCEDE", None),            # Concede the game

    # Play land actions (13-19: hand positions 0-6)
    **{i: ("PLAY_LAND", i-13) for i in range(13, 20)},

    # Play spell actions (20-27: hand positions 0-7)
    **{i: ("PLAY_SPELL", i-20) for i in range(20, 28)},

    # Attack actions (28-47: attacker indices 0-19)
    **{i: ("ATTACK", i-28) for i in range(28, 48)},

    # Block actions (48-67: blocker indices 0-19)
    **{i: ("BLOCK", i-48) for i in range(48, 68)},

    # Tap land actions (68-87: tap land indices 0-19)
    **{i: ("TAP_LAND", i-68) for i in range(68, 88)},

    # Ability activation actions (100-159: card indices 0-19, ability indices 0-2)
    **{100 + (i * 3) + j: ("ACTIVATE_ABILITY", (i, j))
       for i in range(20) for j in range(3)},

    # Transform actions (160-179: card indices 0-19)
    **{160 + i: ("TRANSFORM", i) for i in range(20)},

    # Modal DFC actions (180-195: hand positions 0-7)
    **{i: ("PLAY_MDFC_LAND_BACK", i-180) for i in range(180, 188)},
    **{i: ("PLAY_MDFC_BACK", i-188) for i in range(188, 196)},

    # Adventure actions (196-203: hand positions 0-7)
    **{i: ("PLAY_ADVENTURE", i-196) for i in range(196, 204)},

    # Defend battle actions (204-224: battle indices 0-4, creature indices 0-3)
    **{204 + (i * 4) + j: ("DEFEND_BATTLE", (i, j))
       for i in range(5) for j in range(4)},

    # Mulligan keep/bottom actions (225-228)
    225: ("KEEP_HAND", None),        # Keep hand during mulligan
    **{226 + i: ("BOTTOM_CARD", i) for i in range(4)},  # Bottom cards 0-3

    # Cast from exile actions (230-237: exile indices 0-7)
    **{i: ("CAST_FROM_EXILE", i-230) for i in range(230, 238)},

    # Discard actions (238-247: hand indices 0-9)
    **{238 + i: ("DISCARD_CARD", i) for i in range(10)},

    # Door and class actions (248-257)
    **{248 + i: ("UNLOCK_DOOR", i) for i in range(5)},
    **{253 + i: ("LEVEL_UP_CLASS", i) for i in range(5)},

    # Spree mode selection actions (258-273: 8 cards with 2 modes each)
    **{258 + (i * 2) + j: ("SELECT_SPREE_MODE", (i, j))
       for i in range(8) for j in range(2)},

    # Target selection actions (274-293)
    **{274 + i: ("SELECT_TARGET", i) for i in range(10)},
    **{284 + i: ("SACRIFICE_PERMANENT", i) for i in range(10)},

    # Library and card movement actions (299-308)
    **{299 + i: ("SEARCH_LIBRARY", i) for i in range(5)},
    305: ("PUT_TO_GRAVEYARD", None),  # During surveil
    306: ("PUT_ON_TOP", None),        # During scry/surveil
    307: ("PUT_ON_BOTTOM", None),     # During scry
    308: ("DREDGE", None),            # Use dredge ability from graveyard

    # Counter management (309-329)
    **{309 + i: ("ADD_COUNTER", i) for i in range(10)},
    **{319 + i: ("REMOVE_COUNTER", i) for i in range(10)},
    329: ("PROLIFERATE", None),

    # Zone movement (330-347)
    **{330 + i: ("RETURN_FROM_GRAVEYARD", i) for i in range(6)},
    **{336 + i: ("REANIMATE", i) for i in range(6)},
    **{342 + i: ("RETURN_FROM_EXILE", i) for i in range(6)},

    # Modal and choice actions (348-372)
    **{348 + i: ("CHOOSE_MODE", i) for i in range(10)},
    **{358 + i: ("CHOOSE_X_VALUE", i+1) for i in range(10)},
    **{368 + i: ("CHOOSE_COLOR", i) for i in range(5)},

    # Advanced combat (373-392)
    **{373 + i: ("ATTACK_PLANESWALKER", i) for i in range(5)},
    **{383 + i: ("ASSIGN_MULTIPLE_BLOCKERS", i) for i in range(10)},

    # Alternative casting methods (393-404)
    393: ("CAST_WITH_FLASHBACK", None),
    394: ("CAST_WITH_JUMP_START", None),
    395: ("CAST_WITH_ESCAPE", None),
    396: ("CAST_FOR_MADNESS", None),
    397: ("CAST_WITH_OVERLOAD", None),
    398: ("CAST_FOR_EMERGE", None),
    399: ("CAST_FOR_DELVE", None),
    400: ("PAY_KICKER", True),
    401: ("PAY_KICKER", False),
    402: ("PAY_ADDITIONAL_COST", True),
    403: ("PAY_ADDITIONAL_COST", False),
    404: ("PAY_ESCALATE", None),

    # Token and copy actions (405-412)
    **{405 + i: ("CREATE_TOKEN", i) for i in range(5)},
    410: ("COPY_PERMANENT", None),
    411: ("COPY_SPELL", None),
    412: ("POPULATE", None),

    # Specific mechanics (413-424)
    413: ("INVESTIGATE", None),       # Create Clue token
    414: ("FORETELL", None),          # Foretell a card
    415: ("AMASS", None),             # Put +1/+1 counters on Army
    416: ("LEARN", None),             # Get Lesson or discard/draw
    417: ("VENTURE", None),           # Venture into dungeon
    418: ("EXERT", None),             # Exert a creature
    419: ("EXPLORE", None),           # Creature explores
    420: ("ADAPT", None),             # Adapt a creature
    421: ("MUTATE", None),            # Mutate onto a creature
    422: ("CYCLING", None),           # Cycle a card

    # Combat actions (430-439)
    430: ("FIRST_STRIKE_ORDER", None),
    431: ("ASSIGN_COMBAT_DAMAGE", None),
    432: ("NINJUTSU", None),
    433: ("DECLARE_ATTACKERS_DONE", None),
    434: ("DECLARE_BLOCKERS_DONE", None),
    435: ("LOYALTY_ABILITY_PLUS", None),
    436: ("LOYALTY_ABILITY_ZERO", None),
    437: ("LOYALTY_ABILITY_MINUS", None),
    438: ("ULTIMATE_ABILITY", None),
    439: ("PROTECT_PLANESWALKER", None),

    # Card type specific actions (440-456)
    440: ("CAST_LEFT_HALF", None),    # Split card
    441: ("CAST_RIGHT_HALF", None),   # Split card
    442: ("CAST_FUSE", None),         # Split card
    443: ("AFTERMATH_CAST", None),    # Aftermath card
    444: ("FLIP_CARD", None),         # Flip card
    445: ("EQUIP", None),             # Equipment
    446: ("UNEQUIP", None),           # Equipment
    447: ("ATTACH_AURA", None),       # Aura
    448: ("FORTIFY", None),           # Fortification
    449: ("RECONFIGURE", None),       # Reconfigurable creature
    450: ("MORPH", None),             # Morph card
    451: ("MANIFEST", None),          # Manifest card
    # Attack battle actions (460-479: battle indices 0-19)
    **{460 + i: ("ATTACK_BATTLE", i) for i in range(20)},
}

class ActionHandler:
    """Handles action validation and execution"""
    
    def __init__(self, game_state):
        self.game_state = game_state
        self.card_evaluator = EnhancedCardEvaluator(game_state)
        
        # Use CombatActionHandler for combat-specific functionality
        from .combat_actions import CombatActionHandler
        self.combat_handler = CombatActionHandler(game_state)
        
        # Ensure combat systems are initialized
        self.combat_handler.setup_combat_systems()
        
    def _add_battle_attack_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_battle_attack_actions"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        combat_action_handler._add_battle_attack_actions(player, valid_actions, set_valid_action)
        
    def is_valid_attacker(self, card_id):
        """Delegate to CombatActionHandler.is_valid_attacker"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        return combat_action_handler.is_valid_attacker(card_id)

    def find_optimal_attack(self):
        """Delegate to CombatActionHandler.find_optimal_attack"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        return combat_action_handler.find_optimal_attack()

    def setup_combat_systems(self):
        """Delegate to CombatActionHandler.setup_combat_systems"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        combat_action_handler.setup_combat_systems()

    def _has_first_strike(self, card):
        """Delegate to CombatActionHandler._has_first_strike"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        return combat_action_handler._has_first_strike(card)

    def _add_multiple_blocker_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_multiple_blocker_actions"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        combat_action_handler._add_multiple_blocker_actions(player, valid_actions, set_valid_action)

    def _add_ninjutsu_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_ninjutsu_actions"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        combat_action_handler._add_ninjutsu_actions(player, valid_actions, set_valid_action)

    def _add_equipment_aura_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_equipment_aura_actions"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        combat_action_handler._add_equipment_aura_actions(player, valid_actions, set_valid_action)

    def _add_planeswalker_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_planeswalker_actions"""
        combat_action_handler = integrate_combat_actions(self.game_state)
        combat_action_handler._add_planeswalker_actions(player, valid_actions, set_valid_action)
    
    def should_hold_priority(self, player):
        """
        Determine if the player should hold priority based on game state.
        
        Args:
            player: The player who has priority
            
        Returns:
            bool: Whether the player should consider holding priority
        """
        gs = self.game_state
        
        # Always consider holding priority if the stack isn't empty
        if gs.stack:
            # Check if player has instant-speed responses
            for card_id in player["hand"]:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'card_types'):
                    continue
                    
                # Check for instants or flash
                if 'instant' in card.card_types or (hasattr(card, 'oracle_text') and 'flash' in card.oracle_text.lower()):
                    # Check if player can afford it
                    if hasattr(gs, 'mana_system'):
                        if gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else ""):
                            return True
                    else:
                        # Simple check - any mana available
                        if sum(player["mana_pool"].values()) > 0:
                            return True
            
            # Check if player has activated abilities that can be used at instant speed
            if hasattr(gs, 'ability_handler') and gs.ability_handler:
                for card_id in player["battlefield"]:
                    abilities = gs.ability_handler.get_activated_abilities(card_id)
                    for ability_idx, ability in enumerate(abilities):
                        if gs.ability_handler.can_activate_ability(card_id, ability_idx, player):
                            return True
        
        # By default, don't hold priority
        return False
    
    def recommend_ability_activation(self, card_id, ability_idx):
        """
        Determine if now is a good time to activate an ability with comprehensive strategic analysis.
        
        Returns:
            bool: Whether activation is recommended
            float: Confidence in recommendation (0-1)
        """
        gs = self.game_state
        
        # Use strategic planner if available
        if hasattr(gs, 'strategic_planner') and gs.strategic_planner is not None:
            try:
                return gs.strategic_planner.recommend_ability_activation(card_id, ability_idx)
            except Exception as e:
                logging.debug(f"Error using strategic planner for ability recommendation: {e}")
                # Fall back to original logic if there's an error
        
        # If no strategic planner is available, just return a default recommendation
        return True, 0.5  # Default to "yes" with medium confidence
    
    def generate_valid_actions(self):
        """Return the current action mask as boolean array with reasoning for all possible MTG actions."""
        try:
            # Initialize with expanded size to match all possible actions in ACTION_MEANINGS
            valid_actions = np.zeros(480, dtype=bool)  
            gs = self.game_state
            current_player = gs.p1 if gs.agent_is_p1 else gs.p2
            opponent = gs.p2 if gs.agent_is_p1 else gs.p1
            
            # Store reasons for enabling actions (for debugging)
            action_reasons = {}

            logging.debug(f"Generating valid actions for phase: {gs.phase}")

            # Helper function for tracking valid actions
            def set_valid_action(index, reason=""):
                if index < 0 or index >= len(ACTION_MEANINGS):  # Dynamic bound check based on ACTION_MEANINGS
                    logging.error(f"INVALID ACTION INDEX: {index} is out of bounds! Reason: {reason}")
                    return False
                valid_actions[index] = True
                action_reasons[index] = reason
                logging.debug(f"Setting valid action: {index} ({ACTION_MEANINGS.get(index, ('UNKNOWN', None))}). Reason: {reason}")
                return True
            
            # Check if we're in a main phase with empty stack
            is_main_phase = gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]
            stack_is_empty = len(gs.stack) == 0
            sorcery_allowed = is_main_phase and stack_is_empty
            
            # Always allow conceding as a valid action
            set_valid_action(12, "CONCEDE is always valid")
            
            # Check for mulligan state
            if hasattr(gs, 'mulligan_in_progress') and gs.mulligan_in_progress:
                valid_actions = np.zeros(480, dtype=bool)  # Reset valid actions
                
                # Check if the current player is the one who should be making mulligan decisions
                if gs.mulligan_player == current_player:
                    # Offer mulligan or keep hand
                    set_valid_action(6, "MULLIGAN - Draw a new hand of 7 cards")
                    set_valid_action(225, "KEEP_HAND - Keep current hand")  # FIXED: Use correct index from ACTION_MEANINGS
                else:
                    # If it's not this player's turn for mulligan, they can only pass
                    set_valid_action(11, "PASS_PRIORITY during opponent's mulligan")
                
                return valid_actions

            # Check for bottoming state (after London mulligan)
            if hasattr(gs, 'bottoming_in_progress') and gs.bottoming_in_progress:
                valid_actions = np.zeros(480, dtype=bool)  # Reset valid actions
                
                if gs.bottoming_player == current_player:
                    # Allow bottoming cards up to the required number
                    for i in range(min(len(current_player["hand"]), 4)):  # Support up to 4 hand indices
                        card_id = current_player["hand"][i]
                        card = gs._safe_get_card(card_id)
                        card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
                        set_valid_action(226 + i, f"BOTTOM_CARD {card_name} during mulligan")  # FIXED: Use correct index from ACTION_MEANINGS
                else:
                    # If it's not this player's turn for bottoming, they can only pass
                    set_valid_action(11, "PASS_PRIORITY during opponent's bottoming")
                
                return valid_actions
            
            # Process actions by phase
            if gs.phase == gs.PHASE_UNTAP:
                set_valid_action(1, "UNTAP_NEXT in UNTAP phase")
                    
            elif gs.phase == gs.PHASE_UPKEEP:
                set_valid_action(7, "UPKEEP_PASS in UPKEEP phase")
                
                # In upkeep, priority actions may be possible
                if stack_is_empty:
                    # Check for castable instants
                    self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                    
                    # Check for activated abilities
                    self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                    
                    # Check for flashback/jumpstart opportunities
                    self._add_alternative_casting_actions(current_player, valid_actions, set_valid_action, is_sorcery_timing=False)
                    
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in UPKEEP phase")
                
            elif gs.phase == gs.PHASE_DRAW:
                set_valid_action(2, "DRAW_NEXT in DRAW phase")
                
                # In draw phase, priority actions may be possible
                if stack_is_empty:
                    # Check for castable instants
                    self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                    
                    # Check for activated abilities
                    self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                    
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in DRAW phase")
                    
            elif gs.phase == gs.PHASE_MAIN_PRECOMBAT:
                set_valid_action(3, "MAIN_PHASE_END in MAIN_PRECOMBAT phase")
                set_valid_action(11, "PASS_PRIORITY in MAIN_PRECOMBAT phase")
                
                # Land play validation
                if not current_player["land_played"]:
                    for i in range(13, 20):
                        if (i - 13) < len(current_player["hand"]):
                            card_id = current_player["hand"][i - 13]
                            card = gs._safe_get_card(card_id)
                            # Add explicit check for card being a valid object with required attributes
                            if card and hasattr(card, 'type_line') and 'land' in card.type_line:
                                # Check if it's an MDFC with a land back face
                                is_mdfc_land = (hasattr(card, 'is_mdfc') and card.is_mdfc() and 
                                            hasattr(card, 'back_face') and 
                                            'land' in card.back_face.get('type_line', '').lower())
                                
                                if is_mdfc_land:
                                    # Enable action to play the land side
                                    set_valid_action(i, f"PLAY_LAND for {card.name}")
                                    # Add option to play the back face as land if it's a land
                                    set_valid_action(180 + (i - 13), f"PLAY_MDFC_LAND_BACK for {card.back_face.get('name', 'Unknown')}")
                                else:
                                    set_valid_action(i, f"PLAY_LAND for {card.name}")
                                    
                # Spell casting validation (sorcery timing allowed)
                if stack_is_empty:
                    for i in range(20, 28):
                        hand_idx = i - 20
                        if hand_idx < len(current_player["hand"]):
                            card_id = current_player["hand"][hand_idx]
                            card = gs._safe_get_card(card_id)
                            
                            # Skip if card is invalid
                            if not card or not hasattr(card, 'type_line') or not hasattr(card, 'card_types'):
                                continue
                                
                            # Use mana_system if available, otherwise fall back to simpler check
                            can_afford = False
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(current_player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                            else:
                                # Simple check - at least some mana available
                                can_afford = sum(current_player["mana_pool"].values()) > 0
                                
                            if 'land' not in card.type_line and can_afford:
                                # Check if the spell requires targets
                                requires_target = False
                                valid_targets_exist = True
                                
                                if hasattr(card, 'oracle_text'):
                                    requires_target = 'target' in card.oracle_text.lower()
                                    
                                    # If it requires targets, check if any are available
                                    if requires_target:
                                        valid_targets_exist = self._check_valid_targets_exist(card, current_player, opponent)
                                
                                # Only allow casting if valid targets exist (if required)
                                if requires_target and not valid_targets_exist:
                                    continue
                                
                                # Check if sorcery speed is required (only cast if sorcery_allowed)
                                if 'sorcery' in card.card_types and not sorcery_allowed:
                                    continue
                                    
                                # Check for Spree card handling
                                if hasattr(card, 'is_spree') and card.is_spree:
                                    set_valid_action(i, f"PLAY_SPREE_SPELL for {card.name}")
                                else:
                                    # Regular spell casting
                                    set_valid_action(i, f"PLAY_SPELL for {card.name}")

                                # Check for MDFC - allow casting back face
                                if hasattr(card, 'is_mdfc') and card.is_mdfc() and hasattr(card, 'back_face'):
                                    back_face = card.back_face
                                    if 'land' not in back_face.get('type_line', '').lower():
                                        # Check if we can afford the back face
                                        back_face_cost = back_face.get('mana_cost', '')
                                        if hasattr(gs, 'mana_system'):
                                            can_afford_back = gs.mana_system.can_pay_mana_cost(current_player, back_face_cost)
                                        else:
                                            can_afford_back = sum(current_player["mana_pool"].values()) > 0
                                        
                                        if can_afford_back:
                                            set_valid_action(188 + hand_idx, f"PLAY_MDFC_BACK for {back_face.get('name', 'Unknown')}")
                                
                                # Check for Adventure - allow casting adventure side
                                if hasattr(card, 'has_adventure') and card.has_adventure():
                                    adventure_data = card.get_adventure_data()
                                    if adventure_data:
                                        adventure_cost = adventure_data.get('cost', '')
                                        if hasattr(gs, 'mana_system'):
                                            can_afford_adventure = gs.mana_system.can_pay_mana_cost(current_player, adventure_cost)
                                        else:
                                            can_afford_adventure = sum(current_player["mana_pool"].values()) > 0
                                        
                                        if can_afford_adventure:
                                            # Check if the adventure requires sorcery speed
                                            adventure_type = adventure_data.get('type', '').lower()
                                            if 'sorcery' in adventure_type and not sorcery_allowed:
                                                continue
                                            
                                            set_valid_action(196 + hand_idx, f"PLAY_ADVENTURE for {adventure_data.get('name', 'Unknown')}")
                                            
                # Add support for transform actions
                for idx, card_id in enumerate(current_player["battlefield"]):
                    if idx >= 20:  # Limit to first 20 permanents
                        break
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, "transform"):
                        set_valid_action(160 + idx, f"TRANSFORM {card.name}")
                            
                # Add support for split cards
                self._add_split_card_actions(current_player, valid_actions, set_valid_action, sorcery_allowed)
                
                # Add support for Room doors
                self._add_room_door_actions(current_player, valid_actions, set_valid_action, sorcery_allowed)
                
                # Add support for Class level-ups (only at sorcery speed)
                self._add_class_level_actions(current_player, valid_actions, set_valid_action, sorcery_allowed)
                                            
                # Cycling - when checking hand for potential cycling
                if stack_is_empty:
                    for i in range(20, 28):
                        hand_idx = i - 20
                        if hand_idx < len(current_player["hand"]):
                            card_id = current_player["hand"][hand_idx]
                            card = gs._safe_get_card(card_id)
                            
                            if card and hasattr(card, 'oracle_text') and "cycling" in card.oracle_text.lower():
                                cycle_cost = ""
                                import re
                                match = re.search(r"cycling [^\(]([^\)]+)", card.oracle_text.lower())
                                if match:
                                    cycle_cost = match.group(1)
                                
                                # Check if we can afford cycling cost
                                can_afford_cycle = False
                                if hasattr(gs, 'mana_system'):
                                    can_afford_cycle = gs.mana_system.can_pay_mana_cost(current_player, cycle_cost)
                                else:
                                    can_afford_cycle = sum(current_player["mana_pool"].values()) > 0
                                
                                if can_afford_cycle:
                                    set_valid_action(422, f"CYCLING {card.name}")

                # Add support for alternative casting methods (at sorcery speed)
                self._add_alternative_casting_actions(current_player, valid_actions, set_valid_action, is_sorcery_timing=True)

                # Add support for kicker and additional costs
                self._add_kicker_options(current_player, valid_actions, set_valid_action)

                # Specific mechanics - systematically check for all
                self._add_specific_mechanics_actions(current_player, valid_actions, set_valid_action)
                
                # Check for cards castable from exile (like from Adventure)
                self._add_exile_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow tapping lands for mana
                self._add_land_tapping_actions(current_player, valid_actions, set_valid_action)
                
                # Add ability activation actions
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Add planeswalker loyalty ability actions
                self._add_planeswalker_actions(current_player, valid_actions, set_valid_action)
                
                # Add equipment and aura actions
                self._add_equipment_aura_actions(current_player, valid_actions, set_valid_action)
                
                # Add counter management actions
                self._add_counter_management_actions(current_player, valid_actions, set_valid_action)
                
                # Add zone movement actions
                self._add_zone_movement_actions(current_player, valid_actions, set_valid_action)
                
                # Add token and copy actions
                self._add_token_copy_actions(current_player, valid_actions, set_valid_action)
            
            elif gs.phase == gs.PHASE_BEGINNING_OF_COMBAT:
                # Similar to BEGIN_COMBAT
                set_valid_action(8, "BEGIN_COMBAT_END in BEGINNING_OF_COMBAT phase")
                
                # Allow instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow activated abilities
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in BEGINNING_OF_COMBAT phase")
                    
            elif gs.phase == gs.PHASE_BEGIN_COMBAT:
                set_valid_action(8, "BEGIN_COMBAT_END in BEGIN_COMBAT phase")
                
                # Allow instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow activated abilities
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in BEGIN_COMBAT phase")
                    
            elif gs.phase == gs.PHASE_DECLARE_ATTACKERS:
                # Calculate optimal attackers for observation but don't auto-select them
                try:
                    # Calculate optimal attackers but only for observation purposes
                    if not hasattr(gs, 'optimal_attackers') or gs.optimal_attackers is None:
                        gs.optimal_attackers = self.find_optimal_attack()
                    
                    # Always show all possible attackers
                    for idx in range(min(len(current_player["battlefield"]), 20)):
                        card_id = current_player["battlefield"][idx]
                        if self.is_valid_attacker(card_id):
                            set_valid_action(28 + idx, f"ATTACK with {gs._safe_get_card(card_id).name}")
                        
                    # Always allow ending declare attackers phase
                    set_valid_action(433, "DECLARE_ATTACKERS_DONE in DECLARE_ATTACKERS phase")
                    
                    # Add actions for defending opponent's battles
                    for idx, card_id in enumerate(opponent["battlefield"]):
                        if idx >= 5:  # Limit to 5 battle cards
                            break
                        
                        card = gs._safe_get_card(card_id)
                        if card and hasattr(card, 'is_battle') and card.is_battle:
                            # Get valid defenders (untapped creatures)
                            valid_defenders = []
                            for defender_idx, defender_id in enumerate(current_player["battlefield"]):
                                if defender_idx >= 4:  # Limit to 4 potential defenders per battle
                                    break
                                defender_card = gs._safe_get_card(defender_id)
                                if (defender_card and hasattr(defender_card, 'card_types') and 
                                    'creature' in defender_card.card_types and 
                                    defender_id not in current_player["tapped_permanents"]):
                                    valid_defenders.append(defender_idx)
                                    
                                    # Define action index
                                    battle_action_idx = 204 + (idx * 4) + defender_idx
                                    set_valid_action(battle_action_idx,
                                        f"DEFEND_BATTLE {card.name} with {defender_card.name}")

                    # Add planeswalker attack options
                    for idx, card_id in enumerate(opponent["battlefield"]):
                        if idx >= 5:  # Limit to 5 planeswalkers
                            break
                        
                        card = gs._safe_get_card(card_id)
                        if card and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                            # Enable attacking this planeswalker
                            set_valid_action(373 + idx, f"ATTACK_PLANESWALKER {card.name}")
                            
                    # Add actions for attacking battle cards
                    self._add_battle_attack_actions(current_player, valid_actions, set_valid_action)
                    
                    # Allow instant-speed actions
                    self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                    
                    # Allow activated abilities
                    self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                    
                    # Always allow priority passing
                    set_valid_action(11, "PASS_PRIORITY in DECLARE_ATTACKERS phase")
                    
                except Exception as e:
                    # Log the error but continue with graceful fallback
                    logging.error(f"Error in DECLARE_ATTACKERS phase: {str(e)}")
                    import traceback
                    logging.error(traceback.format_exc())
                    
                    # Fallback: just enable basic attack actions for all valid attackers
                    for idx in range(min(len(current_player["battlefield"]), 20)):
                        if idx < len(current_player["battlefield"]):
                            card_id = current_player["battlefield"][idx]
                            if self.is_valid_attacker(card_id):
                                set_valid_action(28 + idx, f"ATTACK with {gs._safe_get_card(card_id).name}")
                    
                    # Always allow ending declare attackers phase
                    set_valid_action(433, "DECLARE_ATTACKERS_DONE in DECLARE_ATTACKERS phase")
                    
            elif gs.phase == gs.PHASE_DECLARE_BLOCKERS:
                # Only add blockers that aren't already blocking the same attacker
                for idx in range(min(len(opponent["battlefield"]), 20)):
                    if idx < len(opponent["battlefield"]):
                        card_id = opponent["battlefield"][idx]
                        card = gs._safe_get_card(card_id)
                        if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                            # Check if already blocking
                            already_blocking = False
                            for attacker, blockers in gs.current_block_assignments.items():
                                if card_id in blockers:
                                    already_blocking = True
                                    break
                            if not already_blocking:
                                set_valid_action(48 + idx, f"BLOCK with {card.name}")
                        
                # Allow proceeding to damage
                set_valid_action(434, "DECLARE_BLOCKERS_DONE in DECLARE_BLOCKERS phase")
                
                # Add multiple blocker assignment options
                self._add_multiple_blocker_actions(current_player, valid_actions, set_valid_action)
                
                # Allow instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow activated abilities
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in DECLARE_BLOCKERS phase")
                    
            elif gs.phase == gs.PHASE_COMBAT_DAMAGE:
                # Add first strike damage order
                if any(self._has_first_strike(gs._safe_get_card(cid)) for cid in gs.current_attackers):
                    set_valid_action(430, "FIRST_STRIKE_ORDER in COMBAT_DAMAGE phase")
                
                # Allow manual damage assignment
                set_valid_action(431, "ASSIGN_COMBAT_DAMAGE in COMBAT_DAMAGE phase")
                
                # Standard combat damage calculation
                set_valid_action(4, "COMBAT_DAMAGE in COMBAT_DAMAGE phase")
                
                # Add ninjutsu actions if there are unblocked attackers
                self._add_ninjutsu_actions(current_player, valid_actions, set_valid_action)
                
                # Check if planeswalkers are being attacked and offer protection
                if hasattr(gs, "planeswalker_attack_targets") and gs.planeswalker_attack_targets:
                    set_valid_action(439, "PROTECT_PLANESWALKER from attack")
                
                # Allow instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow activated abilities
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in COMBAT_DAMAGE phase")
            
            # First strike damage phase
            elif gs.phase == gs.PHASE_FIRST_STRIKE_DAMAGE:
                set_valid_action(4, "COMBAT_DAMAGE in FIRST_STRIKE_DAMAGE phase")
                
                # Allow instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow activated abilities
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in FIRST_STRIKE_DAMAGE phase")
            
            # NEW PHASE: END_OF_COMBAT
            elif gs.phase == gs.PHASE_END_OF_COMBAT:
                # Similar to END_COMBAT
                set_valid_action(9, "END_COMBAT in END_OF_COMBAT phase")
                
                # Allow Ninjutsu
                self._add_ninjutsu_actions(current_player, valid_actions, set_valid_action)
                
                # Allow instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow activated abilities
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in END_OF_COMBAT phase")
                    
            elif gs.phase == gs.PHASE_END_COMBAT:
                set_valid_action(9, "END_COMBAT in END_COMBAT phase")
                
                # Allow instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow activated abilities
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in END_COMBAT phase")
                    
            elif gs.phase == gs.PHASE_MAIN_POSTCOMBAT:
                # IMPORTANT FIX: Always include END_TURN as a valid action
                set_valid_action(0, "END_TURN in MAIN_POSTCOMBAT phase")
                
                # Also include MAIN_PHASE_END and PASS_PRIORITY as options
                set_valid_action(3, "MAIN_PHASE_END in MAIN_POSTCOMBAT phase")
                set_valid_action(11, "PASS_PRIORITY in MAIN_POSTCOMBAT phase")
                
                # Land play validation (if still allowed)
                if not current_player["land_played"]:
                    for i in range(13, 20):
                        if (i - 13) < len(current_player["hand"]):
                            card_id = current_player["hand"][i - 13]
                            card = gs._safe_get_card(card_id)
                            # Add explicit check for card being a valid object with required attributes
                            if card and hasattr(card, 'type_line') and 'land' in card.type_line:
                                # Check if it's an MDFC with a land back face
                                is_mdfc_land = (hasattr(card, 'is_mdfc') and card.is_mdfc() and 
                                                hasattr(card, 'back_face') and 
                                                'land' in card.back_face.get('type_line', '').lower())
                                
                                if is_mdfc_land:
                                    # Enable action to play the land side
                                    set_valid_action(i, f"PLAY_LAND for {card.name}")
                                    # Add option to play the back face as land if it's a land
                                    set_valid_action(180 + (i - 13), f"PLAY_MDFC_LAND_BACK for {card.back_face.get('name', 'Unknown')}")
                                else:
                                    set_valid_action(i, f"PLAY_LAND for {card.name}")
                
                # Spell casting validation
                if stack_is_empty:
                    for i in range(20, 28):
                        hand_idx = i - 20
                        if hand_idx < len(current_player["hand"]):
                            card_id = current_player["hand"][hand_idx]
                            card = gs._safe_get_card(card_id)
                            
                            # Skip if card is invalid
                            if not card or not hasattr(card, 'type_line') or not hasattr(card, 'card_types'):
                                continue
                                
                            # Use mana_system if available, otherwise fall back to simpler check
                            can_afford = False
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(current_player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                            else:
                                # Simple check - at least some mana available
                                can_afford = sum(current_player["mana_pool"].values()) > 0
                                
                            if 'land' not in card.type_line and can_afford:
                                # Check if the spell requires targets
                                requires_target = False
                                valid_targets_exist = True
                                
                                if hasattr(card, 'oracle_text'):
                                    requires_target = 'target' in card.oracle_text.lower()
                                    
                                    # If it requires targets, check if any are available
                                    if requires_target:
                                        valid_targets_exist = self._check_valid_targets_exist(card, current_player, opponent)
                                
                                # Only allow casting if valid targets exist (if required)
                                if requires_target and not valid_targets_exist:
                                    continue
                                
                                if 'sorcery' in card.card_types and not sorcery_allowed:
                                    continue
                                    
                                # Check for Spree card handling
                                if hasattr(card, 'is_spree') and card.is_spree:
                                    set_valid_action(i, f"PLAY_SPREE_SPELL for {card.name}")
                                else:
                                    # Regular spell casting
                                    set_valid_action(i, f"PLAY_SPELL for {card.name}")

                                # Check for MDFC - allow casting back face
                                if hasattr(card, 'is_mdfc') and card.is_mdfc() and hasattr(card, 'back_face'):
                                    back_face = card.back_face
                                    if 'land' not in back_face.get('type_line', '').lower():
                                        # Check if we can afford the back face
                                        back_face_cost = back_face.get('mana_cost', '')
                                        if hasattr(gs, 'mana_system'):
                                            can_afford_back = gs.mana_system.can_pay_mana_cost(current_player, back_face_cost)
                                        else:
                                            can_afford_back = sum(current_player["mana_pool"].values()) > 0
                                        
                                        if can_afford_back:
                                            set_valid_action(188 + hand_idx, f"PLAY_MDFC_BACK for {back_face.get('name', 'Unknown')}")
                                
                                # Check for Adventure - allow casting adventure side
                                if hasattr(card, 'has_adventure') and card.has_adventure():
                                    adventure_data = card.get_adventure_data()
                                    if adventure_data:
                                        adventure_cost = adventure_data.get('cost', '')
                                        if hasattr(gs, 'mana_system'):
                                            can_afford_adventure = gs.mana_system.can_pay_mana_cost(current_player, adventure_cost)
                                        else:
                                            can_afford_adventure = sum(current_player["mana_pool"].values()) > 0
                                        
                                        if can_afford_adventure:
                                            # Check if the adventure requires sorcery speed
                                            adventure_type = adventure_data.get('type', '').lower()
                                            if 'sorcery' in adventure_type and not sorcery_allowed:
                                                continue
                                            
                                            set_valid_action(196 + hand_idx, f"PLAY_ADVENTURE for {adventure_data.get('name', 'Unknown')}")
                
                # Add support for transform actions
                for idx, card_id in enumerate(current_player["battlefield"]):
                    if idx >= 20:  # Limit to first 20 permanents
                        break
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, "transform"):
                        set_valid_action(160 + idx, f"TRANSFORM {card.name}")
                
                # Add support for split cards
                self._add_split_card_actions(current_player, valid_actions, set_valid_action, sorcery_allowed)
                
                # Add support for Room doors (only at sorcery speed)
                self._add_room_door_actions(current_player, valid_actions, set_valid_action, sorcery_allowed)
                
                # Add support for Class level-ups (only at sorcery speed)
                self._add_class_level_actions(current_player, valid_actions, set_valid_action, sorcery_allowed)
                                            
                # Cycling - when checking hand for potential cycling
                if stack_is_empty:
                    for i in range(20, 28):
                        hand_idx = i - 20
                        if hand_idx < len(current_player["hand"]):
                            card_id = current_player["hand"][hand_idx]
                            card = gs._safe_get_card(card_id)
                            
                            if card and hasattr(card, 'oracle_text') and "cycling" in card.oracle_text.lower():
                                cycle_cost = ""
                                import re
                                match = re.search(r"cycling [^\(]([^\)]+)", card.oracle_text.lower())
                                if match:
                                    cycle_cost = match.group(1)
                                
                                # Check if we can afford cycling cost
                                can_afford_cycle = False
                                if hasattr(gs, 'mana_system'):
                                    can_afford_cycle = gs.mana_system.can_pay_mana_cost(current_player, cycle_cost)
                                else:
                                    can_afford_cycle = sum(current_player["mana_pool"].values()) > 0
                                
                                if can_afford_cycle:
                                    set_valid_action(422, f"CYCLING {card.name}")

                # Add support for alternative casting methods at sorcery speed
                self._add_alternative_casting_actions(current_player, valid_actions, set_valid_action, is_sorcery_timing=True)

                # Add support for kicker and additional costs
                self._add_kicker_options(current_player, valid_actions, set_valid_action)

                # Specific mechanics - systematically check for all
                self._add_specific_mechanics_actions(current_player, valid_actions, set_valid_action)
                
                # Check for cards castable from exile (like from Adventure)
                self._add_exile_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow tapping lands for mana
                self._add_land_tapping_actions(current_player, valid_actions, set_valid_action)
                
                # Add ability activation actions
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Add planeswalker loyalty ability actions
                self._add_planeswalker_actions(current_player, valid_actions, set_valid_action)
                
                # Add equipment and aura actions
                self._add_equipment_aura_actions(current_player, valid_actions, set_valid_action)
                
                # Add counter management actions
                self._add_counter_management_actions(current_player, valid_actions, set_valid_action)
                
                # Add zone movement actions
                self._add_zone_movement_actions(current_player, valid_actions, set_valid_action)
                
                # Add token and copy actions
                self._add_token_copy_actions(current_player, valid_actions, set_valid_action)
            
            elif gs.phase == gs.PHASE_END_STEP:
                # CRITICAL FIX: In END_STEP, allow both END_STEP (to go to cleanup) and END_TURN (to skip cleanup)
                set_valid_action(10, "END_STEP in END_STEP phase")
                set_valid_action(0, "END_TURN in END_STEP phase (skip cleanup)")
                
                # Allow instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Allow activated abilities
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Always allow priority passing
                set_valid_action(11, "PASS_PRIORITY in END_STEP phase")
        
            elif gs.phase == gs.PHASE_CLEANUP:
                # Handle CLEANUP phase properly to ensure turn advancement
                set_valid_action(5, "END_PHASE in CLEANUP phase (advance turn)")
                
                # Check for discard actions (if hand size exceeds 7)
                if len(current_player["hand"]) > 7:
                    cards_to_discard = len(current_player["hand"]) - 7
                    for i in range(min(len(current_player["hand"]), 10)):  # Support up to 10 hand positions
                        if i < len(current_player["hand"]):
                            card_id = current_player["hand"][i]
                            card = gs._safe_get_card(card_id)
                            if card:
                                set_valid_action(238 + i, f"DISCARD_CARD for {card.name if hasattr(card, 'name') else card_id}")
                    
            elif gs.phase == gs.PHASE_PRIORITY:
                set_valid_action(11, "PASS_PRIORITY in PRIORITY phase")
                
                # Add instant-speed actions
                self._add_instant_casting_actions(current_player, valid_actions, set_valid_action)
                
                # Add ability activation actions
                self._add_ability_activation_actions(current_player, valid_actions, set_valid_action)
                
                # Check for Spree mode selection for cards on the stack
                self._add_spree_mode_actions(current_player, valid_actions, set_valid_action)
                
                # Add counter spell actions
                self._add_counter_actions(current_player, valid_actions, set_valid_action)
                
                # Allow damage prevention/redirection
                self._add_damage_prevention_actions(current_player, valid_actions, set_valid_action)
                
                # Add X value selection options for spells on stack
                self._add_x_cost_actions(current_player, valid_actions, set_valid_action)
            
            # TARGETING PHASE
            elif gs.phase == gs.PHASE_TARGETING and hasattr(gs, 'targeting_context'):
                context = gs.targeting_context
                source_id = context.get('source_id')
                source_card = gs._safe_get_card(source_id)
                
                if source_card and context.get('valid_targets'):
                    valid_targets = context['valid_targets']
                    for target_idx, target_id in enumerate(valid_targets[:10]):  # Limit to 10 targets
                        target_card = gs._safe_get_card(target_id)
                        set_valid_action(274 + target_idx, 
                                        f"SELECT_TARGET {target_card.name if target_card and hasattr(target_card, 'name') else target_id} for {source_card.name if hasattr(source_card, 'name') else source_id}")

            # Add this in the elif branch for PHASE_SACRIFICE
            elif gs.phase == gs.PHASE_SACRIFICE and hasattr(gs, 'sacrifice_context'):
                context = gs.sacrifice_context
                source_id = context.get('source_id')
                source_card = gs._safe_get_card(source_id)
                
                if source_card and context.get('valid_sacrifices'):
                    valid_sacrifices = context['valid_sacrifices']
                    for sac_idx, permanent_id in enumerate(valid_sacrifices[:10]):  # Limit to 10 permanents
                        permanent_card = gs._safe_get_card(permanent_id)
                        set_valid_action(284 + sac_idx, 
                                    f"SACRIFICE_PERMANENT {permanent_card.name if permanent_card and hasattr(permanent_card, 'name') else permanent_id} for {source_card.name if hasattr(source_card, 'name') else source_id}")

            # Handle scry in progress
            if hasattr(gs, 'scry_in_progress') and gs.scry_in_progress and hasattr(gs, 'scrying_cards') and gs.scrying_cards:
                # When scrying, only allow put on top or put on bottom actions
                valid_actions = np.zeros(480, dtype=bool)  # Reset valid actions
                
                # Get the card being decided on
                card_id = gs.scrying_cards[0]
                card = gs._safe_get_card(card_id)
                card_name = card.name if card and hasattr(card, 'name') else f"Card {card_id}"
                
                # Allow both actions
                set_valid_action(306, f"PUT_ON_TOP for {card_name}")
                set_valid_action(307, f"PUT_ON_BOTTOM for {card_name}")
                
                # No other actions are valid during scry
                return valid_actions

            # Handle surveil in progress  
            elif hasattr(gs, 'surveil_in_progress') and gs.surveil_in_progress and hasattr(gs, 'cards_being_surveiled') and gs.cards_being_surveiled:
                # When surveiling, only allow put to graveyard or put on top actions
                valid_actions = np.zeros(480, dtype=bool)  # Reset valid actions
                
                # Get the card being decided on
                card_id = gs.cards_being_surveiled[0]
                card = gs._safe_get_card(card_id)
                card_name = card.name if card and hasattr(card, 'name') else f"Card {card_id}"
                
                # Allow both actions
                set_valid_action(305, f"PUT_TO_GRAVEYARD for {card_name}")
                set_valid_action(306, f"PUT_ON_TOP for {card_name}")
                
                # No other actions are valid during surveil
                return valid_actions
                                
            # Store reasons for debugging
            self.action_reasons = action_reasons
            
            # Count and log valid actions
            valid_count = np.sum(valid_actions)
            logging.debug(f"Valid action count: {valid_count}")
            
            # If very few options, log them all
            if valid_count <= 5:
                for idx in np.where(valid_actions)[0]:
                    action_type, param = self.get_action_info(idx)
                    logging.debug(f"  Valid: {action_type}({param}) - {action_reasons.get(idx, 'Unknown reason')}")
            
            # IMPORTANT FIX: Fallback - always allow CONCEDE if no valid action exists
            if not valid_actions.any():
                logging.warning("No valid actions found, adding END_TURN as fallback")
                set_valid_action(0, "FALLBACK - No valid actions")
                # Also add CONCEDE as last resort
                set_valid_action(12, "FALLBACK - Allow conceding")
            
            return valid_actions
            
        except Exception as e:
            logging.error(f"Error generating valid actions: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            
            # Fallback action mask with END_TURN and CONCEDE
            fallback_actions = np.zeros(480, dtype=bool)  # Updated size to 480
            fallback_actions[0] = True  # END_TURN
            fallback_actions[12] = True  # CONCEDE
            return fallback_actions

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
        """Get action type and parameter from action index with better error handling"""
        action_type, param = ACTION_MEANINGS.get(action_idx, (None, None))
        if action_type is None:
            logging.error(f"Unknown action index: {action_idx}")
            return "INVALID", None
        return action_type, param
    
    def apply_action(self, action_type, param):
        
        combat_actions = {
            "FIRST_STRIKE_ORDER", "ASSIGN_COMBAT_DAMAGE", "NINJUTSU",
            "DECLARE_ATTACKERS_DONE", "DECLARE_BLOCKERS_DONE", "ATTACK_PLANESWALKER",
            "ASSIGN_MULTIPLE_BLOCKERS", "DEFEND_BATTLE", "PROTECT_PLANESWALKER", "ATTACK_BATTLE"
        }

        # Check if this is a combat action
        if action_type in combat_actions:
            # Apply the combat action with more granular error handling
            try:
                success = apply_combat_action(self.game_state, action_type, param)
                
                # Calculate reward based on action type and success
                if success:
                    if action_type == "FIRST_STRIKE_ORDER":
                        reward = 0.15  # Optimized damage ordering
                    elif action_type == "ASSIGN_COMBAT_DAMAGE":
                        reward = 0.2   # Skilled damage assignment
                    elif action_type == "NINJUTSU":
                        reward = 0.3   # Tricky combat maneuver
                    elif action_type in ["DECLARE_ATTACKERS_DONE", "DECLARE_BLOCKERS_DONE"]:
                        reward = 0.1   # Basic phase completion
                    elif action_type == "ATTACK_PLANESWALKER":
                        reward = 0.25  # Strategic planeswalker targeting
                    elif action_type == "ASSIGN_MULTIPLE_BLOCKERS":
                        reward = 0.2   # Coordinated defense
                    elif action_type == "DEFEND_BATTLE":
                        reward = 0.25  # Battle interaction
                    elif action_type == "PROTECT_PLANESWALKER":
                        reward = 0.3   # Defensive planeswalker play
                    elif action_type == "ATTACK_BATTLE":
                        reward = 0.2   # Aggressive battle play
                    else:
                        reward = 0.1   # Default small positive reward
                else:
                    reward = -0.1      # Penalty for failed action
                    
                return reward, False   # Combat actions don't end the game
            except KeyError:
                logging.error(f"Invalid combat action type: {action_type}")
                return -0.2, False
            except ValueError as e:
                logging.error(f"Invalid combat action parameters: {e}")
                return -0.15, False
            except Exception as e:
                logging.error(f"Error in combat action: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
                return -0.1, False
        
        try:
            gs = self.game_state
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1
            reward = 0
            done = False
            
            logging.debug(f"Applying action: {action_type} with param: {param}")
            
            # Add context-aware rewards where applicable
            if hasattr(gs, 'strategic_planner') and gs.strategic_planner is not None:
                context = {"action_type": action_type, "param": param}
                if action_type == "PLAY_SPELL" and param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    context_value = gs.strategic_planner.evaluate_play_card_action(card_id, context)
                    reward += context_value * 0.2  # Scale the strategic value as additional reward
                elif action_type == "ATTACK" and param is not None:
                    # Integrate strategic attack evaluation
                    if gs.current_attackers and param < len(me["battlefield"]):
                        card_id = me["battlefield"][param]
                        attackers = gs.current_attackers.copy()
                        if card_id not in attackers:
                            attackers.append(card_id)
                        attack_value = gs.strategic_planner.evaluate_attack_action(attackers)
                        reward += attack_value * 0.3
                elif action_type == "ACTIVATE_ABILITY" and isinstance(param, tuple) and len(param) == 2:
                    card_idx, ability_idx = param
                    if card_idx < len(me["battlefield"]):
                        card_id = me["battlefield"][card_idx]
                        value, _ = gs.strategic_planner.evaluate_ability_activation(card_id, ability_idx)
                        reward += value * 0.3
                        
            # Record state before action to track changes
            previous_state = {
                "me_life": me["life"],
                "opp_life": opp["life"],
                "me_cards": len(me["hand"]),
                "opp_cards": len(opp["hand"]),
                "me_creatures": sum(1 for cid in me["battlefield"] 
                                if gs._safe_get_card(cid) and 
                                hasattr(gs._safe_get_card(cid), 'card_types') 
                                and 'creature' in gs._safe_get_card(cid).card_types),
                "opp_creatures": sum(1 for cid in opp["battlefield"] 
                                if gs._safe_get_card(cid) and 
                                hasattr(gs._safe_get_card(cid), 'card_types') 
                                and 'creature' in gs._safe_get_card(cid).card_types)
            }

            # Basic game flow actions (0-12)
            if action_type == "END_TURN":
                # CRITICAL FIX: END_TURN should force advance to end step then to cleanup phase
                if gs.phase != gs.PHASE_END_STEP:
                    gs.phase = gs.PHASE_END_STEP
                    logging.debug(f"Action: End turn; transitioning to END_STEP phase.")
                else:
                    # Already in END_STEP, go to CLEANUP and process end step effects
                    gs.phase = gs.PHASE_CLEANUP
                    gs._end_phase(me)
                    logging.debug(f"Action: End turn from END_STEP; transitioning to CLEANUP phase.")
                
                # Check if combat was skipped
                if not gs.combat_damage_dealt:
                    reward -= 0.1  # Penalty for not dealing combat damage
            
            elif action_type == "UNTAP_NEXT":
                gs._untap_phase(me)
                gs.phase = gs.PHASE_UPKEEP
                logging.debug("Untap phase completed; transitioning to UPKEEP phase.")

            elif action_type == "DRAW_NEXT":
                gs._draw_phase(me)
                gs.phase = gs.PHASE_MAIN_PRECOMBAT
                logging.debug("Draw phase completed; transitioning to MAIN_PRECOMBAT phase.")

            elif action_type == "MAIN_PHASE_END":
                if gs.phase == gs.PHASE_MAIN_PRECOMBAT:
                    gs.phase = gs.PHASE_BEGIN_COMBAT
                    logging.debug("Pre-combat main phase ended; transitioning to BEGIN_COMBAT phase.")
                else:
                    # CRITICAL FIX: MAIN_PHASE_END in postcombat should go to end step, not skip it
                    gs.phase = gs.PHASE_END_STEP
                    logging.debug("Post-combat main phase ended; transitioning to END_STEP phase.")

            elif action_type == "COMBAT_DAMAGE":
                # Check if there are any attackers before resolving combat
                if not gs.current_attackers:
                    logging.debug("No attackers declared but attempting combat damage; providing feedback")
                    reward -= 0.1  # Penalty for exploring combat with no attackers
                    gs.phase = gs.PHASE_END_COMBAT  # Skip to end combat phase
                else:
                    # Use the existing combat resolver if available
                    if hasattr(gs, 'combat_resolver') and gs.combat_resolver:
                        combat = gs.combat_resolver
                    else:
                        combat = ExtendedCombatResolver(gs)
                        
                    opp_life_before = opp["life"]
                    opp_creatures_before = sum(1 for cid in opp["battlefield"] 
                                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                                        and 'creature' in gs._safe_get_card(cid).card_types)
                    
                    # First, simulate the results
                    results = combat.simulate_combat()
                    
                    # Then resolve and apply the results
                    damage_dealt = combat.resolve_combat()
                    
                    # Track additional metrics for rewards
                    opp_creatures_after = sum(1 for cid in opp["battlefield"] 
                                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                                        and 'creature' in gs._safe_get_card(cid).card_types)
                    creatures_killed = opp_creatures_before - opp_creatures_after
                    
                    if damage_dealt > 0:
                        gs.combat_damage_dealt = True
                        
                        # Base damage reward is increased
                        damage_reward = min(damage_dealt * 0.15, 0.75)
                        reward += damage_reward
                        
                        # Add lethal damage bonus
                        if opp["life"] <= 0:
                            reward += 3.0  # Reward for winning the game
                        # Add "getting close" bonus for bringing opponent below 5 life
                        elif opp["life"] <= 5 and opp_life_before > 5:
                            reward += 0.75
                        
                        # Add increasing rewards as opponent life gets lower
                        elif opp["life"] <= 10 and opp_life_before > 10:
                            reward += 0.5  # New threshold reward
                            
                        # Add reward for killing creatures
                        if creatures_killed > 0:
                            reward += min(creatures_killed * 0.3, 0.9)
                            
                        # Add tempo reward based on relative battlefield position
                        my_power = sum(gs._safe_get_card(cid).power for cid in me["battlefield"] 
                                    if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power') and 'creature' in gs._safe_get_card(cid).card_types)
                        opp_power = sum(gs._safe_get_card(cid).power for cid in opp["battlefield"] 
                                    if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power') and 'creature' in gs._safe_get_card(cid).card_types)
                        if my_power > opp_power:
                            reward += 0.2
                            
                        logging.debug(f"Combat rewards: +{damage_reward:.2f} damage, +{min(creatures_killed * 0.3, 0.9):.2f} kills, board position: {0.2 if my_power > opp_power else 0}")
                    else:
                        # Smaller penalty for trying combat but dealing no damage
                        reward -= 0.15
                    logging.debug(f"Combat damage action: dealt {damage_dealt} damage; reward updated to {reward}")
                    logging.debug(f"Post-combat: P1 Life = {gs.p1['life']}, P2 Life = {gs.p2['life']}")
                    
                    # Check state-based actions after combat to detect game end conditions
                    gs.check_state_based_actions()
                    
                    # CRITICAL FIX: Force update to next phase since damage has been dealt
                    gs.phase = gs.PHASE_END_COMBAT

            elif action_type == "END_PHASE":
                # CRITICAL FIX: Process special END_PHASE actions by phase type
                if gs.phase == gs.PHASE_CLEANUP:
                    # From cleanup, advance to next turn
                    gs.turn += 1
                    gs.phase = gs.PHASE_UNTAP  
                    gs.combat_damage_dealt = False  # Reset for new turn
                    logging.debug(f"Action: End phase from CLEANUP; advancing to Turn {gs.turn}.")
                    
                    # Reset key turn state
                    me["land_played"] = False
                    if hasattr(gs, 'abilities_activated_this_turn'):
                        gs.abilities_activated_this_turn = []
                else:
                    # Default end phase behavior - advance to next logical phase
                    logging.debug(f"Action: End phase from {gs.phase}; transitioning to next phase.")
                    gs._advance_phase()
                
            elif action_type == "UPKEEP_PASS":
                # Process upkeep triggers would go here
                gs.phase = gs.PHASE_DRAW
                logging.debug("Upkeep phase passed; transitioning to DRAW phase.")
                    
            elif action_type == "BEGIN_COMBAT_END":
                gs.phase = gs.PHASE_DECLARE_ATTACKERS
                logging.debug("Begin combat ended; transitioning to DECLARE_ATTACKERS phase.")
                
            elif action_type == "END_COMBAT":
                gs.phase = gs.PHASE_MAIN_POSTCOMBAT
                logging.debug("Combat phase ended; transitioning to MAIN_POSTCOMBAT phase.")
                
            elif action_type == "END_STEP":
                # Process end step effects
                # CRITICAL FIX: END_STEP now transitions to CLEANUP explicitly
                gs.phase = gs.PHASE_CLEANUP
                logging.debug(f"Action: End step completed; transitioning to CLEANUP phase.")
                
                # Perform cleanup actions
                active_player = gs._get_active_player()
                gs._end_phase(active_player)
                
                gs._phase_action_count = 0
                return reward, done

            elif action_type == "PASS_PRIORITY":
                # Check if should hold priority
                if self.should_hold_priority(me):
                    # Still pass priority if explicitly chosen, but add small penalty
                    reward -= 0.05
                
                gs._pass_priority()
                logging.debug("Priority passed.")
                
            elif action_type == "CONCEDE":
                # Immediately end the game as a loss
                reward -= 3.0  # Big penalty for conceding
                done = True
                logging.debug("Player conceded the game.")
                
            # Play land actions (13-19)
            elif action_type == "PLAY_LAND":
                if param is not None and param < len(me["hand"]):
                    land_played = self._handle_play_card(me, param, is_land=True)
                    reward += 0.15 if land_played else 0  # Reward for successful land play
                    
            elif action_type == "PLAY_SPELL":
                if param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    card = gs._safe_get_card(card_id)
                    
                    # Skip if card is invalid
                    if not card or not hasattr(card, 'type_line') or 'land' in card.type_line:
                        logging.debug("Invalid spell action attempted.")
                        reward = 0
                    else:
                        # Use mana_system if available, otherwise fall back to simpler check
                        can_afford = False
                        has_x_cost = hasattr(card, 'mana_cost') and 'X' in card.mana_cost
                        
                        if hasattr(gs, 'mana_system'):
                            can_afford = gs.mana_system.can_pay_mana_cost(me, card.mana_cost if hasattr(card, 'mana_cost') else "")
                        else:
                            # Simple check - at least some mana available
                            can_afford = sum(me["mana_pool"].values()) > 0
                            
                        if not can_afford:
                            logging.debug(f"Cannot afford to cast {card.name}")
                            reward = 0
                        else:
                            # Handle Spree vs regular cards
                            if hasattr(card, 'is_spree') and card.is_spree:
                                # Handle Spree spell casting
                                
                                # For AI implementation, select affordable modes automatically
                                selected_modes = []
                                
                                if hasattr(card, 'spree_modes'):
                                    for i, mode in enumerate(card.spree_modes):
                                        # Check if we can afford this mode
                                        mode_cost = mode.get('cost', '')
                                        if hasattr(gs, 'mana_system'):
                                            if gs.mana_system.can_pay_mana_cost(me, mode_cost):
                                                selected_modes.append(i)
                                        else:
                                            # In simple mode, just add all modes
                                            selected_modes.append(i)
                                
                                # Create context with selected modes
                                spree_context = {"is_spree": True, "selected_modes": selected_modes}
                                
                                me["hand"].pop(param)
                                
                                # Pay base cost
                                if hasattr(gs, 'mana_system'):
                                    gs.mana_system.pay_mana_cost(me, card.mana_cost if hasattr(card, 'mana_cost') else "")
                                else:
                                    # Simple deduction - use all available mana
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                
                                # Pay for selected modes
                                if hasattr(gs, 'mana_system'):
                                    for mode_idx in selected_modes:
                                        if mode_idx < len(card.spree_modes):
                                            mode_cost = card.spree_modes[mode_idx].get('cost', '')
                                            gs.mana_system.pay_mana_cost(me, mode_cost)
                                
                                # Add to stack with spree context
                                gs.stack.append(("SPELL", card_id, me, spree_context))
                                
                                # Increased reward for casting Spree with multiple modes
                                reward += 0.3 * (1 + len(selected_modes) * 0.2)
                                
                                logging.debug(f"Spree spell {card.name} cast with {len(selected_modes)} additional modes")
                                gs.phase = gs.PHASE_PRIORITY
                            else:
                                # Get contextual value before playing
                                context_value = 0
                                if hasattr(gs, 'strategic_planner') and gs.strategic_planner is not None:
                                    context_value = gs.strategic_planner.evaluate_card_for_sequence(card)
                                elif hasattr(self, 'card_evaluator'):
                                    if hasattr(self.card_evaluator, 'evaluate_card'):
                                        context_value = self.card_evaluator.evaluate_card(card_id, "play")
                                    else:
                                        context_value = self.card_evaluator.get_card_context_value(card_id, me, "play")
                                
                                me["hand"].pop(param)
                                
                                # Create casting context
                                cast_context = {}
                                
                                # Special handling for X cost spells
                                if has_x_cost:
                                    # For now, X=0 - player will choose with CHOOSE_X_VALUE action
                                    cast_context["X"] = 0
                                    logging.debug(f"Spell with X cost added to stack. Player will choose X value.")
                                
                                # Use mana_system to pay cost if available
                                efficient_mana_use = False
                                if hasattr(gs, 'mana_system'):
                                    efficient_mana_use = gs.mana_system.pay_mana_cost(me, card.mana_cost if hasattr(card, 'mana_cost') else "", cast_context)
                                else:
                                    # Simple deduction - use all available mana
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                    efficient_mana_use = True
                                
                                gs.stack.append(("SPELL", card_id, me, cast_context))
                                
                                # Base reward scaled by contextual value
                                reward += 0.3 * (1 + min(context_value, 1.0))
                                if efficient_mana_use:
                                    reward += 0.15  # Reward for efficient mana usage
                                    
                                logging.debug(f"Spell {card.name} cast and added to stack. Contextual value: {context_value:.2f}")
                                
                                # Move to priority phase for X cost selection if needed
                                gs.phase = gs.PHASE_PRIORITY
            
            elif action_type == "ATTACK":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if not card or not hasattr(card, 'card_types'):
                        logging.warning(f"Invalid card in battlefield at index {param}.")
                    elif 'creature' not in card.card_types:
                        logging.debug(f"Invalid ATTACK action: {card.name} is not a creature.")
                        reward -= 0.1
                    elif card_id in me["entered_battlefield_this_turn"] and not self._has_haste(card_id):
                        logging.debug(f"Invalid ATTACK action: {card.name} has summoning sickness.")
                        reward -= 0.1
                    elif card_id in gs.current_attackers:
                        # If already attacking, remove from attackers (toggle)
                        gs.current_attackers.remove(card_id)
                        logging.debug(f"{card.name} is already attacking; removing from attackers.")
                    else:
                        # Get attack context value with enhanced evaluation
                        attack_value = 0
                        if hasattr(gs, 'combat_resolver') and gs.combat_resolver:
                            # Simulate to get better attack evaluation
                            original_attackers = list(gs.current_attackers)
                            gs.current_attackers.append(card_id)
                            results = gs.combat_resolver.simulate_combat()
                            attack_value = results.get("expected_value", 0)
                            
                            # Restore original attackers and then add the new one
                            gs.current_attackers = original_attackers
                            gs.current_attackers.append(card_id)
                        else:
                            # Fallback to card evaluator
                            if hasattr(self, 'card_evaluator'):
                                if hasattr(self.card_evaluator, 'evaluate_card'):
                                    attack_value = self.card_evaluator.evaluate_card(card_id, "attack")
                                else:
                                    attack_value = self.card_evaluator.get_card_context_value(card_id, me, "attack")
                        
                        if hasattr(gs, 'ability_handler') and gs.ability_handler:
                            gs.ability_handler.handle_attack_triggers(card_id)
                            
                        # Calculate reward based on attack value
                        attack_reward = max(0.1, min(attack_value * 0.3, 0.75))
                        reward += attack_reward
                        
                        logging.debug(f"Added {card.name} to attackers. Attack value: {attack_value:.2f}, reward: {attack_reward:.2f}")
                        
                        # Mark that we've used at least part of the suggestion
                        if hasattr(gs, 'attack_suggestion_used'):
                            gs.attack_suggestion_used = True
            
            # Block actions (48-67)
            elif action_type == "BLOCK":
                if param is not None and param < len(opp["battlefield"]):
                    blocker_id = opp["battlefield"][param]
                    blocker_card = gs._safe_get_card(blocker_id)
                    
                    if blocker_card is None:
                        logging.warning(f"Invalid blocker card at index {param}.")
                    elif 'creature' not in blocker_card.card_types:
                        logging.debug(f"Invalid BLOCK action: {blocker_card.name} is not a creature.")
                        reward -= 0.1
                    else:
                        # Use strategic block evaluation if available
                        if hasattr(gs, 'strategic_planner'):
                            # Find the best attacker to block based on strategic evaluation
                            best_attacker = None
                            best_block_value = -float('inf')
                            
                            for attacker_id in gs.current_attackers:
                                block_value = gs.strategic_planner.evaluate_block_action(attacker_id, [blocker_id])
                                if block_value > best_block_value:
                                    best_block_value = block_value
                                    best_attacker = attacker_id
                            
                            if best_attacker:
                                if best_attacker not in gs.current_block_assignments:
                                    gs.current_block_assignments[best_attacker] = []
                                
                                gs.current_block_assignments[best_attacker].append(blocker_id)
                                attacker_name = gs._safe_get_card(best_attacker).name if best_attacker in gs.card_db else "Unknown"
                                logging.debug(f"Strategic block: {blocker_card.name} blocks {attacker_name} (value: {best_block_value:.2f})")
                                
                                # Use the strategic value as reward
                                reward += best_block_value * 0.3
                                
                        # If strategic planner not available or no block was made, use original logic
                        else:
                            if param is not None and param < len(opp["battlefield"]):
                                blocker_id = opp["battlefield"][param]
                                blocker_card = gs._safe_get_card(blocker_id)
                                
                                if blocker_card is None:
                                    logging.warning(f"Invalid blocker card at index {param}.")
                                elif 'creature' not in blocker_card.card_types:
                                    logging.debug(f"Invalid BLOCK action: {blocker_card.name} is not a creature.")
                                    reward -= 0.1
                                else:
                                    # Get block context value
                                    block_value = 0
                                    if hasattr(self, 'card_evaluator'):
                                        if hasattr(self.card_evaluator, 'evaluate_card'):
                                            block_value = self.card_evaluator.evaluate_card(blocker_id, "block")
                                        else:
                                            block_value = self.card_evaluator.get_card_context_value(blocker_id, opp, "block")
                                    
                                    # Check if this creature is already blocking
                                    is_already_blocking = False
                                    for attacker_id, blockers in gs.current_block_assignments.items():
                                        if blocker_id in blockers:
                                            blockers.remove(blocker_id)
                                            logging.debug(f"Removed {blocker_card.name} from blocking {gs._safe_get_card(attacker_id).name}")
                                            is_already_blocking = True
                                            break
                                    
                                    # If not already blocking, assign to an attacker
                                    if not is_already_blocking and gs.current_attackers:
                                        # Find the best attacker to block with this blocker
                                        best_attacker = None
                                        best_block_value = -float('inf')
                                        
                                        for attacker_id in gs.current_attackers:
                                            attacker_card = gs._safe_get_card(attacker_id)
                                            if not attacker_card:
                                                continue
                                                
                                            # Simple analysis: favorable block if we kill it without dying
                                            if blocker_card.power >= attacker_card.toughness and blocker_card.toughness > attacker_card.power:
                                                block_score = 2.0  # Very favorable
                                            # Even trade if both die
                                            elif blocker_card.power >= attacker_card.toughness and blocker_card.toughness <= attacker_card.power:
                                                block_score = 1.0  # Even trade
                                            # We chump block - we die but don't kill it
                                            elif blocker_card.power < attacker_card.toughness and blocker_card.toughness <= attacker_card.power:
                                                # Prioritize blocking based on attacker power
                                                block_score = -0.5 + (attacker_card.power / 3)  # Value blocking bigger threats
                                            else:
                                                block_score = 0  # Other scenarios
                                            
                                            # Consider if this attacker is unblocked - higher priority for threats
                                            if attacker_id not in gs.current_block_assignments:
                                                block_score += 0.75
                                                
                                            if block_score > best_block_value:
                                                best_block_value = block_score
                                                best_attacker = attacker_id
                                        
                                        # Assign to best attacker or fallback to first one
                                        if best_attacker is None and gs.current_attackers:
                                            best_attacker = gs.current_attackers[0]
                                            
                                        if best_attacker:
                                            if best_attacker not in gs.current_block_assignments:
                                                gs.current_block_assignments[best_attacker] = []
                                            
                                            gs.current_block_assignments[best_attacker].append(blocker_id)
                                            attacker_name = gs._safe_get_card(best_attacker).name if best_attacker in gs.card_db else "Unknown"
                                            logging.debug(f"Assigned {blocker_card.name} to block {attacker_name} (value: {block_value:.2f})")
                                            
                                            # Adjust reward based on block value
                                            block_reward = 0.1 * (1 + min(block_value, 1.0))
                                            reward += block_reward
                                            logging.debug(f"Block reward: {block_reward:.2f}")
            
            # Tap land actions (68-87)
            elif action_type == "TAP_LAND":
                if param is not None and param < len(me["battlefield"]):
                    land_id = me["battlefield"][param]
                    if self._tap_land_for_effect(me, land_id):
                        logging.debug(f"Tapped land for effect: {gs._safe_get_card(land_id).name}")
                    else:
                        logging.debug(f"Invalid TAP_LAND action for card at index {param}")
                else:
                    logging.warning(f"Invalid TAP_LAND param: {param} vs battlefield size {len(me['battlefield'])}")
                    
            # Ability activation actions (100-159)
            elif action_type == "ACTIVATE_ABILITY":
                if param is not None and isinstance(param, tuple) and len(param) == 2:
                    card_idx, ability_idx = param
                    if card_idx < len(me["battlefield"]):
                        card_id = me["battlefield"][card_idx]
                        card = gs._safe_get_card(card_id)
                        
                        # Handle Room door unlocking
                        if hasattr(card, 'is_room') and card.is_room and ability_idx == 0:
                            # This is a door unlock action
                            # Check if door2 is locked
                            if hasattr(card, 'door2') and not card.door2.get('unlocked', False):
                                door_cost = card.door2.get('mana_cost', '')
                                
                                # Check if we can afford to unlock
                                can_afford = True
                                if hasattr(gs, 'mana_system'):
                                    can_afford = gs.mana_system.can_pay_mana_cost(me, door_cost)
                                
                                if can_afford:
                                    # Pay the cost
                                    if hasattr(gs, 'mana_system'):
                                        gs.mana_system.pay_mana_cost(me, door_cost)
                                    else:
                                        # Simple cost deduction
                                        me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                    
                                    # Unlock the door
                                    card.door2['unlocked'] = True
                                    
                                    # Trigger door unlocked event
                                    gs.trigger_ability(card_id, "DOOR_UNLOCKED", {"door_number": 2})
                                    
                                    # Reset priority for new abilities
                                    gs.phase = gs.PHASE_PRIORITY
                                    
                                    # Reward for unlocking a door
                                    reward += 0.4
                                    
                                    logging.debug(f"Unlocked door 2 for Room {card.name}")
                                    return reward, done
                        
                        # Handle Class level-up
                        elif hasattr(card, 'is_class') and card.is_class and ability_idx == 1:
                            # This is a level-up action
                            if hasattr(card, 'can_level_up') and card.can_level_up():
                                next_level = card.current_level + 1
                                level_cost = card.get_level_cost(next_level)
                                
                                # Check if we can afford to level up
                                can_afford = True
                                if hasattr(gs, 'mana_system') and level_cost:
                                    can_afford = gs.mana_system.can_pay_mana_cost(me, level_cost)
                                
                                if can_afford:
                                    # Pay the cost
                                    if hasattr(gs, 'mana_system') and level_cost:
                                        gs.mana_system.pay_mana_cost(me, level_cost)
                                    else:
                                        # Simple cost deduction
                                        me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                    
                                    # Level up the class
                                    card.current_level = next_level
                                    
                                    # Trigger level-up event
                                    gs.trigger_ability(card_id, "CLASS_LEVEL_UP", {"level": next_level})
                                    
                                    # Update card characteristics based on new level
                                    level_data = card.get_current_class_data()
                                    if level_data and level_data.get("power") is not None:
                                        # This level turns the class into a creature
                                        card.power = level_data["power"]
                                        card.toughness = level_data["toughness"]
                                        card.type_line = level_data["type_line"]
                                    
                                    # Reset priority for new abilities
                                    gs.phase = gs.PHASE_PRIORITY
                                    
                                    # Reward for leveling up
                                    reward += 0.3 * next_level
                                    
                                    logging.debug(f"Leveled up Class {card.name} to level {next_level}")
                                    return reward, done
                        
                        # Use ability handler to activate
                        if hasattr(gs, 'ability_handler') and gs.ability_handler:
                            # Get the ability first to check costs
                            abilities = gs.ability_handler.get_activated_abilities(card_id)
                            
                            if ability_idx < len(abilities):
                                ability = abilities[ability_idx]
                                cost_text = ability.cost if hasattr(ability, 'cost') else ""
                                
                                # Check if ability cost can be paid using the enhanced method
                                can_pay = True
                                if hasattr(gs.ability_handler, 'can_pay_ability_cost'):
                                    can_pay = gs.ability_handler.can_pay_ability_cost(cost_text, me)
                                
                                if can_pay:
                                    # CRITICAL FIX: Check for activation limits to prevent infinite loops
                                    activation_count = 0
                                    if hasattr(gs, 'abilities_activated_this_turn'):
                                        for activated_id, activated_idx in gs.abilities_activated_this_turn:
                                            if activated_id == card_id and activated_idx == ability_idx:
                                                activation_count += 1
                                    
                                    # Limit to 3 activations per turn to prevent infinite loops
                                    if activation_count >= 3:
                                        logging.debug(f"Ability {ability_idx} of {gs._safe_get_card(card_id).name} has already been activated 3 times; preventing potential infinite loop")
                                        reward -= 0.2  # Penalty for trying to overuse ability
                                        return reward, done
                                    
                                    success = gs.ability_handler.activate_ability(card_id, ability_idx, me)
                                    
                                    if success:
                                        # Get ability details for reward calculation
                                        card = gs._safe_get_card(card_id)
                                        
                                        # Add context-aware rewards where applicable
                                        if hasattr(gs, 'strategic_planner') and gs.strategic_planner is not None:
                                            context = {"action_type": action_type, "param": param}
                                            value, _ = gs.strategic_planner.evaluate_ability_activation(card_id, ability_idx)
                                            reward += value * 0.3
                                                                
                                        logging.debug(f"Activated ability {ability_idx} of {card.name}")
                                        gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                                        
                                        # Track ability activation in turn history
                                        if not hasattr(gs, 'abilities_activated_this_turn'):
                                            gs.abilities_activated_this_turn = []
                                        gs.abilities_activated_this_turn.append((card_id, ability_idx))
                                    else:
                                        reward -= 0.1  # Small penalty for failed activation
                                        logging.debug(f"Failed to activate ability {ability_idx}")
                                else:
                                    reward -= 0.1  # Penalty for trying to activate when can't pay
                                    logging.debug(f"Cannot pay cost for ability {ability_idx}")
                            else:
                                reward -= 0.1  # Penalty for invalid ability index
                                logging.debug(f"Invalid ability index {ability_idx}")
            
            # Transform actions (160-179)
            elif action_type == "TRANSFORM":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, "transform"):
                        card.transform()
                        logging.debug(f"Transformed card: {card.name} (Now {'back' if not card.is_transformed else 'front'} face)")
                    else:
                        logging.warning(f"Transform action: Card {card_id} cannot transform or is not valid.")
                else:
                    logging.warning(f"Transform action: Invalid index {param} for battlefield")
            
            # MDFC back face land actions (180-187)
            elif action_type == "PLAY_MDFC_LAND_BACK":
                if param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'is_mdfc') and card.is_mdfc() and hasattr(card, 'back_face'):
                        back_face = card.back_face
                        # Check if back face is a land
                        if 'land' in back_face.get('type_line', '').lower():
                            if not me["land_played"]:
                                # Remove from hand
                                me["hand"].pop(param)
                                
                                # Add to battlefield using back face
                                me["battlefield"].append(card_id)
                                me["land_played"] = True
                                
                                # Mark as using back face
                                gs.cast_as_back_face.add(card_id)
                                
                                # Store original card properties if needed
                                if not hasattr(card, 'original_front_face'):
                                    card.original_front_face = {
                                        'name': card.name,
                                        'type_line': card.type_line,
                                        'card_types': card.card_types.copy() if hasattr(card, 'card_types') else [],
                                        'subtypes': card.subtypes.copy() if hasattr(card, 'subtypes') else [],
                                        'oracle_text': card.oracle_text if hasattr(card, 'oracle_text') else "",
                                        'colors': card.colors.copy() if hasattr(card, 'colors') else [0, 0, 0, 0, 0]
                                    }
                                
                                # Set card properties to back face
                                card.name = back_face.get('name', card.name)
                                card.type_line = back_face.get('type_line', card.type_line)
                                if 'card_types' in back_face:
                                    card.card_types = back_face['card_types']
                                if 'subtypes' in back_face:
                                    card.subtypes = back_face['subtypes']
                                if 'oracle_text' in back_face:
                                    card.oracle_text = back_face['oracle_text']
                                if 'colors' in back_face:
                                    card.colors = back_face['colors']
                                
                                logging.debug(f"Played MDFC land back face: {card.name}")
                                reward += 0.15  # Reward for land play
                                
                            else:
                                logging.debug(f"Invalid action: already played a land this turn")
                                
                        else:
                            logging.debug(f"Invalid action: back face is not a land")
            
            # MDFC back face spell actions (188-195)
            elif action_type == "PLAY_MDFC_BACK":
                if param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'is_mdfc') and card.is_mdfc() and hasattr(card, 'back_face'):
                        back_face = card.back_face
                        # Check if back face is not a land
                        if 'land' not in back_face.get('type_line', '').lower():
                            # Check if we can afford the back face cost
                            back_face_cost = back_face.get('mana_cost', '')
                            
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, back_face_cost)
                            else:
                                can_afford = sum(me["mana_pool"].values()) > 0
                                
                            if can_afford:
                                # Create a special context for casting the back face
                                context = {"cast_back_face": True}
                                
                                # Move card from hand to stack
                                me["hand"].pop(param)
                                gs.stack.append(("SPELL", card_id, me, context))
                                
                                # Store original card properties if needed
                                if not hasattr(card, 'original_front_face'):
                                    card.original_front_face = {
                                        'name': card.name,
                                        'type_line': card.type_line,
                                        'card_types': card.card_types.copy() if hasattr(card, 'card_types') else [],
                                        'subtypes': card.subtypes.copy() if hasattr(card, 'subtypes') else [],
                                        'oracle_text': card.oracle_text if hasattr(card, 'oracle_text') else "",
                                        'colors': card.colors.copy() if hasattr(card, 'colors') else [0, 0, 0, 0, 0]
                                    }
                                
                                # Pay the cost
                                if hasattr(gs, 'mana_system'):
                                    gs.mana_system.pay_mana_cost(me, back_face_cost)
                                else:
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                
                                # Mark as using back face
                                gs.cast_as_back_face.add(card_id)
                                
                                logging.debug(f"Played back face of MDFC: {back_face.get('name', 'Unknown')}")
                                reward += 0.3  # Reward for successful play
                                
                                gs.phase = gs.PHASE_PRIORITY
                            else:
                                logging.debug(f"Cannot afford back face of MDFC")
                        else:
                            logging.debug(f"Invalid action: back face is a land (use PLAY_MDFC_LAND_BACK)")
            
            # Adventure actions (196-203)
            elif action_type == "PLAY_ADVENTURE":
                if param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'has_adventure') and card.has_adventure():
                        # Get adventure data
                        adventure_data = card.get_adventure_data()
                        
                        if adventure_data:
                            adventure_cost = adventure_data.get('cost', '')
                            
                            # Check if we can afford the adventure cost
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, adventure_cost)
                            else:
                                can_afford_adventure = sum(me["mana_pool"].values()) > 0
                                
                            if can_afford:
                                # Create a special context for casting as adventure
                                context = {"cast_as_adventure": True}
                                
                                # Store original card properties if needed
                                if not hasattr(card, 'original_oracle_text'):
                                    card.original_oracle_text = card.oracle_text if hasattr(card, 'oracle_text') else ""
                                    
                                # Set oracle text to adventure effect
                                card.oracle_text = adventure_data.get('effect', card.oracle_text)
                                
                                # Move card from hand to stack
                                me["hand"].pop(param)
                                gs.stack.append(("SPELL", card_id, me, context))
                                
                                # Pay the cost
                                if hasattr(gs, 'mana_system'):
                                    gs.mana_system.pay_mana_cost(me, adventure_cost)
                                else:
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                
                                # Mark as adventure for exile handling
                                gs.adventure_cards.add(card_id)
                                
                                logging.debug(f"Played {card.name} as adventure: {adventure_data.get('name', 'Unknown')}")
                                reward += 0.3  # Reward for successful play
                                
                                gs.phase = gs.PHASE_PRIORITY
                            else:
                                logging.debug(f"Cannot afford adventure cost")
                        else:
                            logging.debug(f"No adventure data found for card")
            
            # Cast from exile actions (230-237)
            elif action_type == "CAST_FROM_EXILE":
                if param is not None and hasattr(gs, 'cards_castable_from_exile'):
                    # Get the list of castable cards from exile
                    castable_cards = list(gs.cards_castable_from_exile)
                    if param < len(castable_cards):
                        card_id = castable_cards[param]
                        
                        if card_id in me["exile"]:
                            card = gs._safe_get_card(card_id)
                            if not card:
                                return reward, done
                            
                            # Check if we can afford to cast it
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, card.mana_cost if hasattr(card, 'mana_cost') else "")
                            else:
                                can_afford = sum(me["mana_pool"].values()) > 0
                            
                            if can_afford:
                                # Remove from exile
                                me["exile"].remove(card_id)
                                
                                # Add to stack
                                gs.stack.append(("SPELL", card_id, me))
                                
                                # Pay the cost
                                if hasattr(gs, 'mana_system'):
                                    gs.mana_system.pay_mana_cost(me, card.mana_cost if hasattr(card, 'mana_cost') else "")
                                else:
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                
                                # Remove from castable tracking
                                gs.cards_castable_from_exile.remove(card_id)
                                
                                # Restore original oracle text if needed
                                if hasattr(card, 'original_oracle_text'):
                                    card.oracle_text = card.original_oracle_text
                                
                                logging.debug(f"Cast {card.name} from exile")
                                reward += 0.35  # Extra reward for exile casting
                                
                                gs.phase = gs.PHASE_PRIORITY
                            else:
                                logging.debug(f"Cannot afford to cast from exile")
            
            # Discard actions (238-247)
            elif action_type == "DISCARD_CARD":
                if param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card:
                        # Move card from hand to graveyard
                        me["hand"].pop(param)
                        me["graveyard"].append(card_id)
                        
                        # Trigger discard effects
                        if hasattr(gs, 'trigger_ability'):
                            gs.trigger_ability(card_id, "DISCARD", {"controller": me})
                        
                        logging.debug(f"Discarded card: {card.name}")
                        
                        # Small reward for successful discard
                        reward += 0.05
                    else:
                        logging.debug(f"Invalid discard - card not found at index {param}")
                else:
                    logging.debug(f"Invalid discard index: {param} vs hand size {len(me['hand'])}")

            # Room door actions (248-252)
            elif action_type == "UNLOCK_DOOR":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'is_room') and card.is_room:
                        # Check if door2 is locked
                        if hasattr(card, 'door2') and not card.door2.get('unlocked', False):
                            door_cost = card.door2.get('mana_cost', '')
                            
                            # Check if we can afford to unlock
                            can_afford = True
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, door_cost)
                            
                            if can_afford:
                                # Pay the cost
                                if hasattr(gs, 'mana_system'):
                                    gs.mana_system.pay_mana_cost(me, door_cost)
                                else:
                                    # Simple cost deduction
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                
                                # Unlock the door
                                card.door2['unlocked'] = True
                                
                                # Trigger door unlocked event
                                if hasattr(gs, 'trigger_ability'):
                                    gs.trigger_ability(card_id, "DOOR_UNLOCKED", {"door_number": 2})
                                
                                # Reset priority for new abilities
                                gs.phase = gs.PHASE_PRIORITY
                                
                                # Reward for unlocking a door
                                reward += 0.4
                                
                                logging.debug(f"Unlocked door 2 for Room {card.name}")
                            else:
                                logging.debug(f"Cannot afford to unlock door for {card.name}")
                        else:
                            logging.debug(f"Door is already unlocked or not available")
                    else:
                        logging.debug(f"Not a Room card")
                else:
                    logging.debug(f"Invalid Room index: {param} vs battlefield size {len(me['battlefield'])}")
            
            # Class level actions (253-257)
            elif action_type == "LEVEL_UP_CLASS":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'is_class') and card.is_class:
                        # Check if the Class can level up
                        if hasattr(card, 'can_level_up') and card.can_level_up():
                            next_level = card.current_level + 1
                            level_cost = card.get_level_cost(next_level)
                            
                            # Check if we can afford to level up
                            can_afford = True
                            if hasattr(gs, 'mana_system') and level_cost:
                                can_afford = gs.mana_system.can_pay_mana_cost(me, level_cost)
                            
                            if can_afford:
                                # Pay the cost
                                if hasattr(gs, 'mana_system') and level_cost:
                                    gs.mana_system.pay_mana_cost(me, level_cost)
                                else:
                                    # Simple cost deduction
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                
                                # Level up the class
                                card.current_level = next_level
                                
                                # Trigger level-up event
                                if hasattr(gs, 'trigger_ability'):
                                    gs.trigger_ability(card_id, "CLASS_LEVEL_UP", {"level": next_level})
                                
                                # Update card characteristics based on new level
                                if hasattr(card, 'get_current_class_data'):
                                    level_data = card.get_current_class_data()
                                    if level_data and level_data.get("power") is not None:
                                        # This level turns the class into a creature
                                        card.power = level_data["power"]
                                        card.toughness = level_data["toughness"]
                                        card.type_line = level_data["type_line"]
                                
                                # Reset priority for new abilities
                                gs.phase = gs.PHASE_PRIORITY
                                
                                # Reward for leveling up
                                reward += 0.3 * next_level
                                
                                logging.debug(f"Leveled up Class {card.name} to level {next_level}")
                            else:
                                logging.debug(f"Cannot afford to level up {card.name}")
                        else:
                            logging.debug(f"Class cannot level up further")
                    else:
                        logging.debug(f"Not a Class card")
                else:
                    logging.debug(f"Invalid Class index: {param} vs battlefield size {len(me['battlefield'])}")
            
            # Spree mode selection actions (258-273)
            elif action_type == "SELECT_SPREE_MODE":
                if isinstance(param, tuple) and len(param) == 2:
                    card_idx, mode_idx = param
                    
                    # Check if we have a Spree spell on the stack
                    if gs.stack and len(gs.stack) > 0:
                        stack_item = gs.stack[-1]  # Get top of stack
                        
                        if isinstance(stack_item, tuple) and len(stack_item) >= 4:
                            stack_type, card_id, controller, context = stack_item
                            
                            # Check if it's a spree spell
                            if stack_type == "SPELL" and context and "is_spree" in context:
                                card = gs._safe_get_card(card_id)
                                
                                if card and hasattr(card, 'is_spree') and card.is_spree and hasattr(card, 'spree_modes'):
                                    # Check if mode is valid
                                    if card_idx < 8 and mode_idx < 2 and len(card.spree_modes) > mode_idx:
                                        mode = card.spree_modes[mode_idx]
                                        mode_cost = mode.get('cost', '')
                                        
                                        # Check if we can afford this mode
                                        can_afford = True
                                        if hasattr(gs, 'mana_system'):
                                            can_afford = gs.mana_system.can_pay_mana_cost(me, mode_cost)
                                        
                                        # Check if mode is already selected
                                        selected_modes = context.get("selected_modes", [])
                                        if mode_idx in selected_modes:
                                            # Mode already selected, remove it
                                            selected_modes.remove(mode_idx)
                                            
                                            # Refund the cost
                                            if hasattr(gs, 'mana_system'):
                                                gs.mana_system.add_mana_to_pool(me, mode_cost)
                                            
                                            logging.debug(f"Removed Spree mode {mode_idx} from {card.name}")
                                            
                                            # Update context
                                            stack_item = (stack_type, card_id, controller, {"is_spree": True, "selected_modes": selected_modes})
                                            gs.stack[-1] = stack_item
                                            
                                            # Small reward for refining spell
                                            reward += 0.05
                                        elif can_afford:
                                            # Add mode to selection
                                            selected_modes.append(mode_idx)
                                            
                                            # Pay the cost
                                            if hasattr(gs, 'mana_system'):
                                                gs.mana_system.pay_mana_cost(me, mode_cost)
                                            
                                            logging.debug(f"Added Spree mode {mode_idx} to {card.name}")
                                            
                                            # Update context
                                            stack_item = (stack_type, card_id, controller, {"is_spree": True, "selected_modes": selected_modes})
                                            gs.stack[-1] = stack_item
                                            
                                            # Reward for adding mode
                                            reward += 0.2
                                        else:
                                            logging.debug(f"Cannot afford Spree mode {mode_idx}")
                                    else:
                                        logging.debug(f"Invalid Spree mode index: {mode_idx}")
                                else:
                                    logging.debug(f"Not a Spree card")
                            else:
                                logging.debug(f"Top of stack is not a Spree spell")
                        else:
                            logging.debug(f"Invalid stack item")
                    else:
                        logging.debug(f"Stack is empty")
                else:
                    logging.debug(f"Invalid Spree mode parameter")
            
            # Select target actions (274-283)
            elif action_type == "SELECT_TARGET":
                if param is not None and param < 10:  # Up to 10 target indices
                    # Check if we have a spell or ability on the stack that needs targeting
                    if gs.stack and len(gs.stack) > 0:
                        stack_item = gs.stack[-1]  # Get top of stack
                        
                        if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                            stack_type, card_id, controller = stack_item[:3]
                            card = gs._safe_get_card(card_id)
                            
                            if card:
                                # Get available targets
                                available_targets = []
                                
                                # Check if we have a targeting system
                                if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'targeting_system'):
                                    # Use targeting system to get valid targets
                                    targets = gs.ability_handler.targeting_system.get_valid_targets(card_id)
                                    
                                    if targets:
                                        # Flatten targets into a list
                                        for target_type, target_list in targets.items():
                                            available_targets.extend(target_list)
                                else:
                                    # Simple targeting - all permanents and players
                                    for player in [gs.p1, gs.p2]:
                                        for permanent_id in player["battlefield"]:
                                            available_targets.append(permanent_id)
                                    available_targets.append("p1")  # Player 1
                                    available_targets.append("p2")  # Player 2
                                
                                # Select target by index
                                if param < len(available_targets):
                                    target_id = available_targets[param]
                                    
                                    # Get target type (creature, player, etc.)
                                    target_type = "unknown"
                                    if target_id in ["p1", "p2"]:
                                        target_type = "player"
                                    else:
                                        target_card = gs._safe_get_card(target_id)
                                        if target_card and hasattr(target_card, 'card_types'):
                                            for card_type in target_card.card_types:
                                                target_type = card_type
                                                break
                                    
                                    # Create targeting context
                                    targeting_context = {"targets": {target_type: [target_id]}}
                                    
                                    # Update stack item with targeting info
                                    if len(stack_item) >= 4:
                                        # Preserve existing context
                                        existing_context = stack_item[3]
                                        if isinstance(existing_context, dict):
                                            existing_context.update(targeting_context)
                                            new_stack_item = (stack_type, card_id, controller, existing_context)
                                        else:
                                            new_stack_item = (stack_type, card_id, controller, targeting_context)
                                    else:
                                        new_stack_item = (stack_type, card_id, controller, targeting_context)
                                    
                                    gs.stack[-1] = new_stack_item
                                    
                                    target_name = "Player " + target_id[1] if target_id in ["p1", "p2"] else (
                                        gs._safe_get_card(target_id).name if gs._safe_get_card(target_id) and hasattr(gs._safe_get_card(target_id), 'name') else "Unknown"
                                    )
                                    logging.debug(f"Selected target: {target_name}")
                                    
                                    # Reward for target selection
                                    reward += 0.1
                                else:
                                    logging.debug(f"Invalid target index: {param} vs {len(available_targets)} available targets")
                            else:
                                logging.debug(f"Invalid card on stack")
                        else:
                            logging.debug(f"Invalid stack item")
                    else:
                        logging.debug(f"Stack is empty")
                else:
                    logging.debug(f"Invalid target index")
            
            # Sacrifice selection actions (284-293)
            elif action_type == "SACRIFICE_PERMANENT":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card:
                        # Check if sacrifice is part of a cost or effect
                        requires_sacrifice = False
                        
                        # Check stack for sacrifice requirements
                        if gs.stack and len(gs.stack) > 0:
                            stack_item = gs.stack[-1]
                            
                            if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                                stack_type, stack_card_id, stack_controller = stack_item[:3]
                                stack_card = gs._safe_get_card(stack_card_id)
                                
                                if stack_card and hasattr(stack_card, 'oracle_text'):
                                    if "sacrifice" in stack_card.oracle_text.lower():
                                        requires_sacrifice = True
                        
                        # Move card from battlefield to graveyard
                        me["battlefield"].remove(card_id)
                        me["graveyard"].append(card_id)
                        
                        # Trigger sacrifice effects
                        if hasattr(gs, 'trigger_ability'):
                            gs.trigger_ability(card_id, "SACRIFICE", {"controller": me})
                        
                        logging.debug(f"Sacrificed permanent: {card.name}")
                        
                        # Reward based on whether sacrifice was required
                        if requires_sacrifice:
                            reward += 0.1  # Reward for fulfilling requirement
                        else:
                            reward -= 0.1  # Small penalty for unnecessary sacrifice
                    else:
                        logging.debug(f"Invalid permanent to sacrifice")
                else:
                    logging.debug(f"Invalid sacrifice index: {param} vs battlefield size {len(me['battlefield'])}")
                    
            elif action_type == "PUT_ON_TOP":
                # Check if we're in a scry or surveil process
                is_scry = hasattr(gs, 'scry_in_progress') and gs.scry_in_progress
                is_surveil = hasattr(gs, 'surveil_in_progress') and gs.surveil_in_progress
                
                if not (is_scry or is_surveil):
                    logging.debug("PUT_ON_TOP action is only valid during scry or surveil")
                    return reward, done
                    
                # Handle scry
                if is_scry and hasattr(gs, 'scrying_cards'):
                    if not gs.scrying_cards:
                        # No more cards to decide on, end scry
                        gs.scry_in_progress = False
                        logging.debug("Scry completed - all cards processed")
                        return reward, done
                        
                    # Take the top card being scried
                    card_id = gs.scrying_cards.pop(0)
                    card = gs._safe_get_card(card_id)
                    
                    # Keep it on top (no action needed since it's already there)
                    logging.debug(f"Scry: Kept {card.name if card and hasattr(card, 'name') else card_id} on top of library")
                    
                    # Check if we're done
                    if not gs.scrying_cards:
                        gs.scry_in_progress = False
                        logging.debug("Scry completed")
                    
                    reward += 0.05  # Small reward for decision
                
                # Handle surveil
                elif is_surveil and hasattr(gs, 'cards_being_surveiled'):
                    if not gs.cards_being_surveiled:
                        # No more cards to decide on, end surveil
                        gs.surveil_in_progress = False
                        logging.debug("Surveil completed - all cards processed")
                        return reward, done
                        
                    # Take the top card being surveiled
                    card_id = gs.cards_being_surveiled.pop(0)
                    card = gs._safe_get_card(card_id)
                    
                    # Keep it on top (no action needed since it's already there)
                    logging.debug(f"Surveil: Kept {card.name if card and hasattr(card, 'name') else card_id} on top of library")
                    
                    # Trigger any surveil-related abilities
                    if hasattr(gs, 'trigger_ability'):
                        context = {"card_id": card_id, "to_graveyard": False}
                        for permanent_id in player["battlefield"]:
                            gs.trigger_ability(permanent_id, "SURVEIL", context)
                    
                    # Check if we're done
                    if not gs.cards_being_surveiled:
                        gs.surveil_in_progress = False
                        logging.debug("Surveil completed")
                    
                    reward += 0.05  # Small reward for decision
                
                return reward, done

            elif action_type == "PUT_TO_GRAVEYARD":
                # Only valid during surveil
                if not hasattr(gs, 'surveil_in_progress') or not gs.surveil_in_progress:
                    logging.debug("PUT_TO_GRAVEYARD action is only valid during surveil")
                    return reward, done
                
                if not gs.cards_being_surveiled:
                    # No more cards to decide on, end surveil
                    gs.surveil_in_progress = False
                    logging.debug("Surveil completed - all cards processed")
                    return reward, done
                    
                # Take the top card being surveiled
                card_id = gs.cards_being_surveiled.pop(0)
                card = gs._safe_get_card(card_id)
                
                # Move it to the graveyard
                player["library"].remove(card_id)
                player["graveyard"].append(card_id)
                logging.debug(f"Surveil: Put {card.name if card and hasattr(card, 'name') else card_id} into graveyard")
                
                # Trigger any surveil-related abilities
                if hasattr(gs, 'trigger_ability'):
                    context = {"card_id": card_id, "to_graveyard": True}
                    for permanent_id in player["battlefield"]:
                        gs.trigger_ability(permanent_id, "SURVEIL", context)
                
                # Check if we're done
                if not gs.cards_being_surveiled:
                    gs.surveil_in_progress = False
                    logging.debug("Surveil completed")
                
                reward += 0.05  # Small reward for decision
                return reward, done
            

            elif action_type == "PUT_TO_GRAVEYARD":
                # Only valid during surveil
                if not hasattr(gs, 'surveil_in_progress') or not gs.surveil_in_progress:
                    logging.debug("PUT_TO_GRAVEYARD action is only valid during surveil")
                    return reward, done
                
                if not hasattr(gs, 'cards_being_surveiled') or not gs.cards_being_surveiled:
                    # No more cards to decide on, end surveil
                    gs.surveil_in_progress = False
                    logging.debug("Surveil completed - all cards processed")
                    return reward, done
                    
                # Take the top card being surveiled
                card_id = gs.cards_being_surveiled.pop(0)
                card = gs._safe_get_card(card_id)
                
                # Move it to the graveyard
                player["library"].remove(card_id)
                player["graveyard"].append(card_id)
                logging.debug(f"Surveil: Put {card.name if card and hasattr(card, 'name') else card_id} into graveyard")
                
                # Trigger any surveil-related abilities
                if hasattr(gs, 'trigger_ability'):
                    context = {"card_id": card_id, "to_graveyard": True}
                    for permanent_id in player["battlefield"]:
                        gs.trigger_ability(permanent_id, "SURVEIL", context)
                
                # Check if we're done
                if not gs.cards_being_surveiled:
                    gs.surveil_in_progress = False
                    logging.debug("Surveil completed")
                
                reward += 0.05  # Small reward for decision
                return reward, done
            
            # Library search actions (299-303)
            elif action_type == "SEARCH_LIBRARY":
                search_types = ["land", "creature", "instant", "sorcery", "any card"]
                if param is not None and param < len(search_types):
                    search_type = search_types[param]
                    # This would typically trigger a UI for the player to select a card
                    # For the AI, we can implement a simple search algorithm
                    found_card = None
                    for idx, card_id in enumerate(me["library"]):
                        card = gs._safe_get_card(card_id)
                        if not card or not hasattr(card, 'type_line'):
                            continue
                            
                        # Check if card matches the search type
                        if search_type == "any card" or search_type in card.type_line.lower():
                            found_card = card_id
                            me["library"].remove(card_id)
                            me["hand"].append(card_id)
                            reward += 0.2  # Significant reward for successful search
                            logging.debug(f"Searched library and found {card.name}")
                            break
                            
                    if not found_card:
                        logging.debug(f"Search library: No matching {search_type} found")
                        
                    # Shuffle library after searching
                    import random
                    random.shuffle(me["library"])
                    logging.debug("Library shuffled after search")
                else:
                    logging.warning(f"Invalid search type index: {param}")
            
            elif action_type == "DREDGE":
                # Logic for using the dredge ability from graveyard
                if param is not None and param < len(me["graveyard"]):
                    card_id = me["graveyard"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'oracle_text') and "dredge" in card.oracle_text.lower():
                        # Parse dredge value
                        import re
                        match = re.search(r"dredge (\d+)", card.oracle_text.lower())
                        if match:
                            dredge_value = int(match.group(1))
                            
                            # Check if enough cards in library
                            if len(me["library"]) >= dredge_value:
                                # Mill cards
                                for _ in range(dredge_value):
                                    if me["library"]:
                                        milled_card = me["library"].pop(0)
                                        me["graveyard"].append(milled_card)
                                        
                                # Return dredge card to hand
                                me["graveyard"].remove(card_id)
                                me["hand"].append(card_id)
                                
                                logging.debug(f"Dredged {card.name} by milling {dredge_value} cards")
                                reward += 0.2  # Reward for successful dredge
                            else:
                                logging.debug(f"Not enough cards in library to dredge {card.name}")
                        else:
                            logging.debug(f"Invalid dredge text on {card.name}")
                    else:
                        logging.debug(f"Card does not have dredge ability")
            
            # Counter management (309-318)
            elif action_type == "ADD_COUNTER":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    # Default to +1/+1 counters for creatures, loyalty for planeswalkers
                    card = gs._safe_get_card(card_id)
                    if card:
                        counter_type = "loyalty" if 'planeswalker' in card.card_types else "+1/+1"
                        if gs.add_counter(card_id, counter_type, 1):
                            reward += 0.1  # Small reward for adding a counter
                            logging.debug(f"Added {counter_type} counter to {card.name}")
                        else:
                            logging.debug(f"Failed to add counter to {card.name}")
                else:
                    logging.warning(f"Invalid counter target index: {param}")
            
            # Counter removal (319-328)
            elif action_type == "REMOVE_COUNTER":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    # Default to -1/-1 counters for creatures, loyalty for planeswalkers
                    card = gs._safe_get_card(card_id)
                    if card:
                        counter_type = "loyalty" if 'planeswalker' in card.card_types else "-1/-1"
                        if gs.add_counter(card_id, counter_type, -1):  # Use negative value to remove
                            reward += 0.1  # Small reward for counter manipulation
                            logging.debug(f"Removed {counter_type} counter from {card.name}")
                        else:
                            logging.debug(f"Failed to remove counter from {card.name}")
                else:
                    logging.warning(f"Invalid counter target index: {param}")
            
            # Proliferate (329)
            elif action_type == "PROLIFERATE":
                # Logic for proliferate - add a counter of each kind already there
                counter_added = False
                for player in [me, opp]:
                    for card_id in player["battlefield"]:
                        card = gs._safe_get_card(card_id)
                        if card and hasattr(card, 'counters') and card.counters:
                            for counter_type, count in card.counters.items():
                                if count > 0:
                                    # Add one more of each counter type
                                    card.counters[counter_type] += 1
                                    counter_added = True
                                    
                                    # Apply counter effects
                                    if counter_type == "+1/+1" and hasattr(card, 'power') and hasattr(card, 'toughness'):
                                        card.power += 1
                                        card.toughness += 1
                                    elif counter_type == "-1/-1" and hasattr(card, 'power') and hasattr(card, 'toughness'):
                                        card.power -= 1
                                        card.toughness -= 1
                                        
                                    logging.debug(f"Proliferate: Added a {counter_type} counter to {card.name}")
                    
                    # Check for poison counters on players
                    if hasattr(player, 'poison_counters') and player['poison_counters'] > 0:
                        player['poison_counters'] += 1
                        counter_added = True
                        logging.debug(f"Proliferate: Added a poison counter to a player")
                
                if counter_added:
                    reward += 0.2  # Reward for successful proliferate
                else:
                    logging.debug("Proliferate: No counters to add")
            
            # Zone movement - Graveyard recovery (330-335)
            elif action_type == "RETURN_FROM_GRAVEYARD":
                if param is not None and param < len(me["graveyard"]):
                    card_id = me["graveyard"][param]
                    me["graveyard"].remove(card_id)
                    me["hand"].append(card_id)
                    logging.debug(f"Returned {gs._safe_get_card(card_id).name if gs._safe_get_card(card_id) else 'card'} from graveyard to hand")
                    reward += 0.2  # Reward for card advantage
            
            # Reanimation (336-341)
            elif action_type == "REANIMATE":
                if param is not None and param < len(me["graveyard"]):
                    card_id = me["graveyard"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                        me["graveyard"].remove(card_id)
                        me["battlefield"].append(card_id)
                        me["entered_battlefield_this_turn"].add(card_id)
                        logging.debug(f"Reanimated {card.name} from graveyard to battlefield")
                        reward += 0.3  # Significant reward for reanimation
                    else:
                        logging.debug(f"Cannot reanimate non-creature card")
            
            # Exile zone interactions (342-347)
            elif action_type == "RETURN_FROM_EXILE":
                if param is not None and param < len(me["exile"]):
                    card_id = me["exile"][param]
                    me["exile"].remove(card_id)
                    me["hand"].append(card_id)
                    logging.debug(f"Returned {gs._safe_get_card(card_id).name if gs._safe_get_card(card_id) else 'card'} from exile to hand")
                    reward += 0.2  # Reward for card advantage
            
            # Modal choices (348-357)
            elif action_type == "CHOOSE_MODE":
                if param is not None and param < 10:  # Up to 10 mode indices
                    # Check if we have a modal spell or ability on the stack
                    if gs.stack and len(gs.stack) > 0:
                        stack_item = gs.stack[-1]
                        
                        if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                            stack_type, card_id, controller = stack_item[:3]
                            card = gs._safe_get_card(card_id)
                            
                            if card and hasattr(card, 'oracle_text'):
                                # Check if this is a modal card
                                oracle_text = card.oracle_text.lower()
                                if "choose one" in oracle_text or "choose two" in oracle_text or "choose one or more" in oracle_text:
                                    # Parse available modes
                                    modes = []
                                    for line in oracle_text.split('\n'):
                                        if line.strip().startswith('•'):
                                            modes.append(line.strip()[1:].strip())
                                    
                                    if param < len(modes):
                                        # Select this mode
                                        mode_text = modes[param]
                                        
                                        # Update stack item with mode selection
                                        if len(stack_item) >= 4:
                                            context = stack_item[3]
                                            if isinstance(context, dict):
                                                if "selected_modes" not in context:
                                                    context["selected_modes"] = []
                                                context["selected_modes"].append(param)
                                            else:
                                                context = {"selected_modes": [param]}
                                            new_stack_item = (stack_type, card_id, controller, context)
                                        else:
                                            new_stack_item = (stack_type, card_id, controller, {"selected_modes": [param]})
                                        
                                        gs.stack[-1] = new_stack_item
                                        logging.debug(f"Selected mode: {mode_text}")
                                        reward += 0.1  # Small reward for mode selection
                                    else:
                                        logging.debug(f"Invalid mode index: {param}")
                                else:
                                    logging.debug(f"Not a modal card: {card.name}")
                            else:
                                logging.debug(f"Invalid card on stack")
                        else:
                            logging.debug(f"Invalid stack item")
                    else:
                        logging.debug(f"Stack is empty")
                else:
                    logging.debug(f"Invalid mode index")
                    
            elif action_type == "MULLIGAN":
                # Perform a mulligan
                if gs.perform_mulligan(me):
                    reward -= 0.05  # Small penalty for taking a mulligan
                    logging.debug("Player chose to mulligan")
                else:
                    logging.debug("Failed to perform mulligan")
                    reward -= 0.1
                    
            elif action_type == "KEEP_HAND":
                # Keep current hand
                if hasattr(gs, 'mulligan_in_progress') and gs.mulligan_in_progress:
                    gs.perform_mulligan(me, keep_hand=True)
                    reward += 0.1  # Small reward for keeping hand
                    logging.debug("Player chose to keep hand")
                else:
                    logging.debug("Not in mulligan phase")
                    reward -= 0.1
                    
            elif action_type == "BOTTOM_CARD":
                # Bottom a card during mulligan resolution
                if param is not None and param < len(me["hand"]):
                    if gs.bottom_card(me, param):
                        reward += 0.01  # Small reward for successful bottoming
                        logging.debug(f"Bottomed card at index {param} during mulligan")
                    else:
                        logging.debug(f"Failed to bottom card at index {param}")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid card index {param} for bottoming")
                    reward -= 0.1
            
            # X spell value selection (358-367)
            elif action_type == "CHOOSE_X_VALUE":
                if param is not None and param > 0:
                    # Param is X value from 1-10
                    if gs.stack and len(gs.stack) > 0:
                        stack_item = gs.stack[-1]
                        
                        if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                            stack_type, card_id, controller = stack_item[:3]
                            card = gs._safe_get_card(card_id)
                            
                            if card and controller == me:
                                x_value = param
                                
                                # Update stack item with X value
                                if len(stack_item) >= 4:
                                    context = stack_item[3]
                                    if isinstance(context, dict):
                                        context["X"] = x_value
                                    else:
                                        context = {"X": x_value}
                                    new_stack_item = (stack_type, card_id, controller, context)
                                else:
                                    new_stack_item = (stack_type, card_id, controller, {"X": x_value})
                                
                                gs.stack[-1] = new_stack_item
                                
                                # Calculate and pay additional mana for X
                                if hasattr(gs, 'mana_system'):
                                    # Pay X mana (additional to base cost already paid)
                                    x_cost = {'generic': x_value}
                                    gs.mana_system.pay_mana_cost(me, x_cost)
                                else:
                                    # Simple deduction
                                    remaining_x = x_value
                                    if me["mana_pool"].get('C', 0) > 0:
                                        colorless_used = min(me["mana_pool"]['C'], remaining_x)
                                        me["mana_pool"]['C'] -= colorless_used
                                        remaining_x -= colorless_used
                                    
                                    # Use colored mana if needed
                                    if remaining_x > 0:
                                        for color in ['G', 'R', 'B', 'U', 'W']:
                                            if remaining_x <= 0:
                                                break
                                            if me["mana_pool"].get(color, 0) > 0:
                                                color_used = min(me["mana_pool"][color], remaining_x)
                                                me["mana_pool"][color] -= color_used
                                                remaining_x -= color_used
                                
                                logging.debug(f"Chose X value: {x_value} for {card.name}")
                                
                                # Reward based on X value - more for higher X
                                reward += min(0.05 * x_value, 0.5)
                            else:
                                logging.debug(f"Not your spell or invalid card")
                        else:
                            logging.debug(f"Invalid stack item")
                    else:
                        logging.debug(f"Stack is empty")
                else:
                    logging.debug(f"Invalid X value: {param}")
            
            # Color choices (368-372)
            elif action_type == "CHOOSE_COLOR":
                if param is not None and param < 5:
                    colors = ["white", "blue", "black", "red", "green"]
                    color = colors[param]
                    
                    # Update stack context with color choice
                    if gs.stack and len(gs.stack) > 0:
                        stack_item = gs.stack[-1]
                        
                        if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                            stack_type, card_id, controller = stack_item[:3]
                            
                            # Update stack item with color choice
                            if len(stack_item) >= 4:
                                context = stack_item[3]
                                if isinstance(context, dict):
                                    context["chosen_color"] = color
                                else:
                                    context = {"chosen_color": color}
                                new_stack_item = (stack_type, card_id, controller, context)
                            else:
                                new_stack_item = (stack_type, card_id, controller, {"chosen_color": color})
                            
                            gs.stack[-1] = new_stack_item
                            logging.debug(f"Chose color: {color}")
                            
                            # Small reward for choosing a color
                            reward += 0.05
                        else:
                            logging.debug(f"Invalid stack item")
                    else:
                        logging.debug(f"Stack is empty")
                else:
                    logging.debug(f"Invalid color index: {param}")
            
            # Multi-block assignment (383-392)
            elif action_type == "ASSIGN_MULTIPLE_BLOCKERS":
                if param is not None and param < len(gs.current_attackers):
                    attacker_id = gs.current_attackers[param]
                    attacker = gs._safe_get_card(attacker_id)
                    
                    # Set this as the current attacker being multi-blocked
                    if not hasattr(gs, 'multi_block_attacker'):
                        gs.multi_block_attacker = attacker_id
                        
                        # Reset current blockers for this attacker
                        if attacker_id in gs.current_block_assignments:
                            gs.current_block_assignments[attacker_id] = []
                        else:
                            gs.current_block_assignments[attacker_id] = []
                        
                        logging.debug(f"Setting up multi-block for {attacker.name}")
                        
                        # This action enables selecting multiple blockers
                        # The actual blockers will be selected with BLOCK actions
                    else:
                        # Already in multi-block mode, finish it
                        multi_block_attacker = gs.multi_block_attacker
                        blockers = gs.current_block_assignments.get(multi_block_attacker, [])
                        
                        logging.debug(f"Completed multi-block with {len(blockers)} blockers")
                        reward += 0.1 * len(blockers)  # Reward based on number of blockers assigned
                        
                        # Clear multi-block mode
                        delattr(gs, 'multi_block_attacker')
                else:
                    logging.debug(f"Invalid attacker index for multi-block: {param}")
            
            # Alternative casting methods (393-399)
            elif action_type == "CAST_WITH_FLASHBACK":
                if param is not None and param < len(me["graveyard"]):
                    card_id = me["graveyard"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'oracle_text') and "flashback" in card.oracle_text.lower():
                        # Parse flashback cost
                        import re
                        match = re.search(r"flashback [^\(]([^\)]+)", card.oracle_text.lower())
                        flashback_cost = match.group(1) if match else None
                        
                        if flashback_cost and hasattr(gs, 'mana_system'):
                            # Check if we can afford the flashback cost
                            flashback_parsed = gs.mana_system.parse_mana_cost(flashback_cost)
                            
                            if gs.mana_system.can_pay_mana_cost(me, flashback_parsed):
                                # Pay the cost
                                gs.mana_system.pay_mana_cost(me, flashback_parsed)
                                
                                # Move from graveyard to stack
                                me["graveyard"].remove(card_id)
                                gs.stack.append(("SPELL", card_id, me, {"flashback": True}))
                                
                                logging.debug(f"Cast {card.name} with flashback")
                                reward += 0.3  # Reward for flashback casting
                                
                                # Flag for exile instead of graveyard
                                if not hasattr(gs, 'flashback_cards'):
                                    gs.flashback_cards = set()
                                gs.flashback_cards.add(card_id)
                            else:
                                logging.debug(f"Cannot afford flashback cost")
                        else:
                            logging.debug(f"Invalid flashback cost")
                    else:
                        logging.debug(f"Card does not have flashback")
            
            elif action_type == "CAST_WITH_JUMP_START":
                if param is not None and param < len(me["graveyard"]):
                    card_id = me["graveyard"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'oracle_text') and "jump-start" in card.oracle_text.lower():
                        # Jump-start uses the same cost as the original spell
                        card_cost = card.mana_cost if hasattr(card, 'mana_cost') else ""
                        
                        # Check if we can afford and have a card to discard
                        if len(me["hand"]) > 0 and hasattr(gs, 'mana_system'):
                            if gs.mana_system.can_pay_mana_cost(me, card_cost):
                                # Pay the cost
                                gs.mana_system.pay_mana_cost(me, card_cost)
                                
                                # Discard a card (in a real game, player would choose)
                                discard_idx = 0
                                discard_id = me["hand"][discard_idx]
                                me["hand"].pop(discard_idx)
                                me["graveyard"].append(discard_id)
                                
                                # Move from graveyard to stack
                                me["graveyard"].remove(card_id)
                                gs.stack.append(("SPELL", card_id, me, {"jump_start": True}))
                                
                                logging.debug(f"Cast {card.name} with jump-start, discarded a card")
                                reward += 0.3  # Reward for jump-start casting
                                
                                # Flag for exile instead of graveyard
                                if not hasattr(gs, 'jump_start_cards'):
                                    gs.jump_start_cards = set()
                                gs.jump_start_cards.add(card_id)
                            else:
                                logging.debug(f"Cannot afford jump-start cost")
                        else:
                            logging.debug(f"Need a card to discard for jump-start")
                    else:
                        logging.debug(f"Card does not have jump-start")

            elif action_type == "CAST_WITH_ESCAPE":
                # Logic for casting a spell from graveyard with escape
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CAST_WITH_ESCAPE action is not fully implemented")

            elif action_type == "CAST_FOR_MADNESS":
                # Logic for casting a spell for its madness cost 
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CAST_FOR_MADNESS action is not fully implemented")

            elif action_type == "CAST_WITH_OVERLOAD":
                # Logic for casting a spell with overload
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CAST_WITH_OVERLOAD action is not fully implemented")

            elif action_type == "CAST_FOR_EMERGE":
                # Logic for casting a spell for its emerge cost
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CAST_FOR_EMERGE action is not fully implemented")

            elif action_type == "CAST_FOR_DELVE":
                # Logic for casting a spell with delve
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CAST_FOR_DELVE action is not fully implemented")

            elif action_type == "PAY_KICKER":
                # Logic for paying the kicker cost for a spell
                # WARNING: This action is not fully implemented in the current code
                logging.warning("PAY_KICKER action is not fully implemented")

            elif action_type == "PAY_ADDITIONAL_COST":
                # Logic for paying additional costs for a spell 
                # WARNING: This action is not fully implemented in the current code
                logging.warning("PAY_ADDITIONAL_COST action is not fully implemented")

            elif action_type == "PAY_ESCALATE":
                # Logic for paying the escalate cost 
                # WARNING: This action is not fully implemented in the current code
                logging.warning("PAY_ESCALATE action is not fully implemented")

            elif action_type == "CREATE_TOKEN":
                # Logic for creating a token 
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CREATE_TOKEN action is not fully implemented")

            elif action_type == "COPY_PERMANENT":
                # Logic for copying a permanent
                # WARNING: This action is not fully implemented in the current code
                logging.warning("COPY_PERMANENT action is not fully implemented")

            elif action_type == "COPY_SPELL":
                # Logic for copying a spell
                # WARNING: This action is not fully implemented in the current code
                logging.warning("COPY_SPELL action is not fully implemented")

            elif action_type == "POPULATE":
                # Logic for copying a token
                # WARNING: This action is not fully implemented in the current code
                logging.warning("POPULATE action is not fully implemented")
                
            # Replace the COUNTER_SPELL section
            elif action_type == "COUNTER_SPELL":
                if param is not None and isinstance(param, tuple) and len(param) == 2:
                    counter_card_idx, target_spell_idx = param
                    
                    # Get the counterspell from hand
                    if counter_card_idx < len(me["hand"]):
                        counter_id = me["hand"][counter_card_idx]
                        counter_card = gs._safe_get_card(counter_id)
                        
                        # Check if it's a valid counterspell
                        is_counter = False
                        if counter_card and hasattr(counter_card, 'oracle_text'):
                            is_counter = "counter target spell" in counter_card.oracle_text.lower()
                        
                        if is_counter:
                            # Check if we can afford to cast it
                            can_afford = False
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, counter_card.mana_cost if hasattr(counter_card, 'mana_cost') else "")
                            else:
                                can_afford = sum(me["mana_pool"].values()) > 0
                            
                            if can_afford:
                                # Check if there are spells on the stack to counter
                                if gs.stack and len(gs.stack) > target_spell_idx:
                                    stack_item = gs.stack[-(target_spell_idx+1)]  # Count from top of stack
                                    
                                    if isinstance(stack_item, tuple) and stack_item[0] == "SPELL":
                                        target_id = stack_item[1]
                                        target_spell = gs._safe_get_card(target_id)
                                        
                                        # Can't counter if target has "can't be countered"
                                        if target_spell and hasattr(target_spell, 'oracle_text') and "can't be countered" in target_spell.oracle_text.lower():
                                            logging.debug(f"Cannot counter {target_spell.name} - it can't be countered")
                                            reward -= 0.1
                                            return reward, done
                                        
                                        # Cast the counterspell
                                        me["hand"].pop(counter_card_idx)
                                        
                                        # Pay the cost
                                        if hasattr(gs, 'mana_system'):
                                            gs.mana_system.pay_mana_cost(me, counter_card.mana_cost if hasattr(counter_card, 'mana_cost') else "")
                                        else:
                                            me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                        
                                        # Add counterspell to stack with targeting info
                                        counter_context = {"targets": {"spell": [target_id]}, "countering": True}
                                        gs.stack.append(("SPELL", counter_id, me, counter_context))
                                        
                                        logging.debug(f"Cast {counter_card.name} targeting {target_spell.name if target_spell else 'spell'}")
                                        reward += 0.4  # Reward for counter play
                                        
                                        gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                                    else:
                                        logging.debug(f"Target is not a spell")
                                        reward -= 0.1
                                else:
                                    logging.debug(f"No valid spell target on stack")
                                    reward -= 0.1
                            else:
                                logging.debug(f"Cannot afford to cast {counter_card.name}")
                                reward -= 0.1
                        else:
                            logging.debug(f"Not a counterspell")
                            reward -= 0.1
                    else:
                        logging.debug(f"Invalid counterspell index")
                        reward -= 0.1
                elif gs.stack:
                    # Alternate version where no specific parameters are given
                    # Look for a counterspell in hand and a spell on stack
                    counterspell_idx = -1
                    for idx, card_id in enumerate(me["hand"]):
                        card = gs._safe_get_card(card_id)
                        if card and hasattr(card, 'oracle_text') and "counter target spell" in card.oracle_text.lower():
                            can_afford = False
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, card.mana_cost if hasattr(card, 'mana_cost') else "")
                            else:
                                can_afford = sum(me["mana_pool"].values()) > 0
                            
                            if can_afford:
                                counterspell_idx = idx
                                break
                    
                    if counterspell_idx >= 0:
                        # Find the top spell on the stack
                        target_idx = -1
                        for i, item in enumerate(reversed(gs.stack)):
                            if isinstance(item, tuple) and item[0] == "SPELL" and item[2] != me:  # Not our own spell
                                target_idx = i
                                break
                        
                        if target_idx >= 0:
                            # Apply the counter logic using the identified spell and counterspell
                            counter_id = me["hand"][counterspell_idx]
                            counter_card = gs._safe_get_card(counter_id)
                            
                            stack_item = gs.stack[-(target_idx+1)]
                            target_id = stack_item[1]
                            target_spell = gs._safe_get_card(target_id)
                            
                            # Can't counter if target has "can't be countered"
                            if target_spell and hasattr(target_spell, 'oracle_text') and "can't be countered" in target_spell.oracle_text.lower():
                                logging.debug(f"Cannot counter {target_spell.name} - it can't be countered")
                                reward -= 0.1
                                return reward, done
                            
                            # Cast the counterspell
                            me["hand"].pop(counterspell_idx)
                            
                            # Pay the cost
                            if hasattr(gs, 'mana_system'):
                                gs.mana_system.pay_mana_cost(me, counter_card.mana_cost if hasattr(counter_card, 'mana_cost') else "")
                            else:
                                me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                            
                            # Add counterspell to stack with targeting info
                            counter_context = {"targets": {"spell": [target_id]}, "countering": True}
                            gs.stack.append(("SPELL", counter_id, me, counter_context))
                            
                            logging.debug(f"Cast {counter_card.name} targeting {target_spell.name if target_spell else 'spell'}")
                            reward += 0.4  # Reward for counter play
                            
                            gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                        else:
                            logging.debug(f"No valid spell target on stack")
                            reward -= 0.1
                    else:
                        logging.debug(f"No valid counterspell in hand or can't afford to cast")
                        reward -= 0.1
                else:
                    logging.debug(f"No spells on stack to counter")
                    reward -= 0.1

            # Replace the COUNTER_ABILITY section
            elif action_type == "COUNTER_ABILITY":
                if param is not None and isinstance(param, tuple) and len(param) == 2:
                    counter_card_idx, target_ability_idx = param
                    
                    # Get the counter ability card from hand
                    if counter_card_idx < len(me["hand"]):
                        counter_id = me["hand"][counter_card_idx]
                        counter_card = gs._safe_get_card(counter_id)
                        
                        # Check if it's a valid counter ability card
                        is_counter_ability = False
                        if counter_card and hasattr(counter_card, 'oracle_text'):
                            is_counter_ability = "counter target activated ability" in counter_card.oracle_text.lower() or "counter target ability" in counter_card.oracle_text.lower()
                        
                        if is_counter_ability:
                            # Check if we can afford to cast it
                            can_afford = False
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, counter_card.mana_cost if hasattr(counter_card, 'mana_cost') else "")
                            else:
                                can_afford = sum(me["mana_pool"].values()) > 0
                            
                            if can_afford:
                                # Check if there are activated abilities on the stack to counter
                                ability_targets = []
                                for i, item in enumerate(gs.stack):
                                    if isinstance(item, tuple) and item[0] == "ABILITY":
                                        ability_targets.append((i, item))
                                
                                if ability_targets and target_ability_idx < len(ability_targets):
                                    target_idx, ability_item = ability_targets[target_ability_idx]
                                    ability_id = ability_item[1]
                                    ability_source = ability_item[2]
                                    
                                    # Cast the counter ability spell
                                    me["hand"].pop(counter_card_idx)
                                    
                                    # Pay the cost
                                    if hasattr(gs, 'mana_system'):
                                        gs.mana_system.pay_mana_cost(me, counter_card.mana_cost if hasattr(counter_card, 'mana_cost') else "")
                                    else:
                                        me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                    
                                    # Add counter ability spell to stack with targeting info
                                    counter_context = {"targets": {"ability": [ability_id]}, "countering_ability": True}
                                    gs.stack.append(("SPELL", counter_id, me, counter_context))
                                    
                                    source_card = gs._safe_get_card(ability_source)
                                    logging.debug(f"Cast {counter_card.name} targeting ability from {source_card.name if source_card else 'source'}")
                                    reward += 0.4  # Reward for counter play
                                    
                                    gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                                else:
                                    logging.debug(f"No valid ability target on stack")
                                    reward -= 0.1
                            else:
                                logging.debug(f"Cannot afford to cast {counter_card.name}")
                                reward -= 0.1
                        else:
                            logging.debug(f"Not a counter ability card")
                            reward -= 0.1
                    else:
                        logging.debug(f"Invalid counter ability card index")
                        reward -= 0.1
                else:
                    # Look for a counter ability card and the first ability on stack
                    counter_idx = -1
                    for idx, card_id in enumerate(me["hand"]):
                        card = gs._safe_get_card(card_id)
                        if card and hasattr(card, 'oracle_text') and ("counter target activated ability" in card.oracle_text.lower() or "counter target ability" in card.oracle_text.lower()):
                            can_afford = False
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, card.mana_cost if hasattr(card, 'mana_cost') else "")
                            else:
                                can_afford = sum(me["mana_pool"].values()) > 0
                            
                            if can_afford:
                                counter_idx = idx
                                break
                    
                    if counter_idx >= 0:
                        # Find activated abilities on the stack
                        ability_targets = []
                        for i, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and item[0] == "ABILITY":
                                ability_targets.append((i, item))
                        
                        if ability_targets:
                            target_idx, ability_item = ability_targets[0]  # Pick first ability
                            ability_id = ability_item[1]
                            ability_source = ability_item[2]
                            
                            counter_id = me["hand"][counter_idx]
                            counter_card = gs._safe_get_card(counter_id)
                            
                            # Cast the counter ability spell
                            me["hand"].pop(counter_idx)
                            
                            # Pay the cost
                            if hasattr(gs, 'mana_system'):
                                gs.mana_system.pay_mana_cost(me, counter_card.mana_cost if hasattr(counter_card, 'mana_cost') else "")
                            else:
                                me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                            
                            # Add counter ability spell to stack with targeting info
                            counter_context = {"targets": {"ability": [ability_id]}, "countering_ability": True}
                            gs.stack.append(("SPELL", counter_id, me, counter_context))
                            
                            source_card = gs._safe_get_card(ability_source)
                            logging.debug(f"Cast {counter_card.name} targeting ability from {source_card.name if source_card else 'source'}")
                            reward += 0.4  # Reward for counter play
                            
                            gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                        else:
                            logging.debug(f"No abilities on stack to counter")
                            reward -= 0.1
                    else:
                        logging.debug(f"No valid counter ability card in hand or can't afford to cast")
                        reward -= 0.1

            elif action_type == "PREVENT_DAMAGE":
                # Logic for preventing damage
                # WARNING: This action is not fully implemented in the current code
                logging.warning("PREVENT_DAMAGE action is not fully implemented")

            elif action_type == "REDIRECT_DAMAGE":
                # Logic for redirecting damage
                # WARNING: This action is not fully implemented in the current code
                logging.warning("REDIRECT_DAMAGE action is not fully implemented") 

            elif action_type == "STIFLE_TRIGGER":
                # Logic for countering a triggered ability
                # WARNING: This action is not fully implemented in the current code
                logging.warning("STIFLE_TRIGGER action is not fully implemented")


            elif action_type == "LOYALTY_ABILITY_PLUS":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                        # Check if card can be activated (not used this turn)
                        if hasattr(gs, "planeswalker_abilities_used") and card_id in gs.planeswalker_abilities_used:
                            logging.debug(f"Planeswalker {card.name} already activated this turn")
                            return reward - 0.5, done  # Apply penalty for trying to activate again
                            
                        # Find the plus loyalty ability
                        plus_ability = None
                        plus_ability_idx = -1
                        
                        if hasattr(card, 'loyalty_abilities'):
                            for i, ability in enumerate(card.loyalty_abilities):
                                if ability.get('cost', 0) > 0:
                                    plus_ability = ability
                                    plus_ability_idx = i
                                    break
                        
                        if plus_ability:
                            # Check if enough loyalty
                            loyalty_gain = plus_ability.get('cost', 1)
                            if hasattr(card, 'loyalty'):
                                card.loyalty += loyalty_gain
                            else:
                                card.loyalty = loyalty_gain
                            
                            # Use ability_handler to activate the ability
                            if hasattr(gs, 'ability_handler'):
                                context = {"source": card_id, "controller": me, "loyalty_ability": True}
                                gs.ability_handler.activate_loyalty_ability(card_id, plus_ability_idx, me, context)
                            
                            # Mark ability as used
                            if not hasattr(gs, 'planeswalker_abilities_used'):
                                gs.planeswalker_abilities_used = set()
                            gs.planeswalker_abilities_used.add(card_id)
                            
                            logging.debug(f"Activated plus loyalty ability of {card.name}")
                            reward += 0.3  # Reward for using planeswalker ability
                            gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                        else:
                            logging.debug(f"No plus loyalty ability found for {card.name}")
                            reward -= 0.1
                    else:
                        logging.debug(f"Not a planeswalker card")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid planeswalker index")
                    reward -= 0.1

            elif action_type == "LOYALTY_ABILITY_MINUS":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                        # Check if card can be activated (not used this turn)
                        if hasattr(gs, "planeswalker_abilities_used") and card_id in gs.planeswalker_abilities_used:
                            logging.debug(f"Planeswalker {card.name} already activated this turn")
                            reward -= 0.5  # Apply penalty for trying to activate again
                            return reward, done
                        
                        # Find a non-ultimate minus loyalty ability
                        minus_ability = None
                        minus_ability_idx = -1
                        
                        if hasattr(card, 'loyalty_abilities'):
                            for i, ability in enumerate(card.loyalty_abilities):
                                if ability.get('cost', 0) < 0 and not ability.get('is_ultimate', False):
                                    minus_ability = ability
                                    minus_ability_idx = i
                                    break
                        
                        if minus_ability:
                            # Check if enough loyalty
                            loyalty_cost = abs(minus_ability.get('cost', 1))
                            has_enough_loyalty = hasattr(card, 'loyalty') and card.loyalty >= loyalty_cost
                            
                            if has_enough_loyalty:
                                # Remove loyalty counters
                                card.loyalty -= loyalty_cost
                                
                                # Use ability_handler to activate the ability
                                if hasattr(gs, 'ability_handler'):
                                    context = {"source": card_id, "controller": me, "loyalty_ability": True}
                                    gs.ability_handler.activate_loyalty_ability(card_id, minus_ability_idx, me, context)
                                
                                # Mark ability as used
                                if not hasattr(gs, 'planeswalker_abilities_used'):
                                    gs.planeswalker_abilities_used = set()
                                gs.planeswalker_abilities_used.add(card_id)
                                
                                logging.debug(f"Activated minus loyalty ability of {card.name}")
                                reward += 0.35  # Reward for using planeswalker ability
                                gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                            else:
                                logging.debug(f"Not enough loyalty on {card.name}")
                                reward -= 0.1  # Penalty for invalid activation
                        else:
                            logging.debug(f"No minus loyalty ability found for {card.name}")
                            reward -= 0.1
                    else:
                        logging.debug(f"Not a planeswalker card")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid planeswalker index")
                    reward -= 0.1
        
            elif action_type == "LOYALTY_ABILITY_ZERO":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                        # Check if card can be activated (not used this turn)
                        if hasattr(gs, "planeswalker_abilities_used") and card_id in gs.planeswalker_abilities_used:
                            logging.debug(f"Planeswalker {card.name} already activated this turn")
                            reward -= 0.5  # Apply penalty for trying to activate again
                            return reward, done
                        
                        # Find the zero loyalty ability
                        zero_ability = None
                        zero_ability_idx = -1
                        
                        if hasattr(card, 'loyalty_abilities'):
                            for i, ability in enumerate(card.loyalty_abilities):
                                if ability.get('cost', 0) == 0:
                                    zero_ability = ability
                                    zero_ability_idx = i
                                    break
                        
                        if zero_ability:
                            # No loyalty change for zero ability
                            
                            # Use ability_handler to activate the ability
                            if hasattr(gs, 'ability_handler'):
                                context = {"source": card_id, "controller": me, "loyalty_ability": True}
                                gs.ability_handler.activate_loyalty_ability(card_id, zero_ability_idx, me, context)
                            
                            # Mark ability as used
                            if not hasattr(gs, 'planeswalker_abilities_used'):
                                gs.planeswalker_abilities_used = set()
                            gs.planeswalker_abilities_used.add(card_id)
                            
                            logging.debug(f"Activated zero loyalty ability of {card.name}")
                            reward += 0.25  # Reward for using planeswalker ability
                            gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                        else:
                            logging.debug(f"No zero loyalty ability found for {card.name}")
                            reward -= 0.1
                    else:
                        logging.debug(f"Not a planeswalker card")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid planeswalker index")
                    reward -= 0.1
                    
            elif action_type == "ULTIMATE_ABILITY":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                        # Check if card can be activated (not used this turn)
                        if hasattr(gs, "planeswalker_abilities_used") and card_id in gs.planeswalker_abilities_used:
                            logging.debug(f"Planeswalker {card.name} already activated this turn")
                            reward -= 0.5  # Apply penalty for trying to activate again
                            return reward, done
                            
                        # Find the ultimate ability (highest cost minus ability)
                        ultimate_ability = None
                        ultimate_ability_idx = -1
                        
                        if hasattr(card, 'loyalty_abilities'):
                            highest_cost = 0
                            for i, ability in enumerate(card.loyalty_abilities):
                                if ability.get('cost', 0) < 0 and (ability.get('is_ultimate', False) or abs(ability.get('cost', 0)) > highest_cost):
                                    highest_cost = abs(ability.get('cost', 0))
                                    ultimate_ability = ability
                                    ultimate_ability_idx = i
                        
                        if ultimate_ability:
                            # Check if enough loyalty
                            loyalty_cost = abs(ultimate_ability.get('cost', 1))
                            
                            # Check loyalty counters first
                            if not hasattr(me, "loyalty_counters"):
                                me["loyalty_counters"] = {}
                                
                            current_loyalty = me["loyalty_counters"].get(card_id, card.loyalty if hasattr(card, 'loyalty') else 0)
                            
                            if current_loyalty >= loyalty_cost:
                                # Pay the loyalty cost
                                me["loyalty_counters"][card_id] = current_loyalty - loyalty_cost
                                
                                # Mark as activated
                                if not hasattr(gs, 'planeswalker_abilities_used'):
                                    gs.planeswalker_abilities_used = set()
                                gs.planeswalker_abilities_used.add(card_id)
                                
                                # Process effect
                                effect = ultimate_ability.get('effect', '')
                                logging.debug(f"Activating ultimate ability of {card.name}: {effect}")
                                
                                # Use our comprehensive helper method to process the ability
                                gs._process_planeswalker_ability_effect(card_id, me, effect)
                                
                                # Add ability to stack
                                gs.stack.append(("PLANESWALKER_ABILITY", card_id, me, {
                                    "ability_index": ultimate_ability_idx,
                                    "is_ultimate": True
                                }))
                                
                                # Reset priority
                                gs.priority_pass_count = 0
                                gs.last_stack_size = len(gs.stack)
                                gs.phase = gs.PHASE_PRIORITY
                                
                                # Higher reward for ultimate ability
                                reward += 0.5
                                
                                # Check state-based actions after resolution
                                gs.check_state_based_actions()
                            else:
                                logging.debug(f"Not enough loyalty on {card.name} for ultimate")
                                reward -= 0.1  # Penalty for invalid activation
                        else:
                            logging.debug(f"No ultimate ability found for {card.name}")
                            reward -= 0.1
                    else:
                        logging.debug(f"Not a planeswalker card")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid planeswalker index")
                    reward -= 0.1

            elif action_type == "CAST_LEFT_HALF":
                if param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    card = gs._safe_get_card(card_id)
                    
                    # Check if it's a split card
                    is_split = False
                    if card and hasattr(card, 'layout'):
                        is_split = card.layout == "split"
                    elif card and hasattr(card, 'oracle_text') and "//" in card.oracle_text:
                        is_split = True
                    
                    if is_split and hasattr(card, 'left_half'):
                        # Get left half cost
                        left_cost = card.left_half.get('mana_cost', card.mana_cost if hasattr(card, 'mana_cost') else "")
                        
                        # Check if we can afford to cast it
                        can_afford = False
                        if hasattr(gs, 'mana_system'):
                            can_afford = gs.mana_system.can_pay_mana_cost(me, left_cost)
                        else:
                            can_afford = sum(me["mana_pool"].values()) > 0
                        
                        if can_afford:
                            # Store original card properties
                            if not hasattr(card, 'original_properties'):
                                card.original_properties = {
                                    'name': card.name,
                                    'type_line': card.type_line if hasattr(card, 'type_line') else "",
                                    'oracle_text': card.oracle_text if hasattr(card, 'oracle_text') else "",
                                    'colors': card.colors.copy() if hasattr(card, 'colors') else []
                                }
                            
                            # Set card to left half properties
                            left_half = card.left_half
                            card.name = left_half.get('name', card.name)
                            if 'type_line' in left_half:
                                card.type_line = left_half['type_line']
                            if 'oracle_text' in left_half:
                                card.oracle_text = left_half['oracle_text']
                            if 'colors' in left_half:
                                card.colors = left_half['colors']
                            
                            # Move card from hand to stack
                            me["hand"].pop(param)
                            gs.stack.append(("SPELL", card_id, me, {"cast_left_half": True}))
                            
                            # Pay the cost
                            if hasattr(gs, 'mana_system'):
                                gs.mana_system.pay_mana_cost(me, left_cost)
                            else:
                                me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                            
                            logging.debug(f"Cast left half of split card {card.name}")
                            reward += 0.3  # Reward for casting
                            
                            gs.phase = gs.PHASE_PRIORITY
                        else:
                            logging.debug(f"Cannot afford to cast left half of {card.name}")
                            reward -= 0.1
                    else:
                        logging.debug(f"Not a split card or no left half defined: {card.name if hasattr(card, 'name') else 'Unknown'}")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid card index for split card casting")
                    reward -= 0.1

            elif action_type == "CAST_RIGHT_HALF":
                if param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    card = gs._safe_get_card(card_id)
                    
                    # Check if it's a split card
                    is_split = False
                    if card and hasattr(card, 'layout'):
                        is_split = card.layout == "split"
                    elif card and hasattr(card, 'oracle_text') and "//" in card.oracle_text:
                        is_split = True
                    
                    if is_split and hasattr(card, 'right_half'):
                        # Get right half cost
                        right_cost = card.right_half.get('mana_cost', card.mana_cost if hasattr(card, 'mana_cost') else "")
                        
                        # Check if we can afford to cast it
                        can_afford = False
                        if hasattr(gs, 'mana_system'):
                            can_afford = gs.mana_system.can_pay_mana_cost(me, right_cost)
                        else:
                            can_afford = sum(me["mana_pool"].values()) > 0
                        
                        if can_afford:
                            # Store original card properties
                            if not hasattr(card, 'original_properties'):
                                card.original_properties = {
                                    'name': card.name,
                                    'type_line': card.type_line if hasattr(card, 'type_line') else "",
                                    'oracle_text': card.oracle_text if hasattr(card, 'oracle_text') else "",
                                    'colors': card.colors.copy() if hasattr(card, 'colors') else []
                                }
                            
                            # Set card to right half properties
                            right_half = card.right_half
                            card.name = right_half.get('name', card.name)
                            if 'type_line' in right_half:
                                card.type_line = right_half['type_line']
                            if 'oracle_text' in right_half:
                                card.oracle_text = right_half['oracle_text']
                            if 'colors' in right_half:
                                card.colors = right_half['colors']
                            
                            # Move card from hand to stack
                            me["hand"].pop(param)
                            gs.stack.append(("SPELL", card_id, me, {"cast_right_half": True}))
                            
                            # Pay the cost
                            if hasattr(gs, 'mana_system'):
                                gs.mana_system.pay_mana_cost(me, right_cost)
                            else:
                                me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                            
                            logging.debug(f"Cast right half of split card {card.name}")
                            reward += 0.3  # Reward for casting
                            
                            gs.phase = gs.PHASE_PRIORITY
                        else:
                            logging.debug(f"Cannot afford to cast right half of {card.name}")
                            reward -= 0.1
                    else:
                        logging.debug(f"Not a split card or no right half defined: {card.name if hasattr(card, 'name') else 'Unknown'}")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid card index for split card casting")
                    reward -= 0.1

            elif action_type == "CAST_FUSE":
                if param is not None and param < len(me["hand"]):
                    card_id = me["hand"][param]
                    card = gs._safe_get_card(card_id)
                    
                    # Check if it's a split card with fuse
                    is_fuse = False
                    if card and hasattr(card, 'oracle_text') and "fuse" in card.oracle_text.lower():
                        is_fuse = True
                    
                    if is_fuse and hasattr(card, 'left_half') and hasattr(card, 'right_half'):
                        # Get combined cost (this is a simplification)
                        left_cost = card.left_half.get('mana_cost', "")
                        right_cost = card.right_half.get('mana_cost', "")
                        
                        # Check if we can afford both costs
                        can_afford = False
                        if hasattr(gs, 'mana_system'):
                            # Need to combine costs properly
                            combined_cost = gs.mana_system.combine_mana_costs(left_cost, right_cost)
                            can_afford = gs.mana_system.can_pay_mana_cost(me, combined_cost)
                        else:
                            # Simple check - assume higher combined cost
                            can_afford = sum(me["mana_pool"].values()) > 3  # Arbitrary threshold
                        
                        if can_afford:
                            # Create special context for fused casting
                            context = {
                                "fused": True,
                                "left_half": card.left_half,
                                "right_half": card.right_half
                            }
                            
                            # Move card from hand to stack
                            me["hand"].pop(param)
                            gs.stack.append(("SPELL", card_id, me, context))
                            
                            # Pay the combined cost
                            if hasattr(gs, 'mana_system'):
                                combined_cost = gs.mana_system.combine_mana_costs(left_cost, right_cost)
                                gs.mana_system.pay_mana_cost(me, combined_cost)
                            else:
                                me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                            
                            logging.debug(f"Cast split card {card.name} with fuse, using both halves")
                            reward += 0.4  # Higher reward for fused casting
                            
                            gs.phase = gs.PHASE_PRIORITY
                        else:
                            logging.debug(f"Cannot afford to fuse cast {card.name}")
                            reward -= 0.1
                    else:
                        logging.debug(f"Card cannot be fused or missing halves: {card.name if hasattr(card, 'name') else 'Unknown'}")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid card index for fuse casting")
                    reward -= 0.1

            elif action_type == "AFTERMATH_CAST":
                # Logic for casting the aftermath half of a card from graveyard
                # WARNING: This action is not fully implemented in the current code 
                logging.warning("AFTERMATH_CAST action is not fully implemented")

            elif action_type == "FLIP_CARD":
                # Logic for flipping a flip card 
                # WARNING: This action is not fully implemented in the current code
                logging.warning("FLIP_CARD action is not fully implemented")

            elif action_type == "EQUIP":
                if param is not None and isinstance(param, tuple) and len(param) == 2:
                    equipment_idx, target_idx = param
                    
                    if equipment_idx < len(me["battlefield"]) and target_idx < len(me["battlefield"]):
                        equipment_id = me["battlefield"][equipment_idx]
                        target_id = me["battlefield"][target_idx]
                        
                        equipment = gs._safe_get_card(equipment_id)
                        target = gs._safe_get_card(target_id)
                        
                        if equipment and hasattr(equipment, 'card_types') and 'equipment' in equipment.card_types:
                            if target and hasattr(target, 'card_types') and 'creature' in target.card_types:
                                # Check if equipment is already attached to something
                                currently_equipped = None
                                if hasattr(gs, 'equipped_to'):
                                    for eq_id, creature_id in gs.equipped_to.items():
                                        if eq_id == equipment_id:
                                            currently_equipped = creature_id
                                            break
                                
                                # Get equip cost with improved parsing
                                equip_cost = ""
                                if hasattr(equipment, 'oracle_text'):
                                    import re
                                    match = re.search(r"equip (\{[^\}]+\})", equipment.oracle_text.lower())
                                    if match:
                                        equip_cost = match.group(1)
                                    else:
                                        # Generic cost match
                                        match = re.search(r"equip (\d+)", equipment.oracle_text.lower())
                                        if match:
                                            equip_cost = "{" + match.group(1) + "}"
                                
                                # Check if we can afford to equip
                                can_afford = False
                                if hasattr(gs, 'mana_system'):
                                    can_afford = gs.mana_system.can_pay_mana_cost(me, equip_cost)
                                else:
                                    can_afford = sum(me["mana_pool"].values()) > 0
                                
                                if can_afford:
                                    # Pay the equip cost
                                    if hasattr(gs, 'mana_system'):
                                        gs.mana_system.pay_mana_cost(me, equip_cost)
                                    else:
                                        me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                    
                                    # Update equipment mapping
                                    if not hasattr(gs, 'equipped_to'):
                                        gs.equipped_to = {}
                                    
                                    # Unequip from current creature if any
                                    if currently_equipped is not None:
                                        # Remove bonuses from current creature
                                        if hasattr(gs, 'ability_handler'):
                                            gs.ability_handler.remove_equipment_bonuses(equipment_id, currently_equipped)
                                    
                                    # Equip to new creature
                                    gs.equipped_to[equipment_id] = target_id
                                    
                                    # Apply equipment bonuses
                                    if hasattr(gs, 'ability_handler'):
                                        gs.ability_handler.apply_equipment_bonuses(equipment_id, target_id)
                                    
                                    logging.debug(f"Equipped {equipment.name} to {target.name} for {equip_cost}")
                                    reward += 0.2  # Reward for equipping
                                    gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                                else:
                                    logging.debug(f"Cannot afford to equip {equipment.name}")
                                    reward -= 0.1
                            else:
                                logging.debug(f"Target is not a creature")
                                reward -= 0.1
                        else:
                            logging.debug(f"Not an equipment card")
                            reward -= 0.1
                    else:
                        logging.debug(f"Invalid equipment or target index")
                        reward -= 0.1
                elif param is not None and param < len(me["battlefield"]):
                    # Alternative format where param is just the equipment index
                    equipment_id = me["battlefield"][param]
                    equipment = gs._safe_get_card(equipment_id)
                    
                    if equipment and hasattr(equipment, 'card_types') and 'equipment' in equipment.card_types:
                        # Find a valid creature target
                        valid_targets = []
                        for idx, creature_id in enumerate(me["battlefield"]):
                            creature = gs._safe_get_card(creature_id)
                            if creature and hasattr(creature, 'card_types') and 'creature' in creature.card_types:
                                valid_targets.append((idx, creature_id))
                        
                        if valid_targets:
                            # Pick the best target (for now, just pick the first one)
                            target_idx, target_id = valid_targets[0]
                            target = gs._safe_get_card(target_id)
                            
                            # Get equip cost
                            equip_cost = ""
                            if hasattr(equipment, 'oracle_text'):
                                import re
                                match = re.search(r"equip (\{[^\}]+\})", equipment.oracle_text.lower())
                                if match:
                                    equip_cost = match.group(1)
                                else:
                                    # Generic cost match
                                    match = re.search(r"equip (\d+)", equipment.oracle_text.lower())
                                    if match:
                                        equip_cost = "{" + match.group(1) + "}"
                            
                            # Check if we can afford to equip
                            can_afford = False
                            if hasattr(gs, 'mana_system'):
                                can_afford = gs.mana_system.can_pay_mana_cost(me, equip_cost)
                            else:
                                can_afford = sum(me["mana_pool"].values()) > 0
                            
                            if can_afford:
                                # Pay the equip cost
                                if hasattr(gs, 'mana_system'):
                                    gs.mana_system.pay_mana_cost(me, equip_cost)
                                else:
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                
                                # Update equipment mapping
                                if not hasattr(gs, 'equipped_to'):
                                    gs.equipped_to = {}
                                
                                # Check if equipment is already attached to something
                                currently_equipped = None
                                for eq_id, creature_id in gs.equipped_to.items():
                                    if eq_id == equipment_id:
                                        currently_equipped = creature_id
                                        break
                                
                                # Unequip from current creature if any
                                if currently_equipped is not None:
                                    # Remove bonuses from current creature
                                    if hasattr(gs, 'ability_handler'):
                                        gs.ability_handler.remove_equipment_bonuses(equipment_id, currently_equipped)
                                
                                # Equip to new creature
                                gs.equipped_to[equipment_id] = target_id
                                
                                # Apply equipment bonuses
                                if hasattr(gs, 'ability_handler'):
                                    gs.ability_handler.apply_equipment_bonuses(equipment_id, target_id)
                                
                                logging.debug(f"Equipped {equipment.name} to {target.name}")
                                reward += 0.2  # Reward for equipping
                                gs.phase = gs.PHASE_PRIORITY  # Enter priority phase
                            else:
                                logging.debug(f"Cannot afford to equip {equipment.name}")
                                reward -= 0.1
                        else:
                            logging.debug(f"No valid creatures to equip")
                            reward -= 0.1
                    else:
                        logging.debug(f"Not an equipment card")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid equipment parameter format")
                    reward -= 0.1

            # Replace the UNEQUIP section
            elif action_type == "UNEQUIP":
                if param is not None and param < len(me["battlefield"]):
                    equipment_id = me["battlefield"][param]
                    equipment = gs._safe_get_card(equipment_id)
                    
                    if equipment and hasattr(equipment, 'card_types') and 'equipment' in equipment.card_types:
                        # Check if equipment is attached to something
                        currently_equipped = None
                        if hasattr(gs, 'equipped_to'):
                            for eq_id, creature_id in gs.equipped_to.items():
                                if eq_id == equipment_id:
                                    currently_equipped = creature_id
                                    break
                        
                        if currently_equipped is not None:
                            # Remove equipment mapping
                            del gs.equipped_to[equipment_id]
                            
                            # Remove bonuses from creature
                            if hasattr(gs, 'ability_handler'):
                                gs.ability_handler.remove_equipment_bonuses(equipment_id, currently_equipped)
                            
                            creature = gs._safe_get_card(currently_equipped)
                            logging.debug(f"Unequipped {equipment.name} from {creature.name if creature else 'creature'}")
                            
                            # Small reward for successful unequip
                            reward += 0.1
                        else:
                            logging.debug(f"{equipment.name} is not equipped to anything")
                            reward -= 0.1
                    else:
                        logging.debug(f"Not an equipment card")
                        reward -= 0.1
                else:
                    logging.debug(f"Invalid equipment index")
                    reward -= 0.1

            elif action_type == "ATTACH_AURA":
                # Logic for attaching an aura to a permanent
                # WARNING: This action is not fully implemented in the current code
                logging.warning("ATTACH_AURA action is not fully implemented")

            elif action_type == "FORTIFY":
                # Logic for attaching a fortification to a land
                # WARNING: This action is not fully implemented in the current code
                logging.warning("FORTIFY action is not fully implemented")

            elif action_type == "RECONFIGURE":
                # Logic for reconfiguring a creature
                # WARNING: This action is not fully implemented in the current code
                logging.warning("RECONFIGURE action is not fully implemented")

            elif action_type == "CLASH":
                # Logic for performing a clash with an opponent
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CLASH action is not fully implemented")

            elif action_type == "CONSPIRE":
                # Logic for paying the conspire cost
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CONSPIRE action is not fully implemented")

            elif action_type == "CONVOKE":
                # Logic for paying the convoke cost
                # WARNING: This action is not fully implemented in the current code
                logging.warning("CONVOKE action is not fully implemented")

            elif action_type == "GRANDEUR":
                # Logic for activating grandeur ability
                # WARNING: This action is not fully implemented in the current code
                logging.warning("GRANDEUR action is not fully implemented")

            elif action_type == "HELLBENT":
                # Logic for activating hellbent ability
                # WARNING: This action is not fully implemented in the current code
                logging.warning("HELLBENT action is not fully implemented")

            elif action_type == "MORPH":
                # Logic for turning a face-down creature face-up
                # WARNING: This action is not fully implemented in the current code
                logging.warning("MORPH action is not fully implemented")

            elif action_type == "MANIFEST":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'is_face_down') and card.is_face_down:
                        # Logic for turning a manifested card face-up
                        if hasattr(gs, 'ability_handler') and gs.ability_handler:
                            context = {"controller": me}
                            gs.ability_handler._apply_manifest(card_id, "TURN_FACE_UP", context)
                        
                        # Set card face-up
                        card.is_face_down = False
                        
                        logging.debug(f"Turned manifested card face-up: {card.name}")
                        reward += 0.2
                    else:
                        logging.debug(f"Not a face-down manifested card")
                else:
                    logging.debug(f"Invalid manifest index: {param} vs battlefield size {len(me['battlefield'])}")

            elif action_type == "LEVEL_UP_CLASS":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card and hasattr(card, 'is_class') and card.is_class:
                        # Check if the Class can level up
                        if hasattr(card, 'can_level_up') and card.can_level_up():
                            next_level = card.current_level + 1
                            level_cost = card.get_level_cost(next_level)
                            
                            # Check if we can afford to level up
                            can_afford = True
                            if hasattr(gs, 'mana_system') and level_cost:
                                can_afford = gs.mana_system.can_pay_mana_cost(me, level_cost)
                            
                            if can_afford:
                                # Pay the cost
                                if hasattr(gs, 'mana_system') and level_cost:
                                    gs.mana_system.pay_mana_cost(me, level_cost)
                                else:
                                    # Simple cost deduction
                                    me["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                                
                                # Level up the class
                                card.current_level = next_level
                                
                                # Trigger level-up event
                                if hasattr(gs, 'trigger_ability'):
                                    gs.trigger_ability(card_id, "CLASS_LEVEL_UP", {"level": next_level})
                                
                                # Update card characteristics based on new level
                                if hasattr(card, 'get_current_class_data'):
                                    level_data = card.get_current_class_data()
                                    if level_data and level_data.get("power") is not None:
                                        # This level turns the class into a creature
                                        card.power = level_data["power"]
                                        card.toughness = level_data["toughness"]
                                        card.type_line = level_data["type_line"]
                                
                                # Reset priority for new abilities
                                gs.phase = gs.PHASE_PRIORITY
                                
                                # Reward for leveling up
                                reward += 0.3 * next_level
                                
                                logging.debug(f"Leveled up Class {card.name} to level {next_level}")
                            else:
                                logging.debug(f"Cannot afford to level up {card.name}")
                        else:
                            logging.debug(f"Class cannot level up further")
                    else:
                        logging.debug(f"Not a Class card")
                else:
                    logging.debug(f"Invalid Class index: {param} vs battlefield size {len(me['battlefield'])}")


            elif action_type == "SELECT_SPREE_MODE":
                if isinstance(param, tuple) and len(param) == 2:
                    card_idx, mode_idx = param
                    
                    # Check if we have a Spree spell on the stack
                    if gs.stack and len(gs.stack) > 0:
                        stack_item = gs.stack[-1]  # Get top of stack
                        
                        if isinstance(stack_item, tuple) and len(stack_item) >= 4:
                            stack_type, card_id, controller, context = stack_item
                            
                            # Check if it's a spree spell
                            if stack_type == "SPELL" and context and "is_spree" in context:
                                card = gs._safe_get_card(card_id)
                                
                                if card and hasattr(card, 'is_spree') and card.is_spree and hasattr(card, 'spree_modes'):
                                    # Check if mode is valid
                                    if card_idx < 8 and mode_idx < 2 and len(card.spree_modes) > mode_idx:
                                        mode = card.spree_modes[mode_idx]
                                        mode_cost = mode.get('cost', '')
                                        
                                        # Check if we can afford this mode
                                        can_afford = True
                                        if hasattr(gs, 'mana_system'):
                                            can_afford = gs.mana_system.can_pay_mana_cost(me, mode_cost)
                                        
                                        # Check if mode is already selected
                                        selected_modes = context.get("selected_modes", [])
                                        if mode_idx in selected_modes:
                                            # Mode already selected, remove it
                                            selected_modes.remove(mode_idx)
                                            
                                            # Refund the cost
                                            if hasattr(gs, 'mana_system'):
                                                gs.mana_system.add_mana_to_pool(me, mode_cost)
                                            
                                            logging.debug(f"Removed Spree mode {mode_idx} from {card.name}")
                                            
                                            # Update context
                                            stack_item = (stack_type, card_id, controller, {"is_spree": True, "selected_modes": selected_modes})
                                            gs.stack[-1] = stack_item
                                            
                                            # Small reward for refining spell
                                            reward += 0.05
                                        elif can_afford:
                                            # Add mode to selection
                                            selected_modes.append(mode_idx)
                                            
                                            # Pay the cost
                                            if hasattr(gs, 'mana_system'):
                                                gs.mana_system.pay_mana_cost(me, mode_cost)
                                            
                                            logging.debug(f"Added Spree mode {mode_idx} to {card.name}")
                                            
                                            # Update context
                                            stack_item = (stack_type, card_id, controller, {"is_spree": True, "selected_modes": selected_modes})
                                            gs.stack[-1] = stack_item
                                            
                                            # Reward for adding mode
                                            reward += 0.2
                                        else:
                                            logging.debug(f"Cannot afford Spree mode {mode_idx}")
                                    else:
                                        logging.debug(f"Invalid Spree mode index: {mode_idx}")
                                else:
                                    logging.debug(f"Not a Spree card")
                            else:
                                logging.debug(f"Top of stack is not a Spree spell")
                        else:
                            logging.debug(f"Invalid stack item")
                    else:
                        logging.debug(f"Stack is empty")
                else:
                    logging.debug(f"Invalid Spree mode parameter")


            elif action_type == "SELECT_TARGET":
                if param is not None and param < 10:  # Up to 10 target indices
                    # Check if we have a spell or ability on the stack that needs targeting
                    if gs.stack and len(gs.stack) > 0:
                        stack_item = gs.stack[-1]  # Get top of stack
                        
                        if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                            stack_type, card_id, controller = stack_item[:3]
                            card = gs._safe_get_card(card_id)
                            
                            if card:
                                # Get available targets
                                available_targets = []
                                
                                # Check if we have a targeting system
                                if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'targeting_system'):
                                    # Use targeting system to get valid targets
                                    targets = gs.ability_handler.targeting_system.get_valid_targets(card_id)
                                    
                                    if targets:
                                        # Flatten targets into a list
                                        for target_type, target_list in targets.items():
                                            available_targets.extend(target_list)
                                else:
                                    # Simple targeting - all permanents and players
                                    for player in [gs.p1, gs.p2]:
                                        for permanent_id in player["battlefield"]:
                                            available_targets.append(permanent_id)
                                    available_targets.append("p1")  # Player 1
                                    available_targets.append("p2")  # Player 2
                                
                                # Select target by index
                                if param < len(available_targets):
                                    target_id = available_targets[param]
                                    
                                    # Get target type (creature, player, etc.)
                                    target_type = "unknown"
                                    if target_id in ["p1", "p2"]:
                                        target_type = "player"
                                    else:
                                        target_card = gs._safe_get_card(target_id)
                                        if target_card and hasattr(target_card, 'card_types'):
                                            for card_type in target_card.card_types:
                                                target_type = card_type
                                                break
                                    
                                    # Create targeting context
                                    targeting_context = {"targets": {target_type: [target_id]}}
                                    
                                    # Update stack item with targeting info
                                    if len(stack_item) >= 4:
                                        # Preserve existing context
                                        existing_context = stack_item[3]
                                        if isinstance(existing_context, dict):
                                            existing_context.update(targeting_context)
                                            new_stack_item = (stack_type, card_id, controller, existing_context)
                                        else:
                                            new_stack_item = (stack_type, card_id, controller, targeting_context)
                                    else:
                                        new_stack_item = (stack_type, card_id, controller, targeting_context)
                                    
                                    gs.stack[-1] = new_stack_item
                                    
                                    target_name = "Player " + target_id[1] if target_id in ["p1", "p2"] else (
                                        gs._safe_get_card(target_id).name if gs._safe_get_card(target_id) and hasattr(gs._safe_get_card(target_id), 'name') else "Unknown"
                                    )
                                    logging.debug(f"Selected target: {target_name}")
                                    
                                    # Reward for target selection
                                    reward += 0.1
                                else:
                                    logging.debug(f"Invalid target index: {param} vs {len(available_targets)} available targets")
                            else:
                                logging.debug(f"Invalid card on stack")
                        else:
                            logging.debug(f"Invalid stack item")
                    else:
                        logging.debug(f"Stack is empty")
                else:
                    logging.debug(f"Invalid target index")

            elif action_type == "SACRIFICE_PERMANENT":
                if param is not None and param < len(me["battlefield"]):
                    card_id = me["battlefield"][param]
                    card = gs._safe_get_card(card_id)
                    
                    if card:
                        # Check if sacrifice is part of a cost or effect
                        requires_sacrifice = False
                        
                        # Check stack for sacrifice requirements
                        if gs.stack and len(gs.stack) > 0:
                            stack_item = gs.stack[-1]
                            
                            if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                                stack_type, stack_card_id, stack_controller = stack_item[:3]
                                stack_card = gs._safe_get_card(stack_card_id)
                                
                                if stack_card and hasattr(stack_card, 'oracle_text'):
                                    if "sacrifice" in stack_card.oracle_text.lower():
                                        requires_sacrifice = True
                        
                        # Move card from battlefield to graveyard
                        me["battlefield"].remove(card_id)
                        me["graveyard"].append(card_id)
                        
                        # Trigger sacrifice effects
                        if hasattr(gs, 'trigger_ability'):
                            gs.trigger_ability(card_id, "SACRIFICE", {"controller": me})
                        
                        logging.debug(f"Sacrificed permanent: {card.name}")
                        
                        # Reward based on whether sacrifice was required
                        if requires_sacrifice:
                            reward += 0.1  # Reward for fulfilling requirement
                        else:
                            reward -= 0.1  # Small penalty for unnecessary sacrifice
                    else:
                        logging.debug(f"Invalid permanent to sacrifice")
                else:
                    logging.debug(f"Invalid sacrifice index: {param} vs battlefield size {len(me['battlefield'])}")
                
            # Handle any other action types here
            else:
                logging.warning(f"Unknown action type: {action_type}")

            # Calculate post-action state changes
            current_state = {
                "me_life": me["life"],
                "opp_life": opp["life"],
                "me_cards": len(me["hand"]),
                "opp_cards": len(opp["hand"]),
                "me_creatures": sum(1 for cid in me["battlefield"] 
                                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                                and 'creature' in gs._safe_get_card(cid).card_types),
                "opp_creatures": sum(1 for cid in opp["battlefield"] 
                                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                                and 'creature' in gs._safe_get_card(cid).card_types)
            }
            
            # Add rewards for state changes
            reward = self._add_state_change_rewards(reward, previous_state, current_state)
            
            # Check for game end conditions
            if opp["life"] <= 0:
                done = True
                reward += 3.0  # Big reward for winning
            elif me["life"] <= 0:
                done = True
                reward -= 3.0  # Big penalty for losing
            elif gs.turn > gs.max_turns:
                done = True
                # Evaluate final board state
                if me["life"] > opp["life"]:
                    reward += 1.0  # Reward for having higher life at turn limit
                elif me["life"] < opp["life"]:
                    reward -= 1.0  # Penalty for having lower life at turn limit

            return reward, done
        except Exception as e:
            logging.error(f"Error applying action {action_type}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return 0, False

    def _add_state_change_rewards(self, base_reward, previous_state, current_state):
        """Add rewards for positive state changes"""
        reward = base_reward
        
        # Life total changes
        life_gain = current_state["me_life"] - previous_state["me_life"]
        if life_gain > 0:
            reward += min(life_gain * 0.05, 0.2)  # Reward for gaining life
            
        life_loss = previous_state["opp_life"] - current_state["opp_life"]
        if life_loss > 0:
            reward += min(life_loss * 0.1, 0.3)  # Reward for damaging opponent
        
        # Card advantage changes
        card_gain = current_state["me_cards"] - previous_state["me_cards"]
        if card_gain > 0:
            reward += min(card_gain * 0.1, 0.3)  # Reward for drawing cards
            
        # Board presence changes
        creature_gain = current_state["me_creatures"] - previous_state["me_creatures"]
        if creature_gain > 0:
            reward += min(creature_gain * 0.15, 0.45)  # Reward for adding creatures
            
        creature_loss = previous_state["opp_creatures"] - current_state["opp_creatures"]
        if creature_loss > 0:
            reward += min(creature_loss * 0.15, 0.45)  # Reward for killing opponent's creatures
            
        return reward
    
    def _has_haste(self, card_id):
        """Check if a creature has haste"""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return False
        
        return 'haste' in card.oracle_text.lower()
        
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

    def _add_ability_activation_actions(self, player, valid_actions, set_valid_action):
        """Add actions for activating abilities with proper timing restrictions."""
        gs = self.game_state
        
        # Determine if we have sorcery timing
        is_main_phase = gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]
        stack_is_empty = len(gs.stack) == 0
        sorcery_allowed = is_main_phase and stack_is_empty
        
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            for idx, card_id in enumerate(player["battlefield"]):
                if idx >= 20:  # Limit to first 20 permanents for action space
                    break
                
                card = gs._safe_get_card(card_id)
                if not card:
                    continue
                
                # Get activated abilities for this permanent
                abilities = gs.ability_handler.get_activated_abilities(card_id)
                
                for ability_idx, ability in enumerate(abilities):
                    if ability_idx >= 3:  # Limit to first 3 abilities per card
                        break
                    
                    # Default to instant speed
                    requires_sorcery_speed = False
                    
                    # Check ability text for timing restrictions if available
                    if hasattr(ability, 'effect_text'):
                        effect_text = ability.effect_text.lower()
                        # Check for explicit sorcery timing restrictions
                        if "activate only as a sorcery" in effect_text or "activate only during your main phase" in effect_text:
                            requires_sorcery_speed = True
                    
                    # Check if ability belongs to a Class or Room card (these require sorcery speed)
                    if (hasattr(card, 'is_class') and card.is_class) or (hasattr(card, 'is_room') and card.is_room):
                        requires_sorcery_speed = True
                    
                    # Skip this ability if it requires sorcery speed but we're not at sorcery timing
                    if requires_sorcery_speed and not sorcery_allowed:
                        continue
                    
                    # Check if the ability can be activated (mana cost, etc.)
                    can_activate = False
                    if hasattr(gs.ability_handler, 'can_pay_ability_cost'):
                        cost_text = ability.cost if hasattr(ability, 'cost') else ""
                        can_activate = gs.ability_handler.can_pay_ability_cost(cost_text, player)
                    else:
                        can_activate = gs.ability_handler.can_activate_ability(card_id, ability_idx, player)
                    
                    if can_activate:
                        # Check activation history to prevent infinite loops
                        activation_count = 0
                        if hasattr(gs, 'abilities_activated_this_turn'):
                            for activated_id, activated_idx in gs.abilities_activated_this_turn:
                                if activated_id == card_id and activated_idx == ability_idx:
                                    activation_count += 1
                        
                        # Only enable if it hasn't been overused (limit to 3 uses per turn)
                        if activation_count < 3:
                            # Get strategic evaluation if available
                            recommended, confidence = self.recommend_ability_activation(
                                card_id, ability_idx)
                            
                            # Define action index for this ability
                            ability_action_idx = 100 + (idx * 3) + ability_idx
                            
                            # Add to valid actions with strategic info
                            reason = f"Activate ability {ability_idx} of {card.name}" + \
                                (f" (Recommended: {confidence:.2f})" if recommended else 
                                f" (Not recommended: {confidence:.2f})")
                            
                            set_valid_action(ability_action_idx, reason)
                            
                            # Store confidence for the agent to use
                            if not hasattr(self, 'action_confidence'):
                                self.action_confidence = {}
                            self.action_confidence[ability_action_idx] = confidence

    def _add_land_tapping_actions(self, player, valid_actions, set_valid_action):
        """Add actions for tapping lands for mana."""
        gs = self.game_state
        
        for idx in range(min(len(player["battlefield"]), 20)):
            if idx < len(player["battlefield"]):
                card_id = player["battlefield"][idx]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'type_line') and 'land' in card.type_line and card_id not in player["tapped_permanents"]:
                    set_valid_action(68 + idx, f"TAP_LAND for {card.name}")

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
            

    def _add_alternative_casting_actions(self, player, valid_actions, set_valid_action, is_sorcery_timing=False):
        """Add actions for alternative casting methods with proper timing restrictions."""
        gs = self.game_state
        
        # Flashback
        for idx, card_id in enumerate(player["graveyard"][:6]):
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "flashback" in card.oracle_text.lower():
                # Check if the card type allows casting at current timing
                card_is_instant = hasattr(card, 'card_types') and 'instant' in card.card_types
                card_requires_sorcery_timing = not card_is_instant
                
                # Skip if card requires sorcery timing but we're not at sorcery timing
                if card_requires_sorcery_timing and not is_sorcery_timing:
                    continue
                    
                # Extract flashback cost
                import re
                match = re.search(r"flashback (?:\{[^\}]+\}|[^\.]+)", card.oracle_text.lower())
                if match:
                    flashback_cost = match.group(0).replace("flashback ", "").strip()
                    
                    # Check if we can afford the flashback cost
                    can_afford = False
                    if hasattr(gs, 'mana_system'):
                        can_afford = gs.mana_system.can_pay_mana_cost(player, flashback_cost)
                    else:
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
                        set_valid_action(393, f"CAST_WITH_FLASHBACK {card.name} from graveyard")
        
        # Jump-start
        for idx, card_id in enumerate(player["graveyard"][:6]):
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "jump-start" in card.oracle_text.lower():
                # Check if the card type allows casting at current timing
                card_is_instant = hasattr(card, 'card_types') and 'instant' in card.card_types
                card_requires_sorcery_timing = not card_is_instant
                
                # Skip if card requires sorcery timing but we're not at sorcery timing
                if card_requires_sorcery_timing and not is_sorcery_timing:
                    continue
                    
                # Jump-start uses original card cost
                jump_start_cost = card.mana_cost if hasattr(card, 'mana_cost') else ""
                
                # Check if we can afford and have a card to discard
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, jump_start_cost)
                else:
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                if can_afford and len(player["hand"]) > 0:
                    set_valid_action(394, f"CAST_WITH_JUMP_START {card.name} from graveyard")
        
        # Escape
        if is_sorcery_timing or gs.phase in [gs.PHASE_PRIORITY, gs.PHASE_UPKEEP, gs.PHASE_END_STEP]:
            for idx, card_id in enumerate(player["graveyard"][:6]):
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "escape" in card.oracle_text.lower():
                    # Check if the card type allows casting at current timing
                    card_is_instant = hasattr(card, 'card_types') and 'instant' in card.card_types
                    card_requires_sorcery_timing = not card_is_instant
                    
                    # Skip if card requires sorcery timing but we're not at sorcery timing
                    if card_requires_sorcery_timing and not is_sorcery_timing:
                        continue
                        
                    # Extract escape cost and exile requirement
                    import re
                    match = re.search(r"escape—([^\.]+)", card.oracle_text.lower())
                    if match:
                        escape_text = match.group(1).strip()
                        
                        # Check for exile requirements
                        exile_req_match = re.search(r"exile ([^\.]+) from your graveyard", escape_text)
                        exile_requirement = exile_req_match.group(1) if exile_req_match else ""
                        
                        # Extract mana cost (remove exile requirement text)
                        escape_cost = escape_text
                        if exile_requirement:
                            escape_cost = escape_cost.replace(f"exile {exile_requirement} from your graveyard", "").strip()
                        
                        # Check if we can afford the escape cost
                        can_afford = False
                        if hasattr(gs, 'mana_system'):
                            can_afford = gs.mana_system.can_pay_mana_cost(player, escape_cost)
                        else:
                            can_afford = sum(player["mana_pool"].values()) > 0
                        
                        # Check if exile requirements can be met
                        # For simplicity, just check if there are enough cards in graveyard
                        has_exile_targets = len(player["graveyard"]) > 1
                        
                        if can_afford and has_exile_targets:
                            set_valid_action(395, f"CAST_WITH_ESCAPE {card.name} from graveyard")
        
        # Overload
        for idx, card_id in enumerate(player["hand"][:8]):
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "overload" in card.oracle_text.lower():
                # Check if the card type allows casting at current timing
                card_is_instant = hasattr(card, 'card_types') and 'instant' in card.card_types
                card_requires_sorcery_timing = not card_is_instant
                
                # Skip if card requires sorcery timing but we're not at sorcery timing
                if card_requires_sorcery_timing and not is_sorcery_timing:
                    continue
                    
                # Extract overload cost
                import re
                match = re.search(r"overload (\{[^\}]+\}|[^\.]+)", card.oracle_text.lower())
                if match:
                    overload_cost = match.group(1).strip()
                    
                    # Check if we can afford the overload cost
                    can_afford = False
                    if hasattr(gs, 'mana_system'):
                        can_afford = gs.mana_system.can_pay_mana_cost(player, overload_cost)
                    else:
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
                        set_valid_action(397, f"CAST_WITH_OVERLOAD {card.name}")
        
        # Only add sorcery-speed options when appropriate
        if is_sorcery_timing:
            # Emerge (sorcery speed only)
            for idx, card_id in enumerate(player["hand"][:8]):
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "emerge" in card.oracle_text.lower():
                    # Check if there's a creature that can be sacrificed
                    creatures_on_battlefield = [cid for cid in player["battlefield"] 
                                            if gs._safe_get_card(cid) and 
                                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                                            'creature' in gs._safe_get_card(cid).card_types]
                    
                    if creatures_on_battlefield:
                        # Extract emerge cost
                        import re
                        emerge_match = re.search(r"emerge (\{[^\}]+\}|[^\.]+)", card.oracle_text.lower())
                        emerge_cost = emerge_match.group(1).strip() if emerge_match else ""
                        
                        # Check if we can afford emerge cost
                        can_afford = False
                        if hasattr(gs, 'mana_system') and emerge_cost:
                            can_afford = gs.mana_system.can_pay_mana_cost(player, emerge_cost)
                        else:
                            can_afford = sum(player["mana_pool"].values()) > 0
                            
                        if can_afford:
                            set_valid_action(398, f"CAST_FOR_EMERGE {card.name}")
            
            # Delve (sorcery speed only)
            for idx, card_id in enumerate(player["hand"][:8]):
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "delve" in card.oracle_text.lower():
                    # Check if there are cards in graveyard to exile
                    if len(player["graveyard"]) > 0:
                        # Original cost minus generic mana that can be paid with delve
                        base_cost = card.mana_cost if hasattr(card, 'mana_cost') else ""
                        
                        # Check if we can afford the base cost with delve
                        can_afford = False
                        if hasattr(gs, 'mana_system'):
                            can_afford = gs.mana_system.can_pay_mana_cost(player, base_cost, 
                                                                        {"delve": len(player["graveyard"])})
                        else:
                            can_afford = sum(player["mana_pool"].values()) > 0
                            
                        if can_afford:
                            set_valid_action(399, f"CAST_FOR_DELVE {card.name}")
                    
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
        """Add actions for kicker and additional costs."""
        gs = self.game_state
        
        # Check for cards in hand with Kicker
        for idx, card_id in enumerate(player["hand"][:8]):
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "kicker" in card.oracle_text.lower():
                # Get base card cost
                base_cost = card.mana_cost if hasattr(card, 'mana_cost') else ""
                
                # Extract kicker cost
                import re
                match = re.search(r"kicker (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                if match:
                    kicker_cost = match.group(1)
                    
                    # Check if we can afford base cost (minimum)
                    can_afford_base = False
                    if hasattr(gs, 'mana_system'):
                        can_afford_base = gs.mana_system.can_pay_mana_cost(player, base_cost)
                    else:
                        can_afford_base = sum(player["mana_pool"].values()) > 0
                    
                    # Check if we can afford base + kicker cost
                    can_afford_kicker = False
                    if hasattr(gs, 'mana_system'):
                        # Combine costs
                        combined_cost = gs.mana_system.combine_mana_costs(base_cost, kicker_cost)
                        can_afford_kicker = gs.mana_system.can_pay_mana_cost(player, combined_cost)
                    else:
                        # Simple approximation
                        can_afford_kicker = sum(player["mana_pool"].values()) > 1
                    
                    # Add both options if we can afford at least the base cost
                    if can_afford_base:
                        if can_afford_kicker:
                            set_valid_action(400, f"PAY_KICKER for {card.name} (total: {base_cost} + {kicker_cost})")
                        set_valid_action(401, f"DON'T_PAY_KICKER for {card.name} (just {base_cost})")
        
        # Check for cards with additional costs
        for idx, card_id in enumerate(player["hand"][:8]):  # Limit to first 8
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "additional cost" in card.oracle_text.lower():
                # Check if we can pay the additional cost (e.g., sacrifice a creature)
                has_resources = False
                
                if "sacrifice" in card.oracle_text.lower():
                    # Check what needs to be sacrificed
                    import re
                    match = re.search(r"sacrifice (a|an) ([^,\.]+)", card.oracle_text.lower())
                    if match:
                        sacrifice_type = match.group(2)
                        # Check if we have that permanent type
                        has_resources = any(gs._safe_get_card(cid) and 
                                        sacrifice_type in gs._safe_get_card(cid).type_line.lower() 
                                        for cid in player["battlefield"])
                
                if has_resources:
                    set_valid_action(402, f"PAY_ADDITIONAL_COST for {card.name}")
                else:
                    set_valid_action(403, f"DON'T_PAY_ADDITIONAL_COST for {card.name}")
        
        # Check for cards with Escalate
        for idx, card_id in enumerate(player["hand"][:8]):  # Limit to first 8
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "escalate" in card.oracle_text.lower():
                # Extract escalate cost
                import re
                match = re.search(r"escalate ([^\(]+)", card.oracle_text.lower())
                if match:
                    escalate_cost = match.group(1)
                    
                    # Check if we can afford to escalate
                    can_afford = False
                    if hasattr(gs, 'mana_system'):
                        can_afford = gs.mana_system.can_pay_mana_cost(player, escalate_cost)
                    else:
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
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

    def _add_spree_mode_actions(self, player, valid_actions, set_valid_action):
        """Add actions for Spree mode selection."""
        gs = self.game_state
        
        for stack_item in gs.stack:
            if isinstance(stack_item, tuple) and len(stack_item) >= 4:
                # Check for Spree context
                spell_type, card_id, spell_caster, context = stack_item
                if spell_type == "SPELL" and spell_caster == player and context.get("is_spree", False):
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'spree_modes'):
                        for card_idx in range(min(8, len(card.spree_modes))):
                            for mode_idx in range(min(2, len(card.spree_modes))):
                                mode = card.spree_modes[mode_idx]
                                mode_cost = mode.get('cost', '')
                                
                                # Check if we can afford this mode
                                can_afford = False
                                if hasattr(gs, 'mana_system'):
                                    can_afford = gs.mana_system.can_pay_mana_cost(player, mode_cost)
                                else:
                                    # Simple check
                                    can_afford = sum(player["mana_pool"].values()) > 0
                                
                                if can_afford:
                                    set_valid_action(258 + (card_idx * 2) + mode_idx, 
                                                    f"SELECT_SPREE_MODE for {card.name}, mode {mode_idx}")

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