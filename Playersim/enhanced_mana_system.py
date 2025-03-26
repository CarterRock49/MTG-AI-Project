import re
import logging
from collections import Counter
from collections import defaultdict
class EnhancedManaSystem:
    """Advanced mana handling system that properly implements MTG mana rules."""
    
    def __init__(self, game_state):
        self.game_state = game_state
        self.mana_symbols = {'W', 'U', 'B', 'R', 'G', 'C'}
        self.color_names = {
            'W': 'white',
            'U': 'blue',
            'B': 'black',
            'R': 'red',
            'G': 'green',
            'C': 'colorless'
        }
        
        # FIXED: Add lowercase variants with explicit assignment
        self.lowercase_symbols = {'w', 'u', 'b', 'r', 'g', 'c', 't'} 
        # Add tap symbol to a separate set of special symbols
        self.special_symbols = {'t'}  # The tap symbol needs special handling 
        
    def track_snow_sources(self, player):
        """
        Track snow permanents that can produce snow mana.
        
        Args:
            player: The player dictionary
            
        Returns:
            int: Number of available snow mana sources
        """
        gs = self.game_state
        
        # Count snow permanents that can produce mana
        snow_sources = 0
        
        for card_id in player["battlefield"]:
            card = gs._safe_get_card(card_id)
            
            # Skip if card doesn't exist or is tapped
            if not card or card_id in player["tapped_permanents"]:
                continue
                
            # Check if it's a snow permanent that can produce mana
            if hasattr(card, 'type_line') and 'snow' in card.type_line.lower():
                # Check if it's a land or has a mana ability
                if 'land' in card.type_line.lower():
                    snow_sources += 1
                elif hasattr(card, 'oracle_text') and ('add' in card.oracle_text.lower() and 
                                                    any(f"{{{c}}}" in card.oracle_text.lower() 
                                                        for c in ['w', 'u', 'b', 'r', 'g', 'c'])):
                    snow_sources += 1
        
        return snow_sources

    def can_pay_snow_cost(self, player, snow_cost):
        """
        Check if a player can pay a snow mana cost.
        
        Args:
            player: The player dictionary
            snow_cost: Number of snow mana required
            
        Returns:
            bool: Whether snow cost can be paid
        """
        if snow_cost <= 0:
            return True
            
        # Count available snow sources
        available_snow = self.track_snow_sources(player)
        
        # Check if player has enough snow mana sources
        return available_snow >= snow_cost

    def pay_snow_cost(self, player, snow_cost):
        """
        Pay a snow mana cost.
        
        Args:
            player: The player dictionary
            snow_cost: Number of snow mana required
            
        Returns:
            bool: Whether payment was successful
        """
        if snow_cost <= 0:
            return True
            
        # Check if cost can be paid
        if not self.can_pay_snow_cost(player, snow_cost):
            return False
        
        # Find snow permanents to tap
        gs = self.game_state
        snow_sources_tapped = 0
        
        for card_id in player["battlefield"]:
            if snow_sources_tapped >= snow_cost:
                break
                
            card = gs._safe_get_card(card_id)
            
            # Skip if card doesn't exist or is already tapped
            if not card or card_id in player["tapped_permanents"]:
                continue
                
            # Check if it's a snow permanent that can produce mana
            if hasattr(card, 'type_line') and 'snow' in card.type_line.lower():
                # Check if it's a land or has a mana ability
                if 'land' in card.type_line.lower() or (hasattr(card, 'oracle_text') and 
                                                    'add' in card.oracle_text.lower()):
                    # Tap this permanent
                    player["tapped_permanents"].append(card_id)
                    snow_sources_tapped += 1
                    
                    # Add appropriate mana to pool
                    if hasattr(card, 'oracle_text'):
                        # Look for mana production text to determine what color to add
                        oracle_text = card.oracle_text.lower()
                        for color in ['w', 'u', 'b', 'r', 'g']:
                            if f"{{{color}}}" in oracle_text:
                                player["mana_pool"][color.upper()] += 1
                                break
                        else:
                            # Default to colorless if no specific color found
                            player["mana_pool"]["C"] += 1
                    else:
                        # Default to colorless if no oracle text
                        player["mana_pool"]["C"] += 1
        
        return snow_sources_tapped >= snow_cost
    
    def _gather_cost_modification_effects(self, player, card, context=None):
        """
        Gather all cost modification effects from permanents on the battlefield.
        
        Args:
            player: The player dictionary
            card: The card being cast
            context: Optional context for special cases
            
        Returns:
            list: List of cost modification effect dictionaries
        """
        gs = self.game_state
        effects = []
        
        # Check for effects from player's own permanents
        for permanent_id in player["battlefield"]:
            perm_effects = self._get_cost_effects_from_permanent(permanent_id, player, card, True)
            effects.extend(perm_effects)
        
        # Check for effects from opponent's permanents
        opponent = gs.p2 if player == gs.p1 else gs.p1
        for permanent_id in opponent["battlefield"]:
            perm_effects = self._get_cost_effects_from_permanent(permanent_id, opponent, card, False)
            effects.extend(perm_effects)
        
        # Add effects from card itself (e.g., affinity, convoke)
        self_effects = self._get_self_cost_modification_effects(card, player, context)
        effects.extend(self_effects)
        
        return effects

    def _get_cost_effects_from_permanent(self, permanent_id, controller, target_card, is_controller):
        """
        Extract cost modification effects from a permanent.
        
        Args:
            permanent_id: ID of the permanent to check
            controller: The player who controls the permanent
            target_card: The card whose cost might be modified
            is_controller: Whether the permanent's controller is casting the spell
            
        Returns:
            list: List of cost modification effect dictionaries
        """
        gs = self.game_state
        permanent = gs._safe_get_card(permanent_id)
        effects = []
        
        if not permanent or not hasattr(permanent, 'oracle_text'):
            return effects
        
        oracle_text = permanent.oracle_text.lower()
        target_card_types = target_card.card_types if hasattr(target_card, 'card_types') else []
        target_card_subtypes = target_card.subtypes if hasattr(target_card, 'subtypes') else []
        target_card_colors = target_card.colors if hasattr(target_card, 'colors') else [0, 0, 0, 0, 0]
        
        # Get permanent name for logging
        perm_name = permanent.name if hasattr(permanent, 'name') else f"Card {permanent_id}"
        
        # =========================================================================
        # Part 1: Handle standard cost reduction effects
        # =========================================================================
        
        # Check for cost reduction effects that apply to controller's spells
        if is_controller and "spells you cast cost" in oracle_text and "less" in oracle_text:
            # Parse amount of reduction
            import re
            match = re.search(r"cost \{(\d+)\} less", oracle_text)
            if match:
                reduction = int(match.group(1))
                
                # Check for targeting restrictions
                applies = True
                if "creature spells" in oracle_text and 'creature' not in target_card_types:
                    applies = False
                elif "artifact spells" in oracle_text and 'artifact' not in target_card_types:
                    applies = False
                elif "instant spells" in oracle_text and 'instant' not in target_card_types:
                    applies = False
                elif "sorcery spells" in oracle_text and 'sorcery' not in target_card_types:
                    applies = False
                elif "enchantment spells" in oracle_text and 'enchantment' not in target_card_types:
                    applies = False
                elif "planeswalker spells" in oracle_text and 'planeswalker' not in target_card_types:
                    applies = False
                
                # Color restrictions
                color_words = ["white", "blue", "black", "red", "green"]
                for i, color in enumerate(color_words):
                    if f"{color} spells" in oracle_text and not target_card_colors[i]:
                        applies = False
                
                # Subtype restrictions
                for subtype in ["spirit", "goblin", "elf", "wizard", "dragon", "zombie", "vampire", "merfolk", "angel"]:
                    if f"{subtype} spells" in oracle_text and subtype not in target_card_subtypes:
                        applies = False
                
                if applies:
                    effects.append({
                        'type': 'reduction',
                        'amount': reduction,
                        'applies_to': 'generic',
                        'source': perm_name
                    })
                    logging.debug(f"Found cost reduction of {reduction} from {perm_name}")
        
        # =========================================================================
        # Part 2: Handle standard cost increase effects
        # =========================================================================
        
        # Check for cost increase effects
        if "spells cost" in oracle_text and "more" in oracle_text:
            # Parse amount of increase
            import re
            match = re.search(r"cost \{(\d+)\} more", oracle_text)
            if match:
                increase = int(match.group(1))
                
                # Check for targeting restrictions
                applies = True
                
                # Check if it applies to opponent's spells only
                if "spells your opponents cast" in oracle_text and is_controller:
                    applies = False
                
                # Check for specific spell types
                if "creature spells" in oracle_text and 'creature' not in target_card_types:
                    applies = False
                elif "noncreature spells" in oracle_text and 'creature' in target_card_types:
                    applies = False
                elif "artifact spells" in oracle_text and 'artifact' not in target_card_types:
                    applies = False
                elif "instant spells" in oracle_text and 'instant' not in target_card_types:
                    applies = False
                elif "sorcery spells" in oracle_text and 'sorcery' not in target_card_types:
                    applies = False
                elif "enchantment spells" in oracle_text and 'enchantment' not in target_card_types:
                    applies = False
                elif "planeswalker spells" in oracle_text and 'planeswalker' not in target_card_types:
                    applies = False
                
                # Color restrictions
                color_words = ["white", "blue", "black", "red", "green"]
                for i, color in enumerate(color_words):
                    if f"{color} spells" in oracle_text and not target_card_colors[i]:
                        applies = False
                
                # Subtype restrictions
                for subtype in ["spirit", "goblin", "elf", "wizard", "dragon", "zombie", "vampire", "merfolk", "angel"]:
                    if f"{subtype} spells" in oracle_text and subtype not in target_card_subtypes:
                        applies = False
                
                if applies:
                    effects.append({
                        'type': 'increase',
                        'amount': increase,
                        'applies_to': 'generic',
                        'source': perm_name
                    })
                    logging.debug(f"Found cost increase of {increase} from {perm_name}")
        
        # =========================================================================
        # Part 3: Handle targeting-specific cost increases
        # =========================================================================
        
        # Spells that target specific permanents cost more
        if "spells that target" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
            target_context = None
            if hasattr(target_card, 'targeting') and target_card.targeting:
                target_context = target_card.targeting
            elif hasattr(gs, 'current_spell_targets') and gs.current_spell_targets:
                target_context = gs.current_spell_targets
            
            if target_context:
                # Parse amount of increase
                import re
                match = re.search(r"cost \{(\d+)\} more", oracle_text)
                if match:
                    increase = int(match.group(1))
                    applies = False
                    
                    # Check if the spell targets specific permanent types
                    target_patterns = [
                        ("you or a permanent you control", lambda tid: controller == gs._find_card_controller(tid)),
                        ("a creature", lambda tid: gs._safe_get_card(tid) and hasattr(gs._safe_get_card(tid), 'card_types') and 'creature' in gs._safe_get_card(tid).card_types),
                        ("a permanent", lambda tid: gs._safe_get_card(tid) and hasattr(gs._safe_get_card(tid), 'card_types') and any(t in gs._safe_get_card(tid).card_types for t in ['creature', 'artifact', 'enchantment', 'planeswalker', 'land']))
                    ]
                    
                    for pattern, check_func in target_patterns:
                        if pattern in oracle_text:
                            for target_id in target_context:
                                if check_func(target_id):
                                    applies = True
                                    break
                    
                    if applies:
                        effects.append({
                            'type': 'increase',
                            'amount': increase,
                            'applies_to': 'generic',
                            'source': perm_name,
                            'reason': 'targeting'
                        })
                        logging.debug(f"Found targeting cost increase of {increase} from {perm_name}")
        
        # =========================================================================
        # Part 4: Handle dynamic cost reductions (based on game state)
        # =========================================================================
        
        # Cards that reduce cost based on number of specific permanents
        dynamic_patterns = [
            # Format: (pattern, get_count_function, controller_only)
            (r"costs \{(\d+)\} less to cast for each (\w+) you control", 
            lambda ptype: sum(1 for cid in controller["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            ptype in gs._safe_get_card(cid).card_types), 
            True),
            
            # Cost reductions based on graveyard
            (r"costs \{(\d+)\} less to cast for each card in your graveyard", 
            lambda _: len(controller["graveyard"]), 
            True),
            
            # Cost reductions based on specific card types in graveyard
            (r"costs \{(\d+)\} less to cast for each (\w+) card in your graveyard", 
            lambda ptype: sum(1 for cid in controller["graveyard"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            ptype in gs._safe_get_card(cid).card_types), 
            True)
        ]
        
        for pattern, count_func, controller_check in dynamic_patterns:
            if controller_check and not is_controller:
                continue  # Skip if effect should only apply to controller's spells
                
            import re
            match = re.search(pattern, oracle_text)
            if match:
                reduction_per_item = int(match.group(1))
                item_type = match.group(2) if len(match.groups()) > 1 else None
                
                # Calculate the count of relevant items
                count = count_func(item_type) if item_type else count_func(None)
                total_reduction = reduction_per_item * count
                
                if total_reduction > 0:
                    effects.append({
                        'type': 'reduction',
                        'amount': total_reduction,
                        'applies_to': 'generic',
                        'source': perm_name,
                        'dynamic': True,
                        'reason': f"based on {count} {item_type or 'items'}"
                    })
                    logging.debug(f"Found dynamic cost reduction of {total_reduction} from {perm_name} ({count} {item_type or 'items'})")
        
        # =========================================================================
        # Part 5: Handle cost reductions for specific colors of mana
        # =========================================================================
        
        # Reduce specific colors of mana in costs
        color_matches = {
            "white": ('W', 0),
            "blue": ('U', 1),
            "black": ('B', 2),
            "red": ('R', 3),
            "green": ('G', 4)
        }
        
        for color_word, (color_symbol, color_idx) in color_matches.items():
            if f"{color_word} mana" in oracle_text and "costs" in oracle_text and "less" in oracle_text:
                # Check if spell has that color in its cost
                has_color = False
                if hasattr(target_card, 'mana_cost'):
                    cost = self.parse_mana_cost(target_card.mana_cost)
                    has_color = cost[color_symbol] > 0
                
                # Also check card color
                if hasattr(target_card, 'colors') and target_card.colors[color_idx]:
                    has_color = True
                    
                if has_color:
                    # Parse amount of reduction
                    import re
                    match = re.search(r"\{(\d+)\} less", oracle_text)
                    if match:
                        reduction = int(match.group(1))
                        
                        effects.append({
                            'type': 'reduction',
                            'amount': reduction,
                            'applies_to': 'specific_color',
                            'color': color_symbol,
                            'source': perm_name
                        })
                        logging.debug(f"Found {color_word} mana cost reduction of {reduction} from {perm_name}")
        
        return effects

    def _get_self_cost_modification_effects(self, card, player, context=None):
        """
        Get cost modification effects from the card itself.
        
        Args:
            card: The card being cast
            player: The player casting the card
            context: Optional context for special cases
            
        Returns:
            list: List of cost modification effect dictionaries
        """
        effects = []
        if not card or not hasattr(card, 'oracle_text'):
            return effects
        
        oracle_text = card.oracle_text.lower()
        
        # Check for affinity
        if "affinity for artifacts" in oracle_text:
            artifact_count = sum(1 for cid in player["battlefield"] 
                            if self.game_state._safe_get_card(cid) and 
                            hasattr(self.game_state._safe_get_card(cid), 'card_types') and 
                            'artifact' in self.game_state._safe_get_card(cid).card_types)
            
            if artifact_count > 0:
                effects.append({
                    'type': 'reduction',
                    'amount': artifact_count,
                    'applies_to': 'generic'
                })
        
        # Check for convoke
        if "convoke" in oracle_text and context and "convoke_creatures" in context:
            convoke_creatures = context["convoke_creatures"]
            if isinstance(convoke_creatures, list):
                effects.append({
                    'type': 'reduction',
                    'amount': len(convoke_creatures),
                    'applies_to': 'generic'
                })
        
        # Check for delve
        if "delve" in oracle_text and context and "delve_cards" in context:
            delve_cards = context["delve_cards"]
            if isinstance(delve_cards, list):
                effects.append({
                    'type': 'reduction',
                    'amount': len(delve_cards),
                    'applies_to': 'generic'
                })
        
        # Add more self-cost modifications as needed
        
        return effects

    def _apply_cost_effect(self, cost, effect):
        """
        Apply a cost modification effect to a cost.
        
        Args:
            cost: The cost dictionary to modify
            effect: The effect to apply
            
        Returns:
            dict: The modified cost
        """
        modified_cost = cost.copy()
        
        if effect['applies_to'] == 'generic':
            if effect['type'] == 'reduction':
                modified_cost['generic'] = max(0, modified_cost['generic'] - effect['amount'])
                logging.debug(f"Reducing generic cost by {effect['amount']} from {effect.get('source', 'unknown')}")
            elif effect['type'] == 'increase':
                modified_cost['generic'] += effect['amount']
                logging.debug(f"Increasing generic cost by {effect['amount']} from {effect.get('source', 'unknown')}")
        elif effect['applies_to'] == 'specific_color' and 'color' in effect:
            color = effect['color']
            if color in modified_cost:
                if effect['type'] == 'reduction':
                    modified_cost[color] = max(0, modified_cost[color] - effect['amount'])
                    logging.debug(f"Reducing {color} cost by {effect['amount']} from {effect.get('source', 'unknown')}")
                elif effect['type'] == 'increase':
                    modified_cost[color] += effect['amount']
                    logging.debug(f"Increasing {color} cost by {effect['amount']} from {effect.get('source', 'unknown')}")
        
        return modified_cost
        
    def apply_cost_modifiers(self, player, cost, card_id, context=None):
        """Apply cost modifiers, now accepting context."""
        gs = self.game_state
        card = gs._safe_get_card(card_id) if card_id else None # Handle cases where card_id is None

        modified_cost = cost.copy()
        applied_modifications = {'reductions': [], 'increases': []}

        # Get effects based on the card being cast (or generic if no card)
        cost_effects = self._gather_cost_modification_effects(player, card, context)

        # Apply reductions first
        for effect in [e for e in cost_effects if e['type'] == 'reduction']:
             original_cost_values = modified_cost.copy() # Copy before applying effect
             modified_cost = self._apply_cost_effect(modified_cost, effect)
             # Track change
             change_amount = 0
             if effect['applies_to'] == 'generic':
                 change_amount = original_cost_values['generic'] - modified_cost['generic']
             elif effect['applies_to'] == 'specific_color':
                 color = effect['color']
                 change_amount = original_cost_values.get(color,0) - modified_cost.get(color,0)
             if change_amount > 0:
                  applied_modifications['reductions'].append({ 'amount': change_amount, 'source': effect.get('source', 'unknown'), 'type': effect['applies_to']})

        # Apply increases next
        for effect in [e for e in cost_effects if e['type'] == 'increase']:
             original_cost_values = modified_cost.copy()
             modified_cost = self._apply_cost_effect(modified_cost, effect)
             # Track change
             change_amount = 0
             if effect['applies_to'] == 'generic':
                 change_amount = modified_cost['generic'] - original_cost_values['generic']
             elif effect['applies_to'] == 'specific_color':
                 color = effect['color']
                 change_amount = modified_cost.get(color,0) - original_cost_values.get(color,0)
             if change_amount > 0:
                  applied_modifications['increases'].append({ 'amount': change_amount, 'source': effect.get('source', 'unknown'), 'type': effect['applies_to']})

        # Apply context-based reductions (Convoke, Delve, Improvise)
        # These modify the *cost itself* before payment check
        if context:
            # Convoke: Reduce generic and colored based on tapped creatures
            if context.get("convoke_creatures") and card and "convoke" in getattr(card, 'oracle_text', '').lower():
                 convoke_creatures = context["convoke_creatures"]
                 # ManaSystem should provide creature colors
                 # Simple version: Reduce generic by count
                 convoke_reduction = len(convoke_creatures)
                 original_generic = modified_cost['generic']
                 modified_cost['generic'] = max(0, modified_cost['generic'] - convoke_reduction)
                 applied_amount = original_generic - modified_cost['generic']
                 if applied_amount > 0: applied_modifications['reductions'].append({'amount': applied_amount, 'source': 'Convoke', 'type': 'generic'})

            # Delve: Reduce generic based on exiled cards
            if context.get("delve_cards") and card and "delve" in getattr(card, 'oracle_text', '').lower():
                 delve_reduction = len(context["delve_cards"])
                 original_generic = modified_cost['generic']
                 modified_cost['generic'] = max(0, modified_cost['generic'] - delve_reduction)
                 applied_amount = original_generic - modified_cost['generic']
                 if applied_amount > 0: applied_modifications['reductions'].append({'amount': applied_amount, 'source': 'Delve', 'type': 'generic'})

            # Improvise: Reduce generic based on tapped artifacts
            if context.get("improvise_artifacts") and card and "improvise" in getattr(card, 'oracle_text', '').lower():
                improvise_reduction = len(context["improvise_artifacts"])
                original_generic = modified_cost['generic']
                modified_cost['generic'] = max(0, modified_cost['generic'] - improvise_reduction)
                applied_amount = original_generic - modified_cost['generic']
                if applied_amount > 0: applied_modifications['reductions'].append({'amount': applied_amount, 'source': 'Improvise', 'type': 'generic'})

        # Apply minimum cost effects last
        final_cost = self.apply_minimum_cost_effects(player, modified_cost, card_id, context)

        # Log changes if significant
        if applied_modifications['reductions'] or applied_modifications['increases']:
            reduction_str = ", ".join([f"{mod['amount']} {mod['type']} from {mod['source']}" for mod in applied_modifications['reductions']])
            increase_str = ", ".join([f"{mod['amount']} {mod['type']} from {mod['source']}" for mod in applied_modifications['increases']])
            logging.debug(f"Cost modifiers applied to {getattr(card,'name','spell')}: Reductions=[{reduction_str}], Increases=[{increase_str}] -> Final Cost: {self._format_mana_cost_for_logging(final_cost)}")

        # Store applied mods in context if provided
        if context is not None:
             context['applied_cost_modifications'] = applied_modifications

        return final_cost
    
    def _get_kicker_cost(self, card):
        """
        Extract the kicker cost from a card's oracle text.
        
        Args:
            card: The card object
            
        Returns:
            dict: Parsed kicker cost dictionary or None if not found
        """
        if not card or not hasattr(card, 'oracle_text'):
            return None
            
        oracle_text = card.oracle_text.lower()
        
        # Check if card has kicker
        if "kicker" not in oracle_text:
            return None
            
        # Parse kicker cost
        import re
        match = re.search(r"kicker [^\(]([^\)]+)", oracle_text)
        if not match:
            return None
            
        kicker_cost = match.group(1).strip()
        return self.parse_mana_cost(kicker_cost)

    def calculate_cost_increase(self, player, cost, card_id, context=None):
        """
        Calculate cost increasing effects that apply to a card.
        
        Args:
            player: The player dictionary
            cost: The parsed mana cost dictionary
            card_id: ID of the card being cast
            context: Optional context for special cases
            
        Returns:
            dict: The increased cost dictionary
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return cost
        
        increased_cost = cost.copy()
        
        # Check for cost increasing effects on the battlefield for all players
        for player_idx, p in enumerate([gs.p1, gs.p2]):
            is_opponent = (player != p)
            
            for battlefield_id in p["battlefield"]:
                battlefield_card = gs._safe_get_card(battlefield_id)
                if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                    continue
                    
                oracle_text = battlefield_card.oracle_text.lower()
                
                # Tax effects like "Spells cost {1} more to cast"
                if "spells cost" in oracle_text and "more to cast" in oracle_text:
                    # Extract amount of increase
                    import re
                    match = re.search(r"cost \{(\d+)\} more", oracle_text)
                    if match:
                        increase = int(match.group(1))
                        increased_cost["generic"] += increase
                        logging.debug(f"Applying generic cost increase of {increase} from {battlefield_card.name}")
                
                # Color-specific tax effects
                for color, symbol in zip(['white', 'blue', 'black', 'red', 'green'], ['W', 'U', 'B', 'R', 'G']):
                    if f"{color} spells" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        # Check if spell is the right color
                        if hasattr(card, 'colors') and card.colors[list('WUBRG').index(symbol)]:
                            match = re.search(r"cost \{(\d+)\} more", oracle_text)
                            if match:
                                increase = int(match.group(1))
                                increased_cost["generic"] += increase
                                logging.debug(f"Applying {color} spell cost increase of {increase} from {battlefield_card.name}")
                
                # Opponent-specific tax effects
                if is_opponent and ("spells your opponents cast" in oracle_text and "cost" in oracle_text and "more" in oracle_text):
                    match = re.search(r"cost \{(\d+)\} more", oracle_text)
                    if match:
                        increase = int(match.group(1))
                        increased_cost["generic"] += increase
                        logging.debug(f"Applying opponent cost increase of {increase} from {battlefield_card.name}")
        
        return increased_cost

    def apply_minimum_cost_effects(self, player, cost, card_id, context=None):
        """
        Apply minimum cost effects like Trinisphere.
        
        Args:
            player: The player dictionary
            cost: The parsed mana cost dictionary
            card_id: ID of the card being cast
            context: Optional context for special cases
            
        Returns:
            dict: The cost dictionary with minimum cost applied
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return cost
        
        final_cost = cost.copy()
        
        # Calculate the total mana value of the spell
        total_cost = 0
        
        # Add colored mana costs
        for color in ['W', 'U', 'B', 'R', 'G', 'C']:
            total_cost += final_cost[color]
        
        # Add generic cost
        total_cost += final_cost['generic']
        
        # Add hybrid costs (count each hybrid symbol as 1)
        total_cost += len(final_cost['hybrid'])
        
        # Add phyrexian costs (count each phyrexian symbol as 1)
        total_cost += len(final_cost['phyrexian'])
        
        # Look for minimum cost effects (like Trinisphere)
        min_cost_value = 0
        
        for player_idx, p in enumerate([gs.p1, gs.p2]):
            for battlefield_id in p["battlefield"]:
                battlefield_card = gs._safe_get_card(battlefield_id)
                if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                    continue
                    
                oracle_text = battlefield_card.oracle_text.lower()
                
                # Trinisphere effect
                if "each spell with mana value less than" in oracle_text and "has a mana value of" in oracle_text:
                    import re
                    match = re.search(r"less than (\d+) has a mana value of (\d+)", oracle_text)
                    if match:
                        threshold = int(match.group(1))
                        min_value = int(match.group(2))
                        
                        if total_cost < threshold and min_value > min_cost_value:
                            min_cost_value = min_value
                            logging.debug(f"Found minimum cost effect: {min_value} from {battlefield_card.name}")
        
        # Apply minimum cost if needed
        if min_cost_value > 0 and total_cost < min_cost_value:
            # Adjust generic mana to meet the minimum cost
            final_cost["generic"] += (min_cost_value - total_cost)
            logging.debug(f"Applied minimum cost effect: Adjusted total cost from {total_cost} to {min_cost_value}")
        
        return final_cost
        
    def pay_phyrexian_mana(self, player, phyrexian_colors):
        """
        Pay phyrexian mana costs optimally using mana or life.
        
        Args:
            player: The player dictionary
            phyrexian_colors: List of phyrexian color costs
            
        Returns:
            tuple: (bool success, int life_paid)
        """
        if not phyrexian_colors:
            return True, 0
        
        # Track payments
        life_paid = 0
        
        # First, try to pay with mana when available
        for color in phyrexian_colors:
            if player["mana_pool"].get(color, 0) > 0:
                player["mana_pool"][color] -= 1
            else:
                # Pay with life
                life_paid += 2
        
        # Check if player has enough life
        if player["life"] < life_paid:
            return False, 0  # Can't pay with life
        
        # Apply life payment
        player["life"] -= life_paid
        return True, life_paid

    def parse_mana_cost(self, cost_text):
        """Parse a mana cost string into structured format with enhanced handling."""
        # Initialize default mana cost dictionary
        mana_cost = {
            'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0,
            'generic': 0, 'X': 0,
            'hybrid': [],  # List of tuples for hybrid tokens (e.g. ["W/U"])
            'phyrexian': [],  # List of tuples for phyrexian tokens (e.g. ["W/P"])
            'snow': 0,  # Snow mana requirement
            'any_color': 0,  # Mana of any color (e.g. {1}), different from generic
            'conditional': []  # Conditional mana requirements
        }
        
        # Handle None, empty string, or non-string inputs
        if cost_text is None or not isinstance(cost_text, str) or cost_text.strip() == '':
            logging.debug(f"Parsing mana cost for input: {cost_text}. Returning default empty cost.")
            return mana_cost
        
        # Remove any whitespace
        cost_text = cost_text.replace(' ', '')
        
        # Special case for 0 mana cost (free spells)
        if cost_text == '':
            return mana_cost
        
        # Extract mana tokens (handles formats like "{W}", "{1}", "{G/U}", etc.)
        tokens = re.findall(r'\{([^}]+)\}', cost_text)
        
        # If no tokens found, log and return default
        if not tokens:
            logging.warning(f"No valid mana tokens found in cost: {cost_text}")
            return mana_cost
        
        for token in tokens:
            # Normalize token to uppercase for consistency
            clean_token = token.strip().upper()
            
            # Skip tap symbol - it's not actually mana
            if clean_token.lower() == 't':
                continue
                
            # Basic mana symbols
            if clean_token in self.mana_symbols:
                mana_cost[clean_token] += 1
                
            # Generic mana
            elif clean_token.isdigit():
                mana_cost['generic'] += int(clean_token)
                
            # X cost
            elif clean_token == 'X':
                mana_cost['X'] += 1
                
            # Hybrid mana (expanded handling)
            elif '/' in clean_token and 'P' not in clean_token and not any(c.isdigit() for c in clean_token):
                # Standard hybrid (e.g., W/U)
                colors = clean_token.split('/')
                if all(c in self.mana_symbols for c in colors):
                    mana_cost['hybrid'].append(tuple(colors))
                else:
                    logging.warning(f"Invalid hybrid mana token: {token}")
                    
            # Phyrexian mana (expanded handling)
            elif '/' in clean_token and 'P' in clean_token:
                colors = clean_token.split('/')
                phyrexian_color = next((c for c in colors if c != 'P'), None)
                if phyrexian_color in self.mana_symbols:
                    mana_cost['phyrexian'].append(phyrexian_color)
                else:
                    logging.warning(f"Invalid phyrexian mana token: {token}")
                    
            # Snow mana
            elif clean_token == 'S':
                mana_cost['snow'] += 1
                
            # Two-hybrid mana (expanded handling)
            elif '/' in clean_token and any(c.isdigit() for c in clean_token):
                parts = clean_token.split('/')
                numeric_part = next((p for p in parts if p.isdigit()), None)
                color_part = next((p for p in parts if p in self.mana_symbols), None)
                
                if numeric_part and color_part:
                    mana_cost['hybrid'].append((numeric_part, color_part))
                else:
                    logging.warning(f"Invalid two-hybrid mana token: {token}")
                    
            # Any mana of any color
            elif clean_token in ['WUBRG', 'WUBRGC']:
                mana_cost['any_color'] += 1
                
            # Other special cases
            else:
                logging.warning(f"Unrecognized mana token: {token} in {cost_text}")
        
        return mana_cost
    
    def calculate_alternative_cost(self, card_id, controller, alt_cost_type, context=None):
        """
        Calculate an alternative cost for a card based on various alternative casting methods.
        
        Args:
            card_id: The card to calculate cost for
            controller: The player casting the card
            alt_cost_type: Type of alternative cost ('foretell', 'flashback', 'suspend', etc.)
            context: Additional context information
            
        Returns:
            dict: The calculated alternative cost dictionary, or None if not applicable
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        if not card or not hasattr(card, 'oracle_text') or not hasattr(card, 'mana_cost'):
            return None
            
        oracle_text = card.oracle_text.lower()
        normal_cost = self.parse_mana_cost(card.mana_cost)
        alt_cost = None
        
        if alt_cost_type == "foretell":
            # Find foretell cost
            import re
            match = re.search(r"foretell [^\(]([^\)]+)", oracle_text)
            if match:
                foretell_cost = match.group(1)
                alt_cost = self.parse_mana_cost(foretell_cost)
                logging.debug(f"Calculated foretell cost for {card.name}: {foretell_cost}")
        
        elif alt_cost_type == "flashback":
            # Find flashback cost
            import re
            match = re.search(r"flashback [^\(]([^\)]+)", oracle_text)
            if match:
                flashback_cost = match.group(1)
                alt_cost = self.parse_mana_cost(flashback_cost)
                logging.debug(f"Calculated flashback cost for {card.name}: {flashback_cost}")
        
        elif alt_cost_type == "escape":
            # Find escape cost and exile requirement
            import re
            match = re.search(r"escape—([^,]+)(, exile [^\.]+)?", oracle_text)
            if match:
                escape_cost = match.group(1).strip()
                exile_requirement = match.group(2) if match.group(2) else "exile five other cards"
                
                # Parse exile requirement
                exile_count = 5  # Default
                exile_match = re.search(r"exile (\w+)", exile_requirement)
                if exile_match:
                    exile_word = exile_match.group(1)
                    if exile_word.isdigit():
                        exile_count = int(exile_word)
                    elif exile_word == "three":
                        exile_count = 3
                    elif exile_word == "four":
                        exile_count = 4
                    elif exile_word == "five":
                        exile_count = 5
                
                # Check if player has enough cards in graveyard to exile
                graveyard_size = len(controller["graveyard"])
                if graveyard_size <= exile_count:
                    return None  # Not enough cards to exile
                    
                alt_cost = self.parse_mana_cost(escape_cost)
                alt_cost["exile_cards"] = exile_count
                logging.debug(f"Calculated escape cost for {card.name}: {escape_cost}, exile {exile_count} cards")
        
        elif alt_cost_type == "adventure":
            # Find adventure cost (if card has an adventure half)
            if "adventure" in oracle_text:
                import re
                # Look for pattern like "Creature Name   W/U\nCreature Type"
                match = re.search(r"\n([^\n]+)\s+([^\n]+)\n", oracle_text)
                if match:
                    adventure_name = match.group(1).strip()
                    adventure_cost = match.group(2).strip()
                    
                    alt_cost = self.parse_mana_cost(adventure_cost)
                    logging.debug(f"Calculated adventure cost for {card.name}: {adventure_cost}")
        
        elif alt_cost_type == "overload":
            # Find overload cost
            import re
            match = re.search(r"overload [^\(]([^\)]+)", oracle_text)
            if match:
                overload_cost = match.group(1)
                alt_cost = self.parse_mana_cost(overload_cost)
                logging.debug(f"Calculated overload cost for {card.name}: {overload_cost}")
        
        elif alt_cost_type == "spectacle":
            # Find spectacle cost
            import re
            match = re.search(r"spectacle [^\(]([^\)]+)", oracle_text)
            if match:
                spectacle_cost = match.group(1)
                alt_cost = self.parse_mana_cost(spectacle_cost)
                
                # Check if opponent lost life this turn
                opponent = gs.p2 if controller == gs.p1 else gs.p1
                lost_life_this_turn = opponent.get("lost_life_this_turn", False)
                
                if not lost_life_this_turn:
                    return None  # Spectacle condition not met
                    
                logging.debug(f"Calculated spectacle cost for {card.name}: {spectacle_cost}")
                
        elif alt_cost_type == "mutate":
            # Find mutate cost
            import re
            match = re.search(r"mutate [^\(]([^\)]+)", oracle_text)
            if match:
                mutate_cost = match.group(1)
                alt_cost = self.parse_mana_cost(mutate_cost)
                logging.debug(f"Calculated mutate cost for {card.name}: {mutate_cost}")
                
        elif alt_cost_type == "surge":
            # Find surge cost
            import re
            match = re.search(r"surge [^\(]([^\)]+)", oracle_text)
            if match:
                surge_cost = match.group(1)
                alt_cost = self.parse_mana_cost(surge_cost)
                
                # Check if surge condition is met (a teammate cast a spell this turn)
                if not context or not context.get("teammate_cast_spell", False):
                    return None  # Surge condition not met
                    
                logging.debug(f"Calculated surge cost for {card.name}: {surge_cost}")
                
        elif alt_cost_type == "prowl":
            # Find prowl cost
            import re
            match = re.search(r"prowl [^\(]([^\)]+)", oracle_text)
            if match:
                prowl_cost = match.group(1)
                alt_cost = self.parse_mana_cost(prowl_cost)
                
                # Check if prowl condition is met (dealt combat damage with creature of same type)
                if not context or not context.get("prowl_condition_met", False):
                    return None  # Prowl condition not met
                    
                logging.debug(f"Calculated prowl cost for {card.name}: {prowl_cost}")
                
        elif alt_cost_type == "evoke":
            # Find evoke cost
            import re
            match = re.search(r"evoke [^\(]([^\)]+)", oracle_text)
            if match:
                evoke_cost = match.group(1)
                alt_cost = self.parse_mana_cost(evoke_cost)
                logging.debug(f"Calculated evoke cost for {card.name}: {evoke_cost}")
        
        elif alt_cost_type == "madness":
            # Find madness cost
            import re
            match = re.search(r"madness [^\(]([^\)]+)", oracle_text)
            if match:
                madness_cost = match.group(1)
                alt_cost = self.parse_mana_cost(madness_cost)
                logging.debug(f"Calculated madness cost for {card.name}: {madness_cost}")
                
        elif alt_cost_type == "aftermath":
            # Find aftermath cost (on the second half of the card)
            if "aftermath" in oracle_text:
                import re
                # Look for pattern like "Aftermath   W/U\n" 
                match = re.search(r"aftermath\s+([^\n]+)", oracle_text, re.IGNORECASE)
                if match:
                    aftermath_cost = match.group(1).strip()
                    alt_cost = self.parse_mana_cost(aftermath_cost)
                    logging.debug(f"Calculated aftermath cost for {card.name}: {aftermath_cost}")
                    
        elif alt_cost_type == "emerge":
            # Find emerge cost
            import re
            match = re.search(r"emerge [^\(]([^\)]+)", oracle_text)
            if match:
                emerge_cost = match.group(1)
                alt_cost = self.parse_mana_cost(emerge_cost)
                
                # If sacrificing a creature, reduce cost accordingly
                if context and "sacrificed_creature" in context:
                    sacrifice_id = context["sacrificed_creature"]
                    sacrifice_card = self.game_state._safe_get_card(sacrifice_id)
                    if sacrifice_card and hasattr(sacrifice_card, 'cmc'):
                        emerge_reduction = sacrifice_card.cmc
                        alt_cost["generic"] = max(0, alt_cost["generic"] - emerge_reduction)
                        
                logging.debug(f"Calculated emerge cost for {card.name}: {emerge_cost}")

        elif alt_cost_type == "dash":
            # Find dash cost
            import re
            match = re.search(r"dash [^\(]([^\)]+)", oracle_text)
            if match:
                dash_cost = match.group(1)
                alt_cost = self.parse_mana_cost(dash_cost)
                logging.debug(f"Calculated dash cost for {card.name}: {dash_cost}")
                
        elif alt_cost_type == "cycling":
            # Find cycling cost
            import re
            match = re.search(r"cycling [^\(]([^\)]+)", oracle_text)
            if match:
                cycling_cost = match.group(1)
                alt_cost = self.parse_mana_cost(cycling_cost)
                logging.debug(f"Calculated cycling cost for {card.name}: {cycling_cost}")
        
        return alt_cost
    
    def _get_usable_conditional_mana(self, conditional_mana, context):
        """
        Determine which conditional mana can be used for the current context.
        
        Args:
            conditional_mana: Dictionary of conditional mana
            context: The spell casting context
            
        Returns:
            dict: Dictionary of usable mana by color
        """
        if not context:
            return {}
        
        usable_mana = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        
        # Get the card being cast
        card = context.get('card')
        if not card:
            card_id = context.get('card_id')
            if card_id:
                card = self.game_state._safe_get_card(card_id)
        
        if not card:
            return usable_mana
        
        # Check each restriction type against the context
        for restriction_key, mana_pool in conditional_mana.items():
            can_use = False
            
            if restriction_key.startswith("cast_only:"):
                target_type = restriction_key[10:]  # Extract the target type
                
                # Check if card matches the restriction
                if "creature" in target_type and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    can_use = True
                elif "dragon" in target_type and hasattr(card, 'subtypes') and 'dragon' in card.subtypes:
                    can_use = True
                elif "artifact" in target_type and hasattr(card, 'card_types') and 'artifact' in card.card_types:
                    can_use = True
                elif "spell" in target_type:  # Generic "spell" restriction
                    can_use = True
            
            elif restriction_key.startswith("spend_only:"):
                target_type = restriction_key[11:]  # Extract the target type
                
                # Check if context matches the restriction
                if "activated abilities" in target_type and context.get('is_ability', False):
                    can_use = True
                elif "activated abilities of creatures" in target_type and context.get('is_ability', False):
                    ability_source = context.get('ability_source')
                    if ability_source:
                        ability_card = self.game_state._safe_get_card(ability_source)
                        if ability_card and hasattr(ability_card, 'card_types') and 'creature' in ability_card.card_types:
                            can_use = True
            
            # Add mana from pools that can be used
            if can_use:
                for color, amount in mana_pool.items():
                    usable_mana[color] += amount
        
        return usable_mana

    def can_pay_mana_cost(self, player, cost, context=None, pool_override=None):
        """
        Enhanced method to check if a player can pay a mana cost with all possible effects.
        
        Args:
            player: The player dictionary
            cost: The mana cost to check (card object, string, or parsed dict)
            context: Optional context for special cases
            pool_override: Optional override for player's mana pool
            
        Returns:
            bool: Whether the cost can be paid
        """
        try:
            # Handle different input types
            if hasattr(cost, 'mana_cost'):
                # If it's a Card object, use its mana cost
                if not hasattr(cost, 'mana_cost'):
                    return False  # Can't determine cost
                card_id = cost.card_id if hasattr(cost, 'card_id') else None
                parsed_cost = self.parse_mana_cost(cost.mana_cost)
            elif isinstance(cost, str):
                # If it's a string, parse the mana cost
                parsed_cost = self.parse_mana_cost(cost)
                card_id = None
            elif isinstance(cost, dict):
                # If it's already a parsed cost dictionary, use it directly
                parsed_cost = cost
                card_id = None
            else:
                # Invalid input type
                logging.warning(f"Invalid cost type: {type(cost)}")
                return False

            # Check for alternative costs
            if context and context.get('use_alt_cost'):
                alt_cost_type = context['use_alt_cost']
                card_id = context.get('card_id', card_id)
                if card_id is not None:
                    return self.can_pay_alternative_cost(player, card_id, alt_cost_type, context)

            # If there's a card_id, apply all cost modifiers
            if card_id is not None:
                parsed_cost = self.apply_cost_modifiers(player, parsed_cost, card_id, context)

            # Use pool_override if provided; otherwise use the player's mana pool
            if pool_override is not None:
                available_mana = pool_override.copy()
                conditional_mana = {}  # No conditional mana in override
            else:
                available_mana = player["mana_pool"].copy()
                conditional_mana = player.get("conditional_mana", {})
                
                # Include phase-restricted mana
                if hasattr(player, "phase_restricted_mana"):
                    for color, amount in player["phase_restricted_mana"].items():
                        if color in available_mana:
                            available_mana[color] += amount
                        else:
                            available_mana[color] = amount

            # Check if this cost can use conditional mana
            usable_conditional_mana = self._get_usable_conditional_mana(conditional_mana, context)

            # Add usable conditional mana to available mana (just for checking)
            for color, amount in usable_conditional_mana.items():
                if color in available_mana:
                    available_mana[color] += amount
                else:
                    available_mana[color] = amount

            # Check colored mana requirements first
            for color in ['W', 'U', 'B', 'R', 'G', 'C']:
                if parsed_cost[color] > available_mana.get(color, 0):
                    return False
                available_mana[color] -= parsed_cost[color]

            # Handle hybrid mana
            for hybrid_pair in parsed_cost['hybrid']:
                # Check if any option is available
                can_pay_hybrid = False
                for color in hybrid_pair:
                    if available_mana.get(color, 0) > 0:
                        can_pay_hybrid = True
                        break
                if not can_pay_hybrid:
                    return False

            # Handle Phyrexian mana
            for phyrexian_color in parsed_cost['phyrexian']:
                # Can pay with either 1 mana of the specified color or 2 life
                if available_mana.get(phyrexian_color, 0) > 0:
                    available_mana[phyrexian_color] -= 1
                elif player["life"] >= 2:
                    # We check if the player has enough life, but we don't deduct it here
                    pass
                else:
                    return False  # Can't pay with either mana or life
                    
            # Handle snow mana
            if parsed_cost['snow'] > 0:
                if not self.can_pay_snow_cost(player, parsed_cost['snow']):
                    return False

            # Calculate generic mana requirement
            generic_requirement = parsed_cost['generic']

            # Handle X costs if X value is provided in context
            if parsed_cost['X'] > 0 and context and 'X' in context:
                x_value = context['X']
                generic_requirement += x_value * parsed_cost['X']

            # Calculate total available mana for generic costs
            total_available = sum(available_mana.values())

            # Check if enough mana for generic cost
            if total_available < generic_requirement:
                return False

            return True
        except Exception as e:
            logging.error(f"Error checking mana payment: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return False  # Assume can't pay on error
        
    def calculate_cost_reduction(self, player, cost, card_id, context=None):
        """
        Calculate cost reduction effects that apply to a card.
        
        Args:
            player: The player dictionary
            cost: The parsed mana cost dictionary
            card_id: ID of the card being cast
            context: Optional context for special cases
            
        Returns:
            dict: The reduced cost dictionary
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return cost
        
        reduced_cost = cost.copy()
        
        # Check for cost reduction effects on the battlefield
        for battlefield_id in player["battlefield"]:
            battlefield_card = gs._safe_get_card(battlefield_id)
            if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                continue
                
            oracle_text = battlefield_card.oracle_text.lower()
            
            # Check for generic cost reduction
            if "spells you cast cost" in oracle_text and "less to cast" in oracle_text:
                # Extract amount of reduction
                import re
                match = re.search(r"cost \{(\d+)\} less", oracle_text)
                if match:
                    reduction = int(match.group(1))
                    reduced_cost["generic"] = max(0, reduced_cost["generic"] - reduction)
            
            # Check for color-specific cost reduction
            for color, symbol in zip(['white', 'blue', 'black', 'red', 'green'], ['W', 'U', 'B', 'R', 'G']):
                if f"{color} spells you cast cost" in oracle_text and "less to cast" in oracle_text:
                    # Check if spell is the right color
                    if hasattr(card, 'colors') and card.colors[list('WUBRG').index(symbol)]:
                        match = re.search(r"cost \{(\d+)\} less", oracle_text)
                        if match:
                            reduction = int(match.group(1))
                            reduced_cost["generic"] = max(0, reduced_cost["generic"] - reduction)
            
            # Check for type-specific cost reduction
            for spell_type in ["creature", "instant", "sorcery", "artifact", "enchantment", "planeswalker"]:
                if f"{spell_type} spells you cast cost" in oracle_text and "less to cast" in oracle_text:
                    if hasattr(card, 'card_types') and spell_type in card.card_types:
                        match = re.search(r"cost \{(\d+)\} less", oracle_text)
                        if match:
                            reduction = int(match.group(1))
                            reduced_cost["generic"] = max(0, reduced_cost["generic"] - reduction)
        
        # Check for commander cost reduction (if applicable)
        if context and context.get("is_commander", False):
            # In Commander format, each time you cast your commander from the command zone,
            # it costs {2} more for each previous time it was cast
            commander_cast_count = context.get("commander_cast_count", 0)
            if commander_cast_count > 0:
                reduced_cost["generic"] += commander_cast_count * 2
        
        # Check for cost reduction based on mechanics
        if card and hasattr(card, 'oracle_text'):
            oracle_text = card.oracle_text.lower()
            
            # Affinity (reduces cost based on number of artifacts you control)
            if "affinity for artifacts" in oracle_text:
                artifacts_count = sum(1 for cid in player["battlefield"] 
                                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                                and 'artifact' in gs._safe_get_card(cid).card_types)
                reduced_cost["generic"] = max(0, reduced_cost["generic"] - artifacts_count)
                
            # Convoke (can tap creatures to pay for spells)
            if "convoke" in oracle_text and context and "convoke_creatures" in context:
                convoke_creatures = context["convoke_creatures"]
                if isinstance(convoke_creatures, list):
                    convoke_amount = len(convoke_creatures)
                    reduced_cost["generic"] = max(0, reduced_cost["generic"] - convoke_amount)
                    
            # Delve (can exile cards from graveyard to pay for spells)
            if "delve" in oracle_text and context and "delve_cards" in context:
                delve_cards = context["delve_cards"]
                if isinstance(delve_cards, list):
                    delve_amount = len(delve_cards)
                    reduced_cost["generic"] = max(0, reduced_cost["generic"] - delve_amount)
                    
            # Improvise (can tap artifacts to help cast spells)
            if "improvise" in oracle_text and context and "improvise_artifacts" in context:
                improvise_artifacts = context["improvise_artifacts"]
                if isinstance(improvise_artifacts, list):
                    improvise_amount = len(improvise_artifacts)
                    reduced_cost["generic"] = max(0, reduced_cost["generic"] - improvise_amount)
        
        # Check for cost increasing effects
        for player_idx, p in enumerate([gs.p1, gs.p2]):
            for battlefield_id in p["battlefield"]:
                battlefield_card = gs._safe_get_card(battlefield_id)
                if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                    continue
                    
                oracle_text = battlefield_card.oracle_text.lower()
                
                # Tax effects like "Spells cost {1} more to cast"
                if "spells cost" in oracle_text and "more to cast" in oracle_text:
                    match = re.search(r"cost \{(\d+)\} more", oracle_text)
                    if match:
                        increase = int(match.group(1))
                        reduced_cost["generic"] += increase
                
                # Check for specific targeting tax effects
                if context and context.get("targeting_opponent", False) and player_idx != (0 if gs.agent_is_p1 else 1):
                    if "spells your opponents cast that target" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        match = re.search(r"cost \{(\d+)\} more", oracle_text)
                        if match:
                            increase = int(match.group(1))
                            reduced_cost["generic"] += increase
        
        return reduced_cost

    def _pay_generic_mana_with_conditional(self, player, amount, payment, usable_conditional_mana, context):
        """
        Pay generic mana optimally using both regular and conditional mana.
        
        Args:
            player: The player dictionary
            amount: Amount of generic mana to pay
            payment: Payment tracking dictionary
            usable_conditional_mana: Dictionary of usable conditional mana
            context: Spell casting context
            
        Returns:
            int: Remaining unpaid amount (0 if fully paid)
        """
        # First use colorless mana
        colorless_used = min(player["mana_pool"].get('C', 0), amount)
        player["mana_pool"]['C'] -= colorless_used
        amount -= colorless_used
        
        if 'C' not in payment['colors']:
            payment['colors']['C'] = 0
        payment['colors']['C'] += colorless_used

        # Then use conditional colorless mana
        if amount > 0 and usable_conditional_mana.get('C', 0) > 0:
            for restriction_key, mana_pool in player.get("conditional_mana", {}).items():
                if amount <= 0:
                    break
                    
                if self._can_use_conditional_mana(restriction_key, context) and mana_pool.get('C', 0) > 0:
                    conditional_used = min(mana_pool.get('C', 0), amount)
                    mana_pool['C'] -= conditional_used
                    amount -= conditional_used
                    
                    if restriction_key not in payment['conditional']:
                        payment['conditional'][restriction_key] = {}
                        
                    if 'C' not in payment['conditional'][restriction_key]:
                        payment['conditional'][restriction_key]['C'] = 0
                        
                    payment['conditional'][restriction_key]['C'] += conditional_used

        # Dynamically prioritize colors based on mana pool availability
        colors = sorted(['G', 'R', 'B', 'U', 'W'], key=lambda color: player["mana_pool"].get(color, 0))

        for color in colors:
            if amount <= 0:
                break

            available = player["mana_pool"].get(color, 0)
            used = min(available, amount)
            player["mana_pool"][color] -= used
            amount -= used
            
            if color not in payment['colors']:
                payment['colors'][color] = 0
            payment['colors'][color] += used

        # If still need more, use conditional colored mana
        if amount > 0:
            for restriction_key, mana_pool in player.get("conditional_mana", {}).items():
                if amount <= 0:
                    break
                    
                if self._can_use_conditional_mana(restriction_key, context):
                    # Use colored mana in the same order
                    for color in colors:
                        if amount <= 0:
                            break
                            
                        available = mana_pool.get(color, 0)
                        used = min(available, amount)
                        mana_pool[color] -= used
                        amount -= used
                        
                        if restriction_key not in payment['conditional']:
                            payment['conditional'][restriction_key] = {}
                            
                        if color not in payment['conditional'][restriction_key]:
                            payment['conditional'][restriction_key][color] = 0
                            
                        payment['conditional'][restriction_key][color] += used

        return amount
    
    def _pay_two_hybrid_mana(self, player, hybrid_pairs, payment):
        """
        Pay two-hybrid mana costs (e.g., {2/R}) optimally.
        
        Args:
            player: The player dictionary
            hybrid_pairs: List of two-hybrid mana pairs (numeric_part, color_part)
            payment: Payment tracking dictionary
            
        Returns:
            bool: Whether the payment was successful
        """
        for hybrid_pair in hybrid_pairs:
            numeric_part, color_part = hybrid_pair
            numeric_value = int(numeric_part)
            color = color_part.upper()
            
            # Check if player has the colored mana
            if player["mana_pool"].get(color, 0) > 0:
                # Pay with colored mana (often more efficient)
                player["mana_pool"][color] -= 1
                
                if color not in payment['colors']:
                    payment['colors'][color] = 0
                payment['colors'][color] += 1
            else:
                # Pay with generic mana
                remaining = self._pay_generic_mana_with_conditional(player, numeric_value, payment, {}, None)
                
                if remaining > 0:
                    # Couldn't pay the generic part
                    return False
        
        return True
    
    def calculate_cost_increase(self, player, cost, card_id, context=None):
        """
        Enhanced cost increase calculation with targeting support.
        
        Args:
            player: The player dictionary
            cost: The parsed mana cost dictionary
            card_id: ID of the card being cast
            context: Optional context for special cases
            
        Returns:
            dict: The increased cost dictionary
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return cost
        
        increased_cost = cost.copy()
        opponent = gs.p2 if player == gs.p1 else gs.p1
        
        # Check for cost increasing effects on the battlefield for all players
        for battlefield_player in [gs.p1, gs.p2]:
            is_opponent = (player != battlefield_player)
            
            for battlefield_id in battlefield_player["battlefield"]:
                battlefield_card = gs._safe_get_card(battlefield_id)
                if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                    continue
                    
                oracle_text = battlefield_card.oracle_text.lower()
                
                # Basic tax effects
                if "spells cost" in oracle_text and "more to cast" in oracle_text:
                    # Extract amount of increase
                    import re
                    match = re.search(r"cost \{(\d+)\} more", oracle_text)
                    if match:
                        increase = int(match.group(1))
                        increased_cost["generic"] += increase
                        logging.debug(f"Applying generic cost increase of {increase} from {battlefield_card.name}")
                
                # Color-specific tax effects
                for color, symbol in zip(['white', 'blue', 'black', 'red', 'green'], ['W', 'U', 'B', 'R', 'G']):
                    if f"{color} spells" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        # Check if spell is the right color
                        if hasattr(card, 'colors') and card.colors[list('WUBRG').index(symbol)]:
                            match = re.search(r"cost \{(\d+)\} more", oracle_text)
                            if match:
                                increase = int(match.group(1))
                                increased_cost["generic"] += increase
                                logging.debug(f"Applying {color} spell cost increase of {increase} from {battlefield_card.name}")
                
                # Opponent-specific tax effects
                if is_opponent and ("spells your opponents cast" in oracle_text and "cost" in oracle_text and "more" in oracle_text):
                    match = re.search(r"cost \{(\d+)\} more", oracle_text)
                    if match:
                        increase = int(match.group(1))
                        increased_cost["generic"] += increase
                        logging.debug(f"Applying opponent cost increase of {increase} from {battlefield_card.name}")
                
                # Targeting-specific tax effects
                if context and context.get('targets'):
                    targets = context.get('targets', [])
                    
                    # Targeting opponent's creatures
                    if "spells that target" in oracle_text and "you control" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        controller_targets = [
                            tid for tid in targets 
                            if battlefield_player == gs._find_card_controller(tid)
                        ]
                        
                        if controller_targets and is_opponent:
                            match = re.search(r"cost \{(\d+)\} more", oracle_text)
                            if match:
                                increase = int(match.group(1))
                                increased_cost["generic"] += increase
                                logging.debug(f"Applying targeting tax of {increase} for targeting opponent's permanents")
                    
                    # Targeting specific card types
                    for card_type in ["creature", "artifact", "enchantment", "planeswalker", "land"]:
                        if f"spells that target {card_type}" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                            type_targets = [
                                tid for tid in targets 
                                if gs._safe_get_card(tid) and 
                                hasattr(gs._safe_get_card(tid), 'card_types') and 
                                card_type in gs._safe_get_card(tid).card_types
                            ]
                            
                            if type_targets:
                                match = re.search(r"cost \{(\d+)\} more", oracle_text)
                                if match:
                                    increase = int(match.group(1))
                                    increased_cost["generic"] += increase
                                    logging.debug(f"Applying targeting tax of {increase} for targeting {card_type}s")
                
                # Multiple target tax effects
                if context and context.get('targets') and len(context.get('targets', [])) > 1:
                    if "spells with more than one target" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        match = re.search(r"cost \{(\d+)\} more", oracle_text)
                        if match:
                            increase = int(match.group(1))
                            increased_cost["generic"] += increase
                            logging.debug(f"Applying multi-target tax of {increase}")
        
        return increased_cost

    def _can_use_conditional_mana(self, restriction_key, context):
        """
        Comprehensive check for whether conditional mana can be used for a given context.
        
        Args:
            restriction_key: The restriction key to check
            context: The spell casting context
            
        Returns:
            bool: Whether the conditional mana can be used
        """
        if not context:
            return False
        
        # Get the card being cast
        card = context.get('card')
        if not card:
            card_id = context.get('card_id')
            if card_id:
                card = self.game_state._safe_get_card(card_id)
        
        if not card:
            return False
        
        # Basic restrictions
        if restriction_key.startswith("cast_only:"):
            target_type = restriction_key[10:].lower()  # Extract the target type and normalize
            
            # Card type restrictions
            if "creature" in target_type and (hasattr(card, 'card_types') and 'creature' in card.card_types):
                return True
            elif "instant" in target_type and (hasattr(card, 'card_types') and 'instant' in card.card_types):
                return True
            elif "sorcery" in target_type and (hasattr(card, 'card_types') and 'sorcery' in card.card_types):
                return True
            elif "artifact" in target_type and (hasattr(card, 'card_types') and 'artifact' in card.card_types):
                return True
            elif "enchantment" in target_type and (hasattr(card, 'card_types') and 'enchantment' in card.card_types):
                return True
            elif "planeswalker" in target_type and (hasattr(card, 'card_types') and 'planeswalker' in card.card_types):
                return True
                
            # Subtype restrictions
            subtypes = card.subtypes if hasattr(card, 'subtypes') else []
            for subtype in subtypes:
                if subtype.lower() in target_type:
                    return True
                    
            # Color restrictions
            if hasattr(card, 'colors'):
                for i, color in enumerate(['white', 'blue', 'black', 'red', 'green']):
                    if color in target_type and card.colors[i]:
                        return True
                        
            # Cost restrictions
            if "colored" in target_type and hasattr(card, 'mana_cost'):
                cost = self.parse_mana_cost(card.mana_cost)
                if any(cost[c] > 0 for c in ['W', 'U', 'B', 'R', 'G']):
                    return True
                    
            if "colorless" in target_type and hasattr(card, 'mana_cost'):
                cost = self.parse_mana_cost(card.mana_cost)
                if cost['C'] > 0 and all(cost[c] == 0 for c in ['W', 'U', 'B', 'R', 'G']):
                    return True
                    
            # Generic spell type
            if "spell" in target_type:
                return True
                
        elif restriction_key.startswith("spend_only:"):
            target_type = restriction_key[11:].lower()  # Extract the target type
            
            # Ability restrictions
            if "activated abilities" in target_type and context.get('is_ability', False):
                return True
                
            # Specific ability types
            if "activated abilities of creatures" in target_type and context.get('is_ability', False):
                ability_source = context.get('ability_source')
                if ability_source:
                    ability_card = self.game_state._safe_get_card(ability_source)
                    if ability_card and hasattr(ability_card, 'card_types') and 'creature' in ability_card.card_types:
                        return True
                        
            if "activated abilities of artifacts" in target_type and context.get('is_ability', False):
                ability_source = context.get('ability_source')
                if ability_source:
                    ability_card = self.game_state._safe_get_card(ability_source)
                    if ability_card and hasattr(ability_card, 'card_types') and 'artifact' in ability_card.card_types:
                        return True
                        
        # Special case for mana from treasures, which can be spent on anything
        elif restriction_key == "from_treasure":
            return True
            
        # Generic "any" mana
        elif restriction_key == "any_color":
            return True
        
        return False
    
    def _refund_payment(self, player, payment):
        """
        Refund all mana from a failed payment.
        
        Args:
            player: The player dictionary
            payment: Payment tracking dictionary
        """
        # Refund regular mana
        for color, amount in payment['colors'].items():
            if color in player["mana_pool"]:
                player["mana_pool"][color] += amount
            else:
                player["mana_pool"][color] = amount
        
        # Refund conditional mana
        for restriction_key, colors in payment['conditional'].items():
            if restriction_key not in player.get("conditional_mana", {}):
                player["conditional_mana"][restriction_key] = {}
                
            for color, amount in colors.items():
                if color in player["conditional_mana"][restriction_key]:
                    player["conditional_mana"][restriction_key][color] += amount
                else:
                    player["conditional_mana"][restriction_key][color] = amount
        
        # Refund life if needed
        if payment['life'] > 0:
            player["life"] += payment['life']

    def _cleanup_empty_conditional_mana(self, player):
        """
        Remove empty conditional mana pools.
        
        Args:
            player: The player dictionary
        """
        if not hasattr(player, "conditional_mana"):
            return
            
        to_remove = []
        
        for restriction_key, mana_pool in player["conditional_mana"].items():
            # Check if this pool is empty
            if all(amount <= 0 for amount in mana_pool.values()):
                to_remove.append(restriction_key)
        
        # Remove empty pools
        for key in to_remove:
            del player["conditional_mana"][key]

    def _format_payment_for_logging(self, payment):
        """Format a payment for logging."""
        parts = []
        
        # Add regular mana paid
        for color, count in payment['colors'].items():
            if count > 0:
                parts.append(f"{count} {self.color_names[color]}")
        
        # Add conditional mana paid
        for restriction_key, colors in payment['conditional'].items():
            for color, count in colors.items():
                if count > 0:
                    parts.append(f"{count} restricted {self.color_names[color]} ({restriction_key})")
        
        # Add life paid
        if payment['life'] > 0:
            parts.append(f"{payment['life']} life")
        
        return ", ".join(parts)

    def pay_mana_cost(self, player, cost, context=None):
        """
        Enhanced method to pay a mana cost from a player's mana pool with all effects,
        including handling non-mana costs like tapping creatures or exiling cards based on context.
        Now handles non-mana costs first.
        """
        if context is None: context = {}

        # --- Determine Final Mana Cost ---
        # Parse cost and apply modifiers
        if hasattr(cost, 'mana_cost'):
            card_id = cost.card_id if hasattr(cost, 'card_id') else None
            parsed_cost = self.parse_mana_cost(cost.mana_cost)
            if card_id:
                parsed_cost = self.apply_cost_modifiers(player, parsed_cost, card_id, context)
        elif isinstance(cost, str):
            card_id = context.get('card_id')
            parsed_cost = self.parse_mana_cost(cost)
            if card_id:
                parsed_cost = self.apply_cost_modifiers(player, parsed_cost, card_id, context)
            else:
                 parsed_cost = self.apply_cost_modifiers(player, parsed_cost, None, context)
        elif isinstance(cost, dict): # Assume already parsed and potentially modified
             parsed_cost = cost
             card_id = context.get('card_id')
             # Re-apply modifiers just in case context has changed (like chosen X)
             if card_id:
                  parsed_cost = self.apply_cost_modifiers(player, parsed_cost, card_id, context)
             else:
                  parsed_cost = self.apply_cost_modifiers(player, parsed_cost, None, context)
        else:
            logging.error(f"Invalid cost type provided to pay_mana_cost: {type(cost)}")
            return False

        # Verify cost can be paid *before* attempting payment
        if not self.can_pay_mana_cost(player, parsed_cost, context):
            logging.warning(f"Cannot afford final cost {self._format_mana_cost_for_logging(parsed_cost)}")
            return False

        # Track payment details
        payment = { 'colors': defaultdict(int), 'conditional': defaultdict(lambda: defaultdict(int)),
                    'life': 0, 'snow': 0, 'tapped_creatures': [], 'exiled_cards': [], 'sacrificed_perms': [], 'discarded_cards': [], 'phase_restricted': defaultdict(int)} # Added phase restricted tracking

        # --- Execute Non-Mana Costs specified in context FIRST ---
        gs = self.game_state # Alias for easier access
        non_mana_costs_paid = True

        # Convoke: Tap creatures provided in context
        if context.get("convoke_creatures"):
             tapped_for_convoke = []
             for creature_idx_or_id in context["convoke_creatures"]:
                 convoke_id = None
                 # Check if param is index or ID
                 if isinstance(creature_idx_or_id, int):
                      if creature_idx_or_id < len(player["battlefield"]):
                           convoke_id = player["battlefield"][creature_idx_or_id]
                 elif isinstance(creature_idx_or_id, str):
                      if creature_idx_or_id in player["battlefield"]:
                           convoke_id = creature_idx_or_id
                 else:
                     logging.warning(f"Invalid creature identifier for Convoke: {creature_idx_or_id}")
                     continue

                 if convoke_id and gs.tap_permanent(convoke_id, player): # Use GameState's method
                         tapped_for_convoke.append(convoke_id)
                 elif convoke_id: # Tap failed (e.g. already tapped)
                     logging.warning(f"Failed to tap {gs._safe_get_card(convoke_id).name} for Convoke.")
                     non_mana_costs_paid = False; break # Stop if any tap fails

             if non_mana_costs_paid:
                 payment['tapped_creatures'].extend(tapped_for_convoke)
                 logging.debug(f"Paid Convoke cost by tapping {len(tapped_for_convoke)} creatures.")
             else:
                 # Rollback convoke taps? More complex. For now, just fail.
                 return False

        # Delve: Exile cards from GY provided in context
        if context.get("delve_cards"):
             exiled_for_delve = []
             gy_indices_to_exile = context["delve_cards"] # Assume list of indices
             # Validate indices first
             valid_indices = [idx for idx in gy_indices_to_exile if 0 <= idx < len(player["graveyard"])]
             if len(valid_indices) != len(gy_indices_to_exile):
                  logging.warning("Invalid graveyard indices provided for Delve.")
                  non_mana_costs_paid = False
             else:
                  # Remove from graveyard (safer: iterate sorted indices descending)
                  gy_cards_to_exile = [player["graveyard"][idx] for idx in sorted(valid_indices, reverse=True)]
                  for card_to_exile in gy_cards_to_exile:
                       if card_to_exile in player["graveyard"]: # Double check presence
                           if gs.move_card(card_to_exile, player, "graveyard", player, "exile"):
                               exiled_for_delve.append(card_to_exile)
                       else: # Card vanished?
                           logging.warning(f"Card {card_to_exile} unexpectedly not in graveyard for Delve.")
                           non_mana_costs_paid = False; break

                  if not non_mana_costs_paid:
                      # Rollback exiled cards? Very complex. Fail for now.
                      return False
                  else:
                      payment['exiled_cards'].extend(exiled_for_delve)
                      logging.debug(f"Paid Delve cost by exiling {len(exiled_for_delve)} cards.")

        # Emerge: Sacrifice was handled in _handle_alternative_casting. No payment action needed here.

        # Escape: Exiling from GY was handled in _handle_alternative_casting.

        # Jump-Start: Discard was handled in _handle_alternative_casting.

        # Generic Sacrifice (from "Additional Costs")
        if context.get("sacrifice_additional"): # Expects list of permanent indices/IDs
            sacrificed_for_add = []
            for sac_idx_or_id in context["sacrifice_additional"]:
                sac_id = None
                if isinstance(sac_idx_or_id, int):
                    if sac_idx_or_id < len(player["battlefield"]):
                        sac_id = player["battlefield"][sac_idx_or_id]
                elif isinstance(sac_idx_or_id, str):
                    if sac_idx_or_id in player["battlefield"]:
                        sac_id = sac_idx_or_id
                if sac_id:
                    if gs.move_card(sac_id, player, "battlefield", player, "graveyard"):
                        sacrificed_for_add.append(sac_id)
                    else: # Failed to sacrifice
                         non_mana_costs_paid = False; break
                else: non_mana_costs_paid = False; break
            if non_mana_costs_paid:
                 payment['sacrificed_perms'].extend(sacrificed_for_add)
                 logging.debug(f"Paid additional sacrifice cost for {len(sacrificed_for_add)} permanents.")
            else:
                # Rollback needed
                return False

        # Generic Discard (from "Additional Costs")
        if context.get("discard_additional"): # Expects list of hand indices/IDs
            discarded_for_add = []
            discard_indices = context["discard_additional"] # Assume indices
            if len(discard_indices) <= len(player["hand"]):
                 cards_to_discard = [player["hand"][idx] for idx in sorted(discard_indices, reverse=True)]
                 for card_to_discard in cards_to_discard:
                      if card_to_discard in player["hand"]: # Double check
                          if gs.move_card(card_to_discard, player, "hand", player, "graveyard"):
                              discarded_for_add.append(card_to_discard)
                          else: non_mana_costs_paid = False; break
                 if non_mana_costs_paid:
                      payment['discarded_cards'].extend(discarded_for_add)
                      logging.debug(f"Paid additional discard cost for {len(discarded_for_add)} cards.")
                 else: return False # Rollback needed
            else: return False # Not enough cards

        # If any non-mana cost failed, stop payment.
        if not non_mana_costs_paid:
            # Ideally, rollback all previously paid non-mana costs here.
            # For now, just return failure.
            logging.error("Failed to pay required non-mana costs.")
            return False

        # --- Pay Mana Costs ---
        # Use a mutable copy of the pool for payment attempt
        current_pool = player["mana_pool"].copy()
        conditional_pool = {k: v.copy() for k, v in player.get("conditional_mana", {}).items()} # Deep copy
        phase_pool = player.get("phase_restricted_mana", {}).copy() # Copy phase restricted mana

        # Get usable conditional mana based on context
        usable_conditional = self._get_usable_conditional_mana(conditional_pool, context)

        # Pay colored mana
        for color in ['W', 'U', 'B', 'R', 'G', 'C']:
            required = parsed_cost[color]
            if required <= 0: continue

            paid_count = 0
            # Priority: Regular -> Phase -> Conditional
            # Pay with Regular
            can_pay_reg = min(required, current_pool.get(color, 0))
            current_pool[color] -= can_pay_reg
            paid_count += can_pay_reg
            payment['colors'][color] += can_pay_reg

            # Pay with Phase
            if paid_count < required:
                 can_pay_phase = min(required - paid_count, phase_pool.get(color, 0))
                 phase_pool[color] -= can_pay_phase
                 paid_count += can_pay_phase
                 payment['phase_restricted'][color] += can_pay_phase # Use correct defaultdict init

            # Pay with Conditional
            if paid_count < required:
                 for restriction_key, pool_part in conditional_pool.items():
                      if paid_count >= required: break
                      # Check usability for *this specific color requirement*?
                      # Assume _get_usable includes all usable for the context.
                      # Use only mana marked usable earlier.
                      if color in usable_conditional.get(restriction_key, {}):
                          can_pay_cond = min(required - paid_count, pool_part.get(color, 0))
                          pool_part[color] -= can_pay_cond
                          paid_count += can_pay_cond
                          payment['conditional'][restriction_key][color] += can_pay_cond # Use correct defaultdict init

            if paid_count < required:
                logging.error(f"Insufficient {color} mana during payment (logic error). Required: {required}, Paid: {paid_count}")
                self._refund_payment(player, payment)
                return False

        # Pay hybrid mana (Use the helper function, ensure it handles pool types)
        if not self._pay_hybrid_mana_with_all_pools(player, parsed_cost['hybrid'], payment, current_pool, phase_pool, conditional_pool, usable_conditional, context):
             logging.error("Failed to pay hybrid mana")
             self._refund_payment(player, payment)
             return False

        # Pay Phyrexian mana
        paid_phy_life = 0
        for color in parsed_cost['phyrexian']:
            # Try paying with mana first (Regular -> Phase -> Conditional)
            if current_pool.get(color, 0) > 0:
                 current_pool[color] -= 1
                 payment['colors'][color] += 1
            elif phase_pool.get(color, 0) > 0:
                 phase_pool[color] -= 1
                 payment['phase_restricted'][color] += 1
            else:
                 paid_with_cond = False
                 for restriction_key, pool_part in conditional_pool.items():
                      if color in usable_conditional.get(restriction_key, {}) and pool_part.get(color, 0) > 0:
                           pool_part[color] -= 1
                           payment['conditional'][restriction_key][color] += 1
                           paid_with_cond = True
                           break
                 if not paid_with_cond:
                     # Pay with life
                     if player['life'] >= 2:
                         paid_phy_life += 2
                     else:
                         logging.error("Cannot pay Phyrexian mana with life.")
                         self._refund_payment(player, payment)
                         return False
        payment['life'] += paid_phy_life
        player['life'] -= paid_phy_life # Deduct life here, before paying generic

        # Pay snow mana
        if parsed_cost['snow'] > 0:
            if not self.pay_snow_cost(player, parsed_cost['snow']): # pay_snow_cost needs to interact with pools or directly tap
                 logging.error("Failed to pay Snow mana cost.")
                 self._refund_payment(player, payment)
                 return False
            payment['snow'] += parsed_cost['snow']

        # Pay generic mana
        generic_required = parsed_cost['generic']
        if parsed_cost['X'] > 0 and context and 'X' in context:
            generic_required += context['X'] * parsed_cost['X']

        if generic_required > 0:
            paid_generic = 0
            # Define pools and keys for payment tracking
            all_pools = [
                (current_pool, 'colors', 'C'),
                (phase_pool, 'phase_restricted', 'C'),
                *[(pool_part, f'conditional.{r_key}', 'C') for r_key, pool_part in conditional_pool.items() if 'C' in usable_conditional.get(r_key, {})],
                (current_pool, 'colors', 'W'), (current_pool, 'colors', 'U'), (current_pool, 'colors', 'B'), (current_pool, 'colors', 'R'), (current_pool, 'colors', 'G'),
                (phase_pool, 'phase_restricted', 'W'), (phase_pool, 'phase_restricted', 'U'), (phase_pool, 'phase_restricted', 'B'), (phase_pool, 'phase_restricted', 'R'), (phase_pool, 'phase_restricted', 'G'),
                 *[(pool_part, f'conditional.{r_key}', color) for r_key, pool_part in conditional_pool.items() for color in ['W', 'U', 'B', 'R', 'G'] if color in usable_conditional.get(r_key, {})]
            ]

            # Pay from pools
            for pool, payment_key, color in all_pools:
                if paid_generic >= generic_required: break
                can_pay = min(generic_required - paid_generic, pool.get(color, 0))
                if can_pay > 0:
                     pool[color] -= can_pay
                     paid_generic += can_pay
                     # Update payment structure based on key
                     if payment_key == 'colors': payment['colors'][color] += can_pay
                     elif payment_key == 'phase_restricted': payment['phase_restricted'][color] += can_pay
                     elif payment_key.startswith('conditional'):
                          r_key = payment_key.split('.')[-1]
                          payment['conditional'][r_key][color] += can_pay

            if paid_generic < generic_required:
                 logging.error(f"Failed to pay generic mana cost. Required={generic_required}, Paid={paid_generic}")
                 self._refund_payment(player, payment)
                 return False

        # --- Finalize Payment ---
        # Update player's pools with the final state from current_pool, conditional_pool, phase_pool
        player["mana_pool"] = current_pool
        player["conditional_mana"] = conditional_pool
        player["phase_restricted_mana"] = phase_pool

        # Log payment
        cost_str = self._format_mana_cost_for_logging(parsed_cost, context.get('X', 0) if 'X' in parsed_cost else 0)
        payment_str = self._format_payment_for_logging(payment)
        logging.debug(f"Paid mana cost {cost_str} with {payment_str}")
        self._cleanup_empty_conditional_mana(player)
        return True
    
    def _pay_generic_mana(self, player, amount, payment_tracker):
        """
        Pay generic mana optimally with a more adaptive color priority.
        """
        # First use colorless mana
        colorless_used = min(player["mana_pool"].get('C', 0), amount)
        player["mana_pool"]['C'] -= colorless_used
        amount -= colorless_used
        payment_tracker['C'] += colorless_used

        # Dynamically prioritize colors based on mana pool availability
        colors = sorted(['G', 'R', 'B', 'U', 'W'], key=lambda color: player["mana_pool"].get(color, 0))

        for color in colors:
            if amount <= 0:
                break

            available = player["mana_pool"].get(color, 0)
            used = min(available, amount)
            player["mana_pool"][color] -= used
            amount -= used
            payment_tracker[color] += used

        return amount
    
    def _pay_hybrid_mana_with_all_pools(self, player, hybrid_pairs, payment, current_pool, phase_pool, conditional_pool, usable_conditional, context):
        """Helper to pay hybrid costs using all available mana pools."""
        for hybrid_pair in hybrid_pairs:
            paid_hybrid = False
            # Define pool preferences (Regular > Phase > Conditional)
            pool_priority = [
                (current_pool, 'colors'),
                (phase_pool, 'phase_restricted'),
            ]
            # Add conditional pools
            for r_key, r_pool in conditional_pool.items():
                 # Only consider conditional pools usable for this context
                 usable_colors_in_pool = usable_conditional.get(r_key, {})
                 if any(color in usable_colors_in_pool for color in hybrid_pair):
                     pool_priority.append((r_pool, f'conditional.{r_key}'))

            # Try to pay from pools in priority order
            pay_options = sorted(hybrid_pair, key=lambda c: sum(p[0].get(c, 0) for p in pool_priority), reverse=True) # Prefer color with more total mana

            for color in pay_options:
                 if paid_hybrid: break
                 for pool, payment_key in pool_priority:
                     if pool.get(color, 0) > 0:
                          pool[color] -= 1
                          # Track payment correctly
                          if payment_key == 'colors': payment['colors'][color] += 1
                          elif payment_key == 'phase_restricted': payment['phase_restricted'][color] += 1
                          else: # Conditional
                               r_key = payment_key.split('.')[-1]
                               payment['conditional'][r_key][color] += 1
                          paid_hybrid = True
                          break # Paid with this color, move to next hybrid pair

            if not paid_hybrid:
                return False # Failed to pay this hybrid cost
        return True # All hybrid costs paid
    
    def add_mana_to_pool(self, player, mana_string, land_context=None, phase_restricted=False):
        """
        Advanced mana addition method that handles complex MTG land mechanics.
        
        Args:
            player: The player dictionary
            mana_string: Mana production string
            land_context: Additional context about land entry conditions
            phase_restricted: Whether this mana only lasts until end of phase
        
        Returns:
            dict: Detailed mana addition information
        """
        # Initialize result tracking
        result = {
            'added': {},
            'skipped': [],
            'conditions': {},
            'logs': []
        }
        
        # Validate and normalize input
        if not isinstance(mana_string, str):
            result['logs'].append(f"Invalid mana string type: {type(mana_string)}")
            return result
            
        # Skip empty strings
        if not mana_string:
            return result
        
        # Initialize conditional_mana if not exists
        if not hasattr(player, "conditional_mana"):
            player["conditional_mana"] = {}
        
        # Initialize phase-restricted mana if needed
        if not hasattr(player, "phase_restricted_mana"):
            player["phase_restricted_mana"] = {}
        
        # Step 1: Separate land conditions from mana text
        # Most land condition text appears before any mana symbols
        mana_parts = mana_string.split('.')
        condition_text = ""
        mana_text = ""
        
        for part in mana_parts:
            part = part.strip()
            # If this part contains mana symbols like {t}, {w}, etc.
            if re.search(r'\{[^{}]+\}', part):
                mana_text += part + " "
            else:
                condition_text += part + " "
        
        # Clean up the separated texts
        condition_text = condition_text.strip().lower()
        mana_text = mana_text.strip().lower()
        
        # Parse usage restrictions from the text
        restrictions = self._parse_mana_restrictions(condition_text + " " + mana_text)
        
        # Step 2: Extract mana symbols
        mana_tokens = re.findall(r'\{([^{}]+)\}', mana_text)
        
        # Process each token
        for token in mana_tokens:
            # Skip empty tokens
            if not token.strip():
                continue
                
            # Special case for tap symbol - explicitly check for 't' or 'T'
            if token.strip().lower() == 't':
                result['logs'].append("Tap symbol recognized (not a mana symbol)")
                continue
            
            # Process mana symbols
            clean_token = token.strip().lower()
            
            # Standard mana colors
            if clean_token in ['w', 'u', 'b', 'r', 'g', 'c']:
                color = clean_token.upper()
                
                # Add to regular mana pool if no restrictions
                if not restrictions:
                    if phase_restricted:
                        # Store in phase-restricted pool
                        if color not in player["phase_restricted_mana"]:
                            player["phase_restricted_mana"][color] = 0
                        player["phase_restricted_mana"][color] += 1
                    else:
                        # Add to normal mana pool
                        player["mana_pool"][color] = player["mana_pool"].get(color, 0) + 1
                        
                    if color not in result['added']:
                        result['added'][color] = 0
                    result['added'][color] += 1
                    
                    scope = "phase-restricted" if phase_restricted else "normal"
                    result['logs'].append(f"Added {color} mana to {scope} pool")
                else:
                    # Add to conditional mana pool
                    restriction_key = self._get_restriction_key(restrictions)
                    if restriction_key not in player["conditional_mana"]:
                        player["conditional_mana"][restriction_key] = {}
                    
                    if color not in player["conditional_mana"][restriction_key]:
                        player["conditional_mana"][restriction_key][color] = 0
                    
                    player["conditional_mana"][restriction_key][color] += 1
                    
                    if color not in result['added']:
                        result['added'][color] = 0
                    result['added'][color] += 1
                    
                    result['logs'].append(f"Added {color} mana with restriction: {restriction_key}")
                
            # Generic mana
            elif clean_token.isdigit():
                # Generic mana is always colorless
                amount = int(clean_token)
                
                # Add to regular mana pool if no restrictions
                if not restrictions:
                    if phase_restricted:
                        # Store in phase-restricted pool
                        if 'C' not in player["phase_restricted_mana"]:
                            player["phase_restricted_mana"]['C'] = 0
                        player["phase_restricted_mana"]['C'] += amount
                    else:
                        # Add to normal mana pool
                        player["mana_pool"]['C'] = player["mana_pool"].get('C', 0) + amount
                        
                    if 'C' not in result['added']:
                        result['added']['C'] = 0
                    result['added']['C'] += amount
                    
                    scope = "phase-restricted" if phase_restricted else "normal"
                    result['logs'].append(f"Added {amount} colorless mana to {scope} pool")
                else:
                    # Add to conditional mana pool
                    restriction_key = self._get_restriction_key(restrictions)
                    if restriction_key not in player["conditional_mana"]:
                        player["conditional_mana"][restriction_key] = {}
                    
                    if 'C' not in player["conditional_mana"][restriction_key]:
                        player["conditional_mana"][restriction_key]['C'] = 0
                    
                    player["conditional_mana"][restriction_key]['C'] += amount
                    
                    if 'C' not in result['added']:
                        result['added']['C'] = 0
                    result['added']['C'] += amount
                    
                    result['logs'].append(f"Added {amount} colorless mana with restriction: {restriction_key}")
                
            # Process other token types...
            
        # Add land conditions to result
        result['conditions'] = self._parse_land_conditions(mana_tokens)
        result['logs'].append(f"Processed land conditions: {result['conditions']}")
        
        return result

    def remove_mana_from_pool(self, player, amount, color='C'):
        """
        Remove mana from a player's mana pool.
        
        Args:
            player: The player dictionary
            amount: Amount of mana to remove
            color: Color of mana to remove ('W', 'U', 'B', 'R', 'G', 'C')
            
        Returns:
            int: Amount of mana actually removed
        """
        if color not in player["mana_pool"]:
            return 0
            
        available = player["mana_pool"][color]
        removed = min(available, amount)
        player["mana_pool"][color] -= removed
        
        return removed

    def clear_phase_restricted_mana(self, player):
        """
        Clear phase-restricted mana at the end of a phase.
        
        Args:
            player: The player dictionary
        """
        if hasattr(player, "phase_restricted_mana"):
            # Log the mana being cleared
            for color, amount in player["phase_restricted_mana"].items():
                if amount > 0:
                    logging.debug(f"Clearing {amount} phase-restricted {color} mana from {player['name']}'s pool")
            
            # Clear the phase-restricted mana
            player["phase_restricted_mana"] = {}
            
    def can_pay_alternative_cost(self, player, card_id, cost_type, context=None):
        """
        Check if a player can pay an alternative cost.
        
        Args:
            player: The player dictionary
            card_id: ID of the card with alternative cost
            cost_type: Type of alternative cost ('flashback', 'escape', etc.)
            context: Additional cost context
        
        Returns:
            bool: Whether the alternative cost can be paid
        """
        # Get the alternative cost
        alt_cost = self.calculate_alternative_cost(card_id, player, cost_type, context)
        if not alt_cost:
            return False
        
        # Check if the alternative cost can be paid
        return self.can_pay_mana_cost(player, alt_cost, context)

    def pay_alternative_cost(self, player, card_id, cost_type, context=None):
        """
        Pay an alternative cost for a card.
        
        Args:
            player: The player dictionary
            card_id: ID of the card with alternative cost
            cost_type: Type of alternative cost ('flashback', 'escape', etc.)
            context: Additional cost context
        
        Returns:
            bool: Whether the cost was successfully paid
        """
        # Get the alternative cost
        alt_cost = self.calculate_alternative_cost(card_id, player, cost_type, context)
        if not alt_cost:
            return False
        
        # Pay the alternative cost
        return self.pay_mana_cost(player, alt_cost, context)

    def _parse_mana_restrictions(self, text):
        """
        Parse restrictions on how mana can be spent.
        
        Args:
            text: Text to parse for restrictions
            
        Returns:
            dict: Dictionary of restrictions
        """
        restrictions = {}
        
        # Common patterns for mana restrictions
        if "spend this mana only to cast" in text:
            # Extract what the mana can be spent on
            import re
            match = re.search(r"spend this mana only to cast ([^\.]+)", text.lower())
            if match:
                target_type = match.group(1).strip()
                restrictions['cast_only'] = target_type
        
        elif "spend this mana only on" in text:
            # Extract what the mana can be spent on
            import re
            match = re.search(r"spend this mana only on ([^\.]+)", text.lower())
            if match:
                target_type = match.group(1).strip()
                restrictions['spend_only'] = target_type
        
        # Add more restriction patterns as needed
        
        return restrictions

    def _get_restriction_key(self, restrictions):
        """
        Create a string key representing the restrictions.
        
        Args:
            restrictions: Dictionary of restrictions
            
        Returns:
            str: A string key for the restrictions
        """
        if 'cast_only' in restrictions:
            return f"cast_only:{restrictions['cast_only']}"
        elif 'spend_only' in restrictions:
            return f"spend_only:{restrictions['spend_only']}"
        
        # Fallback
        return "restricted"

    def _parse_land_conditions(self, tokens):
        """
        Parse complex land entry conditions from tokens.
        
        Args:
            tokens: List of tokens to parse
        
        Returns:
            dict: Parsed land conditions
        """
        conditions = {
            'tapped': False,
            'untapped': True,
            'other_lands': None,
            'land_count_condition': None,
            'timing_restrictions': [],
            'additional_requirements': []
        }
        
        # Number word to integer mapping
        number_map = {
            'zero': 0, 'one': 1, 'two': 2, 'three': 3, 
            'four': 4, 'five': 5, 'six': 6, 'seven': 7
        }
        
        for token in tokens:
            clean_token = token.strip().lower()
            
            # Tapped condition
            if clean_token in ['t', 'tapped']:
                conditions['tapped'] = True
                conditions['untapped'] = False
            
            # Conditional land entry parsing
            if 'unless' in clean_token:
                # Parse land count conditions
                land_count_match = re.search(
                    r'unless.*?control\s*(\w+)\s*or\s*(\w+)\s*other\s*lands', 
                    clean_token
                )
                if land_count_match:
                    number_word = land_count_match.group(1)
                    comparison = land_count_match.group(2)
                    
                    # Convert number word to integer
                    land_count = number_map.get(number_word.lower(), 0)
                    
                    conditions['other_lands'] = {
                        'comparison': comparison,
                        'count': land_count
                    }
            
            # Timing restrictions
            if 'during' in clean_token or 'only' in clean_token:
                conditions['timing_restrictions'].append(clean_token)
        
        return conditions


    def _is_condition_token(self, token):
        """
        Determine if a token is a condition token.
        
        Args:
            token: Token to check
        
        Returns:
            bool: Whether the token is a condition
        """
        condition_keywords = {
            't', 'tapped', 'unless', 'during', 'only', 
            'enters', 'control', 'land', 'other', 
            'this land enters', 'land enters', 'thislandenterstapped'  # Added the combined form
        }

        # Check if token contains any condition keyword
        if any(keyword in token.lower() for keyword in condition_keywords):
            return True
        
        # Also check for specific patterns
        if 'enters' in token.lower() and 'tapped' in token.lower():
            return True
            
        return False

    def _process_mana_token(self, player, token, land_conditions):
        """
        Process a single mana token with advanced parsing.
        
        Args:
            player: Player dictionary
            token: Mana token to process
            land_conditions: Parsed land conditions
        
        Returns:
            dict: Processing result
        """
        result = {
            'added': Counter(),
            'skipped': [],
            'logs': []
        }
        
        # Clean and normalize the token
        clean_token = re.sub(r'[.,;]', '', token.lower().strip())
        
        # FIXED: Enhanced handling of tap symbol
        if clean_token == 't' or clean_token == 'tap':
            result['logs'].append("Tap symbol encountered, not a mana symbol")
            return result  # Return immediately, don't process as mana
        
        # Color name aliases
        color_aliases = {
            'white': 'w', 'blue': 'u', 'black': 'b', 
            'red': 'r', 'green': 'g', 'colorless': 'c'
        }
        
        # Apply color alias
        if clean_token in color_aliases:
            clean_token = color_aliases[clean_token]
        
        # Basic mana colors
        if clean_token in ['w', 'u', 'b', 'r', 'g', 'c']:
            # Check land entry conditions
            can_add_mana = self._check_land_entry_conditions(player, land_conditions)
            
            if can_add_mana:
                color = clean_token.upper()
                player["mana_pool"][color] += 1
                result['added'][color] += 1
                result['logs'].append(f"Added {color} mana")
            else:
                result['skipped'].append(clean_token)
                result['logs'].append(f"Mana production blocked: {land_conditions}")
            
            return result
        
        # Generic mana
        if clean_token.isdigit():
            player["mana_pool"]['C'] += int(clean_token)
            result['added']['C'] += int(clean_token)
            result['logs'].append(f"Added {clean_token} colorless mana")
            return result
        
        # Hybrid and complex mana
        if '/' in clean_token:
            hybrid_result = self._process_hybrid_mana(player, clean_token, land_conditions)
            result.update(hybrid_result)
            return result
        
        # Unrecognized token
        result['skipped'].append(token)
        result['logs'].append(f"Unrecognized mana token: {token}")
        
        return result

    def _process_hybrid_mana(self, player, token, land_conditions):
        """
        Process hybrid mana tokens.
        
        Args:
            player: Player dictionary
            token: Hybrid mana token
            land_conditions: Parsed land conditions
        
        Returns:
            dict: Processing result
        """
        result = {
            'added': Counter(),
            'skipped': [],
            'logs': []
        }
        
        parts = token.split('/')
        
        # Normalize parts
        norm_parts = [p.upper() for p in parts]
        
        # Check if both parts are valid mana symbols
        if all(p in self.mana_symbols for p in norm_parts):
            # Choose the color with more available mana
            best_color = max(norm_parts, key=lambda c: player["mana_pool"].get(c, 0))
            
            # Check land conditions
            if self._check_land_entry_conditions(player, land_conditions):
                player["mana_pool"][best_color] += 1
                result['added'][best_color] += 1
                result['logs'].append(f"Added hybrid mana: {best_color}")
            else:
                result['skipped'].append(token)
                result['logs'].append(f"Hybrid mana blocked by conditions: {land_conditions}")
        
        # Phyrexian mana handling
        elif 'P' in parts:
            phyrexian_color = next((p for p in parts if p.upper() in self.mana_symbols), None)
            if phyrexian_color:
                color = phyrexian_color.upper()
                
                # Check land conditions and life payment
                if (self._check_land_entry_conditions(player, land_conditions) and 
                    self._can_pay_phyrexian_cost(player)):
                    player["mana_pool"][color] += 1
                    result['added'][color] += 1
                    result['logs'].append(f"Added Phyrexian mana: {color}")
                else:
                    result['skipped'].append(token)
                    result['logs'].append(f"Phyrexian mana blocked")
        
        return result

    def _check_land_entry_conditions(self, player, conditions):
        """
        Comprehensive check of land entry conditions.
        
        Args:
            player: Player dictionary
            conditions: Parsed land conditions
        
        Returns:
            bool: Whether mana can be produced
        """
        gs = self.game_state
        
        # Tapped condition
        if conditions.get('tapped', False):
            return False
        
        # Other lands condition
        if conditions.get('other_lands'):
            current_lands = [
                cid for cid in player.get('battlefield', []) 
                if gs._safe_get_card(cid) and 
                hasattr(gs._safe_get_card(cid), 'type_line') and 
                'land' in gs._safe_get_card(cid).type_line
            ]
            
            condition = conditions['other_lands']
            count = condition['count']
            comparison = condition['comparison']
            
            if comparison == 'fewer':
                return len(current_lands) <= count
            elif comparison == 'more':
                return len(current_lands) >= count
        
        # Timing restrictions
        if conditions.get('timing_restrictions'):
            # Additional checks can be added here based on game state
            pass
        
        return True

    def _can_pay_phyrexian_cost(self, player):
        """
        Check if player can pay Phyrexian mana cost.
        
        Args:
            player: Player dictionary
        
        Returns:
            bool: Whether Phyrexian mana can be paid
        """
        return player.get('life', 0) >= 2

    def _log_mana_addition(self, result):
        """
        Log detailed information about mana addition.
        
        Args:
            result: Mana addition result dictionary
        """
        # Detailed logging
        if result['added']:
            added_details = [
                f"{count} {self.color_names.get(color, color)}" 
                for color, count in result['added'].items()
            ]
            logging.debug(f"Mana pool addition: {', '.join(added_details)}")
        
        # Log skipped tokens and conditions
        if result['skipped']:
            logging.info(f"Skipped mana tokens: {result['skipped']}")
        
        # Additional logging for complex conditions
        if result['conditions']:
            logging.debug(f"Land conditions: {result['conditions']}")
        
        # Log any detailed messages
        for log_entry in result['logs']:
            logging.debug(log_entry)
    
    def _determine_best_mana_color(self, player):
        """
        Determine the most needed mana color based on cards in hand.
        
        Args:
            player: The player dictionary
            
        Returns:
            str: Best color to add ('W', 'U', 'B', 'R', or 'G')
        """
        gs = self.game_state
        
        # Count required mana by color
        color_needs = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}
        
        for card_id in player["hand"]:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'mana_cost'):
                continue
                
            cost = self.parse_mana_cost(card.mana_cost)
            
            # Add colored requirements
            for color in ['W', 'U', 'B', 'R', 'G']:
                color_needs[color] += cost[color]
            
            # Add hybrid requirements (split evenly)
            for hybrid_pair in cost['hybrid']:
                for color in hybrid_pair:
                    if color in color_needs:
                        color_needs[color] += 1 / len(hybrid_pair)
            
            # Add phyrexian requirements
            for phyrexian_color in cost['phyrexian']:
                if phyrexian_color in color_needs:
                    color_needs[phyrexian_color] += 0.5  # Lower weight than direct requirements
        
        # Adjust needs based on current mana pool
        for color in color_needs:
            color_needs[color] -= player["mana_pool"].get(color, 0)
        
        # Find the color with highest need
        best_color = max(color_needs.items(), key=lambda x: x[1])[0]
        
        # If all needs are 0 or negative, default to most common color in deck
        if color_needs[best_color] <= 0:
            best_color = self._get_primary_deck_color(player)
        
        return best_color
    
    def _get_primary_deck_color(self, player):
        """
        Determine the primary color of the player's deck.
        
        Args:
            player: The player dictionary
            
        Returns:
            str: Primary color ('W', 'U', 'B', 'R', or 'G')
        """
        gs = self.game_state
        color_counts = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}
        
        # Look at all zones to determine color breakdown
        for zone in ["library", "hand", "battlefield", "graveyard"]:
            for card_id in player[zone]:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'colors'):
                    continue
                    
                for i, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                    if card.colors[i]:
                        color_counts[color] += 1
        
        # Return the most common color
        return max(color_counts.items(), key=lambda x: x[1])[0]
    
    def get_card_color_identity(self, card):
        """
        Determine the color identity of a card.
        
        Args:
            card: The card object
            
        Returns:
            set: Set of color characters in the card's color identity
        """
        if not card:
            return set()
            
        color_identity = set()
        
        # Add colors from the color indicator/array
        if hasattr(card, 'colors'):
            for i, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                if card.colors[i]:
                    color_identity.add(color)
        
        # Add colors from mana cost
        if hasattr(card, 'mana_cost'):
            cost = self.parse_mana_cost(card.mana_cost)
            
            # Add basic colored mana
            for color in ['W', 'U', 'B', 'R', 'G']:
                if cost[color] > 0:
                    color_identity.add(color)
            
            # Add hybrid mana colors
            for hybrid_pair in cost['hybrid']:
                for color in hybrid_pair:
                    if color in ['W', 'U', 'B', 'R', 'G']:
                        color_identity.add(color)
            
            # Add phyrexian mana colors
            for phyrexian_color in cost['phyrexian']:
                if phyrexian_color in ['W', 'U', 'B', 'R', 'G']:
                    color_identity.add(phyrexian_color)
        
        # Add colors from oracle text (mana symbols)
        if hasattr(card, 'oracle_text'):
            for color in ['W', 'U', 'B', 'R', 'G']:
                if f"{{{color}}}" in card.oracle_text:
                    color_identity.add(color)
        
        return color_identity
    
    def get_deck_color_identity(self, deck, card_db):
        """
        Determine the color identity of a deck.
        
        Args:
            deck: List of card IDs
            card_db: Card database
            
        Returns:
            set: Color identity of the deck
        """
        color_identity = set()
        
        for card_id in deck:
            card = card_db.get(card_id)
            if card:
                card_colors = self.get_card_color_identity(card)
                color_identity.update(card_colors)
        
        return color_identity
    
    def format_color_identity(self, color_identity):
        """
        Format a color identity set as a human-readable string.
        
        Args:
            color_identity: Set of color characters
            
        Returns:
            str: Formatted color identity
        """
        if not color_identity:
            return "Colorless"
            
        # Standard color order
        color_order = ['W', 'U', 'B', 'R', 'G']
        
        # Sort colors in standard order
        sorted_colors = [c for c in color_order if c in color_identity]
        
        # Map to common color combinations
        color_combinations = {
            'W': "Mono-White",
            'U': "Mono-Blue",
            'B': "Mono-Black",
            'R': "Mono-Red",
            'G': "Mono-Green",
            'WU': "Azorius",
            'WB': "Orzhov",
            'UB': "Dimir",
            'UR': "Izzet",
            'BR': "Rakdos",
            'BG': "Golgari",
            'RG': "Gruul",
            'RW': "Boros",
            'GW': "Selesnya",
            'GU': "Simic",
            'WUB': "Esper",
            'UBR': "Grixis",
            'BRG': "Jund",
            'RGW': "Naya",
            'GWU': "Bant",
            'WBG': "Abzan",
            'URW': "Jeskai",
            'BRW': "Mardu",
            'GUB': "Sultai",
            'RGU': "Temur",
            'WUBR': "Non-Green",
            'UBRG': "Non-White",
            'BRGW': "Non-Blue",
            'RGWU': "Non-Black",
            'GWUB': "Non-Red",
            'WUBRG': "Five-Color"
        }
        
        color_key = ''.join(sorted_colors)
        return color_combinations.get(color_key, f"{len(color_key)}-Color")
    
    def get_mana_curve(self, deck, card_db):
        """
        Calculate the mana curve of a deck.
        
        Args:
            deck: List of card IDs
            card_db: Card database
            
        Returns:
            dict: Mana curve counts by CMC
        """
        mana_curve = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, "7+": 0}
        
        for card_id in deck:
            card = card_db.get(card_id)
            if not card or not hasattr(card, 'cmc') or ('land' in card.card_types if hasattr(card, 'card_types') else False):
                continue
                
            cmc = card.cmc
            
            if cmc <= 6:
                mana_curve[cmc] += 1
            else:
                mana_curve["7+"] += 1
        
        return mana_curve
    
    def _format_mana_cost_for_logging(self, parsed_cost, x_value=0):
        """Format a parsed mana cost for logging."""
        parts = []
        
        # Add generic mana
        if parsed_cost['generic'] > 0:
            parts.append(f"{parsed_cost['generic']} generic")
        
        # Add X cost
        if parsed_cost['X'] > 0:
            parts.append(f"X={x_value}")
        
        # Add colored mana
        for color in ['W', 'U', 'B', 'R', 'G', 'C']:
            if parsed_cost[color] > 0:
                parts.append(f"{parsed_cost[color]} {self.color_names[color]}")
        
        # Add hybrid mana
        for hybrid_pair in parsed_cost['hybrid']:
            parts.append(f"1 hybrid ({'/'.join(hybrid_pair)})")
        
        # Add phyrexian mana
        for phyrexian_color in parsed_cost['phyrexian']:
            parts.append(f"1 phyrexian {self.color_names[phyrexian_color]}")
        
        return ", ".join(parts)
    