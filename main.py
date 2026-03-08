import os
import asyncio
import re
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set
from datetime import datetime, timedelta
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_CYCLES, ALL_SUITS, SUIT_DISPLAY,
    CONSECUTIVE_FAILURES_NEEDED, NUMBERS_PER_TOUR
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0: 
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH: 
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN: 
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

cycle_trackers: Dict[str, 'SuitCycleTracker'] = {}
pending_predictions: Dict[int, dict] = {}
current_game_number = 0
last_source_game_number = 0
last_prediction_time: Optional[datetime] = None
prediction_channel_ok = False
client = None
suit_block_until: Dict[str, datetime] = {}
waiting_finalization: Dict[int, dict] = {}

# Variables pour le mode hyper serré (modifiables à chaud)
hyper_serré_active = False
hyper_serré_h = 5

# NOUVEAU: Compteur2 - Gestion des costumes manquants
compteur2_trackers: Dict[str, 'Compteur2Tracker'] = {}
compteur2_seuil_B = 2  # Seuil par défaut
compteur2_active = True
last_prediction_number = 0  # Dernier numéro pour lequel on a prédit
prediction_queue = []  # File d'attente des prédictions
last_prediction_sent_time = None  # Pour détecter les blocages
games_without_prediction = 0  # Compteur pour auto-redémarrage

# Historiques pour la commande /history
finalized_messages_history: List[Dict] = []
MAX_HISTORY_SIZE = 50
prediction_history: List[Dict] = []

# ============================================================================
# FONCTION UTILITAIRE - Conversion ID Canal
# ============================================================================

def normalize_channel_id(channel_id) -> int:
    """
    Normalise l'ID du canal pour Telethon.
    Les canaux doivent avoir le format -100xxxxxxxxxx
    """
    if not channel_id:
        return None
    
    channel_str = str(channel_id)
    
    if channel_str.startswith('-100'):
        return int(channel_str)
    
    if channel_str.startswith('-'):
        return int(channel_str)
    
    return int(f"-100{channel_str}")

async def resolve_channel(entity_id):
    """
    Résout l'entité canal et vérifie l'accès.
    Retourne l'entité ou None si inaccessible.
    """
    try:
        if not entity_id:
            return None
        
        normalized_id = normalize_channel_id(entity_id)
        entity = await client.get_entity(normalized_id)
        
        if hasattr(entity, 'broadcast') and entity.broadcast:
            logger.info(f"✅ Canal résolu: {entity.title} (ID: {normalized_id})")
            return entity
        
        if hasattr(entity, 'megagroup') and entity.megagroup:
            logger.info(f"✅ Groupe résolu: {entity.title} (ID: {normalized_id})")
            return entity
            
        return entity
        
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

# ============================================================================
# CLASSES
# ============================================================================

@dataclass
class SuitCycleTracker:
    """Tracker pour suivre les cycles d'une couleur."""
    suit: str
    cycle_numbers: List[int] = field(default_factory=list)
    current_tour: int = 1
    miss_counter: int = 0
    pending_prediction: Optional[int] = None
    tour_checked_numbers: Set[int] = field(default_factory=set)
    verification_history: Dict[int, bool] = field(default_factory=dict)
    last_cycle_index: int = -1
    
    def get_display_name(self) -> str:
        names = {
            '♠': '♠️ Pique',
            '♥': '❤️ Cœur',
            '♦': '♦️ Carreau',
            '♣': '♣️ Trèfle'
        }
        return names.get(self.suit, self.suit)
    
    def update_to_current_game(self, game_number: int):
        """Met à jour le last_cycle_index pour pointer sur le bon cycle."""
        new_index = -1
        for i, cycle_num in enumerate(self.cycle_numbers):
            if cycle_num <= game_number:
                new_index = i
            else:
                break
        
        if new_index != self.last_cycle_index and new_index >= 0:
            old_cycle = self.cycle_numbers[self.last_cycle_index] if self.last_cycle_index >= 0 else 'N/A'
            new_cycle = self.cycle_numbers[new_index]
            logger.info(f"🔄 {self.suit} avance: cycle #{old_cycle} → #{new_cycle} (jeu #{game_number})")
            
            if self.last_cycle_index >= 0 and new_index > self.last_cycle_index:
                if self.miss_counter == 0 and self.current_tour == 1:
                    self.tour_checked_numbers.clear()
                    self.verification_history.clear()
            
            self.last_cycle_index = new_index
    
    def get_current_cycle_target(self) -> Optional[int]:
        """Retourne le numéro de cycle actuel."""
        if self.last_cycle_index >= 0 and self.last_cycle_index < len(self.cycle_numbers):
            return self.cycle_numbers[self.last_cycle_index]
        
        for i, num in enumerate(self.cycle_numbers):
            if num >= current_game_number:
                self.last_cycle_index = max(0, i - 1)
                return self.cycle_numbers[self.last_cycle_index] if self.last_cycle_index >= 0 else num
        return self.cycle_numbers[-1] if self.cycle_numbers else None
    
    def get_numbers_to_check_this_tour(self) -> List[int]:
        """Retourne les numéros à vérifier pour le tour actuel."""
        global hyper_serré_active, hyper_serré_h
        
        current_cycle = self.get_current_cycle_target()
        if current_cycle is None:
            return []
        
        if hyper_serré_active:
            count = hyper_serré_h
        else:
            count = NUMBERS_PER_TOUR
        
        return [current_cycle + i for i in range(count)]
    
    def is_number_in_current_tour(self, game_number: int) -> bool:
        """Vérifie si le numéro fait partie du tour actuel."""
        self.update_to_current_game(game_number)
        return game_number in self.get_numbers_to_check_this_tour()
    
    def process_verification(self, game_number: int, suit_found: bool) -> Optional[int]:
        """Traite la vérification d'un numéro."""
        global hyper_serré_active, hyper_serré_h
        
        self.update_to_current_game(game_number)
        
        if not self.is_number_in_current_tour(game_number):
            return None
        
        if game_number in self.tour_checked_numbers:
            return None
        
        self.tour_checked_numbers.add(game_number)
        self.verification_history[game_number] = suit_found
        
        if suit_found:
            logger.info(f"✅ {self.suit} trouvé au jeu #{game_number} - RESET")
            self.reset()
            return None
        
        tour_misses = len(self.tour_checked_numbers)
        
        if hyper_serré_active:
            needed = hyper_serré_h
            mode_str = f"hyper serré (h={hyper_serré_h})"
        else:
            needed = NUMBERS_PER_TOUR
            mode_str = f"standard ({NUMBERS_PER_TOUR})"
            
        logger.info(f"❌ {self.suit} manqué au jeu #{game_number} ({tour_misses}/{needed}) - Mode {mode_str}")
        
        if tour_misses >= needed:
            self.miss_counter += 1
            logger.info(f"📊 {self.suit} Tour terminé - Manques: {self.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED}")
            
            if self.miss_counter >= CONSECUTIVE_FAILURES_NEEDED:
                current_cycle = self.get_current_cycle_target()
                if current_cycle is not None:
                    
                    if hyper_serré_active:
                        pred_num = current_cycle + hyper_serré_h + 1
                        calc_detail = f"cycle #{current_cycle} + h({hyper_serré_h}) + 1 = {pred_num}"
                    else:
                        interval = SUIT_CYCLES[self.suit]['interval']
                        pred_num = current_cycle + interval - 1
                        calc_detail = f"cycle #{current_cycle} + intervalle({interval}) - 1 = {pred_num}"
                    
                    self.pending_prediction = pred_num
                    logger.info(f"🔮 {self.suit} PRÉDICTION pour #{pred_num} ({calc_detail})")
                    self.reset_after_prediction()
                    return pred_num
            
            if self.current_tour < CONSECUTIVE_FAILURES_NEEDED:
                self.current_tour += 1
                self.tour_checked_numbers.clear()
                self.last_cycle_index += 1
                next_cycle = self.get_current_cycle_target()
                logger.info(f"🔄 {self.suit} passe au Tour {self.current_tour} (cycle #{next_cycle})")
            else:
                logger.warning(f"⚠️ {self.suit} tous tours terminés mais pas de prédiction")
                self.reset()
        
        return None
    
    def reset(self):
        """Reset complet."""
        self.current_tour = 1
        self.miss_counter = 0
        self.tour_checked_numbers.clear()
        self.pending_prediction = None
        self.verification_history.clear()
    
    def reset_after_prediction(self):
        """Reset après création d'une prédiction."""
        self.current_tour = 1
        self.miss_counter = 0
        self.tour_checked_numbers.clear()
        self.verification_history.clear()


