#!/usr/bin/env python3
"""Simulation Monte-Carlo des matchs de C1 + recherche du ticket combiné
le plus probable pour une cote cible (ex. ~100).

Étape 4 du processus : résultats -> pronostics -> vérification -> simulation.
Se base sur pronostics/pronos_YYYY-MM-DD.json (déjà vérifié par ucl_verif.py)
et sur les cotes DraftKings publiées par ESPN (rafraîchies au lancement).

Usage : python3 ucl_simulation.py --date 2026-11-25 --cible 100 [--sims 200000]
"""
import argparse
import json
import math
import os
import random
import sys

from ucl_bot import get_json

SUMMARY = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
           "uefa.champions/summary?event={}")
RHO = -0.10          # correction Dixon-Coles (identique à ucl_predict.py)
MARGE_ESTIMEE = 0.93  # cote estimée = 0.93 / proba quand le bookmaker ne la publie pas
MAXG = 10


def us_to_dec(ml):
    if ml is None:
        return None
    ml = float(ml)
    return 1 + (100 / abs(ml) if ml < 0 else ml / 100)


def matrice_scores(lh, la):
    """Probabilités des scores (Poisson + correction Dixon-Coles), normalisées."""
    def pois(k, l):
        return math.exp(-l) * l ** k / math.factorial(k)
    m = {}
    for h in range(MAXG + 1):
        for a in range(MAXG + 1):
            p = pois(h, lh) * pois(a, la)
            if h == 0 and a == 0:
                p *= 1 - lh * la * RHO
            elif h == 0 and a == 1:
                p *= 1 + lh * RHO
            elif h == 1 and a == 0:
                p *= 1 + la * RHO
            elif h == 1 and a == 1:
                p *= 1 - RHO
            m[(h, a)] = max(p, 0.0)
    s = sum(m.values())
    return {k: v / s for k, v in m.items()}


def cotes_reelles(event_id):
    """Cotes DraftKings actuelles (1N2 + ligne de buts) via ESPN."""
    try:
        d = get_json(SUMMARY.format(event_id))
    except Exception:
        return {}
    pcs = d.get("pickcenter") or []
    if not pcs:
        return {}
    pc = pcs[0]
    out = {}
    h = (pc.get("homeTeamOdds") or {}).get("moneyLine")
    a = (pc.get("awayTeamOdds") or {}).get("moneyLine")
    dr = (pc.get("drawOdds") or {}).get("moneyLine")
    if h is not None and a is not None and dr is not None:
        out["1"], out["N"], out["2"] = us_to_dec(h), us_to_dec(dr), us_to_dec(a)
    if pc.get("overUnder") is not None:
        out["ligne"] = float(pc["overUnder"])
        out["plus"] = us_to_dec(pc.get("overOdds"))
        out["moins"] = us_to_dec(pc.get("underOdds"))
    return out


