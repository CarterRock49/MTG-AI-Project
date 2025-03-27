"""Utility functions for ability processing."""
import logging
import re
from .ability_types import AbilityEffect, DrawCardEffect, GainLifeEffect, DamageEffect, \
    CounterSpellEffect, CreateTokenEffect, DestroyEffect, ExileEffect, \
    DiscardEffect, MillEffect
from .ability_types import TapEffect, UntapEffect, BuffEffect, SearchLibraryEffect # Add imports for new types

def is_beneficial_effect(effect_text):
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
    
    if text.lower() in text_to_num:
        return text_to_num[text.lower()]
    
    try:
        return int(text)
    except ValueError:
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

def resolve_targeting(game_state, card_id, controller, effect_text):
    """Enhanced targeting resolution that uses TargetingSystem if available, otherwise falls back to simple targeting."""
    # Try to use TargetingSystem if available
    if hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
        return game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
            card_id, effect_text, controller)
    
    # Fall back to simple targeting
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
        match = re.search(r"target (.*?)(?:\.|$|,|:| gains| gets| has| deals| draws| is)", effect_text)
        if match:
            return match.group(1).strip()
        elif "each opponent" in effect_text:
            return "each opponent"
        elif "each player" in effect_text:
            return "each player"
        elif "controller" in effect_text: # Less reliable, 'creatures you control' etc.
             return "controller" # Very rough guess
        return None # No target description found

    @staticmethod
    def create_effects(effect_text, targets=None):
        """Create appropriate AbilityEffect objects based on the effect text."""
        if not effect_text:
            return []

        effects = []
        effect_text_lower = effect_text.lower()

        # --- Improved Effect Parsing ---
        # Split text by sentences/clauses to handle multiple effects
        clauses = re.split(r'[.;](?=\s*(?:then|also|next|repeat|$)|\s*\w)', effect_text_lower)

        parsed_indices = set() # Keep track of parts of the string already parsed

        for clause_index, clause in enumerate(clauses):
            clause = clause.strip()
            if not clause:
                 continue

            created_effect = False
            start_pos = effect_text_lower.find(clause, max(parsed_indices) if parsed_indices else 0)
            if start_pos == -1: continue # Should not happen if clause came from split
            end_pos = start_pos + len(clause)

            # Check if this clause section has already been parsed by a broader match
            if any(start <= start_pos and end >= end_pos for start, end in parsed_indices):
                 continue

            # 1. Draw Cards
            match = re.search(r"draw (\d+|a|an|\w+) cards?", clause)
            if match:
                count = match.group(1)
                target_desc = EffectFactory._extract_target_description(clause) or "controller"
                effects.append(DrawCardEffect(text_to_number(count), target=target_desc))
                parsed_indices.add((start_pos, end_pos))
                created_effect = True

            # 2. Gain Life
            match = re.search(r"gain (\d+|a|an|\w+) life", clause)
            if match and not created_effect:
                amount = match.group(1)
                target_desc = EffectFactory._extract_target_description(clause) or "controller"
                effects.append(GainLifeEffect(text_to_number(amount), target=target_desc))
                parsed_indices.add((start_pos, end_pos))
                created_effect = True

            # 3. Damage
            match = re.search(r"deal[s]?\s+(\d+|\w+|x)\s+damage\s+to\s+(any target|target\s+(.*?))(?=\.|$|,| deals| takes)", clause)
            if match and not created_effect:
                amount_str = match.group(1)
                target_desc_full = match.group(2).strip() # "any target" or "target ..."

                amount = text_to_number(amount_str) if amount_str != 'x' else 1 # Use X=1 default

                target_type = "any" # Default
                if target_desc_full == "any target":
                     target_type = "any"
                elif "target" in target_desc_full:
                     target_specific = match.group(3).strip() if match.group(3) else ""
                     if "creature" in target_specific: target_type = "creature"
                     elif "player" in target_specific or "opponent" in target_specific: target_type = "player"
                     elif "planeswalker" in target_specific: target_type = "planeswalker"
                     elif "battle" in target_specific: target_type = "battle" # Add battle type
                     # Allow falling back to "any" if specific type isn't clear from desc
                else: # E.g., "deals damage to you", "deals damage to each opponent"
                     if "you" in target_desc_full or "controller" in target_desc_full:
                         target_type = "player" # Needs context to target specific player
                     elif "each opponent" in target_desc_full or "opponent" in target_desc_full:
                         target_type = "player" # Needs context to target specific player
                     elif "each creature" in target_desc_full:
                         target_type = "creature" # Needs context

                # Use target_type in DamageEffect constructor
                effects.append(DamageEffect(amount, target_type=target_type))
                parsed_indices.add((start_pos, end_pos))
                created_effect = True

            # 4. Counter Spell
            match = re.search(r"counter target (.*?)(?=\.|$|,| unless)", clause)
            if match and not created_effect:
                target_spell_type = match.group(1).strip()
                effects.append(CounterSpellEffect(target_spell_type))
                parsed_indices.add((start_pos, end_pos))
                created_effect = True

            # 5. Create Token (Improved Parsing)
            # Example: create two 1/1 white Soldier creature tokens with vigilance
            match = re.search(r"create (\w+|a|an) (?:(\d+/\d+)\s+)?(?:([\w\s]+?)\s+)?(artifact|creature|enchantment)?\s*tokens?(?: with (.*?))?(?=\.|$|,)", clause)
            if match and not created_effect:
                 count_str, pt_str, colors_and_type, main_type, keywords_str = match.groups()
                 count = text_to_number(count_str) if count_str not in ('a', 'an') else 1
                 power, toughness = (1, 1)
                 if pt_str:
                     p, t = pt_str.split('/')
                     power, toughness = int(p), int(t)

                 creature_type = "Token"
                 if colors_and_type:
                     parts = colors_and_type.strip().split()
                     # Filter out colors, assume rest is type
                     colors = {'white','blue','black','red','green'}
                     type_parts = [p for p in parts if p not in colors]
                     if type_parts: creature_type = " ".join(type_parts).capitalize()

                 keywords = keywords_str.split(',') if keywords_str else []
                 keywords = [k.strip() for k in keywords if k.strip()]

                 effects.append(CreateTokenEffect(power, toughness, creature_type, count, keywords))
                 parsed_indices.add((start_pos, end_pos))
                 created_effect = True

            # 6. Destroy
            match = re.search(r"destroy target (.*?)(?=\.|$|,| unless)", clause)
            if match and not created_effect:
                target_desc = match.group(1).strip()
                effects.append(DestroyEffect(target_desc))
                parsed_indices.add((start_pos, end_pos))
                created_effect = True

            # 7. Exile
            match = re.search(r"exile target (.*?)(?: from (.*?))?(?=\.|$|,| unless)", clause)
            if match and not created_effect:
                target_desc = match.group(1).strip()
                zone = match.group(2).strip() if match.group(2) else "battlefield"
                effects.append(ExileEffect(target_desc, zone=zone))
                parsed_indices.add((start_pos, end_pos))
                created_effect = True

            # 8. Discard
            match = re.search(r"(target player|each opponent) discards? (\d+|\w+|a|an) cards?", clause)
            if match and not created_effect:
                 target_player_desc = match.group(1)
                 count_str = match.group(2)
                 count = text_to_number(count_str) if count_str not in ('a', 'an') else 1
                 target_who = "opponent" if "opponent" in target_player_desc else "target_player"
                 effects.append(DiscardEffect(count=count, target=target_who))
                 parsed_indices.add((start_pos, end_pos))
                 created_effect = True

            # 9. Mill
            match = re.search(r"(?:target player |each opponent |player )?(?:mills?|put(?:s)? the top) (\d+|\w+) cards?", clause)
            if match and not created_effect:
                 count_str = match.group(1)
                 target_desc = EffectFactory._extract_target_description(clause) or "opponent" # Default mill opponent
                 effects.append(MillEffect(text_to_number(count_str), target=target_desc))
                 parsed_indices.add((start_pos, end_pos))
                 created_effect = True

            # 10. Tap/Untap
            match = re.search(r"(tap|untap) target (.*?)(?=\.|$|,)", clause)
            if match and not created_effect:
                 action = match.group(1)
                 target_desc = match.group(2).strip()
                 if action == "tap":
                     effects.append(TapEffect(target_desc))
                 else:
                     effects.append(UntapEffect(target_desc))
                 parsed_indices.add((start_pos, end_pos))
                 created_effect = True

            # 11. Buff/Debuff (Temporary)
            match = re.search(r"target creature gets ([+\-]\d+)/([+\-]\d+) until end of turn", clause)
            if match and not created_effect:
                p_mod = int(match.group(1))
                t_mod = int(match.group(2))
                effects.append(BuffEffect(p_mod, t_mod))
                parsed_indices.add((start_pos, end_pos))
                created_effect = True

            # 12. Search Library
            match = re.search(r"search your library for (?:up to (\w+|a|an) )?(.*?) cards?", clause)
            if match and not created_effect:
                 count_str = match.group(1) or "a"
                 criteria = match.group(2).replace(" card","").strip()
                 count = text_to_number(count_str) if count_str not in ('a', 'an') else 1
                 # Destination? Needs parsing. Default 'hand'.
                 destination = "hand"
                 if "put onto the battlefield" in clause: destination = "battlefield"
                 elif "put into your graveyard" in clause: destination = "graveyard"

                 effects.append(SearchLibraryEffect(criteria, target="controller", destination=destination, count=count))
                 parsed_indices.add((start_pos, end_pos))
                 created_effect = True

            # Add other effects here...

        # If no specific effects were parsed for the whole text, add a generic one
        if not effects:
            logging.warning(f"Could not parse effect text into specific effects: '{effect_text}'")
            return [AbilityEffect(effect_text)]

        # If only parts were parsed, add generic effect for remaining parts
        if parsed_indices:
             # Find unparsed sections (this is complex, skipping for now)
             pass

        return effects
    