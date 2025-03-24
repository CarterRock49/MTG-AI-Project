import logging
import numpy as np
import random
from collections import defaultdict
import re

class MTGStrategicPlanner:
    """Advanced strategic decision making system for Magic: The Gathering AI."""
    
    def __init__(self, game_state, card_evaluator=None, combat_resolver=None):
        self.game_state = game_state
        self.card_evaluator = card_evaluator
        self.combat_resolver = combat_resolver
        
        # Initialize strategy parameters with safe defaults
        self.aggression_level = 0.5  # 0.0 = defensive, 1.0 = all-out aggression
        self.risk_tolerance = 0.5    # 0.0 = risk averse, 1.0 = high risk
        self.strategy_type = "midrange"  # Default strategy type
        
        # Strategy types
        self.strategies = {
            "aggro": {
                "description": "Aggressive strategy: Play fast creatures and attack quickly",
                "aggression_level": 0.8,
                "risk_tolerance": 0.7,
                "card_weights": {"creature": 1.5, "instant": 0.7, "sorcery": 0.7, "artifact": 0.5, "enchantment": 0.5, "planeswalker": 1.2, "land": 0.7}
            },
            "control": {
                "description": "Control strategy: Counter spells, remove threats, win late game",
                "aggression_level": 0.2,
                "risk_tolerance": 0.3,
                "card_weights": {"creature": 0.7, "instant": 1.5, "sorcery": 1.3, "artifact": 1.0, "enchantment": 1.0, "planeswalker": 1.5, "land": 0.8}
            },
            "midrange": {
                "description": "Midrange strategy: Efficient creatures and value plays",
                "aggression_level": 0.5,
                "risk_tolerance": 0.5,
                "card_weights": {"creature": 1.2, "instant": 1.0, "sorcery": 1.0, "artifact": 0.8, "enchantment": 0.8, "planeswalker": 1.3, "land": 0.8}
            },
            "combo": {
                "description": "Combo strategy: Assemble a game-winning combination",
                "aggression_level": 0.4,
                "risk_tolerance": 0.9,
                "card_weights": {"creature": 0.8, "instant": 1.0, "sorcery": 1.2, "artifact": 1.3, "enchantment": 1.3, "planeswalker": 0.7, "land": 0.9}
            }
        }
        
        # Always initialize with a default strategy first
        self._initialize_strategy_params("midrange")
        
        # Remember the current game state analysis
        self.current_analysis = None
        self.opponent_archetype = None
    
        
        # Initialize deck archetype detection
        self._detect_deck_archetype()
        
    def init_after_reset(self):
        """
        Initialize the strategic planner after the game state has been reset 
        and p1/p2 have been established.
        """
        try:
            gs = self.game_state
            
            # Check if game state is properly initialized
            if not hasattr(gs, 'p1') or not hasattr(gs, 'p2') or not gs.p1 or not gs.p2:
                logging.debug("Cannot initialize strategic planner: game state not fully set up yet")
                # Just return silently - we already have a default strategy from __init__
                return
            
            # Ensure player states have minimum required attributes
            for player in [gs.p1, gs.p2]:
                if not all(attr in player for attr in ["hand", "battlefield", "library"]):
                    logging.debug("Player state missing required attributes, using default strategy")
                    return
            
            # Detect deck archetype with proper error handling
            try:
                archetype = self._detect_deck_archetype()
                logging.debug(f"Strategic planner initialized with deck archetype: {archetype}")
            except Exception as e:
                logging.warning(f"Error detecting deck archetype: {e}")
                import traceback
                logging.debug(traceback.format_exc())
                # Default to midrange strategy in case of errors
                self._initialize_strategy_params('midrange')
        except Exception as e:
            logging.error(f"Error initializing strategic planner: {e}")
            import traceback
            logging.error(traceback.format_exc())
            # Ensure a default strategy is always set
            self._initialize_strategy_params('midrange')

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
                creature_power = card.power
                
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
                                    sum(gs._safe_get_card(cid).toughness 
                                        for cid in opp_creatures 
                                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness')))
            
            # Calculate turns to win through combat
            if effective_power > 0:
                turns_to_win = np.ceil(opp["life"] / effective_power)
                
                # Combat damage is viable if we can win in a reasonable timeframe
                win_conditions["combat_damage"]["viable"] = turns_to_win < 10
                win_conditions["combat_damage"]["turns_to_win"] = turns_to_win
                win_conditions["combat_damage"]["score"] = min(1.0, 10 / turns_to_win)
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
                turns_to_win = np.ceil(opp["life"] / total_direct_damage) * 2  # Conservative estimate
                win_conditions["direct_damage"]["turns_to_win"] = turns_to_win
                win_conditions["direct_damage"]["score"] = min(1.0, 10 / turns_to_win)
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
            win_conditions["combo"]["score"] = pieces_have / pieces_needed
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
            observed_cards.extend([gs._safe_get_card(cid) for cid in opp[zone]])
        
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
                        creature_power_sum += card.power
                        creature_toughness_sum += card.toughness
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
                if card.cmc <= 2:
                    low_cmc_count += 1
                elif 3 <= card.cmc <= 4:
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
        
        # Normalize scores to probabilities
        total = sum(scores.values())
        archetypes = ["aggro", "control", "midrange", "combo", "tempo", "ramp"]
        probs = np.array([scores[arch] / total for arch in archetypes], dtype=np.float32)
        
        # Store opponent archetype for reference
        self.opponent_archetype = archetypes[np.argmax(probs)]
        
        logging.debug(f"Predicted opponent archetype: {self.opponent_archetype} (confidence: {np.max(probs):.2f})")
        
        return probs

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
        
        my_power = sum(gs._safe_get_card(cid).power for cid in my_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
        opp_power = sum(gs._safe_get_card(cid).power for cid in opp_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
        
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
        
    def _quick_action_evaluation(self, game_state, action_type, param):
        """Quick heuristic evaluation of an action's impact"""
        # This is a lightweight evaluation to enable pruning
        gs = game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Basic evaluation based on action type
        if action_type == "END_TURN":
            return -0.1  # Slightly negative - usually we want to do something
        elif action_type == "PLAY_CARD":
            card = gs._safe_get_card(param)
            if not card:
                return 0.0
                
            # Simple heuristic based on card type
            if hasattr(card, 'card_types'):
                if 'creature' in card.card_types:
                    return 0.5  # Playing creatures is generally good
                if 'land' in card.card_types:
                    return 0.7  # Playing lands is very good
                return 0.3  # Other card types
        elif action_type == "DECLARE_ATTACKER":
            return 0.3  # Attacking is generally good
        elif action_type == "CAST_SPELL":
            return 0.4  # Casting spells is generally good
        
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
        if not me["land_played"]:
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
        
        my_power = sum(gs._safe_get_card(cid).power for cid in my_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
        my_toughness = sum(gs._safe_get_card(cid).toughness for cid in my_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))
        
        opp_power = sum(gs._safe_get_card(cid).power for cid in opp_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
        opp_toughness = sum(gs._safe_get_card(cid).toughness for cid in opp_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))
        
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
                gs._safe_get_card(cid).power 
                for cid in my_creatures 
                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power')
            )
            
            # Account for potential blocking
            if len(opp_creatures) > 0:
                blocked_power = min(total_power, sum(gs._safe_get_card(cid).toughness for cid in opp_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness')))
                effective_power = max(0, total_power - blocked_power)
                
                # Adjust for evasion (flying, trample, etc.)
                evasive_power = sum(
                    gs._safe_get_card(cid).power 
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
        if hasattr(card, 'cmc'):
            # More sophisticated mana curve evaluation
            if card.cmc <= 2:
                mana_value += 0.5  # Early game efficiency
            elif card.cmc <= 4:
                mana_value += 0.4  # Mid-game impact
            elif card.cmc <= 6:
                mana_value += 0.3  # Late game power
            else:
                mana_value += 0.2  # Very late game bombs
            
            # Discount for uncastable cards
            mana_value *= max(0.2, 1.0 - (card.cmc * 0.05))
        
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
        if hasattr(card, 'power') and hasattr(card, 'toughness'):
            # Power to mana cost efficiency
            power_efficiency = card.power / max(1, card.cmc)
            toughness_efficiency = card.toughness / max(1, card.cmc)
            
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
                
            # Extended safety checks
            if not hasattr(me, "hand") or not hasattr(me, "battlefield") or not hasattr(me, "library"):
                logging.debug("Player state missing required attributes for deck detection")
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
                cmc = card.cmc
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
                    threat_level += card.power * 0.5
            
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
                # Create context if needed
                if context is None:
                    context = {
                        "game_stage": self.current_analysis["game_info"]["game_stage"],
                        "position": self.current_analysis["position"]["overall"],
                        "aggression_level": self.aggression_level,
                        "strategy_type": self.strategy_type,
                        "turn": gs.turn,
                        "phase": gs.phase
                    }
                
                return self.card_evaluator.evaluate_card(card_id, "play", context)
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
        if 'land' in card.card_types and not me["land_played"]:
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
    
    def evaluate_attack_action(self, attacker_ids):
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
            # Store original attackers to restore later
            original_attackers = gs.current_attackers.copy() if hasattr(gs, 'current_attackers') else []
            
            # Set up the proposed attackers
            gs.current_attackers = attacker_ids.copy()
            
            # Simulate combat
            simulation = self.combat_resolver.simulate_combat()
            
            # Restore original state
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
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Get card objects
        attacker = gs._safe_get_card(attacker_id)
        blockers = [gs._safe_get_card(bid) for bid in blocker_ids]
        blockers = [b for b in blockers if b]  # Filter out None values
        
        if not attacker or not blockers:
            return 0.0
        
        # Use combat resolver if available
        if self.combat_resolver:
            # Store original state
            original_attackers = gs.current_attackers.copy() if hasattr(gs, 'current_attackers') else []
            original_blocks = gs.current_block_assignments.copy() if hasattr(gs, 'current_block_assignments') else {}
            
            # Set up the proposed block
            gs.current_attackers = [attacker_id]
            gs.current_block_assignments = {attacker_id: blocker_ids}
            
            # Simulate just this block
            simulation = self.combat_resolver.simulate_single_block(attacker_id, blocker_ids)
            
            # Restore original state
            gs.current_attackers = original_attackers
            gs.current_block_assignments = original_blocks
            
            # Extract simulation results
            attacker_dies = simulation.get("attacker_dies", False)
            blockers_die = simulation.get("blockers_dying", [])
            damage_prevented = simulation.get("damage_prevented", 0)
            
            # Basic evaluation
            value = 0.0
            
            # Killing attacker is good
            if attacker_dies:
                value += 1.0
            
            # Losing blockers is bad
            value -= len(blockers_die) * 0.5
            
            # Preventing damage is good
            value += damage_prevented * 0.2
            
        else:
            # Fallback to simpler evaluation if no combat resolver
            # Basic combat math
            attacker_power = attacker.power if hasattr(attacker, 'power') else 0
            attacker_toughness = attacker.toughness if hasattr(attacker, 'toughness') else 0
            
            blocker_total_power = sum(b.power for b in blockers if hasattr(b, 'power'))
            blocker_total_toughness = sum(b.toughness for b in blockers if hasattr(b, 'toughness'))
            
            # Calculate expected outcomes
            attacker_dies = blocker_total_power >= attacker_toughness
            blockers_die_count = sum(1 for b in blockers 
                                if hasattr(b, 'toughness') and b.toughness <= attacker_power)
            
            # Special abilities
            attacker_has_deathtouch = hasattr(attacker, 'oracle_text') and "deathtouch" in attacker.oracle_text.lower()
            if attacker_has_deathtouch:
                blockers_die_count = len(blockers)
            
            attacker_has_trample = hasattr(attacker, 'oracle_text') and "trample" in attacker.oracle_text.lower()
            damage_prevented = attacker_power
            if attacker_has_trample:
                damage_prevented = min(attacker_power, blocker_total_toughness)
            
            # Basic block value
            value = 0.0
            
            # Killing attacker is good
            if attacker_dies:
                value += 1.0
            
            # Losing blockers is bad
            value -= blockers_die_count * 0.5
            
            # Preventing damage is good
            value += damage_prevented * 0.2
        
        # Additional strategic considerations
        
        # Ensure we have current analysis
        if not self.current_analysis:
            self.analyze_game_state()
        
        # Life total considerations
        my_life = me["life"]
        
        # If we're low on life, preventing damage is more important
        if my_life <= 5:
            value += damage_prevented * 0.5  # Extra value for damage prevention
        elif my_life <= 10:
            value += damage_prevented * 0.3  # Moderate extra value
        
        # Game stage considerations
        game_stage = self.current_analysis["game_info"]["game_stage"]
        
        if game_stage == "early":
            # Early game: preserve creatures unless good trade
            if len(blockers_die) > 0 and not attacker_dies:
                value -= 0.3  # Losing blockers for no gain is worse early
        elif game_stage == "late":
            # Late game: life becomes more valuable
            value += damage_prevented * 0.1  # Extra value for damage prevention
        
        # Defensive strategy adjustment
        value += ((1.0 - self.aggression_level) - 0.5) * 1.0  # -0.5 to +0.5 based on defense
        
        # Risk tolerance adjustment for potentially losing blockers
        if len(blockers_die) > 0:
            value += (self.risk_tolerance - 0.5) * 0.5 * len(blockers_die)  # Risk adjustment
        
        logging.debug(f"Block evaluation: {len(blockers)} blockers vs. attacker {attacker.name if hasattr(attacker, 'name') else 'unknown'}, value={value:.2f}")
        
        return 
    
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
                        power = card.power
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
        card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
        # Check if ability handler exists
        if not hasattr(gs, 'ability_handler') or not gs.ability_handler:
            return 0.0, "No ability handler available"
        
        # Check if card exists
        card = gs._safe_get_card(card_id)
        if not card:
            return -1.0, "Card not found"
        
        # Check if ability exists
        activated_abilities = gs.ability_handler.get_activated_abilities(card_id)
        if ability_idx < 0 or ability_idx >= len(activated_abilities):
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
        
        Returns:
            int: Number of simulations to run
        """
        gs = self.game_state
        
        # Base simulation count
        base_count = 100
        
        # Reduce for complex states to avoid timeouts
        battlefield_size = len(gs.p1["battlefield"]) + len(gs.p2["battlefield"])
        if battlefield_size > 15:
            base_count = 50
        elif battlefield_size > 10:
            base_count = 75
        
        # Reduce for many valid actions (combinatorial explosion)
        valid_actions = gs.action_handler.generate_valid_actions()
        action_count = np.sum(valid_actions)
        if action_count > 20:
            base_count = max(30, base_count - 50)
        
        # Increase for critical game phases
        if gs.phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS]:
            base_count = min(200, base_count + 50)
        
        # Scale based on turn (more computation for later turns)
        turn_factor = min(1.5, 1.0 + (gs.turn / 20))
        base_count = int(base_count * turn_factor)
        
        logging.debug(f"MCTS simulation count: {base_count} (board size: {battlefield_size}, actions: {action_count})")
        return base_count
    
    def recommend_action(self, valid_actions):
        """
        Provide a strategic recommendation for the next action with MCTS integration.
        
        Args:
            valid_actions: List of valid action indices
            
        Returns:
            int: Recommended action index
        """
        try:
            gs = self.game_state
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1
            
            # Handle case where no valid actions are provided
            if not valid_actions or len(valid_actions) == 0:
                logging.warning("No valid actions provided to recommend_action")
                return None
            
            # 1. Analyze current game state
            self.analyze_game_state()
            self.adapt_strategy()
            
            # 2. Check strategy memory for suggestions based on similar game states
            memory_suggestion = None
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory:
                try:
                    memory_suggestion = gs.strategy_memory.get_suggested_action(gs, valid_actions)
                    if memory_suggestion is not None and memory_suggestion in valid_actions:
                        # If we have high confidence in this suggestion based on success rate
                        pattern = gs.strategy_memory.extract_strategy_pattern(gs)
                        if pattern in gs.strategy_memory.strategies:
                            strategy = gs.strategy_memory.strategies[pattern]
                            if strategy.get('success_rate', 0) > 0.8 and strategy.get('count', 0) > 5:
                                logging.debug(f"Using high-confidence memory-suggested action: {memory_suggestion}")
                                return memory_suggestion
                        logging.debug(f"Found memory suggestion: {memory_suggestion} (will consider)")
                except Exception as e:
                    logging.warning(f"Error getting strategy memory suggestion: {str(e)}")
            
            # 3. Determine if critical decision point - use MCTS for important decisions
            is_critical_decision = self._is_critical_decision()
            
            # For critical decisions, use Monte Carlo Tree Search
            if is_critical_decision:
                logging.info("Critical decision point detected - using Monte Carlo Tree Search")
                # Adjust simulation count based on complexity
                simulation_count = self._determine_simulation_count()
                mcts_action = self.monte_carlo_search(num_simulations=simulation_count)
                
                if mcts_action in valid_actions:
                    return mcts_action
                else:
                    logging.warning(f"MCTS selected invalid action {mcts_action}. Falling back to heuristic approach.")
            
            # 4. Action prioritization (for non-critical decisions or MCTS fallback)
            action_priorities = []
            
            # Check for land plays if we haven't played a land yet
            if not me["land_played"]:
                land_plays = []
                for action_idx in valid_actions:
                    action_type, param = gs.action_handler.get_action_info(action_idx)
                    if action_type == "PLAY_CARD":
                        card = gs._safe_get_card(param)
                        if card and hasattr(card, 'type_line') and 'land' in card.type_line:
                            land_plays.append(action_idx)
                
                if land_plays:
                    # Prioritize land play
                    logging.debug("Prioritizing land play")
                    return land_plays[0]  # Just play the first available land
            
            # High priority actions
            high_priority_actions = []
            
            # Check for potential lethal damage
            opp_life = self.current_analysis["life"]["opp_life"]
            
            for action_idx in valid_actions:
                action_type, param = gs.action_handler.get_action_info(action_idx)
                
                # Evaluate attacking if it could be lethal
                if action_type == "DECLARE_ATTACKER" and param:
                    attack_value = self.evaluate_attack_action(param)
                    
                    # If the attack might be lethal, prioritize it
                    my_creatures = [cid for cid in me["battlefield"] 
                                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') and 'creature' in gs._safe_get_card(cid).card_types]
                    total_power = sum(gs._safe_get_card(cid).power for cid in my_creatures 
                                    if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
                    
                    if total_power >= opp_life:
                        high_priority_actions.append((action_idx, attack_value + 2.0, "Potential lethal attack"))
                
                # Deal with urgent threats
                if action_type in ["PLAY_CARD", "ACTIVATE_ABILITY"]:
                    # Get threats from current analysis
                    threats = self.assess_threats()[:3]  # Top 3 threats
                    
                    if threats and threats[0]:
                        top_threat = threats[0]
                        top_threat_id = top_threat.get("card_id") if isinstance(top_threat, dict) else top_threat
                        
                        # Check if this action could remove the threat
                        can_remove = False
                        
                        if action_type == "PLAY_CARD":
                            card = gs._safe_get_card(param)
                            if card and hasattr(card, 'oracle_text'):
                                text = card.oracle_text.lower()
                                if any(term in text for term in ['destroy', 'exile', 'damage', 'return target']):
                                    can_remove = True
                        
                        elif action_type == "ACTIVATE_ABILITY":
                            card_id, ability_idx = param
                            if hasattr(gs, 'ability_handler') and gs.ability_handler:
                                abilities = gs.ability_handler.get_activated_abilities(card_id)
                                if ability_idx < len(abilities):
                                    ability = abilities[ability_idx]
                                    if hasattr(ability, 'effect'):
                                        text = ability.effect.lower()
                                        if any(term in text for term in ['destroy', 'exile', 'damage', 'return target']):
                                            can_remove = True
                        
                        if can_remove:
                            threat_level = top_threat.get('level', 1.0) if isinstance(top_threat, dict) else 1.0
                            high_priority_actions.append((action_idx, 1.0 + threat_level * 0.5, "Remove threat"))
            
            # If we have high priority actions, choose the best one
            if high_priority_actions:
                high_priority_actions.sort(key=lambda x: x[1], reverse=True)
                logging.debug(f"Taking high priority action: {high_priority_actions[0][2]}")
                return high_priority_actions[0][0]
            
            # 5. If memory suggested an action and it's valid, consider it now
            if memory_suggestion is not None and memory_suggestion in valid_actions:
                pattern = gs.strategy_memory.extract_strategy_pattern(gs)
                if pattern in gs.strategy_memory.strategies:
                    strategy = gs.strategy_memory.strategies[pattern]
                    if strategy.get('success_rate', 0) > 0.6:  # More lenient success threshold
                        logging.debug(f"Using memory-suggested action: {memory_suggestion}")
                        return memory_suggestion
            
            # 6. Find best play sequence using forward search
            best_sequence, best_value = self.find_best_play_sequence(valid_actions, depth=2)
            if best_sequence:
                logging.debug(f"Best play sequence found with value {best_value}")
                return best_sequence[0]
            
            # 7. If no good sequence found, evaluate individual actions
            action_evaluations = []
            
            for action_idx in valid_actions:
                action_type, param = gs.action_handler.get_action_info(action_idx)
                
                # Evaluate based on action type
                if action_type == "PLAY_CARD":
                    value = self.evaluate_play_card_action(param)
                    action_evaluations.append((action_idx, value, "Card play"))
                
                elif action_type == "DECLARE_ATTACKER":
                    value = self.evaluate_attack_action(param)
                    action_evaluations.append((action_idx, value, "Attack"))
                
                elif action_type == "DECLARE_BLOCKER":
                    # Parse the blocker assignments
                    attacker_id, blocker_ids = param
                    value = self.evaluate_block_action(attacker_id, blocker_ids)
                    action_evaluations.append((action_idx, value, "Block"))
                
                elif action_type == "ACTIVATE_ABILITY":
                    card_id, ability_idx = param
                    value, reasoning = self.evaluate_ability_activation(card_id, ability_idx)
                    action_evaluations.append((action_idx, value, f"Ability: {reasoning}"))
                
                elif action_type == "END_TURN":
                    # End turn is a fallback action - give it a low value
                    action_evaluations.append((action_idx, 0.1, "End turn"))
                
                else:
                    # Default evaluation for other actions
                    action_evaluations.append((action_idx, 0.5, "Other action"))
            
            # Sort by value
            action_evaluations.sort(key=lambda x: x[1], reverse=True)
            
            # 8. Apply exploration factor based on risk tolerance
            if random.random() < self.risk_tolerance * 0.2:
                # Sometimes pick a suboptimal action to explore
                exploration_candidates = action_evaluations[:min(3, len(action_evaluations))]
                chosen_action, value, reason = random.choice(exploration_candidates)
                logging.debug(f"Exploration choice: {reason} (value={value:.2f})")
                return chosen_action
            
            # 9. Choose the best action
            if action_evaluations:
                best_action, value, reason = action_evaluations[0]
                logging.debug(f"Best action: {reason} (value={value:.2f})")
                
                # Update strategy memory with the chosen action
                if hasattr(gs, 'strategy_memory') and gs.strategy_memory:
                    try:
                        current_pattern = gs.strategy_memory.extract_strategy_pattern(gs)
                        gs.strategy_memory.update_strategy(current_pattern, value)
                    except Exception as e:
                        logging.warning(f"Error updating strategy memory: {str(e)}")
                
                return best_action
            
            # 10. Fallback to first valid action
            logging.debug("No good actions found, using first valid action")
            return valid_actions[0]
            
        except Exception as e:
            logging.error(f"Error in recommend_action: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            
            # Fallback strategy
            if valid_actions and len(valid_actions) > 0:
                # Try to find END_TURN action
                for action_idx in valid_actions:
                    try:
                        action_type, _ = gs.action_handler.get_action_info(action_idx)
                        if action_type == "END_TURN":
                            logging.debug("Fallback to END_TURN action after error")
                            return action_idx
                    except:
                        pass
                
                # If no END_TURN found, use first valid action
                logging.debug("Fallback to first valid action after error")
                return valid_actions[0]
            
            # No valid actions available
            logging.warning("No valid actions available for fallback")
            return None

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
        
        Args:
            num_simulations: Number of simulations to run
            exploration_weight: Weight for exploration in UCB formula
            
        Returns:
            Best action found by MCTS
        """
        gs = self.game_state
        
        # Create root node
        root = MCTSNode(game_state=gs.clone())
        
        # Get valid actions
        valid_actions = gs.action_handler.generate_valid_actions()
        valid_actions = np.where(valid_actions)[0].tolist()
        
        # Use strategic evaluation to initialize action priors
        action_priors = {}
        for action in valid_actions:
            action_type, param = gs.action_handler.get_action_info(action)
            
            # Get basic evaluation based on action type
            if action_type == "PLAY_CARD":
                value = self.evaluate_play_card_action(param)
            elif action_type == "DECLARE_ATTACKER":
                value = self.evaluate_attack_action(param)
            elif action_type == "ACTIVATE_ABILITY":
                card_id, ability_idx = param
                value, _ = self.evaluate_ability_activation(card_id, ability_idx)
            else:
                value = self._quick_action_evaluation(gs, action_type, param)
                
            # Convert to probability (softmax)
            action_priors[action] = max(0.01, value)  # Ensure non-zero probability
        
        # Normalize priors
        total = sum(action_priors.values())
        if total > 0:
            for action in action_priors:
                action_priors[action] /= total
        
        # Expand root with valid actions and priors
        root.expand(valid_actions, action_priors)
        
        # Run simulations
        for i in range(num_simulations):
            # Phase 1: Selection - traverse tree until unexpanded node
            node = root
            search_path = [node]
            
            # Clone game state for simulation
            sim_gs = gs.clone()
            
            # Select actions until we reach a leaf node
            while node.is_expanded and node.children:
                action, node = node.select_child(c_puct=exploration_weight)
                search_path.append(node)
                
                # Apply action in simulation
                action_type, param = sim_gs.action_handler.get_action_info(action)
                sim_gs.action_handler.apply_action(action_type, param)
            
            # Phase 2: Expansion - add new nodes if game not terminal
            # Check if game is done
            game_over = sim_gs.p1["life"] <= 0 or sim_gs.p2["life"] <= 0 or sim_gs.turn > gs.max_turns
            
            if not game_over:
                # Get valid actions in this state
                sim_valid_actions = sim_gs.action_handler.generate_valid_actions()
                sim_valid_actions = np.where(sim_valid_actions)[0].tolist()
                
                # Expand node
                node.expand(sim_valid_actions)
                
                # Phase 3: Simulation (rollout)
                value = self._rollout(sim_gs)
            else:
                # Game is over, evaluate final state
                me = sim_gs.p1 if sim_gs.agent_is_p1 else sim_gs.p2
                opp = sim_gs.p2 if sim_gs.agent_is_p1 else sim_gs.p1
                
                if me["life"] <= 0:
                    value = -1.0  # Loss
                elif opp["life"] <= 0:
                    value = 1.0  # Win
                else:
                    # Draw or turn limit reached - slight advantage to higher life
                    value = 0.1 * np.sign(me["life"] - opp["life"])
            
            # Phase 4: Backpropagation - update values up the tree
            for node in reversed(search_path):
                node.visit_count += 1
                node.value_sum += value
                value = -value  # Negate for alternating levels (assumes perfect play by opponent)
        
        # Return best action based on visit count
        max_visit_count = -1
        best_action = None
        
        for action, child in root.children.items():
            if child.visit_count > max_visit_count:
                max_visit_count = child.visit_count
                best_action = action
        
        logging.debug(f"MCTS selected action {best_action} with {max_visit_count} visits")
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
        
        # Simulate random/strategic actions until game ends or step limit
        for _ in range(max_steps):
            # Check if game is done
            if sim_gs.p1["life"] <= 0 or sim_gs.p2["life"] <= 0 or sim_gs.turn > sim_gs.max_turns:
                break
            
            # Get valid actions
            valid_actions = sim_gs.action_handler.generate_valid_actions()
            valid_actions = np.where(valid_actions)[0].tolist()
            
            if not valid_actions:
                break
            
            # Use strategic rollout policy
            action = self._rollout_policy(sim_gs, valid_actions)
            
            # Apply action
            action_type, param = sim_gs.action_handler.get_action_info(action)
            sim_gs.action_handler.apply_action(action_type, param)
        
        # Evaluate final state
        # Base eval on life totals, board state, and other factors
        if me["life"] <= 0:
            return -1.0  # Loss
        elif opp["life"] <= 0:
            return 1.0  # Win
        else:
            # Compute a heuristic value
            my_creatures = [cid for cid in me["battlefield"] 
                        if sim_gs._safe_get_card(cid) and 
                        hasattr(sim_gs._safe_get_card(cid), 'card_types') and 
                        'creature' in sim_gs._safe_get_card(cid).card_types]
            
            opp_creatures = [cid for cid in opp["battlefield"] 
                        if sim_gs._safe_get_card(cid) and 
                        hasattr(sim_gs._safe_get_card(cid), 'card_types') and 
                        'creature' in sim_gs._safe_get_card(cid).card_types]
            
            my_power = sum(sim_gs._safe_get_card(cid).power for cid in my_creatures 
                        if sim_gs._safe_get_card(cid) and hasattr(sim_gs._safe_get_card(cid), 'power'))
            
            opp_power = sum(sim_gs._safe_get_card(cid).power for cid in opp_creatures 
                        if sim_gs._safe_get_card(cid) and hasattr(sim_gs._safe_get_card(cid), 'power'))
            
            life_diff = me["life"] - opp["life"]
            card_diff = len(me["hand"]) - len(opp["hand"])
            board_diff = len(me["battlefield"]) - len(opp["battlefield"])
            power_diff = my_power - opp_power
            
            # Combine factors - weight can be adjusted based on strategic importance
            value = (
                0.4 * np.tanh(life_diff / 10) +
                0.2 * np.tanh(card_diff / 2) +
                0.2 * np.tanh(board_diff / 4) +
                0.2 * np.tanh(power_diff / 5)
            )
            
            return value

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
                if card and hasattr(card, 'type_line') and 'land' in card.type_line and not me["land_played"]:
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