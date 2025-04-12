"""Utility functions for ability processing."""
import logging
import re
from .ability_types import AbilityEffect, DrawCardEffect, GainLifeEffect, DamageEffect, \
    CounterSpellEffect, CreateTokenEffect, DestroyEffect, ExileEffect, \
    DiscardEffect, MillEffect
from .ability_types import TapEffect, UntapEffect, BuffEffect, SearchLibraryEffect, AddCountersEffect # Added AddCountersEffect too

def is_beneficial_effect(effect_text):
    # (Keep existing implementation)
    """
    Determine if an effect text describes an effect that is beneficial to its target.
    Improved logic with more context and specific phrases.

    Args:
        effect_text: The text of the effect to analyze

    Returns:
        bool: True if the effect is likely beneficial to the target, False otherwise
    """
    effect_text = effect_text.lower() if effect_text else ""

    # Explicitly harmful phrases (high confidence)
    harmful_phrases = [
        "destroy target", "exile target", "sacrifice", "lose life", "deals damage",
        "discard", "counter target spell", "mill", "target player loses", "opponent draws",
        "each player sacrifices", "pay life", "skip your", "remove",
        "can't attack", "can't block", "can't cast spells", "doesn't untap", "tap target"
    ]
    for phrase in harmful_phrases:
        if phrase in effect_text:
            # Exception: Damage prevention
            if "damage" in phrase and ("prevent" in effect_text or "prevented" in effect_text):
                continue
            # Exception: Self-damage for benefit (needs more context, risky to classify)
            # Exception: Sacrificing for benefit (needs more context)
            if phrase == "sacrifice" and "as an additional cost" not in effect_text: # Basic check
                 # If sacrificing own stuff not as cost, usually bad for target being sac'd
                 if "you control" in effect_text: # Targeting self is bad
                     # Hard to say if it benefits the *controller* ultimately. Stick to target.
                     pass # Can't easily determine for target
                 else: # Target opponent sacrifices, bad for them
                      return False
            elif phrase == "deals damage":
                # Check if it targets the *controller* (bad for controller)
                if "deals damage to you" in effect_text or "damage to its controller" in effect_text:
                    pass # Ambiguous - target is controller, but effect *originates* elsewhere
                # Check if it targets opponent (bad for opponent)
                elif re.search(r"deals \d+ damage to target opponent", effect_text):
                    return False
                elif re.search(r"deals \d+ damage to target creature", effect_text):
                     # Harmful to creature, but beneficial to controller if it's opponent's creature
                     # For *target* creature, it's harmful.
                    return False
                elif re.search(r"deals \d+ damage to any target", effect_text):
                     # Ambiguous, could target opponent (harmful) or self (harmful)
                    return False # Assume harmful default for damage
            else:
                return False # Phrase is generally harmful

    # Explicitly beneficial phrases (high confidence)
    beneficial_phrases = [
        "gain life", "draw cards", "+1/+1 counter", "+x/+x", "create token", "search your library",
        "put onto the battlefield", "add {", "untap target", "gain control", "hexproof",
        "indestructible", "protection from", "regenerate", "prevent", "double", "copy"
    ]
    for phrase in beneficial_phrases:
        if phrase in effect_text:
            # Exception: "Protection from" might prevent beneficial effects too (rare).
            # Exception: Creating tokens for opponent is bad for controller.
            if "create token" in phrase and "opponent controls" in effect_text:
                return False
            return True

    # Context-dependent keywords: "return"
    if "return" in effect_text:
        if "return target creature" in effect_text and "to its owner's hand" in effect_text:
            return False # Bounce is harmful to target owner
        if "return target" in effect_text and "from your graveyard" in effect_text and ("to your hand" in effect_text or "to the battlefield" in effect_text):
            return True # Recursion is beneficial

    # Keywords generally beneficial for the permanent
    beneficial_keywords = [
        "flying", "first strike", "double strike", "trample", "vigilance",
        "haste", "lifelink", "reach", "menace", # Menace debatable, but usually better for attacker
    ]
    for keyword in beneficial_keywords:
        # Check for "gains <keyword>" or "has <keyword>"
        if re.search(rf"(gains?|has)\s+{keyword}", effect_text):
            return True

    # Keywords generally harmful for the permanent
    harmful_keywords = ["defender", "decayed"] # Decayed: Can't block, sac after attack
    for keyword in harmful_keywords:
        if re.search(rf"(gains?|has)\s+{keyword}", effect_text):
            return False

    # If no clear indicator, default to harmful/neutral for safety
    # Many effects involve interaction and aren't purely beneficial.
    logging.debug(f"Could not confidently determine benefit of '{effect_text}'. Defaulting to False.")
    return False

