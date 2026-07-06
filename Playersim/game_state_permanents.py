"""Battlefield permanents: status, counters, attachments, tokens, and keyword mechanics.

Extracted from game_state.py. This module defines behavior only (a mixin);
all state lives on GameState itself, which composes every mixin.
"""

import random
import logging
import numpy as np
import copy
from .card import Card
import re


class GameStatePermanentsMixin:
    """Battlefield permanents: status, counters, attachments, tokens, and keyword mechanics."""

    # Empty slots: preserves GameState's __slots__ semantics (no instance __dict__).
    __slots__ = ()

    def apply_temporary_control(self, card_id, new_controller):
        """
        Grant temporary control of a card until end of turn.
        
        Args:
            card_id: ID of the card to control temporarily.
            new_controller: The player dictionary who will temporarily control the card.
        
        Returns:
            bool: True if the effect is applied successfully.
        """
        original_controller = self.find_card_location(card_id)
        if original_controller is None:
            logging.warning(f"Temporary control: Original owner not found for card {card_id}.")
            return False
        # Record the original controller if not already stored
        if card_id not in self.temp_control_effects:
            self.temp_control_effects[card_id] = original_controller
        # Remove the card from its current controller's battlefield
        for player in [self.p1, self.p2]:
            if card_id in player["battlefield"]:
                player["battlefield"].remove(card_id)
        # Add the card to the new controller's battlefield
        new_controller["battlefield"].append(card_id)
        logging.debug(f"Temporary control: {new_controller['name']} now controls {self._safe_get_card(card_id).name} until end of turn.")
        return

    def get_party_count(self, battlefield):
        """
        Calculate party count (Clerics, Rogues, Warriors, and Wizards).
        Used for the Party mechanic from Zendikar Rising.
        
        Args:
            battlefield: List of card IDs on battlefield to check for party members
            
        Returns:
            int: Number of different party classes (max 4)
        """
        party_classes = {"cleric", "rogue", "warrior", "wizard"}
        found_classes = set()
        
        for card_id in battlefield:
            card = self._safe_get_card(card_id)
            if not card or not hasattr(card, 'subtypes'):
                continue
                
            # Check if card is on battlefield and is a creature
            if hasattr(card, 'card_types') and 'creature' in card.card_types:
                # Check for party classes
                card_subtypes = {subtype.lower() for subtype in card.subtypes}
                found_party_classes = party_classes.intersection(card_subtypes)
                found_classes.update(found_party_classes)
        
        # Return the count of different party classes (max 4)
        return min(len(found_classes), 4)

    def get_all_creatures(self, player=None):
        """
        Get IDs of all creatures on the battlefield.
        If player is specified, only returns creatures that player controls.
        
        Args:
            player: Optional player to filter by controller
            
        Returns:
            list: IDs of creature cards
        """
        creature_ids = []
        
        if player:
            players = [player]
        else:
            players = [self.p1, self.p2]
            
        for p in players:
            for card_id in p.get("battlefield", []):
                card = self._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    creature_ids.append(card_id)
                    
        return creature_ids

    def _revert_temporary_control(self):
        """
        Revert any temporary control effects, returning cards to their original controllers.
        This should be called at the end of the turn.
        """
        for card_id, original_controller in list(self.temp_control_effects.items()):
            current_controller = self.find_card_location(card_id)
            if current_controller and current_controller != original_controller:
                # Remove from current controller's battlefield
                if card_id in current_controller["battlefield"]:
                    current_controller["battlefield"].remove(card_id)
                # Return card to original controller's battlefield
                original_controller["battlefield"].append(card_id)
                logging.debug(f"Temporary control: Reverted control of {self._safe_get_card(card_id).name} back to {original_controller['name']}.")
            # Remove the effect record
            del self.temp_control_effects[card_id]

    def add_defense_counter(self, card_id, count=1):
        """
        Add defense counters to a battle card.
        
        Args:
            card_id: ID of the battle card
            count: Number of counters to add (can be negative to remove)
            
        Returns:
            bool: Success status
        """
        # Find the card owner
        card_owner = None
        for player in [self.p1, self.p2]:
            if card_id in player["battlefield"]:
                card_owner = player
                break
        
        if not card_owner:
            logging.warning(f"Cannot add defense counter to card {card_id} - not on battlefield")
            return False
        
        # Get the card
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'type_line') or 'battle' not in card.type_line.lower():
            logging.warning(f"Card {card_id} is not a battle card")
            return False
        
        # Initialize battle defense tracking
        if not hasattr(self, 'battle_cards'):
            self.battle_cards = {}
        
        # Add or remove defense counters
        current_defense = self.battle_cards.get(card_id, 0)
        new_defense = max(0, current_defense + count)  # Cannot go below 0
        self.battle_cards[card_id] = new_defense
        
        logging.debug(f"Changed defense counters on battle card {card.name} by {count}, now has {new_defense}")
        
        # If defense reaches 0, sacrifice the battle
        if new_defense == 0:
            self.move_card(card_id, card_owner, "battlefield", card_owner, "graveyard")
            logging.debug(f"Battle card {card.name} lost all defense counters and was sacrificed")
        
        return True

    def reduce_battle_defense(self, card_id, amount=1):
        """
        Reduce defense counters on a battle card.
        
        Args:
            card_id: ID of the battle card
            amount: Number of defense counters to remove
            
        Returns:
            bool: Success status
        """
        return self.add_defense_counter(card_id, -amount)

    def mark_exhaust_used(self, card_id, ability_index):
        """Mark an exhaust ability as used for this instance of the permanent."""
        key = (card_id, ability_index)
        if key not in self.exhaust_ability_used:
            self.exhaust_ability_used[key] = True
            logging.debug(f"Marked exhaust ability index {ability_index} for {card_id} as used.")
            return True
        else:
            logging.warning(f"Attempted to mark already used exhaust ability {ability_index} for {card_id}.")
            return False # Should not happen if check_exhaust_used is called first

    def check_exhaust_used(self, card_id, ability_index):
        """Check if an exhaust ability has already been used for this instance."""
        return (card_id, ability_index) in self.exhaust_ability_used

    def handle_control_changing_effect(self, source_card_id, target_card_id, duration="end_of_turn"):
        """
        Implement a control-changing effect from source card to target card.
        
        Args:
            source_card_id: ID of the card creating the control effect
            target_card_id: ID of the card being controlled
            duration: How long the control effect lasts
            
        Returns:
            bool: Whether the control effect was successfully applied
        """
        # Find the controller of the source card
        source_controller = None
        for player in [self.p1, self.p2]:
            if source_card_id in player["battlefield"]:
                source_controller = player
                break
                
        if not source_controller:
            logging.warning(f"Control effect: Source card {source_card_id} not found on battlefield")
            return False
        
        # Find the current controller of the target
        target_controller = self.find_card_location(target_card_id)
        if not target_controller or target_controller == source_controller:
            return False  # Already controlled by source or not found
            
        # Apply the temporary control effect
        success = self.apply_temporary_control(target_card_id, source_controller)
        
        # Register in layer system if available
        if success and hasattr(self, 'layer_system') and self.layer_system:
            self.layer_system.register_effect({
                'source_id': source_card_id,
                'layer': 2,  # Control-changing effects are layer 2
                'affected_ids': [target_card_id],
                'effect_type': 'change_control',
                'effect_value': source_controller,
                'duration': duration,
                'start_turn': self.turn
            })
        
        return success

    def activate_planeswalker_ability(self, card_id, ability_idx, controller):
        """Activates a planeswalker ability: Pays cost, potentially enters targeting, adds ability to stack."""
        card = self._safe_get_card(card_id)
        # Ensure card exists, is a planeswalker, and is on the controller's battlefield
        if not card or 'planeswalker' not in getattr(card, 'card_types', []) or card_id not in controller.get('battlefield', []):
            logging.warning(f"Invalid attempt to activate PW ability: Card {card_id} invalid or not controlled PW.")
            return False

        # Check activation limit (only once per turn per PW)
        activated_this_turn_set = controller.setdefault("activated_this_turn", set())
        if card_id in activated_this_turn_set:
            logging.debug(f"Planeswalker {card.name} ({card_id}) already activated this turn.")
            return False

        abilities = getattr(card, 'loyalty_abilities', [])
        if not (0 <= ability_idx < len(abilities)):
            logging.warning(f"Invalid ability index {ability_idx} for {card.name}")
            return False

        ability = abilities[ability_idx]
        cost = ability.get('cost', 0)
        effect_text = ability.get("effect", "")

        # Check loyalty affordability (Rule 118.5)
        current_loyalty = controller.get("loyalty_counters", {}).get(card_id, getattr(card, 'loyalty', 0))
        if current_loyalty + cost < 0: # Rule 118.5: Cannot pay cost if loyalty would become < 0
             logging.debug(f"Cannot activate PW ability for {card.name}: Loyalty {current_loyalty} + Cost {cost} < 0")
             return False

        # --- Costs are paid upon ACTIVATION (Rule 601.2h) ---
        # Pay loyalty cost
        new_loyalty = current_loyalty + cost
        controller.setdefault("loyalty_counters", {})[card_id] = new_loyalty

        # Mark as activated this turn
        activated_this_turn_set.add(card_id)
        # Increment total activations if tracked
        controller.setdefault("pw_activations", {})[card_id] = controller.get("pw_activations", {}).get(card_id, 0) + 1

        logging.debug(f"Paid loyalty cost ({cost:+}) for PW ability {ability_idx} on {card.name}. Loyalty now {new_loyalty}")

        # --- Targeting Setup ---
        requires_target = "target" in effect_text.lower()
        if requires_target:
             # Ability needs targets, set up targeting phase
             logging.debug(f"Planeswalker ability requires target. Entering TARGETING phase.")
             self.previous_priority_phase = self.phase # Store current phase
             self.phase = self.PHASE_TARGETING
             # Create targeting context
             self.targeting_context = {
                  "source_id": card_id,
                  "controller": controller,
                  "ability_idx": ability_idx, # Store index if needed later
                  "effect_text": effect_text,
                  "required_type": self._get_target_type_from_text(effect_text), # Use helper
                  "required_count": 1, # Assume 1 target unless text specifies more
                  "min_targets": 1, # Assumes target is required if text says 'target'
                  "selected_targets": [],
                  # Store info needed to put on stack AFTER targeting
                  "stack_info": {
                       "item_type": "ABILITY",
                       "source_id": card_id,
                       "controller": controller,
                       "context": {
                            "ability_index": ability_idx, # Include original index if needed
                            "ability_cost": cost,
                            "effect_text": effect_text,
                            "targets": {} # To be filled by targeting resolution
                       }
                  }
             }
             # Do NOT add to stack yet. Targeting actions will lead to stack addition.
             logging.debug(f"Set up targeting for PW ability: {effect_text}")

        else:
             # No targets needed, add ability directly to stack
             stack_context = {
                  "ability_index": ability_idx,
                  "ability_cost": cost,
                  "effect_text": effect_text,
                  "targets": {} # Empty targets dict
             }
             self.add_to_stack("ABILITY", card_id, controller, stack_context)
             logging.debug(f"Added non-targeting PW ability {ability_idx} for {card.name} to stack.")

        # Check SBAs immediately after paying cost (e.g., PW died from low loyalty)
        self.check_state_based_actions()

        return True # Activation successful (cost paid, targeting started or added to stack)

    def tap_permanent(self, card_id, player):
        """Tap a permanent, triggering any appropriate abilities."""
        if card_id not in player.get("battlefield", []):
             logging.warning(f"Cannot tap {card_id}: Not on {player['name']}'s battlefield.")
             return False
        tapped_set = player.setdefault("tapped_permanents", set())
        if card_id in tapped_set:
             logging.debug(f"Permanent {card_id} is already tapped.")
             return True # Already tapped is not a failure
        tapped_set.add(card_id)
        card = self._safe_get_card(card_id)
        logging.debug(f"Tapped {getattr(card, 'name', card_id)}")
        self.trigger_ability(card_id, "TAPPED", {"controller": player})
        return True

    def untap_permanent(self, card_id, player):
        """Untap a permanent, triggering any appropriate abilities."""
        if card_id not in player.get("battlefield", []):
             # Check phased out zone?
             if card_id in getattr(self, 'phased_out', set()):
                  logging.debug(f"Cannot untap {card_id}: Currently phased out.")
             else:
                  logging.warning(f"Cannot untap {card_id}: Not on {player['name']}'s battlefield.")
             return False
        tapped_set = player.setdefault("tapped_permanents", set())
        if card_id not in tapped_set:
             logging.debug(f"Permanent {card_id} is already untapped.")
             return True # Already untapped is not a failure
        tapped_set.remove(card_id)
        card = self._safe_get_card(card_id)
        logging.debug(f"Untapped {getattr(card, 'name', card_id)}")
        self.trigger_ability(card_id, "UNTAPPED", {"controller": player})
        return True

    def create_token_copy(self, original_card, controller):
        """Create a token copy of a card, handles details like base P/T."""
        if not original_card: return None
        # Create token tracking if it doesn't exist
        if not hasattr(controller, "tokens"): controller["tokens"] = []

        token_id = f"TOKEN_COPY_{len(controller['tokens'])}_{original_card.name[:10].replace(' ','')}"

        # Use dict/copy.deepcopy to get copyable values
        try:
            # Get copyable characteristics based on Rule 707.2
            copyable_values = {
                "name": original_card.name,
                "mana_cost": original_card.mana_cost,
                #"color": original_card.color, # Use color_identity?
                "color_identity": original_card.colors, # Store the 5-dim vector
                "card_types": copy.deepcopy(original_card.card_types),
                "subtypes": copy.deepcopy(original_card.subtypes),
                "supertypes": copy.deepcopy(original_card.supertypes),
                "oracle_text": original_card.oracle_text,
                # Base power/toughness/loyalty (not including counters/effects)
                "power": getattr(original_card, '_base_power', getattr(original_card, 'power', 0)), # Need base P/T logic
                "toughness": getattr(original_card, '_base_toughness', getattr(original_card, 'toughness', 0)),
                "loyalty": getattr(original_card, '_base_loyalty', getattr(original_card, 'loyalty', 0)),
                "keywords": copy.deepcopy(getattr(original_card,'keywords',[0]*11)), # Copy base keywords
                "faces": copy.deepcopy(getattr(original_card,'faces', None)), # Copy faces for DFCs
            }
            copyable_values["is_token"] = True # Mark as token
            copyable_values["type_line"] = original_card.type_line # Copy type line

        except Exception as e:
             logging.error(f"Error getting copyable values for {original_card.name}: {e}")
             return None

        try:
            token = Card(copyable_values)
            token.card_id = token_id # Assign the unique ID
        except Exception as e:
             logging.error(f"Error creating token copy Card object: {e} | Data: {copyable_values}")
             return None

        # Add to game
        self.card_db[token_id] = token
        # Use move_card to handle ETB triggers and effects
        success = self.move_card(token_id, controller, "nonexistent_zone", controller, "battlefield", cause="token_creation")
        if not success:
             # Clean up if move failed
             del self.card_db[token_id]
             return None

        controller["tokens"].append(token_id) # Add to token tracking list *after* successful entry

        logging.debug(f"Created token copy of {original_card.name} (ID: {token_id})")

        return token_id

    def _apply_planeswalker_uniqueness_rule(self, controller):
        """Apply the planeswalker uniqueness rule (legendary rule for planeswalkers)."""
        # Group planeswalkers by name
        planeswalkers_by_type = {}
        
        # Identify planeswalkers by type
        for card_id in controller["battlefield"]:
            card = self._safe_get_card(card_id)
            if not card or not hasattr(card, 'card_types') or 'planeswalker' not in card.card_types:
                continue
                
            # Group by planeswalker type
            planeswalker_type = None
            if hasattr(card, 'subtypes'):
                for subtype in card.subtypes:
                    if subtype.lower() != 'planeswalker':
                        planeswalker_type = subtype.lower()
                        break
            
            # If no subtype was found, use the card name as fallback
            if not planeswalker_type and hasattr(card, 'name'):
                planeswalker_type = card.name.lower()
            
            if planeswalker_type:
                if planeswalker_type not in planeswalkers_by_type:
                    planeswalkers_by_type[planeswalker_type] = []
                planeswalkers_by_type[planeswalker_type].append(card_id)
        
        # Check each group for duplicates
        for planeswalker_type, cards in planeswalkers_by_type.items():
            if len(cards) > 1:
                # Keep the newest one, sacrifice the rest
                newest = cards[-1]
                for old_pw in cards[:-1]:
                    self.move_card(old_pw, controller, "battlefield", controller, "graveyard")
                    logging.debug(f"Planeswalker uniqueness rule: Sacrificed duplicate planeswalker")

    def _process_keyword_abilities(self, spell_id, controller, context):
        """
        Process additional keyword abilities after the main spell effect.
        
        Args:
            spell_id: The ID of the spell
            controller: The player casting the spell
            context: Additional context information
        """
        spell = self._safe_get_card(spell_id)
        if not spell or not hasattr(spell, 'oracle_text'):
            return
            
        effect_text = spell.oracle_text.lower()
        
        # Storm ability handling
        if context.get("has_storm", False) or "storm" in effect_text:
            # Count spells cast this turn before this one
            storm_count = len([s for s in self.spells_cast_this_turn if s[1] == controller])
            
            if storm_count > 0:
                # Create copies
                for _ in range(storm_count):
                    self.stack.append(("SPELL", spell_id, controller, {"is_copy": True}))
                
                logging.debug(f"Storm: Created {storm_count} copies of {spell.name if hasattr(spell, 'name') else 'spell'}")
        
        # Cascade ability handling
        if context.get("has_cascade", False) or "cascade" in effect_text:
            # Find a lower-cost spell in library
            if controller["library"]:
                cascade_cost = None
                if hasattr(spell, 'cmc'):
                    cascade_cost = spell.cmc
                
                # Find first card with lower mana value
                found_card = None
                found_idx = -1
                
                # Reveal cards until we find one with lower cost
                for idx, lib_card_id in enumerate(controller["library"]):
                    lib_card = self._safe_get_card(lib_card_id)
                    if lib_card and hasattr(lib_card, 'cmc') and lib_card.cmc < cascade_cost:
                        if not hasattr(lib_card, 'card_types') or \
                        ('land' not in lib_card.card_types):
                            found_card = lib_card
                            found_idx = idx
                            break
                
                if found_card and found_idx >= 0:
                    # Cast the found card for free
                    cascade_card_id = controller["library"].pop(found_idx)
                    self.stack.append(("SPELL", cascade_card_id, controller, {"is_free": True}))
                    logging.debug(f"Cascade: Cast {found_card.name} for free")
                    
                    # Put the rest on the bottom in random order
                    revealed_cards = controller["library"][:found_idx]
                    controller["library"] = controller["library"][found_idx:]
                    random.shuffle(revealed_cards)
                    controller["library"].extend(revealed_cards)
        
        # Flashback handling for exile instead of graveyard
        if hasattr(self, 'flashback_cards') and spell_id in self.flashback_cards:
            # Mark to prevent going to graveyard
            context["skip_default_movement"] = True
            
            # Move to exile
            controller["exile"].append(spell_id)
            self.flashback_cards.remove(spell_id)
            logging.debug(f"Flashback: Exiled {spell.name if hasattr(spell, 'name') else 'spell'} after resolution")
        
        # Buyback handling for return to hand instead of graveyard
        if context.get("buyback", False) or (hasattr(self, 'buyback_cards') and spell_id in self.buyback_cards):
            # Mark to prevent going to graveyard
            context["skip_default_movement"] = True
            
            # Return to hand
            controller["hand"].append(spell_id)
            if hasattr(self, 'buyback_cards') and spell_id in self.buyback_cards:
                self.buyback_cards.remove(spell_id)
            logging.debug(f"Buyback: Returned {spell.name if hasattr(spell, 'name') else 'spell'} to hand")

    def add_counter(self, card_id, counter_type, count=1, _annihilating=False):
        """Add counters to a permanent, handling Impending completion."""
        target_owner, target_zone = self.find_card_location(card_id)
        if not target_owner or target_zone != 'battlefield':
             logging.debug(f"Cannot add counter to {card_id}: Not on battlefield.")
             return False # Target must be on battlefield
        target_card = self._safe_get_card(card_id)
        if not target_card: return False

        # Layered/Replacement effect for ADD_COUNTER
        # Note: Needs refinement. Do replacements modify count BEFORE adding? Yes.
        counter_context = {'card_id': card_id, 'target_id': card_id, 'counter_type': counter_type, 'count': count}
        final_count = count
        if self.replacement_effects:
             modified_context, replaced = self.replacement_effects.apply_replacements("ADD_COUNTER", counter_context.copy())
             if replaced:
                 final_count = modified_context.get('count', 0)
                 counter_type = modified_context.get('counter_type', counter_type) # Allow type change?
                 if modified_context.get('prevented'): final_count = 0
             # Carry over modified context if needed downstream

        if final_count == 0:
             logging.debug(f"Adding {counter_type} counters to {target_card.name} prevented or reduced to 0.")
             return True # Still considered successful if prevented/doubled to 0

        count = final_count # Use the possibly modified count

        # Ensure counters attribute exists
        if not hasattr(target_card, 'counters'): target_card.counters = {}

        current_count = target_card.counters.get(counter_type, 0)
        new_count = current_count + count # Note: count can be negative for removal
        actual_change = count # Store the intended change

        # Don't let counters go below 0 unless it's a state (like level?)
        new_count = max(0, new_count)

        # If count didn't actually change state (e.g., remove 1 from 0)
        if new_count == current_count and current_count == 0:
            return False # Indicate no effective change happened

        if new_count > 0:
            target_card.counters[counter_type] = new_count
        elif counter_type in target_card.counters: # Became 0, remove key
             del target_card.counters[counter_type]

        # BUGFIX: annihilation (+1/+1 vs -1/-1) used to be applied inline here,
        # but COUNTER_ADDED/REMOVED triggers re-enter check_state_based_actions,
        # whose own 704.5q handler then interleaved with this one on half-updated
        # state and wiped every counter. Annihilation is a state-based action
        # (CR 704.5q); the SBA handler owns it exclusively now.

        logging.debug(f"Updated {counter_type} counters on {target_card.name} by {actual_change:+}. New count: {new_count}")

        # --- Impending Check ---
        # If the counter was 'time' AND it was REMOVED (count < 0) AND the new count is <= 0
        if counter_type == 'time' and actual_change < 0 and new_count <= 0 and card_id in getattr(self, 'impending_cards', {}):
            logging.info(f"Impending complete for {target_card.name}: Last time counter removed.")
            # 1. Remove Static Effects preventing it from being a creature
            if self.layer_system:
                 removed_impending_effects = self.layer_system.remove_effects_by_source(card_id, effect_description_contains="Impending static effect")
                 if removed_impending_effects:
                     logging.debug(f"Removed {removed_impending_effects} Impending static effects for {target_card.name}.")
                     # Re-apply layers immediately to update characteristics (P/T, type)
                     self.layer_system.apply_all_effects()
            # 2. Clean up Impending Tracking
            if card_id in self.impending_cards:
                del self.impending_cards[card_id]
            # 3. Trigger "becomes creature" event (maybe just IMPENDING_COMPLETE)
            # Ensure it has the context of the permanent triggering this
            trigger_context = {'controller': target_owner, 'card_id': card_id}
            self.trigger_ability(card_id, "IMPENDING_COMPLETE", trigger_context)
        # --- End Impending Check ---

        # Trigger counter addition/removal events AFTER Impending check
        if actual_change != 0: # Only trigger if count changed
            event = "COUNTER_ADDED" if actual_change > 0 else "COUNTER_REMOVED"
            # Use actual_change to reflect intent, even if annihilation happens later
            self.trigger_ability(card_id, event, {"controller": target_owner, "counter_type": counter_type, "count": abs(actual_change)})

        # Always check SBAs after ANY counter change, as it could affect P/T
        self.check_state_based_actions()
        return True

    def give_haste_until_eot(self, card_id):
        """Grant haste until end of turn."""
        if not hasattr(self, 'haste_until_eot'): self.haste_until_eot = set()
        self.haste_until_eot.add(card_id)

    def create_token(self, controller, token_data):
        """
        Create a token and add it to the battlefield.
        
        Args:
            controller: The player who will control the token
            token_data: Dictionary with token specifications (name, types, p/t, etc.)
            
        Returns:
            str: Token ID if successful, None otherwise
        """
        try:
            # Create token tracking if it doesn't exist
            if not hasattr(controller, "tokens"):
                controller["tokens"] = []
                
            # Generate token ID
            token_count = len(controller["tokens"])
            token_id = f"TOKEN_{token_count}_{token_data.get('name', 'Generic').replace(' ', '_')}"
            
            # Set default values if not provided
            if "power" not in token_data:
                token_data["power"] = 1
            if "toughness" not in token_data:
                token_data["toughness"] = 1
            if "card_types" not in token_data:
                token_data["card_types"] = ["creature"]
            if "subtypes" not in token_data:
                token_data["subtypes"] = []
            if "oracle_text" not in token_data:
                token_data["oracle_text"] = ""
            if "keywords" not in token_data:
                token_data["keywords"] = [0] * 11
            if "colors" not in token_data:
                token_data["colors"] = [0, 0, 0, 0, 0]  # Colorless by default
            
            # Create token Card object
            token = Card(token_data)
            
            # Add token to the card database
            self.card_db[token_id] = token
            
            # Add token to battlefield
            controller["battlefield"].append(token_id)
            controller["tokens"].append(token_id)
            
            # Mark as entering this turn (summoning sickness)
            if 'creature' in token_data["card_types"]:
                controller["entered_battlefield_this_turn"].add(token_id)
            
            # Trigger enters-the-battlefield abilities
            self.trigger_ability(token_id, "ENTERS_BATTLEFIELD")
            
            logging.debug(f"Created token: {token_data.get('name', 'Generic Token')}")
            return token_id
            
        except Exception as e:
            logging.error(f"Error creating token: {str(e)}")
            return None

    def check_keyword(self, card_id, keyword):
        """
        Checks if a card has a specific keyword, prioritizing Layer System results.
        This is the central point for keyword checks within GameState.

        Args:
            card_id (str): The ID of the card to check.
            keyword (str): The keyword to check for (e.g., 'flying', 'haste').

        Returns:
            bool: True if the card currently has the keyword, False otherwise.
        """
        card = self._safe_get_card(card_id)
        if not card:
            logging.debug(f"check_keyword: Card {card_id} not found.")
            return False

        keyword_lower = keyword.lower()

        # 1. Prefer Layer System Results (on the live card object)
        # The LayerSystem updates the card object directly with the final 'keywords' array.
        if hasattr(card, 'keywords') and isinstance(card.keywords, (list, np.ndarray)):
            try:
                # Ensure Card.ALL_KEYWORDS is available and populated
                if not hasattr(Card, 'ALL_KEYWORDS') or not Card.ALL_KEYWORDS:
                     # Attempt to load if missing (e.g., if Card class wasn't fully initialized)
                     if hasattr(Card, '_load_keywords'):
                         Card._load_keywords()
                     if not hasattr(Card, 'ALL_KEYWORDS') or not Card.ALL_KEYWORDS:
                          logging.error("Card.ALL_KEYWORDS is missing or empty. Cannot perform keyword check.")
                          return False # Cannot check without the list

                # Use the static list from Card class for consistency
                kw_list = [k.lower() for k in Card.ALL_KEYWORDS]
                idx = kw_list.index(keyword_lower)
                if idx < len(card.keywords):
                    has_keyword = bool(card.keywords[idx])
                    # Logging can be very verbose, disable or make conditional
                    # logging.debug(f"check_keyword (Layer): '{keyword_lower}' on '{card.name}' -> {has_keyword}")
                    return has_keyword
                else:
                    # Index is valid for the keyword list, but out of bounds for *this card's* keyword array
                    # This implies an issue with array initialization or keyword list mismatch.
                    logging.warning(f"check_keyword (Layer): Keyword index {idx} out of bounds for {card.name}'s keyword array (Len: {len(card.keywords)})")
                    return False # Treat as not having the keyword if array is wrong size
            except ValueError:
                 # Keyword is not in the standard Card.ALL_KEYWORDS list
                 # Could be a temporary/pseudo keyword like 'cant_attack' added by layers.
                 # How LayerSystem handles these needs clarification. If it adds them directly
                 # as attributes or modifies the 'keywords' array needs to be consistent.
                 # For now, assume standard keywords are in the array. Non-standard = False.
                 logging.debug(f"check_keyword (Layer): Keyword '{keyword_lower}' not in Card.ALL_KEYWORDS list.")
                 # Check if it exists as a direct attribute (less likely for LayerSystem)
                 # return getattr(card, keyword_lower, False)
                 return False
            except IndexError:
                 # Should be caught by the length check above, but safety catch.
                 logging.warning(f"check_keyword (Layer): Unexpected IndexError for keyword {keyword_lower} on {card.name}.")
                 return False
            except Exception as e:
                 logging.error(f"check_keyword (Layer): Error checking keyword array for {card.name}: {e}")
                 return False # Error implies uncertainty, assume false

        # 2. Fallback (Less Reliable): Check base card text IF no layer system active/result found
        # This is unreliable because layers can grant/remove keywords. Use with caution.
        elif not self.layer_system:
             logging.warning(f"check_keyword: LayerSystem inactive, falling back to basic text check for '{keyword_lower}' on {card.name}. This may be inaccurate.")
             if hasattr(card, 'oracle_text'):
                 # Simple text check (can be fooled by reminder text or unrelated mentions)
                 # Add word boundaries for more accuracy on single-word keywords
                 pattern = r'\b' + re.escape(keyword_lower) + r'\b'
                 return bool(re.search(pattern, card.oracle_text.lower()))

        # If no LayerSystem result and no fallback text check done, assume False
        logging.debug(f"check_keyword: Keyword '{keyword_lower}' not found or verifiable for {card.name}.")
        return False

    def _is_legal_attachment(self, attach_id, target_id):
        """Check if an Aura/Equipment/Fortification can legally be attached to the target."""
        attachment = self._safe_get_card(attach_id)
        target = self._safe_get_card(target_id)
        if not attachment or not target: return False

        _, target_zone = self.find_card_location(target_id)
        if target_zone != 'battlefield': return False

        # Check "enchant X", "equip X", "fortify X" restrictions
        attach_text = getattr(attachment, 'oracle_text', '').lower()
        target_types = getattr(target, 'card_types', [])
        target_subtypes = getattr(target, 'subtypes', [])

        if 'aura' in getattr(attachment, 'subtypes', []):
            if 'enchant creature' in attach_text and 'creature' not in target_types: return False
            if 'enchant artifact' in attach_text and 'artifact' not in target_types: return False
            if 'enchant land' in attach_text and 'land' not in target_types: return False
            if 'enchant permanent' in attach_text: pass # Always legal if target is permanent
            # Add more specific enchant checks (e.g., "enchant artifact or creature")
            # Regex might be needed: re.search(r"enchant ([\w\s]+)", attach_text)
        elif 'equipment' in getattr(attachment, 'subtypes', []):
            if 'creature' not in target_types: return False
        elif 'fortification' in getattr(attachment, 'subtypes', []):
            if 'land' not in target_types: return False

        # Check Protection
        if self.targeting_system and hasattr(self.targeting_system, '_has_protection_from'):
            # Need controllers. Assume attachment controlled by player who owns attachment dict.
            attach_player = self.get_card_controller(attach_id)
            target_player = self.get_card_controller(target_id)
            # Aura/Equip targets the permanent it's attached to
            if self.targeting_system._has_protection_from(target, attachment, target_player, attach_player):
                 return False

        return True # Assume legal if no specific restriction failed

    def _check_and_remove_invalid_tokens(self):
        """Check all zones for tokens that shouldn't exist there and remove them."""
        removed_token = False
        for player in [self.p1, self.p2]:
            if not player: continue
            tokens_in_non_bf_zones = []
            # Check zones other than battlefield
            for zone in ["hand", "graveyard", "exile", "library", "stack_implicit"]: # Check stack too implicitly
                zone_content = player.get(zone)
                if zone_content and isinstance(zone_content, (list, set)):
                    # Iterate over copy for removal
                    for card_id in list(zone_content):
                        card = self._safe_get_card(card_id)
                        if card and hasattr(card, 'is_token') and card.is_token:
                             tokens_in_non_bf_zones.append((card_id, zone))

            # Check stack explicitly
            for item in self.stack:
                if isinstance(item, tuple) and len(item)>1:
                     item_id = item[1]
                     item_card = self._safe_get_card(item_id)
                     if item_card and hasattr(item_card, 'is_token') and item_card.is_token:
                          tokens_in_non_bf_zones.append((item_id, "stack"))

            # Remove found tokens
            for card_id, zone_name in tokens_in_non_bf_zones:
                 # Remove from card_db
                 if card_id in self.card_db:
                      del self.card_db[card_id]
                      logging.debug(f"SBA: Token {card_id} ceased to exist in {zone_name}.")
                      removed_token = True
                 # Remove from player zone / stack
                 if zone_name == "stack":
                      self.stack = [item for item in self.stack if not (isinstance(item, tuple) and item[1] == card_id)]
                 elif zone_name != "stack_implicit" and zone_name in player and isinstance(player[zone_name],(list,set)) and card_id in player[zone_name]:
                      if isinstance(player[zone_name], list): player[zone_name].remove(card_id)
                      elif isinstance(player[zone_name], set): player[zone_name].discard(card_id)

        return removed_token

    def apply_regeneration(self, card_id, player):
        """Applies a regeneration shield if available, preventing destruction."""
        if card_id in player.get("regeneration_shields", set()):
            card = self._safe_get_card(card_id)
            # Verify card still exists and is on battlefield (might have been removed by other SBAs)
            current_controller, current_zone = self.find_card_location(card_id)
            if card and current_controller == player and current_zone == "battlefield":
                player["regeneration_shields"].remove(card_id)
                self.tap_permanent(card_id, player) # Tap the creature
                # Remove damage marked on creature
                if 'damage_counters' in player: player['damage_counters'].pop(card_id, None)
                if 'deathtouch_damage' in player: player.get('deathtouch_damage', {}).pop(card_id, None) # Clear deathtouch mark

                # Also remove from combat if attacking/blocking (Rule 614.8)
                if card_id in self.current_attackers: self.current_attackers.remove(card_id)
                for attacker_id, blockers in list(self.current_block_assignments.items()):
                    if card_id in blockers: blockers.remove(card_id)
                    if not blockers: del self.current_block_assignments[attacker_id] # Clean up if no blockers left

                logging.debug(f"Regeneration shield used for {card.name}. Creature tapped and removed from combat.")
                return True
            else:
                 # Shield exists but creature is gone or no longer controlled by player, remove stale shield
                 player.get("regeneration_shields", set()).discard(card_id)
                 logging.debug(f"Stale regeneration shield removed for {card_id}")

        return False

    def apply_totem_armor(self, card_id, player):
        """Applies totem armor if available, destroying the Aura instead."""
        totem_aura_id = None
        for aura_id in list(player.get("battlefield", [])): # Check player's battlefield for auras attached to the creature
            aura = self._safe_get_card(aura_id)
            if not aura: continue
            is_aura_with_totem = ('aura' in getattr(aura, 'subtypes', [])) and ("totem armor" in getattr(aura, 'oracle_text', '').lower())

            # Check if this aura is attached to the creature being destroyed
            if is_aura_with_totem and player.get("attachments", {}).get(aura_id) == card_id:
                totem_aura_id = aura_id
                break # Found one, apply it

        if totem_aura_id:
            aura_to_destroy = self._safe_get_card(totem_aura_id)
            creature_saved = self._safe_get_card(card_id)
            logging.debug(f"Totem armor: Destroying {getattr(aura_to_destroy,'name','Aura')} instead of {getattr(creature_saved,'name','Creature')}.")
            # Destroy the aura
            if self.move_card(totem_aura_id, player, "battlefield", player, "graveyard", cause="totem_armor"):
                 # Remove damage marked on the creature if destruction is prevented
                 if 'damage_counters' in player: player['damage_counters'].pop(card_id, None)
                 if 'deathtouch_damage' in player: player.get('deathtouch_damage', {}).pop(card_id, None) # Clear deathtouch mark
                 # Don't tap or remove from combat for totem armor
                 return True
            else:
                 logging.error(f"Failed to destroy totem armor aura {totem_aura_id}")
        return False

    def proliferate(self, player, targets="all"):
        """Apply proliferate effect."""
        proliferated_something = False
        valid_targets = []

        # Gather all players and permanents with counters
        for p in [self.p1, self.p2]:
            if p: # Check player exists
                if p.get("poison_counters", 0) > 0 or p.get("experience_counters", 0) > 0 or p.get("energy_counters", 0) > 0:
                     valid_targets.append(p)
                for card_id in p.get("battlefield", []):
                    card = self._safe_get_card(card_id)
                    # Include permanents (including PWs) with any type of counter
                    if card and hasattr(card, 'counters') and card.counters:
                         valid_targets.append(card_id)
                    # Include planeswalkers specifically for loyalty if not in card.counters yet
                    elif card and 'planeswalker' in getattr(card,'card_types',[]) and p.get('loyalty_counters',{}).get(card_id, 0) > 0:
                         valid_targets.append(card_id) # Add PW id if it has loyalty

        # Determine which targets to proliferate based on player choice
        # For AI, need a selection mechanism. Simple: Proliferate all valid targets.
        targets_to_proliferate = valid_targets # Simple: affect all valid

        if not targets_to_proliferate:
            logging.debug("Proliferate: No valid targets with counters found.")
            return False

        # --- AI Choice ---
        # More complex AI would choose which subset of valid_targets to affect.
        # Simple: proliferate all possible targets chosen by the player activating proliferate
        chosen_targets = [t for t in targets_to_proliferate if t == player or (isinstance(t, str) and self.get_card_controller(t) == player)]
        # If the effect specified 'opponent' or 'target', selection would differ.
        # Assuming "You choose..." - AI chooses based on strategy (e.g., buff self, poison opponent)
        # Simplification: affect everything controlled by the player + opponent players
        chosen_targets = []
        for target in valid_targets:
            if target == player: # Target self (player counters)
                chosen_targets.append(target)
            elif target == self._get_non_active_player() and target != player: # Target opponent (player counters)
                chosen_targets.append(target)
            elif isinstance(target, str): # Is a permanent ID
                card = self._safe_get_card(target)
                target_controller = self.get_card_controller(target)
                # Simple heuristic: proliferate own good counters, opponent's bad counters
                is_good_counter = any(ct in card.counters for ct in ["+1/+1", "lore", "loyalty"]) if hasattr(card, 'counters') else False
                is_bad_counter = any(ct in card.counters for ct in ["-1/-1", "poison"]) if hasattr(card, 'counters') else False
                if target_controller == player and is_good_counter: chosen_targets.append(target)
                if target_controller != player and is_bad_counter: chosen_targets.append(target)
                if target_controller != player and 'planeswalker' in getattr(card,'card_types',[]): chosen_targets.append(target) # Proliferate loyalty removal? Seems bad. Skip.

        # Fallback if heuristic finds nothing: proliferate own first valid target with counters.
        if not chosen_targets:
             for target in valid_targets:
                 if isinstance(target, str) and self.get_card_controller(target) == player:
                     chosen_targets.append(target); break

        logging.debug(f"Proliferate choosing targets: {chosen_targets}")


        # --- Apply Proliferation ---
        for target in chosen_targets:
            if isinstance(target, dict) and target in [self.p1, self.p2]: # Player target
                player_to_affect = target
                added_counter = False
                # Choose ONE type of counter the player has to increment
                counters_present = []
                if player_to_affect.get("poison_counters", 0) > 0: counters_present.append("poison")
                if player_to_affect.get("experience_counters", 0) > 0: counters_present.append("experience")
                if player_to_affect.get("energy_counters", 0) > 0: counters_present.append("energy")
                # AI Choice needed here which counter type to choose if multiple exist. Simple: First found.
                if counters_present:
                    chosen_counter_type = counters_present[0]
                    if chosen_counter_type == "poison": player_to_affect["poison_counters"] += 1; added_counter=True
                    elif chosen_counter_type == "experience": player_to_affect["experience_counters"] += 1; added_counter=True
                    elif chosen_counter_type == "energy": player_to_affect["energy_counters"] += 1; added_counter=True

                if added_counter:
                    logging.debug(f"Proliferated {chosen_counter_type} counter on player {player_to_affect['name']}")
                    proliferated_something = True

            elif isinstance(target, str): # Permanent card_id
                card = self._safe_get_card(target)
                target_controller = self.get_card_controller(target)
                if not card or not target_controller: continue

                # Choose ONE kind of counter already on the permanent to add another of.
                counters_present = []
                if hasattr(card, 'counters') and card.counters:
                    counters_present.extend(list(card.counters.keys()))
                # Check loyalty counters separately
                if 'planeswalker' in getattr(card,'card_types',[]) and target_controller.get('loyalty_counters',{}).get(target, 0) > 0:
                    counters_present.append('loyalty')

                if counters_present:
                    # AI Choice needed here which counter type to choose. Simple: First found.
                    chosen_counter_type = counters_present[0]
                    if chosen_counter_type == 'loyalty':
                        # Need method to add loyalty counter
                        current_loyalty = target_controller.get("loyalty_counters", {}).get(target, 0)
                        target_controller.setdefault("loyalty_counters", {})[target] = current_loyalty + 1
                        logging.debug(f"Proliferated loyalty counter on {card.name}")
                        proliferated_something = True
                    else:
                        # Use existing add_counter method
                        if self.add_counter(target, chosen_counter_type, 1):
                             # Logging handled by add_counter
                             proliferated_something = True


        # Check SBAs after proliferation might change things (e.g., PW death, -1/-1 kill)
        if proliferated_something: self.check_state_based_actions()
        return proliferated_something

    def mutate(self, player, mutating_card_id, target_id):
        """Handle the mutate mechanic."""
        target_card = self._safe_get_card(target_id)
        mutating_card = self._safe_get_card(mutating_card_id)
        if not target_card or not mutating_card: return False

        # Validation (non-human creature target)
        if 'creature' not in getattr(target_card, 'card_types', []) or 'Human' in getattr(target_card, 'subtypes', []):
             logging.warning(f"Mutate failed: Target {target_card.name} is not a non-Human creature.")
             return False

        # Decide top/bottom (AI choice, default: new card on top)
        mutate_on_top = True

        # Apply mutation based on top/bottom choice
        merged_card = None
        current_mutation_stack = getattr(player,"mutation_stacks", {}).get(target_id, [target_id])

        if mutate_on_top:
            merged_card = mutating_card # Top card defines name, types, P/T
            # Combine abilities/text (simplistic append)
            merged_card.oracle_text = getattr(target_card, 'oracle_text','') + "\n" + getattr(mutating_card, 'oracle_text','')
            # Keep counters, auras, equipment from target_card
            merged_card.counters = target_card.counters.copy() if hasattr(target_card, 'counters') else {}
            # Update the representation in card_db? Or just modify the live object? Modify live for now.
            target_card.name = merged_card.name
            target_card.power = merged_card.power
            target_card.toughness = merged_card.toughness
            target_card.card_types = merged_card.card_types
            target_card.subtypes = merged_card.subtypes
            target_card.oracle_text = merged_card.oracle_text
            # Keep target_card.counters
            current_mutation_stack.insert(0, mutating_card_id) # New card on top of stack list
        else: # Mutate under
            merged_card = target_card # Target defines name, types, P/T
            merged_card.oracle_text = getattr(target_card, 'oracle_text','') + "\n" + getattr(mutating_card, 'oracle_text','')
            # Keep target_card.counters
            current_mutation_stack.append(mutating_card_id) # New card at bottom of stack list

        # Track the mutation stack
        if not hasattr(player, "mutation_stacks"): player["mutation_stacks"] = {}
        player["mutation_stacks"][target_id] = current_mutation_stack

        # Mutating card leaves original zone (usually hand) implicitly handled by cast_spell
        # If cast from elsewhere, that needs handling too.

        # Trigger mutate ability (triggers for EACH card in the stack now)
        for card_in_stack_id in current_mutation_stack:
            self.trigger_ability(card_in_stack_id, "MUTATES", {"target_id": target_id, "top_card_id": current_mutation_stack[0]})

        logging.debug(f"{mutating_card.name} mutated {'onto' if mutate_on_top else 'under'} {target_card.name}. Result is now {merged_card.name}.")
        if self.layer_system: self.layer_system.apply_all_effects() # Re-apply layers
        return True

    def reanimate(self, player, gy_index):
        """Reanimate a permanent from graveyard."""
        if gy_index < len(player["graveyard"]):
            card_id = player["graveyard"][gy_index]
            card = self._safe_get_card(card_id)
            if card and any(t in getattr(card, 'card_types', []) for t in ["creature", "artifact", "enchantment", "planeswalker"]):
                return self.move_card(card_id, player, "graveyard", player, "battlefield")
        return False

    def flip_card(self, card_id):
        """Handle flipping a flip card. Assumes card object has flip logic."""
        card = self._safe_get_card(card_id)
        player = self.get_card_controller(card_id)
        if card and player and hasattr(card, 'flip'): # Assume a method exists
            if card.flip(): # Assume flip() returns True on success
                logging.debug(f"Flipped {card.name}")
                self.trigger_ability(card_id, "FLIPPED", {"controller": player})
                if self.layer_system: self.layer_system.apply_all_effects()
                return True
        return False

    def equip_permanent(self, player, equip_id, target_id):
        """Attach equipment, potentially replacing existing attachment."""
        equip_card = self._safe_get_card(equip_id)
        target_card = self._safe_get_card(target_id)
        target_owner = self.get_card_controller(target_id)

        # Basic validation
        if not equip_card or 'equipment' not in getattr(equip_card, 'subtypes', []) or \
           not target_card or 'creature' not in getattr(target_card, 'card_types', []) or \
           target_owner != player: # Can only equip to own creatures normally (Rule 301.5)
            logging.warning(f"Invalid equip: Eq:{equip_id} to Tgt:{target_id}. Target controller: {target_owner['name'] if target_owner else 'None'}")
            return False

        if not hasattr(player, "attachments"): player["attachments"] = {}
        # Remove previous attachment of this equipment, if any
        if equip_id in player["attachments"]:
            logging.debug(f"Unequipping {equip_card.name} from previous target {player['attachments'][equip_id]}")
            del player["attachments"][equip_id]
        # Attach to new target
        player["attachments"][equip_id] = target_id
        logging.debug(f"Equipped {equip_card.name} to {target_card.name}")
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(equip_id, "EQUIPPED", {"target_id": target_id})
        self.trigger_ability(target_id, "BECAME_EQUIPPED", {"equipment_id": equip_id})
        return True

    def unequip_permanent(self, player, equip_id):
        """Unequip an equipment."""
        if hasattr(player, "attachments") and equip_id in player["attachments"]:
            equip_name = getattr(self._safe_get_card(equip_id), 'name', equip_id)
            target_id = player["attachments"].pop(equip_id)
            logging.debug(f"Unequipped {equip_name} from {target_id}")
            if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
            # Trigger unequipped events? (Less common than equip)
            return True
        logging.debug(f"Cannot unequip {equip_id}: Not attached.")
        return False # Wasn't attached

    def attach_aura(self, player, aura_id, target_id):
        """Attach an aura. Assumes validation (legal target) happened before."""
        if not hasattr(player, "attachments"): player["attachments"] = {}
        aura_card = self._safe_get_card(aura_id)
        target_card = self._safe_get_card(target_id)
        aura_name = getattr(aura_card, 'name', aura_id)
        target_name = getattr(target_card, 'name', target_id)

        # Find target's actual location/controller for validation
        target_owner, target_zone = self.find_card_location(target_id)
        if target_zone != "battlefield":
             logging.warning(f"Cannot attach {aura_name}: Target {target_name} not on battlefield.")
             return False
        # TODO: Add "enchant <type>" validation and protection checks from TargetingSystem here if not done externally.

        if aura_id in player["attachments"]:
            logging.debug(f"Re-attaching {aura_name} from {player['attachments'][aura_id]} to {target_name}")
            del player["attachments"][aura_id]

        player["attachments"][aura_id] = target_id
        logging.debug(f"Attached {aura_name} to {target_name}")
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(aura_id, "ATTACHED", {"target_id": target_id})
        self.trigger_ability(target_id, "BECAME_ENCHANTED", {"aura_id": aura_id})
        return True

    def fortify_land(self, player, fort_id, target_id):
        """Attach a fortification to a land."""
        fort_card = self._safe_get_card(fort_id)
        target_card = self._safe_get_card(target_id)
        target_owner = self.get_card_controller(target_id)

        # Validation
        if not fort_card or 'fortification' not in getattr(fort_card, 'subtypes', []) or \
           not target_card or 'land' not in getattr(target_card, 'card_types', []) or \
           target_owner != player: # Fortify requires controlling the land (Rule 301.6)
            logging.warning(f"Invalid fortify: Fort:{fort_id} to Land:{target_id}. Target controller: {target_owner['name'] if target_owner else 'None'}")
            return False

        if not hasattr(player, "attachments"): player["attachments"] = {}
        if fort_id in player["attachments"]:
            logging.debug(f"Unequipping {fort_card.name} from previous land {player['attachments'][fort_id]}")
            del player["attachments"][fort_id]
        player["attachments"][fort_id] = target_id
        logging.debug(f"Fortified {target_card.name} with {fort_card.name}")
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(fort_id, "FORTIFIED", {"target_id": target_id})
        self.trigger_ability(target_id, "BECAME_FORTIFIED", {"fortification_id": fort_id})
        return True

    def reconfigure_permanent(self, player, card_id):
        """Handle reconfigure. Assumes cost is paid."""
        card = self._safe_get_card(card_id)
        if not card or card_id not in player["battlefield"] or 'reconfigure' not in getattr(card, 'oracle_text', '').lower():
            logging.warning(f"Invalid reconfigure: Card {card_id} not found, not owned, or doesn't have reconfigure.")
            return False

        if not hasattr(player, "attachments"): player["attachments"] = {}
        is_attached = card_id in player["attachments"]
        # Must be a creature OR equipment to reconfigure
        can_reconfigure = 'creature' in getattr(card, 'card_types',[]) or 'equipment' in getattr(card, 'subtypes',[])

        if not can_reconfigure:
            logging.warning(f"Cannot reconfigure {card.name}, not a creature or equipment currently.")
            return False

        if is_attached: # Unattach: Becomes creature, loses equipment type
            target_id = player["attachments"].pop(card_id)
            if 'equipment' in getattr(card,'subtypes',[]): card.subtypes.remove('equipment')
            if 'creature' not in getattr(card, 'card_types',[]): card.card_types.append('creature')
            logging.debug(f"Reconfigured {card.name} to unattach from {self._safe_get_card(target_id).name}. It's now a creature.")
        else: # Attach: Becomes equipment, loses creature type
             # AI Choice needed for target. Simple: first valid owned creature.
             target_id = None
             for cid in player["battlefield"]:
                  if cid == card_id: continue
                  c = self._safe_get_card(cid)
                  if c and 'creature' in getattr(c, 'card_types', []):
                       target_id = cid; break
             if target_id:
                  player["attachments"][card_id] = target_id
                  if 'creature' in card.card_types: card.card_types.remove('creature')
                  if 'equipment' not in getattr(card, 'subtypes',[]): card.subtypes.append('equipment')
                  logging.debug(f"Reconfigured {card.name} to attach to {self._safe_get_card(target_id).name}. It's now an Equipment.")
             else:
                  logging.warning(f"Reconfigure failed for {card.name}: No valid target creature found.")
                  return False # No target

        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(card_id, "RECONFIGURED")
        return True

    def turn_face_up(self, player, card_id, pay_morph_cost=False, pay_manifest_cost=False):
        """Turn a face-down Morph or Manifest card face up."""
        card = self._safe_get_card(card_id)
        if not card or card_id not in player["battlefield"]: return False

        is_face_down = False
        original_info = None
        cost_to_pay_str = None
        source_mechanic = None

        # Check if manifested
        manifest_info = getattr(self, 'manifested_cards', {}).get(card_id)
        if manifest_info and manifest_info.get('face_down', True):
            is_face_down = True
            source_mechanic = "Manifest"
            if pay_manifest_cost:
                 original_info = manifest_info.get('original')
                 if original_info and 'creature' in original_info.get('card_types', []): # Only creatures can be turned up via manifest cost
                     cost_to_pay_str = original_info.get('mana_cost')
                 else:
                     logging.debug(f"Cannot turn up non-creature manifest {card_id} via cost.")
                     return False # Cannot turn non-creature manifest up this way

        # Check if morphed (if not already identified as manifest)
        morph_info = getattr(self, 'morphed_cards', {}).get(card_id)
        if not is_face_down and morph_info and morph_info.get('face_down', True):
             is_face_down = True
             source_mechanic = "Morph"
             if pay_morph_cost:
                  original_info = morph_info.get('original')
                  original_card_temp = Card(original_info) # Temporary card to parse cost
                  match = re.search(r"morph\s*(\{.*?\})", getattr(original_card_temp, 'oracle_text', '').lower())
                  if match: cost_to_pay_str = match.group(1)
                  else: logging.warning(f"Could not parse Morph cost for {original_info.get('name')}")

        # Check generic face-down attribute if specific tracking missed
        if not is_face_down and getattr(card, 'face_down', False):
             is_face_down = True
             source_mechanic = "Unknown Face-down" # Possibly from other effects
             # Cannot determine original info or cost for generic face-down easily
             logging.warning(f"Cannot turn face-down card {card_id} up: Unknown origin or cost.")
             return False


        if not is_face_down:
            logging.debug(f"Cannot turn {card.name} face up: Not face down.")
            return False

        if (pay_morph_cost or pay_manifest_cost):
            if not cost_to_pay_str:
                logging.debug(f"Cannot turn {card.name} face up: No valid cost found for {source_mechanic}.")
                return False
            # Check and Pay Cost
            if not self.mana_system.can_pay_mana_cost(player, cost_to_pay_str):
                 logging.debug(f"Cannot turn {card.name} face up: Cannot afford cost {cost_to_pay_str}.")
                 return False
            if not self.mana_system.pay_mana_cost(player, cost_to_pay_str):
                 logging.warning(f"Failed to pay cost {cost_to_pay_str} for turning {card.name} face up.")
                 return False

        # If cost paid or turning face up for other reason (e.g., effect):
        if not original_info: # If turning up a generic face-down, we might not have original info
             # Maybe check card's own definition if it wasn't morphed/manifested? Complex.
             logging.warning(f"Turning {card.name} face up, but original info unknown (not Morph/Manifest).")
             # Minimal change: just mark face up, assume current stats are correct.
             card.face_down = False
        else:
             # Restore original card properties from original_info dict
             original_card_temp = Card(original_info) # Create temp instance to avoid modifying original_info
             card.name = getattr(original_card_temp, 'name', card.name)
             card.power = getattr(original_card_temp, 'power', card.power)
             card.toughness = getattr(original_card_temp, 'toughness', card.toughness)
             card.card_types = getattr(original_card_temp, 'card_types', card.card_types).copy()
             card.subtypes = getattr(original_card_temp, 'subtypes', card.subtypes).copy()
             card.supertypes = getattr(original_card_temp, 'supertypes', card.supertypes).copy()
             card.oracle_text = getattr(original_card_temp, 'oracle_text', card.oracle_text)
             card.mana_cost = getattr(original_card_temp, 'mana_cost', card.mana_cost)
             card.cmc = getattr(original_card_temp, 'cmc', card.cmc)
             card.colors = getattr(original_card_temp, 'colors', card.colors).copy()
             card.keywords = getattr(original_card_temp, 'keywords', card.keywords).copy()
             card.type_line = getattr(original_card_temp, 'type_line', card.type_line)
             # Restore other necessary attributes

             card.face_down = False
             # Clear from morph/manifest tracking
             if source_mechanic == "Morph": self.morphed_cards.pop(card_id, None)
             if source_mechanic == "Manifest": self.manifested_cards.pop(card_id, None)

        logging.debug(f"Turned {card.name} face up.")
        self.trigger_ability(card_id, "TURNED_FACE_UP")
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects() # Abilities might change
        return True

    def manifest_card(self, player, count=1):
         """Manifest the top card(s) of the library."""
         manifested_ids = []
         for _ in range(count):
             if not player["library"]: break
             card_id = player["library"].pop(0)
             original_info = self._safe_get_card(card_id).__dict__.copy() # Store original data

             # Create face-down creature state
             manifest_data = {
                 "name": "Manifested Creature", # Generic name
                 "power": 2, "toughness": 2,
                 "card_types": ["creature"], "subtypes": [], "supertypes": [],
                 "colors": [0,0,0,0,0], # Colorless
                 "mana_cost": "", "cmc": 0,
                 "oracle_text": "Face-down creature (2/2). Can be turned face up.",
                 "face_down": True
             }
             # Create a new Card object for the face-down state *or* modify existing?
             # Modifying existing is simpler for tracking, but needs careful state management.
             # Let's modify the existing card object in card_db.
             manifested_card = self._safe_get_card(card_id)
             if manifested_card:
                 manifested_card.power = manifest_data["power"]
                 manifested_card.toughness = manifest_data["toughness"]
                 manifested_card.card_types = manifest_data["card_types"]
                 manifested_card.subtypes = manifest_data["subtypes"]
                 # Keep original name/mana cost/etc hidden but associated? Use tracking dict.
                 if not hasattr(self, 'manifested_cards'): self.manifested_cards = {}
                 self.manifested_cards[card_id] = {'original': original_info, 'face_down': True} # Store original
                 manifested_card.face_down = True # Set flag on card object too

                 # Move to battlefield
                 success = self.move_card(card_id, player, "library_implicit", player, "battlefield")
                 if success: manifested_ids.append(card_id)
                 else: # Failed move, undo?
                      player["library"].insert(0, card_id) # Put back
                      if card_id in self.manifested_cards: del self.manifested_cards[card_id] # Clean up tracking
                      manifested_card.face_down = False # Reset flag
         if manifested_ids:
             logging.debug(f"Manifested {len(manifested_ids)} card(s).")
             return manifested_ids
         return None

    def amass(self, player, amount):
        """Perform Amass N. Finds or creates Army token and adds counters."""
        army_token_id = None
        # Find existing Army token
        for cid in player["battlefield"]:
            card = self._safe_get_card(cid)
            if card and "Army" in getattr(card, 'subtypes', []):
                army_token_id = cid
                break
        # Create if doesn't exist
        if not army_token_id:
            token_data = {"name":"Zombie Army", "power":0, "toughness":0, "card_types":["creature"], "subtypes":["Zombie", "Army"], "colors":[0,0,1,0,0]} # Black zombie
            army_token_id = self.create_token(player, token_data)
            if army_token_id:
                logging.debug("Created 0/0 Zombie Army token for Amass.")
            else:
                 logging.error("Failed to create Army token for Amass.")
                 return False

        if army_token_id:
             success = self.add_counter(army_token_id, "+1/+1", amount)
             if success: logging.debug(f"Amass {amount}: Added {amount} +1/+1 counters to Army.")
             return success
        return False

    def adapt(self, player, creature_id, amount):
        """Perform adapt N."""
        card = self._safe_get_card(creature_id)
        # Adapt only if creature has no +1/+1 counters
        if card and getattr(card, 'counters', {}).get('+1/+1', 0) == 0:
            success = self.add_counter(creature_id, '+1/+1', amount)
            if success:
                logging.debug(f"Adapt {amount}: Added {amount} counters to {card.name}.")
                self.trigger_ability(creature_id, "ADAPTED", {"amount": amount})
            return success
        else:
            logging.debug(f"Adapt: Cannot adapt {getattr(card,'name',creature_id)} (already has +1/+1 counters or not found).")
            return False

    def goad_creature(self, target_id):
        """Mark creature as goaded."""
        card = self._safe_get_card(target_id)
        target_owner = self.get_card_controller(target_id)
        if not card or 'creature' not in getattr(card, 'card_types', []) or not target_owner: return False

        # Track goaded status, perhaps on the player dictionary
        target_owner.setdefault("goaded_creatures", set()).add(target_id)
        # Could store turn goaded for duration: target_owner.setdefault("goaded_status", {})[target_id] = self.turn
        logging.debug(f"Goaded {card.name}")
        self.trigger_ability(target_id, "GOADED") # Trigger ability if needed
        return True

    def add_temp_buff(self, card_id, buff_data):
         """Add a temporary buff until end of turn."""
         owner = self._find_card_controller(card_id)
         if owner:
             if not hasattr(owner, 'temp_buffs'): owner['temp_buffs'] = {}
             if card_id not in owner['temp_buffs']: owner['temp_buffs'][card_id] = {'power':0, 'toughness':0, 'until_end_of_turn': True}
             owner['temp_buffs'][card_id]['power'] += buff_data.get('power', 0)
             owner['temp_buffs'][card_id]['toughness'] += buff_data.get('toughness', 0)
             return True
         return False

    def _share_color(self, card1, card2):
        """Check if two cards share a color."""
        if not card1 or not card2 or not hasattr(card1, 'colors') or not hasattr(card2, 'colors'): return False
        # Compare the 5-element color arrays
        return any(c1 and c2 for c1, c2 in zip(card1.colors[:5], card2.colors[:5]))

    # --- Mana System Helper Getters ---
    def _get_equip_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
            match = re.search(r"equip\s*(\{.*?\})", card.oracle_text.lower())
            if match: return match.group(1)
            match = re.search(r"equip\s*(\d+)", card.oracle_text.lower())
            if match: return f"{{{match.group(1)}}}"
        return None

    def _get_fortify_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"fortify\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"fortify\s*(\d+)", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
        return None

    def _get_reconfigure_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"reconfigure\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"reconfigure\s*(\d+)", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
        return None
