import logging
from .ability_types import Ability, ActivatedAbility, TriggeredAbility, StaticAbility, KeywordAbility, ManaAbility, AbilityEffect
import re
from .card import Card
from .keyword_effects import KeywordEffects
from .ability_utils import EffectFactory

class AbilityHandler:
    """Handles card abilities and special effects"""
    
    def __init__(self, game_state=None):
        self.game_state = game_state
        self.registered_abilities = {}
        self.active_triggers = []
        self.keyword_effects = KeywordEffects(game_state)
        self.keyword_handlers = self._initialize_keyword_handlers()
        
        if game_state is not None:
            self._initialize_abilities()
            self.targeting_system = TargetingSystem(game_state)
    
    def _initialize_keyword_handlers(self):
        """Initialize handlers for all keywords."""
        # Create a mapping of keywords to their handler methods
        handlers = {
            # Basic keywords
            "saga": self.keyword_effects._apply_saga,
            "adventure": self.keyword_effects._apply_adventure,
            "mdfc": self.keyword_effects._apply_mdfc,
            "battle": self.keyword_effects._apply_battle,
            "flying": self.keyword_effects._apply_flying,
            "trample": self.keyword_effects._apply_trample,
            "hexproof": self.keyword_effects._apply_hexproof,
            "lifelink": self.keyword_effects._apply_lifelink,
            "deathtouch": self.keyword_effects._apply_deathtouch,
            "first strike": self.keyword_effects._apply_first_strike,
            "double strike": self.keyword_effects._apply_double_strike,
            "vigilance": self.keyword_effects._apply_vigilance,
            "flash": self.keyword_effects._apply_flash,
            "haste": self.keyword_effects._apply_haste,
            "menace": self.keyword_effects._apply_menace,
            "reach": self.keyword_effects._apply_reach,
            "defender": self.keyword_effects._apply_defender,
            "indestructible": self.keyword_effects._apply_indestructible,
            "protection": self.keyword_effects._apply_protection,
            "ward": self.keyword_effects._apply_ward,
            # Add Room handler
            "room": self.keyword_effects._apply_room_door_state,
        
            # Add Class handler
            "class": self.keyword_effects._apply_class_level,
            # Extended keywords
            "prowess": self.keyword_effects._apply_prowess,
            "scry": self.keyword_effects._apply_scry,
            "cascade": self.keyword_effects._apply_cascade,
            "unblockable": self.keyword_effects._apply_unblockable,
            "shroud": self.keyword_effects._apply_shroud,
            "regenerate": self.keyword_effects._apply_regenerate,
            "persist": self.keyword_effects._apply_persist,
            "undying": self.keyword_effects._apply_undying,
            "riot": self.keyword_effects._apply_riot,
            "enrage": self.keyword_effects._apply_enrage,
            "afflict": self.keyword_effects._apply_afflict,
            "exalted": self.keyword_effects._apply_exalted,
            "mentor": self.keyword_effects._apply_mentor,
            "convoke": self.keyword_effects._apply_convoke,
            "absorb": self.keyword_effects._apply_absorb,
            "affinity": self.keyword_effects._apply_affinity,
            "afterlife": self.keyword_effects._apply_afterlife,
            "cumulative upkeep": self.keyword_effects._apply_cumulative_upkeep,
            "banding": self.keyword_effects._apply_banding,
            "annihilator": self.keyword_effects._apply_annihilator,
            "bloodthirst": self.keyword_effects._apply_bloodthirst,
            "bushido": self.keyword_effects._apply_bushido,
            "companion": self.keyword_effects._apply_companion,
            "cycling": self.keyword_effects._apply_cycling,
            "dash": self.keyword_effects._apply_dash,
            "dredge": self.keyword_effects._apply_dredge,
            "echo": self.keyword_effects._apply_echo,
            "embalm": self.keyword_effects._apply_embalm,
            "devoid": self.keyword_effects._apply_devoid,
            "eternalize": self.keyword_effects._apply_eternalize,
            "evoke": self.keyword_effects._apply_evoke,
            "evolve": self.keyword_effects._apply_evolve,
            "fabricate": self.keyword_effects._apply_fabricate,
            "flashback": self.keyword_effects._apply_flashback,
            "foretell": self.keyword_effects._apply_foretell,
            "gravestorm": self.keyword_effects._apply_gravestorm,
            "hideaway": self.keyword_effects._apply_hideaway,
            "infect": self.keyword_effects._apply_infect,
            "kicker": self.keyword_effects._apply_kicker,
            "modular": self.keyword_effects._apply_modular,
            "morph": self.keyword_effects._apply_morph,
            "mutate": self.keyword_effects._apply_mutate,
            "myriad": self.keyword_effects._apply_myriad,
            "madness": self.keyword_effects._apply_madness,
            "phasing": self.keyword_effects._apply_phasing,
            "prowl": self.keyword_effects._apply_prowl,
            "unearth": self.keyword_effects._apply_unearth,
            "unleash": self.keyword_effects._apply_unleash,
            "shadow": self.keyword_effects._apply_shadow,
            "splice": self.keyword_effects._apply_splice,
            "sunburst": self.keyword_effects._apply_sunburst,
            "suspend": self.keyword_effects._apply_suspend,
            "training": self.keyword_effects._apply_training,
            "amplify": self.keyword_effects._apply_amplify,
            "ascend": self.keyword_effects._apply_ascend,
            "assist": self.keyword_effects._apply_assist,
            "aura swap": self.keyword_effects._apply_aura_swap,
            "awaken": self.keyword_effects._apply_awaken,
            "battle cry": self.keyword_effects._apply_battle_cry,
            "bestow": self.keyword_effects._apply_bestow,
            "blitz": self.keyword_effects._apply_blitz,
            "boast": self.keyword_effects._apply_boast,
            "buyback": self.keyword_effects._apply_buyback,
            "casualty": self.keyword_effects._apply_casualty,
            "storm": self.keyword_effects._apply_storm,
            "crew": self.keyword_effects._apply_crew,
            "delve": self.keyword_effects._apply_delve,
            "equip": self.keyword_effects._apply_equip,
            "cleave": self.keyword_effects._apply_cleave,
            "daybound": self.keyword_effects._apply_daybound,
            "nightbound": self.keyword_effects._apply_nightbound,
            "decayed": self.keyword_effects._apply_decayed,
            "champion": self.keyword_effects._apply_champion,
            "changeling": self.keyword_effects._apply_changeling,
            "conspire": self.keyword_effects._apply_conspire,

            "devour": self.keyword_effects._apply_devour,
            "disturb": self.keyword_effects._apply_disturb,
            "emerge": self.keyword_effects._apply_emerge,
            "enchant": self.keyword_effects._apply_enchant,
            "compleated": self.keyword_effects._apply_compleated,
            "encore": self.keyword_effects._apply_encore,
            "entwine": self.keyword_effects._apply_entwine,
            "epic": self.keyword_effects._apply_epic,
            "escape": self.keyword_effects._apply_escape,
            "exploit": self.keyword_effects._apply_exploit,
            "extort": self.keyword_effects._apply_extort,
            "fading": self.keyword_effects._apply_fading,
            "fear": self.keyword_effects._apply_fear,
            "flanking": self.keyword_effects._apply_flanking,
            "forecast": self.keyword_effects._apply_forecast,
            "fortify": self.keyword_effects._apply_fortify,
            "frenzy": self.keyword_effects._apply_frenzy,
            "friends forever": self.keyword_effects._apply_friends_forever,
            "fuse": self.keyword_effects._apply_fuse,
            "graft": self.keyword_effects._apply_graft,
            "haunt": self.keyword_effects._apply_haunt,
            "hidden agenda": self.keyword_effects._apply_hidden_agenda,
            "horsemanship": self.keyword_effects._apply_horsemanship,
            "improvise": self.keyword_effects._apply_improvise,
            "ingest": self.keyword_effects._apply_ingest,
            "intimidate": self.keyword_effects._apply_intimidate,
            "jump-start": self.keyword_effects._apply_jump_start,
            "landwalk": self.keyword_effects._apply_landwalk,
            "cipher": self.keyword_effects._apply_cipher,
            "demonstrate": self.keyword_effects._apply_demonstrate,
            "living weapon": self.keyword_effects._apply_living_weapon,
            "melee": self.keyword_effects._apply_melee,
            "miracle": self.keyword_effects._apply_miracle,
            "offering": self.keyword_effects._apply_offering,
            "outlast": self.keyword_effects._apply_outlast,
            "overload": self.keyword_effects._apply_overload,
            "partner": self.keyword_effects._apply_partner,
            "poisonous": self.keyword_effects._apply_poisonous,
            "provoke": self.keyword_effects._apply_provoke,
            "rampage": self.keyword_effects._apply_rampage,
            "rebound": self.keyword_effects._apply_rebound,
            "reconfigure": self.keyword_effects._apply_reconfigure,
            "recover": self.keyword_effects._apply_recover,
            "reinforce": self.keyword_effects._apply_reinforce,
            "renown": self.keyword_effects._apply_renown,
            "replicate": self.keyword_effects._apply_replicate,
            "retrace": self.keyword_effects._apply_retrace,
            "ripple": self.keyword_effects._apply_ripple,
            "scavenge": self.keyword_effects._apply_scavenge,
            "skulk": self.keyword_effects._apply_skulk,
            "soulbond": self.keyword_effects._apply_soulbond,
            "soulshift": self.keyword_effects._apply_soulshift,
            "spectacle": self.keyword_effects._apply_spectacle,
            "split second": self.keyword_effects._apply_split_second,
            "surge": self.keyword_effects._apply_surge,
            "totem armor": self.keyword_effects._apply_totem_armor,
            "transfigure": self.keyword_effects._apply_transfigure,
            "transmute": self.keyword_effects._apply_transmute,
            "tribute": self.keyword_effects._apply_tribute,
            "undaunted": self.keyword_effects._apply_undaunted,
            "vanishing": self.keyword_effects._apply_vanishing,
            "wither": self.keyword_effects._apply_wither,
            "aftermath": self.keyword_effects._apply_aftermath
        }

        return handlers

    def handle_class_level_up(self, class_idx):
        """
        Handle leveling up a Class card with proper trigger processing.
        
        Args:
            class_idx: Index of the Class card in the battlefield
            
        Returns:
            bool: True if the class was successfully leveled up
        """
        gs = self.game_state
        
        # Get active player
        active_player = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Check if class index is valid
        if class_idx < 0 or class_idx >= len(active_player["battlefield"]):
            logging.warning(f"Invalid class index: {class_idx}")
            return False
        
        # Get the class card
        class_id = active_player["battlefield"][class_idx]
        class_card = gs._safe_get_card(class_id)
        
        # Verify it's a class card
        if not class_card or not hasattr(class_card, 'is_class') or not class_card.is_class:
            logging.warning(f"Card with index {class_idx} is not a Class")
            return False
        
        # Check if the class can level up
        if not class_card.can_level_up():
            logging.warning(f"Class {class_card.name} cannot level up further")
            return False
        
        # Get the cost to level up
        next_level = class_card.current_level + 1 if hasattr(class_card, 'current_level') else 2
        level_cost = class_card.get_level_cost(next_level)
        
        # Check if we can afford to level up
        can_afford = True
        if level_cost and hasattr(gs, 'mana_system'):
            level_cost_parsed = gs.mana_system.parse_mana_cost(level_cost)
            can_afford = gs.mana_system.can_pay_mana_cost(active_player, level_cost_parsed)
        
        if not can_afford:
            logging.debug(f"Cannot afford to level up {class_card.name}")
            return False
        
        # Pay the cost
        if level_cost and hasattr(gs, 'mana_system'):
            gs.mana_system.pay_mana_cost(active_player, level_cost_parsed)
        
        # Level up the class
        success = class_card.level_up()
        
        if success:
            # Trigger level-up effects
            context = {
                "class_id": class_id,
                "previous_level": next_level - 1,
                "new_level": next_level,
                "controller": active_player
            }
            
            # Trigger ability for level change
            self.check_abilities(class_id, "CLASS_LEVEL_UP", context)
            
            # Reset priority for new abilities
            gs.phase = gs.PHASE_PRIORITY
            
            logging.debug(f"Leveled up {class_card.name} to level {next_level}")
            
            # Check if class became a creature at this level
            if hasattr(class_card, 'get_current_class_data'):
                level_data = class_card.get_current_class_data()
                
                if level_data and 'type_line' in level_data:
                    # Check if type line includes 'creature' at new level but not before
                    if ('creature' in level_data['type_line'].lower() and 
                        (not hasattr(class_card, 'previous_type_line') or 
                        'creature' not in class_card.previous_type_line.lower())):
                        
                        # Update card types
                        if hasattr(class_card, 'card_types'):
                            if 'creature' not in class_card.card_types:
                                class_card.card_types.append('creature')
                        
                        # Set power/toughness if specified
                        if 'power' in level_data:
                            class_card.power = level_data['power']
                        if 'toughness' in level_data:
                            class_card.toughness = level_data['toughness']
                        
                        logging.debug(f"Class {class_card.name} became a creature at level {next_level}")
                        
                    # Save current type line for future reference
                    class_card.previous_type_line = level_data['type_line']
            
            # Re-parse abilities for this level if needed
            self._parse_and_register_abilities(class_id, class_card)
        
        return success
        
    def handle_unlock_door(self, room_idx):
        """
        Handle unlocking a door on a Room card with proper trigger processing.
        
        Args:
            room_idx: Index of the Room card in the battlefield
            
        Returns:
            bool: True if door was unlocked successfully
        """
        gs = self.game_state
        
        # Get active player
        active_player = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Check if room index is valid
        if room_idx < 0 or room_idx >= len(active_player["battlefield"]):
            logging.warning(f"Invalid room index: {room_idx}")
            return False
        
        # Get the room card
        room_id = active_player["battlefield"][room_idx]
        room_card = gs._safe_get_card(room_id)
        
        # Verify it's a room card
        if not room_card or not hasattr(room_card, 'is_room') or not room_card.is_room:
            logging.warning(f"Card with index {room_idx} is not a Room")
            return False
        
        # Check if door2 is locked
        if not hasattr(room_card, 'door2') or room_card.door2.get('unlocked', False):
            logging.warning(f"Door is already unlocked or does not exist for Room {room_card.name}")
            return False
        
        # Get door cost
        door_cost = room_card.door2.get('mana_cost', '')
        
        # Check if we can afford to unlock
        can_afford = True
        if hasattr(gs, 'mana_system'):
            door_cost_parsed = gs.mana_system.parse_mana_cost(door_cost)
            can_afford = gs.mana_system.can_pay_mana_cost(active_player, door_cost_parsed)
        else:
            # Simple cost check (fallback)
            total_cost = 0
            for char in door_cost:
                if char.isdigit():
                    total_cost += int(char)
            can_afford = active_player.get("mana", 0) >= total_cost
        
        if not can_afford:
            logging.debug(f"Cannot afford to unlock door for {room_card.name}")
            return False
        
        # Pay the cost
        if hasattr(gs, 'mana_system'):
            door_cost_parsed = gs.mana_system.parse_mana_cost(door_cost)
            gs.mana_system.pay_mana_cost(active_player, door_cost_parsed)
        else:
            # Simple cost deduction
            active_player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        
        # Unlock the door
        room_card.door2['unlocked'] = True
        
        # Check if both doors are now unlocked
        fully_unlocked = False
        if hasattr(room_card, 'door1') and room_card.door1.get('unlocked', False):
            fully_unlocked = True
        
        # Process trigger effects
        context = {
            "door_number": 2,
            "room_id": room_id,
            "controller": active_player,
            "cost_paid": door_cost,
            "fully_unlocked": fully_unlocked
        }
        
        # Trigger door unlocked ability
        self.check_abilities(room_id, "DOOR_UNLOCKED", context)
        
        # Also trigger specific door2 unlocked event
        self.check_abilities(room_id, "DOOR2_UNLOCKED", context)
        
        # If fully unlocked, trigger that too
        if fully_unlocked:
            self.check_abilities(room_id, "ROOM_FULLY_UNLOCKED", context)
        
        # Check for chapter advancement on the room card
        if hasattr(room_card, 'current_chapter') and hasattr(room_card, 'max_chapters'):
            room_card.current_chapter += 1
            logging.debug(f"Room {room_card.name} advanced to chapter {room_card.current_chapter}")
            
            # Create chapter context
            chapter_context = {
                "room_id": room_id,
                "controller": active_player,
                "chapter": room_card.current_chapter
            }
            
            # Trigger chapter ability if available
            if hasattr(self, 'apply_chapter_ability'):
                self.apply_chapter_ability(room_id, room_card.current_chapter, chapter_context)
            
            # Check if final chapter reached
            if room_card.current_chapter >= room_card.max_chapters:
                logging.debug(f"Room {room_card.name} completed all chapters")
                # Trigger completion effect
                self.check_abilities(room_id, "ROOM_COMPLETED", chapter_context)
        
        # Reset priority for new abilities
        gs.phase = gs.PHASE_PRIORITY
        
        logging.debug(f"Unlocked door 2 for Room {room_card.name}")
        
        # Re-parse abilities to include newly unlocked door
        self._parse_and_register_abilities(room_id, room_card)
        
        return True
    
    def _parse_text_with_patterns(self, text, patterns, ability_type, card_id, card, abilities_list):
        """
        Parse oracle text using regex patterns and create abilities of the specified type.
        
        Args:
            text: The oracle text to parse
            patterns: List of (pattern, restriction_generator) tuples for regex matching
            ability_type: Type of ability to create (ActivatedAbility, TriggeredAbility, etc.)
            card_id: ID of the card with the ability
            card: Card object
            abilities_list: List to add the created abilities to
        """
        for pattern, restriction_func in patterns:
            matches = re.finditer(pattern, text.lower())
            for match in matches:
                # Skip reminder text in parentheses
                if '(' in match.group(0) and ')' in match.group(0):
                    continue
                    
                # Create ability based on type
                if ability_type == "activated":
                    cost, effect = match.groups() if len(match.groups()) >= 2 else (None, None)
                    if cost and effect:
                        ability = ActivatedAbility(
                            card_id=card_id,
                            cost=cost.strip(),
                            effect=effect.strip(),
                            effect_text=f"{cost}: {effect}"
                        )
                        abilities_list.append(ability)
                        logging.debug(f"Registered activated ability for {card.name}: {cost}: {effect}")
                
                elif ability_type == "triggered":
                    # Extract trigger and effect
                    effect_parts = match.group(0).split(',', 1)
                    trigger = effect_parts[0].strip()
                    effect = effect_parts[1].strip() if len(effect_parts) > 1 else ""
                    
                    # Extract any conditions
                    condition = None
                    if " if " in effect:
                        effect_parts = effect.split(" if ", 1)
                        effect = effect_parts[0].strip()
                        condition = "if " + effect_parts[1].strip()
                    elif " only if " in effect:
                        effect_parts = effect.split(" only if ", 1)
                        effect = effect_parts[0].strip()
                        condition = "only if " + effect_parts[1].strip()
                    
                    ability = TriggeredAbility(
                        card_id=card_id,
                        trigger_condition=trigger,
                        effect=effect,
                        effect_text=match.group(0),
                        additional_condition=condition
                    )
                    abilities_list.append(ability)
                    logging.debug(f"Registered triggered ability for {card.name}: {match.group(0)}")
                
                elif ability_type == "static":
                    # Apply any restrictions from the pattern
                    restrictions = {}
                    if callable(restriction_func):
                        restrictions = restriction_func(match)
                        
                    effect = match.group(0)
                    ability = StaticAbility(
                        card_id=card_id,
                        effect=effect,
                        effect_text=effect
                    )
                    abilities_list.append(ability)
                    logging.debug(f"Registered static ability for {card.name}: {effect}")
                    
    def _get_complex_trigger_phrases(self):
        """Get mapping of complex trigger phrases to event types."""
        return {
            "whenever you gain life": "GAIN_LIFE",
            "whenever you lose life": "LOSE_LIFE",
            "whenever a creature you control dies": "CREATURE_DIED",
            "whenever a land enters the battlefield under your control": "LAND_PLAYED",
            "whenever you cast your first spell each turn": "CAST_SPELL",
            "whenever a spell or ability an opponent controls targets": "TARGETED",
            "whenever you complete a room": "ROOM_COMPLETED",
            "when this class level increases": "CLASS_LEVEL_UP",
            "when this door is unlocked": "DOOR_UNLOCKED"
        }
        
    def trigger_ability(self, card_id, event_type, context=None):
        """
        Enhanced ability triggering with comprehensive system integration.
        
        Args:
            card_id: ID of the card triggering the ability
            event_type: Type of event that triggered the ability
            context: Additional context for the trigger
            
        Returns:
            list: List of triggered abilities that were added to the stack
        """
        if context is None:
            context = {}
        
        triggered_abilities = []
        
        # Add card type info to context
        card = self._safe_get_card(card_id)
        if card:
            if hasattr(card, 'card_types'):
                context["card_types"] = card.card_types
            context["source_card_id"] = card_id
            context["source_card_name"] = getattr(card, 'name', f"Card {card_id}")
        
        # Determine the card's owner and controller
        owner = self._find_card_owner(card_id)
        if not owner and card_id is not None:
            logging.warning(f"Could not find owner for card {card_id}")
            return triggered_abilities
        
        # Use ability handler if available
        if hasattr(self, 'ability_handler') and self.ability_handler:
            # Get triggered abilities
            handler_abilities = self.ability_handler.check_abilities(card_id, event_type, context)
            
            if handler_abilities:
                # Add each triggered ability to the stack
                for ability in handler_abilities:
                    # Create a unique ID for this ability instance
                    ability_instance_id = f"trigger_{card_id}_{event_type}_{len(self.stack)}"
                    
                    # Add to the stack unless it's a static ability
                    if getattr(ability, 'is_static', False):
                        # Static abilities don't use the stack
                        logging.debug(f"Static ability from {context.get('source_card_name', f'Card {card_id}')} applied directly")
                    else:
                        # Add to stack
                        self.stack.append(("TRIGGER", card_id, owner, {
                            "ability_instance_id": ability_instance_id,
                            "event_type": event_type,
                            "ability": ability,
                            "context": context
                        }))
                        
                        # Reset priority system for new stack item
                        self.priority_pass_count = 0
                        self.last_stack_size = len(self.stack)
                        self.phase = self.PHASE_PRIORITY
                        
                        triggered_abilities.append(ability_instance_id)
                        
                        logging.debug(f"Added triggered ability to stack: {ability.effect_text if hasattr(ability, 'effect_text') else 'Unknown ability'}")
                
                # Process triggered abilities that don't use the stack
                if hasattr(self.ability_handler, 'process_triggered_abilities'):
                    self.ability_handler.process_triggered_abilities()
        
        # Trigger replacement effects if applicable
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            replacement_context = {
                "event_type": event_type,
                "source_card_id": card_id,
                "affected_id": context.get('affected_id'),
                **context
            }
            
            # Apply any replacement effects
            modified_context, was_replaced = self.replacement_effects.apply_replacements(event_type, replacement_context)
        
        # Layer system interaction for continuous effects
        if hasattr(self, 'layer_system') and self.layer_system:
            # Refresh the layer system
            self.layer_system.apply_all_effects()
        
        return triggered_abilities

    def handle_modal_ability(self, card_id, controller, mode_index):
        """
        Handle a modal ability (with multiple options).
        
        Args:
            card_id: The ID of the card with the modal ability
            controller: The player activating the ability
            mode_index: Which mode was chosen
                    
        Returns:
            bool: Whether the ability was successfully activated and added to the stack
        """
        card = self.game_state._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'):
            return False
        
        # Parse the modes from the oracle text
        modes = self._parse_modal_text(card.oracle_text)
        
        if not modes or mode_index >= len(modes):
            logging.debug(f"Invalid mode index {mode_index} for {card.name}")
            return False
        
        # Get the chosen mode text
        mode_text = modes[mode_index]
        
        # Get the cost of the modal ability
        ability_cost = "0"  # Default to no additional cost
        
        # Check for costs in the modal ability text
        cost_pattern = r'([{][^}]+[}])'
        cost_matches = re.findall(cost_pattern, card.oracle_text.split("choose")[0]) if "choose" in card.oracle_text.lower() else []
        
        if cost_matches:
            ability_cost = ''.join(cost_matches)
        
        # Check if the player can pay the cost
        if ability_cost != "0" and hasattr(self.game_state, 'mana_system') and self.game_state.mana_system:
            parsed_cost = self.game_state.mana_system.parse_mana_cost(ability_cost)
            if not self.game_state.mana_system.can_pay_mana_cost(controller, parsed_cost):
                logging.debug(f"Cannot pay cost for modal ability: {ability_cost}")
                return False
            
            # Pay the cost
            paid = self.game_state.mana_system.pay_mana_cost(controller, parsed_cost)
            if not paid:
                return False
            
            logging.debug(f"Paid cost {ability_cost} for modal ability")
        
        # Create effects for this mode
        mode_effects = EffectFactory.create_effects(mode_text)
        
        # Determine targets if needed
        targets = {}
        if "target" in mode_text.lower() and hasattr(self, 'targeting_system'):
            targets = self.targeting_system.resolve_targeting(card_id, controller, mode_text)
            
            # If targeting fails and was required, refund the cost and return failure
            if not targets and "target" in mode_text.lower():
                if ability_cost != "0" and hasattr(self.game_state, 'mana_system'):
                    # Refund the cost if targeting failed
                    self.game_state.mana_system.refund_mana_cost(controller, parsed_cost)
                return False
        
        # Add the ability to the stack
        self.game_state.add_to_stack("ABILITY", card_id, controller, {
            "ability_text": mode_text,
            "mode_index": mode_index,
            "mode_text": mode_text,
            "targets": targets,
            "effects": mode_effects
        })
        
        logging.debug(f"Added modal ability to stack: {mode_text} for {card.name}")
        return True

    def _parse_modal_text(self, oracle_text):
        """Parse modal text from card oracle text."""
        modes = []
        
        # Convert text to lowercase for easier matching
        text = oracle_text.lower()
        
        # Check for modal markers
        modal_markers = [
            "choose one —", 
            "choose one or both —", 
            "choose one or more —",
            "choose two —",
            "choose up to one —",
            "choose up to two —",
            "choose up to three —"
        ]
        
        modal_match = None
        for marker in modal_markers:
            if marker in text:
                modal_match = marker
                break
        
        if modal_match:
            # Split text after the modal marker
            modal_text = text.split(modal_match, 1)[1].strip()
            
            # Split by bullet points or numbered markers
            if '•' in modal_text or '●' in modal_text:
                # Split by bullet points
                mode_parts = re.split(r'[•●]', modal_text)
                for part in mode_parts:
                    part = part.strip()
                    if part:
                        modes.append(part)
            else:
                # If no bullet points, try to split by sequentially numbered options
                # e.g., "1. Do this. 2. Do that."
                mode_parts = re.split(r'\d+\.\s+', modal_text)
                for part in mode_parts[1:]:  # Skip the first empty part
                    part = part.strip()
                    if part:
                        modes.append(part)
        
        # Post-process modes to clean up
        clean_modes = []
        for mode in modes:
            # Remove trailing periods and other punctuation
            mode = re.sub(r'[;,\.]+$', '', mode).strip()
            if mode:
                clean_modes.append(mode)
        
        return clean_modes
        
    def _find_controller(self, card_id):
        """Find the controller of a card."""
        gs = self.game_state
        for player in [gs.p1, gs.p2]:
            for zone in ["battlefield", "hand", "graveyard", "exile"]:
                if card_id in player.get(zone, []):
                    return player
        return None
        
    def register_card_abilities(self, card_id, player):
        """Register all abilities for a card as it enters the battlefield."""
        try:
            card = self.game_state._safe_get_card(card_id)
            if not card:
                logging.warning(f"Cannot register abilities: card {card_id} not found")
                return
                
            # Parse and register abilities
            self._parse_and_register_abilities(card_id, card)
            
            # Check for replacement effects
            if hasattr(self.game_state, 'replacement_effect_system'):
                self.game_state.replacement_effect_system.register_card_replacement_effects(card_id, player)
                
            # Check for static abilities that need to be applied
            for ability in self.registered_abilities.get(card_id, []):
                if isinstance(ability, StaticAbility):
                    # Determine affected cards
                    affected_cards = ability.get_affected_cards(self.game_state, player)
                    
                    # Apply the static ability
                    ability.apply(self.game_state, affected_cards)
                    
        except Exception as e:
            logging.error(f"Error registering abilities for card {card_id}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
                
    def unregister_card_abilities(self, card_id):
        """Unregister all abilities for a card as it leaves the battlefield."""
        try:
            # Remove from registered abilities
            if card_id in self.registered_abilities:
                del self.registered_abilities[card_id]
                
            # Remove from active triggers
            self.active_triggers = [trigger for trigger in self.active_triggers 
                                if trigger[0].card_id != card_id]
            
            # Remove from replacement effects
            if hasattr(self.game_state, 'replacement_effect_system'):
                # Specific method to remove effects by source
                self.game_state.replacement_effect_system.remove_effects_by_source(card_id)
                
            # Remove from layer system
            if hasattr(self.game_state, 'layer_system'):
                # Remove effects originating from this card
                self.game_state.layer_system.remove_effects_by_source(card_id)
        
        except Exception as e:
            logging.error(f"Error unregistering abilities for card {card_id}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
    
    def handle_door_unlock_triggers(self, room_id, door_number):
        """Handle triggers that occur when a door is unlocked."""
        gs = self.game_state
        card = gs._safe_get_card(room_id)
        if not card:
            return
            
        # Find controller
        controller = None
        for player in [gs.p1, gs.p2]:
            if room_id in player["battlefield"]:
                controller = player
                break
                
        if not controller:
            return
        
        # Create context for the trigger
        context = {
            "door_number": door_number,
            "controller": controller,
            "game_state": gs
        }
        
        # Trigger abilities
        self.check_abilities(room_id, "DOOR_UNLOCKED", context)
        
        # If this is a specific door, also trigger abilities for that door
        if door_number > 0:
            self.check_abilities(room_id, f"DOOR{door_number}_UNLOCKED", context)
        
        # Process door effect triggers from card data if available
        if hasattr(card, 'door_effects') and card.door_effects.get("unlock_trigger"):
            for effect_text in card.door_effects.get("unlock_trigger", []):
                # Create a custom effect and add to stack
                effect_context = {
                    "effect_text": effect_text,
                    "controller": controller,
                    "source_id": room_id
                }
                
                # Add to stack
                gs.add_to_stack("ABILITY", room_id, controller, effect_context)
                
        logging.debug(f"Processed door unlock triggers for door {door_number} of {card.name}")
    
    def _initialize_abilities(self):
        """Parse abilities from all cards in the database and register them"""
        gs = self.game_state
        for card_id, card in gs.card_db.items():
            self._parse_and_register_abilities(card_id, card)
    
    def _parse_and_register_abilities(self, card_id, card):
        """Parse a card's oracle text to identify and register abilities, with class level support."""
        if not hasattr(card, 'oracle_text') or not card.oracle_text:
            return
            
        # Extract abilities from card text
        oracle_text = card.oracle_text.lower()
        abilities = []
        
        # Check if this is a Class card with multiple levels
        if hasattr(card, 'is_class') and card.is_class:
            # Register abilities based on current level
            current_level = getattr(card, 'current_level', 0)
            
            # Get class data for current level
            level_data = None
            if hasattr(card, 'get_current_class_data'):
                level_data = card.get_current_class_data()
                
            if level_data and 'abilities' in level_data:
                # Register level-specific abilities
                for ability_text in level_data['abilities']:
                    self._parse_ability_text(card_id, card, ability_text, abilities)
                    
                logging.debug(f"Registered {len(level_data['abilities'])} abilities for {card.name} at level {current_level}")
                
                # Check if class became a creature at this level
                if 'creature' in level_data.get('type_line', '').lower() and 'creature' not in card.type_line.lower():
                    # Register any keywords that apply to creatures
                    self._register_keyword_abilities(card_id, card, abilities)
                    logging.debug(f"Class {card.name} became a creature at level {current_level}")
        
        # Continue with normal ability registration
        self._register_keyword_abilities(card_id, card, abilities)
        self._parse_activated_abilities(card_id, card, oracle_text, abilities)
        self._parse_triggered_abilities(card_id, card, oracle_text, abilities)
        self._parse_static_abilities(card_id, card, oracle_text, abilities)
        
        # Store all parsed abilities
        if abilities:
            self.registered_abilities[card_id] = abilities
            logging.debug(f"Registered {len(abilities)} abilities for {card.name}")
            
    def _parse_triggered_abilities(self, card_id, card, oracle_text, abilities_list):
        """Parse triggered abilities from card text with improved patterns"""
        # Define enhanced patterns for triggered abilities
        trigger_patterns = [
            # "When/Whenever X, do Y" pattern with named capture groups
            (r'(?P<trigger>when(?:ever)?\s+[^,\.]+?),\s+(?P<effect>[^\.;]+)', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip()}),
            
            # "At the beginning of X, do Y" pattern
            (r'(?P<trigger>at\s+the\s+beginning\s+of\s+[^,\.]+?),\s+(?P<effect>[^\.;]+)', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip()}),
            
            # Pattern for ETB triggers
            (r'(?P<trigger>when(?:ever)?\s+[^,\.]+?\s+enters\s+the\s+battlefield[^,\.]*?),\s+(?P<effect>[^\.;]+)', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip(), "etb": True}),
            
            # Pattern for death triggers
            (r'(?P<trigger>when(?:ever)?\s+[^,\.]+?\s+dies[^,\.]*?),\s+(?P<effect>[^\.;]+)', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip(), "dies": True}),
            
            # Pattern with "if" condition
            (r'(?P<trigger>when(?:ever)?\s+[^,\.]+?),\s+(?P<effect>[^\.;]+?)(?:\s+if\s+(?P<condition>[^\.;]+))?', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), 
                    "effect": m.group("effect").strip(),
                    "additional_condition": ("if " + m.group("condition").strip()) if m.group("condition") else None}),
                    
            # Door unlocked triggers
            (r'(?P<trigger>when(?:ever)?\s+(?:you\s+unlock\s+this\s+door|this\s+door\s+becomes\s+unlocked)[^,\.]*?),\s+(?P<effect>[^\.;]+)',
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip(), "door_unlock": True}),
            
            # Class level up triggers  
            (r'(?P<trigger>when\s+this\s+class\s+level\s+increases[^,\.]*?),\s+(?P<effect>[^\.;]+)',
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip(), "class_level_up": True})
        ]
        
        # Get complex trigger phrases to event type mappings
        complex_triggers = self._get_complex_trigger_phrases()
        
        # Split text by periods to get separate abilities
        sentences = oracle_text.split('.')
        
        for sentence in sentences:
            sentence = sentence.strip().lower()
            if not sentence:
                continue
                
            # Check for complex triggers first
            for phrase, event_type in complex_triggers.items():
                if phrase in sentence:
                    trigger = sentence.split(',', 1)
                    if len(trigger) > 1:
                        effect = trigger[1].strip()
                        # Create TriggeredAbility
                        ability = TriggeredAbility(
                            card_id=card_id,
                            trigger_condition=phrase,
                            effect=effect,
                            effect_text=sentence
                        )
                        abilities_list.append(ability)
                        logging.debug(f"Registered complex triggered ability for {card.name}: {sentence}")
                    break
                    
            # Parse and add abilities using regular patterns
            self._parse_text_with_patterns(
                sentence, trigger_patterns, "triggered", card_id, card, abilities_list)
            
    def _parse_ability_text(self, card_id, card, ability_text, abilities_list):
        """Parse a single ability text and add appropriate ability objects to the list."""
        ability_text = ability_text.lower()
        
        # Check for activated ability pattern
        if ":" in ability_text:
            parts = ability_text.split(":", 1)
            if len(parts) == 2:
                cost, effect = parts[0].strip(), parts[1].strip()
                ability = ActivatedAbility(
                    card_id=card_id,
                    cost=cost,
                    effect=effect,
                    effect_text=ability_text
                )
                abilities_list.append(ability)
                return
                
        # Check for triggered ability patterns
        for pattern, event_type in [
            (r'when(ever)?\s+', 'GENERIC_TRIGGER'),
            (r'at the beginning of', 'PHASE_TRIGGER'),
            (r'when(ever)?\s+you unlock', 'DOOR_UNLOCKED')
        ]:
            if re.search(pattern, ability_text):
                ability = TriggeredAbility(
                    card_id=card_id,
                    trigger_condition=ability_text,
                    effect=ability_text.split(",", 1)[1].strip() if "," in ability_text else ability_text,
                    effect_text=ability_text
                )
                abilities_list.append(ability)
                return
                
        # Default to static ability
        ability = StaticAbility(
            card_id=card_id,
            effect=ability_text,
            effect_text=ability_text
        )
        abilities_list.append(ability)
    
    def _register_keyword_abilities(self, card_id, card, abilities_list):
        """Register keyword abilities from the card's keywords attribute"""
        keywords = [
            "flying", "trample", "hexproof", "lifelink", "deathtouch",
            "first strike", "double strike", "vigilance", "flash", "haste", 
            "menace", "reach", "defender", "indestructible"
        ]
        
        # Check card.keywords (boolean array) if it exists
        if hasattr(card, 'keywords') and isinstance(card.keywords, list):
            # Map keyword indices to actual keyword names
            keyword_indices = {
                0: "flying",
                1: "trample", 
                2: "hexproof",
                3: "lifelink",
                4: "deathtouch",
                5: "first strike",
                6: "double strike",
                7: "vigilance",
                8: "flash",
                9: "haste",
                10: "menace"
            }
            
            for idx, has_keyword in enumerate(card.keywords):
                if has_keyword and idx in keyword_indices:
                    keyword_name = keyword_indices[idx]
                    keyword_ability = KeywordAbility(card_id, keyword_name)
                    abilities_list.append(keyword_ability)
                    logging.debug(f"Registered {keyword_name} for {card.name}")
        
        # Also check card text for keywords as backup
        if hasattr(card, 'oracle_text'):
            for keyword in keywords:
                if keyword in card.oracle_text.lower() and not any(isinstance(a, KeywordAbility) and a.keyword == keyword for a in abilities_list):
                    keyword_ability = KeywordAbility(card_id, keyword)
                    abilities_list.append(keyword_ability)
                    logging.debug(f"Registered {keyword} for {card.name} from text")
                    
    def _create_token(self, controller, token_data):
        """Create a token creature or artifact"""
        gs = self.game_state
        
        # Create token tracking if it doesn't exist
        if not hasattr(controller, "tokens"):
            controller["tokens"] = []
        
        # Generate unique token ID
        token_id = f"TOKEN_{len(controller['tokens'])}"
        
        # Create token based on provided data
        token = Card({
            "name": token_data.get("name", "Token"),
            "type_line": token_data.get("type_line", "Creature"),
            "card_types": token_data.get("card_types", ["creature"]),
            "subtypes": token_data.get("subtypes", []),
            "power": token_data.get("power", 1),
            "toughness": token_data.get("toughness", 1),
            "oracle_text": token_data.get("oracle_text", ""),
            "keywords": token_data.get("keywords", [0] * 11),
            "colors": token_data.get("colors", [0, 0, 0, 0, 0])
        })
        
        # Add token to game
        gs.card_db[token_id] = token
        controller["battlefield"].append(token_id)
        controller["tokens"].append(token_id)
        
        # Mark token as having summoning sickness
        controller["entered_battlefield_this_turn"].add(token_id)
        
        logging.debug(f"Created {token_data.get('name', 'Token')} token")
        return token_id

    def _destroy_permanent(self, permanent_id, player=None):
        """Destroy a permanent, handling indestructible"""
        gs = self.game_state
        
        # Find owner if not provided
        if not player:
            for p in [gs.p1, gs.p2]:
                if permanent_id in p["battlefield"]:
                    player = p
                    break
        
        if not player:
            logging.debug(f"Could not find owner for permanent {permanent_id}")
            return False
            
        # Check for indestructible
        card = gs._safe_get_card(permanent_id)
        if card and hasattr(card, 'oracle_text') and "indestructible" in card.oracle_text.lower():
            logging.debug(f"Cannot destroy {card.name} due to indestructible")
            return False
            
        # Move to graveyard
        gs.move_card(permanent_id, player, "battlefield", player, "graveyard")
        logging.debug(f"Destroyed {card.name if card else permanent_id}")
        return True

    def _exile_permanent(self, permanent_id, player=None):
        """Exile a permanent, bypassing indestructible"""
        gs = self.game_state
        
        # Find owner if not provided
        if not player:
            for p in [gs.p1, gs.p2]:
                if permanent_id in p["battlefield"]:
                    player = p
                    break
        
        if not player:
            logging.debug(f"Could not find owner for permanent {permanent_id}")
            return False
            
        # Move to exile
        gs.move_card(permanent_id, player, "battlefield", player, "exile")
        card = gs._safe_get_card(permanent_id)
        logging.debug(f"Exiled {card.name if card else permanent_id}")
        return True
    
    def _add_counters(self, permanent_id, counter_type, count, player=None):
        """Add counters to a permanent"""
        gs = self.game_state
        
        # Find owner if not provided
        if not player:
            for p in [gs.p1, gs.p2]:
                if permanent_id in p["battlefield"]:
                    player = p
                    break
        
        if not player:
            logging.debug(f"Could not find owner for permanent {permanent_id}")
            return False
            
        # Get the card
        card = gs._safe_get_card(permanent_id)
        if not card:
            return False
            
        # Initialize counters if needed
        if not hasattr(card, "counters"):
            card.counters = {}
            
        # Add counters
        card.counters[counter_type] = card.counters.get(counter_type, 0) + count
        
        # Apply counter effects
        if counter_type == "+1/+1" and hasattr(card, 'power') and hasattr(card, 'toughness'):
            card.power += count
            card.toughness += count
        elif counter_type == "-1/-1" and hasattr(card, 'power') and hasattr(card, 'toughness'):
            card.power -= count
            card.toughness -= count
            
        logging.debug(f"Added {count} {counter_type} counters to {card.name}")
        return True
    

    def _parse_activated_abilities(self, card_id, card, oracle_text, abilities_list):
        """Parse activated abilities from card text using enhanced patterns"""
        # Define more comprehensive patterns for activated abilities
        activated_patterns = [
            # Standard "Cost: Effect" pattern with named capture groups
            (r'(?P<cost>[^:]+?):\s+(?P<effect>[^\.;]+)', 
            lambda m: {"cost": m.group("cost").strip(), "effect": m.group("effect").strip()}),
            
            # Handle loyalty abilities like "+1: Do something"
            (r'(?P<loyalty>[+\-][\d]+):\s+(?P<effect>[^\.;]+)', 
            lambda m: {"cost": m.group("loyalty").strip(), "effect": m.group("effect").strip(), "loyalty_ability": True}),
            
            # Handle tap/untap symbol in costs with other potential costs
            (r'(?P<tap>\{[T]\}|\{[Q]\})(?P<additional_cost>[^:]*?):\s+(?P<effect>[^\.;]+)', 
            lambda m: {"cost": (m.group("tap") + (m.group("additional_cost") if m.group("additional_cost") else "")).strip(), 
                    "effect": m.group("effect").strip()})
        ]
        
        # Parse and add abilities
        self._parse_text_with_patterns(
            oracle_text, activated_patterns, "activated", card_id, card, abilities_list)
    
    def _parse_static_abilities(self, card_id, card, oracle_text, abilities_list):
        """Parse static abilities from card text with improved pattern matching"""
        # Define enhanced patterns for static abilities with functions to extract data
        static_patterns = [
            # "Creatures you control get +X/+Y" pattern
            (r'(?P<target>creatures\s+you\s+control)\s+get\s+(?P<bonus>[+\-][\d]+/[+\-][\d]+)', 
            lambda m: {'power_boost': int(m.group("bonus").split('/')[0]), 
                    'toughness_boost': int(m.group("bonus").split('/')[1]), 
                    'affected_type': m.group("target")}),
            
            # "X creatures get -A/-B" pattern
            (r'(?P<target>[^\.;]+?creatures[^\.;]+?)\s+get\s+(?P<penalty>[+\-][\d]+/[+\-][\d]+)', 
            lambda m: {'power_penalty': int(m.group("penalty").split('/')[0]), 
                    'toughness_penalty': int(m.group("penalty").split('/')[1]), 
                    'affected_type': m.group("target")}),
            
            # "While this door is locked/unlocked" pattern
            (r'while\s+this\s+door\s+is\s+(?P<state>locked|unlocked)', 
            lambda m: {'door_state': m.group("state")}),
            
            # "X spells cost {Y} less" pattern
            (r'(?P<spell_type>[^\.;]+?)\s+spells\s+(?:you\s+cast\s+)?cost\s+\{(?P<amount>[\d]+)\}\s+less', 
            lambda m: {'spell_type': m.group("spell_type").strip(), 'cost_reduction': int(m.group("amount"))}),
            
            # "Creatures can't attack" pattern
            (r'(?P<entity>creatures)(?P<qualifier>[^\.;]*?)\s+can\'t\s+(?P<restriction>attack)', 
            lambda m: {'prevent_attack': True, 'entity': m.group("entity"), 'qualifier': m.group("qualifier").strip()}),
            
            # "You can cast X spells as though they had flash" pattern
            (r'you\s+can\s+cast\s+(?P<spell_type>[^\.;]+?)\s+(?:spells\s+)?as\s+though\s+(?:it|they)\s+had\s+flash', 
            lambda m: {'spell_type_with_flash': m.group("spell_type").strip()}),
            
            # "X creatures you control have Y" pattern
            (r'(?P<creature_type>[^\.;]+?)\s+creatures\s+you\s+control\s+have\s+(?P<ability>[^\.;]+)', 
            lambda m: {'creature_type': m.group("creature_type").strip(), 'granted_ability': m.group("ability").strip()})
        ]
        
        # Parse and add abilities
        self._parse_text_with_patterns(
            oracle_text, static_patterns, "static", card_id, card, abilities_list)
    
    def check_abilities(self, card_id, event_type, context=None):
        try:
            gs = self.game_state
            card = gs._safe_get_card(card_id)
            if not card:
                return []
            
            # Find which player owns this card
            owner = None
            for player in [gs.p1, gs.p2]:
                for zone in ["battlefield", "graveyard", "hand", "exile"]:
                    if zone in player and card_id in player[zone]:
                        owner = player
                        break
                if owner:
                    break
            
            if not owner:
                logging.warning(f"Could not find owner for card {card_id}")
                return []
            
            # Expanded mapping of event types to relevant keywords/abilities
            event_to_keywords = {
                # Existing mappings
                "DEALS_DAMAGE": ["lifelink", "deathtouch", "infect", "wither", "enrage"],
                "ATTACKS": ["prowess", "battle cry", "exalted", "mentor", "myriad", "raid", "annihilator"],
                "BLOCKS": ["bushido", "banding", "flanking"],
                "DIES": ["persist", "undying", "afterlife", "haunt"],
                "ENTERS_BATTLEFIELD": ["saga", "fabricate", "riot", "modular", "evolve"],
                "CAST_SPELL": ["storm", "cascade", "prowess", "cipher"], 
                "UPKEEP": ["cumulative upkeep", "phasing", "echo", "fading", "vanishing"],
                "END_STEP": ["unearth", "dash", "blitz", "madness"],
                
                # New mappings
                "DRAW_CARD": ["madness", "miracle"],
                "DISCARD": ["madness", "hellbent"],
                "GAIN_LIFE": ["ajani's pridemate", "well of lost dreams"],
                "LOSE_LIFE": ["spectacle", "bloodthirst"],
                "ROOM_COMPLETED": ["room", "explore", "dungeon"],
                "DOOR_UNLOCKED": ["door", "room"],
                "CLASS_LEVEL_UP": ["class", "level"],
            }
            
            triggered_abilities = []
            
            # First check card.keywords (more efficient)
            if hasattr(card, 'keywords') and isinstance(card.keywords, list):
                keyword_map = {
                    0: "flying",
                    1: "trample", 
                    2: "hexproof",
                    3: "lifelink",
                    4: "deathtouch",
                    5: "first strike",
                    6: "double strike",
                    7: "vigilance",
                    8: "flash",
                    9: "haste",
                    10: "menace"
                }
                
                # Get relevant keywords for this event type
                relevant_keywords = []
                for evt_type, keywords in event_to_keywords.items():
                    if evt_type == event_type:
                        relevant_keywords.extend(keywords)
                
                # Check each keyword flag that's set
                for idx, has_keyword in enumerate(card.keywords):
                    if has_keyword and idx in keyword_map:
                        keyword = keyword_map[idx]
                        # Only process if relevant for this event
                        if keyword in relevant_keywords:
                            handler = self.keyword_handlers.get(keyword)
                            if handler:
                                result = handler(card_id, event_type, context)
                                if result and not isinstance(result, bool):
                                    triggered_abilities.append(result)
            
            # Check registered abilities with expanded context
            card_abilities = self.registered_abilities.get(card_id, [])
            for ability in card_abilities:
                if isinstance(ability, TriggeredAbility):
                    # Add more context information to help with conditional triggers
                    if context is None:
                        context = {}
                    
                    # Add card info to context
                    if 'card' not in context:
                        context['card'] = card
                    
                    # Add game_state to context for conditions that need it
                    if 'game_state' not in context:
                        context['game_state'] = gs
                    
                    # Add event_type to context
                    context['event_type'] = event_type
                    
                    if ability.can_trigger(event_type, context):
                        triggered_abilities.append(ability)
                        logging.debug(f"Ability triggered for {card.name}: {ability.effect_text}")
            
            # Queue these abilities to be put on the stack
            for ability in triggered_abilities:
                self.active_triggers.append((ability, owner))
                
            return triggered_abilities
        
        except Exception as e:
            logging.error(f"Error checking abilities for card {card_id}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return []

    def get_activated_abilities(self, card_id):
        """Get all activated abilities for a given card"""
        card_abilities = self.registered_abilities.get(card_id, [])
        return [ability for ability in card_abilities if isinstance(ability, ActivatedAbility)]
    
    def can_activate_ability(self, card_id, ability_index, controller):
        """Check if a specific activated ability can be activated"""
        activated_abilities = self.get_activated_abilities(card_id)
        if 0 <= ability_index < len(activated_abilities):
            ability = activated_abilities[ability_index]
            
            if hasattr(self.game_state, 'mana_system') and self.game_state.mana_system:
                # Use enhanced mana system if available
                cost = ability.cost
                parsed_cost = self.game_state.mana_system.parse_mana_cost(cost)
                return self.game_state.mana_system.can_pay_mana_cost(controller, parsed_cost)
            else:
                # Fallback to basic check
                return ability.can_activate(self.game_state, controller)
                
        return False

    def activate_ability(self, card_id, ability_index, controller):
        """Activate a specific activated ability"""
        activated_abilities = self.get_activated_abilities(card_id)
        if 0 <= ability_index < len(activated_abilities):
            ability = activated_abilities[ability_index]
            
            if hasattr(self.game_state, 'mana_system') and self.game_state.mana_system:
                # Use enhanced mana system if available
                cost = ability.cost
                parsed_cost = self.game_state.mana_system.parse_mana_cost(cost)
                can_pay = self.game_state.mana_system.can_pay_mana_cost(controller, parsed_cost)
                
                if can_pay:
                    # Pay cost
                    paid = self.game_state.mana_system.pay_mana_cost(controller, parsed_cost)
                    if paid:
                        # Add to stack
                        self.game_state.add_to_stack("ABILITY", card_id, controller)
                        logging.debug(f"Activated ability {ability_index} for {self.game_state._safe_get_card(card_id).name}")
                        return True
            else:
                # Fallback to basic activation
                if ability.can_activate(self.game_state, controller):
                    cost_paid = ability.pay_cost(self.game_state, controller)
                    if cost_paid:
                        # Add to stack
                        self.game_state.add_to_stack("ABILITY", card_id, controller)
                        logging.debug(f"Activated ability {ability_index} for {self.game_state._safe_get_card(card_id).name}")
                        return True
                        
        return False
    
    def process_triggered_abilities(self):
        """Process all pending triggered abilities, adding them to the stack in APNAP order."""
        gs = self.game_state
        if not self.active_triggers:
            return
            
        # Sort triggers by APNAP order
        ap_triggers = []
        nap_triggers = []
        active_player = gs._get_active_player()
        
        for ability, controller in self.active_triggers:
            if controller == active_player:
                ap_triggers.append((ability, controller))
            else:
                nap_triggers.append((ability, controller))
        
        # Process Active Player's triggers first
        for ability, controller in ap_triggers:
            # Check if ability is a valid object with card_id attribute
            if not ability or not hasattr(ability, 'card_id'):
                logging.warning("Skipping invalid ability in active_triggers")
                continue
                
            # Add context information
            context = {
                "ability": ability,
                "trigger_type": ability.trigger_condition if hasattr(ability, 'trigger_condition') else "",
                "effect_text": ability.effect_text,
                "source_id": ability.card_id
            }
            
            gs.add_to_stack("ABILITY", ability.card_id, controller, context)
            logging.debug(f"Added AP triggered ability to stack: {ability.effect_text}")
        
        # Then process Non-Active Player's triggers
        for ability, controller in nap_triggers:
            # Check if ability is a valid object with card_id attribute
            if not ability or not hasattr(ability, 'card_id'):
                logging.warning("Skipping invalid ability in active_triggers")
                continue
                
            # Add context information
            context = {
                "ability": ability,
                "trigger_type": ability.trigger_condition if hasattr(ability, 'trigger_condition') else "",
                "effect_text": ability.effect_text,
                "source_id": ability.card_id
            }
            
            gs.add_to_stack("ABILITY", ability.card_id, controller, context)
            logging.debug(f"Added NAP triggered ability to stack: {ability.effect_text}")
        
        # Clear active triggers
        self.active_triggers = []
    
    def resolve_ability(self, ability_type, card_id, controller, context=None):
        """
        Resolve an ability that's resolving from the stack.
        
        Args:
            ability_type: Type of ability (ACTIVATED, TRIGGERED, etc.)
            card_id: ID of the card with the ability
            controller: Player controlling the ability
            context: Additional context about the ability
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            logging.warning(f"Cannot resolve ability: card {card_id} not found")
            return
            
        # If context contains an actual ability object, use it directly
        if context and "ability" in context and isinstance(context["ability"], Ability):
            ability = context["ability"]
            if hasattr(ability, 'resolve'):
                try:
                    # Resolve with targets if they exist
                    targets = context.get("targets")
                    if hasattr(ability, 'resolve_with_targets') and targets:
                        ability.resolve_with_targets(gs, controller, targets)
                    else:
                        ability.resolve(gs, controller)
                        
                    logging.debug(f"Resolved ability for {card.name}: {ability.effect_text}")
                    return
                except Exception as e:
                    logging.error(f"Error resolving ability: {str(e)}")
                    import traceback
                    logging.error(traceback.format_exc())
                    return
        
        # Otherwise, find the ability based on type and index
        ability = None
        ability_index = context.get("ability_index", 0) if context else 0
        
        if ability_type == "ACTIVATED":
            activated_abilities = self.get_activated_abilities(card_id)
            if activated_abilities and 0 <= ability_index < len(activated_abilities):
                ability = activated_abilities[ability_index]
        elif ability_type == "TRIGGERED":
            # Find a matching triggered ability from context
            effect_text = context.get("effect_text", "") if context else ""
            card_abilities = self.registered_abilities.get(card_id, [])
            
            for a in card_abilities:
                if isinstance(a, TriggeredAbility) and a.effect_text == effect_text:
                    ability = a
                    break
                    
        # If we found an ability, resolve it
        if ability:
            try:
                # Get targets if any
                targets = context.get("targets") if context else None
                
                # Resolve with targets if they exist
                if hasattr(ability, 'resolve_with_targets') and targets:
                    ability.resolve_with_targets(gs, controller, targets)
                else:
                    ability.resolve(gs, controller)
                    
                logging.debug(f"Resolved ability for {card.name}: {ability.effect_text}")
            except Exception as e:
                logging.error(f"Error resolving ability: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
        else:
            logging.warning(f"No ability found to resolve for {card.name}")
            
    def handle_attack_triggers(self, attacker_id):
        """Handle triggered abilities that trigger when a creature attacks."""
        gs = self.game_state
        card = gs._safe_get_card(attacker_id)
        if not card:
            return
            
        # Find controller
        controller = None
        for player in [gs.p1, gs.p2]:
            if attacker_id in player["battlefield"]:
                controller = player
                break
                
        if not controller:
            return
        
        # First check abilities on the attacking creature itself
        if hasattr(card, 'oracle_text'):
            oracle_text = card.oracle_text.lower()
            
            # Check for "when/whenever this creature attacks" triggers
            if "when this creature attacks" in oracle_text or "whenever this creature attacks" in oracle_text:
                # Parse the ability and add to triggered abilities
                context = {"attacker_id": attacker_id, "controller": controller}
                self.check_abilities(attacker_id, "ATTACKS", context)
                
        # Now check for abilities that trigger on any creature attacking
        for permanent_id in controller["battlefield"]:
            if permanent_id == attacker_id:
                continue  # Already checked
                
            permanent = gs._safe_get_card(permanent_id)
            if not permanent or not hasattr(permanent, 'oracle_text'):
                continue
                
            oracle_text = permanent.oracle_text.lower()
            
            # Check for "when/whenever a creature attacks" triggers
            if "when a creature attacks" in oracle_text or "whenever a creature attacks" in oracle_text:
                # Parse the ability and add to triggered abilities
                context = {"attacker_id": attacker_id, "controller": controller}
                self.check_abilities(permanent_id, "CREATURE_ATTACKS", context)
                
        # Check opponent's triggers for "when/whenever a creature attacks you"
        opponent = gs.p2 if controller == gs.p1 else gs.p1
        for permanent_id in opponent["battlefield"]:
            permanent = gs._safe_get_card(permanent_id)
            if not permanent or not hasattr(permanent, 'oracle_text'):
                continue
                
            oracle_text = permanent.oracle_text.lower()
            
            # Check for "when/whenever a creature attacks you" triggers
            if "when a creature attacks you" in oracle_text or "whenever a creature attacks you" in oracle_text:
                # Parse the ability and add to triggered abilities
                context = {"attacker_id": attacker_id, "controller": opponent}
                self.check_abilities(permanent_id, "CREATURE_ATTACKS_YOU", context)

class TargetingSystem:
    """
    Enhanced system for handling targeting in Magic: The Gathering.
    Supports comprehensive restrictions, protection effects, and validates targets.
    """
    
    def __init__(self, game_state):
        self.game_state = game_state
    
    def get_valid_targets(self, card_id, controller, target_type=None):
        """
        Returns a list of valid targets for a card, based on its text and target type.
        
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
            "creatures": [],
            "players": [],
            "permanents": [],
            "spells": [],
            "lands": [],
            "artifacts": [],
            "enchantments": [],
            "planeswalkers": [],
            "graveyard": [],
            "exile": [],
            "library": [],
            "other": []
        }
        
        # Parse targeting requirements from the oracle text
        target_requirements = self._parse_targeting_requirements(oracle_text)
        
        # If a specific target type is requested, only check that type
        if target_type:
            if target_type not in valid_targets:
                return {}
            valid_targets = {target_type: []}
            target_requirements = [req for req in target_requirements if req.get("type") == target_type]
        
        # Fill valid targets based on requirements
        for requirement in target_requirements:
            req_type = requirement.get("type", "other")
            
            if req_type == "creature":
                # Get all creatures on battlefield
                for player in [gs.p1, gs.p2]:
                    for target_id in player["battlefield"]:
                        if self._is_valid_creature_target(card_id, target_id, controller, player, requirement):
                            valid_targets["creatures"].append(target_id)
            
            elif req_type == "player":
                # Players can be targeted
                if requirement.get("opponent_only"):
                    valid_targets["players"].append("p2" if controller == gs.p1 else "p1")
                elif requirement.get("controller_only"):
                    valid_targets["players"].append("p1" if controller == gs.p1 else "p2")
                else:
                    valid_targets["players"].extend(["p1", "p2"])
                    
            elif req_type == "permanent":
                # Get all permanents on battlefield
                for player in [gs.p1, gs.p2]:
                    for target_id in player["battlefield"]:
                        if self._is_valid_permanent_target(card_id, target_id, controller, player, requirement):
                            valid_targets["permanents"].append(target_id)
            
            elif req_type == "spell":
                # Get all spells on the stack
                for stack_item in gs.stack:
                    if isinstance(stack_item, tuple) and len(stack_item) >= 3:
                        spell_type, spell_id, spell_caster = stack_item[:3]
                        if spell_type == "SPELL":
                            if self._is_valid_spell_target(card_id, spell_id, controller, spell_caster, requirement):
                                valid_targets["spells"].append(spell_id)
            
            elif req_type == "land":
                # Get all lands on battlefield
                for player in [gs.p1, gs.p2]:
                    for target_id in player["battlefield"]:
                        if self._is_valid_land_target(card_id, target_id, controller, player, requirement):
                            valid_targets["lands"].append(target_id)
            
            elif req_type == "artifact":
                # Get all artifacts on battlefield
                for player in [gs.p1, gs.p2]:
                    for target_id in player["battlefield"]:
                        if self._is_valid_artifact_target(card_id, target_id, controller, player, requirement):
                            valid_targets["artifacts"].append(target_id)
            
            elif req_type == "enchantment":
                # Get all enchantments on battlefield
                for player in [gs.p1, gs.p2]:
                    for target_id in player["battlefield"]:
                        if self._is_valid_enchantment_target(card_id, target_id, controller, player, requirement):
                            valid_targets["enchantments"].append(target_id)
            
            elif req_type == "planeswalker":
                # Get all planeswalkers on battlefield
                for player in [gs.p1, gs.p2]:
                    for target_id in player["battlefield"]:
                        if self._is_valid_planeswalker_target(card_id, target_id, controller, player, requirement):
                            valid_targets["planeswalkers"].append(target_id)
            
            elif req_type == "graveyard":
                # Get valid cards from graveyards
                for player in [gs.p1, gs.p2]:
                    for target_id in player["graveyard"]:
                        if self._is_valid_graveyard_target(card_id, target_id, controller, player, requirement):
                            valid_targets["graveyard"].append(target_id)
                            
            elif req_type == "exile":
                # Get valid cards from exile
                for player in [gs.p1, gs.p2]:
                    for target_id in player["exile"]:
                        if self._is_valid_exile_target(card_id, target_id, controller, player, requirement):
                            valid_targets["exile"].append(target_id)
        
        return valid_targets
    
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
                  # TODO: Add player protection checks (e.g., Leyline of Sanctity)
                  pass
             elif isinstance(target_obj, Card): # Permanent or Spell
                 # Protection
                 if self._has_protection_from(target_obj, source_card, target_owner, caster): return False
                 # Hexproof (if targeted by opponent)
                 if caster != target_owner and self._has_hexproof(target_obj): return False
                 # Shroud (if targeted by anyone)
                 if self._has_shroud(target_obj): return False
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
                 if "counter target spell" in getattr(source_card, 'oracle_text', '').lower():
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
    
    def _is_valid_creature_target(self, source_id, target_id, caster, target_owner, requirements):
        """Check if a creature is a valid target based on requirements."""
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        target_card = gs._safe_get_card(target_id)
        
        if not target_card or not hasattr(target_card, 'card_types'):
            return False
            
        # Check if it's a creature
        if 'creature' not in target_card.card_types:
            return False
            
        # Check controller restrictions
        if requirements.get("controller_is_caster") and target_owner != caster:
            return False
            
        if requirements.get("controller_is_opponent") and target_owner == caster:
            return False
            
        # Check power restrictions
        if "power_restriction" in requirements:
            restriction = requirements["power_restriction"]
            if not hasattr(target_card, 'power'):
                return False
                
            if restriction["comparison"] == "greater":
                if target_card.power < restriction["value"]:
                    return False
            elif restriction["comparison"] == "less":
                if target_card.power > restriction["value"]:
                    return False
                    
        # Check toughness restrictions
        if "toughness_restriction" in requirements:
            restriction = requirements["toughness_restriction"]
            if not hasattr(target_card, 'toughness'):
                return False
                
            if restriction["comparison"] == "greater":
                if target_card.toughness < restriction["value"]:
                    return False
            elif restriction["comparison"] == "less":
                if target_card.toughness > restriction["value"]:
                    return False
        
        # Check color restrictions
        if "color_restriction" in requirements:
            color = requirements["color_restriction"]
            color_index = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
            
            if not hasattr(target_card, 'colors') or not target_card.colors[color_index[color]]:
                return False
        
        # Check tapped/untapped status
        if requirements.get("must_be_tapped") and target_id not in target_owner.get("tapped_permanents", set()):
            return False
            
        if requirements.get("must_be_untapped") and target_id in target_owner.get("tapped_permanents", set()):
            return False
            
        # Check attacking/blocking status
        if requirements.get("must_be_attacking") and (not hasattr(gs, 'current_attackers') or target_id not in gs.current_attackers):
            return False
            
        if requirements.get("must_be_blocking"):
            is_blocking = False
            if hasattr(gs, 'current_block_assignments'):
                for blockers in gs.current_block_assignments.values():
                    if target_id in blockers:
                        is_blocking = True
                        break
            if not is_blocking:
                return False
        
        # Check for protection against the source
        if self._has_protection_from(target_card, source_card, target_owner, caster):
            return False
            
        # Check for hexproof
        if target_owner != caster and self._has_hexproof(target_card):
            return False
            
        # Check for shroud
        if self._has_shroud(target_card):
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _is_valid_permanent_target(self, source_id, target_id, caster, target_owner, requirements):
        """Check if a permanent is a valid target based on requirements."""
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        target_card = gs._safe_get_card(target_id)
        
        if not target_card:
            return False
        
        # Check if this is a permanent (on the battlefield)
        if target_id not in target_owner["battlefield"]:
            return False
            
        # Check for land exclusion
        if requirements.get("exclude_land") and hasattr(target_card, 'card_types') and 'land' in target_card.card_types:
            return False
            
        # Check controller restrictions
        if requirements.get("controller_is_caster") and target_owner != caster:
            return False
            
        if requirements.get("controller_is_opponent") and target_owner == caster:
            return False
            
        # Check color restrictions
        if "color_restriction" in requirements:
            color = requirements["color_restriction"]
            color_index = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
            
            if not hasattr(target_card, 'colors') or not target_card.colors[color_index[color]]:
                return False
            
        # Check tapped/untapped status
        if requirements.get("must_be_tapped") and target_id not in target_owner.get("tapped_permanents", set()):
            return False
            
        if requirements.get("must_be_untapped") and target_id in target_owner.get("tapped_permanents", set()):
            return False
            
        # Check for protection against the source
        if self._has_protection_from(target_card, source_card, target_owner, caster):
            return False
            
        # Check for hexproof
        if target_owner != caster and self._has_hexproof(target_card):
            return False
            
        # Check for shroud
        if self._has_shroud(target_card):
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _is_valid_spell_target(self, source_id, target_id, caster, spell_caster, requirements):
        """Check if a spell is a valid target based on requirements."""
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        target_card = gs._safe_get_card(target_id)
        
        if not target_card or not hasattr(target_card, 'card_types'):
            return False
            
        # Check for "can't be countered"
        if hasattr(source_card, 'oracle_text') and "counter target spell" in source_card.oracle_text.lower():
            if hasattr(target_card, 'oracle_text') and "can't be countered" in target_card.oracle_text.lower():
                return False
            
        # Check spell type restrictions
        if "spell_type_restriction" in requirements:
            restriction = requirements["spell_type_restriction"]
            if restriction == "instant_or_sorcery" and 'instant' not in target_card.card_types and 'sorcery' not in target_card.card_types:
                return False
            elif restriction == "creature" and 'creature' not in target_card.card_types:
                return False
            elif restriction == "instant" and 'instant' not in target_card.card_types:
                return False
            elif restriction == "sorcery" and 'sorcery' not in target_card.card_types:
                return False
                
        # Check controller restrictions
        if requirements.get("controller_is_caster") and spell_caster != caster:
            return False
            
        if requirements.get("controller_is_opponent") and spell_caster == caster:
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _is_valid_land_target(self, source_id, target_id, caster, target_owner, requirements):
        """Check if a land is a valid target based on requirements."""
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        target_card = gs._safe_get_card(target_id)
        
        if not target_card or not hasattr(target_card, 'card_types'):
            return False
            
        # Check if it's a land
        if 'land' not in target_card.card_types:
            return False
            
        # Check basic/nonbasic restrictions
        if requirements.get("must_be_basic"):
            land_type = target_card.type_line.lower() if hasattr(target_card, 'type_line') else ""
            if "basic" not in land_type:
                return False
                
        if requirements.get("must_be_nonbasic"):
            land_type = target_card.type_line.lower() if hasattr(target_card, 'type_line') else ""
            if "basic" in land_type:
                return False
            
        # Check controller restrictions
        if requirements.get("controller_is_caster") and target_owner != caster:
            return False
            
        if requirements.get("controller_is_opponent") and target_owner == caster:
            return False
            
        # Check for protection against the source
        if self._has_protection_from(target_card, source_card, target_owner, caster):
            return False
            
        # Check for hexproof
        if target_owner != caster and self._has_hexproof(target_card):
            return False
            
        # Check for shroud
        if self._has_shroud(target_card):
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _is_valid_artifact_target(self, source_id, target_id, caster, target_owner, requirements):
        """Check if an artifact is a valid target based on requirements."""
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        target_card = gs._safe_get_card(target_id)
        
        if not target_card or not hasattr(target_card, 'card_types'):
            return False
            
        # Check if it's an artifact
        if 'artifact' not in target_card.card_types:
            return False
            
        # Check must be creature requirement
        if requirements.get("must_be_creature") and 'creature' not in target_card.card_types:
            return False
            
        # Check controller restrictions
        if requirements.get("controller_is_caster") and target_owner != caster:
            return False
            
        if requirements.get("controller_is_opponent") and target_owner == caster:
            return False
            
        # Check for protection against the source
        if self._has_protection_from(target_card, source_card, target_owner, caster):
            return False
            
        # Check for hexproof
        if target_owner != caster and self._has_hexproof(target_card):
            return False
            
        # Check for shroud
        if self._has_shroud(target_card):
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _is_valid_enchantment_target(self, source_id, target_id, caster, target_owner, requirements):
        """Check if an enchantment is a valid target based on requirements."""
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        target_card = gs._safe_get_card(target_id)
        
        if not target_card or not hasattr(target_card, 'card_types'):
            return False
            
        # Check if it's an enchantment
        if 'enchantment' not in target_card.card_types:
            return False
            
        # Check must be aura requirement
        if requirements.get("must_be_aura") and (not hasattr(target_card, 'subtypes') or 'aura' not in target_card.subtypes):
            return False
            
        # Check controller restrictions
        if requirements.get("controller_is_caster") and target_owner != caster:
            return False
            
        if requirements.get("controller_is_opponent") and target_owner == caster:
            return False
            
        # Check for protection against the source
        if self._has_protection_from(target_card, source_card, target_owner, caster):
            return False
            
        # Check for hexproof
        if target_owner != caster and self._has_hexproof(target_card):
            return False
            
        # Check for shroud
        if self._has_shroud(target_card):
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _is_valid_planeswalker_target(self, source_id, target_id, caster, target_owner, requirements):
        """Check if a planeswalker is a valid target based on requirements."""
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        target_card = gs._safe_get_card(target_id)
        
        if not target_card or not hasattr(target_card, 'card_types'):
            return False
            
        # Check if it's a planeswalker
        if 'planeswalker' not in target_card.card_types:
            return False
            
        # Check controller restrictions
        if requirements.get("controller_is_caster") and target_owner != caster:
            return False
            
        if requirements.get("controller_is_opponent") and target_owner == caster:
            return False
            
        # Check for protection against the source
        if self._has_protection_from(target_card, source_card, target_owner, caster):
            return False
            
        # Check for hexproof
        if target_owner != caster and self._has_hexproof(target_card):
            return False
            
        # Check for shroud
        if self._has_shroud(target_card):
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _is_valid_graveyard_target(self, source_id, target_id, caster, target_owner, requirements):
        """Check if a card in a graveyard is a valid target based on requirements."""
        gs = self.game_state
        target_card = gs._safe_get_card(target_id)
        
        if not target_card:
            return False
            
        # Check card type restrictions
        if "card_type_restriction" in requirements:
            restriction = requirements["card_type_restriction"]
            if restriction == "creature" and (not hasattr(target_card, 'card_types') or 'creature' not in target_card.card_types):
                return False
            elif restriction == "instant_or_sorcery" and (not hasattr(target_card, 'card_types') or 
                                                          ('instant' not in target_card.card_types and 
                                                           'sorcery' not in target_card.card_types)):
                return False
                
        # Check owner restrictions
        if requirements.get("owner_is_caster") and target_owner != caster:
            return False
            
        if requirements.get("owner_is_opponent") and target_owner == caster:
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _is_valid_exile_target(self, source_id, target_id, caster, target_owner, requirements):
        """Check if a card in exile is a valid target based on requirements."""
        gs = self.game_state
        target_card = gs._safe_get_card(target_id)
        
        if not target_card:
            return False
            
        # Check face-down requirement
        if requirements.get("must_be_face_down"):
            # In a real implementation, we'd check if the card is face down
            # For now, assume all exile cards are face-up unless otherwise tracked
            is_face_down = hasattr(gs, 'face_down_cards') and target_id in gs.face_down_cards.get('exile', [])
            if not is_face_down:
                return False
            
        # Check card type restrictions
        if "card_type_restriction" in requirements:
            restriction = requirements["card_type_restriction"]
            if restriction == "creature" and (not hasattr(target_card, 'card_types') or 'creature' not in target_card.card_types):
                return False
            elif restriction == "instant_or_sorcery" and (not hasattr(target_card, 'card_types') or 
                                                          ('instant' not in target_card.card_types and 
                                                           'sorcery' not in target_card.card_types)):
                return False
                
        # Check owner restrictions
        if requirements.get("owner_is_caster") and target_owner != caster:
            return False
            
        if requirements.get("owner_is_opponent") and target_owner == caster:
            return False
            
        # All checks passed, this is a valid target
        return True
    
    def _has_protection_from(self, target_card, source_card, target_owner, source_controller):
        """
        Comprehensive check if target has protection from source.
        Protection prevents DEBT: Damage, Enchanting/Equipping, Blocking, Targeting
        """
        if not target_card or not hasattr(target_card, 'oracle_text'):
            return False
            
        if not source_card:
            return False
            
        oracle_text = target_card.oracle_text.lower()
        
        # Check for protection from everything
        if "protection from everything" in oracle_text:
            return True
            
        # Check for protection from colors
        if hasattr(source_card, 'colors'):
            if "protection from white" in oracle_text and source_card.colors[0]:
                return True
            if "protection from blue" in oracle_text and source_card.colors[1]:
                return True
            if "protection from black" in oracle_text and source_card.colors[2]:
                return True
            if "protection from red" in oracle_text and source_card.colors[3]:
                return True
            if "protection from green" in oracle_text and source_card.colors[4]:
                return True
            if "protection from all colors" in oracle_text and any(source_card.colors):
                return True
            if "protection from multicolored" in oracle_text and sum(source_card.colors) > 1:
                return True
            if "protection from monocolored" in oracle_text and sum(source_card.colors) == 1:
                return True
            if "protection from colorless" in oracle_text and sum(source_card.colors) == 0:
                return True
            
        # Check for protection from card types
        if hasattr(source_card, 'card_types'):
            if "protection from creatures" in oracle_text and 'creature' in source_card.card_types:
                return True
            if "protection from artifacts" in oracle_text and 'artifact' in source_card.card_types:
                return True
            if "protection from enchantments" in oracle_text and 'enchantment' in source_card.card_types:
                return True
            if "protection from planeswalkers" in oracle_text and 'planeswalker' in source_card.card_types:
                return True
            
        # Check for protection from specific subtypes
        if hasattr(source_card, 'subtypes') and hasattr(target_card, 'oracle_text'):
            for subtype in source_card.subtypes:
                if f"protection from {subtype.lower()}" in target_card.oracle_text.lower():
                    return True
        
        # Check for protection from players (rare)
        if target_owner != source_controller and "protection from opponent" in oracle_text:
            return True
            
        return False
    
    def _has_hexproof(self, card):
        """Check if card has hexproof or conditional hexproof."""
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        oracle_text = card.oracle_text.lower()
        
        # Check for standard hexproof
        if "hexproof" in oracle_text:
            # Check for conditional hexproof
            if "hexproof from" in oracle_text:
                # Common conditional hexproof variants
                if "hexproof from white" in oracle_text:
                    # Would need to check source color here
                    return False  # For now, assume source doesn't match
                if "hexproof from blue" in oracle_text:
                    return False
                if "hexproof from black" in oracle_text:
                    return False
                if "hexproof from red" in oracle_text:
                    return False
                if "hexproof from green" in oracle_text:
                    return False
                if "hexproof from multicolored" in oracle_text:
                    return False
            else:
                # Standard hexproof prevents all opponent targeting
                return True
            
        return False
    
    def _has_shroud(self, card):
        """Check if card has shroud."""
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        return "shroud" in card.oracle_text.lower()
    
    def resolve_targeting(self, source_id, controller, effect_text=None, target_types=None):
        """
        Unified method to resolve targeting for both spells and abilities.
        
        Args:
            source_id: ID of the spell or ability source
            controller: Player who controls the source
            effect_text: Text of the effect requiring targets
            target_types: Specific types of targets to find
            
        Returns:
            dict: Selected targets or None if targeting failed
        """
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        if not source_card:
            return None
            
        # Use effect_text if provided, otherwise try to get it from the card
        text_to_parse = effect_text
        if not text_to_parse and hasattr(source_card, 'oracle_text'):
            text_to_parse = source_card.oracle_text
            
        if not text_to_parse:
            return None
            
        # Get valid targets
        valid_targets = self.get_valid_targets(source_id, controller, target_types)
        
        # If no valid targets, targeting fails
        if not valid_targets or not any(valid_targets.values()):
            return None
        
        # Determine target requirements from the text
        target_requirements = self._parse_targeting_requirements(text_to_parse.lower())
        
        # Extract category names from the target requirements
        target_categories = [req.get("type", "other") for req in target_requirements]
        
        # Get AI selected targets based on effect type and context
        selected_targets = self._select_targets_by_strategy(
            source_card, valid_targets, len(target_requirements), target_categories, controller)
        
        if selected_targets and any(selected_targets.values()):
            return selected_targets
        else:
            return None
    
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
        valid_targets = self.get_valid_targets(card_id, controller)
        
        # Check if all targets are valid
        for category, target_list in targets.items():
            if category not in valid_targets:
                return False
                
            for target in target_list:
                if target not in valid_targets[category]:
                    return False
                    
        return True
    
    def _select_targets_by_strategy(self, card, valid_targets, target_count, target_categories, controller):
        """
        Select targets strategically based on card type and effect.
        
        Args:
            card: The card requiring targets
            valid_targets: Dictionary of valid targets by category
            target_count: Number of targets required
            target_categories: Categories of targets needed
            controller: Player controlling the effect
            
        Returns:
            dict: Selected targets by category
        """
        gs = self.game_state
        selected_targets = {}
        
        # Determine if this is a beneficial or harmful effect
        is_beneficial = self._is_beneficial_effect(card.oracle_text.lower() if hasattr(card, 'oracle_text') else "")
        
        # Get opponent
        opponent = gs.p2 if controller == gs.p1 else gs.p1
        
        # Select targets for each required category
        for category in target_categories:
            if category not in valid_targets or not valid_targets[category]:
                continue
                
            # How many targets we still need
            remaining_count = target_count - sum(len(targets) for targets in selected_targets.values())
            if remaining_count <= 0:
                break
                
            # Get targets for this category
            category_targets = valid_targets[category]
            selected_for_category = []
            
            # Strategy depends on category and whether effect is beneficial
            if category == "creatures":
                if is_beneficial:
                    # For beneficial effects, target own creatures
                    own_creatures = [cid for cid in category_targets if cid in controller["battlefield"]]
                    
                    # Sort by most valuable (highest power/toughness)
                    own_creatures.sort(
                        key=lambda cid: (
                            getattr(gs._safe_get_card(cid), 'power', 0) +
                            getattr(gs._safe_get_card(cid), 'toughness', 0)
                        ),
                        reverse=True
                    )
                    
                    selected_for_category = own_creatures[:remaining_count]
                else:
                    # For harmful effects, target opponent creatures
                    opp_creatures = [cid for cid in category_targets if cid in opponent["battlefield"]]
                    
                    # Sort by most threatening (highest power/toughness)
                    opp_creatures.sort(
                        key=lambda cid: (
                            getattr(gs._safe_get_card(cid), 'power', 0) +
                            getattr(gs._safe_get_card(cid), 'toughness', 0)
                        ),
                        reverse=True
                    )
                    
                    selected_for_category = opp_creatures[:remaining_count]
                    
            elif category == "players":
                if is_beneficial:
                    # Target self for beneficial effects
                    if "p1" in category_targets and controller == gs.p1:
                        selected_for_category = ["p1"]
                    elif "p2" in category_targets and controller == gs.p2:
                        selected_for_category = ["p2"]
                else:
                    # Target opponent for harmful effects
                    if "p1" in category_targets and controller != gs.p1:
                        selected_for_category = ["p1"]
                    elif "p2" in category_targets and controller != gs.p2:
                        selected_for_category = ["p2"]
            
            elif category == "spells":
                # For counterspells, target opponent's spells
                opponent_spells = []
                for spell_id in category_targets:
                    for item in gs.stack:
                        if isinstance(item, tuple) and len(item) >= 3:
                            _, stack_id, spell_controller = item[:3]
                            if stack_id == spell_id and spell_controller != controller:
                                opponent_spells.append(spell_id)
                
                # Take the most recently cast spell (top of stack)
                if opponent_spells:
                    selected_for_category = [opponent_spells[0]]
            
            # Handle other categories similarly...
            elif category in ["artifacts", "enchantments", "lands", "planeswalkers", "permanents"]:
                if is_beneficial:
                    own_permanents = [cid for cid in category_targets if cid in controller["battlefield"]]
                    selected_for_category = own_permanents[:remaining_count]
                else:
                    opp_permanents = [cid for cid in category_targets if cid in opponent["battlefield"]]
                    selected_for_category = opp_permanents[:remaining_count]
            
            elif category == "graveyard":
                # For graveyard targeting, choose your own cards for beneficial effects
                if is_beneficial:
                    own_graveyard_cards = [cid for cid in category_targets if cid in controller["graveyard"]]
                    selected_for_category = own_graveyard_cards[:remaining_count]
                else:
                    opp_graveyard_cards = [cid for cid in category_targets if cid in opponent["graveyard"]]
                    selected_for_category = opp_graveyard_cards[:remaining_count]
            
            elif category == "exile":
                # Similar logic for exile zone
                own_exile_cards = [cid for cid in category_targets if cid in controller["exile"]]
                selected_for_category = own_exile_cards[:remaining_count]
                
            # Fallback for any other category - just take the first valid targets
            else:
                selected_for_category = category_targets[:remaining_count]
            
            # Add selected targets to result
            if selected_for_category:
                selected_targets[category] = selected_for_category
        
        return selected_targets

    def _is_beneficial_effect(self, oracle_text):
        """Determine if an effect is beneficial to the target."""
        from .ability_utils import is_beneficial_effect
        return is_beneficial_effect(oracle_text)
    
    def check_can_be_blocked(self, attacker_id, blocker_id):
        """
        Check if an attacker can be blocked by this blocker considering all restrictions.
        
        Args:
            attacker_id: The attacking creature ID
            blocker_id: The potential blocker creature ID
            
        Returns:
            bool: Whether the blocker can legally block the attacker
        """
        gs = self.game_state
        attacker = gs._safe_get_card(attacker_id)
        blocker = gs._safe_get_card(blocker_id)
        
        if not attacker or not blocker:
            return False
            
        # Get controller info
        attacker_controller = None
        blocker_controller = None
        
        for player in [gs.p1, gs.p2]:
            if attacker_id in player["battlefield"]:
                attacker_controller = player
            if blocker_id in player["battlefield"]:
                blocker_controller = player
                
        if not attacker_controller or not blocker_controller:
            return False
        
        # Check if blocker is tapped
        if blocker_id in blocker_controller.get("tapped_permanents", set()):
            return False
            
        # Check for protection
        if hasattr(attacker, 'oracle_text') and hasattr(blocker, 'oracle_text'):
            # Check if attacker has protection from blocker
            if self._has_protection_from(attacker, blocker, attacker_controller, blocker_controller):
                return False
                
            # Check if blocker has protection from attacker
            if self._has_protection_from(blocker, attacker, blocker_controller, attacker_controller):
                return False
        
        # Check for "can't be blocked" abilities
        if hasattr(attacker, 'oracle_text'):
            attacker_text = attacker.oracle_text.lower()
            
            # Absolute unblockable
            if "can't be blocked" in attacker_text and "except" not in attacker_text:
                return False
                
            # Menace (can't be blocked except by two or more creatures)
            if "menace" in attacker_text or "can't be blocked except by two or more creatures" in attacker_text:
                # Check if there are already blockers for this attacker
                existing_blockers = 0
                for blockers_list in gs.current_block_assignments.values():
                    if attacker_id in gs.current_block_assignments:
                        existing_blockers = len(gs.current_block_assignments[attacker_id])
                    
                if existing_blockers == 0:
                    return False  # First blocker can't block alone with menace
            
            # Flying (can only be blocked by creatures with flying or reach)
            if "flying" in attacker_text:
                has_flying = "flying" in blocker.oracle_text.lower() if hasattr(blocker, 'oracle_text') else False
                has_reach = "reach" in blocker.oracle_text.lower() if hasattr(blocker, 'oracle_text') else False
                
                if not has_flying and not has_reach:
                    return False
            
            # Check for other specific "can't be blocked except by" restrictions
            if "can't be blocked except by" in attacker_text:
                can_block = False
                
                # Common variations
                if "can't be blocked except by artifacts" in attacker_text:
                    if hasattr(blocker, 'card_types') and 'artifact' in blocker.card_types:
                        can_block = True
                        
                elif "can't be blocked except by walls" in attacker_text:
                    if hasattr(blocker, 'subtypes') and 'wall' in [s.lower() for s in blocker.subtypes]:
                        can_block = True
                
                # Return false if none of the exceptions apply
                if not can_block:
                    return False
        
        # Check for specific blocker restrictions
        if hasattr(blocker, 'oracle_text'):
            blocker_text = blocker.oracle_text.lower()
            
            # "Can't block" ability
            if "can't block" in blocker_text:
                return False
            
            # "Can block only" restrictions
            if "can block only" in blocker_text:
                can_block = False
                
                # Common variations
                if "can block only creatures with flying" in blocker_text:
                    if hasattr(attacker, 'oracle_text') and "flying" in attacker.oracle_text.lower():
                        can_block = True
                
                # Return false if none of the exceptions apply
                if not can_block:
                    return False
        
        # All checks pass, this blocker can block this attacker
        return True
    
    def check_must_attack(self, card_id):
        """
        Check if a creature must attack this turn.
        
        Args:
            card_id: The creature ID to check
            
        Returns:
            bool: Whether the creature must attack if able
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        oracle_text = card.oracle_text.lower()
        
        # Check for "must attack each combat if able"
        if "must attack" in oracle_text and "if able" in oracle_text:
            return True
            
        # Check for "must attack [specific player/planeswalker] if able"
        if "must attack" in oracle_text and "if able" in oracle_text:
            # For simplicity, return true for any "must attack" restriction
            # In a full implementation, we would check specific attack requirements
            return True
            
        return False
    
    def check_must_block(self, card_id):
        """
        Check if a creature must block this turn.
        
        Args:
            card_id: The creature ID to check
            
        Returns:
            bool: Whether the creature must block if able
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        oracle_text = card.oracle_text.lower()
        
        # Check for "must block if able"
        if "must block" in oracle_text and "if able" in oracle_text:
            return True
            
        # Check for "must block [specific creature] if able"
        if "must block" in oracle_text and "if able" in oracle_text:
            # For simplicity, return true for any "must block" restriction
            # In a full implementation, we would check specific block requirements
            return True
            
        return False