import logging
from collections import defaultdict

from numpy import copy

from Playersim.ability_utils import safe_int
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
        NOTE: Currently uses timestamp sorting within layers, dependency sorting is not active.
        Includes copying calculated protection details to live cards.
        """
        gs = self.game_state
        affected_card_ids = self._get_affected_card_ids()
        # Include cards with abilities that might be affected (e.g., losing abilities)
        cards_with_effects = set()
        for layer_effects in self.layers.values():
            if isinstance(layer_effects, list):
                cards_with_effects.update(data['source_id'] for _, data in layer_effects if 'source_id' in data)
            elif isinstance(layer_effects, dict): # Layer 7 sublayers
                for sub_effects in layer_effects.values():
                    cards_with_effects.update(data['source_id'] for _, data in sub_effects if 'source_id' in data)
        affected_card_ids.update(cards_with_effects)


        if not affected_card_ids:
            # Optimization: If no effects registered AND state hasn't changed since last FULL calc, skip.
            # Need a more robust hash check if skipping here.
            # For now, always run if potentially affected IDs change, even if 0 effects registered.
            # Recalculate characteristics for all cards on battlefield if no effects, to reset them.
            # Let's recalculate only if there *are* effects or potential effects.
            # If no effects, return early for optimization.
            # Check if any card characteristics have been calculated previously
            if not getattr(self, '_calculated_characteristics_cache', {}):
                # logging.debug("LayerSystem: No effects registered and no previous calculation cache. Skipping.")
                # No need to recalculate anything if no effects AND nothing was calculated before
                return # Exit early
            else:
                # Clear cache and proceed to reset cards below.
                self._calculated_characteristics_cache = {}
                # logging.debug("LayerSystem: No effects registered, resetting all battlefield cards to base.")
                # Re-fetch all battlefield cards that *might* have had characteristics modified previously
                # Re-initialize affected_card_ids to include all battlefield cards to ensure reset
                affected_card_ids = set()
                if gs.p1 and 'battlefield' in gs.p1: affected_card_ids.update(gs.p1["battlefield"])
                if gs.p2 and 'battlefield' in gs.p2: affected_card_ids.update(gs.p2["battlefield"])
                # If still no cards, exit
                if not affected_card_ids: return


        # Optimization Hash Check (remains the same)
        # Note: This hash might not capture all relevant state changes (e.g., counters, temporary effects expiring).
        # For absolute correctness, hashing the full relevant game state would be needed, but is costly.
        state_tuple_items = list(gs.p1.get("battlefield", [])) + list(gs.p2.get("battlefield", []))
        # Include timestamps hash maybe?
        timestamps_tuple = tuple(sorted(self.timestamps.items()))
        current_state_tuple = (
            tuple(sorted(state_tuple_items)),
            self.effect_counter, # Track effect registrations/removals
            gs.turn, # Include turn number
            gs.phase, # Include phase
            timestamps_tuple # Include timestamps of registered effects
        )
        current_state_hash = hash(current_state_tuple)

        if hasattr(self,'_last_applied_state_hash') and current_state_hash == self._last_applied_state_hash:
            # logging.debug("LayerSystem: State hash matched, skipping recalculation.")
            return

        # --- Proceed with recalculation ---
        # logging.debug(f"LayerSystem: Recalculating effects for {len(affected_card_ids)} cards.")
        calculated_characteristics = {} # Store calculated state during this run

        # 1. Initialize: Get base characteristics for ALL affected cards
        from .card import Card # Ensure Card class is available
        for card_id in affected_card_ids:
            original_card = gs.card_db.get(card_id)
            live_card = gs._safe_get_card(card_id) # Find the live object

            # Need the card object to exist in db and live state
            # Check if the card is currently in a zone where layers apply (usually battlefield)
            card_owner, card_zone = gs.find_card_location(card_id)
            if card_zone != 'battlefield':
                # If card left battlefield, its effects should have been removed. Skip calculation.
                # logging.debug(f"LayerSystem: Skipping card {card_id}, not on battlefield (in {card_zone}).")
                continue
            if not original_card:
                # Use live card as base if original DB entry missing (e.g., for tokens)
                if live_card and hasattr(live_card, 'is_token') and live_card.is_token:
                    original_card = live_card # Use token's current state as 'base'
                else:
                    logging.warning(f"LayerSystem: Could not find original card data for ID {card_id}. Skipping.")
                    continue

            # Use live card reference to get base state if needed, and for counter application
            if not live_card: live_card = original_card # Fallback if not found in GS zones? Risky.


            # --- Deep copy mutable types ---
            base_chars = {
                'name': getattr(original_card, 'name', 'Unknown'),
                'mana_cost': getattr(original_card, 'mana_cost', ''),
                'colors': copy.deepcopy(getattr(original_card, 'colors', [0]*5)), # Deep copy list
                'card_types': copy.deepcopy(getattr(original_card, 'card_types', [])), # Deep copy list
                'subtypes': copy.deepcopy(getattr(original_card, 'subtypes', [])), # Deep copy list
                'supertypes': copy.deepcopy(getattr(original_card, 'supertypes', [])), # Deep copy list
                'oracle_text': getattr(original_card, 'oracle_text', ''),
                # Start with base keywords array (ensure correct length)
                'keywords': copy.deepcopy(getattr(original_card, 'keywords', [0]*len(Card.ALL_KEYWORDS))), # Deep copy list
                'power': getattr(original_card, 'power', None), # Keep None if base is None
                'toughness': getattr(original_card, 'toughness', None), # Keep None if base is None
                'loyalty': getattr(original_card, 'loyalty', None), # Keep None if base is None
                'defense': getattr(original_card, 'defense', None), # For Battles
                'cmc': getattr(original_card, 'cmc', 0),
                'type_line': getattr(original_card, 'type_line', ''),
                # Base P/T tracked separately, default to original values or 0 if None
                '_base_power': getattr(original_card, 'power', 0) if getattr(original_card, 'power', None) is not None else 0,
                '_base_toughness': getattr(original_card, 'toughness', 0) if getattr(original_card, 'toughness', None) is not None else 0,
                # Calculate inherent abilities from BASE text (before Layer 3 changes)
                '_inherent_abilities': self._approximate_keywords_set(getattr(original_card, 'oracle_text', '')),
                '_granted_abilities': set(), # Track granted abilities within this calculation pass
                '_removed_abilities': set(), # Track removed abilities within this calculation pass
                '_active_protection_details': set(), # *** Initialize empty set for protection details ***
                '_controller': gs.get_card_controller(card_id), # Get current controller
                '_live_card_ref': live_card # Store reference to the live object for counter checks
            }
            # Ensure base keywords array has correct dimension
            if len(base_chars['keywords']) != len(Card.ALL_KEYWORDS):
                logging.warning(f"Correcting keyword array dimension for {base_chars['name']} ({card_id}). Expected {len(Card.ALL_KEYWORDS)}, got {len(base_chars['keywords'])}.")
                kw_copy = base_chars['keywords'][:] # Copy
                base_chars['keywords'] = [0] * len(Card.ALL_KEYWORDS)
                common_len = min(len(kw_copy), len(base_chars['keywords']))
                base_chars['keywords'][:common_len] = kw_copy[:common_len] # Copy known values


            calculated_characteristics[card_id] = base_chars

        # If no cards were initialized (all skipped), exit early
        if not calculated_characteristics:
            # logging.debug("LayerSystem: No valid cards found for layer application.")
            self._last_applied_state_hash = current_state_hash # Still update hash to avoid re-check
            return

        # Store calculated characteristics temporarily for internal lookups during layer application
        self._calculated_characteristics_cache = calculated_characteristics

        # 2. Apply Layers Sequentially
        # --- Layer 1: Copy ---
        sorted_layer1 = self._sort_layer_effects(1, self.layers[1])
        for _, effect_data in sorted_layer1:
            # Only apply if effect condition is met
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer1_copy(effect_data, calculated_characteristics)

        # --- Layer 2: Control ---
        sorted_layer2 = self._sort_layer_effects(2, self.layers[2])
        for _, effect_data in sorted_layer2:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer2_control(effect_data, calculated_characteristics)

        # --- Layer 3: Text ---
        sorted_layer3 = self._sort_layer_effects(3, self.layers[3])
        for _, effect_data in sorted_layer3:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer3_text(effect_data, calculated_characteristics)
            # Re-calculate inherent abilities AFTER text change for affected cards
            target_ids = effect_data.get('affected_ids', [])
            for target_id in target_ids:
                if target_id in calculated_characteristics:
                    chars = calculated_characteristics[target_id]
                    # Recalculate using the MODIFIED oracle_text in chars dict
                    chars['_inherent_abilities'] = self._approximate_keywords_set(chars.get('oracle_text', ''))


        # --- Layer 4: Type ---
        sorted_layer4 = self._sort_layer_effects(4, self.layers[4])
        for _, effect_data in sorted_layer4:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer4_type(effect_data, calculated_characteristics)

        # --- Layer 5: Color ---
        sorted_layer5 = self._sort_layer_effects(5, self.layers[5])
        for _, effect_data in sorted_layer5:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer5_color(effect_data, calculated_characteristics)

        # --- Layer 6: Abilities ---
        sorted_layer6 = self._sort_layer_effects(6, self.layers[6])
        for _, effect_data in sorted_layer6:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            # Apply effect data to modify _granted_abilities and _removed_abilities sets
            self._calculate_layer6_abilities(effect_data, calculated_characteristics)
        # Finalize keywords array and protection details for each card AFTER all layer 6 effects are calculated
        for card_id in calculated_characteristics:
            self._update_final_keywords(calculated_characteristics[card_id])


        # --- Layer 7: P/T ---
        # IMPORTANT: Re-check if object is a creature AFTER Layer 4 (Type changing)

        # 7a: CDAs and Base P/T setting
        sorted_layer7a = self._sort_layer_effects(7, self.layers[7].get('a',[]), sublayer='a') # Use .get for safety
        for _, effect_data in sorted_layer7a:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer7a_cda_and_base(effect_data, calculated_characteristics) # Use correct method

        # 7b: Effects setting P/T to specific values (e.g., "becomes 1/1")
        sorted_layer7b = self._sort_layer_effects(7, self.layers[7].get('b', []), sublayer='b') # Use 'b' sublayer
        for _, effect_data in sorted_layer7b:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer7b_set_specific(effect_data, calculated_characteristics) # Use correct method

        # 7c: P/T modifications from counters (+1/+1, -1/-1)
        for card_id in calculated_characteristics:
            # Check if it's a creature *now*
            if 'creature' in calculated_characteristics[card_id].get('card_types', []):
                self._calculate_layer7c_counters(card_id, calculated_characteristics[card_id]) # Use correct method

        # 7d: P/T modifications from static abilities (+X/+Y, Anthems)
        sorted_layer7d = self._sort_layer_effects(7, self.layers[7].get('c', []), sublayer='c')
        for _, effect_data in sorted_layer7d:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer7d_modify(effect_data, calculated_characteristics) # Use correct method

        # 7e: P/T switching
        sorted_layer7e = self._sort_layer_effects(7, self.layers[7].get('d', []), sublayer='d')
        for _, effect_data in sorted_layer7e:
            if callable(effect_data.get('condition')) and not effect_data['condition'](gs): continue
            self._calculate_layer7e_switch(effect_data, calculated_characteristics) # Use correct method


        # 3. Update GameState LIVE Card Objects
        for card_id, final_chars in calculated_characteristics.items():
            # Use the stored live_card_ref
            live_card = final_chars.get('_live_card_ref')
            if not live_card:
                # logging.debug(f"No live card ref found for {card_id} during update.")
                continue

            # Re-fetch from GameState in case it was recreated (e.g., token copy)
            live_card_check = gs._safe_get_card(card_id)
            if not live_card_check:
                # Card might have been removed during layer application/SBA loop within layers? Unlikely but possible.
                # logging.debug(f"Live card {card_id} disappeared during layer application.")
                continue
            elif live_card_check != live_card:
                # Reference changed, use the new one
                live_card = live_card_check


            # Apply calculated characteristics
            for attr, value in final_chars.items():
                # Skip internal _ attributes EXCEPT the ones we want to store on the card
                if attr.startswith('_') and attr not in ['_base_power', '_base_toughness', '_active_protection_details']:
                     continue

                # *** Explicitly handle setting the protection details ***
                if attr == '_active_protection_details':
                    setattr(live_card, 'active_protections', value) # Set the calculated set on the live card
                    continue # Move to next attribute

                # Ensure the attribute exists on the live Card object before setting
                if hasattr(live_card, attr):
                    current_live_value = getattr(live_card, attr, None)
                    # Handle None P/T conversion if becoming non-creature later
                    if value is None and isinstance(current_live_value, (int, float)):
                        # Defer setting P/T to 0 until after non-creature check below
                        pass # Don't set P/T to None if it was numeric
                    # Safe type conversions
                    elif value is not None and current_live_value is not None and not isinstance(value, type(current_live_value)):
                        try:
                            # Attempt common safe conversions
                            current_type = type(current_live_value)
                            if current_type == int and isinstance(value, float): converted_value = int(value)
                            elif current_type == float and isinstance(value, int): converted_value = float(value)
                            elif current_type == int and isinstance(value, str) and value.isdigit(): converted_value = int(value)
                            elif current_type == str and not isinstance(value, str): converted_value = str(value)
                            elif isinstance(current_live_value, list) and isinstance(value, (list, set)): converted_value = list(value) # Allow set->list
                            elif isinstance(current_live_value, set) and isinstance(value, (list, set)): converted_value = set(value) # Allow list->set
                            else:
                                # Cannot safely convert, skip set and log
                                logging.warning(f"LayerSystem Update: Cannot safely convert value for '{attr}' on {card_id}. Expected {current_type}, got {type(value)}. Skipping set.")
                                continue # Skip setting this attribute
                            # Set the converted value
                            setattr(live_card, attr, converted_value)
                        except (TypeError, ValueError) as conv_e:
                            logging.error(f"LayerSystem Update: Error converting value for '{attr}' on {card_id}. Value: {repr(value)}. Error: {conv_e}")
                    else: # Types match or target is None, set directly
                        try:
                            # Optimization: only set if value changed
                            if value != current_live_value:
                                setattr(live_card, attr, value)
                        except Exception as e:
                            logging.error(f"LayerSystem Update: Error setting attribute '{attr}' on card {card_id}. Value: {repr(value)}. Error: {e}")
                else:
                     # Attribute doesn't exist on live card - might be okay for internal _base P/T
                     if attr not in ['_base_power', '_base_toughness']:
                         logging.warning(f"LayerSystem Update: Live card {card_id} missing attribute '{attr}' during final update.")


            # --- Final Checks/Adjustments ---
            # Ensure non-creatures have 0 P/T (Rule 208.3)
            if 'creature' not in getattr(live_card, 'card_types', []):
                # Only set to 0 if they were previously defined
                if hasattr(live_card, 'power') and live_card.power is not None and live_card.power != 0:
                    live_card.power = 0
                if hasattr(live_card, 'toughness') and live_card.toughness is not None and live_card.toughness != 0:
                    live_card.toughness = 0

        self._last_applied_state_hash = current_state_hash
        # logging.debug(f"LayerSystem: Finished applying effects.")
        # Clear the temporary calculation cache
        self._calculated_characteristics_cache = {}
        

    def _calculate_layer7c_counters(self, card_id, char_dict): # Renamed from _calculate_layer7b_counters
        # Layer 7c application - uses LIVE card's counters, affects calculated characteristics
        live_card = char_dict.get('_live_card_ref')
        # Check if it's a creature at this point in layer application
        if live_card and 'creature' in char_dict.get('card_types', []):
            if hasattr(live_card, 'counters') and live_card.counters:
                plus_counters = live_card.counters.get('+1/+1', 0)
                minus_counters = live_card.counters.get('-1/-1', 0)

                # Annihilation should have already happened in SBAs if P/T is checked there.
                # Recalculate net change based on potentially cleaned counters.
                net_change = plus_counters - minus_counters
                if net_change != 0:
                    # Check if power/toughness exist before modifying (should exist if creature)
                    if 'power' in char_dict: char_dict['power'] += net_change
                    if 'toughness' in char_dict: char_dict['toughness'] += net_change
                    logging.debug(f"Layer 7c: Applied {net_change:+} P/T from counters to {card_id} (P/T now {char_dict.get('power')}/{char_dict.get('toughness')})")

    def _calculate_layer7d_modify(self, effect_data, calculated_characteristics): # Renamed from _calculate_layer7c_modify
        # [...] (Logic remains the same)
        effect_type = effect_data.get('effect_type')
        value = effect_data.get('effect_value')
        for target_id in effect_data.get('affected_ids', []):
            if target_id in calculated_characteristics:
                chars = calculated_characteristics[target_id]
                # Only apply P/T mods if it's a creature
                if 'creature' in chars.get('card_types', []):
                    if effect_type == 'modify_pt' and isinstance(value, (tuple, list)) and len(value)==2:
                        p_mod, t_mod = value
                        # Check if power/toughness exist before modifying
                        if 'power' in chars: chars['power'] += p_mod
                        if 'toughness' in chars: chars['toughness'] += t_mod
                        logging.debug(f"Layer 7d: Modified P/T of {target_id} by {p_mod:+}/{t_mod:+}. New P/T: {chars.get('power')}/{chars.get('toughness')}")
                    # Add variable P/T modification logic here
                    elif effect_type == 'modify_pt_variable':
                        count_type = value # e.g., 'artifact'
                        controller = chars.get('_controller')
                        if controller:
                            count = sum(1 for cid in controller.get("battlefield", []) if count_type in getattr(self.game_state._safe_get_card(cid),'card_types',[]))
                            # Check if power/toughness exist before modifying
                            if 'power' in chars: chars['power'] += count
                            if 'toughness' in chars: chars['toughness'] += count
                            logging.debug(f"Layer 7d (Var): Modified P/T of {target_id} by +{count}/+{count} based on {count_type}. New P/T: {chars.get('power')}/{chars.get('toughness')}")

    def _calculate_layer7e_switch(self, effect_data, calculated_characteristics): # Renamed from _calculate_layer7d_switch
         # [...] (Logic remains the same)
         effect_type = effect_data.get('effect_type')
         for target_id in effect_data.get('affected_ids', []):
              if target_id in calculated_characteristics:
                  chars = calculated_characteristics[target_id]
                  # Only switch if it's a creature
                  if 'creature' in chars.get('card_types', []):
                      if effect_type == 'switch_pt':
                           # Check if power/toughness exist before switching
                           if 'power' in chars and 'toughness' in chars:
                               p, t = chars['power'], chars['toughness']
                               chars['power'], chars['toughness'] = t, p
                               logging.debug(f"Layer 7e: Switched P/T of {target_id} to {t}/{p}")

    def _parse_layer7_effect(self, effect_lower):
        """Parse P/T effects for Layer 7."""
        # Layer 7a: Set Base P/T (e.g., from copy effects or abilities setting base)
        match = re.search(r"(?:base power and toughness|base power|base toughness)\s+(?:is|are)\s+(\d+)/(\d+)", effect_lower)
        if match:
            # Use the safe_int utility function if available, otherwise basic int()
            safe_int_func = safe_int if 'safe_int' in globals() else lambda x, d=None: int(x)
            power = safe_int_func(match.group(1), None); toughness = safe_int_func(match.group(2), None)
            if power is not None and toughness is not None:
                 return {'sublayer': 'a', 'effect_type': 'set_base_pt', 'effect_value': (power, toughness)}
        # Handle Characteristic-Defining Abilities setting base P/T
        match_cda = re.search(r"(?:power and toughness are each equal to|power is equal to|toughness is equal to)\b", effect_lower)
        # Add a negative lookbehind to avoid matching "...is equal to its toughness..." type phrases for CDA base P/T
        if match_cda and not re.search(r"(its power|its toughness)\b", effect_lower[:match_cda.start()]):
             # Register CDA P/T setting effect, actual calculation deferred to LayerSystem application
             cda_type = 'unknown'
             if "number of cards in your graveyard" in effect_lower: cda_type = 'graveyard_count_self'
             elif "number of creatures you control" in effect_lower: cda_type = 'creature_count_self'
             # Add more common CDA types
             logging.debug(f"Registering Layer 7a CDA effect: {cda_type}")
             return {'sublayer': 'a', 'effect_type': 'set_pt_cda', 'effect_value': cda_type} # Pass CDA type identifier

        # Layer 7b: Setting P/T to specific values (without changing base P/T). Examples: "becomes a 1/1", "is a 0/1"
        # Note: These often come with type changes in Layer 4. Layer 7 only handles the P/T part.
        match = re.search(r"\bis a\b\s+(\d+)/(\d+)", effect_lower) or re.search(r"\bbecomes a\b\s+(\d+)/(\d+)", effect_lower)
        if match:
             safe_int_func = safe_int if 'safe_int' in globals() else lambda x, d=None: int(x)
             power = safe_int_func(match.group(1), None); toughness = safe_int_func(match.group(2), None)
             if power is not None and toughness is not None:
                  return {'sublayer': 'b', 'effect_type': 'set_pt', 'effect_value': (power, toughness)}

        # Layer 7c: P/T modification from static abilities (+X/+Y, -X/-Y), anthems etc.
        # Simple +/- N/N modifications
        match = re.search(r"gets? ([+\-]\d+)/([+\-]\d+)", effect_lower)
        if match:
            safe_int_func = safe_int if 'safe_int' in globals() else lambda x, d=None: int(x)
            p_mod = safe_int_func(match.group(1), None); t_mod = safe_int_func(match.group(2), None)
            if p_mod is not None and t_mod is not None:
                 return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Anthem patterns (+N/+N) - Allow flexible spacing and wording
        match = re.search(r"(?:get|have)\s*\+\s*(\d+)\s*/\s*\+\s*(\d+)", effect_lower)
        if match:
            safe_int_func = safe_int if 'safe_int' in globals() else lambda x, d=None: int(x)
            p_mod = safe_int_func(match.group(1), None); t_mod = safe_int_func(match.group(2), None)
            if p_mod is not None and t_mod is not None:
                 return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Penalty patterns (-N/-N) - Allow flexible spacing and wording
        match = re.search(r"(?:get|have)\s*-\s*(\d+)\s*/\s*-\s*(\d+)", effect_lower)
        if match:
            safe_int_func = safe_int if 'safe_int' in globals() else lambda x, d=None: int(x)
            p_mod = -safe_int_func(match.group(1), 0); t_mod = -safe_int_func(match.group(2), 0)
            if p_mod is not None and t_mod is not None: # Check result of safe_int
                return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Variable P/T modification (e.g., +X/+X where X is count)
        # Refined Regex to handle different phrasings and plurals
        match_var = re.search(r"(?:gets?|have)\s*\+X/\+X\s+where X is the number of (\w+)(?:s)?\b", effect_lower)
        if match_var:
             count_type = match_var.group(1).strip().lower() # Use singular form
             # Register variable P/T effect, calculation deferred
             logging.debug(f"Registering Layer 7c variable P/T effect based on: {count_type}")
             return {'sublayer': 'c', 'effect_type': 'modify_pt_variable', 'effect_value': count_type}

        # Layer 7d: Switch P/T
        if "switch" in effect_lower and "power and toughness" in effect_lower:
            return {'sublayer': 'd', 'effect_type': 'switch_pt', 'effect_value': True}

        return None # No Layer 7 effect parsed

    def get_characteristic(self, card_id, characteristic):
        """Get the current characteristic of a card after layers have been applied (read-only)."""
        # Check the calculation cache first
        if hasattr(self, '_calculated_characteristics_cache') and self._calculated_characteristics_cache:
            if card_id in self._calculated_characteristics_cache:
                 return self._calculated_characteristics_cache[card_id].get(characteristic)
            # If not in cache, maybe it wasn't affected by effects? Fallback to live card.

        # If no cache or not found, read from the live card object in GameState
        live_card = self.game_state._safe_get_card(card_id)
        if live_card:
            return getattr(live_card, characteristic, None)
        # If not found anywhere, return None or default
        return None
        
    def _calculate_layer7b_set_specific(self, effect_data, calculated_characteristics):
         effect_type = effect_data.get('effect_type')
         value = effect_data.get('effect_value')
         source_id = effect_data.get('source_id') # Get source for Impending check

         for target_id in effect_data.get('affected_ids', []):
             if target_id in calculated_characteristics:
                 chars = calculated_characteristics[target_id]
                 # --- ADDED: Check if creature *at this point* ---
                 if 'creature' not in chars.get('card_types', []):
                      # Logging moved inside check: If it's Impending setting to 0/0, don't log as skip.
                      is_impending_set_pt_effect = effect_type == 'set_pt' and value == (0,0) and source_id == target_id # Specific check
                      if not is_impending_set_pt_effect:
                           logging.debug(f"Layer 7b: Skipping P/T set for {target_id} - no longer a creature.")
                      continue # Skip P/T effects if not currently a creature
                 # --- END ADDED ---

                 # Apply standard P/T setting logic
                 if effect_type == 'set_pt' and isinstance(value, (tuple, list)) and len(value)==2:
                     p, t = value
                     # This sets current P/T but doesn't change the _base_ P/T
                     chars['power'], chars['toughness'] = p, t
                     logging.debug(f"Layer 7b: Set specific P/T of {target_id} to {p}/{t}")
                          
        
    def _calculate_layer7a_cda_and_base(self, effect_data, calculated_characteristics): # Renamed from _calculate_layer7a_set
         effect_type = effect_data.get('effect_type')
         value = effect_data.get('effect_value')
         for target_id in effect_data.get('affected_ids', []):
             if target_id in calculated_characteristics:
                  chars = calculated_characteristics[target_id]
                  # CDA Effect: Base P/T determined by some condition
                  if effect_type == 'set_pt_cda':
                      cda_type = value # e.g., 'graveyard_count_self'
                      # Actual value calculation happens here based on current game state
                      if cda_type == 'graveyard_count_self':
                          controller = chars.get('_controller')
                          if controller:
                               count = len(controller.get("graveyard", []))
                               # These set both _base_ and current P/T
                               chars['_base_power'] = count; chars['_base_toughness'] = count
                               chars['power'] = count; chars['toughness'] = count
                               logging.debug(f"Layer 7a (CDA): Set Base P/T of {target_id} based on GY count ({count})")
                      elif cda_type == 'creature_count_self':
                           controller = chars.get('_controller')
                           if controller:
                                count = sum(1 for cid in controller.get("battlefield", []) if 'creature' in getattr(self.game_state._safe_get_card(cid),'card_types',[]))
                                # These set both _base_ and current P/T
                                chars['_base_power'] = count; chars['_base_toughness'] = count
                                chars['power'] = count; chars['toughness'] = count
                                logging.debug(f"Layer 7a (CDA): Set Base P/T of {target_id} based on creature count ({count})")
                      # Add more CDA calculations
                  # Set Base P/T (from copy effects, etc.)
                  elif effect_type == 'set_base_pt' and isinstance(value, (tuple, list)) and len(value)==2:
                       p, t = value
                       # This UPDATES the base P/T tracker, and sets current P/T
                       chars['_base_power'] = p; chars['_base_toughness'] = t
                       chars['power'], chars['toughness'] = p, t
                       logging.debug(f"Layer 7a: Set Base P/T of {target_id} to {p}/{t}")
    
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

        
    def _sort_layer_effects(self, layer_num, effects, sublayer=None):
        """
        Sorts effects for a given layer/sublayer based primarily on timestamps.
        Includes a basic check for critical layer dependencies that might override timestamps.
        """
        gs = self.game_state

        # Check for potential dependencies before timestamp sorting
        # Simple dependency: Layer 4 (type) affects Layer 6 (abilities) and 7 (P/T)
        # If effect A in Layer 6/7 targets X, and effect B in Layer 4 targets X,
        # B should conceptually apply first. Timestamp usually handles this, but edge cases exist.

        # Perform timestamp sort (Python's sort is stable)
        # Fallback to 0 if effect_id somehow missing from timestamps dict
        sorted_effects = sorted(effects, key=lambda x: self.timestamps.get(x[0], 0))

        # --- Dependency Check (Post-Sort Warning/Potential Reorder - Simplified) ---
        # This check detects potential violations if timestamps lead to wrong order
        # Note: Full dependency resolution is complex (topological sort). This is a simpler check.
        if layer_num in [6, 7] and self.layers.get(4): # Check only if Layer 4 effects exist
            layer4_effects_map = defaultdict(list)
            # Collect all *active* Layer 4 effects
            for effect_id4, data4 in self.layers[4]:
                # Check effect condition *now*
                is_active4 = True # Assume active if no condition
                if callable(data4.get('condition')):
                     try: is_active4 = data4['condition'](gs)
                     except Exception as e: logging.warning(f"Error checking condition for L4 effect {effect_id4}: {e}"); is_active4=False # Treat error as inactive
                if not is_active4: continue

                # Store effect keyed by target_id
                for target_id in data4.get('affected_ids', []):
                     layer4_effects_map[target_id].append((effect_id4, data4))

            # Check effects in the current layer (6 or 7) against Layer 4 effects
            for i in range(len(sorted_effects)):
                effect_id1, effect_data1 = sorted_effects[i]
                # Skip if this effect itself isn't active
                is_active1 = True
                if callable(effect_data1.get('condition')):
                     try: is_active1 = effect_data1['condition'](gs)
                     except Exception as e: logging.warning(f"Error checking condition for L{layer_num} effect {effect_id1}: {e}"); is_active1=False
                if not is_active1: continue

                source_id1 = effect_data1['source_id']
                ts1 = self.timestamps.get(effect_id1, 0)

                for target_id in effect_data1.get('affected_ids', []):
                    # Check if this target has Layer 4 effects applied later
                    if target_id in layer4_effects_map:
                         for effect_id4, effect_data4 in layer4_effects_map[target_id]:
                             source_id4 = effect_data4['source_id']
                             ts4 = self.timestamps.get(effect_id4, 0)
                             # If a relevant Layer 4 effect has a *later* timestamp, it's a potential dependency violation
                             if ts4 > ts1 and source_id1 != source_id4:
                                 logging.warning(f"Potential Layer Dependency Violation: Layer {layer_num} effect {effect_id1} (TS:{ts1}) "
                                                 f"on {target_id} might depend on Layer 4 effect {effect_id4} (TS:{ts4}) which has a later timestamp.")
                                 # Simple Reorder (Swap adjacent pair if violation found): Risky, can cascade.
                                 # If full topological sort isn't implemented, logging is the safest action.

        # Return timestamp-sorted list (potentially with logged warnings)
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
                 # Ensure lists exist and make copies to modify
                 current_types = chars['card_types'][:]
                 current_subtypes = chars['subtypes'][:]

                 if effect_type == 'add_type':
                     if isinstance(type_val, list): # Handle list of types
                         for t in type_val:
                              if t not in current_types: current_types.append(t)
                     elif isinstance(type_val, str) and type_val not in current_types:
                         current_types.append(type_val)
                 elif effect_type == 'set_type':
                      chars['card_types'] = list(type_val) if isinstance(type_val, list) else [type_val]
                      # Rule 613.1d: Setting type removes previous card types but not supertypes/subtypes initially.
                      # However, new type might make subtypes invalid. Check needed?
                      # For simplicity, we keep subtypes unless explicitly removed or set.
                 elif effect_type == 'remove_type':
                     if isinstance(type_val, list):
                         current_types = [t for t in current_types if t not in type_val]
                     elif isinstance(type_val, str) and type_val in current_types:
                          current_types.remove(type_val)
                 elif effect_type == 'add_subtype':
                     if isinstance(type_val, list):
                         for s in type_val:
                             if s not in current_subtypes: current_subtypes.append(s)
                     elif isinstance(type_val, str) and type_val not in current_subtypes:
                          current_subtypes.append(type_val)
                 elif effect_type == 'set_subtype': # Non-standard, but useful
                      chars['subtypes'] = list(type_val) if isinstance(type_val, list) else [type_val]
                 elif effect_type == 'remove_subtype':
                     if isinstance(type_val, list):
                         current_subtypes = [s for s in current_subtypes if s not in type_val]
                     elif isinstance(type_val, str) and type_val in current_subtypes:
                         current_subtypes.remove(type_val)
                 elif effect_type == 'lose_all_subtypes': # Often type-specific, e.g., lose all creature types
                      type_to_lose = type_val # Expecting 'creature', 'artifact', etc.
                      # This is complex. If it loses 'creature', does it lose 'Goblin'? Yes.
                      # We need a mapping of subtypes to major types. For now, simple remove all.
                      # TODO: Implement subtype removal more precisely based on 'type_to_lose'.
                      current_subtypes = []

                 # Apply changes back to characteristics dict
                 chars['card_types'] = current_types
                 chars['subtypes'] = current_subtypes

                 # Rebuild type line based on updated characteristics
                 try:
                     chars['type_line'] = self.game_state._build_type_line(chars) # Assume GS has helper
                 except Exception as e:
                     logging.error(f"Error rebuilding type line for {target_id}: {e}")

                 logging.debug(f"Layer 4: {effect_type} {type_val} applied to {target_id}. New types: {chars['card_types']}, subtypes: {chars['subtypes']}")

    def _is_impending_active(self, card_id):
        """Delegates check to GameState to determine if Impending is active (has time counters)."""
        return self.game_state._is_impending_active(card_id)

    def _calculate_layer5_color(self, effect_data, calculated_characteristics):
         effect_type = effect_data.get('effect_type')
         color_val = effect_data.get('effect_value') # Should be [W,U,B,R,G] list
         for target_id in effect_data.get('affected_ids', []):
             if target_id in calculated_characteristics:
                 chars = calculated_characteristics[target_id]
                 current_colors = chars['colors'] # Get mutable list reference

                 if effect_type == 'set_color' and isinstance(color_val, list) and len(color_val) == 5:
                     chars['colors'] = color_val[:] # Set to a copy of the new value
                     logging.debug(f"Layer 5: Set color of {target_id} to {chars['colors']}")
                 elif effect_type == 'add_color' and isinstance(color_val, list) and len(color_val) == 5:
                     modified = False
                     for i in range(5):
                         if color_val[i] == 1 and current_colors[i] == 0:
                             current_colors[i] = 1
                             modified = True
                     if modified: logging.debug(f"Layer 5: Added color to {target_id}. New colors: {chars['colors']}")
                 # Add 'remove_color' if needed

    def _calculate_layer6_abilities(self, effect_data, calculated_characteristics):
        effect_type = effect_data.get('effect_type')
        ability_val = effect_data.get('effect_value') # Usually string name
        for target_id in effect_data.get('affected_ids', []):
            if target_id in calculated_characteristics:
                chars = calculated_characteristics[target_id]
                # Initialize sets if they don't exist
                if '_granted_abilities' not in chars: chars['_granted_abilities'] = set()
                if '_removed_abilities' not in chars: chars['_removed_abilities'] = set()
                # *** NEW: Initialize set for protection details ***
                if '_active_protection_details' not in chars: chars['_active_protection_details'] = set()

                ability_val_lower = str(ability_val).lower() # Normalize

                if effect_type == 'add_ability':
                    # Grant the ability
                    chars['_granted_abilities'].add(ability_val_lower)
                    chars['_removed_abilities'].discard(ability_val_lower) # Grant takes precedence

                    # *** NEW: Handle specific protection granting ***
                    if ability_val_lower.startswith("protection from "):
                        detail = ability_val_lower.split("protection from ", 1)[1]
                        chars['_granted_abilities'].add("protection") # Grant base keyword too
                        chars['_active_protection_details'].add(detail.strip())
                    # Handle specific ward granting
                    elif ability_val_lower.startswith("ward"):
                        # detail = ability_val_lower # Keep full "ward {X}" string
                        # chars['_active_ward_details']? Or just rely on the main keyword grant? Keep simple for now.
                        pass # Granted set handles "ward {X}" presence

                    logging.debug(f"Layer 6: Granted '{ability_val_lower}' to {target_id}")

                elif effect_type == 'remove_ability':
                    chars['_removed_abilities'].add(ability_val_lower)
                    chars['_granted_abilities'].discard(ability_val_lower) # Removing takes precedence

                    # *** NEW: Remove specific protection/ward details ***
                    if ability_val_lower.startswith("protection from "):
                        detail = ability_val_lower.split("protection from ", 1)[1]
                        chars['_active_protection_details'].discard(detail.strip())
                        # Should we remove the base 'protection' keyword too if all specifics are gone? Complex dependency.
                        # Let's assume removing "protection from red" doesn't remove general "protection" keyword if granted elsewhere.
                    elif ability_val_lower == "protection": # Remove all specific protections if base keyword lost
                        chars['_active_protection_details'].clear()

                    logging.debug(f"Layer 6: Removed '{ability_val_lower}' from {target_id}")

                elif effect_type == 'remove_all_abilities':
                    # Mark all inherent abilities for removal
                    chars['_removed_abilities'].update(chars.get('_inherent_abilities', set()))
                    # Also remove any currently granted abilities
                    chars['_removed_abilities'].update(chars['_granted_abilities'])
                    # Clear the granted set itself
                    chars['_granted_abilities'].clear()
                    # *** NEW: Clear protection details too ***
                    chars['_active_protection_details'].clear()
                    logging.debug(f"Layer 6: Marked all abilities for removal from {target_id}")
                elif effect_type in ['cant_attack', 'cant_block', 'must_attack', 'must_block']:
                    # Treat these as adding a specific ability keyword
                    chars['_granted_abilities'].add(effect_type)
                    chars['_removed_abilities'].discard(effect_type)
                    logging.debug(f"Layer 6: Applied '{effect_type}' to {target_id}")

    def _update_final_keywords(self, char_dict):
        """ Recalculates the 'keywords' array and populates '_active_protection_details' based on inherent, granted, and removed abilities. """
        # 1. Start with inherent keywords (from potentially text-changed state)
        inherent_keywords_set = char_dict.get('_inherent_abilities', set())

        # 2. Add granted abilities/keywords
        granted_set = char_dict.get('_granted_abilities', set())

        # 3. Remove removed abilities/keywords
        removed_set = char_dict.get('_removed_abilities', set())

        # Calculate final set of active keywords (handle "can't attack/block" separately if needed)
        final_keywords_set = (inherent_keywords_set.union(granted_set)) - removed_set

        # --- Update keyword array ---
        final_keyword_list = [0] * len(Card.ALL_KEYWORDS)
        for i, kw in enumerate(Card.ALL_KEYWORDS):
            # Check if the normalized keyword exists in the final set
            if kw.lower() in final_keywords_set:
                final_keyword_list[i] = 1
        char_dict['keywords'] = final_keyword_list

        # --- Update active protection details ---
        # Start with details derived from granted abilities this round
        # The set '_active_protection_details' was populated during _calculate_layer6
        current_protection_details = char_dict.get('_active_protection_details', set())

        # Also check inherent protection details IF the base 'protection' keyword wasn't removed
        if "protection" in inherent_keywords_set and "protection" not in removed_set:
            # Re-parse base oracle text for inherent protections
            # Note: Use the oracle_text from char_dict, which may have been modified by Layer 3
            base_text = char_dict.get('oracle_text', '').lower()
            inherent_matches = re.finditer(r"protection from ([\w\s\/]+?)(?:\s*(?:and|or|,|where)|\.|$)", base_text)
            for match in inherent_matches:
                protection_type = match.group(1).strip()
                # Check if this specific protection was removed explicitly
                if f"protection from {protection_type}" not in removed_set:
                    current_protection_details.add(protection_type)

        # Assign the final calculated set back to the char_dict
        char_dict['_active_protection_details'] = current_protection_details


        # Debugging log
        active_kws = [kw for i, kw in enumerate(Card.ALL_KEYWORDS) if final_keyword_list[i] == 1]
        #if active_kws: logging.debug(f"Final keywords for {char_dict.get('name', 'Unknown')}: {active_kws}")
        if current_protection_details: logging.debug(f"Final protection details for {char_dict.get('name', 'Unknown')}: {current_protection_details}")

    def _get_all_inherent_abilities(self, card_id):
        """ Helper to get the set of inherent abilities/keywords from a card's (potentially modified) text. """
        # This needs access to the *current* calculated oracle text for the card
        # Assuming calculated_characteristics holds this. Requires passing it in or accessing it.
        # Placeholder implementation
        # text = calculated_characteristics[card_id]['oracle_text']
        # return self._approximate_keywords_set(text)
        return set() # Placeholder


    def _approximate_keywords_set(self, oracle_text):
         """ Helper to get a set of keywords found in text. (Refined) """
         found_keywords = set()
         if not oracle_text: return found_keywords
         text_lower = oracle_text.lower()

         # Check canonical keywords
         for kw in Card.ALL_KEYWORDS:
              kw_lower = kw.lower()
              # More precise matching to avoid substrings ("linking" != "lifelink")
              # Regex with word boundaries for single words, simple check for multi-word
              pattern = r'\b' + re.escape(kw_lower) + r'\b' if ' ' not in kw_lower else re.escape(kw_lower)
              if re.search(pattern, text_lower):
                    # Specific exclusions (e.g., don't match "haste" in "afterhaste") are handled by word boundaries mostly.
                    # Handle parametrized keywords - mark the base keyword only
                    if kw_lower == "protection from": found_keywords.add("protection")
                    elif kw_lower == "landwalk": # Specific landwalk types imply 'landwalk'
                        found_keywords.add("landwalk")
                    elif kw_lower.endswith("walk") and kw_lower != "landwalk": found_keywords.add("landwalk")
                    else: found_keywords.add(kw_lower)

         # Handle common phrases not in keyword list exactly
         if "can't be blocked" in text_lower: found_keywords.add("unblockable")
         if "can't block" in text_lower: found_keywords.add("cant_block") # Treat as ability
         if "attacks each combat if able" in text_lower: found_keywords.add("must_attack")
         if "blocks each combat if able" in text_lower: found_keywords.add("must_block")


         return found_keywords

    # Layer 7 Helpers
    def _calculate_layer7a_set(self, effect_data, calculated_characteristics):
         effect_type = effect_data.get('effect_type')
         value = effect_data.get('effect_value')
         for target_id in effect_data.get('affected_ids', []):
             if target_id in calculated_characteristics:
                  chars = calculated_characteristics[target_id]
                  # CDA Effect: Base P/T determined by some condition
                  if effect_type == 'set_pt_cda':
                      cda_type = value # e.g., 'graveyard_count_self'
                      # Actual value calculation happens here based on current game state
                      if cda_type == 'graveyard_count_self':
                          controller = chars.get('_controller')
                          if controller:
                               count = len(controller.get("graveyard", []))
                               chars['power'] = count; chars['toughness'] = count
                               logging.debug(f"Layer 7a (CDA): Set P/T of {target_id} based on GY count ({count})")
                      elif cda_type == 'creature_count_self':
                           controller = chars.get('_controller')
                           if controller:
                                count = sum(1 for cid in controller.get("battlefield", []) if 'creature' in getattr(self.game_state._safe_get_card(cid),'card_types',[]))
                                chars['power'] = count; chars['toughness'] = count
                                logging.debug(f"Layer 7a (CDA): Set P/T of {target_id} based on creature count ({count})")
                      # Add more CDA calculations
                  # Set Base P/T (from copy effects, etc.)
                  elif effect_type == 'set_base_pt' and isinstance(value, (tuple, list)) and len(value)==2:
                       p, t = value
                       # This UPDATES the base P/T tracker, and sets current P/T
                       chars['_base_power'] = p; chars['_base_toughness'] = t
                       chars['power'], chars['toughness'] = p, t
                       logging.debug(f"Layer 7a: Set Base P/T of {target_id} to {p}/{t}")
                  # Set P/T (usually from effects like "becomes 1/1")
                  elif effect_type == 'set_pt' and isinstance(value, (tuple, list)) and len(value)==2:
                       p, t = value
                       # This sets current P/T but doesn't change the _base_ P/T
                       chars['power'], chars['toughness'] = p, t
                       logging.debug(f"Layer 7b: Set P/T of {target_id} to {p}/{t}")

    def _calculate_layer7b_counters(self, card_id, char_dict):
        # Layer 7b application - uses LIVE card's counters, affects calculated characteristics
        live_card = char_dict.get('_live_card_ref')
        # Check if it's a creature at this point in layer application
        if live_card and 'creature' in char_dict.get('card_types', []):
            if hasattr(live_card, 'counters') and live_card.counters:
                plus_counters = live_card.counters.get('+1/+1', 0)
                minus_counters = live_card.counters.get('-1/-1', 0)

                # Annihilation should have already happened in SBAs if P/T is checked there.
                # Recalculate net change based on potentially cleaned counters.
                net_change = plus_counters - minus_counters
                if net_change != 0:
                    # Check if power/toughness exist before modifying
                    if 'power' in char_dict: char_dict['power'] += net_change
                    if 'toughness' in char_dict: char_dict['toughness'] += net_change
                    logging.debug(f"Layer 7b: Applied {net_change:+} P/T from counters to {card_id} (P/T now {char_dict.get('power')}/{char_dict.get('toughness')})")
                    
    def _calculate_layer7c_modify(self, effect_data, calculated_characteristics):
        effect_type = effect_data.get('effect_type')
        value = effect_data.get('effect_value')
        for target_id in effect_data.get('affected_ids', []):
            if target_id in calculated_characteristics:
                chars = calculated_characteristics[target_id]
                # Only apply P/T mods if it's a creature
                if 'creature' in chars.get('card_types', []):
                    if effect_type == 'modify_pt' and isinstance(value, (tuple, list)) and len(value)==2:
                        p_mod, t_mod = value
                        # Check if power/toughness exist before modifying
                        if 'power' in chars: chars['power'] += p_mod
                        if 'toughness' in chars: chars['toughness'] += t_mod
                        logging.debug(f"Layer 7c: Modified P/T of {target_id} by {p_mod:+}/{t_mod:+}. New P/T: {chars.get('power')}/{chars.get('toughness')}")
                    # Add variable P/T modification logic here
                    elif effect_type == 'modify_pt_variable':
                        count_type = value # e.g., 'artifact'
                        controller = chars.get('_controller')
                        if controller:
                            count = sum(1 for cid in controller.get("battlefield", []) if count_type in getattr(self.game_state._safe_get_card(cid),'card_types',[]))
                            # Check if power/toughness exist before modifying
                            if 'power' in chars: chars['power'] += count
                            if 'toughness' in chars: chars['toughness'] += count
                            logging.debug(f"Layer 7c (Var): Modified P/T of {target_id} by +{count}/+{count} based on {count_type}. New P/T: {chars.get('power')}/{chars.get('toughness')}")

    def _calculate_layer7d_switch(self, effect_data, calculated_characteristics):
         effect_type = effect_data.get('effect_type')
         for target_id in effect_data.get('affected_ids', []):
              if target_id in calculated_characteristics:
                  chars = calculated_characteristics[target_id]
                  # Only switch if it's a creature
                  if 'creature' in chars.get('card_types', []):
                      if effect_type == 'switch_pt':
                           # Check if power/toughness exist before switching
                           if 'power' in chars and 'toughness' in chars:
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
            # Remove from timestamps
            if effect_id in self.timestamps: del self.timestamps[effect_id]
            # Dependency cleanup removed:
            # self.dependencies.pop(effect_id, None)
            # for dep_list in self.dependencies.values(): ...
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
    
    def remove_effects_by_source(self, source_id_to_remove, effect_description_contains=None):
        """Remove all continuous effects originating from a specific source."""
        ids_to_remove = []
        removed_count = 0 # Track number removed

        for layer_num in range(1, 8):
             effects_pool = []
             is_sublayer = False
             if layer_num == 7: # Handle sublayers
                 is_sublayer = True
                 for sublayer in self.layers[7]:
                     effects_pool.append((sublayer, self.layers[7][sublayer]))
             else: # Handle regular layers
                 effects_pool.append((None, self.layers.get(layer_num, [])))

             for sublayer_key, effects_list in effects_pool:
                  # Check effects in the current list
                  for eid, data in effects_list:
                       matches_source = data.get('source_id') == source_id_to_remove
                       matches_desc = (not effect_description_contains or
                                       (effect_description_contains in data.get('description','').lower()))

                       if matches_source and matches_desc:
                            ids_to_remove.append((layer_num, sublayer_key, eid))

        # Perform removal
        if ids_to_remove:
             for layer_num, sublayer_key, eid in ids_to_remove:
                  # Find and remove the effect (handle layer 7 sublayers)
                  removed_this_pass = False
                  if layer_num == 7 and sublayer_key:
                      initial_len = len(self.layers[7][sublayer_key])
                      self.layers[7][sublayer_key] = [(e_id, data) for e_id, data in self.layers[7][sublayer_key] if e_id != eid]
                      if len(self.layers[7][sublayer_key]) < initial_len: removed_this_pass = True
                  elif 1 <= layer_num <= 6:
                      initial_len = len(self.layers[layer_num])
                      self.layers[layer_num] = [(e_id, data) for e_id, data in self.layers[layer_num] if e_id != eid]
                      if len(self.layers[layer_num]) < initial_len: removed_this_pass = True

                  # Cleanup associated data if removed successfully
                  if removed_this_pass:
                      removed_count += 1
                      if eid in self.timestamps: del self.timestamps[eid]
                      if eid in self.dependencies: del self.dependencies[eid]
                      for dep_list in self.dependencies.values():
                           if eid in dep_list: dep_list.remove(eid)
                      logging.debug(f"Removed effect {eid} from source {source_id_to_remove} (Layer {layer_num}{'/'+sublayer_key if sublayer_key else ''})")

             if removed_count > 0:
                  self.invalidate_cache() # Invalidate only if something was removed

        return removed_count # Return how many effects were removed
    
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