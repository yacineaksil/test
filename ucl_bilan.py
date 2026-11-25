#!/usr/bin/env python3
"""Bilan d'une journée de Ligue des champions : pronostics contre résultats réels.

Étape 5 du processus : résultats -> pronostics -> vérification -> simulation -> BILAN.

Compare `pronostics/pronos_AAAA-MM-JJ.json` à `resultats/ucl_AAAA-MM-JJ.json` (lancé via
ucl_bot.py s'il manque) et produit :
  - un bilan en français dans la console et dans `bilans/bilan_AAAA-MM-JJ.md` :
    1N2 (probabilités publiées contre marché seul et modèle statistique seul, score de Brier),
    scores exacts, buts prévus / réels, calibration +2,5 buts et « les deux équipes marquent »,
    buteurs (prévus qui ont marqué, buteurs réels non prévus, penaltys), corrections du
    vérificateur confirmées ou non par les compositions réelles ;
  - une ligne par match ajoutée à `historique/historique.csv` et une ligne par joueur
    (listé, sur le banc ou buteur non prévu) dans `historique/buteurs.csv`, pour que la
    calibration s'accumule de journée en journée (relancer le bilan d'une date remplace
    ses lignes au lieu de les dupliquer) ;
  - un court résumé automatique ajouté à la fin de `LECONS.md` (remplacé s'il existe déjà).

Bibliothèque standard uniquement.
    python3 ucl_bilan.py --date 2026-11-25
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

import ucl_bot
from ucl_bot import date_longue, vers_paris
from ucl_predict import match_condense, matrice_scores, stats_joueurs_saison, stats_matrice
from ucl_verif import meme_joueur, normaliser

ICI = os.path.dirname(os.path.abspath(__file__))
DOSSIER_PRONOS = os.path.join(ICI, "pronostics")
DOSSIER_RESULTATS = os.path.join(ICI, "resultats")
DOSSIER_BILANS = os.path.join(ICI, "bilans")
HISTORIQUE = os.path.join(ICI, "historique", "historique.csv")
HISTORIQUE_BUTEURS = os.path.join(ICI, "historique", "buteurs.csv")
LECONS = os.path.join(ICI, "LECONS.md")

COLONNES = ["date", "id", "match", "source_probas",
            "p1_publie", "pn_publie", "p2_publie", "p1_marche", "pn_marche", "p2_marche",
            "p1_stats", "pn_stats", "p2_stats", "prono_publie", "prono_marche", "issue",
            "score_reel", "score_prevu", "score_exact_ok",
            "buts_att_dom_publie", "buts_att_ext_publie", "buts_att_dom_marche", "buts_att_ext_marche",
            "buts_att_dom_stats", "buts_att_ext_stats", "buts_dom", "buts_ext",
            "p_plus25_publie", "p_plus25_marche", "plus25_reel", "p_btts_publie", "p_btts_marche", "btts_reel",
            "brier_publie", "brier_marche", "brier_stats",
            "buteurs_listes", "buteurs_listes_ayant_marque", "buteurs_reels", "penaltys"]
COLONNES_BUTEURS = ["date", "id", "match", "equipe", "joueur", "id_joueur", "liste", "statut_compo",
                    "entre_en_jeu", "proba", "a_marque", "buts", "dont_penalty", "tireur_penalty",
                    "buts_saison_avant", "buts_c1_avant"]

VRAI = {"1": (1, 0, 0), "N": (0, 1, 0), "2": (0, 0, 1)}


def pct(x):
    return "–" if x is None else f"{100 * x:.0f} %"


def _f3(x):
    return "–" if x is None else f"{x:.3f}"


def _f1(x):
    return "–" if x is None else f"{x:.1f}"


def brier(p, issue):
    return sum((a - b) ** 2 for a, b in zip(p, VRAI[issue]))


def choix(p):
    return "1N2"[max(range(3), key=lambda i: p[i])]


# --------------------------------------------------------------------------
# Chargement
# --------------------------------------------------------------------------
def charger(jour):
    chemin_p = os.path.join(DOSSIER_PRONOS, f"pronos_{jour.isoformat()}.json")
    if not os.path.exists(chemin_p):
        return None, None
    with open(chemin_p, encoding="utf-8") as f:
        pronos = json.load(f)
    chemin_r = os.path.join(DOSSIER_RESULTATS, f"ucl_{jour.isoformat()}.json")
    if not os.path.exists(chemin_r):
        print(f"Résultats absents : lancement de ucl_bot.py pour le {jour.isoformat()}…", file=sys.stderr)
        ucl_bot.main(["--date", jour.isoformat(), "--out", DOSSIER_RESULTATS])
    with open(chemin_r, encoding="utf-8") as f:
        resultats = json.load(f)
    return pronos, resultats


# --------------------------------------------------------------------------
# Analyse
# --------------------------------------------------------------------------
def analyser(jour, pronos, resultats, aujourdhui):
    par_id = {m["id"]: m for m in resultats["matchs"]}
    matchs, ignores = [], []
    for p in pronos["pronostics"]:
        r = par_id.get(p["id"])
        if not r or r.get("etat") != "post" or r["domicile"]["score"] is None:
            ignores.append(f"{p['domicile']} - {p['exterieur']}")
            continue
        matchs.append((p, r))
    # stats des joueurs avant le match (toutes compétitions) : pour expliquer les buteurs non prévus
    ids_eq = sorted({str(p[f"id_{c}"]) for p, _ in matchs for c in ("domicile", "exterieur")})
    try:
        saison = stats_joueurs_saison(ids_eq, jour, aujourdhui)
    except RuntimeError:
        saison = {}

    lignes = []
    for p, r in matchs:
        bd, be = r["domicile"]["score"], r["exterieur"]["score"]
        issue = "1" if bd > be else ("2" if bd < be else "N")
        cond = match_condense("uefa.champions", p["id"]) or {"buts": [], "compo": {}, "penaltys": []}
        pub = p["proba_1n2"]
        mk = (p.get("cotes") or {}).get("proba_1n2")
        ls = p.get("buts_attendus_stats")
        st_stats = stats_matrice(matrice_scores(*ls)) if ls else None
        pst = [st_stats["p1"], st_stats["pn"], st_stats["p2"]] if st_stats else None
        lm = p.get("buts_attendus_cotes")
        st_mk = stats_matrice(matrice_scores(*lm)) if lm else None
        source = p.get("source_probas") or ("melange" if mk else "stats")
        l = {"p": p, "r": r, "bd": bd, "be": be, "issue": issue, "source": source,
             "pub": pub, "mk": mk, "stats": pst,
             "prono_pub": p["pronostic_1n2"], "prono_mk": choix(mk) if mk else None,
             "brier_pub": brier(pub, issue), "brier_mk": brier(mk, issue) if mk else None,
             "brier_stats": brier(pst, issue) if pst else None,
             "score_ok": p["score_probable"] == f"{bd}-{be}",
             "lam_pub": p["buts_attendus"], "lam_mk": lm, "lam_stats": ls,
             "over_pub": p["plus_2_5"], "over_mk": st_mk["over"][2.5] if st_mk else None,
             "btts_pub": p["deux_equipes_marquent"], "btts_mk": st_mk["btts"] if st_mk else None,
             "over": bd + be > 2.5, "btts": bd > 0 and be > 0,
             "camps": {}}
        for camp in ("domicile", "exterieur"):
            tid = str(p[f"id_{camp}"])
            compo = {c["jid"]: c for c in cond["compo"].get(tid) or []}
            compo_noms = list(compo.values())

            def statut(nom, jid=None):
                c = compo.get(str(jid)) if jid else None
                if c is None:
                    c = next((x for x in compo_noms if meme_joueur(x["nom"], nom)), None)
                if c is None:
                    return ("absent du groupe" if compo else "inconnu"), False
                if c["titulaire"]:
                    return "titulaire", True
                return ("remplaçant entré" if c["entre"] else "remplaçant non entré"), c["entre"]

            reels = [b for b in r["buts"] if str(b["equipe_id"]) == tid and b["type"] != "csc"]
            ids_reels = {}
            for b in cond["buts"]:
                if b["tid"] == tid:
                    ids_reels.setdefault(b["nom"], b["jid"])
            buteurs_reels = {}
            for b in reels:
                d = buteurs_reels.setdefault(b["buteur"], {"buts": 0, "penalty": 0,
                                                           "id": ids_reels.get(b["buteur"])})
                d["buts"] += 1
                d["penalty"] += b["type"] == "penalty"

            def a_marque(nom, jid=None):
                for n, d in buteurs_reels.items():
                    if (jid and d["id"] and str(jid) == str(d["id"])) or meme_joueur(n, nom):
                        return n, d
                return None, None

            eq = saison.get(tid) or {"joueurs": {}}

            def avant(nom, jid):
                j = eq["joueurs"].get(str(jid)) if jid else None
                if j is None:
                    j = next((x for x in eq["joueurs"].values() if meme_joueur(x["nom"], nom)), None)
                return (j["buts"], j["buts_c1"], j["penaltys"]) if j else (0, 0, 0)

            listes = []
            for liste, cle in (("oui", f"buteurs_{camp}"), ("banc", f"banc_{camp}")):
                for b in p.get(cle) or []:
                    n, d = a_marque(b["joueur"], b.get("id"))
                    st, entre = statut(b["joueur"], b.get("id"))
                    listes.append({"joueur": b["joueur"], "id": b.get("id"), "liste": liste,
                                   "proba": b["proba_buteur"], "dispo": b.get("disponibilite"),
                                   "tireur": bool(b.get("tireur_penalty")), "statut": st, "entre": entre,
                                   "buts": d["buts"] if d else 0, "penalty": d["penalty"] if d else 0,
                                   "avant": avant(b["joueur"], b.get("id"))})
            non_prevus = []
            for n, d in buteurs_reels.items():
                if any(x["liste"] == "oui" and ((x["id"] and d["id"] and str(x["id"]) == str(d["id"]))
                                                 or meme_joueur(x["joueur"], n)) for x in listes):
                    continue
                st, entre = statut(n, d["id"])
                non_prevus.append({"joueur": n, "id": d["id"], "buts": d["buts"], "penalty": d["penalty"],
                                   "statut": st, "avant": avant(n, d["id"]),
                                   "sur_banc_liste": any(x["liste"] == "banc" and meme_joueur(x["joueur"], n)
                                                         for x in listes)})
            # corrections du vérificateur : joueurs retirés (absents) ont-ils joué ?
            absents = []
            for a in p.get(f"absents_{camp}") or []:
                st, entre = statut(a["joueur"])
                absents.append({"joueur": a["joueur"], "raison": a.get("raison"), "origine": a.get("origine"),
                                "statut": st, "a_joue": st == "titulaire" or entre})
            l["camps"][camp] = {"nom": p[camp], "tid": tid, "listes": listes, "non_prevus": non_prevus,
                                "absents": absents, "penaltys_manques": [
                                    x for x in r.get("penaltys_manques") or [] if x["equipe"] == p[camp]]}
        lignes.append(l)
    return lignes, ignores


def agreger(lignes):
    n = len(lignes)
    a = {"n": n}
    if not n:
        return a
    a["justes_pub"] = sum(l["prono_pub"] == l["issue"] for l in lignes)
    a["brier_pub"] = sum(l["brier_pub"] for l in lignes) / n
    avec_mk = [l for l in lignes if l["mk"]]
    a["n_mk"] = len(avec_mk)
    if avec_mk:
        a["justes_mk"] = sum(l["prono_mk"] == l["issue"] for l in avec_mk)
        a["brier_mk"] = sum(l["brier_mk"] for l in avec_mk) / len(avec_mk)
        a["buts_mk"] = sum(sum(l["lam_mk"]) for l in avec_mk)
        a["over_mk"] = sum(l["over_mk"] for l in avec_mk) / len(avec_mk)
        a["btts_mk"] = sum(l["btts_mk"] for l in avec_mk) / len(avec_mk)
    avec_st = [l for l in lignes if l["stats"]]
    if avec_st:
        a["justes_stats"] = sum(choix(l["stats"]) == l["issue"] for l in avec_st)
        a["brier_stats"] = sum(l["brier_stats"] for l in avec_st) / len(avec_st)
        a["buts_stats"] = sum(sum(l["lam_stats"]) for l in avec_st)
    a["scores_exacts"] = sum(l["score_ok"] for l in lignes)
    a["buts_pub"] = sum(sum(l["lam_pub"]) for l in lignes)
    a["buts_reels"] = sum(l["bd"] + l["be"] for l in lignes)
    a["over_pub"] = sum(l["over_pub"] for l in lignes) / n
    a["over_reel"] = sum(l["over"] for l in lignes)
    a["over_brier"] = sum((l["over_pub"] - l["over"]) ** 2 for l in lignes) / n
    a["btts_pub"] = sum(l["btts_pub"] for l in lignes) / n
    a["btts_reel"] = sum(l["btts"] for l in lignes)
    a["btts_brier"] = sum((l["btts_pub"] - l["btts"]) ** 2 for l in lignes) / n
    listes = [x for l in lignes for c in l["camps"].values() for x in c["listes"] if x["liste"] == "oui"]
    a["listes"] = len(listes)
    a["listes_marque"] = sum(1 for x in listes if x["buts"])
    a["listes_attendu"] = sum(x["proba"] for x in listes)
    a["buts_hors_csc"] = sum(sum(x["buts"] for x in c["listes"] if x["liste"] == "oui")
                             + sum(x["buts"] for x in c["non_prevus"])
                             for l in lignes for c in l["camps"].values())
    a["buts_par_listes"] = sum(x["buts"] for x in listes)
    a["penaltys_marques"] = sum(x["penalty"] for l in lignes for c in l["camps"].values()
                                for x in c["listes"] + c["non_prevus"] if x.get("liste", "oui") != "banc")
    absents = [x for l in lignes for c in l["camps"].values() for x in c["absents"]]
    a["absents"] = len(absents)
    a["absents_confirmes"] = sum(1 for x in absents if not x["a_joue"] and x["statut"] != "inconnu")
    return a


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------
LIB_SOURCE = {"cotes": "cotes seules", "melange": "mélange 70 % cotes / 30 % stats", "stats": "modèle statistique"}


def rapport_markdown(jour, lignes, a, ignores):
    genere = vers_paris(datetime.now(timezone.utc)).strftime("%d/%m/%Y à %Hh%M")
    sources = sorted({l["source"] for l in lignes})
    md = [f"# Bilan Ligue des champions — {date_longue(jour)}", "",
          f"_Généré le {genere} (heure de Paris) par `ucl_bilan.py` : `pronostics/pronos_{jour.isoformat()}.json` "
          f"contre `resultats/ucl_{jour.isoformat()}.json`. Probabilités publiées ce jour-là : "
          f"{', '.join(LIB_SOURCE.get(s, s) for s in sources)}._", ""]
    if not lignes:
        md.append("Aucun match terminé à comparer.")
        return "\n".join(md) + "\n"
    n = a["n"]
    md += ["## En bref", "",
           "| Indicateur | Publié | Marché (cotes seules) | Modèle stat. seul | Réel |", "|---|:-:|:-:|:-:|:-:|",
           f"| 1N2 justes | {a['justes_pub']}/{n} | {a.get('justes_mk', '–')}/{a.get('n_mk', 0)} | "
           f"{a.get('justes_stats', '–')}/{n} | |",
           f"| Brier 1N2 (plus bas = mieux) | {a['brier_pub']:.3f} | "
           f"{_f3(a.get('brier_mk'))} | {_f3(a.get('brier_stats'))} | |",
           f"| Buts (total) | {a['buts_pub']:.1f} | {_f1(a.get('buts_mk'))} | {_f1(a.get('buts_stats'))} | "
           f"{a['buts_reels']} |",
           f"| +2,5 buts (moyenne / fréquence) | {pct(a['over_pub'])} | {pct(a.get('over_mk'))} | | "
           f"{a['over_reel']}/{n} ({pct(a['over_reel'] / n)}) |",
           f"| Les deux équipes marquent | {pct(a['btts_pub'])} | {pct(a.get('btts_mk'))} | | "
           f"{a['btts_reel']}/{n} ({pct(a['btts_reel'] / n)}) |",
           f"| Scores exacts | {a['scores_exacts']}/{n} | | | |",
           f"| Buteurs listés ayant marqué | {a['listes_marque']}/{a['listes']} (attendu "
           f"{a['listes_attendu']:.1f}) | | | |",
           f"| Buts (hors csc) marqués par un joueur listé | {a['buts_par_listes']}/{a['buts_hors_csc']} | | | |",
           f"| Absents retirés par le vérificateur qui n'ont pas joué | {a['absents_confirmes']}/{a['absents']} "
           "| | | |", "",
           f"Brier +2,5 buts : {a['over_brier']:.3f} ; Brier BTTS : {a['btts_brier']:.3f}.", ""]
    if ignores:
        md += [f"_Non comparés (pas terminés ou absents des résultats) : {', '.join(ignores)}._", ""]
    md += ["## Match par match", "",
           "| Match | Publié 1/N/2 | Marché 1/N/2 | Prono publié | Prono marché | Score prévu | Réel | "
           "Buts prévus (publié / marché) | +2,5 | BTTS |",
           "|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|"]
    for l in lignes:
        p = l["p"]

        def ok(x):
            return "✅" if x == l["issue"] else "❌"
        mk = " / ".join(pct(x) for x in l["mk"]) if l["mk"] else "–"
        prono_mk = f"{l['prono_mk']} {ok(l['prono_mk'])}" if l["prono_mk"] else "–"
        buts_mk = f"{sum(l['lam_mk']):.1f}" if l["lam_mk"] else "–"
        md.append(f"| {p['domicile']} - {p['exterieur']} | {' / '.join(pct(x) for x in l['pub'])} | {mk} | "
                  f"{l['prono_pub']} {ok(l['prono_pub'])} | {prono_mk} | "
                  f"{p['score_probable']}{' ✅' if l['score_ok'] else ''} | **{l['bd']}-{l['be']}** | "
                  f"{sum(l['lam_pub']):.1f} / {buts_mk} | "
                  f"{pct(l['over_pub'])} {'✅' if l['over'] else '❌'} | "
                  f"{pct(l['btts_pub'])} {'✅' if l['btts'] else '❌'} |")
    md += ["", "_+2,5 / BTTS : probabilité publiée, ✅ si c'est arrivé._", "", "## Buteurs", ""]
    md += ["### Buteurs probables listés", ""]
    for l in lignes:
        for c in l["camps"].values():
            lst = [x for x in c["listes"] if x["liste"] == "oui"]
            if not lst:
                continue
            md.append(f"- **{c['nom']}** : " + ", ".join(
                f"{x['joueur']} (~{pct(x['proba'])}, {x['statut']}"
                + (", tireur de penalty" if x["tireur"] else "") + ") "
                + (f"⚽ x{x['buts']}" + (" (dont penalty)" if x["penalty"] else "") if x["buts"] else "❌")
                for x in lst))
    banc = [(c["nom"], x) for l in lignes for c in l["camps"].values() for x in c["listes"] if x["liste"] == "banc"]
    if banc:
        md += ["", "### Joueurs signalés « sur le banc »", ""]
        md += [f"- {x['joueur']} ({nom}, ~{pct(x['proba'])}, {x['statut']}) "
               + (f"⚽ x{x['buts']}" if x["buts"] else "❌") for nom, x in banc]
    md += ["", "### Buteurs réels non listés", ""]
    rien = True
    for l in lignes:
        for c in l["camps"].values():
            for x in c["non_prevus"]:
                rien = False
                g, g1, pen = x["avant"]
                md.append(f"- {x['joueur']} ({c['nom']}) : {x['buts']} but{'s' if x['buts'] > 1 else ''}"
                          + (f" dont {x['penalty']} sur penalty" if x["penalty"] else "")
                          + f" — {x['statut']} ; avant le match : {g} but{'s' if g > 1 else ''} cette saison "
                          f"toutes compétitions, dont {g1} en C1"
                          + (f", {pen} penalty(s) tiré(s)" if pen else "")
                          + (" (était dans la ligne « sur le banc »)" if x["sur_banc_liste"] else ""))
    if rien:
        md.append("_Aucun : tous les buteurs étaient listés._")
    pens = [(c["nom"], x["joueur"]) for l in lignes for c in l["camps"].values()
            for x in c["listes"] + c["non_prevus"] if x["penalty"] and x.get("liste", "oui") == "oui"]
    manques = [(c["nom"], x) for l in lignes for c in l["camps"].values() for x in c["penaltys_manques"]]
    md += ["", "### Penaltys", ""]
    md.append(f"- Penaltys transformés : {len(pens)} ({', '.join(f'{j} ({e})' for e, j in pens) or 'aucun'})")
    if manques:
        md.append("- Penaltys manqués / arrêtés : " + ", ".join(
            f"{x['joueur']} ({e}, {x['issue']})" for e, x in manques))
    md += ["", "## Corrections du vérificateur", ""]
    rien = True
    for l in lignes:
        for c in l["camps"].values():
            for x in c["absents"]:
                rien = False
                verdict = ("✅ confirmé (n'a pas joué)" if not x["a_joue"] and x["statut"] != "inconnu"
                           else ("❔ compo inconnue" if x["statut"] == "inconnu" else f"❌ a joué ({x['statut']})"))
                md.append(f"- {x['joueur']} ({c['nom']}) retiré — {x['raison'] or 'absent'} : {verdict}")
    if rien:
        md.append("_Aucun joueur retiré._")
    md += ["", "## Résumé automatique", "", resume_auto(jour, a), ""]
    return "\n".join(md) + "\n"


def resume_auto(jour, a):
    if not a.get("n"):
        return f"- {jour.isoformat()} : aucun match comparé."
    n = a["n"]
    s = [f"- 1N2 : publié {a['justes_pub']}/{n} (Brier {a['brier_pub']:.3f})"
         + (f", marché seul {a['justes_mk']}/{a['n_mk']} (Brier {a['brier_mk']:.3f})" if a.get("n_mk") else "")
         + (f", stats seules {a['justes_stats']}/{n} (Brier {a['brier_stats']:.3f})" if "brier_stats" in a else "")
         + ".",
         f"- Buts : {a['buts_pub']:.1f} prévus (marché {_f1(a.get('buts_mk'))}) pour {a['buts_reels']} marqués ; "
         f"+2,5 prévu {pct(a['over_pub'])} / réel {a['over_reel']}/{n} ; BTTS prévu {pct(a['btts_pub'])} / "
         f"réel {a['btts_reel']}/{n}.",
         f"- Buteurs : {a['listes_marque']}/{a['listes']} listés ont marqué (attendu {a['listes_attendu']:.1f}) ; "
         f"{a['buts_par_listes']}/{a['buts_hors_csc']} buts (hors csc) par un joueur listé.",
         f"- Vérificateur : {a['absents_confirmes']}/{a['absents']} absents retirés n'ont effectivement pas joué."]
    if a.get("n_mk") and a["brier_mk"] < a["brier_pub"] - 1e-9:
        s.append("- ⚠️ Le marché seul a fait mieux que les probabilités publiées (Brier).")
    if a["buts_pub"] - a["buts_reels"] > 0.25 * max(a["buts_reels"], 1):
        s.append("- ⚠️ Nombre de buts surestimé de plus de 25 %.")
    return "\n".join(s)


def rapport_texte(jour, lignes, a, ignores):
    """Version console : le Markdown sans la syntaxe des tableaux les plus larges."""
    return rapport_markdown(jour, lignes, a, ignores).replace("**", "")


# --------------------------------------------------------------------------
# Historique et leçons
# --------------------------------------------------------------------------
def _ecrire_csv(chemin, colonnes, jour, nouvelles):
    """Remplace les lignes de `jour` (relance idempotente) et ajoute les nouvelles."""
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    anciennes = []
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8", newline="") as f:
            anciennes = [r for r in csv.DictReader(f) if r.get("date") != jour.isoformat()]
    with open(chemin, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=colonnes, extrasaction="ignore")
        w.writeheader()
        for r in anciennes + nouvelles:
            w.writerow(r)
    return len(nouvelles)


def _r(x, k=3):
    return "" if x is None else round(x, k)


def lignes_historique(jour, lignes):
    res = []
    for l in lignes:
        p = l["p"]
        mk, st = l["mk"] or [None] * 3, l["stats"] or [None] * 3
        lm, ls = l["lam_mk"] or [None, None], l["lam_stats"] or [None, None]
        listes = [x for c in l["camps"].values() for x in c["listes"] if x["liste"] == "oui"]
        reels = []
        for c in l["camps"].values():
            for x in [y for y in c["listes"] if y["liste"] == "oui" and y["buts"]] + c["non_prevus"]:
                reels.append(f"{x['joueur']}x{x['buts']}" if x["buts"] > 1 else x["joueur"])
        res.append({
            "date": jour.isoformat(), "id": p["id"], "match": f"{p['domicile']} - {p['exterieur']}",
            "source_probas": l["source"],
            "p1_publie": l["pub"][0], "pn_publie": l["pub"][1], "p2_publie": l["pub"][2],
            "p1_marche": mk[0], "pn_marche": mk[1], "p2_marche": mk[2],
            "p1_stats": _r(st[0]), "pn_stats": _r(st[1]), "p2_stats": _r(st[2]),
            "prono_publie": l["prono_pub"], "prono_marche": l["prono_mk"] or "", "issue": l["issue"],
            "score_reel": f"{l['bd']}-{l['be']}", "score_prevu": p["score_probable"],
            "score_exact_ok": int(l["score_ok"]),
            "buts_att_dom_publie": l["lam_pub"][0], "buts_att_ext_publie": l["lam_pub"][1],
            "buts_att_dom_marche": _r(lm[0], 2), "buts_att_ext_marche": _r(lm[1], 2),
            "buts_att_dom_stats": _r(ls[0], 2), "buts_att_ext_stats": _r(ls[1], 2),
            "buts_dom": l["bd"], "buts_ext": l["be"],
            "p_plus25_publie": l["over_pub"], "p_plus25_marche": _r(l["over_mk"]), "plus25_reel": int(l["over"]),
            "p_btts_publie": l["btts_pub"], "p_btts_marche": _r(l["btts_mk"]), "btts_reel": int(l["btts"]),
            "brier_publie": _r(l["brier_pub"]), "brier_marche": _r(l["brier_mk"]), "brier_stats": _r(l["brier_stats"]),
            "buteurs_listes": "; ".join(f"{x['joueur']} ({x['proba']:.2f})" for x in listes),
            "buteurs_listes_ayant_marque": "; ".join(x["joueur"] for x in listes if x["buts"]),
            "buteurs_reels": "; ".join(reels),
            "penaltys": sum(x["penalty"] for c in l["camps"].values()
                            for x in c["listes"] + c["non_prevus"] if x.get("liste", "oui") == "oui"),
        })
    return res


def lignes_buteurs(jour, lignes):
    res = []
    for l in lignes:
        p = l["p"]
        for c in l["camps"].values():
            for x in c["listes"] + [dict(y, liste="non", proba=None, tireur=None, entre=None)
                                    for y in c["non_prevus"]]:
                g, g1, _ = x["avant"]
                res.append({"date": jour.isoformat(), "id": p["id"], "match": f"{p['domicile']} - {p['exterieur']}",
                            "equipe": c["nom"], "joueur": x["joueur"], "id_joueur": x.get("id") or "",
                            "liste": x["liste"], "statut_compo": x["statut"],
                            "entre_en_jeu": "" if x.get("entre") is None else int(bool(x["entre"]) or
                                                                                   x["statut"] == "titulaire"),
                            "proba": _r(x["proba"]), "a_marque": int(x["buts"] > 0), "buts": x["buts"],
                            "dont_penalty": x["penalty"], "tireur_penalty": "" if x["tireur"] is None else int(x["tireur"]),
                            "buts_saison_avant": g, "buts_c1_avant": g1})
    return res


def maj_lecons(jour, a, chemin=LECONS):
    debut, fin = f"<!-- bilan-auto:{jour.isoformat()} -->", f"<!-- /bilan-auto:{jour.isoformat()} -->"
    bloc = (f"{debut}\n### Bilan automatique du {jour.strftime('%d/%m/%Y')} (`ucl_bilan.py`)\n\n"
            f"{resume_auto(jour, a)}\n\nDétail : `bilans/bilan_{jour.isoformat()}.md`.\n{fin}\n")
    texte = ""
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            texte = f.read()
    else:
        texte = "# Leçons tirées des bilans\n\n"
    if debut in texte and fin in texte:
        i, j = texte.index(debut), texte.index(fin) + len(fin) + 1
        texte = texte[:i] + bloc + texte[j:]
    else:
        texte = texte.rstrip("\n") + "\n\n" + bloc
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(texte)


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Bilan d'une journée de C1 : pronostics contre résultats.")
    ap.add_argument("--date", help="Date AAAA-MM-JJ (défaut : aujourd'hui, heure de Paris)")
    ap.add_argument("--sans-historique", action="store_true",
                    help="N'écrit ni historique/*.csv ni LECONS.md (bilan seul)")
    args = ap.parse_args(argv)
    aujourdhui = ucl_bot.aujourdhui_paris()
    try:
        jour = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else aujourdhui
    except ValueError:
        ap.error("date invalide, attendu AAAA-MM-JJ")

    pronos, resultats = charger(jour)
    if pronos is None:
        print(f"Pas de pronostics pour le {jour.isoformat()} (pronostics/pronos_{jour.isoformat()}.json) : "
              "bilan impossible.", file=sys.stderr)
        return 1
    try:
        lignes, ignores = analyser(jour, pronos, resultats, aujourdhui)
    except RuntimeError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    a = agreger(lignes)
    md = rapport_markdown(jour, lignes, a, ignores)
    print(rapport_texte(jour, lignes, a, ignores))
    os.makedirs(DOSSIER_BILANS, exist_ok=True)
    chemin = os.path.join(DOSSIER_BILANS, f"bilan_{jour.isoformat()}.md")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Bilan écrit : {chemin}")
    if not args.sans_historique and lignes:
        n = _ecrire_csv(HISTORIQUE, COLONNES, jour, lignes_historique(jour, lignes))
        nb = _ecrire_csv(HISTORIQUE_BUTEURS, COLONNES_BUTEURS, jour, lignes_buteurs(jour, lignes))
        maj_lecons(jour, a)
        print(f"Historique : {n} match(s) dans {HISTORIQUE}, {nb} joueur(s) dans {HISTORIQUE_BUTEURS} ; "
              f"résumé ajouté à {LECONS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
