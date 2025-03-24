import logging
import numpy as np
import re
from typing import Dict, List, Any, Tuple, Optional, Set

class EnhancedCardEvaluator:
    """Advanced card evaluation system for Magic: The Gathering."""
    
    def __init__(self, game_state, stats_tracker=None, card_memory=None):
        """
        Initialize the card evaluator.
        
        Args:
            game_state: The current game state
            stats_tracker: Optional DeckStatsTracker for performance-based evaluation
            card_memory: Optional CardMemory for historical card performance
        """
        self.game_state = game_state
        self.stats_tracker = stats_tracker
        self.card_memory = card_memory
        
        # Card type weights for evaluation
        self.type_weights = {
            'creature': 1.0,
            'instant': 0.9,
            'sorcery': 0.85,
            'artifact': 0.8,
            'enchantment': 0.8,
            'planeswalker': 1.2,
            'land': 0.7
        }
        
        # Keyword weights for evaluation
        self.keyword_weights = {
            'flying': 0.3,
            'trample': 0.25,
            'deathtouch': 0.35,
            'lifelink': 0.3,
            'first strike': 0.25,
            'double strike': 0.45,
            'vigilance': 0.2,
            'haste': 0.3,
            'hexproof': 0.4,
            'indestructible': 0.5,
            'menace': 0.25,
            'reach': 0.15,
            'flash': 0.25,
            'defender': -0.1,
            'protection': 0.35,
            'ward': 0.3,
            'prowess': 0.2,
            'unblockable': 0.4
        }
        
        # Initialize memory of card synergies
        self.synergy_memory = {}
        
        # Card evaluation cache
        self.evaluation_cache = {}
        self.cache_hits = 0
        self.cache_misses = 0
    
    def evaluate_card(self, card_id: int, context: str = "general", context_details: Dict[str, Any] = None) -> float:
        """
        Evaluate a card in the given context with enhanced historical performance data.
        
        Args:
            card_id: The ID of the card to evaluate
            context: The context for evaluation ("general", "play", "attack", "block", etc.)
            context_details: Additional context information for more nuanced evaluation
                
        Returns:
            float: The card's evaluation score
        """
        try:
            # Check cache first - create a cache key from card_id, context, and key context details
            cache_key = f"{card_id}_{context}"
            
            # Include key context details that significantly affect evaluation in the cache key
            if context_details:
                if "game_stage" in context_details:
                    cache_key += f"_{context_details['game_stage']}"
                if "position" in context_details:
                    cache_key += f"_{context_details['position']}"
                if "aggression_level" in context_details:
                    # Round aggression level to 0.1 precision to avoid too many cache entries
                    aggr = round(context_details['aggression_level'] * 10) / 10
                    cache_key += f"_{aggr}"
                    
            # Try to get from cache
            if cache_key in self.evaluation_cache:
                self.cache_hits += 1
                return self.evaluation_cache[cache_key]
                
            self.cache_misses += 1
            
            # If not in cache, perform evaluation
            gs = self.game_state
            card = gs._safe_get_card(card_id)
            if not card:
                return 0.0
            
            # Default context details if not provided
            if context_details is None:
                context_details = {
                    "game_stage": "mid",
                    "position": "even",
                    "aggression_level": 0.5,
                    "turn": gs.turn,
                    "phase": gs.phase,
                    "deck_archetype": "unknown"
                }
            
            # Calculate base value (static card evaluation)
            base_value = self._calculate_base_value(card)
            
            # Add context-specific value
            context_value = 0.0
            if context == "play":
                context_value = self._evaluate_for_play(card_id)
            elif context == "attack":
                context_value = self._evaluate_for_attack(card_id)
            elif context == "block":
                context_value = self._evaluate_for_block(card_id)
            elif context == "discard":
                context_value = self._evaluate_for_discard(card_id)
            
            # Add historical performance value from card memory and stats tracker
            history_value = 0.0
            deck_archetype = context_details.get("deck_archetype", "unknown")
            
            # Get value from CardMemory if available
            if self.card_memory:
                try:
                    # Get effectiveness rating specific to this archetype
                    effectiveness = self.card_memory.get_effectiveness_for_archetype(card_id, deck_archetype)
                    
                    # Convert 0-1 effectiveness rating to a value boost between -0.5 and +0.5
                    history_value += (effectiveness - 0.5) * 1.5
                    
                    # Get optimal turn data and adjust based on current turn
                    optimal_turn = self.card_memory.get_optimal_play_turn(card_id)
                    if optimal_turn > 0:
                        current_turn = context_details.get("turn", 0)
                        # Bonus for playing near the optimal turn
                        turn_proximity = 1.0 - min(abs(current_turn - optimal_turn) / 3.0, 1.0)
                        history_value += turn_proximity * 0.3
                except Exception as e:
                    logging.warning(f"Error getting card memory data: {e}")
            
            # Add stats value if available
            stats_value = 0.0
            if self.stats_tracker:
                stats_value = self._get_stats_value(card_id)
            
            # Calculate total value with weighted components
            total_value = (
                base_value * 0.6 +     # 60% weight to base card evaluation
                context_value * 0.25 +  # 25% weight to context-specific evaluation
                history_value * 0.1 +   # 10% weight to historical performance
                stats_value * 0.05      # 5% weight to stats tracker data
            )
            
            # Apply game stage multiplier
            stage_multipliers = {
                "early": 0.9,
                "mid": 1.0,
                "late": 1.1
            }
            total_value *= stage_multipliers.get(context_details.get("game_stage", "mid"), 1.0)
            
            # Apply position adjustment
            position_adjustments = {
                "dominating": 1.2,
                "ahead": 1.1,
                "even": 1.0,
                "behind": 0.9,
                "struggling": 0.8
            }
            total_value *= position_adjustments.get(context_details.get("position", "even"), 1.0)
            
            # Apply aggression adjustment
            aggression_level = context_details.get("aggression_level", 0.5)
            if hasattr(card, 'card_types') and 'creature' in card.card_types and hasattr(card, 'power'):
                # More aggressive strategy values offensive creatures higher
                if card.power > 2:
                    total_value *= 1.0 + (aggression_level - 0.5) * 0.4
                # Less aggressive strategy values defensive creatures higher
                elif hasattr(card, 'toughness') and card.toughness > card.power + 1:
                    total_value *= 1.0 - (aggression_level - 0.5) * 0.2
            
            # Store in cache
            self.evaluation_cache[cache_key] = total_value
            
            # If cache is getting too large, remove oldest entries
            if len(self.evaluation_cache) > 1000:
                # Simple approach: just clear the cache
                self.evaluation_cache = {cache_key: total_value}
                self.cache_hits = 0
                self.cache_misses = 0
            
            return total_value
        except Exception as e:
            logging.error(f"Error evaluating card {card_id}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return 0.0  # Default to neutral value on error
    
    def record_card_performance(self, card_id: int, game_result: Dict) -> None:
        """
        Record performance of a card to the card memory system.
        
        Args:
            card_id: ID of the card
            game_result: Dictionary with game result information
        """
        if self.card_memory:
            try:
                # Get card name if available
                card = self.game_state._safe_get_card(card_id)
                if card and hasattr(card, 'name'):
                    game_result['card_name'] = card.name
                    
                    # Add CMC if available
                    if hasattr(card, 'cmc'):
                        game_result['cmc'] = card.cmc
                
                self.card_memory.update_card_performance(card_id, game_result)
            except Exception as e:
                logging.error(f"Error recording card performance: {e}")
    
    def _calculate_base_value(self, card) -> float:
        """
        Calculate the base value of a card with improved evaluation criteria.
        
        Args:
            card: The card object to evaluate
            
        Returns:
            float: Base value of the card
        """
        if not card:
            return 0.0
        
        value = 0.0
        
        # Value based on mana cost - refined curve
        if hasattr(card, 'cmc'):
            # Cards with CMC 2-4 are generally most valuable
            if 2 <= card.cmc <= 4:
                value += card.cmc * 0.8
            elif card.cmc < 2:
                # Low cost cards - higher value for 1-drops than 0-drops
                if card.cmc == 1:
                    value += 0.9
                else:  # 0-cost cards
                    value += 0.5
            else:
                # Diminishing returns for high CMC, but still valuable
                # More granular scaling for high cost cards
                if card.cmc <= 6:
                    value += 4.0 + (card.cmc - 4) * 0.5  # 5 CMC = 4.5, 6 CMC = 5.0
                else:
                    value += 5.0 + (card.cmc - 6) * 0.3  # Slower increase beyond 6 CMC
        
        # Value based on card type - with refined weights
        if hasattr(card, 'card_types'):
            for card_type in card.card_types:
                value += self.type_weights.get(card_type.lower(), 0.0)
                
                # Bonus for multitype cards (e.g., artifact creatures)
                if len(card.card_types) > 1:
                    value += 0.2
        
        # Value based on creature stats with more nuanced evaluation
        if hasattr(card, 'card_types') and 'creature' in card.card_types:
            if hasattr(card, 'power') and hasattr(card, 'toughness'):
                # Basic stats value
                power = card.power
                toughness = card.toughness
                
                # Different formulas for different creature profiles
                if power >= 2 * toughness:  # Glass cannon
                    stats_value = power * 0.7 + toughness * 0.3
                elif toughness >= 2 * power:  # Wall/defensive
                    stats_value = power * 0.4 + toughness * 0.6
                else:  # Balanced
                    stats_value = (power + toughness) / 2
                
                # Additional value for efficient stat-to-cost ratio
                if hasattr(card, 'cmc') and card.cmc > 0:
                    efficiency = (power + toughness) / card.cmc
                    if efficiency > 2:  # Very efficient
                        stats_value *= 1.3
                    elif efficiency > 1:  # Good efficiency
                        stats_value *= 1.1
                
                # Special case for 0 power creatures
                if power == 0:
                    stats_value *= 0.5
                    # But if it has a good ability, don't penalize as much
                    if hasattr(card, 'oracle_text') and len(card.oracle_text) > 50:
                        stats_value *= 1.5  # Partial restoration of value
                
                value += stats_value
        
        # Value based on keywords with more comprehensive evaluation
        if hasattr(card, 'oracle_text'):
            oracle_text = card.oracle_text.lower()
            
            # Count keywords for synergistic value
            keyword_count = 0
            keyword_value = 0
            
            for keyword, weight in self.keyword_weights.items():
                if keyword in oracle_text:
                    keyword_value += weight
                    keyword_count += 1
                    
                    # Special synergies between keywords
                    if keyword == 'deathtouch' and 'first strike' in oracle_text:
                        keyword_value += 0.3  # Bonus for deathtouch + first strike
                    elif keyword == 'trample' and 'double strike' in oracle_text:
                        keyword_value += 0.4  # Bonus for trample + double strike
            
            # Bonus for multiple keywords (synergy)
            if keyword_count > 1:
                keyword_value *= 1 + (keyword_count - 1) * 0.1
                
            value += keyword_value
        
        # Enhanced value for card advantage
        if hasattr(card, 'oracle_text'):
            card_text = card.oracle_text.lower()
            
            # Card draw effects with more granular evaluation
            if 'draw a card' in card_text:
                value += 0.5
                # Check if it's an easy condition
                if 'when' in card_text and 'enters the battlefield' in card_text:
                    value += 0.2  # Bonus for ETB draw
            elif 'draw two cards' in card_text:
                value += 1.0
                if 'you may' in card_text or 'if' in card_text:  # Conditional
                    value -= 0.3
            elif 'draw three cards' in card_text:
                value += 1.5
                if 'you may' in card_text or 'if' in card_text:  # Conditional
                    value -= 0.5
            
            # Value for "cantrips" (spells that replace themselves)
            if ('draw a card' in card_text and 
                (hasattr(card, 'card_types') and 
                any(t in card.card_types for t in ['instant', 'sorcery']))):
                value += 0.2
            
            # Card selection (scry, surveil, etc.)
            if 'scry' in card_text:
                # Extract scry amount
                import re
                scry_match = re.search(r'scry (\d+)', card_text)
                if scry_match:
                    scry_amount = int(scry_match.group(1))
                    value += min(0.4, scry_amount * 0.1)  # Cap at 0.4
            
            if 'surveil' in card_text:
                # Extract surveil amount
                import re
                surveil_match = re.search(r'surveil (\d+)', card_text)
                if surveil_match:
                    surveil_amount = int(surveil_match.group(1))
                    value += min(0.5, surveil_amount * 0.12)  # Slightly better than scry
                    
            # Removal effects with quality assessment
            removal_value = 0
            
            # Check for different types of removal
            if 'destroy target' in card_text:
                removal_value = 0.7
                
                # Check if it's conditional
                if 'if' in card_text or 'only if' in card_text or 'unless' in card_text:
                    removal_value *= 0.7
                    
                # Check targets
                if 'creature' in card_text:
                    if 'nonblack' in card_text or 'nonartifact' in card_text:
                        removal_value *= 0.8  # Restricted by color/type
                    elif 'tapped' in card_text or 'attacking' in card_text:
                        removal_value *= 0.9  # Restricted by state
                elif 'enchantment' in card_text or 'artifact' in card_text:
                    removal_value *= 0.8  # Not creature removal
                elif 'planeswalker' in card_text:
                    removal_value *= 1.1  # Premium for planeswalker removal
                
            # Check exile (better than destroy)
            elif 'exile target' in card_text:
                removal_value = 0.9
                
                # Similar conditions as destroy
                if 'if' in card_text or 'only if' in card_text or 'unless' in card_text:
                    removal_value *= 0.7
                    
                if 'creature' in card_text:
                    if 'nonblack' in card_text or 'nonartifact' in card_text:
                        removal_value *= 0.8
                elif 'enchantment' in card_text or 'artifact' in card_text:
                    removal_value *= 0.8
                elif 'planeswalker' in card_text:
                    removal_value *= 1.1
            
            # Damage-based removal
            elif 'deals' in card_text and 'damage to target' in card_text:
                import re
                damage_match = re.search(r'deals (\d+) damage', card_text)
                if damage_match:
                    damage = int(damage_match.group(1))
                    removal_value = min(0.8, damage * 0.2)  # Value scales with damage
                    
                    if 'creature' in card_text and 'player' in card_text:
                        removal_value *= 1.2  # Flexible targeting is better
            
            # Return to hand (temporary removal)
            elif 'return target' in card_text and 'to its owner\'s hand' in card_text:
                removal_value = 0.5  # Temporary removal worth less
            
            # Add removal value
            value += removal_value
            
            # Board wipes (mass removal)
            if any(term in card_text for term in ['destroy all', 'all creatures get -', 'deal damage to all']):
                board_wipe_value = 1.0
                
                # Conditional board wipes
                if 'nonblack' in card_text or 'non-artifact' in card_text:
                    board_wipe_value *= 0.7
                    
                # One-sided board wipes are premium
                if 'you control' in card_text and 'doesn\'t' in card_text:
                    board_wipe_value *= 1.5
                    
                value += board_wipe_value
            
            # Counterspells
            if 'counter target spell' in card_text:
                counter_value = 0.8
                
                # Conditional counters
                if 'unless' in card_text:
                    counter_value *= 0.7
                    
                # Limited target counters
                if 'creature' in card_text or 'noncreature' in card_text:
                    counter_value *= 0.8
                    
                value += counter_value
            
            # Tutors (card search)
            if 'search your library for' in card_text:
                tutor_value = 0.8
                
                # Restricted tutors
                if 'basic land' in card_text:
                    tutor_value = 0.6  # Land tutors are good but more limited
                elif 'creature' in card_text or 'instant' in card_text or 'sorcery' in card_text:
                    tutor_value *= 0.8  # Type-restricted tutors
                    
                # Does it put directly into hand or battlefield?
                if 'put it onto the battlefield' in card_text:
                    tutor_value *= 1.4  # Premium for cheating mana cost
                elif 'put it into your hand' in card_text:
                    tutor_value *= 1.1  # Good but doesn't cheat mana
                    
                value += tutor_value
                
            # Protection effects
            if 'hexproof' in card_text or 'indestructible' in card_text:
                if 'gains' in card_text or 'gain' in card_text:
                    value += 0.6  # Giving protection to other permanents
                else:
                    value += 0.4  # Having protection itself
                    
            # Mana acceleration/ramp
            if ('add' in card_text and any(f"{{{c}}}" in card_text for c in ['w', 'u', 'b', 'r', 'g', 'c'])):
                # Check if it's a mana ability (not a land)
                if 'land' not in card.card_types:
                    ramp_value = 0.5
                    
                    # Multiple mana is better
                    if any(f"{{{c}}}{{{c}}}" in card_text for c in ['w', 'u', 'b', 'r', 'g', 'c']):
                        ramp_value += 0.2
                        
                    value += ramp_value
        
        return value
    
    def _evaluate_for_attack(self, card_id: int) -> float:
        """Evaluate a card for attacking with it."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Initialize value
        value = 0.0
        
        # Only creatures can attack
        if not hasattr(card, 'card_types') or 'creature' not in card.card_types:
            return -5.0  # Strong negative to prevent non-creatures from attacking
        
        # Can't attack if tapped or has summoning sickness
        if (card_id in me["tapped_permanents"] or
            (card_id in me["entered_battlefield_this_turn"] and
             not hasattr(card, 'oracle_text') or "haste" not in card.oracle_text.lower())):
            return -5.0
        
        # Basic attack value based on power
        if hasattr(card, 'power'):
            value += card.power * 0.5
        
        # Factor: Opponent's blockers
        potential_blockers = [
            cid for cid in opp["battlefield"] 
            if gs._safe_get_card(cid) and 'creature' in gs._safe_get_card(cid).card_types and 
            cid not in opp["tapped_permanents"]
        ]
        
        # Check evasion abilities
        has_evasion = False
        if hasattr(card, 'oracle_text'):
            card_text = card.oracle_text.lower()
            
            # Flying
            if "flying" in card_text:
                has_evasion = not any(
                    "flying" in gs._safe_get_card(cid).oracle_text.lower() or
                    "reach" in gs._safe_get_card(cid).oracle_text.lower()
                    for cid in potential_blockers
                    if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'oracle_text')
                )
                
            # Other evasion
            if any(ability in card_text for ability in ["can't be blocked", "menace", "intimidate", "shadow"]):
                has_evasion = True
        
        # Adjust value based on evasion
        if has_evasion:
            value += 0.8
        elif potential_blockers:
            # Check for unfavorable blocks
            unfavorable_blocks = 0
            for blocker_id in potential_blockers:
                blocker = gs._safe_get_card(blocker_id)
                if not blocker or not hasattr(blocker, 'power') or not hasattr(blocker, 'toughness'):
                    continue
                    
                # Blocker can kill attacker without dying
                if blocker.power >= card.toughness and blocker.toughness > card.power:
                    unfavorable_blocks += 1
            
            # Severe penalty for unfavorable blocks
            if unfavorable_blocks > 0:
                value -= 0.7 * unfavorable_blocks
                
            # Check for favorable blocks
            favorable_blocks = 0
            for blocker_id in potential_blockers:
                blocker = gs._safe_get_card(blocker_id)
                if not blocker or not hasattr(blocker, 'power') or not hasattr(blocker, 'toughness'):
                    continue
                    
                # Attacker can kill blocker without dying
                if card.power >= blocker.toughness and card.toughness > blocker.power:
                    favorable_blocks += 1
            
            # Bonus for favorable blocks
            if favorable_blocks > 0:
                value += 0.5 * favorable_blocks
        else:
            # No blockers is great!
            value += 1.0
        
        # Factor: Special combat abilities
        if hasattr(card, 'oracle_text'):
            card_text = card.oracle_text.lower()
            
            # Deathtouch is great for attacking
            if "deathtouch" in card_text:
                value += 0.4
                
            # First strike / Double strike
            if "first strike" in card_text or "double strike" in card_text:
                value += 0.3
                
            # Trample helps push damage through
            if "trample" in card_text:
                value += 0.3
        
        # Factor: Life totals
        if opp["life"] <= card.power:
            # Could be lethal!
            value += 2.0
        elif opp["life"] <= 5:
            # Opponent at low life - attacking is good
            value += 0.7
            
        # Factor: Strategic considerations
        if gs.turn <= 3:
            # Early game - be more aggressive
            value += 0.2
        
        return value
    
    def _evaluate_for_block(self, card_id: int) -> float:
        """Evaluate a card for blocking with it."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Initialize value
        value = 0.0
        
        # Only creatures can block
        if not hasattr(card, 'card_types') or 'creature' not in card.card_types:
            return -5.0  # Strong negative to prevent non-creatures from blocking
        
        # Can't block if tapped
        if card_id in me["tapped_permanents"]:
            return -5.0
            
        # Can't block if has defender
        if hasattr(card, 'oracle_text') and "defender" in card.oracle_text.lower():
            value += 0.5  # Defender is actually good for blocking
        
        # Basic block value based on toughness
        if hasattr(card, 'toughness'):
            value += card.toughness * 0.4
        
        # No reason to evaluate further if no attackers
        if not gs.current_attackers:
            return -0.5  # Minor negative - no need to block
        
        # Factor: Creature matchups
        best_block_value = -float('inf')
        best_attacker = None
        
        for attacker_id in gs.current_attackers:
            attacker = gs._safe_get_card(attacker_id)
            if not attacker or not hasattr(attacker, 'power') or not hasattr(attacker, 'toughness'):
                continue
                
            # Calculate block value for this attacker
            block_value = 0.0
            
            # Check if blocker can survive
            blocker_survives = card.toughness > attacker.power
            
            # Check if blocker can kill attacker
            kills_attacker = card.power >= attacker.toughness
            
            # Determine block quality
            if kills_attacker and blocker_survives:
                # Ideal: kill attacker and survive
                block_value = 2.0
            elif kills_attacker and not blocker_survives:
                # Trade: kill attacker but die
                block_value = 1.0
                
                # Base trade value on mana costs if available
                if hasattr(card, 'cmc') and hasattr(attacker, 'cmc'):
                    if attacker.cmc > card.cmc:
                        block_value += 0.5  # Good trade
                    elif attacker.cmc < card.cmc:
                        block_value -= 0.3  # Bad trade
            elif not kills_attacker and blocker_survives:
                # Chump with survival: don't kill attacker but survive
                block_value = 0.3
            else:
                # Chump block: die without killing attacker
                block_value = -0.2
                
                # Exception: high power attacker worth chump blocking
                if hasattr(attacker, 'power') and attacker.power >= 4:
                    block_value = 0.4  # Worth chump blocking a big threat
            
            # Special abilities consideration
            if hasattr(card, 'oracle_text') and hasattr(attacker, 'oracle_text'):
                card_text = card.oracle_text.lower()
                attacker_text = attacker.oracle_text.lower()
                
                # Deathtouch makes any block better
                if "deathtouch" in card_text:
                    block_value += 0.5
                
                # First strike is great for blocking
                if "first strike" in card_text and "first strike" not in attacker_text:
                    block_value += 0.4
                
                # Double strike on attacker is dangerous
                if "double strike" in attacker_text:
                    block_value -= 0.3
            
            # Update best block if this one is better
            if block_value > best_block_value:
                best_block_value = block_value
                best_attacker = attacker_id
        
        # Add best block value to overall value
        if best_block_value > -float('inf'):
            value += best_block_value
        
        # Factor: Life totals
        life_difference = me["life"] - opp["life"]
        
        if me["life"] <= 5:
            # Critical life - block more aggressively
            value += 0.5
        elif life_difference < -10:
            # Far behind - need to preserve life
            value += 0.3
        elif life_difference > 10:
            # Far ahead - can take more risks
            value -= 0.2
        
        return value
    
    
    
    def _calculate_card_likelihood(self, card, color_count, visible_creatures, visible_instants, visible_artifacts):
        """Helper to calculate how likely a card is to be in opponent's hand"""
        gs = self.game_state
        weight = 1.0
        
        # Card must have required attributes
        if not card or not hasattr(card, 'colors') or not hasattr(card, 'card_types'):
            return 0.0
        
        # Color matching
        card_colors = np.array(card.colors)
        color_match = np.sum(card_colors * color_count) / (np.sum(color_count) + 1e-6)
        weight *= (1.0 + color_match)
        
        # Card type matching
        if 'creature' in card.card_types and visible_creatures > 0:
            weight *= 1.5
        if 'instant' in card.card_types and visible_instants > 0:
            weight *= 1.2
        if 'artifact' in card.card_types and visible_artifacts > 0:
            weight *= 1.3
            
        # Mana curve considerations - higher probability of having castable cards
        if hasattr(card, 'cmc'):
            if card.cmc <= gs.turn:
                weight *= 2.0
            elif card.cmc <= gs.turn + 2:
                weight *= 1.0
            else:
                weight *= 0.5
        
        return weight
    
    def _evaluate_for_discard(self, card_id: int) -> float:
        """Evaluate a card for discarding it (lower is better to discard)."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Initialize value (higher value = better to KEEP)
        value = 0.0
        
        # Factor: Mana cost relative to game stage
        if hasattr(card, 'cmc'):
            # Early game - favor lower cost cards
            if gs.turn <= 4:
                if card.cmc <= 4:
                    value += (4 - card.cmc) * 0.2  # More value for cheap cards
                else:
                    value -= (card.cmc - 4) * 0.1  # Penalty for expensive cards
            # Mid game - balance
            elif gs.turn <= 8:
                if 3 <= card.cmc <= 6:
                    value += 0.3  # Prefer mid-range cards
                elif card.cmc > 6:
                    value -= 0.2  # Still penalize very expensive cards
            # Late game - favor impact cards
            else:
                if card.cmc >= 5:
                    value += 0.3  # Prefer high-impact cards
                if card.cmc <= 2:
                    value -= 0.2  # Low-impact cards less valuable late
        
        # Factor: Card type
        if hasattr(card, 'card_types'):
            # Lands decrease in value as game progresses
            if 'land' in card.card_types:
                lands_in_play = len([cid for cid in me["battlefield"] 
                                   if gs._safe_get_card(cid) and 'land' in gs._safe_get_card(cid).card_types])
                
                if lands_in_play <= 3:
                    value += 1.0  # Critical to keep early lands
                elif lands_in_play <= 6:
                    value += 0.5  # Still valuable
                else:
                    value += 0.1  # Diminishing returns
            
            # Creatures maintain consistent value
            if 'creature' in card.card_types:
                value += 0.4
                
                # Higher value for powerful creatures
                if hasattr(card, 'power') and hasattr(card, 'toughness'):
                    if card.power + card.toughness >= 7:
                        value += 0.3
            
            # Instants have high value (flexible)
            if 'instant' in card.card_types:
                value += 0.5
        
        # Factor: Card advantage and removal maintain high value
        if hasattr(card, 'oracle_text'):
            card_text = card.oracle_text.lower()
            
            if 'draw' in card_text and 'card' in card_text:
                value += 0.5
                
            if any(term in card_text for term in ['destroy', 'exile', 'sacrifice', 'return to hand']):
                value += 0.4
        
        # Factor: Synergy with board
        synergy_value = self._calculate_synergy_value(card_id, me["battlefield"])
        value += synergy_value
        
        # Invert the value for discard (lower value = better to discard)
        return -value
    
    # Update in enhanced_card_evaluator.py
    def _calculate_synergy_value(self, card_id: int, board: List[int]) -> float:
        """Calculate how well a card synergizes with existing board."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        # Check cache first
        cache_key = (card_id, tuple(sorted(board)))
        if cache_key in self.synergy_memory:
            return self.synergy_memory[cache_key]
        
        synergy_value = 0.0
        
        try:
            # Creature type synergy
            creature_types = set()
            for board_id in board:
                board_card = gs._safe_get_card(board_id)
                if board_card and hasattr(board_card, 'subtypes'):
                    creature_types.update(board_card.subtypes)
            
            if hasattr(card, 'subtypes'):
                shared_types = set(card.subtypes).intersection(creature_types)
                synergy_value += len(shared_types) * 0.1
            
            # Ability synergy
            if hasattr(card, 'oracle_text'):
                card_text = card.oracle_text.lower()
                
                # Check for tribal synergies
                for creature_type in creature_types:
                    if creature_type.lower() in card_text:
                        synergy_value += 0.3
                        break
                
                # Check for keyword synergies
                synergy_keywords = {
                    '+1/+1 counter': ['counter', '+1/+1'],
                    'sacrifice': ['sacrifice'],
                    'discard': ['discard'],
                    'graveyard': ['graveyard', 'from your graveyard'],
                    'enchantment': ['enchantment'],
                    'artifact': ['artifact'],
                    'lifegain': ['gain life', 'life link']
                }
                
                # Count cards with each synergy on board
                synergy_counts = {k: 0 for k in synergy_keywords}
                
                for board_id in board:
                    board_card = gs._safe_get_card(board_id)
                    if not board_card or not hasattr(board_card, 'oracle_text'):
                        continue
                        
                    board_text = board_card.oracle_text.lower()
                    
                    for synergy_type, keywords in synergy_keywords.items():
                        if any(kw in board_text for kw in keywords):
                            synergy_counts[synergy_type] += 1
                
                # Check if this card matches any synergies
                for synergy_type, keywords in synergy_keywords.items():
                    if any(kw in card_text for kw in keywords):
                        # Value scales with number of synergistic cards
                        synergy_value += min(synergy_counts[synergy_type] * 0.15, 0.45)
            
            # Color synergy
            if hasattr(card, 'colors'):
                color_counts = np.zeros(5)
                
                for board_id in board:
                    board_card = gs._safe_get_card(board_id)
                    if board_card and hasattr(board_card, 'colors'):
                        for i, color in enumerate(board_card.colors):
                            color_counts[i] += color
                
                # Calculate color synergy
                color_match = sum(a and b for a, b in zip(card.colors, color_counts > 0))
                colors_in_card = sum(card.colors)
                
                if colors_in_card > 0:
                    color_synergy = color_match / colors_in_card
                    synergy_value += color_synergy * 0.1
        except Exception as e:
            logging.error(f"Error calculating synergy for card {card_id}: {str(e)}")
            # Return 0 synergy on error
            return 0.0
        
        # Cache the result
        self.synergy_memory[cache_key] = synergy_value
        
        return synergy_value
    
    def _get_stats_value(self, card_id: int) -> float:
        """Get value based on statistical performance."""
        if not self.stats_tracker:
            return 0.0
        
        # Get card stats
        card_stats = self.stats_tracker.get_card_stats(card_id)
        if not card_stats:
            return 0.0
        
        # Calculate win rate
        games_played = card_stats.get("games_played", 0)
        if games_played < 5:  # Need enough data
            return 0.0
            
        wins = card_stats.get("wins", 0)
        win_rate = wins / games_played if games_played > 0 else 0.0
        
        # Convert win rate to value (centered around 0.5 win rate)
        stats_value = (win_rate - 0.5) * 1.5
        
        return stats_value
    
    def get_card_rankings(self, card_ids: List[int], context: str = "general") -> List[Tuple[int, float]]:
        """
        Rank a list of cards based on their evaluation scores.
        
        Args:
            card_ids: List of card IDs to rank
            context: The context for evaluation
            
        Returns:
            List[Tuple[int, float]]: List of (card_id, score) pairs sorted by score
        """
        # Evaluate each card
        rankings = [(card_id, self.evaluate_card(card_id, context)) for card_id in card_ids]
        
        # Sort by score (descending)
        rankings.sort(key=lambda x: x[1], reverse=True)
        
        return rankings
    
    def evaluate_deck(self, deck: List[int]) -> Dict[str, Any]:
        """
        Evaluate the overall quality of a deck.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dict: Evaluation results
        """
        gs = self.game_state
        
        # Basic stats
        card_count = len(deck)
        
        # Count card types
        type_counts = {
            'creature': 0,
            'instant': 0,
            'sorcery': 0,
            'artifact': 0,
            'enchantment': 0,
            'planeswalker': 0,
            'land': 0
        }
        
        # Count mana curve
        mana_curve = {
            '0': 0,
            '1': 0,
            '2': 0,
            '3': 0,
            '4': 0,
            '5': 0,
            '6+': 0
        }
        
        # Count colors
        color_counts = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}
        
        # Track card strengths
        card_strengths = []
        
        for card_id in deck:
            card = gs._safe_get_card(card_id)
            if not card:
                continue
                
            # Count card types
            if hasattr(card, 'card_types'):
                for card_type in card.card_types:
                    if card_type in type_counts:
                        type_counts[card_type] += 1
            
            # Count mana costs
            if hasattr(card, 'cmc'):
                if card.cmc == 0:
                    mana_curve['0'] += 1
                elif card.cmc == 1:
                    mana_curve['1'] += 1
                elif card.cmc == 2:
                    mana_curve['2'] += 1
                elif card.cmc == 3:
                    mana_curve['3'] += 1
                elif card.cmc == 4:
                    mana_curve['4'] += 1
                elif card.cmc == 5:
                    mana_curve['5'] += 1
                else:
                    mana_curve['6+'] += 1
            
            # Count colors
            if hasattr(card, 'colors'):
                for i, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                    if card.colors[i]:
                        color_counts[color] += 1
            
            # Evaluate card strength
            card_strength = self._calculate_base_value(card)
            card_strengths.append((card_id, card_strength))
        
        # Sort cards by strength
        card_strengths.sort(key=lambda x: x[1], reverse=True)
        
        # Calculate statistical metrics if available
        stats_metrics = {}
        if self.stats_tracker:
            deck_stats = self.stats_tracker.get_deck_stats(deck)
            if deck_stats:
                games_played = deck_stats.get("games", 0)
                wins = deck_stats.get("wins", 0)
                win_rate = wins / games_played if games_played > 0 else 0.0
                
                stats_metrics = {
                    "games_played": games_played,
                    "wins": wins,
                    "losses": deck_stats.get("losses", 0),
                    "win_rate": win_rate,
                    "avg_game_length": deck_stats.get("avg_game_length", 0)
                }
        
        # Calculate overall deck metrics
        deck_metrics = {
            "card_count": card_count,
            "type_counts": type_counts,
            "mana_curve": mana_curve,
            "color_counts": color_counts,
            "avg_card_strength": sum(s for _, s in card_strengths) / len(card_strengths) if card_strengths else 0,
            "top_cards": [(cid, score) for cid, score in card_strengths[:10]]
        }
        
        # Calculate deck balance score
        balance_score = self._calculate_deck_balance(type_counts, mana_curve, color_counts)
        
        # Compile evaluation results
        evaluation = {
            "deck_metrics": deck_metrics,
            "stats_metrics": stats_metrics,
            "balance_score": balance_score,
            "overall_rating": self._calculate_overall_rating(deck_metrics, stats_metrics, balance_score)
        }
        
        return evaluation
    
    def _calculate_deck_balance(self, type_counts, mana_curve, color_counts):
        """Calculate how well-balanced a deck is."""
        balance_score = 0.0
        
        # Check land count (around 24 is standard for 60-card decks)
        land_count = type_counts['land']
        land_score = 1.0 - abs(land_count - 24) / 12  # Penalty for deviation
        balance_score += land_score * 0.3
        
        # Check creature count (around 20-25 is standard)
        creature_count = type_counts['creature']
        if 18 <= creature_count <= 26:
            creature_score = 1.0
        else:
            creature_score = 1.0 - abs(creature_count - 22) / 15
        balance_score += creature_score * 0.2
        
        # Check mana curve (should be bell-shaped centered on 2-3 CMC)
        curve_score = 0.0
        ideal_curve = {'0': 0.05, '1': 0.15, '2': 0.25, '3': 0.25, '4': 0.15, '5': 0.1, '6+': 0.05}
        
        total_nonland = sum(v for k, v in type_counts.items() if k != 'land')
        for cmc, count in mana_curve.items():
            if total_nonland > 0:
                actual_pct = count / total_nonland
                ideal_pct = ideal_curve[cmc]
                curve_score += 1.0 - abs(actual_pct - ideal_pct) / ideal_pct
        
        curve_score /= len(mana_curve)
        balance_score += curve_score * 0.3
        
        # Check color balance
        color_balance = 0.0
        used_colors = sum(1 for c, count in color_counts.items() if count > 0)
        
        if used_colors == 1:
            # Mono-colored: should have many cards of that color
            main_color = max(color_counts.items(), key=lambda x: x[1])[0]
            color_concentration = color_counts[main_color] / sum(color_counts.values())
            color_balance = color_concentration
        elif used_colors == 2:
            # Two colors: should be roughly balanced
            sorted_colors = sorted(color_counts.items(), key=lambda x: x[1], reverse=True)
            top_two = [c for c, count in sorted_colors[:2]]
            top_two_count = sum(color_counts[c] for c in top_two)
            color_balance = top_two_count / sum(color_counts.values())
        else:
            # 3+ colors: need good mana fixing
            land_pct = land_count / sum(type_counts.values())
            color_balance = land_pct  # More lands needed for multicolor
        
        balance_score += color_balance * 0.2
        
        return balance_score
    
    def _calculate_overall_rating(self, deck_metrics, stats_metrics, balance_score):
        """Calculate an overall deck rating."""
        # Base rating from card strength
        avg_card_strength = deck_metrics["avg_card_strength"]
        card_rating = avg_card_strength / 2.0  # Normalize to ~0-1 range
        
        # Factor in balance score
        balance_rating = balance_score
        
        # Factor in statistics if available
        stats_rating = 0.0
        if "win_rate" in stats_metrics:
            win_rate = stats_metrics["win_rate"]
            games_played = stats_metrics["games_played"]
            
            # Weight by confidence (more games = more confidence)
            confidence = min(games_played / 50, 1.0)
            stats_rating = win_rate * confidence
        
        # Calculate weighted average
        if stats_rating > 0:
            overall_rating = 0.4 * card_rating + 0.3 * balance_rating + 0.3 * stats_rating
        else:
            overall_rating = 0.6 * card_rating + 0.4 * balance_rating
        
        # Scale to 0-10 range
        scaled_rating = overall_rating * 10
        
        return round(scaled_rating, 1)