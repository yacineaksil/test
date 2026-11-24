#!/usr/bin/env python3
"""Robot Ligue des champions : résultats détaillés des matchs d'une journée.

Source : API publique (sans clé) d'ESPN.
Bibliothèque standard uniquement (urllib, json, argparse).

Exemples :
    python3 ucl_bot.py                       # aujourd'hui (heure de Paris)
    python3 ucl_bot.py --date 2026-11-04
    python3 ucl_bot.py --date 2026-11-04 --out resultats/
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions"
# NB : un User-Agent contenant « bot » est refusé (HTTP 403) par ESPN.
USER_AGENT = "ucl-resultats/1.0"

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

# Statuts ESPN -> libellé français
STATUTS = {
    "STATUS_SCHEDULED": "À venir",
    "STATUS_FIRST_HALF": "En cours (1re mi-temps)",
    "STATUS_HALFTIME": "Mi-temps",
    "STATUS_SECOND_HALF": "En cours (2e mi-temps)",
    "STATUS_IN_PROGRESS": "En cours",
    "STATUS_END_OF_REGULATION": "Fin du temps réglementaire",
    "STATUS_EXTRA_TIME": "Prolongation",
    "STATUS_OVERTIME": "Prolongation",
    "STATUS_END_OF_EXTRATIME": "Fin des prolongations",
    "STATUS_SHOOTOUT": "Tirs au but en cours",
    "STATUS_FULL_TIME": "Terminé",
    "STATUS_FINAL": "Terminé",
    "STATUS_FINAL_AET": "Terminé (après prolongation)",
    "STATUS_FINAL_PEN": "Terminé (aux tirs au but)",
    "STATUS_POSTPONED": "Reporté",
    "STATUS_CANCELED": "Annulé",
    "STATUS_ABANDONED": "Arrêté",
    "STATUS_SUSPENDED": "Suspendu",
    "STATUS_DELAYED": "Retardé",
}


# --------------------------------------------------------------------------
# Fuseau horaire Europe/Paris (zoneinfo si dispo, sinon règle UE manuelle)
# --------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # Python < 3.9 ou base tz absente (Windows sans tzdata)
    PARIS = None


def _dernier_dimanche(annee, mois):
    d = date(annee, mois + 1, 1) - timedelta(days=1) if mois < 12 else date(annee, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def vers_paris(dt_utc):
    """Convertit un datetime UTC (aware) en heure de Paris."""
    if PARIS is not None:
        return dt_utc.astimezone(PARIS)
    a = dt_utc.year
    debut = datetime.combine(_dernier_dimanche(a, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    fin = datetime.combine(_dernier_dimanche(a, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    decalage = 2 if debut <= dt_utc < fin else 1
    return dt_utc.astimezone(timezone(timedelta(hours=decalage)))


def aujourdhui_paris():
    return vers_paris(datetime.now(timezone.utc)).date()


def date_longue(d):
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


# --------------------------------------------------------------------------
# Accès HTTP
# --------------------------------------------------------------------------
def get_json(url, essais=3):
    derniere_erreur = None
    for i in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            derniere_erreur = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"Échec de la requête {url} : {derniere_erreur}")


def parse_utc(s):
    # Formats ESPN : "2026-11-04T20:00Z" ou "2026-11-04T20:00:00Z"
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
def libelle_statut(status):
    t = status.get("type", {})
    nom = t.get("name", "")
    etat = t.get("state", "")
    lib = STATUTS.get(nom)
    if lib is None:
        lib = {"pre": "À venir", "in": "En cours", "post": "Terminé"}.get(etat, t.get("description", nom))
    return nom, etat, lib


def _athletes(ev):
    """Noms des joueurs impliqués dans un keyEvent (summary) ou un detail (scoreboard)."""
    if "participants" in ev:
        return [p.get("athlete", {}).get("displayName", "?") for p in ev["participants"]]
    return [a.get("displayName", "?") for a in ev.get("athletesInvolved", [])]


def extraire_evenements(key_events, details, equipes):
    """Retourne (buts, cartons, penaltys_manques, tab) à partir des keyEvents du summary
    (ou, à défaut, des `details` du scoreboard)."""
    buts, cartons, manques, tab = [], [], [], []
    source = key_events if key_events else details or []
    for ev in source:
        typ = ev.get("type", {}).get("text", "")
        typ_l = typ.lower()
        equipe_id = (ev.get("team") or {}).get("id")
        equipe = equipes.get(equipe_id, {}).get("nom", "?")
        minute = (ev.get("clock") or {}).get("displayValue", "") or ""
        periode = (ev.get("period") or {}).get("number")
        noms = _athletes(ev)
        texte = ev.get("text", "") or ""
        shootout = bool(ev.get("shootout"))

        if shootout:
            if "goal" in typ_l or "scored" in typ_l or "penalty" in typ_l or ev.get("scoringPlay"):
                tab.append({"equipe": equipe, "joueur": noms[0] if noms else "?",
                            "reussi": ("scored" in typ_l or "goal" in typ_l) and "miss" not in typ_l
                            and "saved" not in typ_l})
            continue

        est_csc = typ_l.startswith("own goal") or ev.get("ownGoal") is True
        est_peno = typ_l.startswith("penalty - scored") or ev.get("penaltyKick") is True
        est_but = ev.get("scoringPlay") is True or est_csc or typ_l.startswith("goal")

        if est_but:
            buteur = noms[0] if noms else "?"
            passeur = None
            if not est_csc and not est_peno and len(noms) > 1:
                passeur = noms[1]
            nature = "csc" if est_csc else ("penalty" if est_peno else "normal")
            if not est_csc and not est_peno:
                if "header" in typ_l:
                    nature_detail = "de la tête"
                elif "free-kick" in typ_l or "free kick" in typ_l:
                    nature_detail = "sur coup franc"
                else:
                    nature_detail = None
            else:
                nature_detail = None
            buts.append({"minute": minute, "periode": periode, "equipe": equipe,
                         "equipe_id": equipe_id, "buteur": buteur, "passeur": passeur,
                         "type": nature, "precision": nature_detail})
        elif "red card" in typ_l or ev.get("redCard") is True:
            second = "second yellow" in texte.lower()
            cartons.append({"minute": minute, "equipe": equipe, "joueur": noms[0] if noms else "?",
                            "couleur": "rouge", "second_jaune": second})
        elif "yellow card" in typ_l or ev.get("yellowCard") is True:
            cartons.append({"minute": minute, "equipe": equipe, "joueur": noms[0] if noms else "?",
                            "couleur": "jaune", "second_jaune": False})
        elif typ_l.startswith("penalty - missed") or typ_l.startswith("penalty - saved"):
            manques.append({"minute": minute, "equipe": equipe, "joueur": noms[0] if noms else "?",
                            "issue": "arrêté" if "saved" in typ_l else "manqué"})
    return buts, cartons, manques, tab


def construire_match(event, summary):
    comp = event["competitions"][0]
    header_comp = {}
    if summary:
        header_comp = (summary.get("header", {}).get("competitions") or [{}])[0]

    # Le header du summary est plus à jour ; sinon scoreboard.
    competiteurs = header_comp.get("competitors") or comp["competitors"]
    status = header_comp.get("status") or comp.get("status") or event.get("status", {})
    nom_statut, etat, lib_statut = libelle_statut(status)

    equipes = {}
    dom = ext = None
    for c in competiteurs:
        t = c.get("team", {})
        info = {
            "id": t.get("id"),
            "nom": t.get("displayName") or t.get("name"),
            "abrev": t.get("abbreviation"),
            "score": int(c["score"]) if str(c.get("score", "")).isdigit() else None,
            "mi_temps": None,
            "tab": c.get("shootoutScore"),
            "vainqueur": c.get("winner"),
        }
        ls = c.get("linescores") or []
        if ls and etat != "pre":
            v = ls[0].get("displayValue", ls[0].get("value"))
            try:
                info["mi_temps"] = int(float(v))
            except (TypeError, ValueError):
                pass
        equipes[info["id"]] = info
        if c.get("homeAway") == "home":
            dom = info
        else:
            ext = info

    key_events = (summary or {}).get("keyEvents") or []
    buts, cartons, manques, tab = extraire_evenements(key_events, comp.get("details"), equipes)

    # Score à la mi-temps : si non fourni, on le reconstitue depuis les buts de la 1re période.
    if etat != "pre" and (dom["mi_temps"] is None or ext["mi_temps"] is None):
        if any(b["periode"] for b in buts) or not buts:
            dom["mi_temps"] = sum(1 for b in buts if b["periode"] == 1 and b["equipe_id"] == dom["id"])
            ext["mi_temps"] = sum(1 for b in buts if b["periode"] == 1 and b["equipe_id"] == ext["id"])
            # Seulement fiable si la 1re mi-temps est terminée
            if nom_statut == "STATUS_FIRST_HALF":
                dom["mi_temps"] = ext["mi_temps"] = None

    coup_envoi_utc = parse_utc(event.get("date") or comp.get("date"))
    coup_envoi_paris = vers_paris(coup_envoi_utc)

    venue = (comp.get("venue") or {})
    if summary and summary.get("gameInfo", {}).get("venue"):
        venue = summary["gameInfo"]["venue"]
    stade = venue.get("fullName")
    ville = (venue.get("address") or {}).get("city")

    return {
        "id": event["id"],
        "coup_envoi_utc": coup_envoi_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coup_envoi_paris": coup_envoi_paris.strftime("%Y-%m-%d %H:%M"),
        "heure_paris": coup_envoi_paris.strftime("%Hh%M"),
        "statut_code": nom_statut,
        "etat": etat,
        "statut": lib_statut,
        "chrono": status.get("displayClock") if etat == "in" else None,
        "domicile": dom,
        "exterieur": ext,
        "buts": buts,
        "cartons": cartons,
        "penaltys_manques": manques,
        "tirs_au_but": tab,
        "stade": stade,
        "ville": ville,
        "affluence": (summary or {}).get("gameInfo", {}).get("attendance"),
        "phase": " ".join(n.get("headline", "") for n in (comp.get("notes") or []) if n.get("headline")) or None,
        "lien": next((l.get("href") for l in event.get("links", []) if "summary" in (l.get("rel") or [])), None),
    }


def recuperer_journee(jour):
    """Récupère tous les matchs de C1 dont le coup d'envoi (heure de Paris) tombe le `jour`."""
    # ESPN regroupe les matchs par jour (heure US) et n'accepte pas les plages de dates :
    # on interroge la veille et le jour même, puis on filtre sur la date de Paris.
    evenements = []
    for j in (jour - timedelta(days=1), jour):
        sb = get_json(f"{BASE}/scoreboard?dates={j.strftime('%Y%m%d')}&limit=200")
        evenements.extend(sb.get("events", []))
    matchs = []
    vus = set()
    for ev in evenements:
        if ev["id"] in vus:
            continue
        vus.add(ev["id"])
        if vers_paris(parse_utc(ev["date"])).date() != jour:
            continue
        etat = ev.get("status", {}).get("type", {}).get("state")
        summary = None
        if etat in ("in", "post"):
            try:
                summary = get_json(f"{BASE}/summary?event={ev['id']}")
            except RuntimeError as e:
                print(f"[avertissement] détails indisponibles pour le match {ev['id']} : {e}",
                      file=sys.stderr)
        matchs.append(construire_match(ev, summary))
    matchs.sort(key=lambda m: (m["coup_envoi_utc"], m["domicile"]["nom"] or ""))
    return matchs


