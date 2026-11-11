# CRYPT Command Center — configuration Render

La console réutilise l'API ClassInk uniquement comme hôte de l'interface et comme point de contrôle déjà instrumenté. Les connexions Think et Llink passent par des routes internes dédiées et des secrets de service différents. Soko Market est interrogé avec un rôle PostgreSQL strictement en lecture seule.

## 1. Variables du service ClassInk / Console CRYPT

Conserver les variables déjà utilisées par ClassInk :
- `DATABASE_URL` : PostgreSQL ClassInk existant.
- `AUTH_SECRET` : secret de signature des sessions de l'API ClassInk (32 caractères minimum, aléatoire).
- `ADMIN_API_KEY` : clé d'administration historique de ClassInk, utilisée par les autres routes administratives ClassInk.

Ajouter ces variables au service Render `class-net-p` :

| Variable | Valeur / usage |
|---|---|
| `CRYPT_CONSOLE_ADMIN_KEY` | Nouvelle clé secrète réservée à l'ouverture du Command Center. Ne pas réutiliser `ADMIN_API_KEY`. Utiliser une valeur aléatoire longue (au moins 32 caractères). |
| `THINK_CONSOLE_API_URL` | `https://think-crypt-entreprise.onrender.com` |
| `THINK_CONSOLE_SERVICE_TOKEN` | Secret aléatoire dédié Think, au moins 32 caractères. Il doit être identique à `CRYPT_CONSOLE_SERVICE_TOKEN` dans le service Think. |
| `LLINK_CONSOLE_API_URL` | `https://llink-usz9.onrender.com` |
| `LLINK_CONSOLE_SERVICE_TOKEN` | Secret aléatoire dédié Llink, au moins 32 caractères. Il doit être identique à `CRYPT_CONSOLE_SERVICE_TOKEN` dans le service Llink. |
| `SOKO_DATABASE_READONLY_URL` | URL PostgreSQL d'un rôle Soko Market ayant uniquement les droits SELECT sur `users`, `products` et `sales`. |

Ne mets jamais ces secrets dans le fichier HTML, dans GitHub Pages, dans un fichier JavaScript côté navigateur, ni dans une capture d'écran.

## 2. Variables du service Think

Ajouter dans Render → service Think :

| Variable | Valeur / usage |
|---|---|
| `CRYPT_CONSOLE_SERVICE_TOKEN` | Copier exactement la valeur de `THINK_CONSOLE_SERVICE_TOKEN` du service ClassInk. |

Les variables Think déjà en place restent nécessaires et ne changent pas : `THINK_DATABASE_URL`, `THINK_DATABASE_NAME`, `JWT_SECRET`, `LLINK_SSO_SECRET`, `LLINK_SERVICE_TOKEN`, ainsi que les variables CinetPay déjà configurées pour Think.

## 3. Variables du service Llink

Ajouter dans Render → service Llink :

| Variable | Valeur / usage |
|---|---|
| `CRYPT_CONSOLE_SERVICE_TOKEN` | Copier exactement la valeur de `LLINK_CONSOLE_SERVICE_TOKEN` du service ClassInk. |

Conserver les variables existantes de Llink, notamment `FIREBASE_CONFIG` (service account Firebase Admin), `THINK_API_BASE_URL`, `THINK_SERVICE_TOKEN` et `THINK_SSO_SECRET`. La console ne reçoit jamais le contenu de `FIREBASE_CONFIG`.

## 4. Connexion Soko Market — lecture seule

Le connecteur Soko n'exige aucun nouveau jeton dans l'application Soko. Il utilise une connexion PostgreSQL dédiée depuis le service ClassInk/Console.

Dans la base de données Soko, créer un rôle technique distinct du compte applicatif et lui accorder uniquement :
- `CONNECT` sur la base Soko ;
- `USAGE` sur le schéma `public` ;
- `SELECT` sur les tables `users`, `products` et `sales`.

Ne lui accorde pas INSERT, UPDATE, DELETE, TRUNCATE, CREATE ou les privilèges de propriétaire. Génère ensuite son URL de connexion et place cette URL dans `SOKO_DATABASE_READONLY_URL` sur ClassInk. N'utilise pas la valeur principale `DATABASE_URL` de Soko comme identifiant de supervision.

## 5. Exemple de variables à ajouter

Exemple de noms uniquement — remplace chaque valeur entre chevrons dans Render. Ne copie pas cet exemple dans Git avec de vrais secrets.

```dotenv
# ClassInk / CRYPT Console
CRYPT_CONSOLE_ADMIN_KEY=<CLE_ADMIN_CRYPT_LONGUE_ET_ALEATOIRE>
THINK_CONSOLE_API_URL=https://think-crypt-entreprise.onrender.com
THINK_CONSOLE_SERVICE_TOKEN=<SECRET_THINK_CONSOLE_LONG_ET_ALEATOIRE>
LLINK_CONSOLE_API_URL=https://llink-usz9.onrender.com
LLINK_CONSOLE_SERVICE_TOKEN=<SECRET_LLINK_CONSOLE_LONG_ET_ALEATOIRE>
SOKO_DATABASE_READONLY_URL=<URL_POSTGRESQL_DU_ROLE_SOKO_SELECT_ONLY>

# Think
CRYPT_CONSOLE_SERVICE_TOKEN=<MEME_VALEUR_QUE_THINK_CONSOLE_SERVICE_TOKEN>

# Llink
CRYPT_CONSOLE_SERVICE_TOKEN=<MEME_VALEUR_QUE_LLINK_CONSOLE_SERVICE_TOKEN>
```

## 6. Ce que la console peut consulter et contrôler

- ClassInk / PrimeInk : établissements, comptes, appareils, sessions, événements et réclamations, avec les contrôles déjà disponibles.
- Think : comptes CRYPT ID, statut actif/suspendu, soldes monétaires agrégés, paiements et achats de crédits Llink.
- Llink : soldes de crédits, crédits réservés, dettes de crédits, historique récent et état du compte Firebase lié.
- Soko Market : nombre de comptes, produits, stocks faibles, quantités, ventes et chiffre d'affaires ; consultation uniquement.

La suspension Think et la désactivation Firebase Llink sont volontairement deux actions séparées. Elles sont confirmées dans l'interface et enregistrées dans l'audit ClassInk. Les opérations de portefeuille et paiements ne sont pas exposées en écriture dans cette première version.

## 7. Mise en service

1. Ajouter les variables dans les trois services Render concernés.
2. Créer l'utilisateur PostgreSQL Soko en lecture seule et renseigner son URL dans ClassInk.
3. Déployer les branches/PR correspondantes de Think et Llink pour activer leurs routes internes.
4. Déployer le backend ClassInk qui héberge la console.
5. Ouvrir le dossier `/crypt/` sur le même domaine GitHub Pages qui sert le frontend ClassInk (chemin relatif au site du dépôt), puis utiliser `CRYPT_CONSOLE_ADMIN_KEY`. L'interface est un fichier statique ; le domaine exact dépend du domaine GitHub Pages configuré pour ce dépôt.

Les statuts de service sont des contrôles de disponibilité. Il faut ensuite valider les connexions Think, Llink et Soko avec de vrais environnements Render avant d'utiliser les commandes de suspension sur des comptes réels.
