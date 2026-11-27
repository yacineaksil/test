#!/usr/bin/env python3
"""Construit le ticket combiné le plus probable pour un « niveau » donné
(nombre de sélections, cote totale visée, fourchette de cote par sélection),
sur toutes les compétitions suivies par ESPN : football et sports américains
(NFL, NCAA football, NBA, NCAA basket, NHL, MLB, WNBA).

100 % factuel : cotes DraftKings publiées par ESPN, probabilité = cote du
marché marge retirée. À cote totale égale, le meilleur ticket est celui qui
paie le moins de marge au bookmaker : c'est ce que la recherche maximise.

Exemple (même niveau que le ticket du 25/11 : 7 sélections, cote ~27,5,
favoris entre 1,20 et 2,40) :
  python3 ticket_generer.py --du 2026-11-26 --au 2026-11-30 --selections 7 --cible 27.5
      --exclure tickets/ticket_2026-11-25.json
Sports américains ce soir et demain, vainqueur + handicap + total :
  python3 ticket_generer.py --ligues usa --du 2026-11-27 --au 2026-11-29 --jusqua 2026-11-29T06:00
      --selections 4 --cible 8 --marches ml,handicap,total
"""
import argparse
import json
import math
import os
import sys
from datetime import date, timedelta

import paris_espn as pe
import ticket_analyse as ta


MARCHES = ("1n2", "ml", "handicap", "total")


def options_us(m, marches):
    """Sélections possibles d'un match de sport US : (code, cote, probabilité)."""
    c, p = m["cotes"], m["probas"]
    opts = []
    if ("ml" in marches or "1n2" in marches) and "1" in c:
        opts += [("1", c["1"], p["1"]), ("2", c["2"], p["2"])]
    if "handicap" in marches and c.get("hcp"):
        h = c["hcp"]
        for cote, ligne in (("1", h["ligne"]), ("2", -h["ligne"])):
            code = pe.code_hcp(cote, ligne)
            opts.append((code, h[cote], p[code]))
    if "total" in marches and "ligne" in c:
        ln = c["ligne"]
        opts += [(f"+{ln}", c["plus"], p[f"+{ln}"]), (f"-{ln}", c["moins"], p[f"-{ln}"])]
    return opts


def collecter(ligues, du, au, marge_minutes, avec_buts, min_titulaires=0, jusqua=None, marches=None):
    """Matchs à venir avec cotes et sélections possibles. `marches` : None =
    comportement historique (1N2 en football, vainqueur en sport US, + totaux
    si avec_buts) ; sinon ensemble parmi 1n2 / ml / handicap / total."""
    maintenant = pe.maintenant_utc()
    if marches is None:
        marches = {"1n2", "ml"} | ({"total"} if avec_buts else set())
    matchs, vus = [], set()
    for ligue in ligues:
        deux = pe.est_deux_issues(ligue)
        d = du - timedelta(days=1)
        while d <= au + timedelta(days=1):
            for e in pe.scoreboard(ligue, d).get("events") or []:
                if e["id"] in vus:
                    continue
                vus.add(e["id"])
                m = pe.match_depuis_event(e, ligue)
                if not (du <= m["debut_paris"].date() <= au):
                    continue
                if jusqua is not None and m["debut_paris"] > jusqua:
                    continue
                if deux:
                    if m["etat"] != "pre" or not m["cotes"]:
                        continue
                    if (m["debut_utc"] - maintenant).total_seconds() < marge_minutes * 60:
                        continue
                    m["options"] = options_us(m, marches)
                    if m["options"]:
                        matchs.append(m)
                    continue
                if m["etat"] != "pre" or not m["cotes"] or "1" not in m["cotes"]:
                    continue
                if (m["debut_utc"] - maintenant).total_seconds() < marge_minutes * 60:
                    continue
                opts = []
                if "1n2" in marches or "ml" in marches:
                    for code in ("1", "N", "2"):
                        opts.append((code, m["cotes"][code], m["probas"][code]))
                if "total" in marches and "ligne" in m["cotes"]:
                    ln = m["cotes"]["ligne"]
                    opts.append((f"+{ln}", m["cotes"]["plus"], m["probas"][f"+{ln}"]))
                    opts.append((f"-{ln}", m["cotes"]["moins"], m["probas"][f"-{ln}"]))
                # Leçon D : compo publiée et favori qui fait trop tourner -> sélection écartée
                if min_titulaires and (m["debut_utc"] - maintenant).total_seconds() <= 80 * 60:
                    xi = ta.titulaires(ligue, m["id"])
                    hab = {}
                    for code_eq, tid in (("1", m["id_domicile"]), ("2", m["id_exterieur"])):
                        if str(tid) in xi:
                            rot = ta.analyse_rotation(m, tid, xi[str(tid)])
                            if rot:
                                hab[code_eq] = rot["communs"]
                    m["titulaires_habituels"] = hab
                    opts = [o for o in opts if not (o[0] in hab and hab[o[0]] < min_titulaires)]
                m["options"] = opts
                matchs.append(m)
            d += timedelta(days=1)
    return sorted(matchs, key=lambda m: m["debut_utc"])