# ============================================================================
# NOUVELLE CLASSE: COMPTEUR2 TRACKER
# ============================================================================

@dataclass
class Compteur2Tracker:
    """Tracker pour le compteur2 (costumes manquants dans 1er groupe)."""
    suit: str
    counter: int = 0
    last_increment_game: int = 0
    
    def get_display_name(self) -> str:
        names = {
            '♠': '♠️ Pique',
            '♥': '❤️ Cœur',
            '♦': '♦️ Carreau',
            '♣': '♣️ Trèfle'
        }
        return names.get(self.suit, self.suit)
    
    def increment(self, game_number: int):
        """Incrémente le compteur."""
        self.counter += 1
        self.last_increment_game = game_number
        logger.info(f"📊 Compteur2 {self.suit}: {self.counter} (incrémenté au jeu #{game_number})")
    
    def reset(self, game_number: int):
        """Reset le compteur quand le costume est trouvé."""
        if self.counter > 0:
            logger.info(f"🔄 Compteur2 {self.suit}: reset de {self.counter} à 0 (trouvé au jeu #{game_number})")
        self.counter = 0
        self.last_increment_game = 0
    
    def check_threshold(self, seuil_B: int) -> bool:
        """Vérifie si le seuil est atteint."""
        return self.counter >= seuil_B


# ============================================================================
# FONCTIONS D'HISTORIQUE
# ============================================================================

def add_to_history(game_number: int, message_text: str, first_group: str, suits_found: List[str]):
    """Ajoute un message finalisé à l'historique."""
    global finalized_messages_history
    
    entry = {
        'timestamp': datetime.now(),
        'game_number': game_number,
        'message_text': message_text[:200],
        'first_group': first_group,
        'suits_found': suits_found,
        'predictions_verified': []
    }
    
    finalized_messages_history.insert(0, entry)
    
    if len(finalized_messages_history) > MAX_HISTORY_SIZE:
        finalized_messages_history = finalized_messages_history[:MAX_HISTORY_SIZE]

def add_prediction_to_history(game_number: int, suit: str, verification_games: List[int], prediction_type: str = 'standard'):
    """Ajoute une prédiction à l'historique."""
    global prediction_history
    
    prediction_history.insert(0, {
        'predicted_game': game_number,
        'suit': suit,
        'predicted_at': datetime.now(),
        'verification_games': verification_games,
        'status': 'en_cours',
        'verified_by': [],
        'type': prediction_type  # 'standard', 'distribution', 'compteur2'
    })
    
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_prediction_in_history(game_number: int, suit: str, verified_by_game: int, 
                                verified_by_group: str, rattrapage_level: int, final_status: Optional[str] = None):
    """Met à jour l'historique quand une prédiction est vérifiée."""
    global finalized_messages_history, prediction_history
    
    for pred in prediction_history:
        if pred['predicted_game'] == game_number and pred['suit'] == suit:
            pred['verified_by'].append({
                'game_number': verified_by_game,
                'first_group': verified_by_group,
                'rattrapage_level': rattrapage_level
            })
            if final_status:
                pred['status'] = final_status
            break
    
    for msg in finalized_messages_history:
        if msg['game_number'] == verified_by_game:
            msg['predictions_verified'].append({
                'predicted_game': game_number,
                'suit': suit,
                'rattrapage_level': rattrapage_level
            })
            break

# ============================================================================
# INITIALISATION
# ============================================================================

def initialize_trackers(max_game: int = 3000):
    """Initialise les trackers pour chaque couleur."""
    global cycle_trackers, compteur2_trackers
    
    # Initialiser les trackers de cycles existants
    for suit, config in SUIT_CYCLES.items():
        start = config['start']
        interval = config['interval']
        cycle_nums = list(range(start, max_game + 1, interval))
        
        cycle_trackers[suit] = SuitCycleTracker(suit=suit, cycle_numbers=cycle_nums)
        logger.info(f"📊 Cycle {suit}: +{interval}, {len(cycle_nums)} numéros")
    
    # NOUVEAU: Initialiser les trackers Compteur2
    for suit in ALL_SUITS:
        compteur2_trackers[suit] = Compteur2Tracker(suit=suit)
        logger.info(f"📊 Compteur2 {suit}: initialisé")

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé."""
    # Si contient ⏰, ce n'est PAS finalisé
    if '⏰' in message:
        return False
    
    # Doit contenir ✅ ou 🔰 pour être considéré comme finalisé
    return '✅' in message or '🔰' in message

def is_message_being_edited(message: str) -> bool:
    """Vérifie si le message est en cours d'édition (contient ⏰)."""
    return '⏰' in message

