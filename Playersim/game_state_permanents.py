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

    def _refresh_control_dependent_effects(self, card_id, controller):
        """Rebind live static/replacement effects after control changes.

        Parsed triggered/activated abilities discover their controller from the
        battlefield when an event or action occurs. Continuous and replacement
        effects, however, snapshot controller data when registered and must be
        rebuilt for CR 611.2c control changes.
        """
        if self.layer_system:
            self.layer_system.remove_effects_by_source(
                card_id,
                preserve_durations={"end_of_turn", "until_your_next_turn"})
            abilities = getattr(
                getattr(self, "ability_handler", None),
                "registered_abilities", {}).get(card_id, [])
            for ability in abilities:
                if type(ability).__name__ == "StaticAbility":
                    ability.apply(self)
            self.layer_system.apply_all_effects()
        if self.replacement_effects:
            self.replacement_effects.remove_effects_by_source(card_id)
            self.replacement_effects.register_card_replacement_effects(
                card_id, controller)

    def _transfer_permanent_control(self, card_id, new_controller):
        """Move one battlefield object and its controller-scoped state."""
        old_controller = self.get_card_controller(card_id)
        if (old_controller is None or new_controller not in (self.p1, self.p2)
                or old_controller is new_controller):
            return False

        try:
            old_controller["battlefield"].remove(card_id)
        except (KeyError, ValueError):
            logging.warning(
                f"Control change: card {card_id} vanished from its controller's battlefield.")
            return False
        new_controller.setdefault("battlefield", []).append(card_id)

        # These stores describe the permanent, not the player who happened to
        # control it when the entry was created. Preserve them across the move.
        set_stores = (
            "tapped_permanents", "entered_battlefield_this_turn",
            "suspected_permanents", "regeneration_shields",
            "activated_this_turn", "targeted_permanents_this_turn",
        )
        for key in set_stores:
            old_store = old_controller.get(key)
            if isinstance(old_store, set) and card_id in old_store:
                old_store.discard(card_id)
                new_controller.setdefault(key, set()).add(card_id)

        # CR 302.6 keys summoning sickness to continuous control since the
        # beginning of the controller's most recent turn, not merely to when
        # the permanent entered. This set is the engine's canonical sickness
        # tracker, so a control change must mark the object even when it has
        # been on the battlefield for several turns. Haste bypasses the check.
        new_controller.setdefault(
            "entered_battlefield_this_turn", set()).add(card_id)

        dict_stores = (
            "loyalty_counters", "damage_counters", "deathtouch_damage",
            "saga_counters", "mutation_stacks", "chosen_creature_types",
            "chosen_colors", "chosen_card_types", "chosen_opponents",
            "chosen_basic_land_types", "as_enters_choices",
        )
        for key in dict_stores:
            old_store = old_controller.get(key)
            if isinstance(old_store, dict) and card_id in old_store:
                new_controller.setdefault(key, {})[card_id] = old_store.pop(card_id)

        # An Aura/Equipment keeps its attachment when its controller changes.
        old_attachments = old_controller.get("attachments")
        if isinstance(old_attachments, dict) and card_id in old_attachments:
            new_controller.setdefault("attachments", {})[card_id] = \
                old_attachments.pop(card_id)

        if hasattr(self, "_last_card_locations"):
            self._last_card_locations[card_id] = (new_controller, "battlefield")
        self._refresh_control_dependent_effects(card_id, new_controller)
        logging.debug(
            f"Control change: {new_controller['name']} now controls "
            f"{getattr(self._safe_get_card(card_id), 'name', card_id)}.")
        return True

    def apply_temporary_control(self, card_id, new_controller):
        """
        Grant temporary control of a card until end of turn.
        
        Args:
            card_id: ID of the card to control temporarily.
            new_controller: The player dictionary who will temporarily control the card.
        
        Returns:
            bool: True if the effect is applied successfully.
        """
        original_controller = self.get_card_controller(card_id)
        if original_controller is None:
            logging.warning(f"Temporary control: Original owner not found for card {card_id}.")
            return False
        if original_controller is new_controller:
            return False
        # Record the original controller if not already stored
        if card_id not in self.temp_control_effects:
            self.temp_control_effects[card_id] = original_controller
        if self._transfer_permanent_control(card_id, new_controller):
            logging.debug(
                f"Temporary control: {new_controller['name']} controls "
                f"{self._safe_get_card(card_id).name} until end of turn.")
            return True
        # A failed transfer must not leave a phantom end-of-turn instruction.
        self.temp_control_effects.pop(card_id, None)
        return False

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
            current_controller = self.get_card_controller(card_id)
            if current_controller and current_controller is not original_controller:
                if self._transfer_permanent_control(card_id, original_controller):
                    logging.debug(
                        f"Temporary control: Reverted control of "
                        f"{self._safe_get_card(card_id).name} back to "
                        f"{original_controller['name']}.")
            # Remove the effect record
            self.temp_control_effects.pop(card_id, None)

    def add_defense_counter(self, card_id, count=1, defer_defeat=False):
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
        if new_defense == 0 and not defer_defeat:
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
        target_controller = self.get_card_controller(target_card_id)
        if not target_controller or target_controller is source_controller:
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
        # Loyalty abilities remain on a permanent even when a type-changing
        # effect (Kaito) makes it stop being a planeswalker.
        has_loyalty_ability = bool(getattr(card, "loyalty_abilities", []))
        tracks_loyalty = card_id in controller.get("loyalty_counters", {})
        if (not card or not has_loyalty_ability or not tracks_loyalty
                or card_id not in controller.get('battlefield', [])):
            logging.warning(f"Invalid attempt to activate loyalty ability from card {card_id}.")
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
        """Attempt to untap a permanent, applying stun replacement (CR 122.1d)."""
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
        card = self._safe_get_card(card_id)
        stun_count = int(getattr(card, "counters", {}).get("stun", 0) or 0) if card else 0
        if stun_count > 0:
             # This is a replacement, not an untap. The attempt still succeeds
             # for effects and untap costs, but the permanent stays tapped and
             # no UNTAPPED event is emitted.
             removed = self.add_counter(card_id, "stun", -1)
             if removed:
                  logging.debug(
                      f"Stun replaced untapping {getattr(card, 'name', card_id)}; "
                      f"{stun_count - 1} stun counter(s) remain."
                  )
             return bool(removed)
        tapped_set.remove(card_id)
        logging.debug(f"Untapped {getattr(card, 'name', card_id)}")
        self.trigger_ability(card_id, "UNTAPPED", {"controller": player})
        return True

    def create_token_copy(self, original_card, controller):
        """Create a token copy of a card, handles details like base P/T."""
        if not original_card: return None
        # Create token tracking if it doesn't exist
        if "tokens" not in controller: controller["tokens"] = []

        token_id = f"TOKEN_COPY_{len(controller['tokens'])}_{original_card.name[:10].replace(' ','')}"

        # CR 707.2 (July 2026): copyable values are the PRINTED characteristics,
        # never the live ones -- the live object carries continuous-effect output
        # (layer write-back mutates power/keywords/colors in place), so reading
        # live attributes copied pumps and granted keywords onto the token.
        # Card.printed() reads the construction-time snapshot; the snapshot the
        # token takes of THESE values then becomes the token's own printed
        # identity, which is exactly what CR 707.2 wants for copies of copies.
        try:
            _pr = original_card.printed if hasattr(original_card, 'printed') else \
                (lambda attr, default=None: getattr(original_card, attr, default))
            copyable_values = {
                "name": _pr("name"),
                "mana_cost": _pr("mana_cost"),
                "color_identity": copy.deepcopy(_pr("colors", [0] * 5)),  # 5-dim vector
                "card_types": copy.deepcopy(_pr("card_types", [])),
                "subtypes": copy.deepcopy(_pr("subtypes", [])),
                "supertypes": copy.deepcopy(_pr("supertypes", [])),
                "oracle_text": _pr("oracle_text", ""),
                # Printed power/toughness/loyalty (no counters, no effects)
                "power": _pr("power", 0),
                "toughness": _pr("toughness", 0),
                "loyalty": _pr("loyalty", 0),
                "keywords": copy.deepcopy(_pr("keywords", [0] * 21)),
                "faces": copy.deepcopy(getattr(original_card, 'faces', None)),  # DFC faces
            }
            copyable_values["is_token"] = True # Mark as token
            copyable_values["type_line"] = _pr("type_line", original_card.type_line) # Copy printed type line

        except Exception as e:
             logging.error(f"Error getting copyable values for {original_card.name}: {e}")
             return None

        try:
            token = Card(copyable_values)
            token.is_token = True  # Card.__init__ ignores the dict key; set explicitly (matters for stats: tokens aren't deck cards)
            token.snapshot_printed()  # the copied values ARE the token's printed identity (CR 707.2)
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

    def create_token(self, controller, token_data, attach_to_target=None):
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
            if "tokens" not in controller:
                controller["tokens"] = []

            # Generate token ID
            token_count = len(controller["tokens"])
            token_id = f"TOKEN_{token_count}_{token_data.get('name', 'Generic').replace(' ', '_')}"
            reserved_ids = set(self.card_db)
            if self.ability_handler:
                reserved_ids.update(
                    getattr(ability, "card_id", None)
                    for ability, *_ in self.ability_handler.active_triggers)
            reserved_ids.update(
                item[1] for item in self.stack
                if isinstance(item, tuple) and len(item) > 1)
            while token_id in reserved_ids:
                token_count += 1
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
            token.is_token = True
            token.card_id = token_id

            # Add token to the card database
            self.card_db[token_id] = token
            move_context = ({"attach_to_target": attach_to_target}
                            if attach_to_target is not None else {})
            if not self.move_card(
                    token_id, controller, "nonexistent_zone", controller,
                    "battlefield", cause="token_creation", context=move_context):
                del self.card_db[token_id]
                return None
            controller["tokens"].append(token_id)
            
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

        # Official keyword counters grant their named ability for as long as
        # the counter remains.  Keep the central check authoritative so
        # targeting, combat, and policy masks all observe the same live state.
        counter_count = int(
            getattr(card, 'counters', {}).get(keyword_lower, 0) or 0)
        if counter_count > 0:
            lost_all = bool(
                self.layer_system
                and self.layer_system.source_has_lost_all_abilities(card_id))
            if not lost_all:
                keyword_names = {
                    str(value).lower() for value in Card.ALL_KEYWORDS}
                if keyword_lower in keyword_names:
                    return True

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
                 # Restrictions such as cant_block are layer-6 abilities but
                 # are intentionally absent from Card.ALL_KEYWORDS. Preserve
                 # them through the layer system's calculated ability sets.
                 return keyword_lower in set(
                     getattr(card, 'active_abilities', set()))
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
                      self._ceased_token_cards[card_id] = self.card_db[card_id]
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
        chosen_targets = [t for t in targets_to_proliferate if t == player or (not isinstance(t, dict) and self.get_card_controller(t) == player)]
        # If the effect specified 'opponent' or 'target', selection would differ.
        # Assuming "You choose..." - AI chooses based on strategy (e.g., buff self, poison opponent)
        # Simplification: affect everything controlled by the player + opponent players
        chosen_targets = []
        for target in valid_targets:
            if target == player: # Target self (player counters)
                chosen_targets.append(target)
            elif target == self._get_non_active_player() and target != player: # Target opponent (player counters)
                chosen_targets.append(target)
            elif not isinstance(target, dict): # Is a permanent ID (ints, not str -- the SBA int/str bug again)
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
                 if not isinstance(target, dict) and self.get_card_controller(target) == player:
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

            elif not isinstance(target, dict): # Permanent card_id (int, not str)
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

    def _is_valid_mutate_target(self, player, target_id):
        target_card = self._safe_get_card(target_id)
        if not player or not target_card or target_id not in player.get("battlefield", []):
            return False
        subtypes = {str(subtype).lower() for subtype in getattr(target_card, 'subtypes', [])}
        return ('creature' in getattr(target_card, 'card_types', [])
                and 'human' not in subtypes)

    def begin_mutate_position_choice(self, player, mutating_card_id, target_id):
        """Pause a resolving mutating spell for its over/under choice."""
        if (not self._safe_get_card(mutating_card_id)
                or not self._is_valid_mutate_target(player, target_id)):
            return False
        if self.phase not in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
            self.previous_priority_phase = self.phase
        self.phase = self.PHASE_CHOOSE
        self.choice_context = {
            "type": "mutate_position",
            "player": player,
            "controller": player,
            "mutating_card_id": mutating_card_id,
            "target_id": target_id,
            "available_modes": ["over", "under"],
        }
        self.priority_player = player
        self.priority_pass_count = 0
        return True

    def complete_mutate_position_choice(self, mutate_on_top):
        """Apply the pending merge after the controller chooses over or under."""
        context = self.choice_context
        if not context or context.get("type") != "mutate_position":
            return False
        player = context.get("player")
        mutating_card_id = context.get("mutating_card_id")
        target_id = context.get("target_id")
        return_phase = self.previous_priority_phase
        self.choice_context = None
        self.previous_priority_phase = None
        self.phase = return_phase if return_phase is not None else self.PHASE_PRIORITY
        self.priority_player = self._get_active_player()
        self.priority_pass_count = 0
        return self.mutate(player, mutating_card_id, target_id, bool(mutate_on_top))

    def mutate(self, player, mutating_card_id, target_id, mutate_on_top=True):
        """Merge a resolving mutating creature spell with its legal target."""
        target_card = self._safe_get_card(target_id)
        mutating_card = self._safe_get_card(mutating_card_id)
        if (not target_card or not mutating_card
                or not self._is_valid_mutate_target(player, target_id)):
            logging.warning(f"Mutate failed for {mutating_card_id} onto {target_id}.")
            return False

        existing = self.mutated_permanents.get(target_id)
        if existing:
            components = list(existing.get("components", [target_id]))
            component_printed = copy.deepcopy(existing.get("component_printed", {}))
            component_owner_keys = dict(existing.get("component_owner_keys", {}))
        else:
            components = [target_id]
            component_printed = {
                target_id: copy.deepcopy(getattr(target_card, "_printed", {})),
            }
            target_owner = self._find_card_owner_fallback(target_id) or player
            component_owner_keys = {
                target_id: "p1" if target_owner is self.p1 else "p2",
            }
        if mutating_card_id in components:
            return False
        mutating_owner = self._find_card_owner_fallback(mutating_card_id) or player
        component_owner_keys[mutating_card_id] = (
            "p1" if mutating_owner is self.p1 else "p2")
        component_printed[mutating_card_id] = copy.deepcopy(
            getattr(mutating_card, "_printed", {}))
        if mutate_on_top:
            components.insert(0, mutating_card_id)
        else:
            components.append(mutating_card_id)

        top_printed = copy.deepcopy(component_printed[components[0]])
        ability_texts = [
            str(component_printed[component_id].get("oracle_text", "")).strip()
            for component_id in components
            if component_printed.get(component_id, {}).get("oracle_text")
        ]
        top_printed["oracle_text"] = "\n".join(ability_texts)
        top_printed["keywords"] = target_card._extract_keywords(
            top_printed["oracle_text"].lower())

        if self.ability_handler:
            self.ability_handler.unregister_card_abilities(target_id)
        target_card._printed = top_printed
        target_card.reset_to_printed()
        target_card.compute_subtype_vector()
        self.mutated_permanents[target_id] = {
            "components": components,
            "component_printed": component_printed,
            "component_owner_keys": component_owner_keys,
            "top_card_id": components[0],
            "mutation_count": int(existing.get("mutation_count", 0)) + 1 if existing else 1,
        }
        player.setdefault("mutation_stacks", {})[target_id] = list(components)
        if hasattr(self, "_last_card_locations"):
            self._last_card_locations[mutating_card_id] = (player, "merged")
        if self.ability_handler:
            self.ability_handler.register_card_abilities(target_id, player)
        if self.layer_system:
            self.layer_system.invalidate_cache()
            self.layer_system.apply_all_effects()

        self.trigger_ability(target_id, "MUTATES", {
            "controller": player,
            "target_id": target_id,
            "mutating_card_id": mutating_card_id,
            "top_card_id": components[0],
            "mutation_count": self.mutated_permanents[target_id]["mutation_count"],
        })
        logging.debug(
            f"{mutating_card.name} mutated {'over' if mutate_on_top else 'under'} "
            f"{target_id}; merged top is {target_card.name}.")
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

    def meld_cards(self, primary_id, partner_id, result_id, controller):
        """Exile two meld parts and return one combined permanent (CR 713)."""
        if not controller or primary_id == partner_id:
            return False
        battlefield = controller.get("battlefield", [])
        if primary_id not in battlefield or partner_id not in battlefield:
            return False

        primary = self._safe_get_card(primary_id)
        partner = self._safe_get_card(partner_id)
        result = self._safe_get_card(result_id)
        if not primary or not partner or not result:
            return False
        # A meld instruction can combine only two cards the resolving
        # controller both owns and controls. Do not silently use current
        # battlefield control as an ownership approximation.
        if (self._find_card_owner_fallback(primary_id) is not controller
                or self._find_card_owner_fallback(partner_id) is not controller):
            return False

        expected_partner = getattr(primary, "meld_partner_name", None)
        expected_result = getattr(primary, "meld_result_name", None)
        if expected_partner and partner.name.lower() != expected_partner.lower():
            return False
        if expected_result and result.name.lower() != expected_result.lower():
            return False

        original_printed = copy.deepcopy(getattr(primary, "_printed", {}))
        if not self.move_card(
                primary_id, controller, "battlefield", controller, "exile",
                cause="meld_component"):
            return False
        if not self.move_card(
                partner_id, controller, "battlefield", controller, "exile",
                cause="meld_component"):
            self.move_card(primary_id, controller, "exile", controller, "battlefield",
                           cause="meld_rollback")
            return False

        primary._printed = copy.deepcopy(getattr(result, "_printed", {}))
        primary.reset_to_printed()
        self.melded_permanents[primary_id] = {
            "partner_id": partner_id,
            "result_id": result_id,
            "original_printed": original_printed,
        }
        if not self.move_card(
                primary_id, controller, "exile", controller, "battlefield",
                cause="meld_result"):
            self.melded_permanents.pop(primary_id, None)
            primary._printed = original_printed
            primary.reset_to_printed()
            self.move_card(primary_id, controller, "exile", controller, "battlefield",
                           cause="meld_rollback")
            self.move_card(partner_id, controller, "exile", controller, "battlefield",
                           cause="meld_rollback")
            return False

        if self.layer_system:
            self.layer_system.invalidate_cache()
            self.layer_system.apply_all_effects()
        logging.debug(
            f"Melded {primary.name} from components {primary_id} and {partner_id}.")
        return True

    @staticmethod
    def _specialize_color_letters(card):
        color_order = "WUBRG"
        colors = getattr(card, "colors", []) or []
        letters = {
            color for index, color in enumerate(color_order)
            if index < len(colors) and colors[index]
        }
        subtype_colors = {
            "plains": "W", "island": "U", "swamp": "B",
            "mountain": "R", "forest": "G",
        }
        for subtype in getattr(card, "subtypes", []) or []:
            color = subtype_colors.get(str(subtype).lower())
            if color:
                letters.add(color)
        return letters

    def get_specialize_variants(self, source_id):
        """Map WUBRG to linked specialization card IDs available in card_db."""
        source = self._safe_get_card(source_id)
        if not source or not getattr(source, "is_specialize", False):
            return {}
        related_names = {
            part.get("name")
            for part in getattr(source, "all_parts", [])
            if isinstance(part, dict) and part.get("component") == "combo_piece"
        }
        if not related_names:
            return {}

        source_colors = self._specialize_color_letters(source)
        variants = {}
        for variant_id, variant in self.card_db.items():
            if variant_id == source_id or getattr(variant, "name", None) not in related_names:
                continue
            variant_colors = self._specialize_color_letters(variant)
            added_colors = variant_colors - source_colors
            color = None
            if len(added_colors) == 1:
                color = next(iter(added_colors))
            elif variant_colors == source_colors and len(source_colors) == 1:
                color = next(iter(source_colors))
            if color:
                variants[color] = variant_id
        if len(variants) == 5:
            return variants

        log_key = ("missing_specialize_variants", source_id)
        logged_errors = getattr(self, "_logged_errors", set())
        if log_key not in logged_errors:
            logged_errors.add(log_key)
            logging.warning(
                f"Specialize source {source.name} has {len(variants)}/5 linked variants in card_db.")
            counters = getattr(self, "fidelity_counters", None)
            if counters is not None:
                counters["unparsed_effects"] += 1
                counters.setdefault("unparsed_cards", set()).add(source.name)
            try:
                from .card_support import report_unsupported
                report_unsupported(
                    source.name,
                    f"specialize linked variants missing ({len(variants)}/5 loaded)",
                    severity="unparsed")
            except Exception:
                pass
        return {}

    def get_specialize_discard_colors(self, card_id):
        card = self._safe_get_card(card_id)
        return self._specialize_color_letters(card) if card else set()

    def start_specialize_choice(self, source_id, controller):
        """Begin Specialize without paying either activation cost yet."""
        source = self._safe_get_card(source_id)
        if (not source or source_id not in controller.get("battlefield", [])
                or not getattr(source, "is_specialize", False)
                or source_id in self.specialized_cards
                or not self._can_act_at_sorcery_speed(controller)
                or self.priority_player != controller
                or self.choice_context):
            return False

        variants = self.get_specialize_variants(source_id)
        cost = getattr(source, "specialize_cost", None)
        if not variants or not cost or not self.mana_system.can_pay_mana_cost(
                controller, cost, {"card_id": source_id, "is_ability": True}):
            return False
        eligible = [
            card_id for card_id in controller.get("hand", [])
            if self.get_specialize_discard_colors(card_id).intersection(variants)
        ]
        if not eligible:
            return False

        self.previous_priority_phase = self.phase
        self.phase = self.PHASE_CHOOSE
        self.choice_context = {
            "type": "specialize_discard",
            "player": controller,
            "controller": controller,
            "source_id": source_id,
            "cost": cost,
            "available_colors": sorted(variants),
        }
        self.priority_player = controller
        self.priority_pass_count = 0
        return True

    def choose_specialize_discard(self, hand_index):
        context = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and context
                and context.get("type") == "specialize_discard"):
            return False
        controller = context.get("controller") or context.get("player")
        hand = controller.get("hand", []) if controller else []
        if not isinstance(hand_index, int) or not 0 <= hand_index < len(hand):
            return False

        discard_id = hand[hand_index]
        variants = self.get_specialize_variants(context.get("source_id"))
        colors = sorted(self.get_specialize_discard_colors(discard_id).intersection(variants))
        if not colors:
            return False
        context["discard_card_id"] = discard_id
        context["available_colors"] = colors
        if len(colors) > 1:
            context["type"] = "choose_color"
            context["resume_specialize"] = True
            return True
        return self.complete_specialize_choice(colors[0])

    def complete_specialize_choice(self, color):
        context = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and context
                and context.get("resume_specialize", context.get("type") == "specialize_discard")):
            return False
        controller = context.get("controller") or context.get("player")
        source_id = context.get("source_id")
        discard_id = context.get("discard_card_id")
        available_colors = set(context.get("available_colors", []))
        variants = self.get_specialize_variants(source_id)
        variant_id = variants.get(color)
        source = self._safe_get_card(source_id)
        variant = self._safe_get_card(variant_id)
        cost = context.get("cost")
        if (color not in available_colors or not controller or not source or not variant
                or source_id not in controller.get("battlefield", [])
                or discard_id not in controller.get("hand", [])
                or color not in self.get_specialize_discard_colors(discard_id)
                or not self.mana_system.can_pay_mana_cost(
                    controller, cost, {"card_id": source_id, "is_ability": True})):
            return False

        payment_context = {
            "card_id": source_id,
            "is_ability": True,
            "cause": "specialize",
        }
        if not self.mana_system.pay_mana_cost(controller, cost, payment_context):
            return False
        if not self.discard_card(
                controller, discard_id, source_id=source_id, cause="specialize_cost"):
            logging.error("Specialize paid mana but could not discard its validated card.")
            return False

        original_name = source.name
        original_printed = copy.deepcopy(getattr(source, "_printed", {}))
        if self.ability_handler:
            self.ability_handler.unregister_card_abilities(source_id)
        source._printed = copy.deepcopy(getattr(variant, "_printed", {}))
        source.reset_to_printed()
        self.specialized_cards[source_id] = {
            "color": color,
            "variant_id": variant_id,
            "discarded_card_id": discard_id,
            "original_printed": original_printed,
        }
        if self.ability_handler:
            self.ability_handler.register_card_abilities(source_id, controller)

        return_phase = self.previous_priority_phase
        self.choice_context = None
        self.previous_priority_phase = None
        self.phase = return_phase if return_phase is not None else self.PHASE_PRIORITY
        self.priority_player = controller
        self.priority_pass_count = 0
        self.trigger_ability(source_id, "SPECIALIZES", {
            "controller": controller,
            "color": color,
            "discarded_card_id": discard_id,
            "from_name": original_name,
            "to_name": source.name,
        })
        logging.debug(f"Specialized {original_name} into {source.name} ({color}).")
        return True

    def transform_card(self, card_id):
        """Transform a double-faced permanent to its other face (CR 712).

        The generic transform primitive for TransformEffect and for
        triggered/activated 'transform' abilities. TransformEffect already
        called gs.transform_card() assuming it existed - it did not, so every
        parsed 'transform ~' effect logged an error and silently did nothing.
        This flips the face via Card.transform(), recomputes continuous effects
        (the new face has different P/T, types, and abilities), and fires the
        TRANSFORMED trigger - matching the day/night transform path.

        Modal DFCs are excluded: they are cast face-up, not transformed.

        Returns True if the permanent transformed, False otherwise.
        """
        card = self._safe_get_card(card_id)
        player = self.get_card_controller(card_id)
        if not card or not hasattr(card, 'transform'):
            return False
        # Only transforming DFCs transform; MDFCs and single-faced cards cannot.
        if not getattr(card, 'faces', None) or len(card.faces) < 2:
            logging.debug(f"transform_card: {getattr(card, 'name', card_id)} has no alternate face.")
            return False
        if hasattr(card, 'is_transforming_mdfc') and not card.is_transforming_mdfc():
            logging.debug(f"transform_card: {getattr(card, 'name', card_id)} is a modal DFC; cannot transform.")
            return False

        prev_face = getattr(card, 'current_face', 0)
        if self.ability_handler and player:
            self.ability_handler.unregister_card_abilities(card_id)
        card.transform()
        if getattr(card, 'current_face', prev_face) == prev_face:
            if self.ability_handler and player:
                self.ability_handler.register_card_abilities(card_id, player)
            logging.debug(f"transform_card: {getattr(card, 'name', card_id)} did not change face.")
            return False

        if self.ability_handler and player:
            self.ability_handler.register_card_abilities(card_id, player)

        # New face => different P/T, types, keywords: force a full recompute.
        if self.layer_system:
            self.layer_system.invalidate_cache()
            self.layer_system.apply_all_effects()

        self.trigger_ability(card_id, "TRANSFORMED", {"controller": player, "card": card})
        logging.debug(f"transform_card: transformed to face {getattr(card, 'current_face', None)} "
                      f"({getattr(card, 'name', card_id)}).")
        return True

    def _build_type_line(self, type_data):
        """Construct a canonical type-line string from type components.

        Produces "supertypes card_types - subtypes" (em dash) so that
        Card.parse_type_line() round-trips the components back correctly.
        Token creation depends on this: Card.__init__ ALWAYS re-derives
        card_types/subtypes/supertypes from the type_line string, so a token
        built with only a crude 'Token Creature' line silently drops every
        subtype and supertype (a Goblin's Offspring token became a typeless
        Creature, corrupting tribal/type statistics). Previously this method
        was called but never defined, so that crude fallback always ran.
        """
        supertypes = type_data.get('supertypes', []) or []
        card_types = type_data.get('card_types', []) or []
        subtypes = type_data.get('subtypes', []) or []
        main = " ".join([str(t) for t in list(supertypes) + list(card_types)]).strip()
        if subtypes:
            return f"{main} — {' '.join(str(s) for s in subtypes)}".strip()
        return main or "Creature"

    def begin_forced_sacrifice(self, player, count, source_id):
        """Open a forced-sacrifice choice: player must pick count of their own
        permanents to sacrifice, one action per pick (Phyrexian Obliterator)."""
        if self.phase not in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
            self.previous_priority_phase = self.phase
        self.phase = self.PHASE_CHOOSE
        self.choice_context = {
            "type": "forced_sacrifice",
            "player": player,
            "remaining": count,
            "source_id": source_id,
            "choice_page": 0,
        }
        self.priority_player = player
        self.priority_pass_count = 0
        logging.debug(f"{player['name']} must sacrifice {count} permanent(s) of their choice.")

    def complete_forced_sacrifice_choice(self, option_index):
        """Sacrifice the picked battlefield permanent (index into the first 10
        battlefield slots). Closes the choice when the count is met or the
        battlefield empties."""
        ctx = getattr(self, 'choice_context', None)
        if not ctx or ctx.get("type") != "forced_sacrifice":
            logging.warning("complete_forced_sacrifice_choice called without context.")
            return False
        player = ctx.get("player")
        options = player.get("battlefield", [])
        absolute_index = int(ctx.get("choice_page", 0)) * 10 + option_index
        if (not isinstance(option_index, int)
                or not (0 <= absolute_index < len(options))):
            logging.warning(f"Invalid forced-sacrifice option index {option_index}.")
            return False
        card_id = options[absolute_index]
        if not self.move_card(card_id, player, "battlefield", player, "graveyard",
                              cause="sacrifice"):
            logging.warning(f"Forced sacrifice could not move {card_id} to the graveyard.")
            return False
        self.trigger_ability(card_id, "SACRIFICED", {"controller": player})
        ctx["remaining"] = ctx.get("remaining", 1) - 1
        if ctx["remaining"] > 0 and player.get("battlefield"):
            ctx["choice_page"] = 0
            return True
        self.choice_context = None
        if getattr(self, 'previous_priority_phase', None) is not None:
            self.phase = self.previous_priority_phase
            self.previous_priority_phase = None
        else:
            self.phase = self.PHASE_PRIORITY
        self.priority_player = self._get_active_player()
        self.priority_pass_count = 0
        return True

    def _creature_type_choice_options(self, player):
        """Creature subtypes among the player's own cards, most frequent first
        (the 10-option action range bounds an as-enters creature-type choice)."""
        counts = {}
        for zone in ("battlefield", "hand", "library", "graveyard"):
            for cid in player.get(zone, []):
                card = self._safe_get_card(cid)
                if not card or 'creature' not in [t.lower() for t in getattr(card, 'card_types', [])]:
                    continue
                for subtype in getattr(card, 'subtypes', []):
                    key = str(subtype).lower()
                    counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts, key=lambda s: (-counts[s], s))
        return ranked[:10]

    def _as_enters_choice_options(self, choice_kind, player):
        """Return the bounded policy options for a parsed as-enters choice."""
        if choice_kind == "creature_type":
            return self._creature_type_choice_options(player)
        if choice_kind == "color":
            return ["W", "U", "B", "R", "G"]
        if choice_kind == "card_type":
            return [
                "artifact", "battle", "creature", "enchantment", "instant",
                "kindred", "land", "planeswalker", "sorcery",
            ]
        if choice_kind == "basic_land_type":
            return ["plains", "island", "swamp", "mountain", "forest"]
        if choice_kind == "pay_life":
            return (["pay_2_life", "decline"]
                    if int(player.get("life", 0)) >= 2 else ["decline"])
        if choice_kind == "opponent":
            return [key for key, candidate in (("p1", self.p1), ("p2", self.p2))
                    if candidate is not None and candidate is not player]
        return []

    def _finish_battlefield_entry_triggers(self, card_id, player,
                                           enter_context=None):
        """Fire entry events after every mandatory as-enters choice is set."""
        card = self._safe_get_card(card_id)
        context = dict(enter_context or {})
        context.update({
            "controller": player, "to_zone": "battlefield",
            "card_id": card_id,
        })
        self.trigger_ability(card_id, "ENTERS_BATTLEFIELD", context)
        if card and "land" in getattr(card, "card_types", []):
            self.trigger_ability(None, "LANDFALL", context)
            self.trigger_ability(card_id, "LANDFALL_SELF", context)
        if card and "saga" in [
                str(subtype).lower()
                for subtype in getattr(card, "subtypes", [])]:
            player.setdefault("saga_counters", {})[card_id] = 1
            self.trigger_ability(card_id, "SAGA_CHAPTER", {
                "chapter": 1, "controller": player,
            })
            logging.debug(
                f"Saga {getattr(card, 'name', card_id)} entered with lore "
                "counter 1 (chapter I).")

    def complete_as_enters_choice(self, option_index):
        """Commit a generic as-enters choice, then release deferred events."""
        ctx = getattr(self, 'choice_context', None)
        choice_type = ctx.get("type", "") if ctx else ""
        if not choice_type.startswith("as_enters_"):
            logging.warning("complete_as_enters_choice called without context.")
            return False
        options = ctx.get("options", [])
        if not isinstance(option_index, int) or not (0 <= option_index < len(options)):
            logging.warning(f"Invalid as-enters option index {option_index}.")
            return False
        player = ctx.get("player")
        card_id = ctx.get("card_id")
        chosen = options[option_index]
        choice_kind = choice_type[len("as_enters_"):]

        # The second half of a Multiversal Passage entry is a real optional
        # life payment, not an unconditional tapped-entry sentence.  It shares
        # the as-enters choice channel so the first choice and the payment are
        # atomic with respect to deferred ETB/landfall triggers.
        if choice_kind == "pay_life":
            player = ctx.get("player")
            card_id = ctx.get("card_id")
            if not player or chosen not in ("pay_2_life", "decline"):
                logging.warning("Invalid as-enters life-payment choice.")
                return False
            if chosen == "pay_2_life":
                if int(player.get("life", 0)) < 2:
                    logging.warning("Cannot pay 2 life for as-enters choice.")
                    return False
                player["life"] -= 2
                player["lost_life_this_turn"] = True
                self.trigger_ability(card_id, "LOSE_LIFE", {
                    "player": player, "amount": 2,
                    "source_id": card_id, "cause": "cost",
                })
            else:
                player.setdefault("tapped_permanents", set()).add(card_id)
            enter_context = ctx.get("enter_context")
            card = self._safe_get_card(card_id)
            logging.debug(
                f"{getattr(card, 'name', card_id)}: "
                f"{'paid 2 life' if chosen == 'pay_2_life' else 'entered tapped'}.")
            self.choice_context = None
            if getattr(self, 'previous_priority_phase', None) is not None:
                self.phase = self.previous_priority_phase
                self.previous_priority_phase = None
            else:
                self.phase = self.PHASE_PRIORITY
            self.priority_player = player
            self.priority_pass_count = 0
            self._finish_battlefield_entry_triggers(
                card_id, player, enter_context)
            return True

        stores = {
            "creature_type": "chosen_creature_types",
            "color": "chosen_colors",
            "card_type": "chosen_card_types",
            "basic_land_type": "chosen_basic_land_types",
            "opponent": "chosen_opponents",
        }
        store_name = stores.get(choice_kind)
        if not player or not store_name:
            logging.warning(f"Unsupported as-enters choice kind {choice_kind}.")
            return False
        player.setdefault(store_name, {})[card_id] = chosen
        player.setdefault("as_enters_choices", {}).setdefault(card_id, {})[
            choice_kind] = chosen
        card = self._safe_get_card(card_id)
        logging.debug(
            f"{getattr(card, 'name', card_id)}: chose {choice_kind} '{chosen}'.")
        enter_context = ctx.get("enter_context")

        # Capture the printed continuation before CR 305.7 strips the live
        # land's rules text in layer 4.
        oracle_text = (getattr(card, "oracle_text", "") or "").lower()
        if choice_kind == "basic_land_type" and self.layer_system:
            # Setting a basic land type is CR 305.7, rather than merely adding
            # a decorative subtype: it supplies that type's intrinsic mana
            # ability and replaces the land's rules-text abilities.
            self.layer_system.register_effect({
                "source_id": card_id,
                "layer": 4,
                "affected_ids": [card_id],
                "effect_type": "set_basic_land_type",
                "effect_value": chosen,
                "duration": "permanent",
            })
            self.layer_system.apply_all_effects()

        if (choice_kind == "basic_land_type"
                and "then you may pay 2 life" in oracle_text
                and "if you don't, it enters tapped" in oracle_text):
            # The layer application above intentionally removes the land's
            # printed rules text, so detect this continuation from the card
            # text captured before clearing/replacing the choice context.
            can_pay = int(player.get("life", 0)) >= 2
            ctx["type"] = "as_enters_pay_life"
            ctx["options"] = (["pay_2_life", "decline"]
                              if can_pay else ["decline"])
            self.choice_context = ctx
            self.phase = self.PHASE_CHOOSE
            self.priority_player = player
            self.priority_pass_count = 0
            return True

        self.choice_context = None
        if getattr(self, 'previous_priority_phase', None) is not None:
            self.phase = self.previous_priority_phase
            self.previous_priority_phase = None
        else:
            self.phase = self.PHASE_PRIORITY
        self.priority_player = player
        self.priority_pass_count = 0
        self._finish_battlefield_entry_triggers(
            card_id, player, enter_context)
        return True

    def complete_as_enters_creature_type(self, option_index):
        """Compatibility wrapper for the original Cavern-specific path."""
        ctx = getattr(self, "choice_context", None)
        if not ctx or ctx.get("type") != "as_enters_creature_type":
            logging.warning("complete_as_enters_creature_type called without context.")
            return False
        return self.complete_as_enters_choice(option_index)

    def _register_attachment_effects(self, attach_id, target_id):
        """Register an attachment's static P/T and keyword grants on its target.

        First-touch sweep (July 2026): equipment/aura P/T bonuses never entered
        the layer system at all -- "Equipped creature gets +2/+2" was parsed by
        nothing and applied nowhere, so every Equipment and every stat-granting
        Aura was cosmetic. Registers layer 7c (P/T) and layer 6 (keywords)
        effects source-keyed to the attachment, so remove_effects_by_source at
        unattach cleans them up. Parses the common templates:
        "Equipped/Enchanted creature gets +X/+Y" and "... gains <keyword>".
        """
        import re as _re
        if not self.layer_system:
            return
        card = self._safe_get_card(attach_id)
        text = getattr(card, 'oracle_text', '') or ''
        low = text.lower()
        # Clear any prior registration for this attachment (re-attach case).
        self.layer_system.remove_effects_by_source(attach_id, effect_description_contains="attachment:")
        # P/T: "gets +A/+B" or "-A/-B" (either sign per component).
        m = _re.search(r"(?:equipped|enchanted)\s+creature\s+gets\s+([+-]\d+)/([+-]\d+)", low)
        if m and "for each enchantment" not in low:
            self.layer_system.register_effect({
                'source_id': attach_id, 'layer': 7, 'sublayer': 'c',
                'affected_ids': [target_id], 'effect_type': 'modify_pt',
                'effect_value': (int(m.group(1)), int(m.group(2))),
                'duration': 'until_source_leaves',
                'description': f"attachment: {getattr(card,'name',attach_id)} P/T",
            })
        if "enchanted creature has base power and toughness 1/1" in low:
            self.layer_system.register_effect({
                'source_id': attach_id, 'layer': 7, 'sublayer': 'b',
                'affected_ids': [target_id], 'effect_type': 'set_pt',
                'effect_value': (1, 1), 'duration': 'until_source_leaves',
                'description': f"attachment: {getattr(card,'name',attach_id)} base P/T",
            })
        if _re.search(
                r"enchanted\s+creature\s+gets\s+\+1/\+1\s+for each "
                r"enchantment you control", low):
            self.layer_system.register_effect({
                'source_id': attach_id, 'layer': 7, 'sublayer': 'c',
                'affected_ids': [target_id],
                'effect_type': 'modify_pt_enchantments_controller',
                'effect_value': None,
                'controller_id': self.get_card_controller(attach_id),
                'duration': 'until_source_leaves',
                'description': f"attachment: {getattr(card,'name',attach_id)} enchantment count",
            })
        # Keyword grants: "and gains flying", "has trample", etc.
        for kw in Card.ALL_KEYWORDS:
            if _re.search(r"(?:gains|has|have)\s+[^.]*\b" + _re.escape(kw) + r"\b", low):
                self.layer_system.register_effect({
                    'source_id': attach_id, 'layer': 6,
                    'affected_ids': [target_id], 'effect_type': 'add_ability',
                    'effect_value': kw, 'duration': 'until_source_leaves',
                    'description': f"attachment: {getattr(card,'name',attach_id)} grants {kw}",
                })

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

        if "attachments" not in player: player["attachments"] = {}
        # Remove previous attachment of this equipment, if any
        if equip_id in player["attachments"]:
            logging.debug(f"Unequipping {equip_card.name} from previous target {player['attachments'][equip_id]}")
            del player["attachments"][equip_id]
        # Attach to new target
        player["attachments"][equip_id] = target_id
        logging.debug(f"Equipped {equip_card.name} to {target_card.name}")
        self._register_attachment_effects(equip_id, target_id)
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(equip_id, "EQUIPPED", {"target_id": target_id})
        self.trigger_ability(target_id, "BECAME_EQUIPPED", {"equipment_id": equip_id})
        return True

    def unequip_permanent(self, player, equip_id):
        """Unequip an equipment."""
        if "attachments" in player and equip_id in player["attachments"]:
            equip_name = getattr(self._safe_get_card(equip_id), 'name', equip_id)
            target_id = player["attachments"].pop(equip_id)
            logging.debug(f"Unequipped {equip_name} from {target_id}")
            if self.layer_system:
                self.layer_system.remove_effects_by_source(equip_id, effect_description_contains="attachment:")
                self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
            # Trigger unequipped events? (Less common than equip)
            return True
        logging.debug(f"Cannot unequip {equip_id}: Not attached.")
        return False # Wasn't attached

    def attach_aura(self, player, aura_id, target_id):
        """Attach an Aura to a legal battlefield object."""
        if "attachments" not in player: player["attachments"] = {}
        aura_card = self._safe_get_card(aura_id)
        target_card = self._safe_get_card(target_id)
        aura_name = getattr(aura_card, 'name', aura_id)
        target_name = getattr(target_card, 'name', target_id)

        # Find target's actual location/controller for validation
        target_owner, target_zone = self.find_card_location(target_id)
        if target_zone != "battlefield":
             logging.warning(f"Cannot attach {aura_name}: Target {target_name} not on battlefield.")
             return False
        if not self._is_legal_attachment(aura_id, target_id):
             logging.warning(f"Cannot attach {aura_name}: {target_name} is not a legal enchanted object.")
             return False

        if aura_id in player["attachments"]:
            logging.debug(f"Re-attaching {aura_name} from {player['attachments'][aura_id]} to {target_name}")
            del player["attachments"][aura_id]

        player["attachments"][aura_id] = target_id
        logging.debug(f"Attached {aura_name} to {target_name}")
        self._register_attachment_effects(aura_id, target_id)
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

        if "attachments" not in player: player["attachments"] = {}
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

        if "attachments" not in player: player["attachments"] = {}
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
        if not card or card_id not in player["battlefield"]:
            return False

        original_printed = None
        cost_to_pay_str = None
        source_mechanic = None
        manifest_info = getattr(self, "manifested_cards", {}).get(card_id)
        if manifest_info and manifest_info.get("face_down", True):
            source_mechanic = "Manifest"
            original_printed = manifest_info.get("original_printed")
            if pay_manifest_cost:
                if (not original_printed
                        or "creature" not in original_printed.get("card_types", [])):
                    return False
                cost_to_pay_str = original_printed.get("mana_cost", "")

        morph_info = getattr(self, "morphed_cards", {}).get(card_id)
        if source_mechanic is None and morph_info and morph_info.get("face_down", True):
            source_mechanic = "Morph"
            original_printed = morph_info.get("original_printed")
            if original_printed is None and morph_info.get("original"):
                original_card = Card(morph_info["original"])
                original_printed = copy.deepcopy(original_card._printed)
            if pay_morph_cost and original_printed:
                match = re.search(
                    r"morph\s*((?:\{[^}]+\})+)",
                    original_printed.get("oracle_text", ""), re.IGNORECASE)
                cost_to_pay_str = match.group(1) if match else None

        if source_mechanic is None or not original_printed:
            return False
        if pay_manifest_cost or pay_morph_cost:
            if not cost_to_pay_str:
                return False
            parsed_cost = self.mana_system.parse_mana_cost(cost_to_pay_str)
            # Match the mask's untapped-land affordability; payment auto-taps.
            if not self.mana_system.can_pay_mana_cost_with_lands(player, parsed_cost):
                return False
            if self.mana_system.pay_mana_cost_get_details(
                    player, parsed_cost) is None:
                return False

        if self.ability_handler:
            self.ability_handler.unregister_card_abilities(card_id)
        card._printed = copy.deepcopy(original_printed)
        card.reset_to_printed()
        card.face_down = False
        if source_mechanic == "Manifest":
            self.manifested_cards.pop(card_id, None)
        else:
            self.morphed_cards.pop(card_id, None)
        if self.ability_handler:
            self.ability_handler.register_card_abilities(card_id, player)
        if self.layer_system:
            self.layer_system.invalidate_cache()
            self.layer_system.apply_all_effects()
        self.trigger_ability(
            card_id, "TURNED_FACE_UP", {"controller": player})
        return True

    @staticmethod
    def _face_down_creature_printed():
        return {
            "name": "Face-down creature",
            "mana_cost": "",
            "cmc": 0,
            "colors": [0, 0, 0, 0, 0],
            "card_types": ["creature"],
            "subtypes": [],
            "supertypes": [],
            "type_line": "Creature",
            "oracle_text": "",
            "keywords": [0] * len(Card.ALL_KEYWORDS),
            "power": 2,
            "toughness": 2,
            "loyalty": None,
            "defense": None,
        }

    def manifest_selected_card(self, player, card_id,
                               source_zone="library"):
        """Put one physical card onto the battlefield face down as a 2/2."""
        card = self._safe_get_card(card_id)
        if not card:
            return False
        if source_zone != "library_implicit" and card_id not in player.get(source_zone, []):
            return False
        original_printed = copy.deepcopy(getattr(card, "_printed", {}))
        if not original_printed:
            card.snapshot_printed()
            original_printed = copy.deepcopy(card._printed)
        card._printed = self._face_down_creature_printed()
        card.reset_to_printed()
        card.face_down = True
        self.manifested_cards[card_id] = {
            "original_printed": original_printed,
            "face_down": True,
        }
        if self.move_card(
                card_id, player, source_zone, player, "battlefield",
                cause="manifest"):
            return True
        self.manifested_cards.pop(card_id, None)
        card._printed = original_printed
        card.reset_to_printed()
        card.face_down = False
        return False

    def manifest_card(self, player, count=1):
         """Manifest the top card(s) of the library."""
         manifested_ids = []
         for _ in range(max(0, int(count))):
             if not player.get("library"):
                 break
             card_id = player["library"][0]
             if self.manifest_selected_card(player, card_id, "library"):
                 manifested_ids.append(card_id)
             else:
                 break
         return manifested_ids or None

    def complete_manifest_dread_choice(self, option_index):
        choice = getattr(self, "choice_context", None)
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "manifest_dread"):
            return False
        options = list(choice.get("options", []))
        if not isinstance(option_index, int) or not 0 <= option_index < len(options):
            return False
        controller = choice.get("controller") or choice.get("player")
        selected_id = options[option_index]
        graveyard_id = options[1 - option_index]
        if not self.manifest_selected_card(
                controller, selected_id, "library_implicit"):
            controller["library"][:0] = options
            return False
        if not self.move_card(
                graveyard_id, controller, "library_implicit", controller,
                "graveyard", cause="manifest_dread"):
            self.move_card(
                selected_id, controller, "battlefield", controller,
                "library", cause="manifest_dread_rollback")
            controller["library"].insert(
                0 if option_index == 0 else 1, graveyard_id)
            return False

        return_phase = self.previous_priority_phase
        self.choice_context = None
        self.previous_priority_phase = None
        self.phase = return_phase if return_phase is not None else self.PHASE_PRIORITY
        self.priority_player = controller
        self.priority_pass_count = 0
        self.trigger_ability(choice.get("source_id"), "MANIFEST_DREAD", {
            "controller": controller,
            "manifested_card_id": selected_id,
            "graveyard_card_id": graveyard_id,
        })
        return True

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

