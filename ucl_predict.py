#!/usr/bin/env python3
"""Pronostics statistiques pour les matchs de Ligue des champions d'une date donnée.

Modèle (volontairement simple) :
  1. Résultats de la saison (phase de ligue) récupérés sur l'API publique d'ESPN,
     jour par jour depuis le 1er septembre jusqu'à la veille de la date visée
     (mis en cache dans `cache/`).
  2. Notes d'attaque / de défense de chaque équipe (modèle de Poisson multiplicatif,
     ajusté à la force des adversaires), « rétrécies » vers la moyenne par un a priori
     équivalent à ~3 matchs moyens, avec avantage du terrain (x1,15 domicile / x0,87 extérieur).
  3. Si ESPN publie des cotes (DraftKings) pour le match, on en déduit les buts attendus
     implicites du marché (1N2 + plus/moins) et, PAR DÉFAUT, on n'utilise QUE ces cotes
     (leçon du 25/11/2026, voir LECONS.md : le mélange 70 % cotes / 30 % stats était moins
     bien calibré et gonflait le nombre de buts). Le modèle statistique ne sert plus que
     quand il n'y a pas de cotes ; l'ancien mélange reste accessible avec --melange.
  4. Loi de Poisson (avec correction Dixon-Coles des petits scores) → probabilités 1/N/2,
     score exact le plus probable, buts attendus, plus de 2,5 buts, les deux équipes marquent.
  5. Buteurs probables : part des buts de l'équipe marqués par le joueur dans TOUTES les
     compétitions officielles de la saison (championnat, coupes, C1, qualifications ;
     ESPN `all/teams/<id>/schedule` + `summary` de chaque match), bonus modeste pour le
     tireur de penalty, et probabilité de titularisation (5 derniers matchs) tant que la
     composition n'est pas publiée. Ancien calcul (buts en C1 seulement) : --buteurs-c1.
  6. Vérification automatique (ucl_verif.py) avant affichage / écriture : disponibilité
     des buteurs (compo officielle — seuls les titulaires sont alors retenus —, effectif,
     actualité ESPN, absences.json) et contrôles de cohérence. Désactivable avec --sans-verif.

Ce sont des ESTIMATIONS STATISTIQUES, pas des certitudes.

Bibliothèque standard uniquement. Exemples :
    python3 ucl_predict.py                               # aujourd'hui (heure de Paris)
    python3 ucl_predict.py --date 2026-11-25 --out pronostics/
    python3 ucl_predict.py --backtest 2026-11-03 2026-11-04   # 4e journée
    python3 ucl_predict.py --date 2026-11-25 --melange         # ancien mélange 70/30
"""

import argparse
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from ucl_bot import BASE, aujourdhui_paris, date_longue, get_json, parse_utc, vers_paris
from ucl_verif import (appliquer_compo, bloc_verif_markdown, bloc_verif_texte, desc_buts, feuille_de_match,
                       verifier_pronostics)

ICI = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ICI, "cache")
DEBUT_SAISON = date(2026, 9, 1)

# Paramètres du modèle
DOMICILE = 1.15         # multiplicateur des buts attendus de l'équipe qui reçoit
EXTERIEUR = 0.87        # multiplicateur des buts attendus de l'équipe qui se déplace
PRIOR_MATCHS = 3.0      # poids de l'a priori « équipe moyenne », en nombre de matchs
POIDS_COTES = 0.7       # part des cotes dans le mélange (espace log des buts attendus)
RHO_DC = -0.10          # correction Dixon-Coles (petits scores, surtout 0-0 et 1-1)
MAX_BUTS = 10
PART_TEMPS_JEU = 0.85   # un buteur ne joue pas toujours 90 minutes

# Sources des probabilités d'un match (leçon A du 25/11/2026)
#   "cotes"   : cotes seules quand elles existent, modèle statistique sinon (DÉFAUT)
#   "melange" : ancien mélange POIDS_COTES cotes / (1 - POIDS_COTES) stats (--melange)
#   "stats"   : modèle statistique seul (--sans-cotes)
MODE_DEFAUT = "cotes"

# Buteurs toutes compétitions (leçon C du 25/11/2026)
SOCCER = "https://site.api.espn.com/apis/site/v2/sports/soccer"
DEBUT_SAISON_CLUBS = date(2026, 7, 1)   # matchs officiels pris en compte à partir de cette date
LIGUES_EXCLUES = ("club.friendly", "esp.joan_gamper")   # matchs amicaux
PRIOR_BUTS_EQUIPE = 2.0  # rétrécissement de la part des buts : g / (buts de l'équipe + 2)
PART_MAX = 0.45          # plafond de la part des buts de l'équipe
BONUS_PENALTY = 0.25     # tireur : + 0,25 x (penaltys obtenus par match) x (sa part des tirs)
FACTEUR_BANC = 0.35      # même valeur que ucl_verif.FACTEUR_REMPLACANT (non recalibrée : trop peu de données)
FORME_MATCHS = 5         # titularisations comptées sur les 5 derniers matchs (avant la compo officielle)


# --------------------------------------------------------------------------
# Accès aux données (avec cache disque)
# --------------------------------------------------------------------------
def _cache_get(nom):
    chemin = os.path.join(CACHE, nom)
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    return None


def _cache_put(nom, donnees):
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, nom), "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False)


def scoreboard(jour, aujourdhui):
    """Tableau des matchs ESPN d'un jour (jour US). Mis en cache si le jour est révolu."""
    nom = f"scoreboard_{jour.strftime('%Y%m%d')}.json"
    sb = _cache_get(nom)
    if sb is not None:
        return sb
    sb = get_json(f"{BASE}/scoreboard?dates={jour.strftime('%Y%m%d')}&limit=200")
    evts = sb.get("events", [])
    fini = all(e.get("status", {}).get("type", {}).get("state") == "post" for e in evts)
    if jour < aujourdhui - timedelta(days=1) and fini:
        _cache_put(nom, {"events": evts})   # on ne garde que l'utile
    return sb


def summary(event_id, termine):
    nom = f"summary_{event_id}.json"
    s = _cache_get(nom)
    if s is not None:
        return s
    s = get_json(f"{BASE}/summary?event={event_id}")
    utile = {k: s.get(k) for k in ("keyEvents", "pickcenter", "header")}
    if termine:
        _cache_put(nom, utile)
    return utile


def _etat(ev):
    return ev.get("status", {}).get("type", {}).get("state")


def _equipes(ev):
    dom = ext = None
    for c in ev["competitions"][0]["competitors"]:
        t = c["team"]
        info = {"id": t["id"], "nom": t.get("displayName") or t.get("name"),
                "score": c.get("score"), "forme": c.get("form")}
        if c.get("homeAway") == "home":
            dom = info
        else:
            ext = info
    return dom, ext


def matchs_joues(avant, aujourdhui):
    """Tous les matchs terminés de la saison dont le coup d'envoi (heure de Paris) est < `avant`."""
    resultats, vus = [], set()
    j = DEBUT_SAISON
    while j <= avant:  # le jour US `avant` peut contenir des matchs du jour J (filtrés ensuite)
        for ev in scoreboard(j, aujourdhui).get("events", []):
            if ev["id"] in vus or _etat(ev) != "post":
                continue
            ko = vers_paris(parse_utc(ev["date"])).date()
            if ko >= avant:
                continue
            vus.add(ev["id"])
            dom, ext = _equipes(ev)
            try:
                bd, be = int(dom["score"]), int(ext["score"])
            except (TypeError, ValueError):
                continue
            resultats.append({"id": ev["id"], "date": ko.isoformat(), "dom": dom, "ext": ext,
                              "bd": bd, "be": be})
        j += timedelta(days=1)
    return resultats


