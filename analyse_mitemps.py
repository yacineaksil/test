#!/usr/bin/env python3
"""Analyse mi-temps par mi-temps des matchs d'un ticket (ou de n'importe quel
match ESPN), à partir du commentaire minute par minute d'ESPN : tirs, tirs
cadrés, tirs dans la surface, poteaux, corners, fautes, cartons, changements
à la pause. Fonctionne aussi pendant le match (mi-temps en cours).

Usage :
  python3 analyse_mitemps.py tickets/ticket_2026-11-25.json --date 2026-11-26
  python3 analyse_mitemps.py --ligue uefa.europa.conf --date 2026-11-26 --match "Iberia" --match "Pafos"
"""
import argparse
import json
import os
import re
import sys
from datetime import date

import paris_espn as pe


def _equipe(txt, noms):
    """Équipe concernée par une ligne de commentaire."""
    m = re.search(r"\(([^()]+)\)", txt)
    if m:
        for n in noms:
            if pe.similarite(m.group(1), n) >= 0.75:
                return n
    for motif in (r"^Corner,\s*([^.]+)\.", r"^Offside,\s*([^.]+)\.", r"^Substitution,\s*([^.]+)\.",
                  r"^Foul by [^(]+\(([^)]+)\)"):
        m = re.search(motif, txt)
        if m:
            for n in noms:
                if pe.similarite(m.group(1), n) >= 0.75:
                    return n
    return None


def stats_vides():
    return {"buts": 0, "tirs": 0, "cadres": 0, "surface": 0, "contres": 0, "poteaux": 0,
            "corners": 0, "fautes": 0, "jaunes": 0, "rouges": 0, "hors_jeu": 0,
            "changements": 0, "penaltys": 0, "buteurs": [], "entrants": []}


def analyser_match(ligue, event_id):
    d = pe.get_json(pe.SUMMARY.format(ligue, event_id))
    comp = d["header"]["competitions"][0]
    eqs = {c["homeAway"]: c["team"]["displayName"] for c in comp["competitors"]}
    noms = [eqs["home"], eqs["away"]]
    statut = comp["status"]["type"]
    phases = {p: {n: stats_vides() for n in noms} for p in ("1", "pause", "2")}
    phase = "1"
    commentaire = sorted(d.get("commentary") or [], key=lambda c: c.get("sequence", 0))
    for c in commentaire:
        txt = (c.get("text") or "").strip()
        minute = (c.get("time") or {}).get("displayValue") or ""
        if txt.startswith("First Half ends"):
            phase = "pause"
            continue
        if txt.startswith("Second Half begins"):
            phase = "2"
            continue
        if txt.startswith("Second Half ends") or txt.startswith("Match ends"):
            continue
        eq = _equipe(txt, noms)
        if eq is None:
            continue
        s = phases[phase][eq]
        if txt.startswith("Attempt") or txt.startswith("Goal!"):
            s["tirs"] += 1
            if txt.startswith("Attempt saved") or txt.startswith("Goal!"):
                s["cadres"] += 1
            if txt.startswith("Attempt blocked"):
                s["contres"] += 1
            if ("box" in txt and "outside the box" not in txt) or "difficult angle" in txt:
                s["surface"] += 1
            if "penalty" in txt.lower():
                s["penaltys"] += 1
        if txt.startswith("Goal!"):
            s["buts"] += 1
            m = re.search(r"\.\s*([^.(]+?)\s*\(", txt.split("Goal!", 1)[1])
            s["buteurs"].append(f"{minute} {m.group(1).strip() if m else '?'}")
        if "hits the" in txt and ("post" in txt or "bar" in txt):
            s["poteaux"] += 1
            s["tirs"] += 1
        if txt.startswith("Corner,"):
            s["corners"] += 1
        if txt.startswith("Foul by"):
            s["fautes"] += 1
        if "yellow card" in txt and not txt.startswith("Second yellow"):
            s["jaunes"] += 1
        if txt.startswith("Second yellow") or "red card" in txt:
            s["rouges"] += 1
        if txt.startswith("Offside,"):
            s["hors_jeu"] += 1
        if txt.startswith("Substitution,"):
            s["changements"] += 1
            m = re.search(r"\.\s*(.+?) replaces", txt)
            if m:
                s["entrants"].append(m.group(1))
    # possession / tirs du match (boxscore) : cumul, pas par mi-temps
    poss = {}
    for t in (d.get("boxscore") or {}).get("teams") or []:
        for st in t.get("statistics") or []:
            if st.get("name") == "possessionPct":
                poss[t["team"]["displayName"]] = st.get("displayValue")
    score = tuple(int(c.get("score") or 0) for c in sorted(comp["competitors"], key=lambda c: c["homeAway"] != "home"))
    return {"id": event_id, "ligue": ligue, "domicile": eqs["home"], "exterieur": eqs["away"],
            "etat": statut.get("state"), "statut": statut.get("shortDetail") or statut.get("detail"),
            "chrono": comp["status"].get("displayClock"), "score": score, "phases": phases,
            "possession": poss, "commentaire_dispo": bool(commentaire), "phase_actuelle": phase}


