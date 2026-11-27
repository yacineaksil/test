#!/usr/bin/env python3
"""Analyse d'un ticket combiné, toutes compétitions suivies par ESPN
(football et sports américains : NFL, NCAA, NBA, NHL, MLB, WNBA).

Pour chaque sélection :
  - statut (gagné / perdu / remboursé / en cours / à venir) et score ;
  - probabilité réelle (cotes DraftKings via ESPN, marge retirée) et, en
    direct, probabilité recalculée à partir du score et du temps restant ;
  - valeur de ta cote (ta cote × probabilité réelle − 1) et mouvement du marché ;
  - garde-fou : match retrouvé, horaire conforme, pas commencé, actu ESPN
    (blessures / suspensions) ; football : compo officielle et rotation du
    favori ; sports US : blessés des deux équipes, matchs à la suite (NBA,
    NHL), gardiens partants (NHL), météo (NFL, NCAA), lanceurs (MLB).
Puis le ticket : probabilité qu'il passe, valeur juste, maillon faible.

Usage : python3 ticket_analyse.py tickets/ticket_2026-11-25.json [--sans-actu]
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import paris_espn as pe

SNAPSHOT = os.path.join("tickets", "cotes_avant_match.json")


# ---------------------------------------------------- buts attendus (Poisson)

def _pois(k, l):
    return math.exp(-l) * l ** k / math.factorial(k)


def _probas_poisson(lh, la, ligne=None, maxg=10):
    ph = [_pois(k, lh) for k in range(maxg + 1)]
    pa = [_pois(k, la) for k in range(maxg + 1)]
    p1 = pn = p2 = plus = 0.0
    for h in range(maxg + 1):
        for a in range(maxg + 1):
            v = ph[h] * pa[a]
            if h > a:
                p1 += v
            elif h == a:
                pn += v
            else:
                p2 += v
            if ligne is not None and h + a > ligne:
                plus += v
    return p1, pn, p2, plus


def buts_attendus(probas, ligne=None):
    """(lambda_dom, lambda_ext) les plus cohérents avec les probabilités du marché."""
    if "1" not in probas:
        return None
    cible_plus = probas.get(f"+{ligne}") if ligne is not None else None
    meilleur, err_min = None, 9.0
    for i in range(2, 90):
        lh = i * 0.05
        for j in range(2, 90):
            la = j * 0.05
            p1, pn, p2, plus = _probas_poisson(lh, la, ligne, maxg=8)
            err = (p1 - probas["1"]) ** 2 + (p2 - probas["2"]) ** 2 + (pn - probas["N"]) ** 2
            if cible_plus is not None:
                err += (plus - cible_plus) ** 2
            if err < err_min:
                meilleur, err_min = (lh, la), err
    return meilleur


def synthese_1n2(code, p):
    """Probabilités 1/N/2 plausibles quand seul le pari joué a une cote connue."""
    if code == "N":
        return {"1": (1 - p) / 2, "N": p, "2": (1 - p) / 2}
    pn = min(0.26, max(0.05, 1 - p - 0.03))
    autre = max(0.03, 1 - p - pn)
    return {"1": p, "N": pn, "2": autre} if code == "1" else {"1": autre, "N": pn, "2": p}


def minute_jouee(m):
    st = (m.get("statut") or "").upper()
    if "HT" in st or "HALF" in st or "MI-TEMPS" in st:
        return 45
    txt = (m.get("chrono") or "") + " " + (m.get("statut") or "")
    r = re.search(r"(\d+)'\s*(?:\+\s*(\d+))?", txt)
    if r:
        return int(r.group(1)) + (int(r.group(2)) if r.group(2) else 0)
    return None


def proba_en_direct(code, m, lambdas):
    """Probabilité que le pari passe, vu le score actuel et le temps restant."""
    minute = minute_jouee(m)
    h0, a0 = m["score"]
    if minute is None or lambdas is None or h0 is None:
        return None
    reste = max(0.0, (94 - minute) / 94)
    lh, la = lambdas[0] * reste, lambdas[1] * reste
    tot = 0.0
    for dh in range(9):
        for da in range(9):
            if pe.pari_gagnant(code, h0 + dh, a0 + da):
                tot += _pois(dh, lh) * _pois(da, la)
    return tot


# ---------------------------------------------------- photo des cotes

def charger_snapshot():
    try:
        with open(SNAPSHOT, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def sauver_snapshot(snap):
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------- compo et rotation

SCHEDULE = ("https://site.api.espn.com/apis/site/v2/sports/soccer/all/teams/{}/schedule"
            "?season={}")


def titulaires(ligue, event_id):
    try:
        d = pe.get_json(pe.url_summary(ligue, event_id))
    except RuntimeError:
        return {}
    res = {}
    for r in d.get("rosters") or []:
        tid = str((r.get("team") or {}).get("id"))
        xi = [(str(p["athlete"]["id"]), p["athlete"]["displayName"])
              for p in r.get("roster") or [] if p.get("starter")]
        if xi:
            res[tid] = xi
    return res


def dernier_match_championnat(team_id, avant):
    saison = avant.year if avant.month >= 7 else avant.year - 1
    evs = []
    for url in (SCHEDULE.format(team_id, saison),
                SCHEDULE.format(team_id, saison).split("?")[0]):
        try:
            evs = pe.get_json(url).get("events") or []
        except RuntimeError:
            evs = []
        if evs:
            break
    cands = []
    for e in evs:
        slug = (e.get("league") or {}).get("slug") or ""
        try:
            d = pe.parse_utc(e["date"])
        except (KeyError, ValueError):
            continue
        etat = e["competitions"][0]["status"]["type"].get("state")
        if d < avant and etat == "post" and re.match(r"^[a-z]{3}\.\d$", slug):
            cands.append((d, slug, e))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    d, slug, e = cands[-1]
    return {"id": e["id"], "ligue": slug, "date": d, "nom": e.get("shortName") or e.get("name")}


def analyse_rotation(m, team_id, xi_actuel):
    """Combien de titulaires du jour étaient titulaires au dernier match de championnat."""
    ref = dernier_match_championnat(team_id, m["debut_utc"])
    if not ref:
        return None
    xi_ref = titulaires(ref["ligue"], ref["id"]).get(str(team_id))
    if not xi_ref:
        return None
    ids_ref = {i for i, _ in xi_ref}
    communs = sum(1 for i, _ in xi_actuel if i in ids_ref)
    absents = [n for i, n in xi_ref if i not in {j for j, _ in xi_actuel}]
    return {"communs": communs, "reference": f"{ref['nom']} ({ref['date'].strftime('%d/%m')})",
            "absents_vs_reference": absents}


# ---------------------------------------------------- sports américains

RAPPELS_US = {
    "nfl": "liste officielle des inactifs publiée ~90 min avant le coup d'envoi",
    "ncaaf": "le jour du match (météo, quarterback titulaire et lignes NCAA bougent jusqu'au bout)",
    "nba": "statuts confirmés ~30 min avant (repos des stars souvent annoncé tard)",
    "wnba": "statuts confirmés ~30 min avant",
    "ncaab": "~1 h avant (absences connues tard en NCAA, pas de liste officielle)",
    "nhl": "gardiens confirmés le matin ou ~1 h avant le match",
    "mlb": "lanceurs partants annoncés la veille, alignement ~3 h avant",
}


def _camp(code):
    """Équipe sur laquelle repose le pari : '1', '2' ou None (total, nul)."""
    base, _ = pe.decomposer_code(code)
    if base in ("1", "1N") or base.startswith("1 "):
        return "1"
    if base in ("2", "N2") or base.startswith("2 "):
        return "2"
    return None


def _txt_blesse(b):
    txt = f"{b['nom']} ({b['poste'] + ', ' if b['poste'] else ''}{b['statut_fr']}"
    if b["note"] and b["statut"] in ("Day-To-Day", "Questionable", "Doubtful"):
        txt += f", note ESPN : « {b['note']} »"
    if b["etoile"]:
        txt += f" ; ⭐ {b['etoile']}"
    if not b["recent"]:
        txt += f" ; depuis le {b['date'].strftime('%d/%m')}"
    return txt + ")"


def garde_fou_us(m, code, avec_actu=True):
    """Contrôles d'une sélection de sport américain, avant ou pendant le match :
    blessés des deux équipes, quarterback, matchs à la suite (NBA, NHL, WNBA),
    gardiens (NHL), lanceurs (MLB), météo (NFL, NCAA, MLB), FCS et gros écarts
    (NCAA football), mouvement du marché, modèle ESPN (pour info), actu ESPN."""
    L = []
    lg = m["ligue"]
    t = pe.type_pari(code, True)
    camp = _camp(code)
    noms = {"1": m["domicile"], "2": m["exterieur"]}
    ids = {"1": m["id_domicile"], "2": m["id_exterieur"]}
    if m.get("neutre"):
        L.append(("ℹ️", "terrain neutre : « 1 » = équipe listée à domicile par ESPN ; vérifie que ton bookmaker "
                        "présente les équipes dans le même ordre"))
    mv = pe.mouvement_marche(code, m)
    if mv:
        L.append(mv)
    # blessés
    stars = pe.etoiles(m) if m["etat"] == "pre" else {}
    sans_liste = False
    for c in ("1", "2"):
        bl = pe.blessures_equipe(m, ids[c], stars.get(str(ids[c])))
        if bl is None:
            sans_liste = True
            continue
        if not bl:
            L.append(("✅", f"{noms[c]} : aucun blessé déclaré dans la liste ESPN"))
            continue
        graves = [b for b in bl if (b["etoile"] or b["cle"]) and b["recent"]
                  and b["statut"] in ("Out", "Doubtful", "Questionable", "Day-To-Day", "Suspension")]
        icone = "⚠️" if graves and (camp is None or camp == c or t == "total") else "ℹ️"
        reste = f" +{len(bl) - 4} autre(s)" if len(bl) > 4 else ""
        L.append((icone, f"blessés {noms[c]} ({len(bl)}) : " + ", ".join(_txt_blesse(b) for b in bl[:4]) + reste))
        absents = [b for b in graves if b["statut"] in ("Out", "Doubtful", "Suspension")]
        if absents and camp and camp != c and t != "total":
            L.append(("ℹ️", f"absence importante chez l'adversaire ({absents[0]['nom']}, {absents[0]['statut_fr']}) : "
                            f"normalement déjà dans la cote si l'info est publique"))
    if sans_liste:
        L.append(("ℹ️", "pas de liste de blessés ESPN pour cette compétition : vérifie l'actu des deux équipes"))
    if lg in ("nfl", "ncaaf"):
        for c in ("1", "2"):
            q = pe.note_quarterback(m, ids[c])
            if q:
                com = q["commentaire"] if len(q["commentaire"]) <= 150 else q["commentaire"][:147] + "…"
                L.append(("ℹ️", f"quarterback {noms[c]} ({q['date'].strftime('%d/%m')}) : « {com} »"))
    # fatigue : deux matchs en deux soirs
    if lg in ("nba", "nhl", "wnba"):
        rep = {c: pe.dos_a_dos(m, ids[c]) for c in ("1", "2")}
        txts = []
        for c in ("1", "2"):
            x = rep[c]
            if x is None:
                continue
            if x["veille"]:
                txts.append(f"{noms[c]} a joué la veille ({x['dernier']}) : 2e match en 2 soirs")
            else:
                txts.append(f"{noms[c]} : {x['repos']} jour(s) de repos")
        if txts:
            veille = {c for c in ("1", "2") if rep[c] and rep[c]["veille"]}
            icone = "⚠️" if (camp in veille and len(veille) == 1) or (t == "total" and veille) else \
                "ℹ️" if veille else "✅"
            L.append((icone, "fatigue : " + " ; ".join(txts)))
    # gardiens (NHL)
    if lg == "nhl":
        partants, stats = pe.gardiens(m)
        for c, p in zip(("1", "2"), partants):
            if not p:
                L.append(("ℹ️", f"gardien partant de {noms[c]} pas encore annoncé"))
                continue
            statut = {"Confirmed": "confirmé", "Expected": "attendu, pas encore confirmé"}.get(p["statut"], p["statut"] or "?")
            lst = stats.get(str(ids[c])) or []
            moi = next((g for g in lst if g["id"] == p["id"]), None)
            n1 = max(lst, key=lambda g: g["matchs"] or 0) if lst else None
            txt = f"gardien {noms[c]} : {p['nom']} ({statut}"
            if moi and moi["matchs"] is not None:
                txt += f", {moi['matchs']} matchs cette saison"
                try:
                    txt += f", {100 * float(moi['arrets']):.1f} % d'arrêts".replace(".", ",")
                except (TypeError, ValueError):
                    pass
            txt += ")"
            icone = "✅" if p["statut"] == "Confirmed" else "ℹ️"
            if n1 and moi and n1["id"] != moi["id"] and (n1["matchs"] or 0) > (moi["matchs"] or 0):
                if (n1["matchs"] or 0) >= 1.5 * (moi["matchs"] or 0) + 2:
                    txt += f" — c'est le n°2 : le n°1 {n1['nom']} a joué {n1['matchs']} matchs"
                    icone = "⚠️" if camp == c or t == "total" else "ℹ️"
                else:
                    txt += f" — poste partagé avec {n1['nom']} ({n1['matchs']} matchs)"
            L.append((icone, txt))
    # lanceurs (MLB)
    if lg == "mlb":
        for c, p in zip(("1", "2"), m.get("partants") or (None, None)):
            if p:
                L.append(("ℹ️", f"lanceur partant {noms[c]} : {p['nom']}" + (f" ({p['statut']})" if p.get("statut") else "")))
            else:
                L.append(("⚠️", f"lanceur partant de {noms[c]} pas annoncé : la cote peut encore bouger fortement"))
    # météo (plein air)
    if lg in ("nfl", "ncaaf", "mlb") and m["etat"] == "pre":
        w = pe.meteo(m)
        if w:
            txt = f"météo prévue (AccuWeather via ESPN) : {w['temp_c']:.0f} °C"
            if w["rafales_kmh"] is not None:
                txt += f", rafales {w['rafales_kmh']:.0f} km/h"
            if w["precipitations"] is not None:
                txt += f", risque de précipitations {w['precipitations']} %"
            dur = (w["rafales_kmh"] or 0) >= 40 or (w["precipitations"] or 0) >= 60 or w["temp_c"] <= -5
            if dur:
                txt += " → conditions difficiles : en général moins de points (passes et coups de pied gênés)"
            base = pe.decomposer_code(code)[0]
            L.append(("⚠️" if dur and base.startswith("+") else "ℹ️", txt))
        elif lg != "mlb":
            L.append(("ℹ️", "pas de météo publiée (stade couvert ou prévision pas encore disponible)"))
    # NCAA football : FCS, gros écarts, classement
    if lg == "ncaaf":
        for c in ("1", "2"):
            if pe.est_fcs(m, ids[c]):
                L.append(("⚠️", f"{noms[c]} est une équipe de FCS (division inférieure) : écarts énormes possibles"))
        h = (m.get("cotes") or {}).get("hcp")
        if h and abs(h["ligne"]) >= 20 and t in ("handicap", "ml"):
            L.append(("ℹ️", f"gros écart annoncé ({pe.fmt_ligne(h['ligne'])} pour {m['domicile']}) : le favori sort souvent "
                            f"ses titulaires en fin de match, le handicap est plus aléatoire que le vainqueur"))
    rangs = m.get("rangs") or (None, None)
    if lg in ("ncaaf", "ncaab") and any(r and r <= 25 for r in rangs):
        L.append(("ℹ️", "classement AP : " + " | ".join(f"{noms[c]} {'n°' + str(r) if r and r <= 25 else 'non classé'}"
                                                       for c, r in zip(("1", "2"), rangs))))
    # modèle ESPN : information secondaire, jamais utilisée (leçon A)
    pr = pe.predicteur(m) if m["etat"] == "pre" else None
    p = m.get("probas") or {}
    if pr and p.get("1") is not None:
        L.append(("ℹ️", f"modèle ESPN, pour info (non utilisé) : {m['domicile']} {100 * pr[0]:.0f} % / "
                        f"{m['exterieur']} {100 * pr[1]:.0f} % — marché : {100 * p['1']:.0f} % / {100 * p['2']:.0f} %"))
    if avec_actu:
        alertes = []
        for c in ("1", "2"):
            for al in pe.alertes_actu(lg, ids[c], noms[c]):
                alertes.append((noms[c], al))
        if alertes:
            L.append(("⚠️", f"{len(alertes)} article(s) ESPN récents sur blessures / repos / titularisations — à lire :"))
            for nom, al in alertes[:4]:
                L.append(("  •", f"[{nom}] {al['date']} « {al['titre']} » {al['lien']}"))
        else:
            L.append(("✅", "aucune alerte blessure / repos dans l'actu ESPN des 7 derniers jours"))
    if m["etat"] == "pre" and lg in RAPPELS_US:
        L.append(("ℹ️", f"relancer avant de valider : {RAPPELS_US[lg]}"))
    return L


# ---------------------------------------------------- analyse

def analyser(ticket, avec_actu=True):
    snap = charger_snapshot()
    lignes_res = []
    maintenant = pe.maintenant_utc()
    for i, sel in enumerate(ticket["selections"], 1):
        jour = date.fromisoformat(sel["date"])
        deux = pe.est_deux_issues(sel["ligue"])
        m, sim = pe.trouver_match(sel["ligue"], jour, sel["domicile"], sel["exterieur"])
        # pari libellé avec un nom d'équipe (« Knicks -4.5 ») : déjà dans l'ordre ESPN
        code = pe.pari_depuis_equipes(sel["pari"], m) if m is not None else None
        if not code:
            code = pe.normaliser_pari(sel["pari"], sel["ligue"])
            if m is not None and m.get("inverse"):
                code = pe.inverser_code(code)
        r = {"n": i, "sel": sel, "code": code, "match": m, "similarite": sim,
             "garde_fou": [], "alertes": []}
        lignes_res.append(r)
        if m is None:
            r["etat"] = "introuvable"
            r["garde_fou"].append(("❌", f"match introuvable sur ESPN ({pe.nom_competition(sel['ligue'])}"
                                         f", {sel['date']}) — vérifie la compétition et les noms"))
            r["p_depart"] = (1 / sel["cote"]) / pe.MARGE_PAR_DEFAUT
            r["p_maintenant"] = r["p_depart"]
            continue
        r["garde_fou"].append(("✅", f"match retrouvé : {m['domicile']} - {m['exterieur']} "
                                     f"({m['competition']})"))
        if m.get("inverse"):
            r["garde_fou"].append(("⚠️", f"équipes dans l'ordre inverse d'ESPN (à domicile : {m['domicile']}) : "
                                         f"ton pari est lu comme « {pe.libelle_pari(code, m)} »"))
        if pe.type_pari(code, deux) is None:
            r["garde_fou"].append(("❌", f"type de pari non reconnu : « {sel['pari']} » — écris par exemple "
                                         + ("1, 2, 1 -5.5, 2 +3.5, +224.5, -6.5 ou 1X2. V1 (temps réglementaire)"
                                            if deux else "1X2. V1, X, 1X, +2.5, -2.5")))
        h_ticket = sel.get("heure")
        h_espn = m["debut_paris"].strftime("%H:%M")
        if h_ticket and h_ticket != h_espn:
            r["garde_fou"].append(("⚠️", f"horaire différent : ticket {h_ticket}, ESPN {h_espn} (heure de Paris)"))
        else:
            r["garde_fou"].append(("✅", f"coup d'envoi {pe.fmt_heure(m)} (heure de Paris)"))

        # cotes : on garde la dernière photo d'avant-match
        if m["etat"] == "pre" and m["cotes"]:
            snap[m["id"]] = {"cotes": {k: v for k, v in m["cotes"].items()},
                             "capture": maintenant.isoformat(timespec="minutes")}
        cotes_ref = m["cotes"] if (m["etat"] == "pre" and m["cotes"]) else (snap.get(m["id"]) or {}).get("cotes")
        cloture = False
        if not cotes_ref and deux and m["etat"] != "pre":
            # sports US : le tableau ESPN retire les cotes au coup d'envoi, le résumé les garde
            cotes_ref = pe.cotes_resume(m, "close")
            cloture = bool(cotes_ref)
        probas_ref = pe.probas_marche(cotes_ref) if cotes_ref else {}
        mref = dict(m, cotes=cotes_ref, probas=probas_ref)
        p_dep, source = pe.proba_pari(code, mref, sel["cote"])
        r["p_depart"], r["source"] = p_dep, source
        r["cote_marche"] = pe.cote_marche(code, mref) if cotes_ref else None
        r["cote_ouverture"] = pe.cote_ouverture(code, mref) if cotes_ref else None
        if cloture:
            r["garde_fou"].append(("✅", f"cotes de clôture {cotes_ref.get('bookmaker', '?')} retrouvées dans le résumé "
                                         f"ESPN (le tableau des matchs les retire au coup d'envoi)"))
        elif cotes_ref:
            r["garde_fou"].append(("✅", f"cotes du marché publiées ({cotes_ref.get('bookmaker', '?')})"))
        else:
            r["garde_fou"].append(("⚠️", "pas de cote de marché publiée : probabilité estimée depuis ta cote"))
        r["valeur"] = sel["cote"] * p_dep - 1 if p_dep else None

        h, a = m["score"]
        if m["termine"]:
            res = pe.resultat_pari(code, m)
            if res is None:
                r["etat"] = "non_reconnu"
                r["p_maintenant"] = p_dep
            elif res == "rembourse":
                r["etat"] = "rembourse"
                r["p_maintenant"] = 1.0
                r["garde_fou"].append(("↩️", "ligne tombée pile : pari remboursé (push), la sélection compte pour une "
                                             "cote de 1,00"))
            else:
                r["etat"] = res
                r["p_maintenant"] = 1.0 if res == "gagne" else 0.0
            if deux:
                tr = m.get("score_tr") or (None, None)
                if tr[0] is not None and tuple(tr) != (h, a):
                    r["garde_fou"].append(("ℹ️", f"décidé {pe.statut_fin_fr(m['statut'])} : "
                                                 f"{tr[0]}-{tr[1]} à la fin du temps réglementaire"))
                if h == a and pe.type_pari(code, True) == "ml":
                    r["garde_fou"].append(("⚠️", "match nul après prolongation : le moneyline est compté PERDU ici ; "
                                                 "chez DraftKings et beaucoup de bookmakers il est REMBOURSÉ "
                                                 "(cote 1,00) — vérifie ton règlement"))
        elif m["etat"] == "post":
            r["etat"] = "annule"
            r["p_maintenant"] = 1.0
            r["garde_fou"].append(("⚠️", f"match non joué ({m['statut']}) : en général la sélection compte "
                                         f"pour une cote de 1.00 — vérifie le règlement de ton bookmaker"))
        elif m["etat"] == "in":
            r["etat"] = "en_cours"
            if deux:
                pdir = pe.proba_direct_us(code, m, cotes_ref or {}, probas_ref, p_dep)
                r["p_maintenant"] = pdir if pdir is not None else p_dep
                r["passe_actuellement"] = pe.resultat_scores(code, m["score"], m.get("score_tr"), True) \
                    in ("gagne", "rembourse")
                modele = pe.PARAMS.get(m["ligue"], {}).get("modele")
                r["modele_direct"] = ("loi de Poisson sur les buts restants" if modele == "poisson"
                                      else "loi normale sur l'écart et le total restants")
                live = pe.cotes_resume(m, "live")
                if live:
                    pl = pe.probas_marche(live)
                    if code in pl:
                        r["marche_direct"] = pl[code]
            else:
                if probas_ref:
                    lam = buts_attendus(probas_ref, cotes_ref.get("ligne") if cotes_ref else None)
                elif code in ("1", "N", "2") and p_dep:
                    # pas de cote de marché : 1N2 reconstitué depuis ta cote (nul ~26 %)
                    lam = buts_attendus(synthese_1n2(code, p_dep))
                    r["source"] = r.get("source", "") + " ; direct estimé avec un nul à ~26 %"
                else:
                    lam = None
                pdir = proba_en_direct(code, m, lam)
                r["p_maintenant"] = pdir if pdir is not None else p_dep
                r["passe_actuellement"] = pe.pari_gagnant(code, h, a)
        else:
            r["etat"] = "a_venir"
            r["p_maintenant"] = p_dep
            if m["debut_utc"] <= maintenant:
                r["garde_fou"].append(("⚠️", "l'heure du coup d'envoi est passée mais ESPN n'indique pas le match en cours"))

        if deux:
            # sports US : blessés, fatigue, gardiens, météo... pour ce qui reste à jouer
            if r["etat"] in ("a_venir", "en_cours"):
                r["garde_fou"].extend(garde_fou_us(mref if cotes_ref else m, code, avec_actu))
            continue

        # matchs joués : rotation du favori, pour le bilan (leçon D)
        if r["etat"] in ("gagne", "perdu") and code in ("1", "2"):
            xi = titulaires(m["ligue"], m["id"])
            fav = m["id_exterieur"] if code == "2" else m["id_domicile"]
            if str(fav) in xi:
                rot = analyse_rotation(m, fav, xi[str(fav)])
                if rot:
                    r["rotation"] = rot
                    nom_fav = m["exterieur"] if code == "2" else m["domicile"]
                    r["garde_fou"].append(("ℹ️", f"{nom_fav} alignait {rot['communs']}/11 titulaires du dernier "
                                                 f"match de championnat ({rot['reference']})"))

        # actu et compos : seulement pour ce qui reste à jouer
        if r["etat"] in ("a_venir", "en_cours"):
            if avec_actu:
                for cote_eq, tid, nom in (("dom", m["id_domicile"], m["domicile"]),
                                          ("ext", m["id_exterieur"], m["exterieur"])):
                    for al in pe.alertes_actu(m["ligue"], tid, nom):
                        r["alertes"].append((nom, al))
                if r["alertes"]:
                    r["garde_fou"].append(("⚠️", f"{len(r['alertes'])} article(s) ESPN récents parlent de blessure/"
                                                 f"suspension/rotation — à lire"))
                else:
                    r["garde_fou"].append(("✅", "aucune alerte blessure/suspension dans l'actu ESPN des 7 derniers jours"))
            minutes_avant = (m["debut_utc"] - maintenant).total_seconds() / 60
            if minutes_avant <= 80:
                xi = titulaires(m["ligue"], m["id"])
                if xi:
                    r["garde_fou"].append(("✅", "compositions officielles publiées"))
                    favori = m["id_exterieur"] if code in ("2", "N2") else m["id_domicile"]
                    nom_fav = m["exterieur"] if code in ("2", "N2") else m["domicile"]
                    if code in ("1", "2", "1N", "N2") and str(favori) in xi:
                        rot = analyse_rotation(m, favori, xi[str(favori)])
                        r["rotation"] = rot
                        if rot:
                            c = rot["communs"]
                            niveau = ("✅ équipe type" if c >= 8 else
                                      "⚠️ rotation partielle" if c >= 6 else "⚠️ FORTE ROTATION")
                            r["garde_fou"].append(("✅" if c >= 8 else "⚠️",
                                                   f"{nom_fav} : {c}/11 titulaires du dernier match de "
                                                   f"championnat ({rot['reference']}) → {niveau}"))
                else:
                    r["garde_fou"].append(("ℹ️", "compositions pas encore publiées"))
            elif m["ligue"].startswith("uefa."):
                r["garde_fou"].append(("⚠️", "Coupe d'Europe : les favoris font souvent tourner — ne miser qu'après la "
                                             "compo officielle (~1 h avant), voir LECONS.md (leçon D)"))
            else:
                r["garde_fou"].append(("ℹ️", "compositions publiées ~1 h avant le coup d'envoi : relancer l'analyse à ce moment-là"))
    sauver_snapshot(snap)
    return lignes_res


def rapport(ticket, res):
    L = []
    mise = ticket.get("mise")
    dev = ticket.get("devise", "")
    gains = ticket.get("gains_potentiels") or (mise * ticket["cote_totale"] if mise else None)
    L.append(f"Analyse du ticket — {ticket.get('description', '')}")
    L.append(f"{len(res)} sélections — cote {ticket['cote_totale']:.3f}"
             + (f" — mise {mise:,.0f} {dev} — gains potentiels {gains:,.2f} {dev}".replace(",", " ") if mise else ""))
    L.append(f"Cotes de référence : DraftKings via ESPN, relevées le "
             f"{pe.vers_paris(pe.maintenant_utc()).strftime('%d/%m/%Y à %H:%M')} (heure de Paris)")
    L.append("Probabilité réelle = cote du marché, marge du bookmaker retirée. Aucun modèle maison.")
    L.append("")
    icones = {"gagne": "✅ GAGNÉ", "perdu": "❌ PERDU", "en_cours": "🔴 EN COURS", "a_venir": "⏳ À VENIR",
              "annule": "⚪ NON JOUÉ", "introuvable": "❓ INTROUVABLE", "rembourse": "↩️ REMBOURSÉ (cote 1,00)",
              "non_reconnu": "❓ PARI NON RECONNU"}
    for r in res:
        sel, m = r["sel"], r["match"]
        nom = f"{m['domicile']} - {m['exterieur']}" if m else f"{sel['domicile']} - {sel['exterieur']}"
        comp = m["competition"] if m else pe.LIGUES.get(sel["ligue"], sel["ligue"])
        quand = pe.fmt_heure(m) if m else sel["date"]
        lib = pe.libelle_pari(r["code"], m) if m else sel["pari"]
        L.append(f"{r['n']}. {nom}  ({comp}, {quand})")
        etat = icones.get(r["etat"], r["etat"])
        sc = f" — score {m['score'][0]}-{m['score'][1]}" if m and m["etat"] in ("in", "post") and m["score"][0] is not None else ""
        if r["etat"] == "en_cours":
            chrono = m.get("statut") if m.get("deux_issues") else (m.get("chrono") or m.get("statut"))
            sc += f" ({chrono}, " + ("passe pour l'instant" if r.get("passe_actuellement") else "ne passe pas pour l'instant") + ")"
        elif m and m.get("deux_issues") and m["termine"] and m["statut"] and m["statut"] != "Final":
            sc += f" ({pe.statut_fin_fr(m['statut'])})"
        L.append(f"   Pari : {lib} @ {sel['cote']:.3f}  →  {etat}{sc}")
        if r["etat"] in ("a_venir", "en_cours"):
            txt = f"   Probabilité réelle : {pe.fmt_pct(r['p_depart'])} ({r.get('source', '')})"
            if r["etat"] == "en_cours" and r["p_maintenant"] is not None:
                txt += f"  |  en direct : {pe.fmt_pct(r['p_maintenant'])}"
                if r.get("modele_direct"):
                    txt += f" (estimation : {r['modele_direct']})"
            L.append(txt)
            if r.get("marche_direct") is not None:
                L.append(f"   Marché en direct (DraftKings via ESPN, peut avoir quelques minutes de retard) : "
                         f"{pe.fmt_pct(r['marche_direct'])}")
            if r.get("cote_marche"):
                v = r["valeur"]
                L.append(f"   Ta cote {sel['cote']:.3f} vs marché {r['cote_marche']:.2f} → valeur "
                         f"{'+' if v >= 0 else ''}{100 * v:.1f} % (positif = ta cote paie plus que le risque réel)")
            if r.get("cote_ouverture") and r.get("cote_marche") and not (m and m.get("deux_issues")):
                # (sports US : le mouvement, cote et ligne, est dans le garde-fou)
                o, c = r["cote_ouverture"], r["cote_marche"]
                sens = ("le marché croit PLUS en ce pari" if c < o - 0.005 else
                        "le marché croit MOINS en ce pari" if c > o + 0.005 else "stable")
                L.append(f"   Mouvement du marché : ouverture {o:.2f} → maintenant {c:.2f} ({sens})")
            if m and (m["forme"][0] or m["forme"][1]):
                L.append(f"   Forme (5 derniers) : {m['domicile']} {m['forme'][0] or '?'} | {m['exterieur']} {m['forme'][1] or '?'}")
            elif m and m.get("deux_issues") and (m["bilan"][0] or m["bilan"][1]):
                L.append(f"   Bilan de la saison : {m['domicile']} {m['bilan'][0] or '?'} | {m['exterieur']} {m['bilan'][1] or '?'}")
        for ic, txt in r["garde_fou"]:
            L.append(f"   {ic} {txt}")
        for nom_eq, al in r["alertes"][:4]:
            L.append(f"      • [{nom_eq}] {al['date']} « {al['titre']} » {al['lien']}")
        if r.get("rotation") and r["rotation"]["absents_vs_reference"]:
            L.append(f"      Titulaires du dernier match de championnat absents du onze : "
                     + ", ".join(r["rotation"]["absents_vs_reference"]))
        L.append("")

    gagnes = [r for r in res if r["etat"] == "gagne"]
    perdus = [r for r in res if r["etat"] == "perdu"]
    rembourses = [r for r in res if r["etat"] in ("rembourse", "annule")]
    restants = [r for r in res if r["etat"] in ("a_venir", "en_cours", "introuvable", "non_reconnu")]
    p_maint = 0.0 if perdus else math.prod(r["p_maintenant"] for r in restants if r["p_maintenant"] is not None)
    p_dep = math.prod(r["p_depart"] for r in res if r.get("p_depart"))
    cote_rest = math.prod(r["sel"]["cote"] for r in restants)
    L.append("BILAN DU TICKET")
    L.append(f"   {len(gagnes)} gagné(s), {len(perdus)} perdu(s), {len(restants)} à jouer")
    if rembourses:
        c_remb = math.prod(r["sel"]["cote"] for r in rembourses)
        L.append(f"   ↩️ {len(rembourses)} sélection(s) remboursée(s) ou non jouée(s), comptée(s) à 1,00 : cote du ticket "
                 f"recalculée {ticket['cote_totale'] / c_remb:.3f}")
        if gains:
            gains = gains / c_remb
    for r in res:
        rot = r.get("rotation")
        if rot and rot["communs"] <= 5:
            fav = r["match"]["exterieur"] if r["code"] in ("2", "N2") else r["match"]["domicile"]
            L.append(f"   ⚠️ Forte rotation : {fav} n'aligne que {rot['communs']}/11 titulaires habituels"
                     f" ({'sélection perdue' if r['etat'] == 'perdu' else 'risque élevé'})")
    if perdus:
        L.append("   ❌ Ticket perdu.")
    else:
        if restants:
            L.append(f"   Cote restante : {cote_rest:.2f}")
            L.append(f"   Probabilité que le ticket passe : {100 * p_maint:.1f} % (≈ 1 fois sur {1 / p_maint:.0f})"
                     if p_maint > 0 else "   Probabilité que le ticket passe : ~0 %")
            if gains:
                juste = gains * p_maint
                L.append(f"   Valeur juste du ticket maintenant : {juste:,.0f} {dev} (gains × probabilité)".replace(",", " "))
                L.append("   → Si ton bookmaker propose un cash-out, compare-le à cette valeur juste : en dessous,"
                         " il te rachète le ticket moins cher que ce qu'il vaut réellement.")
            faibles = sorted([r for r in restants if r.get("p_maintenant")], key=lambda r: r["p_maintenant"])
            if faibles:
                f = faibles[0]
                nm = f"{f['match']['domicile']} - {f['match']['exterieur']}" if f["match"] else f["sel"]["domicile"]
                L.append(f"   Maillon faible : {nm} ({pe.libelle_pari(f['code'], f['match']) if f['match'] else f['sel']['pari']},"
                         f" {pe.fmt_pct(f['p_maintenant'])})")
        else:
            L.append("   ✅ Ticket GAGNANT !")
    L.append(f"   Probabilité du ticket au moment du pari ({len(res)} sélections, estimation) : {100 * p_dep:.2f} %"
             f" (≈ 1 fois sur {1 / p_dep:.0f})" if p_dep else "")
    vals = [r["valeur"] for r in res if r.get("valeur") is not None and r.get("cote_marche")]
    if vals:
        L.append(f"   Valeur moyenne de tes cotes face au marché : {100 * sum(vals) / len(vals):+.1f} % par sélection")
    return "\n".join(x for x in L if x is not None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticket", help="fichier JSON du ticket (voir tickets/)")
    ap.add_argument("--sans-actu", action="store_true", help="ne pas lire l'actualité ESPN")
    ap.add_argument("--mise", type=float, default=None,
                    help="mise (non enregistrée dans le dépôt, qui est public)")
    ap.add_argument("--gains", type=float, default=None, help="gains potentiels affichés par le bookmaker")
    ap.add_argument("--devise", default=None, help="devise des montants (ex. EUR, DZD)")
    args = ap.parse_args()
    with open(args.ticket, encoding="utf-8") as fh:
        ticket = json.load(fh)
    if args.mise is not None:
        ticket["mise"] = args.mise
    if args.gains is not None:
        ticket["gains_potentiels"] = args.gains
    if args.devise:
        ticket["devise"] = args.devise
    res = analyser(ticket, avec_actu=not args.sans_actu)
    txt = rapport(ticket, res)
    print(txt)
    base = os.path.splitext(os.path.basename(args.ticket))[0]
    chemin = os.path.join(os.path.dirname(args.ticket) or ".", f"analyse_{base}.md")
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write("```\n" + txt + "\n```\n")
    print(f"\nAnalyse écrite : {chemin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