def matchs_du_jour(jour, aujourdhui):
    evts, vus = [], set()
    for j in (jour - timedelta(days=1), jour):
        for ev in scoreboard(j, aujourdhui).get("events", []):
            if ev["id"] in vus:
                continue
            vus.add(ev["id"])
            if vers_paris(parse_utc(ev["date"])).date() == jour:
                evts.append(ev)
    evts.sort(key=lambda e: (e["date"], e["competitions"][0]["competitors"][0]["team"]["displayName"]))
    return evts


# --------------------------------------------------------------------------
# Notes d'attaque / défense (Poisson multiplicatif, rétréci vers la moyenne)
# --------------------------------------------------------------------------
def notes_equipes(matchs):
    if not matchs:
        return 1.35, {}
    total_buts = sum(m["bd"] + m["be"] for m in matchs)
    mu = total_buts / (2 * len(matchs)) / ((DOMICILE + EXTERIEUR) / 2)
    k = PRIOR_MATCHS * mu   # a priori Gamma(k, k) ≈ 3 matchs « moyens »
    equipes = {}
    for m in matchs:
        for t in (m["dom"], m["ext"]):
            equipes.setdefault(t["id"], {"nom": t["nom"], "att": 1.0, "def": 1.0, "mj": 0,
                                         "bp": 0, "bc": 0, "v": 0, "n": 0, "d": 0})
    for m in matchs:
        d, e = equipes[m["dom"]["id"]], equipes[m["ext"]["id"]]
        d["mj"] += 1; e["mj"] += 1
        d["bp"] += m["bd"]; d["bc"] += m["be"]; e["bp"] += m["be"]; e["bc"] += m["bd"]
        if m["bd"] > m["be"]:
            d["v"] += 1; e["d"] += 1
        elif m["bd"] < m["be"]:
            e["v"] += 1; d["d"] += 1
        else:
            d["n"] += 1; e["n"] += 1
    for _ in range(30):  # itérations (ajustement à la force des adversaires)
        acc = {i: [0.0, 0.0, 0.0, 0.0] for i in equipes}  # buts marqués, attendus att., encaissés, attendus déf.
        for m in matchs:
            i, j = m["dom"]["id"], m["ext"]["id"]
            acc[i][0] += m["bd"]; acc[i][1] += mu * DOMICILE * equipes[j]["def"]
            acc[j][0] += m["be"]; acc[j][1] += mu * EXTERIEUR * equipes[i]["def"]
            acc[i][2] += m["be"]; acc[i][3] += mu * EXTERIEUR * equipes[j]["att"]
            acc[j][2] += m["bd"]; acc[j][3] += mu * DOMICILE * equipes[i]["att"]
        for i, a in acc.items():
            equipes[i]["att"] = (a[0] + k) / (a[1] + k)
            equipes[i]["def"] = (a[2] + k) / (a[3] + k)
    return mu, equipes


# --------------------------------------------------------------------------
# Poisson / Dixon-Coles
# --------------------------------------------------------------------------
def _poisson(lmb, n):
    return [math.exp(-lmb) * lmb ** k / math.factorial(k) for k in range(n + 1)]


def matrice_scores(lh, la, rho=RHO_DC):
    ph, pa = _poisson(lh, MAX_BUTS), _poisson(la, MAX_BUTS)
    m = [[ph[i] * pa[j] for j in range(MAX_BUTS + 1)] for i in range(MAX_BUTS + 1)]
    if rho:
        m[0][0] *= 1 - lh * la * rho
        m[0][1] *= 1 + lh * rho
        m[1][0] *= 1 + la * rho
        m[1][1] *= 1 - rho
    s = sum(map(sum, m))
    return [[x / s for x in ligne] for ligne in m]


def stats_matrice(m):
    p1 = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i > j)
    pn = sum(m[i][i] for i in range(len(m)))
    p2 = 1 - p1 - pn
    over = {}
    for ligne in (1.5, 2.5, 3.5, 4.5):
        over[ligne] = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i + j > ligne)
    btts = sum(m[i][j] for i in range(1, len(m)) for j in range(1, len(m)))
    score = max(((i, j) for i in range(len(m)) for j in range(len(m))), key=lambda s: m[s[0]][s[1]])
    return {"p1": p1, "pn": pn, "p2": p2, "over": over, "btts": btts,
            "score": score, "p_score": m[score[0]][score[1]]}


# --------------------------------------------------------------------------
# Cotes (DraftKings via ESPN) → buts attendus implicites
# --------------------------------------------------------------------------
def _decimale(americaine):
    try:
        x = float(str(americaine).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if x == 0:
        return None
    return 1 + x / 100 if x > 0 else 1 + 100 / -x


def cotes_marche(summ):
    """Probabilités implicites (sans marge) : 1/N/2 et plus/moins de la ligne de buts."""
    for pc in (summ or {}).get("pickcenter") or []:
        ml = pc.get("moneyline") or {}
        def cote(cle_top, camp):
            v = (pc.get(cle_top) or {}).get("moneyLine")
            if v is None:
                v = ((ml.get(camp) or {}).get("close") or {}).get("odds")
            return _decimale(v)
        c1, cn, c2 = cote("homeTeamOdds", "home"), cote("drawOdds", "draw"), cote("awayTeamOdds", "away")
        if not (c1 and cn and c2):
            continue
        inv = [1 / c1, 1 / cn, 1 / c2]
        s = sum(inv)
        res = {"bookmaker": (pc.get("provider") or {}).get("name", "?"),
               "cotes": [round(c1, 2), round(cn, 2), round(c2, 2)],
               "p1": inv[0] / s, "pn": inv[1] / s, "p2": inv[2] / s, "ligne": None, "p_over": None}
        co, cu, ligne = _decimale(pc.get("overOdds")), _decimale(pc.get("underOdds")), pc.get("overUnder")
        if co and cu and ligne:
            res["ligne"] = float(ligne)
            res["p_over"] = (1 / co) / (1 / co + 1 / cu)
        return res
    return None


def lambdas_marche(mk):
    """Cherche (λdom, λext) dont la loi de Poisson reproduit au mieux les probabilités du marché."""
    def erreur(lh, la):
        m = matrice_scores(lh, la)
        st = stats_matrice(m)
        e = (st["p1"] - mk["p1"]) ** 2 + (st["pn"] - mk["pn"]) ** 2 + (st["p2"] - mk["p2"]) ** 2
        if mk["p_over"] is not None:
            ligne = mk["ligne"]
            pov = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i + j > ligne)
            e += (pov - mk["p_over"]) ** 2
        return e
    best = (1.4, 1.1)
    pas, centre_h, centre_a, rayon = 0.2, 2.0, 2.0, 1.9
    for _ in range(4):  # recherche en grille de plus en plus fine
        candidats = []
        nh = int(rayon / pas)
        for a in range(-nh, nh + 1):
            for b in range(-nh, nh + 1):
                lh, la = centre_h + a * pas, centre_a + b * pas
                if 0.05 <= lh <= 6 and 0.05 <= la <= 6:
                    candidats.append((erreur(lh, la), lh, la))
        _, centre_h, centre_a = min(candidats)
        best = (centre_h, centre_a)
        rayon, pas = pas * 2, pas / 4
    return best


