# Robot Ligue des champions (UCL)

Petit script Python qui récupère les résultats **détaillés** des matchs de Ligue des champions
pour une date donnée :

- score final et score à la mi-temps ;
- buteurs avec la minute (penalty et but contre son camp signalés, tête / coup franc précisés) ;
- passeurs décisifs quand ils sont disponibles ;
- cartons jaunes et rouges (second jaune signalé), penaltys manqués ou arrêtés ;
- statut du match : **À venir** (avec l'heure du coup d'envoi, heure de Paris), **En cours**
  (avec le chrono), **Mi-temps**, **Terminé**, reporté, etc.

Source des données : l'API publique et gratuite d'ESPN (aucune clé nécessaire).
Le script n'utilise que la **bibliothèque standard** de Python (≥ 3.8) : aucun `pip install`.

## Utilisation

```bash
# Matchs du jour (date du jour à Paris)
python3 ucl_bot.py

# Matchs d'une date précise
python3 ucl_bot.py --date 2026-11-04

# Même chose + écriture des rapports Markdown et JSON
python3 ucl_bot.py --date 2026-11-04 --out resultats/
```

Avec `--out resultats/`, deux fichiers sont créés :

| Fichier | Contenu |
|---|---|
| `resultats/ucl_AAAA-MM-JJ.md` | Rapport lisible : tableau récapitulatif + détail de chaque match |
| `resultats/ucl_AAAA-MM-JJ.json` | Données structurées (scores, buts, passeurs, cartons, statut, stade…) |

### Exemple de sortie console

```
✅ RB Leipzig 2 - 3 Manchester City  —  Terminé
   Mi-temps : 0 - 1
   Stade : Red Bull Arena, Leipzig  |  Coup d'envoi : 21h00 (heure de Paris)
   ⚽ RB Leipzig :
      - 49' Ridle Baku, passe décisive de Brajan Gruda
      - 61' Antonio Nusa, passe décisive de Christopher Nkunku
   ⚽ Manchester City :
      - 37' Castello Lukeba (contre son camp)
      - 58' Enzo Fernández (de la tête), passe décisive de Iliman Ndiaye
      - 78' Erling Haaland, passe décisive de Elliot Anderson
   Cartons :
      🟨 17' Christoph Baumgartner (RB Leipzig)
      ...

⏳ 21h00  Paris Saint-Germain - AS Roma  —  À venir
```

Légende : ✅ terminé, 🔴 en cours, ⏳ à venir, 🟨 carton jaune, 🟥 carton rouge.
Un but contre son camp est compté pour l'équipe qui en bénéficie (le nom affiché est celui du
joueur qui a marqué contre son camp).

## Automatisation (GitHub Actions)

Le workflow `.github/workflows/ucl.yml` :

- s'exécute automatiquement **les mardis et mercredis à 22h30 UTC**
  (23h30 à Paris en hiver, 0h30 en été) ;
- peut être lancé à la main depuis l'onglet **Actions → Résultats Ligue des champions →
  Run workflow**, avec une date facultative (`AAAA-MM-JJ`) ;
- génère `resultats/ucl_AAAA-MM-JJ.md` et `.json`, puis les **commit** dans le dépôt.

Pour que le commit fonctionne, vérifiez dans **Settings → Actions → General → Workflow
permissions** que l'option *Read and write permissions* est cochée (le workflow demande
aussi `contents: write`).

Remarque : en lancement planifié, la date utilisée est celle du jour à Paris. En été, 22h30 UTC
correspond à 0h30 le lendemain à Paris : le workflow passe donc explicitement la date UTC du jour
au script pour toujours cibler la soirée de matchs.

## Points d'attention

- L'API ESPN n'est pas officiellement documentée : ses champs peuvent évoluer.
- ESPN refuse (HTTP 403) les requêtes dont le *User-Agent* contient le mot « bot » ; le script
  envoie donc `ucl-resultats/1.0`.
- Les noms d'équipes et de joueurs sont ceux d'ESPN (en anglais, par ex. « Internazionale »).

## Pronostics (`ucl_predict.py`)

Un second script, `ucl_predict.py`, **estime** l'issue des matchs d'une date donnée
(bibliothèque standard uniquement, mêmes accès ESPN que `ucl_bot.py`).