def extract_parentheses_groups(message: str) -> List[str]:
    """Extrait le contenu entre parenthèses."""
    scored_groups = re.findall(r"(\d+)?\(([^)]*)\)", message)
    if scored_groups:
        return [f"{score}:{content}" if score else content for score, content in scored_groups]
    return re.findall(r"\(([^)]*)\)", message)

def get_suits_in_group(group_str: str) -> List[str]:
    """Extrait les couleurs d'un groupe."""
    if ':' in group_str:
        group_str = group_str.split(':', 1)[1]
    
    normalized = group_str
    for old, new in [('❤️', '♥'), ('❤', '♥'), ('♥️', '♥'),
                     ('♠️', '♠'), ('♦️', '♦'), ('♣️', '♣')]:
        normalized = normalized.replace(old, new)
    
    return [suit for suit in ALL_SUITS if suit in normalized]

def block_suit(suit: str, minutes: int = 5):
    """Bloque une couleur."""
    suit_block_until[suit] = datetime.now() + timedelta(minutes=minutes)
    logger.info(f"🔒 {suit} bloqué {minutes}min")

# ============================================================================
# GESTION DES PRÉDICTIONS - MODIFIÉ
# ============================================================================

async def can_send_prediction() -> bool:
    """Vérifie si on peut envoyer une nouvelle prédiction."""
    global pending_predictions, last_prediction_sent_time
    
    # Vérifier s'il y a des prédictions en cours non terminées
    for pred in pending_predictions.values():
        if pred['status'] == 'en_cours':
            # Vérifier si la prédiction en cours est terminée
            return False
    
    # Vérifier si on n'est pas bloqué depuis trop longtemps
    if last_prediction_sent_time:
        elapsed = datetime.now() - last_prediction_sent_time
        if elapsed > timedelta(minutes=30):  # Si plus de 30min sans prédiction terminée
            logger.warning(f"⚠️ Prédiction en cours bloquée depuis {elapsed.total_seconds()/60:.1f}min")
    
    return True

async def check_and_force_restart_if_needed():
    """Vérifie si un redémarrage forcé est nécessaire."""
    global games_without_prediction, last_prediction_sent_time
    
    # Si plus de 20 numéros sans prédiction et qu'on a des prédictions en cours bloquées
    if games_without_prediction >= 20:
        logger.warning(f"🚨 BLOCAGE DÉTECTÉ: {games_without_prediction} numéros sans prédiction")
        await perform_full_reset("🚨 Redémarrage forcé - Blocage détecté (>20 numéros)")
        return True
    
    # Si on atteint #1440, reset complet
    if current_game_number >= 1440:
        logger.warning(f"🚨 RESET #1440 atteint")
        await perform_full_reset("🚨 Reset automatique - Numéro #1440 atteint")
        return True
    
    return False

async def send_prediction(game_number: int, suit: str, prediction_type: str = 'standard', is_rattrapage: int = 0) -> Optional[int]:
    """Envoie une prédiction au canal configuré."""
    global last_prediction_time, last_prediction_sent_time, last_prediction_number, games_without_prediction
    
    try:
        # Vérifier si la couleur est bloquée
        if suit in suit_block_until and datetime.now() < suit_block_until[suit]:
            logger.info(f"🔒 {suit} bloqué, prédiction annulée")
            return None
        
        # Vérifier si on peut envoyer (pas de prédiction en cours)
        if not await can_send_prediction():
            logger.info(f"⏳ Prédiction en cours existante, mise en file d'attente: #{game_number} {suit}")
            prediction_queue.append({
                'game_number': game_number,
                'suit': suit,
                'type': prediction_type,
                'added_at': datetime.now()
            })
            return None
        
        # Vérifier si le numéro à prédire est à N+2 du dernier numéro reçu
        # Attendre que last_source_game_number >= game_number - 2
        if last_source_game_number < game_number - 2:
            logger.info(f"⏳ Attente approche numéro cible: {last_source_game_number}/{game_number - 2}")
            prediction_queue.append({
                'game_number': game_number,
                'suit': suit,
                'type': prediction_type,
                'added_at': datetime.now()
            })
            return None
        
        # VÉRIFICATION CRITIQUE: Canal configuré ?
        if not PREDICTION_CHANNEL_ID:
            logger.error("❌ PREDICTION_CHANNEL_ID non configuré dans config.py!")
            return None
        
        # RÉSOLUTION DU CANAL avec normalisation d'ID
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error(f"❌ Impossible d'accéder au canal {PREDICTION_CHANNEL_ID}")
            return None
        
        # Préparer le message
        type_indicator = ""
        if prediction_type == 'distribution':
            type_indicator = " [#R]"
        elif prediction_type == 'compteur2':
            type_indicator = " [C2]"
            
        msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours....{type_indicator}"""
        
        # ENVOI avec gestion d'erreurs spécifiques
        try:
            sent = await client.send_message(prediction_entity, msg)
            last_prediction_time = datetime.now()
            last_prediction_sent_time = datetime.now()
            last_prediction_number = game_number
            games_without_prediction = 0  # Reset du compteur
            
            # Stockage de la prédiction
            pending_predictions[game_number] = {
                'suit': suit,
                'message_id': sent.id,
                'status': 'en_cours',
                'rattrapage': is_rattrapage,
                'original_game': game_number if is_rattrapage == 0 else None,
                'awaiting_rattrapage': 0,
                'sent_time': datetime.now(),
                'type': prediction_type
            }
            
            # Historique
            if is_rattrapage == 0:
                verification_games = [game_number, game_number + 1, game_number + 2]
                add_prediction_to_history(game_number, suit, verification_games, prediction_type)
                logger.info(f"📋 Prédiction #{game_number} {suit} ({prediction_type}): vérification sur {verification_games}")
            
            logger.info(f"✅ Prédiction envoyée: #{game_number} {suit} ({prediction_type})")
            return sent.id
            
        except ChatWriteForbiddenError:
            logger.error(f"❌ Bot n'a pas la permission d'écrire dans le canal")
            return None
        except UserBannedInChannelError:
            logger.error(f"❌ Bot banni du canal")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erreur inattendue envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def process_prediction_queue():
    """Traite la file d'attente des prédictions."""
    global prediction_queue
    
    if not prediction_queue:
        return
    
    # Vérifier si on peut envoyer
    if not await can_send_prediction():
        return
    
    # Traiter la première prédiction en attente
    if prediction_queue:
        pred = prediction_queue[0]
        # Vérifier si le timing est bon (N-2 atteint)
        if last_source_game_number >= pred['game_number'] - 2:
            prediction_queue.pop(0)
            await send_prediction(pred['game_number'], pred['suit'], pred['type'])
        else:
            # Vérifier si la prédiction en attente est trop vieille (>5 min)
            if datetime.now() - pred['added_at'] > timedelta(minutes=5):
                logger.warning(f"⚠️ Prédiction en file d'attente trop vieille, suppression: #{pred['game_number']}")
                prediction_queue.pop(0)

