"""Utility functions for ability processing."""
import logging
import re


def has_damage_prevention_instruction(effect_text):
    """Return whether Oracle text contains an affirmative prevention clause.

    Substring checks cannot distinguish ``Prevent all damage`` from
    ``Damage can't be prevented``.  Besides exposing the latter through the
    PREVENT_DAMAGE action, that inversion used to build a prevention effect
    for cards whose actual instruction makes damage unpreventable.
    """
    for clause in re.split(r"[.\n;]+", str(effect_text or '').lower()):
        if 'damage' not in clause or not re.search(
                r"\bprevent(?:s|ed|ing)?\b", clause):
            continue
        if re.search(
                r"\b(?:can(?:not|['’]t)|could(?: not|n['’]t)|may not)"
                r"\s+be\s+"
                r"prevented\b|\b(?:is|are|was|were)(?: not|n't)\s+"
                r"prevented\b",
                clause):
            continue
        return True
    return False


def has_unpreventable_damage_instruction(effect_text):
    """Recognize a global, end-of-turn unpreventable-damage instruction.

    Persistent static abilities and scoped wording (for example, ``that
    damage can't be prevented``) need a different model and deliberately do
    not enter this temporary global-effect path.
    """
    for clause in re.split(r"[.\n;]+", str(effect_text or '').lower()):
        normalized = clause.strip()
        if not any(duration in normalized for duration in (
                'this turn', 'until end of turn')):
            continue
        if re.fullmatch(
                r"(?:until end of turn,\s*)?damage\s+"
                r"(?:can(?:not|['\u2019]t)|may not)\s+be\s+prevented"
                r"(?:\s+(?:this turn|until end of turn))?",
                normalized):
            return True
    return False

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
        if value.isdigit() or (value[:1] in ('+', '-') and value[1:].isdigit()):
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
    _CARD_OVERRIDES = {}
    _SOURCE_COUPLED_COPY_CARDS = frozenset({
        "cursed recording",
        "double down",
        "ether",
        "fin fang foom",
        "fire lord azula",
        "jeong jeong, the deserter",
        "kaervek, the punisher",
        "kaya, spirits' justice",
        "kitsa, otterball elite",
        "loki laufeyson",
        "pyromancer's goggles",
        "ral, crackling wit",
        "rimefire torque",
        "return the favor",
        "roving actuator",
        "shiko, paragon of the way",
        "silverquill, the disputant",
        "slick imitator",
        "taigam, master opportunist",
        "choreographed sparks",
    })
    _SOURCE_UNSUPPORTED_VARIABLE_TOKEN_PATTERNS = {
        "bat colony": (
            r"\bcreate a 1/1 black bat creature token with flying "
            r"for each mana from a cave spent to cast it\b"),
        "twitching doll": (
            r"\bcreate a 2/2 green spider creature token with reach "
            r"for each counter on this creature\b"),
        "glen elendra's answer": (
            r"\bcreate a 1/1 blue and black faerie creature token "
            r"with flying for each spell and ability countered this way\b"),
        "avengers: under siege": (
            r"\bcreate a treasure token for each villain you control\b"),
        "the astonishing ant-man": (
            r"\bcreate that many 1/1 green insect creature tokens\b"),
    }

    @classmethod
    def is_unsupported_source_coupled_copy(
            cls, source_name, effect_text):
        """Whether an audited source's copy instruction is atomic/unsupported.

        This predicate is shared with activation-cost preflight so delayed
        copy setters and conditional copy activations are hidden before they
        can tap, sacrifice, exile, remove counters, or spend mana.
        """
        source_key = str(source_name or "").strip().casefold()
        if source_key not in cls._SOURCE_COUPLED_COPY_CARDS:
            return False
        return bool(re.search(
            r"\b(?:cop(?:y|ies)|casualty|storm)\b",
            str(effect_text or ""), re.IGNORECASE))

    @classmethod
    def is_unsupported_variable_token_instruction(
            cls, source_name, effect_text):
        """Match one audited variable-token surface that must fail closed."""
        source_key = str(source_name or "").strip().casefold()
        pattern = cls._SOURCE_UNSUPPORTED_VARIABLE_TOKEN_PATTERNS.get(
            source_key)
        return bool(pattern and re.search(
            pattern, str(effect_text or ""),
            re.IGNORECASE | re.DOTALL))

    @classmethod
    def register_card_override(cls, card_name, factory):
        """Register a hand-written effect factory for one exact card name.

        ``factory`` receives ``(effect_text, targets, source_name)`` and must
        return an iterable of AbilityEffect objects.  Overrides are consulted
        before generic parsing, which makes this the explicit escape hatch for
        cards whose linked or conditional instructions cannot be represented
        safely by regexes.
        """
        if not card_name or not callable(factory):
            raise ValueError("card override requires an exact name and callable")
        cls._CARD_OVERRIDES[str(card_name).strip().casefold()] = factory

    @classmethod
    def unregister_card_override(cls, card_name):
        return cls._CARD_OVERRIDES.pop(str(card_name).strip().casefold(), None)

    @staticmethod
    def _restore_printed_name_case(name):
        """Recover ordinary Oracle name casing after trigger normalization.

        TriggeredAbility stores its parsed effect lowercase. Preserve already
        cased input, and otherwise title-case significant words while keeping
        interior articles/conjunctions/prepositions lowercase.
        """
        cleaned = str(name or "").strip()
        if not cleaned or any(character.isupper() for character in cleaned):
            return cleaned
        minor_words = {
            "a", "an", "and", "as", "at", "by", "for", "from", "in",
            "of", "on", "or", "the", "to", "with",
        }
        restored = []
        for index, word in enumerate(cleaned.split()):
            core = word.rstrip(",")
            suffix = word[len(core):]
            if index and core in minor_words:
                cased = core
            else:
                cased = "-".join(
                    part[:1].upper() + part[1:]
                    for part in core.split("-"))
            restored.append(cased + suffix)
        return " ".join(restored)

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


    # --- Reflexive trigger extraction (CR 603.12) ---------------------------
    _REFLEXIVE_TRIGGER = re.compile(
        r"^\s*(.+?)\s*(?:[.;]\s*|\s+)(when\s+(?:you\s+do|that player\s+does))\s*,\s*(.+?)\s*$",
        re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _extract_reflexive_trigger(effect_text):
        """Return one gated reflexive-trigger effect, or None.

        A reflexive trigger is created only after the preceding instruction
        succeeds. Keeping both halves in one effect prevents ordinary clause
        splitting from resolving the "when you do" rider unconditionally.
        """
        match = EffectFactory._REFLEXIVE_TRIGGER.match(effect_text.strip())
        if not match:
            return None
        from .ability_types import ReflexiveTriggerEffect
        prerequisite = match.group(1).strip(". ;")
        condition = match.group(2).strip()
        trigger_effect = match.group(3).strip(". ")
        if not prerequisite or not trigger_effect:
            return None
        return ReflexiveTriggerEffect(prerequisite, trigger_effect, condition)

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

        if re.fullmatch(
                r"\s*cast this card from your graveyard for its flashback "
                r"cost\.\s*then exile it\.?\s*",
                effect_text, re.IGNORECASE):
            from .ability_types import RuleDeclarationEffect
            return [RuleDeclarationEffect(effect_text)]
        if re.fullmatch(
                r"\s*cast from graveyard\s*,\s*then exile\.?\s*",
                effect_text, re.IGNORECASE):
            # Compact virtual text synthesized by ActivatedAbility for a
            # Flashback declaration. Casting and exile replacement are owned
            # by the graveyard-cast transaction, not an executable effect.
            from .ability_types import RuleDeclarationEffect
            return [RuleDeclarationEffect(effect_text)]

        override = EffectFactory._CARD_OVERRIDES.get(
            str(source_name or "").strip().casefold())
        if override is not None:
            result = override(effect_text, targets, source_name)
            return list(result or [])

        source_key = str(source_name or "").strip().casefold()
        lowered = effect_text.lower()

        # A kicked "deals N ... instead" instruction is one replacement
        # choice, not two independent damage events.  Parse it before generic
        # sentence splitting so the base and kicked values cannot stack.
        kicked_damage = re.search(
            r"\bdeals\s+(\d+)\s+damage\s+to\s+"
            r"(any target|target creature)\s*\.\s*"
            r"if this spell was kicked\s*,\s*it deals\s+(\d+)\s+damage"
            r"(?:\s+to that creature)?\s+instead\s*\.?",
            effect_text, re.IGNORECASE | re.DOTALL)
        if kicked_damage:
            from .ability_types import KickedDamageEffect
            target_phrase = kicked_damage.group(2).lower()
            target_type = ("any target" if target_phrase == "any target"
                           else "creature")
            return [KickedDamageEffect(
                int(kicked_damage.group(1)),
                int(kicked_damage.group(3)), target_type)]

        if re.fullmatch(
                r"\s*each player exiles all but the bottom six cards of "
                r"their library face down\.?\s*", lowered):
            from .ability_types import ExileLibrariesExceptBottomEffect
            return [ExileLibrariesExceptBottomEffect(
                keep_count=6, face_down=True)]
        # Scry is an instruction keyword, and reminder text must not prevent
        # the following Draw instruction from becoming a separate sequenced
        # effect (Opt and the same simple template).
        sequence_surface = re.sub(
            r"\([^()]*\)", " ", effect_text, flags=re.DOTALL)
        sequence_surface = re.sub(r"\s+", " ", sequence_surface).strip()

        # These current Standard cards express variable token quantities that
        # the generic parser cannot derive from live state. Some never capture
        # a count expression at all (silently becoming one token), and Glen
        # Elendra's Answer can split at "spell and ability" after already
        # creating one Faerie. Classify each bounded printed shape before any
        # clause split so resolution is atomic and diagnostic.
        if EffectFactory.is_unsupported_variable_token_instruction(
                source_name, sequence_surface):
            from .ability_types import UnsupportedEffect
            reason = (
                "unsupported variable token instruction: "
                f"{source_key}")
            if source_name:
                from .card_support import report_unsupported
                report_unsupported(
                    source_name, reason, severity="partial")
            return [UnsupportedEffect(
                effect_text, reason=reason, severity="partial")]

        # Sword's combat trigger has one faithful immediate instruction and
        # one unsupported delayed watcher. Preserve the real Treasure, then
        # surface the missing watcher as a failed/diagnosed effect instead of
        # attempting an immediate CopySpellEffect with no referenced spell.
        if (source_key == "sword of wealth and power"
                and re.search(
                    r"\bcreate a treasure token\b",
                    sequence_surface, re.IGNORECASE)
                and re.search(
                    r"\bwhen you next cast an instant or sorcery spell "
                    r"this turn\s*,?\s*copy that spell\b",
                    sequence_surface, re.IGNORECASE)):
            from .ability_types import (
                CreateTreasureEffect, UnsupportedEffect)
            reason = "unsupported delayed spell-copy watcher"
            if source_name:
                from .card_support import report_unsupported
                report_unsupported(
                    source_name, reason, severity="partial")
            return [
                CreateTreasureEffect(),
                UnsupportedEffect(
                    effect_text, reason=reason, severity="partial"),
            ]

        # These audited cards couple copying to a source-specific trigger,
        # delayed watcher, cast permission, exception, or follow-up mutation
        # that the generic effect sequence cannot represent atomically.  A
        # source-aware guard is required because TriggeredAbility stores the
        # qualifier separately from its effect (for example, Double Down's
        # "outlaw spell" gate and Azula's "while ... attacking" gate).  The
        # casualty/storm markers cover Silverquill and Ral even when reminder
        # text is stripped before the executable surface is parsed.
        source_copy_marker = re.search(
            r"\b(?:cop(?:y|ies)|casualty|storm)\b",
            lowered, re.IGNORECASE)
        if (EffectFactory.is_unsupported_source_coupled_copy(
                source_name, effect_text)
                and source_copy_marker):
            from .ability_types import UnsupportedEffect
            reason = (
                "unsupported source-coupled copy instruction: "
                f"{source_key}: {source_copy_marker.group(0)}")
            if source_name:
                from .card_support import report_unsupported
                report_unsupported(
                    source_name, reason, severity="partial")
            return [UnsupportedEffect(
                effect_text, reason=reason, severity="partial")]

        # A spell copy that depends on an optional precursor, a coin-flip
        # branch, or a copy-changing exception is atomic.  If its surrounding
        # rules are split into clauses, a failed generic precursor can be
        # followed by a successful CopySpellEffect, silently copying a spell
        # whose cost/condition was never satisfied (Aziza, Alania, Breeches,
        # Mica, and Jackal).  Fail the complete instruction before splitting.
        # Plain outer triggers such as Leyline's "copy that spell" and Sage's
        # supported "copy this spell" have none of these coupled markers.
        spell_copy = re.search(
            r"\bcopy\s+(?:that|this)\s+spell\b",
            sequence_surface, re.IGNORECASE)
        unsupported_spell_copy_context = None
        if spell_copy:
            spell_copy_guard_patterns = (
                r"\byou may\b.{0,420}\bcopy\s+(?:that|this)\s+spell\b",
                r"\bif you do\b.{0,420}\bcopy\s+(?:that|this)\s+spell\b",
                r"\bflip a coin\b.{0,420}\bcopy\s+(?:that|this)\s+spell\b",
                r"\bcopy\s+(?:that|this)\s+spell\b"
                r"[^.\n]{0,180}\bexcept\b",
            )
            unsupported_spell_copy_context = next((
                match for pattern in spell_copy_guard_patterns
                if (match := re.search(
                    pattern, sequence_surface,
                    re.IGNORECASE | re.DOTALL))
            ), None)
        if unsupported_spell_copy_context:
            from .ability_types import UnsupportedEffect
            reason = (
                "unsupported coupled spell-copy instruction: "
                f"{unsupported_spell_copy_context.group(0)[:100]}")
            if source_name:
                from .card_support import report_unsupported
                report_unsupported(
                    source_name, reason, severity="partial")
            return [UnsupportedEffect(
                effect_text, reason=reason, severity="partial")]

        scry_draw = re.fullmatch(
            r"scry\s+(\d+|x)\s*\.\s*draw\s+"
            r"(a|an|one|two|three|four|five|\d+)\s+cards?\s*\.?",
            sequence_surface, re.IGNORECASE)
        if scry_draw:
            from .ability_types import DrawCardEffect, ScryEffect
            scry_count = (scry_draw.group(1).lower()
                          if scry_draw.group(1).lower() == "x"
                          else int(scry_draw.group(1)))
            draw_word = scry_draw.group(2).lower()
            draw_count = (int(draw_word) if draw_word.isdigit()
                          else text_to_number(draw_word))
            return [ScryEffect(scry_count), DrawCardEffect(draw_count)]

        if (source_key == "deceit"
                and "target opponent reveals their hand" in lowered
                and "choose a nonland card" in lowered):
            from .ability_types import HandSelectionEffect
            return [HandSelectionEffect(excluded_types={"land"})]

        if (source_key == "colorstorm stallion"
                and re.search(r"this creature gets \+1/\+1 until end of turn",
                              lowered)
                and re.search(r"five or more mana was spent", lowered)):
            from .ability_types import (
                BuffEffect, CreateTokenCopyOfSourceEffect,
                ManaSpentConditionalEffect)
            return [
                BuffEffect(1, 1, target_type="self",
                           duration="end_of_turn"),
                ManaSpentConditionalEffect(
                    5, CreateTokenCopyOfSourceEffect()),
            ]

        # Coupled token-copy instructions must be classified before any
        # sentence/clause splitting. Replacement text can otherwise parse its
        # "would create ... tokens" preamble as an ordinary CreateTokenEffect
        # and mutate state before a later copy fragment fails (Mirrormind
        # Crown and Moonlit Meditation). The exact Three Steps Ahead template
        # is the sole generic target-copy instruction implemented here;
        # Colorstorm's dedicated whole-effect route has already returned
        # above, and real Offspring uses its flagged trigger resolver.
        coupled_token_copy = re.search(
            r"\bcreate(?:s)?\b[^.;\n]*?\btokens?\b\s+"
            r"(?:that(?:['\u2019]s|\s+is)\s+an?\s+copy\s+of|"
            r"that\s+are\s+(?:an?\s+copy|(?:each\s+)?copies)\s+of|"
            r"(?:an?\s+)?cop(?:y|ies)\s+of)\b",
            lowered)
        # Spree owns a modal container and recursively parses each selected
        # mode. Let the container reach that parser; the copy mode itself will
        # return through the exact supported branch on its recursive call.
        if (coupled_token_copy
                and not re.match(r"\s*spree\b", lowered)):
            exact_target_copy = re.fullmatch(
                r"\s*create(?:s)?\s+a\s+token\s+"
                r"that(?:['\u2019]s|\s+is)\s+a\s+copy\s+of\s+"
                r"target\s+artifact\s+or\s+creature\s+you\s+control"
                r"\s*\.?\s*",
                lowered)
            if exact_target_copy and "except" not in lowered:
                from .ability_types import CreateTokenCopyOfTargetEffect
                return [CreateTokenCopyOfTargetEffect(
                    allowed_types={"artifact", "creature"},
                    controller_only=True)]

            from .ability_types import UnsupportedEffect
            reason = (
                "unsupported token-copy instruction: "
                f"{coupled_token_copy.group(0)[:80]}")
            if source_name:
                from .card_support import report_unsupported
                report_unsupported(
                    source_name, reason, severity="partial")
            return [UnsupportedEffect(
                effect_text, reason=reason, severity="partial")]

        # Rules-bearing token riders that are not represented by token_data
        # must fail before clause splitting. Otherwise an approximate token
        # can be created before the unsupported rider is discarded.
        # Reminder text may describe a separate casting transaction (Gift,
        # Offspring, and similar mechanics). Descriptor/rider guards inspect
        # the executable surface only so they do not reject a supported main
        # instruction because of reminder-only token wording.
        token_surface = sequence_surface.lower()
        token_creation = re.search(
            r"\bcreate(?:s)?\b.{0,320}?\btokens?\b",
            token_surface, re.DOTALL)
        unsupported_token_rider = None
        if token_creation:
            rider_patterns = (
                r"\btokens?\b[^;\n]{0,260}\b"
                r"(?:that(?:['\u2019]s|\s+is)|they enter)\s+"
                r"tapped and attacking\b",
                r"\btokens?\b[^;\n]{0,220}"
                r"[\x22]this token can block only\b",
                r"\btokens?\b[^;\n]{0,220}"
                r"[\x22]this token gets \+[^\x22]+?\bfor each\b",
                r"\btokens?\b[^\n.]{0,180}\bwith\b"
                r"[^\n.]{0,120}\band haste\b",
                r"\btokens?\b[^\n.]{0,180}\.\s*it gains haste\b",
                r"\btokens?\b.{0,320}\band attach\b.{0,100}\bto it\b",
                r"\btokens?\b.{0,320}\bexile those tokens\b",
                r"\btokens?\b.{0,360}\bput\b.{0,120}\b"
                r"counters?\s+on\s+(?:it|that token|those tokens)\b",
                r"\btokens?\b.{0,320}\bsacrifice\s+"
                r"(?:it|that token|those tokens)\b",
                r"\bif you do\b[^.\n]{0,260}\bcreate\b",
                r"\bif you (?:don['\u2019]?t|do not)\b"
                r"[^.\n]{0,260}\bcreate\b",
                r"\bfirst time\b.{0,420}\bsecond time\b.{0,320}"
                r"\bthird time\b.{0,220}\bcreate\b",
                r"\bsacrifice it\s+and\s+create\b",
                r"\bdestroy that creature\s*,?\s*then\s+create\b",
                r"\bput a \+1/\+1 counter on this creature\s*\.\s*"
                r"create\b",
                r"\bsurveil\s+(?:\d+|one|two|three)\s*\.\s*create\b",
                r"\btokens?\b.{0,160}\.\s*surveil\s+"
                r"(?:\d+|one|two|three)\b",
                r"\btokens?\b.{0,300}\bchosen player loses x life\b"
                r".{0,180}\byou gain x life\b",
                r"\btokens?\b.{0,300}\bcreatures you control\b"
                r".{0,140}\bgain haste\b",
            )
            unsupported_token_rider = next((
                match for pattern in rider_patterns
                if (match := re.search(
                    pattern, token_surface, re.IGNORECASE | re.DOTALL))
            ), None)
        if unsupported_token_rider:
            from .ability_types import UnsupportedEffect
            reason = (
                "unsupported token rider: "
                f"{unsupported_token_rider.group(0)[:100]}")
            if source_name:
                from .card_support import report_unsupported
                report_unsupported(
                    source_name, reason, severity="partial")
            return [UnsupportedEffect(
                effect_text, reason=reason, severity="partial")]

        # A simple literal draw followed by a simple token instruction is an
        # ordered pair, not a draw-only effect. Parse each sentence through
        # the normal implementations after recognizing the whole sequence.
        draw_then_create = re.fullmatch(
            r"\s*(?P<draw>draw\s+"
            r"(?:a|an|one|two|three|four|five|\d+)\s+cards?)\s*\.\s*"
            r"(?P<create>create\b.+?\btokens?)\s*\.?\s*",
            sequence_surface, re.IGNORECASE | re.DOTALL)
        if draw_then_create:
            return [
                effect
                for instruction in (
                    draw_then_create.group("draw"),
                    draw_then_create.group("create"))
                for effect in EffectFactory.create_effects(
                    instruction, targets, source_name=source_name)
            ]

        quest_reward = re.fullmatch(
            r"\s*if it has (\w+|\d+) or more quest counters on it,\s*"
            r"put a \+1/\+1 counter on target creature you control\.\s*"
            r"it gains trample until end of turn\.?\s*",
            lowered, re.IGNORECASE)
        if quest_reward:
            from .ability_types import SourceCounterThresholdRewardEffect
            threshold = text_to_number(quest_reward.group(1))
            if not isinstance(threshold, int):
                threshold = int(quest_reward.group(1))
            return [SourceCounterThresholdRewardEffect("quest", threshold)]

        quest_resolution = re.fullmatch(
            r"\s*put a \+1/\+1 counter on target creature you control\.\s*"
            r"it gains trample until end of turn\.?\s*",
            lowered, re.IGNORECASE)
        if quest_resolution:
            from .ability_types import AddCountersEffect, GainKeywordEffect
            return [
                AddCountersEffect(
                    "+1/+1", 1,
                    target_type="target creature you control"),
                GainKeywordEffect(
                    "trample", target_type="target creature",
                    duration="end_of_turn"),
            ]

        remove_counter = re.fullmatch(
            r"\s*(you may )?remove\s+(?:a|an|one)\s+"
            r"(?:(\w+|[+\-]\d+/[+\-]\d+)\s+)?counter\s+from\s+"
            r"(?:this (?:creature|permanent)|it|him|her)\.?\s*",
            lowered, re.IGNORECASE)
        if remove_counter:
            from .ability_types import RemoveCounterEffect
            return [RemoveCounterEffect(
                counter_type=remove_counter.group(2),
                optional=bool(remove_counter.group(1)))]

        if re.fullmatch(
                r"\s*double the power of target creature you control "
                r"until end of turn\.?\s*", lowered):
            from .ability_types import DoublePowerEffect
            return [DoublePowerEffect()]
        optional_mana = re.fullmatch(
            r"\s*you may pay\s*((?:\{[^}]+\})+)\.\s*if you do,\s*(.+?)\s*",
            effect_text, re.IGNORECASE | re.DOTALL)
        if optional_mana:
            from .ability_types import OptionalManaThenEffect
            return [OptionalManaThenEffect(
                optional_mana.group(1), optional_mana.group(2))]
        optional_discard = re.fullmatch(
            r"\s*you may discard\s+(?:a|one)\s+card\s*\.\s*"
            r"if you do,\s*(.+?)\s*",
            effect_text, re.IGNORECASE | re.DOTALL)
        if optional_discard:
            from .ability_types import OptionalDiscardThenEffect
            return [OptionalDiscardThenEffect(optional_discard.group(1))]
        if re.fullmatch(
                r"\s*attach(?: this equipment| it)? to target creature"
                r"(?: you control)?(?:\. equip only as a sorcery)?\.?\s*",
                lowered):
            from .ability_types import AttachEquipmentEffect
            return [AttachEquipmentEffect()]
        if (re.fullmatch(r"\s*activate crew ability\.?\s*", lowered)
                or re.fullmatch(
                    r"tap any number of untapped creatures you control with "
                    r"total power\s+\d+\s+or greater:\s*this vehicle becomes "
                    r"an artifact creature until end of turn\.?",
                    lowered)):
            from .ability_types import CrewEffect
            value_match = re.search(r"total power\s+(\d+)\s+or greater", lowered)
            return [CrewEffect(
                int(value_match.group(1)) if value_match else 0)]
        discover_match = re.fullmatch(
            r"\s*discover\s+(\d+)\.?(?:\s+activate only as a sorcery\.?)?\s*",
            lowered)
        if discover_match:
            from .ability_types import DiscoverEffect
            return [DiscoverEffect(int(discover_match.group(1)))]
        dynamic_discover = re.fullmatch(
            r"\s*discover\s+x,\s*where x is (?:that|the) spell's mana "
            r"value\.?(?:\s+activate only as a sorcery\.?)?\s*",
            lowered)
        if dynamic_discover:
            from .ability_types import DiscoverEffect
            return [DiscoverEffect('spell_mana_value')]
        if re.fullmatch(
                r"\s*discover again for the same value\.?(?:\s+this ability "
                r"triggers only once each turn\.?)?\s*", lowered):
            from .ability_types import DiscoverEffect
            return [DiscoverEffect('same')]
        endure_with_life = re.fullmatch(
            r"\s*you lose (?P<life>\d+) life and (?P<subject>it|this creature) "
            r"endures? (?P<value>\d+|x)\.?\s*", lowered)
        if endure_with_life:
            from .ability_types import EndureEffect, LoseLifeEffect
            return [
                LoseLifeEffect(
                    int(endure_with_life.group('life')),
                    target='controller'),
                EndureEffect(
                    endure_with_life.group('value'),
                    subject_event=(
                        endure_with_life.group('subject') == 'it')),
            ]
        endure_match = re.fullmatch(
            r"\s*(?P<subject>it|this creature|[\w'’ ,-]+)\s+endures?\s+"
            r"(?P<value>\d+|x)(?P<counter_value>,\s*where x is the number "
            r"of counters on this creature)?\.?(?:\s+activate only as a "
            r"sorcery\.?)?\s*", lowered)
        if endure_match:
            from .ability_types import EndureEffect
            return [EndureEffect(
                endure_match.group('value'),
                subject_event=endure_match.group('subject').strip() == 'it',
                value_from_source_counters=bool(
                    endure_match.group('counter_value')))]
        if re.fullmatch(
                r"\s*investigate once for each opponent who has more cards "
                r"in hand than you\.?\s*", lowered):
            from .ability_types import InvestigateEffect
            return [InvestigateEffect(count='opponents_more_cards')]
        if re.fullmatch(
                r"\s*investigate x times, where x is the total number of "
                r"creatures those players control\.?\s*", lowered):
            from .ability_types import InvestigateEffect
            return [InvestigateEffect(count='target_players_creatures')]
        if re.fullmatch(
                r"\s*(?:you may have\s+)?(?:it|he|she|this creature|"
                r"[\w.'’ -]+|target(?:\s+\w+){0,3}\s+creature(?: you control)?)"
                r"\s+connives?\.?(?:\s+(?:do this only once each turn|"
                r"activate only during your turn)\.?)?\s*",
                lowered):
            from .ability_types import ConniveEffect
            return [ConniveEffect(
                targeted=bool(re.search(
                    r"target\s+(?:\w+\s+){0,3}creature", lowered)),
                optional="may" in lowered,
                once_each_turn="once each turn" in lowered)]
        if re.match(r"^\s*airbend\b", lowered):
            from .ability_types import AirbendEffect
            # Target extraction can consume a leading targeted keyword action.
            # Preserve every later instruction by peeling only the first
            # sentence (and its reminder text) here.
            without_reminder = re.sub(
                r"\s*\([^()]*\)\s*", " ", effect_text).strip()
            instruction, separator, suffix = without_reminder.partition('.')
            target_match = re.fullmatch(
                r"\s*airbend\s+(.+?)\s*", instruction, re.IGNORECASE)
            if target_match:
                effects = [AirbendEffect(
                    target_description=target_match.group(1).strip())]
                if separator and suffix.strip():
                    effects.extend(EffectFactory.create_effects(
                        suffix.strip(), targets, source_name=source_name))
                return effects
        if re.match(r"^\s*suspect it\.\s+create\b", lowered):
            from .ability_types import SuspectEffect
            suffix = re.split(r"suspect it\.\s*", effect_text,
                              maxsplit=1, flags=re.IGNORECASE)[1]
            return ([SuspectEffect()]
                    + EffectFactory.create_effects(
                        suffix, targets, source_name=source_name))
        if re.search(
                r"you may suspect one of the other creatures\. if you do, "
                r"this creature is no longer suspected", lowered):
            from .ability_types import TransferSuspectEffect
            return [TransferSuspectEffect()]
        if re.fullmatch(
                r"\s*all suspected creatures are no longer suspected\.?\s*",
                lowered):
            from .ability_types import SuspectEffect
            return [SuspectEffect(clear_all=True)]
        if re.fullmatch(
                r"\s*(?:this creature|it)\s+is no longer suspected\.?\s*",
                lowered):
            from .ability_types import SuspectEffect
            return [SuspectEffect(clear_source=True)]
        if re.fullmatch(
                r"\s*(?:you may\s+)?suspect(?: up to one| one)?\s+"
                r"(?:other\s+)?(?:target\s+)?(?:enchanted\s+)?creature"
                r"(?: you control)?\.?(?:\s*\([^)]*\))?\s*"
                r"|\s*(?:you may\s+)?suspect it\.?(?:\s*\([^)]*\))?\s*",
                lowered):
            from .ability_types import SuspectEffect
            return [SuspectEffect(
                targeted="target" in lowered or "enchanted creature" in lowered,
                optional="may" in lowered or "up to" in lowered,
                attached="enchanted creature" in lowered)]
        if (source_key.startswith("esper origins")
                and "surveil 2" in lowered):
            from .ability_types import (
                SurveilEffect, GainLifeEffect, EsperGraveyardTransformEffect)
            return [SurveilEffect(2), GainLifeEffect(2),
                    EsperGraveyardTransformEffect()]
        if (source_key.startswith("esper origins")
                or source_key == "summon: esper maduin"):
            if ("reveal the top card of your library" in lowered
                    and "if it's a permanent card" in lowered):
                from .ability_types import EsperSagaRevealPermanentEffect
                return [EsperSagaRevealPermanentEffect()]
            if re.fullmatch(r"\s*add\s*\{g\}\{g\}\.?\s*", lowered):
                from .ability_types import AddManaEffect
                return [AddManaEffect(mana_dict={"G": 2})]
            if ("other creatures you control get +2/+2" in lowered
                    and "gain trample until end of turn" in lowered):
                from .ability_types import EsperSagaChapterThreeEffect
                return [EsperSagaChapterThreeEffect()]
        if (source_key == "mistrise village"
                and "next spell you cast this turn can't be countered" in lowered):
            from .ability_types import GrantNextSpellUncounterableEffect
            return [GrantNextSpellUncounterableEffect()]
        if source_key == "day of black sun" and "loses all abilities" in lowered:
            from .ability_types import DayOfBlackSunEffect
            return [DayOfBlackSunEffect()]
        if source_key == "erode" and lowered.startswith("destroy target"):
            from .ability_types import ErodeEffect
            return [ErodeEffect()]
        if source_key == "no more lies" and "unless its controller pays" in lowered:
            from .ability_types import CounterUnlessPaysEffect
            return [CounterUnlessPaysEffect('{3}')]
        if (source_key == "archdruid's charm"
                and "search your library for a creature or land card" in lowered):
            from .ability_types import ArchdruidSearchEffect
            return [ArchdruidSearchEffect()]
        if source_key == "deadly cover-up" and "destroy all creatures" in lowered:
            from .ability_types import DeadlyCoverUpEffect
            return [DeadlyCoverUpEffect()]
        # Copiable oracle text must keep working when the resolving object's
        # name changes (for example, Superior Spider-Man entering as a copy of
        # North Wind Avatar).
        if re.search(
                r"card you own from outside the game into your hand",
                lowered):
            from .ability_types import OutsideGameCardEffect
            return [OutsideGameCardEffect()]
        if source_key == "strategic betrayal" and "their graveyard" in lowered:
            from .ability_types import StrategicBetrayalEffect
            return [StrategicBetrayalEffect()]
        if source_key == "lumbering worldwagon":
            if "search your library for a basic land card" in lowered:
                from .ability_types import SearchLibraryEffect
                return [SearchLibraryEffect(
                    search_type="basic land", destination="battlefield",
                    count=1, policy_choice=True, optional=True,
                    enters_tapped=True)]
            crew_match = re.search(r"total power\s+(\d+)\s+or greater", lowered)
            if crew_match:
                from .ability_types import CrewEffect
                return [CrewEffect(int(crew_match.group(1)))]
        if ("can't be blocked this turn" in lowered
                and "target creature" in lowered):
            from .ability_types import GainKeywordEffect
            effect = GainKeywordEffect(
                "unblockable", target_type="target creature",
                duration="end_of_turn")
            effect.effect_text = (
                "target creature with power 2 or less gains unblockable "
                "until end of turn")
            return [effect]
        if (source_key.startswith("aang, swift savior")
                and "airbend up to one other target creature or spell" in lowered):
            from .ability_types import AirbendEffect
            return [AirbendEffect()]
        if (source_key == "cosmogrand zenith"
                and re.match(r"^\s*choose one\s*[—–-]", effect_text,
                             re.IGNORECASE)):
            modes = [
                mode.strip(" .\n") for mode in re.split(
                    r"(?:^|\n)\s*[•●]\s*", effect_text)[1:]
                if mode.strip(" .\n")]
            if modes:
                from .ability_types import ResolutionModalEffect
                return [ResolutionModalEffect(modes, source_name=source_name)]
        # This instruction can belong to another object after a copy effect
        # (notably Superior Spider-Man), so recognize its oracle shape rather
        # than keying the implementation to Brightglass Gearhulk's name.
        if ("search your library" in lowered
                and re.search(
                    r"artifact\s*,\s*creature\s*,\s*and/or\s+enchantment "
                    r"cards? with mana value 1 or less",
                    lowered)):
            from .ability_types import SearchLibraryEffect
            return [SearchLibraryEffect(
                search_type="any", destination="hand", count=2,
                policy_choice=True, optional=True,
                allowed_types={"artifact", "creature", "enchantment"},
                max_mana_value=1)]
        if (source_key == "starfield shepherd"
                and "search your library" in lowered):
            from .ability_types import SearchLibraryEffect
            return [SearchLibraryEffect(
                search_type="basic plains or small creature",
                destination="hand", count=1,
                # A restricted search of a hidden library may legally fail to
                # find even when an eligible card is present (CR 701.19b).
                policy_choice=True, optional=True)]
        if (source_key == "combustion technique"
                and "number of lesson cards" in lowered):
            from .ability_types import LessonDamageWithExileEffect
            return [LessonDamageWithExileEffect()]
        if (source_key == "daydream"
                and "return that card to the battlefield" in lowered):
            from .ability_types import BlinkWithCounterEffect
            return [BlinkWithCounterEffect()]
        if (source_key == "sage of the skies"
                and "copy this spell" in lowered):
            from .ability_types import CopySpellEffect
            return [CopySpellEffect(
                target_type="spell", new_targets=False, copy_that=True)]
        if (source_key == "winternight stories"
                and "draw three cards" in lowered):
            from .ability_types import DrawCardEffect, DiscardTwoUnlessCreatureEffect
            return [DrawCardEffect(3), DiscardTwoUnlessCreatureEffect()]
        if source_key == "duress":
            from .ability_types import HandSelectionEffect
            return [HandSelectionEffect(noncreature_nonland=True)]
        if source_key == "flow state" and "look at the top three" in effect_text.lower():
            from .ability_types import DigEffect
            return [DigEffect(
                look=3, take=1, rest="bottom", bonus_take=2,
                bonus_condition="instant_and_sorcery_in_graveyard",
                rest_order="choice")]
        if (source_key == "accumulate wisdom"
                and "look at the top three" in effect_text.lower()):
            from .ability_types import DigEffect
            return [DigEffect(
                look=3, take=1, rest="bottom", bonus_take=3,
                bonus_condition="three_lessons_in_graveyard",
                rest_order="choice")]
        if (source_key == "consult the star charts"
                and "look at the top x" in effect_text.lower()):
            from .ability_types import DigEffect
            return [DigEffect(
                look="lands_you_control", take=1, rest="bottom",
                bonus_take=2, bonus_condition="kicked",
                rest_order="random")]
        if re.search(
                r"\bearthbend x\s*,\s*where x is that creature(?:'|\u2019)s power",
                effect_text, re.IGNORECASE):
            from .ability_types import EarthbendEffect
            return [EarthbendEffect("event_last_known_power")]
        if source_key == "oildeep gearhulk" and "look at target player's hand" in effect_text.lower():
            from .ability_types import HandSelectionEffect
            return [HandSelectionEffect(optional=True, rummage=True)]
        if (source_key == "mosswood dreadknight // dread whispers"
                and re.search(
                    r"cast it from your graveyard as an adventure until the "
                    r"end of your next turn",
                    effect_text, re.IGNORECASE)):
            from .ability_types import GraveyardAdventurePermissionEffect
            return [GraveyardAdventurePermissionEffect()]
        if source_key == "cacophony scamp" and "may sacrifice" in effect_text.lower():
            from .ability_types import OptionalSacrificeProliferateEffect
            return [OptionalSacrificeProliferateEffect()]
        if (source_key == "caustic bronco"
                and "reveal the top card of your library" in effect_text.lower()):
            from .ability_types import CausticBroncoAttackEffect
            return [CausticBroncoAttackEffect()]
        if re.search(
                r"target instant or sorcery card in your graveyard gains "
                r"flashback until end of turn",
                effect_text, re.IGNORECASE):
            from .ability_types import GrantFlashbackEffect
            return [GrantFlashbackEffect()]
        if (source_key == "bushwhack"
                and re.search(r"search your library for a basic land card",
                              effect_text, re.IGNORECASE)):
            # The reveal/move/shuffle wording is one search instruction.  The
            # generic comma splitter previously emitted an extra no-op
            # ``put it into your hand`` fragment after the real search.
            from .ability_types import SearchLibraryEffect
            return [SearchLibraryEffect(
                search_type="basic land", destination="hand", count=1,
                # A hidden-zone search with a quality restriction may legally
                # fail to find, and the selected land materially affects play.
                # Keep both decisions on the policy surface instead of using
                # search_library_and_choose's deterministic fallback.
                policy_choice=True, optional=True)]

        effects = []

        # CR 608.2: resolving a spell performs only its spell instructions.
        # Printed activated abilities ("<costs>: <effect>") function from the
        # battlefield or other zones, never during resolution, so drop those
        # lines before clause splitting (July 2026, found by Herd Migration:
        # its "{1}{G}, Discard this card: Search..." line resolved alongside
        # the Domain token effect, discarding and gaining life on cast).
        if "\n" in effect_text and ":" in effect_text:
            kept_lines = []
            for line in effect_text.split("\n"):
                cleaned = re.sub(r'\([^()]*\)', ' ', line)
                cleaned = re.sub(r'"[^"]*"', ' ', cleaned)
                colon_idx = cleaned.find(':')
                if colon_idx != -1:
                    prefix = cleaned[:colon_idx]
                    if '.' not in prefix and re.search(
                            r"\{[^}]+\}|\bdiscard this card\b|\bsacrifice\b|\bpay \d+ life\b",
                            prefix, re.IGNORECASE):
                        continue
                kept_lines.append(line)
            effect_text = "\n".join(kept_lines)
            if not effect_text.strip(". \n"):
                return []

        harmonize_lines = [
            line for line in effect_text.splitlines()
            if re.match(r"^\s*harmonize\b", line, re.IGNORECASE)]
        effect_text = "\n".join(
            line for line in effect_text.splitlines()
            if not re.match(
                r"^\s*(?:flashback|harmonize|warp)\b", line,
                re.IGNORECASE))
        unsupported_riders = {
            "if an opponent controls that creature": (
                "conditional target-controller rider is not enforced"),
            "for as long as you control": (
                "source-duration permission/restriction is not fully modeled"),
        }
        lowered_effect_text = effect_text.lower()
        if source_name:
            for marker, reason in unsupported_riders.items():
                if marker in lowered_effect_text:
                    from .card_support import report_unsupported
                    report_unsupported(
                        source_name, reason, severity="partial")
        if not effect_text.strip(". \n"):
            return []

        # Preserve a complete basic-land search transaction before commas and
        # "then" split its destination/shuffle/conditional untap into no-ops.
        # Ignore reminder text (not game instructions) while preserving string
        # offsets for prefix/suffix slicing. Modal shells must split into modes
        # first; otherwise the prefix becomes a bogus ``Choose two — •`` effect.
        search_surface = re.sub(
            r"\([^()]*\)", lambda match: " " * len(match.group(0)),
            effect_text)
        search_transaction = (
            None if re.match(r"^\s*choose\b", search_surface, re.IGNORECASE)
            else re.search(
                r"(?:(you|its controller) may\s+)?search\s+(your|their)\s+library\s+"
                r"for\s+a\s+basic land card\s*,\s*put\s+(?:it|that card)\s+onto\s+"
                r"the battlefield tapped\s*,\s*then shuffle",
                search_surface, re.IGNORECASE))
        if search_transaction:
            prefix = effect_text[:search_transaction.start()].strip(" .,")
            prefix = re.sub(r"\bthen\s*$", "", prefix,
                            flags=re.IGNORECASE).strip(" .,")
            suffix = effect_text[search_transaction.end():].strip(" .,")
            if prefix:
                effects.extend(EffectFactory.create_effects(
                    prefix, targets, source_name))
            if search_transaction.group(1) and source_name:
                from .card_support import report_unsupported
                report_unsupported(
                    source_name,
                    "optional library-search decline is not policy-selectable",
                    severity="partial")
            from .ability_types import SearchLibraryEffect
            effects.append(SearchLibraryEffect(
                search_type="basic land", destination="battlefield", count=1,
                enters_tapped=True,
                search_target_controller=(
                    search_transaction.group(2).lower() == "their"),
                untap_land_threshold=(
                    4 if re.search(r"if you control four or more lands",
                                   suffix, re.IGNORECASE) else None)))
            suffix = re.sub(
                r"^then if you control four or more lands\s*,?\s*untap that land\.?",
                "", suffix, flags=re.IGNORECASE).strip(" .,")
            if suffix:
                effects.extend(EffectFactory.create_effects(
                    suffix, targets, source_name))
            return effects

        # Sample-card compound instructions whose parts share information or
        # must remain atomic at resolution.
        if re.fullmatch(r"\s*manifest dread\s*[.]?\s*", effect_text,
                        re.IGNORECASE):
            from .ability_types import ManifestDreadEffect
            return [ManifestDreadEffect()]

        if ((source_name or "").lower() == "turn inside out"
                or (re.search(r"target creature gets \+3/\+0 until end of turn",
                              effect_text, re.IGNORECASE)
                    and re.search(r"when it dies this turn,\s*manifest dread",
                                  effect_text, re.IGNORECASE))):
            from .ability_types import TurnInsideOutEffect
            return [TurnInsideOutEffect()]

        # "Exile target creature if it has mana value N or less", optionally
        # with the Corrupted override sentence (Anoint with Affliction). The
        # sentences share one target and one resolution decision, so they must
        # not be split into independent clauses.
        anoint_match = re.search(
            r"exile target creature if it has mana value (\d+) or less",
            effect_text, re.IGNORECASE)
        if anoint_match:
            from .ability_types import ConditionalExileEffect
            corrupted_match = re.search(
                r"corrupted\s*[–—-]?\s*exile that creature instead if its "
                r"controller has (\w+) or more poison counters",
                effect_text, re.IGNORECASE)
            threshold = None
            if corrupted_match:
                threshold = text_to_number(corrupted_match.group(1))
                if not isinstance(threshold, int) or threshold <= 0:
                    threshold = 3
            return [ConditionalExileEffect(
                max_mana_value=int(anoint_match.group(1)),
                corrupted_poison_threshold=threshold)]

        # Damage reflected from the triggering DAMAGED event. Screaming
        # Nemesis says "any other target" and is mandatory; Sensational
        # She-Hulk says "you may have ... deal ... to any target" and can use
        # herself. Keep the life-gain rider / optional once-per-turn choice in
        # the same atomic effect instead of losing them during clause splits.
        reflected_damage = re.search(
            r"(?P<optional>you may have\s+)?"
            r"(?:it|this creature|[a-z0-9 .'\u2019\-]+?)\s+deals?\s+"
            r"that much damage to any\s+(?P<other>other\s+)?target",
            effect_text, re.IGNORECASE)
        if reflected_damage:
            from .ability_types import ReflectDamageEffect
            rider = bool(re.search(r"can't gain life for the rest of the game",
                                   effect_text, re.IGNORECASE))
            return [ReflectDamageEffect(
                no_life_gain_rider=rider,
                exclude_source=bool(reflected_damage.group("other")),
                optional=bool(reflected_damage.group("optional")),
                once_each_turn=bool(re.search(
                    r"do this only once each turn",
                    effect_text, re.IGNORECASE)))]

        # Ouroboroid-style mass counters derive X from the source's current
        # power.  The generic comma splitter severs the ``where X`` rider,
        # which otherwise degrades both the amount and the mass-effect scope.
        source_power_counters = re.search(
            r"put x\s+([+\-]\d+/[+\-]\d+|[a-z]+)\s+counters?\s+on\s+"
            r"each\s+(tapped\s+)?creature\s+you control\s*,\s*where x is\s+"
            r"this creature['’]s power",
            effect_text, re.IGNORECASE)
        if source_power_counters:
            from .ability_types import AddCountersEffect
            target_type = (
                "each tapped creature you control"
                if source_power_counters.group(2)
                else "each creature you control")
            return [AddCountersEffect(
                source_power_counters.group(1), "source_power",
                target_type=target_type)]

        # "that source's controller sacrifices that many permanents"
        # (Phyrexian Obliterator): the count and paying player come from the
        # triggering damage event.
        if re.search(r"that source's controller sacrifices that many permanents",
                     effect_text, re.IGNORECASE):
            from .ability_types import SacrificeThatManyEffect
            return [SacrificeThatManyEffect()]

        # "Exile all creatures. Incubate X, where X is the number of creatures
        # exiled this way." (Sunfall): the incubated counter count depends on
        # the exile result, so both sentences are one atomic effect.
        if (re.search(r"exile all creatures", effect_text, re.IGNORECASE)
                and re.search(r"\bincubate x\b", effect_text, re.IGNORECASE)):
            from .ability_types import MassExileIncubateEffect
            return [MassExileIncubateEffect()]

        # Beza, the Bounding Spring: four independent opponent-comparison
        # branches evaluated at one resolution.
        if (re.search(r"create a treasure token if an opponent controls more lands than you",
                      effect_text, re.IGNORECASE)
                and re.search(r"gain 4 life if an opponent has more life than you",
                              effect_text, re.IGNORECASE)):
            from .ability_types import BezaEffect
            return [BezaEffect()]

        # Restless-land style self animation: "(Until end of turn, )this land
        # becomes a N/N <colors> <Subtype> creature (with <keywords>)(until end
        # of turn). It's still a land." Commas inside would be mangled by the
        # generic splitter, so parse the whole sentence here.
        self_animate = re.search(
            r"this land becomes a (\d+)/(\d+)\s+([^.]*?)\bcreature(?:s)?\b([^.]*)",
            effect_text, re.IGNORECASE)
        if self_animate:
            power, toughness = int(self_animate.group(1)), int(self_animate.group(2))
            descriptor = self_animate.group(3).strip().lower()
            trailer = self_animate.group(4).strip().lower()
            known_colors = ["white", "blue", "black", "red", "green"]
            colors = [c for c in known_colors if re.search(rf"\b{c}\b", descriptor)]
            subtype_words = [
                w for w in re.split(r"[\s,]+", descriptor)
                if w and w not in known_colors and w != "and"]
            kw_match = re.search(r"with ([\w\s,]+?)(?:\s+until end of turn)?$", trailer)
            keywords = []
            if kw_match:
                keywords = [k.strip() for k in kw_match.group(1).split(",") if k.strip()]
            from .ability_types import AnimateLandEffect
            return [AnimateLandEffect(
                power=power, toughness=toughness, duration="end_of_turn",
                colors=colors, subtypes=subtype_words, keywords=keywords,
                self_target=True)]

        if ((source_name or "").lower() == "torch the tower"
                or (re.search(r"torch the tower deals 2 damage", effect_text,
                              re.IGNORECASE)
                    and re.search(r"if this spell was bargained", effect_text,
                                  re.IGNORECASE))):
            from .ability_types import TorchTheTowerEffect
            return [TorchTheTowerEffect()]

        # "…deals N damage to target …. If that creature (or planeswalker)
        # would die this turn, exile it instead." (Obliterating Bolt,
        # Elspeth's Smite). The rider modifies the damage sentence, so both
        # stay one atomic effect.
        exile_rider = re.search(
            r"deals (\d+) damage to target [^.]+\.\s*"
            r"if that (creature or planeswalker|creature|permanent) would "
            r"die this turn, exile it instead",
            effect_text, re.IGNORECASE)
        if exile_rider:
            from .ability_types import DamageWithExileReplacementEffect
            rider_scope = exile_rider.group(2).lower()
            return [DamageWithExileReplacementEffect(
                int(exile_rider.group(1)),
                includes_planeswalkers=rider_scope != "creature")]

        if (re.search(
                r"destroy target creature, enchantment, or planeswalker",
                effect_text, re.IGNORECASE)
                and re.search(
                    r"its controller creates two map tokens",
                    effect_text, re.IGNORECASE)):
            from .ability_types import DestroyAndCreateMapsEffect
            return [DestroyAndCreateMapsEffect(count=2)]

        if re.search(
                r"shuffle\s+.+?\s+and target creature with a stun counter on it "
                r"into their owners['’] libraries",
                effect_text, re.IGNORECASE):
            from .ability_types import ShufflePermanentsIntoOwnersLibrariesEffect
            return [ShufflePermanentsIntoOwnersLibrariesEffect()]

        if (re.search(r"return it to the battlefield under its owner['’]s control",
                      effect_text, re.IGNORECASE)
                and re.search(r"it['’]s an enchantment", effect_text, re.IGNORECASE)):
            from .ability_types import ReturnAsEnchantmentEffect
            return [ReturnAsEnchantmentEffect()]

        emblem_match = re.search(
            r"(?:you\s+)?get(?:s)?\s+an emblem with\s+[“\"](.+?)[”\"]",
            effect_text, re.IGNORECASE | re.DOTALL)
        if emblem_match:
            from .ability_types import CreateEmblemEffect
            return [CreateEmblemEffect(emblem_match.group(1))]

        if re.search(r"target creature you control explores\b", effect_text,
                     re.IGNORECASE):
            from .ability_types import ExploreEffect
            return [ExploreEffect()]

        # Numeric die tables are one effect. Keep their result rows together
        # before generic dash/clause splitting can turn each row into an
        # unrelated ability.
        die_match = re.search(r"\broll(?:s)?\s+(?:a\s+)?d(\d+)\b", effect_text, re.IGNORECASE)
        outcome_pattern = re.compile(
            r"(?m)^\s*(\d+)(?:\s*[-\u2013\u2014]\s*(\d+))?\s*\|\s*(.+?)\s*$")
        outcome_matches = list(outcome_pattern.finditer(effect_text))
        if die_match and outcome_matches:
            from .ability_types import RollDieEffect
            prefix = effect_text[:die_match.start()].strip(" .,\n")
            prefix = re.sub(r"\bthen\s*$", "", prefix, flags=re.IGNORECASE).strip(" .,\n")
            if prefix:
                effects.extend(EffectFactory.create_effects(prefix, targets, source_name))
            common_text = effect_text[die_match.end():outcome_matches[0].start()].strip(" .\n")
            outcomes = []
            for match in outcome_matches:
                minimum = int(match.group(1))
                maximum = int(match.group(2) or minimum)
                outcomes.append((minimum, maximum, match.group(3).strip()))
            effects.append(RollDieEffect(
                int(die_match.group(1)), outcomes,
                pre_result_text=common_text, full_text=effect_text))
            return effects

        prepare_match = re.fullmatch(
            r"\s*you may exile (\w+|\d+) cards from your graveyard\.\s*"
            r"if you do,\s*this (?:creature|permanent) becomes prepared\.?\s*",
            effect_text, re.IGNORECASE | re.DOTALL)
        if prepare_match:
            from .ability_types import PrepareFromGraveyardEffect
            raw_count = prepare_match.group(1)
            count = (int(raw_count) if raw_count.isdigit()
                     else text_to_number(raw_count))
            return [PrepareFromGraveyardEffect(count)]

        # CR 603.12: preserve the prerequisite and its "when you do" rider as
        # one gated effect before generic sentence/clause splitting.
        reflexive_effect = EffectFactory._extract_reflexive_trigger(effect_text)
        if reflexive_effect:
            return [reflexive_effect]

        # "If this spell's additional cost was paid, X." resolves from the
        # cast record (Requiting Hex's blight). Carve the sentence out BEFORE
        # clause splitting; otherwise X parses as an unconditional effect.
        paid_rider = re.search(
            r"(?:^|(?<=\.))\s*if this spell(?:'|’)?s additional cost "
            r"was paid,\s*(?P<rider>[^.\n]+)\.?",
            effect_text, re.IGNORECASE)
        if paid_rider:
            from .ability_types import AdditionalCostPaidConditionalEffect
            remainder = (effect_text[:paid_rider.start()]
                         + effect_text[paid_rider.end():]).strip()
            effects = (EffectFactory.create_effects(
                remainder, targets, source_name) if remainder else [])
            nested = EffectFactory.create_effects(
                paid_rider.group("rider").strip(), targets, source_name)
            if nested:
                effects.append(AdditionalCostPaidConditionalEffect(nested))
            return effects

        # An exile followed by a delayed pronoun return is one linked action.
        # Parse it before generic delayed-trigger extraction so registration
        # happens only after the exact object has successfully entered exile.
        delayed_blink_surface = re.sub(
            r"\([^()]*\)", " ", effect_text).strip()
        delayed_blink_match = re.fullmatch(
            r"\s*(?:gift\b[^\n.;]*\r?\n\s*)?"
            r"exile target\s+"
            r"(?P<target_type>(?:(?:nonland|nontoken)\s+)?"
            r"(?:creature|artifact|enchantment|planeswalker|battle|permanent))"
            r"\s*\.\s*"
            r"(?P<gift_condition>if the gift (?:wasn't|was not) promised,\s*)?"
            r"return\s+(?:it|that card|that creature|that permanent)\s+"
            r"to the battlefield under its owner(?:'|\u2019)?s control\s+"
            r"with\s+(?:a|an|one)\s+"
            r"(?P<counter_type>[+\-]\d+/[+\-]\d+|[\w-]+)\s+"
            r"counter on it\s+at the beginning of (?:the |your )?next\s+"
            r"(?P<phase>end step|upkeep|end of combat|combat|cleanup(?: step)?|main phase)"
            r"\s*\.?\s*",
            delayed_blink_surface, re.IGNORECASE)
        if delayed_blink_match:
            from .ability_types import ExileThenDelayedReturnEffect
            return [ExileThenDelayedReturnEffect(
                target_type=delayed_blink_match.group("target_type"),
                counter_type=delayed_blink_match.group("counter_type"),
                phase_key=delayed_blink_match.group("phase"),
                return_unless_context_key=(
                    "gift_promised"
                    if delayed_blink_match.group("gift_condition") else None),
                effect_text=effect_text)]

        # Meld's "exile them, then meld them" is one indivisible action. If
        # generic clause splitting handles the exile first, the pair is gone
        # before the meld instruction can identify it.
        meld_match = re.search(r"\bmeld them into\s+([^.;]+)", effect_text, re.IGNORECASE)
        if meld_match:
            from .ability_types import MeldEffect
            return [MeldEffect(result_name=meld_match.group(1).strip())]

        # CR 603.7: pull out "at the beginning of the next <phase>" sentences
        # as DelayedTriggerEffect BEFORE clause splitting (see helper docstring).
        delayed_effects, effect_text = EffectFactory._extract_delayed_triggers(effect_text)
        effects.extend(delayed_effects)
        if not effect_text.strip(". "):
            return effects

        # Analyze the Pollen's optional casting cost changes the search
        # instruction that resolves. Preserve both branches as one effect so
        # the paid-cost flag from the stack context can select the right one.
        if (re.search(r"\bcollect evidence\s+\d+\b", effect_text, re.IGNORECASE)
                and re.search(
                    r"if evidence was collected,\s*instead search your library "
                    r"for a creature or land card", effect_text, re.IGNORECASE)
                and re.search(
                    r"search your library for a basic land card",
                    effect_text, re.IGNORECASE)):
            from .ability_types import SearchLibraryEffect
            effects.append(SearchLibraryEffect(
                search_type="basic land", destination="hand", count=1,
                evidence_search_type="creature or land"))
            return effects

        # Impulse draw is one instruction even when its permission sentence has
        # an internal comma ("Until end of turn, you may play that card").
        # Preserve it before the generic comma splitter can sever the grant.
        impulse_match = re.match(
            r"^\s*exile the top\s+(?:(\w+|\d+)\s+)?cards?\s+of\s+(?:your|their)\s+library\b",
            effect_text, re.IGNORECASE)
        if impulse_match and re.search(r"\bmay (?:play|cast)\b", effect_text, re.IGNORECASE):
            from .ability_types import ImpulseDrawEffect
            raw_count = impulse_match.group(1)
            count = 1
            if raw_count:
                count = int(raw_count) if raw_count.isdigit() else text_to_number(raw_count)
            if not isinstance(count, int) or count <= 0:
                count = 1
            duration = (
                "end_of_your_next_turn"
                if re.search(
                    r"until (?:the )?end of your next turn",
                    effect_text, re.IGNORECASE)
                else "end_of_turn")
            effects.append(ImpulseDrawEffect(
                count=count, duration=duration))
            return effects

        # "Exile ... until [this source] leaves" is one linked effect, not an
        # ordinary exile followed by a delayed trigger. Preserve the whole
        # instruction before generic sentence and conjunction splitting.
        bat_link = (
            re.search(r"look at target opponent(?:'|\u2019)s hand", effect_text, re.IGNORECASE)
            and re.search(
                r"you may exile a nonland card from it until .+? leaves the battlefield",
                effect_text, re.IGNORECASE))
        if bat_link:
            from .ability_types import LinkedExileEffect
            effects.append(LinkedExileEffect(
                target_type="nonland card", from_zone="hand", return_zone="hand",
                optional=True, choose_from_target_opponent_hand=True,
                effect_text=effect_text))
            return effects

        linked_target = re.search(
            r"\bexile target\s+(nonland permanent|creature|artifact|enchantment|permanent)\b"
            r"[^.]*?\buntil\b[^.]*?\bleaves the battlefield",
            effect_text, re.IGNORECASE)
        if linked_target:
            from .ability_types import LinkedExileEffect
            target_type = linked_target.group(1).lower()
            effects.append(LinkedExileEffect(
                target_type=target_type, from_zone="battlefield",
                return_zone="battlefield", effect_text=effect_text))
            return effects

        # ``Mill N. You may put ... from among the milled cards`` binds its
        # selection to the exact physical cards moved by the first sentence.
        # Preserve it before commas in a type union can fragment the text.
        linked_mill_text = re.sub(
            r'\s*\([^()]*\)\s*', ' ', effect_text).strip()
        linked_mill = re.search(
            r"\bmill\s+(\d+|x|a|an|one|two|three|four|five|six|seven|eight|nine|ten)"
            r"\s+cards?\s*\.\s*you may put\s+(?:a|an|one)\s+(.+?)\s+card\s+"
            r"from among\s+(?:the\s+)?(?:milled cards?|cards? milled(?: this way)?)\s+"
            r"into your hand\s*\.?",
            linked_mill_text, re.IGNORECASE | re.DOTALL)
        linked_mill_suffix = (
            linked_mill_text[linked_mill.end():].strip(" .")
            if linked_mill else "")
        supported_mill_suffix = (
            not linked_mill_suffix
            or re.fullmatch(
                r"you gain\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+life",
                linked_mill_suffix, re.IGNORECASE))
        if linked_mill and supported_mill_suffix:
            raw_count = linked_mill.group(1).lower()
            count = ('x' if raw_count == 'x'
                     else text_to_number(raw_count))
            allowed_text = linked_mill.group(2).lower()
            permanent_types = (
                "artifact", "battle", "creature", "enchantment", "land",
                "planeswalker")
            allowed_types = (["permanent"] if "permanent" in allowed_text
                             else [card_type for card_type in permanent_types
                                   if re.search(rf"\b{card_type}s?\b", allowed_text)])
            if isinstance(count, int) and count > 0 and allowed_types:
                from .ability_types import MillThenChooseEffect
                effects.append(MillThenChooseEffect(
                    count=count, allowed_types=allowed_types, optional=True,
                    effect_text=linked_mill.group(0).strip()))
                if linked_mill_suffix:
                    effects.extend(EffectFactory.create_effects(
                        linked_mill_suffix, targets, source_name))
                return effects

        # Result-linked bounce/counter instructions are one resolution unit:
        # the counter is created only if the optional target was returned.
        if (re.search(
                r"return up to one target\s+.+?\s+permanent you control\s+"
                r"to its owner(?:'|\u2019)s hand",
                effect_text, re.IGNORECASE | re.DOTALL)
                and re.search(
                    r"if a permanent was returned this way,\s*put a "
                    r"\+1/\+1 counter on this creature",
                    effect_text, re.IGNORECASE | re.DOTALL)):
            from .ability_types import ReturnThenAddCounterEffect
            effects.append(ReturnThenAddCounterEffect(effect_text))
            return effects

        # Preserve comma-separated keyword menus as one semantic clause; the
        # general conjunction splitter below would otherwise turn the second
        # and later options into unrelated effects.
        keyword_choice = re.search(
            r"^(?P<target>.+?)\s+gains?\s+your choice of\s+"
            r"(?P<options>.+?)(?:\s+until end of turn)?\s*\.?$",
            effect_text.strip(), re.IGNORECASE | re.DOTALL)
        if keyword_choice:
            from .ability_types import KeywordChoiceGrantEffect
            raw_options = re.sub(
                r"\s+(?:or|and)\s+", ",", keyword_choice.group("options"),
                flags=re.IGNORECASE)
            options = [
                option.strip(" .,;").lower()
                for option in raw_options.split(",")
                if option.strip(" .,;")]
            if len(options) >= 2:
                return [KeywordChoiceGrantEffect(
                    options,
                    duration=("end_of_turn" if re.search(
                        r"until end of turn", effect_text, re.IGNORECASE)
                              else "permanent"),
                    targeting_text=keyword_choice.group("target").strip())]

        processed_clauses = []
        # Basic clause splitting. Most multi-sentence effects are parsed as one
        # semantic unit (copy, impulse, dig), so only split a plain sentence
        # boundary when the next sentence puts counters on the prior target.
        split_pattern = (r'\s*,\s*(?:and\s+)?(?:then\s+)?|'
                         r'\s+and\s+(?:then\s+)?|\s+then\s+|'
                         r'(?<=[.;])\s+then\s+|'
                         r'(?<=\.)\s+(?=(?i:put\b.*\bcounters?\s+on\s+(?:it|that\b)))|'
                         r'(?<=\.)\s+(?=(?i:untap\s+(?:it|that)\b))|'
                         r'(?<=\.)\s+(?=(?i:create\s+(?:a|an|one)\s+(?:cursed|monster|royal|sorcerer|young\s+hero|virtuous|wicked)\s+role token\b))|'
                         r'\s*—\s*|\s*\u2014\s*')
        split_text = re.sub(r'\s*\([^()]*\)\s*', ' ', effect_text).strip('. ')
        # A comma that introduces the characteristics of a named legendary
        # token is part of the same instruction. Protect that delimiter and
        # any comma inside the printed name (for example, ``Primo, the
        # Indivisible``), but leave a comma after ``token`` available to split
        # a real follow-up action.
        token_noun_comma_marker = "__TOKEN_NOUN_COMMA__"
        named_token_preamble = re.compile(
            r"(?P<prefix>\bcreate(?:s)?\s+)"
            r"(?P<name>(?:(?!\bcreate(?:s)?\b|[.;\n]).)+?),"
            r"(?P<descriptor>\s+(?:a|an)\s+legendary\s+\d+/\d+\b)",
            re.IGNORECASE)

        def protect_named_token_preamble(match):
            protected_name = match.group("name").replace(
                ",", token_noun_comma_marker)
            return (match.group("prefix") + protected_name
                    + token_noun_comma_marker + match.group("descriptor"))

        split_text = named_token_preamble.sub(
            protect_named_token_preamble, split_text)

        # A color conjunction inside a token's characteristic noun phrase is
        # not an instruction boundary. Protect only the ``and`` between two
        # color words when it is downstream of ``create`` and upstream of the
        # corresponding ``token`` noun. Genuine sequences such as ``create a
        # token and draw a card`` still reach the ordinary conjunction split.
        token_color_and_marker = "__TOKEN_COLOR_AND__"
        color_word = r"(?:white|blue|black|red|green|colorless)"
        token_color_conjunction = re.compile(
            rf"(\bcreate(?:s)?\b[^.;]*?\b{color_word})\s+and\s+"
            rf"({color_word}\b)(?=[^.;]*\btokens?\b)",
            re.IGNORECASE)
        split_text = token_color_conjunction.sub(
            rf"\1 {token_color_and_marker} \2", split_text)
        parts = re.split(split_pattern, split_text)
        # Carry only an explicit leading player subject into a grammatically
        # subjectless player-action fragment.  This repairs shapes such as
        # ``each opponent discards ... and loses ...`` without binding object
        # conjunctions such as ``destroy target creature and gain 3 life``.
        carried_subject = None
        subjectless_player_verb = re.compile(
            r"^(?:loses?|gains?|draws?|discards?|mills?|sacrifices?)\b",
            re.IGNORECASE)
        for raw_part in parts:
            part = raw_part.replace(token_color_and_marker, "and")
            part = part.replace(token_noun_comma_marker, ",").strip()
            if not part:
                continue
            subject_match = re.match(
                r"^(each opponents?|each other player|each players?|"
                r"target player|target opponent|you)\b",
                part, re.IGNORECASE)
            if subject_match:
                carried_subject = subject_match.group(1).lower()
            elif carried_subject and subjectless_player_verb.match(part):
                part = f"{carried_subject} {part}"
            processed_clauses.append(part)
        if not processed_clauses: processed_clauses = [effect_text] # Use full text if split fails

        # Assuming these are imported at the module level of ability_utils.py:
        # (Relative import assumed)
        from .ability_types import (AbilityEffect, DrawCardEffect, GainLifeEffect, DamageEffect,
            CounterSpellEffect, CreateTokenEffect, CreateRoleEffect, DestroyEffect, ExileEffect,
            DiscardEffect, MillEffect, TapEffect, UntapEffect, BuffEffect,
            SearchLibraryEffect, AddCountersEffect, ReturnToHandEffect,
            ScryEffect, SurveilEffect, CopySpellEffect, TransformEffect, SetDayNightEffect, FightEffect,
            ImpulseDrawEffect, LoseLifeEffect, GainKeywordEffect,
            SacrificeEffect, SacrificeSourceEffect, ReanimateEffect,
            ReturnSourceFromGraveyardEffect,
            AddManaEffect, ControlEffect,
            RegenerateEffect, DigEffect, PutOnLibraryEffect,
            ShuffleGraveyardEffect, PreventDamageEffect,
            UnpreventableDamageEffect,
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

            # This is a rules-changing instruction, not a prevention shield.
            # Append it independently so a combined clause such as
            # Impractical Joke's can still produce its later DamageEffect.
            if has_unpreventable_damage_instruction(clause_lower):
                effects.append(UnpreventableDamageEffect())

            source_sacrifice = re.search(
                r"\bsacrifice\s+this\s+"
                r"(artifact|battle|creature|enchantment|land|permanent|token)\b",
                clause_lower)

            earthbend_match = re.search(r"\bearthbend\s+(\d+)\b", clause_lower)
            if earthbend_match:
                from .ability_types import EarthbendEffect
                effects.append(EarthbendEffect(int(earthbend_match.group(1))))
                continue

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
                if "target player" in clause_lower: ts = "target_player"
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
                if "target player" in clause_lower: target_specifier = "target_player"
                elif "opponent" in clause_lower: target_specifier = "opponent"
                elif "each player" in clause_lower: target_specifier = "each_player"
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
            elif has_damage_prevention_instruction(clause_lower):
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
                elif "damage equal to its power" in clause_lower:
                    amount = (
                        "previous_target_power"
                        if re.search(
                            r"\bit deals damage equal to its power\b",
                            clause_lower)
                        else "source_last_known_power")
                target_desc = EffectFactory._extract_target_description(clause_lower) or "any target" # Changed default
                target_type = "any target" # Default
                if re.search(
                        r"\beach creatures? your opponents? control\b",
                        clause_lower):
                    target_type = "each creature your opponents control"
                elif re.search(
                        r"\beach creatures? you control\b", clause_lower):
                    target_type = "each creature you control"
                elif re.search(r"\beach creatures?\b", clause_lower):
                    target_type = "each creature"
                elif re.search(
                        r"\b(?:creature\s+(?:or|and/or)\s+planeswalker|"
                        r"planeswalker\s+(?:or|and/or)\s+creature)\b",
                        clause_lower):
                    target_type = "creature_or_planeswalker"
                elif "creature or player" in target_desc or "any target" in target_desc: target_type="any target"
                elif "each opponent" in target_desc: target_type="each opponent"
                elif "each creature" in target_desc: target_type="each creature"
                elif "each player" in target_desc: target_type="each player"
                elif "creature" in target_desc: target_type="creature"
                elif "player" in target_desc or "opponent" in target_desc: target_type="player"
                elif "planeswalker" in target_desc: target_type="planeswalker"
                elif "battle" in target_desc: target_type="battle"
                created_effect = DamageEffect(amount, target_type=target_type) # Pass 'x' or number

            # Destroy
            elif re.search(r"\b(destroy(?:s)?)\b\s+(target|all|each)", clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent" # Default if specific target word used
                 target_type = "permanent"
                 # Normalize the target description slightly for easier checks
                 norm_target_desc = target_desc.replace('-',' ')
                 if "artifact or enchantment" in norm_target_desc:
                     target_type = "artifact_or_enchantment"
                 elif "creature" in norm_target_desc: target_type = "creature"
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

            # Exile ("exile target X", "exile up to one target X", "exile all X")
            elif re.search(r"\b(exile(?:s)?)\b\s+(?:up to (?:one|two|three|\d+)\s+)?(target|all|each)", clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                 target_type = "permanent"
                 norm_target_desc = target_desc.replace('-',' ')
                 # Add specific type checks similar to Destroy
                 if "target creature or vehicle" in clause_lower:
                     target_type = "creature_or_vehicle"
                 elif "artifact or enchantment" in clause_lower:
                     target_type = "artifact_or_enchantment"
                 elif "creature" in norm_target_desc: target_type = "creature"
                 elif "artifact" in norm_target_desc: target_type = "artifact"
                 elif "enchantment" in norm_target_desc: target_type = "enchantment"
                 elif "land" in norm_target_desc: target_type = "land"
                 elif "planeswalker" in norm_target_desc: target_type = "planeswalker"
                 elif "card" in norm_target_desc: target_type = "card" # Card in other zones
                 elif "spell" in norm_target_desc: target_type = "spell" # Stack target
                 # Handle "all/each" variations
                 if re.search(r"\b(all|each)\s+creatures?\b", clause_lower): target_type = "all creatures"
                 # ... add other "all X" / "each X" types if needed for exile ...
                 zone_match = re.search(
                     r"\bfrom\s+(?:(?:an opponent's|the|a|your)\s+)?"
                     r"(?:single\s+)?"
                     r"(battlefield|graveyard|hand|library|stack|exile)\b",
                     clause_lower)
                 zone = zone_match.group(1) if zone_match else "battlefield"
                 optional_match = re.search(
                     r"\bup to\s+(one|two|three|\d+)\s+target\b",
                     clause_lower)
                 optional_count = 1
                 if optional_match:
                     count_token = optional_match.group(1)
                     optional_count = (
                         int(count_token) if count_token.isdigit()
                         else {"one": 1, "two": 2, "three": 3}[count_token])
                 created_effect = ExileEffect(
                     target_type=target_type, zone=zone,
                     optional=bool(optional_match),
                     max_targets=optional_count)

            # Explicit token-copy grammar must never fall through to ordinary
            # CreateTokenEffect, which would invent a vanilla 1/1. The exact
            # supported target-copy template remains faithful only when the
            # full instruction has no copy exception. Inspecting effect_text
            # is essential because the clause splitter can sever ", except
            # ..." from the copy prefix (Molten Duplication).
            elif re.search(
                    r"\bcreate(?:s)?\b[^.;\n]*?\btokens?\s+"
                    r"(?:that(?:['\u2019]s|\s+is)\s+an?\s+copy\s+of|"
                    r"that\s+are\s+(?:an?\s+copy|(?:each\s+)?copies)\s+of)\b",
                    clause_lower):
                 exact_target_copy = re.search(
                     r"\bcreate(?:s)?\s+a\s+token\s+"
                     r"that(?:['\u2019]s|\s+is)\s+a\s+copy\s+of\s+"
                     r"target\s+artifact\s+or\s+creature\s+you\s+control\b",
                     clause_lower)
                 if (exact_target_copy
                         and not re.search(
                             r"\bexcept\b", effect_text, re.IGNORECASE)):
                     from .ability_types import CreateTokenCopyOfTargetEffect
                     created_effect = CreateTokenCopyOfTargetEffect(
                         allowed_types={"artifact", "creature"},
                         controller_only=True)
                 else:
                     from .ability_types import UnsupportedEffect
                     reason = (
                         "unsupported token-copy instruction: "
                         f"{clause_clean[:80]}")
                     if source_name:
                         from .card_support import report_unsupported
                         report_unsupported(
                             source_name, reason, severity="partial")
                     created_effect = UnsupportedEffect(
                         clause_clean, reason=reason, severity="partial")

            # Printed-value token copy of a chosen permanent.  This must
            # precede the generic token branch, which otherwise invents a
            # vanilla 1/1 for Three Steps Ahead.
            elif re.search(
                    r"\bcreate(?:s)?\s+a\s+token\s+that['’]s\s+a\s+copy\s+of\s+"
                    r"target\s+artifact\s+or\s+creature\s+you\s+control\b",
                    clause_lower):
                 from .ability_types import CreateTokenCopyOfTargetEffect
                 created_effect = CreateTokenCopyOfTargetEffect(
                     allowed_types={"artifact", "creature"},
                     controller_only=True)

            # Treasure is a predefined noncreature artifact with a mana
            # ability. Keep it off the generic 1/1 creature-token path.
            elif (re.search(
                    r"\bcreate(?:s)?\s+"
                    r"(?:a|an|one|two|three|four|five|\d+)\s+"
                    r"(?:tapped\s+)?treasure tokens?\b",
                    clause_lower)
                  and "for each" not in clause_lower
                  and "that many" not in clause_lower):
                 treasure_match = re.search(
                     r"\bcreate(?:s)?\s+"
                     r"(a|an|one|two|three|four|five|\d+)\s+"
                     r"(?:tapped\s+)?treasure tokens?\b",
                     clause_lower)
                 from .ability_types import CreateTreasureEffect
                 created_effect = CreateTreasureEffect(
                     count=text_to_number(treasure_match.group(1)),
                     enters_tapped=bool(re.search(
                         r"\bcreate(?:s)?\b.*\btapped\s+treasure\b",
                         clause_lower)))

            # Role tokens are Aura enchantments created already attached to a
            # creature, not generic 1/1 creature tokens.
            elif re.search(
                    r"\bcreate(?:s)?\s+(?:a|an|one)\s+(cursed|monster|royal|sorcerer|young\s+hero|virtuous|wicked)\s+role token\s+attached to\s+(.+)$",
                    clause_lower):
                 role_match = re.search(
                     r"\bcreate(?:s)?\s+(?:a|an|one)\s+(cursed|monster|royal|sorcerer|young\s+hero|virtuous|wicked)\s+role token\s+attached to\s+(.+)$",
                     clause_lower)
                 created_effect = CreateRoleEffect(
                     role_match.group(1), attachment_text=role_match.group(2).strip(". "))

            # Map is a noncreature artifact token with a rules-bearing
            # activated ability, so it cannot use the generic 1/1 token path.
            elif re.search(r"\bcreate(?:s)?\s+(?:a|an|one|two|three|four|five|\d+)\s+food tokens?\b",
                           clause_lower):
                 count_match = re.search(
                     r"create(?:s)?\s+(a|an|one|two|three|four|five|\d+)\s+food",
                     clause_lower)
                 count = text_to_number(count_match.group(1)) if count_match else 1
                 from .ability_types import CreateFoodEffect
                 created_effect = CreateFoodEffect(count=count)

            # Map is also a noncreature artifact token with a rules-bearing
            # activated ability.
            elif re.search(r"\bcreate(?:s)?\s+(?:a|an|one|two|\d+)\s+map tokens?\b",
                           clause_lower):
                 count_match = re.search(
                     r"create(?:s)?\s+(a|an|one|two|three|four|five|\d+)\s+map",
                     clause_lower)
                 count = text_to_number(count_match.group(1)) if count_match else 1
                 from .ability_types import CreateMapEffect
                 created_effect = CreateMapEffect(count=count)

            # Keyword actions reached through ordinary spell/ability
            # resolution. Dedicated policy slots are aliases of this path.
            elif re.fullmatch(
                    r"\s*(?:you may have\s+)?(?:it|he|she|this creature|"
                    r"[\w.'’ -]+|target(?:\s+\w+){0,3}\s+creature"
                    r"(?: you control)?)\s+connives?\.?(?:\s+do this only "
                    r"once each turn\.?)?(?:\s+activate only during your "
                    r"turn\.?)?\s*",
                    clause_lower):
                 from .ability_types import ConniveEffect
                 created_effect = ConniveEffect(
                     targeted=bool(re.search(
                         r"target\s+(?:\w+\s+){0,3}creature",
                         clause_lower)),
                     optional="may" in clause_lower,
                     once_each_turn="once each turn" in clause_lower)

            elif re.match(r"^\s*airbend\b", clause_lower):
                 from .ability_types import AirbendEffect
                 airbend_target = re.search(
                     r"airbend\s+(.+?)(?:\.|$)", clause_clean,
                     re.IGNORECASE)
                 created_effect = AirbendEffect(
                     target_description=(airbend_target.group(1).strip()
                                         if airbend_target else
                                         "up to one target creature"))

            elif re.fullmatch(
                    r"\s*(?:you may\s+)?suspect(?: up to one| one)?\s+"
                    r"(?:other\s+)?(?:target\s+)?(?:enchanted\s+)?creature"
                    r"(?: you control)?\.?\s*|\s*(?:you may\s+)?suspect it\.?",
                    clause_lower):
                 from .ability_types import SuspectEffect
                 created_effect = SuspectEffect(
                     targeted=("target" in clause_lower
                               or "enchanted creature" in clause_lower),
                     optional=("may" in clause_lower
                               or "up to" in clause_lower),
                     attached="enchanted creature" in clause_lower)

            elif re.fullmatch(
                    r"\s*(?:you\s+)?investigate(?:\s+(twice|two times))?"
                    r"[.!]?\s*", clause_lower):
                 from .ability_types import InvestigateEffect
                 created_effect = InvestigateEffect(
                     count=2 if re.search(
                         r"\b(?:twice|two times)\b", clause_lower) else 1)

            elif re.fullmatch(
                    r"\s*investigate once for each opponent who has more "
                    r"cards in hand than you[.!]?\s*", clause_lower):
                 from .ability_types import InvestigateEffect
                 created_effect = InvestigateEffect(
                     count='opponents_more_cards')

            elif re.fullmatch(
                    r"\s*investigate x times, where x is the total number of "
                    r"creatures those players control[.!]?\s*", clause_lower):
                 from .ability_types import InvestigateEffect
                 created_effect = InvestigateEffect(
                     count='target_players_creatures')

            elif re.fullmatch(
                    r"\s*discover x,\s*where x is (?:that|the) spell's mana "
                    r"value[.!]?\s*", clause_lower):
                 from .ability_types import DiscoverEffect
                 created_effect = DiscoverEffect('spell_mana_value')

            elif re.fullmatch(
                    r"\s*discover again for the same value[.!]?(?:\s+this "
                    r"ability triggers only once each turn[.!]?)?\s*",
                    clause_lower):
                 from .ability_types import DiscoverEffect
                 created_effect = DiscoverEffect('same')

            elif re.search(r"\bamass(?:\s+\w+)?\s+(\d+)\b", clause_lower):
                 from .ability_types import AmassEffect
                 amount_match = re.search(r"\bamass(?:\s+\w+)?\s+(\d+)\b", clause_lower)
                 created_effect = AmassEffect(int(amount_match.group(1)))

            elif re.fullmatch(
                    r"venture(?:s)?(?: into the dungeon)?[.!]?",
                    clause_lower.strip()):
                 from .ability_types import VentureEffect
                 created_effect = VentureEffect()

            elif re.fullmatch(r"adapt\s+(\d+)[.!]?", clause_lower.strip()):
                 from .ability_types import AdaptEffect
                 amount_match = re.search(r"\d+", clause_lower)
                 created_effect = AdaptEffect(int(amount_match.group(0)))

            elif re.search(r"\bgoad\s+target\s+creature\b", clause_lower):
                 from .ability_types import GoadEffect
                 created_effect = GoadEffect()

            elif re.fullmatch(r"explore[.!]?", clause_lower.strip()):
                 from .ability_types import ExploreEffect
                 created_effect = ExploreEffect()

            # Explore
            elif re.fullmatch(
                    r"\s*(?:it|he|she|this creature)\s+explores x times"
                    r"[.!]?\s*", clause_lower):
                 from .ability_types import ExploreEffect
                 created_effect = ExploreEffect(count='x')

            elif (re.search(r"\b(?:target\s+)?creature\b.*\bexplores\b",
                            clause_lower)
                  or re.fullmatch(
                      r"\s*(?:it|he|she)\s+explores(?:\s+again)?[.!]?\s*",
                      clause_lower)):
                 from .ability_types import ExploreEffect
                 created_effect = ExploreEffect(
                     targeted="target" in clause_lower)

            elif (endure_clause := re.fullmatch(
                    r"\s*(?P<subject>it|this creature|[\w'’ ,-]+)\s+"
                    r"endures?\s+(?P<value>\d+|x)(?P<counter_value>,\s*where "
                    r"x is the number of counters on this creature)?[.!]?\s*",
                    clause_lower)):
                 from .ability_types import EndureEffect
                 created_effect = EndureEffect(
                     endure_clause.group('value'),
                     subject_event=(
                         endure_clause.group('subject').strip() == 'it'),
                     value_from_source_counters=bool(
                         endure_clause.group('counter_value')))

            # Additional combat phase (CR 505.5a): "After this phase, there is
            # an additional combat phase." The comma splitter usually severs
            # the sentence, so match the surviving core phrase.
            elif re.search(r"\ban additional combat phase\b", clause_lower) or \
                    re.search(r"\bthere is an additional combat\b", clause_lower):
                from .ability_types import AdditionalCombatPhaseEffect
                created_effect = AdditionalCombatPhaseEffect(
                    followed_by_main=(
                        "additional main phase" in effect_text.lower()))

            # Create Token
            elif re.search(r"\b(create(?:s)?)\b", clause_lower) and "token" in clause_lower:
                 count_match = re.search(r"create(?:s)?\s+(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+", clause_lower)
                 that_many_count = bool(re.search(
                     r"\bcreate(?:s)?\s+that many\b", clause_lower))
                 # A pronoun count is never implicitly one. Its antecedent
                 # belongs to the resolving event (discarded cards, damage,
                 # mana symbols, and so on), so CreateTokenEffect resolves it
                 # from the frozen context or fails closed.
                 count = (0 if that_many_count else
                          text_to_number(count_match.group(1))
                          if count_match else 1)
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

                 # Keep a printed token name separate from its permanent
                 # types. Stop before characteristic riders such as "that is
                 # every basic land type" or "with flying".
                 token_name = None
                 explicit_card_types = None
                 explicit_subtypes = None
                 created_named_token_match = re.search(
                     r"\bcreate(?:s)?\s+"
                     r"(?P<name>(?:(?!\bcreate(?:s)?\b|[.;\n]).)+?),\s+"
                     r"(?:a|an)\s+legendary\s+"
                     r"(?P<power>\d+)/(?P<toughness>\d+)\s+"
                     r"(?P<descriptor>.+?)\s+creature\s+tokens?\b",
                     clause_clean, re.IGNORECASE)
                 if created_named_token_match:
                     token_name = EffectFactory._restore_printed_name_case(
                         created_named_token_match.group("name"))
                     power = safe_int(
                         created_named_token_match.group("power"))
                     toughness = safe_int(
                         created_named_token_match.group("toughness"))
                     descriptor_words = re.findall(
                         r"[A-Za-z][A-Za-z'\-]*",
                         created_named_token_match.group("descriptor"))
                     explicit_card_types = ["creature"]
                     explicit_subtypes = [
                         word.lower() for word in descriptor_words
                         if word.lower() not in known_colors
                         and word.lower() != "and"]

                 # Generic creature-token descriptors may contain any number
                 # of printed subtypes (``Human Soldier``, ``Dinosaur
                 # Dragon``). The old one-word look-behind below retained
                 # only the final word. Parse the complete characteristic
                 # phrase and exclude only grammar/card-type modifiers; this
                 # also preserves the named/multicolor path above.
                 if explicit_subtypes is None:
                     generic_creature_match = re.search(
                         r"\bcreate(?:s)?\s+"
                         r"(?:a|an|one|two|three|four|five|six|seven|"
                         r"eight|nine|ten|\d+|x|that many)\s+"
                         r"(?P<descriptor>.+?)\s+creature\s+tokens?\b",
                         clause_clean, re.IGNORECASE)
                     if generic_creature_match:
                         descriptor_words = re.findall(
                             r"[A-Za-z][A-Za-z'\-]*",
                             generic_creature_match.group("descriptor"))
                         descriptor_word_set = {
                             word.casefold() for word in descriptor_words}
                         token_descriptor_modifiers = {
                             "a", "an", "and", "artifact", "colorless",
                             "enchantment", "legendary", "tapped", "the",
                             *known_colors,
                         }
                         parsed_subtypes = [
                             word.lower() for word in descriptor_words
                             if word.lower() not in token_descriptor_modifiers]
                         if parsed_subtypes:
                             explicit_subtypes = parsed_subtypes
                             explicit_card_types = ["creature"]
                             for additional_type in (
                                     "artifact", "enchantment"):
                                 if additional_type in descriptor_word_set:
                                     explicit_card_types.append(
                                         additional_type)
                 named_token_match = re.search(
                     r"\btokens?\s+named\s+(.+?)"
                     r"(?=\s+(?:that|with)\b|[.,;]|$)",
                     clause_clean, re.IGNORECASE)
                 if named_token_match and token_name is None:
                     # TriggeredAbility stores its parsed effect lowercase, so
                     # restore ordinary printed-name capitalization here.
                     token_name = EffectFactory._restore_printed_name_case(
                         named_token_match.group(1))

                 if re.search(r"\bland\s+tokens?\b", clause_lower):
                     explicit_card_types = ["land"]
                     # Card uses numeric P/T fields for every object; zero is
                     # the neutral representation for a noncreature token.
                     power, toughness = 0, 0
                     if "every basic land type" in clause_lower:
                         explicit_subtypes = [
                             "plains", "island", "swamp", "mountain",
                             "forest",
                         ]

                 # Extract creature type/name - This is the hardest part generically
                 token_name_type = "Creature" # Default
                 # Remove count, p/t, colors, keywords text to isolate name/type text
                 text_for_type = clause_lower
                 if count_match: text_for_type = text_for_type.replace(count_match.group(0), "")
                 if pt_match: text_for_type = text_for_type.replace(pt_match.group(0), "")
                 if kw_match: text_for_type = text_for_type.replace(kw_match.group(0), "")
                 for color_word in known_colors: text_for_type = text_for_type.replace(color_word,"")
                 # Try to find "X creature token" or another common TYPE
                 # token. A printed token name is carried independently above.
                 type_match = re.search(r"(\w+)\s+(artifact\s+)?(creature|artifact|treasure|food|clue)\s+token", text_for_type) # Basic common types
                 if type_match:
                      prefix = type_match.group(1)
                      base = type_match.group(3)
                      if prefix and prefix not in ['a','an','the']: token_name_type = prefix.capitalize()
                      elif base: token_name_type = base.capitalize()
                      # Refine: Might need better identification based on position relative to P/T etc.

                 # Determine final type line components
                 is_legendary = "legendary" in clause_lower
                 # "for each X" scales the token count at resolution (Domain
                 # counts, permanents you control, etc.).
                 count_expr = "that many" if that_many_count else None
                 for_each_match = re.search(r"tokens?\s+for each\s+(.+?)(?:\.|,|$)", clause_lower)
                 if for_each_match:
                     count_expr = for_each_match.group(1).strip()
                 # ... construct full token_data dict for the game state ...
                 # Using simplified CreateTokenEffect for now
                 created_effect = CreateTokenEffect(
                     power, toughness, token_name_type, count, keywords,
                     colors=colors, is_legendary=is_legendary,
                     count_expr=count_expr,
                     enters_tapped=bool(re.search(
                         r"\bcreate(?:s)?\b.*\btapped\b.*\btokens?\b",
                         clause_lower)),
                     token_name=token_name,
                     card_types=explicit_card_types,
                     subtypes=explicit_subtypes)


            # A source-bound "this card" instruction is not targeted and
            # follows the exact graveyard object that created the trigger.
            elif re.search(
                    r"return\s+this\s+card\s+from\s+your\s+graveyard\s+to\s+the\s+battlefield",
                    clause_lower):
                created_effect = ReturnSourceFromGraveyardEffect(
                    enters_tapped="tapped" in clause_lower)

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
            elif source_sacrifice:
                created_effect = SacrificeSourceEffect(
                    permanent_type=source_sacrifice.group(1))

            elif re.search(
                    r"sacrifices?\s+(?:a|an|another|one|two|three|four|five|"
                    r"six|seven|eight|nine|ten|\d+)\s+", clause_lower):
                m = re.search(
                    r"sacrifices?\s+(a|an|another|one|two|three|four|five|six|"
                    r"seven|eight|nine|ten|\d+)\s+"
                    r"(.+?)(?=\s*,|\s*;|\s*\.|\s+then\b|$)",
                    clause_lower)
                ptype = m.group(2).strip() if m else "creature"
                cnt_raw = m.group(1) if m else "a"
                if cnt_raw in ("a", "an", "another", "one"): cnt = 1
                elif cnt_raw.isdigit(): cnt = int(cnt_raw)
                else: cnt = text_to_number(cnt_raw)
                if not isinstance(cnt, int) or cnt <= 0: cnt = 1
                if cnt_raw == "another":
                    ptype = f"another {ptype}"
                if "each opponent" in clause_lower or "each other player" in clause_lower:
                    who = "each_opponent"
                elif "each player" in clause_lower:
                    who = "each_player"
                elif "target player" in clause_lower or "that player" in clause_lower:
                    who = "target_player"
                else:
                    who = "controller"
                created_effect = SacrificeEffect(
                    permanent_type=ptype, who=who, count=cnt,
                    optional=bool(re.search(r"\bmay\s+sacrifice", clause_lower)))

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
            elif re.search(r"distribute\s+(\w+|\d+)?\s*\+1/\+1 counters?", clause_lower):
                num_m = re.search(r"distribute\s+(\w+|\d+)", clause_lower)
                n = 1
                if num_m and num_m.group(1):
                    n = int(num_m.group(1)) if num_m.group(1).isdigit() else text_to_number(num_m.group(1))
                if not isinstance(n, int) or n <= 0: n = 1
                from .ability_types import DistributeCountersEffect
                distribution_text = re.search(
                    r"distribute\s+[^.]*?\btarget creatures?",
                    effect_text, re.IGNORECASE)
                created_effect = DistributeCountersEffect(
                    "+1/+1", count=n,
                    targeting_text=(distribution_text.group(0).strip()
                                    if distribution_text else clause.strip()))

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

            # Keyword choice grant: "gains your choice of <kw1> or <kw2>"
            # (Manifold Mouse). The pick is exposed through PHASE_CHOOSE
            # instead of auto-resolving; must precede the plain-grant branch.
            elif re.search(r"gains?\s+your choice of\s+", clause_lower):
                cm = re.search(
                    r"^(?P<target>.+?)\s+gains?\s+your choice of\s+"
                    r"(?P<options>.+?)(?:\s+until end of turn)?\s*\.?$",
                    clause_lower)
                if cm:
                    from .ability_types import KeywordChoiceGrantEffect
                    duration = ("end_of_turn"
                                if "until end of turn" in clause_lower
                                else "permanent")
                    raw_options = re.sub(
                        r"\s+(?:or|and)\s+", ",", cm.group("options"))
                    options = [
                        option.strip(" .,;")
                        for option in raw_options.split(",")
                        if option.strip(" .,;")]
                    if len(options) >= 2:
                        created_effect = KeywordChoiceGrantEffect(
                            options, duration=duration,
                            targeting_text=cm.group("target").strip())

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

            # Reveal hand: "target player/opponent reveals their hand".
            elif re.search(r"(target player|target opponent|each player|you)\s+reveals?\s+(their|his or her|your)\s+hand", clause_lower) \
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
                    # Refine target_type based on target_desc (also check the
                    # clause itself: adjectives like "attacking" make the
                    # extracted description drop the "creature" noun).
                    source_subjects = {
                        source_key,
                        source_key.split(",", 1)[0].strip(),
                    } - {""}
                    names_source = any(re.match(
                        rf"^\s*{re.escape(subject)}\s+gets\b",
                        clause_lower)
                        for subject in source_subjects)
                    if (re.search(r"\bthis creature gets\b", clause_lower)
                            or names_source):
                        target_type = "self"
                    elif ("target creature" in target_desc
                            or ("target" in clause_lower and "creature" in target_desc)
                            or re.search(r"\btarget\s+(?:[\w-]+\s+){0,3}creature\b", clause_lower)):
                        target_type = "target creature"
                    elif "creatures you control" in target_desc: target_type = "creatures you control"
                    elif "each creature" in target_desc and "target" not in clause_lower: target_type = "each creature" # Target all
                    elif "creatures opponent controls" in target_desc: target_type = "creatures opponent controls"
                    # Add more specific permanent types if needed
                    created_effect = BuffEffect(p_mod, t_mod, duration=duration, target_type=target_type)

            # Tap
            # Ritual / add-mana SPELL effect: "Add {B}{B}{B}", "add N mana of
            # any color". (Mana ACTIVATED abilities on permanents are handled by
            # ManaAbility, not here.) July 2026 parser expansion.
            elif re.search(
                    r"^\s*add\s+(?:an additional\s+)?"
                    r"(\{[wubrgc0-9/p]+\}|\w+ mana)", clause_lower):
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

            elif re.search(
                    r"\b(?:tap|taps)\b\s+(?:up to\s+(?:one|two|three|\d+)\s+)?target",
                    clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                 target_type = "permanent" # Refine based on desc
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "land" in target_desc: target_type = "land"
                 optional_match = re.search(
                     r"\bup to\s+(one|two|three|\d+)\s+target\b",
                     clause_lower)
                 max_targets = (text_to_number(optional_match.group(1))
                                if optional_match else 1)
                 if not isinstance(max_targets, int) or max_targets < 1:
                     max_targets = 1
                 created_effect = TapEffect(
                     target_type=target_type,
                     min_targets=0 if optional_match else 1,
                     max_targets=max_targets)

            # Mass untap: "untap all <type> you control".
            elif re.search(r"untap\s+all\s+(\w+)\s+you control", clause_lower):
                um = re.search(r"untap\s+all\s+(\w+)", clause_lower)
                tt = um.group(1).rstrip('s') if um else "permanent"
                if tt not in ("creature", "artifact", "land", "permanent"):
                    tt = "permanent"
                created_effect = UntapEffect(target_type=tt, scope="all_yours")

            # Untap
            elif re.search(r"\b(untap(?:s)?)\b\s+(?:target|that|it\b)", clause_lower):
                 target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                 target_type = "permanent" # Refine based on desc
                 if "creature" in target_desc: target_type = "creature"
                 elif "artifact" in target_desc: target_type = "artifact"
                 elif "land" in target_desc: target_type = "land"
                 created_effect = UntapEffect(target_type=target_type)
            elif re.search(r"\buntap\s+this\s+(?:creature|permanent|artifact|land)\b",
                           clause_lower):
                 target_type = "creature" if "creature" in clause_lower else "permanent"
                 created_effect = UntapEffect(target_type=target_type, scope="self")

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
                 if re.search(r"\btarget\b", clause_lower):
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
                 elif re.search(r"\b(each|all)\s+tapped creatures? you control\b", clause_lower): target_type = "each tapped creature you control"
                 elif re.search(r"\b(each|all)\s+creatures? you control\b", clause_lower): target_type = "each creature you control"
                 elif re.search(r"\b(each|all)\s+creatures? (?:an opponent|your opponents?) controls?\b", clause_lower): target_type = "each creature your opponents control"
                 elif re.search(r"\b(each|all)\s+creatures?\b", clause_lower): target_type = "each creature"
                 elif re.search(r"\b(each|all)\s+opponents?\b", clause_lower): target_type = "each opponent"
                 elif re.search(r"\b(each|all)\s+players?\b", clause_lower): target_type = "each player"
                 # A later clause can refer to the target selected by an
                 # earlier clause. Reuse the stack's target set rather than
                 # treating "it" as the source permanent.
                 elif re.search(r"\bon\s+(?:it|that\s+(?:creature|permanent)|each of those creatures)\b", clause_lower):
                      has_prior_targets = (isinstance(targets, dict)
                                           and any(isinstance(value, (list, tuple, set)) and value
                                                   for value in targets.values()))
                      target_type = "target permanent" if has_prior_targets else "self"

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
                 if ("you discard" in clause_lower
                         or re.match(r"^discard\b", clause_lower)):
                     target_specifier = "controller"
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
                duration = (
                    "end_of_your_next_turn"
                    if re.search(
                        r"until (?:the )?end of your next turn",
                        clause_lower)
                    else "end_of_turn")
                created_effect = ImpulseDrawEffect(
                    count=n, duration=duration)

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
                if (re.search(r"\byou\s+(?:may\s+)?mill\b", clause_lower)
                        or re.match(r"^mill\b", clause_lower)
                        or re.search(r",\s*mill\b", clause_lower)):
                    target_specifier = "controller"
                elif "opponent mills" in clause_lower or "each opponent mills" in clause_lower: target_specifier = "opponent"
                elif "each player mills" in clause_lower: target_specifier = "each_player"  # underscore: MillEffect's branch key (space form silently no-opped)
                created_effect = MillEffect(count, target=target_specifier) # Pass 'x' or number

            # Mass bounce: "return all <type> to their owners' hands" / "...you
            # control...". Must precede the single-target bounce branch.
            elif re.search(r"return\s+all\s+(\w+)", clause_lower) and re.search(r"to (?:its|their) owner(?:'s|s'|s)? hands?|to your hand", clause_lower):
                tt = "permanent"
                if "creature" in clause_lower: tt = "creature"
                elif "artifact" in clause_lower: tt = "artifact"
                elif "enchantment" in clause_lower: tt = "enchantment"
                elif "land" in clause_lower: tt = "land"
                sc = "all_yours" if "you control" in clause_lower else "all"
                excluded_subtypes = set()
                non_subtype = re.search(
                    r"return\s+all\s+non-([\w-]+)\s+creatures?",
                    clause_lower)
                if non_subtype:
                    excluded_subtypes.add(non_subtype.group(1).lower())
                created_effect = ReturnToHandEffect(
                    target_type=tt, zone="battlefield", scope=sc,
                    excluded_subtypes=excluded_subtypes)

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
                rest_order = "preserve"
                if re.search(r"\bin any order\b", clause_lower):
                    rest_order = "choice"
                elif re.search(r"\bin (?:a )?random order\b", clause_lower):
                    rest_order = "random"
                created_effect = DigEffect(
                    look=look, take=take, rest=rest,
                    rest_order=rest_order)

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
            elif re.search(r"\breturn(?:s)?\b", clause_lower) and re.search(r"to (?:its|their) owner(?:'s|s'|s)? hands?|to your hand", clause_lower):
                target_desc = EffectFactory._extract_target_description(clause_lower) or "permanent"
                target_type = "permanent"
                zone = "battlefield" # Default zone
                normalized_target = target_desc.replace('-', ' ')
                if re.search(
                        r"target\s+(?:spell\s+or\s+permanent|"
                        r"permanent\s+or\s+spell)", clause_lower):
                    target_type = "spell or permanent"
                    zone = "any"
                elif re.search(
                        r"target\s+(?:[a-z-]+\s+)*permanents?\b",
                        clause_lower):
                    target_type = "permanent"
                elif "card" in normalized_target: target_type = "card" # Could be from GY etc.
                elif "creature" in normalized_target: target_type = "creature"
                elif "artifact" in normalized_target: target_type = "artifact"
                elif "enchantment" in normalized_target: target_type = "enchantment"
                elif "planeswalker" in normalized_target: target_type = "planeswalker"
                # Test the noun before the substring ``land``: a nonland
                # permanent is still a permanent, not a land.  This Town's
                # mixed creature/enchantment targets rely on that distinction.
                elif "permanent" in normalized_target: target_type = "permanent"
                elif "land" in normalized_target: target_type = "land"
                # Check originating zone
                if "from your graveyard" in clause_lower: zone = "graveyard"; target_type="card"
                elif "from exile" in clause_lower: zone = "exile"; target_type="card"
                # Add other zones
                optional_match = re.search(
                    r"\bup to\s+(one|two|three|\d+)\s+"
                    r"(?:other\s+)?target\b", clause_lower)
                min_targets = 0 if optional_match else 1
                max_targets = (text_to_number(optional_match.group(1))
                               if optional_match else 1)
                if not isinstance(max_targets, int) or max_targets < 1:
                    max_targets = 1
                created_effect = ReturnToHandEffect(
                    target_type=target_type, zone=zone,
                    min_targets=min_targets, max_targets=max_targets)

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
                 count_expr = None
                 if match:
                      count_str = match.group(1)
                      count = 'x' if count_str == 'x' else text_to_number(count_str)
                 if count == 'x':
                      # The comma splitter separates ``Surveil X`` from its
                      # rules definition, so recover the definition from the
                      # complete effect text and evaluate it at resolution.
                      count_expr_match = re.search(
                          r"\bwhere\s+x\s+is\s+the\s+number\s+of\s+"
                          r"(.+?)(?=[.;]|$)",
                          effect_text, re.IGNORECASE)
                      if count_expr_match:
                           count_expr = count_expr_match.group(1).strip()
                 created_effect = SurveilEffect(
                     count, count_expr=count_expr) # Pass 'x' or number

            # Life Drain (Checked earlier with em dash fix)

            # Copy Spell
            elif (re.search(r"\bcopy target\b.*\bspell\b", clause_lower)
                  or re.search(r"\bcopy (?:that|this) spell\b", clause_lower)):
                 target_type = "spell"
                 if "instant or sorcery spell" in clause_lower: target_type = "instant or sorcery spell"
                 elif "instant spell" in clause_lower: target_type = "instant"
                 elif "sorcery spell" in clause_lower: target_type = "sorcery"
                 elif "creature spell" in clause_lower: target_type = "creature spell"
                 # Add other types
                 new_targets = "choose new targets" in clause_lower
                 # "copy this spell" (Sage of the Skies-style self-copy cast
                 # triggers) resolves against the referencing spell too; it
                 # previously fell through to the unimplemented-effect stub.
                 created_effect = CopySpellEffect(
                     target_type=target_type, new_targets=new_targets,
                     copy_that=bool(re.search(
                         r"\bcopy (?:that|this) spell\b", clause_lower)))

            # Transform
            elif re.search(r"\btransform\b", clause_lower):
                 created_effect = TransformEffect()

            # Explicit day/night instructions (CR 727.1).
            elif re.search(r"\b(?:it\s+)?becomes?\s+(day|night)\b", clause_lower):
                 state_match = re.search(
                     r"\b(?:it\s+)?becomes?\s+(day|night)\b", clause_lower)
                 created_effect = SetDayNightEffect(state_match.group(1))

            # Fight
            elif re.search(r"\bfights?\b.*?\btarget\b", clause_lower):
                 target_type = "creature" # Default
                 match_target = re.search(r"target ([\w\s]+)", clause_lower)
                 if match_target:
                      desc = match_target.group(1).strip()
                      if "creature" in desc: target_type="creature"
                      # Add other types if creatures can fight non-creatures (rare)
                 if re.search(
                         r"\btarget creature you control fights target creature "
                         r"(?:you (?:don['\u2019]?t|do not) control|an opponent controls)\b",
                         clause_lower):
                      fighter = "target_pair"
                 else:
                      fighter = ("enchanted_creature"
                                 if re.search(
                                     r"\benchanted creature fights?\b",
                                     clause_lower)
                                 else "source")
                 created_effect = FightEffect(
                     target_type=target_type, fighter=fighter,
                     optional=("may" in clause_lower
                               or "up to" in clause_lower))

            # --- Fallback and Effect Addition ---
            if created_effect:
                effects.append(created_effect)
            else:
                 # A bare "(reveal it and) put it into your hand" fragment is
                 # the severed tail of a search/look instruction that already
                 # owns the card movement (the comma splitter again).  Adding
                 # a generic no-op effect for it only produces an
                 # "unimplemented effect" warning at resolution.
                 dangling_hand_move = re.fullmatch(
                     r"(?:reveal (?:it|that card) and\s+)?put (?:it|that card)"
                     r" into (?:your|their) hand[.\s]*",
                     clause_lower)
                 if dangling_hand_move and re.search(
                         r"\bsearch(?:es)?\s+(?:your|their)\s+library\b"
                         r"|\blook at the top\b|\breveal the top\b",
                         effect_text, re.IGNORECASE):
                     logging.debug(
                         f"Skipping dangling hand-move fragment already "
                         f"handled by its search/look instruction: "
                         f"'{clause_clean}'")
                     continue
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
