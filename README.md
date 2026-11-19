# 🎄 Radar cadeaux : bot deals Noël et Black Friday

Le bot repère les vrais gros deals (mode, beauté, maison, high-tech, jeux, jouets) et les range sur **une page web** qui se met à jour toute seule **toutes les 30 minutes**, sur les serveurs de GitHub. Ça marche même ordinateur éteint, c'est gratuit, et il n'y a rien à installer.

**Une seule page pour tout le monde, sans compte.** Tu partages le lien : chaque personne crée ses propres proches (prénom, budget, ce qui leur plaît) directement sur la page, et voit les deals qui leur correspondent. C'est enregistré dans son navigateur, personne n'a besoin de GitHub. Seul toi (le propriétaire) installes le bot, une fois.

## Ce que tu trouves sur la page

| Section | À quoi ça sert |
|---|---|
| **À ne pas rater** | Les meilleurs deals des 3 derniers jours, 2 par marque au maximum pour varier. |
| **Pour qui ?** | Une tuile par personne avec son nombre de deals. Touche-la pour ne voir que ses idées. **✎ Mes personnes** pour les créer et les modifier, **Partager** pour envoyer tes personnes et ta sélection. |
| **Ma sélection** | Les produits que tu as gardés avec « ♡ Garder », rangés par personne, avec le **total à prévoir**. Un message t'avertit si un produit n'est plus en promo, ou s'il a encore baissé. Elle est enregistrée sur ton appareil. |
| **Tous les deals** | Recherche, budget max, tri (meilleurs, plus grosse baisse, plus votés, prix, récents) et filtre par source. Le badge **Nouveau** signale ce qui est apparu depuis ta dernière visite. |
| **Produits surveillés** | Les produits dont tu suis le prix : prix du jour, plus bas prix vu, ton prix cible et la courbe. |
| **Baisses en cours** | Les produits LeDénicheur dont le prix vient de baisser. |
| **Mode d'emploi** | Le résumé en 4 étapes, en bas de la page. |

**Affichage simple par défaut** : un visiteur ne voit que l'essentiel (À ne pas rater, Pour qui ?, Ma sélection, Tous les deals avec recherche, budget et marque). Le bouton **« Afficher les options avancées »**, tout en bas de la page, montre le reste : tri, sources, regroupement par marque, Suivre le prix, Produits surveillés, Baisses en cours, Partager. Le choix est mémorisé sur chaque appareil.

En haut à droite, tu choisis l'apparence : **Automatique** (comme ton téléphone ou ton ordinateur), **Clair** ou **Sombre**.

### Créer ses personnes (pour chaque visiteur)
**✎ Mes personnes** ouvre l'éditeur. Pour chaque proche, tu indiques :
- un **prénom** et un **emoji** ;
- un **budget max** ;
- **ce qui lui plaît**, à cocher parmi 22 centres d'intérêt : parfum femme, parfum homme, maquillage, soins, cheveux et beauté tech, rasage, mode femme, mode homme, sneakers, sacs et bijoux, montres, jeux vidéo, tech et audio, maison et déco, cuisine et café, Lego et jouets, bricolage et barbecue, vins et gourmandises, sport, sorties et expériences, calendriers de l'avent, coups de cœur ;
- des **idées en plus** (mots-clés libres : « Dyson », « Stanley »…) et des mots **à éviter**.

Puis **Enregistrer**. La page recalcule aussitôt « pour qui » est chaque deal. Une personne qui découvre la page part des proches proposés (Maman, Papa, Sœurs, Frère, Copain / Copine, Maison & déco, Coup de cœur), qu'elle modifie ou supprime à sa guise. « Revenir aux personnes proposées » remet la liste d'origine.

### Partager à plusieurs, sans compte
**Partager** crée un lien qui contient tes personnes et ta sélection. Envoie-le par SMS ou WhatsApp : la personne qui l'ouvre voit un bandeau « On t'a partagé des idées » et peut tout **ajouter à sa page** en un toucher. Ses propres personnes ne sont pas touchées. Rien ne transite par un serveur : tout est dans le lien.

⚠️ Pense à la surprise : n'envoie pas un lien avec « Sœurs » à ta sœur. 😉

### Garder et comparer
Sur chaque fiche, **♡ Garder** met le produit de côté. Dans **Ma sélection**, tu vois tes idées par personne avec le total. Ça permet de comparer deux idées pour Maman, ou de vérifier que tu restes dans le budget. **Retirer** enlève un produit. La sélection est enregistrée **sur l'appareil** où tu l'as faite (téléphone et ordinateur ont chacun la leur).

