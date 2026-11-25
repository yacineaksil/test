#!/usr/bin/env python3
"""Robot de vérification des pronostics (et des résultats) de Ligue des champions.

Appelé automatiquement par `ucl_predict.py` avant l'affichage et l'écriture des
pronostics, il peut aussi être lancé seul :

    python3 ucl_verif.py --date 2026-11-25
    python3 ucl_verif.py --date 2026-11-04 --resultats resultats/ucl_2026-11-04.json

Ce qu'il contrôle :

1. Disponibilité de chaque « buteur probable » :
   - feuille de match officielle (ESPN `summary.rosters`, publiée ~1 h avant le coup
     d'envoi) : joueur absent du groupe → retiré et remplacé ; une fois la compo publiée,
     seuls les TITULAIRES sont listés comme buteurs probables (leçon du 25/11/2026) ;
     les remplaçants passent dans une ligne « Sur le banc » (probabilité x0,35) ;
   - effectif actuel de l'équipe (ESPN `teams/<id>/roster`) : joueur parti → retiré ;
     champ `injuries` de cet effectif s'il est renseigné ;
   - actualités ESPN de l'équipe (`news?team=<id>`) des derniers jours : article qui
     annonce le joueur forfait / blessé ;
   - fichier manuel `absences.json` (saisi à la main, prioritaire).
   Sans aucune information de disponibilité pour une équipe, ses buteurs sont marqués
   « ⚠️ disponibilité non vérifiée ».
2. Cohérence des données : probabilités 1/N/2, équipes et heures conformes au tableau
   ESPN, matchs pas encore commencés, pas de doublon, buteurs dans la bonne équipe ;
   pour les résultats (`ucl_bot.py`) : buts = score final, buts de 1re période = score
   à la mi-temps, buteurs dans l'effectif de leur équipe.

Bibliothèque standard uniquement.
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ucl_bot import BASE, aujourdhui_paris, date_longue, get_json, parse_utc, vers_paris

ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER_ABSENCES = os.path.join(ICI, "absences.json")

FENETRE_NEWS_JOURS = 7     # ancienneté maximale d'un article pour être pris en compte
# Un remplaçant joue ~20-30 min en moyenne. Valeur conservée après le bilan du 25/11/2026 :
# deux remplaçants buteurs (Lautaro, Malen) ne suffisent pas à la recalibrer. Les lignes
# « banc » de historique/buteurs.csv serviront à l'ajuster quand il y aura assez de données.
FACTEUR_REMPLACANT = 0.35
FACTEUR_INCERTAIN = 0.5    # joueur signalé blessé / incertain dans l'actualité

# Mots-clés (en anglais : les articles ESPN le sont) dans le titre ou le chapeau d'un article.
MOTS_FORFAIT = ["ruled out", "out of", "will miss", "to miss", "set to miss", "sidelined",
                "long-term injur", "injury blow", "out injured", "out for", "with no",
                "without", "absence", "not travel", "left out", "misses"]
MOTS_DOUTE = ["injur", "doubt", "knock", "fitness", "hamstring", "ankle", "knee", "muscle",
              "thigh", "calf", "groin", "illness", "suspended", "suspension"]
# Contexte qui annule l'alerte : retour de blessure, ou « out of contract » / rumeurs de transfert.
MOTS_EXCLUS = ["return", "back from", "back in training", "fit again", "recovered",
               "available again", "boost", "comeback from injury", "out of contract",
               "contract", "transfer", "rumor", "rumour", "eyed", "target"]


# --------------------------------------------------------------------------
# Comparaison de noms (accents, casse, prénom facultatif)
# --------------------------------------------------------------------------
def normaliser(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ø", "o").replace("Ø", "o").replace("ß", "ss").replace("ł", "l").replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def meme_joueur(a, b):
    """Vrai si les deux noms désignent vraisemblablement le même joueur.
    « Lamine Yamal » = « lamine yamal » = « Yamal » ; « Christensen » = « Andreas Christensen »."""
    na, nb = normaliser(a).split(), normaliser(b).split()
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na[-1] != nb[-1]:
        # nom composé : « Gabriel Jesus » contre « Jesus » est géré ci-dessus ;
        # ici on tolère « Fermin » / « Fermin Lopez » seulement si un seul mot de part et d'autre.
        return len(na) == 1 and na[0] in nb or len(nb) == 1 and nb[0] in na
    if len(na) == 1 or len(nb) == 1:
        return True
    # deux prénoms connus : on exige la même initiale (évite R. Lewandowski / K. Lewandowski)
    return na[0][0] == nb[0][0]


def _positions(mots, expr, prefixe=False):
    """Positions (index de mot) où l'expression normalisée `expr` commence dans la liste `mots`.
    Avec `prefixe`, chaque mot de l'expression peut être le début du mot du texte (injur → injuries)."""
    e = expr.split()
    if not e:
        return []
    egal = (lambda m, x: m.startswith(x)) if prefixe else (lambda m, x: m == x)
    return [i for i in range(len(mots) - len(e) + 1) if all(egal(mots[i + k], e[k]) for k in range(len(e)))]


