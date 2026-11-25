#!/usr/bin/env python3
"""Ticket combiné « factuel » : uniquement les cotes réelles publiées par le
bookmaker (DraftKings via ESPN), aucune estimation, aucun modèle.

Pour chaque pari, la probabilité utilisée est celle du marché, marge retirée
(normalisation des cotes 1N2 et des cotes plus/moins). Le ticket retenu est
celui qui a la plus forte probabilité de passer pour une cote totale dans la
fourchette demandée.

Usage : python3 ucl_ticket.py --date 2026-11-25 --cible 100 [--tous]
  --tous : un pari obligatoire sur CHAQUE match du jour.
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

from ucl_bot import get_json
from ucl_simulation import us_to_dec

SUMMARY = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
           "uefa.champions/summary?event={}")


def paris_reels(p):
    d = get_json(SUMMARY.format(p["id"]))
    comp = d["header"]["competitions"][0]
    etat = comp["status"]["type"]["state"]  # pre / in / post
    debut = comp["date"]
    pcs = d.get("pickcenter") or []
    if not pcs:
        return etat, debut, None, []
    pc = pcs[0]
    dom, ext = p["domicile"], p["exterieur"]
    out = []
    try:
        c = [us_to_dec(pc["homeTeamOdds"]["moneyLine"]),
             us_to_dec(pc["drawOdds"]["moneyLine"]),
             us_to_dec(pc["awayTeamOdds"]["moneyLine"])]
        s = sum(1 / x for x in c)
        for lib, x in zip([f"Victoire {dom}", "Match nul", f"Victoire {ext}"], c):
            out.append((lib, round(x, 2), (1 / x) / s, s))
    except (KeyError, TypeError):
        pass
    if pc.get("overUnder") is not None and pc.get("overOdds") and pc.get("underOdds"):
        ln = pc["overUnder"]
        o, u = us_to_dec(pc["overOdds"]), us_to_dec(pc["underOdds"])
        s = 1 / o + 1 / u
        out.append((f"Plus de {ln} buts", round(o, 2), (1 / o) / s, s))
        out.append((f"Moins de {ln} buts", round(u, 2), (1 / u) / s, s))
    return etat, debut, pc.get("provider", {}).get("name"), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-11-25")
    ap.add_argument("--cible", type=float, default=100.0)
    ap.add_argument("--min", type=float, default=None)
    ap.add_argument("--max", type=float, default=None)
    ap.add_argument("--tous", action="store_true")
    args = ap.parse_args()
    cmin = args.min or args.cible * 0.95
    cmax = args.max or args.cible * 1.10

    data = json.load(open(os.path.join("pronostics", f"pronos_{args.date}.json"),
                          encoding="utf-8"))
    verif = data.get("verification", {})
    controles, erreurs = [], []
    matchs = []
    for p in data["pronostics"]:
        etat, debut, bk, paris = paris_reels(p)
        nom = f"{p['domicile']} - {p['exterieur']}"
        if etat != "pre":
            erreurs.append(f"{nom} : match déjà commencé/terminé ({etat}) → exclu")
            continue
        if not paris:
            erreurs.append(f"{nom} : aucune cote publiée → exclu")
            continue
        matchs.append((p, paris))
    controles.append(f"{len(matchs)}/{len(data['pronostics'])} matchs avec cotes réelles et pas encore commencés")
    statut = verif.get("statut", "?")
    controles.append(f"Vérification des pronostics (ucl_verif) : statut {statut}")

    # Programmation dynamique exacte sur la log-cote (pas fin), max de log-proba.
    pas = 0.005
    etats = {0: (0.0, [])}
    for i, (p, paris) in enumerate(matchs):
        nouveaux = {} if args.tous else dict(etats)
        for b, (lp, ch) in etats.items():
            for j, (lib, cote, pr, s) in enumerate(paris):
                nb = b + int(round(math.log(cote) / pas))
                if nb * pas > math.log(cmax) + pas:
                    continue
                nlp = lp + math.log(pr)
                if nb not in nouveaux or nouveaux[nb][0] < nlp:
                    nouveaux[nb] = (nlp, ch + [(i, j)])
        etats = nouveaux

    meilleurs = []
    for b, (lp, ch) in etats.items():
        cote = math.prod(matchs[i][1][j][1] for i, j in ch)
        if cmin <= cote <= cmax and ch:
            meilleurs.append((math.exp(lp), cote, ch))
    meilleurs.sort(key=lambda x: -x[0])

    print(f"Ticket factuel — cotes réelles {matchs[0][1] and 'DraftKings'} uniquement — cote visée {cmin:.0f}-{cmax:.0f}"
          + (" — un pari sur chaque match" if args.tous else ""))
    print(f"Cotes relevées le {datetime.now(timezone.utc).astimezone().strftime('%d/%m/%Y %H:%M')}")
    print("Contrôles :")
    for c in controles:
        print("   ✅", c)
    for e in erreurs:
        print("   ⚠️", e)
    if not meilleurs:
        print("Aucune combinaison dans la fourchette.")
        return 1
    for n, (pr, cote, ch) in enumerate(meilleurs[:3], 1):
        print(f"\nTicket {n} — cote {cote:.2f} — probabilité (marché, marge retirée) {pr:.2%}"
              f" ≈ 1 fois sur {1 / pr:.0f}")
        for i, j in ch:
            p, paris = matchs[i]
            lib, c, q, s = paris[j]
            print(f"   • {p['heure_paris']} {p['domicile']} - {p['exterieur']} : {lib} @ {c:.2f}"
                  f"  [{q:.0%}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