### Suivre le prix d'un produit précis
Deux façons de faire :
- **Depuis la page**, c'est le plus simple. Touche **⏱ Suivre le prix** sur une fiche, ou **＋ Suivre un produit** dans « Produits surveillés » pour coller n'importe quel lien : LeDénicheur, Sklum, Nike, une boutique… Un formulaire GitHub s'ouvre déjà rempli. Indique ton **prix cible** (ou laisse vide), puis **Submit new issue**. Au passage suivant, le produit apparaît dans « Produits surveillés ».
  - Le produit passe dans « À ne pas rater » dès qu'il descend **sous ton prix cible**, ou quand son prix chute fortement.
  - Pour **modifier** le prix cible, modifie la demande sur GitHub. Pour **arrêter** le suivi, **ferme** la demande (lien « Modifier ou arrêter le suivi » sous le produit).
  - Pour un vêtement vu chez Camino ou ailleurs : ouvre le post, va jusqu'au produit sur le site du marchand, copie son lien et colle-le dans « Suivre un produit ».
- **Dans `config.yaml`**, section `surveillance`, si tu préfères tout garder dans le fichier.

Conseil : pour un produit vendu partout (Dyson, AirPods, parfum…), donne le lien de sa fiche **LeDénicheur**. Le bot suit alors le meilleur prix de tous les marchands, avec 3 mois d'historique.

## D'où viennent les deals

| Source | Ce qu'elle apporte |
|---|---|
| **Dealabs** | Les deals repérés par la communauté, avec leurs votes. Couvre aussi les enseignes qui bloquent les robots : Sephora, Nocibé, Zalando, Uniqlo, Fnac, Kave Home, Maisons du Monde… |
| **LeDénicheur** | Environ 1 500 produits populaires dans 34 catégories cadeaux. Le bot compare le prix du jour de chaque produit à son **historique de 3 mois**, tous marchands confondus. |
| **Catalogues complets** | Le bot lit le **plan du site** de chaque enseigne et parcourt **toutes** ses fiches produit à tour de rôle : **Sklum** (6 500 produits), **IKEA**, **Westwing**, **Drawer**, **Miliboo**, **Etam**, **Undiz**, **Maje**, **Lookfantastic**. Il repère les prix barrés et les baisses par rapport aux prix déjà relevés. |
| **Marques en direct** | Plus de **100 enseignes** lues directement (catalogue Shopify ou plan du site + fiches produit), testées automatiquement par l'outil « Découvrir des enseignes ». Quelques exemples. **Mode** : Sandro, ba&sh, Isabel Marant, A.P.C., AMI, Sessùn, Rouje, Make My Lemonade, Balzac, Monoprix, Nike, Puma, Sarenza, Foot Locker. **Accessoires** : Longchamp, Lancel, Le Tanneur, Agatha, Histoire d'Or, Daniel Wellington. **Beauté** : Diptyque, Nuxe, Filorga, Rituals, Le Labo, ghd, Cult Beauty, Oh My Cream. **Maison** : Sklum, IKEA, Westwing, Sarah Lavoine, Merci, Fleux, Le Creuset, KitchenAid. **Gourmand** : Ladurée, La Maison du Chocolat, Nicolas. La liste complète et le résultat de chaque test sont dans `data/enseignes.yaml`. |
| **Tes suivis** | Les produits précis dont tu suis le prix. |

Sur le prix barré du Black Friday, le bot se méfie : pour LeDénicheur et les catalogues, il compare au **prix réellement constaté** les semaines précédentes, pas seulement au prix barré affiché. Il lit chaque site en respectant ses règles (robots.txt), à raison d'une page toutes les 2 secondes par site.

Uniqlo, Kave Home, Zara, H&M, Sézane, Sephora, Zalando, Maisons du Monde, La Redoute et AM.PM bloquent la lecture automatique. Ce sont des **marques suivies** : le bot garde **tous** leurs bons plans publiés sur Dealabs, même peu votés. Il lit pour cela les flux de nouveautés et la page Dealabs de chaque marque. **Uniqlo est prioritaire** : sa page est relue à chaque passage. Sur la page, choisis **★ Marques suivies** (ou directement « Uniqlo ») dans le menu **Marque**. Pour suivre une autre marque, ajoute-la dans `dealabs → marques_suivies` du `config.yaml`.

---

## Installation (environ 10 minutes)

