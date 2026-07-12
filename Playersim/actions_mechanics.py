"""Handlers for special card mechanics and permanent manipulation.

Extracted from actions.py. This module defines behavior only (a mixin);
all state lives on ActionHandler, which composes every mixin.
"""

import logging
import re


class MechanicsHandlersMixin:
    """Handlers for special card mechanics and permanent manipulation."""

    __slots__ = ()

    def _handle_saddle(self, param, context, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        battlefield_idx = (context or {}).get("battlefield_idx")
        if battlefield_idx is None or not (0 <= battlefield_idx < len(player.get("battlefield", []))):
            return -0.1, False
        source_id = player["battlefield"][battlefield_idx]
        card = gs._safe_get_card(source_id)
        match = re.search(r"\bsaddle\s+(\d+)", getattr(card, "oracle_text", ""), re.IGNORECASE)
        if not match or not gs._can_act_at_sorcery_speed(player):
            return -0.1, False
        options = [cid for cid in player.get("battlefield", [])[:10]
                   if cid != source_id and cid not in player.get("tapped_permanents", set())
                   and gs._is_creature(cid)]
        def saddle_power(cid):
            try:
                return max(0, int(getattr(gs._safe_get_card(cid), "power", 0) or 0))
            except (TypeError, ValueError):
                return 0
        if sum(saddle_power(cid) for cid in options) < int(match.group(1)):
            return -0.1, False
        gs.choice_context = {"type": "saddle", "player": player, "source_id": source_id,
                             "options": options, "selected": [], "selected_power": 0,
                             "required_power": int(match.group(1)), "resume_phase": gs.phase}
        gs.phase = gs.PHASE_CHOOSE
        return 0.0, True

    def _handle_level_up_class(self, param, context, **kwargs):
        """Handle leveling up a class card."""
        gs = self.game_state
        player = self._get_policy_player(context)
        class_idx = param

        if not hasattr(gs, 'ability_handler') or not gs.ability_handler:
            logging.error("LEVEL_UP_CLASS failed: AbilityHandler not found.")
            return -0.15, False # Failure

        if class_idx is None or not isinstance(class_idx, int):
            logging.error(f"LEVEL_UP_CLASS failed: Invalid or missing index parameter '{class_idx}'.")
            return -0.15, False # Failure

        if gs.ability_handler.handle_class_level_up(class_idx):
            return 0.35, True # Success
        else:
            logging.debug(f"Leveling up class at index {class_idx} failed (handled by ability_handler).")
            return -0.1, False # Failure

    def _handle_level_up_creature(self, param, context, **kwargs):
        """Level up a leveler creature (CR 711).

        Pays the creature's "Level up {cost}" and adds one level counter, which
        moves it into the next band (P/T + abilities applied by the layer system).
        Distinct from LEVEL_UP_CLASS: levelers pay a repeatable activated cost and
        track progress with per-permanent level counters, not a Class level index.
        """
        gs = self.game_state
        player = self._get_policy_player(context)
        bf_idx = param

        if bf_idx is None or not isinstance(bf_idx, int):
            logging.error(f"LEVEL_UP_CREATURE failed: invalid index parameter '{bf_idx}'.")
            return -0.15, False
        battlefield = player.get("battlefield", [])
        if not (0 <= bf_idx < len(battlefield)):
            logging.warning(f"LEVEL_UP_CREATURE failed: index {bf_idx} out of range.")
            return -0.15, False

        card_id = battlefield[bf_idx]
        card = gs._safe_get_card(card_id)
        if not card or not getattr(card, 'is_leveler', False):
            logging.warning(f"LEVEL_UP_CREATURE failed: card at index {bf_idx} is not a leveler.")
            return -0.15, False

        cost_str = getattr(card, 'level_up_cost', None)
        if not cost_str:
            logging.warning(f"LEVEL_UP_CREATURE failed: no level-up cost on {getattr(card, 'name', '?')}.")
            return -0.15, False

        if not getattr(gs, 'mana_system', None):
            logging.warning("LEVEL_UP_CREATURE failed: mana system unavailable.")
            return -0.15, False

        # Check affordability, then pay. The mask counts untapped lands, so
        # this pre-gate must too (payment auto-taps the planned lands).
        try:
            parsed = gs.mana_system.parse_mana_cost(cost_str)
            if not gs.mana_system.can_pay_mana_cost_with_lands(player, parsed):
                logging.debug(f"Cannot afford level up for {card.name} (cost {cost_str}).")
                return -0.1, False
        except Exception as e:
            logging.error(f"LEVEL_UP_CREATURE cost check error for {card.name}: {e}")
            return -0.15, False

        if not gs.mana_system.pay_mana_cost(player, parsed):
            logging.warning(f"LEVEL_UP_CREATURE failed to pay {cost_str} for {card.name}.")
            return -0.1, False

        # Add one level counter. It lives in the permanent's counters dict (the
        # same per-permanent store as +1/+1), so the layer system reads it for
        # band P/T/abilities and it resets with the board between games rather
        # than leaking on the shared Card object. add_counter also fires
        # COUNTER_ADDED and runs state-based actions.
        before = (getattr(card, 'counters', {}) or {}).get('level', 0)
        gs.add_counter(card_id, 'level', 1)
        after = (getattr(card, 'counters', {}) or {}).get('level', 0)
        if after <= before:
            logging.warning(f"LEVEL_UP_CREATURE: level counter did not increase on {card.name}.")
            return -0.1, False

        # Recompute characteristics so the new band's P/T and abilities apply now.
        if getattr(gs, 'layer_system', None):
            gs.layer_system.invalidate_cache()
            gs.layer_system.apply_all_effects()

        logging.info(f"Leveled up {card.name}: level counters {before} -> {after}.")
        return 0.15, True

    def _handle_transform(self, param, **kwargs):
        gs = self.game_state; player = self._get_policy_player(kwargs.get('context'))
        bf_idx = param
        if bf_idx >= len(player.get("battlefield", [])): return -0.2, False

        card_id = player["battlefield"][bf_idx]; card = gs._safe_get_card(card_id)
        if card and getattr(card, "is_specialize", False):
            if gs.start_specialize_choice(card_id, player):
                return 0.02, True
            logging.debug(f"SPECIALIZE failed for {getattr(card, 'name', card_id)}.")
            return -0.1, False
        if card and hasattr(card, 'transform') and hasattr(card, 'can_transform') and card.can_transform(gs): # Check if possible
            card.transform() # Card method handles its state change
            gs.trigger_ability(card_id, "TRANSFORMED", {"controller": player})
            return 0.1, True # Success
        logging.debug(f"TRANSFORM failed for {getattr(card, 'name', card_id)} (cannot transform).")
        return -0.1, False # Not transformable or cannot transform now (Failure)

    def _handle_unlock_door(self, param, context, **kwargs):
        gs = self.game_state
        bf_idx = param
        if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler,'handle_unlock_door'):
             success = gs.ability_handler.handle_unlock_door(bf_idx)
             return 0.3, success # Reward successful unlock
        logging.error("UNLOCK_DOOR: AbilityHandler or method missing.")
        return -0.15, False # Failure if handler missing

    def _handle_dredge(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2 # Who has dredge choice? Needs context. Assume active.
        player = self._get_policy_player(context)
        gy_choice_idx = param # Agent chose which dredge option (index 0-5)

        # Need context to know which card/value this corresponds to
        # Dredge action generation (_add_special_choice_actions) should provide context.
        dredge_context = context or {} # Get context from kwargs
        if 'gy_idx' not in dredge_context: # OLD dredge pending check
            if not hasattr(gs, 'dredge_pending') or not gs.dredge_pending or gs.dredge_pending['player'] != player:
                 logging.warning("DREDGE action called but no dredge pending or invalid context.")
                 return -0.1, False
            dredge_info = gs.dredge_pending
            dredge_card_id = dredge_info['card_id'] # Use ID from pending state
        else: # NEW context-based approach (prefer this)
            gy_idx = dredge_context['gy_idx']
            if gy_idx >= len(player.get("graveyard",[])): return -0.15, False
            dredge_card_id = player["graveyard"][gy_idx]
            card = gs._safe_get_card(dredge_card_id)
            if not card or "dredge" not in getattr(card,'oracle_text','').lower(): return -0.1, False # Card invalid


        if hasattr(gs, 'perform_dredge') and gs.perform_dredge(player, dredge_card_id):
            # perform_dredge returns True on successful dredge execution
            return 0.3, True
        else:
            logging.warning(f"Dredge failed (perform_dredge returned False for {dredge_card_id}).")
            # Dredge was chosen but failed execution (e.g., not enough cards)
            # Clear pending state if gs.perform_dredge didn't
            if hasattr(gs,'dredge_pending') and gs.dredge_pending: gs.dredge_pending = None
            return -0.05, False

    def _handle_add_counter(self, param, context, **kwargs):
        gs = self.game_state
        target_idx = param # Combined battlefield index
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if target_id is None:
            logging.warning(f"ADD_COUNTER: Invalid target index {target_idx}.")
            return -0.1, False

        if context is None or 'counter_type' not in context:
            logging.error(f"ADD_COUNTER context missing 'counter_type' for target {target_id}.")
            return -0.15, False

        counter_type = context['counter_type']
        count = context.get('count', 1)

        success = gs.add_counter(target_id, counter_type, count)
        if success:
            reward = 0.1 * count if '+1/+1' in counter_type else 0.05 * count
            return reward, True # Success
        else:
            logging.debug(f"ADD_COUNTER failed for {target_id} (handled by gs.add_counter).")
            return -0.05, False # Failure

    def _handle_remove_counter(self, param, context, **kwargs):
        gs = self.game_state
        target_idx = param
        target_id, target_owner = gs.get_permanent_by_combined_index(target_idx)
        if target_id is None:
             logging.warning(f"REMOVE_COUNTER: Invalid target index {target_idx}.")
             return -0.1, False

        if context is None: # Requires context
            logging.error(f"REMOVE_COUNTER context missing for target {target_id}.")
            return -0.15, False

        counter_type = context.get('counter_type') # Should be provided in context
        count = context.get('count', 1)

        if not counter_type: # Ensure type is present
             logging.error(f"REMOVE_COUNTER context missing 'counter_type' for target {target_id}.")
             return -0.15, False

        success = gs.add_counter(target_id, counter_type, -count) # Use negative count
        if success:
            reward = 0.15 * count if '-1/-1' in counter_type else 0.05 * count
            return reward, True # Success
        else:
            logging.warning(f"REMOVE_COUNTER: gs.add_counter failed for {target_id}")
            return -0.05, False # Failure

    def _handle_proliferate(self, param, context=None, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        if hasattr(gs, 'proliferate') and callable(gs.proliferate):
            chosen_targets = context.get('proliferate_targets') if context else None
            # gs.proliferate returns True if *any* counter was added
            proliferated_something = gs.proliferate(player, targets=chosen_targets)
            # Action succeeds if proliferate logic runs, reward based on outcome
            return (0.3 if proliferated_something else 0.0), True
        else:
             logging.error("Proliferate function missing in GameState.")
             return -0.1, False # Failure (cannot perform)

    def _handle_return_from_graveyard(self, param, context=None, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        gy_idx = param

        if gy_idx >= len(player.get("graveyard", [])):
             logging.warning(f"Invalid GY index {gy_idx} for RETURN_FROM_GRAVEYARD.")
             return -0.15, False

        card_id = player["graveyard"][gy_idx] # Do NOT pop yet, move_card handles it
        card_value = self.card_evaluator.evaluate_card(card_id, "return_from_gy") if self.card_evaluator else 0.0
        success = gs.move_card(card_id, player, "graveyard", player, "hand", cause="return_from_gy_action")
        reward = 0.2 + card_value * 0.2
        return reward, success # Return success flag from move_card

    def _handle_reanimate(self, param, context=None, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        gy_idx = param

        if gy_idx >= len(player.get("graveyard", [])):
             logging.warning(f"Invalid GY index {gy_idx} for REANIMATE.")
             return -0.15, False

        card_id = player["graveyard"][gy_idx] # Do NOT pop yet
        card = gs._safe_get_card(card_id)
        valid_types = ["creature", "artifact", "enchantment", "planeswalker", "land", "battle"]
        if card and any(t in getattr(card, 'card_types', []) or t in getattr(card, 'type_line','').lower() for t in valid_types):
            card_value = self.card_evaluator.evaluate_card(card_id, "reanimate") if self.card_evaluator else 0.0
            success = gs.move_card(card_id, player, "graveyard", player, "battlefield", cause="reanimate_action")
            reward = 0.5 + card_value * 0.3
            return reward, success # Return success flag from move_card
        else:
             logging.warning(f"Cannot reanimate {getattr(card, 'name', card_id)}: Invalid type.")
             return -0.1, False # Failure (invalid type)

    def _handle_return_from_exile(self, param, context=None, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        exile_idx = param

        if exile_idx >= len(player.get("exile", [])):
            logging.warning(f"Invalid Exile index {exile_idx} for RETURN_FROM_EXILE.")
            return -0.15, False

        card_id = player["exile"][exile_idx] # Do NOT pop yet
        card_value = self.card_evaluator.evaluate_card(card_id, "return_from_exile") if self.card_evaluator else 0.0
        success = gs.move_card(card_id, player, "exile", player, "hand", cause="return_from_exile_action")
        reward = 0.3 + card_value * 0.1
        return reward, success # Return success flag from move_card

    def _handle_copy_permanent(self, param, context, **kwargs):
        gs = self.game_state; player = self._get_policy_player(context)
        target_identifier = context.get('target_identifier', context.get('target_permanent_idx'))

        if target_identifier is None:
             logging.warning(f"Copy Permanent context missing 'target_identifier'")
             return -0.15, False

        target_id, target_owner = gs.get_permanent_by_identifier(target_identifier)
        if target_id is None:
             logging.warning(f"Target identifier invalid for copy: {target_identifier}")
             return -0.15, False

        target_card = gs._safe_get_card(target_id)
        if not target_card:
             logging.warning(f"Target card not found for copy: {target_id}")
             return -0.15, False

        token_id = gs.create_token_copy(target_card, player)
        success = token_id is not None
        reward = 0.4 if success else -0.1
        return reward, success # Success based on token creation

    def _find_permanent_id(self, player, identifier):
        """Finds a permanent ID on the player's battlefield using index or ID string."""
        # This might already exist in GameState, just ensuring it's callable
        if isinstance(identifier, int):
             if 0 <= identifier < len(player.get("battlefield", [])):
                  return player["battlefield"][identifier]
        elif isinstance(identifier, str):
             if identifier in player.get("battlefield", []):
                  return identifier
        return None

    def _handle_populate(self, param, context, **kwargs):
        gs = self.game_state; player = self._get_policy_player(context)
        target_identifier = context.get('target_token_identifier', context.get('target_token_idx'))

        if target_identifier is None:
             logging.warning(f"Populate context missing 'target_token_identifier'")
             return -0.15, False

        token_to_copy_id = self._find_permanent_id(player, target_identifier) # Helper finds ID from index/ID
        if not token_to_copy_id:
             logging.warning(f"Target token identifier invalid for populate: {target_identifier}")
             return -0.15, False

        original_token = gs._safe_get_card(token_to_copy_id)
        if not (original_token and getattr(original_token,'is_token', False) and 'creature' in getattr(original_token, 'card_types', [])):
            logging.warning(f"Target for populate {token_to_copy_id} is not a valid creature token.")
            return -0.15, False

        new_token_id = gs.create_token_copy(original_token, player)
        success = new_token_id is not None
        reward = 0.35 if success else -0.1
        return reward, success # Success based on token creation

    def _handle_investigate(self, param, context=None, **kwargs):
        return self._handle_activate_ability(None, context or {})

    def _handle_foretell(self, param, context, **kwargs):
        gs = self.game_state; player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx')

        if hand_idx is None:
             logging.warning(f"Foretell context missing 'hand_idx'")
             return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError):
            logging.warning(f"Foretell context has non-integer index: {context}")
            return -0.15, False

        if hand_idx >= len(player["hand"]):
            logging.warning(f"Foretell hand index out of bounds: {hand_idx}")
            return -0.1, False

        card_id = player["hand"][hand_idx]; card = gs._safe_get_card(card_id)
        if not card or "foretell" not in getattr(card, 'oracle_text', '').lower():
            logging.warning(f"Foretell card {card_id} invalid or has no Foretell.")
            return -0.1, False

        cost_str = "{2}" # Standard foretell cost
        # Match the mask's untapped-land affordability; payment auto-taps.
        if not gs.mana_system.can_pay_mana_cost_with_lands(player, cost_str):
            logging.debug(f"Foretell failed: Cannot afford cost {cost_str} for {card.name}")
            return -0.05, False

        if not gs.mana_system.pay_mana_cost(player, cost_str):
            logging.warning(f"Foretell failed: Error paying cost for {card.name}")
            return -0.05, False

        success_move = gs.move_card(card_id, player, "hand", player, "exile", cause="foretell")
        if success_move:
            if not hasattr(gs, 'foretold_cards'): gs.foretold_cards = {}
            gs.foretold_cards[card_id] = { 'turn': gs.turn, 'original': card.__dict__.copy() }
            logging.debug(f"Foretold {card.name}")
            return 0.2, True # Success
        else: # Move failed
            # Mana was spent, need rollback? Assume lost for now.
            logging.error(f"Foretell move failed for {card.name}")
            return -0.1, False

    def _handle_amass(self, param, context, **kwargs):
        return self._handle_activate_ability(None, context or {})

    def _handle_learn(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        drew_card = False; discarded_card = False
        reward = 0.0; overall_success = False

        # Option 1: Draw, then discard
        card_drawn_id = None
        if player["library"]:
            card_drawn_id = gs._draw_card(player)
            if card_drawn_id is not None:
                drew_card = True; overall_success = True # Action led to something
                card_name = getattr(gs._safe_get_card(card_drawn_id), 'name', card_drawn_id)
                logging.debug(f"Learn: Drew {card_name}")
                reward += 0.1
            else: pass # Draw failed handled internally
        else: logging.warning(f"Learn: Cannot draw, library empty for {player['name']}")

        if drew_card and player["hand"]:
            chosen_discard_id = None
            # ... (AI discard choice logic) ...
            if self.card_evaluator: lowest_value=float('inf') ; [ (val < lowest_value and (lowest_value:=val, chosen_discard_id:=cid)) for cid in player["hand"] if (val := self.card_evaluator.evaluate_card(cid, "discard")) ]
            else: chosen_discard_id = card_drawn_id if card_drawn_id in player["hand"] else (player["hand"][0] if player["hand"] else None)

            if chosen_discard_id is not None:
                discard_success = gs.move_card(chosen_discard_id, player, "hand", player, "graveyard", cause="learn_discard")
                if discard_success:
                    discarded_card = True; overall_success = True
                    card_name = getattr(gs._safe_get_card(chosen_discard_id), 'name', chosen_discard_id)
                    logging.debug(f"Learn: Discarded {card_name}")
                    reward += 0.05
                else:
                    logging.warning("Learn: Failed to move card to graveyard for discard.")
                    reward -= 0.05
            else: pass # No card chosen to discard (e.g., empty hand after draw?)

        # Option 2: Sideboard interaction (not implemented)

        if overall_success: gs.trigger_ability(None, "LEARNED", {"controller": player, "drew": drew_card, "discarded": discarded_card})
        return reward, overall_success # True if draw or discard happened

    def _handle_venture(self, param, context, **kwargs):
        return self._handle_activate_ability(None, context or {})

    def _handle_explore(self, param, context, **kwargs):
        return self._handle_activate_ability(None, context or {})

    def _handle_adapt(self, param, context, **kwargs):
        return self._handle_activate_ability(None, context or {})

    def _handle_mutate(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        context = dict(context or {})
        context.update(kwargs.get('context', {}))
        hand_idx = context.get('hand_idx')

        if hand_idx is None:
            logging.warning(f"Mutate context missing hand index: {context}")
            return -0.15, False
        try:
            hand_idx = int(hand_idx)
        except (ValueError, TypeError):
            logging.warning(f"Mutate context has non-integer hand index: {context}")
            return -0.15, False
        if not 0 <= hand_idx < len(player.get("hand", [])):
            logging.warning(f"Mutate hand index out of bounds: {hand_idx}")
            return -0.15, False

        mutating_card_id = player["hand"][hand_idx]
        mutating_card = gs._safe_get_card(mutating_card_id)
        if not mutating_card: return -0.15, False

        match = re.search(
            r"\bmutate\s*((?:\{[^}]+\})+)",
            getattr(mutating_card, 'oracle_text', ''), re.IGNORECASE)
        if not match:
            logging.warning(f"Cannot mutate {mutating_card.name}: no mutate cost found.")
            return -0.05, False

        cast_context = dict(context)
        cast_context.update({
            "hand_idx": hand_idx,
            "source_idx": hand_idx,
            "source_zone": "hand",
            "use_alt_cost": "mutate",
            "cast_for_mutate": True,
            "effect_text": "Target non-Human creature you own.",
        })
        success = gs.cast_spell(mutating_card_id, player, context=cast_context)
        return (0.25 if success else -0.1), success

    def _handle_goad(self, param, context, **kwargs):
        return self._handle_activate_ability(None, context or {})

    def _handle_flip_card(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        target_idx = context.get('battlefield_idx')

        if target_idx is None: logging.warning(f"Flip Card context missing 'battlefield_idx'"); return -0.15, False
        try: target_idx = int(target_idx)
        except (ValueError, TypeError): logging.warning(f"Flip Card context non-integer index: {context}"); return -0.15, False

        if target_idx >= len(player["battlefield"]): logging.warning(f"Flip Card index out of bounds: {target_idx}"); return -0.15, False

        card_id = player["battlefield"][target_idx]
        success = False
        if hasattr(gs, 'flip_card') and callable(gs.flip_card):
             success = gs.flip_card(card_id)
        else: logging.error("flip_card method missing in GameState.")

        reward = 0.2 if success else -0.1
        return reward, success

    def _handle_equip(self, param, context, **kwargs): # Param is None
        gs = self.game_state
        player = self._get_policy_player(context)
        if context is None: context = {}

        equip_identifier = context.get('equipment_identifier', context.get('equip_identifier'))
        target_identifier = context.get('target_identifier')

        if equip_identifier is None or target_identifier is None:
            logging.error(f"Equip context missing required identifiers: {context}")
            return -0.15, False

        equip_id = self._find_permanent_id(player, equip_identifier)
        target_id = self._find_permanent_id(player, target_identifier)

        if not equip_id or not target_id:
            logging.warning(f"Equip failed: Invalid identifiers. Equip:'{equip_identifier}', Target:'{target_identifier}'")
            return -0.15, False

        equip_card = gs._safe_get_card(equip_id)
        # Get equip cost from card text
        equip_cost_str = None
        match = re.search(r"equip (\{[^\}]+\}|[0-9]+)", getattr(equip_card, 'oracle_text', '').lower())
        if match:
            equip_cost_str = match.group(1)
            if equip_cost_str.isdigit(): equip_cost_str = f"{{{equip_cost_str}}}" # Normalize cost

        if not equip_cost_str or not self._can_afford_cost_string(player, equip_cost_str):
            logging.debug(f"Cannot afford equip cost {equip_cost_str or 'N/A'} for {getattr(equip_card, 'name', equip_id)}")
            return -0.05, False

        if not gs.mana_system.pay_mana_cost(player, equip_cost_str):
            logging.warning(f"Failed to pay equip cost {equip_cost_str}")
            return -0.05, False

        success = False
        if hasattr(gs, 'equip_permanent') and gs.equip_permanent(player, equip_id, target_id):
            success = True
        else:
            logging.debug(f"Equip action failed validation or execution for {equip_id} -> {target_id}")
            # Rollback mana? Assume cost wasted.

        return (0.25 if success else -0.1), success

    def _handle_unequip(self, param, context, **kwargs):
        # Usually UNEQUIP is not a player action, but happens via Equip/Destroy/SBA
        # If mapped to NO_OP, this handler shouldn't be called.
        # If kept as a potential (non-standard) action:
        gs = self.game_state
        player = self._get_policy_player(context)
        equip_idx = context.get('equip_idx') # Context needed

        if equip_idx is None: logging.warning("Unequip context missing 'equip_idx'"); return -0.15, False
        try: equip_idx = int(equip_idx)
        except (ValueError, TypeError): logging.warning(f"Unequip context has non-integer index: {context}"); return -0.15, False

        if equip_idx >= len(player["battlefield"]): logging.warning(f"Unequip index out of bounds: {equip_idx}"); return -0.15, False

        equip_id = player["battlefield"][equip_idx]
        success = False
        if hasattr(gs, 'unequip_permanent'): success = gs.unequip_permanent(player, equip_id)
        else: logging.error("unequip_permanent method missing in GameState.")

        return (0.1 if success else -0.1), success

    def _handle_attach_aura(self, param, context, **kwargs):
        # Attach usually happens on spell resolution, not as a separate action.
        # This might be for effects that say "Attach target Aura..."
        gs = self.game_state
        player = self._get_policy_player(context)
        aura_id = context.get('aura_id')
        target_id = context.get('target_id')

        if not aura_id or not target_id:
            logging.warning(f"ATTACH_AURA context missing aura_id or target_id: {context}")
            return -0.15, False

        success = False
        if hasattr(gs, 'attach_aura'):
            success = gs.attach_aura(player, aura_id, target_id) # GS handles validation
        else: logging.error("attach_aura method missing in GameState.")

        if not success: logging.warning(f"Failed to attach aura {aura_id} to {target_id}")
        return (0.25 if success else -0.1), success

    def _handle_fortify(self, param, context, **kwargs): # Param is None
        gs = self.game_state
        player = self._get_policy_player(context)
        if context is None: context = {}

        fort_identifier = context.get('fort_identifier')
        target_identifier = context.get('target_identifier')

        if fort_identifier is None or target_identifier is None:
            logging.error(f"Fortify context missing required identifiers: {context}")
            return -0.15, False

        fort_id = self._find_permanent_id(player, fort_identifier)
        target_id = self._find_permanent_id(player, target_identifier)

        if not fort_id or not target_id:
             logging.warning(f"Fortify failed: Invalid identifiers. Fort:'{fort_identifier}', Target:'{target_identifier}'")
             return -0.15, False

        fort_card = gs._safe_get_card(fort_id)
        fort_cost_str = self._get_fortify_cost_str(fort_card)

        if not fort_cost_str or not self._can_afford_cost_string(player, fort_cost_str):
            logging.debug(f"Cannot afford fortify cost {fort_cost_str or 'N/A'} for {fort_id}")
            return -0.05, False

        if not gs.mana_system.pay_mana_cost(player, fort_cost_str):
            logging.warning(f"Failed to pay fortify cost {fort_cost_str}")
            return -0.05, False

        success = False
        if hasattr(gs, 'fortify_land') and gs.fortify_land(player, fort_id, target_id):
            success = True
        else:
            logging.debug(f"Fortify action failed validation or execution for {fort_id} -> {target_id}")
            # Rollback cost?

        return (0.2 if success else -0.1), success

    def _handle_reconfigure(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        card_identifier = context.get('card_identifier', context.get('battlefield_idx')) # Use context, fallback index

        if card_identifier is None:
             logging.error(f"Reconfigure context missing 'card_identifier': {context}")
             return -0.15, False

        card_id = self._find_permanent_id(player, card_identifier)
        if card_id is None:
             logging.warning(f"Reconfigure failed: Invalid identifier '{card_identifier}' -> {card_id}")
             return -0.15, False

        card = gs._safe_get_card(card_id)
        # --- Use GameState helpers for cost string retrieval ---
        reconf_cost_str = None
        if hasattr(gs, '_get_reconfigure_cost_str'):
             reconf_cost_str = gs._get_reconfigure_cost_str(card)

        if not reconf_cost_str or not self._can_afford_cost_string(player, reconf_cost_str):
            logging.debug(f"Cannot afford reconfigure cost {reconf_cost_str or 'N/A'} for {card_id}")
            return -0.05, False

        target_id = None # Determine target if attaching (needs logic or context)
        is_attached = hasattr(player, 'attachments') and card_id in player["attachments"]
        if not is_attached: # Trying to attach
             target_identifier_ctx = context.get('target_identifier')
             if target_identifier_ctx is None:
                  logging.error("Reconfigure attach requires target identifier in context.")
                  return -0.1, False # Expect agent choice via context
             target_id = self._find_permanent_id(player, target_identifier_ctx)
             if target_id is None:
                  logging.warning(f"Reconfigure attach target invalid: {target_identifier_ctx}")
                  return -0.1, False

        if not gs.mana_system.pay_mana_cost(player, reconf_cost_str):
            logging.warning(f"Failed to pay reconfigure cost {reconf_cost_str}")
            return -0.05, False

        success = False
        if hasattr(gs, 'reconfigure_permanent') and gs.reconfigure_permanent(player, card_id, target_id=target_id):
            success = True
        else: logging.debug(f"Reconfigure failed for {card_id}")

        return (0.2 if success else -0.1), success

    def _handle_morph(self, param, context, **kwargs):
        """Handle turning morphed card face up. Expects battlefield_idx in context."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        # context passed from apply_action
        card_idx = context.get('battlefield_idx') # Get from context

        if card_idx is not None:
            try: card_idx = int(card_idx)
            except (ValueError, TypeError):
                logging.warning(f"Morph context has non-integer index: {context}")
                return (-0.15, False)

            if card_idx < len(player["battlefield"]):
                card_id = player["battlefield"][card_idx]
                # GS method checks if morphed, face down, and pays cost
                success = gs.turn_face_up(player, card_id, pay_morph_cost=True)
                return (0.3, success) if success else (-0.1, False)
            else: logging.warning(f"Morph index out of bounds: {card_idx}")
        else: logging.warning(f"Morph context missing 'battlefield_idx'")
        return (-0.15, False)

    def _handle_clash(self, param, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(kwargs.get('context'))
        opponent = self._get_policy_opponent(kwargs.get('context'))

        winner = None
        if hasattr(gs, 'clash') and callable(gs.clash):
            winner = gs.clash(player, opponent)
            # Clash itself is successful regardless of win/loss
            reward = 0.1 if winner == player else (0.0 if winner is None else -0.05)
            return reward, True # Action performed successfully
        else:
             logging.error("Clash method missing in GameState.")
             return -0.1, False # Cannot perform action

    def _handle_grandeur(self, param, context, **kwargs):
        gs = self.game_state; player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx')

        if hand_idx is None: logging.warning(f"Grandeur context missing 'hand_idx'"); return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError): logging.warning(f"Grandeur context has non-integer index: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Grandeur hand index out of bounds: {hand_idx}"); return -0.1, False

        card_id_to_discard = player["hand"][hand_idx] # Do NOT pop yet
        discard_card = gs._safe_get_card(card_id_to_discard)
        if not discard_card: return -0.1, False

        grandeur_id_on_bf = None; grandeur_bf_idx = -1
        for bf_idx, bf_id in enumerate(player["battlefield"]):
            bf_card = gs._safe_get_card(bf_id)
            if bf_card and bf_card.name == discard_card.name and "grandeur" in getattr(bf_card,'oracle_text','').lower():
                grandeur_id_on_bf = bf_id; grandeur_bf_idx = bf_idx; break

        if not grandeur_id_on_bf:
            logging.warning(f"No card named {discard_card.name} with Grandeur found on battlefield.")
            return -0.1, False

        # Discard the card (pay cost)
        success_discard = gs.move_card(card_id_to_discard, player, "hand", player, "graveyard", cause="grandeur_cost")
        if not success_discard: logging.warning(f"Grandeur failed: Could not discard {discard_card.name}."); return -0.05, False

        # Activate the ability (assume index 0 or find specific grandeur ability)
        # --- Needs specific context/logic to find the correct ability index ---
        grandeur_ability_idx = 0 # Placeholder - find actual index

        success_ability = False
        if hasattr(gs, 'ability_handler'):
            success_ability = gs.ability_handler.activate_ability(grandeur_id_on_bf, grandeur_ability_idx, player)
        else: logging.error("Cannot activate Grandeur: AbilityHandler missing.")

        # Reward success, but action successful even if ability fizzles/fails after cost paid
        reward = 0.35 if success_ability else 0.0 # Base reward for performing discard+activation attempt
        return reward, True # Cost paid, activation attempted = successful action

    def _handle_create_token(self, param, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(kwargs.get('context'))
        token_data = gs.get_token_data_by_index(param) # Index 0-4
        if not token_data:
            logging.error(f"CREATE_TOKEN failed: No data found for index {param}.")
            return -0.15, False

        success = gs.create_token(player, token_data)
        reward = 0.15 if success else -0.1
        return reward, success

    def _handle_cycling(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx')

        if hand_idx is None: logging.warning(f"Cycling context missing 'hand_idx'"); return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError): logging.warning(f"Cycling context has non-integer index: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Cycling index out of bounds: {hand_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        card = gs._safe_get_card(card_id)
        if not card or "cycling" not in getattr(card, 'oracle_text', '').lower():
            logging.warning(f"Cycling card {card_id} invalid or has no Cycling."); return -0.1, False

        cost_match = re.search(r"cycling (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
        if not cost_match: logging.warning(f"Cycling cost parse failed for {card.name}"); return -0.1, False

        cost_str = cost_match.group(1)
        if cost_str.isdigit(): cost_str = f"{{{cost_str}}}"

        # Match the mask's untapped-land affordability; payment auto-taps.
        if not gs.mana_system.can_pay_mana_cost_with_lands(player, cost_str): logging.debug(f"Cycling failed: Cannot afford cost {cost_str}"); return -0.05, False
        if not gs.mana_system.pay_mana_cost(player, cost_str): logging.warning(f"Cycling cost payment failed for {card.name}"); return -0.05, False

        success_discard = gs.move_card(card_id, player, "hand", player, "graveyard", cause="cycling_discard")
        if success_discard:
            gs._draw_phase(player) # GS handles empty library etc.
            gs.trigger_ability(card_id, "CYCLING", {"controller": player})
            return 0.1, True # Success
        else: # Discard failed
            # Mana cost rollback? Assume wasted.
            logging.error(f"Cycling move failed for {card.name}")
            return -0.05, False

    def _handle_manifest(self, param, context, **kwargs):
            """Handle turning a manifested card face up. Expects battlefield_idx in context."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            # context passed from apply_action
            card_idx = context.get('battlefield_idx') # Get from context

            if card_idx is not None:
                try: card_idx = int(card_idx)
                except (ValueError, TypeError):
                    logging.warning(f"Manifest context has non-integer index: {context}")
                    return (-0.15, False)

                if card_idx < len(player["battlefield"]):
                    card_id = player["battlefield"][card_idx]
                    # GS method checks if manifested, face down, is creature, and pays cost
                    success = gs.turn_face_up(player, card_id, pay_manifest_cost=True)
                    return (0.25, success) if success else (-0.1, False)
                else: logging.warning(f"Manifest index out of bounds: {card_idx}")
            else: logging.warning(f"Manifest context missing 'battlefield_idx'")
            return (-0.15, False)

