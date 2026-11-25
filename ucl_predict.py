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
     implicites du marché et on les mélange avec le modèle statistique (les cotes sont
     en général le meilleur signal disponible).
  4. Loi de Poisson (avec correction Dixon-Coles des petits scores) → probabilités 1/N/2,
     score exact le plus probable, buts attendus, plus de 2,5 buts, les deux équipes marquent.
  5. Buteurs probables : les 2 meilleurs buteurs de chaque équipe en C1 cette saison.
  6. Vérification automatique (ucl_verif.py) avant affichage / écriture : disponibilité
     des buteurs (compo officielle, effectif, actualité ESPN, absences.json) et
     contrôles de cohérence. Désactivable avec --sans-verif.

Ce sont des ESTIMATIONS STATISTIQUES, pas des certitudes.

Bibliothèque standard uniquement. Exemples :
    python3 ucl_predict.py                               # aujourd'hui (heure de Paris)
    python3 ucl_predict.py --date 2026-11-25 --out pronostics/
    python3 ucl_predict.py --backtest 2026-11-03 2026-11-04   # 4e journée
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

from ucl_bot import BASE, aujourdhui_paris, date_longue, get_json, parse_utc, vers_paris
from ucl_verif import bloc_verif_markdown, bloc_verif_texte, verifier_pronostics

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
# Prédiction
# --------------------------------------------------------------------------
def predire(jour, aujourdhui, avec_cotes=True, donnees_avant=None, verbeux=True):
    avant = donnees_avant or jour
    historique = matchs_joues(avant, aujourdhui)
    mu, notes = notes_equipes(historique)
    buteurs, manquants, ids_joueurs = buteurs_saison(historique)
    if verbeux:
        print(f"Données : {len(historique)} matchs de C1 joués avant le {avant.strftime('%d/%m/%Y')}"
              f" ({len(notes)} équipes), moyenne {mu * (DOMICILE + EXTERIEUR) / 2:.2f} buts/équipe/match.",
              file=sys.stderr)
        if manquants:
            print(f"[avertissement] {manquants} match(s) sans détail des buteurs.", file=sys.stderr)

    defaut = {"att": 1.0, "def": 1.0, "mj": 0, "bp": 0, "bc": 0, "v": 0, "n": 0, "d": 0}
    pronos = []
    for ev in matchs_du_jour(jour, aujourdhui):
        dom, ext = _equipes(ev)
        nd, ne = notes.get(dom["id"], dict(defaut, nom=dom["nom"])), notes.get(ext["id"], dict(defaut, nom=ext["nom"]))
        lh_stat = mu * DOMICILE * nd["att"] * ne["def"]
        la_stat = mu * EXTERIEUR * ne["att"] * nd["def"]
        mk = None
        if avec_cotes:
            try:
                mk = cotes_marche(summary(ev["id"], termine=_etat(ev) == "post"))
            except RuntimeError as e:
                print(f"[avertissement] cotes indisponibles ({ev['id']}) : {e}", file=sys.stderr)
        if mk:
            lh_mk, la_mk = lambdas_marche(mk)
            lh = math.exp(POIDS_COTES * math.log(lh_mk) + (1 - POIDS_COTES) * math.log(lh_stat))
            la = math.exp(POIDS_COTES * math.log(la_mk) + (1 - POIDS_COTES) * math.log(la_stat))
        else:
            lh_mk = la_mk = None
            lh, la = lh_stat, la_stat
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
        pronos.append({
            "id": ev["id"], "heure_paris": ko.strftime("%Hh%M"),
            "domicile": dom["nom"], "exterieur": ext["nom"],
            "id_domicile": dom["id"], "id_exterieur": ext["id"],
            "bilan_domicile": _bilan(nd), "bilan_exterieur": _bilan(ne),
            "forme_domicile": dom.get("forme"), "forme_exterieur": ext.get("forme"),
            "notes": {"att_dom": round(nd["att"], 2), "def_dom": round(nd["def"], 2),
                      "att_ext": round(ne["att"], 2), "def_ext": round(ne["def"], 2)},
            "buts_attendus_stats": [round(lh_stat, 2), round(la_stat, 2)],
            "buts_attendus_cotes": [round(lh_mk, 2), round(la_mk, 2)] if mk else None,
            "cotes": mk and {"bookmaker": mk["bookmaker"], "cotes_1n2": mk["cotes"],
                             "proba_1n2": [round(mk["p1"], 3), round(mk["pn"], 3), round(mk["p2"], 3)]},
            "buts_attendus": [round(lh, 2), round(la, 2)],
            "proba_1n2": [round(st["p1"], 3), round(st["pn"], 3), round(st["p2"], 3)],
            "pronostic_1n2": choix,
            "score_probable": f"{st['score'][0]}-{st['score'][1]}", "proba_score": round(st["p_score"], 3),
            "plus_2_5": round(st["over"][2.5], 3), "deux_equipes_marquent": round(st["btts"], 3),
            "buteurs_domicile": buteurs_probables(dom["id"], lh, buteurs, nd["bp"], ids=ids_joueurs),
            "buteurs_exterieur": buteurs_probables(ext["id"], la, buteurs, ne["bp"], ids=ids_joueurs),
            # liste complète (interne) pour remplacer un buteur absent ; retirée par ucl_verif
            "_candidats_domicile": buteurs_probables(dom["id"], lh, buteurs, nd["bp"], n=None, ids=ids_joueurs),
            "_candidats_exterieur": buteurs_probables(ext["id"], la, buteurs, ne["bp"], n=None, ids=ids_joueurs),
            "resultat_reel": reel,
        })
    return pronos, {"matchs_historique": len(historique), "equipes": len(notes), "mu": mu}


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
        return "pas de données (aucun but en C1 cette saison)"
    morceaux = []
    for b in lst:
        s = f"{b['joueur']} ({b['buts_c1']} but{'s' if b['buts_c1'] > 1 else ''} en C1, ~{pct(b['proba_buteur'])}"
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
                       f"  (modèle seul : {p['buts_attendus_stats'][0]:.2f} - {p['buts_attendus_stats'][1]:.2f} buts attendus)")
        else:
            out.append("   Cotes : non disponibles (modèle statistique seul)")
        for camp, nom in (("domicile", d), ("exterieur", e)):
            out.append(f"   Buteurs probables {nom} : {_txt_buteurs(p[f'buteurs_{camp}'], p.get(f'disponibilite_{camp}'))}")
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
                      f"modèle seul {p['buts_attendus_stats'][0]:.2f} - {p['buts_attendus_stats'][1]:.2f})")
        else:
            md.append("- **Cotes :** non disponibles (modèle statistique seul)")
        for camp, nom in (("domicile", d), ("exterieur", e)):
            md.append(f"- **Buteurs probables {nom} :** {_txt_buteurs(p[f'buteurs_{camp}'], p.get(f'disponibilite_{camp}'))}")
            if p.get(f"absents_{camp}"):
                md.append(f"- **Absents {nom} :** " + ", ".join(
                    (f"{a['joueur']} ({a['raison']}" + (f", [source]({a['source']})" if str(a.get('source', '')).startswith('http') else "") + ")")
                    for a in p[f"absents_{camp}"]))
        if p["resultat_reel"]:
            md.append(f"- **Résultat réel :** {p['resultat_reel']['score']}")
    md += ["", "## Méthode", "",
           "- Notes d'attaque et de défense calculées sur les matchs de C1 de la saison (ajustées à la force "
           f"des adversaires), rapprochées de la moyenne par un a priori équivalent à {PRIOR_MATCHS:.0f} matchs.",
           f"- Avantage du terrain : x{DOMICILE} pour l'équipe qui reçoit, x{EXTERIEUR} pour le visiteur.",
           f"- Quand ESPN publie des cotes, les buts attendus implicites du marché comptent pour "
           f"{POIDS_COTES:.0%} et le modèle statistique pour {1 - POIDS_COTES:.0%}.".replace("%", " %"),
           "- Loi de Poisson avec correction Dixon-Coles pour les petits scores.",
           "- Buteurs : part des buts de l'équipe en C1 cette saison (rétrécie), appliquée aux buts attendus ; "
           "probabilité « buteur à tout moment » indicative (titularisation non connue avant la compo officielle).",
           "- Vérification (`ucl_verif.py`) : un buteur absent (compo officielle, effectif ESPN, actualité ESPN, "
           "`absences.json`) est retiré et remplacé par le meilleur buteur suivant de l'équipe ; un remplaçant voit sa "
           "probabilité réduite. Les probabilités 1/N/2 ne sont pas recalculées (les cotes intègrent en général "
           "déjà les absences connues)."]
    return "\n".join(md) + "\n"