def positions_nom(nom, mots):
    """Positions du joueur dans le texte : nom complet, ou nom de famille d'au moins 4 lettres."""
    n = normaliser(nom)
    pos = _positions(mots, n)
    dernier = n.split()[-1] if n else ""
    if not pos and len(dernier) >= 4:
        pos = _positions(mots, dernier)
    return pos


def mot_cle_proche(mots, pos_nom, mots_cles, distance=6):
    """Premier mot-clé situé à moins de `distance` mots d'une mention du joueur."""
    for m in mots_cles:
        for p in _positions(mots, normaliser(m), prefixe=True):
            if any(abs(p - q) <= distance for q in pos_nom):
                return m
    return None


_MOTS_CLUB = {"fc", "cf", "sc", "ac", "afc", "club", "the", "de", "fk", "sk", "as", "ssc", "cp"}


def meme_equipe(a, b):
    na, nb = normaliser(a), normaliser(b)
    if na == nb:
        return True
    ra = " ".join(w for w in na.split() if w not in _MOTS_CLUB)
    rb = " ".join(w for w in nb.split() if w not in _MOTS_CLUB)
    return bool(ra) and ra == rb or bool(ra and rb) and (ra in rb or rb in ra)


# --------------------------------------------------------------------------
# Sources de disponibilité
# --------------------------------------------------------------------------
def charger_absences(jour, chemin=FICHIER_ABSENCES):
    """Absences saisies à la main pour `jour` : {nom d'équipe: [ {joueur, raison, source} ]}."""
    if not os.path.exists(chemin):
        return {}
    with open(chemin, encoding="utf-8") as f:
        donnees = json.load(f)
    jour_d = donnees.get(jour.isoformat()) or {}
    return {k: v or [] for k, v in jour_d.items() if not k.startswith("_")}


def absences_equipe(absences, equipe_nom, equipe_id):
    for cle, lst in absences.items():
        if cle == str(equipe_id) or meme_equipe(cle, equipe_nom):
            return lst
    return None   # None = équipe non renseignée (≠ liste vide = « vérifié, aucun absent »)


def _essai(url):
    try:
        return get_json(url), None
    except RuntimeError as e:
        return None, str(e)


def effectif(team_id):
    """Effectif actuel ESPN (C1). Renvoie (joueurs, erreur)."""
    d, err = _essai(f"{BASE}/teams/{team_id}/roster")
    if d is None:
        return None, err
    joueurs = []
    for a in d.get("athletes") or []:
        joueurs.append({"id": str(a.get("id")), "nom": a.get("displayName") or a.get("fullName") or "?",
                        "statut": (a.get("status") or {}).get("type"),
                        "blessures": a.get("injuries") or []})
    return joueurs, None


def actualites(team_id):
    d, err = _essai(f"{BASE}/news?team={team_id}&limit=50")
    if d is None:
        return None, err
    return d.get("articles") or [], None


def feuille_de_match(summary):
    """{team_id: [ {id, nom, titulaire} ]} si la composition officielle est publiée, sinon {}."""
    res = {}
    for r in (summary or {}).get("rosters") or []:
        lst = r.get("roster") or []
        if not lst:
            continue
        tid = str((r.get("team") or {}).get("id"))
        res[tid] = [{"id": str((p.get("athlete") or {}).get("id")),
                     "nom": (p.get("athlete") or {}).get("displayName", "?"),
                     "titulaire": bool(p.get("starter"))} for p in lst]
    return res


def trouver(joueurs, nom, jid=None):
    if not joueurs:
        return None
    if jid:
        for j in joueurs:
            if j["id"] == str(jid):
                return j
    for j in joueurs:
        if meme_joueur(j["nom"], nom):
            return j
    return None


def articles_joueur(articles, nom, jid, jour):
    """Articles récents (≤ FENETRE_NEWS_JOURS) annonçant le joueur absent / incertain.
    Renvoie une liste de (niveau, article) avec niveau "forfait" ou "doute"."""
    res = []
    limite_haute = datetime(jour.year, jour.month, jour.day, 23, 59, tzinfo=timezone.utc)
    limite_basse = limite_haute - timedelta(days=FENETRE_NEWS_JOURS)
    for a in articles or []:
        try:
            publie = parse_utc(a.get("published") or "")
        except ValueError:
            continue
        if not (limite_basse <= publie <= limite_haute):
            continue
        mots = normaliser(f"{a.get('headline', '')} . {a.get('description', '')}").split()
        etiquettes = {str(c.get("athleteId")) for c in a.get("categories") or [] if c.get("type") == "athlete"}
        pos = positions_nom(nom, mots)
        if not pos:
            continue   # l'étiquette seule ne suffit pas : le joueur doit être nommé dans le titre/chapeau
        if mot_cle_proche(mots, pos, MOTS_EXCLUS):
            continue
        tague = jid is not None and str(jid) in etiquettes
        if mot_cle_proche(mots, pos, MOTS_FORFAIT):
            res.append(("forfait", a, tague))
        elif mot_cle_proche(mots, pos, MOTS_DOUTE):
            res.append(("doute", a, tague))
    return res