```bash
# Pronostics du jour (date du jour à Paris)
python3 ucl_predict.py

# Date précise + écriture de pronostics/pronos_AAAA-MM-JJ.md et .json
python3 ucl_predict.py --date 2026-11-25 --out pronostics/

# Modèle statistique seul (sans les cotes)
python3 ucl_predict.py --date 2026-11-25 --sans-cotes

# Ancien calcul : mélange 70 % cotes / 30 % modèle, buteurs sur la C1 seule
python3 ucl_predict.py --date 2026-11-25 --melange --buteurs-c1

# Contrôle a posteriori : pour chaque date, uniquement les données antérieures à cette date
python3 ucl_predict.py --backtest 2026-11-03 2026-11-04 2026-11-24 2026-11-25
```

Pour chaque match : probabilités **1 / N / 2**, pronostic, **score exact le plus probable**,
**buts attendus** de chaque équipe, probabilité de **plus de 2,5 buts**, probabilité que
**les deux équipes marquent**, et 2 **buteurs probables** par équipe (avec une probabilité
indicative de marquer, la mention « tireur de penalty » le cas échéant et, une fois la compo
publiée, une ligne « Sur le banc »).

Méthode, en bref :

1. tous les matchs de C1 2026/27 joués avant la date visée sont récupérés jour par jour
   (mis en cache dans `cache/`, dossier ignoré par git) ;
2. chaque équipe reçoit une note d'attaque et de défense (modèle de Poisson ajusté à la force
   des adversaires). Avec seulement 4 ou 5 matchs par équipe, ces notes sont fortement
   rapprochées de la moyenne (a priori équivalent à 3 matchs « moyens ») ; avantage du terrain
   x1,15 pour l'équipe qui reçoit, x0,87 pour le visiteur ;
3. quand ESPN publie des cotes (DraftKings), on en déduit les buts attendus implicites du marché
   (1N2 + plus/moins, marge retirée) et on utilise **les cotes seules** ; le modèle statistique
   ne sert que s'il n'y a pas de cotes. (Jusqu'au 25/11/2026, on mélangeait 70 % cotes / 30 %
   modèle : moins bien calibré et trop de buts prévus, voir `LECONS.md` ; toujours disponible
   avec `--melange`.) Le JSON garde les trois variantes (`proba_1n2_variantes`) ;
