#!/usr/bin/env python3
"""Analyse des tickets qui contiennent des marchés « dérivés » proposés par les
bookmakers mais absents des cotes DraftKings publiées par ESPN :

  - Double chance + les deux équipes marquent (« 12 Et Chaque Equipe Va Marquer »)
  - Les deux équipes marquent (oui / non), 1 ou 2 + les deux marquent
  - 1X2 (2UP) : gagné si l'équipe gagne OU si elle mène de 2 buts à un moment

Méthode : on cale un modèle de Poisson (buts attendus de chaque équipe) sur les
cotes 1N2 et plus/moins de DraftKings, marge retirée, puis on calcule la
probabilité du marché dérivé. Le 2UP est simulé minute par minute (Monte-Carlo).
Les marchés classiques (1, N, 2, double chance, plus/moins) gardent la
probabilité directe du marché. Garde-fou : actualité ESPN, compos et rotation
(à partir d'environ 1 h avant le match).

Usage : python3 marches_derives.py tickets/fichier.json [--demarge puissance] [--sims 200000]
"""
import argparse
import json
import math
import os
import random
import sys
import unicodedata
from datetime import date

import paris_espn as pe
import ticket_analyse as ta


def _ascii(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def normaliser(pari, ligue):
    """Libellé du bookmaker -> code interne."""
    s = _ascii(pari).replace("–", "-")
    deux_marquent = ("deux equipes" in s or "chaque equipe" in s or "btts" in s or "both teams" in s)
    if "2up" in s:
        cote = "2" if ("v2" in s or s.strip().endswith("2")) else "1"
        return f"{cote} 2UP"
    if deux_marquent:
        non = s.rstrip().endswith("non") or "- non" in s or ": non" in s
        if "12" in s.replace("1x2", ""):
            return "12+BTTS"
        if "1x" in s or "1n" in s:
            return "1N+BTTS"
        if "x2" in s or "n2" in s:
            return "N2+BTTS"
        if "v1" in s or " 1 " in f" {s} ":
            return "1+BTTS"
        if "v2" in s:
            return "2+BTTS"
        return "BTTS NON" if non else "BTTS"
    return pe.normaliser_pari(pari, ligue)


LIBELLES = {
    "12+BTTS": "Pas de nul + les deux équipes marquent",
    "1N+BTTS": "{dom} ou nul + les deux équipes marquent",
    "N2+BTTS": "{ext} ou nul + les deux équipes marquent",
    "1+BTTS": "Victoire {dom} + les deux équipes marquent",
    "2+BTTS": "Victoire {ext} + les deux équipes marquent",
    "BTTS": "Les deux équipes marquent",
    "BTTS NON": "Une équipe au moins ne marque pas",
    "1 2UP": "Victoire {dom} (2UP : payé si {dom} mène de 2 buts)",
    "2 2UP": "Victoire {ext} (2UP : payé si {ext} mène de 2 buts)",
}


def libelle(code, m):
    if code in LIBELLES:
        return LIBELLES[code].format(dom=m["domicile"], ext=m["exterieur"])
    return pe.libelle_pari(code, m)


def condition(code):
    return {
        "12+BTTS": lambda h, a: h != a and h > 0 and a > 0,
        "1N+BTTS": lambda h, a: h >= a and h > 0 and a > 0,
        "N2+BTTS": lambda h, a: a >= h and h > 0 and a > 0,
        "1+BTTS": lambda h, a: h > a and a > 0,
        "2+BTTS": lambda h, a: a > h and h > 0,
        "BTTS": lambda h, a: h > 0 and a > 0,
        "BTTS NON": lambda h, a: h == 0 or a == 0,
    }.get(code)


def lambdas(m):
    p = m.get("probas") or {}
    ligne = (m.get("cotes") or {}).get("ligne")
    if "1" not in p or "N" not in p:
        return None
    return ta.buts_attendus(p, ligne)


def proba_grille(lh, la, cond, maxg=10):
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(maxg + 1)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(maxg + 1)]
    return sum(ph[h] * pa[a] for h in range(maxg + 1) for a in range(maxg + 1) if cond(h, a))


