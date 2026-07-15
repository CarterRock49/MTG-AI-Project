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
        valid_actions = (
            [] if valid_actions is None else list(valid_actions))
        if not valid_actions:
            self.is_expanded = True
            return
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
        
        # Project for each turn.  Every term is an observer-relative
        # difference transformed by an odd function, so a symmetric state is
        # exactly neutral and swapping observers negates the projection.
        for i in range(num_turns):
            # More weight to current advantages in earlier projections, 
            # more uncertainty in later turns
            certainty = 1.0 / (i + 1)
            
            # Project life difference based on board state
            projected_life_diff = life_diff
            
            # Signed combat pressure must change sign with the observer.  The
            # previous branch penalized both observers on an even board and
            # computed power_adv without ever using it.
            total_power = my_power + opp_power
            projected_power_adv = power_adv * (0.9 ** i)
            signed_combat_pressure = total_power * projected_power_adv
            projected_life_diff += (
                signed_combat_pressure * (i + 1) * (0.9 ** i))

            # Both players receive the same baseline draw, so only the current
            # observer-relative difference survives.
            projected_card_diff = card_diff * (0.9 ** i)
            
            # Estimate board development based on cards in hand
            estimated_new_permanents = min(3, len(me["hand"])) * (0.8 ** i)  # Assume we play our hand
            estimated_opp_permanents = min(3, len(opp["hand"])) * (0.8 ** i)  # Assume opponent plays their hand
            projected_board_adv = board_adv + (estimated_new_permanents - estimated_opp_permanents) * 0.1 * (0.9 ** i)
            
            # Combine factors into an overall state value (-1 to +1 scale)
            state_value = (
                0.45 * np.tanh(projected_life_diff / 10) +
                0.25 * np.tanh(projected_card_diff / 3) +
                0.15 * np.tanh(projected_board_adv * 2) +
                0.15 * np.tanh(projected_power_adv * 2)
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

    def _valid_action_indices(self, valid_actions):
        """Normalize either a Boolean mask or an action-index collection."""
        if valid_actions is None:
            return []
        array = np.asarray(valid_actions)
        if array.ndim == 0:
            array = array.reshape(1)
        action_handler = getattr(self.game_state, "action_handler", None)
        expected_mask_size = getattr(
            action_handler, "ACTION_SPACE_SIZE", None)
        is_binary_integer_mask = (
            np.issubdtype(array.dtype, np.integer)
            and expected_mask_size is not None
            and array.size == int(expected_mask_size)
            and np.all((array == 0) | (array == 1))
        )
        values = (
            np.flatnonzero(array).tolist()
            if array.dtype == np.bool_ or is_binary_integer_mask
            else array.reshape(-1).tolist()
        )
        result = []
        for value in values:
            try:
                action = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if action >= 0 and action not in result:
                result.append(action)
        return sorted(result)

    @staticmethod
    def _generated_action_context(action_handler, action_idx):
        generated = getattr(
            action_handler, "action_reasons_with_context", {}).get(
                action_idx, {})
        context = generated.get("context", {}) if isinstance(
            generated, dict) else {}
        return dict(context or {})

    @staticmethod
    def _zone_card_id(player, zone, index, context):
        card_id = context.get("card_id")
        if card_id is not None:
            return card_id
        try:
            resolved_index = int(index)
        except (TypeError, ValueError, OverflowError):
            return None
        cards = player.get(zone, ())
        if 0 <= resolved_index < len(cards):
            return cards[resolved_index]
        return None

    def _evaluate_action_candidate(self, action_idx):
        """Evaluate one real public action without mutating the game state."""
        gs = self.game_state
        action_handler = getattr(gs, "action_handler", None)
        if action_handler is None:
            return 0.0, "No action handler"
        action_type, param = action_handler.get_action_info(action_idx)
        context = self._generated_action_context(
            action_handler, action_idx)
        me = gs.p1 if gs.agent_is_p1 else gs.p2

        value = 0.0
        reason = action_type
        try:
            if action_type in (
                    "PLAY_LAND", "PLAY_MDFC_LAND_BACK",
                    "PLAY_SPELL", "PLAY_MDFC_BACK", "PLAY_ADVENTURE"):
                hand_index = context.get("hand_idx", param)
                card_id = self._zone_card_id(
                    me, "hand", hand_index, context)
                if card_id is not None:
                    value = self.evaluate_play_card_action(
                        card_id, context=context)
                    reason = "Card play"
                else:
                    value = self._quick_action_evaluation(
                        gs, action_type, param)
            elif action_type == "ATTACK":
                attacker_id = self._zone_card_id(
                    me, "battlefield",
                    context.get("battlefield_idx", param), context)
                if attacker_id is not None:
                    value = self.evaluate_attack_action([attacker_id])
                    reason = "Attack"
            elif action_type == "BLOCK":
                blocker_id = self._zone_card_id(
                    me, "battlefield",
                    context.get("battlefield_idx", param), context)
                attacker_id = context.get("target_attacker_id")
                if blocker_id is not None and attacker_id is not None:
                    value = self.evaluate_block_action(
                        attacker_id, [blocker_id])
                    reason = "Block"
            elif action_type == "ACTIVATE_ABILITY":
                battlefield_index = context.get("battlefield_idx")
                ability_index = context.get("ability_idx")
                card_id = self._zone_card_id(
                    me, "battlefield", battlefield_index, context)
                if card_id is not None and isinstance(ability_index, int):
                    value, reason = self.evaluate_ability_activation(
                        card_id, ability_index)
            elif action_type in ("END_TURN", "PASS_PRIORITY"):
                value = 0.2 if not getattr(gs, "stack", ()) else 0.1
            elif action_type in (
                    "DECLARE_ATTACKERS_DONE", "DECLARE_BLOCKERS_DONE"):
                value = 0.15
            elif action_type == "CONCEDE":
                value = -10.0
            else:
                value = self._quick_action_evaluation(
                    gs, action_type, param)
        except Exception as error:
            logging.warning(
                "Could not evaluate action %s (%s): %s",
                action_idx, action_type, error)
            return 0.0, "Evaluation fallback"

        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError):
            numeric_value = 0.0
        if not math.isfinite(numeric_value):
            numeric_value = 0.0
        return numeric_value, str(reason or action_type)

    def find_best_play_sequence(
            self, valid_actions, depth=None, discount_factor=0.9):
        """Return a deterministic one-ply recommendation.

        The old implementation attempted to recurse through cloned GameStates,
        but applied action names instead of public action indices, omitted
        generated contexts, and then recursed on the original state. Until a
        full environment-level alternating-priority search exists, a bounded
        current-state evaluation is the honest deterministic contract.
        """
        del depth, discount_factor
        actions = self._valid_action_indices(valid_actions)
        if not actions:
            return [], 0.0
        evaluations = []
        for action_idx in actions:
            value, _ = self._evaluate_action_candidate(action_idx)
            evaluations.append((value, -action_idx, action_idx))
        best_value, _, best_action = max(evaluations)
        return [best_action], best_value
    def plan_multi_turn_sequence(
            self, depth=3, analysis=None, win_conditions=None,
            opponent_threats=None):
        """
        Enhanced multi-turn sequence planning with advanced mana and resource management.
        Now with improved outcome probability modeling and risk assessment.
        
        Args:
            depth: Number of turns to plan ahead
            analysis: Optional game-state analysis already computed by caller
            win_conditions: Optional win-condition analysis from this state
            opponent_threats: Optional threat list from this state
            
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
        
        # Get current battlefield state. Keep IDs so the live projection can
        # distinguish untapped mana sources from lands already used this turn.
        my_land_ids = [
            card_id for card_id in me["battlefield"]
            if ((card := gs._safe_get_card(card_id)) is not None
                and "land" in str(getattr(card, "type_line", "")).lower())
        ]
        my_lands = [gs._safe_get_card(card_id) for card_id in my_land_ids]
        
        # Respect both the canonical counter and the legacy boolean.  Taking
        # the maximum is conservative for old or partially migrated states.
        try:
            land_play_limit = max(1, int(gs.land_play_limit(me)))
        except (AttributeError, TypeError, ValueError):
            land_play_limit = 1
        try:
            lands_already_played = max(
                0, int(gs.lands_played_this_turn(me)))
        except (AttributeError, TypeError, ValueError):
            lands_already_played = max(
                0, int(me.get("lands_played_this_turn", 0) or 0))
        if me.get("land_played", False):
            lands_already_played = max(1, lands_already_played)
        current_land_drops_remaining = max(
            0, land_play_limit - lands_already_played)

        def pool_total(pool):
            total = 0.0
            for amount in (pool or {}).values():
                try:
                    number = float(amount)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(number):
                    total += max(0.0, number)
            return total

        tapped_permanents = set(me.get("tapped_permanents", ()))
        current_untapped_lands = sum(
            card_id not in tapped_permanents for card_id in my_land_ids)
        # Conditional mana needs a concrete spell context before it is
        # spendable. Ordinary and phase-restricted pools are safe to include
        # in the generic live-turn projection.
        current_floating_mana = (
            pool_total(me.get("mana_pool", {}))
            + pool_total(me.get("phase_restricted_mana", {}))
        )
        
        # Estimate mana development curve
        def project_mana_development(turns_ahead):
            """Project expected mana without sampling or consuming game RNG."""
            # This is an observation summary, not a rollout.  Use the expected
            # value of unknown future land draws so equal public states always
            # produce equal policy inputs and observation reads cannot perturb
            # later gameplay randomness.
            land_count = float(len(my_lands))
            
            # Count lands in hand
            lands_in_hand = len([card for card in hand if hasattr(card, 'type_line') and 'land' in card.type_line])
            
            # Use every live land-play allowance. Unknown draws can contribute
            # only on future turns and at most one expected card per turn.
            projected_lands = []
            known_lands_used = 0
            for i in range(turns_ahead):
                drop_slots = (
                    current_land_drops_remaining
                    if i == 0 else land_play_limit)
                known_drops = min(
                    drop_slots, lands_in_hand - known_lands_used)
                known_lands_used += known_drops
                land_count += known_drops

                if i > 0 and known_drops < drop_slots:
                    land_count += 0.4

                if i == 0:
                    projected_lands.append(
                        current_untapped_lands
                        + current_floating_mana
                        + known_drops)
                else:
                    projected_lands.append(land_count)
            
            # Convert to mana availability
            return projected_lands
        
        # Advanced multi-turn planning
        turn_plans = []
        remaining_hand = hand.copy()
        
        # Project mana development
        mana_projection = project_mana_development(depth)
        
        # First, analyze current game state and objectives
        current_analysis = (
            analysis if analysis is not None else self.analyze_game_state())
        position = current_analysis["position"]["overall"]
        game_stage = current_analysis["game_info"]["game_stage"]
        
        # Identify win conditions
        win_conditions = (
            win_conditions if win_conditions is not None
            else self.identify_win_conditions())
        primary_win_condition = None
        for wc_name, wc_data in win_conditions.items():
            if wc_data["viable"] and (primary_win_condition is None or 
                                    win_conditions[primary_win_condition]["score"] < wc_data["score"]):
                primary_win_condition = wc_name
        
        # Identify opponent threats
        opponent_threats = (
            opponent_threats if opponent_threats is not None
            else self.assess_threats())
        
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
                "land_plays": [],
                "spells": [],
                "abilities": [],
                "expected_value": 0.0
            }
            
            # First priority: Land drop if available.  A used current-turn
            # allowance does not suppress the fresh allowance next turn.
            land_drop_slots = (
                current_land_drops_remaining
                if turn_idx == 0 else land_play_limit)
            if land_drop_slots > 0:
                lands_in_hand = [card for card in remaining_hand 
                            if hasattr(card, 'type_line') and 'land' in card.type_line]
                
                if lands_in_hand:
                    for land in lands_in_hand[:land_drop_slots]:
                        land_play = {
                            "card": land,
                            "name": (land.name if hasattr(land, 'name')
                                     else "Unknown Land"),
                            "value": 0.7,
                        }
                        turn_plan["land_plays"].append(land_play)
                        remaining_hand.remove(land)
                    turn_plan["land_play"] = turn_plan["land_plays"][0]
            
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
            for land_play in turn_plan["land_plays"]:
                all_plays.append({
                    "type": "land",
                    "card": land_play["card"],
                    "name": land_play["name"],
                    "value": land_play["value"],
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
        """Return a deterministic, mask-valid strategic recommendation.

        Recommendation deliberately uses bounded current-state evaluation.
        GameState-only search cannot model the environment's alternating
        learned/opponent priority loop, so the previous pseudo-MCTS and clone
        recursion could assign values to the wrong actor and are not used here.
        """
        actions = self._valid_action_indices(valid_actions)
        if not actions:
            return None

        gs = self.game_state
        action_handler = getattr(gs, "action_handler", None)
        if action_handler is None:
            logging.error(
                "Recommend action cannot proceed: gs.action_handler is missing")
            return 11 if 11 in actions else actions[0]
        if (not getattr(gs, "p1", None)
                or not getattr(gs, "p2", None)):
            logging.error(
                "Recommend action failed: GameState players are not initialized")
            return 11 if 11 in actions else actions[0]

        try:
            self.analyze_game_state()
            self.adapt_strategy()
        except Exception as error:
            logging.warning(
                "Strategic analysis failed before recommendation: %s", error)

        memory_suggestion = None
        memory = getattr(gs, "strategy_memory", None)
        if memory is not None:
            try:
                memory_suggestion = memory.get_suggested_action(
                    gs, actions)
                if memory_suggestion not in actions:
                    memory_suggestion = None
                if memory_suggestion is not None:
                    pattern = memory.extract_strategy_pattern(gs)
                    strategy = memory.strategies.get(pattern)
                    if (strategy
                            and strategy.get("success_rate", 0) > 0.8
                            and strategy.get("count", 0) > 5):
                        return memory_suggestion
            except Exception as error:
                logging.warning(
                    "Could not obtain strategy-memory suggestion: %s", error)
                memory_suggestion = None

        me = gs.p1 if gs.agent_is_p1 else gs.p2
        if gs.can_play_land_this_turn(me):
            for action_idx in actions:
                action_type, param = action_handler.get_action_info(
                    action_idx)
                if action_type not in (
                        "PLAY_LAND", "PLAY_MDFC_LAND_BACK"):
                    continue
                context = self._generated_action_context(
                    action_handler, action_idx)
                card_id = self._zone_card_id(
                    me, "hand", context.get("hand_idx", param), context)
                card = gs._safe_get_card(card_id)
                if card and "land" in str(
                        getattr(card, "type_line", "")).lower():
                    return action_idx

        if memory_suggestion is not None:
            pattern = memory.extract_strategy_pattern(gs)
            strategy = memory.strategies.get(pattern)
            if strategy and strategy.get("success_rate", 0) > 0.6:
                return memory_suggestion

        best_sequence, best_value = self.find_best_play_sequence(
            actions, depth=1)
        if best_sequence and best_sequence[0] in actions:
            logging.debug(
                "Deterministic strategic recommendation %s (value %.3f)",
                best_sequence[0], best_value)
            return best_sequence[0]

        return 11 if 11 in actions else actions[0]
    def suggest_action_from_memory(self, valid_actions):
        """Delegate to deterministic action-specific strategy evidence."""
        try:
            memory = getattr(self.game_state, 'strategy_memory', None)
            if memory is None:
                return None
            return memory.get_suggested_action(
                self.game_state, valid_actions, exploration_rate=0.0)
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

        # Use the same real action/context decoder as deterministic advice.
        action_priors = {}
        for action in valid_actions:
            try:
                value, _ = self._evaluate_action_candidate(action)
            except Exception as eval_e:
                logging.warning(
                    "MCTS prior evaluation failed for action %s: %s",
                    action, eval_e)
                value = 0.0

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
                    action_context = self._generated_action_context(
                        sim_action_handler, action)
                    sim_action_handler.apply_action(
                        action, context=action_context)

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
                for node_in_path in reversed(search_path):
                    node_in_path.visit_count += 1
                    # Values are evaluated from the fixed observer carried by
                    # the cloned GameState. A Magic action edge does not imply
                    # an actor change, so alternating sign per edge is invalid.
                    node_in_path.value_sum += leaf_value

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
                action_context = self._generated_action_context(
                    sim_gs.action_handler, action_idx)
                sim_gs.action_handler.apply_action(
                    action_idx, context=action_context)
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
            my_creatures = [cid for cid in me.get('battlefield', [])
                            if sim_gs._safe_get_card(cid) and 'creature' in getattr(sim_gs._safe_get_card(cid),'card_types',[])]
            opp_creatures = [cid for cid in opp.get('battlefield', [])
                            if sim_gs._safe_get_card(cid) and 'creature' in getattr(sim_gs._safe_get_card(cid),'card_types',[])]
            my_power = sum(
                _card_number(sim_gs._safe_get_card(cid), 'power')
                for cid in my_creatures)
            opp_power = sum(
                _card_number(sim_gs._safe_get_card(cid), 'power')
                for cid in opp_creatures)
            my_hand_size = len(me.get('hand', []))
            opp_hand_size = len(opp.get('hand', []))
            my_board_size = len(me.get('battlefield', []))
            opp_board_size = len(opp.get('battlefield', []))

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
            context = self._generated_action_context(
                gs.action_handler, action)
            priority = 0
            
            # Land plays have high priority if land hasn't been played
            if action_type in ("PLAY_LAND", "PLAY_MDFC_LAND_BACK"):
                card_id = self._zone_card_id(
                    me, "hand", context.get("hand_idx", param), context)
                card = gs._safe_get_card(card_id)
                if (card and hasattr(card, 'type_line')
                        and 'land' in card.type_line
                        and gs.can_play_land_this_turn(me)):
                    priority = 100
            elif action_type in (
                    "PLAY_SPELL", "PLAY_MDFC_BACK", "PLAY_ADVENTURE"):
                card_id = self._zone_card_id(
                    me, "hand", context.get("hand_idx", param), context)
                card = gs._safe_get_card(card_id)
                if card:
                    priority = 90 - _card_number(card, "cmc") * 10
                    if 'creature' in getattr(card, 'card_types', []):
                        priority += 5
            
            # Combat actions
            elif action_type == "ATTACK":
                priority = 80
            elif action_type == "BLOCK":
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