4. loi de Poisson avec correction Dixon-Coles des petits scores ;
5. buteurs : part des buts de l'équipe marqués par le joueur dans **toutes les compétitions
   officielles** de la saison (championnat, coupes, supercoupes, C1, qualifications ; amicaux
   exclus), lue sur ESPN (`soccer/all/teams/<id>/schedule` puis `soccer/<ligue>/summary` de chaque
   match, en cache), rétrécie (buts / (buts de l'équipe quand il joue + 2), plafond 45 %) et
   appliquée aux buts attendus. Bonus modeste pour un **tireur de penalty** (+0,25 x penaltys
   obtenus par match x sa part des tirs). Avant la compo, pondération par les titularisations des
   5 derniers matchs ; après, **titulaires seulement** (remplaçants x0,35, ligne « Sur le banc »).
   Ancien calcul (C1 seule) : `--buteurs-c1`. Pas de championnat chez ESPN pour quelques clubs
   (Slavia Prague, Shakhtar, Sabah) : seuls la C1 et les qualifications comptent pour eux.

Backtest (03/11, 04/11, 24/11 et 25/11/2026, 36 matchs, pour chaque date uniquement les données
antérieures ; cotes relues sur ESPN après les matchs) :

| Variante | 1N2 justes | Brier 1N2 | Buts prévus / réels |
|---|:-:|:-:|:-:|
| Modèle statistique seul | 20/36 | 0,607 | 145,1 / 117 |
| Mélange 70/30 (ancien défaut) | 26/36 | 0,479 | 131,9 / 117 |
| **Cotes seules (défaut)** | **27/36** | **0,442** | 128,5 / 117 |

Buteurs (2 listés par équipe, compo appliquée) : 32/141 ont marqué avec le nouveau calcul contre
27/139 avec l'ancien ; 42 des 113 buts marqués par un joueur listé contre 34. C'est un
échantillon minuscule : il ne prouve pas grand-chose, d'où le bilan après chaque soirée.

> ⚠️ **Avertissement.** Ces pronostics sont des **estimations statistiques**, pas des
> certitudes. Ils reposent sur très peu de matchs. Les absences ne sont prises en compte que pour
> les buteurs probables (voir « Vérification » ci-dessous, forcément incomplète) ; les probabilités
> 1/N/2 ignorent blessures, suspensions, fatigue et enjeux (sauf ce que les cotes intègrent déjà).
> Un favori à 60 % perd ou fait match nul 4 fois sur 10. Ils ne constituent en aucun cas un
> conseil de pari.

## Vérification (`ucl_verif.py`)

Le 25 novembre 2026, le pronostic listait Lamine Yamal parmi les buteurs probables de Barcelone à
Sabah alors qu'il était forfait (cheville, resté à Barcelone). D'où un **robot de vérification**,
lancé automatiquement par `ucl_predict.py` avant l'affichage et l'écriture des fichiers
(`--sans-verif` pour le désactiver, déconseillé), et utilisable seul :

```bash
# Vérifie les résultats (ucl_bot) et les pronostics (ucl_predict) d'une date
python3 ucl_verif.py --date 2026-11-25

# Contrôle un fichier de résultats déjà écrit
python3 ucl_verif.py --date 2026-11-04 --resultats resultats/ucl_2026-11-04.json
```

Code de sortie : 0 si tout va bien ou s'il n'y a que des avertissements, 2 en cas d'erreur de
cohérence (même chose pour `ucl_predict.py`).

### 1. Disponibilité des buteurs probables

Pour chaque buteur probable, dans cet ordre :

| Source | Ce qu'elle apporte réellement |
|---|---|
| `absences.json` (dans le dépôt, saisi à la main) | Absences confirmées (blessure, non convoqué…) avec raison et lien source. **Prioritaire.** |
| Effectif ESPN `teams/<id>/roster` | Effectif actuel : un joueur qui n'y figure plus (transfert) est retiré. Le champ `injuries` existe mais est **vide pour toutes les équipes** en novembre 2026 (idem avec l'effectif du championnat, `esp.1`…), et `status` vaut toujours « Active ». |
| Feuille de match ESPN `summary?event=<id>` → `rosters` | Vide avant la publication des compositions (~1 h avant le coup d'envoi), puis titulaires + remplaçants. Joueur absent de la feuille → retiré. Une fois la compo publiée, **seuls les titulaires** restent « buteurs probables » ; les remplaçants passent dans une ligne « Sur le banc » (probabilité x0,35, facteur conservé après le bilan du 25/11 faute de données suffisantes pour le changer). |
| Actualités ESPN `news?team=<id>` | Articles en anglais des 7 derniers jours. Si le titre ou le chapeau nomme le joueur près d'un mot du type *ruled out*, *out for*, *will miss*, *sidelined*, *without*… il est retiré ; près de *injury*, *doubt*, *knock*… il est « incertain » (probabilité x0,5). Les articles de transfert / retour de blessure sont ignorés. C'est une heuristique : le titre et le lien sont recopiés dans le rapport pour contrôle humain. |

(Le champ `injuries` du `summary` n'existe pas pour les matchs de C1.)

Un buteur retiré est remplacé par le meilleur buteur suivant de l'équipe cette saison (toutes
compétitions) qui n'est pas lui-même absent — ou, si la compo est publiée, par le titulaire le
plus probable. Les noms sont comparés sans accents ni majuscules, et le nom de famille
seul suffit (« Christensen » = « Andreas Christensen »).

Si **aucune** information de disponibilité n'existe pour une équipe (pas de compo officielle, pas
d'entrée dans `absences.json`, pas de blessure ESPN), ses buteurs sont marqués
**« ⚠️ disponibilité non vérifiée »** : avant les compositions officielles, c'est le cas de la
plupart des équipes. Pour vérifier une équipe, ajoutez-la dans `absences.json` (une liste vide
signifie « vérifié, aucun absent ») :

```json
{
  "2026-11-25": {
    "Barcelona": [
      {"joueur": "Lamine Yamal", "raison": "cheville", "source": "https://…"}
    ]
  }
}
```

Le nom d'équipe est celui d'ESPN (« Barcelona », « Bayern Munich »…). Un absent déclaré qui
n'existe pas dans l'effectif ESPN déclenche un avertissement (faute de frappe ?).

### 2. Contrôles de cohérence

- probabilités 1/N/2 dont la somme vaut 100 % (± 1 point) ;
- équipes, identifiants et heure de coup d'envoi identiques au tableau ESPN (relu sans cache),
  aucun match oublié ni en double ;
- matchs pas encore commencés (sinon avertissement : pronostic a posteriori) ;
- chaque buteur appartient bien à l'effectif de l'équipe sous laquelle il est listé ;
- résultats de `ucl_bot.py` : nombre de buts par équipe = score final, buts de 1re période =
  score à la mi-temps, chaque buteur figure sur la feuille de match de son équipe (celle de
  l'adversaire pour un but contre son camp), pas de match en double.

### 3. Sortie

- console et rapport `.md` : section **« Contrôles »** (✅ / ⚠️ / ❌) et **« ⚠️ Corrections
  appliquées »**, par exemple : *Lamine Yamal (Barcelona) retiré (absent : cheville)* puis
  *Fermín López (Barcelona) titulaire, ajouté aux buteurs probables (9 buts cette saison dont 1 en
  C1, ~40 %)* ; dans le détail de chaque match, des lignes « Sur le banc » et « Absents » ;
- `.json` : clé `verification` (statut, contrôles, corrections, sources consultées) et, pour
  chaque buteur, un champ `disponibilite` (`titulaire`, `remplaçant`, `incertain`, `vérifiée`,
  `non vérifiée`) et `remplace` le cas échéant.

## Bilan (`ucl_bilan.py`)

Après les matchs, le bilan compare les pronostics publiés aux résultats réels :

```bash
python3 ucl_bilan.py --date 2026-11-25
```

- lit `pronostics/pronos_AAAA-MM-JJ.json` (sans pronostics, il s'arrête : rien à comparer) et
  `resultats/ucl_AAAA-MM-JJ.json` (lance `ucl_bot.py` si le fichier manque) ;
- affiche et écrit `bilans/bilan_AAAA-MM-JJ.md` : 1N2 justes et score de Brier des probabilités
  publiées, du **marché seul** et du modèle statistique seul ; scores exacts ; buts prévus contre
  buts réels ; calibration « plus de 2,5 buts » et « les deux équipes marquent » ; buteurs listés
  qui ont marqué, **buteurs réels non listés** (avec leurs buts de la saison avant le match),
  penaltys ; joueurs retirés par le vérificateur qui n'ont effectivement pas joué ;
- ajoute une ligne par match à `historique/historique.csv` (probabilités publiées / marché /
  stats, issue, buts prévus et réels, +2,5, BTTS, Brier, buteurs) et une ligne par joueur
  (listé, sur le banc ou buteur non prévu) à `historique/buteurs.csv`, pour que la calibration
  s'accumule de journée en journée. Relancer le bilan d'une date remplace ses lignes au lieu de
  les dupliquer (`--sans-historique` pour ne rien écrire) ;
- ajoute un court résumé automatique en fin de `LECONS.md`, le journal des leçons tirées (et des
  changements faits en conséquence).

## Processus complet en 5 étapes

```bash
# 1. Résultats de la veille / du jour
python3 ucl_bot.py --date 2026-11-24 --out resultats/

# 2. Pronostics (la vérification de l'étape 3 est lancée automatiquement)
#    Idéalement : saisir d'abord les absences connues dans absences.json, et relancer
#    après la publication des compositions (~1 h avant le coup d'envoi).
python3 ucl_predict.py --date 2026-11-25 --out pronostics/

# 3. Vérification (seule, pour relire résultats + pronostics)
python3 ucl_verif.py --date 2026-11-25

# 4. Simulation Monte-Carlo et tickets combinés (facultatif)
python3 ucl_simulation.py --date 2026-11-25 --cible 100
python3 ucl_ticket.py --date 2026-11-25 --cible 100

# 5. Bilan, après les matchs : résultats + comparaison + historique + LECONS.md
python3 ucl_bot.py --date 2026-11-25 --out resultats/
python3 ucl_bilan.py --date 2026-11-25
```

Le dossier `cache/` reste ignoré par git ; `bilans/` et `historique/` sont faits pour être
commités.

## Tickets combinés, toutes compétitions (`ticket_analyse.py`, `ticket_generer.py`)

Ces deux robots fonctionnent sur toutes les compétitions dont ESPN publie les cotes
DraftKings : C1, Ligue Europa, Ligue Conférence, Premier League, Championship, Liga,
Serie A, Bundesliga, Ligue 1, Liga Portugal, Eredivisie, Belgique, Turquie, Écosse
(liste dans `paris_espn.py` → `LIGUES`), et sur les sports américains (NFL, NCAA, NBA,
NHL, MLB, WNBA : voir la section [Sports américains](#sports-américains) plus bas).
Ils sont **100 % factuels** : la probabilité d'un pari est la cote du marché, marge du
bookmaker retirée. Aucun modèle maison (c'est la leçon des bilans, voir `LECONS.md`).

### Analyser un ticket joué

Décrire le ticket dans un fichier JSON (exemple : `tickets/ticket_2026-11-25.json`) :
compétition (code ESPN), date, heure, équipes, pari (`1X2. V1`, `V2`, `X`, `1X`, `X2`,
`+2.5`, `-2.5`…), cote. **Ne pas y mettre la mise ni les gains : le dépôt est public.**
Ils se passent en option et ne sont jamais enregistrés.

```bash
python3 ticket_analyse.py tickets/ticket_2026-11-25.json --mise 4000 --gains 109947.92 --devise DZD
```

Pour chaque sélection : statut (gagné / perdu / en cours / à venir), probabilité réelle,
valeur de ta cote face au marché, mouvement des cotes depuis l'ouverture, et le
**garde-fou** :
- match retrouvé sur ESPN, horaire conforme, pas encore commencé ;
- actualité ESPN des 7 derniers jours (blessures, suspensions) ;
- à partir d'environ 1 h avant le match : compositions officielles et **détection de la
  rotation** du favori (nombre de titulaires du dernier match de championnat) ;
- pendant le match : probabilité recalculée selon le score et le temps restant.

Puis le bilan du ticket : probabilité qu'il passe, valeur juste (à comparer à un éventuel
cash-out) et maillon faible. Les cotes d'avant-match sont gardées dans
`tickets/cotes_avant_match.json` pour les calculs en direct.

### Construire un ticket du même niveau

```bash
python3 ticket_generer.py --du 2026-11-26 --au 2026-11-30 --selections 7 --cible 27.5 \
    --exclure tickets/ticket_2026-11-25.json --comparer tickets/ticket_2026-11-25.json
```

Le robot lit tous les matchs à venir avec cotes, puis cherche (programmation dynamique
exacte) la combinaison de N sélections dont la cote totale est dans la fourchette visée et
dont la probabilité réelle est la plus haute, c'est-à-dire celle qui paie le moins de marge
au bookmaker. Options utiles : `--cote-min` / `--cote-max` (cote d'une sélection, défaut
1,20–2,40), `--avec-buts` (autorise plus/moins de buts), `--ligues`, `--top`.
Le ticket n°1 passe par le garde-fou, et chaque ticket est écrit en JSON pour être
analysé ensuite avec `ticket_analyse.py`.

**À savoir :** à cote égale, deux bons tickets ont presque la même probabilité. Chaque
sélection ajoutée rajoute la marge du bookmaker (~4 à 6 %). Un combiné de 7 sélections
rend donc en moyenne environ 73 % de la mise sur la durée, contre ~95 % pour un pari simple.

### Analyser les mi-temps (`analyse_mitemps.py`)

```bash
python3 analyse_mitemps.py tickets/ticket_2026-11-25.json --date 2026-11-26
python3 analyse_mitemps.py --ligue uefa.europa.conf --date 2026-11-26 --match Getafe
```

Découpe chaque match mi-temps par mi-temps à partir du commentaire minute par minute
d'ESPN : score à la pause, tirs, tirs cadrés, tirs dans la surface, poteaux, corners,
fautes, cartons, changements à la pause, plus quelques constats factuels (domination,
manque d'efficacité, réaction de l'entraîneur). Marche aussi pendant le match.

## Sports américains

`ticket_analyse.py` et `ticket_generer.py` couvrent aussi les sports américains, avec la même
règle : probabilité = cote DraftKings publiée par ESPN, marge retirée (ici sur 2 issues, il n'y a
pas de nul). Le football reste strictement inchangé (mêmes codes, mêmes tickets, mêmes options).

| Code | Compétition | Remarque |
|---|---|---|
| `nfl` | NFL | |
| `ncaaf` | NCAA football, division FBS | tableau ESPN lu avec `groups=80&limit=300` (sinon seulement le top 25) |
| `nba` | NBA | |
| `ncaab` | NCAA basket, division I | `groups=50&limit=400` : 55 matchs le 27/11 au lieu de 14 |
| `nhl` | NHL | |
| `mlb` | MLB | hors saison en novembre : programmé mais pas testé sur de vraies cotes |
| `wnba` | WNBA | hors saison : idem |
| `usa.1` | MLS | c'est du football : 1N2 comme avant |

`--ligues` accepte aussi des groupes : `usa` (sports US + MLS), `foot` (tout le football), `tout`.
Sans `--ligues`, le générateur prend tout le football, comme avant.

```bash
# Ce soir et demain, jusqu'à dimanche 6 h (heure de Paris), vainqueur seulement
python3 ticket_generer.py --ligues usa --du 2026-11-27 --au 2026-11-29 --jusqua 2026-11-29T06:00 \
    --selections 4 --cible 8
# Même chose avec handicaps et totaux
python3 ticket_generer.py --ligues usa --du 2026-11-27 --au 2026-11-29 --jusqua 2026-11-29T06:00 \
    --selections 4 --cible 8 --marches ml,handicap,total
# Test de règlement sur des matchs terminés (prolongation, tirs au but, remboursement, nul NFL)
python3 ticket_analyse.py tickets/test_reglement_us_2026-11-26.json --sans-actu
```

`--marches` : `ml` (vainqueur ; en football c'est le 1N2), `handicap`, `total` ; défaut `ml`.
`--avec-buts` reste valable et ajoute `total`. Les fourchettes `--cote-min` / `--cote-max`
s'appliquent à chaque sélection, quel que soit le marché. Les fichiers écrits portent le suffixe
`_usa` pour ne jamais écraser un ticket de football du même jour.

### Écrire un pari US dans un ticket JSON

Mêmes champs qu'en football (`ligue` = code ci-dessus, `date` et `heure` de Paris, équipes,
`pari`, `cote`). Le pari peut être écrit comme chez le bookmaker :

| Pari chez le bookmaker | Code interne | Règlement retenu |
|---|---|---|
| `Vainqueur 1`, `V1`, `Moneyline 1`, `Knicks` | `1` | prolongation incluse (et tirs au but en NHL) |
| `Handicap 2 (+5.5)`, `2 +5.5`, `H1 -3.5`, `Knicks -4.5` | `2 +5.5` | prolongation incluse |
| `Total plus de 224.5`, `Over 224.5`, `Moins de 46.5` | `+224.5`, `-46.5` | prolongation incluse |
| `1X2. V1`, `X`, `1X`, `12` (tout sport US) | `1 TR`, `N TR`, `1N TR`, `12 TR` | temps réglementaire seulement (60 min en NHL) |
| `Plus de 5.5 buts (temps réglementaire)` | `+5.5 TR` | temps réglementaire seulement |

- **Remboursement (push).** Handicap ou total à ligne entière qui tombe pile (Bills -3, victoire
  13-10 ; total 239 avec 129-110) : la sélection est remboursée et compte pour une cote de 1,00.
  Le bilan recalcule la cote du ticket.
- **Prolongation et tirs au but.** Le vainqueur, le handicap et le total se règlent sur le score
  final, comme chez DraftKings. En NHL, le vainqueur des tirs au but reçoit un but de plus (ESPN
  affiche 3-2 pour un 2-2 décidé aux tirs au but) : le total vaut alors 5.
- **Nul en NFL** (rare : 27-27 Giants-Commanders le 11/10/2026) : le moneyline est compté perdu
  ici ; DraftKings et beaucoup de bookmakers le remboursent. Vérifie ton règlement.
- **Ordre des équipes.** ESPN met l'équipe qui reçoit en « domicile ». Si ton bookmaker affiche
  l'ordre américain (« extérieur @ domicile »), le robot le détecte, inverse 1 et 2 et le signale.
  Un nom d'équipe dans le pari (« Knicks -4.5 ») est toujours lu tel quel.
- **Terrain neutre** (tournois NCAA) : « domicile » est purement nominal, le garde-fou le rappelle.

#### Le piège du hockey : « 1X2 » ≠ « vainqueur »

Chez les bookmakers européens, le **1X2 du hockey se joue sur 60 minutes** : le nul est une issue,
et un match gagné en prolongation ou aux tirs au but est perdu pour un « V1 ». Le **vainqueur**
(moneyline) inclut prolongation et tirs au but. Ce ne sont pas les mêmes paris : dans la nuit du
25 au 26/11/2026, Islanders-Blues (2-2 après 60 min, 3-2 aux tirs au but) a fait gagner
« vainqueur Islanders » et perdre « 1X2. V1 Islanders ». DraftKings ne cote pas le 1X2 : sa
probabilité est **estimée** (signalé comme tel) à partir du moneyline et du total du marché, loi de
Poisson ajustée puis recalée sur la fréquence NHL d'environ 23 % de matchs à égalité après
60 minutes. Pour un match équilibré, le nul « 60 min » vaut donc ~23 % : un « 1X2. V1 » à 2,10
n'a rien à voir avec un moneyline à 2,10.
Même logique, moins marquée, en NBA et NFL (~6 % de prolongations).

### Ce que vérifie le garde-fou (sports US)

Il remplace, pour les sports US, la vérification des compositions et de la rotation du football
(qui ne tourne plus que pour le football) :

- **blessés des deux équipes** (liste ESPN de la ligue, plus complète que les 5 noms du résumé) :
  absents, douteux, incertains, « au jour le jour » avec la note ESPN (« probable »,
  « incertain »…), ⭐ quand c'est un leader de l'équipe (meilleur marqueur, QB n°1…), ⚠️ si
  l'équipe sur laquelle tu paries est concernée ; les inactifs NFL « choix de l'entraîneur » sont
  ignorés ; NFL : dernière note sur le quarterback titulaire (« Rudolph will start… ») ;
- **fatigue** (NBA, NHL, WNBA) : jours de repos de chaque équipe, ⚠️ si ton équipe joue son 2e
  match en 2 soirs et pas l'adversaire ;
- **gardiens partants** (NHL) : nom et statut (confirmé / attendu), matchs joués et % d'arrêts,
  ⚠️ si c'est le n°2 (Dallas a aligné DeSmith, 6 matchs, au lieu d'Oettinger, 19 matchs, le
  27/11) ;
- **lanceurs partants** (MLB) ;
- **météo** (NFL, NCAA football, MLB ; absente en stade couvert) : température, rafales,
  risque de précipitations, ⚠️ sur un « plus de » quand il y a beaucoup de vent ou de pluie ;
- **NCAA football** : adversaire de FCS (division inférieure), gros écart (≥ 20 points), classement
  AP ;
- **mouvement du marché** ouverture → maintenant, en cote et en ligne (« handicap Denver : ouverture
  -1.5 → -4.5, le marché croit PLUS en Denver »), avec les limites ci-dessous ;
- **modèle ESPN** (« Matchup Predictor ») affiché pour information seulement, jamais utilisé
  (leçon A) : le 27/11 il donnait 51 % à Pittsburgh quand le marché, qui intégrait le changement
  de quarterback et la tempête annoncée, en donnait 32 % ;
- actualité ESPN des 7 derniers jours (mots-clés anglais : *questionable*, *ruled out*, *will
  start*…) et rappel du bon moment pour relancer (inactifs NFL ~90 min avant, gardiens NHL le
  matin, statuts NBA ~30 min avant).

**En direct.** Basket et football américain : loi normale sur l'écart et le total restants
(moyennes tirées des lignes d'avant-match, écart-type d'environ 12,5 points sur un match NBA, 13,5
en NFL, 11 en NCAA basket, 16 en NCAA football). Hockey (et baseball) : la loi de Poisson du
football sur les buts restants, une égalité après le temps réglementaire donnant un but de plus au
vainqueur. Ce sont des approximations simples ; la cote en direct de DraftKings, quand ESPN la
publie pour la même ligne, est affichée à côté (elle peut avoir quelques minutes de retard).

### Ce qui compte le plus, sport par sport

| Sport | Paris principaux | Ce qui pèse le plus |
|---|---|---|
| **NFL** | moneyline, handicap (*spread*, souvent -3 / -7 : remboursements fréquents), total | le **quarterback** titulaire, la **météo** (vent, pluie : totaux plus bas), le repos (match du jeudi, semaine de repos), la liste des inactifs publiée 90 min avant |
| **NCAA football** | handicap surtout (écarts de 20-35 points), total, moneyline (souvent très bas sur les gros favoris) | gros écarts et « garbage time » (le favori sort ses titulaires), adversaires de **FCS**, rivalités de fin novembre, motivation (qualification ou non pour un bowl), pas de liste officielle des blessés |
| **NBA** | moneyline, handicap, total (~220-235 points) | **repos des stars** et *load management*, annoncés tard (~30 min avant), **2e match en 2 soirs** (*back-to-back*), statuts « questionable » des stars |
| **NCAA basket** | handicap, total, moneyline (retiré pour les très gros favoris) | **programme énorme** (des dizaines de matchs par jour, tournois de Thanksgiving sur terrain neutre), beaucoup de matchs sans cote, pas de liste des blessés |
| **NHL** | moneyline (prolongation et tirs au but inclus), *puck line* ±1,5, total 5,5 / 6,5 ; **1X2 = 60 min** en Europe | le **gardien partant** (n°1 ou n°2, confirmé le matin), le **2e match en 2 soirs** (souvent avec le gardien n°2), ~23 % de matchs à égalité après 60 min |
| **MLB** | moneyline, *run line* ±1,5, total | les **lanceurs partants** (la cote est souvent conditionnée à leur présence), le vent au stade |

### Limites des données ESPN (constatées le 27/11/2026)

- Le tableau des matchs **retire les cotes au coup d'envoi** (et après le match) pour les sports
  US. Le résumé (`summary` → `pickcenter`) garde les cotes de clôture et ajoute des cotes « en
  direct » : l'analyse s'en sert pour les matchs en cours ou terminés.
- **NFL** : la cote d'ouverture date souvent de semaines avant le match (lignes « en avance ») :
  des écarts de 10 points sur un total (Patriots-Chargers 45,5 → 35,5) ne sont pas rares. Le
  mouvement du marché le signale.
- **Ouvertures incohérentes** : sur Knicks-Heat et Spurs-Lakers (NBA, 27/11), le handicap
  d'ouverture a le signe inverse du moneyline d'ouverture (Knicks favoris à -198 mais +5,5).
  Dans ce cas l'ouverture du handicap est ignorée.
- **NCAA basket** : moneyline « OFF » pour les très gros favoris (Houston -36,5) : seuls le
  handicap et le total sont proposés ; les matchs du samedi n'avaient pas encore de cotes le
  vendredi soir (NBA du samedi non plus).
- **Blessés** : la liste de la ligue (`…/injuries`) est la plus complète (NFL limitée à 25 entrées
  par équipe, notes « Active » comprises) ; le résumé du match n'en montre que 5 par équipe ; rien
  d'exploitable en NCAA. Les identifiants des joueurs y manquent (on les lit dans le lien de la
  fiche joueur).
- **Gardiens NHL** : partant probable dans le tableau (`probables`, statut *Confirmed* ou
  *Expected*) ; les statistiques des gardiens (`summary` → `goalies`) ne sont publiées qu'avant le
  match.
- **NCAA** : les conférences changent (North Dakota State et Sacramento State en FBS en 2026) ; la
  FCS se détecte par l'équipe (`teams/<id>` → `groups.parent.id` = 81), pas par la conférence.
- `site.web.api.espn.com` et `sports.core.api.espn.com` sont bloqués depuis cet environnement : pas
  de statistiques détaillées par joueur.
