# Fixture de recette ClassInk / PrimeInk

Ce jeu de données est **entièrement synthétique** et destiné aux tests automatisés et à la recette manuelle.

Il contient :
- 6 comptes enseignants de démonstration ;
- 3 classes avec 5, 6 et 4 élèves (15 élèves au total) ;
- 6 cours répartis dans les classes ;
- 10 relations parent-élève, dont 5 avec accès aux résultats et 5 bloquées ;
- un rôle de préfet de démonstration ;
- les vérifications attendues dans CRYPT Command Center.

Le fichier ne contient aucun mot de passe, aucune clé CRYPT et aucun identifiant réel. Il ne doit jamais être injecté automatiquement dans la base de production. Pour tester les comptes, créer les données dans une base d'environnement de test puis utiliser des secrets de test séparés.
