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
        
        # Initialize tracking dictionaries for combat state
        self._initialize_combat_state_tracking()
        
        logging.debug("CombatActionHandler initialized")
        
    
    def _add_planeswalker_actions(self, player, valid_actions, set_valid_action):
        """Add actions for planeswalker loyalty abilities."""
        gs = self.game_state
        
        for idx, card_id in enumerate(player["battlefield"][:5]):  # Limit to first 5 planeswalkers
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                # Check if this planeswalker hasn't activated an ability this turn
                already_activated = False
                if hasattr(gs, 'planeswalker_abilities_used'):
                    already_activated = card_id in gs.planeswalker_abilities_used
                elif hasattr(player, "activated_this_turn"):
                    already_activated = card_id in player["activated_this_turn"]
                
                # Allow actions for all planeswalkers, but add warning if already activated
                warning = " (ALREADY ACTIVATED - WILL BE PENALIZED)" if already_activated else ""
                
                # Get current loyalty count
                current_loyalty = 0
                if hasattr(player, "loyalty_counters") and card_id in player["loyalty_counters"]:
                    current_loyalty = player["loyalty_counters"][card_id]
                else:
                    current_loyalty = getattr(card, 'loyalty', 0)
                    
                # Check for loyalty abilities
                if hasattr(card, 'loyalty_abilities'):
                    for ability_idx, ability in enumerate(card.loyalty_abilities):
                        if ability.get('cost', 0) > 0:
                            set_valid_action(435, f"LOYALTY_ABILITY_PLUS for {card.name}{warning}")
                        elif ability.get('cost', 0) == 0:
                            set_valid_action(436, f"LOYALTY_ABILITY_ZERO for {card.name}{warning}")
                        elif ability.get('cost', 0) < 0:
                            # Check if walker has enough loyalty for minus ability
                            if current_loyalty >= abs(ability.get('cost', 0)):
                                if ability.get('is_ultimate', False):
                                    set_valid_action(438, f"ULTIMATE_ABILITY for {card.name}{warning}")
                                else:
                                    set_valid_action(437, f"LOYALTY_ABILITY_MINUS for {card.name}{warning}")

    def _add_equipment_aura_actions(self, player, valid_actions, set_valid_action):
        """Add actions for equipment and aura manipulation with improved cost handling."""
        gs = self.game_state
        
        # Check for equipment that can be equipped
        for idx, card_id in enumerate(player["battlefield"][:10]):  # Limit to first 10 equipment
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'card_types') and 'equipment' in card.card_types:
                # Check if already equipped
                is_equipped = False
                if hasattr(gs, 'equipped_to'):
                    is_equipped = card_id in gs.equipped_to
                
                # Get equip cost
                equip_cost = ""
                if hasattr(card, 'oracle_text'):
                    import re
                    match = re.search(r"equip \{?([^}]+)\}?", card.oracle_text.lower())
                    if match:
                        equip_cost = match.group(1)
                        # Ensure it's in the right format for mana_system
                        if not equip_cost.startswith('{'):
                            equip_cost = '{' + equip_cost + '}'
                    else:
                        # Generic cost match
                        match = re.search(r"equip (\d+)", card.oracle_text.lower())
                        if match:
                            equip_cost = '{' + match.group(1) + '}'
                
                # Check if we can afford equip cost
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, equip_cost)
                else:
                    # Simple check - at least some mana available
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                # Check if there are creatures to equip
                valid_targets = []
                for creature_idx, creature_id in enumerate(player["battlefield"]):
                    creature = gs._safe_get_card(creature_id)
                    if creature and hasattr(creature, 'card_types') and 'creature' in creature.card_types:
                        valid_targets.append((creature_idx, creature_id))
                
                has_creatures = len(valid_targets) > 0
                
                if has_creatures and can_afford:
                    if is_equipped:
                        # Already equipped, allow unequipping
                        set_valid_action(446, f"UNEQUIP {card.name}")
                        # Allow moving to another creature
                        for creature_idx, creature_id in valid_targets:
                            creature = gs._safe_get_card(creature_id)
                            equip_param = (idx, creature_idx)
                            set_valid_action(445, f"EQUIP {card.name} to {creature.name} (cost: {equip_cost})")
                            break  # Just add the first one for now
                    else:
                        # Not equipped yet
                        for creature_idx, creature_id in valid_targets:
                            creature = gs._safe_get_card(creature_id)
                            equip_param = (idx, creature_idx)
                            set_valid_action(445, f"EQUIP {card.name} to {creature.name} (cost: {equip_cost})")
                            break  # Just add the first one for now
        
        # Check for reconfigurable equipment
        for idx, card_id in enumerate(player["battlefield"][:10]):  # Limit to first 10
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "reconfigure" in card.oracle_text.lower():
                # Get reconfigure cost
                reconfigure_cost = ""
                import re
                match = re.search(r"reconfigure \{?([^}]+)\}?", card.oracle_text.lower())
                if match:
                    reconfigure_cost = match.group(1)
                    # Ensure it's in the right format for mana_system
                    if not reconfigure_cost.startswith('{'):
                        reconfigure_cost = '{' + reconfigure_cost + '}'
                
                # Check if we can afford reconfigure cost
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, reconfigure_cost)
                else:
                    # Simple check - at least some mana available
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                if can_afford:
                    set_valid_action(449, f"RECONFIGURE {card.name} (cost: {reconfigure_cost})")
        
        # Check for auras that can be moved
        for idx, card_id in enumerate(player["battlefield"][:10]):  # Limit to first 10 auras
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'card_types') and 'aura' in card.card_types:
                # Check if it has an ability that allows moving
                if hasattr(card, 'oracle_text') and "attach" in card.oracle_text.lower():
                    # Look for attach cost
                    attach_cost = ""
                    import re
                    match = re.search(r"(\{[^}]+\}): attach", card.oracle_text.lower())
                    if match:
                        attach_cost = match.group(1)
                    
                    # Check if we can afford attach cost
                    can_afford = False
                    if hasattr(gs, 'mana_system'):
                        can_afford = gs.mana_system.can_pay_mana_cost(player, attach_cost)
                    else:
                        # Simple check - at least some mana available
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
                        set_valid_action(447, f"ATTACH_AURA {card.name} (cost: {attach_cost})")


    def _add_ninjutsu_actions(self, player, valid_actions, set_valid_action):
        """Add actions for using Ninjutsu."""
        gs = self.game_state
        
        # Ninjutsu can only be used during combat after blockers are declared
        if gs.phase not in [gs.PHASE_DECLARE_BLOCKERS, gs.PHASE_COMBAT_DAMAGE]:
            return
            
        # Check if there are unblocked attackers
        unblocked_attackers = []
        for attacker_id in gs.current_attackers:
            # Check if this attacker is blocked
            is_blocked = False
            for blocker_list in gs.current_block_assignments.values():
                if attacker_id in blocker_list:
                    is_blocked = True
                    break
                    
            # If not blocked and controlled by this player, add to list
            if not is_blocked and attacker_id in player["battlefield"]:
                unblocked_attackers.append(attacker_id)
        
        # If we have unblocked attackers, check for ninjas in hand
        if unblocked_attackers:
            for idx, card_id in enumerate(player["hand"]):
                card = gs._safe_get_card(card_id)
                
                # Check if card has ninjutsu ability
                if card and hasattr(card, 'oracle_text') and "ninjutsu" in card.oracle_text.lower():
                    # Get ninjutsu cost
                    import re
                    match = re.search(r"ninjutsu (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                    ninjutsu_cost = match.group(1) if match else ""
                    
                    # Check if we can afford the ninjutsu cost
                    can_afford = False
                    if hasattr(gs, 'mana_system') and ninjutsu_cost:
                        can_afford = gs.mana_system.can_pay_mana_cost(player, ninjutsu_cost)
                    else:
                        can_afford = sum(player["mana_pool"].values()) > 0
                    
                    if can_afford:
                        # Enable ninjutsu action for this ninja
                        set_valid_action(432, f"NINJUTSU with {card.name}")
        
    def _add_multiple_blocker_actions(self, player, valid_actions, set_valid_action):
        """Add actions for assigning multiple blockers."""
        gs = self.game_state
        
        # Only applicable in declare blockers phase
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS:
            return
        
        # Check if we have attackers and multiple blockers
        if gs.current_attackers and len(player["battlefield"]) > 1:
            # Get creatures that could block
            potential_blockers = [
                cid for cid in player["battlefield"]
                if gs._safe_get_card(cid) and 
                hasattr(gs._safe_get_card(cid), 'card_types') and
                'creature' in gs._safe_get_card(cid).card_types and
                cid not in player.get("tapped_permanents", set())
            ]
            
            if len(potential_blockers) > 1:
                # Enable the multi-block option for the first attacker
                for atk_idx, attacker_id in enumerate(gs.current_attackers[:5]):  # Limit to 5 attackers
                    set_valid_action(383 + atk_idx, f"ASSIGN_MULTIPLE_BLOCKERS to {gs._safe_get_card(attacker_id).name if gs._safe_get_card(attacker_id) else 'Unknown'}")        
        
    def _has_first_strike(self, card):
        """Check if a card has first strike."""
        if not card:
            return False
        
        if hasattr(card, 'oracle_text') and "first strike" in card.oracle_text.lower():
            return True
        
        if hasattr(card, 'keywords') and len(card.keywords) > 5:
            return card.keywords[5] == 1  # Index 5 corresponds to first strike
        
        return False
            
    def setup_combat_systems(self):
        """
        Set up combat systems for the game if not already present.
        Ensures that all combat-related components are properly initialized and connected.
        """
        gs = self.game_state
        
        # Initialize the combat resolver if needed
        if not hasattr(gs, 'combat_resolver'):
            from .enhanced_combat import ExtendedCombatResolver
            gs.combat_resolver = ExtendedCombatResolver(gs)
        
        # Use the integration function to ensure the action handler is properly set up
        # and connected to the resolver
        if not hasattr(gs, 'combat_action_handler'):
            from .combat_integration import integrate_combat_actions
            integrate_combat_actions(gs)
        
        # Initialize combat-related data structures
        if not hasattr(gs, 'current_attackers'):
            gs.current_attackers = []
        if not hasattr(gs, 'current_block_assignments'):
            gs.current_block_assignments = {}
        if not hasattr(gs, 'planeswalker_attack_targets'):
            gs.planeswalker_attack_targets = {}
        if not hasattr(gs, 'planeswalker_protectors'):
            gs.planeswalker_protectors = {}
        if not hasattr(gs, 'combat_damage_dealt'):
            gs.combat_damage_dealt = False         

    def evaluate_attack_configuration(self, attackers):
        """
        Evaluate the expected value of a particular attack configuration with improved MTG-specific evaluation.
        Returns an estimated reward value.
        """
        gs = self.game_state
        
        # Use EnhancedCombatResolver.simulate_combat if available
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, 'simulate_combat'):
            # Save current attackers
            original_attackers = gs.current_attackers.copy()
            
            # Set attackers for simulation
            gs.current_attackers = attackers
            
            # Simulate combat
            simulation_results = gs.combat_resolver.simulate_combat()
            
            # Restore original attackers
            gs.current_attackers = original_attackers
            
            # Use simulation results for evaluation
            if isinstance(simulation_results, dict) and "expected_value" in simulation_results:
                return simulation_results["expected_value"]
        
        # Fallback to original evaluation logic
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1

        # Gather detailed game state information
        if not attackers:
            # More significant penalty for not attacking when you have attackers
            potential_attackers = [cid for cid in me["battlefield"] 
                                if gs._safe_get_card(cid) and 'creature' in gs._safe_get_card(cid).card_types 
                                and cid not in me["entered_battlefield_this_turn"]]
            if potential_attackers:
                return -0.5  # Increased penalty for having potential attackers but not using them
            return -0.2  # Increased penalty if there are no valid attackers
        
        # Get defender's potential blockers
        potential_blockers = [cid for cid in opp["battlefield"] 
                            if gs._safe_get_card(cid) and 'creature' in gs._safe_get_card(cid).card_types]
        
        # Calculate total power and relevant abilities of attackers
        total_attacking_power = 0
        evasive_power = 0  # Power that might be unblockable
        has_trample = False
        has_deathtouch = False
        has_lifelink = False
        has_first_strike = False
        has_double_strike = False
        
        for attacker_id in attackers:
            card = gs._safe_get_card(attacker_id)
            if not card:
                continue
                
            attacker_power = card.power
            total_attacking_power += attacker_power
            
            # Check for evasive abilities
            if "flying" in card.oracle_text.lower() and not any("flying" in gs._safe_get_card(bid).oracle_text.lower() or "reach" in gs._safe_get_card(bid).oracle_text.lower() for bid in potential_blockers if gs._safe_get_card(bid)):
                evasive_power += attacker_power
                
            # Check for combat abilities
            if "trample" in card.oracle_text.lower():
                has_trample = True
            if "deathtouch" in card.oracle_text.lower():
                has_deathtouch = True
            if "lifelink" in card.oracle_text.lower():
                has_lifelink = True
            if "first strike" in card.oracle_text.lower():
                has_first_strike = True
            if "double strike" in card.oracle_text.lower():
                has_double_strike = True
        
        # Estimate how many attackers will be blocked
        max_blockers = len(potential_blockers)
        blocked_attackers = min(len(attackers), max_blockers)
        unblocked_attackers = len(attackers) - blocked_attackers
        
        # Evaluate attackers that might force unfavorable blocks
        threatening_attackers = sum(1 for aid in attackers if any(gs._safe_get_card(aid).power >= gs._safe_get_card(bid).toughness for bid in potential_blockers if gs._safe_get_card(bid)))
        
        # Calculate estimated damage output with more sophisticated calculation
        # Direct damage from unblocked attackers
        direct_damage = evasive_power  # Start with evasive creatures
        if unblocked_attackers > 0:
            non_evasive_unblocked = unblocked_attackers - (1 if evasive_power > 0 else 0)
            for i, aid in enumerate(attackers):
                if i >= blocked_attackers and "flying" not in gs._safe_get_card(aid).oracle_text.lower():
                    card = gs._safe_get_card(aid)
                    if card and non_evasive_unblocked > 0:
                        direct_damage += card.power
                        non_evasive_unblocked -= 1
        
        # Add trample damage - more realistic calculation
        trample_damage = 0
        if has_trample and blocked_attackers > 0:
            for aid in attackers:
                attacker = gs._safe_get_card(aid)
                if not attacker or "trample" not in attacker.oracle_text.lower():
                    continue
                    
                # Find best blocker assignment for this trampler
                best_block_toughness = 0
                for bid in potential_blockers:
                    blocker = gs._safe_get_card(bid)
                    if blocker:
                        best_block_toughness = max(best_block_toughness, blocker.toughness)
                
                # Calculate trample damage
                if attacker.power > best_block_toughness:
                    trample_damage += (attacker.power - best_block_toughness)
        
        # Calculate potential creature losses
        potential_attacker_losses = 0
        potential_blocker_losses = 0
        
        # More MTG-like combat simulation - match up attackers and blockers
        remaining_blockers = list(potential_blockers)
        for aid in attackers:
            attacker = gs._safe_get_card(aid)
            if not attacker:
                continue
                
            # Find best blocker for this attacker
            best_blocker = None
            best_blocker_survival_chance = -1
            
            for bid in remaining_blockers[:]:
                blocker = gs._safe_get_card(bid)
                if not blocker:
                    continue
                    
                # Will blocker survive?
                # Account for first strike/double strike
                blocker_survives = True
                if has_first_strike or has_double_strike:
                    if attacker.power >= blocker.toughness:
                        blocker_survives = False
                else:
                    blocker_survives = blocker.toughness > attacker.power
                    
                # Will attacker survive?
                attacker_survives = attacker.toughness > blocker.power
                
                # Can blocker kill attacker?
                can_kill_attacker = blocker.power >= attacker.toughness
                
                # Score this blocking assignment
                block_score = 0
                if can_kill_attacker:
                    block_score += 2  # Good to kill attacker
                if blocker_survives:
                    block_score += 1  # Good if blocker survives
                    
                if block_score > best_blocker_survival_chance:
                    best_blocker_survival_chance = block_score
                    best_blocker = bid
            
            if best_blocker:
                # Simulate this block
                blocker = gs._safe_get_card(best_blocker)
                
                if attacker.power >= blocker.toughness:
                    potential_blocker_losses += 1
                    remaining_blockers.remove(best_blocker)
                    
                if blocker.power >= attacker.toughness:
                    potential_attacker_losses += 1
        
        # Convert to estimated reward
        estimated_damage = direct_damage + trample_damage
        
        # Lifelink bonus - increased
        lifelink_bonus = 0
        if has_lifelink:
            lifelink_bonus = min(estimated_damage * 0.1, 0.3)  # Doubled bonus for lifelink
        
        # Deathtouch bonus - increased
        deathtouch_bonus = 0
        if has_deathtouch and blocked_attackers > 0:
            deathtouch_bonus = min(blocked_attackers * 0.15, 0.45)  # 50% more bonus for deathtouch blocks
        
        # Calculate creature exchange value - weighted by creature quality
        creature_value_diff = 0
        for aid in attackers:
            attacker = gs._safe_get_card(aid)
            if attacker:
                # If this attacker will die
                if any(gs._safe_get_card(bid).power >= attacker.toughness for bid in potential_blockers if gs._safe_get_card(bid)):
                    creature_value_diff -= (attacker.power + attacker.toughness) * 0.08  # Reduced penalty for losing attackers
        
        for bid in potential_blockers:
            blocker = gs._safe_get_card(bid)
            if blocker:
                # If this blocker will die
                if any(gs._safe_get_card(aid).power >= blocker.toughness for aid in attackers if gs._safe_get_card(aid)):
                    creature_value_diff += (blocker.power + blocker.toughness) * 0.08  # Higher value for killing blockers
        
        # Damage rewards - increased significantly
        damage_reward = min(estimated_damage * 0.2, 1.0)  # Increased damage value
        
        # Opponent life check - extra rewards when opponent is getting low
        # Progressive scaling based on opponent's current life
        if estimated_damage >= opp["life"]:
            lethal_bonus = 3.0  # Increased bonus for potential lethal
        elif opp["life"] - estimated_damage <= 3:
            lethal_bonus = 1.0  # Increased bonus for getting opponent very low
        elif opp["life"] - estimated_damage <= 5:
            lethal_bonus = 0.75  # Bonus for getting opponent to low life
        elif opp["life"] - estimated_damage <= 10:
            lethal_bonus = 0.5  # New bonus for significant life reduction
        else:
            lethal_bonus = 0
        
        total_reward = damage_reward + creature_value_diff + lethal_bonus + lifelink_bonus + deathtouch_bonus
        
        # Extra reward for attacking when opponent is at low life - increased
        if opp["life"] <= 5:
            total_reward *= 2.0  # Doubled urgency factor
        elif opp["life"] <= 10:
            total_reward *= 1.75  # New intermediate urgency factor
        
        # Scale reward by game stage - encourage earlier attacks
        if gs.turn <= 5:
            total_reward *= 1.2  # Increase reward for early aggression
        
        logging.debug(f"Attack config evaluation: {len(attackers)} attackers, est. damage: {estimated_damage:.1f}, exchange: {creature_value_diff:.2f}, lethal_check: {lethal_bonus:.1f}, reward: {total_reward:.2f}")
        
        return total_reward
        
    def find_optimal_attack(self):
        """
        Find the optimal combination of attackers using strategic evaluation.
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Make sure the combat resolver is initialized
        if not hasattr(gs, 'combat_resolver'):
            from .enhanced_combat import ExtendedCombatResolver
            gs.combat_resolver = ExtendedCombatResolver(gs)
        
        # Get valid attackers
        potential_attackers = []
        for card_id in me["battlefield"]:
            if self.is_valid_attacker(card_id):
                potential_attackers.append(card_id)
        
        if not potential_attackers:
            return []
        
        # Use the combat resolver's find_optimal_attack method if available
        if hasattr(gs.combat_resolver, 'find_optimal_attack'):
            return gs.combat_resolver.find_optimal_attack(potential_attackers)
                
        # For reasonable computation, limit to max 8 attackers
        if len(potential_attackers) > 8:
            # Sort by power to prioritize stronger creatures
            potential_attackers.sort(
                key=lambda cid: gs._safe_get_card(cid).power if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power') else 0,
                reverse=True
            )
            potential_attackers = potential_attackers[:8]
        
        # Generate all possible attacker combinations
        import itertools
        all_combinations = []
        
        # Include empty set for "no attack" option
        all_combinations.append([])
        
        # Add combinations of attackers, but limit complexity
        max_combination_size = min(len(potential_attackers), 5)
        for size in range(1, max_combination_size + 1):
            for combo in itertools.combinations(potential_attackers, size):
                all_combinations.append(list(combo))
        
        # Evaluate each combination using strategic planner
        best_combo = []
        best_value = -float('inf')
        
        for combo in all_combinations:
            # Use strategic planner's evaluate_attack_action
            value = gs.strategic_planner.evaluate_attack_action(combo)
            
            # Adjust for game state
            opp = gs.p2 if gs.agent_is_p1 else gs.p1  # Define the opponent's game state
            if opp["life"] <= 10 and combo:
                # Prioritize attacking when opponent is low
                value *= 1.5
            
            if value > best_value:
                best_value = value
                best_combo = combo
        
        if best_combo:
            combo_power = sum(gs._safe_get_card(cid).power for cid in best_combo if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
            combo_names = [gs._safe_get_card(cid).name for cid in best_combo if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'name')]
            logging.debug(f"Strategic optimal attack: {len(best_combo)} attackers with value {best_value:.2f}, total power {combo_power}")
            logging.debug(f"Attacking with: {', '.join(combo_names)}")
        
        return best_combo
        
    def is_valid_attacker(self, card_id):
        """Determine if a creature can attack based on its properties and current game state."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        current_player = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Check if card is valid
        if not card or not hasattr(card, 'card_types'):
            return False
            
        # Check if it's a creature
        if 'creature' not in card.card_types:
            return False
            
        # Check if it's tapped
        if card_id in current_player["tapped_permanents"]:
            return False
            
        # Check for summoning sickness (entered this turn and doesn't have haste)
        has_haste = "haste" in card.oracle_text.lower() if hasattr(card, 'oracle_text') else False
        if card_id in current_player["entered_battlefield_this_turn"] and not has_haste:
            return False
            
        # Check for defender
        has_defender = "defender" in card.oracle_text.lower() if hasattr(card, 'oracle_text') else False
        if has_defender:
            return False
            
        # Check if abilities prevent attacking
        if gs.ability_handler:
            # Use the ability system to check for restrictions
            context = {"type": "ATTACKS"}
            if not gs.ability_handler._apply_defender(card_id, "ATTACKS", context):
                return False
                
        return True
    
    def _initialize_combat_state_tracking(self):
        """Initialize or reset tracking dictionaries for combat state."""
        gs = self.game_state
        
        # Ensure these attributes exist without overwriting existing data
        if not hasattr(gs, "current_attackers"):
            gs.current_attackers = []
        if not hasattr(gs, "current_block_assignments"):
            gs.current_block_assignments = {}
        if not hasattr(gs, "planeswalker_attack_targets"):
            gs.planeswalker_protectors = {}
        if not hasattr(gs, "first_strike_ordering"):
            gs.first_strike_ordering = {}
        if not hasattr(gs, "combat_damage_dealt"):
            gs.combat_damage_dealt = False
            
        logging.debug("Combat state tracking initialized")
    
    def handle_first_strike_order(self):
        """
        Set the damage assignment order for first strike combat with enhanced threat assessment.
        
        Returns:
            bool: True if order was set successfully
        """
        gs = self.game_state
        
        # For each attacker that's blocked by multiple creatures
        for attacker_id, blockers in gs.current_block_assignments.items():
            if len(blockers) <= 1:
                continue  # No ordering needed for single or no blockers
                
            attacker_card = gs._safe_get_card(attacker_id)
            if not attacker_card:
                continue
                
            # Check if attacker has first strike
            has_first_strike = False
            if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_has_keyword'):
                has_first_strike = (gs.combat_resolver._has_keyword(attacker_card, "first strike") or 
                                gs.combat_resolver._has_keyword(attacker_card, "double strike"))
            elif hasattr(attacker_card, 'oracle_text'):
                has_first_strike = ('first strike' in attacker_card.oracle_text.lower() or 
                                'double strike' in attacker_card.oracle_text.lower())
                
            if not has_first_strike:
                continue  # Skip attackers without first strike
                
            # Determine optimal ordering - enhanced threat assessment
            def blocker_threat(blocker_id):
                blocker = gs._safe_get_card(blocker_id)
                if not blocker:
                    return 0
                
                # Base threat calculation
                threat = 0
                
                # Power as primary threat metric with scaling based on power level
                blocker_power = getattr(blocker, 'power', 0)
                threat += blocker_power * (1.5 + blocker_power * 0.2)  # Higher power is exponentially more threatening
                
                # Toughness as defensive capability (inversely weighted)
                blocker_toughness = getattr(blocker, 'toughness', 0)
                threat -= min(blocker_toughness * 0.5, 3)  # Cap the penalty so high toughness isn't overly beneficial
                
                # Convert CMC to threat if available (higher CMC = likely more threatening)
                if hasattr(blocker, 'cmc'):
                    threat += min(blocker.cmc * 0.5, 3)  # Higher mana value generally means more powerful
                
                # Strategic ability assessment
                if hasattr(blocker, 'oracle_text'):
                    oracle_text = blocker.oracle_text.lower()
                    
                    # High-value combat abilities
                    if "first strike" in oracle_text or "double strike" in oracle_text:
                        threat += 3.5  # Extremely high priority for first strike/double strike
                    if "deathtouch" in oracle_text:
                        threat += 4.5  # Highest priority - kill these first
                    if "lifelink" in oracle_text:
                        threat += 2.5  # High strategic value
                    if "indestructible" in oracle_text:
                        threat -= 2  # Lower priority since it won't die from combat
                    if "protection" in oracle_text:
                        threat += 2  # Potential to be immune to attacker
                    
                    # Activated abilities
                    if any(activate_word in oracle_text for activate_word in 
                        ["{t}:", "{w}:", "{u}:", "{b}:", "{r}:", "{g}:", "activate"]):
                        threat += 2  # Has activated abilities
                    
                    # Death triggers and recursion
                    if "when" in oracle_text and "dies" in oracle_text:
                        threat += 2.5  # Has death trigger
                    if any(recursion_word in oracle_text for recursion_word in 
                        ["return", "from graveyard", "from your graveyard"]):
                        threat += 1.5  # Can recur
                    
                    # Removal/disruptive abilities
                    if any(removal_word in oracle_text for removal_word in 
                        ["destroy", "exile", "sacrifice", "remove", "damage", "-1/-1"]):
                        threat += 3  # Can remove creatures
                    
                    # Card advantage
                    if any(advantage_word in oracle_text for advantage_word in 
                        ["draw", "search", "reveal", "look at", "scry"]):
                        threat += 2  # Provides card advantage
                        
                    # Counters
                    if "counter" in oracle_text and "target spell" in oracle_text:
                        threat += 3  # Can counter spells
                
                # Check for keyword abilities if present in a structured format
                if hasattr(blocker, 'keywords') and isinstance(blocker.keywords, list):
                    keyword_values = {
                        'flying': 1.5,
                        'trample': 2,
                        'hexproof': 2.5,
                        'lifelink': 2.5,
                        'deathtouch': 4.5,
                        'first strike': 3.5,
                        'double strike': 4,
                        'vigilance': 1,
                        'flash': 1.5,
                        'haste': 2,
                        'menace': 1.5,
                        'reach': 1,
                        'indestructible': 3
                    }
                    
                    for keyword, value in keyword_values.items():
                        keyword_index = list(keyword_values.keys()).index(keyword)
                        if len(blocker.keywords) > keyword_index and blocker.keywords[keyword_index] == 1:
                            threat += value
                
                # Context-specific threat calculation
                # For first strike ordering, creatures that can kill our attacker are higher threats
                attacker_toughness = getattr(attacker_card, 'toughness', 0)
                if blocker_power >= attacker_toughness:
                    threat += 5  # Can kill our attacker
                
                # Creatures with low toughness that our attacker can kill easily are lower threats
                attacker_power = getattr(attacker_card, 'power', 0)
                if attacker_power >= blocker_toughness:
                    threat -= 1  # Easy to kill
                
                return threat
                
            # Sort blockers by threat level descending
            ordered_blockers = sorted(blockers, key=blocker_threat, reverse=True)
            
            # Store the ordering
            gs.first_strike_ordering[attacker_id] = ordered_blockers
            
            blocker_names = [gs._safe_get_card(bid).name if gs._safe_get_card(bid) else "Unknown" for bid in ordered_blockers]
            logging.debug(f"First strike ordering set for {attacker_card.name}: {', '.join(blocker_names)}")
            
        return True
    
    def handle_assign_combat_damage(self, damage_assignments=None):
        """
        Handle manual assignment of combat damage with improved handling for deathtouch, trample, etc.
        
        Args:
            damage_assignments: Optional dict mapping attacker IDs to dicts of 
                                {blocker_id: damage} assignments
                                
        Returns:
            bool: True if damage was assigned successfully
        """
        gs = self.game_state
        
        # If no specific assignments, use the combat resolver to auto-assign damage
        if not damage_assignments and hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, 'resolve_combat'):
            logging.debug("Auto-assigning combat damage")
            gs.combat_resolver.resolve_combat()
            gs.combat_damage_dealt = True
            return True
            
        # Manual assignment (for advanced cases like deathtouch optimization)
        if damage_assignments:
            logging.debug("Manually assigning combat damage")
            
            # Get attacker player
            attacker_player = gs.p1 if gs.agent_is_p1 else gs.p2
            defender_player = gs.p2 if gs.agent_is_p1 else gs.p1
            
            # Process each attacker's damage assignments
            for attacker_id, assignments in damage_assignments.items():
                attacker_card = gs._safe_get_card(attacker_id)
                if not attacker_card:
                    logging.warning(f"Invalid attacker card ID: {attacker_id}")
                    continue
                    
                # Check if the attacker is valid
                if attacker_id not in gs.current_attackers:
                    logging.warning(f"Invalid attacker {attacker_id} in damage assignments")
                    continue
                    
                # Check for deathtouch on attacker
                has_deathtouch = False
                if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_has_keyword'):
                    has_deathtouch = gs.combat_resolver._has_keyword(attacker_card, "deathtouch")
                elif hasattr(attacker_card, 'oracle_text'):
                    has_deathtouch = 'deathtouch' in attacker_card.oracle_text.lower()
                
                # Check for trample on attacker
                has_trample = False
                if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_has_keyword'):
                    has_trample = gs.combat_resolver._has_keyword(attacker_card, "trample")
                elif hasattr(attacker_card, 'oracle_text'):
                    has_trample = 'trample' in attacker_card.oracle_text.lower()
                
                # Track total damage assigned
                total_damage = sum(assignments.values())
                attacker_power = getattr(attacker_card, 'power', 0)
                
                # Special handling for deathtouch - needs only 1 damage per blocker
                blockers = gs.current_block_assignments.get(attacker_id, [])
                if has_deathtouch and blockers:
                    # Calculate minimum damage needed with deathtouch
                    min_damage_needed = len(blockers)
                    
                    # If trample and valid player assignment, ensure we calculate correctly
                    if has_trample and 'player' in assignments:
                        min_damage_needed = len([bid for bid in blockers if bid in assignments])
                        
                    # If even deathtouch can't account for all damage assignment...
                    if min_damage_needed < total_damage and total_damage > attacker_power:
                        # Scale down excess damage proportionally
                        excess_factor = (attacker_power - min_damage_needed) / (total_damage - min_damage_needed)
                        
                        # Ensure each blocker gets at least 1 damage for deathtouch
                        # and scale the rest proportionally
                        for target_id in assignments:
                            if target_id != 'player' and target_id in blockers:
                                assignments[target_id] = 1  # Minimum for deathtouch
                        
                        # Recalculate remaining damage and distribute
                        assigned_minimum = sum(1 for tid in assignments if tid != 'player' and tid in blockers)
                        remaining_damage = attacker_power - assigned_minimum
                        remaining_targets = {tid: assignments[tid] for tid in assignments 
                                        if tid == 'player' or assignments[tid] > 1}
                        
                        if remaining_targets and remaining_damage > 0:
                            remaining_total = sum(remaining_targets.values())
                            for target_id in remaining_targets:
                                proportion = remaining_targets[target_id] / remaining_total
                                assignments[target_id] = (1 if target_id != 'player' and target_id in blockers else 0) + \
                                                    max(1, int(remaining_damage * proportion))
                
                # Standard damage assignment scaling if no deathtouch or exceeds power
                elif total_damage > attacker_power:
                    logging.warning(f"Total damage {total_damage} exceeds attacker power {attacker_power}")
                    # Scale down damage proportionally
                    scale_factor = attacker_power / total_damage
                    for target_id in assignments:
                        assignments[target_id] = max(1, int(assignments[target_id] * scale_factor))
                
                # Apply damage to blockers
                for blocker_id, damage in assignments.items():
                    # Special case for damage to player (trample)
                    if blocker_id == 'player':
                        defender_player["life"] -= damage
                        logging.debug(f"{attacker_card.name} deals {damage} damage to player (trample)")
                        continue
                        
                    # Ensure blocker is valid
                    if blocker_id not in blockers:
                        logging.warning(f"Invalid blocker {blocker_id} for attacker {attacker_id}")
                        continue
                        
                    blocker_card = gs._safe_get_card(blocker_id)
                    if not blocker_card:
                        continue
                        
                    # Apply damage
                    if not hasattr(defender_player, "damage_counters"):
                        defender_player["damage_counters"] = {}
                        
                    current_damage = defender_player["damage_counters"].get(blocker_id, 0)
                    defender_player["damage_counters"][blocker_id] = current_damage + damage
                    
                    # Check for lethal damage - considering both regular damage and deathtouch
                    is_lethal = False
                    blocker_toughness = getattr(blocker_card, 'toughness', 0)
                    
                    if has_deathtouch and damage > 0:
                        is_lethal = True
                        logging.debug(f"{attacker_card.name} dealt lethal damage to {blocker_card.name} via deathtouch")
                    elif current_damage + damage >= blocker_toughness:
                        is_lethal = True
                        logging.debug(f"{attacker_card.name} dealt lethal damage to {blocker_card.name}")
                    
                    # Apply lethal damage effects
                    if is_lethal:
                        # Check for indestructible
                        is_indestructible = False
                        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_has_keyword'):
                            is_indestructible = gs.combat_resolver._has_keyword(blocker_card, "indestructible")
                        elif hasattr(blocker_card, 'oracle_text'):
                            is_indestructible = 'indestructible' in blocker_card.oracle_text.lower()
                        
                        if not is_indestructible:
                            # Use get_card_controller utility if available, otherwise fallback
                            blocker_controller = None
                            if 'get_card_controller' in globals():
                                blocker_controller = gs.get_card_controller(gs, blocker_id)
                            else:
                                # Fallback to direct check
                                for player in [gs.p1, gs.p2]:
                                    if blocker_id in player["battlefield"]:
                                        blocker_controller = player
                                        break
                                    
                            if blocker_controller:
                                gs.move_card(blocker_id, blocker_controller, "battlefield", blocker_controller, "graveyard")
                                
                                # Trigger death abilities
                                if hasattr(gs, 'trigger_ability'):
                                    gs.trigger_ability(blocker_id, "DIES", {"from_combat": True})
            
            # Process unblocked attackers
            for attacker_id in gs.current_attackers:
                # Skip attackers already processed
                if attacker_id in damage_assignments:
                    continue
                    
                # Skip blocked attackers
                if attacker_id in gs.current_block_assignments and gs.current_block_assignments[attacker_id]:
                    continue
                    
                # Check if targeting a planeswalker
                if hasattr(gs, "planeswalker_attack_targets") and attacker_id in gs.planeswalker_attack_targets:
                    planeswalker_id = gs.planeswalker_attack_targets[attacker_id]
                    planeswalker_card = gs._safe_get_card(planeswalker_id)
                    
                    if not planeswalker_card:
                        continue
                        
                    # Apply damage to planeswalker
                    attacker_card = gs._safe_get_card(attacker_id)
                    damage = getattr(attacker_card, 'power', 0)
                    
                    # Reduce loyalty
                    if hasattr(planeswalker_card, 'loyalty'):
                        planeswalker_card.loyalty -= damage
                        logging.debug(f"{attacker_card.name} deals {damage} damage to planeswalker {planeswalker_card.name}")
                        
                        # Check if planeswalker died
                        if planeswalker_card.loyalty <= 0:
                            # Use get_card_controller utility if available, otherwise fallback
                            planeswalker_controller = None
                            if 'get_card_controller' in globals():
                                planeswalker_controller = gs.get_card_controller(gs, planeswalker_id)
                            else:
                                # Fallback to direct check
                                for player in [gs.p1, gs.p2]:
                                    if planeswalker_id in player["battlefield"]:
                                        planeswalker_controller = player
                                        break
                                    
                            if planeswalker_controller:
                                gs.move_card(planeswalker_id, planeswalker_controller, "battlefield", planeswalker_controller, "graveyard")
                                logging.debug(f"Planeswalker {planeswalker_card.name} died from combat damage")
                                
                                # Trigger death abilities
                                if hasattr(gs, 'trigger_ability'):
                                    gs.trigger_ability(planeswalker_id, "DIES", {"from_combat": True})
                else:
                    # Deal damage to the opponent
                    attacker_card = gs._safe_get_card(attacker_id)
                    damage = getattr(attacker_card, 'power', 0)
                    
                    has_lifelink = False
                    if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_has_keyword'):
                        has_lifelink = gs.combat_resolver._has_keyword(attacker_card, "lifelink")
                    elif hasattr(attacker_card, 'oracle_text'):
                        has_lifelink = 'lifelink' in attacker_card.oracle_text.lower()
                    
                    # Apply damage to opponent
                    defender_player["life"] -= damage
                    logging.debug(f"{attacker_card.name} deals {damage} unblocked damage to player")
                    
                    # Apply lifelink if applicable
                    if has_lifelink:
                        attacker_controller = gs.p1 if gs.agent_is_p1 else gs.p2
                        attacker_controller["life"] += damage
                        logging.debug(f"Lifelink from {attacker_card.name} gained {damage} life")
            
            # Mark combat damage as dealt
            gs.combat_damage_dealt = True
            return True
            
        return False
    
    def handle_attack_battle(self, battle_idx):
        """
        Handle attack action against a battle card.
        
        Args:
            battle_idx: Index of the battle in opponent's battlefield
            
        Returns:
            bool: True if attack was successful
        """
        gs = self.game_state
        
        # Get active player and opponent
        active_player = gs.p1 if gs.agent_is_p1 else gs.p2
        opponent = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Get the creature index from the stored mapping
        creature_idx = None
        if hasattr(gs, '_battle_attack_creatures') and battle_idx in gs._battle_attack_creatures:
            creature_idx = gs._battle_attack_creatures[battle_idx]
        
        # If no creature was stored, return failure
        if creature_idx is None:
            logging.warning(f"No attacking creature selected for battle index {battle_idx}")
            return False
        
        # Find the battle card
        battle_cards = []
        for idx, card_id in enumerate(opponent["battlefield"]):
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_battle') and card.is_battle:
                battle_cards.append((idx, card_id))
                
        if battle_idx >= len(battle_cards):
            logging.warning(f"Invalid battle index: {battle_idx}")
            return False
            
        _, battle_id = battle_cards[battle_idx]
        battle_card = gs._safe_get_card(battle_id)
        
        # Find the creature
        if creature_idx >= len(active_player["battlefield"]):
            logging.warning(f"Invalid creature index: {creature_idx}")
            return False
            
        creature_id = active_player["battlefield"][creature_idx]
        creature_card = gs._safe_get_card(creature_id)
        
        # Check if the creature can attack
        if creature_id in active_player["tapped_permanents"]:
            logging.warning(f"Creature {creature_card.name} is already tapped")
            return False
            
        has_haste = False
        if hasattr(creature_card, 'oracle_text'):
            has_haste = "haste" in creature_card.oracle_text.lower()
            
        if creature_id in active_player.get("entered_battlefield_this_turn", set()) and not has_haste:
            logging.warning(f"Creature {creature_card.name} has summoning sickness")
            return False
        
        # Process the attack
        # First, tap the attacking creature
        active_player["tapped_permanents"].add(creature_id)
        
        # Deal damage to the battle equal to the creature's power
        creature_power = getattr(creature_card, 'power', 0)
        
        # Initialize damage tracking on battle if needed
        if not hasattr(battle_card, 'damage'):
            battle_card.damage = 0
            
        battle_card.damage += creature_power
        
        # Check if battle is defeated
        battle_defeated = False
        if hasattr(battle_card, 'defense'):
            battle_defeated = battle_card.damage >= battle_card.defense
            
        if battle_defeated:
            # Move battle to graveyard
            opponent["battlefield"].remove(battle_id)
            opponent["graveyard"].append(battle_id)
            
            # Trigger appropriate abilities
            if hasattr(gs, 'trigger_ability'):
                gs.trigger_ability(battle_id, "DEFEATED", {"from_attack": True, "attacker_id": creature_id})
                gs.trigger_ability(creature_id, "DEFEATED_BATTLE", {"battle_id": battle_id})
                
            logging.debug(f"{creature_card.name} attacked and defeated {battle_card.name}")
        else:
            logging.debug(f"{creature_card.name} attacked {battle_card.name} for {creature_power} damage")
        
        return True

    def handle_ninjutsu(self, ninjutsu_card_id, attacker_id=None):
        """
        Handle the ninjutsu mechanic.
        
        Args:
            ninjutsu_card_id: ID of the ninjutsu card in hand
            attacker_id: ID of the unblocked attacker to return to hand
            
        Returns:
            bool: True if ninjutsu was executed successfully
        """
        gs = self.game_state
        
        # Get the active player
        active_player = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Verify the ninjutsu card
        ninja_card = gs._safe_get_card(ninjutsu_card_id)
        if not ninja_card or ninjutsu_card_id not in active_player["hand"]:
            logging.warning(f"Invalid ninjutsu card ID: {ninjutsu_card_id}")
            return False
            
        # Check if ninjutsu ability exists
        has_ninjutsu = False
        if hasattr(ninja_card, 'oracle_text'):
            has_ninjutsu = 'ninjutsu' in ninja_card.oracle_text.lower()
            
        if not has_ninjutsu:
            logging.warning(f"Card {ninja_card.name} doesn't have ninjutsu")
            return False
            
        # Find all unblocked attackers
        unblocked_attackers = [
            aid for aid in gs.current_attackers
            if aid not in gs.current_block_assignments or not gs.current_block_assignments[aid]
        ]
        
        # If attacker_id is provided, check if it's a valid unblocked attacker
        if attacker_id and attacker_id not in unblocked_attackers:
            logging.warning(f"Invalid unblocked attacker: {attacker_id}")
            return False
            
        # If no attacker_id provided, use the first unblocked attacker
        if not attacker_id and unblocked_attackers:
            attacker_id = unblocked_attackers[0]
            
        if not attacker_id:
            logging.warning("No unblocked attacker available for ninjutsu")
            return False
            
        # Get the attacker card
        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card:
            logging.warning(f"Invalid attacker card ID: {attacker_id}")
            return False
            
        # Execute ninjutsu
        # 1. Return unblocked attacker to hand
        gs.move_card(attacker_id, active_player, "battlefield", active_player, "hand")
        
        # 2. Put ninja onto battlefield tapped and attacking
        gs.move_card(ninjutsu_card_id, active_player, "hand", active_player, "battlefield")
        active_player["tapped_permanents"].add(ninjutsu_card_id)
        
        # 3. Add to current attackers, remove the original attacker
        if attacker_id in gs.current_attackers:
            gs.current_attackers.remove(attacker_id)
        gs.current_attackers.append(ninjutsu_card_id)
        
        # 4. If the attacker was targeting a planeswalker, transfer the target
        if hasattr(gs, "planeswalker_attack_targets") and attacker_id in gs.planeswalker_attack_targets:
            planeswalker_id = gs.planeswalker_attack_targets[attacker_id]
            del gs.planeswalker_attack_targets[attacker_id]
            gs.planeswalker_attack_targets[ninjutsu_card_id] = planeswalker_id
            
        logging.debug(f"Ninjutsu: Replaced {attacker_card.name} with {ninja_card.name}")
        return True
    
    def handle_declare_attackers_done(self):
        """
        Handle the end of the declare attackers phase.
        
        Returns:
            bool: True if phase transition was successful
        """
        gs = self.game_state
        
        # Check that we're in the correct phase
        if gs.phase != gs.PHASE_DECLARE_ATTACKERS:
            logging.warning(f"Cannot end declare attackers phase when in phase {gs.phase}")
            return False
            
        # Check that at least some valid attackers were declared
        # This is a good place to add validation if needed
        
        # Advance to declare blockers phase
        gs.phase = gs.PHASE_DECLARE_BLOCKERS
        logging.debug(f"Advancing to DECLARE_BLOCKERS phase with {len(gs.current_attackers)} attackers")
        
        return True
    
    def handle_declare_blockers_done(self):
        """
        Handle the end of the declare blockers phase.
        
        Returns:
            bool: True if phase transition was successful
        """
        gs = self.game_state
        
        # Check that we're in the correct phase
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS:
            logging.warning(f"Cannot end declare blockers phase when in phase {gs.phase}")
            return False
            
        # Check if any creatures have first strike
        has_first_strike = False
        
        # Check attackers for first strike
        for attacker_id in gs.current_attackers:
            attacker_card = gs._safe_get_card(attacker_id)
            if not attacker_card:
                continue
                
            if hasattr(attacker_card, 'oracle_text'):
                if 'first strike' in attacker_card.oracle_text.lower() or 'double strike' in attacker_card.oracle_text.lower():
                    has_first_strike = True
                    break
                    
        # Check blockers for first strike
        if not has_first_strike:
            for attacker_id, blockers in gs.current_block_assignments.items():
                if has_first_strike:
                    break
                    
                for blocker_id in blockers:
                    blocker_card = gs._safe_get_card(blocker_id)
                    if not blocker_card:
                        continue
                        
                    if hasattr(blocker_card, 'oracle_text'):
                        if 'first strike' in blocker_card.oracle_text.lower() or 'double strike' in blocker_card.oracle_text.lower():
                            has_first_strike = True
                            break
        
        # Advance to the appropriate phase
        if has_first_strike:
            gs.phase = gs.PHASE_FIRST_STRIKE_DAMAGE
            logging.debug("Advancing to FIRST_STRIKE_DAMAGE phase")
        else:
            gs.phase = gs.PHASE_COMBAT_DAMAGE
            logging.debug("Advancing to COMBAT_DAMAGE phase (no first strike)")
            
        return True
    
    def handle_attack_planeswalker(self, planeswalker_idx):
        """
        Handle an attack targeting a planeswalker.
        
        Args:
            planeswalker_idx: Index of the planeswalker in the opponent's battlefield
            
        Returns:
            bool: True if the attack was targeted successfully
        """
        gs = self.game_state
        
        # Check that we're in the correct phase
        if gs.phase != gs.PHASE_DECLARE_ATTACKERS:
            logging.warning(f"Cannot attack planeswalker when not in DECLARE_ATTACKERS phase ({gs.phase})")
            return False
            
        # Get opponent's battlefield
        opponent = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Get all planeswalkers
        planeswalkers = [
            card_id for card_id in opponent["battlefield"]
            if gs._safe_get_card(card_id) and 
            hasattr(gs._safe_get_card(card_id), 'card_types') and
            'planeswalker' in gs._safe_get_card(card_id).card_types
        ]
        
        # Check if planeswalker_idx is valid
        if planeswalker_idx < 0 or planeswalker_idx >= len(planeswalkers):
            logging.warning(f"Invalid planeswalker index: {planeswalker_idx}")
            return False
            
        # Get the planeswalker card
        planeswalker_id = planeswalkers[planeswalker_idx]
        planeswalker_card = gs._safe_get_card(planeswalker_id)
        
        # Check that there's an active attacker to direct
        if not gs.current_attackers:
            logging.warning("No active attacker to direct at planeswalker")
            return False
            
        # Get the most recent attacker
        attacker_id = gs.current_attackers[-1]
        attacker_card = gs._safe_get_card(attacker_id)
        
        # Set the attack target
        if not hasattr(gs, "planeswalker_attack_targets"):
            gs.planeswalker_attack_targets = {}
            
        gs.planeswalker_attack_targets[attacker_id] = planeswalker_id
        
        logging.debug(f"{attacker_card.name} is attacking planeswalker {planeswalker_card.name}")
        return True
    
    def handle_assign_multiple_blockers(self, attacker_idx):
        """
        Handle assignment of multiple blockers to a single attacker.
        
        Args:
            attacker_idx: Index of the attacker in current_attackers
            
        Returns:
            bool: True if blockers were assigned successfully
        """
        gs = self.game_state
        
        # Check that we're in the correct phase
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS:
            logging.warning(f"Cannot assign blockers when not in DECLARE_BLOCKERS phase ({gs.phase})")
            return False
            
        # Check if attacker_idx is valid
        if attacker_idx < 0 or attacker_idx >= len(gs.current_attackers):
            logging.warning(f"Invalid attacker index: {attacker_idx}")
            return False
            
        # Get the attacker
        attacker_id = gs.current_attackers[attacker_idx]
        attacker_card = gs._safe_get_card(attacker_id)
        
        # Get defender player
        defender = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Get potential blockers
        potential_blockers = [
            card_id for card_id in defender["battlefield"]
            if gs._safe_get_card(card_id) and 
            hasattr(gs._safe_get_card(card_id), 'card_types') and
            'creature' in gs._safe_get_card(card_id).card_types and
            card_id not in defender.get("tapped_permanents", set())
        ]
        
        # Check block restrictions using combat resolver if available
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, '_check_block_restrictions'):
            valid_blockers = [bid for bid in potential_blockers 
                             if gs.combat_resolver._check_block_restrictions(attacker_id, bid)]
        else:
            # Simplified check for flying, etc.
            valid_blockers = []
            attacker_has_flying = False
            
            if attacker_card and hasattr(attacker_card, 'oracle_text'):
                attacker_has_flying = 'flying' in attacker_card.oracle_text.lower()
                
            for bid in potential_blockers:
                blocker_card = gs._safe_get_card(bid)
                if not blocker_card:
                    continue
                    
                if attacker_has_flying:
                    # Only flying or reach creatures can block
                    if hasattr(blocker_card, 'oracle_text'):
                        if 'flying' in blocker_card.oracle_text.lower() or 'reach' in blocker_card.oracle_text.lower():
                            valid_blockers.append(bid)
                else:
                    # Any creature can block
                    valid_blockers.append(bid)
        
        # Check for menace
        attacker_has_menace = False
        if attacker_card and hasattr(attacker_card, 'oracle_text'):
            attacker_has_menace = 'menace' in attacker_card.oracle_text.lower()
            
        # For menace, we need at least two blockers
        if attacker_has_menace and len(valid_blockers) < 2:
            logging.warning(f"Not enough valid blockers for creature with menace")
            return False
            
        # Use the combat resolver to find optimal blocks
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, 'evaluate_potential_blocks'):
            block_options = gs.combat_resolver.evaluate_potential_blocks(attacker_id, valid_blockers)
            
            # Choose the best blocking option
            if block_options and block_options[0]['value'] > 0:
                best_option = block_options[0]
                blockers = best_option['blocker_ids']
                
                if blockers:
                    gs.current_block_assignments[attacker_id] = blockers
                    blocker_names = [gs._safe_get_card(bid).name if gs._safe_get_card(bid) else "Unknown" for bid in blockers]
                    logging.debug(f"Assigned {len(blockers)} blockers to {attacker_card.name}: {', '.join(blocker_names)}")
                    return True
        else:
            # Fallback: Just assign all valid blockers
            if valid_blockers:
                gs.current_block_assignments[attacker_id] = valid_blockers
                blocker_names = [gs._safe_get_card(bid).name if gs._safe_get_card(bid) else "Unknown" for bid in valid_blockers]
                logging.debug(f"Assigned {len(valid_blockers)} blockers to {attacker_card.name}: {', '.join(blocker_names)}")
                return True
                
        logging.warning(f"No valid blockers assigned to {attacker_card.name}")
        return False
    
    def handle_defend_battle(self, battle_type, defender_idx):
        """
        Handle defending against a battle card.
        
        Args:
            battle_type: Type of battle defense (0-4)
            defender_idx: Index of the defending creature
            
        Returns:
            bool: True if battle defense was successful
        """
        gs = self.game_state
        
        # Get all battle cards on the battlefield
        battles = []
        for player in [gs.p1, gs.p2]:
            for card_id in player["battlefield"]:
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'is_battle') and card.is_battle():
                    battles.append((card_id, player))
        
        # Check if there are any battles
        if not battles:
            logging.warning("No battle cards found on the battlefield")
            return False
        
        # Get active player
        active_player = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Get potential defenders
        potential_defenders = [
            card_id for card_id in active_player["battlefield"]
            if gs._safe_get_card(card_id) and 
            hasattr(gs._safe_get_card(card_id), 'card_types') and
            'creature' in gs._safe_get_card(card_id).card_types and
            card_id not in active_player.get("tapped_permanents", set())
        ]
        
        # Check if defender_idx is valid
        if defender_idx < 0 or defender_idx >= len(potential_defenders):
            logging.warning(f"Invalid defender index: {defender_idx}")
            return False
        
        # Get the defender
        defender_id = potential_defenders[defender_idx]
        defender_card = gs._safe_get_card(defender_id)
        
        # Check if battle_type is valid
        if battle_type < 0 or battle_type > 4:
            logging.warning(f"Invalid battle type: {battle_type}")
            return False
        
        # Get the first battle card (could be improved to choose specific battles)
        battle_id, battle_controller = battles[0]
        battle_card = gs._safe_get_card(battle_id)
        
        # Battle defense types:
        # 0: Attack battle (apply damage)
        # 1: Complete chapter (advance to next chapter)
        # 2: Defend battle (prevent damage)
        # 3: Reinforce battle (add defense counters)
        # 4: Chapter ability (activate chapter ability)
        
        if battle_type == 0:  # Attack battle
            # Apply damage to battle based on creature's power
            defender_power = getattr(defender_card, 'power', 0)
            
            # Tap the defender
            active_player["tapped_permanents"].add(defender_id)
            
            # Apply damage
            if not hasattr(battle_card, 'damage'):
                battle_card.damage = 0
            
            battle_card.damage += defender_power
            
            # Check if battle is defeated
            if hasattr(battle_card, 'defense') and battle_card.damage >= battle_card.defense:
                # Move to graveyard
                gs.move_card(battle_id, battle_controller, "battlefield", battle_controller, "graveyard")
                logging.debug(f"{defender_card.name} dealt {defender_power} damage and defeated {battle_card.name}")
            else:
                logging.debug(f"{defender_card.name} dealt {defender_power} damage to {battle_card.name}")
            
            return True
        
        elif battle_type == 1:  # Complete chapter
            # This would advance the battle to the next chapter
            if hasattr(battle_card, 'chapter') and hasattr(battle_card, 'max_chapters'):
                battle_card.chapter += 1
                
                # Check if battle is completed
                if battle_card.chapter > battle_card.max_chapters:
                    # Move to graveyard
                    gs.move_card(battle_id, battle_controller, "battlefield", battle_controller, "graveyard")
                    logging.debug(f"{defender_card.name} completed the final chapter of {battle_card.name}")
                else:
                    logging.debug(f"{defender_card.name} advanced {battle_card.name} to chapter {battle_card.chapter}")
                
                # Tap the defender
                active_player["tapped_permanents"].add(defender_id)
                return True
            else:
                logging.warning(f"Battle card {battle_card.name} doesn't have chapter mechanics")
                return False
        
        elif battle_type == 2:  # Defend battle
            # Tap the defender to prevent damage to battle this turn
            active_player["tapped_permanents"].add(defender_id)
            
            # Set up damage prevention
            if not hasattr(gs, 'battle_damage_prevention'):
                gs.battle_damage_prevention = {}
            
            gs.battle_damage_prevention[battle_id] = True
            
            logging.debug(f"{defender_card.name} is defending {battle_card.name} from damage this turn")
            return True
        
        elif battle_type == 3:  # Reinforce battle
            # Tap the defender to add defense counters
            active_player["tapped_permanents"].add(defender_id)
            
            # Add defense counter
            if not hasattr(battle_card, 'defense_counters'):
                battle_card.defense_counters = 0
            
            battle_card.defense_counters += 1
            logging.debug(f"{defender_card.name} added a defense counter to {battle_card.name}")
            return True
        
        elif battle_type == 4:  # Chapter ability
            # Tap the defender to activate the chapter ability
            active_player["tapped_permanents"].add(defender_id)
            
            # Trigger the chapter ability
            if hasattr(gs, 'trigger_ability'):
                gs.trigger_ability(battle_id, "ACTIVATE_CHAPTER", {"chapter": getattr(battle_card, 'chapter', 1)})
                logging.debug(f"{defender_card.name} activated chapter {getattr(battle_card, 'chapter', 1)} of {battle_card.name}")
                return True
            else:
                logging.warning("Cannot activate chapter ability - trigger_ability not available")
                return False
        
        return False
    
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
    
    def handle_protect_planeswalker(self, attacked_planeswalker_id=None, defender_idx=None):
        """
        Handle redirecting damage from a planeswalker to a creature with improved error handling.
        
        Args:
            attacked_planeswalker_id: ID of the planeswalker being attacked
            defender_idx: Index of the creature to protect the planeswalker
            
        Returns:
            bool: True if protection was set up successfully
        """
        gs = self.game_state
        
        # If no planeswalker ID provided, try to find one being attacked
        if attacked_planeswalker_id is None:
            # Check if there are any planeswalkers being attacked
            if hasattr(gs, 'planeswalker_attack_targets') and gs.planeswalker_attack_targets:
                # Get the first planeswalker being attacked
                for attacker_id, pw_id in gs.planeswalker_attack_targets.items():
                    attacked_planeswalker_id = pw_id
                    break
        
        if attacked_planeswalker_id is None:
            logging.warning("Protect planeswalker failed - no planeswalker being attacked")
            return False
        
        # Find planeswalker controller
        planeswalker_controller = None
        for player in [gs.p1, gs.p2]:
            if attacked_planeswalker_id in player["battlefield"]:
                planeswalker_controller = player
                break
        
        if planeswalker_controller is None:
            logging.warning("Protect planeswalker failed - planeswalker not found on battlefield")
            return False
        
        # Get potential defenders
        potential_defenders = [
            cid for cid in planeswalker_controller["battlefield"]
            if gs._safe_get_card(cid) and 
            hasattr(gs._safe_get_card(cid), 'card_types') and
            'creature' in gs._safe_get_card(cid).card_types and
            cid not in planeswalker_controller.get("tapped_permanents", set())  # Ensure untapped
        ]
        
        if not potential_defenders:
            logging.warning("Protect planeswalker failed - no untapped creatures to protect with")
            return False
        
        # If defender index specified, use that
        if defender_idx is not None and defender_idx < len(potential_defenders):
            defender_id = potential_defenders[defender_idx]
        else:
            # Choose the first available defender
            defender_id = potential_defenders[0]
        
        defender_card = gs._safe_get_card(defender_id)
        if not defender_card:
            logging.warning("Protect planeswalker failed - invalid defender card")
            return False
        
        # Set up the protection - redirect damage to the defender
        if not hasattr(gs, "planeswalker_protectors"):
            gs.planeswalker_protectors = {}
        
        gs.planeswalker_protectors[attacked_planeswalker_id] = defender_id
        
        # Tap the defender (optional, but reasonable for game mechanics)
        planeswalker_controller["tapped_permanents"].add(defender_id)
        
        planeswalker_card = gs._safe_get_card(attacked_planeswalker_id)
        logging.debug(f"{defender_card.name} is now protecting {planeswalker_card.name}")
        
        return True
        
    def _has_keyword(self, card, keyword):
        """Check if a card has a specific keyword ability."""
        if not card:
            return False
            
        # First check if the card has an oracle text with the keyword
        if hasattr(card, 'oracle_text') and keyword.lower() in card.oracle_text.lower():
            return True
            
        # Then check for keyword arrays if present
        if hasattr(card, 'keywords'):
            keyword_mapping = {
                'flying': 0,
                'trample': 1,
                'hexproof': 2,
                'lifelink': 3,
                'deathtouch': 4,
                'first strike': 5,
                'double strike': 6,
                'vigilance': 7,
                'flash': 8,
                'haste': 9,
                'menace': 10
            }
            
            if keyword in keyword_mapping and len(card.keywords) > keyword_mapping[keyword]:
                return card.keywords[keyword_mapping[keyword]] == 1
                
        # Use game state's ability handler if available for more robust checking
        gs = self.game_state
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            # Check if the ability_handler has the appropriate method
            if hasattr(gs.ability_handler, '_check_keyword'):
                return gs.ability_handler._check_keyword(card, keyword)
            elif hasattr(gs.ability_handler, 'has_keyword'):
                card_id = getattr(card, 'card_id', None)
                if card_id is not None:
                    return gs.ability_handler.has_keyword(card_id, keyword)
                    
        return False