def marches(p, mat, cr):
    """Liste des paris possibles pour un match : (libellé, test(h,a), cote, réelle?)."""
    dom, ext = p["domicile"], p["exterieur"]
    L = []

    def add(lib, f, cote_reelle=None):
        pr = sum(v for (h, a), v in mat.items() if f(h, a))
        if pr <= 0.005:
            return
        if cote_reelle:
            L.append((lib, f, round(cote_reelle, 2), True, pr))
        else:
            L.append((lib, f, round(MARGE_ESTIMEE / pr, 2), False, pr))

    add(f"Victoire {dom}", lambda h, a: h > a, cr.get("1"))
    add("Match nul", lambda h, a: h == a, cr.get("N"))
    add(f"Victoire {ext}", lambda h, a: h < a, cr.get("2"))
    # Double chance : cote dérivée des cotes 1N2 réelles (marge ~5 %)
    if cr.get("1"):
        i1, iN, i2 = 1 / cr["1"], 1 / cr["N"], 1 / cr["2"]
        s = i1 + iN + i2
        dc = lambda x: 0.95 / (x / s)
        add(f"{dom} ou nul", lambda h, a: h >= a, dc(i1 + iN))
        add(f"{ext} ou nul", lambda h, a: h <= a, dc(i2 + iN))
    else:
        add(f"{dom} ou nul", lambda h, a: h >= a)
        add(f"{ext} ou nul", lambda h, a: h <= a)
    if cr.get("ligne") is not None and cr.get("plus"):
        ln = cr["ligne"]
        add(f"Plus de {ln} buts", lambda h, a, ln=ln: h + a > ln, cr["plus"])
        add(f"Moins de {ln} buts", lambda h, a, ln=ln: h + a < ln, cr["moins"])
    for ln in (1.5, 2.5, 3.5):
        if cr.get("ligne") == ln:
            continue
        add(f"Plus de {ln} buts", lambda h, a, ln=ln: h + a > ln)
        add(f"Moins de {ln} buts", lambda h, a, ln=ln: h + a < ln)
    add("Les deux équipes marquent", lambda h, a: h > 0 and a > 0)
    add("Une équipe ne marque pas", lambda h, a: h == 0 or a == 0)
    add(f"{dom} gagne et +1,5 buts", lambda h, a: h > a and h + a >= 2)
    add(f"{ext} gagne et +1,5 buts", lambda h, a: a > h and h + a >= 2)
    add(f"{dom} gagne et les deux marquent", lambda h, a: h > a and a > 0)
    add(f"{ext} gagne et les deux marquent", lambda h, a: a > h and h > 0)
    add(f"{dom} gagne par 2 buts ou +", lambda h, a: h - a >= 2)
    add(f"{ext} gagne par 2 buts ou +", lambda h, a: a - h >= 2)
    return L