# --------------------------------------------------------------------------
def backtest(jours, aujourdhui):
    avant = min(jours)
    print(f"Backtest sur {', '.join(j.isoformat() for j in jours)} (données antérieures au {avant.isoformat()})")
    for avec_cotes in (False, True):
        bons = total = 0
        brier = 0.0
        for j in jours:
            pronos, _ = predire(j, aujourdhui, avec_cotes=avec_cotes, donnees_avant=avant, verbeux=False)
            for p in pronos:
                p.pop("_candidats_domicile", None)
                p.pop("_candidats_exterieur", None)
            for p in pronos:
                if not p["resultat_reel"]:
                    continue
                total += 1
                bons += p["resultat_reel"]["1n2"] == p["pronostic_1n2"]
                vrai = {"1": (1, 0, 0), "N": (0, 1, 0), "2": (0, 0, 1)}[p["resultat_reel"]["1n2"]]
                brier += sum((a - b) ** 2 for a, b in zip(p["proba_1n2"], vrai))
        lib = "modèle + cotes" if avec_cotes else "modèle statistique seul"
        if total:
            print(f"  {lib:25s}: {bons}/{total} pronostics 1N2 justes ({bons / total:.0%}), "
                  f"score de Brier moyen {brier / total:.3f}")
        else:
            print(f"  {lib:25s}: aucun match terminé")


