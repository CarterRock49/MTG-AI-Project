
import logging
import re
from collections import defaultdict

import numpy as np
from .card import Card # Need Card for keyword checks etc.
from .ability_utils import is_beneficial_effect # Import helper


def aura_cast_targeting_text(card_or_text):
    """Return the one target announced by an Aura's ``Enchant`` ability.

    Aura spells target even though Oracle's Enchant line does not use the
    word ``target``.  Keeping this conversion in the targeting module gives
    action availability and the live cast transaction one canonical target
    surface, without exposing targets from the Aura's triggered abilities.
    """
    oracle_text = (
        card_or_text if isinstance(card_or_text, str)
        else getattr(card_or_text, "oracle_text", "")) or ""
    enchant_match = re.search(
        r"^\s*enchant\s+([^\n.]+)", oracle_text,
        re.IGNORECASE | re.MULTILINE)
    if not enchant_match:
        return ""
    restriction = enchant_match.group(1).strip()
    return f"target {restriction}" if restriction else ""


class TargetingSystem:
    """
    Enhanced system for handling targeting in Magic: The Gathering.
    Supports comprehensive restrictions, protection effects, and validates targets.
    (Moved from ability_handler.py)
    """

    _PERMANENT_CARD_TYPES = frozenset({
        "artifact", "battle", "creature", "enchantment", "land",
        "planeswalker",
    })

    def __init__(self, game_state):
        self.game_state = game_state
        # Add reference to ability_handler if needed for centralized keyword checks
        self.ability_handler = getattr(game_state, 'ability_handler', None)

    @staticmethod
    def _hidden_exile_matches_requirement(requirement):
        """Whether an opaque exile object can satisfy a public restriction.

        A face-down exiled object is still a card, so an unrestricted target
        card in exile may select it. Type, color, subtype, name, mana-value,
        and similar identity restrictions fail closed because this engine has
        no explicit permission metadata allowing a player to look at it.
        """
        requirement = requirement or {}
        if str(requirement.get("type", "target")).lower() != "card":
            return False
        public_keys = {
            "type", "zone", "controller_is_caster",
            "controller_is_opponent", "opponent_only",
        }
        return not any(
            key not in public_keys and bool(value)
            for key, value in requirement.items())

    def check_keyword(self, card_id, keyword):
         card = self.game_state._safe_get_card(card_id)
         return self._check_keyword_internal(card, keyword)

    def get_valid_targets(self, card_id, controller, target_type=None, effect_text=None):
        """
        Returns a list of valid targets for a card, based on its text and target type,
        using the unified _is_valid_target checker.

        Args:
            card_id: ID of the card doing the targeting
            controller: Player dictionary of the card's controller
            target_type: Optional specific target type to filter for

        Returns:
            dict: Dictionary of target types to lists of valid targets
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if effect_text is None and (not card or not hasattr(card, 'oracle_text')):
            return {}

        oracle_text = (effect_text or getattr(card, "oracle_text", "")).lower()
        opponent = gs.p2 if controller is gs.p1 else gs.p1

        valid_targets = {
            "creature": [], "player": [], "permanent": [], "spell": [],
            "land": [], "artifact": [], "enchantment": [], "planeswalker": [],
            "battle": [],
            "artifact_or_enchantment": [],
            "creature_or_vehicle": [], "creature_or_spell": [],
            "spell_or_permanent": [],
            "card": [], # For graveyard/exile etc.
            "ability": [], # For targeting abilities on stack
            "other": [] # Fallback
        }
        all_target_types = list(valid_targets.keys())

        # Parse targeting requirements from the oracle text
        target_requirements = self._parse_targeting_requirements(oracle_text)

        # An explicit target_type is sufficient for ability contexts whose
        # effect text is stored on the stack rather than printed on the source.
        if not target_requirements and target_type:
            target_requirements.append({"type": target_type})
        elif not target_requirements and "target" in oracle_text:
            target_requirements.append({"type": "target"}) # Generic target

        # Filter requirements if a specific target type is requested
        if target_type and target_type not in {"target", "any"}:
            normalized_target_type = str(target_type).lower()
            target_requirements = [
                req for req in target_requirements
                if req.get("type") == target_type
                or req.get("type") in ["any", "target"]
                or self._card_restriction_accepts_requested_type(
                    req.get("card_type_restriction"),
                    normalized_target_type)
                or str(req.get("subtype_restriction", "")).lower()
                == normalized_target_type
            ]
            if not target_requirements: return {} # No matching requirement for requested type

        # Define potential target sources
        target_sources = [
            # Players
            ("p1", gs.p1, "player"),
            ("p2", gs.p2, "player"),
            # Battlefield
            *[(perm_id, player, "battlefield") for player in [gs.p1, gs.p2] for perm_id in player.get("battlefield", [])],
            # Stack (Spells and Abilities)
            *[(item[1], item[2], "stack") for item in gs.stack if isinstance(item, tuple) and len(item) >= 3],
            # Graveyards
            *[(card_id, player, "graveyard") for player in [gs.p1, gs.p2] for card_id in player.get("graveyard", [])],
            # Exile
            *[(card_id, player, "exile") for player in [gs.p1, gs.p2] for card_id in player.get("exile", [])],
        ]

        processed_valid = defaultdict(set) # Use set to avoid duplicates

        # Check each requirement against potential targets
        for requirement in target_requirements:
            req_type = requirement.get("type", "target") # Use "target" as fallback
            required_zone = requirement.get("zone")

            for target_id, target_obj_or_owner, current_zone in target_sources:
                # Skip if zone doesn't match (unless zone isn't specified in req)
                if required_zone and current_zone != required_zone:
                    continue
                if (current_zone == "exile"
                        and hasattr(gs, "is_face_down_exile_card")
                        and gs.is_face_down_exile_card(
                            target_id, target_obj_or_owner)
                        and not self._hidden_exile_matches_requirement(
                            requirement)):
                    continue

                target_object = None
                target_owner = None

                if current_zone == "player":
                    target_object = target_obj_or_owner # target_obj_or_owner is the player dict
                    target_owner = target_obj_or_owner # Player owns themselves? Or maybe None? Let's use the player.
                elif current_zone == "stack":
                    target_object = None
                    # Find the actual stack item (tuple or Card) based on target_id
                    for item in gs.stack:
                        if isinstance(item, tuple) and len(item) >= 3 and item[1] == target_id:
                            target_object = item # The stack item tuple itself
                            target_owner = item[2] # Controller of the spell/ability
                            break
                        # Less common: stack item is just Card object?
                        # elif isinstance(item, Card) and item.card_id == target_id:
                        #      target_object = item; target_owner = ??? # Need controller info
                    if target_object is None: continue # Stack item not found correctly
                elif current_zone in ["battlefield", "graveyard", "exile", "library"]:
                    target_object = gs._safe_get_card(target_id)
                    target_owner = target_obj_or_owner # target_obj_or_owner is the player dict
                else:
                    continue # Unknown zone

                if not target_object: continue # Could be player or Card or Stack Tuple

                target_info = (target_object, target_owner, current_zone) # Pass tuple to checker

                # Use the unified validation function
                if self._is_valid_target(card_id, target_id, controller, target_info, requirement):
                    # Determine primary category for this target
                    primary_cat = "other"
                    actual_types = set()
                    if isinstance(target_object, Card):
                        actual_types.update(getattr(target_object, 'card_types', []))
                        if current_zone in {'graveyard', 'exile', 'library'}:
                            primary_cat = 'card'
                        elif 'creature' in actual_types: primary_cat = 'creature'
                        elif 'land' in actual_types: primary_cat = 'land'
                        elif 'planeswalker' in actual_types: primary_cat = 'planeswalker'
                        elif 'battle' in actual_types: primary_cat = 'battle'
                        elif 'artifact' in actual_types: primary_cat = 'artifact'
                        elif 'enchantment' in actual_types: primary_cat = 'enchantment'
                        elif current_zone == 'stack': primary_cat = 'spell'
                    elif current_zone == 'player': primary_cat = 'player'
                    elif current_zone == 'stack' and isinstance(target_object, tuple):
                        primary_cat = (
                            'spell' if target_object[0] == 'SPELL'
                            else 'ability')

                    # If specific type requested, use that, otherwise use derived primary category
                    cat_to_add = target_type if target_type else primary_cat

                    # Ensure category exists and add target
                    if cat_to_add in valid_targets:
                        processed_valid[cat_to_add].add(target_id)
                    elif req_type in valid_targets: # Fallback to requirement type
                        processed_valid[req_type].add(target_id)
                    else: # Last resort: "other"
                        processed_valid["other"].add(target_id)

        # Convert sets back to lists for the final dictionary
        final_valid_targets = {cat: list(ids) for cat, ids in processed_valid.items() if ids}

        # "any OTHER target" / "another target": the source object is never a
        # legal choice for its own effect (Screaming Nemesis's reflected
        # damage, Restless Ridgeline's pump).
        if ("any other target" in oracle_text
                or "another target" in oracle_text
                or "other target" in oracle_text):
            final_valid_targets = {
                cat: [t for t in ids if t != card_id]
                for cat, ids in final_valid_targets.items()}
            final_valid_targets = {cat: ids for cat, ids in final_valid_targets.items() if ids}

        return final_valid_targets

    def resolve_targeting_for_ability(self, card_id, ability_text, controller):
        """
        Handle targeting for an ability using the unified targeting system.

        Args:
            card_id: ID of the card with the ability
            ability_text: Text of the ability requiring targets
            controller: Player controlling the ability

        Returns:
            dict: Selected targets or None if targeting failed
        """
        return self.resolve_targeting(card_id, controller, ability_text)

    def resolve_targeting_for_spell(self, spell_id, controller,
                                    effect_text=None):
        """
        Handle targeting for a spell using the unified targeting system.

        Args:
            spell_id: ID of the spell requiring targets
            controller: Player casting the spell

        Returns:
            dict: Selected targets or None if targeting failed
        """
        return self.resolve_targeting(spell_id, controller, effect_text)

    def _live_characteristic(self, card, characteristic, default=None,
                             *, use_layers=True):
        """Read a battlefield object's current layered characteristic."""
        if card is None:
            return default
        card_id = getattr(card, "card_id", None)
        layer_system = getattr(self.game_state, "layer_system", None)
        if use_layers and card_id is not None and layer_system:
            value = layer_system.get_characteristic(card_id, characteristic)
            if value is not None:
                return value
        return getattr(card, characteristic, default)

    @staticmethod
    def _normalized_values(values):
        return {str(value).lower() for value in (values or [])}

    @staticmethod
    def _card_type_restriction_options(restriction):
        if not restriction:
            return set()
        if isinstance(restriction, (list, tuple, set, frozenset)):
            return {str(value).strip().lower() for value in restriction}
        return {
            value.strip().lower()
            for value in re.split(r"\s+or\s+", str(restriction))
            if value.strip()
        }

    @classmethod
    def _card_restriction_accepts_requested_type(
            cls, restriction, requested_type):
        options = cls._card_type_restriction_options(restriction)
        if requested_type in options:
            return True
        if requested_type != "permanent" or not options:
            return False
        return all(
            option in cls._PERMANENT_CARD_TYPES
            or option in {"permanent", "nonland permanent"}
            for option in options)

    @staticmethod
    def _matches_numeric_restriction(value, restriction):
        """Compare a numeric characteristic, failing closed on symbols/None."""
        try:
            numeric_value = float(value)
            threshold = float(restriction["value"])
        except (KeyError, TypeError, ValueError):
            return False
        comparison = restriction.get("comparison", "exactly")
        if comparison == "greater":
            return numeric_value >= threshold
        if comparison == "less":
            return numeric_value <= threshold
        return numeric_value == threshold

    def _is_valid_target(self, source_id, target_id, caster, target_info, requirement):
        """Unified check for any target type."""
        gs = self.game_state
        target_type = requirement.get("type")
        target_obj, target_owner, target_zone = target_info # Expect target_info=(obj, owner, zone)

        if not target_obj: return False
        if (target_zone == "battlefield"
                and target_id in getattr(gs, "phased_out", set())):
            return False

        # 1. Zone Check. Magic distinguishes a permanent from a spell with
        # permanent-card types: an object with ``creature`` in its types is a
        # *creature spell* on the stack and a *creature* only on the
        # battlefield.  Treating stack as a default zone for every target type
        # exposed creature spells to removal such as Anoint with Affliction;
        # the selection mask then disagreed with cast-time validation.
        req_zone = requirement.get("zone")
        if req_zone:
            if target_zone != req_zone:
                return False
        else:
            battlefield_types = {
                "creature", "permanent", "land", "artifact",
                "enchantment", "planeswalker", "battle",
                "artifact_or_enchantment",
                "creature_or_vehicle",
                "creature_or_spell",
                "spell_or_permanent",
            }
            if (target_type in battlefield_types
                    and target_type not in {
                        "creature_or_spell", "spell_or_permanent"}
                    and target_zone != "battlefield"):
                return False
            if (target_type == "creature_or_spell"
                    and target_zone not in {"battlefield", "stack"}):
                return False
            if (target_type == "spell_or_permanent"
                    and target_zone not in {"battlefield", "stack"}):
                return False
            if target_type in {"spell", "ability"} and target_zone != "stack":
                return False
            if target_type == "player" and target_zone != "player":
                return False
            if (target_type in {"any", "target"}
                    and target_zone not in {"battlefield", "player"}):
                return False
            if (target_type == "card"
                    and target_zone not in {"graveyard", "exile", "library"}):
                return False


        # 2. Type Check
        actual_types = set()
        characteristic_card = None
        if isinstance(target_obj, dict) and target_id in ["p1", "p2"]: # Player target
            actual_types.add("player")
            # Also check owner relationship for player targets
            if requirement.get("opponent_only") and target_obj is caster: return False
            if requirement.get("controller_is_caster") and target_obj is not caster: return False # Target self only
        elif isinstance(target_obj, Card): # Card object
            characteristic_card = target_obj
            actual_types.update(self._normalized_values(
                self._live_characteristic(
                    target_obj, 'card_types', [],
                    use_layers=target_zone == "battlefield")))
            actual_types.update(self._normalized_values(
                self._live_characteristic(
                    target_obj, 'subtypes', [],
                    use_layers=target_zone == "battlefield")))
        elif isinstance(target_obj, tuple): # Stack item (Spell/Ability/Trigger)
             item_type = target_obj[0]
             if item_type == "SPELL":
                  actual_types.add("spell")
                  spell_card = gs._safe_get_card(target_obj[1])
                  if spell_card:
                       characteristic_card = spell_card
                       actual_types.update(self._normalized_values(
                           getattr(spell_card, 'card_types', [])))
             elif item_type == "ABILITY": actual_types.add("ability")
             elif item_type == "TRIGGER": actual_types.add("ability"); actual_types.add("triggered") # Allow target triggered ability

        # Check against required type
        valid_type = False
        if target_type == "target": valid_type = True # Generic "target" - skip specific type check initially
        elif target_type == "any": # Creature, player, planeswalker, or battle
             valid_type = any(t in actual_types for t in [
                 "creature", "player", "planeswalker", "battle"])
        elif target_type == "card" and isinstance(target_obj, Card): valid_type = True # Targeting a card in specific zone
        elif target_type in actual_types: valid_type = True
        elif target_type == "permanent" and any(t in actual_types for t in ["creature", "artifact", "enchantment", "land", "planeswalker", "battle"]): valid_type = True
        elif target_type == "artifact_or_enchantment":
             valid_type = bool({"artifact", "enchantment"}.intersection(actual_types))
        elif target_type == "creature_or_vehicle":
             valid_type = "creature" in actual_types or "vehicle" in actual_types
        elif target_type == "creature_or_spell":
             valid_type = (
                 (target_zone == "battlefield" and "creature" in actual_types)
                 or (target_zone == "stack" and "spell" in actual_types))
        elif target_type == "spell_or_permanent":
             valid_type = (
                 (target_zone == "stack" and "spell" in actual_types)
                 or (target_zone == "battlefield" and any(
                     card_type in actual_types for card_type in {
                         "creature", "artifact", "enchantment", "land",
                         "planeswalker", "battle"})))
        elif (target_type == "spell" and target_zone == "stack"
                and isinstance(target_obj, tuple)
                and target_obj[0] == "SPELL"):
             valid_type = True

        allowed_types = set(requirement.get("allowed_types", []))
        if allowed_types and not allowed_types.intersection(actual_types):
            return False
        card_type_options = self._card_type_restriction_options(
            requirement.get("card_type_restriction"))
        if card_type_options:
            type_matches = False
            for card_type_option in card_type_options:
                if card_type_option == "permanent":
                    type_matches = bool(
                        self._PERMANENT_CARD_TYPES.intersection(actual_types))
                elif card_type_option == "nonland permanent":
                    type_matches = (
                        "land" not in actual_types
                        and bool(self._PERMANENT_CARD_TYPES.intersection(
                            actual_types)))
                else:
                    type_matches = card_type_option in actual_types
                if type_matches:
                    break
            if not type_matches:
                return False
        if (requirement.get("controller_is_caster")
                and target_owner is not caster):
            return False
        if (requirement.get("controller_is_opponent")
                and target_owner is caster):
            return False
        if not valid_type: return False

        # 3. Protection / Hexproof / Shroud / Ward (Only for permanents, players, spells)
        if target_zone in ["battlefield", "stack", "player"]:
             source_card = gs._safe_get_card(source_id)
             if isinstance(target_obj, dict) and target_id in ["p1","p2"]: # Player
                  # --- ADDED: Player Protection Checks ---
                  # Assumes _check_keyword can delegate to GS for player checks
                  # Check for hexproof (granted by effects like Leyline of Sanctity)
                  if caster is not target_owner and self._check_keyword(target_obj, "hexproof"):
                       logging.debug(f"Targeting failed: Player {target_id} has hexproof from opponent.")
                       return False
                  # Check for shroud (less common on players, but possible)
                  if self._check_keyword(target_obj, "shroud"):
                       logging.debug(f"Targeting failed: Player {target_id} has shroud.")
                       return False
                  # --- END ADDED ---
             elif isinstance(target_obj, Card): # Permanent or Spell
                 target_card_id = getattr(target_obj, 'card_id', None)
                 if target_card_id is None: return False # Need ID to check keywords centrally

                 # Protection
                 if self._has_protection_from(target_obj, source_card, target_owner, caster): return False
                 # Hexproof (if targeted by opponent)
                 live_handler = getattr(gs, "ability_handler", None)
                 ignores_hexproof = bool(
                     live_handler
                     and hasattr(live_handler, "suppresses_target_protection")
                     and live_handler.suppresses_target_protection(
                         caster, target_card_id, "hexproof"))
                 if (caster is not target_owner
                         and self._check_keyword(target_obj, "hexproof")
                         and not ignores_hexproof):
                     return False
                 # Shroud (if targeted by anyone)
                 if self._check_keyword(target_obj, "shroud"): return False
                 # Ward (Check handled separately - involves paying cost)

        # 4. Specific Requirement Checks (applies mostly to battlefield permanents)
        if target_zone == "battlefield" and isinstance(target_obj, Card):
            # Owner/Controller
            if requirement.get("controller_is_caster") and target_owner is not caster: return False
            if requirement.get("controller_is_opponent") and target_owner is caster: return False

            # Exclusions
            if requirement.get("exclude_land") and 'land' in actual_types: return False
            if requirement.get("exclude_creature") and 'creature' in actual_types: return False
            if requirement.get("exclude_artifact") and 'artifact' in actual_types: return False
            if requirement.get("exclude_enchantment") and 'enchantment' in actual_types: return False
            if requirement.get("exclude_token") and getattr(target_obj, 'is_token', False): return False
            if requirement.get("exclude_color") and self._has_color(target_obj, requirement["exclude_color"]): return False
            excluded_subtypes = {
                str(subtype).lower()
                for subtype in requirement.get("exclude_subtypes", [])
            }
            if excluded_subtypes.intersection(
                    str(subtype).lower() for subtype in getattr(target_obj, 'subtypes', [])):
                return False

            # Inclusions
            if requirement.get("must_be_artifact") and 'artifact' not in actual_types: return False
            if requirement.get("must_be_aura") and 'aura' not in actual_types: return False
            supertypes = self._normalized_values(self._live_characteristic(
                target_obj, 'supertypes', []))
            if requirement.get("must_be_basic") and 'basic' not in supertypes: return False
            if requirement.get("must_be_nonbasic") and 'basic' in supertypes: return False
            if requirement.get("must_be_legendary") and 'legendary' not in supertypes: return False

            # State
            if requirement.get("must_be_tapped") and target_id not in target_owner.get("tapped_permanents", set()): return False
            if requirement.get("must_be_untapped") and target_id in target_owner.get("tapped_permanents", set()): return False
            if requirement.get("must_be_attacking") and target_id not in getattr(gs, 'current_attackers', []): return False
            # --- MODIFIED: Blocking State Check ---
            if requirement.get("must_be_blocking"):
                 # Check the GameState's combat assignments
                 is_blocker = False
                 block_assignments = getattr(gs, 'current_block_assignments', {})
                 for attacker, blockers_list in block_assignments.items():
                     if target_id in blockers_list:
                         is_blocker = True
                         break
                 if not is_blocker: return False # Must be blocking but isn't
            # --- END MODIFICATION ---
            if requirement.get("must_be_face_down") and not getattr(target_obj, 'face_down', False): return False
            required_counter = requirement.get("must_have_counter")
            if (required_counter
                    and getattr(target_obj, "counters", {}).get(required_counter, 0) <= 0):
                return False

            # Color Restriction
            colors_req = requirement.get("color_restriction", [])
            if colors_req:
                colors = self._live_characteristic(target_obj, 'colors', [0] * 5)
                color_count = sum(bool(value) for value in (colors or []))
                matches_color = any(
                    (color == "multicolored" and color_count > 1)
                    or (color == "colorless" and color_count == 0)
                    or self._has_color(target_obj, color)
                    for color in colors_req)
                if not matches_color: return False

            # Stat Restrictions
            if "power_restriction" in requirement:
                pr = requirement["power_restriction"]
                power = self._live_characteristic(target_obj, 'power', None)
                if not self._matches_numeric_restriction(power, pr): return False
            if "toughness_restriction" in requirement:
                tr = requirement["toughness_restriction"]
                toughness = self._live_characteristic(target_obj, 'toughness', None)
                if not self._matches_numeric_restriction(toughness, tr): return False

            # Subtype Restriction
            if "subtype_restriction" in requirement:
                if str(requirement["subtype_restriction"]).lower() not in actual_types: return False

        # Mana-value restrictions also apply to spells and cards outside the
        # battlefield (Spell Snare, Unearth, and similar effects).
        if "mana value_restriction" in requirement:
            if characteristic_card is None:
                return False
            cmc = self._live_characteristic(
                characteristic_card, 'cmc', None,
                use_layers=target_zone == "battlefield")
            if not self._matches_numeric_restriction(
                    cmc, requirement["mana value_restriction"]):
                return False

        # 5. Spell/Ability Specific Checks
        if target_zone == "stack":
             source_card = gs._safe_get_card(source_id)
             spell_target = (
                 target_obj if isinstance(target_obj, Card)
                 else gs._safe_get_card(target_obj[1])
                 if (isinstance(target_obj, tuple)
                     and target_obj[0] == "SPELL")
                 else None)
             if spell_target:
                 # A spell that can't be countered remains a legal target. Its
                 # countering instruction simply fails during resolution.
                 # Spell Type
                 st_req = requirement.get("spell_type_restriction")
                 if st_req == "instant" and 'instant' not in actual_types: return False
                 if st_req == "sorcery" and 'sorcery' not in actual_types: return False
                 if st_req == "creature" and 'creature' not in actual_types: return False
                 if st_req == "noncreature" and 'creature' in actual_types: return False
             elif isinstance(target_obj, tuple): # Ability target
                 ab_req = requirement.get("ability_type_restriction")
                 item_type = target_obj[0]
                 if ab_req == "activated" and item_type != "ABILITY": return False
                 if ab_req == "triggered" and item_type != "TRIGGER": return False

        return True # All checks passed
    
    def _check_keyword(self, card, keyword):
        """Internal helper to check keywords, delegating to AbilityHandler/GameState."""
        gs = self.game_state
        card_id = None

        # Handle checking keyword on player object (less common)
        if isinstance(card, dict) and 'name' in card: # Player dict
             player_id = card.get("player_id")
             if player_id and hasattr(gs, 'check_player_keyword') and callable(gs.check_player_keyword):
                  result = gs.check_player_keyword(player_id, keyword)
                  logging.debug(f"Delegated player keyword check to GS for {player_id}/{keyword}: {result}")
                  return result
             granted = card.get("keywords", ()) or card.get(
                 "granted_keywords", ())
             return str(keyword).lower() in {
                 str(value).lower() for value in granted}
        elif isinstance(card, Card):
             card_id = getattr(card, 'card_id', None)
        else:
            logging.warning(f"_check_keyword received invalid object type: {type(card)}")
            return False

        if card_id is None:
             # If card object passed without ID, try to find ID?
             logging.warning(f"_check_keyword: Card object {getattr(card, 'name', 'Unknown')} missing card_id.")
             return False

        # 1. Prefer AbilityHandler (should use GameState.check_keyword or layer system)
        # *** MODIFYING: Check if handler itself exists first ***
        live_handler = getattr(gs, "ability_handler", None)
        if live_handler and hasattr(live_handler, 'check_keyword'):
            return live_handler.check_keyword(card_id, keyword)

        # 2. Fallback to GameState's check_keyword directly
        elif hasattr(gs, 'check_keyword') and callable(gs.check_keyword):
            return gs.check_keyword(card_id, keyword)

        # 3. Ultimate fallback: Direct check on live card object's 'keywords' array (Layer result)
        logging.warning(f"Using basic card keyword fallback check in TargetingSystem for {keyword} on {getattr(card, 'name', 'Unknown')}")
        live_card = gs._safe_get_card(card_id)
        if live_card and hasattr(live_card, 'keywords') and isinstance(live_card.keywords, (list, np.ndarray)):
             try:
                 # Ensure Card.ALL_KEYWORDS is available
                 if not hasattr(Card, 'ALL_KEYWORDS') or not Card.ALL_KEYWORDS:
                     logging.error("Card.ALL_KEYWORDS not available for keyword check.")
                     return False
                 idx = Card.ALL_KEYWORDS.index(keyword.lower())
                 if idx < len(live_card.keywords): return bool(live_card.keywords[idx])
             except ValueError: pass # Keyword not standard
             except IndexError: pass # Index out of bounds

        logging.debug(f"Keyword check failed in TargetingSystem fallback for {keyword} on {getattr(live_card, 'name', 'Unknown')}")
        return False

    def _has_color(self, card, color_name):
        """Check if a card has a specific color."""
        colors = self._live_characteristic(card, 'colors', [0] * 5)
        if not card or not colors or len(colors) != 5: return False
        color_index_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
        if color_name not in color_index_map: return False
        return bool(colors[color_index_map[color_name]])

    def _parse_targeting_requirements(self, oracle_text):
        """Parse targeting requirements from oracle text with comprehensive rules."""
        requirements = []
        oracle_text = oracle_text.lower()

        def apply_mana_value_restriction(requirement, text):
            match = re.search(
                r"\bmana value\s+(\d+)"
                r"(?:\s+(or greater|or less|exactly))?", text)
            if not match:
                return
            value, comparison = match.groups()
            requirement["mana value_restriction"] = {
                "comparison": (comparison or "exactly").replace(
                    "or ", "").strip(),
                "value": int(value),
            }

        # Alternative type wording needs one OR requirement. Treating only the
        # first noun as authoritative makes noncreature Vehicles invisible.
        artifact_creature_pattern = (
            r"target\s+artifact\s+or\s+creature\s+you\s+control")
        if re.search(artifact_creature_pattern, oracle_text):
            requirements.append({
                "type": "permanent",
                "allowed_types": ["artifact", "creature"],
                "controller_is_caster": True,
            })
            oracle_text = re.sub(
                artifact_creature_pattern, "", oracle_text)

        artifact_enchantment_pattern = (
            r"target\s+artifact\s+or\s+enchantment"
            r"(?:\s+(you control|an opponent controls|that player controls))?")
        for union_match in list(re.finditer(
                artifact_enchantment_pattern, oracle_text)):
            requirement = {"type": "artifact_or_enchantment"}
            control_text = union_match.group(1)
            if control_text == "you control":
                requirement["controller_is_caster"] = True
            elif control_text:
                requirement["controller_is_opponent"] = True
            requirements.append(requirement)
        oracle_text = re.sub(
            artifact_enchantment_pattern, "", oracle_text)

        creature_vehicle_pattern = r"target\s+creature\s+or\s+vehicle"
        if re.search(creature_vehicle_pattern, oracle_text):
            requirements.append({"type": "creature_or_vehicle"})
            oracle_text = re.sub(creature_vehicle_pattern, "", oracle_text)

        creature_spell_pattern = r"target\s+creature\s+or\s+spell"
        if re.search(creature_spell_pattern, oracle_text):
            requirements.append({"type": "creature_or_spell"})
            oracle_text = re.sub(creature_spell_pattern, "", oracle_text)

        spell_permanent_pattern = (
            r"target\s+(?:spell\s+or\s+permanent|"
            r"permanent\s+or\s+spell)")
        if re.search(spell_permanent_pattern, oracle_text):
            requirements.append({"type": "spell_or_permanent"})
            oracle_text = re.sub(spell_permanent_pattern, "", oracle_text)

        creature_planeswalker_pattern = r"target\s+creature\s+or\s+planeswalker"
        if re.search(creature_planeswalker_pattern, oracle_text):
            requirements.append({
                "type": "permanent",
                "allowed_types": ["creature", "planeswalker"],
            })
            oracle_text = re.sub(creature_planeswalker_pattern, "", oracle_text)

        # Mutate reminder text uses both an adjective before the type and the
        # ownership wording "you own". The generic target parser otherwise
        # reads "non" as the target type and silently offers no legal targets.
        mutate_target_pattern = r"target\s+non-human\s+creature\s+you\s+own"
        if re.search(mutate_target_pattern, oracle_text):
            requirements.append({
                "type": "creature",
                "controller_is_caster": True,
                "exclude_subtypes": ["human"],
            })
            oracle_text = re.sub(mutate_target_pattern, "", oracle_text)

        # Nurturing-Pixie-style exclusions combine a subtype adjective, a
        # comma, and ``nonland`` before the actual noun.  Parse the whole
        # target phrase before the generic comma-bounded pattern mistakes
        # ``non-Faerie`` for the target type.
        excluded_subtype_nonland = (
            r"target\s+non-([a-z]+)\s*,\s*nonland\s+permanent"
            r"(?:\s+(you control|an opponent controls|you don't control))?")
        for special_match in list(re.finditer(
                excluded_subtype_nonland, oracle_text)):
            requirement = {
                "type": "permanent", "exclude_land": True,
                "exclude_subtypes": [special_match.group(1).lower()],
            }
            controller_text = special_match.group(2)
            if controller_text == "you control":
                requirement["controller_is_caster"] = True
            elif controller_text:
                requirement["controller_is_opponent"] = True
            requirements.append(requirement)
        oracle_text = re.sub(excluded_subtype_nonland, "", oracle_text)

        counter_target_pattern = (
            r"target\s+(creature|permanent)\s+with\s+"
            r"(?:a|an|one or more|\d+)\s+([+\-\w/]+)\s+counters?\s+on\s+it")
        for counter_target in list(re.finditer(counter_target_pattern, oracle_text)):
            requirements.append({
                "type": counter_target.group(1),
                "must_have_counter": counter_target.group(2).lower(),
            })
        oracle_text = re.sub(counter_target_pattern, "", oracle_text)

        # The generic adjective parser historically treated "nonland" as the
        # target's noun. Pull this common permanent shape out first so cards
        # such as Leyline Binding retain both type and controller restrictions.
        mana_value_clause = (
            r"with\s+mana value\s+\d+"
            r"(?:\s+(?:or greater|or less|exactly))?")
        nonland_permanent_pattern = (
            r"target\s+nonland\s+permanent(?!\s+cards?\b)"
            rf"((?:(?:\s+(?:an opponent controls|you don't control|"
            rf"you control))|(?:\s+{mana_value_clause})){{0,2}})")
        for special_match in list(re.finditer(nonland_permanent_pattern, oracle_text)):
            requirement = {"type": "permanent", "exclude_land": True}
            restrictions = special_match.group(1) or ""
            if ("an opponent controls" in restrictions
                    or "you don't control" in restrictions):
                requirement["controller_is_opponent"] = True
            elif "you control" in restrictions:
                requirement["controller_is_caster"] = True
            apply_mana_value_restriction(requirement, restrictions)
            requirements.append(requirement)
        oracle_text = re.sub(nonland_permanent_pattern, "", oracle_text)

        # One chosen target from an explicit union of permanent types.
        union_pattern = (
            r"target\s+creature\s*,\s*enchantment\s*,\s*or\s+planeswalker")
        if re.search(union_pattern, oracle_text):
            requirements.append({
                "type": "permanent",
                "allowed_types": ["creature", "enchantment", "planeswalker"],
            })
            oracle_text = re.sub(union_pattern, "", oracle_text)

        instant_sorcery_spell_pattern = (
            r"target\s+instant\s+or\s+sorcery\s+spell"
            rf"(\s+{mana_value_clause})?")
        for spell_match in list(re.finditer(
                instant_sorcery_spell_pattern, oracle_text)):
            requirement = {
                "type": "spell",
                "allowed_types": ["instant", "sorcery"],
            }
            apply_mana_value_restriction(
                requirement, spell_match.group(1) or "")
            requirements.append(requirement)
        oracle_text = re.sub(
            instant_sorcery_spell_pattern, "", oracle_text)

        # A printed card type before ``card`` restricts characteristics, while
        # the following zone phrase determines where the object can be found.
        # Keep those dimensions separate so Helping Hand-style text cannot be
        # mistaken for a battlefield creature target.
        core_card_type = (
            r"(?:artifact|battle|creature|enchantment|instant|land|"
            r"permanent|planeswalker|sorcery)")
        typed_zone_card_pattern = (
            rf"target\s+(nonland\s+permanent|{core_card_type}"
            rf"(?:\s+or\s+{core_card_type})*)\s+cards?"
            rf"((?:\s+(?:you own|{mana_value_clause}))*)"
            r"\s+(?:in|from)\s+"
            r"(your graveyard|an opponent'?s graveyard|"
            r"defending player's graveyard|that player's graveyard|"
            r"a graveyard|graveyards?|exile)"
            rf"(\s+{mana_value_clause})?")
        for card_match in list(re.finditer(
                typed_zone_card_pattern, oracle_text)):
            card_kind = card_match.group(1)
            location = card_match.group(3)
            requirement = {
                "type": "card",
                "card_type_restriction": card_kind,
                "zone": "exile" if location == "exile" else "graveyard",
            }
            ownership_text = card_match.group(2) or ""
            if location == "your graveyard" or "you own" in ownership_text:
                requirement["controller_is_caster"] = True
            elif (location.startswith("an opponent")
                    or location.startswith("defending player")):
                requirement["controller_is_opponent"] = True
            apply_mana_value_restriction(
                requirement,
                f"{ownership_text} {card_match.group(4) or ''}")
            requirements.append(requirement)
        oracle_text = re.sub(typed_zone_card_pattern, "", oracle_text)

        graveyard_spell_card_pattern = (
            r"target\s+instant\s+or\s+sorcery\s+card\s+"
            r"(?:in|from)\s+your\s+graveyard")
        if re.search(graveyard_spell_card_pattern, oracle_text):
            requirements.append({
                "type": "card", "zone": "graveyard",
                "allowed_types": ["instant", "sorcery"],
                "controller_is_caster": True,
            })
            oracle_text = re.sub(graveyard_spell_card_pattern, "", oracle_text)

        # Oracle may name a subtype without repeating "creature" or
        # "permanent" ("target Mouse you control"). Keep that grammar narrow:
        # the controller phrase identifies it as a battlefield permanent and
        # the captured noun remains an exact subtype restriction.
        subtype_only_pattern = (
            r"target\s+(?!(?:creatures?|players?|opponents?|permanents?|"
            r"spells?|abilities?|lands?|artifacts?|enchantments?|"
            r"planeswalkers?|battles?|cards?|instants?|sorceries?|auras?)\b)"
            r"([a-z][a-z\-]*)\s+"
            r"(you control|an opponent controls|you don't control)")
        for subtype_match in list(re.finditer(
                subtype_only_pattern, oracle_text)):
            requirement = {
                "type": "permanent",
                "subtype_restriction": subtype_match.group(1).lower(),
            }
            if subtype_match.group(2) == "you control":
                requirement["controller_is_caster"] = True
            else:
                requirement["controller_is_opponent"] = True
            requirements.append(requirement)
        oracle_text = re.sub(subtype_only_pattern, "", oracle_text)

        # Anchor the noun to a real target kind. The former lazy generic noun
        # captured the first adjective, so common Oracle such as "target
        # nontoken creature" became the unsupported type ``nontoken``.
        target_nouns = (
            r"creatures?|players?|opponents?|permanents?|spells?|"
            r"abilities?|lands?|artifacts?|enchantments?|planeswalkers?|"
            r"battles?|cards?|instants?|sorceries?|auras?")
        target_pattern = (
            rf"\btarget\s+((?:(?:[a-z][a-z\-]*|or)\s+)*?)"
            rf"({target_nouns})\b\s*"
            r"((?:(?:with|of|that|in|from)\s+[^,\.;\(]+|"
            r"you control|an opponent controls|you don\'t control)*)")

        matches = re.finditer(target_pattern, oracle_text)

        for match in matches:
            raw_noun = match.group(2).strip()
            req = {"type": raw_noun} # Basic type (creature, player, etc.)
            adjectives = match.group(1).strip().split() if match.group(1) else []
            restrictions = match.group(3).strip()

            # ---- Map Type ----
            type_map = {
                "creature": "creature", "player": "player", "opponent": "player", "permanent": "permanent",
                "spell": "spell", "ability": "ability", "land": "land", "artifact": "artifact",
                "enchantment": "enchantment", "planeswalker": "planeswalker", "card": "card", # General card (often in GY/Exile)
                "battle": "battle",
                "instant": "spell", "sorcery": "spell", "aura": "enchantment",
                "creatures": "creature", "players": "player", "opponents": "player", "permanents": "permanent",
                "spells": "spell", "abilities": "ability", "lands": "land", "artifacts": "artifact",
                "enchantments": "enchantment", "planeswalkers": "planeswalker", "cards": "card",
                "battles": "battle",
                "instants": "spell", "sorceries": "spell", "auras": "enchantment",
                # Add more specific types if needed
            }
            req["type"] = type_map.get(req["type"], req["type"]) # Normalize type

            # ---- Process Adjectives & Restrictions ----
            # Owner/Controller
            if "you control" in restrictions: req["controller_is_caster"] = True
            elif "an opponent controls" in restrictions or "you don't control" in restrictions: req["controller_is_opponent"] = True
            if raw_noun in {"opponent", "opponents"}:
                req["opponent_only"] = True

            # State/Status
            if "tapped" in adjectives: req["must_be_tapped"] = True
            if "untapped" in adjectives: req["must_be_untapped"] = True
            if "attacking" in adjectives: req["must_be_attacking"] = True
            if "blocking" in adjectives: req["must_be_blocking"] = True
            if "face-down" in adjectives or "face down" in restrictions: req["must_be_face_down"] = True
            counter_match = re.search(
                r"with\s+(?:a|an|one or more|\d+)?\s*([+\-\w/]+)\s+counters?",
                restrictions)
            if counter_match:
                req["must_have_counter"] = counter_match.group(1).lower()

            # Card Type / Supertype / Subtype Restrictions
            if "nonland" in adjectives: req["exclude_land"] = True
            if "noncreature" in adjectives: req["exclude_creature"] = True
            if "nonartifact" in adjectives: req["exclude_artifact"] = True
            if "nonenchantment" in adjectives: req["exclude_enchantment"] = True
            if "nontoken" in adjectives: req["exclude_token"] = True
            if "nonblack" in adjectives: req["exclude_color"] = 'black'
            # ... add more non-X types

            if "basic" in adjectives and req["type"] == "land": req["must_be_basic"] = True
            if "nonbasic" in adjectives and req["type"] == "land": req["must_be_nonbasic"] = True
            if "legendary" in adjectives: req["must_be_legendary"] = True

            if re.search(r"\bartifact\s+creature\b", match.group(0)):
                req["must_be_artifact_creature"] = True
            elif "artifact" in adjectives and req["type"]=="creature": req["must_be_artifact"] = True # Adj before type
            elif "artifact" in adjectives and req["type"]=="permanent": req["must_be_artifact"] = True

            if "aura" in adjectives and req["type"]=="enchantment": req["must_be_aura"] = True # Check Aura specifically

            # Color Restrictions (from adjectives or restrictions)
            colors = {"white", "blue", "black", "red", "green", "colorless", "multicolored"}
            found_colors = colors.intersection(set(adjectives)) or colors.intersection(set(restrictions.split()))
            if found_colors: req["color_restriction"] = list(found_colors)

            # Power/Toughness/CMC Restrictions (from restrictions)
            pt_cmc_pattern = (
                r"(?:with|of)\s+(power|toughness)\s+(\d+)"
                r"(?:\s+(or greater|or less|exactly))?")
            pt_match = re.search(pt_cmc_pattern, restrictions)
            if pt_match:
                 stat, value, comparison = pt_match.groups()
                 comparison = comparison or "exactly"
                 req[f"{stat}_restriction"] = {"comparison": comparison.replace("or ","").strip(), "value": int(value)}
            apply_mana_value_restriction(req, restrictions)

            # Zone restrictions (usually implied by context, but check)
            if ("in a graveyard" in restrictions
                    or "in your graveyard" in restrictions
                    or "from your graveyard" in restrictions):
                req["zone"] = "graveyard"; req["type"]="card" # Override type
                if "your graveyard" in restrictions:
                    req["controller_is_caster"] = True
            elif "in exile" in restrictions: req["zone"] = "exile"; req["type"]="card"
            elif "on the stack" in restrictions: req["zone"] = "stack" # Type should be spell/ability

            # Spell/Ability type restrictions
            if req["type"] == "spell":
                 if "instant" in adjectives: req["spell_type_restriction"] = "instant"
                 elif "sorcery" in adjectives: req["spell_type_restriction"] = "sorcery"
                 elif "creature" in adjectives: req["spell_type_restriction"] = "creature"
                 elif "noncreature" in adjectives: req["spell_type_restriction"] = "noncreature"
                 # ... add others
            elif req["type"] == "ability":
                if "activated" in adjectives: req["ability_type_restriction"] = "activated"
                elif "triggered" in adjectives: req["ability_type_restriction"] = "triggered"

            # Specific subtype adjectives. Descriptor words above have their
            # own predicates; an otherwise-unclaimed adjective before creature
            # or land is an Oracle subtype ("Goblin creature", "Island land").
            descriptors = {
                "another", "other", "attacking", "blocking", "tapped",
                "untapped", "face-down", "basic", "nonbasic", "legendary",
                "artifact", "nonartifact", "noncreature", "nonland",
                "nonenchantment", "nontoken", "nonblack", "white", "blue",
                "black", "red", "green", "colorless", "multicolored", "or",
            }
            subtype_words = [
                adjective for adjective in adjectives
                if adjective not in descriptors]
            if req["type"] in {"creature", "land"} and subtype_words:
                subtype = subtype_words[-1]
                if subtype == "non-outlaw":
                    req.setdefault("exclude_subtypes", []).extend([
                        "assassin", "mercenary", "pirate", "rogue",
                        "warlock"])
                elif subtype.startswith("non-"):
                    req.setdefault("exclude_subtypes", []).append(
                        subtype[4:])
                else:
                    req["subtype_restriction"] = subtype

            requirements.append(req)

        # Special cases not matching the main pattern
        if "any target" in oracle_text or "any other target" in oracle_text:
             requirements.append({"type": "any"}) # Any target includes creatures, players, planeswalkers

        # Damage dealt to unqualified "targets" ("deals 1 damage to each of
        # one or two targets", "divided as you choose among ... targets") is
        # any-target damage: creatures, players, planeswalkers, battles.
        # Falling through to the generic requirement offered every permanent,
        # so pure artifacts/enchantments were selectable and the damage
        # effect then refused the commit at resolution (Prismari Charm
        # fizzle warnings, July 13-14). Mirrors _get_target_type_from_text.
        if (not requirements
                and re.search(r"deals?\s+\S+\s+damage[^.]{0,60}\btargets\b",
                              oracle_text)):
             requirements.append({"type": "any"})

        if not requirements and "target" in oracle_text:
             # An unparsed target noun must not expose every permanent and
             # player. Keep unsupported grammar fail-closed in both masks and
             # execution until it has an explicit legality implementation.
             requirements.append({"type": "unsupported"})

        # Refine types based on restrictions
        for req in requirements:
            if req.get("must_be_artifact_creature"): req["type"] = "creature"; req["must_be_artifact"]=True
            if req.get("must_be_aura"): req["type"] = "enchantment"
            if req.get("type") == "opponent": req["type"] = "player"; req["opponent_only"] = True
            if req.get("type") == "card": # Refine card targets
                if req.get("zone") == "graveyard": pass # Okay
                elif req.get("zone") == "exile": pass # Okay
                else: req["zone"] = "graveyard" # Default to GY if zone unspecified for 'card'

        return requirements


    def _has_protection_from(self, target_card, source_card, target_owner, source_controller):
        """Robust protection check using centralized keyword checking and AbilityHandler details."""
        if not target_card or not source_card:
            return False
        target_card_id = getattr(target_card, 'card_id', None)
        if target_card_id is None:
            return False

        if not self._check_keyword(target_card, "protection"):
            return False

        protection_details = []
        live_handler = getattr(self.game_state, "ability_handler", None)
        if live_handler and hasattr(live_handler, 'get_protection_details'):
            protection_details = (
                live_handler.get_protection_details(target_card_id) or [])
        if (not protection_details
                and hasattr(target_card, '_granted_protection_details')):
            protection_details = (
                target_card._granted_protection_details or [])
        if not protection_details:
            matches = re.findall(
                r"protection from ([\w\s]+)(?:\s*where|\.|$|,|;)",
                self._active_rules_text(target_card))
            protection_details.extend(match.strip() for match in matches)

        source_colors = self._live_characteristic(
            source_card, 'colors', [0] * 5)
        source_types = self._normalized_values(self._live_characteristic(
            source_card, 'card_types', []))
        source_subtypes = self._normalized_values(self._live_characteristic(
            source_card, 'subtypes', []))
        source_name = getattr(source_card, 'name', '').lower()

        for protection_detail in protection_details:
            detail = str(protection_detail).lower()
            color_index = {
                "white": 0, "blue": 1, "black": 2,
                "red": 3, "green": 4,
            }.get(detail)
            if color_index is not None and source_colors[color_index]:
                return True
            if detail == "everything":
                return True
            if detail == "all colors" and any(source_colors):
                return True
            if detail == "colorless" and not any(source_colors):
                return True
            if detail == "multicolored" and sum(source_colors) > 1:
                return True
            if detail == "monocolored" and sum(source_colors) == 1:
                return True
            protected_type = {
                "creatures": "creature", "artifacts": "artifact",
                "enchantments": "enchantment",
                "planeswalkers": "planeswalker", "instants": "instant",
                "sorceries": "sorcery", "lands": "land",
            }.get(detail)
            if protected_type and protected_type in source_types:
                return True
            if (detail == "permanents" and source_types.intersection({
                    "creature", "artifact", "enchantment", "land",
                    "planeswalker", "battle"})):
                return True
            if (detail in {"opponent", "opponents", "your opponents"}
                    and target_owner is not source_controller):
                return True
            if detail in source_subtypes or detail == source_name:
                return True
        return False


    def resolve_targeting(self, source_id, controller, effect_text=None, target_types=None):
        """
        Unified method to resolve targeting for both spells and abilities.

        Args:
            source_id: ID of the spell or ability source
            controller: Player who controls the source
            effect_text: Text of the effect requiring targets (use card text if None)
            target_types: Specific types of targets to find (Optional)

        Returns:
            dict: Selected targets or None if targeting failed
        """
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        if not source_card:
            logging.warning(f"resolve_targeting: Source {source_id} not found.")
            return None

        # Use effect_text if provided, otherwise try to get it from the card
        text_to_parse = effect_text
        if not text_to_parse and hasattr(source_card, 'oracle_text'):
            text_to_parse = source_card.oracle_text

        if not text_to_parse:
            # Check if it's an ability on the stack without text stored
            ability_obj = None
            for item in gs.stack:
                if isinstance(item, tuple) and item[1] == source_id:
                     context = item[3] if len(item) > 3 else {}
                     if 'ability' in context and hasattr(context['ability'], 'effect_text'):
                         ability_obj = context['ability']
                         text_to_parse = ability_obj.effect_text
                         break
            if not text_to_parse:
                 logging.warning(f"resolve_targeting: No effect text found for {source_id}.")
                 return None

        # Share one bounds calculation between the no-candidate and selection
        # paths. "Up to" targeting is legal with zero candidates.
        min_required, num_required = gs._target_bounds_from_text(text_to_parse)

        # Get valid targets
        # Pass target_types to get_valid_targets if provided
        valid_targets = self.get_valid_targets(
            source_id, controller, target_type=target_types,
            effect_text=text_to_parse)

        # If no valid targets and the effect *requires* a target, targeting fails
        if not valid_targets or not any(valid_targets.values()):
            if min_required > 0:
                logging.debug(f"resolve_targeting: No valid targets found for required target effect: {text_to_parse}")
                return None
            else: # Effect doesn't require targets
                return {} # Return empty dict for no targets

        # Determine target requirements from the text
        target_requirements = self._parse_targeting_requirements(text_to_parse.lower())

        # Share the casting parser so reminder text and later references to
        # "the target" do not invent additional choices during resolution.
        # Select targets (can be AI or rule-based)
        # Simplified: Use strategic selection helper
        selected_targets = self._select_targets_by_strategy(
            source_card, valid_targets, num_required, target_requirements,
            controller, effect_text=text_to_parse,
            minimum_target_count=min_required)

        # Validate number of targets selected (if num_required > 0)
        if min_required > 0:
             selected_count = sum(len(ids) for ids in selected_targets.values()) if selected_targets else 0
             if selected_count < min_required:
                  logging.debug(f"resolve_targeting: Failed to select enough targets ({selected_count}/{min_required}) for: {text_to_parse}")
                  return None # Failed to select required number

        return selected_targets if selected_targets else {}

    def validate_targets(self, card_id, targets, controller, effect_text=None):
        # effect_text: optional specific effect text (e.g., a chosen modal mode);
        # accepted for callers that pass it (resolution-time validation). BUGFIX:
        # previously absent, so every resolution-time validation raised TypeError
        # and the resolving spell was silently lost.
        """
        Validate if the selected targets are legal for the card.

        Args:
            card_id: ID of the card doing the targeting
            targets: Dictionary of target categories to target IDs
            controller: Player dictionary of the card's controller

        Returns:
            bool: Whether the targets are valid
        """
        if not targets: return True # No targets to validate

        valid_targets_now = self.get_valid_targets(
            card_id, controller, effect_text=effect_text)
        all_valid_target_ids = set()
        for ids in valid_targets_now.values():
            all_valid_target_ids.update(ids)

        original_count = 0
        legal_count = 0
        for category, target_list in list(targets.items()):
            if not isinstance(target_list, list): # Ensure it's a list
                 logging.warning(f"Invalid target list format for category '{category}' in validate_targets.")
                 return False
            original_count += len(target_list)

            valid_in_category = set()
            if category in ("chosen", "target", "targets"):
                valid_in_category = set(all_valid_target_ids)
            else:
                for alias in self._target_category_aliases(category):
                    valid_in_category.update(valid_targets_now.get(alias, []))
            legal_targets = []
            for target_id in target_list:
                if target_id not in valid_in_category:
                    # Log details about why the target is invalid
                    logging.debug(f"Target validation failed: '{target_id}' is no longer a valid target for category '{category}' for source {card_id}.")
                    # Optionally, try re-running _is_valid_target for detailed reason
                    # target_info = self._get_target_info(target_id)
                    # requirement = self._get_requirement_for_category(card_id, category)
                    # self._is_valid_target(card_id, target_id, controller, target_info, requirement) # For debug logging inside
                    continue
                legal_targets.append(target_id)
            targets[category] = legal_targets
            legal_count += len(legal_targets)

        # CR 608.2b: an object is countered by the rules only when every one
        # of its targets is illegal. Otherwise it resolves against the targets
        # that remain legal, without rechoosing or redistributing decisions.
        return original_count == 0 or legal_count > 0

    def _target_category_aliases(self, category):
        """Return accepted singular/plural aliases for target category keys."""
        aliases = {category}
        singular_to_plural = {
            "creature": "creatures", "player": "players", "permanent": "permanents",
            "spell": "spells", "ability": "abilities", "land": "lands",
            "artifact": "artifacts", "enchantment": "enchantments",
            "artifact_or_enchantment": "artifact_or_enchantment",
            "planeswalker": "planeswalkers", "card": "cards", "battle": "battles",
        }
        plural_to_singular = {v: k for k, v in singular_to_plural.items()}
        if category in singular_to_plural:
            aliases.add(singular_to_plural[category])
        if category in plural_to_singular:
            aliases.add(plural_to_singular[category])
        return aliases

    def _target_infos_for_id(self, target_id):
        """Yield every public object occurrence represented by a target ID."""
        gs = self.game_state
        if target_id == "p1":
            yield gs.p1, gs.p1, "player"
            return
        if target_id == "p2":
            yield gs.p2, gs.p2, "player"
            return
        for item in gs.stack:
            if (isinstance(item, tuple) and len(item) >= 3
                    and item[1] == target_id):
                yield item, item[2], "stack"
        for zone in ("battlefield", "graveyard", "exile"):
            for owner in (gs.p1, gs.p2):
                if target_id in owner.get(zone, []):
                    card = gs._safe_get_card(target_id)
                    if card:
                        yield card, owner, zone

    def _select_targets_by_strategy(
            self, card, valid_targets, target_count, target_requirements,
            controller, effect_text=None, minimum_target_count=None):
        """Select only candidates that satisfy each individual requirement."""
        gs = self.game_state
        selected_targets = defaultdict(list)
        opponent = gs.p2 if controller is gs.p1 else gs.p1
        minimum_target_count = (
            target_count if minimum_target_count is None
            else int(minimum_target_count))
        requirements = list(target_requirements or [{"type": "target"}])
        potential_targets = sorted({
            target_id
            for ids in valid_targets.values()
            for target_id in ids
        }, key=lambda target_id: (
            isinstance(target_id, str), str(target_id)))
        selected_ids = set()
        targets_remaining = int(target_count)
        is_beneficial = is_beneficial_effect(
            (effect_text or getattr(card, 'oracle_text', '')).lower())
        mandatory_requirement_count = min(
            minimum_target_count, len(requirements))

        for requirement_index, requirement in enumerate(requirements):
            if targets_remaining <= 0:
                break
            valid_info = {}
            for target_id in potential_targets:
                if target_id in selected_ids:
                    continue
                for target_info in self._target_infos_for_id(target_id):
                    if self._is_valid_target(
                            getattr(card, "card_id", None), target_id,
                            controller, target_info, requirement):
                        valid_info[target_id] = target_info
                        break

            if (requirement_index < mandatory_requirement_count
                    and not valid_info):
                return None
            if not valid_info:
                continue

            desired_owner = controller if is_beneficial else opponent

            def priority(target_id):
                target_obj, target_owner, target_zone = valid_info[target_id]
                owner_rank = 0 if target_owner is desired_owner else 1
                if target_zone == "battlefield" and isinstance(target_obj, Card):
                    power = self._live_characteristic(
                        target_obj, 'power', 0) or 0
                    toughness = self._live_characteristic(
                        target_obj, 'toughness', 0) or 0
                    try:
                        board_value = float(power) + float(toughness)
                    except (TypeError, ValueError):
                        board_value = 0.0
                    return owner_rank, -board_value, str(target_id)
                return owner_rank, 0.0, str(target_id)

            candidates = sorted(valid_info, key=priority)
            requirements_left = len(requirements) - requirement_index - 1
            selection_limit = max(1, targets_remaining - requirements_left)
            selected_for_requirement = 0
            for target_id in candidates[:selection_limit]:
                selected_targets[
                    self._determine_target_category(target_id)].append(target_id)
                selected_ids.add(target_id)
                selected_for_requirement += 1
                targets_remaining -= 1
                if targets_remaining <= 0:
                    break
            if (requirement_index < mandatory_requirement_count
                    and selected_for_requirement == 0):
                return None

        if len(selected_ids) < minimum_target_count:
            logging.warning(
                "Could not select mandatory %s targets for %s. Only found %s.",
                minimum_target_count, getattr(card, 'name', 'Unknown'),
                len(selected_ids))
            return None
        return dict(selected_targets)
    
    def _get_card_owner_fallback(self, card_id):
        """Fallback to find card owner based on original deck assignment or DB."""
        gs = self.game_state
        owner_key = getattr(gs, "card_instance_owners", {}).get(card_id)
        if owner_key == "p1": return gs.p1
        if owner_key == "p2": return gs.p2
        if hasattr(gs, 'original_p1_deck') and card_id in gs.original_p1_deck: return gs.p1
        if hasattr(gs, 'original_p2_deck') and card_id in gs.original_p2_deck: return gs.p2
        return gs.p1 # Default
    
    def _determine_target_category(self, target_id):
        """Determines the primary category ('creatures', 'players', etc.) for a given target ID."""
        gs = self.game_state
        if target_id in {"p1", "p2"}:
            return "players"
        owner, zone = gs.find_card_location(target_id)
        if zone == 'player': return 'players'
        if zone == 'stack':
            # Check if spell or ability
            for item in gs.stack:
                 if isinstance(item, tuple) and item[1] == target_id:
                      return 'spells' if item[0] == 'SPELL' else 'abilities'
        if zone in ['graveyard', 'exile', 'library']: return 'cards'
        if zone == 'battlefield':
             card = gs._safe_get_card(target_id)
             if card:
                  if 'creature' in getattr(card, 'card_types',[]): return 'creatures'
                  if 'planeswalker' in getattr(card, 'card_types',[]): return 'planeswalkers'
                  if 'battle' in getattr(card, 'card_types', []): return 'battles'
                  if 'land' in getattr(card, 'card_types',[]): return 'lands'
                  if 'artifact' in getattr(card, 'card_types',[]): return 'artifacts'
                  if 'enchantment' in getattr(card, 'card_types',[]): return 'enchantments'
                  return 'permanents' # Default for battlefield if specific type unclear
        return 'other' # Fallback
    
    def _map_req_type_to_valid_targets_key(self, req_type):
        """Maps parsed requirement types to the standard keys used in the valid_targets dict."""
        type_map = {
            "creature": "creatures", "player": "players", "permanent": "permanents",
            "spell": "spells", "ability": "abilities", "land": "lands",
            "artifact": "artifacts", "enchantment": "enchantments", "planeswalker": "planeswalkers",
            "card": "cards", # Card targets often in GY/Exile
            "target": "permanents", # Generic target defaults to permanent? Or needs context? Use permanent for now.
            "any": "permanents", # 'Any target' can hit creatures, players, PWs. Store under permanent? Better to handle separately if possible.
            "battle": "battles",
        }
        # If type has 's', assume it's already plural
        return type_map.get(req_type, req_type + "s" if not req_type.endswith('s') else req_type)

    def _active_rules_text(self, card):
        """Return current rules text only while the object's abilities exist."""
        if not card:
            return ""
        layer_system = getattr(self.game_state, "layer_system", None)
        card_id = getattr(card, "card_id", None)
        if (layer_system and card_id is not None
                and layer_system.source_has_lost_all_abilities(card_id)):
            return ""
        return str(self._live_characteristic(
            card, 'oracle_text', '') or '').lower()

    def check_can_be_blocked(self, attacker_id, blocker_id):
        """Check one proposed attacker/blocker pair against live restrictions."""
        gs = self.game_state
        attacker = gs._safe_get_card(attacker_id)
        blocker = gs._safe_get_card(blocker_id)
        if not attacker or not blocker:
            return False

        attacker_controller = gs.get_card_controller(attacker_id)
        blocker_controller = gs.get_card_controller(blocker_id)
        if (not attacker_controller or not blocker_controller
                or attacker_controller is blocker_controller):
            return False
        if (attacker_id in getattr(gs, 'phased_out', set())
                or blocker_id in getattr(gs, 'phased_out', set())):
            return False

        attacker_types = self._normalized_values(
            self._live_characteristic(attacker, 'card_types', []))
        blocker_types = self._normalized_values(
            self._live_characteristic(blocker, 'card_types', []))
        if 'creature' not in attacker_types or 'creature' not in blocker_types:
            return False
        if blocker_id in blocker_controller.get("tapped_permanents", set()):
            return False
        if (self._check_keyword(blocker, "cant_block")
                or self._check_keyword(blocker, "decayed")):
            return False

        attacker_text = self._active_rules_text(attacker)
        blocker_text = self._active_rules_text(blocker)
        match_only = re.search(
            r"can block only creatures with ([\w\s]+)", blocker_text)
        if (match_only and not self._check_keyword(
                attacker, match_only.group(1).strip())):
            return False

        # Protection on the attacker prevents a matching creature from
        # blocking it; protection on the blocker does not prevent the block.
        if self._has_protection_from(
                attacker, blocker, attacker_controller, blocker_controller):
            return False
        if (self._check_keyword(attacker, "unblockable")
                and "except by" not in attacker_text):
            return False
        if (self._check_keyword(attacker, "flying")
                and not (self._check_keyword(blocker, "flying")
                         or self._check_keyword(blocker, "reach"))):
            return False
        if (self._check_keyword(attacker, "shadow")
                != self._check_keyword(blocker, "shadow")):
            return False
        landwalk_type = self._get_landwalk_type(attacker)
        if (landwalk_type
                and self._controls_land_type(
                    blocker_controller, landwalk_type)):
            return False

        blocker_is_artifact = 'artifact' in blocker_types
        if (self._check_keyword(attacker, "fear")
                and not (blocker_is_artifact
                         or self._has_color(blocker, 'black'))):
            return False
        if (self._check_keyword(attacker, "intimidate")
                and not (blocker_is_artifact
                         or self._share_color(attacker, blocker))):
            return False
        if (self._check_keyword(attacker, "skulk")
                and (self._live_characteristic(blocker, 'power', 0) or 0)
                > (self._live_characteristic(attacker, 'power', 0) or 0)):
            return False
        if (self._check_keyword(attacker, "horsemanship")
                and not self._check_keyword(blocker, "horsemanship")):
            return False

        match_except = re.search(
            r"can't be blocked except by ([\w\s]+)", attacker_text)
        if not match_except:
            return True
        criteria = match_except.group(1).strip().lower()
        blocker_subtypes = self._normalized_values(
            self._live_characteristic(blocker, 'subtypes', []))
        blocker_meets = (
            ('artifact' in criteria and blocker_is_artifact)
            or ('wall' in criteria and 'wall' in blocker_subtypes)
            or ('flying' in criteria and self._check_keyword(blocker, "flying"))
            or any(
                color in criteria and self._has_color(blocker, color)
                for color in ('white', 'blue', 'black', 'red', 'green')))
        if blocker_meets:
            return True
        power_match = re.search(
            r"power\s+(\d+)(?:\s+or\s+(greater|less))?", criteria)
        if not power_match:
            return False
        power_required = int(power_match.group(1))
        direction = power_match.group(2)
        blocker_power = self._live_characteristic(blocker, 'power', 0) or 0
        if direction == 'greater':
            return blocker_power >= power_required
        if direction == 'less':
            return blocker_power <= power_required
        return blocker_power == power_required

    # Helper for landwalk check
    def _get_landwalk_type(self, card):
        if card:
            text = self._active_rules_text(card)
            for land_type in [
                    "island", "swamp", "mountain", "forest", "plains",
                    "desert"]:
                keyword = f"{land_type}walk"
                if (self._check_keyword(card, keyword)
                        or keyword in text):
                    return land_type
        return None

    # Helper for landwalk check
    def _controls_land_type(self, player, land_type):
        for card_id in player.get("battlefield", []):
            card = self.game_state._safe_get_card(card_id)
            card_types = self._normalized_values(
                self._live_characteristic(card, 'card_types', []))
            subtypes = self._normalized_values(
                self._live_characteristic(card, 'subtypes', []))
            if card and 'land' in card_types and land_type in subtypes:
                 return True
        return False

    # Helper for intimidate check
    def _share_color(self, card1, card2):
        if card1 and card2:
            colors1 = self._live_characteristic(card1, 'colors', [0] * 5)
            colors2 = self._live_characteristic(card2, 'colors', [0] * 5)
            return any(c1 and c2 for c1, c2 in zip(colors1, colors2))
        return False

    def check_must_attack(self, card_id):
        """Check if a creature must attack this turn."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'): return False

        text = self._active_rules_text(card)
        # Rule: "attacks each combat if able" or "must attack if able"
        if ("unless" not in text and (
                "attacks each combat if able" in text
                or "must attack if able" in text)):
            # Check for exclusions (e.g., "attacks each combat if able unless you control...")
            # Complex exclusions require AbilityHandler/Layer system. Basic check here.
            return True
        return False

    def check_must_block(self, card_id):
        """Check if a creature must block this turn."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'): return False

        text = self._active_rules_text(card)
        # Rule: "blocks each combat if able" or "must block if able"
        if ("unless" not in text and (
                "blocks each combat if able" in text
                or "must block if able" in text)):
             # Check for exclusions
             return True
        return False
