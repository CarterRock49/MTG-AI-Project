
import logging
import re
from collections import defaultdict

import numpy as np
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

        # 1. Zone Check
        req_zone = requirement.get("zone")
        if req_zone and target_zone != req_zone: return False
        if not req_zone and target_zone not in ["battlefield", "stack", "player"]: # Default targetable zones
            # Check if the type allows targeting outside default zones
            if target_type == "card" and target_zone not in ["graveyard", "exile", "library"]: return False
            # Other types usually target battlefield/stack/players unless zone specified
            elif target_type != "card": return False


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
                  # --- ADDED: Player Protection Checks ---
                  # Assumes _check_keyword can delegate to GS for player checks
                  # Check for hexproof (granted by effects like Leyline of Sanctity)
                  if caster != target_owner and self._check_keyword(target_obj, "hexproof"):
                       logging.debug(f"Targeting failed: Player {target_id} has hexproof from opponent.")
                       return False
                  # Check for shroud (less common on players, but possible)
                  if self._check_keyword(target_obj, "shroud"):
                       logging.debug(f"Targeting failed: Player {target_id} has shroud.")
                       return False
                  # --- END ADDED ---
             elif isinstance(target_obj, Card): # Permanent or Spell
                 target_card_id = getattr(target_obj, 'card_id', None)
                 if not target_card_id: return False # Need ID to check keywords centrally

                 # Protection
                 if self._has_protection_from(target_obj, source_card, target_owner, caster): return False
                 # Hexproof (if targeted by opponent)
                 if caster != target_owner and self._check_keyword(target_obj, "hexproof"): return False
                 # Shroud (if targeted by anyone)
                 if self._check_keyword(target_obj, "shroud"): return False
                 # Ward (Check handled separately - involves paying cost)

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
                     # --- Use central rule/keyword check for 'cant_be_countered' ---
                     cannot_be_countered = False
                     if hasattr(gs, 'check_rule'):
                          cannot_be_countered = gs.check_rule('cant_be_countered', {'target_card_id': target_id, 'target_card': target_obj})
                     elif self._check_keyword(target_obj, "cant_be_countered"):
                          cannot_be_countered = True
                     elif "can't be countered" in getattr(target_obj, 'oracle_text', '').lower():
                          cannot_be_countered = True

                     if cannot_be_countered:
                          logging.debug(f"Spell {target_obj.name} can't be countered.")
                          return False
                     # --- END MODIFICATION ---
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
             # --- ADDED: Delegate Player Keyword Check to GameState ---
             player_id = card.get("player_id") # Assuming player dict has a unique ID ("p1", "p2")
             if player_id and hasattr(gs, 'check_player_keyword') and callable(gs.check_player_keyword):
                  # Let GameState determine if the player has the keyword based on global effects, etc.
                  result = gs.check_player_keyword(player_id, keyword)
                  logging.debug(f"Delegated player keyword check to GS for {player_id}/{keyword}: {result}")
                  return result
             else:
                 logging.warning(f"Player keyword check for '{keyword}' on {card.get('name')} requires GameState.check_player_keyword method or player_id. Returning False.")
                 # TODO: Implement GameState.check_player_keyword using LayerSystem results for players.
                 return False
             # --- END ADDED ---
        elif isinstance(card, Card):
             card_id = getattr(card, 'card_id', None)
        else:
            logging.warning(f"_check_keyword received invalid object type: {type(card)}")
            return False

        if not card_id:
             # If card object passed without ID, try to find ID?
             logging.warning(f"_check_keyword: Card object {getattr(card, 'name', 'Unknown')} missing card_id.")
             return False

        # 1. Prefer AbilityHandler (should use GameState.check_keyword or layer system)
        # *** MODIFYING: Check if handler itself exists first ***
        if hasattr(self, 'ability_handler') and self.ability_handler and hasattr(self.ability_handler, 'check_keyword'):
            return self.ability_handler.check_keyword(card_id, keyword)

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
        target_card_id = getattr(target_card, 'card_id', None)
        if not target_card_id: return False

        # 1. Use central check_keyword for the general "protection" keyword
        if self.check_keyword(target_card, "protection"): # Check if the *general* keyword is active
             # 2. Get specific protection details (Needs reliable source like AbilityHandler/LayerSystem)
             # Attempt to get details via AbilityHandler first
             protection_details = []
             if self.ability_handler and hasattr(self.ability_handler, 'get_protection_details'):
                 protection_details = self.ability_handler.get_protection_details(target_card_id) or []
             # Fallback: Check card's own properties if handler fails (Less reliable)
             elif hasattr(target_card,'_granted_protection_details'): # Check for cached layer results directly?
                  protection_details = target_card._granted_protection_details or []
             else: # Last resort: parse text again (least reliable)
                  matches = re.findall(r"protection from ([\w\s]+)(?:\s*where|\.|$|,|;)", getattr(target_card, 'oracle_text', '').lower())
                  for match in matches: protection_details.append(match.strip())

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
        Select targets strategically based on card type and effect.
        Ensures mandatory target counts are met if possible.
        """
        gs = self.game_state
        selected_targets = defaultdict(list)
        opponent = gs.p2 if controller == gs.p1 else gs.p1
        total_selected = 0
        num_targets_specified_in_text = getattr(card, 'num_targets', 1) # Get expected count if stored
        target_is_optional = "up to" in getattr(card, 'oracle_text', '').lower() # Check "up to N"

        # Check if target count passed matches card's internal count (if available)
        # If target_count from parser differs from card schema, prioritize card schema?
        # Let's use target_count passed from resolver, but maybe log discrepancy.
        if hasattr(card,'num_targets') and card.num_targets != target_count:
            logging.debug(f"Target count discrepancy for {card.name}: Resolver wants {target_count}, card schema expects {card.num_targets}.")
            # Stick with target_count requested by resolver for now.

        # Helper to select first available valid target from a list not already chosen
        def select_first_available(candidates, current_selections):
            for tid in candidates:
                if tid not in current_selections:
                    return tid
            return None

        # Determine if beneficial (uses utility function)
        effect_text = getattr(card, 'oracle_text', '').lower()
        is_beneficial = is_beneficial_effect(effect_text)

        # Iterate through required target categories (based on parsed requirements)
        # Prioritize more specific requirements first? (e.g., "target artifact creature")
        target_requirements.sort(key=lambda r: len(r), reverse=True) # Simple sort by num restrictions

        potential_targets_flat = []
        for cat, ids in valid_targets.items():
            potential_targets_flat.extend(ids)
        potential_targets_flat = list(set(potential_targets_flat)) # Unique available targets

        all_selected_ids_this_call = set() # Track selections within this call

        # --- AI Target Selection Loop ---
        # First pass: Select based on strategy/heuristics up to target_count
        targets_remaining_to_select = target_count
        for req in target_requirements:
            if targets_remaining_to_select <= 0: break

            req_type = req.get("type", "target")
            target_cat = self._map_req_type_to_valid_targets_key(req_type)

            if target_cat not in valid_targets or not valid_targets[target_cat]:
                continue # No valid targets for this specific requirement type

            potential_targets_for_req = [tid for tid in valid_targets[target_cat] if tid not in all_selected_ids_this_call] # Exclude already selected
            if not potential_targets_for_req: continue

            target_to_select = None

            # --- Enhanced AI Target Selection Logic ---
            priority_list = []
            player_map = {"p1": gs.p1, "p2": gs.p2}
            if target_cat == "players":
                 player_ids = [p_id for p_id in potential_targets_for_req if p_id in player_map] # Filter valid player ids
                 if is_beneficial: # Target self first if possible
                     priority_list = [p_id for p_id in player_ids if player_map[p_id] == controller]
                     priority_list.extend([p_id for p_id in player_ids if player_map[p_id] != controller])
                 else: # Target opponent first if possible
                     priority_list = [p_id for p_id in player_ids if player_map[p_id] == opponent]
                     priority_list.extend([p_id for p_id in player_ids if player_map[p_id] == controller])
            elif target_cat in ["creatures", "permanents", "artifacts", "enchantments", "lands", "planeswalkers"]:
                # Sort candidates based on benefit/threat
                # Benefit: Target own best, Harm: Target opponent best
                potential_target_cards = [(tid, gs._safe_get_card(tid)) for tid in potential_targets_for_req]
                if is_beneficial:
                    potential_target_cards = [(tid,c) for tid,c in potential_target_cards if gs.get_card_controller(tid) == controller]
                    priority_list = sorted(potential_target_cards, key=lambda x: (getattr(x[1],'power',0) or 0) + (getattr(x[1],'toughness',0) or 0), reverse=True) # Target strongest own
                else:
                    potential_target_cards = [(tid,c) for tid,c in potential_target_cards if gs.get_card_controller(tid) == opponent]
                    priority_list = sorted(potential_target_cards, key=lambda x: (getattr(x[1],'power',0) or 0) + (getattr(x[1],'toughness',0) or 0), reverse=True) # Target strongest opponent
                priority_list = [tid for tid, card in priority_list] # Extract IDs
            elif target_cat == "cards": # Graveyard/Exile - harder to evaluate simply
                if is_beneficial: # Target own cards
                     priority_list = [tid for tid in potential_targets_for_req if self._get_card_owner_fallback(tid) == controller]
                     priority_list.extend([tid for tid in potential_targets_for_req if self._get_card_owner_fallback(tid) != controller])
                else: # Target opponent cards
                     priority_list = [tid for tid in potential_targets_for_req if self._get_card_owner_fallback(tid) == opponent]
                     priority_list.extend([tid for tid in potential_targets_for_req if self._get_card_owner_fallback(tid) != opponent])
            else: # Spells, Abilities, Fallback
                 priority_list = potential_targets_for_req[:] # Use original order or random?

            target_to_select = select_first_available(priority_list, all_selected_ids_this_call)
            # --- End Enhanced AI Logic ---

            # --- Add Selected Target ---
            if target_to_select:
                # Map back to the correct category key for the output dictionary
                output_cat_key = self._map_req_type_to_valid_targets_key(req_type) # Use helper
                selected_targets[output_cat_key].append(target_to_select)
                all_selected_ids_this_call.add(target_to_select)
                targets_remaining_to_select -= 1

        # --- Mandatory Target Enforcement ---
        current_selected_count = len(all_selected_ids_this_call)
        # If target is NOT optional AND we selected fewer than required AND more valid targets EXIST
        if not target_is_optional and current_selected_count < target_count and len(potential_targets_flat) >= target_count:
            logging.debug(f"Strategically selected {current_selected_count}/{target_count} targets. Forcing selection of remaining.")
            # Find remaining available targets
            remaining_available = [tid for tid in potential_targets_flat if tid not in all_selected_ids_this_call]
            needed_more = target_count - current_selected_count
            # Select the first N available remaining targets
            for i in range(min(needed_more, len(remaining_available))):
                 forced_target = remaining_available[i]
                 # Determine its category for output dict
                 forced_cat = self._determine_target_category(forced_target)
                 selected_targets[forced_cat].append(forced_target)
                 all_selected_ids_this_call.add(forced_target)
                 logging.debug(f"Forced selection of target: {forced_target}")
        # --- End Mandatory Enforcement ---

        final_selected_count = len(all_selected_ids_this_call)
        # Final validation check (only if targets are mandatory)
        if not target_is_optional and final_selected_count < target_count:
             logging.warning(f"Could not select mandatory {target_count} targets for {card.name}. Only found/selected {final_selected_count}. Returning None.")
             return None

        return dict(selected_targets) # Convert back to regular dict
    
    def _get_card_owner_fallback(self, card_id):
        """Fallback to find card owner based on original deck assignment or DB."""
        gs = self.game_state
        if hasattr(gs, 'original_p1_deck') and card_id in gs.original_p1_deck: return gs.p1
        if hasattr(gs, 'original_p2_deck') and card_id in gs.original_p2_deck: return gs.p2
        return gs.p1 # Default
    
    def _determine_target_category(self, target_id):
        """Determines the primary category ('creatures', 'players', etc.) for a given target ID."""
        gs = self.game_state
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
                  if 'battle' in getattr(card, 'type_line','').lower(): return 'battles'
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

    def check_can_be_blocked(self, attacker_id, blocker_id):
            """
            Check if an attacker can be blocked by this blocker considering all restrictions.
            Uses centralized keyword checking and includes Banding logic.
            """
            gs = self.game_state
            attacker = gs._safe_get_card(attacker_id)
            blocker = gs._safe_get_card(blocker_id)

            if not attacker or not blocker: return False

            # Get controller info using GameState helper
            attacker_controller = gs.get_card_controller(attacker_id)
            blocker_controller = gs.get_card_controller(blocker_id)
            if not attacker_controller or not blocker_controller: return False

            # --- Start Basic Checks ---
            # Check if blocker is tapped
            if blocker_id in blocker_controller.get("tapped_permanents", set()): return False
            # Check blocker restrictions (can't block, decayed) - must be done before Banding overrides
            if self._check_keyword(blocker, "can't block"): return False
            if self._check_keyword(blocker, "decayed"): return False # Decayed can't block
            # Conditional Blocking ("Can block only...")
            match_only = re.search(r"can block only creatures with ([\w\s]+)", getattr(blocker, 'oracle_text', '').lower())
            if match_only:
                required_ability = match_only.group(1).strip()
                if not self._check_keyword(attacker, required_ability): return False # Attacker doesn't have required ability
            # --- End Basic Checks ---


            # --- Banding Interactions (Rules 702.22) ---
            # 702.22c: Creature with banding can block creatures with evasion that normally couldn't be blocked.
            attacker_has_evasion_blocked_by_banding = False
            if self._has_keyword(attacker,"fear") or self._has_keyword(attacker,"intimidate") or self._get_landwalk_type(attacker):
                attacker_has_evasion_blocked_by_banding = True

            if self._check_keyword(blocker, "banding") and attacker_has_evasion_blocked_by_banding:
                logging.debug(f"Blocker {blocker.name} with Banding can block {attacker.name} despite evasion.")
                # It can block *if* no other restrictions apply. Don't return True yet, proceed to other checks.
                pass # Banding allows block against Fear/Intimidate/Landwalk, other checks still apply

            # 702.22f: Creature with banding attacking ignores most blocking restrictions on the blockers.
            if self._check_keyword(attacker, "banding"):
                # It bypasses flying/reach, shadow, landwalk, fear, intimidate restrictions ON THE BLOCKER
                # It does NOT bypass "can't block", "can only block X", protection, conditional unblockable ("except by"), skulk, or basic unblockable.
                logging.debug(f"Attacker {attacker.name} has Banding, ignoring most blocker evasion requirements.")
                # We still need to check protection and other specific restrictions below. Don't return True yet.
                pass # Banding attacker ignores *some* restrictions, continue checking others


            # --- Remaining Checks (Protection, Evasion, Conditional) ---
            # Protection (Prevents blocking - Rule 702.16e)
            # Check both ways: Attacker protected from Blocker, Blocker protected from Attacker
            if self._has_protection_from(attacker, blocker, attacker_controller, blocker_controller): return False
            if self._has_protection_from(blocker, attacker, blocker_controller, attacker_controller): return False

            # Attacker's Evasion (only if not overridden by Attacker's Banding)
            if not self._check_keyword(attacker, "banding"):
                if self._check_keyword(attacker, "unblockable"):
                    if "except by" not in getattr(attacker, 'oracle_text', '').lower(): return False

                if self._check_keyword(attacker, "flying") and not (self._check_keyword(blocker, "flying") or self._check_keyword(blocker, "reach")): return False
                if self._check_keyword(attacker, "shadow") and not self._check_keyword(blocker, "shadow"): return False
                # Landwalk (if defender controls relevant land type) - Blocked by Blocker Banding if applicable
                landwalk_type = self._get_landwalk_type(attacker)
                if landwalk_type and self._controls_land_type(blocker_controller, landwalk_type):
                    if not self._check_keyword(blocker, "banding"): return False # Cannot block unless blocker has banding

                # Fear (Blocked by Blocker Banding)
                if self._check_keyword(attacker, "fear") and not (self._check_keyword(blocker, "artifact") or self._check_keyword(blocker, "black")):
                    if not self._check_keyword(blocker, "banding"): return False

                # Intimidate (Blocked by Blocker Banding)
                if self._check_keyword(attacker, "intimidate") and not (self._check_keyword(blocker, "artifact") or self._share_color(attacker, blocker)):
                    if not self._check_keyword(blocker, "banding"): return False

                # Skulk (Not affected by Banding)
                if self._check_keyword(attacker, "skulk") and (getattr(blocker, 'power', 0) or 0) > (getattr(attacker, 'power', 0) or 0): return False
                # Horsemanship (Assume not affected by Banding unless rules specify)
                if self._check_keyword(attacker, "horsemanship") and not self._check_keyword(blocker, "horsemanship"): return False

            # Conditional Unblockable ("Can't be blocked except by...") - Not bypassed by Banding
            match_except = re.search(r"can't be blocked except by ([\w\s]+)", getattr(attacker, 'oracle_text', '').lower())
            if match_except:
                exception_criteria = match_except.group(1).strip().lower().split()
                blocker_meets = False
                # Check blocker properties against criteria
                if 'artifacts' in exception_criteria and self._check_keyword(blocker, "artifact"): blocker_meets = True
                elif 'artifact creature' in exception_criteria and self._check_keyword(blocker, "artifact") and 'creature' in getattr(blocker, 'card_types',[]): blocker_meets = True
                elif 'walls' in exception_criteria and 'wall' in getattr(blocker, 'subtypes', []): blocker_meets = True
                elif 'creatures with flying' in exception_criteria and self._check_keyword(blocker, "flying"): blocker_meets = True
                # Add color checks
                elif 'white' in exception_criteria and self._has_color(blocker, 'white'): blocker_meets = True
                elif 'blue' in exception_criteria and self._has_color(blocker, 'blue'): blocker_meets = True
                # Add power checks
                elif any(c.startswith("power ") for c in exception_criteria):
                    power_req = int(re.search(r"power (\d+)", exception_criteria).group(1))
                    comparator = ">=" if "or greater" in exception_criteria else "<=" if "or less" in exception_criteria else "=="
                    blocker_power = getattr(blocker,'power',0) or 0
                    if eval(f"{blocker_power} {comparator} {power_req}"): blocker_meets = True
                # Add more common exceptions
                if not blocker_meets: return False # Blocker does not meet the specific exception criteria

            # Menace check handled during assignment (needs 2+ blockers) - individual block check is fine here

            return True # No restriction found preventing this specific block

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