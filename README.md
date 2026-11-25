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
> certitudes. Ils reposent sur très peu de matchs et ignorent les blessures, suspensions,
> compositions d'équipe, la fatigue et les enjeux. Un favori à 60 % perd ou fait match nul
> 4 fois sur 10. Ils ne constituent en aucun cas un conseil de pari.
