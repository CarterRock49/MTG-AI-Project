import copy
import re
import logging
from collections import Counter
from collections import defaultdict, deque

class EnhancedManaSystem:
    """Advanced mana handling system that properly implements MTG mana rules."""
        # Define card types and keywords directly here to avoid circular imports
    ALL_CARD_TYPES = [
        'creature', 'artifact', 'enchantment', 'land', 'planeswalker',
        'instant', 'sorcery', 'battle', 'conspiracy', 'dungeon',
        'phenomenon', 'plane', 'scheme', 'vanguard', 'class', 'room'
    ]
    
    def __init__(self, game_state):
        self.game_state = game_state
        self.mana_symbols = {'W', 'U', 'B', 'R', 'G', 'C'}
        self.color_names = {
            'W': 'white',
            'U': 'blue',
            'B': 'black',
            'R': 'red',
            'G': 'green',
            'C': 'colorless'
        }
        
        # FIXED: Add lowercase variants with explicit assignment
        self.lowercase_symbols = {'w', 'u', 'b', 'r', 'g', 'c', 't'} 
        # Add tap symbol to a separate set of special symbols
        self.special_symbols = {'t'}  # The tap symbol needs special handling

    def add_mana(self, player, mana):
        """Add resolved mana production to a player's pool."""
        pool = player.setdefault(
            "mana_pool", {symbol: 0 for symbol in self.mana_symbols})
        for symbol, amount in dict(mana or {}).items():
            normalized = str(symbol).upper()
            if normalized not in self.mana_symbols:
                logging.warning(f"Ignoring unsupported mana-pool key: {symbol}")
                continue
            pool[normalized] = pool.get(normalized, 0) + max(0, int(amount or 0))
        return True

    def _reserved_payment_permanent_ids(self, player, context=None):
        """Return permanents committed to a different component of a cost."""
        context = context or {}
        reserved = set(context.get("_payment_exclude_tap_ids", ()) or ())
        for key in ("convoke_creatures", "improvise_artifacts"):
            choices = context.get(key, ()) or ()
            if not isinstance(choices, (list, tuple)):
                continue
            for identifier in choices:
                permanent_id = self.game_state._find_permanent_id(
                    player, identifier)
                if permanent_id is not None:
                    reserved.add(permanent_id)
        return reserved

    @staticmethod
    def _is_snow_mana_permanent(card):
        """Return whether a snow permanent has a recognizable mana ability."""
        if card is None:
            return False
        type_line = str(getattr(card, "type_line", "") or "")
        if "snow" not in type_line.lower():
            return False
        if "land" in type_line.lower():
            return True
        oracle_text = str(getattr(card, "oracle_text", "") or "")
        return bool(re.search(
            r"\badd\s+(?:\{[WUBRGC]\}|(?:one|two|three|four|five)\s+mana)",
            oracle_text, re.IGNORECASE))

    def track_snow_sources(self, player, exclude_ids=None):
        """
        Track snow permanents that can produce snow mana.
        
        Args:
            player: The player dictionary
            
        Returns:
            int: Number of available snow mana sources
        """
        gs = self.game_state
        
        # Count snow permanents that can produce mana
        snow_sources = 0
        
        excluded = set(exclude_ids or ())
        for card_id in player["battlefield"]:
            if card_id in excluded:
                continue
            card = gs._safe_get_card(card_id)
            
            # Skip if card doesn't exist or is tapped
            if not card or card_id in player["tapped_permanents"]:
                continue
                
            if self._is_snow_mana_permanent(card):
                snow_sources += 1
        
        return snow_sources

    def can_pay_snow_cost(self, player, snow_cost, context=None):
        """
        Check if a player can pay a snow mana cost.
        
        Args:
            player: The player dictionary
            snow_cost: Number of snow mana required
            
        Returns:
            bool: Whether snow cost can be paid
        """
        if snow_cost <= 0:
            return True
            
        # Mana already produced by a snow source retains that quality even
        # after its source is tapped (CR 106.3). Untapped snow sources can be
        # activated atomically by the payment transaction.
        available_snow = sum(
            max(0, min(int(amount or 0),
                       int(player.get("mana_pool", {}).get(color, 0) or 0)))
            for color, amount in player.get("snow_mana_pool", {}).items())
        available_snow += sum(
            max(0, min(int(amount or 0), int(
                player.get("phase_restricted_mana", {}).get(color, 0) or 0)))
            for color, amount in player.get(
                "phase_restricted_snow_mana", {}).items())
        for restriction_key, provenance in player.get(
                "conditional_snow_mana", {}).items():
            if not self._can_use_conditional_mana(
                    restriction_key, context or {}):
                continue
            restricted_pool = player.get("conditional_mana", {}).get(
                restriction_key, {})
            available_snow += sum(
                max(0, min(int(amount or 0), int(
                    restricted_pool.get(color, 0) or 0)))
                for color, amount in provenance.items())
        available_snow += self.track_snow_sources(
            player,
            exclude_ids=self._reserved_payment_permanent_ids(
                player, context))
        
        # Check if player has enough snow mana sources
        return available_snow >= snow_cost

    def pay_snow_cost(self, player, snow_cost, mana_pool=None,
                      snow_pool=None, payment=None, phase_pool=None,
                      conditional_pool=None, phase_snow_pool=None,
                       conditional_snow_pool=None, context=None,
                       exclude_ids=None, defer_source_taps=False):
        """
        Pay a snow mana cost.
        
        Args:
            player: The player dictionary
            snow_cost: Number of snow mana required
            
        Returns:
            bool: Whether payment was successful
        """
        if snow_cost <= 0:
            return True
            
        mana_pool = mana_pool if mana_pool is not None else player["mana_pool"]
        snow_pool = snow_pool if snow_pool is not None else dict(
            player.get("snow_mana_pool", {}))
        payment = payment if payment is not None else {}
        payment.setdefault("snow_spent_colors", defaultdict(int))
        payment.setdefault("snow_tapped_sources", [])
        phase_pool = phase_pool if phase_pool is not None else dict(
            player.get("phase_restricted_mana", {}))
        conditional_pool = conditional_pool if conditional_pool is not None else {
            key: dict(value) for key, value in player.get(
                "conditional_mana", {}).items()}
        phase_snow_pool = (
            phase_snow_pool if phase_snow_pool is not None else dict(
                player.get("phase_restricted_snow_mana", {})))
        conditional_snow_pool = (
            conditional_snow_pool if conditional_snow_pool is not None else {
                key: dict(value) for key, value in player.get(
                    "conditional_snow_mana", {}).items()})

        remaining = int(snow_cost)
        # Consume already-floated snow mana first. Cap provenance against the
        # transaction's current pool so colored/hybrid payments made earlier
        # cannot reuse the same mana unit for {S}.
        for color in ('C', 'W', 'U', 'B', 'R', 'G'):
            available = min(
                max(0, int(snow_pool.get(color, 0) or 0)),
                max(0, int(mana_pool.get(color, 0) or 0)))
            spend = min(remaining, available)
            if spend:
                mana_pool[color] -= spend
                snow_pool[color] = available - spend
                payment.setdefault("colors", defaultdict(int))[color] += spend
                payment["snow_spent_colors"][color] += spend
                remaining -= spend
            if remaining <= 0:
                return True

        for color in ('C', 'W', 'U', 'B', 'R', 'G'):
            available = min(
                max(0, int(phase_snow_pool.get(color, 0) or 0)),
                max(0, int(phase_pool.get(color, 0) or 0)))
            spend = min(remaining, available)
            if spend:
                phase_pool[color] -= spend
                phase_snow_pool[color] = available - spend
                payment.setdefault(
                    "phase_restricted", defaultdict(int))[color] += spend
                payment["snow_spent_colors"][color] += spend
                remaining -= spend
            if remaining <= 0:
                return True

        for restriction_key, provenance in conditional_snow_pool.items():
            if not self._can_use_conditional_mana(
                    restriction_key, context or {}):
                continue
            restricted_pool = conditional_pool.get(restriction_key, {})
            for color in ('C', 'W', 'U', 'B', 'R', 'G'):
                available = min(
                    max(0, int(provenance.get(color, 0) or 0)),
                    max(0, int(restricted_pool.get(color, 0) or 0)))
                spend = min(remaining, available)
                if spend:
                    restricted_pool[color] -= spend
                    provenance[color] = available - spend
                    payment.setdefault("conditional", defaultdict(
                        lambda: defaultdict(int)))[restriction_key][color] += spend
                    payment["snow_spent_colors"][color] += spend
                    remaining -= spend
                if remaining <= 0:
                    return True

        # Find untapped snow permanents. Their produced mana pays {S}
        # directly; it must not also be left in the pool.
        gs = self.game_state
        snow_sources_tapped = 0
        
        excluded = set(exclude_ids or ())
        for card_id in player["battlefield"]:
            if snow_sources_tapped >= remaining:
                break
            if card_id in excluded:
                continue
                
            card = gs._safe_get_card(card_id)
            
            # Skip if card doesn't exist or is already tapped
            if not card or card_id in player["tapped_permanents"]:
                continue
                
            if self._is_snow_mana_permanent(card):
                if (not defer_source_taps
                        and not gs.tap_permanent(card_id, player)):
                    continue
                snow_sources_tapped += 1
                payment["snow_tapped_sources"].append(card_id)
                if defer_source_taps:
                    payment["_snow_source_taps_deferred"] = True

        return snow_sources_tapped >= remaining
    
    def _gather_cost_modification_effects(self, player, card, context=None):
        """
        Gather all cost modification effects from permanents on the battlefield.
        
        Args:
            player: The player dictionary
            card: The card being cast
            context: Optional context for special cases
            
        Returns:
            list: List of cost modification effect dictionaries
        """
        gs = self.game_state
        effects = []
        
        # Check for effects from player's own permanents
        for permanent_id in player["battlefield"]:
            perm_effects = self._get_cost_effects_from_permanent(
                permanent_id, player, card, True, context=context)
            effects.extend(perm_effects)
        
        # Check for effects from opponent's permanents
        opponent = gs.p2 if player == gs.p1 else gs.p1
        for permanent_id in opponent["battlefield"]:
            perm_effects = self._get_cost_effects_from_permanent(
                permanent_id, opponent, card, False, context=context)
            effects.extend(perm_effects)
        
        # Add effects from card itself (e.g., affinity, convoke)
        self_effects = self._get_self_cost_modification_effects(card, player, context)
        effects.extend(self_effects)
        
        return effects

    def _permanent_rules_text_for_costs(self, permanent):
        """Return only the permanent rules text currently affecting costs."""
        active_rules_text = getattr(
            self.game_state, "_active_permanent_rules_text", None)
        if callable(active_rules_text):
            return str(active_rules_text(permanent) or "")
        return str(getattr(permanent, "oracle_text", "") or "")

    @staticmethod
    def _spell_colors_for_cast(card, context=None):
        """Return the announced Room face's colors, or printed card colors."""
        printed_colors = getattr(card, "colors", [0, 0, 0, 0, 0])
        if not getattr(card, "is_room", False) or not isinstance(context, dict):
            return printed_colors
        face_colors = context.get("room_cast_face_colors")
        if (not isinstance(face_colors, (list, tuple))
                or len(face_colors) != 5
                or not all(
                    isinstance(value, (bool, int)) and int(value) in (0, 1)
                    for value in face_colors)):
            return printed_colors
        return [int(bool(value)) for value in face_colors]

    def spell_characteristics_for_cast(self, card, context=None):
        """Return the announced spell's types, subtypes, and Flying status.

        Cost modifiers inspect the spell face being cast, which can differ
        from the physical card's front face for Adventures, MDFCs, and
        prepared copies.  The same helper snapshots successful casts so a
        later first-spell check never depends on a card's current zone/face.
        """
        context = context or {}
        snap_types = context.get("cast_card_types")
        snap_subtypes = context.get("cast_card_subtypes")
        snap_flying = context.get("cast_card_has_flying")
        if (snap_types is not None and snap_subtypes is not None
                and snap_flying is not None):
            return (
                {str(card_type).lower() for card_type in snap_types},
                {str(subtype).lower() for subtype in snap_subtypes},
                bool(snap_flying),
            )

        face = None
        if context.get("prepared_copy"):
            face = context.get("prepared_face") or None
        elif context.get("cast_as_back_face"):
            faces = list(getattr(card, "faces", []) or [])
            face = faces[1] if len(faces) > 1 else None
        elif (context.get("cast_as_adventure")
              and hasattr(card, "get_adventure_data")):
            adventure = card.get_adventure_data() or {}
            if adventure:
                face = {
                    "type_line": adventure.get("type", ""),
                    "oracle_text": adventure.get("effect", ""),
                }

        type_line = str(
            (face or {}).get("type_line", "")
            or getattr(card, "type_line", "") or "")
        oracle_text = str(
            (face or {}).get("oracle_text", "")
            or getattr(card, "oracle_text", "") or "")
        split_type = re.split(r"\s+[\u2014-]\s+", type_line.lower(),
                              maxsplit=1)
        main_type_text = split_type[0]
        card_types = {
            card_type for card_type in self.ALL_CARD_TYPES
            if re.search(rf"\b{re.escape(card_type)}\b", main_type_text)}
        subtypes = ({
            subtype.strip(" ,./").lower()
            for subtype in split_type[1].split()
            if subtype.strip(" ,./")}
            if len(split_type) > 1 else set())
        if face is None:
            card_types = {
                str(card_type).lower() for card_type in getattr(
                    card, "card_types", card_types)} or card_types
            subtypes = {
                str(subtype).lower() for subtype in getattr(
                    card, "subtypes", subtypes)} or subtypes

        has_flying = False
        intrinsic_keywords = getattr(
            type(card), "intrinsic_keyword_names", None)
        if callable(intrinsic_keywords):
            has_flying = "flying" in intrinsic_keywords(oracle_text)
        else:
            has_flying = bool(re.search(
                r"(?im)^\s*flying(?:\s*[,.;]|\s*$)", oracle_text))
        if face is None:
            keyword_vocabulary = list(
                getattr(type(card), "ALL_KEYWORDS", []) or [])
            keywords = getattr(card, "keywords", [])
            if "flying" in keyword_vocabulary:
                flying_index = keyword_vocabulary.index("flying")
                if flying_index < len(keywords):
                    has_flying = has_flying or bool(keywords[flying_index])
        return card_types, subtypes, has_flying

    def _is_momo_eligible_spell(self, card, context=None):
        if not card:
            return False
        card_types, subtypes, has_flying = \
            self.spell_characteristics_for_cast(card, context)
        return bool(
            "creature" in card_types
            and "lemur" not in subtypes
            and has_flying)

    def _get_cost_effects_from_permanent(
            self, permanent_id, controller, target_card, is_controller,
            context=None):
        """
        Extract cost modification effects from a permanent. (Enhanced Parsing & Validation)

        Args:
            permanent_id: ID of the permanent to check
            controller: The player who controls the permanent
            target_card: The card whose cost might be modified
            is_controller: Whether the permanent's controller is casting the spell

        Returns:
            list: List of cost modification effect dictionaries
        """
        gs = self.game_state
        permanent = gs._safe_get_card(permanent_id)
        effects = []

        # Ensure necessary objects and attributes exist
        if not permanent or not hasattr(permanent, 'oracle_text') or not target_card:
            return effects
        if (getattr(gs, "layer_system", None)
                and gs.layer_system.source_has_lost_all_abilities(
                    permanent_id)):
            return effects

        # Get info for conditional checks - use getattr for safety
        oracle_text = self._permanent_rules_text_for_costs(permanent).lower()
        target_card_types = getattr(target_card, 'card_types', [])
        target_card_subtypes = getattr(target_card, 'subtypes', [])
        target_card_colors = self._spell_colors_for_cast(
            target_card, context=context)
        perm_name = getattr(permanent, 'name', f"Card {permanent_id}")

        momo_reduction = re.search(
            r"the first non-lemur creature spell with flying you cast during "
            r"each of your turns costs \{1\} less to cast", oracle_text)
        if momo_reduction:
            # This exact declaration has restrictions that the generic
            # qualifier parser cannot represent.  Handle it atomically and
            # return so the broad parser cannot add a duplicate reduction.
            if (not is_controller
                    or gs._get_active_player() is not controller
                    or not self._is_momo_eligible_spell(
                        target_card, context=context)):
                return effects
            already_cast = any(
                isinstance(entry, tuple) and len(entry) >= 2
                and entry[1] is controller
                and self._is_momo_eligible_spell(
                    gs._safe_get_card(entry[0]),
                    context=(entry[2] if len(entry) > 2
                             and isinstance(entry[2], dict) else {}))
                for entry in getattr(gs, "spells_cast_this_turn", []))
            if not already_cast:
                effects.append({
                    'type': 'reduction', 'amount': 1,
                    'applies_to': 'generic', 'source': perm_name,
                })
            return effects

        # Conditional battlefield reductions must prove their condition
        # before the otherwise-generic cost-modifier parser sees them.  In
        # particular, Gran-Gran's ``Noncreature spells ...`` line used to
        # discount every matching spell even with no Lessons in the
        # graveyard because the trailing ``as long as`` clause was ignored.
        lesson_threshold = re.search(
            r"noncreature spells you cast cost \{(\d+)\} less to cast "
            r"as long as there are (\w+|\d+) or more lesson cards in your "
            r"graveyard", oracle_text)
        if lesson_threshold:
            if "creature" in {
                    str(card_type).lower()
                    for card_type in target_card_types}:
                return effects
            threshold_token = lesson_threshold.group(2)
            number_words = {
                "one": 1, "two": 2, "three": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10,
            }
            threshold = (int(threshold_token)
                         if threshold_token.isdigit()
                         else number_words.get(threshold_token, 0))
            lesson_count = sum(
                1 for graveyard_id in controller.get("graveyard", [])
                if "lesson" in {
                    str(subtype).lower() for subtype in getattr(
                        gs._safe_get_card(graveyard_id), "subtypes", [])})
            if lesson_count < threshold:
                return effects

        # --- Regex to find cost modifications ---
        # Pattern: "(Qualifier) spells (Scope)? cost {Amount} (less|more)"
        # Qualifier examples: "Creature", "Red", "Artifact", "" (any spell)
        # Scope examples: "you cast", "your opponents cast", "" (applies to all)
        # Amount: N or Color Symbol (WUBRGC)
        cost_pattern = r"(?:^|\n|;|\.)\s*([a-zA-Z\s\-,]+?)?\s*spells?\s*(you cast|your opponents cast)?\s*cost\s+\{(\w+)\}\s+(less|more)"
        # Example non-spell cost pattern (Needs more specific rules context)
        # ability_cost_pattern = r"activated abilities cost ... less/more"

        matches = re.finditer(cost_pattern, oracle_text)
        for match in matches:
            qualifier, subject_scope, amount_str, direction = match.groups()
            qualifier = qualifier.strip() if qualifier else "any"
            subject_scope = subject_scope.strip() if subject_scope else ""
            amount_str = amount_str.strip().upper() # Normalize amount

            # Check if effect applies based on who is casting
            if subject_scope == "you cast" and not is_controller: continue
            if subject_scope == "your opponents cast" and is_controller: continue

            # Validate amount and determine if it's generic or colored
            amount = 0
            color_specific = None
            is_generic_modifier = False
            if amount_str.isdigit():
                amount = int(amount_str)
                is_generic_modifier = True
            elif amount_str in self.mana_symbols: # W, U, B, R, G, C
                color_specific = amount_str
                amount = 1 # e.g., {W} less means 1 less W required
            else:
                logging.warning(f"Invalid amount '{amount_str}' in cost mod from {perm_name}. Skipping.")
                continue # Skip if amount isn't number or single color symbol

            # --- Check qualifier against target card ---
            applies = True
            qualifier_lower = qualifier.lower()

            # Handle common qualifiers more precisely
            color_words = ["white", "blue", "black", "red", "green", "colorless", "multicolored"]
            # Use our own constant instead of Card.ALL_CARD_TYPES
            card_type_words = self.ALL_CARD_TYPES
            all_qualifiers = qualifier_lower.split() # Handle multi-word qualifiers like "artifact creature"

            found_match_for_qualifier = False
            is_negation = False # e.g., "noncreature"
            effective_qualifier = qualifier_lower

            if qualifier_lower == "any" or qualifier_lower == "spells":
                found_match_for_qualifier = True # Applies to any spell
            else:
                 if qualifier_lower.startswith("non"):
                      is_negation = True
                      effective_qualifier = qualifier_lower[3:] # e.g., "creature" from "noncreature"

                 # Check Colors
                 matched_color = False
                 color_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
                 if effective_qualifier == "colorless":
                      matched_color = sum(target_card_colors) == 0
                 elif effective_qualifier == "multicolored":
                      matched_color = sum(target_card_colors) > 1
                 elif effective_qualifier in color_map:
                      matched_color = bool(target_card_colors[color_map[effective_qualifier]])

                 if is_negation: matched_color = not matched_color
                 if matched_color: found_match_for_qualifier = True

                 # Check Card Types (if not already matched by color)
                 if not found_match_for_qualifier:
                      matched_type = effective_qualifier in target_card_types
                      if is_negation: matched_type = not matched_type
                      if matched_type: found_match_for_qualifier = True

                 # Check Subtypes (if not already matched by color or type)
                 # Simple check - assumes qualifier is a single subtype word
                 if not found_match_for_qualifier:
                     matched_subtype = effective_qualifier in target_card_subtypes
                     # Negation doesn't typically apply to subtypes like this.
                     if matched_subtype: found_match_for_qualifier = True


            # If no match found for the specific qualifier, the effect doesn't apply
            if not found_match_for_qualifier and qualifier_lower not in ["any", "spells"]:
                 applies = False

            # Construct and add effect if it applies
            if applies:
                 effect = {'type': 'reduction' if direction == 'less' else 'increase', 'amount': amount, 'source': perm_name}
                 if color_specific:
                     # Rule 609.4b: Cannot reduce colored costs below zero.
                     # Rule 609.4a: Cost increases add to the cost.
                     # Reducing specific colors is complex. Simpler: Apply reduction to generic if possible, else ignore.
                     # Let's assume reduction ONLY applies if that color pip exists.
                     # Increasing adds generic for now (simpler than adding pips).
                     if effect['type'] == 'reduction':
                          effect['applies_to'] = 'specific_color_pip' # Signal it reduces pips
                     else: # Increase
                          effect['applies_to'] = 'generic' # Increase adds generic cost for simplicity
                          effect['amount'] = amount # Generic amount is 1 for a colored increase {W}
                     effect['color'] = color_specific
                 else: # Generic modifier amount
                     effect['applies_to'] = 'generic'
                 effects.append(effect)
                 logging.debug(f"Found cost effect: {direction} {amount_str} ({qualifier} spells) from {perm_name}")

        return effects

    def _get_self_cost_modification_effects(self, card, player, context=None):
        """
        Get cost modification effects from the card itself. (Enhanced Context Handling)

        Args:
            card: The card being cast
            player: The player casting the card
            context: Optional context for special cases (e.g., Convoke, Delve choices)

        Returns:
            list: List of cost modification effect dictionaries
        """
        effects = []
        if not card or not hasattr(card, 'oracle_text'):
            return effects
        if context is None: context = {}

        oracle_text = card.oracle_text.lower()
        card_name = getattr(card, 'name', 'Unknown Card')

        # Domain cost reductions count distinct basic land TYPES among lands
        # controlled, not basic lands and not the number of lands. Nonbasic
        # duals with basic land types contribute each printed/live subtype.
        domain_match = re.search(
            r"this spell costs \{(\d+)\} less to cast for each basic land type "
            r"among lands you control", oracle_text)
        if domain_match:
            basic_land_types = {"plains", "island", "swamp", "mountain", "forest"}
            controlled_types = set()
            for permanent_id in player.get("battlefield", []):
                permanent = self.game_state._safe_get_card(permanent_id)
                if not permanent or "land" not in getattr(permanent, "card_types", []):
                    continue
                controlled_types.update(
                    basic_land_types.intersection(
                        str(subtype).lower()
                        for subtype in getattr(permanent, "subtypes", [])))
            reduction = int(domain_match.group(1)) * len(controlled_types)
            if reduction:
                effects.append({
                    'type': 'reduction', 'amount': reduction,
                    'applies_to': 'generic', 'source': f'{card_name} Domain',
                })

        graveyard_spell_match = re.search(
            r"this spell costs \{(\d+)\} less to cast for each instant and "
            r"sorcery card in your graveyard", oracle_text)
        if graveyard_spell_match:
            spell_count = sum(
                1 for graveyard_id in player.get("graveyard", [])
                if ({"instant", "sorcery"}.intersection(
                    str(card_type).lower()
                    for card_type in getattr(
                        self.game_state._safe_get_card(graveyard_id),
                        "card_types", []))))
            reduction = int(graveyard_spell_match.group(1)) * spell_count
            if reduction:
                effects.append({
                    'type': 'reduction', 'amount': reduction,
                    'applies_to': 'generic',
                    'source': f'{card_name} graveyard spells',
                })

        # Hearth Elemental counts a union of three card qualities.  In
        # particular, an adventurer is still eligible while its creature face
        # is the card's active graveyard characteristic, and a card matching
        # more than one quality contributes only once.
        hearth_reduction = re.search(
            r"this spell costs \{x\} less to cast, where x is the number of "
            r"cards in your graveyard that are instant cards, sorcery cards, "
            r"and/or have an adventure", oracle_text)
        if hearth_reduction and not context.get("cast_as_adventure"):
            eligible_cards = 0
            for graveyard_id in player.get("graveyard", []):
                graveyard_card = self.game_state._safe_get_card(graveyard_id)
                if not graveyard_card:
                    continue
                card_types = {
                    str(card_type).lower() for card_type in getattr(
                        graveyard_card, "card_types", [])}
                has_adventure = getattr(graveyard_card, "has_adventure", None)
                if ({"instant", "sorcery"}.intersection(card_types)
                        or (callable(has_adventure) and has_adventure())):
                    eligible_cards += 1
            if eligible_cards:
                effects.append({
                    'type': 'reduction', 'amount': eligible_cards,
                    'applies_to': 'generic',
                    'source': f'{card_name} graveyard card types',
                })

        # Sunderflock and the same template use a characteristic of the
        # caster's battlefield to define X; X is not a mana symbol in the
        # printed cost.  Only the greatest single mana value is subtracted.
        elemental_reduction = re.search(
            r"this spell costs \{x\} less to cast, where x is the greatest "
            r"mana value among elementals you control", oracle_text)
        if elemental_reduction:
            greatest_mana_value = max((
                max(0, int(getattr(
                    self.game_state._safe_get_card(permanent_id),
                    "cmc", 0) or 0))
                for permanent_id in player.get("battlefield", [])
                if "elemental" in {
                    str(subtype).lower() for subtype in getattr(
                        self.game_state._safe_get_card(permanent_id),
                        "subtypes", [])}
            ), default=0)
            if greatest_mana_value:
                effects.append({
                    'type': 'reduction', 'amount': greatest_mana_value,
                    'applies_to': 'generic',
                    'source': f'{card_name} controlled Elementals',
                })

        # These reductions are determined from targets chosen at CR 601.2c.
        # Casts with these phrases defer payment until that choice is committed,
        # so context['targets'] is authoritative here.
        target_ids = self._target_ids_from_cast_context(context)
        target_discount = re.search(
            r"this spell costs \{(\d+)\} less to cast if it targets "
            r"(?:a|an) (tapped permanent|permanent you control)", oracle_text)
        if target_discount and target_ids:
            condition = target_discount.group(2)
            qualifies = False
            if condition == "permanent you control":
                qualifies = any(
                    self.game_state.get_card_controller(target_id) is player
                    for target_id in target_ids)
            elif condition == "tapped permanent":
                for target_id in target_ids:
                    target_controller = self.game_state.get_card_controller(target_id)
                    if (target_controller
                            and target_id in target_controller.get("tapped_permanents", set())):
                        qualifies = True
                        break
            if qualifies:
                effects.append({
                    'type': 'reduction', 'amount': int(target_discount.group(1)),
                    'applies_to': 'generic',
                    'source': f'{card_name} target condition',
                })

        # Check for affinity
        affinity_match = re.search(r"affinity for (\w+)", oracle_text)
        if affinity_match:
            affinity_type = affinity_match.group(1) # e.g., 'artifacts', 'planeswalkers'
            count = 0
            if affinity_type == "artifacts":
                count = sum(1 for cid in player["battlefield"]
                              if 'artifact' in getattr(self.game_state._safe_get_card(cid), 'card_types', []))
            # Add other affinity types if needed (islands, planeswalkers...)
            if count > 0:
                effects.append({'type': 'reduction', 'amount': count, 'applies_to': 'generic', 'source': f'{card_name} Affinity'})
                logging.debug(f"Applying Affinity reduction: {count} generic for {card_name}")


        # Check for convoke - USE context['convoke_creatures'] which should be list of IDs/indices
        if "convoke" in oracle_text and context.get("convoke_creatures"):
            convoke_list = context["convoke_creatures"]
            # ManaSystem doesn't know creature colors/types here. Assume GameState/ActionHandler validated.
            # Simple version: Reduce generic by count. Full version needs color handling during payment.
            convoke_reduction = len(convoke_list)
            if convoke_reduction > 0:
                 # The *effect* is reducing the cost now, actual tapping happens during payment.
                 effects.append({'type': 'reduction', 'amount': convoke_reduction, 'applies_to': 'generic', 'source': f'{card_name} Convoke'})
                 logging.debug(f"Applying Convoke cost reduction: {convoke_reduction} generic for {card_name}")
                 # We need to signal mana system how much of each *color* can be paid via convoke during payment phase
                 # Adding this info to the effect is one way.
                 # This is complex. Let's defer colored cost reduction via convoke to the payment step for now.

        # Check for delve - USE context['delve_cards'] which should be list of GY indices/IDs
        if "delve" in oracle_text and context.get("delve_cards"):
            delve_list = context["delve_cards"]
            delve_reduction = len(delve_list)
            if delve_reduction > 0:
                effects.append({'type': 'reduction', 'amount': delve_reduction, 'applies_to': 'generic', 'source': f'{card_name} Delve'})
                logging.debug(f"Applying Delve cost reduction: {delve_reduction} generic for {card_name}")
                # Actual exiling happens during payment step based on context.

        # Check for Improvise - USE context['improvise_artifacts']
        if "improvise" in oracle_text and context.get("improvise_artifacts"):
             improvise_list = context["improvise_artifacts"]
             improvise_reduction = len(improvise_list)
             if improvise_reduction > 0:
                 effects.append({'type': 'reduction', 'amount': improvise_reduction, 'applies_to': 'generic', 'source': f'{card_name} Improvise'})
                 logging.debug(f"Applying Improvise cost reduction: {improvise_reduction} generic for {card_name}")
                 # Actual tapping happens during payment step based on context.

        return effects

    @staticmethod
    def _target_ids_from_cast_context(context):
        """Flatten only target collections from a casting context."""
        if not isinstance(context, dict):
            return []
        targets = context.get("targets")
        if not isinstance(targets, dict):
            return []
        target_ids = []
        for value in targets.values():
            if isinstance(value, (list, tuple, set)):
                target_ids.extend(value)
        return list(dict.fromkeys(target_ids))

    @staticmethod
    def has_target_dependent_reduction(card):
        """Whether a spell's own discount depends on its chosen targets."""
        oracle_text = getattr(card, "oracle_text", "").lower() if card else ""
        return bool(re.search(
            r"this spell costs \{\d+\} less to cast if it targets "
            r"(?:a|an) (?:tapped permanent|permanent you control)",
            oracle_text))

    def can_pay_with_target_dependent_reduction(self, player, base_cost,
                                                card_id, context=None):
        """Return True when some legal target makes a target-priced spell payable.

        This is an affordability probe only. It never commits a target or spends
        mana; the actual cast recalculates from the player's eventual choices.
        """
        card = self.game_state._safe_get_card(card_id)
        if not self.has_target_dependent_reduction(card):
            return False
        target_type = self.game_state._get_target_type_from_text(card.oracle_text)
        valid_map = self.game_state.targeting_system.get_valid_targets(
            card_id, player, target_type, effect_text=card.oracle_text)
        valid_ids = {
            target_id
            for category_ids in valid_map.values()
            for target_id in category_ids
        }
        for target_id in valid_ids:
            candidate_context = dict(context or {})
            candidate_context["targets"] = {"chosen": [target_id]}
            candidate_cost = self.apply_cost_modifiers(
                player, base_cost, card_id, candidate_context)
            if self.can_pay_mana_cost(player, candidate_cost, candidate_context):
                return True
        return False

    def _apply_cost_effect(self, cost, effect):
        """
        Apply a cost modification effect to a cost.
        
        Args:
            cost: The cost dictionary to modify
            effect: The effect to apply
            
        Returns:
            dict: The modified cost
        """
        modified_cost = self._normalize_mana_cost(cost)
        
        if effect['applies_to'] == 'generic':
            if effect['type'] == 'reduction':
                modified_cost['generic'] = max(0, modified_cost['generic'] - effect['amount'])
                logging.debug(f"Reducing generic cost by {effect['amount']} from {effect.get('source', 'unknown')}")
            elif effect['type'] == 'increase':
                modified_cost['generic'] += effect['amount']
                logging.debug(f"Increasing generic cost by {effect['amount']} from {effect.get('source', 'unknown')}")
        elif effect['applies_to'] == 'specific_color' and 'color' in effect:
            color = effect['color']
            if color in modified_cost:
                if effect['type'] == 'reduction':
                    modified_cost[color] = max(0, modified_cost[color] - effect['amount'])
                    logging.debug(f"Reducing {color} cost by {effect['amount']} from {effect.get('source', 'unknown')}")
                elif effect['type'] == 'increase':
                    modified_cost[color] += effect['amount']
                    logging.debug(f"Increasing {color} cost by {effect['amount']} from {effect.get('source', 'unknown')}")
        
        return modified_cost
        
    def apply_cost_modifiers(self, player, cost, card_id, context=None):
        """Apply cost modifiers, now accepting context."""
        gs = self.game_state
        context_card = (context or {}).get("card")
        card = (context_card if hasattr(context_card, "card_types")
                else gs._safe_get_card(card_id)
                if card_id is not None else None)

        modified_cost = self._normalize_mana_cost(cost)
        applied_modifications = {'reductions': [], 'increases': []}

        # Get effects based on the card being cast (or generic if no card)
        cost_effects = self._gather_cost_modification_effects(player, card, context)

        # CR 601.2f BUGFIX (July 2026): cost INCREASES apply before cost
        # reductions. The old reductions-first order let a reduction bottom
        # out at zero generic before a tax applied, over-pricing the spell by
        # up to the clipped amount (e.g. {2} +2 tax -3 discount: correct {1},
        # old order {2}). Cost-reduction decks were systematically mis-priced
        # against tax effects.
        # Apply increases first
        for effect in [e for e in cost_effects if e['type'] == 'increase']:
             original_cost_values = modified_cost.copy()
             modified_cost = self._apply_cost_effect(modified_cost, effect)
             # Track change
             change_amount = 0
             if effect['applies_to'] == 'generic':
                 change_amount = modified_cost['generic'] - original_cost_values['generic']
             elif effect['applies_to'] == 'specific_color':
                 color = effect['color']
                 change_amount = modified_cost.get(color,0) - original_cost_values.get(color,0)
             if change_amount > 0:
                  applied_modifications['increases'].append({ 'amount': change_amount, 'source': effect.get('source', 'unknown'), 'type': effect['applies_to']})

        # Apply reductions after all increases (CR 601.2f)
        for effect in [e for e in cost_effects if e['type'] == 'reduction']:
             original_cost_values = modified_cost.copy() # Copy before applying effect
             modified_cost = self._apply_cost_effect(modified_cost, effect)
             # Track change
             change_amount = 0
             if effect['applies_to'] == 'generic':
                 change_amount = original_cost_values['generic'] - modified_cost['generic']
             elif effect['applies_to'] == 'specific_color':
                 color = effect['color']
                 change_amount = original_cost_values.get(color,0) - modified_cost.get(color,0)
             if change_amount > 0:
                  applied_modifications['reductions'].append({ 'amount': change_amount, 'source': effect.get('source', 'unknown'), 'type': effect['applies_to']})

        # Apply minimum cost effects last
        final_cost = self.apply_minimum_cost_effects(player, modified_cost, card_id, context)

        # Log changes if significant
        if applied_modifications['reductions'] or applied_modifications['increases']:
            reduction_str = ", ".join([f"{mod['amount']} {mod['type']} from {mod['source']}" for mod in applied_modifications['reductions']])
            increase_str = ", ".join([f"{mod['amount']} {mod['type']} from {mod['source']}" for mod in applied_modifications['increases']])
            logging.debug(f"Cost modifiers applied to {getattr(card,'name','spell')}: Reductions=[{reduction_str}], Increases=[{increase_str}] -> Final Cost: {self._format_mana_cost_for_logging(final_cost)}")

        # Store applied mods in context if provided
        if context is not None:
             context['applied_cost_modifications'] = applied_modifications

        return final_cost
    
    def _get_kicker_cost(self, card):
        """
        Extract the kicker cost from a card's oracle text.
        
        Args:
            card: The card object
            
        Returns:
            dict: Parsed kicker cost dictionary or None if not found
        """
        if not card or not hasattr(card, 'oracle_text'):
            return None
            
        oracle_text = card.oracle_text.lower()
        
        # Check if card has kicker
        if "kicker" not in oracle_text:
            return None
            
        # Parse kicker cost
        import re
        match = re.search(r"kicker [^\(]([^\)]+)", oracle_text)
        if not match:
            return None
            
        kicker_cost = match.group(1).strip()
        return self.parse_mana_cost(kicker_cost)

    def apply_minimum_cost_effects(self, player, cost, card_id, context=None):
        """
        Apply minimum cost effects like Trinisphere.
        
        Args:
            player: The player dictionary
            cost: The parsed mana cost dictionary
            card_id: ID of the card being cast
            context: Optional context for special cases
            
        Returns:
            dict: The cost dictionary with minimum cost applied
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        final_cost = self._normalize_mana_cost(cost)
        if not card:
            return final_cost
        
        # Calculate the total mana value of the spell
        total_cost = 0
        
        # Add colored mana costs
        for color in ['W', 'U', 'B', 'R', 'G', 'C']:
            total_cost += final_cost[color]
        
        # Add generic cost
        total_cost += final_cost['generic']
        
        # Add hybrid costs (count each hybrid symbol as 1)
        total_cost += len(final_cost['hybrid'])
        
        # Add phyrexian costs (count each phyrexian symbol as 1)
        total_cost += len(final_cost['phyrexian'])
        
        # Look for minimum cost effects (like Trinisphere)
        min_cost_value = 0
        
        for player_idx, p in enumerate([gs.p1, gs.p2]):
            for battlefield_id in p["battlefield"]:
                battlefield_card = gs._safe_get_card(battlefield_id)
                if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                    continue
                    
                oracle_text = self._permanent_rules_text_for_costs(
                    battlefield_card).lower()
                
                # Trinisphere effect
                if "each spell with mana value less than" in oracle_text and "has a mana value of" in oracle_text:
                    import re
                    match = re.search(r"less than (\d+) has a mana value of (\d+)", oracle_text)
                    if match:
                        threshold = int(match.group(1))
                        min_value = int(match.group(2))
                        
                        if total_cost < threshold and min_value > min_cost_value:
                            min_cost_value = min_value
                            logging.debug(f"Found minimum cost effect: {min_value} from {battlefield_card.name}")
        
        # Apply minimum cost if needed
        if min_cost_value > 0 and total_cost < min_cost_value:
            # Adjust generic mana to meet the minimum cost
            final_cost["generic"] += (min_cost_value - total_cost)
            logging.debug(f"Applied minimum cost effect: Adjusted total cost from {total_cost} to {min_cost_value}")
        
        return final_cost
        
    def pay_phyrexian_mana(self, player, phyrexian_colors):
        """
        Pay phyrexian mana costs optimally using mana or life.
        
        Args:
            player: The player dictionary
            phyrexian_colors: List of phyrexian color costs
            
        Returns:
            tuple: (bool success, int life_paid)
        """
        if not phyrexian_colors:
            return True, 0
        
        # Track payments
        life_paid = 0
        
        # First, try to pay with mana when available
        for color in phyrexian_colors:
            if player["mana_pool"].get(color, 0) > 0:
                player["mana_pool"][color] -= 1
            else:
                # Pay with life
                life_paid += 2
        
        # Check if player has enough life
        if player["life"] < life_paid:
            return False, 0  # Can't pay with life
        
        # Apply life payment
        player["life"] -= life_paid
        return True, life_paid

    def parse_mana_cost(self, cost_text):
        """Parse a mana cost string into structured format with enhanced handling."""
        # Initialize default mana cost dictionary
        mana_cost = {
            'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0,
            'generic': 0, 'X': 0,
            'hybrid': [],  # List of tuples for hybrid tokens (e.g. ["W/U"])
            'phyrexian': [],  # List of tuples for phyrexian tokens (e.g. ["W/P"])
            'snow': 0,  # Snow mana requirement
            'any_color': 0,  # Mana of any color (e.g. {1}), different from generic
            'conditional': []  # Conditional mana requirements
        }
        
        # Handle None, empty string, or non-string inputs
        if cost_text is None or not isinstance(cost_text, str) or cost_text.strip() == '':
            logging.debug(f"Parsing mana cost for input: {cost_text}. Returning default empty cost.")
            return mana_cost
        
        # Remove any whitespace
        cost_text = cost_text.replace(' ', '')
        
        # Special case for 0 mana cost (free spells)
        if cost_text == '':
            return mana_cost
        
        # Extract mana tokens (handles formats like "{W}", "{1}", "{G/U}", etc.)
        tokens = re.findall(r'\{([^}]+)\}', cost_text)
        
        # If no tokens found, log and return default
        if not tokens:
            logging.warning(f"No valid mana tokens found in cost: {cost_text}")
            return mana_cost
        
        for token in tokens:
            # Normalize token to uppercase for consistency
            clean_token = token.strip().upper()
            
            # Skip tap symbol - it's not actually mana
            if clean_token.lower() == 't':
                continue
                
            # Basic mana symbols
            if clean_token in self.mana_symbols:
                mana_cost[clean_token] += 1
                
            # Generic mana
            elif clean_token.isdigit():
                mana_cost['generic'] += int(clean_token)
                
            # X cost
            elif clean_token == 'X':
                mana_cost['X'] += 1
                
            # Hybrid mana (expanded handling)
            elif '/' in clean_token and 'P' not in clean_token and not any(c.isdigit() for c in clean_token):
                # Standard hybrid (e.g., W/U)
                colors = clean_token.split('/')
                if all(c in self.mana_symbols for c in colors):
                    mana_cost['hybrid'].append(tuple(colors))
                else:
                    logging.warning(f"Invalid hybrid mana token: {token}")
                    
            # Phyrexian mana (expanded handling)
            elif '/' in clean_token and 'P' in clean_token:
                colors = clean_token.split('/')
                phyrexian_color = next((c for c in colors if c != 'P'), None)
                if phyrexian_color in self.mana_symbols:
                    mana_cost['phyrexian'].append(phyrexian_color)
                else:
                    logging.warning(f"Invalid phyrexian mana token: {token}")
                    
            # Snow mana
            elif clean_token == 'S':
                mana_cost['snow'] += 1
                
            # Two-hybrid mana (expanded handling)
            elif '/' in clean_token and any(c.isdigit() for c in clean_token):
                parts = clean_token.split('/')
                numeric_part = next((p for p in parts if p.isdigit()), None)
                color_part = next((p for p in parts if p in self.mana_symbols), None)
                
                if numeric_part and color_part:
                    mana_cost['hybrid'].append((numeric_part, color_part))
                else:
                    logging.warning(f"Invalid two-hybrid mana token: {token}")
                    
            # Any mana of any color
            elif clean_token in ['WUBRG', 'WUBRGC']:
                mana_cost['any_color'] += 1
                
            # Other special cases
            else:
                logging.warning(f"Unrecognized mana token: {token} in {cost_text}")
        
        return mana_cost

    @staticmethod
    def _normalize_mana_cost(cost):
        """Return an independent, complete parsed-cost mapping.

        Alternative-cost and legacy callers can supply sparse dictionaries.
        Unknown metadata keys remain available to specialized costs, while
        mutable fields are copied so affordability probes cannot mutate their
        caller's context.
        """
        normalized = {
            'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0,
            'generic': 0, 'X': 0, 'hybrid': [], 'phyrexian': [],
            'snow': 0, 'any_color': 0, 'conditional': [],
        }
        if not isinstance(cost, dict):
            return normalized
        for key, value in cost.items():
            if isinstance(value, list):
                normalized[key] = list(value)
            elif isinstance(value, dict):
                normalized[key] = value.copy()
            else:
                normalized[key] = value
        return normalized
    
    def calculate_alternative_cost(self, card_id, controller, alt_cost_type, context=None):
        """
        Calculate an alternative cost for a card based on various alternative casting methods.
        
        Args:
            card_id: The card to calculate cost for
            controller: The player casting the card
            alt_cost_type: Type of alternative cost ('foretell', 'flashback', 'suspend', etc.)
            context: Additional context information
            
        Returns:
            dict: The calculated alternative cost dictionary, or None if not applicable
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        if not card or not hasattr(card, 'oracle_text') or not hasattr(card, 'mana_cost'):
            return None
            
        oracle_text = card.oracle_text.lower()
        normal_cost = self.parse_mana_cost(card.mana_cost)
        alt_cost = None
        
        if alt_cost_type == "foretell":
            # Find foretell cost
            import re
            match = re.search(r"foretell [^\(]([^\)]+)", oracle_text)
            if match:
                foretell_cost = match.group(1)
                alt_cost = self.parse_mana_cost(foretell_cost)
                logging.debug(f"Calculated foretell cost for {card.name}: {foretell_cost}")
        
        elif alt_cost_type == "exile_permission":
            alternative_cost = (context or {}).get("alternative_cost")
            if not alternative_cost:
                return None
            alt_cost = self.parse_mana_cost(alternative_cost)
        elif alt_cost_type == "harmonize":
            harmonize_cost = (context or {}).get("harmonize_cost")
            if not harmonize_cost:
                return None
            alt_cost = self.parse_mana_cost(harmonize_cost)
            alt_cost["generic"] = max(
                0, int(alt_cost.get("generic", 0))
                - max(0, int((context or {}).get(
                    "harmonize_reduction", 0) or 0)))
        elif alt_cost_type == "flashback":
            # Find flashback cost
            import re
            flashback_cost = (context or {}).get("flashback_cost")
            if not flashback_cost:
                match = re.search(
                    r"(?:^|\n)flashback\s+((?:\{[^}]+\})+)",
                    oracle_text, re.IGNORECASE)
                flashback_cost = match.group(1) if match else None
            if flashback_cost:
                alt_cost = self.parse_mana_cost(flashback_cost)
                logging.debug(f"Calculated flashback cost for {card.name}: {flashback_cost}")
        
        elif alt_cost_type == "escape":
            # Find escape cost and exile requirement
            import re
            match = re.search(r"escape—([^,]+)(, exile [^\.]+)?", oracle_text)
            if match:
                escape_cost = match.group(1).strip()
                exile_requirement = match.group(2) if match.group(2) else "exile five other cards"
                
                # Parse exile requirement
                exile_count = 5  # Default
                exile_match = re.search(r"exile (\w+)", exile_requirement)
                if exile_match:
                    exile_word = exile_match.group(1)
                    if exile_word.isdigit():
                        exile_count = int(exile_word)
                    elif exile_word == "three":
                        exile_count = 3
                    elif exile_word == "four":
                        exile_count = 4
                    elif exile_word == "five":
                        exile_count = 5
                
                # Check if player has enough cards in graveyard to exile
                graveyard_size = len(controller["graveyard"])
                if graveyard_size <= exile_count:
                    return None  # Not enough cards to exile
                    
                alt_cost = self.parse_mana_cost(escape_cost)
                alt_cost["exile_cards"] = exile_count
                logging.debug(f"Calculated escape cost for {card.name}: {escape_cost}, exile {exile_count} cards")
        
        elif alt_cost_type == "adventure":
            # Find adventure cost (if card has an adventure half)
            if "adventure" in oracle_text:
                import re
                # Look for pattern like "Creature Name   W/U\nCreature Type"
                match = re.search(r"\n([^\n]+)\s+([^\n]+)\n", oracle_text)
                if match:
                    adventure_name = match.group(1).strip()
                    adventure_cost = match.group(2).strip()
                    
                    alt_cost = self.parse_mana_cost(adventure_cost)
                    logging.debug(f"Calculated adventure cost for {card.name}: {adventure_cost}")
        
        elif alt_cost_type == "overload":
            # Find overload cost
            import re
            match = re.search(r"overload [^\(]([^\)]+)", oracle_text)
            if match:
                overload_cost = match.group(1)
                alt_cost = self.parse_mana_cost(overload_cost)
                logging.debug(f"Calculated overload cost for {card.name}: {overload_cost}")
        
        elif alt_cost_type == "spectacle":
            # Find spectacle cost
            import re
            match = re.search(r"spectacle [^\(]([^\)]+)", oracle_text)
            if match:
                spectacle_cost = match.group(1)
                alt_cost = self.parse_mana_cost(spectacle_cost)
                
                # Check if opponent lost life this turn
                opponent = gs.p2 if controller == gs.p1 else gs.p1
                lost_life_this_turn = opponent.get("lost_life_this_turn", False)
                
                if not lost_life_this_turn:
                    return None  # Spectacle condition not met
                    
                logging.debug(f"Calculated spectacle cost for {card.name}: {spectacle_cost}")
                
        elif alt_cost_type == "mutate":
            # Find mutate cost
            import re
            match = re.search(r"\bmutate\s*((?:\{[^}]+\})+)", oracle_text)
            if match:
                mutate_cost = match.group(1)
                alt_cost = self.parse_mana_cost(mutate_cost)
                logging.debug(f"Calculated mutate cost for {card.name}: {mutate_cost}")
                
        elif alt_cost_type == "surge":
            # Find surge cost
            import re
            match = re.search(r"surge [^\(]([^\)]+)", oracle_text)
            if match:
                surge_cost = match.group(1)
                alt_cost = self.parse_mana_cost(surge_cost)
                
                # Check if surge condition is met (a teammate cast a spell this turn)
                if not context or not context.get("teammate_cast_spell", False):
                    return None  # Surge condition not met
                    
                logging.debug(f"Calculated surge cost for {card.name}: {surge_cost}")
                
        elif alt_cost_type == "prowl":
            # Find prowl cost
            import re
            match = re.search(r"prowl [^\(]([^\)]+)", oracle_text)
            if match:
                prowl_cost = match.group(1)
                alt_cost = self.parse_mana_cost(prowl_cost)
                
                # Check if prowl condition is met (dealt combat damage with creature of same type)
                if not context or not context.get("prowl_condition_met", False):
                    return None  # Prowl condition not met
                    
                logging.debug(f"Calculated prowl cost for {card.name}: {prowl_cost}")
                
        elif alt_cost_type == "evoke":
            # Evoke is printed as its own Oracle line.  The legacy pattern
            # consumed the first ``{`` before capturing, turning
            # ``{U/B}{U/B}`` into the malformed ``U/B}{U/B}``.
            import re
            match = re.search(
                r"(?:^|\n)evoke\s+((?:\{[^}]+\})+)",
                oracle_text, re.IGNORECASE)
            if match:
                evoke_cost = match.group(1)
                alt_cost = self.parse_mana_cost(evoke_cost)
                logging.debug(f"Calculated evoke cost for {card.name}: {evoke_cost}")
        
        elif alt_cost_type == "madness":
            # Find madness cost
            import re
            match = re.search(r"madness [^\(]([^\)]+)", oracle_text)
            if match:
                madness_cost = match.group(1)
                alt_cost = self.parse_mana_cost(madness_cost)
                logging.debug(f"Calculated madness cost for {card.name}: {madness_cost}")
                
        elif alt_cost_type == "aftermath":
            # Find aftermath cost (on the second half of the card)
            if "aftermath" in oracle_text:
                import re
                # Look for pattern like "Aftermath   W/U\n" 
                match = re.search(r"aftermath\s+([^\n]+)", oracle_text, re.IGNORECASE)
                if match:
                    aftermath_cost = match.group(1).strip()
                    alt_cost = self.parse_mana_cost(aftermath_cost)
                    logging.debug(f"Calculated aftermath cost for {card.name}: {aftermath_cost}")
                    
        elif alt_cost_type == "emerge":
            # Find emerge cost
            import re
            match = re.search(r"emerge [^\(]([^\)]+)", oracle_text)
            if match:
                emerge_cost = match.group(1)
                alt_cost = self.parse_mana_cost(emerge_cost)
                
                # If sacrificing a creature, reduce cost accordingly
                if context and "sacrificed_creature" in context:
                    sacrifice_id = context["sacrificed_creature"]
                    sacrifice_card = self.game_state._safe_get_card(sacrifice_id)
                    if sacrifice_card and hasattr(sacrifice_card, 'cmc'):
                        emerge_reduction = sacrifice_card.cmc
                        alt_cost["generic"] = max(0, alt_cost["generic"] - emerge_reduction)
                        
                logging.debug(f"Calculated emerge cost for {card.name}: {emerge_cost}")

        elif alt_cost_type == "dash":
            # Find dash cost
            import re
            match = re.search(r"dash [^\(]([^\)]+)", oracle_text)
            if match:
                dash_cost = match.group(1)
                alt_cost = self.parse_mana_cost(dash_cost)
                logging.debug(f"Calculated dash cost for {card.name}: {dash_cost}")
                
        elif alt_cost_type == "cycling":
            # Find cycling cost
            import re
            match = re.search(r"cycling [^\(]([^\)]+)", oracle_text)
            if match:
                cycling_cost = match.group(1)
                alt_cost = self.parse_mana_cost(cycling_cost)
                logging.debug(f"Calculated cycling cost for {card.name}: {cycling_cost}")
        
        return alt_cost
    
    def _get_usable_conditional_mana(self, conditional_mana, context):
        """
        Determine which conditional mana can be used for the current context.
        
        Args:
            conditional_mana: Dictionary of conditional mana
            context: The spell casting context
            
        Returns:
            dict: Dictionary of usable mana by color
        """
        if not context:
            return {}
        
        usable_mana = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        
        # Get the card being cast
        card = context.get('card')
        if not card:
            card_id = context.get('card_id')
            if card_id:
                card = self.game_state._safe_get_card(card_id)
        
        if not card:
            return usable_mana
        
        # Keep affordability, planning, and live payment on one restriction
        # predicate. The former duplicated only a subset of the latter (for
        # example it omitted instant/sorcery restrictions), which made a land
        # produce usable conditional mana that the next affordability check
        # immediately ignored.
        for restriction_key, mana_pool in conditional_mana.items():
            if self._can_use_conditional_mana(restriction_key, context):
                for color, amount in mana_pool.items():
                    usable_mana[color] += amount
        
        return usable_mana

    def can_pay_mana_cost(self, player, cost, context=None, pool_override=None):
        """
        Enhanced method to check if a player can pay a mana cost with all possible effects.
        
        Args:
            player: The player dictionary
            cost: The mana cost to check (card object, string, or parsed dict)
            context: Optional context for special cases
            pool_override: Optional override for player's mana pool
            
        Returns:
            bool: Whether the cost can be paid
        """
        cost_is_precomputed = isinstance(cost, dict)
        try:
            # Handle different input types
            if hasattr(cost, 'mana_cost'):
                # If it's a Card object, use its mana cost
                if not hasattr(cost, 'mana_cost'):
                    return False  # Can't determine cost
                card_id = cost.card_id if hasattr(cost, 'card_id') else None
                parsed_cost = self.parse_mana_cost(cost.mana_cost)
            elif isinstance(cost, str):
                # If it's a string, parse the mana cost
                parsed_cost = self.parse_mana_cost(cost)
                card_id = None
            elif isinstance(cost, dict):
                # If it's already a parsed cost dictionary, use it directly
                parsed_cost = self._normalize_mana_cost(cost)
                card_id = None
            else:
                # Invalid input type
                logging.warning(f"Invalid cost type: {type(cost)}")
                return False

            # Check for alternative costs
            if (not cost_is_precomputed and context
                    and context.get('use_alt_cost')):
                alt_cost_type = context['use_alt_cost']
                card_id = context.get('card_id', card_id)
                if card_id is not None:
                    return self.can_pay_alternative_cost(player, card_id, alt_cost_type, context)

            # If there's a card_id, apply all cost modifiers
            if card_id is not None:
                parsed_cost = self.apply_cost_modifiers(player, parsed_cost, card_id, context)

            probe_player = player
            if pool_override is not None:
                # Preserve the historical override semantics: it replaces the
                # ordinary pool and suppresses restricted pools, while snow
                # permanents remain available just as they did before.
                probe_player = dict(player)
                probe_player["mana_pool"] = dict(pool_override)
                probe_player["phase_restricted_mana"] = {}
                probe_player["conditional_mana"] = {}
                probe_player["snow_mana_pool"] = {}
                probe_player["phase_restricted_snow_mana"] = {}
                probe_player["conditional_snow_mana"] = {}

            # Affordability uses the same global, distinct-source assignment
            # as auto-tap and live payment.  Excluding lands here preserves
            # this method's pool-only contract while still allowing an
            # untapped nonland snow mana permanent to pay {S} directly.
            excluded_sources = {
                permanent_id
                for permanent_id in probe_player.get("battlefield", [])
                if (pool_override is not None
                    or "land" in str(getattr(
                        self.game_state._safe_get_card(permanent_id),
                        "type_line", "") or "").lower())
            }
            return self._plan_mana_payment(
                probe_player, parsed_cost, context,
                exclude_ids=excluded_sources,
                include_lands=False,
            ) is not None
        except Exception as e:
            logging.error(f"Error checking mana payment: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return False  # Assume can't pay on error
        
    def can_pay_mana_cost_with_lands(self, player, cost, context=None,
                                     exclude_ids=None):
        """Affordability check that also counts the player's untapped lands.

        The mask uses this so a spell is castable without the policy having
        to float mana first; pay_mana_cost auto-taps the planned lands so
        mask legality and execution stay consistent. ``exclude_ids`` removes
        battlefield permanents from the land scan — used to test whether a
        cost stays payable after an additional cost returns one of them.
        """
        if (context and context.get('use_alt_cost')
                and not isinstance(cost, dict)):
            return self.can_pay_mana_cost(player, cost, context)
        combined_excludes = set(exclude_ids or ())
        combined_excludes.update(
            self._reserved_payment_permanent_ids(player, context))
        return self._plan_mana_payment(
            player, cost, context, exclude_ids=combined_excludes,
            include_lands=True) is not None

    def can_pay_replacing_cost_with_lands(self, player, card_id, cost,
                                          alt_cost_type, context=None):
        """Price and probe one replacing alternative mana cost exactly once."""
        probe_context = dict(context or {})
        probe_context.update({
            'card_id': card_id,
            'use_alt_cost': alt_cost_type,
        })
        if alt_cost_type == 'impending':
            probe_context['cast_for_impending'] = True
        parsed_cost = (self._normalize_mana_cost(cost)
                       if isinstance(cost, dict)
                       else self.parse_mana_cost(cost))
        final_cost = self.apply_cost_modifiers(
            player, parsed_cost, card_id, probe_context)
        return self.can_pay_mana_cost_with_lands(
            player, final_cost, probe_context)

    def _plan_mana_payment(self, player, cost, context=None,
                           exclude_ids=None, include_lands=True):
        """Assign every mana/life pip to one distinct usable source.

        Pool provenance, lands, direct snow-permanent taps, Phyrexian life,
        hybrid choices, and generic pips all participate in one augmenting-
        path match.  This is the shared feasibility contract for masks,
        auto-tap, and live payment.
        """
        try:
            context = context or {}
            parsed = (self._normalize_mana_cost(cost)
                      if isinstance(cost, dict)
                      else self.parse_mana_cost(cost))
            colors = ('W', 'U', 'B', 'R', 'G', 'C')
            sources = []

            def add_pool_sources(kind, mana_pool, snow_provenance,
                                 restriction_key=None):
                mana_pool = mana_pool or {}
                snow_provenance = snow_provenance or {}
                for color in colors:
                    total = max(0, int(mana_pool.get(color, 0) or 0))
                    snow_count = min(
                        total, max(0, int(
                            snow_provenance.get(color, 0) or 0)))
                    # Put non-snow units first as a useful deterministic
                    # preference; matching can still reassign either unit.
                    for is_snow in (
                            [False] * (total - snow_count)
                            + [True] * snow_count):
                        sources.append({
                            "kind": kind,
                            "color": color,
                            "symbols": {color},
                            "is_snow": is_snow,
                            "restriction_key": restriction_key,
                        })

            add_pool_sources(
                "regular", player.get("mana_pool", {}),
                player.get("snow_mana_pool", {}))
            add_pool_sources(
                "phase", player.get("phase_restricted_mana", {}),
                player.get("phase_restricted_snow_mana", {}))
            for restriction_key, restricted_pool in player.get(
                    "conditional_mana", {}).items():
                if not self._can_use_conditional_mana(
                        restriction_key, context):
                    continue
                add_pool_sources(
                    "conditional", restricted_pool,
                    player.get("conditional_snow_mana", {}).get(
                        restriction_key, {}),
                    restriction_key=restriction_key)

            excluded = set(exclude_ids or ())
            excluded.update(
                self._reserved_payment_permanent_ids(player, context))
            seen_permanent_ids = set(excluded)
            if include_lands:
                land_source_start = len(sources)
                for card_id in player.get("battlefield", []):
                    if (card_id in player.get("tapped_permanents", set())
                            or card_id in seen_permanent_ids):
                        continue
                    card = self.game_state._safe_get_card(card_id)
                    type_line = str(
                        getattr(card, "type_line", "") or "")
                    if card is None or "land" not in type_line.lower():
                        continue
                    options = []
                    for option in self._land_mana_options(player, card):
                        restriction = str(
                            option.get("restriction", "") or "")
                        if restriction:
                            parsed_restriction = \
                                self._parse_mana_restrictions(
                                    restriction.lower())
                            if not parsed_restriction:
                                continue
                            restriction_key = self._get_restriction_key(
                                parsed_restriction)
                            if not self._can_use_conditional_mana(
                                    restriction_key, context):
                                continue
                        options.append(option)
                    if not options:
                        continue
                    options.sort(key=lambda option: int(
                        option.get("damage", 0) or 0))
                    sources.append({
                        "kind": "land", "card_id": card_id,
                        "options": options,
                        "symbols": {
                            option["symbol"] for option in options},
                        "is_snow": "snow" in type_line.lower(),
                    })
                    seen_permanent_ids.add(card_id)
                # Source iteration is the matcher's deterministic preference.
                # Prefer a globally damage-free land before a pain land even
                # when battlefield order lists the pain source first; this
                # preserves life for Phyrexian alternatives.
                sources[land_source_start:] = sorted(
                    sources[land_source_start:],
                    key=lambda source: min(
                        int(option.get("damage", 0) or 0)
                        for option in source["options"]))

            # Nonland snow mana permanents pay {S} directly; they are not
            # general mana sources in this auto-tap/payment surface.
            for card_id in player.get("battlefield", []):
                if (card_id in player.get("tapped_permanents", set())
                        or card_id in seen_permanent_ids):
                    continue
                card = self.game_state._safe_get_card(card_id)
                type_line = str(getattr(card, "type_line", "") or "")
                if (not self._is_snow_mana_permanent(card)
                        or "land" in type_line.lower()):
                    continue
                sources.append({
                    "kind": "snow_permanent", "card_id": card_id,
                    "symbols": set(colors), "is_snow": True,
                })
                seen_permanent_ids.add(card_id)

            for _ in range(max(
                    0, int(player.get("life", 0) or 0) // 2)):
                sources.append({
                    "kind": "phyrexian_life",
                    "symbols": set(colors), "is_snow": False,
                })

            needs = []
            for color in colors:
                for _ in range(max(0, int(parsed.get(color, 0) or 0))):
                    needs.append({"kind": "colored", "symbols": {color}})
            for pair in parsed.get("hybrid", []):
                symbols = {
                    str(symbol).upper() for symbol in pair
                    if str(symbol).upper() in self.mana_symbols}
                if not symbols:
                    return None
                needs.append({"kind": "hybrid", "symbols": symbols})
            for phy_color in parsed.get("phyrexian", []):
                needs.append({
                    "kind": "phyrexian",
                    "symbols": {str(phy_color).upper()},
                })
            for _ in range(max(0, int(parsed.get("snow", 0) or 0))):
                needs.append({"kind": "snow", "symbols": set(colors)})
            generic_needed = max(0, int(parsed.get("generic", 0) or 0))
            if parsed.get("X", 0) and "X" in context:
                generic_needed += (
                    max(0, int(parsed.get("X", 0) or 0))
                    * max(0, int(context.get("X", 0) or 0)))
            for _ in range(generic_needed):
                needs.append({"kind": "generic", "symbols": set(colors)})

            def source_can_pay(source, need):
                if (source["kind"] == "snow_permanent"
                        and need["kind"] != "snow"):
                    return False
                if (source["kind"] == "phyrexian_life"
                        and need["kind"] != "phyrexian"):
                    return False
                if need["kind"] == "snow" and not source["is_snow"]:
                    return False
                return bool(source["symbols"].intersection(
                    need["symbols"]))

            # Min-cost maximum matching.  Ordinary mana costs zero life,
            # Phyrexian-life sources cost two, and a land edge costs the
            # damage of its cheapest compatible output.  Residual edges let a
            # later generic/snow/phy pip globally reassign earlier hybrid
            # choices while also finding a payment within the life budget.
            source_node = 0
            source_offset = 1
            need_offset = source_offset + len(sources)
            sink_node = need_offset + len(needs)
            graph = [[] for _ in range(sink_node + 1)]

            def add_edge(start, end, capacity, edge_cost, metadata=None):
                forward = [end, len(graph[end]), capacity,
                           edge_cost, metadata]
                reverse = [start, len(graph[start]), 0,
                           -edge_cost, None]
                graph[start].append(forward)
                graph[end].append(reverse)

            for source_index in range(len(sources)):
                add_edge(source_node, source_offset + source_index, 1, 0)
            for need_index in range(len(needs)):
                add_edge(need_offset + need_index, sink_node, 1, 0)
            for source_index, source in enumerate(sources):
                for need_index, need in enumerate(needs):
                    if not source_can_pay(source, need):
                        continue
                    option = None
                    edge_cost = 0
                    if source["kind"] == "phyrexian_life":
                        edge_cost = 2
                    elif source["kind"] == "land":
                        compatible_options = [
                            candidate for candidate in source["options"]
                            if candidate["symbol"] in need["symbols"]]
                        if not compatible_options:
                            continue
                        option = min(
                            compatible_options,
                            key=lambda candidate: int(
                                candidate.get("damage", 0) or 0))
                        edge_cost = int(option.get("damage", 0) or 0)
                    add_edge(
                        source_offset + source_index,
                        need_offset + need_index, 1, edge_cost,
                        ("assignment", need_index, option))

            flow = 0
            total_life_cost = 0
            while flow < len(needs):
                infinity = 10 ** 18
                distance = [infinity] * len(graph)
                previous = [None] * len(graph)
                in_queue = [False] * len(graph)
                distance[source_node] = 0
                queue = deque([source_node])
                in_queue[source_node] = True
                while queue:
                    node = queue.popleft()
                    in_queue[node] = False
                    for edge_index, edge in enumerate(graph[node]):
                        target, _, capacity, edge_cost, _ = edge
                        if (capacity <= 0
                                or distance[target]
                                <= distance[node] + edge_cost):
                            continue
                        distance[target] = distance[node] + edge_cost
                        previous[target] = (node, edge_index)
                        if not in_queue[target]:
                            queue.append(target)
                            in_queue[target] = True
                if previous[sink_node] is None:
                    return None
                node = sink_node
                while node != source_node:
                    previous_node, edge_index = previous[node]
                    edge = graph[previous_node][edge_index]
                    edge[2] -= 1
                    graph[node][edge[1]][2] += 1
                    node = previous_node
                flow += 1
                total_life_cost += distance[sink_node]

            if total_life_cost > int(player.get("life", 0) or 0):
                return None

            assignments = []
            land_taps = []
            for source_index, source_data in enumerate(sources):
                source_graph_node = source_offset + source_index
                for edge in graph[source_graph_node]:
                    metadata = edge[4]
                    if (not metadata or metadata[0] != "assignment"
                            or edge[2] != 0):
                        continue
                    _, need_index, option = metadata
                    source = dict(source_data)
                    need = dict(needs[need_index])
                    if source["kind"] == "land":
                        source["option"] = option
                        land_taps.append((source["card_id"], option))
                    assignments.append({"source": source, "need": need})
                    break
            pool_spends = Counter()
            snow_provenance_spends = Counter()
            snow_tap_ids = []
            life_paid = 0
            for assignment in assignments:
                source = assignment["source"]
                if source["kind"] in (
                        "regular", "phase", "conditional"):
                    locator = (
                        source["kind"], source.get("restriction_key"),
                        source["color"])
                    pool_spends[locator] += 1
                    if source.get("is_snow"):
                        snow_provenance_spends[locator] += 1
                elif source["kind"] == "snow_permanent":
                    snow_tap_ids.append(source["card_id"])
                elif source["kind"] == "phyrexian_life":
                    life_paid += 2
            return {
                "parsed_cost": parsed,
                "assignments": assignments,
                "land_taps": land_taps,
                "pool_spends": dict(pool_spends),
                "snow_provenance_spends": dict(
                    snow_provenance_spends),
                "snow_tap_ids": snow_tap_ids,
                "life_paid": life_paid,
            }
        except Exception as error:
            logging.warning("Mana-source planning failed: %s", error)
            return None

    def _plan_auto_tap(self, player, cost, context=None, exclude_ids=None):
        """Return the land portion of the shared global payment plan."""
        plan = self._plan_mana_payment(
            player, cost, context, exclude_ids=exclude_ids,
            include_lands=True)
        return None if plan is None else plan["land_taps"]

    def calculate_cost_reduction(self, player, cost, card_id, context=None):
        """
        Calculate cost reduction effects that apply to a card.
        
        Args:
            player: The player dictionary
            cost: The parsed mana cost dictionary
            card_id: ID of the card being cast
            context: Optional context for special cases
            
        Returns:
            dict: The reduced cost dictionary
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return self._normalize_mana_cost(cost)
        
        reduced_cost = self._normalize_mana_cost(cost)
        target_card_colors = self._spell_colors_for_cast(
            card, context=context)
        
        # Check for cost reduction effects on the battlefield
        for battlefield_id in player["battlefield"]:
            battlefield_card = gs._safe_get_card(battlefield_id)
            if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                continue
                
            oracle_text = self._permanent_rules_text_for_costs(
                battlefield_card).lower()
            
            # Check for generic cost reduction
            if "spells you cast cost" in oracle_text and "less to cast" in oracle_text:
                # Extract amount of reduction
                import re
                match = re.search(r"cost \{(\d+)\} less", oracle_text)
                if match:
                    reduction = int(match.group(1))
                    reduced_cost["generic"] = max(0, reduced_cost["generic"] - reduction)
            
            # Check for color-specific cost reduction
            for color, symbol in zip(['white', 'blue', 'black', 'red', 'green'], ['W', 'U', 'B', 'R', 'G']):
                if f"{color} spells you cast cost" in oracle_text and "less to cast" in oracle_text:
                    # Check if spell is the right color
                    if target_card_colors[list('WUBRG').index(symbol)]:
                        match = re.search(r"cost \{(\d+)\} less", oracle_text)
                        if match:
                            reduction = int(match.group(1))
                            reduced_cost["generic"] = max(0, reduced_cost["generic"] - reduction)
            
            # Check for type-specific cost reduction
            for spell_type in ["creature", "instant", "sorcery", "artifact", "enchantment", "planeswalker"]:
                if f"{spell_type} spells you cast cost" in oracle_text and "less to cast" in oracle_text:
                    if hasattr(card, 'card_types') and spell_type in card.card_types:
                        match = re.search(r"cost \{(\d+)\} less", oracle_text)
                        if match:
                            reduction = int(match.group(1))
                            reduced_cost["generic"] = max(0, reduced_cost["generic"] - reduction)
        
        # Check for commander cost reduction (if applicable)
        if context and context.get("is_commander", False):
            # In Commander format, each time you cast your commander from the command zone,
            # it costs {2} more for each previous time it was cast
            commander_cast_count = context.get("commander_cast_count", 0)
            if commander_cast_count > 0:
                reduced_cost["generic"] += commander_cast_count * 2
        
        # Check for cost reduction based on mechanics
        if card and hasattr(card, 'oracle_text'):
            oracle_text = card.oracle_text.lower()
            
            # Affinity (reduces cost based on number of artifacts you control)
            if "affinity for artifacts" in oracle_text:
                artifacts_count = sum(1 for cid in player["battlefield"] 
                                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                                and 'artifact' in gs._safe_get_card(cid).card_types)
                reduced_cost["generic"] = max(0, reduced_cost["generic"] - artifacts_count)
                
            # Convoke (can tap creatures to pay for spells)
            if "convoke" in oracle_text and context and "convoke_creatures" in context:
                convoke_creatures = context["convoke_creatures"]
                if isinstance(convoke_creatures, list):
                    convoke_amount = len(convoke_creatures)
                    reduced_cost["generic"] = max(0, reduced_cost["generic"] - convoke_amount)
                    
            # Delve (can exile cards from graveyard to pay for spells)
            if "delve" in oracle_text and context and "delve_cards" in context:
                delve_cards = context["delve_cards"]
                if isinstance(delve_cards, list):
                    delve_amount = len(delve_cards)
                    reduced_cost["generic"] = max(0, reduced_cost["generic"] - delve_amount)
                    
            # Improvise (can tap artifacts to help cast spells)
            if "improvise" in oracle_text and context and "improvise_artifacts" in context:
                improvise_artifacts = context["improvise_artifacts"]
                if isinstance(improvise_artifacts, list):
                    improvise_amount = len(improvise_artifacts)
                    reduced_cost["generic"] = max(0, reduced_cost["generic"] - improvise_amount)
        
        # Check for cost increasing effects
        for player_idx, p in enumerate([gs.p1, gs.p2]):
            for battlefield_id in p["battlefield"]:
                battlefield_card = gs._safe_get_card(battlefield_id)
                if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                    continue
                    
                oracle_text = self._permanent_rules_text_for_costs(
                    battlefield_card).lower()
                
                # Tax effects like "Spells cost {1} more to cast"
                if "spells cost" in oracle_text and "more to cast" in oracle_text:
                    match = re.search(r"cost \{(\d+)\} more", oracle_text)
                    if match:
                        increase = int(match.group(1))
                        reduced_cost["generic"] += increase
                
                # Check for specific targeting tax effects
                if context and context.get("targeting_opponent", False) and player_idx != (0 if gs.agent_is_p1 else 1):
                    if "spells your opponents cast that target" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        match = re.search(r"cost \{(\d+)\} more", oracle_text)
                        if match:
                            increase = int(match.group(1))
                            reduced_cost["generic"] += increase
        
        return reduced_cost

    def _pay_generic_mana_with_conditional(self, player, amount, payment, usable_conditional_mana, context):
        """
        Pay generic mana optimally using both regular and conditional mana.
        
        Args:
            player: The player dictionary
            amount: Amount of generic mana to pay
            payment: Payment tracking dictionary
            usable_conditional_mana: Dictionary of usable conditional mana
            context: Spell casting context
            
        Returns:
            int: Remaining unpaid amount (0 if fully paid)
        """
        # First use colorless mana
        colorless_used = min(player["mana_pool"].get('C', 0), amount)
        player["mana_pool"]['C'] -= colorless_used
        amount -= colorless_used
        
        if 'C' not in payment['colors']:
            payment['colors']['C'] = 0
        payment['colors']['C'] += colorless_used

        # Then use conditional colorless mana
        if amount > 0 and usable_conditional_mana.get('C', 0) > 0:
            for restriction_key, mana_pool in player.get("conditional_mana", {}).items():
                if amount <= 0:
                    break
                    
                if self._can_use_conditional_mana(restriction_key, context) and mana_pool.get('C', 0) > 0:
                    conditional_used = min(mana_pool.get('C', 0), amount)
                    mana_pool['C'] -= conditional_used
                    amount -= conditional_used
                    
                    if restriction_key not in payment['conditional']:
                        payment['conditional'][restriction_key] = {}
                        
                    if 'C' not in payment['conditional'][restriction_key]:
                        payment['conditional'][restriction_key]['C'] = 0
                        
                    payment['conditional'][restriction_key]['C'] += conditional_used

        # Dynamically prioritize colors based on mana pool availability
        colors = sorted(['G', 'R', 'B', 'U', 'W'], key=lambda color: player["mana_pool"].get(color, 0))

        for color in colors:
            if amount <= 0:
                break

            available = player["mana_pool"].get(color, 0)
            used = min(available, amount)
            player["mana_pool"][color] -= used
            amount -= used
            
            if color not in payment['colors']:
                payment['colors'][color] = 0
            payment['colors'][color] += used

        # If still need more, use conditional colored mana
        if amount > 0:
            for restriction_key, mana_pool in player.get("conditional_mana", {}).items():
                if amount <= 0:
                    break
                    
                if self._can_use_conditional_mana(restriction_key, context):
                    # Use colored mana in the same order
                    for color in colors:
                        if amount <= 0:
                            break
                            
                        available = mana_pool.get(color, 0)
                        used = min(available, amount)
                        mana_pool[color] -= used
                        amount -= used
                        
                        if restriction_key not in payment['conditional']:
                            payment['conditional'][restriction_key] = {}
                            
                        if color not in payment['conditional'][restriction_key]:
                            payment['conditional'][restriction_key][color] = 0
                            
                        payment['conditional'][restriction_key][color] += used

        return amount
    
    def _pay_two_hybrid_mana(self, player, hybrid_pairs, payment):
        """
        Pay two-hybrid mana costs (e.g., {2/R}) optimally.
        
        Args:
            player: The player dictionary
            hybrid_pairs: List of two-hybrid mana pairs (numeric_part, color_part)
            payment: Payment tracking dictionary
            
        Returns:
            bool: Whether the payment was successful
        """
        for hybrid_pair in hybrid_pairs:
            numeric_part, color_part = hybrid_pair
            numeric_value = int(numeric_part)
            color = color_part.upper()
            
            # Check if player has the colored mana
            if player["mana_pool"].get(color, 0) > 0:
                # Pay with colored mana (often more efficient)
                player["mana_pool"][color] -= 1
                
                if color not in payment['colors']:
                    payment['colors'][color] = 0
                payment['colors'][color] += 1
            else:
                # Pay with generic mana
                remaining = self._pay_generic_mana_with_conditional(player, numeric_value, payment, {}, None)
                
                if remaining > 0:
                    # Couldn't pay the generic part
                    return False
        
        return True
    
    def calculate_cost_increase(self, player, cost, card_id, context=None):
        """
        Enhanced cost increase calculation with targeting support.
        
        Args:
            player: The player dictionary
            cost: The parsed mana cost dictionary
            card_id: ID of the card being cast
            context: Optional context for special cases
            
        Returns:
            dict: The increased cost dictionary
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return self._normalize_mana_cost(cost)
        
        increased_cost = self._normalize_mana_cost(cost)
        opponent = gs.p2 if player == gs.p1 else gs.p1
        target_card_colors = self._spell_colors_for_cast(
            card, context=context)
        
        # Check for cost increasing effects on the battlefield for all players
        for battlefield_player in [gs.p1, gs.p2]:
            is_opponent = (player != battlefield_player)
            
            for battlefield_id in battlefield_player["battlefield"]:
                battlefield_card = gs._safe_get_card(battlefield_id)
                if not battlefield_card or not hasattr(battlefield_card, 'oracle_text'):
                    continue
                    
                oracle_text = self._permanent_rules_text_for_costs(
                    battlefield_card).lower()
                
                # Basic tax effects
                if "spells cost" in oracle_text and "more to cast" in oracle_text:
                    # Extract amount of increase
                    import re
                    match = re.search(r"cost \{(\d+)\} more", oracle_text)
                    if match:
                        increase = int(match.group(1))
                        increased_cost["generic"] += increase
                        logging.debug(f"Applying generic cost increase of {increase} from {battlefield_card.name}")
                
                # Color-specific tax effects
                for color, symbol in zip(['white', 'blue', 'black', 'red', 'green'], ['W', 'U', 'B', 'R', 'G']):
                    if f"{color} spells" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        # Check if spell is the right color
                        if target_card_colors[list('WUBRG').index(symbol)]:
                            match = re.search(r"cost \{(\d+)\} more", oracle_text)
                            if match:
                                increase = int(match.group(1))
                                increased_cost["generic"] += increase
                                logging.debug(f"Applying {color} spell cost increase of {increase} from {battlefield_card.name}")
                
                # Opponent-specific tax effects
                if is_opponent and ("spells your opponents cast" in oracle_text and "cost" in oracle_text and "more" in oracle_text):
                    match = re.search(r"cost \{(\d+)\} more", oracle_text)
                    if match:
                        increase = int(match.group(1))
                        increased_cost["generic"] += increase
                        logging.debug(f"Applying opponent cost increase of {increase} from {battlefield_card.name}")
                
                # Targeting-specific tax effects
                if context and context.get('targets'):
                    targets = context.get('targets', [])
                    
                    # Targeting opponent's creatures
                    if "spells that target" in oracle_text and "you control" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        controller_targets = [
                            tid for tid in targets 
                            if battlefield_player == gs._find_card_controller(tid)
                        ]
                        
                        if controller_targets and is_opponent:
                            match = re.search(r"cost \{(\d+)\} more", oracle_text)
                            if match:
                                increase = int(match.group(1))
                                increased_cost["generic"] += increase
                                logging.debug(f"Applying targeting tax of {increase} for targeting opponent's permanents")
                    
                    # Targeting specific card types
                    for card_type in ["creature", "artifact", "enchantment", "planeswalker", "land"]:
                        if f"spells that target {card_type}" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                            type_targets = [
                                tid for tid in targets 
                                if gs._safe_get_card(tid) and 
                                hasattr(gs._safe_get_card(tid), 'card_types') and 
                                card_type in gs._safe_get_card(tid).card_types
                            ]
                            
                            if type_targets:
                                match = re.search(r"cost \{(\d+)\} more", oracle_text)
                                if match:
                                    increase = int(match.group(1))
                                    increased_cost["generic"] += increase
                                    logging.debug(f"Applying targeting tax of {increase} for targeting {card_type}s")
                
                # Multiple target tax effects
                if context and context.get('targets') and len(context.get('targets', [])) > 1:
                    if "spells with more than one target" in oracle_text and "cost" in oracle_text and "more" in oracle_text:
                        match = re.search(r"cost \{(\d+)\} more", oracle_text)
                        if match:
                            increase = int(match.group(1))
                            increased_cost["generic"] += increase
                            logging.debug(f"Applying multi-target tax of {increase}")
        
        return increased_cost

    def _can_use_conditional_mana(self, restriction_key, context):
        """
        Comprehensive check for whether conditional mana can be used for a given context.
        
        Args:
            restriction_key: The restriction key to check
            context: The spell casting context
            
        Returns:
            bool: Whether the conditional mana can be used
        """
        if not context:
            return False
        
        # Get the card being cast
        card = context.get('card')
        if not card:
            card_id = context.get('card_id')
            if card_id:
                card = self.game_state._safe_get_card(card_id)
        
        if not card:
            return False
        
        # Basic restrictions
        if restriction_key.startswith("cast_only:"):
            target_type = restriction_key[10:].lower()  # Extract the target type and normalize

            # Chosen-type restriction (Cavern of Souls): the card must be a
            # creature OF the recorded type; the generic creature check below
            # must not accept it.
            chosen_type_match = re.search(r"chosen type \((\w+)\)", target_type)
            if chosen_type_match:
                required_subtype = chosen_type_match.group(1).lower()
                return ('creature' in getattr(card, 'card_types', [])
                        and required_subtype in [str(s).lower() for s in getattr(card, 'subtypes', [])])

            # Card type restrictions
            if ("noncreature" in target_type
                    and hasattr(card, 'card_types')
                    and 'creature' not in card.card_types):
                return True
            if ("noncreature" not in target_type
                    and "creature" in target_type
                    and (hasattr(card, 'card_types')
                         and 'creature' in card.card_types)):
                return True
            elif "instant" in target_type and (hasattr(card, 'card_types') and 'instant' in card.card_types):
                return True
            elif "sorcery" in target_type and (hasattr(card, 'card_types') and 'sorcery' in card.card_types):
                return True
            elif "artifact" in target_type and (hasattr(card, 'card_types') and 'artifact' in card.card_types):
                return True
            elif "enchantment" in target_type and (hasattr(card, 'card_types') and 'enchantment' in card.card_types):
                return True
            elif "planeswalker" in target_type and (hasattr(card, 'card_types') and 'planeswalker' in card.card_types):
                return True
                
            # Subtype restrictions
            subtypes = card.subtypes if hasattr(card, 'subtypes') else []
            for subtype in subtypes:
                if subtype.lower() in target_type:
                    return True
                    
            # Color restrictions
            if hasattr(card, 'colors'):
                for i, color in enumerate(['white', 'blue', 'black', 'red', 'green']):
                    if color in target_type and card.colors[i]:
                        return True
                        
            # Cost restrictions
            if "colored" in target_type and hasattr(card, 'mana_cost'):
                cost = self.parse_mana_cost(card.mana_cost)
                if any(cost[c] > 0 for c in ['W', 'U', 'B', 'R', 'G']):
                    return True
                    
            if "colorless" in target_type and hasattr(card, 'mana_cost'):
                cost = self.parse_mana_cost(card.mana_cost)
                if cost['C'] > 0 and all(cost[c] == 0 for c in ['W', 'U', 'B', 'R', 'G']):
                    return True
                    
            # Generic spell type
            if re.fullmatch(r"(?:a |any )?spells?", target_type.strip()):
                return True
                
        elif restriction_key.startswith("spend_only:"):
            target_type = restriction_key[11:].lower()  # Extract the target type
            
            # Ability restrictions
            if "activated abilities" in target_type and context.get('is_ability', False):
                return True
                
            # Specific ability types
            if "activated abilities of creatures" in target_type and context.get('is_ability', False):
                ability_source = context.get('ability_source')
                if ability_source:
                    ability_card = self.game_state._safe_get_card(ability_source)
                    if ability_card and hasattr(ability_card, 'card_types') and 'creature' in ability_card.card_types:
                        return True
                        
            if "activated abilities of artifacts" in target_type and context.get('is_ability', False):
                ability_source = context.get('ability_source')
                if ability_source:
                    ability_card = self.game_state._safe_get_card(ability_source)
                    if ability_card and hasattr(ability_card, 'card_types') and 'artifact' in ability_card.card_types:
                        return True
                        
        # Special case for mana from treasures, which can be spent on anything
        elif restriction_key == "from_treasure":
            return True
            
        # Generic "any" mana
        elif restriction_key == "any_color":
            return True
        
        return False
    
    def _prepare_non_mana_payment(self, player, context):
        """Resolve and validate every non-mana cost without mutating state.

        Composite costs used to validate each component immediately before
        paying it.  A later bad discard/sacrifice index therefore failed only
        after earlier permanents had left the battlefield and their zone-change
        triggers and cleanup had already run.  The subsequent list-based
        "refund" could not recreate that battlefield object.  Keep a concrete
        immutable-by-convention plan so all ordinary validation failures happen
        before the first tap or zone move.
        """
        context = context or {}
        gs = self.game_state

        def cost_list(key, label):
            value = context.get(key, [])
            if value is None:
                return []
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{label} payment failed: Expected a list of choices.")
            return list(value)

        tap_ids = []
        seen_tap_ids = set()
        for choice_key, label, required_type in (
                ("convoke_creatures", "Convoke", "creature"),
                ("improvise_artifacts", "Improvise", "artifact")):
            for identifier in cost_list(choice_key, label):
                perm_id = gs._find_permanent_id(player, identifier)
                if (perm_id is None
                        or perm_id not in player.get("battlefield", [])):
                    raise ValueError(
                        f"{label} payment failed: {identifier} "
                        "(invalid identifier).")
                permanent = gs._safe_get_card(perm_id)
                permanent_types = {
                    str(card_type).lower() for card_type in
                    (getattr(permanent, "card_types", []) or [])}
                if required_type not in permanent_types:
                    raise ValueError(
                        f"{label} payment failed: {perm_id} is not a "
                        f"{required_type}.")
                if perm_id in player.get("tapped_permanents", set()):
                    raise ValueError(
                        f"{label} payment failed: {perm_id} "
                        "(already tapped).")
                if perm_id in seen_tap_ids:
                    raise ValueError(
                        f"{label} payment failed: {perm_id} was selected "
                        "more than once.")
                seen_tap_ids.add(perm_id)
                tap_ids.append(perm_id)

        graveyard = player.get("graveyard", [])
        grave_indices = (
            cost_list("delve_cards", "Delve")
            + cost_list("escape_cards", "Escape")
        )
        if any(not isinstance(idx, int) for idx in grave_indices):
            raise ValueError(
                "Delve/Escape payment failed: Graveyard choices must be indices.")
        if len(set(grave_indices)) != len(grave_indices):
            raise ValueError(
                "Delve/Escape payment failed: A graveyard card was selected "
                "more than once.")
        if any(idx < 0 or idx >= len(graveyard) for idx in grave_indices):
            raise ValueError(
                "Delve/Escape payment failed: Invalid GY indices provided.")
        source_zone = context.get("_payment_source_zone")
        source_index = context.get("_payment_source_index")
        if (source_zone == "graveyard" and isinstance(source_index, int)
                and source_index in grave_indices):
            raise ValueError(
                "Delve/Escape payment cannot exile the spell being cast.")
        exile_choices = [
            (idx, graveyard[idx]) for idx in sorted(grave_indices, reverse=True)
        ]

        sacrifice_ids = []
        seen_sacrifice_choices = set()
        for identifier in cost_list(
                "sacrifice_additional", "Additional Sacrifice"):
            sac_id = gs._find_permanent_id(player, identifier)
            if sac_id is None or sac_id not in player.get("battlefield", []):
                raise ValueError(
                    "Additional Sacrifice payment failed: Invalid/missing "
                    f"permanent {identifier}.")
            # Integer choices are battlefield occurrences, so two different
            # indices may legally represent two physical copies that still
            # share a legacy card ID.  Reject only the same occurrence twice.
            choice_key = (type(identifier), identifier)
            if choice_key in seen_sacrifice_choices:
                raise ValueError(
                    "Additional Sacrifice payment failed: Choice "
                    f"{identifier} was selected more than once.")
            seen_sacrifice_choices.add(choice_key)
            sacrifice_ids.append(sac_id)

        bargain_sacrifice_ids = []
        if context.get("bargained"):
            bargain_id = context.get("bargain_sacrifice_id")
            bargain_card = gs._safe_get_card(bargain_id)
            bargain_types = set(getattr(bargain_card, "card_types", []) or [])
            if (bargain_id is None
                    or bargain_id not in player.get("battlefield", [])
                    or not (bargain_types.intersection(
                        {"artifact", "enchantment"})
                        or bool(getattr(bargain_card, "is_token", False)))):
                raise ValueError(
                    "Bargain payment failed: invalid sacrifice selection.")
            if (Counter(sacrifice_ids)[bargain_id]
                    >= Counter(player.get("battlefield", []))[bargain_id]):
                raise ValueError(
                    "Bargain payment selected a permanent already used for "
                    "another cost.")
            bargain_sacrifice_ids.append(bargain_id)

        return_choices = []
        returned_id = context.get("returned_for_additional_cost")
        if returned_id is not None:
            returned_index = context.get(
                "_returned_for_additional_cost_index")
            if not isinstance(returned_index, int):
                try:
                    returned_index = player.get("battlefield", []).index(
                        returned_id)
                except ValueError:
                    returned_index = None
            if (returned_index is None or returned_index < 0
                    or returned_index >= len(player.get("battlefield", []))
                    or player["battlefield"][returned_index] != returned_id):
                raise ValueError(
                    "Additional Return payment failed: selected permanent "
                    "is no longer on the battlefield.")
            if (returned_id in sacrifice_ids
                    or returned_id in bargain_sacrifice_ids):
                raise ValueError(
                    "Additional Return payment selected a permanent already "
                    "used for another cost.")
            return_choices.append((returned_index, returned_id))

        evidence_choices = []
        if context.get("evidence_collected"):
            evidence_cards = cost_list(
                "evidence_cards", "Collect Evidence")
            raw_evidence_choices = context.get("_evidence_choices", []) or []
            if raw_evidence_choices:
                if (not isinstance(raw_evidence_choices, (list, tuple))
                        or any(
                            not isinstance(choice, (list, tuple))
                            or len(choice) != 2
                            or not isinstance(choice[0], int)
                            for choice in raw_evidence_choices)):
                    raise ValueError(
                        "Collect Evidence payment has malformed choices.")
                evidence_choices = [tuple(choice)
                                    for choice in raw_evidence_choices]
            else:
                # Compatibility for an older staged context: resolve each ID
                # to one unused graveyard occurrence without guessing across
                # the spell source or another graveyard cost.
                used = set(grave_indices)
                if source_zone == "graveyard" and isinstance(source_index, int):
                    used.add(source_index)
                for evidence_id in evidence_cards:
                    evidence_index = next((
                        index for index, card_id in enumerate(graveyard)
                        if index not in used and card_id == evidence_id), None)
                    if evidence_index is None:
                        raise ValueError(
                            "Collect Evidence payment could not resolve an "
                            "exact graveyard occurrence.")
                    used.add(evidence_index)
                    evidence_choices.append((evidence_index, evidence_id))
            evidence_indices = [choice[0] for choice in evidence_choices]
            if (len(set(evidence_indices)) != len(evidence_indices)
                    or set(evidence_indices).intersection(grave_indices)
                    or any(
                        index < 0 or index >= len(graveyard)
                        or graveyard[index] != card_id
                        for index, card_id in evidence_choices)
                    or (source_zone == "graveyard"
                        and isinstance(source_index, int)
                        and source_index in evidence_indices)):
                raise ValueError(
                    "Collect Evidence payment overlaps or no longer matches "
                    "the graveyard.")
            if evidence_cards != [card_id for _, card_id in evidence_choices]:
                raise ValueError(
                    "Collect Evidence card identities diverged from choices.")
            threshold = int(context.get("_evidence_threshold", 0) or 0)
            evidence_value = sum(
                max(0, int(getattr(gs._safe_get_card(card_id), "cmc", 0) or 0))
                for _, card_id in evidence_choices)
            if not evidence_choices or evidence_value < threshold:
                raise ValueError(
                    "Collect Evidence payment does not meet its threshold.")

        hand = player.get("hand", [])

        discard_indices = cost_list(
            "discard_additional", "Additional Discard")
        if any(not isinstance(idx, int) for idx in discard_indices):
            raise ValueError(
                "Additional Discard payment failed: Hand choices must be indices.")
        if len(set(discard_indices)) != len(discard_indices):
            raise ValueError(
                "Additional Discard payment failed: A hand card was selected "
                "more than once.")
        if any(idx < 0 or idx >= len(hand) for idx in discard_indices):
            raise ValueError(
                "Additional Discard payment failed: Invalid hand indices "
                f"{discard_indices}.")
        if (source_zone == "hand" and isinstance(source_index, int)
                and source_index in discard_indices):
            raise ValueError(
                "Additional Discard payment cannot discard the spell being cast.")
        discard_choices = [
            (idx, hand[idx]) for idx in sorted(discard_indices, reverse=True)
        ]

        return {
            "tap_ids": tap_ids,
            "exile_choices": exile_choices,
            "sacrifice_ids": sacrifice_ids,
            "bargain_sacrifice_ids": bargain_sacrifice_ids,
            "return_choices": return_choices,
            "evidence_choices": sorted(
                evidence_choices, key=lambda choice: choice[0], reverse=True),
            "evidence_threshold": int(
                context.get("_evidence_threshold", 0) or 0),
            "discard_choices": discard_choices,
            "source_card_id": context.get("_payment_source_card_id"),
            # Legacy callers could report an Emerge sacrifice that they had
            # already moved before entering the mana system.  Preserve that
            # successful-payment accounting without ever trying to revive it
            # on a failed mana transaction.
            "prepaid_sacrifice_ids": (
                [context["emerge_sacrificed_id"]]
                if context.get("emerge_sacrificed_id") else []),
        }

    def _preflight_non_mana_payment(
            self, checkpoint, player, plan, payment):
        """Run the exact commit on an isolated branch before touching live state."""
        # Clone the *current* branch, after any mana abilities have completed,
        # so one-shot mana replacements and TAPPED-trigger queues match the
        # state the live cost commit will see.  ``checkpoint`` remains the
        # untouched pre-activation rollback donor.
        trial = self.game_state.create_transaction_checkpoint().get("state")
        if trial is None:
            raise RuntimeError("Unable to create payment preflight branch.")

        # GameState clones intentionally share analytics services.  They are
        # not part of rules evaluation and must not observe speculative zone
        # moves or triggers from this branch.
        trial.strategy_memory = None
        trial.stats_tracker = None
        trial.card_memory = None
        if trial.card_evaluator:
            trial.card_evaluator.stats_tracker = None
            trial.card_evaluator.card_memory = None
        if trial.strategic_planner and hasattr(
                trial.strategic_planner, "strategy_memory"):
            trial.strategic_planner.strategy_memory = None

        trial_player = trial.p1 if player is self.game_state.p1 else trial.p2
        trial_payment = copy.deepcopy(payment)
        trial.mana_system._commit_non_mana_payment(
            trial_player, copy.deepcopy(plan), trial_payment)
        return True

    def _commit_non_mana_payment(self, player, plan, payment):
        """Commit a fully validated non-mana payment plan.

        This is deliberately called only after the mana-pool transaction has
        proved it can pay the complete mana cost.  Recheck the resolved plan
        before the first mutation so no ordinary failure remains after a cost
        card changes zones.
        """
        gs = self.game_state
        battlefield = player.get("battlefield", [])
        graveyard = player.get("graveyard", [])
        hand = player.get("hand", [])

        snow_tap_ids = list(payment.get("snow_tapped_sources", []))
        if (len(set(snow_tap_ids)) != len(snow_tap_ids)
                or set(snow_tap_ids).intersection(plan["tap_ids"])
                or any(
                    source_id not in battlefield
                    or source_id in player.get("tapped_permanents", set())
                    for source_id in snow_tap_ids)):
            raise ValueError(
                "Snow-source payment state changed after validation.")

        if any(
                perm_id not in battlefield
                or perm_id in player.get("tapped_permanents", set())
                for perm_id in plan["tap_ids"]):
            raise ValueError(
                "Convoke/Improvise payment state changed after validation.")
        battlefield_counts = Counter(battlefield)
        sacrifice_counts = Counter(
            plan["sacrifice_ids"] + plan.get("bargain_sacrifice_ids", []))
        if any(
                battlefield_counts[sac_id] < count
                for sac_id, count in sacrifice_counts.items()):
            raise ValueError(
                "Additional Sacrifice payment state changed after validation.")
        if any(
                idx < 0 or idx >= len(graveyard)
                or graveyard[idx] != expected_id
                for idx, expected_id in (
                    plan["exile_choices"]
                    + plan.get("evidence_choices", []))):
            raise ValueError(
                "Graveyard-exile payment state changed after validation.")
        if any(
                idx < 0 or idx >= len(battlefield)
                or battlefield[idx] != expected_id
                for idx, expected_id in plan.get("return_choices", [])):
            raise ValueError(
                "Additional Return payment state changed after validation.")
        if any(
                idx < 0 or idx >= len(hand) or hand[idx] != expected_id
                for idx, expected_id in plan["discard_choices"]):
            raise ValueError(
                "Additional Discard payment state changed after validation.")

        tapped_for_cost = []
        for source_id in snow_tap_ids:
            if not hasattr(gs, "tap_permanent") \
                    or not gs.tap_permanent(source_id, player):
                raise RuntimeError(
                    f"Snow commit could not tap {source_id}.")
        for perm_id in plan["tap_ids"]:
            if not hasattr(gs, "tap_permanent") \
                    or not gs.tap_permanent(perm_id, player):
                raise RuntimeError(
                    f"Convoke/Improvise commit could not tap {perm_id}.")
            tapped_for_cost.append(perm_id)
        payment["tapped_creatures"] = tapped_for_cost

        exiled_this_step = []
        grave_exile_operations = [
            (idx, card_id, "cost_exile")
            for idx, card_id in plan["exile_choices"]
        ] + [
            (idx, card_id, "collect_evidence")
            for idx, card_id in plan.get("evidence_choices", [])
        ]
        grave_exile_operations.sort(key=lambda operation: operation[0],
                                    reverse=True)
        for idx, expected_exile_id, exile_cause in grave_exile_operations:
            exile_id = player["graveyard"].pop(idx)
            if exile_id != expected_exile_id:
                raise RuntimeError(
                    "Graveyard-exile commit diverged from its validated plan.")
            exile_context = {"source_id": plan.get("source_card_id")}
            if exile_cause == "collect_evidence":
                exile_context["evidence_threshold"] = plan.get(
                    "evidence_threshold", 0)
            if not gs.move_card(
                    exile_id, player, "graveyard_implicit", player, "exile",
                    cause=exile_cause, context=exile_context):
                raise RuntimeError(
                    f"Graveyard-exile commit could not move {exile_id}.")
            exiled_this_step.append(exile_id)
        if exiled_this_step:
            payment["exiled_cards"] = exiled_this_step

        for return_index, return_id in plan.get("return_choices", []):
            # Mirror fixtures can reuse one numeric ID for both seats.  The
            # selected controller occurrence is authoritative when both
            # original decks contain it; runtime owner metadata handles normal
            # materialized cards.
            owner_key = getattr(gs, "card_instance_owners", {}).get(return_id)
            if owner_key == "p1":
                owner = gs.p1
            elif owner_key == "p2":
                owner = gs.p2
            elif (return_id in getattr(gs, "original_p1_deck", [])
                    and return_id in getattr(gs, "original_p2_deck", [])):
                owner = player
            else:
                owner = (gs._find_card_owner_fallback(return_id)
                         if hasattr(gs, "_find_card_owner_fallback") else None)
                owner = owner or player
            if not gs.move_card(
                    return_id, player, "battlefield", owner, "hand",
                    cause="additional_cost",
                    context={
                        "source_id": plan.get("source_card_id"),
                        "casting_additional_cost": "return_permanent",
                    }):
                raise RuntimeError(
                    f"Additional Return commit could not move {return_id}.")
            payment.setdefault("returned_permanents", []).append(return_id)

        for sac_id in plan["sacrifice_ids"]:
            if not gs.move_card(
                    sac_id, player, "battlefield", player, "graveyard",
                    cause="additional_cost_sacrifice"):
                raise RuntimeError(
                    f"Additional Sacrifice commit could not move {sac_id}.")
            payment["sacrificed_perms"].append(sac_id)

        for bargain_id in plan.get("bargain_sacrifice_ids", []):
            if not gs.move_card(
                    bargain_id, player, "battlefield", player, "graveyard",
                    cause="bargain",
                    context={"source_id": plan.get("source_card_id")}):
                raise RuntimeError(
                    f"Bargain commit could not move {bargain_id}.")
            payment["sacrificed_perms"].append(bargain_id)

        payment["sacrificed_perms"].extend(
            plan.get("prepaid_sacrifice_ids", []))

        for idx, expected_discard_id in plan["discard_choices"]:
            discard_id = player["hand"].pop(idx)
            if discard_id != expected_discard_id:
                player["hand"].insert(idx, discard_id)
                raise RuntimeError(
                    "Additional Discard commit diverged from its validated plan.")
            if not gs.move_card(
                    discard_id, player, "hand_implicit", player, "graveyard",
                    cause="additional_cost_discard"):
                player["hand"].insert(idx, discard_id)
                raise RuntimeError(
                    f"Additional Discard commit could not move {discard_id}.")
            payment["discarded_cards"].append(discard_id)

    def _refund_payment(self, player, payment):
        """Undo early tap attempts from a failed staged payment.

        Args:
            player: The player dictionary
            payment: Payment tracking dictionary
        """
        logging.debug(f"Refunding payment: {payment}")

        # Mana pools and Phyrexian life are calculated on transaction-local
        # state and committed only after non-mana costs succeed.  Zone changes
        # are also committed only after the last recoverable failure and are
        # intentionally never "refunded": moving a card back cannot undo
        # triggers, replacement effects, attachment cleanup, counters, or the
        # permanent's zone-change generation.  Only early tap attempts can
        # need an explicit rollback here.

        auto_tap_snapshot = payment.pop("_auto_tap_snapshot", None)
        if auto_tap_snapshot:
            for key, value in auto_tap_snapshot["player_fields"].items():
                player[key] = copy.deepcopy(value)

        if not payment.get("_snow_source_taps_deferred"):
            for source_id in payment.get('snow_tapped_sources', []):
                if hasattr(self.game_state, 'untap_permanent'):
                    self.game_state.untap_permanent(source_id, player)
                else:
                    player.get('tapped_permanents', set()).discard(source_id)

        # Untap creatures tapped for Convoke
        if payment['tapped_creatures']:
            for creature_id in payment['tapped_creatures']:
                 # Use GameState's untap method if available and safe
                 if hasattr(self.game_state, 'untap_permanent'):
                     self.game_state.untap_permanent(creature_id, player)
                 elif creature_id in player.get("tapped_permanents", set()): # Fallback
                     player["tapped_permanents"].remove(creature_id)

        logging.debug("Payment refund completed.")
        # No need to clean up empty conditional mana here, done after successful payment

    def _cleanup_empty_conditional_mana(self, player):
        """
        Remove empty conditional mana pools.
        
        Args:
            player: The player dictionary
        """
        if "conditional_mana" not in player:
            return
            
        to_remove = []
        
        for restriction_key, mana_pool in player["conditional_mana"].items():
            # Check if this pool is empty
            if all(amount <= 0 for amount in mana_pool.values()):
                to_remove.append(restriction_key)
        
        # Remove empty pools
        for key in to_remove:
            del player["conditional_mana"][key]
            player.get("conditional_snow_mana", {}).pop(key, None)

    def _format_payment_for_logging(self, payment):
        """Format a payment for logging."""
        parts = []
        
        # Add regular mana paid
        for color, count in payment['colors'].items():
            if count > 0:
                parts.append(f"{count} {self.color_names[color]}")
        
        # Add conditional mana paid
        for restriction_key, colors in payment['conditional'].items():
            for color, count in colors.items():
                if count > 0:
                    parts.append(f"{count} restricted {self.color_names[color]} ({restriction_key})")
        
        # Add life paid
        if payment['life'] > 0:
            parts.append(f"{payment['life']} life")
        
        return ", ".join(parts)

    def pay_mana_cost(self, player, cost, context=None):
        """
        Enhanced method to pay a mana cost from a player's mana pool with all effects,
        including handling non-mana costs like tapping creatures or exiling cards based on context.
        Now handles non-mana costs first and includes rollback. (Complete Implementation)
        """
        if context is None: context = {}
        gs = self.game_state

        # Track payment details - EXPANDED
        payment = {
            'colors': defaultdict(int), 'conditional': defaultdict(lambda: defaultdict(int)),
            'phase_restricted': defaultdict(int),
            'life': 0, 'snow': 0,
            'snow_spent_colors': defaultdict(int),
            'snow_tapped_sources': [],
            'tapped_creatures': [], 'exiled_cards': [],
            'sacrificed_perms': [], 'discarded_cards': [],
            'returned_permanents': [],
        }
        card_id = context.get('card_id') # Optional: ID of card being cast/activated

        # --- Determine Final Mana Cost ---
        try:
            if hasattr(cost, 'mana_cost'): # Card Object
                card_obj = cost # Keep reference if needed
                card_id = getattr(cost, 'card_id', card_id) # Get/update card_id
                parsed_cost_base = self.parse_mana_cost(cost.mana_cost)
                final_cost = self.apply_cost_modifiers(player, parsed_cost_base, card_id, context)
            elif isinstance(cost, str): # String Cost
                parsed_cost_base = self.parse_mana_cost(cost)
                final_cost = self.apply_cost_modifiers(player, parsed_cost_base, card_id, context)
            elif isinstance(cost, dict): # Pre-parsed/Modified Cost Dict
                # A dict is the caller's already-calculated final cost. Applying
                # modifiers here again double-discounts cast_spell's result
                # (Domain {2}{W} became {W}, for example).
                final_cost = self._normalize_mana_cost(cost)
            else:
                logging.error(f"Invalid cost type provided to pay_mana_cost: {type(cost)}")
                return False
        except Exception as cost_calc_e:
             logging.error(f"Error calculating final cost: {cost_calc_e}", exc_info=True)
             return False

        # Resolve every non-mana choice before auto-tapping lands or moving a
        # card.  The plan contains exact card IDs/indices from this unchanged
        # state, so a malformed later component cannot force a lossy rollback
        # of an earlier sacrifice, discard, or exile.
        try:
            non_mana_plan = self._prepare_non_mana_payment(player, context)
        except ValueError as non_mana_error:
            logging.warning(f"Failed to validate non-mana costs: {non_mana_error}")
            return False
        except Exception as non_mana_e:
            logging.error(
                f"Error validating non-mana costs: {non_mana_e}",
                exc_info=True)
            return False

        context = dict(context)
        transaction_checkpoint = context.get("_payment_transaction_checkpoint")

        def ensure_transaction_checkpoint():
            nonlocal transaction_checkpoint
            if transaction_checkpoint is None:
                transaction_checkpoint = gs.create_transaction_checkpoint()
            return transaction_checkpoint

        def abort_transaction():
            if transaction_checkpoint is not None:
                gs.restore_transaction_checkpoint(transaction_checkpoint)
            else:
                self._refund_payment(player, payment)

        reserved_permanents = set(non_mana_plan["tap_ids"])
        context["_payment_exclude_tap_ids"] = reserved_permanents

        # --- Affordability Check (Before Paying Anything) ---
        # ``final_cost`` already includes context reductions. Reapplying
        # Convoke/Delve/Improvise here underprices the spell a second time.
        cost_for_check = final_cost.copy()

        # One global assignment chooses exact land options together with every
        # pool/life/snow source.  A valid plan may contain zero land taps; None
        # alone means the cost is unpayable.
        initial_allocation = self._plan_mana_payment(
            player, cost_for_check, context,
            exclude_ids=reserved_permanents, include_lands=True)
        if initial_allocation is None:
            cost_str = self._format_mana_cost_for_logging(final_cost, context.get('X', 0) if 'X' in final_cost else 0)
            card_name_log = getattr(gs._safe_get_card(card_id), 'name', 'spell/ability') if card_id else 'spell/ability'
            logging.warning(f"Cannot afford final cost {cost_str} for {card_name_log}")
            abort_transaction()
            return False
        auto_taps = initial_allocation["land_taps"]
        if auto_taps:
            ensure_transaction_checkpoint()
            snapshot_keys = (
                "mana_pool", "snow_mana_pool", "conditional_mana",
                "conditional_snow_mana", "phase_restricted_mana",
                "phase_restricted_snow_mana", "tapped_permanents",
                "life", "lost_life_this_turn")
            payment["_auto_tap_snapshot"] = {
                "player_fields": {
                    key: copy.deepcopy(player.get(key))
                    for key in snapshot_keys if key in player
                }
            }
            for land_id, option in auto_taps:
                if not self._produce_land_mana_option(
                        player, land_id, option):
                    abort_transaction()
                    return False

        # --- Pay Mana Costs ---
        mana_payment_successful = False
        # Use a mutable copy of the pools for the payment attempt
        current_pool = player["mana_pool"].copy()
        snow_pool = player.get("snow_mana_pool", {}).copy()
        conditional_pool = {k: v.copy() for k, v in player.get("conditional_mana", {}).items()}
        phase_pool = player.get("phase_restricted_mana", {}).copy()
        conditional_snow_pool = {
            key: value.copy() for key, value in player.get(
                "conditional_snow_mana", {}).items()}
        phase_snow_pool = player.get(
            "phase_restricted_snow_mana", {}).copy()

        try:
            # Re-plan after exact land activations, using only the now-floated
            # pools plus direct nonland snow sources.  Mana abilities and their
            # replacements may have produced a different quantity/scope than
            # the initial land source advertised.
            execution_allocation = self._plan_mana_payment(
                player, cost_for_check, context,
                exclude_ids=reserved_permanents, include_lands=False)
            if execution_allocation is None:
                raise ValueError(
                    "Post-activation mana allocation no longer pays cost")

            for assignment in execution_allocation["assignments"]:
                source = assignment["source"]
                need = assignment["need"]
                kind = source["kind"]
                if kind == "phyrexian_life":
                    payment["life"] += 2
                    continue
                if kind == "snow_permanent":
                    payment["snow_tapped_sources"].append(
                        source["card_id"])
                    payment["_snow_source_taps_deferred"] = True
                    continue
                if kind not in ("regular", "phase", "conditional"):
                    raise ValueError(
                        f"Unexpected live allocation source {kind}")

                color = source["color"]
                restriction_key = source.get("restriction_key")
                if kind == "regular":
                    target_pool = current_pool
                    target_snow = snow_pool
                    payment_bucket = payment["colors"]
                elif kind == "phase":
                    target_pool = phase_pool
                    target_snow = phase_snow_pool
                    payment_bucket = payment["phase_restricted"]
                else:
                    target_pool = conditional_pool.get(
                        restriction_key, {})
                    target_snow = conditional_snow_pool.setdefault(
                        restriction_key, {})
                    payment_bucket = payment["conditional"][
                        restriction_key]
                if int(target_pool.get(color, 0) or 0) <= 0:
                    raise ValueError(
                        "Allocated mana unit disappeared before commit")
                target_pool[color] -= 1
                payment_bucket[color] += 1
                if source.get("is_snow"):
                    if int(target_snow.get(color, 0) or 0) <= 0:
                        raise ValueError(
                            "Allocated snow provenance disappeared before commit")
                    target_snow[color] -= 1
                if need["kind"] == "snow":
                    payment["snow_spent_colors"][color] += 1

            payment["snow"] = max(
                0, int(final_cost.get("snow", 0) or 0))
            mana_payment_successful = True

        except ValueError as mana_error:
            logging.error(f"Failed to pay mana costs: {mana_error}")
            abort_transaction()
            return False
        except Exception as mana_e:
             logging.error(f"Error paying mana costs: {mana_e}", exc_info=True)
             abort_transaction()
             return False

        # Mana was paid only against transaction-local pool copies.  Commit
        # the already-validated zone/tap costs now, so a mana calculation
        # failure can never require reconstructing a sacrificed permanent or
        # reversing discard/exile triggers.
        has_live_non_mana_commit = bool(
            non_mana_plan["tap_ids"]
            or non_mana_plan["exile_choices"]
            or non_mana_plan["sacrifice_ids"]
            or non_mana_plan.get("bargain_sacrifice_ids")
            or non_mana_plan.get("return_choices")
            or non_mana_plan.get("evidence_choices")
            or non_mana_plan["discard_choices"]
            or payment.get("snow_tapped_sources"))
        try:
            if has_live_non_mana_commit:
                ensure_transaction_checkpoint()
                self._preflight_non_mana_payment(
                    transaction_checkpoint, player, non_mana_plan, payment)
            self._commit_non_mana_payment(player, non_mana_plan, payment)
        except ValueError as non_mana_error:
            logging.warning(f"Failed to commit non-mana costs: {non_mana_error}")
            abort_transaction()
            return False
        except Exception as non_mana_e:
            logging.critical(
                f"Invariant failure committing validated non-mana costs: "
                f"{non_mana_e}",
                exc_info=True)
            abort_transaction()
            return False


        # --- Finalize Payment ---
        if mana_payment_successful:
            # COMMIT CHANGES TO PLAYER STATE
            player["mana_pool"] = current_pool
            for color in list(snow_pool):
                snow_pool[color] = min(
                    max(0, int(snow_pool.get(color, 0) or 0)),
                    max(0, int(current_pool.get(color, 0) or 0)))
            player["snow_mana_pool"] = snow_pool
            player["conditional_mana"] = conditional_pool
            player["phase_restricted_mana"] = phase_pool
            for color in list(phase_snow_pool):
                phase_snow_pool[color] = min(
                    max(0, int(phase_snow_pool.get(color, 0) or 0)),
                    max(0, int(phase_pool.get(color, 0) or 0)))
            player["phase_restricted_snow_mana"] = phase_snow_pool
            for restriction_key, provenance in conditional_snow_pool.items():
                restricted_pool = conditional_pool.get(restriction_key, {})
                for color in list(provenance):
                    provenance[color] = min(
                        max(0, int(provenance.get(color, 0) or 0)),
                        max(0, int(restricted_pool.get(color, 0) or 0)))
            player["conditional_snow_mana"] = conditional_snow_pool
            if payment['life'] > 0:
                player['life'] -= payment['life']
                logging.debug(
                    f"Paid {payment['life']} life for Phyrexian mana.")

            # Log payment
            cost_str = self._format_mana_cost_for_logging(final_cost, context.get('X', 0) if 'X' in final_cost else 0)
            payment_str = self._format_payment_for_logging(payment)
            card_name_log = getattr(gs._safe_get_card(card_id), 'name', 'spell/ability') if card_id else 'spell/ability'
            logging.debug(f"Paid cost {cost_str} for {card_name_log} with {payment_str}")
            self._cleanup_empty_conditional_mana(player)
            payment.pop("_auto_tap_snapshot", None)
            payment.pop("_snow_source_taps_deferred", None)
            self._last_payment = payment  # exposed via pay_mana_cost_get_details
            # Cavern of Souls rider: stage the result in payment details.  A
            # cast transaction still has to remove the exact spell occurrence
            # from its source zone; mutating the caller's context here leaked
            # ``cant_be_countered`` into a failed retry.
            if any(
                    "can't be countered" in str(key).lower()
                    and any(colors.values())
                    for key, colors in payment.get('conditional', {}).items()):
                payment['grants_cant_be_countered'] = True
            return True
        else:
             # This path might be reached if non-mana costs failed. Rollback handled there.
             logging.warning("pay_mana_cost reached end with mana_payment_successful=False.")
             return False
         
    def pay_mana_cost_get_details(self, player, cost, context=None):
        """Pay a mana cost and return a details dict, or None on failure.

        The details dict contains:
          - 'spent_specific': {color: amount} totals across all pools, suitable
            for refunding via add_mana() if the cast must be rolled back.
          - 'payment': the full internal payment breakdown (colors, conditional,
            phase_restricted, life, snow, tapped/exiled/sacrificed/discarded).
          - 'life_paid' / 'snow_paid': convenience scalars.
        Callers: game_state_stack.cast_spell, ability_types (activated-ability
        mana costs), actions_choices (X payment).
        """
        self._last_payment = None
        if not self.pay_mana_cost(player, cost, context):
            return None
        payment = getattr(self, '_last_payment', None) or {}
        spent = defaultdict(int)
        for color, amt in payment.get('colors', {}).items():
            if amt: spent[color] += amt
        for color, amt in payment.get('phase_restricted', {}).items():
            if amt: spent[color] += amt
        for _restriction, pool_part in payment.get('conditional', {}).items():
            for color, amt in pool_part.items():
                if amt: spent[color] += amt
        return {
            'spent_specific': dict(spent),
            'payment': payment,
            'life_paid': payment.get('life', 0),
            'snow_paid': payment.get('snow', 0),
        }

    def _pay_generic_mana_with_all_pools(self, player, amount, payment, current_pool, phase_pool, conditional_pool, usable_conditional, context):
         """Pay generic mana optimally using all pools."""
         # 1. Regular Colorless
         used = min(amount, current_pool.get('C', 0))
         if used > 0: current_pool['C'] -= used; payment['colors']['C'] += used; amount -= used;
         # 2. Phase Colorless
         used = min(amount, phase_pool.get('C', 0))
         if used > 0: phase_pool['C'] -= used; payment['phase_restricted']['C'] += used; amount -= used;
         # 3. Conditional Colorless
         for r_key, pool_part in conditional_pool.items():
             if amount <= 0: break
             if self._can_use_conditional_mana(r_key, context) and pool_part.get('C', 0) > 0:
                 used = min(amount, pool_part['C'])
                 pool_part['C'] -= used; payment['conditional'][r_key]['C'] += used; amount -= used;

         # Use colored mana pools if needed
         # Prioritize colors with more mana first? Or least valuable? Let's use availability.
         colors = sorted(['W', 'U', 'B', 'R', 'G'], key=lambda c: current_pool.get(c,0) + phase_pool.get(c,0) + usable_conditional.get(c,0), reverse=True)

         for color in colors:
             if amount <= 0: break
             # Use Regular Color
             used = min(amount, current_pool.get(color, 0))
             if used > 0: current_pool[color] -= used; payment['colors'][color] += used; amount -= used;
             if amount <= 0: break
             # Use Phase Color
             used = min(amount, phase_pool.get(color, 0))
             if used > 0: phase_pool[color] -= used; payment['phase_restricted'][color] += used; amount -= used;
             if amount <= 0: break
             # Use Conditional Color
             for r_key, pool_part in conditional_pool.items():
                 if amount <= 0: break
                 if self._can_use_conditional_mana(r_key, context) and pool_part.get(color, 0) > 0:
                     used = min(amount, pool_part[color])
                     pool_part[color] -= used; payment['conditional'][r_key][color] += used; amount -= used;

         return amount # Return remaining unpaid amount
    
    def _pay_generic_mana(self, player, amount, payment_tracker):
        """
        Pay generic mana optimally with a more adaptive color priority.
        """
        # First use colorless mana
        colorless_used = min(player["mana_pool"].get('C', 0), amount)
        player["mana_pool"]['C'] -= colorless_used
        amount -= colorless_used
        payment_tracker['C'] += colorless_used

        # Dynamically prioritize colors based on mana pool availability
        colors = sorted(['G', 'R', 'B', 'U', 'W'], key=lambda color: player["mana_pool"].get(color, 0))

        for color in colors:
            if amount <= 0:
                break

            available = player["mana_pool"].get(color, 0)
            used = min(available, amount)
            player["mana_pool"][color] -= used
            amount -= used
            payment_tracker[color] += used

        return amount
    
    def _plan_standard_hybrid_colors(self, hybrid_pairs, available_mana):
        """Assign one available colored unit to every standard hybrid pip.

        Returns colors in the original pip order, or ``None`` when no complete
        assignment exists. This small backtracking matcher is shared by the
        affordability and live-payment paths so their decisions cannot drift.
        """
        pairs = [tuple(str(color).upper() for color in pair)
                 for pair in hybrid_pairs]
        if not pairs:
            return []
        if any(any(color not in self.mana_symbols for color in pair)
               for pair in pairs):
            return None
        remaining = {
            color: max(0, int(available_mana.get(color, 0) or 0))
            for color in self.mana_symbols
        }
        result = [None] * len(pairs)
        # Most constrained pips first sharply bounds the search while result
        # remains indexed in printed order for deterministic payment tracking.
        order = sorted(
            range(len(pairs)),
            key=lambda index: (
                sum(remaining.get(color, 0) > 0
                    for color in pairs[index]),
                len(pairs[index]), index))

        def assign(position):
            if position >= len(order):
                return True
            pair_index = order[position]
            options = sorted(
                dict.fromkeys(pairs[pair_index]),
                key=lambda color: (-remaining.get(color, 0), color))
            for color in options:
                if remaining.get(color, 0) <= 0:
                    continue
                remaining[color] -= 1
                result[pair_index] = color
                if assign(position + 1):
                    return True
                result[pair_index] = None
                remaining[color] += 1
            return False

        return result if assign(0) else None

    def _pay_hybrid_mana_with_all_pools(self, player, hybrid_pairs, payment, current_pool, phase_pool, conditional_pool, usable_conditional, context):
        """Helper to pay hybrid costs using all available mana pools."""
        standard_hybrid = all(
            all(color in self.mana_symbols for color in pair)
            for pair in hybrid_pairs)
        if standard_hybrid:
            conditional_available = {
                color: sum(
                    restricted_pool.get(color, 0)
                    for restriction_key, restricted_pool
                    in conditional_pool.items()
                    if self._can_use_conditional_mana(
                        restriction_key, context))
                for color in self.mana_symbols
            }
            available = {
                color: (current_pool.get(color, 0)
                        + phase_pool.get(color, 0)
                        + conditional_available.get(color, 0))
                for color in self.mana_symbols
            }
            hybrid_plan = self._plan_standard_hybrid_colors(
                hybrid_pairs, available)
            if hybrid_plan is None:
                return False
            for color in hybrid_plan:
                if current_pool.get(color, 0) > 0:
                    current_pool[color] -= 1
                    payment['colors'][color] += 1
                    continue
                if phase_pool.get(color, 0) > 0:
                    phase_pool[color] -= 1
                    payment['phase_restricted'][color] += 1
                    continue
                paid = False
                for restriction_key, restricted_pool in conditional_pool.items():
                    if (self._can_use_conditional_mana(
                            restriction_key, context)
                            and restricted_pool.get(color, 0) > 0):
                        restricted_pool[color] -= 1
                        payment['conditional'][restriction_key][color] += 1
                        paid = True
                        break
                if not paid:
                    return False
            return True

        # Legacy two-hybrid handling: only the colored alternative is modeled.
        for hybrid_pair in hybrid_pairs:
            paid_hybrid = False
            # Define pool preferences (Regular > Phase > Conditional)
            pool_priority = [
                (current_pool, 'colors'),
                (phase_pool, 'phase_restricted'),
            ]
            # Add conditional pools
            for r_key, r_pool in conditional_pool.items():
                 # Only consider conditional pools usable for this context
                 usable_colors_in_pool = usable_conditional.get(r_key, {})
                 if any(color in usable_colors_in_pool for color in hybrid_pair):
                     pool_priority.append((r_pool, f'conditional.{r_key}'))

            # Try to pay from pools in priority order
            pay_options = sorted(hybrid_pair, key=lambda c: sum(p[0].get(c, 0) for p in pool_priority), reverse=True) # Prefer color with more total mana

            for color in pay_options:
                 if paid_hybrid: break
                 for pool, payment_key in pool_priority:
                     if pool.get(color, 0) > 0:
                          pool[color] -= 1
                          # Track payment correctly
                          if payment_key == 'colors': payment['colors'][color] += 1
                          elif payment_key == 'phase_restricted': payment['phase_restricted'][color] += 1
                          else: # Conditional
                               r_key = payment_key.split('.')[-1]
                               payment['conditional'][r_key][color] += 1
                          paid_hybrid = True
                          break # Paid with this color, move to next hybrid pair

            if not paid_hybrid:
                return False # Failed to pay this hybrid cost
        return True # All hybrid costs paid

    def tap_land_for_mana(self, player, card_id, option_index=None):
        """Activate a land mana ability, exposing its output choice (CR 605)."""
        gs = self.game_state
        if not player or card_id is None:
            return False
        card = gs._safe_get_card(card_id)
        if not card:
            return False
        if card_id not in player.get("battlefield", []):
            return False
        if card_id in player.get("tapped_permanents", set()):
            logging.debug(f"tap_land_for_mana: {getattr(card, 'name', card_id)} is already tapped.")
            return False
        if 'land' not in (getattr(card, 'type_line', '') or '').lower():
            logging.debug(f"tap_land_for_mana: {getattr(card, 'name', card_id)} is not a land.")
            return False

        options = self._land_mana_options(player, card)
        if not options:
            logging.debug(f"tap_land_for_mana: could not determine mana output for {getattr(card, 'name', card_id)}.")
            return False

        if option_index is None and len(options) > 1:
            agent_player = gs.p1 if gs.agent_is_p1 else gs.p2
            if player == agent_player:
                if gs.phase != gs.PHASE_CHOOSE:
                    gs.previous_priority_phase = gs.phase
                gs.phase = gs.PHASE_CHOOSE
                gs.choice_context = {
                    "type": "land_mana",
                    "player": player,
                    "controller": player,
                    "source_id": card_id,
                    "card_id": card_id,
                    "options": options,
                }
                gs.priority_player = player
                gs.priority_pass_count = 0
                return True
            # The current scripted opponent has no choice-policy callback.
            option_index = 0

        if option_index is None:
            option_index = 0
        if not isinstance(option_index, int) or not 0 <= option_index < len(options):
            return False
        return self._produce_land_mana_option(player, card_id, options[option_index])

    def complete_land_mana_choice(self, option_index):
        """Resolve one pending agent choice among a land's mana abilities."""
        gs = self.game_state
        context = getattr(gs, "choice_context", None)
        if not (gs.phase == gs.PHASE_CHOOSE and context
                and context.get("type") == "land_mana"):
            return False
        options = context.get("options", [])
        if not isinstance(option_index, int) or not 0 <= option_index < len(options):
            return False
        player = context.get("player")
        card_id = context.get("card_id")
        card = gs._safe_get_card(card_id)
        if (not card or card_id not in player.get("battlefield", [])
                or card_id in player.get("tapped_permanents", set())):
            return False

        if not self._produce_land_mana_option(player, card_id, options[option_index]):
            return False
        gs.choice_context = None
        if getattr(gs, "previous_priority_phase", None) is not None:
            gs.phase = gs.previous_priority_phase
            gs.previous_priority_phase = None
        else:
            gs.phase = gs.PHASE_PRIORITY
        gs.priority_player = player
        gs.priority_pass_count = 0
        return True

    def _produce_land_mana_option(self, player, card_id, option):
        """Tap the source, add the selected mana, then apply its rider."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card or card_id in player.get("tapped_permanents", set()):
            return False

        # Tap the land (fall back to the tapped set if tap_permanent is unavailable).
        tapped = gs.tap_permanent(card_id, player) if hasattr(gs, 'tap_permanent') else False
        if not tapped:
            player.setdefault("tapped_permanents", set()).add(card_id)

        symbol = option.get("symbol", "")
        mana_string = f"{{{symbol}}}"
        restriction = option.get("restriction", "")
        if restriction:
            mana_string += f". {restriction}"
        self.add_mana_to_pool(
            player,
            mana_string,
            land_context={"card_id": card_id, "source_permanent_id": card_id, "tapped": True},
        )
        damage = int(option.get("damage", 0) or 0)
        if damage > 0:
            gs.damage_player(player, damage, source_id=card_id, is_combat_damage=False)
        logging.debug(
            f"tap_land_for_mana: {getattr(card, 'name', card_id)} tapped for {symbol}"
            f" with {damage} damage."
        )
        return True

    def _land_mana_options(self, player, card):
        """Return legal mana-ability outputs for a land.

        Each option carries the selected symbol plus riders that matter after
        activation. This covers the sample decks' duals, pain lands, Verge
        activation conditions, typed lands, and restricted any-color lands.
        """
        if not card:
            return []
        text = getattr(card, "oracle_text", "") or ""
        options = []

        for line in text.splitlines():
            match = re.search(r"\{t\}\s*:\s*add\s+([^.\n]+)", line, re.IGNORECASE)
            if not match:
                continue
            lower_line = line.lower()
            if ("activate only if you control" in lower_line
                    and not self._land_activation_condition_met(player, lower_line)):
                continue

            output_text = match.group(1)
            symbols = [s.upper() for s in re.findall(
                r"\{([WUBRGC])\}", output_text, re.IGNORECASE
            )]
            if re.search(r"one mana of any colou?r", output_text, re.IGNORECASE):
                symbols = list("WUBRG")
            if not symbols:
                continue

            damage_match = re.search(r"deals\s+(\d+)\s+damage to you", line, re.IGNORECASE)
            damage = int(damage_match.group(1)) if damage_match else 0
            restriction = ""
            restriction_match = re.search(r"(spend this mana only[^.]*\.)", line, re.IGNORECASE)
            if restriction_match:
                restriction = restriction_match.group(1).strip()
                # Cavern of Souls: resolve "the chosen type" to this land's
                # recorded as-enters choice so the pooled restriction stays
                # checkable after the mana leaves the land. No choice yet =>
                # this ability produces nothing usable; skip it.
                if re.search(r"the chosen type", restriction, re.IGNORECASE):
                    chosen = None
                    if player:
                        chosen = player.get("chosen_creature_types", {}).get(
                            getattr(card, 'card_id', None))
                    if not chosen:
                        continue
                    restriction = re.sub(
                        r"the chosen type", f"the chosen type ({chosen})",
                        restriction, flags=re.IGNORECASE)
            for symbol in symbols:
                options.append({
                    "symbol": symbol,
                    "damage": damage,
                    "restriction": restriction,
                })

        if not options:
            basic_symbols = {
                'plains': 'W', 'island': 'U', 'swamp': 'B',
                'mountain': 'R', 'forest': 'G', 'wastes': 'C',
            }
            subtypes = [str(s).lower() for s in (getattr(card, 'subtypes', []) or [])]
            for subtype, symbol in basic_symbols.items():
                if subtype in subtypes:
                    options.append({"symbol": symbol, "damage": 0, "restriction": ""})

        deduped = []
        seen = set()
        for option in options:
            key = (option["symbol"], option["damage"], option["restriction"].lower())
            if key not in seen:
                seen.add(key)
                deduped.append(option)
        return deduped

    def _land_activation_condition_met(self, player, line):
        """Evaluate the basic-land-type condition used by Verge lands."""
        if not player:
            return False
        condition = line.split("activate only if you control", 1)[-1]
        required_types = re.findall(
            r"\b(plains|island|swamp|mountain|forest)\b", condition, re.IGNORECASE
        )
        if not required_types:
            return False
        controlled_types = set()
        for permanent_id in player.get("battlefield", []):
            permanent = self.game_state._safe_get_card(permanent_id)
            if permanent and "land" in (getattr(permanent, "type_line", "") or "").lower():
                controlled_types.update(
                    str(subtype).lower() for subtype in (getattr(permanent, "subtypes", []) or [])
                )
        return any(required.lower() in controlled_types for required in required_types)

    def _land_mana_output(self, card):
        """Compatibility helper returning the first mana option's symbol."""
        options = self._land_mana_options(None, card)
        return options[0]["symbol"] if options else ""

    def add_mana_to_pool(self, player, mana_string, land_context=None, phase_restricted=False):
        """
        Advanced mana addition method that handles complex MTG land mechanics.
        
        Args:
            player: The player dictionary
            mana_string: Mana production string
            land_context: Additional context about land entry conditions
            phase_restricted: Whether this mana only lasts until end of phase
        
        Returns:
            dict: Detailed mana addition information
        """
        # Initialize result tracking
        result = {
            'added': {},
            'skipped': [],
            'conditions': {},
            'logs': []
        }
        
        # Validate and normalize input
        if not isinstance(mana_string, str):
            result['logs'].append(f"Invalid mana string type: {type(mana_string)}")
            return result
            
        # Skip empty strings
        if not mana_string:
            return result

        source_id = None
        if isinstance(land_context, dict):
            source_id = land_context.get('source_permanent_id')
            if source_id is None:
                source_id = land_context.get('card_id')
        source_card = (self.game_state._safe_get_card(source_id)
                       if source_id is not None else None)
        source_is_snow = bool(
            source_card
            and 'snow' in (getattr(source_card, 'type_line', '') or '').lower())
        
        # --- PRODUCE_MANA replacements (July 2026 triage fix) ---
        # Mana-doubling replacements registered against a 'PRODUCE_MANA' event
        # that nothing ever fired: they were dead code. Fire it here, at the
        # single entry point for produced mana, and translate any increase
        # back into extra mana symbols so the untouched downstream machinery
        # (restrictions, phase pools, conditional pools) handles them normally.
        re_sys = getattr(self.game_state, 'replacement_effects', None)
        if re_sys is not None and land_context is not None:
            try:
                _symbols = re.findall(r'\{([^{}]+)\}', mana_string.lower())
                _produced = {}
                for _s in _symbols:
                    _key = _s.upper()
                    _produced[_key] = _produced.get(_key, 0) + 1
                if _produced:
                    _ctx = {
                        'event_type': 'PRODUCE_MANA',
                        'player': player,
                        'player_key': (
                            'p1' if player is self.game_state.p1 else 'p2'),
                        'source_is_tap_ability': bool(land_context.get('tapped', True)) if isinstance(land_context, dict) else True,
                        'source_permanent_id': source_id,
                        'source_card_types': list(
                            getattr(source_card, 'card_types', []) or []),
                        'source_subtypes': list(
                            getattr(source_card, 'subtypes', []) or []),
                        'mana_produced': dict(_produced),
                    }
                    _modified, _was_replaced = re_sys.apply_replacements('PRODUCE_MANA', _ctx)
                    if _was_replaced:
                        _new = _modified.get('mana_produced', _produced) or {}
                        _extra = []
                        for _color, _count in _new.items():
                            _delta = int(_count) - _produced.get(_color, 0)
                            if _delta > 0:
                                _extra.append(('{%s}' % _color.lower()) * _delta)
                        if _extra:
                            mana_string = mana_string + ''.join(_extra)
                            result['logs'].append(f"PRODUCE_MANA replacement increased production: {_produced} -> {_new}")
            except Exception as _e:
                logging.error(f"Error applying PRODUCE_MANA replacements: {_e}")
        
        # Initialize conditional_mana if not exists
        if "conditional_mana" not in player:
            player["conditional_mana"] = {}
        if "conditional_snow_mana" not in player:
            player["conditional_snow_mana"] = {}
        
        # Initialize phase-restricted mana if needed
        if "phase_restricted_mana" not in player:
            player["phase_restricted_mana"] = {}
        if "phase_restricted_snow_mana" not in player:
            player["phase_restricted_snow_mana"] = {}
        
        # Step 1: Separate land conditions from mana text
        # Most land condition text appears before any mana symbols
        mana_parts = mana_string.split('.')
        condition_text = ""
        mana_text = ""
        
        for part in mana_parts:
            part = part.strip()
            # If this part contains mana symbols like {t}, {w}, etc.
            if re.search(r'\{[^{}]+\}', part):
                mana_text += part + " "
            else:
                condition_text += part + " "
        
        # Clean up the separated texts
        condition_text = condition_text.strip().lower()
        mana_text = mana_text.strip().lower()
        
        # Parse usage restrictions from the text
        restrictions = self._parse_mana_restrictions(condition_text + " " + mana_text)
        
        # Step 2: Extract mana symbols
        mana_tokens = re.findall(r'\{([^{}]+)\}', mana_text)
        
        # Process each token
        for token in mana_tokens:
            # Skip empty tokens
            if not token.strip():
                continue
                
            # Special case for tap symbol - explicitly check for 't' or 'T'
            if token.strip().lower() == 't':
                result['logs'].append("Tap symbol recognized (not a mana symbol)")
                continue
            
            # Process mana symbols
            clean_token = token.strip().lower()
            
            # Standard mana colors
            if clean_token in ['w', 'u', 'b', 'r', 'g', 'c']:
                color = clean_token.upper()
                
                # Add to regular mana pool if no restrictions
                if not restrictions:
                    if phase_restricted:
                        # Store in phase-restricted pool
                        if color not in player["phase_restricted_mana"]:
                            player["phase_restricted_mana"][color] = 0
                        player["phase_restricted_mana"][color] += 1
                    else:
                        # Add to normal mana pool
                        player["mana_pool"][color] = player["mana_pool"].get(color, 0) + 1
                        
                    if color not in result['added']:
                        result['added'][color] = 0
                    result['added'][color] += 1
                    
                    scope = "phase-restricted" if phase_restricted else "normal"
                    result['logs'].append(f"Added {color} mana to {scope} pool")
                else:
                    # Add to conditional mana pool
                    restriction_key = self._get_restriction_key(restrictions)
                    if restriction_key not in player["conditional_mana"]:
                        player["conditional_mana"][restriction_key] = {}
                    
                    if color not in player["conditional_mana"][restriction_key]:
                        player["conditional_mana"][restriction_key][color] = 0
                    
                    player["conditional_mana"][restriction_key][color] += 1
                    
                    if color not in result['added']:
                        result['added'][color] = 0
                    result['added'][color] += 1
                    
                    result['logs'].append(f"Added {color} mana with restriction: {restriction_key}")
                
            # Generic mana
            elif clean_token.isdigit():
                # Generic mana is always colorless
                amount = int(clean_token)
                
                # Add to regular mana pool if no restrictions
                if not restrictions:
                    if phase_restricted:
                        # Store in phase-restricted pool
                        if 'C' not in player["phase_restricted_mana"]:
                            player["phase_restricted_mana"]['C'] = 0
                        player["phase_restricted_mana"]['C'] += amount
                    else:
                        # Add to normal mana pool
                        player["mana_pool"]['C'] = player["mana_pool"].get('C', 0) + amount
                        
                    if 'C' not in result['added']:
                        result['added']['C'] = 0
                    result['added']['C'] += amount
                    
                    scope = "phase-restricted" if phase_restricted else "normal"
                    result['logs'].append(f"Added {amount} colorless mana to {scope} pool")
                else:
                    # Add to conditional mana pool
                    restriction_key = self._get_restriction_key(restrictions)
                    if restriction_key not in player["conditional_mana"]:
                        player["conditional_mana"][restriction_key] = {}
                    
                    if 'C' not in player["conditional_mana"][restriction_key]:
                        player["conditional_mana"][restriction_key]['C'] = 0
                    
                    player["conditional_mana"][restriction_key]['C'] += amount
                    
                    if 'C' not in result['added']:
                        result['added']['C'] = 0
                    result['added']['C'] += amount
                    
                    result['logs'].append(f"Added {amount} colorless mana with restriction: {restriction_key}")
                
            # Process other token types...
            
        # Add land conditions to result
        result['conditions'] = self._parse_land_conditions(mana_tokens)
        result['logs'].append(f"Processed land conditions: {result['conditions']}")
        if source_is_snow:
            if restrictions:
                restriction_key = self._get_restriction_key(restrictions)
                provenance = player.setdefault(
                    "conditional_snow_mana", {}).setdefault(
                        restriction_key, {})
            elif phase_restricted:
                provenance = player.setdefault(
                    "phase_restricted_snow_mana", {})
            else:
                provenance = player.setdefault(
                    "snow_mana_pool",
                    {symbol: 0 for symbol in self.mana_symbols})
            for color, amount in result['added'].items():
                provenance[color] = provenance.get(color, 0) + int(amount or 0)
        
        return result

    def remove_mana_from_pool(self, player, amount, color='C'):
        """
        Remove mana from a player's mana pool.
        
        Args:
            player: The player dictionary
            amount: Amount of mana to remove
            color: Color of mana to remove ('W', 'U', 'B', 'R', 'G', 'C')
            
        Returns:
            int: Amount of mana actually removed
        """
        if color not in player["mana_pool"]:
            return 0
            
        available = player["mana_pool"][color]
        removed = min(available, amount)
        player["mana_pool"][color] -= removed
        
        return removed

    def clear_phase_restricted_mana(self, player):
        """
        Clear phase-restricted mana at the end of a phase.
        
        Args:
            player: The player dictionary
        """
        if "phase_restricted_mana" in player:
            # Log the mana being cleared
            for color, amount in player["phase_restricted_mana"].items():
                if amount > 0:
                    logging.debug(f"Clearing {amount} phase-restricted {color} mana from {player['name']}'s pool")
            
            # Clear the phase-restricted mana
            player["phase_restricted_mana"] = {}
            player["phase_restricted_snow_mana"] = {}
            
    def can_pay_alternative_cost(self, player, card_id, cost_type, context=None):
        """
        Check if a player can pay an alternative cost.
        
        Args:
            player: The player dictionary
            card_id: ID of the card with alternative cost
            cost_type: Type of alternative cost ('flashback', 'escape', etc.)
            context: Additional cost context
        
        Returns:
            bool: Whether the alternative cost can be paid
        """
        # Get the alternative cost
        alt_cost = self.calculate_alternative_cost(card_id, player, cost_type, context)
        if not alt_cost:
            return False
        
        # Check if the alternative cost can be paid
        return self.can_pay_mana_cost(player, alt_cost, context)

    def pay_alternative_cost(self, player, card_id, cost_type, context=None):
        """
        Pay an alternative cost for a card.
        
        Args:
            player: The player dictionary
            card_id: ID of the card with alternative cost
            cost_type: Type of alternative cost ('flashback', 'escape', etc.)
            context: Additional cost context
        
        Returns:
            bool: Whether the cost was successfully paid
        """
        # Get the alternative cost
        alt_cost = self.calculate_alternative_cost(card_id, player, cost_type, context)
        if not alt_cost:
            return False
        
        # Pay the alternative cost
        return self.pay_mana_cost(player, alt_cost, context)

    def _parse_mana_restrictions(self, text):
        """
        Parse restrictions on how mana can be spent.
        
        Args:
            text: Text to parse for restrictions
            
        Returns:
            dict: Dictionary of restrictions
        """
        restrictions = {}
        
        # Common patterns for mana restrictions
        if "spend this mana only to cast" in text:
            # Extract what the mana can be spent on
            import re
            match = re.search(r"spend this mana only to cast ([^\.]+)", text.lower())
            if match:
                target_type = match.group(1).strip()
                restrictions['cast_only'] = target_type
        
        elif "spend this mana only on" in text:
            # Extract what the mana can be spent on
            import re
            match = re.search(r"spend this mana only on ([^\.]+)", text.lower())
            if match:
                target_type = match.group(1).strip()
                restrictions['spend_only'] = target_type
        
        # Add more restriction patterns as needed
        
        return restrictions

    def _get_restriction_key(self, restrictions):
        """
        Create a string key representing the restrictions.
        
        Args:
            restrictions: Dictionary of restrictions
            
        Returns:
            str: A string key for the restrictions
        """
        if 'cast_only' in restrictions:
            return f"cast_only:{restrictions['cast_only']}"
        elif 'spend_only' in restrictions:
            return f"spend_only:{restrictions['spend_only']}"
        
        # Fallback
        return "restricted"

    def _parse_land_conditions(self, tokens):
        """
        Parse complex land entry conditions from tokens.
        
        Args:
            tokens: List of tokens to parse
        
        Returns:
            dict: Parsed land conditions
        """
        conditions = {
            'tapped': False,
            'untapped': True,
            'other_lands': None,
            'land_count_condition': None,
            'timing_restrictions': [],
            'additional_requirements': []
        }
        
        # Number word to integer mapping
        number_map = {
            'zero': 0, 'one': 1, 'two': 2, 'three': 3, 
            'four': 4, 'five': 5, 'six': 6, 'seven': 7
        }
        
        for token in tokens:
            clean_token = token.strip().lower()
            
            # Tapped condition
            if clean_token in ['t', 'tapped']:
                conditions['tapped'] = True
                conditions['untapped'] = False
            
            # Conditional land entry parsing
            if 'unless' in clean_token:
                # Parse land count conditions
                land_count_match = re.search(
                    r'unless.*?control\s*(\w+)\s*or\s*(\w+)\s*other\s*lands', 
                    clean_token
                )
                if land_count_match:
                    number_word = land_count_match.group(1)
                    comparison = land_count_match.group(2)
                    
                    # Convert number word to integer
                    land_count = number_map.get(number_word.lower(), 0)
                    
                    conditions['other_lands'] = {
                        'comparison': comparison,
                        'count': land_count
                    }
            
            # Timing restrictions
            if 'during' in clean_token or 'only' in clean_token:
                conditions['timing_restrictions'].append(clean_token)
        
        return conditions


    def _is_condition_token(self, token):
        """
        Determine if a token is a condition token.
        
        Args:
            token: Token to check
        
        Returns:
            bool: Whether the token is a condition
        """
        condition_keywords = {
            't', 'tapped', 'unless', 'during', 'only', 
            'enters', 'control', 'land', 'other', 
            'this land enters', 'land enters', 'thislandenterstapped'  # Added the combined form
        }

        # Check if token contains any condition keyword
        if any(keyword in token.lower() for keyword in condition_keywords):
            return True
        
        # Also check for specific patterns
        if 'enters' in token.lower() and 'tapped' in token.lower():
            return True
            
        return False

    def _process_mana_token(self, player, token, land_conditions):
        """
        Process a single mana token with advanced parsing.
        
        Args:
            player: Player dictionary
            token: Mana token to process
            land_conditions: Parsed land conditions
        
        Returns:
            dict: Processing result
        """
        result = {
            'added': Counter(),
            'skipped': [],
            'logs': []
        }
        
        # Clean and normalize the token
        clean_token = re.sub(r'[.,;]', '', token.lower().strip())
        
        # FIXED: Enhanced handling of tap symbol
        if clean_token == 't' or clean_token == 'tap':
            result['logs'].append("Tap symbol encountered, not a mana symbol")
            return result  # Return immediately, don't process as mana
        
        # Color name aliases
        color_aliases = {
            'white': 'w', 'blue': 'u', 'black': 'b', 
            'red': 'r', 'green': 'g', 'colorless': 'c'
        }
        
        # Apply color alias
        if clean_token in color_aliases:
            clean_token = color_aliases[clean_token]
        
        # Basic mana colors
        if clean_token in ['w', 'u', 'b', 'r', 'g', 'c']:
            # Check land entry conditions
            can_add_mana = self._check_land_entry_conditions(player, land_conditions)
            
            if can_add_mana:
                color = clean_token.upper()
                player["mana_pool"][color] += 1
                result['added'][color] += 1
                result['logs'].append(f"Added {color} mana")
            else:
                result['skipped'].append(clean_token)
                result['logs'].append(f"Mana production blocked: {land_conditions}")
            
            return result
        
        # Generic mana
        if clean_token.isdigit():
            player["mana_pool"]['C'] += int(clean_token)
            result['added']['C'] += int(clean_token)
            result['logs'].append(f"Added {clean_token} colorless mana")
            return result
        
        # Hybrid and complex mana
        if '/' in clean_token:
            hybrid_result = self._process_hybrid_mana(player, clean_token, land_conditions)
            result.update(hybrid_result)
            return result
        
        # Unrecognized token
        result['skipped'].append(token)
        result['logs'].append(f"Unrecognized mana token: {token}")
        
        return result

    def _process_hybrid_mana(self, player, token, land_conditions):
        """
        Process hybrid mana tokens.
        
        Args:
            player: Player dictionary
            token: Hybrid mana token
            land_conditions: Parsed land conditions
        
        Returns:
            dict: Processing result
        """
        result = {
            'added': Counter(),
            'skipped': [],
            'logs': []
        }
        
        parts = token.split('/')
        
        # Normalize parts
        norm_parts = [p.upper() for p in parts]
        
        # Check if both parts are valid mana symbols
        if all(p in self.mana_symbols for p in norm_parts):
            # Choose the color with more available mana
            best_color = max(norm_parts, key=lambda c: player["mana_pool"].get(c, 0))
            
            # Check land conditions
            if self._check_land_entry_conditions(player, land_conditions):
                player["mana_pool"][best_color] += 1
                result['added'][best_color] += 1
                result['logs'].append(f"Added hybrid mana: {best_color}")
            else:
                result['skipped'].append(token)
                result['logs'].append(f"Hybrid mana blocked by conditions: {land_conditions}")
        
        # Phyrexian mana handling
        elif 'P' in parts:
            phyrexian_color = next((p for p in parts if p.upper() in self.mana_symbols), None)
            if phyrexian_color:
                color = phyrexian_color.upper()
                
                # Check land conditions and life payment
                if (self._check_land_entry_conditions(player, land_conditions) and 
                    self._can_pay_phyrexian_cost(player)):
                    player["mana_pool"][color] += 1
                    result['added'][color] += 1
                    result['logs'].append(f"Added Phyrexian mana: {color}")
                else:
                    result['skipped'].append(token)
                    result['logs'].append(f"Phyrexian mana blocked")
        
        return result

    def _check_land_entry_conditions(self, player, conditions):
        """
        Comprehensive check of land entry conditions.
        
        Args:
            player: Player dictionary
            conditions: Parsed land conditions
        
        Returns:
            bool: Whether mana can be produced
        """
        gs = self.game_state
        
        # Tapped condition
        if conditions.get('tapped', False):
            return False
        
        # Other lands condition
        if conditions.get('other_lands'):
            current_lands = [
                cid for cid in player.get('battlefield', []) 
                if gs._safe_get_card(cid) and 
                hasattr(gs._safe_get_card(cid), 'type_line') and 
                'land' in gs._safe_get_card(cid).type_line
            ]
            
            condition = conditions['other_lands']
            count = condition['count']
            comparison = condition['comparison']
            
            if comparison == 'fewer':
                return len(current_lands) <= count
            elif comparison == 'more':
                return len(current_lands) >= count
        
        # Timing restrictions
        if conditions.get('timing_restrictions'):
            # Additional checks can be added here based on game state
            pass
        
        return True

    def _can_pay_phyrexian_cost(self, player):
        """
        Check if player can pay Phyrexian mana cost.
        
        Args:
            player: Player dictionary
        
        Returns:
            bool: Whether Phyrexian mana can be paid
        """
        return player.get('life', 0) >= 2

    def _log_mana_addition(self, result):
        """
        Log detailed information about mana addition.
        
        Args:
            result: Mana addition result dictionary
        """
        # Detailed logging
        if result['added']:
            added_details = [
                f"{count} {self.color_names.get(color, color)}" 
                for color, count in result['added'].items()
            ]
            logging.debug(f"Mana pool addition: {', '.join(added_details)}")
        
        # Log skipped tokens and conditions
        if result['skipped']:
            logging.info(f"Skipped mana tokens: {result['skipped']}")
        
        # Additional logging for complex conditions
        if result['conditions']:
            logging.debug(f"Land conditions: {result['conditions']}")
        
        # Log any detailed messages
        for log_entry in result['logs']:
            logging.debug(log_entry)
    
    def _determine_best_mana_color(self, player):
        """
        Determine the most needed mana color based on cards in hand.
        
        Args:
            player: The player dictionary
            
        Returns:
            str: Best color to add ('W', 'U', 'B', 'R', or 'G')
        """
        gs = self.game_state
        
        # Count required mana by color
        color_needs = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}
        
        for card_id in player["hand"]:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'mana_cost'):
                continue
                
            cost = self.parse_mana_cost(card.mana_cost)
            
            # Add colored requirements
            for color in ['W', 'U', 'B', 'R', 'G']:
                color_needs[color] += cost[color]
            
            # Add hybrid requirements (split evenly)
            for hybrid_pair in cost['hybrid']:
                for color in hybrid_pair:
                    if color in color_needs:
                        color_needs[color] += 1 / len(hybrid_pair)
            
            # Add phyrexian requirements
            for phyrexian_color in cost['phyrexian']:
                if phyrexian_color in color_needs:
                    color_needs[phyrexian_color] += 0.5  # Lower weight than direct requirements
        
        # Adjust needs based on current mana pool
        for color in color_needs:
            color_needs[color] -= player["mana_pool"].get(color, 0)
        
        # Find the color with highest need
        best_color = max(color_needs.items(), key=lambda x: x[1])[0]
        
        # If all needs are 0 or negative, default to most common color in deck
        if color_needs[best_color] <= 0:
            best_color = self._get_primary_deck_color(player)
        
        return best_color
    
    def _get_primary_deck_color(self, player):
        """
        Determine the primary color of the player's deck.
        
        Args:
            player: The player dictionary
            
        Returns:
            str: Primary color ('W', 'U', 'B', 'R', or 'G')
        """
        gs = self.game_state
        color_counts = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}
        
        # Look at all zones to determine color breakdown
        for zone in ["library", "hand", "battlefield", "graveyard"]:
            for card_id in player[zone]:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'colors'):
                    continue
                    
                for i, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                    if card.colors[i]:
                        color_counts[color] += 1
        
        # Return the most common color
        return max(color_counts.items(), key=lambda x: x[1])[0]
    
    def get_card_color_identity(self, card):
        """
        Determine the color identity of a card.
        
        Args:
            card: The card object
            
        Returns:
            set: Set of color characters in the card's color identity
        """
        if not card:
            return set()
            
        color_identity = set()
        
        # Add colors from the color indicator/array
        if hasattr(card, 'colors'):
            for i, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                if card.colors[i]:
                    color_identity.add(color)
        
        # Add colors from mana cost
        if hasattr(card, 'mana_cost'):
            cost = self.parse_mana_cost(card.mana_cost)
            
            # Add basic colored mana
            for color in ['W', 'U', 'B', 'R', 'G']:
                if cost[color] > 0:
                    color_identity.add(color)
            
            # Add hybrid mana colors
            for hybrid_pair in cost['hybrid']:
                for color in hybrid_pair:
                    if color in ['W', 'U', 'B', 'R', 'G']:
                        color_identity.add(color)
            
            # Add phyrexian mana colors
            for phyrexian_color in cost['phyrexian']:
                if phyrexian_color in ['W', 'U', 'B', 'R', 'G']:
                    color_identity.add(phyrexian_color)
        
        # Add colors from oracle text (mana symbols)
        if hasattr(card, 'oracle_text'):
            for color in ['W', 'U', 'B', 'R', 'G']:
                if f"{{{color}}}" in card.oracle_text:
                    color_identity.add(color)
        
        return color_identity
    
    def get_deck_color_identity(self, deck, card_db):
        """
        Determine the color identity of a deck.
        
        Args:
            deck: List of card IDs
            card_db: Card database
            
        Returns:
            set: Color identity of the deck
        """
        color_identity = set()
        
        for card_id in deck:
            card = card_db.get(card_id)
            if card:
                card_colors = self.get_card_color_identity(card)
                color_identity.update(card_colors)
        
        return color_identity
    
    def format_color_identity(self, color_identity):
        """
        Format a color identity set as a human-readable string.
        
        Args:
            color_identity: Set of color characters
            
        Returns:
            str: Formatted color identity
        """
        if not color_identity:
            return "Colorless"
            
        # Standard color order
        color_order = ['W', 'U', 'B', 'R', 'G']
        
        # Sort colors in standard order
        sorted_colors = [c for c in color_order if c in color_identity]
        
        # Map to common color combinations
        color_combinations = {
            'W': "Mono-White",
            'U': "Mono-Blue",
            'B': "Mono-Black",
            'R': "Mono-Red",
            'G': "Mono-Green",
            'WU': "Azorius",
            'WB': "Orzhov",
            'UB': "Dimir",
            'UR': "Izzet",
            'BR': "Rakdos",
            'BG': "Golgari",
            'RG': "Gruul",
            'RW': "Boros",
            'GW': "Selesnya",
            'GU': "Simic",
            'WUB': "Esper",
            'UBR': "Grixis",
            'BRG': "Jund",
            'RGW': "Naya",
            'GWU': "Bant",
            'WBG': "Abzan",
            'URW': "Jeskai",
            'BRW': "Mardu",
            'GUB': "Sultai",
            'RGU': "Temur",
            'WUBR': "Non-Green",
            'UBRG': "Non-White",
            'BRGW': "Non-Blue",
            'RGWU': "Non-Black",
            'GWUB': "Non-Red",
            'WUBRG': "Five-Color"
        }
        
        color_key = ''.join(sorted_colors)
        return color_combinations.get(color_key, f"{len(color_key)}-Color")
    
    def get_mana_curve(self, deck, card_db):
        """
        Calculate the mana curve of a deck.
        
        Args:
            deck: List of card IDs
            card_db: Card database
            
        Returns:
            dict: Mana curve counts by CMC
        """
        mana_curve = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, "7+": 0}
        
        for card_id in deck:
            card = card_db.get(card_id)
            if not card or not hasattr(card, 'cmc') or ('land' in card.card_types if hasattr(card, 'card_types') else False):
                continue
                
            cmc = card.cmc
            
            if cmc <= 6:
                mana_curve[cmc] += 1
            else:
                mana_curve["7+"] += 1
        
        return mana_curve
    
    def _format_mana_cost_for_logging(self, parsed_cost, x_value=0):
        """Format a parsed mana cost for logging."""
        parsed_cost = self._normalize_mana_cost(parsed_cost)
        parts = []
        
        # Add generic mana
        if parsed_cost['generic'] > 0:
            parts.append(f"{parsed_cost['generic']} generic")
        
        # Add X cost
        if parsed_cost['X'] > 0:
            parts.append(f"X={x_value}")
        
        # Add colored mana
        for color in ['W', 'U', 'B', 'R', 'G', 'C']:
            if parsed_cost[color] > 0:
                parts.append(f"{parsed_cost[color]} {self.color_names[color]}")
        
        # Add hybrid mana
        for hybrid_pair in parsed_cost['hybrid']:
            parts.append(f"1 hybrid ({'/'.join(hybrid_pair)})")
        
        # Add phyrexian mana
        for phyrexian_color in parsed_cost['phyrexian']:
            parts.append(f"1 phyrexian {self.color_names[phyrexian_color]}")
        
        return ", ".join(parts)
