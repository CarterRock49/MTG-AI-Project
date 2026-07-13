"""The stack: casting, targeting on resolution, and spell/ability resolution.

Extracted from game_state.py. This module defines behavior only (a mixin);
all state lives on GameState itself, which composes every mixin.
"""

import copy as copy_module
import logging
from .ability_utils import EffectFactory
import re
from .ability_types import BoundExileTriggeredAbility, TriggeredAbility
from .card import Card
from .targeting import aura_cast_targeting_text


class GameStateStackMixin:
    """The stack: casting, targeting on resolution, and spell/ability resolution."""

    # Empty slots: preserves GameState's __slots__ semantics (no instance __dict__).
    __slots__ = ()

    _ASYNC_EFFECT_CHOICE_TYPES = frozenset({
        "discard", "sacrifice_effect", "distribute_counters", "dig_select",
        "resolution_modal", "resolution_choice", "scry", "surveil",
        "connive_discard", "hand_selection", "prepared_payment",
    })

    def _effect_controller_id(self, controller):
        return "p1" if controller is self.p1 else "p2"

    def _effect_controller_from_id(self, controller_id):
        return self.p1 if controller_id == "p1" else self.p2

    @staticmethod
    def _counter_distribution_spec(effect_text):
        """Return the cast/activation-time division declared by Oracle text."""
        effect_text = str(effect_text or "")
        # This probe exists only for "distribute/put counters" instructions.
        # Feeding ordinary targeting metadata such as an Aura's synthesized
        # ``target creature you control`` through EffectFactory creates a
        # generic no-op effect and a false parser warning during every cast.
        if not (re.search(r"\b(?:put|distribute)\b", effect_text,
                          re.IGNORECASE)
                and re.search(r"\bcounters?\b", effect_text,
                              re.IGNORECASE)):
            return None
        effects = EffectFactory.create_effects(effect_text) or []
        for effect in effects:
            if type(effect).__name__ == "DistributeCountersEffect":
                return {
                    "count": int(effect.count),
                    "counter_type": effect.counter_type,
                }
        return None

    def _copy_stack_context(self, context):
        """Copy rule data without dragging runtime engine objects into it.

        Casting context contains a live ``Card`` object for mana-cost helpers.
        Cards retain engine-owned objects (including locks), so blindly deep
        copying that context during resolution raises and can strand the popped
        physical spell outside every zone.  Finalizers only need declarative rule
        data; runtime object references are deliberately omitted.
        """
        copied = {}
        for key, value in dict(context or {}).items():
            if key in {
                    "card", "controller", "player", "ability",
                    "source_card", "event_card", "game_state"}:
                continue
            try:
                copied[key] = copy_module.deepcopy(value)
            except Exception:
                logging.warning(
                    "Omitting non-copyable stack context key %r during resolution.",
                    key)
        return copied

    def _physical_occurrence_count(self, card_id):
        """Count real occurrences of one shared card-database ID."""
        count = 0
        for player in (self.p1, self.p2):
            for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
                count += player.get(zone, []).count(card_id)
        for item in self.stack:
            if not (isinstance(item, tuple) and len(item) >= 2
                    and item[0] == "SPELL" and item[1] == card_id):
                continue
            item_context = item[3] if len(item) > 3 else {}
            if not item_context.get("is_copy", False):
                count += 1
        return count

    def _run_effect_sequence(self, effects, source_id, controller, targets=None,
                             context=None, finalizer=None,
                             initial_success=True):
        """Apply parsed effects in order, pausing on policy choices.

        The stack item has already been popped when resolution reaches here.
        Remaining effect objects and a declarative finalizer therefore live on
        the choice itself until the chooser finishes; the spell/ability is not
        reparsed or put back on the stack.
        """
        effects = list(effects or [])
        context = dict(context or {})
        # A shared mutable identity list lets a delayed instruction parsed
        # before a token-creation sentence bind "that token" after the earlier
        # instruction has actually created it.
        context.setdefault("_created_object_ids", [])
        success = bool(initial_success)
        for effect_index, effect in enumerate(effects):
            previous_choice = getattr(self, 'choice_context', None)
            previous_targeting = getattr(self, 'targeting_context', None)
            try:
                # Modal/Spree instructions can have independent target sets.
                # A bound target snapshot keeps the ordinary sequential-effect
                # runner (including async discard/dig continuations) while
                # preventing one mode from seeing another mode's targets.
                effect_targets = getattr(effect, '_bound_targets', targets)
                applied = effect.apply(
                    self, source_id, controller, effect_targets,
                    context=context)
                success = bool(applied) and success
            except Exception:
                logging.exception(
                    "Error applying sequenced effect %r from source %r",
                    getattr(effect, 'effect_text', effect), source_id)
                success = False

            choice = getattr(self, 'choice_context', None)
            if (choice is not None and choice is not previous_choice
                    and choice.get('type') in self._ASYNC_EFFECT_CHOICE_TYPES):
                choice['effect_continuation'] = {
                    'effects': effects[effect_index + 1:],
                    'source_id': source_id,
                    'controller_id': self._effect_controller_id(controller),
                    'targets': copy_module.deepcopy(targets),
                    'resolution_context': self._copy_stack_context(context),
                    'finalizer': copy_module.deepcopy(finalizer),
                    'success': success,
                }
                return success, True

            targeting = getattr(self, 'targeting_context', None)
            if targeting is not None and targeting is not previous_targeting:
                targeting['effect_continuation'] = {
                    'effects': effects[effect_index + 1:],
                    'source_id': source_id,
                    'controller_id': self._effect_controller_id(controller),
                    'targets': copy_module.deepcopy(targets),
                    'resolution_context': self._copy_stack_context(context),
                    'finalizer': copy_module.deepcopy(finalizer),
                    'success': success,
                }
                return success, True

        if finalizer:
            success = bool(self._complete_effect_finalizer(finalizer, success))
        return success, False

    def _complete_effect_finalizer(self, finalizer, success=True):
        """Finish a stack object whose effects paused for a policy choice."""
        kind = (finalizer or {}).get('kind')
        controller = self._effect_controller_from_id(
            (finalizer or {}).get('controller_id'))
        source_id = (finalizer or {}).get('source_id')
        context = self._copy_stack_context((finalizer or {}).get('context', {}))
        if kind == 'instant_sorcery':
            return self._finish_instant_sorcery_resolution(
                source_id, controller, context)
        if kind == 'modal_spell':
            return self._finish_modal_spell_resolution(
                source_id, controller, context, success)
        if kind == 'ability':
            ability_type = (finalizer or {}).get('ability_type', 'ABILITY')
            if success:
                self.trigger_ability(
                    source_id, f"{ability_type}_RESOLVED", context)
            self.check_state_based_actions()
            return success
        return success

    def _resume_effect_continuation(self, completed_choice):
        """Resume the parsed effect sequence after an async choice completes."""
        continuation = (completed_choice or {}).get('effect_continuation')
        resume_phase = self._normalized_choice_resume_phase(
            (completed_choice or {}).get('resume_phase', self.PHASE_PRIORITY))
        self.choice_context = None
        self.phase = resume_phase
        if (resume_phase == self.PHASE_PRIORITY
                and self.previous_priority_phase not in self._TURN_PHASES
                and self._last_turn_phase in self._TURN_PHASES):
            self.previous_priority_phase = self._last_turn_phase

        if not continuation:
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0
            return True

        controller = self._effect_controller_from_id(
            continuation.get('controller_id'))
        success, pending = self._run_effect_sequence(
            continuation.get('effects', []),
            continuation.get('source_id'), controller,
            continuation.get('targets'),
            continuation.get('resolution_context', {}),
            finalizer=continuation.get('finalizer'),
            initial_success=continuation.get('success', True))

        release_split_second = continuation.get('release_split_second', False)
        if pending and release_split_second and self.choice_context:
            self.choice_context.setdefault('effect_continuation', {})[
                'release_split_second'] = True
        elif release_split_second:
            any_other_split_second = any(
                isinstance(item, tuple) and len(item) > 3
                and item[3].get('is_split_second')
                for item in self.stack)
            if not any_other_split_second:
                self.split_second_active = False

        if not pending:
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0
        if not success:
            # The chooser's mask-valid action was legal and fully applied. A
            # later instruction of the resolving object no-opping must not
            # retroactively fail that action — it doesn't during ordinary
            # (unpaused) resolution either.
            logging.warning(
                "Effect continuation from source %s finished with a failed "
                "instruction; the completed choice action stands.",
                continuation.get('source_id'))
        return True

    def _get_target_type_from_text(self, text):
         """Simple helper to guess target type."""
         text = text.lower()
         if re.search(
                 r"target\s+creature\s*,\s*enchantment\s*,\s*or\s+planeswalker",
                 text):
             return "permanent"
         if "target creature or vehicle" in text:
             return "creature_or_vehicle"
         if "target creature or spell" in text:
             return "creature_or_spell"
         if ("target spell or permanent" in text
                 or "target permanent or spell" in text):
             return "spell_or_permanent"
         if "target artifact or creature" in text:
             return "permanent"
         if "target artifact or enchantment" in text:
             return "artifact_or_enchantment"
         if "target creature or planeswalker" in text:
             return "permanent"
         if "target creature" in text:
             return "creature"
         if "target player" in text or "target opponent" in text: return "player"
         # Keep this looser adjective form after explicit player targets. It
         # must not reinterpret "target opponent exiles a creature" as though
         # the creature were targeted.
         if re.search(
                 r"\btarget(?:\s+[a-z-]+){1,4}\s+creatures?\b", text):
             return "creature"
         if "target spell" in text: return "spell"
         if ("target card" in text
                 or re.search(r"target\s+(?:instant|sorcery)(?:\s+or\s+"
                              r"(?:instant|sorcery))?\s+card", text)):
             return "card"
         if "target artifact" in text: return "artifact"
         if "target enchantment" in text: return "enchantment"
         if "target land" in text: return "land"
         if ("target permanent" in text
                 or re.search(r"\btarget(?:\s+[a-z-]+){1,4}\s+permanents?\b", text)):
             return "permanent"
         if "any target" in text or "any other target" in text: return "any"
         # "target <creature subtype> you control" (Manifold Mouse's
         # "target Mouse you control"): pass the subtype through so the
         # targeting system can filter by it. The generic categories above
         # have already been ruled out at this point.
         subtype_match = re.search(r"\btarget\s+([a-z][a-z'\-]+)\s+you control\b", text)
         if subtype_match and subtype_match.group(1) not in {
                 "spell", "ability", "player", "opponent", "card",
                 "permanent", "creature", "land", "artifact", "enchantment",
                 "planeswalker", "battle"}:
             return subtype_match.group(1)
         return "target" # Default

    def _can_finalize_targeted_cast(self, targeting_context, selected_targets):
        """Whether the current target set leaves a deferred cast payable.

        Target-dependent discounts are determined after targets are chosen.  A
        spell may therefore be affordable for *some* legal target set while an
        optional zero-target or opponent-only set is not affordable.  Target
        selection uses this pure probe before exposing FINISH (and before an
        at-maximum selection auto-commits the cast).
        """
        context = targeting_context or {}
        if not context.get("resume_cast"):
            return True

        card_id = context.get("source_id")
        controller = context.get("controller")
        card = self._safe_get_card(card_id)
        if (not card or not controller or not self.mana_system
                or not self.mana_system.has_target_dependent_reduction(card)):
            return True

        cost_before_modifiers = context.get("cost_before_modifiers")
        if not isinstance(cost_before_modifiers, dict):
            # Older/directly-constructed contexts do not carry the pure cost
            # snapshot.  Preserve their historical behavior rather than
            # guessing at alternative or additional costs.
            return True

        # Casting contexts can contain Card/effect objects linked back to this
        # GameState (and therefore thread locks). The affordability probe only
        # adds a top-level targets entry, so a shallow copy is both sufficient
        # and safe under Windows SubprocVecEnv.
        cast_context = dict(context.get("original_cast_context", {}))
        cast_context["targets"] = {"chosen": list(selected_targets or [])}
        candidate_cost = self.mana_system.apply_cost_modifiers(
            controller, copy_module.deepcopy(cost_before_modifiers),
            card_id, cast_context)
        return self.mana_system.can_pay_mana_cost_with_lands(
            controller, candidate_cost, cast_context)

    def trigger_ability(self, card_id, event_type, context=None):
        """Forward ability triggering to the AbilityHandler"""
        queued = False
        if hasattr(self, 'ability_handler') and self.ability_handler:
            # BUGFIX: AbilityHandler's method is check_abilities; the old name
            # raised AttributeError on EVERY trigger check, which step()'s broad
            # exception handling converted into 'error' game endings.
            queued = bool(self.ability_handler.check_abilities(
                card_id, event_type, context))
            remaining = []
            for delayed in getattr(self, "delayed_event_triggers", []):
                if delayed.get("expires_turn", self.turn) < self.turn:
                    continue
                if (delayed.get("event_type") != event_type
                        or delayed.get("watched_card_id") != card_id):
                    remaining.append(delayed)
                    continue
                if (event_type == "DIES"
                        and not (context or {}).get(
                            "last_known", {}).get("was_creature", False)):
                    remaining.append(delayed)
                    continue
                controller = (self.p1 if delayed.get("controller") == "p1"
                              else self.p2)
                effect_text = delayed.get("effect_text", "")
                ability = TriggeredAbility(
                    card_id=card_id,
                    trigger_condition="when that creature dies",
                    effect=effect_text,
                    effect_text=f"When that creature dies, {effect_text}.")
                trigger_context = dict(context or {})
                trigger_context.update({
                    "ability": ability,
                    "source_id": delayed.get("source_id", card_id),
                    "effect_text": ability.effect_text,
                    "delayed_event_trigger": True,
                })
                self.ability_handler.active_triggers.append(
                    (ability, controller, trigger_context))
                queued = True
            self.delayed_event_triggers = remaining
        return queued

    def notify_targets_committed(self, source_id, controller, targets, stack_context=None):
        """Emit target events after a spell or ability's targets are final.

        The per-controller set records every battlefield permanent that player
        has targeted this turn, even when it has no Valiant ability yet. This
        keeps first-time checks correct if an object gains Valiant later.
        """
        if not controller or not isinstance(targets, dict):
            return 0
        targeted_this_turn = controller.setdefault(
            "targeted_permanents_this_turn", set())
        target_ids = []
        for value in targets.values():
            if isinstance(value, (list, tuple, set)):
                target_ids.extend(value)

        emitted = 0
        for target_id in dict.fromkeys(target_ids):
            target_controller, target_zone = self.find_card_location(target_id)
            if target_zone != "battlefield":
                continue
            first_time = target_id not in targeted_this_turn
            targeted_this_turn.add(target_id)
            self.trigger_ability(target_id, "BECOMES_TARGET", {
                "target_id": target_id,
                "target_controller": target_controller,
                "targeting_controller": controller,
                "targeting_source_id": source_id,
                "first_time_targeted_by_controller_this_turn": first_time,
            })
            emitted += 1

        if stack_context is None and source_id is not None:
            for item in reversed(self.stack):
                if not (isinstance(item, tuple) and len(item) >= 4
                        and item[1] == source_id and item[2] is controller
                        and isinstance(item[3], dict)):
                    continue
                candidate = item[3]
                if candidate.get("targets") == targets:
                    stack_context = candidate
                    break
        if isinstance(stack_context, dict):
            stack_context["ward_obligations"] = self._collect_ward_obligations(
                controller, targets)
            stack_context["ward_checked_on_targeting"] = True
        return emitted

    def _collect_ward_obligations(self, controller, targets):
        """Snapshot ward triggers at the moment targets are committed."""
        obligations = []
        for target_id in self._flatten_target_ids(targets):
            if target_id in ["p1", "p2"]:
                continue
            target_card = self._safe_get_card(target_id)
            target_controller = self.get_card_controller(target_id)
            if (not target_card or not target_controller
                    or target_controller is controller
                    or not self.check_keyword(target_id, "ward")):
                continue
            if (self.ability_handler
                    and hasattr(self.ability_handler, "suppresses_target_protection")
                    and self.ability_handler.suppresses_target_protection(
                        controller, target_id, "ward")):
                continue
            ward_costs = []
            if self.ability_handler and hasattr(self.ability_handler, 'get_ward_costs'):
                ward_costs = self.ability_handler.get_ward_costs(target_id)
            if not ward_costs:
                ward_costs = ["ward_generic"]
            obligations.extend(
                {"target_id": target_id, "cost": ward_cost}
                for ward_cost in ward_costs)
        return obligations

    def add_to_stack(self, item_type, source_id, controller, context=None):
            """
            Add an item to the stack with context.
            Sets priority to the controller (Rule 117.3c).
            """
            if context is None: context = {}
            # Ensure source_id is valid
            card = self._safe_get_card(source_id)
            card_name = getattr(card, 'name', source_id) if card else source_id

            stack_item = (item_type, source_id, controller, context)
            self.stack.append(stack_item)
            logging.debug(f"Added to stack: {item_type} {card_name} ({source_id}) with context keys: {context.keys()}")

            # *** RULE 117.3c: The player who cast the spell/ability gets priority. ***
            # By default, after adding to stack, the game state should NOT set priority to None.
            # It should be the player who took the action.
            self.priority_player = controller

            # Reset pass count because the state has changed (stack is not empty/same)
            self.priority_pass_count = 0
            self.last_stack_size = len(self.stack)

            # Handling Phase transitions related to Special Choices
            if self.phase not in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                # If not already in priority phase (e.g. we were in Main Phase), enter it
                if self.phase != self.PHASE_PRIORITY:
                    self.previous_priority_phase = self.phase # Store where we came from
                    self.phase = self.PHASE_PRIORITY
                logging.debug(f"Stack changed, priority returned to {self.priority_player['name']}")
            else:
                # Still update stack size even if not resetting priority context
                logging.debug("Added to stack during special choice phase, priority maintained.")

    @staticmethod
    def _target_bounds_from_text(effect_text):
        text = (effect_text or "").lower()
        # Reminder text and later references such as "the target creature" do
        # not create additional target choices.
        text = re.sub(r"\([^()]*\)", " ", text)

        def target_designator_count(value):
            return len(re.findall(
                r"(?<!the )(?<!that )(?<!each )(?<!chosen )"
                r"\btarget\s+(?!of\b)", value))

        word_counts = {"two": 2, "three": 3, "four": 4, "five": 5}
        if "any number of target" in text:
            distribute = re.search(r"distribute\s+(one|two|three|four|five|\d+)", text)
            maximum = 20
            if distribute:
                token = distribute.group(1)
                maximum = int(token) if token.isdigit() else {"one": 1, **word_counts}.get(token, 1)
            return 0, maximum
        listed = re.search(
            r"among\s+one(?:\s+or\s+two|,\s*two(?:,?\s+or\s+three)?)?\s+target", text)
        if listed:
            maximum = 3 if "three" in listed.group(0) else 2 if "two" in listed.group(0) else 1
            return 1, maximum
        counted = re.search(r"\btarget\s+(two|three|four|five)\b", text)
        if not counted:
            counted = re.search(
                r"\b(?:up to\s+)?(two|three|four|five)\s+"
                r"(?:other\s+)?target\b", text)
        if counted:
            maximum = word_counts[counted.group(1)]
        else:
            maximum = max(1, target_designator_count(text))
        if "up to" in text:
            mandatory_text = re.sub(
                r"\bup to\s+(?:one|two|three|four|five|\d+)\s+"
                r"(?:other\s+)?target\b",
                "", text)
            minimum = target_designator_count(mandatory_text)
        else:
            minimum = maximum
        return minimum, maximum

    @staticmethod
    def _target_count_from_text(effect_text):
        return GameStateStackMixin._target_bounds_from_text(effect_text)[1]

    def _spree_cost_for_modes(self, card_id, controller, selected_modes,
                              context=None, apply_modifiers=True):
        """Return the one cumulative mana cost for a Spree announcement.

        Spree mode costs are additional costs, not separate payments.  They
        join the printed/alternative base before taxes and reductions are
        applied exactly once (CR 702.172a).
        """
        card = self._safe_get_card(card_id)
        if not card or not getattr(card, 'is_spree', False):
            return None
        modes = list(getattr(card, 'spree_modes', []) or [])
        chosen = sorted(set(selected_modes or []))
        if (not chosen or any(
                not isinstance(index, int) or not 0 <= index < len(modes)
                for index in chosen)):
            return None

        cast_context = dict(context or {})
        cast_context['card'] = card
        cast_context['selected_spree_modes'] = chosen
        base_cost = self.mana_system.parse_mana_cost(
            getattr(card, 'mana_cost', '') or '')
        total_cost = base_cost
        for index in chosen:
            mode_cost = self.mana_system.parse_mana_cost(
                modes[index].get('cost', '') or '')
            total_cost = self._combine_cost_dicts(total_cost, mode_cost)
        if apply_modifiers:
            total_cost = self.mana_system.apply_cost_modifiers(
                controller, total_cost, card_id, cast_context)
        return total_cost

    def _spree_target_slots(self, card, selected_modes):
        """Build one independent casting target slot per chosen Spree mode."""
        slots = []
        modes = list(getattr(card, 'spree_modes', []) or [])
        for mode_index in sorted(set(selected_modes or [])):
            if not isinstance(mode_index, int) or not 0 <= mode_index < len(modes):
                continue
            effect_text = str(modes[mode_index].get('effect', '') or '')
            if 'target' not in effect_text.lower():
                continue
            min_targets, max_targets = self._target_bounds_from_text(effect_text)
            slots.append({
                'mode_index': mode_index,
                'required_type': self._get_target_type_from_text(effect_text),
                'effect_text': effect_text,
                'required_count': max_targets,
                'min_targets': min_targets,
                'max_targets': max_targets,
            })
        return slots

    @staticmethod
    def _ordinary_instruction_segments(effect_text):
        """Split plain resolving instructions without severing target riders.

        This is deliberately narrower than a general Oracle-text sentence
        parser.  It exists for ordinary spells whose printed instructions own
        separate target choices, for example ``... any target. Tap up to one
        target creature.``  A following sentence that refers back to the
        previous target remains attached to that instruction.
        """
        # Parenthetical Oracle text is reminder text, not a resolving
        # instruction.  Remove it before sentence splitting so periods inside
        # Flashback/Gift reminders cannot manufacture extra instructions.
        instruction_text = re.sub(
            r"\([^()]*\)", " ", str(effect_text or "")).strip()
        raw_segments = re.split(
            r"(?<=[.;])\s+|\n+", instruction_text)
        segments = []
        linked_reference = re.compile(
            r"^(?:then\s+)?(?:it|that\s+(?:card|creature|player|permanent|"
            r"spell)|the\s+target|those\s+(?:cards|creatures|players|"
            r"permanents))\b",
            re.IGNORECASE)
        for raw_segment in raw_segments:
            segment = raw_segment.strip()
            if not segment:
                continue
            if segments and linked_reference.match(segment):
                segments[-1] = f"{segments[-1]} {segment}"
            else:
                segments.append(segment)
        return segments

    @staticmethod
    def _is_nonresolving_spell_instruction(instruction):
        """Whether a segment describes casting permission/cost, not effects."""
        return bool(re.match(
            r"^(?:flashback|buyback|kicker|multikicker|rebound|retrace|"
            r"jump-start|aftermath|overload|surge|spree|gift)\b",
            str(instruction or "").strip(), re.IGNORECASE))

    def _ordinary_target_instructions(self, effect_text):
        """Return resolving instructions that announce ordinary targets.

        Conditional replacement branches describe an alternative target set,
        not another target chosen alongside the default instruction.  Until a
        mechanic-specific announcement selects such a branch (Gift, Bargain,
        and similar choices), the ordinary cast path represents the printed
        default branch.
        """
        slots = []
        for instruction_index, instruction in enumerate(
                self._ordinary_instruction_segments(effect_text)):
            # Oracle's targeting noun is the standalone word "target".
            # Cost/condition text such as "if it targets a permanent" only
            # describes the spell after targets are chosen and must not create
            # another announcement slot.
            if not re.search(r"\btarget\b", instruction, re.IGNORECASE):
                continue
            # Conditional/replacement branches do not announce another target
            # alongside the base instruction.  Their target requirement is
            # selected by the mechanic's own casting path (Gift, Bargain,
            # "if ... instead", and similar alternatives).
            if re.match(r"^(?:if|unless|otherwise|instead)\b", instruction,
                        re.IGNORECASE):
                continue
            # A fight instruction can announce two creatures with opposing
            # controller restrictions in one sentence.  They are distinct
            # target roles, not an undifferentiated two-creature pool: the
            # first creature is the fighter and the second is what it fights.
            two_target_fight = re.search(
                r"\btarget creature you control fights target creature "
                r"(?:you (?:don['\u2019]?t|do not) control|an opponent controls)\b",
                instruction, re.IGNORECASE)
            if two_target_fight:
                slots.extend([
                    {
                        "instruction_index": instruction_index,
                        "target_role": "fighter",
                        "required_type": "creature",
                        "effect_text": "target creature you control",
                        "required_count": 1, "min_targets": 1,
                        "max_targets": 1,
                    },
                    {
                        "instruction_index": instruction_index,
                        "target_role": "fight_opponent",
                        "required_type": "creature",
                        "effect_text": "target creature you don't control",
                        "required_count": 1, "min_targets": 1,
                        "max_targets": 1,
                    },
                ])
                continue
            min_targets, max_targets = self._target_bounds_from_text(
                instruction)
            slots.append({
                "instruction_index": instruction_index,
                "required_type": self._get_target_type_from_text(instruction),
                "effect_text": instruction,
                "required_count": max_targets,
                "min_targets": min_targets,
                "max_targets": max_targets,
            })
        return slots

    def _ordinary_target_slots(self, effect_text):
        """Return independent target slots for a multi-instruction spell.

        The legacy single target map remains appropriate when only one target
        role exists. Slot mode is enabled for independent instructions and
        for one instruction that announces multiple restricted roles, such as
        Bushwhack's friendly fighter and opposing fight target.
        """
        slots = self._ordinary_target_instructions(effect_text)
        return slots if len(slots) > 1 else []

    def _ordinary_single_targeting_text(self, effect_text):
        """Narrow a one-target spell to its active targeting instruction.

        Target bounds and legality must not inspect an unselected conditional
        ``instead`` branch.  Multi-instruction spells keep their slot model;
        unrecognized/conditional-only text fails back to the legacy parser.
        """
        instructions = self._ordinary_target_instructions(effect_text)
        if len(instructions) == 1:
            return instructions[0].get('effect_text', effect_text)
        return effect_text

    def _categorize_targets_for_slot(self, slot, target_ids):
        """Categorize targets using the announced slot, not shared card IDs."""
        required_type = str(
            (slot or {}).get('required_type', '') or '').lower()
        category_map = {
            'spell': 'spells', 'ability': 'abilities',
            'player': 'players', 'creature': 'creatures',
            'permanent': 'permanents', 'land': 'lands',
            'artifact': 'artifacts', 'enchantment': 'enchantments',
            'planeswalker': 'planeswalkers', 'card': 'cards',
        }
        forced_category = category_map.get(required_type)
        categorized = {}
        for target_id in list(target_ids or []):
            category = forced_category or self._determine_target_category(
                target_id)
            categorized.setdefault(category, []).append(target_id)
        return categorized

    def spree_mode_is_selectable(self, card_id, controller, selected_modes,
                                  mode_index, context=None):
        """Whether adding one Spree mode can still complete a legal cast."""
        card = self._safe_get_card(card_id)
        modes = list(getattr(card, 'spree_modes', []) or []) if card else []
        selected = list(selected_modes or [])
        if (not getattr(card, 'is_spree', False)
                or not isinstance(mode_index, int)
                or not 0 <= mode_index < len(modes)
                or mode_index in selected):
            return False

        candidate_modes = sorted(selected + [mode_index])
        pay_context = dict(context or {})
        pay_context['card'] = card
        pay_context['selected_spree_modes'] = candidate_modes
        total_cost = self._spree_cost_for_modes(
            card_id, controller, candidate_modes, context=pay_context)
        if (total_cost is None
                or not self.mana_system.can_pay_mana_cost_with_lands(
                    controller, total_cost, pay_context)):
            return False

        effect_text = str(modes[mode_index].get('effect', '') or '')
        if 'target' not in effect_text.lower():
            return True
        target_type = self._get_target_type_from_text(effect_text)
        minimum, _ = self._target_bounds_from_text(effect_text)
        valid_map = self.targeting_system.get_valid_targets(
            card_id, controller, target_type, effect_text=effect_text)
        valid_ids = {
            target_id for ids in valid_map.values() for target_id in ids
        }
        return len(valid_ids) >= minimum

    def modal_mode_is_selectable(self, choice, mode_index):
        """Return whether a pending mode can complete a legal cast.

        Modes are chosen before targets (CR 601.2b/c), but a mode whose
        mandatory targets do not exist cannot be chosen.  The old mask exposed
        every printed mode, so Bushwhack's fight mode could be mask-valid with
        only one of its two required creatures on the battlefield.
        """
        if (not isinstance(choice, dict)
                or choice.get("type") not in {"choose_mode", "resolution_modal"}):
            return False
        modes = list(choice.get("available_modes", []))
        selected = list(choice.get("selected_modes", []))
        if (not isinstance(mode_index, int)
                or not 0 <= mode_index < len(modes)
                or mode_index in selected):
            return False
        controller = choice.get("controller") or choice.get("player")
        card_id = choice.get("card_id")
        if controller is None or card_id is None:
            return False

        if choice.get('is_spree'):
            return self.spree_mode_is_selectable(
                card_id, controller, selected, mode_index,
                context=choice.get('original_cast_context', {}))

        candidate_modes = selected + [mode_index]
        targeting_text = " ".join(modes[index] for index in candidate_modes)
        if "target" not in targeting_text.lower():
            return True
        target_slots = self._ordinary_target_slots(targeting_text)
        if target_slots:
            for slot in target_slots:
                valid_map = self.targeting_system.get_valid_targets(
                    card_id, controller,
                    slot.get("required_type", "target"),
                    effect_text=slot.get("effect_text", ""))
                valid_ids = {
                    target_id for ids in valid_map.values()
                    for target_id in ids}
                if len(valid_ids) < int(slot.get("min_targets", 0)):
                    return False
            return True
        target_type = self._get_target_type_from_text(targeting_text)
        minimum, _ = self._target_bounds_from_text(targeting_text)
        valid_map = self.targeting_system.get_valid_targets(
            card_id, controller, target_type, effect_text=targeting_text)
        valid_ids = {
            target_id for ids in valid_map.values() for target_id in ids
        }
        return len(valid_ids) >= minimum

    def start_pending_stack_target_choice(self):
        """Open the next unresolved target choice already waiting on the stack."""
        if self.targeting_context:
            return True

        stack_index = 0
        while stack_index < len(self.stack):
            item = self.stack[stack_index]
            if not (isinstance(item, tuple) and len(item) >= 4):
                stack_index += 1
                continue
            item_type, source_id, controller, context = item
            if not context.get("target_choice_pending"):
                stack_index += 1
                continue

            effect_text = context.get("targeting_text") or context.get("effect_text", "")
            target_type = self._get_target_type_from_text(effect_text)
            parsed_min, parsed_max = self._target_bounds_from_text(effect_text)
            required_count = int(context.get("required_count", parsed_max))
            min_targets = int(context.get("min_targets", parsed_min))
            valid_map = self.targeting_system.get_valid_targets(
                source_id, controller, target_type, effect_text=effect_text)
            valid_ids = {target_id for ids in valid_map.values() for target_id in ids}
            excluded_target_ids = set()
            if "other than that creature" in effect_text.lower():
                prior_target = context.get("target_id")
                if prior_target is not None:
                    excluded_target_ids.add(prior_target)
            valid_ids.difference_update(excluded_target_ids)

            if len(valid_ids) < min_targets:
                logging.debug(
                    f"{item_type} {source_id} was not put on the stack: "
                    f"{len(valid_ids)}/{min_targets} legal targets available.")
                self.stack.pop(stack_index)
                self.last_stack_size = len(self.stack)
                continue

            instance_id = context.get("target_instance_id")
            if not instance_id:
                instance_id = f"{item_type}:{source_id}:{self.turn}:{stack_index}:{id(context)}"
                context["target_instance_id"] = instance_id
                self.stack[stack_index] = item[:3] + (context,)

            if self.previous_priority_phase is None and self.phase not in [
                    self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                self.previous_priority_phase = self.phase
            self.phase = self.PHASE_TARGETING
            self.targeting_context = {
                "source_id": source_id,
                "controller": controller,
                "required_type": target_type,
                "required_count": required_count,
                "min_targets": min_targets,
                "max_targets": required_count,
                "selected_targets": [],
                "effect_text": effect_text,
                "target_instance_id": instance_id,
                "excluded_target_ids": list(excluded_target_ids),
            }
            self.priority_player = controller
            self.priority_pass_count = 0
            return True

        if (not self.stack and self.phase == self.PHASE_PRIORITY
                and self.previous_priority_phase is not None):
            self.phase = self.previous_priority_phase
            self.previous_priority_phase = None
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0
        return False

    def copy_spell_on_stack(self, stack_reference, controller, copied_by=None,
                            allow_new_targets=False, context_overrides=None):
        """Create a spell copy while preserving the decisions made for the original."""
        original_item = None
        if isinstance(stack_reference, int):
            if 0 <= stack_reference < len(self.stack):
                original_item = self.stack[stack_reference]
        elif isinstance(stack_reference, tuple):
            original_item = stack_reference

        if not (isinstance(original_item, tuple) and len(original_item) >= 4
                and original_item[0] == "SPELL"):
            logging.warning(f"Cannot copy non-spell stack item: {stack_reference}")
            return None

        _, spell_id, original_controller, original_context = original_item
        spell = self._safe_get_card(spell_id)
        if not spell:
            logging.warning(f"Cannot copy missing spell card {spell_id}.")
            return None

        new_context = self._copy_stack_context(original_context)
        existing_copy_ids = {
            item[3].get("copy_instance_id")
            for item in self.stack
            if isinstance(item, tuple) and len(item) > 3 and isinstance(item[3], dict)
        }
        base_copy_id = f"copy_{self.turn}_{len(self.stack)}"
        copy_instance_id = base_copy_id
        suffix = 2
        while copy_instance_id in existing_copy_ids:
            copy_instance_id = f"{base_copy_id}_{suffix}"
            suffix += 1

        new_context.update({
            "is_copy": True,
            "copied_by": copied_by,
            "original_caster": original_controller,
            "copy_instance_id": copy_instance_id,
        })
        if context_overrides:
            new_context.update(self._copy_stack_context(context_overrides))

        original_targets = new_context.get("targets")
        target_count = 0
        if isinstance(original_targets, dict):
            target_count = sum(
                len(ids) for ids in original_targets.values()
                if isinstance(ids, (list, tuple, set))
            )
        elif isinstance(original_targets, (list, tuple, set)):
            target_count = len(original_targets)

        requires_target = bool(new_context.get("requires_target", target_count > 0))
        required_count = int(new_context.get("num_targets", target_count or 1))
        can_retarget = bool(allow_new_targets and requires_target and required_count > 0)
        new_context["needs_new_targets"] = can_retarget

        self.add_to_stack("SPELL", spell_id, controller, new_context)
        logging.debug(
            f"Created copy {copy_instance_id} of {getattr(spell, 'name', spell_id)} "
            f"controlled by {controller.get('name', 'unknown')}."
        )

        if can_retarget:
            if target_count > 1:
                self._begin_copy_retarget_slots(
                    spell_id, copy_instance_id, controller, new_context,
                    original_targets)
                return copy_instance_id
            target_type = self._get_target_type_from_text(
                getattr(spell, "oracle_text", ""))
            if target_type == "target" and isinstance(original_targets, dict) and len(original_targets) == 1:
                category = next(iter(original_targets))
                singular_categories = {
                    "creatures": "creature", "players": "player",
                    "permanents": "permanent", "spells": "spell",
                    "lands": "land", "artifacts": "artifact",
                    "enchantments": "enchantment",
                    "planeswalkers": "planeswalker", "cards": "card",
                    "abilities": "ability",
                }
                target_type = singular_categories.get(category)
            if not target_type:
                target_type = "target"

            self.previous_priority_phase = self.phase
            self.phase = self.PHASE_TARGETING
            self.targeting_context = {
                "source_id": spell_id,
                "copy_instance_id": copy_instance_id,
                "controller": controller,
                "required_type": target_type,
                "required_count": required_count,
                "min_targets": int(new_context.get("min_targets", required_count)),
                "max_targets": int(new_context.get("max_targets", required_count)),
                "selected_targets": [],
                "effect_text": getattr(spell, "oracle_text", ""),
                "allow_keep_original_targets": True,
            }
            self.priority_player = controller
            self.priority_pass_count = 0
        elif target_count:
            self.notify_targets_committed(
                spell_id, controller,
                original_targets if isinstance(original_targets, dict) else {},
                stack_context=new_context)

        return copy_instance_id

    @staticmethod
    def _target_category_singular(category):
        return {
            "creatures": "creature", "players": "player",
            "permanents": "permanent", "spells": "spell",
            "lands": "land", "artifacts": "artifact",
            "enchantments": "enchantment",
            "planeswalkers": "planeswalker", "cards": "card",
            "abilities": "ability", "battles": "battle",
        }.get(category, "target")

    def _begin_copy_retarget_slots(self, spell_id, copy_instance_id,
                                   controller, stack_context,
                                   original_targets):
        """Ask keep/retarget independently for each inherited target."""
        slots = []
        existing_slots = stack_context.get("targets_by_slot") or []
        slot_definitions = (
            stack_context.get("spree_target_slots")
            if stack_context.get("is_spree") else
            stack_context.get("instruction_target_slots")) or []
        if existing_slots:
            for slot_index, slot_targets in enumerate(existing_slots):
                definition = (slot_definitions[slot_index]
                              if slot_index < len(slot_definitions) else {})
                for target_id in slot_targets:
                    category = self._determine_target_category(target_id)
                    slots.append({
                        "target_id": target_id,
                        "stack_slot_index": slot_index,
                        "required_type": definition.get(
                            "required_type",
                            self._target_category_singular(category)),
                        "effect_text": definition.get("effect_text", ""),
                        "target_role": definition.get("target_role"),
                    })
        elif isinstance(original_targets, dict):
            for category, target_ids in original_targets.items():
                if not isinstance(target_ids, (list, tuple, set)):
                    continue
                for target_id in target_ids:
                    slots.append({
                        "target_id": target_id,
                        "stack_slot_index": len(slots),
                        "required_type": self._target_category_singular(
                            category),
                    })
        self.previous_priority_phase = self.phase
        self.phase = self.PHASE_CHOOSE
        self.choice_context = {
            "type": "copy_retarget_slots", "player": controller,
            "controller": controller, "source_id": spell_id,
            "copy_instance_id": copy_instance_id,
            "slots": slots, "slot_index": 0,
            "chosen_targets": [slot["target_id"] for slot in slots],
            "target_slot_count": len(existing_slots) if existing_slots else len(slots),
            "effect_text": getattr(self._safe_get_card(spell_id), "oracle_text", ""),
        }
        self.priority_player = controller
        self.priority_pass_count = 0

    def choose_copy_retarget_slot(self, retarget=False):
        choice = self.choice_context
        if not (choice and choice.get("type") == "copy_retarget_slots"):
            return False
        index = int(choice.get("slot_index", 0))
        slots = choice.get("slots", [])
        if not 0 <= index < len(slots):
            return False
        if retarget:
            self.choice_context = None
            self.phase = self.PHASE_TARGETING
            self.targeting_context = {
                "source_id": choice.get("source_id"),
                "copy_instance_id": choice.get("copy_instance_id"),
                "controller": choice.get("controller"),
                "required_type": slots[index].get("required_type", "target"),
                "required_count": 1, "min_targets": 1, "max_targets": 1,
                "selected_targets": [],
                "excluded_target_ids": list(dict.fromkeys(
                    [slots[index].get("target_id")] + [
                        target_id for slot_index, target_id in enumerate(
                            choice.get("chosen_targets", []))
                        if slot_index != index])),
                "effect_text": (slots[index].get("effect_text")
                                or choice.get("effect_text", "")),
                "target_role": slots[index].get("target_role"),
                "copy_retarget_state": choice,
            }
            return True
        return self._advance_copy_retarget_slot(choice)

    def complete_copy_retarget_slot(self, selected_target):
        context = self.targeting_context or {}
        choice = context.get("copy_retarget_state")
        if not choice:
            return False
        index = int(choice.get("slot_index", 0))
        choice["chosen_targets"][index] = selected_target
        self.targeting_context = None
        self.choice_context = choice
        self.phase = self.PHASE_CHOOSE
        return self._advance_copy_retarget_slot(choice)

    def _advance_copy_retarget_slot(self, choice):
        choice["slot_index"] = int(choice.get("slot_index", 0)) + 1
        if choice["slot_index"] < len(choice.get("slots", [])):
            self.priority_player = choice.get("player")
            self.priority_pass_count = 0
            return True
        categorized = {}
        for target_id in choice.get("chosen_targets", []):
            category = self._determine_target_category(target_id)
            categorized.setdefault(category, []).append(target_id)
        committed = False
        for index in range(len(self.stack) - 1, -1, -1):
            item = self.stack[index]
            if not (isinstance(item, tuple) and len(item) >= 4
                    and item[1] == choice.get("source_id")
                    and item[3].get("copy_instance_id") == choice.get("copy_instance_id")):
                continue
            stack_context = item[3]
            stack_context["targets"] = categorized
            rebuilt_slots = [
                [] for _ in range(int(choice.get(
                    "target_slot_count", len(choice.get("slots", [])))))]
            for retarget_slot, target_id in zip(
                    choice.get("slots", []),
                    choice.get("chosen_targets", [])):
                slot_index = int(retarget_slot.get("stack_slot_index", 0))
                if 0 <= slot_index < len(rebuilt_slots):
                    rebuilt_slots[slot_index].append(target_id)
            stack_context["targets_by_slot"] = rebuilt_slots
            stack_context["needs_new_targets"] = False
            self.stack[index] = item[:3] + (stack_context,)
            self.notify_targets_committed(
                choice.get("source_id"), choice.get("controller"), categorized,
                stack_context=stack_context)
            committed = True
            break
        self.choice_context = None
        self.phase = (self.previous_priority_phase
                      if self.previous_priority_phase is not None
                      else self.PHASE_PRIORITY)
        self.previous_priority_phase = None
        self.priority_player = choice.get("controller")
        self.priority_pass_count = 0
        return committed

    def finish_optional_copy_targeting(self):
        """Keep a copied spell's inherited targets and leave its targeting choice."""
        context = self.targeting_context
        if not (self.phase == self.PHASE_TARGETING and context
                and context.get("allow_keep_original_targets")
                and not context.get("selected_targets")):
            return False

        controller = context.get("controller")
        source_id = context.get("source_id")
        copy_instance_id = context.get("copy_instance_id")
        inherited_targets = {}
        inherited_context = None
        for item in reversed(self.stack):
            if not (isinstance(item, tuple) and len(item) >= 4
                    and item[0] == "SPELL" and item[1] == source_id):
                continue
            item_context = item[3] if isinstance(item[3], dict) else {}
            if item_context.get("copy_instance_id") == copy_instance_id:
                inherited_targets = item_context.get("targets", {})
                inherited_context = item_context
                break
        self.notify_targets_committed(
            source_id, controller,
            inherited_targets if isinstance(inherited_targets, dict) else {},
            stack_context=inherited_context)
        self.targeting_context = None
        if self.previous_priority_phase is not None:
            self.phase = self.previous_priority_phase
            self.previous_priority_phase = None
        else:
            self.phase = self.PHASE_PRIORITY
        self.priority_pass_count = 0
        self.priority_player = self._get_active_player() or controller
        logging.debug("Copied spell kept its inherited targets.")
        return True

    def finalize_modal_spell_choice(self):
        """Resume a pending cast after its modes have been selected."""
        choice = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "choose_mode"):
            return False

        selected_modes = sorted(set(choice.get("selected_modes", [])))
        min_required = int(choice.get("min_required", 1))
        max_required = int(choice.get("max_required", 1))
        if not (min_required <= len(selected_modes) <= max_required):
            return False

        card_id = choice.get("card_id")
        controller = choice.get("controller") or choice.get("player")
        if card_id is None or controller is None:
            logging.error("Cannot finalize modal spell: missing card or controller.")
            return False

        cast_context = dict(choice.get("original_cast_context", {}))
        if choice.get('is_spree'):
            cast_context["selected_spree_modes"] = selected_modes
            cast_context["is_spree"] = True
        else:
            cast_context["selected_modes"] = selected_modes
        self.choice_context = None
        self._restore_casting_return(choice)
        success = self.cast_spell(card_id, controller, cast_context)
        if success:
            logging.debug(f"Finalized modal spell {card_id} with modes {selected_modes}.")
        return success

    def choose_x_for_pending_spell(self, x_value):
        """Resume a pending cast after the player chooses the value of X."""
        choice = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "choose_x"):
            return False
        min_x = int(choice.get("min_x", 0))
        max_x = int(choice.get("max_x", 0))
        if not isinstance(x_value, int) or not min_x <= x_value <= max_x:
            return False

        card_id = choice.get("card_id") or choice.get("source_id")
        controller = choice.get("controller") or choice.get("player")
        cast_context = dict(choice.get("original_cast_context", {}))
        cast_context["X"] = x_value
        self.choice_context = None
        self._restore_casting_return(choice)
        if card_id is None or controller is None:
            logging.error("Cannot resume X spell: missing card or controller.")
            return False
        return self.cast_spell(card_id, controller, cast_context)

    @staticmethod
    def _casting_additional_cost(card):
        """Parse the sample-deck nonmana casting costs handled as choices."""
        oracle_text = getattr(card, "oracle_text", "").lower() if card else ""
        if re.search(
                r"as an additional cost to cast this spell,\s*return a permanent "
                r"you control to its owner'?s hand", oracle_text):
            return {"type": "return_permanent"}
        evidence_match = re.search(
            r"as an additional cost to cast this spell,\s*you may collect evidence (\d+)",
            oracle_text)
        if evidence_match:
            return {"type": "collect_evidence", "threshold": int(evidence_match.group(1))}
        return None

    def _begin_casting_choice(self, choice_context):
        # A deferred casting choice must preserve both layers of timing state.
        # In particular, PHASE_PRIORITY can wrap a main phase in
        # previous_priority_phase.  Saving only PHASE_PRIORITY loses the main
        # phase and makes a sorcery-speed spell fail timing when the choice
        # resumes (observed with Mockingbird's X choice).
        choice_context.setdefault("casting_return_phase", self.phase)
        choice_context.setdefault(
            "casting_return_previous_priority_phase",
            self.previous_priority_phase)
        self.phase = self.PHASE_CHOOSE
        self.choice_context = choice_context
        self.priority_player = choice_context.get("player")
        self.priority_pass_count = 0
        return True

    def _restore_casting_return(self, choice):
        """Restore the exact timing state captured before a casting choice."""
        if "casting_return_phase" in choice:
            self.phase = choice.get("casting_return_phase")
            self.previous_priority_phase = choice.get(
                "casting_return_previous_priority_phase")
            return
        # Compatibility for any older in-memory choice context.
        return_phase = self.previous_priority_phase
        self.previous_priority_phase = None
        self.phase = (return_phase if return_phase is not None
                      else self.PHASE_PRIORITY)

    def _resume_after_casting_choice(self, choice, cast_context):
        card_id = choice.get("card_id")
        controller = choice.get("controller") or choice.get("player")
        self.choice_context = None
        self._restore_casting_return(choice)
        self.priority_player = controller
        self.priority_pass_count = 0
        if card_id is None or controller is None:
            return False
        return self.cast_spell(card_id, controller, cast_context)

    @staticmethod
    def _mana_spent_on_cast(context):
        """Return the amount of mana actually spent for a resolving spell."""
        details = (context or {}).get("final_paid_details", {})
        spent = details.get("spent_specific", {}) if isinstance(details, dict) else {}
        total = 0
        for amount in spent.values() if isinstance(spent, dict) else ():
            try:
                total += max(0, int(amount))
            except (TypeError, ValueError):
                continue
        return total

    def _mockingbird_copy_options(self, mana_spent):
        options = []
        for player in (self.p1, self.p2):
            for permanent_id in player.get("battlefield", []):
                permanent = self._safe_get_card(permanent_id)
                if not permanent or "creature" not in getattr(permanent, "card_types", []):
                    continue
                try:
                    mana_value = int(getattr(permanent, "cmc", 0) or 0)
                except (TypeError, ValueError):
                    mana_value = 0
                if mana_value <= mana_spent:
                    options.append(permanent_id)
        return options

    def _apply_mockingbird_copy_identity(self, card_id, target_id):
        """Apply Mockingbird's copyable values and its Bird/flying exception."""
        card = self._safe_get_card(card_id)
        target = self._safe_get_card(target_id)
        if not card or not target:
            return False
        target_controller, target_zone = self.find_card_location(target_id)
        if target_zone != "battlefield" or "creature" not in getattr(target, "card_types", []):
            return False

        original_printed = copy_module.deepcopy(getattr(card, "_printed", {}))
        copied = copy_module.deepcopy(getattr(target, "_printed", {}))
        if not copied:
            target.snapshot_printed()
            copied = copy_module.deepcopy(target._printed)

        subtypes = list(copied.get("subtypes", []) or [])
        if "bird" not in {str(subtype).lower() for subtype in subtypes}:
            subtypes.append("bird")
        copied["subtypes"] = subtypes
        copied["type_line"] = self._build_type_line({
            "supertypes": copied.get("supertypes", []),
            "card_types": copied.get("card_types", []),
            "subtypes": subtypes,
        })

        keywords = list(copied.get("keywords", []) or [])
        if len(keywords) != len(Card.ALL_KEYWORDS):
            keywords = [0] * len(Card.ALL_KEYWORDS)
        flying_index = Card.ALL_KEYWORDS.index("flying")
        keywords[flying_index] = 1
        copied["keywords"] = keywords
        oracle_text = str(copied.get("oracle_text", "") or "")
        if not re.search(r"(?:^|\n)flying(?:\s|,|$)", oracle_text, re.IGNORECASE):
            oracle_text = f"{oracle_text}\nFlying".strip()
        copied["oracle_text"] = oracle_text

        self.copy_overrides[card_id] = {
            "original_printed": original_printed,
            "copied_from": target_id,
        }
        card._printed = copied
        card.reset_to_printed()
        return True

    def complete_mockingbird_copy_choice(self, option_index=None):
        """Choose Mockingbird's copy object, or pass None to decline."""
        choice = getattr(self, "choice_context", None)
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "mockingbird_copy"):
            return False
        controller = choice.get("controller") or choice.get("player")
        card_id = choice.get("card_id")
        options = choice.get("options", [])
        selected_id = None
        if option_index is not None:
            if not isinstance(option_index, int) or not 0 <= option_index < len(options):
                return False
            selected_id = options[option_index]
            if not self._apply_mockingbird_copy_identity(card_id, selected_id):
                return False

        return_phase = self.previous_priority_phase
        self.choice_context = None
        self.previous_priority_phase = None
        self.phase = return_phase if return_phase is not None else self.PHASE_PRIORITY
        self.priority_player = controller
        self.priority_pass_count = 0
        context = dict(choice.get("resolution_context", {}))
        context["mockingbird_copy_choice_complete"] = True
        context["mockingbird_copied_from"] = selected_id
        success = self.move_card(
            card_id, controller, "stack_implicit", controller, "battlefield",
            cause="spell_resolution", context=context)
        if not success:
            override = self.copy_overrides.pop(card_id, None)
            card = self._safe_get_card(card_id)
            if card and override and override.get("original_printed"):
                card._printed = copy_module.deepcopy(override["original_printed"])
                card.reset_to_printed()
            controller.setdefault("graveyard", []).append(card_id)
        return success

    def _superior_spider_copy_options(self):
        """Creature cards in either graveyard, in stable policy order."""
        options = []
        for player in (self.p1, self.p2):
            for card_id in player.get("graveyard", []):
                card = self._safe_get_card(card_id)
                if (card and "creature" in {
                        str(card_type).lower() for card_type in getattr(
                            card, "card_types", [])}):
                    options.append(card_id)
        return options

    def _apply_superior_spider_copy_identity(self, card_id, target_id):
        """Apply Mind Swap's layer-1 copy and its printed exceptions."""
        card = self._safe_get_card(card_id)
        target = self._safe_get_card(target_id)
        if not card or not target:
            return False
        _, target_zone = self.find_card_location(target_id)
        if (target_zone != "graveyard"
                or "creature" not in {
                    str(card_type).lower() for card_type in getattr(
                        target, "card_types", [])}):
            return False

        original_printed = copy_module.deepcopy(
            getattr(card, "_printed", {}))
        copied = copy_module.deepcopy(getattr(target, "_printed", {}))
        if not copied:
            target.snapshot_printed()
            copied = copy_module.deepcopy(target._printed)

        copied["name"] = "Superior Spider-Man"
        copied["power"] = 4
        copied["toughness"] = 4
        subtypes = list(copied.get("subtypes", []) or [])
        present = {str(subtype).lower() for subtype in subtypes}
        for subtype in ("Spider", "Human", "Hero"):
            if subtype.lower() not in present:
                subtypes.append(subtype)
                present.add(subtype.lower())
        copied["subtypes"] = subtypes
        copied["type_line"] = self._build_type_line({
            "supertypes": copied.get("supertypes", []),
            "card_types": copied.get("card_types", []),
            "subtypes": subtypes,
        })

        self.copy_overrides[card_id] = {
            "original_printed": original_printed,
            "copied_from": target_id,
            "copy_kind": "mind_swap",
        }
        card._printed = copied
        card.reset_to_printed()
        return True

    def _queue_superior_spider_exile_trigger(self, source_id, controller,
                                              bound_card_id):
        bound_owner, _ = self.find_card_location(bound_card_id)
        trigger = BoundExileTriggeredAbility(
            source_id, bound_card_id, bound_zone="graveyard",
            bound_zone_generation=getattr(
                self._safe_get_card(bound_card_id),
                "_zone_change_generation", None),
            bound_owner_id=(
                "p1" if bound_owner is self.p1 else
                "p2" if bound_owner is self.p2 else None))
        trigger_context = {
            "ability": trigger,
            "source_id": source_id,
            "effect_text": trigger.effect,
            "is_reflexive_trigger": True,
            "reflexive_prerequisite": "Mind Swap copy replacement",
            "bound_object_id": bound_card_id,
            "bound_zone": "graveyard",
        }
        self.ability_handler.active_triggers.append(
            (trigger, controller, trigger_context))

    def complete_superior_spider_copy_choice(self, option_index=None):
        """Commit or decline Mind Swap, then finish the creature's entry."""
        choice = getattr(self, "choice_context", None)
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "resolution_choice"
                and choice.get("choice_kind") == "superior_spider_copy"):
            return False
        controller = choice.get("controller") or choice.get("player")
        card_id = choice.get("card_id")
        options = list(choice.get("options", []))
        selected_id = None
        if option_index is not None:
            if (not isinstance(option_index, int)
                    or not 0 <= option_index < len(options)):
                return False
            selected_id = options[option_index]
            if selected_id not in self._superior_spider_copy_options():
                return False
            if not self._apply_superior_spider_copy_identity(
                    card_id, selected_id):
                return False

        context = dict(choice.get("resolution_context", {}))
        context.update({
            "superior_spider_copy_choice_complete": True,
            "superior_spider_copied_from": selected_id,
        })
        self.choice_context = None
        self.phase = choice.get("resume_phase", self.PHASE_PRIORITY)
        if self.phase in self._TURN_PHASES:
            self.previous_priority_phase = None
        self.priority_player = controller
        self.priority_pass_count = 0
        success = self.move_card(
            card_id, controller, "stack_implicit", controller, "battlefield",
            cause="spell_resolution", context=context)
        if success and selected_id is not None:
            self._queue_superior_spider_exile_trigger(
                card_id, controller, selected_id)
        elif not success:
            override = self.copy_overrides.pop(card_id, None)
            card = self._safe_get_card(card_id)
            if card and override and override.get("original_printed"):
                card._printed = copy_module.deepcopy(
                    override["original_printed"])
                card.reset_to_printed()
            controller.setdefault("graveyard", []).append(card_id)
        return bool(success)

    @staticmethod
    def _bargain_options(player, card_lookup):
        options = []
        for permanent_id in player.get("battlefield", []):
            permanent = card_lookup(permanent_id)
            if not permanent:
                continue
            types = set(getattr(permanent, "card_types", []))
            if (types.intersection({"artifact", "enchantment"})
                    or bool(getattr(permanent, "is_token", False))):
                options.append(permanent_id)
        return options

    def complete_bargain_choice(self, option_index=None):
        """Stage an optional Bargain sacrifice and resume casting."""
        choice = getattr(self, "choice_context", None)
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "bargain"):
            return False
        options = choice.get("options", [])
        selected_id = None
        if option_index is not None:
            if not isinstance(option_index, int) or not 0 <= option_index < len(options):
                return False
            selected_id = options[option_index]
            controller = choice.get("controller") or choice.get("player")
            if selected_id not in self._bargain_options(controller, self._safe_get_card):
                return False
        cast_context = dict(choice.get("original_cast_context", {}))
        cast_context["bargain_choice_complete"] = True
        cast_context["bargained"] = selected_id is not None
        cast_context["bargain_sacrifice_id"] = selected_id
        return self._resume_after_casting_choice(choice, cast_context)

    def choose_casting_additional_return(self, option_index):
        """Pay a mandatory casting cost by returning one controlled permanent."""
        choice = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "casting_additional_return"):
            return False
        options = choice.get("options", [])
        if not isinstance(option_index, int) or not 0 <= option_index < len(options):
            return False
        permanent_id = options[option_index]
        controller = choice.get("controller") or choice.get("player")
        if permanent_id not in controller.get("battlefield", []):
            return False
        in_p1_deck = permanent_id in getattr(self, "original_p1_deck", [])
        in_p2_deck = permanent_id in getattr(self, "original_p2_deck", [])
        # Mirror decks reuse numeric card IDs for two physical occurrences.
        # This choice was built from controller's battlefield, so that zone
        # membership is authoritative.  A global P1-first controller lookup
        # cannot distinguish P2's occurrence of the same ID.
        owner = (controller if in_p1_deck and in_p2_deck
                 else self._find_card_owner_fallback(permanent_id) or controller)
        if not self.move_card(
                permanent_id, controller, "battlefield", owner, "hand",
                cause="additional_cost",
                context={"source_id": choice.get("card_id"),
                         "casting_additional_cost": "return_permanent"}):
            return False

        cast_context = dict(choice.get("original_cast_context", {}))
        cast_context["sample_nonmana_cost_complete"] = True
        cast_context["returned_for_additional_cost"] = permanent_id
        success = self._resume_after_casting_choice(choice, cast_context)
        if not success and permanent_id in owner.get("hand", []):
            self.move_card(
                permanent_id, owner, "hand", controller, "battlefield",
                cause="additional_cost_rollback")
        return success

    def choose_collect_evidence_card(self, option_index):
        """Stage one graveyard card toward a collect-evidence threshold."""
        choice = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "collect_evidence"):
            return False
        options = choice.get("options", [])
        if not isinstance(option_index, int) or not 0 <= option_index < len(options):
            return False
        card_id = options[option_index]
        controller = choice.get("controller") or choice.get("player")
        if card_id not in controller.get("graveyard", []):
            return False
        options.pop(option_index)
        choice.setdefault("selected_cards", []).append(card_id)
        card = self._safe_get_card(card_id)
        try:
            mana_value = int(getattr(card, "cmc", 0) or 0)
        except (TypeError, ValueError):
            mana_value = 0
        choice["selected_mana_value"] = (
            int(choice.get("selected_mana_value", 0)) + mana_value)
        return True

    def finish_collect_evidence_choice(self):
        """Decline evidence, or exile staged cards and resume the pending cast."""
        choice = self.choice_context
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "collect_evidence"):
            return False
        controller = choice.get("controller") or choice.get("player")
        selected = list(choice.get("selected_cards", []))
        total = int(choice.get("selected_mana_value", 0))
        threshold = int(choice.get("threshold", 0))
        if selected and total < threshold:
            return False

        moved = []
        if selected:
            for card_id in selected:
                if not self.move_card(
                        card_id, controller, "graveyard", controller, "exile",
                        cause="collect_evidence",
                        context={"source_id": choice.get("card_id"),
                                 "evidence_threshold": threshold}):
                    for moved_id in reversed(moved):
                        self.move_card(
                            moved_id, controller, "exile", controller, "graveyard",
                            cause="additional_cost_rollback")
                    return False
                moved.append(card_id)

        cast_context = dict(choice.get("original_cast_context", {}))
        cast_context["sample_nonmana_cost_complete"] = True
        cast_context["evidence_collected"] = bool(selected)
        cast_context["evidence_cards"] = list(selected)
        success = self._resume_after_casting_choice(choice, cast_context)
        if not success:
            for card_id in reversed(moved):
                self.move_card(
                    card_id, controller, "exile", controller, "graveyard",
                    cause="additional_cost_rollback")
        return success

    @staticmethod
    def _effect_targets_from_context(context):
        """Combine chosen targets with casting choices consumed by effects."""
        targets = copy_module.deepcopy(context.get("targets") or {})
        if "X" in context:
            targets["X"] = context["X"]
        if "evidence_collected" in context:
            targets["evidence_collected"] = bool(context["evidence_collected"])
        return targets

    @staticmethod
    def _combine_cost_dicts(cost_dict1, cost_dict2):
        """Helper to combine two parsed mana cost dictionaries."""
        combined = cost_dict1.copy()
        for key, value in cost_dict2.items():
            if isinstance(value, list):
                combined[key] = list(combined.get(key, [])) + list(value)
            elif isinstance(value, dict):
                nested = dict(combined.get(key, {}))
                nested.update(value)
                combined[key] = nested
            else:
                combined[key] = combined.get(key, 0) + value
        return combined

    @staticmethod
    def _player_key_for_permission(player, p1):
        return "p1" if player is p1 else "p2"

    @staticmethod
    def _printed_flashback_cost(card):
        text = getattr(card, "oracle_text", "") or ""
        match = re.search(
            r"(?:^|\n)flashback\s+((?:\{[^}]+\})+)", text,
            re.IGNORECASE)
        return match.group(1) if match else None

    def flashback_cost_for(self, player, card_id):
        """Return the currently legal Flashback cost for a graveyard card."""
        if card_id not in player.get("graveyard", []):
            return None
        card = self._safe_get_card(card_id)
        printed = self._printed_flashback_cost(card)
        if printed:
            return printed
        for entry in getattr(self, "flashback_permissions", []):
            if (entry.get("card_id") == card_id
                    and entry.get("player") == self._discard_player_key(player)
                    and entry.get("expires_turn", self.turn) >= self.turn):
                return entry.get("cost")
        return None

    @staticmethod
    def _printed_harmonize_cost(card):
        text = getattr(card, "oracle_text", "") or ""
        match = re.search(
            r"(?:^|\n)harmonize\s+((?:\{[^}]+\})+)", text,
            re.IGNORECASE)
        return match.group(1) if match else None

    def harmonize_cost_for(self, player, card_id):
        """Return a printed Harmonize cost for a card in that graveyard."""
        if card_id not in player.get("graveyard", []):
            return None
        return self._printed_harmonize_cost(self._safe_get_card(card_id))

    def finalize_harmonize_tap_choice(self, creature_id=None):
        """Tap zero or one creature, then resume a pending Harmonize cast."""
        choice = getattr(self, "choice_context", None)
        if not (self.phase == self.PHASE_CHOOSE and choice
                and choice.get("type") == "harmonize_tap"):
            return False
        player = choice.get("player")
        card_id = choice.get("card_id")
        reduction = 0
        if creature_id is not None:
            if (creature_id not in choice.get("options", [])
                    or creature_id not in player.get("battlefield", [])
                    or creature_id in player.get("tapped_permanents", set())):
                return False
            creature = self._safe_get_card(creature_id)
            reduction = max(0, int(getattr(creature, "power", 0) or 0))
            if not self.tap_permanent(creature_id, player):
                return False
        cast_context = dict(choice.get("cast_context", {}))
        cast_context["harmonize_reduction"] = reduction
        self.choice_context = None
        self.phase = choice.get("resume_phase", self.PHASE_MAIN_PRECOMBAT)
        self.priority_player = player
        success = self.cast_spell(card_id, player, cast_context)
        if not success and creature_id is not None:
            self.untap_permanent(creature_id, player)
        return success

    def grant_flashback_permission(self, player, card_id, cost=None):
        """Grant one instant/sorcery card Flashback until end of turn."""
        if card_id not in player.get("graveyard", []):
            return False
        card = self._safe_get_card(card_id)
        if not card or not set(getattr(card, "card_types", [])).intersection(
                {"instant", "sorcery"}):
            return False
        cost = cost or getattr(card, "mana_cost", "")
        if not cost:
            return False
        key = self._discard_player_key(player)
        self.flashback_permissions = [
            entry for entry in getattr(self, "flashback_permissions", [])
            if not (entry.get("card_id") == card_id
                    and entry.get("player") == key)
        ]
        self.flashback_permissions.append({
            "card_id": card_id, "cost": cost,
            "expires_turn": self.turn, "player": key,
        })
        return True

    def grant_graveyard_adventure_permission(self, player, card_id):
        """Permit this graveyard object to be cast as its Adventure half."""
        if card_id not in player.get("graveyard", []):
            return False
        card = self._safe_get_card(card_id)
        adventure = card.get_adventure_data() if card else None
        if not adventure:
            return False
        active_player = self._get_active_player()
        expires_turn = self.turn + (2 if active_player is player else 1)
        player_key = self._player_key_for_permission(player, self.p1)
        self.graveyard_adventure_permissions = [
            entry for entry in self.graveyard_adventure_permissions
            if not (entry.get("card_id") == card_id
                    and entry.get("controller") == player_key)
        ]
        self.graveyard_adventure_permissions.append({
            "card_id": card_id,
            "controller": player_key,
            "granted_turn": self.turn,
            "expires_turn": expires_turn,
        })
        return True

    def has_graveyard_adventure_permission(self, player, card_id):
        player_key = self._player_key_for_permission(player, self.p1)
        return any(
            entry.get("card_id") == card_id
            and entry.get("controller") == player_key
            and entry.get("expires_turn", -1) >= self.turn
            and card_id in player.get("graveyard", [])
            for entry in self.graveyard_adventure_permissions)

    def _consume_graveyard_adventure_permission(self, player, card_id):
        player_key = self._player_key_for_permission(player, self.p1)
        before = len(self.graveyard_adventure_permissions)
        self.graveyard_adventure_permissions = [
            entry for entry in self.graveyard_adventure_permissions
            if not (entry.get("card_id") == card_id
                    and entry.get("controller") == player_key)
        ]
        return len(self.graveyard_adventure_permissions) != before

    def plot_card(self, player, hand_index):
        """Take the Plot special action from hand at sorcery speed."""
        if not self._can_act_at_sorcery_speed(player):
            return False
        if not isinstance(hand_index, int) or not 0 <= hand_index < len(player.get("hand", [])):
            return False
        card_id = player["hand"][hand_index]
        card = self._safe_get_card(card_id)
        plot_cost = getattr(card, "plot_cost", None) if card else None
        if not card or not getattr(card, "is_plot", False) or not plot_cost:
            return False
        parsed_cost = self.mana_system.parse_mana_cost(plot_cost)
        if not self.mana_system.can_pay_mana_cost_with_lands(player, parsed_cost):
            return False
        paid = self.mana_system.pay_mana_cost_get_details(player, parsed_cost)
        if paid is None:
            return False
        if not self.move_card(
                card_id, player, "hand", player, "exile", cause="plot",
                context={"plot_cost": plot_cost}):
            self.mana_system.add_mana(player, paid.get("spent_specific", {}))
            return False
        self.plotted_cards.append({
            "card_id": card_id,
            "controller": self._player_key_for_permission(player, self.p1),
            "plotted_turn": self.turn,
        })
        return True

    @staticmethod
    def _prepare_spell_face(card):
        """Return the castable spell face of a Prepare-layout card."""
        if (not card or getattr(card, "layout", "") != "prepare"
                or not getattr(card, "faces", None)
                or len(card.faces) < 2):
            return None
        face = dict(card.faces[1] or {})
        type_line = str(face.get("type_line", "")).lower()
        if not any(card_type in type_line for card_type in ("instant", "sorcery")):
            return None
        return face

    def can_cast_prepared_copy(self, source_id, player):
        """Whether ``player`` can cast a prepared permanent's spell-face copy."""
        card = self._safe_get_card(source_id)
        face = self._prepare_spell_face(card)
        if (not face or source_id not in player.get("battlefield", [])
                or source_id not in self.prepared_cards):
            return False
        context = {
            "prepared_copy": True,
            "prepared_source_id": source_id,
            "prepared_face": face,
            "source_zone": "prepared_exile",
            "is_copy": True,
            "skip_default_movement": True,
            "effect_text": face.get("oracle_text", ""),
        }
        if not self._can_cast_now(source_id, player, context=context):
            return False

        effect_text = str(face.get("oracle_text", "") or "")
        for slot in self._ordinary_target_slots(effect_text):
            minimum = int(slot.get("min_targets", 0))
            if minimum <= 0:
                continue
            valid = self.targeting_system.get_valid_targets(
                source_id, player, slot.get("required_type", "target"),
                effect_text=slot.get("effect_text", effect_text))
            valid_ids = {
                target_id for target_ids in valid.values()
                for target_id in target_ids
            }
            if len(valid_ids) < minimum:
                return False

        # Use the spell face as the object being costed so type-restricted mana,
        # taxes, and reductions see an instant/sorcery rather than the creature.
        context["card"] = Card(face)
        parsed_cost = self.mana_system.parse_mana_cost(
            face.get("mana_cost", ""))
        final_cost = self.mana_system.apply_cost_modifiers(
            player, parsed_cost, source_id, context)
        return self.mana_system.can_pay_mana_cost_with_lands(
            player, final_cost, context)

    def cast_prepared_copy(self, source_id, player):
        """Start casting a prepared permanent's spell face as a copy."""
        if not self.can_cast_prepared_copy(source_id, player):
            return False
        face = self._prepare_spell_face(self._safe_get_card(source_id))
        return self.cast_spell(source_id, player, {
            "prepared_copy": True,
            "prepared_source_id": source_id,
            "prepared_source_generation": getattr(
                self._safe_get_card(source_id), "_zone_change_generation", 0),
            "prepared_face": face,
            "source_zone": "prepared_exile",
            "is_copy": True,
            "skip_default_movement": True,
            "effect_text": face.get("oracle_text", ""),
        })

    def choose_prepared_payment_card(self, option_index):
        """Stage one graveyard occurrence; commit all eight atomically."""
        choice = getattr(self, "choice_context", None)
        if not choice or choice.get("type") != "prepared_payment":
            return False
        page = int(choice.get("choice_page", 0))
        absolute = page * 10 + int(option_index)
        options = choice.get("options", [])
        if not 0 <= absolute < len(options):
            return False
        choice.setdefault("selected_cards", []).append(options.pop(absolute))
        choice["choice_page"] = 0
        if len(choice["selected_cards"]) < int(
                choice.get("required_count", 8)):
            return True

        controller = choice.get("player")
        source_id = choice.get("source_id")
        source = self._safe_get_card(source_id)
        source_controller, source_zone = self.find_card_location(source_id)
        valid_source = (
            source_controller is controller and source_zone == "battlefield"
            and source_id not in self.prepared_cards
            and getattr(source, "_zone_change_generation", 0)
            == choice.get("source_generation"))
        remaining = list(controller.get("graveyard", []))
        for card_id in choice["selected_cards"]:
            if card_id not in remaining:
                valid_source = False
                break
            remaining.remove(card_id)
        if not valid_source:
            return self.decline_prepared_payment()

        for card_id in choice["selected_cards"]:
            if not self.move_card(
                    card_id, controller, "graveyard", controller, "exile",
                    cause="prepare", context={"source_id": source_id}):
                logging.error(
                    "Prepared payment failed after its graveyard snapshot "
                    "was validated for source %s.", source_id)
                return False
        self.prepared_cards.add(source_id)
        if choice.get("effect_continuation"):
            self._resume_effect_continuation(choice)
        else:
            self.choice_context = None
            self.phase = self._normalized_choice_resume_phase(
                choice.get("resume_phase"))
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0
        return True

    def decline_prepared_payment(self):
        """Decline Prepare without moving any staged graveyard cards."""
        choice = getattr(self, "choice_context", None)
        if not (choice and choice.get("type") == "prepared_payment"):
            return False
        if choice.get("effect_continuation"):
            self._resume_effect_continuation(choice)
        else:
            self.choice_context = None
            self.phase = self._normalized_choice_resume_phase(
                choice.get("resume_phase"))
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0
        return True

    def get_exile_cast_options(self, player):
        """Return deterministic ordinary and Plot casting permissions in exile."""
        options = []
        ordinary = getattr(self, "cards_castable_from_exile", set())
        seen_ordinary = set()
        for source_idx, card_id in enumerate(player.get("exile", [])):
            if card_id in ordinary and card_id not in seen_ordinary:
                options.append({
                    "card_id": card_id,
                    "source_idx": source_idx,
                    "permission": (
                        "airbend" if card_id in getattr(
                            self, "exile_alternative_costs", {})
                        else "ordinary"),
                    "alternative_cost": getattr(
                        self, "exile_alternative_costs", {}).get(card_id),
                })
                seen_ordinary.add(card_id)

        if self._can_act_at_sorcery_speed(player):
            player_key = self._player_key_for_permission(player, self.p1)
            for entry in getattr(self, "plotted_cards", []):
                card_id = entry.get("card_id")
                if (entry.get("controller") != player_key
                        or entry.get("plotted_turn", self.turn) >= self.turn
                        or card_id not in player.get("exile", [])):
                    continue
                options.append({
                    "card_id": card_id,
                    "source_idx": player["exile"].index(card_id),
                    "permission": "plot",
                    "plotted_turn": entry.get("plotted_turn"),
                })
        return options

    def _consume_plot_permission(self, player, card_id):
        player_key = self._player_key_for_permission(player, self.p1)
        for index, entry in enumerate(self.plotted_cards):
            if (entry.get("card_id") == card_id
                    and entry.get("controller") == player_key):
                self.plotted_cards.pop(index)
                return True
        return False

    def cast_spell(self, card_id, player, context=None):
        """
        Cast a spell: Validate source/timing -> Determine Cost -> Pay Costs -> Move to Stack (or Enter Choice Phase) -> Set up Targeting/Choices.
        Handles regular casts, alternative costs (incl. Impending), additional costs (incl. Offspring), modal spells, and targeting setup.
        """
        if context is None: context = {}
        card = self._safe_get_card(card_id)
        if not card:
             logging.error(f"Cannot cast spell: Invalid card_id {card_id}")
             return False
        if context.get("prepared_copy"):
            prepared_face = self._prepare_spell_face(card)
            expected_generation = context.get("prepared_source_generation")
            if (not prepared_face
                    or card_id not in player.get("battlefield", [])
                    or card_id not in self.prepared_cards
                    or (expected_generation is not None
                        and expected_generation != getattr(
                            card, "_zone_change_generation", 0))):
                logging.warning(
                    "Prepared copy cast lost its live prepared source %s.",
                    card_id)
                return False
            context["prepared_face"] = prepared_face
            context["effect_text"] = prepared_face.get("oracle_text", "")
            context["source_zone"] = "prepared_exile"
            context["is_copy"] = True
            context["skip_default_movement"] = True
            # Mana restrictions and modifiers must see the copied spell face,
            # not the creature permanent that created it.
            context["card"] = Card(prepared_face)
        # The mana system's conditional-pool checks ("spend this mana only to
        # cast...") identify the spell from context['card']; without it every
        # cast ignored conditional mana during affordability/payment. Only the
        # card OBJECT is set: adding 'card_id' would make can_pay/pay re-apply
        # cost modifiers to the precomputed final cost (the Round 7.16 bug).
        context.setdefault('card', card)

        # --- 1. Validate Source Zone and Timing ---
        source_zone = context.get("source_zone", "hand") # Default source
        if context.get("emblem_graveyard_cast"):
            permanent_spell_types = {
                "creature", "artifact", "enchantment", "planeswalker", "battle"
            }
            has_permission = any(
                emblem.get("kind") == "graveyard_permanents"
                for emblem in player.get("emblems", []))
            if (source_zone != "graveyard" or not has_permission
                    or not permanent_spell_types.intersection(
                        getattr(card, "card_types", []))):
                logging.warning("Invalid Wrenn-emblem graveyard cast permission.")
                return False
        if context.get("graveyard_adventure_cast"):
            if (source_zone != "graveyard"
                    or not context.get("cast_as_adventure")
                    or not self.has_graveyard_adventure_permission(
                        player, card_id)):
                logging.warning("Invalid graveyard Adventure cast permission.")
                return False
        if context.get("flashback_cast"):
            flashback_cost = self.flashback_cost_for(player, card_id)
            if source_zone != "graveyard" or not flashback_cost:
                logging.warning("Invalid Flashback graveyard cast permission.")
                return False
            context.setdefault("flashback_cost", flashback_cost)
            context["use_alt_cost"] = "flashback"
        if context.get("harmonize_cast"):
            harmonize_cost = self.harmonize_cost_for(player, card_id)
            if source_zone != "graveyard" or not harmonize_cost:
                logging.warning("Invalid Harmonize graveyard casting permission.")
                return False
            context.setdefault("harmonize_cost", harmonize_cost)
            context["use_alt_cost"] = "harmonize"
        if context.get("airbend_cast"):
            alternative_cost = getattr(
                self, "exile_alternative_costs", {}).get(card_id)
            if source_zone != "exile" or not alternative_cost:
                logging.warning("Invalid Airbend exile casting permission.")
                return False
            context["alternative_cost"] = alternative_cost
            context["use_alt_cost"] = "exile_permission"
        source_idx = context.get("source_idx")
        source_list = None
        card_in_source = False
        # ...(rest of source zone validation remains the same)...
        if source_zone == "command":
            source_list = player.get(source_zone)
            if isinstance(source_list, (list, set)) and card_id in source_list: card_in_source = True
        elif source_zone in ["stack_implicit", "library_implicit", "hand_implicit", "nonexistent_zone", "prepared_exile"]: card_in_source = True; source_list = []
        else:
             source_list = player.get(source_zone)
             if source_list is not None:
                  if isinstance(source_list, (list, set)) and card_id in source_list:
                       card_in_source = True
                       if source_idx is None and isinstance(source_list, list):
                            try: source_idx = source_list.index(card_id)
                            except ValueError: card_in_source = False
                  elif isinstance(source_list, dict) and card_id in source_list: card_in_source = True

        if not card_in_source:
            logging.warning(f"Cannot cast {getattr(card,'name', card_id)}: Not found in {player['name']}'s {source_zone}.")
            return False
        if not self._can_cast_now(card_id, player, context=context):
            priority_name = (
                self.priority_player.get('name', 'None')
                if isinstance(self.priority_player, dict)
                else getattr(self.priority_player, 'name', 'None'))
            logging.warning(
                f"Cannot cast {getattr(card,'name', card_id)}: Invalid timing "
                f"(Phase: {self._PHASE_NAMES.get(self.phase)}, "
                f"Prio: {priority_name}, Stack:{len(self.stack)}).")
            return False

        # --- 2. Check for Modal Spell ---
        modal_modes, min_modes, max_modes = None, 0, 0
        is_spree_spell = bool(
            getattr(card, 'is_spree', False)
            and getattr(card, 'spree_modes', None))
        is_modal_spell = is_spree_spell
        if is_spree_spell:
             modal_modes = [
                 str(mode.get('effect', '') or '')
                 for mode in card.spree_modes]
             min_modes, max_modes = 1, len(modal_modes)
        else:
             # CR 601.2b announces modes of the spell being cast, not modes
             # embedded in an ability of a permanent spell.  Parsing the full
             # text of every card made Cosmogrand Zenith's
             # ``Whenever ... choose one`` trigger look like creature-spell
             # modes.  Resolution then ran the chosen trigger instruction but
             # never put the popped creature spell onto the battlefield.
             modal_spell_type = getattr(card, 'type_line', '') or ''
             modal_spell_text = getattr(card, 'oracle_text', '') or ''
             if context.get('prepared_copy'):
                  prepared_face = context.get('prepared_face', {})
                  modal_spell_type = prepared_face.get(
                      'type_line', '') or modal_spell_type
                  modal_spell_text = prepared_face.get(
                      'oracle_text', '') or ''
             elif (context.get('cast_as_back_face')
                     and hasattr(card, 'get_face_type_line')):
                  modal_spell_type = (
                      card.get_face_type_line(1) or modal_spell_type)
                  modal_spell_text = card.get_face_text(1) or ''
             elif (context.get('cast_as_adventure')
                   and hasattr(card, 'get_adventure_data')):
                  adventure = card.get_adventure_data() or {}
                  modal_spell_type = adventure.get('type', '') or ''
                  modal_spell_text = adventure.get('effect', '') or ''
             has_spell_instructions = any(
                 card_type in modal_spell_type.lower()
                 for card_type in ('instant', 'sorcery'))
             if (has_spell_instructions and self.ability_handler
                     and hasattr(self.ability_handler, '_parse_modal_text')):
                  modal_modes, min_modes, max_modes = \
                      self.ability_handler._parse_modal_text(modal_spell_text)
             if modal_modes: is_modal_spell = True

        # Modes are chosen before targets and costs are determined (CR 601.2b).
        modes_are_announced = (
            'selected_spree_modes' in context if is_spree_spell
            else 'selected_modes' in context)
        if is_modal_spell and not modes_are_announced:
             if (is_spree_spell and not any(
                     self.spree_mode_is_selectable(
                         card_id, player, [], mode_index, context=context)
                     for mode_index in range(len(modal_modes)))):
                 logging.warning(
                     f"Cannot cast {card.name}: no legal affordable Spree mode.")
                 return False
             choice_context = {
                 'type': 'choose_mode', 'player': player, 'controller': player,
                 'card_id': card_id,
                 'num_choices': len(modal_modes),
                 'min_required': min_modes, 'max_required': max_modes,
                 'available_modes': modal_modes, 'selected_modes': [],
                 'is_spree': is_spree_spell,
                 'mode_costs': ([mode.get('cost', '') for mode in card.spree_modes]
                                if is_spree_spell else []),
                 'original_cast_context': self._copy_stack_context(context),
                 'resolved': False,
             }
             self._begin_casting_choice(choice_context)
             logging.info(f"Waiting for mode choice before casting {card.name}.")
             return True

        # --- 3. Determine Base Cost String ---
        cast_for_impending = context.get('cast_for_impending', False) # Check flag set by handler
        alt_cost_type = None # Assume no alt cost initially
        if cast_for_impending: alt_cost_type = 'impending' # Flag for cost modification checks
        elif context.get('use_alt_cost'): # Check for other generic alt cost flags
             alt_cost_type = context.get('use_alt_cost')

        base_cost_str = "" # Default empty
        final_cost_dict = {} # Store parsed/modified cost

        if cast_for_impending:
             impending_cost_str = getattr(card, 'impending_cost', None)
             if not impending_cost_str: return False
             base_cost_str = impending_cost_str
             # context['cast_for_impending'] = True # Already set by caller
        elif alt_cost_type == 'plot':
             final_cost_dict = self.mana_system.parse_mana_cost("")
        elif alt_cost_type: # Other alternative costs handled first
             final_cost_dict = self.mana_system.calculate_alternative_cost(card_id, player, alt_cost_type, context)
             if final_cost_dict is None: return False
        else: # Normal cost
            base_cost_str = getattr(card, 'mana_cost', '')
            if context.get('cast_as_adventure') and hasattr(
                    card, 'get_adventure_data'):
                adventure = card.get_adventure_data() or {}
                if not adventure:
                    return False
                base_cost_str = adventure.get('cost', '')
                context['effect_text'] = adventure.get('effect', '')
            # MDFC back-face casting (July 2026): use the BACK face's mana cost
            # when this cast is flagged as the back face. Previously the spell
            # path always used the front cost, so casting a spell MDFC's back
            # face charged the wrong amount.
            if context.get('cast_as_back_face') and hasattr(card, 'get_face_cost'):
                _back_cost = card.get_face_cost(1)
                base_cost_str = _back_cost if _back_cost is not None else base_cost_str
                context['effect_text'] = card.get_face_text(1)

        # --- 4. Calculate Final Cost (Mana & Non-Mana) ---
        # Parse base cost if applicable
        if context.get('prepared_copy'):
             prepared_face = context.get('prepared_face', {})
             base_cost_str = prepared_face.get('mana_cost', '')
             context['effect_text'] = prepared_face.get('oracle_text', '')
             final_cost_dict = self.mana_system.parse_mana_cost(
                 base_cost_str)
        elif cast_for_impending:
            # Impending replaces the normal cost with another printed mana
            # cost. Parse that cost before modifiers/payment; treating it like
            # a non-mana alternative left an empty dict and could become either
            # a sparse-cost crash or a free cast.
            final_cost_dict = self.mana_system.parse_mana_cost(base_cost_str)
        elif base_cost_str and not alt_cost_type:
            final_cost_dict = self.mana_system.parse_mana_cost(base_cost_str)
        elif not alt_cost_type: # Handle cases with no base cost (like Suspend resolution?)
            final_cost_dict = self.mana_system.parse_mana_cost("")

        # Add additional mana costs ONLY IF NOT using a fully replacing alternative cost
        # Check alt_cost_type (Impending is handled above, others might replace fully)
        apply_additional_costs = alt_cost_type in (None, 'plot')

        if apply_additional_costs:
            pay_offspring = context.get('pay_offspring', False)
            if pay_offspring and getattr(card, 'is_offspring', False):
                offspring_cost_str = getattr(card, 'offspring_cost', None)
                if offspring_cost_str:
                    offspring_cost_dict = self.mana_system.parse_mana_cost(offspring_cost_str)
                    # *** FIXED: Use internal helper ***
                    final_cost_dict = self._combine_cost_dicts(final_cost_dict, offspring_cost_dict)
                    context['paid_offspring'] = True # Add final flag for ETB trigger check
            # Kicker
            if context.get('kicked'):
                kicker_cost_str = context.get('kicker_cost_to_pay')
                if kicker_cost_str:
                    kicker_cost_dict = self.mana_system.parse_mana_cost(kicker_cost_str)
                    # *** FIXED: Use internal helper ***
                    final_cost_dict = self._combine_cost_dicts(final_cost_dict, kicker_cost_dict)
                    context['actual_kicker_paid'] = kicker_cost_str
            # Escalate
            escalate_count = context.get('escalate_count', 0)
            if escalate_count > 0:
                escalate_cost_each_str = context.get('escalate_cost_each')
                if escalate_cost_each_str:
                    escalate_cost_each_dict = self.mana_system.parse_mana_cost(escalate_cost_each_str)
                    # Combine cost N times
                    for _ in range(escalate_count):
                        # *** FIXED: Use internal helper repeatedly ***
                        final_cost_dict = self._combine_cost_dicts(final_cost_dict, escalate_cost_each_dict)

        # Spree mode costs are mandatory additional costs for the modes chosen
        # at announcement.  They remain payable even when an effect waives or
        # replaces the printed mana cost, so this intentionally sits outside
        # ``apply_additional_costs`` and before the one modifier pass.
        if is_spree_spell:
            selected_spree_modes = sorted(set(
                context.get('selected_spree_modes', [])))
            if (not selected_spree_modes
                    or any(not isinstance(index, int)
                           or not 0 <= index < len(card.spree_modes)
                           for index in selected_spree_modes)):
                logging.warning(
                    f"Cannot cast {card.name}: Spree requires one or more "
                    "distinct valid modes.")
                return False
            context['selected_spree_modes'] = selected_spree_modes
            context['is_spree'] = True
            context['spree_mode_costs'] = []
            for mode_index in selected_spree_modes:
                mode_cost_text = card.spree_modes[mode_index].get('cost', '')
                mode_cost = self.mana_system.parse_mana_cost(mode_cost_text)
                final_cost_dict = self._combine_cost_dicts(
                    final_cost_dict, mode_cost)
                context['spree_mode_costs'].append(mode_cost_text)

        # Work out target bounds before the final cost because some sample
        # spells price themselves from the targets chosen at CR 601.2c.
        requires_target = False
        num_targets = 0
        up_to_N = False
        explicit_effect_text = context.get('effect_text')
        spell_types = set(getattr(card, 'card_types', []) or [])
        aura_target_text = aura_cast_targeting_text(card)
        # Printed targets in a permanent's triggered/activated abilities are
        # not targets of the permanent spell itself.  Alternate casts such as
        # Mutate provide their actual spell text explicitly; instants and
        # sorceries use their printed resolving instructions.
        if explicit_effect_text is not None:
            targeting_text = explicit_effect_text
        elif aura_target_text:
            targeting_text = aura_target_text
            # Resolution must validate the same Enchant restriction announced
            # during casting, not a later targeted ability printed on the Aura.
            context['targeting_text'] = targeting_text
        elif spell_types.intersection({'instant', 'sorcery'}):
            targeting_text = getattr(card, 'oracle_text', '')
        else:
            targeting_text = ''
        selected_modes = []
        spree_target_slots = []
        instruction_target_slots = []
        if is_modal_spell:
            selected_modes = context.get(
                'selected_spree_modes' if is_spree_spell else 'selected_modes',
                [])
            targeting_text = " ".join(
                modal_modes[index]
                for index in selected_modes
                if 0 <= index < len(modal_modes)
            )
        if is_spree_spell:
            spree_target_slots = self._spree_target_slots(
                card, selected_modes)
            context['spree_target_slots'] = copy_module.deepcopy(
                spree_target_slots)
            requires_target = bool(spree_target_slots)
            min_targets = sum(
                int(slot.get('min_targets', 0))
                for slot in spree_target_slots)
            num_targets = sum(
                int(slot.get('max_targets', 0))
                for slot in spree_target_slots)
            up_to_N = any(
                int(slot.get('min_targets', 0)) == 0
                for slot in spree_target_slots)
        else:
            instruction_target_slots = self._ordinary_target_slots(
                targeting_text)
            if instruction_target_slots:
                context['instruction_target_slots'] = copy_module.deepcopy(
                    instruction_target_slots)
                requires_target = True
                min_targets = sum(
                    int(slot.get('min_targets', 0))
                    for slot in instruction_target_slots)
                num_targets = sum(
                    int(slot.get('max_targets', 0))
                    for slot in instruction_target_slots)
                up_to_N = any(
                    int(slot.get('min_targets', 0)) == 0
                    for slot in instruction_target_slots)
            else:
                active_targeting_text = \
                    self._ordinary_single_targeting_text(targeting_text)
                if active_targeting_text != targeting_text:
                    targeting_text = active_targeting_text
                    # Resolution-time target validation must use the same
                    # announced branch that casting and the action mask used.
                    context['targeting_text'] = targeting_text
                targeting_text_lower = targeting_text.lower()
                requires_target = "target" in targeting_text_lower
                parsed_min, parsed_max = (
                    self._target_bounds_from_text(targeting_text)
                    if requires_target else (0, 0))
                num_targets = parsed_max
                up_to_N = parsed_min == 0
                min_targets = parsed_min
        casting_target_slots = (
            spree_target_slots or instruction_target_slots)
        targets_committed = (
            "targets" in context and isinstance(context.get("targets"), dict))
        target_selection_pending = False
        target_selection_affordable_via_reduction = False

        # Apply Generic Cost Modifiers LAST. Preserve the premodifier total so
        # an affordability probe can recalculate once with a candidate target.
        cost_before_modifiers = final_cost_dict.copy()
        final_cost_dict = self.mana_system.apply_cost_modifiers(
            player, final_cost_dict, card_id, context)

        # X is chosen while casting, before affordability, payment, or zone
        # movement. Resume the same cast after the agent supplies the value.
        if final_cost_dict.get('X', 0) > 0 and 'X' not in context:
            affordable_values = []
            # Bound X by actual available mana sources instead of an arbitrary
            # numeric ceiling. This stays finite even for generated pools and
            # cannot hide a legal value above a hard-coded simulator limit.
            mana_upper_bound = sum(
                max(0, int(amount or 0))
                for amount in player.get('mana_pool', {}).values())
            mana_upper_bound += sum(
                max(0, int(amount or 0))
                for pool in player.get('conditional_mana', {}).values()
                if isinstance(pool, dict)
                for amount in pool.values())
            mana_upper_bound += sum(
                max(0, int(amount or 0))
                for amount in player.get(
                    'phase_restricted_mana', {}).values())
            for permanent_id in player.get('battlefield', []):
                if permanent_id in player.get('tapped_permanents', set()):
                    continue
                permanent = self._safe_get_card(permanent_id)
                if not permanent or 'land' not in getattr(
                        permanent, 'card_types', []):
                    continue
                output_counts = []
                for line in str(getattr(
                        permanent, 'oracle_text', '') or '').splitlines():
                    match = re.search(
                        r"\{t\}\s*:\s*add\s+([^.;\n]+)", line,
                        re.IGNORECASE)
                    if match:
                        output_counts.append(max(
                            1, len(re.findall(r"\{[WUBRGC]\}",
                                              match.group(1), re.IGNORECASE))))
                if output_counts or self.mana_system._land_mana_options(
                        player, permanent):
                    mana_upper_bound += max(output_counts or [1])
            for x_value in range(mana_upper_bound + 1):
                candidate_context = dict(context)
                candidate_context['X'] = x_value
                if self.mana_system.can_pay_mana_cost_with_lands(
                        player, final_cost_dict, candidate_context):
                    affordable_values.append(x_value)
            if not affordable_values:
                logging.warning(f"Cannot cast {card.name}: no affordable value of X.")
                return False
            choice_context = {
                'type': 'choose_x', 'player': player, 'controller': player,
                'card_id': card_id, 'source_id': card_id,
                'min_x': min(affordable_values), 'max_x': max(affordable_values),
                'affordable_values': affordable_values,
                'choice_page': 0,
                'original_cast_context': dict(context),
            }
            self._begin_casting_choice(choice_context)
            logging.info(f"Waiting for X choice before casting {card.name}.")
            return True

        has_bargain = bool(re.search(
            r"(?:^|\n)\s*bargain(?:\s|\(|$)", getattr(card, "oracle_text", ""),
            re.IGNORECASE))
        if has_bargain and not context.get("bargain_choice_complete", False):
            bargain_options = self._bargain_options(player, self._safe_get_card)
            if bargain_options:
                return self._begin_casting_choice({
                    "type": "bargain",
                    "player": player,
                    "controller": player,
                    "card_id": card_id,
                    "source_id": card_id,
                    "options": bargain_options,
                    "optional": True,
                    "original_cast_context": dict(context),
                })
            context["bargain_choice_complete"] = True
            context["bargained"] = False
            context["bargain_sacrifice_id"] = None

        # --- Check Targets and target-conditioned affordability ---
        target_type = None
        if requires_target and num_targets > 0:
            if not self.targeting_system:
                logging.warning("Cannot check target availability: TargetingSystem missing.")
                return False
            if casting_target_slots:
                target_type = casting_target_slots[0].get(
                    'required_type', 'target')
                if targets_committed:
                    targets_by_slot = context.get('targets_by_slot')
                    if (not isinstance(targets_by_slot, list)
                            or len(targets_by_slot) != len(casting_target_slots)):
                        logging.warning(
                            f"Cannot cast {card.name}: target slots are "
                            "missing or incomplete.")
                        return False
                    for slot, slot_targets in zip(
                            casting_target_slots, targets_by_slot):
                        slot_targets = list(slot_targets or [])
                        slot_min = int(slot.get('min_targets', 0))
                        slot_max = int(slot.get('max_targets', 0))
                        if not slot_min <= len(slot_targets) <= slot_max:
                            logging.warning(
                                f"Cannot cast {card.name}: target instruction "
                                f"{slot.get('mode_index', slot.get('instruction_index'))} chose "
                                f"{len(slot_targets)} targets, expected "
                                f"{slot_min}-{slot_max}.")
                            return False
                        valid_map = self.targeting_system.get_valid_targets(
                            card_id, player,
                            slot.get('required_type', 'target'),
                            effect_text=slot.get('effect_text', ''))
                        valid_ids = {
                            target_id
                            for ids in valid_map.values()
                            for target_id in ids
                        }
                        if any(
                                target_id not in valid_ids
                                for target_id in slot_targets):
                            logging.warning(
                                f"Cannot cast {card.name}: chosen target for "
                                f"instruction {slot.get('mode_index', slot.get('instruction_index'))} "
                                "is illegal.")
                            return False
                else:
                    for slot in casting_target_slots:
                        valid_targets_map = self.targeting_system.get_valid_targets(
                            card_id, player,
                            slot.get('required_type', 'target'),
                            effect_text=slot.get('effect_text', ''))
                        valid_target_ids = {
                            target_id
                            for ids in valid_targets_map.values()
                            for target_id in ids
                        }
                        if len(valid_target_ids) < int(
                                slot.get('min_targets', 0)):
                            logging.warning(
                                f"Cannot cast {card.name}: no legal target for "
                                f"instruction {slot.get('mode_index', slot.get('instruction_index'))}.")
                            return False
                    target_selection_pending = True
            else:
                target_type = self._get_target_type_from_text(targeting_text)
                if targets_committed:
                    chosen_targets = context.get("targets", {})
                    chosen_count = len(self._flatten_target_ids(chosen_targets))
                    if not min_targets <= chosen_count <= num_targets:
                        logging.warning(
                            f"Cannot cast {card.name}: chose {chosen_count} targets, "
                            f"expected {min_targets}-{num_targets}.")
                        return False
                    if not self.targeting_system.validate_targets(
                            card_id, chosen_targets, player,
                            effect_text=targeting_text):
                        logging.warning(f"Cannot cast {card.name}: chosen targets are illegal.")
                        return False
                else:
                    valid_targets_map = self.targeting_system.get_valid_targets(
                        card_id, player, target_type,
                        effect_text=targeting_text)
                    valid_target_ids = {
                        target_id
                        for ids in valid_targets_map.values()
                        for target_id in ids
                    }
                    if len(valid_target_ids) < min_targets:
                        logging.warning(
                            f"Cannot cast {card.name}: Not enough valid targets available "
                            f"({len(valid_target_ids)}/{min_targets} needed).")
                        return False
                    target_selection_pending = True

            if (target_selection_pending
                    and self.mana_system.has_target_dependent_reduction(card)):
                can_pay_without_discount = self.mana_system.can_pay_mana_cost_with_lands(
                    player, final_cost_dict, context)
                can_pay_with_discount = (
                    self.mana_system.can_pay_with_target_dependent_reduction(
                        player, cost_before_modifiers, card_id, context))
                if not (can_pay_without_discount or can_pay_with_discount):
                    logging.warning(
                        f"Cannot cast {card.name}: no legal target selection is affordable.")
                    return False
                target_selection_affordable_via_reduction = can_pay_with_discount

        # --- Check Affordability and nonmana costs ---
        additional_cost_info = context.get('additional_cost_info')
        can_pay_non_mana_add = True
        if context.get('pay_additional') and additional_cost_info:
             if not self.mana_system._can_pay_non_mana_cost(player, additional_cost_info, context):
                  can_pay_non_mana_add = False
                  logging.warning(f"Cannot cast {card.name}: Cannot meet non-mana additional cost.")
        if not can_pay_non_mana_add: return False

        # Check final mana affordability
        if (not self.mana_system.can_pay_mana_cost_with_lands(
                player, final_cost_dict, context)
                and not (target_selection_pending
                         and target_selection_affordable_via_reduction)):
            cost_str_log = self.mana_system._format_mana_cost_for_logging(
                final_cost_dict, context.get('X', 0))
            logging.warning(f"Cannot cast {card.name}: Cannot afford final cost {cost_str_log}.")
            return False

        sample_additional_cost = self._casting_additional_cost(card)
        if (sample_additional_cost
                and not context.get("sample_nonmana_cost_complete", False)):
            if sample_additional_cost["type"] == "return_permanent":
                # Returning a permanent can strip an untapped land the mana
                # plan counts on; only offer returns that keep the final cost
                # payable, so every mask-exposed choice completes the cast.
                options = [
                    permanent_id
                    for permanent_id in dict.fromkeys(
                        player.get("battlefield", []))
                    if self.mana_system.can_pay_mana_cost_with_lands(
                        player, final_cost_dict, context,
                        exclude_ids={permanent_id})]
                if not options:
                    logging.warning(
                        f"Cannot cast {card.name}: no permanent can pay its "
                        "return cost while leaving its mana cost payable.")
                    return False
                return self._begin_casting_choice({
                    "type": "casting_additional_return",
                    "player": player, "controller": player,
                    "card_id": card_id, "source_id": card_id,
                    "options": options,
                    "original_cast_context": dict(context),
                })
            if sample_additional_cost["type"] == "collect_evidence":
                threshold = sample_additional_cost["threshold"]
                available_value = 0
                for graveyard_id in player.get("graveyard", []):
                    graveyard_card = self._safe_get_card(graveyard_id)
                    try:
                        available_value += int(getattr(graveyard_card, "cmc", 0) or 0)
                    except (TypeError, ValueError):
                        pass
                if available_value >= threshold:
                    return self._begin_casting_choice({
                        "type": "collect_evidence",
                        "player": player, "controller": player,
                        "card_id": card_id, "source_id": card_id,
                        "threshold": threshold,
                        "options": list(player.get("graveyard", [])),
                        "selected_cards": [],
                        "selected_mana_value": 0,
                        "original_cast_context": dict(context),
                    })
                context["sample_nonmana_cost_complete"] = True
                context["evidence_collected"] = False

        # CR 601.2c chooses targets before CR 601.2h pays any costs.  Deferring
        # only target-priced spells left ordinary targeted spells able to pay
        # and sacrifice their sole legal target (for example, Bargain on Torch
        # the Tower), stranding the policy in TARGETING with no legal action.
        # Stage every uncommitted target choice, then re-enter cast_spell with
        # the committed target set before mana/nonmana costs or zone movement.
        if target_selection_pending:
            # Preserve both layers of timing state.  PHASE_PRIORITY may wrap a
            # main phase in previous_priority_phase; overwriting that field
            # with PHASE_PRIORITY made a legal sorcery (Duress) fail timing
            # when its target choice resumed.
            targeting_return_phase = self.phase
            targeting_return_previous_priority_phase = \
                self.previous_priority_phase
            first_target_slot = (
                casting_target_slots[0] if casting_target_slots else None)
            self.phase = self.PHASE_TARGETING
            self.targeting_context = {
                "source_id": card_id, "controller": player,
                "required_type": (
                    first_target_slot.get('required_type', 'target')
                    if first_target_slot else target_type),
                "required_count": (
                    int(first_target_slot.get('required_count', 1))
                    if first_target_slot else num_targets),
                "min_targets": (
                    int(first_target_slot.get('min_targets', 1))
                    if first_target_slot else min_targets),
                "max_targets": (
                    int(first_target_slot.get('max_targets', 1))
                    if first_target_slot else num_targets),
                "selected_targets": [],
                "effect_text": (
                    first_target_slot.get('effect_text', '')
                    if first_target_slot else targeting_text),
                "resume_cast": True,
                "original_cast_context": self._copy_stack_context(context),
                "targeting_return_phase": targeting_return_phase,
                "targeting_return_previous_priority_phase":
                    targeting_return_previous_priority_phase,
                # Pure pre-modifier cost snapshot used by the action mask to
                # reject completed target sets that cannot pay.
                "cost_before_modifiers": copy_module.deepcopy(
                    cost_before_modifiers),
            }
            distribution_spec = self._counter_distribution_spec(
                targeting_text)
            if distribution_spec:
                self.targeting_context["counter_distribution"] = \
                    distribution_spec
            if casting_target_slots:
                self.targeting_context.update({
                    'target_slots': copy_module.deepcopy(casting_target_slots),
                    'target_slot_index': 0,
                    'targets_by_slot': [],
                })
            self.priority_player = player
            self.priority_pass_count = 0
            logging.info(f"Waiting for targets before casting {card.name}.")
            return True

        # --- Costs Paid Here ---
        # 1. Pay Non-Mana Additional Costs FIRST
        if context.get('pay_additional') and additional_cost_info:
            if not self.mana_system._pay_non_mana_cost(player, additional_cost_info, context):
                logging.warning(f"Failed to pay non-mana additional cost for {card.name}.")
                return False

        # 2. Pay Final Mana Cost
        paid_mana_details = self.mana_system.pay_mana_cost_get_details(player, final_cost_dict, context)
        if paid_mana_details is None:
             logging.warning(f"Failed to pay final mana cost for {card.name}. Rolling back non-mana costs...")
             if context.get('pay_additional') and additional_cost_info:
                  self.mana_system._rollback_non_mana_cost(player, additional_cost_info, context)
             return False

        bargain_sacrifice_id = context.get("bargain_sacrifice_id")
        bargain_sacrifice_was_token = False
        if context.get("bargained"):
             if bargain_sacrifice_id not in self._bargain_options(player, self._safe_get_card):
                  self.mana_system.add_mana(
                      player, paid_mana_details.get('spent_specific', {}))
                  if context.get('pay_additional') and additional_cost_info:
                      self.mana_system._rollback_non_mana_cost(
                          player, additional_cost_info, context)
                  return False
             bargain_card = self._safe_get_card(bargain_sacrifice_id)
             bargain_sacrifice_was_token = bool(
                 getattr(bargain_card, "is_token", False))
             if not self.move_card(
                     bargain_sacrifice_id, player, "battlefield", player,
                     "graveyard", cause="bargain",
                     context={"source_id": card_id}):
                  self.mana_system.add_mana(
                      player, paid_mana_details.get('spent_specific', {}))
                  if context.get('pay_additional') and additional_cost_info:
                      self.mana_system._rollback_non_mana_cost(
                          player, additional_cost_info, context)
                  return False

        # --- Move Card from Source Zone ---
        removed = False
        source_list_live = player.get(source_zone)
        if source_list_live is not None:
             if isinstance(source_list_live, list) and source_idx is not None and 0 <= source_idx < len(source_list_live) and source_list_live[source_idx] == card_id:
                  source_list_live.pop(source_idx)
                  removed = True
             elif isinstance(source_list_live, (list, set)) and card_id in source_list_live:
                 if isinstance(source_list_live, list): source_list_live.remove(card_id)
                 elif isinstance(source_list_live, set): source_list_live.discard(card_id)
                 removed = True
        elif source_zone in ["stack_implicit", "library_implicit", "hand_implicit", "nonexistent_zone", "prepared_exile"]: removed = True

        if not removed:
             logging.error(f"CRITICAL: Could not remove {card.name} from {source_zone} after paying costs.")
             if paid_mana_details: self.mana_system.add_mana(player, paid_mana_details.get('spent_specific',{}))
             if context.get('pay_additional') and additional_cost_info: self.mana_system._rollback_non_mana_cost(player, additional_cost_info, context)
             if (bargain_sacrifice_id is not None
                     and not bargain_sacrifice_was_token
                     and bargain_sacrifice_id in player.get("graveyard", [])):
                  self.move_card(
                      bargain_sacrifice_id, player, "graveyard", player,
                      "battlefield", cause="bargain_rollback")
             return False
        if source_zone == "exile" and hasattr(self, "cards_castable_from_exile"):
             self.cards_castable_from_exile.discard(card_id)
             getattr(self, "exile_alternative_costs", {}).pop(card_id, None)
        if source_zone == "exile":
             self._consume_plot_permission(player, card_id)
        if source_zone == "graveyard" and context.get(
                "graveyard_adventure_cast"):
             self._consume_graveyard_adventure_permission(player, card_id)
        if source_zone == "graveyard" and context.get("flashback_cast"):
             self.flashback_cards.add(card_id)
        if source_zone == "graveyard" and context.get("harmonize_cast"):
             self.flashback_cards.add(card_id)
        if context.get("prepared_copy"):
             # Casting the virtual copy from exile makes the source permanent
             # unprepared; the permanent itself never changes zones.
             self.prepared_cards.discard(card_id)

        # --- Prepare FINAL stack context ---
        final_stack_context = context.copy()
        # Runtime Card objects link back to GameState and thread locks. Every
        # rule-relevant cast characteristic is serialized elsewhere in context.
        final_stack_context.pop("card", None)
        final_stack_context["source_zone"] = source_zone
        # Preserve cast provenance through permanent resolution and its ETB
        # event.  Zone movement later rewrites ``source_zone`` to the trigger
        # source's current zone, so that field alone cannot answer intervening
        # conditions such as Sunderflock's "if you cast it".
        final_stack_context["was_cast"] = True
        final_stack_context["cast_controller_id"] = (
            "p1" if player is self.p1 else "p2")
        final_stack_context["final_paid_cost"] = final_cost_dict
        final_stack_context["final_paid_details"] = paid_mana_details
        final_stack_context["requires_target"] = requires_target
        final_stack_context["num_targets"] = num_targets
        final_stack_context["min_targets"] = min_targets
        final_stack_context["max_targets"] = num_targets
        casting_card = context.get("card")
        if not hasattr(casting_card, "card_types"):
            casting_card = card
        (cast_card_types, cast_card_subtypes,
         cast_card_has_flying) = \
            self.mana_system.spell_characteristics_for_cast(
                casting_card, context)
        final_stack_context["cast_card_types"] = sorted(cast_card_types)
        final_stack_context["cast_card_subtypes"] = sorted(
            cast_card_subtypes)
        final_stack_context["cast_card_has_flying"] = bool(
            cast_card_has_flying)
        if context.get("prepared_copy"):
             prepared_type_line = str(
                 context.get("prepared_face", {}).get("type_line", ""))
             final_stack_context["cast_card_types"] = [
                 card_type for card_type in ("instant", "sorcery")
                 if card_type in prepared_type_line.lower()
             ]
        if player.pop('next_spell_uncounterable', False):
            final_stack_context['cant_be_countered'] = True
        final_stack_context.pop('pay_offspring', None) # Clear intent flag
        final_stack_context.pop('kicker_cost_to_pay', None)
        final_stack_context.pop('additional_cost_info', None)
        final_stack_context.pop('source_idx', None)

        self.add_to_stack("SPELL", card_id, player, final_stack_context)
        if targets_committed:
             self.notify_targets_committed(
                 card_id, player, final_stack_context.get("targets", {}),
                 stack_context=final_stack_context)
        elif requires_target and num_targets > 0:
             raise RuntimeError(
                 f"Targeted spell {card.name} reached the stack without "
                 "committed targets")

        logging.info(f"Successfully cast spell: {card.name} ({card_id}) from {source_zone}")

        # --- Track Cast & Trigger ---
        # ...(tracking/trigger remains the same)...
        self.track_card_played(card_id, player_idx = 0 if player == self.p1 else 1)
        if not hasattr(self, 'spells_cast_this_turn'): self.spells_cast_this_turn = []
        self.spells_cast_this_turn.append((card_id, player, final_stack_context)) # Include context

        # Cast triggers receive event data, not ownership of the spell's live
        # stack context.  A shallow expansion aliases nested target payloads;
        # resolution validation of a nontargeted cast trigger (for example
        # Namor) could then clear the actual Spell Snare/Bounce Off targets.
        # Copy declarative values before adding the live player references.
        cast_trigger_context = self._copy_stack_context(final_stack_context)
        cast_trigger_context.update({
            'cast_card_id': card_id,
            'card_id': card_id,
            'controller': player,
            'casting_player': player,
        })
        self.trigger_ability(None, "CAST_SPELL", cast_trigger_context)
        cast_card_types = set(final_stack_context.get(
            "cast_card_types", getattr(card, 'card_types', [])))
        if 'creature' in cast_card_types:
             self.trigger_ability(None, "CAST_CREATURE_SPELL", cast_trigger_context)
        elif cast_card_types.intersection({'instant', 'sorcery'}):
             self.trigger_ability(None, "CAST_NONCREATURE_SPELL", cast_trigger_context)

        # Clear pending context if this cast matches it
        if getattr(self, 'pending_spell_context', None) and self.pending_spell_context.get('card_id') == card_id:
            self.pending_spell_context = None

        return True

    def _can_cast_now(self, card_id, player, context=None):
        """
        Check if a spell can be cast at the current time based on phase, stack state, etc.
        
        Args:
            card_id: ID of the card to check
            player: Player attempting to cast
            
        Returns:
            bool: Whether the spell can be cast
        """
        context = context or {}
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'card_types'):
            return False
        # Jennifer Walters / Sensational She-Hulk.  This is a turn rule, not
        # a continuous-effect layer: while the source controller is active,
        # their opponents cannot cast even if an effect grants permission to
        # cast during another spell's resolution.
        active_player = self._get_active_player()
        if not self.can_player_cast_spells(player):
            return False
        if context.get('cast_during_resolution'):
            return True

        # Timing follows the face/Adventure actually being cast. A front-face
        # type must not make an instant back face sorcery-only (or vice versa).
        type_line = getattr(card, 'type_line', '') or ''
        oracle_text = getattr(card, 'oracle_text', '') or ''
        use_printed_card_types = True
        if context.get('prepared_copy'):
            prepared_face = context.get('prepared_face', {})
            type_line = prepared_face.get('type_line', '') or type_line
            oracle_text = prepared_face.get('oracle_text', '') or ''
            use_printed_card_types = False
        elif context.get('cast_as_back_face') and hasattr(card, 'get_face_type_line'):
            type_line = card.get_face_type_line(1) or type_line
            oracle_text = card.get_face_text(1) or ''
            use_printed_card_types = False
        elif context.get('cast_as_adventure') and hasattr(card, 'get_adventure_data'):
            adventure = card.get_adventure_data() or {}
            type_line = adventure.get('type', '') or type_line
            oracle_text = adventure.get('effect', '') or ''
            use_printed_card_types = False

        type_line_lower = type_line.lower()
        is_instant = ('instant' in type_line_lower
                      or (use_printed_card_types
                          and 'instant' in getattr(card, 'card_types', [])))
        has_flash = 'flash' in oracle_text.lower()

        # Check if player has priority
        has_priority = (player == active_player and self.priority_pass_count == 0) or self.priority_player == player
        if not has_priority:
            return False

        # Instants and cards with flash can be cast whenever the player has
        # priority. Every other spell uses the exact canonical predicate used
        # by action-mask generation, including transient PHASE_PRIORITY over a
        # main phase.
        if is_instant or has_flash:
            return True
        return self._can_act_at_sorcery_speed(player)

    def _rules_text_source_is_active(self, card_id):
        """Whether a battlefield object's printed rules text is operative."""
        return not (
            getattr(self, "layer_system", None)
            and self.layer_system.source_has_lost_all_abilities(card_id))

    def can_player_cast_spells(self, player):
        """Return False under a live "opponents can't cast" turn rule."""
        active_player = self._get_active_player()
        if player is active_player or active_player is None:
            return True
        for permanent_id in active_player.get("battlefield", []):
            if not self._rules_text_source_is_active(permanent_id):
                continue
            permanent = self._safe_get_card(permanent_id)
            text = str(getattr(permanent, "oracle_text", "") or "").lower()
            if re.search(
                    r"\byour opponents can(?:'|\u2019)t cast spells during "
                    r"your turn\b", text):
                return False
        return True

    def land_play_limit(self, controller):
        """Current number of land plays allowed this turn."""
        additional = 0
        for permanent_id in controller.get("battlefield", []):
            if not self._rules_text_source_is_active(permanent_id):
                continue
            permanent = self._safe_get_card(permanent_id)
            text = str(getattr(permanent, "oracle_text", "") or "").lower()
            if re.search(
                    r"\byou may play an additional land on each of your turns\b",
                    text):
                additional += 1
        return 1 + additional

    def lands_played_this_turn(self, controller):
        """Return the canonical land-play count with old-state fallback."""
        if "lands_played_this_turn" in controller:
            return max(0, int(controller.get("lands_played_this_turn", 0) or 0))
        return 1 if controller.get("land_played", False) else 0

    def can_play_land_this_turn(self, controller):
        return self.lands_played_this_turn(controller) < self.land_play_limit(
            controller)

    def can_play_lands_from_graveyard(self, controller):
        """Whether a live controlled permanent grants this zone permission."""
        for permanent_id in controller.get("battlefield", []):
            if not self._rules_text_source_is_active(permanent_id):
                continue
            permanent = self._safe_get_card(permanent_id)
            text = str(getattr(permanent, "oracle_text", "") or "").lower()
            if re.search(r"\byou may play lands from your graveyard\b", text):
                return True
        return False

    def _record_land_play(self, controller):
        count = self.lands_played_this_turn(controller) + 1
        controller["lands_played_this_turn"] = count
        # Retain the legacy observation/planner flag. Rules legality uses the
        # count and current limit above.
        controller["land_played"] = count > 0

    def play_land(self, card_id, controller, play_back_face=False,
                  source_zone="hand", permission=None):
            """
            Play a land card from hand to battlefield, respecting the one-land-per-turn rule.
            Handles MDFC (Modal Double-Faced Card) lands.
            
            Args:
                card_id: ID of the land card to play
                controller: Player dictionary of the player playing the land
                play_back_face: Boolean, if True, play the back face of an MDFC
                
            Returns:
                bool: Whether the land was successfully played
            """
            source_cards = controller.get(source_zone, [])
            if card_id not in source_cards:
                logging.warning(f"Land {card_id} not found in {source_zone}")
                return False
            if source_zone == "graveyard":
                has_emblem_permission = (
                    permission == "graveyard_permanents"
                    and any(
                        emblem.get("kind") == "graveyard_permanents"
                        for emblem in controller.get("emblems", [])))
                has_permanent_permission = (
                    permission == "controlled_permanent"
                    and self.can_play_lands_from_graveyard(controller))
                if not (has_emblem_permission or has_permanent_permission):
                    logging.warning("No effect permits this graveyard land play.")
                    return False
            
            # Check if the card is actually a land (checking the correct face)
            card = self._safe_get_card(card_id)
            if not card:
                logging.warning(f"Card {card_id} invalid")
                return False

            is_land = False
            # If playing back face, check back face type line
            if play_back_face:
                if hasattr(card, 'back_face') and card.back_face and 'land' in card.back_face.get('type_line', '').lower():
                    is_land = True
                else:
                    logging.debug(f"Play land failed: Back face of {card.name} is not a land.")
            # If playing front face, check normal type line
            else:
                if hasattr(card, 'type_line') and 'land' in card.type_line.lower():
                    is_land = True

            if not is_land:
                logging.warning(f"Card {card.name} (Back: {play_back_face}) is not a land")
                return False
            
            # Check if player has already played a land this turn
            if not self.can_play_land_this_turn(controller):
                logging.warning(f"Player has already played a land this turn")
                return False
            
            # Use the same timing predicate as action-mask generation. The
            # engine represents post-resolution priority as PHASE_PRIORITY
            # while retaining the underlying main phase separately; checking
            # only the literal phase constants rejected mask-valid land plays.
            if not self._can_act_at_sorcery_speed(controller):
                logging.warning(
                    "Cannot play a land at sorcery speed during phase %s "
                    "(underlying phase %s)",
                    self.phase, self.previous_priority_phase)
                return False

            # Land play is a special action and still requires priority.
            if self.priority_player is not None and self.priority_player != controller:
                logging.warning(f"Player does not have priority to play a land")
                return False
            
            # Register back face status if applicable so the engine knows how to treat it on BF
            if play_back_face:
                if not hasattr(self, 'cast_as_back_face'):
                    self.cast_as_back_face = set()
                self.cast_as_back_face.add(card_id)

            # Prepare context for move_card
            move_context = {'play_back_face': play_back_face}

            # Move the land from its permitted source zone to the battlefield.
            result = self.move_card(
                card_id, controller, source_zone, controller, "battlefield",
                cause="land_play", context=move_context)
            
            if result:
                # Mark that player has played a land this turn
                self._record_land_play(controller)
                
                # Track the land play for statistics
                player_idx = 0 if controller == self.p1 else 1
                self.track_card_played(card_id, player_idx)
                
                # Determine properties for logging and tapped check based on the played face
                card_name = card.name
                oracle_text = getattr(card, 'oracle_text', '').lower()
                
                if play_back_face and hasattr(card, 'back_face'):
                    card_name = card.back_face.get('name', card_name)
                    oracle_text = card.back_face.get('oracle_text', '').lower()

                logging.debug(f"Played land {card_name}")
                
                # move_card applies this centrally; retain the face-aware
                # check here for logging and MDFC callers without resetting
                # any other tapped permanents in the player's dictionary.
                if self._enters_battlefield_tapped(card, controller, card_id, move_context):
                    controller.setdefault("tapped_permanents", set()).add(card_id)
                    logging.debug(f"Land {card_name} enters tapped")
            else:
                # If move failed, cleanup the back face registration
                if play_back_face and hasattr(self, 'cast_as_back_face') and card_id in self.cast_as_back_face:
                    self.cast_as_back_face.remove(card_id)
            
            return result

    def _validate_targets_on_resolution(self, source_id, controller, targets, context=None):
        """Checks if the targets selected for a spell/ability are still valid upon resolution."""
        if context is None: context = {} # Ensure context is dict

        # Independent instructions (including each chosen Spree mode) own
        # independent target slots.  An invalid target skips only its
        # instruction; the whole spell fizzles only when it originally had
        # targets and every one of them is now illegal (CR 608.2b).
        target_slots = (
            context.get('spree_target_slots')
            if context.get('is_spree') else
            context.get('instruction_target_slots')) or []
        slot_targets = context.get('targets_by_slot') or []
        if target_slots:
            if len(slot_targets) != len(target_slots):
                return False
            filtered_slots = []
            lifecycle_slots = []
            original_target_count = 0
            legal_target_count = 0
            for slot, selected_ids in zip(target_slots, slot_targets):
                selected_ids = list(selected_ids or [])
                original_target_count += len(selected_ids)
                valid_map = self.targeting_system.get_valid_targets(
                    source_id, controller,
                    slot.get('required_type', 'target'),
                    effect_text=slot.get('effect_text', ''))
                valid_ids = {
                    target_id
                    for ids in valid_map.values()
                    for target_id in ids
                }
                legal_ids = [
                    target_id for target_id in selected_ids
                    if target_id in valid_ids]
                filtered_slots.append(legal_ids)
                legal_target_count += len(legal_ids)
                lifecycle_slots.append({
                    "instruction_index": slot.get("instruction_index"),
                    "mode_index": slot.get("mode_index"),
                    "min_targets": int(slot.get("min_targets", 0)),
                    "original_target_count": len(selected_ids),
                    "legal_target_count": len(legal_ids),
                })

            categorized_targets = {}
            for slot, selected_ids in zip(target_slots, filtered_slots):
                for category, target_ids in self._categorize_targets_for_slot(
                        slot, selected_ids).items():
                    categorized_targets.setdefault(category, []).extend(
                        target_ids)
            context['targets_by_slot'] = filtered_slots
            context['targets'] = categorized_targets
            if isinstance(targets, dict):
                targets.clear()
                targets.update(categorized_targets)
            context['_target_resolution_lifecycle'] = {
                "validated": True,
                "original_target_count": original_target_count,
                "legal_target_count": legal_target_count,
                "slots": lifecycle_slots,
            }
            return original_target_count == 0 or legal_target_count > 0

        # Use TargetingSystem if available
        if hasattr(self, 'targeting_system') and self.targeting_system:
            card = self._safe_get_card(source_id)
            if not card: return False # Source disappeared?

            # --- Pass Effect Text and Context ---
            # Use specific effect text from context if available (e.g., chosen modal effect)
            # Otherwise, fallback to card's oracle text.
            effect_text = (context.get('targeting_text')
                           or context.get('effect_text',
                                          getattr(card, 'oracle_text', None)))

            # Validate using TargetingSystem
            if hasattr(self.targeting_system, 'validate_targets'):
                original_target_count = len(self._flatten_target_ids(targets))
                is_valid = self.targeting_system.validate_targets(source_id, targets, controller, effect_text=effect_text)
                legal_target_count = len(self._flatten_target_ids(targets))
                context['_target_resolution_lifecycle'] = {
                    "validated": True,
                    "original_target_count": original_target_count,
                    "legal_target_count": legal_target_count,
                    "slots": [],
                }
                if not is_valid:
                     logging.debug(f"Target validation failed for {getattr(card,'name',source_id)} using TargetingSystem.validate_targets.")
                return is_valid
            else:
                logging.warning("TargetingSystem missing 'validate_targets' method.")
                # Fallback? Re-evaluate get_valid_targets? Risky, assume true.
                return True
        else:
            logging.warning("Cannot validate targets: TargetingSystem not available.")
            return True # Assume valid if no system? Safer than failing spells.

    def _flatten_target_ids(self, targets):
        """Flatten a target dict/list into ordered target ids."""
        flattened = []
        seen = set()

        def add_target(target_id):
            if target_id is None or target_id == "X":
                return
            try:
                hash(target_id)
            except TypeError:
                return
            if target_id not in seen:
                seen.add(target_id)
                flattened.append(target_id)

        if isinstance(targets, dict):
            for key, value in targets.items():
                if key == "X":
                    continue
                if isinstance(value, (list, tuple, set)):
                    for target_id in value:
                        add_target(target_id)
                else:
                    add_target(value)
        elif isinstance(targets, (list, tuple, set)):
            for target_id in targets:
                add_target(target_id)
        return flattened

    def _pay_ward_costs_for_targets(self, item_type, source_id, controller, targets, context=None):
        """Resolve or stage policy choices for opposing Ward obligations.

        Returns True when every obligation was paid, False when declined or
        unpayable, and None when a choice was opened and resolution must pause.
        """
        if context is None:
            context = {}
        if context.get("ward_choice_complete"):
            return not context.get("countered_by_ward", False)
        target_ids = self._flatten_target_ids(targets)
        if not target_ids:
            return True

        if context.get("ward_checked_on_targeting"):
            obligations = list(context.get("ward_obligations", []))
        else:
            # Backward-compatible fallback for legacy/manually constructed
            # stack entries that predate target-commit snapshots.
            obligations = self._collect_ward_obligations(controller, targets)

        if not obligations:
            context["ward_choice_complete"] = True
            return True
        obligation = obligations[0]
        self.choice_context = {
            "type": "ward_payment", "player": controller,
            "controller": controller, "source_id": source_id,
            "stack_context": context, "stack_item_type": item_type,
            "copy_instance_id": context.get("copy_instance_id"),
            "obligations": obligations,
            "obligation_index": 0,
            "resume_phase": self.phase,
        }
        self._configure_ward_payment_choice(self.choice_context, obligation)
        self.phase = self.PHASE_CHOOSE
        self.priority_player = controller
        self.priority_pass_count = 0
        return None

    def _ward_stack_context(self, choice):
        """Find the live paused stack context after cloning or continuation."""
        source_id = choice.get("source_id")
        item_type = choice.get("stack_item_type")
        copy_instance_id = choice.get("copy_instance_id")
        for item in reversed(self.stack):
            if not (isinstance(item, tuple) and len(item) >= 4
                    and item[0] == item_type and item[1] == source_id
                    and isinstance(item[3], dict)):
                continue
            if item[3].get("copy_instance_id") == copy_instance_id:
                return item[3]
        fallback = choice.get("stack_context")
        return fallback if isinstance(fallback, dict) else None

    def _ward_payment_spec(self, player, ward_cost, source_id, target_id, context):
        """Return a clone-safe Ward payment kind and its concrete options."""
        text = str(ward_cost or "").strip()
        life = re.fullmatch(r"pay\s+(\d+)\s+life", text, re.IGNORECASE)
        if life:
            amount = int(life.group(1))
            return "life", ["pay"] if player.get("life", 0) >= amount else []
        sacrifice = re.fullmatch(
            r"sacrifice\s+(?:a|an)\s+([a-z -]+)", text, re.IGNORECASE)
        if sacrifice:
            criteria = sacrifice.group(1).strip().lower()
            options = [
                card_id for card_id in player.get("battlefield", [])
                if self._card_matches_ward_criteria(card_id, criteria)]
            return "sacrifice", options
        if re.fullmatch(r"discard\s+(?:a|one)\s+card", text, re.IGNORECASE):
            return "discard", list(player.get("hand", []))
        mana_text = f"{{{text}}}" if text.isdigit() else text
        if "{" in mana_text and self.mana_system:
            parsed = self.mana_system.parse_mana_cost(mana_text)
            ward_context = dict(context)
            ward_context.update({"card_id": source_id,
                                 "ward_target_id": target_id})
            payable = self.mana_system.can_pay_mana_cost(
                player, parsed, ward_context)
            return "mana", ["pay"] if payable else []
        return "unsupported", []

    def _card_matches_ward_criteria(self, card_id, criteria):
        card = self._safe_get_card(card_id)
        if not card:
            return False
        words = set(str(criteria).lower().replace("-", " ").split())
        card_types = {str(value).lower() for value in getattr(card, "card_types", [])}
        subtypes = {str(value).lower() for value in getattr(card, "subtypes", [])}
        if "nonland" in words and "land" in card_types:
            return False
        if "nontoken" in words and getattr(card, "is_token", False):
            return False
        if "token" in words and "nontoken" not in words and not getattr(card, "is_token", False):
            return False
        ignored = {"nonland", "nontoken", "token", "permanent"}
        required = words - ignored
        return not required or bool(required & (card_types | subtypes))

    def _configure_ward_payment_choice(self, choice, obligation):
        player = choice["player"]
        context = self._ward_stack_context(choice) or {}
        choice["stack_context"] = context
        kind, options = self._ward_payment_spec(
            player, obligation.get("cost"), choice.get("source_id"),
            obligation.get("target_id"), context)
        choice.update({
            "payment_kind": kind, "options": options,
            "target_id": obligation.get("target_id"),
            "ward_cost": obligation.get("cost"), "choice_page": 0,
        })

    def complete_ward_payment_choice(self, option_index=None, decline=False):
        """Commit one Ward payment/decline and advance stacked obligations."""
        choice = self.choice_context
        if not (choice and choice.get("type") == "ward_payment"):
            return False
        player = choice.get("player")
        stack_context = self._ward_stack_context(choice)
        choice["stack_context"] = stack_context
        if not player or not isinstance(stack_context, dict):
            return False
        if decline:
            stack_context["countered_by_ward"] = True
            stack_context["unpaid_ward_cost"] = choice.get("ward_cost")
            stack_context["ward_choice_complete"] = True
            return self._finish_ward_payment_choice(choice)

        options = choice.get("options", [])
        absolute = int(choice.get("choice_page", 0)) * 10 + int(option_index or 0)
        if not 0 <= absolute < len(options):
            return False
        kind = choice.get("payment_kind")
        paid = False
        if kind in {"mana", "life"}:
            paid = self._pay_single_ward_cost(
                player, choice.get("ward_cost"), choice.get("source_id"),
                choice.get("target_id"), stack_context)
        elif kind == "sacrifice":
            card_id = options[absolute]
            owner = self._find_card_owner_fallback(card_id) or player
            paid = self.move_card(
                card_id, player, "battlefield", owner, "graveyard",
                cause="ward_cost")
        elif kind == "discard":
            paid = self.discard_card(
                player, options[absolute], source_id=choice.get("source_id"),
                cause="ward_cost")
        if not paid:
            return False
        stack_context.setdefault("ward_costs_paid", []).append({
            "target_id": choice.get("target_id"),
            "cost": choice.get("ward_cost"),
        })
        next_index = int(choice.get("obligation_index", 0)) + 1
        obligations = choice.get("obligations", [])
        if next_index < len(obligations):
            choice["obligation_index"] = next_index
            self._configure_ward_payment_choice(choice, obligations[next_index])
            return True
        stack_context["ward_choice_complete"] = True
        return self._finish_ward_payment_choice(choice)

    def _finish_ward_payment_choice(self, choice):
        player = choice.get("player")
        self.choice_context = None
        self.phase = choice.get("resume_phase", self.PHASE_PRIORITY)
        self.priority_player = player
        self.priority_pass_count = 0
        return True

    def _pay_single_ward_cost(self, player, ward_cost, source_id, target_id, context):
        """Pay one ward cost. Supports mana costs and simple life-payment ward."""
        if ward_cost is None:
            return False
        cost_text = str(ward_cost).strip()
        if not cost_text or cost_text == "ward_generic":
            return False

        life_match = re.fullmatch(r"pay\s+(\d+)\s+life", cost_text, re.IGNORECASE)
        if life_match:
            amount = int(life_match.group(1))
            if player.get("life", 0) < amount:
                return False
            player["life"] -= amount
            return True

        if cost_text.isdigit():
            cost_text = f"{{{cost_text}}}"
        if "{" not in cost_text:
            logging.debug(f"Unsupported ward cost '{ward_cost}' on target {target_id}.")
            return False
        if not hasattr(self, 'mana_system') or not self.mana_system:
            return False

        parsed_cost = self.mana_system.parse_mana_cost(cost_text)
        ward_context = dict(context)
        ward_context.update({"card_id": source_id, "ward_target_id": target_id})
        if not self.mana_system.can_pay_mana_cost(player, parsed_cost, ward_context):
            return False
        return self.mana_system.pay_mana_cost_get_details(player, parsed_cost, ward_context) is not None

    def _determine_target_category(self, target_id):
        """Helper to determine the primary category ('creatures', 'players', etc.) for logging/categorization."""
        # This can reuse the logic from the Environment's helper if preferred,
        # or keep a local version for GameState internal use.
        if target_id in ["p1", "p2"]:
            return "players"

        # Repeated fixture/deck IDs can legitimately exist in a graveyard and
        # on the battlefield at the same time.  ``find_card_location`` honors
        # the latest physical-move hint, which is useful for zone movement but
        # must not recategorize a target selected from the battlefield as a
        # graveyard card while a spell is being committed.
        battlefield_controller = self.get_card_controller(target_id)
        if battlefield_controller is not None:
             card = self._safe_get_card(target_id)
             if card:
                  types = getattr(card, 'card_types', [])
                  type_line = getattr(card, 'type_line', '').lower()
                  if 'creature' in types: return 'creatures'
                  if 'planeswalker' in types: return 'planeswalkers'
                  if 'battle' in type_line: return 'battles'
                  if 'land' in types: return 'lands'
                  if 'artifact' in types: return 'artifacts'
                  if 'enchantment' in types: return 'enchantments'
             return 'permanents'

        owner, zone = self.find_card_location(target_id)
        if zone == 'player': return 'players'
        if zone == 'stack':
            for item in self.stack:
                if isinstance(item, tuple) and item[1] == target_id:
                    return 'spells' if item[0] == 'SPELL' else 'abilities'
            return 'stack_items' # Generic if not found matching ID
        if zone in ['graveyard', 'exile', 'library']: return 'cards'
        if zone == 'battlefield':
             card = self._safe_get_card(target_id)
             if card:
                  types = getattr(card, 'card_types', [])
                  type_line = getattr(card, 'type_line', '').lower()
                  if 'creature' in types: return 'creatures'
                  if 'planeswalker' in types: return 'planeswalkers'
                  if 'battle' in type_line: return 'battles'
                  if 'land' in types: return 'lands'
                  if 'artifact' in types: return 'artifacts'
                  if 'enchantment' in types: return 'enchantments'
                  return 'permanents' # Default permanent
        return 'other' # Fallback

    def resolve_top_of_stack(self):
        """Resolve the top item of the stack."""
        if not self.stack: return False

        # Targets are chosen while an object is being put on the stack, never
        # after both players pass.  This guard repairs legacy/interleaved trigger
        # batches whose pending choice was not opened immediately.  With no
        # legal mandatory target, start_pending_stack_target_choice removes the
        # object as required; otherwise it opens the policy choice and leaves
        # the stack untouched.
        if any(
                isinstance(item, tuple) and len(item) >= 4
                and isinstance(item[3], dict)
                and item[3].get("target_choice_pending")
                for item in self.stack):
            if self.start_pending_stack_target_choice():
                return True
            if not self.stack:
                return True

        top_item = self.stack.pop()
        expected_spell_occurrences = None
        if (isinstance(top_item, tuple) and len(top_item) >= 3
                and top_item[0] == "SPELL"):
            popped_context = top_item[3] if len(top_item) > 3 else {}
            if not popped_context.get("is_copy", False):
                # The stack object has already been popped, so add its one real
                # occurrence to the count still visible in physical zones/stack.
                expected_spell_occurrences = (
                    self._physical_occurrence_count(top_item[1]) + 1)
        resolution_success = False
        new_special_phase_entered = False
        resolved_item_had_split_second = False # Track if the resolved item had split second
        try:
            if isinstance(top_item, tuple) and len(top_item) >= 3:
                item_type, item_id, controller = top_item[:3]
                context = top_item[3] if len(top_item) > 3 else {}
                # Check context for split second
                if context.get('is_split_second', False):
                    resolved_item_had_split_second = True
                targets_on_stack_raw = context.get("targets")

                logging.debug(f"Resolving stack item: {item_type} {item_id} with raw targets: {targets_on_stack_raw}")
                card = self._safe_get_card(item_id)
                card_name = getattr(card, 'name', f"Item {item_id}") if card else f"Item {item_id}"

                # TARGET VALIDATION STEP
                validation_targets = {}
                if isinstance(targets_on_stack_raw, dict):
                    validation_targets = targets_on_stack_raw
                elif isinstance(targets_on_stack_raw, list): # Handle potential flat list from simple targeting
                    validation_targets = {"chosen": targets_on_stack_raw}
                # Else: If not list or dict, keep empty dict

                # --- Pass full context to validation ---
                targets_still_valid = self._validate_targets_on_resolution(item_id, controller, validation_targets, context)

                if not targets_still_valid:
                    if item_type == "SPELL" and context.get("cast_for_mutate", False):
                        # CR 702.140c: an illegal mutate target makes this resolve
                        # as an ordinary creature spell instead of being countered.
                        logging.info(
                            f"Mutate target for {card_name} became illegal; "
                            "resolving it as a creature spell.")
                        fallback_context = dict(context)
                        fallback_context["cast_for_mutate"] = False
                        fallback_context["requires_target"] = False
                        resolution_success = self._resolve_creature_spell(
                            item_id, controller, fallback_context)
                    elif item_type == "SPELL" and not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                        logging.info(f"Stack Item {item_type} {card_name} fizzled: All targets invalid.")
                        # Spell fizzles - move to GY unless it shouldn't move (e.g., rebound, flashback)
                        # Replacement effects can still apply here (e.g., exile instead of GY)
                        self.move_card(item_id, controller, "stack_implicit", controller, "graveyard", cause="spell_fizzle", context=context)
                        resolution_success = True # Fizzling counts as resolution finishing
                    else:
                        logging.info(f"Stack Item {item_type} {card_name} fizzled: All targets invalid.")
                        # If an ability fizzles, it simply leaves the stack.
                        resolution_success = True
                else:
                    ward_status = self._pay_ward_costs_for_targets(
                        item_type, item_id, controller, validation_targets,
                        context)
                    if ward_status is None:
                        # The top object was popped before validation. Restore
                        # it while its controller makes the Ward decision.
                        self.stack.append(top_item)
                        resolution_success = True
                        new_special_phase_entered = True
                    elif not ward_status:
                        logging.info(f"Stack Item {item_type} {card_name} countered by unpaid ward.")
                        if item_type == "SPELL" and not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                            self.move_card(item_id, controller, "stack_implicit", controller, "graveyard", cause="ward_countered", context=context)
                        resolution_success = True # Countering by ward successfully finishes this stack item
                    else:
                        # --- Proceed with resolution ---
                        if item_type == "SPELL": resolution_success = self._resolve_spell(item_id, controller, context)
                        elif item_type == "ABILITY" or item_type == "TRIGGER":
                            if self.ability_handler:
                                if targets_still_valid: context['targets'] = validation_targets
                                resolution_success = self.ability_handler.resolve_ability(item_type, item_id, controller, context)
                            else: resolution_success = False
                        else: logging.warning(f"Unknown stack item type: {item_type}"); resolution_success = False

                        if resolution_success and self.phase in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                            new_special_phase_entered = True
                            logging.debug(f"Resolution of {card_name} led to new special phase: {self._PHASE_NAMES.get(self.phase)}")
            else:
                 logging.warning(f"Invalid stack item format: {top_item}")
                 resolution_success = False
        except Exception as e:
            logging.error(f"Error resolving stack item: {str(e)}", exc_info=True)
            resolution_success = False
            # BUGFIX: a crash mid-resolution used to delete the card from the game
            # entirely (already off the stack, never reaching any zone). Best-effort
            # recovery: a real card spell goes to its controller's graveyard.
            try:
                if (isinstance(top_item, tuple) and len(top_item) >= 3
                        and top_item[0] == "SPELL" and isinstance(top_item[1], (int, str))):
                    _spell_id = top_item[1]
                    _owner, _zone = self.find_card_location(_spell_id)
                    if _zone is None and isinstance(top_item[2], dict):
                        top_item[2].setdefault("graveyard", []).append(_spell_id)
                        logging.warning(f"Recovered lost spell {_spell_id} to graveyard after resolution error.")
            except Exception:
                pass
        finally:
            # --- Post-Resolution Cleanup ---
            # A resolving spell can legitimately live outside every physical
            # zone while a policy choice (Dig, mutate position, etc.) is
            # pending.  Its continuation/finalizer owns that occurrence until
            # the choice completes, so recovering it here would duplicate or
            # prematurely graveyard the spell.
            if (expected_spell_occurrences is not None
                    and not new_special_phase_entered):
                spell_id = top_item[1]
                actual_occurrences = self._physical_occurrence_count(spell_id)
                if actual_occurrences < expected_spell_occurrences:
                    controller = top_item[2]
                    missing = expected_spell_occurrences - actual_occurrences
                    controller.setdefault("graveyard", []).extend(
                        [spell_id] * missing)
                    logging.error(
                        "Recovered %d lost physical occurrence(s) of spell %r "
                        "after stack resolution.", missing, spell_id)

            # Clear split second flag *after* resolution if it was the last one
            if resolved_item_had_split_second:
                continuation = (
                    self.choice_context.get('effect_continuation')
                    if self.choice_context else None)
                if continuation is not None:
                    continuation['release_split_second'] = True
                else:
                    any_other_ss_on_stack = any(
                        isinstance(i, tuple) and len(i) > 3
                        and i[3].get('is_split_second') for i in self.stack)
                    if not any_other_ss_on_stack:
                        self.split_second_active = False
                        logging.info("Split Second is now INACTIVE.")

            # PHASE_PRIORITY is a transient wrapper around the real turn
            # phase. Once the last stack item finishes (and resolution did not
            # open another choice), restore that underlying phase before
            # exposing sorcery-speed actions. Otherwise the action mask can
            # legitimately offer lands/spells that their handlers reject for
            # seeing PHASE_PRIORITY.
            if (not new_special_phase_entered and not self.stack
                    and self.phase == self.PHASE_PRIORITY
                    and self.previous_priority_phase is not None):
                self.phase = self.previous_priority_phase
                self.previous_priority_phase = None

            # --- Reset Priority ---
            # Only reset priority if a *new* special phase wasn't entered AND
            # if the stack is now empty or the active player should get priority back.
            if not new_special_phase_entered:
                self.priority_player = self._get_active_player() # AP gets priority after resolution
                self.priority_pass_count = 0
                logging.debug(f"Finished resolving stack item. Priority to AP ({self.priority_player['name']})")
            else:
                # If a special phase was entered, priority logic is handled by that phase setup.
                logging.debug(f"Resolution led to special phase, priority already set.")

            # --- Update stack size tracking ---
            self.last_stack_size = len(self.stack)

        return resolution_success

    def _resolve_ability(self, ability_id, controller, context=None):
        """
        Resolve an activated ability.
        
        Args:
            ability_id: The ID of the card with the ability
            controller: The player activating the ability
            context: Additional ability context
        """
        if context is None:
            context = {}
                
        # Check if we have pre-created effects in the context (from modal abilities, etc.)
        if "effects" in context and context["effects"]:
            effects = context["effects"]
            targets = context.get("targets")
            
            # Apply each effect
            for effect in effects:
                effect.apply(self, ability_id, controller, targets)
            return
        
        # If we have an ability handler, use it
        if hasattr(self, 'ability_handler'):
            ability_index = context.get("ability_index", 0)
            
            # Get the activated ability
            activated_abilities = self.ability_handler.get_activated_abilities(ability_id)
            if 0 <= ability_index < len(activated_abilities):
                ability = activated_abilities[ability_index]
                
                # Handle targeting if needed
                targets = context.get("targets")
                if not targets and hasattr(self.ability_handler, 'targeting_system'):
                    targets = self.ability_handler.targeting_system.resolve_targeting_for_ability(
                        ability_id, ability.effect_text, controller)
                        
                # Resolve the ability
                ability.resolve_with_targets(self, controller, targets)
                return
        
        # Fallback for when we have ability_text but no pre-created effects
        if "ability_text" in context:
            ability_text = context["ability_text"]
            targets = context.get("targets")
            
            # Create effects from the text
            if hasattr(self, 'ability_handler') and hasattr(self.ability_handler, '_create_ability_effects'):
                effects = self.ability_handler._create_ability_effects(ability_text, targets)
                for effect in effects:
                    effect.apply(self, ability_id, controller, targets)
                return
        
        logging.warning(f"Could not resolve ability for card {ability_id}")

    def _resolve_triggered_ability(self, trigger_id, controller, context=None):
        """
        Resolve a triggered ability.
        
        Args:
            trigger_id: The ID of the card with the triggered ability
            controller: The player controlling the ability
            context: Additional trigger context
        """
        if context is None:
            context = {}
            
        # If we have an ability handler, use it
        if hasattr(self, 'ability_handler'):
            # Find the triggered ability based on the context
            trigger_event = context.get("trigger_event")
            
            # Check each ability on the card
            card_abilities = self.ability_handler.registered_abilities.get(trigger_id, [])
            for ability in card_abilities:
                if isinstance(ability, TriggeredAbility) and ability.can_trigger(trigger_event, context):
                    # Handle targeting if needed
                    targets = context.get("targets")
                    if not targets and hasattr(self.ability_handler, 'targeting_system'):
                        targets = self.ability_handler.targeting_system.resolve_targeting_for_ability(
                            trigger_id, ability.effect_text, controller)
                        
                    # Resolve the triggered ability
                    ability.resolve_with_targets(self, controller, targets)
                    return
        self.check_state_based_actions()    
        logging.warning(f"Could not resolve triggered ability for card {trigger_id}")

    def _resolve_spell(self, spell_id, controller, context=None):
        """Resolve a spell with handling for modal spells based on context."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        if not spell:
             logging.warning(f"Cannot resolve spell: card {spell_id} not found")
             # Don't move to graveyard if it didn't exist
             return False

        spell_name = getattr(spell, "name", f"Spell {spell_id}")
        logging.debug(f"Resolving spell: {spell_name}")

        # Check if countered (e.g., by a replacement effect during resolution?) - less common
        if context.get("countered"):
             logging.debug(f"Spell {spell_name} was countered - moving to graveyard")
             if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                  self.move_card(spell_id, controller, "stack_implicit", controller, "graveyard")
             return False # Resolution stopped

        # Determine spell type and base characteristics post-layers (layers shouldn't affect stack usually)
        card_types = getattr(spell, 'card_types', [])

        if context.get("cast_for_mutate", False):
            targets = self._flatten_target_ids(context.get("targets", {}))
            target_id = targets[0] if targets else None
            if target_id is not None and self._is_valid_mutate_target(controller, target_id):
                return self.begin_mutate_position_choice(controller, spell_id, target_id)
            fallback_context = dict(context)
            fallback_context["cast_for_mutate"] = False
            fallback_context["requires_target"] = False
            return self._resolve_creature_spell(spell_id, controller, fallback_context)

        if ('selected_spree_modes' in context
                or context.get('is_spree', False)):
            return self._resolve_spree_modes(
                spell_id, controller, context)

        # --- MODAL SPELL RESOLUTION ---
        selected_modes_indices = context.get("selected_modes") # Get list of chosen indices
        if selected_modes_indices is not None: # Check specifically for None, empty list is valid (for "up to" maybe)
            logging.debug(f"Resolving modal spell {spell_name} with chosen modes: {selected_modes_indices}")
            all_modes_text, _, _ = self.ability_handler._parse_modal_text(getattr(spell, 'oracle_text', ''))

            if not all_modes_text:
                 logging.error(f"Failed to re-parse modes for resolving modal spell {spell_name}")
                 # Move to GY if non-permanent?
                 return False

            modal_effects = []
            mode_targets = self._effect_targets_from_context(context)
            for mode_idx in selected_modes_indices:
                if 0 <= mode_idx < len(all_modes_text):
                     mode_text = all_modes_text[mode_idx]
                     logging.debug(f"Applying mode {mode_idx}: '{mode_text}'")
                     # Create and apply effects for THIS mode's text
                     # Pass targets that were selected *for the whole spell* if available
                     # If modes have separate targets, targeting phase needs modification. Assume shared targets for now.
                     effects = EffectFactory.create_effects(mode_text, mode_targets, source_name=getattr(spell, 'name', None))
                     modal_effects.extend(effects)
                else:
                     logging.warning(f"Invalid mode index {mode_idx} found in context for {spell_name}")

            finalizer = {
                'kind': 'modal_spell', 'source_id': spell_id,
                'controller_id': self._effect_controller_id(controller),
                'context': self._copy_stack_context(context),
            }
            success, pending = self._run_effect_sequence(
                modal_effects, spell_id, controller, mode_targets,
                context=context, finalizer=finalizer,
                initial_success=bool(modal_effects))
            return True if pending else success

        # --- NON-MODAL SPELL RESOLUTION ---
        else:
            # Handle different card types (calls helpers which use move_card)
            if context.get('prepared_copy'):
                 success = self._resolve_instant_sorcery_spell(
                     spell_id, controller, context)
            elif context.get('cast_as_adventure'):
                 success = self._resolve_instant_sorcery_spell(
                     spell_id, controller, context)
            elif 'creature' in card_types:
                 success = self._resolve_creature_spell(spell_id, controller, context)
            elif 'planeswalker' in card_types:
                 success = self._resolve_planeswalker_spell(spell_id, controller, context)
            elif any(t in card_types for t in ['artifact', 'enchantment', 'battle']):
                 success = self._resolve_permanent_spell(spell_id, controller, context)
            elif 'land' in card_types:
                 success = self._resolve_land_spell(spell_id, controller, context)
            elif any(t in card_types for t in ['instant', 'sorcery']):
                 success = self._resolve_instant_sorcery_spell(spell_id, controller, context)
            else:
                 logging.warning(f"Unknown card type for resolution: {card_types} on {spell_name}")
                 if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                     self.move_card(spell_id, controller, "stack_implicit", controller, "graveyard")
                 success = False # Unknown type failed resolution

            # Post-resolution SBAs are handled by the main loop
            return success

    def _resolve_spree_modes(self, spell_id, controller, context):
        """Resolve chosen Spree modes in printed order with bound targets."""
        spell = self._safe_get_card(spell_id)
        modes = list(getattr(spell, 'spree_modes', []) or []) if spell else []
        selected_modes = sorted(set(
            context.get('selected_spree_modes', [])))
        if (not modes or not selected_modes
                or any(not isinstance(index, int)
                       or not 0 <= index < len(modes)
                       for index in selected_modes)):
            logging.error(
                f"Cannot resolve Spree spell {getattr(spell, 'name', spell_id)}: "
                "invalid mode announcement.")
            return False

        targets_by_mode = {}
        slots = list(context.get('spree_target_slots', []) or [])
        targets_by_slot = list(context.get('targets_by_slot', []) or [])
        for slot_index, slot in enumerate(slots):
            mode_index = slot.get('mode_index')
            selected_ids = (
                list(targets_by_slot[slot_index] or [])
                if slot_index < len(targets_by_slot) else [])
            targets_by_mode[mode_index] = self._categorize_targets_for_slot(
                slot, selected_ids)

        effects = []
        parsed_all_modes = True
        for mode_index in selected_modes:
            mode_text = str(modes[mode_index].get('effect', '') or '')
            mode_targets = targets_by_mode.get(mode_index, {})
            # Resolution-time validation has already removed illegal targets.
            # A targeted mode with none left does nothing, while targetless
            # chosen modes still resolve.
            if ('target' in mode_text.lower()
                    and not self._flatten_target_ids(mode_targets)):
                continue
            mode_effects = EffectFactory.create_effects(
                mode_text, mode_targets,
                source_name=getattr(spell, 'name', None))
            if not mode_effects:
                parsed_all_modes = False
                logging.warning(
                    f"No effects parsed for Spree mode {mode_index} of "
                    f"{getattr(spell, 'name', spell_id)}.")
                continue
            for effect in mode_effects:
                effect._bound_targets = copy_module.deepcopy(mode_targets)
                effect._spree_mode_index = mode_index
                effects.append(effect)

        finalizer = {
            'kind': 'modal_spell', 'source_id': spell_id,
            'controller_id': self._effect_controller_id(controller),
            'context': self._copy_stack_context(context),
        }
        success, pending = self._run_effect_sequence(
            effects, spell_id, controller, {}, context=context,
            finalizer=finalizer, initial_success=parsed_all_modes)
        return True if pending else success

    def _ordinary_instruction_effects(self, spell, resolving_text, context):
        """Parse ordinary instructions and bind each one to its own targets."""
        slots = list(context.get('instruction_target_slots', []) or [])
        targets_by_slot = list(context.get('targets_by_slot', []) or [])
        slot_by_instruction = {
            int(slot.get('instruction_index')): (slot_index, slot)
            for slot_index, slot in enumerate(slots)
            if slot.get('instruction_index') is not None
        }
        effects = []
        parsed_all = True
        for instruction_index, instruction in enumerate(
                self._ordinary_instruction_segments(resolving_text)):
            if self._is_nonresolving_spell_instruction(instruction):
                continue
            slot_entry = slot_by_instruction.get(instruction_index)
            instruction_targets = {}
            if slot_entry:
                slot_index, slot = slot_entry
                selected_ids = (
                    list(targets_by_slot[slot_index] or [])
                    if slot_index < len(targets_by_slot) else [])
                instruction_targets = self._categorize_targets_for_slot(
                    slot, selected_ids)
                # An instruction whose mandatory targets all became illegal
                # does nothing.  An optional zero-target instruction still
                # resolves (TapEffect, for example, treats that as success).
                if (int(slot.get('min_targets', 0)) > 0
                        and not selected_ids):
                    continue
            instruction_effects = EffectFactory.create_effects(
                instruction, instruction_targets,
                source_name=getattr(spell, 'name', None))
            if not instruction_effects:
                parsed_all = False
                logging.warning(
                    "No effects parsed for instruction %s of %s.",
                    instruction_index, getattr(spell, 'name', 'spell'))
                continue
            for effect in instruction_effects:
                effect._bound_targets = copy_module.deepcopy(
                    instruction_targets)
                effect._instruction_index = instruction_index
                effects.append(effect)
        return effects, parsed_all

    def _finish_modal_spell_resolution(self, spell_id, controller, context,
                                       effects_succeeded=True):
        """Move and announce a modal spell after all choices finish."""
        spell = self._safe_get_card(spell_id)
        if not spell:
            return False
        card_types = getattr(spell, 'card_types', [])
        if not any(t in card_types for t in [
                'creature', 'artifact', 'enchantment', 'planeswalker',
                'land', 'battle']):
            if (not context.get("is_copy", False)
                    and not context.get("skip_default_movement", False)):
                self.move_card(
                    spell_id, controller, "stack_implicit", controller,
                    "graveyard", cause="spell_resolution", context=context)
        self.trigger_ability(
            spell_id, "SPELL_RESOLVED", {"controller": controller, **context})
        return bool(effects_succeeded)

    def _resolve_modal_spell(self, spell_id, controller, mode, context=None):
        """
        Resolve a modal spell based on the chosen mode.
        
        Args:
            spell_id: The ID of the modal spell
            controller: The player casting the spell
            mode: The chosen mode index
            context: Additional spell context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            return
            
        # Handle through ability handler if available
        if hasattr(self, 'ability_handler') and hasattr(self.ability_handler, 'handle_modal_ability'):
            success = self.ability_handler.handle_modal_ability(spell_id, controller, mode)
            if success:
                # If this is not a copy, move to graveyard (unless it's a permanent)
                if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                    if not (hasattr(spell, 'card_types') and 
                            any(t in spell.card_types for t in ['creature', 'artifact', 'enchantment', 'planeswalker', 'land'])):
                        controller["graveyard"].append(spell_id)
                return
                
        # Fallback - parse modes from oracle text
        if hasattr(spell, 'oracle_text'):
            modes = self._parse_modes_from_text(spell.oracle_text)
            if modes and 0 <= mode < len(modes):
                mode_text = modes[mode]
                
                # Create context with targets for this specific mode
                mode_context = dict(context)
                mode_context["mode_text"] = mode_text
                
                # Resolve as if it were a regular spell with this mode's effect
                if 'instant' in spell.card_types or 'sorcery' in spell.card_types:
                    # Resolve mode effects
                    targets = context.get("targets")
                    if not targets and hasattr(self, 'targeting_system'):
                        targets = self.targeting_system.resolve_targeting_for_spell(spell_id, controller, mode_text)
                        
                    self._resolve_mode_effects(spell_id, controller, mode_text, targets, mode_context)
                    
                    # Move to graveyard if not a copy
                    if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                        controller["graveyard"].append(spell_id)
                else:
                    # For permanent modal spells, handle differently based on the mode
                    # This is more complex and depends on the specific card
                    logging.warning(f"Modal permanent spell {spell.name} resolution not fully implemented")
                    
                    # Default handling for permanents
                    if not context.get("is_copy", False):
                        controller["battlefield"].append(spell_id)
                        self.trigger_ability(spell_id, "ENTERS_BATTLEFIELD", {"controller": controller})
            else:
                logging.warning(f"Invalid mode {mode} for spell {spell.name}")
                # Move to graveyard if not a permanent and not a copy
                if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                    if not (hasattr(spell, 'card_types') and 
                            any(t in spell.card_types for t in ['creature', 'artifact', 'enchantment', 'planeswalker', 'land'])):
                        controller["graveyard"].append(spell_id)
        else:
            logging.warning(f"Modal spell {spell_id} has no oracle_text attribute")
            # Move to graveyard if not a copy
            if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                controller["graveyard"].append(spell_id)

    def _parse_modes_from_text(self, text):
        """Parse modes from card text for modal spells."""
        modes = []
        
        # Check for common modal text patterns
        if "choose one —" in text.lower():
            # Split after the "Choose one —" text
            parts = text.split("Choose one —", 1)[1]
            
            # Split by bullet points or similar indicators
            import re
            mode_parts = re.split(r'[•●]', parts)
            
            # Clean and add each mode
            for part in mode_parts:
                cleaned = part.strip()
                if cleaned:
                    modes.append(cleaned)
        
        # Also handle "Choose one or both —" pattern
        elif "choose one or both —" in text.lower():
            parts = text.split("Choose one or both —", 1)[1]
            import re
            mode_parts = re.split(r'[•●]', parts)
            
            for part in mode_parts:
                cleaned = part.strip()
                if cleaned:
                    modes.append(cleaned)
        
        return modes

    def _resolve_creature_spell(self, spell_id, controller, context=None):
        """Resolve a creature spell - put it onto the battlefield using move_card."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        if not spell or ('creature' not in getattr(spell, 'card_types', [])):
             # Spell might have lost creature type? Or invalid ID?
             logging.warning(f"Attempted to resolve {spell_id} as creature, but it's not.")
             # Move to GY if not a copy
             if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
             return False

        # Use move_card to handle ETB, replacements, static effects
        if context.get("is_copy", False):
            # Create a token copy on the battlefield
            token_id = self.create_token_copy(spell, controller)
            logging.debug(f"Resolved copy of Creature spell {spell.name} as token {token_id}.")
            return token_id is not None
        else:
            oracle_text = getattr(spell, "oracle_text", "")
            is_superior_spider = (
                getattr(spell, "name", "").casefold()
                == "superior spider-man"
                and "copy of any creature card in a graveyard"
                in oracle_text.casefold()
                and not context.get(
                    "superior_spider_copy_choice_complete"))
            if is_superior_spider:
                options = self._superior_spider_copy_options()
                if options:
                    resume_phase = self._normalized_choice_resume_phase(
                        self.previous_priority_phase)
                    self.phase = self.PHASE_CHOOSE
                    self.choice_context = {
                        "type": "resolution_choice",
                        "choice_kind": "superior_spider_copy",
                        "player": controller,
                        "controller": controller,
                        "card_id": spell_id,
                        "source_id": spell_id,
                        "options": options,
                        "choice_page": 0,
                        "resolution_context": dict(context),
                        "optional": True,
                        "resume_phase": resume_phase,
                    }
                    self.priority_player = controller
                    self.priority_pass_count = 0
                    return True
            is_mockingbird = (
                getattr(spell, "name", "").lower() == "mockingbird"
                and "amount of mana spent to cast this creature" in oracle_text.lower()
                and not context.get("mockingbird_copy_choice_complete"))
            if is_mockingbird:
                mana_spent = self._mana_spent_on_cast(context)
                options = self._mockingbird_copy_options(mana_spent)
                if options:
                    if self.phase not in [self.PHASE_TARGETING,
                                          self.PHASE_SACRIFICE,
                                          self.PHASE_CHOOSE]:
                        self.previous_priority_phase = self.phase
                    self.phase = self.PHASE_CHOOSE
                    self.choice_context = {
                        "type": "mockingbird_copy",
                        "player": controller,
                        "controller": controller,
                        "card_id": spell_id,
                        "source_id": spell_id,
                        "options": options,
                        "mana_spent": mana_spent,
                        "resolution_context": dict(context),
                        "optional": True,
                    }
                    self.priority_player = controller
                    self.priority_pass_count = 0
                    return True
            # Move the actual card to the battlefield
            success = self.move_card(spell_id, controller, "stack_implicit", controller, "battlefield", cause="spell_resolution", context=context)
            if success:
                 logging.debug(f"Resolved Creature spell {spell.name}")
                 if context.get("warp_cast"):
                     self.register_delayed_trigger(
                         phase=self.PHASE_END_STEP,
                         description=f"Warp exile {spell.name}",
                         payload={
                             "kind": "warp_exile", "card_id": spell_id,
                             "controller_id": self._effect_controller_id(controller),
                         })
            else: # Move failed
                 controller["graveyard"].append(spell_id)
            return success

    def _resolve_planeswalker_spell(self, spell_id, controller, context=None):
        """Resolve a planeswalker spell - put it onto the battlefield using move_card."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        # Ensure it's still a planeswalker upon resolution
        if not spell or ('planeswalker' not in getattr(spell, 'card_types', [])):
            logging.warning(f"Attempted to resolve {spell_id} as planeswalker, but it's not.")
            if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
            return False

        if context.get("is_copy", False):
            token_id = self.create_token_copy(spell, controller)
            logging.debug(f"Resolved copy of Planeswalker spell {spell.name} as token {token_id}.")
            return token_id is not None
        else:
            success = self.move_card(spell_id, controller, "stack_implicit", controller, "battlefield", cause="spell_resolution", context=context)
            if success:
                logging.debug(f"Resolved Planeswalker spell {spell.name}")
                # Uniqueness rule checked via SBAs
            else: # Move failed
                controller["graveyard"].append(spell_id)
            return success

    def _resolve_permanent_spell(self, spell_id, controller, context=None):
        """Resolve other permanent spells (Artifact, Enchantment, Battle) using move_card."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        valid_types = ['artifact', 'enchantment', 'battle']
        # Check if it's one of the expected permanent types
        if not spell or not any(t in getattr(spell, 'card_types', []) or t in getattr(spell, 'type_line', '').lower() for t in valid_types):
            logging.warning(f"Attempted to resolve {spell_id} as permanent, but type is invalid.")
            if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
            return False

        if context.get("is_copy", False):
            token_id = self.create_token_copy(spell, controller)
            logging.debug(f"Resolved copy of Permanent spell {spell.name} as token {token_id}.")
            return token_id is not None
        else:
            # Handle Aura attachment targeting specifically during resolution if needed
            if 'aura' in getattr(spell, 'subtypes', []):
                 # Target commitment categorizes the chosen object by its live
                 # type (usually ``creatures``), so preserve that announced
                 # Aura target by flattening the categorized target map.
                 chosen_targets = self._flatten_target_ids(
                     context.get('targets', {}))
                 if not chosen_targets:
                      logging.warning(f"Aura {spell.name} resolving without target, fizzling to graveyard.")
                      controller["graveyard"].append(spell_id)
                      return False
                 target_id = chosen_targets[0] # Assume first chosen target
                 # Check if target is still valid *now*
                 target_card = self._safe_get_card(target_id)
                 target_owner, target_zone = self.find_card_location(target_id)
                 if not target_card or target_zone != 'battlefield': # Add legality check later
                      logging.warning(f"Target {target_id} for Aura {spell.name} no longer valid. Fizzling.")
                      controller["graveyard"].append(spell_id)
                      return False
                 # Store attachment intention for move_card/ETB handling
                 context['attach_to_target'] = target_id

            # Use move_card for ETB, replacements, etc.
            success = self.move_card(spell_id, controller, "stack_implicit", controller, "battlefield", cause="spell_resolution", context=context)
            if success:
                logging.debug(f"Resolved Permanent spell {spell.name}")
                # If it was an Aura, move_card's ETB handling should call _resolve_aura_attachment
                # if context included 'attach_to_target'
            else: # Move failed
                controller["graveyard"].append(spell_id)
            return success

    def _resolve_aura_attachment(self, aura_id, controller, context):
        """Handles attaching an aura when it resolves or enters the battlefield."""
        aura_card = self._safe_get_card(aura_id)
        if not aura_card: return

        target_id = context.get('attach_to_target') # Get target decided during casting/ETB
        if target_id:
             # Verify target still valid
             target_card = self._safe_get_card(target_id)
             target_owner, target_zone = self.find_card_location(target_id)
             if target_card and target_zone == 'battlefield': # Add legality check
                 if hasattr(self, 'attach_aura') and self.attach_aura(controller, aura_id, target_id):
                     logging.debug(f"Aura {aura_card.name} resolved and attached to {target_card.name}")
                     return
             # Target invalid or attachment failed
             logging.warning(f"Target {target_id} for Aura {aura_card.name} invalid on resolution or attachment failed.")
             # Aura goes to graveyard if target invalid upon resolution (handled by SBA usually)
             # Move directly here for clarity
             if aura_id in controller["battlefield"]:
                  self.move_card(aura_id, controller, "battlefield", controller, "graveyard", cause="aura_fizzle")
        else:
             logging.warning(f"Aura {aura_card.name} resolving without a target specified in context.")
             # Goes to graveyard if it needed a target but didn't have one
             if aura_id in controller["battlefield"]:
                 self.move_card(aura_id, controller, "battlefield", controller, "graveyard", cause="aura_fizzle")

    def _resolve_land_spell(self, spell_id, controller, context=None):
        """Resolve a land spell (e.g., from effects like Dryad Arbor). Uses move_card."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        if not spell or ('land' not in getattr(spell, 'card_types', []) and 'land' not in getattr(spell,'type_line','').lower()):
             logging.warning(f"Attempted to resolve {spell_id} as land spell, but type is invalid.")
             if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
             return False

        # Lands resolving as spells don't count towards land drop normally
        # Use move_card to handle ETB
        success = self.move_card(spell_id, controller, "stack_implicit", controller, "battlefield", cause="spell_resolution", context=context)
        if success:
             logging.debug(f"Resolved Land spell {spell.name}")
        else: # Move failed
             controller["graveyard"].append(spell_id)
        return success

    def _resolve_instant_sorcery_spell(self, spell_id, controller, context=None):
        """Resolve instant/sorcery. Applies effects then moves to appropriate zone."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        valid_types = ['instant', 'sorcery']
        # When cast as an Adventure, the card is a creature whose Adventure
        # half is an instant/sorcery -- honor the flag rather than rejecting
        # it on the creature type (July 2026 sweep).
        if not spell or not (any(t in getattr(spell, 'card_types', []) for t in valid_types)
                             or context.get('cast_as_adventure')
                             or context.get('prepared_copy')):
             logging.warning(f"Attempted to resolve {spell_id} as instant/sorcery, but type is invalid.")
             if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
             return False

        prepared_face = context.get('prepared_face', {})
        spell_name = (prepared_face.get('name')
                      if context.get('prepared_copy') else None) \
            or getattr(spell, 'name', f"Spell {spell_id}")
        logging.debug(f"Resolving Instant/Sorcery: {spell_name}")

        # Apply effects using AbilityHandler or EffectFactory
        effects = []
        effect_targets = self._effect_targets_from_context(context)
        if hasattr(self, 'ability_handler'):
            resolving_text = context.get('effect_text')
            if resolving_text is None and context.get('cast_as_adventure'):
                resolving_text = (spell.get_adventure_data() or {}).get(
                    'effect', '')
            if resolving_text is None:
                resolving_text = getattr(spell, 'oracle_text', '')
            if context.get('instruction_target_slots'):
                effects, parsed_all_instructions = \
                    self._ordinary_instruction_effects(
                        spell, resolving_text, context)
            else:
                parsed_all_instructions = True
                effects = EffectFactory.create_effects(
                    resolving_text, effect_targets,
                    source_name=spell_name)
        else:
            logging.warning("No ability handler found to resolve instant/sorcery effects.")
            parsed_all_instructions = False

        finalizer = {
            'kind': 'instant_sorcery', 'source_id': spell_id,
            'controller_id': self._effect_controller_id(controller),
            'context': self._copy_stack_context(context),
        }
        success, pending = self._run_effect_sequence(
            effects, spell_id, controller, effect_targets,
            context=context, finalizer=finalizer,
            initial_success=parsed_all_instructions)
        return True if pending else success

    def _finish_instant_sorcery_resolution(self, spell_id, controller, context=None):
        """Move and announce an instant/sorcery after all choices finish."""
        if context is None:
            context = {}
        spell = self._safe_get_card(spell_id)
        if not spell:
            logging.warning(
                f"Cannot finish instant/sorcery resolution for missing card {spell_id}.")
            return False
        spell_name = getattr(spell, 'name', f"Spell {spell_id}")

        # Determine final destination zone based on context (Flashback, Rebound etc.)
        final_zone = "graveyard"
        was_cast_from_hand = context.get('source_zone') == 'hand' # Need source zone info
        has_rebound = "rebound" in getattr(spell,'oracle_text','').lower()

        if (context.get("flashback_cast")
                or (context.get('source_zone') == 'graveyard'
                    and context.get('use_alt_cost') == 'flashback')):
            final_zone = "exile"
            self.flashback_cards.discard(spell_id)
        elif (context.get("harmonize_cast")
                or (context.get('source_zone') == 'graveyard'
                    and context.get('use_alt_cost') == 'harmonize')):
            final_zone = "exile"
            self.flashback_cards.discard(spell_id)
        # --- Adventure (CR 715.3f, July 2026 sweep) ---
        # A spell cast as its Adventure half goes to EXILE, and the owner may
        # later cast the creature from exile. cast_as_adventure was set at
        # cast time but nothing read it here, so adventure spells went to the
        # graveyard and the creature half was lost forever.
        elif context.get('cast_as_adventure'):
            final_zone = "exile"
            if not hasattr(self, 'cards_castable_from_exile'):
                self.cards_castable_from_exile = set()
            self.cards_castable_from_exile.add(spell_id)
            logging.debug(f"{spell_name} exiled on Adventure; creature side castable from exile.")
        # --- MODIFIED: Rebound Logic ---
        elif has_rebound and was_cast_from_hand:
            final_zone = "exile"
            if not hasattr(self, 'rebounded_cards'): self.rebounded_cards = {}
            self.rebounded_cards[spell_id] = {'owner': controller, 'turn_exiled': self.turn} # Track owner and turn
            logging.debug(f"{spell_name} exiled via Rebound.")
        # --- END MODIFIED ---

        # Handle copies (they cease to exist)
        if context.get("is_copy", False):
            logging.debug(f"Copy of {spell_name} resolved and ceased to exist.")
        elif context.get("skip_default_movement", False):
             logging.debug(f"Default movement skipped for {spell_name} (e.g., Buyback, Commander tax zone).")
        elif self.find_card_location(spell_id)[1] == 'battlefield':
             logging.debug(f"{spell_name} moved itself to the battlefield while resolving.")
        elif final_zone != "battlefield": # Ensure permanents aren't moved here
             # Use move_card to handle triggers etc.
             self.move_card(spell_id, controller, "stack_implicit", controller, final_zone, cause="spell_resolution", context=context)

        self.trigger_ability(spell_id, "SPELL_RESOLVED", {"controller": controller})
        return True

    def _word_to_number(self, word):
        """Convert word representation of number to int."""
        from .ability_utils import text_to_number
        return text_to_number(word)

    def _get_madness_cost_str_gs(self, card):
        """Helper to extract madness cost string from GameState context."""
        if card and hasattr(card, 'oracle_text'):
            match = re.search(r"madness\s+(\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
            if match:
                cost_str = match.group(1)
                if cost_str.isdigit(): return f"{{{cost_str}}}"
                return cost_str
        return None

    def resolve_spell_effects(self, spell_id, controller, targets=None, context=None):
        """
        Apply the effects of a spell using AbilityEffect objects.
        
        Args:
            spell_id: The ID of the spell to resolve
            controller: The player casting the spell
            targets: Dictionary of targets for the spell
            context: Additional spell context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            logging.warning(f"Cannot resolve spell effects: card {spell_id} not found")
            return
        
        # If ability_handler is available, use it to create effect objects
        if hasattr(self, 'ability_handler'):
            try:
                effect_text = spell.oracle_text if hasattr(spell, 'oracle_text') else ""
                
                # Use the ability handler to create effect objects
                if hasattr(self.ability_handler, '_create_ability_effects'):
                    effects = self.ability_handler._create_ability_effects(effect_text, targets)
                    
                    for effect in effects:
                        effect.apply(self, spell_id, controller, targets)
                        
                    logging.debug(f"Applied effects for {spell.name if hasattr(spell, 'name') else 'unknown spell'}")
                    
                    # Check state-based actions after resolution
                    self.check_state_based_actions()
                    
                    # Process additional keyword abilities after main effects
                    self._process_keyword_abilities(spell_id, controller, context)
                    return
            except Exception as e:
                logging.error(f"Error creating or applying effect objects: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
        
        # Process keyword abilities after other effects
        self._process_keyword_abilities(spell_id, controller, context)
        
        # Check state-based actions after resolution
        self.check_state_based_actions()

    def resolve_modal_spell(self, card_id, controller, modes=None, context=None):
        """
        Resolve a spell with multiple modes.
        
        Args:
            card_id: ID of the modal spell
            controller: The player who cast the spell
            modes: List of selected mode indices
            context: Additional context for resolution
            
        Returns:
            bool: Whether resolution was successful
        """
        if not context:
            context = {}
            
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        oracle_text = card.oracle_text.lower()
        
        # Parse modes from oracle text
        mode_texts = []
        
        # Look for standard bullet point modes
        bullet_modes = re.findall(r'[•\-−–—] (.*?)(?=[•\-−–—]|$)', oracle_text, re.DOTALL)
        if bullet_modes:
            mode_texts = bullet_modes
        
        # Look for numbered modes
        if not mode_texts:
            numbered_modes = re.findall(r'(\d+\. .*?)(?=\d+\. |$)', oracle_text, re.DOTALL)
            if numbered_modes:
                mode_texts = numbered_modes
        
        # Check for "choose one" or similar text
        choose_match = re.search(r'choose (one|two|up to two|up to three|one or more)', oracle_text)
        max_modes = 1
        if choose_match:
            choice_text = choose_match.group(1)
            if choice_text == "two":
                max_modes = 2
            elif choice_text == "up to two":
                max_modes = 2
            elif choice_text == "up to three":
                max_modes = 3
            elif choice_text == "one or more":
                max_modes = len(mode_texts)
        
        # Check for entwine
        has_entwine = "entwine" in oracle_text
        if has_entwine and "entwine" in context:
            # With entwine, we can choose all modes
            max_modes = len(mode_texts)
        
        # Check for kicker
        has_kicker = "kicker" in oracle_text
        if has_kicker and "kicked" in context:
            # Some kicked spells have additional effects
            kicked_modes = []
            for mode_text in mode_texts:
                if "if this spell was kicked" in mode_text:
                    kicked_modes.append(mode_text)
            
            # Add kicked modes to the selection
            if not modes:
                modes = []
            for i, mode_text in enumerate(mode_texts):
                if mode_text in kicked_modes:
                    modes.append(i)
        
        # If no modes specified, default to just the first mode
        if not modes and mode_texts:
            modes = [0]
        
        # Limit number of selected modes
        if len(modes) > max_modes:
            modes = modes[:max_modes]
        
        # Process each selected mode
        successful_modes = 0
        for mode_idx in modes:
            if 0 <= mode_idx < len(mode_texts):
                mode_text = mode_texts[mode_idx]
                
                # Process the effect based on the mode text
                # This would need more detailed implementation to handle all possible effects
                if "draw" in mode_text and "card" in mode_text:
                    # Draw cards effect
                    match = re.search(r'draw (\w+) cards?', mode_text)
                    count = 1
                    if match:
                        count_word = match.group(1)
                        if count_word.isdigit():
                            count = int(count_word)
                        elif count_word == "two":
                            count = 2
                        elif count_word == "three":
                            count = 3
                    
                    for _ in range(count):
                        self._draw_phase(controller)
                    
                    successful_modes += 1
                    logging.debug(f"Modal spell: Mode {mode_idx} - Drew {count} cards")
                    
                elif "destroy" in mode_text or "exile" in mode_text:
                    # Destruction/exile effect
                    # For simplicity, just destroy a creature
                    opponent = self.p2 if controller == self.p1 else self.p1
                    creatures = [cid for cid in opponent["battlefield"] 
                            if self._safe_get_card(cid) and hasattr(self._safe_get_card(cid), 'card_types') 
                            and 'creature' in self._safe_get_card(cid).card_types]
                    
                    if creatures:
                        target = creatures[0]  # Just take first one for simplicity
                        target_card = self._safe_get_card(target)
                        
                        if "exile" in mode_text:
                            self.move_card(target, opponent, "battlefield", opponent, "exile")
                        else:
                            self.move_card(target, opponent, "battlefield", opponent, "graveyard")
                        
                        successful_modes += 1
                        action = "Exiled" if "exile" in mode_text else "Destroyed"
                        logging.debug(f"Modal spell: Mode {mode_idx} - {action} {target_card.name}")
                
                elif "gain" in mode_text and "life" in mode_text:
                    # Life gain effect
                    match = re.search(r'gain (\w+) life', mode_text)
                    amount = 3  # Default
                    if match:
                        amount_word = match.group(1)
                        if amount_word.isdigit():
                            amount = int(amount_word)
                        elif amount_word == "two":
                            amount = 2
                        elif amount_word == "three":
                            amount = 3
                        elif amount_word == "four":
                            amount = 4
                    
                    controller["life"] += amount
                    successful_modes += 1
                    logging.debug(f"Modal spell: Mode {mode_idx} - Gained {amount} life")
                
                # Add more mode effect handlers as needed
        
        # Move the spell to the graveyard after resolution
        controller["graveyard"].append(card_id)
        
        return successful_modes > 0

    def get_stack_item_controller(self, stack_item_id):
        """Find the controller of a spell or ability on the stack."""
        for item in self.stack:
            if isinstance(item, tuple) and len(item) >= 3 and item[1] == stack_item_id:
                return item[2] # The controller is the 3rd element
        return None

    def handle_cast_trigger(self, card_id, controller, context=None):
        """Handle triggers that occur when a spell is cast."""
        if not context:
            context = {}
            
        # Add card type info to context
        card = self._safe_get_card(card_id)
        if card and hasattr(card, 'card_types'):
            context["card_types"] = card.card_types
            
        # Check for cast triggers on all permanents in play
        for player in [self.p1, self.p2]:
            for permanent_id in player["battlefield"]:
                self.trigger_ability(permanent_id, "SPELL_CAST", context)
                
                # Specific triggers for instant/sorcery casts
                if card and hasattr(card, 'card_types'):
                    if 'instant' in card.card_types or 'sorcery' in card.card_types:
                        self.trigger_ability(permanent_id, "CAST_NONCREATURE_SPELL", context)
                    elif 'creature' in card.card_types:
                        self.trigger_ability(permanent_id, "CAST_CREATURE_SPELL", context)
                
        # Process specific ability triggers like Storm
        if card and hasattr(card, 'oracle_text'):
            oracle_text = card.oracle_text.lower()
            
            # Storm ability
            if "storm" in oracle_text:
                # Count spells cast this turn
                if not hasattr(self, 'spells_cast_this_turn'):
                    self.spells_cast_this_turn = []
                    
                storm_count = len(self.spells_cast_this_turn)
                
                # Create copies
                for _ in range(storm_count):
                    self.stack.append(("SPELL", card_id, controller, {"is_copy": True}))
                    
                logging.debug(f"Storm triggered: Created {storm_count} copies of {card.name}")

    def conspire(self, player, spell_stack_idx, creature1_identifier, creature2_identifier):
        """Perform conspire."""
        if spell_stack_idx < 0 or spell_stack_idx >= len(self.stack) or self.stack[spell_stack_idx][0] != "SPELL":
             logging.warning("Invalid spell index for conspire.")
             return False

        spell_type, spell_id, controller, context = self.stack[spell_stack_idx]
        if controller != player: return False # Can only conspire own spells
        spell_card = self._safe_get_card(spell_id)
        if not spell_card: return False

        # --- Find Creatures ---
        c1_id = self._find_permanent_id(player, creature1_identifier)
        c2_id = self._find_permanent_id(player, creature2_identifier)

        if not c1_id or not c2_id or c1_id == c2_id:
             logging.warning("Invalid or duplicate creatures for conspire.")
             return False

        c1 = self._safe_get_card(c1_id)
        c2 = self._safe_get_card(c2_id)

        if not c1 or 'creature' not in getattr(c1, 'card_types', []) or c1_id in player.get("tapped_permanents", set()):
             logging.warning(f"Creature 1 ({getattr(c1,'name','N/A')}) invalid or tapped for conspire.")
             return False
        if not c2 or 'creature' not in getattr(c2, 'card_types', []) or c2_id in player.get("tapped_permanents", set()):
             logging.warning(f"Creature 2 ({getattr(c2,'name','N/A')}) invalid or tapped for conspire.")
             return False

        # Check color sharing
        if self._share_color(spell_card, c1) and self._share_color(spell_card, c2):
            success_tap1 = self.tap_permanent(c1_id, player)
            success_tap2 = self.tap_permanent(c2_id, player)
            if not success_tap1 or not success_tap2:
                 # Rollback taps if needed (simple untap here)
                 if success_tap1: self.untap_permanent(c1_id, player)
                 if success_tap2: self.untap_permanent(c2_id, player)
                 logging.warning("Failed to tap creatures for conspire.")
                 return False

            copy_id = self.copy_spell_on_stack(
                spell_stack_idx,
                player,
                copied_by=spell_id,
                allow_new_targets=True,
                context_overrides={"is_conspired": True},
            )
            if copy_id is None:
                return False
            logging.debug(f"Conspired {spell_card.name}")
            return True
        else:
            logging.debug("Creatures do not share a color with conspired spell.")
            return False

    def counter_spell(self, stack_index):
        """Counter spell at stack_index."""
        if 0 <= stack_index < len(self.stack):
            item_type, card_id, controller, context = self.stack.pop(stack_index)
            if item_type == "SPELL":
                # Prevent "leaves stack" triggers if appropriate? Rules check needed.
                # Move to graveyard unless specified otherwise (e.g., exile by counter)
                if context.get("is_copy", False):
                    logging.debug(f"Countered copy of {self._safe_get_card(card_id).name}; copy ceased to exist.")
                else:
                    target_zone = context.get('counter_to_zone', 'graveyard')
                    self.move_card(card_id, controller, "stack_implicit", controller, target_zone)
                    logging.debug(f"Countered spell {self._safe_get_card(card_id).name}, moved to {target_zone}.")
                self.last_stack_size = len(self.stack) # Update stack size immediately
                return True
            else: # Not a spell, put it back
                self.stack.insert(stack_index, (item_type, card_id, controller, context))
        return False

    def counter_ability(self, stack_index):
        """Counter ability/trigger at stack_index."""
        if 0 <= stack_index < len(self.stack):
            item_type, card_id, controller, context = self.stack[stack_index]
            if item_type == "ABILITY" or item_type == "TRIGGER":
                self.stack.pop(stack_index)
                logging.debug(f"Countered {item_type} from {self._safe_get_card(card_id).name}")
                self.last_stack_size = len(self.stack)
                return True
        return False

        # Add helper method to resolve individual mode effects
    def _resolve_mode_effects(self, spell_id, controller, effect_text, targets, context):
        """
        Resolve a specific mode effect.
        
        Args:
            spell_id: The ID of the spell
            controller: The player casting the spell
            effect_text: The text of the effect to apply
            targets: Targets for this mode
            context: Additional context
        """
        # Parse and apply the effect based on common patterns
        effect_text = effect_text.lower()
        
        # Import modules we'll need
        import re
        
        # Try to create a proper effect using ability_handler
        effect = None
        if hasattr(self, 'ability_handler') and hasattr(self.ability_handler, '_create_ability_effects'):
            try:
                effects = self.ability_handler._create_ability_effects(effect_text, targets)
                if effects:
                    for effect in effects:
                        effect.apply(self, spell_id, controller, targets)
                    return
            except Exception as e:
                logging.error(f"Error creating effect from text '{effect_text}': {str(e)}")
        
        # Fallback pattern matching for common effects
        if "draw" in effect_text and "card" in effect_text:
            # Card draw effect
            match = re.search(r"draw (\w+) cards?", effect_text)
            count = 1
            if match:
                count_word = match.group(1)
                if count_word.isdigit():
                    count = int(count_word)
                elif count_word == "two":
                    count = 2
                elif count_word == "three":
                    count = 3
                    
            for _ in range(count):
                self._draw_phase(controller)
            logging.debug(f"Mode effect: drew {count} cards")
            
        elif "damage" in effect_text:
            # Damage effect
            match = re.search(r"(\d+) damage", effect_text)
            damage = 2  # Default
            if match:
                damage = int(match.group(1))
                
            # Determine target
            opponent = self.p2 if controller == self.p1 else self.p1
            
            if "to target player" in effect_text or "to any target" in effect_text:
                # Damage to opponent
                opponent["life"] -= damage
                logging.debug(f"Mode effect: dealt {damage} damage to opponent")
                
            elif "to target creature" in effect_text or "to target permanent" in effect_text:
                # For simplicity, target the strongest opponent creature
                creatures = [cid for cid in opponent["battlefield"] 
                        if self._safe_get_card(cid) and 
                        hasattr(self._safe_get_card(cid), 'card_types') and 
                        'creature' in self._safe_get_card(cid).card_types]
                
                if creatures:
                    target = max(creatures, key=lambda cid: self._safe_get_card(cid).power 
                                                        if hasattr(self._safe_get_card(cid), 'power') else 0)
                    target_card = self._safe_get_card(target)
                    
                    # Check if lethal damage
                    if target_card.toughness <= damage:
                        self.move_card(target, opponent, "battlefield", opponent, "graveyard")
                        logging.debug(f"Mode effect: killed {target_card.name} with {damage} damage")
                    else:
                        # Add damage counter
                        if "damage_counters" not in opponent:
                            opponent["damage_counters"] = {}
                        opponent["damage_counters"][target] = opponent["damage_counters"].get(target, 0) + damage
                        logging.debug(f"Mode effect: dealt {damage} damage to {target_card.name}")
        
        elif "gain" in effect_text and "life" in effect_text:
            # Life gain effect
            match = re.search(r"gain (\d+) life", effect_text)
            life_gain = 2  # Default
            if match:
                life_gain = int(match.group(1))
                
            controller["life"] += life_gain
            logging.debug(f"Mode effect: gained {life_gain} life")
        
        elif "create" in effect_text and "token" in effect_text:
            # Token creation effect
            match = re.search(r"create (?:a|an|\d+) (.*?) token", effect_text)
            if match:
                token_desc = match.group(1)
                
                # Parse token details
                power, toughness = 1, 1
                pt_match = re.search(r"(\d+)/(\d+)", token_desc)
                if pt_match:
                    power = int(pt_match.group(1))
                    toughness = int(pt_match.group(2))
                
                # Parse token type
                token_type = "creature"
                if "artifact" in token_desc:
                    token_type = "artifact"
                if "treasure" in token_desc:
                    token_type = "treasure"
                    
                # Create token data
                token_data = {
                    "name": f"{token_desc.title()} Token",
                    "power": power,
                    "toughness": toughness,
                    "card_types": [token_type],
                    "subtypes": [],
                    "oracle_text": ""
                }
                
                # Add specific token abilities
                if "flying" in token_desc:
                    token_data["oracle_text"] += "Flying\n"
                if "vigilance" in token_desc:
                    token_data["oracle_text"] += "Vigilance\n"
                if "treasure" in token_desc:
                    token_data["oracle_text"] += "{T}, Sacrifice this artifact: Add one mana of any color."
                    
                # Create the token
                self.create_token(controller, token_data)
                logging.debug(f"Mode effect: created a {token_desc} token")
        
        elif "exile" in effect_text:
            # Exile effect
            opponent = self.p2 if controller == self.p1 else self.p1
            
            if "exile target permanent" in effect_text or "exile target creature" in effect_text:
                # For simplicity, target the strongest opponent creature
                target_type = "permanent" if "target permanent" in effect_text else "creature"
                
                if target_type == "creature":
                    targets = [cid for cid in opponent["battlefield"] 
                            if self._safe_get_card(cid) and 
                            hasattr(self._safe_get_card(cid), 'card_types') and 
                            'creature' in self._safe_get_card(cid).card_types]
                else:
                    targets = opponent["battlefield"]
                    
                if targets:
                    # For creatures, target the strongest one
                    if target_type == "creature":
                        target = max(targets, key=lambda cid: self._safe_get_card(cid).power 
                                                        if hasattr(self._safe_get_card(cid), 'power') else 0)
                    else:
                        # For any permanent, just take the first one
                        target = targets[0]
                        
                    target_card = self._safe_get_card(target)
                    self.move_card(target, opponent, "battlefield", opponent, "exile")
                    logging.debug(f"Mode effect: exiled {target_card.name}")
        
        elif "counter target" in effect_text:
            # Counter spell effect
            if self.stack:
                # Get the top spell on the stack
                top_item = self.stack[-1]
                
                if isinstance(top_item, tuple) and len(top_item) >= 3 and top_item[0] == "SPELL":
                    spell_id = top_item[1]
                    spell = self._safe_get_card(spell_id)
                    
                    # Check if this spell meets the counter conditions
                    can_counter = True
                    
                    if "counter target creature spell" in effect_text:
                        can_counter = hasattr(spell, 'card_types') and 'creature' in spell.card_types
                    elif "counter target noncreature spell" in effect_text:
                        can_counter = hasattr(spell, 'card_types') and 'creature' not in spell.card_types
                    
                    if can_counter:
                        # Remove from stack
                        self.stack.pop()
                        
                        # Move to graveyard
                        spell_controller = top_item[2]
                        spell_controller["graveyard"].append(spell_id)
                        
                        logging.debug(f"Mode effect: countered {spell.name}")

    def _resolve_spree_spell(self, spell_id, controller, context):
        """
        Resolve a Spree spell with selected modes.
        
        Args:
            spell_id: The ID of the Spree spell
            controller: The player casting the spell
            context: Context containing selected modes
        """
        spell = self._safe_get_card(spell_id)
        if not spell or not hasattr(spell, 'spree_modes'):
            return
        
        # Get selected modes from context
        selected_modes = context.get("selected_modes", [])
        
        # First, apply the base spell effect
        if hasattr(spell, 'card_types'):
            # Handle different card types for the base spell
            if 'instant' in spell.card_types or 'sorcery' in spell.card_types:
                # For simplicity, just apply targeting and effects
                targets = context.get("targets")
                self.resolve_spell_effects(spell_id, controller, targets, context)
            else:
                # For permanents, put them on the battlefield
                controller["battlefield"].append(spell_id)
                self.trigger_ability(spell_id, "ENTERS_BATTLEFIELD", {"controller": controller})
        
        # Apply effects for each selected mode
        for mode_idx in selected_modes:
            if mode_idx < len(spell.spree_modes):
                mode = spell.spree_modes[mode_idx]
                effect_text = mode.get("effect", "")
                
                # Create a context for this specific mode
                mode_context = dict(context)
                mode_context["mode_text"] = effect_text
                
                # Process targeting for this mode
                target_desc = mode.get("targets", "")
                mode_targets = context.get(f"mode_{mode_idx}_targets")
                
                # Apply the mode effect
                self._resolve_mode_effects(spell_id, controller, effect_text, mode_targets, mode_context)
                
                logging.debug(f"Applied Spree mode {mode_idx} for {spell.name}")
        
        # Move to graveyard if it's an instant or sorcery
        if hasattr(spell, 'card_types') and ('instant' in spell.card_types or 'sorcery' in spell.card_types):
            if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                controller["graveyard"].append(spell_id)
