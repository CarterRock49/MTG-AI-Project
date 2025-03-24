import logging
import re

class ReplacementEffectSystem:
    """
    System for handling replacement effects in MTG.
    Replacement effects change how events happen, replacing them with different events.
    Examples: "If you would draw a card, instead..." or "If a creature would die, instead..."
    """
    EVENT_TYPES = [
        'DRAW', 'DAMAGE', 'DIES', 'ENTERS_BATTLEFIELD', 'CAST_SPELL', 'TAP', 
        'UNTAP', 'ATTACK', 'BLOCK', 'LIFE_GAIN', 'LIFE_LOSS', 'DISCARD',
        'CREATE_TOKEN', 'ADD_COUNTER', 'SHUFFLE', 'SCRY', 'REVEAL', 'SEARCH',
        'MILL', 'EXILE', 'DESTROY', 'COUNTER_SPELL', 'TAPPED', 'UNTAPPED',
        'SACRIFICE', 'RETURN_TO_HAND', 'PAY_MANA', 'PAY_LIFE', 'PHASE_CHANGE'
    ]
    
    def __init__(self, game_state):
        self.game_state = game_state
        self.active_effects = []  # List of active replacement effects
        self.effect_counter = 0   # For assigning unique IDs
        self.effect_index = {}  # Map of event_type to list of applicable effects
        
    def register_effect(self, effect_data):
        """Register a replacement effect with improved indexing."""
        effect_id = f"replace_{self.effect_counter}"
        self.effect_counter += 1
        
        # Add current turn for duration tracking
        effect_data['start_turn'] = self.game_state.turn
        effect_data['effect_id'] = effect_id
        
        # Record the current active player for "until_my_next_turn" duration
        if effect_data.get('duration') == 'until_my_next_turn':
            active_player = self.game_state._get_active_player()
            effect_data['controller_is_p1'] = (active_player == self.game_state.p1)
        
        self.active_effects.append(effect_data)
        
        # Update the effect index
        event_type = effect_data['event_type']
        if event_type not in self.effect_index:
            self.effect_index[event_type] = []
        self.effect_index[event_type].append(effect_data)
        
        logging.debug(f"Registered replacement effect {effect_id} for {effect_data['event_type']} events")
        
        return effect_id
    

    def remove_effect(self, effect_id):
        """Remove a replacement effect with index update."""
        # Get the effect before removal to update index
        to_remove = next((e for e in self.active_effects if e.get('effect_id') == effect_id), None)
        
        if to_remove:
            # Remove from index
            event_type = to_remove.get('event_type')
            if event_type in self.effect_index:
                self.effect_index[event_type] = [e for e in self.effect_index[event_type] 
                                            if e.get('effect_id') != effect_id]
        
        # Remove from main list
        self.active_effects = [effect for effect in self.active_effects 
                            if effect.get('effect_id') != effect_id]
    
    def handle_enter_battlefield_replacements(self, card_id, controller):
        """
        Handle replacement effects for a card entering the battlefield.
        This should be called BEFORE the card is actually added to the battlefield.
        
        Args:
            card_id: The ID of the card entering the battlefield
            controller: The player who will control the card
            
        Returns:
            modified_card_id: Potentially modified card ID (if replaced by another card)
            applied_effects: List of effects that were applied
        """
        # Create context for the enter battlefield event
        context = {
            'card_id': card_id,
            'controller': controller,
            'zone_from': None,  # This would typically be set by the caller
            'zone_to': 'battlefield'
        }
        
        # Apply any replacement effects
        modified_context, was_replaced = self.apply_replacements('ENTERS_BATTLEFIELD', context)
        
        # If the card was replaced, return the new card ID
        if was_replaced and 'card_id' in modified_context:
            return modified_context['card_id'], ['replacement']
        
        return card_id, []
    
    def register_card_replacement_effects(self, card_id, player):
        """
        Scan a card's text for replacement effects and register them.
        
        Args:
            card_id: ID of the card with potential replacement effects
            player: Player who controls the card
            
        Returns:
            list: IDs of registered replacement effects
        """
        card = self.game_state._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'):
            return []
            
        oracle_text = card.oracle_text
        source_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
        registered_effects = []
        
        # Scan for standard replacement patterns
        if "if" in oracle_text.lower() and "would" in oracle_text.lower() and "instead" in oracle_text.lower():
            effect_ids = self._register_if_would_instead_effect(card_id, player, oracle_text)
            registered_effects.extend(effect_ids)
        
        # Scan for ETB with counters effects
        if "enters the battlefield with" in oracle_text.lower() and "counter" in oracle_text.lower():
            effect_id = self._register_etb_with_effect(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)
        
        # Scan for damage prevention effects
        if "prevent" in oracle_text.lower() and "damage" in oracle_text.lower():
            effect_id = self._register_damage_prevention(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)
        
        # Check for lifelink
        if "lifelink" in oracle_text.lower() or (hasattr(card, 'keywords') and 
                                            len(card.keywords) > 7 and card.keywords[7]):
            effect_id = self._register_lifelink_effect(card_id, player)
            if effect_id:
                registered_effects.append(effect_id)
        
        # Check for deathtouch
        if "deathtouch" in oracle_text.lower() or (hasattr(card, 'keywords') and 
                                                len(card.keywords) > 2 and card.keywords[2]):
            effect_id = self._register_deathtouch_effect(card_id, player)
            if effect_id:
                registered_effects.append(effect_id)
        
        # Register specialized effects
        
        # 1. Life gain replacements
        if "if you would gain life" in oracle_text.lower() or "if a player would gain life" in oracle_text.lower():
            effect_id = self._register_life_gain_replacement(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)
        
        # 2. Token creation replacements
        if "if you would create" in oracle_text.lower() or "if a player would create" in oracle_text.lower():
            effect_id = self._register_token_creation_replacement(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)
        
        # 3. Redirect damage effects
        if any(phrase in oracle_text.lower() for phrase in ["redirect", "dealt to you is dealt to", "would be dealt to"]):
            effect_id = self._register_redirect_damage_effect(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)
        
        # 4. Doubling effects
        if "double" in oracle_text.lower() and any(word in oracle_text.lower() for word in ["counters", "tokens"]):
            effect_id = self._register_doubling_effect(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)
        
        # 5. Skip step/phase effects
        if any(phrase in oracle_text.lower() for phrase in ["skip", "additional"]) and any(
            phase in oracle_text.lower() for phase in 
            ["upkeep", "draw", "combat", "end", "turn", "phase", "step"]):
            effect_id = self._register_skip_phase_effect(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)
        
        return registered_effects
    
    def _register_token_creation_replacement(self, card_id, player, oracle_text):
        """Register token creation replacement effects."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        
        # Look for token creation replacement patterns
        if "if you would create" in oracle_text.lower() or "if a player would create" in oracle_text.lower():
            # Figure out what happens instead
            replacement_text = ""
            
            # Check for common patterns
            if "create twice that many" in oracle_text.lower():
                replacement_text = "create twice that many tokens"
            elif "create that many plus" in oracle_text.lower():
                # Extract bonus amount
                bonus_match = re.search(r'plus (\d+)', oracle_text.lower())
                bonus = int(bonus_match.group(1)) if bonus_match else 1
                replacement_text = f"create that many plus {bonus} tokens"
            elif "you create no tokens" in oracle_text.lower() or "doesn't create tokens" in oracle_text.lower():
                replacement_text = "create no tokens"
            
            # Create the condition function
            def token_creation_condition(context):
                # Get details from the token creation event
                creator = context.get('creator')
                
                # Check if this is the right player
                if "you would create" in oracle_text.lower():
                    return creator == player
                elif "an opponent would create" in oracle_text.lower():
                    return creator != player
                elif "a player would create" in oracle_text.lower():
                    return True  # Applies to any player
                
                return creator == player  # Default to controller only
            
            # Create the replacement function based on what should happen instead
            def token_creation_replacement(context):
                # Get the token details
                token_count = context.get('token_count', 1)
                token_type = context.get('token_type', 'unknown')
                
                # Create modified context based on replacement text
                modified_context = dict(context)
                
                if "twice that many" in replacement_text:
                    modified_context['token_count'] = token_count * 2
                    logging.debug(f"{source_name} doubled token creation to {token_count * 2} {token_type} tokens")
                elif "plus" in replacement_text:
                    bonus_match = re.search(r'plus (\d+)', replacement_text)
                    bonus = int(bonus_match.group(1)) if bonus_match else 1
                    modified_context['token_count'] = token_count + bonus
                    logging.debug(f"{source_name} added {bonus} more {token_type} tokens, creating {token_count + bonus} total")
                elif "create no tokens" in replacement_text:
                    modified_context['token_count'] = 0
                    modified_context['prevented'] = True
                    logging.debug(f"{source_name} prevented token creation")
                
                return modified_context
            
            # Determine duration
            duration = 'permanent'
            if "until end of turn" in oracle_text.lower():
                duration = 'end_of_turn'
            elif "until your next turn" in oracle_text.lower():
                duration = 'until_my_next_turn'
            
            # Register the effect
            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': 'CREATE_TOKEN',
                'condition': token_creation_condition,
                'replacement': token_creation_replacement,
                'duration': duration,
                'controller_id': player,
                'description': f"{source_name} token creation replacement effect"
            })
            
            logging.debug(f"Registered token creation replacement effect for {source_name}")
            return effect_id
        
        return None
    
    def _register_life_gain_replacement(self, card_id, player, oracle_text):
        """Register life gain replacement effects."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        
        # Look for life gain replacement patterns
        if "if you would gain life" in oracle_text.lower() or "if a player would gain life" in oracle_text.lower():
            # Figure out what happens instead
            replacement_text = ""
            
            # Check for common patterns
            if "twice that much" in oracle_text.lower():
                replacement_text = "gain twice that much life"
            elif "you gain no life" in oracle_text.lower() or "doesn't gain life" in oracle_text.lower():
                replacement_text = "gain no life"
            elif "opponent loses that much life" in oracle_text.lower():
                replacement_text = "opponent loses that much life instead"
            
            # Create the condition function
            def life_gain_condition(context):
                # Get details from the life gain event
                target_player = context.get('player')
                
                # Check if this is the right player
                if "you would gain" in oracle_text.lower():
                    return target_player == player
                elif "an opponent would gain" in oracle_text.lower():
                    return target_player != player
                elif "a player would gain" in oracle_text.lower():
                    return True  # Applies to any player
                
                return target_player == player  # Default to controller only
            
            # Create the replacement function based on what should happen instead
            def life_gain_replacement(context):
                # Get the amount being gained
                amount = context.get('life_amount', 0)
                
                # Create modified context based on replacement text
                modified_context = dict(context)
                
                if "twice that much" in replacement_text:
                    modified_context['life_amount'] = amount * 2
                    logging.debug(f"{source_name} doubled life gain to {amount * 2}")
                elif "gain no life" in replacement_text:
                    modified_context['life_amount'] = 0
                    logging.debug(f"{source_name} prevented life gain")
                elif "opponent loses that much life" in replacement_text:
                    # Original gain still happens, but opponent also loses life
                    opponent = self.game_state.p2 if player == self.game_state.p1 else self.game_state.p1
                    if hasattr(opponent, 'life'):
                        opponent['life'] -= amount
                        logging.debug(f"{source_name} caused opponent to lose {amount} life")
                
                return modified_context
            
            # Determine duration
            duration = 'permanent'
            if "until end of turn" in oracle_text.lower():
                duration = 'end_of_turn'
            elif "until your next turn" in oracle_text.lower():
                duration = 'until_my_next_turn'
            
            # Register the effect
            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': 'LIFE_GAIN',
                'condition': life_gain_condition,
                'replacement': life_gain_replacement,
                'duration': duration,
                'controller_id': player,
                'description': f"{source_name} life gain replacement effect"
            })
            
            logging.debug(f"Registered life gain replacement effect for {source_name}")
            return effect_id
        
        return None
        
    def remove_effect(self, effect_id):
        """Remove a replacement effect."""
        self.active_effects = [effect for effect in self.active_effects 
                             if effect.get('effect_id') != effect_id]
    
    def apply_replacements(self, event_type, event_context):
        """Apply replacement effects with improved conflict resolution."""
        modified_context = dict(event_context)
        was_replaced = False
        
        # Get applicable effects from index
        applicable_effects = self.effect_index.get(event_type, [])
        
        if not applicable_effects:
            return modified_context, was_replaced
        
        # Filter by condition
        valid_effects = []
        for effect in applicable_effects:
            condition_met = True
            if 'condition' in effect:
                if callable(effect['condition']):
                    try:
                        condition_met = effect['condition'](modified_context)
                    except Exception as e:
                        logging.error(f"Error in condition function: {str(e)}")
                        condition_met = False
                else:
                    logging.warning(f"Condition is not callable")
                    condition_met = False
            
            if condition_met:
                valid_effects.append(effect)
        
        if not valid_effects:
            return modified_context, was_replaced
        
        # Implement MTG rule 616.1 for replacement effect ordering
        # 1. Self-replacement effects first
        # 2. Affected player choices (determined by affected object's controller)
        # 3. Affecting player choices (determined by source's controller)
        
        # Determine the affected object and its controller
        affected_id = modified_context.get('affected_id') or modified_context.get('target_id') or modified_context.get('card_id')
        affected_controller = None
        if affected_id:
            affected_controller = self._find_card_controller(affected_id)
        
        # Group effects by relationship to affected object
        self_effects = [e for e in valid_effects if e.get('source_id') == affected_id]
        controller_effects = [e for e in valid_effects 
                            if e.get('source_id') != affected_id and e.get('controller_id') == affected_controller]
        other_effects = [e for e in valid_effects 
                    if e.get('source_id') != affected_id and e.get('controller_id') != affected_controller]
        
        # Sort each group by timestamp (creation order)
        self_effects.sort(key=lambda e: e.get('start_turn', 0))
        controller_effects.sort(key=lambda e: e.get('start_turn', 0))
        other_effects.sort(key=lambda e: e.get('start_turn', 0))
        
        # Combine groups in the correct order
        ordered_effects = self_effects + controller_effects + other_effects
        
        # Apply replacements in sequence
        for effect in ordered_effects:
            if 'replacement' in effect and callable(effect['replacement']):
                try:
                    result = effect['replacement'](modified_context)
                    if result is not None:
                        modified_context = result
                        was_replaced = True
                        
                        source_id = effect.get('source_id')
                        source_card = self.game_state._safe_get_card(source_id)
                        source_name = source_card.name if source_card and hasattr(source_card, 'name') else "Unknown"
                        logging.debug(f"Replacement effect from {source_name} applied to {event_type} event")
                        
                        # Handle one-time effects
                        if effect.get('apply_once', False):
                            self.remove_effect(effect['effect_id'])
                            break
                except Exception as e:
                    logging.error(f"Error in replacement function: {str(e)}")
        
        return modified_context, was_replaced
    
    def _is_creature_controlled_by(self, card_id, player):
        """Check if a card is a creature controlled by a specific player."""
        if not card_id or not player:
            return False
            
        # Check if card is in player's battlefield
        if card_id not in player.get('battlefield', []):
            return False
            
        # Check if it's a creature
        card = self.game_state._safe_get_card(card_id)
        return card and hasattr(card, 'card_types') and 'creature' in card.card_types
    
    def _extract_replacement_clauses(self, text):
        """Extract replacement clauses from card text with better pattern recognition."""
        clauses = []
        
        # Common replacement patterns with named groups
        patterns = [
            # If X would Y, instead Z
            r'if (?P<subject>[^,]+?) would (?P<action>[^,]+?), (?:instead )?(?P<replacement>[^\.]+)',
            # If X would Y, Z instead
            r'if (?P<subject>[^,]+?) would (?P<action>[^,]+?), (?P<replacement>[^\.]+?) instead',
            # Instead of X doing Y, Z
            r'instead of (?P<subject>[^,]+?) (?P<action>[^,]+?), (?P<replacement>[^\.]+)',
        ]
        
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                subject = match.group('subject').strip()
                action = match.group('action').strip()
                replacement = match.group('replacement').strip()
                
                # Determine event type from action
                event_type = self._determine_event_type(action)
                
                # Extract condition (if any)
                condition = None
                if " if " in replacement:
                    parts = replacement.split(" if ", 1)
                    replacement = parts[0].strip()
                    condition = parts[1].strip()
                
                clauses.append({
                    'clause': match.group(0),
                    'event_type': event_type,
                    'subject': subject,
                    'action': action,
                    'replacement': replacement,
                    'condition': condition
                })
        
        return clauses
    
    def _handle_set_pt_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 7a: Set power/toughness effect."""
        if hasattr(card, 'power') and hasattr(card, 'toughness'):
            card.power, card.toughness = effect_value
            logging.debug(f"Set {card.name}'s power/toughness to {effect_value[0]}/{effect_value[1]}")

    def _handle_modify_pt_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 7c: Modify power/toughness effect."""
        if hasattr(card, 'power') and hasattr(card, 'toughness'):
            power_mod, toughness_mod = effect_value
            card.power += power_mod
            card.toughness += toughness_mod
            logging.debug(f"Modified {card.name}'s power/toughness by +{power_mod}/+{toughness_mod}")

    def _handle_switch_pt_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 7d: Switch power/toughness effect."""
        if hasattr(card, 'power') and hasattr(card, 'toughness'):
            card.power, card.toughness = card.toughness, card.power
            logging.debug(f"Switched {card.name}'s power/toughness to {card.power}/{card.toughness}")

    def _handle_set_color_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 5: Set color effect."""
        if hasattr(card, 'colors'):
            card.colors = effect_value
            logging.debug(f"Set {card.name}'s colors")

    def _handle_add_color_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 5: Add color effect."""
        if hasattr(card, 'colors'):
            for i, color in enumerate(effect_value):
                if i < len(card.colors) and color:
                    card.colors[i] = 1
            logging.debug(f"Added colors to {card.name}")

    def _handle_remove_color_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 5: Remove color effect."""
        if hasattr(card, 'colors'):
            for i, color in enumerate(effect_value):
                if i < len(card.colors) and color:
                    card.colors[i] = 0
            logging.debug(f"Removed colors from {card.name}")

    def _handle_add_type_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 4: Add type effect."""
        if hasattr(card, 'card_types'):
            if effect_value not in card.card_types:
                card.card_types.append(effect_value)
                logging.debug(f"Added type '{effect_value}' to {card.name}")

    def _handle_remove_type_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 4: Remove type effect."""
        if hasattr(card, 'card_types') and effect_value in card.card_types:
            card.card_types.remove(effect_value)
            logging.debug(f"Removed type '{effect_value}' from {card.name}")

    def _handle_add_subtype_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 4: Add subtype effect."""
        if hasattr(card, 'subtypes'):
            if effect_value not in card.subtypes:
                card.subtypes.append(effect_value)
                logging.debug(f"Added subtype '{effect_value}' to {card.name}")

    def _handle_remove_subtype_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 4: Remove subtype effect."""
        if hasattr(card, 'subtypes') and effect_value in card.subtypes:
            card.subtypes.remove(effect_value)
            logging.debug(f"Removed subtype '{effect_value}' from {card.name}")

    def _handle_add_ability_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 6: Add ability effect."""
        if not hasattr(card, 'granted_abilities'):
            card.granted_abilities = []
        if effect_value not in card.granted_abilities:
            card.granted_abilities.append(effect_value)
            
            # Update keywords for game mechanics
            if hasattr(card, 'keywords') and isinstance(effect_value, str):
                ability_to_keyword_index = {
                    'flying': 0, 'trample': 1, 'hexproof': 2, 
                    'lifelink': 3, 'deathtouch': 4, 'first strike': 5,
                    'double strike': 6, 'vigilance': 7, 'flash': 8,
                    'haste': 9, 'menace': 10
                }
                if effect_value.lower() in ability_to_keyword_index:
                    card.keywords[ability_to_keyword_index[effect_value.lower()]] = 1
                    logging.debug(f"Added ability '{effect_value}' to {card.name}")

    def _handle_remove_ability_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 6: Remove ability effect."""
        if hasattr(card, 'granted_abilities') and effect_value in card.granted_abilities:
            card.granted_abilities.remove(effect_value)
            
            # Update keywords for game mechanics
            if hasattr(card, 'keywords') and isinstance(effect_value, str):
                ability_to_keyword_index = {
                    'flying': 0, 'trample': 1, 'hexproof': 2, 
                    'lifelink': 3, 'deathtouch': 4, 'first strike': 5,
                    'double strike': 6, 'vigilance': 7, 'flash': 8,
                    'haste': 9, 'menace': 10
                }
                if effect_value.lower() in ability_to_keyword_index:
                    card.keywords[ability_to_keyword_index[effect_value.lower()]] = 0
                    logging.debug(f"Removed ability '{effect_value}' from {card.name}")

    def _handle_copy_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 1: Copy effect."""
        source_card = self.game_state._safe_get_card(effect_value)
        if source_card:
            # Copy core attributes but maintain original card ID
            original_id = card.card_id if hasattr(card, 'card_id') else None
            for attr in ['name', 'type_line', 'oracle_text', 'power', 'toughness', 
                        'mana_cost', 'cmc', 'card_types', 'colors', 'subtypes']:
                if hasattr(source_card, attr):
                    setattr(card, attr, getattr(source_card, attr))
            # Restore original ID
            if hasattr(card, 'card_id') and original_id is not None:
                card.card_id = original_id
            logging.debug(f"{card.name} copied {source_card.name}")

    def _handle_change_text_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 3: Change text effect."""
        old_text, new_text = effect_value
        if hasattr(card, 'oracle_text'):
            card.oracle_text = card.oracle_text.replace(old_text, new_text)
            logging.debug(f"Changed text in {card.name}: '{old_text}' to '{new_text}'")

    def _handle_change_control_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 2: Change control effect."""
        new_controller = effect_value
        current_controller = owner
        
        # Move card to new controller's battlefield
        if hasattr(card, 'card_id'):
            if card.card_id in current_controller["battlefield"]:
                current_controller["battlefield"].remove(card.card_id)
                new_controller["battlefield"].append(card.card_id)
                logging.debug(f"Control changed: {card.name} now controlled by different player")

    def _handle_cant_attack_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 6: Add restriction on attacking."""
        if not hasattr(card, 'attack_restrictions'):
            card.attack_restrictions = []
        if effect_value not in card.attack_restrictions:
            card.attack_restrictions.append(effect_value)
            logging.debug(f"Added attack restriction to {card.name}: {effect_value}")

    def _handle_cant_block_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 6: Add restriction on blocking."""
        if not hasattr(card, 'block_restrictions'):
            card.block_restrictions = []
        if effect_value not in card.block_restrictions:
            card.block_restrictions.append(effect_value)
            logging.debug(f"Added block restriction to {card.name}: {effect_value}")

    def _handle_assign_damage_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 6: Special combat damage assignment rule."""
        if not hasattr(card, 'combat_abilities'):
            card.combat_abilities = []
        if 'assign_damage_as_though_not_blocked' not in card.combat_abilities:
            card.combat_abilities.append('assign_damage_as_though_not_blocked')
            logging.debug(f"{card.name} can now assign combat damage as though it weren't blocked")

    def _handle_add_protection_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 6: Add protection."""
        if not hasattr(card, 'protection'):
            card.protection = []
        # effect_value should be what the card has protection from (e.g., 'red', 'creatures')
        if effect_value not in card.protection:
            card.protection.append(effect_value)
            logging.debug(f"Added protection from {effect_value} to {card.name}")

    def _handle_must_attack_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 6: Add attack requirement."""
        if not hasattr(card, 'attack_requirements'):
            card.attack_requirements = []
        if effect_value not in card.attack_requirements:
            card.attack_requirements.append(effect_value)
            logging.debug(f"Added attack requirement to {card.name}: {effect_value}")

    def _handle_enchanted_must_attack_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 6: Force enchanted creature to attack."""
        if hasattr(self.game_state, 'attachments') and card.card_id in self.game_state.attachments:
            enchanted_id = self.game_state.attachments[card.card_id]
            enchanted_card = self.game_state._safe_get_card(enchanted_id)
            if enchanted_card:
                if not hasattr(enchanted_card, 'attack_requirements'):
                    enchanted_card.attack_requirements = []
                if 'must_attack_each_turn' not in enchanted_card.attack_requirements:
                    enchanted_card.attack_requirements.append('must_attack_each_turn')
                    logging.debug(f"Enchanted creature {enchanted_card.name} must attack each turn if able")
    
    def _create_enhanced_condition_function(self, event_type, subject, condition, controller):
        """Create an enhanced condition function with better context handling."""
        gs = self.game_state
        
        # Basic subject checks
        subject_checks = {
            'you': lambda ctx: ctx.get('player') == controller,
            'opponent': lambda ctx: ctx.get('player') != controller and ctx.get('player') in [gs.p1, gs.p2],
            'this': lambda ctx: ctx.get('source_id') == getattr(self, 'card_id', None) or ctx.get('card_id') == getattr(self, 'card_id', None),
            'creature you control': lambda ctx: self._is_creature_controlled_by(ctx.get('source_id'), controller) or self._is_creature_controlled_by(ctx.get('card_id'), controller)
        }
        
        # Find matching subject check
        subject_check = lambda ctx: True  # Default to always true
        for subject_pattern, check_func in subject_checks.items():
            if subject_pattern in subject:
                subject_check = check_func
                break
        
        # Additional condition check if present
        if condition:
            return lambda ctx: subject_check(ctx) and self._evaluate_enhanced_condition(condition, ctx, controller)
        else:
            return subject_check

    def _evaluate_enhanced_condition(self, condition, context, controller):
        """Evaluate condition with enhanced context awareness."""
        # Enhanced condition evaluation logic
        # This would be a more sophisticated version of the existing condition evaluation
        # that handles more complex conditions and game state checks
        
        # Example implementation that could be expanded
        gs = self.game_state
        
        # Common condition patterns
        if "control a" in condition or "controls a" in condition:
            # Extract what needs to be controlled
            match = re.search(r"controls? (?:a|an|[\d]+) ([^\.]+)", condition)
            if match:
                permanent_type = match.group(1).strip()
                
                # Count matching permanents
                count = 0
                for perm_id in controller["battlefield"]:
                    perm = gs._safe_get_card(perm_id)
                    if not perm or not hasattr(perm, 'type_line'):
                        continue
                        
                    if permanent_type.lower() in perm.type_line.lower():
                        count += 1
                
                return count > 0
        
        # Life total conditions
        if "life total" in condition:
            if "your life total" in condition:
                life = controller.get("life", 0)
                
                if "less than" in condition:
                    match = re.search(r"less than (\d+)", condition)
                    if match:
                        threshold = int(match.group(1))
                        return life < threshold
                elif "greater than" in condition:
                    match = re.search(r"greater than (\d+)", condition)
                    if match:
                        threshold = int(match.group(1))
                        return life > threshold
        
        # Default for unknown conditions
        return True
        
    def _register_if_would_instead_effect(self, card_id, player, oracle_text):
        """Register 'if...would...instead' replacement effects with improved text parsing."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        registered_effects = []
        
        # Normalize text for better pattern matching
        normalized_text = oracle_text.lower()
        normalized_text = re.sub(r'\([^)]*\)', '', normalized_text)  # Remove reminder text in parentheses
        normalized_text = re.sub(r'\s+', ' ', normalized_text)  # Normalize whitespace
        
        # Extract replacement clauses with improved regex
        replacement_clauses = self._extract_replacement_clauses(normalized_text)
        
        for clause_data in replacement_clauses:
            clause = clause_data['clause']
            event_type = clause_data['event_type']
            subject = clause_data['subject']
            condition = clause_data['condition']
            replacement = clause_data['replacement']
            
            # Create condition and replacement functions
            condition_func = self._create_enhanced_condition_function(event_type, subject, condition, player)
            replacement_func = self._create_enhanced_replacement_function(event_type, replacement, player, source_name)
            
            # Determine duration
            duration = 'permanent'
            if "until end of turn" in normalized_text:
                duration = 'end_of_turn'
            elif "this turn" in normalized_text and not "whenever" in normalized_text:
                duration = 'end_of_turn'
            elif "until your next turn" in normalized_text:
                duration = 'until_my_next_turn'
            
            # Register the effect
            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': event_type,
                'condition': condition_func,
                'replacement': replacement_func,
                'duration': duration,
                'controller_id': player,
                'description': f"{source_name}: {subject} {replacement}"
            })
            
            registered_effects.append(effect_id)
            logging.debug(f"Registered {event_type} replacement: {subject} {replacement}")
        
        return registered_effects

    def _find_clause_end(self, text):
        """Find the end of a clause in the given text."""
        end_markers = ['.', ';']
        positions = [text.find(marker) for marker in end_markers if text.find(marker) != -1]
        return min(positions) if positions else -1

    def _create_enhanced_replacement_function(self, event_type, replacement_text, controller, source_name):
        """
        Create a more sophisticated replacement function based on event type and replacement text.
        
        Args:
            event_type: The type of event being replaced
            replacement_text: Text describing what happens instead
            controller: The player who controls the replacement effect
            source_name: Name of the card/effect source for logging
            
        Returns:
            function: A function that modifies the event context appropriately
        """
        gs = self.game_state
        replacement_text = replacement_text.lower() if replacement_text else ""
        
        # DRAW event replacements
        if event_type == 'DRAW':
            if 'draw twice that many' in replacement_text or 'instead draw twice that many' in replacement_text:
                def double_draw_replacement(ctx):
                    draw_count = ctx.get('draw_count', 1) 
                    new_count = draw_count * 2
                    logging.debug(f"{source_name}: Doubling draw from {draw_count} to {new_count} cards")
                    return {**ctx, 'draw_count': new_count}
                return double_draw_replacement
                
            elif 'draw that many plus' in replacement_text:
                match = re.search(r'plus (\d+)', replacement_text)
                bonus = 1
                if match:
                    bonus = int(match.group(1))
                    
                def bonus_draw_replacement(ctx):
                    draw_count = ctx.get('draw_count', 1)
                    new_count = draw_count + bonus
                    logging.debug(f"{source_name}: Adding {bonus} to draw, from {draw_count} to {new_count} cards")
                    return {**ctx, 'draw_count': new_count}
                return bonus_draw_replacement
                
            elif "doesn't draw" in replacement_text or "draw no cards" in replacement_text:
                def prevent_draw_replacement(ctx):
                    draw_count = ctx.get('draw_count', 1)
                    logging.debug(f"{source_name}: Preventing draw of {draw_count} cards")
                    return {**ctx, 'draw_count': 0, 'prevented': True}
                return prevent_draw_replacement
        
        # DAMAGE event replacements
        elif event_type == 'DAMAGE':
            if 'prevent' in replacement_text or 'is prevented' in replacement_text:
                def prevent_damage_replacement(ctx):
                    damage = ctx.get('damage_amount', 0)
                    logging.debug(f"{source_name}: Preventing {damage} damage")
                    return {**ctx, 'damage_amount': 0, 'prevented': True}
                return prevent_damage_replacement
                
            elif 'double' in replacement_text or 'twice' in replacement_text:
                def double_damage_replacement(ctx):
                    damage = ctx.get('damage_amount', 0)
                    new_damage = damage * 2
                    logging.debug(f"{source_name}: Doubling damage from {damage} to {new_damage}")
                    return {**ctx, 'damage_amount': new_damage}
                return double_damage_replacement
                
            elif 'deal that much damage to' in replacement_text:
                # Redirect damage to a different target
                def redirect_damage_replacement(ctx):
                    damage = ctx.get('damage_amount', 0)
                    original_target = ctx.get('target_id')
                    original_is_player = ctx.get('target_is_player', False)
                    
                    # Determine new target based on replacement text
                    new_target = None
                    new_is_player = False
                    
                    if 'to its controller' in replacement_text:
                        # Find controller of original source
                        source_id = ctx.get('source_id')
                        for p in [gs.p1, gs.p2]:
                            if source_id in p.get('battlefield', []):
                                new_target = p
                                new_is_player = True
                                break
                    elif 'to you' in replacement_text:
                        new_target = controller
                        new_is_player = True
                    elif 'to each opponent' in replacement_text:
                        # This would need special handling to hit all opponents
                        new_target = gs.p2 if controller == gs.p1 else gs.p1
                        new_is_player = True
                    
                    if new_target:
                        # Apply damage to new target and prevent it to original target
                        if new_is_player:
                            new_target['life'] -= damage
                            logging.debug(f"{source_name}: Redirected {damage} damage to {new_target.get('name', 'player')}")
                        
                        # Return modified context to prevent original damage
                        return {**ctx, 'damage_amount': 0, 'redirected': True, 
                                'redirected_to': new_target, 'redirected_is_player': new_is_player}
                    
                    return ctx  # Original context if no redirection happened
                return redirect_damage_replacement
        
        # ENTERS_BATTLEFIELD event replacements
        elif event_type == 'ENTERS_BATTLEFIELD':
            if 'enters the battlefield tapped' in replacement_text:
                def etb_tapped_replacement(ctx):
                    card_id = ctx.get('card_id')
                    controller = ctx.get('controller')
                    
                    if controller and card_id:
                        # Flag to tap the permanent once it enters
                        return {**ctx, 'enters_tapped': True}
                    return ctx
                return etb_tapped_replacement
                
            elif 'enters the battlefield with' in replacement_text:
                counter_match = re.search(r'with (\w+) (\w+) counters?', replacement_text)
                if counter_match:
                    count_word, counter_type = counter_match.groups()
                    count = 1
                    if count_word.isdigit():
                        count = int(count_word)
                    elif count_word == "a" or count_word == "an":
                        count = 1
                    
                    def etb_with_counters_replacement(ctx):
                        card_id = ctx.get('card_id')
                        return {**ctx, 'enter_counters': {'type': counter_type, 'count': count}}
                    return etb_with_counters_replacement
        
        # DIES event replacements
        elif event_type == 'DIES':
            if 'exile' in replacement_text or 'instead exile it' in replacement_text:
                def exile_instead_replacement(ctx):
                    logging.debug(f"{source_name}: Exiling card instead of putting it into graveyard")
                    return {**ctx, 'to_zone': 'exile'}
                return exile_instead_replacement
                
            elif 'return' in replacement_text and 'hand' in replacement_text:
                def return_to_hand_replacement(ctx):
                    logging.debug(f"{source_name}: Returning card to hand instead of putting it into graveyard")
                    return {**ctx, 'to_zone': 'hand'}
                return return_to_hand_replacement
        
        # LIFE_GAIN event replacements        
        elif event_type == 'LIFE_GAIN':
            if 'twice' in replacement_text or 'double' in replacement_text:
                def double_life_gain_replacement(ctx):
                    amount = ctx.get('life_amount', 0)
                    new_amount = amount * 2
                    logging.debug(f"{source_name}: Doubling life gain from {amount} to {new_amount}")
                    return {**ctx, 'life_amount': new_amount}
                return double_life_gain_replacement
                
            elif 'no life' in replacement_text or "doesn't gain life" in replacement_text:
                def prevent_life_gain_replacement(ctx):
                    amount = ctx.get('life_amount', 0)
                    logging.debug(f"{source_name}: Preventing life gain of {amount}")
                    return {**ctx, 'life_amount': 0, 'prevented': True}
                return prevent_life_gain_replacement
        
        # Default replacement that just returns the original context
        return lambda ctx: ctx

    def _create_condition_function(self, event_type, subject, controller):
        """Create a condition function based on event type and subject."""
        gs = self.game_state
        
        # 'You' condition
        if 'you' in subject:
            return lambda ctx: ctx.get('player') == controller
        
        # 'Opponent' condition
        if 'opponent' in subject:
            return lambda ctx: ctx.get('player') != controller and ctx.get('player') in [gs.p1, gs.p2]
        
        # 'This creature' or 'it' condition
        if 'this' in subject or 'it' in subject:
            source_id = getattr(self, 'card_id', None)
            return lambda ctx: ctx.get('source_id') == source_id or ctx.get('card_id') == source_id
        
        # 'Creature you control' condition
        if 'creature you control' in subject:
            return lambda ctx: self._is_creature_controlled_by(ctx.get('source_id'), controller) or self._is_creature_controlled_by(ctx.get('card_id'), controller)
        
        # Default to always true
        return lambda ctx: True

    def _create_replacement_function(self, event_type, replacement_text, controller, source_name):
        """Create a replacement function based on event type and replacement text."""
        gs = self.game_state
        
        # Draw replacements
        if event_type == 'DRAW':
            if 'draw two cards' in replacement_text:
                return lambda ctx: {**ctx, 'draw_count': 2}
            elif 'draw no cards' in replacement_text or 'doesn\'t draw' in replacement_text:
                return lambda ctx: {**ctx, 'draw_count': 0}
            elif 'draw an additional card' in replacement_text:
                return lambda ctx: {**ctx, 'draw_count': ctx.get('draw_count', 1) + 1}
            elif 'draw twice that many cards' in replacement_text:
                return lambda ctx: {**ctx, 'draw_count': ctx.get('draw_count', 1) * 2}
            elif 'draw half that many cards, rounded up' in replacement_text:
                return lambda ctx: {**ctx, 'draw_count': (ctx.get('draw_count', 1) + 1) // 2}
            elif 'lose 1 life' in replacement_text:
                def replacement(ctx):
                    # Apply the side effect of losing life
                    if controller and 'life' in controller:
                        controller['life'] -= 1
                        logging.debug(f"{source_name} effect: {controller['name']} lost 1 life instead of drawing")
                    return ctx  # Original draw still happens
                return replacement
            elif 'reveal it' in replacement_text:
                # Reveal effect - just log the revealed card for now
                def reveal_replacement(ctx):
                    card_id = ctx.get('card_id')
                    if card_id:
                        card = gs._safe_get_card(card_id)
                        if card and hasattr(card, 'name'):
                            logging.debug(f"{controller['name']} revealed {card.name} due to {source_name}")
                    return ctx  # Original draw still happens
                return reveal_replacement
        
        # Damage replacements
        elif event_type == 'DAMAGE':
            if 'prevent' in replacement_text or 'no damage' in replacement_text:
                return lambda ctx: {**ctx, 'damage_amount': 0}
            elif 'twice that much' in replacement_text or 'double' in replacement_text:
                return lambda ctx: {**ctx, 'damage_amount': ctx.get('damage_amount', 0) * 2}
            elif 'half that much, rounded up' in replacement_text:
                return lambda ctx: {**ctx, 'damage_amount': (ctx.get('damage_amount', 0) + 1) // 2}
            elif 'half that much, rounded down' in replacement_text:
                return lambda ctx: {**ctx, 'damage_amount': ctx.get('damage_amount', 0) // 2}
            elif 'plus' in replacement_text:
                # Try to extract a number
                match = re.search(r'plus (\d+)', replacement_text)
                if match:
                    bonus = int(match.group(1))
                    return lambda ctx: {**ctx, 'damage_amount': ctx.get('damage_amount', 0) + bonus}
            elif 'prevent the first' in replacement_text:
                # Extract the amount to prevent
                match = re.search(r'prevent the first (\d+)', replacement_text)
                if match:
                    prevent_amount = int(match.group(1))
                    def partial_prevention(ctx):
                        damage = ctx.get('damage_amount', 0)
                        new_damage = max(0, damage - prevent_amount)
                        prevented = damage - new_damage
                        if prevented > 0:
                            logging.debug(f"Prevented {prevented} damage due to {source_name}")
                        return {**ctx, 'damage_amount': new_damage}
                    return partial_prevention
            elif 'deals that much damage to' in replacement_text:
                # Damage redirection
                def redirect_damage(ctx):
                    damage = ctx.get('damage_amount', 0)
                    original_target = ctx.get('target_id')
                    
                    # Determine new target based on replacement text
                    new_target = None
                    if 'deals that much damage to you' in replacement_text:
                        new_target = controller
                    elif 'deals that much damage to its controller' in replacement_text:
                        # Find original source's controller
                        source_id = ctx.get('source_id')
                        for p in [gs.p1, gs.p2]:
                            if source_id in p.get('battlefield', []):
                                new_target = p
                                break
                    
                    if new_target:
                        # Apply damage to new target
                        new_target['life'] -= damage
                        logging.debug(f"Redirected {damage} damage from {original_target} to {new_target['name']}")
                        return {**ctx, 'damage_amount': 0}  # Original damage is prevented
                    
                    return ctx
                return redirect_damage
        
        # Death/dies replacements
        elif event_type == 'DIES':
            if 'exile' in replacement_text:
                return lambda ctx: {**ctx, 'to_zone': 'exile'}
            elif 'return to its owner\'s hand' in replacement_text or 'return it to your hand' in replacement_text:
                return lambda ctx: {**ctx, 'to_zone': 'hand'}
            elif 'shuffle into' in replacement_text or 'put into its owner\'s library' in replacement_text:
                return lambda ctx: {**ctx, 'to_zone': 'library', 'shuffle': True}
            elif 'regenerate' in replacement_text:
                def regenerate_replacement(ctx):
                    card_id = ctx.get('card_id')
                    if card_id:
                        # Tap the creature
                        owner = ctx.get('controller')
                        if owner:
                            owner['tapped_permanents'].add(card_id)
                        
                        # Remove all damage
                        for p in [gs.p1, gs.p2]:
                            if 'damage_counters' in p and card_id in p['damage_counters']:
                                del p['damage_counters'][card_id]
                        
                        logging.debug(f"Regenerated {gs._safe_get_card(card_id).name}")
                        return {**ctx, 'prevented': True}
                    return ctx
                return regenerate_replacement
            elif 'create a token that\'s a copy of it' in replacement_text:
                def copy_token_replacement(ctx):
                    card_id = ctx.get('card_id')
                    owner = ctx.get('controller')
                    if card_id and owner:
                        # Make a token copy - simplified implementation
                        logging.debug(f"Created a token copy of {gs._safe_get_card(card_id).name}")
                        
                        # Move the original to graveyard
                        return {**ctx, 'create_token': True, 'copy_of': card_id}
                    return ctx
                return copy_token_replacement
        
        # Life gain replacements
        elif event_type == 'LIFE_GAIN':
            if 'gain twice that much' in replacement_text or 'double' in replacement_text:
                return lambda ctx: {**ctx, 'life_amount': ctx.get('life_amount', 0) * 2}
            elif 'gain that much plus' in replacement_text:
                match = re.search(r'plus (\d+)', replacement_text)
                if match:
                    bonus = int(match.group(1))
                    return lambda ctx: {**ctx, 'life_amount': ctx.get('life_amount', 0) + bonus}
            elif 'doesn\'t gain life' in replacement_text or 'gain no life' in replacement_text:
                return lambda ctx: {**ctx, 'life_amount': 0}
            elif 'half that much' in replacement_text:
                return lambda ctx: {**ctx, 'life_amount': ctx.get('life_amount', 0) // 2}
            elif 'each opponent loses that much life' in replacement_text:
                def life_drain_replacement(ctx):
                    amount = ctx.get('life_amount', 0)
                    modified_ctx = dict(ctx)
                    
                    # Original life gain still happens, but opponents also lose life
                    opponent = gs.p2 if controller == gs.p1 else gs.p1
                    if opponent:
                        opponent['life'] -= amount
                        logging.debug(f"{opponent['name']} lost {amount} life due to {source_name}")
                    
                    return modified_ctx
                return life_drain_replacement
        
        # Life loss replacements
        elif event_type == 'LIFE_LOSS':
            if 'lose twice that much' in replacement_text:
                return lambda ctx: {**ctx, 'life_amount': ctx.get('life_amount', 0) * 2}
            elif 'lose that much plus' in replacement_text:
                match = re.search(r'plus (\d+)', replacement_text)
                if match:
                    bonus = int(match.group(1))
                    return lambda ctx: {**ctx, 'life_amount': ctx.get('life_amount', 0) + bonus}
            elif 'doesn\'t lose life' in replacement_text or 'lose no life' in replacement_text:
                return lambda ctx: {**ctx, 'life_amount': 0}
            elif 'half that much' in replacement_text:
                rounding = 'up' if 'rounded up' in replacement_text else 'down'
                if rounding == 'up':
                    return lambda ctx: {**ctx, 'life_amount': (ctx.get('life_amount', 0) + 1) // 2}
                else:
                    return lambda ctx: {**ctx, 'life_amount': ctx.get('life_amount', 0) // 2}
            elif 'each opponent loses that much life' in replacement_text:
                def shared_life_loss(ctx):
                    amount = ctx.get('life_amount', 0)
                    opponent = gs.p2 if controller == gs.p1 else gs.p1
                    if opponent:
                        opponent['life'] -= amount
                        logging.debug(f"{opponent['name']} also lost {amount} life due to {source_name}")
                    return ctx
                return shared_life_loss
        
        # Counter replacements
        elif event_type == 'COUNTER':
            if 'gets twice that many' in replacement_text:
                return lambda ctx: {**ctx, 'counter_amount': ctx.get('counter_amount', 1) * 2}
            elif 'gets that many plus' in replacement_text:
                match = re.search(r'plus (\d+)', replacement_text)
                if match:
                    bonus = int(match.group(1))
                    return lambda ctx: {**ctx, 'counter_amount': ctx.get('counter_amount', 1) + bonus}
            elif 'doesn\'t get any counters' in replacement_text:
                return lambda ctx: {**ctx, 'counter_amount': 0}
            elif 'that many of each kind of counter' in replacement_text:
                def multiple_counter_types(ctx):
                    # The original counter still gets applied, but we add other types
                    counter_type = ctx.get('counter_type', '')
                    amount = ctx.get('counter_amount', 1)
                    
                    # We would add other counter types here, but for now just log it
                    logging.debug(f"Would add {amount} of each counter type due to {source_name}")
                    
                    return ctx
                return multiple_counter_types
        
        # Discard replacements
        elif event_type == 'DISCARD':
            if 'discard two cards instead' in replacement_text:
                return lambda ctx: {**ctx, 'discard_count': 2}
            elif 'doesn\'t discard' in replacement_text:
                return lambda ctx: {**ctx, 'discard_count': 0}
            elif 'discard a card at random' in replacement_text:
                def random_discard(ctx):
                    modified_ctx = dict(ctx)
                    modified_ctx['random_discard'] = True
                    return modified_ctx
                return random_discard
        
        # Destroy replacements
        elif event_type == 'DESTROY':
            if 'exile' in replacement_text:
                return lambda ctx: {**ctx, 'to_zone': 'exile'}
            elif 'regenerate' in replacement_text:
                def regenerate_replacement(ctx):
                    card_id = ctx.get('card_id')
                    if card_id:
                        # Implementation similar to DIES regenerate
                        owner = ctx.get('controller')
                        if owner:
                            owner['tapped_permanents'].add(card_id)
                        
                        # Remove all damage
                        for p in [gs.p1, gs.p2]:
                            if 'damage_counters' in p and card_id in p['damage_counters']:
                                del p['damage_counters'][card_id]
                        
                        logging.debug(f"Regenerated {gs._safe_get_card(card_id).name}")
                        return {**ctx, 'prevented': True}
                    return ctx
                return regenerate_replacement
        
        # Spell counter replacements
        elif event_type == 'COUNTER_SPELL':
            if 'can\'t be countered' in replacement_text:
                return lambda ctx: {**ctx, 'prevented': True}
            elif 'exile it instead' in replacement_text:
                return lambda ctx: {**ctx, 'to_zone': 'exile'}
        
        # Enters battlefield replacements
        elif event_type == 'ENTERS_BATTLEFIELD':
            if 'enters with' in replacement_text:
                # Extract counter information
                counter_match = re.search(r'enters with (\w+) (\w+) counters?', replacement_text)
                if counter_match:
                    count_word = counter_match.group(1)
                    counter_type = counter_match.group(2)
                    
                    # Convert word to number
                    count = 1
                    if count_word.isdigit():
                        count = int(count_word)
                    elif count_word == "a" or count_word == "an":
                        count = 1
                    elif count_word == "two":
                        count = 2
                    elif count_word == "three":
                        count = 3
                    
                    def enter_with_counters(ctx):
                        modified_ctx = dict(ctx)
                        modified_ctx['enter_counters'] = {
                            'type': counter_type,
                            'count': count
                        }
                        return modified_ctx
                    return enter_with_counters
            
            elif 'enters tapped' in replacement_text:
                def enter_tapped(ctx):
                    card_id = ctx.get('card_id')
                    controller = ctx.get('controller')
                    if card_id and controller:
                        controller['tapped_permanents'].add(card_id)
                        logging.debug(f"{gs._safe_get_card(card_id).name} enters tapped due to {source_name}")
                    return ctx
                return enter_tapped
        
        # Tapped replacements
        elif event_type == 'TAPPED':
            if 'enters untapped' in replacement_text:
                def enter_untapped(ctx):
                    card_id = ctx.get('card_id')
                    controller = ctx.get('controller')
                    if card_id and controller and card_id in controller.get('tapped_permanents', set()):
                        controller['tapped_permanents'].remove(card_id)
                        logging.debug(f"{gs._safe_get_card(card_id).name} enters untapped due to {source_name}")
                    return ctx
                return enter_untapped
        
        # Default return original context
        return lambda ctx: ctx

    def _register_etb_with_effect(self, card_id, player, oracle_text):
        """Register 'enters the battlefield with' replacement effects."""
        # Look for counter patterns
        counter_match = re.search(r'enters the battlefield with (\w+) (\+1/\+1|charge|loyalty) counters?', oracle_text)
        if counter_match:
            count_word = counter_match.group(1)
            counter_type = counter_match.group(2)
            
            # Convert word to number
            count = 1
            if count_word.isdigit():
                count = int(count_word)
            elif count_word == "a" or count_word == "an":
                count = 1
            elif count_word == "two":
                count = 2
            elif count_word == "three":
                count = 3
            elif count_word == "four":
                count = 4
            
            # Register a replacement effect for this card entering with counters
            source_card = self.game_state._safe_get_card(card_id)
            source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
            
            def etb_with_counters_condition(context):
                entering_card_id = context.get('card_id')
                # Only apply to this specific card
                return entering_card_id == card_id
            
            def etb_with_counters_replacement(context):
                # Get the card and add counters (without actually modifying the context)
                entering_card_id = context.get('card_id')
                controller = context.get('controller')
                
                if hasattr(self.game_state, 'add_counter'):
                    # This will be called after the card is on the battlefield
                    def add_counters_later():
                        self.game_state.add_counter(entering_card_id, counter_type, count)
                        
                    # Schedule this to run after the current event resolves
                    if not hasattr(self.game_state, 'delayed_triggers'):
                        self.game_state.delayed_triggers = []
                    self.game_state.delayed_triggers.append(add_counters_later)
                
                # Return unmodified context since we're not changing the event itself
                return context
            
            self.register_effect({
                'source_id': card_id,
                'event_type': 'ENTERS_BATTLEFIELD',
                'condition': etb_with_counters_condition,
                'replacement': etb_with_counters_replacement,
                'duration': 'permanent',
                'controller_id': player,
                'description': f"{source_name} enters with {count} {counter_type} counters"
            })
            
            logging.debug(f"Registered 'enters with counters' effect for {source_name}")

    def _register_redirect_damage_effect(self, card_id, player, oracle_text):
        """Register a damage redirection effect."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        
        import re
        
        # Various redirection patterns
        redirect_patterns = [
            r'(damage that would be dealt to you) (is dealt to|is dealt to you and to) (.+?) (instead)',
            r'(if .+? would deal damage to you), (it deals that damage to) (.+?) (instead)',
            r'(redirect .+? damage from) (.+?) to (.+)'
        ]
        
        for pattern in redirect_patterns:
            matches = re.finditer(pattern, oracle_text.lower())
            
            for match in matches:
                groups = match.groups()
                
                # Extract target based on pattern
                target_phrase = groups[2] if len(groups) >= 3 else ""
                
                def redirect_condition(context):
                    # Only apply for damage that matches our criteria
                    target_id = context.get('target_id')
                    target_is_player = context.get('target_is_player', False)
                    
                    # Default condition: damage to the effect's controller
                    is_to_controller = target_is_player and target_id == player
                    
                    # Adjust based on text
                    if "each opponent" in target_phrase:
                        opponent = self.game_state.p2 if player == self.game_state.p1 else self.game_state.p1
                        return target_is_player and target_id == opponent
                    elif "you" in target_phrase:
                        return is_to_controller
                    
                    return is_to_controller  # Default
                
                def redirect_replacement(context):
                    # Determine new target
                    new_target = None
                    new_is_player = False
                    
                    # Parse target from text
                    if "you" in target_phrase:
                        # Redirect to controller
                        new_target = player
                        new_is_player = True
                    elif "each opponent" in target_phrase:
                        # Redirect to opponent
                        new_target = self.game_state.p2 if player == self.game_state.p1 else self.game_state.p1
                        new_is_player = True
                    else:
                        # Try to find a creature mentioned by name
                        creature_name = target_phrase.strip()
                        for p in [self.game_state.p1, self.game_state.p2]:
                            for cid in p.get('battlefield', []):
                                card = self.game_state._safe_get_card(cid)
                                if card and hasattr(card, 'name') and card.name.lower() == creature_name:
                                    new_target = cid
                                    new_is_player = False
                                    break
                    
                    if new_target:
                        # Create modified context with new target
                        modified_context = dict(context)
                        modified_context['target_id'] = new_target
                        modified_context['target_is_player'] = new_is_player
                        
                        # Log the redirection
                        damage_amount = context.get('damage_amount', 0)
                        original_target = "you" if context.get('target_is_player', False) else "a creature"
                        new_target_name = "you" if new_is_player else self.game_state._safe_get_card(new_target).name
                        
                        logging.debug(f"Redirected {damage_amount} damage from {original_target} to {new_target_name} with {source_name}")
                        
                        return modified_context
                    
                    return context
                
                # Determine duration
                duration = 'permanent'
                if "until end of turn" in oracle_text.lower():
                    duration = 'end_of_turn'
                    
                # Register the effect
                effect_id = self.register_effect({
                    'source_id': card_id,
                    'event_type': 'DAMAGE',
                    'condition': redirect_condition,
                    'replacement': redirect_replacement,
                    'duration': duration,
                    'controller_id': player,
                    'description': f"{source_name} damage redirection effect"
                })
                
                logging.debug(f"Registered damage redirection effect for {source_name}")
                return effect_id
        
        return None

    def _register_doubling_effect(self, card_id, player, oracle_text):
        """Register effects that double counters or tokens."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        
        # Check for counter doubling
        if "double the number of" in oracle_text.lower() and "counter" in oracle_text.lower():
            def counter_doubling_condition(context):
                # Only apply to counter events
                counter_type = context.get('counter_type', '')
                # Check if this counter type is included in the effect
                counter_text_matches = True
                if "+1/+1" in oracle_text.lower() and not "+1/+1" in counter_type:
                    counter_text_matches = False
                elif "-1/-1" in oracle_text.lower() and not "-1/-1" in counter_type:
                    counter_text_matches = False
                    
                # Check controller constraints
                target_id = context.get('target_id')
                controller_constrained = "you control" in oracle_text.lower()
                
                if controller_constrained:
                    # Find controller of the target
                    target_controller = None
                    for p in [self.game_state.p1, self.game_state.p2]:
                        if target_id in p.get('battlefield', []):
                            target_controller = p
                            break
                    
                    return counter_text_matches and target_controller == player
                
                return counter_text_matches
            
            def counter_doubling_replacement(context):
                # Double the number of counters
                counter_amount = context.get('counter_amount', 1)
                modified_context = dict(context)
                modified_context['counter_amount'] = counter_amount * 2
                
                # Log the doubling
                target_id = context.get('target_id')
                target_card = self.game_state._safe_get_card(target_id)
                target_name = target_card.name if target_card and hasattr(target_card, 'name') else f"Card {target_id}"
                counter_type = context.get('counter_type', '')
                
                logging.debug(f"Doubled {counter_type} counters on {target_name} from {counter_amount} to {counter_amount * 2} with {source_name}")
                
                return modified_context
            
            # Register the effect
            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': 'COUNTER',
                'condition': counter_doubling_condition,
                'replacement': counter_doubling_replacement,
                'duration': 'permanent',
                'controller_id': player,
                'description': f"{source_name} counter doubling effect"
            })
            
            logging.debug(f"Registered counter doubling effect for {source_name}")
            return effect_id
        
        # Check for token doubling
        if "double" in oracle_text.lower() and "token" in oracle_text.lower():
            def token_doubling_condition(context):
                # Only apply to token creation events
                event_type = context.get('event_type', '')
                is_token = context.get('is_token', False)
                
                # Check controller constraints
                creator = context.get('creator')
                controller_constrained = "you control" in oracle_text.lower() or "under your control" in oracle_text.lower()
                
                if controller_constrained:
                    return is_token and event_type == 'CREATE_TOKEN' and creator == player
                
                return is_token and event_type == 'CREATE_TOKEN'
            
            def token_doubling_replacement(context):
                # Double the number of tokens
                token_count = context.get('token_count', 1)
                modified_context = dict(context)
                modified_context['token_count'] = token_count * 2
                
                # Log the doubling
                token_name = context.get('token_data', {}).get('name', 'token')
                
                logging.debug(f"Doubled token creation from {token_count} to {token_count * 2} {token_name} tokens with {source_name}")
                
                return modified_context
            
            # Register the effect
            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': 'CREATE_TOKEN',
                'condition': token_doubling_condition,
                'replacement': token_doubling_replacement,
                'duration': 'permanent',
                'controller_id': player,
                'description': f"{source_name} token doubling effect"
            })
            
            logging.debug(f"Registered token doubling effect for {source_name}")
            return effect_id
        
        return None

    def _register_skip_phase_effect(self, card_id, player, oracle_text):
        """Register effects that cause players to skip phases or steps."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        
        # Identify which phase or step to skip
        phase_mapping = {
            "upkeep": self.game_state.PHASE_UPKEEP,
            "draw": self.game_state.PHASE_DRAW,
            "main": self.game_state.PHASE_MAIN_PRECOMBAT,
            "beginning of combat": self.game_state.PHASE_BEGIN_COMBAT,
            "combat": self.game_state.PHASE_DECLARE_ATTACKERS,
            "end": self.game_state.PHASE_END_STEP,
            "untap": self.game_state.PHASE_UNTAP
        }
        
        target_phase = None
        for phase_name, phase_value in phase_mapping.items():
            if phase_name in oracle_text.lower():
                target_phase = phase_value
                break
        
        if target_phase is None:
            return None
        
        # Determine if it affects the controller, opponent, or both
        affects_you = "you" in oracle_text.lower() and not "opponent" in oracle_text.lower()
        affects_opponents = "opponent" in oracle_text.lower() or "each player" in oracle_text.lower()
        
        # Register the phase skipping effect
        def phase_skip_condition(context):
            current_phase = self.game_state.phase
            active_player = self.game_state._get_active_player()
            
            # Only apply when entering the targeted phase
            if current_phase != target_phase:
                return False
            
            # Check who it affects
            if affects_you and active_player == player:
                return True
            if affects_opponents and active_player != player:
                return True
            if not affects_you and not affects_opponents:
                # Default to affecting everyone
                return True
            
            return False
        
        def phase_skip_replacement(context):
            # Skip to the next phase
            current_phase_idx = list(phase_mapping.values()).index(target_phase)
            next_phase = list(phase_mapping.values())[min(current_phase_idx + 1, len(phase_mapping) - 1)]
            
            # Store the phase skip in the context
            modified_context = dict(context)
            modified_context['skip_to_phase'] = next_phase
            
            # Log the skip
            active_player = self.game_state._get_active_player()
            player_name = "Your" if active_player == player else "Opponent's"
            phase_name = [name for name, value in phase_mapping.items() if value == target_phase][0]
            
            logging.debug(f"{player_name} {phase_name} phase skipped due to {source_name}")
            
            # Actual phase change happens in the game state
            self.game_state.phase = next_phase
            
            return modified_context
        
        # Determine duration
        duration = 'permanent'
        if "until end of turn" in oracle_text.lower():
            duration = 'end_of_turn'
        elif "until your next turn" in oracle_text.lower():
            duration = 'until_my_next_turn'
        
        # Register the effect
        effect_id = self.register_effect({
            'source_id': card_id,
            'event_type': 'PHASE_CHANGE',
            'condition': phase_skip_condition,
            'replacement': phase_skip_replacement,
            'duration': duration,
            'controller_id': player,
            'description': f"{source_name} phase skip effect"
        })
        
        logging.debug(f"Registered phase skip effect for {source_name}")
        return effect_id

    def _register_damage_prevention(self, card_id, player, oracle_text):
        """Register damage prevention effects."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        
        # Look for prevention patterns
        prevent_all = "prevent all damage" in oracle_text
        prevent_combat = "prevent all combat damage" in oracle_text
        prevent_noncombat = "prevent all noncombat damage" in oracle_text
        
        # Target restrictions
        to_you = "to you" in oracle_text
        to_creatures = "to creatures" in oracle_text
        you_control = "you control" in oracle_text
        
        def damage_condition(context):
            # Get details from the damage event
            target_id = context.get('target_id')
            target_is_player = context.get('target_is_player', False)
            is_combat_damage = context.get('is_combat_damage', False)
            damage_amount = context.get('damage_amount', 0)
            
            # Check if this damage should be prevented
            if prevent_all:
                pass  # No additional check needed
            elif prevent_combat and not is_combat_damage:
                return False
            elif prevent_noncombat and is_combat_damage:
                return False
                
            # Check target restrictions
            if to_you:
                return target_is_player and target_id == player
            elif to_creatures and not target_is_player:
                creature_card = self.game_state._safe_get_card(target_id)
                is_creature = creature_card and hasattr(creature_card, 'card_types') and 'creature' in creature_card.card_types
                
                if not is_creature:
                    return False
                    
                if you_control:
                    # Check if creature is controlled by the effect's controller
                    for card_id in player.get('battlefield', []):
                        if card_id == target_id:
                            return True
                    return False
            
            return True
        
        def damage_replacement(context):
            # Simply reduce damage to 0
            modified_context = dict(context)
            modified_context['damage_amount'] = 0
            
            # Log the prevention
            target_id = context.get('target_id')
            target_name = "Unknown"
            if context.get('target_is_player', False):
                target_name = "Player"
            else:
                target_card = self.game_state._safe_get_card(target_id)
                if target_card and hasattr(target_card, 'name'):
                    target_name = target_card.name
                    
            damage_amount = context.get('damage_amount', 0)
            logging.debug(f"Prevented {damage_amount} damage to {target_name} with {source_name}")
            
            return modified_context
        
        duration = 'permanent'
        if "until end of turn" in oracle_text:
            duration = 'end_of_turn'
        elif "until your next turn" in oracle_text:
            duration = 'until_my_next_turn'
        
        self.register_effect({
            'source_id': card_id,
            'event_type': 'DAMAGE',
            'condition': damage_condition,
            'replacement': damage_replacement,
            'duration': duration,
            'controller_id': player,
            'description': f"{source_name} damage prevention effect"
        })
        
        logging.debug(f"Registered damage prevention effect for {source_name}")

    def _register_lifelink_effect(self, card_id, player):
        """Register the lifelink replacement effect."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        
        def lifelink_condition(context):
            # Only apply for damage dealt by this creature
            source_id = context.get('source_id')
            return source_id == card_id and context.get('damage_amount', 0) > 0
        
        def lifelink_replacement(context):
            # Get the damage amount
            damage_amount = context.get('damage_amount', 0)
            
            # Original context is unchanged, but we trigger a life gain
            if hasattr(player, 'life'):
                player['life'] += damage_amount
                logging.debug(f"Lifelink: {source_name} caused player to gain {damage_amount} life")
            
            # Return unmodified context (lifelink doesn't replace the damage)
            return context
        
        self.register_effect({
            'source_id': card_id,
            'event_type': 'DAMAGE',
            'condition': lifelink_condition,
            'replacement': lifelink_replacement,
            'duration': 'until_source_leaves',
            'controller_id': player,
            'description': f"{source_name} lifelink effect"
        })
        
        logging.debug(f"Registered lifelink effect for {source_name}")

    def _register_deathtouch_effect(self, card_id, player):
        """Register the deathtouch replacement effect."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        
        def deathtouch_condition(context):
            # Only apply for damage dealt by this creature to another creature
            source_id = context.get('source_id')
            target_is_player = context.get('target_is_player', False)
            return source_id == card_id and not target_is_player and context.get('damage_amount', 0) > 0
        
        def deathtouch_replacement(context):
            # Get the target creature
            target_id = context.get('target_id')
            target_card = self.game_state._safe_get_card(target_id)
            target_name = target_card.name if target_card and hasattr(target_card, 'name') else f"Card {target_id}"
            
            # Mark damage as deathtouch damage (for state-based actions)
            target_controller = self.game_state._find_card_controller(target_id)
            if target_controller:
                if not hasattr(target_controller, 'deathtouch_damage'):
                    target_controller['deathtouch_damage'] = {}
                target_controller['deathtouch_damage'][target_id] = context.get('damage_amount', 0)
                logging.debug(f"Deathtouch: {source_name} dealt deathtouch damage to {target_name}")
            
            # Return unmodified context (deathtouch doesn't change the damage amount)
            return context
        
        self.register_effect({
            'source_id': card_id,
            'event_type': 'DAMAGE',
            'condition': deathtouch_condition,
            'replacement': deathtouch_replacement,
            'duration': 'until_source_leaves',
            'controller_id': player,
            'description': f"{source_name} deathtouch effect"
        })
        
        logging.debug(f"Registered deathtouch effect for {source_name}")
        
        # Scan battlefield for cards with continuous effects
    def register_common_effects(self):
        """Register commonly used continuous effects from cards on the battlefield."""
        if not hasattr(self, 'layer_system') or not self.layer_system:
            return

        # Ensure p1 and p2 exist before proceeding
        if not (hasattr(self, 'p1') and hasattr(self, 'p2')):
            return

        # Clear existing effects first
        self.layer_system.layers = {
            1: [],  # Copy effects
            2: [],  # Control-changing effects
            3: [],  # Text-changing effects
            4: [],  # Type-changing effects
            5: [],  # Color-changing effects
            6: [],  # Ability adding/removing effects
            7: {    # Power/toughness with sublayers
                'a': [],  # Set P/T to specific values
                'b': [],  # Modify P/T (+N/+N counters)
                'c': [],  # Modify P/T (other effects)
                'd': [],  # P/T switching effects
            }
        }

        # Scan battlefield for cards with continuous effects
        for player in [self.p1, self.p2]:
            for card_id in player["battlefield"]:
                card = self._safe_get_card(card_id)
                if not card or not hasattr(card, 'oracle_text'):
                    continue

                oracle_text = card.oracle_text.lower()

                # Type-changing effects
                if "all creatures are" in oracle_text or "each creature is" in oracle_text:
                    if "in addition to" in oracle_text:
                        types_added = []
                        if "artifact" in oracle_text:
                            types_added.append("artifact")
                        for creature_id in self._get_all_creatures():
                            for type_to_add in types_added:
                                self.layer_system.register_effect({
                                    'source_id': card_id,
                                    'layer': 4,
                                    'affected_ids': [creature_id],
                                    'effect_type': 'add_type',
                                    'effect_value': type_to_add,
                                    'duration': 'permanent',
                                    'only_battlefield': True
                                })

                # P/T setting effects
                if "creatures you control are" in oracle_text and "/" in oracle_text:
                    import re
                    pt_match = re.search(r'are (\d+)/(\d+)', oracle_text)
                    if pt_match:
                        power = int(pt_match.group(1))
                        toughness = int(pt_match.group(2))
                        for creature_id in self._get_player_creatures(player):
                            self.layer_system.register_effect({
                                'source_id': card_id,
                                'layer': 7,
                                'sublayer': 'a',
                                'affected_ids': [creature_id],
                                'effect_type': 'set_pt',
                                'effect_value': (power, toughness),
                                'duration': 'permanent',
                                'only_battlefield': True
                            })
                            
                # Color-changing effects
                if "all creatures are" in oracle_text and any(color in oracle_text for color in ["white", "blue", "black", "red", "green"]):
                    colors = [0, 0, 0, 0, 0]  # [W, U, B, R, G]
                    if "white" in oracle_text:
                        colors[0] = 1
                    if "blue" in oracle_text:
                        colors[1] = 1
                    if "black" in oracle_text:
                        colors[2] = 1
                    if "red" in oracle_text:
                        colors[3] = 1
                    if "green" in oracle_text:
                        colors[4] = 1
                    
                    for creature_id in self._get_all_creatures():
                        self.layer_system.register_effect({
                            'source_id': card_id,
                            'layer': 5,
                            'affected_ids': [creature_id],
                            'effect_type': 'set_color',
                            'effect_value': colors,
                            'duration': 'permanent',
                            'only_battlefield': True
                        })
                
                # Keyword ability granting effects
                keywords = ["flying", "first strike", "deathtouch", "double strike", "haste", 
                            "hexproof", "indestructible", "lifelink", "reach", "trample", "vigilance"]
                
                for keyword in keywords:
                    keyword_pattern = f"creatures you control (have|gain) {keyword}"
                    if re.search(keyword_pattern, oracle_text):
                        for creature_id in self._get_player_creatures(player):
                            self.layer_system.register_effect({
                                'source_id': card_id,
                                'layer': 6,
                                'affected_ids': [creature_id],
                                'effect_type': 'add_ability',
                                'effect_value': keyword,
                                'duration': 'permanent',
                                'only_battlefield': True
                            })
                
                # P/T modification effects (+X/+Y to creatures)
                pt_boost_match = re.search(r'creatures you control get \+(\d+)/\+(\d+)', oracle_text)
                if pt_boost_match:
                    power_boost = int(pt_boost_match.group(1))
                    toughness_boost = int(pt_boost_match.group(2))
                    
                    for creature_id in self._get_player_creatures(player):
                        self.layer_system.register_effect({
                            'source_id': card_id,
                            'layer': 7,
                            'sublayer': 'c',
                            'affected_ids': [creature_id],
                            'effect_type': 'modify_pt',
                            'effect_value': (power_boost, toughness_boost),
                            'duration': 'permanent',
                            'only_battlefield': True
                        })


    def _get_all_creatures(self):
        """Get IDs of all creatures on the battlefield."""
        creature_ids = []
        for player in [self.p1, self.p2]:
            for card_id in player["battlefield"]:
                card = self._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    creature_ids.append(card_id)
        return creature_ids

    def _get_player_creatures(self, player):
        """Get IDs of all creatures controlled by a specific player."""
        creature_ids = []
        for card_id in player["battlefield"]:
            card = self._safe_get_card(card_id)
            if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                creature_ids.append(card_id)
        return creature_ids
    
    def _find_card_controller(self, card_id):
        """Find which player controls a card."""
        gs = self.game_state
        
        for player in [gs.p1, gs.p2]:
            if card_id in player["battlefield"]:
                return player
                
        return None
    
    def _determine_event_type(self, action):
        """Determine the event type from action text with improved recognition."""
        action = action.lower()
        
        # Map action phrases to event types
        action_to_event = {
            'draw': 'DRAW',
            'deal damage': 'DAMAGE',
            'be dealt damage': 'DAMAGE',
            'die': 'DIES',
            'be destroyed': 'DESTROY',
            'enter the battlefield': 'ENTERS_BATTLEFIELD',
            'cast': 'CAST_SPELL',
            'be countered': 'COUNTER_SPELL',
            'gain life': 'LIFE_GAIN',
            'lose life': 'LIFE_LOSS',
            'discard': 'DISCARD',
            'sacrifice': 'SACRIFICE',
            'put into exile': 'EXILE',
            'create a token': 'CREATE_TOKEN',
            'get a counter': 'ADD_COUNTER',
            'put a counter': 'ADD_COUNTER',
        }
        
        # Find the matching event type
        for action_phrase, event_type in action_to_event.items():
            if action_phrase in action:
                return event_type
        
        # Default to generic replacement
        return 'GENERIC_REPLACEMENT'
        
    def cleanup_expired_effects(self):
        """Remove effects that have expired."""
        current_turn = self.game_state.turn
        current_phase = self.game_state.phase
        
        effects_to_remove = []
        
        for effect in self.active_effects:
            duration = effect.get('duration', 'permanent')
            effect_id = effect.get('effect_id')
            
            if duration == 'permanent':
                # Permanent effects don't expire
                continue
                
            elif duration == 'end_of_turn' and effect.get('start_turn', 0) < current_turn:
                effects_to_remove.append(effect_id)
                
            elif duration == 'end_of_combat' and (
                effect.get('start_turn', 0) < current_turn or 
                (effect.get('start_turn', 0) == current_turn and 
                current_phase > self.game_state.PHASE_END_COMBAT)):
                effects_to_remove.append(effect_id)
                
            elif duration == 'next_turn' and effect.get('start_turn', 0) < current_turn - 1:
                effects_to_remove.append(effect_id)
                
            elif duration == 'until_my_next_turn':
                # Check if it's now the controller's turn
                active_player = self.game_state._get_active_player()
                is_active_p1 = (active_player == self.game_state.p1)
                
                # If we've gone around to the controller's turn again
                if (is_active_p1 == effect.get('controller_is_p1', False) and 
                    effect.get('start_turn', 0) < current_turn):
                    effects_to_remove.append(effect_id)
                    
            elif duration == 'until_source_leaves':
                # Check if source card is still on the battlefield
                source_id = effect.get('source_id')
                source_on_battlefield = False
                
                for player in [self.game_state.p1, self.game_state.p2]:
                    if source_id in player.get('battlefield', []):
                        source_on_battlefield = True
                        break
                        
                if not source_on_battlefield:
                    effects_to_remove.append(effect_id)
                    
            elif duration == 'conditional':
                # For conditional duration, check the condition function
                if 'duration_condition' in effect and callable(effect['duration_condition']):
                    try:
                        if not effect['duration_condition']():
                            effects_to_remove.append(effect_id)
                    except Exception as e:
                        logging.error(f"Error in duration condition for effect {effect_id}: {str(e)}")
                        # If the condition function fails, we should probably remove the effect
                        effects_to_remove.append(effect_id)
        
        # Remove expired effects
        for effect_id in effects_to_remove:
            self.remove_effect(effect_id)
            logging.debug(f"Removed expired replacement effect {effect_id}")
