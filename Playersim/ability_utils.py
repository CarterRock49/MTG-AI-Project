"""Utility functions for ability processing."""
import logging
import re

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
        """Helper to find the most specific target description."""
        # Pattern tries to find "target [adjective(s)] [type]"
        # No dash change needed here, relies on whitespace.
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
        """
        Create appropriate AbilityEffect objects based on the effect text.
        Handles clause splitting including em dashes and various common MTG effects.
        """
        if not effect_text: return []

        effects = []
        processed_clauses = []
        # Basic clause splitting (commas, 'and', 'then', em dash) - needs improvement for complex sentences
        # Added splitting on sentence endings like ". Then" or "; then" and em dash used as separator
        split_pattern = r'\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+(?:then\s+)?|\s+then\s+|(?<=[.;])\s+then\s+|\s*—\s*|\s*\u2014\s*' # Added em dash split
        parts = re.split(split_pattern, effect_text.strip('. '))
        processed_clauses.extend(p.strip() for p in parts if p.strip())
        if not processed_clauses: processed_clauses = [effect_text] # Use full text if split fails

        # Assuming these are imported at the module level of ability_utils.py:
        # (Relative import assumed)
        from .ability_types import (AbilityEffect, DrawCardEffect, GainLifeEffect, DamageEffect,
            CounterSpellEffect, CreateTokenEffect, DestroyEffect, ExileEffect,
            DiscardEffect, MillEffect, TapEffect, UntapEffect, BuffEffect,
            SearchLibraryEffect, AddCountersEffect, ReturnToHandEffect,
            ScryEffect, SurveilEffect, LifeDrainEffect, CopySpellEffect, TransformEffect, FightEffect)

        for clause in processed_clauses:
            # Process individual clauses case-insensitively
            # Remove reminder text AFTER potential keyword matching? No, remove before general parsing.
            # Ensure removal handles nested parentheses correctly if needed.
            clause_clean = re.sub(r'\s*\([^()]*?\)\s*', ' ', clause).strip() # Basic reminder text removal
            clause_lower = clause_clean.lower()
            created_effect = None

            # Draw Card
            match = re.search(r"(?:target player|you)?\s*\b(draw(?:s)?)\b\s+(a|an|one|two|three|four|five|six|seven|eight|nine|ten|x|\d+)\s+cards?", clause_lower)
            if match:
                count_str = match.group(2)
                # Pass X through, handle in effect application
                count = 'x' if count_str == 'x' else text_to_number(count_str)
                target_desc = EffectFactory._extract_target_description(clause_lower) or "controller"
                target_specifier = "controller"
                if "target player" in target_desc: target_specifier = "target_player"
                elif "opponent" in target_desc: target_specifier = "opponent"
                elif "each player" in target_desc: target_specifier = "each_player"
                created_effect = DrawCardEffect(count, target=target_specifier) # Pass 'x' or number

            # Gain Life
            elif re.search(r"(?:target player|you)?\s*\b(gain(?:s)?)\b\s+(\d+|x)\s+life", clause_lower):
                amount_str_match = re.search(r"gain(?:s)?\s+(\d+|x)\s+life", clause_lower)
                if amount_str_match: # Check if match found before accessing group
                     amount_str = amount_str_match.group(1)
                     # Pass X through
                     amount = 'x' if amount_str == 'x' else text_to_number(amount_str)
                     target_desc = EffectFactory._extract_target_description(clause_lower) or "controller"
                     target_specifier = "controller"
                     if "target player" in target_desc: target_specifier = "target_player"
                     elif "opponent" in target_desc: target_specifier = "opponent"
                     elif "each player" in target_desc: target_specifier = "each_player"
                     created_effect = GainLifeEffect(amount, target=target_specifier) # Pass 'x' or number

            # Damage
            elif re.search(r"\b(deals?)\b.*\bdamage\b", clause_lower):
                amount_match = re.search(r"deals?\s+(\d+|x)\s+damage", clause_lower)
                amount = 1 # Default if not specified
                if amount_match:
                    amount_str = amount_match.group(1)
                    amount = 'x' if amount_str == 'x' else text_to_number(amount_str)
                target_desc = EffectFactory._extract_target_description(clause_lower) or "any target" # Changed default
                target_type = "any target" # Default
                if "creature or player" in target_desc or "any target" in target_desc: target_type="any target"
                elif "creature" in target_desc: target_type="creature"
                elif "player" in target_desc or "opponent" in target_desc: target_type="player"
                elif "planeswalker" in target_desc: target_type="planeswalker"
                elif "battle" in target_desc: target_type="battle"
                elif "each opponent" in target_desc: target_type="each opponent"
                elif "each creature" in target_desc: target_type="each creature"
                elif "each player" in target_desc: target_type="each player" # Added
                created_effect = DamageEffect(amount, target_type=target_type) # Pass 'x' or number

            # Destroy
            elif re.search(r"\b(destroy(?:s)?)\b\s+(target|all|each)", clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent" # Default if specific target word used
                 target_type = "permanent"
                 # Normalize the target description slightly for easier checks
                 norm_target_desc = target_desc.replace('-',' ')
                 if "creature" in norm_target_desc: target_type = "creature"
                 elif "artifact" in norm_target_desc: target_type = "artifact"
                 elif "enchantment" in norm_target_desc: target_type = "enchantment"
                 elif "land" in norm_target_desc: target_type = "land"
                 elif "nonland permanent" in norm_target_desc: target_type = "nonland permanent"
                 elif "planeswalker" in norm_target_desc: target_type = "planeswalker" # Added
                 # Handle "all X" / "each X" types
                 if re.search(r"\b(all|each)\s+creatures?\b", clause_lower): target_type = "all creatures"
                 elif re.search(r"\b(all|each)\s+permanents?\b", clause_lower): target_type = "all permanents"
                 elif re.search(r"\b(all|each)\s+artifacts?\b", clause_lower): target_type = "all artifacts"
                 elif re.search(r"\b(all|each)\s+enchantments?\b", clause_lower): target_type = "all enchantments"
                 elif re.search(r"\b(all|each)\s+lands?\b", clause_lower): target_type = "all lands"
                 elif re.search(r"\b(all|each)\s+planeswalkers?\b", clause_lower): target_type = "all planeswalkers"
                 created_effect = DestroyEffect(target_type=target_type)

            # Exile
            elif re.search(r"\b(exile(?:s)?)\b\s+(target|all|each)", clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                 target_type = "permanent"
                 norm_target_desc = target_desc.replace('-',' ')
                 # Add specific type checks similar to Destroy
                 if "creature" in norm_target_desc: target_type = "creature"
                 elif "artifact" in norm_target_desc: target_type = "artifact"
                 elif "enchantment" in norm_target_desc: target_type = "enchantment"
                 elif "land" in norm_target_desc: target_type = "land"
                 elif "planeswalker" in norm_target_desc: target_type = "planeswalker"
                 elif "card" in norm_target_desc: target_type = "card" # Card in other zones
                 elif "spell" in norm_target_desc: target_type = "spell" # Stack target
                 # Handle "all/each" variations
                 if re.search(r"\b(all|each)\s+creatures?\b", clause_lower): target_type = "all creatures"
                 # ... add other "all X" / "each X" types if needed for exile ...
                 zone_match = re.search(r"from (?:the |a |your |an opponent's )?(\w+)", clause_lower)
                 zone = zone_match.group(1) if zone_match else "battlefield"
                 created_effect = ExileEffect(target_type=target_type, zone=zone)

            # Create Token
            elif re.search(r"\b(create(?:s)?)\b", clause_lower) and "token" in clause_lower:
                 count_match = re.search(r"create(?:s)?\s+(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+", clause_lower)
                 count = text_to_number(count_match.group(1)) if count_match else 1
                 pt_match = re.search(r"(\d+)/(\d+)", clause_lower)
                 power, toughness = (safe_int(pt_match.group(1)), safe_int(pt_match.group(2))) if pt_match else (1, 1)

                 # Improved type parsing - look for P/T, then colors/keywords, then base type (Creature/Artifact Creature/...) then name
                 # Example: Create two 1/1 white Soldier creature tokens.
                 # Example: Create a 0/0 black Germ creature token with lifelink.
                 # Example: Create a Treasure token. (It's an artifact...)
                 type_regex = r"(\d+/\d+|\w+)\s+((?:[a-z]+\s+)*)?((?:[A-Za-z\s\-]+))\s+token" # Complex regex needs careful building
                 # Simpler approach: Find keywords first, then try to extract P/T, colors, and name/types
                 keywords = []
                 kw_match = re.search(r"with ([\w\s,]+)", clause_lower)
                 if kw_match:
                     kw_candidates = [k.strip() for k in kw_match.group(1).split(',') if k.strip()]
                     # Validate against known keywords if possible, or just store text
                     keywords = [k.capitalize() for k in kw_candidates]

                 colors = []
                 known_colors = ["white", "blue", "black", "red", "green", "colorless"]
                 color_pattern = r'\b(' + '|'.join(known_colors) + r')\b'
                 color_matches = re.findall(color_pattern, clause_lower)
                 if color_matches: colors = [c.capitalize() for c in color_matches]
                 if not colors: # Infer from mana cost if token has one (rare)
                      pass

                 # Extract creature type/name - This is the hardest part generically
                 token_name_type = "Creature" # Default
                 # Remove count, p/t, colors, keywords text to isolate name/type text
                 text_for_type = clause_lower
                 if count_match: text_for_type = text_for_type.replace(count_match.group(0), "")
                 if pt_match: text_for_type = text_for_type.replace(pt_match.group(0), "")
                 if kw_match: text_for_type = text_for_type.replace(kw_match.group(0), "")
                 for color_word in known_colors: text_for_type = text_for_type.replace(color_word,"")
                 # Try to find "creature token named X", or "X creature token", or "TYPE token"
                 named_match = re.search(r"token(?:s)?\s+named\s+([\w\s]+)", text_for_type)
                 if named_match: token_name_type = named_match.group(1).strip().capitalize()
                 else:
                     type_match = re.search(r"(\w+)\s+(artifact\s+)?(creature|artifact|treasure|food|clue)\s+token", text_for_type) # Basic common types
                     if type_match:
                          prefix = type_match.group(1)
                          base = type_match.group(3)
                          if prefix and prefix not in ['a','an','the']: token_name_type = prefix.capitalize()
                          elif base: token_name_type = base.capitalize()
                          # Refine: Might need better identification based on position relative to P/T etc.

                 # Determine final type line components
                 is_legendary = "legendary" in clause_lower
                 # ... construct full token_data dict for the game state ...
                 # Using simplified CreateTokenEffect for now
                 created_effect = CreateTokenEffect(power, toughness, token_name_type, count, keywords, colors=colors, is_legendary=is_legendary)


            # Buff (+X/+Y)
            elif re.search(r"(?:target |creatures you control|each creature\b)?\s*(get(?:s)?|has)\b\s*([+\-]\d+)/([+\-]\d+)", clause_lower):
                match = re.search(r"(get(?:s)?|has)\s+([+\-]\d+)/([+\-]\d+)", clause_lower)
                if match: # Check match exists
                    p_mod, t_mod = safe_int(match.group(2)), safe_int(match.group(3))
                    duration = "end_of_turn" if "until end of turn" in clause_lower else "permanent"
                    target_desc = EffectFactory._extract_target_description(clause_lower) or "creatures you control"
                    target_type = "creature" # Default
                    # Refine target_type based on target_desc
                    if "target creature" in target_desc: target_type = "target creature"
                    elif "creatures you control" in target_desc: target_type = "creatures you control"
                    elif "each creature" in target_desc and "target" not in clause_lower: target_type = "each creature" # Target all
                    elif "creatures opponent controls" in target_desc: target_type = "creatures opponent controls"
                    # Add more specific permanent types if needed
                    created_effect = BuffEffect(p_mod, t_mod, duration=duration, target_type=target_type)

            # Tap
            elif re.search(r"\b(tap(?:s)?)\b\s+target", clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                 target_type = "permanent" # Refine based on desc
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "land" in target_desc: target_type = "land"
                 created_effect = TapEffect(target_type=target_type)

            # Untap
            elif re.search(r"\b(untap(?:s)?)\b\s+target", clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                 target_type = "permanent" # Refine based on desc
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "land" in target_desc: target_type = "land"
                 created_effect = UntapEffect(target_type=target_type)

            # Add Counters
            elif re.search(r"\bput(?:s)?\b.*?\bcounter", clause_lower):
                 count_match = re.search(r"put\s+(a|an|one|two|three|four|five|six|seven|eight|nine|ten|x|\d+)", clause_lower) # Include 'x'
                 count = 1 # Default
                 if count_match:
                      count_str = count_match.group(1)
                      count = 'x' if count_str == 'x' else text_to_number(count_str)

                 # Capture counter type more broadly, including words like 'loyalty', 'charge', 'poison'
                 # Allow +/- before digits/slash
                 type_match = re.search(r"([+\-]\d+/[+\-]\d+)\s+counter|\b(loyalty|charge|poison|time|fade|level|quest|storage|shield|\w+)\s+counter", clause_lower) # Match +/-N/+/-N or named type
                 counter_type = "+1/+1" # Default
                 if type_match:
                     if type_match.group(1): # Found P/T modifier type like "+1/+1" or "-1/-1"
                         counter_type = type_match.group(1) # Keep the raw +/- string
                     elif type_match.group(2): # Found named counter type
                         counter_type = type_match.group(2).lower()

                 # Determine target
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "self" # Default to self if no target word
                 target_type = "self" # Default if self or not targeted
                 if "target" in target_desc:
                     # Use a mapping or series of checks to determine best fit based on keywords in desc
                     if "creature" in target_desc: target_type = "target creature"
                     elif "artifact" in target_desc: target_type = "target artifact"
                     elif "enchantment" in target_desc: target_type = "target enchantment"
                     elif "planeswalker" in target_desc: target_type = "target planeswalker"
                     elif "player" in target_desc: target_type = "target player" # For poison, etc.
                     elif "land" in target_desc: target_type = "target land"
                     elif "battle" in target_desc: target_type = "target battle" # Battles have defense counters
                     elif "permanent" in target_desc: target_type = "target permanent"
                     else: target_type = "target permanent" # Fallback if type unclear but target specified
                 # Handle "each" targets
                 elif re.search(r"\b(each|all)\s+creatures? you control\b", clause_lower): target_type = "each creature you control"
                 elif re.search(r"\b(each|all)\s+creatures? opponent controls\b", clause_lower): target_type = "each creature opponent controls"
                 elif re.search(r"\b(each|all)\s+creatures?\b", clause_lower): target_type = "each creature"
                 elif re.search(r"\b(each|all)\s+opponents?\b", clause_lower): target_type = "each opponent"
                 elif re.search(r"\b(each|all)\s+players?\b", clause_lower): target_type = "each player"

                 created_effect = AddCountersEffect(counter_type, count, target_type=target_type) # Pass 'x' or number

            # Counter Spell
            elif re.search(r"\bcounter(?:s)?\b\s+target", clause_lower):
                target_desc = EffectFactory._extract_target_description(clause_lower) or "spell"
                target_type = "spell" # Default
                if "creature spell" in target_desc: target_type = "creature spell"
                elif "noncreature spell" in target_desc: target_type = "noncreature spell"
                elif "activated ability" in target_desc: target_type = "activated ability"
                elif "triggered ability" in target_desc: target_type = "triggered ability"
                elif "ability" in target_desc: target_type = "ability" # Generic ability
                created_effect = CounterSpellEffect(target_type=target_type)

            # Discard
            elif re.search(r"\bdiscard(?:s)?\b", clause_lower):
                 count = 1
                 # Check for specific count, "all", or "x"
                 count_match = re.search(r"discard\s+(a|an|one|two|three|four|five|six|seven|eight|nine|ten|all|x)\s+cards?", clause_lower)
                 is_random = "at random" in clause_lower
                 if count_match:
                     count_str = count_match.group(1)
                     count = -1 if count_str == "all" else ('x' if count_str == 'x' else text_to_number(count_str))

                 target_desc = EffectFactory._extract_target_description(clause_lower) or "target_player" # Default target
                 target_specifier = "target_player"
                 if "you discard" in clause_lower: target_specifier = "controller"
                 elif "opponent discards" in clause_lower or "each opponent discards" in clause_lower: target_specifier = "opponent"
                 elif "each player discards" in clause_lower: target_specifier = "each_player"
                 created_effect = DiscardEffect(count, target=target_specifier, is_random=is_random) # Pass 'x', -1, or number

            # Mill
            elif re.search(r"\bmill(?:s)?\b", clause_lower):
                count = 1
                count_match = re.search(r"mill(?:s)?\s+(\d+|x)\s+cards?", clause_lower) # Include 'x'
                if count_match:
                    count_str = count_match.group(1)
                    count = 'x' if count_str == 'x' else text_to_number(count_str)

                target_desc = EffectFactory._extract_target_description(clause_lower) or "target_player"
                target_specifier = "target_player"
                if "you mill" in clause_lower: target_specifier = "controller"
                elif "opponent mills" in clause_lower or "each opponent mills" in clause_lower: target_specifier = "opponent"
                elif "each player mills" in clause_lower: target_specifier = "each player" # Added
                created_effect = MillEffect(count, target=target_specifier) # Pass 'x' or number

            # Return to Hand (Bounce)
            elif re.search(r"\breturn(?:s)?\b", clause_lower) and ("to (?:its|their) owner's hand" in clause_lower or "to your hand" in clause_lower):
                target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                target_type = "permanent"
                zone = "battlefield" # Default zone
                if "target card" in target_desc: target_type = "card" # Could be from GY etc.
                elif "target creature" in target_desc: target_type = "creature"
                elif "target artifact" in target_desc: target_type = "artifact"
                elif "target enchantment" in target_desc: target_type = "enchantment"
                elif "target land" in target_desc: target_type = "land"
                elif "target planeswalker" in target_desc: target_type = "planeswalker"
                # Check originating zone
                if "from your graveyard" in clause_lower: zone = "graveyard"; target_type="card"
                elif "from exile" in clause_lower: zone = "exile"; target_type="card"
                # Add other zones
                created_effect = ReturnToHandEffect(target_type=target_type, zone=zone)

            # Search Library
            elif re.search(r"\bsearch(?:es)?\s+your library", clause_lower):
                 count = 1
                 count_match = re.search(r"search.*? for (?:up to )?(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s", clause_lower)
                 if count_match: count = text_to_number(count_match.group(1))

                 # Extract card type criteria more robustly
                 type_match = re.search(r"for (?:up to \w+ )?(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)?\s*((?:[\w\-]+\s+)*[\w\-]+(?:\s+with\s+[\w\s]+)?)?\s*card", clause_lower) # Allow "with X"
                 search_type = "any" # Default
                 if type_match and type_match.group(1): # Ensure group 1 matched
                      search_type = type_match.group(1).strip()
                      # Normalize common types (could use a set/dict for better matching)
                      # Simple check for known types
                      known_simple_types = ["basic land", "creature", "artifact", "enchantment", "land", "instant", "sorcery", "planeswalker", "battle", "legendary", "card"]
                      if not any(kt in search_type for kt in known_simple_types): search_type = "any" # Reset if parse seems off

                 # Determine destination
                 dest_match = re.search(r"put (?:it|that card|them) (?:onto|into|in) (?:the|your) (\w+)", clause_lower)
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

            # Scry
            elif re.search(r"\bscry\b", clause_lower):
                match = re.search(r"scry (\d+|x)\b", clause_lower)
                count = 1 # Default Scry 1
                if match:
                     count_str = match.group(1)
                     count = 'x' if count_str == 'x' else text_to_number(count_str)
                created_effect = ScryEffect(count) # Pass 'x' or number

            # Surveil
            elif re.search(r"\bsurveil\b", clause_lower):
                 match = re.search(r"surveil (\d+|x)\b", clause_lower)
                 count = 1 # Default Surveil 1
                 if match:
                      count_str = match.group(1)
                      count = 'x' if count_str == 'x' else text_to_number(count_str)
                 created_effect = SurveilEffect(count) # Pass 'x' or number

            # Life Drain (Checked earlier with em dash fix)

            # Copy Spell
            elif re.search(r"\bcopy target\b.*\bspell\b", clause_lower):
                 target_type = "spell"
                 if "instant or sorcery spell" in clause_lower: target_type = "instant or sorcery spell"
                 elif "instant spell" in clause_lower: target_type = "instant"
                 elif "sorcery spell" in clause_lower: target_type = "sorcery"
                 elif "creature spell" in clause_lower: target_type = "creature spell"
                 # Add other types
                 new_targets = "choose new targets" in clause_lower
                 created_effect = CopySpellEffect(target_type=target_type, new_targets=new_targets)

            # Transform
            elif re.search(r"\btransform\b", clause_lower):
                 created_effect = TransformEffect()

            # Fight
            elif re.search(r"\bfights?\b.*?\btarget\b", clause_lower):
                 target_type = "creature" # Default
                 match_target = re.search(r"target ([\w\s]+)", clause_lower)
                 if match_target:
                      desc = match_target.group(1).strip()
                      if "creature" in desc: target_type="creature"
                      # Add other types if creatures can fight non-creatures (rare)
                 created_effect = FightEffect(target_type=target_type)

            # --- Fallback and Effect Addition ---
            if created_effect:
                effects.append(created_effect)
            else:
                 # Add generic effect if specific parsing fails for this clause
                 effect_keywords = ["destroy", "exile", "draw", "gain", "lose", "counter", "create", "search", "tap", "untap", "put", "scry", "surveil", "fight", "transform", "copy", "mill", "discard", "return"]
                 if clause_clean and any(kw in clause_lower for kw in effect_keywords): # Check clean text and lower
                     logging.debug(f"Adding generic AbilityEffect for clause: '{clause_clean}'")
                     effects.append(AbilityEffect(clause_clean)) # Store original case text

        # Final fallback if no clauses yielded effects
        if not effects and effect_text:
            logging.warning(f"Could not parse effect text into specific effects: '{effect_text}'. Adding as generic effect.")
            effects.append(AbilityEffect(effect_text)) # Store original case text

        return effects