def ids_du_ticket(chemin):
    if not chemin:
        return set()
    with open(chemin, encoding="utf-8") as fh:
        t = json.load(fh)
    ids = set()
    for s in t["selections"]:
        m, _ = pe.trouver_match(s["ligue"], date.fromisoformat(s["date"]), s["domicile"], s["exterieur"])
        if m:
            ids.add(m["id"])
    return ids


def chercher(matchs, n, lo, hi, cmin, cmax, top=3, pas=0.012):
    """Programmation dynamique : exactement n sélections (une par match au plus),
    cote totale dans [lo, hi], probabilité maximale. Garde les `top` meilleurs."""
    K = top + 2
    lim = math.log(hi) + pas
    etats = {(0, 0): [(0.0, ())]}
    for i, m in enumerate(matchs):
        nouveaux = {k: list(v) for k, v in etats.items()}
        for (k, b), lst in etats.items():
            if k >= n:
                continue
            for j, (code, cote, p) in enumerate(m["options"]):
                if not (cmin <= cote <= cmax):
                    continue
                nb = b + int(round(math.log(cote) / pas))
                if nb * pas > lim:
                    continue
                cle = (k + 1, nb)
                cible = nouveaux.setdefault(cle, [])
                for lp, ch in lst:
                    cible.append((lp + math.log(p), ch + ((i, j),)))
                if len(cible) > K:
                    cible.sort(key=lambda x: -x[0])
                    del cible[K:]
        etats = nouveaux
    res, vus = [], set()
    for (k, b), lst in etats.items():
        if k != n:
            continue
        for lp, ch in lst:
            cote = math.prod(matchs[i]["options"][j][1] for i, j in ch)
            if lo <= cote <= hi and ch not in vus:
                vus.add(ch)
                res.append((math.exp(lp), cote, ch))
    res.sort(key=lambda x: -x[0])
    # tickets vraiment différents : au moins 2 sélections changent
    retenus = []
    for r in res:
        if all(len(set(r[2]) ^ set(x[2])) >= 4 for x in retenus):
            retenus.append(r)
        if len(retenus) >= top:
            break
    return retenus


