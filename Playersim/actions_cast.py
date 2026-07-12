"""Handlers for playing lands, casting spells, costs, and stack responses.

Extracted from actions.py. This module defines behavior only (a mixin);
all state lives on ActionHandler, which composes every mixin.
"""

import logging
import re


class CastingHandlersMixin:
    """Handlers for playing lands, casting spells, costs, and stack responses."""

    __slots__ = ()

    def _handle_pay_offspring_cost(self, param, context=None, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        pending_context = getattr(gs, 'pending_spell_context', None)

        if not pending_context or 'card_id' not in pending_context:
            logging.warning("PAY_OFFSPRING_COST called but no spell context is pending.")
            return -0.1, False

        card_id = pending_context['card_id']
        card = gs._safe_get_card(card_id)
        if not card or not getattr(card, 'is_offspring', False):
            logging.warning(f"Cannot PAY_OFFSPRING_COST: Card {card_id} not found or has no Offspring.")
            return -0.05, False

        offspring_cost_str = getattr(card, 'offspring_cost', None)
        if not offspring_cost_str:
            logging.warning(f"Offspring cost not found on card {card_id}.")
            return -0.05, False

        # Pass existing pending_context to affordability check
        if not self._can_afford_cost_string(player, offspring_cost_str, context=pending_context):
            logging.debug(f"Cannot afford Offspring cost {offspring_cost_str} for {card.name}")
            return -0.05, False

        pending_context['pay_offspring'] = True
        pending_context['offspring_cost_to_pay'] = offspring_cost_str
        logging.debug(f"Offspring cost context flag set for pending {card.name}")
        return 0.01, True # Successful flag setting

    def _handle_cast_for_impending(self, param, context=None, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        if context is None: context = {}
        if kwargs.get('context'): context.update(kwargs['context'])

        hand_idx = context.get('hand_idx')
        if hand_idx is None or not isinstance(hand_idx, int) or hand_idx >= len(player.get("hand", [])):
            logging.error(f"CAST_FOR_IMPENDING missing or invalid 'hand_idx' in context: {context}")
            return -0.15, False

        card_id = player["hand"][hand_idx]
        card = gs._safe_get_card(card_id)
        if not card or not getattr(card, 'is_impending', False):
            logging.warning(f"Card {card_id} at index {hand_idx} does not have Impending.")
            return -0.05, False

        impending_cost_str = getattr(card, 'impending_cost', None)
        if not impending_cost_str:
             logging.warning(f"Impending cost not found for card {card_id} at index {hand_idx}.")
             return -0.05, False

        # Create context for casting
        cast_context = context.copy()
        cast_context['use_alt_cost'] = 'impending'
        cast_context['card_id'] = card_id
        cast_context['hand_idx'] = hand_idx
        cast_context['source_zone'] = 'hand'
        # --- Flag needed for move_card/ETB ---
        cast_context['cast_for_impending'] = True

        if not gs.mana_system.can_pay_replacing_cost_with_lands(
                player, card_id, impending_cost_str, 'impending',
                context=cast_context):
            logging.debug(
                f"Cannot afford Impending cost {impending_cost_str} "
                f"for {card.name}")
            return -0.05, False

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.25 if success else -0.1
        return reward, success

    def _handle_play_land(self, param, **kwargs):
        gs = self.game_state
        context = kwargs.get('context', {})
        player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx', param)

        if not isinstance(hand_idx, int) or hand_idx < 0:
            self.last_handler_error = (
                f"PLAY_LAND received invalid hand index {hand_idx!r}")
            return -0.2, False

        if hand_idx >= len(player.get("hand", [])):
            self.last_handler_error = (
                f"PLAY_LAND hand index {hand_idx} is outside a "
                f"{len(player.get('hand', []))}-card hand")
            logging.warning(self.last_handler_error)
            return -0.2, False

        card_id = player["hand"][hand_idx]
        expected_card_id = context.get('card_id')
        if expected_card_id is not None and card_id != expected_card_id:
            # A generated action must keep referring to the card it exposed.
            # Duplicate printed IDs are interchangeable, so relocating the
            # expected ID is safe; otherwise this is a stale action contract.
            if expected_card_id in player.get("hand", []):
                hand_idx = player["hand"].index(expected_card_id)
                card_id = expected_card_id
            else:
                self.last_handler_error = (
                    f"PLAY_LAND stale hand slot {context.get('hand_idx', param)}: "
                    f"expected card {expected_card_id}, found {card_id}")
                logging.warning(self.last_handler_error)
                return -0.2, False

        success = gs.play_land(card_id, player, play_back_face=context.get('play_back_face', False))
        if success:
            self.last_handler_error = None
            return 0.2, True # Success
        else:
            card = gs._safe_get_card(card_id, None)
            active_player = gs._get_active_player()
            self.last_handler_error = (
                "PLAY_LAND rejected after mask validation: "
                f"card_id={card_id}, card={getattr(card, 'name', None)!r}, "
                f"hand_idx={hand_idx}, in_hand={card_id in player.get('hand', [])}, "
                f"land_played={bool(player.get('land_played', False))}, "
                f"phase={getattr(gs, 'phase', None)}, "
                f"active_controller={'p1' if active_player is gs.p1 else 'p2'}, "
                f"acting_controller={'p1' if player is gs.p1 else 'p2'}, "
                f"back_face={bool(context.get('play_back_face', False))}")
            logging.warning(self.last_handler_error)
            return -0.1, False # Failure

    def _handle_play_from_graveyard(self, param, context=None, **kwargs):
        """Use an explicit graveyard play/cast permission."""
        gs = self.game_state
        player = self._get_policy_player(context)
        context = dict(context or {})
        source_index = context.get("source_idx", param)
        if context.get("harmonize_cast"):
            if (not isinstance(source_index, int)
                    or not 0 <= source_index < len(
                        player.get("graveyard", []))):
                return -0.15, False
            card_id = player["graveyard"][source_index]
            harmonize_cost = gs.harmonize_cost_for(player, card_id)
            if not harmonize_cost:
                return -0.15, False
            candidates = [
                creature_id for creature_id in player.get("battlefield", [])
                if creature_id not in player.get("tapped_permanents", set())
                and "creature" in getattr(
                    gs._safe_get_card(creature_id), "card_types", [])]
            cast_context = dict(context)
            cast_context.update({
                "source_zone": "graveyard", "source_idx": source_index,
                "harmonize_cast": True, "harmonize_cost": harmonize_cost,
                "use_alt_cost": "harmonize",
            })
            def _payable(reduction):
                cost = gs.mana_system.calculate_alternative_cost(
                    card_id, player, "harmonize", {
                        "harmonize_cost": harmonize_cost,
                        "harmonize_reduction": reduction,
                    })
                return bool(cost is not None and
                    gs.mana_system.can_pay_mana_cost_with_lands(
                        player, cost, {"card": gs._safe_get_card(card_id)}))

            can_decline = _payable(0)
            candidates = [
                creature_id for creature_id in candidates
                if _payable(max(0, int(getattr(
                    gs._safe_get_card(creature_id), "power", 0) or 0)))]
            if candidates:
                gs.choice_context = {
                    "type": "harmonize_tap", "player": player,
                    "card_id": card_id, "options": candidates,
                    "cast_context": cast_context,
                    "can_decline": can_decline,
                    "resume_phase": gs.phase,
                }
                gs.phase = gs.PHASE_CHOOSE
                gs.priority_player = player
                return 0.05, True
            if not can_decline:
                return -0.1, False
            success = gs.cast_spell(card_id, player, context=cast_context)
            return (0.2, True) if success else (-0.1, False)
        if context.get("flashback_cast"):
            if (not isinstance(source_index, int)
                    or not 0 <= source_index < len(player.get("graveyard", []))):
                return -0.15, False
            card_id = player["graveyard"][source_index]
            flashback_cost = gs.flashback_cost_for(player, card_id)
            if not flashback_cost:
                return -0.15, False
            cast_context = dict(context)
            cast_context.update({
                "source_zone": "graveyard", "source_idx": source_index,
                "flashback_cast": True, "flashback_cost": flashback_cost,
                "use_alt_cost": "flashback",
            })
            success = gs.cast_spell(card_id, player, context=cast_context)
            return (0.2, True) if success else (-0.1, False)
        if context.get("graveyard_adventure_cast"):
            if (not isinstance(source_index, int)
                    or not 0 <= source_index < len(player.get("graveyard", []))):
                return -0.15, False
            card_id = player["graveyard"][source_index]
            if not gs.has_graveyard_adventure_permission(player, card_id):
                return -0.15, False
            cast_context = dict(context)
            cast_context.update({
                "source_zone": "graveyard",
                "source_idx": source_index,
                "cast_as_adventure": True,
                "graveyard_adventure_cast": True,
            })
            success = gs.cast_spell(card_id, player, context=cast_context)
            return (0.2, True) if success else (-0.1, False)
        if not any(
                emblem.get("kind") == "graveyard_permanents"
                for emblem in player.get("emblems", [])):
            return -0.15, False
        if (not isinstance(source_index, int)
                or not 0 <= source_index < len(player.get("graveyard", []))):
            return -0.15, False
        card_id = player["graveyard"][source_index]
        card = gs._safe_get_card(card_id)
        if not card:
            return -0.15, False
        card_types = set(getattr(card, "card_types", []))
        if "land" in card_types:
            success = gs.play_land(
                card_id, player, source_zone="graveyard",
                permission="graveyard_permanents")
            return (0.2, True) if success else (-0.1, False)
        if not card_types.intersection(
                {"creature", "artifact", "enchantment", "planeswalker", "battle"}):
            return -0.1, False
        cast_context = dict(context or {})
        cast_context.update({
            "source_zone": "graveyard",
            "source_idx": source_index,
            "emblem_graveyard_cast": True,
        })
        success = gs.cast_spell(card_id, player, context=cast_context)
        return (0.2, True) if success else (-0.1, False)

    def _handle_play_spell(self, param, **kwargs):
        gs = self.game_state
        context = kwargs.get('context', {})
        player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx', param)

        if (not isinstance(hand_idx, int) or hand_idx < 0
                or hand_idx >= len(player.get("hand", []))):
            self.last_handler_error = (
                f"PLAY_SPELL invalid hand index {hand_idx!r} for a "
                f"{len(player.get('hand', []))}-card hand")
            logging.warning(self.last_handler_error)
            return -0.2, False

        card_id = player["hand"][hand_idx]
        expected_card_id = context.get('card_id')
        if expected_card_id is not None and card_id != expected_card_id:
            if expected_card_id in player.get("hand", []):
                hand_idx = player["hand"].index(expected_card_id)
                card_id = expected_card_id
            else:
                self.last_handler_error = (
                    f"PLAY_SPELL stale hand slot {context.get('hand_idx', param)}: "
                    f"expected card {expected_card_id}, found {card_id}")
                logging.warning(self.last_handler_error)
                return -0.2, False
        card = gs._safe_get_card(card_id)
        if not card: return -0.2, False

        if 'hand_idx' not in context: context['hand_idx'] = hand_idx
        if 'source_zone' not in context: context['source_zone'] = 'hand'

        card_value = 0
        if self.card_evaluator:
            eval_context = {"situation": "casting", "current_phase": gs.phase, **context}
            card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context)

        success = gs.cast_spell(card_id, player, context=context)
        if success:
            self.last_handler_error = None
            return 0.1 + card_value * 0.3, True # Success
        else:
            self.last_handler_error = (
                "PLAY_SPELL rejected after mask validation: "
                f"card_id={card_id}, card={getattr(card, 'name', None)!r}, "
                f"hand_idx={hand_idx}, in_hand={card_id in player.get('hand', [])}, "
                f"phase={getattr(gs, 'phase', None)}, "
                f"underlying_phase={getattr(gs, 'previous_priority_phase', None)}, "
                f"priority_controller="
                f"{'p1' if gs.priority_player is gs.p1 else 'p2' if gs.priority_player is gs.p2 else None}, "
                f"acting_controller={'p1' if player is gs.p1 else 'p2'}, "
                f"stack_size={len(getattr(gs, 'stack', []))}")
            logging.warning(self.last_handler_error)
            logging.debug(f"PLAY_SPELL: Failed (handled by gs.cast_spell). Card: {card_id}")
            return -0.1, False # Failure

    def _handle_play_mdfc_land_back(self, param, **kwargs):
        context = kwargs.get('context', {})
        context['play_back_face'] = True # Ensure flag is set
        # Use standard play_land handler with modified context
        return self._handle_play_land(param, context=context) # Returns (reward, success)

    def _handle_play_mdfc_back(self, param, **kwargs):
        context = kwargs.get('context', {})
        context['cast_as_back_face'] = True # Ensure flag matches cast_spell
        # Use standard play_spell handler with modified context
        return self._handle_play_spell(param, context=context) # Returns (reward, success)

    def _handle_play_adventure(self, param, **kwargs):
        context = kwargs.get('context', {})
        context['cast_as_adventure'] = True # Ensure flag is set
        # Use standard play_spell handler with modified context
        return self._handle_play_spell(param, context=context) # Returns (reward, success)

    def _handle_cast_from_exile(self, param, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(kwargs.get('context'))
        context = dict(kwargs.get('context', {}) or {})
        castable_options = gs.get_exile_cast_options(player)

        if param >= len(castable_options):
             logging.warning(f"CAST_FROM_EXILE: Invalid index {param}, only {len(castable_options)} available.")
             return -0.2, False

        option = castable_options[param]
        card_id = option["card_id"]
        # Verify card still exists in player's exile (might have moved)
        if card_id not in player.get("exile",[]):
             logging.warning(f"CAST_FROM_EXILE: Card {card_id} no longer in {player['name']}'s exile.")
             return -0.15, False

        context['source_zone'] = 'exile'
        context['source_idx'] = option.get('source_idx', player['exile'].index(card_id))
        if option.get("permission") == "plot":
            context['use_alt_cost'] = 'plot'
            context['plot_cast'] = True
        elif option.get("permission") == "airbend":
            context['use_alt_cost'] = 'exile_permission'
            context['alternative_cost'] = option.get('alternative_cost', '{2}')
            context['airbend_cast'] = True

        card_value = 0
        if self.card_evaluator:
            card_value = self.card_evaluator.evaluate_card(card_id, "play")

        success = gs.cast_spell(card_id, player, context=context)
        if success:
            return 0.2 + card_value * 0.3, True # Success
        else:
            logging.debug(f"CAST_FROM_EXILE: Failed (handled by gs.cast_spell). Card: {card_id}")
            return -0.1, False # Failure

    def _handle_plot_card(self, param, context=None, **kwargs):
        """Take the hand-indexed Plot action or cast a spell for Warp."""
        gs = self.game_state
        player = self._get_policy_player(context)
        if (context or {}).get("warp_cast"):
            if not isinstance(param, int) or not 0 <= param < len(player.get("hand", [])):
                return -0.1, False
            card_id = player["hand"][param]
            card = gs._safe_get_card(card_id)
            warp_cost = getattr(card, "warp_cost", None) if card else None
            if not warp_cost:
                return -0.1, False
            success = gs.cast_spell(card_id, player, {
                "source_zone": "hand", "source_idx": param,
                "warp_cast": True, "use_alt_cost": "exile_permission",
                "alternative_cost": warp_cost,
            })
            return (0.2, True) if success else (-0.1, False)
        success = gs.plot_card(player, param)
        return (0.1, True) if success else (-0.1, False)

    def _handle_tap_land_for_mana(self, param, **kwargs):
         gs = self.game_state
         player = self._get_policy_player(kwargs.get('context'))
         land_idx = param

         if land_idx >= len(player.get("battlefield", [])):
             logging.warning(f"TAP_LAND_FOR_MANA: Invalid land index {land_idx}")
             return -0.2, False

         card_id = player["battlefield"][land_idx]
         success = False
         if gs.mana_system and hasattr(gs.mana_system, 'tap_land_for_mana'):
             success = gs.mana_system.tap_land_for_mana(player, card_id)
         else:
             logging.warning("TAP_LAND_FOR_MANA: ManaSystem not available or missing method.")

         if success:
             return 0.05, True # Success
         else:
             card_name = getattr(gs._safe_get_card(card_id), 'name', card_id)
             logging.warning(f"TAP_LAND_FOR_MANA: Failed (handled by gs.mana_system). Card: {card_name}")
             return -0.1, False # Failure

    def _handle_tap_land_for_effect(self, param, **kwargs):
         gs = self.game_state
         player = self._get_policy_player(kwargs.get('context'))
         land_idx = param
         # Assume ability index 0 for non-mana tap ability from context
         context = kwargs.get('context', {})
         ability_idx = context.get('ability_idx', 0)

         if land_idx >= len(player.get("battlefield", [])):
             logging.warning(f"TAP_LAND_FOR_EFFECT: Invalid land index {land_idx}")
             return -0.2, False

         card_id = player["battlefield"][land_idx]
         card = gs._safe_get_card(card_id)
         if not card or 'land' not in getattr(card,'type_line',''):
             logging.warning(f"TAP_LAND_FOR_EFFECT: Card {card_id} not a land.")
             return -0.15, False

         if not hasattr(gs, 'ability_handler'):
             logging.error("TAP_LAND_FOR_EFFECT: AbilityHandler not found.")
             return -0.15, False

         # Use the generic activate ability handler now
         success = gs.ability_handler.activate_ability(card_id, ability_idx, player)
         if success:
             return 0.15, True # Land effects can be good
         else:
             logging.debug(f"TAP_LAND_FOR_EFFECT failed for {card.name}, ability {ability_idx} (handled by activate_ability).")
             return -0.1, False # Failure

    def _handle_discard_card(self, param, **kwargs):
        gs = self.game_state
        context = getattr(gs, "choice_context", None)
        if not (gs.phase == gs.PHASE_CHOOSE and context
                and context.get("type") in ["discard", "specialize_discard"]):
             logging.warning("DISCARD_CARD called outside a discard choice.")
             return -0.2, False

        player = context.get("player")
        acting_player = gs.p1 if gs.agent_is_p1 else gs.p2
        if player != acting_player:
             logging.warning("DISCARD_CARD called by a player without choice authority.")
             return -0.2, False

        hand_idx = int(context.get("choice_page", 0)) * 10 + param
        if not isinstance(hand_idx, int) or not 0 <= hand_idx < len(player.get("hand", [])):
             logging.warning(f"DISCARD_CARD: Invalid hand index {hand_idx}")
             return -0.2, False

        card_id = player["hand"][hand_idx]
        value = self.card_evaluator.evaluate_card(card_id, "discard") if self.card_evaluator else 0
        if context.get("type") == "specialize_discard":
             success = gs.choose_specialize_discard(hand_idx)
        else:
             success = gs.choose_discard_card(hand_idx)
        reward = -0.05 - value * 0.2
        return reward, success

    def _get_madness_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
            match = re.search(r"madness (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
            if match:
                 cost_str = match.group(1)
                 if cost_str.isdigit(): return f"{{{cost_str}}}"
                 return cost_str
        return None

    def _handle_pay_kicker(self, param, context, **kwargs):
        """Flag intent to pay kicker. param=True/False. Checks affordability."""
        if context is None: context = {}
        context.update(kwargs.get('context', {})) # Merge from kwargs

        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if not pending_context or 'card_id' not in pending_context:
            logging.warning("PAY_KICKER called but no spell context is pending.")
            return -0.1, False # No spell context pending

        card_id = pending_context['card_id']
        card = self.game_state._safe_get_card(card_id)
        if not card or "kicker" not in getattr(card, 'oracle_text', '').lower():
             logging.warning(f"Cannot set kicker flag: Card {card_id} not found or has no kicker.")
             return -0.05, False

        kicker_cost_str = self._get_kicker_cost_str(card) # Use helper
        player = self._get_policy_player(context)

        # If trying to pay, check affordability now
        if param is True:
            if kicker_cost_str and self._can_afford_cost_string(player, kicker_cost_str, context=pending_context):
                pending_context['kicked'] = True
                # Store the cost string itself for ManaSystem to use later
                pending_context['kicker_cost_to_pay'] = kicker_cost_str
                logging.debug(f"Kicker context flag set to True for pending {card.name} (Cost: {kicker_cost_str})")
                return 0.01, True
            else:
                logging.warning(f"Cannot afford kicker cost {kicker_cost_str or 'N/A'} for {card.name}")
                return -0.05, False # Cannot set kicker=True if unaffordable or no cost
        else: # param is False
            pending_context['kicked'] = False
            pending_context.pop('kicker_cost_to_pay', None) # Remove cost if not paying
            logging.debug(f"Kicker context flag set to False for pending {card.name}")
            return 0.01, True

    def _get_kicker_cost_str(self, card):
        """Helper to extract kicker cost string (mana or bare-number forms).

        BUGFIX (July 2026 sweep): the fallback branch read .group(1) on a
        regex with NO capture group -> IndexError('no such group') on any
        card whose kicker cost wasn't in the '{X}' braces form immediately
        after 'kicker'. That crash propagated up through PAY_KICKER and could
        abort the whole cast. The fallback now captures its cost.
        """
        if card and hasattr(card, 'oracle_text'):
            text = card.oracle_text.lower()
            # Prioritize a braces cost directly after the word 'kicker'.
            direct_match = re.search(r"\bkicker\s*(\{.*?\})", text)
            if direct_match:
                return direct_match.group(1)
            # Fallback: 'kicker' followed by a braces OR a bare number
            # ('kicker 3'). Capture the cost so .group(1) is always valid.
            later_match = re.search(r"kicker\s+(\{[^\}]+\}|[0-9]+)", text)
            if later_match:
                cost_str = later_match.group(1)
                if cost_str.isdigit():
                    return f"{{{cost_str}}}"
                return cost_str
        return None

    def _handle_pay_additional_cost(self, param, context, **kwargs):
        """Flag intent to pay additional costs. param=True/False"""
        if context is None: context = {}
        context.update(kwargs.get('context', {})) # Merge from kwargs

        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if not pending_context or 'card_id' not in pending_context:
             logging.warning("PAY_ADDITIONAL_COST called but no spell context is pending.")
             return -0.1, False

        card_id = pending_context['card_id']
        card = self.game_state._safe_get_card(card_id)
        cost_info = self._get_additional_cost_info(card) if card else None # Use helper

        if not cost_info:
             logging.warning(f"PAY_ADDITIONAL_COST called, but no additional cost found on {card.name}")
             return -0.05, False # Action inappropriate if no cost

        player = self._get_policy_player(context)

        # If trying to pay, check if the non-mana part can be met (mana checked later)
        if param is True:
            # Pass pending context to check helper for costs like Escape
            if self._can_pay_specific_additional_cost(player, cost_info, pending_context):
                pending_context['pay_additional'] = True
                # Store cost info for cast_spell/pay_mana_cost to handle actual payment/action
                pending_context['additional_cost_info'] = cost_info
                logging.debug(f"Additional Cost context flag set to True for pending {card.name}")
                # Agent might need follow-up actions (e.g., SACRIFICE_PERMANENT) if choice is required
                return 0.01, True
            else:
                 logging.warning(f"Cannot meet non-mana part of additional cost for {card.name}")
                 return -0.05, False
        else: # param is False
             if cost_info.get("optional", True): # Can only choose not to pay if optional (Rule 601.2b)
                 # Need to parse if cost is optional from text? Assume mandatory if pattern matched.
                 logging.warning("Skipping mandatory additional cost is usually not allowed.")
                 return -0.05, False # Cannot skip mandatory cost
                 # If optional costs are added later, this needs refinement.
             else:
                  # Cost is mandatory, player *must* choose param=True if able
                  logging.warning("Cannot choose not to pay a mandatory additional cost.")
                  return -0.05, False

    def _can_pay_specific_additional_cost(self, player, cost_info, context):
        """Check if the non-mana part of an additional cost can be met."""
        gs = self.game_state
        cost_type = cost_info.get("type")

        if cost_type == "sacrifice":
            target_type = cost_info.get("target")
            # Check if there's *at least one* valid permanent to sacrifice
            return any(target_type == "permanent" or target_type in getattr(gs._safe_get_card(cid), 'card_types', [])
                       for cid in player.get("battlefield", []))
        elif cost_type == "discard":
            return len(player.get("hand", [])) >= cost_info.get("count", 1)
        elif cost_type == "pay_life":
             return player.get("life", 0) >= cost_info.get("amount", 0)
        elif cost_type == "tap_permanents":
            count_needed = cost_info.get("count", 1)
            target_type = cost_info.get("target_type")
            untapped_matching = 0
            tapped_set = player.get("tapped_permanents", set())
            for cid in player.get("battlefield", []):
                 if cid not in tapped_set:
                      card = gs._safe_get_card(cid)
                      if card and (target_type == "permanent" or target_type in getattr(card, 'card_types', []) or target_type in getattr(card, 'subtypes',[])):
                           untapped_matching += 1
            return untapped_matching >= count_needed
        elif cost_type == "escape_exile": # Check if enough cards in GY are provided
            gy_indices = context.get("gy_indices_escape", []) # indices provided by agent
            valid_indices = [idx for idx in gy_indices if idx < len(player.get("graveyard",[]))]
            # Need the required count from card text (assumed already parsed into context/cost_info?)
            # Let's retrieve it again or assume it's implicitly checked by caller providing enough indices.
            required_count = cost_info.get("count", 0) # Assume count is stored here if needed
            if required_count == 0: # Re-parse if missing
                match = re.search(r"exile (\w+|\d+) other cards?", cost_info.get('description','').lower())
                if match: required_count = self._word_to_number(match.group(1))
            return len(valid_indices) >= required_count
        elif cost_type == "delve": # Just need *some* cards in GY
            return len(player.get("graveyard",[])) > 0


        # Assume true if type unknown or check not implemented, cast_spell will fail later if needed
        logging.warning(f"Cannot validate non-mana additional cost type: {cost_type}")
        return True

    def _get_additional_cost_info(self, card):
        """Helper to identify additional costs (sacrifice, discard, pay life etc.)."""
        if card and hasattr(card, 'oracle_text'):
            text = card.oracle_text.lower()
            # Pattern for "As an additional cost to cast..., [ACTION]"
            # Handles variations like "to cast this spell" or "to cast ~"
            match = re.search(r"as an additional cost to cast (?:this spell|.*?),\s+(.+?)(?:\.|$|,)", text)
            if match:
                cost_desc = match.group(1).strip()
                # Sacrifice Creature
                sac_match = re.search(r"sacrifice (a|an|\d*)?\s*(\w+)", cost_desc)
                if sac_match and sac_match.group(2) in ["creature", "artifact", "enchantment", "land", "permanent", "planeswalker"]:
                    return {"type": "sacrifice", "target": sac_match.group(2), "optional": False, "description": cost_desc}
                # Discard Card
                disc_match = re.search(r"discard (\w+|\d*) cards?", cost_desc)
                if disc_match:
                    count = self._word_to_number(disc_match.group(1))
                    return {"type": "discard", "count": count, "optional": False, "description": cost_desc}
                # Pay Life
                life_match = re.search(r"pay (\d+) life", cost_desc)
                if life_match:
                    amount = int(life_match.group(1))
                    return {"type": "pay_life", "amount": amount, "optional": False, "description": cost_desc}
                # Tap Permanents
                tap_match = re.search(r"tap (\w+|\d*) untapped ([\w\s]+?) you control", cost_desc)
                if tap_match:
                     count = self._word_to_number(tap_match.group(1))
                     target_type = tap_match.group(2).strip().replace('s','') # Singularize
                     return {"type": "tap_permanents", "count": count, "target_type": target_type, "optional": False, "description": cost_desc}

                # Unrecognized additional cost type within the pattern
                logging.debug(f"Unrecognized additional cost pattern: {cost_desc}")
                return {"type": "unknown", "optional": False, "description": cost_desc} # Mark as unknown cost
        return None

    def _word_to_number(self, word):
        """Convert word representation of number to int."""
        if isinstance(word, int): return word
        if isinstance(word, str) and word.isdigit(): return int(word)
        mapping = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                   "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        return mapping.get(str(word).lower(), 1) # Default to 1 if word not found

    def _handle_pay_escalate(self, param, context, **kwargs):
        """Set number of extra modes chosen via escalate. param=count (e.g., 1 or 2). Checks affordability."""
        if context is None: context = {}
        context.update(kwargs.get('context', {})) # Merge from kwargs

        num_extra_modes = param if isinstance(param, int) and param >= 0 else 0

        pending_context = getattr(self.game_state, 'pending_spell_context', None)
        if not pending_context or 'card_id' not in pending_context:
             logging.warning("PAY_ESCALATE called but no spell context is pending.")
             return -0.1, False

        card_id = pending_context['card_id']
        card = self.game_state._safe_get_card(card_id)
        if not card or "escalate" not in getattr(card, 'oracle_text', '').lower():
             logging.warning(f"PAY_ESCALATE called, but card {card_id} not found or has no Escalate.")
             return -0.05, False

        escalate_cost_str = self._get_escalate_cost_str(card) # Use helper
        player = self._get_policy_player(context)

        # Check affordability for *each* extra mode
        if not escalate_cost_str:
             logging.warning(f"Cannot parse escalate cost for {card.name}")
             return -0.05, False

        # Check if *total* mana for escalate can be paid (relative to base cost affordability)
        # This is complex. ManaSystem needs to verify combined cost later.
        # Simple check: can afford escalate cost N times *in addition* to base? Hard to isolate.
        # We will just store the intent here, and let ManaSystem check during payment.
        # Basic affordability check of just the escalate cost:
        if num_extra_modes > 0 and not self._can_afford_cost_string(player, escalate_cost_str, context=pending_context):
             logging.warning(f"Cannot afford *one* instance of escalate cost {escalate_cost_str} for {card.name}")
             # Note: This doesn't guarantee affordability for N instances + base cost.
             # Maybe don't even check here and let cast_spell fail? Less informative.
             # Let's allow setting intent, fail at cast if overall cost too high.

        # TODO: Add check against number of modes available on the card vs. extra modes chosen.
        # Needs mode parsing first.

        pending_context['escalate_count'] = num_extra_modes
        pending_context['escalate_cost_each'] = escalate_cost_str # Store cost per mode for ManaSystem
        logging.debug(f"Escalate context flag set to {num_extra_modes} for pending {card.name}")
        return 0.01, True

    def _handle_copy_spell(self, param, context, **kwargs):
        gs = self.game_state; player = self._get_policy_player(context)
        target_identifier = context.get('target_stack_identifier', context.get('target_spell_idx'))

        if target_identifier is None:
             logging.warning(f"Copy Spell context missing 'target_stack_identifier'")
             return -0.15, False

        target_stack_item = None
        if isinstance(target_identifier, int):
             if 0 <= target_identifier < len(gs.stack) and gs.stack[target_identifier][0] == "SPELL":
                  target_stack_item = gs.stack[target_identifier]
        else:
            for item in gs.stack:
                 if item[0] == "SPELL" and item[1] == target_identifier: target_stack_item = item; break

        if not target_stack_item:
             logging.warning(f"Target stack item not found or not a spell: {target_identifier}")
             return -0.15, False

        _, card_id, _, _ = target_stack_item
        card = gs._safe_get_card(card_id)
        if not card:
            logging.warning(f"Spell card {card_id} not found for copy.")
            return -0.15, False

        copy_id = gs.copy_spell_on_stack(
            target_stack_item,
            player,
            copied_by=context.get("source_id"),
            allow_new_targets=context.get("allow_new_targets", True),
        )
        if copy_id is None:
            return -0.15, False
        logging.debug(f"Successfully copied spell {card.name} onto stack.")
        return 0.4, True # Success

    def _handle_counter_spell(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx')
        target_stack_idx = context.get('target_spell_idx') # Index on stack

        if hand_idx is None or target_stack_idx is None: logging.warning(f"Counter Spell context missing indices: {context}"); return -0.15, False
        try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
        except (ValueError, TypeError): logging.warning(f"Counter Spell context has non-integer indices: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Counter Spell hand index out of bounds: {hand_idx}"); return -0.1, False
        if target_stack_idx < 0 or target_stack_idx >= len(gs.stack) or gs.stack[target_stack_idx][0] != "SPELL":
             logging.warning(f"Counter Spell target stack index invalid or not a spell: {target_stack_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        # Add targeting info to cast context
        cast_context = {'target_stack_index': target_stack_idx, 'source_zone':'hand', 'hand_idx':hand_idx}
        cast_context.update(context) # Merge other context

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.6 if success else -0.1
        return reward, success # Success flag from cast_spell

    def _handle_prevent_damage(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx')

        if hand_idx is None: logging.warning(f"Prevent Damage context missing 'hand_idx'"); return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError): logging.warning(f"Prevent Damage context non-integer index: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Prevent Damage hand index OOB: {hand_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        cast_context = context.copy() # Use provided context for targets etc.
        cast_context['source_zone'] = 'hand'; cast_context['hand_idx'] = hand_idx

        success = gs.cast_spell(card_id, player, context=cast_context)
        # The effect registers on resolution, cast success is rewarded here
        reward = 0.2 if success else -0.1
        return reward, success

    def _handle_redirect_damage(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx')

        if hand_idx is None: logging.warning(f"Redirect Damage context missing 'hand_idx'"); return -0.15, False
        try: hand_idx = int(hand_idx)
        except (ValueError, TypeError): logging.warning(f"Redirect Damage context non-integer index: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Redirect Damage hand index OOB: {hand_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        cast_context = context.copy()
        cast_context['source_zone'] = 'hand'; cast_context['hand_idx'] = hand_idx

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.3 if success else -0.1
        return reward, success

    def _handle_stifle_trigger(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx')
        target_stack_idx = context.get('target_trigger_idx') # Use specific key

        if hand_idx is None or target_stack_idx is None: logging.warning(f"Stifle context missing indices: {context}"); return -0.15, False
        try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
        except (ValueError, TypeError): logging.warning(f"Stifle context non-integer indices: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Stifle hand index OOB: {hand_idx}"); return -0.1, False
        if target_stack_idx < 0 or target_stack_idx >= len(gs.stack) or gs.stack[target_stack_idx][0] != "TRIGGER":
             logging.warning(f"Stifle target index invalid/not trigger: {target_stack_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        cast_context = {'target_stack_index': target_stack_idx, 'source_zone':'hand', 'hand_idx':hand_idx}
        cast_context.update(context)

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.5 if success else -0.1
        return reward, success

    def _handle_counter_ability(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        hand_idx = context.get('hand_idx')
        target_stack_idx = context.get('target_ability_idx') # Use specific key

        if hand_idx is None or target_stack_idx is None: logging.warning(f"Counter Ability context missing indices: {context}"); return -0.15, False
        try: hand_idx, target_stack_idx = int(hand_idx), int(target_stack_idx)
        except (ValueError, TypeError): logging.warning(f"Counter Ability context non-integer indices: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Counter Ability hand index OOB: {hand_idx}"); return -0.1, False
        if target_stack_idx < 0 or target_stack_idx >= len(gs.stack) or gs.stack[target_stack_idx][0] not in ["ABILITY", "TRIGGER"]:
             logging.warning(f"Counter Ability target index invalid/not ability: {target_stack_idx}"); return -0.1, False

        card_id = player["hand"][hand_idx]
        cast_context = {'target_stack_index': target_stack_idx, 'source_zone':'hand', 'hand_idx':hand_idx}
        cast_context.update(context)

        success = gs.cast_spell(card_id, player, context=cast_context)
        reward = 0.5 if success else -0.1
        return reward, success

    def _handle_conspire(self, param, context, **kwargs):
        gs = self.game_state
        player = self._get_policy_player(context)
        spell_stack_idx = context.get('spell_stack_idx')
        c1_identifier = context.get('creature1_identifier')
        c2_identifier = context.get('creature2_identifier')

        if spell_stack_idx is None or c1_identifier is None or c2_identifier is None:
             logging.error(f"Conspire context missing required indices: {context}")
             return -0.15, False
        try: spell_stack_idx = int(spell_stack_idx)
        except (ValueError, TypeError): return -0.15, False

        success = False
        if hasattr(gs, 'conspire'):
            success = gs.conspire(player, spell_stack_idx, c1_identifier, c2_identifier)
        else: logging.error("Conspire method missing in GameState.")

        if not success: logging.debug("Conspire action failed validation or execution.")
        return (0.4 if success else -0.1), success

    def _handle_cast_with_flashback(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_WITH_FLASHBACK", **kwargs)

    def _handle_cast_with_jump_start(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_WITH_JUMP_START", **kwargs)

    def _handle_cast_with_escape(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_WITH_ESCAPE", **kwargs)

    def _handle_cast_for_madness(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_FOR_MADNESS", **kwargs)

    def _handle_cast_with_overload(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_WITH_OVERLOAD", **kwargs)

    def _handle_cast_for_emerge(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_FOR_EMERGE", **kwargs)

    def _handle_cast_for_delve(self, param, **kwargs): return self._handle_alternative_casting(param, "CAST_FOR_DELVE", **kwargs)

    def _handle_cast_left_half(self, param, **kwargs): return self._handle_cast_split(param, action_type="CAST_LEFT_HALF", **kwargs)

    def _handle_cast_right_half(self, param, **kwargs): return self._handle_cast_split(param, action_type="CAST_RIGHT_HALF", **kwargs)

    def _handle_cast_fuse(self, param, **kwargs): return self._handle_cast_split(param, action_type="CAST_FUSE", **kwargs)

    def _handle_aftermath_cast(self, param, **kwargs): return self._handle_alternative_casting(param, "AFTERMATH_CAST", **kwargs)

    def _handle_alternative_casting(self, param, action_type, context=None, **kwargs):
        """
        Improved handler for alternative casting methods with better organization.
        
        Args:
            param: Action parameter value
            action_type: The specific alternative casting action type
            context: Additional context for the casting
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if context is None: context = {}
        if kwargs.get('context'): context.update(kwargs['context'])
        
        # Common casting info for all alternative methods
        casting_info = {
            "CAST_WITH_FLASHBACK": {
                "source_zone": "graveyard",
                "index_key": "gy_idx",
                "cost_pattern": r"flashback ((?:\{[^\}]+\})+)",
                "timing_check": lambda card: True  # Flashback follows the timing of the card
            },
            "CAST_WITH_JUMP_START": {
                "source_zone": "graveyard",
                "index_key": "gy_idx",
                "cost_pattern": None,  # Uses original cost
                "requires_discard": True,
                "timing_check": lambda card: True  # Jump-start follows the timing of the card
            },
            "CAST_WITH_ESCAPE": {
                "source_zone": "graveyard",
                "index_key": "gy_idx",
                "cost_pattern": r"escape—([^\,]+)",
                "additional_pattern": r"exile ([^\.]+)",
                "timing_check": lambda card: True  # Escape follows the timing of the card
            },
            "CAST_FOR_MADNESS": {
                "source_zone": "exile",
                "index_key": "exile_idx",
                "cost_pattern": r"madness (\{[^\}]+\}|[0-9]+)",
                "timing_check": lambda card: True  # Madness follows the timing of the card
            },
            "CAST_WITH_OVERLOAD": {
                "source_zone": "hand",
                "index_key": "hand_idx",
                "cost_pattern": r"overload (\{[^\}]+\})",
                "timing_check": lambda card: True  # Overload follows the timing of the card
            },
            "CAST_FOR_EMERGE": {
                "source_zone": "hand",
                "index_key": "hand_idx",
                "cost_pattern": r"emerge (\{[^\}]+\})",
                "requires_sacrifice": True,
                "timing_check": lambda card: 'sorcery' in getattr(card, 'card_types', []) or 
                                        not ('instant' in getattr(card, 'card_types', []))
            },
            "CAST_FOR_DELVE": {
                "source_zone": "hand",
                "index_key": "hand_idx",
                "cost_pattern": None,  # Uses original cost with delve reduction
                "timing_check": lambda card: True  # Delve follows the timing of the card
            },
            "AFTERMATH_CAST": {
                "source_zone": "graveyard",
                "index_key": "gy_idx",
                "cast_right_half": True,
                "timing_check": lambda card: True  # Aftermath follows the timing of the card's right half
            }
        }
        
        # Retrieve casting configuration
        if action_type not in casting_info:
            logging.error(f"Unsupported alternative casting type: {action_type}")
            return -0.2, False
        
        cast_config = casting_info[action_type]
        source_zone = cast_config["source_zone"]
        index_key = cast_config["index_key"]
        
        # Get card ID based on context or param
        card_id = None
        
        # Special case for madness which uses card_id directly from context
        if action_type == "CAST_FOR_MADNESS" and "card_id" in context:
            card_id = context["card_id"]
        else:
            # Otherwise get index from context or param
            idx = context.get(index_key)
            if idx is None:
                # If no index in context, try to use param as index
                if isinstance(param, int):
                    idx = param
                else:
                    logging.error(f"{action_type} missing required {index_key} in context: {context}")
                    return -0.15, False
            
            # Validate index
            if idx >= len(player.get(source_zone, [])):
                logging.error(f"{action_type}: Invalid {index_key} {idx} (max: {len(player.get(source_zone, []))-1})")
                return -0.1, False
            
            card_id = player[source_zone][idx]
            # Store index in context for downstream handlers
            context['source_idx'] = idx
        
        # Get card and validate
        card = gs._safe_get_card(card_id)
        if not card:
            logging.error(f"{action_type}: Card {card_id} not found")
            return -0.15, False
        
        # Set up context for alternative casting
        context["source_zone"] = source_zone
        context["use_alt_cost"] = action_type.replace('CAST_WITH_', '').replace('CAST_FOR_', '').replace('CAST_', '').replace('_', ' ').lower()
        # Jump-start and delve use the printed mana cost; they add permission
        # and non-mana payment/cost reduction rather than replacing that cost.
        if action_type in {"CAST_WITH_JUMP_START", "CAST_FOR_DELVE"}:
            context.pop("use_alt_cost", None)
        
        # Handle special card-half logic
        if cast_config.get("cast_right_half"):
            context["cast_right_half"] = True
        
        # Handle additional costs and requirements
        if cast_config.get("requires_discard"):
            if "discard_idx" not in context:
                logging.error(f"{action_type} requires 'discard_idx' in context")
                return -0.1, False
            
            discard_idx = context["discard_idx"]
            if discard_idx >= len(player.get("hand", [])):
                logging.error(f"{action_type}: Invalid discard_idx {discard_idx}")
                return -0.1, False
            
            context["discard_additional"] = [discard_idx]
        
        if cast_config.get("requires_sacrifice"):
            if "sacrifice_idx" not in context:
                logging.error(f"{action_type} requires 'sacrifice_idx' in context")
                return -0.1, False
            
            sac_idx = context["sacrifice_idx"]
            if sac_idx >= len(player.get("battlefield", [])):
                logging.error(f"{action_type}: Invalid sacrifice_idx {sac_idx}")
                return -0.1, False
            
            sac_id = player["battlefield"][sac_idx]
            sac_card = gs._safe_get_card(sac_id)
            if not sac_card or 'creature' not in getattr(sac_card, 'card_types', []):
                logging.error(f"{action_type} sacrifice target must be a creature")
                return -0.1, False
            
            context["sacrificed_creature"] = sac_id
            context["sacrifice_additional"] = [sac_idx]
            context["sacrificed_cmc"] = getattr(sac_card, 'cmc', 0)
        
        # Handle escape exile requirements
        if action_type == "CAST_WITH_ESCAPE":
            if "gy_indices_escape" not in context or not isinstance(context["gy_indices_escape"], list):
                logging.error(f"{action_type} requires 'gy_indices_escape' list in context")
                return -0.1, False
            
            exile_req_str = None
            pattern = cast_config.get("additional_pattern")
            if pattern:
                match = re.search(pattern, getattr(card, 'oracle_text', '').lower())
                if match: exile_req_str = match.group(1).strip()
            
            required_exile_count = self._word_to_number(re.search(r"(\w+|\d+)", exile_req_str).group(1)) if exile_req_str else 0
            if required_exile_count <= 0: required_exile_count = 5  # Default if not specified
            
            actual_gy_indices = [idx for idx in context["gy_indices_escape"] if idx < len(player.get("graveyard", []))]
            if len(actual_gy_indices) < required_exile_count:
                logging.warning(f"{action_type}: Not enough valid graveyard cards to exile ({len(actual_gy_indices)}/{required_exile_count})")
                return -0.1, False
            
            context["escape_cards"] = actual_gy_indices[:required_exile_count]
        
        # Handle delve cost reduction
        if action_type == "CAST_FOR_DELVE":
            if "gy_indices" not in context or not isinstance(context["gy_indices"], list):
                logging.error(f"{action_type} requires 'gy_indices' list in context")
                return -0.1, False
            
            actual_gy_indices = [idx for idx in context["gy_indices"] if idx < len(player.get("graveyard", []))]
            context["delve_cards"] = actual_gy_indices
            context["delve_count"] = len(actual_gy_indices)
        
        # Cast the spell using game state
        success = gs.cast_spell(card_id, player, context=context)
        
        if success:
            # Clear madness state after successful cast
            if action_type == "CAST_FOR_MADNESS" and hasattr(gs, 'madness_cast_available') and gs.madness_cast_available and gs.madness_cast_available.get('card_id') == card_id:
                gs.madness_cast_available = None
                logging.debug(f"Madness state cleared after successful cast of {card.name}")
            
            # Calculate reward based on card value
            card_value = 0.3  # Base value for successful alternative cast
            if self.card_evaluator:
                eval_context = {"situation": f"cast_{context['use_alt_cost']}"}
                eval_context.update(context)
                card_value += self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context) * 0.2
            
            return card_value, True
        else:
            logging.warning(f"{action_type} failed for {getattr(card, 'name', card_id)}. Handled by gs.cast_spell.")
            return -0.1, False

    def _handle_cast_split(self, param, action_type, **kwargs):
        """Handler for casting split cards. (Updated Context)"""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        context = kwargs.get('context', {}) # Use context passed in
        hand_idx = context.get('hand_idx', param)

        if isinstance(hand_idx, int) and 0 <= hand_idx < len(player["hand"]):
            card_id = player["hand"][hand_idx]
            card = gs._safe_get_card(card_id)
            if not card: return -0.2, False

            # Update context based on action_type
            context["source_zone"] = "hand"
            context["hand_idx"] = hand_idx # Add hand_idx for clarity
            if action_type == "CAST_LEFT_HALF": context["cast_left_half"] = True
            elif action_type == "CAST_RIGHT_HALF": context["cast_right_half"] = True
            elif action_type == "CAST_FUSE": context["fuse"] = True

            # Use CardEvaluator to estimate value
            eval_context = {"situation": "casting", **context} # Merge context
            card_value = self.card_evaluator.evaluate_card(card_id, "play", context_details=eval_context) if self.card_evaluator else 0.0

            if gs.cast_spell(card_id, player, context=context):
                return 0.15 + card_value * 0.2, True # Base reward + value mod
            else:
                logging.warning(f"Cast split failed ({action_type}) for {card_id}. Handled by gs.cast_spell.")
                return -0.1, False
        return -0.2, False # Invalid hand index

    def _get_escalate_cost_str(self, card):
        """Helper to extract escalate cost string."""
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"escalate\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
        return None

    def _handle_play_card(self, player, hand_idx, is_land=False):
        """Handle playing a card from hand, considering land/spell rules."""
        gs = self.game_state
        try:
            card_id = player["hand"][hand_idx]
            card = gs.card_db[card_id]
            
            if is_land:
                if 'land' not in card.type_line:
                    logging.debug(f"Invalid action: {card.name} is not a land")
                    return 0  # Invalid action: not a land
                if player["land_played"]:
                    logging.debug(f"Invalid action: already played a land this turn")
                    return 0  # Already played a land this turn
                
                player["battlefield"].append(card_id)
                player["hand"].pop(hand_idx)
                player["land_played"] = True
                for idx, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                    player["mana_production"][color] += card.colors[idx]
                return 0.1  # Reduced reward for playing a land
                
            else:
                if 'land' in card.type_line:
                    logging.debug(f"Invalid action: can't cast {card.name} as a spell")
                    return 0  # Can't cast a land as a spell
                    
                # Check if can afford using mana_system if available
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                else:
                    # Simple check - at least some mana available  
                    can_afford = sum(player["mana_pool"].values()) > 0
                    
                if not can_afford:
                    logging.debug(f"Invalid action: can't afford {card.name}")
                    return 0  # Not enough mana
                
                if 'creature' in card.card_types:
                    # Mark creatures as having summoning sickness
                    if not hasattr(gs, 'summoning_sick'):
                        gs.summoning_sick = set()
                    gs.summoning_sick.add(card_id)
                
                # Add to stack instead of directly to battlefield
                gs.stack.append(("SPELL", card_id, player))
                player["hand"].pop(hand_idx)
                
                # Use mana_system to pay cost if available
                if hasattr(gs, 'mana_system'):
                    gs.mana_system.pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                else:
                    # Simple deduction - use all available mana
                    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                
                return 0.25  # Reduced reward for casting a spell
                
        except IndexError:
            logging.warning(f"Attempted to play a card at invalid hand index {hand_idx}.")
            return 

    def _tap_land_for_effect(self, player, land_id):
        """Tap a land to activate abilities (excluding mana production)."""
        gs = self.game_state
        
        # Get the land card
        card = gs._safe_get_card(land_id)
        if not card or 'land' not in card.type_line or land_id in player["tapped_permanents"]:
            return False
        
        # Mark the land as tapped
        player["tapped_permanents"].add(land_id)
        
        # Check for tap effects if ability handler exists
        if hasattr(gs, 'ability_handler'):
            gs.ability_handler.handle_tap_effects(land_id, player)
        
        # Trigger any "when this land becomes tapped" abilities
        if hasattr(gs, 'trigger_ability'):
            gs.trigger_ability(land_id, "TAPPED", {"controller": player})
        
        logging.debug(f"Tapped {card.name} for effect")
        return True

    def resolve_stack_item(self):
        """
        Resolve the top item on the stack if priority has been passed appropriately.
        
        Returns:
            bool: Whether an item was resolved
        """
        gs = self.game_state
        
        # Check if both players have passed priority
        if gs.priority_pass_count >= 2 and gs.stack:
            # Process any triggered abilities first
            if hasattr(gs, 'ability_handler') and gs.ability_handler:
                gs.ability_handler.process_triggered_abilities()
                
            # Resolve top of stack
            gs.resolve_top_of_stack()  # Changed from gs._resolve_top_of_stack()
            
            # Reset priority
            gs.priority_pass_count = 0
            gs.priority_player = gs._get_active_player()
            return True
            
        return False 

