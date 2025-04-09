import logging
from collections import defaultdict
from .card import Card
import re
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
            1: [], 2: [], 3: [], 4: [], 5: [], 6: [],
            7: {'a': [], 'b': [], 'c': [], 'd': []} # Layer 7 sublayers
        }
        self.timestamps = {}
        self.effect_counter = 0
        self.dependencies = defaultdict(list)
        self._last_applied_state_hash = None # For optimization if state hasn't changed significantly
        

    def apply_all_effects(self):
        """
        Apply all continuous effects in the correct layer order, non-destructively during calculation.
        Updates the actual card objects in GameState only after all layers are processed.
        """
        gs = self.game_state
        affected_card_ids = self._get_affected_card_ids()
        if not affected_card_ids:
            # logging.debug("LayerSystem: No affected cards, skipping application.")
            return # No effects to apply

        # Optimization: Check if significant game state affecting layers has changed
        # This hash needs to be carefully constructed to include relevant state.
        # Simple version: Hash based on card IDs on battlefield and effect registry count.
        current_state_tuple = (
            tuple(sorted(gs.p1.get("battlefield", []))),
            tuple(sorted(gs.p2.get("battlefield", []))),
            self.effect_counter # Track if effects were added/removed
        )
        current_state_hash = hash(current_state_tuple)

        if current_state_hash == self._last_applied_state_hash:
             # logging.debug("LayerSystem: Skipping recalculation, relevant state unchanged.")
             return # State hasn't changed enough to warrant recalculation

        logging.debug(f"LayerSystem: Recalculating effects for {len(affected_card_ids)} cards.")

        # Store calculated characteristics temporarily
        calculated_characteristics = {} # { card_id: { characteristic_name: value } }

        # 1. Initialize: Get base characteristics from card_db for affected cards
        for card_id in affected_card_ids:
            # CRITICAL: Fetch the *original* definition from card_db
            original_card = gs.card_db.get(card_id)
            # Get the current *live* card object from GameState to access current counters etc.
            # Important for Layer 7b. Also used to set the final state.
            live_card = gs._safe_get_card(card_id)

            if not original_card or not live_card:
                logging.warning(f"LayerSystem: Could not find original or live card for ID {card_id}. Skipping.")
                continue

            # Initialize with base characteristics from ORIGINAL card definition
            import copy
            base_chars = {
                'name': getattr(original_card, 'name', 'Unknown'),
                'mana_cost': getattr(original_card, 'mana_cost', ''),
                'colors': copy.deepcopy(getattr(original_card, 'colors', [0]*5)),
                'card_types': copy.deepcopy(getattr(original_card, 'card_types', [])),
                'subtypes': copy.deepcopy(getattr(original_card, 'subtypes', [])),
                'supertypes': copy.deepcopy(getattr(original_card, 'supertypes', [])),
                'oracle_text': getattr(original_card, 'oracle_text', ''),
                'keywords': copy.deepcopy(getattr(original_card, 'keywords', [])), # Base keywords
                'power': getattr(original_card, 'power', 0), # Base P/T
                'toughness': getattr(original_card, 'toughness', 0),
                'loyalty': getattr(original_card, 'loyalty', 0),
                'cmc': getattr(original_card, 'cmc', 0),
                'type_line': getattr(original_card, 'type_line', ''),
                '_base_power': getattr(original_card, 'power', 0), # Store true base P/T
                '_base_toughness': getattr(original_card, 'toughness', 0),
                '_granted_abilities': set(), # Track added abilities
                '_removed_abilities': set(), # Track removed abilities
                '_controller': gs.get_card_controller(card_id), # Store current controller for context
                '_live_card_ref': live_card # Keep reference to live card for Layer 7b counters
            }
            calculated_characteristics[card_id] = base_chars

        # 2. Apply Layers Sequentially (Logic for applying layers remains the same)
        # --- Layer 1: Copy Effects ---
        sorted_layer1 = self._sort_layer_effects(1, self.layers[1])
        for _, effect_data in sorted_layer1:
             self._calculate_layer1_copy(effect_data, calculated_characteristics)

        # --- Layer 2: Control-Changing Effects ---
        sorted_layer2 = self._sort_layer_effects(2, self.layers[2])
        for _, effect_data in sorted_layer2:
             self._calculate_layer2_control(effect_data, calculated_characteristics)

        # --- Layer 3: Text-Changing Effects ---
        sorted_layer3 = self._sort_layer_effects(3, self.layers[3])
        for _, effect_data in sorted_layer3:
             self._calculate_layer3_text(effect_data, calculated_characteristics)

        # --- Layer 4: Type-Changing Effects ---
        sorted_layer4 = self._sort_layer_effects(4, self.layers[4])
        for _, effect_data in sorted_layer4:
             self._calculate_layer4_type(effect_data, calculated_characteristics)

        # --- Layer 5: Color-Changing Effects ---
        # Base colors are set initially. This layer modifies them.
        sorted_layer5 = self._sort_layer_effects(5, self.layers[5])
        for _, effect_data in sorted_layer5:
             self._calculate_layer5_color(effect_data, calculated_characteristics)

        # --- Layer 6: Ability Adding/Removing Effects ---
        # Reset inherent abilities based on potentially modified text before applying layer 6
        for card_id in calculated_characteristics:
            char_dict = calculated_characteristics[card_id]
            inherent_abilities = self._approximate_keywords_set(char_dict['oracle_text']) # Get abilities from current text
            char_dict['_inherent_abilities'] = inherent_abilities # Store for final calculation

        sorted_layer6 = self._sort_layer_effects(6, self.layers[6])
        for _, effect_data in sorted_layer6:
            self._calculate_layer6_abilities(effect_data, calculated_characteristics) # Updates _granted/_removed

        # Update final keywords list after all layer 6 effects for *each card*
        for card_id in calculated_characteristics:
             self._update_final_keywords(calculated_characteristics[card_id])

        # --- Layer 7: Power/Toughness Changing Effects ---
        # Initialize P/T based on base values (already set, possibly modified by layer 1)
        # Layer 7a: Set P/T to specific values (Overrides base)
        sorted_layer7a = self._sort_layer_effects(7, self.layers[7]['a'], sublayer='a')
        for _, effect_data in sorted_layer7a:
             self._calculate_layer7a_set(effect_data, calculated_characteristics)

        # Layer 7b: Modify P/T based on counters (Reads from LIVE card state)
        for card_id in calculated_characteristics:
             self._calculate_layer7b_counters(card_id, calculated_characteristics[card_id])

        # Layer 7c: Modify P/T (other static effects like anthems)
        sorted_layer7c = self._sort_layer_effects(7, self.layers[7]['c'], sublayer='c')
        for _, effect_data in sorted_layer7c:
             self._calculate_layer7c_modify(effect_data, calculated_characteristics)

        # Layer 7d: P/T switching effects
        sorted_layer7d = self._sort_layer_effects(7, self.layers[7]['d'], sublayer='d')
        for _, effect_data in sorted_layer7d:
             self._calculate_layer7d_switch(effect_data, calculated_characteristics)

        # 3. Update GameState LIVE Card Objects
        for card_id, final_chars in calculated_characteristics.items():
            live_card = final_chars.get('_live_card_ref') # Use stored reference
            if not live_card: continue

            # Update attributes based on final calculated values
            for attr, value in final_chars.items():
                # Skip internal tracking attributes
                if attr.startswith('_'): continue
                try:
                    # Skip setting if value is None (e.g., power/toughness for non-creatures)
                    if value is None and attr in ['power', 'toughness', 'loyalty']:
                        # Ensure attribute exists but set to default if None not allowed
                        if hasattr(live_card, attr):
                            setattr(live_card, attr, 0) # Default to 0 if None
                        continue

                    # Only set attribute if it exists on the live card object
                    if hasattr(live_card, attr):
                        # Type check/conversion might be needed for safety
                        current_type = type(getattr(live_card, attr, None))
                        if value is not None and not isinstance(value, current_type):
                             try:
                                  # Attempt conversion (e.g., int from float)
                                  if current_type == int and isinstance(value, float): value = int(value)
                                  # Add other necessary conversions
                             except (TypeError, ValueError):
                                  logging.warning(f"LayerSystem: Type mismatch for '{attr}' on {card_id}. Expected {current_type}, got {type(value)}. Skipping set.")
                                  continue

                        setattr(live_card, attr, value)
                except Exception as e: # Catch broader exceptions during setattr
                    logging.error(f"LayerSystem: Error setting attribute '{attr}' on card {card_id}. Value: {value}. Error: {e}")

            # Final sanity check for P/T (optional, depends on rules)
            # if 'creature' not in getattr(live_card, 'card_types', []):
            #      live_card.power = 0
            #      live_card.toughness = 0

        # Update the state hash cache
        self._last_applied_state_hash = current_state_hash
        logging.debug(f"LayerSystem: Finished applying effects.")
        
    
    def _get_affected_card_ids(self):
        """Get all card IDs currently affected by any registered continuous effect."""
        affected_card_ids = set()
        for layer_num in range(1, 8):
            if layer_num == 7:
                for sublayer_effects in self.layers[7].values():
                    for _, effect_data in sublayer_effects:
                        # Ensure affected_ids exists and is iterable
                        ids = effect_data.get('affected_ids')
                        if ids and isinstance(ids, (list, set)):
                           affected_card_ids.update(ids)
            else:
                effects = self.layers.get(layer_num, [])
                for _, effect_data in effects:
                    ids = effect_data.get('affected_ids')
                    if ids and isinstance(ids, (list, set)):
                         affected_card_ids.update(ids)
        return affected_card_ids
    
    def _calculate_layer1_copy(self, effect_data, calculated_characteristics):
        source_to_copy_id = effect_data.get('effect_value')
        if not source_to_copy_id: return
        source_to_copy_card = self.game_state._safe_get_card(source_to_copy_id)
        if not source_to_copy_card: return

        for target_id in effect_data.get('affected_ids', []):
            if target_id in calculated_characteristics:
                 logging.debug(f"Layer 1: Applying copy of {source_to_copy_card.name} to {target_id}")
                 # Apply copyable values based on Rule 707.2
                 import copy
                 target_chars = calculated_characteristics[target_id]
                 source_attrs = source_to_copy_card.__dict__ # Simple way, assumes no slots

                 copyable_attrs = ['name', 'mana_cost', 'colors', 'card_types', 'subtypes', 'supertypes', 'oracle_text', 'power', 'toughness', 'loyalty']
                 for attr in copyable_attrs:
                     if hasattr(source_to_copy_card, attr):
                         value = getattr(source_to_copy_card, attr)
                         # Deep copy lists/dicts
                         target_chars[attr] = copy.deepcopy(value) if isinstance(value, (list, dict)) else value

                 # Reset non-copyable aspects implicit in the copy action
                 # Note: Status (tapped, counters, etc.) aren't part of copy effect itself
                 # But derived properties might change:
                 target_chars['_base_power'] = target_chars.get('power', 0)
                 target_chars['_base_toughness'] = target_chars.get('toughness', 0)
                 # Recalculate type line potentially
                 target_chars['type_line'] = self.game_state._build_type_line(target_chars) # Assume GS has helper
                 target_chars['_granted_abilities'] = set()
                 target_chars['_removed_abilities'] = set()


        
    def _sort_layer_effects(self, layer_num, effects, sublayer=None):
        """Sorts effects for a given layer/sublayer."""
        # Sort by timestamp first
        key = (layer_num, sublayer) if sublayer else layer_num
        # Using timestamp sort as a primary, slightly simplified approach here.
        sorted_by_timestamp = sorted(effects, key=lambda x: self.timestamps.get(x[0], 0))

        # Attempt dependency sort (may need further refinement)
        try:
            # Need the card_id from the effect_data for dependency check
            # Dependency check now needs to work with calculated characteristics perhaps?
            # Keeping simplified timestamp sort for now. Dependency needs more work.
            # sorted_effects = self._sort_with_dependencies(sorted_by_timestamp)
            sorted_effects = sorted_by_timestamp
        except Exception as e:
            logging.warning(f"Dependency sort failed for layer {key}: {e}. Falling back to timestamp order.")
            sorted_effects = sorted_by_timestamp

        return sorted_effects
    
    def _calculate_layer1_copy(self, effect_data, calculated_characteristics):
        source_to_copy_id = effect_data.get('effect_value')
        if not source_to_copy_id: return
        source_to_copy_card = self.game_state._safe_get_card(source_to_copy_id)
        if not source_to_copy_card: return

        for target_id in effect_data.get('affected_ids', []):
            if target_id in calculated_characteristics:
                 logging.debug(f"Layer 1: Applying copy of {source_to_copy_card.name} to {target_id}")
                 # Apply copyable values based on Rule 707.2
                 import copy
                 target_chars = calculated_characteristics[target_id]
                 source_attrs = source_to_copy_card.__dict__ # Simple way, assumes no slots

                 copyable_attrs = ['name', 'mana_cost', 'colors', 'card_types', 'subtypes', 'supertypes', 'oracle_text', 'power', 'toughness', 'loyalty']
                 for attr in copyable_attrs:
                     if hasattr(source_to_copy_card, attr):
                         value = getattr(source_to_copy_card, attr)
                         # Deep copy lists/dicts
                         target_chars[attr] = copy.deepcopy(value) if isinstance(value, (list, dict)) else value

                 # Reset non-copyable aspects implicit in the copy action
                 # Note: Status (tapped, counters, etc.) aren't part of copy effect itself
                 # But derived properties might change:
                 target_chars['_base_power'] = target_chars.get('power', 0)
                 target_chars['_base_toughness'] = target_chars.get('toughness', 0)
                 # Recalculate type line potentially
                 target_chars['type_line'] = self.game_state._build_type_line(target_chars) # Assume GS has helper
                 target_chars['_granted_abilities'] = set()
                 target_chars['_removed_abilities'] = set()


    def _calculate_layer2_control(self, effect_data, calculated_characteristics):
        new_controller = effect_data.get('effect_value') # Should be player object/dict
        for target_id in effect_data.get('affected_ids', []):
             if target_id in calculated_characteristics:
                 # Only log the intent; actual move happens elsewhere (apply_temporary_control or permanent control change logic)
                 logging.debug(f"Layer 2: Control of {target_id} intended to change to {new_controller['name'] if new_controller else 'Unknown'}")
                 calculated_characteristics[target_id]['_controller'] = new_controller


    def _calculate_layer3_text(self, effect_data, calculated_characteristics):
         new_text = effect_data.get('effect_value')
         for target_id in effect_data.get('affected_ids', []):
              if target_id in calculated_characteristics:
                  logging.debug(f"Layer 3: Text of {target_id} changes.")
                  calculated_characteristics[target_id]['oracle_text'] = new_text # Assumes full replacement for simplicity
                  # TODO: Implement lose abilities logic if needed by rule 613.1c


    def _calculate_layer4_type(self, effect_data, calculated_characteristics):
         effect_type = effect_data.get('effect_type')
         type_val = effect_data.get('effect_value')
         for target_id in effect_data.get('affected_ids', []):
             if target_id in calculated_characteristics:
                 chars = calculated_characteristics[target_id]
                 if effect_type == 'add_type' and type_val not in chars['card_types']: chars['card_types'].append(type_val)
                 elif effect_type == 'remove_type' and type_val in chars['card_types']: chars['card_types'].remove(type_val)
                 elif effect_type == 'add_subtype' and type_val not in chars['subtypes']: chars['subtypes'].append(type_val)
                 elif effect_type == 'remove_subtype' and type_val in chars['subtypes']: chars['subtypes'].remove(type_val)
                 elif effect_type == 'set_type': chars['card_types'] = [type_val] # Assumes setting removes others
                 # Update derived type line
                 chars['type_line'] = self.game_state._build_type_line(chars) # Assume GS helper
                 logging.debug(f"Layer 4: Type/Subtype of {target_id} modified by {effect_type}:{type_val}")


    def _calculate_layer5_color(self, effect_data, calculated_characteristics):
         effect_type = effect_data.get('effect_type')
         color_val = effect_data.get('effect_value')
         for target_id in effect_data.get('affected_ids', []):
             if target_id in calculated_characteristics:
                 chars = calculated_characteristics[target_id]
                 current_colors = chars['colors'] # This is the [W,U,B,R,G] array
                 if effect_type == 'set_color' and isinstance(color_val, list) and len(color_val) == 5:
                     chars['colors'] = color_val[:] # Use copy
                 elif effect_type == 'add_color' and isinstance(color_val, list) and len(color_val) == 5:
                     for i in range(5):
                         if color_val[i]: chars['colors'][i] = 1
                 elif effect_type == 'remove_color': # Assumes makes colorless
                     chars['colors'] = [0,0,0,0,0]
                 logging.debug(f"Layer 5: Color of {target_id} modified by {effect_type}")

    def _calculate_layer6_abilities(self, effect_data, calculated_characteristics):
        effect_type = effect_data.get('effect_type')
        ability_val = effect_data.get('effect_value') # Usually string name of keyword/ability
        for target_id in effect_data.get('affected_ids', []):
            if target_id in calculated_characteristics:
                 chars = calculated_characteristics[target_id]
                 # Use internal tracking sets
                 if effect_type == 'add_ability':
                      chars['_granted_abilities'].add(ability_val)
                      chars['_removed_abilities'].discard(ability_val) # Adding overrides removal
                 elif effect_type == 'remove_ability':
                      chars['_removed_abilities'].add(ability_val)
                      chars['_granted_abilities'].discard(ability_val) # Removing overrides grant
                 elif effect_type == 'remove_all_abilities':
                      chars['_removed_abilities'].update(self._get_all_inherent_abilities(target_id)) # Need helper
                      chars['_granted_abilities'].clear()
                 elif effect_type == 'cant_attack': chars['_granted_abilities'].add('cant_attack')
                 elif effect_type == 'cant_block': chars['_granted_abilities'].add('cant_block')
                 # ... other specific ability modifications
                 logging.debug(f"Layer 6: Ability '{ability_val}' {effect_type} for {target_id}")

        # Update the final 'keywords' array based on _granted/_removed AFTER processing all layer 6 effects for the card
        # This needs to happen *after* the loop finishes for each card. Best place is maybe during the final GameState update?
        # For now, let's put a placeholder update here (will update after loop completion):
        for target_id in effect_data.get('affected_ids', []):
            if target_id in calculated_characteristics:
                self._update_final_keywords(calculated_characteristics[target_id])

    def _update_final_keywords(self, char_dict):
         """ Recalculates the 'keywords' array based on inherent, granted, and removed abilities. """
         # 1. Start with inherent keywords (from potentially copied/text-changed state)
         inherent_keywords_set = self._approximate_keywords_set(char_dict.get('oracle_text', ''))

         # 2. Add granted abilities/keywords
         granted_set = char_dict.get('_granted_abilities', set())

         # 3. Remove removed abilities/keywords
         removed_set = char_dict.get('_removed_abilities', set())

         # Calculate final set of active keywords
         final_keywords_set = (inherent_keywords_set.union(granted_set)) - removed_set

         # 4. Convert back to array/list format expected
         final_keyword_list = [0] * len(Card.ALL_KEYWORDS)
         for i, kw in enumerate(Card.ALL_KEYWORDS):
              # Normalize keyword from ALL_KEYWORDS for comparison
              if kw.lower() in final_keywords_set:
                   final_keyword_list[i] = 1

         char_dict['keywords'] = final_keyword_list
         # Log the final keywords for debugging if needed
         # active_kws = [kw for i, kw in enumerate(Card.ALL_KEYWORDS) if final_keyword_list[i] == 1]
         # logging.debug(f"Final keywords for {char_dict.get('name', 'Unknown')}: {active_kws}")

    def _get_all_inherent_abilities(self, card_id):
        """ Helper to get the set of inherent abilities/keywords from a card's (potentially modified) text. """
        # This needs access to the *current* calculated oracle text for the card
        # Assuming calculated_characteristics holds this. Requires passing it in or accessing it.
        # Placeholder implementation
        # text = calculated_characteristics[card_id]['oracle_text']
        # return self._approximate_keywords_set(text)
        return set() # Placeholder

    def _approximate_keywords_set(self, oracle_text):
         """ Helper to get a set of keywords found in text. """
         found_keywords = set()
         if not oracle_text: return found_keywords
         text_lower = oracle_text.lower()
         # Improved check using word boundaries for some keywords
         for kw in Card.ALL_KEYWORDS:
              kw_lower = kw.lower()
              # Use word boundaries for single-word keywords prone to false positives
              if ' ' not in kw_lower and kw_lower in ["flash", "haste", "reach", "ward", "fear", "band"]:
                   if re.search(r'\b' + re.escape(kw_lower) + r'\b', text_lower):
                        found_keywords.add(kw_lower)
              # Use simple substring check for multi-word or less ambiguous keywords
              elif kw_lower in text_lower:
                    # Add specific checks for variations if needed
                    if kw_lower == 'protection' and 'protection from' in text_lower: found_keywords.add(kw_lower)
                    elif kw_lower == 'landwalk' and 'walk' in text_lower: found_keywords.add(kw_lower) # Simple check
                    elif kw_lower not in ['protection', 'landwalk']: # Avoid double adding
                         found_keywords.add(kw_lower)

         # Special case for "can't be blocked"
         if "can't be blocked" in text_lower:
              found_keywords.add("unblockable")

         return found_keywords

    # Layer 7 Helpers
    def _calculate_layer7a_set(self, effect_data, calculated_characteristics):
         effect_type = effect_data.get('effect_type')
         value = effect_data.get('effect_value')
         for target_id in effect_data.get('affected_ids', []):
             if target_id in calculated_characteristics:
                  chars = calculated_characteristics[target_id]
                  # Handle effects that set P/T directly (overrides base)
                  if effect_type == 'set_pt' and isinstance(value, (tuple, list)) and len(value)==2:
                       p, t = value
                       chars['power'], chars['toughness'] = p, t
                       logging.debug(f"Layer 7a: Set P/T of {target_id} to {p}/{t}")
                  # TODO: Handle CDA P/T setting (very complex)

    def _calculate_layer7b_counters(self, card_id, char_dict):
        # Layer 7b application - uses LIVE card's counters, affects calculated characteristics
        live_card = self.game_state._safe_get_card(card_id)
        if live_card and hasattr(live_card, 'counters'):
            plus_counters = live_card.counters.get('+1/+1', 0)
            minus_counters = live_card.counters.get('-1/-1', 0)
            net_change = plus_counters - minus_counters
            if net_change != 0:
                char_dict['power'] += net_change
                char_dict['toughness'] += net_change
                logging.debug(f"Layer 7b: Applied {net_change} P/T from counters to {card_id}")

    def _calculate_layer7c_modify(self, effect_data, calculated_characteristics):
        effect_type = effect_data.get('effect_type')
        value = effect_data.get('effect_value')
        for target_id in effect_data.get('affected_ids', []):
            if target_id in calculated_characteristics:
                chars = calculated_characteristics[target_id]
                if effect_type == 'modify_pt' and isinstance(value, (tuple, list)) and len(value)==2:
                    p_mod, t_mod = value
                    chars['power'] += p_mod
                    chars['toughness'] += t_mod
                    logging.debug(f"Layer 7c: Modified P/T of {target_id} by {p_mod:+}/{t_mod:+}")

    def _calculate_layer7d_switch(self, effect_data, calculated_characteristics):
         effect_type = effect_data.get('effect_type')
         for target_id in effect_data.get('affected_ids', []):
              if target_id in calculated_characteristics:
                  chars = calculated_characteristics[target_id]
                  if effect_type == 'switch_pt':
                       p, t = chars['power'], chars['toughness']
                       chars['power'], chars['toughness'] = t, p
                       logging.debug(f"Layer 7d: Switched P/T of {target_id} to {t}/{p}")
        
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
        layer = effect_data.get('layer')
        if layer is None:
             logging.error(f"Effect data missing 'layer': {effect_data}")
             return None

        if layer == 7:
            # Layer 7 has sublayers
            sublayer = effect_data.get('sublayer', 'c')  # Default to typical +N/+N effects
            if sublayer not in self.layers[7]:
                 logging.error(f"Invalid sublayer '{sublayer}' for layer 7.")
                 return None
            self.layers[7][sublayer].append((effect_id, effect_data))
        elif 1 <= layer <= 6:
            self.layers[layer].append((effect_id, effect_data))
        else:
             logging.error(f"Invalid layer number '{layer}'. Must be 1-7.")
             return None

        # Dependencies analysis can be complex and might be deferred or simplified.
        # self._analyze_dependencies(effect_id, effect_data)

        logging.debug(f"Registered effect {effect_id} in layer {layer}" + (f" sublayer {sublayer}" if layer==7 else ""))
        self.invalidate_cache() # Invalidate cache when effects change
        return effect_id
    
    def invalidate_cache(self):
        """Invalidates any cached state, forcing recalculation."""
        self._last_applied_state_hash = None
        # Potentially reset cached characteristics on cards if applicable
        # for card_id in self._get_affected_card_ids():
        #     card = self.game_state._safe_get_card(card_id)
        #     if card:
        #          # Reset flags or calculated values stored on the card itself
        #          pass # Example: card._layers_applied = False
    
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
        found = False
        for layer_num in range(1, 8):
            if layer_num == 7:
                for sublayer in self.layers[7]:
                    initial_len = len(self.layers[7][sublayer])
                    self.layers[7][sublayer] = [(eid, data) for eid, data in self.layers[7][sublayer] if eid != effect_id]
                    if len(self.layers[7][sublayer]) < initial_len: found = True
            else:
                initial_len = len(self.layers[layer_num])
                self.layers[layer_num] = [(eid, data) for eid, data in self.layers[layer_num] if eid != effect_id]
                if len(self.layers[layer_num]) < initial_len: found = True

        if found:
            # Remove from timestamps and dependencies if found
            if effect_id in self.timestamps: del self.timestamps[effect_id]
            self.dependencies.pop(effect_id, None)
            # Remove from others' dependency lists
            for dep_list in self.dependencies.values():
                 if effect_id in dep_list: dep_list.remove(effect_id)
            self.invalidate_cache() # Invalidate cache on removal
            logging.debug(f"Removed effect {effect_id}")
        else:
             logging.warning(f"Attempted to remove non-existent effect ID: {effect_id}")
    
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
        ids_to_remove = []
        for layer_num in range(1, 8):
             if layer_num == 7:
                  for sublayer in self.layers[7]:
                       for eid, data in self.layers[7][sublayer]:
                            if data.get('source_id') == source_id:
                                 ids_to_remove.append(eid)
             else:
                  for eid, data in self.layers[layer_num]:
                       if data.get('source_id') == source_id:
                            ids_to_remove.append(eid)

        if ids_to_remove:
             logging.debug(f"Removing {len(ids_to_remove)} effects from source {source_id}")
             for eid in ids_to_remove:
                  self.remove_effect(eid) # Use remove_effect to handle cleanup and cache invalidation
             self.invalidate_cache() # Explicitly invalidate after bulk removal
             return True
        return False
    
    def _find_card_location(self, card_id):
        """Find which player controls a card and in which zone it is."""
        return self.game_state.find_card_location(card_id)
    

    def cleanup_expired_effects(self):
        """Remove effects that have expired based on duration and game state."""
        current_turn = self.game_state.turn
        current_phase = self.game_state.phase
        effects_to_remove = []

        for layer_num in range(1, 8):
            effects_list = []
            if layer_num == 7:
                 for sublayer in self.layers[7]:
                     effects_list.extend(self.layers[7][sublayer])
            else:
                 effects_list = self.layers[layer_num]

            for effect_id, effect_data in effects_list:
                 if self._is_effect_expired(effect_data, current_turn, current_phase):
                      effects_to_remove.append(effect_id)

        if effects_to_remove:
             logging.debug(f"Cleaning up {len(effects_to_remove)} expired effects.")
             for effect_id in effects_to_remove:
                  self.remove_effect(effect_id) # Use central removal method
             self.invalidate_cache() # Ensure cache is invalid after cleanup
        
        # After handling layer effects, let game state handle temporary control effects
        if current_phase == self.game_state.PHASE_END_STEP:
            self.game_state._revert_temporary_control()
    

    def _is_effect_expired(self, effect_data, current_turn, current_phase):
        """Check if an effect has expired based on its duration."""
        duration = effect_data.get('duration', 'permanent')
        start_turn = effect_data.get('start_turn', 0) # Assume effects store their start turn

        if duration == 'permanent': return False
        # Duration 'end_of_turn' means it expires during the cleanup step of the turn it was created
        # Or more precisely, *after* the end step completes, during cleanup.
        if duration == 'end_of_turn':
            # If it's past the turn it started OR it's the cleanup step of the starting turn
            return current_turn > start_turn or \
                   (current_turn == start_turn and current_phase == self.game_state.PHASE_CLEANUP)
        if duration == 'until_your_next_turn':
            # Expires at the START of the controller's next turn
            # Requires knowing the effect controller. Assuming it's implicit for now.
            return current_turn > start_turn # Simplified: expires once the turn number increments
        # Add more duration checks ('end_of_combat', etc.)
        return False