def text_to_number(text):
    """Convert text number (e.g., 'three') to integer."""
    text_to_num = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10
    }

    if isinstance(text, str) and text.lower() in text_to_num:
        return text_to_num[text.lower()]

    try:
        return int(text)
    except (ValueError, TypeError):
        return 1  # Default to 1 if conversion fails

def resolve_simple_targeting(game_state, card_id, controller, effect_text):
    """Simplified targeting resolution when targeting system isn't available"""
    targets = {"creatures": [], "players": [], "spells": [], "lands": [],
            "artifacts": [], "enchantments": [], "permanents": []}
    opponent = game_state.p2 if controller == game_state.p1 else game_state.p1

    # Target creature
    if "target creature" in effect_text:
        for player in [opponent, controller]:  # Prioritize opponent targets
            for card_id in player["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    # Check restrictions
                    if "you control" in effect_text and player != controller:
                        continue
                    if "opponent controls" in effect_text and player == controller:
                        continue
                    targets["creatures"].append(card_id)
                    break  # Just take the first valid target
            if targets["creatures"]:
                break

    # Target player
    if "target player" in effect_text or "target opponent" in effect_text:
        if "target opponent" in effect_text:
            targets["players"].append("p2" if controller == game_state.p1 else "p1")
        else:
            # Default to targeting opponent
            targets["players"].append("p2" if controller == game_state.p1 else "p1")

    # Target land
    if "target land" in effect_text:
        for player in [opponent, controller]:
            for card_id in player["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'type_line') and 'land' in card.type_line.lower():
                    targets["lands"].append(card_id)
                    break  # Just take the first valid target
            if targets["lands"]:
                break

    # Target artifact
    if "target artifact" in effect_text:
        for player in [opponent, controller]:
            for card_id in player["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'artifact' in card.card_types:
                    targets["artifacts"].append(card_id)
                    break  # Just take the first valid target
            if targets["artifacts"]:
                break

    # Target enchantment
    if "target enchantment" in effect_text:
        for player in [opponent, controller]:
            for card_id in player["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'enchantment' in card.card_types:
                    targets["enchantments"].append(card_id)
                    break  # Just take the first valid target
            if targets["enchantments"]:
                break

    # Target permanent (any permanent type)
    if "target permanent" in effect_text:
        for player in [opponent, controller]:
            if player["battlefield"]:
                targets["permanents"].append(player["battlefield"][0])  # Just take the first one
                break

    # Target spell (on the stack)
    if "target spell" in effect_text:
        for item in reversed(list(game_state.stack)):  # Start from top of stack
            if isinstance(item, tuple) and len(item) >= 3 and item[0] == "SPELL":
                spell_id = item[1]
                spell_card = game_state._safe_get_card(spell_id)

                if not spell_card:
                    continue

                # Check for spell type restrictions
                if "creature spell" in effect_text and (not hasattr(spell_card, 'card_types') or
                                                    'creature' not in spell_card.card_types):
                    continue
                elif "noncreature spell" in effect_text and (hasattr(spell_card, 'card_types') and
                                                        'creature' in spell_card.card_types):
                    continue

                targets["spells"].append(spell_id)
                break

    return targets

def safe_int(value, default=0):
    """Safely convert a value to int, handling None and non-numeric strings."""
    if value is None: return default
    if isinstance(value, int): return value
    if isinstance(value, float): return int(value) # Allow floats
    if isinstance(value, str):
        if value.isdigit() or (value.startswith('-') and value[1:].isdigit()):
            return int(value)
    # Return default for non-convertible types or values like '*'
    return default

def resolve_targeting(game_state, card_id, controller, effect_text):
    """Enhanced targeting resolution that uses TargetingSystem if available, otherwise falls back to simple targeting."""
    # Try to use TargetingSystem if available
    # Check both targeting_system and ability_handler.targeting_system
    targeting_system = getattr(game_state, 'targeting_system', None) or \
                       getattr(getattr(game_state, 'ability_handler', None), 'targeting_system', None)

    if targeting_system:
        if hasattr(targeting_system, 'resolve_targeting'):
             # Assuming resolve_targeting can handle both spells and abilities based on source_id and effect_text
             return targeting_system.resolve_targeting(card_id, controller, effect_text)
        # Add checks for other method names if necessary

    # Fall back to simple targeting
    logging.warning(f"TargetingSystem not found or missing 'resolve_targeting' method. Using simple targeting fallback.")
    return resolve_simple_targeting(game_state, card_id, controller, effect_text)


class EffectFactory:
    """
    Factory class to create AbilityEffect objects.
    NOTE: This parser is basic and covers common cases. Many MTG effects have
    complex conditions, targets, and variations not captured here.
    """
    @staticmethod
    def _extract_target_description(effect_text):
        # (Keep existing helper implementation)
        """Helper to find the most specific target description."""
        # Pattern tries to find "target [adjective(s)] [type]"
        match = re.search(r"target\s+(?:(up to \w+)\s+)?(?:((?:[\w\-]+\s+)*?)(\w+))?", effect_text)
        if match:
            count_mod, adjectives, noun = match.groups()
            desc = ""
            if count_mod: desc += count_mod + " "
            if adjectives: desc += adjectives.strip() + " "
            if noun: desc += noun
            return desc.strip() if desc else "target" # Fallback to generic 'target' if parts missing
        elif "each opponent" in effect_text: return "each opponent"
        elif "each player" in effect_text: return "each player"
        elif "you" == effect_text.split()[0]: return "controller" # Simple "You draw a card"
        elif re.search(r"(creatures?|permanents?) you control", effect_text): return "permanents you control" # Group targets
        return None # No target description found


    @staticmethod
    def create_effects(effect_text, targets=None): # targets arg currently unused here
        """Create appropriate AbilityEffect objects based on the effect text."""
        if not effect_text: return []

        effects = []
        processed_clauses = []
        # Basic clause splitting (commas, 'and', 'then') - needs improvement for complex sentences
        # Added splitting on sentence endings like ". Then" or "; then"
        parts = re.split(r'\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+(?:then\s+)?|\s+then\s+|(?<=[.;])\s+then\s+', effect_text.lower().strip('. '))
        processed_clauses.extend(p.strip() for p in parts if p.strip())
        if not processed_clauses: processed_clauses = [effect_text.lower()] # Use full text if split fails

        # Assuming these are imported at the module level:
        from .ability_types import (AbilityEffect, DrawCardEffect, GainLifeEffect, DamageEffect,
            CounterSpellEffect, CreateTokenEffect, DestroyEffect, ExileEffect,
            DiscardEffect, MillEffect, TapEffect, UntapEffect, BuffEffect,
            SearchLibraryEffect, AddCountersEffect, ReturnToHandEffect,
            ScryEffect, SurveilEffect, LifeDrainEffect, CopySpellEffect, TransformEffect, FightEffect)

        for clause in processed_clauses:
            created_effect = None

            # --- Existing Effect Parsing (Keep current logic) ---

            # Draw Card
            match = re.search(r"(?:target player|you)?\s*\b(draw(?:s)?)\b\s+(a|an|one|two|three|four|\d+)\s+cards?", clause)
            if match:
                count = text_to_number(match.group(2))
                target_desc = EffectFactory._extract_target_description(clause) or "controller"
                target_specifier = "controller"
                if "target player" in target_desc: target_specifier = "target_player"
                elif "opponent" in target_desc: target_specifier = "opponent"
                elif "each player" in target_desc: target_specifier = "each_player"
                created_effect = DrawCardEffect(count, target=target_specifier)

            # Gain Life
            elif re.search(r"(?:target player|you)?\s*\b(gain(?:s)?)\b\s+(\d+|x)\s+life", clause):
                amount_str_match = re.search(r"gain(?:s)?\s+(\d+|x)\s+life", clause)
                if amount_str_match: # Check if match found before accessing group
                     amount_str = amount_str_match.group(1)
                     amount = text_to_number(amount_str) if amount_str != 'x' else 1 # Default X=1 for now
                     target_desc = EffectFactory._extract_target_description(clause) or "controller"
                     target_specifier = "controller"
                     if "target player" in target_desc: target_specifier = "target_player"
                     elif "opponent" in target_desc: target_specifier = "opponent"
                     elif "each player" in target_desc: target_specifier = "each_player"
                     created_effect = GainLifeEffect(amount, target=target_specifier)

            # Damage
            elif re.search(r"\b(deals?)\b.*\bdamage\b", clause):
                amount_match = re.search(r"deals?\s+(\d+|x)\s+damage", clause)
                amount = 1 # Default if not specified
                if amount_match: amount = text_to_number(amount_match.group(1)) if amount_match.group(1) != 'x' else 1
                target_desc = EffectFactory._extract_target_description(clause) or "any target" # Changed default
                target_type = "any target" # Default
                if "creature or player" in target_desc or "any target" in target_desc: target_type="any target"
                elif "creature" in target_desc: target_type="creature"
                elif "player" in target_desc or "opponent" in target_desc: target_type="player"
                elif "planeswalker" in target_desc: target_type="planeswalker"
                elif "battle" in target_desc: target_type="battle"
                elif "each opponent" in target_desc: target_type="each opponent"
                elif "each creature" in target_desc: target_type="each creature"
                elif "each player" in target_desc: target_type="each player" # Added
                created_effect = DamageEffect(amount, target_type=target_type)

            # Destroy
            elif re.search(r"\b(destroy(?:s)?)\b\s+(target|all|each)", clause):
                 target_desc = EffectFactory._extract_target_description(clause) or "permanent" # Default if specific target word used
                 target_type = "permanent"
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "enchantment" in target_desc: target_type = "enchantment"
                 elif "land" in target_desc: target_type = "land"
                 elif "nonland permanent" in target_desc: target_type = "nonland permanent"
                 elif "planeswalker" in target_desc: target_type = "planeswalker" # Added
                 elif "all creatures" in target_desc or ("each creature" in target_desc and "target" not in clause): target_type = "all creatures"
                 elif "all permanents" in target_desc or ("each permanent" in target_desc and "target" not in clause): target_type = "all permanents"
                 # Add other "all X" types
                 created_effect = DestroyEffect(target_type=target_type)

            # Exile
            elif re.search(r"\b(exile(?:s)?)\b\s+(target|all|each)", clause):
                 target_desc = EffectFactory._extract_target_description(clause) or "permanent"
                 target_type = "permanent"
                 # Add specific type checks similar to Destroy
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "card" in target_desc: target_type = "card" # Card in other zones
                 elif "spell" in target_desc: target_type = "spell" # Stack target
                 # ... add more
                 zone_match = re.search(r"from (?:the )?(\w+)", clause)
                 zone = zone_match.group(1) if zone_match else "battlefield"
                 created_effect = ExileEffect(target_type=target_type, zone=zone)

            # Create Token
            elif re.search(r"\b(create(?:s)?)\b", clause) and "token" in clause:
                 count_match = re.search(r"create(?:s)?\s+(a|an|one|two|three|\d+)", clause)
                 count = text_to_number(count_match.group(1)) if count_match else 1
                 pt_match = re.search(r"(\d+)/(\d+)", clause)
                 power, toughness = (int(pt_match.group(1)), int(pt_match.group(2))) if pt_match else (1, 1)
                 # Improved type parsing
                 type_match = re.search(r"(\d+/\d+)\s+((?:[\w\s\-]+?)\s+)?(creature|artifact creature)\s+token(?:s)?(?:\s+named\s+([\w\s]+))?", clause)
                 creature_type = "Creature" # Default
                 if type_match:
                      # type_match.group(2) has color/modifiers, group(3) is base type (creature/artifact creature), group(4) optional name
                      creature_type = type_match.group(3).capitalize()
                      if type_match.group(4): creature_type = type_match.group(4).capitalize() # Use explicit name if provided
                      elif type_match.group(2): # Use modifier as type if no name and type is generic
                           # Check if modifier is likely a type (e.g., "Goblin", "Spirit") vs color ("white")
                           potential_subtype = type_match.group(2).strip().capitalize()
                           if potential_subtype not in ["White", "Blue", "Black", "Red", "Green", "Colorless", "Artifact"]:
                                creature_type = potential_subtype
                 keywords = []
                 kw_match = re.search(r"with ([\w\s,]+)", clause)
                 if kw_match: keywords = [k.strip().capitalize() for k in kw_match.group(1).split(',') if k.strip()]
                 # Color parsing needed
                 color_match = re.search(r"\b(white|blue|black|red|green)\b", clause)
                 colors = [color_match.group(1)] if color_match else None
                 created_effect = CreateTokenEffect(power, toughness, creature_type, count, keywords, colors=colors)


            # Buff (+X/+Y)
            elif re.search(r"(?:target |creatures you control|each creature\b)?\s*(get(?:s)?|has)\b\s+([+\-]\d+)/([+\-]\d+)", clause):
                match = re.search(r"(get(?:s)?|has)\s+([+\-]\d+)/([+\-]\d+)", clause)
                p_mod, t_mod = int(match.group(2)), int(match.group(3))
                duration = "end_of_turn" if "until end of turn" in clause else "permanent"
                target_desc = EffectFactory._extract_target_description(clause) or "creatures you control"
                target_type = "creature" # Determine based on target_desc
                if "target creature" in target_desc: target_type = "target creature"
                elif "creatures you control" in target_desc: target_type = "creatures you control"
                elif "each creature" in target_desc and "target" not in clause: target_type = "each creature" # Target all
                created_effect = BuffEffect(p_mod, t_mod, duration=duration, target_type=target_type)

            # Tap
            elif re.search(r"\b(tap(?:s)?)\b\s+target", clause):
                 target_desc = EffectFactory._extract_target_description(clause) or "permanent"
                 target_type = "permanent" # Refine based on desc
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "land" in target_desc: target_type = "land"
                 created_effect = TapEffect(target_type=target_type)

            # Untap
            elif re.search(r"\b(untap(?:s)?)\b\s+target", clause):
                 target_desc = EffectFactory._extract_target_description(clause) or "permanent"
                 target_type = "permanent" # Refine based on desc
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "land" in target_desc: target_type = "land"
                 created_effect = UntapEffect(target_type=target_type)

            # Add Counters
            elif re.search(r"\bput(?:s)?\b.*?\bcounter", clause):
                 count_match = re.search(r"put\s+(a|an|one|two|three|\d+)", clause)
                 count = text_to_number(count_match.group(1)) if count_match else 1
                 # Capture counter type more broadly, including words like 'loyalty', 'charge', 'poison'
                 type_match = re.search(r"([\+\-\d/]+)\s+counter|\b(loyalty|charge|poison|time|fade|level|quest|storage|shield)\s+counter", clause)
                 counter_type = "+1/+1" # Default
                 if type_match:
                     if type_match.group(1): # Found P/T modifier type
                         counter_type = type_match.group(1).replace('/','_')
                     elif type_match.group(2): # Found named counter type
                         counter_type = type_match.group(2).lower()
                 # Determine target
                 target_desc = EffectFactory._extract_target_description(clause) or "self" # Default to self if no target word
                 target_type = "self" # Default if self or not targeted
                 if "target" in target_desc:
                     target_type = "permanent" # Base for targeted effects
                     if "creature" in target_desc: target_type = "target creature"
                     elif "artifact" in target_desc: target_type = "target artifact"
                     elif "enchantment" in target_desc: target_type = "target enchantment"
                     elif "land" in target_desc: target_type = "target land"
                     elif "planeswalker" in target_desc: target_type = "target planeswalker"
                     elif "player" in target_desc: target_type = "target player" # For poison, etc.
                     elif "permanent" in target_desc: target_type = "target permanent"
                     elif "battle" in target_desc: target_type = "target battle" # Battles have defense counters
                 elif "each creature" in target_desc and "target" not in clause: target_type = "each creature"
                 elif "each opponent" in target_desc and "target" not in clause: target_type = "each opponent"

                 created_effect = AddCountersEffect(counter_type, count, target_type=target_type)

            # Counter Spell
            elif re.search(r"\bcounter(?:s)?\b\s+target", clause):
                target_desc = EffectFactory._extract_target_description(clause) or "spell"
                target_type = "spell" # Default
                if "creature spell" in target_desc: target_type = "creature spell"
                elif "noncreature spell" in target_desc: target_type = "noncreature spell"
                elif "activated ability" in target_desc: target_type = "activated ability"
                elif "triggered ability" in target_desc: target_type = "triggered ability"
                elif "ability" in target_desc: target_type = "ability" # Generic ability
                created_effect = CounterSpellEffect(target_type=target_type)

            # Discard
            elif re.search(r"\bdiscard(?:s)?\b", clause):
                 count = 1
                 # Check for specific count or "all"
                 count_match = re.search(r"discard\s+(a|an|one|two|three|four|\d+|all|x)\s+cards?", clause)
                 is_random = "at random" in clause
                 if count_match:
                     count_str = count_match.group(1)
                     count = -1 if count_str == "all" else text_to_number(count_str) if count_str != 'x' else -2 # Use -2 to denote X for discard?

                 target_desc = EffectFactory._extract_target_description(clause) or "target_player" # Default target
                 target_specifier = "target_player"
                 if "you discard" in clause: target_specifier = "controller"
                 elif "opponent discards" in clause or "each opponent discards" in clause: target_specifier = "opponent"
                 elif "each player discards" in clause: target_specifier = "each_player"
                 created_effect = DiscardEffect(count, target=target_specifier, is_random=is_random)

            # Mill
            elif re.search(r"\bmill(?:s)?\b", clause):
                count = 1
                count_match = re.search(r"mill(?:s)?\s+(\d+)\s+cards?", clause)
                if count_match: count = int(count_match.group(1))

                target_desc = EffectFactory._extract_target_description(clause) or "target_player"
                target_specifier = "target_player"
                if "you mill" in clause: target_specifier = "controller"
                elif "opponent mills" in clause or "each opponent mills" in clause: target_specifier = "opponent"
                elif "each player mills" in clause: target_specifier = "each player" # Added
                created_effect = MillEffect(count, target=target_specifier)

            # Return to Hand (Bounce)
            elif re.search(r"\breturn(?:s)?\b", clause) and ("to (?:its|their) owner's hand" in clause or "to your hand" in clause):
                target_desc = EffectFactory._extract_target_description(clause) or "permanent"
                target_type = "permanent"
                zone = "battlefield" # Default zone
                if "target card" in target_desc: target_type = "card" # Could be from GY etc.
                if "target creature" in target_desc: target_type = "creature"
                elif "target artifact" in target_desc: target_type = "artifact"
                elif "target enchantment" in target_desc: target_type = "enchantment"
                elif "target land" in target_desc: target_type = "land"
                # Check originating zone
                if "from your graveyard" in clause: zone = "graveyard"; target_type="card"
                elif "from exile" in clause: zone = "exile"; target_type="card"
                # Add other zones
                created_effect = ReturnToHandEffect(target_type=target_type, zone=zone)

            # Search Library
            elif re.search(r"\bsearch(?:es)?\s+your library", clause):
                 count = 1
                 count_match = re.search(r"search.* for (a|an|one|two|three|\d+)\s", clause)
                 if count_match: count = text_to_number(count_match.group(1))

                 # Extract card type criteria more robustly
                 type_match = re.search(r"for (?:a|an|one|two|three|\d+)?\s*((?:[\w\-]+\s+)*[\w\-]+)\s+card", clause)
                 search_type = "any" # Default
                 if type_match:
                      search_type = type_match.group(1).strip()
                      # Normalize common types (could use a set/dict for better matching)
                      known_types = ["basic land", "creature", "artifact", "enchantment", "land", "instant", "sorcery", "planeswalker", "battle", "legendary"]
                      if search_type not in known_types: search_type = "any" # Reset if specific parse failed

                 # Determine destination
                 dest_match = re.search(r"put (?:it|that card|them) (?:onto|into|in) (?:the|your) (\w+)", clause)
                 destination = "hand" # Default
                 if dest_match:
                      dest_word = dest_match.group(1)
                      if dest_word == "battlefield": destination = "battlefield"
                      elif dest_word == "graveyard": destination = "graveyard"
                      elif dest_word == "hand": destination = "hand"
                      elif dest_word == "library": destination = "library_top" # Assume top
                 # Check if destination implies battlefield tapped
                 # tapped = "tapped" in (dest_match.group(0) if dest_match else "")

                 created_effect = SearchLibraryEffect(search_type=search_type, destination=destination, count=count)

            # --- New Effects ---
            # Scry
            elif re.search(r"\bscry\b", clause):
                match = re.search(r"scry (\d+|x)\b", clause)
                count = 1 # Default Scry 1
                if match: count = text_to_number(match.group(1)) if match.group(1) != 'x' else 1
                created_effect = ScryEffect(count)

            # Surveil
            elif re.search(r"\bsurveil\b", clause):
                 match = re.search(r"surveil (\d+|x)\b", clause)
                 count = 1 # Default Surveil 1
                 if match: count = text_to_number(match.group(1)) if match.group(1) != 'x' else 1
                 created_effect = SurveilEffect(count)

            # Life Drain
            elif re.search(r"(lose|loses) (\d+|x) life and (?:you gain|controller gains) (\d+|that much) life", clause):
                amount_match = re.search(r"loses? (\d+|x) life", clause)
                amount = 1 # Default
                if amount_match: amount = text_to_number(amount_match.group(1)) if amount_match.group(1) != 'x' else 1
                target_desc = EffectFactory._extract_target_description(clause) or "target_opponent"
                target_specifier = "opponent" # Default for lose life
                if "target player" in target_desc: target_specifier = "target_player"
                elif "each opponent" in target_desc: target_specifier = "each_opponent"

                # Check gain amount matches loss amount
                gain_match_res = re.search(r"gain (\d+|that much) life", clause)
                if gain_match_res:
                     gain_match_str = gain_match_res.group(1)
                     if gain_match_str == "that much" or (gain_match_str.isdigit() and text_to_number(gain_match_str) == amount):
                         created_effect = LifeDrainEffect(amount, target=target_specifier)
                     else: # Separate loss/gain effects needed
                         gain_amount = text_to_number(gain_match_str)
                         # Add both loss and gain effect - ORDER MATTERS
                         # Rules usually process loss first for triggers, then gain? Add Loss first.
                         # effects.append(LifeLossEffect(amount, target=target_specifier)) # Need LifeLossEffect
                         effects.append(GainLifeEffect(gain_amount))
                 # If no gain phrase found or amounts differ, only create loss if needed (needs LifeLossEffect)


            # Copy Spell
            elif re.search(r"\bcopy target\b.*\bspell\b", clause):
                 target_type = "spell"
                 if "instant or sorcery spell" in clause: target_type = "instant or sorcery spell"
                 elif "instant spell" in clause: target_type = "instant"
                 elif "sorcery spell" in clause: target_type = "sorcery"
                 # Add other types
                 new_targets = "choose new targets" in clause
                 created_effect = CopySpellEffect(target_type=target_type, new_targets=new_targets)

            # Transform
            elif re.search(r"\btransform\b", clause):
                 created_effect = TransformEffect()

            # Fight
            elif re.search(r"\bfights?\b.*?\btarget\b", clause):
                 target_type = "creature" # Default
                 match_target = re.search(r"target ([\w\s]+)", clause)
                 if match_target:
                      desc = match_target.group(1).strip()
                      if "creature" in desc: target_type="creature"
                 created_effect = FightEffect(target_type=target_type)

            # --- Fallback and Effect Addition ---
            if created_effect:
                effects.append(created_effect)
            else:
                 # Add generic effect if specific parsing fails for this clause
                 effect_keywords = ["destroy", "exile", "draw", "gain", "lose", "counter", "create", "search", "tap", "untap", "put", "scry", "surveil", "fight", "transform", "copy", "mill", "discard", "return"]
                 if clause and any(kw in clause for kw in effect_keywords):
                     logging.debug(f"Adding generic AbilityEffect for clause: '{clause}'")
                     effects.append(AbilityEffect(clause))

        # Final fallback if no clauses yielded effects
        if not effects and effect_text:
            logging.warning(f"Could not parse effect text into specific effects: '{effect_text}'. Adding as generic effect.")
            effects.append(AbilityEffect(effect_text))

        return effects
    