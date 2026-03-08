"""
Configuration BACCARAT AI 🤖
Bot de prédiction basé sur les cycles numériques
"""
import os

def parse_channel_id(env_var: str, default: str) -> int:
    """Parse et formate l'ID de canal Telegram."""
    value = os.getenv(env_var) or default
    channel_id = int(value)
    if channel_id > 0 and len(str(channel_id)) >= 10:
        channel_id = -channel_id
    return channel_id

# Canaux Telegram
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1002543915361')

# Authentification
ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')
API_ID = int(os.getenv('API_ID') or '0')
API_HASH = os.getenv('API_HASH') or ''
BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
TELEGRAM_SESSION = os.getenv('TELEGRAM_SESSION', '')

# Serveur
PORT = int(os.getenv('PORT') or '10000')

# Cycles de prédiction
SUIT_CYCLES = {
    '♦': {'start': 1, 'interval': 6},   # Carreau: 1, 7, 13, 19...
    '♥': {'start': 1, 'interval': 6},   # Cœur: 1, 7, 13, 19...
    '♣': {'start': 1, 'interval': 7},   # Trèfle: 1, 8, 15, 22...
    '♠': {'start': 1, 'interval': 5},   # Pique: 1, 6, 11, 16...
}

ALL_SUITS = ['♠', '♥', '♦', '♣']

SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}

# Paramètres
CONSECUTIVE_FAILURES_NEEDED = 2
MAX_PENDING_PREDICTIONS = 10
BLOCK_DURATION_AFTER_LOSS = 5
ENABLE_TIME_RESTRICTION = True

# Ancienne config (compatibilité)
SUIT_MAPPING = {'♠': '♣', '♥': '♠', '♦': '♥', '♣': '♦'}
SUIT_SEQUENCE = ['♠', '♥', '♦', '♣']
PREDICTION_INTERVAL = 4