def _lien(a):
    return ((a.get("links") or {}).get("web") or {}).get("href")


# --------------------------------------------------------------------------
# Vérification des pronostics
# --------------------------------------------------------------------------
class Rapport:
    def __init__(self):
        self.controles = []     # {"controle", "niveau": ok|avertissement|erreur, "detail"}
        self.corrections = []   # phrases en français

    def ajoute(self, controle, niveau, detail=""):
        self.controles.append({"controle": controle, "niveau": niveau, "detail": detail})

    def statut(self):
        niv = {c["niveau"] for c in self.controles}
        return "ERREUR" if "erreur" in niv else ("ATTENTION" if "avertissement" in niv or self.corrections else "OK")


def _scoreboard_frais(jour):
    evts, vus = [], set()
    for j in (jour - timedelta(days=1), jour):
        sb = get_json(f"{BASE}/scoreboard?dates={j.strftime('%Y%m%d')}&limit=200")
        for ev in sb.get("events", []):
            if ev["id"] not in vus and vers_paris(parse_utc(ev["date"])).date() == jour:
                vus.add(ev["id"])
                evts.append(ev)
    return evts


def _equipes_ev(ev):
    dom = ext = None
    for c in ev["competitions"][0]["competitors"]:
        t = {"id": str(c["team"]["id"]), "nom": c["team"].get("displayName")}
        if c.get("homeAway") == "home":
            dom = t
        else:
            ext = t
    return dom, ext


def _fmt_p(x):
    return f"{100 * x:.0f} %"


def desc_buts(b):
    """« 8 buts cette saison dont 1 en C1 » (toutes compétitions) ou « 2 buts en C1 » (ancien calcul)."""
    if b.get("buts_saison") is not None:
        g, c1 = b["buts_saison"], b.get("buts_c1", 0)
        s = f"{g} but{'s' if g > 1 else ''} cette saison"
        if c1:
            s += f" dont {c1} en C1"
        return s
    g = b.get("buts_c1", 0)
    return f"{g} but{'s' if g > 1 else ''} en C1"


def _base_titulaire(b):
    return b.get("proba_si_titulaire", b["proba_buteur"])


def appliquer_compo(candidats, groupe, n=2, n_banc=2):
    """Compo officielle publiée : (titulaires retenus, meilleurs remplaçants, absents du groupe).
    Les titulaires gardent leur probabilité « si titulaire », les remplaçants x FACTEUR_REMPLACANT ;
    un candidat absent de la feuille de match est écarté. Utilisé par la vérification et le backtest."""
    tit, banc, absents = [], [], []
    for c in candidats:
        g = trouver(groupe, c["joueur"], c.get("id"))
        if g is None:
            absents.append(c)
            continue
        b = dict(c)
        if g["titulaire"]:
            b["proba_buteur"] = _base_titulaire(c)
            b["disponibilite"] = "titulaire"
            tit.append(b)
        else:
            b["proba_buteur"] = _base_titulaire(c) * FACTEUR_REMPLACANT
            b["disponibilite"] = "remplaçant"
            banc.append(b)
    tit.sort(key=lambda x: -x["proba_buteur"])
    banc.sort(key=lambda x: -x["proba_buteur"])
    return tit[:n], banc[:n_banc], absents


