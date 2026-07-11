"""Game setup, mulligans, and effect registration.

Extracted from game_state.py. This module defines behavior only (a mixin);
all state lives on GameState itself, which composes every mixin.
"""

import random
import logging
from .ability_types import StaticAbility


class GameStateSetupMixin:
    """Game setup, mulligans, and effect registration."""

    # Empty slots: preserves GameState's __slots__ semantics (no instance __dict__).
    __slots__ = ()

    def track_card_played(self, card_id, player_idx):
        """Track WHICH cards are played and WHEN (turn) for statistics.

        Triage fix (July 2026): only the which-list existed; the stats tracker
        then FABRICATED play turns as estimated_turn = CMC, so every curve
        statistic fed to the deck builder was fiction. play_history records
        {player_idx: {turn: [card_ids]}} with the real turn of each play.
        """
        # Create tracking dictionaries if they don't exist
        if not hasattr(self, 'cards_played'):
            self.cards_played = {0: [], 1: []}
        if not hasattr(self, 'play_history'):
            self.play_history = {0: {}, 1: {}}
        
        # Accept either a player dict or an index. Triage fix (July 2026):
        # this line unconditionally re-mapped by comparing to the player DICT,
        # but every caller passes an int index -- 0 == self.p1 is always False,
        # so ALL plays were credited to index 1 (p1's plays counted as p2's).
        if player_idx is self.p1:
            player_idx = 0
        elif player_idx is self.p2:
            player_idx = 1
        elif player_idx not in (0, 1):
            logging.warning(f"track_card_played: unrecognized player_idx {player_idx!r}; defaulting to 1")
            player_idx = 1
        self.cards_played[player_idx].append(card_id)
        turn = getattr(self, 'turn', 0)
        self.play_history[player_idx].setdefault(turn, []).append(card_id)
        
        # If stats tracker is available, inform it
        if hasattr(self, 'stats_tracker') and self.stats_tracker:
            # Just collect the data, actual stats will be processed at game end
            pass

    def initialize_turn_tracking(self):
        """Initialize turn phase tracking for keyword abilities"""
        gs = self
        
        # Create or reset turn tracking data
        gs.spells_cast_this_turn = []
        gs.attackers_this_turn = set()
        gs.creatures_died_this_turn = {}
        gs.damage_dealt_this_turn = {}
        gs.cards_drawn_this_turn = {'p1': 0, 'p2': 0}
        
        # Reset any "until end of turn" effects tracking
        gs.until_end_of_turn_effects = {}
        
        logging.debug("Initialized turn tracking for keyword abilities")

    def track_mulligan(self, player, count=1):
        """Track mulligan decisions for statistics"""
        # Ensure mulligan_data exists
        if not hasattr(self, 'mulligan_data'):
            self.mulligan_data = {'p1': 0, 'p2': 0}
        
        # Update the appropriate counter
        if player == self.p1:
            self.mulligan_data['p1'] += count
        else:
            self.mulligan_data['p2'] += count

    def apply_layer_effects(self):
        """Apply all continuous effects in the proper layer order."""
        if self.layer_system:
            self.layer_system.apply_all_effects()
            self.check_state_based_actions()

    def apply_replacement_effect(self, event_type, event_context):
        """
        Apply any applicable replacement effects to an event.
        
        Args:
            event_type: The type of event (e.g., 'DRAW', 'DAMAGE', 'DIES')
            event_context: Dictionary with event information
            
        Returns:
            tuple: (modified_context, was_replaced)
        """
        # If the game state doesn't have a replacement effect system, create one
        if not hasattr(self, 'replacement_effects') or self.replacement_effects is None:
            try:
                from .replacement_effects import ReplacementEffectSystem
                self.replacement_effects = ReplacementEffectSystem(self)
            except ImportError:
                # If module not available, return unmodified context
                return event_context, False
        
        # Apply replacement effects if available
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            return self.replacement_effects.apply_replacements(event_type, event_context)
        else:
            # If no replacement effects system, just return the original context
            return event_context, False

    def register_continuous_effect(self, effect_data):
        """Register a continuous effect with the layer system."""
        if self.layer_system:
            return self.layer_system.register_effect(effect_data)
        return None

    def register_replacement_effect(self, effect_data):
        """Register a replacement effect."""
        if self.replacement_effects:
            return self.replacement_effects.register_effect(effect_data)
        return None

    def perform_mulligan(self, player, keep_hand=False):
        """
        Implement the London Mulligan rule. Transitions between mulliganing and bottoming.
        Handles turn switching during the mulligan phase and game start. (Corrected State Assignment v4)
        """
        if not self.mulligan_in_progress:
            logging.warning("Attempted mulligan action when not in mulligan phase.")
            return False

        # Safety check for null player
        if player is None:
            logging.error("Mulligan error: Null player object passed to perform_mulligan.")
            return False

        player_id_str = 'p1' if player == self.p1 else 'p2'
        opponent = self.p2 if player == self.p1 else self.p1

        # Check if opponent exists, handle gracefully if not
        if opponent is None:
            logging.warning("Mulligan warning: Opponent object is None, simulating single-player mode.")
            # In single-player mode, proceed directly to game start after this player's decision
            if keep_hand:
                mulligan_count = self.mulligan_count.get(player_id_str, 0)
                logging.debug(f"{player['name']} decided to keep hand after {mulligan_count} mulligan(s).")
                player['_mulligan_decision_made'] = True
                
                # Set bottom cards if needed
                if mulligan_count > 0:
                    current_hand_size = len(player.get("hand", []))
                    num_to_bottom = min(mulligan_count, current_hand_size)
                    if num_to_bottom > 0:
                        # Skip bottoming in single-player mode, auto-bottom worst cards
                        self._auto_bottom_cards(player, num_to_bottom)
                
                # End mulligan phase and start game
                self._end_mulligan_phase()
                return True
            else:
                # Handle mulligan in single-player mode
                self.track_mulligan(player)
                current_mull_count = self.mulligan_count.get(player_id_str, 0) + 1
                self.mulligan_count[player_id_str] = current_mull_count
                
                # Redraw hand
                if not player.get('library'): player['library'] = []
                player["library"].extend(player.get("hand", []))
                player["hand"] = []
                random.shuffle(player["library"])
                for _ in range(7):
                    if player["library"]:
                        player["hand"].append(player["library"].pop(0))
                    else:
                        logging.warning(f"Attempted to draw during mulligan but library became empty.")
                        break
                
                player['_mulligan_decision_made'] = False
                self.mulligan_player = player
                return True

        # Rest of the original function code continues from here...
        opponent_has_decided = opponent.get('_mulligan_decision_made', False)

        if keep_hand:
            mulligan_count = self.mulligan_count.get(player_id_str, 0)
            logging.debug(f"{player['name']} decided to keep hand after {mulligan_count} mulligan(s).")
            player['_mulligan_decision_made'] = True # Mark this player as having made the mulligan *decision*

            # Determine if this player needs to bottom cards based on mulligans taken
            needs_to_bottom = False
            num_to_bottom_calc = 0
            if mulligan_count > 0:
                current_hand_size = len(player.get("hand", []))
                num_to_bottom_calc = min(mulligan_count, current_hand_size) # Can't bottom more than hand size
                needs_to_bottom = num_to_bottom_calc > 0
            player['_needs_to_bottom_next'] = needs_to_bottom # Flag if bottoming is required *at some point*
            player['_bottoming_complete'] = not needs_to_bottom # Flag if bottoming is NOT required for this player

            logging.debug(f"Player {player['name']} flags after KEEP: DecisionMade={player.get('_mulligan_decision_made')}, NeedsBottom={player.get('_needs_to_bottom_next')}, BottomComplete={player.get('_bottoming_complete')}")

            # --- Determine Next State ---
            if not opponent_has_decided:
                # If opponent hasn't decided, it's their turn to choose mulligan/keep
                logging.debug(f"Current player ({player['name']}) kept. Opponent ({opponent['name']}) has not decided. Switching mulligan player.")
                self.mulligan_player = opponent # Set opponent as the active decision-maker
                self.bottoming_player = None    # Ensure bottoming is not active
                self.bottoming_in_progress = False
                return None # Indicate a state transition occurred, requires new action mask.
            else:
                # Both players have now made their mulligan KEEP/MULLIGAN decision. Move to bottoming if needed.
                # Check P1 first, then P2, to determine who bottoms next.
                p1_needs_and_not_done = self.p1 and self.p1.get('_needs_to_bottom_next', False) and not self.p1.get('_bottoming_complete', False)
                p2_needs_and_not_done = self.p2 and self.p2.get('_needs_to_bottom_next', False) and not self.p2.get('_bottoming_complete', False)

                logging.debug(f"Both decided mulligan. P1 needs bottom: {p1_needs_and_not_done}. P2 needs bottom: {p2_needs_and_not_done}.")

                # --- Transition to Bottoming Phase or End Mulligan Phase ---
                if p1_needs_and_not_done:
                    # P1 needs to bottom first
                    logging.info(f"Transitioning to bottoming phase for {self.p1['name']}.")
                    self.mulligan_player = None         # Clear mulligan decision player
                    self.bottoming_in_progress = True   # Enter bottoming phase
                    self.bottoming_player = self.p1     # Assign P1 to act
                    self.bottoming_count = 0            # Reset counter for this player
                    self.cards_to_bottom = min(self.mulligan_count.get('p1', 0), len(self.p1.get("hand", []))) # Determine count
                    return None # State transitioned, requires new action mask.
                elif p2_needs_and_not_done:
                    # P1 is done (or didn't need to bottom), now P2 needs to bottom
                    logging.info(f"Transitioning to bottoming phase for {self.p2['name']}.")
                    self.mulligan_player = None         # Clear mulligan decision player
                    self.bottoming_in_progress = True   # Stay/Enter bottoming phase
                    self.bottoming_player = self.p2     # Assign P2 to act
                    self.bottoming_count = 0            # Reset counter for this player
                    self.cards_to_bottom = min(self.mulligan_count.get('p2', 0), len(self.p2.get("hand", []))) # Determine count
                    return None # State transitioned, requires new action mask.
                else:
                    # Neither player needs to bottom (or both finished if logic allowed concurrent tracking)
                    logging.debug("Both players finished mulligan decisions and don't need/finished bottoming.")
                    self._end_mulligan_phase() # End the entire mulligan process
                    # Return False because the KEEP action itself doesn't draw cards. Mulligan PROCESS finished.
                    return False # Indicate the 'keep' action finished processing, didn't fail but no draw.

        else: # Player chose to Mulligan (keep_hand=False)
            # --- Mulligan Logic (Shuffle and Draw New Hand) ---
            self.track_mulligan(player) # Track stat
            current_mull_count = self.mulligan_count.get(player_id_str, 0) + 1 # Increment for logging/logic
            self.mulligan_count[player_id_str] = current_mull_count # Update count

            # Return hand, shuffle, draw new hand
            if not player.get('library'): player['library'] = [] # Ensure library list exists
            player["library"].extend(player.get("hand", [])) # Add hand back to library
            player["hand"] = [] # Clear hand
            random.shuffle(player["library"]) # Shuffle
            for _ in range(7): # Draw 7 cards
                if player["library"]:
                    player["hand"].append(player["library"].pop(0))
                else: # Stop if library empty
                    logging.warning(f"Attempted to draw during mulligan for {player['name']} but library became empty.")
                    break
            logging.debug(f"{player['name']} took mulligan #{current_mull_count}, drew new hand of {len(player['hand'])} cards.")

            # Reset THIS player's decision flags - they MUST decide again on the new hand.
            player['_mulligan_decision_made'] = False
            player['_needs_to_bottom_next'] = False
            player['_bottoming_complete'] = False

            # Keep opponent's decision flags as they were.
            # Mulligan phase remains active. This player must act again.
            self.mulligan_player = player       # Assign THIS player to make the next decision
            self.bottoming_player = None        # Ensure bottoming is not active
            self.bottoming_in_progress = False
            # Return True because a mulligan action (drawing new hand) was performed.
            return True

    def check_mulligan_state(self):
        """
        Helper function to diagnose mulligan state inconsistencies and force recovery.
        Returns True if state is valid, False otherwise and attempts recovery.
        (Enhanced with stronger recovery v2)
        """
        # Case 1: Both mulligan_player and bottoming_player are None but still in mulligan phase
        if self.mulligan_in_progress and self.mulligan_player is None and not self.bottoming_in_progress:
            logging.error("Inconsistent state: In mulligan phase with no active mulligan player")
            # Count remaining players who haven't decided
            unmade_decisions = 0
            for p, p_id in [(self.p1, 'p1'), (self.p2, 'p2')]:
                if p and not p.get('_mulligan_decision_made', False):
                    unmade_decisions += 1
                    self.mulligan_player = p
                    logging.info(f"Recovering mulligan state by assigning {p_id} as mulligan player")
            
            # If no undecided players were found OR we found multiple (inconsistent), force end mulligan
            if unmade_decisions != 1:
                logging.warning(f"Found {unmade_decisions} players with undecided mulligans. Forcing end of mulligan phase.")
                self._end_mulligan_phase()
                return False
            return True
        
        # Case 2: In bottoming phase but no bottoming player
        if self.bottoming_in_progress and self.bottoming_player is None:
            logging.error("Inconsistent state: In bottoming phase with no active bottoming player")
            # Find a player who needs to bottom
            needs_bottom_found = 0
            for p, p_id in [(self.p1, 'p1'), (self.p2, 'p2')]:
                if p and p.get('_needs_to_bottom_next', False) and not p.get('_bottoming_complete', False):
                    needs_bottom_found += 1
                    self.bottoming_player = p
                    self.bottoming_count = 0
                    self.cards_to_bottom = min(self.mulligan_count.get(p_id, 0), len(p.get("hand", [])))
                    logging.info(f"Recovering bottoming state by assigning {p_id} as bottoming player")
            
            # If no players need to bottom OR multiple (inconsistent), force end bottoming
            if needs_bottom_found != 1:
                logging.warning(f"Found {needs_bottom_found} players needing to bottom. Forcing end of mulligan phase.")
                self._end_mulligan_phase()
                return False
            return True
        
        # Case 3: Neither mulligan nor bottoming in progress, but mulligan_in_progress flag is still set
        if self.mulligan_in_progress and not self.bottoming_in_progress and self.mulligan_player is None:
            # Check if all players have completed their mulligan decisions
            all_decided = True
            for p in [self.p1, self.p2]:
                if p and not p.get('_mulligan_decision_made', False):
                    all_decided = False
                    break
                    
            if all_decided:
                logging.info("All players have made mulligan decisions but phase not ended. Ending mulligan.")
                self._end_mulligan_phase()
                return False
            else:
                # Inconsistent state - someone still needs to decide but mulligan_player is None
                logging.error("Inconsistent mulligan state: No bottoming, not all decided, but no mulligan_player")
                self._end_mulligan_phase()  # Safety: force end the phase
                return False
        
        # Case 4: Bottoming needed but stalled - check counters
        if self.bottoming_in_progress and self.bottoming_player:
            # Check if bottoming is stalled (no cards to bottom or count inconsistency)
            if self.cards_to_bottom <= 0 or self.bottoming_count >= self.cards_to_bottom:
                logging.error(f"Bottoming stalled: to_bottom={self.cards_to_bottom}, count={self.bottoming_count}")
                # Mark this player as complete and check if we need to move to the next player
                self.bottoming_player['_bottoming_complete'] = True
                
                # Check if other player needs to bottom
                other_player = self.p2 if self.bottoming_player == self.p1 else self.p1
                if other_player and other_player.get('_needs_to_bottom_next', False) and not other_player.get('_bottoming_complete', False):
                    self.bottoming_player = other_player
                    self.bottoming_count = 0
                    other_id = 'p2' if other_player == self.p2 else 'p1'
                    self.cards_to_bottom = min(self.mulligan_count.get(other_id, 0), len(other_player.get("hand", [])))
                    logging.info(f"Transitioning bottoming to next player: {other_player['name']}")
                else:
                    # No other player needs to bottom, end mulligan
                    logging.info("No more players need to bottom. Ending mulligan phase.")
                    self._end_mulligan_phase()
                    return False
        
        # ``turn`` is intentionally initialized to 1 before pregame decisions,
        # so it cannot be used as evidence that the mulligan phase is stale.
        # In non-error cases, continue
        return True

    def _register_impending_static_effect(self, card_id, controller, layer, effect_type, effect_value, sublayer=None):
        """Helper to register the static effects for Impending."""
        if not self.layer_system: return
        effect_data = {
             'source_id': card_id,
             'layer': layer,
             'affected_ids': [card_id],
             'effect_type': effect_type,
             'effect_value': effect_value,
             'duration': 'permanent', # Active while condition met
             'controller_id': controller,
             'description': f"Impending static effect ({effect_type})",
             # Condition: Only active while it has time counters
             'condition': lambda gs: gs._is_impending_active(card_id)
        }
        if sublayer: effect_data['sublayer'] = sublayer
        self.layer_system.register_effect(effect_data)

    def _is_impending_active(self, card_id):
        """Checks if an Impending permanent should currently not be a creature."""
        # Use LIVE card data if available (updated by SBAs etc.)
        card = self._safe_get_card(card_id)
        if not card:
            logging.debug(f"_is_impending_active check failed: Card {card_id} not found.")
            return False

        # Get current counters directly from card object (assumed updated)
        has_time_counters = getattr(card,'counters', {}).get('time', 0) > 0
        # Check if it's on the battlefield
        owner, zone = self.find_card_location(card_id)

        is_active = has_time_counters and zone == 'battlefield'
        # logging.debug(f"_is_impending_active check for {card_id}: Counters={has_time_counters}, Zone={zone}. Active={is_active}")
        return is_active

    def _register_card_effects(self, card_id, card, player):
        """Register static and replacement effects originating from a card."""
        # Register static abilities via AbilityHandler if they exist
        if self.ability_handler:
            abilities = self.ability_handler.registered_abilities.get(card_id, [])
            for ability in abilities:
                if isinstance(ability, StaticAbility):
                     # StaticAbility.apply() handles registration with LayerSystem
                     ability.apply(self) # Pass GameState

        # Register replacement effects
        if self.replacement_effects:
            self.replacement_effects.register_card_replacement_effects(card_id, player)

    def record_strategy_pattern(self, action_idx, reward):
        """Record the current strategy pattern and action."""
        if hasattr(self, 'strategy_memory'):
            try:
                # Extract pattern
                pattern = self.strategy_memory.extract_strategy_pattern(self)
                
                # Update strategy with reward
                self.strategy_memory.update_strategy(pattern, reward)
                
                # Record action sequence
                if not hasattr(self, 'current_action_sequence'):
                    self.current_action_sequence = []
                    
                self.current_action_sequence.append(action_idx)
                
                # Periodically save strategy memory
                if random.random() < 0.1:  # 10% chance each time
                    self.strategy_memory.save_memory()
                    
            except Exception as e:
                logging.error(f"Error recording strategy pattern: {str(e)}")

    def handle_card_type_specific_rules(self, card_id, zone, player):
        """
        Handle rules specific to different card types when they enter a zone.
        
        Args:
            card_id: ID of the card
            zone: The zone the card is entering ('battlefield', 'graveyard', etc.)
            player: The player who controls the card
            
        Returns:
            bool: Whether any special handling was performed
        """
        gs = self
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'card_types'):
            return False
        
        # Battlefield entry rules
        if zone == "battlefield":
            # Creatures enter with summoning sickness
            if 'creature' in card.card_types:
                player["entered_battlefield_this_turn"].add(card_id)
                
            # Planeswalkers enter with loyalty counters
            if 'planeswalker' in card.card_types:
                if not hasattr(player, "loyalty_counters"):
                    player["loyalty_counters"] = {}
                    
                base_loyalty = card.loyalty if hasattr(card, 'loyalty') else 3
                player["loyalty_counters"][card_id] = base_loyalty
                logging.debug(f"Planeswalker {card.name} entered with {base_loyalty} loyalty")
                
            # Saga enchantments enter with lore counters
            if 'enchantment' in card.card_types and hasattr(card, 'subtypes') and 'saga' in card.subtypes:
                if not hasattr(player, "saga_counters"):
                    player["saga_counters"] = {}
                    
                player["saga_counters"][card_id] = 1
                
                # Trigger first chapter ability
                self.trigger_ability(card_id, "SAGA_CHAPTER", {"chapter": 1})
                
            # Equipment enters unattached
            if 'artifact' in card.card_types and hasattr(card, 'subtypes') and 'equipment' in card.subtypes:
                if not hasattr(player, "attachments"):
                    player["attachments"] = {}
                    
                if card_id in player["attachments"]:
                    del player["attachments"][card_id]
                
            # Auras need a target when cast
            if 'enchantment' in card.card_types and hasattr(card, 'subtypes') and 'aura' in card.subtypes:
                if not hasattr(player, "attachments") or card_id not in player["attachments"]:
                    # In a real implementation, this would be handled during casting/resolution
                    # For simulation purposes, we'll just attach to a legal target if possible
                    target_found = False
                    
                    # Look for a creature to attach to
                    for p in [gs.p1, gs.p2]:
                        for target_id in p["battlefield"]:
                            target_card = self._safe_get_card(target_id)
                            if target_card and hasattr(target_card, 'card_types') and 'creature' in target_card.card_types:
                                if not hasattr(player, "attachments"):
                                    player["attachments"] = {}
                                    
                                player["attachments"][card_id] = target_id
                                target_found = True
                                logging.debug(f"Aura {card.name} attached to {target_card.name}")
                                break
                        
                        if target_found:
                            break
                    
                    if not target_found:
                        # If no valid target, Aura goes to graveyard
                        logging.debug(f"Aura {card.name} had no valid targets, moving to graveyard")
                        self.move_card(card_id, player, "battlefield", player, "graveyard")
                        return True
        
        # Graveyard entry rules
        elif zone == "graveyard":
            # Check for death triggers
            if card_id in player["battlefield"]:
                # Card is moving from battlefield to graveyard (dying)
                if 'creature' in card.card_types:
                    self.trigger_ability(card_id, "DIES")
                    
                # Artifact going to graveyard
                elif 'artifact' in card.card_types:
                    self.trigger_ability(card_id, "ARTIFACT_PUT_INTO_GRAVEYARD")
        
        # Hand entry rules
        elif zone == "hand":
            # Cards returning to hand lose counters, attachments, etc.
            if card_id in player["battlefield"]:
                # Remove any counters
                if hasattr(card, "counters"):
                    card.counters = {}
                    
                # Remove any attachments
                if hasattr(player, "attachments"):
                    if card_id in player["attachments"]:
                        del player["attachments"][card_id]
                    
                    # Also remove this card as an attachment from other cards
                    attached_to = [aid for aid, target in player["attachments"].items() if target == card_id]
                    for aid in attached_to:
                        del player["attachments"][aid]
        
        # Exile entry rules
        elif zone == "exile":
            # Similar to graveyard, but different triggers
            if card_id in player["battlefield"]:
                self.trigger_ability(card_id, "EXILED")
                
                # Remove any attachments
                if hasattr(player, "attachments"):
                    if card_id in player["attachments"]:
                        del player["attachments"][card_id]
                    
                    # Also remove this card as an attachment from other cards
                    attached_to = [aid for aid, target in player["attachments"].items() if target == card_id]
                    for aid in attached_to:
                        del player["attachments"][aid]
        
        return True

    def _end_mulligan_phase(self):
            """Helper to clean up mulligan state and transition to Turn 1. (Revised Priority Assignment v5 - Improved State Cleanup)"""
            # Check if already ended to prevent potential recursion/double execution
            if not self.mulligan_in_progress and not self.bottoming_in_progress:
                logging.debug("_end_mulligan_phase called, but mulligan/bottoming already inactive.")
                return # Avoid running logic again if already ended

            # IMPORTANT: Force end mulligan regardless of unfinished business
            logging.info("Ending mulligan phase - transitioning to main game.")
            
            # --- CRITICAL FIX: Reset flags BEFORE changing turn number ---
            # This prevents "In mulligan but turn >= 1" errors during SBA/Trigger checks that might occur immediately.
            self.mulligan_in_progress = False
            self.mulligan_player = None
            self.bottoming_in_progress = False
            self.bottoming_player = None

            # Force clean up temporary flags using dict.pop
            for p in [self.p1, self.p2]:
                if p: # Check if player exists
                    p.pop('_mulligan_decision_made', None)
                    p.pop('_needs_to_bottom_next', None)
                    p.pop('_bottoming_complete', None)

            # CR 103.6c: cards like Leylines may begin the game on the
            # battlefield from the opening hand. Starting player decides
            # first. If any such choice exists, the first turn is deferred
            # until every begin-game decision resolves.
            # Capture the kept, post-bottoming hands before those permissions
            # move Leylines or similar cards onto the battlefield.
            self.opening_hands = {
                'p1': list(self.p1.get('hand', [])) if self.p1 else [],
                'p2': list(self.p2.get('hand', [])) if self.p2 else [],
            }
            starting_player = self.p1  # Turn 1 belongs to p1 (_get_active_player).
            other_player = self.p2
            self._opening_hand_players = [
                p for p in (starting_player, other_player)
                if p and self._collect_opening_hand_cards(p)]
            if self._opening_hand_players:
                self._begin_opening_hand_choice(self._opening_hand_players.pop(0))
                return

            self._begin_first_turn()

    def _collect_opening_hand_cards(self, player):
        """Hand cards with a 'begin the game on the battlefield' permission."""
        found = []
        for cid in player.get("hand", []):
            card = self._safe_get_card(cid)
            text = (getattr(card, 'oracle_text', '') or '').lower()
            if "you may begin the game with it on the battlefield" in text:
                found.append(cid)
        return found

    def _begin_opening_hand_choice(self, player):
        """Open the begin-game battlefield choice for one player (CR 103.6c)."""
        options = self._collect_opening_hand_cards(player)
        self.phase = self.PHASE_CHOOSE
        self.choice_context = {
            "type": "opening_hand",
            "player": player,
            "options": options,
            "source_id": options[0] if options else None,
        }
        self.priority_player = player
        self.priority_pass_count = 0
        logging.debug(f"{player['name']} may begin the game with "
                      f"{len(options)} card(s) on the battlefield.")

    def complete_opening_hand_choice(self, option_index):
        """Apply one begin-game decision. option_index None declines the
        player's remaining begin-game cards; an int puts that option onto the
        battlefield. When a player's choice closes, the next player's choice
        opens, and after the last one the deferred first turn begins."""
        ctx = getattr(self, 'choice_context', None)
        if not ctx or ctx.get("type") != "opening_hand":
            logging.warning("complete_opening_hand_choice called without an opening_hand context.")
            return False
        player = ctx.get("player")
        remaining = []
        if option_index is not None:
            options = ctx.get("options", [])
            if not isinstance(option_index, int) or not (0 <= option_index < len(options)):
                logging.warning(f"Invalid opening-hand option index {option_index}.")
                return False
            card_id = options[option_index]
            if card_id not in player.get("hand", []):
                logging.warning(f"Opening-hand card {card_id} is no longer in hand.")
                return False
            if not self.move_card(card_id, player, "hand", player, "battlefield",
                                  cause="opening_hand"):
                logging.warning(f"Could not put opening-hand card {card_id} onto the battlefield.")
                return False
            remaining = self._collect_opening_hand_cards(player)
        if remaining:
            ctx["options"] = remaining
            ctx["source_id"] = remaining[0]
            return True
        self.choice_context = None
        queue = getattr(self, '_opening_hand_players', []) or []
        if queue:
            self._begin_opening_hand_choice(queue.pop(0))
            return True
        self._begin_first_turn()
        return True

    def _begin_first_turn(self):
            """Start Turn 1 after mulligans (and any begin-game choices)."""
            # --- Set State for Start of Game ---
            self.turn = 1 # Officially Turn 1
            self.phase = self.PHASE_UNTAP # Start with Untap

            try:
                self._reset_turn_tracking_variables() # Reset turn vars for Turn 1
            except Exception as e:
                logging.error(f"Error resetting turn tracking variables: {e}")
                # Continue even if this fails

            # Get active player with fallback
            active_player = self._get_active_player() # P1 is active player on Turn 1
            if not active_player:
                logging.critical("CRITICAL ERROR in _end_mulligan_phase: _get_active_player() returned None!")
                return # Stop if no player

            logging.debug(f"Performing Turn {self.turn} Untap Step for {active_player['name']}...")
            try:
                self._untap_phase(active_player)
                self.check_state_based_actions() # Check SBAs after untap
            except Exception as e:
                logging.error(f"Error in untap phase / SBA check: {e}")
                # Continue even if untap/SBA fails initially

            # ** Automatically advance to Upkeep **
            self.phase = self.PHASE_UPKEEP
            logging.debug(f"Automatically advanced to Upkeep Step.")

            # ** Trigger upkeep abilities AFTER phase set **
            try:
                # Perform phase-start triggers/actions BEFORE assigning priority
                self._handle_beginning_of_phase_triggers() # Use helper for upkeep triggers (includes SBA check)
            except Exception as e:
                logging.error(f"Error handling beginning of phase triggers: {e}")
                # Continue even if trigger handling fails

            # ** Assign Priority AFTER triggers **
            # Priority is given *unless* a trigger caused a state change requiring a different player to act (rare)
            # SBAs/triggers might resolve here or later. AP gets priority initially in Upkeep.
            self.priority_player = active_player
            self.priority_pass_count = 0
            self.last_stack_size = len(self.stack) # Initialize stack size tracking
            logging.debug(f"Entering Upkeep. Priority assigned to AP ({active_player['name']})")

            # Game Turn Limit Check (Keep as is)
            if self.turn > self.max_turns and not getattr(self, '_turn_limit_checked', False):
                logging.info(f"Turn limit ({self.max_turns}) reached! Ending game.")
                self._turn_limit_checked = True
                if self.p1 and self.p2:
                    if self.p1.get("life",0) > self.p2.get("life",0): self.p1["won_game"] = True; self.p2["lost_game"] = True
                    elif self.p2.get("life",0) > self.p1.get("life",0): self.p2["won_game"] = True; self.p1["lost_game"] = True
                    else: self.p1["game_draw"] = True; self.p2["game_draw"] = True

    def initialize_targeting_system(self):
        """Initialize the targeting system."""
        try:
            from .targeting import TargetingSystem
            self.targeting_system = TargetingSystem(self)
            logging.debug("TargetingSystem initialized successfully")
        except ImportError as e:
            logging.warning(f"TargetingSystem not available: {e}")
            self.targeting_system = None
        except Exception as e:
            logging.error(f"Error initializing targeting system: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            self.targeting_system = None

