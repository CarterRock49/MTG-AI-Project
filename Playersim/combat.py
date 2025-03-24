import logging
import numpy as np
from collections import defaultdict
from .card import Card
from .debug import DEBUG_MODE

class EnhancedCombatResolver:
    """Advanced combat resolution system implementing detailed MTG combat rules"""
    
    def __init__(self, game_state):
        self.game_state = game_state
        self.combat_log = []
        self.creatures_killed = 0
        self.cards_drawn = 0
        self.damage_prevention = defaultdict(int)  # Track damage prevention effects
        self.combat_triggers = []  # Track combat triggers that need to be processed
        
        # Check if ability_handler exists in game_state
        if not hasattr(game_state, 'ability_handler') or game_state.ability_handler is None:
            # Optionally create it if needed
            try:
                from .ability_handler import AbilityHandler
                game_state.ability_handler = AbilityHandler(game_state)
                logging.debug("Initialized AbilityHandler in EnhancedCombatResolver")
            except (ImportError, AttributeError) as e:
                logging.debug(f"Could not initialize AbilityHandler: {e}")
                
    def assign_manual_combat_damage(self, damage_assignments):
        """
        Apply manual combat damage assignments.
        
        Args:
            damage_assignments: Dict mapping attacker IDs to dicts of {blocker_id: damage} assignments
            
        Returns:
            bool: True if damage was assigned successfully
        """
        gs = self.game_state
        
        if not damage_assignments:
            return False
            
        # Get player references
        attacker = gs.p1 if gs.agent_is_p1 else gs.p2
        defender = gs.p2 if gs.agent_is_p1 else gs.p1
        
        damage_to_creatures = defaultdict(int)  # Total damage dealt to each creature
        damage_to_players = {"p1": 0, "p2": 0}  # Total damage dealt to each player
        creatures_dealt_damage = set()          # Creatures that dealt damage
        
        # Process each attacker's damage assignments
        for attacker_id, assignments in damage_assignments.items():
            attacker_card = gs._safe_get_card(attacker_id)
            if not attacker_card:
                continue
                
            # Verify valid attacker
            if attacker_id not in gs.current_attackers:
                logging.warning(f"Invalid attacker {attacker_id} in manual damage assignments")
                continue
                
            # Determine attacker abilities
            has_deathtouch = self._has_keyword(attacker_card, "deathtouch")
            has_lifelink = self._has_keyword(attacker_card, "lifelink")
            has_trample = self._has_keyword(attacker_card, "trample")
            
            # Track total damage assigned
            total_damage_dealt = 0
            
            # Process assignments to blockers
            blockers = gs.current_block_assignments.get(attacker_id, [])
            for target_id, damage in assignments.items():
                if target_id == 'player':
                    # Damage to player (trample)
                    player_id = "p2" if defender == gs.p2 else "p1"
                    damage_to_players[player_id] += damage
                    defender["life"] -= damage
                    logging.debug(f"COMBAT: {attacker_card.name} deals {damage} damage to player (trample)")
                    total_damage_dealt += damage
                elif target_id in blockers:
                    # Damage to blocker
                    blocker_card = gs._safe_get_card(target_id)
                    if not blocker_card:
                        continue
                        
                    damage_to_creatures[target_id] += damage
                    logging.debug(f"COMBAT: {attacker_card.name} deals {damage} damage to {blocker_card.name}")
                    total_damage_dealt += damage
                else:
                    logging.warning(f"Invalid target {target_id} for attacker {attacker_id}")
                    
            # Apply lifelink
            if has_lifelink and total_damage_dealt > 0:
                attacker["life"] += total_damage_dealt
                logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {total_damage_dealt} life")
                
            # Mark creature as having dealt damage
            if total_damage_dealt > 0:
                creatures_dealt_damage.add(attacker_id)
        
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
                self._process_planeswalker_damage(attacker_id, attacker, planeswalker_id)
            else:
                # Deal damage to opponent directly
                self._process_player_damage(attacker_id, attacker, defender)
        
        # Process combat triggers
        self._process_combat_triggers(creatures_dealt_damage)
        
        # Process state-based actions (creatures dying)
        self._check_lethal_damage(damage_to_creatures, set())
        
        # Clean up and mark combat as resolved
        gs.current_attackers = []
        gs.current_block_assignments = {}
        gs.combat_damage_dealt = True
        
        return True

    def _process_planeswalker_damage(self, attacker_id, attacker_player, planeswalker_id):
        """Process damage to a planeswalker."""
        gs = self.game_state
        
        attacker_card = gs._safe_get_card(attacker_id)
        planeswalker_card = gs._safe_get_card(planeswalker_id)
        
        if not attacker_card or not planeswalker_card:
            return
            
        # Check for damage prevention/redirection
        if hasattr(gs, "planeswalker_protectors") and planeswalker_id in gs.planeswalker_protectors:
            protector_id = gs.planeswalker_protectors[planeswalker_id]
            if self._redirect_damage_to_protector(attacker_id, protector_id):
                return
        
        # Apply damage to planeswalker
        damage = self._get_card_power(attacker_card, attacker_player)
        
        # Check for lifelink
        if self._has_keyword(attacker_card, "lifelink"):
            attacker_player["life"] += damage
            logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {damage} life")
        
        # Reduce loyalty
        if hasattr(planeswalker_card, 'loyalty'):
            planeswalker_card.loyalty -= damage
            logging.debug(f"COMBAT: {attacker_card.name} deals {damage} damage to planeswalker {planeswalker_card.name}")
            
            # Check if planeswalker died
            if planeswalker_card.loyalty <= 0:
                # Find controller using utility if available
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
                    logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} died from combat damage")

    def _process_player_damage(self, attacker_id, attacker_player, defender_player):
        """Process direct damage to a player."""
        gs = self.game_state
        
        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card:
            return
            
        # Calculate damage
        damage = self._get_card_power(attacker_card, attacker_player)
        
        # Apply damage to player
        defender_player["life"] -= damage
        logging.debug(f"COMBAT: {attacker_card.name} deals {damage} unblocked damage to player")
        
        # Apply lifelink if applicable
        if self._has_keyword(attacker_card, "lifelink"):
            attacker_player["life"] += damage
            logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {damage} life")

    def _redirect_damage_to_protector(self, attacker_id, protector_id):
        """Redirect damage from planeswalker to protector creature."""
        gs = self.game_state
        
        attacker_card = gs._safe_get_card(attacker_id)
        protector_card = gs._safe_get_card(protector_id)
        
        if not attacker_card or not protector_card:
            return False
        
        # Find controller of protector using utility if available
        protector_controller = None
        if 'get_card_controller' in globals():
            protector_controller = gs.get_card_controller(gs, protector_id)
        else:
            # Fallback to direct check
            for player in [gs.p1, gs.p2]:
                if protector_id in player["battlefield"]:
                    protector_controller = player
                    break
                
        if not protector_controller:
            return False
        
        # Calculate damage
        attacker_player = gs.p1 if gs.agent_is_p1 else gs.p2
        damage = self._get_card_power(attacker_card, attacker_player)
        
        # Apply damage to protector
        if not hasattr(protector_controller, "damage_counters"):
            protector_controller["damage_counters"] = {}
            
        protector_controller["damage_counters"][protector_id] = protector_controller["damage_counters"].get(protector_id, 0) + damage
        logging.debug(f"COMBAT: {attacker_card.name} deals {damage} damage to protector {protector_card.name}")
        
        # Check for lethal damage
        protector_toughness = self._get_card_toughness(protector_card, protector_controller)
        current_damage = protector_controller["damage_counters"].get(protector_id, 0)
        
        if current_damage >= protector_toughness:
            if not self._has_keyword(protector_card, "indestructible"):
                gs.move_card(protector_id, protector_controller, "battlefield", protector_controller, "graveyard")
                logging.debug(f"COMBAT: Protector {protector_card.name} died from combat damage")
        
        # Apply lifelink if applicable
        if self._has_keyword(attacker_card, "lifelink"):
            attacker_player = gs.p1 if gs.agent_is_p1 else gs.p2
            attacker_player["life"] += damage
            logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {damage} life")
        
        return True
        
    def apply_combat_results(self, combat_results):
        """
        Apply comprehensive combat results to the game state.
        
        Args:
            combat_results: Dictionary containing combat outcomes including:
                - damage_to_player: Amount of damage dealt to opponent
                - life_gained: Amount of life gained by attacker
                - attackers_dying: List of attacker creature IDs that died
                - blockers_dying: List of blocker creature IDs that died
                
        Returns:
            bool: Whether combat results were successfully applied
        """
        gs = self.game_state
        
        # Apply damage to opponent with damage prevention consideration
        if "damage_to_player" in combat_results:
            defender = gs.p2 if gs.agent_is_p1 else gs.p1
            damage_amount = combat_results["damage_to_player"]
            
            # Check for damage prevention effects
            if hasattr(gs, 'apply_replacement_effect'):
                damage_context = {
                    "target_id": "p2" if defender == gs.p2 else "p1",
                    "target_is_player": True,
                    "damage_amount": damage_amount,
                    "is_combat_damage": True
                }
                modified_context, was_replaced = gs.apply_replacement_effect("DAMAGE", damage_context)
                if was_replaced:
                    damage_amount = modified_context.get("damage_amount", damage_amount)
            
            # Apply the damage to player
            if damage_amount > 0:
                defender["life"] = max(0, defender["life"] - damage_amount)
                logging.debug(f"Combat: Applied {damage_amount} combat damage to player (now at {defender['life']} life)")
                
                # Track damage for triggers
                if not hasattr(gs, "damage_dealt_this_turn"):
                    gs.damage_dealt_this_turn = {}
                
                defender_id = "p2" if defender == gs.p2 else "p1"
                gs.damage_dealt_this_turn[defender_id] = gs.damage_dealt_this_turn.get(defender_id, 0) + damage_amount
            
        # Apply life gain from lifelink and other effects
        if "life_gained" in combat_results:
            attacker = gs.p1 if gs.agent_is_p1 else gs.p2
            life_gain = combat_results["life_gained"]
            
            # Check for life gain replacement/modification effects
            if hasattr(gs, 'apply_replacement_effect'):
                life_gain_context = {
                    "player": attacker,
                    "life_gain": life_gain,
                    "from_combat": True
                }
                modified_context, was_replaced = gs.apply_replacement_effect("LIFE_GAIN", life_gain_context)
                if was_replaced:
                    life_gain = modified_context.get("life_gain", life_gain)
            
            if life_gain > 0:
                attacker["life"] += life_gain
                logging.debug(f"Combat: Gained {life_gain} life from combat effects (now at {attacker['life']} life)")
                
                # Track life gain for triggers
                if not hasattr(gs, "life_gained_this_turn"):
                    gs.life_gained_this_turn = {}
                
                attacker_id = "p1" if attacker == gs.p1 else "p2"
                gs.life_gained_this_turn[attacker_id] = gs.life_gained_this_turn.get(attacker_id, 0) + life_gain
                
                # Trigger life gain abilities
                for permanent_id in attacker["battlefield"]:
                    gs.trigger_ability(permanent_id, "LIFE_GAINED", {"amount": life_gain, "from_combat": True})
        
        # Apply creature deaths with proper ordering and triggers
        # Process attackers dying
        for creature_id in combat_results.get("attackers_dying", []):
            # Find controller
            for player in [gs.p1, gs.p2]:
                if creature_id in player["battlefield"]:
                    # Check for indestructible
                    creature = gs._safe_get_card(creature_id)
                    if creature and hasattr(creature, 'oracle_text') and "indestructible" in creature.oracle_text.lower():
                        logging.debug(f"Combat: {creature.name} has indestructible and survived")
                        continue
                    
                    # Check for regeneration
                    if hasattr(player, "regeneration_shields") and creature_id in player["regeneration_shields"]:
                        player["regeneration_shields"].remove(creature_id)
                        player["tapped_permanents"].add(creature_id)
                        if "damage_counters" in player and creature_id in player["damage_counters"]:
                            del player["damage_counters"][creature_id]
                        logging.debug(f"Combat: {creature.name} regenerated and survived")
                        continue
                    
                    # Normal death processing
                    gs.move_card(creature_id, player, "battlefield", player, "graveyard")
                    
                    # Track for statistics
                    self.creatures_killed += 1
                    
                    # Log the death
                    logging.debug(f"Combat: Attacker {gs._safe_get_card(creature_id).name} died")
                    break
        
        # Process blockers dying
        for creature_id in combat_results.get("blockers_dying", []):
            # Find controller
            for player in [gs.p1, gs.p2]:
                if creature_id in player["battlefield"]:
                    # Check for indestructible
                    creature = gs._safe_get_card(creature_id)
                    if creature and hasattr(creature, 'oracle_text') and "indestructible" in creature.oracle_text.lower():
                        logging.debug(f"Combat: {creature.name} has indestructible and survived")
                        continue
                    
                    # Check for regeneration
                    if hasattr(player, "regeneration_shields") and creature_id in player["regeneration_shields"]:
                        player["regeneration_shields"].remove(creature_id)
                        player["tapped_permanents"].add(creature_id)
                        if "damage_counters" in player and creature_id in player["damage_counters"]:
                            del player["damage_counters"][creature_id]
                        logging.debug(f"Combat: {creature.name} regenerated and survived")
                        continue
                    
                    # Normal death processing
                    gs.move_card(creature_id, player, "battlefield", player, "graveyard")
                    
                    # Track for statistics
                    self.creatures_killed += 1
                    
                    # Log the death
                    logging.debug(f"Combat: Blocker {gs._safe_get_card(creature_id).name} died")
                    break
        
        # Clean up combat state
        gs.current_attackers = []
        gs.current_block_assignments = {}
        gs.combat_damage_dealt = True
        
        # Apply state-based actions to handle any remaining effects
        if hasattr(gs, 'check_state_based_actions'):
            gs.check_state_based_actions()
        
        return True
                
    def find_optimal_attack(self, possible_attackers, max_combinations=32):
        """
        Find the optimal attack configuration using combat simulation and strategy memory.
        
        Args:
            possible_attackers: List of creature IDs that can attack
            max_combinations: Maximum number of attack combinations to evaluate
            
        Returns:
            List of creature IDs representing the optimal attackers
        """
        # Store the original game state
        gs = self.game_state
        original_attackers = list(gs.current_attackers)
        original_block_assignments = {k: list(v) for k, v in gs.current_block_assignments.items()}
        
        # Generate attack combinations efficiently
        attack_combinations = []
        
        # Check if we have a strategic_planner for advanced evaluation
        use_strategic_evaluation = hasattr(gs, 'strategic_planner') and gs.strategic_planner is not None
        
        # For many possible attackers, use a smarter approach
        if len(possible_attackers) > 5:  # 2^5 = 32 combinations
            # Sort attackers by power (highest first)
            sorted_attackers = sorted(
                possible_attackers,
                key=lambda aid: self._get_card_power(gs._safe_get_card(aid), gs.p1 if gs.agent_is_p1 else gs.p2),
                reverse=True
            )
            
            # Always consider attacking with everything
            attack_combinations.append(sorted_attackers)
            
            # Consider attacking with just the strongest creatures
            if len(sorted_attackers) >= 2:
                attack_combinations.append(sorted_attackers[:len(sorted_attackers)//2])
            
            # Consider the top creatures individually
            for attacker in sorted_attackers[:3]:
                attack_combinations.append([attacker])
                
            # Consider strategic combinations (top n creatures)
            for n in range(2, min(5, len(sorted_attackers))):
                attack_combinations.append(sorted_attackers[:n])
            
            # If using strategic planner, query historical successful patterns
            if use_strategic_evaluation:
                # Try to get successful attack patterns from strategy memory
                if hasattr(gs.strategic_planner, 'memory') and gs.strategic_planner.memory:
                    memory = gs.strategic_planner.memory
                    
                    # Extract the current game state pattern
                    if hasattr(memory, 'extract_strategy_pattern'):
                        current_pattern = memory.extract_strategy_pattern(gs)
                        
                        # Find similar patterns in memory where attacks were successful
                        if hasattr(memory, '_pattern_similarity') and hasattr(memory, 'strategies'):
                            matched_patterns = []
                            for pattern, strategy_data in memory.strategies.items():
                                if isinstance(pattern, tuple) and len(pattern) >= len(current_pattern):
                                    # Compare patterns using similarity function
                                    similarity = memory._pattern_similarity(current_pattern, pattern)
                                    if similarity > 0.7 and strategy_data.get('reward', 0) > 0:
                                        matched_patterns.append((pattern, strategy_data, similarity))
                            
                            # Sort by similarity * reward for best matches
                            if matched_patterns:
                                matched_patterns.sort(key=lambda x: x[1].get('reward', 0) * x[2], reverse=True)
                                
                                # Extract attack patterns from the best matches
                                for _, strategy_data, _ in matched_patterns[:3]:
                                    if 'action_sequences' in strategy_data:
                                        # Look for attack sequences in this strategy
                                        for action_seq in strategy_data.get('action_sequences', []):
                                            attack_actions = [a for a in action_seq if isinstance(a, dict) and a.get('action_type') == 'ATTACK']
                                            if attack_actions:
                                                # Reconstruct the attack pattern and try to apply it
                                                pattern_attackers = []
                                                for attack_action in attack_actions:
                                                    # Map historical patterns to current board state
                                                    param = attack_action.get('param')
                                                    if param is not None and param < len(possible_attackers):
                                                        pattern_attackers.append(possible_attackers[param])
                                                
                                                if pattern_attackers:
                                                    attack_combinations.append(pattern_attackers)
                    
            # Add some more combinations for diversity
            import random
            while len(attack_combinations) < max_combinations and len(attack_combinations) < 2**len(possible_attackers):
                size = random.randint(1, len(sorted_attackers))
                combo = random.sample(sorted_attackers, size)
                if combo not in attack_combinations:
                    attack_combinations.append(combo)
        else:
            # Generate all possible attack combinations
            import itertools
            for i in range(1, len(possible_attackers) + 1):
                attack_combinations.extend(list(itertools.combinations(possible_attackers, i)))
        
        # Evaluate each attack combination
        best_attack = []
        best_score = -float('inf')
        best_results = None
        
        # Track detailed evaluations if we're using strategic evaluation
        attack_evaluations = []
        
        for attackers in attack_combinations:
            # Set up the attack configuration
            gs.current_attackers = list(attackers)
            gs.current_block_assignments = {}
            
            # Simulate the opponent's optimal blocks
            self._simulate_opponent_blocks()
            
            # Simulate combat
            results = self.simulate_combat()
            
            # Calculate a score for this attack configuration
            attack_score = results["expected_value"]
            
            # If using strategic planner, incorporate its evaluation
            if use_strategic_evaluation:
                strategic_score = gs.strategic_planner.evaluate_attack_action(list(attackers))
                
                # Blend the scores (60% simulation, 40% strategic)
                attack_score = (attack_score * 0.6) + (strategic_score * 0.4)
                
                # Record this evaluation for learning
                attack_evaluations.append({
                    'attackers': list(attackers),
                    'simulation_score': results["expected_value"],
                    'strategic_score': strategic_score,
                    'combined_score': attack_score,
                    'damage_to_player': results.get('damage_to_player', 0),
                    'attackers_dying': len(results.get('attackers_dying', [])),
                    'blockers_dying': len(results.get('blockers_dying', []))
                })
            
            # Update best attack if better
            if attack_score > best_score:
                best_score = attack_score
                best_attack = list(attackers)
                best_results = results
        
        # If using strategic planner, record the best attack pattern for future learning
        if use_strategic_evaluation and best_attack and hasattr(gs.strategic_planner, 'memory'):
            memory = gs.strategic_planner.memory
            
            # Create an action sequence representing this attack
            attack_actions = []
            for attacker_id in best_attack:
                try:
                    idx = possible_attackers.index(attacker_id)
                    attack_actions.append({
                        'action_type': 'ATTACK',
                        'param': idx,
                        'card_id': attacker_id
                    })
                except ValueError:
                    continue
                    
            if attack_actions:
                # Extract the current game state pattern
                if hasattr(memory, 'extract_strategy_pattern'):
                    current_pattern = memory.extract_strategy_pattern(gs)
                    
                    # Calculate reward based on the combat outcome
                    combat_reward = 0.0
                    if best_results:
                        # Reward for damage
                        combat_reward += min(best_results.get('damage_to_player', 0) * 0.15, 0.75)
                        
                        # Reward for favorable exchanges
                        attackers_dying = len(best_results.get('attackers_dying', []))
                        blockers_dying = len(best_results.get('blockers_dying', []))
                        exchange_value = blockers_dying - attackers_dying
                        combat_reward += max(-0.5, min(0.5, exchange_value * 0.2))
                    
                    # Record this pattern in strategy memory
                    if hasattr(memory, 'update_strategy'):
                        memory.update_strategy(current_pattern, combat_reward)
                    
                    # Record the action sequence
                    if hasattr(memory, 'record_action_sequence'):
                        memory.record_action_sequence(attack_actions, combat_reward, gs)
        
        # Restore original game state
        gs.current_attackers = original_attackers
        gs.current_block_assignments = original_block_assignments
        
        logging.debug(f"Optimal attack found: {len(best_attack)} attackers, score: {best_score:.2f}")
        if best_results:
            logging.debug(f"Expected: {best_results['damage_to_player']} damage, {len(best_results['attackers_dying'])} attackers dying, {len(best_results['blockers_dying'])} blockers dying")
        
        return best_attack
        
    def _simulate_opponent_blocks(self):
        """
        Simulate the opponent's blocking decisions using evaluate_potential_blocks.
        """
        gs = self.game_state
        defender = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Get potential blockers
        potential_blockers = [
            card_id for card_id in defender["battlefield"]
            if gs._safe_get_card(card_id) and 
            'creature' in gs._safe_get_card(card_id).card_types and
            card_id not in defender.get("tapped_permanents", set())
        ]
        
        # For each attacker, evaluate blocking options
        block_assignments = {}
        remaining_blockers = set(potential_blockers)
        
        # First handle evasive attackers since they're harder to block
        sorted_attackers = sorted(
            gs.current_attackers,
            key=lambda aid: self._has_evasion(gs._safe_get_card(aid)),
            reverse=True
        )
        
        for attacker_id in sorted_attackers:
            # Skip if no blockers left
            if not remaining_blockers:
                break
                
            # Get only valid blockers for this attacker
            valid_blockers = [bid for bid in remaining_blockers if self._check_block_restrictions(attacker_id, bid)]
            
            if not valid_blockers:
                continue
                
            # Evaluate potential blocks for this attacker
            blocking_options = self.evaluate_potential_blocks(attacker_id, valid_blockers)
            
            # Choose the best blocking option
            if blocking_options and blocking_options[0]['value'] > 0:
                # Only block if valuable
                best_option = blocking_options[0]
                blockers = best_option['blocker_ids']
                
                if blockers:
                    block_assignments[attacker_id] = blockers
                    
                    # Remove used blockers
                    for blocker_id in blockers:
                        if blocker_id in remaining_blockers:
                            remaining_blockers.remove(blocker_id)
        
        # Set the block assignments
        gs.current_block_assignments = block_assignments
        
    def _has_evasion(self, card):
        """Helper method to determine if a creature has evasion abilities for sorting purposes."""
        if not card:
            return 0
            
        evasion_score = 0
        
        # Check for various evasion abilities
        if self._has_keyword(card, "flying"):
            evasion_score += 3
        if self._has_keyword(card, "menace"):
            evasion_score += 2
        if self._has_keyword(card, "shadow") or self._has_keyword(card, "fear") or self._has_keyword(card, "intimidate"):
            evasion_score += 4
            
        # Unblockable is strongest
        if hasattr(card, "oracle_text") and "can't be blocked" in card.oracle_text.lower():
            evasion_score += 5
            
        # Also consider power
        if hasattr(card, "power"):
            evasion_score += min(card.power, 5) / 2
            
        return evasion_score
            
    def resolve_combat(self):
        """
        Implements a complete combat resolution sequence following MTG rules:
        1. First strike damage step (if needed)
        2. Regular damage step
        3. Combat triggers
        4. State-based actions (creature death)
        """
        try:
            gs = self.game_state
            
            # Reset combat metrics
            self.combat_log = []
            self.creatures_killed = 0
            self.cards_drawn = 0
            self.damage_prevention.clear()
            self.combat_triggers = []
            
            if gs.combat_damage_dealt:
                logging.debug("Combat damage already applied this turn, skipping.")
                return 0
            
            if not gs.current_attackers:
                logging.debug("No attackers declared; skipping combat resolution.")
                return 0
            
            # Get player references
            attacker = gs.p1 if gs.agent_is_p1 else gs.p2
            defender = gs.p2 if gs.agent_is_p1 else gs.p1
        
            # Log combat participants
            self._log_combat_state()
            
            # Process combat-related abilities before damage calculation
            self._process_combat_abilities()
            
            # Check if first strike damage is needed
            has_first_strike = False
            has_double_strike = False
            
            # Check attackers for first/double strike
            for attacker_id in gs.current_attackers:
                attacker_card = gs._safe_get_card(attacker_id)
                if not attacker_card:
                    continue
                    
                if self._has_keyword(attacker_card, "first strike"):
                    has_first_strike = True
                if self._has_keyword(attacker_card, "double strike"):
                    has_double_strike = True
                    has_first_strike = True  # Double strike includes first strike
            
            # Check blockers for first/double strike
            for attacker_id, blockers in gs.current_block_assignments.items():
                for blocker_id in blockers:
                    blocker_card = gs._safe_get_card(blocker_id)
                    if not blocker_card:
                        continue
                        
                    if self._has_keyword(blocker_card, "first strike"):
                        has_first_strike = True
                    if self._has_keyword(blocker_card, "double strike"):
                        has_double_strike = True
                        has_first_strike = True  # Double strike includes first strike
            
            # Initialize damage tracking structures
            damage_to_creatures = defaultdict(int)  # Total damage dealt to each creature
            damage_to_players = {"p1": 0, "p2": 0}  # Total damage dealt to each player - using string keys
            creatures_dealt_damage = set()          # Creatures that dealt damage (for lifelink etc.)
            creatures_dealt_first_strike_damage = set()  # For specific first strike triggers
            killed_in_first_strike = set()          # Creatures that died in first strike damage step
            
            # STEP 1: First Strike Damage (if needed)
            if has_first_strike:
                logging.debug("COMBAT: First strike damage step")
                
                # Process attackers with first strike
                for attacker_id in gs.current_attackers:
                    attacker_card = gs._safe_get_card(attacker_id)
                    if not attacker_card:
                        continue
                        
                    # Check if attacker has first strike or double strike
                    has_fs = self._has_keyword(attacker_card, "first strike")
                    has_ds = self._has_keyword(attacker_card, "double strike")
                    
                    # Skip if doesn't have first/double strike    
                    if not has_fs and not has_ds:
                        continue
                        
                    # Process first strike damage
                    self._process_attacker_damage(
                        attacker_id,
                        attacker,
                        defender,
                        damage_to_creatures,
                        damage_to_players,
                        creatures_dealt_damage,
                        killed_in_first_strike,
                        is_first_strike=True
                    )
                    
                    # Track specifically for first strike triggers
                    creatures_dealt_first_strike_damage.add(attacker_id)
                
                # Process blockers with first strike
                for attacker_id, blockers in gs.current_block_assignments.items():
                    # Skip if attacker died from first strike
                    if attacker_id in killed_in_first_strike:
                        continue
                        
                    for blocker_id in blockers:
                        blocker_card = gs._safe_get_card(blocker_id)
                        if not blocker_card:
                            continue
                            
                        # Check if blocker has first strike or double strike
                        has_fs = self._has_keyword(blocker_card, "first strike")
                        has_ds = self._has_keyword(blocker_card, "double strike")
                        
                        # Skip if doesn't have first/double strike    
                        if not has_fs and not has_ds:
                            continue
                            
                        # Process first strike damage
                        self._process_blocker_damage(
                            blocker_id,
                            attacker_id,
                            attacker,
                            defender,
                            damage_to_creatures,
                            creatures_dealt_damage,
                            killed_in_first_strike,
                            is_first_strike=True
                        )
                        
                        # Track specifically for first strike triggers
                        creatures_dealt_first_strike_damage.add(blocker_id)
                
                # Process state-based actions (creatures dying)
                self._check_lethal_damage(damage_to_creatures, killed_in_first_strike)
                
                # Process first strike damage triggers
                if creatures_dealt_first_strike_damage:
                    self._process_combat_triggers(creatures_dealt_first_strike_damage, is_first_strike=True)
                
                logging.debug(f"COMBAT: First strike damage - {len(killed_in_first_strike)} creatures died")
            
            # STEP 2: Regular Damage
            logging.debug("COMBAT: Regular damage step")
            
            # Normal attackers deal damage
            for attacker_id in gs.current_attackers:
                # Skip if dead from first strike
                if attacker_id in killed_in_first_strike:
                    continue
                    
                attacker_card = gs._safe_get_card(attacker_id)
                if not attacker_card:
                    continue
                    
                # Check if attacker has first strike (but not double strike)
                has_first_strike_only = self._has_keyword(attacker_card, "first strike") and not self._has_keyword(attacker_card, "double strike")
                
                # Skip creatures with only first strike (not double strike)
                if has_first_strike_only:
                    continue
                    
                # Process regular damage
                self._process_attacker_damage(
                    attacker_id,
                    attacker,
                    defender,
                    damage_to_creatures,
                    damage_to_players,
                    creatures_dealt_damage,
                    killed_in_first_strike,
                    is_first_strike=False
                )
            
            # Process blocker damage
            for attacker_id, blockers in gs.current_block_assignments.items():
                # Skip if attacker died from first strike
                if attacker_id in killed_in_first_strike:
                    continue
                    
                for blocker_id in blockers:
                    # Skip if blocker died from first strike
                    if blocker_id in killed_in_first_strike:
                        continue
                        
                    blocker_card = gs._safe_get_card(blocker_id)
                    if not blocker_card:
                        continue
                        
                    # Check if blocker has first strike (but not double strike)
                    has_first_strike_only = self._has_keyword(blocker_card, "first strike") and not self._has_keyword(blocker_card, "double strike")
                    
                    # Skip creatures with only first strike (not double strike)
                    if has_first_strike_only:
                        continue
                        
                    # Process regular damage
                    self._process_blocker_damage(
                        blocker_id,
                        attacker_id,
                        attacker,
                        defender,
                        damage_to_creatures,
                        creatures_dealt_damage,
                        killed_in_first_strike,
                        is_first_strike=False
                    )
            
            # STEP 3: Process Combat Triggers
            self._process_combat_triggers(creatures_dealt_damage)
            
            # STEP 4: Process State-Based Actions (creatures dying)
            self._check_lethal_damage(damage_to_creatures, set())
            
            defender_id = "p2" if defender == gs.p2 else "p1"
            total_damage_to_defender = damage_to_players[defender_id]
            
            # Cleanup and mark combat as resolved
            gs.current_attackers = []
            gs.current_block_assignments = {}
            gs.combat_damage_dealt = True
            
            # Final combat summary
            logging.debug(f"COMBAT SUMMARY: Total damage to opponent: {total_damage_to_defender}, "
                        f"Creatures killed: {self.creatures_killed}")
            logging.debug(f"COMBAT SUMMARY: Life totals after combat - Attacker: {attacker['life']}, "
                        f"Defender: {defender['life']}")
            
            return total_damage_to_defender
        
        except Exception as e:
            logging.error(f"Error during combat resolution: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            
            # Return 0 damage as a fallback
            return 0
    
    def _is_valid_attacker(self, attacker_id, attacker_card):
        """Check if an attacker is valid using AbilityHandler when available."""
        gs = self.game_state
        
        # Use AbilityHandler if available
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            # Check if it has a method to check attack restrictions
            if hasattr(gs.ability_handler, 'is_valid_attacker'):
                return gs.ability_handler.is_valid_attacker(attacker_id, attacker_card)
        
        # Fallback implementation
        attacker = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Basic validation
        if not attacker_card or 'creature' not in attacker_card.card_types:
            return False
            
        # Check summoning sickness and haste
        if (attacker_id in attacker["entered_battlefield_this_turn"] and 
            not self._has_keyword(attacker_card, "haste")):
            return False
            
        # Check if tapped or otherwise unable to attack
        if attacker_id in attacker["tapped_permanents"]:
            return False
            
        # Check for defender
        if self._has_keyword(attacker_card, "defender"):
            return False
        
        return True
    
    def _determine_optimal_damage_assignment(self, attacker_id, blockers, attacker_power, has_deathtouch, has_trample):
        """
        Determine the optimal damage assignment for an attacker against multiple blockers.
        
        Args:
            attacker_id: ID of the attacking creature
            blockers: List of blocking creature IDs
            attacker_power: Power of the attacking creature
            has_deathtouch: Whether the attacker has deathtouch
            has_trample: Whether the attacker has trample
            
        Returns:
            dict: Mapping of blocker IDs to damage amounts, with 'player' key for trample damage
        """
        gs = self.game_state
        damage_assignment = {}
        remaining_damage = attacker_power
        
        # Get the damage assignment order (either from first_strike_ordering or default)
        if hasattr(gs, 'first_strike_ordering') and attacker_id in gs.first_strike_ordering:
            ordered_blockers = [b for b in gs.first_strike_ordering[attacker_id] if b in blockers]
            # Add any blockers not in the ordering (shouldn't happen normally)
            ordered_blockers.extend([b for b in blockers if b not in ordered_blockers])
        else:
            # Default to ordering by toughness (lowest first) as a reasonable heuristic
            defender = gs.p2 if gs.agent_is_p1 else gs.p1
            ordered_blockers = sorted(blockers, 
                                key=lambda bid: self._get_card_toughness(gs._safe_get_card(bid), defender) 
                                            if gs._safe_get_card(bid) else 0)
        
        # With deathtouch, we only need to assign 1 damage to each blocker
        if has_deathtouch:
            for blocker_id in ordered_blockers:
                if remaining_damage <= 0:
                    break
                    
                damage_assignment[blocker_id] = 1
                remaining_damage -= 1
        else:
            # Without deathtouch, we need to assign lethal damage to each blocker in order
            for blocker_id in ordered_blockers:
                if remaining_damage <= 0:
                    break
                    
                blocker_card = gs._safe_get_card(blocker_id)
                if not blocker_card:
                    continue
                    
                # Find controller
                blocker_controller = gs.get_card_controller(blocker_id)
                if not blocker_controller:
                    continue
                    
                # Calculate lethal damage needed
                toughness = self._get_card_toughness(blocker_card, blocker_controller)
                # Account for existing damage on the blocker
                existing_damage = blocker_controller.get("damage_counters", {}).get(blocker_id, 0)
                lethal_damage = max(0, toughness - existing_damage)
                
                # Assign lethal damage or as much as available
                damage_to_assign = min(lethal_damage, remaining_damage)
                damage_assignment[blocker_id] = damage_to_assign
                remaining_damage -= damage_to_assign
        
        # If there's leftover damage and trample, assign to player
        if has_trample and remaining_damage > 0:
            damage_assignment['player'] = remaining_damage
        
        return damage_assignment
    
    def _has_keyword(self, card, keyword):
        """
        More robust keyword detection checking both oracle text, keywords attribute, and ability handler.
        
        Args:
            card: The card object to check
            keyword: The keyword to look for
            
        Returns:
            bool: True if the card has the keyword, False otherwise
        """
        if not card:
            return False
            
        gs = self.game_state
        
        # First try ability_handler if available for most accurate results
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            # Try different method variants that might exist
            if hasattr(gs.ability_handler, '_check_keyword'):
                return gs.ability_handler._check_keyword(card, keyword)
            elif hasattr(gs.ability_handler, 'has_keyword'):
                card_id = getattr(card, 'card_id', None)
                if card_id is not None:
                    return gs.ability_handler.has_keyword(card_id, keyword)
        
        # Check oracle text
        if hasattr(card, 'oracle_text') and isinstance(card.oracle_text, str):
            # More precise keyword matching with word boundaries where appropriate
            keyword_lower = keyword.lower()
            oracle_lower = card.oracle_text.lower()
            
            # For keywords that are likely to be part of other words (like "flash")
            if keyword_lower in ["flash", "haste", "reach"]:
                import re
                pattern = r'\b' + re.escape(keyword_lower) + r'\b'
                if re.search(pattern, oracle_lower):
                    return True
            elif keyword_lower in oracle_lower:
                return True
        
        # Check keywords array with error handling
        if hasattr(card, 'keywords') and isinstance(card.keywords, list):
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
                'menace': 10,
                'reach': 11,
                'indestructible': 12,
                'defender': 13
            }
            
            if keyword in keyword_mapping and len(card.keywords) > keyword_mapping[keyword]:
                return card.keywords[keyword_mapping[keyword]] == 1
        
        # Check card types for artifact (sometimes referenced as a "keyword")
        if keyword.lower() == "artifact" and hasattr(card, 'card_types'):
            return 'artifact' in card.card_types
        
        # Check colors for color-related keywords
        if keyword.lower() in ["white", "blue", "black", "red", "green"] and hasattr(card, 'colors'):
            color_index = {"white": 0, "blue": 1, "black": 2, "red": 3, "green": 4}
            if keyword.lower() in color_index and len(card.colors) > color_index[keyword.lower()]:
                return card.colors[color_index[keyword.lower()]] == 1
        
        return False
            
    def simulate_first_strike_damage(self):
        """
        Simulate the first strike damage phase.
        
        Returns:
            dict: Results of first strike damage
        """
        gs = self.game_state
        
        # Identify creatures with first strike or double strike
        first_strikers = []
        for attacker_id in gs.current_attackers:
            attacker = gs._safe_get_card(attacker_id)
            if attacker and hasattr(attacker, 'oracle_text'):
                if "first strike" in attacker.oracle_text.lower() or "double strike" in attacker.oracle_text.lower():
                    first_strikers.append(attacker_id)
        
        # For each blocker, check for first strike
        blockers_with_first_strike = {}
        for attacker_id, blocker_ids in gs.current_block_assignments.items():
            first_strike_blockers = []
            for blocker_id in blocker_ids:
                blocker = gs._safe_get_card(blocker_id)
                if blocker and hasattr(blocker, 'oracle_text'):
                    if "first strike" in blocker.oracle_text.lower() or "double strike" in blocker.oracle_text.lower():
                        first_strike_blockers.append(blocker_id)
            if first_strike_blockers:
                blockers_with_first_strike[attacker_id] = first_strike_blockers
        
        # Only proceed if there are first strikers
        if not first_strikers and not blockers_with_first_strike:
            return {"no_first_strikers": True}
        
        # Process first strike attackers
        attackers_dying = set()
        blockers_dying = set()
        damage_to_player = 0
        
        # For each attacking first striker
        for attacker_id in first_strikers:
            # Get the blockers
            blocker_ids = gs.current_block_assignments.get(attacker_id, [])
            attacker = gs._safe_get_card(attacker_id)
            
            if not blocker_ids:  # Unblocked
                # Deal damage to player
                damage_to_player += attacker.power if hasattr(attacker, 'power') else 0
            else:
                # Assign first strike damage to blockers
                attacker_power = attacker.power if hasattr(attacker, 'power') else 0
                
                # Apply damage assignment rules
                for blocker_id in blocker_ids:
                    blocker = gs._safe_get_card(blocker_id)
                    if not blocker:
                        continue
                        
                    # Calculate damage to assign
                    damage_to_assign = min(attacker_power, 
                                        blocker.toughness if hasattr(blocker, 'toughness') else 0)
                    
                    if damage_to_assign <= 0:
                        continue
                        
                    # Apply damage
                    if damage_to_assign >= blocker.toughness:
                        blockers_dying.add(blocker_id)
                    
                    attacker_power -= damage_to_assign
                    
                    if attacker_power <= 0:
                        break
        
        # Process first strike blockers
        for attacker_id, first_strike_blocker_ids in blockers_with_first_strike.items():
            attacker = gs._safe_get_card(attacker_id)
            if not attacker:
                continue
                
            # Calculate total first strike damage from blockers
            total_blocker_damage = sum(
                gs._safe_get_card(bid).power 
                for bid in first_strike_blocker_ids 
                if gs._safe_get_card(bid) and hasattr(gs._safe_get_card(bid), 'power')
            )
            
            # Apply damage to attacker
            if total_blocker_damage >= (attacker.toughness if hasattr(attacker, 'toughness') else 0):
                attackers_dying.add(attacker_id)
        
        return {
            "attackers_dying": list(attackers_dying),
            "blockers_dying": list(blockers_dying),
            "damage_to_player": damage_to_player
        }
        
    def _process_attacker_damage(self, attacker_id, attacker_player, defender_player, 
                            damage_to_creatures, damage_to_players, creatures_dealt_damage,
                            killed_creatures, is_first_strike):
        """Process damage from an attacking creature with comprehensive combat rules."""
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)
        
        if not attacker_card or attacker_id in killed_creatures:
            return 0
        
        # Get blockers for this attacker
        blockers = gs.current_block_assignments.get(attacker_id, [])
        valid_blockers = [b for b in blockers if b not in killed_creatures]
        
        defender_id = "p2" if defender_player == gs.p2 else "p1"
        
        # Determine base damage to assign
        base_power = self._get_card_power(attacker_card, attacker_player)
        damage_to_assign = base_power
        
        # Create comprehensive damage context for replacement effects
        damage_context = {
            "source_id": attacker_id,
            "target_id": defender_id if not valid_blockers else valid_blockers[0],
            "target_is_player": not valid_blockers,
            "damage_amount": damage_to_assign,
            "is_combat_damage": True,
            "has_deathtouch": self._has_keyword(attacker_card, "deathtouch"),
            "has_lifelink": self._has_keyword(attacker_card, "lifelink"),
            "has_infect": self._has_keyword(attacker_card, "infect"),
            "has_wither": self._has_keyword(attacker_card, "wither") if hasattr(attacker_card, "oracle_text") and "wither" in attacker_card.oracle_text.lower() else False
        }

        # Apply global replacement effects to the initial damage context
        modified_context, was_replaced = gs.apply_replacement_effect("DAMAGE", damage_context)

        if was_replaced:
            # Use the modified values from the replaced effect
            damage_to_assign = modified_context.get("damage_amount", damage_to_assign)
            has_deathtouch = modified_context.get("has_deathtouch", damage_context["has_deathtouch"])
            has_lifelink = modified_context.get("has_lifelink", damage_context["has_lifelink"])
            has_infect = modified_context.get("has_infect", damage_context["has_infect"])
            has_wither = modified_context.get("has_wither", damage_context["has_wither"])
            # Additional replacement context updates
            target_is_player = modified_context.get("target_is_player", damage_context["target_is_player"])
            target_id = modified_context.get("target_id", damage_context["target_id"])
        else:
            # Use original context values
            has_deathtouch = damage_context["has_deathtouch"]
            has_lifelink = damage_context["has_lifelink"]
            has_infect = damage_context["has_infect"]
            has_wither = damage_context["has_wither"]
            target_is_player = damage_context["target_is_player"]
            target_id = damage_context["target_id"]
                
        # Check if this creature should deal damage in this step
        if is_first_strike:
            # In first strike step, only first strike/double strike creatures deal damage
            has_first_strike = self._has_keyword(attacker_card, "first strike")
            has_double_strike = self._has_keyword(attacker_card, "double strike")
            if not has_first_strike and not has_double_strike:
                return 0
        else:
            # In regular step, first strike creatures don't deal damage again (only double strike)
            has_first_strike = self._has_keyword(attacker_card, "first strike")
            has_double_strike = self._has_keyword(attacker_card, "double strike")
            if has_first_strike and not has_double_strike:
                return 0
        
        # Get attacker's abilities after any replacements
        has_trample = self._has_keyword(attacker_card, "trample")
        
        # Calculate total damage
        total_damage_dealt = 0
        
        # Handle damage based on updated target information
        if target_is_player:
            # Damage is going to a player (either unblocked or redirected)
            player_id = target_id  # Should be "p1" or "p2"
            target_player = gs.p1 if player_id == "p1" else gs.p2
            
            if has_infect:
                # Infect deals poison counters instead of damage
                target_player["poison_counters"] = target_player.get("poison_counters", 0) + damage_to_assign
                logging.debug(f"COMBAT: {attacker_card.name} deals {damage_to_assign} poison counters to player")
                
                if target_player["poison_counters"] >= 10:
                    target_player["life"] = 0  # Game loss from poison
                    logging.debug(f"COMBAT: Player reached 10+ poison counters and loses the game")
            else:
                # Regular damage to player - use player index as key
                damage_to_players[player_id] += damage_to_assign
                target_player["life"] -= damage_to_assign
                logging.debug(f"COMBAT: {attacker_card.name} deals {damage_to_assign} damage to player")
            
            total_damage_dealt = damage_to_assign
            creatures_dealt_damage.add(attacker_id)
        else:
            # Damage is going to creatures (either blocked or redirected)
            if not valid_blockers:
                # Damage was redirected from player to a creature
                redirected_target = gs._safe_get_card(target_id)
                if redirected_target:
                    if has_infect or has_wither:
                        # Infect/wither deals -1/-1 counters to creatures
                        if not hasattr(redirected_target, "counters"):
                            redirected_target.counters = {}
                        
                        redirected_target.counters["-1/-1"] = redirected_target.counters.get("-1/-1", 0) + damage_to_assign
                        redirected_target.power = max(0, redirected_target.power - damage_to_assign)
                        redirected_target.toughness = max(0, redirected_target.toughness - damage_to_assign)
                        
                        logging.debug(f"COMBAT: {attacker_card.name} deals {damage_to_assign} -1/-1 counters to redirected target {redirected_target.name}")
                        
                        # Check if creature dies from -1/-1 counters
                        if redirected_target.toughness <= 0:
                            killed_creatures.add(target_id)
                    else:
                        # Regular damage
                        damage_to_creatures[target_id] = damage_to_creatures.get(target_id, 0) + damage_to_assign
                        logging.debug(f"COMBAT: {attacker_card.name} deals {damage_to_assign} damage to redirected target {redirected_target.name}")
                    
                    total_damage_dealt = damage_to_assign
                    creatures_dealt_damage.add(attacker_id)
            else:
                # Process normal blocked damage with replacement effects for each blocker
                remaining_damage = damage_to_assign
                
                # Implement damage assignment order
                sorted_blockers = sorted(valid_blockers, 
                                    key=lambda bid: self._get_card_toughness(gs._safe_get_card(bid), defender_player))
                
                for blocker_id in sorted_blockers:
                    if remaining_damage <= 0 and not has_deathtouch:
                        break
                        
                    blocker_card = gs._safe_get_card(blocker_id)
                    if not blocker_card:
                        continue
                        
                    blocker_toughness = self._get_card_toughness(blocker_card, defender_player)
                    
                    # Calculate lethal damage (deathtouch makes 1 damage lethal)
                    lethal_damage = 1 if has_deathtouch else blocker_toughness
                    
                    # Assign damage to this blocker
                    damage_to_this_blocker = min(remaining_damage, lethal_damage)
                    
                    # Create context for replacement effects for each blocker
                    blocker_damage_context = {
                        "source_id": attacker_id,
                        "target_id": blocker_id,
                        "target_is_player": False,
                        "damage_amount": damage_to_this_blocker,
                        "is_combat_damage": True,
                        "has_deathtouch": has_deathtouch,
                        "has_lifelink": has_lifelink,
                        "has_infect": has_infect,
                        "has_wither": has_wither
                    }
                    
                    # Apply replacement effects for this specific blocker damage
                    modified_blocker_context, was_blocker_replaced = gs.apply_replacement_effect("DAMAGE", blocker_damage_context)
                    
                    if was_blocker_replaced:
                        # Extract modified values
                        blocker_damage = modified_blocker_context.get("damage_amount", damage_to_this_blocker)
                        blocker_target_id = modified_blocker_context.get("target_id", blocker_id)
                        blocker_target_is_player = modified_blocker_context.get("target_is_player", False)
                        blocker_has_infect = modified_blocker_context.get("has_infect", has_infect)
                        blocker_has_wither = modified_blocker_context.get("has_wither", has_wither)
                        
                        # Check if damage was redirected
                        if blocker_target_is_player or blocker_target_id != blocker_id:
                            if blocker_target_is_player:
                                # Redirected to a player
                                player_id = blocker_target_id
                                redirect_player = gs.p1 if player_id == "p1" else gs.p2
                                
                                if blocker_has_infect:
                                    redirect_player["poison_counters"] = redirect_player.get("poison_counters", 0) + blocker_damage
                                    logging.debug(f"COMBAT: Damage redirected to player as poison counters")
                                else:
                                    damage_to_players[player_id] += blocker_damage
                                    redirect_player["life"] -= blocker_damage
                                    logging.debug(f"COMBAT: Damage redirected to player")
                            else:
                                # Redirected to another creature
                                redirect_card = gs._safe_get_card(blocker_target_id)
                                if redirect_card:
                                    if blocker_has_infect or blocker_has_wither:
                                        # Handle infect/wither for redirected damage
                                        if not hasattr(redirect_card, "counters"):
                                            redirect_card.counters = {}
                                        
                                        redirect_card.counters["-1/-1"] = redirect_card.counters.get("-1/-1", 0) + blocker_damage
                                        redirect_card.power = max(0, redirect_card.power - blocker_damage)
                                        redirect_card.toughness = max(0, redirect_card.toughness - blocker_damage)
                                        
                                        logging.debug(f"COMBAT: Damage redirected as -1/-1 counters to {redirect_card.name}")
                                        
                                        if redirect_card.toughness <= 0:
                                            killed_creatures.add(blocker_target_id)
                                    else:
                                        damage_to_creatures[blocker_target_id] = damage_to_creatures.get(blocker_target_id, 0) + blocker_damage
                                        logging.debug(f"COMBAT: Damage redirected to {redirect_card.name}")
                        else:
                            # Regular damage to original blocker
                            if blocker_has_infect or blocker_has_wither:
                                # Handle infect/wither
                                if not hasattr(blocker_card, "counters"):
                                    blocker_card.counters = {}
                                
                                blocker_card.counters["-1/-1"] = blocker_card.counters.get("-1/-1", 0) + blocker_damage
                                blocker_card.power = max(0, blocker_card.power - blocker_damage)
                                blocker_card.toughness = max(0, blocker_card.toughness - blocker_damage)
                                
                                logging.debug(f"COMBAT: {attacker_card.name} deals {blocker_damage} -1/-1 counters to {blocker_card.name}")
                                
                                if blocker_card.toughness <= 0:
                                    killed_creatures.add(blocker_id)
                            else:
                                damage_to_creatures[blocker_id] = damage_to_creatures.get(blocker_id, 0) + blocker_damage
                                logging.debug(f"COMBAT: {attacker_card.name} deals {blocker_damage} damage to {blocker_card.name}")
                    else:
                        # No replacement - apply normal damage
                        if has_infect or has_wither:
                            # Infect/wither deals -1/-1 counters to creatures
                            if not hasattr(blocker_card, "counters"):
                                blocker_card.counters = {}
                            
                            blocker_card.counters["-1/-1"] = blocker_card.counters.get("-1/-1", 0) + damage_to_this_blocker
                            blocker_card.power = max(0, blocker_card.power - damage_to_this_blocker)
                            blocker_card.toughness = max(0, blocker_card.toughness - damage_to_this_blocker)
                            
                            logging.debug(f"COMBAT: {attacker_card.name} deals {damage_to_this_blocker} -1/-1 counters to {blocker_card.name}")
                            
                            if blocker_card.toughness <= 0:
                                killed_creatures.add(blocker_id)
                        else:
                            # Regular damage
                            damage_to_creatures[blocker_id] = damage_to_creatures.get(blocker_id, 0) + damage_to_this_blocker
                            logging.debug(f"COMBAT: {attacker_card.name} deals {damage_to_this_blocker} damage to {blocker_card.name}")
                    
                    remaining_damage -= damage_to_this_blocker
                    total_damage_dealt += damage_to_this_blocker
                    
                    if damage_to_this_blocker > 0:
                        creatures_dealt_damage.add(attacker_id)
                
                # Handle trample with replacement effects
                if has_trample and remaining_damage > 0:
                    # Create context for trample damage
                    trample_context = {
                        "source_id": attacker_id,
                        "target_id": defender_id,
                        "target_is_player": True,
                        "damage_amount": remaining_damage,
                        "is_combat_damage": True,
                        "is_trample": True,
                        "has_deathtouch": has_deathtouch,
                        "has_lifelink": has_lifelink,
                        "has_infect": has_infect
                    }
                    
                    # Apply replacement effects for trample damage
                    modified_trample, was_trample_replaced = gs.apply_replacement_effect("DAMAGE", trample_context)
                    
                    if was_trample_replaced:
                        # Extract modified values
                        trample_damage = modified_trample.get("damage_amount", remaining_damage)
                        trample_target_id = modified_trample.get("target_id", defender_id)
                        trample_target_is_player = modified_trample.get("target_is_player", True)
                        trample_has_infect = modified_trample.get("has_infect", has_infect)
                        
                        # Check if trample damage was redirected
                        if not trample_target_is_player:
                            # Redirected to a creature
                            redirect_card = gs._safe_get_card(trample_target_id)
                            if redirect_card:
                                if trample_has_infect or has_wither:
                                    # Handle infect for redirected trample
                                    if not hasattr(redirect_card, "counters"):
                                        redirect_card.counters = {}
                                    
                                    redirect_card.counters["-1/-1"] = redirect_card.counters.get("-1/-1", 0) + trample_damage
                                    redirect_card.power = max(0, redirect_card.power - trample_damage)
                                    redirect_card.toughness = max(0, redirect_card.toughness - trample_damage)
                                    
                                    logging.debug(f"COMBAT: Trample damage redirected as -1/-1 counters")
                                    
                                    if redirect_card.toughness <= 0:
                                        killed_creatures.add(trample_target_id)
                                else:
                                    damage_to_creatures[trample_target_id] = damage_to_creatures.get(trample_target_id, 0) + trample_damage
                                    logging.debug(f"COMBAT: Trample damage redirected to creature")
                        else:
                            # Still to a player, possibly different player
                            trample_player = gs.p1 if trample_target_id == "p1" else gs.p2
                            
                            if trample_has_infect:
                                trample_player["poison_counters"] = trample_player.get("poison_counters", 0) + trample_damage
                                logging.debug(f"COMBAT: Trample from {attacker_card.name} deals {trample_damage} poison counters")
                                
                                if trample_player["poison_counters"] >= 10:
                                    trample_player["life"] = 0
                                    logging.debug(f"COMBAT: Player reached 10+ poison counters and loses the game")
                            else:
                                damage_to_players[trample_target_id] += trample_damage
                                trample_player["life"] -= trample_damage
                                logging.debug(f"COMBAT: Trample from {attacker_card.name} deals {trample_damage} damage")
                    else:
                        # No replacement - normal trample damage
                        if has_infect:
                            defender_player["poison_counters"] = defender_player.get("poison_counters", 0) + remaining_damage
                            logging.debug(f"COMBAT: Trample from {attacker_card.name} deals {remaining_damage} poison counters")
                            
                            if defender_player["poison_counters"] >= 10:
                                defender_player["life"] = 0
                                logging.debug(f"COMBAT: Player reached 10+ poison counters and loses the game")
                        else:
                            damage_to_players[defender_id] += remaining_damage
                            defender_player["life"] -= remaining_damage
                            logging.debug(f"COMBAT: Trample from {attacker_card.name} deals {remaining_damage} damage")
                    
                    total_damage_dealt += remaining_damage
        
        # Process lifelink after all damage is dealt
        if has_lifelink and total_damage_dealt > 0:
            attacker_player["life"] += total_damage_dealt
            logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {total_damage_dealt} life")
        
        self._add_combat_trigger(attacker_id, "deals_damage", {
            "damage_amount": total_damage_dealt,
            "to_player": target_is_player or (has_trample and remaining_damage > 0)
        }, is_first_strike=is_first_strike)
        
        return total_damage_dealt
    
    def _process_blocker_damage(self, blocker_id, attacker_id, attacker_player, defender_player,
                            damage_to_creatures, creatures_dealt_damage, killed_creatures, is_first_strike):
        """Process damage from a blocking creature."""
        gs = self.game_state
        blocker_card = gs._safe_get_card(blocker_id)
        attacker_card = gs._safe_get_card(attacker_id)
        
        if not blocker_card or not attacker_card or blocker_id in killed_creatures or attacker_id in killed_creatures:
            return 0
        
        # Define defender_id 
        defender_id = "p2" if defender_player == gs.p2 else "p1"
        
        # Determine blockers for this attacker (even though we might not use it directly)
        blockers = gs.current_block_assignments.get(attacker_id, [])
        
        # Get base damage (blocker's power)
        damage_to_assign = self._get_card_power(blocker_card, defender_player)
        
        damage_context = {
            "source_id": blocker_id,
            "target_id": defender_id if not blockers else attacker_id,
            "damage_amount": damage_to_assign,
            "is_combat_damage": True,
            "has_deathtouch": self._has_keyword(blocker_card, "deathtouch"),
            "has_lifelink": self._has_keyword(blocker_card, "lifelink"),
            "has_infect": self._has_keyword(blocker_card, "infect")
        }

        modified_context, was_replaced = self.game_state.apply_replacement_effect("DAMAGE", damage_context)

        if was_replaced:
            # Use the modified values from the replaced effect
            damage_to_assign = modified_context.get("damage_amount", damage_to_assign)
            has_deathtouch = modified_context.get("has_deathtouch", False)
            has_lifelink = modified_context.get("has_lifelink", False)
            has_infect = modified_context.get("has_infect", False)
        
        # Check if this creature should deal damage in this step
        if is_first_strike:
            # In first strike step, only first strike/double strike creatures deal damage
            has_first_strike = self._has_keyword(blocker_card, "first strike")
            has_double_strike = self._has_keyword(blocker_card, "double strike")
            if not has_first_strike and not has_double_strike:
                return 0
        else:
            # In regular step, first strike creatures don't deal damage again (only double strike)
            has_first_strike = self._has_keyword(blocker_card, "first strike")
            has_double_strike = self._has_keyword(blocker_card, "double strike")
            if has_first_strike and not has_double_strike:
                return 0
            
        # Get blocker's power and abilities
        power = self._get_card_power(blocker_card, defender_player)
        has_deathtouch = self._has_keyword(blocker_card, "deathtouch")
        has_lifelink = self._has_keyword(blocker_card, "lifelink")
        infect = self._has_keyword(blocker_card, "infect")
        
        
        # Assign damage to attacker
        damage_to_assign = power
        
        if damage_to_assign > 0:
            if infect:
                # Infect deals -1/-1 counters instead of damage
                if not hasattr(attacker_card, "counters"):
                    attacker_card.counters = {}
                    
                attacker_card.counters["-1/-1"] = attacker_card.counters.get("-1/-1", 0) + damage_to_assign
                attacker_card.power = max(0, attacker_card.power - damage_to_assign)
                attacker_card.toughness = max(0, attacker_card.toughness - damage_to_assign)
                
                logging.debug(f"COMBAT: Blocking {blocker_card.name} deals {damage_to_assign} -1/-1 counters to {attacker_card.name}")
                
                # Check if creature dies from -1/-1 counters
                if attacker_card.toughness <= 0:
                    killed_creatures.add(attacker_id)
                    logging.debug(f"COMBAT: {attacker_card.name} dies from -1/-1 counters")
            else:
                # Regular damage
                damage_to_creatures[attacker_id] += damage_to_assign
                logging.debug(f"COMBAT: Blocking {blocker_card.name} deals {damage_to_assign} damage to {attacker_card.name}")
            
            creatures_dealt_damage.add(blocker_id)
            
            # Process lifelink
            if has_lifelink:
                defender_player["life"] += damage_to_assign
                logging.debug(f"COMBAT: Lifelink from {blocker_card.name} gained {damage_to_assign} life")
            
            # Process combat triggers for the blocker
            self._add_combat_trigger(blocker_id, "deals_damage", {
                "damage_amount": damage_to_assign,
                "to_player": False,
                "to_creature": attacker_id
            }, is_first_strike=is_first_strike)
            
            # Process deathtouch
            if has_deathtouch and damage_to_assign > 0:
                # Deathtouch causes any amount of damage to be lethal
                if attacker_id not in killed_creatures:
                    damage_to_creatures[attacker_id] = max(
                        damage_to_creatures.get(attacker_id, 0),
                        self._get_card_toughness(attacker_card, attacker_player)
                    )
                    logging.debug(f"COMBAT: Deathtouch from {blocker_card.name} marked {attacker_card.name} for death")
        
        return damage_to_assign
        
    def _assign_damage_to_multiple_blockers(self, attacker_id, blockers, attacker_player, defender_player):
        """Sophisticated damage assignment for multiple blockers following MTG rules."""
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)
        
        if not attacker_card or not hasattr(attacker_card, 'power'):
            return 0
            
        # Check special abilities
        has_deathtouch = self._has_keyword(attacker_card, "deathtouch")
        has_trample = self._has_keyword(attacker_card, "trample")
        
        # Sort blockers in damage assignment order
        # In a real game, the attacking player would choose the order
        # Here we sort by toughness (lowest first) as a reasonable heuristic
        sorted_blockers = sorted(blockers, 
                            key=lambda bid: (self._get_card_toughness(gs._safe_get_card(bid), defender_player) 
                                            if gs._safe_get_card(bid) else 0))
        
        # Initialize damage tracking
        damage_to_assign = self._get_card_power(attacker_card, attacker_player)
        damage_to_creatures = {}
        
        # Assign minimum damage to each blocker
        for blocker_id in sorted_blockers:
            blocker_card = gs._safe_get_card(blocker_id)
            if not blocker_card:
                continue
                
            # With deathtouch, only need to assign 1 damage to each blocker
            min_damage = 1 if has_deathtouch else self._get_card_toughness(blocker_card, defender_player)
            
            # Assign minimum lethal damage or as much as available
            assigned_damage = min(min_damage, damage_to_assign)
            damage_to_creatures[blocker_id] = assigned_damage
            damage_to_assign -= assigned_damage
            
            # If out of damage, break
            if damage_to_assign <= 0:
                break
        
        # If there's leftover damage and trample, it goes to the player
        trample_damage = 0
        if has_trample and damage_to_assign > 0:
            trample_damage = damage_to_assign
            defender_id = "p2" if defender_player == gs.p2 else "p1"
            gs.p1["life"] -= trample_damage if defender_id == "p1" else 0
            gs.p2["life"] -= trample_damage if defender_id == "p2" else 0
            
        # Log the damage assignment
        blocker_names = [f"{gs._safe_get_card(bid).name}: {damage_to_creatures.get(bid, 0)}" 
                        for bid in sorted_blockers if gs._safe_get_card(bid)]
        logging.debug(f"Damage assignment for {attacker_card.name}: " + 
                    ", ".join(blocker_names) + 
                    (f", {trample_damage} to player" if trample_damage > 0 else ""))
        
        # Apply the damage to creatures
        for blocker_id, damage in damage_to_creatures.items():
            blocker_card = gs._safe_get_card(blocker_id)
            if not blocker_card:
                continue
                
            # Apply damage
            if "damage_counters" not in defender_player:
                defender_player["damage_counters"] = {}
            defender_player["damage_counters"][blocker_id] = defender_player["damage_counters"].get(blocker_id, 0) + damage
            
            # Apply deathtouch effect if needed
            if has_deathtouch and damage > 0:
                if not hasattr(defender_player, "deathtouch_damage"):
                    defender_player["deathtouch_damage"] = {}
                defender_player["deathtouch_damage"][blocker_id] = defender_player["deathtouch_damage"].get(blocker_id, 0) + damage
        
        # Return total damage dealt
        return sum(damage_to_creatures.values()) + trample_damage
    
    def _process_state_based_actions(self, damage_to_creatures, killed_creatures, max_iterations=10):
        """
        Process state-based actions in a controlled manner to prevent infinite loops.
        
        Args:
            damage_to_creatures: Dict of creature IDs to damage amounts
            killed_creatures: Set of creature IDs that have already been killed
            max_iterations: Maximum number of iterations to prevent infinite loops
            
        Returns:
            set: Updated set of killed creatures
        """
        gs = self.game_state
        iteration_count = 0
        has_changes = True
        
        # Track creatures killed in this process
        newly_killed = set()
        
        # Create a local copy to avoid concurrent modification
        damage_to_process = dict(damage_to_creatures)
        already_killed = set(killed_creatures)
        
        # Process state-based actions until no more changes or max iterations
        while has_changes and iteration_count < max_iterations:
            has_changes = False
            iteration_count += 1
            newly_processed = []
            
            for creature_id, damage in damage_to_process.items():
                # Skip if already handled
                if creature_id in already_killed or creature_id in newly_killed or creature_id in newly_processed:
                    continue
                    
                # Find creature details
                creature_card = gs._safe_get_card(creature_id)
                if not creature_card:
                    newly_processed.append(creature_id)
                    continue
                    
                # Find creature controller
                owner = gs.get_card_controller(creature_id)
                if not owner:
                    newly_processed.append(creature_id)
                    continue
                    
                # Check if damage is lethal
                toughness = self._get_card_toughness(creature_card, owner)
                
                # Indestructible creatures don't die from damage
                if self._has_keyword(creature_card, "indestructible"):
                    logging.debug(f"COMBAT: {creature_card.name} is indestructible and ignores lethal damage")
                    newly_processed.append(creature_id)
                    continue
                    
                # Check for damage marked as deathtouch
                has_deathtouch_damage = False
                if hasattr(owner, "deathtouch_damage") and creature_id in owner["deathtouch_damage"]:
                    if owner["deathtouch_damage"][creature_id] > 0:
                        has_deathtouch_damage = True
                
                # Check for regeneration shields
                has_regenerated = False
                if hasattr(creature_card, 'regeneration_shields') and creature_card.regeneration_shields > 0:
                    # Use up a regeneration shield instead of dying
                    creature_card.regeneration_shields -= 1
                    # Tap the creature as part of regeneration
                    owner["tapped_permanents"].add(creature_id)
                    # Remove damage
                    damage_to_process[creature_id] = 0
                    if hasattr(owner, "damage_counters") and creature_id in owner["damage_counters"]:
                        owner["damage_counters"][creature_id] = 0
                    has_regenerated = True
                    has_changes = True
                    logging.debug(f"COMBAT: {creature_card.name} regenerated instead of dying")
                    newly_processed.append(creature_id)
                    continue
                
                if (damage >= toughness or has_deathtouch_damage) and not has_regenerated:
                    # Check for replacement effects before moving the card
                    was_replaced = False
                    if hasattr(gs, 'apply_replacement_effect'):
                        death_context = {
                            "card_id": creature_id,
                            "card_type": "creature",
                            "controller": owner,
                            "destination": "graveyard",
                            "from_damage": True
                        }
                        modified_death, was_replaced = gs.apply_replacement_effect("DIES", death_context)
                        
                        if was_replaced:
                            dest = modified_death.get("destination", "graveyard")
                            if dest != "battlefield":  # Don't move if staying on battlefield
                                gs.move_card(creature_id, owner, "battlefield", owner, dest)
                                logging.debug(f"COMBAT: {creature_card.name} moved to {dest} instead of dying")
                                newly_killed.add(creature_id)
                                has_changes = True
                    
                    if not was_replaced:
                        # Lethal damage - move to graveyard
                        gs.move_card(creature_id, owner, "battlefield", owner, "graveyard")
                        self.creatures_killed += 1
                        logging.debug(f"COMBAT: {creature_card.name} died from lethal damage")
                        newly_killed.add(creature_id)
                        has_changes = True
                    
                    # Trigger "dies" abilities
                    if hasattr(gs, 'trigger_ability'):
                        gs.trigger_ability(creature_id, "DIES", {"from_combat": True, "from_damage": True})
                        
                    newly_processed.append(creature_id)
            
            # Remove processed creatures from damage_to_process
            for creature_id in newly_processed:
                if creature_id in damage_to_process:
                    del damage_to_process[creature_id]
        
        # If we hit the iteration limit, log a warning
        if iteration_count >= max_iterations and has_changes:
            logging.warning(f"COMBAT: Reached maximum state-based action iterations ({max_iterations})")
        
        # Update the killed_creatures set with newly killed creatures
        return killed_creatures.union(newly_killed)

    def _check_block_restrictions(self, attacker_id, blocker_id):
        """
        Check restrictions on who can block based on keywords with comprehensive rules handling.
        
        Args:
            attacker_id: ID of the attacking creature
            blocker_id: ID of the potential blocking creature
            
        Returns:
            bool: True if the blocker can block the attacker, False otherwise
        """
        gs = self.game_state
        
        # Use AbilityHandler if available
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            # Check if it has a method to check block restrictions
            if hasattr(gs.ability_handler, 'check_block_restrictions'):
                return gs.ability_handler.check_block_restrictions(attacker_id, blocker_id)
        
        # Fallback implementation if AbilityHandler not available
        attacker = gs._safe_get_card(attacker_id)
        blocker = gs._safe_get_card(blocker_id)
        
        if not attacker or not blocker:
            return False
            
        # Get blocker controller
        blocker_controller = self._get_card_controller(blocker_id)
        if not blocker_controller:
            return False
        
        # Check flying
        attacker_has_flying = self._has_keyword(attacker, "flying")
        blocker_has_flying = self._has_keyword(blocker, "flying")
        blocker_has_reach = self._has_keyword(blocker, "reach")
        
        if attacker_has_flying and not (blocker_has_flying or blocker_has_reach):
            return False
        
        # Check menace (requires at least two blockers)
        if self._has_keyword(attacker, "menace"):
            blockers = []
            for a_id, b_list in gs.current_block_assignments.items():
                if a_id == attacker_id:
                    blockers = b_list
                    break
                    
            # If this is the first blocker assigned to a creature with menace, it's valid
            # Additional checks will happen when blocks are confirmed
            if not blockers:
                return True
            # If this would be the second+ blocker, it's valid
            elif blocker_id not in blockers:
                return True
            # Otherwise, a single blocker is trying to block something with menace
            else:
                return False
        
        # Check intimidate (can only be blocked by artifact creatures or creatures that share a color)
        if self._has_keyword(attacker, "intimidate"):
            blocker_is_artifact = self._has_keyword(blocker, "artifact")
            
            if not blocker_is_artifact:
                # Check if blocker shares a color with attacker
                shares_color = False
                if hasattr(attacker, 'colors') and hasattr(blocker, 'colors'):
                    for i, color in enumerate(["white", "blue", "black", "red", "green"]):
                        if i < len(attacker.colors) and i < len(blocker.colors):
                            if attacker.colors[i] == 1 and blocker.colors[i] == 1:
                                shares_color = True
                                break
                
                if not shares_color:
                    return False
        
        # Check fear (can only be blocked by black or artifact creatures)
        if self._has_keyword(attacker, "fear"):
            blocker_is_artifact = self._has_keyword(blocker, "artifact")
            blocker_is_black = False
            if hasattr(blocker, 'colors') and len(blocker.colors) > 2:
                blocker_is_black = blocker.colors[2] == 1  # Black is at index 2
                    
            if not (blocker_is_artifact or blocker_is_black):
                return False
                    
        # Check shadow (can only be blocked by creatures with shadow)
        if self._has_keyword(attacker, "shadow"):
            blocker_has_shadow = self._has_keyword(blocker, "shadow")
                    
            if not blocker_has_shadow:
                return False
        
        # Check protection
        if hasattr(attacker, 'protection'):
            for protection_from in attacker.protection:
                # Protection from a color
                if protection_from in ["white", "blue", "black", "red", "green"]:
                    if self._has_keyword(blocker, protection_from):
                        return False
                
                # Protection from a creature type
                if protection_from == "creatures":
                    return False
        
        # Check decayed - decayed creatures can't block
        if self._has_keyword(blocker, "decayed"):
            return False
        
        # Check defender - creatures with defender can't attack
        if self._has_keyword(blocker, "defender") and hasattr(blocker, 'oracle_text') and "can attack" not in blocker.oracle_text.lower():
            return True  # Defender doesn't affect blocking, only attacking
        
        # Check skulk - can't be blocked by creatures with greater power
        if self._has_keyword(attacker, "skulk"):
            if hasattr(attacker, 'power') and hasattr(blocker, 'power'):
                if blocker.power > attacker.power:
                    return False
        
        # Check unblockable
        if hasattr(attacker, 'oracle_text') and "can't be blocked" in attacker.oracle_text.lower():
            # Check for conditional unblockability
            if "can't be blocked except by" in attacker.oracle_text.lower():
                exception_text = attacker.oracle_text.lower().split("can't be blocked except by")[1].split(".")[0].strip()
                
                # Handle common exceptions
                if "artifact creatures" in exception_text and self._has_keyword(blocker, "artifact"):
                    return True
                elif "flying creatures" in exception_text and self._has_keyword(blocker, "flying"):
                    return True
                elif "walls" in exception_text and hasattr(blocker, 'subtypes') and "wall" in [s.lower() for s in blocker.subtypes]:
                    return True
                else:
                    return False
            else:
                # Completely unblockable
                return False
        
        # Check if the blocker has "can't block" or similar restrictions
        if hasattr(blocker, 'oracle_text'):
            if "can't block" in blocker.oracle_text.lower():
                return False
                
            if "can't block creatures with flying" in blocker.oracle_text.lower() and attacker_has_flying:
                return False
                
            if "can only block creatures with flying" in blocker.oracle_text.lower() and not attacker_has_flying:
                return False
        
        # If no restrictions prevent blocking, it's valid
        return True

    def _process_combat_abilities(self):
        """Process additional combat-related abilities before damage calculation."""
        gs = self.game_state
        
        # Battle cry - When this creature attacks, each other attacking creature gets +1/+0 until end of turn
        for attacker_id in gs.current_attackers:
            attacker = gs._safe_get_card(attacker_id)
            if not attacker or not hasattr(attacker, 'oracle_text'):
                continue
                
            if "battle cry" in attacker.oracle_text.lower():
                # Give +1/+0 to other attackers
                for other_attacker_id in gs.current_attackers:
                    if other_attacker_id != attacker_id:
                        other_attacker = gs._safe_get_card(other_attacker_id)
                        if other_attacker:
                            # Add temporary buff
                            controller = None
                            for player in [gs.p1, gs.p2]:
                                if attacker_id in player["battlefield"]:
                                    controller = player
                                    break
                                    
                            if controller:
                                if not hasattr(controller, "temp_buffs"):
                                    controller["temp_buffs"] = {}
                                    
                                if other_attacker_id not in controller["temp_buffs"]:
                                    controller["temp_buffs"][other_attacker_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                                    
                                controller["temp_buffs"][other_attacker_id]["power"] += 1
                                
                logging.debug(f"Battle cry: {attacker.name} gave other attackers +1/+0")
                
            # Process melee (gets +1/+1 for each opponent attacked)
            if "melee" in attacker.oracle_text.lower():
                controller = None
                for player in [gs.p1, gs.p2]:
                    if attacker_id in player["battlefield"]:
                        controller = player
                        break
                        
                if controller:
                    # In real MTG, melee depends on how many different opponents you attacked
                    # For single-opponent format, give +1/+1 if any attack was declared
                    if len(gs.current_attackers) > 0:
                        # Add temporary buff
                        if not hasattr(controller, "temp_buffs"):
                            controller["temp_buffs"] = {}
                            
                        if attacker_id not in controller["temp_buffs"]:
                            controller["temp_buffs"][attacker_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                            
                        controller["temp_buffs"][attacker_id]["power"] += 1
                        controller["temp_buffs"][attacker_id]["toughness"] += 1
                        
                        logging.debug(f"Melee: {attacker.name} got +1/+1 until end of turn")
        
        # Exalted - When a creature attacks alone, it gets +1/+1 until end of turn
        if len(gs.current_attackers) == 1:
            attacker_id = gs.current_attackers[0]
            attacker = gs._safe_get_card(attacker_id)
            if attacker:
                # Check all permanents for exalted
                for player in [gs.p1, gs.p2]:
                    # Only check controller's permanents
                    if attacker_id not in player["battlefield"]:
                        continue
                        
                    exalted_count = 0
                    for permanent_id in player["battlefield"]:
                        permanent = gs._safe_get_card(permanent_id)
                        if permanent and hasattr(permanent, 'oracle_text') and "exalted" in permanent.oracle_text.lower():
                            exalted_count += 1
                    
                    if exalted_count > 0:
                        # Add temporary buff
                        if not hasattr(player, "temp_buffs"):
                            player["temp_buffs"] = {}
                            
                        if attacker_id not in player["temp_buffs"]:
                            player["temp_buffs"][attacker_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                            
                        player["temp_buffs"][attacker_id]["power"] += exalted_count
                        player["temp_buffs"][attacker_id]["toughness"] += exalted_count
                        
                        logging.debug(f"Exalted: {attacker.name} got +{exalted_count}/+{exalted_count} from attacking alone")
                        
        # Process flanking abilities
        for attacker_id in gs.current_attackers:
            attacker = gs._safe_get_card(attacker_id)
            if not attacker or not hasattr(attacker, 'oracle_text') or "flanking" not in attacker.oracle_text.lower():
                continue
                
            # Flanking will be handled during the block resolution
            logging.debug(f"Flanking: {attacker.name} has flanking ability")
                
        # Process bushido abilities (handled during block resolution)
        for attacker_id in gs.current_attackers:
            attacker = gs._safe_get_card(attacker_id)
            if not attacker or not hasattr(attacker, 'oracle_text'):
                continue
                
            if "bushido" in attacker.oracle_text.lower():
                logging.debug(f"Bushido: {attacker.name} has bushido ability")
    
    def _check_lethal_damage(self, damage_to_creatures, already_killed):
        """Check for lethal damage and move creatures to graveyard using improved state-based action processing."""
        return self._process_state_based_actions(damage_to_creatures, already_killed)
    
    def _process_combat_triggers(self, creatures_dealt_damage, is_first_strike=False):
        """Process triggers that happened during combat."""
        gs = self.game_state
        
        # Process each stored trigger
        for creature_id, trigger_type, context in self.combat_triggers:
            if is_first_strike:
                # Only process triggers specific to first strike damage step
                if trigger_type == "deals_damage" and context.get("is_first_strike", False):
                    gs.trigger_ability(creature_id, "DEALS_DAMAGE", context)
                elif trigger_type == "is_dealt_damage" and context.get("is_first_strike", False):
                    gs.trigger_ability(creature_id, "DEALT_DAMAGE", context)
            else:
                # Process regular damage step triggers
                if trigger_type == "deals_damage" and not context.get("is_first_strike", False):
                    gs.trigger_ability(creature_id, "DEALS_DAMAGE", context)
                elif trigger_type == "is_dealt_damage" and not context.get("is_first_strike", False):
                    gs.trigger_ability(creature_id, "DEALT_DAMAGE", context)
                
        # Generic "whenever a creature deals combat damage" triggers
        for creature_id in creatures_dealt_damage:
            # Only trigger if it matches the current damage step
            matching_trigger = any(
                trigger[0] == creature_id and 
                is_first_strike == trigger[2].get("is_first_strike", False) 
                for trigger in self.combat_triggers
            )
            
            if matching_trigger:
                gs.trigger_ability(creature_id, "DEALS_COMBAT_DAMAGE")
        
        # Clear stored triggers after processing
        self.combat_triggers.clear()
        
    def _add_combat_trigger(self, creature_id, trigger_type, context=None, is_first_strike=False):
        """Add a combat trigger to be processed later."""
        if context is None:
            context = {}
        
        # Add first strike flag to context
        context["is_first_strike"] = is_first_strike
        
        self.combat_triggers.append((creature_id, trigger_type, context))
    
    def _get_card_power(self, card, controller):
        """Get a card's current power with improved error handling."""
        try:
            if not card:
                return 0
                
            if not hasattr(card, 'power'):
                logging.warning(f"Card {getattr(card, 'name', 'Unknown')} missing 'power' attribute")
                return 0
                
            base_power = card.power
            
            # Add +1/+1 counters
            if hasattr(card, 'counters'):
                plus_counters = card.counters.get("+1/+1", 0)
                minus_counters = card.counters.get("-1/-1", 0)
                base_power += plus_counters - minus_counters
            
            # Add temporary buffs
            if hasattr(controller, 'temp_buffs') and hasattr(card, 'card_id') and card.card_id in controller.get('temp_buffs', {}):
                base_power += controller['temp_buffs'][card.card_id].get('power', 0)
            
            # Ensure minimum of 0
            return max(0, base_power)
        except Exception as e:
            logging.error(f"Error calculating power for card {getattr(card, 'name', 'Unknown')}: {e}")
            return 0  # Safe default
    
    def _get_card_toughness(self, card, controller):
        """Get a card's current toughness, accounting for counters and effects."""
        if not card or not hasattr(card, 'toughness'):
            return 0
            
        base_toughness = card.toughness
        
        # Add +1/+1 counters
        if hasattr(card, 'counters'):
            plus_counters = card.counters.get("+1/+1", 0)
            minus_counters = card.counters.get("-1/-1", 0)
            base_toughness += plus_counters - minus_counters
        
        # Add temporary buffs
        if hasattr(controller, 'temp_buffs') and card.card_id in controller['temp_buffs']:
            base_toughness += controller['temp_buffs'][card.card_id].get('toughness', 0)
        
        # Account for damage marked on the creature
        marked_damage = controller.get("damage_counters", {}).get(card.card_id, 0)
        
        # Damage doesn't reduce toughness directly in MTG, but for simplicity 
        # we return this for lethal damage checks
        return max(0, base_toughness)
        
    def _log_combat_state(self):
        """Log the current combat state for debugging."""
        gs = self.game_state
        attacker = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Log attackers
        attacker_names = [
            f"{gs._safe_get_card(aid).name} ({self._get_card_power(gs._safe_get_card(aid), attacker)}/{self._get_card_toughness(gs._safe_get_card(aid), attacker)})"
            for aid in gs.current_attackers
            if gs._safe_get_card(aid)
        ]
        logging.debug(f"COMBAT: {len(gs.current_attackers)} attackers: {', '.join(attacker_names)}")
        
        # Log blockers
        for attacker_id, blockers in gs.current_block_assignments.items():
            attacker_card = gs._safe_get_card(attacker_id)
            if not attacker_card:
                continue
                
            blocker_names = [
                f"{gs._safe_get_card(bid).name} ({self._get_card_power(gs._safe_get_card(bid), attacker)}/{self._get_card_toughness(gs._safe_get_card(bid), attacker)})"
                for bid in blockers
                if gs._safe_get_card(bid)
            ]
            logging.debug(f"COMBAT: {attacker_card.name} blocked by {len(blockers)} creatures: {', '.join(blocker_names)}")
    
    def simulate_combat(self):
        """
        Simulate combat without actually changing game state.
        Returns detailed information about expected outcomes.
        """
        try:
            gs = self.game_state
            
            # Use deep copies to avoid modifying game state
            attackers = list(gs.current_attackers)
            block_assignments = {k: list(v) for k, v in gs.current_block_assignments.items()}
            
            attacker_player = gs.p1 if gs.agent_is_p1 else gs.p2
            defender_player = gs.p2 if gs.agent_is_p1 else gs.p1
            
            # Track expected outcomes
            simulation_results = {
                "damage_to_player": 0,
                "attackers_dying": [],
                "blockers_dying": [],
                "life_gained": 0,
                "expected_value": 0.0
            }
            
            # Tracking structures for the simulation
            damage_to_creatures = {}
            damage_to_players = {"p1": 0, "p2": 0}
            creatures_dealt_damage = set()
            killed_in_first_strike = set()
            
            # Simulate first strike damage step if needed
            has_first_strike = False
            for attacker_id in attackers:
                attacker_card = gs._safe_get_card(attacker_id)
                if attacker_card and (self._has_keyword(attacker_card, "first strike") or 
                                    self._has_keyword(attacker_card, "double strike")):
                    has_first_strike = True
                    break
                    
            if not has_first_strike:
                # Check blockers for first/double strike
                for attacker_id, blockers in block_assignments.items():
                    for blocker_id in blockers:
                        blocker_card = gs._safe_get_card(blocker_id)
                        if blocker_card and (self._has_keyword(blocker_card, "first strike") or 
                                            self._has_keyword(blocker_card, "double strike")):
                            has_first_strike = True
                            break
                    if has_first_strike:
                        break
            
            # First strike damage step if needed
            if has_first_strike:
                # Process attackers with first strike
                for attacker_id in attackers:
                    self._process_attacker_damage(
                        attacker_id,
                        attacker_player,
                        defender_player,
                        damage_to_creatures,
                        damage_to_players,
                        creatures_dealt_damage,
                        killed_in_first_strike,
                        is_first_strike=True
                    )
                
                # Process blockers with first strike
                for attacker_id, blockers in block_assignments.items():
                    # Skip if attacker died from first strike
                    if attacker_id in killed_in_first_strike:
                        continue
                        
                    for blocker_id in blockers:
                        self._process_blocker_damage(
                            blocker_id,
                            attacker_id,
                            attacker_player,
                            defender_player,
                            damage_to_creatures,
                            creatures_dealt_damage,
                            killed_in_first_strike,
                            is_first_strike=True
                        )
                
                # Process creatures dying in first strike damage step
                self._check_lethal_damage(damage_to_creatures, killed_in_first_strike)
            
            # Regular damage step
            for attacker_id in attackers:
                # Skip if attacker died in first strike
                if attacker_id in killed_in_first_strike:
                    continue
                    
                self._process_attacker_damage(
                    attacker_id,
                    attacker_player,
                    defender_player,
                    damage_to_creatures,
                    damage_to_players,
                    creatures_dealt_damage,
                    killed_in_first_strike,
                    is_first_strike=False
                )
            
            # Process blocker regular damage
            for attacker_id, blockers in block_assignments.items():
                # Skip if attacker died in first strike
                if attacker_id in killed_in_first_strike:
                    continue
                    
                for blocker_id in blockers:
                    # Skip if blocker died in first strike
                    if blocker_id in killed_in_first_strike:
                        continue
                        
                    self._process_blocker_damage(
                        blocker_id,
                        attacker_id,
                        attacker_player,
                        defender_player,
                        damage_to_creatures,
                        creatures_dealt_damage,
                        killed_in_first_strike,
                        is_first_strike=False
                    )
            
            # Check for lethal damage
            all_killed = set(killed_in_first_strike)  # Start with creatures killed in first strike
            self._check_lethal_damage(damage_to_creatures, all_killed)
            
            # Populate simulation results
            defender_id = "p2" if defender_player == gs.p2 else "p1"
            simulation_results["damage_to_player"] = damage_to_players[defender_id]
            
            # Calculate life gained from lifelink
            total_life_gained = 0
            for creature_id in creatures_dealt_damage:
                creature_card = gs._safe_get_card(creature_id)
                if creature_card and self._has_keyword(creature_card, "lifelink"):
                    # Find which player controls this creature
                    controller = None
                    for player in [gs.p1, gs.p2]:
                        if creature_id in player["battlefield"]:
                            controller = player
                            break
                    
                    if controller:
                        # Calculate damage dealt by this creature
                        damage_dealt = 0
                        # For attackers with lifelink
                        if creature_id in attackers:
                            # Unblocked damage directly to player
                            blockers = block_assignments.get(creature_id, [])
                            if not blockers:
                                damage_dealt = self._get_card_power(creature_card, controller)
                            else:
                                # Damage to blockers and possibly trample
                                # This is a simplified approximation
                                damage_dealt = self._get_card_power(creature_card, controller)
                        
                        # For blockers with lifelink (approximation)
                        else:
                            for a_id, b_list in block_assignments.items():
                                if creature_id in b_list:
                                    damage_dealt = self._get_card_power(creature_card, controller)
                                    break
                        
                        total_life_gained += damage_dealt
            
            simulation_results["life_gained"] = total_life_gained
            
            # Track creatures that died
            for creature_id, damage in damage_to_creatures.items():
                creature_card = gs._safe_get_card(creature_id)
                if not creature_card:
                    continue
                    
                # Find controller
                creature_controller = None
                for player in [gs.p1, gs.p2]:
                    if creature_id in player["battlefield"]:
                        creature_controller = player
                        break
                
                if not creature_controller:
                    continue
                    
                # Check if damage is lethal
                toughness = self._get_card_toughness(creature_card, creature_controller)
                
                # Skip indestructible creatures
                if self._has_keyword(creature_card, "indestructible"):
                    continue
                    
                if damage >= toughness:
                    # Determine if this is an attacker or blocker
                    if creature_id in attackers:
                        simulation_results["attackers_dying"].append(creature_id)
                    else:
                        # Check if it's a blocker
                        is_blocker = False
                        for a_id, b_list in block_assignments.items():
                            if creature_id in b_list:
                                is_blocker = True
                                break
                        
                        if is_blocker:
                            simulation_results["blockers_dying"].append(creature_id)
            
            # Calculate expected value of this combat
            damage_value = simulation_results["damage_to_player"] * 0.2
            my_creatures_lost = len(simulation_results["attackers_dying"])
            their_creatures_lost = len(simulation_results["blockers_dying"])
            life_gained = simulation_results["life_gained"]
            
            # Evaluate exchange
            creature_exchange_value = their_creatures_lost - my_creatures_lost
            simulation_results["expected_value"] = damage_value + creature_exchange_value * 0.4 + life_gained * 0.1
            
            return simulation_results
            
        except Exception as e:
            logging.error(f"Error in combat simulation: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            
            # Return default values
            return {
                "damage_to_player": 0,
                "attackers_dying": [],
                "blockers_dying": [],
                "life_gained": 0,
                "expected_value": 0.0
            }
        
    def evaluate_potential_blocks(self, attacker_id, potential_blockers):
        """Evaluate different blocking configurations for an attacker."""
        import itertools
        
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card:
            return []
            
        # Get attacker properties
        attacker_player = gs.p1 if gs.agent_is_p1 else gs.p2
        defender_player = gs.p2 if gs.agent_is_p1 else gs.p1
        attacker_power = self._get_card_power(attacker_card, attacker_player)
        attacker_has_trample = self._has_keyword(attacker_card, "trample")
        attacker_has_deathtouch = self._has_keyword(attacker_card, "deathtouch")
        
        # Evaluate no blockers option
        blocking_options = [{
            'blocker_ids': [],
            'value': -1.0 * attacker_power,  # Negative value proportional to damage taken
            'attacker_dies': False,
            'damage_prevented': 0
        }]
        
        # Check if attacker has evasion abilities that restrict blocking
        has_flying = self._has_keyword(attacker_card, "flying")
        has_fear = self._has_keyword(attacker_card, "fear")
        has_intimidate = self._has_keyword(attacker_card, "intimidate")
        has_menace = self._has_keyword(attacker_card, "menace")
        has_unblockable = any(x in attacker_card.oracle_text.lower() 
                             for x in ["can't be blocked", "unblockable"]) if hasattr(attacker_card, "oracle_text") else False
        
        # Filter valid blockers based on attacker's evasion abilities
        valid_blockers = []
        for blocker_id in potential_blockers:
            blocker_card = gs._safe_get_card(blocker_id)
            if not blocker_card:
                continue
                
            # Flying check
            if has_flying and not (self._has_keyword(blocker_card, "flying") or self._has_keyword(blocker_card, "reach")):
                continue
                
            # Fear check
            if has_fear and not (self._has_keyword(blocker_card, "artifact") or self._has_keyword(blocker_card, "black")):
                continue
                
            # Intimidate check
            if has_intimidate:
                # Would check color sharing here
                continue
                
            # Unblockable check
            if has_unblockable:
                continue
                
            valid_blockers.append(blocker_id)
        
        # If no valid blockers or unblockable, return just the default option
        if not valid_blockers or has_unblockable:
            return blocking_options
            
        # Menace requires at least 2 blockers
        if has_menace and len(valid_blockers) < 2:
            return blocking_options
            
        # Generate valid blocker combinations
        max_blockers = min(len(valid_blockers), 3)  # Limit to 3 blockers for performance
        if has_menace:
            # Menace requires at least 2 blockers
            min_blockers = 2
        else:
            min_blockers = 1
            
        # Single blocker options
        if min_blockers == 1:
            for blocker_id in valid_blockers:
                blocker_card = gs._safe_get_card(blocker_id)
                if not blocker_card:
                    continue
                    
                blocker_power = self._get_card_power(blocker_card, defender_player)
                blocker_toughness = self._get_card_toughness(blocker_card, defender_player)
                
                # Calculate outcomes
                attacker_dies = blocker_power >= attacker_card.toughness or self._has_keyword(blocker_card, "deathtouch")
                blocker_dies = attacker_power >= blocker_toughness or attacker_has_deathtouch
                
                # Calculate damage prevention
                damage_prevented = attacker_power
                if attacker_has_trample:
                    trample_damage = max(0, attacker_power - blocker_toughness)
                    damage_prevented = attacker_power - trample_damage
                
                # Evaluate block
                if attacker_dies and not blocker_dies:
                    # Ideal: kill attacker without losing blocker
                    value = 2.0 + blocker_power  # Better with higher power blocker
                elif attacker_dies and blocker_dies:
                    # Trade: both die
                    value = 1.0
                    
                    # Consider mana values for trade evaluation
                    if hasattr(attacker_card, 'cmc') and hasattr(blocker_card, 'cmc'):
                        if attacker_card.cmc > blocker_card.cmc:
                            value += 0.5  # Good trade
                        elif attacker_card.cmc < blocker_card.cmc:
                            value -= 0.5  # Bad trade
                elif not attacker_dies and not blocker_dies:
                    # Chump block that survives
                    value = damage_prevented * 0.2
                else:
                    # Chump block: lose blocker without killing attacker
                    value = damage_prevented * 0.15 - 0.5
                    
                    # If attacker has high power, chump blocking is better
                    if attacker_power >= 4:
                        value += attacker_power * 0.1
                
                blocking_options.append({
                    'blocker_ids': [blocker_id],
                    'value': value,
                    'attacker_dies': attacker_dies,
                    'blocker_dies': blocker_dies,
                    'damage_prevented': damage_prevented
                })
        
        # Multi-blocker options
        for num_blockers in range(max(2, min_blockers), max_blockers + 1):
            for blocker_combo in itertools.combinations(valid_blockers, num_blockers):
                total_power = sum(self._get_card_power(gs._safe_get_card(bid), defender_player) for bid in blocker_combo if gs._safe_get_card(bid))
                total_toughness = sum(self._get_card_toughness(gs._safe_get_card(bid), defender_player) for bid in blocker_combo if gs._safe_get_card(bid))
                
                # Determine combat outcomes
                attacker_dies = total_power >= attacker_card.toughness or any(self._has_keyword(gs._safe_get_card(bid), "deathtouch") for bid in blocker_combo if gs._safe_get_card(bid))
                
                # Count how many blockers will die
                blockers_dying = 0
                for bid in blocker_combo:
                    blocker_card = gs._safe_get_card(bid)
                    if not blocker_card:
                        continue
                        
                    blocker_toughness = self._get_card_toughness(blocker_card, defender_player)
                    if attacker_power >= blocker_toughness or attacker_has_deathtouch:
                        blockers_dying += 1
                
                # Calculate damage prevention
                damage_prevented = attacker_power
                if attacker_has_trample:
                    trample_damage = max(0, attacker_power - total_toughness)
                    damage_prevented = attacker_power - trample_damage
                
                # Evaluate block
                if attacker_dies:
                    value = 1.5 - (blockers_dying * 0.5)  # Good to kill attacker, subtract for each blocker lost
                else:
                    value = (damage_prevented * 0.15) - (blockers_dying * 0.75)  # Less valuable if not killing attacker
                
                blocking_options.append({
                    'blocker_ids': list(blocker_combo),
                    'value': value,
                    'attacker_dies': attacker_dies,
                    'blockers_dying': blockers_dying,
                    'damage_prevented': damage_prevented
                })
        
        # Sort by value
        blocking_options.sort(key=lambda x: x['value'], reverse=True)
        
        return blocking_options