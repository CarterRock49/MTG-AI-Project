"""Decision making: action recommendation, sequencing, and Monte Carlo tree search.

Extracted from strategic_planner.py. This module defines behavior only (a mixin);
all state lives on MTGStrategicPlanner, which composes every mixin.
"""

import logging
import math
import numpy as np
import random

def _card_number(card, attribute, default=0.0):
    try:
        value = float(getattr(card, attribute, default) or 0)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


class MCTSNode:
    """Node for Monte Carlo Tree Search."""
    
    def __init__(self, game_state=None, parent=None, action=None, prior_prob=0.0):
        """
        Initialize an MCTS node.
        
        Args:
            game_state: The game state at this node
            parent: Parent node
            action: Action leading to this node from parent
            prior_prob: Prior probability of selecting this node
        """
        self.game_state = game_state
        self.parent = parent
        self.action = action  # Action that led to this state
        self.children = {}  # Map from actions to child nodes
        self.visit_count = 0
        self.value_sum = 0.0  # Sum of values from rollouts
        self.prior_prob = prior_prob
        self.is_expanded = False
        
    def value(self):
        """Get the average value of this node."""
        if self.visit_count == 0:
            return 0.0
        return self.value_sum / self.visit_count
    
    def select_child(self, c_puct=1.0):
        """
        Select the child with the highest UCB score.
        
        Args:
            c_puct: Exploration constant
            
        Returns:
            The selected child node and action
        """
        # Find child with highest UCB score
        best_score = -float('inf')
        best_action = -1
        best_child = None
        
        # UCB formula: Q(s,a) + c_puct * P(s,a) * sqrt(sum(N(s,b)))/(1 + N(s,a))
        total_visit_count = sum(child.visit_count for child in self.children.values())
        
        for action, child in self.children.items():
            # Exploitation term
            q_value = child.value()
            
            # Exploration term
            exploration = c_puct * child.prior_prob * (total_visit_count ** 0.5) / (1 + child.visit_count)
            
            # UCB score
            ucb_score = q_value + exploration
            
            if ucb_score > best_score:
                best_score = ucb_score
                best_action = action
                best_child = child
        
        return best_action, best_child
    
    def expand(self, valid_actions, action_priors=None):
        """
        Expand the node by adding children for all valid actions.
        
        Args:
            valid_actions: List of valid action indices
            action_priors: Optional map from actions to prior probabilities
        """
        if action_priors is None:
            # Use uniform priors if no policy provided
            action_priors = {a: 1.0/len(valid_actions) for a in valid_actions}
        
        for action in valid_actions:
            if action not in self.children:
                self.children[action] = MCTSNode(
                    parent=self,
                    action=action,
                    prior_prob=action_priors.get(action, 1.0/len(valid_actions))
                )
        
        self.is_expanded = True


