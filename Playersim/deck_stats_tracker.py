import os
import json
import logging
import hashlib
import gzip
import asyncio
import time
import threading
import re
import glob
import tempfile
import copy
from collections import defaultdict, Counter
from typing import Dict, List, Any, Optional, Tuple, Union
from enum import Enum
import math

from .archetypes import (
    DeckStrategyProfile,
    classify_full_deck,
    deck_composition_hash,
    normalize_declared_profile,
)
# Version information for tracking schema changes
STATS_VERSION = "3.4.0"  # Canonical-ID per-deck card analytics


def _deck_seat_share(appearances: int, total_games: int) -> float:
    """Probability that a randomly selected deck seat has an appearance."""
    return appearances / (2 * total_games) if total_games else 0.0


def _opposing_position(position: "GamePosition") -> "GamePosition":
    """Translate a board assessment to the other player's perspective."""
    if position == GamePosition.AHEAD:
        return GamePosition.BEHIND
    if position == GamePosition.BEHIND:
        return GamePosition.AHEAD
    return GamePosition.PARITY


def _player_turn_number(global_turn: Any, went_first: Optional[bool]) -> Optional[int]:
    """Translate the engine's alternating turn into this seat's turn count."""
    if went_first is None:
        return None
    try:
        global_turn = int(global_turn)
    except (TypeError, ValueError, OverflowError):
        return None
    if global_turn <= 0:
        return None
    return ((global_turn + 1) // 2
            if went_first else global_turn // 2)


def _player_turn_history(history: Dict, went_first: Optional[bool]) -> Dict:
    """Re-key global-turn telemetry by turns received by one player."""
    if not isinstance(history, dict) or went_first is None:
        return {}
    normalized = {}
    for raw_turn, raw_cards in history.items():
        player_turn = _player_turn_number(raw_turn, went_first)
        if not player_turn or not isinstance(raw_cards, (list, tuple, set)):
            continue
        normalized.setdefault(player_turn, []).extend(list(raw_cards))
    return normalized


def _finite_card_number(card, attribute: str, default: float = 0.0) -> float:
    """Read a finite numeric card characteristic for aggregate analytics.

    Printed variable characteristics such as ``*`` are represented as
    ``None`` until rules context supplies a value. Deck-level archetype
    scoring has no such context, so unknown/non-finite P/T or mana value is a
    neutral zero rather than an exception or NaN contaminating every score.
    """
    try:
        value = float(getattr(card, attribute, default))
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return value if math.isfinite(value) else float(default)

# Game stage definitions
class GameStage(Enum):
    EARLY = "early"  # Turns 1-3
    MID = "mid"      # Turns 4-7
    LATE = "late"    # Turns 8+

# Board position definitions (renamed from GameState to avoid collision with game_state.GameState)
class GamePosition(Enum):
    AHEAD = "ahead"      # Winning position
    PARITY = "parity"    # Even position
    BEHIND = "behind"    # Losing position

# Format definitions
class GameFormat(Enum):
    STANDARD = "standard"
    PIONEER = "pioneer"
    MODERN = "modern"
    LEGACY = "legacy"
    VINTAGE = "vintage"
    COMMANDER = "commander"
    CUSTOM = "custom"

# Extended archetype definitions
class DeckArchetype(Enum):
    AGGRO = "aggro"
    CONTROL = "control"
    MIDRANGE = "midrange"
    COMBO = "combo"
    TEMPO = "tempo"
    RAMP = "ramp"
    BURN = "burn"
    TRIBAL = "tribal"
    REANIMATOR = "reanimator"
    MILL = "mill"
    TOKENS = "tokens"
    STOMPY = "stompy"
    VOLTRON = "voltron"
    STAX = "stax"
    PRISON = "prison"
    ARISTOCRATS = "aristocrats"
    LIFEGAIN = "lifegain"
    SPELLSLINGER = "spellslinger"
    LANDS = "lands"
    DISCARD = "discard"
    BLINK = "blink"
    TOOLBOX = "toolbox"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"

class DeckStatsTracker:
    """Comprehensive deck statistics tracker with analytics and recommendations"""
    
    def __init__(self, storage_path: str = "./deck_stats",
                 card_db: Dict = None, use_compression: bool = True,
                 decks: List = None, decks_directory: str = None,
                 persistence_interval_games: int = 1):
        # Initialize storage path
        self.base_path = storage_path
        self.use_compression = use_compression
        self.card_db = card_db or {}
        self.source_decks = list(decks or [])
        self.decks_directory = decks_directory
        self.persistence_interval_games = max(
            1, int(persistence_interval_games))
        self._games_since_persistence = 0
        self._last_record_flush_succeeded = None
        self._meta_data_cache = None
        self._meta_data_dirty = False
        self._individual_card_cache = {}
        self._dirty_individual_card_files = set()
        self._strategy_profiles_by_composition = {}
        self._ensure_directories()
        self.current_deck_name_p1 = None
        self.current_deck_name_p2 = None
        # Set up locks for thread safety
        self.batch_lock = threading.RLock()
        self.lock = threading.RLock()
        self._io_lock = threading.RLock()
        
        # Initialize cache with default settings
        self.cache = self._create_statistics_cache(max_size=100, ttl=3600)
        
        # Initialize batch updates storage
        self.batch_updates = defaultdict(dict)
        
        self.deck_name_to_id = {}
        self.deck_id_to_name = {}
        self.card_id_to_name = {}
        self._initialize_mappings()
        
    def _initialize_mappings(self):
        """Initialize deck name to ID and card ID to name mappings"""
        # Build deck name to ID mapping from existing deck files
        deck_files = self._get_all_deck_files()
        for file_path in deck_files:
            try:
                deck_data = self.load(file_path)
                if deck_data and "name" in deck_data and "deck_id" in deck_data:
                    self.deck_name_to_id[deck_data["name"]] = deck_data["deck_id"]
                    self.deck_id_to_name[deck_data["deck_id"]] = deck_data["name"]
                    
                    # Also load card mappings from the card_list if available
                    if "card_list" in deck_data:
                        for card in deck_data["card_list"]:
                            if "id" in card and "name" in card:
                                self.card_id_to_name[card["id"]] = card["name"]
            except Exception as e:
                logging.error(f"Error initializing deck mapping for {file_path}: {str(e)}")

        # The environment already owns the exact active corpus. Mapping those
        # runtime decks avoids hard-coding Standard when training or Harvest
        # is running a Pioneer, Modern, imported, or custom pool.
        for deck in self.source_decks:
            if not isinstance(deck, dict):
                continue
            deck_name = str(deck.get("name", "")).strip()
            card_ids = list(deck.get("cards", []))
            if not deck_name or not card_ids:
                continue
            deck_id = self.get_deck_fingerprint(card_ids)
            self.deck_name_to_id[deck_name] = deck_id
            self.deck_id_to_name[deck_id] = deck_name
            raw_profile = deck.get("strategy_profile")
            if raw_profile is not None:
                try:
                    self._strategy_profiles_by_composition[
                        deck_composition_hash(card_ids)
                    ] = normalize_declared_profile(raw_profile)
                except ValueError as error:
                    logging.warning(
                        "Ignoring invalid strategy profile for %s: %s",
                        deck_name, error)
            
        # Legacy/direct callers that do not supply runtime decks may still
        # discover a configured hydrated directory (Standard by default).
        try:
            parent_dir = os.path.dirname(self.base_path)
            decks_dir = None if self.source_decks else (
                self.decks_directory or os.path.join(
                    parent_dir, "formats", "standard", "decks"))
            
            if (decks_dir and os.path.exists(decks_dir)
                    and os.path.isdir(decks_dir)):
                logging.info(f"Scanning for deck files in {decks_dir}")
                deck_files = glob.glob(
                    os.path.join(decks_dir, "**", "*.json"), recursive=True)
                
                for deck_file in deck_files:
                    try:
                        # Read the deck file to calculate its fingerprint
                        with open(deck_file, 'r', encoding='utf-8') as f:
                            deck_data = json.load(f)
                        deck_name = str(
                            deck_data.get("name")
                            if isinstance(deck_data, dict) else "").strip()
                        if not deck_name:
                            deck_name = os.path.basename(
                                deck_file).replace('.json', '')
                        
                        # Extract card IDs depending on format
                        if isinstance(deck_data, list):
                            # If it's a simple card list
                            card_ids = [card.get("id") for card in deck_data if isinstance(card, dict) and "id" in card]
                            if not card_ids:
                                card_ids = [card for card in deck_data if isinstance(card, int)]
                        elif isinstance(deck_data, dict) and "cards" in deck_data:
                            # If it's a structured deck with a cards array
                            card_ids = [card.get("id") for card in deck_data["cards"] if isinstance(card, dict) and "id" in card]
                        elif isinstance(deck_data, dict) and "deck" in deck_data:
                            # Hydrated corpus: resolve embedded card names
                            # through the active canonical card database.
                            ids_by_name = {
                                str(getattr(card, "name", "")).casefold(): card_id
                                for card_id, card in (self.card_db or {}).items()
                            }
                            card_ids = []
                            for entry in deck_data["deck"]:
                                raw = entry.get("card", {}) if isinstance(entry, dict) else {}
                                name = raw.get("name") if isinstance(raw, dict) else raw
                                card_id = ids_by_name.get(str(name).casefold())
                                if card_id is not None:
                                    card_ids.extend(
                                        [card_id] * max(0, int(entry.get("count", 1))))
                        else:
                            continue  # Skip if we can't extract card IDs
                        
                        if card_ids:
                            # Calculate fingerprint and store mapping
                            deck_id = self.get_deck_fingerprint(card_ids)
                            self.deck_name_to_id[deck_name] = deck_id
                            self.deck_id_to_name[deck_id] = deck_name  # Add this line
                            logging.info(f"Added deck mapping: {deck_name} -> {deck_id}")
                    except Exception as e:
                        logging.debug(f"Error processing deck file {deck_file}: {e}")
                        continue
        except Exception as e:
            logging.debug(f"Error scanning hydrated deck directory: {e}")
        
        # Build card ID to name mapping
        if self.card_db:
            for card_id, card in self.card_db.items():
                if hasattr(card, 'name'):
                    self.card_id_to_name[card_id] = card.name
    
    # === Storage Backend Methods ===
    
    def _ensure_directories(self):
        """Create necessary directories if they don't exist"""
        os.makedirs(self.base_path, exist_ok=True)
        os.makedirs(os.path.join(self.base_path, "decks"), exist_ok=True)
        os.makedirs(os.path.join(self.base_path, "meta"), exist_ok=True)
        os.makedirs(os.path.join(self.base_path, "cards"), exist_ok=True)
        
    def save(self, path: str, data: Any) -> bool:
        """Atomically save data to a JSON file."""
        temp_path = None
        with self._io_lock:
            try:
                full_path = os.path.join(self.base_path, path)
                directory = os.path.dirname(full_path)
                os.makedirs(directory, exist_ok=True)
                target_path = f"{full_path}.gz" if self.use_compression else full_path
                fd, temp_path = tempfile.mkstemp(
                    prefix=os.path.basename(full_path) + "_",
                    suffix=".tmp", dir=directory)
                os.close(fd)

                if self.use_compression:
                    with gzip.open(temp_path, 'wt', encoding='utf-8') as f:
                        json.dump(data, f, indent=2)
                else:
                    with open(temp_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2)
                os.replace(temp_path, target_path)
                temp_path = None
                return True
            except Exception as e:
                logging.error(f"Error saving data to {path}: {str(e)}")
                return False
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
        
    def validate_and_repair_compressed_files(self, directory: str):
        """
        Scan and attempt to repair corrupted compressed JSON files
        """
        import os
        import gzip
        import shutil

        for filename in os.listdir(directory):
            if filename.endswith('.json.gz'):
                full_path = os.path.join(directory, filename)
                try:
                    # Attempt to read the file
                    with gzip.open(full_path, 'rb') as f:
                        json.loads(f.read().decode('utf-8'))
                except Exception as e:
                    logging.warning(f"Corrupted file detected: {filename}. Error: {str(e)}")
                    
                    # Create a backup of the corrupted file
                    backup_path = f"{full_path}.corrupt"
                    shutil.copy(full_path, backup_path)
                    
                    # Attempt to recreate the file from the original JSON if possible
                    try:
                        # Remove .gz extension to get original JSON path
                        json_path = full_path[:-3]
                        if os.path.exists(json_path):
                            # Recompress the original JSON
                            with open(json_path, 'rb') as f_in:
                                with gzip.open(full_path, 'wb') as f_out:
                                    shutil.copyfileobj(f_in, f_out)
                            logging.info(f"Successfully recompressed {filename}")
                    except Exception as repair_error:
                        logging.error(f"Could not repair {filename}: {str(repair_error)}")
            
    def load(self, path: str) -> Any:
        try:
            full_path = os.path.join(self.base_path, path)
            # If use_compression is enabled and a gzipped version exists, use that.
            if self.use_compression and os.path.exists(f"{full_path}.gz"):
                full_path += ".gz"
            if not os.path.exists(full_path):
                return None

            # Open in binary mode to check the first two bytes
            with open(full_path, 'rb') as f:
                magic = f.read(2)
            
            if magic == b'\x1f\x8b':
                # It is a gzip file; open in text mode with automatic decompression.
                try:
                    with gzip.open(full_path, 'rt', encoding='utf-8') as f:
                        return json.load(f)
                except UnicodeDecodeError as e:
                    logging.error(f"UTF-8 decode error for {path}, trying latin-1: {e}")
                    try:
                        with gzip.open(full_path, 'rt', encoding='latin-1') as f:
                            return json.load(f)
                    except Exception as decode_error:
                        logging.error(f"Failed to decode compressed file {path} with latin-1: {decode_error}")
                        return None
                except Exception as e:
                    logging.error(f"Error reading compressed file {path}: {e}")
                    return None
            else:
                # Not gzipped – open normally.
                with open(full_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logging.error(f"Error loading data from {path}: {e}")
            return None

    def exists(self, path: str) -> bool:
        """Check if a file exists"""
        full_path = os.path.join(self.base_path, path)
        return os.path.exists(full_path) or os.path.exists(f"{full_path}.gz")

    async def save_async(self, path: str, data: Any) -> bool:
        """Save data to a JSON file asynchronously"""
        def _save():
            return self.save(path, data)
            
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _save)
        
    async def load_async(self, path: str) -> Any:
        """Load data from a JSON file asynchronously"""
        def _load():
            return self.load(path)
            
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _load)
    
    def _validate_stats_types(self, stats: Dict) -> Dict:
        """
        Validate and correct data types in statistics dictionaries.
        
        Args:
            stats: Dictionary containing statistics data
            
        Returns:
            Dictionary with validated and corrected data types
        """
        # Define expected types for each field
        type_map = {
            "games": int,
            "wins": int,
            "losses": int,
            "draws": int,
            "total_turns": int,
            "win_rate": float,
            "usage_count": int,
            "games_played": int,
            "games_drawn": int,
            "wins_when_drawn": float,  # Float to handle draws = 0.5
            "games_not_drawn": int,
            "wins_when_not_drawn": float,
            "games_in_opening_hand": int,
            "wins_when_in_opening_hand": float,
            "drawn_win_rate": float,
            "not_drawn_win_rate": float,
            "opening_hand_win_rate": float
        }
        
        # Process top-level fields
        for field, expected_type in type_map.items():
            if field in stats:
                try:
                    # Convert to expected type
                    stats[field] = expected_type(stats[field])
                except (TypeError, ValueError):
                    # Set default value on error
                    if expected_type == int:
                        stats[field] = 0
                    elif expected_type == float:
                        stats[field] = 0.0
        
        # Process nested dictionaries
        for key, value in list(stats.items()):
            if isinstance(value, dict):
                # Recursively validate nested dictionaries
                stats[key] = self._validate_stats_types(value)
            elif isinstance(value, list):
                # Process lists of dictionaries
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        stats[key][i] = self._validate_stats_types(item)
        
        return stats
    
    # === Statistics Cache Methods ===
    def _safe_numeric_operation(self, operation: str, a: Any, b: Any, default: float = 0.0) -> float:
        # (Implementation remains the same)
        """
        Safely perform numeric operations with error handling and type checking.

        Args:
            operation: Operation to perform ('add', 'subtract', 'multiply', 'divide')
            a: First operand
            b: Second operand
            default: Default value to return on error

        Returns:
            Result of operation or default on error
        """
        try:
            # Convert inputs to float for consistent behavior
            a_float = float(a)
            b_float = float(b)

            if operation == 'add':
                return a_float + b_float
            elif operation == 'subtract':
                return a_float - b_float
            elif operation == 'multiply':
                return a_float * b_float
            elif operation == 'divide':
                # Prevent division by zero
                if b_float == 0:
                    return default
                return a_float / b_float
            else:
                logging.warning(f"Unknown operation: {operation}")
                return default
        except (TypeError, ValueError) as e:
            logging.warning(f"Numeric operation error: {str(e)}")
            return default
    
    def _create_statistics_cache(self, max_size: int = 100, ttl: int = 3600): # ttl is no longer used
        """Create a new cache for frequently accessed statistics with LRU eviction"""
        from collections import OrderedDict

        cache = {
            "max_size": max_size,
            # "ttl": ttl, # TTL mechanism removed
            "cache": OrderedDict(),  # Use OrderedDict for LRU behavior
            # "timestamps": {}, # Timestamps removed
            "lock": threading.RLock()
        }
        return cache


    def cache_get(self, key: str) -> Optional[Any]:
        """Get a value from the cache with LRU update (TTL removed)"""
        with self.cache["lock"]:
            if key in self.cache["cache"]:
                # No TTL check needed anymore
                # Move to end to mark as recently used
                self.cache["cache"].move_to_end(key)
                return self.cache["cache"][key]
            return None

    def cache_set(self, key: str, value: Any) -> None:
        """Store a value in the cache with LRU eviction (TTL removed)"""
        with self.cache["lock"]:
            # If key already exists, remove it first to update order
            if key in self.cache["cache"]:
                del self.cache["cache"][key]

            # Evict if full
            if len(self.cache["cache"]) >= self.cache["max_size"]:
                # Evict least recently used item (first item in OrderedDict)
                self.cache["cache"].popitem(last=False)
                # No timestamp to remove

            # Add to cache
            self.cache["cache"][key] = value
            # No timestamp to set

    def cache_invalidate(self, key: str) -> None:
        """Remove a specific key from the cache"""
        with self.cache["lock"]:
            if key in self.cache["cache"]:
                del self.cache["cache"][key]
                # No timestamp to remove

    def cache_clear(self) -> None:
        """Clear the entire cache"""
        with self.cache["lock"]:
            self.cache["cache"].clear()
            # No timestamps to clear

    def _cache_evict_oldest(self) -> None:
        """Evict the oldest entry from the cache (LRU via OrderedDict)"""
        with self.cache["lock"]:
            if self.cache["cache"]:
                self.cache["cache"].popitem(last=False)
        
    def _check_deck_conflict(self, deck_id: str, deck_name: str, card_list: List) -> bool:
        """
        Verify this isn't a duplicate of a different deck with the same ID.
        
        Args:
            deck_id: The fingerprint/ID of the deck
            deck_name: The name of the deck
            card_list: The list of cards in the deck
            
        Returns:
            bool: True if a conflict is detected, False otherwise
        """
        # Check if this ID already exists but with a different name
        if deck_id in self.deck_id_to_name and self.deck_id_to_name[deck_id] != deck_name:
            existing_name = self.deck_id_to_name[deck_id]
            logging.warning(f"Potential deck fingerprint collision: '{deck_name}' has same ID as '{existing_name}': {deck_id}")
            
            # Try to load the existing deck to compare
            existing_stats = self.get_deck_stats(deck_id)
            if existing_stats and "card_list" in existing_stats:
                existing_cards = [card["id"] for card in existing_stats["card_list"]]
                similarity = self.calculate_deck_similarity(card_list, existing_cards)
                
                if similarity < 0.9:  # If decks are less than 90% similar, this is a real conflict
                    logging.error(f"Deck ID collision confirmed: {deck_name} and {existing_name} only have {similarity:.1%} similarity but share ID {deck_id}")
                    return True
                    
            return True  # Conservatively report a conflict if we can't verify
        
        return False
    
    def get_deck_fingerprint(self, card_list: List[Union[int, str, dict]], deck_name: str = None) -> str:
        """
        Generate a deterministic fingerprint for a deck based solely on its card composition.
        
        Args:
            card_list: List of card IDs or dictionaries with card details
            deck_name: Optional name of the deck (not used in fingerprint calculation)
            
        Returns:
            str: MD5 hash fingerprint of the deck
        """
        # Convert the card list to a consistent representation
        if card_list and isinstance(card_list[0], dict) and "id" in card_list[0] and "count" in card_list[0]:
            # Use the immutable composition with card IDs sorted
            items = sorted([(str(item["id"]), item["count"]) for item in card_list], key=lambda x: x[0])
            deck_str = ",".join(f"{card}:{count}" for card, count in items)
        else:
            # Count occurrences of each card
            card_counter = Counter([str(card) for card in card_list])
            items = sorted(card_counter.items(), key=lambda x: x[0])
            deck_str = ",".join(f"{card}:{count}" for card, count in items)
        
        # Generate the hash (no salt or timestamp)
        fingerprint = hashlib.md5(deck_str.encode()).hexdigest()
        
        logging.debug(f"Generated fingerprint {fingerprint} for deck with {len(items)} unique cards" +
                    (f" (name: {deck_name})" if deck_name else ""))
        
        return fingerprint
    
    def identify_strategy_profile(
            self, card_list: List[int]) -> DeckStrategyProfile:
        """Return the centralized, versioned profile for one exact deck."""
        declared = getattr(
            self, "_strategy_profiles_by_composition", {}).get(
                deck_composition_hash(card_list))
        return classify_full_deck(
            card_list, self.card_db or {}, declared=declared)

    def identify_archetype(self, card_list: List[int]) -> str:
        """
        Return the centralized profile's primary macro as a legacy string API.

        Existing analytics persist a single lowercase string. Keeping that
        boundary avoids an unversioned stats migration while all new
        classification logic and reviewed overrides live in archetypes.py.
        """
        return self.identify_strategy_profile(card_list).primary.value

    def calculate_deck_similarity(self, deck1: List[int], deck2: List[int]) -> float:
        """
        Calculate similarity between two decks.
        Returns a value between 0 (completely different) and 1 (identical).
        """
        # Convert lists to sets for intersection/union calculation
        set1 = set(deck1)
        set2 = set(deck2)
        
        # Calculate Jaccard similarity coefficient
        intersection = len(set1.intersection(set2))
        union = len(set1.union(set2))
        
        if union == 0:
            return 0
            
        return intersection / union
    
    def find_similar_decks(self, deck: List[int], threshold: float = 0.7) -> List[str]:
        """
        Find decks similar to the given deck.
        Returns a list of deck IDs that exceed the similarity threshold.
        """
        similar_decks = []
        deck_database = self._get_all_deck_data()
        
        for deck_id, deck_data in deck_database.items():
            if "card_list" in deck_data:
                other_deck = [card["id"] for card in deck_data["card_list"]]
                similarity = self.calculate_deck_similarity(deck, other_deck)
                
                if similarity >= threshold:
                    similar_decks.append(deck_id)
                    
        return similar_decks
    
    # === Statistics Validation Methods ===
    
    def validate_deck_stats(self, stats: Dict) -> Tuple[bool, List[str]]:
        """Validate deck statistics data"""
        errors = []
        
        # Check required fields
        required_fields = ["wins", "losses", "games", "avg_game_length", "archetype", "name", "card_list"]
        for field in required_fields:
            if field not in stats:
                errors.append(f"Missing required field: {field}")
                
        # Check draws field exists
        if "draws" not in stats:
            errors.append("Missing 'draws' field")
        
        # Check numeric fields for valid values
        numeric_fields = ["wins", "losses", "games", "avg_game_length", "draws"]
        for field in numeric_fields:
            if field in stats:
                value = stats[field]
                if (isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value)) or value < 0):
                    errors.append(f"Invalid value for {field}: {value}")
                
        # Check consistency
        if "wins" in stats and "losses" in stats and "draws" in stats and "games" in stats:
            if stats["wins"] + stats["losses"] + stats["draws"] != stats["games"]:
                errors.append(f"Inconsistent game count: wins({stats['wins']}) + losses({stats['losses']}) + draws({stats['draws']}) != games({stats['games']})")
                
        # Check card list
        if "card_list" in stats:
            if not isinstance(stats["card_list"], list):
                errors.append("Card list must be an array")
            else:
                for card in stats["card_list"]:
                    if not isinstance(card, dict) or "id" not in card or "name" not in card or "count" not in card:
                        errors.append(f"Invalid card entry in card list: {card}")
        
        # Check performance_by_stage has draws
        if "performance_by_stage" in stats:
            for stage, stage_data in stats["performance_by_stage"].items():
                if "draws" not in stage_data:
                    errors.append(f"Missing 'draws' field in performance_by_stage.{stage}")
        
        return len(errors) == 0, errors
        
    def validate_card_stats(self, stats: Dict) -> Tuple[bool, List[str]]:
        """Validate card statistics data"""
        errors = []
        
        # Check required fields
        required_fields = ["wins", "losses", "games", "avg_game_length", "archetype", "name", "composition"]
        for field in required_fields:
            if field not in stats:
                errors.append(f"Missing required field: {field}")
                
        # Check numeric fields for valid values
        numeric_fields = ["games_played", "wins", "losses", "usage_count", "win_rate"]
        for field in numeric_fields:
            if field in stats:
                value = stats[field]
                if (isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or (field != "win_rate" and value < 0)):
                    errors.append(f"Invalid value for {field}: {value}")
                
        # Check consistency
        if "wins" in stats and "losses" in stats and "games_played" in stats:
            if stats["wins"] + stats["losses"] != stats["games_played"]:
                errors.append(f"Inconsistent game count: wins({stats['wins']}) + losses({stats['losses']}) != games_played({stats['games_played']})")
                
        # Check win rate calculation
        if "wins" in stats and "games_played" in stats and "win_rate" in stats:
            if stats["games_played"] > 0:
                expected_win_rate = stats["wins"] / stats["games_played"]
                if abs(stats["win_rate"] - expected_win_rate) > 0.001:  # Allow for floating point differences
                    errors.append(f"Inconsistent win rate: {stats['win_rate']} vs expected {expected_win_rate}")
                    
        return len(errors) == 0, errors
        
    def repair_deck_stats(self, stats: Dict) -> Dict:
        """Attempt to repair inconsistent deck statistics"""
        # Ensure draws field exists
        if "draws" not in stats:
            stats["draws"] = 0
        
        # Fix inconsistent game count by recalculating total games
        if "wins" in stats and "losses" in stats and "draws" in stats:
            stats["games"] = stats["wins"] + stats["losses"] + stats["draws"]
                
        if "wins" in stats and "games" in stats and stats["games"] > 0:
            # Calculate win rate including draws (0.5 points per draw)
            stats["win_rate"] = (stats["wins"] + 0.5 * stats["draws"]) / stats["games"]
                
        if "card_list" not in stats:
            stats["card_list"] = []
                
        if "archetype" not in stats:
            stats["archetype"] = DeckArchetype.MIDRANGE.value
                
        if "name" not in stats:
            stats["name"] = "Unknown Deck"
                
        if "avg_game_length" not in stats and "games" in stats and "total_turns" in stats:
            stats["avg_game_length"] = stats["total_turns"] / max(1, stats["games"])

        # Add additional stats repair for enhanced fields
        if "matchups" not in stats:
            stats["matchups"] = {}
                
        if "card_performance" not in stats:
            stats["card_performance"] = {}
                
        if "performance_by_stage" not in stats:
            stats["performance_by_stage"] = {
                "early": {"wins": 0, "losses": 0, "draws": 0},
                "mid": {"wins": 0, "losses": 0, "draws": 0},
                "late": {"wins": 0, "losses": 0, "draws": 0}
            }
        else:
            # Ensure each stage has draws field
            for stage in stats["performance_by_stage"]:
                if "draws" not in stats["performance_by_stage"][stage]:
                    stats["performance_by_stage"][stage]["draws"] = 0
                
        if "meta_position" not in stats:
            stats["meta_position"] = {}
        
        # Ensure performance_by_position has draws fields
        if "performance_by_position" in stats:
            for position in stats["performance_by_position"]:
                if "draws" not in stats["performance_by_position"][position]:
                    stats["performance_by_position"][position]["draws"] = 0
        else:
            stats["performance_by_position"] = {
                "ahead": {"wins": 0, "losses": 0, "draws": 0},
                "parity": {"wins": 0, "losses": 0, "draws": 0},
                "behind": {"wins": 0, "losses": 0, "draws": 0}
            }
            
        return stats
        
    def repair_card_stats(self, stats: Dict) -> Dict:
        """Attempt to repair inconsistent card statistics"""
        if "wins" in stats and "losses" in stats:
            stats["games_played"] = stats["wins"] + stats["losses"]
            
        if "wins" in stats and "games_played" in stats and stats["games_played"] > 0:
            stats["win_rate"] = stats["wins"] / stats["games_played"]
            
        if "name" not in stats:
            stats["name"] = "Unknown Card"
            
        if "usage_count" not in stats:
            stats["usage_count"] = stats.get("games_played", 0)
            
        # Repair enhanced card statistics
        if "performance_by_turn" not in stats:
            stats["performance_by_turn"] = {}
            
        if "drawn_win_rate" not in stats and "games_drawn" in stats and stats["games_drawn"] > 0:
            stats["drawn_win_rate"] = stats.get("wins_when_drawn", 0) / stats["games_drawn"]
            
        if "not_drawn_win_rate" not in stats and "games_not_drawn" in stats and stats["games_not_drawn"] > 0:
            stats["not_drawn_win_rate"] = stats.get("wins_when_not_drawn", 0) / stats["games_not_drawn"]
            
        if "opening_hand_win_rate" not in stats and "games_in_opening_hand" in stats and stats["games_in_opening_hand"] > 0:
            stats["opening_hand_win_rate"] = stats.get("wins_when_in_opening_hand", 0) / stats["games_in_opening_hand"]
            
        if "performance_by_position" not in stats:
            stats["performance_by_position"] = {
                "ahead": {"wins": 0, "losses": 0, "played": 0},
                "parity": {"wins": 0, "losses": 0, "played": 0},
                "behind": {"wins": 0, "losses": 0, "played": 0}
            }
            
        return stats

    # === Card Synergy Methods ===
    
    def calculate_synergy_score(self, card1_id: int, card2_id: int) -> float:
        """
        Calculate a synergy score between two cards with enhanced safety checks.
        Returns a value between 0 (no synergy) and 1 (maximum synergy).
        """
        # Check if score is already calculated
        key = f"{min(card1_id, card2_id)}_{max(card1_id, card2_id)}"
        cached_score = self.cache_get(f"synergy:{key}")
        if cached_score is not None:
            return cached_score
            
        # Get the card objects with explicit error handling
        card1 = self.card_db.get(card1_id)
        card2 = self.card_db.get(card2_id)
        
        if not card1 or not card2:
            return 0.0

        # Define synergy types with weights
        synergy_types = {
            "tribal": 0.8,       # Cards that share a creature type
            "mechanic": 0.7,     # Cards that share a keyword or mechanic
            "combo": 0.9,        # Cards that form a known combo
            "theme": 0.6,        # Cards that share a theme
            "color": 0.3,        # Cards that share a color
            "curve": 0.4,        # Cards that fit well on curve
            "support": 0.75      # Cards that directly support each other
        }
            
        score = 0.0
        max_score = 0.0
        
        # Safely check for tribal synergies
        has_tribal_synergy = False
        if hasattr(card1, 'subtypes') and hasattr(card2, 'subtypes'):
            shared_types = set(getattr(card1, 'subtypes', [])) & set(getattr(card2, 'subtypes', []))
            has_tribal_synergy = len(shared_types) > 0
        
        if has_tribal_synergy:
            score += synergy_types["tribal"]
        max_score += synergy_types["tribal"]
            
        # Check for mechanic synergies
        if hasattr(card1, 'keywords') and hasattr(card2, 'keywords'):
            shared_keywords = 0
            for i in range(min(len(card1.keywords), len(card2.keywords))):
                if card1.keywords[i] > 0 and card2.keywords[i] > 0:
                    shared_keywords += 1
            
            if shared_keywords > 0:
                score += synergy_types["mechanic"] * (shared_keywords / max(1, min(len(card1.keywords), len(card2.keywords))))
            max_score += synergy_types["mechanic"]
            
        # Check for color synergies
        if hasattr(card1, 'colors') and hasattr(card2, 'colors'):
            shared_colors = sum(1 for i in range(min(len(card1.colors), len(card2.colors))) 
                              if card1.colors[i] > 0 and card2.colors[i] > 0)
            
            if shared_colors > 0:
                score += synergy_types["color"] * (shared_colors / 5)  # Assuming 5 colors
            max_score += synergy_types["color"]
            
        # Check for curve synergies
        if hasattr(card1, 'cmc') and hasattr(card2, 'cmc'):
            # Cards with sequential CMCs often play well together
            if abs(card1.cmc - card2.cmc) == 1:
                score += synergy_types["curve"] * 0.8
            elif abs(card1.cmc - card2.cmc) == 2:
                score += synergy_types["curve"] * 0.5
            max_score += synergy_types["curve"]
            
        # Check for text-based synergies
        if hasattr(card1, 'oracle_text') and hasattr(card2, 'oracle_text'):
            # Check for references to each other's characteristics in oracle text
            card1_text = card1.oracle_text.lower()
            card2_text = card2.oracle_text.lower()
            
            # Check if card1 mentions anything related to card2
            card2_relevant_terms = []
            if hasattr(card2, 'card_types'):
                card2_relevant_terms.extend(card2.card_types)
            if hasattr(card2, 'subtypes'):
                card2_relevant_terms.extend(card2.subtypes)
            
            card1_relevant_terms = []
            if hasattr(card1, 'card_types'):
                card1_relevant_terms.extend(card1.card_types)
            if hasattr(card1, 'subtypes'):
                card1_relevant_terms.extend(card1.subtypes)
            
            # Count mentions
            mentions = 0
            for term in card2_relevant_terms:
                if term.lower() in card1_text:
                    mentions += 1
                    
            for term in card1_relevant_terms:
                if term.lower() in card2_text:
                    mentions += 1
            
            if mentions > 0:
                score += synergy_types["support"] * min(1.0, mentions / 3)
            max_score += synergy_types["support"]
            
        # Hard-coded known combos could be added here
        # This would require a database of known card combinations
        
        # Normalize the score
        if max_score > 0:
            final_score = score / max_score
        else:
            final_score = 0.0
            
        # Cache the result
        self.cache_set(f"synergy:{key}", final_score)
        
        return final_score
    
    def find_synergistic_cards(self, card_id: int, card_pool: List[int], threshold: float = 0.4) -> List[Tuple[int, float]]:
        """
        Find cards that have good synergy with the given card.
        Returns a list of (card_id, synergy_score) tuples.
        """
        synergies = []
        
        for other_id in card_pool:
            if other_id != card_id:
                score = self.calculate_synergy_score(card_id, other_id)
                if score >= threshold:
                    synergies.append((other_id, score))
                    
        # Sort by synergy score (highest first)
        synergies.sort(key=lambda x: x[1], reverse=True)
        
        return synergies
    
    def get_deck_synergy_matrix(self, deck: List[int]) -> Dict[Tuple[int, int], float]:
        """
        Calculate synergy scores between all pairs of cards in a deck.
        Returns a dictionary mapping (card1_id, card2_id) to synergy score.
        """
        synergy_matrix = {}
        
        for i, card1_id in enumerate(deck):
            for j, card2_id in enumerate(deck[i+1:], i+1):
                score = self.calculate_synergy_score(card1_id, card2_id)
                synergy_matrix[(card1_id, card2_id)] = score
                
        return synergy_matrix
    
    def calculate_deck_synergy_score(self, deck: List[int]) -> float:
        """
        Calculate an overall synergy score for a deck.
        Returns a value between 0 (low synergy) and 1 (high synergy).
        """
        if len(deck) <= 1:
            return 0.0
            
        # Get pairwise synergies
        synergy_matrix = self.get_deck_synergy_matrix(deck)
        
        # Calculate average synergy score
        if synergy_matrix:
            return sum(synergy_matrix.values()) / len(synergy_matrix)
        else:
            return 0.0
    
    # === Meta Analysis Methods ===
    
    def _load_meta_data(self) -> Dict:
        """Load meta data from storage"""
        if self._meta_data_cache is not None:
            return self._meta_data_cache
        meta_data = self.load("meta/meta_data.json")
        if not meta_data:
            # Initialize empty meta data
            meta_data = {
                "version": STATS_VERSION,
                # "last_updated": time.time(), # Removed time dependency
                "total_games": 0,
                "archetypes": {},
                "cards": {},
                "matchups": {},
                "trends": {
                    "weekly": {},
                    "monthly": {}
                }
            }
        # ``games`` on card/archetype aggregates counts deck-seat appearances,
        # while ``total_games`` counts matches.  Normalize legacy 3.1 files on
        # read so recommendation code never consumes the old >100% rates.
        total_games = meta_data.get("total_games", 0)
        for card_data in meta_data.get("cards", {}).values():
            card_data["play_rate"] = _deck_seat_share(
                card_data.get("games", 0), total_games)
        meta_data["version"] = STATS_VERSION
        self._meta_data_cache = meta_data
        return meta_data

    def save_meta_data(self) -> bool:
        """Save meta data to storage"""
        meta_data = self._load_meta_data()
        # meta_data["last_updated"] = time.time() # Removed time dependency
        saved = self.save("meta/meta_data.json", meta_data)
        if saved:
            self._meta_data_dirty = False
        return saved
    
    def update_meta_with_game_result(self, winner_deck: List[int], loser_deck: List[int], 
                                winner_archetype: str, loser_archetype: str,
                                cards_played: Dict[int, List[int]], turn_count: int,
                                is_draw: bool = False) -> bool:
        """
        Update meta data with a new game result.
        
        Args:
            winner_deck: List of card IDs in the winning deck
            loser_deck: List of card IDs in the losing deck
            winner_archetype: Archetype of the winning deck
            loser_archetype: Archetype of the losing deck
            cards_played: Dictionary mapping player ID to list of cards played
            turn_count: Number of turns the game lasted
            is_draw: Whether the game ended in a draw
            
        Returns:
            bool: Whether the update was successful
        """
        # Get current meta data
        meta_data = self._load_meta_data()
        
        # Update total games count
        meta_data["total_games"] += 1
        
        # Add draws counter if it doesn't exist
        if "draws" not in meta_data:
            meta_data["draws"] = 0
        
        # Update archetype stats
        if winner_archetype not in meta_data["archetypes"]:
            meta_data["archetypes"][winner_archetype] = {
                "games": 0, "wins": 0, "losses": 0, "draws": 0, "win_rate": 0
            }
        if loser_archetype not in meta_data["archetypes"]:
            meta_data["archetypes"][loser_archetype] = {
                "games": 0, "wins": 0, "losses": 0, "draws": 0, "win_rate": 0
            }
            
        meta_data["archetypes"][winner_archetype]["games"] += 1
        meta_data["archetypes"][loser_archetype]["games"] += 1
        
        if is_draw:
            meta_data["draws"] += 1
            meta_data["archetypes"][winner_archetype]["draws"] += 1
            meta_data["archetypes"][loser_archetype]["draws"] += 1
        else:
            meta_data["archetypes"][winner_archetype]["wins"] += 1
            meta_data["archetypes"][loser_archetype]["losses"] += 1
        
        # Update win rates
        for archetype in [winner_archetype, loser_archetype]:
            arch_data = meta_data["archetypes"][archetype]
            if arch_data["games"] > 0:
                # Adjust win rate calculation to account for draws
                # Option 1: Count only decisive games (wins / (wins + losses))
                # Option 2: Count draws as half-wins (wins + 0.5*draws) / games
                # Using option 2 here
                arch_data["win_rate"] = (arch_data["wins"] + 0.5 * arch_data["draws"]) / arch_data["games"]
                
                
        # Update matchup matrix
        matchup_key = f"{winner_archetype}_vs_{loser_archetype}"
        reverse_key = f"{loser_archetype}_vs_{winner_archetype}"
        
        if matchup_key not in meta_data["matchups"]:
            meta_data["matchups"][matchup_key] = {"wins": 0, "losses": 0, "draws": 0, "win_rate": 0}
        if reverse_key not in meta_data["matchups"]:
            meta_data["matchups"][reverse_key] = {"wins": 0, "losses": 0, "draws": 0, "win_rate": 0}
            
        if is_draw:
            meta_data["matchups"][matchup_key]["draws"] += 1
            meta_data["matchups"][reverse_key]["draws"] += 1
        else:
            meta_data["matchups"][matchup_key]["wins"] += 1
            meta_data["matchups"][reverse_key]["losses"] += 1
        
        # Update win rates for matchups with draw consideration
        for key in [matchup_key, reverse_key]:
            matchup_data = meta_data["matchups"][key]
            total_games = matchup_data["wins"] + matchup_data["losses"] + matchup_data["draws"]
            if total_games > 0:
                # Count draws as half-wins
                matchup_data["win_rate"] = (matchup_data["wins"] + 0.5 * matchup_data["draws"]) / total_games
        
        # Update card stats - using card name instead of ID
        all_cards = set(winner_deck + loser_deck)
        
        for card_id in all_cards:
            # Get card name
            card_name = self._get_card_name(card_id)
            if not card_name:
                continue  # Skip cards without names
                
            # Use card name as key
            if card_name not in meta_data["cards"]:
                meta_data["cards"][card_name] = {
                    "usage_count": 0,
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "draws": 0,
                    "win_rate": 0,
                    "play_rate": 0,
                    "archetypes": {}
                }
                
            card_data = meta_data["cards"][card_name]
            
            # Update usage count
            card_data["usage_count"] += (
                winner_deck.count(card_id) + loser_deck.count(card_id)
            )
            
            # Update game stats
            if card_id in winner_deck:
                card_data["games"] += 1
                if is_draw:
                    card_data["draws"] += 1
                else:
                    card_data["wins"] += 1
                
                # Update archetype association
                if winner_archetype not in card_data["archetypes"]:
                    card_data["archetypes"][winner_archetype] = 0
                card_data["archetypes"][winner_archetype] += 1
                
            if card_id in loser_deck:
                card_data["games"] += 1
                if is_draw:
                    # Card results are counted per deck-seat appearance. If the
                    # same card is in both decks, a draw contributes two games
                    # and therefore two draws.
                    card_data["draws"] += 1
                else:
                    card_data["losses"] += 1
                
                # Update archetype association
                if loser_archetype not in card_data["archetypes"]:
                    card_data["archetypes"][loser_archetype] = 0
                card_data["archetypes"][loser_archetype] += 1
                
            # Update win rate with draws considered
            if card_data["games"] > 0:
                card_data["win_rate"] = (card_data["wins"] + 0.5 * card_data["draws"]) / card_data["games"]
                
        # The denominator changes every game, including for cards absent from
        # the latest matchup. Refresh all rates so old entries do not retain a
        # stale play-rate denominator.
        for card_data in meta_data["cards"].values():
            card_data["play_rate"] = _deck_seat_share(
                card_data["games"], meta_data["total_games"])
        
        self._meta_data_dirty = True
        return True
    
    def get_top_archetypes(self, limit: int = 5, min_games: int = 10) -> List[Dict]:
        """
        Get the top performing archetypes.
        
        Args:
            limit: Maximum number of archetypes to return
            min_games: Minimum number of games required for an archetype to be considered
            
        Returns:
            List of archetype data dictionaries
        """
        meta_data = self._load_meta_data()
        
        # Filter archetypes with enough games
        valid_archetypes = [
            (archetype, data) for archetype, data in meta_data["archetypes"].items()
            if data["games"] >= min_games
        ]
        
        # Sort by win rate (descending)
        valid_archetypes.sort(key=lambda x: x[1]["win_rate"], reverse=True)
        
        # Return limited number of results
        return [
            {"archetype": archetype, **data}
            for archetype, data in valid_archetypes[:limit]
        ]
    
    def get_top_cards(self, limit: int = 20, min_games: int = 5) -> List[Dict]:
        """
        Get the top performing cards.
        
        Args:
            limit: Maximum number of cards to return
            min_games: Minimum number of games required for a card to be considered
            
        Returns:
            List of card data dictionaries
        """
        meta_data = self._load_meta_data()
        
        # Filter cards with enough games
        valid_cards = [
            (card_name, data) for card_name, data in meta_data["cards"].items()
            if data["games"] >= min_games
        ]
        
        # Sort by win rate (descending)
        valid_cards.sort(key=lambda x: x[1]["win_rate"], reverse=True)
        
        # Return limited number of results
        return [
            {"card_name": card_name, **data}
            for card_name, data in valid_cards[:limit]
        ]
    
    def get_archetype_matchups(self, archetype: str) -> Dict[str, float]:
        """
        Get matchup win rates for a specific archetype.
        
        Args:
            archetype: The archetype to get matchups for
            
        Returns:
            Dictionary mapping opponent archetypes to win rates
        """
        meta_data = self._load_meta_data()
        matchups = {}
        
        # Look for all matchups involving this archetype
        for key, data in meta_data["matchups"].items():
            if key.startswith(f"{archetype}_vs_"):
                # Extract opponent archetype
                opponent = key.split("_vs_")[1]
                matchups[opponent] = data["win_rate"]
                
        return matchups
    
    def get_card_prevalence_by_archetype(self, card_name: str) -> Dict[str, float]:
        """
        Get the prevalence of a card across different archetypes.
        
        Args:
            card_name: The card name to analyze
            
        Returns:
            Dictionary mapping archetypes to prevalence
        """
        meta_data = self._load_meta_data()
        
        if card_name not in meta_data["cards"]:
            return {}
            
        card_data = meta_data["cards"][card_name]
        total_appearances = sum(card_data["archetypes"].values())
        
        if total_appearances == 0:
            return {}
            
        # Calculate prevalence percentages
        return {
            archetype: count / total_appearances
            for archetype, count in card_data["archetypes"].items()
        }
    
    def get_meta_snapshot(self) -> Dict:
        """
        Get a snapshot of the current metagame.
        
        Returns:
            Dictionary with metagame overview statistics
        """
        meta_data = self._load_meta_data()
        
        return {
            "total_games": meta_data["total_games"],
            "last_updated": meta_data.get("last_updated"),
            "top_archetypes": self.get_top_archetypes(),
            "top_cards": self.get_top_cards(),
            "archetype_distribution": {
                archetype: _deck_seat_share(
                    data["games"], meta_data["total_games"])
                for archetype, data in meta_data["archetypes"].items()
                if data["games"] >= 5  # Filter for relevant archetypes
            }
        }
    
    # === Deck Recommendation Methods ===
    
    def suggest_card_replacements(self, deck: List[int], cards_to_replace: List[int]) -> Dict[int, List[Tuple[int, float]]]:
        """
        Suggest replacement cards for cards in a deck.
        
        Args:
            deck: List of card IDs in the deck
            cards_to_replace: List of card IDs to find replacements for
            
        Returns:
            Dictionary mapping card IDs to replace to list of (replacement_id, score) tuples
        """
        recommendations = {}
        
        # Get all decks from storage to form a card pool
        deck_files = self._get_all_deck_files()
        card_pool = set()
        
        for file_path in deck_files:
            deck_data = self.load(file_path)
            if deck_data and "card_list" in deck_data:
                for card in deck_data["card_list"]:
                    card_pool.add(card["id"])
        
        # For each card to replace, find suitable replacements
        for card_id in cards_to_replace:
            # Calculate synergy with the rest of the deck
            remaining_deck = [c for c in deck if c != card_id]
            
            # Calculate CMC and color requirements of the card to replace
            card = self.card_db.get(card_id)
            if not card:
                continue
                
            target_cmc = getattr(card, 'cmc', 0)
            target_colors = getattr(card, 'colors', [0, 0, 0, 0, 0])
            target_types = getattr(card, 'card_types', [])
            
            # Score potential replacements
            replacement_scores = []
            
            for candidate_id in card_pool:
                if candidate_id in deck:
                    continue  # Skip cards already in the deck
                    
                candidate = self.card_db.get(candidate_id)
                if not candidate:
                    continue
                    
                # Base score starts at 0
                score = 0.0
                
                # CMC similarity (closer is better)
                cmc_diff = abs(target_cmc - getattr(candidate, 'cmc', 0))
                cmc_score = max(0, 1 - (cmc_diff / 3))  # Normalize to 0-1
                score += cmc_score * 0.3  # 30% weight to CMC
                
                # Color compatibility
                candidate_colors = getattr(candidate, 'colors', [0, 0, 0, 0, 0])
                color_match = sum(1 for i in range(min(len(target_colors), len(candidate_colors))) 
                                if target_colors[i] > 0 and candidate_colors[i] > 0)
                color_score = color_match / max(1, sum(1 for c in target_colors if c > 0))
                score += color_score * 0.2  # 20% weight to color
                
                # Type compatibility
                candidate_types = getattr(candidate, 'card_types', [])
                type_match = len(set(target_types) & set(candidate_types))
                type_score = type_match / max(1, len(target_types))
                score += type_score * 0.2  # 20% weight to type
                
                # Synergy with remaining deck
                synergy_scores = [
                    self.calculate_synergy_score(candidate_id, other_id)
                    for other_id in remaining_deck
                ]
                avg_synergy = sum(synergy_scores) / max(1, len(synergy_scores))
                score += avg_synergy * 0.3  # 30% weight to synergy
                
                replacement_scores.append((candidate_id, score))
            
            # Sort by score (highest first) and take top 5
            replacement_scores.sort(key=lambda x: x[1], reverse=True)
            recommendations[card_id] = replacement_scores[:5]
            
        return recommendations
    
    def optimize_deck(self, deck: List[int], optimization_goal: str = "win_rate") -> Dict:
        """
        Suggest optimizations for a deck.
        
        Args:
            deck: List of card IDs in the deck
            optimization_goal: Target metric for optimization ("win_rate", "consistency", "synergy")
            
        Returns:
            Dictionary with optimization suggestions
        """
        # Analyze the current deck
        deck_stats = self._analyze_deck(deck)
        
        # Identify underperforming cards
        underperforming = []
        meta_data = self._load_meta_data()
        
        for card_id in deck:
            # Get card name
            card_name = self._get_card_name(card_id)
            if not card_name:
                continue
                
            # Check meta data for this card
            if card_name in meta_data["cards"]:
                card_data = meta_data["cards"][card_name]
                
                # Check if card is underperforming
                if card_data["games"] >= 5:  # Minimum sample size
                    if optimization_goal == "win_rate" and card_data["win_rate"] < 0.45:
                        underperforming.append(card_id)
                    elif optimization_goal == "consistency" and card_data["play_rate"] < 0.3:
                        underperforming.append(card_id)
            
            # If not enough meta data, check synergy
            else:
                if optimization_goal == "synergy":
                    # Calculate synergy with rest of deck
                    other_cards = [c for c in deck if c != card_id]
                    synergy_scores = [
                        self.calculate_synergy_score(card_id, other_id)
                        for other_id in other_cards
                    ]
                    avg_synergy = sum(synergy_scores) / max(1, len(synergy_scores))
                    
                    if avg_synergy < 0.3:  # Low synergy threshold
                        underperforming.append(card_id)
        
        # Get recommendations for underperforming cards
        replacements = self.suggest_card_replacements(deck, underperforming)
        
        # Adjust deck composition (mana curve, color balance)
        compositional_suggestions = self._get_compositional_suggestions(deck)
        
        return {
            "current_stats": deck_stats,
            "underperforming_cards": [
                {"id": card_id, "name": self._get_card_name(card_id) or f"Card {card_id}"}
                for card_id in underperforming
            ],
            "replacement_suggestions": {
                str(card_id): [
                    {"id": rep_id, "name": self._get_card_name(rep_id) or f"Card {rep_id}", "score": score}
                    for rep_id, score in reps
                ]
                for card_id, reps in replacements.items()
            },
            "compositional_suggestions": compositional_suggestions
        }
    
    def _analyze_deck(self, deck: List[int]) -> Dict:
        """
        Analyze a deck's characteristics and performance.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with deck statistics
        """
        # Initialize stats
        stats = {
            "card_count": len(deck),
            "mana_curve": {},
            "color_distribution": [0, 0, 0, 0, 0],  # WUBRG
            "card_types": {},
            "synergy_score": 0,
            "meta_position": {}
        }
        
        # Calculate mana curve
        for card_id in deck:
            card = self.card_db.get(card_id)
            if not card:
                continue
                
            # Update mana curve
            cmc = getattr(card, 'cmc', 0)
            cmc_str = str(int(cmc))
            if cmc_str not in stats["mana_curve"]:
                stats["mana_curve"][cmc_str] = 0
            stats["mana_curve"][cmc_str] += 1
            
            # Update color distribution
            if hasattr(card, 'colors'):
                for i, color in enumerate(card.colors):
                    stats["color_distribution"][i] += color
                    
            # Update card types
            if hasattr(card, 'card_types'):
                for card_type in card.card_types:
                    if card_type not in stats["card_types"]:
                        stats["card_types"][card_type] = 0
                    stats["card_types"][card_type] += 1
        
        # Calculate overall synergy score
        stats["synergy_score"] = self.calculate_deck_synergy_score(deck)
        
        # Determine meta position
        deck_fingerprint = self.get_deck_fingerprint(deck)
        deck_archetype = self.identify_archetype(deck)
        
        # Check if we have meta data for this archetype
        meta_data = self._load_meta_data()
        if deck_archetype in meta_data["archetypes"]:
            archetype_data = meta_data["archetypes"][deck_archetype]
            stats["meta_position"]["archetype_win_rate"] = archetype_data["win_rate"]
            stats["meta_position"]["archetype_popularity"] = \
                _deck_seat_share(
                    archetype_data["games"], meta_data["total_games"])
            
            # Get matchups
            stats["meta_position"]["matchups"] = self.get_archetype_matchups(deck_archetype)
        
        return stats
    
    def _get_compositional_suggestions(self, deck: List[int]) -> Dict:
        """
        Get suggestions for adjusting deck composition.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with compositional suggestions
        """
        suggestions = {}
        deck_stats = self._analyze_deck(deck)
        
        # Check mana curve
        curve = deck_stats["mana_curve"]
        curve_int = {int(k): v for k, v in curve.items()}
        
        # Ideal curve roughly follows a bell shape centered on 2-3 CMC
        ideal_curve = {
            0: 0.05,  # 5% 0-drops
            1: 0.15,  # 15% 1-drops
            2: 0.25,  # 25% 2-drops
            3: 0.25,  # 25% 3-drops
            4: 0.15,  # 15% 4-drops
            5: 0.10,  # 10% 5-drops
            6: 0.05   # 5% 6+ drops
        }
        
        curve_suggestions = []
        for cmc, ideal_ratio in ideal_curve.items():
            current_count = curve_int.get(cmc, 0)
            ideal_count = int(ideal_ratio * len(deck))
            diff = ideal_count - current_count
            
            if diff > 2:  # Need significantly more
                curve_suggestions.append(f"Add {diff} more {cmc}-cost cards")
            elif diff < -2:  # Need significantly fewer
                curve_suggestions.append(f"Remove {-diff} {cmc}-cost cards")
                
        if curve_suggestions:
            suggestions["mana_curve"] = curve_suggestions
            
        # Check color balance
        colors = ["white", "blue", "black", "red", "green"]
        color_distr = deck_stats["color_distribution"]
        total_colors = sum(color_distr)
        
        if total_colors > 0:
            color_ratios = [count / total_colors for count in color_distr]
            
            # Check if any color is very underrepresented or overrepresented
            color_suggestions = []
            for i, ratio in enumerate(color_ratios):
                if ratio > 0.4:  # Color is dominant
                    color_suggestions.append(f"Consider reducing {colors[i]} concentration")
                elif 0 < ratio < 0.1:  # Color is splashed
                    color_suggestions.append(f"Consider either strengthening or removing {colors[i]}")
                    
            if color_suggestions:
                suggestions["color_balance"] = color_suggestions
                
        # Check card type distribution
        card_types = deck_stats["card_types"]
        total_cards = sum(card_types.values())
        
        # Land count check
        land_count = card_types.get("land", 0)
        land_ratio = land_count / max(1, total_cards)
        
        if land_ratio < 0.33:
            suggestions["land_count"] = [f"Add more lands (currently {land_count}, aim for at least {int(0.38 * total_cards)})"]
        elif land_ratio > 0.45:
            suggestions["land_count"] = [f"Consider reducing land count (currently {land_count}, try {int(0.38 * total_cards)})"]
            
        # Creature/spell balance
        creature_count = card_types.get("creature", 0)
        creature_ratio = creature_count / max(1, total_cards - land_count)
        
        if creature_ratio < 0.3:
            suggestions["creature_count"] = [f"Add more creatures (currently {creature_count}, aim for at least {int(0.4 * (total_cards - land_count))})"]
        elif creature_ratio > 0.7:
            suggestions["creature_count"] = [f"Add more non-creature spells (currently {total_cards - land_count - creature_count})"]
        
        return suggestions
    
    # === Deck Stats Collection Methods ===
    
    def get_deck_stats(self, deck_key: str) -> Dict:
        """Get statistics for a specific deck"""
        # Check cache first
        cached_stats = self.cache_get(f"deck:{deck_key}")
        if cached_stats:
            return cached_stats
            
        # First try to find the deck by name in our name-to-id mapping
        deck_id = self.deck_name_to_id.get(deck_key, deck_key)
        
        # Try to load using the deck_id
        stats = self.load(f"decks/{deck_id}.json")
        
        # If not found, try to find files with the deck name or key in the filename
        if not stats:
            deck_files = self._get_all_deck_files()
            for file_path in deck_files:
                # Check if the file contains the deck name or key
                file_name = os.path.basename(file_path)
                if deck_key.lower() in file_name.lower():
                    deck_data = self.load(file_path)
                    if deck_data:
                        stats = deck_data
                        # Update mapping
                        if "name" in deck_data and "deck_id" in deck_data:
                            self.deck_name_to_id[deck_data["name"]] = deck_data["deck_id"]
                        break
        
        if stats:
            # Validate and repair if needed
            valid, errors = self.validate_deck_stats(stats)
            if not valid:
                logging.warning(f"Invalid deck stats for {deck_key}: {errors}")
                stats = self.repair_deck_stats(stats)
                
            # Update cache
            self.cache_set(f"deck:{deck_key}", stats)
            
        return stats or {}
    
    def update_deck_stats(self, deck_key: str, update_data: Dict) -> bool:
        """Update statistics for a specific deck with data validation."""
        with self.batch_lock:
            # Get current stats (or initialize if not present)
            current_stats = self.get_deck_stats(deck_key)
            
            # Validate data types before updating
            update_data = self._validate_stats_types(update_data)
            outcome_fields = ("wins", "losses", "draws")
            if any(field in update_data for field in outcome_fields):
                for field in outcome_fields:
                    current_stats.setdefault(field, 0)
            
            for key, value in update_data.items():
                if key == "card_list":
                    # Set static deck composition with enhanced data
                    if "card_list" not in current_stats or not current_stats["card_list"]:
                        enhanced_card_list = []
                        for card in value:
                            enhanced_card = card.copy()  # Copy the card data
                            
                            # Always ensure CMC data is present
                            if "cmc" not in enhanced_card and "id" in enhanced_card:
                                card_id = enhanced_card["id"]
                                if card_id in self.card_db:
                                    card_obj = self.card_db[card_id]
                                    if hasattr(card_obj, 'cmc'):
                                        enhanced_card["cmc"] = card_obj.cmc
                                    else:
                                        # Default CMC if not found
                                        enhanced_card["cmc"] = 0
                                else:
                                    # Default CMC if card not in database
                                    enhanced_card["cmc"] = 0
                            
                            enhanced_card_list.append(enhanced_card)
                            
                        current_stats["card_list"] = enhanced_card_list
                    
                    # Also ensure existing card list has CMC data
                    elif "card_list" in current_stats:
                        for card in current_stats["card_list"]:
                            if "cmc" not in card and "id" in card:
                                card_id = card["id"]
                                if card_id in self.card_db:
                                    card_obj = self.card_db[card_id]
                                    if hasattr(card_obj, 'cmc'):
                                        card["cmc"] = card_obj.cmc
                                    else:
                                        card["cmc"] = 0
                                else:
                                    card["cmc"] = 0
                        
                    # Update cumulative usage in a separate field "card_usage"
                    if "card_usage" not in current_stats:
                        current_stats["card_usage"] = {}
                    for new_card in value:
                        card_id = new_card["id"]
                        # Increment usage count for each card.
                        current_stats["card_usage"][card_id] = current_stats["card_usage"].get(card_id, 0) + new_card["count"]
                elif isinstance(value, dict) and isinstance(current_stats.get(key, {}), dict):
                    if key not in current_stats:
                        current_stats[key] = {}
                    current_stats[key].update(value)
                elif isinstance(value, list) and isinstance(current_stats.get(key, []), list):
                    # For lists other than "card_list", merge without duplication.
                    if key not in current_stats:
                        current_stats[key] = []
                    if key != "card_list":
                        existing_items = set(str(item) for item in current_stats[key])
                        for item in value:
                            if str(item) not in existing_items:
                                current_stats[key].append(item)
                                existing_items.add(str(item))
                else:
                    # For numeric fields, we add; for others, we replace.
                    if key in ["wins", "losses", "draws", "games", "total_turns"]:
                        current_stats[key] = current_stats.get(key, 0) + value
                    else:
                        current_stats[key] = value
            
            # Ensure consistency between total games and sum of outcomes
            if all(k in current_stats for k in ["wins", "losses", "draws"]):
                expected_games = current_stats["wins"] + current_stats["losses"] + current_stats["draws"]
                if current_stats.get("games", 0) != expected_games:
                    logging.info(f"Fixing inconsistent game count for {deck_key}: {current_stats.get('games', 0)} to {expected_games}")
                    current_stats["games"] = expected_games

            # Recalculate derived values after outcome reconciliation so draws
            # contribute half a win and corrected game totals are respected.
            games = current_stats.get("games", 0)
            if games > 0:
                current_stats["win_rate"] = (
                    current_stats.get("wins", 0)
                    + 0.5 * current_stats.get("draws", 0)
                ) / games
                if "total_turns" in current_stats:
                    current_stats["avg_game_length"] = (
                        current_stats["total_turns"] / games)
            elif "games" in current_stats:
                current_stats["win_rate"] = 0.0
            
            current_stats["last_updated"] = time.time()
                    
            # Update cache and batch updates.
            self.cache_set(f"deck:{deck_key}", current_stats)
            self.batch_updates[deck_key] = current_stats
            
            # Update mapping between deck names and IDs.
            if "name" in current_stats and "deck_id" in current_stats:
                self.deck_name_to_id[current_stats["name"]] = current_stats["deck_id"]
                self.deck_id_to_name[current_stats["deck_id"]] = current_stats["name"]
            
            return True

    def _replace_deck_stats(self, deck_key: str, stats: Dict) -> bool:
        """Store a fully accumulated deck snapshot without treating it as a delta.

        ``update_deck_stats`` is intentionally an additive public API. Internal
        game-recording paths already mutate a complete cached snapshot, so
        sending that snapshot back through the additive API doubles every
        cumulative counter (and compounds on each game).
        """
        with self.batch_lock:
            stats = self._validate_stats_types(stats)
            self.cache_set(f"deck:{deck_key}", stats)
            self.batch_updates[deck_key] = stats
            if "name" in stats and "deck_id" in stats:
                self.deck_name_to_id[stats["name"]] = stats["deck_id"]
                self.deck_id_to_name[stats["deck_id"]] = stats["name"]
            return True
        
    def get_win_rate_confidence_interval(self, deck_key: str, confidence: float = 0.95) -> Tuple[float, float]:
        """Calculate win rate confidence interval using Wilson score interval"""
        stats = self.get_deck_stats(deck_key)
        wins = stats.get("wins", 0)
        games = stats.get("games", 0)
        
        if games == 0:
            return 0.0, 0.0
            
        # Wilson score interval
        z = 1.96  # 95% confidence
        if confidence == 0.99:
            z = 2.576
        elif confidence == 0.90:
            z = 1.645
            
        p = wins / games
        denominator = 1 + (z**2 / games)
        center = (p + (z**2 / (2 * games))) / denominator
        interval = z * math.sqrt((p * (1 - p) + (z**2 / (4 * games))) / games) / denominator
        
        lower = max(0, center - interval)
        upper = min(1, center + interval)
        
        return lower, upper
    
    def get_card_stats(self, card_id: int) -> Dict:
        """
        Get aggregated statistics for a specific card across all decks.
        
        Args:
            card_id: The card ID to get statistics for
            
        Returns:
            Dictionary with card statistics
        """
        # Get card name
        card_name = self._get_card_name(card_id)
        if not card_name:
            return {
                "name": "Unknown Card",
                "games_played": 0,
                "wins": 0,
                "losses": 0,
                "usage_count": 0,
                "win_rate": 0
            }
        
        # Canonical aggregate files are keyed by ID. A legacy name file is
        # imported only when it identifies this card without ambiguity.
        card_file = self._card_stats_file(card_name, card_id)
        card_stats = self._load_individual_card_stats(
            card_id, card_name, persist_migration=True)
        if card_stats:
            return card_stats

        # Initialize default stats
        stats = {
            "id": card_id,
            "name": card_name,
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "usage_count": 0,
            "win_rate": 0,
            "games_drawn": 0,
            "wins_when_drawn": 0.0,
            "games_not_drawn": 0,
            "wins_when_not_drawn": 0.0,
            "games_in_opening_hand": 0,
            "wins_when_in_opening_hand": 0.0,
            "drawn_win_rate": 0,
            "not_drawn_win_rate": 0,
            "opening_hand_win_rate": 0,
            "performance_by_turn": {},
            "performance_by_position": {
                "ahead": {"wins": 0, "losses": 0, "played": 0},
                "parity": {"wins": 0, "losses": 0, "played": 0},
                "behind": {"wins": 0, "losses": 0, "played": 0}
            }
        }

        # Name-keyed meta data is also legacy identity. It is safe only when
        # the active card corpus has a single canonical ID for this name.
        meta_data = self._load_meta_data()
        meta_card_stats = meta_data.get("cards", {}).get(card_name)
        used_meta = (
            isinstance(meta_card_stats, dict)
            and self._matching_card_ids(card_name) == {str(card_id)})
        if used_meta:
            stats["games_played"] = meta_card_stats.get("games", 0)
            stats["wins"] = meta_card_stats.get("wins", 0)
            stats["losses"] = meta_card_stats.get("losses", 0)
            stats["draws"] = meta_card_stats.get("draws", 0)
            stats["usage_count"] = meta_card_stats.get("usage_count", 0)
            stats["archetypes"] = meta_card_stats.get("archetypes", {})
        else:
            # Rebuild from canonical per-deck records. A legacy name record is
            # used only if that deck composition maps the name to this one ID.
            for file_path in self._get_all_deck_files():
                deck_stats = self.load(file_path)
                if not deck_stats or not (
                        "card_performance" in deck_stats
                        or "card_performance_by_name" in deck_stats):
                    continue

                card_str = str(card_id)
                card_performance = deck_stats.get("card_performance", {})
                card_perf = card_performance.get(card_str)
                if card_perf is None:
                    legacy = deck_stats.get("card_performance_by_name", {})
                    candidate = legacy.get(card_name)
                    ids_for_name = {
                        str(entry.get("id"))
                        for entry in deck_stats.get("card_list", [])
                        if (isinstance(entry, dict)
                            and str(entry.get("name", "")).casefold()
                            == str(card_name).casefold())
                    }
                    if ids_for_name == {card_str}:
                        card_perf = candidate
                if not isinstance(card_perf, dict):
                    continue

                stats["games_played"] += card_perf.get("games_played", 0)
                stats["wins"] += card_perf.get("wins", 0)
                stats["losses"] += card_perf.get("losses", 0)
                stats["draws"] += card_perf.get("draws", 0)
                stats["usage_count"] += card_perf.get("usage_count", 0)
                stats["games_drawn"] += card_perf.get(
                    "games_drawn", 0)
                stats["wins_when_drawn"] += card_perf.get(
                    "wins_when_drawn", 0)
                stats["games_not_drawn"] += card_perf.get(
                    "games_not_drawn", 0)
                stats["wins_when_not_drawn"] += card_perf.get(
                    "wins_when_not_drawn", 0)
                stats["games_in_opening_hand"] += card_perf.get(
                    "games_in_opening_hand", 0)
                stats["wins_when_in_opening_hand"] += card_perf.get(
                    "wins_when_in_opening_hand", 0)

                for position in ["ahead", "parity", "behind"]:
                    if position in card_perf.get(
                            "performance_by_position", {}):
                        pos_stats = card_perf[
                            "performance_by_position"][position]
                        aggregate = stats[
                            "performance_by_position"][position]
                        aggregate["wins"] += pos_stats.get("wins", 0)
                        aggregate["losses"] += pos_stats.get("losses", 0)
                        aggregate["played"] += pos_stats.get("played", 0)

                for turn, turn_stats in card_perf.get(
                        "performance_by_turn", {}).items():
                    aggregate = stats["performance_by_turn"].setdefault(
                        turn, {"wins": 0, "losses": 0, "played": 0})
                    aggregate["wins"] += turn_stats.get("wins", 0)
                    aggregate["losses"] += turn_stats.get("losses", 0)
                    aggregate["played"] += turn_stats.get("played", 0)
        
        # Calculate aggregate win rates
        if stats["games_played"] > 0:
            stats["win_rate"] = (
                stats["wins"] + 0.5 * stats["draws"]
            ) / stats["games_played"]
        if stats["games_drawn"] > 0:
            stats["drawn_win_rate"] = (
                stats["wins_when_drawn"] / stats["games_drawn"])
        if stats["games_not_drawn"] > 0:
            stats["not_drawn_win_rate"] = (
                stats["wins_when_not_drawn"]
                / stats["games_not_drawn"])
        if stats["games_in_opening_hand"] > 0:
            stats["opening_hand_win_rate"] = (
                stats["wins_when_in_opening_hand"]
                / stats["games_in_opening_hand"])
        
        self._individual_card_cache[card_file] = stats
        if self.save(card_file, stats):
            self._dirty_individual_card_files.discard(card_file)
        else:
            self._dirty_individual_card_files.add(card_file)
        
        return stats
    
    def record_game(self, winner_deck: List[int], loser_deck: List[int], 
                    card_db: Dict, turn_count: int, cards_played: Dict = None, 
                    winner_life: int = 20, winner_deck_name: str = None, 
                    loser_deck_name: str = None, is_draw: bool = False,
                    game_stage: str = None, game_state: Union[str, GamePosition] = "parity", 
                    mulligan_data: Dict = None, opening_hands: Dict = None,
                    draw_history: Dict = None, play_order: Dict = None,
                    play_history: Dict = None,
                    winner_archetype: str = None,
                    loser_archetype: str = None) -> bool:
        """Record a game result with comprehensive error handling and additional tracking"""
        try:
            # Initialize card database if needed
            if not self.card_db and card_db:
                self.card_db = card_db
                self._initialize_mappings()
            
            # Generate fingerprints with error handling
            try:
                winner_deck_fingerprint = self.get_deck_fingerprint(winner_deck, winner_deck_name)
            except Exception as e:
                logging.error(f"Error generating winner deck fingerprint: {e}")
                winner_deck_fingerprint = hashlib.md5(str(winner_deck).encode()).hexdigest()
                
            try:
                loser_deck_fingerprint = self.get_deck_fingerprint(loser_deck, loser_deck_name)
            except Exception as e:
                logging.error(f"Error generating loser deck fingerprint: {e}")
                loser_deck_fingerprint = hashlib.md5(str(loser_deck).encode()).hexdigest()
            
            # Determine archetypes safely
            if winner_archetype is None:
                try:
                    winner_archetype = self.identify_archetype(winner_deck)
                except Exception as e:
                    logging.error(f"Error identifying winner archetype: {e}")
                    winner_archetype = "midrange"  # Default fallback value
            winner_archetype = str(
                getattr(winner_archetype, "value", winner_archetype)
                or "midrange").strip().lower() or "midrange"
                
            if loser_archetype is None:
                try:
                    loser_archetype = self.identify_archetype(loser_deck)
                except Exception as e:
                    logging.error(f"Error identifying loser archetype: {e}")
                    loser_archetype = "midrange"  # Default fallback value
            loser_archetype = str(
                getattr(loser_archetype, "value", loser_archetype)
                or "midrange").strip().lower() or "midrange"
            
            # Determine game stage and state
            # Handle game stage conversion
            if not game_stage:
                game_stage = "late" if turn_count >= 8 else "mid" if turn_count >= 4 else "early"
            
            # Convert string game stage to enum if needed
            if isinstance(game_stage, str):
                try:
                    game_stage = GameStage(game_stage)
                except ValueError:
                    game_stage = (GameStage.LATE if turn_count >= 8 
                                else GameStage.MID if turn_count >= 4 
                                else GameStage.EARLY)
            
            # Handle game state conversion
            if isinstance(game_state, str):
                try:
                    game_state = GamePosition(game_state)
                except ValueError:
                    game_state = GamePosition.PARITY  # Default to parity if invalid
            
            # Default mulligan data if not provided
            if mulligan_data is None:
                mulligan_data = {
                    "winner": 0,
                    "loser": 0
                }
            
            # Default opening hand data if not provided
            if opening_hands is None:
                opening_hands = {
                    "winner": [],
                    "loser": []
                }
            
            # Default draw history if not provided
            if draw_history is None:
                draw_history = {
                    "winner": {},
                    "loser": {}
                }
            
            # Default play order if not provided
            if play_order is None:
                play_order = {"first_player": "unknown"}
            first_player = play_order.get("first_player")
            winner_play_order = (
                True if first_player == "winner"
                else False if first_player == "loser" else None)
            loser_play_order = (
                True if first_player == "loser"
                else False if first_player == "winner" else None)
            loser_game_state = _opposing_position(game_state)
            
            # Record game result
            game_result_success = self.update_meta_with_game_result(
                winner_deck=winner_deck,
                loser_deck=loser_deck,
                winner_archetype=winner_archetype,
                loser_archetype=loser_archetype,
                cards_played=cards_played or {0: [], 1: []},
                turn_count=turn_count,
                is_draw=is_draw
            )
            
            # In a draw, both decks are technically winners or losers, but for our tracking we'll just update both
            # with accurate is_draw flag rather than treating either as a definitive winner or loser
            success_1 = self._update_deck_stats(
                deck_id=winner_deck_fingerprint,
                deck=winner_deck,
                archetype=winner_archetype,
                is_winner=True if not is_draw else False,
                is_draw=is_draw,
                turn_count=turn_count,
                game_stage=game_stage,
                game_state=game_state,
                deck_name=winner_deck_name,
                mulligan_count=mulligan_data.get("winner", 0),
                opening_hand=opening_hands.get("winner", []),
                draw_history=draw_history.get("winner", {}),
                play_order=winner_play_order
            )
            
            success_2 = self._update_deck_stats(
                deck_id=loser_deck_fingerprint,
                deck=loser_deck,
                archetype=loser_archetype,
                is_winner=False,
                is_draw=is_draw,
                turn_count=turn_count,
                game_stage=game_stage,
                game_state=loser_game_state,
                deck_name=loser_deck_name,
                mulligan_count=mulligan_data.get("loser", 0),
                opening_hand=opening_hands.get("loser", []),
                draw_history=draw_history.get("loser", {}),
                play_order=loser_play_order
            )
            
            # Update card statistics, passing draw information
            success_3 = self._update_card_stats(
                winner_deck_id=winner_deck_fingerprint,
                loser_deck_id=loser_deck_fingerprint,
                cards_played=cards_played or {0: [], 1: []},
                game_stage=game_stage,
                game_state=game_state,
                is_draw=is_draw,
                opening_hands=opening_hands,
                draw_history=draw_history,
                play_order=play_order,
                play_history=play_history
            )
            
            # Training workers batch the many small compressed card/deck files;
            # direct callers retain the historical immediate-persistence default.
            self._last_record_flush_succeeded = None
            self._games_since_persistence += 1
            if self._games_since_persistence >= \
                    self.persistence_interval_games:
                self._last_record_flush_succeeded = bool(
                    self.save_updates_sync())
                if not self._last_record_flush_succeeded:
                    logging.error(
                        "Game analytics were accepted but their scheduled "
                        "flush failed; dirty updates remain queued")
            
            if is_draw:
                logging.info(f"Game recorded: Draw between {winner_archetype} and {loser_archetype}, Turns: {turn_count}")
            else:
                logging.info(f"Game recorded: {winner_archetype} (W) vs {loser_archetype} (L), Turns: {turn_count}")
            
            return (
                game_result_success and success_1 and success_2 and success_3)
        
        except Exception as e:
            logging.error(f"Error recording game statistics: {e}")
            import traceback
            logging.error(traceback.format_exc())
        return False
        
    def _extract_deck_name_from_files(self, deck_id: str, fallback_archetype: str) -> str:
        """
        Extract a meaningful deck name from various potential sources.
        
        Args:
            deck_id: Unique identifier for the deck
            fallback_archetype: Archetype to use if no other name is found
        
        Returns:
            A cleaned and meaningful deck name
        """
        # Try using predefined deck names (if set during reset)
        if hasattr(self, 'current_deck_name_p1'):
            return self._sanitize_deck_name(self.current_deck_name_p1)
        
        # Try extracting name from filename
        try:
            # Split filename and remove extensions
            filename_parts = deck_id.split('_')
            
            # Look for meaningful parts before "Deck" or archetype
            meaningful_parts = [part for part in filename_parts 
                                if part.lower() not in ['deck', fallback_archetype.lower(), 
                                                        'combo', 'aggro', 'control', 'midrange']]
            
            # Join meaningful parts or use archetype as fallback
            if meaningful_parts:
                return self._sanitize_deck_name(' '.join(meaningful_parts))
        except Exception as e:
            logging.warning(f"Error extracting deck name from filename: {e}")
        
        # Fallback to archetype-based name
        return f"{fallback_archetype.title()} Deck"
    
    def _sanitize_deck_name(self, name: str) -> str:
        """
        Sanitize deck name, removing non-meaningful parts.
        
        Args:
            name: Original deck name
        
        Returns:
            str: Cleaned, meaningful deck name
        """
        # Remove common suffixes and prefixes
        name = re.sub(r'^(My\s*)?', '', name, flags=re.IGNORECASE)
        name = re.sub(r'(Deck)?$', '', name, flags=re.IGNORECASE)
        
        # Remove numeric identifiers
        name = re.sub(r'\d+$', '', name)
        
        # Remove extra whitespace
        name = name.strip()
        
        # Capitalize
        name = name.title()
        
        # Use a default if name becomes empty
        return name if name else "Unnamed Deck"
    
    def _update_deck_stats(self, deck_id: str, deck: List[int], archetype: str,
                            is_winner: bool, turn_count: int,
                            game_stage: GameStage, game_state: GamePosition,
                            deck_name: str = None, is_draw: bool = False,
                            mulligan_count: int = 0, opening_hand: List[int] = None,
                            draw_history: Dict = None,
                            play_order: Optional[bool] = None) -> bool:
        """
        Update statistics for a deck with enhanced tracking for mulligans and game progression.
        (Removed time.time() update for last_updated)
        """
        # (Get current stats logic remains)
        stats = self.get_deck_stats(deck_id)
        if not stats:
            # (Initialize new stats logic remains, but without last_updated time)
            card_list = []
            for card_id in set(deck):
                count = deck.count(card_id)
                card_name = self._get_card_name(card_id)
                card_list.append({
                    "id": card_id,
                    "name": card_name or f"Card {card_id}",
                    "count": count
                })
            if deck_name is None:
                deck_name = f"{archetype.title()} Deck"
            stats = {
                "name": deck_name,
                "deck_id": deck_id,
                "archetype": archetype,
                "card_list": card_list,
                "games": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "total_turns": 0,
                "win_rate": 0,
                "mulligan_stats": {
                    "total_mulligans": 0,
                    "games_with_mulligan": 0,
                    "win_rate_with_mulligan": 0,
                    "win_rate_without_mulligan": 0,
                    "avg_mulligans": 0
                },
                "play_order_stats": {
                    "play_first": {"games": 0, "wins": 0, "draws": 0},
                    "play_second": {"games": 0, "wins": 0, "draws": 0}
                },
                "performance_by_stage": {
                    "early": {"wins": 0, "losses": 0, "draws": 0},
                    "mid": {"wins": 0, "losses": 0, "draws": 0},
                    "late": {"wins": 0, "losses": 0, "draws": 0}
                },
                "performance_by_position": {
                    "ahead": {"wins": 0, "losses": 0, "draws": 0},
                    "parity": {"wins": 0, "losses": 0, "draws": 0},
                    "behind": {"wins": 0, "losses": 0, "draws": 0}
                },
                "performance_by_turn": {},
                "matchups": {},
                "specific_matchups": {},
                "card_performance": {},
                "meta_position": {},
                # "last_updated": time.time() # Removed time dependency
            }
        else:
            # (Ensure fields exist logic remains the same)
            if "draws" not in stats: stats["draws"] = 0
            if "performance_by_stage" in stats:
                for stage_data in stats["performance_by_stage"].values():
                    if "draws" not in stage_data: stage_data["draws"] = 0
            else: # Ensure structure exists
                stats["performance_by_stage"] = {"early": {"wins": 0,"losses": 0,"draws": 0}, "mid": {"wins": 0,"losses": 0,"draws": 0}, "late": {"wins": 0,"losses": 0,"draws": 0}}
            if "mulligan_stats" not in stats:
                 stats["mulligan_stats"] = {"total_mulligans": 0, "games_with_mulligan": 0, "win_rate_with_mulligan": 0, "win_rate_without_mulligan": 0, "avg_mulligans": 0}
            if "play_order_stats" not in stats:
                 stats["play_order_stats"] = {"play_first": {"games": 0, "wins": 0, "draws": 0}, "play_second": {"games": 0, "wins": 0, "draws": 0}}
            if "performance_by_turn" not in stats: stats["performance_by_turn"] = {}
            if "specific_matchups" not in stats: stats["specific_matchups"] = {}
            if "performance_by_position" not in stats: # Ensure structure exists
                 stats["performance_by_position"] = {"ahead": {"wins": 0,"losses": 0,"draws": 0}, "parity": {"wins": 0,"losses": 0,"draws": 0}, "behind": {"wins": 0,"losses": 0,"draws": 0}}
            else: # Add draws if missing
                for state_data in stats["performance_by_position"].values():
                    if "draws" not in state_data: state_data["draws"] = 0

            if deck_name is not None and deck_name != stats.get("name", ""): stats["name"] = deck_name

        # The caller may own a more authoritative classification than an old
        # persisted snapshot. Keep deck and individual-card buckets aligned.
        stats["archetype"] = archetype
        # Update dynamic game statistics.
        stats["games"] += 1
        if is_draw:
            stats["draws"] += 1
        elif is_winner:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        stats["total_turns"] += turn_count
        stats["avg_game_length"] = (
            stats["total_turns"] / max(1, stats["games"]))
        # Update win_rate to account for draws (0.5 points per draw)
        if stats["games"] > 0: # Avoid division by zero
             stats["win_rate"] = (stats["wins"] + 0.5 * stats["draws"]) / stats["games"]
        else: stats["win_rate"] = 0.0

        # stats["last_updated"] = time.time() # Removed time dependency

        # (Rest of the update logic for mulligan, play order, stage, turn, position remains)
        # Update mulligan statistics
        if mulligan_count > 0:
            stats["mulligan_stats"]["total_mulligans"] += mulligan_count
            stats["mulligan_stats"]["games_with_mulligan"] += 1

        games_with_mulligan = stats["mulligan_stats"]["games_with_mulligan"]
        wins_with_mulligan = stats["mulligan_stats"].get("wins_with_mulligan", 0)
        draws_with_mulligan = stats["mulligan_stats"].get("draws_with_mulligan", 0) # Need to track draws with mulligan
        if mulligan_count > 0:
             if is_winner: wins_with_mulligan += 1
             if is_draw: draws_with_mulligan += 1
        stats["mulligan_stats"]["wins_with_mulligan"] = wins_with_mulligan # Store cumulative wins/draws with mulligans
        stats["mulligan_stats"]["draws_with_mulligan"] = draws_with_mulligan # Store cumulative draws

        games_without_mulligan = stats["games"] - games_with_mulligan
        wins_without_mulligan = stats["mulligan_stats"].get("wins_without_mulligan", 0)
        draws_without_mulligan = stats["mulligan_stats"].get("draws_without_mulligan", 0) # Track draws without mulligan
        if mulligan_count == 0:
             if is_winner: wins_without_mulligan += 1
             if is_draw: draws_without_mulligan += 1
        stats["mulligan_stats"]["wins_without_mulligan"] = wins_without_mulligan
        stats["mulligan_stats"]["draws_without_mulligan"] = draws_without_mulligan

        # Calculate win rates including draws
        if games_with_mulligan > 0: stats["mulligan_stats"]["win_rate_with_mulligan"] = (wins_with_mulligan + 0.5 * draws_with_mulligan) / games_with_mulligan
        else: stats["mulligan_stats"]["win_rate_with_mulligan"] = 0.0
        if games_without_mulligan > 0: stats["mulligan_stats"]["win_rate_without_mulligan"] = (wins_without_mulligan + 0.5 * draws_without_mulligan) / games_without_mulligan
        else: stats["mulligan_stats"]["win_rate_without_mulligan"] = 0.0
        if stats["games"] > 0: stats["mulligan_stats"]["avg_mulligans"] = stats["mulligan_stats"]["total_mulligans"] / stats["games"]
        else: stats["mulligan_stats"]["avg_mulligans"] = 0.0

        # Play order stats
        play_position = (
            "play_first" if play_order is True
            else "play_second" if play_order is False else "unknown")
        if play_position not in stats["play_order_stats"]: # Ensure sub-dict exists
            stats["play_order_stats"][play_position] = {"games": 0, "wins": 0, "draws": 0}
        stats["play_order_stats"][play_position]["games"] += 1
        if is_draw: stats["play_order_stats"][play_position]["draws"] += 1
        elif is_winner: stats["play_order_stats"][play_position]["wins"] += 1

        # Perf by stage
        stage_key = game_stage.value
        if stage_key not in stats["performance_by_stage"]: # Ensure sub-dict exists
             stats["performance_by_stage"][stage_key] = {"wins": 0, "losses": 0, "draws": 0}
        if is_draw: stats["performance_by_stage"][stage_key]["draws"] += 1
        elif is_winner: stats["performance_by_stage"][stage_key]["wins"] += 1
        else: stats["performance_by_stage"][stage_key]["losses"] += 1

        # Perf by turn
        turn_key = str(turn_count)
        if turn_key not in stats["performance_by_turn"]: # Ensure sub-dict exists
            stats["performance_by_turn"][turn_key] = {"games": 0, "wins": 0, "losses": 0, "draws": 0}
        stats["performance_by_turn"][turn_key]["games"] += 1
        if is_draw: stats["performance_by_turn"][turn_key]["draws"] += 1
        elif is_winner: stats["performance_by_turn"][turn_key]["wins"] += 1
        else: stats["performance_by_turn"][turn_key]["losses"] += 1

        # Perf by position
        position_key = game_state.value
        if position_key not in stats["performance_by_position"]: # Ensure sub-dict exists
             stats["performance_by_position"][position_key] = {"wins": 0, "losses": 0, "draws": 0}
        if is_draw: stats["performance_by_position"][position_key]["draws"] += 1
        elif is_winner: stats["performance_by_position"][position_key]["wins"] += 1
        else: stats["performance_by_position"][position_key]["losses"] += 1

        # Opening hand stats
        if opening_hand:
            if "opening_hand_stats" not in stats: stats["opening_hand_stats"] = {}
            # Buckets represent deck-seat games containing a card, not the
            # number of physical copies of that card in one hand.
            for card_id in set(opening_hand):
                card_key = str(card_id)
                if card_key not in stats["opening_hand_stats"]:
                    stats["opening_hand_stats"][card_key] = {"games": 0, "wins": 0, "losses": 0, "draws": 0}
                stats["opening_hand_stats"][card_key]["games"] += 1
                if is_draw: stats["opening_hand_stats"][card_key]["draws"] += 1
                elif is_winner: stats["opening_hand_stats"][card_key]["wins"] += 1
                else: stats["opening_hand_stats"][card_key]["losses"] += 1

        # Draw history is recorded on the engine's alternating global turn.
        # Store it against this deck seat's actual turn count.
        player_draw_history = _player_turn_history(
            draw_history or {}, play_order)
        if player_draw_history:
            if "draw_history_stats" not in stats: stats["draw_history_stats"] = {}
            for turn, cards in player_draw_history.items():
                turn_key = str(turn)
                if turn_key not in stats["draw_history_stats"]: stats["draw_history_stats"][turn_key] = {}
                for card_id in set(cards):
                    card_key = str(card_id)
                    if card_key not in stats["draw_history_stats"][turn_key]:
                        stats["draw_history_stats"][turn_key][card_key] = {"games": 0, "wins": 0, "losses": 0, "draws": 0}
                    stats["draw_history_stats"][turn_key][card_key]["games"] += 1
                    if is_draw: stats["draw_history_stats"][turn_key][card_key]["draws"] += 1
                    elif is_winner: stats["draw_history_stats"][turn_key][card_key]["wins"] += 1
                    else: stats["draw_history_stats"][turn_key][card_key]["losses"] += 1

        # Update name mapping
        if "name" in stats:
            self.deck_name_to_id[stats["name"]] = deck_id
            self.deck_id_to_name[deck_id] = stats["name"]


        return self._replace_deck_stats(deck_id, stats)

    @staticmethod
    def _card_performance_template(card_name: str) -> Dict:
        """Return the canonical per-deck card-performance schema."""
        return {
            "name": card_name,
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "usage_count": 0,
            "win_rate": 0,
            "games_drawn": 0,
            "wins_when_drawn": 0,
            "games_not_drawn": 0,
            "wins_when_not_drawn": 0,
            "games_in_opening_hand": 0,
            "wins_when_in_opening_hand": 0,
            "performance_by_turn": {},
            "performance_by_stage": {
                "early": {
                    "wins": 0, "losses": 0, "draws": 0, "played": 0},
                "mid": {
                    "wins": 0, "losses": 0, "draws": 0, "played": 0},
                "late": {
                    "wins": 0, "losses": 0, "draws": 0, "played": 0},
            },
            "performance_by_position": {
                "ahead": {
                    "wins": 0, "losses": 0, "draws": 0, "played": 0},
                "parity": {
                    "wins": 0, "losses": 0, "draws": 0, "played": 0},
                "behind": {
                    "wins": 0, "losses": 0, "draws": 0, "played": 0},
            },
            "play_order_performance": {
                "play_first": {
                    "wins": 0, "losses": 0, "draws": 0, "played": 0},
                "play_second": {
                    "wins": 0, "losses": 0, "draws": 0, "played": 0},
            },
            "play_curve_stats": {
                "on_curve": {"games": 0, "wins": 0, "draws": 0},
                "under_curve": {"games": 0, "wins": 0, "draws": 0},
                "over_curve": {"games": 0, "wins": 0, "draws": 0},
            },
        }

    @classmethod
    def _merge_card_performance_defaults(
            cls, target: Dict, defaults: Dict) -> None:
        """Fill missing legacy fields without replacing accumulated values."""
        for key, default in defaults.items():
            if isinstance(default, dict):
                current = target.get(key)
                if not isinstance(current, dict):
                    target[key] = copy.deepcopy(default)
                else:
                    cls._merge_card_performance_defaults(current, default)
            elif key not in target:
                target[key] = copy.deepcopy(default)

    @staticmethod
    def _migrate_name_card_performance(
            deck_stats: Dict, composition: List[Dict]) -> None:
        """Move legacy name-keyed entries into the canonical ID map."""
        legacy = deck_stats.pop("card_performance_by_name", None)
        if not isinstance(legacy, dict):
            return
        canonical = deck_stats.setdefault("card_performance", {})
        ids_by_name = defaultdict(set)
        for card_entry in composition:
            card_name = card_entry.get("name")
            if card_name:
                ids_by_name[card_name].add(str(card_entry.get("id")))
        for card_name, card_keys in ids_by_name.items():
            if card_name not in legacy:
                continue
            if len(card_keys) != 1:
                logging.warning(
                    "Dropping ambiguous legacy card statistics for %r; "
                    "composition IDs=%s",
                    card_name, sorted(card_keys))
                continue
            card_key = next(iter(card_keys))
            if card_key not in canonical:
                canonical[card_key] = copy.deepcopy(legacy[card_name])

    @staticmethod
    def _record_card_outcome(bucket: Dict, outcome: str) -> None:
        field = {
            "win": "wins",
            "loss": "losses",
            "draw": "draws",
        }[outcome]
        if field in bucket:
            bucket[field] += 1

    def _update_deck_card_performance(
            self, deck_stats: Dict, composition: List[Dict],
            cards_played: List[int], opening_hand: List[int],
            draw_history: Dict, play_history: Dict,
            went_first: Optional[bool], game_stage: GameStage,
            game_state: GamePosition, outcome: str) -> bool:
        """Apply one deck seat's card telemetry to canonical ID records."""
        played = set(cards_played or ())
        opening = set(opening_hand or ())
        player_draw_history = _player_turn_history(
            draw_history or {}, went_first)
        player_play_history = _player_turn_history(
            play_history or {}, went_first)
        play_position = (
            "play_first" if went_first is True
            else "play_second" if went_first is False
            else "unknown")
        points = 1.0 if outcome == "win" else 0.5 if outcome == "draw" else 0.0
        success = True

        self._migrate_name_card_performance(deck_stats, composition)
        performance = deck_stats.setdefault("card_performance", {})

        for card_entry in composition:
            card_id = card_entry["id"]
            card_key = str(card_id)
            card_name = card_entry.get(
                "name") or self._get_card_name(card_id) or f"Card {card_id}"
            defaults = self._card_performance_template(card_name)
            card_perf = performance.setdefault(card_key, defaults)
            self._merge_card_performance_defaults(card_perf, defaults)
            card_perf["name"] = card_name

            card_perf["games_played"] += 1
            self._record_card_outcome(card_perf, outcome)

            was_played = card_id in played
            if was_played:
                card_perf["usage_count"] += 1

                stage_stats = card_perf["performance_by_stage"].setdefault(
                    game_stage.value,
                    {"wins": 0, "losses": 0, "draws": 0, "played": 0})
                stage_stats["played"] += 1
                self._record_card_outcome(stage_stats, outcome)

                position_stats = card_perf[
                    "performance_by_position"].setdefault(
                        game_state.value,
                        {"wins": 0, "losses": 0,
                         "draws": 0, "played": 0})
                position_stats["played"] += 1
                self._record_card_outcome(position_stats, outcome)

                order_stats = card_perf[
                    "play_order_performance"].setdefault(
                        play_position,
                        {"wins": 0, "losses": 0,
                         "draws": 0, "played": 0})
                order_stats["played"] += 1
                self._record_card_outcome(order_stats, outcome)

                for turn, cards in player_play_history.items():
                    if card_id not in cards:
                        continue
                    turn_key = str(turn)
                    turn_stats = card_perf[
                        "performance_by_turn"].setdefault(
                            turn_key,
                            {"played": 0, "wins": 0,
                             "losses": 0, "draws": 0})
                    turn_stats.setdefault("losses", 0)
                    turn_stats["played"] += 1
                    self._record_card_outcome(turn_stats, outcome)

                    card = self.card_db.get(card_id)
                    if not card or not hasattr(card, "cmc"):
                        continue
                    try:
                        mana_value = int(card.cmc)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    curve_status = (
                        "on_curve" if turn == mana_value
                        else "under_curve" if turn < mana_value
                        else "over_curve")
                    curve_stats = card_perf["play_curve_stats"][curve_status]
                    curve_stats["games"] += 1
                    self._record_card_outcome(curve_stats, outcome)

            in_opening_hand = card_id in opening
            if in_opening_hand:
                card_perf["games_in_opening_hand"] += 1
                card_perf["wins_when_in_opening_hand"] += points

            was_drawn = False
            for turn, cards in player_draw_history.items():
                if card_id not in cards:
                    continue
                was_drawn = True
                turn_key = str(turn)
                draw_stats = card_perf.setdefault(
                    "draw_performance_by_turn", {}).setdefault(
                        turn_key, {"drawn": 0, "wins": 0, "draws": 0})
                draw_stats["drawn"] += 1
                self._record_card_outcome(draw_stats, outcome)

            if was_drawn or in_opening_hand:
                card_perf["games_drawn"] += 1
                card_perf["wins_when_drawn"] += points
            else:
                card_perf["games_not_drawn"] += 1
                card_perf["wins_when_not_drawn"] += points

            games_played = max(1, card_perf["games_played"])
            card_perf["win_rate"] = (
                card_perf["wins"] + 0.5 * card_perf["draws"]
            ) / games_played
            card_perf["drawn_win_rate"] = (
                card_perf["wins_when_drawn"]
                / max(1, card_perf["games_drawn"]))
            card_perf["not_drawn_win_rate"] = (
                card_perf["wins_when_not_drawn"]
                / max(1, card_perf["games_not_drawn"]))
            card_perf["opening_hand_win_rate"] = (
                card_perf["wins_when_in_opening_hand"]
                / max(1, card_perf["games_in_opening_hand"]))

            if (card_perf["games_drawn"] > 0
                    and card_perf["games_not_drawn"] > 0):
                card_perf["improvement_factor"] = (
                    card_perf["drawn_win_rate"]
                    / max(0.01, card_perf["not_drawn_win_rate"]))
            try:
                base_score = (
                    card_perf["improvement_factor"] - 0.5) * 2
                win_rate_boost = card_perf["win_rate"] - 0.5
                card_perf["performance_rating"] = max(
                    0.0, min(
                        1.0,
                        0.5 + 0.25 * base_score
                        + 0.25 * win_rate_boost))
            except (KeyError, TypeError):
                card_perf["performance_rating"] = 0.5

            if not self._save_individual_card_stats(card_name, {
                    "name": card_name,
                    "id": card_id,
                    "wins": 1 if outcome == "win" else 0,
                    "losses": 1 if outcome == "loss" else 0,
                    "draws": 1 if outcome == "draw" else 0,
                    "games_played": 1,
                    "was_played": was_played,
                    "was_drawn": was_drawn,
                    "in_opening_hand": in_opening_hand,
                    "usage_count": 1 if was_played else 0,
                    "win_rate": points,
                    "game_stage": game_stage.value,
                    "game_state": game_state.value,
                    "deck_archetype": deck_stats.get(
                        "archetype", "unknown"),
            }):
                success = False

        return success

    def _update_card_stats(self, winner_deck_id: str, loser_deck_id: str,
                           cards_played: Dict[int, List[int]],
                           game_stage: GameStage,
                           game_state: GamePosition,
                           is_draw: bool = False,
                           opening_hands: Dict = None,
                           draw_history: Dict = None,
                           play_order: Dict = None,
                           play_history: Dict = None) -> bool:
        """Update canonical per-card aggregates for both deck seats."""
        winner_stats = self.get_deck_stats(winner_deck_id)
        loser_stats = self.get_deck_stats(loser_deck_id)
        if not winner_stats or not loser_stats:
            return False

        opening_hands = opening_hands or {"winner": [], "loser": []}
        draw_history = draw_history or {"winner": {}, "loser": {}}
        play_order = play_order or {"first_player": "unknown"}
        play_history = play_history or {}
        cards_played = cards_played or {0: [], 1: []}

        first_player = play_order.get("first_player")
        winner_went_first = (
            True if first_player == "winner"
            else False if first_player == "loser" else None)
        loser_went_first = (
            True if first_player == "loser"
            else False if first_player == "winner" else None)
        loser_game_state = _opposing_position(game_state)

        winner_success = self._update_deck_card_performance(
            winner_stats,
            winner_stats.get("card_list", []),
            cards_played.get(0, []),
            opening_hands.get("winner", []),
            draw_history.get("winner", {}),
            play_history.get("winner", {}),
            winner_went_first,
            game_stage,
            game_state,
            "draw" if is_draw else "win",
        )
        loser_success = self._update_deck_card_performance(
            loser_stats,
            loser_stats.get("card_list", []),
            cards_played.get(1, []),
            opening_hands.get("loser", []),
            draw_history.get("loser", {}),
            play_history.get("loser", {}),
            loser_went_first,
            game_stage,
            loser_game_state,
            "draw" if is_draw else "loss",
        )

        winner_saved = self._replace_deck_stats(
            winner_deck_id, winner_stats)
        loser_saved = self._replace_deck_stats(
            loser_deck_id, loser_stats)
        return (
            winner_success and loser_success
            and winner_saved and loser_saved)

    def _save_individual_card_stats(self, card_name: str, stats_update: Dict) -> bool:
        """
        Save statistics for an individual card to its own file.
        This allows better tracking of card performance across all decks.
        
        Args:
            card_name: Name of the card
            stats_update: New statistics to add
            
        Returns:
            bool: Whether the save was successful
        """
        if not card_name:
            return False

        card_id = self._resolve_unique_card_id(
            card_name, stats_update.get("id"))
        card_file = self._card_stats_file(card_name, card_id)

        # Keep the working copy in memory. A Standard game touches dozens of
        # unique cards, so loading and rewriting one gzip per card per episode
        # dominated the statistics path during vectorized training.
        card_stats = self._load_individual_card_stats(
            card_id, card_name, persist_migration=False)
        if not card_stats:
            card_stats = {
                "id": card_id,
                "name": card_name,
                "games_played": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "usage_count": 0,
                "win_rate": 0,
                "games_drawn": 0,
                "wins_when_drawn": 0.0,
                "games_not_drawn": 0,
                "wins_when_not_drawn": 0.0,
                "games_in_opening_hand": 0,
                "wins_when_in_opening_hand": 0.0,
                "drawn_win_rate": 0.0,
                "not_drawn_win_rate": 0.0,
                "opening_hand_win_rate": 0.0,
                "archetypes": {},
                "by_game_stage": {
                    "early": {"games": 0, "wins": 0, "draws": 0},
                    "mid": {"games": 0, "wins": 0, "draws": 0},
                    "late": {"games": 0, "wins": 0, "draws": 0}
                },
                "by_game_state": {
                    "ahead": {"games": 0, "wins": 0, "draws": 0},
                    "parity": {"games": 0, "wins": 0, "draws": 0},
                    "behind": {"games": 0, "wins": 0, "draws": 0}
                }
            }
        else:
            if card_id is not None:
                card_stats["id"] = card_id
            # For older card files that might be missing these keys, initialize them.
            if "by_game_stage" not in card_stats:
                card_stats["by_game_stage"] = {
                    "early": {"games": 0, "wins": 0, "draws": 0},
                    "mid": {"games": 0, "wins": 0, "draws": 0},
                    "late": {"games": 0, "wins": 0, "draws": 0}
                }
            if "by_game_state" not in card_stats:
                card_stats["by_game_state"] = {
                    "ahead": {"games": 0, "wins": 0, "draws": 0},
                    "parity": {"games": 0, "wins": 0, "draws": 0},
                    "behind": {"games": 0, "wins": 0, "draws": 0}
                }
            if "archetypes" not in card_stats:
                card_stats["archetypes"] = {}

            # Individual-card files written before exact draw telemetry did
            # not contain these counters. Preserve their accumulated outcome
            # data while starting exact telemetry from the first flagged
            # update rather than guessing whether an old game saw the card.
            telemetry_defaults = {
                "games_drawn": 0,
                "wins_when_drawn": 0.0,
                "games_not_drawn": 0,
                "wins_when_not_drawn": 0.0,
                "games_in_opening_hand": 0,
                "wins_when_in_opening_hand": 0.0,
                "drawn_win_rate": 0.0,
                "not_drawn_win_rate": 0.0,
                "opening_hand_win_rate": 0.0,
            }
            for field, default in telemetry_defaults.items():
                card_stats.setdefault(field, default)
            
            # Add draws field if it doesn't exist
            if "draws" not in card_stats:
                card_stats["draws"] = 0
                
            # Add draws to game stages if missing
            for stage in card_stats["by_game_stage"]:
                if "draws" not in card_stats["by_game_stage"][stage]:
                    card_stats["by_game_stage"][stage]["draws"] = 0
                    
            # Add draws to game states if missing
            for state in card_stats["by_game_state"]:
                if "draws" not in card_stats["by_game_state"][state]:
                    card_stats["by_game_state"][state]["draws"] = 0

        # Normalize legacy numeric strings/nulls before incrementing them.
        card_stats = self._validate_stats_types(card_stats)

        # Update basic stats
        card_stats["games_played"] += 1
        card_stats["wins"] += stats_update.get("wins", 0)
        card_stats["losses"] += stats_update.get("losses", 0)
        card_stats["draws"] += stats_update.get("draws", 0)

        if stats_update.get("was_played", False):
            card_stats["usage_count"] += 1

        # ``games_drawn`` is the exact per-game seen union used by the viewer:
        # a card counts once when it was either in the final opening hand or
        # drawn later, even when both flags are true. Older private callers may
        # omit both flags; do not fabricate not-drawn evidence for those calls.
        has_draw_telemetry = (
            "was_drawn" in stats_update
            or "in_opening_hand" in stats_update)
        if has_draw_telemetry:
            was_drawn = bool(stats_update.get("was_drawn", False))
            in_opening_hand = bool(
                stats_update.get("in_opening_hand", False))
            was_seen = was_drawn or in_opening_hand
            outcome_points = (
                stats_update.get("wins", 0)
                + 0.5 * stats_update.get("draws", 0))

            if was_seen:
                card_stats["games_drawn"] += 1
                card_stats["wins_when_drawn"] += outcome_points
            else:
                card_stats["games_not_drawn"] += 1
                card_stats["wins_when_not_drawn"] += outcome_points

            if in_opening_hand:
                card_stats["games_in_opening_hand"] += 1
                card_stats["wins_when_in_opening_hand"] += outcome_points

        card_stats["drawn_win_rate"] = (
            card_stats["wins_when_drawn"]
            / max(1, card_stats["games_drawn"]))
        card_stats["not_drawn_win_rate"] = (
            card_stats["wins_when_not_drawn"]
            / max(1, card_stats["games_not_drawn"]))
        card_stats["opening_hand_win_rate"] = (
            card_stats["wins_when_in_opening_hand"]
            / max(1, card_stats["games_in_opening_hand"]))

        # Update win rate (count draws as 0.5 wins)
        if card_stats["games_played"] > 0:
            card_stats["win_rate"] = (card_stats["wins"] + 0.5 * card_stats["draws"]) / card_stats["games_played"]

        # Update archetype stats
        deck_archetype = stats_update.get("deck_archetype", "unknown")
        if deck_archetype not in card_stats["archetypes"]:
            card_stats["archetypes"][deck_archetype] = {"games": 0, "wins": 0, "draws": 0}

        card_stats["archetypes"][deck_archetype]["games"] += 1
        card_stats["archetypes"][deck_archetype]["wins"] += stats_update.get("wins", 0)
        card_stats["archetypes"][deck_archetype]["draws"] += stats_update.get("draws", 0)

        # Update game stage stats
        game_stage = stats_update.get("game_stage", "mid")
        if game_stage not in card_stats["by_game_stage"]:
            card_stats["by_game_stage"][game_stage] = {"games": 0, "wins": 0, "draws": 0}

        card_stats["by_game_stage"][game_stage]["games"] += 1
        card_stats["by_game_stage"][game_stage]["wins"] += stats_update.get("wins", 0)
        card_stats["by_game_stage"][game_stage]["draws"] += stats_update.get("draws", 0)

        # Update game state stats
        game_state = stats_update.get("game_state", "parity")
        if game_state not in card_stats["by_game_state"]:
            card_stats["by_game_state"][game_state] = {"games": 0, "wins": 0, "draws": 0}

        card_stats["by_game_state"][game_state]["games"] += 1
        card_stats["by_game_state"][game_state]["wins"] += stats_update.get("wins", 0)
        card_stats["by_game_state"][game_state]["draws"] += stats_update.get("draws", 0)

        self._individual_card_cache[card_file] = card_stats
        self._dirty_individual_card_files.add(card_file)
        return True
    
    # === Analysis and Recommendations ===
    
    def get_deck_analysis(self, deck: List[int]) -> Dict:
        """
        Get comprehensive analysis for a deck.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with deck analysis information
        """
        # First try to find this deck in the database
        deck_id = self.get_deck_fingerprint(deck)
        stats = self.get_deck_stats(deck_id)
        
        # If not found, find similar decks
        similar_decks = []
        all_decks = self._get_all_deck_keys()
        
        for other_id in all_decks:
            other_stats = self.get_deck_stats(other_id)
            if other_stats and "card_list" in other_stats:
                other_deck = [card["id"] for card in other_stats["card_list"]]
                similarity = self.calculate_deck_similarity(deck, other_deck)
                
                if similarity >= 0.7:  # 70% similarity threshold
                    similar_decks.append((other_id, similarity, other_stats))
        
        # Sort similar decks by similarity
        similar_decks.sort(key=lambda x: x[1], reverse=True)
        
        # Generate analysis results
        result = {
            "deck_id": deck_id,
            "deck_size": len(deck),
            "archetype": self.identify_archetype(deck),
            "exact_match_found": stats is not None,
            "similar_decks": [
                {
                    "id": other_id,
                    "similarity": similarity,
                    "name": other_stats["name"],
                    "win_rate": other_stats.get("win_rate", 0),
                    "games_played": other_stats.get("games", 0)
                }
                for other_id, similarity, other_stats in similar_decks[:5]  # Top 5 similar decks
            ],
            "card_analysis": [],
            "synergy_analysis": self._analyze_deck_synergy(deck),
            "meta_position": self._analyze_meta_position(deck)
        }
        
        # Add card analysis
        for card_id in set(deck):
            count = deck.count(card_id)
            
            # Get card info
            card_name = self._get_card_name(card_id)
            
            # Calculate card metrics
            card_metrics = self._get_card_metrics(card_id)
            
            result["card_analysis"].append({
                "id": card_id,
                "name": card_name or f"Card {card_id}",
                "count": count,
                "metrics": card_metrics
            })
        
        return result
    
    def _analyze_deck_synergy(self, deck: List[int]) -> Dict:
        """
        Analyze synergies within a deck.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with synergy analysis
        """
        # Calculate overall synergy score
        overall_score = self.calculate_deck_synergy_score(deck)
        
        # Calculate synergy matrix
        synergy_matrix = self.get_deck_synergy_matrix(deck)
        
        # Find highest synergy pairs
        synergy_pairs = []
        for (card1_id, card2_id), score in synergy_matrix.items():
            # Get card names
            card1_name = self._get_card_name(card1_id)
            card2_name = self._get_card_name(card2_id)
                    
            synergy_pairs.append({
                "card1_id": card1_id,
                "card1_name": card1_name or f"Card {card1_id}",
                "card2_id": card2_id,
                "card2_name": card2_name or f"Card {card2_id}",
                "synergy_score": score
            })
            
        # Sort by synergy score (highest first)
        synergy_pairs.sort(key=lambda x: x["synergy_score"], reverse=True)
        
        # Find low-synergy cards
        card_avg_synergies = {}
        for card_id in set(deck):
            synergies = []
            
            for other_id in set(deck):
                if card_id != other_id:
                    key = (min(card_id, other_id), max(card_id, other_id))
                    if key in synergy_matrix:
                        synergies.append(synergy_matrix[key])
                        
            if synergies:
                card_avg_synergies[card_id] = sum(synergies) / len(synergies)
        
        # Find cards with low average synergy
        low_synergy_cards = []
        for card_id, avg_synergy in card_avg_synergies.items():
            if avg_synergy < 0.3:  # Threshold for low synergy
                card_name = self._get_card_name(card_id)
                        
                low_synergy_cards.append({
                    "id": card_id,
                    "name": card_name or f"Card {card_id}",
                    "avg_synergy": avg_synergy
                })
                
        # Sort by synergy (lowest first)
        low_synergy_cards.sort(key=lambda x: x["avg_synergy"])
        
        return {
            "overall_score": overall_score,
            "top_synergy_pairs": synergy_pairs[:10],  # Top 10 pairs
            "low_synergy_cards": low_synergy_cards[:5]  # 5 lowest synergy cards
        }
    
    def _analyze_meta_position(self, deck: List[int]) -> Dict:
        """
        Analyze a deck's position in the current metagame.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with meta position analysis
        """
        # Get deck archetype
        archetype = self.identify_archetype(deck)
        
        # Get meta snapshot
        meta_snapshot = self.get_meta_snapshot()
        
        # Check archetype position
        archetype_position = {}
        meta_data = self._load_meta_data()
        if archetype in meta_data["archetypes"]:
            arch_data = meta_data["archetypes"][archetype]
            
            # Calculate percentile rank among archetypes
            all_win_rates = [
                data["win_rate"] for name, data in meta_data["archetypes"].items()
                if data["games"] >= 10  # Minimum sample size
            ]
            
            if all_win_rates:
                all_win_rates.sort()
                percentile = sum(1 for wr in all_win_rates if wr <= arch_data["win_rate"]) / len(all_win_rates)
                
                archetype_position = {
                    "games_played": arch_data["games"],
                    "win_rate": arch_data["win_rate"],
                    "percentile": percentile,
                    "meta_share": _deck_seat_share(
                        arch_data["games"], meta_snapshot["total_games"])
                }
                
        # Get archetype matchups
        matchups = self.get_archetype_matchups(archetype)
        
        # Calculate deck vs. meta performance
        meta_performance = {}
        
        # Get the top meta archetypes
        top_archetypes = self.get_top_archetypes()
        
        # Calculate expected performance against the meta
        if top_archetypes:
            meta_win_rate = 0
            weight_sum = 0
            
            for arch_data in top_archetypes:
                arch_name = arch_data["archetype"]
                arch_meta_share = _deck_seat_share(
                    arch_data["games"], meta_snapshot["total_games"])
                
                matchup_win_rate = matchups.get(arch_name, 0.5)  # Default to 50% if unknown
                
                meta_win_rate += matchup_win_rate * arch_meta_share
                weight_sum += arch_meta_share
                
            if weight_sum > 0:
                meta_performance["expected_win_rate"] = meta_win_rate / weight_sum
                
            # Evaluate meta position
            position_score = 0
            if "expected_win_rate" in meta_performance:
                position_score = (meta_performance["expected_win_rate"] - 0.5) * 2  # -1 to 1 scale
                
            if position_score >= 0.3:
                meta_performance["evaluation"] = "Well positioned in the current meta"
            elif position_score >= 0:
                meta_performance["evaluation"] = "Adequately positioned in the current meta"
            elif position_score >= -0.3:
                meta_performance["evaluation"] = "Weakly positioned in the current meta"
            else:
                meta_performance["evaluation"] = "Poorly positioned in the current meta"
        
        return {
            "archetype": archetype,
            "archetype_position": archetype_position,
            "top_matchups": sorted(matchups.items(), key=lambda x: x[1], reverse=True)[:3],
            "worst_matchups": sorted(matchups.items(), key=lambda x: x[1])[:3],
            "meta_performance": meta_performance
        }
    
    def _get_card_metrics(self, card_id: int) -> Dict:
        """Get performance metrics for a card"""
        # Get card name
        card_name = self._get_card_name(card_id)
        
        # Check individual card stats first
        if card_name:
            card_stats = self._load_individual_card_stats(
                card_id, card_name, persist_migration=True)
            if card_stats:
                return {
                    "win_rate": card_stats.get("win_rate", 0),
                    "games_played": card_stats.get("games_played", 0),
                    "usage_rate": card_stats.get("usage_count", 0) / max(1, card_stats.get("games_played", 0)),
                    "best_archetype": max(card_stats.get("archetypes", {}).items(), 
                                       key=lambda x: x[1].get("wins", 0) / max(1, x[1].get("games", 1)))[0] 
                                       if card_stats.get("archetypes") else None,
                    "best_game_stage": max(card_stats.get("by_game_stage", {}).items(),
                                        key=lambda x: x[1].get("wins", 0) / max(1, x[1].get("games", 1)))[0]
                                        if card_stats.get("by_game_stage") else None
                }
        
        # Fall back to global stats
        stats = self.get_card_stats(card_id)
        
        # Get card data from database
        card = self.card_db.get(card_id)
        
        metrics = {
            "win_rate": stats.get("win_rate", 0),
            "games_played": stats.get("games_played", 0),
            "usage_rate": stats.get("usage_count", 0) / max(1, stats.get("games_played", 0)),
            "type": getattr(card, 'type_line', "Unknown") if card else "Unknown",
            "cmc": getattr(card, 'cmc', 0) if card else 0,
            "performance_rating": getattr(card, 'performance_rating', 0.5) if card else 0.5
        }
        
        # Calculate positional performance
        if stats.get("performance_by_position"):
            pos_perf = stats["performance_by_position"]
            metrics["ahead_win_rate"] = (
                pos_perf["ahead"]["wins"] / max(1, pos_perf["ahead"]["wins"] + pos_perf["ahead"]["losses"])
            )
            metrics["behind_win_rate"] = (
                pos_perf["behind"]["wins"] / max(1, pos_perf["behind"]["wins"] + pos_perf["behind"]["losses"])
            )
            metrics["comeback_potential"] = metrics["behind_win_rate"] * 2  # Scale up for emphasis
        
        return metrics
    
    # === Utility Methods ===
    
    def _get_card_name(self, card_id: int) -> Optional[str]:
        """Get the name of a card from its ID"""
        # Check cache first
        if card_id in self.card_id_to_name:
            return self.card_id_to_name[card_id]
            
        # Try to get from card database
        if card_id in self.card_db:
            card = self.card_db[card_id]
            if hasattr(card, 'name'):
                self.card_id_to_name[card_id] = card.name
                return card.name
        
        return None
    
    def _sanitize_filename(self, name: str) -> str:
        """
        Sanitize a string for use as a filename.
        Removes illegal characters and shortens if necessary.
        """
        # Remove illegal filename characters
        sanitized = re.sub(r'[<>:"/\\|?*]', '', name)
        # Replace spaces with underscores
        sanitized = sanitized.replace(' ', '_')
        # Limit length
        max_length = 50  # Maximum reasonable filename length
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length]
        return sanitized or "card"

    def _matching_card_ids(self, card_name: str) -> set:
        """Return known canonical IDs with the supplied display name."""
        folded_name = str(card_name).casefold()
        matches = {
            str(card_id)
            for card_id, known_name in self.card_id_to_name.items()
            if str(known_name).casefold() == folded_name
        }
        for card_id, card in (self.card_db or {}).items():
            if str(getattr(card, "name", "")).casefold() == folded_name:
                matches.add(str(card_id))
        return matches

    def _resolve_unique_card_id(
            self, card_name: str, card_id: Any = None) -> Any:
        if card_id is not None:
            return card_id
        matches = self._matching_card_ids(card_name)
        if len(matches) == 1:
            return next(iter(matches))
        return None

    def _legacy_card_stats_files(self, card_name: str) -> List[str]:
        """Return every path produced by the former name-keyed scheme."""
        base = self._sanitize_filename(card_name)
        legacy_path = f"cards/{base}.json"
        digest = hashlib.sha256(
            str(card_name).encode("utf-8")).hexdigest()[:12]
        collision_path = f"cards/{base[:37]}_{digest}.json"
        return ([legacy_path] if collision_path == legacy_path
                else [legacy_path, collision_path])

    def _load_card_stats_path(self, card_file: str) -> Optional[Dict]:
        cached = self._individual_card_cache.get(card_file)
        if cached is not None:
            return cached
        if not self.exists(card_file):
            return None
        loaded = self.load(card_file)
        if isinstance(loaded, dict) and loaded:
            self._individual_card_cache[card_file] = loaded
            return loaded
        return None

    def _legacy_card_file_matches_id(
            self, payload: Dict, card_name: str, card_id: Any) -> bool:
        """Reject legacy name aggregates that could describe multiple IDs."""
        target_id = str(card_id)
        payload_id = payload.get("id")
        if payload_id is not None:
            return str(payload_id) == target_id
        return self._matching_card_ids(card_name) == {target_id}

    def _load_individual_card_stats(
            self, card_id: Any, card_name: str,
            persist_migration: bool = False) -> Optional[Dict]:
        """Load canonical stats, migrating an unambiguous legacy file once."""
        card_id = self._resolve_unique_card_id(card_name, card_id)
        card_file = self._card_stats_file(card_name, card_id)
        canonical = self._load_card_stats_path(card_file)
        if canonical is not None or card_id is None:
            return canonical

        legacy_matches = []
        for legacy_file in self._legacy_card_stats_files(card_name):
            legacy = self._load_card_stats_path(legacy_file)
            if (legacy is not None
                    and str(legacy.get("name", "")).casefold()
                    == str(card_name).casefold()
                    and self._legacy_card_file_matches_id(
                        legacy, card_name, card_id)):
                legacy_matches.append(legacy)

        if len(legacy_matches) != 1:
            return None

        migrated = copy.deepcopy(legacy_matches[0])
        migrated["id"] = card_id
        migrated["name"] = card_name
        self._individual_card_cache[card_file] = migrated
        if persist_migration and self.save(card_file, migrated):
            self._dirty_individual_card_files.discard(card_file)
        else:
            self._dirty_individual_card_files.add(card_file)
        return migrated

    def _card_stats_file(
            self, card_name: str, card_id: Any = None) -> str:
        """Return the canonical-ID aggregate path, with a legacy fallback."""
        card_id = self._resolve_unique_card_id(card_name, card_id)
        if card_id is None:
            return self._legacy_card_stats_files(card_name)[0]
        identity = str(card_id)
        safe_identity = self._sanitize_filename(identity)[:24]
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return f"cards/id_{safe_identity}_{digest}.json"
    
    def _get_all_deck_files(self) -> List[str]:
        """Get all deck JSON files from storage"""
        deck_files = []
        deck_dir = os.path.join(self.base_path, "decks")
        
        if os.path.exists(deck_dir) and os.path.isdir(deck_dir):
            for file_name in os.listdir(deck_dir):
                if file_name.endswith(".json") or file_name.endswith(".json.gz"):
                    deck_files.append(f"decks/{file_name}")
                    
        return deck_files
    
    def _get_all_deck_keys(self) -> List[str]:
        """Get all deck keys in the database"""
        deck_dir = os.path.join(self.base_path, "decks")
        if os.path.exists(deck_dir) and os.path.isdir(deck_dir):
            return [
                os.path.splitext(file)[0] for file in os.listdir(deck_dir)
                if file.endswith(".json") or file.endswith(".json.gz")
            ]
        return []
    
    def _get_all_deck_data(self) -> Dict[str, Dict]:
        """Get all deck data from storage"""
        data = {}
        keys = self._get_all_deck_keys()
        
        for key in keys:
            stats = self.get_deck_stats(key)
            if stats:
                data[key] = stats
                
        return data
    
    def _generate_deck_filename(self, stats: Dict, deck_key: str, original_filename: str = None) -> str:
        # If a filename has already been generated, reuse it.
        if "filename" in stats:
            return stats["filename"]

        # Use the provided original filename, or fallback to the deck name.
        if original_filename:
            base_name = self._sanitize_deck_name(os.path.splitext(original_filename)[0])
        elif stats and "name" in stats:
            base_name = self._sanitize_deck_name(stats["name"])
        else:
            base_name = deck_key[:20]

        # Include the predicted archetype in the filename.
        archetype = stats.get("archetype", "Unknown")
        
        # Construct a deterministic filename using the base name, archetype, and deck_key.
        filename = f"{base_name}_{archetype}_{deck_key}.json"
        
        # Lock in the filename by saving it in stats.
        stats["filename"] = filename
        return filename

    # === Batch Update Methods ===
    
    async def save_batch_updates(self) -> bool:
        """Save all batched updates to storage with enhanced error handling and locking"""
        batch = None
        try:
            with self.batch_lock:
                # Copy and clear batch to minimize lock time
                batch = self.batch_updates.copy()
                self.batch_updates.clear()
        except Exception as e:
            logging.error(f"Error acquiring batch lock: {e}")
            return False
        
        if not batch:
            return True  # Nothing to save
        
        success = True
        failed_updates = {}
        for deck_key, stats in batch.items():
            try:
                # Validate before saving
                valid, errors = self.validate_deck_stats(stats)
                if not valid:
                    logging.warning(f"Invalid deck stats for {deck_key}: {errors}")
                    stats = self.repair_deck_stats(stats)
                
                # Check if original filename was stored
                original_filename = stats.get("name")
                
                # Create user-friendly filename
                filename = self._generate_deck_filename(stats, deck_key, original_filename)
                
                # Asynchronous save
                save_result = await self.save_async(f"decks/{filename}", stats)
                
                if not save_result:
                    logging.error(f"Failed to save stats for deck {deck_key}")
                    success = False
                    failed_updates[deck_key] = stats
            
            except Exception as e:
                logging.error(f"Error saving deck stats for {deck_key}: {str(e)}")
                success = False
                failed_updates[deck_key] = stats

        if failed_updates:
            with self.batch_lock:
                # A newer snapshot for the same deck wins; otherwise retain
                # the failed snapshot so the next flush can retry it.
                for deck_key, stats in failed_updates.items():
                    self.batch_updates.setdefault(deck_key, stats)
        
        return success
    
    def _flush_auxiliary_stats(self):
        """Persist cached meta and per-card snapshots without losing failures."""
        success = True
        if self._meta_data_dirty and not self.save_meta_data():
            success = False
        for card_file in list(self._dirty_individual_card_files):
            card_stats = self._individual_card_cache.get(card_file)
            if card_stats is None or not self.save(card_file, card_stats):
                success = False
                continue
            self._dirty_individual_card_files.discard(card_file)
        return success

    def save_updates_sync(self):
        """
        Synchronous method to save all pending updates.
        
        This method provides a way to save batch updates when an async context 
        is not available or when a simple synchronous method is preferred.
        
        Returns:
            bool: True if updates were saved successfully, False otherwise
        """
        deck_success = False
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                deck_success = asyncio.run(self.save_batch_updates())
            else:
                # A loop cannot be nested in the same thread. Direct notebook
                # and async-service callers still need this synchronous flush,
                # so run the coroutine to completion in a short-lived worker.
                result = {}

                def _run_batch_save():
                    try:
                        result["value"] = asyncio.run(
                            self.save_batch_updates())
                    except Exception as error:
                        result["error"] = error

                worker = threading.Thread(
                    target=_run_batch_save,
                    name="deck-stats-sync-save",
                    daemon=False)
                worker.start()
                worker.join()
                if "error" in result:
                    raise result["error"]
                deck_success = bool(result.get("value", False))
        except Exception as e:
            logging.error(f"Error saving updates synchronously: {e}")
            return False
        auxiliary_success = self._flush_auxiliary_stats()
        if deck_success and auxiliary_success:
            self._games_since_persistence = 0
        return deck_success and auxiliary_success
    
    async def save_all_pending_updates(self) -> bool:
        """Save all pending updates to storage"""
        deck_success = await self.save_batch_updates()
        auxiliary_success = self._flush_auxiliary_stats()
        if deck_success and auxiliary_success:
            self._games_since_persistence = 0
        return deck_success and auxiliary_success
    
    


class DeckStatsCollector:
    """
    A simplified interface to DeckStatsTracker for collecting stats only.
    This class is intended to be used in environments where only recording
    game results is needed, without the full analysis functionality.
    """
    
    def __init__(self, storage_path: str = "./deck_stats", card_db: Dict = None):
        self.tracker = DeckStatsTracker(storage_path, card_db)
    
    def save_pending_updates(self) -> bool:
        """Save any pending updates to disk"""
        return self.tracker.save_updates_sync()
