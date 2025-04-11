
import logging
import re
from collections import defaultdict
from .card import Card # Need Card for keyword checks etc.
from .ability_utils import is_beneficial_effect # Import helper

class TargetingSystem:
    """
    Enhanced system for handling targeting in Magic: The Gathering.
    Supports comprehensive restrictions, protection effects, and validates targets.
    (Moved from ability_handler.py)
    """

    def __init__(self, game_state):
        self.game_state = game_state
        # Add reference to ability_handler if needed for centralized keyword checks
        self.ability_handler = getattr(game_state, 'ability_handler', None)

    def check_keyword(self, card_id, keyword):
         card = self.game_state._safe_get_card(card_id)
         return self._check_keyword_internal(card, keyword)

    def get_valid_targets(self, card_id, controller, target_type=None):
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
        if not card or not hasattr(card, 'oracle_text'):
            return {}

        oracle_text = card.oracle_text.lower()
        opponent = gs.p2 if controller == gs.p1 else gs.p1

        valid_targets = {
            "creature": [], "player": [], "permanent": [], "spell": [],
            "land": [], "artifact": [], "enchantment": [], "planeswalker": [],
            "card": [], # For graveyard/exile etc.
            "ability": [], # For targeting abilities on stack
            "other": [] # Fallback
        }
        all_target_types = list(valid_targets.keys())

        # Parse targeting requirements from the oracle text
        target_requirements = self._parse_targeting_requirements(oracle_text)

        # If no requirements found but text has "target", add a generic requirement
        if not target_requirements and "target" in oracle_text:
            target_requirements.append({"type": "target"}) # Generic target

        # Filter requirements if a specific target type is requested
        if target_type:
            target_requirements = [req for req in target_requirements if req.get("type") == target_type or req.get("type") in ["any", "target"]]
            if not target_requirements: return {} # No matching requirement for requested type

        # Define potential target sources
        target_sources = [
            # Players
            ("p1", gs.p1, "player"),
            ("p2", gs.p2, "player"),
            # Battlefield
            *[(perm_id, gs.get_card_controller(perm_id), "battlefield") for player in [gs.p1, gs.p2] for perm_id in player.get("battlefield", [])],
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
                        if 'creature' in actual_types: primary_cat = 'creature'
                        elif 'land' in actual_types: primary_cat = 'land'
                        elif 'planeswalker' in actual_types: primary_cat = 'planeswalker'
                        elif 'artifact' in actual_types: primary_cat = 'artifact'
                        elif 'enchantment' in actual_types: primary_cat = 'enchantment'
                        elif current_zone == 'stack': primary_cat = 'spell'
                        elif current_zone == 'graveyard' or current_zone == 'exile': primary_cat = 'card'
                    elif current_zone == 'player': primary_cat = 'player'
                    elif current_zone == 'stack' and isinstance(target_object, tuple): primary_cat = 'ability' # Could be spell too

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

    def resolve_targeting_for_spell(self, spell_id, controller):
        """
        Handle targeting for a spell using the unified targeting system.

        Args:
            spell_id: ID of the spell requiring targets
            controller: Player casting the spell

        Returns:
            dict: Selected targets or None if targeting failed
        """
        return self.resolve_targeting(spell_id, controller)

    def _is_valid_target(self, source_id, target_id, caster, target_info, requirement):
        """Unified check for any target type."""
        gs = self.game_state
        target_type = requirement.get("type")
        target_obj, target_owner, target_zone = target_info # Expect target_info=(obj, owner, zone)

        if not target_obj: return False

        # ... (Zone Check logic remains the same) ...
        # 1. Zone Check
        req_zone = requirement.get("zone")
        if req_zone and target_zone != req_zone: return False
        if not req_zone and target_zone not in ["battlefield", "stack", "player"]: # Default targetable zones
            # Check if the type allows targeting outside default zones
            if target_type == "card" and target_zone not in ["graveyard", "exile", "library"]: return False
            # Other types usually target battlefield/stack/players unless zone specified
            elif target_type != "card": return False


        # ... (Type Check logic remains the same) ...
        # 2. Type Check
        actual_types = set()
        if isinstance(target_obj, dict) and target_id in ["p1", "p2"]: # Player target
            actual_types.add("player")
            # Also check owner relationship for player targets
            if requirement.get("opponent_only") and target_obj == caster: return False
            if requirement.get("controller_is_caster") and target_obj != caster: return False # Target self only
        elif isinstance(target_obj, Card): # Card object
            actual_types.update(getattr(target_obj, 'card_types', []))
            actual_types.update(getattr(target_obj, 'subtypes', []))
        elif isinstance(target_obj, tuple): # Stack item (Ability/Trigger)
             item_type = target_obj[0]
             if item_type == "ABILITY": actual_types.add("ability")
             elif item_type == "TRIGGER": actual_types.add("ability"); actual_types.add("triggered") # Allow target triggered ability

        # Check against required type
        valid_type = False
        if target_type == "target": valid_type = True # Generic "target" - skip specific type check initially
        elif target_type == "any": # Creature, Player, Planeswalker
             valid_type = any(t in actual_types for t in ["creature", "player", "planeswalker"])
        elif target_type == "card" and isinstance(target_obj, Card): valid_type = True # Targeting a card in specific zone
        elif target_type in actual_types: valid_type = True
        elif target_type == "permanent" and any(t in actual_types for t in ["creature", "artifact", "enchantment", "land", "planeswalker"]): valid_type = True
        elif target_type == "spell" and target_zone == "stack" and isinstance(target_obj, Card): valid_type = True # Targeting spell on stack

        if not valid_type: return False

        # 3. Protection / Hexproof / Shroud / Ward (Only for permanents, players, spells)
        if target_zone in ["battlefield", "stack", "player"]:
             source_card = gs._safe_get_card(source_id)
             if isinstance(target_obj, dict) and target_id in ["p1","p2"]: # Player
                  # TODO: Add player protection checks (e.g., Leyline of Sanctity)
                  pass
             elif isinstance(target_obj, Card): # Permanent or Spell
                 # *** Use self._check_keyword for hexproof/shroud ***
                 # Protection
                 if self._has_protection_from(target_obj, source_card, target_owner, caster): return False
                 # Hexproof (if targeted by opponent)
                 if caster != target_owner and self._check_keyword(target_obj, "hexproof"): return False
                 # Shroud (if targeted by anyone)
                 if self._check_keyword(target_obj, "shroud"): return False
                 # Ward (Check handled separately - involves paying cost)


        # ... (Rest of specific requirement checks remain the same) ...
        # 4. Specific Requirement Checks (applies mostly to battlefield permanents)
        if target_zone == "battlefield" and isinstance(target_obj, Card):
            # Owner/Controller
            if requirement.get("controller_is_caster") and target_owner != caster: return False
            if requirement.get("controller_is_opponent") and target_owner == caster: return False

            # Exclusions
            if requirement.get("exclude_land") and 'land' in actual_types: return False
            if requirement.get("exclude_creature") and 'creature' in actual_types: return False
            if requirement.get("exclude_color") and self._has_color(target_obj, requirement["exclude_color"]): return False

            # Inclusions
            if requirement.get("must_be_artifact") and 'artifact' not in actual_types: return False
            if requirement.get("must_be_aura") and 'aura' not in actual_types: return False
            if requirement.get("must_be_basic") and 'basic' not in getattr(target_obj,'type_line',''): return False
            if requirement.get("must_be_nonbasic") and 'basic' in getattr(target_obj,'type_line',''): return False

            # State
            if requirement.get("must_be_tapped") and target_id not in target_owner.get("tapped_permanents", set()): return False
            if requirement.get("must_be_untapped") and target_id in target_owner.get("tapped_permanents", set()): return False
            if requirement.get("must_be_attacking") and target_id not in getattr(gs, 'current_attackers', []): return False
            # Note: Blocking state needs better tracking than just the current assignments dict
            # if requirement.get("must_be_blocking") and not is_blocking(target_id): return False
            if requirement.get("must_be_face_down") and not getattr(target_obj, 'face_down', False): return False

            # Color Restriction
            colors_req = requirement.get("color_restriction", [])
            if colors_req:
                if not any(self._has_color(target_obj, color) for color in colors_req): return False
                if "multicolored" in colors_req and sum(getattr(target_obj,'colors',[0]*5)) <= 1: return False
                if "colorless" in colors_req and sum(getattr(target_obj,'colors',[0]*5)) > 0: return False

            # Stat Restrictions
            if "power_restriction" in requirement:
                pr = requirement["power_restriction"]
                power = getattr(target_obj, 'power', None)
                if power is None: return False
                if pr["comparison"] == "greater" and not power >= pr["value"]: return False
                if pr["comparison"] == "less" and not power <= pr["value"]: return False
                if pr["comparison"] == "exactly" and not power == pr["value"]: return False
            if "toughness_restriction" in requirement:
                tr = requirement["toughness_restriction"]
                toughness = getattr(target_obj, 'toughness', None)
                if toughness is None: return False
                if tr["comparison"] == "greater" and not toughness >= tr["value"]: return False
                if tr["comparison"] == "less" and not toughness <= tr["value"]: return False
                if tr["comparison"] == "exactly" and not toughness == tr["value"]: return False
            if "mana value_restriction" in requirement:
                cmcr = requirement["mana value_restriction"]
                cmc = getattr(target_obj, 'cmc', None)
                if cmc is None: return False
                if cmcr["comparison"] == "greater" and not cmc >= cmcr["value"]: return False
                if cmcr["comparison"] == "less" and not cmc <= cmcr["value"]: return False
                if cmcr["comparison"] == "exactly" and not cmc == cmcr["value"]: return False

            # Subtype Restriction
            if "subtype_restriction" in requirement:
                if requirement["subtype_restriction"] not in actual_types: return False

        # 5. Spell/Ability Specific Checks
        if target_zone == "stack":
             source_card = gs._safe_get_card(source_id)
             if isinstance(target_obj, Card): # Spell target
                 # Can't be countered? (Only if source is a counter)
                 if source_card and "counter target spell" in getattr(source_card, 'oracle_text', '').lower():
                     if "can't be countered" in getattr(target_obj, 'oracle_text', '').lower(): return False
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
        """Internal helper to check keywords, possibly delegating."""
        gs = self.game_state
        card_id = getattr(card, 'card_id', None)
        if not card_id: return False

        # Prefer using AbilityHandler's centralized check if available
        if self.ability_handler and hasattr(self.ability_handler, 'check_keyword'):
            return self.ability_handler.check_keyword(card_id, keyword)

        # Fallback to GameState's check if no AbilityHandler reference
        elif hasattr(gs, 'check_keyword') and callable(gs.check_keyword):
            return gs.check_keyword(card_id, keyword)

        # Ultimate fallback: basic check on the card object (less reliable)
        logging.warning(f"Using basic card keyword fallback check in TargetingSystem for {keyword} on {getattr(card, 'name', 'Unknown')}")
        if hasattr(card, 'has_keyword'): # Use card's own checker if it exists
             return card.has_keyword(keyword)
        elif hasattr(card, 'keywords') and isinstance(card.keywords, list): # Check keyword array directly
             try:
                 if not Card.ALL_KEYWORDS: return False
                 idx = Card.ALL_KEYWORDS.index(keyword.lower())
                 if idx < len(card.keywords): return bool(card.keywords[idx])
             except ValueError: pass
             except IndexError: pass
        elif hasattr(card, 'oracle_text'): # Check oracle text as last resort
             return keyword.lower() in getattr(card, 'oracle_text', '').lower()

        return False

    def _has_color(self, card, color_name):
        """Check if a card has a specific color."""
        if not card or not hasattr(card, 'colors') or len(getattr(card,'colors',[])) != 5: return False
        color_index_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
        if color_name not in color_index_map: return False
        return card.colors[color_index_map[color_name]] == 1

    def _parse_targeting_requirements(self, oracle_text):
        """Parse targeting requirements from oracle text with comprehensive rules."""
        requirements = []
        oracle_text = oracle_text.lower()

        # Pattern to find "target X" phrases, excluding nested clauses
        # Matches "target [adjectives] type [restrictions]"
        target_pattern = r"target\s+((?:(?:[a-z\-]+)\s+)*?)?([a-z]+)\s*((?:(?:with|of|that)\s+[^,\.;\(]+?|you control|an opponent controls|you don\'t control)*)"

        matches = re.finditer(target_pattern, oracle_text)

        for match in matches:
            req = {"type": match.group(2).strip()} # Basic type (creature, player, etc.)
            adjectives = match.group(1).strip().split() if match.group(1) else []
            restrictions = match.group(3).strip()

            # ---- Map Type ----
            type_map = {
                "creature": "creature", "player": "player", "opponent": "player", "permanent": "permanent",
                "spell": "spell", "ability": "ability", "land": "land", "artifact": "artifact",
                "enchantment": "enchantment", "planeswalker": "planeswalker", "card": "card", # General card (often in GY/Exile)
                "instant": "spell", "sorcery": "spell", "aura": "enchantment",
                # Add more specific types if needed
            }
            req["type"] = type_map.get(req["type"], req["type"]) # Normalize type

            # ---- Process Adjectives & Restrictions ----
            # Owner/Controller
            if "you control" in restrictions: req["controller_is_caster"] = True
            elif "an opponent controls" in restrictions or "you don't control" in restrictions: req["controller_is_opponent"] = True
            elif "target opponent" in oracle_text: req["opponent_only"] = True # Different phrasing

            # State/Status
            if "tapped" in adjectives: req["must_be_tapped"] = True
            if "untapped" in adjectives: req["must_be_untapped"] = True
            if "attacking" in adjectives: req["must_be_attacking"] = True
            if "blocking" in adjectives: req["must_be_blocking"] = True
            if "face-down" in adjectives or "face down" in restrictions: req["must_be_face_down"] = True

            # Card Type / Supertype / Subtype Restrictions
            if "nonland" in adjectives: req["exclude_land"] = True
            if "noncreature" in adjectives: req["exclude_creature"] = True
            if "nonblack" in adjectives: req["exclude_color"] = 'black'
            # ... add more non-X types

            if "basic" in adjectives and req["type"] == "land": req["must_be_basic"] = True
            if "nonbasic" in adjectives and req["type"] == "land": req["must_be_nonbasic"] = True

            if "artifact creature" in match.group(0): req["must_be_artifact_creature"] = True
            elif "artifact" in adjectives and req["type"]=="creature": req["must_be_artifact"] = True # Adj before type
            elif "artifact" in adjectives and req["type"]=="permanent": req["must_be_artifact"] = True

            if "aura" in adjectives and req["type"]=="enchantment": req["must_be_aura"] = True # Check Aura specifically

            # Color Restrictions (from adjectives or restrictions)
            colors = {"white", "blue", "black", "red", "green", "colorless", "multicolored"}
            found_colors = colors.intersection(set(adjectives)) or colors.intersection(set(restrictions.split()))
            if found_colors: req["color_restriction"] = list(found_colors)

            # Power/Toughness/CMC Restrictions (from restrictions)
            pt_cmc_pattern = r"(?:with|of)\s+(power|toughness|mana value)\s+(\d+)\s+(or greater|or less|exactly)"
            pt_match = re.search(pt_cmc_pattern, restrictions)
            if pt_match:
                 stat, value, comparison = pt_match.groups()
                 req[f"{stat}_restriction"] = {"comparison": comparison.replace("or ","").strip(), "value": int(value)}

            # Zone restrictions (usually implied by context, but check)
            if "in a graveyard" in restrictions: req["zone"] = "graveyard"; req["type"]="card" # Override type
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

            # Specific subtypes
            # Look for "target Goblin creature", "target Island land" etc.
            subtype_match = re.search(r"target\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:creature|land|artifact|etc)", match.group(0))
            if subtype_match:
                potential_subtype = subtype_match.group(1).strip()
                # TODO: Check if potential_subtype is a known subtype for the target type
                req["subtype_restriction"] = potential_subtype

            requirements.append(req)

        # Special cases not matching the main pattern
        if "any target" in oracle_text:
             requirements.append({"type": "any"}) # Any target includes creatures, players, planeswalkers

        if not requirements and "target" in oracle_text:
             # Fallback if "target" exists but pattern failed
             requirements.append({"type": "target"})

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
        if not target_card or not source_card: return False

        # 1. Check static abilities granting protection directly via layer system/live object state
        #    (check_keyword already does this)
        if self.check_keyword(target_card.card_id, "protection"): # Check if the *general* keyword is active
             # 2. Get specific protection details from AbilityHandler
             #    This method should aggregate protection from static abilities, granted abilities etc.
             protection_details = []
             if self.ability_handler and hasattr(self.ability_handler, 'get_protection_details'):
                 protection_details = self.ability_handler.get_protection_details(target_card.card_id) or [] # Expect list

             if protection_details:
                 source_colors = getattr(source_card, 'colors', [0]*5)
                 source_types = getattr(source_card, 'card_types', [])
                 source_subtypes = getattr(source_card, 'subtypes', [])
                 source_name = getattr(source_card, 'name', '').lower()

                 for protection_detail in protection_details:
                     protection_detail = protection_detail.lower() # Normalize
                     # Basic Color Check
                     if protection_detail == "white" and source_colors[0]: return True
                     if protection_detail == "blue" and source_colors[1]: return True
                     if protection_detail == "black" and source_colors[2]: return True
                     if protection_detail == "red" and source_colors[3]: return True
                     if protection_detail == "green" and source_colors[4]: return True
                     # Broader Categories
                     if protection_detail == "everything": return True
                     if protection_detail == "all colors" and any(source_colors): return True
                     if protection_detail == "colorless" and not any(source_colors): return True
                     if protection_detail == "multicolored" and sum(source_colors) > 1: return True
                     if protection_detail == "monocolored" and sum(source_colors) == 1: return True
                     # Card Types
                     if protection_detail == "creatures" and "creature" in source_types: return True
                     if protection_detail == "artifacts" and "artifact" in source_types: return True
                     if protection_detail == "enchantments" and "enchantment" in source_types: return True
                     if protection_detail == "planeswalkers" and "planeswalker" in source_types: return True
                     if protection_detail == "instants" and "instant" in source_types: return True
                     if protection_detail == "sorceries" and "sorcery" in source_types: return True
                     if protection_detail == "lands" and "land" in source_types: return True
                     if protection_detail == "permanents" and any(t in source_types for t in ["creature", "artifact", "enchantment", "land", "planeswalker"]): return True # Basic permanent check
                     # Player Relationship
                     if protection_detail == "opponent" and target_owner != source_controller: return True
                     # Subtypes (check against list)
                     if protection_detail in [sub.lower() for sub in source_subtypes]: return True
                     # Specific Name
                     if protection_detail == source_name: return True
                     # Add more protection types (cmc, etc.) if needed

                 # If general protection is active but no specific match found (unusual)
                 # Default to false, specific protection is needed.

        return False # No relevant protection found

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

        # Get valid targets
        # Pass target_types to get_valid_targets if provided
        valid_targets = self.get_valid_targets(source_id, controller, target_type=target_types)

        # If no valid targets and the effect *requires* a target, targeting fails
        if not valid_targets or not any(valid_targets.values()):
            if "target" in text_to_parse.lower():
                logging.debug(f"resolve_targeting: No valid targets found for required target effect: {text_to_parse}")
                return None
            else: # Effect doesn't require targets
                return {} # Return empty dict for no targets

        # Determine target requirements from the text
        target_requirements = self._parse_targeting_requirements(text_to_parse.lower())

        # Simple check for number of required targets
        num_required = text_to_parse.lower().count("target ")
        if num_required == 0 and "target" in text_to_parse.lower(): # Handle "target N"
             match = re.search(r"target (\w+) ", text_to_parse.lower())
             if match and match.group(1) in ["two", "three", "four"]: num_required = {"two":2, "three":3, "four":4}.get(match.group(1), 1)
             else: num_required = 1 # Assume 1 if pattern is complex

        # Select targets (can be AI or rule-based)
        # Simplified: Use strategic selection helper
        selected_targets = self._select_targets_by_strategy(
            source_card, valid_targets, num_required, target_requirements, controller)

        # Validate number of targets selected (if num_required > 0)
        if num_required > 0:
             selected_count = sum(len(ids) for ids in selected_targets.values()) if selected_targets else 0
             if selected_count < num_required:
                  logging.debug(f"resolve_targeting: Failed to select enough targets ({selected_count}/{num_required}) for: {text_to_parse}")
                  return None # Failed to select required number

        return selected_targets if selected_targets else {}

    def validate_targets(self, card_id, targets, controller):
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

        valid_targets_now = self.get_valid_targets(card_id, controller)

        for category, target_list in targets.items():
            if not isinstance(target_list, list): # Ensure it's a list
                 logging.warning(f"Invalid target list format for category '{category}' in validate_targets.")
                 return False

            valid_in_category = valid_targets_now.get(category, [])
            for target_id in target_list:
                if target_id not in valid_in_category:
                    # Log details about why the target is invalid
                    logging.debug(f"Target validation failed: '{target_id}' is no longer a valid target for category '{category}' for source {card_id}.")
                    # Optionally, try re-running _is_valid_target for detailed reason
                    # target_info = self._get_target_info(target_id)
                    # requirement = self._get_requirement_for_category(card_id, category)
                    # self._is_valid_target(card_id, target_id, controller, target_info, requirement) # For debug logging inside
                    return False

        return True

    def _select_targets_by_strategy(self, card, valid_targets, target_count, target_requirements, controller):
        """
        Select targets strategically based on card type and effect. Enhanced logic.
        """
        gs = self.game_state
        selected_targets = defaultdict(list)
        opponent = gs.p2 if controller == gs.p1 else gs.p1
        total_selected = 0

        # Determine if beneficial (uses utility function)
        effect_text = getattr(card, 'oracle_text', '').lower()
        is_beneficial = is_beneficial_effect(effect_text)

        # Group requirements by category if multiple targets needed
        req_by_cat = defaultdict(list)
        for req in target_requirements:
            req_type = req.get("type", "target")
            req_by_cat[req_type].append(req)

        # Iterate through required target categories
        # Prioritize stricter requirements? Or process in arbitrary order?
        # For now, iterate through the requirements parsed
        for req in target_requirements:
            if total_selected >= target_count: break

            req_type = req.get("type", "target")
            # Map common aliases
            cat_map = {"creature": "creatures", "player": "players", "permanent": "permanents", "spell": "spells", "ability": "abilities", "card": "cards"}
            target_cat = cat_map.get(req_type, req_type) # Use normalized category

            if target_cat not in valid_targets or not valid_targets[target_cat]:
                 logging.debug(f"No valid targets for required type '{target_cat}' for {card.name}")
                 continue # Skip if no valid targets for this requirement

            potential_targets_for_req = valid_targets[target_cat]
            target_to_select = None

            # --- AI Target Selection Logic ---
            if target_cat == "players":
                 player_id = None
                 if is_beneficial: player_id = "p1" if controller == gs.p1 else "p2"
                 else: player_id = "p2" if controller == gs.p1 else "p1"
                 if player_id in potential_targets_for_req:
                      target_to_select = player_id
            elif target_cat == "creatures":
                priority_list = []
                if is_beneficial: # Target own creatures
                     own_creatures = [cid for cid in potential_targets_for_req if gs.get_card_controller(cid) == controller]
                     # Sort by P/T desc
                     priority_list = sorted(own_creatures, key=lambda cid: getattr(gs._safe_get_card(cid),'power',0)+getattr(gs._safe_get_card(cid),'toughness',0), reverse=True)
                else: # Target opponent's creatures
                     opp_creatures = [cid for cid in potential_targets_for_req if gs.get_card_controller(cid) == opponent]
                     # Sort by P/T desc (target biggest threat)
                     priority_list = sorted(opp_creatures, key=lambda cid: getattr(gs._safe_get_card(cid),'power',0)+getattr(gs._safe_get_card(cid),'toughness',0), reverse=True)

                # Select first available target not already selected for *this specific spell*
                for tid in priority_list:
                     if tid not in selected_targets[target_cat]:
                          target_to_select = tid
                          break
            # Add logic for spells, abilities, permanents, cards (GY/Exile)
            elif target_cat in ["spells", "abilities"]: # Counter targets
                 # Target opponent's spells/abilities first
                 priority_list = [tid for tid in potential_targets_for_req if gs.get_stack_item_controller(tid) == opponent]
                 priority_list.extend([tid for tid in potential_targets_for_req if gs.get_stack_item_controller(tid) == controller]) # Self last
                 for tid in priority_list:
                     if tid not in selected_targets.get(target_cat, []): target_to_select = tid; break
            elif target_cat in ["permanents", "artifacts", "enchantments", "lands", "planeswalkers"]:
                 priority_list = []
                 if is_beneficial: # Target own
                     own_perms = [cid for cid in potential_targets_for_req if gs.get_card_controller(cid) == controller]
                     priority_list = sorted(own_perms, key=lambda cid: getattr(gs._safe_get_card(cid),'cmc',0), reverse=True) # Target highest CMC?
                 else: # Target opponent's
                     opp_perms = [cid for cid in potential_targets_for_req if gs.get_card_controller(cid) == opponent]
                     priority_list = sorted(opp_perms, key=lambda cid: getattr(gs._safe_get_card(cid),'cmc',0), reverse=True) # Target highest CMC threat
                 for tid in priority_list:
                     if tid not in selected_targets.get(target_cat, []): target_to_select = tid; break
            elif target_cat == "cards": # Graveyard / Exile
                priority_list = []
                zone = req.get("zone", "graveyard")
                if is_beneficial: # Target own
                     own_cards = [cid for cid in potential_targets_for_req if cid in controller.get(zone, [])]
                     priority_list = sorted(own_cards, key=lambda cid: getattr(gs._safe_get_card(cid),'cmc',0), reverse=True)
                else: # Target opponent's
                     opp_cards = [cid for cid in potential_targets_for_req if cid in opponent.get(zone, [])]
                     priority_list = sorted(opp_cards, key=lambda cid: getattr(gs._safe_get_card(cid),'cmc',0), reverse=True)
                for tid in priority_list:
                     if tid not in selected_targets.get(target_cat, []): target_to_select = tid; break
            else: # Fallback for "target" or unknown categories
                 if potential_targets_for_req:
                      target_to_select = potential_targets_for_req[0] # Just pick first valid

            # --- Add Selected Target ---
            if target_to_select:
                selected_targets[target_cat].append(target_to_select)
                total_selected += 1

        # Check if minimum target count met
        if total_selected < target_count:
            logging.debug(f"Could only select {total_selected}/{target_count} targets for {card.name}.")
            # Return None if required count not met (rules check needed - some spells allow fewer targets)
            # Assume for now that exact count is required if target_count > 0
            if target_count > 0: return None

        return dict(selected_targets) # Convert back to regular dict


    def check_can_be_blocked(self, attacker_id, blocker_id):
        """
        Check if an attacker can be blocked by this blocker considering all restrictions.
        Uses centralized keyword checking.
        """
        gs = self.game_state
        attacker = gs._safe_get_card(attacker_id)
        blocker = gs._safe_get_card(blocker_id)

        if not attacker or not blocker: return False

        # Get controller info using GameState helper
        attacker_controller = gs.get_card_controller(attacker_id)
        blocker_controller = gs.get_card_controller(blocker_id)
        if not attacker_controller or not blocker_controller: return False

        # Check if blocker is tapped
        if blocker_id in blocker_controller.get("tapped_permanents", set()): return False

        # Check for protection (attacker has protection from blocker OR blocker has protection from attacker)
        # Protection prevents blocking
        if self._has_protection_from(attacker, blocker, attacker_controller, blocker_controller): return False
        if self._has_protection_from(blocker, attacker, blocker_controller, attacker_controller): return False

        # --- Check Attacker's Evasion ---
        # Absolute unblockable
        # Note: Need careful text parsing. "Can't be blocked." vs "Can't be blocked except by..."
        if self._check_keyword(attacker, "unblockable"): # Assuming keyword implies absolute
             if "except by" not in getattr(attacker, 'oracle_text', '').lower():
                  return False

        # Flying
        if self._check_keyword(attacker, "flying") and not (self._check_keyword(blocker, "flying") or self._check_keyword(blocker, "reach")): return False

        # Shadow
        if self._check_keyword(attacker, "shadow") and not self._check_keyword(blocker, "shadow"): return False

        # Fear
        if self._check_keyword(attacker, "fear") and not (self._check_keyword(blocker, "artifact") or self._check_keyword(blocker, "black")): return False

        # Intimidate
        if self._check_keyword(attacker, "intimidate") and not (self._check_keyword(blocker, "artifact") or self._share_color(attacker, blocker)): return False

        # Skulk
        if self._check_keyword(attacker, "skulk") and getattr(blocker, 'power', 0) > getattr(attacker, 'power', 0): return False

        # Horsemanship
        if self._check_keyword(attacker, "horsemanship") and not self._check_keyword(blocker, "horsemanship"): return False

        # Landwalk (if defender controls relevant land type)
        landwalk_type = self._get_landwalk_type(attacker)
        if landwalk_type and self._controls_land_type(blocker_controller, landwalk_type): return False

        # Conditional Unblockable ("Can't be blocked except by...")
        match_except = re.search(r"can't be blocked except by ([\w\s]+)", getattr(attacker, 'oracle_text', '').lower())
        if match_except:
            exception_criteria = match_except.group(1).strip().split()
            # Check if blocker meets criteria
            blocker_meets = False
            if 'artifacts' in exception_criteria and self._check_keyword(blocker, "artifact"): blocker_meets = True
            elif 'walls' in exception_criteria and 'wall' in getattr(blocker, 'subtypes', []): blocker_meets = True
            elif 'creatures with flying' in exception_criteria and self._check_keyword(blocker, "flying"): blocker_meets = True
            # Add more common exceptions
            if not blocker_meets: return False # Does not meet exception criteria

        # --- Check Blocker's Restrictions ---
        if self._check_keyword(blocker, "can't block"): return False
        if self._check_keyword(blocker, "decayed"): return False # Decayed can't block

        # Conditional Blocking ("Can block only...")
        match_only = re.search(r"can block only creatures with ([\w\s]+)", getattr(blocker, 'oracle_text', '').lower())
        if match_only:
             required_ability = match_only.group(1).strip()
             if not self._check_keyword(attacker, required_ability): return False

        # Menace handled separately (requires >=2 blockers, this checks if ONE blocker is legal)

        return True # No restriction found preventing this block

    # Helper for landwalk check
    def _get_landwalk_type(self, card):
        if card and hasattr(card, 'oracle_text'):
            for land_type in ["island", "swamp", "mountain", "forest", "plains", "desert"]:
                if f"{land_type}walk" in card.oracle_text.lower(): return land_type
        return None

    # Helper for landwalk check
    def _controls_land_type(self, player, land_type):
        for card_id in player.get("battlefield", []):
            card = self.game_state._safe_get_card(card_id)
            if card and 'land' in getattr(card, 'type_line', '') and land_type in getattr(card, 'subtypes', []):
                 return True
        return False

    # Helper for intimidate check
    def _share_color(self, card1, card2):
        if card1 and card2 and hasattr(card1, 'colors') and hasattr(card2, 'colors'):
            return any(c1 and c2 for c1, c2 in zip(card1.colors, card2.colors))
        return False

    def check_must_attack(self, card_id):
        """Check if a creature must attack this turn."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'): return False

        text = card.oracle_text.lower()
        # Rule: "attacks each combat if able" or "must attack if able"
        if "attacks each combat if able" in text or "must attack if able" in text:
            # Check for exclusions (e.g., "attacks each combat if able unless you control...")
            # Complex exclusions require AbilityHandler/Layer system. Basic check here.
            return True
        return False

    def check_must_block(self, card_id):
        """Check if a creature must block this turn."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'): return False

        text = card.oracle_text.lower()
        # Rule: "blocks each combat if able" or "must block if able"
        if "blocks each combat if able" in text or "must block if able" in text:
             # Check for exclusions
             return True
        return False