class SearchDecisionMixin:
    """Decision making: action recommendation, sequencing, and Monte Carlo tree search."""

    __slots__ = ()

    def project_future_states(self, num_turns=7):  # Increased from 5 to 7
        """
        Project the game state values for future turns based on current trajectory.
        
        Args:
            num_turns: Number of turns to project into the future
            
        Returns:
            numpy.ndarray: Projected state values for each future turn
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Current state metrics
        my_creatures = [cid for cid in me["battlefield"] if gs._safe_get_card(cid) and 'creature' in gs._safe_get_card(cid).card_types]
        opp_creatures = [cid for cid in opp["battlefield"] if gs._safe_get_card(cid) and 'creature' in gs._safe_get_card(cid).card_types]
        
        my_power = sum(_card_number(gs._safe_get_card(cid), 'power') for cid in my_creatures if gs._safe_get_card(cid))
        opp_power = sum(_card_number(gs._safe_get_card(cid), 'power') for cid in opp_creatures if gs._safe_get_card(cid))
        
        life_diff = me["life"] - opp["life"]
        card_diff = len(me["hand"]) - len(opp["hand"])
        
        # Enhanced projection model with Monte Carlo simulations
        projections = np.zeros(num_turns, dtype=np.float32)
        
        # Base trajectory using current advantage
        board_adv = (len(my_creatures) - len(opp_creatures)) / max(1, len(my_creatures) + len(opp_creatures))
        power_adv = (my_power - opp_power) / max(1, my_power + opp_power)
        
        # Project for each turn
        for i in range(num_turns):
            turn = gs.turn + i + 1
            
            # More weight to current advantages in earlier projections, 
            # more uncertainty in later turns
            certainty = 1.0 / (i + 1)
            
            # Project life difference based on board state
            projected_life_diff = life_diff
            
            # Simulate potential damage each turn based on current board state
            if board_adv > 0:  # We're ahead on board
                # Project us dealing damage
                projected_damage_per_turn = max(0, my_power - opp_power/2)  # Account for blocking
                projected_life_diff += projected_damage_per_turn * (i + 1) * (0.9 ** i)  # Diminish impact over time
            else:  # Opponent ahead or even
                # Project opponent dealing damage
                projected_damage_per_turn = max(0, opp_power - my_power/2)  # Account for blocking
                projected_life_diff -= projected_damage_per_turn * (i + 1) * (0.9 ** i)  # Diminish impact over time
            
            # Project card advantage with expected card draw
            expected_cards_drawn = i + 1  # One card per turn
            projected_card_diff = card_diff + (0.2 * expected_cards_drawn)  # Assume slight card advantage over time
            projected_card_diff *= (0.9 ** i)  # Card advantage diminishes impact over time
            
            # Estimate board development based on cards in hand
            estimated_new_permanents = min(3, len(me["hand"])) * (0.8 ** i)  # Assume we play our hand
            estimated_opp_permanents = min(3, len(opp["hand"])) * (0.8 ** i)  # Assume opponent plays their hand
            projected_board_adv = board_adv + (estimated_new_permanents - estimated_opp_permanents) * 0.1 * (0.9 ** i)
            
            # Combine factors into an overall state value (-1 to +1 scale)
            state_value = (
                0.5 * np.tanh(projected_life_diff / 10) +  # Life difference (normalized with tanh)
                0.3 * np.tanh(projected_card_diff / 3) +   # Card advantage
                0.2 * np.tanh(projected_board_adv * 2)     # Board presence (normalized with tanh)
            ) * certainty
            
            projections[i] = state_value
        
        return projections

    def suggest_mulligan_decision(self, hand, deck_name=None, on_play=True):
        """
        Suggest a mulligan decision based on the initial hand with enhanced evaluation.
        
        Args:
            hand: List of card IDs in the hand
            deck_name: Name of the deck being played
            on_play: Whether the player is going first (True) or on the draw (False)
            
        Returns:
            dict: Decision and explanation
        """
        gs = self.game_state
        
        # Initialize analysis components
        hand_size = len(hand)
        lands = 0
        creatures = 0
        low_drops = 0  # Cards with CMC <= 2
        curve_cards = 0  # Cards with 3 <= CMC <= 5
        high_drops = 0  # Cards with CMC > 5
        
        # Card objects for deeper analysis
        cards = [gs._safe_get_card(card_id) for card_id in hand]
        cards = [card for card in cards if card]  # Filter None values
        
        # Basic hand metrics
        for card in cards:
            if hasattr(card, 'type_line') and 'land' in card.type_line.lower():
                lands += 1
            
            if hasattr(card, 'card_types') and 'creature' in card.card_types:
                creatures += 1
            
            if hasattr(card, 'cmc'):
                if card.cmc <= 2:
                    low_drops += 1
                elif 3 <= card.cmc <= 5:
                    curve_cards += 1
                else:
                    high_drops += 1
        
        # Initialize decision components
        decision = {
            "keep": False,
            "land_status": "balanced",  # balanced, flood, screw
            "curve_status": "balanced",  # balanced, top-heavy, bottom-heavy
            "has_action": False,         # early plays available
            "has_interaction": False,    # has removal/counterspells
            "has_synergy": False,        # cards work well together
            "strength": 0.0,             # 0.0 to 1.0
            "reasoning": []
        }
        
        # Analyze lands
        land_ratio = lands / hand_size
        
        if land_ratio < 0.2:
            decision["land_status"] = "screw"
            decision["reasoning"].append(f"Too few lands ({lands}/{hand_size})")
        elif land_ratio > 0.6:
            decision["land_status"] = "flood"
            decision["reasoning"].append(f"Too many lands ({lands}/{hand_size})")
        else:
            decision["reasoning"].append(f"Good land count ({lands}/{hand_size})")
        
        # Analyze curve
        if low_drops == 0 and lands < 3:
            decision["curve_status"] = "top-heavy"
            decision["reasoning"].append("No early plays")
        elif high_drops > 2 and lands < 4:
            decision["curve_status"] = "top-heavy"
            decision["reasoning"].append("Too many expensive cards")
        elif low_drops >= 2 and lands >= 2:
            decision["has_action"] = True
            decision["reasoning"].append("Has early plays")
        
        # Check for interaction
        for card in cards:
            if hasattr(card, 'oracle_text'):
                text = card.oracle_text.lower()
                if any(keyword in text for keyword in ['destroy', 'exile', 'counter', 'return', 'damage']):
                    decision["has_interaction"] = True
                    decision["reasoning"].append("Has interaction")
                    break
        
        # Check for synergy between cards
        synergy_count = 0
        if len(cards) >= 2:
            # Check for tribal synergies (creature types)
            creature_types = {}
            for card in cards:
                if hasattr(card, 'subtypes'):
                    for subtype in card.subtypes:
                        if subtype not in creature_types:
                            creature_types[subtype] = 0
                        creature_types[subtype] += 1
            
            # Count types with multiple cards
            synergy_count += sum(1 for count in creature_types.values() if count >= 2)
            
            # Check for color synergy
            colors = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}
            for card in cards:
                if hasattr(card, 'colors'):
                    for i, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                        if i < len(card.colors) and card.colors[i]:
                            colors[color] += 1
            
            # Hand is focused on 1-2 colors
            color_focus = sum(1 for count in colors.values() if count >= 2)
            if color_focus <= 2:
                synergy_count += 1
        
        if synergy_count > 0:
            decision["has_synergy"] = True
            decision["reasoning"].append(f"Hand has synergy ({synergy_count} synergy points)")
        
        # Ensure we've detected the deck archetype
        if not hasattr(self, 'strategy_type') or not self.strategy_type:
            self._detect_deck_archetype()
        
        # Adapt decision based on deck archetype
        deck_specific_score = 0
        
        if self.strategy_type == "aggro":
            # Aggro wants creatures and low drops
            if creatures >= 2 and low_drops >= 2 and lands >= 2:
                deck_specific_score += 0.3
                decision["reasoning"].append("Good aggro hand")
            if land_ratio > 0.5:
                deck_specific_score -= 0.2
                decision["reasoning"].append("Too many lands for aggro")
        
        elif self.strategy_type == "control":
            # Control wants interaction and lands
            if decision["has_interaction"] and lands >= 3:
                deck_specific_score += 0.3
                decision["reasoning"].append("Good control hand")
            if lands < 3:
                deck_specific_score -= 0.3
                decision["reasoning"].append("Too few lands for control")
        
        elif self.strategy_type == "midrange":
            # Midrange wants a mix of early and mid-game plays
            if low_drops >= 1 and curve_cards >= 1 and lands >= 3:
                deck_specific_score += 0.3
                decision["reasoning"].append("Balanced midrange hand")
        
        elif self.strategy_type == "combo":
            # Combo wants combo pieces or ways to find them
            combo_pieces = self._identify_combo_pieces(hand)
            if combo_pieces:
                pieces_needed = combo_pieces.get("needed", 3)
                pieces_have = combo_pieces.get("have", 0)
                if pieces_have >= pieces_needed - 1:
                    deck_specific_score += 0.5
                    decision["reasoning"].append("Has most combo pieces")
                elif pieces_have >= 1:
                    deck_specific_score += 0.2
                    decision["reasoning"].append("Has some combo pieces")
        
        # Consider whether on play or draw
        if not on_play:  # On the draw
            # More forgiving on land counts
            if decision["land_status"] == "screw" and lands == 1:
                decision["reasoning"].append("Drawing an extra card helps with low lands")
                deck_specific_score += 0.1
        
        # Calculate overall hand strength
        base_strength = 0.5  # Start at neutral
        
        # Land status affects strength
        if decision["land_status"] == "balanced":
            base_strength += 0.2
        else:
            base_strength -= 0.3
        
        # Action affects strength
        if decision["has_action"]:
            base_strength += 0.2
        
        # Interaction affects strength
        if decision["has_interaction"]:
            base_strength += 0.1
        
        # Synergy affects strength
        if decision["has_synergy"]:
            base_strength += 0.2
        
        # Add deck-specific score
        decision["strength"] = min(1.0, max(0.0, base_strength + deck_specific_score))
        
        # Make final decision
        # More conservative with fewer cards in hand (due to previous mulligans)
        mulligan_threshold = 0.6 - (0.1 * (7 - hand_size))
        decision["keep"] = decision["strength"] >= mulligan_threshold
        
        # Add final reasoning
        if decision["keep"]:
            decision["reasoning"].append(f"Hand strength {decision['strength']:.2f} meets threshold {mulligan_threshold:.2f}")
        else:
            decision["reasoning"].append(f"Hand strength {decision['strength']:.2f} below threshold {mulligan_threshold:.2f}")
        
        # Log decision
        logging.debug(f"Mulligan decision for {deck_name or 'unknown deck'}: {'KEEP' if decision['keep'] else 'MULLIGAN'}")
        logging.debug(f"Hand composition: {lands} lands, {creatures} creatures, {low_drops} low drops, {curve_cards} mid drops")
        
        return decision

    def find_best_play_sequence(self, valid_actions, depth=None, discount_factor=0.9):
        """
        Find the best sequence of plays looking ahead by dynamic depth using enhanced pruning.
        
        Args:
            valid_actions: List of valid action indices
            depth: Number of plays ahead to look (default None - will be dynamically set)
            discount_factor: Discount factor for future actions (default 0.9)
            
        Returns:
            List: Best sequence of actions
            float: Value of the sequence
        """
        gs = self.game_state
        
        # Dynamically set depth based on game state complexity
        if depth is None:
            # Start with base depth
            base_depth = 3
            
            # Reduce depth for complex board states
            battlefield_size = len(gs.p1["battlefield"]) + len(gs.p2["battlefield"])
            if battlefield_size > 10:
                base_depth -= 1
                
            # Reduce depth if too many valid actions (combinatorial explosion)
            if len(valid_actions) > 15:
                base_depth -= 1
                
            # Never go below minimum depth
            depth = max(1, base_depth)
            
            logging.debug(f"Dynamic search depth: {depth} (board size: {battlefield_size}, actions: {len(valid_actions)})")
        
        # Base case - evaluate current position
        if depth == 0:
            analysis = self.analyze_game_state()
            return [], analysis["position"]["score"]
        
        best_sequence = []
        best_value = -float('inf')
        
        # For pruning, first evaluate each valid action quickly
        action_evaluations = []
        for action_idx in valid_actions:
            action_type, param = gs.action_handler.get_action_info(action_idx)
            
            # Get a quick estimate of action value for pruning
            if action_type == "PLAY_CARD":
                value = self.evaluate_play_card_action(param)
            elif action_type == "DECLARE_ATTACKER":
                value = self._quick_action_evaluation(gs, action_type, param)
            elif action_type == "ACTIVATE_ABILITY":
                card_id, ability_idx = param
                value, _ = self.evaluate_ability_activation(card_id, ability_idx)
            else:
                value = self._quick_action_evaluation(gs, action_type, param)
            
            action_evaluations.append((action_idx, value))
        
        # Sort by value and take only the top N actions to consider
        action_evaluations.sort(key=lambda x: x[1], reverse=True)
        
        # Adaptive pruning - consider more actions in early depths, fewer in deeper depths
        search_width = max(3, 8 - depth * 2)  # Start with 8 at depth 1, 6 at depth 2, etc.
        
        # Ensure we keep at least the top 3 actions regardless of value
        pruned_actions = [a for a, _ in action_evaluations[:search_width]]
        
        # Add any very high-value actions that might have been pruned
        pruned_actions.extend([a for a, v in action_evaluations[search_width:] if v > 0.8])
        
        # Add land plays if we haven't played a land yet
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        if gs.can_play_land_this_turn(me):
            land_plays = [a for a, _ in action_evaluations 
                        if gs.action_handler.get_action_info(a)[0] == "PLAY_CARD" and 
                        hasattr(gs._safe_get_card(gs.action_handler.get_action_info(a)[1]), 'type_line') and
                        'land' in gs._safe_get_card(gs.action_handler.get_action_info(a)[1]).type_line]
            pruned_actions.extend(land_plays)
        
        # Uncertainty bonus - randomly add a few additional actions for exploration
        if depth == 1 and random.random() < self.risk_tolerance:
            unexplored_actions = [a for a, _ in action_evaluations[search_width:] 
                            if a not in pruned_actions and len(pruned_actions) < 10]
            if unexplored_actions:
                explore_count = min(2, len(unexplored_actions))
                random_explores = random.sample(unexplored_actions, explore_count)
                pruned_actions.extend(random_explores)
        
        # For logging purposes
        logging.debug(f"Considering {len(pruned_actions)} actions out of {len(valid_actions)} at depth {depth}")
        
        # Try each pruned action
        for action_idx in pruned_actions:
            action_type, param = gs.action_handler.get_action_info(action_idx)
            
            # Create a copy of the game state for simulation
            gs_copy = gs.clone()
            
            # Apply the action
            gs_copy.action_handler.apply_action(action_type, param)
            
            # Get new valid actions
            new_valid_actions = gs_copy.action_handler.generate_valid_actions()
            
            # If no valid actions, evaluate end state
            if not new_valid_actions:
                # This is a terminal state (e.g., end of turn)
                analysis = self.analyze_game_state(gs_copy)
                sequence_value = analysis["position"]["score"]
                if sequence_value > best_value:
                    best_value = sequence_value
                    best_sequence = [action_idx]
                continue
            
            # Recursively find best sequence
            seq, value = self.find_best_play_sequence(new_valid_actions, depth-1, discount_factor)
            
            # Apply discount factor for future actions
            discounted_value = value * discount_factor
            
            # Current action's immediate value
            immediate_value = 0.0
            
            # Get immediate value based on action type
            if action_type == "PLAY_CARD":
                immediate_value = self.evaluate_play_card_action(param) * (1 - discount_factor)
            elif action_type == "DECLARE_ATTACKER":
                immediate_value = self.evaluate_attack_action(param) * (1 - discount_factor)
            elif action_type == "ACTIVATE_ABILITY":
                card_id, ability_idx = param
                immediate_value, _ = self.evaluate_ability_activation(card_id, ability_idx)
                immediate_value *= (1 - discount_factor)
            
            # Combine immediate and future value
            total_value = immediate_value + discounted_value
            
            # Apply strategy-specific value adjustments
            # For aggressive strategies, value immediate board impact more
            if self.strategy_type == "aggro" and action_type in ["PLAY_CARD", "DECLARE_ATTACKER"]:
                total_value += 0.1 * self.aggression_level
            # For control strategies, value card advantage and removal more
            elif self.strategy_type == "control" and action_type == "ACTIVATE_ABILITY":
                if "draw" in str(param) or "destroy" in str(param):
                    total_value += 0.1 * (1 - self.aggression_level)
            
            if total_value > best_value:
                best_value = total_value
                best_sequence = [action_idx] + seq
        
        return best_sequence, best_value

    def plan_multi_turn_sequence(self, depth=3):
        """
        Enhanced multi-turn sequence planning with advanced mana and resource management.
        Now with improved outcome probability modeling and risk assessment.
        
        Args:
            depth: Number of turns to plan ahead
            
        Returns:
            List of turn plans with detailed strategic insights
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Analyze hand for multi-turn play potential
        hand = [gs._safe_get_card(cid) for cid in me["hand"]]
        hand = [card for card in hand if card]  # Filter out None
        
        # Advanced sorting considering strategic value and synergies
        hand.sort(key=lambda card: (
            -self.evaluate_card_for_sequence(card),  # Highest strategic value first
            card.cmc if hasattr(card, 'cmc') else float('inf')  # Secondary sort by CMC
        ))
        
        # Get current battlefield state
        my_lands = [gs._safe_get_card(cid) for cid in me["battlefield"] 
                if gs._safe_get_card(cid) and 
                hasattr(gs._safe_get_card(cid), 'type_line') and 
                'land' in gs._safe_get_card(cid).type_line]
        
        # Current available mana (enhanced estimation)
        current_mana = {}
        for color in ['W', 'U', 'B', 'R', 'G', 'C']:
            current_mana[color] = me["mana_pool"].get(color, 0)
        
        # Estimate mana development curve
        def project_mana_development(current_turn, turns_ahead):
            """Project mana availability for future turns"""
            # Start with current lands
            land_count = len(my_lands)
            
            # Count lands in hand
            lands_in_hand = len([card for card in hand if hasattr(card, 'type_line') and 'land' in card.type_line])
            
            # Assume land drop each turn if possible
            projected_lands = []
            for i in range(turns_ahead):
                # Add a land if we have one
                if i < lands_in_hand:
                    land_count += 1
                else:
                    # Probability of drawing a land
                    draw_chance = 0.4  # 40% chance to draw a land
                    if random.random() < draw_chance:
                        land_count += 1
                
                # Store projected land count for this turn
                projected_lands.append(land_count)
            
            # Convert to mana availability
            return projected_lands
        
        # Advanced multi-turn planning
        turn_plans = []
        remaining_hand = hand.copy()
        lands_played = 0
        
        # Project mana development
        mana_projection = project_mana_development(gs.turn, depth)
        
        # First, analyze current game state and objectives
        current_analysis = self.analyze_game_state()
        position = current_analysis["position"]["overall"]
        game_stage = current_analysis["game_info"]["game_stage"]
        
        # Identify win conditions
        win_conditions = self.identify_win_conditions()
        primary_win_condition = None
        for wc_name, wc_data in win_conditions.items():
            if wc_data["viable"] and (primary_win_condition is None or 
                                    win_conditions[primary_win_condition]["score"] < wc_data["score"]):
                primary_win_condition = wc_name
        
        # Identify opponent threats
        opponent_threats = self.assess_threats()
        
        for turn_idx in range(depth):
            current_turn = gs.turn + turn_idx
            expected_mana = mana_projection[turn_idx]
            
            # Define turn objectives based on game state
            objectives = []
            
            if game_stage == "early":
                objectives.append({"type": "develop_mana", "priority": 0.9})
                objectives.append({"type": "develop_board", "priority": 0.8})
                
                if position in ["behind", "struggling"]:
                    objectives.append({"type": "stabilize", "priority": 1.0})
            
            elif game_stage == "mid":
                if position in ["ahead", "dominating"]:
                    objectives.append({"type": "press_advantage", "priority": 0.9})
                    objectives.append({"type": "develop_win_condition", "priority": 0.8})
                else:
                    objectives.append({"type": "stabilize", "priority": 0.9})
                    objectives.append({"type": "develop_board", "priority": 0.8})
                    
                # Always want to make land drops
                objectives.append({"type": "develop_mana", "priority": 0.7})
            
            else:  # Late game
                if primary_win_condition:
                    objectives.append({"type": "execute_win_condition", "priority": 1.0, 
                                    "win_condition": primary_win_condition})
                
                if position in ["behind", "struggling"]:
                    objectives.append({"type": "stabilize", "priority": 0.9})
            
            # If threats exist, address them
            if opponent_threats:
                objectives.append({"type": "remove_threats", "priority": 0.8, 
                                "threats": [t["card_id"] for t in opponent_threats[:2]]})
            
            # Sort objectives by priority
            objectives.sort(key=lambda x: x["priority"], reverse=True)
            
            # Create turn plan
            turn_plan = {
                "turn": current_turn,
                "expected_mana": expected_mana,
                "objectives": objectives,
                "plays": [],
                "land_play": None,
                "spells": [],
                "abilities": [],
                "expected_value": 0.0
            }
            
            # First priority: Land drop if available
            if turn_idx == 0 or lands_played < turn_idx:
                lands_in_hand = [card for card in remaining_hand 
                            if hasattr(card, 'type_line') and 'land' in card.type_line]
                
                if lands_in_hand:
                    # Choose best land (e.g., based on color needs)
                    best_land = lands_in_hand[0]  # Simple selection, could be enhanced
                    
                    turn_plan["land_play"] = {
                        "card": best_land,
                        "name": best_land.name if hasattr(best_land, 'name') else "Unknown Land",
                        "value": 0.7  # High value for playing lands
                    }
                    
                    lands_played += 1
                    remaining_hand.remove(best_land)
            
            # Estimate available mana for this turn
            available_mana = expected_mana
            
            # Plan plays based on objectives
            for objective in objectives:
                obj_type = objective["type"]
                
                if obj_type == "develop_mana" and turn_plan["land_play"]:
                    # Already handled land play
                    continue
                    
                elif obj_type == "develop_board":
                    # Look for good creatures to play
                    for card in remaining_hand[:]:
                        if (hasattr(card, 'card_types') and 'creature' in card.card_types and
                            hasattr(card, 'cmc') and card.cmc <= available_mana):
                            
                            # Value of this play
                            play_value = self.evaluate_card_for_sequence(card)
                            
                            # Add to plan
                            turn_plan["spells"].append({
                                "card": card,
                                "name": card.name if hasattr(card, 'name') else "Unknown Card",
                                "cmc": card.cmc,
                                "type": "creature",
                                "value": play_value,
                                "objective": obj_type
                            })
                            
                            # Update resources
                            available_mana -= card.cmc
                            remaining_hand.remove(card)
                            
                            # Don't plan too many plays per turn
                            if len(turn_plan["spells"]) >= 2:
                                break
                
                elif obj_type == "stabilize":
                    # Look for removal or defensive plays
                    for card in remaining_hand[:]:
                        if hasattr(card, 'oracle_text') and hasattr(card, 'cmc') and card.cmc <= available_mana:
                            oracle_text = card.oracle_text.lower()
                            
                            # Check for removal or defensive effects
                            is_removal = any(term in oracle_text for term in ['destroy', 'exile', 'damage to'])
                            is_defensive = ('gain life' in oracle_text or 
                                        'prevent' in oracle_text or 
                                        ('create' in oracle_text and 'token' in oracle_text))
                            
                            if is_removal or is_defensive:
                                # Value of this play
                                play_value = self.evaluate_card_for_sequence(card) * 1.2  # Boost value for stabilizing
                                
                                # Add to plan
                                turn_plan["spells"].append({
                                    "card": card,
                                    "name": card.name if hasattr(card, 'name') else "Unknown Card",
                                    "cmc": card.cmc,
                                    "type": "removal" if is_removal else "defensive",
                                    "value": play_value,
                                    "objective": obj_type
                                })
                                
                                # Update resources
                                available_mana -= card.cmc
                                remaining_hand.remove(card)
                                
                                # Emergency stabilization is highest priority
                                break
                
                elif obj_type == "remove_threats" and "threats" in objective:
                    # Focus on removing specific threats
                    target_threats = objective["threats"]
                    
                    for card in remaining_hand[:]:
                        if hasattr(card, 'oracle_text') and hasattr(card, 'cmc') and card.cmc <= available_mana:
                            oracle_text = card.oracle_text.lower()
                            
                            # Check if this can remove threats
                            is_removal = any(term in oracle_text for term in ['destroy', 'exile', 'damage to'])
                            
                            if is_removal:
                                # Value this higher if we're addressing priority threats
                                play_value = self.evaluate_card_for_sequence(card) * 1.5  # High priority for threat removal
                                
                                # Add to plan
                                turn_plan["spells"].append({
                                    "card": card,
                                    "name": card.name if hasattr(card, 'name') else "Unknown Card",
                                    "cmc": card.cmc,
                                    "type": "removal",
                                    "value": play_value,
                                    "objective": obj_type,
                                    "targets": [t for t in target_threats]
                                })
                                
                                # Update resources
                                available_mana -= card.cmc
                                remaining_hand.remove(card)
                                
                                # Prioritize removing the biggest threat
                                break
                
                elif obj_type == "execute_win_condition" and "win_condition" in objective:
                    wc_type = objective["win_condition"]
                    
                    # Handle specific win condition types
                    if wc_type == "combat_damage":
                        # Look for creatures and combat enhancement
                        for card in remaining_hand[:]:
                            if hasattr(card, 'cmc') and card.cmc <= available_mana:
                                play_value = 0.0
                                
                                if hasattr(card, 'card_types') and 'creature' in card.card_types:
                                    # Creatures advance combat win condition
                                    play_value = self.evaluate_card_for_sequence(card) * 1.3
                                
                                elif hasattr(card, 'oracle_text'):
                                    oracle_text = card.oracle_text.lower()
                                    # Combat tricks also help
                                    if '+1/+1' in oracle_text or 'gets +' in oracle_text:
                                        play_value = self.evaluate_card_for_sequence(card) * 1.2
                                
                                if play_value > 0:
                                    # Add to plan
                                    turn_plan["spells"].append({
                                        "card": card,
                                        "name": card.name if hasattr(card, 'name') else "Unknown Card",
                                        "cmc": card.cmc,
                                        "type": "win_condition",
                                        "value": play_value,
                                        "objective": obj_type
                                    })
                                    
                                    # Update resources
                                    available_mana -= card.cmc
                                    remaining_hand.remove(card)
                                    
                                    # Don't plan too many plays per turn
                                    if len(turn_plan["spells"]) >= 2:
                                        break
                    
                    elif wc_type == "direct_damage":
                        # Look for burn spells
                        for card in remaining_hand[:]:
                            if (hasattr(card, 'oracle_text') and hasattr(card, 'cmc') and 
                                card.cmc <= available_mana and 'damage to' in card.oracle_text.lower()):
                                
                                # Direct damage spells are high value for burn strategy
                                play_value = self.evaluate_card_for_sequence(card) * 1.4
                                
                                # Add to plan
                                turn_plan["spells"].append({
                                    "card": card,
                                    "name": card.name if hasattr(card, 'name') else "Unknown Card",
                                    "cmc": card.cmc,
                                    "type": "burn",
                                    "value": play_value,
                                    "objective": obj_type
                                })
                                
                                # Update resources
                                available_mana -= card.cmc
                                remaining_hand.remove(card)
                                
                                # Burn strategies often want to chain multiple burn spells
                                if available_mana <= 1:  # Stop if we're out of significant mana
                                    break
            
            # Combine all plays into a single list for the turn
            all_plays = []
            if turn_plan["land_play"]:
                all_plays.append({
                    "type": "land",
                    "card": turn_plan["land_play"]["card"],
                    "name": turn_plan["land_play"]["name"],
                    "value": turn_plan["land_play"]["value"],
                    "objective": "develop_mana"
                })
            
            for spell in turn_plan["spells"]:
                all_plays.append({
                    "type": "spell",
                    "card": spell["card"],
                    "name": spell["name"],
                    "cmc": spell["cmc"],
                    "card_type": spell["type"],
                    "value": spell["value"],
                    "objective": spell["objective"]
                })
            
            # Calculate expected value of this turn's plan
            turn_value = sum(play["value"] for play in all_plays)
            turn_plan["expected_value"] = turn_value
            turn_plan["plays"] = all_plays
            
            # Add strategic insights
            turn_plan["insights"] = {
                "primary_objective": objectives[0]["type"] if objectives else "none",
                "mana_efficiency": len(all_plays) / max(1, expected_mana),
                "card_efficiency": len(all_plays) / max(1, len(remaining_hand) + len(all_plays)),
                "handles_threats": any(p.get("objective") == "remove_threats" for p in all_plays),
                "advances_win_condition": any(p.get("objective") == "execute_win_condition" for p in all_plays)
            }
            
            turn_plans.append(turn_plan)
            
            # Check if we're not making meaningful progress
            if not all_plays and turn_idx > 0:
                # If there are no plays for this turn, don't project further
                break
        
        # Final review - look for overall patterns and optimizations
        if turn_plans:
            logging.debug(f"Multi-turn plan generated for {len(turn_plans)} turns")
            logging.debug(f"Turn {turn_plans[0]['turn']} plan: {len(turn_plans[0]['plays'])} plays with priority on {turn_plans[0]['insights']['primary_objective']}")
            
            # Calculate overall plan quality
            avg_value = sum(plan["expected_value"] for plan in turn_plans) / len(turn_plans)
            logging.debug(f"Plan average value per turn: {avg_value:.2f}")
        
        return turn_plans

    def _is_critical_decision(self):
        """
        Determine if the current game state requires a critical decision.
        
        Returns:
            bool: True if current decision is critical
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # 1. Check if we're in a vulnerable state (low life)
        if me["life"] <= 5:
            return True
        
        # 2. Check if we're close to winning
        if opp["life"] <= 5:
            return True
        
        # 3. Check if there's a complex board state (many creatures)
        my_creatures = [cid for cid in me["battlefield"] 
                    if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'card_types') and 
                    'creature' in gs._safe_get_card(cid).card_types]
                    
        opp_creatures = [cid for cid in opp["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types]
                        
        if len(my_creatures) + len(opp_creatures) >= 6:
            return True
        
        # 4. Check for complex combat situations
        if gs.phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS]:
            if len(gs.current_attackers) >= 3 or any(len(blockers) >= 2 for blockers in gs.current_block_assignments.values()):
                return True
        
        # 5. Check for significant card advantage opportunities
        if len(me["hand"]) >= 4:
            return True
        
        # 6. Check for late game
        if gs.turn >= 8:
            return True
        
        # Default: not a critical decision
        return False

    def _determine_simulation_count(self):
        """
        Determine how many MCTS simulations to run based on game complexity.
        Includes safety checks for action_handler availability.

        Returns:
            int: Number of simulations to run
        """
        gs = self.game_state
        action_count = 10 # Default reasonable action count if handler fails

        # Base simulation count
        base_count = 100

        # Reduce for complex states to avoid timeouts
        battlefield_size = 0
        # --- ADDED: Safety check for player objects ---
        if hasattr(gs, 'p1') and gs.p1 and 'battlefield' in gs.p1:
            battlefield_size += len(gs.p1["battlefield"])
        if hasattr(gs, 'p2') and gs.p2 and 'battlefield' in gs.p2:
            battlefield_size += len(gs.p2["battlefield"])
        # --- END safety check ---

        if battlefield_size > 15:
            base_count = 50
        elif battlefield_size > 10:
            base_count = 75

        # Reduce for many valid actions (combinatorial explosion)
        # --- MODIFIED: Safety Check for action_handler ---
        if hasattr(gs, 'action_handler') and gs.action_handler:
            try:
                valid_actions = gs.action_handler.generate_valid_actions()
                action_count = np.sum(valid_actions)
                if action_count > 20:
                    base_count = max(30, base_count - 50)
            except Exception as e:
                logging.error(f"Error generating valid actions in _determine_simulation_count: {e}")
                # Use default action_count
        else:
            logging.warning("ActionHandler missing in _determine_simulation_count. Using default action count for complexity check.")
        # --- END modification ---

        # Increase for critical game phases
        current_phase = getattr(gs, 'phase', -1) # Safely get phase
        critical_phases = [
            getattr(gs, 'PHASE_DECLARE_ATTACKERS', -1),
            getattr(gs, 'PHASE_DECLARE_BLOCKERS', -1)
        ]
        if current_phase in critical_phases and current_phase != -1: # Ensure phase value is valid
            base_count = min(200, base_count + 50)

        # Scale based on turn (more computation for later turns)
        current_turn = getattr(gs, 'turn', 1) # Safely get turn
        turn_factor = min(1.5, 1.0 + (current_turn / 20))
        base_count = int(base_count * turn_factor)

        logging.debug(f"MCTS simulation count: {base_count} (board size: {battlefield_size}, actions: {action_count})")
        return base_count

    def recommend_action(self, valid_actions):
        """
        Provide a strategic recommendation for the next action with MCTS integration.
        Includes robust checks for game state and handlers.

        Args:
            valid_actions: List of valid action indices

        Returns:
            int: Recommended action index
        """
        try:
            gs = self.game_state
            # --- MODIFIED: Centralized Action Handler Check ---
            # Access action_handler VIA the game_state instance
            action_handler = getattr(gs, 'action_handler', None)
            if not action_handler:
                logging.error("Recommend action cannot proceed: gs.action_handler is missing.")
                # Attempt safe fallback based only on valid_actions list
                if valid_actions:
                    # Ensure valid_actions is a list or convert from mask
                    if isinstance(valid_actions, np.ndarray) and valid_actions.dtype == bool:
                         valid_actions = np.where(valid_actions)[0].tolist()
                    if not isinstance(valid_actions, list): valid_actions = [] # Ensure list

                    if 11 in valid_actions: return 11 # Prioritize PASS
                    if 12 in valid_actions: return 12 # Prioritize CONCEDE
                    if valid_actions: return valid_actions[0] # Last resort: first valid
                return None # No valid actions given either
            # --- END Modification ---

            # --- Initial GameState/Player validation ---
            if not gs or not hasattr(gs, 'p1') or not hasattr(gs, 'p2') or not gs.p1 or not gs.p2:
                logging.error("Recommend action failed: GameState or players not properly initialized.")
                return None if not valid_actions else valid_actions[0]

            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1

            # Handle case where no valid actions are provided
            if valid_actions is None: # Check for None explicitly
                 logging.warning("Valid_actions list is None in recommend_action.")
                 return None
            if isinstance(valid_actions, np.ndarray) and valid_actions.dtype == bool: # Handle mask case
                 valid_actions = np.where(valid_actions)[0].tolist()
            if not isinstance(valid_actions, list): # Ensure list type
                 logging.error(f"Valid actions provided is not a list or ndarray: {type(valid_actions)}")
                 return None
            if not valid_actions:
                logging.warning("No valid actions provided to recommend_action")
                # Check if PASS_PRIORITY or CONCEDE are possible as absolute fallback
                try:
                     # Use the verified handler instance
                     mask = action_handler.generate_valid_actions()
                     valid_now = mask.nonzero()[0]
                     if 11 in valid_now: return 11 # PASS
                     if 12 in valid_now: return 12 # CONCEDE
                except Exception as e:
                     logging.error(f"Error regenerating mask in recommend_action fallback: {e}")
                     pass # Ignore errors during absolute fallback check
                return None # No valid actions and cannot generate new ones


            # 1. Analyze current game state
            # Use self.analyze_game_state which checks if planner exists
            self.analyze_game_state()
            self.adapt_strategy()

            # 2. Check strategy memory for suggestions (Keep existing logic)
            memory_suggestion = None
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory: # Check if memory exists on GS
                try:
                    # Pass the list of valid actions
                    memory_suggestion = gs.strategy_memory.get_suggested_action(gs, valid_actions)
                    if memory_suggestion is not None and memory_suggestion in valid_actions:
                        # High confidence check
                        pattern = gs.strategy_memory.extract_strategy_pattern(gs)
                        strategy = gs.strategy_memory.strategies.get(pattern)
                        if strategy and strategy.get('success_rate', 0) > 0.8 and strategy.get('count', 0) > 5:
                            logging.debug(f"Using high-confidence memory-suggested action: {memory_suggestion}")
                            return memory_suggestion
                        logging.debug(f"Found memory suggestion: {memory_suggestion} (will consider)")
                    else:
                         memory_suggestion = None # Reset if invalid or not found
                except Exception as e:
                    logging.warning(f"Error getting strategy memory suggestion: {str(e)}")
                    memory_suggestion = None # Ensure it's None on error


            # 3. Determine if critical decision point - use MCTS
            is_critical_decision = self._is_critical_decision()

            if is_critical_decision and hasattr(self, 'monte_carlo_search'): # Check if MCTS exists
                logging.info("Critical decision point detected - using Monte Carlo Tree Search")
                simulation_count = self._determine_simulation_count() # Uses safe check inside
                mcts_action = self.monte_carlo_search(num_simulations=simulation_count) # Assumes MCTS handles missing handler

                if mcts_action is not None and mcts_action in valid_actions:
                    logging.debug(f"MCTS recommendation: {mcts_action}")
                    return mcts_action
                else:
                    logging.warning(f"MCTS selected invalid action {mcts_action} or failed. Falling back to heuristic.")


            # 4. Action prioritization (Heuristic approach or MCTS fallback) (Keep existing logic)
            action_priorities = []
            high_priority_actions = []

            # Check land play
            if gs.can_play_land_this_turn(me):
                 for action_idx in valid_actions:
                     # Use the verified handler instance
                     action_type, param = action_handler.get_action_info(action_idx)
                     if action_type == "PLAY_LAND" and param is not None:
                         card = gs._safe_get_card(param)
                         if card and 'land' in getattr(card, 'type_line', ''):
                             logging.debug("Prioritizing land play")
                             return action_idx # Immediate return for land drop


            # Check for lethal damage or threat removal (Keep existing logic)
            opp_life = opp.get('life', 20)
            try:
                 threats = self.assess_threats()[:3] # Assess threats safely
            except Exception:
                 threats = []

            for action_idx in valid_actions:
                # Use the verified handler instance
                action_type, param = action_handler.get_action_info(action_idx)
                if action_type is None: continue

                # Evaluate potential lethal attack
                if action_type == "DECLARE_ATTACKER" and param:
                    my_power = 0
                    # Correctly sum power based on param which should be attacker IDs or index?
                    # Assuming param IS the battlefield index for now, matching action dict
                    if isinstance(param, int) and param < len(me.get('battlefield',[])):
                        attacker_id = me['battlefield'][param]
                        att_card = gs._safe_get_card(attacker_id)
                        my_power = _card_number(att_card, 'power')
                    elif isinstance(param, list): # If param is list of IDs
                        for att_id in param:
                            att_card = gs._safe_get_card(att_id)
                            my_power += _card_number(att_card, 'power')
                    else: # Invalid param for attacker
                        continue

                    if my_power >= opp_life:
                          attack_value = self.evaluate_attack_action([attacker_id] if isinstance(param, int) else param)
                          high_priority_actions.append((action_idx, attack_value + 2.0, "Potential lethal attack"))

                # Evaluate removing top threat
                elif action_type in ["PLAY_SPELL", "ACTIVATE_ABILITY"]:
                    if threats and threats[0]:
                        top_threat = threats[0]
                        top_threat_id = top_threat.get("card_id")
                        can_remove = False
                        text_to_check = ""

                        # Adjust getting card/ability info
                        if action_type == "PLAY_SPELL" and param is not None and isinstance(param, int) and param < len(me.get('hand',[])):
                            card_id = me['hand'][param]
                            card = gs._safe_get_card(card_id)
                            text_to_check = getattr(card,'oracle_text','').lower() if card else ""
                        elif action_type == "ACTIVATE_ABILITY" and isinstance(param, tuple) and len(param) == 2:
                            ability = action_handler.get_ability_object(param[0], param[1]) # Use helper
                            text_to_check = getattr(ability, 'effect', getattr(ability, 'effect_text', '')).lower() if ability else ""
                        else: # Handle param being None for ACTIVATE_ABILITY if context used instead
                            # Need access to context here? Complex. Assume basic structure for now.
                            pass

                        if any(term in text_to_check for term in ['destroy', 'exile', 'damage', 'return target']):
                            can_remove = True

                        if can_remove:
                            threat_level = top_threat.get('level', 1.0)
                            value = 1.0 + threat_level * 0.5
                            high_priority_actions.append((action_idx, value, f"Remove threat {top_threat.get('name','N/A')}"))

            if high_priority_actions:
                high_priority_actions.sort(key=lambda x: x[1], reverse=True)
                logging.debug(f"Taking high priority action: {high_priority_actions[0][2]}")
                return high_priority_actions[0][0]

            # 5. Memory suggestion (re-checked) (Keep existing logic)
            if memory_suggestion is not None and memory_suggestion in valid_actions:
                 pattern = gs.strategy_memory.extract_strategy_pattern(gs)
                 strategy = gs.strategy_memory.strategies.get(pattern)
                 if strategy and strategy.get('success_rate', 0) > 0.6:
                     logging.debug(f"Using memory-suggested action: {memory_suggestion}")
                     return memory_suggestion

            # 6. Forward search / sequence evaluation (Keep existing logic)
            if hasattr(self, 'find_best_play_sequence') and self.find_best_play_sequence:
                try:
                    best_sequence, best_value = self.find_best_play_sequence(list(valid_actions), depth=2)
                    if best_sequence:
                        logging.debug(f"Best play sequence found with value {best_value}, taking action {best_sequence[0]}")
                        return best_sequence[0]
                except Exception as seq_e:
                     logging.warning(f"Error during find_best_play_sequence: {seq_e}")

            # 7. Individual action evaluation (Heuristics) (Keep existing logic, ensure safe get_action_info)
            action_evaluations = []
            for action_idx in valid_actions:
                # Use the verified handler instance
                action_type, param = action_handler.get_action_info(action_idx)
                if action_type is None: continue

                value, reason = 0.5, "Default Eval"
                try:
                    # Keep existing evaluation logic, assume context handled correctly by get_ability_object etc.
                    if action_type == "PLAY_SPELL" and param is not None and isinstance(param, int) and param < len(me.get("hand",[])):
                        value = self.evaluate_play_card_action(me['hand'][param]) if hasattr(self, 'evaluate_play_card_action') else 0.5
                        reason = "Card Play"
                    elif action_type == "DECLARE_ATTACKER" and param is not None: # Assuming param is battlefield_idx
                         if isinstance(param, int) and param < len(me.get('battlefield',[])):
                             value = self.evaluate_attack_action([me['battlefield'][param]]) if hasattr(self, 'evaluate_attack_action') else 0.5
                             reason = "Attack"
                         elif isinstance(param, list): # If action allows list of attackers
                              value = self.evaluate_attack_action(param) if hasattr(self, 'evaluate_attack_action') else 0.5
                              reason = "Multi-Attack"
                    elif action_type == "DECLARE_BLOCKER" and isinstance(param, tuple) and len(param)==2:
                        value = self.evaluate_block_action(param[0], param[1]) if hasattr(self, 'evaluate_block_action') else 0.5
                        reason = "Block"
                    elif action_type == "ACTIVATE_ABILITY":
                        # Get ability object safely using handler helper
                        ability_obj = action_handler.get_ability_object_from_context(action_idx) # Uses index lookup
                        if ability_obj:
                             bf_idx, internal_ability_idx = action_handler.get_indices_from_activate_action(action_idx)
                             value, reason = self.evaluate_ability_activation(bf_idx, internal_ability_idx) if hasattr(self, 'evaluate_ability_activation') else (0.5, "Ability Activation")
                    elif action_type in ["END_TURN", "PASS_PRIORITY"]:
                         is_stack_empty = not gs.stack
                         value = 0.2 if is_stack_empty else 0.1
                         reason = action_type
                    # Add other action types
                except Exception as eval_e:
                    logging.warning(f"Error evaluating action {action_idx} ({action_type}): {eval_e}")
                    value, reason = 0.05, "Eval Error Fallback"

                action_evaluations.append((action_idx, value, reason))

            action_evaluations.sort(key=lambda x: x[1], reverse=True)

            # 8. Exploration vs Exploitation (Keep existing logic)
            risk = getattr(self, 'risk_tolerance', 0.5) * 0.2
            if random.random() < risk and len(action_evaluations) > 1:
                top_n = min(3, len(action_evaluations))
                chosen_action, value, reason = random.choice(action_evaluations[:top_n])
                logging.debug(f"Exploration choice: {reason} (value={value:.2f}), Action: {chosen_action}")
                return chosen_action

            # 9. Choose best heuristic action (Keep existing logic)
            if action_evaluations:
                best_action, value, reason = action_evaluations[0]
                logging.debug(f"Best heuristic action: {reason} (value={value:.2f}), Action: {best_action}")
                return best_action

            # 10. Absolute Fallback (Keep existing logic)
            logging.error("Recommend_action reached end without selecting an action. No heuristics passed.")
            try:
                # Use verified handler instance
                mask = action_handler.generate_valid_actions()
                valid_now = mask.nonzero()[0]
                if 11 in valid_now: return 11 # PASS
                if 12 in valid_now: return 12 # CONCEDE
            except Exception: pass
            return None # Truly stuck

        except Exception as e:
            logging.error(f"CRITICAL Error in recommend_action: {str(e)}", exc_info=True)
            # Attempt graceful fallback
            if valid_actions and isinstance(valid_actions, list) and len(valid_actions) > 0:
                if 11 in valid_actions: return 11 # Prioritize PASS if possible
                return valid_actions[0] # Return first valid as absolute fallback
            return None # Truly stuck if no valid actions list available

    def suggest_action_from_memory(self, valid_actions):
        """
        Use strategy memory to suggest an action.
        
        Args:
            valid_actions: List of valid action indices
            
        Returns:
            int or None: Suggested action index
        """
        try:
            # Check if strategy memory exists in game state
            if not hasattr(self.game_state, 'strategy_memory'):
                return None
            
            # Get current game state pattern
            current_pattern = self.game_state.strategy_memory.extract_strategy_pattern(self.game_state)
            
            # Find strategies matching the current game state
            matching_strategies = [
                (pattern, strategy) for pattern, strategy in self.game_state.strategy_memory.strategies.items()
                if pattern == current_pattern and strategy['success_rate'] > 0.6 and strategy['count'] > 3
            ]
            
            # If no exact match, try partial matching
            if not matching_strategies:
                matching_strategies = [
                    (pattern, strategy) for pattern, strategy in self.game_state.strategy_memory.strategies.items()
                    if self._pattern_similarity(pattern, current_pattern) > 0.7
                ]
            
            # Sort strategies by success rate and count
            matching_strategies.sort(
                key=lambda x: x[1]['success_rate'] * x[1]['count'], 
                reverse=True
            )
            
            # Find matching action sequences
            for pattern, _ in matching_strategies:
                matching_sequences = [
                    (seq[0], reward) for seq, reward in self.game_state.strategy_memory.action_sequences
                    if len(seq) > 0 and seq[0] in valid_actions and reward > 0
                ]
                
                # Weighted random selection if matches found
                if matching_sequences:
                    weights = [max(0.1, r) for _, r in matching_sequences]
                    chosen_action = random.choices(
                        [a for a, _ in matching_sequences], 
                        weights=[w/sum(weights) for w in weights]
                    )[0]
                    
                    logging.debug(f"Strategy memory suggested action {chosen_action}")
                    return chosen_action
            
            return None
        
        except Exception as e:
            logging.error(f"Error in suggest_action_from_memory: {str(e)}")
            return None

    def _pattern_similarity(self, pattern1, pattern2, tolerance=0.7):
        """
        Calculate similarity between two patterns with enhanced contextual weighting.
        
        Args:
            pattern1: First pattern to compare
            pattern2: Second pattern to compare
            tolerance: Similarity threshold (default 0.7)
        
        Returns:
            float: Similarity score between 0 and 1
        """
        if len(pattern1) != len(pattern2):
            return 0.0
        
        # Define weights for different pattern elements based on their importance
        weights = {
            "exact_match": 1.0,  # Exact matches
            "numeric_close": 0.8,  # Numerically close values
            "category_similar": 0.6,  # Similar categories
            "opposite": 0.0  # Opposite values
        }
        
        # Track similarity components
        similarity_score = 0.0
        total_possible = 0.0
        
        for i, (a, b) in enumerate(zip(pattern1, pattern2)):
            # Determine the type of element and apply appropriate comparison
            element_weight = 1.0
            
            # Different weights for different indices (pattern elements)
            if i == 0:  # Game stage
                element_weight = 2.0  # Very important
                if a == b:
                    similarity_score += weights["exact_match"] * element_weight
                elif abs(a - b) == 1:  # Adjacent stages
                    similarity_score += weights["numeric_close"] * element_weight
            elif i in [1, 2]:  # Creature counts
                element_weight = 1.5  # Important
                if a == b:
                    similarity_score += weights["exact_match"] * element_weight
                elif abs(a - b) <= 1:  # Within 1
                    similarity_score += weights["numeric_close"] * element_weight
                elif abs(a - b) <= 2:  # Within 2
                    similarity_score += weights["category_similar"] * element_weight
            elif i in [3, 4]:  # Power/toughness differences
                element_weight = 1.8  # Very important
                if a == b:
                    similarity_score += weights["exact_match"] * element_weight
                elif abs(a - b) == 1:  # Similar situation
                    similarity_score += weights["numeric_close"] * element_weight
                elif (a > 0 and b > 0) or (a < 0 and b < 0):  # Same direction
                    similarity_score += weights["category_similar"] * element_weight
            elif i == 5:  # Life difference
                element_weight = 2.0  # Very important
                if a == b:
                    similarity_score += weights["exact_match"] * element_weight
                elif abs(a - b) == 1:  # Similar life situation
                    similarity_score += weights["numeric_close"] * element_weight
                elif (a > 0 and b > 0) or (a < 0 and b < 0):  # Same direction
                    similarity_score += weights["category_similar"] * element_weight
            elif i == 6:  # Card advantage
                element_weight = 1.7  # Important
                if a == b:
                    similarity_score += weights["exact_match"] * element_weight
                elif abs(a - b) == 1:  # Similar card situation
                    similarity_score += weights["numeric_close"] * element_weight
                elif (a > 0 and b > 0) or (a < 0 and b < 0):  # Same direction
                    similarity_score += weights["category_similar"] * element_weight
            elif i == 7:  # Mana development
                element_weight = 1.3  # Moderately important
                if a == b:
                    similarity_score += weights["exact_match"] * element_weight
                elif abs(a - b) == 1:  # Similar mana situation
                    similarity_score += weights["numeric_close"] * element_weight
                elif (a > 0 and b > 0) or (a < 0 and b < 0):  # Same direction
                    similarity_score += weights["category_similar"] * element_weight
            elif i == 8:  # Current phase
                element_weight = 0.8  # Less important
                if a == b:
                    similarity_score += weights["exact_match"] * element_weight
                elif abs(a - b) == 1:  # Adjacent phases
                    similarity_score += weights["numeric_close"] * element_weight
            
            total_possible += weights["exact_match"] * element_weight
        
        # Normalize the similarity score
        normalized_similarity = similarity_score / total_possible if total_possible > 0 else 0.0
        
        return normalized_similarity

    def monte_carlo_search(self, num_simulations=200, exploration_weight=1.0):
        """
        Perform Monte Carlo Tree Search to find the best action.
        Includes safety checks for action_handler.

        Args:
            num_simulations: Number of simulations to run
            exploration_weight: Weight for exploration in UCB formula

        Returns:
            Best action found by MCTS, or None on failure
        """
        gs = self.game_state
        # --- ADDED: Safety check for action handler ---
        action_handler = getattr(gs, 'action_handler', None)
        if not action_handler:
            logging.error("MCTS cannot run: gs.action_handler is missing.")
            return None
        # --- END check ---

        # Create root node
        root = MCTSNode(game_state=gs.clone()) # Clone state for root

        # Get valid actions
        try:
             valid_actions = action_handler.generate_valid_actions()
             valid_actions = np.where(valid_actions)[0].tolist()
             if not valid_actions:
                 logging.warning("MCTS: No valid actions at root node.")
                 return None
        except Exception as e:
             logging.error(f"MCTS Error getting valid actions at root: {e}")
             return None

        # Use strategic evaluation to initialize action priors
        action_priors = {}
        for action in valid_actions:
            # --- Use verified action_handler ---
            action_type, param = action_handler.get_action_info(action)
            value = 0.5 # Default value
            try:
                if action_type == "PLAY_CARD" and param:
                    value = self.evaluate_play_card_action(param) if hasattr(self,'evaluate_play_card_action') else 0.5
                elif action_type == "DECLARE_ATTACKER" and param:
                    value = self.evaluate_attack_action(param) if hasattr(self,'evaluate_attack_action') else 0.5
                elif action_type == "ACTIVATE_ABILITY" and isinstance(param,tuple) and len(param)==2:
                    value, _ = self.evaluate_ability_activation(param[0], param[1]) if hasattr(self,'evaluate_ability_activation') else (0.5, "")
                else:
                    value = self._quick_action_evaluation(gs, action_type, param) if hasattr(self,'_quick_action_evaluation') else 0.5
            except Exception as eval_e:
                 logging.warning(f"MCTS Prior Eval Error for action {action}: {eval_e}")

            action_priors[action] = max(0.01, value) # Ensure non-zero probability

        # Normalize priors
        total = sum(action_priors.values())
        if total > 0:
            for action in action_priors: action_priors[action] /= total
        else: # Fallback to uniform if all evals failed
             action_priors = {a: 1.0/len(valid_actions) for a in valid_actions}

        # Expand root with valid actions and priors
        root.expand(valid_actions, action_priors)

        # Run simulations
        for i in range(num_simulations):
            try: # Wrap simulation loop
                # Phase 1: Selection
                node = root
                search_path = [node]
                sim_gs = gs.clone() # Clone for each simulation run
                # --- ADDED: Ensure cloned state also has handler linked ---
                sim_action_handler = getattr(sim_gs, 'action_handler', None)
                if not sim_action_handler:
                     logging.error(f"MCTS Sim {i}: Cloned GameState missing action_handler. Aborting sim.")
                     continue # Skip this simulation if clone failed
                # --- END check ---

                while node.is_expanded and node.children:
                    action, node = node.select_child(c_puct=exploration_weight)
                    search_path.append(node)

                    # Apply action in simulation (Use sim_action_handler)
                    action_type, param = sim_action_handler.get_action_info(action)
                    if action_type is None:
                         logging.warning(f"MCTS Sim {i}: Invalid action {action} encountered during selection. Breaking path.")
                         break # Stop traversing this path
                    # Need to apply action robustly
                    sim_action_handler.apply_action(action) # Assume apply_action handles details internally now

                # Check if the node traversal failed early
                if not node:
                     logging.warning(f"MCTS Sim {i}: Node selection failed unexpectedly.")
                     continue

                # Phase 2: Expansion
                # Check game over status safely
                game_over = False
                if hasattr(sim_gs, 'p1') and hasattr(sim_gs, 'p2') and sim_gs.p1 and sim_gs.p2:
                    game_over = sim_gs.p1.get("life", 20) <= 0 or sim_gs.p2.get("life", 20) <= 0 or getattr(sim_gs, 'turn', 1) > getattr(sim_gs, 'max_turns', 50)
                else:
                     logging.error(f"MCTS Sim {i}: Player state missing in simulation. Ending sim.")
                     game_over = True # Treat as ended

                leaf_value = 0.0
                if not game_over:
                    # Get valid actions in sim state (Use sim_action_handler)
                    sim_valid_actions_mask = sim_action_handler.generate_valid_actions()
                    sim_valid_actions = np.where(sim_valid_actions_mask)[0].tolist()

                    # Expand node (handle case where node already expanded?)
                    if not node.is_expanded: # Only expand if not already done
                         node.expand(sim_valid_actions) # Use uniform prior for expansion if not using policy network

                    # Phase 3: Simulation (rollout)
                    leaf_value = self._rollout(sim_gs) # Pass the sim state
                else:
                    # Evaluate final state
                    me_sim = sim_gs.p1 if sim_gs.agent_is_p1 else sim_gs.p2
                    opp_sim = sim_gs.p2 if sim_gs.agent_is_p1 else sim_gs.p1
                    me_life = me_sim.get("life", 0) if me_sim else 0
                    opp_life = opp_sim.get("life", 0) if opp_sim else 0

                    if me_life <= 0: leaf_value = -1.0
                    elif opp_life <= 0: leaf_value = 1.0
                    else: leaf_value = 0.1 * np.sign(me_life - opp_life) if me_life != opp_life else 0.0 # Draw/Turn limit

                # Phase 4: Backpropagation
                current_value = leaf_value
                for node_in_path in reversed(search_path):
                    node_in_path.visit_count += 1
                    node_in_path.value_sum += current_value
                    current_value = -current_value # Alternate for opponent turns

            except Exception as sim_e:
                 logging.error(f"Error during MCTS simulation {i}: {sim_e}", exc_info=True)
                 # Skip this simulation if an error occurs

        # Return best action based on visit count
        best_visit_count = -1 # Changed from max_visit_count to avoid potential name clash
        best_action = None

        # Check if root has children before iterating
        if not root.children:
             logging.warning("MCTS finished but root node has no children. Returning first valid action or None.")
             return valid_actions[0] if valid_actions else None

        for action, child in root.children.items():
            if child.visit_count > best_visit_count:
                best_visit_count = child.visit_count
                best_action = action

        # Fallback if no best action found (e.g., all visits are 0)
        if best_action is None and valid_actions:
             logging.warning("MCTS completed but no best action found based on visits. Returning first valid action.")
             best_action = valid_actions[0]

        logging.debug(f"MCTS selected action {best_action} with {best_visit_count} visits")
        return best_action

    def _rollout(self, game_state, max_steps=20):
        """
        Perform a rollout (simulated game) from the current state.

        Args:
            game_state: The game state to start from
            max_steps: Maximum simulation steps

        Returns:
            float: Estimated value of the final state
        """
        sim_gs = game_state.clone()
        me = sim_gs.p1 if sim_gs.agent_is_p1 else sim_gs.p2
        opp = sim_gs.p2 if sim_gs.agent_is_p1 else sim_gs.p1

        # Ensure action handler exists on the cloned state
        if not hasattr(sim_gs, 'action_handler') or sim_gs.action_handler is None:
            logging.error("MCTS Rollout: Cloned GameState missing action_handler.")
            return 0.0 # Neutral value if handler is missing

        # Simulate random/strategic actions until game ends or step limit
        for _ in range(max_steps):
            # Check if game is done
            if (me and me.get("life", 20) <= 0) or \
               (opp and opp.get("life", 20) <= 0) or \
               getattr(sim_gs, 'turn', 1) > getattr(sim_gs, 'max_turns', 50):
                break

            # Get valid actions
            try:
                valid_actions_mask = sim_gs.action_handler.generate_valid_actions()
                valid_actions = np.where(valid_actions_mask)[0].tolist()
            except Exception as e:
                 logging.error(f"MCTS Rollout: Error generating valid actions - {e}")
                 break # Stop rollout if actions can't be generated

            if not valid_actions:
                # logging.debug("MCTS Rollout: No valid actions found.")
                break

            # Use strategic rollout policy
            action_idx = self._rollout_policy(sim_gs, valid_actions) # Returns index
            if action_idx is None:
                logging.warning("MCTS Rollout: Rollout policy returned None.")
                break # Stop if policy fails

            # Apply action using the action index
            try:
                # *** FIXED: Call apply_action with action_idx ***
                sim_gs.action_handler.apply_action(action_idx)
            except TypeError as te:
                 logging.error(f"MCTS Rollout: TypeError applying action {action_idx} - {te}")
                 break # Stop rollout on type error
            except Exception as e:
                 logging.error(f"MCTS Rollout: Unexpected error applying action {action_idx} - {e}")
                 break # Stop rollout on general error

        # Evaluate final state safely
        final_me_life = me.get("life", 0) if me else 0
        final_opp_life = opp.get("life", 0) if opp else 0

        if final_me_life <= 0:
            return -1.0  # Loss
        elif final_opp_life <= 0:
            return 1.0  # Win
        else:
            # Compute a heuristic value (can reuse parts of _calculate_board_state_reward or advanced_position_evaluation)
            # Simplified version for rollout:
            my_creatures = [cid for cid in getattr(me,'battlefield',[])
                            if sim_gs._safe_get_card(cid) and 'creature' in getattr(sim_gs._safe_get_card(cid),'card_types',[])]
            opp_creatures = [cid for cid in getattr(opp,'battlefield',[])
                            if sim_gs._safe_get_card(cid) and 'creature' in getattr(sim_gs._safe_get_card(cid),'card_types',[])]
            my_power = sum(getattr(sim_gs._safe_get_card(cid), 'power', 0) or 0 for cid in my_creatures)
            opp_power = sum(getattr(sim_gs._safe_get_card(cid), 'power', 0) or 0 for cid in opp_creatures)
            my_hand_size = len(getattr(me,'hand',[]))
            opp_hand_size = len(getattr(opp,'hand',[]))
            my_board_size = len(getattr(me,'battlefield',[]))
            opp_board_size = len(getattr(opp,'battlefield',[]))

            life_diff_val = (final_me_life - final_opp_life) / 20.0
            card_adv_val = (my_hand_size - opp_hand_size) / 5.0
            board_adv_val = (my_board_size - opp_board_size) / 10.0
            power_adv_val = (my_power - opp_power) / 15.0

            value = 0.4 * life_diff_val + 0.2 * card_adv_val + 0.2 * board_adv_val + 0.2 * power_adv_val
            return np.clip(value, -1.0, 1.0)

    def _rollout_policy(self, game_state, valid_actions):
        """
        Policy for choosing actions during rollout.
        This can be a simple heuristic or a more complex policy.
        
        Args:
            game_state: Current game state
            valid_actions: List of valid action indices
            
        Returns:
            int: Selected action index
        """
        if not valid_actions:
            return None
            
        gs = game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Prioritize actions
        action_priorities = []
        
        # Evaluate each action with quick heuristics
        for action in valid_actions:
            action_type, param = gs.action_handler.get_action_info(action)
            priority = 0
            
            # Land plays have high priority if land hasn't been played
            if action_type == "PLAY_CARD":
                card = gs._safe_get_card(param)
                if (card and hasattr(card, 'type_line')
                        and 'land' in card.type_line
                        and gs.can_play_land_this_turn(me)):
                    priority = 100
                elif card and hasattr(card, 'cmc'):
                    # Prioritize cheap spells
                    priority = 90 - card.cmc * 10
                    
                    # Bonus for creatures
                    if hasattr(card, 'card_types') and 'creature' in card.card_types:
                        priority += 5
            
            # Combat actions
            elif action_type == "DECLARE_ATTACKER":
                priority = 80
            elif action_type == "DECLARE_BLOCKER":
                priority = 85
            
            # Ability activation
            elif action_type == "ACTIVATE_ABILITY":
                priority = 70
            
            # Phase transitions
            elif action_type.startswith("END") or "PHASE" in action_type:
                priority = 50
            
            # Other actions
            else:
                priority = 20
                
            # Add random factor to avoid deterministic rollouts
            priority += random.random() * 10
            
            action_priorities.append((action, priority))
        
        # Select action with highest priority
        action_priorities.sort(key=lambda x: x[1], reverse=True)
        return action_priorities[0][0]