def proba_2up(cote, lh, la, sims=200000, graine=2026):
    """Monte-Carlo : buts répartis uniformément sur 94 minutes."""
    rnd = random.Random(graine)
    ok = 0
    for _ in range(sims):
        nh = _tirage_poisson(rnd, lh)
        na = _tirage_poisson(rnd, la)
        buts = sorted([(rnd.random(), 1) for _ in range(nh)] + [(rnd.random(), -1) for _ in range(na)])
        ecart, maxi, mini = 0, 0, 0
        for _, s in buts:
            ecart += s
            maxi, mini = max(maxi, ecart), min(mini, ecart)
        if cote == "1":
            ok += (nh > na) or maxi >= 2
        else:
            ok += (na > nh) or mini <= -2
    return ok / sims


def _tirage_poisson(rnd, lam):
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= l:
            return k
        k += 1


def evolution_score(m):
    """Écarts successifs à partir des buts (détails du tableau ESPN)."""
    data = pe.scoreboard(m["ligue"], m["debut_paris"].date())
    ev = next((e for e in data.get("events") or [] if e["id"] == m["id"]), None)
    if not ev:
        return None
    det = ev["competitions"][0].get("details") or []
    ecarts, ecart = [], 0
    for d in det:
        if not d.get("scoringPlay"):
            continue
        tid = str((d.get("team") or {}).get("id"))
        ecart += 1 if tid == str(m["id_domicile"]) else -1
        ecarts.append(ecart)
    return ecarts


def analyser_ticket(t, sims, avec_actu=True):
    res = []
    for i, sel in enumerate(t["selections"], 1):
        jour = date.fromisoformat(sel["date"])
        m, _ = pe.trouver_match(sel["ligue"], jour, sel["domicile"], sel["exterieur"])
        code = normaliser(sel["pari"], sel["ligue"])
        r = {"n": i, "sel": sel, "code": code, "m": m, "garde_fou": []}
        res.append(r)
        if not m:
            r.update(etat="introuvable", p=None)
            continue
        lam = lambdas(m)
        r["lambdas"] = lam
        cond = condition(code)
        if code.endswith("2UP"):
            r["p"] = proba_2up(code[0], *lam, sims=sims) if lam else None
            r["p_simple"] = (m.get("probas") or {}).get(code[0])
            r["source"] = "simulation calée sur les cotes DraftKings"
        elif cond:
            r["p"] = proba_grille(*lam, cond) if lam else None
            r["source"] = "Poisson calé sur les cotes DraftKings (estimation)"
        else:
            p, src = pe.proba_pari(code, m, sel["cote"])
            r["p"], r["source"] = p, src
        # statut
        h, a = m["score"]
        if m["termine"]:
            if code.endswith("2UP"):
                gagne = (h > a) if code[0] == "1" else (a > h)
                if not gagne:
                    ec = evolution_score(m) or []
                    gagne = any(e >= 2 for e in ec) if code[0] == "1" else any(e <= -2 for e in ec)
                r["etat"] = "gagne" if gagne else "perdu"
            elif cond:
                r["etat"] = "gagne" if cond(h, a) else "perdu"
            else:
                ok = pe.pari_gagnant(code, h, a)
                r["etat"] = "gagne" if ok else "perdu"
        elif m["etat"] == "in":
            r["etat"] = "en_cours"
        else:
            r["etat"] = "a_venir"
        # garde-fou
        if r["etat"] in ("a_venir", "en_cours"):
            if avec_actu:
                for tid, nom in ((m["id_domicile"], m["domicile"]), (m["id_exterieur"], m["exterieur"])):
                    for al in pe.alertes_actu(m["ligue"], tid, nom):
                        r["garde_fou"].append(("⚠️", f"[{nom}] {al['date']} « {al['titre']} »"))
            minutes = (m["debut_utc"] - pe.maintenant_utc()).total_seconds() / 60
            if minutes <= 80:
                xi = ta.titulaires(m["ligue"], m["id"])
                for tid, nom in ((m["id_domicile"], m["domicile"]), (m["id_exterieur"], m["exterieur"])):
                    if str(tid) in xi:
                        rot = ta.analyse_rotation(m, tid, xi[str(tid)])
                        if rot:
                            c = rot["communs"]
                            r["garde_fou"].append(("✅" if c >= 8 else "⚠️",
                                                   f"{nom} : {c}/11 titulaires habituels"
                                                   + (f" (absents : {', '.join(rot['absents_vs_reference'][:4])})"
                                                      if rot["absents_vs_reference"] else "")))
            else:
                r["garde_fou"].append(("ℹ️", "compos officielles ~1 h avant le coup d'envoi"))
    return res