def verifier_pronostics(jour, pronos, chemin_absences=FICHIER_ABSENCES, verbeux=True):
    """Vérifie (et corrige sur place) la liste `pronos` produite par ucl_predict.predire.

    Chaque pronostic doit contenir `id`, `id_domicile`, `id_exterieur`, `buteurs_domicile`,
    `buteurs_exterieur` et, pour pouvoir remplacer un absent, `_candidats_domicile` /
    `_candidats_exterieur` (tous les buteurs de l'équipe classés, avec leur probabilité).
    Renvoie le dictionnaire `verification` (à mettre dans le JSON)."""
    rap = Rapport()
    absences = charger_absences(jour, chemin_absences)
    sources = {"absences_json": os.path.basename(chemin_absences) if absences else None,
               "feuilles_de_match": [], "effectifs": 0, "effectifs_avec_blessures": 0,
               "articles_consultes": 0, "erreurs_reseau": []}

    # --- Téléchargements (en parallèle) -------------------------------------------------
    equipes = []
    for p in pronos:
        equipes += [(p.get("id_domicile"), p["domicile"]), (p.get("id_exterieur"), p["exterieur"])]
    ids = sorted({str(t) for t, _ in equipes if t})
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_eff = {t: ex.submit(effectif, t) for t in ids}
        f_news = {t: ex.submit(actualites, t) for t in ids}
        f_sum = {p["id"]: ex.submit(_essai, f"{BASE}/summary?event={p['id']}") for p in pronos}
        try:
            f_sb = ex.submit(_scoreboard_frais, jour)
            tableau = f_sb.result()
        except RuntimeError as e:
            tableau = None
            sources["erreurs_reseau"].append(f"tableau des matchs : {e}")
        effectifs, news, summaries = {}, {}, {}
        for t in ids:
            effectifs[t], err = f_eff[t].result()
            if err:
                sources["erreurs_reseau"].append(f"effectif {t} : {err}")
            news[t], err = f_news[t].result()
            if err:
                sources["erreurs_reseau"].append(f"actualités {t} : {err}")
        for pid, fut in f_sum.items():
            summaries[pid], err = fut.result()
            if err:
                sources["erreurs_reseau"].append(f"summary {pid} : {err}")
    sources["effectifs"] = sum(1 for v in effectifs.values() if v)
    sources["effectifs_avec_blessures"] = sum(1 for v in effectifs.values() if v and any(j["blessures"] for j in v))
    sources["articles_consultes"] = sum(len(v) for v in news.values() if v)

    # --- 0. Buteurs dans la bonne équipe (sur le pronostic brut, avant corrections) -----
    _controle_buteurs_equipe(rap, pronos, effectifs)

    # --- 1. Disponibilité des buteurs probables ----------------------------------------
    dispo_equipes = {}
    absents_declares_ok = absents_declares_ko = 0
    for p in pronos:
        feuille = feuille_de_match(summaries.get(p["id"]))
        if feuille:
            sources["feuilles_de_match"].append(f"{p['domicile']} - {p['exterieur']}")
        for camp in ("domicile", "exterieur"):
            tid, nom_eq = str(p.get(f"id_{camp}")), p[camp]
            roster = effectifs.get(tid)
            groupe = feuille.get(tid)
            manuel = absences_equipe(absences, nom_eq, tid)
            blessures_espn = [j for j in (roster or []) if j["blessures"]]
            sources_eq = []
            if groupe:
                sources_eq.append("feuille de match officielle (ESPN)")
            if manuel is not None:
                sources_eq.append(f"absences.json ({len(manuel)} absent(s))")
            if blessures_espn:
                sources_eq.append("blessures ESPN (effectif)")
            verifiee = bool(sources_eq)
            if news.get(tid) is not None:
                sources_eq.append(f"actualités ESPN ({len(news[tid])} articles, {FENETRE_NEWS_JOURS} derniers jours analysés)")
            if roster:
                sources_eq.append(f"effectif ESPN ({len(roster)} joueurs)")

            # Absents déclarés à la main : on vérifie qu'ils existent bien dans l'effectif
            absents = []
            for a in manuel or []:
                j = trouver(roster, a.get("joueur", ""))
                if roster and j is None:
                    absents_declares_ko += 1
                    rap.ajoute(f"absences.json : {a.get('joueur')} ({nom_eq})", "avertissement",
                               "joueur introuvable dans l'effectif ESPN (orthographe ? mauvaise équipe ?)")
                else:
                    absents_declares_ok += 1
                absents.append({"joueur": j["nom"] if j else a.get("joueur"), "raison": a.get("raison"),
                                "source": a.get("source"), "origine": "absences.json"})
            for j in blessures_espn:
                absents.append({"joueur": j["nom"], "raison": "blessure (ESPN)", "source": "ESPN roster",
                                "origine": "espn_injuries"})

            def motif_absence(nom, jid):
                """(niveau, raison, source) ou None. niveau : absent | remplacant | titulaire | doute"""
                for a in absents:
                    if meme_joueur(a["joueur"], nom):
                        return ("absent", a["raison"] or "absent", a["source"])
                if roster is not None and roster and trouver(roster, nom, jid) is None:
                    return ("absent", "ne figure plus dans l'effectif ESPN (transfert ?)", "ESPN roster")
                if groupe:
                    g = trouver(groupe, nom, jid)
                    if g is None:
                        return ("absent", "absent de la feuille de match officielle", "ESPN summary.rosters")
                    if not g["titulaire"]:
                        return ("remplacant", "sur le banc au coup d'envoi", "ESPN summary.rosters")
                    return ("titulaire", "titulaire", "ESPN summary.rosters")
                arts = articles_joueur(news.get(tid), nom, jid, jour)
                fort = [a for niv, a, _ in arts if niv == "forfait"]
                if fort:
                    a = fort[0]
                    return ("absent", f"annoncé forfait/blessé par ESPN : « {a.get('headline')} » "
                                      f"({a.get('published', '')[:10]})", _lien(a))
                if arts:
                    a = arts[0][1]
                    return ("doute", f"incertain (ESPN : « {a.get('headline')} », {a.get('published', '')[:10]})",
                            _lien(a))
                return None

            cle_b, cle_c = f"buteurs_{camp}", f"_candidats_{camp}"
            candidats = p.get(cle_c) or []
            retenus, retires = [], []
            for b in p.get(cle_b) or []:
                m = motif_absence(b["joueur"], b.get("id"))
                if m and m[0] == "absent":
                    retires.append((b, m))
                    if not any(meme_joueur(a["joueur"], b["joueur"]) for a in absents):
                        absents.append({"joueur": b["joueur"], "raison": m[1], "source": m[2],
                                        "origine": "espn"})
                    continue
                retenus.append(b)
                _applique_statut(b, m, verifiee)
            # remplacement des retirés par les meilleurs candidats éligibles suivants
            deja = {normaliser(b["joueur"]) for b in retenus} | {normaliser(b["joueur"]) for b, _ in retires}
            for b, m in retires:
                remplacant = None
                for c in ([] if groupe else candidats):   # avec la compo : voir « titulaires » plus bas
                    if normaliser(c["joueur"]) in deja:
                        continue
                    mc = motif_absence(c["joueur"], c.get("id"))
                    deja.add(normaliser(c["joueur"]))
                    if mc and mc[0] == "absent":
                        continue
                    remplacant = dict(c)
                    remplacant["remplace"] = b["joueur"]
                    _applique_statut(remplacant, mc, verifiee)
                    break
                phrase = f"{b['joueur']} ({nom_eq}) retiré (absent : {m[1]})"
                if remplacant:
                    phrase += (f" → remplacé par {remplacant['joueur']} ({desc_buts(remplacant)}, "
                               f"~{_fmt_p(remplacant['proba_buteur'])})")
                    retenus.append(remplacant)
                elif not groupe:
                    phrase += " → aucun autre buteur éligible cette saison"
                if m[2]:
                    phrase += f" [source : {m[2]}]"
                rap.corrections.append(phrase)
            if groupe:
                # Compo officielle publiée : seuls les titulaires sont des « buteurs probables »
                # (leçon du 25/11/2026) ; les remplaçants passent dans la ligne « Sur le banc ».
                eligibles = []
                for c in candidats:
                    mc = motif_absence(c["joueur"], c.get("id"))
                    if not (mc and mc[0] == "absent"):
                        eligibles.append(c)
                n_liste = max(2, len(p.get(cle_b) or []))
                tit, banc, _ = appliquer_compo(eligibles, groupe, n=n_liste)
                noms_tit = {normaliser(b["joueur"]) for b in tit}
                noms_avant = {normaliser(b["joueur"]) for b in retenus}
                for b in retenus:
                    if normaliser(b["joueur"]) in noms_tit:
                        continue
                    if b.get("disponibilite") == "remplaçant":
                        rap.corrections.append(
                            f"{b['joueur']} ({nom_eq}) sur le banc au coup d'envoi : retiré des buteurs probables "
                            f"(ligne « Sur le banc », ~{_fmt_p(b['proba_buteur'])})")
                    else:
                        rap.corrections.append(f"{b['joueur']} ({nom_eq}) dépassé par un titulaire plus probable")
                for b in tit:
                    if normaliser(b["joueur"]) not in noms_avant:
                        rap.corrections.append(f"{b['joueur']} ({nom_eq}) titulaire, ajouté aux buteurs probables "
                                               f"({desc_buts(b)}, ~{_fmt_p(b['proba_buteur'])})")
                retenus = tit
                p[f"banc_{camp}"] = banc
            for b in ([] if groupe else retenus):
                if b.get("disponibilite") == "remplaçant":
                    rap.corrections.append(f"{b['joueur']} ({nom_eq}) sur le banc au coup d'envoi : "
                                           f"probabilité ramenée à ~{_fmt_p(b['proba_buteur'])}")
                elif b.get("disponibilite") == "incertain":
                    rap.corrections.append(f"{b['joueur']} ({nom_eq}) {b['note_dispo']} : "
                                           f"probabilité réduite à ~{_fmt_p(b['proba_buteur'])}")
            retenus.sort(key=lambda x: -x["proba_buteur"])
            p[cle_b] = retenus
            p[f"absents_{camp}"] = absents
            p[f"disponibilite_{camp}"] = "vérifiée" if verifiee else "non vérifiée"
            dispo_equipes[nom_eq] = {"verifiee": verifiee, "sources": sources_eq}

    non_verif = [e for e, d in dispo_equipes.items() if not d["verifiee"]]
    if dispo_equipes:
        if non_verif:
            rap.ajoute("Disponibilité des buteurs", "avertissement",
                       f"aucune information de disponibilité (compo officielle, absences.json, blessures ESPN) "
                       f"pour {len(non_verif)}/{len(dispo_equipes)} équipes : {', '.join(non_verif)} "
                       "→ buteurs marqués « disponibilité non vérifiée » (seule l'actualité ESPN a été analysée)")
        else:
            rap.ajoute("Disponibilité des buteurs", "ok", "information disponible pour toutes les équipes")

    # --- 2. Cohérence ------------------------------------------------------------------
    _controles_coherence(rap, jour, pronos, tableau, effectifs)

    # nettoyage des champs internes
    for p in pronos:
        for camp in ("domicile", "exterieur"):
            p.pop(f"_candidats_{camp}", None)

    verif = {
        "statut": rap.statut(),
        "genere": vers_paris(datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M"),
        "controles": rap.controles,
        "corrections": rap.corrections,
        "disponibilite_equipes": dispo_equipes,
        "sources": sources,
    }
    if verbeux and sources["erreurs_reseau"]:
        for e in sources["erreurs_reseau"]:
            print(f"[vérification] {e}", file=sys.stderr)
    return verif


def _applique_statut(b, motif, verifiee):
    niveau = motif[0] if motif else None
    if niveau == "titulaire":
        b["disponibilite"] = "titulaire"
        b["proba_buteur"] = _base_titulaire(b)
    elif niveau == "remplacant":
        b["disponibilite"] = "remplaçant"
        b["proba_buteur"] = _base_titulaire(b) * FACTEUR_REMPLACANT
    elif niveau == "doute":
        b["disponibilite"] = "incertain"
        b["note_dispo"] = motif[1]
        b["source_dispo"] = motif[2]
        b["proba_buteur"] = b["proba_buteur"] * FACTEUR_INCERTAIN
    elif verifiee:
        b["disponibilite"] = "vérifiée"
    else:
        b["disponibilite"] = "non vérifiée"


def _controles_coherence(rap, jour, pronos, tableau, effectifs):
    # Probabilités 1/N/2
    mauvais = []
    for p in pronos:
        s = sum(p["proba_1n2"])
        if abs(s - 1) > 0.01 or any(not 0 <= x <= 1 for x in p["proba_1n2"]):
            mauvais.append(f"{p['domicile']} - {p['exterieur']} ({100 * s:.1f} %)")
        for camp in ("domicile", "exterieur"):
            for b in p.get(f"buteurs_{camp}") or []:
                if not 0 <= b["proba_buteur"] <= 1:
                    mauvais.append(f"proba buteur {b['joueur']} = {b['proba_buteur']}")
    rap.ajoute("Probabilités 1/N/2 (somme ≈ 100 %)", "erreur" if mauvais else "ok",
               "; ".join(mauvais) if mauvais else f"{len(pronos)} matchs conformes")

    # Doublons
    vus, doublons, equipes_vues = set(), [], {}
    for p in pronos:
        cle = (normaliser(p["domicile"]), normaliser(p["exterieur"]))
        if p["id"] in vus or cle in vus:
            doublons.append(f"{p['domicile']} - {p['exterieur']}")
        vus.update({p["id"], cle})
        for e in (p["domicile"], p["exterieur"]):
            equipes_vues[e] = equipes_vues.get(e, 0) + 1
    doublons += [f"{e} joue {n} fois" for e, n in equipes_vues.items() if n > 1]
    rap.ajoute("Pas de match en double", "erreur" if doublons else "ok",
               "; ".join(doublons) if doublons else "")

    # Conformité au tableau ESPN (frais, sans cache)
    if tableau is None:
        rap.ajoute("Conformité au tableau des matchs ESPN", "erreur", "tableau ESPN injoignable")
    else:
        par_id = {ev["id"]: ev for ev in tableau}
        ecarts, deja_commences = [], []
        for p in pronos:
            ev = par_id.get(p["id"])
            if ev is None:
                ecarts.append(f"{p['domicile']} - {p['exterieur']} absent du tableau ESPN du {jour.isoformat()}")
                continue
            dom, ext = _equipes_ev(ev)
            if not (meme_equipe(dom["nom"], p["domicile"]) and meme_equipe(ext["nom"], p["exterieur"])):
                ecarts.append(f"{p['domicile']} - {p['exterieur']} ≠ ESPN {dom['nom']} - {ext['nom']}")
            if p.get("id_domicile") and (str(p["id_domicile"]), str(p["id_exterieur"])) != (dom["id"], ext["id"]):
                ecarts.append(f"identifiants d'équipes différents pour {p['domicile']} - {p['exterieur']}")
            heure = vers_paris(parse_utc(ev["date"])).strftime("%Hh%M")
            if heure != p["heure_paris"]:
                ecarts.append(f"{p['domicile']} - {p['exterieur']} : coup d'envoi {p['heure_paris']} ≠ ESPN {heure}")
            etat = ev.get("status", {}).get("type", {})
            if etat.get("state") != "pre":
                deja_commences.append(f"{p['domicile']} - {p['exterieur']} ({etat.get('description', etat.get('state'))})")
        ids_p = {p["id"] for p in pronos}
        for ev in tableau:
            if ev["id"] not in ids_p:
                ecarts.append(f"match ESPN sans pronostic : {ev.get('name')}")
        rap.ajoute("Équipes et heures conformes au tableau ESPN", "erreur" if ecarts else "ok",
                   "; ".join(ecarts) if ecarts else f"{len(tableau)} matchs, équipes et coups d'envoi identiques")
        rap.ajoute("Matchs pas encore commencés", "avertissement" if deja_commences else "ok",
                   ("déjà commencés ou terminés (pronostic a posteriori) : " + "; ".join(deja_commences))
                   if deja_commences else "tous les matchs sont à venir")


def _controle_buteurs_equipe(rap, pronos, effectifs):
    """Contrôle fait AVANT toute correction : un buteur listé dans la mauvaise équipe est une erreur
    de données (il sera ensuite retiré par la vérification de disponibilité)."""
    # Chaque buteur appartient à l'équipe sous laquelle il est listé
    ko, nb = [], 0
    for p in pronos:
        for camp, autre in (("domicile", "exterieur"), ("exterieur", "domicile")):
            r = effectifs.get(str(p.get(f"id_{camp}")))
            r_autre = effectifs.get(str(p.get(f"id_{autre}")))
            for b in p.get(f"buteurs_{camp}") or []:
                nb += 1
                if r and trouver(r, b["joueur"], b.get("id")) is None:
                    ko.append(f"{b['joueur']} absent de l'effectif de {p[camp]}")
                elif r_autre and b.get("id") and trouver(r_autre, "", b.get("id")):
                    ko.append(f"{b['joueur']} figure dans l'effectif adverse ({p[autre]})")
    rap.ajoute("Buteurs dans l'effectif de leur équipe", "erreur" if ko else "ok",
               "; ".join(ko) if ko else f"{nb} buteurs vérifiés")


# --------------------------------------------------------------------------
# Vérification des résultats (ucl_bot.py)
# --------------------------------------------------------------------------
def verifier_resultats(matchs, rapport=None, avec_effectifs=True):
    """Contrôles sur les matchs renvoyés par ucl_bot.recuperer_journee (ou lus dans son JSON)."""
    rap = rapport or Rapport()
    if not matchs:
        rap.ajoute("Résultats : matchs du jour", "ok", "aucun match")
        return rap
    joues = [m for m in matchs if m.get("etat") in ("in", "post")]
    # doublons
    vus, doublons = set(), []
    for m in matchs:
        cle = (m["domicile"]["nom"], m["exterieur"]["nom"])
        if m["id"] in vus or cle in vus:
            doublons.append(" - ".join(cle))
        vus.update({m["id"], cle})
    rap.ajoute("Résultats : pas de match en double", "erreur" if doublons else "ok", "; ".join(doublons))
    if not joues:
        rap.ajoute("Résultats : buts / score", "ok", "aucun match commencé, rien à contrôler")
        return rap

    ecarts_score, ecarts_mt, ecarts_equipe = [], [], []
    summaries = {}
    if avec_effectifs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {m["id"]: ex.submit(_essai, f"{BASE}/summary?event={m['id']}") for m in joues}
            summaries = {k: f.result()[0] for k, f in futs.items()}
    for m in joues:
        d, e = m["domicile"], m["exterieur"]
        lib = f"{d['nom']} - {e['nom']}"
        for camp in (d, e):
            n = sum(1 for b in m["buts"] if str(b["equipe_id"]) == str(camp["id"]))
            if camp["score"] is not None and n != camp["score"]:
                ecarts_score.append(f"{lib} : {n} but(s) listé(s) pour {camp['nom']} mais score {camp['score']}")
        if m.get("statut_code") not in ("STATUS_FIRST_HALF",) and d.get("mi_temps") is not None:
            for camp in (d, e):
                n1 = sum(1 for b in m["buts"] if b.get("periode") == 1 and str(b["equipe_id"]) == str(camp["id"]))
                if n1 != camp["mi_temps"]:
                    ecarts_mt.append(f"{lib} : {n1} but(s) en 1re période pour {camp['nom']} "
                                     f"mais {camp['mi_temps']} à la mi-temps")
        feuille = feuille_de_match(summaries.get(m["id"]))
        if feuille:
            for b in m["buts"]:
                tid = str(b["equipe_id"])
                # un csc est compté pour l'équipe qui en bénéficie : le buteur est adverse
                if b["type"] == "csc":
                    tid = str(e["id"]) if tid == str(d["id"]) else str(d["id"])
                if trouver(feuille.get(tid), b["buteur"]) is None:
                    ecarts_equipe.append(f"{lib} : {b['buteur']} ({b['minute']}) absent de la feuille de match "
                                         f"de l'équipe attendue")
    rap.ajoute("Résultats : buts listés = score final", "erreur" if ecarts_score else "ok",
               "; ".join(ecarts_score) if ecarts_score else f"{len(joues)} match(s) conformes")
    rap.ajoute("Résultats : buts de 1re période = score à la mi-temps", "erreur" if ecarts_mt else "ok",
               "; ".join(ecarts_mt) if ecarts_mt else f"{len(joues)} match(s) conformes")
    if avec_effectifs:
        rap.ajoute("Résultats : buteurs dans la feuille de match de leur équipe",
                   "erreur" if ecarts_equipe else "ok", "; ".join(ecarts_equipe))
    return rap


# --------------------------------------------------------------------------
# Mise en forme
# --------------------------------------------------------------------------
ICONES = {"ok": "✅", "avertissement": "⚠️", "erreur": "❌"}


def bloc_verif_texte(verif):
    lignes = [f"Vérification automatique (ucl_verif.py) — statut : {verif['statut']}"]
    lignes.append("✅ Contrôles :")
    for c in verif["controles"]:
        lignes.append(f"   {ICONES[c['niveau']]} {c['controle']}" + (f" — {c['detail']}" if c["detail"] else ""))
    if verif["corrections"]:
        lignes.append("⚠️ Corrections appliquées :")
        for c in verif["corrections"]:
            lignes.append(f"   - {c}")
    else:
        lignes.append("Aucune correction appliquée.")
    return "\n".join(lignes)


def bloc_verif_markdown(verif):
    md = ["## Contrôles", "", f"_Vérification automatique (`ucl_verif.py`) du {verif['genere']} — "
                              f"statut : **{verif['statut']}**._", "", "### ✅ Contrôles", ""]
    for c in verif["controles"]:
        md.append(f"- {ICONES[c['niveau']]} **{c['controle']}**" + (f" — {c['detail']}" if c["detail"] else ""))
    md += ["", "### ⚠️ Corrections appliquées", ""]
    if verif["corrections"]:
        md += [f"- {c}" for c in verif["corrections"]]
    else:
        md.append("_Aucune correction._")
    s = verif.get("sources") or {}
    if s:
        md += ["", "<details><summary>Sources de disponibilité consultées</summary>", "",
               f"- Feuilles de match officielles publiées : {', '.join(s['feuilles_de_match']) or 'aucune'}",
               f"- Effectifs ESPN lus : {s['effectifs']} (dont {s['effectifs_avec_blessures']} avec des blessures renseignées)",
               f"- Articles d'actualité ESPN analysés : {s['articles_consultes']}",
               f"- Fichier manuel : {s['absences_json'] or 'aucune entrée pour cette date'}", "", "</details>"]
    return "\n".join(md) + "\n"


# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="Vérifie les pronostics (et résultats) de C1 d'une date.")
    p.add_argument("--date", help="Date AAAA-MM-JJ (défaut : aujourd'hui, heure de Paris)")
    p.add_argument("--resultats", help="Fichier JSON de ucl_bot.py à contrôler (défaut : données ESPN du jour)")
    p.add_argument("--absences", default=FICHIER_ABSENCES, help="Fichier d'absences manuelles")
    args = p.parse_args(argv)
    try:
        jour = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else aujourdhui_paris()
    except ValueError:
        p.error("date invalide, attendu AAAA-MM-JJ")

    import ucl_bot
    import ucl_predict

    titre = f"Vérification Ligue des champions — {date_longue(jour)}"
    print(titre + "\n" + "=" * len(titre))
    try:
        if args.resultats:
            with open(args.resultats, encoding="utf-8") as f:
                matchs = json.load(f)["matchs"]
        else:
            matchs = ucl_bot.recuperer_journee(jour)
        rap_res = verifier_resultats(matchs)
        pronos, _ = ucl_predict.predire(jour, aujourdhui_paris(), verbeux=False)
        verif = verifier_pronostics(jour, pronos, args.absences)
    except RuntimeError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    verif["controles"] = rap_res.controles + verif["controles"]
    if any(c["niveau"] == "erreur" for c in verif["controles"]):
        verif["statut"] = "ERREUR"
    print(bloc_verif_texte(verif))
    print("\nButeurs probables après vérification :")
    for pr in pronos:
        for camp in ("domicile", "exterieur"):
            noms = ", ".join(f"{b['joueur']} (~{_fmt_p(b['proba_buteur'])}, {b['disponibilite']})"
                             for b in pr[f"buteurs_{camp}"]) or "—"
            print(f"   {pr[camp]} : {noms}")
    return 2 if verif["statut"] == "ERREUR" else 0


if __name__ == "__main__":
    sys.exit(main())
