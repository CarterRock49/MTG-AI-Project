import logging
import re

from numpy import copy

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
        # Also handle "Whenever you tap..."
        match = re.search(r"(?:if|whenever)\s+you\s+tap\s+a(?:n)?\s+(\w+)\s+for\s+mana,\s+.*?(add|produce)s?\s+twice\s+that\s+much", oracle_text.lower())
        if match:
             tapped_type = match.group(1).lower() # e.g., 'permanent', 'land', 'forest'

             def condition(ctx):
                 # Check if event is mana production via tapping from player
                 if ctx.get('event_type') != 'PRODUCE_MANA': return False # Event needs to exist
                 if ctx.get('player') != player: return False
                 if not ctx.get('source_is_tap_ability', False): return False # Requires ability tapped source

                 # Check if source permanent type matches
                 source_perm_id = ctx.get('source_permanent_id')
                 source_perm = self.game_state._safe_get_card(source_perm_id)
                 if not source_perm: return False

                 if tapped_type == 'permanent': return True
                 perm_types = getattr(source_perm, 'card_types', [])
                 perm_subtypes = getattr(source_perm, 'subtypes', [])
                 if tapped_type in perm_types: return True
                 if tapped_type in perm_subtypes: return True
                 return False

             def replacement(ctx):
                 mana_produced = ctx.get('mana_produced', {}) # Expecting dict like {'G': 1}
                 doubled_mana = {k: v*2 for k,v in mana_produced.items()}
                 ctx['mana_produced'] = doubled_mana
                 source_perm_id = ctx.get('source_permanent_id', 'Unknown source')
                 logging.debug(f"{source_name}: Doubled mana production from {source_perm_id}. Original: {mana_produced}, New: {doubled_mana}")
                 return ctx

             duration = self._determine_duration(oracle_text.lower())
             effect_id = self.register_effect({
                 'source_id': card_id,
                 'event_type': 'PRODUCE_MANA', # Use a standardized event type
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
        effect_id = None

        # Pattern: "If [subject] would create [tokens], [subject] create(s) [replacement] instead."
        match = re.search(r"if (you|a player|an opponent) would create (?:one or more )?tokens?(?:.*?),\s*(?:instead )?(?:he or she creates?|they create|you create)\s*(.*?)(?:\.|$)", oracle_text.lower())
        if match:
            subject, replacement_text = match.groups()
            replacement_text = replacement_text.strip()

            # Determine what the replacement is
            modifier = "none"
            if "twice that many" in replacement_text: modifier = "double"
            elif "one additional" in replacement_text or "create that many plus one" in replacement_text: modifier = "plus_one"
            elif re.search(r"(?:two|three|four|five)\s+additional", replacement_text):
                 num_word = re.search(r"(two|three|four|five)", replacement_text).group(1)
                 modifier = f"plus_{self._word_to_number(num_word)}"
            elif "doesn't create" in replacement_text or "create no tokens" in replacement_text: modifier = "prevent"
            # Add more modifiers if needed

            def condition(ctx):
                event_player = ctx.get('creator')
                if subject == "you" and event_player != player: return False
                if subject == "an opponent" and event_player == player: return False
                if subject == "a player": pass # Always applies
                return True

            def replacement(ctx):
                 original_count = ctx.get('token_count', 1)
                 if modifier == "double": new_count = original_count * 2
                 elif modifier == "plus_one": new_count = original_count + 1
                 elif modifier.startswith("plus_"): new_count = original_count + int(modifier.split('_')[1])
                 elif modifier == "prevent": new_count = 0; ctx['prevented'] = True
                 else: new_count = original_count # No change if modifier unknown

                 if new_count != original_count:
                     logging.debug(f"{source_name}: Replacing token creation ({original_count}) with {new_count}.")
                     ctx['token_count'] = new_count
                 return ctx

            duration = self._determine_duration(oracle_text.lower())
            effect_id = self.register_effect({
                'source_id': card_id, 'event_type': 'CREATE_TOKEN',
                'condition': condition, 'replacement': replacement,
                'duration': duration, 'controller_id': player,
                'description': f"{source_name} Token Creation Replacement ({modifier})"
            })
            logging.debug(f"Registered Token Creation Replacement effect for {source_name}")

        return effect_id
    
    def _register_life_gain_replacement(self, card_id, player, oracle_text):
        """Register life gain replacement effects."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        effect_id = None

        # Pattern: "If [subject] would gain life, [replacement] instead."
        match = re.search(r"if (you|a player|an opponent) would gain life(?:.*?),\s*(?:instead )?(.*?)(?:\.|$)", oracle_text.lower())
        if match:
            subject, replacement_text = match.groups()
            replacement_text = replacement_text.strip()

            # Determine replacement type
            modifier = "none"
            if "gain twice that much" in replacement_text: modifier = "double"
            elif "gain that much life plus" in replacement_text:
                plus_match = re.search(r"plus (\d+)", replacement_text)
                modifier = f"plus_{plus_match.group(1)}" if plus_match else "plus_1"
            elif "gain no life" in replacement_text or "doesn't gain life" in replacement_text: modifier = "prevent"
            elif "each opponent loses that much life" in replacement_text: modifier = "opponent_lose"
            # Add more modifiers

            def condition(ctx):
                event_player = ctx.get('player') # Player gaining life
                if subject == "you" and event_player != player: return False
                if subject == "an opponent" and event_player == player: return False
                if subject == "a player": pass # Always applies
                return True

            def replacement(ctx):
                original_amount = ctx.get('life_amount', 0)
                new_amount = original_amount
                side_effect = None

                if modifier == "double": new_amount = original_amount * 2
                elif modifier.startswith("plus_"): new_amount = original_amount + int(modifier.split('_')[1])
                elif modifier == "prevent": new_amount = 0; ctx['prevented'] = True
                elif modifier == "opponent_lose":
                     side_effect = ("lose_life", original_amount)
                     # Original gain is NOT replaced unless explicitly stated otherwise

                if new_amount != original_amount:
                    logging.debug(f"{source_name}: Replacing life gain ({original_amount}) with {new_amount}.")
                    ctx['life_amount'] = new_amount

                # Handle side effects like opponent losing life
                if side_effect and side_effect[0] == "lose_life":
                     opponent = self.game_state.p2 if player == self.game_state.p1 else self.game_state.p1
                     loss_amount = side_effect[1]
                     if loss_amount > 0:
                         # Use LifeLossEffect for consistency? Or direct modification? Direct for now.
                         opponent['life'] -= loss_amount
                         logging.debug(f"{source_name}: Opponent loses {loss_amount} life due to replacement effect.")
                         # Trigger life loss event if needed
                         # self.game_state.trigger_ability(opponent_id, "LOSE_LIFE", ...)
                return ctx

            duration = self._determine_duration(oracle_text.lower())
            effect_id = self.register_effect({
                'source_id': card_id, 'event_type': 'LIFE_GAIN',
                'condition': condition, 'replacement': replacement,
                'duration': duration, 'controller_id': player,
                'description': f"{source_name} Life Gain Replacement ({modifier})"
            })
            logging.debug(f"Registered Life Gain Replacement effect for {source_name}")

        return effect_id
        
    def remove_effect(self, effect_id):
        """Remove a replacement effect."""
        self.active_effects = [effect for effect in self.active_effects 
                             if effect.get('effect_id') != effect_id]
    
    def apply_replacements(self, event_type, event_context):
            """Apply replacement effects with improved conflict resolution and Madness handling."""
            modified_context = dict(event_context)
            was_replaced = False
            card_id = event_context.get('card_id') # Get card_id early if available
            card = self.game_state._safe_get_card(card_id) if card_id else None

            # Get applicable effects from index
            applicable_effects = self.effect_index.get(event_type, [])

            # --- Madness Check (Specific logic before standard replacements) ---
            # If discard event, card exists, and has Madness:
            if event_type == "DISCARD" and card and "madness" in getattr(card, 'oracle_text', '').lower():
                logging.debug(f"Madness check for discarded card {card.name}")
                # Create a "potential" Madness replacement
                madness_cost_str = self.game_state._get_madness_cost_str_gs(card)
                if madness_cost_str:
                     # Create a dummy replacement effect data structure
                     madness_replacement_data = {
                         'source_id': card_id, # Source is the card itself
                         'event_type': 'DISCARD',
                         'condition': lambda ctx: True, # Condition met (discarding this card)
                         'replacement': self._create_madness_replacement_func(card_id, event_context['player'], madness_cost_str),
                         'controller_id': event_context['player'],
                         'description': f"{card.name} Madness",
                         'effect_id': f"madness_{card_id}", # Unique ID
                         'is_madness_effect': True # Flag this special replacement
                     }
                     # Add this potential replacement to the list to be considered
                     # It will compete based on standard replacement rules (616.1)
                     # Player controlling the DISCARDED card is the affected player
                     applicable_effects.append(madness_replacement_data)
                     logging.debug(f"Added potential Madness replacement for {card.name}")


            # --- Standard Replacement Processing (Starts Here) ---
            if not applicable_effects:
                return modified_context, was_replaced

            # Filter by condition
            # ... (keep existing condition filtering) ...
            valid_effects = []
            for effect in applicable_effects:
                # ... (expiration check) ...
                if self._is_effect_expired(effect, self.game_state.turn, self.game_state.phase):
                     logging.warning(f"Applying replacement effect {effect.get('effect_id')} that should potentially be expired.")

                condition_met = True
                if 'condition' in effect:
                    if callable(effect['condition']):
                        try:
                            condition_met = effect['condition'](copy.deepcopy(modified_context))
                        except Exception as e:
                            logging.error(f"Error evaluating condition for effect {effect.get('effect_id')}: {str(e)}")
                            condition_met = False
                    else:
                        condition_met = False
                if condition_met:
                    valid_effects.append(effect)

            if not valid_effects:
                return modified_context, was_replaced

            # Implement MTG rule 616.1 for replacement effect ordering
            # ... (keep existing affected player/object determination) ...
            affected_object_id = None
            affected_player = None
            possible_keys = ['target_id', 'card_id', 'player', 'affected_id']
            for key in possible_keys:
                if key in modified_context:
                    affected_object_id = modified_context[key]
                    break
            if affected_object_id:
                if isinstance(affected_object_id, dict) and 'name' in affected_object_id:
                    affected_player = affected_object_id
                    affected_object_id = affected_player['name']
                else:
                    affected_player = self.game_state.get_card_controller(affected_object_id) \
                                    if hasattr(self.game_state, 'get_card_controller') else None


            # Group effects (Self > Control > Other)
            # --- MODIFIED: Handle Madness positioning ---
            self_effects = []
            controller_effects = []
            other_effects = []

            for e in valid_effects:
                # Effect's controller
                effect_controller = self.game_state.get_card_controller(e.get('source_id'))
                # Is it a madness effect generated above? Controller is player discarding.
                is_madness = e.get('is_madness_effect', False)

                if is_madness:
                    # Madness effect applies if the AFFECTED player (discarder) chooses it.
                    # Group with controller effects, player chooses order among these.
                    if affected_player == e.get('controller_id'):
                         controller_effects.append(e)
                    else: # Should not happen, madness effect generated for discarder
                        other_effects.append(e)
                elif e.get('source_id') == affected_object_id: # Self-replacement
                     self_effects.append(e)
                elif affected_player and effect_controller == affected_player: # Controlled by affected player
                    controller_effects.append(e)
                else: # Other player's effect
                    other_effects.append(e)

            # Sort groups by timestamp
            # ... (keep existing sorting logic) ...
            get_timestamp = lambda e: self.game_state.card_db.get(e.get('source_id'), {}).get('_timestamp', e.get('start_turn', 0)) if hasattr(self.game_state, 'card_db') else e.get('start_turn', 0)
            self_effects.sort(key=get_timestamp)
            controller_effects.sort(key=get_timestamp)
            other_effects.sort(key=get_timestamp)

            # Combine ordered effects
            ordered_effects = self_effects + controller_effects + other_effects

            # Apply replacements sequentially (looping)
            # ... (keep existing loop logic, application, tracking) ...
            logging.debug(f"Applying {len(ordered_effects)} replacements for {event_type} to {affected_object_id}")
            active_effect_applied_in_loop = True
            applied_ids = set()
            max_replacement_loops = 10; current_loop = 0

            while active_effect_applied_in_loop and current_loop < max_replacement_loops:
                current_loop += 1
                active_effect_applied_in_loop = False

                # Re-filter applicable effects
                valid_effects_for_loop = []
                # --- ADDED: Track if a Madness effect is applicable in this loop ---
                applicable_madness_effect = None
                # --- END ADDED ---
                for effect in ordered_effects:
                    if effect.get('effect_id') in applied_ids: continue
                    condition_met = True
                    if 'condition' in effect and callable(effect['condition']):
                        try: condition_met = effect['condition'](copy.deepcopy(modified_context))
                        except Exception as e: condition_met = False; logging.error(f"Error re-evaluating condition for {effect.get('effect_id')}: {e}")
                    if condition_met:
                        valid_effects_for_loop.append(effect)
                        # --- ADDED: Track applicable Madness ---
                        if effect.get('is_madness_effect'):
                             applicable_madness_effect = effect
                        # --- END ADDED ---

                if not valid_effects_for_loop: break

                # --- Player Choice Point (Madness Priority - Rule 616.1) ---
                effect_to_apply = None
                # If a Madness replacement is applicable *and* the affected player controls it:
                if applicable_madness_effect and affected_player == applicable_madness_effect.get('controller_id'):
                     # Player (Affected) CHOOSES which replacement applies first (Madness vs others).
                     # AI choice: Prioritize Madness? Or other effect? Let's prioritize Madness for now.
                     effect_to_apply = applicable_madness_effect
                     logging.debug(f"Affected player ({affected_player['name']}) controls Madness effect. Prioritizing it.")
                # Otherwise, apply first valid effect based on timestamp order
                else:
                     effect_to_apply = valid_effects_for_loop[0]

                # --- Apply the Chosen Effect ---
                if effect_to_apply:
                    effect_id_applying = effect_to_apply.get('effect_id')
                    if 'replacement' in effect_to_apply and callable(effect_to_apply['replacement']):
                         # ... (apply effect, track, handle one-shot, as before) ...
                        source_id = effect_to_apply.get('source_id')
                        source_name = getattr(self.game_state._safe_get_card(source_id), 'name', source_id)
                        try:
                            original_context_before_apply = copy.deepcopy(modified_context)
                            result = effect_to_apply['replacement'](modified_context)
                            if result is not None: modified_context = result

                            # Only mark as replaced if the event wasn't just generating a side effect
                            # Madness replacement technically *replaces* GY destination with Exile, so it counts.
                            if modified_context != original_context_before_apply:
                                was_replaced = True # Set if context actually changed
                                active_effect_applied_in_loop = True
                                applied_ids.add(effect_id_applying)

                                logging.debug(f"Replacement effect '{effect_id_applying}' from {source_name} applied to {event_type} event.")
                                logging.debug(f"Context change: {original_context_before_apply} -> {modified_context}")

                                if effect_to_apply.get('apply_once', False):
                                     self.remove_effect(effect_id_applying)
                                     ordered_effects = [e for e in ordered_effects if e.get('effect_id') != effect_id_applying]
                        except Exception as e:
                             logging.error(f"Error in replacement function for effect {effect_id_applying} from {source_name}: {str(e)}")
                             ordered_effects = [e for e in ordered_effects if e.get('effect_id') != effect_id_applying]


            if current_loop >= max_replacement_loops:
                logging.error(f"Exceeded max replacement loops for event {event_type}.")

            return modified_context, was_replaced
        
    def _create_madness_replacement_func(self, card_id, player, madness_cost_str):
        """Creates the replacement function for Madness."""
        def madness_replacement(ctx):
            logging.debug(f"Madness replacing discard of {card_id} ({getattr(self.game_state._safe_get_card(card_id),'name', card_id)}) to graveyard -> exile.")
            ctx['to_zone'] = 'exile' # Redirect destination
            # Set state for casting opportunity
            if hasattr(self.game_state, 'madness_cast_available'):
                self.game_state.madness_cast_available = {
                    'card_id': card_id,
                    'player': player,
                    'cost': madness_cost_str
                }
                logging.debug(f"Madness opportunity set for {card_id}")
            else:
                 logging.error("GameState missing 'madness_cast_available' attribute.")
            return ctx
        return madness_replacement

    
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
        """Register effects like Rest in Peace ('If X would die, exile it instead')."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        effect_id = None

        # Pattern: "If [subject] would die, exile it instead."
        # Pattern: "If a nontoken creature would die..."
        match = re.search(r"if a (nontoken\s+)?(creature|permanent|card) would die(?:.*?),\s*exile it instead", oracle_text.lower())
        if match:
            is_nontoken, subject_type = match.groups()
            is_nontoken = bool(is_nontoken)

            def condition(ctx):
                # Only applies if source is on battlefield
                if card_id not in player.get("battlefield", []): return False
                # Event must be 'DIES'
                # if ctx.get('event_type') != 'DIES': return False # ApplyReplacements passes type

                target_card_id = ctx.get('card_id')
                if not target_card_id: return False
                target_card = self.game_state._safe_get_card(target_card_id)
                if not target_card: return False

                # Check subject type
                if subject_type == "creature" and 'creature' not in getattr(target_card, 'card_types', []): return False
                if subject_type == "permanent": # Assumes it's dying from battlefield
                     if ctx.get('from_zone') != 'battlefield': return False
                if subject_type == "card": # Can apply to cards from other zones too if text specifies
                    # Example: "If a card would be put into an opponent's graveyard..."
                    # Add specific checks based on full oracle text parsing if needed.
                    pass

                # Check nontoken restriction
                if is_nontoken and hasattr(target_card, 'is_token') and target_card.is_token:
                     return False

                return True

            def replacement(ctx):
                logging.debug(f"{source_name}: Replacing death with exile for {ctx.get('card_id')}.")
                ctx['to_zone'] = 'exile' # Change destination zone
                return ctx

            duration = 'until_source_leaves'
            effect_id = self.register_effect({
                'source_id': card_id,
                'event_type': 'DIES', # Register primarily for DIES event
                'condition': condition,
                'replacement': replacement,
                'duration': duration,
                'controller_id': player,
                'description': f"{source_name} Exile Instead of Death ({subject_type}{' nontoken' if is_nontoken else ''})"
            })
            # Consider if it also needs to apply to MILL, DISCARD events? Read card carefully.
            # Example: If card says "If a card would be put into a graveyard FROM ANYWHERE..."
            # register for other event types too.

            if effect_id: logging.debug(f"Registered Exile Instead of Death effect for {source_name}")

        return effect_id
    
    def _register_as_enters_effect(self, card_id, player, oracle_text):
        """Register 'As ~ enters the battlefield' replacement effects (e.g., choosing a type/color)."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        effect_id = None

        # Look for "As [this permanent] enters the battlefield, [action]"
        # Action is usually "choose" or placing counters
        match = re.search(r"as (?:this permanent|this creature|{}) enters the battlefield".format(re.escape(source_name.lower())), oracle_text.lower())
        if match:
             following_text = oracle_text.lower()[match.end():].split('.')[0].strip() # Get text after "enters..."

             choice_needed = None
             etb_counters = None

             if following_text.startswith(", choose"):
                 if "a card type" in following_text: choice_needed = "card_type"
                 elif "a color" in following_text: choice_needed = "color"
                 elif "an opponent" in following_text: choice_needed = "opponent"
                 elif "a creature type" in following_text: choice_needed = "creature_type"
                 # Add more common choices
             elif following_text.startswith("with"): # e.g., "with a +1/+1 counter"
                 counter_match = re.search(r"with (a|an|one|two|three|\d+)\s+([\+\-\d/]+\s+)?(\w+)\s+counters?", following_text)
                 if counter_match:
                     count_word, _, counter_type = counter_match.groups() # Ignored middle group for p/t marker
                     count = self._word_to_number(count_word)
                     counter_type = counter_type.strip()
                     etb_counters = {'type': counter_type, 'count': count}

             if choice_needed or etb_counters:
                 def condition(ctx):
                     # Applies only when THIS card is entering
                     return ctx.get('card_id') == card_id

                 def replacement(ctx):
                     logging.debug(f"Applying 'As enters' effect from {source_name} ({card_id})")
                     if choice_needed:
                         ctx['as_enters_choice_needed'] = choice_needed
                         ctx['as_enters_source_id'] = card_id
                         # The GameState needs to handle pausing resolution to get this choice.
                         # Choice is stored (e.g., on the card or GS tracking) for static abilities to use.
                     if etb_counters:
                          # Add counters info to the context, will be applied during ETB processing
                          ctx['enter_counters'] = ctx.get('enter_counters', [])
                          ctx['enter_counters'].append(etb_counters)
                          logging.debug(f"'As enters' adding counters: {etb_counters}")
                     return ctx

                 effect_id = self.register_effect({
                     'source_id': card_id,
                     'event_type': 'ENTERS_BATTLEFIELD',
                     'condition': condition,
                     'replacement': replacement,
                     'duration': 'self', # Applies only to the ETB event itself
                     'controller_id': player,
                     'description': f"{source_name} 'As enters' {choice_needed or 'counters'}"
                 })
                 if effect_id: logging.debug(f"Registered 'As enters' effect for {source_name}")

        return effect_id
    
    def _register_draw_replacement(self, card_id, player, oracle_text):
        """Register standard 'if you would draw... instead...' effects, including Dredge."""
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        effect_id = None

        # Standard "if you would draw a card..., instead..."
        match = re.search(r"if you would draw a card(?:.*?),\s*instead (.*?)(?:\.|$)", oracle_text.lower())
        if match:
            replacement_text = match.group(1).strip()

            def condition(ctx):
                # Only affects the controller of the source card
                return ctx.get('player') == player

            # Use the enhanced function creator
            replacement_func = self._create_enhanced_replacement_function("DRAW", replacement_text, player, source_name)

            duration = self._determine_duration(oracle_text.lower())

            effect_id = self.register_effect({
                'source_id': card_id, 'event_type': 'DRAW',
                'condition': condition, 'replacement': replacement_func,
                'duration': duration, 'controller_id': player,
                'description': f"{source_name} Draw Replacement: {replacement_text}"
            })
            if effect_id: logging.debug(f"Registered Draw Replacement effect for {source_name}")

        # Dredge specific replacement (from graveyard only)
        dredge_match = re.search(r"dredge (\d+)", oracle_text.lower())
        if dredge_match:
             dredge_value = int(dredge_match.group(1))

             def dredge_condition(ctx):
                 # Card must be in player's graveyard AND they must be about to draw
                 _, current_zone = self.game_state.find_card_location(card_id)
                 return ctx.get('player') == player and current_zone == "graveyard"

             def dredge_replacement(ctx):
                  # Replace the draw. Set up pending dredge state.
                  # Player needs to choose whether to dredge via an action.
                  if self.game_state.phase != self.game_state.PHASE_DRAW: # Check timing? Dredge usually replaces draw step draw.
                       # Allow dredge replacement anytime a draw would happen
                       pass

                  # Check if enough cards to mill
                  if len(player.get("library", [])) >= dredge_value:
                      logging.debug(f"Dredge opportunity: {source_name} (Dredge {dredge_value}) replacing draw.")
                      # Set state for player to choose dredge action
                      # Prevent the default draw
                      ctx['prevented'] = True
                      # Store dredge info for action handler
                      self.game_state.dredge_pending = {
                          'player': player,
                          'card_id': card_id,
                          'value': dredge_value,
                          'original_draw_context': ctx # Store original context if needed
                      }
                      # Need to enter a choice phase or similar
                      # self.game_state.phase = GameState.PHASE_CHOOSE # Example phase
                      # self.game_state.choice_context = {'type':'dredge', ...}
                  else:
                       logging.debug(f"Cannot dredge {source_name}: Not enough cards in library.")
                       # Don't prevent draw if dredge isn't possible

                  return ctx # Return modified (or original) context

             # Register this effect only when card is in graveyard? Complex.
             # Let condition handle it for now. Duration is permanent while in GY.
             dredge_effect_id = self.register_effect({
                 'source_id': card_id, 'event_type': 'DRAW',
                 'condition': dredge_condition, 'replacement': dredge_replacement,
                 'duration': 'permanent', 'controller_id': player,
                 'description': f"{source_name} Dredge {dredge_value}"
             })
             if dredge_effect_id: logging.debug(f"Registered Dredge effect for {source_name}")
             # Return the FIRST effect ID registered if multiple match
             if not effect_id: effect_id = dredge_effect_id

        return effect_id
    
    def _extract_replacement_clauses(self, text):
        """Extract replacement clauses, handling em dash."""
        clauses = []
        # Patterns accept em dash in some cases if it replaces other punctuation
        patterns = [
            r'if (?P<subject>[^,]+?) would (?P<action>[^,:\u2014]+?),?\s*(?:instead\s+)?(?P<replacement>[^\.]+)\.?', # Handle optional comma/dash before instead
            # Removed redundant/overlapping patterns
            r'as (?P<subject>[^,]+?) enters.*?,?\s*(?P<replacement>[^\.]+) instead\.?',
        ]
        text = text.replace('\n', ' ')

        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
                clause_data = match.groupdict()
                # Basic post-processing
                subject = clause_data.get('subject', 'it').strip()
                action = clause_data.get('action', '').strip()
                replacement = clause_data.get('replacement', '').strip()
                event_type = self._determine_event_type(action) if action else 'UNKNOWN' # Determine from action
                # Simple condition extraction
                condition = None
                if " if " in replacement:
                    parts = replacement.split(" if ", 1)
                    replacement = parts[0].strip()
                    condition = parts[1].strip()

                clauses.append({
                    'clause': match.group(0), 'event_type': event_type, 'subject': subject,
                    'action': action, 'replacement': replacement, 'condition': condition
                })

        # Add pattern specifically for Dredge "If you would draw..., instead you may..."
        dredge_pattern = r"if you would draw a card, instead you may put exactly (\d+) cards from the top of your library into your graveyard"
        for match in re.finditer(dredge_pattern, text, re.IGNORECASE):
             clauses.append({
                 'clause': match.group(0), 'event_type': 'DRAW', 'subject': 'you',
                 'action': 'draw a card', 'replacement': 'dredge', 'condition': None, # Dredge is a special replacement handled differently
                 'dredge_value': int(match.group(1))
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
        """Register standard 'if...would...instead' effects (No dash change needed)."""
        # The core logic relies on finding these keywords, not specific separators.
        # The _extract_replacement_clauses helper is responsible for parsing the structure.
        source_card = self.game_state._safe_get_card(card_id)
        source_name = source_card.name if source_card and hasattr(source_card, 'name') else f"Card {card_id}"
        registered_effects = []

        normalized_text = re.sub(r'\([^)]*\)', '', oracle_text.lower()).strip()
        clauses = self._extract_replacement_clauses(normalized_text) # Call the updated helper

        for clause_data in clauses:
            event_type = clause_data['event_type']
            if event_type == 'UNKNOWN': continue

            condition_func = self._create_enhanced_condition_function(event_type, clause_data['subject'], clause_data['condition'], player)
            replacement_func = self._create_enhanced_replacement_function(event_type, clause_data['replacement'], player, source_name)

            duration = self._determine_duration(normalized_text)

            effect_id = self.register_effect({
                'source_id': card_id, 'event_type': event_type, 'condition': condition_func,
                'replacement': replacement_func, 'duration': duration, 'controller_id': player,
                'description': f"{source_name}: {clause_data['clause']}"
            })
            registered_effects.append(effect_id)
            # Logging inside register_effect is sufficient

        return registered_effects

    def _find_clause_end(self, text):
        """Find the end of a clause in the given text."""
        end_markers = ['.', ';']
        positions = [text.find(marker) for marker in end_markers if text.find(marker) != -1]
        return min(positions) if positions else -1

    def _create_enhanced_replacement_function(self, event_type, replacement_text, controller, source_name):
        """
        Create a sophisticated replacement function based on event type and replacement text.

        Args:
            event_type: The type of event being replaced (e.g., 'DRAW', 'DAMAGE').
            replacement_text: Text describing what happens instead (e.g., "draw two cards", "prevent that damage").
            controller: The player who controls the replacement effect source.
            source_name: Name of the card/effect source for logging.

        Returns:
            function: A function that takes the event context dictionary (ctx) as input
                      and returns the modified context dictionary.
        """
        gs = self.game_state
        replacement_text = replacement_text.lower() if replacement_text else ""
        # Helper function to safely parse numbers
        def parse_num(text, default=1):
            match = re.search(r'\d+', text)
            if match: return int(match.group(0))
            mapping = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
            for word, num in mapping.items():
                 if word in text: return num
            return default

        # --- DRAW Replacements ---
        if event_type == 'DRAW':
            if 'draw twice that many' in replacement_text or 'draw two cards instead' in replacement_text: # Handle specific "two"
                is_specific_two = 'draw two cards instead' in replacement_text
                def double_draw_replacement(ctx):
                    draw_count = ctx.get('draw_count', 1)
                    new_count = 2 if is_specific_two else (draw_count * 2)
                    logging.debug(f"{source_name}: Replacing draw of {draw_count} with draw {new_count} cards.")
                    ctx['draw_count'] = new_count
                    return ctx
                return double_draw_replacement

            elif 'draw that many plus' in replacement_text or 'draw an additional card' in replacement_text:
                match = re.search(r'(?:plus|additional)\s+(\w+|\d+)', replacement_text)
                bonus = 1
                if match: bonus = self._word_to_number(match.group(1))

                def bonus_draw_replacement(ctx):
                    draw_count = ctx.get('draw_count', 1)
                    new_count = draw_count + bonus
                    logging.debug(f"{source_name}: Adding {bonus} to draw, from {draw_count} to {new_count} cards.")
                    ctx['draw_count'] = new_count
                    return ctx
                return bonus_draw_replacement

            elif "skip that draw" in replacement_text or "doesn't draw" in replacement_text or "draw no cards" in replacement_text:
                def prevent_draw_replacement(ctx):
                    draw_count = ctx.get('draw_count', 1)
                    logging.debug(f"{source_name}: Preventing draw of {draw_count} cards.")
                    ctx['draw_count'] = 0
                    ctx['prevented'] = True
                    return ctx
                return prevent_draw_replacement

            # Example: "...instead reveal it and put it into your hand." (Handled by Draw already, but could be modified)
            # Example: "...instead mill N cards."
            elif "mill" in replacement_text:
                 mill_count = parse_num(replacement_text, 1)
                 def mill_instead_of_draw(ctx):
                     logging.debug(f"{source_name}: Replacing draw with mill {mill_count}.")
                     ctx['prevented'] = True # Prevent original draw
                     ctx['side_effect'] = ('mill', mill_count, ctx.get('player')) # Need to handle side effect application
                     return ctx
                 return mill_instead_of_draw

            # Add other draw replacements like Scry, Look at top N, etc.

        # --- DAMAGE Replacements ---
        elif event_type == 'DAMAGE':
            if 'prevent all' in replacement_text or 'prevent that damage' in replacement_text:
                def prevent_damage_replacement(ctx):
                    damage = ctx.get('damage_amount', 0)
                    logging.debug(f"{source_name}: Preventing {damage} damage.")
                    ctx['damage_amount'] = 0
                    ctx['prevented'] = True
                    return ctx
                return prevent_damage_replacement

            elif 'prevent the next' in replacement_text:
                amount_to_prevent = parse_num(replacement_text, 1)
                # Note: This creates a stateful replacement, usually requires removing the effect after one use.
                def prevent_next_damage_replacement(ctx):
                     original_damage = ctx.get('damage_amount', 0)
                     prevented = min(original_damage, amount_to_prevent)
                     new_damage = original_damage - prevented
                     logging.debug(f"{source_name}: Preventing next {prevented} damage. Remaining: {new_damage}")
                     ctx['damage_amount'] = new_damage
                     # Need mechanism to track/remove this shield - often done by removing the registered effect
                     ctx['used_prevention_shield'] = True # Flag for caller to handle removal
                     return ctx
                return prevent_next_damage_replacement

            elif 'double' in replacement_text or 'twice that much' in replacement_text:
                def double_damage_replacement(ctx):
                    damage = ctx.get('damage_amount', 0)
                    new_damage = damage * 2
                    logging.debug(f"{source_name}: Doubling damage from {damage} to {new_damage}.")
                    ctx['damage_amount'] = new_damage
                    return ctx
                return double_damage_replacement

            elif re.search(r'deals? that much (damage|\w+) plus (\d+)', replacement_text):
                match = re.search(r'plus (\d+)', replacement_text)
                bonus = int(match.group(1))
                def bonus_damage_replacement(ctx):
                     damage = ctx.get('damage_amount', 0)
                     new_damage = damage + bonus
                     logging.debug(f"{source_name}: Adding {bonus} to damage, from {damage} to {new_damage}.")
                     ctx['damage_amount'] = new_damage
                     return ctx
                return bonus_damage_replacement

            # Add redirection logic here if needed (complex due to target selection)

        # --- ENTERS_BATTLEFIELD Replacements ---
        elif event_type == 'ENTERS_BATTLEFIELD':
            if 'enters the battlefield tapped' in replacement_text:
                def etb_tapped_replacement(ctx):
                    logging.debug(f"{source_name}: Marking {ctx.get('card_id')} to enter tapped.")
                    ctx['enters_tapped'] = True
                    return ctx
                return etb_tapped_replacement

            elif 'enters the battlefield with' in replacement_text and 'counter' in replacement_text:
                counter_match = re.search(r'with (a|an|one|two|three|four|five|\d+)\s+([\+\-\d/]+\s+)?([\w\-]+)\s+counters?', replacement_text)
                if counter_match:
                    count_word, _, counter_type = counter_match.groups()
                    count = self._word_to_number(count_word)
                    counter_type = counter_type.strip().replace('/','_').upper() # Normalize

                    def etb_with_counters_replacement(ctx):
                        logging.debug(f"{source_name}: Marking {ctx.get('card_id')} to enter with {count} {counter_type} counters.")
                        # Ensure the structure matches how ETB counters are handled elsewhere
                        if 'enter_counters' not in ctx: ctx['enter_counters'] = []
                        ctx['enter_counters'].append({'type': counter_type, 'count': count})
                        return ctx
                    return etb_with_counters_replacement
            # Add 'As ~ enters choose...' effects if parsed here

        # --- DIES Replacements ---
        elif event_type == 'DIES':
            if 'exile it instead' in replacement_text:
                def exile_instead_replacement(ctx):
                    logging.debug(f"{source_name}: Replacing death with exile for {ctx.get('card_id')}.")
                    ctx['to_zone'] = 'exile'
                    return ctx
                return exile_instead_replacement

            elif "return it to its owner's hand" in replacement_text:
                def return_to_hand_replacement(ctx):
                    logging.debug(f"{source_name}: Replacing death with return to hand for {ctx.get('card_id')}.")
                    ctx['to_zone'] = 'hand'
                    return ctx
                return return_to_hand_replacement

            elif 'shuffle it into its owner\'s library' in replacement_text:
                def shuffle_into_library_replacement(ctx):
                     logging.debug(f"{source_name}: Replacing death with shuffle into library for {ctx.get('card_id')}.")
                     ctx['to_zone'] = 'library'
                     ctx['shuffle_required'] = True # Flag for move_card logic
                     return ctx
                return shuffle_into_library_replacement

            # Add Persist/Undying style returns here (may need counter check condition)

        # --- LIFE_GAIN Replacements ---
        elif event_type == 'LIFE_GAIN':
            if 'gain twice that much' in replacement_text:
                def double_life_gain_replacement(ctx):
                    amount = ctx.get('life_amount', 0)
                    new_amount = amount * 2
                    logging.debug(f"{source_name}: Doubling life gain from {amount} to {new_amount}.")
                    ctx['life_amount'] = new_amount
                    return ctx
                return double_life_gain_replacement

            elif 'gain that much plus' in replacement_text:
                match = re.search(r'plus (\d+)', replacement_text)
                bonus = int(match.group(1)) if match else 1
                def plus_life_gain_replacement(ctx):
                    amount = ctx.get('life_amount', 0)
                    new_amount = amount + bonus
                    logging.debug(f"{source_name}: Increasing life gain by {bonus}, from {amount} to {new_amount}.")
                    ctx['life_amount'] = new_amount
                    return ctx
                return plus_life_gain_replacement

            elif "gain no life" in replacement_text or "doesn't gain life" in replacement_text:
                def prevent_life_gain_replacement(ctx):
                    amount = ctx.get('life_amount', 0)
                    logging.debug(f"{source_name}: Preventing life gain of {amount}.")
                    ctx['life_amount'] = 0
                    ctx['prevented'] = True
                    return ctx
                return prevent_life_gain_replacement

            # Add opponent lose life effects

        # --- LIFE_LOSS Replacements ---
        elif event_type == 'LIFE_LOSS':
             # Add prevent, double, plus logic similar to LIFE_GAIN
             if "lose no life" in replacement_text:
                  def prevent_life_loss(ctx):
                       amount = ctx.get('life_amount', 0)
                       logging.debug(f"{source_name}: Preventing life loss of {amount}.")
                       ctx['life_amount'] = 0
                       ctx['prevented'] = True
                       return ctx
                  return prevent_life_loss

        # --- ADD_COUNTER Replacements ---
        elif event_type == 'ADD_COUNTER':
            if "twice that many" in replacement_text or "double the number" in replacement_text:
                def double_counters_replacement(ctx):
                    amount = ctx.get('count', 1)
                    new_amount = amount * 2
                    logging.debug(f"{source_name}: Doubling {ctx.get('counter_type')} counters from {amount} to {new_amount}.")
                    ctx['count'] = new_amount
                    return ctx
                return double_counters_replacement

            elif "one additional" in replacement_text or "plus one" in replacement_text:
                 def plus_one_counter_replacement(ctx):
                     amount = ctx.get('count', 1)
                     new_amount = amount + 1
                     logging.debug(f"{source_name}: Adding one additional {ctx.get('counter_type')} counter (total {new_amount}).")
                     ctx['count'] = new_amount
                     return ctx
                 return plus_one_counter_replacement

            elif "prevent putting counters" in replacement_text or "can't get counters" in replacement_text:
                 def prevent_counters_replacement(ctx):
                     logging.debug(f"{source_name}: Preventing {ctx.get('count')} {ctx.get('counter_type')} counters.")
                     ctx['count'] = 0
                     ctx['prevented'] = True
                     return ctx
                 return prevent_counters_replacement

        # --- CREATE_TOKEN Replacements ---
        elif event_type == 'CREATE_TOKEN':
             if "twice that many" in replacement_text or "double the number" in replacement_text:
                 def double_tokens_replacement(ctx):
                     amount = ctx.get('token_count', 1)
                     new_amount = amount * 2
                     logging.debug(f"{source_name}: Doubling token creation from {amount} to {new_amount}.")
                     ctx['token_count'] = new_amount
                     return ctx
                 return double_tokens_replacement
             # Add create "that many plus N"

             elif "create no tokens" in replacement_text or "doesn't create tokens" in replacement_text:
                  def prevent_tokens_replacement(ctx):
                     logging.debug(f"{source_name}: Preventing token creation.")
                     ctx['token_count'] = 0
                     ctx['prevented'] = True
                     return ctx
                  return prevent_tokens_replacement
             # Add modify token type replacements

        # --- DISCARD Replacements ---
        elif event_type == 'DISCARD':
             if "exile it instead" in replacement_text:
                 def discard_to_exile(ctx):
                     logging.debug(f"{source_name}: Replacing discard with exile for {ctx.get('card_id')}")
                     ctx['to_zone'] = 'exile'
                     return ctx
                 return discard_to_exile

        # --- MILL Replacements ---
        elif event_type == 'MILL':
             if "exile them instead" in replacement_text:
                 def mill_to_exile(ctx):
                     logging.debug(f"{source_name}: Replacing mill with exile")
                     ctx['to_zone'] = 'exile' # Signal to move_card
                     return ctx
                 return mill_to_exile


        # --- Default: Return Identity Function ---
        # This function does nothing to the context if no specific pattern was matched.
        # It should still log that no specific replacement was found.
        def identity_replacement(ctx):
            # Only log if the text wasn't empty or obviously simple like "prevented"
            if replacement_text and replacement_text not in ["prevented", "exiled"]:
                logging.debug(f"No specific replacement logic found for {event_type}: '{replacement_text}'. Event proceeds as modified or unmodified.")
            return ctx
        return identity_replacement

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
        
    def cleanup_expired_effects(self):
        """Remove effects that have expired."""
        current_turn = self.game_state.turn
        # Need to know active player to handle 'until_my_next_turn'
        active_player = self.game_state._get_active_player() # Get current active player
        active_player_is_p1 = (active_player == self.game_state.p1)
        # Get current phase from game state
        current_phase = self.game_state.phase

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