async def check_prediction_result(game_number: int, first_group: str) -> bool:
    """Vérifie si une prédiction est gagnante."""
    suits_in_result = get_suits_in_group(first_group)
    
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            pred_type = pred.get('type', 'standard')
            logger.info(f"🔍 Vérif #{game_number} original ({pred_type}): {target_suit} dans {suits_in_result}")
            
            if target_suit in suits_in_result:
                await update_prediction_message(game_number, '✅0️⃣', True)
                update_prediction_in_history(game_number, target_suit, game_number, first_group, 0, 'gagne_r0')
                await process_prediction_queue()  # Traiter la file d'attente
                return True
            else:
                pred['awaiting_rattrapage'] = 1
                logger.info(f"❌ #{game_number} échoué, attente rattrapage #{game_number + 1}")
                return False
    
    for original_game, pred in list(pending_predictions.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting > 0 and game_number == original_game + awaiting:
            target_suit = pred['suit']
            logger.info(f"🔍 Vérif rattrapage R{awaiting} #{game_number}: {target_suit}")
            
            if target_suit in suits_in_result:
                status = f'✅{awaiting}️⃣'
                await update_prediction_message(original_game, status, True, awaiting)
                final_status = f'gagne_r{awaiting}'
                update_prediction_in_history(original_game, target_suit, game_number, first_group, awaiting, final_status)
                await process_prediction_queue()  # Traiter la file d'attente
                return True
            else:
                if awaiting < 2:
                    pred['awaiting_rattrapage'] = awaiting + 1
                    logger.info(f"❌ R{awaiting} échoué, attente #{original_game + awaiting + 1}")
                    return False
                else:
                    logger.info(f"❌ R2 échoué, prédiction perdue")
                    await update_prediction_message(original_game, '❌', False)
                    update_prediction_in_history(original_game, target_suit, game_number, first_group, 2, 'perdu')
                    await process_prediction_queue()  # Traiter la file d'attente
                    return False
    
    return False

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    """Met à jour le statut d'une prédiction."""
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    pred_type = pred.get('type', 'standard')
    
    type_indicator = ""
    if pred_type == 'distribution':
        type_indicator = " [#R]"
    elif pred_type == 'compteur2':
        type_indicator = " [C2]"
    
    if status == '✅0️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅0️⃣ GAGNÉ{type_indicator}"
    elif status == '✅1️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅1️⃣ GAGNÉ{type_indicator}"
    elif status == '✅2️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅2️⃣ GAGNÉ{type_indicator}"
    else:
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ❌ PERDU 😭{type_indicator}"
    
    new_msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {result_line}"""
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal de prédiction non accessible pour mise à jour")
            return
            
        await client.edit_message(prediction_entity, msg_id, new_msg)
        pred['status'] = status
        
        if trouve:
            logger.info(f"✅ Gagné: #{game_number} {status}")
        else:
            logger.info(f"❌ Perdu: #{game_number}")
            block_suit(suit, 5)
        
        del pending_predictions[game_number]
        
    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

# ============================================================================
# NOUVELLES FONCTIONS: GESTION #R ET COMPTEUR2
# ============================================================================

def extract_first_two_groups(message: str) -> tuple:
    """Extrait les 2 premiers groupes de parenthèses."""
    groups = extract_parentheses_groups(message)
    if len(groups) >= 2:
        return groups[0], groups[1]
    elif len(groups) == 1:
        return groups[0], ""
    return "", ""

def check_distribution_rule(game_number: int, message_text: str) -> Optional[tuple]:
    """
    Vérifie la règle #R (Distribution).
    Retourne (suit_manquant, numero_prediction) ou None.
    """
    # Vérifier si #R est présent
    if '#R' not in message_text:
        return None
    
    # Extraire les 2 premiers groupes
    first_group, second_group = extract_first_two_groups(message_text)
    
    if not first_group and not second_group:
        return None
    
    # Récupérer tous les costumes des 2 groupes
    suits_first = set(get_suits_in_group(first_group))
    suits_second = set(get_suits_in_group(second_group))
    all_suits_found = suits_first.union(suits_second)
    
    # Trouver le costume manquant
    all_suits = set(ALL_SUITS)
    missing_suits = all_suits - all_suits_found
    
    # Doit avoir exactement 1 costume manquant
    if len(missing_suits) == 1:
        missing_suit = list(missing_suits)[0]
        prediction_number = game_number + 5
        logger.info(f"🎯 #R DÉTECTÉ: {missing_suit} manquant dans groupes '{first_group}' et '{second_group}' → Prédiction #{prediction_number}")
        return (missing_suit, prediction_number)
    
    return None

def update_compteur2(game_number: int, first_group: str):
    """
    Met à jour les compteurs Compteur2 basé sur le 1er groupe.
    Incrémente pour les costumes manquants, reset pour les présents.
    """
    global compteur2_trackers, compteur2_seuil_B
    
    suits_in_first = set(get_suits_in_group(first_group))
    all_suits = set(ALL_SUITS)
    
    # Pour chaque costume
    for suit in ALL_SUITS:
        tracker = compteur2_trackers[suit]
        
        if suit in suits_in_first:
            # Costume présent → reset
            tracker.reset(game_number)
        else:
            # Costume manquant → incrémenter
            tracker.increment(game_number)
            
            # Vérifier si seuil atteint
            if tracker.check_threshold(compteur2_seuil_B):
                logger.info(f"🚨 Compteur2 {suit} a atteint le seuil {compteur2_seuil_B}!")
                # La prédiction sera gérée dans process_game_result

def get_compteur2_ready_predictions(current_game: int) -> List[tuple]:
    """
    Retourne les costumes du Compteur2 prêts à prédire.
    Format: [(suit, prediction_number), ...]
    """
    global compteur2_trackers, compteur2_seuil_B
    
    ready = []
    for suit in ALL_SUITS:
        tracker = compteur2_trackers[suit]
        if tracker.check_threshold(compteur2_seuil_B):
            # Prédire à N+2 où N est le dernier numéro reçu (current_game)
            pred_number = current_game + 2
            ready.append((suit, pred_number))
            # Reset après prédiction
            tracker.reset(current_game)
    
    return ready

# ============================================================================
# TRAITEMENT DES MESSAGES - MODIFIÉ
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    """Traite un résultat de jeu finalisé."""
    global current_game_number, last_source_game_number, games_without_prediction
    
    current_game_number = game_number
    last_source_game_number = game_number
    games_without_prediction += 1
    
    # Vérifier si redémarrage forcé nécessaire
    await check_and_force_restart_if_needed()
    
    # Mettre à jour les trackers de cycles
    for tracker in cycle_trackers.values():
        tracker.update_to_current_game(game_number)
    
    # Extraire les groupes
    groups = extract_parentheses_groups(message_text)
    if not groups:
        logger.warning(f"⚠️ Pas de groupe trouvé dans #{game_number}")
        return
    
    first_group = groups[0]
    suits_in_first = get_suits_in_group(first_group)
    
    logger.info(f"📊 Jeu #{game_number}: {suits_in_first} dans '{first_group[:30]}...'")
    
    add_to_history(game_number, message_text, first_group, suits_in_first)
    
    # === VÉRIFICATION DES PRÉDICTIONS EXISTANTES ===
    if await check_prediction_result(game_number, first_group):
        return
    
    # === NOUVEAU: GESTION #R (Distribution) ===
    distribution_result = check_distribution_rule(game_number, message_text)
    if distribution_result:
        suit, pred_num = distribution_result
        # Vérifier qu'on n'a pas déjà une prédiction pour ce numéro
        if pred_num not in pending_predictions:
            await send_prediction(pred_num, suit, 'distribution')
            return  # Priorité à #R, on ne fait pas Compteur2 sur ce message
    
    # === NOUVEAU: MISE À JOUR COMPTEUR2 ===
    if compteur2_active:
        update_compteur2(game_number, first_group)
        
        # Vérifier si des seuils sont atteints et envoyer les prédictions
        compteur2_preds = get_compteur2_ready_predictions(game_number)
        for suit, pred_num in compteur2_preds:
            if pred_num not in pending_predictions:
                await send_prediction(pred_num, suit, 'compteur2')
    
    # === TRAITEMENT CYCLES EXISTANTS (Hyper Serré/Standard) ===
    for suit, tracker in cycle_trackers.items():
        pred_num = tracker.process_verification(game_number, suit in suits_in_first)
        if pred_num:
            await send_prediction(pred_num, suit, 'standard', 0)

async def handle_message(event, is_edit: bool = False):
    """Gère les messages entrants et édités."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        
        # Normaliser l'ID source pour comparaison
        normalized_source = normalize_channel_id(SOURCE_CHANNEL_ID)
        if chat_id != normalized_source:
            return
        
        message_text = event.message.message
        edit_info = " [EDITÉ]" if is_edit else ""
        logger.info(f"📨{edit_info} Msg {event.message.id}: {message_text[:60]}...")
        
        # === NOUVEAU: Ignorer si message en cours d'édition (contient ⏰) ===
        if is_message_being_edited(message_text):
            logger.info(f"⏳ Message en cours d'édition (⏰), ignoré pour l'instant")
            # Stocker pour traitement ultérieur quand il sera finalisé
            if '⏰' in message_text:
                match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
                if match:
                    waiting_finalization[int(match.group(1))] = {
                        'msg_id': event.message.id,
                        'text': message_text
                    }
            return
        
        # Vérifier si finalisé
        if not is_message_finalized(message_text):
            logger.info(f"⏳ Non finalisé ignoré")
            return
        
        match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
        if not match:
            match = re.search(r"(?:^|[^\d])(\d{3,4})(?:[^\d]|$)", message_text)
        
        if not match:
            logger.warning("⚠️ Numéro non trouvé")
            return
        
        game_number = int(match.group(1))
        
        if game_number in waiting_finalization:
            del waiting_finalization[game_number]
        
        await process_game_result(game_number, message_text)
        
    except Exception as e:
        logger.error(f"❌ Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def handle_new_message(event):
    await handle_message(event, False)

async def handle_edited_message(event):
    await handle_message(event, True)

# ============================================================================
# RESET AUTOMATIQUE
# ============================================================================

async def auto_reset_system():
    """Reset automatique après 1h ou à 1h00."""
    global last_prediction_time
    
    while True:
        try:
            now = datetime.now()
            
            if now.hour == 1 and now.minute == 0:
                logger.info("🕐 Reset 1h00")
                await perform_full_reset("🕐 Reset automatique 1h00")
                await asyncio.sleep(60)
            
            if last_prediction_time:
                elapsed = now - last_prediction_time
                if elapsed > timedelta(hours=1) and pending_predictions:
                    logger.info(f"⏰ Reset inactivité ({elapsed.total_seconds()/3600:.1f}h)")
                    await perform_full_reset("⏰ Reset inactivité 1h")
            
            # Vérifier la file d'attente des prédictions
            await process_prediction_queue()
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"❌ Erreur auto_reset: {e}")
            await asyncio.sleep(60)

async def perform_full_reset(reason: str):
    """Effectue un reset complet."""
    global pending_predictions, last_prediction_time, waiting_finalization, prediction_queue
    global games_without_prediction, last_prediction_number, last_prediction_sent_time
    global compteur2_trackers
    
    stats = len(pending_predictions)
    
    for tracker in cycle_trackers.values():
        tracker.reset()
    
    # Reset Compteur2
    for tracker in compteur2_trackers.values():
        tracker.counter = 0
        tracker.last_increment_game = 0
    
    pending_predictions.clear()
    waiting_finalization.clear()
    prediction_queue.clear()
    last_prediction_time = None
    last_prediction_sent_time = None
    last_prediction_number = 0
    games_without_prediction = 0
    suit_block_until.clear()
    
    logger.info(f"🔄 {reason} - {stats} prédictions cleared, Compteur2 reset")
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and client and client.is_connected():
            await client.send_message(
                prediction_entity,
                f"""🔄 **RESET SYSTÈME**

{reason}

✅ Compteurs cycles remis à zéro
✅ Compteurs Compteur2 remis à zéro
✅ {stats} prédictions cleared
✅ File d'attente vidée
✅ Nouvelle analyse

⏳BACCARAT AI 🤖⏳"""
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN - MODIFIÉES ET NOUVELLES
# ============================================================================

async def cmd_compteur2(event):
    """Commande /compteur2 - Affiche et configure le Compteur2."""
    global compteur2_seuil_B, compteur2_active, compteur2_trackers
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            # Afficher le statut actuel
            status_str = "✅ ACTIF" if compteur2_active else "❌ INACTIF"
            
            lines = [
                "📊 **COMPTEUR2 - STATUT**",
                f"Statut: {status_str}",
                f"🎯 Seuil B (prédiction): {compteur2_seuil_B}",
                f"🎮 Dernier jeu analysé: #{current_game_number}",
                "",
                "📈 **Compteurs actuels:**"
            ]
            
            for suit in ALL_SUITS:
                tracker = compteur2_trackers.get(suit)
                if tracker:
                    progress = min(tracker.counter, compteur2_seuil_B)
                    bar_filled = '█' * progress
                    bar_empty = '░' * (compteur2_seuil_B - progress)
                    bar = f"[{bar_filled}{bar_empty}]"
                    
                    if tracker.counter >= compteur2_seuil_B:
                        status = "🔮 PRÊT"
                    elif tracker.counter > 0:
                        status = f"⏳ En cours ({tracker.counter}/{compteur2_seuil_B})"
                    else:
                        status = "✅ En attente"
                    
                    lines.append(f"{tracker.get_display_name()}: {bar} {status}")
            
            lines.extend([
                "",
                "💡 **Règle:**",
                f"• Compte les costumes manquants dans le 1er groupe",
                f"• Incrémente quand costume absent",
                f"• Reset à 0 quand costume présent",
                f"• Prédiction à N+2 quand compteur = {compteur2_seuil_B}",
                "",
                "**Usage:**",
                "`/compteur2 [2-10]` - Définir le seuil B",
                "`/compteur2 on` - Activer",
                "`/compteur2 off` - Désactiver",
                "`/compteur2 reset` - Reset les compteurs"
            ])
            
            await event.respond("\n".join(lines))
            return
        
        arg = parts[1].lower()
        
        if arg == 'off':
            compteur2_active = False
            await event.respond("❌ **Compteur2 DÉSACTIVÉ**")
            logger.info("Admin désactive Compteur2")
            return
        
        if arg == 'on':
            compteur2_active = True
            await event.respond("✅ **Compteur2 ACTIVÉ**")
            logger.info("Admin active Compteur2")
            return
        
        if arg == 'reset':
            for tracker in compteur2_trackers.values():
                tracker.counter = 0
                tracker.last_increment_game = 0
            await event.respond("🔄 **Compteurs Compteur2 remis à zéro**")
            logger.info("Admin reset Compteur2")
            return
        
        try:
            b_val = int(arg)
            if not 2 <= b_val <= 10:
                await event.respond("❌ B doit être entre 2 et 10")
                return
            
            old_b = compteur2_seuil_B
            compteur2_seuil_B = b_val
            
            await event.respond(
                f"✅ **Seuil B modifié**\n\n"
                f"Ancien: {old_b}\n"
                f"Nouveau: **{compteur2_seuil_B}**\n\n"
                f"💡 Règle: {compteur2_seuil_B} manques consécutifs → prédiction N+2"
            )
            logger.info(f"Admin change seuil B: {old_b} → {b_val}")
            
        except ValueError:
            await event.respond("❌ Usage: `/compteur2 [2-10]`, `/compteur2 on/off/reset`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_compteur2: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_h(event):
    """Commande /h - définit le nombre h de numéros à vérifier en mode hyper serré."""
    global hyper_serré_active, hyper_serré_h
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            mode_str = "✅ ACTIF" if hyper_serré_active else "❌ INACTIF"
            
            if hyper_serré_active:
                example = f"Cycle 596 échoue sur 596-600 → Prédit 602 (596+5+1)"
            else:
                example = f"Cycle 1020 échoue sur 1020-1022 → Prédit 1025 (1020+6-1)"
            
            await event.respond(
                f"📊 **MODE HYPER SERRÉ**\n\n"
                f"Statut: {mode_str}\n"
                f"h = {hyper_serré_h} numéros à vérifier\n\n"
                f"📋 **Fonctionnement actuel:**\n"
                f"• {example}\n"
                f"• Vérification après prédiction: 3 numéros (prédit, +1, +2)\n\n"
                f"**Usage:**\n"
                f"`/h [3-15]` - Activer avec h numéros\n"
                f"`/h off` - Désactiver (mode standard)\n"
                f"`/h on` - Réactiver avec la dernière valeur"
            )
            return
        
        arg = parts[1].lower()
        
        if arg == 'off':
            hyper_serré_active = False
            for tracker in cycle_trackers.values():
                tracker.reset()
            await event.respond(
                f"❌ **Mode hyper serré DÉSACTIVÉ**\n\n"
                f"Retour au mode standard:\n"
                f"• Vérifie {NUMBERS_PER_TOUR} numéros consécutifs\n"
                f"• Prédit: cycle + intervalle - 1\n"
                f"• Exemple: ♥️ cycle 1020 échoue sur 1020-1022 → prédit 1025\n\n"
                f"✅ Compteurs reset pour nouvelle analyse."
            )
            logger.info(f"Admin désactive mode hyper serré")
            return
        
        if arg == 'on':
            hyper_serré_active = True
            for tracker in cycle_trackers.values():
                tracker.reset()
            await event.respond(
                f"✅ **Mode hyper serré ACTIVÉ**\n\n"
                f"h = {hyper_serré_h} numéros à vérifier\n"
                f"Prédiction = cycle + h + 1\n"
                f"Exemple: cycle 596 échoue sur 596-600 → prédit 602\n\n"
                f"✅ Compteurs reset pour nouvelle analyse."
            )
            logger.info(f"Admin active mode hyper serré (h={hyper_serré_h})")
            return
        
        try:
            h_val = int(arg)
            if not 3 <= h_val <= 15:
                await event.respond("❌ h doit être entre 3 et 15")
                return
            
            old_h = hyper_serré_h
            hyper_serré_h = h_val
            hyper_serré_active = True
            
            for tracker in cycle_trackers.values():
                tracker.reset()
            
            example_cycle = 596
            example_pred = example_cycle + h_val + 1
            
            await event.respond(
                f"✅ **Mode hyper serré configuré**\n\n"
                f"h: {old_h} → **{hyper_serré_h}**\n"
                f"Statut: ✅ ACTIF\n\n"
                f"📋 **Nouvelle logique:**\n"
                f"• Vérifie **{h_val}** numéros consécutifs depuis le cycle\n"
                f"• Si tous échouent → prédit **cycle + h + 1**\n"
                f"• Exemple: cycle {example_cycle} échoue sur {example_cycle}-{example_cycle+h_val-1}\n"
                f"  → Prédiction: **{example_pred}**\n"
                f"• Vérification: {example_pred}, {example_pred+1}, {example_pred+2}\n\n"
                f"✅ Compteurs reset pour nouvelle analyse."
            )
            logger.info(f"Admin set h={h_val} (hyper serré)")
            
        except ValueError:
            await event.respond("❌ Usage: `/h [3-15]`, `/h on` ou `/h off`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_h: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_history(event):
    """Affiche l'historique des 5 derniers messages finalisés et prédictions."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    lines = [
        "📜 **HISTORIQUE DES 5 DERNIERS MESSAGES FINALISÉS**",
        "═══════════════════════════════════════",
        ""
    ]
    
    recent_messages = finalized_messages_history[:5]
    
    if not recent_messages:
        lines.append("❌ Aucun message dans l'historique")
    else:
        for i, msg in enumerate(recent_messages, 1):
            time_str = msg['timestamp'].strftime('%H:%M:%S')
            game_num = msg['game_number']
            group = msg['first_group']
            suits = ', '.join([SUIT_DISPLAY.get(s, s) for s in msg['suits_found']]) if msg['suits_found'] else 'Aucune'
            
            verif_indicator = ""
            if msg['predictions_verified']:
                verif_details = []
                for v in msg['predictions_verified']:
                    suit_display = SUIT_DISPLAY.get(v['suit'], v['suit'])
                    if v['rattrapage_level'] == 0:
                        verif_details.append(f"✅0️⃣ #{v['predicted_game']}{suit_display}")
                    elif v['rattrapage_level'] == 1:
                        verif_details.append(f"✅1️⃣ #{v['predicted_game']}{suit_display}")
                    elif v['rattrapage_level'] == 2:
                        verif_details.append(f"✅2️⃣ #{v['predicted_game']}{suit_display}")
                verif_indicator = "\n   🔍 Vérification: " + " | ".join(verif_details)
            
            lines.append(
                f"{i}. 🕐 `{time_str}` | **Jeu #{game_num}**\n"
                f"   📝 `{group}`\n"
                f"   🎨 Couleurs: {suits}{verif_indicator}"
            )
            lines.append("")
    
    lines.append("🔮 **PRÉDICTIONS RÉCENTES**")
    lines.append("───────────────────────────────────────")
    
    recent_predictions = prediction_history[:5]
    
    if not recent_predictions:
        lines.append("❌ Aucune prédiction dans l'historique")
    else:
        for pred in recent_predictions:
            pred_game = pred['predicted_game']
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            status = pred['status']
            pred_time = pred['predicted_at'].strftime('%H:%M:%S')
            pred_type = pred.get('type', 'standard')
            
            type_emoji = {'standard': '🔁', 'distribution': '#️⃣', 'compteur2': '2️⃣'}.get(pred_type, '❓')
            
            if status == 'en_cours':
                status_str = "⏳ En cours..."
            elif status == 'gagne_r0':
                status_str = "✅0️⃣ GAGNÉ direct"
            elif status == 'gagne_r1':
                status_str = "✅1️⃣ GAGNÉ R1"
            elif status == 'gagne_r2':
                status_str = "✅2️⃣ GAGNÉ R2"
            elif status == 'perdu':
                status_str = "❌ PERDU"
            else:
                status_str = f"❓ {status}"
            
            lines.append(f"{type_emoji} **#{pred_game}** {suit} | {status_str}")
            lines.append(f"   🕐 Prédit à: {pred_time}")
            
            if pred['verified_by']:
                lines.append("   📋 Vérifié par:")
                for v in pred['verified_by']:
                    r_text = f"R{v['rattrapage_level']}" if v['rattrapage_level'] > 0 else "Direct"
                    lines.append(f"      • Jeu #{v['game_number']} ({r_text}): `{v['first_group']}`")
            else:
                verif_games = pred['verification_games']
                if status == 'en_cours':
                    pending_games = [g for g in verif_games if g > current_game_number]
                    checked_games = [g for g in verif_games if g <= current_game_number]
                    
                    if checked_games:
                        lines.append(f"   ✅ Déjà vérifié: {', '.join(['#' + str(g) for g in checked_games])}")
                    if pending_games:
                        lines.append(f"   ⏳ En attente: {', '.join(['#' + str(g) for g in pending_games])}")
            
            lines.append("")
    
    lines.append("═══════════════════════════════════════")
    lines.append("💡 **Légende:**")
    lines.append("• 🔁 = Cycle standard")
    lines.append("• #️⃣ = Distribution (#R)")
    lines.append("• 2️⃣ = Compteur2")
    
    await event.respond("\n".join(lines))

async def cmd_status(event):
    """Affiche les compteurs détaillés."""
    global hyper_serré_active, hyper_serré_h, compteur2_active, compteur2_seuil_B
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    if hyper_serré_active:
        mode_str = f"🔥 HYPER SERRÉ (h={hyper_serré_h})"
        count_needed = hyper_serré_h
        pred_formula = f"cycle + {hyper_serré_h} + 1"
    else:
        mode_str = f"📊 STANDARD ({NUMBERS_PER_TOUR} num/tour)"
        count_needed = NUMBERS_PER_TOUR
        pred_formula = "cycle + intervalle - 1"
    
    compteur2_str = "✅ ON" if compteur2_active else "❌ OFF"
    
    lines = [
        "📈 **COUNTERS DE MANQUES DES CYCLES**",
        f"Mode Cycles: {mode_str}",
        f"Formule prédiction: {pred_formula}",
        f"Mode Compteur2: {compteur2_str} (seuil B={compteur2_seuil_B})",
        "",
        f"🎮 Dernier jeu: #{current_game_number}",
        f"📋 Prédictions actives: {len(pending_predictions)}",
        f"⏳ File d'attente: {len(prediction_queue)}",
        f"🚫 Sans prédiction: {games_without_prediction}/20",
        ""
    ]
    
    for suit in ALL_SUITS:
        if suit not in cycle_trackers:
            continue
        
        tracker = cycle_trackers[suit]
        tracker.update_to_current_game(current_game_number)
        
        current = tracker.get_current_cycle_target()
        to_check = tracker.get_numbers_to_check_this_tour()
        checked = tracker.tour_checked_numbers
        
        progress = len(checked)
        
        bar_filled = '█' * progress
        bar_empty = '░' * (count_needed - progress)
        bar = f"[{bar_filled}{bar_empty}]"
        
        if tracker.pending_prediction:
            emoji, status = "🔮", f"PRÉDICTION #{tracker.pending_prediction}"
        elif tracker.current_tour == 2:
            emoji, status = "⚠️", f"Tour 2 critique"
        elif progress > 0:
            emoji, status = "⏳", f"Tour {tracker.current_tour} en cours"
        else:
            emoji, status = "✅", "En attente"
        
        nums = []
        for i, n in enumerate(to_check):
            if n in checked:
                found = tracker.verification_history.get(n, False)
                nums.append(f"{'✅' if found else '❌'}{n}")
            else:
                nums.append(f"⏳{n}")
        
        if hyper_serré_active:
            if current:
                pred_num = current + hyper_serré_h + 1
                pred_info = f"Si échec → prédit #{pred_num} (cycle+{hyper_serré_h}+1)"
            else:
                pred_info = "N/A"
        else:
            if current:
                interval = SUIT_CYCLES[suit]['interval']
                pred_num = current + interval - 1
                pred_info = f"Si échec → prédit #{pred_num} (cycle+{interval}-1)"
            else:
                pred_info = "N/A"
        
        lines.extend([
            f"📊 {tracker.get_display_name()} {emoji}",
            f"   ├─ 🎯 Cycle: #{current if current else 'N/A'}",
            f"   ├─ 🔄 Tour: {tracker.current_tour}/{CONSECUTIVE_FAILURES_NEEDED}",
            f"   ├─ 📉 Manques: {tracker.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED}",
            f"   ├─ 🔍 {bar} ({progress}/{count_needed})",
            f"   ├─ 🎲 {' → '.join(nums) if nums else 'N/A'}",
            f"   ├─ 📝 {pred_info}",
            f"   └─ 📌 {status}",
            ""
        ])
    
    # Ajouter section Compteur2
    lines.append("📊 **COMPTEUR2**")
    lines.append("─────────────────────")
    for suit in ALL_SUITS:
        tracker = compteur2_trackers.get(suit)
        if tracker:
            progress = min(tracker.counter, compteur2_seuil_B)
            bar_filled = '█' * progress
            bar_empty = '░' * (compteur2_seuil_B - progress)
            bar = f"[{bar_filled}{bar_empty}]"
            
            if tracker.counter >= compteur2_seuil_B:
                status = "🔮 PRÊT"
            elif tracker.counter > 0:
                status = f"⏳ En cours ({tracker.counter}/{compteur2_seuil_B})"
            else:
                status = "✅ En attente"
            
            lines.append(f"{tracker.get_display_name()}: {bar} {status}")
    
    lines.append("")
    
    if pending_predictions:
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            r = pred.get('rattrapage', 0)
            ar = pred.get('awaiting_rattrapage', 0)
            ptype = pred.get('type', 'standard')
            
            type_emoji = {'standard': '🔁', 'distribution': '#️⃣', 'compteur2': '2️⃣'}.get(ptype, '❓')
            
            if ar > 0:
                status_str = f"attente R{ar} (#{num + ar})"
            else:
                status_str = pred['status']
            
            lines.append(f"• #{num} {suit} {type_emoji}: {status_str}")
        lines.append("")
    
    lines.extend([
        "**Légende:**",
        "✅=Trouvé ❌=Manqué ⏳=Attente 🔮=Prédiction ⚠️=Critique",
        "🔁=Cycle #️⃣=Distribution 2️⃣=Compteur2"
    ])
    
    await event.respond("\n".join(lines))

async def cmd_help(event):
    """Affiche l'aide complète avec TOUTES les commandes."""
    if event.is_group or event.is_channel:
        return
    
    help_text = f"""📖 **BACCARAT AI - AIDE COMPLÈTE**

**🎮 Systèmes de prédiction:**

1️⃣ **Cycles (Standard/Hyper Serré)**
• Mode Standard: 3 échecs → prédit cycle+intervalle-1
• Mode Hyper Serré (/h): h échecs → prédit cycle+h+1

2️⃣ **Distribution (#R)**
• Message avec #R et finalisé
• Vérifie 1er ET 2ème groupe de parenthèses
• Si 1 costume manquant exactement → prédit #N+5

3️⃣ **Compteur2**
• Compte costumes manquants dans 1er groupe
• Incrémente quand absent, reset quand présent
• Seuil B atteint → prédit N+2

**📋 Règles de sécurité:**
• Jamais 2 prédictions simultanées
• Attente N-2 avant envoi
• Auto-reset si >20 numéros sans prédiction
• Reset complet au #1440

**🔧 Commandes Admin:**

`/status` - Voir tous les compteurs
`/compteur2 [B/on/off/reset]` - Gérer Compteur2
`/h [n/on/off]` - Mode hyper serré
`/history` - Historique messages et prédictions
`/reset` - Reset manuel complet
`/help` - Cette aide

**💡 Détails:**
• ⏰ = Message en cours d'édition (ignoré)
• ✅/🔰 = Message finalisé (traité)
• Vérification: 3 numéros (prédit, +1, +2)

⏳BACCARAT AI 🤖⏳"""
    
    await event.respond(help_text)

async def cmd_reset(event):
    """Reset manuel."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel")
    await event.respond("✅ Reset effectué!")

def setup_handlers():
    """Configure les handlers."""
    # NOUVEAU: Commande compteur2
    client.add_event_handler(cmd_compteur2, events.NewMessage(pattern=r'^/compteur2'))
    
    client.add_event_handler(cmd_h, events.NewMessage(pattern=r'^/h'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    
    client.add_event_handler(handle_new_message, events.NewMessage())
    client.add_event_handler(handle_edited_message, events.MessageEdited())

async def start_bot():
    """Démarre le bot."""
    global client, prediction_channel_ok
    
    session = os.getenv('TELEGRAM_SESSION', '')
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    
    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()
        initialize_trackers(3000)
        
        # Vérifier le canal de prédiction au démarrage
        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal prédiction OK: {getattr(pred_entity, 'title', 'Unknown')}")
                else:
                    logger.error(f"❌ Canal prédition inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal prédiction: {e}")
        
        logger.info("🤖 Bot démarré avec succès")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    """Fonction principale."""
    try:
        if not await start_bot():
            return
        
        asyncio.create_task(auto_reset_system())
        logger.info("🔄 Auto-reset démarré")
        
        # Serveur web Render
        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT AI 🤖 Running"))
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"🌐 Web server port {PORT}")
        logger.info(f"📊 Mode: {'Hyper serré h=' + str(hyper_serré_h) if hyper_serré_active else 'Standard'}")
        logger.info(f"📊 Compteur2: {'Actif B=' + str(compteur2_seuil_B) if compteur2_active else 'Inactif'}")
        
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info("🔌 Déconnecté")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