def lecture(a, fav=None):
    """Quelques constats factuels sur la 1re mi-temps."""
    dom, ext = a["domicile"], a["exterieur"]
    p1 = a["phases"]["1"]
    L = []
    sd, se = p1[dom], p1[ext]
    if sd["tirs"] + se["tirs"] == 0:
        return ["aucun tir recensé dans le commentaire"]
    dom_tirs = sd["tirs"] - se["tirs"]
    if abs(dom_tirs) >= 3:
        L.append(f"{dom if dom_tirs > 0 else ext} a dominé aux tirs ({sd['tirs']}-{se['tirs']})")
    else:
        L.append(f"1re mi-temps équilibrée aux tirs ({sd['tirs']}-{se['tirs']})")
    for n, s, o in ((dom, sd, se), (ext, se, sd)):
        if s["tirs"] >= 6 and s["buts"] == 0:
            L.append(f"{n} manque d'efficacité : {s['tirs']} tirs, {s['cadres']} cadrés, 0 but")
        if s["buts"] > 0 and s["tirs"] <= 2:
            L.append(f"{n} a marqué avec très peu d'occasions ({s['tirs']} tir(s))")
        if s["rouges"]:
            L.append(f"🟥 {n} réduit à {11 - s['rouges']} en 1re mi-temps")
        if s["poteaux"]:
            L.append(f"{n} a touché les montants {s['poteaux']} fois")
    pz = a["phases"]["pause"]
    for n in (dom, ext):
        if pz[n]["changements"] >= 2:
            L.append(f"{n} fait {pz[n]['changements']} changements à la pause ({', '.join(pz[n]['entrants'])}) : "
                     f"l'entraîneur réagit")
    return L


