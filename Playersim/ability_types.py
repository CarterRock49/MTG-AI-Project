import logging
import re
import random
import copy
from .card import Card
from .ability_utils import text_to_number, safe_int, resolve_simple_targeting, EffectFactory


def _permanent_matches_criteria(game_state, card_id, criteria,
                                controller=None, source_id=None):
    """Match common Oracle permanent characteristics, failing closed."""
    card = game_state._safe_get_card(card_id)
    if not card:
        return False
    text = str(criteria or "permanent").lower().strip(" .,;")
    words = set(re.findall(r"[a-z]+", text))
    types = {str(value).lower() for value in getattr(card, "card_types", [])}
    subtypes = {str(value).lower() for value in getattr(card, "subtypes", [])}
    supertypes = {str(value).lower() for value in getattr(card, "supertypes", [])}
    controller = controller or game_state.get_card_controller(card_id)
    if "another" in words and card_id == source_id:
        return False
    if "nonland" in words and "land" in types:
        return False
    if "noncreature" in words and "creature" in types:
        return False
    if "nonartifact" in words and "artifact" in types:
        return False
    if "nontoken" in words and getattr(card, "is_token", False):
        return False
    if ("token" in words and "nontoken" not in words
            and not getattr(card, "is_token", False)):
        return False
    if "nonlegendary" in words and "legendary" in supertypes:
        return False
    if ("legendary" in words and "nonlegendary" not in words
            and "legendary" not in supertypes):
        return False
    if "nonbasic" in words and "basic" in supertypes:
        return False
    if ("basic" in words and "nonbasic" not in words
            and "basic" not in supertypes):
        return False
    tapped = card_id in (controller or {}).get("tapped_permanents", set())
    if "tapped" in words and "untapped" not in words and not tapped:
        return False
    if "untapped" in words and tapped:
        return False
    if "attacking" in words and card_id not in getattr(
            game_state, "current_attackers", []):
        return False
    blockers = {
        blocker for values in getattr(
            game_state, "current_block_assignments", {}).values()
        for blocker in values}
    if "blocking" in words and card_id not in blockers:
        return False

    name_match = re.search(r"\bnamed\s+(.+?)(?:\s+with\b|$)", text)
    if name_match and str(getattr(card, "name", "")).lower() \
            != name_match.group(1).strip():
        return False

    for comparison in re.finditer(
            r"(mana value|power|toughness)\s+(?:is\s+)?(\d+)"
            r"(?:\s+or\s+(less|greater))?", text):
        field, raw_value, direction = comparison.groups()
        actual = (getattr(card, "cmc", 0) if field == "mana value"
                  else getattr(card, field, 0))
        try:
            actual, bound = int(actual or 0), int(raw_value)
        except (TypeError, ValueError):
            return False
        if ((direction == "less" and actual > bound)
                or (direction == "greater" and actual < bound)
                or (direction is None and actual != bound)):
            return False
    mana_value = int(getattr(card, "cmc", 0) or 0)
    if "odd mana value" in text and mana_value % 2 != 1:
        return False
    if "even mana value" in text and mana_value % 2 != 0:
        return False

    colors = getattr(card, "colors", []) or []
    if (isinstance(colors, (list, tuple)) and len(colors) == 5
            and all(isinstance(value, (int, float, bool)) for value in colors)):
        present_colors = {
            symbol for symbol, present in zip("WUBRG", colors) if present}
    elif isinstance(colors, dict):
        present_colors = {
            str(symbol).upper() for symbol, present in colors.items() if present}
    else:
        present_colors = {str(value).upper() for value in colors}
    color_map = {"white": "W", "blue": "U", "black": "B",
                 "red": "R", "green": "G"}
    requested_colors = {
        symbol for word, symbol in color_map.items() if word in words}
    if requested_colors:
        color_names = "|".join(color_map)
        color_disjunction = bool(re.search(
            rf"\b(?:{color_names})\s+or\s+(?:{color_names})\b", text))
        if ((color_disjunction
             and not requested_colors.intersection(present_colors))
                or (not color_disjunction
                    and not requested_colors.issubset(present_colors))):
            return False
    if "colorless" in words and present_colors:
        return False
    if "multicolored" in words and len(present_colors) < 2:
        return False
    if "monocolored" in words and len(present_colors) != 1:
        return False

    counters = getattr(card, "counters", {}) or {}
    positive_counters = {
        str(kind).lower(): int(amount or 0)
        for kind, amount in counters.items() if int(amount or 0) > 0}
    if "with no counters" in text and positive_counters:
        return False
    if "with a counter" in text and not positive_counters:
        return False
    counter_match = re.search(
        r"(?:with|and) (?:an?|one or more) ([+\-/\w]+) counters?", text)
    if counter_match and positive_counters.get(counter_match.group(1), 0) <= 0:
        return False
    without_counter = re.search(r"without (?:an? )?([+\-/\w]+) counters?", text)
    if without_counter and positive_counters.get(without_counter.group(1), 0) > 0:
        return False

    matched_keywords = set()
    for keyword in sorted(Card.ALL_KEYWORDS, key=len, reverse=True):
        if re.search(rf"\bwith\s+(?:[^,;]+\s+and\s+)?{re.escape(keyword)}\b", text):
            if not game_state.check_keyword(card_id, keyword):
                return False
            matched_keywords.update(re.findall(r"[a-z]+", keyword))
        if re.search(rf"\bwithout\s+{re.escape(keyword)}\b", text):
            if game_state.check_keyword(card_id, keyword):
                return False
            matched_keywords.update(re.findall(r"[a-z]+", keyword))

    required_types = {
        word.rstrip("s") for word in words
        if word.rstrip("s") in set(Card.ALL_CARD_TYPES)}
    if required_types:
        type_names = "|".join(re.escape(value) for value in required_types)
        type_disjunction = bool(re.search(
            rf"\b(?:{type_names})s?\s+or\s+(?:{type_names})s?\b", text))
        if type_disjunction:
            if not required_types.intersection(types):
                return False
        elif not required_types.issubset(types):
            return False

    grammar = {
        "a", "an", "another", "one", "two", "three", "four", "five",
        "six", "seven", "eight", "nine", "ten", "x", "or", "and",
        "other", "this", "it", "the", "of", "that", "its", "their",
        "permanent", "permanents", "token",
        "tokens", "nontoken", "nonland", "noncreature", "nonartifact",
        "tapped", "untapped", "you", "control", "controls", "with",
        "without", "no", "counter", "counters", "mana", "value", "is",
        "power", "toughness", "less", "greater", "odd", "even", "named",
        "white", "blue", "black", "red", "green", "colorless",
        "multicolored", "monocolored", "legendary", "nonlegendary",
        "basic", "nonbasic", "attacking", "blocking", "greatest", "least",
        "among", "creatures", "one", "more",
    } | required_types | matched_keywords
    if name_match:
        grammar.update(re.findall(r"[a-z]+", name_match.group(1)))
    if counter_match:
        grammar.update(re.findall(r"[a-z]+", counter_match.group(1)))
    if without_counter:
        grammar.update(re.findall(r"[a-z]+", without_counter.group(1)))
    unknown = {
        word.rstrip("s") for word in words
        if not word.isdigit() and word not in grammar
        and word.rstrip("s") not in grammar
        and word.rstrip("s") not in subtypes
        and word.rstrip("s") not in supertypes}
    if unknown:
        return False
    subtype_terms = {
        word.rstrip("s") for word in words
        if word.rstrip("s") in subtypes}
    return not subtype_terms or subtype_terms.issubset(subtypes)


class Ability:
    """Base class for card abilities"""
    def __init__(self, card_id, effect_text=""):
        self.card_id = card_id
        self.effect_text = effect_text
        self.source_card = None # Add a reference to the card object i

    def can_trigger(self, event, context):
        """Check if this ability should trigger"""
        return False
    def resolve(self, game_state, controller):
        """Resolve the ability's effect with improved error handling and target validation."""
        card = game_state._safe_get_card(self.card_id)
        if not card:
            logging.warning(f"Cannot resolve ability: card {self.card_id} not found")
            return False

        try:
            # Check if ability requires targeting based on its effect text
            text_to_check = getattr(self, 'effect', getattr(self, 'effect_text', ''))
            requires_target = "target" in text_to_check.lower() # Basic check
            targets_resolved = {} # Targets resolved for this instance

            if requires_target:
                targets_resolved = self._handle_targeting(game_state, controller)
                # Validate targets just before resolution (they might have become invalid)
                # Targets should be in a structured dict {cat:[id,...]} by now if resolved properly
                if not game_state._validate_targets_on_resolution(self.card_id, controller, targets_resolved):
                    logging.info(f"Ability {self.effect_text} fizzled: Targets became invalid before resolution.")
                    return False # Fizzle (counts as resolved successfully technically)

            # Delegate to specific implementation, passing resolved targets
            return self._resolve_ability_implementation(game_state, controller, targets_resolved)

        except Exception as e:
            logging.error(f"Error resolving ability ({type(self).__name__}): {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return False

    def _handle_targeting(self, game_state, controller):
            """
            Handle targeting for this ability by using TargetingSystem if available.

            Args:
                game_state: The game state
                controller: The player controlling the ability

            Returns:
                dict: Dictionary of targets for this ability
            """
            # Prefer GameState's targeting system instance first
            if hasattr(game_state, 'targeting_system') and game_state.targeting_system:
                # Pass the correct effect text (prefer self.effect if exists)
                text_for_targeting = getattr(self, 'effect', self.effect_text)
                return game_state.targeting_system.resolve_targeting(
                    self.card_id, controller, text_for_targeting)

            # Check AbilityHandler's targeting system as a secondary option
            elif hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system') and game_state.ability_handler.targeting_system:
                text_for_targeting = getattr(self, 'effect', self.effect_text)
                # Method name might be different here, use the specific one if known
                # Assuming resolve_targeting_for_ability exists
                if hasattr(game_state.ability_handler.targeting_system, 'resolve_targeting_for_ability'):
                    return game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
                        self.card_id, text_for_targeting, controller)
                # Fallback if method name differs or is resolve_targeting
                elif hasattr(game_state.ability_handler.targeting_system, 'resolve_targeting'):
                    return game_state.ability_handler.targeting_system.resolve_targeting(
                        self.card_id, controller, text_for_targeting)


            # Fall back to simple targeting if no system instance found
            text_for_targeting = getattr(self, 'effect', self.effect_text)
            logging.warning(f"TargetingSystem instance not found on GameState or AbilityHandler. Falling back to simple targeting for {self.card_id}")
            return self._resolve_simple_targeting(game_state, controller, text_for_targeting)

    def _resolve_ability_implementation(self, game_state, controller, targets=None,
                                        resolution_context=None):
        """Ability-specific implementation of resolution. Uses EffectFactory and handles sequences."""
        effect_text_to_use = getattr(self, 'effect', getattr(self, 'effect_text', None))
        if not effect_text_to_use:
            logging.error(f"Cannot resolve triggered ability implementation for {self.card_id}: Missing effect text.")
            return False

        # Special handling for specific sequenced keywords like Living Weapon
        if getattr(self, 'keyword', None) == 'living weapon':
            # Sequence: Create Germ, then Attach
            logging.debug(f"Resolving Living Weapon for {self.card_id}")
            # 1. Create Germ Token
            germ_token_data = {"name": "Phyrexian Germ", "power": 0, "toughness": 0, "card_types":["creature"], "subtypes":["Phyrexian", "Germ"], "colors":[0,0,1,0,0]} # Black
            created_token_id = None
            if hasattr(game_state, 'create_token'):
                created_token_id = game_state.create_token(controller, germ_token_data)
            else: # Fallback
                token_id = f"TOKEN_Germ_{random.randint(1000,9999)}"
                germ_token_data['is_token'] = True
                new_token = Card(germ_token_data)
                new_token.card_id = token_id
                game_state.card_db[token_id] = new_token
                controller.setdefault("tokens",[]).append(token_id)
                controller["battlefield"].append(token_id)
                created_token_id = token_id
                game_state.trigger_ability(created_token_id, "ENTERS_BATTLEFIELD", {"controller": controller})

            # 2. Attach Equipment (self.card_id) to the token
            if created_token_id:
                if hasattr(game_state, 'equip_permanent'):
                     # No cost associated with Living Weapon attachment
                     if game_state.equip_permanent(controller, self.card_id, created_token_id, bypass_cost=True):
                          logging.debug(f"Living Weapon: Attached {self.card_id} to Germ token {created_token_id}.")
                          return True
                     else:
                          logging.warning(f"Living Weapon: Failed to attach {self.card_id} to Germ token {created_token_id}.")
                          return False # Attachment failed
                else:
                     logging.warning("Living Weapon: GameState missing 'equip_permanent' method.")
                     return False
            else:
                logging.warning(f"Living Weapon: Failed to create Germ token for {self.card_id}.")
                return False # Token creation failed

        # Default: Use EffectFactory for other triggers
        # Exact-card effect overrides need the source name at real resolution,
        # not only when callers invoke EffectFactory directly.  Omitting it
        # made linked effects such as Caustic Bronco fragment into generic
        # partial no-ops during matches while their parser tests still passed.
        source_card = (getattr(self, "source_card", None)
                       or game_state._safe_get_card(self.card_id))
        source_name = getattr(source_card, "name", None)
        if not targets and re.search(r"\bon it\b", effect_text_to_use, re.IGNORECASE):
            for player in (game_state.p1, game_state.p2):
                attached_to = player.get("attachments", {}).get(self.card_id)
                if attached_to is not None:
                    targets = {"creatures": [attached_to]}
                    break
        effects = self._create_ability_effects(
            effect_text_to_use, targets, source_name=source_name)
        if not effects:
            logging.warning(f"No effects created for triggered ability: {effect_text_to_use}")
            return False

        success = True
        if hasattr(game_state, '_run_effect_sequence'):
            success, _ = game_state._run_effect_sequence(
                effects, self.card_id, controller, targets,
                context=resolution_context)
            return success
        for effect_obj in effects:
            if not effect_obj.apply(
                    game_state, self.card_id, controller, targets,
                    context=resolution_context):
                success = False
        return success


    def _create_ability_effects(self, effect_text, targets=None,
                                source_name=None):
        """Create appropriate AbilityEffect objects based on the effect text"""
        return EffectFactory.create_effects(
            effect_text, targets, source_name=source_name)

    def _resolve_simple_targeting(self, game_state, controller, effect_text):
        """Simplified targeting resolution when targeting system isn't available"""
        return resolve_simple_targeting(game_state, self.card_id, controller, effect_text)

    def __str__(self):
        return f"Ability({self.effect_text})"


class ActivatedAbility(Ability):
    """Ability that can be activated by paying a cost"""
    def __init__(self, card_id, cost=None, effect=None, effect_text="", is_exhaust=False, activation_index=None):
        # Ensure base __init__ gets original effect_text if available
        super().__init__(card_id, effect_text or f"{cost or ''}: {effect or ''}".strip(': '))

        # Determine cost and effect using parsing if not explicitly provided
        parsed_cost, parsed_effect = None, None
        if cost is None and effect is None and self.effect_text:
            # *** CHANGED: Use the updated, stricter parser ***
            parsed_cost, parsed_effect = self._parse_cost_effect_strict(self.effect_text) # Use stricter parser

        # Prioritize provided args, then parsed values, then empty string
        self.cost = str(cost) if cost is not None else (str(parsed_cost) if parsed_cost is not None else "")
        self.effect = str(effect) if effect is not None else (str(parsed_effect) if parsed_effect is not None else "")

        # --- Exhaust Handling: If cost starts with 'Exhaust', set flag and strip ---
        self.is_exhaust = is_exhaust # Initialize with passed flag
        # --- CHANGED: Check cost *after* assigning it ---
        if self.cost.lower().startswith("exhaust"):
            self.is_exhaust = True
            # Remove "Exhaust" and separator (comma or dash)
            self.cost = re.sub(r"^\s*Exhaust\s*[,—\u2014-]?\s*", "", self.cost, flags=re.IGNORECASE).strip()

        self.activation_index = activation_index

        # --- Validation: Only raise error if parsing failed *despite* finding separator ---
        # Implicit cost keywords are handled by _parse_cost_effect_strict
        if not self.cost and not self.effect:
             # If parsing failed completely (returned None, None) and the original text
             # *looked* like it should have been activated (had ':'), raise the error.
             if parsed_cost is None and parsed_effect is None and ":" in self.effect_text:
                  # *** This error indicates a potential parsing issue for seemingly valid activated text ***
                  # Keep this as an error because it points to a problem needing fixing.
                  raise ValueError(f"Failed to parse cost/effect from text with separator: '{self.effect_text}'")
             # Otherwise, it likely wasn't an activated ability text, which is fine (no error/warning needed here).

        # Reconstruct effect_text only if parsing was successful and changed something
        if parsed_cost is not None or parsed_effect is not None:
             prefix = "Exhaust, " if self.is_exhaust else ""
             reconstructed_text = f"{prefix}{self.cost}: {self.effect}".strip(': ')
             if reconstructed_text != self.effect_text:
                 self.effect_text = reconstructed_text

    @staticmethod
    def _parse_cost_effect_strict(text):
        """
        Strict parser for Activated Abilities. Requires a valid cost indicator
        followed by a colon or whitespace-delimited dash, OR a known
        keyword-cost pattern.
        Includes warning if separator found but cost pattern not matched.
        Returns (cost_str, effect_str) or (None, None).
        """
        if not text: return None, None
        text = text.strip()

        # 1. Check for explicit separators. An ASCII hyphen inside a word or
        # modifier (``non-Faerie``, ``-3/-3``) is not a separator. Colons may
        # be tight; dash variants must be surrounded by whitespace.
        separator_match = re.match(
            r'^\s*(.+?)(\s*:\s*|\s+(?:–|—|\u2013|\u2014|-)\s+)(.+)\s*$',
            text, re.DOTALL)

        if separator_match:
            cost_part = separator_match.group(1).strip()
            separator = separator_match.group(2)
            effect_part = separator_match.group(3).strip().rstrip('.') # Clean effect

            # Validate cost part: Does it contain known cost elements?
            cost_indicators_pattern = r'\{[WUBRGCXSPMTQ0-9\/\.]+\}|\(\{T\}\)|\b(Tap|Sacrifice|Discard|Pay\s+\d+\s+life|Remove\s+.*?\s+counter|Exhaust|Cycling|Equip|Flashback|Level\s+up)\b|^\s*\d+\s*$'
            if re.search(cost_indicators_pattern, cost_part, re.IGNORECASE):
                logging.debug(f"_parse_cost_effect_strict: Parsed Separator Cost='{cost_part}', Effect='{effect_part}'")
                return cost_part, effect_part
            else:
                # Named abilities can put an ability word before the actual
                # activation (``Mental Organism -- Pay 3 life: ...``). The
                # first dash is descriptive punctuation, not the cost/effect
                # separator. Retry only a suffix that independently parses as
                # a complete activated ability.
                if ":" not in separator:
                    named_cost, named_effect = (
                        ActivatedAbility._parse_cost_effect_strict(effect_part))
                    if named_cost is not None and named_effect is not None:
                        return named_cost, named_effect
                # A colon strongly signals a malformed activation. A dash is
                # also ordinary ability-word punctuation (Valiant —, Eerie —)
                # and should not flood every reset with warnings.
                log = logging.warning if ":" in separator else logging.debug
                log(f"_parse_cost_effect_strict: Found separator in '{text}', but left side '{cost_part}' not recognized as standard cost. Treating as non-activated.")
                return None, None # Return None, None as it's not a clearly parsed activated ability

        # 2. Check for Keyword-Only Structures (No explicit separator)
        # (Keep existing keyword pattern logic)
        keyword_cost_patterns = {
            r"^\s*(Cycling|Equip|Fortify|Reconfigure|Unearth|Flashback|Bestow|Dash|Buyback|Madness|Transmute|Channel|Kicker|Entwine|Overload|Splice|Surge|Embalm|Eternalize|Jump-start|Escape|Awaken|Level up|Retrace|Ninjutsu)\s*(\{.*?\})": "kw_explicit_cost",
            r"^\s*(Cycling|Equip|Fortify|Reconfigure|Unearth|Flashback|Bestow|Dash|Buyback|Madness|Transmute|Channel|Kicker|Entwine|Overload|Splice|Surge|Embalm|Eternalize|Jump-start|Escape|Awaken|Level up|Retrace|Ninjutsu)\s*(\d+)": "kw_digit_cost", # Handle plain number cost
             r"^\s*(Outlast|Monstrosity|Adapt|Reinforce|Scavenge|Crew)\s*(\{?\d+\}?)": "kw_numeric_value", # {N} or N
             r"^\s*(Morph)\s*(\{.*?\})?": "kw_optional_cost", # Optional cost for Morph
             r"^\s*(Boast)\b": "kw_no_cost_parsed",
        }
        for pattern, pattern_type in keyword_cost_patterns.items():
            match = re.match(pattern + r'\s*\.?$', text, re.IGNORECASE | re.DOTALL) # Ensure pattern consumes whole string (approx)
            if match:
                keyword = match.group(1)
                cost_str = "{0}"
                if pattern_type == "kw_explicit_cost" and len(match.groups()) > 1:
                     cost_str = match.group(2)
                elif pattern_type == "kw_digit_cost" and len(match.groups()) > 1:
                     cost_str = f"{{{match.group(2)}}}"
                elif pattern_type == "kw_numeric_value" and len(match.groups()) > 1:
                     cost_str = "{0}"
                elif pattern_type == "kw_optional_cost" and len(match.groups()) > 1 and match.group(2):
                     cost_str = match.group(2)
                elif pattern_type == "kw_no_cost_parsed":
                    cost_str = "{0}"

                effect_map = {
                    "cycling": "Draw a card.", "equip": "Attach to target creature.",
                    "flashback": "Cast from graveyard, then exile.", "level up": "Put a level counter on this.",
                    "morph": "Turn this face up.", "boast": "Activate boast effect.",
                }
                effect_part = effect_map.get(keyword.lower(), f"Activate {keyword} ability.")
                logging.debug(f"_parse_cost_effect_strict: Parsed Keyword Activation: Keyword='{keyword}', Cost='{cost_str}', Effect='{effect_part}'")
                return cost_str, effect_part

        # 3. No Standard Pattern Found
        return None, None

    def resolve(self, game_state, controller, targets=None):
        """Resolve this activated ability using the default implementation."""
        # Overriding resolve allows specific subclasses (like ManaAbility) to change behavior.
        # This calls the default Ability._resolve_ability_implementation.
        return super()._resolve_ability_implementation(game_state, controller, targets)


    def resolve_with_targets(self, game_state, controller, targets=None,
                             context=None):
        """Resolve this ability with specific targets."""
        # This method is useful if the activation logic needs to pass pre-selected targets.
        # Default implementation calls the main resolve logic.
        return self._resolve_ability_implementation(
            game_state, controller, targets, resolution_context=context)


    @staticmethod
    def _word_to_number(value):
        """Convert the number words used in costs to a positive count."""
        normalized = str(value or "").lower().strip()
        if normalized in {"a", "an", "another"}:
            return 1
        return max(1, text_to_number(normalized))

    @classmethod
    def _resolve_cost_count(cls, value, context=None, default=1):
        normalized = str(value or "").lower().strip()
        if normalized == "x":
            return max(0, int((context or {}).get(
                "activation_X", (context or {}).get("X", 0)) or 0))
        if not normalized:
            return default
        return cls._word_to_number(normalized)

    @staticmethod
    def _is_self_sacrifice_requirement(requirement):
        req_lower = str(requirement or "").lower().strip(" .,;")
        return (
            req_lower in {"it", "this", "this permanent", "this creature",
                          "this artifact", "this enchantment", "this land"}
            or req_lower.startswith("this ")
        )

    def get_sacrifice_cost_spec(self):
        """Return the parsed sacrifice component of this activation cost.

        The result is intentionally reusable by the action layer: it can expose
        a non-self permanent choice before ``pay_cost`` commits the transaction.
        """
        cost_lower = str(self.cost or "").lower()
        match = re.search(
            r"\bsacrifice\s+(.+?)(?=,\s*(?:\{|discard\b|pay\b|remove\b|tap\b|untap\b)|$)",
            cost_lower)
        if not match:
            return None
        requirement = match.group(1).strip(" .,;")
        count_match = re.match(
            r"^(a|an|another|one|two|three|four|five|six|seven|eight|nine|ten|x|\d+)\b",
            requirement)
        count_expr = count_match.group(1) if count_match else "one"
        return {
            "count": count_expr,
            "requirement": requirement,
            "self_sacrifice": self._is_self_sacrifice_requirement(requirement),
        }

    def get_discard_cost_spec(self, context=None):
        match = re.search(
            r"\bdiscard\s+(?:(a|an|one|two|three|four|five|x|\d+)\s+)?"
            r"(?:(\w+)\s+)?cards?(?:\s+at random)?",
            str(self.cost or "").lower())
        if not match:
            return None
        count_expr = match.group(1) or "one"
        return {
            "count": self._resolve_cost_count(count_expr, context),
            "count_expr": count_expr,
            "qualifier": (match.group(2) or "card").lower(),
            "random": "at random" in match.group(0),
        }

    def get_discard_cost_candidates(self, game_state, controller, spec):
        qualifier = str((spec or {}).get("qualifier", "card")).lower()
        candidates = []
        for card_id in controller.get("hand", []):
            card = game_state._safe_get_card(card_id)
            if card is None:
                continue
            types = set(getattr(card, "card_types", []))
            if qualifier == "card" or qualifier in types:
                candidates.append(card_id)
        return candidates

    def max_affordable_x(self, game_state, controller, context=None):
        """Derive a finite X bound from every parsed resource in the cost."""
        cost = str(self.cost or "").lower()
        bounds = []
        if "{x}" in cost:
            pools = [controller.get("mana_pool", {})]
            pools.extend((
                controller.get("conditional_mana", {}) or {}).values())
            mana_bound = sum(
                max(0, int(amount or 0))
                for pool in pools if isinstance(pool, dict)
                for amount in pool.values())
            mana_bound += sum(
                max(0, int(amount or 0))
                for amount in (controller.get(
                    "phase_restricted_mana", {}) or {}).values())
            bounds.append(mana_bound)
        if re.search(r"pay\s+x\s+life", cost):
            bounds.append(max(0, int(controller.get("life", 0) or 0)))
        sacrifice = self.get_sacrifice_cost_spec()
        if sacrifice and sacrifice.get("count") == "x":
            bounds.append(len(self.get_sacrifice_cost_candidates(
                game_state, controller, sacrifice["requirement"])))
        discard = self.get_discard_cost_spec({"activation_X": 0})
        if discard and discard.get("count_expr") == "x":
            bounds.append(len(controller.get("hand", [])))
        counter = re.search(
            r"remove\s+x\s+(\w+|[+\-]\d+/[+\-]\d+)\s+counters?", cost)
        if counter:
            kind = counter.group(1)
            if "/" not in kind:
                kind = kind.upper()
            card = game_state._safe_get_card(self.card_id)
            bounds.append(max(0, int(
                getattr(card, "counters", {}).get(kind, 0) or 0)))
        return min(bounds) if bounds else 0

    @staticmethod
    def _as_sacrifice_occurrence(value):
        """Normalize a staged ``(card_id, battlefield_slot)`` selection."""
        if (isinstance(value, (tuple, list)) and len(value) == 2
                and isinstance(value[1], int)):
            return (value[0], value[1])
        return None

    def get_sacrifice_cost_candidates(self, game_state, controller,
                                       requirement=None, excluded=None,
                                       source_occurrence=None,
                                       return_occurrences=False):
        """Return live permanents that can pay a parsed sacrifice cost.

        Battlefield lists may contain the same card id more than once.  The
        optional occurrence form preserves those physical slots so selecting
        one copy does not accidentally exclude every repeated-id copy.
        """
        spec = self.get_sacrifice_cost_spec()
        if requirement is None:
            if not spec:
                return []
            requirement = spec["requirement"]
        req_lower = str(requirement or "").lower().strip(" .,;")
        self_sacrifice = self._is_self_sacrifice_requirement(req_lower)
        excluded_occurrences = set()
        excluded_ids = set()
        for value in excluded or []:
            occurrence = self._as_sacrifice_occurrence(value)
            if occurrence is not None:
                excluded_occurrences.add(occurrence)
            else:
                excluded_ids.add(value)
        source_occurrence = self._as_sacrifice_occurrence(source_occurrence)
        battlefield = controller.get("battlefield", [])
        if (source_occurrence is None
                or not 0 <= source_occurrence[1] < len(battlefield)
                or battlefield[source_occurrence[1]] != self.card_id):
            source_slot = next(
                (slot for slot, card_id in enumerate(battlefield)
                 if card_id == self.card_id), None)
            source_occurrence = ((self.card_id, source_slot)
                                 if source_slot is not None else None)
        candidates = []
        for slot, candidate_id in enumerate(battlefield):
            occurrence = (candidate_id, slot)
            if occurrence in excluded_occurrences or candidate_id in excluded_ids:
                continue
            if "another" in req_lower and occurrence == source_occurrence:
                continue
            if self_sacrifice and occurrence != source_occurrence:
                continue
            if not _permanent_matches_criteria(
                    game_state, candidate_id, req_lower,
                    controller=controller, source_id=None):
                continue
            candidates.append(occurrence if return_occurrences else candidate_id)
        return candidates

    def _normalize_sacrifice_selections(self, game_state, controller,
                                        selections, requirement,
                                        source_occurrence=None):
        """Map legacy id selections and new occurrence selections to slots."""
        candidates = self.get_sacrifice_cost_candidates(
            game_state, controller, requirement,
            source_occurrence=source_occurrence, return_occurrences=True)
        available = list(candidates)
        normalized = []
        for value in selections or []:
            occurrence = self._as_sacrifice_occurrence(value)
            if occurrence is None:
                occurrence = next(
                    (candidate for candidate in available
                     if candidate[0] == value), None)
            if occurrence is None or occurrence not in available:
                return None, candidates
            normalized.append(occurrence)
            available.remove(occurrence)
        return normalized, candidates

    def can_pay_cost(self, game_state, controller, context=None):
        """Preflight every supported activation-cost component without mutation."""
        context = context or {}
        timing_text = str(self.effect_text or "").lower()
        if ("activate only during your turn" in timing_text
                and game_state._get_active_player() is not controller):
            return False
        cost_text = str(self.cost or "")
        cost_lower = cost_text.lower()

        if self.is_exhaust:
            activation_idx = getattr(self, 'activation_index', -1)
            if activation_idx == -1 or game_state.check_exhaust_used(
                    self.card_id, activation_idx):
                return False

        if "{t}" in cost_lower or re.search(r"\btap\b", cost_lower):
            if self.card_id in controller.get("tapped_permanents", set()):
                return False
            card = game_state._safe_get_card(self.card_id)
            is_creature = card and "creature" in getattr(card, "card_types", [])
            if (is_creature
                    and self.card_id in controller.get("entered_battlefield_this_turn", set())
                    and not game_state.check_keyword(self.card_id, "haste")):
                return False

        if ("{q}" in cost_lower or re.search(r"\buntap\b", cost_lower)) \
                and self.card_id not in controller.get("tapped_permanents", set()):
            return False

        sacrifice_spec = self.get_sacrifice_cost_spec()
        if sacrifice_spec:
            sacrifice_count = self._resolve_cost_count(
                sacrifice_spec["count"], context)
            source_occurrence = context.get("activation_source_occurrence")
            selected_values = context.get("activation_sacrifice_occurrences")
            selection_key_present = "activation_sacrifice_occurrences" in context
            if selected_values is None:
                selected_values = context.get("activation_sacrifice_ids", [])
                selection_key_present = "activation_sacrifice_ids" in context
            selected, candidates = self._normalize_sacrifice_selections(
                game_state, controller, selected_values,
                sacrifice_spec["requirement"], source_occurrence)
            if len(candidates) < sacrifice_count:
                return False
            if selected is None or len(selected) > sacrifice_count:
                return False
            if (selection_key_present
                    and not sacrifice_spec["self_sacrifice"]
                    and len(selected) != sacrifice_count):
                return False

        discard_spec = self.get_discard_cost_spec(context)
        if discard_spec:
            candidates = self.get_discard_cost_candidates(
                game_state, controller, discard_spec)
            if len(candidates) < discard_spec["count"]:
                return False
            selected_discards = context.get("activation_discard_ids")
            if selected_discards is not None:
                if (len(selected_discards) != discard_spec["count"]
                        or len(set(selected_discards)) != len(selected_discards)
                        or any(card_id not in candidates
                               for card_id in selected_discards)):
                    return False

        life_match = re.search(r"pay\s+(x|\d+)\s+life", cost_lower)
        if life_match:
            life_amount = (int(context.get(
                'activation_X', context.get('X', 0)) or 0)
                if life_match.group(1) == 'x'
                else int(life_match.group(1)))
            if controller.get("life", 0) < life_amount:
                return False

        counter_match = re.search(
            r"remove\s+(?:(a|an|one|two|three|x|\d+)\s+)?"
            r"(\w+|[+\-]\d+/[+\-]\d+)\s+counters?", cost_lower)
        if counter_match:
            count = self._resolve_cost_count(
                counter_match.group(1) or "one", context)
            counter_type = counter_match.group(2)
            if "/" not in counter_type:
                counter_type = counter_type.upper()
            card = game_state._safe_get_card(self.card_id)
            if int(getattr(card, "counters", {}).get(counter_type, 0)) < count:
                return False

        mana_symbols = re.findall(r'\{([WUBRGCXSPMTQA0-9\/\.]+)\}', cost_text)
        if mana_symbols:
            if not getattr(game_state, "mana_system", None):
                return False
            mana_cost = "".join(f"{{{symbol}}}" for symbol in mana_symbols)
            parsed_cost = game_state.mana_system.parse_mana_cost(mana_cost)
            mana_context = dict(context)
            if 'activation_X' in mana_context:
                mana_context['X'] = mana_context['activation_X']
            return game_state.mana_system.can_pay_mana_cost(
                controller, parsed_cost, mana_context)
        return True

    def pay_cost(self, game_state, controller, sacrifice_choices=None,
                 source_occurrence=None, context=None):
        """Pay the activation cost of this ability with comprehensive cost handling."""
        preflight_context = dict(context or {})
        if sacrifice_choices is not None:
            choices = list(sacrifice_choices)
            if all(self._as_sacrifice_occurrence(choice) is not None
                   for choice in choices):
                preflight_context["activation_sacrifice_occurrences"] = choices
            else:
                preflight_context["activation_sacrifice_ids"] = choices
        if source_occurrence is not None:
            preflight_context["activation_source_occurrence"] = source_occurrence
        sacrifice_spec = self.get_sacrifice_cost_spec()
        if (sacrifice_spec and not sacrifice_spec["self_sacrifice"]
                and sacrifice_choices is None):
            # Non-self choices must come from a policy transaction. Direct
            # callers may probe legality but cannot silently pick a permanent.
            return False
        discard_spec = self.get_discard_cost_spec(preflight_context)
        if (discard_spec and not discard_spec["random"]
                and "activation_discard_ids" not in preflight_context):
            return False
        if not self.can_pay_cost(game_state, controller, preflight_context):
            return False
        # Use self.cost (which has Exhaust prefix removed if applicable by __init__)
        cost_text = self.cost
        cost_lower = cost_text.lower() if cost_text else ""
        all_costs_paid = True
        rollback_steps = []
        paid_sacrifice_ids = []

        # --- Handle is_exhaust flag (set during init) ---
        if self.is_exhaust:
            activation_idx = getattr(self, 'activation_index', -1)
            if activation_idx == -1:
                 logging.error(f"Exhaust ability on {self.card_id} missing activation_index. Cannot pay cost.")
                 return False
            if game_state.check_exhaust_used(self.card_id, activation_idx):
                 logging.debug(f"Cannot pay cost: Exhaust ability {activation_idx} for {self.card_id} already used this turn.")
                 return False
            # Exhaust itself isn't 'paid' here, but marked later IF other costs succeed

        # --- Non-Mana Costs FIRST (Logic mostly unchanged, uses cost_lower) ---
        # Tap Cost ({T} or tap)
        if "{t}" in cost_lower or re.search(r'\btap\b', cost_lower): # Added word boundary tap check
             card_name = getattr(game_state._safe_get_card(self.card_id), 'name', self.card_id)
             if self.card_id in controller.get("tapped_permanents", set()):
                 logging.debug(f"Cannot pay tap cost: {card_name} already tapped.")
                 return False # Cannot pay cost if already tapped
             # --- ADDED: Check if card can be tapped (e.g., not summoning sick if tapping requires it as an action) ---
             # Rule 302.6: A creature's activated ability with the tap symbol in its cost can't be activated unless the creature has been under its controller's control continuously since their most recent turn began. Ignore this restriction if the creature has haste.
             # Check if card is creature AND lacks haste AND entered this turn
             card_obj = game_state._safe_get_card(self.card_id)
             is_creature = card_obj and 'creature' in getattr(card_obj,'card_types',[])
             entered_this_turn = card_obj and self.card_id in controller.get("entered_battlefield_this_turn",set())
             has_haste = game_state.check_keyword(self.card_id, 'haste') # Use central check

             if is_creature and entered_this_turn and not has_haste:
                  logging.debug(f"Cannot pay tap cost: {card_name} has summoning sickness.")
                  return False # Summoning sickness prevents tapping for cost
             # --- END ADDED CHECK ---
             if not game_state.tap_permanent(self.card_id, controller):
                 logging.debug(f"Cannot pay tap cost: {card_name} couldn't be tapped (e.g., 'can't be tapped' effect).")
                 self._perform_rollback(game_state, controller, rollback_steps)
                 return False
             rollback_steps.append(("untap", self.card_id))
             logging.debug(f"Paid tap cost for {card_name}")

        # ... (Untap, Sacrifice, Discard, Pay Life, Remove Counters logic remains the same, ensure they use cost_lower correctly) ...
        # Untap Cost ({Q} or untap) - Use cost_lower for regex
        if "{q}" in cost_lower or re.search(r'\buntap\b', cost_lower):
             card_name = getattr(game_state._safe_get_card(self.card_id), 'name', self.card_id)
             if self.card_id not in controller.get("tapped_permanents", set()):
                 logging.debug(f"Cannot pay untap cost: {card_name} already untapped.")
                 return False
             if not game_state.untap_permanent(self.card_id, controller):
                 logging.debug(f"Cannot pay untap cost for {card_name} (e.g., 'doesn't untap' effect)")
                 self._perform_rollback(game_state, controller, rollback_steps)
                 return False
             rollback_steps.append(("tap", self.card_id))
             logging.debug(f"Paid untap cost for {card_name}")

        # Sacrifice Cost - Use cost_lower for regex
        sacrifice_spec = self.get_sacrifice_cost_spec()
        if sacrifice_spec:
             sac_req = sacrifice_spec["requirement"]
             count = self._resolve_cost_count(
                 sacrifice_spec["count"], preflight_context)
             if (sacrifice_choices is not None
                     and not sacrifice_spec["self_sacrifice"]
                     and len(sacrifice_choices) != count):
                  return False
             chosen_ids = iter(list(sacrifice_choices or []))
             for i in range(count):
                  preferred_choice = next(chosen_ids, None)
                  preferred_occurrence = self._as_sacrifice_occurrence(
                      preferred_choice)
                  preferred_id = (preferred_occurrence[0]
                                  if preferred_occurrence is not None
                                  else preferred_choice)
                  sacrifice_paid, sacrificed_id = self._pay_sacrifice_cost_with_rollback(
                      game_state, controller, sac_req, self.card_id,
                      rollback_steps, preferred_id=preferred_id)
                  if not sacrifice_paid:
                      logging.debug(f"Failed sacrifice cost: Required {count}, attempt {i+1} failed for '{sac_req}'.")
                      self._perform_rollback(game_state, controller, rollback_steps); return False
                  paid_sacrifice_ids.append(sacrificed_id)
             logging.debug(f"Paid sacrifice cost ({count}x '{sac_req}').")

        # Discard Cost - Use cost_lower for regex
        discard_spec = self.get_discard_cost_spec(preflight_context)
        if discard_spec:
             count = discard_spec["count"]
             is_random = discard_spec["random"]
             chosen_discards = preflight_context.get("activation_discard_ids")
             discard_paid, _ = self._pay_discard_cost_with_rollback(
                 game_state, controller, count, rollback_steps,
                 choices=chosen_discards, is_random=is_random)
             if not discard_paid: self._perform_rollback(game_state, controller, rollback_steps); return False
             logging.debug(f"Paid discard cost ({count} cards{' randomly' if is_random else ''}).")

        # Pay Life Cost - Use cost_lower for regex
        life_match = re.search(r"pay\s+(x|\d+)\s+life", cost_lower)
        if life_match:
             amount = (int(preflight_context.get(
                 'activation_X', preflight_context.get('X', 0)) or 0)
                 if life_match.group(1) == 'x'
                 else int(life_match.group(1)))
             if controller["life"] < amount: # Rule: Can pay life even if it brings you to 0 or less. Only prevent if already 0 or less? Re-check 119.4. Need > 0 to pay.
                  # Corrected: Cannot pay if life is less than cost, unless an effect allows paying with life you don't have (very rare). Rule 119.4
                  logging.debug(f"Cannot pay life cost {amount}: Only have {controller['life']} life.")
                  self._perform_rollback(game_state, controller, rollback_steps)
                  return False
             controller["life"] -= amount
             rollback_steps.append(("gain_life", amount))
             logging.debug(f"Paid {amount} life. Life is now {controller['life']}")
             # Trigger life loss event
             if hasattr(game_state, 'trigger_ability'):
                 game_state.trigger_ability(self.card_id, "LOSE_LIFE", {"player": controller, "amount": amount, "cause": "cost"})


        # Remove Counters Cost - Use cost_lower for regex
        counter_match = re.search(r"remove\s+(?:(?:a|an|one|two|three|x|\d+)\s+)?(\w+|[+\-]\d+/[+\-]\d+)\s+counters?(?: from.*?)?(?:$|,?\s*\{)", cost_lower)
        if counter_match:
            count_word_match = re.search(r"remove\s+(a|an|one|two|three|x|\d+)", cost_lower)
            count = 1
            if count_word_match:
                count = self._resolve_cost_count(
                    count_word_match.group(1), preflight_context)
            counter_type = counter_match.group(1)
            if '/' in counter_type and counter_type.replace('/','').replace('+','').replace('-','').isdigit(): pass
            else: counter_type = counter_type.upper()
            # Check source of counters (default: self, but could be target)
            counter_source_id = self.card_id # Assume self unless "from TARGET" specified
            from_match = re.search(r"from (.*?)($|,?\s*\{)", cost_lower)
            if from_match: # Needs to resolve target specification based on context
                logging.warning(f"Parsing 'remove counter from TARGET' cost is complex and not fully supported.")
                # For now, assume source is self if 'from' part exists but isn't parsed
                # Better implementation would need target context here.

            source_card = game_state._safe_get_card(counter_source_id)
            current_counter_count = 0
            if counter_source_id == self.card_id and source_card and hasattr(source_card, 'counters'): # Check self
                 current_counter_count = source_card.counters.get(counter_type, 0)
            # Check other permanents/players if source differs and is implemented

            if current_counter_count < count:
                logging.debug(f"Cannot remove {count} {counter_type}: Only {current_counter_count} available on {counter_source_id}.")
                self._perform_rollback(game_state, controller, rollback_steps); return False
            if not game_state.add_counter(counter_source_id, counter_type, -count): # Use add_counter for removal
                logging.warning(f"Failed to remove {count} {counter_type} counters from {counter_source_id}.")
                self._perform_rollback(game_state, controller, rollback_steps); return False
            rollback_steps.append(("add_counter", counter_source_id, counter_type, count))
            logging.debug(f"Paid by removing {count} {counter_type} counters from {counter_source_id}.")


        # --- Mana Costs LAST (uses cost_text for parsing) ---
        mana_cost_paid = True
        paid_mana_details = None
        # Regex for standard mana symbols {.}
        mana_symbols = re.findall(r'\{([WUBRGCXSPMTQA0-9\/\.]+)\}', cost_text)
        if mana_symbols:
            if hasattr(game_state, 'mana_system') and game_state.mana_system:
                mana_cost_str = "".join(f"{{{s}}}" for s in mana_symbols) # Reconstruct from found symbols only
                if mana_cost_str: # Ensure non-empty mana cost string
                    parsed_cost = game_state.mana_system.parse_mana_cost(mana_cost_str)
                    mana_context = dict(preflight_context)
                    if 'activation_X' in mana_context:
                        mana_context['X'] = mana_context['activation_X']
                    can_pay_mana = game_state.mana_system.can_pay_mana_cost(
                        controller, parsed_cost, mana_context)
                    if can_pay_mana:
                        # --- PAY MANA ---
                        paid_mana_details = game_state.mana_system.pay_mana_cost_get_details(
                            controller, parsed_cost, mana_context)
                        if paid_mana_details:
                            mana_cost_paid = True
                            rollback_steps.append(("refund_mana", paid_mana_details))
                            logging.debug(f"Paid mana cost: {mana_cost_str}")
                        else: # Payment failed internally
                            mana_cost_paid = False
                            logging.warning(f"Failed to pay mana cost '{mana_cost_str}' (pay_mana_cost_get_details returned None).")
                    else: # Cannot afford
                        mana_cost_paid = False
                        logging.debug(f"Cannot afford mana cost '{mana_cost_str}'.")
            else: # No mana system fallback
                logging.warning("Mana system not found, cannot handle mana costs properly.")
                mana_cost_paid = False # Cannot pay mana without system

            if not mana_cost_paid:
                logging.error(f"Rolling back costs due to failed mana payment for '{self.cost}'.")
                self._perform_rollback(game_state, controller, rollback_steps)
                return False

        # --- Mark Exhaust AFTER other costs paid ---
        if self.is_exhaust:
             activation_idx = getattr(self, 'activation_index', -1)
             if activation_idx == -1: # Should have index if is_exhaust was set
                  logging.error("CRITICAL: Exhaust activation_index lost during cost payment.")
                  self._perform_rollback(game_state, controller, rollback_steps)
                  return False
             if not game_state.mark_exhaust_used(self.card_id, activation_idx):
                  logging.error(f"Failed to mark Exhaust used for {self.card_id} index {activation_idx} AFTER paying costs.")
                  self._perform_rollback(game_state, controller, rollback_steps)
                  return False
             rollback_steps.append(("clear_exhaust", self.card_id, activation_idx))
             logging.debug(f"Marked Exhaust as used for {self.card_id} ability {activation_idx}.")

        # --- Final Check ---
        if all_costs_paid and mana_cost_paid:
             for sacrificed_id in paid_sacrifice_ids:
                  game_state.trigger_ability(
                      sacrificed_id, "SACRIFICED",
                      {"controller": controller, "cause": "ability_cost"})
             card_name = getattr(game_state._safe_get_card(self.card_id), 'name', self.card_id)
             logging.debug(f"Successfully paid cost '{self.effect_text}' for {card_name}")
             return True
        else:
             # This path shouldn't be reached due to early returns on failure
             logging.error(f"Cost payment check reached end incorrectly for '{self.cost}'. Rolling back.")
             self._perform_rollback(game_state, controller, rollback_steps)
             return False
         
    def _pay_sacrifice_cost_with_rollback(self, game_state, controller,
                                           sacrifice_req, ability_source_id,
                                           rollback_steps, preferred_id=None):
        """Pay one sacrifice cost after all choices and mana were preflighted."""
        req_lower = str(sacrifice_req or "").lower().strip(" .,;")
        if not req_lower:
            return False, None

        self_sacrifice = self._is_self_sacrifice_requirement(req_lower)
        candidates = self.get_sacrifice_cost_candidates(
            game_state, controller, requirement=req_lower)

        if not candidates:
            return False, None
        if preferred_id is not None:
            if preferred_id not in candidates:
                return False, None
            sacrifice_id = preferred_id
        else:
            sacrifice_id = ability_source_id if self_sacrifice else min(
                candidates,
                key=lambda cid: (
                    not bool(getattr(game_state._safe_get_card(cid), "is_token", False)),
                    getattr(game_state._safe_get_card(cid), "cmc", 0) or 0,
                ))
        sacrificed_card = game_state._safe_get_card(sacrifice_id)
        was_token = bool(getattr(sacrificed_card, "is_token", False))
        owner = game_state._find_card_owner_fallback(sacrifice_id) or controller
        if not game_state.move_card(
                sacrifice_id, controller, "battlefield", owner, "graveyard",
                cause="ability_cost"):
            return False, None
        # A token ceases to exist and cannot be rolled back. The caller
        # preflights mana and tap legality before committing this cost.
        if not was_token:
            rollback_steps.append(("return_sacrificed_permanent", sacrifice_id, owner))
        return True, sacrifice_id

    def _pay_discard_cost_with_rollback(
            self, game_state, controller, count, rollback_steps,
            choices=None, is_random=False):
        """Pay exactly the announced discard selection with rollback."""
        if len(controller["hand"]) < count:
            return False, None
        if is_random:
            hand_copy = random.sample(list(controller["hand"]), count)
        else:
            hand_copy = list(choices or [])
            if (len(hand_copy) != count or len(set(hand_copy)) != count
                    or any(card_id not in controller["hand"]
                           for card_id in hand_copy)):
                return False, None
        successfully_discarded = []
        failed_to_discard = False

        for _ in range(count):
            if hand_copy:
                discard_id = hand_copy.pop(0) # Take from front of copy
                if game_state.discard_card(
                        controller, discard_id, source_id=self.card_id,
                        cause="ability_cost"):
                    successfully_discarded.append(discard_id)
                else:
                    # If move failed, abort cost payment immediately
                    failed_to_discard = True
                    break
            else: # Should not happen if initial check passed
                 failed_to_discard = True
                 break

        if failed_to_discard:
             # Add rollback steps for successfully discarded cards *before* the failure
             for success_id in successfully_discarded:
                  rollback_steps.append(("return_discarded_to_hand", success_id))
             return False, None
        else:
             # Add rollback steps for all successfully discarded cards
             for success_id in successfully_discarded:
                 rollback_steps.append(("return_discarded_to_hand", success_id))
             logging.debug(f"Paid discard cost ({len(successfully_discarded)} cards).")
             return True, successfully_discarded

    def _perform_rollback(self, game_state, controller, rollback_steps):
        """Performs rollback steps in reverse order."""
        logging.warning(f"Performing cost payment rollback: {rollback_steps}")
        for step in reversed(rollback_steps):
            action = step[0]
            try:
                if action == "untap": game_state.untap_permanent(step[1], controller)
                elif action == "tap": game_state.tap_permanent(step[1], controller)
                elif action == "return_sacrificed_permanent": game_state.move_card(step[1], step[2], "graveyard", controller, "battlefield")
                elif action == "return_from_graveyard_to_hand": game_state.move_card(step[1], controller, "graveyard", controller, "hand")
                elif action == "return_discarded_to_hand":
                    owner, zone = game_state.find_card_location(step[1])
                    if owner is controller and zone in {"graveyard", "exile"}:
                        game_state.move_card(
                            step[1], controller, zone, controller, "hand",
                            cause="cost_rollback")
                elif action == "gain_life": controller["life"] += step[1]
                elif action == "add_counter": game_state.add_counter(step[1], step[2], step[3])
                elif action == "refund_mana": game_state.mana_system.add_mana(controller, step[1]) # Assumes add_mana handles refunding specific details
                elif action == "restore_mana_pool": controller["mana_pool"] = step[1] # Basic fallback
            except Exception as e:
                logging.error(f"Error during rollback step {step}: {e}")
    
    def _can_sacrifice(game_state, controller, sacrifice_req):
        """Basic check if controller can meet sacrifice requirements"""
        if not sacrifice_req: return False
        req_lower = sacrifice_req.lower()
        valid_types = ['creature', 'artifact', 'enchantment', 'land', 'planeswalker', 'permanent']
        req_type = next((t for t in valid_types if t in req_lower), None)

        # Check if self sacrifice
        if "this permanent" in req_lower or "this creature" in req_lower or req_lower == 'it':
            # In pay_cost, self.card_id will be the source card ID.
            # Here, we only check if *a* sacrifice is possible, specific card checked later.
            return True # Assume the source itself is valid if required.

        # Check if player controls any permanent of the required type
        if req_type:
            for card_id in controller.get("battlefield", []):
                card = game_state._safe_get_card(card_id)
                if card and (req_type == 'permanent' or req_type in getattr(card, 'card_types', [])):
                    return True # Found at least one valid permanent
            return False # No valid permanent found

        # If no type specified, assume any permanent can be sacrificed
        return bool(controller.get("battlefield"))
    
    def _pay_generic_mana(self, game_state, controller, amount):
        """Pay generic mana cost using available colored mana"""
        # First use colorless mana if available
        colorless_used = min(controller["mana_pool"].get('C', 0), amount)
        controller["mana_pool"]['C'] -= colorless_used
        amount -= colorless_used
        
        # Then use colored mana in a reasonable order (usually save WUBRG for colored costs)
        colors = ['G', 'R', 'B', 'U', 'W']  # Priority order for spending
        
        for color in colors:
            if amount <= 0:
                break
                
            available = controller["mana_pool"].get(color, 0)
            used = min(available, amount)
            controller["mana_pool"][color] -= used
            amount -= used
            
        if amount > 0:
            logging.warning(f"Failed to pay all generic mana costs, {amount} mana short")
            
        return amount <= 0
    
    def _pay_sacrifice_cost(game_state, controller, sacrifice_req, ability_source_id):
        """Basic payment of sacrifice cost (AI chooses simplest valid target)"""
        if not sacrifice_req: return False
        req_lower = sacrifice_req.lower()
        valid_types = ['creature', 'artifact', 'enchantment', 'land', 'planeswalker', 'permanent']
        req_type = next((t for t in valid_types if t in req_lower), None)
        target_id_to_sacrifice = None

        if "this permanent" in req_lower or "this creature" in req_lower or req_lower == 'it':
            target_id_to_sacrifice = ability_source_id # Sacrifice the source itself
        else:
            # Find a suitable permanent (simple choice: first valid found)
            valid_options = []
            for card_id in controller.get("battlefield", []):
                card = game_state._safe_get_card(card_id)
                if card and (req_type == 'permanent' or not req_type or req_type in getattr(card, 'card_types', [])):
                    valid_options.append(card_id)
            # Basic AI: sacrifice least valuable (e.g., lowest CMC, or a token)
            if valid_options:
                # Prefer tokens
                tokens = [opt for opt in valid_options if "TOKEN" in opt]
                if tokens: target_id_to_sacrifice = tokens[0]
                else:
                    # Choose lowest CMC non-token
                    non_tokens = sorted([opt for opt in valid_options if "TOKEN" not in opt], key=lambda cid: getattr(game_state._safe_get_card(cid), 'cmc', 99))
                    if non_tokens: target_id_to_sacrifice = non_tokens[0]

        if target_id_to_sacrifice and target_id_to_sacrifice in controller.get("battlefield", []):
            sac_card_name = getattr(game_state._safe_get_card(target_id_to_sacrifice), 'name', target_id_to_sacrifice)
            if game_state.move_card(target_id_to_sacrifice, controller, "battlefield", controller, "graveyard"):
                logging.debug(f"Sacrificed {sac_card_name} to pay cost.")
                return True
        logging.warning(f"Could not find valid permanent to sacrifice for '{sacrifice_req}'")
        return False
    
    def _pay_discard_cost(self, game_state, controller, discard_req):
        """Pay a discard cost"""
        # Parse the discard requirement
        if 'a card' in discard_req or 'card' in discard_req:
            # Discard any card
            if controller["hand"]:
                card_id = controller["hand"][0]  # Just pick the first card
                game_state.move_card(card_id, controller, "hand", controller, "graveyard")
                logging.debug(f"Discarded {game_state._safe_get_card(card_id).name} to pay ability cost")
        elif 'your hand' in discard_req:
            # Discard entire hand
            while controller["hand"]:
                card_id = controller["hand"][0]
                game_state.move_card(card_id, controller, "hand", controller, "graveyard")
            logging.debug(f"Discarded entire hand to pay ability cost")
            
    def _can_exile_from_graveyard(self, game_state, controller, exile_req):
        """Check if controller can meet exile from graveyard requirements"""
        # Handle various exile requirements
        if exile_req == "a creature card":
            for card_id in controller["graveyard"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    return True
            return False
        elif exile_req == "a card":
            return len(controller["graveyard"]) > 0
        
        # Default to assuming requirement can be met
        return True

    def _pay_exile_from_graveyard_cost(self, game_state, controller, exile_req):
        """Pay an exile from graveyard cost"""
        # Find appropriate card to exile
        target_id = None
        
        if exile_req == "a creature card":
            for card_id in controller["graveyard"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    target_id = card_id
                    break
        elif exile_req == "a card":
            if controller["graveyard"]:
                target_id = controller["graveyard"][0]
        
        # Perform the exile
        if target_id:
            game_state.move_card(target_id, controller, "graveyard", controller, "exile")
            card = game_state._safe_get_card(target_id)
            logging.debug(f"Exiled {card.name if card else target_id} from graveyard to pay cost")

    def _can_exile_from_hand(self, game_state, controller, exile_req):
        """Check if controller can meet exile from hand requirements"""
        if not controller["hand"]:
            return False
        
        if exile_req == "a creature card":
            for card_id in controller["hand"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    return True
            return False
        
        # Default to assuming requirement can be met if hand has cards
        return len(controller["hand"]) > 0

    def _pay_exile_from_hand_cost(self, game_state, controller, exile_req):
        """Pay an exile from hand cost"""
        # Find appropriate card to exile
        target_id = None
        
        if exile_req == "a creature card":
            for card_id in controller["hand"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    target_id = card_id
                    break
        elif exile_req == "a card":
            if controller["hand"]:
                target_id = controller["hand"][0]
        
        # Perform the exile
        if target_id:
            game_state.move_card(target_id, controller, "hand", controller, "exile")
            card = game_state._safe_get_card(target_id)
            logging.debug(f"Exiled {card.name if card else target_id} from hand to pay cost")


class TriggeredAbility(Ability):
    """Ability that triggers on certain game events"""
    def __init__(self, card_id, trigger_condition=None, effect=None, effect_text="", additional_condition=None):
        super().__init__(card_id, effect_text)
        # Allow parsing from effect_text if condition/effect not provided
        parsed_condition, parsed_effect = None, None
        if trigger_condition is None and effect is None and effect_text:
            parsed_condition, parsed_effect = self._parse_condition_effect(effect_text) # Uses updated parser

        # Ensure attributes are strings, default to empty if None
        self.trigger_condition = str(trigger_condition) if trigger_condition is not None else (str(parsed_condition) if parsed_condition is not None else "Unknown")
        self.effect = str(effect) if effect is not None else (str(parsed_effect) if parsed_effect is not None else "Unknown")
        self.trigger_condition = self.trigger_condition.lower() # Store lower
        self.effect = self.effect.lower() # Store lower
        self.additional_condition = additional_condition # Can be string or callable

        # CR 603.4 intervening "if": "When/Whenever/At [event], if [condition],
        # [effect]." The condition is checked at trigger time (can_trigger) AND
        # again at resolution (resolve/resolve_with_targets); if false at either
        # point the ability does not trigger / does nothing.
        self.intervening_if = None
        _iv = re.match(r'^\s*if\s+(.+?),\s*(.+)$', self.effect, re.IGNORECASE)
        if _iv:
            self.intervening_if = "if " + _iv.group(1).strip()
            self.effect = _iv.group(2).strip()

        # Validation after potential parsing
        if self.trigger_condition == "unknown":
             raise ValueError(f"TriggeredAbility requires trigger_condition. Got text='{effect_text}'")
        if self.effect == "unknown":
             raise ValueError(f"TriggeredAbility requires effect. Got text='{effect_text}'")

        # Targeting belongs to the parsed instruction, not necessarily the
        # reconstructed full trigger text.  AbilityHandler uses this marker as
        # a defensive resolution contract after targets have been chosen.
        # A reflexive ``When you do`` sentence is a distinct triggered
        # ability.  Its target is chosen only after the prerequisite happens,
        # not when the parent trigger is put on the stack (CR 603.12).
        parent_effect = re.split(
            r"\bwhen (?:you do|that player does)\b", self.effect,
            maxsplit=1, flags=re.IGNORECASE)[0]
        self.requires_target = "target" in parent_effect.lower()

        # Store original text if not provided
        if not effect_text:
            self.effect_text = f"{self.trigger_condition.capitalize()}, {self.effect.capitalize()}." # Reconstruct from parts

    def _parse_condition_effect(self, text):
        """Attempt to parse 'When/Whenever/At..., Effect.' or 'When/Whenever/At... — Effect' format. Handles em dash."""
        # Regex includes comma, colon, en dash, em dash, unicode em dash as separators
        # BUGFIX (July 2026): the separator between trigger and effect was fully
        # optional, so the non-greedy trigger group matched a single character
        # ("when t...") and every text-parsed trigger condition was mangled --
        # can_trigger's patterns never matched and text-parsed triggers never
        # fired. The separator (comma / colon / dash) is now mandatory.
        match = re.match(
            r'^\s*((?:when|whenever|at)\b[^,:\u2013\u2014]*?)\s*[,:\u2013\u2014]\s*(.+?)\s*$',
            text.strip(), re.IGNORECASE | re.DOTALL)
        if match:
            trigger_part = match.group(1).strip()
            effect_part = match.group(2).strip()
            # Remove trailing period if present
            if effect_part.endswith('.'): effect_part = effect_part[:-1]
            # Simple validation: effect shouldn't contain trigger keywords unless nested
            if not re.match(r'^(when|whenever|at)\b', effect_part.lower()):
                 return trigger_part, effect_part
            else: # Effect seems to contain another trigger keyword, parse likely failed
                 logging.debug(f"Possible nested trigger in effect part: '{effect_part}'. Parse might be inaccurate.")
                 # Return best guess
                 return trigger_part, effect_part
        logging.warning(f"Could not parse Trigger[?,:,\u2014] Effect from '{text}'")
        return None, None # Return None if parse fails
        
    def can_trigger(self, event_type, context=None):
        """Check if the ability should trigger based on an event and additional conditions with improved pattern matching."""
        # Define trigger condition patterns with more flexibility
        trigger_conditions = {
            "ENTERS_BATTLEFIELD": [
                r"when(ever)?\s+.*enters the battlefield",
                r"when(ever)?\s+.*enters",
                r"when(ever)?\s+.*comes into play"
            ],
            "ATTACKS": [
                r"when(ever)?\s+.*attacks",
                r"when(ever)?\s+.*declares? attack",
                r"when(ever)?\s+.*becomes? attacking"
            ],
            "BLOCKS": [
                r"when(ever)?\s+.*blocks",
                r"when(ever)?\s+.*declares? block",
                r"when(ever)?\s+.*becomes? blocking"
            ],
            "DEALS_DAMAGE": [
                r"when(ever)?\s+.*deals damage",
                r"when(ever)?\s+.*deals combat damage",
                r"when(ever)?\s+damage is dealt"
            ],
            "DIES": [
                r"when(ever)?\s+.*dies",
                r"when(ever)?\s+.*is put into a graveyard from the battlefield",
                r"when(ever)?\s+.*goes to the graveyard"
            ],
            "CASTS": [
                r"when(ever)?\s+.*cast",
                r"when(ever)?\s+.*casts?",
                r"when(ever)?\s+.*play"
            ],
            "CAST_SPELL": [
                r"when(ever)?\s+.*cast",
            ],
            "BEGINNING_OF_UPKEEP": [
                r"at the beginning of (your|each) upkeep",
                r"at the beginning of the upkeep",
                r"during (your|each) upkeep"
            ],
            "END_OF_TURN": [
                r"at the end of (your|each) turn",
                r"at the beginning of (your|the|each) end step",
                r"at the end of (the|each) turn"
            ],
            "BEGINNING_OF_COMBAT": [
                r"at the beginning of (each )?combat",
            ],
            # The phase dispatcher's actual end-step event name. The legacy
            # END_OF_TURN entry above matches the same wordings but no code
            # path ever dispatched it.
            "BEGINNING_OF_END_STEP": [
                r"at the end of (your|each) turn",
                r"at the beginning of (your|the|each) end step",
                r"at the end of (the|each) turn"
            ],
            "DISCARD": [
                r"when(ever)?\s+.*discard",
                r"when(ever)?\s+.*discards?",
                r"when(ever)?\s+.*is discarded"
            ],
            "LEAVE_GRAVEYARD": [
                r"when(ever)?\s+.*\bleave(?:s)?\s+(?:your|a|the) graveyard",
            ],
            "DOOR_UNLOCKED": [
                r"when(ever)?\s+.*unlock",
                r"when(ever)?\s+.*unlocks?",
                r"when(ever)?\s+.*becomes? unlocked"
            ],
            "ROOM_FULLY_UNLOCKED": [
                r"when(ever)?\s+.*fully unlock.*room",
            ],
            "GAIN_LIFE": [
                r"when(ever)?\s+.*gain(s)? life",
                r"when(ever)?\s+.*life is gained"
            ],
            "LOSE_LIFE": [
                r"when(ever)?\s+.*lose(s)? life",
                r"when(ever)?\s+.*life is lost"
            ],
            "DIE_ROLLED": [
                r"when(ever)?\s+.*rolls?\s+(?:one or more\s+)?dice",
                r"when(ever)?\s+.*rolls?\s+(?:a|one or more)\s+di(?:e|ce)"
            ],
            "SPECIALIZES": [
                r"when(ever)?\s+.*specializes?"
            ],
            "MUTATES": [
                r"when(ever)?\s+.*mutates?"
            ],
            "DISCOVER": [
                r"when(ever)?\s+.*\bdiscover(?:s|ed)?\b"
            ],
            "UNTAPPED": [
                r"when(ever)?\s+.*becomes? untapped",
                r"when(ever)?\s+.*untaps?"
            ],
            "BECOMES_TARGET": [
                r"when(ever)?\s+.*becomes?\s+(?:a|the)\s+target",
                r"when(ever)?\s+.*is\s+targeted"
            ],
            "DAMAGED": [
                r"when(ever)?\s+.*is dealt damage",
                r"when(ever)?\s+a source deals damage to"
            ],
            "SAGA_CHAPTER": [
                r"saga chapter \d+"
            ]
        }
        
        # Helper function to check if text matches any pattern
        def matches_any_pattern(text, patterns):
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            return False
        
        # Get condition patterns for this event
        event_patterns = trigger_conditions.get(event_type, [])

        # "a source deals damage to this creature" is a DAMAGED-class trigger
        # on the damaged object, not a source-side DEALS_DAMAGE trigger, even
        # though it matches the loose "deals damage" pattern.
        if (event_type == "DEALS_DAMAGE"
                and re.search(r"deals damage to this\s+(?:creature|permanent)",
                              self.trigger_condition, re.IGNORECASE)):
            return False

        # Check if our trigger condition matches any of the patterns
        if matches_any_pattern(self.trigger_condition, event_patterns):
            if (event_type == "SAGA_CHAPTER"
                    and int((context or {}).get("chapter", 0) or 0)
                    != int(getattr(self, "saga_chapter", 0) or 0)):
                return False
            if event_type == "DEALS_DAMAGE":
                context = context or {}
                source_card_id = context.get("source_card_id", self.card_id)
                event_card_id = context.get("event_card_id")
                source_card = context.get("source_card")
                source_name = str(
                    getattr(source_card, "name", "") or "").lower()
                short_name = source_name.split(",")[0].strip()
                names_source = bool(
                    re.search(r"\bthis\s+(?:creature|permanent)\b.*\bdeals\b",
                              self.trigger_condition, re.IGNORECASE)
                    or (short_name and re.search(
                        rf"^\s*whenever\s+{re.escape(short_name)}\s+deals\b",
                        self.trigger_condition, re.IGNORECASE)))
                if names_source and source_card_id != event_card_id:
                    return False
                if ("combat damage to a player" in self.trigger_condition
                        and (not context.get("to_player")
                             or int(context.get("damage_amount", 0) or 0) <= 0)):
                    return False
            if event_type == "DIES":
                context = context or {}
                controlled_creature = re.search(
                    r"\b(a|another)\s+(?:(nonland|nontoken)\s+)?"
                    r"creature you control dies\b",
                    self.trigger_condition, re.IGNORECASE)
                if controlled_creature:
                    last_known = context.get("last_known") or {}
                    game_state = context.get("game_state")
                    controller = context.get("controller")
                    if game_state is None or controller is None:
                        return False
                    card_types = {
                        str(card_type).lower()
                        for card_type in last_known.get("card_types", [])}
                    if (not last_known.get("was_creature", False)
                            and "creature" not in card_types):
                        return False
                    if (controlled_creature.group(2) == 'nonland'
                            and "land" in card_types):
                        return False
                    if (controlled_creature.group(2) == 'nontoken'
                            and last_known.get('is_token', False)):
                        return False
                    controller_key = (
                        "p1" if controller is game_state.p1
                        else "p2")
                    if last_known.get("controller_key") != controller_key:
                        return False
                    if (controlled_creature.group(1).lower() == "another"
                            and context.get("source_card_id")
                            == context.get("event_card_id")):
                        return False
            if event_type == "ENTERS_BATTLEFIELD":
                context = context or {}
                source_card_id = context.get("source_card_id")
                event_card_id = context.get("event_card_id")
                source_card = context.get("source_card")
                full_name = str(getattr(source_card, "name", "") or "").lower()
                source_name = re.escape(full_name)
                # Oracle text refers to legendaries by their short name
                # ("When Beza enters" on Beza, the Bounding Spring).
                short_name = re.escape(full_name.split(",")[0].strip())
                self_entry = bool(
                    re.search(r"\bthis\s+(?:artifact|aura|battle|creature|"
                              r"enchantment|land|permanent|card)\b.*\benters\b",
                              self.trigger_condition, re.IGNORECASE)
                    or (source_name and re.search(
                        rf"^\s*when(?:ever)?\s+{source_name}\s+enters\b",
                        self.trigger_condition, re.IGNORECASE))
                    or (short_name and re.search(
                        rf"^\s*when(?:ever)?\s+{short_name}\s+enters\b",
                        self.trigger_condition, re.IGNORECASE)))
                if self_entry and source_card_id != event_card_id:
                    return False
                if "a land you control enters" in self.trigger_condition:
                    event_card = context.get("event_card")
                    event_types = {
                        str(card_type).lower() for card_type in getattr(
                            event_card, "card_types", [])}
                    if ("land" not in event_types
                            or context.get(
                                "event_controller", context.get("controller"))
                            is not context.get("controller")):
                        return False
                if "an enchantment you control enters" in self.trigger_condition:
                    event_types = {str(t).lower() for t in getattr(context.get('event_card'), 'card_types', [])}
                    if ('enchantment' not in event_types
                            or context.get('event_controller', context.get('controller')) is not context.get('controller')):
                        return False
                controlled_entry = re.search(
                    r"\b(another\s+)?(nontoken\s+)?creature you control "
                    r"enters\b", self.trigger_condition, re.IGNORECASE)
                if controlled_entry:
                    event_card = context.get('event_card')
                    event_types = {
                        str(card_type).lower() for card_type in getattr(
                            event_card, 'card_types', [])}
                    if ('creature' not in event_types
                            or context.get('event_controller',
                                           context.get('controller')) is not
                            context.get('controller')):
                        return False
                    if (controlled_entry.group(1)
                            and source_card_id == event_card_id):
                        return False
                    if (controlled_entry.group(2)
                            and bool(getattr(event_card, 'is_token', False))):
                        return False
            if event_type == "LEAVE_GRAVEYARD":
                context = context or {}
                if ("your graveyard" in self.trigger_condition
                        and context.get("from_player") is not context.get("controller")):
                    return False
                event_card = context.get("event_card") or context.get("card")
                event_types = {
                    str(card_type).lower()
                    for card_type in getattr(event_card, "card_types", [])
                }
                trigger_prefix = self.trigger_condition.split("leave", 1)[0]
                mentioned_types = {
                    card_type for card_type in (
                        "artifact", "battle", "creature", "enchantment",
                        "land", "planeswalker")
                    if re.search(rf"\b{card_type}s?\b", trigger_prefix)
                }
                if mentioned_types and not event_types.intersection(mentioned_types):
                    return False
            if event_type == "CAST_SPELL" and "targets only a single creature you control" in self.trigger_condition:
                context = context or {}
                if context.get('controller') is not context.get('casting_player', context.get('controller')):
                    return False
                target_ids = []
                for values in (context.get('targets') or {}).values():
                    if isinstance(values, list):
                        target_ids.extend(values)
                target_ids = list(dict.fromkeys(target_ids))
                if len(target_ids) != 1:
                    return False
                target = target_ids[0]
                if not isinstance(target, int) or context.get('game_state').get_card_controller(target) is not context.get('controller'):
                    return False
                cast_card = context.get('game_state')._safe_get_card(context.get('cast_card_id'))
                cast_types = {
                    str(card_type).lower() for card_type in context.get(
                        'cast_card_types', getattr(cast_card, 'card_types', []))}
                if not cast_card or not ({'instant', 'sorcery'} & cast_types):
                    return False
            if event_type == "CAST_SPELL":
                context = context or {}
                if (re.search(r"\byou cast\b", self.trigger_condition,
                              re.IGNORECASE)
                        and context.get("casting_player") is not
                        context.get("controller")):
                    return False
                # "When you cast this spell/card" is a self-cast trigger; it
                # must not fire for other spells cast while the source is on
                # the battlefield (Sage of the Skies was copying every later
                # spell its controller cast, then fizzling with a warning).
                if re.search(r"\byou cast\s+this\s+(?:spell|card|creature)\b",
                             self.trigger_condition, re.IGNORECASE):
                    cast_id = context.get(
                        "cast_card_id", context.get("event_card_id"))
                    if cast_id != getattr(self, "card_id", None):
                        return False
                if "your second spell each turn" in self.trigger_condition:
                    game_state = context.get("game_state")
                    trigger_controller = context.get("controller")
                    if game_state is None or trigger_controller is None:
                        return False
                    cast_count = sum(
                        1 for entry in getattr(
                            game_state, "spells_cast_this_turn", [])
                        if isinstance(entry, tuple) and len(entry) > 1
                        and entry[1] is trigger_controller)
                    if cast_count != 2:
                        return False
                if "instant or sorcery spell" in self.trigger_condition:
                    game_state = context.get("game_state")
                    cast_card = (game_state._safe_get_card(
                        context.get("cast_card_id")) if game_state else None)
                    cast_types = {
                        str(card_type).lower() for card_type in context.get(
                            "cast_card_types", getattr(
                                cast_card, "card_types", []))}
                    if not cast_types.intersection({"instant", "sorcery"}):
                        return False
                mana_value_match = re.search(
                    r"spell with mana value (\d+) or greater",
                    self.trigger_condition, re.IGNORECASE)
                if mana_value_match:
                    game_state = context.get("game_state")
                    if game_state is None:
                        return False
                    cast_card = game_state._safe_get_card(
                        context.get("cast_card_id",
                                    context.get("event_card_id")))
                    mana_value = float(
                        getattr(cast_card, "cmc", 0) or 0) if cast_card else 0
                    if cast_card and "X" in context:
                        x_symbols = len(re.findall(
                            r"\{X\}",
                            str(getattr(cast_card, "mana_cost", "") or ""),
                            re.IGNORECASE))
                        mana_value += max(0, int(context.get("X", 0) or 0)) \
                            * x_symbols
                    if (not cast_card
                            or mana_value < int(mana_value_match.group(1))):
                        return False
            if event_type == "DISCOVER":
                context = context or {}
                if (re.search(r"\byou discover\b", self.trigger_condition,
                              re.IGNORECASE)
                        and context.get("discovering_player") is not
                        context.get("controller")):
                    return False
            if event_type == "BECOMES_TARGET":
                context = context or {}
                target_id = context.get("target_id", context.get("event_card_id"))
                source_card_id = context.get("source_card_id")
                if (re.search(r"\bthis\s+(?:creature|permanent|card)\b",
                              self.trigger_condition, re.IGNORECASE)
                        and source_card_id != target_id):
                    return False
                if (re.search(r"target of (?:a|an) spell or ability you control",
                              self.trigger_condition, re.IGNORECASE)
                        and context.get("targeting_controller") is not context.get("controller")):
                    return False
                # Watchers such as Pawpatch Recruit distinguish both sides of
                # the event: the targeted permanent must be controlled by the
                # watcher, and the targeting object must be controlled by an
                # opponent.  Without these gates, choosing the target of the
                # Recruit trigger itself recursively created another trigger.
                if (re.search(r"(?:a|another) creature you control becomes "
                              r"the target", self.trigger_condition,
                              re.IGNORECASE)
                        and context.get("target_controller") is not
                        context.get("controller")):
                    return False
                if (re.search(r"spell or ability an opponent controls",
                              self.trigger_condition, re.IGNORECASE)
                        and context.get("targeting_controller") is
                        context.get("controller")):
                    return False
                if ("first time each turn" in self.trigger_condition
                        and not context.get("first_time_targeted_by_controller_this_turn", False)):
                    return False
            if event_type == "ATTACKS":
                context = context or {}
                attacker_id = context.get("attacker_id", context.get("event_card_id"))
                gs = context.get("game_state")
                source_id = context.get("source_card_id")
                attachment_subject_id = None
                if gs and source_id is not None:
                    for player in (gs.p1, gs.p2):
                        if source_id in player.get("attachments", {}):
                            attachment_subject_id = player["attachments"][source_id]
                            break
                # "this creature/land attacks" only triggers for the attacker
                # itself, never for other permanents that merely hear the event.
                if (re.search(r"\bthis\s+(?:creature|permanent|land)\b.*\battacks\b",
                              self.trigger_condition, re.IGNORECASE)
                        and (attachment_subject_id
                             if attachment_subject_id is not None else source_id)
                        != attacker_id):
                    return False
                if ("first time each turn" in self.trigger_condition
                        and not context.get("first_attack_this_turn", False)):
                    return False
                attacker_card = (gs._safe_get_card(attacker_id)
                                 if gs and attacker_id is not None else None)
                # A trigger granted by an Aura or Equipment is scoped to the
                # creature that source is actually attached to. Previously
                # these wordings either over-fired for every attacker or were
                # suppressed by the generic watcher vocabulary check.
                if (gs and attacker_id is not None
                        and re.search(
                            r"\b(?:equipped|enchanted)\s+creature\s+attacks\b",
                            self.trigger_condition, re.IGNORECASE)):
                    attached_to = None
                    for player in (gs.p1, gs.p2):
                        if source_id in player.get("attachments", {}):
                            attached_to = player["attachments"][source_id]
                            break
                    if attached_to != attacker_id:
                        return False
                # Watcher wordings ("whenever a/another <what> [you control]
                # attacks") fire only when the attacker matches the printed
                # scope. Before July 2026 every watcher fired on every attack,
                # wrong-controller and wrong-type watchers included.
                watcher = re.search(
                    r"when(?:ever)?\s+(a|an|another|one or more)\s+(.+?)\s+attacks?\b",
                    self.trigger_condition, re.IGNORECASE)
                if watcher and attacker_card:
                    scope = watcher.group(2).strip().lower()
                    if scope.endswith("you control"):
                        scope = scope[: -len("you control")].strip()
                        if gs.get_card_controller(attacker_id) is not context.get("controller"):
                            return False
                    if (watcher.group(1).lower() == "another"
                            and context.get("source_card_id") == attacker_id):
                        return False
                    vocabulary = {"permanent"}
                    for group in ("card_types", "subtypes", "supertypes"):
                        vocabulary.update(
                            str(t).lower()
                            for t in getattr(attacker_card, group, None) or [])
                    scope_words = [
                        word for word in re.split(r"[\s,]+", scope) if word]
                    is_token = bool(getattr(attacker_card, "is_token", False))
                    if "nontoken" in scope_words and is_token:
                        return False
                    if "token" in scope_words and not is_token:
                        return False
                    for word in scope_words:
                        if word in {"nontoken", "token"}:
                            continue
                        if (word and word not in vocabulary
                                and not (word.endswith("s") and word[:-1] in vocabulary)):
                            return False
                # Defender-side wordings ("attacks you or a planeswalker you
                # control") belong to the player being attacked, never the
                # attacker's own controller.
                if (re.search(r"\battacks?\s+you\b", self.trigger_condition, re.IGNORECASE)
                        and gs and attacker_id is not None
                        and gs.get_card_controller(attacker_id) is context.get("controller")):
                    return False
            if event_type == "DAMAGED":
                context = context or {}
                # "this creature is dealt damage" belongs to the damaged object.
                if (re.search(r"\bthis\s+(?:creature|permanent)\b",
                              self.trigger_condition, re.IGNORECASE)
                        and context.get("source_card_id") != context.get("event_card_id")):
                    return False
            if event_type == "BEGINNING_OF_COMBAT":
                context = context or {}
                gs = context.get("game_state")
                # "at the beginning of combat on your turn" belongs to the
                # ability controller's own turns only.
                if ("on your turn" in self.trigger_condition and gs
                        and gs._get_active_player() is not context.get("controller")):
                    return False
            if event_type in ("BEGINNING_OF_UPKEEP", "END_OF_TURN",
                              "BEGINNING_OF_DRAW", "BEGINNING_OF_END_STEP",
                              "BEGINNING_OF_PRECOMBAT_MAIN"):
                context = context or {}
                gs = context.get("game_state")
                cond = self.trigger_condition.lower()
                # "your upkeep / your end step" belongs to the ability
                # controller's own turns; "an opponent's upkeep" to turns of
                # the controller's opponents. Ungated wordings previously
                # fired on every player's phase.
                if (gs and re.search(
                        r"\byour\s+(?:upkeep|end step|draw step|precombat main)",
                        cond)
                        and gs._get_active_player() is not context.get("controller")):
                    return False
                if (gs and re.search(
                        r"\b(?:each\s+)?opponent'?s?\s+(?:upkeep|end step)", cond)
                        and gs._get_active_player() is context.get("controller")):
                    return False

            once_per_turn_key = None
            if "this ability triggers only once each turn" in self.effect:
                context = context or {}
                game_state = context.get("game_state")
                if game_state is None:
                    return False
                once_per_turn_key = (
                    context.get("source_card_id", self.card_id),
                    self.trigger_condition,
                    self.effect,
                )
                if game_state.once_per_turn_triggers.get(
                        once_per_turn_key) == game_state.turn:
                    return False

            # Parse for any conditional clause in the trigger text
            condition_clause = getattr(self, 'intervening_if', None) or self._extract_condition_clause(self.effect_text)
            
            # If there's a conditional clause, evaluate it
            if condition_clause:
                if not self._evaluate_condition(condition_clause, context):
                    return False
            
            # Check explicitly added additional condition if present
            if self.additional_condition and context:
                if not self._check_additional_condition(context):
                    return False

            if once_per_turn_key is not None:
                context["game_state"].once_per_turn_triggers[
                    once_per_turn_key] = context["game_state"].turn
                    
            return True
                    
        return False

    def resolve_with_targets(self, game_state, controller, targets=None, context=None):
        """Resolve this ability with specific targets."""
        if not self._intervening_if_met(game_state, controller, context):
            logging.debug(f"CR 603.4: intervening 'if' no longer true at resolution; ability does nothing: {self.effect_text}")
            return False  # Fizzle convention: resolves, does nothing
        return self._resolve_ability_implementation(
            game_state, controller, targets, resolution_context=context)


             

    def _card_matches_criteria(self, card, criteria):
         """Basic check if card matches simple criteria. (Helper)"""
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

    def _evaluate_condition(self, condition_text, context):
        """Evaluate if a trigger's conditional clause is met."""
        if not condition_text or not context: return True
        gs = context.get('game_state')
        controller = context.get('controller') # Controller of the trigger source
        if not gs or not controller: return True

        normalized_condition = str(condition_text).lower().strip(" .,;")
        last_known = context.get("last_known") or {}
        if ("if it's not suspected" in normalized_condition
                or "if it is not suspected" in normalized_condition):
            return self.card_id not in controller.setdefault(
                'suspected_permanents', set())
        if ("if this creature is suspected" in normalized_condition
                or "if it's suspected" in normalized_condition):
            return self.card_id in controller.setdefault(
                'suspected_permanents', set())
        if normalized_condition in {"if it was a creature", "it was a creature"}:
            return bool(last_known.get("was_creature", False))
        if normalized_condition in {"if you cast it", "you cast it"}:
            controller_id = "p1" if controller is gs.p1 else "p2"
            return bool(
                context.get("was_cast")
                and context.get("cast_controller_id") == controller_id)
        if normalized_condition in {"if it was cast", "it was cast"}:
            # Unlike "if you cast it", this wording asks only whether the
            # entering object came from a completed cast transaction.  The
            # stack captures that provenance before the permanent moves, so a
            # card put directly onto the battlefield does not qualify.
            return bool(context.get("was_cast"))

        source_counter_match = re.fullmatch(
            r"if it has (\w+|\d+) or more ([\w+/-]+) counters? on it",
            normalized_condition, re.IGNORECASE)
        if source_counter_match:
            threshold = text_to_number(source_counter_match.group(1))
            if not isinstance(threshold, int):
                threshold = int(source_counter_match.group(1))
            source = gs._safe_get_card(self.card_id)
            return bool(source and int(getattr(
                source, 'counters', {}).get(
                    source_counter_match.group(2).lower(), 0) or 0)
                >= threshold)

        mana_spent_match = re.fullmatch(
            r"if\s+((?:\{[wubrgc]\})+)\s+was spent to cast it",
            normalized_condition, re.IGNORECASE)
        if mana_spent_match:
            required = {}
            for symbol in re.findall(r"\{([wubrgc])\}",
                                     mana_spent_match.group(1),
                                     re.IGNORECASE):
                symbol = symbol.upper()
                required[symbol] = required.get(symbol, 0) + 1
            details = context.get("final_paid_details", {})
            spent = (details.get("spent_specific", {})
                     if isinstance(details, dict) else {})
            return all(
                int(spent.get(symbol, 0) or 0) >= count
                for symbol, count in required.items())

        toughness_match = re.search(
            r"if its toughness is (\d+) or (less|greater)",
            normalized_condition)
        if toughness_match:
            subject_id = context.get("attacker_id", context.get("event_card_id"))
            subject = gs._safe_get_card(subject_id) if subject_id is not None else None
            if subject is None:
                return False
            threshold = int(toughness_match.group(1))
            toughness = int(getattr(subject, "toughness", 0) or 0)
            return (toughness <= threshold if toughness_match.group(2) == "less"
                    else toughness >= threshold)

        # Delirium: "if there are four or more card types among cards in your
        # graveyard" counts DISTINCT card types, not cards.
        delirium_match = re.search(
            r"(\w+) or more card types among cards in your graveyard",
            normalized_condition)
        if delirium_match:
            needed = text_to_number(delirium_match.group(1))
            if not isinstance(needed, int) or needed <= 0:
                needed = 4
            types_found = set()
            for cid in controller.get("graveyard", []):
                card = gs._safe_get_card(cid)
                if card:
                    types_found.update(
                        t.lower() for t in getattr(card, 'card_types', []))
            types_found.discard("token")
            types_found.discard("unknown")
            return len(types_found) >= needed

        # Use the card evaluator for condition checking if available
        if hasattr(gs, 'card_evaluator') and gs.card_evaluator and hasattr(gs.card_evaluator, 'evaluate_condition'):
            try:
                # Pass context and condition text
                return gs.card_evaluator.evaluate_condition(condition_text, context)
            except NotImplementedError:
                logging.warning(f"CardEvaluator does not implement condition: {condition_text}")
            except Exception as e:
                logging.error(f"Error evaluating condition via CardEvaluator: {e}")

        # --- Basic Fallback Parsing ---
        logging.debug(f"Evaluating basic condition: '{condition_text}'")
        opponent = gs.p2 if controller == gs.p1 else gs.p1

        def matching_count(player, criteria):
            return sum(
                1 for cid in player.get("battlefield", [])
                if self._card_matches_criteria(
                    gs._safe_get_card(cid), criteria.rstrip("s")))

        numeric_control = re.search(
            r"if you control (one|two|three|four|five|\d+) or more "
            r"([\w\s-]+)$", normalized_condition)
        if numeric_control:
            needed = text_to_number(numeric_control.group(1))
            if not isinstance(needed, int):
                needed = int(numeric_control.group(1))
            return matching_count(
                controller, numeric_control.group(2).strip()) >= needed

        no_control = re.search(
            r"if you (?:control no|don't control (?:a|an|any))\s+"
            r"([\w\s-]+)$", normalized_condition)
        if no_control:
            return matching_count(controller, no_control.group(1).strip()) == 0

        opponent_no_control = re.search(
            r"if an opponent controls no\s+([\w\s-]+)$",
            normalized_condition)
        if opponent_no_control:
            return matching_count(
                opponent, opponent_no_control.group(1).strip()) == 0

        if normalized_condition in {
                "if it's your turn", "if it is your turn",
                "if this is your turn"}:
            return gs._get_active_player() is controller
        if normalized_condition in {
                "if it's not your turn", "if it is not your turn"}:
            return gs._get_active_player() is not controller
        if "if you attacked this turn" in normalized_condition:
            return any(
                gs.get_card_controller(cid) is controller
                for cid in getattr(gs, "attackers_this_turn", []))
        if "if a creature died this turn" in normalized_condition:
            return sum((getattr(gs, "creatures_died_this_turn", {}) or {}).values()) > 0
        if "if you gained life this turn" in normalized_condition:
            return bool(controller.get("gained_life_this_turn")
                        or getattr(gs, "life_gained_this_turn", {}).get(
                            "p1" if controller is gs.p1 else "p2", 0))
        if "if you lost life this turn" in normalized_condition:
            return bool(controller.get("lost_life_this_turn"))
        if normalized_condition in {
                "if you have no cards in hand", "if your hand is empty"}:
            return not controller.get("hand", [])
        if "if you have more cards in hand than an opponent" in normalized_condition:
            return len(controller.get("hand", [])) > len(opponent.get("hand", []))
        if "if you have fewer cards in hand than an opponent" in normalized_condition:
            return len(controller.get("hand", [])) < len(opponent.get("hand", []))

        # Check "if you control..."
        control_match = re.search(r"if you control (?:a|an|another|\d+)?\s*([\w\s\-]+?)(?: with|$)", condition_text)
        if control_match:
            required_type = control_match.group(1).strip()
            return any(self._card_matches_criteria(gs._safe_get_card(cid), required_type)
                       for cid in controller.get("battlefield", []))

        # Check opponent control
        opp_control_match = re.search(r"if an opponent controls (?:a|an|\d+)?\s*([\w\s\-]+?)(?: with|$)", condition_text)
        if opp_control_match:
            required_type = opp_control_match.group(1).strip()
            return any(self._card_matches_criteria(gs._safe_get_card(cid), required_type)
                       for cid in opponent.get("battlefield", []))

        # Check life total. BUGFIX (July 2026): a matched pattern must return
        # its actual result; previously a matched-but-false condition fell
        # through to the "assume True" default, so intervening "if" checks
        # could never come back False.
        life_match = re.search(r"if (you have|your life total is) (\d+) or more life", condition_text)
        if life_match: return controller["life"] >= int(life_match.group(2))
        life_match = re.search(r"if (you have|your life total is) (\d+) or less life", condition_text)
        if life_match: return controller["life"] <= int(life_match.group(2))

        # Check card count in hand/graveyard
        card_count_match = re.search(r"if you have (\d+) or more cards in (your hand|your graveyard)", condition_text)
        if card_count_match:
             count = int(card_count_match.group(1))
             zone = card_count_match.group(2).replace("your ", "")
             return len(controller.get(zone, [])) >= count

        if "you've cast another spell this turn" in normalized_condition:
            return sum(
                1 for entry in getattr(gs, "spells_cast_this_turn", [])
                if isinstance(entry, tuple) and len(entry) > 1
                and entry[1] is controller) >= 2

        logging.warning(
            f"Could not parse trigger condition: '{condition_text}'. "
            "Failing closed.")
        fidelity = getattr(gs, "fidelity_counters", None)
        if fidelity is not None:
            fidelity["unparsed_effects"] += 1
            source = gs._safe_get_card(self.card_id)
            fidelity.setdefault("unparsed_cards", set()).add(
                getattr(source, "name", f"card_{self.card_id}"))
        return False
    
    def _extract_condition_clause(self, text):
        """Return the intervening 'if' clause (CR 603.4) from ability text, or None.

        NOTE: this method was previously called from can_trigger but never
        existed -- a latent AttributeError masked by the trigger-parse bug.
        The clause is normally extracted once in __init__ (self.intervening_if);
        this re-derives it from raw text as a fallback.
        """
        if getattr(self, 'intervening_if', None):
            return self.intervening_if
        m = re.search(r'(?:when|whenever|at)\b[^,]*,\s*if\s+([^,]+?),', text or '', re.IGNORECASE)
        return ("if " + m.group(1).strip()) if m else None

    def _intervening_if_met(self, game_state, controller, context=None):
        """Evaluate the intervening 'if' right now (used at resolution, CR 603.4)."""
        cond = getattr(self, 'intervening_if', None)
        if not cond:
            return True
        resolution_context = dict(context or {})
        resolution_context.update({'game_state': game_state, 'controller': controller})
        return self._evaluate_condition(cond, resolution_context)

    def _check_additional_condition(self, context):
        """Checks self.additional_condition using the same evaluation logic."""
        if not self.additional_condition: return True
        # Callable conditions (Offspring's cost-paid check, the synthesized
        # Impending tick) take the trigger context directly; passing them to
        # the TEXT evaluator raised, and check_abilities' per-ability
        # exception handler silently dropped the trigger.
        if callable(self.additional_condition):
            try:
                return bool(self.additional_condition(context))
            except Exception as e:
                logging.error(f"Error in callable additional_condition: {e}")
                return False
        return self._evaluate_condition(self.additional_condition, context)
    

    def resolve(self, game_state, controller, targets=None, context=None):
        """Resolve this triggered ability using the default implementation."""
        if not self._intervening_if_met(game_state, controller, context):
            logging.debug(f"CR 603.4: intervening 'if' no longer true at resolution; ability does nothing: {self.effect_text}")
            return False  # Fizzle convention: resolves, does nothing
        return super()._resolve_ability_implementation(
            game_state, controller, targets, resolution_context=context)


class BoundExileTriggeredAbility(TriggeredAbility):
    """A reflexive trigger that exiles one previously chosen zone object.

    ``that card`` in Mind Swap is not a target.  Binding the physical card at
    creation time preserves that distinction and lets the trigger resolve as
    a no-op if the card has left its expected zone in response.
    """

    def __init__(self, source_id, bound_card_id, bound_zone="graveyard",
                 bound_zone_generation=None, bound_owner_id=None):
        super().__init__(
            source_id,
            trigger_condition="when you do",
            effect="exile that card",
            effect_text="When you do, exile that card.")
        self.bound_card_id = bound_card_id
        self.bound_zone = str(bound_zone or "graveyard").lower()
        self.bound_zone_generation = bound_zone_generation
        self.bound_owner_id = bound_owner_id
        self.requires_target = False
        self._is_reflexive_trigger = True

    def resolve_with_targets(self, game_state, controller, targets=None,
                             context=None):
        bound_card = game_state._safe_get_card(self.bound_card_id)
        current_generation = getattr(
            bound_card, "_zone_change_generation", None)
        if (self.bound_zone_generation is not None
                and current_generation != self.bound_zone_generation):
            logging.debug(
                "Bound exile resolved after %s changed zones.",
                self.bound_card_id)
            return True
        if self.bound_owner_id == "p1":
            owner = game_state.p1
        elif self.bound_owner_id == "p2":
            owner = game_state.p2
        else:
            owner = next(
                (player for player in (game_state.p1, game_state.p2)
                 if self.bound_card_id in player.get(self.bound_zone, [])),
                None)
        if (owner is None
                or self.bound_card_id not in owner.get(self.bound_zone, [])):
            logging.debug(
                "Bound exile resolved with %s no longer in %s.",
                self.bound_card_id, self.bound_zone)
            return True
        return bool(game_state.move_card(
            self.bound_card_id, owner, self.bound_zone, owner, "exile",
            cause="reflexive_exile", context={"source_id": self.card_id}))


class StaticAbility(Ability):
    """Continuous ability that affects the game state"""
    def __init__(self, card_id, effect, effect_text=""):
        super().__init__(card_id, effect_text)
        self.effect = effect.lower() if effect else "" # Handle potential None effect
        # Set effect_text from effect if not provided
        if not effect_text and self.effect:
            self.effect_text = self.effect.capitalize()

    def _parse_static_keyword_grants(self, effect_lower_clean):
        """Return every canonical keyword granted by a static declaration."""
        match = re.search(
            r'\b(?:have|has|gain|gains)\s+(.+?)$', effect_lower_clean)
        if not match:
            return []
        grant_text = match.group(1).strip().strip('. ')
        grant_text = re.sub(r',\s*and\s+', ',', grant_text)
        grant_text = re.sub(r'\s+and\s+', ',', grant_text)
        values = []
        canonical = {keyword.lower(): keyword for keyword in Card.ALL_KEYWORDS}
        for part in (piece.strip() for piece in grant_text.split(',')):
            if not part:
                continue
            if part in canonical:
                values.append(canonical[part])
                continue
            if part.startswith('ward'):
                values.append(part)
                continue
            if part.startswith('protection from '):
                values.append(part)
                continue
            # A non-keyword item means this is not a pure keyword bundle.
            return []
        return values

    def _dynamic_affected_scope(self, effect_lower_clean):
        """Describe scopes whose membership must be recomputed each layer pass."""
        if re.search(r"\bnonbasic lands?\b", effect_lower_clean):
            return {
                'players': 'all',
                'all_card_types': ('land',),
                'excluded_supertypes': ('basic',),
            }
        compound_scopes = {
            r'\benchantment creatures? you control\b':
                ('enchantment', 'creature'),
            r'\bartifact creatures? you control\b':
                ('artifact', 'creature'),
        }
        for pattern, required_types in compound_scopes.items():
            if re.search(pattern, effect_lower_clean):
                return {
                    'player': 'controller',
                    'all_card_types': required_types,
                }
        return None

    def apply(self, game_state, affected_cards=None):
        """Register the static ability's effect with the LayerSystem. Validates controller."""
        gs = game_state # Alias

        # --- 1. Check Controller and Zone ---
        # Static abilities only function while the source is on the battlefield typically
        card_owner, card_zone = gs.find_card_location(self.card_id)
        if not card_owner or card_zone != 'battlefield':
            # This static ability source isn't on the battlefield, do not apply.
            # Log if expected to be applied but isn't found.
            # logging.debug(f"StaticAbility source {self.card_id} not on battlefield (Zone: {card_zone}). Skipping apply.")
            return False # Signal that application was skipped/failed due to zone

        # Use the determined owner as the controller for this ability instance
        controller = card_owner

        # --- 2. Pre-validation: Check if effect looks like non-static ---
        # Prevent attempting to register activated/triggered abilities as static layers
        non_static_pattern = r'^\s*(\{.*?\}|tap|sacrifice|pay\s\d+\slife|discard|remove.*?counter)\s*[:—\u2014]' # Matches Cost: Effect
        trigger_pattern = r'^\s*(when|whenever|at)\b'
        # Also check for explicit action verbs less common in static abilities applied continuously
        action_verbs = r'\b(destroy|exile|counter|return target|deals? damage|create token|search|target player draws?|target player loses?)\b'

        if re.match(non_static_pattern, self.effect) or re.match(trigger_pattern, self.effect) or re.search(action_verbs, self.effect):
             # Log only if it wasn't caught by the parser earlier (this is a double-check)
             # logging.warning(f"StaticAbility.apply skipped: Effect text '{self.effect_text}' resembles activated/triggered/action ability.")
             return False # Do not register this with LayerSystem

        # --- 3. Proceed with Registration ---
        if not hasattr(gs, 'layer_system') or not gs.layer_system:
            logging.warning(f"Layer system not found, cannot apply static ability: {self.effect_text}")
            return False

        # Clean effect text for layer determination
        effect_lower_clean = self.effect.lower().strip('.—\u2014: ')
        layer = self._determine_layer_for_effect(effect_lower_clean)
        dynamic_scope = self._dynamic_affected_scope(effect_lower_clean)

        if layer is None:
            logging.warning(f"StaticAbility.apply: Could not determine layer for static effect: '{self.effect_text}'")
            return False

        if affected_cards is None:
            affected_cards = self.get_affected_cards(game_state, controller)
            if affected_cards is None: affected_cards = [] # Ensure list

        # --- Parse and Register Potentially Multiple Layer Effects ---
        # Handle complex static abilities that affect multiple layers (like Kaito)
        parsed_effects_data = self._parse_multi_layer_effect(
            effect_lower_clean, game_state, controller)
        registered_count = 0

        if parsed_effects_data: # Got specific parsed data
            for layer_data in parsed_effects_data:
                final_data = {
                    'source_id': self.card_id,
                    'affected_ids': affected_cards, # Apply to same targets unless overridden
                    'effect_text': self.effect_text, # Store original text
                    'source_ability': effect_lower_clean,
                    'duration': 'permanent',
                    'condition': lambda gs_check: (self.card_id in controller.get("battlefield", [])), # Standard condition
                    'controller_id': controller,
                    **layer_data # Merge parsed layer, sublayer, type, value
                }
                if dynamic_scope:
                    final_data['affected_scope'] = dynamic_scope
                if game_state.layer_system.register_effect(final_data):
                    registered_count += 1
            if registered_count > 0:
                 logging.debug(f"Registered {registered_count} layer effects from static ability '{self.effect_text}'")
                 return True
            else:
                 logging.warning(f"Parsed multi-layer data for '{self.effect_text}', but failed to register any.")
                 return False

        else: # Fallback: Try to parse as single layer effect (less robust)
            parsed_data_single = None
            try:
                if layer == 7: parsed_data_single = self._parse_layer7_effect(effect_lower_clean)
                elif layer == 6: parsed_data_single = self._parse_layer6_effect(effect_lower_clean)
                elif layer == 5: parsed_data_single = self._parse_layer5_effect(effect_lower_clean)
                elif layer == 4: parsed_data_single = self._parse_layer4_effect(effect_lower_clean)
                # Layers 1-3 less common for this kind of fallback
            except Exception as parse_e:
                logging.error(f"Error parsing single Layer {layer} effect '{self.effect_text}': {parse_e}", exc_info=True)

            if parsed_data_single:
                 effect_data = {
                     'source_id': self.card_id,
                     'layer': layer,
                     'affected_ids': affected_cards,
                     'effect_text': self.effect_text,
                     'source_ability': effect_lower_clean,
                     'duration': 'permanent',
                     'condition': lambda gs_check: (self.card_id in controller.get("battlefield", [])),
                     'controller_id': controller,
                     **parsed_data_single # Add sublayer, type, value
                 }
                 if dynamic_scope:
                     effect_data['affected_scope'] = dynamic_scope
                 if game_state.layer_system.register_effect(effect_data):
                      logging.debug(f"Registered static effect (single) '{self.effect_text}' in Layer {layer}")
                      return True
                 else:
                      logging.warning(f"Failed to register single-layer static effect '{self.effect_text}'")
                      return False
            else:
                 logging.warning(f"Static ability parser could not interpret effect (apply): '{self.effect_text}'")
                 return False


    def _parse_multi_layer_effect(self, effect_lower_clean, game_state, controller):
        """
        Attempt to parse complex static abilities that affect multiple layers.
        Returns a list of effect data dictionaries, one for each layer/sublayer.
        Returns None if no complex pattern is matched.
        """
        keyword_grants = self._parse_static_keyword_grants(effect_lower_clean)
        if len(keyword_grants) > 1:
            return [
                {
                    'layer': 6,
                    'effect_type': 'add_ability',
                    'effect_value': keyword,
                }
                for keyword in keyword_grants
            ]

        # Example: Kaito: "During your turn, as long as Kaito has one or more loyalty counters on him, he's a 3/4 Ninja creature and has hexproof."
        kaito_match = re.match(r"(during your turn,)?\s*(as long as .+?,)?\s*(?:it's|he's|she's)\s+a\s+(\d+)/(\d+)\s+(.*?)\s+creature(?: and has (.*?))?(?:\.|$)", effect_lower_clean)
        if kaito_match:
            turn_restriction, condition_part, power_str, toughness_str, types_part, extra_keywords = kaito_match.groups()
            power = safe_int(power_str); toughness = safe_int(toughness_str)

            def conditional_func(gs):
                live_controller = gs.get_card_controller(self.card_id)
                if not live_controller or self.card_id not in live_controller.get("battlefield", []):
                    return False
                if turn_restriction and gs._get_active_player() is not live_controller:
                    return False
                if ("loyalty counter" in (condition_part or "")
                        and live_controller.get("loyalty_counters", {}).get(self.card_id, 0) <= 0):
                    return False
                return True

            # Base effect data for this ability
            base_data = {'affected_ids': [self.card_id], 'condition': conditional_func}

            effects = []
            # Layer 4: Add types (e.g., "Ninja")
            subtype_words = [
                word.strip().lower() for word in types_part.split()
                if word.strip() and word.strip().lower() not in Card.ALL_CARD_TYPES
            ]
            effects.append({
                **base_data, 'layer': 4, 'effect_type': 'set_type',
                'effect_value': ["creature"]})
            effects.append({
                **base_data, 'layer': 4, 'effect_type': 'set_subtype',
                'effect_value': subtype_words})
            # Layer 6: Add keywords (e.g., "hexproof")
            if extra_keywords:
                 keywords_to_add = [kw.strip() for kw in extra_keywords.split('and') if kw.strip()]
                 for kw in keywords_to_add:
                     if kw in Card.ALL_KEYWORDS:
                          effects.append({**base_data, 'layer': 6, 'effect_type': 'add_ability', 'effect_value': kw})
            # Layer 7b: Set P/T
            effects.append({**base_data, 'layer': 7, 'sublayer': 'b', 'effect_type': 'set_pt', 'effect_value': (power, toughness)})
            return effects

        # Add patterns for other multi-layer static abilities here

        return None # No multi-layer pattern matched
        
    def _parse_layer1_effect(self, effect_lower):
        """Parse continuous copy effects for Layer 1 (Rare for static abilities)."""
        # Examples: "Creatures you control are copies of X" (X needs context)
        # Copy effects are usually established by spells/ETBs. Static abilities
        # granting copy status continuously are very rare and hard to parse generically.
        # This parser will look for simple markers but may not be fully functional
        # without knowing the target of the copy effect.

        copy_match = re.search(r"\b(is|are)\s+(a\s+)?copy of\s+(.+)", effect_lower)
        if copy_match:
            target_description = copy_match.group(3).strip()
            # Problem: Need to resolve 'target_description' to a specific card ID
            # which usually happens when the copy effect is created, not via static text.
            # We can register a marker effect, but LayerSystem needs the target ID.
            logging.warning(f"Layer 1 'copy' effect found ('{effect_lower}'), but target '{target_description}' cannot be resolved generically from static text. Effect may not apply correctly.")
            # For now, return a placeholder or None, as LayerSystem copy needs a target ID.
            # return {'effect_type': 'become_copy', 'effect_value': target_description} # Placeholder
            return None

        # Keyword "Changeling" is technically Layer 1-ish (sets types) but handled as Layer 4/6 usually.
        # If "changeling" is the *only* effect text, it implies type/ability setting.
        if effect_lower == "changeling":
             # Let Layer 4 handle the type setting, Layer 6 handle ability implications.
             return None

        return None # No common static Layer 1 effect parsed

    def _parse_layer2_effect(self, effect_lower):
        """Parse continuous control-changing effects for Layer 2 (Very rare for static abilities)."""
        # Examples: "You control target creature." (This is usually established by the effect resolution)
        # An Aura like "Control Magic" establishes this, but it's tied to the Aura's attachment state.
        # A static ability on Permanent A granting control of Permanent B continuously without targeting
        # is almost non-existent.

        gain_control_match = re.search(r"\b(gain|have)\s+control of\s+(.+)", effect_lower)
        if gain_control_match:
             target_description = gain_control_match.group(2).strip()
             # Similar to Layer 1, static control gain needs a target defined elsewhere.
             logging.warning(f"Layer 2 'control' effect found ('{effect_lower}'), but target '{target_description}' cannot be resolved generically from static text. Effect may not apply correctly.")
             # Returning None as control changes are typically handled by the source effect's resolution.
             return None

        return None # No common static Layer 2 effect parsed

    def _parse_layer3_effect(self, effect_lower):
        """Parse continuous text-changing effects for Layer 3 (Extremely rare)."""
        # Examples: "Creatures named X have text Y" (very specific and rare)
        # Most text-implication effects (like losing abilities) are handled functionally in Layer 6.
        # Literal text replacement is usually an activated/triggered ability (e.g., Mind Bend).

        text_change_match = re.search(r"text becomes\s+['\"](.+)['\"]", effect_lower)
        if text_change_match:
            new_text = text_change_match.group(1).strip()
            # Need to know the target subject of the text change.
            logging.warning(f"Layer 3 'text becomes' effect found ('{effect_lower}'), but determining target/subject generically is complex. Effect may not apply correctly.")
            # return {'effect_type': 'change_text', 'effect_value': new_text} # Placeholder
            return None

        # "Loses all abilities" implies text change but is handled functionally in Layer 6.
        # We avoid double-registering it here.

        return None # No common static Layer 3 effect parsed
        
    def _parse_layer7_effect(self, effect_lower):
        """Parse P/T effects for Layer 7."""
        # Layer 7a: Set Base P/T (e.g., from copy effects or abilities setting base)
        match = re.search(r"(?:base power and toughness|base power|base toughness)\s+(?:is|are)\s+(\d+)/(\d+)", effect_lower)
        if match:
            power = safe_int(match.group(1)); toughness = safe_int(match.group(2))
            if power is not None and toughness is not None:
                 return {'sublayer': 'a', 'effect_type': 'set_base_pt', 'effect_value': (power, toughness)}
        # Handle Characteristic-Defining Abilities setting base P/T
        match_cda = re.search(r"(?:power and toughness are each equal to|power is equal to|toughness is equal to)\b", effect_lower)
        if match_cda:
             # Register CDA P/T setting effect, actual calculation deferred to LayerSystem application
             cda_type = 'unknown'
             if "number of cards in your graveyard" in effect_lower: cda_type = 'graveyard_count_self'
             elif "number of creatures you control" in effect_lower: cda_type = 'creature_count_self'
             elif ("power is equal to the number of lands you control"
                   in effect_lower): cda_type = 'land_count_power_self'
             else:
                 subtype_count = re.search(
                     r"\bpower is equal to the number of\s+"
                     r"([a-z][\w'-]*)\s+you control\b",
                     effect_lower)
                 if subtype_count:
                     cda_type = {
                         'kind': 'subtype_count_power_self',
                         'subtype': subtype_count.group(1).lower(),
                     }
             # Add more common CDA types
             logging.debug(f"Registering Layer 7a CDA effect: {cda_type}")
             return {'sublayer': 'a', 'effect_type': 'set_pt_cda', 'effect_value': cda_type} # Pass CDA type identifier

        # Layer 7b: Setting P/T to specific values (without changing base P/T). Examples: "becomes a 1/1", "is a 0/1"
        # Note: These often come with type changes in Layer 4. Layer 7 only handles the P/T part.
        match = re.search(r"\bis a\b\s+(\d+)/(\d+)", effect_lower) or re.search(r"\bbecomes a\b\s+(\d+)/(\d+)", effect_lower)
        if match:
             power = safe_int(match.group(1)); toughness = safe_int(match.group(2))
             if power is not None and toughness is not None:
                  return {'sublayer': 'b', 'effect_type': 'set_pt', 'effect_value': (power, toughness)}

        # Layer 7c: P/T modification from static abilities (+X/+Y, -X/-Y), anthems etc.
        # Simple +/- N/N modifications
        match = re.search(r"gets? ([+\-]\d+)/([+\-]\d+)", effect_lower)
        if match:
            p_mod = safe_int(match.group(1)); t_mod = safe_int(match.group(2))
            if p_mod is not None and t_mod is not None:
                 return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Anthem patterns (+N/+N)
        match = re.search(r"(?:get|have)\s*\+\s*(\d+)/\+\s*(\d+)", effect_lower)
        if match:
            p_mod = safe_int(match.group(1)); t_mod = safe_int(match.group(2))
            if p_mod is not None and t_mod is not None:
                 return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Penalty patterns (-N/-N)
        match = re.search(r"(?:get|have)\s*\-\s*(\d+)/\-\s*(\d+)", effect_lower)
        if match:
            p_mod = -safe_int(match.group(1), 0); t_mod = -safe_int(match.group(2), 0)
            if p_mod is not None and t_mod is not None: # Check result of safe_int
                return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Variable P/T modification (e.g., +X/+X where X is count)
        match_var = re.search(r"(?:get|have)\s*\+X/\+X\s+where X is the number of (\w+)", effect_lower)
        if match_var:
             count_type = match_var.group(1).strip()
             # Register variable P/T effect, calculation deferred
             logging.debug(f"Registering Layer 7c variable P/T effect based on: {count_type}")
             return {'sublayer': 'c', 'effect_type': 'modify_pt_variable', 'effect_value': count_type}

        # Layer 7d: Switch P/T
        if "switch" in effect_lower and "power and toughness" in effect_lower:
            return {'sublayer': 'd', 'effect_type': 'switch_pt', 'effect_value': True}

        return None # No Layer 7 effect parsed

    def _parse_layer6_effect(self, effect_lower_clean):
        """Parse ability adding/removing effects for Layer 6. (Uses cleaned text)"""
        # Check for removal first (more specific patterns)
        if "lose all abilities" in effect_lower_clean:
            return {'effect_type': 'remove_all_abilities', 'effect_value': True}

        # Keyword registration stores parameterized Ward internally as
        # ``ward <cost>`` (including ``ward ward_generic``).  Its human-facing
        # effect text says "This permanent has ward", but parsing uses the
        # internal title.  Preserve the base keyword here; the registered
        # ability retains the actual payment detail separately.
        if re.fullmatch(r"ward(?:\s+.+)?", effect_lower_clean):
            return {'effect_type': 'add_ability', 'effect_value': 'Ward'}

        keyword_grants = self._parse_static_keyword_grants(effect_lower_clean)
        if keyword_grants:
            return {
                'effect_type': 'add_ability',
                'effect_value': keyword_grants[0],
            }

        # Simple "loses X" check - uses cleaned text
        lose_match = re.search(r"loses ([\w\s\-]+?)(?: and |,|$)", effect_lower_clean) # Removed check for trailing punctuation as it should be stripped
        if lose_match:
            ability_to_lose = lose_match.group(1).strip()
            # Normalize: Check against canonical keywords
            normalized_kw_lose = None
            for official_kw in Card.ALL_KEYWORDS:
                 # Use exact match after cleaning
                 if ability_to_lose == official_kw.lower():
                     normalized_kw_lose = official_kw # Use canonical name
                     break
            if normalized_kw_lose:
                 # Found a standard keyword being lost
                 return {'effect_type': 'remove_ability', 'effect_value': normalized_kw_lose}
            else:
                logging.debug(f"Potential non-keyword ability loss detected: '{ability_to_lose}' (not standard)")

        # Check for additions: "gains/has [ability list]" - uses cleaned text
        # Regex updated to stop at potential separators or end of string reliably
        gain_match = re.search(r"\b(have|has|gains?|gain)\s+(.*?)(?: and |,| until| —|\u2014|$)", effect_lower_clean)
        if gain_match:
            gained_abilities_text = gain_match.group(2).strip()
            # Split potential list by comma
            potential_gains = gained_abilities_text.split(',')
            # Process first matched keyword (refine later if multiple needed per effect)
            for potential_kw_phrase in potential_gains:
                potential_kw_phrase = potential_kw_phrase.strip()
                if not potential_kw_phrase: continue

                # Handle parametrized keywords explicitly first
                if potential_kw_phrase.startswith("protection from"):
                    # Use safer splitting
                    parts = potential_kw_phrase.split("protection from", 1)
                    if len(parts) == 2:
                        protected_from_value = parts[1].strip()
                        return {'effect_type': 'add_ability', 'effect_value': f"protection from {protected_from_value}"}
                elif potential_kw_phrase.startswith("ward"):
                    # Regex for ward cost ({X}, N, Pay X life etc.) - improved
                    ward_cost_match = re.match(r"ward\s*(?:-|—)?\s*(\{.*?\})$|\bward\s*(\d+)$|\bward\s*(pay \d+ life|discard a card)", potential_kw_phrase)
                    ward_cost = "{1}" # Default ward {1}
                    if ward_cost_match:
                         cost_part = ward_cost_match.group(1) or ward_cost_match.group(2) or ward_cost_match.group(3)
                         if cost_part:
                              if cost_part.isdigit(): ward_cost = f"{{{cost_part}}}"
                              else: ward_cost = cost_part.strip() # Takes {X}, pay N life, discard...
                    return {'effect_type': 'add_ability', 'effect_value': f"ward {ward_cost}"}

                # Check simple keywords against canonical list (using cleaned phrase)
                for official_kw in Card.ALL_KEYWORDS:
                    if potential_kw_phrase == official_kw.lower():
                        return {'effect_type': 'add_ability', 'effect_value': official_kw}
                # If it gets here after checking a phrase part, it wasn't a recognized keyword
                break # Move to next check after first phrase part processed

        # Check specific "can't attack/block" / "must attack/block" phrases - use cleaned text
        if "can't attack" in effect_lower_clean: return {'effect_type': 'add_ability', 'effect_value': 'cant_attack'}
        if "can't block" in effect_lower_clean: return {'effect_type': 'add_ability', 'effect_value': 'cant_block'}
        if "attacks each combat if able" in effect_lower_clean or "must attack if able" in effect_lower_clean:
            return {'effect_type': 'add_ability', 'effect_value': 'must_attack'}
        if "blocks each combat if able" in effect_lower_clean or "must block if able" in effect_lower_clean:
            return {'effect_type': 'add_ability', 'effect_value': 'must_block'}

        # Check if the *entire* cleaned effect is just a keyword
        for official_kw in Card.ALL_KEYWORDS:
             if effect_lower_clean == official_kw.lower():
                  return {'effect_type': 'add_ability', 'effect_value': official_kw}
        # Handle comma separated lists like "Flying, lifelink"
        parts = [p.strip() for p in effect_lower_clean.split(',')]
        if len(parts) > 1 and all(p in [k.lower() for k in Card.ALL_KEYWORDS] for p in parts):
             # Need to return multiple effects? Or handle list? Return first for now.
             return {'effect_type': 'add_ability', 'effect_value': Card.ALL_KEYWORDS[[k.lower() for k in Card.ALL_KEYWORDS].index(parts[0])]} # Return canonical name of first


        return None # No Layer 6 effect parsed


    def _parse_layer5_effect(self, effect_lower):
        """Parse color adding/removing effects for Layer 5."""
        colors_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
        color_indices = {'W': 0, 'U': 1, 'B': 2, 'R': 3, 'G': 4}
        target_colors = None # None means no change from this effect
        effect_type = None

        # Check if SETTING specific colors (e.g., "is blue", "are white and black")
        # Matches "is [color]" or "are [color1] and [color2]" but NOT "is also"
        if re.search(r"\b(is|are)\b(?!\s+also)", effect_lower):
             is_setting = False
             found_colors_in_set = [0] * 5
             for color_name, index in colors_map.items():
                  if re.search(r'\b' + re.escape(color_name) + r'\b', effect_lower):
                       found_colors_in_set[index] = 1
                       is_setting = True
             # Check for "is colorless"
             if re.search(r'\bis colorless\b', effect_lower):
                  found_colors_in_set = [0] * 5
                  is_setting = True # Setting to colorless is a type of setting

             if is_setting:
                  effect_type = 'set_color'
                  target_colors = found_colors_in_set

        # Check if ADDING colors (e.g., "is also blue")
        elif re.search(r"\b(is also|are also)\b", effect_lower):
             added_colors = [0] * 5
             found_addition = False
             for color_name, index in colors_map.items():
                  if re.search(r'\b' + re.escape(color_name) + r'\b', effect_lower):
                       added_colors[index] = 1
                       found_addition = True
             if found_addition:
                  effect_type = 'add_color'
                  target_colors = added_colors

        # Check if removing colors / becoming colorless (if not caught by "is colorless")
        elif "loses all colors" in effect_lower or "becomes colorless" in effect_lower:
             effect_type = 'set_color'
             target_colors = [0,0,0,0,0]

        if effect_type and target_colors is not None:
             return {'effect_type': effect_type, 'effect_value': target_colors}

        return None # No Layer 5 effect parsed


    def _parse_layer4_effect(self, effect_lower):
        """Parse type/subtype adding/removing effects for Layer 4."""
        basic_land_match = re.search(
            r"\b(?:nonbasic )?lands? (?:is|are) "
            r"(plains|island|swamp|mountain|forest)s?\b",
            effect_lower)
        if basic_land_match:
            return {
                'effect_type': 'set_basic_land_type',
                'effect_value': basic_land_match.group(1),
            }
        # Patterns to detect type/subtype changes
        set_type_match = re.search(r"becomes? a(?:n)? ([\w\s]+?)(?: in addition| that's still|$)", effect_lower)
        add_type_match = re.search(r"(is|are) also a(?:n)? (\w+)", effect_lower)
        set_subtype_match = re.search(r"becomes? a(?:n)? ([\w\s]+?) creature", effect_lower)
        add_subtype_match = re.search(r"(?:is|are) also ([\w\s]+)", effect_lower)
        lose_type_match = re.search(r"loses all creature types", effect_lower) # Example removal

        # --- Process Type Setting/Adding ---
        # Handle "becomes TYPE..." / "is TYPE..."
        if set_type_match:
             type_text = set_type_match.group(1).strip()
             # Determine if it's setting or adding based on keywords
             is_addition = "in addition" in set_type_match.group(0) or "also a" in set_type_match.group(0) or "still a" in set_type_match.group(0)

             parts = type_text.split()
             types = [p for p in parts if p in Card.ALL_CARD_TYPES] # Filter known card types
             subtypes = [p.capitalize() for p in parts if p.capitalize() in Card.SUBTYPE_VOCAB] # Check known subtypes

             if types: # Change primary card types
                  effect_type = 'add_type' if is_addition else 'set_type'
                  logging.debug(f"Layer 4: Parsed {effect_type} with value {types}")
                  # set_type clears old types and subtypes unless specified together.
                  if not is_addition:
                      return {'effect_type': 'set_type_and_subtype', 'effect_value': (types, subtypes)}
                  else: # Just adding the type(s)
                      return {'effect_type': effect_type, 'effect_value': types}
             # If no main card types found, but parts exist, check subtypes
             elif subtypes and is_addition:
                 logging.debug(f"Layer 4: Parsed add_subtype from 'becomes/is also' clause: {subtypes}")
                 return {'effect_type': 'add_subtype', 'effect_value': subtypes}

        # Handle "is also a [type]" (Redundant with above, but safe fallback)
        elif add_type_match:
             type_text = add_type_match.group(2).strip()
             if type_text in Card.ALL_CARD_TYPES:
                  logging.debug(f"Layer 4: Parsed add_type with value {[type_text]}")
                  return {'effect_type': 'add_type', 'effect_value': [type_text]}
             elif type_text.capitalize() in Card.SUBTYPE_VOCAB: # Check if it's a subtype instead
                  logging.debug(f"Layer 4: Parsed add_subtype from 'is also a' clause: {[type_text.capitalize()]}")
                  return {'effect_type': 'add_subtype', 'effect_value': [type_text.capitalize()]}

        # --- Process Subtype Setting/Adding ---
        elif add_subtype_match: # "are also Saprolings"
             subtype_text = add_subtype_match.group(1).strip()
             potential_subtypes = [s.capitalize() for s in subtype_text.split() if s.capitalize() in Card.SUBTYPE_VOCAB]
             if potential_subtypes:
                  logging.debug(f"Layer 4: Parsed add_subtype with value {potential_subtypes}")
                  return {'effect_type': 'add_subtype', 'effect_value': potential_subtypes}

        # --- Process Type/Subtype Removal ---
        elif lose_type_match: # "loses all creature types"
             logging.debug("Layer 4: Parsed lose_all_subtypes (Creature)")
             # This effect is complex: Removes subtypes associated with 'creature' type.
             # Need better subtype mapping or specific LayerSystem handling.
             # For now, return a generic marker or handle in LayerSystem application.
             return {'effect_type': 'lose_subtype_by_type', 'effect_value': 'creature'}

        return None # No Layer 4 effect parsed

    def _determine_layer_for_effect(self, effect_lower):
        """Determine the appropriate layer for an effect based on its text. (Improved Pattern Matching)"""
        # Strip common punctuation and leading/trailing separators that might interfere
        cleaned_effect = effect_lower.strip('.—\u2014: ')

        # Layer 1: Copy effects
        if "copy" in cleaned_effect or "becomes a copy" in cleaned_effect: return 1
        # Layer 2: Control-changing effects
        if "gain control" in cleaned_effect or "exchange control" in cleaned_effect: return 2
        # Layer 3: Text-changing effects
        if "text becomes" in cleaned_effect: return 3

        # Layer 4: Type-changing effects
        # Check for "becomes [type]", "is also [type]", or specific type removals
        # Use word boundaries to avoid partial matches within other words
        type_pattern = r"\b(becomes?|is also|are also)\b.*\b(artifact|creature|enchantment|land|planeswalker|battle)\b"
        if (re.search(type_pattern, cleaned_effect)
                or "loses all creature types" in cleaned_effect
                or re.search(
                    r"\b(?:nonbasic )?lands? (?:is|are) "
                    r"(?:plains|island|swamp|mountain|forest)s?\b",
                    cleaned_effect)):
            return 4

        # Layer 5: Color-changing effects
        color_pattern = r"\b(is|are|becomes?)\b.*\b(white|blue|black|red|green|colorless)\b"
        if re.search(color_pattern, cleaned_effect) or "loses all colors" in cleaned_effect:
            return 5

        # Layer 6: Ability adding/removing effects
        # Use word boundaries for most keywords
        # Need to handle multi-word keywords and parametrized keywords like protection
        # Parsed keyword abilities can arrive as internal forms such as
        # ``ward ward_generic`` without a leading "has" or "gains" phrase.
        # They are still ability-layer effects.
        if self._parse_static_keyword_grants(cleaned_effect):
            return 6
        if any(
                cleaned_effect == keyword.lower()
                or cleaned_effect.startswith(f"{keyword.lower()} ")
                for keyword in Card.ALL_KEYWORDS):
            return 6
        for kw in Card.ALL_KEYWORDS:
            kw_lower = kw.lower()
            # Use word boundaries for single-word keywords, simple substring for multi-word
            pattern = r'\b' + re.escape(kw_lower) + r'\b' if ' ' not in kw_lower else re.escape(kw_lower)
            # Check if the text explicitly grants or removes this keyword
            if re.search(rf"\b(gains?|has|lose|loses)\b.*\b{pattern}", cleaned_effect):
                 return 6
        # Catch cases like "lose all abilities", "can't attack/block", "must attack/block"
        if "lose all abilities" in cleaned_effect: return 6
        if any(restriction in cleaned_effect for restriction in ["can't attack", "can't block", "must attack", "must block"]): return 6

        # Layer 7: Power/toughness changing effects
        pt_patterns = [
            r"([+\-]\d+)\s*/\s*([+\-]\d+)",  # +N/+M, -N/-M
            r"\b(base power and toughness|base power|base toughness)\s+(?:is|are)\b", # Set base P/T
            r"\b(is|are|becomes)\s+\d+/\d+", # Set P/T to specific value
            r"(?:power and toughness are each equal to|power is equal to|toughness is equal to)", # CDA P/T setting
            r"switch.*power and toughness" # Switch P/T
        ]
        if any(re.search(pattern, cleaned_effect) for pattern in pt_patterns):
            return 7

        # If no standard static effect pattern matched, return None
        # Avoid classifying activated/triggered text like "Exile target creature..."
        # Basic check: Does it contain common action verbs typical of non-static effects?
        non_static_verbs = [r'\bexile\b', r'\bdestroy\b', r'\bcounter\b', r'\btap\b', r'\buntap\b', r'\bdraw\b', r'\bdiscard\b', r'\bsacrifice\b', r'\bsearch\b']
        if any(re.search(verb, cleaned_effect) for verb in non_static_verbs):
            # If it looks like an activated/triggered effect text, don't assign a layer
            # Exception: If it ALSO contains "gains/has/loses", it might be Layer 6. Handled above.
            is_layer6 = False
            for kw in Card.ALL_KEYWORDS:
                 pattern = r'\b' + re.escape(kw.lower()) + r'\b' if ' ' not in kw.lower() else re.escape(kw.lower())
                 if re.search(rf"\b(gains?|has|lose|loses)\b.*\b{pattern}", cleaned_effect):
                      is_layer6 = True; break
            if not is_layer6: return None # Looks like non-static

        # Final check: If it's just a keyword like "Flying" or "Lifelink" alone. This is Layer 6.
        # Use word boundaries and match entire cleaned string for single keywords.
        for kw in Card.ALL_KEYWORDS:
             kw_lower = kw.lower()
             if kw_lower == cleaned_effect:
                  return 6
        # Handle comma separated lists like "Flying, lifelink"
        parts = [p.strip() for p in cleaned_effect.split(',')]
        if len(parts) > 1 and all(p in [k.lower() for k in Card.ALL_KEYWORDS] for p in parts):
            return 6

        # If unsure, return None or log warning
        # Returning None is safer to avoid misclassification.
        logging.warning(f"LayerSystem: Could not determine layer for effect text: '{effect_lower}' (Cleaned: '{cleaned_effect}')")
        return None

    def _find_all_battlefield_cards(self, game_state):
        """Helper function to find all cards on the battlefield."""
        battlefield_cards = []
        for player in [game_state.p1, game_state.p2]:
            battlefield_cards.extend(player["battlefield"])
        return battlefield_cards

    def get_affected_cards(self, game_state, controller):
        """Determine which cards this static ability affects (Improved Scope Parsing)"""
        effect_lower = self.effect.lower() if self.effect else ""
        affected_cards = []
        me = controller
        opp = game_state.p2 if me == game_state.p1 else game_state.p1

        # The controlled objects named inside a characteristic-defining
        # ability are the things being counted, not the things being modified.
        # For example, "Namor's power is equal to the number of Merfolk you
        # control" affects Namor alone; the generic "you control" scope below
        # must not turn every controlled permanent into a variable-power object.
        if re.search(
                r"(?:power and toughness are each equal to|"
                r"power is equal to|toughness is equal to)", effect_lower):
            return [self.card_id]

        # Common scopes using regex for more flexibility
        scopes = {
            r"\bnonbasic lands?\b": (None, "nonbasic_land"),
            r"\benchantment creatures? you control\b":
                (me, ("enchantment", "creature")),
            r"\bartifact creatures? you control\b":
                (me, ("artifact", "creature")),
            r"\bcreatures? you control\b": (me, "creature"),
            r"\bartifacts? you control\b": (me, "artifact"),
            r"\bpermanents? you control\b": (me, "permanent"),
            r"\blands? you control\b": (me, "land"),
            r"\bplaneswalkers? you control\b": (me, "planeswalker"),
            r"\bcreatures? opponents? control\b": (opp, "creature"),
            r"\bpermanents? opponents? control\b": (opp, "permanent"),
            r"\b(each|all) creatures?\b": (None, "creature"), # Affects both players
            r"\b(each|all) permanents?\b": (None, "permanent"),
            r"\b(each|all) artifacts?\b": (None, "artifact"),
            r"\b(each|all) enchantments?\b": (None, "enchantment"),
            r"\b(each|all) lands?\b": (None, "land"),
            r"\b(each|all) planeswalkers?\b": (None, "planeswalker"),
            r"\byou control\b": (me, "any"), # Generic "you control"
            r"opponents control\b": (opp, "any"), # Generic "opponents control"
            # More specific scopes like "attacking creatures", "untapped creatures", etc.
            r"\battacking creatures?\b": (None, "attacking_creature"),
            r"\bblocking creatures?\b": (None, "blocking_creature"),
            r"\buntapped creatures?\b": (None, "untapped_creature"),
            r"\btapped creatures?\b": (None, "tapped_creature"),
        }

        matched_scope = False
        for pattern, (player_scope, type_scope) in scopes.items():
            if re.search(pattern, effect_lower):
                players_to_check = []
                if player_scope is None: # Affects all players
                    players_to_check = [p for p in [me, opp] if p] # Check both if they exist
                else:
                    players_to_check.append(player_scope)

                for p in players_to_check:
                    if not p: continue # Skip if player is None
                    for card_id in p.get("battlefield", []): # Use get for safety
                         card = game_state._safe_get_card(card_id)
                         if self._card_matches_scope_criteria(card, type_scope, card_id, game_state, p):
                              affected_cards.append(card_id)
                matched_scope = True
                break # Stop after first matching scope (most specific should come first ideally)

        # Default: Affects the source card itself if no other scope matched
        if not matched_scope:
            affected_cards.append(self.card_id)

        # Remove duplicates and return
        return list(set(affected_cards))
    
    def _card_matches_scope_criteria(self, card, type_scope, card_id, game_state, player):
        """Helper to check if a card matches the scope criteria (type, state)."""
        if not card: return False
        # Check basic type
        card_types = getattr(card, 'card_types', [])
        if type_scope == "nonbasic_land":
            if ("land" not in card_types
                    or "basic" in {
                        str(value).lower()
                        for value in getattr(card, 'supertypes', [])}):
                return False
        elif isinstance(type_scope, (tuple, list, set)):
            if not all(required in card_types for required in type_scope):
                return False
        elif type_scope != "any":
            if type_scope != "permanent" and type_scope not in card_types and type_scope not in getattr(card,'subtypes',[]): # Allow subtype match
                return False # Type doesn't match

        # Check specific states
        if type_scope == "attacking_creature":
            if card_id not in getattr(game_state, 'current_attackers', []): return False
        elif type_scope == "blocking_creature":
             is_blocking = any(card_id in blockers for blockers in getattr(game_state, 'current_block_assignments', {}).values())
             if not is_blocking: return False
        elif type_scope == "tapped_creature":
             if 'creature' not in getattr(card, 'card_types', []) or card_id not in player.get("tapped_permanents", set()): return False
        elif type_scope == "untapped_creature":
             if 'creature' not in getattr(card, 'card_types', []) or card_id in player.get("tapped_permanents", set()): return False

        return True


class TargetingOverrideAbility(Ability):
    """A static rules exception that changes targeting or ward triggering.

    These effects do not add or remove an ability in layer 6. Their source is
    consulted while it remains on the battlefield, which also makes legality
    rechecks behave correctly if the source leaves before resolution.
    """

    def __init__(self, card_id, protection, effect_text=""):
        super().__init__(card_id, effect_text)
        self.targeting_override = str(protection).lower()
        self.scope = "opponent_creatures"


class ManaAbility(ActivatedAbility):
    """Special case of activated ability that produces mana"""
    def __init__(self, card_id, cost, mana_produced, effect_text=""):
        # Effect text derived implicitly for ManaAbility if not provided
        effect = f"Add {self._format_mana(mana_produced)}."
        if not effect_text:
            effect_text = f"{cost}: {effect}"
        super().__init__(card_id, cost, effect, effect_text)
        self.mana_produced = mana_produced # Expects dict like {'G': 1, 'C': 2}
        choice_match = re.search(
            r"\badd\s+\{([WUBRG])\}\s+or\s+\{([WUBRG])\}",
            effect_text, re.IGNORECASE)
        self.available_colors = (
            list(dict.fromkeys(symbol.upper()
                               for symbol in choice_match.groups()))
            if choice_match else [])


    def _format_mana(self, mana_dict):
        """Helper to format mana dict into string like {G}{G}{1}"""
        parts = []
        for color in ['W', 'U', 'B', 'R', 'G']:
             parts.extend([f"{{{color}}}"] * mana_dict.get(color, 0))
        if mana_dict.get('C', 0): parts.append(f"{{{mana_dict['C']}}}")
        if mana_dict.get('X', 0): parts.append(f"{{{mana_dict['X']}X}}") # How to represent X?
        # Add other types (Snow, Phyrexian, Hybrid) if needed
        return "".join(parts)

    def resolve(self, game_state, controller):
        """Add the produced mana to the controller's mana pool using ManaSystem"""
        if hasattr(game_state, 'mana_system') and game_state.mana_system:
             game_state.mana_system.add_mana(controller, self.mana_produced)
        else: # Fallback if no mana system
             for color, amount in self.mana_produced.items():
                  pool = controller.setdefault("mana_pool", {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0})
                  pool[color] = pool.get(color, 0) + amount
                  card_name = getattr(game_state._safe_get_card(self.card_id), 'name', self.card_id)
                  logging.debug(f"(Fallback) Mana ability of {card_name} added {amount} {color} mana.")
        return True # Mana abilities usually succeed if cost paid

class AbilityEffect:
    """Base class for ability effects with improved targeting integration."""
    def __init__(self, effect_text, condition=None):
        self.effect_text = effect_text
        self.condition = condition
        cleaned_text = re.sub(r'\([^()]*?\)', '', effect_text.lower())
        cleaned_text = re.sub(r'"[^"]*?"', '', cleaned_text)
        self.requires_target = "target" in cleaned_text

    def apply(self, game_state, source_id, controller, targets=None, context=None):
        """
        Apply the effect to the game state with improved targeting.

        Args:
            game_state: The game state instance
            source_id: ID of the source card/ability
            controller: Player who controls the effect
            targets: Dictionary of targets for the effect

        Returns:
            bool: Whether the effect was successfully applied
        """
        # Legacy effect loops that have not yet moved to the shared sequence
        # runner must not execute through an outstanding resolution choice.
        # Queue this effect behind that choice instead of overwriting it.
        active_choice = getattr(game_state, 'choice_context', None)
        async_types = getattr(game_state, '_ASYNC_EFFECT_CHOICE_TYPES', ())
        if active_choice and active_choice.get('type') in async_types:
            continuation = active_choice.setdefault('effect_continuation', {
                'effects': [], 'source_id': source_id,
                'controller_id': ('p1' if controller is game_state.p1 else 'p2'),
                'targets': copy.deepcopy(targets),
                'resolution_context': copy.deepcopy(context or {}),
                'finalizer': None, 'success': True,
            })
            continuation.setdefault('effects', []).append(self)
            return True

        if getattr(self, '_is_offspring_token_effect', False):
            # Assume `source_id` is the creature that entered the battlefield.
            source_creature = game_state._safe_get_card(source_id)
            if source_creature:
                 # The TriggeredAbility should have already checked the 'offspring_cost_paid' context.
                 # Here, we just perform the effect.
                 logging.debug(f"Applying Offspring effect: creating 1/1 copy of {source_creature.name}")
                 # Dynamically create the specialized copy effect. Pass the original creature card.
                 copy_effect = CreateTokenEffect(power=1, toughness=1, count=1,
                                                 is_copy=True, source_card_for_copy=source_creature,
                                                 controller_gets=True)
                 # Apply the copy effect.
                 return copy_effect._apply_effect(game_state, source_id, controller, targets)
            else:
                 logging.error(f"Offspring effect cannot find source creature {source_id}")
                 return False
             
        self.resolution_context = context or {}
        targets_were_supplied = targets is not None
        effective_targets = targets if targets_were_supplied else {} # Ensure targets is a dict

        if self.condition and not self._evaluate_condition(game_state, source_id, controller):
            logging.debug(f"Condition not met for effect: {self.effect_text}")
            return False

        # Expose missing targets as a real action instead of silently choosing
        # a strategic fallback. The chosen targets resume this same effect from
        # ActionHandler._finalize_targeting_choice.
        if self.requires_target and not targets_were_supplied:
            if not source_id or not getattr(game_state, 'targeting_system', None):
                logging.warning(f"Cannot expose target choice for effect: {self.effect_text}")
                return False
            if game_state.targeting_context:
                logging.warning("Cannot start a direct-effect target choice while another target choice is pending.")
                return False
            target_type = game_state._get_target_type_from_text(self.effect_text)
            min_targets, max_targets = game_state._target_bounds_from_text(self.effect_text)
            valid_map = game_state.targeting_system.get_valid_targets(
                source_id, controller, target_type, effect_text=self.effect_text)
            valid_ids = {target_id for ids in valid_map.values() for target_id in ids}
            if len(valid_ids) < min_targets:
                logging.warning(f"Targeting failed or yielded too few targets for: {self.effect_text}")
                return False
            if (game_state.previous_priority_phase is None
                    and game_state.phase not in [game_state.PHASE_TARGETING,
                                                 game_state.PHASE_SACRIFICE,
                                                 game_state.PHASE_CHOOSE]):
                game_state.previous_priority_phase = game_state.phase
            game_state.phase = game_state.PHASE_TARGETING
            game_state.targeting_context = {
                "source_id": source_id,
                "controller": controller,
                "effect_text": self.effect_text,
                "required_type": target_type,
                "required_count": max_targets,
                "min_targets": min_targets,
                "max_targets": max_targets,
                "selected_targets": [],
                "resume_effect": self,
            }
            game_state.priority_player = controller
            game_state.priority_pass_count = 0
            return True

        # An explicit empty target set is not interchangeable with a target
        # that became illegal during resolution.  Direct effect callers must
        # either provide a mandatory target or carry the validation snapshot
        # produced by GameStateStackMixin._validate_targets_on_resolution.
        # Optional ``up to`` instructions remain legal with zero selections.
        if (self.requires_target and targets_were_supplied
                and not self._contains_target_id(effective_targets)
                and not self._allows_empty_target_set(self.resolution_context)):
            source = game_state._safe_get_card(source_id)
            source_name = getattr(source, "name", source_id)
            logging.warning(
                "Mandatory targeted effect %s from %s (%s) received an empty "
                "target set without a validated post-commit invalidation "
                "context: %s",
                type(self).__name__, source_name, source_id,
                self.effect_text)
            return False

        # Call the implementation-specific effect application
        try:
            result = self._apply_effect(game_state, source_id, controller, effective_targets) # Pass resolved targets
            if result is None: # Handle NotImplementedError cases gracefully
                logging.warning(f"Effect application returned None for: {self.effect_text}. Might be unimplemented.")
                return False # Treat unimplemented as failure
            return result
        except NotImplementedError:
             logging.error(f"Effect application not implemented for: {self.effect_text}")
             self._report_support_issue(game_state, source_id,
                                        f"unimplemented effect: {self.effect_text[:80]}", "unparsed")
             return False
        except Exception as e:
             logging.error(f"Error applying effect '{self.effect_text}': {e}")
             import traceback
             logging.error(traceback.format_exc())
             # Card support manifest (July 2026): a crash during a card's
             # effect is the highest-severity support issue -- attribute it
             # so the deck builder can exclude the card.
             self._report_support_issue(game_state, source_id,
                                        f"crash in effect '{self.effect_text[:60]}': {type(e).__name__}: {e}",
                                        "crash")
             return False

    @staticmethod
    def _contains_target_id(targets):
        """Whether a target payload contains an actual object/player id."""
        if not isinstance(targets, dict):
            return bool(targets)
        for key, value in targets.items():
            if key == "X":
                continue
            if isinstance(value, (list, tuple, set)):
                if any(target_id is not None for target_id in value):
                    return True
            elif value is not None:
                return True
        return False

    def _allows_empty_target_set(self, context):
        """Recognize optional zero targets or a proven resolution fizzle."""
        context = context if isinstance(context, dict) else {}
        if bool(getattr(self, "optional", False)):
            return True
        min_targets = getattr(self, "min_targets", None)
        if min_targets is not None and int(min_targets) == 0:
            return True

        # Simple targeted stack objects retain their announced lower bound.
        if ("min_targets" in context
                and int(context.get("min_targets", 1) or 0) == 0):
            return True

        slots = (context.get("spree_target_slots")
                 if context.get("is_spree") else
                 context.get("instruction_target_slots")) or []
        slot_kind = ("mode_index" if hasattr(self, "_spree_mode_index")
                     else "instruction_index")
        effect_slot = (getattr(self, "_spree_mode_index", None)
                       if slot_kind == "mode_index" else
                       getattr(self, "_instruction_index", None))
        matching_slots = [
            slot for slot in slots
            if effect_slot is not None and slot.get(slot_kind) == effect_slot]
        if not matching_slots and effect_slot is None and len(slots) == 1:
            matching_slots = list(slots)
        if matching_slots and all(
                int(slot.get("min_targets", 0)) == 0
                for slot in matching_slots):
            return True

        lifecycle = context.get("_target_resolution_lifecycle", {})
        if not isinstance(lifecycle, dict) or not lifecycle.get("validated"):
            return False
        lifecycle_slots = lifecycle.get("slots", []) or []
        matching_lifecycle = [
            slot for slot in lifecycle_slots
            if effect_slot is not None and slot.get(slot_kind) == effect_slot]
        if (not matching_lifecycle and effect_slot is None
                and len(lifecycle_slots) == 1):
            matching_lifecycle = list(lifecycle_slots)
        if matching_lifecycle:
            return all(
                int(slot.get("original_target_count", 0)) > 0
                and int(slot.get("legal_target_count", 0)) == 0
                for slot in matching_lifecycle)
        return (
            int(lifecycle.get("original_target_count", 0)) > 0
            and int(lifecycle.get("legal_target_count", 0)) == 0)

    @staticmethod
    def _report_support_issue(game_state, source_id, reason, severity):
        """Attribute a support issue to the source card, if resolvable."""
        try:
            card = game_state._safe_get_card(source_id) if source_id is not None else None
            name = getattr(card, 'name', None)
            if name:
                from .card_support import report_unsupported
                report_unsupported(name, reason, severity=severity)
        except Exception:
            pass  # telemetry must never take the game down

    def _apply_effect(self, game_state, source_id, controller, targets):
        """
        Implementation-specific effect application.
        Should be overridden by subclasses.
        """
        # Default implementation logs a warning
        logging.warning(f"_apply_effect not implemented for effect type: {type(self).__name__} ('{self.effect_text}')")
        return False # Return False to indicate failure


    def _evaluate_condition(self, game_state, source_id, controller):
         if not self.condition: return True
         condition_text = str(self.condition).lower()
         if "if you control a creature" in condition_text:
             return any('creature' in getattr(game_state._safe_get_card(cid),'card_types',[]) for cid in controller["battlefield"])
         return True


class RollDieEffect(AbilityEffect):
    """Roll a numeric die and execute the matching oracle result row."""

    def __init__(self, sides, outcomes, pre_result_text=None, full_text=None):
        self.sides = max(1, int(sides))
        self.outcomes = list(outcomes)
        self.pre_result_text = (pre_result_text or "").strip(" .\n")
        effect_text = full_text or f"Roll a d{self.sides}"
        super().__init__(effect_text)

    def _apply_effect(self, game_state, source_id, controller, targets):
        result = random.randint(1, self.sides)
        roller = "p1" if controller is game_state.p1 else "p2"
        roll_record = {
            "source_id": source_id,
            "roller": roller,
            "sides": self.sides,
            "result": result,
            "turn": game_state.turn,
        }
        game_state.last_die_roll = dict(roll_record)
        game_state.die_roll_history.append(dict(roll_record))
        game_state.trigger_ability(source_id, "DIE_ROLLED", {
            "controller": controller,
            "roller": controller,
            "sides": self.sides,
            "result": result,
        })

        selected_text = None
        for minimum, maximum, outcome_text in self.outcomes:
            if minimum <= result <= maximum:
                selected_text = outcome_text
                break
        if selected_text is None:
            logging.warning(
                f"No d{self.sides} result row covers roll {result}: {self.effect_text}")
            return False

        result_targets = dict(targets or {})
        result_targets["X"] = result
        texts_to_apply = [text for text in (self.pre_result_text, selected_text) if text]
        applied_any = False
        all_succeeded = True
        for outcome_text in texts_to_apply:
            nested_effects = EffectFactory.create_effects(outcome_text, result_targets)
            if not nested_effects:
                logging.warning(f"Could not parse die-roll result text: {outcome_text}")
                all_succeeded = False
                continue
            for effect in nested_effects:
                applied = effect.apply(game_state, source_id, controller, result_targets)
                applied_any = bool(applied) or applied_any
                all_succeeded = bool(applied) and all_succeeded
        return applied_any and all_succeeded



class DrawCardEffect(AbilityEffect):
    """Effect that causes players to draw cards."""
    def __init__(self, count=1, target="controller", condition=None, count_expr=None):
        count_str = "X" if count == 'x' else str(count) if count != 1 else "a"
        card_str = "cards" if (isinstance(count, int) and count > 1) or count == 'x' else "card"
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        super().__init__(f"{target_desc} draw{'s' if target in ['controller','opponent','target_player'] else ''} {count_str} {card_str}", condition)
        self.base_count = count # Store original 'x' or number
        # "draw cards equal to the number of X" -> counted at resolution
        # against the controller (July 2026 parser expansion).
        self.count_expr = count_expr
        self.target = target
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        has_chosen_x = isinstance(targets, dict) and 'X' in targets
        x_value = targets.get('X', 0) if has_chosen_x else 0
        if self.count_expr:
            effective_count = game_state.count_dynamic_quantity(self.count_expr, controller)
            logging.debug(f"DrawCardEffect: dynamic count '{self.count_expr}' = {effective_count}.")
        elif self.base_count == 'x' and has_chosen_x:
            effective_count = x_value
            logging.debug(f"DrawCardEffect: Using X={x_value} for draw count.")
        else:
            effective_count = text_to_number(self.base_count)
        # --- End X Cost Handling ---

        if effective_count <= 0: return True # Draw 0 has no effect

        target_players = []
        # Target selection logic... (remains the same)
        if self.target == "controller": target_players.append(controller)
        elif self.target == "opponent": target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            if player_ids: target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else: logging.warning(f"DrawCardEffect target_player failed: No player ID in targets {targets}"); return False
        elif self.target == "each_player": target_players.extend([p for p in [game_state.p1, game_state.p2] if p])

        if not target_players: return False

        overall_success = True
        for p in target_players:
            num_drawn = 0
            success_player = True
            if hasattr(game_state, '_draw_cards'):
                num_drawn = len(game_state._draw_cards(p, effective_count))
                # Decking is a rules-defined result, not an engine failure.
                # A replacement such as Dredge may also produce zero actual
                # draws while legally consuming the instruction.
            elif hasattr(game_state, '_draw_card'):
                for _ in range(effective_count):
                    drawn_card_id = game_state._draw_card(p)
                    if drawn_card_id is not None:
                        # Numeric card ID 0 is a valid card, not a failed draw.
                        num_drawn += 1
                    elif p.get("attempted_draw_from_empty", False):
                        break
            else: # Fallback
                for _ in range(effective_count):
                    if p["library"]: p["hand"].append(p["library"].pop(0)); num_drawn += 1
                    else: p["attempted_draw_from_empty"] = True; success_player = False; break
            logging.debug(f"DrawCardEffect: Player {p['name']} drew {num_drawn} card(s).")
            overall_success &= success_player
        return overall_success


class GainLifeEffect(AbilityEffect):
    """Effect that causes players to gain life."""
    def __init__(self, amount, target="controller", condition=None, count_expr=None):
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        amount_str = "X" if amount == 'x' else str(amount) # Represent X in description
        super().__init__(f"{target_desc} gain {amount_str} life", condition)
        self.base_amount = amount # Store the original 'x' or number
        # "gain life equal to the number of X" -> dynamic (July 2026).
        self.count_expr = count_expr
        self.target = target
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        # Use X from context if available, otherwise use the base amount (converted)
        has_chosen_x = isinstance(targets, dict) and 'X' in targets
        x_value = targets.get('X', 0) if has_chosen_x else 0
        if getattr(self, 'count_expr', None):
            effective_amount = game_state.count_dynamic_quantity(self.count_expr, controller)
            logging.debug(f"GainLifeEffect: dynamic count '{self.count_expr}' = {effective_amount}.")
        elif self.base_amount == 'x' and has_chosen_x:
            effective_amount = x_value
            logging.debug(f"GainLifeEffect: Using X={x_value} for life gain amount.")
        else:
            # Convert base amount only if not using X
            effective_amount = text_to_number(self.base_amount)
        # --- End X Cost Handling ---

        if effective_amount <= 0: return True # Gain 0 or less has no effect

        target_players = []
        # --- Target selection logic (remains the same) ---
        if self.target == "controller":
            target_players.append(controller)
        elif self.target == "opponent":
            target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            if player_ids:
                target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else:
                 logging.warning(f"GainLifeEffect target_player failed: No player ID in targets {targets}")
                 return False
        elif self.target == "each_player":
             target_players.extend([p for p in [game_state.p1, game_state.p2] if p])

        if not target_players: return False

        overall_success = True
        for p in target_players:
             if hasattr(game_state, 'gain_life'):
                  # Pass effective_amount derived from X or base value
                  actual_gained = game_state.gain_life(p, effective_amount, source_id)
                  if actual_gained <= 0:
                      pass # Logging handled in gain_life
                  else: pass
             else: # Fallback
                  original_life = p.get('life', 0)
                  p['life'] += effective_amount
                  gained = p['life'] - original_life
                  if gained > 0: logging.debug(f"GainLifeEffect (Manual): Player {p['name']} gained {gained} life.")
                  else: overall_success = False # Less precise check without gain_life
        return overall_success



class SacrificeEffect(AbilityEffect):
    """A player sacrifices permanent(s) (CR 701.17). "Sacrifice a creature" =
    controller sacrifices; "target player sacrifices..." = that player does
    (edict). Previously hit the generic no-op fallback (July 2026 parser
    expansion). The affected player chooses each permanent through PHASE_CHOOSE.
    """
    def __init__(self, permanent_type="creature", who="controller", count=1,
                 condition=None, optional=False):
        self.permanent_type = permanent_type.lower()
        self.who = who  # 'controller' | 'target_player' | 'each_player' | 'each_opponent'
        self.count = count
        self.optional = bool(optional)
        super().__init__(f"{who} sacrifices {count} {self.permanent_type}", condition)
        self.requires_target = who == "target_player"

    def _type_matches(self, game_state, cid, source_id=None):
        return _permanent_matches_criteria(
            game_state, cid, self.permanent_type,
            controller=game_state.get_card_controller(cid),
            source_id=source_id)

    def _apply_effect(self, game_state, source_id, controller, targets):
        players = []
        if self.who == "controller":
            players = [controller]
        elif self.who == "each_player":
            players = [p for p in (game_state.p1, game_state.p2) if p]
        elif self.who == "each_opponent":
            players = [game_state.p2 if controller == game_state.p1 else game_state.p1]
        elif self.who == "target_player":
            pids = targets.get("players", []) if isinstance(targets, dict) else []
            if pids:
                players = [game_state.p1 if pids[0] == "p1" else game_state.p2]
            else:
                logging.warning(f"SacrificeEffect (edict): no target player in {targets}")
                return False
        if not players:
            return False
        pending = []
        for player in players:
            candidates = [cid for cid in player.get("battlefield", [])
                          if self._type_matches(game_state, cid, source_id)]
            extreme = re.search(
                r"\b(greatest|least)\s+(power|toughness|mana value)\b",
                self.permanent_type)
            if candidates and extreme:
                direction, field = extreme.groups()
                def characteristic(card_id):
                    card = game_state._safe_get_card(card_id)
                    return int((getattr(card, "cmc", 0)
                                if field == "mana value"
                                else getattr(card, field, 0)) or 0)
                boundary = (max if direction == "greatest" else min)(
                    characteristic(cid) for cid in candidates)
                candidates = [
                    cid for cid in candidates
                    if characteristic(cid) == boundary]
            if candidates:
                pending.append({
                    "player_id": "p1" if player is game_state.p1 else "p2",
                    "remaining": min(self.count, len(candidates)),
                    "options": candidates,
                    "optional": self.optional,
                })
        if not pending:
            return True
        current = pending.pop(0)
        current_player = game_state.p1 if current["player_id"] == "p1" else game_state.p2
        game_state.choice_context = {
            "type": "sacrifice_effect", "player": current_player,
            "pending_players": pending, "remaining": current["remaining"],
            "options": current["options"],
            "optional": self.optional, "sacrifice_performed": False,
            "permanent_type": self.permanent_type, "source_id": source_id,
            "resume_phase": game_state.phase,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = current_player
        return True


class SacrificeSourceEffect(AbilityEffect):
    """Sacrifice the source object named by ``this <permanent type>``.

    This wording appears on the effect side of abilities such as
    ``{2}{B}: Sacrifice this enchantment``.  It is not an activation cost and
    must never open a chooser that could sacrifice a different permanent.
    """
    def __init__(self, permanent_type="permanent", condition=None):
        self.permanent_type = str(permanent_type or "permanent").lower()
        super().__init__(f"Sacrifice this {self.permanent_type}", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        # An activated ability exists independently of its source.  If the
        # source left or changed controllers before resolution, its original
        # controller cannot substitute another permanent and the instruction
        # simply does nothing.
        if source_id not in controller.get("battlefield", []):
            return True
        owner = game_state._find_card_owner_fallback(source_id) or controller
        if not game_state.move_card(
                source_id, controller, "battlefield", owner, "graveyard",
                cause="sacrifice", context={"source_id": source_id}):
            return False
        game_state.trigger_ability(
            source_id, "SACRIFICED",
            {"controller": controller, "cause": "ability_effect"})
        return True


class DistributeCountersEffect(AbilityEffect):
    """Let the policy assign counters one at a time among committed targets."""
    def __init__(self, counter_type="+1/+1", count=1, condition=None,
                 targeting_text=None):
        self.counter_type = counter_type
        self.count = int(count)
        effect_text = targeting_text or (
            f"Distribute {count} {counter_type} counters among any number of target creatures")
        super().__init__(effect_text, condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        options = []
        for category in ("creatures", "permanents"):
            options.extend((targets or {}).get(category, []))
        options = list(dict.fromkeys(options))
        if not options:
            return True
        announced = getattr(self, "resolution_context", {}).get(
            "counter_allocations")
        if announced is not None:
            allocations = {
                target_id: int(announced.get(target_id, 0) or 0)
                for target_id in options
            }
            if (sum(int(value or 0) for value in announced.values()) != self.count
                    or any(count <= 0 for count in allocations.values())):
                logging.error(
                    "Invalid announced counter division for %s: %s",
                    source_id, announced)
                return False
            # Illegal targets keep their announced share; those counters are
            # not redistributed among the targets still legal at resolution.
            for target_id, count in allocations.items():
                owner, zone = game_state.find_card_location(target_id)
                if zone == "battlefield":
                    game_state.add_counter(
                        target_id, self.counter_type, count)
            return True
        if len(options) > self.count:
            logging.warning(
                f"Cannot distribute {self.count} counters among "
                f"{len(options)} targets while giving each target a counter.")
            return False
        game_state.choice_context = {
            "type": "distribute_counters", "player": controller,
            "options": options, "remaining": self.count,
            "allocations": {},
            "counter_type": self.counter_type, "source_id": source_id,
            "resume_phase": game_state.phase,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class ReanimateEffect(AbilityEffect):
    """Return a creature (or other permanent) card from a graveyard to the
    battlefield -- reanimation (CR 701.x). "Return ... to the battlefield" was
    previously routed to ReturnToHandEffect only for the "to hand" phrasing;
    the battlefield destination hit the generic no-op (July 2026 parser
    expansion). Distinct effect so the card lands in play, not hand.
    """
    def __init__(self, target_type="creature", from_zone="graveyard", enters_tapped=False, condition=None):
        self.target_type = target_type.lower()
        self.from_zone = from_zone.lower()
        self.enters_tapped = enters_tapped
        super().__init__(f"Return target {self.target_type} card from {self.from_zone} to the battlefield", condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        ids = []
        if isinstance(targets, dict):
            ids = list(targets.get("cards", []) or targets.get("creatures", []))
        if not ids:
            logging.warning(f"ReanimateEffect: no target card in {targets}")
            return False
        revived = 0
        for cid in ids:
            loc = game_state.find_card_location(cid)
            if not loc or loc[1] != self.from_zone:
                logging.debug(f"ReanimateEffect: {cid} not in {self.from_zone} (in {loc}).")
                continue
            owner = loc[0]
            # Reanimated cards enter under the CONTROLLER's control (owner keeps ownership).
            if game_state.move_card(cid, owner, self.from_zone, controller, "battlefield", cause="reanimate"):
                revived += 1
                if self.enters_tapped:
                    controller.setdefault("tapped_permanents", set()).add(cid)
        return revived > 0


class LoseLifeEffect(AbilityEffect):
    """A player loses life (CR 118.4). Distinct from damage: life loss is not
    dealt by a source, cannot be prevented, and does not trigger damage
    replacements. Common in drain/edict effects; previously hit the generic
    no-op fallback (July 2026 parser expansion).
    """
    def __init__(self, amount, target="target_player", condition=None):
        amount_str = "X" if amount == 'x' else str(amount)
        target_map = {"target_player": "Target player", "opponent": "Each opponent",
                      "controller": "You", "each_player": "Each player"}
        super().__init__(f"{target_map.get(target, 'Target player')} loses {amount_str} life", condition)
        self.base_amount = amount
        self.target = target
        self.requires_target = target == "target_player"

    def _apply_effect(self, game_state, source_id, controller, targets):
        has_chosen_x = isinstance(targets, dict) and 'X' in targets
        x_value = targets.get('X', 0) if has_chosen_x else 0
        amount = x_value if self.base_amount == 'x' and has_chosen_x else text_to_number(self.base_amount)
        if amount <= 0:
            return True
        target_players = []
        if self.target == "controller":
            target_players.append(controller)
        elif self.target == "opponent":
            target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "each_player":
            target_players.extend([p for p in (game_state.p1, game_state.p2) if p])
        elif self.target == "target_player":
            pids = targets.get("players", []) if isinstance(targets, dict) else []
            if pids:
                target_players.append(game_state.p1 if pids[0] == "p1" else game_state.p2)
            else:
                logging.warning(f"LoseLifeEffect: no target player in {targets}")
                return False
        if not target_players:
            return False
        for p in target_players:
            p["life"] = p.get("life", 0) - amount
            logging.debug(f"{p.get('name','player')} loses {amount} life (now {p['life']}).")
            # Feed life-loss triggers (e.g. 'whenever an opponent loses life').
            game_state.trigger_ability(source_id, "LIFE_LOSS",
                                       {"player": p, "amount": amount, "controller": controller})
        return True


class GainKeywordEffect(AbilityEffect):
    """Target creature(s) gain a keyword, usually until end of turn.

    Registers a layer-6 add_ability effect (mirrors BuffEffect's layer-7
    registration for P/T). The single most common combat-trick shape
    ("gains flying/trample/indestructible until end of turn") previously hit
    the generic no-op fallback (July 2026 parser expansion).
    """
    def __init__(self, keyword, target_type="creature", duration="end_of_turn", condition=None):
        self.keyword = keyword.lower().strip()
        self.target_type = target_type
        self.duration = duration
        super().__init__(f"{target_type} gains {self.keyword}", condition)
        self.requires_target = "target" in target_type or target_type == "creature"

    def apply(self, game_state, source_id, controller, targets=None, context=None):
        self.resolution_context = context or {}
        if not getattr(game_state, 'layer_system', None):
            logging.warning("GainKeywordEffect: LayerSystem unavailable.")
            return False
        affected = []
        if self.requires_target:
            if isinstance(targets, dict):
                affected = list(targets.get("creatures", []) or targets.get("permanents", []))
        elif self.target_type == "creatures you control":
            affected = [c for c in controller.get("battlefield", []) if game_state._is_creature(c)]
        elif self.target_type == "self":
            affected = [source_id]
        if not affected:
            logging.debug(f"GainKeywordEffect '{self.keyword}': no affected creatures.")
            return False
        effect_id = game_state.layer_system.register_effect({
            'source_id': source_id, 'layer': 6, 'affected_ids': affected,
            'effect_type': 'add_ability', 'effect_value': self.keyword,
            'duration': self.duration, 'description': f"grant {self.keyword} ({self.duration})",
        })
        if effect_id:
            game_state.layer_system.invalidate_cache()
            game_state.layer_system.apply_all_effects()
            logging.debug(f"Granted '{self.keyword}' to {affected} ({self.duration}).")
            return True
        return False

    def _apply_effect(self, game_state, source_id, controller, targets):
        return self.apply(game_state, source_id, controller, targets)


class DamageEffect(AbilityEffect):
    """Effect that deals damage to targets."""
    def __init__(self, amount, target_type="any target", condition=None):
        target_type_str = str(target_type).lower() if target_type is not None else "any target"
        if amount == "source_last_known_power":
            amount_str = "its last-known power"
        else:
            amount_str = "X" if amount == 'x' else str(amount) # Represent X in description
        super().__init__(f"Deal {amount_str} damage to {target_type_str}", condition)
        # Store original amount which might be 'x' or a number
        self.base_amount = amount
        self.target_type = target_type_str # e.g., "creature", "player", "any target", "each opponent"
        self.requires_target = "target" in self.target_type or "any" in self.target_type or "each" not in self.target_type

    @staticmethod
    def _target_identity_matches(game_state, target_id, relevant_categories):
        """Whether a committed target's actual identity fits the requirement,
        independent of the single category key it was filed under."""
        if target_id in ("p1", "p2"):
            return "players" in relevant_categories
        card = game_state._safe_get_card(target_id)
        if not card:
            return False
        types = {str(t).lower() for t in (getattr(card, 'card_types', None) or [])}
        if 'battle' in str(getattr(card, 'type_line', '') or '').lower():
            types.add('battle')
        category_types = {
            "creatures": "creature", "planeswalkers": "planeswalker",
            "battles": "battle", "artifacts": "artifact",
            "enchantments": "enchantment", "lands": "land",
        }
        return any(
            category_types[cat] in types
            for cat in relevant_categories if cat in category_types)

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        has_chosen_x = isinstance(targets, dict) and 'X' in targets
        x_value = targets.get('X', 0) if has_chosen_x else 0
        if self.base_amount == 'x' and has_chosen_x:
            effective_amount = x_value
            logging.debug(f"DamageEffect: Using X={x_value} for damage amount.")
        elif self.base_amount == "source_last_known_power":
            last_known = getattr(self, "resolution_context", {}).get("last_known", {})
            effective_amount = max(0, safe_int(last_known.get("power"), 0) or 0)
        else:
            effective_amount = text_to_number(self.base_amount)
        # --- End X Cost Handling ---

        if effective_amount <= 0: return True # No damage dealt

        targets_to_damage = [] # List of target_id
        processed_ids = set()

        # --- Target Collection Logic (remains the same) ---
        if self.requires_target:
            if not targets or not any(v for v in targets.values()):
                 # Cast/activation legality owns the mandatory-target check.
                 # At resolution an explicit empty set means every committed
                 # target became illegal (or an up-to-N instruction chose
                 # zero), both of which are successful rules no-ops.
                 return True
            relevant_categories = set()
            if self.target_type == "any target": relevant_categories = {"creatures", "players", "planeswalkers", "battles"}
            elif self.target_type == "creature": relevant_categories = {"creatures"}
            elif self.target_type == "player": relevant_categories = {"players"}
            elif self.target_type == "planeswalker": relevant_categories = {"planeswalkers"}
            elif self.target_type == "battle": relevant_categories = {"battles"}
            elif self.target_type == "permanent": relevant_categories = {"creatures", "planeswalkers", "battles", "artifacts", "enchantments", "lands"}
            else:
                 base_cat = self.target_type.replace('target ', '') # Basic removal
                 relevant_categories.add(base_cat + "s" if not base_cat.endswith('s') else base_cat)

            for cat, id_list in targets.items():
                # Non-category payload entries ('X' carries the chosen X
                # value as a bare int) are not target lists.
                if not isinstance(id_list, (list, tuple, set)):
                    continue
                for target_id in id_list:
                    if target_id in processed_ids:
                        continue
                    # Category keys file a multi-type permanent under exactly
                    # one type, so a legal target can arrive under a key this
                    # instruction does not read (July 14: a battlefield Summon
                    # saga arrived as 'enchantments' for any-target damage and
                    # falsely fizzled). The key is a routing hint, not the
                    # legality authority: accept any committed target whose
                    # actual identity satisfies the requirement.
                    if (cat in relevant_categories
                            or self._target_identity_matches(
                                game_state, target_id, relevant_categories)):
                        processed_ids.add(target_id)
                        targets_to_damage.append(target_id)
        elif "each opponent" in self.target_type:
             opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
             opp_id = "p2" if opponent == game_state.p2 else "p1"
             targets_to_damage.append(opp_id)
        elif "each creature your opponents control" in self.target_type:
             opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
             targets_to_damage.extend(
                 card_id for card_id in opponent.get("battlefield", [])
                 if game_state._is_creature(card_id))
        elif "each creature you control" in self.target_type:
             targets_to_damage.extend(
                 card_id for card_id in controller.get("battlefield", [])
                 if game_state._is_creature(card_id))
        elif "each creature" in self.target_type:
             targets_to_damage.extend(game_state.get_all_creatures()) # Assumes GS helper exists
        elif "each player" in self.target_type:
             targets_to_damage.extend(["p1", "p2"])

        if not targets_to_damage:
             # A nontargeted instruction such as "deal 3 damage to each
             # creature" legally does nothing when its affected set is empty.
             # It still resolved successfully and should not poison training
             # diagnostics as an engine failure.
             if not self.requires_target:
                 return True
             source = game_state._safe_get_card(source_id)
             source_name = getattr(source, 'name', source_id)
             logging.warning(
                 f"DamageEffect from {source_name}: No valid targets "
                 f"collected for '{self.effect_text}'. Provided: {targets}")
             return False

        # --- Damage Application Logic (uses effective_amount, otherwise remains the same) ---
        has_lifelink = game_state.check_keyword(source_id, "lifelink") if hasattr(game_state, 'check_keyword') else False
        has_deathtouch = game_state.check_keyword(source_id, "deathtouch") if hasattr(game_state, 'check_keyword') else False
        has_infect = game_state.check_keyword(source_id, "infect") if hasattr(game_state, 'check_keyword') else False
        total_actual_damage = 0
        success_overall = False

        for target_id in targets_to_damage:
             is_player_target = target_id in ["p1", "p2"]
             if is_player_target:
                 target_owner = game_state.p1 if target_id == "p1" else game_state.p2
                 target_zone = "player"
             else:
                 target_owner, target_zone = game_state.find_card_location(target_id)
             target_obj = target_owner if is_player_target else game_state._safe_get_card(target_id)

             if not target_obj or (not is_player_target and target_zone != "battlefield"):
                  logging.debug(f"Damage target {target_id} invalid or not on battlefield.")
                  continue

             damage_applied = 0
             try:
                 if is_player_target:
                     if has_infect:
                          target_owner.setdefault("poison_counters", 0)
                          target_owner["poison_counters"] += effective_amount # Use effective amount for counters
                          damage_applied = effective_amount # Track for lifelink based on intended damage
                          logging.debug(f"{target_owner['name']} got {effective_amount} poison counters from infect.")
                     elif hasattr(game_state, 'damage_player'):
                          # Pass effective_amount
                          damage_applied = game_state.damage_player(target_owner, effective_amount, source_id)
                     else: # Fallback
                          target_owner['life'] -= effective_amount; damage_applied = effective_amount
                 else: # Permanent target
                      if 'creature' in getattr(target_obj, 'card_types', []):
                           if has_infect: # Damage is -1/-1 counters
                                if hasattr(game_state,'add_counter'):
                                    game_state.add_counter(target_id, '-1/-1', effective_amount) # Use effective amount
                                    damage_applied = effective_amount
                           else:
                                damage_applied = game_state.apply_damage_to_permanent(target_id, effective_amount, source_id, False, has_deathtouch) # Pass effective amount
                      elif 'planeswalker' in getattr(target_obj, 'card_types', []):
                           damage_applied = game_state.damage_planeswalker(target_id, effective_amount, source_id) # Pass effective amount
                      elif 'battle' in getattr(target_obj, 'type_line', ''):
                           damage_applied = game_state.damage_battle(target_id, effective_amount, source_id) # Pass effective amount

                 if damage_applied > 0:
                      total_actual_damage += damage_applied
                      success_overall = True
             except Exception as dmg_e:
                  logging.error(f"Error applying damage to {target_id}: {dmg_e}", exc_info=True)

        # --- Lifelink logic (remains the same) ---
        if has_lifelink and total_actual_damage > 0:
            if hasattr(game_state, 'gain_life'): game_state.gain_life(controller, total_actual_damage, source_id)
            else: controller['life'] += total_actual_damage

        return success_overall


class KeywordChoiceGrantEffect(AbilityEffect):
    """Grant one policy-selected keyword from an arbitrary printed list.

    Resolution pauses in PHASE_CHOOSE so the controller picks the keyword;
    the chosen grant is then an ordinary layer-6 GainKeywordEffect.
    """

    def __init__(self, first_keyword, second_keyword=None,
                 duration="end_of_turn", condition=None,
                 targeting_text="target creature"):
        raw_options = (list(first_keyword)
                       if isinstance(first_keyword, (list, tuple, set))
                       else [first_keyword, second_keyword])
        self.options = list(dict.fromkeys(
            str(keyword).lower().strip(" .,;")
            for keyword in raw_options if keyword))
        self.targeting_text = str(targeting_text or "target creature").strip()
        self.duration = duration
        super().__init__(
            f"{self.targeting_text} gains your choice of "
            f"{', '.join(self.options)}", condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            for ids in targets.values():
                if isinstance(ids, (list, tuple, set)):
                    target_ids.extend(ids)
        if not target_ids:
            return False
        if game_state.choice_context:
            logging.warning(
                "KeywordChoiceGrantEffect: another choice is already pending.")
            return False
        previous_priority_phase = game_state.previous_priority_phase
        resume_phase = game_state.phase
        if game_state.phase != game_state.PHASE_CHOOSE:
            game_state.previous_priority_phase = game_state.phase
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.choice_context = {
            "type": "keyword_grant",
            "player": controller,
            "options": list(self.options),
            "target_id": target_ids[0],
            "source_id": source_id,
            "duration": self.duration,
            "targeting_text": self.targeting_text,
            "resume_phase": resume_phase,
            "previous_priority_phase_before_choice": previous_priority_phase,
        }
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        return True


class TorchTheTowerEffect(AbilityEffect):
    """Resolve Torch's Bargain branch and damage-linked exile replacement."""

    def __init__(self, condition=None):
        super().__init__(
            "Torch the Tower deals damage to target creature or planeswalker",
            condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            for ids in targets.values():
                if isinstance(ids, (list, tuple, set)):
                    target_ids.extend(ids)
        if not target_ids:
            return False
        target_id = target_ids[0]
        target = game_state._safe_get_card(target_id)
        target_controller, target_zone = game_state.find_card_location(target_id)
        if (not target or target_zone != "battlefield"
                or not set(getattr(target, "card_types", [])).intersection(
                    {"creature", "planeswalker"})):
            return False

        bargained = bool(getattr(self, "resolution_context", {}).get("bargained"))
        amount = 3 if bargained else 2
        if "creature" in getattr(target, "card_types", []):
            damage_dealt = game_state.apply_damage_to_permanent(
                target_id, amount, source_id)
        else:
            damage_dealt = game_state.damage_planeswalker(
                target_id, amount, source_id, defer_sba=True)

        if damage_dealt > 0 and game_state.replacement_effects:
            def _is_torch_damaged_creature_dying(event_context):
                if event_context.get("card_id") != target_id:
                    return False
                dying = game_state._safe_get_card(target_id)
                return bool(
                    dying
                    and "creature" in getattr(dying, "card_types", []))

            def _exile_instead(event_context):
                event_context["to_player"] = (
                    event_context.get("to_player") or target_controller)
                event_context["to_zone"] = "exile"
                event_context["torch_exile_replacement"] = True
                return event_context

            game_state.replacement_effects.register_effect({
                "event_type": "DIES",
                "condition": _is_torch_damaged_creature_dying,
                "replacement": _exile_instead,
                "source_id": source_id,
                "controller_id": controller,
                "duration": "end_of_turn",
                "description": "Torch the Tower exiles its damaged permanent instead",
            })

        # No player receives priority between Torch's instructions. Run SBAs
        # only after its damage-linked replacement exists.
        game_state.check_state_based_actions()
        if bargained:
            ScryEffect(1)._apply_effect(
                game_state, source_id, controller, targets or {})
        return damage_dealt > 0

class DamageWithExileReplacementEffect(AbilityEffect):
    """Deal damage to one target; if it would die this turn, exile it instead.

    Covers riders like Obliterating Bolt's "If that creature or planeswalker
    would die this turn, exile it instead." (Torch the Tower keeps its own
    effect because Bargain changes its damage and adds a scry.)
    """

    def __init__(self, amount, includes_planeswalkers=False, condition=None):
        super().__init__(
            f"deals {amount} damage to target; exiled instead if it would "
            "die this turn", condition)
        self.amount = amount
        self.includes_planeswalkers = includes_planeswalkers
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            for ids in targets.values():
                if isinstance(ids, (list, tuple, set)):
                    target_ids.extend(ids)
        if not target_ids:
            return False
        target_id = target_ids[0]
        target = game_state._safe_get_card(target_id)
        target_controller, target_zone = game_state.find_card_location(target_id)
        if (not target or target_zone != "battlefield"
                or not set(getattr(target, "card_types", [])).intersection(
                    {"creature", "planeswalker"})):
            return False

        if "creature" in getattr(target, "card_types", []):
            damage_dealt = game_state.apply_damage_to_permanent(
                target_id, self.amount, source_id)
        else:
            damage_dealt = game_state.damage_planeswalker(
                target_id, self.amount, source_id, defer_sba=True)

        if damage_dealt > 0 and game_state.replacement_effects:
            rider_types = {"creature"}
            if self.includes_planeswalkers:
                rider_types.add("planeswalker")

            def _is_damaged_target_dying(event_context):
                if event_context.get("card_id") != target_id:
                    return False
                dying = game_state._safe_get_card(target_id)
                return bool(
                    dying
                    and rider_types.intersection(
                        getattr(dying, "card_types", [])))

            def _exile_instead(event_context):
                event_context["to_player"] = (
                    event_context.get("to_player") or target_controller)
                event_context["to_zone"] = "exile"
                return event_context

            game_state.replacement_effects.register_effect({
                "event_type": "DIES",
                "condition": _is_damaged_target_dying,
                "replacement": _exile_instead,
                "source_id": source_id,
                "controller_id": controller,
                "duration": "end_of_turn",
                "description": (
                    "Damaged permanent is exiled instead of dying this turn"),
            })

        # No player receives priority between the spell's instructions. Run
        # SBAs only after the damage-linked replacement exists.
        game_state.check_state_based_actions()
        return damage_dealt > 0

class AddCountersEffect(AbilityEffect):
    """Effect that adds counters to permanents or players."""
    def __init__(self, counter_type, count=1, target_type="creature", condition=None):
        count_str = ("X" if count == 'x' else "its power"
                     if count == "source_power" else str(count))
        target_prefix = "target " if "target" in str(target_type).lower() else ""
        super().__init__(
            f"Put {count_str} {counter_type} counter(s) on "
            f"{target_prefix}{target_type}", condition)
        self.counter_type = counter_type.replace('_','/') # Allow P/T format storage
        # Store original count which might be 'x' or a number
        self.base_count = count
        self.target_type = target_type.lower() # Normalize
        self.requires_target = "target" in self.target_type

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        has_chosen_x = isinstance(targets, dict) and 'X' in targets
        x_value = targets.get('X', 0) if has_chosen_x else 0
        if self.base_count == 'x' and has_chosen_x:
            effective_count = x_value
            logging.debug(f"AddCountersEffect: Using X={x_value} for counter count.")
        elif self.base_count == "source_power":
            source = game_state._safe_get_card(source_id)
            effective_count = max(
                0, safe_int(getattr(source, "power", 0), 0) or 0)
        else:
            effective_count = text_to_number(self.base_count) # Use original base count
        # --- End X Cost Handling ---

        if effective_count <= 0: return True # Adding 0 or less has no effect

        targets_to_affect = []
        processed_ids = set()
        # --- Target Collection Logic (remains the same) ---
        if self.requires_target:
            if not targets or not any(v for v in targets.values()):
                 logging.warning(f"AddCountersEffect requires targets, none provided/resolved: {targets}")
                 return False
            relevant_categories = set()
            if "creature" in self.target_type: relevant_categories.add("creatures")
            if "artifact" in self.target_type: relevant_categories.add("artifacts")
            if "planeswalker" in self.target_type: relevant_categories.add("planeswalkers")
            if "enchantment" in self.target_type: relevant_categories.add("enchantments")
            if "land" in self.target_type: relevant_categories.add("lands")
            if "permanent" in self.target_type: relevant_categories.update(["creatures", "artifacts", "enchantments", "planeswalkers", "lands", "battles"])
            if "player" in self.target_type: relevant_categories.add("players")
            if not relevant_categories: relevant_categories.add(self.target_type+"s")

            for cat, id_list in targets.items():
                 if cat in relevant_categories:
                     targets_to_affect.extend(id_list)
        elif "self" == self.target_type: targets_to_affect.append(source_id)
        elif self.target_type in {
                "each creature you control",
                "each tapped creature you control"}:
            tapped_only = "tapped" in self.target_type
            targets_to_affect.extend(
                card_id for card_id in controller.get("battlefield", [])
                if game_state._is_creature(card_id)
                and (not tapped_only
                     or card_id in controller.get("tapped_permanents", set())))
        elif "each creature your opponents control" == self.target_type:
            opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
            targets_to_affect.extend(
                card_id for card_id in opponent.get("battlefield", [])
                if game_state._is_creature(card_id))
        elif "each creature" == self.target_type: targets_to_affect.extend(game_state.get_all_creatures())
        elif "each opponent" == self.target_type:
            opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
            opp_id = "p2" if opponent == game_state.p2 else "p1"
            targets_to_affect.append(opp_id)

        if not targets_to_affect:
            # Nontargeted aggregate instructions legally resolve when their
            # affected set is empty. Mandatory targeted effects still fail.
            if not self.requires_target:
                return True
            logging.warning(f"AddCountersEffect: No valid targets collected for '{self.effect_text}'. Targets: {targets}")
            return False

        unique_targets = set(targets_to_affect)
        success_count = 0
        # --- Counter Application (uses effective_count, otherwise remains the same) ---
        for target_id in unique_targets:
             target_owner, target_zone = game_state.find_card_location(target_id)
             is_player_target = target_id in ["p1", "p2"]
             target_obj = target_owner if is_player_target else game_state._safe_get_card(target_id)

             if not target_obj or (not is_player_target and target_zone != "battlefield"):
                 logging.debug(f"AddCountersEffect: Target {target_id} invalid or not on battlefield.")
                 continue

             if is_player_target: # Add counters to player
                 if self.counter_type == 'poison':
                     target_owner.setdefault("poison_counters", 0); target_owner["poison_counters"] += effective_count # Use effective count
                     success_count += 1; logging.debug(f"Added {effective_count} poison counter(s) to player {target_owner['name']}.")
                 elif self.counter_type == 'energy':
                     target_owner.setdefault("energy_counters", 0); target_owner["energy_counters"] += effective_count # Use effective count
                     success_count += 1; logging.debug(f"Added {effective_count} energy counter(s) to player {target_owner['name']}.")
                 else: logging.warning(f"Cannot add counter type '{self.counter_type}' to player.")
             else: # Add counters to permanent
                  if hasattr(game_state, 'add_counter') and callable(game_state.add_counter):
                      # Pass effective_count
                      if game_state.add_counter(target_id, self.counter_type, effective_count): success_count += 1
                  else: # Fallback
                      target_card = target_obj
                      if not hasattr(target_card, 'counters'): target_card.counters = {}
                      target_card.counters[self.counter_type] = target_card.counters.get(self.counter_type, 0) + effective_count # Use effective count
                      logging.debug(f"Fallback AddCounters: Added {effective_count} {self.counter_type} to {target_card.name}")
                      success_count += 1

        return success_count > 0


class RemoveCounterEffect(AbilityEffect):
    """Remove a counter from the source, exposing type and decline choices."""

    def __init__(self, counter_type=None, count=1, optional=False,
                 condition=None):
        self.counter_type = (
            str(counter_type).lower() if counter_type else None)
        self.count = max(1, int(count))
        self.optional = bool(optional)
        counter_text = self.counter_type or "a counter"
        super().__init__(
            f"{'You may remove' if optional else 'Remove'} "
            f"{counter_text} from this permanent", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        source = game_state._safe_get_card(source_id)
        counters = getattr(source, "counters", {}) if source else {}
        options = [
            counter_type for counter_type, amount in sorted(counters.items())
            if int(amount or 0) >= self.count]
        if self.counter_type:
            options = [
                counter_type for counter_type in options
                if str(counter_type).lower() == self.counter_type]
        if not options:
            return True if self.optional else False
        game_state.choice_context = {
            "type": "resolution_choice",
            "choice_kind": "remove_counter",
            "player": controller,
            "controller": controller,
            "source_id": source_id,
            "options": options,
            "counter_count": self.count,
            "optional": self.optional,
            "resume_phase": game_state.phase,
            "choice_page": 0,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        return True


class CreateTokenEffect(AbilityEffect):
    """Effect that creates token creatures. Can handle copies with modified P/T."""
    # ADDED: is_copy flag and source_card_for_copy attribute
    def __init__(self, power, toughness, creature_type="Creature", count=1, keywords=None, colors=None, is_legendary=False, controller_gets=True, condition=None, is_copy=False, source_card_for_copy=None, count_expr=None):
        self.is_copy = is_copy
        self.source_card_for_copy = source_card_for_copy # Should be the Card object
        # "for each X" token counts are resolved against the controller at
        # resolution time via GameState.count_dynamic_quantity (Domain etc.).
        self.count_expr = count_expr

        if is_copy and source_card_for_copy and isinstance(source_card_for_copy, Card):
             # Use getattr for safer access to name
             token_desc = f"{count} 1/1 token copy of {getattr(source_card_for_copy, 'name', 'original creature')}"
        elif is_copy: # source_card_for_copy is missing or invalid
            token_desc = f"{count} 1/1 token copy of original" # Fallback description
            logging.warning("CreateTokenEffect init: is_copy=True but source_card_for_copy is missing or invalid.")
        else:
            # Ensure colors and keywords are lists for join
            colors_str = ','.join(colors) if colors else ''
            keywords_str = ' with ' + ', '.join(keywords) if keywords else ''
            token_desc = f"{count} {power}/{toughness} {colors_str} {creature_type} token{keywords_str}"

        super().__init__(f"Create {token_desc}", condition)
        # Store power/toughness relevant for *this* effect (e.g., 1/1 for Offspring)
        self.power = 1 if self.is_copy else power
        self.toughness = 1 if self.is_copy else toughness
        self.creature_type = creature_type # Base type if not copy
        self.count = count
        self.keywords = keywords or [] # Base keywords if not copy
        self.colors = colors # Base colors if not copy
        self.is_legendary = is_legendary # Base legendary status if not copy
        self.controller_gets = controller_gets

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_player = controller
        if not self.controller_gets:
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1

        effective_count = self.count
        if self.count_expr:
            effective_count = game_state.count_dynamic_quantity(self.count_expr, controller)
            logging.debug(f"CreateTokenEffect: dynamic count '{self.count_expr}' = {effective_count}.")
            if effective_count <= 0:
                return True  # Zero tokens is a successful resolution.

        created_token_ids = []

        # --- Handle Copy Case (Offspring) ---
        if self.is_copy and self.source_card_for_copy and isinstance(self.source_card_for_copy, Card):
            original_card = self.source_card_for_copy
            logging.debug(f"Applying CreateTokenCopyEffect for {original_card.name}")
            token_data = None # Initialize token_data
            try:
                import copy # Import copy locally if not already imported
                # Get copyable characteristics based on Rule 707.2
                # Make sure to use getattr for safety and handle potential None values
                token_data = {
                    "name": getattr(original_card, 'name', 'Unknown'),
                    "mana_cost": getattr(original_card, 'mana_cost', ""),
                    "color_identity": copy.deepcopy(getattr(original_card, 'colors', [0]*5)), # Use colors for copy identity
                    "card_types": copy.deepcopy(getattr(original_card, 'card_types', [])),
                    "subtypes": copy.deepcopy(getattr(original_card, 'subtypes', [])),
                    "supertypes": copy.deepcopy(getattr(original_card, 'supertypes', [])),
                    "oracle_text": getattr(original_card, 'oracle_text', ''),
                    # *** OFFSPRING Specific: Set P/T to 1/1 ***
                    "power": 1,
                    "toughness": 1,
                    # Keywords: Copy the *final* keyword state from the original (post-layers?)
                    # Simpler: copy the base keyword array/list from original Card object
                    "keywords": copy.deepcopy(getattr(original_card, 'keywords', [])),
                    "is_token": True,
                }
                # Rebuild typeline (assuming helper exists and handles supertypes/types/subtypes)
                token_data["type_line"] = game_state._build_type_line(token_data) if hasattr(game_state, '_build_type_line') else "Token Creature" # Fallback typeline
                # Ensure copied 'keywords' has the correct dimension
                if len(token_data['keywords']) != len(Card.ALL_KEYWORDS):
                     token_data['keywords'] = [0] * len(Card.ALL_KEYWORDS) # Reset if wrong size


            except Exception as e:
                logging.error(f"Error preparing token copy data for {original_card.name}: {e}", exc_info=True)
                return False

            # Create the specified number of token copies
            for _ in range(effective_count):
                 if token_data: # Ensure data was prepared
                     # Use GameState.create_token method
                     token_id = game_state.create_token(target_player, token_data.copy()) # Pass copy
                     if token_id: created_token_ids.append(token_id)

        # --- Handle Normal Token Creation (Existing Logic) ---
        else:
            # Convert color names to the 5-dim list format
            color_list = [0] * 5
            if self.colors:
                color_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
                for color_name in self.colors:
                    if color_name.lower() in color_map:
                         color_list[color_map[color_name.lower()]] = 1

            # Handle "artifact creature" type line properly
            card_types_list = ["token"] # Always a token
            subtypes_list = []
            base_type = "Creature" # Default unless specified
            # Known noncreature artifact token types.
            ARTIFACT_TOKEN_TYPES = {"treasure", "food", "clue", "map", "blood",
                                    "gold", "powerstone", "junk", "incubator"}
            if isinstance(self.creature_type, str):
                ct_lower = self.creature_type.lower()
                if "artifact" in ct_lower: card_types_list.append("artifact")
                if "creature" in ct_lower: card_types_list.append("creature")
                if "enchantment" in ct_lower: card_types_list.append("enchantment")
                # "Beast", "Fish", "Soldier"... name a creature subtype, not a
                # card type. Without this the token had NO card type at all --
                # it could never attack, block, or be targeted as a creature.
                if len(card_types_list) == 1:
                    if ct_lower.strip() in ARTIFACT_TOKEN_TYPES:
                        card_types_list.append("artifact")
                    else:
                        card_types_list.append("creature")

                parts = self.creature_type.split()
                # Improved heuristic for base type and subtypes
                # Check Card static lists for accuracy
                non_type_parts = [p.capitalize() for p in parts if p.lower() not in Card.ALL_CARD_TYPES]
                # SUBTYPE_VOCAB is model-feature metadata built from the loaded
                # pool; a printed token subtype (e.g. Beast) is real rules data
                # even when no loaded card shares it, so fall back to the
                # parsed words instead of silently dropping the subtype.
                valid_subtypes = [s for s in non_type_parts if s in Card.SUBTYPE_VOCAB] or non_type_parts
                subtypes_list.extend(valid_subtypes)
                if valid_subtypes: base_type = valid_subtypes[-1] # Guess base type from last subtype
                elif parts: base_type = parts[-1].capitalize() # Fallback

            # Build type line
            type_line = "Token "
            if self.is_legendary: type_line += "Legendary "
            # Order types conventionally
            type_order = {"artifact": 1, "enchantment": 2, "creature": 3}
            sorted_types = sorted([ct for ct in card_types_list if ct != "token"], key=lambda t: type_order.get(t, 99))
            type_line += " ".join(t.capitalize() for t in sorted_types) + " "
            if subtypes_list:
                 type_line += f"— {' '.join(sorted(list(set(subtypes_list))))}"

            token_data = {
                "name": f"{base_type} Token",
                "type_line": type_line.strip(),
                "card_types": list(set(card_types_list)),
                "subtypes": sorted(list(set(subtypes_list))),
                "supertypes": ["legendary", "token"] if self.is_legendary else ["token"],
                "power": self.power,
                "toughness": self.toughness,
                "oracle_text": " ".join(self.keywords) if self.keywords else "",
                "keywords": [0] * len(Card.ALL_KEYWORDS), # Initialize keyword array
                # Card.__init__ derives its colors from "color_identity" as
                # WUBRG letters; a bare "colors" vector was silently ignored,
                # so every colored token came out colorless.
                "colors": color_list,
                "color_identity": [letter for letter, present
                                   in zip("WUBRG", color_list) if present],
                "is_token": True,
            }
            # Ensure correct keyword array size before mapping
            if len(token_data['keywords']) != len(Card.ALL_KEYWORDS):
                token_data['keywords'] = [0] * len(Card.ALL_KEYWORDS)
            # Map keywords
            kw_indices = {kw.lower(): i for i, kw in enumerate(Card.ALL_KEYWORDS)}
            for kw in self.keywords:
                kw_lower = kw.lower()
                if kw_lower in kw_indices:
                    token_data["keywords"][kw_indices[kw_lower]] = 1

            for _ in range(effective_count):
                # Use GameState.create_token
                token_id = game_state.create_token(target_player, token_data.copy()) # Pass copy
                if token_id: created_token_ids.append(token_id)

        created_context = getattr(self, "resolution_context", None)
        if isinstance(created_context, dict) and created_token_ids:
            created_context.setdefault("_created_object_ids", []).extend(
                created_token_ids)
        return len(created_token_ids) > 0


class ManifestDreadEffect(AbilityEffect):
    """Look at the top two cards, manifest one, and graveyard the other."""

    def __init__(self, condition=None):
        super().__init__("Manifest dread", condition)
        self.requires_target = False

    @staticmethod
    def _emit(game_state, source_id, controller, manifested_id=None,
              graveyard_id=None):
        game_state.trigger_ability(source_id, "MANIFEST_DREAD", {
            "controller": controller,
            "manifested_card_id": manifested_id,
            "graveyard_card_id": graveyard_id,
        })

    def _apply_effect(self, game_state, source_id, controller, targets):
        library = controller.get("library", [])
        if not library:
            self._emit(game_state, source_id, controller)
            return True
        if len(library) == 1:
            card_id = library[0]
            success = game_state.manifest_selected_card(
                controller, card_id, "library")
            if success:
                self._emit(game_state, source_id, controller,
                           manifested_id=card_id)
            return success

        looked_at = list(library[:2])
        del library[:2]
        if game_state.phase not in [game_state.PHASE_CHOOSE,
                                    game_state.PHASE_TARGETING,
                                    game_state.PHASE_SACRIFICE]:
            game_state.previous_priority_phase = game_state.phase
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.choice_context = {
            "type": "manifest_dread",
            "player": controller,
            "controller": controller,
            "source_id": source_id,
            "options": looked_at,
            "resolved": False,
        }
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        return True


class CreateFoodEffect(AbilityEffect):
    """Create the predefined colorless Food artifact token."""

    FOOD_ORACLE_TEXT = "{2}, {T}, Sacrifice this token: You gain 3 life."

    def __init__(self, count=1, condition=None):
        self.count = max(1, int(count))
        super().__init__(f"Create {self.count} Food token(s)", condition)
        self.requires_target = False

    @classmethod
    def create_for(cls, game_state, player, count):
        created = []
        token_data = {
            "name": "Food",
            "type_line": "Token Artifact - Food",
            "card_types": ["artifact"],
            "subtypes": ["food"],
            "supertypes": [],
            "oracle_text": cls.FOOD_ORACLE_TEXT,
            "power": 0,
            "toughness": 0,
            "keywords": [0] * len(Card.ALL_KEYWORDS),
            "colors": [0, 0, 0, 0, 0],
            "is_token": True,
        }
        for _ in range(max(0, int(count))):
            token_id = game_state.create_token(player, token_data.copy())
            if token_id:
                created.append(token_id)
        return created

    def _apply_effect(self, game_state, source_id, controller, targets):
        return bool(self.create_for(game_state, controller, self.count))


class CreateMapEffect(AbilityEffect):
    """Create the predefined colorless Map artifact token."""

    MAP_ORACLE_TEXT = (
        "{1}, {T}, Sacrifice this artifact: Target creature you control explores. "
        "Activate only as a sorcery."
    )

    def __init__(self, count=1, condition=None):
        self.count = max(1, int(count))
        super().__init__(f"Create {self.count} Map token(s)", condition)
        self.requires_target = False

    @classmethod
    def create_for(cls, game_state, player, count):
        created = []
        token_data = {
            "name": "Map",
            "type_line": "Token Artifact - Map",
            "card_types": ["artifact"],
            "subtypes": ["map"],
            "supertypes": [],
            "oracle_text": cls.MAP_ORACLE_TEXT,
            "power": 0,
            "toughness": 0,
            "keywords": [0] * len(Card.ALL_KEYWORDS),
            "colors": [0, 0, 0, 0, 0],
            "is_token": True,
        }
        for _ in range(max(0, int(count))):
            token_id = game_state.create_token(player, token_data.copy())
            if token_id:
                created.append(token_id)
        return created

    def _apply_effect(self, game_state, source_id, controller, targets):
        return bool(self.create_for(game_state, controller, self.count))


class DestroyAndCreateMapsEffect(AbilityEffect):
    """Get Lost's linked instructions with the pre-destruction controller."""

    def __init__(self, count=2, condition=None):
        self.count = max(1, int(count))
        super().__init__(
            "Destroy target creature, enchantment, or planeswalker. "
            f"Its controller creates {self.count} Map tokens.",
            condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            for category_ids in targets.values():
                if isinstance(category_ids, (list, tuple, set)):
                    target_ids.extend(category_ids)
        if not target_ids:
            return False
        target_id = target_ids[0]
        target_controller, target_zone = game_state.find_card_location(target_id)
        if not target_controller or target_zone != "battlefield":
            return False

        # The second instruction happens even if indestructible or another
        # replacement prevents the destruction.
        destroy = DestroyEffect(target_type="permanent")
        destroy._apply_effect(game_state, source_id, controller, targets)
        return bool(CreateMapEffect.create_for(
            game_state, target_controller, self.count))


class AttachEquipmentEffect(AbilityEffect):
    """Attach the source Equipment to its committed controlled creature."""

    def __init__(self, condition=None):
        super().__init__("Attach this Equipment to target creature you control",
                         condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        candidates = []
        if isinstance(targets, dict):
            for category in ('creatures', 'permanents', 'chosen'):
                candidates.extend(targets.get(category, []))
        return bool(candidates and game_state.equip_permanent(
            controller, source_id, candidates[0]))


class ConniveEffect(AbilityEffect):
    """Draw one, discard one by policy, then counter a nonland discard."""

    def __init__(self, targeted=False, optional=False, once_each_turn=False,
                 condition=None):
        text = ("Target creature you control connives" if targeted
                else "This creature connives")
        super().__init__(text, condition)
        self.requires_target = bool(targeted)
        self.optional = bool(optional)
        self.once_each_turn = bool(once_each_turn)

    @staticmethod
    def _already_used(controller, source_id, turn):
        return controller.setdefault('connive_once_each_turn', {}).get(
            source_id) == turn

    @staticmethod
    def _mark_used(controller, source_id, turn):
        controller.setdefault('connive_once_each_turn', {})[source_id] = turn

    def _apply_effect(self, game_state, source_id, controller, targets):
        candidates = []
        if isinstance(targets, dict):
            candidates.extend(targets.get('creatures', []))
            candidates.extend(targets.get('permanents', []))
        creature_id = candidates[0] if candidates else source_id
        creature_controller, zone = game_state.find_card_location(creature_id)
        card = game_state._safe_get_card(creature_id)
        if (creature_controller is not controller or zone != 'battlefield'
                or not card or 'creature' not in getattr(card, 'card_types', [])):
            return False
        if (self.once_each_turn
                and self._already_used(controller, source_id,
                                       game_state.turn)):
            return True
        if self.optional:
            game_state.choice_context = {
                'type': 'resolution_choice', 'choice_kind': 'connive_begin',
                'player': controller, 'options': [creature_id],
                'optional': True, 'source_id': source_id,
                'connive_creature_id': creature_id,
                'connive_once_each_turn': self.once_each_turn,
                'resume_phase': game_state.PHASE_PRIORITY,
            }
            game_state.phase = game_state.PHASE_CHOOSE
            game_state.priority_player = controller
            return True
        if self.once_each_turn:
            self._mark_used(controller, source_id, game_state.turn)
        return self.start_connive(
            game_state, source_id, controller, creature_id)

    @staticmethod
    def start_connive(game_state, source_id, controller, creature_id):
        game_state._draw_card(controller)
        if not controller.get('hand', []):
            return True
        game_state.choice_context = {
            'type': 'connive_discard', 'player': controller,
            'source_id': source_id, 'creature_id': creature_id,
            'choice_page': 0, 'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        return True


class DiscoverEffect(AbilityEffect):
    """Discover a fixed or resolution-derived value."""

    def __init__(self, value, condition=None):
        self.value = (int(value) if isinstance(value, int)
                      or str(value).isdigit() else str(value).lower())
        super().__init__(f"Discover {self.value}", condition)
        self.requires_target = False

    def _spell_mana_value(self, game_state, card_id):
        card = game_state._safe_get_card(card_id)
        if not card:
            return 0
        value = int(float(getattr(card, 'cmc', 0) or 0))
        for item in game_state.stack:
            if (isinstance(item, tuple) and len(item) >= 4
                    and item[0] == 'SPELL' and item[1] == card_id):
                context = item[3] or {}
                x_symbols = len(re.findall(
                    r'\{X\}', str(getattr(card, 'mana_cost', '') or ''),
                    re.IGNORECASE))
                value += max(0, int(context.get('X', 0) or 0)) * x_symbols
                break
        return value

    def _resolve_value(self, game_state, targets):
        if isinstance(self.value, int):
            return self.value
        context = getattr(self, 'resolution_context', {}) or {}
        if self.value == 'same':
            return max(0, int(context.get('discover_value', 0) or 0))
        if self.value == 'spell_mana_value':
            target_ids = []
            if isinstance(targets, dict):
                for key in ('spells', 'chosen', 'targets'):
                    target_ids.extend(targets.get(key, []))
            card_id = next(iter(target_ids), None)
            if card_id is None:
                card_id = context.get('cast_card_id', context.get('event_card_id'))
            return self._spell_mana_value(game_state, card_id)
        return max(0, int((targets or {}).get(
            'X', context.get('X', 0)) or 0))

    @staticmethod
    def finish_discover(game_state, source_id, controller, value):
        game_state.trigger_ability(source_id, 'DISCOVER', {
            'discovering_player': controller,
            'discover_value': int(value),
            'source_id': source_id,
        })

    @staticmethod
    def put_rest_on_bottom(game_state, controller, card_ids):
        rest = list(card_ids or [])
        random.shuffle(rest)
        for card_id in rest:
            if card_id in controller.get('exile', []):
                game_state.move_card(
                    card_id, controller, 'exile', controller, 'library',
                    cause='discover_bottom')

    def _apply_effect(self, game_state, source_id, controller, targets):
        value = self._resolve_value(game_state, targets)
        revealed = []
        discovered = None
        while controller.get('library', []):
            card_id = controller['library'][0]
            card = game_state._safe_get_card(card_id)
            if not game_state.move_card(
                    card_id, controller, 'library', controller, 'exile',
                    cause='discover'):
                break
            if (card and 'land' not in getattr(card, 'card_types', [])
                    and float(getattr(card, 'cmc', 0) or 0) <= value):
                discovered = card_id
                break
            revealed.append(card_id)
        if discovered is None:
            self.put_rest_on_bottom(game_state, controller, revealed)
            self.finish_discover(game_state, source_id, controller, value)
            return True
        game_state.choice_context = {
            'type': 'resolution_choice', 'choice_kind': 'discover',
            'player': controller, 'options': [discovered], 'optional': True,
            'source_id': source_id, 'discover_card_id': discovered,
            'discover_rest': revealed,
            'discover_value': value,
            'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class SuspectEffect(AbilityEffect):
    """Mark creatures suspected: menace plus an inability to block."""

    def __init__(self, targeted=False, optional=False, clear_all=False,
                 clear_source=False, attached=False, condition=None):
        self.targeted = bool(targeted)
        self.optional = bool(optional)
        self.clear_all = bool(clear_all)
        self.clear_source = bool(clear_source)
        self.attached = bool(attached)
        text = ("All suspected creatures are no longer suspected"
                if clear_all else "Target creature becomes suspected"
                if targeted else "This creature becomes suspected")
        super().__init__(text, condition)
        self.requires_target = bool(targeted)

    @staticmethod
    def _clear_one(game_state, player, card_id):
        player.setdefault('suspected_permanents', set()).discard(card_id)
        game_state.layer_system.remove_effects_by_source(
            card_id, effect_description_contains='suspected:')

    def _apply_effect(self, game_state, source_id, controller, targets):
        if self.clear_all:
            for player in (game_state.p1, game_state.p2):
                for card_id in list(player.setdefault(
                        'suspected_permanents', set())):
                    self._clear_one(game_state, player, card_id)
            game_state.layer_system.apply_all_effects()
            return True
        candidates = []
        if isinstance(targets, dict):
            candidates.extend(targets.get('creatures', []))
            candidates.extend(targets.get('permanents', []))
        if self.attached and not candidates:
            for player in (game_state.p1, game_state.p2):
                attached_to = player.get('attachments', {}).get(source_id)
                if attached_to is not None:
                    candidates.append(attached_to)
                    break
        card_id = candidates[0] if candidates else source_id
        player, zone = game_state.find_card_location(card_id)
        card = game_state._safe_get_card(card_id)
        if self.clear_source:
            if player:
                self._clear_one(game_state, player, card_id)
                game_state.layer_system.apply_all_effects()
            return True
        if (not player or zone != 'battlefield' or not card
                or 'creature' not in getattr(card, 'card_types', [])):
            return bool(self.optional and not candidates)
        self._clear_one(game_state, player, card_id)
        player.setdefault('suspected_permanents', set()).add(card_id)
        for effect_type, value in (('add_ability', 'menace'),
                                   ('cant_block', True)):
            game_state.layer_system.register_effect({
                'source_id': card_id, 'layer': 6, 'affected_ids': [card_id],
                'effect_type': effect_type, 'effect_value': value,
                'duration': 'permanent',
                'description': f'suspected: {effect_type}',
            })
        game_state.layer_system.apply_all_effects()
        return True


class TransferSuspectEffect(AbilityEffect):
    """Suspect another controlled creature, then clear the source."""

    def __init__(self, condition=None):
        super().__init__(
            "You may suspect one of the other creatures; if you do, this "
            "creature is no longer suspected", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        if source_id not in controller.setdefault('suspected_permanents', set()):
            return True
        candidates = []
        for card_id in controller.get('battlefield', []):
            card = game_state._safe_get_card(card_id)
            if (card_id != source_id and card
                    and 'creature' in getattr(card, 'card_types', [])):
                candidates.append(card_id)
        if not candidates:
            return True
        game_state.choice_context = {
            'type': 'resolution_choice', 'choice_kind': 'transfer_suspect',
            'player': controller, 'options': candidates, 'optional': True,
            'source_id': source_id, 'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class InvestigateEffect(AbilityEffect):
    def __init__(self, count=1, condition=None):
        self.count = (max(1, int(count)) if str(count).isdigit()
                      else str(count).lower())
        super().__init__(
            "Investigate" if self.count == 1
            else f"Investigate {self.count} times", condition)

    def _resolve_count(self, game_state, controller, targets):
        if isinstance(self.count, int):
            return self.count
        if self.count == 'opponents_more_cards':
            return sum(
                1 for player in (game_state.p1, game_state.p2)
                if player is not controller
                and len(player.get('hand', [])) > len(controller.get('hand', [])))
        if self.count == 'target_players_creatures':
            player_ids = (targets or {}).get('players', [])
            players = [game_state.p1 if pid == 'p1' else game_state.p2
                       for pid in player_ids]
            return sum(
                1 for player in players
                for card_id in player.get('battlefield', [])
                if 'creature' in getattr(
                    game_state._safe_get_card(card_id), 'card_types', []))
        context = getattr(self, 'resolution_context', {}) or {}
        return max(0, int((targets or {}).get(
            'X', context.get('X', 0)) or 0))

    def _apply_effect(self, game_state, source_id, controller, targets):
        token_data = game_state.get_token_data_by_index(4) or {
            "name": "Clue", "type_line": "Token Artifact - Clue",
            "card_types": ["artifact"], "subtypes": ["Clue"],
            "oracle_text": "{2}, Sacrifice this artifact: Draw a card.",
        }
        created = [game_state.create_token(controller, token_data)
                   for _ in range(self._resolve_count(
                       game_state, controller, targets))]
        return all(card_id is not None for card_id in created)


class AmassEffect(AbilityEffect):
    def __init__(self, amount=1, condition=None):
        super().__init__(f"Amass {amount}", condition)
        self.amount = max(1, int(amount))

    def _apply_effect(self, game_state, source_id, controller, targets):
        return bool(game_state.amass(controller, self.amount))


class VentureEffect(AbilityEffect):
    def __init__(self, condition=None):
        super().__init__("Venture into the dungeon", condition)

    def _apply_effect(self, game_state, source_id, controller, targets):
        return bool(game_state.venture(controller))


class AdaptEffect(AbilityEffect):
    def __init__(self, amount=1, condition=None):
        super().__init__(f"Adapt {amount}", condition)
        self.amount = max(1, int(amount))

    def _apply_effect(self, game_state, source_id, controller, targets):
        return bool(game_state.adapt(controller, source_id, self.amount))


class GoadEffect(AbilityEffect):
    def __init__(self, condition=None):
        super().__init__("Goad target creature", condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            for key in ("creatures", "permanents", "chosen"):
                target_ids.extend(targets.get(key, []))
        return bool(target_ids and game_state.goad_creature(target_ids[0]))


class ExploreEffect(AbilityEffect):
    """Have the selected creature explore, deferring its nonland choice."""

    def __init__(self, count=1, condition=None, targeted=False):
        subject = "Target creature you control" if targeted else "This creature"
        super().__init__(f"{subject} explores", condition)
        self.count = (max(1, int(count)) if str(count).isdigit()
                      else str(count).lower())
        self.requires_target = bool(targeted)

    def _resolve_count(self, targets):
        if isinstance(self.count, int):
            return self.count
        context = getattr(self, 'resolution_context', {}) or {}
        return max(0, int((targets or {}).get(
            'X', context.get('X', 0)) or 0))

    def _apply_effect(self, game_state, source_id, controller, targets):
        candidates = []
        if isinstance(targets, dict):
            candidates.extend(targets.get("creatures", []))
            candidates.extend(targets.get("permanents", []))
        if not candidates:
            candidates.append(source_id)
        for creature_id in dict.fromkeys(candidates):
            target_controller, target_zone = game_state.find_card_location(creature_id)
            target = game_state._safe_get_card(creature_id)
            if (target_controller is controller and target_zone == "battlefield"
                    and target and "creature" in getattr(target, "card_types", [])):
                count = self._resolve_count(targets)
                for index in range(count):
                    active_choice = getattr(game_state, 'choice_context', None)
                    if (active_choice and active_choice.get('type') in
                            getattr(game_state, '_ASYNC_EFFECT_CHOICE_TYPES', ())):
                        for _ in range(index, count):
                            ExploreEffect().apply(
                                game_state, source_id, controller,
                                {'creatures': [creature_id]},
                                context=getattr(
                                    self, 'resolution_context', {}))
                        return True
                    if not game_state.explore(
                            controller, creature_id, source_id=source_id):
                        return False
                return True
        return False


class EndureEffect(AbilityEffect):
    """Choose counters on the enduring creature or an equally sized Spirit."""

    def __init__(self, value, subject_event=False,
                 value_from_source_counters=False, condition=None):
        self.value = (int(value) if str(value).isdigit()
                      else str(value).lower())
        self.subject_event = bool(subject_event)
        self.value_from_source_counters = bool(value_from_source_counters)
        super().__init__(f"This creature endures {self.value}", condition)
        self.requires_target = False

    def _resolve_value(self, game_state, source_id, targets):
        if isinstance(self.value, int):
            return self.value
        if self.value_from_source_counters:
            source = game_state._safe_get_card(source_id)
            return sum(int(value or 0) for value in getattr(
                source, 'counters', {}).values()) if source else 0
        context = getattr(self, 'resolution_context', {}) or {}
        return max(0, int((targets or {}).get(
            'X', context.get('X', 0)) or 0))

    def _apply_effect(self, game_state, source_id, controller, targets):
        context = getattr(self, 'resolution_context', {}) or {}
        creature_id = (context.get('event_card_id')
                       if self.subject_event else source_id)
        creature_controller, zone = game_state.find_card_location(creature_id)
        creature = game_state._safe_get_card(creature_id)
        if (creature_controller is not controller or zone != 'battlefield'
                or not creature
                or 'creature' not in getattr(creature, 'card_types', [])):
            return False
        game_state.choice_context = {
            'type': 'resolution_choice', 'choice_kind': 'endure',
            'player': controller, 'options': ['counters', 'spirit'],
            'optional': False, 'source_id': source_id,
            'endure_creature_id': creature_id,
            'endure_value': self._resolve_value(
                game_state, source_id, targets),
            'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class PrepareFromGraveyardEffect(AbilityEffect):
    """Optionally exile an exact number of cards to prepare the source."""

    def __init__(self, count=8, condition=None):
        self.count = max(1, int(count))
        super().__init__(
            f"You may exile {self.count} cards from your graveyard. "
            "If you do, this creature becomes prepared.", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        source_controller, source_zone = game_state.find_card_location(
            source_id)
        # A permanent cannot become prepared while it is already prepared.
        if (source_controller is not controller
                or source_zone != "battlefield"
                or source_id in game_state.prepared_cards):
            return True
        options = list(controller.get("graveyard", []))
        if len(options) < self.count:
            return True
        source = game_state._safe_get_card(source_id)
        game_state.choice_context = {
            "type": "prepared_payment", "player": controller,
            "controller": controller, "source_id": source_id,
            "source_generation": getattr(
                source, "_zone_change_generation", 0),
            "required_count": self.count, "options": options,
            "selected_cards": [], "choice_page": 0,
            "optional": True, "resume_phase": game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        return True


class OptionalManaThenEffect(AbilityEffect):
    """Expose 'you may pay {cost}; if you do, ...' during resolution."""

    def __init__(self, mana_cost, followup_text, condition=None):
        self.mana_cost = str(mana_cost).strip()
        self.followup_text = str(followup_text).strip()
        super().__init__(
            f"You may pay {self.mana_cost}. If you do, {self.followup_text}",
            condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        parsed_cost = game_state.mana_system.parse_mana_cost(self.mana_cost)
        payment_context = {
            'card_id': source_id,
            'optional_resolution_payment': True,
        }
        if not game_state.mana_system.can_pay_mana_cost_with_lands(
                controller, parsed_cost, payment_context):
            return True
        game_state.choice_context = {
            'type': 'resolution_choice',
            'choice_kind': 'optional_mana_then',
            'player': controller, 'options': ['pay'], 'optional': True,
            'source_id': source_id, 'mana_cost': self.mana_cost,
            'followup_text': self.followup_text,
            'targets': copy.deepcopy(targets),
            'resolution_context': copy.deepcopy(
                getattr(self, 'resolution_context', {}) or {}),
            'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class OptionalDiscardThenEffect(AbilityEffect):
    """Expose ``you may discard a card; if you do, ...`` as one choice.

    The selected hand object is both the accept decision and the discard
    selection.  Keeping the follow-up inside the same effect prevents generic
    clause splitting from drawing unconditionally when the discard is
    declined or cannot be performed.
    """

    def __init__(self, followup_text, condition=None):
        self.followup_text = str(followup_text or '').strip()
        super().__init__(
            f"You may discard a card. If you do, {self.followup_text}",
            condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        options = list(controller.get('hand', []))
        if not options:
            return True
        # Resolution contexts commonly retain the live Card object used by
        # mana payment.  Cards own locks and other engine state that cannot be
        # deep-copied into a choice continuation.  Use the stack's established
        # declarative copier, which omits runtime objects and preserves each
        # serializable rule field independently.
        resolution_context = game_state._copy_stack_context(
            getattr(self, 'resolution_context', {}) or {})
        game_state.choice_context = {
            'type': 'resolution_choice',
            'choice_kind': 'optional_discard_then',
            'player': controller,
            'options': options,
            'optional': True,
            'source_id': source_id,
            'followup_text': self.followup_text,
            'targets': copy.deepcopy(targets),
            'resolution_context': resolution_context,
            'resume_phase': game_state.PHASE_PRIORITY,
            'choice_page': 0,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        return True


class ShufflePermanentsIntoOwnersLibrariesEffect(AbilityEffect):
    """Shuffle the source and a chosen permanent into their owners' libraries."""

    def __init__(self, condition=None):
        super().__init__(
            "Shuffle source and target creature with a stun counter on it into "
            "their owners' libraries",
            condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            target_ids.extend(targets.get("creatures", []))
            target_ids.extend(targets.get("permanents", []))
        if not target_ids:
            return False

        moved = False
        owners_to_shuffle = []
        for permanent_id in dict.fromkeys([source_id, target_ids[0]]):
            current_controller, current_zone = game_state.find_card_location(permanent_id)
            if not current_controller or current_zone != "battlefield":
                continue
            owner = game_state._find_card_owner_fallback(permanent_id) or current_controller
            if game_state.move_card(
                    permanent_id, current_controller, "battlefield", owner, "library",
                    cause="shuffle_into_library"):
                moved = True
                if owner not in owners_to_shuffle:
                    owners_to_shuffle.append(owner)
        for owner in owners_to_shuffle:
            game_state.shuffle_library(owner)
        return moved


class CreateEmblemEffect(AbilityEffect):
    """Create a persistent command-zone emblem rules object."""

    def __init__(self, emblem_text, condition=None):
        self.emblem_text = str(emblem_text).strip().strip('"')
        normalized = self.emblem_text.lower()
        if "ninjas you control get +1/+1" in normalized:
            self.kind = "ninja_anthem"
        elif ("play lands" in normalized and "cast permanent spells" in normalized
              and "from your graveyard" in normalized):
            self.kind = "graveyard_permanents"
        else:
            self.kind = "generic"
        super().__init__(f"Create emblem: {self.emblem_text}", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        source = game_state._safe_get_card(source_id)
        controller.setdefault("emblems", []).append({
            "kind": self.kind,
            "text": self.emblem_text,
            "source_name": getattr(source, "name", None),
        })
        if game_state.layer_system:
            game_state.layer_system.invalidate_cache()
            game_state.layer_system.apply_all_effects()
        return True


class ReturnAsEnchantmentEffect(AbilityEffect):
    """Return Enduring Curiosity if its death snapshot permits it."""

    def __init__(self, condition=None):
        super().__init__(
            "Return source to the battlefield under its owner's control as an enchantment",
            condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        last_known = getattr(self, "resolution_context", {}).get("last_known", {})
        if not last_known.get("was_creature", False):
            return True
        if last_known.get("was_token", False):
            return True
        owner_key = last_known.get("owner_key")
        owner = game_state.p1 if owner_key == "p1" else (
            game_state.p2 if owner_key == "p2" else None)
        if not owner:
            owner, zone = game_state.find_card_location(source_id)
        else:
            zone = "graveyard" if source_id in owner.get("graveyard", []) else None
        if not owner or zone != "graveyard":
            return True
        return game_state.move_card(
            source_id, owner, "graveyard", owner, "battlefield",
            cause="enduring_return",
            context={"return_as_enchantment": True})


class CreateRoleEffect(AbilityEffect):
    """Create and attach one of the Role tokens used by the sample decks."""
    ROLE_TEXT = {
        "cursed": (
            "Enchant creature\n"
            "Enchanted creature has base power and toughness 1/1."
        ),
        "monster": (
            "Enchant creature\n"
            "Enchanted creature gets +1/+1 and has trample."
        ),
        "royal": (
            "Enchant creature\n"
            "Enchanted creature gets +1/+1 and has ward {1}."
        ),
        "sorcerer": (
            "Enchant creature\n"
            "Enchanted creature gets +1/+1.\n"
            "Whenever enchanted creature attacks, scry 1."
        ),
        "young hero": (
            "Enchant creature\n"
            "Whenever enchanted creature attacks, if its toughness is 3 or "
            "less, put a +1/+1 counter on it."
        ),
        "virtuous": (
            "Enchant creature\n"
            "Enchanted creature gets +1/+1 for each enchantment you control."
        ),
        "wicked": (
            "Enchant creature\n"
            "Enchanted creature gets +1/+1.\n"
            "When this Aura is put into a graveyard from the battlefield, "
            "each opponent loses 1 life."
        ),
    }

    def __init__(self, role_name, attachment_text="target creature", condition=None):
        normalized = str(role_name).strip().lower()
        if normalized not in self.ROLE_TEXT:
            raise ValueError(f"Unsupported Role token: {role_name}")
        self.role_name = normalized
        self.attachment_text = str(attachment_text).strip().lower()
        super().__init__(
            f"Create a {normalized.title()} Role token attached to {self.attachment_text}",
            condition)
        self.requires_target = "target" in self.attachment_text

    def _apply_effect(self, game_state, source_id, controller, targets):
        candidates = []
        if isinstance(targets, dict):
            candidates.extend(targets.get("creatures", []))
            candidates.extend(targets.get("permanents", []))

        target_id = None
        for candidate in dict.fromkeys(candidates):
            target_controller, target_zone = game_state.find_card_location(candidate)
            target = game_state._safe_get_card(candidate)
            if (target_zone != "battlefield" or not target
                    or "creature" not in getattr(target, "card_types", [])):
                continue
            if "you control" in self.attachment_text and target_controller is not controller:
                continue
            target_id = candidate
            break
        if target_id is None:
            logging.debug(f"CreateRoleEffect: no legal creature in {targets}.")
            return False

        role_title = self.role_name.title()
        token_data = {
            "name": f"{role_title} Role",
            "type_line": "Enchantment - Aura Role",
            "card_types": ["enchantment"],
            "subtypes": ["aura", "role"],
            "oracle_text": self.ROLE_TEXT[self.role_name],
            "color_identity": [],
            "power": 0,
            "toughness": 0,
            "is_token": True,
        }
        role_id = game_state.create_token(
            controller, token_data, attach_to_target=target_id)
        if role_id is None:
            return False
        if controller.get("attachments", {}).get(role_id) == target_id:
            return True
        if not game_state._is_legal_attachment(role_id, target_id):
            logging.warning(
                f"CreateRoleEffect: {role_title} Role cannot attach to {target_id}.")
            game_state.move_card(
                role_id, controller, "battlefield", controller, "graveyard",
                cause="illegal_role_attachment")
            return False
        return game_state.attach_aura(controller, role_id, target_id)


class ReturnToHandEffect(AbilityEffect):
    """Effect that returns cards to their owner's hand."""
    def __init__(self, target_type="permanent", zone="battlefield", condition=None,
                 scope="target", min_targets=1, max_targets=1,
                 excluded_subtypes=None):
        target_type_str = str(target_type).lower() if target_type is not None else "permanent"
        zone_str = str(zone).lower() if zone is not None else "battlefield"
        # scope: 'target' | 'all' (all matching permanents) | 'all_yours'
        # (all you control). July 2026 parser expansion for mass bounce.
        self.scope = scope
        self.excluded_subtypes = {
            str(subtype).lower() for subtype in (excluded_subtypes or set())}
        self.min_targets = int(min_targets)
        self.max_targets = int(max_targets)
        if scope == "target" and self.min_targets == 0:
            count_word = {1: "one", 2: "two", 3: "three"}.get(
                self.max_targets, str(self.max_targets))
            desc = f"Return up to {count_word} target"
        else:
            desc = "Return target" if scope == "target" else "Return all"
        super().__init__(f"{desc} {target_type_str} from {zone_str} to its owner's hand", condition)
        self.target_type = target_type_str
        self.zone = zone_str
        self.requires_target = scope == "target" and "target" in self.effect_text.lower()


    def _apply_effect(self, game_state, source_id, controller, targets):
        returned_count = 0
        resolved_noop = False
        target_ids_to_process = []

        # --- Target Collection (Improved) ---
        relevant_categories = set()
        if "creature" == self.target_type: relevant_categories.add("creatures")
        elif "artifact" == self.target_type: relevant_categories.add("artifacts")
        # ... add other specific types ...
        elif "permanent" == self.target_type: relevant_categories.update(["permanents", "creatures", "artifacts", "enchantments", "planeswalkers", "lands", "battles"])
        elif "spell or permanent" == self.target_type:
            relevant_categories.update([
                "spells", "permanents", "creatures", "artifacts",
                "enchantments", "planeswalkers", "lands", "battles"])
        elif "card" == self.target_type: relevant_categories.add("cards") # Assumes target dict might have 'cards' key for GY/Exile targets
        else: relevant_categories.add(self.target_type + "s") # Pluralize fallback

        if self.scope in ("all", "all_yours"):
            # Mass bounce: gather matching permanents across battlefields.
            players = [controller] if self.scope == "all_yours" else [game_state.p1, game_state.p2]
            for p in players:
                if not p: continue
                for cid in list(p.get("battlefield", [])):
                    card = game_state._safe_get_card(cid)
                    card_subtypes = {
                        str(subtype).lower() for subtype in getattr(
                            card, 'subtypes', [])} if card else set()
                    if self.excluded_subtypes.intersection(card_subtypes):
                        continue
                    if self.target_type in ("permanent",) or (card and self.target_type in [t.lower() for t in getattr(card, 'card_types', [])]):
                        target_ids_to_process.append(cid)
        elif self.requires_target:
            if not targets or not any(v for v in targets.values()):
                return True
            for category in relevant_categories:
                 target_ids_to_process.extend(targets.get(category, []))

        if not target_ids_to_process:
             # Empty mass sets and targets filtered out by resolution
             # validation both resolve without moving anything.
             return True

        spell_target_ids = set((targets or {}).get("spells", []))
        # Process unique targets
        for target_id in set(target_ids_to_process):
            if target_id in spell_target_ids:
                stack_match = next((
                    (index, item)
                    for index, item in enumerate(game_state.stack)
                    if isinstance(item, tuple) and len(item) >= 4
                    and item[0] == "SPELL" and item[1] == target_id), None)
                if not stack_match:
                    logging.warning(
                        f"ReturnToHandEffect: Target spell {target_id} "
                        "was not found on the stack.")
                    continue
                stack_index, stack_item = stack_match
                _, spell_id, spell_controller, spell_context = stack_item
                game_state.stack.pop(stack_index)
                game_state.last_stack_size = len(game_state.stack)
                if spell_context.get("is_copy", False):
                    returned_count += 1
                    continue
                owner = (game_state._find_card_owner_fallback(spell_id)
                         or spell_controller)
                if game_state.move_card(
                        spell_id, spell_controller, "stack_implicit", owner,
                        "hand", cause="return_to_hand",
                        context={"source_id": source_id}):
                    returned_count += 1
                continue
            # A repeated deck ID can have one physical occurrence in a
            # graveyard and another on the battlefield.  For an effect whose
            # declared source zone is the battlefield, resolve the targeted
            # permanent occurrence directly instead of accepting a newer
            # unrelated move hint from find_card_location().
            if self.zone == 'battlefield':
                battlefield_controller = game_state.get_card_controller(target_id)
                location_info = ((battlefield_controller, 'battlefield')
                                 if battlefield_controller is not None else None)
            else:
                location_info = game_state.find_card_location(target_id)
            if not location_info:
                 # A previously legal object can leave its expected zone
                 # before a later instruction reaches it.  The instruction
                 # then does nothing; that is a successful rules no-op, not a
                 # runtime warning or an effect-sequence failure.
                 logging.debug(
                     f"ReturnToHandEffect: Target {target_id} already left "
                     f"the expected zone.")
                 resolved_noop = True
                 continue

            target_owner, current_zone = location_info

            # Validate source zone specified in effect constructor
            if self.zone != 'any' and current_zone != self.zone:
                logging.debug(f"ReturnToHandEffect: Target {target_id} not in expected zone '{self.zone}', found in '{current_zone}'. Skipping.")
                continue

            # Perform the move using GameState method
            if game_state.move_card(target_id, target_owner, current_zone, target_owner, "hand", cause="return_to_hand", context={"source_id": source_id}):
                 returned_count += 1
                 # Logging handled within move_card
            else:
                 source = game_state._safe_get_card(source_id)
                 source_name = getattr(source, 'name', source_id)
                 logging.warning(
                     f"ReturnToHandEffect from {source_name}: Failed to "
                     f"move {target_id} to hand from {current_zone}.")

        return returned_count > 0 or resolved_noop


class ReturnThenAddCounterEffect(AbilityEffect):
    """Return an optional target, then counter the source only on success.

    This preserves result-linked wording such as ``If a permanent was
    returned this way`` as one resolution unit.  Supplying no target for an
    ``up to one`` instruction is a successful resolution with no follow-up.
    """
    def __init__(self, effect_text, target_type="permanent",
                 counter_type="+1/+1", count=1, condition=None):
        self.target_type = str(target_type or "permanent").lower()
        self.counter_type = str(counter_type or "+1/+1")
        self.count = max(1, int(count))
        super().__init__(effect_text, condition)
        self.min_targets = 0
        self.max_targets = 1
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            for category in (
                    "permanents", "creatures", "artifacts", "enchantments",
                    "lands", "planeswalkers", "battles"):
                target_ids.extend(targets.get(category, []))
        target_ids = list(dict.fromkeys(target_ids))
        if not target_ids:
            return True

        hand_counts = []
        for target_id in target_ids:
            target_controller = game_state.get_card_controller(target_id)
            owner = game_state._find_card_owner_fallback(target_id) \
                or target_controller
            if owner:
                hand_counts.append(
                    (owner, target_id, owner.get("hand", []).count(target_id)))

        bounce = ReturnToHandEffect(
            target_type=self.target_type, zone="battlefield")
        bounce._apply_effect(game_state, source_id, controller, targets)
        returned = any(
            owner.get("hand", []).count(target_id) > before
            for owner, target_id, before in hand_counts)
        if returned and source_id in controller.get("battlefield", []):
            return bool(game_state.add_counter(
                source_id, self.counter_type, self.count))
        return True


class CounterSpellEffect(AbilityEffect):
    """Effect that counters a spell on the stack."""
    def __init__(self, target_type="spell", condition=None):
        target_type_str = str(target_type).lower() if target_type else "spell"
        super().__init__(f"Counter target {target_type_str}", condition)
        self.target_type = target_type_str
        self.requires_target = True


    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = targets.get("spells", []) # Expect targets in 'spells' list
        if not target_ids:
            return True

        countered_count = 0
        target_left_stack = False
        # Typically counters one target, but handle list in case of "Counter up to two..."
        for target_id in target_ids:
            # Find the spell on the stack
            target_item = None
            target_index = -1
            for i, item in enumerate(game_state.stack):
                 if isinstance(item, tuple) and len(item) > 3 and item[0] == "SPELL" and item[1] == target_id:
                      target_item = item
                      target_index = i
                      break

            if not target_item:
                logging.debug(
                    "CounterSpellEffect: target spell %s already left the "
                    "stack before resolution.", target_id)
                target_left_stack = True
                continue # Try next target if any

            spell_type, spell_id, spell_caster, spell_context = target_item
            spell = game_state._safe_get_card(spell_id)
            if not spell: continue # Should not happen

            # Check "can't be countered"
            # Use central check if available, otherwise text check
            can_be_countered = True
            if hasattr(game_state, 'check_rule'): # Ideal way
                 can_be_countered = not game_state.check_rule('cant_be_countered', {'card_id': spell_id})
            elif hasattr(spell, 'oracle_text'): # Fallback
                 can_be_countered = "can't be countered" not in spell.oracle_text.lower()
            # A cast can be uncounterable through how it was paid (Cavern of
            # Souls' restricted mana), recorded on the stack item context.
            if spell_context.get('cant_be_countered'):
                 can_be_countered = False

            if not can_be_countered:
                logging.debug(f"Cannot counter {spell.name} - it can't be countered")
                continue

            # Remove from stack and move to graveyard
            game_state.stack.pop(target_index)
            if not spell_context.get("is_copy", False): # Don't move copies
                # Handle replacements for going to GY (e.g., Rest in Peace -> Exile)
                # Use move_card with stack_implicit source
                game_state.move_card(spell_id, spell_caster, "stack_implicit", spell_caster, "graveyard", cause="countered")
            logging.debug(f"Countered {spell.name}")
            countered_count += 1
            # Stop after countering one spell unless effect says "up to N"?
            break # Default: Counter first valid target

        # Check SBAs? Unlikely needed here, main loop handles post-resolution checks.
        # A target leaving the stack is a successful rules no-op. Preserve
        # the historical False result when a present target cannot actually
        # be countered (for example Cavern of Souls).
        return countered_count > 0 or target_left_stack

class DiscardEffect(AbilityEffect):
    """Effect that causes players to discard cards."""
    def __init__(self, count=1, target="opponent", is_random=False, condition=None):
        # Handle 'x' for description
        if count == 'x':
             count_text = "X card(s)"
             self.base_count = 'x'
        elif count == -1: # Represents "all"
             count_text = "their hand"
             self.base_count = -1
        else: # Specific number
             count_num = text_to_number(count) # Ensure it's a number
             count_text = f"{count_num} card{'s' if count_num != 1 else ''}"
             self.base_count = count_num

        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        random_text = " at random" if is_random else ""
        super().__init__(f"{target_desc} discards {count_text}{random_text}", condition)
        # self.base_count stored above
        self.target = target
        self.is_random = is_random
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        has_chosen_x = isinstance(targets, dict) and 'X' in targets
        x_value = targets.get('X', 0) if has_chosen_x else 0
        if self.base_count == 'x' and has_chosen_x:
            effective_count = x_value
            logging.debug(f"DiscardEffect: Using X={x_value} for discard count.")
        else:
            effective_count = self.base_count # Already numeric (-1 for all, or N)
        # --- End X Cost Handling ---

        target_players = []
        # Target selection logic... (remains the same)
        if self.target == "controller": target_players.append(controller)
        elif self.target == "opponent": target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            if player_ids: target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else: logging.warning(f"DiscardEffect target_player failed: No player ID in targets {targets}"); return False
        elif self.target == "each_player": target_players.extend([p for p in [game_state.p1, game_state.p2] if p])

        if not target_players: return False
        return game_state.start_discard_choice(
            target_players,
            count=effective_count,
            source_id=source_id,
            is_random=self.is_random,
            cause="discard",
        )

class GraveyardAdventurePermissionEffect(AbilityEffect):
    """Allow a dying Adventure card to cast that half from its graveyard."""

    def __init__(self):
        super().__init__(
            "you may cast it from your graveyard as an Adventure until the "
            "end of your next turn")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        return game_state.grant_graveyard_adventure_permission(
            controller, source_id)


class GrantFlashbackEffect(AbilityEffect):
    """Grant a targeted instant/sorcery in your graveyard Flashback this turn."""

    def __init__(self, condition=None):
        super().__init__(
            "Target instant or sorcery card in your graveyard gains "
            "flashback until end of turn", condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        card_ids = targets.get("cards", []) if isinstance(targets, dict) else []
        if not card_ids:
            return False
        card_id = card_ids[0]
        card = game_state._safe_get_card(card_id)
        return game_state.grant_flashback_permission(
            controller, card_id, getattr(card, "mana_cost", ""))


class RuleDeclarationEffect(AbilityEffect):
    """A recognized rules permission with no separate resolving action."""

    def __init__(self, effect_text):
        super().__init__(effect_text)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        return True


class ImpulseDrawEffect(AbilityEffect):
    """Exile the top N cards; the controller may play them for a duration.

    First-touch sweep (July 2026): "exile the top card, you may play it this
    turn" (impulse draw) had no handler -- it hit the generic no-op fallback,
    so the cards were never exiled and never became playable. The whole
    mechanic did nothing. This moves the cards to exile and registers them in
    gs.cards_castable_from_exile, the set the action space already reads for
    play-from-exile actions.
    """
    def __init__(self, count=1, duration="end_of_turn", condition=None):
        super().__init__(f"Exile the top {count} card(s); you may play them", condition)
        self.count = count
        self.duration = duration
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        if not controller or not controller.get("library"):
            return False
        count = min(self.count, len(controller["library"]))
        if count <= 0:
            return True
        exiled = []
        for _ in range(count):
            card_id = controller["library"][0]
            if game_state.move_card(card_id, controller, "library", controller, "exile",
                                    cause="impulse_draw"):
                exiled.append(card_id)
                if not hasattr(game_state, "cards_castable_from_exile"):
                    game_state.cards_castable_from_exile = set()
                game_state.cards_castable_from_exile.add(card_id)
                # Track for end-of-turn cleanup so the permission expires.
                if not hasattr(game_state, "impulse_until_eot"):
                    game_state.impulse_until_eot = set()
                if self.duration == "end_of_turn":
                    game_state.impulse_until_eot.add(card_id)
            else:
                break
        if exiled:
            logging.debug(f"Impulse draw: exiled {exiled}, now playable from exile.")
            return True
        return False


class MillEffect(AbilityEffect):
    """Effect that mills cards from library to graveyard."""
    def __init__(self, count=1, target="opponent", condition=None):
        count_str = "X" if count == 'x' else str(count) # Represent X in description
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        super().__init__(f"{target_desc} mills {count_str} card{'s' if count == 'x' or count > 1 else ''}", condition)
        self.base_count = count # Store original 'x' or number
        self.target = target
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        has_chosen_x = isinstance(targets, dict) and 'X' in targets
        x_value = targets.get('X', 0) if has_chosen_x else 0
        if self.base_count == 'x' and has_chosen_x:
            effective_count = x_value
            logging.debug(f"MillEffect: Using X={x_value} for mill count.")
        else:
            effective_count = text_to_number(self.base_count)
        # --- End X Cost Handling ---

        if effective_count <= 0: return True

        target_players = []
        # Target selection logic... (remains the same)
        if self.target == "controller": target_players.append(controller)
        elif self.target == "opponent": target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            if player_ids: target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else: logging.warning(f"MillEffect target_player failed: No player ID in targets {targets}"); return False
        elif self.target == "each_player": target_players.extend([p for p in [game_state.p1, game_state.p2] if p])

        if not target_players: return False

        overall_success = True
        for p in target_players:
             player_library = p.get("library", [])
             if not player_library: logging.debug(f"MillEffect: Player {p['name']}'s library is empty."); continue

             # Use effective_count
             num_to_mill = min(effective_count, len(player_library))
             if num_to_mill <= 0: continue

             ids_to_mill = player_library[:num_to_mill]
             actual_milled_count = 0
             for card_id in ids_to_mill:
                  # Use move_card (library source zone implicit)
                  success_move = game_state.move_card(card_id, p, "library", p, "graveyard", cause="mill", context={"source_id": source_id})
                  if success_move: actual_milled_count += 1
                  else: pass # Logging in move_card

             logging.debug(f"MillEffect: Milled {actual_milled_count} card(s) from {p['name']}'s library.")
             overall_success &= (actual_milled_count > 0)
             # Tracking logic... (remains the same)
             if actual_milled_count > 0:
                  if not hasattr(game_state, 'cards_milled_this_turn'): game_state.cards_milled_this_turn = {}
                  player_id = 'p1' if p == game_state.p1 else 'p2'
                  game_state.cards_milled_this_turn[player_id] = game_state.cards_milled_this_turn.get(player_id, 0) + actual_milled_count
                  if not p.get("library"): p["library_empty_warning"] = True

        return overall_success


class MillThenChooseEffect(AbilityEffect):
    """Mill the controller, then optionally recover one newly milled card.

    The eligible set is bound to the physical moves performed by this effect,
    rather than the controller's whole graveyard.  This models the common
    ``from among the milled cards`` wording while reusing the policy-visible
    Dig choice machinery for the optional selection.
    """
    _PERMANENT_TYPES = frozenset({
        "artifact", "battle", "creature", "enchantment", "land",
        "planeswalker",
    })

    def __init__(self, count=1, allowed_types=None, optional=True,
                 effect_text=None, condition=None):
        self.base_count = count
        self.allowed_types = frozenset(
            str(card_type).lower().rstrip("s")
            for card_type in (allowed_types or ("permanent",)))
        self.optional = bool(optional)
        count_text = "X" if count == "x" else str(count)
        allowed_text = ", ".join(sorted(self.allowed_types))
        super().__init__(
            effect_text or (
                f"Mill {count_text} cards. You may put a {allowed_text} card "
                "from among the milled cards into your hand"),
            condition)
        self.requires_target = False

    def _is_eligible(self, game_state, card_id):
        card = game_state._safe_get_card(card_id)
        if not card:
            return False
        card_types = {
            str(card_type).lower().rstrip("s")
            for card_type in getattr(card, "card_types", [])
        }
        if "permanent" in self.allowed_types:
            return bool(card_types.intersection(self._PERMANENT_TYPES))
        return bool(card_types.intersection(self.allowed_types))

    def _apply_effect(self, game_state, source_id, controller, targets):
        has_chosen_x = isinstance(targets, dict) and "X" in targets
        if self.base_count == "x" and has_chosen_x:
            count = int(targets.get("X", 0))
        else:
            count = text_to_number(self.base_count)
        if not isinstance(count, int) or count <= 0:
            return True

        library = controller.get("library", [])
        to_mill = list(library[:min(count, len(library))])
        milled = []
        for card_id in to_mill:
            graveyard_count = controller.get("graveyard", []).count(card_id)
            moved = game_state.move_card(
                card_id, controller, "library", controller, "graveyard",
                cause="mill", context={"source_id": source_id})
            if (moved and controller.get("graveyard", []).count(card_id)
                    > graveyard_count):
                milled.append(card_id)

        if milled:
            if not hasattr(game_state, "cards_milled_this_turn"):
                game_state.cards_milled_this_turn = {}
            player_id = "p1" if controller is game_state.p1 else "p2"
            game_state.cards_milled_this_turn[player_id] = (
                game_state.cards_milled_this_turn.get(player_id, 0)
                + len(milled))
            if not controller.get("library"):
                controller["library_empty_warning"] = True

        options = [
            card_id for card_id in milled
            if self._is_eligible(game_state, card_id)
        ]
        if not options:
            return True

        game_state.choice_context = {
            "type": "dig_select", "player": controller,
            "options": options, "remaining": 1, "selected": [],
            "source_zone": "graveyard", "destination": "hand",
            "rest_destination": "stay", "optional": self.optional,
            "move_cause": "milled_card_selection",
            "source_id": source_id, "resume_phase": game_state.phase,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class SearchLibraryEffect(AbilityEffect):
    """Effect that allows searching a library for cards."""
    def __init__(self, search_type="any", destination="hand", count=1,
                 condition=None, shuffle_required=True, enters_tapped=False,
                 evidence_search_type=None, search_target_controller=False,
                 untap_land_threshold=None, policy_choice=False,
                 optional=False, allowed_types=None, max_mana_value=None,
                 required_subtype=None):
        target_desc = "your library" # Assuming most searches target controller's library
        dest_desc = f"into {destination}" if destination != 'library' else "on top of your library" # Basic phrasing
        super().__init__(f"Search {target_desc} for {count} {search_type} card(s) and put {dest_desc}", condition)
        self.search_type = search_type
        self.destination = destination.lower()
        self.count = count
        self.shuffle_required = shuffle_required # Usually true unless effect says otherwise
        self.enters_tapped = enters_tapped  # "...onto the battlefield tapped"
        self.evidence_search_type = evidence_search_type
        self.search_target_controller = search_target_controller
        self.untap_land_threshold = untap_land_threshold
        self.policy_choice = bool(policy_choice)
        self.optional = bool(optional)
        self.allowed_types = {
            str(card_type).lower() for card_type in (allowed_types or [])}
        self.max_mana_value = max_mana_value
        self.required_subtype = (
            str(required_subtype).lower() if required_subtype else None)

    def _is_policy_candidate(self, game_state, card_id, search_type):
        card = game_state._safe_get_card(card_id)
        if not card:
            return False
        card_types = {
            str(card_type).lower()
            for card_type in getattr(card, "card_types", [])}
        subtypes = {
            str(subtype).lower()
            for subtype in getattr(card, "subtypes", [])}
        type_line = str(getattr(card, "type_line", "") or "").lower()
        if self.allowed_types and not self.allowed_types.intersection(card_types):
            return False
        if self.required_subtype and self.required_subtype not in subtypes:
            return False
        if (self.max_mana_value is not None
                and float(getattr(card, "cmc", 0) or 0) > self.max_mana_value):
            return False
        normalized = str(search_type or "any").lower()
        if normalized == "any":
            return True
        if normalized == "basic plains":
            return "basic" in type_line and "plains" in subtypes
        if normalized == "basic land":
            return "basic" in type_line and "land" in card_types
        if normalized == "basic plains or small creature":
            return (("basic" in type_line and "plains" in subtypes)
                    or ("creature" in card_types
                        and float(getattr(card, "cmc", 0) or 0) <= 1))
        return normalized in card_types or normalized in subtypes

    def _apply_effect(self, game_state, source_id, controller, targets):
        # Search usually targets controller's library unless specified otherwise
        player_to_search = controller
        if targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            player_to_search = game_state.p1 if player_id == "p1" else game_state.p2
        elif self.search_target_controller and isinstance(targets, dict):
            target_ids = []
            for values in targets.values():
                if isinstance(values, list):
                    target_ids.extend(values)
            controller_keys = targets.get(
                "_last_known_target_controllers", {})
            for target_id in target_ids:
                controller_key = controller_keys.get(target_id)
                if controller_key in {"p1", "p2"}:
                    player_to_search = (
                        game_state.p1 if controller_key == "p1"
                        else game_state.p2)
                    break
                owner, _ = game_state.find_card_location(target_id)
                if owner:
                    player_to_search = owner
                    break
        # Add logic here if effect text specifies searching opponent's library

        active_search_type = self.search_type
        if (self.evidence_search_type
                and isinstance(targets, dict)
                and targets.get("evidence_collected", False)):
            active_search_type = self.evidence_search_type

        if self.policy_choice:
            options = [
                card_id for card_id in player_to_search.get("library", [])
                if self._is_policy_candidate(
                    game_state, card_id, active_search_type)]
            if not options:
                if self.shuffle_required:
                    game_state.shuffle_library(player_to_search)
                return True
            game_state.choice_context = {
                "type": "dig_select", "player": player_to_search,
                "options": options,
                "remaining": min(self.count, len(options)), "selected": [],
                "source_zone": "library", "destination": self.destination,
                "rest_destination": "stay", "optional": self.optional,
                "move_cause": "library_search", "source_id": source_id,
                "resume_phase": game_state.PHASE_PRIORITY,
                "shuffle_after": self.shuffle_required,
                "enters_tapped": self.enters_tapped,
            }
            game_state.phase = game_state.PHASE_CHOOSE
            game_state.priority_player = player_to_search
            return True

        found_card_ids = []
        num_to_find = self.count
        search_attempts = 0
        max_search_attempts = self.count * 2 + 1 # Safety break for choosing

        while num_to_find > 0 and search_attempts < max_search_attempts:
            search_attempts += 1
            # Use GameState method which should incorporate AI choice/player interaction
            if hasattr(game_state, 'search_library_and_choose'):
                 ai_context = {"goal": active_search_type, "count_needed": num_to_find}
                 # Provide list of already found cards to avoid duplicates
                 found_id = game_state.search_library_and_choose(
                     player_to_search, active_search_type,
                     ai_choice_context=ai_context,
                     exclude_ids=found_card_ids, shuffle=False)
                 if found_id:
                      found_card_ids.append(found_id)
                      num_to_find -= 1
                 else: # No more valid cards found
                      break
            else: # Fallback if GS method missing
                 logging.warning("SearchLibraryEffect requires GameState.search_library_and_choose method.")
                 break

        # Move found cards to destination
        success_moves = 0
        if found_card_ids:
             for card_id in found_card_ids:
                  card = game_state._safe_get_card(card_id)
                  card_name = card.name if card else card_id
                  # search_library_and_choose currently places its selection in
                  # hand. Count that as the completed move for hand searches;
                  # for other destinations, move onward from hand exactly once.
                  already_in_destination = (
                      self.destination == "hand"
                      and card_id in player_to_search.get("hand", []))
                  source_zone = "hand" if card_id in player_to_search.get("hand", []) \
                      else "library_implicit"
                  moved = already_in_destination or game_state.move_card(
                      card_id, player_to_search, source_zone,
                      player_to_search, self.destination, cause="search_effect")
                  if moved:
                      success_moves += 1
                      if self.enters_tapped and self.destination == "battlefield":
                          player_to_search.setdefault("tapped_permanents", set()).add(card_id)
                      if (self.untap_land_threshold is not None
                              and self.destination == "battlefield"):
                          land_count = sum(
                              1 for permanent_id in player_to_search.get(
                                  "battlefield", [])
                              if "land" in getattr(
                                  game_state._safe_get_card(permanent_id),
                                  "card_types", []))
                          if land_count >= self.untap_land_threshold:
                              player_to_search.setdefault(
                                  "tapped_permanents", set()).discard(card_id)
                      logging.debug(f"Search found '{card_name}' matching '{active_search_type}', moved to {self.destination}"
                                    f"{' tapped' if self.enters_tapped and self.destination == 'battlefield' else ''}.")
                  else:
                      logging.warning(f"Search found '{card_name}', but failed to move to {self.destination}.")
                      # Return to library?
                      player_to_search.setdefault("library",[]).append(card_id) # Add back to lib if move fails

             # Shuffle library if required (and if library was searched)
             if self.shuffle_required:
                 game_state.shuffle_library(player_to_search)
        else: # Nothing found
            logging.debug(f"Search failed for '{active_search_type}' in {player_to_search['name']}'s library.")
            # Shuffle library even if search fails, if it was inspected
            if self.shuffle_required: game_state.shuffle_library(player_to_search)

        # Searching a hidden zone may legally find nothing. A chosen card that
        # could not reach its instructed destination is an engine failure, not
        # a legal miss.
        return (not found_card_ids
                or success_moves == len(found_card_ids))

class AddManaEffect(AbilityEffect):
    """Add mana to a player's pool (rituals, and spell effects that make mana).
    July 2026 parser expansion: "Add {B}{B}{B}" as a SPELL effect (Dark Ritual
    et al.) hit the generic no-op fallback and produced nothing. Mana
    ACTIVATED abilities on permanents are handled separately (ManaAbility).
    """
    def __init__(self, mana_dict=None, any_color_count=0, condition=None):
        self.mana_dict = mana_dict or {}
        self.any_color_count = any_color_count  # "add N mana of any color/one color"
        desc = "".join(f"{{{c}}}" * n for c, n in self.mana_dict.items()) or f"{any_color_count} any"
        super().__init__(f"Add {desc}", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        added = False
        if self.mana_dict:
            mana_str = "".join(("{%s}" % c) * n for c, n in self.mana_dict.items())
            if hasattr(game_state, 'mana_system') and game_state.mana_system:
                game_state.mana_system.add_mana_to_pool(controller, mana_str)
            else:
                pool = controller.setdefault("mana_pool", {})
                for c, n in self.mana_dict.items():
                    pool[c] = pool.get(c, 0) + n
            added = True
        if self.any_color_count > 0:
            # v1: default any-color to green (a deterministic legal choice);
            # color choice is a Tier 3 agent-choice item.
            pool = controller.setdefault("mana_pool", {})
            pool["G"] = pool.get("G", 0) + self.any_color_count
            added = True
        return added


class ControlEffect(AbilityEffect):
    """Gain control of a permanent (Threaten / Control Magic style). July 2026
    parser expansion: "gain control of target creature" hit the generic no-op.
    Uses apply_temporary_control; duration is tracked for end-of-turn release
    by the existing temp_control_effects machinery.
    """
    def __init__(self, target_type="creature", duration="end_of_turn", condition=None):
        self.target_type = target_type.lower()
        self.duration = duration
        super().__init__(f"Gain control of target {self.target_type}", condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        ids = []
        if isinstance(targets, dict):
            for cat in ("creatures", "permanents", "artifacts", "lands"):
                ids.extend(targets.get(cat, []))
        if not ids:
            logging.warning(f"ControlEffect: no target in {targets}")
            return False
        gained = False
        for cid in set(ids):
            if hasattr(game_state, 'apply_temporary_control') and \
                    game_state.apply_temporary_control(cid, controller):
                gained = True
                # Untap on gaining control is common (Threaten grants haste too,
                # handled separately); leave tap state as-is for v1 accuracy.
        return gained


class RegenerateEffect(AbilityEffect):
    """Grant a regeneration shield (CR 701.15). July 2026 parser expansion:
    "regenerate target creature" hit the generic no-op. Adds the creature to
    its controller's regeneration_shields; apply_regeneration consumes it in
    place of the next destruction.
    """
    def __init__(self, target_type="creature", condition=None):
        self.target_type = target_type.lower()
        super().__init__(f"Regenerate target {self.target_type}", condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        ids = []
        if isinstance(targets, dict):
            ids = list(targets.get("creatures", []) or targets.get("permanents", []))
        if not ids and "self" in str(getattr(self, 'effect_text', '')).lower():
            ids = [source_id]
        if not ids:
            logging.warning(f"RegenerateEffect: no target in {targets}")
            return False
        shielded = False
        for cid in set(ids):
            owner, zone = game_state.find_card_location(cid)
            if owner and zone == "battlefield":
                owner.setdefault("regeneration_shields", set()).add(cid)
                shielded = True
                logging.debug(f"Regeneration shield granted to {cid}.")
        return shielded


class TapEffect(AbilityEffect):
    """Effect that taps a permanent."""
    def __init__(self, target_type="permanent", condition=None, scope="target",
                 min_targets=1, max_targets=1):
        self.min_targets = int(min_targets)
        self.max_targets = int(max_targets)
        if scope == "target" and self.min_targets == 0:
            count_word = {1: "one", 2: "two", 3: "three"}.get(
                self.max_targets, str(self.max_targets))
            description = f"Tap up to {count_word} target {target_type}"
        elif scope == "all_target_player":
            description = f"Tap all {target_type} target player controls"
        else:
            description = f"Tap target {target_type}"
        super().__init__(description, condition)
        self.target_type = target_type.lower()
        # scope: 'target' (a specific permanent) or 'all_target_player' (mass
        # tap of everything a target player controls). July 2026 parser expansion.
        self.scope = scope
        self.requires_target = True


    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if self.scope == "all_target_player":
            # Tap every matching permanent the target player controls.
            pids = targets.get("players", []) if isinstance(targets, dict) else []
            tp = None
            if pids:
                tp = game_state.p1 if pids[0] == "p1" else game_state.p2
            else:
                # Fallback: the opponent of the controller.
                tp = game_state.p2 if controller == game_state.p1 else game_state.p1
            for cid in list(tp.get("battlefield", [])):
                card = game_state._safe_get_card(cid)
                if self.target_type == "permanent" or (card and self.target_type in [t.lower() for t in getattr(card, 'card_types', [])]):
                    target_ids.append(cid)
        else:
            # Collect targets from relevant categories
            cats = ["creatures", "artifacts", "lands", "permanents"] # Add others if needed
            for cat in cats:
                 target_ids.extend(targets.get(cat, []))
        if not target_ids:
            if self.scope == "target" and self.min_targets == 0:
                return True
            logging.warning(f"TapEffect failed: No targets provided/resolved in dict {targets}")
            return False

        tapped_count = 0
        for target_id in set(target_ids): # Process unique targets
             target_owner, target_zone = game_state.find_card_location(target_id)
             if not target_owner or target_zone != "battlefield":
                  logging.debug(f"TapEffect: Target {target_id} not valid for tapping.")
                  continue
             # Filter by type if necessary (e.g., "Tap target creature")
             if self.target_type != "permanent":
                 card = game_state._safe_get_card(target_id)
                 if not card or self.target_type not in getattr(card,'card_types',[]) : continue # Skip if type mismatch

             if game_state.tap_permanent(target_id, target_owner):
                  tapped_count += 1
                  # Logging inside tap_permanent

        return tapped_count > 0

class UntapEffect(AbilityEffect):
    """Effect that untaps a permanent."""
    def __init__(self, target_type="permanent", condition=None, scope="target"):
        # scope: 'target' | 'all_yours' | 'self'.
        self.scope = scope
        super().__init__(f"Untap {'target' if scope=='target' else 'all'} {target_type}", condition)
        self.target_type = target_type.lower()
        self.requires_target = scope == "target"


    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if self.scope == "self":
            target_ids = [source_id]
        elif self.scope == "all_yours":
            for cid in list(controller.get("battlefield", [])):
                card = game_state._safe_get_card(cid)
                if self.target_type == "permanent" or (card and self.target_type in [t.lower() for t in getattr(card, 'card_types', [])]):
                    target_ids.append(cid)
        else:
            cats = ["creatures", "artifacts", "lands", "permanents"]
            for cat in cats:
                 target_ids.extend(targets.get(cat, []))
        if not target_ids:
            logging.warning(f"UntapEffect failed: No targets provided/resolved in dict {targets}")
            return False

        untapped_count = 0
        for target_id in set(target_ids): # Process unique targets
             target_owner, target_zone = game_state.find_card_location(target_id)
             if not target_owner or target_zone != "battlefield":
                  logging.debug(f"UntapEffect: Target {target_id} not valid for untapping.")
                  continue
             # Filter by type if necessary
             if self.target_type != "permanent":
                 card = game_state._safe_get_card(target_id)
                 if not card or self.target_type not in getattr(card,'card_types',[]): continue # Skip if type mismatch

             if game_state.untap_permanent(target_id, target_owner):
                  untapped_count += 1
                  # Logging inside untap_permanent

        return untapped_count > 0


class DigEffect(AbilityEffect):
    """Look at the top N cards; put one (or more) into your hand, the rest on
    the bottom (or top). July 2026 parser expansion: impulsive-dig selection
    ("look at the top three, put one into your hand and the rest on the
    bottom") hit the no-op fallback. The controller chooses the card(s).
    """
    def __init__(self, look=3, take=1, rest="bottom", condition=None,
                 bonus_take=None, bonus_condition=None,
                 rest_order="preserve"):
        self.look = look
        self.take = take
        self.rest = rest  # 'bottom' | 'top' | 'graveyard'
        self.bonus_take = bonus_take
        self.bonus_condition = bonus_condition
        # ``preserve`` keeps the looked-at order, ``choice`` exposes a second
        # policy choice for "in any order", and ``random`` shuffles only the
        # remainder before placing it.  This is separate from the library's
        # ordinary shuffle instruction: the kept cards must never participate.
        self.rest_order = rest_order
        super().__init__(f"Look at the top {look}, put {take} into hand, rest on {rest}", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        lib = controller.get("library", [])
        if not lib:
            return True
        look = self.look
        if look == "lands_you_control":
            look = sum(
                1 for card_id in controller.get("battlefield", [])
                if "land" in getattr(
                    game_state._safe_get_card(card_id), "card_types", []))
        look = max(0, int(look))
        n = min(look, len(lib))
        looked = lib[:n]
        del lib[:n]
        requested_take = self.take
        graveyard_cards = [
            game_state._safe_get_card(card_id)
            for card_id in controller.get("graveyard", [])]
        if self.bonus_take is not None:
            if self.bonus_condition == "instant_and_sorcery_in_graveyard":
                types = {
                    card_type for card in graveyard_cards if card
                    for card_type in getattr(card, "card_types", [])}
                if {"instant", "sorcery"}.issubset(types):
                    requested_take = self.bonus_take
            elif self.bonus_condition == "three_lessons_in_graveyard":
                lessons = sum(
                    1 for card in graveyard_cards if card
                    and "lesson" in getattr(card, "subtypes", []))
                if lessons >= 3:
                    requested_take = self.bonus_take
            elif self.bonus_condition == "kicked":
                context = getattr(self, "resolution_context", {}) or {}
                if context.get("kicked") or context.get("actual_kicker_paid"):
                    requested_take = self.bonus_take
        take = min(requested_take, len(looked))
        if take <= 0:
            controller["library"].extend(looked)
            return True
        game_state.choice_context = {
            "type": "dig_select", "player": controller, "options": looked,
            "remaining": take, "selected": [], "rest_destination": self.rest,
            "rest_order": self.rest_order,
            "source_id": source_id, "resume_phase": game_state.phase,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class PutOnLibraryEffect(AbilityEffect):
    """Put a target permanent onto the top or bottom of its owner's library
    (tempo removal / tuck). July 2026 parser expansion: "put target creature on
    top of its owner's library" hit the no-op fallback.
    """
    def __init__(self, target_type="creature", position="top", condition=None):
        self.target_type = target_type.lower()
        self.position = position  # 'top' | 'bottom'
        super().__init__(f"Put target {self.target_type} on {position} of its owner's library", condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        ids = []
        if isinstance(targets, dict):
            for cat in ("creatures", "permanents", "artifacts", "enchantments", "lands"):
                ids.extend(targets.get(cat, []))
        if not ids:
            logging.warning(f"PutOnLibraryEffect: no target in {targets}")
            return False
        moved = False
        for cid in set(ids):
            owner, zone = game_state.find_card_location(cid)
            if not owner or zone != "battlefield":
                continue
            if game_state.move_card(cid, owner, "battlefield", owner, "library", cause="put_on_library"):
                # move_card appends; reposition to top if needed.
                if self.position == "top" and cid in owner["library"]:
                    owner["library"].remove(cid)
                    owner["library"].insert(0, cid)
                moved = True
        return moved


class ShuffleGraveyardEffect(AbilityEffect):
    """Shuffle a player's graveyard into their library (graveyard hate /
    anti-mill / recursion). July 2026 parser expansion: "shuffle your
    graveyard into your library" hit the no-op fallback.
    """
    def __init__(self, who="controller", condition=None):
        self.who = who  # 'controller' | 'target_player' | 'each_player'
        super().__init__(f"Shuffle {who} graveyard into library", condition)
        self.requires_target = who == "target_player"

    def _apply_effect(self, game_state, source_id, controller, targets):
        players = []
        if self.who == "controller":
            players = [controller]
        elif self.who == "each_player":
            players = [p for p in (game_state.p1, game_state.p2) if p]
        elif self.who == "target_player":
            pids = targets.get("players", []) if isinstance(targets, dict) else []
            if pids:
                players = [game_state.p1 if pids[0] == "p1" else game_state.p2]
        if not players:
            players = [controller]
        did = False
        for p in players:
            gy = p.get("graveyard", [])
            if not gy:
                continue
            p.setdefault("library", []).extend(gy)
            p["graveyard"] = []
            did = True
            if hasattr(game_state, 'shuffle_library'):
                game_state.shuffle_library(p)
            else:
                import random as _r
                _r.shuffle(p["library"])
        return did


class PreventDamageEffect(AbilityEffect):
    """Register a damage-prevention replacement (fog / prevention shields).

    July 2026 parser expansion: "prevent all combat damage this turn" and
    "prevent the next N damage" hit the no-op fallback. Registers a DAMAGE
    replacement (the replacement system was made functional earlier this
    campaign). combat_only restricts it to combat damage; amount=None means
    prevent all (a fog), else prevent up to N.
    """
    def __init__(self, amount=None, combat_only=False, target_scope="all", condition=None):
        self.amount = amount            # None = prevent all; int = prevent next N
        self.combat_only = combat_only
        self.target_scope = target_scope  # 'all' | 'to_you' | 'target'
        super().__init__(f"Prevent {'all' if amount is None else amount} {'combat ' if combat_only else ''}damage", condition)
        self.requires_target = target_scope == "target"

    def apply(self, game_state, source_id, controller, targets=None, context=None):
        self.resolution_context = context or {}
        re_sys = getattr(game_state, 'replacement_effects', None)
        if not re_sys:
            return False
        remaining = {'n': self.amount}  # closure box for "next N" tracking
        combat_only = self.combat_only

        def _prevent(ctx):
            if ctx.get('damage_amount', 0) <= 0:
                return ctx
            if combat_only and not ctx.get('is_combat_damage', False):
                return ctx
            amt = ctx['damage_amount']
            if remaining['n'] is None:
                ctx['damage_amount'] = 0
            else:
                prevented = min(amt, remaining['n'])
                ctx['damage_amount'] = amt - prevented
                remaining['n'] -= prevented
            return ctx

        def _condition(ctx):
            if remaining['n'] is not None and remaining['n'] <= 0:
                return False
            if combat_only and not ctx.get('is_combat_damage', False):
                return False
            return True

        re_sys.register_effect({
            'event_type': 'DAMAGE',
            'replacement': _prevent,
            'condition': _condition,
            'source_id': source_id,
            'duration': 'end_of_turn',
            'description': self.effect_text,
        })
        logging.debug(f"Registered prevention: {self.effect_text}")
        return True

    def _apply_effect(self, game_state, source_id, controller, targets):
        return self.apply(game_state, source_id, controller, targets)


class ScryEffect(AbilityEffect):
    def __init__(self, count=1, condition=None):
        super().__init__(f"Scry {count}", condition)
        self.count = count

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Initiate the scry process by setting the game state."""
        if not controller or "library" not in controller or not controller["library"]:
            logging.debug(f"Cannot Scry: Player {controller.get('name', 'Unknown')} or library invalid.")
            return False # Cannot scry with no library

        count = min(self.count, len(controller["library"]))
        if count <= 0: return True # Scry 0 is valid, does nothing

        scried_cards = controller["library"][:count]
        if not scried_cards: return False # Should not happen
        # BUGFIX (July 2026 sweep): pull the looked-at cards OFF the library.
        # They were left in place, and finalize then re-prepended the kept
        # cards -> kept cards duplicated, bottom cards never actually moved.
        # The choice handler puts each card back at its chosen destination.
        del controller["library"][:count]

        # --- Set up state for external AI/ActionHandler to make choices ---
        # Store previous phase if not already in a special choice phase
        if game_state.phase not in [game_state.PHASE_CHOOSE, game_state.PHASE_TARGETING, game_state.PHASE_SACRIFICE]:
            game_state.previous_priority_phase = game_state.phase

        game_state.phase = game_state.PHASE_CHOOSE
        # Create context for the choice
        game_state.choice_context = {
            'type': 'scry',
            'player': controller,
            'count': count, # Original scry number
            'cards': scried_cards[:], # Copy of cards being looked at (list can be modified)
            'kept_on_top': [], # Store IDs player chooses to keep on top
            'put_on_bottom': [], # Store IDs player chooses to put on bottom
            'source_id': source_id,
            'resolved': False # Flag to indicate choice processing is complete
        }
        # Clear priority passing and set priority to the choosing player
        game_state.priority_pass_count = 0
        game_state.priority_player = controller # Scrying player has priority to choose

        logging.info(f"Entering Scry choice phase for {controller['name']} ({count} cards: {[getattr(game_state._safe_get_card(cid), 'name', cid) for cid in scried_cards]}).")
        return True # Initiated scry choice process successfully

class SurveilEffect(AbilityEffect):
    def __init__(self, count=1, condition=None):
        super().__init__(f"Surveil {count}", condition)
        self.count = count

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Initiate the surveil process by setting the game state."""
        if not controller or "library" not in controller or not controller["library"]:
            logging.debug(f"Cannot Surveil: Player {controller.get('name', 'Unknown')} or library invalid.")
            return False # Cannot surveil with no library

        count = min(self.count, len(controller["library"]))
        if count <= 0: return True # Surveil 0 is valid, does nothing

        surveiled_cards = controller["library"][:count]
        if not surveiled_cards: return False
        # BUGFIX (July 2026 sweep): remove looked-at cards from the library;
        # the handler re-inserts kept cards on top and moves the rest to the
        # graveyard. Leaving them caused kept cards to duplicate.
        del controller["library"][:count]

        # --- Set up state for external AI/ActionHandler to make choices ---
        # Store previous phase
        if game_state.phase not in [game_state.PHASE_CHOOSE, game_state.PHASE_TARGETING, game_state.PHASE_SACRIFICE]:
            game_state.previous_priority_phase = game_state.phase

        game_state.phase = game_state.PHASE_CHOOSE
        # Create context
        game_state.choice_context = {
            'type': 'surveil',
            'player': controller,
            'count': count,
            'cards': surveiled_cards[:], # Copy of cards to process
            'kept_on_top': [], # Unused for surveil, kept for potential future compatibility?
            'put_in_graveyard': [], # Track cards put in graveyard
            'source_id': source_id,
            'resolved': False
        }
        # Clear priority passing and set priority to the choosing player
        game_state.priority_pass_count = 0
        game_state.priority_player = controller

        logging.info(f"Entering Surveil choice phase for {controller['name']} ({count} cards: {[getattr(game_state._safe_get_card(cid), 'name', cid) for cid in surveiled_cards]}).")
        return True # Initiated surveil choice process successfully

class LifeDrainEffect(AbilityEffect):
    def __init__(self, amount=1, target="opponent", gain_target="controller", condition=None):
        super().__init__(f"Target {target} loses {amount} life and you gain {amount} life", condition)
        self.amount = amount
        self.target = target # "opponent", "each opponent", "target player"
        self.gain_target = gain_target # Usually "controller"
        self.requires_target = "target" in target # Requires specific player target?

    def _apply_effect(self, game_state, source_id, controller, targets):
        if self.amount <= 0: return True # No effect

        life_lost_this_instance = 0 # Track life lost by this specific effect application

        # --- Target(s) for Life Loss ---
        target_players_loss = []
        opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
        if self.target == "opponent":
            target_players_loss.append(opponent)
        elif self.target == "each opponent":
             # Assumes 2 players for now
             target_players_loss.append(opponent)
             # TODO: Extend for multi-player
        elif self.target == "target player":
             player_ids = targets.get("players", [])
             if player_ids:
                 p_target = game_state.p1 if player_ids[0] == "p1" else game_state.p2
                 target_players_loss.append(p_target)
             else:
                 logging.warning("LifeDrainEffect: Target player missing for life loss.")
                 return False # Needs target

        if not target_players_loss: return False

        # Apply life loss
        for p_loss in target_players_loss:
             # Life loss is different from damage
             # Use GameState method if available
             if hasattr(game_state, 'lose_life'):
                 actual_loss = game_state.lose_life(p_loss, self.amount, source_id=source_id)
                 life_lost_this_instance += actual_loss
             else: # Fallback direct modification
                 # Check for replacements manually (simplified)
                 loss_context = {'player': p_loss, 'life_amount': self.amount, 'source_id': source_id}
                 modified_context, replaced = game_state.apply_replacement_effect("LIFE_LOSS", loss_context)
                 actual_loss = modified_context.get('life_amount', 0) if not modified_context.get('prevented') else 0

                 if actual_loss > 0:
                      p_loss['life'] -= actual_loss
                      life_lost_this_instance += actual_loss
                      p_loss['lost_life_this_turn'] = True # Flag for Spectacle etc.
                      logging.debug(f"(Fallback) LifeDrainEffect: {p_loss['name']} lost {actual_loss} life.")
                      game_state.trigger_ability(None, "LOSE_LIFE", {"player": p_loss, "amount": actual_loss, "source_id": source_id})


        # --- Target for Life Gain ---
        player_gaining_life = None
        if self.gain_target == "controller":
            player_gaining_life = controller
        # TODO: Handle other gain targets if needed

        # Apply life gain (Amount depends on specific card - usually amount drained OR fixed amount)
        # Simple implementation: Gain amount equal to life lost *by this effect instance*.
        amount_to_gain = life_lost_this_instance

        if player_gaining_life and amount_to_gain > 0:
             if hasattr(game_state, 'gain_life'):
                 # gain_life handles logging and triggers
                 game_state.gain_life(player_gaining_life, amount_to_gain, source_id=source_id)
             else: # Fallback
                  gain_context = {'player': player_gaining_life, 'life_amount': amount_to_gain, 'source_id': source_id}
                  modified_gain_context, replaced = game_state.apply_replacement_effect("LIFE_GAIN", gain_context)
                  actual_gain = modified_gain_context.get('life_amount', 0) if not modified_gain_context.get('prevented') else 0
                  if actual_gain > 0:
                      player_gaining_life['life'] += actual_gain
                      logging.debug(f"(Fallback) LifeDrainEffect: {player_gaining_life['name']} gained {actual_gain} life.")
                      game_state.trigger_ability(source_id, "GAIN_LIFE", {"player": player_gaining_life, "amount": actual_gain, "source_id": source_id})

        # Check SBAs after life changes (done in main loop usually)
        # game_state.check_state_based_actions() # Optional immediate check
        return life_lost_this_instance > 0 # Return success if any life was lost


class CopySpellEffect(AbilityEffect):
    def __init__(self, target_type="spell", new_targets=True, condition=None,
                 copy_that=False):
        reference = "that spell" if copy_that else f"target {target_type}"
        super().__init__(f"Copy {reference}{' and you may choose new targets' if new_targets else ''}", condition)
        self.target_type = target_type # spell, instant, sorcery
        self.new_targets = new_targets # If the copy can choose new targets
        self.copy_that = bool(copy_that)
        self.requires_target = not self.copy_that

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = targets.get("spells", []) # Expect spell target
        if not target_ids and self.copy_that:
            context = getattr(self, "resolution_context", {}) or {}
            referenced_id = context.get("cast_card_id", context.get("spell_id"))
            if referenced_id is not None:
                target_ids = [referenced_id]
        if not target_ids:
             logging.warning("CopySpellEffect: No spell target provided in targets dict.")
             return False

        original_spell_id = target_ids[0] # Assume first target

        # Find the original spell on the stack
        original_stack_item = None
        for item in reversed(game_state.stack):
            if isinstance(item, tuple) and item[0] == "SPELL" and item[1] == original_spell_id:
                 original_stack_item = item
                 break

        if not original_stack_item:
             logging.warning(f"CopySpellEffect: Target spell {original_spell_id} not found on stack.")
             return False

        _, spell_id, _, _ = original_stack_item

        # Check target type restriction if specified
        spell_card = game_state._safe_get_card(spell_id)
        if not spell_card: return False # Card vanished?
        spell_types = {str(card_type).lower()
                       for card_type in getattr(spell_card, 'card_types', [])}
        restriction = str(self.target_type).lower()
        if "instant or sorcery" in restriction and not ({"instant", "sorcery"} & spell_types):
            return False
        if restriction in {"instant", "instant spell"} and "instant" not in spell_types:
            return False
        if restriction in {"sorcery", "sorcery spell"} and "sorcery" not in spell_types:
            return False
        if restriction in {"creature", "creature spell"} and "creature" not in spell_types:
            return False

        return game_state.copy_spell_on_stack(
            original_stack_item,
            controller,
            copied_by=source_id,
            allow_new_targets=self.new_targets,
        ) is not None


class CreateTokenCopyOfTargetEffect(AbilityEffect):
    """Create a token using a targeted permanent's copyable values."""

    def __init__(self, allowed_types=None, controller_only=True,
                 condition=None):
        allowed = set(allowed_types or {"artifact", "creature"})
        self.allowed_types = {str(card_type).lower() for card_type in allowed}
        self.controller_only = bool(controller_only)
        super().__init__(
            "Create a token that's a copy of target artifact or creature you control",
            condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            for category in (
                    "artifacts", "creatures", "permanents", "chosen",
                    "targets"):
                values = targets.get(category, [])
                if isinstance(values, (list, tuple, set)):
                    target_ids.extend(values)
        target_ids = list(dict.fromkeys(target_ids))
        if not target_ids:
            logging.warning(
                "CreateTokenCopyOfTargetEffect: no permanent target supplied.")
            return False

        target_id = target_ids[0]
        target = game_state._safe_get_card(target_id)
        if not target:
            return False
        target_types = {
            str(card_type).lower()
            for card_type in getattr(target, 'card_types', [])}
        if not target_types.intersection(self.allowed_types):
            return False
        if (self.controller_only
                and game_state.get_card_controller(target_id) is not controller):
            return False
        return game_state.create_token_copy(target, controller) is not None


class CreateTokenCopyOfSourceEffect(AbilityEffect):
    """Create a token from the source permanent's copyable values."""

    def __init__(self, condition=None):
        super().__init__(
            "Create a token that's a copy of this creature", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        source = game_state._safe_get_card(source_id)
        if (not source
                or source_id not in controller.get("battlefield", [])
                or "creature" not in getattr(source, "card_types", [])):
            return False
        return game_state.create_token_copy(source, controller) is not None


class ManaSpentConditionalEffect(AbilityEffect):
    """Apply a nested effect only when a spell used enough actual mana."""

    def __init__(self, minimum, nested_effect, condition=None):
        self.minimum = max(0, int(minimum))
        self.nested_effect = nested_effect
        super().__init__(
            f"If {self.minimum} or more mana was spent, "
            f"{nested_effect.effect_text}", condition)
        self.requires_target = bool(
            getattr(nested_effect, "requires_target", False))

    def _apply_effect(self, game_state, source_id, controller, targets):
        details = (getattr(self, "resolution_context", {}) or {}).get(
            "final_paid_details", {})
        spent = (details.get("spent_specific", {})
                 if isinstance(details, dict) else {})
        total = 0
        for amount in spent.values() if isinstance(spent, dict) else ():
            total += max(0, safe_int(amount, 0) or 0)
        if total < self.minimum:
            return True
        return self.nested_effect.apply(
            game_state, source_id, controller, targets,
            context=getattr(self, "resolution_context", {}))

class MeldEffect(AbilityEffect):
    """Exile a named meld pair and return the combined result."""
    def __init__(self, result_name=None, condition=None):
        self.result_name = result_name.strip() if result_name else None
        super().__init__(
            f"Meld them into {self.result_name or 'their meld result'}", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        source = game_state._safe_get_card(source_id)
        if not source or source_id not in controller.get("battlefield", []):
            return False
        partner_name = getattr(source, "meld_partner_name", None)
        result_name = self.result_name or getattr(source, "meld_result_name", None)
        if not partner_name or not result_name:
            return False

        partner_id = next((
            card_id for card_id in controller.get("battlefield", [])
            if card_id != source_id
            and getattr(game_state._safe_get_card(card_id), "name", "").lower()
            == partner_name.lower()
        ), None)
        result_id = next((
            card_id for card_id, card in game_state.card_db.items()
            if getattr(card, "name", "").lower() == result_name.lower()
        ), None)
        if partner_id is None or result_id is None:
            return False
        return game_state.meld_cards(source_id, partner_id, result_id, controller)


class TransformEffect(AbilityEffect):
    def __init__(self, condition=None):
        super().__init__("Transform this permanent", condition)
        self.requires_target = False # Usually affects self

    def _apply_effect(self, game_state, source_id, controller, targets):
        # Transform usually targets the source itself
        target_id = source_id
        # Allow context to override target if necessary (e.g., specific instruction)
        # Check if context provides a specific permanent target ID
        target_id_from_context = None
        if targets and isinstance(targets, dict) and "permanents" in targets and targets["permanents"]:
             target_id_from_context = targets["permanents"][0] # Assume first permanent target
        elif targets and isinstance(targets, list): # Handle flat list if passed by simple resolver
             # Cannot determine if it's the intended target, default to source_id
             pass

        if target_id_from_context and target_id_from_context != source_id:
             target_id = target_id_from_context
             logging.debug(f"Transform effect targeting {target_id} instead of source {source_id} due to context.")

        # Use GameState method to handle transformation and triggers
        if hasattr(game_state, 'transform_card') and callable(game_state.transform_card):
             # transform_card handles validation (is transformable, can transform now?)
             success = game_state.transform_card(target_id)
             if success:
                 card = game_state._safe_get_card(target_id)
                 logging.debug(f"Successfully triggered transform for {getattr(card,'name', target_id)}")
                 return True
             else:
                 logging.debug(f"Transform failed for {target_id} (handled by game_state.transform_card).")
                 return False
        else:
             logging.error("TransformEffect failed: GameState lacks 'transform_card' method.")
             return False


class SetDayNightEffect(AbilityEffect):
    """Apply an explicit instruction that makes it day or night (CR 727.1)."""

    def __init__(self, state, condition=None):
        normalized = str(state).strip().lower()
        if normalized not in ("day", "night"):
            raise ValueError(f"Invalid day/night state: {state}")
        self.state = normalized
        super().__init__(f"It becomes {normalized}", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        changed = game_state.day_night_state != self.state
        game_state.day_night_state = self.state
        if changed:
            game_state.transform_day_night_cards()
        return True

class FightEffect(AbilityEffect):
    def __init__(self, target_type="creature", condition=None,
                 fighter="source", optional=False):
        # Ensure effect text correctly reflects the source fighting the target
        description = (
            "Target creature you control fights target creature you don't control"
            if fighter == "target_pair"
            else f"This creature fights target {target_type}")
        super().__init__(description, condition)
        self.target_type = target_type # Usually creature
        self.fighter = fighter
        self.optional = bool(optional)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        fighter1_id = source_id # The source of the fight effect
        if self.fighter == "enchanted_creature":
            fighter1_id = controller.get("attachments", {}).get(source_id)
        fighter2_id = None

        if self.fighter == "target_pair":
            role_targets = {}
            context = getattr(self, "resolution_context", {}) or {}
            slots = list(context.get("instruction_target_slots", []) or [])
            targets_by_slot = list(context.get("targets_by_slot", []) or [])
            for slot_index, slot in enumerate(slots):
                role = slot.get("target_role")
                selected = (list(targets_by_slot[slot_index] or [])
                            if slot_index < len(targets_by_slot) else [])
                if role and selected:
                    role_targets[role] = selected[0]
            fighter1_id = role_targets.get("fighter")
            fighter2_id = role_targets.get("fight_opponent")
            if fighter1_id is None or fighter2_id is None:
                # Direct EffectFactory callers can still supply the two
                # targets in Oracle order without a casting context.
                ordered = list(targets.get("creatures", [])) + list(
                    targets.get("permanents", []))
                if len(ordered) >= 2:
                    fighter1_id, fighter2_id = ordered[:2]
            # A fight cannot occur after either one of its two targets becomes
            # illegal. The spell itself may still resolve when the other
            # target remains legal (CR 608.2b), so this is a successful no-op.
            if fighter1_id is None or fighter2_id is None:
                return True

        # Source/enchanted-creature fights use the ordinary target payload.
        if self.fighter != "target_pair":
            target_candidates = (targets.get("creatures", [])
                                 + targets.get("permanents", []))
            if not target_candidates:
                return True
            # Filter out the source if it was accidentally targeted
            possible_targets = [tid for tid in target_candidates if tid != fighter1_id]
            if possible_targets:
                fighter2_id = possible_targets[0] # Assume first valid target
            else:
                return True

        fighter1 = game_state._safe_get_card(fighter1_id)
        fighter2 = game_state._safe_get_card(fighter2_id)
        f1_owner, f1_zone = game_state.find_card_location(fighter1_id)
        f2_owner, f2_zone = game_state.find_card_location(fighter2_id)

        # Both must be creatures on the battlefield currently
        if not fighter1 or 'creature' not in getattr(fighter1, 'card_types', []) or f1_zone != 'battlefield':
            logging.debug(f"FightEffect: Fighter1 ({fighter1_id}) is not a valid creature on the battlefield.")
            return False
        if not fighter2 or 'creature' not in getattr(fighter2, 'card_types', []) or f2_zone != 'battlefield':
            logging.debug(f"FightEffect: Fighter2 ({fighter2_id}) is not a valid creature on the battlefield.")
            return False

        # Get current power post-layers (Important!)
        power1 = getattr(fighter1, 'power', 0) or 0 # Use 0 if power is None
        power2 = getattr(fighter2, 'power', 0) or 0

        logging.debug(f"Fight: {fighter1.name} ({power1} power) vs {fighter2.name} ({power2} power)")

        # Deal damage simultaneously using GameState methods that handle replacements etc.
        # Source of damage is the creature itself
        damage_dealt_by_1 = 0
        damage_dealt_by_2 = 0
        if power1 > 0:
             damage_dealt_by_1 = game_state.apply_damage_to_permanent(fighter2_id, power1, fighter1_id, is_combat_damage=False)
        if power2 > 0:
             damage_dealt_by_2 = game_state.apply_damage_to_permanent(fighter1_id, power2, fighter2_id, is_combat_damage=False)

        # SBAs checked in main loop after resolution
        game_state.trigger_ability(fighter1_id, "FIGHT_RESOLVED", {"opponent_id": fighter2_id, "damage_dealt": damage_dealt_by_1, "damage_taken": damage_dealt_by_2})
        game_state.trigger_ability(fighter2_id, "FIGHT_RESOLVED", {"opponent_id": fighter1_id, "damage_dealt": damage_dealt_by_2, "damage_taken": damage_dealt_by_1})
        # Return true if the fight happened (damage was attempted)
        return True
    
class AnimateLandEffect(AbilityEffect):
    """Target land becomes an N/N creature (still a land). July 2026 parser
    expansion: "target land becomes a 3/3 creature until end of turn" hit the
    no-op fallback. Registers layer-4 add_type (creature) and layer-7b set_pt.
    """
    def __init__(self, power=0, toughness=0, duration="end_of_turn", keep_types=True, condition=None,
                 colors=None, subtypes=None, keywords=None, self_target=False):
        self.power = power
        self.toughness = toughness
        self.duration = duration
        self.keep_types = keep_types  # "it's still a land"
        # Restless-land style self animation carries colors, a creature
        # subtype, and granted keywords ("becomes a 2/3 white and blue Bird
        # creature with flying").
        self.colors = colors or []
        self.subtypes = subtypes or []
        self.keywords = keywords or []
        self.self_target = self_target
        target_desc = "This land" if self_target else "Target land"
        super().__init__(f"{target_desc} becomes a {power}/{toughness} creature", condition)
        self.requires_target = not self_target

    def apply(self, game_state, source_id, controller, targets=None, context=None):
        self.resolution_context = context or {}
        if not getattr(game_state, 'layer_system', None):
            return False
        if self.self_target:
            ids = [source_id] if source_id is not None else []
        else:
            ids = []
            if isinstance(targets, dict):
                for cat in ("lands", "permanents", "creatures"):
                    ids.extend(targets.get(cat, []))
        if not ids:
            logging.warning(f"AnimateLandEffect: no target land in {targets}")
            return False
        applied = False
        for cid in set(ids):
            # Layer 4: add the creature type (and any printed subtype).
            game_state.layer_system.register_effect({
                'source_id': source_id, 'layer': 4, 'affected_ids': [cid],
                'effect_type': 'add_type', 'effect_value': 'creature',
                'duration': self.duration, 'description': f"animate: {cid} becomes creature",
            })
            if self.subtypes:
                game_state.layer_system.register_effect({
                    'source_id': source_id, 'layer': 4, 'affected_ids': [cid],
                    'effect_type': 'add_subtype', 'effect_value': [s.lower() for s in self.subtypes],
                    'duration': self.duration, 'description': f"animate: {cid} subtypes {self.subtypes}",
                })
            # Layer 5: set the animated colors.
            if self.colors:
                color_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
                color_vec = [0, 0, 0, 0, 0]
                for color_name in self.colors:
                    idx = color_map.get(str(color_name).lower())
                    if idx is not None:
                        color_vec[idx] = 1
                game_state.layer_system.register_effect({
                    'source_id': source_id, 'layer': 5, 'affected_ids': [cid],
                    'effect_type': 'set_color', 'effect_value': color_vec,
                    'duration': self.duration, 'description': f"animate: {cid} colors {self.colors}",
                })
            # Layer 6: granted keywords ("with flying").
            for kw in self.keywords:
                game_state.layer_system.register_effect({
                    'source_id': source_id, 'layer': 6, 'affected_ids': [cid],
                    'effect_type': 'add_ability', 'effect_value': kw,
                    'duration': self.duration, 'description': f"animate: {cid} gains {kw}",
                })
            # Layer 7b: set its P/T.
            game_state.layer_system.register_effect({
                'source_id': source_id, 'layer': 7, 'sublayer': 'b', 'affected_ids': [cid],
                'effect_type': 'set_pt', 'effect_value': (self.power, self.toughness),
                'duration': self.duration, 'description': f"animate: {cid} P/T {self.power}/{self.toughness}",
            })
            applied = True
        if applied:
            game_state.layer_system.apply_all_effects()
        return applied

    def _apply_effect(self, game_state, source_id, controller, targets):
        return self.apply(game_state, source_id, controller, targets)


class AirbendEffect(AbilityEffect):
    """Exile the selected objects and grant each owner a {2} cast."""

    def __init__(self, alternative_cost="{2}", condition=None,
                 target_description="up to one other target creature or spell"):
        super().__init__(f"airbend {target_description}", condition)
        self.alternative_cost = alternative_cost
        self.target_description = target_description
        self.requires_target = True
        # 'Airbend up to one ...' resolves legally with zero selections;
        # without the explicit bound an empty target set is misreported as a
        # mandatory-target fizzle.
        if "up to" in str(target_description).lower():
            self.min_targets = 0

    def _grant_permission(self, game_state, owner, card_id):
        if not owner or card_id not in owner.get("exile", []):
            return False
        game_state.cards_castable_from_exile.add(card_id)
        game_state.exile_alternative_costs[card_id] = self.alternative_cost
        return True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        for category in ("creatures", "artifacts", "enchantments",
                         "planeswalkers", "battles", "spells",
                         "creature_or_spell", "permanents", "chosen"):
            values = (targets or {}).get(category, [])
            if isinstance(values, (list, tuple, set)):
                target_ids.extend(values)
        target_ids = list(dict.fromkeys(target_ids))
        if not target_ids:
            return True
        applied = False
        for target_id in target_ids:
            if target_id == source_id:
                continue
            stack_match = next((
                (index, item) for index, item in enumerate(game_state.stack)
                if isinstance(item, tuple) and len(item) >= 4
                and item[0] == 'SPELL' and item[1] == target_id), None)
            if stack_match:
                stack_index, item = stack_match
                _, spell_id, spell_controller, spell_context = item
                game_state.stack.pop(stack_index)
                game_state.last_stack_size = len(game_state.stack)
                if spell_context.get('is_copy', False):
                    applied = True
                    continue
                moved = game_state.move_card(
                    spell_id, spell_controller, 'stack_implicit',
                    spell_controller, 'exile', cause='airbend')
                applied = bool(moved and self._grant_permission(
                    game_state, spell_controller, spell_id)) or applied
                continue
            current_controller, zone = game_state.find_card_location(target_id)
            card = game_state._safe_get_card(target_id)
            if (zone != 'battlefield' or not current_controller or not card
                    or 'land' in getattr(card, 'card_types', [])):
                continue
            owner = game_state._find_card_owner_fallback(target_id) \
                or current_controller
            moved = game_state.move_card(
                target_id, current_controller, 'battlefield', owner, 'exile',
                cause='airbend')
            applied = bool(
                moved and self._grant_permission(
                    game_state, owner, target_id)) or applied
        return applied


class BlinkWithCounterEffect(AbilityEffect):
    """Exile a controlled creature and immediately return it with a counter."""

    def __init__(self, counter_type="+1/+1", condition=None):
        super().__init__(
            "Exile target creature you control, then return that card to the "
            "battlefield under its owner's control with a +1/+1 counter on it",
            condition)
        self.counter_type = counter_type
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = list((targets or {}).get("creatures", []))
        if not target_ids:
            return False
        target_id = target_ids[0]
        current_controller, zone = game_state.find_card_location(target_id)
        if zone != "battlefield" or current_controller is not controller:
            return False
        owner = game_state._find_card_owner_fallback(target_id) or controller
        blink_context = {}
        if not game_state.move_card(
                target_id, controller, "battlefield", owner, "exile",
                cause="blink", context=blink_context):
            return False
        # Tokens cease to exist after leaving the battlefield.
        if target_id not in owner.get("exile", []):
            return True
        if not game_state.move_card(
                target_id, owner, "exile", owner, "battlefield",
                cause="blink_return"):
            return False
        returned_ids = [target_id]
        meld_partner_id = blink_context.get("_separated_meld_partner_id")
        if meld_partner_id is not None:
            partner_owner = game_state._find_card_owner_fallback(
                meld_partner_id) or owner
            if meld_partner_id in partner_owner.get("exile", []):
                if not game_state.move_card(
                        meld_partner_id, partner_owner, "exile",
                        partner_owner, "battlefield", cause="blink_return",
                        context={"meld_primary_id": target_id}):
                    return False
                returned_ids.append(meld_partner_id)
        applied = False
        for returned_id in returned_ids:
            applied = bool(game_state.add_counter(
                returned_id, self.counter_type, 1)) or applied
        return applied


class LessonDamageWithExileEffect(DamageWithExileReplacementEffect):
    """Combustion Technique's graveyard-scaled damage and exile rider."""

    def __init__(self, condition=None):
        super().__init__(2, includes_planeswalkers=False, condition=condition)
        self.effect_text = (
            "deals 2 plus Lessons in your graveyard damage to target creature; "
            "exile it instead if it would die this turn")

    def _apply_effect(self, game_state, source_id, controller, targets):
        lessons = sum(
            1 for card_id in controller.get("graveyard", [])
            if "lesson" in {
                str(subtype).lower() for subtype in getattr(
                    game_state._safe_get_card(card_id), "subtypes", [])})
        self.amount = 2 + lessons
        return super()._apply_effect(game_state, source_id, controller, targets)


class ResolutionModalEffect(AbilityEffect):
    """Expose a modal instruction chosen while a trigger is resolving."""

    def __init__(self, modes, source_name=None, condition=None):
        super().__init__("Choose one — " + " • ".join(modes), condition)
        self.modes = list(modes)
        self.source_name = source_name
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        if not self.modes:
            return False
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.choice_context = {
            "type": "resolution_modal", "player": controller,
            "controller": controller, "card_id": source_id,
            "source_id": source_id, "source_name": self.source_name,
            "num_choices": len(self.modes), "min_required": 1,
            "max_required": 1, "available_modes": self.modes,
            "selected_modes": [], "resume_phase": game_state.PHASE_PRIORITY,
        }
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        return True


class DiscardTwoUnlessCreatureEffect(AbilityEffect):
    """Make the controller discard one creature card or any two cards."""

    def __init__(self, condition=None):
        super().__init__(
            "Discard two cards unless you discard a creature card", condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        remaining = min(2, len(controller.get("hand", [])))
        if remaining <= 0:
            return True
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.choice_context = {
            "type": "discard", "player": controller,
            "source_id": source_id, "remaining": remaining,
            "stop_after_creature": True, "cause": "spell_effect",
            "resume_phase": game_state.PHASE_PRIORITY,
        }
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        return True


class EarthbendEffect(AbilityEffect):
    """Animate one controlled land permanently and add N +1/+1 counters."""

    def __init__(self, amount, condition=None):
        self.amount = (
            amount if amount == "event_last_known_power"
            else max(0, int(amount)))
        amount_text = "X" if amount == "event_last_known_power" else self.amount
        super().__init__(
            f"Target land you control becomes a 0/0 creature with haste "
            f"that's still a land. Put {amount_text} +1/+1 counters on it",
            condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        land_ids = []
        if isinstance(targets, dict):
            land_ids.extend(targets.get("lands", []))
            land_ids.extend(targets.get("permanents", []))
        if not land_ids:
            return False
        land_id = land_ids[0]
        land_controller, zone = game_state.find_card_location(land_id)
        card = game_state._safe_get_card(land_id)
        if (land_controller is not controller or zone != "battlefield"
                or not card or "land" not in getattr(card, "card_types", [])):
            return False
        if not AnimateLandEffect(
                0, 0, duration="permanent", keep_types=True,
                keywords=["haste"], self_target=True).apply(
                    game_state, land_id, controller, targets={}):
            return False
        amount = self.amount
        if amount == "event_last_known_power":
            last_known = getattr(
                self, "resolution_context", {}).get("last_known", {})
            amount = max(0, safe_int(last_known.get("power"), 0) or 0)
        if amount:
            game_state.add_counter(land_id, "+1/+1", amount)
        game_state.earthbent_lands[land_id] = {
            "controller": "p1" if controller is game_state.p1 else "p2",
            "source_id": source_id,
        }
        game_state.trigger_ability(source_id, "EARTHBEND", {
            "controller": controller, "land_id": land_id,
            "amount": amount,
        })
        return True


class RevealHandEffect(AbilityEffect):
    """A player reveals their hand. July 2026 parser expansion: "target player
    reveals their hand" hit the no-op fallback. In a two-player perfect-info
    sim this is mostly informational, but marking it lets downstream effects
    ("you choose a card", discard-selection) and triggers key off it.
    """
    def __init__(self, who="target_player", condition=None):
        self.who = who
        super().__init__(f"{who} reveals their hand", condition)
        self.requires_target = who == "target_player"

    def _apply_effect(self, game_state, source_id, controller, targets):
        players = []
        if self.who == "controller":
            players = [controller]
        elif self.who == "each_player":
            players = [p for p in (game_state.p1, game_state.p2) if p]
        elif self.who == "target_player":
            pids = targets.get("players", []) if isinstance(targets, dict) else []
            if pids:
                players = [game_state.p1 if pids[0] == "p1" else game_state.p2]
        if not players:
            players = [controller]
        for p in players:
            p["hand_revealed"] = True
            game_state.trigger_ability(source_id, "HAND_REVEALED",
                                       {"player": p, "controller": controller})
            logging.debug(f"{p.get('name','player')} reveals their hand ({len(p.get('hand', []))} cards).")
        return True


class HandSelectionEffect(AbilityEffect):
    """Expose a target hand's legal cards to the choosing policy."""
    def __init__(self, noncreature_nonland=False, optional=False, rummage=False,
                 excluded_types=None):
        super().__init__("Choose a card from target player's hand")
        self.noncreature_nonland = noncreature_nonland
        self.excluded_types = {
            str(card_type).lower() for card_type in (excluded_types or ())}
        if noncreature_nonland:
            self.excluded_types.update({"creature", "land"})
        self.optional = optional
        self.rummage = rummage
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        pids = targets.get('players', []) if isinstance(targets, dict) else []
        target_player = game_state.p1 if pids and pids[0] == 'p1' else game_state.p2 if pids else None
        if not target_player:
            return False
        target_player['hand_revealed'] = True
        legal = []
        for card_id in target_player.get('hand', []):
            card = game_state._safe_get_card(card_id)
            types = {str(t).lower() for t in getattr(card, 'card_types', [])} if card else set()
            if self.excluded_types.intersection(types):
                continue
            legal.append(card_id)
        if not legal:
            return True
        game_state.choice_context = {
            'type': 'hand_selection', 'player': controller, 'target_player': target_player,
            'source_id': source_id, 'options': legal, 'optional': self.optional,
            'rummage': self.rummage, 'resume_phase': game_state.phase,
            'choice_page': 0,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class OptionalSacrificeProliferateEffect(AbilityEffect):
    """Cacophony Scamp's optional sacrifice followed by its gated rider."""
    def __init__(self):
        super().__init__("You may sacrifice this creature. If you do, proliferate")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        if source_id not in controller.get('battlefield', []):
            return True
        game_state.choice_context = {
            'type': 'optional_sacrifice_proliferate', 'player': controller,
            'source_id': source_id, 'options': [source_id],
            'resume_phase': game_state.phase,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class CausticBroncoAttackEffect(AbilityEffect):
    """Resolve Caustic Bronco's linked reveal, hand move, and life rider."""

    def __init__(self):
        super().__init__(
            "Reveal the top card of your library and put it into your hand; "
            "apply Caustic Bronco's saddled life rider")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        if not controller or not controller.get("library"):
            return True
        card_id = controller["library"][0]
        card = game_state._safe_get_card(card_id)
        mana_value = max(0, int(getattr(card, "cmc", 0) or 0))
        if not game_state.move_card(
                card_id, controller, "library", controller, "hand",
                cause="caustic_bronco_reveal"):
            return False
        saddled = source_id in controller.get("saddled_permanents", set())
        life_target = "opponent" if saddled else "controller"
        return LoseLifeEffect(mana_value, target=life_target).apply(
            game_state, source_id, controller, targets={})


class BuffEffect(AbilityEffect):
    """Effect that buffs power/toughness. Registers with LayerSystem."""
    def __init__(self, power_mod, toughness_mod, target_type="creature", duration="end_of_turn", condition=None, count_expr=None):
        super().__init__(f"{target_type} gets {power_mod:+}/{toughness_mod:+}", condition)
        self.power_mod = power_mod
        self.toughness_mod = toughness_mod
        self.target_type = target_type
        self.duration = duration # 'end_of_turn' or 'permanent' (until source leaves)
        # "+X/+X where X is the number of ..." -> count computed at apply time
        # against the controller (July 2026 parser expansion).
        self.count_expr = count_expr
        self.requires_target = "target" in target_type # Check if it targets specifically

    def apply(self, game_state, source_id, controller, targets=None, context=None):
        self.resolution_context = context or {}
        """Register the buff with the Layer System."""
        if not hasattr(game_state, 'layer_system') or not game_state.layer_system:
             logging.warning("BuffEffect: LayerSystem not available.")
             return False

        # Resolve a dynamic "+X/+X where X is the number of ..." amount.
        if getattr(self, 'count_expr', None):
            x = game_state.count_dynamic_quantity(self.count_expr, controller)
            self.power_mod = x
            self.toughness_mod = x
            logging.debug(f"BuffEffect: dynamic +X/+X, X('{self.count_expr}') = {x}.")

        if self.power_mod == 0 and self.toughness_mod == 0: return True # No change

        # Determine affected IDs
        affected_ids = []
        if self.requires_target:
            if targets and "creatures" in targets: affected_ids = targets["creatures"]
            elif targets and "permanents" in targets: affected_ids = targets["permanents"] # Assume can buff non-creatures if type is permanent
            # ... add other target types if needed
        elif self.target_type == "creatures you control":
             affected_ids = [cid for cid in controller.get("battlefield",[]) if game_state._is_creature(cid)]
        elif self.target_type == "all creatures":
             affected_ids.extend(game_state.get_all_creatures(game_state.p1))
             affected_ids.extend(game_state.get_all_creatures(game_state.p2))
        elif self.target_type == "self":
             affected_ids.append(source_id)

        if not affected_ids:
            logging.debug("BuffEffect: No affected targets found.")
            return False # No targets to buff

        # Register with Layer System
        effect_data = {
             'source_id': source_id,
             'layer': 7, 'sublayer': 'c', # Modifiers like +N/+N
             'affected_ids': affected_ids,
             'effect_type': 'modify_pt',
             'effect_value': (self.power_mod, self.toughness_mod),
             'duration': self.duration,
             'controller_id': controller, # Store controller for conditional effects
             'description': self.effect_text
        }
        # Add conditional logic if needed for the effect's activity
        if self.duration == 'until_source_leaves':
             effect_data['condition'] = lambda gs_check: source_id in gs_check.get_card_controller(source_id).get("battlefield", []) if gs_check.get_card_controller(source_id) else False
        elif self.duration == 'permanent': # Static anthem etc. needs source condition
            effect_data['condition'] = lambda gs_check: source_id in gs_check.get_card_controller(source_id).get("battlefield", []) if gs_check.get_card_controller(source_id) else False

        effect_id = game_state.layer_system.register_effect(effect_data)
        if effect_id:
            logging.debug(f"Registered Buff effect {effect_id} ({self.power_mod:+}/{self.toughness_mod:+}) from {source_id} duration {self.duration}")
            return True
        else:
            logging.warning(f"Failed to register Buff effect from {source_id}")
            return False

    def _apply_effect(self, game_state, source_id, controller, targets):
        # This effect works by registering with LayerSystem during the 'apply' phase,
        # so this direct application method shouldn't be called unless it's a one-shot buff
        # which isn't standard. Assume registration handled by apply().
        logging.warning("BuffEffect._apply_effect called directly. Buffs should be registered via LayerSystem.")
        # Re-register for safety?
        return self.apply(game_state, source_id, controller, targets)


class DoublePowerEffect(AbilityEffect):
    """Double one creature's live power by granting +X/+0 for the duration."""

    def __init__(self, duration="end_of_turn", condition=None):
        self.duration = duration
        super().__init__(
            "Double the power of target creature you control until end of turn",
            condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = list((targets or {}).get("creatures", []))
        if not target_ids:
            target_ids = list((targets or {}).get("permanents", []))
        if not target_ids:
            return False
        target_id = target_ids[0]
        if (game_state.get_card_controller(target_id) is not controller
                or not game_state._is_creature(target_id)):
            return False
        live_power = None
        if getattr(game_state, "layer_system", None):
            live_power = game_state.layer_system.get_characteristic(
                target_id, "power")
        if live_power is None:
            live_power = getattr(
                game_state._safe_get_card(target_id), "power", 0)
        power = max(0, safe_int(live_power, 0) or 0)
        if power == 0:
            return True
        return BuffEffect(
            power, 0, target_type="target creature",
            duration=self.duration).apply(
                game_state, source_id, controller,
                {"creatures": [target_id]},
                context=getattr(self, "resolution_context", {}))


class SourceCounterThresholdRewardEffect(AbilityEffect):
    """Gate a targeted counter/keyword rider on the source's counters."""

    def __init__(self, source_counter_type, threshold,
                 added_counter_type="+1/+1", keyword="trample",
                 condition=None):
        self.source_counter_type = str(source_counter_type).lower()
        self.threshold = max(0, int(threshold))
        self.added_counter_type = added_counter_type
        self.keyword = keyword
        super().__init__(
            f"If this permanent has {self.threshold} or more "
            f"{self.source_counter_type} counters, put a "
            f"{self.added_counter_type} counter on target creature you "
            f"control. It gains {self.keyword} until end of turn", condition)
        self.requires_target = True

    def _threshold_met(self, game_state, source_id):
        source = game_state._safe_get_card(source_id)
        return bool(source and int(getattr(
            source, "counters", {}).get(self.source_counter_type, 0) or 0)
            >= self.threshold)

    def _apply_effect(self, game_state, source_id, controller, targets):
        if not self._threshold_met(game_state, source_id):
            return True
        target_ids = list((targets or {}).get("creatures", []))
        if not target_ids:
            return False
        target_id = target_ids[0]
        if game_state.get_card_controller(target_id) is not controller:
            return False
        if not game_state.add_counter(
                target_id, self.added_counter_type, 1):
            return False
        return GainKeywordEffect(
            self.keyword, target_type="target creature",
            duration="end_of_turn").apply(
                game_state, source_id, controller,
                {"creatures": [target_id]},
                context=getattr(self, "resolution_context", {}))


class TurnInsideOutEffect(AbilityEffect):
    """Pump one creature and create its one-turn manifest-dread death trigger."""

    def __init__(self, condition=None):
        super().__init__(
            "Target creature gets +3/+0 until end of turn. "
            "When it dies this turn, manifest dread.", condition)
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            target_ids.extend(targets.get("creatures", []))
            target_ids.extend(targets.get("permanents", []))
            target_ids.extend(targets.get("chosen", []))
        if not target_ids:
            return False
        target_id = target_ids[0]
        _, target_zone = game_state.find_card_location(target_id)
        if target_zone != "battlefield" or not game_state._is_creature(target_id):
            return False
        if not BuffEffect(
                3, 0, target_type="target creature",
                duration="end_of_turn").apply(
                    game_state, source_id, controller, targets):
            return False
        game_state.delayed_event_triggers.append({
            "event_type": "DIES",
            "watched_card_id": target_id,
            "controller": "p1" if controller is game_state.p1 else "p2",
            "effect_text": "manifest dread",
            "expires_turn": game_state.turn,
            "source_id": source_id,
        })
        return True


class GrantNextSpellUncounterableEffect(AbilityEffect):
    """Make the controller's next spell this turn uncounterable."""

    def __init__(self):
        super().__init__("The next spell you cast this turn can't be countered")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        controller['next_spell_uncounterable'] = True
        return True


class DayOfBlackSunEffect(AbilityEffect):
    """Snapshot the X-or-less creatures, strip abilities, then destroy them."""

    def __init__(self):
        super().__init__(
            "Each creature with mana value X or less loses all abilities until "
            "end of turn. Destroy those creatures.")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        context = getattr(self, 'resolution_context', {}) or {}
        x_value = int((targets or {}).get('X', context.get('X', 0)) or 0)
        affected = []
        for player in (game_state.p1, game_state.p2):
            for card_id in list(player.get('battlefield', [])):
                card = game_state._safe_get_card(card_id)
                if (card and 'creature' in getattr(card, 'card_types', [])
                        and float(getattr(card, 'cmc', 0) or 0) <= x_value):
                    affected.append(card_id)
        if not affected:
            return True
        game_state.layer_system.register_effect({
            'source_id': source_id, 'layer': 6, 'affected_ids': affected,
            'effect_type': 'remove_all_abilities', 'effect_value': True,
            'duration': 'end_of_turn', 'start_turn': game_state.turn,
            'description': 'Day of Black Sun ability removal',
        })
        game_state.layer_system.apply_all_effects()
        return DestroyEffect('permanent').apply(
            game_state, source_id, controller, targets={
                'permanents': affected, 'X': x_value})


class ErodeEffect(AbilityEffect):
    """Destroy the target, then let its controller search for a basic land."""

    def __init__(self):
        super().__init__(
            "Destroy target creature or planeswalker. Its controller may "
            "search their library for a basic land card")
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        for category in ('creatures', 'planeswalkers', 'permanents', 'chosen'):
            target_ids.extend((targets or {}).get(category, []))
        if not target_ids:
            return False
        target_id = target_ids[0]
        target_controller = game_state.get_card_controller(target_id)
        if not target_controller:
            return False
        DestroyEffect('permanent').apply(
            game_state, source_id, controller,
            {'permanents': [target_id]})
        options = [
            cid for cid in target_controller.get('library', [])
            if SearchLibraryEffect('basic land')._is_policy_candidate(
                game_state, cid, 'basic land')]
        if not options:
            game_state.shuffle_library(target_controller)
            return True
        game_state.choice_context = {
            'type': 'dig_select', 'player': target_controller,
            'options': options, 'remaining': 1, 'selected': [],
            'source_zone': 'library', 'destination': 'battlefield',
            'rest_destination': 'stay', 'optional': True,
            'enters_tapped': True, 'shuffle_after': True,
            'source_id': source_id, 'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = target_controller
        return True


class CounterUnlessPaysEffect(AbilityEffect):
    """Policy-visible Mana Leak effect with an exile-on-counter rider."""

    def __init__(self, cost='{3}'):
        super().__init__(f"Counter target spell unless its controller pays {cost}")
        self.cost = cost
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        spell_ids = (targets or {}).get('spells', [])
        if not spell_ids:
            return False
        target_id = spell_ids[0]
        item = next((entry for entry in game_state.stack
                     if entry[0] == 'SPELL' and entry[1] == target_id), None)
        if not item:
            return False
        spell_controller = item[2]
        if item[3].get('cant_be_countered'):
            return True
        cost = game_state.mana_system.parse_mana_cost(self.cost)
        can_pay = game_state.mana_system.can_pay_mana_cost_with_lands(
            spell_controller, cost)
        game_state.choice_context = {
            'type': 'resolution_choice', 'choice_kind': 'counter_unless_pay',
            'player': spell_controller, 'options': (['pay'] if can_pay else []),
            'optional': True, 'cost': self.cost,
            'target_spell_id': target_id, 'source_id': source_id,
            'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = spell_controller
        return True


class ArchdruidSearchEffect(AbilityEffect):
    def __init__(self):
        super().__init__("Search your library for a creature or land card")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        options = []
        for card_id in controller.get('library', []):
            card = game_state._safe_get_card(card_id)
            if card and {'creature', 'land'}.intersection(
                    getattr(card, 'card_types', [])):
                options.append(card_id)
        if not options:
            game_state.shuffle_library(controller)
            return True
        game_state.choice_context = {
            'type': 'dig_select', 'player': controller, 'options': options,
            'remaining': 1, 'selected': [], 'source_zone': 'library',
            'destination': 'hand', 'destination_by_card_type': True,
            'rest_destination': 'stay', 'optional': False,
            'shuffle_after': True, 'source_id': source_id,
            'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class DeadlyCoverUpEffect(AbilityEffect):
    def __init__(self):
        super().__init__("Destroy all creatures and collect the evidence rider")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        DestroyEffect('all creatures').apply(
            game_state, source_id, controller, targets or {})
        context = getattr(self, 'resolution_context', {}) or {}
        if not (context.get('evidence_collected')
                or (targets or {}).get('evidence_collected')):
            return True
        opponent = game_state.p2 if controller is game_state.p1 else game_state.p1
        options = list(opponent.get('graveyard', []))
        if not options:
            return True
        game_state.choice_context = {
            'type': 'resolution_choice', 'choice_kind': 'deadly_cover_up',
            'player': controller, 'options': options, 'optional': False,
            'opponent_id': 'p1' if opponent is game_state.p1 else 'p2',
            'source_id': source_id, 'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class OutsideGameCardEffect(AbilityEffect):
    def __init__(self):
        super().__init__("You may put a card you own from outside the game into your hand")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        context = getattr(self, 'resolution_context', {}) or {}
        if context and not (context.get('was_cast')
                            or context.get('source_zone') == 'stack_implicit'
                            or context.get('source_zone') in {'hand', 'exile', 'graveyard'}):
            return True
        pool_key = ('outside_game' if 'outside_game' in controller
                    else 'sideboard')
        options = list(controller.get(pool_key, []))
        if not options:
            return True
        game_state.choice_context = {
            'type': 'resolution_choice', 'choice_kind': 'outside_game',
            'player': controller, 'options': options, 'optional': True,
            'outside_zone': pool_key,
            'source_id': source_id, 'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class StrategicBetrayalEffect(AbilityEffect):
    def __init__(self):
        super().__init__("Target opponent exiles a creature they control and their graveyard")
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        player_ids = (targets or {}).get('players', [])
        opponent = (game_state.p1 if player_ids and player_ids[0] == 'p1'
                    else game_state.p2 if player_ids and player_ids[0] == 'p2'
                    else game_state.p2 if controller is game_state.p1 else game_state.p1)
        options = [cid for cid in opponent.get('battlefield', [])
                   if 'creature' in getattr(
                       game_state._safe_get_card(cid), 'card_types', [])]
        if not options:
            for card_id in list(opponent.get('graveyard', [])):
                game_state.move_card(card_id, opponent, 'graveyard', opponent,
                                     'exile', cause='strategic_betrayal')
            return True
        game_state.choice_context = {
            'type': 'resolution_choice', 'choice_kind': 'strategic_betrayal',
            'player': opponent, 'options': options, 'optional': False,
            'source_id': source_id, 'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = opponent
        return True


class CrewEffect(AbilityEffect):
    def __init__(self, power):
        self.power = int(power)
        super().__init__(f"Crew {self.power}")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        context = getattr(self, 'resolution_context', {}) or {}
        if context.get('crew_cost_paid'):
            game_state.crewed_vehicles.add(source_id)
            game_state.layer_system.register_effect({
                'source_id': source_id, 'layer': 4,
                'affected_ids': [source_id], 'effect_type': 'add_type',
                'effect_value': 'creature', 'duration': 'end_of_turn',
                'start_turn': game_state.turn,
                'description': 'Crew animation',
            })
            card = game_state._safe_get_card(source_id)
            if (card and "power is equal to the number of lands you control"
                    in getattr(card, 'oracle_text', '').lower()):
                game_state.layer_system.register_effect({
                    'source_id': source_id, 'layer': 7, 'sublayer': 'a',
                    'affected_ids': [source_id],
                    'effect_type': 'set_pt_cda',
                    'effect_value': 'land_count_power_self',
                    'duration': 'permanent',
                    'description': 'land-count power CDA',
                })
            game_state.layer_system.apply_all_effects()
            return True
        options = [cid for cid in controller.get('battlefield', [])
                   if cid != source_id
                   and cid not in controller.get('tapped_permanents', set())
                   and 'creature' in getattr(
                       game_state._safe_get_card(cid), 'card_types', [])]
        game_state.choice_context = {
            'type': 'saddle', 'crew': True, 'player': controller,
            'source_id': source_id, 'options': options, 'selected': [],
            'selected_power': 0, 'required_power': self.power,
            'resume_phase': game_state.PHASE_PRIORITY,
        }
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.priority_player = controller
        return True


class EsperGraveyardTransformEffect(AbilityEffect):
    def __init__(self):
        super().__init__("If cast from a graveyard, exile it then return transformed with a finality counter")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        context = getattr(self, 'resolution_context', {}) or {}
        if context.get('source_zone') != 'graveyard':
            return True
        card = game_state._safe_get_card(source_id)
        if not card:
            return False
        game_state.move_card(source_id, controller, 'stack_implicit', controller,
                             'exile', cause='esper_origins')
        card.set_current_face(1)
        moved = game_state.move_card(source_id, controller, 'exile', controller,
                                     'battlefield', cause='esper_origins')
        if moved:
            game_state.add_counter(source_id, 'finality', 1)
            context['skip_default_movement'] = True
            self.resolution_context['skip_default_movement'] = True
        return bool(moved)


class EsperSagaRevealPermanentEffect(AbilityEffect):
    """Resolve Summon: Esper Maduin's linked chapter-I instruction."""

    def __init__(self):
        super().__init__(
            "Reveal the top card of your library; if it is a permanent, "
            "put it into your hand")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        if not controller or not controller.get("library"):
            return True
        card_id = controller["library"][0]
        card = game_state._safe_get_card(card_id)
        permanent_types = {
            "artifact", "battle", "creature", "enchantment", "land",
            "planeswalker",
        }
        card_types = {
            str(card_type).lower()
            for card_type in getattr(card, "card_types", [])
        } if card else set()
        if not card_types.intersection(permanent_types):
            # Revealing does not otherwise change the card's position.
            return True
        return bool(game_state.move_card(
            card_id, controller, "library", controller, "hand",
            cause="esper_maduin_chapter_one"))


class EsperSagaChapterThreeEffect(AbilityEffect):
    """Snapshot Maduin's other creatures, grant the EOT anthem, sacrifice."""

    def __init__(self):
        super().__init__(
            "Other creatures you control get +2/+2 and gain trample until "
            "end of turn")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        affected = [
            card_id for card_id in controller.get("battlefield", [])
            if card_id != source_id and game_state._is_creature(card_id)
        ]
        success = True
        if affected:
            bound_targets = {"creatures": affected}
            success = bool(BuffEffect(
                2, 2, target_type="target creature",
                duration="end_of_turn").apply(
                    game_state, source_id, controller, bound_targets,
                    context=getattr(self, "resolution_context", {})))
            success = bool(GainKeywordEffect(
                "trample", target_type="target creature",
                duration="end_of_turn").apply(
                    game_state, source_id, controller, bound_targets,
                    context=getattr(self, "resolution_context", {}))) \
                and success

        # A Saga is sacrificed after its final chapter ability leaves the
        # stack. For this creature Saga the finality counter replaces that
        # graveyard move with exile; move_card owns that replacement.
        if source_id in controller.get("battlefield", []):
            success = bool(game_state.move_card(
                source_id, controller, "battlefield", controller,
                "graveyard", cause="saga_completed")) and success
        return success


class DestroyEffect(AbilityEffect):
    """Effect that destroys permanents."""
    def __init__(self, target_type="permanent", condition=None):
        super().__init__(f"Destroy target {target_type}", condition)
        self.target_type = target_type.lower() # e.g., "creature", "artifact", "nonland permanent", "all creatures"
        self.requires_target = "all " not in self.target_type


    def _apply_effect(self, game_state, source_id, controller, targets):
        targets_to_destroy = []
        # --- Target Collection ---
        if "all " in self.target_type: # Handle board wipes
            wipe_type = self.target_type.split("all ")[1].replace('s','') # 'creature', 'permanent' etc.
            for p in [game_state.p1, game_state.p2]:
                for card_id in list(p.get("battlefield",[])): # Iterate copy
                     card = game_state._safe_get_card(card_id)
                     if card:
                          # Check if card matches type to wipe
                          matches = False
                          if wipe_type == "permanent": matches = True
                          elif wipe_type == "creature" and 'creature' in getattr(card, 'card_types', []): matches = True
                          elif wipe_type == "artifact" and 'artifact' in getattr(card, 'card_types', []): matches = True
                          # Add more wipe types
                          if matches: targets_to_destroy.append((card_id, p))
        elif self.requires_target:
            # Get target IDs from resolved targets dictionary
            cats = []
            if self.target_type == "creature": cats = ["creatures"]
            elif self.target_type == "artifact": cats = ["artifacts"]
            elif self.target_type == "enchantment": cats = ["enchantments"]
            elif self.target_type == "artifact_or_enchantment":
                cats = [
                    "artifacts", "enchantments",
                    "artifact_or_enchantment", "chosen", "permanents"]
            elif self.target_type == "land": cats = ["lands"]
            elif self.target_type == "planeswalker": cats = ["planeswalkers"]
            elif self.target_type == "permanent": cats = ["creatures", "artifacts", "enchantments", "lands", "planeswalkers", "battles", "permanents"]
            elif self.target_type == "nonland permanent": cats = ["creatures", "artifacts", "enchantments", "planeswalkers", "battles", "permanents"]

            ids_found = []
            if targets:
                for cat in cats:
                    ids_found.extend(targets.get(cat, []))
            # Filter nonland if necessary
            if self.target_type == "nonland permanent":
                 ids_found = [tid for tid in ids_found if 'land' not in getattr(game_state._safe_get_card(tid),'card_types',[])]

            for target_id in set(ids_found): # Process unique targets
                 target_owner, target_zone = game_state.find_card_location(target_id)
                 if target_owner and target_zone == 'battlefield':
                     targets_to_destroy.append((target_id, target_owner))
        else: # Should not happen if requires_target is set correctly
            logging.warning(f"DestroyEffect has requires_target={self.requires_target} but no targets resolved.")
            return False

        if not targets_to_destroy: return False

        # Later instructions such as "Its controller may search their library"
        # need the target's battlefield controller even after destruction has
        # moved the object to its owner's graveyard.
        if isinstance(targets, dict):
            snapshots = targets.setdefault(
                "_last_known_target_controllers", {})
            for card_id, target_controller in targets_to_destroy:
                snapshots[card_id] = (
                    "p1" if target_controller is game_state.p1 else "p2")

        # --- Destruction ---
        destroyed_count = 0
        for card_id, owner in targets_to_destroy:
            card = game_state._safe_get_card(card_id)
            if not card: continue

            # 1. Check Indestructible
            if game_state.check_keyword(card_id, "indestructible"):
                 logging.debug(f"Cannot destroy {card.name}: Indestructible.")
                 continue

            # 2. Check Regeneration/Replacement Effects
            can_be_destroyed = True
            # Regeneration
            if game_state.apply_regeneration(card_id, owner):
                logging.debug(f"DestroyEffect: {card.name} regenerated.")
                can_be_destroyed = False
            # Totem Armor
            elif hasattr(game_state, 'apply_totem_armor') and game_state.apply_totem_armor(card_id, owner):
                 logging.debug(f"DestroyEffect: {card.name} saved by Totem Armor.")
                 can_be_destroyed = False
            # Other Replacements
            elif hasattr(game_state, 'replacement_effects'):
                 destroy_context = {'card_id': card_id, 'controller': owner, 'cause': 'destroy_effect', 'source_id': source_id}
                 modified_context, replaced = game_state.replacement_effects.apply_replacements("DESTROYED", destroy_context)
                 if replaced:
                      final_dest = modified_context.get('to_zone')
                      if final_dest and final_dest != "battlefield":
                          game_state.move_card(card_id, owner, "battlefield", owner, final_dest, cause="destroy_replaced")
                      # Else prevented
                      can_be_destroyed = False

            # 3. Perform Destruction (Move to Graveyard)
            if can_be_destroyed:
                if game_state.move_card(card_id, owner, "battlefield", owner, "graveyard", cause="destroy_effect", context={"source_id": source_id}):
                    destroyed_count += 1
                    # Logging handled by move_card

        # SBAs handled by main loop
        return destroyed_count > 0
    
class ExileLibrariesExceptBottomEffect(AbilityEffect):
    """Exile every card above the bottom N cards of each library face down."""

    def __init__(self, keep_count=6, face_down=True, condition=None):
        self.keep_count = max(0, int(keep_count))
        self.face_down = bool(face_down)
        super().__init__(
            "Each player exiles all but the bottom "
            f"{self.keep_count} cards of their library"
            + (" face down" if self.face_down else ""),
            condition)
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        for player in (game_state.p1, game_state.p2):
            if not player:
                continue
            library = player.get("library", [])
            # Library index zero is the top throughout the engine. Snapshot
            # the prefix before moving anything so the retained suffix keeps
            # its exact top-to-bottom order.
            cutoff = max(0, len(library) - self.keep_count)
            cards_to_exile = list(library[:cutoff])
            for card_id in cards_to_exile:
                game_state.move_card(
                    card_id, player, "library", player, "exile",
                    cause="exile_library_except_bottom",
                    context={
                        "source_id": source_id,
                        "face_down_exile": self.face_down,
                    })
        # An empty library, a library of six or fewer cards, or a replacement
        # that changes an individual move still resolves this instruction.
        return True


class ExileEffect(AbilityEffect):
    """Effect that exiles permanents or cards from zones."""
    def __init__(self, target_type="permanent", zone="battlefield", condition=None):
        super().__init__(f"Exile target {target_type}" + (f" from {zone}" if zone != "battlefield" else ""), condition)
        self.target_type = target_type.lower()
        self.zone = zone.lower() # graveyard, hand, library, battlefield, stack
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        targets_to_exile = []
        cats = []
        if self.target_type == "creature": cats = ["creatures"]
        elif self.target_type == "artifact": cats = ["artifacts"]
        elif self.target_type == "creature_or_vehicle": cats = ["creatures", "artifacts"]
        # Add more specific types
        elif self.target_type == "permanent": cats = ["creatures", "artifacts", "enchantments", "lands", "planeswalkers", "battles", "permanents"]
        elif self.target_type == "card": cats = ["cards"] # GY/Exile/Hand/Lib targets
        elif self.target_type == "spell": cats = ["spells"] # Stack targets
        else: cats.append(self.target_type+"s") # Basic plural

        ids_found = []
        if targets:
            for cat in cats:
                 ids_found.extend(targets.get(cat, []))

        for target_id in set(ids_found):
            target_owner, target_zone = game_state.find_card_location(target_id)
            # Validate source zone specified in constructor matches current zone
            if self.zone == 'any' or target_zone == self.zone:
                 if target_owner: # Ensure target found
                     targets_to_exile.append((target_id, target_owner, target_zone))
            elif target_zone: # Found, but wrong zone
                 logging.debug(f"Exile target {target_id} found in {target_zone}, expected {self.zone}. Skipping.")

        if not targets_to_exile: return False

        exiled_count = 0
        for card_id, owner, current_zone in targets_to_exile:
             # Use move_card to handle replacements (e.g., "If would be exiled, put in GY instead")
             # Also handles triggers for leaving zone/entering exile
             if game_state.move_card(card_id, owner, current_zone, owner, "exile", cause="exile_effect", context={"source_id": source_id}):
                  exiled_count += 1
                  # Logging handled by move_card

        return exiled_count > 0


class ConditionalExileEffect(AbilityEffect):
    """'Exile target creature if it has mana value N or less' (CR 608.2b:
    the condition is checked at resolution, not when targeting), optionally
    overridden by Corrupted: exile regardless of mana value when the
    creature's controller has the poison-counter threshold or more."""
    def __init__(self, max_mana_value, corrupted_poison_threshold=None, condition=None):
        super().__init__(
            f"Exile target creature if it has mana value {max_mana_value} or less",
            condition)
        self.max_mana_value = max_mana_value
        self.corrupted_poison_threshold = corrupted_poison_threshold
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        if isinstance(targets, dict):
            for ids in targets.values():
                target_ids.extend(ids if isinstance(ids, list) else [ids])
        target_ids = [t for t in target_ids if t not in ("p1", "p2")]
        if not target_ids:
            return False
        target_id = target_ids[0]
        target_owner, target_zone = game_state.find_card_location(target_id)
        if not target_owner or target_zone != "battlefield":
            return False
        card = game_state._safe_get_card(target_id)
        corrupted = (self.corrupted_poison_threshold is not None
                     and target_owner.get("poison_counters", 0)
                     >= self.corrupted_poison_threshold)
        if not corrupted and getattr(card, 'cmc', 0) > self.max_mana_value:
            logging.debug(
                f"ConditionalExileEffect: target {target_id} has mana value "
                f"{getattr(card, 'cmc', 0)} > {self.max_mana_value} and no "
                f"corrupted override; the spell resolves doing nothing.")
            return True
        return game_state.move_card(
            target_id, target_owner, "battlefield", target_owner, "exile",
            cause="exile_effect", context={"source_id": source_id})


class ReflectDamageEffect(AbilityEffect):
    """'...it deals that much damage to any other target', reading the amount
    from the triggering DAMAGED event. With the rider, a player dealt damage
    this way can't gain life for the rest of the game (Screaming Nemesis)."""
    def __init__(self, no_life_gain_rider=False, condition=None):
        super().__init__("It deals that much damage to any other target", condition)
        self.no_life_gain_rider = no_life_gain_rider
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        amount = 0
        resolution_context = getattr(self, 'resolution_context', None) or {}
        amount = resolution_context.get('amount', 0)
        if not isinstance(amount, int) or amount <= 0:
            logging.debug("ReflectDamageEffect: no damage amount to reflect.")
            return True
        target_ids = []
        if isinstance(targets, dict):
            for ids in targets.values():
                target_ids.extend(ids if isinstance(ids, list) else [ids])
        target_ids = [t for t in target_ids if t != source_id]
        if not target_ids:
            return False
        target_id = target_ids[0]
        if target_id in ("p1", "p2"):
            player = game_state.p1 if target_id == "p1" else game_state.p2
            dealt = game_state.damage_player(player, amount, source_id)
            if dealt > 0 and self.no_life_gain_rider:
                # CR 614-style continuous restriction with no duration: it
                # survives every turn-boundary reset for the rest of the game.
                player["cant_gain_life"] = True
                logging.debug(
                    f"ReflectDamageEffect: {player.get('name', '?')} can't "
                    f"gain life for the rest of the game.")
            return True
        card = game_state._safe_get_card(target_id)
        if card and 'planeswalker' in getattr(card, 'card_types', []):
            game_state.damage_planeswalker(target_id, amount, source_id)
            return True
        game_state.apply_damage_to_permanent(target_id, amount, source_id)
        game_state.check_state_based_actions()
        return True


class AdditionalCombatPhaseEffect(AbilityEffect):
    """'After this phase, there is an additional combat phase.' (CR 505.5a)
    Registers one extra combat phase; _advance_phase consumes it instead of
    entering the postcombat main phase."""
    def __init__(self, condition=None, followed_by_main=False):
        self.followed_by_main = bool(followed_by_main)
        text = "After this phase, there is an additional combat phase"
        if self.followed_by_main:
            text += " followed by an additional main phase"
        super().__init__(text, condition)

    def _apply_effect(self, game_state, source_id, controller, targets):
        if self.followed_by_main:
            anchor = game_state.phase
            turn_phases = {
                game_state.PHASE_UNTAP, game_state.PHASE_UPKEEP,
                game_state.PHASE_DRAW, game_state.PHASE_MAIN_PRECOMBAT,
                game_state.PHASE_BEGIN_COMBAT,
                game_state.PHASE_DECLARE_ATTACKERS,
                game_state.PHASE_DECLARE_BLOCKERS,
                game_state.PHASE_FIRST_STRIKE_DAMAGE,
                game_state.PHASE_COMBAT_DAMAGE,
                game_state.PHASE_END_OF_COMBAT,
                game_state.PHASE_MAIN_POSTCOMBAT,
                game_state.PHASE_END_STEP, game_state.PHASE_CLEANUP,
            }
            if anchor not in turn_phases:
                anchor = (game_state.previous_priority_phase
                          if game_state.previous_priority_phase in turn_phases
                          else game_state._last_turn_phase)
            if (game_state.additional_phase_anchor not in (None, anchor)
                    or game_state.additional_phase_stage is not None):
                logging.warning(
                    "Cannot schedule a combat+main pair across a different "
                    "already-running inserted phase sequence.")
                return False
            game_state.additional_phase_anchor = anchor
            game_state.additional_phase_pairs_pending += 1
            return True
        game_state.extra_combat_phases = getattr(game_state, 'extra_combat_phases', 0) + 1
        logging.debug(
            f"AdditionalCombatPhaseEffect: {game_state.extra_combat_phases} "
            f"additional combat phase(s) pending this turn.")
        return True


class SacrificeThatManyEffect(AbilityEffect):
    """'That source's controller sacrifices that many permanents of their
    choice' (Phyrexian Obliterator). The damage amount and source come from
    the triggering DAMAGED event; the sacrificing player picks each permanent
    through a forced_sacrifice choice."""
    def __init__(self, condition=None):
        super().__init__(
            "That source's controller sacrifices that many permanents of their choice",
            condition)

    def _apply_effect(self, game_state, source_id, controller, targets):
        resolution_context = getattr(self, 'resolution_context', None) or {}
        amount = resolution_context.get('amount', 0)
        if not isinstance(amount, int) or amount <= 0:
            return True
        damage_source = resolution_context.get('source_id')
        payer = None
        if damage_source is not None and damage_source != source_id:
            payer = game_state.get_card_controller(damage_source)
        if payer is None:
            # v1: if the source left play before resolution, fall back to the
            # opponent of the trigger's controller (the common combat case).
            payer = game_state.p2 if controller == game_state.p1 else game_state.p1
        count = min(amount, len(payer.get("battlefield", [])))
        if count <= 0:
            return True
        game_state.begin_forced_sacrifice(payer, count, source_id)
        return True


class MassExileIncubateEffect(AbilityEffect):
    """'Exile all creatures. Incubate X, where X is the number of creatures
    exiled this way.' (Sunfall). One atomic effect: the incubated counter
    count depends on the exile result. The Incubator token is a transforming
    DFC whose back face is the 0/0 Phyrexian artifact creature."""
    def __init__(self, condition=None):
        super().__init__("Exile all creatures and incubate that many", condition)

    def _apply_effect(self, game_state, source_id, controller, targets):
        exiled = 0
        for player in (game_state.p1, game_state.p2):
            if not player:
                continue
            for cid in list(player.get("battlefield", [])):
                if not game_state._is_creature(cid):
                    continue
                if game_state.move_card(cid, player, "battlefield", player, "exile",
                                        cause="exile_effect", context={"source_id": source_id}):
                    exiled += 1
        token_data = {
            "name": "Incubator",
            "type_line": "Token Artifact — Incubator",
            "card_types": ["artifact"],
            "subtypes": ["incubator"],
            "supertypes": ["token"],
            "oracle_text": "{2}: Transform this token.",
            "power": 0, "toughness": 0,
            "keywords": [0] * len(Card.ALL_KEYWORDS),
            "colors": [0, 0, 0, 0, 0],
            "is_token": True,
            "layout": "transform",
            "faces": [
                {
                    "name": "Incubator", "mana_cost": "",
                    "type_line": "Token Artifact — Incubator",
                    "oracle_text": "{2}: Transform this token.",
                },
                {
                    "name": "Phyrexian Token", "mana_cost": "",
                    "type_line": "Token Artifact Creature — Phyrexian",
                    "power": "0", "toughness": "0",
                    "oracle_text": "",
                },
            ],
        }
        token_id = game_state.create_token(controller, token_data)
        if token_id and exiled > 0:
            game_state.add_counter(token_id, "+1/+1", exiled)
        return token_id is not None


class CreateTreasureEffect(AbilityEffect):
    """Create the predefined colorless Treasure artifact token."""

    TREASURE_ORACLE_TEXT = "{T}, Sacrifice this token: Add one mana of any color."

    def __init__(self, count=1, condition=None):
        self.count = max(1, int(count))
        super().__init__(f"Create {self.count} Treasure token(s)", condition)
        self.requires_target = False

    @classmethod
    def create_for(cls, game_state, player, count):
        created = []
        token_data = {
            "name": "Treasure",
            "type_line": "Token Artifact - Treasure",
            "card_types": ["artifact"],
            "subtypes": ["treasure"],
            "supertypes": [],
            "oracle_text": cls.TREASURE_ORACLE_TEXT,
            "power": 0,
            "toughness": 0,
            "keywords": [0] * len(Card.ALL_KEYWORDS),
            "colors": [0, 0, 0, 0, 0],
            "is_token": True,
        }
        for _ in range(max(0, int(count))):
            token_id = game_state.create_token(player, token_data.copy())
            if token_id:
                created.append(token_id)
        return created

    def _apply_effect(self, game_state, source_id, controller, targets):
        return bool(self.create_for(game_state, controller, self.count))


class BezaEffect(AbilityEffect):
    """Beza, the Bounding Spring's ETB: four independent opponent-comparison
    branches sharing one resolution (CR 603.4-adjacent 'if' riders checked at
    resolution, not intervening-if at trigger time)."""
    def __init__(self, condition=None):
        super().__init__("Beza's four conditional catch-up effects", condition)

    @staticmethod
    def _count_type(game_state, player, card_type):
        return sum(
            1 for cid in player.get("battlefield", [])
            if card_type in [t.lower() for t in getattr(
                game_state._safe_get_card(cid), 'card_types', [])])

    def _apply_effect(self, game_state, source_id, controller, targets):
        opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
        if not opponent:
            return True
        if (self._count_type(game_state, opponent, 'land')
                > self._count_type(game_state, controller, 'land')):
            CreateTreasureEffect.create_for(game_state, controller, 1)
        if opponent.get("life", 0) > controller.get("life", 0):
            game_state.gain_life(controller, 4, source_id)
        if (self._count_type(game_state, opponent, 'creature')
                > self._count_type(game_state, controller, 'creature')):
            CreateTokenEffect(1, 1, "Fish", 2, colors=["blue"])._apply_effect(
                game_state, source_id, controller, {})
        if len(opponent.get("hand", [])) > len(controller.get("hand", [])):
            if hasattr(game_state, '_draw_card'):
                game_state._draw_card(controller)
            elif controller.get("library"):
                controller["hand"].append(controller["library"].pop(0))
        return True


class LinkedExileEffect(AbilityEffect):
    """Exile an object until this effect's source leaves the battlefield.

    The duration is represented by a link in the zone system rather than a
    delayed trigger. That distinction is important: the card returns
    immediately, and nothing is exiled if the source has already left when
    this effect resolves.
    """

    def __init__(self, target_type="nonland permanent", from_zone="battlefield",
                 return_zone="battlefield", optional=False,
                 choose_from_target_opponent_hand=False, effect_text=None):
        self.target_type = target_type.lower()
        self.from_zone = from_zone.lower()
        self.return_zone = return_zone.lower()
        self.optional = bool(optional)
        self.choose_from_target_opponent_hand = bool(choose_from_target_opponent_hand)
        text = effect_text or f"Exile target {target_type} until this source leaves the battlefield"
        super().__init__(text)
        self.requires_target = True

    @staticmethod
    def _player_from_target_id(game_state, target_id):
        if target_id == "p1":
            return game_state.p1
        if target_id == "p2":
            return game_state.p2
        return None

    def _apply_effect(self, game_state, source_id, controller, targets):
        _, source_zone = game_state.find_card_location(source_id)
        if source_zone != "battlefield":
            logging.debug(
                f"Linked exile from {source_id} did nothing because its source already left.")
            return True

        if self.choose_from_target_opponent_hand:
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            target_player = self._player_from_target_id(
                game_state, player_ids[0] if player_ids else None)
            if not target_player or target_player is controller:
                return False
            legal_cards = []
            for card_id in target_player.get("hand", []):
                card = game_state._safe_get_card(card_id)
                if card and "land" not in [
                        str(card_type).lower()
                        for card_type in getattr(card, "card_types", [])]:
                    legal_cards.append(card_id)
            if not legal_cards:
                return True
            return game_state.begin_linked_exile_choice(
                source_id, controller, target_player, legal_cards,
                return_zone=self.return_zone, optional=self.optional)

        target_ids = []
        if isinstance(targets, dict):
            for category in (
                    "creatures", "artifacts", "enchantments", "planeswalkers",
                    "battles", "permanents", "lands", "cards"):
                target_ids.extend(targets.get(category, []))
        for target_id in dict.fromkeys(target_ids):
            target_owner, target_zone = game_state.find_card_location(target_id)
            if not target_owner or target_zone != self.from_zone:
                continue
            card = game_state._safe_get_card(target_id)
            if ("nonland" in self.target_type and card
                    and "land" in [str(card_type).lower()
                                   for card_type in getattr(card, "card_types", [])]):
                continue
            return game_state.exile_until_source_leaves(
                source_id, controller, target_id, target_owner,
                from_zone=self.from_zone, return_zone=self.return_zone)
        return False

class ReflexiveTriggerEffect(AbilityEffect):
    """Create a reflexive triggered ability after an instruction succeeds.

    CR 603.12 reflexive triggers use wording such as "When you do". The
    prerequisite resolves as part of the parent spell or ability; only after
    it succeeds is the reflexive ability queued for the normal trigger stack.
    """
    def __init__(self, prerequisite_text, trigger_effect_text,
                 trigger_condition="when you do"):
        self.prerequisite_text = prerequisite_text.strip()
        self.trigger_effect_text = trigger_effect_text.strip()
        self.trigger_condition = trigger_condition.strip().lower()
        super().__init__(
            f"{self.prerequisite_text}. {self.trigger_condition.capitalize()}, "
            f"{self.trigger_effect_text}.")
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        prerequisite_effects = EffectFactory.create_effects(self.prerequisite_text, targets)
        if not prerequisite_effects:
            logging.warning(
                f"Reflexive trigger prerequisite could not be parsed: {self.prerequisite_text}")
            self._report_support_issue(
                game_state, source_id,
                f"unparsed reflexive prerequisite: {self.prerequisite_text[:80]}",
                "unparsed")
            return True

        completed = True
        for effect in prerequisite_effects:
            if not effect.apply(game_state, source_id, controller, targets):
                completed = False
            choice = getattr(game_state, 'choice_context', None)
            if isinstance(effect, SacrificeEffect):
                if choice and choice.get('type') == 'sacrifice_effect':
                    choice['reflexive_followup'] = {
                        'source_id': source_id,
                        'controller_id': 'p1' if controller is game_state.p1 else 'p2',
                        'trigger_condition': self.trigger_condition,
                        'trigger_effect_text': self.trigger_effect_text,
                        'prerequisite_text': self.prerequisite_text,
                    }
                    return True
                # Sacrificing nothing does not satisfy "when you do" even
                # though the parent instruction itself resolves normally.
                completed = False
            elif (isinstance(effect, RemoveCounterEffect)
                  and choice and choice.get('type') == 'resolution_choice'
                  and choice.get('choice_kind') == 'remove_counter'):
                choice['reflexive_followup'] = {
                    'source_id': source_id,
                    'controller_id': (
                        'p1' if controller is game_state.p1 else 'p2'),
                    'trigger_condition': self.trigger_condition,
                    'trigger_effect_text': self.trigger_effect_text,
                    'prerequisite_text': self.prerequisite_text,
                }
                return True
        if not completed:
            logging.debug(
                f"Reflexive trigger not created; prerequisite did not happen: "
                f"{self.prerequisite_text}")
            return True

        threshold_match = re.search(
            r"^if it has (\w+|\d+) or more ([\w+/-]+) counters? on it",
            self.trigger_effect_text, re.IGNORECASE)
        if threshold_match:
            threshold = text_to_number(threshold_match.group(1))
            if not isinstance(threshold, int):
                threshold = int(threshold_match.group(1))
            source = game_state._safe_get_card(source_id)
            counter_type = threshold_match.group(2).lower()
            if (not source or int(getattr(
                    source, 'counters', {}).get(counter_type, 0) or 0)
                    < threshold):
                return True

        trigger = TriggeredAbility(
            source_id,
            trigger_condition=self.trigger_condition,
            effect=self.trigger_effect_text,
            effect_text=f"{self.trigger_condition.capitalize()}, {self.trigger_effect_text}.")
        trigger._is_reflexive_trigger = True
        trigger_context = {
            "ability": trigger,
            "source_id": source_id,
            "effect_text": self.trigger_effect_text,
            "is_reflexive_trigger": True,
            "reflexive_prerequisite": self.prerequisite_text,
        }

        handler = getattr(game_state, "ability_handler", None)
        if handler is not None:
            handler.active_triggers.append((trigger, controller, trigger_context))
        else:
            game_state.add_to_stack("TRIGGER", source_id, controller, trigger_context)
        logging.debug(
            f"Queued reflexive trigger after '{self.prerequisite_text}': "
            f"{self.trigger_effect_text}")
        return True


class DelayedTriggerEffect(AbilityEffect):
    """One-shot delayed triggered ability created from oracle text (CR 603.7).

    Produced by EffectFactory._extract_delayed_triggers when a sentence reads
    "At the beginning of the next <phase>, <effect>." (leading form) or
    "<effect> at the beginning of the next <phase>." (trailing form).

    Applying this effect performs NOTHING immediately; it registers the inner
    effect with game_state.register_delayed_trigger so it fires exactly once
    at the beginning of the named phase, then expires.

    Binding of "it"-style pronouns (v1): the bound object is the single
    explicit target if exactly one is present, else the source card. This is
    correct for the common producers (unearth-style "Exile it...", blink
    riders on a targeted permanent). A rider whose pronoun refers to an
    object created earlier in the same resolution (e.g. a token made by a
    previous sentence) mis-binds to the source; those fire as a safe no-op
    if the bound object has left the battlefield, and are a documented
    limitation until pronoun tracking lands.
    """

    #: normalized phase phrase -> GameState phase-constant attribute name
    PHASE_ATTR = {
        "end step": "PHASE_END_STEP",
        "upkeep": "PHASE_UPKEEP",
        "end of combat": "PHASE_END_OF_COMBAT",
        "combat": "PHASE_BEGIN_COMBAT",
        "cleanup": "PHASE_CLEANUP",
        "cleanup step": "PHASE_CLEANUP",
        "main phase": "PHASE_MAIN_POSTCOMBAT",
    }

    # Simple riders on a bound object, handled directly instead of re-parsing,
    # because the pronoun ("it") is meaningless to the text parser.
    _RIDER_RE = re.compile(
        r"^(exile|sacrifice|destroy|return)\s+"
        r"(it|this creature|this permanent|that creature|that token)"
        r"(?:\s+to its owner'?s hand)?\s*\.?$",
        re.IGNORECASE)

    def __init__(self, inner_text, phase_key, full_text=None):
        super().__init__(full_text or inner_text)
        self.inner_text = inner_text.strip().rstrip('.')
        self.phase_key = phase_key.strip().lower()
        # Registration itself never needs a pre-resolved target; the inner
        # effect resolves its own targets when it fires.
        self.requires_target = False

    def _apply_effect(self, game_state, source_id, controller, targets):
        gs = game_state
        phase_attr = self.PHASE_ATTR.get(self.phase_key)
        phase_const = getattr(gs, phase_attr, None) if phase_attr else None
        if phase_const is None:
            logging.warning(
                f"DelayedTriggerEffect: unknown phase '{self.phase_key}' in '{self.effect_text}'")
            _card = game_state._safe_get_card(source_id) if source_id is not None else None
            if _card is not None:
                from .card_support import report_unsupported
                report_unsupported(getattr(_card, 'name', None),
                                   f"delayed trigger with unknown phase: {self.phase_key}",
                                   severity="unparsed")
            try:
                gs.fidelity_counters["unparsed_effects"] += 1
            except Exception:
                pass
            return False

        # Bind the object an "it"-style rider refers to (see class docstring).
        bound_id = None
        if targets:
            flat = [tid for tl in targets.values() if isinstance(tl, list) for tid in tl]
            if len(flat) == 1:
                bound_id = flat[0]
        rider_match = self._RIDER_RE.match(self.inner_text.lower())
        binds_created_token = bool(
            rider_match and rider_match.group(2).lower() == "that token")
        if bound_id is None and not binds_created_token:
            bound_id = source_id

        gs.register_delayed_trigger(
            phase=phase_const,
            payload={
                "kind": "oracle_text",
                "inner_text": self.inner_text,
                "source_id": source_id,
                "controller_id": "p1" if controller is gs.p1 else "p2",
                "targets": copy.deepcopy(targets or {}),
                "bound_id": bound_id,
                # Keep this exact list reference through the rest of the
                # resolution; clone() deep-copies it once resolution ends.
                "created_object_ids": (
                    getattr(self, "resolution_context", {})
                    .setdefault("_created_object_ids", [])),
            },
            description=f"text: {self.effect_text[:80]}")
        logging.debug(
            f"Registered text-parsed delayed trigger ({self.phase_key}): {self.inner_text[:60]}")
        return True


