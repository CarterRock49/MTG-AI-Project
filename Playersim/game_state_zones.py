"""Zone queries and card movement between zones.

Extracted from game_state.py. This module defines behavior only (a mixin);
all state lives on GameState itself, which composes every mixin.
"""

import random
import logging
from collections import defaultdict


class GameStateZonesMixin:
    """Zone queries and card movement between zones."""

    # Empty slots: preserves GameState's __slots__ semantics (no instance __dict__).
    __slots__ = ()

    def move_card(self, card_id, from_player, from_zone, to_player, to_zone, cause=None, context=None):
        """Move a card between zones, applying replacement effects and triggering abilities, handling Madness, Offspring, Impending."""
        if context is None: context = {}
        card = self._safe_get_card(card_id)
        card_name = getattr(card, 'name', f"Card {card_id}") if card else f"Card {card_id}"
        original_from_zone = from_zone # Track for LTB specifically

        # --- Zone Validation / Implicit Zones ---
        # ... (Keep existing validation logic) ...
        source_list = None
        actual_from_zone = from_zone
        if from_zone == "stack_implicit": actual_from_zone = "stack"; source_list = [] # Card data exists, just not in player list yet
        elif from_zone == "library_implicit": actual_from_zone = "library"; source_list = []
        elif from_zone == "hand_implicit": actual_from_zone = "hand"; source_list = []
        elif from_zone == "nonexistent_zone": actual_from_zone = "nonexistent"; source_list = [] # For tokens entering
        elif from_player is None: # Moving from a game-level zone (e.g., phased_out)
             container = getattr(self, actual_from_zone, None)
             if container is not None and card_id in container: source_list = container
             else: logging.warning(f"Cannot move {card_name}: Invalid global source zone '{actual_from_zone}'."); return False
        else: # Standard player zone
             source_list = from_player.get(actual_from_zone)
             if source_list is None: logging.warning(f"Cannot move {card_name}: Invalid source zone '{actual_from_zone}' for player."); return False
             if card_id not in source_list: logging.warning(f"Cannot move {card_name}: Not found in {from_player['name']}'s {actual_from_zone}."); return False


        # --- Replacements ---
        # ... (Keep existing replacement effect handling) ...
        final_destination_player = to_player
        final_destination_zone = to_zone
        event_context = {'card_id': card_id, 'card': card, 'from_player': from_player, 'from_zone': actual_from_zone, 'to_player': to_player, 'to_zone': to_zone, 'cause': cause, **context }
        prevented = False
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            # Check LEAVE zone replacements first
            leave_event = f"LEAVE_{actual_from_zone.upper()}"
            modified_leave_ctx, replaced_leave = self.replacement_effects.apply_replacements(leave_event, event_context.copy())
            if replaced_leave:
                 event_context.update(modified_leave_ctx); final_destination_player = event_context.get('to_player'); final_destination_zone = event_context.get('to_zone'); prevented = event_context.get('prevented', False)
                 logging.debug(f"Leave replacement applied for {card_name}: New Dest: {final_destination_zone}, Prevented: {prevented}")
            # Check ENTER zone replacements (only if not prevented)
            if not prevented:
                 enter_event = f"ENTER_{final_destination_zone.upper()}" if final_destination_zone else None
                 if enter_event:
                     modified_enter_ctx, replaced_enter = self.replacement_effects.apply_replacements(enter_event, event_context.copy())
                     if replaced_enter:
                          final_destination_player = modified_enter_ctx.get('to_player'); final_destination_zone = modified_enter_ctx.get('to_zone'); prevented = modified_enter_ctx.get('prevented', False)
                          # Carry over ETB modifiers like 'tapped' or 'counters'
                          if 'enters_tapped' in modified_enter_ctx: event_context['enters_tapped'] = modified_enter_ctx['enters_tapped']
                          if 'enter_counters' in modified_enter_ctx: event_context.setdefault('enter_counters', []).extend(modified_enter_ctx['enter_counters'])
                          if 'as_enters_choice_needed' in modified_enter_ctx: event_context['as_enters_choice_needed'] = modified_enter_ctx['as_enters_choice_needed']
                          logging.debug(f"Enter replacement applied for {card_name}: Final Dest: {final_destination_zone}, Prevented: {prevented}")

        if prevented:
            logging.debug(f"Movement of {card_name} from {actual_from_zone} to {final_destination_zone} prevented.")
            return False # Movement stopped

        # --- Perform Actual Move ---
        # ... (Keep existing removal logic) ...
        removed_successfully = False
        if source_list is not None and original_from_zone not in ["stack_implicit", "library_implicit", "hand_implicit", "nonexistent_zone"]:
             source_list_live = None
             if from_player: source_list_live = from_player.get(actual_from_zone)
             else: source_list_live = getattr(self, actual_from_zone, None)

             if source_list_live is not None:
                 if isinstance(source_list_live, list) and card_id in source_list_live: source_list_live.remove(card_id); removed_successfully = True
                 elif isinstance(source_list_live, set) and card_id in source_list_live: source_list_live.discard(card_id); removed_successfully = True
                 elif isinstance(source_list_live, dict) and card_id in source_list_live: del source_list_live[card_id]; removed_successfully = True

             if not removed_successfully:
                 logging.error(f"CRITICAL: Failed to remove {card_name} from {actual_from_zone} even after validation.")
                 # State is inconsistent, cannot proceed safely
                 return False
        else: removed_successfully = True # Implicit removal assumed


        # --- 2. LTB Cleanup/Triggers (Only if removed from battlefield) ---
        # ... (Keep existing LTB logic) ...
        if actual_from_zone == "battlefield" and from_player:
            ltb_trigger_context = { 'controller': from_player, 'from_zone': actual_from_zone, 'to_zone': final_destination_zone, 'cause': cause, **context }
            self.trigger_ability(card_id, "LEAVE_BATTLEFIELD", ltb_trigger_context)
            logging.debug(f"Cleaning up state for {card_name} ({card_id}) leaving battlefield.")
            # Remove tracked statuses
            from_player.get("tapped_permanents", set()).discard(card_id)
            from_player.get("entered_battlefield_this_turn", set()).discard(card_id)
            keys_to_remove = [key for key in self.exhaust_ability_used if key[0] == card_id]
            if keys_to_remove: logging.debug(f"Clearing exhaust state for {card_name}."); [self.exhaust_ability_used.pop(k) for k in keys_to_remove]
            # Remove attachments TO this card and attachments OF this card
            attachments = from_player.get("attachments")
            if attachments:
                attachments.pop(card_id, None) # Remove what this card is attached to
                for att_id, target_id in list(attachments.items()): # Remove auras/equip attached TO this card
                    if target_id == card_id: del attachments[att_id]
            # Clear counters stored on player dicts (old system?)
            if hasattr(from_player, 'loyalty_counters'): from_player['loyalty_counters'].pop(card_id, None)
            if hasattr(from_player, 'damage_counters'): from_player['damage_counters'].pop(card_id, None)
            if hasattr(from_player, 'deathtouch_damage'): from_player.get('deathtouch_damage', {}).pop(card_id, None)
            # Clear counters stored on game state dicts
            if hasattr(self, 'saga_counters'): self.saga_counters.pop(card_id, None)
            if hasattr(self, 'battle_cards'): self.battle_cards.pop(card_id, None)
            # Clear other statuses
            if hasattr(from_player, 'regeneration_shields'): from_player['regeneration_shields'].discard(card_id)
            if hasattr(from_player, 'mutation_stacks') and card_id in from_player['mutation_stacks']: del from_player['mutation_stacks'][card_id]
            # Unregister effects originating from this card
            if self.layer_system: self.layer_system.remove_effects_by_source(card_id)
            if self.replacement_effects: self.replacement_effects.remove_effects_by_source(card_id)
            if self.ability_handler: self.ability_handler.unregister_card_abilities(card_id)
            # Reset card state itself (e.g., face-down)
            if card and hasattr(card, 'reset_state_on_zone_change'): card.reset_state_on_zone_change()


        # --- 3. Add to destination zone ---
        # ... (Keep existing destination logic) ...
        destination_list = final_destination_player.get(final_destination_zone)
        if destination_list is None: logging.error(f"Invalid destination zone '{final_destination_zone}'."); return False
        # Avoid duplicates, important for sets
        if card_id not in destination_list:
             if isinstance(destination_list, list): destination_list.append(card_id)
             elif isinstance(destination_list, set): destination_list.add(card_id)
             elif isinstance(destination_list, dict): destination_list[card_id] = True # Example for dict zone
             else: logging.error(f"Dest zone '{final_destination_zone}' not list/set/dict."); return False
        logging.debug(f"Moved {card_name} from {from_player['name'] if from_player else actual_from_zone} to {final_destination_player['name']}'s {final_destination_zone}")

        # --- 4. Trigger ENTER Abilities & Handle ETB Effects ---
        # --- UPDATED BLOCK ---
        enter_trigger_context = {'controller': final_destination_player, 'from_zone': actual_from_zone, 'to_zone': final_destination_zone, 'cause': cause, **event_context } # Pass merged context

        if final_destination_zone == "battlefield":
            # --- Standard ETB Setup ---
            final_destination_player.setdefault("entered_battlefield_this_turn", set()).add(card_id)
            etb_tapped_from_text = (hasattr(card, 'oracle_text') and "enters the battlefield tapped" in card.oracle_text.lower())
            enters_tapped = event_context.get('enters_tapped', False) or etb_tapped_from_text
            if enters_tapped: final_destination_player.setdefault("tapped_permanents", set()).add(card_id)
            if card and 'saga' in getattr(card,'subtypes',[]): self.add_counter(card_id, "lore", 1)
            if card and 'planeswalker' in getattr(card,'card_types',[]):
                base_loyalty = getattr(card, 'loyalty', 0)
                final_destination_player.setdefault("loyalty_counters", {})[card_id] = base_loyalty
            if card and 'battle' in getattr(card,'type_line','').lower():
                base_defense = getattr(card, 'defense', 0)
                self.battle_cards = getattr(self, 'battle_cards', {}); self.battle_cards[card_id] = base_defense
            etb_counters = event_context.get('enter_counters')
            if etb_counters and isinstance(etb_counters, list):
                for info in etb_counters: self.add_counter(card_id, info['type'], info['count'])

            # --- Impending ETB Handling ---
            cast_for_impending = context.get('cast_for_impending', False)
            if cast_for_impending and card:
                logging.debug(f"Applying Impending ETB effects for {card_name}")
                # 1. Add Time Counters
                n_value = getattr(card, 'impending_n', 1) # Get N value from card
                if n_value > 0:
                    self.add_counter(card_id, 'time', n_value)
                # 2. Track Impending Status
                self.impending_cards = getattr(self, 'impending_cards', {})
                self.impending_cards[card_id] = {'initial_n': n_value}
                # 3. Apply Static "Isn't a Creature" Effects via Layer System
                if self.layer_system:
                     # Layer 4: Remove Creature Type
                     self._register_impending_static_effect(card_id, final_destination_player, layer=4, effect_type='remove_type', effect_value=['Creature'])
                     # Layer 7b: Set P/T to 0/0 (Implicit by rule 208.3 for non-creatures, but can enforce)
                     self._register_impending_static_effect(card_id, final_destination_player, layer=7, sublayer='b', effect_type='set_pt', effect_value=(0, 0))
                     # Re-apply layers immediately after registering these effects
                     self.layer_system.apply_all_effects()
            # --- End Impending ETB ---

            # --- Register Abilities FIRST ---
            if card and self.ability_handler: self.ability_handler.register_card_abilities(card_id, final_destination_player)

            # --- Record Offspring Cost Payment *BEFORE* triggering ETB ---
            # The trigger condition will check this context map for the specific card ID instance.
            paid_offspring = context.get('paid_offspring', False)
            if paid_offspring:
                self._offspring_cost_paid_context = getattr(self, '_offspring_cost_paid_context', {})
                self._offspring_cost_paid_context[card_id] = True # Simple flag is enough
                logging.debug(f"Recorded offspring cost payment context for {card_name} ({card_id}) entering battlefield.")
            # --- End Offspring Recording ---

            # Handle "As enters" choice setup (must happen BEFORE ETB triggers)
            if event_context.get('as_enters_choice_needed'):
                 logging.debug(f"Entering CHOICE phase for 'As {card_name} enters...'")
                 if self.phase not in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                     self.previous_priority_phase = self.phase
                 self.phase = self.PHASE_CHOOSE
                 self.choice_context = {
                     'type': f"as_enters_{event_context['as_enters_choice_needed']}",
                     'player': final_destination_player, 'card_id': card_id,
                     'source_id': event_context.get('as_enters_source_id', card_id),
                     'resolved': False
                 }
                 self.priority_player = final_destination_player
                 self.priority_pass_count = 0
                 logging.info(f"'As enters' choice required for {card_name}. Waiting.")
            else:
                # --- Trigger ETB Abilities (Only if no choice needed immediately) ---
                # Add card_id to the enter trigger context now that it's fully on the BF
                enter_trigger_context['card_id'] = card_id
                self.trigger_ability(card_id, "ENTERS_BATTLEFIELD", enter_trigger_context)
                if card and 'land' in getattr(card,'card_types',[]):
                     self.trigger_ability(None, "LANDFALL", enter_trigger_context)
                     self.trigger_ability(card_id, "LANDFALL_SELF", enter_trigger_context)

            # Handle Aura attachment *after* ETB setup (and triggers queued/resolved?) - Queue first is safer.
            if card and 'aura' in getattr(card, 'subtypes', []):
                 self._resolve_aura_attachment(card_id, final_destination_player, event_context) # Pass original event context

            # --- Offspring Cost Cleanup (Needs careful placement) ---
            # Clean up offspring context map *after* the ETB trigger for this specific card
            # has been processed. Best handled maybe during SBA check or turn end?
            # For simplicity, let's leave the cleanup task elsewhere, e.g., after trigger resolution.
            # *** Moved from Ability Handler: Cleanup after resolution (potentially in resolve_ability or main loop) ***
            # Example check during trigger resolution (if cost was checked there):
            # if ability._is_offspring_etb_trigger and card_id in self._offspring_cost_paid_context:
            #     del self._offspring_cost_paid_context[card_id]
            # Here, just ensure the context was set correctly above.

        # --- Enter Non-Battlefield Zone Triggers ---
        else: # Enters GY, Hand, Exile, Library etc.
             trigger_name = f"ENTER_{final_destination_zone.upper()}"
             self.trigger_ability(card_id, trigger_name, enter_trigger_context)
             if final_destination_zone == "graveyard":
                 if not hasattr(self, 'cards_to_graveyard_this_turn'): self.cards_to_graveyard_this_turn = defaultdict(list)
                 self.cards_to_graveyard_this_turn[self.turn].append(card_id)
                 if actual_from_zone == "battlefield": # From BF to GY = Dies
                     # Trigger "dies" ability
                     dies_context = {'controller': from_player, 'from_zone': actual_from_zone, 'to_zone': final_destination_zone, 'cause': cause, **context}
                     self.trigger_ability(card_id, "DIES", dies_context)
                     self.gravestorm_count = getattr(self, 'gravestorm_count', 0) + 1
            # --- END UPDATE ---


        # --- 5. Post-Move Cleanup ---
        # ... (Keep existing token/madness/etc. cleanup) ...
        card_was_token = hasattr(card, 'is_token') and card.is_token # Check *before* potential reset
        if card_was_token and final_destination_zone != "battlefield":
             # Remove from destination zone list/set
             dest_list_live = final_destination_player.get(final_destination_zone)
             if dest_list_live:
                 if isinstance(dest_list_live, list) and card_id in dest_list_live: dest_list_live.remove(card_id)
                 elif isinstance(dest_list_live, set) and card_id in dest_list_live: dest_list_live.discard(card_id)
             # Remove from card_db
             if card_id in self.card_db:
                  del self.card_db[card_id]
                  logging.debug(f"Token {card_name} ({card_id}) ceased to exist after moving to {final_destination_zone}.")
             # Remove from player's token tracking if present
             if hasattr(final_destination_player, "tokens") and card_id in final_destination_player["tokens"]:
                  final_destination_player["tokens"].remove(card_id)

        # Clear Madness opportunity if card moved FROM exile via non-Madness means
        if actual_from_zone == "exile" and not context.get("is_madness_cast", False) and \
           getattr(self, 'madness_cast_available', None) and self.madness_cast_available.get('card_id') == card_id:
             logging.debug(f"Clearing Madness opportunity for {card_name} as it moved from exile by other means.")
             self.madness_cast_available = None

        # --- Re-check layers if moved TO battlefield and is Impending ---
        # (Already applied earlier in this block)
        # if final_destination_zone == "battlefield" and card and getattr(card,'is_impending',False) and self.layer_system:
        #     self.layer_system.apply_all_effects()

        return True

    def bottom_card(self, player, hand_index_to_bottom):
        """
        Handle bottoming a card from hand during mulligan resolution.
        Handles switching turns or ending the mulligan phase. (Revised State Assignment v4)
        """
        if not self.bottoming_in_progress or self.bottoming_player != player:
            logging.warning("Invalid state to bottom card.")
            return False
        # Validate index before popping
        if not (0 <= hand_index_to_bottom < len(player.get("hand", []))): # Use get for safety
            logging.warning(f"Invalid hand index {hand_index_to_bottom} to bottom.")
            return False

        player_id_str = 'p1' if player == self.p1 else 'p2'
        opponent = self.p2 if player == self.p1 else self.p1
        opponent_id_str = 'p2' if player == self.p1 else 'p1'

        # Move the card from hand to bottom of library
        card_id = player["hand"].pop(hand_index_to_bottom)
        player.setdefault("library", []).append(card_id) # Ensure library exists and append
        card = self._safe_get_card(card_id)
        logging.debug(f"{player['name']} bottomed {getattr(card, 'name', card_id)}.")
        self.bottoming_count += 1 # Increment count for THIS player

        # --- Check if THIS player's bottoming requirement is met ---
        if self.bottoming_count >= self.cards_to_bottom:
            logging.info(f"Bottoming complete for {player['name']}.")
            player['_bottoming_complete'] = True # Mark this player as done bottoming

            # --- Check Opponent's Status to Determine Next State ---
            opp_needs_to_bottom = opponent and opponent.get('_needs_to_bottom_next', False) # Check opponent exists
            opp_has_finished_bottoming = opponent and opponent.get('_bottoming_complete', False)

            if opp_needs_to_bottom and not opp_has_finished_bottoming:
                # Current player finished, but opponent still needs to bottom. Switch turns.
                logging.debug(f"Switching to {opponent['name']} for bottoming.")
                self.mulligan_player = None        # Ensure mulligan player remains None
                self.bottoming_player = opponent   # Assign opponent to act next
                self.bottoming_in_progress = True  # Stay in bottoming phase
                self.bottoming_count = 0           # Reset counter for opponent
                self.cards_to_bottom = min(self.mulligan_count.get(opponent_id_str, 0), len(opponent.get("hand", []))) # Determine count for opponent
                return True # State changed, bottoming action successful
            else:
                # Opponent doesn't need to bottom OR is already done bottoming. End mulligan phase.
                logging.debug("Opponent does not need to bottom or is finished. Ending mulligan phase.")
                self.bottoming_player = None       # Clear the acting player *before* ending phase
                self._end_mulligan_phase()        # Transition game state to start Turn 1
                return True # Bottoming action successful, phase ended
        else:
            # More cards needed from the *same* player.
            logging.debug(f"{player['name']} needs to bottom {self.cards_to_bottom - self.bottoming_count} more.")
            # Ensure the current player remains the bottoming_player to act again
            self.bottoming_player = player # <<<<<<<<<< ENSURE player is set to act again
            return True # Incremental bottoming action was successful

    def handle_miracle_draw(self, card_id, player):
        """
        Handle drawing a card with miracle, giving the player a chance to cast it for its miracle cost.
        
        Args:
            card_id: ID of the drawn card
            player: The player who drew the card
            
        Returns:
            bool: Whether the miracle was handled
        """
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text') or "miracle" not in card.oracle_text.lower():
            return False
                
        # Parse miracle cost
        import re
        match = re.search(r"miracle\s+([^\(]+)(?:\(|$)", card.oracle_text.lower())
        miracle_cost = match.group(1).strip() if match else None
        
        if not miracle_cost:
            logging.warning(f"Could not parse miracle cost for {card.name}")
            return False
                
        # Set up miracle window
        self.miracle_card = card_id
        self.miracle_cost = miracle_cost
        self.miracle_player = player
        
        # Track that this is the first card drawn this turn (to meet miracle conditions)
        if not hasattr(self, 'cards_drawn_this_turn'):
            self.cards_drawn_this_turn = {}
            
        player_key = "p1" if player == self.p1 else "p2"
        turn_key = self.turn
        if turn_key not in self.cards_drawn_this_turn:
            self.cards_drawn_this_turn[turn_key] = {}
        if player_key not in self.cards_drawn_this_turn[turn_key]:
            self.cards_drawn_this_turn[turn_key][player_key] = []
            
        # Check if this is the first card drawn this turn
        is_first_draw = len(self.cards_drawn_this_turn[turn_key].get(player_key, [])) == 0
        self.cards_drawn_this_turn[turn_key][player_key].append(card_id)
        
        # Only offer miracle if this is the first draw and player can afford
        if is_first_draw and hasattr(self, 'mana_system'):
            parsed_cost = self.mana_system.parse_mana_cost(miracle_cost)
            if self.mana_system.can_pay_mana_cost(player, parsed_cost):
                logging.debug(f"Miracle opportunity for {card.name}")
                
                # Set up the miracle state for action generation
                self.miracle_active = True
                self.miracle_card_id = card_id
                self.miracle_cost_parsed = parsed_cost
                
                # In a full implementation, we'd set a flag and let the agent choose 
                # whether to cast via miracle. For now, we'll just return True to
                # indicate the miracle was set up successfully.
                return True
        
        return False

    def surveil(self, player, count=1):
        """
        Implement the Surveil mechanic.
        Look at top N cards of library, put any number in graveyard and rest on top in any order.
        
        Args:
            player: Player dictionary
            count: Number of cards to surveil
            
        Returns:
            list: The cards that were surveiled
        """
        if not player["library"]:
            return []
            
        # Limit to number of cards in library
        count = min(count, len(player["library"]))
        
        if count <= 0:
            return []
        
        # Look at top N cards without removing them yet
        surveiled_cards = [player["library"][i] for i in range(count)]
        
        # Store the surveiling state for action generation
        self.surveil_in_progress = True
        self.cards_being_surveiled = surveiled_cards.copy()
        self.surveiling_player = player
        
        logging.debug(f"Started surveiling {count} cards - waiting for surveil actions")
        
        return surveiled_cards

    def scry(self, player, count=1):
        """
        Implement the Scry mechanic with better decision-making.
        Look at top N cards of library, put any number on bottom and rest on top in any order.
        
        Args:
            player: Player dictionary
            count: Number of cards to scry
            
        Returns:
            list: The cards that were scryed
        """
        if not player["library"]:
            return []
            
        # Limit to number of cards in library
        count = min(count, len(player["library"]))
        
        if count <= 0:
            return []
        
        # Look at top N cards without removing them yet
        scryed_cards = [player["library"][i] for i in range(count)]
        
        # Store the scrying state for action generation
        self.scry_in_progress = True
        self.scrying_cards = scryed_cards.copy()
        self.scrying_player = player
        self.scrying_tops = []
        self.scrying_bottoms = []
        
        logging.debug(f"Started scrying {count} cards - waiting for scry actions")
        
        return scryed_cards

    # --- Added method in GameState ---
    def perform_dredge(self, player, dredge_card_id):
        """Performs the dredge action after the player confirms."""
        dredge_info = getattr(self, 'dredge_pending', None)
        if not dredge_info or dredge_info['player'] != player or dredge_info['card_id'] != dredge_card_id:
            logging.warning("Invalid state for perform_dredge.")
            self.dredge_pending = None # Clear inconsistent state
            return False

        dredge_val = dredge_info['value']
        source_zone = dredge_info.get('source_zone', 'graveyard')

        # Double check card location and library size
        current_owner, current_zone = self.find_card_location(dredge_card_id)
        if current_owner != player or current_zone != source_zone:
            logging.warning(f"Dredge card {dredge_card_id} no longer in {player['name']}'s {source_zone}.")
            self.dredge_pending = None
            return False
        if len(player.get("library", [])) < dredge_val:
            logging.warning(f"Cannot dredge {dredge_card_id}: Not enough cards in library ({len(player['library'])}/{dredge_val}).")
            self.dredge_pending = None
            return False

        # Mill N cards
        milled_count = 0
        ids_to_mill = player["library"][:dredge_val]
        player["library"] = player["library"][dredge_val:] # Remove from library first

        for card_id_to_mill in ids_to_mill:
            # Use move_card to handle triggers for milling
            if self.move_card(card_id_to_mill, player, "library_implicit", player, "graveyard", cause="mill_dredge"):
                 milled_count += 1
            else:
                 logging.error(f"Failed to move {card_id_to_mill} to graveyard during dredge mill.")
                 # Should attempt to put back? State might be complex.

        # Return dredged card to hand
        success_move = self.move_card(dredge_card_id, player, source_zone, player, "hand", cause="dredge_return")

        # Clear pending state regardless of move success
        self.dredge_pending = None

        if success_move:
            card = self._safe_get_card(dredge_card_id)
            card_name = getattr(card, 'name', dredge_card_id)
            # Trigger DREDGED event
            self.trigger_ability(dredge_card_id, "DREDGED", {"controller": player, "milled": milled_count})
            logging.info(f"Performed dredge: Returned {card_name}, milled {milled_count}.")
            # Return to priority phase (since draw was replaced)
            self.phase = self.PHASE_PRIORITY
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0
            return True
        else:
            logging.error(f"Dredge failed during final move_card for {dredge_card_id}")
            # Attempt recovery? Put milled cards back? Very complex state.
            return False

    def _card_matches_criteria(self, card, criteria):
        """Basic check if card matches simple criteria."""
        if not card: return False
        types = getattr(card, 'card_types', [])
        subtypes = getattr(card, 'subtypes', [])
        type_line = getattr(card, 'type_line', '').lower()
        name = getattr(card, 'name', '').lower()

        if criteria == "any": return True
        if criteria == "basic land" and 'basic' in type_line and 'land' in type_line: return True
        if criteria == "land" and 'land' in type_line: return True
        if criteria in types: return True
        if criteria in subtypes: return True
        if criteria == name: return True
        # Add checks for colors, CMC, P/T if needed for more complex searches
        return False

    def search_library_and_choose(self, player, criteria, ai_choice_context=None):
        """Search library for a card matching criteria and let AI choose one."""
        matches = []
        indices_to_remove = []
        for i, card_id in enumerate(player["library"]):
            card = self._safe_get_card(card_id)
            if self._card_matches_criteria(card, criteria): # Uses GameState's helper now
                 matches.append(card_id)
                 indices_to_remove.append(i) # Store index along with card_id

        if not matches:
            logging.debug(f"Search failed: No '{criteria}' found in library.")
            if hasattr(self, 'shuffle_library'): self.shuffle_library(player) # Shuffle even on fail
            return None

        # AI Choice - Use CardEvaluator if available, else first match
        chosen_id = None
        if hasattr(self, 'card_evaluator') and self.card_evaluator:
             best_choice_id = None
             best_score = -float('inf')
             # Add turn and phase to context
             eval_context = {"current_turn": self.turn, "current_phase": self.phase, "goal": criteria}
             if ai_choice_context: eval_context.update(ai_choice_context)

             for card_id in matches:
                  score = self.card_evaluator.evaluate_card(card_id, "search_find", context_details=eval_context)
                  if score > best_score:
                       best_score = score
                       best_choice_id = card_id
             chosen_id = best_choice_id if best_choice_id is not None else (matches[0] if matches else None)
        elif matches:
            chosen_id = matches[0] # Simple: Choose first match

        # Remove chosen card from library and move to hand (default)
        if chosen_id:
             # Find index to remove (important if library changed during evaluation?)
             original_index = -1
             try:
                 # Iterate through stored indices
                 for i in indices_to_remove:
                     if player["library"][i] == chosen_id:
                         original_index = i
                         break
             except IndexError: # Handle case where library might have changed mid-search? Unlikely here.
                 logging.warning("Library changed during search? Cannot find index.")
                 pass # Fallback to just removing by value if index fails

             if original_index != -1:
                 player["library"].pop(original_index)
             else: # Fallback remove by value
                 if chosen_id in player["library"]: player["library"].remove(chosen_id)
                 else: logging.error("Chosen card vanished from library!"); chosen_id = None # Cannot proceed

        # Perform move and shuffle if card was successfully found and removed
        if chosen_id:
            target_zone = "hand" # Default target zone for search
            success_move = self.move_card(chosen_id, player, "library_implicit", player, target_zone, cause="search") # Use implicit source
            if not success_move: chosen_id = None # Move failed

        # Shuffle library after search
        if hasattr(self, 'shuffle_library'): self.shuffle_library(player)
        else: random.shuffle(player["library"])

        if chosen_id:
            logging.debug(f"Search found: Moved '{self._safe_get_card(chosen_id).name}' matching '{criteria}' to {target_zone}.")
        return chosen_id # Return ID of chosen card

    def shuffle_library(self, player):
        """Shuffles the player's library."""
        if player and "library" in player:
            random.shuffle(player["library"])
            logging.debug(f"{player['name']}'s library shuffled.")
            return True
        return False

    def venture(self, player):
        """Handle venture into the dungeon. Needs dungeon tracking."""
        if not hasattr(self, 'dungeons'):
             logging.warning("Venture called but dungeon system not implemented.")
             return False
        # TODO: Implement dungeon choice and room progression logic
        logging.debug("Venture placeholder.")
        return True

    def get_permanent_by_combined_index(self, combined_index):
        """Get permanent ID and owner by a combined index across both battlefields (P1 first)."""
        p1_bf_len = len(self.p1.get("battlefield", [])) # Use get for safety
        if 0 <= combined_index < p1_bf_len:
            card_id = self.p1["battlefield"][combined_index]
            return card_id, self.p1
        p2_bf_len = len(self.p2.get("battlefield", []))
        if p1_bf_len <= combined_index < p1_bf_len + p2_bf_len:
            card_id = self.p2["battlefield"][combined_index - p1_bf_len]
            return card_id, self.p2
        logging.warning(f"Invalid combined battlefield index: {combined_index}")
        return None, None # Return None if index is out of bounds

    def get_token_data_by_index(self, index):
        """Returns predefined token data for CREATE_TOKEN action."""
        # Example mapping - needs to be defined based on game needs
        token_map = {
            0: {"name": "Soldier", "type_line": "Token Creature — Soldier", "power": 1, "toughness": 1, "colors":[1,0,0,0,0]},
            1: {"name": "Spirit", "type_line": "Token Creature — Spirit", "power": 1, "toughness": 1, "colors":[1,0,0,0,0], "keywords":[1,0,0,0,0,0,0,0,0,0,0]}, # Flying
            2: {"name": "Goblin", "type_line": "Token Creature — Goblin", "power": 1, "toughness": 1, "colors":[0,0,0,1,0]},
            3: {"name": "Treasure", "type_line": "Token Artifact — Treasure", "card_types":["artifact"], "subtypes":["Treasure"], "oracle_text": "{T}, Sacrifice this artifact: Add one mana of any color."},
            4: {"name": "Clue", "type_line": "Token Artifact — Clue", "card_types": ["artifact"], "subtypes":["Clue"], "oracle_text": "{2}, Sacrifice this artifact: Draw a card."}
        }
        return token_map.get(index)

    def put_on_top(self, player, card_idx):
        """
        Put a card from hand on top of library.
        
        Args:
            player: Player dictionary
            card_idx: Index of card in hand to put on top
            
        Returns:
            bool: Whether the operation was successful
        """
        if 0 <= card_idx < len(player["hand"]):
            card_id = player["hand"].pop(card_idx)
            player["library"].insert(0, card_id)
            
            card = self._safe_get_card(card_id)
            card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
            logging.debug(f"Put {card_name} on top of library")
            return True
        
        logging.warning(f"Invalid card index {card_idx} for put_on_top")
        return False

    def put_on_bottom(self, player, card_idx):
        """
        Put a card from hand on bottom of library.
        
        Args:
            player: Player dictionary
            card_idx: Index of card in hand to put on bottom
            
        Returns:
            bool: Whether the operation was successful
        """
        if 0 <= card_idx < len(player["hand"]):
            card_id = player["hand"].pop(card_idx)
            player["library"].append(card_id)
            
            card = self._safe_get_card(card_id)
            card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
            logging.debug(f"Put {card_name} on bottom of library")
            return True
            
        logging.warning(f"Invalid card index {card_idx} for put_on_bottom")
        return False

    def reveal_top(self, player, count=1):
        """
        Reveal the top N cards of library without changing their order.
        
        Args:
            player: Player dictionary
            count: Number of cards to reveal
            
        Returns:
            list: The revealed card objects
        """
        if not player["library"]:
            logging.debug("Cannot reveal - library is empty")
            return []
            
        # Limit to number of cards in library
        count = min(count, len(player["library"]))
        revealed_cards = []
        
        # Get top cards without changing their order
        for i in range(count):
            card_id = player["library"][i]
            card = self._safe_get_card(card_id)
            revealed_cards.append(card)
            
            card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
            logging.debug(f"Revealed {card_name} from top of library")
            
        return revealed_cards

    def clash(self, player1, player2):
        """Perform clash."""
        # Ensure players are valid and have libraries
        if not player1 or not player2 or not player1.get("library") or not player2.get("library"):
             logging.warning("Clash cannot occur: Invalid players or empty library.")
             return None

        card1_id = player1["library"].pop(0)
        card2_id = player2["library"].pop(0)
        card1 = self._safe_get_card(card1_id)
        card2 = self._safe_get_card(card2_id)
        cmc1 = getattr(card1, 'cmc', -1) if card1 else -1
        cmc2 = getattr(card2, 'cmc', -1) if card2 else -1

        name1 = getattr(card1,'name','nothing')
        name2 = getattr(card2,'name','nothing')
        logging.debug(f"Clash: {player1['name']} revealed {name1} (CMC {cmc1}), {player2['name']} revealed {name2} (CMC {cmc2})")

        # AI Choice needed for top/bottom. Simple: put back on top for now.
        # Store revealed cards temporarily for potential choice phase
        self.clash_context = {'p1': (card1_id, card1), 'p2': (card2_id, card2)}
        # TODO: Implement PHASE_CHOOSE for clash result destination
        # Temporary: Put back on top
        if card1_id: player1["library"].insert(0, card1_id)
        if card2_id: player2["library"].insert(0, card2_id)

        # Trigger clash event
        self.trigger_ability(None, "CLASHED", {"player1": player1, "player2": player2, "card1_id": card1_id, "card2_id": card2_id})

        # Return winning player (or None for draw/neither)
        if cmc1 > cmc2:
            logging.debug(f"Clash result: {player1['name']} wins.")
            return player1
        elif cmc2 > cmc1:
            logging.debug(f"Clash result: {player2['name']} wins.")
            return player2
        else:
            logging.debug("Clash result: Draw.")
            return None

    def _find_card_in_hand(self, player, identifier):
        """Finds a card ID in the player's hand using index or ID string."""
        if isinstance(identifier, int):
             if 0 <= identifier < len(player["hand"]):
                  return player["hand"][identifier]
        elif isinstance(identifier, str):
             if identifier in player["hand"]:
                  return identifier
        return None

    def _find_permanent_id(self, player, identifier):
        """Finds a permanent ID on the player's battlefield using index or ID string."""
        if isinstance(identifier, int):
             if 0 <= identifier < len(player["battlefield"]):
                  return player["battlefield"][identifier]
        elif isinstance(identifier, str):
             # Check if it's a direct ID
             if identifier in player["battlefield"]:
                  return identifier
             # Could potentially add lookup by name here if needed, but ID/index preferred
        return None

    def explore(self, player, creature_id):
        """Perform explore for a creature."""
        if not player or "library" not in player or not player["library"]:
            logging.debug("Explore: Library empty.")
            return False # Nothing to reveal

        top_card_id = player["library"].pop(0) # Remove from top
        top_card = self._safe_get_card(top_card_id)
        if not top_card: # Should not happen if library is just IDs
            logging.error(f"Explore failed: Invalid card ID {top_card_id} found in library.")
            return False
        card_name = getattr(top_card,'name','Unknown Card')
        exploring_creature = self._safe_get_card(creature_id)
        exploring_creature_name = getattr(exploring_creature, 'name', creature_id) if exploring_creature else creature_id
        logging.debug(f"Exploring (via {exploring_creature_name}): Revealed {card_name}")

        is_land = 'land' in getattr(top_card, 'type_line', '').lower()

        if is_land:
            success_move = self.move_card(top_card_id, player, "library_implicit", player, "hand") # Use implicit source zone
            if success_move:
                 logging.debug(f"Explore hit a land ({card_name}), put into hand.")
                 self.trigger_ability(creature_id, "EXPLORED_LAND", {"revealed_card_id": top_card_id})
            else:
                 player["library"].insert(0, top_card_id) # Put back if move fails? Rare.
            return success_move
        else:
            # Put +1/+1 counter on exploring creature
            success_counter = self.add_counter(creature_id, "+1/+1", 1)
            if success_counter: logging.debug(f"Explore hit nonland, put +1/+1 counter on {exploring_creature_name}")

            # AI choice: top or graveyard? Use CardEvaluator if available.
            put_in_gy = True # Default to graveyard
            if self.card_evaluator:
                 value = self.card_evaluator.evaluate_card(top_card_id, "explore_nonland")
                 if value > 0.6: # Threshold to keep non-land on top
                      put_in_gy = False
            elif getattr(top_card, 'cmc', 0) >= 4: # Simple heuristic: Keep expensive non-lands
                put_in_gy = False

            if put_in_gy:
                 success_move = self.move_card(top_card_id, player, "library_implicit", player, "graveyard")
                 if success_move: logging.debug(f"Explore: Put nonland {card_name} into graveyard.")
                 else: player["library"].insert(0, top_card_id) # Put back if move fails
                 self.trigger_ability(creature_id, "EXPLORED_NONLAND_GY", {"revealed_card_id": top_card_id})
                 return success_move
            else:
                 player["library"].insert(0, top_card_id) # Put back on top
                 logging.debug(f"Explore: Kept nonland {card_name} on top.")
                 self.trigger_ability(creature_id, "EXPLORED_NONLAND_TOP", {"revealed_card_id": top_card_id})
                 return True

    def _find_card_controller(self, card_id):
        """Find which player controls a card currently on the battlefield."""
        for p in [self.p1, self.p2]:
            if card_id in p.get("battlefield",[]):
                return p
        return None

    def _get_permanent_at_idx(self, player, index):
         """Safely get permanent from battlefield index."""
         if index < len(player["battlefield"]):
             return self._safe_get_card(player["battlefield"][index])
         return None

    def find_card_location(self, card_id):
        """
        Find which player controls a card and in which zone it is.
        Also handles finding the controller of the source of an effect on the stack.

        Args:
            card_id: ID of the card or stack item source to locate

        Returns:
            tuple: (player_object, zone_string) or (None, None) if not found
        """
        zones = ["battlefield", "hand", "graveyard", "exile", "library"]
        special_zones_map = {
             "adventure_cards": "adventure_zone", "phased_out": "phased_out",
             "foretold_cards": "foretold_zone", "suspended_cards": "suspended",
             "unearthed_cards": "unearthed_zone", # Add other special tracking if needed
             "morphed_cards": "face_down_zone", # Represent face-down state
             "manifested_cards": "face_down_zone",
             "commander_zone": "command", # Standardize command zone name
             "companion": "companion_zone",
        }

        # Check standard zones for both players
        for player in [self.p1, self.p2]:
            if not player: continue # Safety check
            for zone in zones:
                if zone in player and isinstance(player[zone], (list, set)) and card_id in player[zone]:
                    return player, zone

            # Check player-specific special zones (like revealed hand?) - Not standard MTG, skip for now.

        # Check game-level special zones / tracking dicts
        for attr_name, zone_name in special_zones_map.items():
            if hasattr(self, attr_name):
                 container = getattr(self, attr_name)
                 if isinstance(container, set) and card_id in container:
                     # Find original owner/controller if possible, default to p1
                     owner = self._find_card_owner_fallback(card_id) # Use fallback owner finder
                     return owner, zone_name
                 elif isinstance(container, dict) and card_id in container:
                      # Check if the dict value stores the controller
                      entry = container[card_id]
                      controller = entry.get("controller") if isinstance(entry, dict) else None
                      if controller: return controller, zone_name
                      # Fallback owner find
                      owner = self._find_card_owner_fallback(card_id)
                      return owner, zone_name

        # Check stack (Handles spells and abilities)
        for item in self.stack:
            # Stack items are tuples: (type, source_id, controller, context)
            if isinstance(item, tuple) and len(item) >= 3 and item[1] == card_id:
                 return item[2], "stack" # Return the controller and "stack" zone

        # If not found in any common zone
        # logging.debug(f"Card/Source ID {card_id} not found in any tracked zone.")
        return None, None 

    # Add a helper to find original owner if controller isn't readily available
    def _find_card_owner_fallback(self, card_id):
        """Fallback to find card owner based on original deck assignment or DB."""
        # Check original decks if tracked
        if hasattr(self, 'original_p1_deck') and card_id in self.original_p1_deck:
             return self.p1
        if hasattr(self, 'original_p2_deck') and card_id in self.original_p2_deck:
             return self.p2
        # Last resort - default to p1 if owner ambiguous
        return self.p1

    # Consolidate get_card_controller (use find_card_location)
    def get_card_controller(self, card_id):
        """Find the controller of a card currently on the battlefield."""
        player, zone = self.find_card_location(card_id)
        if zone == "battlefield":
             return player
        # Consider returning controller even if not on battlefield?
        # Depends on rules context. For most purposes, only battlefield controller matters.
        # If you need owner regardless of zone, use _find_card_owner_fallback or similar.
        return None