def bloc(a, pari=None):
    dom, ext = a["domicile"], a["exterieur"]
    L = []
    etat = {"post": "terminé", "in": f"en cours ({a['chrono'] or a['statut']})", "pre": "à venir"}.get(a["etat"], a["etat"])
    L.append(f"⚽ {dom} {a['score'][0]}-{a['score'][1]} {ext}  —  {etat}" + (f"  |  ton pari : {pari}" if pari else ""))
    if not a["commentaire_dispo"]:
        L.append("   (pas de commentaire détaillé ESPN pour ce match)")
        return L
    h1 = a["phases"]["1"]
    b1 = (h1[dom]["buts"], h1[ext]["buts"])
    titre = "1re mi-temps" + (" (en cours)" if a["etat"] == "in" and a["phase_actuelle"] == "1" else "")
    L.append(f"   {titre} : {dom} {b1[0]}-{b1[1]} {ext}")
    lignes = [("Tirs", "tirs"), ("Cadrés", "cadres"), ("Dans la surface", "surface"), ("Corners", "corners"),
              ("Fautes", "fautes"), ("Cartons jaunes", "jaunes"), ("Rouges", "rouges")]
    L.append(f"   {'':<17}{dom[:16]:>16} {ext[:16]:>16}")
    for lib, k in lignes:
        L.append(f"   {lib:<17}{h1[dom][k]:>16} {h1[ext][k]:>16}")
    if h1[dom]["poteaux"] or h1[ext]["poteaux"]:
        L.append(f"   {'Poteaux/barre':<17}{h1[dom]['poteaux']:>16} {h1[ext]['poteaux']:>16}")
    buts = h1[dom]["buteurs"] + h1[ext]["buteurs"]
    if buts:
        L.append("   Buts : " + ", ".join(buts))
    for x in lecture(a):
        L.append(f"   → {x}")
    if a["etat"] in ("in", "post") and a["phase_actuelle"] == "2":
        h2 = a["phases"]["2"]
        L.append(f"   2e mi-temps : tirs {h2[dom]['tirs']}-{h2[ext]['tirs']}, cadrés {h2[dom]['cadres']}-{h2[ext]['cadres']},"
                 f" buts {h2[dom]['buts']}-{h2[ext]['buts']}"
                 + (f", rouges {h2[dom]['rouges']}-{h2[ext]['rouges']}" if h2[dom]['rouges'] or h2[ext]['rouges'] else ""))
    if a["possession"]:
        L.append("   Possession (match, cumul) : " + " / ".join(f"{k} {v} %" for k, v in a["possession"].items()))
    return L


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticket", nargs="?", help="ticket JSON (analyse ses matchs du jour)")
    ap.add_argument("--date", default=None, help="date (AAAA-MM-JJ, heure de Paris) ; défaut : aujourd'hui")
    ap.add_argument("--ligue", default=None)
    ap.add_argument("--match", action="append", default=[], help="filtre sur un nom d'équipe")
    args = ap.parse_args()
    jour = date.fromisoformat(args.date) if args.date else pe.vers_paris(pe.maintenant_utc()).date()

    cibles = []
    if args.ticket:
        with open(args.ticket, encoding="utf-8") as fh:
            t = json.load(fh)
        for s in t["selections"]:
            if s["date"] != jour.isoformat():
                continue
            m, _ = pe.trouver_match(s["ligue"], jour, s["domicile"], s["exterieur"])
            if m:
                cibles.append((m, f"{pe.libelle_pari(pe.normaliser_pari(s['pari']), m)} @ {s['cote']}"))
    if args.ligue:
        for m in pe.matchs_du_jour_paris(args.ligue, jour):
            nom = m["domicile"] + " " + m["exterieur"]
            if not args.match or any(f.lower() in nom.lower() for f in args.match):
                cibles.append((m, None))
    if not cibles:
        print("Aucun match trouvé pour cette date.")
        return 1
    L = [f"Analyse par mi-temps — {jour.strftime('%d/%m/%Y')} — relevé à "
         f"{pe.vers_paris(pe.maintenant_utc()).strftime('%H:%M')} (heure de Paris)",
         "Source : commentaire minute par minute d'ESPN (tirs, corners, cartons attribués à chaque mi-temps).", ""]
    for m, pari in sorted(cibles, key=lambda x: x[0]["debut_utc"]):
        a = analyser_match(m["ligue"], m["id"])
        L += bloc(a, pari)
        L.append("")
    txt = "\n".join(L)
    print(txt)
    os.makedirs("tickets", exist_ok=True)
    nom = os.path.splitext(os.path.basename(args.ticket))[0] if args.ticket else (args.ligue or "matchs")
    with open(os.path.join("tickets", f"mitemps_{nom}_{jour.isoformat()}.md"), "w", encoding="utf-8") as fh:
        fh.write("```\n" + txt + "\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