def tirer(mat_items, cum, rnd):
    u = rnd.random()
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < u:
            lo = mid + 1
        else:
            hi = mid
    return mat_items[lo][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-11-25")
    ap.add_argument("--cible", type=float, default=100.0)
    ap.add_argument("--sims", type=int, default=200000)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--reelles-seulement", action="store_true",
                    help="n'utiliser que des cotes publiées par le bookmaker")
    ap.add_argument("--source", choices=["marche", "modele"], default="marche",
                    help="marche = buts attendus déduits des cotes (le plus fiable au backtest) ; "
                         "modele = buts attendus publiés dans le JSON (cotes seules depuis le 25/11/2026, "
                         "mélange 70/30 avant ou avec ucl_predict.py --melange)")
    ap.add_argument("--out", default="pronostics/")
    args = ap.parse_args()

    src = os.path.join("pronostics", f"pronos_{args.date}.json")
    data = json.load(open(src, encoding="utf-8"))
    rnd = random.Random(args.seed)

    matchs = []
    for p in data["pronostics"]:
        if args.source == "marche" and p.get("buts_attendus_cotes"):
            lh, la = p["buts_attendus_cotes"]
        else:
            lh, la = p["buts_attendus"]
        mat = matrice_scores(lh, la)
        cr = cotes_reelles(p["id"])
        items = sorted(mat.items(), key=lambda kv: -kv[1])
        cum, s = [], 0.0
        for _, v in items:
            s += v
            cum.append(s)
        cum[-1] = 1.0
        matchs.append({"p": p, "mat": mat, "cr": cr, "items": items, "cum": cum,
                       "marches": marches(p, mat, cr)})

    # --- Simulation Monte-Carlo conjointe des 9 matchs
    N = args.sims
    sims = [[tirer(m["items"], m["cum"], rnd) for _ in range(N)] for m in matchs]

    lignes = []
    lignes.append(f"Simulation Monte-Carlo — {N:,} simulations de chaque match".replace(",", " ")
                  + f" — probabilités : {'marché (cotes)' if args.source == 'marche' else 'buts attendus publiés'}")
    lignes.append("=" * 60)
    for m, sm in zip(matchs, sims):
        p = m["p"]
        cnt = {}
        for sc in sm:
            cnt[sc] = cnt.get(sc, 0) + 1
        top = sorted(cnt.items(), key=lambda kv: -kv[1])[:5]
        v1 = sum(1 for h, a in sm if h > a) / N
        vn = sum(1 for h, a in sm if h == a) / N
        v2 = 1 - v1 - vn
        o25 = sum(1 for h, a in sm if h + a > 2.5) / N
        btts = sum(1 for h, a in sm if h and a) / N
        bm = sum(h for h, a in sm) / N
        ba = sum(a for h, a in sm) / N
        lignes.append("")
        lignes.append(f"⚽ {p['heure_paris']}  {p['domicile']} - {p['exterieur']}")
        lignes.append(f"   1 = {v1:.1%}  N = {vn:.1%}  2 = {v2:.1%}   |  buts moyens {bm:.2f} - {ba:.2f}")
        lignes.append(f"   +2,5 buts {o25:.1%}  |  les deux marquent {btts:.1%}")
        lignes.append("   Scores les plus fréquents : " + ", ".join(
            f"{h}-{a} ({c / N:.1%})" for (h, a), c in top))
        cr = m["cr"]
        if cr.get("1"):
            txt = f"   Cotes DraftKings : {cr['1']:.2f} / {cr['N']:.2f} / {cr['2']:.2f}"
            if cr.get("ligne") is not None and cr.get("plus"):
                txt += f"  |  +{cr['ligne']} : {cr['plus']:.2f}  -{cr['ligne']} : {cr['moins']:.2f}"
            lignes.append(txt)

    # --- Recherche du ticket : au plus un pari par match, cote totale ≈ cible,
    #     probabilité conjointe maximale (programmation dynamique sur log-cote).
    pas = 0.02
    lo_c, hi_c = math.log(args.cible * 0.95), math.log(args.cible * 1.15)
    etats = {0: (0.0, [])}  # bucket log-cote -> (log-proba, choix)
    for i, m in enumerate(matchs):
        nouveaux = dict(etats)
        for b, (lp, ch) in etats.items():
            for j, (lib, f, cote, reelle, pr) in enumerate(m["marches"]):
                if args.reelles_seulement and not reelle:
                    continue
                if cote < 1.04:
                    continue
                nb = b + int(round(math.log(cote) / pas))
                if nb * pas > hi_c:
                    continue
                nlp = lp + math.log(pr)
                if nb not in nouveaux or nouveaux[nb][0] < nlp:
                    nouveaux[nb] = (nlp, ch + [(i, j)])
        etats = nouveaux

    candidats = [(lp, ch) for b, (lp, ch) in etats.items() if lo_c <= b * pas <= hi_c]
    candidats.sort(key=lambda x: -x[0])

    def evaluer(ch):
        cote = 1.0
        ok = [True] * N
        det = []
        for i, j in ch:
            lib, f, c, reelle, pr = matchs[i]["marches"][j]
            cote *= c
            sm = sims[i]
            for k in range(N):
                if ok[k] and not f(*sm[k]):
                    ok[k] = False
            det.append((matchs[i]["p"], lib, c, reelle, pr))
        return cote, sum(ok) / N, det

    lignes.append("")
    lignes.append(f"🎯 Tickets les plus probables pour une cote ≈ {args.cible:g}")
    lignes.append("=" * 60)
    resultats = []
    vus = set()
    for lp, ch in candidats:
        cle = tuple(ch)
        if cle in vus:
            continue
        vus.add(cle)
        cote, psim, det = evaluer(ch)
        resultats.append((cote, psim, det))
        if len(resultats) >= 3:
            break
    for n, (cote, psim, det) in enumerate(resultats, 1):
        lignes.append("")
        lignes.append(f"Ticket {n} — cote totale {cote:.1f} — passe dans {psim:.2%} des simulations"
                      f" (≈ 1 fois sur {1 / psim:.0f})" if psim else f"Ticket {n} — cote {cote:.1f}")
        for p, lib, c, reelle, pr in det:
            lignes.append(f"   • {p['heure_paris']} {p['domicile']} - {p['exterieur']} : {lib}"
                          f"  @ {c:.2f}{'' if reelle else ' (cote estimée)'}  [{pr:.0%}]")
        ev = cote * psim
        lignes.append(f"   Espérance : pour 10 € misés, gain moyen simulé {10 * ev:.2f} € (retour {ev:.0%})")

    txt = "\n".join(lignes)
    print(txt)
    os.makedirs(args.out, exist_ok=True)
    chemin = os.path.join(args.out, f"simulation_{args.date}_{args.source}.md")
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write("```\n" + txt + "\n```\n")
    print(f"\nRapport écrit : {chemin}")


if __name__ == "__main__":
    sys.exit(main())