# --------------------------------------------------------------------------
# Buteurs
# --------------------------------------------------------------------------
def buteurs_saison(matchs):
    """({team_id: {joueur: buts}}, nb de matchs sans détail, {joueur: id ESPN}) à partir des
    keyEvents des matchs joués (hors csc et t.a.b.)."""
    par_equipe, manquants, ids = {}, 0, {}
    for m in matchs:
        s = summary(m["id"], termine=True)
        evs = s.get("keyEvents") or []
        if not evs and (m["bd"] + m["be"]) > 0:
            manquants += 1
        for ev in evs:
            typ = (ev.get("type", {}).get("text") or "").lower()
            if ev.get("shootout") or not ev.get("scoringPlay"):
                continue
            if typ.startswith("own goal") or ev.get("ownGoal"):
                continue
            tid = (ev.get("team") or {}).get("id")
            parts = ev.get("participants") or []
            if not tid or not parts:
                continue
            nom = parts[0].get("athlete", {}).get("displayName", "?")
            if parts[0].get("athlete", {}).get("id"):
                ids[nom] = str(parts[0]["athlete"]["id"])
            par_equipe.setdefault(tid, {})
            par_equipe[tid][nom] = par_equipe[tid].get(nom, 0) + 1
    return par_equipe, manquants, ids


