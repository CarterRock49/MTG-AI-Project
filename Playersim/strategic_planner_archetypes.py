"""Deck archetype detection, opponent modeling, and win-condition analysis.

Extracted from strategic_planner.py. This module defines behavior only (a mixin);
all state lives on MTGStrategicPlanner, which composes every mixin.
"""

from collections.abc import Mapping
import logging
import math

import numpy as np


def _card_number(card, attribute, default=0.0):
    """Return a finite numeric characteristic for strategic estimates."""
    try:
        value = float(getattr(card, attribute, default) or 0)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if math.isfinite(value) else default


class ArchetypeAnalysisMixin:
    """Deck archetype detection, opponent modeling, and win-condition analysis."""

    __slots__ = ()

    def update_archetype_detection(self, force=False):
        """
        Periodically update the deck archetype detection as more cards are seen.
        
        Args:
            force: Force update regardless of turn
        
        Returns:
            bool: Whether the archetype was updated
        """
        gs = self.game_state
        
        # Only update every few turns or when forced
        if not force and gs.turn % 3 != 0:
            return False
        
        previous_archetype = getattr(self, 'strategy_type', None)
        previous_opponent_archetype = getattr(self, 'opponent_archetype', None)
        
        # Re-detect archetypes
        self._detect_deck_archetype()
        
        # Check if archetypes changed
        archetype_changed = (previous_archetype != self.strategy_type or 
                            previous_opponent_archetype != self.opponent_archetype)
        
        if archetype_changed:
            logging.info(f"Updated archetypes - Own: {previous_archetype} -> {self.strategy_type}, "
                        f"Opponent: {previous_opponent_archetype} -> {self.opponent_archetype}")
            
        return archetype_changed

    def identify_win_conditions(self):
        """
        Comprehensively identify and evaluate potential win conditions for the current game.
        
        This advanced analysis identifies multiple viable paths to victory based on
        current board state, hand, and deck archetype.
        
        Returns:
            dict: Win condition analysis with viability scores and projected turns to win
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        win_conditions = {
            "combat_damage": {
                "viable": False,
                "turns_to_win": float('inf'),
                "score": 0.0,
                "description": "Win through combat damage",
                "key_cards": []
            },
            "direct_damage": {
                "viable": False,
                "turns_to_win": float('inf'),
                "score": 0.0,
                "description": "Win through direct damage spells",
                "key_cards": []
            },
            "card_advantage": {
                "viable": False,
                "turns_to_win": float('inf'),
                "score": 0.0, 
                "description": "Win through overwhelming card advantage",
                "key_cards": []
            },
            "combo": {
                "viable": False,
                "turns_to_win": float('inf'),
                "score": 0.0,
                "description": "Win through executing a combo",
                "key_cards": []
            },
            "control": {
                "viable": False,
                "turns_to_win": float('inf'),
                "score": 0.0,
                "description": "Win by controlling the board until victory",
                "key_cards": []
            },
            "alternate": {
                "viable": False,
                "turns_to_win": float('inf'),
                "score": 0.0,
                "description": "Win through an alternate win condition",
                "key_cards": []
            }
        }
        
        # Check combat damage win condition
        my_creatures = [cid for cid in me["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types]
        
        opp_creatures = [cid for cid in opp["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types]
        
        if my_creatures:
            # Calculate potential combat damage considering evasion, buffs, etc.
            my_power = 0
            evasive_power = 0
            key_creatures = []
            
            for cid in my_creatures:
                card = gs._safe_get_card(cid)
                if not card or not hasattr(card, 'power'):
                    continue
                    
                # Base power
                creature_power = _card_number(card, 'power')
                
                # Check for evasion (flying, unblockable, etc.)
                has_evasion = False
                if hasattr(card, 'oracle_text'):
                    oracle_text = card.oracle_text.lower()
                    if any(keyword in oracle_text for keyword in ['flying', 'can\'t be blocked', 'shadow', 'horsemanship']):
                        has_evasion = True
                        # Check if opponent has blockers that can block this
                        if 'flying' in oracle_text:
                            has_evasion = not any(
                                'flying' in gs._safe_get_card(opp_cid).oracle_text.lower() or 
                                'reach' in gs._safe_get_card(opp_cid).oracle_text.lower()
                                for opp_cid in opp_creatures 
                                if gs._safe_get_card(opp_cid) and hasattr(gs._safe_get_card(opp_cid), 'oracle_text')
                            )
                
                my_power += creature_power
                if has_evasion:
                    evasive_power += creature_power
                    
                # Add to key creatures if significant
                if creature_power >= 3 or has_evasion:
                    key_creatures.append(cid)
            
            # Estimate effective combat damage considering blockers
            effective_power = evasive_power
            if len(opp_creatures) < len(my_creatures):
                # Add non-evasive creatures that can get through
                effective_power += max(0, my_power - evasive_power - 
                                    sum(_card_number(
                                        gs._safe_get_card(cid), 'toughness')
                                        for cid in opp_creatures
                                        if gs._safe_get_card(cid)))
            
            # Calculate turns to win through combat
            if effective_power > 0:
                turns_to_win = max(1.0, float(np.ceil(
                    max(0, opp["life"]) / effective_power)))
                
                # Combat damage is viable if we can win in a reasonable timeframe
                combat_viable = turns_to_win < 10
                win_conditions["combat_damage"]["viable"] = combat_viable
                win_conditions["combat_damage"]["turns_to_win"] = turns_to_win
                win_conditions["combat_damage"]["score"] = (
                    max(0.0, min(1.0, (10.0 - turns_to_win) / 9.0))
                    if combat_viable else 0.0)
                win_conditions["combat_damage"]["key_cards"] = key_creatures
        
        # Direct damage win condition
        direct_damage_sources = []
        total_direct_damage = 0
        
        # Check hand and battlefield for damage sources
        for card_id in me["hand"] + me["battlefield"]:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                continue
                
            oracle_text = card.oracle_text.lower()
            
            # Look for damage effects
            if 'damage to' in oracle_text and ('player' in oracle_text or 'opponent' in oracle_text or 'any target' in oracle_text):
                # Extract damage amount
                import re
                damage_match = re.search(r'(\d+) damage', oracle_text)
                if damage_match:
                    damage = int(damage_match.group(1))
                    direct_damage_sources.append((card_id, damage))
                    total_direct_damage += damage
        
        if direct_damage_sources:
            # If we have enough damage to kill opponent, or close
            if total_direct_damage >= opp["life"] * 0.7:
                win_conditions["direct_damage"]["viable"] = True
                turns_to_win = max(1.0, float(np.ceil(
                    max(0, opp["life"]) / total_direct_damage) * 2))  # Conservative estimate
                win_conditions["direct_damage"]["turns_to_win"] = turns_to_win
                win_conditions["direct_damage"]["score"] = max(
                    0.0, min(1.0, (10.0 - turns_to_win) / 9.0))
                win_conditions["direct_damage"]["key_cards"] = [card_id for card_id, _ in direct_damage_sources]
        
        # Card advantage win condition
        if len(me["hand"]) >= len(opp["hand"]) + 3:
            win_conditions["card_advantage"]["viable"] = True
            win_conditions["card_advantage"]["turns_to_win"] = 15  # Generic estimate
            win_conditions["card_advantage"]["score"] = 0.6
            
            # Find key card draw/advantage sources
            for card_id in me["hand"] + me["battlefield"]:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'oracle_text'):
                    continue
                    
                oracle_text = card.oracle_text.lower()
                if 'draw' in oracle_text and 'card' in oracle_text:
                    win_conditions["card_advantage"]["key_cards"].append(card_id)
        
        # Combo win condition
        combo_pieces = self._identify_combo_pieces(me["hand"] + me["battlefield"])
        if combo_pieces:
            win_conditions["combo"]["viable"] = True
            pieces_needed = combo_pieces.get("needed", 3)
            pieces_have = combo_pieces.get("have", 0)
            win_conditions["combo"]["turns_to_win"] = max(1, pieces_needed - pieces_have) * 2
            win_conditions["combo"]["score"] = min(
                1.0, pieces_have / max(1, pieces_needed))
            win_conditions["combo"]["key_cards"] = combo_pieces.get("cards", [])
            win_conditions["combo"]["description"] = f"Win with {combo_pieces.get('combo', 'unknown')} combo"
        
        # Control win condition
        if len(opp_creatures) == 0 and len(my_creatures) > 0:
            win_conditions["control"]["viable"] = True
            win_conditions["control"]["turns_to_win"] = 15  # Generic estimate
            win_conditions["control"]["score"] = 0.5
            
            # Look for removal spells and countermagic
            for card_id in me["hand"] + me["battlefield"]:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'oracle_text'):
                    continue
                    
                oracle_text = card.oracle_text.lower()
                if any(keyword in oracle_text for keyword in ['destroy', 'exile', 'counter', 'return']):
                    win_conditions["control"]["key_cards"].append(card_id)
        
        # Check for alternate win conditions
        for card_id in me["hand"] + me["battlefield"]:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                continue
                
            oracle_text = card.oracle_text.lower()
            
            # Check for explicit win conditions
            if 'you win the game' in oracle_text:
                win_conditions["alternate"]["viable"] = True
                win_conditions["alternate"]["turns_to_win"] = 5  # Generic estimate
                win_conditions["alternate"]["score"] = 0.7
                win_conditions["alternate"]["key_cards"].append(card_id)
                
                # Try to determine what type of alt win condition
                if 'life' in oracle_text and 'total' in oracle_text:
                    win_conditions["alternate"]["description"] = "Win through high life total"
                elif 'poison' in oracle_text or 'counter' in oracle_text:
                    win_conditions["alternate"]["description"] = "Win through poison counters"
                elif 'card' in oracle_text and 'draw' in oracle_text:
                    win_conditions["alternate"]["description"] = "Win through decking opponent"
        
        # Return all viable win conditions
        return win_conditions

    def predict_opponent_archetype(self):
        """
        Enhanced prediction of the opponent's deck archetype based on observed cards and game patterns.
        Uses pattern recognition and comprehensive analysis of card choices with improved accuracy.
        
        Returns:
            numpy.ndarray: Probabilities for [aggro, control, midrange, combo, tempo, ramp]
        """
        gs = self.game_state
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Initialize archetype scores with prior probabilities
        scores = {
            "aggro": 0.20,
            "control": 0.20,
            "midrange": 0.25,
            "combo": 0.15,
            "tempo": 0.10,
            "ramp": 0.10
        }
        
        # Count observed card types and game patterns
        observed_cards = []
        for zone in ["battlefield", "graveyard", "exile"]:
            for card_id in opp.get(zone, []):
                if (zone == "exile"
                        and hasattr(gs, "is_face_down_exile_card")
                        and gs.is_face_down_exile_card(card_id, opp)):
                    continue
                card = gs._safe_get_card(card_id)
                # An opponent's face-down permanent is a public object, but
                # its printed identity, types, stats, and rules text are not
                # public inputs to archetype inference.
                if (zone == "battlefield"
                        and bool(getattr(card, "face_down", False))):
                    continue
                observed_cards.append(card)
        
        # Filter out None values
        observed_cards = [card for card in observed_cards if card]
        
        # If we haven't observed any cards, return prior probabilities
        if not observed_cards:
            return np.array([0.20, 0.20, 0.25, 0.15, 0.10, 0.10], dtype=np.float32)
        
        # Count card types and mana values
        creature_count = 0
        instant_count = 0
        sorcery_count = 0
        enchantment_count = 0
        artifact_count = 0
        planeswalker_count = 0
        land_count = 0
        
        low_cmc_count = 0  # CMC <= 2
        mid_cmc_count = 0  # 3 <= CMC <= 4
        high_cmc_count = 0  # CMC >= 5
        
        # Enhanced card counting with detailed attributes
        creature_power_sum = 0
        creature_toughness_sum = 0
        creature_count_with_stats = 0
        
        # Keyword tracking (much more comprehensive)
        keyword_counts = {
            # Aggro keywords
            "aggro": {
                "haste": 0,
                "menace": 0,
                "first strike": 0, 
                "double strike": 0,
                "trample": 0,
                "prowess": 0,
                "riot": 0,
                "bloodthirst": 0
            },
            # Control keywords
            "control": {
                "counter": 0,
                "destroy": 0,
                "exile": 0,
                "return": 0,
                "draw": 0,
                "wrath": 0,
                "scry": 0,
                "discard": 0
            },
            # Midrange keywords
            "midrange": {
                "vigilance": 0,
                "deathtouch": 0,
                "lifelink": 0,
                "scry": 0,
                "etb": 0,
                "value": 0,
                "fight": 0,
                "modal": 0
            },
            # Combo keywords
            "combo": {
                "when": 0,
                "sacrifice": 0,
                "whenever": 0,
                "search": 0,
                "tutor": 0,
                "infinite": 0,
                "copy": 0,
                "untap": 0
            },
            # Tempo keywords
            "tempo": {
                "flash": 0,
                "flying": 0,
                "bounce": 0,
                "tap": 0,
                "doesn't untap": 0,
                "phase": 0,
                "sleep": 0,
                "draw-discard": 0
            },
            # Ramp keywords
            "ramp": {
                "add": 0,
                "mana": 0,
                "search library": 0,
                "land": 0,
                "forest": 0,
                "mana rock": 0,
                "dork": 0,
                "untap land": 0
            }
        }
        
        # Card name analysis for recognizing specific archetypes
        name_indicators = {
            "aggro": ["goblin", "slith", "knight", "warrior", "berserker", "aggr", "raid", "burn"],
            "control": ["control", "counter", "cancel", "negate", "deny", "wrath", "verdict", "supreme", "doom"],
            "midrange": ["value", "midrange", "modal", "charm", "command", "siege", "titan"],
            "combo": ["combo", "infinite", "twin", "storm", "ritual", "chain", "untap"],
            "tempo": ["tempo", "delver", "faerie", "sprite", "bounce", "unsummon", "remand"],
            "ramp": ["ramp", "cultivate", "growth", "fertile", "explosive", "bloom", "dork", "rock"]
        }
        
        # Game pattern observations - enhanced with more patterns
        patterns = {
            "played_creature_t1_t2": 0,  # Played creature turns 1-2
            "played_ramp_t1_t3": 0,      # Played ramp turns 1-3
            "played_removal_early": 0,   # Played removal early
            "played_draw_spells": 0,     # Played card draw
            "played_counterspells": 0,   # Played counterspells
            "played_discard": 0,         # Played discard effects
            "aggressive_attacks": 0,     # Made aggressive attacks
            "conservative_blocks": 0,    # Made conservative blocks
            "tutored_for_combo": 0,      # Searched for specific cards (combo indicator)
            "used_mana_abilities": 0,    # Used mana abilities (ramp indicator)
            "played_high_cmc_early": 0   # Played high CMC cards early (ramp indicator)
        }
        
        # Initialize creature type tracking for tribal indicators
        creature_types = {}
        
        # Analyze observed cards - with enhanced analysis
        for card in observed_cards:
            # Basic type counting
            if hasattr(card, 'card_types'):
                if 'creature' in card.card_types:
                    creature_count += 1
                    
                    # Track creature power/toughness for archetype analysis
                    if hasattr(card, 'power') and hasattr(card, 'toughness'):
                        creature_power_sum += _card_number(card, 'power')
                        creature_toughness_sum += _card_number(
                            card, 'toughness')
                        creature_count_with_stats += 1
                        
                    # Track creature types for tribal detection
                    if hasattr(card, 'subtypes'):
                        for subtype in card.subtypes:
                            if subtype not in creature_types:
                                creature_types[subtype] = 0
                            creature_types[subtype] += 1
                    
                if 'instant' in card.card_types:
                    instant_count += 1
                if 'sorcery' in card.card_types:
                    sorcery_count += 1
                if 'enchantment' in card.card_types:
                    enchantment_count += 1
                if 'artifact' in card.card_types:
                    artifact_count += 1
                if 'planeswalker' in card.card_types:
                    planeswalker_count += 1
                if 'land' in card.card_types:
                    land_count += 1
            
            # Mana curve analysis
            if hasattr(card, 'cmc'):
                cmc = _card_number(card, 'cmc')
                if cmc <= 2:
                    low_cmc_count += 1
                elif 3 <= cmc <= 4:
                    mid_cmc_count += 1
                else:
                    high_cmc_count += 1
            
            # Keyword analysis
            if hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                
                # Check all keywords
                for archetype, keywords in keyword_counts.items():
                    for keyword in keywords:
                        if keyword in oracle_text:
                            keyword_counts[archetype][keyword] += 1
                
                # Card name analysis
                if hasattr(card, 'name'):
                    card_name = card.name.lower()
                    for archetype, indicators in name_indicators.items():
                        if any(indicator in card_name for indicator in indicators):
                            scores[archetype] += 0.15  # Boost for archetype-indicating card names
            
            # Enhanced effect analysis
            if hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                
                # Count specific effects that strongly indicate archetypes
                # Aggro indicators
                if "haste" in oracle_text or "gets +X/+0" in oracle_text:
                    patterns["aggressive_attacks"] += 1
                    
                # Control indicators
                if "counter target spell" in oracle_text:
                    patterns["played_counterspells"] += 1
                    
                if "discard" in oracle_text and "card" in oracle_text:
                    patterns["played_discard"] += 1
                    
                if "destroy target" in oracle_text or "exile target" in oracle_text:
                    patterns["played_removal_early"] += 1
                    
                # Draw indicators
                if "draw" in oracle_text and "card" in oracle_text:
                    patterns["played_draw_spells"] += 1
                    
                # Combo indicators
                if "search your library" in oracle_text and "card" in oracle_text:
                    patterns["tutored_for_combo"] += 1
                    
                # Ramp indicators
                if "add" in oracle_text and any(f"{{{c}}}" in oracle_text for c in ['g', 'r', 'u', 'b', 'w', 'c']):
                    patterns["used_mana_abilities"] += 1
                    
                if "search your library" in oracle_text and "land" in oracle_text:
                    patterns["played_ramp_t1_t3"] += 1
        
        # Calculate average creature stats if available
        avg_power = creature_power_sum / max(1, creature_count_with_stats)
        avg_toughness = creature_toughness_sum / max(1, creature_count_with_stats)
        
        # Detect tribal synergies (significant concentration of one creature type)
        tribal_threshold = 3
        tribal_archetype = None
        for creature_type, count in creature_types.items():
            if count >= tribal_threshold:
                tribal_archetype = creature_type
                # Increase midrange score for tribal - most tribal decks are midrange-y
                scores["midrange"] += 0.3
                break
        
        # Calculate more advanced metrics
        total_cards = len(observed_cards)
        nonland_cards = total_cards - land_count
        
        # Calculate spell-to-creature ratio (high for control, low for aggro)
        spell_to_creature_ratio = (instant_count + sorcery_count) / max(1, creature_count)
        
        # Calculate low-to-high cost ratio (high for aggro, low for control/ramp)
        low_to_high_ratio = low_cmc_count / max(1, high_cmc_count)
        
        # Analyze play patterns more thoroughly
        aggro_patterns = patterns["aggressive_attacks"] + patterns["played_creature_t1_t2"]
        control_patterns = patterns["played_counterspells"] + patterns["played_removal_early"] + patterns["played_discard"]
        combo_patterns = patterns["tutored_for_combo"]
        ramp_patterns = patterns["used_mana_abilities"] + patterns["played_ramp_t1_t3"] + patterns["played_high_cmc_early"]
        
        # Update archetype scores based on comprehensive card analysis
        # Aggro indicators
        if creature_count / max(1, nonland_cards) >= 0.6 and low_cmc_count / max(1, total_cards) >= 0.5:
            scores["aggro"] += 0.4
            scores["control"] -= 0.2
            
            # Check average power - aggro tends to have higher power
            if avg_power >= 2.5:
                scores["aggro"] += 0.2
                
        # Control indicators
        if (instant_count + sorcery_count) / max(1, nonland_cards) >= 0.4:
            scores["control"] += 0.3
            
            # Control plays more instants than sorceries typically
            if instant_count > sorcery_count:
                scores["control"] += 0.2
                scores["tempo"] += 0.1
                
        # Midrange indicators
        if 0.4 <= creature_count / max(1, nonland_cards) <= 0.6 and 0.4 <= mid_cmc_count / max(1, total_cards) <= 0.6:
            scores["midrange"] += 0.3
            
            # Midrange tends to have balanced power/toughness
            if 1.8 <= avg_power <= 3.5 and 2.0 <= avg_toughness <= 4.0:
                scores["midrange"] += 0.2
                
        # Combo indicators
        if artifact_count + enchantment_count >= 3 and patterns["tutored_for_combo"] >= 1:
            scores["combo"] += 0.4
            
        # Ramp indicators
        if patterns["played_ramp_t1_t3"] >= 2 or patterns["used_mana_abilities"] >= 2:
            scores["ramp"] += 0.4
            
            # Ramp into high CMC payoffs
            if high_cmc_count >= 3:
                scores["ramp"] += 0.2
                
        # Tempo indicators
        if 0.4 <= creature_count / max(1, nonland_cards) <= 0.7 and patterns["played_counterspells"] >= 1:
            scores["tempo"] += 0.3
            
            # Tempo decks often have evasive creatures (flying)
            if keyword_counts["tempo"]["flying"] >= 2:
                scores["tempo"] += 0.2
        
        # Keyword-based scoring refinement with improvements
        for archetype, keywords in keyword_counts.items():
            # Calculate weighted keyword score (some keywords are stronger indicators)
            weighted_score = 0
            total_weight = 0
            
            for keyword, count in keywords.items():
                # Assign weights to different keywords based on their significance
                weight = 1.0
                if keyword in ["haste", "counter", "whenever", "flash", "add", "search library"]:
                    weight = 1.5  # Strong indicators
                    
                weighted_score += count * weight
                total_weight += weight
                
            # Normalize score
            if total_weight > 0:
                keyword_score = weighted_score / total_weight
                scores[archetype] += 0.25 * keyword_score
        
        # Pattern-based scoring with enhanced weighting
        if aggro_patterns >= 3:
            scores["aggro"] += 0.3
            
        if control_patterns >= 3:
            scores["control"] += 0.3
            
        if combo_patterns >= 2:
            scores["combo"] += 0.4
            
        if ramp_patterns >= 3:
            scores["ramp"] += 0.3
        
        # Consider card ratios more explicitly
        if spell_to_creature_ratio >= 2.0:
            scores["control"] += 0.3
            scores["combo"] += 0.1
            scores["aggro"] -= 0.2
            
        if spell_to_creature_ratio <= 0.5:
            scores["aggro"] += 0.3
            scores["midrange"] += 0.1
            scores["control"] -= 0.2
            
        if low_to_high_ratio >= 4.0:
            scores["aggro"] += 0.3
            scores["tempo"] += 0.2
            scores["control"] -= 0.1
            
        if low_to_high_ratio <= 1.0:
            scores["control"] += 0.2
            scores["ramp"] += 0.3
            scores["aggro"] -= 0.3
        
        archetypes = ["aggro", "control", "midrange", "combo", "tempo", "ramp"]
        # Penalties above are allowed to push a raw score below zero, but a
        # probability vector cannot contain negative entries (which also made
        # other entries exceed one after division by the signed total).
        score_vector = np.array(
            [max(0.0, float(scores[arch])) for arch in archetypes],
            dtype=np.float64)
        total = float(score_vector.sum())
        if total <= 0.0:
            score_vector = np.array(
                [0.20, 0.20, 0.25, 0.15, 0.10, 0.10], dtype=np.float64)
            total = 1.0
        probs = (score_vector / total).astype(np.float32)
        
        # Store opponent archetype for reference
        self.opponent_archetype = archetypes[np.argmax(probs)]
        
        logging.debug(f"Predicted opponent archetype: {self.opponent_archetype} (confidence: {np.max(probs):.2f})")
        
        return probs

    def _assess_win_conditions(self, my_creatures, opp_creatures, my_life, opp_life):
        """Assess possible win conditions based on the current state."""
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        win_conditions = {
            "combat_damage": {
                "viable": len(my_creatures) > 0,
                "turns_to_win": float('inf'),
                "description": "Win through combat damage"
            },
            "card_advantage": {
                "viable": False,
                "turns_to_win": float('inf'),
                "description": "Win through card advantage and attrition"
            },
            "combo": {
                "viable": False,
                "turns_to_win": float('inf'),
                "description": "Win through a combo"
            },
            "control": {
                "viable": False,
                "turns_to_win": float('inf'),
                "description": "Win by controlling the board and eventually winning"
            },
            "mill": {
                "viable": False,
                "turns_to_win": float('inf'),
                "description": "Win by depleting opponent's library"
            },
            "alternate": {
                "viable": False,
                "turns_to_win": float('inf'),
                "description": "Win through alternate win condition"
            }
        }
        all_cards = me["battlefield"] + me["hand"]
        combo_pieces = self._identify_combo_pieces(all_cards)
        if combo_pieces:
            win_conditions["combo"]["viable"] = True
            # Estimate turns based on how many pieces we have
            pieces_needed = combo_pieces.get("needed", 3)
            pieces_have = combo_pieces.get("have", 0)
            win_conditions["combo"]["turns_to_win"] = max(1, pieces_needed - pieces_have) * 2
            
            # Add specific combo information
            win_conditions["combo"]["description"] = f"Win through {combo_pieces.get('combo', 'unknown')} combo"
            win_conditions["combo"]["pieces"] = combo_pieces.get("cards", [])
        
        # Assess combat damage win condition
        if len(my_creatures) > 0:
            total_power = sum(
                _card_number(gs._safe_get_card(cid), 'power')
                for cid in my_creatures
                if gs._safe_get_card(cid))
            
            # Account for potential blocking
            if len(opp_creatures) > 0:
                blocked_power = min(total_power, sum(
                    _card_number(gs._safe_get_card(cid), 'toughness')
                    for cid in opp_creatures if gs._safe_get_card(cid)))
                effective_power = max(0, total_power - blocked_power)
                
                # Adjust for evasion (flying, trample, etc.)
                evasive_power = sum(
                    _card_number(gs._safe_get_card(cid), 'power')
                    for cid in my_creatures 
                    if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'power') and 
                    hasattr(gs._safe_get_card(cid), 'oracle_text') and 
                    any(keyword in gs._safe_get_card(cid).oracle_text.lower() 
                        for keyword in ['flying', 'trample', 'unblockable', 'can\'t be blocked'])
                )
                
                effective_power = max(effective_power, evasive_power)
            else:
                effective_power = total_power
                
            if effective_power > 0:
                turns_to_win = np.ceil(opp_life / effective_power)
                win_conditions["combat_damage"]["turns_to_win"] = turns_to_win
                win_conditions["combat_damage"]["viable"] = turns_to_win < float('inf')
        
        # Assess card advantage win condition
        if len(me["hand"]) > len(opp["hand"]) + 2:
            win_conditions["card_advantage"]["viable"] = True
            # Rough estimate - more cards means faster win
            win_conditions["card_advantage"]["turns_to_win"] = 20 - min(10, len(me["hand"]) - len(opp["hand"]))
        
        # Assess control win condition
        if len(opp_creatures) == 0 and len(my_creatures) > 0:
            win_conditions["control"]["viable"] = True
            win_conditions["control"]["turns_to_win"] = 15  # General estimate
        
        # Assess mill win condition
        mill_effects = 0
        for card_id in me["battlefield"]:
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text'):
                if any(term in card.oracle_text.lower() for term in ['mill', 'puts top card', 'puts the top']):
                    mill_effects += 1
        
        if mill_effects > 0:
            library_size = len(opp["library"])
            if library_size < 20:  # Getting low on cards
                win_conditions["mill"]["viable"] = True
                # Rough estimate of turns to mill out
                win_conditions["mill"]["turns_to_win"] = library_size // max(1, mill_effects)
        
        # Assess alternate win conditions
        for card_id in me["battlefield"] + me["hand"]:
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text'):
                # Check for alternate win condition cards
                if "you win the game" in card.oracle_text.lower():
                    win_conditions["alternate"]["viable"] = True
                    win_conditions["alternate"]["turns_to_win"] = 5  # Rough estimate
                    break
        
        # Check for combo potential
        combo_pieces = self._identify_combo_pieces(me["battlefield"] + me["hand"])
        if combo_pieces:
            win_conditions["combo"]["viable"] = True
            # Estimate turns based on how many pieces we have
            pieces_needed = combo_pieces["needed"]
            pieces_have = combo_pieces["have"]
            win_conditions["combo"]["turns_to_win"] = max(1, pieces_needed - pieces_have) * 2
        
        return win_conditions

    def _identify_combo_pieces(self, card_ids):
        """Identify potential combo pieces in a list of cards."""
        gs = self.game_state
        
        # Define common combo signatures to look for
        common_combos = {
            "infinite_mana": ["untap", "add mana", "activate"],
            "infinite_turns": ["extra turn", "return from graveyard"],
            "infinite_damage": ["damage", "untap", "activate"],
            "token_swarm": ["create token", "tokens you control", "get +1/+1"],
            "life_drain": ["drain", "life", "opponent loses life"]
        }
        
        # Count cards matching each combo signature
        combo_matches = {combo: 0 for combo in common_combos}
        matched_cards = {combo: [] for combo in common_combos}
        
        for card_id in card_ids:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                continue
                
            card_text = card.oracle_text.lower()
            
            for combo, keywords in common_combos.items():
                if all(keyword in card_text for keyword in keywords):
                    combo_matches[combo] += 1
                    matched_cards[combo].append(card_id)
                elif any(keyword in card_text for keyword in keywords):
                    combo_matches[combo] += 0.5
                    if card_id not in matched_cards[combo]:
                        matched_cards[combo].append(card_id)
        
        # Check if we have enough pieces for any combo
        for combo, count in combo_matches.items():
            if count >= 2:  # Arbitrary threshold - most combos need at least 2 pieces
                return {
                    "combo": combo,
                    "have": len(matched_cards[combo]),
                    "needed": 3,  # Typical combo requires ~3 specific cards
                    "cards": matched_cards[combo]
                }
        
        return None

    def _initialize_strategy_params(self, archetype):
        """
        Initialize comprehensive strategy parameters for a given archetype.
        
        Args:
            archetype: The detected deck archetype
        """
        # Make sure we have this archetype in our strategies
        if archetype not in self.strategies:
            archetype = 'midrange'  # Default fallback
            
        strategy_details = self.strategies.get(archetype)
        
        self.strategy_type = archetype
        self.strategy_params = strategy_details
        self.aggression_level = strategy_details.get("aggression_level", 0.5)
        self.risk_tolerance = strategy_details.get("risk_tolerance", 0.5)
        
        logging.debug(f"Initialized strategy parameters for {archetype} archetype")

    def _detect_deck_archetype(self):
        """
        Enhanced deck archetype detection with sophisticated pattern recognition.
        Analyzes card types, keywords, mana curve and synergies to determine the most likely archetype.
        
        Returns:
            str: Detected deck archetype
        """
        gs = self.game_state
        
        # Initialize strategy with safe defaults
        self.strategy_type = "midrange"
        self.aggression_level = 0.5
        self.risk_tolerance = 0.5
        self.strategy_params = self.strategies.get("midrange", {
            "description": "Default midrange strategy",
            "aggression_level": 0.5,
            "risk_tolerance": 0.5,
            "card_weights": {
                "creature": 1.2, "instant": 1.0, "sorcery": 1.0,
                "artifact": 0.8, "enchantment": 0.8, "planeswalker": 1.3, 
                "land": 0.8
            }
        })
        
        # Validate game state initialization
        if not hasattr(gs, 'p1') or not hasattr(gs, 'p2') or not gs.p1 or not gs.p2:
            logging.debug("Cannot detect deck archetype: game state not fully initialized")
            return "midrange"
        
        # Ensure agent_is_p1 attribute exists
        if not hasattr(gs, 'agent_is_p1'):
            logging.debug("agent_is_p1 attribute missing in game state")
            return "midrange"
        
        # Proceed with detection
        try:
            me = gs.p1 if gs.agent_is_p1 else gs.p2
                
            # Player state is a mapping, not an attribute-bearing object.
            required_zones = ("hand", "battlefield", "library", "graveyard")
            if (not isinstance(me, Mapping)
                    or any(zone not in me for zone in required_zones)):
                logging.debug(
                    "Player state missing required zones for deck detection")
                return "midrange"
        except Exception as e:
            logging.warning(f"Error accessing player state: {e}")
            return "midrange"
            
        # Enhanced archetype definitions with detailed markers
        archetypes = {
            "aggro": {
                "description": "Aggressive strategy: Play fast creatures and attack quickly",
                "aggression_level": 0.8,
                "risk_tolerance": 0.7,
                "card_weights": {"creature": 1.5, "instant": 0.7, "sorcery": 0.7, "artifact": 0.5, "enchantment": 0.5, "planeswalker": 1.2, "land": 0.7},
                "mana_curve_ideal": {0: 0, 1: 10, 2: 12, 3: 8, 4: 4, 5: 1, 6: 0, "7+": 0},
                "keywords": ["haste", "menace", "first strike", "double strike", "trample", "prowess", "riot", "bloodthirst"],
                "theme_cards": ["goblin", "slith", "burn", "lightning", "boros", "embercleave", "raid", "aggro", "sligh", "rdw"],
                "signature_mechanics": ["spectacle", "dash", "unleash", "battalion", "exert"]
            },
            "midrange": {
                "description": "Midrange strategy: Efficient creatures and value plays",
                "aggression_level": 0.5,
                "risk_tolerance": 0.5,
                "card_weights": {"creature": 1.2, "instant": 1.0, "sorcery": 1.0, "artifact": 0.8, "enchantment": 0.8, "planeswalker": 1.3, "land": 0.8},
                "mana_curve_ideal": {0: 0, 1: 6, 2: 8, 3: 10, 4: 6, 5: 3, 6: 2, "7+": 0},
                "keywords": ["vigilance", "trample", "deathtouch", "lifelink", "menace", "adapt", "outlast"],
                "theme_cards": ["value", "jund", "abzan", "golgari", "selesnya", "naya", "siege", "rhino", "mid"],
                "signature_mechanics": ["explore", "adapt", "undergrowth", "scavenge", "mentor", "bloodrush"]
            },
            "control": {
                "description": "Control strategy: Counter spells, remove threats, win late game",
                "aggression_level": 0.2,
                "risk_tolerance": 0.3,
                "card_weights": {"creature": 0.7, "instant": 1.5, "sorcery": 1.3, "artifact": 1.0, "enchantment": 1.0, "planeswalker": 1.5, "land": 0.8},
                "mana_curve_ideal": {0: 0, 1: 4, 2: 8, 3: 8, 4: 8, 5: 5, 6: 3, "7+": 3},
                "keywords": ["flash", "flying", "hexproof", "ward", "counterspell", "wrath", "removal", "boardwipe"],
                "theme_cards": ["azorius", "esper", "dimir", "cancel", "counterspell", "wrath", "verdict", "control", "sphinx"],
                "signature_mechanics": ["addendum", "forecast", "overload", "cipher", "detain", "split second"]
            },
            "combo": {
                "description": "Combo strategy: Assemble a game-winning combination",
                "aggression_level": 0.4,
                "risk_tolerance": 0.9,
                "card_weights": {"creature": 0.8, "instant": 1.0, "sorcery": 1.2, "artifact": 1.3, "enchantment": 1.3, "planeswalker": 0.7, "land": 0.9},
                "mana_curve_ideal": {0: 0, 1: 6, 2: 9, 3: 9, 4: 5, 5: 2, 6: 2, "7+": 1},
                "keywords": ["sacrifice", "etb", "dies", "when you cast", "copy", "storm", "infinite"],
                "theme_cards": ["combo", "infinite", "twin", "storm", "ritual", "chain", "untap", "loop", "tutor", "search"],
                "signature_mechanics": ["cascade", "storm", "convoke", "flashback", "rebound", "splice", "transmute"]
            },
            "tempo": {
                "description": "Tempo strategy: Disrupt opponent while applying pressure",
                "aggression_level": 0.6,
                "risk_tolerance": 0.5,
                "card_weights": {"creature": 1.3, "instant": 1.4, "sorcery": 0.9, "artifact": 0.6, "enchantment": 0.6, "planeswalker": 0.5, "land": 0.8},
                "mana_curve_ideal": {0: 0, 1: 8, 2: 12, 3: 8, 4: 4, 5: 2, 6: 0, "7+": 0},
                "keywords": ["flash", "flying", "prowess", "counterspell", "bounce", "return", "tap"],
                "theme_cards": ["tempo", "delver", "faerie", "sprite", "bounce", "unsummon", "remand", "izzet", "grixis"],
                "signature_mechanics": ["prowess", "replicate", "surge", "awaken", "foretell", "snapback"]
            },
            "ramp": {
                "description": "Ramp strategy: Accelerate mana to cast big spells early",
                "aggression_level": 0.4,
                "risk_tolerance": 0.6,
                "card_weights": {"creature": 1.0, "instant": 0.7, "sorcery": 0.9, "artifact": 1.0, "enchantment": 0.9, "planeswalker": 1.0, "land": 1.4},
                "mana_curve_ideal": {0: 0, 1: 5, 2: 6, 3: 7, 4: 6, 5: 5, 6: 4, "7+": 4},
                "keywords": ["search your library", "add mana", "land", "untap", "mana rock", "mana dork"],
                "theme_cards": ["ramp", "cultivate", "growth", "fertile", "explosive", "bloom", "dork", "rock", "simic", "gruul"],
                "signature_mechanics": ["landfall", "rampant growth", "mana production", "green ramp", "lotus", "dryad"]
            },
            "tribal": {
                "description": "Tribal strategy: Leverage creature type synergies",
                "aggression_level": 0.6,
                "risk_tolerance": 0.5,
                "card_weights": {"creature": 1.4, "instant": 0.8, "sorcery": 0.8, "artifact": 0.6, "enchantment": 0.7, "planeswalker": 0.8, "land": 0.8},
                "mana_curve_ideal": {0: 0, 1: 7, 2: 9, 3: 8, 4: 5, 5: 2, 6: 1, "7+": 0},
                "keywords": ["lord", "other", "creatures you control", "get +1/+1", "creature type"],
                "theme_cards": ["tribal", "elf", "goblin", "merfolk", "zombie", "human", "warrior", "wizard", "knight", "dinosaur", "dragon"],
                "signature_mechanics": ["changeling", "champion", "kinship", "lord effects", "creature type matters"]
            }
        }
        
        # Count card types and mana curve
        type_counts = {"creature": 0, "instant": 0, "sorcery": 0, "artifact": 0, "enchantment": 0, "planeswalker": 0, "land": 0}
        mana_curve = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, "7+": 0}
        
        # Track additional features for enhanced detection
        keyword_counts = {}
        for archetype in archetypes.values():
            for keyword in archetype["keywords"]:
                keyword_counts[keyword] = 0
                
        creature_types = {}  # For tribal detection
        theme_word_counts = {}  # For thematic elements
        
        for archetype, data in archetypes.items():
            for theme_word in data["theme_cards"]:
                theme_word_counts[theme_word] = 0
        
        # Track colors distribution
        color_counts = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        multicolor_count = 0
        
        # Combine all cards from relevant zones
        all_cards = []
        for zone in ["hand", "battlefield", "library", "graveyard"]:
            all_cards.extend(me[zone])
        
        # Process each card
        for card_id in all_cards:
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            
            # Count card types
            if hasattr(card, 'card_types'):
                for card_type in card.card_types:
                    if card_type.lower() in type_counts:
                        type_counts[card_type.lower()] += 1
            
            # Build mana curve
            if hasattr(card, 'cmc'):
                cmc = _card_number(card, 'cmc')
                if cmc <= 6:
                    mana_curve[cmc] += 1
                else:
                    mana_curve["7+"] += 1
            
            # Analyze card text for keywords
            if hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                for keyword in keyword_counts:
                    if keyword in oracle_text:
                        keyword_counts[keyword] += 1
                
                # Check for theme words
                for theme_word in theme_word_counts:
                    if theme_word in oracle_text or (hasattr(card, 'name') and theme_word in card.name.lower()):
                        theme_word_counts[theme_word] += 1
            
            # Check creature types for tribal detection
            if hasattr(card, 'subtypes'):
                for subtype in card.subtypes:
                    subtype_lower = subtype.lower()
                    if subtype_lower not in creature_types:
                        creature_types[subtype_lower] = 0
                    creature_types[subtype_lower] += 1
            
            # Analyze colors
            if hasattr(card, 'colors'):
                color_count = sum(card.colors)
                if color_count > 1:
                    multicolor_count += 1
                
                # Add to specific color counts
                for i, color_code in enumerate(['W', 'U', 'B', 'R', 'G']):
                    if i < len(card.colors) and card.colors[i]:
                        color_counts[color_code] += 1
        
        # Calculate total nonland cards for percentage calculations
        total_nonland = sum(type_counts.values()) - type_counts["land"]
        if total_nonland <= 0:
            logging.debug("Not enough cards to analyze")
            return "midrange"  # Default if not enough data
        
        # Detect dominant creature types for tribal archetypes
        dominant_creature_type = None
        tribal_threshold = max(5, total_nonland * 0.15)  # At least 15% of deck or 5 cards
        
        for creature_type, count in creature_types.items():
            if count >= tribal_threshold:
                if dominant_creature_type is None or count > creature_types[dominant_creature_type]:
                    dominant_creature_type = creature_type
        
        # Calculate detailed scores for each archetype with weighted features
        archetype_scores = {}
        
        for archetype_name, archetype_data in archetypes.items():
            score = 0.0
            feature_scores = {}
            
            # 1. Card Type Distribution (25%)
            type_score = 0
            for card_type, weight in archetype_data["card_weights"].items():
                type_ratio = type_counts[card_type] / max(1, sum(type_counts.values()))
                # Calculate ideal ratio based on archetype
                ideal_ratio = 0
                if card_type == "creature":
                    if archetype_name == "aggro":
                        ideal_ratio = 0.6
                    elif archetype_name == "control":
                        ideal_ratio = 0.25
                    elif archetype_name == "combo":
                        ideal_ratio = 0.35
                    else:
                        ideal_ratio = 0.45
                elif card_type == "instant" or card_type == "sorcery":
                    if archetype_name == "control":
                        ideal_ratio = 0.35
                    elif archetype_name == "combo":
                        ideal_ratio = 0.3
                    else:
                        ideal_ratio = 0.2
                else:
                    ideal_ratio = 0.1  # Default for other types
                
                type_similarity = 1.0 - min(1.0, abs(type_ratio - ideal_ratio) * 3)
                type_score += type_similarity * weight
            
            # Normalize type score
            type_score /= sum(archetype_data["card_weights"].values())
            feature_scores["type_distribution"] = type_score * 0.25  # 25% weight
            
            # 2. Mana Curve (20%)
            curve_score = 0
            total_curve_points = sum(archetype_data["mana_curve_ideal"].values())
            
            for cmc, count in mana_curve.items():
                actual_ratio = count / max(1, sum(mana_curve.values()))
                ideal_ratio = archetype_data["mana_curve_ideal"][cmc] / total_curve_points
                
                curve_similarity = 1.0 - min(1.0, abs(actual_ratio - ideal_ratio) * 3)
                curve_score += curve_similarity
            
            curve_score /= len(mana_curve)
            feature_scores["mana_curve"] = curve_score * 0.2  # 20% weight
            
            # 3. Keywords and Mechanics (25%)
            keyword_score = 0
            relevant_keywords = archetype_data["keywords"]
            keyword_matches = 0
            
            for keyword in relevant_keywords:
                if keyword_counts[keyword] > 0:
                    keyword_matches += 1
                    # Higher score for multiple instances of the same keyword
                    keyword_score += min(1.0, keyword_counts[keyword] / 3)
            
            # Normalize by number of keywords
            keyword_score = keyword_score / max(1, len(relevant_keywords))
            feature_scores["keywords"] = keyword_score * 0.25  # 25% weight
            
            # 4. Theme Cards and Signature Mechanics (15%)
            theme_score = 0
            for theme_word in archetype_data["theme_cards"]:
                if theme_word_counts[theme_word] > 0:
                    theme_score += min(1.0, theme_word_counts[theme_word] / 2)
            
            # Normalize theme score
            theme_score = min(1.0, theme_score / max(3, len(archetype_data["theme_cards"]) / 3))
            feature_scores["theme_cards"] = theme_score * 0.15  # 15% weight
            
            # 5. Special Cases - Tribal Synergy (10% bonus)
            tribal_bonus = 0
            if archetype_name == "tribal" and dominant_creature_type:
                tribal_creature_count = creature_types.get(dominant_creature_type, 0)
                tribal_ratio = tribal_creature_count / max(1, type_counts["creature"])
                tribal_bonus = min(0.1, tribal_ratio)
                feature_scores["tribal_bonus"] = tribal_bonus
            
            # 6. Color Distribution Analysis (15%)
            color_score = 0
            # Aggro tends to be mono or dual colored
            if archetype_name == "aggro":
                color_score = 0.15 * (1.0 - min(1.0, multicolor_count / max(1, total_nonland) * 2))
            # Control tends to have more colors
            elif archetype_name == "control":
                color_score = 0.15 * min(1.0, multicolor_count / max(1, total_nonland) * 3)
            # Ramp tends to be green-heavy
            elif archetype_name == "ramp":
                color_score = 0.15 * min(1.0, color_counts["G"] / max(1, total_nonland) * 3)
            # Combo can be any color distribution
            else:
                color_score = 0.075  # Neutral score
            
            feature_scores["color_distribution"] = color_score
            
            # Combine all feature scores
            score = sum(feature_scores.values())
            
            # Apply adjustments for special cases
            # If very few creatures, reduce aggro and tribal scores
            if type_counts["creature"] / max(1, total_nonland) < 0.3 and archetype_name in ["aggro", "tribal"]:
                score *= 0.7
            
            # If very spell-heavy, boost control and combo scores
            if (type_counts["instant"] + type_counts["sorcery"]) / max(1, total_nonland) > 0.5 and archetype_name in ["control", "combo"]:
                score *= 1.3
            
            # Store final score
            archetype_scores[archetype_name] = score
        
        # Choose the highest scoring archetype
        best_archetype = max(archetype_scores.items(), key=lambda x: x[1])[0]
        best_score = archetype_scores[best_archetype]
        
        # Log detailed detection results
        confidence = best_score / max(1.0, sum(archetype_scores.values()) / len(archetype_scores))
        logging.debug(f"Deck archetype detection results:")
        for arch, score in sorted(archetype_scores.items(), key=lambda x: x[1], reverse=True):
            logging.debug(f"  {arch}: {score:.2f}")
        logging.debug(f"Selected archetype: {best_archetype} (confidence: {confidence:.2f})")
        
        # Record card type distribution
        card_type_ratios = {t: c/max(1, sum(type_counts.values())) for t, c in type_counts.items()}
        logging.debug(f"Card type distribution: {card_type_ratios}")
        
        # Record color distribution
        color_ratios = {c: count/max(1, sum(color_counts.values())) for c, count in color_counts.items() if count > 0}
        logging.debug(f"Color distribution: {color_ratios}")
        
        # Keep the detector's specialized profile instead of silently falling
        # back to midrange when tempo, ramp, or tribal wins the score.  The
        # planner owns its own profile mapping, so this does not mutate shared
        # module state.
        detected_profile = archetypes[best_archetype]
        self.strategies[best_archetype] = {
            key: detected_profile[key]
            for key in (
                "description", "aggression_level", "risk_tolerance",
                "card_weights")
        }

        # Initialize strategy parameters based on detected archetype
        self._initialize_strategy_params(best_archetype)
        
        return best_archetype

    def adapt_strategy(self):
        """
        Advanced strategy adaptation based on current game state, opponent archetype, and game progression.
        Dynamically adjusts aggression and risk parameters based on multiple factors with improved responsiveness.
        """
        # Ensure we have a current analysis
        if not hasattr(self, 'current_analysis') or self.current_analysis is None:
            self.analyze_game_state()
            
        # Now check again to make sure the analysis exists and is not None
        if not hasattr(self, 'current_analysis') or self.current_analysis is None:
            # Create a default analysis if still None
            self.current_analysis = {
                "position": {"overall": "even"},
                "game_info": {"game_stage": "mid"},
                "life": {"life_diff": 0}
            }
        
        position = self.current_analysis["position"]["overall"]
        game_stage = self.current_analysis["game_info"]["game_stage"]
        life_diff = self.current_analysis["life"]["life_diff"]
        
        # Get player and opponent references
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Base strategy parameters from deck archetype
        base_aggression = self.strategy_params["aggression_level"]
        base_risk = self.strategy_params["risk_tolerance"]
        
        # Enhanced position modifiers with more granularity
        position_modifiers = {
            "dominating": 0.3,    # When dominating, be more aggressive
            "ahead": 0.15,        # When ahead, be slightly more aggressive
            "even": 0.0,          # When even, maintain baseline
            "behind": -0.15,      # When behind, be slightly more defensive
            "struggling": -0.3    # When struggling, be more defensive
        }
        
        # More detailed stage modifiers based on archetype and game stage
        stage_modifiers = {
            "early": {
                "aggro": 0.25,     # Aggro starts strong
                "control": -0.25,  # Control builds up
                "midrange": 0.0,   # Midrange is balanced
                "combo": -0.2,     # Combo builds up
                "tempo": 0.2,      # Tempo applies early pressure
                "ramp": -0.15      # Ramp develops mana early
            },
            "mid": {
                "aggro": 0.0,      # Aggro normalizes
                "control": 0.0,    # Control stabilizes
                "midrange": 0.2,   # Midrange shines in mid-game
                "combo": 0.1,      # Combo prepares
                "tempo": 0.1,      # Tempo maintains pressure
                "ramp": 0.15       # Ramp starts to leverage mana
            },
            "late": {
                "aggro": -0.3,     # Aggro falls off sharply
                "control": 0.25,   # Control takes over
                "midrange": 0.05,  # Midrange stays balanced
                "combo": 0.35,     # Combo executes win condition
                "tempo": -0.2,     # Tempo falls off
                "ramp": 0.3        # Ramp uses big spells
            }
        }
        
        # Apply modifications
        position_mod = position_modifiers.get(position, 0.0)
        stage_mod = stage_modifiers.get(game_stage, {}).get(self.strategy_type, 0.0)
        
        # Enhanced life total considerations based on archetype
        life_mod = 0.0
        
        if self.strategy_type == "aggro":
            # Aggro cares less about life total, more about tempo
            if life_diff <= -10:
                life_mod = -0.1  # Only slightly more defensive
                self.risk_tolerance = min(1.0, base_risk + 0.1)  # Stay risky
            elif life_diff >= 10:
                life_mod = 0.2  # Press the advantage hard
                self.risk_tolerance = min(1.0, base_risk + 0.1)
        elif self.strategy_type == "control":
            # Control cares a lot about life total
            if life_diff <= -10:
                life_mod = -0.4  # Get very defensive
                self.risk_tolerance = max(0.0, base_risk - 0.3)
            elif life_diff <= -5:
                life_mod = -0.2
                self.risk_tolerance = max(0.0, base_risk - 0.1)
            elif life_diff >= 10:
                life_mod = 0.1  # Slight shift to closing out the game
                self.risk_tolerance = base_risk
        elif self.strategy_type == "midrange":
            # Midrange adapts based on matchup and life total
            if life_diff <= -10:
                life_mod = -0.3
                self.risk_tolerance = max(0.0, base_risk - 0.2)
            elif life_diff <= -5:
                life_mod = -0.1
                self.risk_tolerance = max(0.0, base_risk - 0.1)
            elif life_diff >= 10:
                life_mod = 0.2
                self.risk_tolerance = min(1.0, base_risk + 0.1)
        else:  # Combo or other
            # Combo cares about survival until combo turn
            if life_diff <= -10:
                life_mod = -0.4  # Pure survival mode
                self.risk_tolerance = max(0.0, base_risk - 0.2)
            elif life_diff <= -5:
                life_mod = -0.2
                self.risk_tolerance = base_risk
            elif life_diff >= 5:
                life_mod = 0.0  # Stay on course
                self.risk_tolerance = min(1.0, base_risk + 0.1)  # Slightly more willing to go for it
        
        # Board complexity adjustment - more detailed analysis
        battlefield_size = len(me["battlefield"]) + len(opp["battlefield"])
        
        # Count creatures, planeswalkers and other key permanents
        my_creatures = sum(1 for cid in me["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types)
                        
        opp_creatures = sum(1 for cid in opp["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types)
                        
        my_planeswalkers = sum(1 for cid in me["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            'planeswalker' in gs._safe_get_card(cid).card_types)
                            
        opp_planeswalkers = sum(1 for cid in opp["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            'planeswalker' in gs._safe_get_card(cid).card_types)
        
        complexity_mod = 0.0
        
        # Board complexity adjustments based on game state and strategy type
        if battlefield_size > 15:  # Very complex board
            if self.strategy_type == "control":
                complexity_mod = 0.1  # Control likes complex boards
                self.risk_tolerance = max(0.0, self.risk_tolerance - 0.05)
            else:
                complexity_mod = -0.1  # Other archetypes struggle with very complex boards
                self.risk_tolerance = max(0.0, self.risk_tolerance - 0.1)
        elif battlefield_size > 10:  # Moderately complex
            if self.strategy_type == "midrange":
                complexity_mod = 0.05  # Midrange can navigate moderate complexity well
            elif self.strategy_type == "aggro":
                complexity_mod = -0.1  # Aggro prefers cleaner boards
                self.risk_tolerance = max(0.0, self.risk_tolerance - 0.05)
        elif battlefield_size < 5:  # Simple board
            if self.strategy_type == "aggro":
                complexity_mod = 0.15  # Aggro likes simple boards
                self.risk_tolerance = min(1.0, self.risk_tolerance + 0.1)
            elif self.strategy_type == "control" and game_stage != "early":
                complexity_mod = -0.1  # Control prefers having some board presence mid-late
        
        # Card advantage consideration with more nuanced analysis
        card_advantage = len(me["hand"]) - len(opp["hand"])
        
        card_advantage_mod = 0.0
        
        if card_advantage >= 3:
            # Significant card advantage
            if self.strategy_type == "control":
                card_advantage_mod = 0.2  # Control leverages card advantage well
                self.risk_tolerance = min(1.0, self.risk_tolerance + 0.15)
            else:
                card_advantage_mod = 0.1  # Other archetypes benefit but not as much
                self.risk_tolerance = min(1.0, self.risk_tolerance + 0.1)
        elif card_advantage <= -3:
            # Significant card disadvantage
            if self.strategy_type == "aggro":
                card_advantage_mod = -0.1  # Aggro cares less about card disadvantage
                self.risk_tolerance = min(1.0, self.risk_tolerance + 0.05)  # Take more risks
            else:
                card_advantage_mod = -0.2  # Other archetypes struggle with card disadvantage
                self.risk_tolerance = max(0.0, self.risk_tolerance - 0.15)
        
        # Opponent archetype adaptation
        opponent_archetype = getattr(self, 'opponent_archetype', None)
        opponent_mod = 0.0
        
        if opponent_archetype:
            # Adjust based on matchup dynamics
            matchup_adjustments = {
                # Aggro matchups
                ("aggro", "aggro"): (0.1, 0.1),      # Be slightly more aggressive and risky
                ("aggro", "control"): (0.2, 0.2),    # Be very aggressive against control
                ("aggro", "midrange"): (0.1, 0.05),  # Be slightly more aggressive
                ("aggro", "combo"): (0.3, 0.2),      # Race against combo
                
                # Control matchups
                ("control", "aggro"): (-0.2, -0.1),  # Be defensive against aggro
                ("control", "control"): (0.05, 0.0), # Slightly more proactive in the mirror
                ("control", "midrange"): (0.0, 0.0), # Standard play
                ("control", "combo"): (0.1, 0.1),    # Counter their key pieces
                
                # Midrange matchups
                ("midrange", "aggro"): (-0.1, -0.05), # Slightly defensive
                ("midrange", "control"): (0.2, 0.15), # More aggressive against control
                ("midrange", "midrange"): (0.0, 0.0), # Standard play in mirror
                ("midrange", "combo"): (0.2, 0.1),    # Apply pressure against combo
                
                # Combo matchups
                ("combo", "aggro"): (-0.2, -0.1),     # Defensive against aggro
                ("combo", "control"): (0.0, 0.2),     # Take calculated risks vs control
                ("combo", "midrange"): (0.1, 0.1),    # Slightly faster against midrange
                ("combo", "combo"): (0.3, 0.2),       # Race in the combo mirror
            }
            
            adjustment = matchup_adjustments.get((self.strategy_type, opponent_archetype), (0.0, 0.0))
            opponent_mod = adjustment[0]
            self.risk_tolerance = max(0.0, min(1.0, self.risk_tolerance + adjustment[1]))
        
        # Game progression adjustments (turn-based)
        turn_mod = 0.0
        if gs.turn <= 3:  # Very early game
            if self.strategy_type == "aggro":
                turn_mod = 0.1  # Aggro wants to establish early pressure
            elif self.strategy_type == "combo" and base_aggression < 0.4:
                turn_mod = -0.1  # Slow combo wants to develop resources
        elif gs.turn >= 10:  # Late game
            if self.strategy_type == "aggro" and base_aggression > 0.6:
                turn_mod = -0.2  # Aggro loses steam late
            elif self.strategy_type == "control":
                turn_mod = 0.1  # Control takes over late
        
        # Combine all modifications with appropriate weights
        aggression_modifiers = {
            "position": position_mod * 0.25,
            "stage": stage_mod * 0.25,
            "life": life_mod * 0.15,
            "complexity": complexity_mod * 0.1,
            "card_advantage": card_advantage_mod * 0.1,
            "opponent": opponent_mod * 0.1,
            "turn": turn_mod * 0.05
        }
        
        # Calculate total adjustment with enhanced logging
        total_adjustment = sum(aggression_modifiers.values())
        
        # Apply final adjustments with bounds checking
        self.aggression_level = max(0.1, min(0.9, base_aggression + total_adjustment))
        self.risk_tolerance = max(0.1, min(0.9, self.risk_tolerance))  # Ensure risk stays in reasonable bounds
        
        # Detailed logging for strategy adaptation
        logging.debug(f"Strategy adapted: aggression={self.aggression_level:.2f}, risk={self.risk_tolerance:.2f}")
        logging.debug(f"Modifiers: position={position_mod:.2f}, stage={stage_mod:.2f}, life={life_mod:.2f}, " 
                    f"complexity={complexity_mod:.2f}, cards={card_advantage_mod:.2f}, " 
                    f"opponent={opponent_mod:.2f}, turn={turn_mod:.2f}")
        
        return {
            "aggression": self.aggression_level,
            "risk": self.risk_tolerance,
            "position": position,
            "game_stage": game_stage,
            "modifiers": aggression_modifiers
        }

    def establish_long_term_goals(self):
        """
        Establish strategic long-term goals based on deck archetype and game state.
        
        Provides a high-level strategic planning framework for multi-turn decision making.
        
        Returns:
            dict: Long-term strategic goals and prioritized objectives
        """
        # Ensure we have current analysis
        if not self.current_analysis:
            self.analyze_game_state()
        
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Initialize goal framework
        goals = {
            "primary_win_condition": None,
            "backup_win_condition": None,
            "immediate_objectives": [],
            "medium_term_objectives": [],
            "resource_priorities": {},
            "threat_assessment": {},
            "estimated_win_turn": None,
        }
        
        # Analyze viable win conditions
        win_conditions = self.identify_win_conditions()
        viable_wins = {k: v for k, v in win_conditions.items() if v["viable"]}
        
        # Sort by score and turns to win
        sorted_wins = sorted(viable_wins.items(), 
                            key=lambda x: (x[1]["score"], -x[1]["turns_to_win"]), 
                            reverse=True)
        
        # Set primary and backup win conditions
        if sorted_wins:
            goals["primary_win_condition"] = {
                "type": sorted_wins[0][0],
                "description": sorted_wins[0][1]["description"],
                "turns_to_win": sorted_wins[0][1]["turns_to_win"],
                "key_cards": sorted_wins[0][1]["key_cards"]
            }
            goals["estimated_win_turn"] = gs.turn + sorted_wins[0][1]["turns_to_win"]
            
            if len(sorted_wins) > 1:
                goals["backup_win_condition"] = {
                    "type": sorted_wins[1][0],
                    "description": sorted_wins[1][1]["description"],
                    "turns_to_win": sorted_wins[1][1]["turns_to_win"],
                    "key_cards": sorted_wins[1][1]["key_cards"]
                }
        
        # Set immediate objectives based on game state
        position = self.current_analysis["position"]["overall"]
        game_stage = self.current_analysis["game_info"]["game_stage"]
        
        if position in ["struggling", "behind"]:
            # When behind, focus on stabilizing
            goals["immediate_objectives"].append("Stabilize the board")
            goals["immediate_objectives"].append("Prevent further life loss")
            
            if len(opp["battlefield"]) > len(me["battlefield"]):
                goals["immediate_objectives"].append("Remove key threats")
        
        elif position in ["even"]:
            # When even, develop board or gain advantage
            goals["immediate_objectives"].append("Develop board presence")
            goals["immediate_objectives"].append("Gain card advantage")
            
            if game_stage == "mid":
                goals["immediate_objectives"].append("Prepare win condition")
        
        else:  # ahead or dominating
            # When ahead, press advantage
            goals["immediate_objectives"].append("Press advantage")
            goals["immediate_objectives"].append("Execute win condition")
            
            if goals["primary_win_condition"]:
                win_type = goals["primary_win_condition"]["type"]
                if win_type == "combat_damage":
                    goals["immediate_objectives"].append("Attack aggressively")
                elif win_type == "combo":
                    goals["immediate_objectives"].append("Protect combo pieces")
        
        # Set medium-term objectives
        if game_stage == "early":
            goals["medium_term_objectives"].append("Develop mana base")
            goals["medium_term_objectives"].append("Establish board presence")
        
        elif game_stage == "mid":
            goals["medium_term_objectives"].append("Gain card advantage")
            goals["medium_term_objectives"].append("Control key threats")
            goals["medium_term_objectives"].append("Set up win condition")
        
        else:  # late game
            goals["medium_term_objectives"].append("Execute win condition")
            goals["medium_term_objectives"].append("Prevent opponent's win condition")
        
        # Resource priorities
        if self.strategy_type == "aggro":
            goals["resource_priorities"] = {
                "mana": "efficiency",
                "life": "resource",
                "cards": "medium"
            }
        elif self.strategy_type == "control":
            goals["resource_priorities"] = {
                "mana": "high",
                "life": "high",
                "cards": "critical"
            }
        elif self.strategy_type == "midrange":
            goals["resource_priorities"] = {
                "mana": "medium",
                "life": "medium",
                "cards": "high"
            }
        elif self.strategy_type == "combo":
            goals["resource_priorities"] = {
                "mana": "critical",
                "life": "resource",
                "cards": "critical"
            }
        
        # Threat assessment
        threats = []
        for cid in opp["battlefield"]:
            card = gs._safe_get_card(cid)
            if not card:
                continue
                
            threat_level = 0
            
            # Basic threat assessment
            if hasattr(card, 'card_types') and 'creature' in card.card_types:
                if hasattr(card, 'power'):
                    threat_level += _card_number(card, 'power') * 0.5
            
            if hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                threat_level += 4  # Planeswalkers are high threats
            
            if hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                if 'when' in oracle_text and 'dies' in oracle_text:
                    threat_level += 1  # Death triggers
                
                if 'sacrifice' in oracle_text:
                    threat_level += 1  # Sacrifice outlets
                
                if 'draw' in oracle_text and 'card' in oracle_text:
                    threat_level += 1.5  # Card advantage engines
            
            if threat_level > 2:
                threats.append({
                    "card_id": cid,
                    "name": card.name if hasattr(card, 'name') else "Unknown",
                    "level": threat_level
                })
        
        # Sort threats by level
        threats.sort(key=lambda x: x["level"], reverse=True)
        goals["threat_assessment"] = threats[:3]  # Top 3 threats
        
        return goals

