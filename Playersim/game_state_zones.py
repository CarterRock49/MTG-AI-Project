"""Zone queries and card movement between zones.

Extracted from game_state.py. This module defines behavior only (a mixin);
all state lives on GameState itself, which composes every mixin.
"""

import random
import logging
import copy
import re


class GameStateZonesMixin:
    """Zone queries and card movement between zones."""

    # Empty slots: preserves GameState's __slots__ semantics (no instance __dict__).
    __slots__ = ()

    def _clear_exile_play_permissions(self, player, card_id):
        """Clear permissions attached to the current exile-zone object.

        A card that leaves exile is a new object if it later returns.  Keeping
        an old impulse expiry keyed only by card ID could therefore revoke a
        newer permission when the old duration expires.
        """
        if (player is not None
                and hasattr(self, "_consume_plot_permission")):
            self._consume_plot_permission(player, card_id)
        getattr(self, "cards_castable_from_exile", set()).discard(card_id)
        getattr(self, "exile_alternative_costs", {}).pop(card_id, None)
        getattr(self, "impulse_until_eot", set()).discard(card_id)
        getattr(self, "impulse_until_next_turn", {}).pop(card_id, None)

    def _snapshot_battlefield_object(self, card_id, controller):
        """Capture characteristics used by leave/dies triggers before the move."""
        card = self._safe_get_card(card_id)
        owner = self._find_card_owner_fallback(card_id) or controller
        owner_key = "p1" if owner is self.p1 else ("p2" if owner is self.p2 else None)
        card_types = list(getattr(card, "card_types", []) or []) if card else []
        subtypes = list(getattr(card, "subtypes", []) or []) if card else []
        supertypes = list(getattr(card, "supertypes", []) or []) if card else []
        power = getattr(card, "power", 0) if card else 0
        toughness = getattr(card, "toughness", 0) if card else 0
        layer_system = getattr(self, "layer_system", None)
        if card and layer_system:
            try:
                layered_types = layer_system.get_characteristic(
                    card_id, "card_types")
                layered_subtypes = layer_system.get_characteristic(
                    card_id, "subtypes")
                layered_supertypes = layer_system.get_characteristic(
                    card_id, "supertypes")
                if layered_types is not None:
                    card_types = list(layered_types)
                if layered_subtypes is not None:
                    subtypes = list(layered_subtypes)
                if layered_supertypes is not None:
                    supertypes = list(layered_supertypes)
                power = layer_system.get_characteristic(card_id, "power")
                toughness = layer_system.get_characteristic(
                    card_id, "toughness")
            except Exception:
                # Last-known information must remain available even while a
                # malformed layer is being diagnosed; raw characteristics are
                # the conservative fallback.
                pass
        attachment_source_ids = []
        for player in (self.p1, self.p2):
            if not player:
                continue
            attachment_source_ids.extend(
                attachment_id for attachment_id, target_id in
                (player.get("attachments", {}) or {}).items()
                if target_id == card_id)
        keyword_names = list(getattr(card, "ALL_KEYWORDS", []) or []) \
            if card else []
        raw_keyword_values = getattr(card, "keywords", None) if card else None
        keyword_values = (
            list(raw_keyword_values) if raw_keyword_values is not None else [])
        active_keywords = {
            str(keyword).casefold() for keyword, active in
            zip(keyword_names, keyword_values) if active}
        for counter_name, amount in (
                getattr(card, "counters", {}) or {}).items() if card else ():
            if (int(amount or 0) > 0
                    and str(counter_name).casefold() in {
                        str(value).casefold() for value in keyword_names}):
                active_keywords.add(str(counter_name).casefold())
        return {
            "card_id": card_id,
            "controller_key": "p1" if controller is self.p1 else "p2",
            "owner_key": owner_key,
            "card_types": card_types,
            "subtypes": subtypes,
            "supertypes": supertypes,
            "counters": copy.deepcopy(
                getattr(card, "counters", {}) or {}) if card else {},
            "attachment_source_ids": sorted(set(attachment_source_ids)),
            "power": power,
            "toughness": toughness,
            "mana_value": getattr(card, "cmc", 0) if card else 0,
            "keywords": sorted(active_keywords),
            "was_creature": "creature" in card_types,
            "was_token": bool(getattr(card, "is_token", False)) if card else False,
            "was_attacking": card_id in getattr(
                self, "current_attackers", []),
            "was_face_down": bool(
                getattr(card, "face_down", False)) if card else False,
            "lost_all_abilities": bool(
                getattr(self, "layer_system", None)
                and self.layer_system.source_has_lost_all_abilities(card_id)),
        }

    def _enters_battlefield_tapped(self, card, controller, card_id=None, context=None):
        """Return whether a permanent's own text makes it enter tapped.

        Scryfall's current wording uses both "enters tapped" and the older
        "enters the battlefield tapped" template. Conditional lands inspect
        the preexisting battlefield after the entering card has been appended.
        """
        if not card or not controller:
            return False
        context = context or {}
        oracle_text = getattr(card, "oracle_text", "") or ""
        if context.get("play_back_face") and getattr(card, "back_face", None):
            oracle_text = card.back_face.get("oracle_text", oracle_text) or ""
        normalized = oracle_text.lower().replace("the battlefield ", "")

        fast_land_clause = (
            "enters tapped unless you control two or fewer other lands")
        slow_land_clause = (
            "enters tapped unless you control two or more other lands")
        if fast_land_clause in normalized or slow_land_clause in normalized:
            land_count = 0
            for permanent_id in controller.get("battlefield", []):
                permanent = self._safe_get_card(permanent_id)
                if permanent and "land" in (getattr(permanent, "type_line", "") or "").lower():
                    land_count += 1
            # move_card calls this after appending the entering land. Subtract
            # one occurrence, rather than filtering by ID, because deck copies
            # intentionally share card IDs in the current zone model.
            other_lands = max(0, land_count - 1)
            if fast_land_clause in normalized:
                return other_lands > 2
            return other_lands < 2

        if re.search(
                r"enters tapped unless it(?:'|\u2019| i)s your first, second, "
                r"or third turn of the game", normalized):
            try:
                global_turn = int(self.turn)
            except (TypeError, ValueError):
                global_turn = 0
            player_turn = (
                (global_turn + 1) // 2
                if controller is self.p1 else global_turn // 2)
            return not (
                self._get_active_player() is controller
                and player_turn in (1, 2, 3))

        if re.search(
                r"enters tapped if it(?:'|\u2019| i)s not your turn",
                normalized):
            return self._get_active_player() is not controller

        if "enters tapped unless you control a basic land" in normalized:
            skipped_entering_occurrence = False
            for permanent_id in controller.get("battlefield", []):
                if (card_id is not None and permanent_id == card_id
                        and not skipped_entering_occurrence):
                    skipped_entering_occurrence = True
                    continue
                permanent = self._safe_get_card(permanent_id)
                if not permanent:
                    continue
                supertypes = {
                    str(supertype).lower()
                    for supertype in (
                        getattr(permanent, "supertypes", []) or [])
                }
                card_types = {
                    str(card_type).lower()
                    for card_type in (
                        getattr(permanent, "card_types", []) or [])
                }
                if "basic" in supertypes and "land" in card_types:
                    return False
            return True

        # These lands check for basic land *types*, not basic supertypes.
        # move_card asks after appending the entering object, so skip exactly
        # one occurrence of that object while inspecting existing permanents.
        land_type_condition = re.search(
            r"enters tapped unless you control (?:a|an)\s+([a-z]+)\s+or\s+"
            r"(?:a|an)\s+([a-z]+)", normalized)
        if land_type_condition:
            required_types = {
                land_type_condition.group(1).lower(),
                land_type_condition.group(2).lower(),
            }
            skipped_entering_occurrence = False
            for permanent_id in controller.get("battlefield", []):
                if (card_id is not None and permanent_id == card_id
                        and not skipped_entering_occurrence):
                    skipped_entering_occurrence = True
                    continue
                permanent = self._safe_get_card(permanent_id)
                if not permanent:
                    continue
                land_types = {
                    str(subtype).lower()
                    for subtype in (getattr(permanent, "subtypes", []) or [])
                }
                land_types.update(re.findall(
                    r"[a-z]+",
                    (getattr(permanent, "type_line", "") or "").lower()))
                if required_types.intersection(land_types):
                    return False
            return True

        # Shockland-style payment clauses and Multiversal Passage defer the
        # tapped decision until their entry choice has been made.  Treating
        # the words "enters tapped" as unconditional here tapped the land
        # before the player could pay life.
        if ("you may pay 2 life" in normalized
                and "if you don't, it enters tapped" in normalized):
            return False

        return "enters tapped" in normalized

    def _parse_own_as_enters(self, card):
        """Return a permanent's first-entry choice and counter modifiers.

        A permanent's own replacement ability functions before it is on the
        battlefield. The ordinary ability registrar necessarily runs after the
        move, so these self-replacements must be read from the entering object
        during the move transaction itself.
        """
        if not card:
            return None, []
        text = (getattr(card, "oracle_text", "") or "").lower()
        printed_name = (getattr(card, "name", "") or "").lower()
        short_name = printed_name.split(",", 1)[0].strip()
        named_subjects = [re.escape(printed_name)]
        if short_name and short_name != printed_name:
            named_subjects.append(re.escape(short_name))
        name_pattern = "|".join(named_subjects)
        subject = (
            rf"(?:this (?:permanent|creature|land|artifact)|{name_pattern})")
        prefix = rf"as {subject} enters(?: the battlefield)?"

        choice_kind = None
        choice_match = re.search(
            prefix + r",?\s*choose\s+"
            r"(a creature type|a card type|a basic land type|a color|an opponent)", text)
        if choice_match:
            choice_kind = {
                "a creature type": "creature_type",
                "a card type": "card_type",
                "a basic land type": "basic_land_type",
                "a color": "color",
                "an opponent": "opponent",
            }[choice_match.group(1)]
        elif re.search(
                prefix + r",?\s*you may pay 2 life\.\s*if you don(?:'|\u2019)t, "
                r"(?:this (?:permanent|land)|it) enters tapped", text):
            choice_kind = "pay_life"

        counters = []
        # "Enters with" is itself a replacement effect (CR 614.1c); it does
        # not use the word "as" in current Oracle templating.  Read the
        # entering object's named form too, including gendered self-pronouns
        # such as Leatherhead's "on her".
        counter_prefix = rf"(?:as\s+)?{subject} enters(?: the battlefield)?"
        counter_match = re.search(
            counter_prefix + r",?\s+with\s+"
            r"(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
            r"((?:[+-]\d+/[+-]\d+)|[a-z][a-z0-9_-]*)\s+counters?"
            r"(?:\s+on\s+(?:it|him|her))?(?=\s*(?:[.\n]|$))",
            text)
        if counter_match:
            count_token, counter_type = counter_match.groups()
            words = {
                "a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
                "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10,
            }
            count = words.get(count_token, int(count_token)
                              if count_token.isdigit() else 0)
            if count > 0:
                counters.append({"type": counter_type, "count": count})
        return choice_kind, counters

    def _discard_player_key(self, player):
        if player == self.p1:
            return "p1"
        if player == self.p2:
            return "p2"
        return None

    def _discard_player_from_key(self, player_key):
        if player_key == "p1":
            return self.p1
        if player_key == "p2":
            return self.p2
        return None

    def _discard_puts_onto_battlefield(self, card_id, player, source_id):
        """'If a spell or ability an opponent controls causes you to discard
        this card, put it onto the battlefield instead' (Obstinate Baloth)."""
        card = self._safe_get_card(card_id)
        text = (getattr(card, 'oracle_text', '') or '')
        if not re.search(
                r"if a spell or ability an opponent controls causes you to "
                r"discard this card, put it onto the battlefield",
                text, re.IGNORECASE):
            return False
        if source_id is None:
            return False
        # The causing spell/ability is usually still on the stack; fall back
        # to the source's current zone controller. An undeterminable
        # controller conservatively keeps the ordinary graveyard destination.
        for item in self.stack:
            if isinstance(item, tuple) and len(item) >= 3 and item[1] == source_id:
                return item[2] is not player
        source_controller, _ = self.find_card_location(source_id)
        return source_controller is not None and source_controller is not player

    def discard_card(self, player, card_id, source_id=None, cause="discard"):
        """Process one discard event, including replacements and Madness."""
        if not player or card_id not in player.get("hand", []):
            return False

        discard_context = {
            "card_id": card_id,
            "player": player,
            "cause": cause,
            "source_id": source_id,
            "to_player": player,
            "to_zone": "graveyard",
        }
        modified_context, replaced = self.apply_replacement_effect(
            "DISCARD", discard_context)
        if replaced and modified_context.get("prevented", False):
            return True

        destination_player = modified_context.get("to_player") or player
        destination_zone = modified_context.get("to_zone", "graveyard")
        # Obstinate Baloth style self-replacement: an opponent-caused discard
        # puts the card onto the battlefield instead of the graveyard.
        if (destination_zone == "graveyard"
                and self._discard_puts_onto_battlefield(card_id, player, source_id)):
            destination_zone = "battlefield"
        return self.move_card(
            card_id, player, "hand", destination_player, destination_zone,
            cause=cause, context={"source_id": source_id})

    def start_discard_choice(self, players, count=1, source_id=None,
                             is_random=False, cause="discard"):
        """Resolve random discards or queue affected players' card choices."""
        if self.choice_context:
            logging.warning("Cannot start discard choice while another choice is pending.")
            return False

        entries = []
        for player in players:
            if not player:
                continue
            hand = player.get("hand", [])
            discard_count = len(hand) if count == -1 else min(max(int(count), 0), len(hand))
            if discard_count <= 0:
                continue
            if is_random:
                for card_id in random.sample(list(hand), discard_count):
                    self.discard_card(player, card_id, source_id=source_id, cause=cause)
                continue
            player_key = self._discard_player_key(player)
            if player_key:
                entries.append({"player_key": player_key, "count": discard_count})

        if is_random or not entries:
            return True

        simultaneous = len(entries) > 1
        first = entries.pop(0)
        chooser = self._discard_player_from_key(first["player_key"])
        resume_phase = self.phase
        previous_priority_phase = self.previous_priority_phase
        if self.phase != self.PHASE_CHOOSE:
            self.previous_priority_phase = self.phase
        self.phase = self.PHASE_CHOOSE
        self.choice_context = {
            "type": "discard",
            "player": chooser,
            "remaining": first["count"],
            "pending": entries,
            "simultaneous": simultaneous,
            "current_player_key": first["player_key"],
            "selected_current": [],
            "staged_discards": [],
            "source_id": source_id,
            "cause": cause,
            "resume_phase": resume_phase,
            "previous_priority_phase_before_choice": previous_priority_phase,
            "choice_page": 0,
        }
        self.priority_player = chooser
        self.priority_pass_count = 0
        logging.info(
            f"Entering discard choice for {chooser.get('name', 'player')} "
            f"({first['count']} card(s)).")
        return True

    def _finish_or_advance_discard_choice(self):
        context = self.choice_context
        if not context or context.get("type") != "discard":
            return False

        pending = context.get("pending", [])
        while pending:
            entry = pending.pop(0)
            chooser = self._discard_player_from_key(entry.get("player_key"))
            remaining = min(entry.get("count", 0), len(chooser.get("hand", []))) \
                if chooser else 0
            if chooser and remaining > 0:
                context["player"] = chooser
                context["remaining"] = remaining
                context["pending"] = pending
                context["current_player_key"] = entry.get("player_key")
                context["selected_current"] = []
                context["choice_page"] = 0
                self.priority_player = chooser
                self.priority_pass_count = 0
                return True

        if context.get("simultaneous"):
            for selection in context.get("staged_discards", []):
                player = self._discard_player_from_key(
                    selection.get("player_key"))
                for card_id in selection.get("card_ids", []):
                    if not self.discard_card(
                            player, card_id,
                            source_id=context.get("source_id"),
                            cause=context.get("cause", "discard")):
                        return False

        return_phase = self.previous_priority_phase
        if context.get("effect_continuation") is not None:
            # The remaining instructions of the resolving object are bound to
            # the completed discard choice.  Resume them only after every
            # affected player has made every required discard.
            self.previous_priority_phase = context.get(
                "previous_priority_phase_before_choice")
            return self._resume_effect_continuation(context)
        self.choice_context = None
        self.previous_priority_phase = None
        self.phase = return_phase if return_phase is not None else self.PHASE_PRIORITY
        if self.phase in (self.PHASE_UNTAP, self.PHASE_CLEANUP):
            self.priority_player = None
        else:
            self.priority_player = self._get_active_player()
        self.priority_pass_count = 0
        return True

    def choose_discard_card(self, hand_index):
        """Apply one hand-index selection from an active discard choice."""
        context = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and context
                and context.get("type") == "discard"):
            return False
        chooser = context.get("player")
        hand = chooser.get("hand", []) if chooser else []
        if not isinstance(hand_index, int) or not 0 <= hand_index < len(hand):
            return False

        card_id = hand[hand_index]
        if context.get("simultaneous"):
            selected = context.setdefault("selected_current", [])
            if card_id in selected:
                return False
            selected.append(card_id)
        elif not self.discard_card(
                    chooser, card_id, source_id=context.get("source_id"),
                    cause=context.get("cause", "discard")):
                return False

        context["remaining"] = max(0, int(context.get("remaining", 1)) - 1)
        card = self._safe_get_card(card_id)
        if (context.get("stop_after_creature")
                and card
                and "creature" in getattr(card, "card_types", [])):
            context["remaining"] = 0
        available_hand = [
            cid for cid in chooser.get("hand", [])
            if cid not in context.get("selected_current", [])]
        if context["remaining"] > 0 and available_hand:
            self.priority_player = chooser
            self.priority_pass_count = 0
            return True
        if context.get("simultaneous"):
            context.setdefault("staged_discards", []).append({
                "player_key": context.get("current_player_key"),
                "card_ids": list(context.get("selected_current", [])),
            })
        return self._finish_or_advance_discard_choice()

    def exile_until_source_leaves(self, source_id, source_controller, card_id,
                                  card_owner, from_zone="battlefield",
                                  return_zone="battlefield"):
        """Perform and record one linked temporary-exile one-shot effect."""
        _, source_zone = self.find_card_location(source_id)
        if source_zone != "battlefield":
            return False
        if not card_owner or card_id not in card_owner.get(from_zone, []):
            return False
        if not self.move_card(
                card_id, card_owner, from_zone, card_owner, "exile",
                cause="linked_exile", context={"source_id": source_id}):
            return False

        # Tokens cease to exist after moving, and exile replacements may send
        # the card elsewhere. Only an object actually in exile is linked.
        if card_id not in card_owner.get("exile", []):
            return True
        owner_key = self._discard_player_key(card_owner)
        if not owner_key:
            return False
        source_controller.setdefault("linked_exile", {}).setdefault(
            source_id, []).append({
                "card_id": card_id,
                "owner_key": owner_key,
                "return_zone": return_zone,
            })
        return True

    def _return_linked_exile_cards(self, source_id):
        """End every temporary-exile duration linked to a leaving source."""
        entries = []
        for player in (self.p1, self.p2):
            if player:
                entries.extend(player.setdefault("linked_exile", {}).pop(source_id, []))
        returned = 0
        for entry in entries:
            owner = self._discard_player_from_key(entry.get("owner_key"))
            card_id = entry.get("card_id")
            return_zone = entry.get("return_zone", "battlefield")
            if not owner or card_id not in owner.get("exile", []):
                continue
            if self.move_card(
                    card_id, owner, "exile", owner, return_zone,
                    cause="linked_exile_return", context={"source_id": source_id}):
                returned += 1
        return returned

    def begin_linked_exile_choice(self, source_id, controller, target_player,
                                  options, return_zone="hand", optional=True):
        """Expose the card choice inside a resolving linked-exile effect."""
        if self.choice_context:
            logging.warning("Cannot start linked-exile choice while another choice is pending.")
            return False
        _, source_zone = self.find_card_location(source_id)
        target_key = self._discard_player_key(target_player)
        options = list(dict.fromkeys(options))
        if source_zone != "battlefield" or not target_key or not options:
            return source_zone != "battlefield" or not options
        if self.phase != self.PHASE_CHOOSE and self.previous_priority_phase is None:
            self.previous_priority_phase = self.phase
        self.phase = self.PHASE_CHOOSE
        self.choice_context = {
            "type": "linked_exile",
            "player": controller,
            "source_id": source_id,
            "target_player_key": target_key,
            "options": options,
            "return_zone": return_zone,
            "optional": bool(optional),
        }
        self.priority_player = controller
        self.priority_pass_count = 0
        return True

    def _finish_linked_exile_choice(self):
        self.choice_context = None
        if self.stack:
            self.phase = self.PHASE_PRIORITY
        elif self.previous_priority_phase is not None:
            self.phase = self.previous_priority_phase
            self.previous_priority_phase = None
        else:
            self.phase = self.PHASE_PRIORITY
        self.priority_player = self._get_active_player()
        self.priority_pass_count = 0
        return True

    def choose_linked_exile_card(self, option_index):
        context = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and context
                and context.get("type") == "linked_exile"):
            return False
        options = context.get("options", [])
        if not isinstance(option_index, int) or not 0 <= option_index < len(options):
            return False
        target_player = self._discard_player_from_key(context.get("target_player_key"))
        source_id = context.get("source_id")
        controller = context.get("player")
        card_id = options[option_index]
        success = self.exile_until_source_leaves(
            source_id, controller, card_id, target_player,
            from_zone="hand", return_zone=context.get("return_zone", "hand"))
        if not success:
            return False
        return self._finish_linked_exile_choice()

    def decline_linked_exile_choice(self):
        context = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and context
                and context.get("type") == "linked_exile"
                and context.get("optional", False)):
            return False
        return self._finish_linked_exile_choice()

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
        elif from_zone == "graveyard_implicit": actual_from_zone = "graveyard"; source_list = []
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
        # Finality counters create a built-in replacement: a creature that
        # would die is exiled instead. This is checked before ordinary
        # registered replacements because the counter carries the rule.
        if (actual_from_zone == "battlefield" and to_zone == "graveyard"
                and card and "creature" in getattr(card, "card_types", [])
                and int(getattr(card, 'counters', {}).get('finality', 0) or 0) > 0):
            final_destination_zone = 'exile'
        event_context = {'card_id': card_id, 'card': card, 'from_player': from_player, 'from_zone': actual_from_zone, 'to_player': to_player, 'to_zone': to_zone, 'cause': cause, **context }
        event_context['to_zone'] = final_destination_zone
        prevented = False
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            # "Dies" means a creature would move from the battlefield to a
            # graveyard. Apply that replacement event before the component
            # leave/enter events so "exile it instead" can change the move.
            if (actual_from_zone == "battlefield"
                    and final_destination_zone == "graveyard"
                    and card
                    and "creature" in getattr(card, "card_types", [])):
                modified_die_ctx, replaced_die = self.replacement_effects.apply_replacements(
                    "DIES", event_context.copy())
                if replaced_die:
                    event_context.update(modified_die_ctx)
                    final_destination_player = event_context.get(
                        "to_player", final_destination_player)
                    final_destination_zone = event_context.get(
                        "to_zone", final_destination_zone)
                    prevented = event_context.get("prevented", False)
            # Check LEAVE zone replacements first
            if not prevented:
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
                          event_context.update(modified_enter_ctx)
                          final_destination_player = modified_enter_ctx.get('to_player'); final_destination_zone = modified_enter_ctx.get('to_zone'); prevented = modified_enter_ctx.get('prevented', False)
                          # Carry over ETB modifiers like 'tapped' or 'counters'
                          if 'enters_tapped' in modified_enter_ctx: event_context['enters_tapped'] = modified_enter_ctx['enters_tapped']
                          if 'enter_counters' in modified_enter_ctx:
                              # update() above may have installed the SAME list
                              # object; extending it with itself doubled every
                              # ETB counter entry.
                              existing_counters = event_context.setdefault('enter_counters', [])
                              if existing_counters is not modified_enter_ctx['enter_counters']:
                                  existing_counters.extend(modified_enter_ctx['enter_counters'])
                          if 'as_enters_choice_needed' in modified_enter_ctx: event_context['as_enters_choice_needed'] = modified_enter_ctx['as_enters_choice_needed']
                          logging.debug(f"Enter replacement applied for {card_name}: Final Dest: {final_destination_zone}, Prevented: {prevented}")

        if prevented:
            logging.debug(f"Movement of {card_name} from {actual_from_zone} to {final_destination_zone} prevented.")
            return False # Movement stopped

        # A merged permanent is one object but still contains separately owned
        # physical cards. Once it leaves the battlefield, each component must
        # enter its owner's private zone (CR 721.3). Route the representative
        # component correctly before emitting leave/enter events; the remaining
        # components are separated after the representative move completes.
        pending_mutation = (
            getattr(self, "mutated_permanents", {}).get(card_id)
            if actual_from_zone == "battlefield"
            and final_destination_zone != "battlefield"
            else None)
        if (pending_mutation
                and final_destination_zone in {
                    "hand", "library", "graveyard", "exile"}):
            owner_key = pending_mutation.get(
                "component_owner_keys", {}).get(card_id)
            owner = self.p1 if owner_key == "p1" else self.p2 if owner_key == "p2" else None
            if owner is not None:
                final_destination_player = owner
                event_context["to_player"] = owner
                event_context["to_zone"] = final_destination_zone

        earthbend_return = None
        if actual_from_zone == "battlefield" \
                and final_destination_zone != "battlefield":
            earthbend_info = getattr(self, "earthbent_lands", {}).pop(
                card_id, None)
            if (earthbend_info
                    and final_destination_zone in {"graveyard", "exile"}
                    and not getattr(card, "is_token", False)):
                earthbend_return = earthbend_info

        # Flashback's exile destination applies whether the spell resolves,
        # fizzles, or is countered.
        if (actual_from_zone == "stack"
                and final_destination_zone == "graveyard"
                and card_id in getattr(self, "flashback_cards", set())):
            final_destination_zone = "exile"
            event_context["to_zone"] = "exile"
            self.flashback_cards.discard(card_id)

        if (final_destination_zone == "battlefield" and actual_from_zone != "battlefield"
                and card and hasattr(self, "prepare_day_night_entry")):
            self.prepare_day_night_entry(card_id)

        last_known = None
        if actual_from_zone == "battlefield" and from_player:
            last_known = self._snapshot_battlefield_object(card_id, from_player)
            event_context["last_known"] = last_known
            # A source-referential effect on an already-triggered object uses
            # that source's last known battlefield characteristics if the
            # source leaves before resolution (Ouroboroid's current power).
            # Keep this separate from ``last_known``, which may already refer
            # to the event object that caused a watcher to trigger.
            for stack_index, item in enumerate(list(self.stack)):
                if not (isinstance(item, tuple) and len(item) >= 4
                        and item[1] == card_id
                        and isinstance(item[3], dict)):
                    continue
                stack_context = dict(item[3])
                stack_context["source_last_known"] = copy.deepcopy(last_known)
                self.stack[stack_index] = item[:3] + (stack_context,)
            ability_handler = getattr(self, "ability_handler", None)
            for entry in list(getattr(
                    ability_handler, "active_triggers", []) or []):
                if not (isinstance(entry, tuple) and len(entry) >= 3
                        and getattr(entry[0], "card_id", None) == card_id
                        and isinstance(entry[2], dict)):
                    continue
                entry[2]["source_last_known"] = copy.deepcopy(last_known)

        # --- Perform Actual Move ---
        # ... (Keep existing removal logic) ...
        removed_successfully = False
        if source_list is not None and original_from_zone not in ["stack_implicit", "library_implicit", "hand_implicit", "graveyard_implicit", "nonexistent_zone"]:
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

        if (actual_from_zone == "graveyard"
                and final_destination_zone != "graveyard" and from_player):
            # Battlefield watchers such as Dredger's Insight need the event
            # while the moved card's old zone/controller/type are still
            # explicit.  Emit only after a real removal and after replacements
            # have selected the final destination.
            self.trigger_ability(card_id, "LEAVE_GRAVEYARD", {
                "controller": from_player,
                "from_player": from_player,
                "from_zone": actual_from_zone,
                "to_player": final_destination_player,
                "to_zone": final_destination_zone,
                "cause": cause,
                "card": card,
                **context,
            })

        if actual_from_zone == "exile" and final_destination_zone != "exile":
            self._clear_exile_play_permissions(from_player, card_id)
            # Face-down identity is a property of this exile-zone object, not
            # of the physical card in its next zone.
            if hasattr(self, "clear_face_down_exile"):
                self.clear_face_down_exile(from_player, card_id)
            else:
                getattr(self, "face_down_exile_cards", set()).discard(card_id)


        meld_partner_id = None
        mutation_info = None

        # --- 2. LTB Cleanup/Triggers (Only if removed from battlefield) ---
        # ... (Keep existing LTB logic) ...
        if actual_from_zone == "battlefield" and from_player:
            self.prepared_cards.discard(card_id)
            self.defender_attack_permissions.pop(card_id, None)
            for player in (self.p1, self.p2):
                if player:
                    player.get("targeted_permanents_this_turn", set()).discard(card_id)
            ltb_trigger_context = { 'controller': from_player, 'from_zone': actual_from_zone, 'to_zone': final_destination_zone, 'cause': cause, **context }
            if last_known is not None:
                ltb_trigger_context["last_known"] = last_known
            self.trigger_ability(card_id, "LEAVE_BATTLEFIELD", ltb_trigger_context)
            self._return_linked_exile_cards(card_id)
            logging.debug(f"Cleaning up state for {card_name} ({card_id}) leaving battlefield.")
            if final_destination_zone != "battlefield":
                mutation_info = getattr(self, "mutated_permanents", {}).pop(card_id, None)
            # Remove tracked statuses
            from_player.get("tapped_permanents", set()).discard(card_id)
            from_player.get("entered_battlefield_this_turn", set()).discard(card_id)
            from_player.get("suspected_permanents", set()).discard(card_id)
            # "As enters" choices belong to that battlefield object. They do
            # not follow the card through a zone change; a later entry makes a
            # new choice. Control changes use _transfer_permanent_control and
            # deliberately migrate these stores instead of clearing them.
            for choice_store in (
                    "chosen_creature_types", "chosen_colors",
                    "chosen_card_types", "chosen_basic_land_types",
                    "chosen_opponents", "as_enters_choices"):
                store = from_player.get(choice_store)
                if isinstance(store, dict):
                    store.pop(card_id, None)
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
            from_player.get('saga_counters', {}).pop(card_id, None)
            # Clear counters stored on game state dicts
            if hasattr(self, 'saga_counters'): self.saga_counters.pop(card_id, None)
            if hasattr(self, 'battle_cards'): self.battle_cards.pop(card_id, None)
            # Clear other statuses
            if hasattr(from_player, 'regeneration_shields'): from_player['regeneration_shields'].discard(card_id)
            from_player.get('mutation_stacks', {}).pop(card_id, None)
            # Unregister effects originating from this card
            if self.layer_system:
                self.layer_system.remove_effects_by_source(
                    card_id,
                    preserve_durations={"end_of_turn", "until_your_next_turn"})
            if self.replacement_effects: self.replacement_effects.remove_effects_by_source(card_id)
            if self.ability_handler: self.ability_handler.unregister_card_abilities(card_id)
            if mutation_info and card:
                component_printed = mutation_info.get("component_printed", {})
                if card_id in component_printed:
                    card._printed = copy.deepcopy(component_printed[card_id])
                    card.reset_to_printed()

            copy_info = getattr(self, "copy_overrides", {}).pop(card_id, None)
            if copy_info and card and copy_info.get("original_printed"):
                card._printed = copy.deepcopy(copy_info["original_printed"])
                card.reset_to_printed()

            if final_destination_zone != "battlefield":
                manifest_info = getattr(self, "manifested_cards", {}).pop(
                    card_id, None)
                if (manifest_info and card
                        and manifest_info.get("original_printed")):
                    card._printed = copy.deepcopy(
                        manifest_info["original_printed"])
                    card.reset_to_printed()
                    card.face_down = False

            # Reset card state itself (e.g., face-down)
            if card and hasattr(card, 'reset_state_on_zone_change'):
                card.reset_state_on_zone_change()
                if hasattr(card, "reset_to_printed"):
                    card.reset_to_printed()

            # A melded permanent is represented by one battlefield ID plus its
            # partner waiting in exile. It has result characteristics for LTB,
            # then both physical cards regain their front identities in the
            # destination zone.
            if (final_destination_zone != "battlefield"
                    and card_id in getattr(self, "melded_permanents", {})):
                meld_info = self.melded_permanents.pop(card_id)
                meld_partner_id = meld_info.get("partner_id")
                # Immediate one-shot zone-change effects (blink/flicker) need
                # to find both physical cards represented by the former melded
                # object. Return the separated partner identity to the caller
                # through its transaction context without retaining stale game
                # state after an ordinary leave event.
                if isinstance(context, dict) and meld_partner_id is not None:
                    context["_separated_meld_partner_id"] = meld_partner_id
                original_printed = meld_info.get("original_printed")
                if card and original_printed:
                    card._printed = original_printed
                    card.reset_to_printed()


        # --- 3. Add to destination zone ---
        # ... (Keep existing destination logic) ...
        destination_list = final_destination_player.get(final_destination_zone)
        if destination_list is None: logging.error(f"Invalid destination zone '{final_destination_zone}'."); return False
        # Lists model real zones that can contain multiple copies represented by
        # the same card ID; sets/dicts model status trackers and remain unique.
        if isinstance(destination_list, list):
             destination_list.append(card_id)
        elif isinstance(destination_list, set):
             destination_list.add(card_id)
        elif isinstance(destination_list, dict):
             destination_list[card_id] = True # Example for dict zone
        else:
             logging.error(f"Dest zone '{final_destination_zone}' not list/set/dict."); return False
        if (final_destination_zone == "exile"
                and event_context.get("face_down_exile", False)):
            if hasattr(self, "mark_face_down_exile"):
                self.mark_face_down_exile(
                    final_destination_player, card_id)
            else:
                self.face_down_exile_cards.add(card_id)
        if hasattr(self, "_last_card_locations"):
             self._last_card_locations[card_id] = (final_destination_player, final_destination_zone)
        if card:
             card._zone_change_generation = int(getattr(
                 card, "_zone_change_generation", 0) or 0) + 1
        logging.debug(f"Moved {card_name} from {from_player['name'] if from_player else actual_from_zone} to {final_destination_player['name']}'s {final_destination_zone}")

        # --- 4. Trigger ENTER Abilities & Handle ETB Effects ---
        # --- UPDATED BLOCK ---
        enter_trigger_context = {'controller': final_destination_player, 'from_zone': actual_from_zone, 'to_zone': final_destination_zone, 'cause': cause, **event_context } # Pass merged context

        if final_destination_zone == "battlefield":
            # Casting either half of a Room unlocks exactly that door as the
            # permanent enters. Keep the combined Room identity/current_face;
            # only the independent door-state dictionaries change.
            room_cast_door = event_context.get("room_cast_door_number")
            if (card and getattr(card, "is_room", False)
                    and cause == "spell_resolution"
                    and event_context.get("was_cast")
                    and room_cast_door in (1, 2)):
                for door_number in (1, 2):
                    door = getattr(card, f"door{door_number}", None)
                    if door:
                        door["unlocked"] = door_number == room_cast_door
                event_context["room_cast_door_unlocked_on_entry"] = True
                enter_trigger_context[
                    "room_cast_door_unlocked_on_entry"] = True

            if (card and getattr(card, "layout", "") == "prepare"
                    and re.search(r"\benters prepared\b",
                                  getattr(card, "oracle_text", ""),
                                  re.IGNORECASE)):
                self.prepared_cards.add(card_id)

            if event_context.get("return_as_enchantment") and self.layer_system:
                self.layer_system.register_effect({
                    "source_id": card_id,
                    "layer": 4,
                    "affected_ids": [card_id],
                    "effect_type": "set_type",
                    "effect_value": ["enchantment"],
                    "duration": "permanent",
                })
                self.layer_system.register_effect({
                    "source_id": card_id,
                    "layer": 4,
                    "affected_ids": [card_id],
                    "effect_type": "set_subtype",
                    "effect_value": [],
                    "duration": "permanent",
                })
                self.layer_system.apply_all_effects()

            # A permanent's own "as enters" replacement applies on its first
            # entry, before register_card_abilities can see the battlefield
            # object. Merge it with any externally registered replacements.
            own_choice, own_counters = self._parse_own_as_enters(card)
            if own_choice and not event_context.get("as_enters_choice_needed"):
                event_context["as_enters_choice_needed"] = own_choice
                event_context["as_enters_source_id"] = card_id
            if own_counters:
                event_context.setdefault("enter_counters", []).extend(own_counters)

            # --- Standard ETB Setup ---
            final_destination_player.setdefault("entered_battlefield_this_turn", set()).add(card_id)
            etb_tapped_from_text = self._enters_battlefield_tapped(
                card, final_destination_player, card_id, event_context
            )
            enters_tapped = event_context.get('enters_tapped', False) or etb_tapped_from_text
            if enters_tapped: final_destination_player.setdefault("tapped_permanents", set()).add(card_id)

            # The object is now on the battlefield, so its static and triggered
            # abilities must exist before any enter counters invoke an immediate
            # layer/SBA pass.  In particular, a creature with symbolic ``*``
            # power needs its CDA registered before +1/+1 counters are applied.
            if card and self.ability_handler:
                self.ability_handler.register_card_abilities(
                    card_id, final_destination_player)

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
                     # Layer 4: Remove Creature Type (card_types are stored
                     # lowercase; 'Creature' never matched anything).
                     self._register_impending_static_effect(card_id, final_destination_player, layer=4, effect_type='remove_type', effect_value=['creature'])
                     # Layer 7b: Set P/T to 0/0 (Implicit by rule 208.3 for non-creatures, but can enforce)
                     self._register_impending_static_effect(card_id, final_destination_player, layer=7, sublayer='b', effect_type='set_pt', effect_value=(0, 0))
                     # Re-apply layers immediately after registering these effects
                     self.layer_system.apply_all_effects()
            # --- End Impending ETB ---

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
                     'resolved': False,
                     # Casting/move contexts may contain live Card objects
                     # linked to this GameState and its thread locks. Keep only
                     # declarative event data so MCTS can clone between chained
                     # entry choices without dropping the transaction.
                     'enter_context': self._copy_stack_context(
                         enter_trigger_context),
                 }
                 self.choice_context['options'] = self._as_enters_choice_options(
                     event_context['as_enters_choice_needed'],
                     final_destination_player)
                 self.priority_player = final_destination_player
                 self.priority_pass_count = 0
                 logging.info(f"'As enters' choice required for {card_name}. Waiting.")
            else:
                # --- Trigger ETB Abilities (Only if no choice needed immediately) ---
                self._finish_battlefield_entry_triggers(
                    card_id, final_destination_player, enter_trigger_context)

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
                 if (not hasattr(self, 'cards_to_graveyard_this_turn')
                         or self.cards_to_graveyard_this_turn is None):
                     self.cards_to_graveyard_this_turn = {}
                 self.cards_to_graveyard_this_turn.setdefault(
                     self.turn, []).append(card_id)
                 if actual_from_zone == "battlefield":
                     # Trigger "dies" ability
                     dies_context = {'controller': from_player, 'from_zone': actual_from_zone, 'to_zone': final_destination_zone, 'cause': cause, **context}
                     if last_known is not None:
                         dies_context["last_known"] = last_known
                     self.trigger_ability(card_id, "DIES", dies_context)
                     self.gravestorm_count = getattr(self, 'gravestorm_count', 0) + 1
                     # Per-player died-this-turn tracking ("for each creature
                     # that died under your control this turn").
                     if card and "creature" in getattr(card, "card_types", []):
                         died_key = self._discard_player_key(from_player)
                         if died_key:
                             if not hasattr(self, 'creatures_died_this_turn') or self.creatures_died_this_turn is None:
                                 self.creatures_died_this_turn = {}
                             self.creatures_died_this_turn[died_key] = \
                                 self.creatures_died_this_turn.get(died_key, 0) + 1
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
                  self._ceased_token_cards[card_id] = card
                  del self.card_db[card_id]
                  logging.debug(f"Token {card_name} ({card_id}) ceased to exist after moving to {final_destination_zone}.")
             # Remove from player's token tracking if present
             if "tokens" in final_destination_player and card_id in final_destination_player["tokens"]:
                  final_destination_player["tokens"].remove(card_id)
             if self.ability_handler:
                  self.ability_handler.registered_abilities.pop(card_id, None)

        # Clear Madness opportunity if card moved FROM exile via non-Madness means
        if actual_from_zone == "exile" and not context.get("is_madness_cast", False) and \
           getattr(self, 'madness_cast_available', None) and self.madness_cast_available.get('card_id') == card_id:
             logging.debug(f"Clearing Madness opportunity for {card_name} as it moved from exile by other means.")
             self.madness_cast_available = None

        if (meld_partner_id is not None and final_destination_zone != "exile"
                and meld_partner_id in from_player.get("exile", [])):
            self.move_card(
                meld_partner_id, from_player, "exile",
                final_destination_player, final_destination_zone,
                cause="meld_separated", context={"meld_primary_id": card_id})

        if mutation_info:
            for component_id in mutation_info.get("components", []):
                if component_id == card_id:
                    continue
                owner_key = mutation_info.get(
                    "component_owner_keys", {}).get(component_id)
                component_destination = (
                    self.p1 if owner_key == "p1"
                    else self.p2 if owner_key == "p2"
                    else final_destination_player)
                self.move_card(
                    component_id, from_player, "stack_implicit",
                    component_destination, final_destination_zone,
                    cause="mutate_separated", context={"mutate_primary_id": card_id})

        # Resolve Earthbend's deterministic delayed return after the original
        # dies/exile event has completed, including its zone triggers.
        if (earthbend_return and card_id in final_destination_player.get(
                final_destination_zone, [])):
            return_controller = (
                self.p1 if earthbend_return.get("controller") == "p1"
                else self.p2)
            self.move_card(
                card_id, final_destination_player, final_destination_zone,
                return_controller, "battlefield", cause="earthbend_return",
                context={"enters_tapped": True,
                         "source_id": earthbend_return.get("source_id")})

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

    def handle_miracle_draw(self, card_id, player, is_first_draw=None):
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
        
        player_key = "p1" if player == self.p1 else "p2"
        if is_first_draw is None:
            # Canonical draws increment the count before miracle is checked.
            is_first_draw = self.cards_drawn_this_turn.get(player_key, 0) <= 1
        
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

        if isinstance(criteria, str) and " or " in criteria:
            return any(
                self._card_matches_criteria(card, part.strip())
                for part in criteria.split(" or "))
        if criteria == "any": return True
        if criteria == "basic land" and 'basic' in type_line and 'land' in type_line: return True
        if criteria == "land" and 'land' in type_line: return True
        if criteria in types: return True
        if criteria in subtypes: return True
        if criteria == name: return True
        # Add checks for colors, CMC, P/T if needed for more complex searches
        return False

    def search_library_and_choose(self, player, criteria,
                                  ai_choice_context=None, exclude_ids=None,
                                  shuffle=True):
        """Search library for a card matching criteria and let AI choose one.

        exclude_ids: ids already taken by this same search (multi-card
        fetches). SearchLibraryEffect always passed this parameter, but the
        signature lacked it -- the TypeError was swallowed and EVERY library
        search silently found nothing (first-touch sweep, July 2026; the
        fixture ramp spell has been a no-op in every random episode).
        """
        exclude = set(exclude_ids or [])
        matches = []
        indices_to_remove = []
        for i, card_id in enumerate(player["library"]):
            if card_id in exclude:
                continue
            card = self._safe_get_card(card_id)
            if self._card_matches_criteria(card, criteria): # Uses GameState's helper now
                 matches.append(card_id)
                 indices_to_remove.append(i) # Store index along with card_id

        if not matches:
            logging.debug(f"Search failed: No '{criteria}' found in library.")
            if shuffle and hasattr(self, 'shuffle_library'):
                self.shuffle_library(player)  # Shuffle even on a legal miss.
            return None

        # AI Choice - Use CardEvaluator if available, else first match
        chosen_id = None
        if hasattr(self, 'card_evaluator') and self.card_evaluator:
             best_choice_id = None
             best_score = -float('inf')
             # Add turn and phase to context
             eval_context = {
                 "current_turn": self.turn,
                 "current_phase": self.phase,
                 "goal": criteria,
                 "perspective": "p1" if player is self.p1 else "p2",
             }
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
        if shuffle:
            if hasattr(self, 'shuffle_library'):
                self.shuffle_library(player)
            else:
                random.shuffle(player["library"])

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

    def explore(self, player, creature_id, source_id=None):
        """Perform explore through its reveal and, when needed, open a choice."""
        if not player or "library" not in player:
            return False
        if not player["library"]:
            # CR 701.40a: no card is revealed, so the result is nonland and the
            # exploring permanent still gets a +1/+1 counter if it is present.
            self.add_counter(creature_id, "+1/+1", 1)
            self.trigger_ability(
                creature_id, "EXPLORED_NONLAND_EMPTY",
                {"controller": player, "source_id": source_id})
            self.trigger_ability(
                creature_id, "EXPLORED",
                {"controller": player, "source_id": source_id,
                 "revealed_card_id": None})
            logging.debug("Explore: Library empty; no card revealed.")
            return True

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
            # Put +1/+1 counter on the exploring permanent if it is still on
            # the battlefield, then let its controller choose top or graveyard.
            success_counter = self.add_counter(creature_id, "+1/+1", 1)
            if success_counter: logging.debug(f"Explore hit nonland, put +1/+1 counter on {exploring_creature_name}")
            if (self.previous_priority_phase is None
                    and self.phase not in [self.PHASE_TARGETING,
                                           self.PHASE_SACRIFICE,
                                           self.PHASE_CHOOSE]):
                self.previous_priority_phase = self.phase
            self.phase = self.PHASE_CHOOSE
            self.choice_context = {
                "type": "explore",
                "player": player,
                "controller": player,
                "source_id": source_id or creature_id,
                "exploring_creature_id": creature_id,
                "cards": [top_card_id],
            }
            self.priority_player = player
            self.priority_pass_count = 0
            logging.debug(f"Explore: Waiting for top/graveyard choice for {card_name}.")
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
        # Fixture decks store multiple copies as repeated card IDs, so the same
        # ID can legitimately appear in several zones. If move_card has moved
        # this ID during the current game, use that current-location hint first.
        last = getattr(self, "_last_card_locations", {}).get(card_id)
        if last:
            player, zone = last
            if player and zone in player and isinstance(player[zone], (list, set)) and card_id in player[zone]:
                return player, zone

        # Prefer the highest-impact zone across both players before falling
        # through, otherwise a P1 hand copy can hide the P2 battlefield
        # permanent that combat/effects target.
        zones = ["battlefield", "exile", "graveyard", "hand", "library"]
        special_zones_map = {
             "adventure_cards": "adventure_zone", "phased_out": "phased_out",
             "foretold_cards": "foretold_zone", "suspended_cards": "suspended",
             "unearthed_cards": "unearthed_zone", # Add other special tracking if needed
             "morphed_cards": "face_down_zone", # Represent face-down state
             "manifested_cards": "face_down_zone",
             "commander_zone": "command", # Standardize command zone name
             "companion": "companion_zone",
        }

        # Check standard zones by zone priority across both players.
        for zone in zones:
            for player in [self.p1, self.p2]:
                if not player:
                    continue # Safety check
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
        owner_key = getattr(self, "card_instance_owners", {}).get(card_id)
        if owner_key == "p1":
            return self.p1
        if owner_key == "p2":
            return self.p2
        in_p1 = hasattr(self, 'original_p1_deck') and card_id in self.original_p1_deck
        in_p2 = hasattr(self, 'original_p2_deck') and card_id in self.original_p2_deck
        if in_p1 and not in_p2:
             return self.p1
        if in_p2 and not in_p1:
             return self.p2
        # Mirror fixtures reuse the same card IDs in both decks. When ownership
        # is ambiguous, prefer the current battlefield controller over always
        # sending the card to P1's zone.
        if in_p1 and in_p2:
            controller = self._find_card_controller(card_id)
            if controller:
                return controller
        # Synthetic cards and tokens are not present in either original deck.
        # Their latest tracked player is the best available ownership record;
        # this avoids silently routing every such object to Player 1.
        if not in_p1 and not in_p2:
            last_location = getattr(self, "_last_card_locations", {}).get(card_id)
            if last_location and last_location[0] in (self.p1, self.p2):
                return last_location[0]
            controller = self._find_card_controller(card_id)
            if controller:
                return controller
        # Last resort - default to p1 if owner ambiguous and not controlled.
        return self.p1

    # Consolidate get_card_controller (use find_card_location)
    def get_card_controller(self, card_id):
        """Find the controller of a card currently on the battlefield."""
        # Do the battlefield scan directly. With repeated card IDs, a copy in
        # hand/library must not obscure an on-battlefield permanent.
        for player in [self.p1, self.p2]:
            if player and card_id in player.get("battlefield", []):
                return player
        # Consider returning controller even if not on battlefield?
        # Depends on rules context. For most purposes, only battlefield controller matters.
        # If you need owner regardless of zone, use _find_card_owner_fallback or similar.
        return None
