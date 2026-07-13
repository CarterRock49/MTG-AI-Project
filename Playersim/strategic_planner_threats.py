"""Board analysis: threats, synergies, and position evaluation.

Extracted from strategic_planner.py. This module defines behavior only (a mixin);
all state lives on MTGStrategicPlanner, which composes every mixin.
"""

import logging
import math
import numpy as np


def _card_number(card, attribute, default=0.0):
    try:
        value = float(getattr(card, attribute, default) or 0)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


class ThreatSynergyMixin:
    """Board analysis: threats, synergies, and position evaluation."""

    __slots__ = ()

    def analyze_game_state(self):
        """
        Perform a comprehensive analysis of the current game state.
        
        Returns:
            dict: Analysis of the current game state
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Basic game info
        turn = gs.turn
        phase = gs.phase
        
        # Board state analysis
        my_creatures = [cid for cid in me["battlefield"] if gs._safe_get_card(cid) and 'creature' in gs._safe_get_card(cid).card_types]
        opp_creatures = [cid for cid in opp["battlefield"] if gs._safe_get_card(cid) and 'creature' in gs._safe_get_card(cid).card_types]
        
        my_power = sum(
            _card_number(gs._safe_get_card(cid), 'power')
            for cid in my_creatures if gs._safe_get_card(cid))
        my_toughness = sum(
            _card_number(gs._safe_get_card(cid), 'toughness')
            for cid in my_creatures if gs._safe_get_card(cid))
        
        opp_power = sum(
            _card_number(gs._safe_get_card(cid), 'power')
            for cid in opp_creatures if gs._safe_get_card(cid))
        opp_toughness = sum(
            _card_number(gs._safe_get_card(cid), 'toughness')
            for cid in opp_creatures if gs._safe_get_card(cid))
        
        # Resource analysis
        my_lands = [cid for cid in me["battlefield"] if gs._safe_get_card(cid) and 'land' in gs._safe_get_card(cid).card_types]
        opp_lands = [cid for cid in opp["battlefield"] if gs._safe_get_card(cid) and 'land' in gs._safe_get_card(cid).card_types]
        
        my_mana = sum(me["mana_pool"].values())
        my_cards = len(me["hand"])
        opp_cards = len(opp["hand"])
        
        # Life totals
        my_life = me["life"]
        opp_life = opp["life"]
        life_diff = my_life - opp_life
        
        # Graveyard analysis
        my_graveyard = [cid for cid in me["graveyard"] if gs._safe_get_card(cid)]
        opp_graveyard = [cid for cid in opp["graveyard"] if gs._safe_get_card(cid)]
        
        # Calculate advantage metrics
        board_advantage = self._calculate_board_advantage(my_creatures, opp_creatures, my_power, opp_power)
        card_advantage = my_cards - opp_cards
        mana_advantage = len(my_lands) - len(opp_lands)
        
        # Determine game stage
        if turn <= 3:
            game_stage = "early"
        elif turn <= 7:
            game_stage = "mid"
        else:
            game_stage = "late"
            
        # Determine overall position
        position_score = (
            board_advantage * 0.4 +
            card_advantage * 0.3 +
            mana_advantage * 0.2 +
            (life_diff / 10) * 0.1  # Normalize life diff to roughly -1 to 1
        )
        
        if position_score > 1.0:
            position = "dominating"
        elif position_score > 0.3:
            position = "ahead"
        elif position_score > -0.3:
            position = "even"
        elif position_score > -1.0:
            position = "behind"
        else:
            position = "struggling"
        
        # Determine tempo (board development relative to turn)
        my_tempo = sum(gs._safe_get_card(cid).cmc for cid in me["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc')) / max(1, turn)
        opp_tempo = sum(gs._safe_get_card(cid).cmc for cid in opp["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc')) / max(1, turn)
        tempo_advantage = my_tempo - opp_tempo
        
        # Win condition assessment
        win_conditions = self._assess_win_conditions(my_creatures, opp_creatures, my_life, opp_life)
        
        # Store the complete analysis
        self.current_analysis = {
            "game_info": {
                "turn": turn,
                "phase": phase,
                "game_stage": game_stage
            },
            "board_state": {
                "my_creatures": len(my_creatures),
                "opp_creatures": len(opp_creatures),
                "my_power": my_power,
                "my_toughness": my_toughness,
                "opp_power": opp_power,
                "opp_toughness": opp_toughness,
                "board_advantage": board_advantage
            },
            "resources": {
                "my_lands": len(my_lands),
                "opp_lands": len(opp_lands),
                "my_mana": my_mana,
                "my_cards": my_cards,
                "opp_cards": opp_cards,
                "card_advantage": card_advantage,
                "mana_advantage": mana_advantage
            },
            "life": {
                "my_life": my_life,
                "opp_life": opp_life,
                "life_diff": life_diff
            },
            "tempo": {
                "my_tempo": my_tempo,
                "opp_tempo": opp_tempo,
                "tempo_advantage": tempo_advantage
            },
            "position": {
                "overall": position,
                "score": position_score
            },
            "win_conditions": win_conditions
        }
        
        # Log a summary of the analysis
        logging.debug(f"Game state analysis: {game_stage} game, position: {position}, " 
                      f"board: {len(my_creatures)} vs {len(opp_creatures)}, life: {my_life} vs {opp_life}")
        
        return self.current_analysis

    def _calculate_board_advantage(self, my_creatures, opp_creatures, my_power, opp_power):
        """Calculate the current board advantage."""
        if not my_creatures and not opp_creatures:
            return 0
            
        # Creature count difference normalized to [-1, 1]
        count_diff = len(my_creatures) - len(opp_creatures)
        max_count = max(len(my_creatures) + len(opp_creatures), 1)
        normalized_count_diff = count_diff / max_count
        
        # Power difference normalized to [-1, 1]
        power_diff = my_power - opp_power
        max_power = max(my_power + opp_power, 1)
        normalized_power_diff = power_diff / max_power
        
        # Combined board advantage
        board_advantage = (normalized_count_diff * 0.4) + (normalized_power_diff * 0.6)
        
        return board_advantage

    def assess_threats(self, prioritize_removal=True):
        """
        Assess threats on the opponent's battlefield with detailed scoring and prioritization.
        
        Args:
            prioritize_removal: Whether to prioritize targets for removal
            
        Returns:
            list: Threat assessment results, sorted by priority
        """
        gs = self.game_state
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        
        my_creatures = [cid for cid in me["battlefield"] 
                    if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'card_types') and 
                    'creature' in gs._safe_get_card(cid).card_types]
        
        opp_creatures = [cid for cid in opp["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types]
        
        threats = []
        
        # Analyze opponent's battlefield
        for card_id in opp["battlefield"]:
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            
            # Base threat level starts at 0
            threat_level = 0
            threat_urgency = 0
            threat_categories = []
            
            if hasattr(card, 'card_types'):
                # Creatures - assess based on power, abilities, etc.
                if 'creature' in card.card_types:
                    # Power-based threat
                    if hasattr(card, 'power'):
                        power = _card_number(card, 'power')
                        threat_level += power * 0.7
                        
                        # High power creatures are more threatening
                        if power >= 6:
                            threat_level += 2
                            threat_categories.append("high_power")
                        elif power >= 4:
                            threat_level += 1
                            threat_categories.append("significant_power")
                    
                    # Check for evasion abilities
                    if hasattr(card, 'oracle_text'):
                        oracle_text = card.oracle_text.lower()
                        
                        # Evasive threats
                        if 'flying' in oracle_text:
                            threat_level += 1
                            threat_categories.append("evasive")
                        if 'trample' in oracle_text:
                            threat_level += 0.5
                            threat_categories.append("evasive")
                        if "can't be blocked" in oracle_text:
                            threat_level += 2
                            threat_categories.append("unblockable")
                        
                        # Protection abilities
                        if 'hexproof' in oracle_text:
                            threat_level += 1
                            threat_urgency += 1  # Harder to remove later
                            threat_categories.append("protected")
                        if 'indestructible' in oracle_text:
                            threat_level += 1.5
                            threat_urgency += 2  # Very hard to remove
                            threat_categories.append("indestructible")
                        
                        # Offensive keywords
                        if 'double strike' in oracle_text:
                            threat_level += 1.5
                            threat_categories.append("double_damage")
                        if 'deathtouch' in oracle_text:
                            threat_level += 0.5
                            threat_categories.append("combat_advantage")
                        if 'lifelink' in oracle_text:
                            threat_level += 0.5
                            threat_categories.append("life_swing")
                        
                        # Snowballing threats
                        if '+1/+1 counter' in oracle_text:
                            threat_level += 0.5
                            threat_urgency += 1  # Gets worse over time
                            threat_categories.append("growing")
                        
                        # Special abilities that generate value
                        if 'when' in oracle_text and 'enters the battlefield' in oracle_text:
                            if 'draw' in oracle_text:
                                threat_level += 1
                                threat_categories.append("card_advantage")
                            if 'destroy' in oracle_text or 'exile' in oracle_text:
                                threat_level += 1
                                threat_categories.append("removal")
                        
                        # On-hit triggers
                        if 'whenever' in oracle_text and 'deals combat damage' in oracle_text:
                            threat_level += 1
                            threat_urgency += 1  # Dangerous if it connects
                            threat_categories.append("combat_trigger")
                            
                            # Check severity of trigger
                            if 'draw' in oracle_text:
                                threat_level += 1
                                threat_categories.append("card_advantage")
                            if 'create' in oracle_text and 'token' in oracle_text:
                                threat_level += 1
                                threat_categories.append("token_generator")
                
                # Planeswalkers - high threat
                elif 'planeswalker' in card.card_types:
                    threat_level += 4  # Base threat for planeswalkers
                    threat_urgency += 2  # High priority to remove
                    threat_categories.append("planeswalker")
                    
                    # Check loyalty and abilities
                    if hasattr(card, 'loyalty'):
                        loyalty = card.loyalty
                        threat_level += min(3, loyalty / 2)  # Higher loyalty = more threat
                    
                    if hasattr(card, 'oracle_text'):
                        oracle_text = card.oracle_text.lower()
                        
                        # Check for particularly dangerous abilities
                        if 'draw' in oracle_text:
                            threat_level += 1
                            threat_categories.append("card_advantage")
                        if 'destroy' in oracle_text or 'exile' in oracle_text:
                            threat_level += 1
                            threat_categories.append("removal")
                        if 'ultimate' in oracle_text or 'emblem' in oracle_text:
                            threat_level += 2
                            threat_urgency += 1
                            threat_categories.append("game_ending")
                
                # Enchantments and Artifacts - assess based on effect
                elif 'enchantment' in card.card_types or 'artifact' in card.card_types:
                    if hasattr(card, 'oracle_text'):
                        oracle_text = card.oracle_text.lower()
                        
                        # Value engines
                        if 'at the beginning of' in oracle_text:
                            threat_level += 1
                            threat_urgency += 1  # Gets better over time
                            threat_categories.append("repeating_value")
                        
                        # Card advantage
                        if 'draw' in oracle_text:
                            threat_level += 2
                            threat_categories.append("card_advantage")
                        
                        # Mana advantage
                        if 'add' in oracle_text and any(f"{{{c}}}" in oracle_text for c in ['w', 'u', 'b', 'r', 'g']):
                            threat_level += 1
                            threat_categories.append("mana_advantage")
                        
                        # Combat advantage
                        if 'creatures you control get' in oracle_text:
                            threat_level += 1.5
                            threat_categories.append("anthem")
                        
                        # Lockdown effects
                        if "can't" in oracle_text:
                            threat_level += 2
                            threat_urgency += 1
                            threat_categories.append("stax")
            
            # Adjust for current game state
            # Lower threat when we have blockers for creatures
            if 'creature' in card.card_types and len(my_creatures) >= len(opp_creatures):
                threat_level *= 0.8
            
            # Higher threat when we're at low life
            if me["life"] <= 10 and 'creature' in card.card_types:
                threat_level *= 1.2
                threat_urgency += 1
            
            # Higher threat for things that enable win conditions
            win_conditions = self.identify_win_conditions()
            for wc_name, wc_data in win_conditions.items():
                if wc_data["viable"] and card_id in wc_data.get("key_cards", []):
                    threat_level *= 1.5
                    threat_urgency += 2
                    threat_categories.append("win_condition_enabler")
            
            # If specifically looking for removal targets
            if prioritize_removal:
                # Hard-to-remove threats get lower priority for removal
                if 'indestructible' in threat_categories:
                    threat_level *= 0.7  # Less ideal removal target
                if 'protected' in threat_categories:
                    threat_level *= 0.8  # Less ideal removal target
            
            # Final threat level is a combination of base threat and urgency
            final_threat = threat_level * (1 + 0.2 * threat_urgency)
            
            # Add to threat list if significant
            if final_threat > 0.5:
                threats.append({
                    "card_id": card_id,
                    "name": card.name if hasattr(card, 'name') else "Unknown Card",
                    "level": final_threat,
                    "raw_threat": threat_level,
                    "urgency": threat_urgency,
                    "categories": threat_categories,
                    "card_type": card.card_types[0] if hasattr(card, 'card_types') and card.card_types else "unknown"
                })
        
        # Sort by threat level
        threats.sort(key=lambda x: x["level"], reverse=True)
        
        # Log top threats
        if threats:
            logging.debug(f"Top threat: {threats[0]['name']} (Level: {threats[0]['level']:.1f}, Categories: {', '.join(threats[0]['categories'])})")
        else:
            logging.debug("No significant threats detected")
        
        return threats

    def identify_card_synergies(self, card_id, hand_ids, battlefield_ids):
        """
        Identify synergies between a card and other cards in hand or battlefield with comprehensive 
        analysis of tribal, mechanic, color and strategic synergies.
        
        Args:
            card_id: ID of the card to check for synergies
            hand_ids: List of card IDs in hand
            battlefield_ids: List of card IDs on battlefield
            
        Returns:
            float: Synergy score (higher = more synergy)
            dict: Detailed synergy breakdown by category
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0, {}
        
        # Base synergy value
        synergy_value = 0.0
        synergy_details = {
            "tribal": 0.0,
            "color": 0.0,
            "mechanic": 0.0,
            "curve": 0.0,
            "ability": 0.0,
            "card_specific": 0.0,
            "archetypes": 0.0,
            "keyword": 0.0,
            "combo": 0.0
        }
        
        # Get all cards for comparison (exclude self)
        comparison_cards = []
        comparison_cards.extend([gs._safe_get_card(cid) for cid in hand_ids if cid != card_id])
        comparison_cards.extend([gs._safe_get_card(cid) for cid in battlefield_ids if cid != card_id])
        comparison_cards = [c for c in comparison_cards if c]  # Filter out None values
        
        if not comparison_cards:
            return 0.0, synergy_details
        
        # Get card text and types for synergy analysis
        card_text = card.oracle_text.lower() if hasattr(card, 'oracle_text') else ""
        card_types = card.card_types if hasattr(card, 'card_types') else []
        card_name = card.name.lower() if hasattr(card, 'name') else ""
        card_cmc = card.cmc if hasattr(card, 'cmc') else 0
        
        # 1. Tribal Synergy Analysis
        if 'creature' in card_types and hasattr(card, 'subtypes'):
            card_creature_types = set(card.subtypes)
            tribal_synergy = 0.0
            tribal_synergy_cards = []
            tribal_lords = []
            
            # Define common tribal payoffs/lords
            tribal_lord_patterns = {
                "lord": [r"other (\w+)s you control get"],
                "tribal_payoff": [r"whenever a(nother)? (\w+) enters", r"for each (\w+) you control"],
                "tribal_cost": [r"(\w+) you control", r"sacrifice a(nother)? (\w+)"]
            }
            
            # Enhanced creature type synergy detection
            for comp_card in comparison_cards:
                if not hasattr(comp_card, 'oracle_text') or not hasattr(comp_card, 'subtypes'):
                    continue
                
                comp_text = comp_card.oracle_text.lower()
                comp_creature_types = set(comp_card.subtypes)
                
                # Check if our creature types are mentioned in other cards (tribal payoffs)
                for creature_type in card_creature_types:
                    creature_type_lower = creature_type.lower()
                    
                    # Direct mention of creature type
                    if creature_type_lower in comp_text:
                        # Check for lord effects that boost our creature
                        for pattern_type, patterns in tribal_lord_patterns.items():
                            for pattern in patterns:
                                import re
                                matches = re.findall(pattern.replace(r"(\w+)", creature_type_lower), comp_text)
                                if matches:
                                    if pattern_type == "lord":
                                        tribal_synergy += 0.8
                                        tribal_lords.append(comp_card)
                                    elif pattern_type == "tribal_payoff":
                                        tribal_synergy += 0.6
                                    else:
                                        tribal_synergy += 0.4
                                    tribal_synergy_cards.append(comp_card)
                    
                    # Check for more general tribal effects that might not mention type by name
                    for pattern_type, patterns in tribal_lord_patterns.items():
                        for pattern in patterns:
                            import re
                            type_matches = re.findall(pattern, comp_text)
                            for match in type_matches:
                                match_type = match[1] if isinstance(match, tuple) and len(match) > 1 else match
                                if match_type == creature_type_lower:
                                    tribal_synergy += 0.5
                                    tribal_synergy_cards.append(comp_card)
                
                # Check if other card shares our creature types (e.g., both are Elves)
                shared_types = card_creature_types.intersection(comp_creature_types)
                if shared_types:
                    # More value for sharing multiple types
                    type_synergy = 0.3 * len(shared_types)
                    tribal_synergy += type_synergy
                    tribal_synergy_cards.append(comp_card)
            
            # Cap tribal synergy at a reasonable value but allow for high synergy
            synergy_value += min(3.0, tribal_synergy)
            synergy_details["tribal"] = min(3.0, tribal_synergy)
            synergy_details["tribal_cards"] = list(set([c.name if hasattr(c, 'name') else "Unknown Card" for c in tribal_synergy_cards]))
            if tribal_lords:
                synergy_details["tribal_lords"] = list(set([c.name if hasattr(c, 'name') else "Unknown Card" for c in tribal_lords]))
        
        # 2. Enhanced Mechanic Synergy Analysis
        # Expanded with more Magic-specific mechanics and synergy patterns
        mechanic_synergies = {
            # Core mechanics
            "counter": {
                "keywords": ["counter", "+1/+1", "-1/-1", "proliferate", "adapt", "evolve", "mentor", "support", "bolster", "modular"],
                "value": 0.0,
                "cards": []
            },
            "sacrifice": {
                "keywords": ["sacrifice", "dies", "when", "whenever", "graveyard", "exploit", "emerge", "devour", "casualty", "aristocrats"],
                "value": 0.0,
                "cards": []
            },
            "discard": {
                "keywords": ["discard", "madness", "hellbent", "delirium", "threshold", "cycling", "channel", "loot", "rummage"],
                "value": 0.0,
                "cards": []
            },
            "lifegain": {
                "keywords": ["life", "gain life", "lifelink", "extort", "devotion", "drain", "soul", "bond", "tribute"],
                "value": 0.0,
                "cards": []
            },
            "tokens": {
                "keywords": ["create", "token", "populate", "convoke", "afterlife", "amass", "fabricate", "swarm"],
                "value": 0.0,
                "cards": []
            },
            "spellslinger": {
                "keywords": ["instant", "sorcery", "cast", "prowess", "magecraft", "storm", "splice", "cipher", "spell", "non-creature"],
                "value": 0.0,
                "cards": []
            },
            "artifacts": {
                "keywords": ["artifact", "metalcraft", "affinity", "improvise", "fabricate", "modular", "contraption", "thopter", "construct"],
                "value": 0.0,
                "cards": []
            },
            "enchantments": {
                "keywords": ["enchantment", "aura", "constellation", "bestow", "totem armor", "saga", "shrine"],
                "value": 0.0,
                "cards": []
            },
            "graveyard": {
                "keywords": ["graveyard", "flashback", "unearth", "dredge", "delve", "escape", "aftermath", "embalm", "eternalize", "reanimation", "recursion"],
                "value": 0.0,
                "cards": []
            },
            "landfall": {
                "keywords": ["landfall", "land", "enters", "battlefield", "search library", "ramp", "exploration", "amulet", "dryad"],
                "value": 0.0,
                "cards": []
            },
            "combat": {
                "keywords": ["combat", "attack", "block", "exalted", "raid", "battalion", "bloodthirst", "battle cry", "mentor", "double strike", "first strike", "deathtouch"],
                "value": 0.0,
                "cards": []
            },
            "tapping": {
                "keywords": ["tap", "untap", "does not untap", "exert", "inspired", "vehicles", "crew", "convoke"],
                "value": 0.0,
                "cards": []
            },
            "blink": {
                "keywords": ["exile", "return", "flicker", "blink", "teleportation", "bounces", "phase out", "phasing"],
                "value": 0.0,
                "cards": []
            },
            "tribal": {
                "keywords": ["creature type", "lord", "elf", "goblin", "merfolk", "zombie", "human", "warrior", "wizard", "knight", "dinosaur", "dragon"],
                "value": 0.0,
                "cards": []
            },
            "mill": {
                "keywords": ["mill", "put top card", "library into graveyard", "cards from top of library"],
                "value": 0.0,
                "cards": []
            },
            # Advanced mechanic groups
            "protection": {
                "keywords": ["hexproof", "protection", "indestructible", "shroud", "ward", "regenerate", "phasing"],
                "value": 0.0,
                "cards": []
            },
            "recursion": {
                "keywords": ["return", "from graveyard", "to hand", "to battlefield", "exile", "embalm", "eternalize", "flashback"],
                "value": 0.0,
                "cards": []
            },
            "ramp": {
                "keywords": ["search library for land", "put land onto battlefield", "additional land", "mana dork", "mana rock", "treasure", "add mana"],
                "value": 0.0,
                "cards": []
            },
            "copy": {
                "keywords": ["copy", "clone", "replicate", "token that's a copy", "populate"],
                "value": 0.0,
                "cards": []
            }
        }
        
        # Check for our card enabling synergies
        card_mechanics = set()
        for mechanic, data in mechanic_synergies.items():
            if any(keyword in card_text for keyword in data["keywords"]):
                card_mechanics.add(mechanic)
        
        # For each comparison card, check mechanics synergy
        for comp_card in comparison_cards:
            if not hasattr(comp_card, 'oracle_text'):
                continue
                
            comp_text = comp_card.oracle_text.lower()
            comp_name = comp_card.name if hasattr(comp_card, 'name') else "Unknown Card"
            
            # Check for synergies between mechanics
            for mechanic in card_mechanics:
                data = mechanic_synergies[mechanic]
                
                # If comparison card also has this mechanic, there's synergy
                if any(keyword in comp_text for keyword in data["keywords"]):
                    # Value the synergy based on mechanic type
                    mechanic_value = 0.0
                    
                    # Higher value for engine-building mechanics
                    if mechanic in ["sacrifice", "discard", "lifegain", "tokens", "landfall", "counter"]:
                        mechanic_value = 0.7
                    # Medium value for strategy-enabling mechanics
                    elif mechanic in ["spellslinger", "combat", "graveyard", "recursion", "ramp", "blink"]:
                        mechanic_value = 0.5
                    # Lower value for support mechanics
                    else:
                        mechanic_value = 0.3
                    
                    # Add synergy
                    mechanic_synergies[mechanic]["value"] += mechanic_value
                    mechanic_synergies[mechanic]["cards"].append(comp_name)
        
        # Calculate total mechanic synergy
        mechanic_synergy = 0.0
        mechanic_details = {}
        for mechanic, data in mechanic_synergies.items():
            value = data["value"]
            if value > 0:
                mechanic_synergy += value
                mechanic_details[mechanic] = {
                    "value": value,
                    "cards": list(set(data["cards"]))
                }
        
        # Cap mechanic synergy at a reasonable value
        synergy_value += min(4.0, mechanic_synergy)
        synergy_details["mechanic"] = min(4.0, mechanic_synergy)
        if mechanic_details:
            synergy_details["mechanic_details"] = mechanic_details

        # 3. Color Synergy Analysis
        if hasattr(card, 'colors'):
            card_colors = card.colors
            color_synergy = 0.0
            color_match_cards = []
            color_names = ['White', 'Blue', 'Black', 'Red', 'Green']
            card_color_count = sum(card_colors)
            card_color_names = [color_names[i] for i in range(len(card_colors)) if i < len(card_colors) and card_colors[i]]
            
            # Track multicolor themes
            multicolor_theme = card_color_count > 1
            
            for comp_card in comparison_cards:
                if not hasattr(comp_card, 'colors'):
                    continue
                    
                comp_colors = comp_card.colors
                # Count matching colors
                matching_colors = sum(
                    1 for i in range(min(len(card_colors), len(comp_colors))) 
                    if i < len(card_colors) and i < len(comp_colors) and 
                    card_colors[i] == 1 and comp_colors[i] == 1
                )
                
                # Value depends on how many colors match
                if matching_colors > 0:
                    # More value for exact color matches (all colors match)
                    comp_color_count = sum(comp_colors)
                    
                    # Perfect color identity match
                    if matching_colors == card_color_count and matching_colors == comp_color_count:
                        color_synergy += 0.5
                        color_match_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
                    # Partial match but good overlap
                    elif matching_colors >= 2:
                        color_synergy += 0.3
                        color_match_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
                    # Single color match
                    else:
                        color_synergy += 0.1
                    
                    # Check for multicolor synergies
                    if multicolor_theme and comp_color_count > 1:
                        comp_text = comp_card.oracle_text.lower() if hasattr(comp_card, 'oracle_text') else ""
                        # Cards that care about multicolor
                        if "multicolored" in comp_text or "multicolor" in comp_text:
                            color_synergy += 0.5
                            if comp_card.name not in color_match_cards:
                                color_match_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
            
            # Check for color-matters text in either direction
            for comp_card in comparison_cards:
                if not hasattr(comp_card, 'oracle_text'):
                    continue
                    
                comp_text = comp_card.oracle_text.lower()
                
                # Check for color-matters texts
                for i, color_name in enumerate(color_names):
                    if i < len(card_colors) and card_colors[i]:
                        color_lower = color_name.lower()
                        if f"{color_lower} permanent" in comp_text or f"{color_lower} spell" in comp_text or f"{color_lower} card" in comp_text:
                            color_synergy += 0.4
                            if comp_card.name not in color_match_cards:
                                color_match_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
            
            # Cap color synergy and add to details
            synergy_value += min(2.0, color_synergy)
            synergy_details["color"] = min(2.0, color_synergy)
            if color_match_cards:
                synergy_details["color_cards"] = color_match_cards
    
        # 4. Mana Curve and Cost Synergy
        if hasattr(card, 'cmc'):
            curve_synergy = 0.0
            curve_synergy_cards = []
            
            # Different patterns based on card cost
            if card_cmc >= 5:  # High-cost card
                # Look for mana ramp/acceleration
                for comp_card in comparison_cards:
                    if not hasattr(comp_card, 'oracle_text'):
                        continue
                        
                    comp_text = comp_card.oracle_text.lower()
                    # Ramp patterns
                    is_ramp = (
                        ("search your library for" in comp_text and "land" in comp_text) or
                        ("add" in comp_text and any(f"{{{c}}}" in comp_text for c in ['w', 'u', 'b', 'r', 'g'])) or
                        ("untap" in comp_text and "land" in comp_text) or
                        ("mana" in comp_text and "cost" in comp_text and "less" in comp_text) or
                        ("additional mana" in comp_text)
                    )
                    
                    if is_ramp:
                        curve_synergy += 0.5
                        curve_synergy_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
            
            elif card_cmc <= 2:  # Low-cost card
                # Check if this enables high-cost cards
                for comp_card in comparison_cards:
                    if not hasattr(comp_card, 'cmc'):
                        continue
                        
                    # Synergy with high-cost payoffs
                    if comp_card.cmc >= 5:
                        curve_synergy += 0.3
                        curve_synergy_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
                    
                # Value for early interaction
                if "counter" in card_text or "destroy" in card_text or "exile" in card_text:
                    for comp_card in comparison_cards:
                        if hasattr(comp_card, 'cmc') and comp_card.cmc >= 4:
                            curve_synergy += 0.2
                            curve_synergy_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
                
                # Value for early card draw/filtering
                if "draw" in card_text or "scry" in card_text:
                    curve_synergy += 0.3
            
            # Look for cost reduction effects
            for comp_card in comparison_cards:
                if not hasattr(comp_card, 'oracle_text'):
                    continue
                    
                comp_text = comp_card.oracle_text.lower()
                
                # Cost reduction effects
                if ("cost" in comp_text and "less" in comp_text) or ("reduce" in comp_text and "cost" in comp_text):
                    curve_synergy += 0.4
                    curve_synergy_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
            
            # Add curve synergy
            synergy_value += min(2.0, curve_synergy)
            synergy_details["curve"] = min(2.0, curve_synergy)
            if curve_synergy_cards:
                synergy_details["curve_cards"] = curve_synergy_cards
        
        # 5. Ability-based Synergies
        ability_synergy = 0.0
        ability_synergy_cards = []
        ability_pairs = []
        
        # Common ability pairings in MTG
        ability_pairs = [
            {"abilities": ["deathtouch", "first strike"], "value": 0.6},
            {"abilities": ["deathtouch", "double strike"], "value": 0.8},
            {"abilities": ["trample", "gets +"], "value": 0.5},
            {"abilities": ["lifelink", "whenever you gain life"], "value": 0.7},
            {"abilities": ["flying", "equip"], "value": 0.4},
            {"abilities": ["flying", "enchant creature"], "value": 0.4},
            {"abilities": ["hexproof", "gets +"], "value": 0.5},
            {"abilities": ["indestructible", "destroy all"], "value": 0.9},
            {"abilities": ["haste", "gets +"], "value": 0.5},
            {"abilities": ["menace", "gets +"], "value": 0.4},
            {"abilities": ["vigilance", "exalted"], "value": 0.5},
            {"abilities": ["double strike", "gets +"], "value": 0.7},
            {"abilities": ["flash", "counter"], "value": 0.6},
            {"abilities": ["deathtouch", "fight"], "value": 0.8},
            {"abilities": ["indestructible", "sacrifice"], "value": 0.7},
            {"abilities": ["vigilance", "untap"], "value": 0.5},
            {"abilities": ["flying", "gets +"], "value": 0.6},
            {"abilities": ["first strike", "gets +"], "value": 0.5},
            {"abilities": ["deathtouch", "ping"], "value": 0.8},  # Ping effects like "deal 1 damage"
            {"abilities": ["lifelink", "drain"], "value": 0.7},
            {"abilities": ["trample", "double strike"], "value": 0.8},
            {"abilities": ["double strike", "first strike"], "value": 0.3},  # Less synergy as double strike includes first strike
            {"abilities": ["flash", "etb"], "value": 0.6},  # ETB (enters-the-battlefield) effects
            {"abilities": ["flash", "sacrifice"], "value": 0.5},
            {"abilities": ["hexproof", "aura"], "value": 0.6},
            {"abilities": ["hexproof", "equipment"], "value": 0.6},
            {"abilities": ["vigilance", "attacks"], "value": 0.5}
        ]
        
        # Check for ability synergies
        for pair in ability_pairs:
            if pair["abilities"][0] in card_text:
                # Find cards with matching second ability
                for comp_card in comparison_cards:
                    if hasattr(comp_card, 'oracle_text') and pair["abilities"][1] in comp_card.oracle_text.lower():
                        ability_synergy += pair["value"]
                        ability_synergy_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
            
            # Check the other way around
            elif pair["abilities"][1] in card_text:
                # Find cards with matching first ability
                for comp_card in comparison_cards:
                    if hasattr(comp_card, 'oracle_text') and pair["abilities"][0] in comp_card.oracle_text.lower():
                        ability_synergy += pair["value"]
                        ability_synergy_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
        
        # Add ability synergy
        synergy_value += min(3.0, ability_synergy)
        synergy_details["ability"] = min(3.0, ability_synergy)
        if ability_synergy_cards:
            synergy_details["ability_cards"] = list(set(ability_synergy_cards))
        
        # 6. Card-Specific Combos and Interactions
        combo_synergy = 0.0
        combo_cards = []
        
        # Define known powerful card combinations
        combo_pairs = [
            # Basic combos
            {"cards": ["exquisite blood", "sanguine bond"], "value": 3.0},
            {"cards": ["helm of obedience", "rest in peace"], "value": 3.0},
            {"cards": ["splinter twin", "deceiver exarch"], "value": 3.0},
            {"cards": ["reveillark", "karmic guide"], "value": 2.5},
            {"cards": ["kiki-jiki", "pestermite"], "value": 3.0},
            {"cards": ["isochron scepter", "dramatic reversal"], "value": 2.5},
            {"cards": ["siona", "shielded by faith"], "value": 2.5},
            {"cards": ["heliod", "walking ballista"], "value": 3.0},
            {"cards": ["mikaeus", "triskelion"], "value": 3.0},
            {"cards": ["thopter foundry", "sword of the meek"], "value": 2.5},
            {"cards": ["urza", "winter orb"], "value": 2.0},
            {"cards": ["deadeye navigator", "peregrine drake"], "value": 2.5},
            {"cards": ["omniscience", "enter the infinite"], "value": 2.5},
            {"cards": ["tinker", "blightsteel colossus"], "value": 2.5},
            {"cards": ["laboratory maniac", "demonic consultation"], "value": 2.5},
            {"cards": ["worldgorger dragon", "animate dead"], "value": 3.0},
            {"cards": ["aluren", "imperial recruiter"], "value": 2.0},
            {"cards": ["food chain", "misthollow griffin"], "value": 2.5},
            {"cards": ["painter's servant", "grindstone"], "value": 3.0},
            {"cards": ["doomsday", "laboratory maniac"], "value": 2.5},
            {"cards": ["krark", "sakashima"], "value": 2.0},
            {"cards": ["underworld breach", "lions eye diamond"], "value": 2.5},
            {"cards": ["ad nauseam", "angel's grace"], "value": 2.5},
            {"cards": ["polymorph", "emrakul"], "value": 2.0},
            {"cards": ["godo", "helm of the host"], "value": 2.5},
            {"cards": ["dramatic reversal", "isochron scepter"], "value": 2.5},
            {"cards": ["protean hulk", "viscera seer"], "value": 2.0},
            {"cards": ["blood artist", "gravecrawler"], "value": 1.5},
            {"cards": ["ashnod's altar", "nim deathmantle"], "value": 2.0},
            {"cards": ["sanguine bond", "exquisite blood"], "value": 3.0}
        ]
        
        # Pattern-based combos
        combo_patterns = [
            {"pattern1": "sacrifice", "pattern2": "when creature dies", "value": 1.5},
            {"pattern1": "untap", "pattern2": "add mana", "value": 2.0},
            {"pattern1": "copy", "pattern2": "spell", "value": 1.5},
            {"pattern1": "exile", "pattern2": "return to battlefield", "value": 1.5},
            {"pattern1": "extra turn", "pattern2": "return from graveyard", "value": 2.5},
            {"pattern1": "damage", "pattern2": "lifelink", "value": 1.0},
            {"pattern1": "double", "pattern2": "token", "value": 1.5},
            {"pattern1": "search library", "pattern2": "put onto battlefield", "value": 1.5},
            {"pattern1": "discard", "pattern2": "return from graveyard", "value": 1.5},
            {"pattern1": "counter", "pattern2": "return to hand", "value": 1.5},
            {"pattern1": "draw card", "pattern2": "discard", "value": 1.0},
            {"pattern1": "etb", "pattern2": "blink", "value": 1.5},
            {"pattern1": "sacrifice", "pattern2": "token", "value": 1.5},
            {"pattern1": "copy", "pattern2": "token", "value": 1.5},
            {"pattern1": "whenever", "pattern2": "untap", "value": 1.5},
            {"pattern1": "cast", "pattern2": "copy", "value": 1.5}
        ]
        
        # Check for specific named combos
        card_name_lower = card_name.lower()
        for combo in combo_pairs:
            if card_name_lower in combo["cards"]:
                # Look for other combo pieces
                other_pieces = [piece for piece in combo["cards"] if piece != card_name_lower]
                for other_piece in other_pieces:
                    for comp_card in comparison_cards:
                        comp_name = comp_card.name.lower() if hasattr(comp_card, 'name') else ""
                        if other_piece in comp_name:
                            combo_synergy += combo["value"]
                            combo_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
        
        # Check for pattern-based combos
        for pattern in combo_patterns:
            if pattern["pattern1"] in card_text:
                for comp_card in comparison_cards:
                    if hasattr(comp_card, 'oracle_text') and pattern["pattern2"] in comp_card.oracle_text.lower():
                        combo_synergy += pattern["value"]
                        combo_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
            elif pattern["pattern2"] in card_text:
                for comp_card in comparison_cards:
                    if hasattr(comp_card, 'oracle_text') and pattern["pattern1"] in comp_card.oracle_text.lower():
                        combo_synergy += pattern["value"]
                        combo_cards.append(comp_card.name if hasattr(comp_card, 'name') else "Unknown Card")
        
        # Add combo synergy
        synergy_value += min(5.0, combo_synergy)  # Allow higher cap for powerful combos
        synergy_details["combo"] = min(5.0, combo_synergy)
        if combo_cards:
            synergy_details["combo_cards"] = list(set(combo_cards))
        
        # 7. Keyword Synergy Analysis
        keywords_list = [
            "flying", "first strike", "double strike", "deathtouch", "hexproof", 
            "indestructible", "lifelink", "menace", "reach", "trample", "vigilance",
            "flash", "haste", "defender", "protection", "ward", "cascade", "convoke",
            "cycling", "delve", "emerge", "encore", "entwine", "equip", "escape",
            "evoke", "exalted", "extort", "fading", "flanking", "flashback", "fortify",
            "fuse", "graft", "gravestorm", "hideaway", "infect", "jumpstart", "kicker",
            "landfall", "madness", "miracle", "modular", "morph", "myriad", "ninjutsu",
            "offering", "overload", "persist", "proliferate", "prowess", "rampage",
            "rebound", "replicate", "retrace", "riot", "ripple", "scavenge", "shadow",
            "soulbond", "storm", "sunburst", "surge", "totem armor", "transform",
            "transmute", "undying", "unearth", "unleash", "vanishing", "wither"
        ]
        
        # Extract keywords from the card
        card_keywords = []
        for keyword in keywords_list:
            if keyword in card_text:
                card_keywords.append(keyword)
        
        # Analyze keyword synergy with other cards
        keyword_synergy = 0.0
        keyword_synergy_cards = {}
        
        # Define keyword pairs that work well together
        keyword_synergy_pairs = {
            "deathtouch": ["first strike", "double strike", "trample", "fight", "damage"],
            "lifelink": ["double strike", "first strike", "life gain", "life total", "drain"],
            "double strike": ["deathtouch", "lifelink", "trample", "power", "gets +"],
            "first strike": ["deathtouch", "lifelink", "power", "gets +"],
            "flying": ["power", "gets +", "trample", "vigilance"],
            "trample": ["deathtouch", "double strike", "power", "gets +"],
            "hexproof": ["aura", "equipment", "enchant", "gets +"],
            "indestructible": ["board wipe", "destroy", "sacrifice", "damage"],
            "vigilance": ["exalted", "when attack", "untap"],
            "menace": ["power", "gets +"],
            "haste": ["power", "gets +", "etb"],
            "flash": ["counter", "instant", "draw", "response"],
            "defender": ["high toughness", "damage on defense"],
            "infect": ["proliferate", "power", "gets +"],
            "unearth": ["discard", "mill", "sacrifice"],
            "undying": ["sacrifice", "counter", "remove counter"],
            "persist": ["sacrifice", "counter", "remove counter"]
        }
        
        for keyword in card_keywords:
            if keyword in keyword_synergy_pairs:
                synergy_triggers = keyword_synergy_pairs[keyword]
                
                # Check each comparison card for synergy triggers
                for comp_card in comparison_cards:
                    if not hasattr(comp_card, 'oracle_text'):
                        continue
                        
                    comp_text = comp_card.oracle_text.lower()
                    comp_name = comp_card.name if hasattr(comp_card, 'name') else "Unknown Card"
                    
                    for trigger in synergy_triggers:
                        if trigger in comp_text:
                            # More value for explicit keyword mentions
                            if keyword in comp_text:
                                keyword_synergy += 0.7
                            else:
                                keyword_synergy += 0.4
                            
                            if keyword not in keyword_synergy_cards:
                                keyword_synergy_cards[keyword] = []
                            if comp_name not in keyword_synergy_cards[keyword]:
                                keyword_synergy_cards[keyword].append(comp_name)
        
        # Add keyword synergy
        synergy_value += min(2.5, keyword_synergy)
        synergy_details["keyword"] = min(2.5, keyword_synergy)
        if keyword_synergy_cards:
            synergy_details["keyword_synergy"] = keyword_synergy_cards
        
        # 8. Archetype Synergy Analysis
        archetype_synergy = 0.0
        archetype_match_cards = []
        
        # Common magic archetypes and their key indicators
        archetypes = {
            "aggro": ["haste", "attack", "power", "gets +", "combat", "fast", "anthem", "burn"],
            "control": ["counter", "destroy", "exile", "removal", "board wipe", "draw", "return to hand"],
            "midrange": ["value", "etb", "remove", "draw", "efficient", "utility"],
            "combo": ["infinite", "loop", "search", "tutor", "mana", "untap", "copy", "extra turn"],
            "tempo": ["flash", "bounce", "tap", "return to hand", "counter", "flying", "evasion"],
            "ramp": ["search for land", "add mana", "untap", "extra land", "land", "draws", "big creature"],
            "tokens": ["create", "token", "copy", "anthem", "populate", "etb", "leaves", "creature"],
            "reanimator": ["graveyard", "return", "discard", "mill", "put into", "from graveyard", "resurrection"],
            "aristocrats": ["sacrifice", "dies", "creature dies", "when", "blood artist", "token", "death"],
            "spellslinger": ["cast", "instant", "sorcery", "copy", "prowess", "magecraft", "storm"],
            "voltron": ["equipped", "attach", "aura", "enchant", "commander", "enchantment", "gets +"],
            "tribal": ["creature type", "elf", "goblin", "zombie", "human", "merfolk", "dragon", "dinosaur", "gets +"],
            "superfriends": ["planeswalker", "loyalty", "counter", "proliferate", "emblem", "ultimate"],
            "lifegain": ["life", "gain", "lifelink", "whenever you gain life", "life total", "drain"],
            "mill": ["mill", "put cards into graveyard", "put top card", "library into", "deck", "cards in graveyard"]
        }
        
        # Check which archetypes this card supports
        card_archetypes = []
        for archetype, indicators in archetypes.items():
            if any(indicator in card_text for indicator in indicators):
                card_archetypes.append(archetype)
        
        # Check for other cards that share these archetypes
        for archetype in card_archetypes:
            for comp_card in comparison_cards:
                if not hasattr(comp_card, 'oracle_text'):
                    continue
                    
                comp_text = comp_card.oracle_text.lower()
                comp_name = comp_card.name if hasattr(comp_card, 'name') else "Unknown Card"
                
                # Check if comparison card also fits this archetype
                if any(indicator in comp_text for indicator in archetypes[archetype]):
                    archetype_synergy += 0.5
                    archetype_match_cards.append((comp_name, archetype))
        
        # Add archetype synergy
        synergy_value += min(3.0, archetype_synergy)
        synergy_details["archetypes"] = min(3.0, archetype_synergy)
        if archetype_match_cards:
            archetype_cards = {}
            for card_name, archetype in archetype_match_cards:
                if archetype not in archetype_cards:
                    archetype_cards[archetype] = []
                if card_name not in archetype_cards[archetype]:
                    archetype_cards[archetype].append(card_name)
            synergy_details["archetype_cards"] = archetype_cards
        
        # Final synergy calculation - cap total value to prevent extreme scores
        # but allow for really powerful combinations to score highly
        final_synergy = min(float(synergy_value), 10.0)
        
        return final_synergy, synergy_details

    def advanced_position_evaluation(self):
        """
        Evaluate the current board position using comprehensive strategic metrics.
        
        Returns:
            float: Position score between -1.0 and 1.0
            dict: Detailed evaluation components
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # 1. Material advantage (creatures, planeswalkers and other permanents)
        my_permanents = me["battlefield"]
        opp_permanents = opp["battlefield"]
        
        # Calculate material value with card quality consideration
        my_material_value = sum(self._get_card_value(cid) for cid in my_permanents)
        opp_material_value = sum(self._get_card_value(cid) for cid in opp_permanents)
        
        # Normalize to [-1, 1]
        total_material = my_material_value + opp_material_value
        material_advantage = (my_material_value - opp_material_value) / max(1, total_material)
        
        # 2. Card advantage (card quantity and quality)
        my_hand_size = len(me["hand"])
        opp_hand_size = len(opp["hand"])
        my_hand_value = sum(self._get_card_value(cid) for cid in me["hand"])
        
        # Card advantage score
        card_advantage = (my_hand_size - opp_hand_size) / max(1, my_hand_size + opp_hand_size)
        
        # 3. Board presence (creatures and their stats)
        my_creatures = [cid for cid in my_permanents 
                    if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'card_types') and 
                    'creature' in gs._safe_get_card(cid).card_types]
                    
        opp_creatures = [cid for cid in opp_permanents 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types]
        
        my_power = sum(gs._safe_get_card(cid).power for cid in my_creatures 
                    if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
        
        my_toughness = sum(gs._safe_get_card(cid).toughness for cid in my_creatures 
                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))
        
        opp_power = sum(gs._safe_get_card(cid).power for cid in opp_creatures 
                    if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
        
        opp_toughness = sum(gs._safe_get_card(cid).toughness for cid in opp_creatures 
                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))
        
        # Calculate board advantages
        creature_count_advantage = (len(my_creatures) - len(opp_creatures)) / max(1, len(my_creatures) + len(opp_creatures))
        power_advantage = (my_power - opp_power) / max(1, my_power + opp_power)
        toughness_advantage = (my_toughness - opp_toughness) / max(1, my_toughness + opp_toughness)
        
        # 4. Tempo advantage (board development relative to mana investment)
        my_mana_curve = sum(gs._safe_get_card(cid).cmc for cid in my_permanents 
                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc'))
        
        opp_mana_curve = sum(gs._safe_get_card(cid).cmc for cid in opp_permanents 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc'))
        
        my_lands = [cid for cid in my_permanents if gs._safe_get_card(cid) and 
                hasattr(gs._safe_get_card(cid), 'type_line') and 'land' in gs._safe_get_card(cid).type_line]
        
        opp_lands = [cid for cid in opp_permanents if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'type_line') and 'land' in gs._safe_get_card(cid).type_line]
        
        my_tempo = my_mana_curve / max(1, len(my_lands))
        opp_tempo = opp_mana_curve / max(1, len(opp_lands))
        tempo_advantage = (my_tempo - opp_tempo) / max(1, my_tempo + opp_tempo)
        
        # 5. Life total advantage
        life_advantage = (me["life"] - opp["life"]) / max(1, me["life"] + opp["life"])
        
        # 6. Strategic resource advantage (mana development)
        mana_advantage = (len(my_lands) - len(opp_lands)) / max(1, len(my_lands) + len(opp_lands))
        
        # 7. Battlefield control (planeswalkers, removal potential)
        my_walkers = [cid for cid in my_permanents 
                    if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'card_types') and 
                    'planeswalker' in gs._safe_get_card(cid).card_types]
                    
        opp_walkers = [cid for cid in opp_permanents 
                    if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'card_types') and 
                    'planeswalker' in gs._safe_get_card(cid).card_types]
                    
        walker_advantage = (len(my_walkers) - len(opp_walkers)) / max(1, len(my_walkers) + len(opp_walkers) + 1)
        
        # 8. Synergy evaluation (improved from base analysis)
        synergy_value = 0.0
        if hasattr(self, 'identify_card_synergies'):
            total_synergy = 0.0
            count = 0
            for card_id in my_permanents[:10]:  # Limit to 10 cards for performance
                synergy_score, _ = self.identify_card_synergies(card_id, me["hand"], my_permanents)
                total_synergy += synergy_score
                count += 1
            
            if count > 0:
                synergy_value = min(1.0, total_synergy / (count * 2))  # Normalize
        
        # 9. Win condition proximity
        win_condition_value = 0.0
        if hasattr(self, 'identify_win_conditions'):
            win_conditions = self.identify_win_conditions()
            viable_wins = [wc for wc_name, wc in win_conditions.items() if wc["viable"]]
            
            if viable_wins:
                # Get the fastest win condition
                fastest_win = min(viable_wins, key=lambda wc: wc["turns_to_win"])
                turns_to_win = fastest_win["turns_to_win"]
                
                # Higher value for closer wins
                win_condition_value = 1.0 / max(1, turns_to_win)
        
        # Weight these factors based on game stage and strategy
        game_stage = "early" if gs.turn <= 3 else "mid" if gs.turn <= 8 else "late"
        
        # Adjust weights based on strategy type and game stage
        weights = {
            "material": 0.15,
            "card_advantage": 0.15,
            "creature_count": 0.05,
            "power": 0.10,
            "toughness": 0.05,
            "tempo": 0.10,
            "life": 0.10,
            "mana": 0.10,
            "walker": 0.05,
            "synergy": 0.05,
            "win_condition": 0.10
        }
        
        # Adapt weights to game stage
        if game_stage == "early":
            # Early game focuses on mana and board development
            weights["mana"] = 0.20
            weights["tempo"] = 0.15
            weights["material"] = 0.10
            weights["life"] = 0.05
        elif game_stage == "late":
            # Late game focuses on win conditions and card advantage
            weights["win_condition"] = 0.20
            weights["power"] = 0.15
            weights["life"] = 0.15
            weights["card_advantage"] = 0.10
        
        # Adapt weights to strategy type
        if hasattr(self, 'strategy_type'):
            if self.strategy_type == "aggro":
                weights["power"] = 0.20
                weights["tempo"] = 0.15
                weights["life"] = 0.05
            elif self.strategy_type == "control":
                weights["card_advantage"] = 0.20
                weights["walker"] = 0.10
                weights["material"] = 0.10
            elif self.strategy_type == "combo":
                weights["synergy"] = 0.15
                weights["win_condition"] = 0.20
        
        # Calculate final score
        components = {
            "material": material_advantage,
            "card_advantage": card_advantage,
            "creature_count": creature_count_advantage,
            "power": power_advantage,
            "toughness": toughness_advantage,
            "tempo": tempo_advantage,
            "life": life_advantage,
            "mana": mana_advantage,
            "walker": walker_advantage,
            "synergy": synergy_value,
            "win_condition": win_condition_value
        }
        
        weighted_sum = sum(components[k] * weights[k] for k in components)
        
        # Tanh normalization to keep in [-1, 1] range
        final_score = np.tanh(weighted_sum)
        
        # Create detailed evaluation
        evaluation = {
            "score": final_score,
            "components": components,
            "weights": weights,
            "game_stage": game_stage,
            "strategy": getattr(self, 'strategy_type', "unknown")
        }
        
        return final_score, evaluation
