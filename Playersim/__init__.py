"""Playersim: MTG rules engine and AlphaZero-style RL environment.

This file intentionally imports nothing. Several Playersim modules have
circular import relationships that are currently managed with lazy imports
inside functions; importing submodules here would re-trigger those cycles
at package-import time. Import what you need explicitly, e.g.:

    from Playersim.environment import AlphaZeroMTGEnv
    from Playersim.card import load_decks_and_card_db
"""

__version__ = "3.0.0"
