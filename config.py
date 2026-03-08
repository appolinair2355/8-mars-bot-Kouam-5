"""
Configuration du bot Baccarat AI
"""

# ============================================================================
# TELEGRAM API CREDENTIALS
# ============================================================================

# Remplacez par vos valeurs réelles
API_ID = 29177661  # Votre API ID Telegram (entier)
API_HASH = "a8639172fa8d35dbfd8ea46286d349ab"  # Votre API Hash Telegram (string)
BOT_TOKEN = "7815360317:AAGsrFzeUZrHOjujf5aY2UjlBj4GOblHSig"  # Token du bot

# ============================================================================
# ADMIN ET CANAUX
# ============================================================================

# ID de l'administrateur (votre ID Telegram)
ADMIN_ID = 1190237801  # Remplacez par votre ID Telegram

# ID du canal source (où arrivent les messages avec #N)
# Format: -100xxxxxxxxxx ou juste les chiffres
SOURCE_CHANNEL_ID = -1002682552255

# ID du canal de prédiction (où envoyer les prédictions)
# Le bot doit être administrateur avec droit d'envoi de messages
PREDICTION_CHANNEL_ID = -1003725380926

# ============================================================================
# PARAMÈTRES DU SERVEUR WEB (pour Render/Heroku)
# ============================================================================

PORT = 10000  # Port pour le serveur web de health check

# ============================================================================
# CONFIGURATION DES CYCLES (Mode Standard/Hyper Serré)
# ============================================================================

# Intervalles entre les cycles pour chaque costume
SUIT_CYCLES = {
    '♠': {'start': 1, 'interval': 5},   # Pique: tous les 5 numéros
    '♥': {'start': 1, 'interval': 6},   # Cœur: tous les 6 numéros
    '♦': {'start': 1, 'interval': 6},   # Carreau: tous les 6 numéros
    '♣': {'start': 1, 'interval': 7},   # Trèfle: tous les 7 numéros
}

# Liste des costumes
ALL_SUITS = ['♠', '♥', '♦', '♣']

# Affichage des costumes
SUIT_DISPLAY = {
    '♠': '♠️ Pique',
    '♥': '❤️ Cœur',
    '♦': '♦️ Carreau',
    '♣': '♣️ Trèfle'
}

# ============================================================================
# PARAMÈTRES DE JEU
# ============================================================================

# Nombre de tours consécutifs avant prédiction (pour les cycles)
CONSECUTIVE_FAILURES_NEEDED = 2

# Nombre de numéros par tour en mode standard
NUMBERS_PER_TOUR = 3

# ============================================================================
# PARAMÈTRES COMPTEUR2 (NOUVEAU)
# ============================================================================

# Seuil B par défaut pour le Compteur2 (nombre de manques avant prédiction)
COMPTEUR2_SEUIL_B_DEFAULT = 2

# Activation par défaut du Compteur2
COMPTEUR2_ACTIVE_DEFAULT = True

# ============================================================================
# PARAMÈTRES DE SÉCURITÉ
# ============================================================================

# Nombre de numéros sans prédiction avant redémarrage forcé
FORCE_RESTART_THRESHOLD = 20

# Numéro de jeu pour reset automatique
RESET_AT_GAME_NUMBER = 1440

# Timeout pour considérer une prédiction comme bloquée (minutes)
PREDICTION_TIMEOUT_MINUTES = 30

# ============================================================================
# PARAMÈTRES DE LOGGING
# ============================================================================

LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
