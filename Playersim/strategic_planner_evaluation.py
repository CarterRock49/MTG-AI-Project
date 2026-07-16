"""Card and action evaluation for plays, attacks, blocks, and abilities.

Extracted from strategic_planner.py. This module defines behavior only (a mixin);
all state lives on MTGStrategicPlanner, which composes every mixin.
"""

import logging
import math
import re


def _finite_number(value, default=0.0):
    """Return a finite numeric value for advisory scoring."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _card_number(card, attribute, default=0.0):
    return _finite_number(
        getattr(card, attribute, default) if card else default, default)


class CardEvaluationMixin:
    """Card and action evaluation for plays, attacks, blocks, and abilities."""

    __slots__ = ()

    def evaluate_special_card_types(self, battlefield, controller):
        """Evaluate the value and potential of special card types like Sagas and Battles"""
        assessment = {
            "sagas": [],
            "battles": [],
            "opportunities": []
        }
        
        gs = self.game_state
        
        for card_id in battlefield:
            card = gs._safe_get_card(card_id)
            if not card:
                continue
                
            # Evaluate Sagas
            if hasattr(card, 'is_saga') and card.is_saga():
                # Check current lore counter state
                lore_count = 0
                if hasattr(card, 'counters'):
                    lore_count = card.counters.get('lore', 0)
                    
                # Determine value based on remaining chapters
                saga_eval = {
                    "card_id": card_id,
                    "name": card.name,
                    "lore_count": lore_count,
                    "chapters_remaining": 3 - lore_count,  # Assuming most sagas have 3 chapters
                    "value": 0.5 * (3 - lore_count)  # Higher value for more remaining chapters
                }
                
                assessment["sagas"].append(saga_eval)
                
            # Evaluate Battles
            elif hasattr(card, 'is_battle') and card.is_battle():
                # Check current defense counter state
                defense_count = 0
                if hasattr(card, 'counters'):
                    defense_count = card.counters.get('defense', 0)
                    
                # Determine value based on counters and potential back face
                has_back_face = hasattr(card, 'is_mdfc') and card.is_mdfc() and hasattr(card, 'back_face')
                
                battle_eval = {
                    "card_id": card_id,
                    "name": card.name,
                    "defense_count": defense_count,
                    "has_back_face": has_back_face,
                    "value": 0.3 * defense_count + (0.5 if has_back_face else 0)
                }
                
                assessment["battles"].append(battle_eval)
                
        return assessment

    def _quick_action_evaluation(self, game_state, action_type, param):
        """Quick heuristic evaluation of an action's impact"""
        # This is a lightweight evaluation to enable pruning
        gs = game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Basic evaluation based on action type
        if action_type == "END_TURN":
            return -0.1  # Slightly negative - usually we want to do something
        elif action_type in ("PLAY_LAND", "PLAY_MDFC_LAND_BACK"):
            return 0.7
        elif action_type in (
                "PLAY_SPELL", "PLAY_MDFC_BACK", "PLAY_ADVENTURE"):
            return 0.4
        elif action_type == "ATTACK":
            return 0.3  # Attacking is generally good
        elif action_type == "BLOCK":
            return 0.25
        elif action_type == "ACTIVATE_ABILITY":
            return 0.3
        
        return 0.0  # Neutral for other actions

    def recommend_ability_activation(self, card_id, ability_idx):
        """
        Determine if now is a good time to activate an ability with comprehensive strategic analysis.
        
        Returns:
            bool: Whether activation is recommended
            float: Confidence in recommendation (0-1)
        """
        gs = self.game_state
        phase = gs.phase
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        card = gs._safe_get_card(card_id)
        
        if not card:
            return False, 0.0
        
        # Get the ability
        if not hasattr(gs, 'ability_handler') or not gs.ability_handler:
            return True, 0.5  # Default to "yes" with medium confidence if no handler
        
        abilities = gs.ability_handler.get_activated_abilities(card_id)
        if ability_idx >= len(abilities):
            return False, 0.0
            
        ability = abilities[ability_idx]
        
        # Use strategic planner if available and not None
        if hasattr(gs, 'strategic_planner') and gs.strategic_planner is not None:
            try:
                if hasattr(gs.strategic_planner, 'evaluate_ability_activation'):
                    value, reasoning = gs.strategic_planner.evaluate_ability_activation(card_id, ability_idx)
                    recommended = value > 0.6  # Threshold for recommendation
                    confidence = min(1.0, value)
                    return recommended, confidence
            except Exception as e:
                logging.debug(f"Error using strategic planner for ability evaluation: {e}")
                # Fall through to enhanced heuristics
        
        # Enhanced phase-based and ability-specific heuristics
        effect_text = ability.effect.lower() if hasattr(ability, 'effect') else ""
        cost_text = ability.cost.lower() if hasattr(ability, 'cost') else ""
        
        # Parse the cost to understand what we're paying
        tap_cost = "{t}" in cost_text or "tap" in cost_text
        sac_cost = "sacrifice" in cost_text
        life_cost = "pay" in cost_text and "life" in cost_text
        mana_cost = any(color in cost_text for color in ["{w}", "{u}", "{b}", "{r}", "{g}", "{c}"])
        discard_cost = "discard" in cost_text
        
        # Important game state factors
        my_life = me["life"]
        opp_life = opp["life"]
        my_creatures = sum(1 for cid in me["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types)
        opp_creatures = sum(1 for cid in opp["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types)
        hand_size = len(me["hand"])
        
        # Calculate base value and confidence based on effect type
        value = 0.5  # Start neutral
        confidence = 0.5
        reason = "Neutral ability value"
        
        # Card advantage abilities (draw effects)
        if "draw" in effect_text and "card" in effect_text:
            # Extract number of cards to draw if available
            draw_amount = 1  # Default to 1
            import re
            match = re.search(r"draw (\w+) cards?", effect_text)
            if match:
                if match.group(1).isdigit():
                    draw_amount = int(match.group(1))
                elif match.group(1) == "two":
                    draw_amount = 2
                elif match.group(1) == "three":
                    draw_amount = 3
            
            # Higher value for more card draw
            value = 0.6 + (draw_amount * 0.1)
            
            # Card draw more valuable with low hand size
            if hand_size < 2:
                value += 0.3
                reason = f"Draw {draw_amount} cards is very valuable with empty hand"
            else:
                reason = f"Draw {draw_amount} cards"
                
            # Card draw better at sorcery speed unless responding to threat
            if phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]:
                if gs.stack:
                    value -= 0.1  # Slightly worse to draw in response to stack
                else:
                    value += 0.1  # Better to draw during main phase with empty stack
            
            confidence = 0.7 + (draw_amount * 0.05)
        
        # Damage dealing abilities
        elif "damage" in effect_text:
            # Extract damage amount
            damage_amount = 1  # Default
            import re
            match = re.search(r"(\d+) damage", effect_text)
            if match:
                damage_amount = int(match.group(1))
            
            # Check target (player/creature/any target)
            target_player = "damage to player" in effect_text or "damage to opponent" in effect_text or "any target" in effect_text
            target_creature = "damage to creature" in effect_text or "damage to target creature" in effect_text or "any target" in effect_text
            
            # Value direct damage to opponent based on board state and life totals
            if target_player:
                # More valuable when opponent is low on life
                if damage_amount >= opp_life:
                    value = 1.0  # Lethal damage is maximum value
                    confidence = 0.95
                    reason = f"{damage_amount} damage is lethal to opponent at {opp_life} life"
                elif opp_life <= 5:
                    value = 0.9
                    confidence = 0.85
                    reason = f"{damage_amount} damage brings opponent to critical life total"
                elif opp_life <= 10:
                    value = 0.8
                    confidence = 0.8
                    reason = f"{damage_amount} damage to low-life opponent"
                else:
                    value = 0.6
                    confidence = 0.7
                    reason = f"{damage_amount} damage to opponent"
                    
            # Value creature removal based on board state
            elif target_creature:
                if opp_creatures > 0:
                    # Check if any good targets (creatures that would die)
                    has_good_targets = any(
                        gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'toughness') and 
                        gs._safe_get_card(cid).toughness <= damage_amount
                        for cid in opp["battlefield"]
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types
                    )
                    
                    if has_good_targets:
                        # Even more valuable if opponent has more creatures than us
                        if opp_creatures > my_creatures:
                            value = 0.9
                            confidence = 0.85
                            reason = f"{damage_amount} damage can remove opponent's creature (they have board advantage)"
                        else:
                            value = 0.8
                            confidence = 0.75
                            reason = f"{damage_amount} damage can remove opponent's creature"
                    else:
                        value = 0.4
                        confidence = 0.6
                        reason = f"{damage_amount} damage to creature with no good targets"
                else:
                    value = 0.2
                    confidence = 0.7
                    reason = f"No creature targets for {damage_amount} damage"
        
        # Pump effects (power/toughness boosts)
        elif "+1/+1" in effect_text or "gets +" in effect_text:
            # Extract pump amount
            pump_amount = 1  # Default
            import re
            match = re.search(r"gets \+(\d+)/\+(\d+)", effect_text)
            if match:
                power_boost = int(match.group(1))
                toughness_boost = int(match.group(2))
                pump_amount = power_boost + toughness_boost
            
            # Combat-based timing is critical for pump effects
            is_combat = phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, gs.PHASE_COMBAT_DAMAGE]
            is_pre_combat = phase == gs.PHASE_MAIN_PRECOMBAT and not gs.stack and card_id not in me["tapped_permanents"] 
            
            if is_combat:
                value = 0.8 + (pump_amount * 0.05)
                confidence = 0.9
                reason = f"Pump +{pump_amount} during combat is excellent timing"
            elif is_pre_combat:
                value = 0.7
                confidence = 0.75
                reason = f"Pump +{pump_amount} before attacking is good timing"
            else:
                value = 0.3
                confidence = 0.6
                reason = f"Pump +{pump_amount} outside combat phases is not optimal"
                
            # If stack has removal targeting our creature, pumping is excellent
            if gs.stack:
                for item in gs.stack:
                    if item[0] == "SPELL" and item[2] != me:  # Opponent spell
                        spell_id = item[1]
                        spell = gs._safe_get_card(spell_id)
                        if spell and hasattr(spell, 'oracle_text') and "destroy target creature" in spell.oracle_text.lower():
                            value = 0.9
                            confidence = 0.9
                            reason = "Pump in response to removal is excellent"
        
        # Life gain effects
        elif "gain" in effect_text and "life" in effect_text:
            # Extract life gain amount
            life_amount = 1  # Default
            import re
            match = re.search(r"gain (\d+) life", effect_text)
            if match:
                if match.group(1).isdigit():
                    life_amount = int(match.group(1))
            
            # Life gain value depends on current life total
            if my_life <= 5:
                value = 0.85
                confidence = 0.9
                reason = f"Gain {life_amount} life is critical at low life total"
            elif my_life <= 10:
                value = 0.7
                confidence = 0.8
                reason = f"Gain {life_amount} life is valuable at {my_life} life"
            else:
                value = 0.3
                confidence = 0.6
                reason = f"Gain {life_amount} life when already at healthy {my_life} life"
        
        # Mana production abilities
        elif "add" in effect_text and any(color in effect_text for color in ["{w}", "{u}", "{b}", "{r}", "{g}", "{c}"]):
            # More valuable in main phases with cards to cast
            if phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and hand_size > 0:
                value = 0.7
                confidence = 0.8
                reason = "Mana production with spells to cast"
            else:
                value = 0.3
                confidence = 0.6
                reason = "Mana production outside main phase or with empty hand"
        
        # Token creation
        elif "create" in effect_text and "token" in effect_text:
            # Better when behind on board or in main phases
            if phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]:
                if my_creatures < opp_creatures:
                    value = 0.8
                    confidence = 0.85
                    reason = "Token creation when behind on board is valuable"
                else:
                    value = 0.7
                    confidence = 0.75
                    reason = "Token creation to further board advantage"
            else:
                value = 0.5
                confidence = 0.6
                reason = "Token creation outside main phase"
        
        # Removal/Exile effects
        elif "destroy" in effect_text or "exile" in effect_text:
            if "target creature" in effect_text or "target permanent" in effect_text:
                # Check if good targets exist
                if opp_creatures > 0:
                    # More valuable when opponent has threatening creatures
                    high_power_creatures = any(
                        gs._safe_get_card(cid).power >= 4
                        for cid in opp["battlefield"]
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types and
                        hasattr(gs._safe_get_card(cid), 'power')
                    )
                    
                    if high_power_creatures:
                        value = 0.9
                        confidence = 0.9
                        reason = "Removal with high-power targets is excellent"
                    else:
                        value = 0.8
                        confidence = 0.8
                        reason = "Removal with valid targets"
                else:
                    value = 0.2
                    confidence = 0.7
                    reason = "Removal with no valid targets"
        
        # Counterspell effects
        elif "counter target" in effect_text:
            # Most valuable when there are spells on the stack
            if gs.stack:
                value = 0.9
                confidence = 0.95
                reason = "Counterspell with spell on stack is excellent timing"
            else:
                value = 0.2
                confidence = 0.8
                reason = "Counterspell with no spells on stack"
        
        # Discard effects
        elif "discard" in effect_text:
            if "opponent" in effect_text and len(opp["hand"]) > 0:
                value = 0.7
                confidence = 0.75
                reason = "Discard effect with opponent having cards"
            else:
                value = 0.4
                confidence = 0.6
                reason = "Discard effect with limited impact"
        
        # Tutoring/Search effects
        elif "search your library" in effect_text:
            value = 0.75
            confidence = 0.8
            reason = "Library search effects are generally valuable"
        
        # Cost-based adjustments
        if tap_cost:
            # Adjust for tap costs based on timing
            if phase in [gs.PHASE_MAIN_POSTCOMBAT, gs.PHASE_END_STEP]:
                value += 0.1  # Better to use tap abilities after combat
                reason += ", good timing for tap ability"
            elif opp_creatures > 2 and phase == gs.PHASE_MAIN_PRECOMBAT:
                value -= 0.2  # Risky to tap before opponent attacks
                reason += ", risky to tap before opponent attacks"
        
        if sac_cost:
            # Sacrifice costs are higher - need higher payoff
            sacrifice_target = ""
            if "sacrifice a creature" in cost_text:
                sacrifice_target = "creature"
            elif "sacrifice a land" in cost_text:
                sacrifice_target = "land"
            
            # Check if we have expendable targets
            if sacrifice_target == "creature" and my_creatures <= 1:
                value -= 0.3
                reason += ", sacrifice cost too high with few creatures"
            elif sacrifice_target == "land" and len([cid for cid in me["battlefield"] 
                                                    if gs._safe_get_card(cid) and 
                                                    hasattr(gs._safe_get_card(cid), 'type_line') and 
                                                    'land' in gs._safe_get_card(cid).type_line]) <= 3:
                value -= 0.3
                reason += ", sacrifice cost too high with few lands"
        
        if life_cost:
            # Extract life cost amount
            life_cost_amount = 1  # Default
            import re
            match = re.search(r"pay (\d+) life", cost_text)
            if match:
                life_cost_amount = int(match.group(1))
            
            # Adjust based on current life total
            if my_life <= life_cost_amount + 3:
                value -= 0.4
                reason += f", life cost ({life_cost_amount}) too risky at {my_life} life"
            elif my_life <= 10:
                value -= 0.2
                reason += f", life cost ({life_cost_amount}) significant at {my_life} life"
        
        # Game stage considerations
        if gs.turn <= 3:  # Early game
            if "draw" in effect_text or "land" in effect_text:
                value += 0.1  # Card draw and land tutoring more valuable early
            if "exile" in effect_text or "destroy" in effect_text:
                value += 0.05  # Removal slightly more valuable early
        elif gs.turn >= 8:  # Late game
            if "damage to" in effect_text and "player" in effect_text:
                value += 0.15  # Direct damage more valuable late
            if "create token" in effect_text:
                value -= 0.05  # Tokens less impactful late
        
        # Final adjustments
        # Cap value and confidence between 0 and 1
        value = max(0.0, min(1.0, value))
        confidence = max(0.5, min(0.95, confidence))
        
        # If there are costs, the benefit should outweigh them
        if tap_cost or sac_cost or life_cost or discard_cost:
            if value < 0.4:
                return False, confidence
        
        # Final recommendation based on threshold
        recommended = value >= 0.5
        
        return recommended, confidence

    def evaluate_card_for_sequence(self, card):
        """
        Advanced evaluation of a card's strategic value for multi-turn sequence planning.
        
        Considers multiple aspects of a card's potential impact and strategic value.
        
        Args:
            card: Card object to evaluate
        
        Returns:
            float: Strategic value score for the card
        """
        if not card:
            return 0.0
        
        # Initialize base value components
        base_value = 0.0
        type_value = 0.0
        mana_value = 0.0
        ability_value = 0.0
        
        # Type-based valuation with more nuanced scoring
        if hasattr(card, 'card_types'):
            # Specific type weights
            type_weights = {
                'creature': {
                    'base': 1.0,
                    'evasive_bonus': 0.3,  # Flying, unblockable etc.
                    'etb_bonus': 0.2,       # Enters-the-battlefield effects
                    'scaling_bonus': 0.2    # Cards that get better over time
                },
                'planeswalker': {
                    'base': 1.5,
                    'loyalty_bonus': 0.3,   # Ability to generate ongoing value
                    'ultimate_potential': 0.2
                },
                'instant': {
                    'base': 0.8,
                    'interaction_bonus': 0.3,  # Removal, counters, etc.
                    'surprise_factor': 0.2     # Can be cast at unexpected times
                },
                'sorcery': {
                    'base': 0.7,
                    'sweeper_bonus': 0.4,   # Board wipes
                    'game_changer_bonus': 0.3
                },
                'enchantment': {
                    'base': 0.6,
                    'continuous_effect_bonus': 0.4,
                    'synergy_potential': 0.3
                },
                'artifact': {
                    'base': 0.5,
                    'utility_bonus': 0.3,
                    'combo_potential': 0.4
                }
            }
            
            # Evaluate each card type
            for card_type in card.card_types:
                type_config = type_weights.get(card_type.lower(), {'base': 0.5})
                type_value += type_config['base']
                
                # Additional type-specific evaluations
                if hasattr(card, 'oracle_text'):
                    oracle_text = card.oracle_text.lower()
                    
                    # Evasive creatures bonus
                    if card_type == 'creature' and any(keyword in oracle_text for keyword in ['flying', 'unblockable', 'can\'t be blocked']):
                        type_value += type_config.get('evasive_bonus', 0)
                    
                    # Enters-the-battlefield effects for creatures
                    if card_type == 'creature' and 'when' in oracle_text and 'enters the battlefield' in oracle_text:
                        type_value += type_config.get('etb_bonus', 0)
                    
                    # Planeswalker loyalty and ultimate potential
                    if card_type == 'planeswalker':
                        if 'ultimate' in oracle_text:
                            type_value += type_config.get('ultimate_potential', 0)
        
        # Mana cost considerations
        card_cmc = 0.0
        if hasattr(card, 'cmc'):
            try:
                card_cmc = float(getattr(card, 'cmc', 0) or 0)
            except (TypeError, ValueError):
                card_cmc = 0.0
            # More sophisticated mana curve evaluation
            if card_cmc <= 2:
                mana_value += 0.5  # Early game efficiency
            elif card_cmc <= 4:
                mana_value += 0.4  # Mid-game impact
            elif card_cmc <= 6:
                mana_value += 0.3  # Late game power
            else:
                mana_value += 0.2  # Very late game bombs
            
            # Discount for uncastable cards
            mana_value *= max(0.2, 1.0 - (card_cmc * 0.05))
        
        # Ability and effect evaluation
        if hasattr(card, 'oracle_text'):
            oracle_text = card.oracle_text.lower()
            
            # Identify powerful effects
            powerful_effects = {
                'draw': 0.4,      # Card advantage
                'destroy': 0.5,   # Removal
                'exile': 0.6,     # Stronger removal
                'counter': 0.5,   # Interaction
                'gain life': 0.3, # Sustain
                'create token': 0.4,  # Board presence
                'proliferate': 0.3,   # Counter manipulation
                'scry': 0.2,      # Deck manipulation
                'search library': 0.3 # Tutor effects
            }
            
            for effect, bonus in powerful_effects.items():
                if effect in oracle_text:
                    ability_value += bonus
            
            # Combo and synergy potential
            if any(keyword in oracle_text for keyword in ['whenever', 'if', 'each', 'for each']):
                ability_value += 0.3  # Potential for complex interactions
        
        # Creature-specific evaluation
        if 'creature' in getattr(card, 'card_types', []):
            try:
                power = float(card.power)
                toughness = float(card.toughness)
                stats_are_finite = (
                    math.isfinite(power) and math.isfinite(toughness))
            except (TypeError, ValueError):
                stats_are_finite = False
            if not stats_are_finite:
                power = toughness = 0.0
            # Power to mana cost efficiency
            power_efficiency = power / max(1.0, card_cmc)
            toughness_efficiency = toughness / max(1.0, card_cmc)
            
            base_value += min(power_efficiency, 1.0) * 0.3
            base_value += min(toughness_efficiency, 1.0) * 0.2
        
        # Combine all components
        total_value = (
            base_value + 
            type_value * 1.2 + 
            mana_value * 1.1 + 
            ability_value * 1.3
        )
        
        # Strategy-specific modifier
        if hasattr(self, 'strategy_type'):
            strategy_modifiers = {
                'aggro': 1.3,     # Favor aggressive, low-cost cards
                'control': 1.2,   # Value interactive, high-impact cards
                'midrange': 1.1,  # Balanced approach
                'combo': 1.4,     # Prioritize synergistic, complex cards
                'tempo': 1.2      # Value efficient, low-cost cards
            }
            total_value *= strategy_modifiers.get(self.strategy_type, 1.0)
        
        # Normalize and clip the value
        return max(0.0, min(total_value, 2.0))

    def evaluate_play_card_action(self, card_id, context=None):
        """
        Comprehensively evaluate the strategic value of playing a specific card with
        enhanced context awareness and synergy detection.
        
        Args:
            card_id: ID of the card to evaluate
            context: Optional context dictionary with additional information
                
        Returns:
            float: Strategic value of playing the card (0.0 to 5.0 scale)
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
            
        # Ensure we have current analysis
        if not self.current_analysis:
            self.analyze_game_state()
            
        # Use card evaluator if available
        if self.card_evaluator and hasattr(self.card_evaluator, 'evaluate_card'):
            try:
                # Mechanical action context (slot, card ID, chosen face) must
                # augment rather than replace the strategic evaluation state.
                analysis = (
                    self.current_analysis
                    if isinstance(self.current_analysis, dict) else {})
                player_idx = 0 if gs.agent_is_p1 else 1
                analytics_archetype = getattr(
                    gs, "deck_archetypes", {}).get(
                        player_idx, self.strategy_type)
                strategic_context = {
                    "game_stage": analysis.get(
                        "game_info", {}).get("game_stage", "mid"),
                    "position": analysis.get(
                        "position", {}).get("overall", "even"),
                    "aggression_level": self.aggression_level,
                    "strategy_type": self.strategy_type,
                    # Historical lookup uses the same episode label that owns
                    # terminal CardMemory recording. strategy_type remains the
                    # planner's independent tactical posture.
                    "deck_archetype": analytics_archetype,
                    "turn": gs.turn,
                    "phase": gs.phase,
                }
                strategic_context.update(dict(context or {}))

                return self.card_evaluator.evaluate_card(
                    card_id, "play", strategic_context)
            except Exception as e:
                logging.warning(f"Error using card evaluator: {e}, falling back to internal evaluation")
        
        # Fallback: Enhanced internal evaluation logic
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Initialize evaluation score components
        base_value = 0.0
        synergy_value = 0.0
        tempo_value = 0.0
        tactical_value = 0.0
        context_value = 0.0
        
        # Obtain game state information
        game_stage = self.current_analysis["game_info"]["game_stage"]
        board_position = self.current_analysis["position"]["overall"]
        life_diff = self.current_analysis["life"]["life_diff"]
        opponent_creatures = self.current_analysis["board_state"]["opp_creatures"]
        my_creatures = self.current_analysis["board_state"]["my_creatures"]
        
        # 1. Basic card quality assessment (0-1 scale)
        if hasattr(card, 'card_types'):
            # Base value from card type with enhanced weighting by strategy
            for card_type in card.card_types:
                type_weight = self.strategy_params["card_weights"].get(card_type.lower(), 1.0)
                base_value += type_weight * 0.2  # Scale appropriately
                
            # Creature evaluation
            if 'creature' in card.card_types:
                if hasattr(card, 'power') and hasattr(card, 'toughness'):
                    # Efficiency calculation (power+toughness per mana)
                    cmc = card.cmc if hasattr(card, 'cmc') else 1
                    efficiency = (card.power + card.toughness) / max(1, cmc)
                    base_value += min(0.6, efficiency * 0.2)  # Cap at 0.6
                    
                    # Evasion bonus
                    if hasattr(card, 'oracle_text'):
                        oracle_text = card.oracle_text.lower()
                        if any(keyword in oracle_text for keyword in 
                            ['flying', 'trample', 'menace', 'shadow', 'unblockable', "can't be blocked"]):
                            base_value += 0.2
                    
                    # P/T ratio analysis
                    if card.power > card.toughness + 1:
                        # High power, low toughness = aggressive
                        if self.strategy_type == "aggro":
                            base_value += 0.2
                    elif card.toughness > card.power + 1:
                        # High toughness, low power = defensive
                        if self.strategy_type == "control":
                            base_value += 0.2
            
            # Planeswalker evaluation
            elif 'planeswalker' in card.card_types:
                base_value += 0.7  # Planeswalkers are inherently high value
                if hasattr(card, 'loyalty'):
                    base_value += min(0.3, card.loyalty * 0.1)  # Higher loyalty = better
            
            # Spell evaluation
            elif 'instant' in card.card_types or 'sorcery' in card.card_types:
                # Check for key spell effects
                if hasattr(card, 'oracle_text'):
                    oracle_text = card.oracle_text.lower()
                    
                    # Card draw/advantage
                    if 'draw' in oracle_text:
                        draw_count = 1
                        match = re.search(r'draw (\w+) card', oracle_text)
                        if match:
                            try:
                                draw_word = match.group(1)
                                if draw_word.isdigit():
                                    draw_count = int(draw_word)
                                elif draw_word == 'two':
                                    draw_count = 2
                                elif draw_word == 'three':
                                    draw_count = 3
                            except:
                                pass
                        base_value += min(0.7, 0.2 * draw_count)
                    
                    # Removal/interaction
                    if any(term in oracle_text for term in ['destroy', 'exile', 'damage', 'counter']):
                        base_value += 0.4
                        
                        # Higher value for board wipes when opponent has many creatures
                        if 'all' in oracle_text and 'creature' in oracle_text and opponent_creatures > 2:
                            base_value += 0.4
                            
                        # Higher value for targeted removal against specific high threats
                        threats = self.assess_threats()
                        if threats and threats[0]["level"] > 3:
                            base_value += 0.3
        
        # 2. Synergy assessment (0-1 scale)
        # Check synergies with cards on battlefield
        if len(me["battlefield"]) > 0:
            synergy_score, synergy_details = self.identify_card_synergies(card_id, me["hand"], me["battlefield"])
            synergy_value = min(1.0, synergy_score * 0.25)  # Scale appropriately
        
        # 3. Tempo assessment (0-1 scale)
        if hasattr(card, 'cmc'):
            # On-curve play bonus (highest when played on curve)
            curve_distance = abs(card.cmc - gs.turn)
            if curve_distance == 0:
                tempo_value += 0.3
            elif curve_distance == 1:
                tempo_value += 0.2
            elif curve_distance == 2:
                tempo_value += 0.1
            
            # Mana efficiency
            available_mana = sum(me["mana_pool"].values())
            mana_usage_ratio = card.cmc / max(1, available_mana)
            if mana_usage_ratio <= 1.0:
                tempo_value += mana_usage_ratio * 0.4  # More value when using most available mana
            
            # Overcosted penalty
            average_cmc_for_type = 3  # Default
            if 'creature' in card.card_types:
                average_creature_stats = card.power + card.toughness if hasattr(card, 'power') and hasattr(card, 'toughness') else 4
                expected_cmc = average_creature_stats / 2
                if card.cmc > expected_cmc + 1:
                    tempo_value -= min(0.3, (card.cmc - expected_cmc) * 0.1)
        
        # 4. Tactical value based on game state (0-1 scale)
        # Defensive plays when behind
        if board_position in ["behind", "struggling"]:
            if 'creature' in card.card_types and hasattr(card, 'toughness') and card.toughness >= 3:
                tactical_value += 0.3  # Good blocker when behind
            
            if hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                if any(term in oracle_text for term in ['destroy', 'exile', 'damage to']):
                    tactical_value += 0.4  # Removal is critical when behind
                
                if 'gain life' in oracle_text and life_diff < 0:
                    tactical_value += min(0.3, abs(life_diff) * 0.03)  # More valuable at lower life
        
        # Offensive plays when ahead
        elif board_position in ["ahead", "dominating"]:
            if 'creature' in card.card_types and hasattr(card, 'power') and card.power >= 3:
                tactical_value += 0.3  # Good attacker when ahead
            
            if hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                if any(term in oracle_text for term in ['double', 'damage', 'combat']):
                    tactical_value += 0.3  # Combat enhancers valuable when ahead
        
        # 5. Context-specific value (0-1 scale)
        # Game stage considerations
        if game_stage == "early":
            if hasattr(card, 'cmc') and card.cmc <= 2:
                context_value += 0.2  # Early plays valuable in early game
            
            if 'land' in card.card_types:
                context_value += 0.5  # Lands extremely valuable early
        
        elif game_stage == "mid":
            if hasattr(card, 'cmc') and 3 <= card.cmc <= 5:
                context_value += 0.2  # Mid-cost cards valuable in mid game
            
            if hasattr(card, 'oracle_text') and 'draw' in card.oracle_text.lower():
                context_value += 0.2  # Card advantage important in mid game
        
        elif game_stage == "late":
            if hasattr(card, 'cmc') and card.cmc >= 5:
                context_value += 0.2  # High-impact plays valuable in late game
            
            # Win condition cards get a boost late game
            win_conditions = self.identify_win_conditions()
            for wc_name, wc_data in win_conditions.items():
                if wc_data["viable"] and card_id in wc_data.get("key_cards", []):
                    context_value += 0.4
                    break
        
        # Phase-specific considerations
        if gs.phase in [gs.PHASE_MAIN_PRECOMBAT]:
            if 'creature' in card.card_types:
                # Creatures with haste more valuable pre-combat
                if hasattr(card, 'oracle_text') and 'haste' in card.oracle_text.lower():
                    context_value += 0.2
        
        elif gs.phase in [gs.PHASE_MAIN_POSTCOMBAT]:
            if 'creature' in card.card_types:
                # Defensive creatures more valuable post-combat
                if hasattr(card, 'toughness') and card.toughness > card.power:
                    context_value += 0.1
        
        # Special cases - land drops
        if ('land' in card.card_types
                and gs.can_play_land_this_turn(me)):
            # Essential to make land drops
            lands_in_play = sum(1 for cid in me["battlefield"] 
                            if gs._safe_get_card(cid) and 'land' in gs._safe_get_card(cid).card_types)
            
            if lands_in_play < gs.turn:
                context_value += 0.8  # Very high value to catch up on lands
            else:
                context_value += 0.5  # Still important to make land drops on curve
        
        # Combine all components with appropriate weights
        # Weights vary by card type and game stage
        if 'land' in card.card_types:
            # Lands are primarily evaluated on context and tempo
            value = (base_value * 0.1 +
                    synergy_value * 0.2 +
                    tempo_value * 0.3 +
                    tactical_value * 0.1 +
                    context_value * 0.3)
        
        elif 'creature' in card.card_types:
            if game_stage == "early":
                # Early game: tempo and base card quality matter most
                value = (base_value * 0.3 +
                        synergy_value * 0.1 +
                        tempo_value * 0.3 +
                        tactical_value * 0.2 +
                        context_value * 0.1)
            else:
                # Mid/late: synergy and tactical considerations matter more
                value = (base_value * 0.2 +
                        synergy_value * 0.25 +
                        tempo_value * 0.15 +
                        tactical_value * 0.25 +
                        context_value * 0.15)
        
        else:  # Spells, enchantments, artifacts, etc.
            if game_stage == "early":
                value = (base_value * 0.25 +
                        synergy_value * 0.15 +
                        tempo_value * 0.25 +
                        tactical_value * 0.2 +
                        context_value * 0.15)
            else:
                value = (base_value * 0.2 +
                        synergy_value * 0.25 +
                        tempo_value * 0.15 +
                        tactical_value * 0.25 +
                        context_value * 0.15)
        
        # Apply strategy-specific adjustments
        if self.strategy_type == "aggro":
            # Aggro values creatures and direct damage higher
            if 'creature' in card.card_types:
                value += 0.2
            elif hasattr(card, 'oracle_text') and 'damage' in card.oracle_text.lower():
                value += 0.2
        
        elif self.strategy_type == "control":
            # Control values card advantage and removal higher
            if hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                if 'draw' in oracle_text:
                    value += 0.2
                if any(term in oracle_text for term in ['destroy', 'exile', 'counter']):
                    value += 0.2
        
        elif self.strategy_type == "combo":
            # Combo values card selection and combo pieces higher
            win_conditions = self.identify_win_conditions()
            for wc_name, wc_data in win_conditions.items():
                if wc_name == "combo" and wc_data["viable"] and card_id in wc_data.get("key_cards", []):
                    value += 0.5
                    break
        
        # Final adjustment for aggression level
        value += (self.aggression_level - 0.5) * 0.2  # -0.1 to +0.1 based on aggression
        
        # Debug logging
        card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
        logging.debug(f"Card evaluation for {card_name}: total={value:.2f} (base={base_value:.2f}, synergy={synergy_value:.2f}, tempo={tempo_value:.2f}, tactical={tactical_value:.2f}, context={context_value:.2f})")
        
        # Return final value (0-5 scale)
        return max(0.0, min(5.0, value * 5))

    def evaluate_attack_action(self, attacker_ids, simulation=None):
        """
        Evaluate the strategic value of a specific attack configuration using CombatResolver.
        
        Args:
            attacker_ids: List of creature IDs to attack with
            
        Returns:
            float: Strategic value of the attack
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Use combat resolver if available
        if self.combat_resolver:
            # Attack search already simulated this exact candidate. Accepting
            # that immutable result avoids repeating the dominant combat
            # advisory cost for every combination.
            if simulation is None:
                original_attackers = list(getattr(
                    gs, 'current_attackers', []))
                try:
                    gs.current_attackers = list(attacker_ids)
                    simulation = self.combat_resolver.simulate_combat()
                finally:
                    gs.current_attackers = original_attackers
            
            # Extract simulation results
            damage_to_opponent = simulation.get("damage_to_player", 0)
            my_creatures_lost = len(simulation.get("attackers_dying", []))
            their_creatures_lost = len(simulation.get("blockers_dying", []))
            life_gained = simulation.get("life_gained", 0)
            
            # Evaluate based on simulation results
            value = 0.0
            
            # Damage to opponent is good
            value += damage_to_opponent * 0.2
            
            # Favorable trades are good
            trade_value = their_creatures_lost - my_creatures_lost
            value += trade_value * 0.5
            
            # Life gain is good
            value += life_gained * 0.1
            
            # Potential lethal is very good
            if damage_to_opponent >= opp["life"]:
                value += 2.0
            
        else:
            # Fallback to simpler evaluation if no combat resolver
            if not attacker_ids:
                return 0.0
            
            # Calculate total attacking power
            total_power = sum(gs._safe_get_card(cid).power 
                            for cid in attacker_ids 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
            
            # Estimate potential damage
            opp_blockers = [cid for cid in opp["battlefield"] 
                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                        and 'creature' in gs._safe_get_card(cid).card_types]
            
            total_blocker_toughness = sum(gs._safe_get_card(cid).toughness 
                                        for cid in opp_blockers 
                                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))
            
            # Estimate damage and losses
            damage_to_opponent = max(0, total_power - total_blocker_toughness)
            my_creatures_lost = min(len(attacker_ids), len(opp_blockers))
            their_creatures_lost = min(len(opp_blockers), total_power // 2)  # Rough estimate
            
            # Basic evaluation
            value = damage_to_opponent * 0.2 + (their_creatures_lost - my_creatures_lost) * 0.5
            
            # Potential lethal is very good
            if damage_to_opponent >= opp["life"]:
                value += 2.0
        
        # Additional strategic considerations
        
        # Ensure we have current analysis
        if not self.current_analysis:
            self.analyze_game_state()
        
        # Game stage considerations
        game_stage = self.current_analysis["game_info"]["game_stage"]
        
        if game_stage == "early":
            # Early game: more conservative attacks unless aggro
            if self.strategy_type == "aggro":
                value += 0.5  # Aggro decks want to attack early
            elif my_creatures_lost > 0 and their_creatures_lost == 0:
                value -= 0.5  # Bad trades are especially bad early
        elif game_stage == "mid":
            # Mid game: value favorable trades more
            if their_creatures_lost > my_creatures_lost:
                value += 0.3  # Good trades are valuable
        else:
            # Late game: more aggressive, damage is more valuable
            value += damage_to_opponent * 0.1  # Additional value for damage
        
        # Board position considerations
        position = self.current_analysis["position"]["overall"]
        if position in ["behind", "struggling"]:
            # If behind, we need to be more careful
            if my_creatures_lost > 0 and their_creatures_lost == 0:
                value -= 0.7  # Bad trades are very bad when behind
        elif position in ["ahead", "dominating"]:
            # If ahead, we can be more aggressive
            value += damage_to_opponent * 0.1  # Additional value for damage
        
        # Aggressive strategy adjustment
        value += (self.aggression_level - 0.5) * 1.0  # -0.5 to +0.5 based on aggression
        
        # Risk tolerance adjustment
        if my_creatures_lost > 0:
            value += (self.risk_tolerance - 0.5) * 0.5 * my_creatures_lost  # Risk adjustment
        
        logging.debug(f"Attack evaluation: {len(attacker_ids)} attackers, value={value:.2f}")
        
        return value

    def evaluate_block_action(self, attacker_id, blocker_ids):
        """
        Evaluate the strategic value of a specific block configuration using CombatResolver.
        
        Args:
            attacker_id: ID of the attacking creature
            blocker_ids: List of creature IDs to block with
            
        Returns:
            float: Strategic value of the block
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        blocker_ids = list(blocker_ids or ())

        attacker = gs._safe_get_card(attacker_id)
        blocker_pairs = [
            (blocker_id, gs._safe_get_card(blocker_id))
            for blocker_id in blocker_ids]
        blocker_pairs = [
            (blocker_id, blocker) for blocker_id, blocker in blocker_pairs
            if blocker]
        if not attacker or not blocker_pairs:
            return 0.0

        blockers = [blocker for _, blocker in blocker_pairs]
        attacker_power = max(0.0, _card_number(attacker, "power"))
        attacker_toughness = max(0.0, _card_number(attacker, "toughness"))
        blocker_total_power = sum(
            max(0.0, _card_number(blocker, "power"))
            for blocker in blockers)
        blocker_total_toughness = sum(
            max(0.0, _card_number(blocker, "toughness"))
            for blocker in blockers)

        simulation = None
        if self.combat_resolver:
            original_attackers = getattr(gs, 'current_attackers', [])
            original_blocks = getattr(
                gs, 'current_block_assignments', {})
            original_agent_is_p1 = gs.agent_is_p1
            try:
                gs.current_attackers = [attacker_id]
                gs.current_block_assignments = {
                    attacker_id: [
                        blocker_id for blocker_id, _ in blocker_pairs]}
                attacker_controller = (
                    gs.get_card_controller(attacker_id)
                    if hasattr(gs, "get_card_controller") else None)
                if attacker_controller is not None:
                    gs.agent_is_p1 = attacker_controller is gs.p1
                simulation = self.combat_resolver.simulate_combat()
            except Exception as exc:
                logging.warning(
                    "Combat block simulation failed for attacker %s: %s; "
                    "using finite combat-math fallback", attacker_id, exc)
            finally:
                gs.current_attackers = original_attackers
                gs.current_block_assignments = original_blocks
                gs.agent_is_p1 = original_agent_is_p1

        if not isinstance(simulation, dict):
            simulation = None

        if simulation is not None:
            try:
                attackers_dying = set(
                    simulation.get("attackers_dying", ()) or ())
            except TypeError:
                attackers_dying = set()
            try:
                blockers_dying = set(
                    simulation.get("blockers_dying", ()) or ())
            except TypeError:
                blockers_dying = set()
            attacker_dies = attacker_id in attackers_dying
            blockers_die = [
                blocker_id for blocker_id, _ in blocker_pairs
                if blocker_id in blockers_dying]
            damage_to_player = max(
                0.0, _finite_number(
                    simulation.get("damage_to_player", 0.0)))
            damage_prevented = max(
                0.0, attacker_power - damage_to_player)
        else:
            deathtouch_blocker = any(
                "deathtouch" in str(
                    getattr(blocker, "oracle_text", "")).lower()
                and _card_number(blocker, "power") > 0
                for blocker in blockers)
            attacker_dies = bool(
                attacker_toughness > 0
                and (blocker_total_power >= attacker_toughness
                     or deathtouch_blocker))
            attacker_has_deathtouch = (
                "deathtouch" in str(
                    getattr(attacker, "oracle_text", "")).lower())
            blockers_die = [
                blocker_id for blocker_id, blocker in blocker_pairs
                if (attacker_power > 0
                    and (attacker_has_deathtouch
                         or attacker_power >= max(
                             0.0, _card_number(blocker, "toughness"))))]
            attacker_has_trample = (
                "trample" in str(
                    getattr(attacker, "oracle_text", "")).lower())
            damage_prevented = attacker_power
            if attacker_has_trample:
                damage_prevented = min(attacker_power, blocker_total_toughness)

        value = 0.0
        if attacker_dies:
            value += 1.0
        value -= len(blockers_die) * 0.5
        value += damage_prevented * 0.2
        
        # Additional strategic considerations
        
        # Ensure we have current analysis
        if not self.current_analysis:
            try:
                self.analyze_game_state()
            except Exception as exc:
                logging.warning(
                    "Strategic analysis failed during block evaluation: %s",
                    exc)
        
        # Life total considerations
        my_life = _finite_number(me.get("life", 20), 20.0)
        
        # If we're low on life, preventing damage is more important
        if my_life <= 5:
            value += damage_prevented * 0.5  # Extra value for damage prevention
        elif my_life <= 10:
            value += damage_prevented * 0.3  # Moderate extra value
        
        # Game stage considerations
        analysis = (
            self.current_analysis
            if isinstance(self.current_analysis, dict) else {})
        game_stage = analysis.get("game_info", {}).get(
            "game_stage", "mid")
        
        if game_stage == "early":
            # Early game: preserve creatures unless good trade
            if len(blockers_die) > 0 and not attacker_dies:
                value -= 0.3  # Losing blockers for no gain is worse early
        elif game_stage == "late":
            # Late game: life becomes more valuable
            value += damage_prevented * 0.1  # Extra value for damage prevention
        
        # Defensive strategy adjustment
        aggression = min(
            1.0, max(0.0, _finite_number(self.aggression_level, 0.5)))
        value += (0.5 - aggression)
        
        # Risk tolerance adjustment for potentially losing blockers
        if len(blockers_die) > 0:
            risk = min(
                1.0, max(0.0, _finite_number(self.risk_tolerance, 0.5)))
            value += (risk - 0.5) * 0.5 * len(blockers_die)
        
        logging.debug(f"Block evaluation: {len(blockers)} blockers vs. attacker {attacker.name if hasattr(attacker, 'name') else 'unknown'}, value={value:.2f}")
        
        return _finite_number(value)

    def evaluate_ability_activation(self, card_id, ability_idx):
        """
        Comprehensively evaluate the strategic value of activating a specific ability
        with enhanced cost-benefit analysis and contextual awareness.
        
        Args:
            card_id: ID of the card with the ability
            ability_idx: Index of the ability to evaluate
                
        Returns:
            tuple: (value: float, reasoning: str) - Value score and explanation
        """
        gs = self.game_state
        # Check if ability handler exists
        if not hasattr(gs, 'ability_handler') or not gs.ability_handler:
            return 0.0, "No ability handler available"
        
        # Check if card exists
        card = gs._safe_get_card(card_id)
        if not card:
            return -1.0, "Card not found"
        # BUGFIX: was computed before `card` was fetched -> NameError on every call.
        card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
        
        # Check if ability exists
        activated_abilities = gs.ability_handler.get_activated_abilities(card_id)
        if (not isinstance(ability_idx, int) or ability_idx < 0
                or ability_idx >= len(activated_abilities)):
            return -1.0, "Invalid ability index"
        
        ability = activated_abilities[ability_idx]
        
        # Check if ability can be activated
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        if not gs.ability_handler.can_activate_ability(card_id, ability_idx, me):
            return 0.0, "Cannot activate ability (cost cannot be paid)"
        
        # Analyze the ability effect
        ability_text = ability.effect.lower() if hasattr(ability, 'effect') else ""
        cost_text = ability.cost.lower() if hasattr(ability, 'cost') else ""
        
        # Get game state analysis if available
        if not hasattr(self, 'current_analysis') or not self.current_analysis:
            self.analyze_game_state()
        
        # Get current game context
        game_stage = self.current_analysis["game_info"]["game_stage"]
        board_position = self.current_analysis["position"]["overall"]
        phase = gs.phase
        turn = gs.turn
        my_life = self.current_analysis["life"]["my_life"]
        opp_life = self.current_analysis["life"]["opp_life"]
        
        # 1. Cost Analysis (negative value)
        cost_value = 0.0
        cost_reasoning = []
        
        # Mana cost analysis
        if '{' in cost_text and '}' in cost_text:
            mana_matches = re.findall(r'\{([^\}]+)\}', cost_text)
            total_mana_cost = 0
            colored_requirements = []
            
            for mana_symbol in mana_matches:
                if mana_symbol.isdigit():
                    total_mana_cost += int(mana_symbol)
                elif mana_symbol in ['W', 'U', 'B', 'R', 'G']:
                    total_mana_cost += 1
                    colored_requirements.append(mana_symbol)
                elif mana_symbol == 'X':
                    # X costs are variable, assume at least 1
                    total_mana_cost += 1
            
            # Calculate current available mana
            available_mana = sum(me["mana_pool"].values())
            available_colored = {color: me["mana_pool"].get(color, 0) for color in ['W', 'U', 'B', 'R', 'G']}
            
            # Check if colored requirements can be met
            colored_satisfied = all(available_colored.get(color, 0) > 0 for color in colored_requirements)
            
            # Analyze mana cost relative to available mana
            mana_ratio = total_mana_cost / max(1, available_mana)
            
            if mana_ratio > 0.8:
                cost_value -= 0.4  # High cost relative to available mana
                cost_reasoning.append(f"Uses most available mana ({total_mana_cost}/{available_mana})")
            elif mana_ratio > 0.5:
                cost_value -= 0.2  # Moderate cost
                cost_reasoning.append(f"Uses significant mana ({total_mana_cost}/{available_mana})")
            else:
                cost_value -= 0.1  # Low cost
                cost_reasoning.append(f"Uses little mana ({total_mana_cost}/{available_mana})")
            
            # Additional penalty if colored requirements are hard to meet
            if not colored_satisfied:
                cost_value -= 0.3
                cost_reasoning.append("Difficult color requirements")
        
        # Tap cost analysis
        if '{t}' in cost_text or 'tap' in cost_text:
            # Check what phase we're in
            in_combat = phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, gs.PHASE_COMBAT_DAMAGE]
            pre_combat = phase == gs.PHASE_MAIN_PRECOMBAT
            
            # For creatures, tapping is a bigger cost before combat
            if hasattr(card, 'card_types') and 'creature' in card.card_types:
                if pre_combat:
                    has_summoning_sickness = card_id in me.get("entered_battlefield_this_turn", set())
                    if not has_summoning_sickness and not in_combat:
                        # Analyze if creature could attack profitably
                        can_attack_profitably = False
                        if hasattr(card, 'power'):
                            # Check if opponent has blockers that could trade or better
                            opp_creatures = [cid for cid in opp["battlefield"] 
                                            if gs._safe_get_card(cid) and 
                                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                                            'creature' in gs._safe_get_card(cid).card_types]
                            
                            if not opp_creatures:
                                can_attack_profitably = True
                            else:
                                # Check for evasion
                                has_evasion = False
                                if hasattr(card, 'oracle_text'):
                                    evasion_abilities = ['flying', 'trample', 'menace', 'intimidate', 'shadow', 'unblockable']
                                    has_evasion = any(ability in card.oracle_text.lower() for ability in evasion_abilities)
                                
                                if has_evasion:
                                    can_attack_profitably = True
                                else:
                                    # Check if creature can attack without being blocked profitably
                                    opp_blockers = [gs._safe_get_card(cid) for cid in opp_creatures 
                                                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness')]
                                    
                                    can_attack_profitably = not any(blocker.toughness >= card.power for blocker in opp_blockers)
                        
                        if can_attack_profitably:
                            cost_value -= 0.5  # High cost to tap potential attacker
                            cost_reasoning.append("Gives up potential attack")
                        else:
                            cost_value -= 0.2  # Lower cost if attacking isn't profitable
                            cost_reasoning.append("Tapping creature with limited attack potential")
                elif in_combat:
                    # Once in combat, tapping is less of an issue unless card could block
                    opp_attacking = len(gs.current_attackers) > 0 if hasattr(gs, 'current_attackers') else False
                    if opp_attacking and phase == gs.PHASE_DECLARE_BLOCKERS:
                        cost_value -= 0.4  # Higher cost during blocker declaration
                        cost_reasoning.append("Gives up potential blocking")
                    else:
                        cost_value -= 0.1  # Minimal cost otherwise
                        cost_reasoning.append("Tapping has minimal downside now")
                else:
                    # Post-combat or end step, tapping is less relevant
                    cost_value -= 0.1
                    cost_reasoning.append("Tapping has minimal downside now")
            else:
                # For non-creatures, tapping is generally a consistent cost
                cost_value -= 0.2
                cost_reasoning.append("Tapping permanent for ability")
        
        # Life payment cost
        life_payment = 0
        if 'pay' in cost_text and 'life' in cost_text:
            match = re.search(r'pay (\d+) life', cost_text)
            if match:
                life_payment = int(match.group(1))
                
                # Scale cost based on current life total and game position
                life_percentage = life_payment / max(1, my_life)
                
                if life_percentage > 0.25:  # Paying >25% of current life
                    cost_value -= 0.6
                    cost_reasoning.append(f"Very high life payment ({life_payment}, {life_percentage*100:.0f}% of life)")
                elif life_percentage > 0.1:  # Paying >10% of current life
                    cost_value -= 0.4
                    cost_reasoning.append(f"Significant life payment ({life_payment}, {life_percentage*100:.0f}% of life)")
                else:
                    cost_value -= 0.2
                    cost_reasoning.append(f"Moderate life payment ({life_payment}, {life_percentage*100:.0f}% of life)")
                
                # Higher cost when at low life
                if my_life <= 5:
                    cost_value -= 0.4
                elif my_life <= 10:
                    cost_value -= 0.2
        
        # Sacrifice cost
        if 'sacrifice' in cost_text:
            sacrifice_targets = []
            
            # Check what's being sacrificed
            if 'sacrifice a creature' in cost_text:
                sacrifice_targets = [cid for cid in me["battlefield"] 
                                    if gs._safe_get_card(cid) and 
                                    hasattr(gs._safe_get_card(cid), 'card_types') and 
                                    'creature' in gs._safe_get_card(cid).card_types]
                
                if sacrifice_targets:
                    # Evaluate creatures that could be sacrificed
                    best_sacrifice = min(sacrifice_targets, 
                                        key=lambda cid: gs._safe_get_card(cid).power if hasattr(gs._safe_get_card(cid), 'power') else 0)
                    
                    sacrifice_card = gs._safe_get_card(best_sacrifice)
                    if hasattr(sacrifice_card, 'power'):
                        if sacrifice_card.power <= 1:
                            cost_value -= 0.3  # Low value creature
                            cost_reasoning.append(f"Sacrificing low-value creature")
                        elif sacrifice_card.power <= 3:
                            cost_value -= 0.5  # Medium value creature
                            cost_reasoning.append(f"Sacrificing medium-value creature")
                        else:
                            cost_value -= 0.8  # High value creature
                            cost_reasoning.append(f"Sacrificing high-value creature")
                    else:
                        cost_value -= 0.4  # Default value
                        cost_reasoning.append(f"Sacrificing creature")
                else:
                    # No valid sacrifice targets
                    return 0.0, "No valid sacrifice targets"
            
            elif 'sacrifice a land' in cost_text:
                sacrifice_targets = [cid for cid in me["battlefield"] 
                                    if gs._safe_get_card(cid) and 
                                    hasattr(gs._safe_get_card(cid), 'card_types') and 
                                    'land' in gs._safe_get_card(cid).card_types]
                
                if sacrifice_targets:
                    lands_count = len(sacrifice_targets)
                    if lands_count <= 3:
                        cost_value -= 0.7  # Very high cost when low on lands
                        cost_reasoning.append("Sacrificing land with few lands in play")
                    elif lands_count <= 5:
                        cost_value -= 0.5  # High cost with moderate lands
                        cost_reasoning.append("Sacrificing land with moderate land count")
                    else:
                        cost_value -= 0.3  # Moderate cost with many lands
                        cost_reasoning.append("Sacrificing land with sufficient lands in play")
                else:
                    # No valid sacrifice targets
                    return 0.0, "No valid sacrifice targets"
            
            elif 'sacrifice an artifact' in cost_text:
                sacrifice_targets = [cid for cid in me["battlefield"] 
                                    if gs._safe_get_card(cid) and 
                                    hasattr(gs._safe_get_card(cid), 'card_types') and 
                                    'artifact' in gs._safe_get_card(cid).card_types]
                
                if sacrifice_targets:
                    cost_value -= 0.4  # Moderate cost
                    cost_reasoning.append("Sacrificing artifact")
                else:
                    # No valid sacrifice targets
                    return 0.0, "No valid sacrifice targets"
            
            # Self-sacrifice
            elif 'sacrifice this' in cost_text or 'sacrifice it' in cost_text:
                # Evaluate the card itself
                value = 0
                if hasattr(card, 'card_types'):
                    if 'creature' in card.card_types and hasattr(card, 'power'):
                        if card.power <= 1:
                            cost_value -= 0.3  # Low value creature
                        elif card.power <= 3:
                            cost_value -= 0.5  # Medium value creature
                        else:
                            cost_value -= 0.8  # High value creature
                    else:
                        cost_value -= 0.4  # Non-creature permanent
                
                cost_reasoning.append("Sacrificing self")
        
        # Discard cost
        if 'discard' in cost_text:
            if 'discard a card' in cost_text:
                # Check hand size
                hand_size = len(me["hand"])
                
                if hand_size <= 1:  # This would be our last card
                    cost_value -= 0.7
                    cost_reasoning.append("Discarding last card in hand")
                elif hand_size <= 3:
                    cost_value -= 0.4
                    cost_reasoning.append("Discarding with few cards in hand")
                else:
                    cost_value -= 0.2
                    cost_reasoning.append("Discarding with sufficient hand size")
            
            elif 'discard your hand' in cost_text:
                hand_size = len(me["hand"])
                
                if hand_size > 3:
                    cost_value -= 0.8
                    cost_reasoning.append(f"Discarding entire hand ({hand_size} cards)")
                elif hand_size > 0:
                    cost_value -= 0.4
                    cost_reasoning.append(f"Discarding entire hand ({hand_size} cards)")
                else:
                    cost_value -= 0.0  # No cost if hand is empty
                    cost_reasoning.append("No cards to discard")
        
        # Exile cost from graveyard
        if 'exile' in cost_text and 'graveyard' in cost_text:
            graveyard_size = len(me["graveyard"])
            
            if graveyard_size > 0:
                cost_value -= 0.2
                cost_reasoning.append(f"Exiling cards from graveyard")
                
                # Special case: delve or specific amounts
                if 'delve' in cost_text or re.search(r'exile (\d+) cards', cost_text):
                    match = re.search(r'exile (\d+) cards', cost_text)
                    exile_count = int(match.group(1)) if match else 1
                    
                    if exile_count > graveyard_size:
                        return 0.0, "Not enough cards in graveyard"
                    
                    cost_value -= 0.1 * exile_count  # Additional cost per card
            else:
                return 0.0, "No cards in graveyard to exile"
        
        # Calculate total cost value (negative number representing cost magnitude)
        total_cost_value = max(-1.0, cost_value)  # Cap at -1.0
        cost_summary = ", ".join(cost_reasoning)
        
        # 2. Effect Value Analysis (positive value)
        effect_value = 0.0
        effect_reasoning = []
        
        # Common effects parsing with more detail
        # Draw effects
        if 'draw' in ability_text:
            draw_count = 1
            match = re.search(r'draw (\w+) card', ability_text)
            if match:
                try:
                    draw_word = match.group(1)
                    if draw_word.isdigit():
                        draw_count = int(draw_word)
                    elif draw_word == 'two':
                        draw_count = 2
                    elif draw_word == 'three':
                        draw_count = 3
                except:
                    pass
            
            # Scale value based on current hand size and game stage
            hand_size = len(me["hand"])
            
            if hand_size <= 1:
                effect_value += 0.7 * draw_count  # Higher value when low on cards
                effect_reasoning.append(f"Drawing {draw_count} with nearly empty hand")
            elif hand_size <= 3:
                effect_value += 0.5 * draw_count
                effect_reasoning.append(f"Drawing {draw_count} with few cards in hand")
            else:
                effect_value += 0.3 * draw_count
                effect_reasoning.append(f"Drawing {draw_count} cards")
            
            # Higher value in control strategies
            if self.strategy_type == "control":
                effect_value += 0.1 * draw_count
        
        # Damage effects
        elif 'damage' in ability_text:
            damage_amount = 1
            match = re.search(r'(\d+) damage', ability_text)
            if match:
                damage_amount = int(match.group(1))
            
            # Check targets
            if 'any target' in ability_text:
                # Check if opponent has important creatures to target
                threats = self.assess_threats()
                if threats and threats[0]["level"] > 2:
                    effect_value += 0.5
                    effect_reasoning.append(f"Can remove significant threat with {damage_amount} damage")
                elif opp_life <= damage_amount:
                    effect_value += 1.0  # Lethal damage
                    effect_reasoning.append(f"Lethal {damage_amount} damage to opponent")
                elif opp_life <= 5:
                    effect_value += 0.7  # Near lethal
                    effect_reasoning.append(f"{damage_amount} damage to low-life opponent")
                else:
                    effect_value += 0.3
                    effect_reasoning.append(f"{damage_amount} damage to opponent")
            
            elif 'creature' in ability_text or 'target creature' in ability_text:
                # Check if there are good targets
                opp_creatures = [cid for cid in opp["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            'creature' in gs._safe_get_card(cid).card_types]
                
                if opp_creatures:
                    # Check for creatures that would die from the damage
                    lethal_targets = [cid for cid in opp_creatures 
                                    if gs._safe_get_card(cid) and 
                                    hasattr(gs._safe_get_card(cid), 'toughness') and 
                                    gs._safe_get_card(cid).toughness <= damage_amount]
                    
                    if lethal_targets:
                        # Find highest power creature we can kill
                        best_target = max(lethal_targets, 
                                        key=lambda cid: gs._safe_get_card(cid).power if hasattr(gs._safe_get_card(cid), 'power') else 0)
                        
                        target_power = gs._safe_get_card(best_target).power if hasattr(gs._safe_get_card(best_target), 'power') else 1
                        
                        if target_power >= 3:
                            effect_value += 0.7  # High value target
                            effect_reasoning.append(f"Can remove high-power creature with {damage_amount} damage")
                        else:
                            effect_value += 0.5  # Medium value target
                            effect_reasoning.append(f"Can remove creature with {damage_amount} damage")
                    else:
                        effect_value += 0.2  # Damage but not lethal
                        effect_reasoning.append(f"Non-lethal {damage_amount} damage to creature")
                else:
                    effect_value += 0.1  # No targets currently
                    effect_reasoning.append("No good creature targets for damage")
            
            elif 'player' in ability_text or 'opponent' in ability_text:
                if opp_life <= damage_amount:
                    effect_value += 1.0  # Lethal damage
                    effect_reasoning.append(f"Lethal {damage_amount} damage to opponent")
                elif opp_life <= 5:
                    effect_value += 0.7  # Near lethal
                    effect_reasoning.append(f"{damage_amount} damage to low-life opponent")
                elif self.strategy_type == "aggro" or self.strategy_type == "burn":
                    effect_value += 0.5  # Higher value in aggro
                    effect_reasoning.append(f"{damage_amount} damage to opponent (aggressive strategy)")
                else:
                    effect_value += 0.3
                    effect_reasoning.append(f"{damage_amount} damage to opponent")
        
        # Life gain effects
        elif 'gain' in ability_text and 'life' in ability_text:
            life_gain = 1
            match = re.search(r'gain (\d+) life', ability_text)
            if match:
                life_gain = int(match.group(1))
            
            # Scale value based on current life total and game position
            if my_life <= 5:
                effect_value += 0.7  # Critical life gain
                effect_reasoning.append(f"Gaining {life_gain} life at critical health")
            elif my_life <= 10:
                effect_value += 0.5  # Important life gain
                effect_reasoning.append(f"Gaining {life_gain} life at low health")
            elif board_position in ["behind", "struggling"]:
                effect_value += 0.3  # Tactical life gain when behind
                effect_reasoning.append(f"Gaining {life_gain} life while behind")
            else:
                effect_value += 0.2  # Standard life gain
                effect_reasoning.append(f"Gaining {life_gain} life")
            
            # Bonus for lifegain synergies
            lifegain_synergies = [cid for cid in me["battlefield"] 
                                if gs._safe_get_card(cid) and 
                                hasattr(gs._safe_get_card(cid), 'oracle_text') and
                                "whenever you gain life" in gs._safe_get_card(cid).oracle_text.lower()]
            
            if lifegain_synergies:
                effect_value += 0.3
                effect_reasoning.append("Triggers lifegain synergies")
        
        # Creature pump effects
        elif 'gets +' in ability_text:
            # Parse pump amount
            pump_power = 0
            pump_toughness = 0
            match = re.search(r'gets \+(\d+)/\+(\d+)', ability_text)
            if match:
                pump_power = int(match.group(1))
                pump_toughness = int(match.group(2))
            
            # Check phase - worth more during combat
            in_combat = phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, gs.PHASE_COMBAT_DAMAGE]
            pre_combat = phase == gs.PHASE_MAIN_PRECOMBAT
            
            # Value depends on timing
            if in_combat:
                effect_value += 0.5 + (pump_power + pump_toughness) * 0.1
                effect_reasoning.append(f"+{pump_power}/+{pump_toughness} during combat")
            elif pre_combat:
                effect_value += 0.3 + (pump_power + pump_toughness) * 0.05
                effect_reasoning.append(f"+{pump_power}/+{pump_toughness} before combat")
            else:
                effect_value += 0.2 + (pump_power + pump_toughness) * 0.03
                effect_reasoning.append(f"+{pump_power}/+{pump_toughness} outside combat")
        
        # Token creation
        elif 'create' in ability_text and 'token' in ability_text:
            # Parse token details
            token_count = 1
            token_power = 1
            token_toughness = 1
            
            # Try to extract token count
            match = re.search(r'create (\w+|[0-9]+)', ability_text)
            if match:
                token_word = match.group(1)
                if token_word.isdigit():
                    token_count = int(token_word)
                elif token_word in ['two', 'a pair of']:
                    token_count = 2
                elif token_word in ['three']:
                    token_count = 3
            
            # Try to extract token stats
            match = re.search(r'(\d+)/(\d+)', ability_text)
            if match:
                token_power = int(match.group(1))
                token_toughness = int(match.group(2))
            
            # Base value for tokens
            token_value = (token_power + token_toughness) / 4  # Scale appropriately
            
            # Special token bonuses
            has_evasion = False
            has_keywords = False
            
            if 'flying' in ability_text:
                has_evasion = True
                token_value += 0.1
            if 'vigilance' in ability_text or 'lifelink' in ability_text or 'first strike' in ability_text:
                has_keywords = True
                token_value += 0.1
            
            # Final token value
            effect_value += token_value * token_count
            
            token_description = f"{token_count} {token_power}/{token_toughness} token"
            if has_evasion:
                token_description += " with evasion"
            if has_keywords:
                token_description += " with keywords"
            
            effect_reasoning.append(f"Creating {token_description}")
            
            # Additional value in certain situations
            if board_position in ["behind", "struggling"] and token_toughness >= 3:
                effect_value += 0.2  # Defensive tokens valuable when behind
                effect_reasoning.append("Defensive tokens while behind")
            
            if self.strategy_type == "aggro" and token_power >= 2:
                effect_value += 0.2  # Offensive tokens valuable in aggro
                effect_reasoning.append("Offensive tokens for aggressive strategy")
        
        # Counter/removal effects
        elif any(term in ability_text for term in ['destroy', 'exile', 'counter']):
            # Check for targets
            if 'counter target spell' in ability_text:
                # Check if stack has targetable spells
                if gs.stack:
                    effect_value += 0.7
                    effect_reasoning.append("Can counter spell on stack")
                else:
                    effect_value += 0.2  # Still valuable as deterrent
                    effect_reasoning.append("Counter ability (no targets currently)")
            
            elif 'destroy target creature' in ability_text or 'exile target creature' in ability_text:
                # Check for targets
                opp_creatures = [cid for cid in opp["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            'creature' in gs._safe_get_card(cid).card_types]
                
                if opp_creatures:
                    # Find highest power creature to remove
                    best_target = max(opp_creatures, 
                                    key=lambda cid: gs._safe_get_card(cid).power if hasattr(gs._safe_get_card(cid), 'power') else 0)
                    
                    target_power = gs._safe_get_card(best_target).power if hasattr(gs._safe_get_card(best_target), 'power') else 1
                    
                    if target_power >= 4:
                        effect_value += 0.8  # High value target
                        effect_reasoning.append("Can remove very powerful creature")
                    elif target_power >= 2:
                        effect_value += 0.6  # Medium value target
                        effect_reasoning.append("Can remove significant creature")
                    else:
                        effect_value += 0.4  # Low value target
                        effect_reasoning.append("Can remove creature")
                else:
                    effect_value += 0.1  # No targets currently
                    effect_reasoning.append("Removal ability (no targets currently)")
            
            elif 'destroy target artifact' in ability_text or 'destroy target enchantment' in ability_text:
                # Check for targets
                target_type = 'artifact' if 'artifact' in ability_text else 'enchantment'
                
                opp_targets = [cid for cid in opp["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            target_type in gs._safe_get_card(cid).card_types]
                
                if opp_targets:
                    effect_value += 0.5
                    effect_reasoning.append(f"Can remove {target_type}")
                else:
                    effect_value += 0.1  # No targets currently
                    effect_reasoning.append(f"Can remove {target_type} (no targets currently)")
        
        # Scry/deck manipulation
        elif 'scry' in ability_text:
            scry_amount = 1
            match = re.search(r'scry (\d+)', ability_text)
            if match:
                scry_amount = int(match.group(1))
            
            effect_value += 0.1 + 0.1 * scry_amount
            effect_reasoning.append(f"Scry {scry_amount}")
            
            # Higher value when we need specific cards
            if self.strategy_type == "combo":
                effect_value += 0.1 * scry_amount
                effect_reasoning.append("Scry more valuable in combo strategy")
        
        # Mana production
        elif ('add' in ability_text and 
            any(f"{{{c}}}" in ability_text for c in ['w', 'u', 'b', 'r', 'g', 'c'])):
            # Count mana produced
            total_mana = 0
            colored_mana = 0
            
            for color in ['w', 'u', 'b', 'r', 'g', 'c']:
                pattern = f"{{{{\\s*{color}\\s*}}}}"
                matches = re.findall(pattern, ability_text, re.IGNORECASE)
                if matches:
                    total_mana += len(matches)
                    if color != 'c':
                        colored_mana += len(matches)
            
            # If X is specified (like "add X mana")
            if 'add x' in ability_text.lower():
                # Estimate X conservatively
                total_mana = 2  # Assume X=2 as default
            
            # Value depends on current available mana and game stage
            if total_mana > 0:
                if game_stage == "early":
                    effect_value += 0.3 + total_mana * 0.1  # Ramp valuable early
                    effect_reasoning.append(f"Producing {total_mana} mana in early game")
                else:
                    effect_value += 0.2 + total_mana * 0.05  # Less valuable later
                    effect_reasoning.append(f"Producing {total_mana} mana")
        
        # Untap effects
        elif 'untap' in ability_text:
            # Check what's being untapped
            untap_target = None
            
            if 'untap target creature' in ability_text:
                untap_target = 'creature'
            elif 'untap target land' in ability_text:
                untap_target = 'land'
            elif 'untap target permanent' in ability_text:
                untap_target = 'permanent'
            
            # Find tapped permanents of the relevant type
            tapped_permanents = []
            if untap_target:
                for cid in me["battlefield"]:
                    if cid in me.get("tapped_permanents", set()):
                        card = gs._safe_get_card(cid)
                        if card and hasattr(card, 'card_types'):
                            if (untap_target == 'permanent' or 
                                untap_target in card.card_types):
                                tapped_permanents.append(cid)
            
            if tapped_permanents:
                if untap_target == 'creature':
                    # Value depends on creature quality
                    best_creature = max(tapped_permanents, 
                                    key=lambda cid: gs._safe_get_card(cid).power if hasattr(gs._safe_get_card(cid), 'power') else 0)
                    
                    creature_power = gs._safe_get_card(best_creature).power if hasattr(gs._safe_get_card(best_creature), 'power') else 1
                    
                    if creature_power >= 3:
                        effect_value += 0.6  # High value creature
                        effect_reasoning.append("Can untap powerful creature")
                    else:
                        effect_value += 0.3  # Lower value creature
                        effect_reasoning.append("Can untap creature")
                
                elif untap_target == 'land':
                    effect_value += 0.4
                    effect_reasoning.append("Can untap land for mana")
                
                else:  # Generic permanent
                    effect_value += 0.4
                    effect_reasoning.append("Can untap permanent")
            else:
                effect_value += 0.1  # No targets currently
                effect_reasoning.append(f"Untap ability (no valid targets currently)")
        
        # If no specific effect recognized, provide generic value
        if effect_value == 0.0:
            effect_value = 0.3  # Default value
            effect_reasoning.append("Generic ability effect")
        
        # Calculate total effect value (positive number representing benefit)
        total_effect_value = min(1.0, effect_value)  # Cap at 1.0
        effect_summary = ", ".join(effect_reasoning)
        
        # 3. Contextual Adjustments
        context_value = 0.0
        context_reasoning = []
        
        # Game stage adjustments
        if game_stage == "early":
            # In early game, value abilities that develop board
            if ('create' in ability_text and 'token' in ability_text) or ('add' in ability_text and 'mana' in ability_text):
                context_value += 0.2
                context_reasoning.append("Development valuable in early game")
        
        elif game_stage == "mid":
            # In mid game, value card advantage and tempo plays
            if 'draw' in ability_text or 'destroy' in ability_text or 'damage' in ability_text:
                context_value += 0.1
                context_reasoning.append("Card advantage/removal valuable in mid game")
        
        else:  # Late game
            # In late game, value game-ending abilities
            if opp_life <= 10 and 'damage' in ability_text:
                context_value += 0.2
                context_reasoning.append("Damage more valuable in late game with opponent at low life")
            
            if 'destroy' in ability_text or 'exile' in ability_text:
                context_value += 0.1
                context_reasoning.append("Removal valuable for dealing with late game threats")
        
        # Board position adjustments
        if board_position in ["behind", "struggling"]:
            # When behind, defensive abilities are more valuable
            if ('gain life' in ability_text or 
                ('create' in ability_text and 'token' in ability_text) or
                ('destroy' in ability_text or 'exile' in ability_text)):
                context_value += 0.2
                context_reasoning.append("Defensive ability valuable when behind")
        
        elif board_position in ["ahead", "dominating"]:
            # When ahead, offensive abilities are more valuable
            if 'damage' in ability_text or '+' in ability_text:
                context_value += 0.2
                context_reasoning.append("Offensive ability valuable when ahead")
        
        # Phase-specific adjustments
        if phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, gs.PHASE_COMBAT_DAMAGE]:
            # Combat abilities more valuable during combat
            if ('+' in ability_text or 'destroy' in ability_text or 
                'damage' in ability_text or 'gain' in ability_text and 'life' in ability_text):
                context_value += 0.2
                context_reasoning.append("Combat ability valuable during combat phase")
        
        # Strategy-specific adjustments
        if self.strategy_type == "aggro":
            # Aggro values damage and creature enhancement
            if 'damage' in ability_text or ('+' in ability_text and '/' in ability_text):
                context_value += 0.2
                context_reasoning.append("Offensive ability aligns with aggressive strategy")
        
        elif self.strategy_type == "control":
            # Control values card draw and removal
            if 'draw' in ability_text or 'counter' in ability_text or 'destroy' in ability_text:
                context_value += 0.2
                context_reasoning.append("Control ability aligns with control strategy")
        
        elif self.strategy_type == "combo":
            # Combo values card selection, mana generation, and untapping
            if 'draw' in ability_text or 'scry' in ability_text or 'add' in ability_text or 'untap' in ability_text:
                context_value += 0.2
                context_reasoning.append("Setup ability aligns with combo strategy")
        
        # Risk tolerance consideration
        if total_cost_value < -0.5:  # High cost ability
            context_value += (self.risk_tolerance - 0.5) * 0.2  # -0.1 to +0.1 based on risk tolerance
            if self.risk_tolerance > 0.6:
                context_reasoning.append("Higher risk tolerance favors costly ability")
            elif self.risk_tolerance < 0.4:
                context_reasoning.append("Lower risk tolerance disfavors costly ability")
        
        # Adjust for aggression level
        if 'damage' in ability_text or '+' in ability_text:
            context_value += (self.aggression_level - 0.5) * 0.2  # -0.1 to +0.1 based on aggression
            if self.aggression_level > 0.6:
                context_reasoning.append("Higher aggression favors offensive ability")
            elif self.aggression_level < 0.4:
                context_reasoning.append("Lower aggression disfavors offensive ability")
        
        # Cap context adjustments
        total_context_value = max(-0.3, min(0.3, context_value))
        context_summary = ", ".join(context_reasoning)
        
        # 4. Final Evaluation
        # Combine components with sensible weights
        final_value = total_cost_value + total_effect_value + total_context_value
        
        # Scale to 0-1 range and ensure non-negative
        final_value = max(0.0, min(1.0, final_value + 0.5))  # Shift from -0.5-0.5 to 0-1
        
        # Generate final reasoning text
        final_reasoning = ""
        if cost_summary:
            final_reasoning += f"Costs: {cost_summary}. "
        if effect_summary:
            final_reasoning += f"Effects: {effect_summary}. "
        if context_summary:
            final_reasoning += f"Context: {context_summary}."
        
        if not final_reasoning:
            final_reasoning = "Neutral value ability."
        
        logging.debug(f"Ability evaluation: {card_name} ability {ability_idx}: {final_value:.2f} - {final_reasoning}")
        
        return final_value, final_reasoning

    def _get_card_value(self, card_id):
        """
        Estimate the strategic value of a card.
        
        Args:
            card_id: ID of the card to evaluate
            
        Returns:
            float: Strategic value of the card
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        value = 0.0
        
        # Base value: mana cost (cards with higher cost are generally more powerful)
        if hasattr(card, 'cmc'):
            value += min(10, card.cmc) * 0.6  # Cap at 10 to avoid overvaluing expensive cards
        
        # Card type modifiers
        if hasattr(card, 'card_types'):
            if 'creature' in card.card_types:
                # Creature value based on power and toughness
                if hasattr(card, 'power') and hasattr(card, 'toughness'):
                    value += (card.power + card.toughness) * 0.3
                    
                # Keyword abilities add value
                if hasattr(card, 'oracle_text'):
                    oracle_text = card.oracle_text.lower()
                    keywords = ['flying', 'first strike', 'double strike', 'deathtouch', 
                            'lifelink', 'menace', 'trample', 'vigilance', 'reach']
                    for keyword in keywords:
                        if keyword in oracle_text:
                            value += 0.5
            
            if 'planeswalker' in card.card_types:
                # Planeswalkers are very valuable
                value += 5.0
                
                # Loyalty adds value
                if hasattr(card, 'loyalty'):
                    value += card.loyalty * 0.5
            
            if 'instant' in card.card_types or 'sorcery' in card.card_types:
                # Value based on effects (simple heuristic)
                if hasattr(card, 'oracle_text'):
                    oracle_text = card.oracle_text.lower()
                    
                    # Card draw
                    if 'draw' in oracle_text:
                        value += 2.0
                    
                    # Removal
                    if any(term in oracle_text for term in ['destroy', 'exile', 'damage to']):
                        value += 3.0
        
        # Card-specific performance data if available
        if hasattr(card, 'performance_rating'):
            value *= (0.5 + card.performance_rating)
        
        return value

