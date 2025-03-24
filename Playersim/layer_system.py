import logging
from collections import defaultdict

class LayerSystem:
    """
    Implements the 7-layer system for applying continuous effects in MTG.
    Layer order:
    1. Copy effects
    2. Control-changing effects
    3. Text-changing effects
    4. Type-changing effects
    5. Color-changing effects
    6. Ability adding/removing effects
    7. Power/toughness changing effects (with sublayers)
    """

    def __init__(self, game_state):
        self.game_state = game_state
        # Initialize layers 1-7 with lists to store effects
        self.layers = {
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
        self.timestamps = {}
        self.effect_counter = 0
        self.dependencies = defaultdict(list)
        
        # Add cache for effect application
        self.effects_cache = {}
        self.cache_valid = False
        self.last_game_state_hash = None
        
    def invalidate_cache(self):
        """Invalidate the effects cache when game state changes."""
        self.cache_valid = False
        
    def apply_all_effects(self):
        """Apply all continuous effects in the correct layer order with caching."""
        # Generate a simple hash of relevant game state to check cache validity
        current_state_hash = self._generate_game_state_hash()
        
        # If cache is valid and state hasn't changed, skip recomputation
        if self.cache_valid and current_state_hash == self.last_game_state_hash:
            logging.debug("Using cached layer effects (game state unchanged)")
            return
        
        # Clear the cache and update hash
        self.effects_cache = {}
        self.last_game_state_hash = current_state_hash
        
        # Apply effects layer by layer (existing implementation)
        for layer in range(1, 7):
            self._apply_layer_effects(layer)
            
        # Apply layer 7 sublayers in order
        for sublayer in ['a', 'b', 'c', 'd']:
            self._apply_layer7_effects(sublayer)
            
        # Mark cache as valid
        self.cache_valid = True

    def _generate_game_state_hash(self):
        """Generate a simple hash of the relevant game state for caching."""
        gs = self.game_state
        hash_components = []
        
        # Include battlefield cards and their states
        for player in [gs.p1, gs.p2]:
            for card_id in player.get("battlefield", []):
                card = gs._safe_get_card(card_id)
                if not card:
                    continue
                    
                # Include basic card properties in hash
                card_hash = f"{card_id}:"
                
                # Add relevant properties to the hash
                for attr in ['power', 'toughness', 'colors', 'card_types', 'subtypes', 'counters']:
                    if hasattr(card, attr):
                        card_hash += f"{attr}={str(getattr(card, attr))};"
                        
                hash_components.append(card_hash)
        
        # Include active effects in the hash
        for layer in range(1, 7):
            for effect_id, effect_data in self.layers[layer]:
                hash_components.append(f"effect:{effect_id}")
        
        for sublayer in ['a', 'b', 'c', 'd']:
            for effect_id, effect_data in self.layers[7][sublayer]:
                hash_components.append(f"effect:{effect_id}:{sublayer}")
        
        # Sort to ensure consistent ordering and join to create the hash
        hash_components.sort()
        return hash("".join(hash_components))
        
    def register_effect(self, effect_data):
        """
        Register a continuous effect with the layer system.
        
        Args:
            effect_data: Dictionary with the following keys:
                - 'source_id': ID of the card generating the effect
                - 'layer': Which layer the effect applies in (1-7)
                - 'sublayer': For layer 7, which sublayer (a-d)
                - 'affected_ids': List of card IDs affected
                - 'effect_type': Type of effect (e.g., 'set_pt', 'add_ability')
                - 'effect_value': Value for the effect
                - 'duration': How long the effect lasts ('permanent', 'end_of_turn', etc.)
                - 'condition': Optional function that returns whether effect is active
                
        Returns:
            effect_id: A unique identifier for the registered effect
        """
        # Create a unique ID for this effect
        effect_id = f"effect_{self.effect_counter}"
        self.effect_counter += 1
        
        # Record timestamp
        self.timestamps[effect_id] = self.effect_counter
        
        # Add to appropriate layer
        layer = effect_data['layer']
        if layer == 7:
            # Layer 7 has sublayers
            sublayer = effect_data.get('sublayer', 'c')  # Default to typical +N/+N effects
            self.layers[7][sublayer].append((effect_id, effect_data))
        else:
            self.layers[layer].append((effect_id, effect_data))
            
        # Check for dependencies
        self._analyze_dependencies(effect_id, effect_data)
        
        logging.debug(f"Registered effect {effect_id} in layer {layer}")
        return effect_id
    
    def _analyze_dependencies(self, effect_id, effect_data):
        """Analyze and record dependencies between effects with enhanced handling."""
        layer = effect_data['layer']
        
        # Track affected objects for this effect
        affected_ids = set(effect_data['affected_ids'])
        
        # Examine all other registered effects for dependencies
        for other_layer in range(1, 8):
            # Skip examining effects in the same layer unless it's layer 7
            if other_layer == layer and layer != 7:
                continue
                
            # For layer 7, check sublayer dependencies
            if other_layer == 7:
                for sublayer in ['a', 'b', 'c', 'd']:
                    for other_id, other_data in self.layers[7][sublayer]:
                        if other_id == effect_id:
                            continue  # Skip self
                        
                        # Check for shared affected objects
                        other_affected = set(other_data.get('affected_ids', []))
                        if affected_ids.intersection(other_affected):
                            # Determine dependency direction
                            if self._is_dependent_on(layer, other_layer, sublayer, effect_data, other_data):
                                self.dependencies[effect_id].append(other_id)
                            elif self._is_dependent_on(other_layer, layer, None, other_data, effect_data):
                                self.dependencies[other_id].append(effect_id)
            else:
                # Handle regular layer effects
                effects_in_layer = self.layers.get(other_layer, [])
                for other_id, other_data in effects_in_layer:
                    if other_id == effect_id:
                        continue  # Skip self
                    
                    # Check for shared affected objects
                    other_affected = set(other_data.get('affected_ids', []))
                    if affected_ids.intersection(other_affected):
                        # Determine dependency direction
                        if self._is_dependent_on(layer, other_layer, None, effect_data, other_data):
                            self.dependencies[effect_id].append(other_id)
                        elif self._is_dependent_on(other_layer, layer, None, other_data, effect_data):
                            self.dependencies[other_id].append(effect_id)

    def _is_dependent_on(self, layer1, layer2, sublayer, effect1, effect2):
        """Determine if effect1 depends on effect2 based on layer rules."""
        # Layer dependencies follow MTG's comprehensive rules
        
        # Layer 7 special cases (sublayers)
        if layer1 == 7 and layer2 == 7:
            sublayer1 = effect1.get('sublayer', 'c')
            sublayer2 = sublayer or effect2.get('sublayer', 'c')
            
            # Sublayer ordering: a -> b -> c -> d
            sublayer_order = {'a': 0, 'b': 1, 'c': 2, 'd': 3}
            return sublayer_order.get(sublayer1, 2) > sublayer_order.get(sublayer2, 2)
        
        # Special cases for certain effect types
        effect1_type = effect1.get('effect_type', '')
        effect2_type = effect2.get('effect_type', '')
        
        # Type-changing effects (layer 4) can affect power/toughness (layer 7)
        if layer1 == 7 and layer2 == 4 and effect2_type in ['add_type', 'remove_type']:
            return True
        
        # Copy effects (layer 1) are applied before all others
        if layer2 == 1 and layer1 > 1:
            return True
        
        # Control effects (layer 2) can affect any other effect
        if layer2 == 2 and layer1 > 2:
            return True
        
        # Text effects (layer 3) can affect type effects (layer 4)
        if layer1 == 4 and layer2 == 3 and effect2_type == 'change_text':
            return True
        
        # Type effects (layer 4) can affect abilities (layer 6)
        if layer1 == 6 and layer2 == 4 and effect1_type in ['add_ability', 'remove_ability']:
            return True
        
        # Normal layer ordering
        return layer1 > layer2
    
    def remove_effect(self, effect_id):
        """Remove an effect from the layer system."""
        for layer in range(1, 7):
            self.layers[layer] = [(eid, data) for eid, data in self.layers[layer] if eid != effect_id]
            
        # Also check layer 7 sublayers
        for sublayer in ['a', 'b', 'c', 'd']:
            self.layers[7][sublayer] = [(eid, data) for eid, data in self.layers[7][sublayer] if eid != effect_id]
        
        # Remove from timestamps and dependencies
        if effect_id in self.timestamps:
            del self.timestamps[effect_id]
        self.dependencies.pop(effect_id, None)
        
        # Remove from dependency lists
        for dep_id in self.dependencies:
            if effect_id in self.dependencies[dep_id]:
                self.dependencies[dep_id].remove(effect_id)
    
    def apply_all_effects(self):
        """Apply all continuous effects in the correct layer order."""
        # Apply effects layer by layer
        for layer in range(1, 7):
            self._apply_layer_effects(layer)
            
        # Apply layer 7 sublayers in order
        for sublayer in ['a', 'b', 'c', 'd']:
            self._apply_layer7_effects(sublayer)
    
    def _apply_layer_effects(self, layer):
        """Apply effects from a specific layer."""
        # Sort effects by timestamp
        sorted_effects = sorted(self.layers[layer], key=lambda x: self.timestamps[x[0]])
        
        # Handle dependencies
        sorted_effects = self._sort_with_dependencies(sorted_effects)
        
        # Apply each effect
        for effect_id, effect_data in sorted_effects:
            self._apply_single_effect(effect_data)
    
    def _apply_layer7_effects(self, sublayer):
        """Apply effects from a specific sublayer of layer 7."""
        # Sort effects by timestamp
        sorted_effects = sorted(self.layers[7][sublayer], key=lambda x: self.timestamps[x[0]])
        
        # Handle dependencies
        sorted_effects = self._sort_with_dependencies(sorted_effects)
        
        # Apply each effect
        for effect_id, effect_data in sorted_effects:
            self._apply_single_effect(effect_data)
    
    def _sort_with_dependencies(self, effects):
        """Sort effects considering dependencies."""
        # Simple topological sort for dependencies
        result = []
        visited = set()
        temp_mark = set()
        
        def visit(effect):
            effect_id = effect[0]
            if effect_id in temp_mark:
                # Circular dependency, just use timestamp order
                return
            if effect_id in visited:
                return
                
            temp_mark.add(effect_id)
            
            # Visit dependencies first
            for dep_id in self.dependencies.get(effect_id, []):
                for dep_effect in effects:
                    if dep_effect[0] == dep_id:
                        visit(dep_effect)
            
            temp_mark.remove(effect_id)
            visited.add(effect_id)
            result.append(effect)
        
        # Visit all effects
        for effect in effects:
            if effect[0] not in visited:
                visit(effect)
                
        return list(reversed(result))  # Reverse to get correct order
    
    def remove_effects_by_source(self, source_id):
        """Remove all effects originating from a specific source card."""
        # Remove from regular layers
        for layer in range(1, 7):
            self.layers[layer] = [(eid, data) for eid, data in self.layers[layer] 
                                if data.get('source_id') != source_id]
            
        # Remove from layer 7 sublayers
        for sublayer in ['a', 'b', 'c', 'd']:
            self.layers[7][sublayer] = [(eid, data) for eid, data in self.layers[7][sublayer]
                                    if data.get('source_id') != source_id]
        
        # Remove from timestamps and dependencies
        effect_ids_to_remove = [eid for eid in self.timestamps 
                            if any(eid == e_id for e_id, data in 
                                [(e, d) for layer in range(1, 7) for e, d in self.layers[layer]] +
                                [(e, d) for sublayer in ['a', 'b', 'c', 'd'] for e, d in self.layers[7][sublayer]]
                                if data.get('source_id') == source_id)]
        
        for eid in effect_ids_to_remove:
            if eid in self.timestamps:
                del self.timestamps[eid]
            self.dependencies.pop(eid, None)
            
            # Remove from dependency lists
            for dep_id in self.dependencies:
                if eid in self.dependencies[dep_id]:
                    self.dependencies[dep_id].remove(eid)
    
    def _apply_single_effect(self, effect_data):
        """Apply a single effect to affected cards with a more extensible handler system."""
        # Check if effect is active via condition
        if 'condition' in effect_data and callable(effect_data['condition']):
            if not effect_data['condition']():
                return  # Skip if condition not met
        
        # Get affected cards
        affected_ids = effect_data['affected_ids']
        effect_type = effect_data['effect_type']
        effect_value = effect_data['effect_value']
        
        # Define handlers for different effect types
        effect_handlers = {
            'set_pt': self._handle_set_pt_effect,
            'modify_pt': self._handle_modify_pt_effect,
            'switch_pt': self._handle_switch_pt_effect,
            'set_color': self._handle_set_color_effect,
            'add_color': self._handle_add_color_effect,
            'remove_color': self._handle_remove_color_effect,
            'add_type': self._handle_add_type_effect,
            'remove_type': self._handle_remove_type_effect,
            'add_subtype': self._handle_add_subtype_effect,
            'remove_subtype': self._handle_remove_subtype_effect,
            'add_ability': self._handle_add_ability_effect,
            'remove_ability': self._handle_remove_ability_effect,
            'copy': self._handle_copy_effect,
            'change_text': self._handle_change_text_effect,
            'change_control': self._handle_change_control_effect,
            'cant_attack': self._handle_cant_attack_effect,
            'cant_block': self._handle_cant_block_effect,
            'assign_damage_as_though_not_blocked': self._handle_assign_damage_effect,
            'add_protection': self._handle_add_protection_effect,
            'must_attack': self._handle_must_attack_effect,
            'enchanted_must_attack': self._handle_enchanted_must_attack_effect
        }
        
        # Get the appropriate handler for this effect type
        handler = effect_handlers.get(effect_type)
        
        if handler:
            # Process each affected card with the handler
            for card_id in affected_ids:
                card_location = self._find_card_location(card_id)
                if not card_location:
                    continue
                    
                owner, zone = card_location
                
                # Only apply to cards in appropriate zones (mainly battlefield)
                if zone != "battlefield" and effect_data.get('only_battlefield', True):
                    continue
                    
                card = self.game_state._safe_get_card(card_id)
                if not card:
                    continue
                    
                # Apply the effect using the handler
                handler(card, effect_value, owner, zone, effect_data)
        else:
            logging.warning(f"No handler found for effect type: {effect_type}")
            
    def _handle_set_pt_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 7a: Set power/toughness effect with improved error handling."""
        if hasattr(card, 'power') and hasattr(card, 'toughness'):
            try:
                power, toughness = effect_value
                card.power, card.toughness = power, toughness
                card_name = card.name if hasattr(card, 'name') else f"Card {getattr(card, 'card_id', 'unknown')}"
                logging.debug(f"Set {card_name}'s power/toughness to {power}/{toughness}")
                return True
            except (ValueError, TypeError) as e:
                logging.error(f"Error setting P/T: {e}")
                return False
        return False

    def _handle_modify_pt_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 7c: Modify power/toughness effect with proper value checking."""
        if hasattr(card, 'power') and hasattr(card, 'toughness'):
            try:
                power_mod, toughness_mod = effect_value
                if not isinstance(power_mod, int) or not isinstance(toughness_mod, int):
                    power_mod = int(power_mod) if power_mod is not None else 0
                    toughness_mod = int(toughness_mod) if toughness_mod is not None else 0
                    
                card.power += power_mod
                card.toughness += toughness_mod
                card_name = card.name if hasattr(card, 'name') else f"Card {getattr(card, 'card_id', 'unknown')}"
                logging.debug(f"Modified {card_name}'s power/toughness by +{power_mod}/+{toughness_mod}")
                return True
            except (ValueError, TypeError) as e:
                logging.error(f"Error modifying P/T: {e}")
                return False
        return False

    def _handle_switch_pt_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 7d: Switch power/toughness effect with validation."""
        if hasattr(card, 'power') and hasattr(card, 'toughness'):
            try:
                # Store original values
                original_power = card.power
                original_toughness = card.toughness
                
                # Swap values
                card.power = original_toughness
                card.toughness = original_power
                
                card_name = card.name if hasattr(card, 'name') else f"Card {getattr(card, 'card_id', 'unknown')}"
                logging.debug(f"Switched {card_name}'s power/toughness to {card.power}/{card.toughness}")
                return True
            except Exception as e:
                logging.error(f"Error switching P/T: {e}")
                return False
        return False

    def _handle_add_type_effect(self, card, effect_value, owner, zone, effect_data):
        """Handle Layer 4: Add type effect with proper type validation."""
        valid_types = ["creature", "artifact", "enchantment", "land", "planeswalker", "instant", "sorcery"]
        
        if not hasattr(card, 'card_types'):
            card.card_types = []
        
        try:
            new_type = str(effect_value).lower()
            
            # Validate the type
            if new_type not in valid_types:
                logging.warning(f"Invalid card type: {new_type}")
                return False
                
            if new_type not in card.card_types:
                card.card_types.append(new_type)
                
                # If becoming a creature, add power and toughness if needed
                if new_type == 'creature' and (not hasattr(card, 'power') or not hasattr(card, 'toughness')):
                    # Default p/t values
                    base_pt = effect_data.get('base_pt', (1, 1))
                    card.power = base_pt[0]
                    card.toughness = base_pt[1]
                    
                card_name = card.name if hasattr(card, 'name') else f"Card {getattr(card, 'card_id', 'unknown')}"
                logging.debug(f"Added type '{new_type}' to {card_name}")
                return True
        except Exception as e:
            logging.error(f"Error adding type: {e}")
        
        return False
    
    def _find_card_location(self, card_id):
        """Find which player controls a card and in which zone it is."""
        return self.game_state.find_card_location(card_id)
    
    def cleanup_expired_effects(self):
        """Remove effects that have expired."""
        current_turn = self.game_state.turn
        current_phase = self.game_state.phase
        
        effects_to_remove = []
        
        # Check all layers
        for layer in range(1, 7):
            for effect_id, effect_data in self.layers[layer]:
                if self._is_effect_expired(effect_data, current_turn, current_phase):
                    effects_to_remove.append(effect_id)
        
        # Check layer 7 sublayers
        for sublayer in ['a', 'b', 'c', 'd']:
            for effect_id, effect_data in self.layers[7][sublayer]:
                if self._is_effect_expired(effect_data, current_turn, current_phase):
                    effects_to_remove.append(effect_id)
        
        # Remove expired effects
        for effect_id in effects_to_remove:
            # Check if this is a control-changing effect in layer 2
            for _, effect_data in self.layers[2]:
                if effect_data.get('effect_id') == effect_id and effect_data.get('effect_type') == 'change_control':
                    # Ensure temporary control is explicitly reverted
                    for card_id in effect_data.get('affected_ids', []):
                        if card_id in self.game_state.temp_control_effects:
                            # Call the revert function directly for this card
                            original_controller = self.game_state.temp_control_effects[card_id]
                            current_controller = self.game_state._find_card_owner(card_id)
                            if current_controller and current_controller != original_controller:
                                # Manually revert just this card
                                if card_id in current_controller["battlefield"]:
                                    current_controller["battlefield"].remove(card_id)
                                original_controller["battlefield"].append(card_id)
                                del self.game_state.temp_control_effects[card_id]
            
            self.remove_effect(effect_id)
            logging.debug(f"Removed expired effect {effect_id}")
        
        # After handling layer effects, let game state handle temporary control effects
        if current_phase == self.game_state.PHASE_END_STEP:
            self.game_state._revert_temporary_control()
    
    def _is_effect_expired(self, effect_data, current_turn, current_phase):
        """Check if an effect has expired based on its duration."""
        duration = effect_data.get('duration', 'permanent')
        
        if duration == 'permanent':
            return False
            
        elif duration == 'end_of_turn':
            return effect_data.get('start_turn', 0) < current_turn
            
        elif duration == 'end_of_combat':
            return (effect_data.get('start_turn', 0) < current_turn or 
                    (effect_data.get('start_turn', 0) == current_turn and 
                     current_phase > self.game_state.PHASE_END_COMBAT))
                     
        elif duration == 'next_turn':
            return effect_data.get('start_turn', 0) < current_turn - 1
            
        return False  # Unknown duration type