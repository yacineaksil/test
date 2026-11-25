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

# Contrôle a posteriori : pronostics de la 4e journée avec les seules données antérieures
python3 ucl_predict.py --backtest 2026-11-03 2026-11-04
```

Pour chaque match : probabilités **1 / N / 2**, pronostic, **score exact le plus probable**,
**buts attendus** de chaque équipe, probabilité de **plus de 2,5 buts**, probabilité que
**les deux équipes marquent**, et 2 **buteurs probables** par équipe (meilleurs buteurs en C1
cette saison, avec une probabilité indicative de marquer).

Méthode, en bref :

1. tous les matchs de C1 2026/27 joués avant la date visée sont récupérés jour par jour
   (mis en cache dans `cache/`, dossier ignoré par git) ;
2. chaque équipe reçoit une note d'attaque et de défense (modèle de Poisson ajusté à la force
   des adversaires). Avec seulement 4 ou 5 matchs par équipe, ces notes sont fortement
   rapprochées de la moyenne (a priori équivalent à 3 matchs « moyens ») ; avantage du terrain
   x1,15 pour l'équipe qui reçoit, x0,87 pour le visiteur ;
3. quand ESPN publie des cotes (DraftKings), on en déduit les buts attendus implicites du marché
   et on les mélange au modèle (70 % cotes, 30 % modèle) : les cotes sont le meilleur signal
   disponible ;
4. loi de Poisson avec correction Dixon-Coles des petits scores.

Contrôle sur la 4e journée (3 et 4 novembre 2026, 18 matchs, données antérieures uniquement) :
15/18 bons pronostics 1N2 avec les cotes, 11/18 avec le modèle statistique seul. C'est un
échantillon minuscule : il ne prouve pas grand-chose.

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
| Feuille de match ESPN `summary?event=<id>` → `rosters` | Vide avant la publication des compositions (~1 h avant le coup d'envoi), puis titulaires + remplaçants. Joueur absent de la feuille → retiré ; sur le banc → « remplaçant », probabilité x0,35. |
| Actualités ESPN `news?team=<id>` | Articles en anglais des 7 derniers jours. Si le titre ou le chapeau nomme le joueur près d'un mot du type *ruled out*, *out for*, *will miss*, *sidelined*, *without*… il est retiré ; près de *injury*, *doubt*, *knock*… il est « incertain » (probabilité x0,5). Les articles de transfert / retour de blessure sont ignorés. C'est une heuristique : le titre et le lien sont recopiés dans le rapport pour contrôle humain. |

(Le champ `injuries` du `summary` n'existe pas pour les matchs de C1.)

Un buteur retiré est remplacé par le meilleur buteur suivant de l'équipe en C1 cette saison qui
n'est pas lui-même absent. Les noms sont comparés sans accents ni majuscules, et le nom de famille
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
  appliquées »**, par exemple : *Lamine Yamal (Barcelona) retiré (absent : cheville) → remplacé
  par Karim Adeyemi (2 buts en C1, ~32 %)* ; dans le détail de chaque match, une ligne
  « Absents » ;
- `.json` : clé `verification` (statut, contrôles, corrections, sources consultées) et, pour
  chaque buteur, un champ `disponibilite` (`titulaire`, `remplaçant`, `incertain`, `vérifiée`,
  `non vérifiée`) et `remplace` le cas échéant.

## Processus complet en 3 étapes

```bash
# 1. Résultats de la veille / du jour
python3 ucl_bot.py --date 2026-11-24 --out resultats/

# 2. Pronostics (la vérification de l'étape 3 est lancée automatiquement)
#    Idéalement : saisir d'abord les absences connues dans absences.json, et relancer
#    après la publication des compositions (~1 h avant le coup d'envoi).
python3 ucl_predict.py --date 2026-11-25 --out pronostics/

# 3. Vérification (seule, pour relire résultats + pronostics)
python3 ucl_verif.py --date 2026-11-25
```

Le dossier `cache/` reste ignoré par git.
