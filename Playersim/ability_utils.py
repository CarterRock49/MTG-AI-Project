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
    logging.warning(f"Could not confidently determine benefit of '{effect_text}'. Defaulting to False.")
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


    # --- Delayed trigger extraction (CR 603.7) ------------------------------
    # Only the "next <phase>" wording is a one-shot delayed trigger created by
    # a resolving spell/ability. Recurring wordings ("at the beginning of your
    # upkeep", "...of each end step") are triggered abilities of permanents,
    # parsed elsewhere, and must NOT match here.
    _DELAYED_PHASE = r"(end step|upkeep|end of combat|combat|cleanup(?: step)?|main phase)"
    _DELAYED_LEADING = re.compile(
        r"^\s*at the beginning of (?:the |your )?next " + _DELAYED_PHASE
        + r"\s*,?\s*(.+?)\s*$",
        re.IGNORECASE)
    _DELAYED_TRAILING = re.compile(
        r"^\s*(.+?)\s+at the beginning of (?:the |your )?next " + _DELAYED_PHASE
        + r"\s*\.?\s*$",
        re.IGNORECASE)

    @staticmethod
    def _extract_delayed_triggers(effect_text):
        """Carve delayed-trigger sentences out of effect text (CR 603.7).

        Must run BEFORE clause splitting: the comma split would sever
        "At the beginning of the next end step" from its effect.

        Returns (delayed_effects, remaining_text) where delayed_effects is a
        list of DelayedTriggerEffect and remaining_text contains every
        sentence that is not a delayed trigger, to flow through the normal
        clause pipeline unchanged.
        """
        from .ability_types import DelayedTriggerEffect
        delayed = []
        kept = []
        for sentence in re.split(r"(?<=[.!;])\s+", effect_text.strip()):
            if not sentence.strip():
                continue
            # Reminder text is removed for matching only; the original
            # sentence is preserved if it is not a delayed trigger.
            probe = re.sub(r"\s*\([^()]*?\)\s*", " ", sentence).strip()
            m = EffectFactory._DELAYED_LEADING.match(probe)
            if m:
                phase_key, inner = m.group(1), m.group(2)
                delayed.append(DelayedTriggerEffect(inner, phase_key, full_text=probe))
                continue
            m = EffectFactory._DELAYED_TRAILING.match(probe)
            if m:
                inner, phase_key = m.group(1), m.group(2)
                delayed.append(DelayedTriggerEffect(inner, phase_key, full_text=probe))
                continue
            kept.append(sentence)
        return delayed, " ".join(kept)

    @staticmethod
    def create_effects(effect_text, targets=None, source_name=None): # targets arg currently unused here
        """
        Create appropriate AbilityEffect objects based on the effect text.
        Handles clause splitting including em dashes and various common MTG effects.
        """
        if not effect_text: return []

        effects = []

        # CR 603.7: pull out "at the beginning of the next <phase>" sentences
        # as DelayedTriggerEffect BEFORE clause splitting (see helper docstring).
        delayed_effects, effect_text = EffectFactory._extract_delayed_triggers(effect_text)
        effects.extend(delayed_effects)
        if not effect_text.strip(". "):
            return effects

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
            ScryEffect, SurveilEffect, CopySpellEffect, TransformEffect, FightEffect,
            ImpulseDrawEffect, LoseLifeEffect, GainKeywordEffect,
            SacrificeEffect, ReanimateEffect, AddManaEffect, ControlEffect,
            RegenerateEffect, DigEffect, PutOnLibraryEffect,
            ShuffleGraveyardEffect, PreventDamageEffect,
            AnimateLandEffect, RevealHandEffect)
        from .card import Card  # for ALL_KEYWORDS in the keyword-grant branch

        # --- Offspring ETB Trigger Detection (before standard token creation) ---
        offspring_trigger_pattern = re.compile(
            r"when this (?:creature|permanent|card|enters).*offspring cost was paid.*create a 1/1 token copy",
            re.IGNORECASE
        )

        for clause in processed_clauses:
            clause_clean = re.sub(r'\s*\([^()]*?\)\s*', ' ', clause).strip() # Basic reminder text removal
            clause_lower = clause_clean.lower()
            created_effect = None

            # --- Offspring ETB special handling ---
            if offspring_trigger_pattern.search(clause_lower):
                # Create a generic AbilityEffect but mark it as an offspring token effect
                effect = AbilityEffect(clause_clean)
                effect._is_offspring_token_effect = True
                # Attach a condition function to check context for offspring_cost_paid
                def offspring_condition(trigger_context):
                    return trigger_context.get('offspring_cost_paid', False)
                effect.offspring_condition = offspring_condition
                effects.append(effect)
                continue

            # Variable draw: "draw cards equal to the number of X".
            if re.search(r"draw\s+cards?\s+equal to the number of", clause_lower):
                cem = re.search(r"equal to the number of\s+(.+?)(?:\.|$)", clause_lower)
                expr = cem.group(1).strip() if cem else "creatures you control"
                td = EffectFactory._extract_target_description(clause_lower) or "controller"
                ts = "controller"
                if "target player" in td: ts = "target_player"
                elif "each player" in clause_lower: ts = "each_player"
                created_effect = DrawCardEffect(1, target=ts, count_expr=expr)
                effects.append(created_effect)
                continue

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

            # Variable life gain: "gain life equal to the number of X".
            elif re.search(r"gains?\s+life\s+equal to the number of", clause_lower):
                cem = re.search(r"equal to the number of\s+(.+?)(?:\.|$)", clause_lower)
                expr = cem.group(1).strip() if cem else "creatures you control"
                td = EffectFactory._extract_target_description(clause_lower) or "controller"
                ts = "controller"
                if "target player" in td: ts = "target_player"
                elif "each player" in clause_lower: ts = "each_player"
                created_effect = GainLifeEffect(0, target=ts, count_expr=expr)

            # Shuffle graveyard into library (graveyard hate / recursion).
            elif re.search(r"shuffle\s+(your|target player's|his or her)\s+graveyard\s+into\s+(your|their|his or her|that player's)\s+library", clause_lower):
                who = "controller"
                if "target player" in clause_lower: who = "target_player"
                elif "each player" in clause_lower: who = "each_player"
                created_effect = ShuffleGraveyardEffect(who=who)

            # Damage prevention (fog / prevention shields).
            elif "prevent" in clause_lower and "damage" in clause_lower:
                combat_only = "combat damage" in clause_lower
                amount = None
                nm = re.search(r"prevent the next\s+(\d+|x)\s+damage", clause_lower)
                if nm and nm.group(1) != 'x':
                    amount = int(nm.group(1))
                scope = "all"
                if "to you" in clause_lower: scope = "to_you"
                elif "to any target" in clause_lower or "to target" in clause_lower: scope = "target"
                created_effect = PreventDamageEffect(amount=amount, combat_only=combat_only, target_scope=scope)

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


            # Reanimation: "return ... from (your/a) graveyard to the battlefield".
            # Must come before the bounce branch (which handles "to hand").
            elif re.search(r"return\s+.*from\s+(?:your|a|target player's)?\s*graveyard\s+to\s+the\s+battlefield", clause_lower):
                tt = "creature"
                if "artifact" in clause_lower: tt = "artifact"
                elif "enchantment" in clause_lower: tt = "enchantment"
                elif "permanent" in clause_lower: tt = "permanent"
                elif "card" in clause_lower and "creature" not in clause_lower: tt = "card"
                tapped = "tapped" in clause_lower
                created_effect = ReanimateEffect(target_type=tt, from_zone="graveyard", enters_tapped=tapped)

            # Sacrifice / Edict: "sacrifice a <type>", "target player sacrifices
            # a <type>", "each player/opponent sacrifices a <type>".
            elif re.search(r"sacrifices?\s+(?:a|an|one|two|\d+)\s+(\w+)", clause_lower):
                m = re.search(r"sacrifices?\s+(a|an|one|two|three|\d+)\s+(\w+)", clause_lower)
                ptype = m.group(2) if m else "creature"
                cnt_raw = m.group(1) if m else "a"
                if cnt_raw in ("a", "an", "one"): cnt = 1
                elif cnt_raw.isdigit(): cnt = int(cnt_raw)
                else: cnt = text_to_number(cnt_raw)
                if not isinstance(cnt, int) or cnt <= 0: cnt = 1
                if "each opponent" in clause_lower or "each other player" in clause_lower:
                    who = "each_opponent"
                elif "each player" in clause_lower:
                    who = "each_player"
                elif "target player" in clause_lower or "that player" in clause_lower:
                    who = "target_player"
                else:
                    who = "controller"
                created_effect = SacrificeEffect(permanent_type=ptype, who=who, count=cnt)

            # Life loss: "target player loses N life" / "each opponent loses N life"
            elif re.search(r"loses?\s+(\d+|x)\s+life", clause_lower):
                amt_m = re.search(r"loses?\s+(\d+|x)\s+life", clause_lower)
                amt = amt_m.group(1) if amt_m else "1"
                amt = int(amt) if amt.isdigit() else 'x'
                if "each opponent" in clause_lower or "each other player" in clause_lower:
                    lt = "opponent"
                elif "each player" in clause_lower:
                    lt = "each_player"
                elif "you lose" in clause_lower:
                    lt = "controller"
                else:
                    lt = "target_player"
                created_effect = LoseLifeEffect(amt, target=lt)

            # Distribute +1/+1 counters among target creatures.
            elif re.search(r"distribute\s+(\w+|\d+)?\s*\+1/\+1 counters", clause_lower):
                num_m = re.search(r"distribute\s+(\w+|\d+)", clause_lower)
                n = 1
                if num_m and num_m.group(1):
                    n = int(num_m.group(1)) if num_m.group(1).isdigit() else text_to_number(num_m.group(1))
                if not isinstance(n, int) or n <= 0: n = 1
                # v1: with a single chosen target all counters land there; the
                # multi-target split is an agent-choice item (Tier 3).
                created_effect = AddCountersEffect("+1/+1", count=n, target_type="target creature")

            # Combat restrictions: "can't attack/block". Modeled as granted
            # 'cant_attack'/'cant_block' abilities on the target (same layer-6
            # add_ability path the static parser uses). July 2026 parser expansion.
            elif "can't block" in clause_lower or "cant block" in clause_lower:
                dur = "end_of_turn" if ("this turn" in clause_lower or "until end of turn" in clause_lower) else "permanent"
                gt = "target creature" if "target" in clause_lower else "self"
                created_effect = GainKeywordEffect("cant_block", target_type=gt, duration=dur)
            elif "can't attack" in clause_lower or "cant attack" in clause_lower:
                dur = "end_of_turn" if ("this turn" in clause_lower or "until end of turn" in clause_lower) else "permanent"
                gt = "target creature" if "target" in clause_lower else "self"
                created_effect = GainKeywordEffect("cant_attack", target_type=gt, duration=dur)

            # Keyword grant: "target creature gains <keyword> [until end of turn]".
            # Must come before the Buff branch (which only handles +N/+N) and
            # only fire when there is NO P/T change in the clause.
            elif re.search(r"(gains?|has)\s+(\w[\w'\- ]*?)(?:\s+until end of turn)?\s*\.?$", clause_lower) \
                    and not re.search(r"[+\-]\d+/[+\-]\d+", clause_lower) \
                    and any(re.search(rf"(gains?|has)\s+{re.escape(kw)}\b", clause_lower) for kw in Card.ALL_KEYWORDS):
                granted = next(kw for kw in Card.ALL_KEYWORDS
                               if re.search(rf"(gains?|has)\s+{re.escape(kw)}\b", clause_lower))
                duration = "end_of_turn" if "until end of turn" in clause_lower else "permanent"
                if "creatures you control" in clause_lower:
                    gt = "creatures you control"
                elif "target" in clause_lower:
                    gt = "target creature"
                else:
                    gt = "self"
                created_effect = GainKeywordEffect(granted, target_type=gt, duration=duration)

            # Variable pump: "gets +X/+X ... where X is the number of Y". The
            # clause splitter severs the "where X is..." part at the comma
            # (same disease as delayed triggers), so read the count expression
            # from the FULL effect_text, not just this clause.
            elif re.search(r"get(?:s)?\s+\+x/\+x", clause_lower) and "where x is the number of" in effect_text.lower():
                cem = re.search(r"where x is the number of\s+(.+?)(?:\.|$)", effect_text.lower())
                expr = cem.group(1).strip() if cem else "creatures you control"
                duration = "end_of_turn" if "until end of turn" in clause_lower else "permanent"
                tt = "target creature" if "target" in clause_lower else "creatures you control"
                created_effect = BuffEffect(0, 0, target_type=tt, duration=duration, count_expr=expr)

            # Animate land: "target land becomes a N/N creature".
            elif re.search(r"target\s+land\s+becomes?\s+a\s+(\d+)/(\d+)\s+creature", clause_lower) \
                    or re.search(r"becomes?\s+a\s+(\d+)/(\d+)\s+creature", clause_lower) and "land" in clause_lower:
                am = re.search(r"becomes?\s+a\s+(\d+)/(\d+)\s+creature", clause_lower)
                p = int(am.group(1)) if am else 0
                t = int(am.group(2)) if am else 0
                duration = "end_of_turn" if "until end of turn" in clause_lower else "permanent"
                keep = "still a land" in clause_lower or "in addition" in clause_lower
                created_effect = AnimateLandEffect(power=p, toughness=t, duration=duration, keep_types=keep)

            # Reveal hand: "target player reveals their hand".
            elif re.search(r"(target player|each player|you)\s+reveals?\s+(their|his or her|your)\s+hand", clause_lower) \
                    and "you choose" not in clause_lower and "discards" not in clause_lower:
                who = "target_player"
                if "each player" in clause_lower: who = "each_player"
                elif clause_lower.strip().startswith("you reveal") or "you reveal your hand" in clause_lower: who = "controller"
                created_effect = RevealHandEffect(who=who)

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
            # Ritual / add-mana SPELL effect: "Add {B}{B}{B}", "add N mana of
            # any color". (Mana ACTIVATED abilities on permanents are handled by
            # ManaAbility, not here.) July 2026 parser expansion.
            elif re.search(r"^\s*add\s+(\{[wubrgc0-9/p]+\}|\w+ mana)", clause_lower):
                mana_syms = re.findall(r"\{([wubrgc])\}", clause_lower)
                generic = re.findall(r"\{(\d+)\}", clause_lower)
                mana_dict = {}
                for s in mana_syms:
                    mana_dict[s.upper()] = mana_dict.get(s.upper(), 0) + 1
                for g in generic:
                    mana_dict["C"] = mana_dict.get("C", 0) + int(g)
                any_count = 0
                any_m = re.search(r"add\s+(\w+)\s+mana of any (?:one )?color", clause_lower)
                if any_m:
                    w = any_m.group(1)
                    any_count = int(w) if w.isdigit() else text_to_number(w)
                    if not isinstance(any_count, int) or any_count <= 0: any_count = 1
                if mana_dict or any_count:
                    created_effect = AddManaEffect(mana_dict=mana_dict, any_color_count=any_count)

            # Gain control of target permanent (Threaten / Control Magic).
            elif re.search(r"gains?\s+control\s+of\s+target", clause_lower):
                ct = "creature"
                if "artifact" in clause_lower: ct = "artifact"
                elif "enchantment" in clause_lower: ct = "enchantment"
                elif "permanent" in clause_lower: ct = "permanent"
                elif "land" in clause_lower: ct = "land"
                dur = "end_of_turn" if ("until end of turn" in clause_lower or "this turn" in clause_lower) else "permanent"
                created_effect = ControlEffect(target_type=ct, duration=dur)

            # Regenerate target creature.
            elif re.search(r"regenerate\s+(target\s+)?", clause_lower) and "regenerate" in clause_lower:
                ct = "creature"
                if "target" not in clause_lower and ("this" in clause_lower or "it" in clause_lower):
                    created_effect = RegenerateEffect(target_type=ct)
                else:
                    created_effect = RegenerateEffect(target_type=ct)

            # Mass tap: "tap all creatures target player controls".
            elif re.search(r"tap\s+all\s+(\w+)\s+target player controls", clause_lower) \
                    or re.search(r"tap\s+all\s+(\w+)\s+(?:that\s+)?(?:your\s+opponents?|target player)", clause_lower):
                tt = "permanent"
                if "creature" in clause_lower: tt = "creature"
                elif "artifact" in clause_lower: tt = "artifact"
                elif "land" in clause_lower: tt = "land"
                created_effect = TapEffect(target_type=tt, scope="all_target_player")

            elif re.search(r"\b(tap(?:s)?)\b\s+target", clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                 target_type = "permanent" # Refine based on desc
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "land" in target_desc: target_type = "land"
                 created_effect = TapEffect(target_type=target_type)

            # Mass untap: "untap all <type> you control".
            elif re.search(r"untap\s+all\s+(\w+)\s+you control", clause_lower):
                um = re.search(r"untap\s+all\s+(\w+)", clause_lower)
                tt = um.group(1).rstrip('s') if um else "permanent"
                if tt not in ("creature", "artifact", "land", "permanent"):
                    tt = "permanent"
                created_effect = UntapEffect(target_type=tt, scope="all_yours")

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
            # Impulse draw: "exile the top N cards, you may play them". Must
            # come before generic exile handling. (July 2026 sweep.)
            elif re.search(r"exile the top\s+(\w+)?\s*cards?\s+of\s+(?:your|their)\s+library", clause_lower) \
                    and ("may play" in clause_lower or "may cast" in clause_lower):
                num_match = re.search(r"exile the top\s+(\w+|\d+)?\s*cards?", clause_lower)
                n = 1
                if num_match and num_match.group(1):
                    n = text_to_number(num_match.group(1)) if not num_match.group(1).isdigit() else int(num_match.group(1))
                if not isinstance(n, int) or n <= 0:
                    n = 1
                created_effect = ImpulseDrawEffect(count=n)

            elif re.search(r"\bmill(?:s)?\b", clause_lower):
                count = 1
                # Accept word numbers too ("mills two cards") -- digits-only
                # left every worded count at 1 (first-touch sweep, July 2026).
                count_match = re.search(r"mill(?:s)?\s+(\d+|x|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+cards?", clause_lower)
                if count_match:
                    count_str = count_match.group(1)
                    count = 'x' if count_str == 'x' else text_to_number(count_str)

                target_desc = EffectFactory._extract_target_description(clause_lower) or "target_player"
                target_specifier = "target_player"
                if "you mill" in clause_lower: target_specifier = "controller"
                elif "opponent mills" in clause_lower or "each opponent mills" in clause_lower: target_specifier = "opponent"
                elif "each player mills" in clause_lower: target_specifier = "each_player"  # underscore: MillEffect's branch key (space form silently no-opped)
                created_effect = MillEffect(count, target=target_specifier) # Pass 'x' or number

            # Mass bounce: "return all <type> to their owners' hands" / "...you
            # control...". Must precede the single-target bounce branch.
            elif re.search(r"return\s+all\s+(\w+)", clause_lower) and re.search(r"to (?:its|their) owner'?s?'? hands?|to your hand", clause_lower):
                tt = "permanent"
                if "creature" in clause_lower: tt = "creature"
                elif "artifact" in clause_lower: tt = "artifact"
                elif "enchantment" in clause_lower: tt = "enchantment"
                elif "land" in clause_lower: tt = "land"
                sc = "all_yours" if "you control" in clause_lower else "all"
                created_effect = ReturnToHandEffect(target_type=tt, zone="battlefield", scope=sc)

            # Dig: "look at the top N cards ... put one into your hand ... rest
            # on the bottom/top".
            elif re.search(r"look at the top\s+(\w+|\d+)\s+cards?", clause_lower) and ("into your hand" in clause_lower or "in your hand" in clause_lower):
                lm = re.search(r"look at the top\s+(\w+|\d+)", clause_lower)
                look = 3
                if lm and lm.group(1):
                    look = int(lm.group(1)) if lm.group(1).isdigit() else text_to_number(lm.group(1))
                if not isinstance(look, int) or look <= 0: look = 3
                rest = "bottom"
                if "on the bottom" in clause_lower: rest = "bottom"
                elif "on top" in clause_lower or "on the top" in clause_lower: rest = "top"
                elif "graveyard" in clause_lower: rest = "graveyard"
                take = 1
                tm = re.search(r"put\s+(\w+|\d+)\s+(?:of them\s+)?into your hand", clause_lower)
                if tm and tm.group(1) and tm.group(1) not in ("one", "a", "an"):
                    take = int(tm.group(1)) if tm.group(1).isdigit() else text_to_number(tm.group(1))
                if not isinstance(take, int) or take <= 0: take = 1
                created_effect = DigEffect(look=look, take=take, rest=rest)

            # Put target permanent on top/bottom of its owner's library (tuck).
            elif re.search(r"put\s+target\s+(\w+).*on\s+(?:the\s+)?(top|bottom)\s+of\s+(?:its|their|his or her)\s+owner'?s?\s+library", clause_lower):
                pm = re.search(r"put\s+target\s+(\w+).*on\s+(?:the\s+)?(top|bottom)", clause_lower)
                tt = pm.group(1) if pm else "creature"
                pos = pm.group(2) if pm else "top"
                if tt not in ("creature", "artifact", "enchantment", "permanent", "land", "planeswalker"):
                    tt = "creature"
                created_effect = PutOnLibraryEffect(target_type=tt, position=pos)

            # Return to Hand (Bounce). BUGFIX (July 2026): the owner's-hand
            # test embedded a regex pattern inside a plain substring `in`
            # check, so it literally searched for "to (?:its|their) owner's
            # hand" and never matched -- standard bounce phrasing fell through
            # to the no-op fallback. Use a real regex.
            elif re.search(r"\breturn(?:s)?\b", clause_lower) and re.search(r"to (?:its|their) owner'?s hand|to your hand", clause_lower):
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

                 # Determine destination. The clause splitter severs
                 # "search ... AND put it onto the battlefield" into two
                 # clauses (same disease as the delayed-trigger comma split),
                 # so fall back to the FULL effect text when this clause
                 # carries no destination (first-touch sweep, July 2026).
                 _dest_re = r"put (?:it|that card|them|those cards?) (?:onto|into|in) (?:the|your) (\w+)"
                 dest_match = re.search(_dest_re, clause_lower) or re.search(_dest_re, effect_text.lower())
                 destination = "hand" # Default
                 if dest_match:
                      dest_word = dest_match.group(1)
                      if dest_word == "battlefield": destination = "battlefield"
                      elif dest_word == "graveyard": destination = "graveyard"
                      elif dest_word == "hand": destination = "hand"
                      elif dest_word == "library": destination = "library_top" # Assume top
                 # Check if destination implies battlefield tapped
                 # tapped = "tapped" in (dest_match.group(0) if dest_match else "")

                 _dest_span = (dest_match.group(0) if dest_match else "")
                 _tap_scope = effect_text.lower()[effect_text.lower().find(_dest_span):] if _dest_span else clause_lower
                 enters_tapped = destination == "battlefield" and bool(re.search(r"\btapped\b", _tap_scope))
                 created_effect = SearchLibraryEffect(search_type=search_type, destination=destination, count=count,
                                                      enters_tapped=enters_tapped)

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
                     # Card support manifest: a generic fallback means this clause
                     # will not do anything faithful at resolution. Attribute it.
                     if source_name:
                         from .card_support import report_unsupported
                         report_unsupported(source_name, f"unparsed clause: {clause_clean[:80]}", severity="partial")
                     effects.append(AbilityEffect(clause_clean)) # Store original case text

        # Final fallback if no clauses yielded effects
        if not effects and effect_text:
            logging.warning(f"Could not parse effect text into specific effects: '{effect_text}'. Adding as generic effect.")
            # Card support manifest: NOTHING in this text parsed -- the whole
            # effect is a no-op. Highest text-level severity.
            if source_name:
                from .card_support import report_unsupported
                report_unsupported(source_name, f"unparsed effect text: {effect_text[:80]}", severity="unparsed")
            effects.append(AbilityEffect(effect_text)) # Store original case text

        return effects