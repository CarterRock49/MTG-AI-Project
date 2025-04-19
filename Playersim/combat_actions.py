import logging
import re
from collections import defaultdict
import numpy as np

from Playersim.card import Card  # Add if needed for array operations
from .enhanced_card_evaluator import EnhancedCardEvaluator  # If used directly
from .enhanced_combat import ExtendedCombatResolver  # If referenced directly

class CombatActionHandler:
    """
    Handles specialized combat actions in MTG, implementing specific mechanics with clear, focused responsibilities.
    
    This class is specifically responsible for game state actions during combat, 
    distinguishing it from the combat resolution logic in the resolver.
    """
    
    def __init__(self, game_state):
        """
        Initialize the combat action handler with game state tracking.

        Args:
            game_state: The game state object
        """
        self.game_state = game_state

        # Initialize card evaluator if needed
        if hasattr(game_state, 'card_evaluator'):
            self.card_evaluator = game_state.card_evaluator
        else:
             # Initialize evaluator if not present in game_state yet
             try:
                 # Assuming EnhancedCardEvaluator is available
                 from .enhanced_card_evaluator import EnhancedCardEvaluator
                 self.card_evaluator = EnhancedCardEvaluator(game_state,
                 getattr(game_state, 'stats_tracker', None),
                 getattr(game_state, 'card_memory', None))
                 game_state.card_evaluator = self.card_evaluator
             except ImportError:
                 logging.warning("EnhancedCardEvaluator not found, evaluator functionality limited.")
                 self.card_evaluator = None
             except Exception as e:
                  logging.error(f"Failed to init CardEvaluator in CombatActionHandler: {e}")
                  self.card_evaluator = None

        # Initialize tracking dictionaries for combat state
        self._initialize_combat_state_tracking()

        logging.debug("CombatActionHandler initialized")

    def _add_planeswalker_actions(self, player, valid_actions, set_valid_action):
        """Add actions for planeswalker loyalty abilities."""
        # Ensure planeswalker abilities can only be activated at sorcery speed
        gs = self.game_state
        is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
        is_main_phase_empty_stack = gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and not gs.stack

        if not is_my_turn or not is_main_phase_empty_stack:
            return # Can only activate at sorcery speed

        for idx, card_id in enumerate(player["battlefield"]):
            if idx >= 5: break # ACTION_MEANINGS only maps up to index 4 for ATTACK_PLANESWALKER/DEFEND_BATTLE
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                already_activated = card_id in player.get("activated_this_turn", set())
                warning = " (ALREADY ACTIVATED)" if already_activated else ""

                current_loyalty = player.get("loyalty_counters", {}).get(card_id, getattr(card, 'loyalty', 0))

                if hasattr(card, 'loyalty_abilities'):
                    for ability_idx, ability in enumerate(card.loyalty_abilities):
                        cost = ability.get('cost', 0)
                        is_ultimate = ability.get('is_ultimate', False)

                        # Check affordability based on loyalty
                        if current_loyalty + cost < 0 and cost < 0: continue # Cannot pay minus if loyalty goes < 0

                        # Allow setting action even if already activated, handle penalty in reward/env
                        param_for_action = idx # Use the battlefield index as parameter

                        if cost > 0:
                            # Corrected from 435 to 440 to match ACTION_MEANINGS
                            set_valid_action(440, f"LOYALTY_ABILITY_PLUS for {card.name}{warning} (Index {idx})")
                        elif cost == 0:
                            # Corrected from 436 to 441 to match ACTION_MEANINGS
                            set_valid_action(441, f"LOYALTY_ABILITY_ZERO for {card.name}{warning} (Index {idx})")
                        else: # cost < 0
                            if is_ultimate:
                                # Corrected from 438 to 443 to match ACTION_MEANINGS
                                set_valid_action(443, f"ULTIMATE_ABILITY for {card.name}{warning} (Index {idx})")
                            else:
                                # Corrected from 437 to 442 to match ACTION_MEANINGS
                                set_valid_action(442, f"LOYALTY_ABILITY_MINUS for {card.name}{warning} (Index {idx})")

    def _add_equipment_aura_actions(self, player, valid_actions, set_valid_action):
        """Add actions for equipment and aura manipulation with improved cost handling."""
        gs = self.game_state
        # Sorcery speed only
        is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
        is_main_phase_empty_stack = gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and not gs.stack
        if not is_my_turn or not is_main_phase_empty_stack: return

        creature_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                             if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])]
        equipment_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                              if gs._safe_get_card(cid) and 'equipment' in getattr(gs._safe_get_card(cid), 'subtypes', [])]
        aura_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                         if gs._safe_get_card(cid) and 'aura' in getattr(gs._safe_get_card(cid), 'subtypes', [])]
        fortification_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                                if gs._safe_get_card(cid) and 'fortification' in getattr(gs._safe_get_card(cid), 'subtypes', [])]

        # Equip/Reconfigure
        for eq_idx, equip_id in equipment_indices:
            equip_card = gs._safe_get_card(equip_id)
            is_equipped = equip_id in getattr(player, "attachments", {}) # Check if key exists

            # Check Equip
            equip_cost_str = self._get_equip_cost_str(equip_card)
            if equip_cost_str and self._can_afford_cost_string(player, equip_cost_str):
                for c_idx, creature_id in creature_indices:
                    # Don't allow equipping to self if it's currently a creature
                    if equip_id == creature_id: continue
                    # Don't allow re-equipping to the same target
                    if is_equipped and player["attachments"][equip_id] == creature_id: continue
                    # Set action 445, assuming handler uses context for params (eq_idx, c_idx)
                    set_valid_action(445, f"EQUIP {equip_card.name} (Idx {eq_idx}) to {gs._safe_get_card(creature_id).name} (Idx {c_idx}) Cost: {equip_cost_str}")
                    # For simplicity, only allow targeting one creature? No, let agent choose.

            # Check Reconfigure
            reconf_cost_str = self._get_reconfigure_cost_str(equip_card)
            if reconf_cost_str and self._can_afford_cost_string(player, reconf_cost_str):
                 set_valid_action(449, f"RECONFIGURE {equip_card.name} (Idx {eq_idx}) Cost: {reconf_cost_str}") # Param=eq_idx

        # Unequip (This isn't a standard action, usually done via reconfigure or replacement)
        # Action 446 (UNEQUIP) might be misleading. Remove? Or map to Reconfigure?
        # Let's comment it out unless there's a specific need.
        # if hasattr(player, "attachments"):
        #     for equip_id, target_id in player["attachments"].items():
        #         equip_card = gs._safe_get_card(equip_id)
        #         if equip_card and 'equipment' in getattr(equip_card, 'subtypes', []):
        #             eq_idx = -1; ... find eq_idx ...
        #             if eq_idx != -1: set_valid_action(446, f"UNEQUIP {equip_card.name} (Idx {eq_idx})")

        # Attach Aura (Usually happens on cast, or via activated abilities?)
        # Action 447 seems misplaced for direct attachment outside casting/abilities.
        # Revisit if there are abilities that say "Attach CARDNAME to target creature".

        # Fortify
        land_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                         if gs._safe_get_card(cid) and 'land' in getattr(gs._safe_get_card(cid), 'type_line', '')]
        for fort_idx, fort_id in fortification_indices:
             fort_card = gs._safe_get_card(fort_id)
             fort_cost_str = self._get_fortify_cost_str(fort_card)
             if fort_cost_str and self._can_afford_cost_string(player, fort_cost_str):
                  for l_idx, land_id in land_indices:
                       set_valid_action(448, f"FORTIFY {fort_card.name} (Idx {fort_idx}) onto {gs._safe_get_card(land_id).name} (Idx {l_idx}) Cost: {fort_cost_str}")

    def _add_ninjutsu_actions(self, player, valid_actions, set_valid_action):
        """Add actions for using Ninjutsu."""
        gs = self.game_state

        # Check if in the correct phase (after blockers declared, before damage)
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS:
            return

        # Find unblocked attackers controlled by the player
        unblocked_attackers = []
        if hasattr(gs, 'current_attackers'):
            for attacker_id in gs.current_attackers:
                if attacker_id in player["battlefield"]: # Is it mine?
                    is_blocked = attacker_id in gs.current_block_assignments and gs.current_block_assignments[attacker_id]
                    if not is_blocked:
                        # Find index on battlefield for potential param
                        bf_idx = -1
                        for i, cid in enumerate(player["battlefield"]):
                            if cid == attacker_id: bf_idx = i; break
                        if bf_idx != -1:
                            unblocked_attackers.append((bf_idx, attacker_id))

        if not unblocked_attackers: return # No unblocked attackers to swap

        # Check hand for cards with Ninjutsu
        for hand_idx, card_id in enumerate(player["hand"]):
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "ninjutsu" in card.oracle_text.lower():
                ninjutsu_cost_str = self._get_ninjutsu_cost_str(card) # Get cost
                if ninjutsu_cost_str and self._can_afford_cost_string(player, ninjutsu_cost_str):
                    # Allow Ninjutsu action for each possible swap
                    for atk_bf_idx, attacker_id in unblocked_attackers:
                        # Corrected from 432 to 437 to match ACTION_MEANINGS for NINJUTSU
                        set_valid_action(437, f"NINJUTSU with {card.name} (H:{hand_idx}) for {gs._safe_get_card(attacker_id).name} (B:{atk_bf_idx}) Cost:{ninjutsu_cost_str}")
        
    def _add_multiple_blocker_actions(self, player, valid_actions, set_valid_action):
        """Add actions for assigning multiple blockers."""
        gs = self.game_state
        # Check phase
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS or not gs.current_attackers: return

        possible_blockers = [cid for cid in player["battlefield"]
                            if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', []) and cid not in player.get("tapped_permanents", set())]

        if len(possible_blockers) < 2: return # Need at least 2 to multi-block

        # Identify attackers that *can* be blocked by multiple creatures (not required by menace, just possible)
        for atk_idx, attacker_id in enumerate(gs.current_attackers):
             attacker_card = gs._safe_get_card(attacker_id)
             if not attacker_card: continue

             # Check if at least two valid blockers exist for this attacker
             num_valid_for_this_attacker = 0
             for blocker_id in possible_blockers:
                  if self._can_block(blocker_id, attacker_id):
                       num_valid_for_this_attacker += 1
                  if num_valid_for_this_attacker >= 2: break

             if num_valid_for_this_attacker >= 2:
                 if atk_idx < 10: # Action 383-392 assume attacker index 0-9
                    set_valid_action(383 + atk_idx, f"ASSIGN_MULTIPLE_BLOCKERS to {attacker_card.name} (Atk Index {atk_idx})")

            
    def setup_combat_systems(self):
        """
        Set up combat systems for the game if not already present.
        Ensures that all combat-related components are properly initialized and connected.
        """
        gs = self.game_state
        
        # Initialize combat resolver if needed (use Extended by default)
        if not hasattr(gs, 'combat_resolver') or gs.combat_resolver is None:
            logging.debug("Initializing ExtendedCombatResolver.")
            try:
                 gs.combat_resolver = ExtendedCombatResolver(gs)
                 gs.combat_resolver.action_handler = self # Link resolver back to handler if needed by resolver
            except Exception as e:
                 logging.error(f"Failed to initialize ExtendedCombatResolver: {e}")

        # Ensure this handler instance is linked in the game state
        if not hasattr(gs, 'combat_action_handler') or gs.combat_action_handler is not self:
             gs.combat_action_handler = self

        # Initialize combat-related data structures if they don't exist
        combat_attrs = [ "current_attackers", "current_block_assignments",
                         "planeswalker_attack_targets", "battle_attack_targets",
                         "planeswalker_protectors", "first_strike_ordering",
                         "combat_damage_dealt"]
        defaults = { "current_attackers": [], "current_block_assignments": {},
                     "planeswalker_attack_targets": {}, "battle_attack_targets": {},
                     "planeswalker_protectors": {}, "first_strike_ordering": {},
                     "combat_damage_dealt": False}

        for attr in combat_attrs:
             if not hasattr(gs, attr):
                  setattr(gs, attr, defaults[attr])   

    def evaluate_attack_configuration(self, attackers):
        """
        Evaluate the expected value of a particular attack configuration using CombatResolver simulation.
        Returns an estimated reward value.
        """
        gs = self.game_state

        # Use ExtendedCombatResolver's simulate_combat if available
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, 'simulate_combat'):
            # Save current state relevant to simulation
            original_attackers = gs.current_attackers[:]
            original_block_assignments = {k: v[:] for k, v in gs.current_block_assignments.items()}

            # Set attackers for simulation
            gs.current_attackers = list(attackers) # Ensure it's a list
            gs.current_block_assignments = {} # Simulate blocks from scratch

            # Simulate combat (including optimal blocks estimation)
            # simulate_combat might need internal optimal block simulation first
            if hasattr(gs.combat_resolver, '_simulate_opponent_blocks'):
                gs.combat_resolver._simulate_opponent_blocks() # Simulate blocks based on current attackers
            simulation_results = gs.combat_resolver.simulate_combat()

            # Restore original state
            gs.current_attackers = original_attackers
            gs.current_block_assignments = original_block_assignments

            # Evaluate based on simulation results
            if isinstance(simulation_results, dict) and "expected_value" in simulation_results:
                # Add strategic adjustments based on game state
                value = simulation_results["expected_value"]
                # Apply aggression/risk modifiers?
                value += (self.game_state.strategic_planner.aggression_level - 0.5) * 0.1
                return value
            else:
                 logging.warning(f"Combat simulation did not return expected dictionary: {simulation_results}")
                 return -0.1 # Default penalty if simulation failed

        # Fallback if resolver or simulate_combat not found
        logging.warning("Combat simulation not available, using basic evaluation.")
        if not attackers: return -0.2 # Penalize not attacking if possible
        power = sum(getattr(gs._safe_get_card(a),'power',0) for a in attackers)
        return power * 0.1 # Simple evaluation
        
    def find_optimal_attack(self):
        """
        Find the optimal combination of attackers using strategic evaluation and combat simulation.
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Get valid attackers
        potential_attackers = [cid for cid in me["battlefield"] if self.is_valid_attacker(cid)]

        if not potential_attackers: return []

        # Use the combat resolver's specialized method if available
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, 'find_optimal_attack'):
            return gs.combat_resolver.find_optimal_attack(potential_attackers)

        # Fallback: Simplified evaluation if resolver method unavailable
        logging.warning("Using fallback find_optimal_attack.")
        import itertools
        best_combo, best_value = [], -float('inf')

        # Generate combinations (limit complexity)
        max_attackers = min(len(potential_attackers), 6) # Limit combinations
        for i in range(1, max_attackers + 1):
            for combo in itertools.combinations(potential_attackers, i):
                 # Evaluate this combination (using simplified eval here)
                 combo_power = sum(getattr(gs._safe_get_card(cid),'power',0) for cid in combo)
                 # Simple eval: just total power
                 value = combo_power
                 if value > best_value:
                      best_value = value; best_combo = list(combo)

        # Always consider attacking with all valid attackers if feasible
        if len(potential_attackers) <= 6:
             value = sum(getattr(gs._safe_get_card(cid),'power',0) for cid in potential_attackers)
             if value > best_value: best_combo = potential_attackers[:]

        logging.debug(f"Fallback optimal attack: {len(best_combo)} attackers with value {best_value:.2f}")
        return best_combo
        
    def is_valid_attacker(self, card_id):
        """Determine if a creature can attack, incorporating dynamic restrictions. Uses centralized keyword check."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        player = gs.get_card_controller(card_id)

        # Basic checks
        if not card or not player or card_id not in player.get("battlefield", []): return False
        if 'creature' not in getattr(card, 'card_types', []): return False

        # Tapped check
        if card_id in player.get("tapped_permanents", set()): return False

        # Summoning Sickness check (using central keyword check for haste)
        if card_id in player.get("entered_battlefield_this_turn", set()) and not self._has_keyword(card, "haste"):
             return False

        # Defender check (using central keyword check)
        if self._has_keyword(card, "defender"):
             # Simple exception check - might be overridden by layer effects
             if "can attack as though it didn't have defender" not in getattr(card, 'oracle_text', '').lower():
                  return False

        # --- Check Layer System Effects for 'cant_attack' ---
        cant_attack = False
        if hasattr(gs, 'layer_system') and gs.layer_system:
            # This assumes LayerSystem calculates the 'keywords' array correctly,
            # including 'cant_attack' as a negative ability/restriction.
            # Need a consistent way to represent this. Let's assume 'cant_attack' is a pseudo-keyword.
            try:
                if self._has_keyword(card, "cant_attack"): # Check the effective keywords
                    cant_attack = True
            except Exception as e:
                 logging.warning(f"Error checking LayerSystem cant_attack effect: {e}")
        # Direct check if LayerSystem doesn't use keyword array for this
        # elif hasattr(gs, 'layer_system') and hasattr(gs.layer_system, 'has_effect'):
        #     if gs.layer_system.has_effect(card_id, 'cant_attack'): cant_attack = True

        if cant_attack:
            logging.debug(f"Attacker {card.name} invalid: 'Can't Attack' effect active.")
            return False

        # Check other game state restrictions if applicable (e.g., Ghostly Prison effect)
        # if gs.has_attack_restriction(player, card_id): return False # Example hook

        return True # All checks passed
    
    def _initialize_combat_state_tracking(self):
        """Initialize or reset tracking dictionaries for combat state."""
        gs = self.game_state
        # Use setattr to ensure attributes are created if they don't exist
        attrs_defaults = {
            "current_attackers": [],
            "current_block_assignments": {},
            "planeswalker_attack_targets": {},
            "battle_attack_targets": {},
            "planeswalker_protectors": {},
            "first_strike_ordering": {},
            "combat_damage_dealt": False
        }
        for attr, default in attrs_defaults.items():
            if not hasattr(gs, attr):
                setattr(gs, attr, default)
            elif attr == "current_block_assignments": # Ensure nested dicts are cleared
                getattr(gs, attr).clear()
            elif isinstance(default, list): # Clear lists
                 getattr(gs, attr).clear()
            elif isinstance(default, dict): # Clear dicts
                 getattr(gs, attr).clear()
            elif isinstance(default, bool): # Reset flags
                 setattr(gs, attr, default)
        logging.debug("Combat state tracking reset/initialized")
    
    def handle_first_strike_order(self):
        """Set the damage assignment order for first strike combat."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2 # The ATTACKING player assigns damage order

        for attacker_id, blockers in gs.current_block_assignments.items():
            if len(blockers) <= 1: continue # No order needed
            attacker_card = gs._safe_get_card(attacker_id)
            if not attacker_card: continue

            # Get player choice for order (AI needs to provide this)
            # Placeholder: Default order (e.g., by toughness asc)
            defender = gs.p2 if player == gs.p1 else gs.p1
            ordered_blockers = sorted(blockers, key=lambda bid: getattr(gs._safe_get_card(bid), 'toughness', 0))

            gs.first_strike_ordering[attacker_id] = ordered_blockers # Store chosen order
            logging.debug(f"Set damage assignment order for {attacker_card.name}: {[gs._safe_get_card(bid).name for bid in ordered_blockers]}")

        return True # Succeeded in setting (or determining no need for) orders
    
    def handle_assign_combat_damage(self, damage_assignments=None):
        """Handle assignment of combat damage."""
        gs = self.game_state
        if not gs.combat_resolver: return False

        if damage_assignments:
             # Apply manual assignments
             if hasattr(gs.combat_resolver, 'assign_manual_combat_damage'):
                  success = gs.combat_resolver.assign_manual_combat_damage(damage_assignments)
             else:
                  logging.warning("Manual damage assignment not supported by resolver.")
                  success = False # Fallback: Fail if resolver missing function
        else:
             # Auto-resolve damage if no specific assignments given
             _ = gs.combat_resolver.resolve_combat() # Resolve combat automatically
             success = True

        if success:
             # Move to next phase (End of Combat) if damage resolution succeeded
             gs.phase = gs.PHASE_END_OF_COMBAT
             gs.priority_player = gs._get_active_player()
             gs.priority_pass_count = 0
        return success
    
    def handle_attack_battle(self, battle_target_idx):
        """Assign last declared attacker to target a specific battle. Param is battle index (0-4)."""
        gs = self.game_state
        # Check if it's the right phase and if attackers have been declared
        if gs.phase != gs.PHASE_DECLARE_ATTACKERS or not gs.current_attackers:
            logging.warning("Cannot assign battle target outside Declare Attackers phase or with no attackers.")
            return False # Changed return value to indicate failure

        opponent = gs.p2 if gs.agent_is_p1 else gs.p1
        # Get battles relative to opponent's battlefield
        opponent_battles = [(idx, cid) for idx, cid in enumerate(opponent["battlefield"])
                            if gs._safe_get_card(cid) and 'battle' in getattr(gs._safe_get_card(cid), 'type_line', '')]

        # Parameter battle_target_idx is 0-4, maps to index within opponent_battles list
        if 0 <= battle_target_idx < len(opponent_battles):
            # Find the absolute battlefield index and card ID of the target battle
            abs_bf_idx, battle_id = opponent_battles[battle_target_idx] # abs_bf_idx is the index on opponent's full battlefield

            # --- Assign Attacker Rule ---
            # Rule: Assume the *last* creature added to gs.current_attackers is the one choosing this target.
            # This requires the agent to declare attacker THEN declare target (if not player).
            if not gs.current_attackers:
                logging.warning("No attacker declared before assigning battle target.")
                return False # Should have declared an attacker first
            attacker_id = gs.current_attackers[-1]
            attacker_card = gs._safe_get_card(attacker_id)
            if not attacker_card: return False # Attacker card not found?

            # Ensure the battle_attack_targets dict exists
            if not hasattr(gs, 'battle_attack_targets'): gs.battle_attack_targets = {}

            # Remove any previous target assignment for this attacker (if re-assigning target mid-declaration)
            if attacker_id in gs.battle_attack_targets: del gs.battle_attack_targets[attacker_id]
            if hasattr(gs, 'planeswalker_attack_targets') and attacker_id in gs.planeswalker_attack_targets: del gs.planeswalker_attack_targets[attacker_id]

            # Assign attacker to battle
            gs.battle_attack_targets[attacker_id] = battle_id
            battle_card = gs._safe_get_card(battle_id)
            logging.debug(f"Attacker {attacker_card.name} now targeting Battle {battle_card.name} (Opp BF Idx {abs_bf_idx})")
            return True # Action successful
        else:
            logging.warning(f"Invalid battle target index {battle_target_idx}. Available battles: {len(opponent_battles)}")
            return False # Invalid index selected

    # --- Helpers for finding targets based on identifiers ---
    def _find_planeswalker_target(self, pw_identifier):
        gs = self.game_state
        opponent = gs.p2 if gs.agent_is_p1 else gs.p1
        pw_targets_on_stack = getattr(gs, 'planeswalker_attack_targets', {})

        target_pw_id = None
        # Try finding by ID first
        if isinstance(pw_identifier, str):
            if pw_identifier in opponent["battlefield"]: target_pw_id = pw_identifier
        # Try finding by index relative to opponent's PWs
        elif isinstance(pw_identifier, int):
             opponent_pws = [(idx, cid) for idx, cid in enumerate(opponent["battlefield"]) if gs._safe_get_card(cid) and 'planeswalker' in getattr(gs._safe_get_card(cid), 'card_types', [])]
             if 0 <= pw_identifier < len(opponent_pws):
                  target_pw_id = opponent_pws[pw_identifier][1]

        # Find attacker targeting this PW ID
        if target_pw_id:
             for atk_id, target_pw in pw_targets_on_stack.items():
                  if target_pw == target_pw_id:
                       return target_pw_id, atk_id
        return None, None

    def _find_battle_target(self, battle_identifier):
        gs = self.game_state
        opponent = gs.p2 if gs.agent_is_p1 else gs.p1
        battle_targets_on_stack = getattr(gs, 'battle_attack_targets', {})

        target_battle_id = None
        # Try finding by ID first
        if isinstance(battle_identifier, str):
             if battle_identifier in opponent["battlefield"]: target_battle_id = battle_identifier
        # Try finding by index relative to opponent's Battles
        elif isinstance(battle_identifier, int):
            opponent_battles = [(idx, cid) for idx, cid in enumerate(opponent["battlefield"]) if gs._safe_get_card(cid) and 'battle' in getattr(gs._safe_get_card(cid), 'type_line', '')]
            if 0 <= battle_identifier < len(opponent_battles):
                target_battle_id = opponent_battles[battle_identifier][1]

        # Find attacker targeting this Battle ID
        if target_battle_id:
             for atk_id, target_battle in battle_targets_on_stack.items():
                  if target_battle == target_battle_id:
                       return target_battle_id, atk_id
        return None, None

    # Helper to find a permanent ID from index or string ID
    def _find_permanent_id(self, player, identifier):
        """Finds a permanent ID on the player's battlefield using index or ID string."""
        if isinstance(identifier, int):
             if 0 <= identifier < len(player["battlefield"]):
                  return player["battlefield"][identifier]
        elif isinstance(identifier, str):
             # Check if it's a direct ID
             if identifier in player["battlefield"]:
                  return identifier
             # Could potentially add lookup by name here if needed, but ID/index preferred
        return None

    # Helper to find a card ID in hand from index or ID string
    def _find_card_in_hand(self, player, identifier):
        """Finds a card ID in the player's hand using index or ID string."""
        if isinstance(identifier, int):
             if 0 <= identifier < len(player["hand"]):
                  return player["hand"][identifier]
        elif isinstance(identifier, str):
             if identifier in player["hand"]:
                  return identifier
        return None

    def handle_ninjutsu(self, param=None, context=None, **kwargs):
        """Handle the ninjutsu mechanic. Expects ('ninja_identifier', 'attacker_identifier') in context."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2 # Player performing ninjutsu
        if context is None: context = {}

        # --- Get Parameters from Context ---
        # Assume context keys like 'ninja_hand_idx', 'attacker_bf_idx' are provided if param not used.
        # Use descriptive keys: 'ninja_identifier' and 'attacker_identifier' (can be index or ID).
        ninja_identifier = context.get('ninja_identifier')
        attacker_identifier = context.get('attacker_identifier')

        # Fallback logic using param if context keys are missing - LESS ROBUST
        # Assumes param contains a tuple or other structure if used this way.
        if ninja_identifier is None and attacker_identifier is None and isinstance(param, tuple) and len(param) == 2:
            ninja_identifier, attacker_identifier = param
            logging.warning("Using 'param' for Ninjutsu identifiers - context preferred.")

        if ninja_identifier is None or attacker_identifier is None:
            logging.error(f"Ninjutsu handler missing parameters 'ninja_identifier' or 'attacker_identifier' in context: {context} / param: {param}")
            return False

        # --- Validate Ninja ---
        ninja_id = self._find_card_in_hand(player, ninja_identifier)
        if not ninja_id: logging.warning(f"Invalid ninja identifier: {ninja_identifier}."); return False
        ninja_card = gs._safe_get_card(ninja_id)
        # Check using central keyword check now
        if not ninja_card or not self._has_keyword(ninja_card, "ninjutsu"):
            logging.warning(f"Card {getattr(ninja_card, 'name', 'N/A')} lacks Ninjutsu.")
            return False

        # --- Validate Attacker ---
        attacker_id = self._find_permanent_id(player, attacker_identifier)
        if not attacker_id: logging.warning(f"Invalid attacker identifier: {attacker_identifier}."); return False
        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card:
            logging.warning(f"Attacker card not found for ID {attacker_id}"); return False
        if attacker_id not in getattr(gs, 'current_attackers', []): # Check against gs list
            logging.warning(f"Selected permanent {attacker_card.name} is not a currently declared attacker."); return False
        # Check if unblocked
        if getattr(gs, 'current_block_assignments', {}).get(attacker_id): # Check if key exists and has blockers
            logging.warning("Attacker is blocked, cannot use Ninjutsu."); return False

        # --- Pay Cost ---
        ninjutsu_cost_str = self._get_ninjutsu_cost_str(ninja_card)
        if not ninjutsu_cost_str or not self._can_afford_cost_string(player, ninjutsu_cost_str):
             logging.warning(f"Cannot pay Ninjutsu cost {ninjutsu_cost_str}."); return False
        if not hasattr(gs, 'mana_system') or not gs.mana_system or not gs.mana_system.pay_mana_cost(player, ninjutsu_cost_str):
             logging.warning(f"Failed to pay Ninjutsu cost {ninjutsu_cost_str}.")
             # Need mana system rollback? Assume cost failed cleanly for now.
             return False # Payment failed

        # --- Perform Swap ---
        logging.debug(f"Performing Ninjutsu: Returning {attacker_card.name}, Putting {ninja_card.name} onto battlefield attacking.")
        success_return = gs.move_card(attacker_id, player, "battlefield", player, "hand", cause="ninjutsu_return")
        if not success_return: logging.error("Failed to return attacker for Ninjutsu."); return False

        success_enter = gs.move_card(ninja_id, player, "hand", player, "battlefield", cause="ninjutsu_enter")
        if not success_enter:
            logging.error("Failed to put ninja onto battlefield.")
            # Attempt rollback of attacker
            gs.move_card(attacker_id, player, "hand", player, "battlefield")
            # Need cost refund mechanism? Complex.
            return False

        # Tap the ninja entering, add it to attackers, remove original attacker
        gs.tap_permanent(ninja_id, player)
        if hasattr(gs, 'current_attackers'):
            if attacker_id in gs.current_attackers: gs.current_attackers.remove(attacker_id)
            gs.current_attackers.append(ninja_id)

        # Transfer attack target (Planeswalker/Battle)
        pw_targets = getattr(gs, 'planeswalker_attack_targets', {})
        battle_targets = getattr(gs, 'battle_attack_targets', {})
        target_description = "" # For logging
        if attacker_id in pw_targets:
             target_id = pw_targets.pop(attacker_id)
             pw_targets[ninja_id] = target_id
             target_description = f" (Target: {gs._safe_get_card(target_id).name})"
        if attacker_id in battle_targets:
             target_id = battle_targets.pop(attacker_id)
             battle_targets[ninja_id] = target_id
             target_description = f" (Target: {gs._safe_get_card(target_id).name})"


        logging.info(f"Ninjutsu successful: {attacker_card.name} returned, {ninja_card.name} entered attacking{target_description}.")
        # Ninjas often have ETB triggers, check for them
        gs.trigger_ability(ninja_id, "ENTERS_BATTLEFIELD", {"controller": player, "used_ninjutsu": True})
        return True
    
    def handle_declare_attackers_done(self):
        """Handle the end of the declare attackers phase."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_ATTACKERS:
             logging.warning(f"Tried to end Declare Attackers in phase {gs.phase}")
             return False
        gs.phase = gs.PHASE_DECLARE_BLOCKERS
        gs.priority_player = gs._get_non_active_player() # Priority to blocker
        gs.priority_pass_count = 0
        logging.debug(f"Ended Declare Attackers. Priority to {gs.priority_player['name']} in Declare Blockers.")
        return True
    
    def handle_declare_blockers_done(self):
        """Handle the end of the declare blockers phase."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS:
             logging.warning(f"Tried to end Declare Blockers in phase {gs.phase}")
             return False

        # Determine if First Strike combat step is needed
        needs_first_strike_step = False
        combatants = gs.current_attackers[:]
        for blockers in gs.current_block_assignments.values(): combatants.extend(blockers)
        for cid in combatants:
             card = gs._safe_get_card(cid)
             if card and (self._has_keyword(card, "first strike") or self._has_keyword(card, "double strike")):
                  needs_first_strike_step = True; break

        if needs_first_strike_step:
             gs.phase = gs.PHASE_FIRST_STRIKE_DAMAGE
             logging.debug("Ended Declare Blockers. Moving to First Strike Damage.")
        else:
             gs.phase = gs.PHASE_COMBAT_DAMAGE
             logging.debug("Ended Declare Blockers. Moving to Combat Damage (no first strike).")

        gs.combat_damage_dealt = False # Reset flag before damage steps
        gs.priority_player = gs._get_active_player() # Priority back to active player for damage step
        gs.priority_pass_count = 0
        return True
    
    def handle_attack_planeswalker(self, pw_target_idx):
        """Handle attack targeting a planeswalker."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_ATTACKERS or not gs.current_attackers: return False

        opponent = gs.p2 if gs.agent_is_p1 else gs.p1
        opponent_planeswalkers = [(idx, cid) for idx, cid in enumerate(opponent["battlefield"])
                                   if gs._safe_get_card(cid) and 'planeswalker' in getattr(gs._safe_get_card(cid), 'card_types', [])]

        if 0 <= pw_target_idx < len(opponent_planeswalkers):
            abs_bf_idx, pw_id = opponent_planeswalkers[pw_target_idx]
            attacker_id = gs.current_attackers[-1] # Assign to last declared attacker
            if not hasattr(gs, 'planeswalker_attack_targets'): gs.planeswalker_attack_targets = {}
            gs.planeswalker_attack_targets[attacker_id] = pw_id
            logging.debug(f"{gs._safe_get_card(attacker_id).name} now targeting PW {gs._safe_get_card(pw_id).name}")
            return True
        return False


    def handle_assign_multiple_blockers(self, param, context, **kwargs):
        """Handle assigning multiple blockers. Attacker index from PARAM, blocker identifiers from CONTEXT."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS: return False, False # Ensure correct phase
        if context is None: context = {}

        attacker_idx = param # Param is the attacker index (0-9)
        if attacker_idx is None or not isinstance(attacker_idx, int) or not (0 <= attacker_idx < len(gs.current_attackers)):
            logging.error(f"Invalid or missing attacker index for multi-block: {attacker_idx}")
            return -0.15, False
        attacker_id = gs.current_attackers[attacker_idx]
        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card: return -0.15, False

        # --- Get Blocker Identifiers from Context ---
        blocker_identifiers = context.get('blocker_identifiers') # List of indices or IDs
        if not blocker_identifiers or not isinstance(blocker_identifiers, list):
            logging.error("Missing or invalid 'blocker_identifiers' list in context for multi-block.")
            return -0.15, False

        # --- Validate Blockers ---
        player = gs._get_non_active_player() # Player controlling blockers
        valid_blocker_ids = []
        for identifier in blocker_identifiers:
            # Use helper to find ID from index or string ID
            blocker_id = self._find_permanent_id(player, identifier)
            if not blocker_id:
                 logging.warning(f"Invalid blocker identifier {identifier} for multi-block.")
                 return -0.1, False
            blocker_card = gs._safe_get_card(blocker_id)
            if not blocker_card: return -0.1, False

            if not self._can_block(blocker_id, attacker_id):
                 logging.warning(f"Blocker {blocker_card.name} cannot block {attacker_card.name}")
                 return -0.1, False
            valid_blocker_ids.append(blocker_id)

        if len(valid_blocker_ids) < 2:
            logging.warning("Must assign at least 2 valid blockers for ASSIGN_MULTIPLE_BLOCKERS action.")
            return -0.1, False

        # Check Menace explicitly if needed (though _can_block might implicitly handle)
        if self._has_keyword(attacker_card, "menace") and len(valid_blocker_ids) < 2:
             logging.warning(f"Menace requires at least 2 blockers, only {len(valid_blocker_ids)} valid blockers assigned.")
             return -0.1, False

        # --- Assign Block ---
        if not hasattr(gs, 'current_block_assignments'): gs.current_block_assignments = {}
        # Replace any existing single blocks for this attacker with the multi-block
        gs.current_block_assignments[attacker_id] = valid_blocker_ids

        blocker_names = [getattr(gs._safe_get_card(bid), 'name', bid) for bid in valid_blocker_ids]
        logging.info(f"Assigned multiple blockers ({', '.join(blocker_names)}) to {attacker_card.name}")
        return 0.15, True # Higher reward for complex block
    

    def handle_defend_battle(self, param=None, context=None, **kwargs):
        """Assign a creature to block an attacker targeting a battle. Expects (battle_identifier, defender_identifier) in context."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS: return False
        if context is None: context = {}

        # --- Get Parameters from Context ---
        battle_identifier = context.get('battle_identifier') # Use consistent key
        defender_identifier = context.get('defender_identifier') # Use consistent key

        if battle_identifier is None or defender_identifier is None:
            logging.error(f"Defend Battle handler missing parameters in context: {context}")
            return False

        # --- Find Battle Being Attacked and the Attacker ---
        target_battle_id, attacker_id = self._find_battle_target(battle_identifier)
        if not attacker_id:
            logging.warning(f"Battle {battle_identifier} not found or not being attacked.")
            return False

        # --- Find Defender ---
        player = gs.p1 if gs.agent_is_p1 else gs.p2 # Player controlling the blocker
        defender_id = self._find_permanent_id(player, defender_identifier)
        if not defender_id:
             logging.warning(f"Invalid defender identifier {defender_identifier}.")
             return False

        # --- Validate Blocker ---
        if not self._can_block(defender_id, attacker_id):
            logging.warning(f"Defender {gs._safe_get_card(defender_id).name} cannot block attacker {gs._safe_get_card(attacker_id).name}")
            return False

        # --- Assign Block ---
        if attacker_id not in gs.current_block_assignments: gs.current_block_assignments[attacker_id] = []
        if defender_id not in gs.current_block_assignments[attacker_id]:
             gs.current_block_assignments[attacker_id].append(defender_id)
             logging.info(f"{gs._safe_get_card(defender_id).name} assigned to block {gs._safe_get_card(attacker_id).name} (defending Battle {gs._safe_get_card(target_battle_id).name})")
             return True
        logging.debug("Blocker already assigned to this attacker.")
        return False # Already assigned
    
    def _add_battle_attack_actions(self, player, valid_actions, set_valid_action):
        """Add actions for attacking battle cards."""
        gs = self.game_state
        
        # Only applicable in certain phases
        if gs.phase not in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT, gs.PHASE_DECLARE_ATTACKERS]:
            return
            
        # Get opponent's battlefield
        opponent = gs.p2 if player == gs.p1 else gs.p1
        
        # Find battle cards on opponent's battlefield
        battle_cards = []
        for idx, card_id in enumerate(opponent["battlefield"]):
            if idx >= 5:  # Limit to 5 battle cards
                break
                
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_battle') and card.is_battle:
                battle_cards.append((idx, card_id, card))
        
        if not battle_cards:
            return  # No battle cards to attack
            
        # Get available untapped creatures
        available_creatures = []
        for idx, card_id in enumerate(player["battlefield"]):
            if idx >= 20:  # Limit to 20 creatures
                break
                
            card = gs._safe_get_card(card_id)
            if (card and hasattr(card, 'card_types') and 'creature' in card.card_types and 
                card_id not in player.get("tapped_permanents", set())):
                
                # Check for summoning sickness
                has_haste = self._has_keyword(card, "haste")
                if card_id in player.get("entered_battlefield_this_turn", set()) and not has_haste:
                    continue  # Skip creatures with summoning sickness
                    
                available_creatures.append((idx, card_id, card))
        
        # For each battle card, add action using indices 462-466 (for battle 0-4)
        for battle_idx, battle_id, battle_card in enumerate(battle_cards):
            if battle_idx >= 5: break  # Only handle 5 battles max
            
            # Use correct action index from ACTION_MEANINGS (462-466)
            action_idx = 462 + battle_idx
            
            # Battle info and damage potential
            battle_info = f" (Defense: {battle_card.defense})" if hasattr(battle_card, 'defense') else ""
            
            set_valid_action(action_idx, 
                f"ATTACK_BATTLE {battle_card.name}{battle_info}")
                
    def _add_attack_declaration_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions specific to the Declare Attackers step. (Called by ActionHandler)"""
        gs = self.game_state
        # Declare Attackers
        possible_attackers = []
        player_battlefield = player.get("battlefield", [])
        for i in range(min(len(player_battlefield), 20)): # Indices 0-19 map to actions 28-47
            try:
                card_id = player_battlefield[i]
                # Use internal validation which delegates back to GS/Layers etc.
                if self.is_valid_attacker(card_id):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, 'name', f'Creature {i}')
                    set_valid_action(28 + i, f"ATTACK with {card_name}")
                    possible_attackers.append((i, card_id)) # Store index and ID
            except IndexError:
                logging.warning(f"Combat Handler: IndexError accessing battlefield for ATTACK at index {i}")
                break

        # Add actions for declaring targets for attackers (Planeswalkers, Battles)
        if possible_attackers:
            # Add actions for attacking Planeswalkers (action indices 378-382, corrected from 373-377)
            opponent_planeswalkers = [(idx, card_id) for idx, card_id in enumerate(opponent.get("battlefield", []))
                                        if gs._safe_get_card(card_id) and 'planeswalker' in getattr(gs._safe_get_card(card_id), 'card_types', [])]
            for pw_rel_idx in range(min(len(opponent_planeswalkers), 5)): # PW relative index 0-4
                pw_abs_idx, pw_id = opponent_planeswalkers[pw_rel_idx]
                pw_card = gs._safe_get_card(pw_id)
                pw_name = getattr(pw_card, 'name', f'PW {pw_rel_idx}')
                # Corrected action index to match ACTION_MEANINGS (378-382)
                set_valid_action(378 + pw_rel_idx, f"Target PLANESWALKER: {pw_name}")

            # Add actions for attacking Battles (action indices 462-466, corrected from 460-464)
            opponent_battles = [(idx, card_id) for idx, card_id in enumerate(opponent.get("battlefield", []))
                                if gs._safe_get_card(card_id) and 'battle' in getattr(gs._safe_get_card(card_id), 'type_line', '')]
            for battle_rel_idx in range(min(len(opponent_battles), 5)): # Battle relative index 0-4
                battle_abs_idx, battle_id = opponent_battles[battle_rel_idx]
                battle_card = gs._safe_get_card(battle_id)
                battle_name = getattr(battle_card, 'name', f'Battle {battle_rel_idx}')
                # Corrected action index to match ACTION_MEANINGS (462-466)
                set_valid_action(462 + battle_rel_idx, f"Target BATTLE: {battle_name}")

        # Always allow finishing declaration if player has declared at least one action or no valid attacks
        # Corrected from 433 to 438 to match ACTION_MEANINGS
        set_valid_action(438, "Finish Declaring Attackers")

    def _add_block_declaration_actions(self, player, valid_actions, set_valid_action):
        """Adds actions specific to the Declare Blockers step. (Called by ActionHandler)"""
        gs = self.game_state
        if not getattr(gs, 'current_attackers', []): return

        player_battlefield = player.get("battlefield", [])
        possible_blockers = []
        for i in range(min(len(player_battlefield), 20)): # Indices 0-19 map to actions 48-67
            try:
                card_id = player_battlefield[i]
                card = gs._safe_get_card(card_id)
                if not card: continue

                if 'creature' not in getattr(card, 'card_types', []) or card_id in player.get("tapped_permanents", set()):
                    continue

                can_block_anything = False
                for attacker_id in gs.current_attackers:
                    if self._can_block(card_id, attacker_id):
                        can_block_anything = True
                        break
                if can_block_anything:
                    card_name = getattr(card, 'name', f'Blocker {i}')
                    is_currently_blocking = any(card_id in blockers for blockers in gs.current_block_assignments.values())
                    action_text = "Assign Block" if not is_currently_blocking else "Unassign Block"
                    set_valid_action(48 + i, f"{action_text} with {card_name}")
                    possible_blockers.append((i, card_id))
            except IndexError:
                logging.warning(f"Combat Handler: IndexError accessing battlefield for BLOCK at index {i}")
                break

        # Assign multiple blockers action - corrected indices from 383-392 to match ACTION_MEANINGS
        if len(possible_blockers) >= 2:
            for atk_idx, attacker_id in enumerate(gs.current_attackers[:10]):
                attacker_card = gs._safe_get_card(attacker_id)
                attacker_name = getattr(attacker_card, 'name', f"Attacker {atk_idx}") if attacker_card else f"Attacker {atk_idx}"
                valid_multi_blockers_for_attacker = [b_id for _, b_id in possible_blockers if self._can_block(b_id, attacker_id)]
                if len(valid_multi_blockers_for_attacker) >= 2:
                    # Corrected from 383 to match ACTION_MEANINGS
                    set_valid_action(383 + atk_idx, f"Assign Multiple Blockers to {attacker_name}")

        # Protect planeswalker action - corrected from 439 to 444
        attacked_pws = getattr(gs, 'planeswalker_attack_targets', {}).values()
        if attacked_pws and possible_blockers:
            set_valid_action(444, "Assign Blocker to protect Planeswalker")

        # Defend battle action - already correctly using 204
        attacked_battles = getattr(gs, 'battle_attack_targets', {}).values()
        if attacked_battles and possible_blockers:
            set_valid_action(204, "Assign Blocker to defend Battle")

        # Allow finishing block declaration - corrected from 434 to 439
        set_valid_action(439, "Finish Declaring Blockers")


    def _add_combat_damage_actions(self, player, valid_actions, set_valid_action):
        """Adds actions for assigning combat damage order if needed. (Called by ActionHandler)"""
        gs = self.game_state
        # Check if damage assignment order is needed (multiple blockers assigned)
        needs_order_assignment = False
        for attacker_id, blockers in gs.current_block_assignments.items():
            if len(blockers) > 1:
                attacker_card = gs._safe_get_card(attacker_id)
                # Check power to see if damage assignment matters
                if attacker_card and (getattr(attacker_card, 'power', 0) or 0) > 0:
                    needs_order_assignment = True
                    break
        if needs_order_assignment:
            # Corrected from 430 to 435 to match ACTION_MEANINGS
            set_valid_action(435, "Assign Combat Damage Order")

        # Corrected from 431 to 436 to match ACTION_MEANINGS
        set_valid_action(436, "Resolve Combat Damage")


    def handle_protect_planeswalker(self, param=None, context=None, **kwargs):
        """Assign a creature to protect a planeswalker. Expects (pw_identifier, defender_identifier) in context."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS:
            logging.warning("Cannot protect PW outside Declare Blockers phase.")
            return False
        if context is None: context = {}

        # --- Get Parameters from Context ---
        pw_identifier = context.get('pw_identifier') # Use consistent key
        defender_identifier = context.get('defender_identifier') # Use consistent key

        if pw_identifier is None or defender_identifier is None:
            logging.error(f"Protect Planeswalker handler missing parameters in context: {context}")
            return False

        # --- Find Planeswalker Being Attacked ---
        # Use _find_planeswalker_target helper which uses context identifiers
        target_pw_id, attacker_id = self._find_planeswalker_target(pw_identifier)
        if not attacker_id:
            logging.warning(f"PW {pw_identifier} not found or not being attacked.")
            return False

        # --- Find Defender ---
        # Blocker is the non-agent player
        player = gs.p1 if not gs.agent_is_p1 else gs.p2
        defender_id = self._find_permanent_id(player, defender_identifier)
        if not defender_id:
             logging.warning(f"Invalid defender identifier {defender_identifier}.")
             return False
        defender_card = gs._safe_get_card(defender_id)
        attacker_card = gs._safe_get_card(attacker_id)
        if not defender_card or not attacker_card: return False # Safety

        # --- Validate Blocker ---
        if not self._can_block(defender_id, attacker_id):
            logging.warning(f"Defender {defender_card.name} cannot block attacker {attacker_card.name}")
            return False

        # --- Assign Block ---
        if not hasattr(gs, 'current_block_assignments'): gs.current_block_assignments = {}
        if attacker_id not in gs.current_block_assignments: gs.current_block_assignments[attacker_id] = []
        if defender_id not in gs.current_block_assignments[attacker_id]:
             gs.current_block_assignments[attacker_id].append(defender_id)
             logging.info(f"{defender_card.name} assigned to block {attacker_card.name} (protecting PW {gs._safe_get_card(target_pw_id).name})")
             return True
        logging.debug("Blocker already assigned to this attacker.")
        return False # Already assigned
    
    # --- Mana Cost String Helpers ---
    def _get_equip_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
            # Match 'equip' followed optionally by em dash or hyphen, then cost
            match = re.search(r"equip\s*(?:-|—)?\s*(\{.*?\})", card.oracle_text.lower())
            if match: return match.group(1)
            match = re.search(r"equip\s*(\d+)\b", card.oracle_text.lower()) # Match digits only if bracketed cost not found
            if match: return f"{{{match.group(1)}}}"
        return None

    def _get_reconfigure_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"reconfigure\s*(?:-|—)?\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"reconfigure\s*(\d+)\b", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
        return None

    def _get_ninjutsu_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"ninjutsu\s*(?:-|—)?\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             # Ninjutsu usually requires mana cost, less likely just digits
        return None

    def _get_fortify_cost_str(self, card):
         if card and hasattr(card, 'oracle_text'):
             match = re.search(r"fortify\s*(?:-|—)?\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"fortify\s*(\d+)\b", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
         return None

    def _can_afford_cost_string(self, player, cost_string):
        """Helper to check affordability of a cost string."""
        gs = self.game_state
        if not hasattr(gs, 'mana_system') or not gs.mana_system:
            return sum(player.get("mana_pool", {}).values()) >= 1 if cost_string else True
        if not cost_string: return True
        return gs.mana_system.can_pay_mana_cost(player, cost_string)

    def _can_block(self, blocker_id, attacker_id):
        """Check if blocker_id can legally block attacker_id. Uses TargetingSystem."""
        gs = self.game_state
        # --- Check Phasing Status ---
        if hasattr(gs, 'phased_out'):
            if blocker_id in gs.phased_out:
                logging.debug(f"Blocker {blocker_id} cannot block: Phased Out.")
                return False
            if attacker_id in gs.phased_out: # Attacker phased out cannot be blocked
                 logging.debug(f"Attacker {attacker_id} cannot be blocked: Phased Out.")
                 # Is this check correct? Phased-out creatures can't attack. Validation happens earlier.
                 # Assume if attacker is attacking, it's phased in.
                 pass
        # --- End Phasing Check ---

        # Delegate to TargetingSystem preferred
        if hasattr(gs, 'targeting_system') and gs.targeting_system:
            if hasattr(gs.targeting_system, 'check_can_be_blocked'):
                 try:
                     # Add Banding consideration: If attacker has banding, any creature can block it.
                     # If blocker has banding, it can block creatures with landwalk/fear/intimidate.
                     # This interaction logic belongs more in check_can_be_blocked itself.
                     can_be_blocked = gs.targeting_system.check_can_be_blocked(attacker_id, blocker_id)
                     # Post-check modification for Banding:
                     attacker = gs._safe_get_card(attacker_id)
                     if attacker and self._has_keyword(attacker, "banding") and not can_be_blocked:
                          logging.debug(f"Banding allows {blocker_id} to block {attacker_id} despite other restrictions.")
                          can_be_blocked = True # Banding on attacker removes blocking restrictions

                     # Add blocker banding handling inside check_can_be_blocked if possible.
                     # Example (if added here):
                     # blocker = gs._safe_get_card(blocker_id)
                     # if blocker and self._has_keyword(blocker, "banding") and not can_be_blocked:
                     #    # Check specific evasion keywords that banding circumvents
                     #    if self._has_keyword(attacker,"fear") or self._has_keyword(attacker,"intimidate") or gs.targeting_system._get_landwalk_type(attacker):
                     #         logging.debug(f"Banding allows {blocker_id} to block {attacker_id} with evasion.")
                     #         can_be_blocked = True

                     return can_be_blocked
                 except Exception as e:
                      logging.error(f"Error checking block via TargetingSystem: {e}")

        # --- Fallback logic (without Banding interaction) ---
        logging.warning("Using basic _can_block fallback in CombatActionHandler.")
        # ... (keep existing fallback logic, but Banding isn't handled here) ...
        return True


        # --- Fallback logic ---
        logging.warning("Using basic _can_block fallback in CombatActionHandler.")
        blocker = gs._safe_get_card(blocker_id); attacker = gs._safe_get_card(attacker_id)
        if not blocker or not attacker: return False
        if 'creature' not in getattr(blocker, 'card_types', []): return False # Must be creature
        if blocker_id in getattr(gs.get_card_controller(blocker_id), "tapped_permanents", set()): return False # Must be untapped

        # Use central _has_keyword for evasion checks
        if self._has_keyword(attacker, "flying") and not (self._has_keyword(blocker, "flying") or self._has_keyword(blocker, "reach")): return False
        if self._has_keyword(blocker, "can't block"): return False
        if self._has_keyword(attacker, "shadow") and not self._has_keyword(blocker, "shadow"): return False
        if self._has_keyword(attacker, "unblockable"): return False # Basic unblockable
        # Add other evasion/restriction checks if needed (fear, intimidate, landwalk etc.)

        return True

    def _has_keyword(self, card, keyword):
        """Checks if a card has a keyword using the central checker (AbilityHandler preferred)."""
        gs = self.game_state
        card_id = getattr(card, 'card_id', None)
        if not card_id: return False

        # 1. Prefer AbilityHandler (handles static grants/removals)
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            if hasattr(gs.ability_handler, 'check_keyword'):
                 try:
                     # Use AbilityHandler's public method
                     return gs.ability_handler.check_keyword(card_id, keyword)
                 except Exception as e:
                      logging.error(f"Error checking keyword via AbilityHandler in CombatActionHandler: {e}")
                      # Fall through to GameState check on error
            # else: Fall through if check_keyword doesn't exist on handler
        # --- DELEGATION ADDED: Check GameState next ---
        if hasattr(gs, 'check_keyword') and callable(gs.check_keyword):
            try:
                return gs.check_keyword(card_id, keyword)
            except Exception as e:
                 logging.error(f"Error checking keyword via GameState in CombatActionHandler: {e}")
                 
        logging.warning(f"Keyword check failed in CombatActionHandler for {keyword} on {getattr(card, 'name', 'Unknown')}: Delegation methods failed or keyword not found.")
        return False
     
