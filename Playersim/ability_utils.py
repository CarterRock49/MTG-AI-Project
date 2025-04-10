"""Utility functions for ability processing."""
import logging
import re
from .ability_types import AbilityEffect, DrawCardEffect, GainLifeEffect, DamageEffect, \
    CounterSpellEffect, CreateTokenEffect, DestroyEffect, ExileEffect, \
    DiscardEffect, MillEffect
# --- MODIFIED: Added TapEffect, UntapEffect, BuffEffect, SearchLibraryEffect ---
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
        parts = re.split(r'\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+(?:then\s+)?|\s+then\s+', effect_text.lower().strip('. '))
        processed_clauses.extend(p.strip() for p in parts if p.strip())
        if not processed_clauses: processed_clauses = [effect_text.lower()] # Use full text if split fails

        for clause in processed_clauses:
            created_effect = None
            # Draw Card
            match = re.search(r"(?:target player|you)?\s*\b(draw(?:s)?)\b\s+(a|an|one|two|three|four|\d+)\s+cards?", clause)
            if match:
                count = text_to_number(match.group(2))
                target_desc = EffectFactory._extract_target_description(clause) or "controller"
                created_effect = DrawCardEffect(count, target=target_desc)

            # Gain Life
            elif re.search(r"(?:target player|you)?\s*\b(gain(?:s)?)\b\s+(\d+|x)\s+life", clause):
                amount_str = re.search(r"gain(?:s)?\s+(\d+|x)\s+life", clause).group(1)
                amount = text_to_number(amount_str) if amount_str != 'x' else 1 # Default X=1 for now
                target_desc = EffectFactory._extract_target_description(clause) or "controller"
                created_effect = GainLifeEffect(amount, target=target_desc)

            # Damage
            elif re.search(r"\b(deals?)\b.*\bdamage\b", clause):
                amount_match = re.search(r"deals?\s+(\d+|x)\s+damage", clause)
                amount = 1
                if amount_match: amount = text_to_number(amount_match.group(1)) if amount_match.group(1) != 'x' else 1
                target_desc = EffectFactory._extract_target_description(clause) or "any target"
                target_type = "any target" # Default
                if "creature or player" in target_desc or "any target" in target_desc: target_type="any target"
                elif "creature" in target_desc: target_type="creature"
                elif "player" in target_desc or "opponent" in target_desc: target_type="player"
                elif "planeswalker" in target_desc: target_type="planeswalker"
                elif "battle" in target_desc: target_type="battle"
                elif "each opponent" in target_desc: target_type="each opponent"
                created_effect = DamageEffect(amount, target_type=target_type)

            # Destroy
            elif re.search(r"\b(destroy(?:s)?)\b\s+target", clause):
                 target_desc = EffectFactory._extract_target_description(clause) or "permanent"
                 target_type = "permanent"
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "enchantment" in target_desc: target_type = "enchantment"
                 elif "land" in target_desc: target_type = "land"
                 elif "nonland permanent" in target_desc: target_type = "nonland permanent"
                 created_effect = DestroyEffect(target_type=target_type)

            # Exile
            elif re.search(r"\b(exile(?:s)?)\b\s+target", clause):
                 target_desc = EffectFactory._extract_target_description(clause) or "permanent"
                 target_type = "permanent"
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 # Add more types
                 zone_match = re.search(r"from (?:the )?(\w+)", clause)
                 zone = zone_match.group(1) if zone_match else "battlefield"
                 created_effect = ExileEffect(target_type=target_type, zone=zone)

            # Create Token
            elif re.search(r"\b(create(?:s)?)\b", clause) and "token" in clause:
                 count_match = re.search(r"create(?:s)?\s+(a|an|one|two|three|\d+)", clause)
                 count = text_to_number(count_match.group(1)) if count_match else 1
                 pt_match = re.search(r"(\d+)/(\d+)", clause)
                 power, toughness = (int(pt_match.group(1)), int(pt_match.group(2))) if pt_match else (1, 1)
                 type_match = re.search(r"(\d+/\d+)\s+(?:([\w\-]+)\s+)?(\w+)\s+(?:creature )?token", clause)
                 creature_type = type_match.group(3).capitalize() if type_match else "Creature"
                 keywords = []
                 kw_match = re.search(r"with ([\w\s,]+)", clause)
                 if kw_match: keywords = [k.strip() for k in kw_match.group(1).split(',') if k.strip()]
                 created_effect = CreateTokenEffect(power, toughness, creature_type, count, keywords)

            # Buff (+X/+Y)
            elif re.search(r"(?:target |creatures you control)?\s*(get(?:s)?)\b\s+([+\-]\d+)/([+\-]\d+)", clause):
                match = re.search(r"get(?:s)?\s+([+\-]\d+)/([+\-]\d+)", clause)
                p_mod, t_mod = int(match.group(1)), int(match.group(2))
                duration = "end_of_turn" if "until end of turn" in clause else "permanent"
                target_desc = EffectFactory._extract_target_description(clause) or "creatures you control"
                target_type = "creature" # Determine based on target_desc
                if "target creature" in target_desc: target_type = "target creature"
                elif "creatures you control" in target_desc: target_type = "creatures you control"
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
                 # Add others
                 created_effect = UntapEffect(target_type=target_type)

            # Add Counters
            elif re.search(r"\bput(?:s)?\b.*?\bcounter", clause):
                 count_match = re.search(r"put\s+(a|an|one|two|three|\d+)", clause)
                 count = text_to_number(count_match.group(1)) if count_match else 1
                 type_match = re.search(r"([\+\-\d/]+|[\w\-]+)\s+counter", clause) # Matches "+1/+1", "-1/-1", "loyalty", "charge" etc.
                 counter_type = type_match.group(1).replace('/','_') if type_match else "+1/+1" # Default
                 target_desc = EffectFactory._extract_target_description(clause) or "self" # Default target is source? Or requires target? Needs context.
                 target_type = "creature" # Determine from desc
                 if "target creature" in target_desc: target_type = "target creature"
                 elif "target permanent" in target_desc: target_type = "target permanent"
                 elif "target artifact" in target_desc: target_type = "target artifact"
                 elif "target planeswalker" in target_desc: target_type = "target planeswalker"
                 elif "this permanent" in target_desc or target_desc == "self": target_type="self"
                 created_effect = AddCountersEffect(counter_type, count, target_type=target_type)


            if created_effect:
                effects.append(created_effect)
            else:
                 # Add generic effect if specific parsing fails for this clause
                 logging.debug(f"Adding generic AbilityEffect for clause: '{clause}'")
                 effects.append(AbilityEffect(clause))

        # Final fallback if no clauses yielded effects
        if not effects and effect_text:
            logging.warning(f"Could not parse effect text into specific effects: '{effect_text}'. Adding as generic effect.")
            effects.append(AbilityEffect(effect_text))

        return effects
    