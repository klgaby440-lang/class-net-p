# Passerelle Intranet ClassInk -> PrimeInk

## Rôle unique

Ce service Python est un relais local. Il reçoit les moyennes, présences, conduites autorisées et signatures préparées par l'application enseignante ClassInk, les met brièvement en mémoire vive, puis les remet à PrimeInk quand celui-ci les demande.

Il n'héberge aucun compte, ne fait aucun calcul scolaire, ne gère aucune classe et ne remplace pas PrimeInk.

## Stockage

- Aucune base de données.
- Aucun fichier de notes ou de présences.
- Aucune écriture sur disque par le code du relais.
- Aucun journal contenant le corps des requêtes.
- Un seul paquet en attente par couple établissement/enseignant : un nouvel envoi remplace l'ancien.
- Le paquet reste uniquement en RAM jusqu'à l'accusé de réception de PrimeInk, ou pendant 24 heures au maximum.
- Un redémarrage du relais efface tous les paquets non encore récupérés. Les données originales restent dans le stockage local de ClassInk ; l'enseignant peut relancer l'envoi.

La mémoire vive est nécessaire pour faire le pont entre l'envoi de l'enseignant et le prochain passage de PrimeInk. Elle n'est pas un stockage permanent.

## Échanges

1. ClassInk envoie POST /api/intranet/sync avec le jeton signé de l'enseignant.
2. Le relais vérifie la signature, l'expiration, l'émetteur, l'audience, le rôle, l'adresse électronique et l'établissement du jeton. Le jeton est signé avec une clé dédiée au relais, différente de la clé principale des sessions cloud. Il met le paquet métier en attente dans la RAM.
3. PrimeInk interroge GET /api/primenet/sync toutes les 10 secondes avec le jeton de l'école.
4. PrimeInk applique les notes et présences à son état local, puis sauvegarde localement.
5. Seulement après une sauvegarde locale réussie, PrimeInk envoie POST /api/primenet/ack avec les identifiants reçus. Le relais supprime alors ces paquets de sa mémoire.

Le relais confirme seulement la réception temporaire. L'accusé final est envoyé par PrimeInk après sa sauvegarde locale.

## Installation sur l'ordinateur de l'école

Python 3.11 ou plus récent est recommandé.

Installation des dépendances :
    py -m pip install -r requirements-intranet.txt

Créer une clé dédiée aléatoire (ne jamais réutiliser AUTH_SECRET) :
    py -c "import secrets; print(secrets.token_urlsafe(48))"

Configurer cette valeur sous le nom INTRANET_AUTH_SECRET dans les variables d'environnement du service ClassInk sur Render, puis redéployer ce service. Configurer exactement la même valeur sur l'ordinateur intranet :
    $env:INTRANET_AUTH_SECRET = "COLLER_ICI_LA_CLE_DEDIEE"

Les applications devront ensuite se reconnecter une fois pour recevoir les jetons intranet dédiés. Le relais refuse les anciens jetons cloud et ne connaît pas la clé qui signe les sessions cloud.

Lancer le relais :
    py intranet_server.py

Le relais écoute par défaut sur 0.0.0.0:5001. Réservez une adresse IP locale fixe à cet ordinateur, par exemple 192.168.1.200, ou configurez le nom DNS local serveur-class-net-prime.local.

Autorisez le port TCP 5001 uniquement sur le profil réseau privé du pare-feu Windows. Ne publiez pas ce port sur Internet.

## HTTPS et sécurité du réseau

Le relais accepte HTTPS lorsque les variables INTRANET_SSL_CERTFILE et INTRANET_SSL_KEYFILE pointent vers un certificat et sa clé privée. Si l'application enseignante est ouverte depuis une origine HTTPS, le navigateur peut bloquer un appel vers une passerelle HTTP : il faut alors configurer HTTPS sur le relais et installer une autorité de certification approuvée sur les appareils de l'école.

En l'absence de TLS, réserver le relais à un réseau local privé et contrôlé, protégé par le Wi-Fi de l'établissement et le pare-feu. Ne pas le rendre accessible depuis Internet.

Le jeton est vérifié hors ligne avec la clé HMAC partagée ; le relais ne consulte donc pas la base cloud et ne peut pas connaître immédiatement une révocation récente. La date d'expiration du jeton reste appliquée.

## Variables facultatives

- INTRANET_HOST : adresse d'écoute, défaut 0.0.0.0.
- INTRANET_PORT : port, défaut 5001.
- INTRANET_AUTH_SECRET : clé aléatoire dédiée, identique sur Render et sur le serveur local, mais différente de AUTH_SECRET.
- INTRANET_ALLOWED_ORIGINS : origines web autorisées, séparées par des virgules ; défaut *.
- INTRANET_BUFFER_TTL_SECONDS : durée maximale en RAM, défaut 86400 secondes.
- INTRANET_MAX_PAYLOAD_BYTES : taille maximale d'un paquet, défaut 8 MiB.
- INTRANET_MAX_TOTAL_BUFFER_BYTES : plafond global de RAM des paquets, défaut 128 MiB.

## Compilation

Le service peut être lancé directement avec Python pour les essais. La compilation en exécutable Windows (par exemple avec PyInstaller) sera faite à l'étape de compilation, après validation sur le réseau local.