def rapport(t, res):
    L = [f"Analyse — {t.get('description', '')}",
         f"{len(res)} sélections — cote affichée {t['cote_totale']:.3f}",
         f"Méthode : cotes DraftKings (ESPN) marge retirée ({pe.METHODE_DEMARGE}) ; marchés dérivés estimés par "
         "un modèle de Poisson calé sur ces cotes.", ""]
    icones = {"gagne": "✅ GAGNÉ", "perdu": "❌ PERDU", "en_cours": "🔴 EN COURS", "a_venir": "⏳ À VENIR",
              "introuvable": "❓ INTROUVABLE"}
    for r in res:
        sel, m = r["sel"], r["m"]
        if not m:
            L.append(f"{r['n']}. {sel['domicile']} - {sel['exterieur']} : ❓ introuvable sur ESPN ({sel['ligue']})")
            continue
        sc = f" — {m['score'][0]}-{m['score'][1]}" if m["etat"] in ("in", "post") else ""
        L.append(f"{r['n']}. {m['domicile']} - {m['exterieur']}  ({m['competition']}, {pe.fmt_heure(m)}){sc}")
        L.append(f"   Pari : {libelle(r['code'], m)} @ {sel['cote']:.3f}  →  {icones.get(r['etat'], r['etat'])}")
        if r.get("p"):
            implicite = 1 / sel["cote"]
            valeur = sel["cote"] * r["p"] - 1
            L.append(f"   Probabilité réelle estimée : {100 * r['p']:.1f} %  |  ta cote suppose {100 * implicite:.1f} %"
                     f"  →  ce que le bookmaker garde sur ce pari : {(-100 * valeur):.1f} %"
                     if valeur < 0 else
                     f"   Probabilité réelle estimée : {100 * r['p']:.1f} %  |  ta cote suppose {100 * implicite:.1f} %"
                     f"  →  ta cote est AU-DESSUS du juste prix (+{100 * valeur:.1f} %)")
            if r.get("p_simple"):
                L.append(f"   (victoire simple : {100 * r['p_simple']:.1f} % ; le 2UP ajoute "
                         f"{100 * (r['p'] - r['p_simple']):.1f} points)")
            if r.get("lambdas"):
                L.append(f"   Buts attendus (marché) : {m['domicile']} {r['lambdas'][0]:.2f} – {r['lambdas'][1]:.2f} {m['exterieur']}")
        for ic, txt in r["garde_fou"][:6]:
            L.append(f"   {ic} {txt}")
        L.append("")
    ps = [r["p"] for r in res if r.get("p")]
    perdus = [r for r in res if r.get("etat") == "perdu"]
    if len(ps) == len(res):
        p = math.prod(ps)
        L.append(f"BILAN : probabilité que le ticket passe ≈ {100 * p:.2f} % (≈ 1 fois sur {1 / p:.0f})"
                 f" pour une cote de {t['cote_totale']:.1f}")
        L.append(f"   Rendement moyen sur la durée : {100 * p * t['cote_totale']:.0f} % de la mise"
                 f" (100 % = pari équitable)")
    if perdus:
        L.append("   ❌ Ticket perdu.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticket")
    ap.add_argument("--sims", type=int, default=200000)
    ap.add_argument("--demarge", choices=["proportionnelle", "puissance"], default="puissance")
    ap.add_argument("--sans-actu", action="store_true")
    args = ap.parse_args()
    pe.METHODE_DEMARGE = args.demarge
    t = json.load(open(args.ticket, encoding="utf-8"))
    res = analyser_ticket(t, args.sims, not args.sans_actu)
    txt = rapport(t, res)
    print(txt)
    base = os.path.splitext(os.path.basename(args.ticket))[0]
    with open(os.path.join(os.path.dirname(args.ticket) or ".", f"analyse_{base}.md"), "w", encoding="utf-8") as fh:
        fh.write("```\n" + txt + "\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