def garde_fou(m, code, avec_actu):
    msgs = []
    msgs.append(("✅", f"pas commencé (coup d'envoi {pe.fmt_heure(m)}), cotes DraftKings publiées"))
    if m.get("deux_issues"):
        # sports US : blessés, fatigue, gardiens, météo, mouvement du marché...
        return msgs + ta.garde_fou_us(m, code, avec_actu)
    mvt = pe.cote_ouverture(code, m)
    c = pe.cote_marche(code, m)
    if mvt and c:
        if c > mvt + 0.03:
            msgs.append(("⚠️", f"le marché croit MOINS en ce pari qu'à l'ouverture ({mvt:.2f} → {c:.2f})"))
        elif c < mvt - 0.03:
            msgs.append(("✅", f"le marché croit PLUS en ce pari qu'à l'ouverture ({mvt:.2f} → {c:.2f})"))
    if avec_actu:
        alertes = []
        for tid, nom in ((m["id_domicile"], m["domicile"]), (m["id_exterieur"], m["exterieur"])):
            for al in pe.alertes_actu(m["ligue"], tid, nom):
                alertes.append((nom, al))
        if alertes:
            msgs.append(("⚠️", f"{len(alertes)} article(s) ESPN récents sur blessures/suspensions — à lire :"))
            for nom, al in alertes[:3]:
                msgs.append(("  •", f"[{nom}] {al['date']} « {al['titre']} »"))
        else:
            msgs.append(("✅", "aucune alerte blessure/suspension dans l'actu ESPN (7 jours)"))
    minutes = (m["debut_utc"] - pe.maintenant_utc()).total_seconds() / 60
    if minutes <= 80:
        xi = ta.titulaires(m["ligue"], m["id"])
        fav = m["id_exterieur"] if code in ("2", "N2") else m["id_domicile"]
        if xi and str(fav) in xi and code in ("1", "2"):
            rot = ta.analyse_rotation(m, fav, xi[str(fav)])
            if rot:
                cc = rot["communs"]
                msgs.append(("✅" if cc >= 8 else "⚠️",
                             f"compo officielle : {cc}/11 titulaires du dernier match de championnat"
                             + (" — FORTE ROTATION" if cc <= 5 else "")))
    elif m["ligue"].startswith("uefa."):
        msgs.append(("⚠️", "Coupe d'Europe : rotation fréquente des favoris — ne valider qu'après la compo "
                           "officielle (≈ 1 h avant), relancer le générateur à ce moment-là (leçon D)"))
    else:
        msgs.append(("ℹ️", "compo officielle pas encore publiée (≈ 1 h avant) : relancer avant de valider"))
    return msgs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    demain = pe.vers_paris(pe.maintenant_utc()).date() + timedelta(days=1)
    ap.add_argument("--du", default=demain.isoformat(), help="premier jour inclus, AAAA-MM-JJ (défaut : demain)")
    ap.add_argument("--au", default=None, help="dernier jour inclus (défaut : du + 4 jours)")
    ap.add_argument("--ligues", default=",".join(pe.LIGUES),
                    help="codes séparés par des virgules (football : eng.1, fra.1… ; sports US : nfl, ncaaf, nba, "
                         "ncaab, nhl, mlb, wnba) ou groupes : usa (sports US + MLS), foot (tout le football), "
                         "tout. Défaut : tout le football")
    ap.add_argument("--selections", type=int, default=7, help="nombre de sélections du ticket (défaut : 7)")
    ap.add_argument("--cible", type=float, default=27.5, help="cote totale visée (défaut : 27.5)")
    ap.add_argument("--tol-bas", type=float, default=0.95, help="cote minimale acceptée = cible × tol-bas (défaut : 0.95)")
    ap.add_argument("--tol-haut", type=float, default=1.10, help="cote maximale acceptée = cible × tol-haut (défaut : 1.10)")
    ap.add_argument("--cote-min", type=float, default=1.20, help="cote minimale d'une sélection")
    ap.add_argument("--cote-max", type=float, default=2.40, help="cote maximale d'une sélection")
    ap.add_argument("--avec-buts", action="store_true",
                    help="autoriser aussi plus/moins de buts ou de points (équivaut à ajouter « total » à --marches)")
    ap.add_argument("--marches", default=None,
                    help="types de paris autorisés, séparés par des virgules : ml (vainqueur, prolongation incluse "
                         "en sport US ; 1N2 en football), 1n2, handicap (sports US), total. Défaut : ml (+ total "
                         "avec --avec-buts)")
    ap.add_argument("--exclure", default=None, help="ticket JSON dont les matchs sont exclus")
    ap.add_argument("--marge-minutes", type=int, default=30, help="exclure les matchs qui commencent avant")
    ap.add_argument("--min-titulaires", type=int, default=6,
                    help="compo publiée : écarter un favori qui aligne moins de N titulaires habituels (0 = désactivé)")
    ap.add_argument("--jusqua", default=None,
                    help="dernier coup d'envoi accepté, heure de Paris (ex. 2026-11-28T04:30)")
    ap.add_argument("--sans-actu", action="store_true", help="ne pas lire l'actualité ESPN (plus rapide)")
    ap.add_argument("--top", type=int, default=3, help="nombre de tickets différents proposés (défaut : 3)")
    ap.add_argument("--comparer", default=None, help="ticket JSON à comparer (probabilité au départ)")
    ap.add_argument("--out", default="tickets", help="dossier des fichiers écrits (défaut : tickets)")
    args = ap.parse_args()

    du = date.fromisoformat(args.du)
    jusqua = None
    if args.jusqua:
        from datetime import datetime
        tz = pe.vers_paris(pe.maintenant_utc()).tzinfo
        jusqua = datetime.fromisoformat(args.jusqua).replace(tzinfo=tz)
    au = date.fromisoformat(args.au) if args.au else du + timedelta(days=4)
    ligues = pe.developper_ligues(args.ligues)
    marches = None
    if args.marches:
        marches = {x.strip().lower() for x in args.marches.split(",") if x.strip()}
        marches = {"total" if x in ("buts", "points", "plus/moins") else "ml" if x in ("vainqueur", "moneyline") else x
                   for x in marches}
        inconnus = marches - set(MARCHES)
        if inconnus:
            ap.error(f"marché(s) inconnu(s) : {', '.join(sorted(inconnus))} (choix : {', '.join(MARCHES)})")
        if args.avec_buts:
            marches.add("total")
    exclus = ids_du_ticket(args.exclure)
    matchs = [m for m in collecter(ligues, du, au, args.marge_minutes, args.avec_buts, args.min_titulaires, jusqua,
                                   marches) if m["id"] not in exclus]
    us = any(pe.est_deux_issues(x) for x in ligues)
    lo, hi = args.cible * args.tol_bas, args.cible * args.tol_haut

    L = []
    L.append(f"Générateur de ticket — {args.selections} sélections, cote visée {lo:.1f}–{hi:.1f}, "
             f"chaque sélection entre {args.cote_min:.2f} et {args.cote_max:.2f}")
    L.append(f"Matchs du {du.strftime('%d/%m')} au {au.strftime('%d/%m')} avec cotes DraftKings : {len(matchs)}"
             f" ({', '.join(sorted({m['competition'] for m in matchs}))})")
    if jusqua and us:
        L.append(f"Dernier début accepté : {jusqua.strftime('%d/%m à %H:%M')} (heure de Paris)")
    if us or marches:
        foot = any(not pe.est_deux_issues(x) for x in ligues)
        libelles = {"1n2": "1N2 (football)",
                    "ml": ("1N2 en football, vainqueur prolongation incluse en sport US" if foot
                           else "vainqueur (prolongation incluse)"),
                    "handicap": "handicap" + (" (sports US)" if foot else ""), "total": "plus/moins"}
        choix = marches or ({"ml", "total"} if args.avec_buts else {"ml"})
        if "ml" in choix:
            choix = choix - {"1n2"}
        L.append("Paris autorisés : " + ", ".join(libelles[x] for x in MARCHES if x in choix))
    if exclus:
        L.append(f"Exclus : {len(exclus)} match(s) déjà dans {args.exclure}")
    L.append(f"Cotes relevées le {pe.vers_paris(pe.maintenant_utc()).strftime('%d/%m/%Y à %H:%M')} (heure de Paris)")

    tickets = chercher(matchs, args.selections, lo, hi, args.cote_min, args.cote_max, args.top)
    if not tickets:
        L.append("\nAucune combinaison possible avec ces contraintes.")
        print("\n".join(L))
        return 1
    sorties = []
    for n, (p, cote, ch) in enumerate(tickets, 1):
        L.append("")
        L.append(f"🎟️ TICKET {n} — cote {cote:.2f} — probabilité réelle {100 * p:.2f} % (≈ 1 fois sur {1 / p:.0f})")
        sels = []
        for i, j in sorted(ch, key=lambda x: matchs[x[0]]["debut_utc"]):
            m = matchs[i]
            code, c, pr = m["options"][j]
            L.append(f"   {pe.fmt_heure(m)}  {m['competition']:<20} {m['domicile']} - {m['exterieur']}")
            L.append(f"        → {pe.libelle_pari(code, m)} @ {c:.2f}   (probabilité réelle {100 * pr:.0f} %,"
                     f" cote juste {1 / pr:.2f})")
            if n == 1:
                for ic, txt in garde_fou(m, code, not args.sans_actu):
                    L.append(f"        {ic} {txt}")
            sels.append({"ligue": m["ligue"], "date": m["debut_paris"].date().isoformat(),
                         "heure": m["debut_paris"].strftime("%H:%M"), "domicile": m["domicile"],
                         "exterieur": m["exterieur"], "pari": code, "cote": round(c, 3)})
        sorties.append({"description": f"Ticket généré n°{n} ({args.selections} sélections, cote ~{args.cible})",
                        "mise": None, "devise": None, "cote_totale": round(cote, 3),
                        "probabilite_reelle": round(p, 5), "selections": sels})

    if args.comparer:
        with open(args.comparer, encoding="utf-8") as fh:
            t = json.load(fh)
        res = ta.analyser(t, avec_actu=False)
        p0 = math.prod(r["p_depart"] for r in res if r.get("p_depart"))
        L.append("")
        L.append(f"Comparaison : ton ticket ({args.comparer}) avait au départ ≈ {100 * p0:.2f} %"
                 f" pour une cote {t['cote_totale']:.2f} ; le ticket 1 ci-dessus : {100 * tickets[0][0]:.2f} %"
                 f" pour une cote {tickets[0][1]:.2f}.")
    L.append("")
    L.append("Rappel : ce sont les cotes DraftKings. Chez ton bookmaker, prends chaque sélection seulement"
             " si sa cote est au moins égale ; sinon le ticket vaut moins.")
    if us:
        L.append("Sports US : vainqueur = prolongation (et tirs au but en NHL) incluse ; handicap ou total à ligne"
                 " entière tombé pile = remboursé (cote 1,00). En hockey, le « 1X2 » des bookmakers européens se"
                 " joue sur 60 minutes : ce n'est PAS le même pari que le vainqueur (voir README).")
    txt = "\n".join(L)
    print(txt)
    os.makedirs(args.out, exist_ok=True)
    suffixe = ""
    if us:
        suffixe = "_usa" if all(x in pe.GROUPES["usa"] for x in ligues) else "_multi"
        if marches and marches - {"ml", "1n2"}:
            suffixe += "_" + "-".join(x for x in MARCHES if x in marches)
    base = f"genere_{du.isoformat()}_{args.selections}sel_cote{args.cible:g}" + ("_soir" if jusqua else "") + suffixe
    with open(os.path.join(args.out, base + ".md"), "w", encoding="utf-8") as fh:
        fh.write("```\n" + txt + "\n```\n")
    for n, s in enumerate(sorties, 1):
        with open(os.path.join(args.out, f"{base}_ticket{n}.json"), "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=1)
    print(f"\nÉcrit : {os.path.join(args.out, base + '.md')} (+ tickets JSON, analysables avec ticket_analyse.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
