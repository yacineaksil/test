# Leçons tirées des bilans

Ce fichier garde la trace de ce que chaque bilan a appris et de ce qui a été changé en
conséquence. Les sections « Bilan automatique » en fin de fichier sont ajoutées (ou remplacées)
par `ucl_bilan.py --date AAAA-MM-JJ` ; les leçons rédigées à la main restent au-dessus.

Rappel : 9 matchs par soir, c'est un échantillon minuscule. Une leçon n'est retenue que si elle
est confirmée par le backtest sur plusieurs journées, pas sur une anecdote.

---

## 25 novembre 2026 (5e journée, 2e soir)

Sources : `pronostics/pronos_2026-11-25.json` (pronostics publiés : mélange 70 % cotes / 30 %
stats, vérifiés avec les compositions officielles) contre `resultats/ucl_2026-11-25.json`.
Bilan détaillé : `bilans/bilan_2026-11-25.md`.

### Leçon A — Quand il y a des cotes, n'utiliser que les cotes

**Constat.**

| 25/11 (9 matchs) | 1N2 justes | Brier 1N2 |
|---|:-:|:-:|
| Publié (mélange 70/30) | 5/9 | 0,580 |
| Marché seul (cotes DraftKings, marge retirée) | 5/9 | **0,518** |
| Modèle statistique seul | 4/9 | 0,796 |

Le backtest (données strictement antérieures à chaque date, cotes ESPN relues après match) va
dans le même sens sur les 4 soirées disponibles :

| 36 matchs (03/11, 04/11, 24/11, 25/11) | 1N2 justes | Brier 1N2 |
|---|:-:|:-:|
| Modèle statistique seul | 20/36 | 0,607 |
| Mélange 70/30 (ancien défaut) | 26/36 | 0,479 |
| **Cotes seules (nouveau défaut)** | **27/36** | **0,442** |

Le marché est meilleur à chaque soirée prise séparément (Brier 0,403 / 0,391 / 0,457 / 0,518
contre 0,453 / 0,402 / 0,481 / 0,579 pour le mélange). Avec la règle d'origine du backtest
(données antérieures au 03/11 pour les deux soirs) : 0,397 contre 0,429.

**Changement.** `ucl_predict.py` utilise désormais par défaut les **cotes seules** (1N2 +
plus/moins → buts attendus implicites) dès qu'ESPN publie des cotes ; le modèle statistique ne
sert que quand il n'y en a pas. L'ancien mélange reste disponible avec `--melange`, le modèle
seul avec `--sans-cotes`. Le JSON garde les trois variantes (`proba_1n2_variantes`) et indique
la source utilisée (`source_probas`) pour que les bilans suivants puissent continuer à les
comparer.

### Leçon B — Le modèle statistique gonfle le nombre de buts

**Constat (25/11).** 33,1 buts prévus (publié) pour 25 marqués ; « les deux équipes marquent »
prévu en moyenne à 64 % (≈ 6/9), arrivé 4/9 ; +2,5 buts prévu 70 %, arrivé 5/9.

**Vérification (backtest, mêmes 36 matchs).**

| | Buts prévus | Buts réels | +2,5 prévu / réel | BTTS prévu / réel |
|---|:-:|:-:|:-:|:-:|
| Modèle statistique seul | 145,1 | 117 | 75 % / 61 % | 71 % / 50 % |
| Mélange 70/30 | 131,9 | 117 | 70 % / 61 % | 65 % / 50 % |
| Cotes seules | 128,5 | 117 | 68 % / 61 % | 62 % / 50 % |

Par soirée (cotes seules / mélange / réel) : 03/11 32,3 / 33,4 / 26 ; 04/11 30,9 / 31,7 / 44 ;
24/11 33,2 / 33,8 / 22 ; 25/11 32,1 / 33,0 / 25.

**Conclusion honnête.** Passer aux cotes seules réduit bien les buts prévus (−3,4 sur 36 matchs,
−0,9 sur le 25/11) et améliore légèrement le Brier BTTS (0,285 contre 0,293), mais **l'écart
reste** : le marché lui-même a prévu ~10 % de buts de trop sur ces 4 soirées (et 30 % de trop peu
le 04/11). Le 25/11 était une soirée pauvre en buts, pas seulement une erreur de modèle. Aucun
coefficient correcteur n'a été ajouté (4 soirées ne suffisent pas) ; les colonnes
`buts_att_*` de `historique/historique.csv` permettront de le revoir.

### Leçon C — Buteurs : élargir les données, penaltys, titulaires

**Constat.** Les 10 buteurs les plus probables (≈ 35 % en moyenne) ont donné 3 buteurs (Suárez
x2, Álvarez, Adeyemi) : calibration à peu près correcte. Mais beaucoup de buteurs réels n'étaient
même pas candidats : Isak x2, Gordon x2, Wirtz, Sørloth, Muriqi, Ueda… des joueurs à 0-1 but en
C1 (≈ 4 matchs de C1 seulement). 5 buts sur penalty (Suárez, Bruno Fernandes, Dembélé, Sucic,
Malen ; Suárez en a aussi manqué un) sur 24 buts hors csc (25 au total dont 1 csc). Lautaro et
Malen sont entrés en jeu et ont marqué.

