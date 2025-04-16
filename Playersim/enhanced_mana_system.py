import re
import logging
from collections import Counter
from collections import defaultdict

class EnhancedManaSystem:
    """Advanced mana handling system that properly implements MTG mana rules."""
        # Define card types and keywords directly here to avoid circular imports
    ALL_CARD_TYPES = [
        'creature', 'artifact', 'enchantment', 'land', 'planeswalker',
        'instant', 'sorcery', 'battle', 'conspiracy', 'dungeon',
        'phenomenon', 'plane', 'scheme', 'vanguard', 'class', 'room'
    ]
    
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
        Extract cost modification effects from a permanent. (Enhanced Parsing & Validation)

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

        # Ensure necessary objects and attributes exist
        if not permanent or not hasattr(permanent, 'oracle_text') or not target_card:
            return effects

        # Get info for conditional checks - use getattr for safety
        oracle_text = getattr(permanent, 'oracle_text', '').lower()
        target_card_types = getattr(target_card, 'card_types', [])
        target_card_subtypes = getattr(target_card, 'subtypes', [])
        target_card_colors = getattr(target_card, 'colors', [0, 0, 0, 0, 0]) # Default to colorless if missing
        perm_name = getattr(permanent, 'name', f"Card {permanent_id}")

        # --- Regex to find cost modifications ---
        # Pattern: "(Qualifier) spells (Scope)? cost {Amount} (less|more)"
        # Qualifier examples: "Creature", "Red", "Artifact", "" (any spell)
        # Scope examples: "you cast", "your opponents cast", "" (applies to all)
        # Amount: N or Color Symbol (WUBRGC)
        cost_pattern = r"(?:^|\n|;|\.)\s*([a-zA-Z\s\-,]+?)?\s*spells?\s*(you cast|your opponents cast)?\s*cost\s+\{(\w+)\}\s+(less|more)"
        # Example non-spell cost pattern (Needs more specific rules context)
        # ability_cost_pattern = r"activated abilities cost ... less/more"

        matches = re.finditer(cost_pattern, oracle_text)
        for match in matches:
            qualifier, subject_scope, amount_str, direction = match.groups()
            qualifier = qualifier.strip() if qualifier else "any"
            subject_scope = subject_scope.strip() if subject_scope else ""
            amount_str = amount_str.strip().upper() # Normalize amount

            # Check if effect applies based on who is casting
            if subject_scope == "you cast" and not is_controller: continue
            if subject_scope == "your opponents cast" and is_controller: continue

            # Validate amount and determine if it's generic or colored
            amount = 0
            color_specific = None
            is_generic_modifier = False
            if amount_str.isdigit():
                amount = int(amount_str)
                is_generic_modifier = True
            elif amount_str in self.mana_symbols: # W, U, B, R, G, C
                color_specific = amount_str
                amount = 1 # e.g., {W} less means 1 less W required
            else:
                logging.warning(f"Invalid amount '{amount_str}' in cost mod from {perm_name}. Skipping.")
                continue # Skip if amount isn't number or single color symbol

            # --- Check qualifier against target card ---
            applies = True
            qualifier_lower = qualifier.lower()

            # Handle common qualifiers more precisely
            color_words = ["white", "blue", "black", "red", "green", "colorless", "multicolored"]
            # Use our own constant instead of Card.ALL_CARD_TYPES
            card_type_words = self.ALL_CARD_TYPES
            all_qualifiers = qualifier_lower.split() # Handle multi-word qualifiers like "artifact creature"

            found_match_for_qualifier = False
            is_negation = False # e.g., "noncreature"
            effective_qualifier = qualifier_lower

            if qualifier_lower == "any" or qualifier_lower == "spells":
                found_match_for_qualifier = True # Applies to any spell
            else:
                 if qualifier_lower.startswith("non"):
                      is_negation = True
                      effective_qualifier = qualifier_lower[3:] # e.g., "creature" from "noncreature"

                 # Check Colors
                 matched_color = False
                 color_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
                 if effective_qualifier == "colorless":
                      matched_color = sum(target_card_colors) == 0
                 elif effective_qualifier == "multicolored":
                      matched_color = sum(target_card_colors) > 1
                 elif effective_qualifier in color_map:
                      matched_color = bool(target_card_colors[color_map[effective_qualifier]])

                 if is_negation: matched_color = not matched_color
                 if matched_color: found_match_for_qualifier = True

                 # Check Card Types (if not already matched by color)
                 if not found_match_for_qualifier:
                      matched_type = effective_qualifier in target_card_types
                      if is_negation: matched_type = not matched_type
                      if matched_type: found_match_for_qualifier = True

                 # Check Subtypes (if not already matched by color or type)
                 # Simple check - assumes qualifier is a single subtype word
                 if not found_match_for_qualifier:
                     matched_subtype = effective_qualifier in target_card_subtypes
                     # Negation doesn't typically apply to subtypes like this.
                     if matched_subtype: found_match_for_qualifier = True


            # If no match found for the specific qualifier, the effect doesn't apply
            if not found_match_for_qualifier and qualifier_lower not in ["any", "spells"]:
                 applies = False

            # Construct and add effect if it applies
            if applies:
                 effect = {'type': 'reduction' if direction == 'less' else 'increase', 'amount': amount, 'source': perm_name}
                 if color_specific:
                     # Rule 609.4b: Cannot reduce colored costs below zero.
                     # Rule 609.4a: Cost increases add to the cost.
                     # Reducing specific colors is complex. Simpler: Apply reduction to generic if possible, else ignore.
                     # Let's assume reduction ONLY applies if that color pip exists.
                     # Increasing adds generic for now (simpler than adding pips).
                     if effect['type'] == 'reduction':
                          effect['applies_to'] = 'specific_color_pip' # Signal it reduces pips
                     else: # Increase
                          effect['applies_to'] = 'generic' # Increase adds generic cost for simplicity
                          effect['amount'] = amount # Generic amount is 1 for a colored increase {W}
                     effect['color'] = color_specific
                 else: # Generic modifier amount
                     effect['applies_to'] = 'generic'
                 effects.append(effect)
                 logging.debug(f"Found cost effect: {direction} {amount_str} ({qualifier} spells) from {perm_name}")

        return effects

    def _get_self_cost_modification_effects(self, card, player, context=None):
        """
        Get cost modification effects from the card itself. (Enhanced Context Handling)

        Args:
            card: The card being cast
            player: The player casting the card
            context: Optional context for special cases (e.g., Convoke, Delve choices)

        Returns:
            list: List of cost modification effect dictionaries
        """
        effects = []
        if not card or not hasattr(card, 'oracle_text'):
            return effects
        if context is None: context = {}

        oracle_text = card.oracle_text.lower()
        card_name = getattr(card, 'name', 'Unknown Card')

        # Check for affinity
        affinity_match = re.search(r"affinity for (\w+)", oracle_text)
        if affinity_match:
            affinity_type = affinity_match.group(1) # e.g., 'artifacts', 'planeswalkers'
            count = 0
            if affinity_type == "artifacts":
                count = sum(1 for cid in player["battlefield"]
                              if 'artifact' in getattr(self.game_state._safe_get_card(cid), 'card_types', []))
            # Add other affinity types if needed (islands, planeswalkers...)
            if count > 0:
                effects.append({'type': 'reduction', 'amount': count, 'applies_to': 'generic', 'source': f'{card_name} Affinity'})
                logging.debug(f"Applying Affinity reduction: {count} generic for {card_name}")


        # Check for convoke - USE context['convoke_creatures'] which should be list of IDs/indices
        if "convoke" in oracle_text and context.get("convoke_creatures"):
            convoke_list = context["convoke_creatures"]
            # ManaSystem doesn't know creature colors/types here. Assume GameState/ActionHandler validated.
            # Simple version: Reduce generic by count. Full version needs color handling during payment.
            convoke_reduction = len(convoke_list)
            if convoke_reduction > 0:
                 # The *effect* is reducing the cost now, actual tapping happens during payment.
                 effects.append({'type': 'reduction', 'amount': convoke_reduction, 'applies_to': 'generic', 'source': f'{card_name} Convoke'})
                 logging.debug(f"Applying Convoke cost reduction: {convoke_reduction} generic for {card_name}")
                 # We need to signal mana system how much of each *color* can be paid via convoke during payment phase
                 # Adding this info to the effect is one way.
                 # This is complex. Let's defer colored cost reduction via convoke to the payment step for now.

        # Check for delve - USE context['delve_cards'] which should be list of GY indices/IDs
        if "delve" in oracle_text and context.get("delve_cards"):
            delve_list = context["delve_cards"]
            delve_reduction = len(delve_list)
            if delve_reduction > 0:
                effects.append({'type': 'reduction', 'amount': delve_reduction, 'applies_to': 'generic', 'source': f'{card_name} Delve'})
                logging.debug(f"Applying Delve cost reduction: {delve_reduction} generic for {card_name}")
                # Actual exiling happens during payment step based on context.

        # Check for Improvise - USE context['improvise_artifacts']
        if "improvise" in oracle_text and context.get("improvise_artifacts"):
             improvise_list = context["improvise_artifacts"]
             improvise_reduction = len(improvise_list)
             if improvise_reduction > 0:
                 effects.append({'type': 'reduction', 'amount': improvise_reduction, 'applies_to': 'generic', 'source': f'{card_name} Improvise'})
                 logging.debug(f"Applying Improvise cost reduction: {improvise_reduction} generic for {card_name}")
                 # Actual tapping happens during payment step based on context.

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
        Refund all costs (mana, life, tapped permanents, etc.) from a failed payment.

        Args:
            player: The player dictionary
            payment: Payment tracking dictionary
        """
        logging.debug(f"Refunding payment: {payment}")

        # Refund regular mana
        for color, amount in payment['colors'].items():
            if amount > 0:
                player["mana_pool"][color] = player["mana_pool"].get(color, 0) + amount

        # Refund conditional mana
        for restriction_key, colors in payment['conditional'].items():
            if restriction_key not in player.get("conditional_mana", {}):
                player["conditional_mana"][restriction_key] = {}
            for color, amount in colors.items():
                if amount > 0:
                    player["conditional_mana"][restriction_key][color] = player["conditional_mana"][restriction_key].get(color, 0) + amount

        # Refund phase-restricted mana
        for color, amount in payment['phase_restricted'].items():
             if amount > 0:
                  player["phase_restricted_mana"][color] = player["phase_restricted_mana"].get(color, 0) + amount


        # Refund life paid for Phyrexian mana
        if payment['life'] > 0:
            player["life"] += payment['life']

        # Refund snow mana? (Harder, involves untapping) - For now, assume snow payment succeeds if checked.

        # Untap creatures tapped for Convoke
        if payment['tapped_creatures']:
            for creature_id in payment['tapped_creatures']:
                 # Use GameState's untap method if available and safe
                 if hasattr(self.game_state, 'untap_permanent'):
                     self.game_state.untap_permanent(creature_id, player)
                 elif creature_id in player.get("tapped_permanents", set()): # Fallback
                     player["tapped_permanents"].remove(creature_id)

        # Return exiled cards for Delve (to Graveyard)
        if payment['exiled_cards']:
            for card_id in payment['exiled_cards']:
                if card_id in player.get("exile", []):
                    player["exile"].remove(card_id)
                    player.setdefault("graveyard", []).append(card_id)

        # Return sacrificed permanents for Additional Costs (to Battlefield - complex state reset needed)
        # Basic rollback: Just put back on battlefield, needs state reset (tapped, counters etc.)
        if payment['sacrificed_perms']:
            for card_id in payment['sacrificed_perms']:
                if card_id in player.get("graveyard", []): # Assuming it went to GY
                     player["graveyard"].remove(card_id)
                     player.setdefault("battlefield", []).append(card_id)
                     # TODO: Full state reset for the returned permanent is needed here

        # Return discarded cards for Additional Costs (to Hand)
        if payment['discarded_cards']:
            for card_id in payment['discarded_cards']:
                 if card_id in player.get("graveyard", []): # Assuming it went to GY
                     player["graveyard"].remove(card_id)
                     player.setdefault("hand", []).append(card_id)

        logging.debug("Payment refund completed.")
        # No need to clean up empty conditional mana here, done after successful payment

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
        Now handles non-mana costs first and includes rollback. (Complete Implementation)
        """
        if context is None: context = {}
        gs = self.game_state

        # Track payment details - EXPANDED
        payment = {
            'colors': defaultdict(int), 'conditional': defaultdict(lambda: defaultdict(int)),
            'phase_restricted': defaultdict(int),
            'life': 0, 'snow': 0,
            'tapped_creatures': [], 'exiled_cards': [],
            'sacrificed_perms': [], 'discarded_cards': [],
        }
        card_id = context.get('card_id') # Optional: ID of card being cast/activated

        # --- Determine Final Mana Cost ---
        try:
            if hasattr(cost, 'mana_cost'): # Card Object
                card_obj = cost # Keep reference if needed
                card_id = getattr(cost, 'card_id', card_id) # Get/update card_id
                parsed_cost_base = self.parse_mana_cost(cost.mana_cost)
                final_cost = self.apply_cost_modifiers(player, parsed_cost_base, card_id, context)
            elif isinstance(cost, str): # String Cost
                parsed_cost_base = self.parse_mana_cost(cost)
                final_cost = self.apply_cost_modifiers(player, parsed_cost_base, card_id, context)
            elif isinstance(cost, dict): # Pre-parsed/Modified Cost Dict
                # Ensure all keys are present
                default_keys = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0, 'generic': 0, 'X': 0, 'hybrid': [], 'phyrexian': [], 'snow': 0}
                cost_dict_checked = {**default_keys, **cost}
                final_cost = self.apply_cost_modifiers(player, cost_dict_checked.copy(), card_id, context)
            else:
                logging.error(f"Invalid cost type provided to pay_mana_cost: {type(cost)}")
                return False
        except Exception as cost_calc_e:
             logging.error(f"Error calculating final cost: {cost_calc_e}", exc_info=True)
             return False

        # --- Affordability Check (Before Paying Anything) ---
        # Create a temporary cost dict reflecting context costs that reduce mana needs
        cost_for_check = final_cost.copy()
        check_context_costs = 0
        if "convoke_creatures" in context: check_context_costs += len(context["convoke_creatures"])
        if "delve_cards" in context: check_context_costs += len(context["delve_cards"])
        if "improvise_artifacts" in context: check_context_costs += len(context["improvise_artifacts"])
        cost_for_check['generic'] = max(0, cost_for_check['generic'] - check_context_costs)

        if not self.can_pay_mana_cost(player, cost_for_check, context):
            cost_str = self._format_mana_cost_for_logging(final_cost, context.get('X', 0) if 'X' in final_cost else 0)
            card_name_log = getattr(gs._safe_get_card(card_id), 'name', 'spell/ability') if card_id else 'spell/ability'
            logging.warning(f"Cannot afford final cost {cost_str} for {card_name_log}")
            return False

        # --- Execute Non-Mana Costs specified in context FIRST ---
        non_mana_costs_paid_successfully = True
        try:
            # Convoke/Improvise: Tap creatures/artifacts provided in context
            tapped_for_cost = []
            convoke_list = context.get("convoke_creatures", [])
            improvise_list = context.get("improvise_artifacts", [])
            for identifier in convoke_list + improvise_list:
                perm_id = gs._resolve_permanent_identifier(player, identifier) # Assumes GS has this helper
                if not perm_id or perm_id in player.get("tapped_permanents", set()): # Cannot tap invalid or already tapped
                     reason = "already tapped" if perm_id and perm_id in player.get("tapped_permanents", set()) else "invalid identifier"
                     raise ValueError(f"Convoke/Improvise payment failed: {perm_id} ({reason}).")
                if hasattr(gs, 'tap_permanent') and gs.tap_permanent(perm_id, player):
                    tapped_for_cost.append(perm_id)
                else:
                    raise ValueError(f"Convoke/Improvise payment failed: Could not tap {perm_id}.")
            if tapped_for_cost: payment['tapped_creatures'] = tapped_for_cost # Record successful taps


            # Delve/Escape: Exile cards from GY provided in context
            exiled_for_cost = []
            delve_indices = context.get("delve_cards", []) # Expect list of GY indices
            escape_indices = context.get("escape_cards", []) # Expect list of GY indices
            all_indices_to_exile = sorted(list(set(delve_indices + escape_indices)), reverse=True) # Unique indices, descending

            valid_indices = [idx for idx in all_indices_to_exile if isinstance(idx, int) and 0 <= idx < len(player["graveyard"])]
            if len(valid_indices) != len(all_indices_to_exile):
                raise ValueError("Delve/Escape payment failed: Invalid GY indices provided.")

            # Check if enough cards *remain* in GY if indices overlap (unlikely but possible)
            if len(valid_indices) > len(player.get("graveyard",[])):
                raise ValueError("Delve/Escape payment failed: Not enough cards in graveyard after index validation.")

            gy_cards_to_exile_ids = [player["graveyard"][idx] for idx in valid_indices]
            temp_gy = player["graveyard"][:] # Operate on a copy temporarily
            exiled_this_step = []

            for idx in valid_indices:
                try:
                     exile_id = temp_gy.pop(idx) # Remove from copy based on original index
                     # Use move_card for robustness (e.g., Leyline of the Void)
                     if not gs.move_card(exile_id, player, "graveyard_implicit", player, "exile", cause="cost_exile"):
                          raise ValueError(f"Delve/Escape payment failed: Could not exile {exile_id}.")
                     exiled_this_step.append(exile_id) # Track successfully exiled
                except IndexError:
                     # This might happen if indices weren't unique or GY changed unexpectedly
                     raise ValueError(f"Delve/Escape payment failed: Index {idx} became invalid during removal.")

            if exiled_this_step:
                 payment['exiled_cards'] = exiled_this_step
                 player["graveyard"] = temp_gy # Commit removal from actual GY list

            # Emerge Sacrifice (Check context for ID already sacrificed by game logic)
            if context.get("emerge_sacrificed_id"):
                 payment['sacrificed_perms'].append(context["emerge_sacrificed_id"]) # Record

            # Additional Costs (Sacrifice, Discard) from context
            # Assume these lists contain identifiers (indices or card IDs)
            sac_additional = context.get("sacrifice_additional", [])
            discard_additional = context.get("discard_additional", [])

            for identifier in sac_additional:
                 sac_id = gs._resolve_permanent_identifier(player, identifier)
                 if not sac_id or sac_id not in player.get("battlefield",[]):
                      raise ValueError(f"Additional Sacrifice payment failed: Invalid/missing permanent {identifier}.")
                 # Use move_card; if it fails, raise error
                 if not gs.move_card(sac_id, player, "battlefield", player, "graveyard", cause="additional_cost_sacrifice"):
                      raise ValueError(f"Additional Sacrifice payment failed: move_card failed for {sac_id}.")
                 payment['sacrificed_perms'].append(sac_id)

            # Process discard indices descending to avoid index issues
            discard_ids_to_discard = []
            valid_discard_indices = [idx for idx in sorted(discard_additional, reverse=True) if isinstance(idx, int) and 0 <= idx < len(player.get("hand",[]))]
            if len(valid_discard_indices) != len(discard_additional):
                 raise ValueError(f"Additional Discard payment failed: Invalid hand indices {discard_additional}.")

            for idx in valid_discard_indices:
                 discard_id = player["hand"].pop(idx) # Remove from hand
                 # Use move_card to put into graveyard
                 if not gs.move_card(discard_id, player, "hand_implicit", player, "graveyard", cause="additional_cost_discard"):
                      # If move fails, try to put card back in hand - difficult state
                      player["hand"].insert(idx, discard_id) # Put back at original index? Risky.
                      raise ValueError(f"Additional Discard payment failed: move_card failed for {discard_id}.")
                 payment['discarded_cards'].append(discard_id)

        except ValueError as non_mana_error:
             logging.warning(f"Failed to pay non-mana costs: {non_mana_error}")
             self._refund_payment(player, payment) # Rollback costs paid so far
             return False
        except Exception as non_mana_e:
             logging.error(f"Error paying non-mana costs: {non_mana_e}", exc_info=True)
             self._refund_payment(player, payment)
             return False

        # --- Pay Mana Costs ---
        mana_payment_successful = False
        # Use a mutable copy of the pools for the payment attempt
        current_pool = player["mana_pool"].copy()
        conditional_pool = {k: v.copy() for k, v in player.get("conditional_mana", {}).items()}
        phase_pool = player.get("phase_restricted_mana", {}).copy()

        try:
            usable_conditional = self._get_usable_conditional_mana(conditional_pool, context)

            # Pay colored mana first (WUBRGC)
            for color in ['W', 'U', 'B', 'R', 'G', 'C']:
                required = final_cost.get(color, 0)
                if required <= 0: continue
                paid_count = 0
                # Priority: Regular -> Phase -> Conditional
                paid_reg = min(required, current_pool.get(color, 0))
                if paid_reg > 0: current_pool[color] -= paid_reg; payment['colors'][color] += paid_reg; paid_count += paid_reg;
                if paid_count < required:
                    paid_phase = min(required - paid_count, phase_pool.get(color, 0))
                    if paid_phase > 0: phase_pool[color] -= paid_phase; payment['phase_restricted'][color] += paid_phase; paid_count += paid_phase;
                if paid_count < required:
                    for r_key, pool_part in conditional_pool.items():
                        if paid_count >= required: break
                        if self._can_use_conditional_mana(r_key, context) and pool_part.get(color, 0) > 0:
                            paid_cond = min(required - paid_count, pool_part[color])
                            pool_part[color] -= paid_cond; payment['conditional'][r_key][color] += paid_cond; paid_count += paid_cond;

                if paid_count < required:
                    raise ValueError(f"Insufficient {color} mana during payment (Needed {required}, Found {paid_count} usable)")

            # Pay hybrid mana (including 2-brid)
            if not self._pay_hybrid_mana_with_all_pools(player, final_cost.get('hybrid', []), payment, current_pool, phase_pool, conditional_pool, usable_conditional, context):
                 raise ValueError("Failed to pay hybrid mana")

            # Pay Phyrexian mana
            phy_colors_to_pay = list(final_cost.get('phyrexian', []))
            paid_phy_life = 0
            phy_success = True
            # Try mana first from all pools
            remaining_phy_to_pay_life = []
            for color in phy_colors_to_pay:
                 paid_with_mana = False
                 if current_pool.get(color, 0) > 0: current_pool[color] -= 1; payment['colors'][color] += 1; paid_with_mana = True; continue;
                 if phase_pool.get(color, 0) > 0: phase_pool[color] -= 1; payment['phase_restricted'][color] += 1; paid_with_mana = True; continue;
                 for r_key, pool_part in conditional_pool.items():
                     if self._can_use_conditional_mana(r_key, context) and pool_part.get(color, 0) > 0:
                         pool_part[color] -= 1; payment['conditional'][r_key][color] += 1; paid_with_mana = True; break;
                 if not paid_with_mana: remaining_phy_to_pay_life.append(color)

            # Pay remaining with life
            life_needed = len(remaining_phy_to_pay_life) * 2
            if player['life'] >= life_needed:
                 paid_phy_life = life_needed
                 payment['life'] += paid_phy_life
                 # COMMIT LIFE PAYMENT HERE
                 player['life'] -= paid_phy_life
                 if paid_phy_life > 0: logging.debug(f"Paid {paid_phy_life} life for Phyrexian mana.")
            else:
                phy_success = False
                raise ValueError(f"Cannot pay Phyrexian mana with life (Need {life_needed}, Have {player['life']})")

            # Pay snow mana
            if final_cost.get('snow', 0) > 0:
                # pay_snow_cost taps permanents and adds mana to pools. This needs integration.
                # Simplified: Assume pay_snow_cost works if can_pay was true.
                # Needs revision: Pay snow should consume specific mana from tapped snow sources.
                if not self.pay_snow_cost(player, final_cost['snow']): # CAUTION: Modifies player state directly
                    raise ValueError("Failed to pay Snow mana cost")
                payment['snow'] += final_cost['snow'] # Track that snow was paid


            # Pay generic mana (and X cost)
            generic_required = final_cost.get('generic', 0)
            x_value = context.get('X', 0) if final_cost.get('X',0) > 0 else 0
            generic_required += x_value * final_cost.get('X',0)

            if generic_required > 0:
                remaining_generic = self._pay_generic_mana_with_all_pools(player, generic_required, payment, current_pool, phase_pool, conditional_pool, usable_conditional, context)
                if remaining_generic > 0:
                    raise ValueError(f"Failed to pay generic mana cost. Required={generic_required}, Paid={generic_required-remaining_generic}, Short={remaining_generic}")

            mana_payment_successful = True # If no error thrown

        except ValueError as mana_error:
            logging.error(f"Failed to pay mana costs: {mana_error}")
            self._refund_payment(player, payment) # *** ROLLBACK EVERYTHING ***
            return False
        except Exception as mana_e:
             logging.error(f"Error paying mana costs: {mana_e}", exc_info=True)
             self._refund_payment(player, payment) # *** ROLLBACK EVERYTHING ***
             return False


        # --- Finalize Payment ---
        if mana_payment_successful:
            # COMMIT CHANGES TO PLAYER STATE
            player["mana_pool"] = current_pool
            player["conditional_mana"] = conditional_pool
            player["phase_restricted_mana"] = phase_pool
            # Life already deducted during phyrexian check

            # Log payment
            cost_str = self._format_mana_cost_for_logging(final_cost, context.get('X', 0) if 'X' in final_cost else 0)
            payment_str = self._format_payment_for_logging(payment)
            card_name_log = getattr(gs._safe_get_card(card_id), 'name', 'spell/ability') if card_id else 'spell/ability'
            logging.debug(f"Paid cost {cost_str} for {card_name_log} with {payment_str}")
            self._cleanup_empty_conditional_mana(player)
            return True
        else:
             # This path might be reached if non-mana costs failed. Rollback handled there.
             logging.warning("pay_mana_cost reached end with mana_payment_successful=False.")
             return False
         
    def _pay_generic_mana_with_all_pools(self, player, amount, payment, current_pool, phase_pool, conditional_pool, usable_conditional, context):
         """Pay generic mana optimally using all pools."""
         # 1. Regular Colorless
         used = min(amount, current_pool.get('C', 0))
         if used > 0: current_pool['C'] -= used; payment['colors']['C'] += used; amount -= used;
         # 2. Phase Colorless
         used = min(amount, phase_pool.get('C', 0))
         if used > 0: phase_pool['C'] -= used; payment['phase_restricted']['C'] += used; amount -= used;
         # 3. Conditional Colorless
         for r_key, pool_part in conditional_pool.items():
             if amount <= 0: break
             if self._can_use_conditional_mana(r_key, context) and pool_part.get('C', 0) > 0:
                 used = min(amount, pool_part['C'])
                 pool_part['C'] -= used; payment['conditional'][r_key]['C'] += used; amount -= used;

         # Use colored mana pools if needed
         # Prioritize colors with more mana first? Or least valuable? Let's use availability.
         colors = sorted(['W', 'U', 'B', 'R', 'G'], key=lambda c: current_pool.get(c,0) + phase_pool.get(c,0) + usable_conditional.get(c,0), reverse=True)

         for color in colors:
             if amount <= 0: break
             # Use Regular Color
             used = min(amount, current_pool.get(color, 0))
             if used > 0: current_pool[color] -= used; payment['colors'][color] += used; amount -= used;
             if amount <= 0: break
             # Use Phase Color
             used = min(amount, phase_pool.get(color, 0))
             if used > 0: phase_pool[color] -= used; payment['phase_restricted'][color] += used; amount -= used;
             if amount <= 0: break
             # Use Conditional Color
             for r_key, pool_part in conditional_pool.items():
                 if amount <= 0: break
                 if self._can_use_conditional_mana(r_key, context) and pool_part.get(color, 0) > 0:
                     used = min(amount, pool_part[color])
                     pool_part[color] -= used; payment['conditional'][r_key][color] += used; amount -= used;

         return amount # Return remaining unpaid amount
    
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
    