def main(argv=None):
    p = argparse.ArgumentParser(description="Pronostics statistiques Ligue des champions (source ESPN).")
    p.add_argument("--date", help="Date AAAA-MM-JJ (défaut : aujourd'hui, heure de Paris)")
    p.add_argument("--out", help="Dossier de sortie pour pronos_AAAA-MM-JJ.md et .json")
    p.add_argument("--sans-cotes", action="store_true", help="N'utiliser que le modèle statistique")
    p.add_argument("--sans-verif", action="store_true",
                   help="Ne pas lancer la vérification automatique (ucl_verif.py) — déconseillé")
    p.add_argument("--backtest", nargs="+", metavar="AAAA-MM-JJ",
                   help="Évalue les pronostics de ces dates (données antérieures à la 1re date uniquement)")
    args = p.parse_args(argv)
    aujourdhui = aujourdhui_paris()

    def lire(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            p.error(f"date invalide « {s} », attendu AAAA-MM-JJ")

    try:
        if args.backtest:
            backtest([lire(s) for s in args.backtest], aujourdhui)
            return 0
        jour = lire(args.date) if args.date else aujourdhui
        pronos, meta = predire(jour, aujourdhui, avec_cotes=not args.sans_cotes)
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
                       "parametres": {"domicile": DOMICILE, "exterieur": EXTERIEUR,
                                      "prior_matchs": PRIOR_MATCHS, "poids_cotes": POIDS_COTES,
                                      "rho_dixon_coles": RHO_DC},
                       "matchs_historique": meta["matchs_historique"],
                       "verification": verif,
                       "pronostics": pronos}, f, ensure_ascii=False, indent=2)
        print(f"\nPronostics écrits : {base}.md et {base}.json")
    return 2 if verif and verif["statut"] == "ERREUR" else 0


if __name__ == "__main__":
    sys.exit(main())