**Changements.**

1. **Toutes compétitions.** Les buteurs sont calculés sur tous les matchs officiels de la saison
   (championnat, coupes nationales, supercoupes, C1, qualifications ; amicaux exclus) : ESPN
   `site.api.espn.com/apis/site/v2/sports/soccer/all/teams/<id>/schedule?season=2026` puis
   `…/soccer/<ligue>/summary?event=<id>` (keyEvents + compositions), mis en cache dans `cache/`.
   Ces données sont datées : le backtest n'utilise que les matchs antérieurs à chaque date.
   (Les points d'accès « athlete » `site.web.api.espn.com/…/athletes/<id>/stats|overview` sont
   bloqués depuis cet environnement ; `site.api.espn.com/apis/common/v3/…/athletes/<id>/overview`
   répond mais donne des totaux à date du jour, donc inutilisables en backtest.) Limites : pas de
   championnat pour Slavia Prague, Shakhtar et Sabah (seulement C1/qualifs) ; certains matchs de
   coupe n'ont pas de composition (ignorés pour les parts de buts).
2. **Tireur de penalty.** Tout joueur qui a tiré (marqué ou manqué) un penalty pour l'équipe cette
   saison est signalé « tireur de penalty » et reçoit un bonus modeste : + 0,25 x (penaltys obtenus
   par l'équipe par match) x (sa part des penaltys de l'équipe), ajouté à son taux de buts. Effet
   mesuré au backtest : quasi nul sur le Brier (0,0734 sans bonus, 0,0732 avec), mais 11 des 12
   penaltys tirés lors des 4 soirées l'ont été par un joueur signalé (6/6 le 25/11).
3. **Titulaires seulement** une fois la composition publiée (`ucl_verif.py`) : les remplaçants
   passent dans une ligne « Sur le banc » avec la probabilité x0,35. Avant la compo, la probabilité
   est pondérée par les titularisations des 5 derniers matchs.
4. **Facteur banc : inchangé (0,35).** Deux remplaçants buteurs (Lautaro, Malen) sont deux
   anecdotes, pas une preuve. Le backtest donne d'ailleurs : 253 candidats sur le banc, 11,7 buteurs
   attendus avec x0,35, **11 réels** — aucun motif de changer. (Côté titulaires : 61,5 attendus,
   73 réels, soit une légère sous-estimation à surveiller.) `historique/buteurs.csv` garde les
   lignes « banc » pour recalibrer plus tard sur un vrai échantillon.

**Résultat au backtest** (36 matchs, 2 buteurs listés par équipe, compo officielle appliquée) :

| | Listés ayant marqué | Attendu | Buts marqués par un listé | Brier (tous les joueurs entrés en jeu) | Log-loss |
|---|:-:|:-:|:-:|:-:|:-:|
| Ancien (C1 seule, remplaçants gardés x0,35) | 27/139 | 30,6 | 34/113 | 0,0791 | 0,323 |
| Nouveau (toutes compétitions, penalty, titulaires) | 32/141 | 34,8 | 42/113 | **0,0732** | **0,267** |

Le 25/11 seul : 7/35 dans les deux cas, buts couverts 9 → 10 sur 24. Le nouveau calcul aurait
listé Isak (~39 %, 2 buts), Muriqi, Ueda, Upamecano, Gabriel Jesus, Suárez et Bruno Fernandes ;
il aurait raté Adeyemi et Álvarez (peu de buts cette saison toutes compétitions) et, par
construction, Lautaro et Malen (sur le banc). Le gain est réel mais modeste : marquer reste
très aléatoire.

### Ce qui a marché : le vérificateur

Les 14 joueurs retirés (Yamal, Kane, Doué, Davies, Cunha, Mendoza, Lookman, etc.) n'ont pas joué :
14/14 confirmés par les feuilles de match. Rien à changer.

### Tickets (rappel, non stockés dans les fichiers)

Le ticket « double chance » prudent (Barça, Atlético, PSG ou nul, Inter ou nul) est passé ; le
ticket 4 favoris (PSG nul), le ticket factuel 9 matchs (5/9) et le ticket « value » du modèle ont
perdu. Une soirée ne dit rien de la valeur d'une méthode de ticket.

---

## Bilans automatiques

<!-- bilan-auto:2026-11-25 -->
### Bilan automatique du 25/11/2026 (`ucl_bilan.py`)

- 1N2 : publié 5/9 (Brier 0.580), marché seul 5/9 (Brier 0.518), stats seules 4/9 (Brier 0.796).
- Buts : 33.1 prévus (marché 32.3) pour 25 marqués ; +2,5 prévu 70 % / réel 5/9 ; BTTS prévu 64 % / réel 4/9.
- Buteurs : 7/36 listés ont marqué (attendu 7.1) ; 9/24 buts (hors csc) par un joueur listé.
- Vérificateur : 14/14 absents retirés n'ont effectivement pas joué.
- ⚠️ Le marché seul a fait mieux que les probabilités publiées (Brier).
- ⚠️ Nombre de buts surestimé de plus de 25 %.

Détail : `bilans/bilan_2026-11-25.md`.
<!-- /bilan-auto:2026-11-25 -->
