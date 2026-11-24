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
