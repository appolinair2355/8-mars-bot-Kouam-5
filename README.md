# 🤖 BACCARAT AI

Bot Telegram de prédiction pour Baccarat basé sur l'analyse des cycles numériques.

## 📋 Principe de Fonctionnement

Chaque couleur de carte suit un **cycle numérique fixe**:

| Couleur | Intervalle | Séquence |
|---------|-----------|----------|
| ♠️ Pique | +5 | 1, 6, 11, 16, 21, 26, 31, 36, 41, 46... |
| ❤️ Cœur | +6 | 1, 7, 13, 19, 25, 31, 37, 43, 49, 55... |
| ♦️ Carreau | +6 | 1, 7, 13, 19, 25, 31, 37, 43, 49, 55... |
| ♣️ Trèfle | +7 | 1, 8, 15, 22, 29, 36, 43, 50, 57, 64... |

## 🎯 Système de Prédiction

### Compteur de Manques

Le bot analyse chaque jeu reçu et compte les **manques consécutifs** pour chaque couleur:

```
📊 ♠️ Pique
   ├─ 🎯 Numéro du cycle analysé: #756
   ├─ 📉 Compteur de manques: 2/2 [██]
   ├─ 🔄 Tour: 2/2
   └─ 🔮 PRÉDICTION: Jouer #761
```

### Tours de Vérification

- **Tour 1**: Vérification au 1er numéro du cycle → Si manqué, compteur = 1
- **Tour 2**: Vérification au 2ème numéro du cycle → Si manqué, compteur = 2
- **Prédiction**: Après 2 manques, prédiction au 3ème numéro du cycle

### Exemple

```
Jeu #751: ♠️ attendu mais absent → Manque #1 [█░]
Jeu #756: ♠️ attendu mais absent → Manque #2 [██]
→ 🔮 PRÉDICTION pour jeu #761: ♠️
```

## 📁 Structure des Fichiers

```
.
├── config.py          # Configuration (canaux, cycles, paramètres)
├── main.py            # Code principal du bot
├── requirements.txt   # Dépendances Python
└── README.md          # Ce fichier
```

## 🚀 Installation

### 1. Cloner/Préparer les fichiers

```bash
# Créer le dossier
mkdir baccarat-ai
cd baccarat-ai

# Copier les fichiers
cp /mnt/kimi/output/config_final.py config.py
cp /mnt/kimi/output/baccarat_ai_final.py main.py
cp /mnt/kimi/output/requirements.txt .
```

### 2. Installer les dépendances

```bash
pip install -r requirements.txt
```

### 3. Configurer les variables d'environnement

Créer un fichier `.env`:

```env
# Telegram API (obtenir sur https://my.telegram.org)
API_ID=123456
API_HASH=votre_api_hash_ici

# Bot Telegram (obtenir via @BotFather)
BOT_TOKEN=votre_bot_token_ici

# ID de l'administrateur
ADMIN_ID=votre_telegram_id

# Canaux (obtenir via @userinfobot)
SOURCE_CHANNEL_ID=-1002682552255
PREDICTION_CHANNEL_ID=-1002543915361

# Optionnel: Session string pour persistance
TELEGRAM_SESSION=
```

### 4. Lancer le bot

```bash
python main.py
```

## 💬 Format des Messages

### Message de Prédiction

```
⏳BACCARAT AI 🤖⏳

PLAYER : 761 ♠️ : en cours....
```

### Mise à Jour (Résultat)

**Si trouvé:**
```
⏳BACCARAT AI 🤖⏳

PLAYER : 761 ♠️ : ✅ Trouvé
```

**Si manqué:**
```
⏳BACCARAT AI 🤖⏳

PLAYER : 761 ♠️ : ❌ Manqué
```

## 🎮 Commandes Admin

| Commande | Description |
|----------|-------------|
| `/status` | Affiche les compteurs de manques pour toutes les couleurs |
| `/help` | Affiche l'aide du bot |

## ⚙️ Configuration Avancée

Dans `config.py`, vous pouvez modifier:

```python
# Nombre de manques avant prédiction (défaut: 2)
CONSECUTIVE_FAILURES_NEEDED = 2

# Durée de blocage après perte (minutes)
BLOCK_DURATION_AFTER_LOSS = 5

# Restriction horaire (H:00 à H:29 uniquement)
ENABLE_TIME_RESTRICTION = True
```

## 🔒 Sécurité

- Le bot vérifie l'ID de l'administrateur pour les commandes sensibles
- Les prédictions sont bloquées de H:30 à H:59 (configurable)
- Une couleur est bloquée 5 minutes après une prédiction perdue

## 📝 Logs

Le bot affiche les logs en temps réel:
```
2024-01-15 14:30:15 - INFO - 📊 Jeu #751 reçu: ['♦', '♥']
2024-01-15 14:30:15 - INFO - 🎯 ♠️: 1 manque détecté
2024-01-15 14:35:22 - INFO - 📊 Jeu #756 reçu: ['♣', '♦']
2024-01-15 14:35:22 - INFO - 🎯 ♠️: 2 manques → Prédiction #761
2024-01-15 14:35:22 - INFO - ✅ Prédiction envoyée: #761 - ♠️
```

## 🐛 Dépannage

### Le bot ne démarre pas
- Vérifier que `API_ID` et `API_HASH` sont corrects
- Vérifier que `BOT_TOKEN` est valide

### Le bot ne voit pas les messages
- Vérifier que le bot est membre du canal source
- Vérifier que `SOURCE_CHANNEL_ID` est correct

### Les prédictions ne s'envoient pas
- Vérifier que le bot est admin du canal de prédiction
- Vérifier que `PREDICTION_CHANNEL_ID` est correct

## 📄 Licence

Usage privé uniquement.