### 1. Créer le dépôt GitHub
1. Crée un compte gratuit sur [github.com](https://github.com) si tu n'en as pas.
2. En haut à droite, **+ → New repository**. Donne un nom discret (ex. `radar-x7`), choisis **Public**, puis **Create repository**.
3. Sur la page du dépôt : **uploading an existing file**. Glisse **tout le contenu** du dossier dézippé, y compris les dossiers `.github`, `data` et `docs`, puis **Commit changes**.
   Sur Mac, le dossier `.github` est caché : appuie sur `Cmd + Maj + .` dans le Finder pour l'afficher.

### 2. Activer la page
**Settings → Pages**, puis « Source : Deploy from a branch », branche **main**, dossier **/docs**, et **Save**.
Une à deux minutes plus tard, ta page est en ligne à l'adresse `https://<ton-pseudo>.github.io/<nom-du-depot>/`. Ajoute-la à l'écran d'accueil de ton téléphone.

### 3. Démarrer le bot
Onglet **Actions** : si GitHub le demande, clique sur **I understand my workflows, go ahead and enable them**.
Ouvre « Radar cadeaux » et clique sur **Run workflow** pour un premier passage (environ 6 minutes). Ensuite, le bot tourne tout seul toutes les 30 minutes.

C'est terminé. 🎁 Les boutons « Suivre le prix » apparaissent dès ce premier passage.

**Conseillé une fois** : **Actions → Découvrir des enseignes → Run workflow** (case « Retester toute la liste » cochée). Certaines enseignes n'étaient pas joignables depuis l'environnement où le bot a été préparé : depuis GitHub, elles le seront peut-être, et l'outil les ajoutera.

> 💡 Lance-le dès maintenant : il lui faut quelques jours pour parcourir les catalogues complets et connaître les prix habituels avant le Black Friday (27 novembre).

⚠️ La page est **publique** : elle n'apparaît pas sur Google, mais toute personne qui a l'adresse peut la voir. D'où l'intérêt d'un nom de dépôt discret, pour garder la surprise. Le bot ne tient compte que des demandes de suivi que **tu** ouvres.

---

## Personnaliser (fichier `config.yaml`)

Ouvre `config.yaml` sur GitHub, clique sur l'icône ✏️, modifie, puis **Commit changes**. C'est pris en compte au passage suivant.

- **Les idées et budgets de chacun** se règlent sur la page (« ✎ Mes personnes »), pas dans ce fichier.
- **Moins de deals** : monte `temperature_min` (ex. 300) ou `reduction_min` (ex. 40). Pour plus de deals, baisse-les.
- **Changer les personnes proposées par défaut** (celles que voit un nouveau visiteur) : section `profils`, avec leurs `interets`.
- **Ajouter un centre d'intérêt** : un bloc dans `centres_interet` (nom, emoji, mots-clés, catégories, marchands). Il apparaît aussitôt dans l'éditeur de la page.
- **Ajouter une enseigne** : ajoute une ligne dans `outils/enseignes.yaml` (ex. `{site: www.exemple.fr, nom: Exemple, interet: mode_femme}`), puis onglet **Actions → Découvrir des enseignes → Run workflow**. L'outil teste le site (Shopify, plan du site, fiches produit) et l'ajoute tout seul au bot s'il est lisible. Le résultat de chaque test est dans `data/enseignes.yaml`.
- **Ajouter une marque Shopify** : ouvre `https://<site>/products.json` dans ton navigateur. Si une liste de produits s'affiche, ajoute le site dans `marchands → shopify → boutiques`.

## Facultatif

**Promos des comptes X (CaminoTV…).** La seule façon autorisée de lire des posts X automatiquement est l'API officielle, qui est payante ([developer.x.com](https://developer.x.com)). Si un jour tu la prends, ajoute le secret `X_BEARER_TOKEN` (Settings → Secrets and variables → Actions) et le lecteur intégré s'active tout seul. Sans clé, cette source est ignorée. Solution gratuite : la cloche 🔔 sur le profil de @Caminotv dans l'appli X, et « Suivre un produit » pour les articles qui t'intéressent.

**Notifications sur le téléphone.** Le bot sait aussi envoyer des messages sur Telegram, ntfy ou Discord. Ajoute les secrets `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID`, ou `NTFY_TOPIC`, ou `DISCORD_WEBHOOK_URL`, puis passe-les en variables d'environnement dans `.github/workflows/bot-deals.yml`.

## Bon à savoir
- Pour forcer une mise à jour : onglet **Actions → Radar cadeaux → Run workflow**.
- Les catalogues complets sont parcourus à tour de rôle (60 à 100 fiches par site et par passage). Un tour complet prend de 1 à 5 jours selon la taille du site. Leur mémoire est gardée dans le cache de GitHub Actions, pas dans le dépôt.
- Si une source est en panne (site en maintenance…), le bot continue avec les autres.
- Les deals restent sur la page 7 jours après leur dernière apparition. Vérifie toujours le prix sur le site avant d'acheter.
- `data/etat.json` est la mémoire du bot (deals déjà vus, historique de prix). N'y touche pas.
- Si GitHub met la tâche en pause après une longue période sans activité, retourne dans **Actions** et clique sur **Enable workflow**.