# --------------------------------------------------------------------------
# Mise en forme (français)
# --------------------------------------------------------------------------
def fmt_but(b):
    s = f"{b['minute']} {b['buteur']}"
    if b["type"] == "penalty":
        s += " (sur penalty)"
    elif b["type"] == "csc":
        s += " (contre son camp)"
    elif b["precision"]:
        s += f" ({b['precision']})"
    if b["passeur"]:
        s += f", passe décisive de {b['passeur']}"
    return s


def fmt_carton(c):
    if c["couleur"] == "rouge":
        icone = "🟥"
        suffixe = " (second jaune)" if c["second_jaune"] else ""
    else:
        icone = "🟨"
        suffixe = ""
    return f"{icone} {c['minute']} {c['joueur']}{suffixe}"


def ligne_score(m):
    d, e = m["domicile"], m["exterieur"]
    if m["etat"] == "pre" or d["score"] is None:
        return f"{d['nom']} - {e['nom']}"
    s = f"{d['nom']} {d['score']} - {e['score']} {e['nom']}"
    if d.get("tab") is not None and e.get("tab") is not None:
        s += f" ({d['tab']}-{e['tab']} aux t.a.b.)"
    return s


def bloc_match_texte(m):
    lignes = []
    d, e = m["domicile"], m["exterieur"]
    if m["etat"] == "pre":
        lignes.append(f"⏳ {m['heure_paris']}  {ligne_score(m)}  —  {m['statut']}")
    else:
        statut = m["statut"]
        if m["etat"] == "in" and m["chrono"]:
            statut += f", {m['chrono']}"
        icone = "🔴" if m["etat"] == "in" else "✅"
        lignes.append(f"{icone} {ligne_score(m)}  —  {statut}")
        if d["mi_temps"] is not None and e["mi_temps"] is not None:
            lignes.append(f"   Mi-temps : {d['mi_temps']} - {e['mi_temps']}")
    if m["stade"]:
        lieu = m["stade"] + (f", {m['ville']}" if m["ville"] else "")
        lignes.append(f"   Stade : {lieu}  |  Coup d'envoi : {m['heure_paris']} (heure de Paris)")
    for camp in (d, e):
        buts = [b for b in m["buts"] if b["equipe_id"] == camp["id"]]
        if buts:
            lignes.append(f"   ⚽ {camp['nom']} :")
            for b in buts:
                lignes.append(f"      - {fmt_but(b)}")
    for p in m["penaltys_manques"]:
        lignes.append(f"   ❌ Penalty {p['issue']} : {p['minute']} {p['joueur']} ({p['equipe']})")
    if m["cartons"]:
        lignes.append("   Cartons :")
        for c in m["cartons"]:
            lignes.append(f"      {fmt_carton(c)} ({c['equipe']})")
    return "\n".join(lignes)


