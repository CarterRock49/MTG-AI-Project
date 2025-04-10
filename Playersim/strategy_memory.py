import logging
import numpy as np
import random
import pickle
import os
import time
import math

class StrategyMemory:
    """Memory system to record successful action sequences and game patterns."""
    
    def __init__(self, memory_file="strategy_memory.pkl", max_size=50000):
        self.memory_file = memory_file
        self.max_size = max_size
        self.strategies = {}  # Pattern -> {count, reward, success_rate}
        self.action_sequences = []  # List of (sequence, reward) tuples
        self.load_memory()
    
    def load_memory(self):
        """Load strategy memory from file if it exists."""
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, 'rb') as f:
                    data = pickle.load(f)
                    self.strategies = data.get('strategies', {})
                    self.action_sequences = data.get('action_sequences', [])
                logging.info(f"Loaded {len(self.strategies)} strategy patterns from memory")
            except Exception as e:
                logging.error(f"Error loading strategy memory: {e}")
                self.strategies = {}
                self.action_sequences = []
        else:
            logging.info("No strategy memory file found, starting with empty memory")
            
        
    def _enhance_strategy_memory(self):
        """
        Enhanced strategy memory consolidation with configurable parameters 
        and better preservation of valuable strategies.
        """
        try:
            # Only process if we have a substantial amount of data
            if len(self.strategies) < 50:
                return
            
            # Configurable parameters
            similarity_threshold = 0.7  # Threshold for considering strategies similar
            cluster_size_min = 3  # Minimum cluster size to consider consolidation
            success_threshold = 0.5  # Minimum success rate to preserve
            
            # Group similar strategies
            strategy_clusters = {}
            processed_patterns = set()
            
            for pattern, strategy in self.strategies.items():
                if pattern in processed_patterns:
                    continue
                
                # Create a new cluster
                cluster = {pattern: strategy}
                processed_patterns.add(pattern)
                
                # Find similar patterns
                for other_pattern, other_strategy in self.strategies.items():
                    if other_pattern in processed_patterns:
                        continue
                    
                    # Check pattern similarity
                    similarity = self._pattern_similarity(pattern, other_pattern)
                    
                    # If patterns are similar, add to cluster
                    if similarity > similarity_threshold:
                        cluster[other_pattern] = other_strategy
                        processed_patterns.add(other_pattern)
                
                # Calculate cluster-level metrics
                if len(cluster) > 1:
                    # Weight success rate by count and recency
                    weighted_success = 0
                    total_weight = 0
                    for p, s in cluster.items():
                        recency_weight = 1.0
                        if 'timestamp' in s:
                            age_hours = (time.time() - s['timestamp']) / 3600
                            recency_weight = max(0.5, math.exp(-age_hours / 24))  # Decay based on age
                        
                        weight = s['count'] * recency_weight
                        weighted_success += s['success_rate'] * weight
                        total_weight += weight
                    
                    cluster_success_rate = weighted_success / total_weight if total_weight > 0 else 0
                    cluster_count = sum(s['count'] for s in cluster.values())
                    
                    # Choose the most representative pattern based on success and count
                    representative_pattern = max(
                        cluster.items(), 
                        key=lambda x: (x[1]['success_rate'] * math.sqrt(x[1]['count'])) * 
                                    (1 + 0.2 * ('timestamp' in x[1] and (time.time() - x[1]['timestamp']) < 86400))
                    )[0]
                    
                    strategy_clusters[representative_pattern] = {
                        'strategies': cluster,
                        'cluster_success_rate': cluster_success_rate,
                        'cluster_count': cluster_count
                    }
                else:
                    # Single-item clusters are preserved as-is
                    strategy_clusters[pattern] = {
                        'strategies': cluster,
                        'cluster_success_rate': strategy['success_rate'],
                        'cluster_count': strategy['count']
                    }
            
            # Prune and consolidate strategies
            new_strategies = {}
            for rep_pattern, cluster_info in strategy_clusters.items():
                # Keep all good-performing clusters regardless of size
                if cluster_info['cluster_success_rate'] > success_threshold:
                    # For larger clusters, merge data
                    if len(cluster_info['strategies']) >= cluster_size_min:
                        # Create a new consolidated strategy
                        merged_strategy = {
                            'count': cluster_info['cluster_count'],
                            'reward': sum(s['reward'] * s['count'] for p, s in cluster_info['strategies'].items()) / 
                                    cluster_info['cluster_count'],
                            'success_rate': cluster_info['cluster_success_rate'],
                            'timestamp': max(s.get('timestamp', 0) for s in cluster_info['strategies'].values())
                        }
                        new_strategies[rep_pattern] = merged_strategy
                        logging.debug(f"Merged {len(cluster_info['strategies'])} similar patterns with success rate {cluster_info['cluster_success_rate']:.2f}")
                    else:
                        # For smaller clusters, keep the best strategy
                        best_pattern, best_strategy = max(
                            cluster_info['strategies'].items(),
                            key=lambda x: x[1]['success_rate'] * x[1]['count']
                        )
                        new_strategies[best_pattern] = best_strategy
                else:
                    # For underperforming clusters, only keep if it has high count (might be improving)
                    if cluster_info['cluster_count'] > 10:
                        best_pattern, best_strategy = max(
                            cluster_info['strategies'].items(),
                            key=lambda x: x[1]['success_rate'] * x[1]['count']
                        )
                        new_strategies[best_pattern] = best_strategy
            
            # Preserve a portion of the most recent strategies regardless of performance
            # This helps maintain exploration of new strategies
            unclustered_strategies = {p: s for p, s in self.strategies.items() if p not in processed_patterns}
            if unclustered_strategies:
                recent_strategies = sorted(
                    unclustered_strategies.items(),
                    key=lambda x: x[1].get('timestamp', 0),
                    reverse=True
                )[:max(5, len(unclustered_strategies) // 10)]
                
                for pattern, strategy in recent_strategies:
                    new_strategies[pattern] = strategy
            
            # Update strategies
            self.strategies = new_strategies
            
            logging.info(f"Enhanced strategy memory: reduced from {len(processed_patterns)} to {len(new_strategies)} strategies")
        
        except Exception as e:
            logging.error(f"Error in strategy memory enhancement: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
    
    def save_memory(self):
        """
        Enhanced save method that includes periodic memory enhancement.
        """
        try:
            # Periodically enhance memory before saving
            if random.random() < 0.2:  # 20% chance of enhancement
                self._enhance_strategy_memory()
            
            # Standard save logic
            data = {
                'strategies': self.strategies,
                'action_sequences': self.action_sequences
            }
            
            # Ensure save directory exists
            dir_name = os.path.dirname(self.memory_file) 
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            
            with open(self.memory_file, 'wb') as f:
                pickle.dump(data, f)
            
            logging.info(f"Saved {len(self.strategies)} strategy patterns to {self.memory_file}")
        
        except Exception as e:
            logging.error(f"Error saving strategy memory: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
    
    def update_strategy(self, pattern, reward):
        """Update a strategy pattern with new reward information."""
        current_time = time.time()
        
        if pattern not in self.strategies:
            self.strategies[pattern] = {
                'count': 1,
                'reward': reward,
                'success_rate': 1.0 if reward > 0 else 0.0,
                'timestamp': current_time
            }
        else:
            entry = self.strategies[pattern]
            entry['count'] += 1
            # Use exponential moving average to update reward
            entry['reward'] = 0.9 * entry['reward'] + 0.1 * reward
            # Update success rate
            success = 1.0 if reward > 0 else 0.0
            entry['success_rate'] = ((entry['count'] - 1) * entry['success_rate'] + success) / entry['count']
            # Update timestamp
            entry['timestamp'] = current_time
        
        # Periodically prune memory (new addition)
        if len(self.strategies) > self.max_size * 0.8 or random.random() < 0.1:  # Prune at 80% capacity or 10% chance
            self.prune_memory()
    
    def record_action_sequence(self, action_sequence, reward, game_state=None):
        """
        Record a successful action sequence with improved subsequence tracking, 
        credit assignment, and structured representation.
        
        Args:
            action_sequence: List of actions performed (action indices)
            reward: Final reward received
            game_state: Optional game state reference for extracting context
        """
        # Only record sequences with positive rewards
        if reward <= 0:
            return
        
        # Convert to structured representation if we have access to the game state
        structured_sequence = []
        try:
            if hasattr(self, 'game_state') and self.game_state:
                gs = self.game_state
                for action_idx in action_sequence:
                    action_type, param = gs.action_handler.get_action_info(action_idx)
                    
                    # Get the active player
                    me = gs.p1 if gs.agent_is_p1 else gs.p2
                    opp = gs.p2 if gs.agent_is_p1 else gs.p1
                    
                    # Create rich context for this action
                    structured_sequence.append({
                        'action_idx': action_idx,
                        'action_type': action_type,
                        'param': param,
                        'phase': gs.phase,
                        'turn': gs.turn,
                        'board_context': {
                            'my_creatures': sum(1 for cid in me["battlefield"] 
                                        if gs._safe_get_card(cid) and 
                                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                                        'creature' in gs._safe_get_card(cid).card_types),
                            'opp_creatures': sum(1 for cid in opp["battlefield"] 
                                            if gs._safe_get_card(cid) and 
                                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                                            'creature' in gs._safe_get_card(cid).card_types),
                            'my_life': me['life'],
                            'opp_life': opp['life'],
                            'my_hand_size': len(me['hand']),
                            'opp_hand_size': len(opp['hand']),
                            'stack_size': len(gs.stack)
                        }
                    })
            else:
                # Fallback to simple indices
                structured_sequence = [{'action_idx': idx} for idx in action_sequence]
        except Exception as e:
            logging.warning(f"Error creating structured action sequence: {e}")
            # Fallback to simple indices
            structured_sequence = [{'action_idx': idx} for idx in action_sequence]
            
        # Store the full sequence
        self.action_sequences.append((structured_sequence.copy(), reward))
        
        # Generate meaningful subsequences (focus on more recent actions)
        subsequences = []
        
        sequence_length = len(structured_sequence)
        if sequence_length > 1:
            # Last 5 actions (most influential)
            if sequence_length >= 5:
                subsequences.append((structured_sequence[-5:], reward * 0.9))
            
            # Last 3 actions
            if sequence_length >= 3:
                subsequences.append((structured_sequence[-3:], reward * 0.8))
            
            # Last 2 actions
            subsequences.append((structured_sequence[-2:], reward * 0.7))
            
            # Last action only
            subsequences.append(([structured_sequence[-1]], reward * 0.6))
            
            # Add all subsequences to memory
            for subseq, subseq_reward in subsequences:
                self.action_sequences.append((subseq, subseq_reward))
        
        # Keep only the top max_size sequences by reward
        if len(self.action_sequences) > self.max_size:
            self.action_sequences.sort(key=lambda x: x[1], reverse=True)
            self.action_sequences = self.action_sequences[:self.max_size]
            
    def identify_strategic_concepts(self):
        """
        Analyze action sequences to identify higher-level strategic concepts.
        This helps with applying learned strategies more generally.
        
        Returns:
            dict: Identified strategic concepts and their effectiveness
        """
        try:
            # Initialize concept tracking
            concepts = {
                'aggro': {'count': 0, 'reward': 0.0},
                'control': {'count': 0, 'reward': 0.0},
                'midrange': {'count': 0, 'reward': 0.0},
                'tempo': {'count': 0, 'reward': 0.0},
                'combo': {'count': 0, 'reward': 0.0}
            }
            
            for sequence, reward in self.action_sequences:
                if reward <= 0:
                    continue
                    
                # Initialize concept scores for this sequence
                sequence_concepts = {
                    'aggro': 0,
                    'control': 0,
                    'midrange': 0,
                    'tempo': 0,
                    'combo': 0
                }
                
                # Analyze the sequence for concept indicators
                for action in sequence:
                    # Skip if we don't have detailed action info
                    if not isinstance(action, dict):
                        continue
                        
                    action_type = action.get('action_type', '')
                    param = action.get('param', None)
                    
                    # Aggro indicators
                    if action_type in ['DECLARE_ATTACKER', 'PLAY_CREATURE']:
                        sequence_concepts['aggro'] += 1
                    
                    # Control indicators
                    if any(term in action_type for term in ['COUNTER', 'DESTROY', 'EXILE']):
                        sequence_concepts['control'] += 1
                    
                    # Midrange indicators - playing efficient creatures and removal
                    if action_type == 'PLAY_CREATURE' and action.get('board_context', {}).get('turn', 0) >= 3:
                        sequence_concepts['midrange'] += 1
                    
                    # Tempo indicators - bouncing opponent's permanents, tapping them
                    if any(term in action_type for term in ['RETURN', 'TAP']):
                        sequence_concepts['tempo'] += 1
                    
                    # Combo indicators - playing multiple spells in one turn
                    same_turn_count = sum(1 for a in sequence if isinstance(a, dict) and 
                                        a.get('board_context', {}).get('turn', 0) == 
                                        action.get('board_context', {}).get('turn', 0))
                    if same_turn_count >= 3:
                        sequence_concepts['combo'] += 1
                
                # Determine the dominant concept for this sequence
                if sum(sequence_concepts.values()) > 0:
                    dominant_concept = max(sequence_concepts.items(), key=lambda x: x[1])[0]
                    concepts[dominant_concept]['count'] += 1
                    concepts[dominant_concept]['reward'] += reward
            
            # Calculate average reward for each concept
            for concept in concepts:
                if concepts[concept]['count'] > 0:
                    concepts[concept]['avg_reward'] = concepts[concept]['reward'] / concepts[concept]['count']
                else:
                    concepts[concept]['avg_reward'] = 0.0
            
            return concepts
        
        except Exception as e:
            logging.error(f"Error identifying strategic concepts: {str(e)}")
            return {}
            
    def save_memory_async(self):
        """Save strategy memory asynchronously to avoid blocking game flow."""
        try:
            import threading
            
            # Create a thread for saving
            save_thread = threading.Thread(target=self._save_memory_worker)
            save_thread.daemon = True  # Thread will exit when main program exits
            save_thread.start()
            
            logging.debug("Started asynchronous memory save")
            return True
        except Exception as e:
            logging.error(f"Error starting async save: {str(e)}")
            # Fall back to synchronous save
            return self.save_memory()

        
    def _save_memory_worker(self):
            """Worker function for asynchronous memory saving."""
            try:
                # Periodically enhance memory before saving
                if random.random() < 0.2:  # 20% chance of enhancement
                    self._enhance_strategy_memory()

                # Create a copy of the data to avoid modification during saving
                import copy
                data = {
                    'strategies': copy.deepcopy(self.strategies),
                    'action_sequences': copy.deepcopy(self.action_sequences)
                }

                # Ensure save directory exists
                import os
                dir_name = os.path.dirname(self.memory_file)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)

                # Try different approaches to save the file
                temp_file = None # Initialize temp_file
                try:
                    # First try: use temp file for atomicity/permissions
                    import tempfile
                    import shutil
                    with tempfile.NamedTemporaryFile(mode='wb', delete=False, dir=dir_name, suffix=".pkl.tmp") as f:
                        temp_file = f.name
                        pickle.dump(data, f)
                    # Rename/replace atomically if possible, fallback to copy+remove
                    try:
                        os.replace(temp_file, self.memory_file) # Atomic rename/replace if supported
                    except OSError: # Fallback for cross-device links or other errors
                        shutil.copy2(temp_file, self.memory_file)
                        try: os.remove(temp_file) # Clean up temp file
                        except OSError: pass # Ignore if removal fails
                    logging.info(f"Save completed via temp file: {len(self.strategies)} strategy patterns to {self.memory_file}")
                    return True
                except PermissionError as pe:
                    # Log specific permission error
                    logging.error(f"Permission error saving to {self.memory_file}: {pe}. Attempting direct save...")
                    # Second try: direct write (less safe)
                    try:
                        with open(self.memory_file, 'wb') as f:
                            pickle.dump(data, f)
                        logging.info(f"Direct save completed after permission issue: {len(self.strategies)} strategy patterns to {self.memory_file}")
                        return True
                    except Exception as direct_save_e:
                        logging.error(f"Direct save failed after permission issue: {str(direct_save_e)}")
                        if temp_file and os.path.exists(temp_file):
                            logging.warning(f"Data saved to temporary file: {temp_file}. Manual copy to {self.memory_file} might be needed.")
                            # Keep the temp file if direct save also fails
                        return False
                except Exception as e:
                    logging.error(f"Error during temp file save: {str(e)}")
                    if temp_file and os.path.exists(temp_file): # Clean up temp file on other errors
                        try: os.remove(temp_file)
                        except OSError: pass
                    return False
            except Exception as e:
                logging.error(f"Error in async memory save worker: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
                return False
    
    def extract_strategy_pattern(self, game_state, detailed=False):
        """
        Extract a comprehensive strategic pattern from the current game state.
        
        Args:
            game_state: The current game state
            detailed: Whether to include more detailed context
            
        Returns:
            tuple: A pattern tuple that represents the game state strategically
        """
        try:
            gs = game_state
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1
            
            # Board state analysis
            my_creatures = [cid for cid in me["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types]
            
            opp_creatures = [cid for cid in opp["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types]
            
            # Calculate power and toughness
            my_power = sum(gs._safe_get_card(cid).power 
                        for cid in my_creatures 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'power'))
            
            my_toughness = sum(gs._safe_get_card(cid).toughness 
                            for cid in my_creatures 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'toughness'))
            
            opp_power = sum(gs._safe_get_card(cid).power 
                        for cid in opp_creatures 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'power'))
            
            opp_toughness = sum(gs._safe_get_card(cid).toughness 
                            for cid in opp_creatures 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'toughness'))
            
            # Advantage abstractions
            power_advantage_category = 0
            if my_power > opp_power + 5:
                power_advantage_category = 2  # Strong advantage
            elif my_power > opp_power:
                power_advantage_category = 1  # Slight advantage
            elif my_power < opp_power - 5:
                power_advantage_category = -2  # Strong disadvantage
            elif my_power < opp_power:
                power_advantage_category = -1  # Slight disadvantage
            
            # Board presence advantage
            board_advantage_category = 0
            if len(my_creatures) > len(opp_creatures) + 2:
                board_advantage_category = 2
            elif len(my_creatures) > len(opp_creatures):
                board_advantage_category = 1
            elif len(my_creatures) < len(opp_creatures) - 2:
                board_advantage_category = -2
            elif len(my_creatures) < len(opp_creatures):
                board_advantage_category = -1
            
            # Life total advantage
            life_advantage_category = 0
            life_diff = me["life"] - opp["life"]
            if life_diff > 10:
                life_advantage_category = 2
            elif life_diff > 0:
                life_advantage_category = 1
            elif life_diff < -10:
                life_advantage_category = -2
            elif life_diff < 0:
                life_advantage_category = -1
            
            # Card advantage
            card_advantage_category = 0
            hand_diff = len(me["hand"]) - len(opp["hand"])
            if hand_diff > 2:
                card_advantage_category = 2
            elif hand_diff > 0:
                card_advantage_category = 1
            elif hand_diff < -2:
                card_advantage_category = -2
            elif hand_diff < 0:
                card_advantage_category = -1
            
            # Mana development
            my_lands = [cid for cid in me["battlefield"] 
                    if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'type_line') and 
                    'land' in gs._safe_get_card(cid).type_line]
            
            opp_lands = [cid for cid in opp["battlefield"] 
                    if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'type_line') and 
                    'land' in gs._safe_get_card(cid).type_line]
            
            mana_advantage_category = 0
            if len(my_lands) > len(opp_lands) + 2:
                mana_advantage_category = 2
            elif len(my_lands) > len(opp_lands):
                mana_advantage_category = 1
            elif len(my_lands) < len(opp_lands) - 2:
                mana_advantage_category = -2
            elif len(my_lands) < len(opp_lands):
                mana_advantage_category = -1
            
            # Game stage determination
            game_stage = 0  # Early game
            if gs.turn >= 8:
                game_stage = 2  # Late game
            elif gs.turn >= 4:
                game_stage = 1  # Mid game
            
            # Phase category (simplified)
            phase_category = 0  # Main phases
            if gs.phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, gs.PHASE_COMBAT_DAMAGE]:
                phase_category = 1  # Combat phases
            elif gs.phase in [gs.PHASE_END_STEP, gs.PHASE_CLEANUP]:
                phase_category = 2  # End phases
            
            # Stack status
            stack_status = 0
            if gs.stack:
                stack_status = 1
                
                # Check if our spell is on top
                if gs.stack and len(gs.stack) > 0:
                    top_item = gs.stack[-1]
                    if isinstance(top_item, tuple) and len(top_item) >= 3:
                        _, _, controller = top_item[:3]
                        if controller == me:
                            stack_status = 2  # Our spell on top
            
            # Analyze hands for threat assessment
            have_removal = False
            have_combat_trick = False
            have_big_threat = False
            
            for card_id in me["hand"]:
                card = gs._safe_get_card(card_id)
                if not card:
                    continue
                    
                # Check for removal
                if hasattr(card, 'oracle_text') and any(term in card.oracle_text.lower() 
                                                for term in ['destroy', 'exile', 'damage to']):
                    have_removal = True
                    
                # Check for combat tricks
                if ('instant' in getattr(card, 'card_types', []) and 
                    hasattr(card, 'oracle_text') and 
                    any(term in card.oracle_text.lower() for term in ['+', 'gets +', 'target creature'])):
                    have_combat_trick = True
                    
                # Check for big threats
                if ('creature' in getattr(card, 'card_types', []) and 
                    hasattr(card, 'power') and card.power >= 4):
                    have_big_threat = True
            
            # Threat level on board
            threat_level = 0
            
            # Check if opponent has threatening creatures
            threatening_creatures = sum(1 for cid in opp_creatures 
                                    if gs._safe_get_card(cid) and 
                                    hasattr(gs._safe_get_card(cid), 'power') and 
                                    gs._safe_get_card(cid).power >= 3)
            
            if threatening_creatures >= 2:
                threat_level = 2  # High threat
            elif threatening_creatures > 0:
                threat_level = 1  # Moderate threat
                
            # Check if opponent can kill us soon
            potential_damage = sum(gs._safe_get_card(cid).power 
                                for cid in opp_creatures 
                                if gs._safe_get_card(cid) and 
                                hasattr(gs._safe_get_card(cid), 'power'))
            
            if potential_damage >= me["life"]:
                threat_level = 3  # Critical threat - lethal on board
            elif potential_damage >= me["life"] // 2:
                threat_level = max(threat_level, 2)  # High threat - significant damage potential
            
            # Create the base pattern tuple
            pattern = (
                game_stage,                # 0: Game stage (early/mid/late)
                board_advantage_category,  # 1: Board presence advantage
                power_advantage_category,  # 2: Power advantage
                life_advantage_category,   # 3: Life total advantage
                card_advantage_category,   # 4: Card advantage
                mana_advantage_category,   # 5: Mana development advantage
                phase_category,            # 6: Phase category (main/combat/end)
                stack_status,              # 7: Stack status (empty/opponent/ours)
                min(len(my_creatures), 5),  # 8: My creature count (capped at 5)
                min(len(opp_creatures), 5),  # 9: Opponent creature count (capped at 5)
                int(have_removal),         # 10: Have removal in hand
                int(have_combat_trick),    # 11: Have combat trick in hand
                int(have_big_threat),      # 12: Have big threat in hand
                threat_level               # 13: Threat level on board
            )
            
            if detailed:
                # Add more detailed info for deeper analysis (not used for pattern matching)
                detailed_pattern = pattern + (
                    me["life"],            # 14: Exact life total
                    opp["life"],           # 15: Opponent life total 
                    len(me["hand"]),       # 16: Hand size
                    len(opp["hand"]),      # 17: Opponent hand size
                    len(my_lands),         # 18: Land count
                    my_power,              # 19: Total power
                    my_toughness,          # 20: Total toughness
                    gs.turn                # 21: Exact turn number
                )
                return detailed_pattern
            
            return pattern
        
        except Exception as e:
            logging.error(f"Error extracting strategy pattern: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            # Return a default pattern
            return (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            
    def _pattern_similarity(self, pattern1, pattern2, tolerance=0.7):
        """
        Enhanced similarity calculation between two patterns with contextual weighting
        and adaptive features.
        
        Args:
            pattern1: First pattern to compare
            pattern2: Second pattern to compare
            tolerance: Similarity threshold (default 0.7)
        
        Returns:
            float: Similarity score between 0 and 1
        """
        if len(pattern1) != len(pattern2):
            return 0.0
        
        # Define context-aware weights for different pattern elements
        # More important elements get higher weights
        weights = {
            0: 2.0,   # Game stage (crucial - early/mid/late game strategy differs greatly)
            1: 1.5,   # Board advantage category
            2: 1.5,   # Power advantage category
            3: 1.8,   # Life advantage category
            4: 1.2,   # Card advantage category
            5: 1.0,   # Mana advantage category
            6: 1.0,   # Phase category
            7: 1.0,   # Stack status
            8: 1.5,   # My creature count
            9: 1.2    # Opponent creature count
        }
        
        # Calculate component-wise similarity with fuzzy matching
        component_similarity = []
        
        for i, (a, b) in enumerate(zip(pattern1, pattern2)):
            # Exact match
            if a == b:
                component_similarity.append(1.0)
                continue
            
            # Game stage: only similar if adjacent (early/mid or mid/late)
            if i == 0:
                component_similarity.append(0.5 if abs(a - b) == 1 else 0.0)
                continue
            
            # Advantage categories (board, power, life, card, mana)
            if i in [1, 2, 3, 4, 5]:
                # Same direction of advantage
                if (a > 0 and b > 0) or (a < 0 and b < 0) or (a == 0 and b == 0):
                    # For exact magnitude match, high similarity
                    if a == b:
                        component_similarity.append(1.0)
                    # For close magnitudes, good similarity
                    elif abs(a - b) == 1:
                        component_similarity.append(0.8)
                    # For different magnitudes but same direction, moderate similarity
                    else:
                        component_similarity.append(0.6)
                # Different direction but one is neutral
                elif a == 0 or b == 0:
                    component_similarity.append(0.3)
                # Opposite directions
                else:
                    component_similarity.append(0.0)
                continue
            
            # Phase category
            if i == 6:
                # All main phases are similar
                if a == 0 and b == 0:
                    component_similarity.append(1.0)
                # All combat phases are similar
                elif a == 1 and b == 1:
                    component_similarity.append(1.0)
                # All end phases are similar
                elif a == 2 and b == 2:
                    component_similarity.append(1.0)
                # Main and combat can be somewhat similar
                elif (a == 0 and b == 1) or (a == 1 and b == 0):
                    component_similarity.append(0.4)
                # Main and end can be somewhat similar
                elif (a == 0 and b == 2) or (a == 2 and b == 0):
                    component_similarity.append(0.3)
                # Combat and end are less similar
                else:
                    component_similarity.append(0.2)
                continue
            
            # Stack status
            if i == 7:
                # Empty stack similar to empty stack
                if a == 0 and b == 0:
                    component_similarity.append(1.0)
                # Our spell on stack similar to our spell on stack
                elif a == 2 and b == 2:
                    component_similarity.append(1.0)
                # Opponent spell on stack similar to opponent spell on stack
                elif a == 1 and b == 1:
                    component_similarity.append(1.0)
                # Any stack vs empty stack
                elif (a == 0 and b > 0) or (a > 0 and b == 0):
                    component_similarity.append(0.2)
                # Different non-empty stack states
                else:
                    component_similarity.append(0.1)
                continue
            
            # Creature counts (my creatures, opponent creatures)
            if i in [8, 9]:
                # Identical counts
                if a == b:
                    component_similarity.append(1.0)
                # Very close counts
                elif abs(a - b) == 1:
                    component_similarity.append(0.8)
                # Moderately close
                elif abs(a - b) == 2:
                    component_similarity.append(0.5)
                # Somewhat different
                elif abs(a - b) <= 4:
                    component_similarity.append(0.3)
                # Very different
                else:
                    component_similarity.append(0.0)
                continue
            
            # Default fallback for any other elements
            diff_ratio = 1.0 - (abs(a - b) / max(5.0, max(abs(a), abs(b))))
            component_similarity.append(max(0.0, diff_ratio))
        
        # Calculate weighted sum of similarities
        total_weight = sum(weights.values())
        weighted_similarity = sum(weights.get(i, 1.0) * c for i, c in enumerate(component_similarity)) / total_weight
        
        # Game state context analysis
        context_bonus = 0.0
        
        # Critical contexts that should strongly influence similarity:
        
        # 1. Board position context (very important)
        # If both patterns represent similar board positions
        board_position_match = all(
            (pattern1[i] > 0 and pattern2[i] > 0) or 
            (pattern1[i] < 0 and pattern2[i] < 0) or
            (pattern1[i] == 0 and pattern2[i] == 0)
            for i in [1, 2]  # Board and power advantage
        )
        
        # 2. Game stage + resources context
        # Similar game stages with similar resource situations
        resource_context_match = (
            abs(pattern1[0] - pattern2[0]) <= 1 and  # Similar game stage
            all((pattern1[i] > 0 and pattern2[i] > 0) or 
                (pattern1[i] < 0 and pattern2[i] < 0) or
                (pattern1[i] == 0 and pattern2[i] == 0)
                for i in [4, 5])  # Card and mana advantage
        )
        
        # 3. Life pressure context
        # Similar life pressure situations
        life_context_match = (
            (pattern1[3] > 0 and pattern2[3] > 0) or 
            (pattern1[3] < 0 and pattern2[3] < 0) or
            (pattern1[3] == 0 and pattern2[3] == 0)
        )
        
        # Apply context bonuses
        if board_position_match:
            context_bonus += 0.15
        if resource_context_match:
            context_bonus += 0.1
        if life_context_match:
            context_bonus += 0.05
        
        # Extra bonus if all three contexts match
        if board_position_match and resource_context_match and life_context_match:
            context_bonus += 0.1
        
        # Apply context bonus, but cap at 1.0
        final_similarity = min(1.0, weighted_similarity + context_bonus)
        
        return final_similarity

    
    def get_suggested_action(self, game_state, valid_actions, exploration_rate=None, for_mcts=False):
        """
        Get a suggested action with adaptive exploration/exploitation balance.
        
        Args:
            game_state: Current game state
            valid_actions: List or array of valid action indices
            exploration_rate: Probability of exploration (if None, will be adaptive)get_suggested_action
            for_mcts: Whether this is being called during Monte Carlo Tree Search
            
        Returns:
            int or None: Suggested action index, or (action_index, value) tuple if for_mcts=True
        """
        try:
            # Convert NumPy array to list of valid actions if needed
            if hasattr(valid_actions, 'nonzero'):
                valid_actions = valid_actions.nonzero()[0].tolist()
            elif hasattr(valid_actions, 'any') and not valid_actions.any():
                return None
            
            # Ensure valid_actions is a list or regular array
            if not valid_actions:
                return None
            
            # Adaptive exploration rate based on game progress and strategy confidence
            if exploration_rate is None:
                # Base rate depends on game stage (explore more early, less later)
                turn = game_state.turn
                if turn <= 3:
                    base_rate = 0.3  # Early game: explore more
                elif turn <= 7:
                    base_rate = 0.2  # Mid game: moderate exploration
                else:
                    base_rate = 0.1  # Late game: exploit more
                
                # Modify based on available strategies
                strategy_count = len(self.strategies)
                if strategy_count < 50:
                    # With few strategies, explore more
                    exploration_rate = min(0.5, base_rate + 0.2)
                elif strategy_count < 200:
                    exploration_rate = base_rate
                else:
                    # With many strategies, explore less
                    exploration_rate = max(0.05, base_rate - 0.1)
            
            # Exploration: randomly decide to explore new actions
            if random.random() < exploration_rate:
                # Choose a random action from valid actions
                return random.choice(valid_actions)
            
            # Extract current pattern with full game state details
            current_pattern = self.extract_strategy_pattern(game_state)
            
            # Try to find exact match first (exploitation)
            exact_matches = []
            if current_pattern in self.strategies:
                strategy = self.strategies[current_pattern]
                
                # Find matching action sequences
                for seq, reward in self.action_sequences:
                    if len(seq) > 0 and reward > 0:
                        # Handle both structured and simple action formats
                        if isinstance(seq[0], dict) and 'action_idx' in seq[0]:
                            action_idx = seq[0]['action_idx']
                        else:
                            action_idx = seq[0]  # For backward compatibility
                            
                        if action_idx in valid_actions:
                            exact_matches.append((action_idx, reward))
                
                if exact_matches:
                    # Weighted random selection based on reward
                    weights = [max(0.1, r) for _, r in exact_matches]
                    total_weight = sum(weights)
                    probabilities = [w / total_weight for w in weights]
                    
                    chosen_action = np.random.choice(
                        [a for a, _ in exact_matches], 
                        p=probabilities
                    )
                    
                    logging.debug(f"Exact pattern match: Suggested action {chosen_action}")
                    
                    # If called during MCTS, return with a value
                    if for_mcts:
                        # Find the value for this action
                        for action, reward in exact_matches:
                            if action == chosen_action:
                                return (chosen_action, min(1.0, max(0.0, reward / 10.0)))
                        
                    return chosen_action
            
            # Partial pattern matching (generalization)
            partial_matches = []
            for pattern, strategy in self.strategies.items():
                similarity = self._pattern_similarity(pattern, current_pattern)
                if similarity > 0.7:  # Similarity threshold
                    # Weight by both similarity and strategy quality
                    value = similarity * strategy['success_rate'] * (strategy['count'] ** 0.5)
                    partial_matches.append((pattern, strategy, value))
            
            if partial_matches:
                # Sort by value
                partial_matches.sort(key=lambda x: x[2], reverse=True)
                
                # Try to find matching sequences for top matches
                suggested_actions = []
                
                for pattern, strategy, _ in partial_matches[:5]:  # Top 5 partial matches
                    for seq, reward in self.action_sequences:
                        if len(seq) > 0 and reward > 0:
                            # Handle both structured and simple action formats
                            if isinstance(seq[0], dict) and 'action_idx' in seq[0]:
                                action_idx = seq[0]['action_idx']
                                # Extract more context if available
                                action_type = seq[0].get('action_type', 'unknown')
                                board_context = seq[0].get('board_context', {})
                            else:
                                action_idx = seq[0]  # For backward compatibility
                                action_type = 'unknown'
                                board_context = {}
                                
                            if action_idx in valid_actions:
                                # Calculate contextual relevance if context exists
                                context_relevance = 1.0
                                if board_context:
                                    # Compare current state to the recorded context
                                    me = game_state.p1 if game_state.agent_is_p1 else game_state.p2
                                    opp = game_state.p2 if game_state.agent_is_p1 else game_state.p1
                                    
                                    # More sophisticated context matching
                                    context_similarity = 0.0
                                    points = 0
                                    
                                    # Life total similarity
                                    if 'my_life' in board_context:
                                        life_diff = abs(me['life'] - board_context['my_life'])
                                        life_sim = max(0.0, 1.0 - life_diff / 20.0)
                                        context_similarity += life_sim
                                        points += 1
                                    
                                    # Creature count similarity
                                    if 'my_creatures' in board_context:
                                        my_creatures = sum(1 for cid in me["battlefield"] 
                                                    if game_state._safe_get_card(cid) and 
                                                    hasattr(game_state._safe_get_card(cid), 'card_types') and 
                                                    'creature' in game_state._safe_get_card(cid).card_types)
                                        
                                        creature_diff = abs(my_creatures - board_context['my_creatures'])
                                        creature_sim = max(0.0, 1.0 - creature_diff / 3.0)
                                        context_similarity += creature_sim
                                        points += 1
                                    
                                    # Calculate overall context relevance
                                    if points > 0:
                                        context_relevance = context_similarity / points
                                    
                                # Add to suggested actions with combined relevance and reward
                                action_value = context_relevance * reward
                                suggested_actions.append((action_idx, action_value))
                
                if suggested_actions:
                    # Remove duplicates by taking highest value for each action
                    action_values = {}
                    for action_idx, value in suggested_actions:
                        action_values[action_idx] = max(value, action_values.get(action_idx, 0))
                    
                    # Convert back to list
                    unique_actions = [(action, value) for action, value in action_values.items()]
                    
                    # Weighted random selection
                    weights = [max(0.1, v) for _, v in unique_actions]
                    total_weight = sum(weights)
                    probabilities = [w / total_weight for w in weights]
                    
                    chosen_action = np.random.choice(
                        [a for a, _ in unique_actions], 
                        p=probabilities
                    )
                    
                    logging.debug(f"Pattern generalization: Suggested action {chosen_action}")
                    
                    # If called during MCTS, return with a value
                    if for_mcts:
                        # Find the value for this action
                        value = 0.5  # Default neutral value
                        for act, val in unique_actions:
                            if act == chosen_action:
                                value = min(1.0, max(0.0, val / 5.0))
                                break
                        return (chosen_action, value)
                        
                    return chosen_action
            
            # No match found - fall back to random choice
            return random.choice(valid_actions)
        
        except Exception as e:
            logging.error(f"Error in get_suggested_action: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return None
        
    def prune_memory(self):
        """
        Remove low-value or old entries to keep memory size manageable,
        with time decay and better prioritization.
        """
        # Always prune if we're over the max size
        if len(self.strategies) > self.max_size * 0.95:  # Prune at 95% capacity (increased from 90%)
            # Apply time decay to older patterns
            current_time = time.time()
            decay_factor = 0.98  # Reduced decay from 0.95 to 0.98 to preserve more history
            
            for pattern in self.strategies:
                # Apply decay based on age if timestamp exists
                if 'timestamp' in self.strategies[pattern]:
                    age_in_hours = (current_time - self.strategies[pattern]['timestamp']) / 3600
                    # Decay more for older entries (limiting decay to 30% to preserve more history)
                    age_decay = max(0.3, decay_factor ** min(24, age_in_hours))
                    self.strategies[pattern]['success_rate'] *= age_decay
                    self.strategies[pattern]['reward'] *= age_decay
                else:
                    # Add timestamp if not present
                    self.strategies[pattern]['timestamp'] = current_time
            
            # Sort by composite value (success_rate * reward * sqrt(count))
            sorted_strategies = sorted(
                self.strategies.items(),
                key=lambda x: x[1]['success_rate'] * abs(x[1]['reward']) * (x[1]['count'] ** 0.5),
                reverse=True
            )
            
            # Keep only the top entries
            to_keep = int(self.max_size * 0.8)  # Keep 80% (increased from 70%)
            self.strategies = {k: v for k, v in sorted_strategies[:to_keep]}
            logging.info(f"Pruned strategy memory from {len(sorted_strategies)} to {len(self.strategies)} entries")
        
        # Prune action sequences more conservatively
        if len(self.action_sequences) > self.max_size * 0.9:  # Increased from 0.8
            # Sort by reward
            self.action_sequences.sort(key=lambda x: x[1], reverse=True)
            
            # Keep top sequences and some random lower-reward ones for exploration
            top_count = int(self.max_size * 0.6)  # Increased from 0.5
            random_count = int(self.max_size * 0.3)  # Increased from 0.2
            
            top_sequences = self.action_sequences[:top_count]
            
            # Select random items from the rest
            if len(self.action_sequences) > top_count:
                import random
                random_indices = random.sample(
                    range(top_count, len(self.action_sequences)), 
                    min(random_count, len(self.action_sequences) - top_count)
                )
                random_sequences = [self.action_sequences[i] for i in random_indices]
            else:
                random_sequences = []
                
            self.action_sequences = top_sequences + random_sequences
            logging.info(f"Pruned action sequences to {len(self.action_sequences)} entries")
