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

        # Scan for standard replacement patterns ("if..would..instead")
        if "if" in oracle_text.lower() and "would" in oracle_text.lower() and "instead" in oracle_text.lower():
            effect_ids = self._register_if_would_instead_effect(card_id, player, oracle_text)
            registered_effects.extend(effect_ids)

        # Scan for "As ~ enters the battlefield" replacement effects (e.g., choose a type)
        if "as this permanent enters the battlefield" in oracle_text.lower() or \
           "as this creature enters the battlefield" in oracle_text.lower() or \
           re.search(r"as .* enters the battlefield", oracle_text.lower()):
            effect_id = self._register_as_enters_effect(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)

        # Scan for ETB with counters effects (separate from 'as enters')
        # Make this more specific to avoid overlap with 'as enters' counter effects
        if "enters the battlefield with" in oracle_text.lower() and "counter" in oracle_text.lower() and not "as this permanent enters" in oracle_text.lower():
            effect_id = self._register_etb_with_effect(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)

        # Scan for damage prevention effects
        if "prevent" in oracle_text.lower() and "damage" in oracle_text.lower():
            effect_id = self._register_damage_prevention(card_id, player, oracle_text)
            if effect_id:
                registered_effects.append(effect_id)

        # Scan for effects that exile instead of going to graveyard ("exile it instead")
        if "if a creature would die" in oracle_text.lower() and "exile it instead" in oracle_text.lower():
             effect_id = self._register_exile_instead_of_death(card_id, player, oracle_text)
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
        if "double" in oracle_text.lower() and any(word in oracle_text.lower() for word in ["counters", "tokens", "damage", "mana"]):
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

        # 6. Card draw replacements (e.g., Dredge - though dredge might be handled differently)
        if "if you would draw a card" in oracle_text.lower() and "instead" in oracle_text.lower():
            effect_id = self._register_draw_replacement(card_id, player, oracle_text)
            if effect_id:
                 registered_effects.append(effect_id)

        return registered_effects
    
    def _register_mana_doubling_effect(self, card_id, player, oracle_text):
        """Register mana doubling effects (simplified)."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        effect_id = None

        # Simple pattern: "If you tap a [type] for mana, it produces twice as much"
        match = re.search(r"tap a (\w+) for mana, it produces twice as much", oracle_text.lower())
        if match:
             tapped_type = match.group(1) # e.g., 'permanent', 'land', 'forest'

             def condition(ctx):
                 # Check if event is mana production via tapping
                 if ctx.get('source_action') != 'tap_for_mana': return False
                 # Check if player matches
                 if ctx.get('player') != player: return False
                 # Check if source permanent type matches
                 source_perm_id = ctx.get('source_permanent_id')
                 source_perm = self.game_state._safe_get_card(source_perm_id)
                 if not source_perm: return False
                 if tapped_type == 'permanent': return True
                 if tapped_type == 'land' and 'land' in getattr(source_perm, 'type_line',''): return True
                 if tapped_type in getattr(source_perm, 'subtypes', []): return True
                 # Add more specific type checks if needed
                 return False

             def replacement(ctx):
                 mana_produced = ctx.get('mana_produced', {}) # Expecting dict like {'G': 1}
                 doubled_mana = {k: v*2 for k,v in mana_produced.items()}
                 ctx['mana_produced'] = doubled_mana
                 logging.debug(f"{source_name}: Doubled mana production from {source_perm_id}. Original: {mana_produced}, New: {doubled_mana}")
                 return ctx

             duration = self._determine_duration(oracle_text.lower())
             effect_id = self.register_effect({
                 'source_id': card_id,
                 'event_type': 'PRODUCE_MANA', # Need a suitable event type
                 'condition': condition,
                 'replacement': replacement,
                 'duration': duration,
                 'controller_id': player,
                 'description': f"{source_name} Mana Doubling ({tapped_type})"
             })
             if effect_id: logging.debug(f"Registered Mana Doubling effect for {source_name}")

        return effect_id
    
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
    
    def _register_exile_instead_of_death(self, card_id, player, oracle_text):
        """Register effects like Rest in Peace."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"

        # Determine scope (e.g., "creatures", "cards", specific types?)
        subject = "creature" # Default based on common phrasing
        if "if a card would be put into a graveyard" in oracle_text.lower():
            subject = "card"
        # TODO: Add more specific subject parsing if needed

        def condition(ctx):
            # Check if the event is a 'dies' or 'put into graveyard' event
            target_card_id = ctx.get('card_id')
            if not target_card_id: return False

            target_card = self.game_state._safe_get_card(target_card_id)
            if not target_card: return False

            # Check if subject matches
            if subject == "creature" and 'creature' not in getattr(target_card, 'card_types', []):
                return False
            if subject == "card": # Applies to any card going to GY from anywhere? Check rules.
                # Assume it applies if card is involved
                pass

            # Check source zone (usually battlefield for 'dies')
            # Rule 614.1a: Only applies if source is on battlefield unless specified. Assume source is the card itself.
            return card_id in player["battlefield"] # Effect only active if source is on BF

        def replacement(ctx):
            logging.debug(f"{source_name}: Exiling {ctx.get('card_id')} instead of sending to graveyard.")
            ctx['to_zone'] = 'exile' # Change destination zone
            return ctx

        # Usually permanent duration while source is on battlefield
        duration = 'until_source_leaves'
        effect_id = self.register_effect({
            'source_id': card_id,
            'event_type': 'DIES', # Also need to register for other 'to graveyard' events?
            'condition': condition,
            'replacement': replacement,
            'duration': duration,
            'controller_id': player,
            'description': f"{source_name} Exile instead of Death"
        })
        # Might need to register for other events like DISCARD -> Graveyard, MILL -> Graveyard too if it affects all cards
        # For now, just handle DIES.

        if effect_id: logging.debug(f"Registered Exile Instead of Death effect for {source_name}")
        return effect_id
    
    def _register_as_enters_effect(self, card_id, player, oracle_text):
        """Register 'As ~ enters the battlefield' replacement effects (e.g., choosing a type/color)."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        effect_id = None

        # Common patterns: Choose a type, choose a color, choose an opponent
        choice_needed = None
        if "choose a card type" in oracle_text.lower(): choice_needed = "card_type"
        elif "choose a color" in oracle_text.lower(): choice_needed = "color"
        elif "choose an opponent" in oracle_text.lower(): choice_needed = "opponent"
        # TODO: Add pattern for choosing counters if done with "as enters"

        if choice_needed:
            def condition(ctx):
                # Applies only when THIS card is entering
                return ctx.get('card_id') == card_id

            def replacement(ctx):
                logging.debug(f"Applying 'As enters' effect from {source_name} ({card_id})")
                # Signal that a choice is required BEFORE the card fully enters.
                # The GameState needs to handle pausing resolution to get this choice.
                ctx['as_enters_choice_needed'] = choice_needed
                ctx['as_enters_source_id'] = card_id
                # The actual effect application (e.g., granting protection) will often be
                # handled by a static ability that *uses* the choice made here.
                # The choice itself is stored (e.g., on the card or in GameState tracking).
                return ctx

            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': 'ENTERS_BATTLEFIELD',
                'condition': condition,
                'replacement': replacement,
                'duration': 'self', # Applies only to the ETB event itself
                'controller_id': player,
                'description': f"{source_name} 'As enters' choice"
            })
            if effect_id: logging.debug(f"Registered 'As enters' effect for {source_name}")

        return effect_id
    
    def _register_draw_replacement(self, card_id, player, oracle_text):
        """Register standard 'if you would draw... instead...' effects."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        effect_id = None

        replacement_text = ""
        match = re.search(r"if you would draw a card.*?, instead (.*?)(?:\.|$)", oracle_text.lower())
        if match: replacement_text = match.group(1).strip()

        if replacement_text:
            def condition(ctx):
                # Only affects the controller of the source card
                return ctx.get('player') == player

            replacement_func = self._create_enhanced_replacement_function("DRAW", replacement_text, player, source_name)

            duration = self._determine_duration(oracle_text.lower())

            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': 'DRAW',
                'condition': condition,
                'replacement': replacement_func,
                'duration': duration,
                'controller_id': player,
                'description': f"{source_name} Draw Replacement"
            })
            if effect_id: logging.debug(f"Registered Draw Replacement effect for {source_name}")

        return effect_id
    
    def _extract_replacement_clauses(self, text):
        """Extract replacement clauses from card text with better pattern recognition."""
        clauses = []

        # Common replacement patterns with named groups
        patterns = [
            # If X would Y, instead Z
            r'if (?P<subject>[^,]+?) would (?P<action>[^,]+?), (?:instead )?(?P<replacement>[^\.]+)\.?',
            # If X would Y, Z instead
            r'if (?P<subject>[^,]+?) would (?P<action>[^,]+?), (?P<replacement>[^\.]+?) instead\.?',
            # Instead of X doing Y, Z
            r'instead of (?P<subject>[^,]+?) (?P<action>[^,]+?), (?P<replacement>[^\.]+)\.?',
            # As X enters..., ... instead
            r'as (?P<subject>[^,]+?) enters.*?, (?P<replacement>[^\.]+) instead\.?',
            # X is replaced by Y
            r'(?P<action>[^,]+?) is replaced by (?P<replacement>[^\.]+)\.?' # Subject might be implicit
        ]

        # Normalize potential line breaks affecting patterns
        text = text.replace('\n', ' ')

        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
                subject = match.groupdict().get('subject', 'it').strip() # Default subject if pattern misses it
                action = match.groupdict().get('action', '').strip()
                replacement = match.groupdict().get('replacement', '').strip()

                # Determine event type from action
                event_type = self._determine_event_type(action)

                # Extract condition (if any) - check within replacement text first
                condition = None
                if " if " in replacement:
                    parts = replacement.split(" if ", 1)
                    replacement = parts[0].strip()
                    condition = parts[1].strip()
                # Also check subject for simple conditions like "a creature an opponent controls"
                if condition is None and "opponent controls" in subject:
                     # This simple check is weak, _create_enhanced_condition_function is better
                     # condition = f"subject controller is opponent" # Example marker
                     pass

                clauses.append({
                    'clause': match.group(0),
                    'event_type': event_type,
                    'subject': subject,
                    'action': action,
                    'replacement': replacement,
                    'condition': condition
                })

        return clauses
    
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
        """Register standard 'if...would...instead' effects."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        registered_effects = []

        # Normalize text and extract clauses
        normalized_text = re.sub(r'\([^)]*\)', '', oracle_text.lower()).strip()
        clauses = self._extract_replacement_clauses(normalized_text)

        for clause_data in clauses:
            event_type = clause_data['event_type']
            if event_type == 'UNKNOWN': continue # Skip if we couldn't map action

            # Create condition and replacement functions
            condition_func = self._create_enhanced_condition_function(event_type, clause_data['subject'], clause_data['condition'], player)
            replacement_func = self._create_enhanced_replacement_function(event_type, clause_data['replacement'], player, source_name)

            duration = self._determine_duration(normalized_text)

            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': event_type,
                'condition': condition_func,
                'replacement': replacement_func,
                'duration': duration,
                'controller_id': player, # Store who controls the effect source
                'description': f"{source_name}: {clause_data['clause']}"
            })
            registered_effects.append(effect_id)
            logging.debug(f"Registered {event_type} replacement from {source_name}: {clause_data['clause']}")

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
        """Register 'enters the battlefield with' effects (e.g., counters)."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"

        # Check for counters
        counter_match = re.search(r'enters the battlefield with (\w+|\d+)\s+([\+\-\d/]+)\s+counters?', oracle_text.lower())
        if counter_match:
            count_word, counter_type = counter_match.groups()
            count = self._word_to_number(count_word)
            counter_type = counter_type.replace('/','_').upper() # Normalize like +1_+1

            def condition(ctx):
                return ctx.get('card_id') == card_id

            def replacement(ctx):
                 logging.debug(f"Applying ETB counters from {source_name} to {card_id}: {count} {counter_type}")
                 # Add counters info to the context, Layer system or ETB handler will use it
                 ctx['enter_counters'] = ctx.get('enter_counters', [])
                 ctx['enter_counters'].append({'type': counter_type, 'count': count})
                 return ctx

            effect_id = self.register_effect({
                'source_id': card_id, 'event_type': 'ENTERS_BATTLEFIELD',
                'condition': condition, 'replacement': replacement,
                'duration': 'permanent', 'controller_id': player,
                'description': f"{source_name} ETB with counters"
            })
            logging.debug(f"Registered ETB counter effect for {source_name}")
            return effect_id

        # Check for ETB tapped
        if "enters the battlefield tapped" in oracle_text.lower():
            def condition(ctx): return ctx.get('card_id') == card_id
            def replacement(ctx):
                 logging.debug(f"Applying ETB tapped from {source_name} to {card_id}")
                 ctx['enters_tapped'] = True
                 return ctx

            effect_id = self.register_effect({
                'source_id': card_id, 'event_type': 'ENTERS_BATTLEFIELD',
                'condition': condition, 'replacement': replacement,
                'duration': 'permanent', 'controller_id': player,
                'description': f"{source_name} ETB tapped"
            })
            logging.debug(f"Registered ETB tapped effect for {source_name}")
            return effect_id

        return None
    
    # Helper for word to number
    def _word_to_number(self, word):
        mapping = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        if word.isdigit(): return int(word)
        return mapping.get(word.lower(), 1)

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
        """Register effects that double counters, tokens, damage, or mana."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        registered_id = None
        
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
        
        # Check for damage doubling
        elif "double" in oracle_text.lower() and "damage" in oracle_text.lower():
            # ... existing damage doubling logic (add if not present) ...
            def damage_doubling_condition(context): return context.get('damage_amount', 0) > 0 # Basic condition
            def damage_doubling_replacement(context):
                 ctx = context.copy()
                 ctx['damage_amount'] = ctx.get('damage_amount', 0) * 2
                 logging.debug(f"{source_name} doubled damage.")
                 return ctx
            registered_id = self.register_effect({
                'source_id': card_id, 'event_type': 'DAMAGE',
                'condition': damage_doubling_condition, 'replacement': damage_doubling_replacement,
                'duration': 'permanent', 'controller_id': player, 'description': f"{source_name} Damage Doubling"})
            if registered_id: logging.debug(f"Registered damage doubling effect for {source_name}")

        # Check for mana doubling (call new helper)
        elif "double" in oracle_text.lower() and "mana" in oracle_text.lower():
            registered_id = self._register_mana_doubling_effect(card_id, player, oracle_text)
            # Logging handled inside helper
        
        # Check for token doubling
        elif "double" in oracle_text.lower() and "token" in oracle_text.lower():
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
        
        return registered_id

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
        """Register static damage prevention effects."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"

        prevent_amount = -1 # -1 means prevent all
        prevent_all = "prevent all damage" in oracle_text.lower()
        prevent_combat = "prevent all combat damage" in oracle_text.lower()
        prevent_noncombat = "prevent all noncombat damage" in oracle_text.lower()
        prevent_next = "prevent the next" in oracle_text.lower()

        if prevent_next:
             amount_match = re.search(r'prevent the next (\d+|x)', oracle_text.lower())
             if amount_match:
                  amount_str = amount_match.group(1)
                  prevent_amount = int(amount_str) if amount_str.isdigit() else 1 # Placeholder for X
                  # TODO: Handle X based on context

        # Target restrictions
        to_target = None
        if "would be dealt to you" in oracle_text.lower(): to_target = "player_self"
        elif "would be dealt to creatures you control" in oracle_text.lower(): to_target = "creatures_self"
        elif "would be dealt to target creature or player" in oracle_text.lower(): to_target = "any_target" # Simplified
        elif "would be dealt to any target" in oracle_text.lower(): to_target = "any_target"
        elif "would be dealt to target creature" in oracle_text.lower(): to_target = "creature"
        elif "would be dealt to permanents you control" in oracle_text.lower(): to_target = "permanents_self"


        def condition(ctx):
            # Check event type and context
            damage = ctx.get('damage_amount', 0)
            if damage <= 0: return False
            is_combat = ctx.get('is_combat_damage', False)

            if prevent_combat and not is_combat: return False
            if prevent_noncombat and is_combat: return False

            # Check target
            if to_target:
                 target_id = ctx.get('target_id')
                 target_is_player = ctx.get('target_is_player', False)
                 target_controller = self.game_state._find_card_controller(target_id) if not target_is_player else (self.game_state.p1 if target_id == "p1" else self.game_state.p2)

                 if to_target == "player_self" and (not target_is_player or target_controller != player): return False
                 if to_target == "creatures_self" and (target_is_player or target_controller != player): return False
                 # Add more target checks
                 if to_target == "any_target": pass # Allow any target

            return True # Passed checks

        def replacement(ctx):
            original_damage = ctx.get('damage_amount', 0)
            amount_to_prevent = original_damage if prevent_amount == -1 else min(original_damage, prevent_amount)
            ctx['damage_amount'] = max(0, original_damage - amount_to_prevent)
            logging.debug(f"{source_name} preventing {amount_to_prevent} damage. Remaining: {ctx['damage_amount']}")
            ctx['damage_prevented'] = amount_to_prevent
            # Handle "prevent next" by potentially removing effect after one use
            if prevent_next and amount_to_prevent > 0:
                # Need to reference effect_id which isn't easily available here
                # Flag context instead?
                ctx['used_one_shot_prevention'] = effect_id # Pass ID to check later
            return ctx

        duration = self._determine_duration(oracle_text.lower())
        effect_id = self.register_effect({
            'source_id': card_id, 'event_type': 'DAMAGE',
            'condition': condition, 'replacement': replacement,
            'duration': duration, 'controller_id': player,
            'description': f"{source_name} damage prevention"
        })
        logging.debug(f"Registered damage prevention effect for {source_name}")
        return effect_id
    
        # Helper for duration
    def _determine_duration(self, oracle_text_lower):
        if "until end of turn" in oracle_text_lower or "this turn" in oracle_text_lower: return 'end_of_turn'
        if "until your next turn" in oracle_text_lower: return 'until_my_next_turn'
        return 'permanent'


    def _register_lifelink_effect(self, card_id, player):
        """Register lifelink (as a damage replacement that adds a side effect)."""
        # Note: Lifelink is now often considered a static ability modifying damage,
        # but implementing as a replacement ensures it triggers correctly.
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"

        def condition(ctx):
            # Only applies when *this creature* deals damage
            return ctx.get('source_id') == card_id and ctx.get('damage_amount', 0) > 0

        def replacement(ctx):
            damage_dealt = ctx.get('damage_amount', 0)
            # Capture effect_id and player from outer scope (or get dynamically?)
            # NOTE: Capturing effect_id here can be tricky if it's assigned after function creation.
            # It might be better to pass necessary info into the scheduled function.
            lifelink_source_id = card_id # Use card_id as source
            lifelink_controller = player # Use captured player controller

            # Create a life gain side effect (doesn't replace the damage itself)
            if damage_dealt > 0:
                def gain_life_later():
                    # Verify controller still exists and hasn't lost
                    if lifelink_controller and lifelink_controller.get("life", 0) > 0:
                        # Apply life gain replacement effects to this gain
                        # Ensure self.apply_replacements is callable or exists
                        gain_context = {'player': lifelink_controller, 'life_amount': damage_dealt, 'source_type': 'lifelink'}
                        modified_gain_context, gain_replaced = self.apply_replacements("LIFE_GAIN", gain_context)
                        final_life_gain = modified_gain_context.get('life_amount', 0)

                        if final_life_gain > 0:
                            lifelink_controller['life'] += final_life_gain
                            logging.debug(f"Lifelink: {source_name} gained {final_life_gain} life.")
                            # Trigger "gain life" events
                            if hasattr(self.game_state, 'trigger_ability'):
                                # Pass lifelink source ID and controller
                                self.game_state.trigger_ability(lifelink_source_id, "LIFE_GAINED", {"amount": final_life_gain, "controller": lifelink_controller})
                    else:
                        logging.debug(f"Lifelink gain prevented for {source_name} (controller lost or invalid).")


                # Schedule the life gain after damage event fully resolves
                if not hasattr(self.game_state, 'delayed_triggers'): self.game_state.delayed_triggers = []
                self.game_state.delayed_triggers.append(gain_life_later)
            return ctx # Don't modify the damage event itself

        # Ensure effect_id is assigned *before* being potentially captured by the closure
        effect_id = f"replace_{self.effect_counter}" # Pre-assign ID

        # Register the effect
        registered_id = self.register_effect({
            'source_id': card_id, 'event_type': 'DAMAGE',
            'condition': condition, 'replacement': replacement,
            'duration': 'until_source_leaves', # Lifelink is tied to the card being on battlefield
            'controller_id': player,
            'description': f"{source_name} Lifelink",
            'effect_id': effect_id # Explicitly pass pre-assigned ID
        })
        # If register_effect reassigns ID, use the returned one if needed elsewhere
        # For the closure, using card_id might be safer if effect_id isn't stable

        logging.debug(f"Registered Lifelink effect for {source_name}")
        return registered_id # Return the actual registered ID


    def _register_deathtouch_effect(self, card_id, player):
        """Register deathtouch (as a damage replacement that adds a flag)."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"

        def condition(ctx):
            # Applies when this creature deals *any* damage > 0 to another creature
            return (ctx.get('source_id') == card_id and
                    ctx.get('damage_amount', 0) > 0 and
                    not ctx.get('target_is_player', False) and
                    ctx.get('target_id') is not None)

        def replacement(ctx):
            target_id = ctx.get('target_id')
            # Find controller using _find_card_controller helper
            target_controller = self._find_card_controller(target_id)
            if target_controller:
                 # Use setdefault for cleaner handling if dict doesn't exist
                 target_controller.setdefault('deathtouch_damage', {})[target_id] = True # Mark as lethal
                 logging.debug(f"Deathtouch: {source_name} marked damage to {target_id} as deathtouch.")
            return ctx # Don't modify the damage amount

        effect_id = self.register_effect({
            'source_id': card_id, 'event_type': 'DAMAGE',
            'condition': condition, 'replacement': replacement,
            'duration': 'until_source_leaves',
            'controller_id': player,
            'description': f"{source_name} Deathtouch"
        })
        logging.debug(f"Registered Deathtouch effect for {source_name}")
        return effect_id
        
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

        # Check players first if card_id might be a player ID string
        if card_id == "p1": return gs.p1
        if card_id == "p2": return gs.p2

        # Check zones for card objects
        for player in [gs.p1, gs.p2]:
            # Check primary zones where control matters most often
            if card_id in player.get("battlefield", []): return player
            if card_id in player.get("hand", []): return player
            if card_id in player.get("graveyard", []): return player
            if card_id in player.get("library", []): return player
            if card_id in player.get("exile", []): return player

        # Check stack
        for item in gs.stack:
            if isinstance(item, tuple) and len(item) >= 3 and item[1] == card_id:
                 return item[2] # The controller of the spell/ability

        # Check other potential locations tracked by GameState if needed
        # E.g., gs.phased_out, gs.suspended_cards (might store controller info)

        return None # Card not found or controller unclear
    
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
        
    # Cleanup method
    def cleanup_expired_effects(self):
        """Remove effects that have expired."""
        current_turn = self.game_state.turn
        # Need to know active player to handle 'until_my_next_turn'
        active_player = self.game_state._get_active_player() # Get current active player
        active_player_is_p1 = (active_player == self.game_state.p1)

        expired_ids = []
        for effect_data in list(self.active_effects): # Iterate over a copy
            effect_id = effect_data.get('effect_id')
            duration = effect_data.get('duration', 'permanent')
            start_turn = effect_data.get('start_turn', 0)

            is_expired = False
            if duration == 'permanent' or duration == 'until_source_leaves': # until_source_leaves handled separately
                is_expired = False # These don't expire based on time
            elif duration == 'end_of_turn':
                # Expired if it's not the turn it started AND it's the cleanup step or later
                is_expired = start_turn < current_turn or (start_turn == current_turn and current_phase >= self.game_state.PHASE_CLEANUP)
            elif duration == 'next_turn': # Expires at start of player's next turn AFTER the one it started
                # Hard to track perfectly without start player context, approximate:
                is_expired = start_turn < current_turn - 1
            elif duration == 'until_my_next_turn':
                effect_controller_is_p1 = effect_data.get('controller_is_p1')
                # Expires if it's the controller's turn again and it's *not* the turn it started
                is_expired = (effect_controller_is_p1 == active_player_is_p1 and start_turn < current_turn)
            elif duration == 'until_source_leaves':
                source_id = effect_data.get('source_id')
                source_location = self.game_state.find_card_location(source_id)
                if not source_location or source_location[1] != 'battlefield':
                    is_expired = True
            elif duration == 'conditional' and callable(effect_data.get('duration_condition')):
                try:
                    if not effect_data['duration_condition'](): is_expired = True
                except Exception as e:
                    logging.error(f"Error in duration condition for effect {effect_id}: {e}"); is_expired = True # Remove on error
            elif duration == 'one_shot': # Handled during apply_replacements usually
                pass # This should have been removed already if applied_once was True
            else: # Default for unknown or standard time-based durations
                # Assuming simple end-of-turn expiration for others for now
                is_expired = start_turn < current_turn


            if is_expired:
                expired_ids.append(effect_id)

        # Remove expired effects
        for effect_id in expired_ids:
            self.remove_effect(effect_id)
            logging.debug(f"Removed expired replacement effect {effect_id}")

    def remove_effects_by_source(self, source_id_to_remove):
        """Remove all replacement effects originating from a specific source."""
        effects_to_remove = [e['effect_id'] for e in self.active_effects if e.get('source_id') == source_id_to_remove]
        if effects_to_remove:
             logging.debug(f"Removing {len(effects_to_remove)} replacement effects from source {source_id_to_remove}")
             for effect_id in effects_to_remove:
                 self.remove_effect(effect_id)