def buteurs_probables(tid, lam, buteurs, buts_equipe, n=2, ids=None):
    """Les `n` meilleurs buteurs de l'équipe en C1 (tous si n=None), avec une probabilité indicative."""
    joueurs = sorted((buteurs.get(tid) or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    res = []
    for nom, g in joueurs:
        # part des buts de l'équipe, rétrécie (petits échantillons), plafonnée à 45 %
        part = min(0.45, g / (buts_equipe + 2))
        p = 1 - math.exp(-lam * part * PART_TEMPS_JEU)
        res.append({"joueur": nom, "id": (ids or {}).get(nom), "buts_c1": g, "proba_buteur": p})
    return res


# --------------------------------------------------------------------------
# Buteurs : toutes compétitions de la saison (championnat, coupes, C1…)
# --------------------------------------------------------------------------
def _essai_json(url):
    try:
        return get_json(url)
    except RuntimeError:
        return None


def calendrier_equipe(tid, aujourdhui):
    """Matchs officiels de l'équipe cette saison (toutes compétitions), via
    `soccer/all/teams/<id>/schedule`. Liste de {id, ligue, date (UTC), etat}.
    Cache disque d'une journée (le calendrier évolue)."""
    nom = f"calendrier_{tid}_{aujourdhui.strftime('%Y%m%d')}.json"
    c = _cache_get(nom)
    if c is not None:
        return c
    evts = []
    # l'URL sans saison renvoie parfois une liste vide : on essaie les deux
    for url in (f"{SOCCER}/all/teams/{tid}/schedule?season={DEBUT_SAISON_CLUBS.year}",
                f"{SOCCER}/all/teams/{tid}/schedule"):
        d = _essai_json(url)
        evts = (d or {}).get("events") or []
        if evts:
            break
    res = []
    for e in evts:
        ligue = (e.get("league") or {}).get("slug") or ""
        if not ligue or ligue in LIGUES_EXCLUES or "friendly" in ligue:
            continue
        comp = (e.get("competitions") or [{}])[0]
        res.append({"id": e["id"], "ligue": ligue, "date": e.get("date"),
                    "etat": ((comp.get("status") or {}).get("type") or {}).get("state")})
    if res:
        _cache_put(nom, res)
    return res


def match_condense(ligue, event_id):
    """Résumé compact d'un match terminé (toutes compétitions) : buts (penalty ou non),
    penaltys manqués, composition (titulaires / entrés en jeu). Mis en cache."""
    nom = f"match_{ligue}_{event_id}.json"
    c = _cache_get(nom)
    if c is not None:
        return c
    s = _essai_json(f"{SOCCER}/{ligue}/summary?event={event_id}")
    if s is None:
        return None
    buts, penos = [], []
    for ev in s.get("keyEvents") or []:
        if ev.get("shootout"):
            continue
        typ = (ev.get("type", {}).get("text") or "").lower()
        tid = str((ev.get("team") or {}).get("id"))
        parts = ev.get("participants") or []
        if not parts:
            continue
        ath = parts[0].get("athlete") or {}
        j = {"tid": tid, "jid": str(ath.get("id")), "nom": ath.get("displayName", "?")}
        if typ.startswith("penalty - missed") or typ.startswith("penalty - saved"):
            penos.append(dict(j, reussi=False))
        elif ev.get("scoringPlay") and not typ.startswith("own goal") and not ev.get("ownGoal"):
            peno = typ.startswith("penalty - scored")
            buts.append(dict(j, penalty=peno))
            if peno:
                penos.append(dict(j, reussi=True))
    compo = {}
    for r in s.get("rosters") or []:
        lst = r.get("roster") or []
        if lst:
            compo[str((r.get("team") or {}).get("id"))] = [
                {"jid": str((p.get("athlete") or {}).get("id")),
                 "nom": (p.get("athlete") or {}).get("displayName", "?"),
                 "titulaire": bool(p.get("starter")), "entre": bool(p.get("subbedIn"))} for p in lst]
    hc = ((s.get("header") or {}).get("competitions") or [{}])[0]
    etat = ((hc.get("status") or {}).get("type") or {}).get("state")
    scores = {}
    for t in hc.get("competitors") or []:
        try:
            scores[str(t.get("id") or (t.get("team") or {}).get("id"))] = int(t.get("score"))
        except (TypeError, ValueError):
            pass
    res = {"id": str(event_id), "ligue": ligue, "date": hc.get("date"), "etat": etat,
           "scores": scores, "buts": buts, "penaltys": penos, "compo": compo,
           "evenements": bool(s.get("keyEvents"))}
    if etat == "post":
        _cache_put(nom, res)
    return res


def stats_joueurs_saison(team_ids, avant, aujourdhui):
    """Statistiques des joueurs de chaque équipe, toutes compétitions officielles,
    matchs dont le coup d'envoi (heure de Paris) est strictement antérieur à `avant`.
    Renvoie {tid: {"matchs", "matchs_compo", "buts", "penaltys", "joueurs": {jid: {...}}}}."""
    cal = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {t: ex.submit(calendrier_equipe, t, aujourdhui) for t in team_ids}
        for t, f in futs.items():
            cal[t] = f.result()
    a_lire = {}
    for t, evs in cal.items():
        for e in evs:
            try:
                ko = vers_paris(parse_utc(e["date"])).date()
            except (TypeError, ValueError, AttributeError):
                continue
            if e["etat"] == "post" and DEBUT_SAISON_CLUBS <= ko < avant:
                a_lire[(e["ligue"], e["id"])] = ko
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {k: ex.submit(match_condense, *k) for k in a_lire}
        matchs = {k: f.result() for k, f in futs.items()}
    res = {}
    for t, evs in cal.items():
        eq = {"matchs": 0, "matchs_compo": 0, "buts": 0, "penaltys": 0, "joueurs": {},
              "ligues": {}}
        lst = []
        for e in evs:
            m = matchs.get((e["ligue"], e["id"]))
            if m and t in m["scores"]:
                lst.append((a_lire[(e["ligue"], e["id"])], m))
        lst.sort(key=lambda x: x[0])
        recents = set([m["id"] for _, m in lst if m["compo"].get(t)][-FORME_MATCHS:])
        for ko, m in lst:
            eq["matchs"] += 1
            eq["ligues"][m["ligue"]] = eq["ligues"].get(m["ligue"], 0) + 1
            buts_eq = m["scores"][t]
            eq["buts"] += buts_eq
            for p in m["penaltys"]:
                if p["tid"] == t:
                    eq["penaltys"] += 1
                    j = eq["joueurs"].setdefault(p["jid"], _joueur_vide(p["nom"]))
                    j["penaltys"] += 1
                    j["dernier_penalty"] = ko.isoformat()
            compo = m["compo"].get(t)
            if not compo:
                continue   # sans composition, on ne sait pas qui a joué : match ignoré pour les parts
            eq["matchs_compo"] += 1
            for c in compo:
                if not (c["titulaire"] or c["entre"]):
                    continue
                j = eq["joueurs"].setdefault(c["jid"], _joueur_vide(c["nom"]))
                j["nom"] = c["nom"]
                j["matchs"] += 1
                j["titularisations"] += c["titulaire"]
                j["buts_equipe"] += buts_eq
                if m["id"] in recents:
                    j["recents_tit"] += c["titulaire"]
                    j["recents_entre"] += c["entre"]
            for b in m["buts"]:
                if b["tid"] == t:
                    j = eq["joueurs"].setdefault(b["jid"], _joueur_vide(b["nom"]))
                    j["buts"] += 1
                    j["buts_penalty"] += b["penalty"]
                    if m["ligue"] == "uefa.champions":
                        j["buts_c1"] += 1
        eq["recents"] = len(recents)
        res[t] = eq
    return res


def _joueur_vide(nom):
    return {"nom": nom, "matchs": 0, "titularisations": 0, "buts": 0, "buts_penalty": 0, "buts_c1": 0,
            "buts_equipe": 0, "penaltys": 0, "dernier_penalty": None, "recents_tit": 0, "recents_entre": 0}


def candidats_buteurs(eq, lam):
    """Tous les buteurs de la saison d'une équipe (toutes compétitions), classés.
    proba_si_titulaire : probabilité de marquer s'il est titulaire ;
    proba_buteur : avant la compo officielle, pondérée par ses titularisations récentes."""
    if not eq or not eq["matchs"]:
        return []
    taux_peno = eq["penaltys"] / eq["matchs"]
    res = []
    for jid, j in eq["joueurs"].items():
        if j["buts"] == 0 and j["penaltys"] == 0:
            continue
        part = min(PART_MAX, j["buts"] / (j["buts_equipe"] + PRIOR_BUTS_EQUIPE)) if j["matchs"] else 0.0
        bonus = 0.0
        if j["penaltys"] and eq["penaltys"]:
            bonus = BONUS_PENALTY * taux_peno * j["penaltys"] / eq["penaltys"]
        p_tit = 1 - math.exp(-(lam * part * PART_TEMPS_JEU + bonus))
        n = eq.get("recents") or 0
        if n:
            titu = j["recents_tit"] / n
            entre = j["recents_entre"] / n
        else:
            titu, entre = 1.0, 0.0
        p_pre = p_tit * (titu + FACTEUR_BANC * entre)
        res.append({"joueur": j["nom"], "id": jid, "buts_c1": j["buts_c1"], "buts_saison": j["buts"],
                    "buts_penalty": j["buts_penalty"], "matchs_saison": j["matchs"],
                    "tireur_penalty": bool(j["penaltys"]), "penaltys_tires": j["penaltys"],
                    "titularisations_recentes": f"{j['recents_tit']}/{n}" if n else None,
                    "proba_si_titulaire": p_tit, "proba_buteur": p_pre})
    res.sort(key=lambda x: (-x["proba_buteur"], -x["buts_saison"], x["joueur"]))
    return res


# --------------------------------------------------------------------------
# Prédiction
# --------------------------------------------------------------------------
def _proba3(st):
    return [round(st["p1"], 3), round(st["pn"], 3), round(st["p2"], 3)]


def predire(jour, aujourdhui, mode=MODE_DEFAUT, donnees_avant=None, verbeux=True,
            buteurs_mode="saison", avec_cotes=None):
    """Pronostics des matchs de `jour`.
    mode : "cotes" (défaut : cotes seules si disponibles, sinon stats), "melange" (70/30), "stats".
    buteurs_mode : "saison" (toutes compétitions, défaut) ou "c1" (ancien calcul, C1 seulement)."""
    if avec_cotes is False:   # compatibilité avec l'ancienne signature
        mode = "stats"
    avant = donnees_avant or jour
    historique = matchs_joues(avant, aujourdhui)
    mu, notes = notes_equipes(historique)
    buteurs, manquants, ids_joueurs = buteurs_saison(historique)
    evts = matchs_du_jour(jour, aujourdhui)
    stats_saison = {}
    if buteurs_mode == "saison" and evts:
        ids_eq = sorted({t["id"] for ev in evts for t in _equipes(ev)})
        stats_saison = stats_joueurs_saison(ids_eq, avant, aujourdhui)
        if not any(e["matchs"] for e in stats_saison.values()):
            print("[avertissement] données toutes compétitions indisponibles : buteurs calculés sur la C1 seule.",
                  file=sys.stderr)
            buteurs_mode = "c1"
    if verbeux:
        print(f"Données : {len(historique)} matchs de C1 joués avant le {avant.strftime('%d/%m/%Y')}"
              f" ({len(notes)} équipes), moyenne {mu * (DOMICILE + EXTERIEUR) / 2:.2f} buts/équipe/match.",
              file=sys.stderr)
        if stats_saison:
            n = sum(e["matchs"] for e in stats_saison.values())
            print(f"Buteurs : {n} matchs officiels toutes compétitions lus pour {len(stats_saison)} équipes.",
                  file=sys.stderr)
        if manquants:
            print(f"[avertissement] {manquants} match(s) sans détail des buteurs.", file=sys.stderr)

    defaut = {"att": 1.0, "def": 1.0, "mj": 0, "bp": 0, "bc": 0, "v": 0, "n": 0, "d": 0}
    pronos = []
    for ev in evts:
        dom, ext = _equipes(ev)
        nd, ne = notes.get(dom["id"], dict(defaut, nom=dom["nom"])), notes.get(ext["id"], dict(defaut, nom=ext["nom"]))
        lh_stat = mu * DOMICILE * nd["att"] * ne["def"]
        la_stat = mu * EXTERIEUR * ne["att"] * nd["def"]
        mk = None
        if mode != "stats":
            try:
                mk = cotes_marche(summary(ev["id"], termine=_etat(ev) == "post"))
            except RuntimeError as e:
                print(f"[avertissement] cotes indisponibles ({ev['id']}) : {e}", file=sys.stderr)
        st_stat = stats_matrice(matrice_scores(lh_stat, la_stat))
        variantes = {"stats": _proba3(st_stat)}
        lh_mk = la_mk = None
        if mk:
            lh_mk, la_mk = lambdas_marche(mk)
            lh_mel = math.exp(POIDS_COTES * math.log(lh_mk) + (1 - POIDS_COTES) * math.log(lh_stat))
            la_mel = math.exp(POIDS_COTES * math.log(la_mk) + (1 - POIDS_COTES) * math.log(la_stat))
            variantes["melange"] = _proba3(stats_matrice(matrice_scores(lh_mel, la_mel)))
            variantes["cotes"] = _proba3(stats_matrice(matrice_scores(lh_mk, la_mk)))
            if mode == "melange":
                lh, la, source = lh_mel, la_mel, "melange"
            else:
                lh, la, source = lh_mk, la_mk, "cotes"
        else:
            lh, la, source = lh_stat, la_stat, "stats"
        st = stats_matrice(matrice_scores(lh, la))
        choix = max((("1", st["p1"]), ("N", st["pn"]), ("2", st["p2"])), key=lambda x: x[1])[0]
        ko = vers_paris(parse_utc(ev["date"]))
        reel = None
        if _etat(ev) == "post":
            try:
                bd, be = int(dom["score"]), int(ext["score"])
                reel = {"score": f"{bd}-{be}", "1n2": "1" if bd > be else ("2" if bd < be else "N")}
            except (TypeError, ValueError):
                pass
        if buteurs_mode == "saison":
            cand_d = candidats_buteurs(stats_saison.get(dom["id"]), lh)
            cand_e = candidats_buteurs(stats_saison.get(ext["id"]), la)
        else:
            cand_d = buteurs_probables(dom["id"], lh, buteurs, nd["bp"], n=None, ids=ids_joueurs)
            cand_e = buteurs_probables(ext["id"], la, buteurs, ne["bp"], n=None, ids=ids_joueurs)
        pronos.append({
            "id": ev["id"], "heure_paris": ko.strftime("%Hh%M"),
            "domicile": dom["nom"], "exterieur": ext["nom"],
            "id_domicile": dom["id"], "id_exterieur": ext["id"],
            "bilan_domicile": _bilan(nd), "bilan_exterieur": _bilan(ne),
            "forme_domicile": dom.get("forme"), "forme_exterieur": ext.get("forme"),
            "notes": {"att_dom": round(nd["att"], 2), "def_dom": round(nd["def"], 2),
                      "att_ext": round(ne["att"], 2), "def_ext": round(ne["def"], 2)},
            "source_probas": source,
            "buts_attendus_stats": [round(lh_stat, 2), round(la_stat, 2)],
            "buts_attendus_cotes": [round(lh_mk, 2), round(la_mk, 2)] if mk else None,
            "cotes": mk and {"bookmaker": mk["bookmaker"], "cotes_1n2": mk["cotes"],
                             "proba_1n2": [round(mk["p1"], 3), round(mk["pn"], 3), round(mk["p2"], 3)],
                             "ligne_buts": mk["ligne"],
                             "proba_plus": round(mk["p_over"], 3) if mk["p_over"] is not None else None},
            "proba_1n2_variantes": variantes,
            "buts_attendus": [round(lh, 2), round(la, 2)],
            "proba_1n2": _proba3(st),
            "pronostic_1n2": choix,
            "score_probable": f"{st['score'][0]}-{st['score'][1]}", "proba_score": round(st["p_score"], 3),
            "plus_2_5": round(st["over"][2.5], 3), "deux_equipes_marquent": round(st["btts"], 3),
            "buteurs_mode": buteurs_mode,
            "buteurs_domicile": [dict(c) for c in cand_d[:2]],
            "buteurs_exterieur": [dict(c) for c in cand_e[:2]],
            # liste complète (interne) pour remplacer un buteur absent ; retirée par ucl_verif
            "_candidats_domicile": cand_d,
            "_candidats_exterieur": cand_e,
            "resultat_reel": reel,
        })
    return pronos, {"matchs_historique": len(historique), "equipes": len(notes), "mu": mu,
                    "mode": mode, "buteurs_mode": buteurs_mode,
                    "matchs_toutes_competitions": sum(e["matchs"] for e in stats_saison.values())}


def _bilan(n):
    return {"mj": n["mj"], "v": n["v"], "n": n["n"], "d": n["d"], "bp": n["bp"], "bc": n["bc"]}


# --------------------------------------------------------------------------
# Mise en forme (français)
# --------------------------------------------------------------------------
def pct(x):
    return f"{100 * x:.0f} %"


def _forme_fr(f):
    return (f or "").translate(str.maketrans("WDL", "VND")) or "?"


def _txt_bilan(b):
    if not b["mj"]:
        return "aucun match de C1 cette saison"
    return f"{b['mj']} m. : {b['v']}V {b['n']}N {b['d']}D, {b['bp']}-{b['bc']}"


TAGS_DISPO = {"titulaire": "titulaire ✅", "remplaçant": "remplaçant ⚠️", "incertain": "incertain ⚠️"}


def _txt_buteurs(lst, dispo=None):
    if not lst:
        return "pas de données (aucun buteur connu cette saison)"
    morceaux = []
    for b in lst:
        s = f"{b['joueur']} ({desc_buts(b)}, ~{pct(b['proba_buteur'])}"
        if b.get("tireur_penalty"):
            s += ", tireur de penalty"
        if b.get("disponibilite") in TAGS_DISPO:
            s += f", {TAGS_DISPO[b['disponibilite']]}"
        if b.get("remplace"):
            s += f", remplace {b['remplace']}"
        morceaux.append(s + ")")
    s = ", ".join(morceaux)
    if dispo == "non vérifiée":
        s += " — ⚠️ disponibilité non vérifiée"
    return s


def _txt_absents(lst):
    return ", ".join(f"{a['joueur']} ({a['raison']})" if a.get("raison") else a["joueur"] for a in lst)


def _de(nom):
    return ("d'" if nom[:1].upper() in "AEIOUYÉÈÂÎ" else "de ") + nom


LIB_1N2 = {"1": "victoire {d}", "N": "match nul", "2": "victoire {e}"}
LIB_SOURCE = {"cotes": "cotes seules",
              "melange": f"mélange {POIDS_COTES:.0%} cotes / {1 - POIDS_COTES:.0%} stats".replace("%", " %"),
              "stats": "modèle statistique seul"}

AVERTISSEMENT = ("⚠️ Ces pronostics sont des **estimations statistiques** (loi de Poisson, notes "
                 "d'équipes sur peu de matchs, cotes de bookmaker quand elles existent). Ce ne sont "
                 "**pas des certitudes** : le football reste très aléatoire ; les absences ne sont prises en "
                 "compte que pour les buteurs (vérification automatique, forcément incomplète) et les enjeux "
                 "pas du tout. Ne pariez pas sur cette base.")


def rapport_texte(jour, pronos, meta, verif=None):
    titre = f"Pronostics Ligue des champions — {date_longue(jour)}"
    out = [titre, "=" * len(titre),
           f"Basé sur {meta['matchs_historique']} matchs de C1 2026/27 joués avant ce jour"
           " + cotes DraftKings (via ESPN) quand disponibles.",
           AVERTISSEMENT.replace("**", "")]
    if verif:
        out += ["", bloc_verif_texte(verif)]
    if not pronos:
        out.append("\nAucun match de Ligue des champions ce jour-là.")
        return "\n".join(out)
    for p in pronos:
        d, e = p["domicile"], p["exterieur"]
        p1, pn, p2 = p["proba_1n2"]
        out += ["", f"⚽ {p['heure_paris']}  {d} - {e}",
                f"   Bilan C1 : {d} {_txt_bilan(p['bilan_domicile'])} | {e} {_txt_bilan(p['bilan_exterieur'])}",
                f"   Forme (5 derniers, ESPN) : {d} {_forme_fr(p['forme_domicile'])} | {e} {_forme_fr(p['forme_exterieur'])}",
                f"   Probabilités : 1 = {pct(p1)}  N = {pct(pn)}  2 = {pct(p2)}"
                f"  →  pronostic : {p['pronostic_1n2']} ({LIB_1N2[p['pronostic_1n2']].format(d=_de(d), e=_de(e))})",
                f"   Score le plus probable : {p['score_probable']} ({pct(p['proba_score'])})"
                f"  |  Buts attendus : {p['buts_attendus'][0]:.2f} - {p['buts_attendus'][1]:.2f}",
                f"   Plus de 2,5 buts : {pct(p['plus_2_5'])}  |  Les deux équipes marquent : {pct(p['deux_equipes_marquent'])}"]
        if p["cotes"]:
            c = p["cotes"]
            out.append(f"   Cotes {c['bookmaker']} : {c['cotes_1n2'][0]} / {c['cotes_1n2'][1]} / {c['cotes_1n2'][2]}"
                       f"  (probabilités : {LIB_SOURCE[p.get('source_probas', 'melange')]} ; modèle stat. seul : "
                       f"{p['buts_attendus_stats'][0]:.2f} - {p['buts_attendus_stats'][1]:.2f} buts attendus)")
        else:
            out.append("   Cotes : non disponibles (modèle statistique seul)")
        for camp, nom in (("domicile", d), ("exterieur", e)):
            out.append(f"   Buteurs probables {nom} : {_txt_buteurs(p[f'buteurs_{camp}'], p.get(f'disponibilite_{camp}'))}")
            if p.get(f"banc_{camp}"):
                out.append(f"   Sur le banc {nom} : {_txt_buteurs(p[f'banc_{camp}'])}")
            if p.get(f"absents_{camp}"):
                out.append(f"   Absents {nom} : {_txt_absents(p[f'absents_{camp}'])}")
        if p["resultat_reel"]:
            ok = "✅" if p["resultat_reel"]["1n2"] == p["pronostic_1n2"] else "❌"
            out.append(f"   Résultat réel : {p['resultat_reel']['score']} {ok}")
    return "\n".join(out)


def rapport_markdown(jour, pronos, meta, verif=None):
    genere = vers_paris(datetime.now(timezone.utc)).strftime("%d/%m/%Y à %Hh%M")
    md = [f"# Pronostics Ligue des champions — {date_longue(jour)}", "",
          f"_Généré le {genere} (heure de Paris). Données : ESPN ({meta['matchs_historique']} matchs de C1 "
          "2026/27 joués avant ce jour) + cotes DraftKings quand disponibles._", "",
          f"> {AVERTISSEMENT}", ""]
    if verif:
        md += [bloc_verif_markdown(verif)]
    if not pronos:
        md.append("Aucun match de Ligue des champions ce jour-là.")
        return "\n".join(md) + "\n"
    md += ["## Résumé", "",
           "| Heure | Match | 1 | N | 2 | Prono | Score probable | Buts attendus | +2,5 | BTTS |",
           "|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|"]
    for p in pronos:
        p1, pn, p2 = p["proba_1n2"]
        md.append(f"| {p['heure_paris']} | {p['domicile']} - {p['exterieur']} | {pct(p1)} | {pct(pn)} | {pct(p2)} "
                  f"| **{p['pronostic_1n2']}** | {p['score_probable']} | {p['buts_attendus'][0]:.2f} - "
                  f"{p['buts_attendus'][1]:.2f} | {pct(p['plus_2_5'])} | {pct(p['deux_equipes_marquent'])} |")
    md += ["", "_BTTS : les deux équipes marquent._", "", "## Détail des matchs"]
    for p in pronos:
        d, e = p["domicile"], p["exterieur"]
        md += ["", f"### {d} - {e} ({p['heure_paris']})", "",
               f"- **Pronostic :** {p['pronostic_1n2']} — {LIB_1N2[p['pronostic_1n2']].format(d=_de(d), e=_de(e))}",
               f"- **Score le plus probable :** {p['score_probable']} ({pct(p['proba_score'])})",
               f"- **Bilan C1 :** {d} {_txt_bilan(p['bilan_domicile'])} ; {e} {_txt_bilan(p['bilan_exterieur'])}",
               f"- **Forme (5 derniers matchs, ESPN) :** {d} {_forme_fr(p['forme_domicile'])} ; "
               f"{e} {_forme_fr(p['forme_exterieur'])}"]
        if p["cotes"]:
            c = p["cotes"]
            md.append(f"- **Cotes {c['bookmaker']} :** {c['cotes_1n2'][0]} / {c['cotes_1n2'][1]} / {c['cotes_1n2'][2]} "
                      f"(buts attendus implicites {p['buts_attendus_cotes'][0]:.2f} - {p['buts_attendus_cotes'][1]:.2f} ; "
                      f"modèle stat. seul {p['buts_attendus_stats'][0]:.2f} - {p['buts_attendus_stats'][1]:.2f} ; "
                      f"probabilités publiées : {LIB_SOURCE[p.get('source_probas', 'melange')]})")
        else:
            md.append("- **Cotes :** non disponibles (modèle statistique seul)")
        for camp, nom in (("domicile", d), ("exterieur", e)):
            md.append(f"- **Buteurs probables {nom} :** {_txt_buteurs(p[f'buteurs_{camp}'], p.get(f'disponibilite_{camp}'))}")
            if p.get(f"banc_{camp}"):
                md.append(f"- **Sur le banc {nom} :** {_txt_buteurs(p[f'banc_{camp}'])}")
            if p.get(f"absents_{camp}"):
                md.append(f"- **Absents {nom} :** " + ", ".join(
                    (f"{a['joueur']} ({a['raison']}" + (f", [source]({a['source']})" if str(a.get('source', '')).startswith('http') else "") + ")")
                    for a in p[f"absents_{camp}"]))
        if p["resultat_reel"]:
            md.append(f"- **Résultat réel :** {p['resultat_reel']['score']}")
    md += ["", "## Méthode", "",
           "- Notes d'attaque et de défense calculées sur les matchs de C1 de la saison (ajustées à la force "
           f"des adversaires), rapprochées de la moyenne par un a priori équivalent à {PRIOR_MATCHS:.0f} matchs.",
           f"- Avantage du terrain : x{DOMICILE} pour l'équipe qui reçoit, x{EXTERIEUR} pour le visiteur "
           "(modèle statistique, utilisé seulement sans cotes).",
           ("- Quand ESPN publie des cotes, les probabilités et les buts attendus sont **ceux des cotes seules** "
            "(1N2 + plus/moins, marge retirée) : au bilan du 25/11/2026 et au backtest, le marché seul était mieux "
            "calibré que le mélange 70/30 (voir `LECONS.md`). Le modèle statistique ne sert que sans cotes."
            if meta.get("mode", MODE_DEFAUT) == "cotes" else
            f"- Probabilités : {LIB_SOURCE[meta.get('mode', 'melange')]} (option choisie à la main)."),
           "- Loi de Poisson avec correction Dixon-Coles pour les petits scores.",
           ("- Buteurs : part des buts de l'équipe marqués par le joueur dans **toutes les compétitions officielles** "
            f"de la saison (championnat, coupes, C1 ; rétrécie : buts / (buts de l'équipe quand il joue + "
            f"{PRIOR_BUTS_EQUIPE:.0f}), plafonnée à {PART_MAX:.0%}), appliquée aux buts attendus de l'équipe ; "
            f"bonus modeste pour le tireur de penalty (+{BONUS_PENALTY} x penaltys obtenus par match) ; avant la "
            f"compo officielle, pondération par les titularisations des {FORME_MATCHS} derniers matchs, "
            "ensuite **titulaires seulement**.".replace("%", " %")
            if meta.get("buteurs_mode", "saison") == "saison" else
            "- Buteurs : part des buts de l'équipe en C1 cette saison (rétrécie), appliquée aux buts attendus."),
           "- Vérification (`ucl_verif.py`) : un buteur absent (compo officielle, effectif ESPN, actualité ESPN, "
           "`absences.json`) est retiré et remplacé par le meilleur buteur suivant de l'équipe ; une fois la compo "
           f"publiée, seuls les titulaires sont listés (remplaçants : probabilité x{FACTEUR_BANC}, ligne « Sur le "
           "banc »). Les probabilités 1/N/2 ne sont pas recalculées (les cotes intègrent en général déjà les "
           "absences connues)."]
    return "\n".join(md) + "\n"


# --------------------------------------------------------------------------
def _lambdas_variantes(p):
    """(λdom, λext) de chaque variante pour un pronostic : stats, mélange 70/30, cotes seules."""
    ls = tuple(p["buts_attendus_stats"])
    lc = tuple(p["buts_attendus_cotes"]) if p.get("buts_attendus_cotes") else None
    if lc is None:
        return {"stats": ls, "melange": ls, "cotes": ls}
    lm = tuple(math.exp(POIDS_COTES * math.log(c) + (1 - POIDS_COTES) * math.log(x)) for c, x in zip(lc, ls))
    return {"stats": ls, "melange": lm, "cotes": lc}


def _liste_ancienne(cands, groupe, n=2):
    """Ancien comportement (buts en C1, vérification d'avant le 25/11) : les n meilleurs buteurs,
    un absent de la feuille est remplacé par le suivant, un remplaçant garde sa place avec p x 0,35."""
    res = []
    for c in cands:
        if len(res) >= n:
            break
        if groupe:
            g = None
            for x in groupe:
                if x["jid"] == str(c.get("id")):
                    g = x
            if g is None:
                continue
            b = dict(c)
            if not g["titulaire"]:
                b["proba_buteur"] *= FACTEUR_BANC
            res.append(b)
        else:
            res.append(dict(c))
    return res


def _groupe_verif(compo):
    """compo condensée → format attendu par ucl_verif (id, nom, titulaire)."""
    return [{"id": c["jid"], "nom": c["nom"], "titulaire": c["titulaire"]} for c in compo or []]


def evaluer_jours(jours, aujourdhui, meme_coupure=False, verbeux=True):
    """Calcule, pour chaque match terminé des `jours`, les prévisions des différentes variantes
    (données strictement antérieures à chaque date, ou à la 1re date si meme_coupure) et le réel."""
    lignes = []
    for j in jours:
        avant = min(jours) if meme_coupure else j
        pr_new, _ = predire(j, aujourdhui, mode="cotes", donnees_avant=avant, verbeux=False, buteurs_mode="saison")
        pr_old, _ = predire(j, aujourdhui, mode="melange", donnees_avant=avant, verbeux=False, buteurs_mode="c1")
        anciens = {p["id"]: p for p in pr_old}
        for p in pr_new:
            if not p["resultat_reel"]:
                continue
            m = match_condense("uefa.champions", p["id"]) or {"buts": [], "compo": {}, "penaltys": []}
            bd, be = (int(x) for x in p["resultat_reel"]["score"].split("-"))
            buteurs = {}
            for camp in ("domicile", "exterieur"):
                tid = str(p[f"id_{camp}"])
                groupe = m["compo"].get(tid)
                nouv = appliquer_compo(p[f"_candidats_{camp}"], _groupe_verif(groupe))[0] if groupe \
                    else p[f"_candidats_{camp}"][:2]
                anc = _liste_ancienne(anciens[p["id"]][f"_candidats_{camp}"], groupe)
                # probabilités de TOUS les candidats (univers commun pour comparer les deux calculs)
                if groupe:
                    t_, b_, _ = appliquer_compo(p[f"_candidats_{camp}"], _groupe_verif(groupe), n=999, n_banc=999)
                    tous_nouv = t_ + b_
                else:
                    tous_nouv = p[f"_candidats_{camp}"]
                tous_anc = _liste_ancienne(anciens[p["id"]][f"_candidats_{camp}"], groupe, n=999)
                buteurs[camp] = {"nouveau": nouv, "ancien": anc,
                                 "tous_nouveau": tous_nouv, "tous_ancien": tous_anc,
                                 "groupe": [c["jid"] for c in groupe or [] if c["titulaire"] or c["entre"]],
                                 "reels": [b for b in m["buts"] if b["tid"] == tid],
                                 "penaltys": [x for x in m["penaltys"] if x["tid"] == tid],
                                 "candidats_nouveau": p[f"_candidats_{camp}"]}
            lignes.append({"date": j.isoformat(), "match": f"{p['domicile']} - {p['exterieur']}", "p": p,
                           "variantes": p["proba_1n2_variantes"], "lambdas": _lambdas_variantes(p),
                           "cotes": bool(p.get("buts_attendus_cotes")), "reel": (bd, be),
                           "issue": p["resultat_reel"]["1n2"], "buteurs": buteurs})
        if verbeux:
            print(f"  {j.isoformat()} : {sum(1 for l in lignes if l['date'] == j.isoformat())} matchs terminés "
                  f"(données antérieures au {avant.isoformat()})", file=sys.stderr)
    return lignes


def _brier3(p, issue):
    vrai = {"1": (1, 0, 0), "N": (0, 1, 0), "2": (0, 0, 1)}[issue]
    return sum((a - b) ** 2 for a, b in zip(p, vrai))


def resume_variantes(lignes):
    """Agrégats par variante : 1N2 (justes, Brier), buts (attendus / réels), +2,5 et BTTS."""
    res = {}
    for v in ("stats", "melange", "cotes"):
        r = {"n": 0, "justes": 0, "brier": 0.0, "buts_pred": 0.0, "buts_reels": 0, "over_p": 0.0,
             "over_r": 0, "over_brier": 0.0, "btts_p": 0.0, "btts_r": 0, "btts_brier": 0.0}
        for l in lignes:
            pv = l["variantes"].get(v) or l["variantes"]["stats"]
            lh, la = l["lambdas"][v]
            st = stats_matrice(matrice_scores(lh, la))
            bd, be = l["reel"]
            r["n"] += 1
            choix = "1N2"[max(range(3), key=lambda i: pv[i])]
            r["justes"] += choix == l["issue"]
            r["brier"] += _brier3(pv, l["issue"])
            r["buts_pred"] += lh + la
            r["buts_reels"] += bd + be
            o, bt = bd + be > 2.5, bd > 0 and be > 0
            r["over_p"] += st["over"][2.5]; r["over_r"] += o; r["over_brier"] += (st["over"][2.5] - o) ** 2
            r["btts_p"] += st["btts"]; r["btts_r"] += bt; r["btts_brier"] += (st["btts"] - bt) ** 2
        res[v] = r
    return res


def resume_buteurs(lignes):
    """Agrégats sur les buteurs listés (2 par équipe) : ancien calcul contre nouveau."""
    res = {}
    for meth in ("ancien", "nouveau"):
        r = {"listes": 0, "attendus": 0.0, "marque": 0, "brier": 0.0, "buts": 0, "buts_couverts": 0,
             "univers": 0, "brier_univers": 0.0, "logloss_univers": 0.0}
        for l in lignes:
            for camp in ("domicile", "exterieur"):
                d = l["buteurs"][camp]
                ids_marque = {b["jid"] for b in d["reels"]}
                # univers commun : tous les joueurs entrés en jeu (+ buteurs), proba 0 s'ils ne sont pas candidats
                probas = {str(b.get("id")): b["proba_buteur"] for b in d[f"tous_{meth}"]}
                for jid in set(d["groupe"]) | ids_marque:
                    q = probas.get(jid, 0.0)
                    a = jid in ids_marque
                    r["univers"] += 1
                    r["brier_univers"] += (q - a) ** 2
                    q = min(max(q, 0.01), 0.99)
                    r["logloss_univers"] -= math.log(q if a else 1 - q)
                ids_liste = {str(b.get("id")) for b in d[meth]}
                for b in d[meth]:
                    a = str(b.get("id")) in ids_marque
                    r["listes"] += 1; r["attendus"] += b["proba_buteur"]; r["marque"] += a
                    r["brier"] += (b["proba_buteur"] - a) ** 2
                r["buts"] += len(d["reels"])
                r["buts_couverts"] += sum(1 for b in d["reels"] if b["jid"] in ids_liste)
        res[meth] = r
    # penaltys : le tireur effectif était-il signalé « tireur de penalty » ?
    pen = {"penaltys": 0, "tireur_signale": 0}
    for l in lignes:
        for camp in ("domicile", "exterieur"):
            d = l["buteurs"][camp]
            tireurs = {str(c["id"]) for c in d["candidats_nouveau"] if c.get("tireur_penalty")}
            for x in d["penaltys"]:
                pen["penaltys"] += 1
                pen["tireur_signale"] += x["jid"] in tireurs
    res["penaltys"] = pen
    return res


def backtest(jours, aujourdhui, meme_coupure=False):
    regle = (f"données antérieures au {min(jours).isoformat()}" if meme_coupure
             else "pour chaque date, données strictement antérieures à cette date")
    print(f"Backtest sur {', '.join(j.isoformat() for j in jours)} ({regle})")
    lignes = evaluer_jours(jours, aujourdhui, meme_coupure)
    if not lignes:
        print("  aucun match terminé")
        return
    n_cotes = sum(l["cotes"] for l in lignes)
    print(f"{len(lignes)} matchs terminés, dont {n_cotes} avec cotes DraftKings.")
    print("\n1N2, buts, +2,5 et BTTS par variante :")
    libs = {"stats": "modèle statistique seul", "melange": "mélange 70/30 (ancien défaut)",
            "cotes": "cotes seules (nouveau défaut)"}
    for v, r in resume_variantes(lignes).items():
        n = r["n"]
        print(f"  {libs[v]:31s}: 1N2 {r['justes']}/{n} ({r['justes'] / n:.0%}), Brier {r['brier'] / n:.3f} | "
              f"buts prévus {r['buts_pred']:.1f} / réels {r['buts_reels']} | "
              f"+2,5 prévu {r['over_p'] / n:.0%} / réel {r['over_r'] / n:.0%} (Brier {r['over_brier'] / n:.3f}) | "
              f"BTTS prévu {r['btts_p'] / n:.0%} / réel {r['btts_r'] / n:.0%} (Brier {r['btts_brier'] / n:.3f})")
    print("\nPar date (Brier 1N2 / buts prévus vs réels) :")
    for j in jours:
        sous = [l for l in lignes if l["date"] == j.isoformat()]
        if not sous:
            continue
        rv = resume_variantes(sous)
        print(f"  {j.isoformat()} : " + " | ".join(
            f"{v} {rv[v]['justes']}/{rv[v]['n']} Brier {rv[v]['brier'] / rv[v]['n']:.3f} "
            f"buts {rv[v]['buts_pred']:.1f}/{rv[v]['buts_reels']}" for v in ("stats", "melange", "cotes")))
    print("\nButeurs probables (2 par équipe, compo officielle appliquée) :")
    bt = resume_buteurs(lignes)
    libs_b = {"ancien": "ancien (C1 seule, remplaçants x0,35 gardés)",
              "nouveau": "nouveau (toutes compét., penalty, titulaires)"}
    for meth in ("ancien", "nouveau"):
        r = bt[meth]
        if not r["listes"]:
            continue
        print(f"  {libs_b[meth]:46s}: {r['marque']}/{r['listes']} ont marqué "
              f"(attendu {r['attendus']:.1f}, proba moyenne {r['attendus'] / r['listes']:.0%}), "
              f"Brier {r['brier'] / r['listes']:.3f}, buts marqués par un joueur listé "
              f"{r['buts_couverts']}/{r['buts']}")
        print(f"  {'':46s}  univers commun ({r['univers']} joueurs entrés en jeu) : Brier "
              f"{r['brier_univers'] / r['univers']:.4f}, log-loss {r['logloss_univers'] / r['univers']:.4f}")
    pen = bt["penaltys"]
    if pen["penaltys"]:
        print(f"  Penaltys tirés : {pen['penaltys']}, dont {pen['tireur_signale']} par un joueur signalé "
              "« tireur de penalty »")


def main(argv=None):
    p = argparse.ArgumentParser(description="Pronostics statistiques Ligue des champions (source ESPN).")
    p.add_argument("--date", help="Date AAAA-MM-JJ (défaut : aujourd'hui, heure de Paris)")
    p.add_argument("--out", help="Dossier de sortie pour pronos_AAAA-MM-JJ.md et .json")
    p.add_argument("--sans-cotes", action="store_true", help="N'utiliser que le modèle statistique")
    p.add_argument("--melange", action="store_true",
                   help=f"Ancien calcul : mélange {POIDS_COTES:.0%} cotes / {1 - POIDS_COTES:.0%} modèle statistique "
                        "(par défaut : cotes seules quand elles existent)".replace("%", "%%"))
    p.add_argument("--buteurs-c1", action="store_true",
                   help="Ancien calcul des buteurs (buts en C1 seulement, au lieu de toutes les compétitions)")
    p.add_argument("--meme-coupure", action="store_true",
                   help="Backtest : utiliser pour toutes les dates les données antérieures à la 1re date")
    p.add_argument("--sans-verif", action="store_true",
                   help="Ne pas lancer la vérification automatique (ucl_verif.py) — déconseillé")
    p.add_argument("--backtest", nargs="+", metavar="AAAA-MM-JJ",
                   help="Évalue les pronostics de ces dates (pour chaque date, données antérieures uniquement)")
    args = p.parse_args(argv)
    aujourdhui = aujourdhui_paris()

    def lire(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            p.error(f"date invalide « {s} », attendu AAAA-MM-JJ")

    try:
        if args.backtest:
            backtest([lire(s) for s in args.backtest], aujourdhui, meme_coupure=args.meme_coupure)
            return 0
        jour = lire(args.date) if args.date else aujourdhui
        mode = "stats" if args.sans_cotes else ("melange" if args.melange else MODE_DEFAUT)
        pronos, meta = predire(jour, aujourdhui, mode=mode, buteurs_mode="c1" if args.buteurs_c1 else "saison")
    except RuntimeError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1

    verif = None
    if args.sans_verif:
        for pr in pronos:
            pr.pop("_candidats_domicile", None)
            pr.pop("_candidats_exterieur", None)
    else:
        try:
            verif = verifier_pronostics(jour, pronos)
        except RuntimeError as e:
            print(f"Erreur pendant la vérification : {e}", file=sys.stderr)
            return 1
        if verif["statut"] == "ERREUR":
            print("❌ La vérification a détecté des erreurs de cohérence (voir « Contrôles »).", file=sys.stderr)

    print(rapport_texte(jour, pronos, meta, verif))
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        base = os.path.join(args.out, f"pronos_{jour.isoformat()}")
        with open(base + ".md", "w", encoding="utf-8") as f:
            f.write(rapport_markdown(jour, pronos, meta, verif))
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump({"date": jour.isoformat(), "competition": "UEFA Champions League",
                       "source": "ESPN (+ cotes DraftKings via ESPN)",
                       "avertissement": AVERTISSEMENT.replace("**", "").replace("⚠️ ", ""),
                       "parametres": {"mode": meta["mode"], "buteurs": meta["buteurs_mode"],
                                      "domicile": DOMICILE, "exterieur": EXTERIEUR,
                                      "prior_matchs": PRIOR_MATCHS, "poids_cotes": POIDS_COTES,
                                      "rho_dixon_coles": RHO_DC, "bonus_penalty": BONUS_PENALTY,
                                      "facteur_banc": FACTEUR_BANC},
                       "matchs_historique": meta["matchs_historique"],
                       "verification": verif,
                       "pronostics": pronos}, f, ensure_ascii=False, indent=2)
        print(f"\nPronostics écrits : {base}.md et {base}.json")
    return 2 if verif and verif["statut"] == "ERREUR" else 0


if __name__ == "__main__":
    sys.exit(main())
