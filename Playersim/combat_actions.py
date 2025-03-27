import logging
import re
from collections import defaultdict
import numpy as np  # Add if needed for array operations
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
                 self.card_evaluator = EnhancedCardEvaluator(game_state,
                 getattr(game_state, 'stats_tracker', None),
                 getattr(game_state, 'card_memory', None))
                 game_state.card_evaluator = self.card_evaluator
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
            if idx >= 5: break # ACTION_MEANINGS only maps up to index 4 for ATTACK_PLANESWALKER/DEFEND_BATTLE, reuse? Unclear limit.
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
                            # Need a way to map (PW index, ability_idx) or just (PW index) to actions 435-438.
                            # Let's assume param=PW index (idx here) maps correctly for now.
                            # Need context to differentiate which ability (+/-/0/ult) is chosen by the agent.
                            set_valid_action(435, f"LOYALTY_ABILITY_PLUS for {card.name}{warning} (Index {idx})")
                        elif cost == 0:
                            set_valid_action(436, f"LOYALTY_ABILITY_ZERO for {card.name}{warning} (Index {idx})")
                        else: # cost < 0
                            if is_ultimate:
                                set_valid_action(438, f"ULTIMATE_ABILITY for {card.name}{warning} (Index {idx})")
                            else:
                                set_valid_action(437, f"LOYALTY_ABILITY_MINUS for {card.name}{warning} (Index {idx})")

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
        # Can be activated anytime an attacker you control is unblocked. Usually checked after blockers are declared.
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS: # Or maybe also during damage steps before resolution? Rules check. Let's assume just after blockers declared.
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
                          # Action 432 needs params (hand_idx, atk_bf_idx)
                          set_valid_action(432, f"NINJUTSU with {card.name} (H:{hand_idx}) for {gs._safe_get_card(attacker_id).name} (B:{atk_bf_idx}) Cost:{ninjutsu_cost_str}")
        
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
     
        
    def _has_first_strike(self, card):
        """Check if a card has first strike."""
        # Prioritize checking keywords attribute if available and correct length
        if hasattr(card, 'keywords') and isinstance(card.keywords, list) and len(card.keywords) > 5:
            return card.keywords[5] == 1 # Index 5 = First Strike

        # Fallback to oracle text
        if card and hasattr(card, 'oracle_text') and "first strike" in card.oracle_text.lower():
            return True

        return False

            
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
        """Determine if a creature can attack."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        player = gs.get_card_controller(card_id) # Need controller
        if not card or not player or card_id not in player["battlefield"]: return False
        if 'creature' not in getattr(card, 'card_types', []): return False

        # Tapped check
        if card_id in player.get("tapped_permanents", set()): return False

        # Summoning Sickness check
        if card_id in player.get("entered_battlefield_this_turn", set()):
             if not self._has_keyword(card, "haste"):
                  return False

        # Defender check
        if self._has_keyword(card, "defender"):
             # Check for exceptions like "can attack as though it didn't have defender"
             if "can attack" not in getattr(card, 'oracle_text', '').lower():
                  return False

        # Ability Restrictions (Can't Attack etc.)
        if hasattr(gs, 'prevention_effects'):
            for effect in gs.prevention_effects:
                if effect.get('type') == 'attack' and card_id in effect.get('affected_cards', []):
                     if effect.get('condition') is None or effect['condition'](): # Check conditional prevention
                         return False

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


    def handle_ninjutsu(self, ninja_hand_idx_or_id, attacker_bf_idx_or_id):
        """Handle the ninjutsu mechanic."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2 # Player performing ninjutsu

        # --- Validate Ninja ---
        ninja_id = None
        if isinstance(ninja_hand_idx_or_id, int):
             if ninja_hand_idx_or_id < len(player["hand"]): ninja_id = player["hand"][ninja_hand_idx_or_id]
        elif isinstance(ninja_hand_idx_or_id, str):
             if ninja_hand_idx_or_id in player["hand"]: ninja_id = ninja_hand_idx_or_id
        if not ninja_id: logging.warning("Invalid ninja identifier."); return False
        ninja_card = gs._safe_get_card(ninja_id)
        if not ninja_card or "ninjutsu" not in getattr(ninja_card,'oracle_text','').lower():
            logging.warning("Card is not a ninja or lacks Ninjutsu."); return False

        # --- Validate Attacker ---
        attacker_id = None
        if isinstance(attacker_bf_idx_or_id, int):
             if attacker_bf_idx_or_id < len(player["battlefield"]): attacker_id = player["battlefield"][attacker_bf_idx_or_id]
        elif isinstance(attacker_bf_idx_or_id, str):
             if attacker_bf_idx_or_id in player["battlefield"]: attacker_id = attacker_bf_idx_or_id
        if not attacker_id: logging.warning("Invalid attacker identifier."); return False
        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card or attacker_id not in gs.current_attackers:
             logging.warning("Selected permanent is not a valid attacker."); return False
        # Check if unblocked
        if attacker_id in gs.current_block_assignments and gs.current_block_assignments[attacker_id]:
            logging.warning("Attacker is blocked, cannot use Ninjutsu."); return False

        # --- Pay Cost ---
        ninjutsu_cost_str = self._get_ninjutsu_cost_str(ninja_card)
        if not ninjutsu_cost_str or not gs.mana_system or not gs.mana_system.can_pay_mana_cost(player, ninjutsu_cost_str):
             logging.warning(f"Cannot pay Ninjutsu cost {ninjutsu_cost_str}."); return False
        if not gs.mana_system.pay_mana_cost(player, ninjutsu_cost_str): return False

        # --- Perform Swap ---
        # 1. Return attacker to hand
        success_return = gs.move_card(attacker_id, player, "battlefield", player, "hand")
        if not success_return: logging.error("Failed to return attacker for Ninjutsu."); return False # Needs rollback?

        # 2. Put ninja onto battlefield tapped and attacking
        success_enter = gs.move_card(ninja_id, player, "hand", player, "battlefield")
        if not success_enter: logging.error("Failed to put ninja onto battlefield."); return False # Needs rollback?

        gs.tap_permanent(ninja_id, player) # Tap the incoming ninja
        gs.current_attackers.remove(attacker_id) # Remove original attacker
        gs.current_attackers.append(ninja_id) # Add ninja as attacker

        # 3. Transfer attack target (PW/Battle) if applicable
        if hasattr(gs, 'planeswalker_attack_targets') and attacker_id in gs.planeswalker_attack_targets:
             target_id = gs.planeswalker_attack_targets.pop(attacker_id)
             gs.planeswalker_attack_targets[ninja_id] = target_id
             logging.debug(f"{ninja_card.name} now attacking PW {gs._safe_get_card(target_id).name}")
        if hasattr(gs, 'battle_attack_targets') and attacker_id in gs.battle_attack_targets:
             target_id = gs.battle_attack_targets.pop(attacker_id)
             gs.battle_attack_targets[ninja_id] = target_id
             logging.debug(f"{ninja_card.name} now attacking Battle {gs._safe_get_card(target_id).name}")

        logging.info(f"Ninjutsu successful: {attacker_card.name} returned, {ninja_card.name} entered attacking.")
        gs.trigger_ability(ninja_id, "ENTERS_BATTLEFIELD") # Trigger ETB for ninja
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

    
    def handle_assign_multiple_blockers(self, attacker_idx_or_id):
        """Handle selecting an attacker to assign multiple blockers to."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS: return False

        attacker_id = None
        if isinstance(attacker_idx_or_id, int):
             if 0 <= attacker_idx_or_id < len(gs.current_attackers):
                  attacker_id = gs.current_attackers[attacker_idx_or_id]
        elif isinstance(attacker_idx_or_id, str):
             if attacker_idx_or_id in gs.current_attackers: attacker_id = attacker_idx_or_id

        if attacker_id:
             # Set context that the *next* BLOCK actions are for this attacker
             gs.multi_block_target = attacker_id
             logging.debug(f"Preparing to assign multiple blockers to {gs._safe_get_card(attacker_id).name}")
             return True
        return False
    
    def handle_defend_battle(self, battle_idx_or_id, defender_idx_or_id):
        """Assign a creature to defend a battle."""
        gs = self.game_state
        # This isn't standard MTG. Battles have defense counters and are attacked directly.
        # Creatures "defend" by being chosen as blockers *against creatures attacking the battle*.
        # Reinterpret this as assigning a block against a creature attacking a battle.

        if gs.phase != gs.PHASE_DECLARE_BLOCKERS: return False

        # --- Find Battle Being Attacked ---
        battle_id = None
        attacker_targeting_battle = None
        if isinstance(battle_idx_or_id, int): # Assume index of battle on my field
             my_battles = [(idx, cid) for idx, cid in enumerate(gs._get_active_player()["battlefield"]) if gs._safe_get_card(cid) and 'battle' in getattr(gs._safe_get_card(cid), 'type_line', '')]
             if 0 <= battle_idx_or_id < len(my_battles): battle_id = my_battles[battle_idx_or_id][1]
        elif isinstance(battle_idx_or_id, str): # Assume battle ID
             if battle_idx_or_id in gs._get_active_player()["battlefield"]: battle_id = battle_idx_or_id

        if not battle_id: return False

        # Find attacker targeting this battle
        if hasattr(gs, 'battle_attack_targets'):
            for atk_id, target_battle in gs.battle_attack_targets.items():
                 if target_battle == battle_id:
                      attacker_targeting_battle = atk_id
                      break
        if not attacker_targeting_battle: return False # No one attacking this battle

        # --- Find Defender ---
        player = gs._get_active_player() # Player controlling the blocker
        defender_id = None
        if isinstance(defender_idx_or_id, int): # Assume index on player's battlefield
             if 0 <= defender_idx_or_id < len(player["battlefield"]):
                  defender_id = player["battlefield"][defender_idx_or_id]
        elif isinstance(defender_idx_or_id, str): # Assume card ID
             if defender_idx_or_id in player["battlefield"]: defender_id = defender_idx_or_id
        if not defender_id: return False # Invalid defender

        # --- Assign Block ---
        if not self._can_block(defender_id, attacker_targeting_battle):
            logging.warning(f"Defender {gs._safe_get_card(defender_id).name} cannot block attacker {gs._safe_get_card(attacker_targeting_battle).name}")
            return False

        if attacker_targeting_battle not in gs.current_block_assignments: gs.current_block_assignments[attacker_targeting_battle] = []
        if defender_id not in gs.current_block_assignments[attacker_targeting_battle]:
             gs.current_block_assignments[attacker_targeting_battle].append(defender_id)
             logging.info(f"{gs._safe_get_card(defender_id).name} assigned to block {gs._safe_get_card(attacker_targeting_battle).name} (defending Battle {gs._safe_get_card(battle_id).name})")
             return True
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
                has_haste = "haste" in card.oracle_text.lower() if hasattr(card, 'oracle_text') else False
                if card_id in player.get("entered_battlefield_this_turn", set()) and not has_haste:
                    continue  # Skip creatures with summoning sickness
                    
                available_creatures.append((idx, card_id, card))
        
        # For each battle card, add attack actions for available creatures
        for battle_idx, battle_id, battle_card in battle_cards:
            # Battle specific action index starting at 500
            base_action_idx = 500 + (battle_idx * 20)
            
            for creature_idx, creature_id, creature_card in available_creatures:
                action_idx = base_action_idx + creature_idx
                action_name = f"ATTACK_BATTLE"
                
                # Additional battle card info if available
                battle_info = ""
                if hasattr(battle_card, 'defense'):
                    battle_info = f" (Defense: {battle_card.defense})"
                    
                # Calculate damage potential
                damage_potential = creature_card.power if hasattr(creature_card, 'power') else 0
                
                set_valid_action(action_idx, 
                    f"{action_name} {battle_card.name}{battle_info} with {creature_card.name} ({damage_potential} damage)")
    

    def handle_protect_planeswalker(self, pw_idx_or_id, defender_idx_or_id=None):
        """Assign a creature to protect a planeswalker being attacked."""
        gs = self.game_state
        # This isn't a standard MTG action. It represents an AI decision *before* damage.
        # Standard way is to block the creature attacking the PW.
        # Let's adapt this to *set a block assignment*.

        if gs.phase != gs.PHASE_DECLARE_BLOCKERS: return False

        # --- Find Planeswalker ---
        pw_id = None
        if isinstance(pw_idx_or_id, int): # Assume index relative to PWs on my battlefield
            my_pws = [(idx, cid) for idx, cid in enumerate(gs._get_active_player()["battlefield"]) if gs._safe_get_card(cid) and 'planeswalker' in getattr(gs._safe_get_card(cid), 'card_types', [])]
            if 0 <= pw_idx_or_id < len(my_pws): pw_id = my_pws[pw_idx_or_id][1]
        elif isinstance(pw_idx_or_id, str): # Assume card ID
             if pw_idx_or_id in gs._get_active_player()["battlefield"]: pw_id = pw_idx_or_id # Must control the PW being protected? No, PW is being *attacked*.
             # Re-find based on *opponent's* battlefield PWs being attacked.
             opponent = gs._get_non_active_player()
             target_pw_id = None
             if hasattr(gs, 'planeswalker_attack_targets'):
                  # Find attacker targeting this PW
                  for atk_id, target_pw in gs.planeswalker_attack_targets.items():
                       if target_pw == pw_idx_or_id: # If param was PW ID
                            target_pw_id = target_pw
                            attacker_id = atk_id
                            break
             if not target_pw_id: return False # This PW isn't being attacked

        else: return False # Invalid PW identifier

        # --- Find Defender ---
        player = gs._get_active_player() # The player who CONTROLS the potential blocker
        defender_id = None
        if isinstance(defender_idx_or_id, int): # Assume index on player's battlefield
            if 0 <= defender_idx_or_id < len(player["battlefield"]):
                 defender_id = player["battlefield"][defender_idx_or_id]
        elif isinstance(defender_idx_or_id, str): # Assume card ID
            if defender_idx_or_id in player["battlefield"]: defender_id = defender_idx_or_id
        if not defender_id: return False # Invalid defender

        # --- Validate Blocker ---
        if not self._can_block(defender_id, attacker_id): # Check if defender can block attacker
            logging.warning(f"Defender {gs._safe_get_card(defender_id).name} cannot block attacker {gs._safe_get_card(attacker_id).name}")
            return False

        # --- Assign Block ---
        if attacker_id not in gs.current_block_assignments: gs.current_block_assignments[attacker_id] = []
        if defender_id not in gs.current_block_assignments[attacker_id]:
             gs.current_block_assignments[attacker_id].append(defender_id)
             logging.info(f"{gs._safe_get_card(defender_id).name} assigned to block {gs._safe_get_card(attacker_id).name} (protecting PW {gs._safe_get_card(target_pw_id).name})")
             return True
        return False # Already assigned
    
    # --- Mana Cost String Helpers ---
    def _get_equip_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
            match = re.search(r"equip\s*(?:—)?\s*(\{.*?\})", card.oracle_text.lower()) # More robust pattern
            if match: return match.group(1)
            match = re.search(r"equip\s*(\d+)", card.oracle_text.lower())
            if match: return f"{{{match.group(1)}}}"
        return None

    def _get_reconfigure_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"reconfigure\s*(?:—)?\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"reconfigure\s*(\d+)", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
        return None

    def _get_ninjutsu_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"ninjutsu\s*(?:—)?\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"ninjutsu\s*(\d+)", card.oracle_text.lower()) # Fallback for just number? Unlikely for Ninjutsu
             if match: return f"{{{match.group(1)}}}"
        return None

    def _get_fortify_cost_str(self, card):
         if card and hasattr(card, 'oracle_text'):
             match = re.search(r"fortify\s*(?:—)?\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"fortify\s*(\d+)", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
         return None


    def _can_afford_cost_string(self, player, cost_string):
        """Helper to check affordability of a cost string."""
        gs = self.game_state
        if not hasattr(gs, 'mana_system') or not gs.mana_system:
            # Basic check if no mana system
            return sum(player.get("mana_pool", {}).values()) >= 1 if cost_string else True
        if not cost_string: return True
        return gs.mana_system.can_pay_mana_cost(player, cost_string)


    def _can_block(self, blocker_id, attacker_id):
        """Check if blocker_id can legally block attacker_id."""
        gs = self.game_state
        # Use resolver's check if available
        # Adjusted to use check_can_be_blocked from TargetingSystem if resolver missing specific method
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_check_block_restrictions'):
            return gs.combat_resolver._check_block_restrictions(attacker_id, blocker_id)
        elif hasattr(gs, 'targeting_system') and hasattr(gs.targeting_system, 'check_can_be_blocked'):
            return gs.targeting_system.check_can_be_blocked(attacker_id, blocker_id)
        # Basic fallback (should be avoided if possible)
        logging.warning("Using basic _can_block fallback.")
        blocker = gs._safe_get_card(blocker_id); attacker = gs._safe_get_card(attacker_id)
        if not blocker or not attacker: return False
        # ... (keep basic checks like flying/reach/can't block as fallback) ...
        if self._has_keyword(attacker, "flying") and not (self._has_keyword(blocker, "flying") or self._has_keyword(blocker, "reach")): return False
        if self._has_keyword(blocker, "can't block"): return False
        # Assume true for fallback if basic checks pass
        return True

    def _has_keyword(self, card, keyword):
        """Check if card has a keyword, preferring resolver."""
        gs = self.game_state
        # Prefer resolver's check for consistency
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_has_keyword'):
            return gs.combat_resolver._has_keyword(card, keyword)
        # Fallback to card's method or simple text check
        elif hasattr(card, 'has_keyword') and callable(card.has_keyword):
            return card.has_keyword(keyword)
        # Basic text check fallback
        elif hasattr(card, 'oracle_text') and isinstance(card.oracle_text, str):
            return keyword.lower() in card.oracle_text.lower()
        return False
     
     