def rapport_texte(jour, matchs):
    titre = f"Ligue des champions — {date_longue(jour)}"
    out = [titre, "=" * len(titre)]
    if not matchs:
        out.append("Aucun match de Ligue des champions ce jour-là.")
        return "\n".join(out)
    for m in matchs:
        out.append("")
        out.append(bloc_match_texte(m))
    return "\n".join(out)


def rapport_markdown(jour, matchs):
    genere = vers_paris(datetime.now(timezone.utc)).strftime("%d/%m/%Y à %Hh%M")
    md = [f"# Ligue des champions — {date_longue(jour)}", "",
          f"_Rapport généré le {genere} (heure de Paris). Source : ESPN._", ""]
    if not matchs:
        md.append("Aucun match de Ligue des champions ce jour-là.")
        return "\n".join(md) + "\n"

    md += ["## Résumé", "", "| Heure | Domicile | Score | Extérieur | Mi-temps | Statut |",
           "|---|---|:---:|---|:---:|---|"]
    for m in matchs:
        d, e = m["domicile"], m["exterieur"]
        score = "–" if m["etat"] == "pre" else f"{d['score']} - {e['score']}"
        if d.get("tab") is not None and e.get("tab") is not None:
            score += f" ({d['tab']}-{e['tab']} tab)"
        mt = (f"{d['mi_temps']} - {e['mi_temps']}"
              if d["mi_temps"] is not None and e["mi_temps"] is not None else "–")
        statut = m["statut"] + (f" ({m['chrono']})" if m["chrono"] else "")
        md.append(f"| {m['heure_paris']} | {d['nom']} | **{score}** | {e['nom']} | {mt} | {statut} |")
    md.append("")
    md.append("## Détail des matchs")
    for m in matchs:
        d, e = m["domicile"], m["exterieur"]
        md += ["", f"### {ligne_score(m)}", ""]
        statut = m["statut"] + (f" ({m['chrono']})" if m["chrono"] else "")
        md.append(f"- **Statut :** {statut}")
        md.append(f"- **Coup d'envoi :** {m['heure_paris']} (heure de Paris)")
        if m["etat"] != "pre" and d["mi_temps"] is not None:
            md.append(f"- **Mi-temps :** {d['mi_temps']} - {e['mi_temps']}")
        if m["stade"]:
            md.append(f"- **Stade :** {m['stade']}" + (f", {m['ville']}" if m["ville"] else ""))
        if m["affluence"]:
            md.append(f"- **Affluence :** {m['affluence']:,}".replace(",", " ") + " spectateurs")
        if m["etat"] == "pre":
            continue
        md += ["", "**Buts**", ""]
        if not m["buts"]:
            md.append("_Aucun but._")
        for camp in (d, e):
            buts = [b for b in m["buts"] if b["equipe_id"] == camp["id"]]
            if buts:
                md.append(f"- {camp['nom']} :")
                for b in buts:
                    md.append(f"  - ⚽ {fmt_but(b)}")
        if m["penaltys_manques"]:
            md += ["", "**Penaltys non transformés**", ""]
            for p in m["penaltys_manques"]:
                md.append(f"- ❌ {p['minute']} {p['joueur']} ({p['equipe']}) — {p['issue']}")
        if m["tirs_au_but"]:
            md += ["", "**Séance de tirs au but**", ""]
            for t in m["tirs_au_but"]:
                md.append(f"- {'✅' if t['reussi'] else '❌'} {t['joueur']} ({t['equipe']})")
        md += ["", "**Cartons**", ""]
        if not m["cartons"]:
            md.append("_Aucun carton._")
        for c in m["cartons"]:
            md.append(f"- {fmt_carton(c)} ({c['equipe']})")
    return "\n".join(md) + "\n"


# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="Résultats détaillés de la Ligue des champions (source ESPN).")
    p.add_argument("--date", help="Date au format AAAA-MM-JJ (défaut : aujourd'hui, heure de Paris)")
    p.add_argument("--out", help="Dossier de sortie pour ucl_AAAA-MM-JJ.md et .json")
    args = p.parse_args(argv)

    if args.date:
        try:
            jour = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            p.error("format de date invalide, attendu AAAA-MM-JJ (ex. 2026-11-25)")
    else:
        jour = aujourdhui_paris()

    try:
        matchs = recuperer_journee(jour)
    except RuntimeError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1

    print(rapport_texte(jour, matchs))

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        base = os.path.join(args.out, f"ucl_{jour.isoformat()}")
        with open(base + ".md", "w", encoding="utf-8") as f:
            f.write(rapport_markdown(jour, matchs))
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump({"date": jour.isoformat(), "competition": "UEFA Champions League",
                       "source": "ESPN", "matchs": matchs}, f, ensure_ascii=False, indent=2)
        print(f"\nRapports écrits : {base}.md et {base}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
