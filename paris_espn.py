#!/usr/bin/env python3
"""Outils partagés pour l'analyse et la construction de tickets combinés,
toutes compétitions couvertes par ESPN (cotes DraftKings publiées par ESPN).

Principe « factuel » : la probabilité d'un pari est celle du marché, marge du
bookmaker retirée (normalisation proportionnelle des cotes 1N2 et plus/moins).
Aucun modèle maison n'intervient ici : c'est la source la plus fiable d'après
nos bilans (voir LECONS.md).
"""
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from ucl_bot import get_json, vers_paris

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard?dates={}"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/summary?event={}"
NEWS = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/news?team={}&limit=50"
CACHE = "cache"

# Compétitions suivies (code ESPN -> nom en français)
LIGUES = {
    "uefa.champions": "Ligue des champions",
    "uefa.europa": "Ligue Europa",
    "uefa.europa.conf": "Ligue Conférence",
    "eng.1": "Premier League",
    "eng.2": "Championship",
    "esp.1": "Liga",
    "ita.1": "Serie A",
    "ger.1": "Bundesliga",
    "fra.1": "Ligue 1",
    "por.1": "Liga Portugal",
    "ned.1": "Eredivisie",
    "bel.1": "Pro League (Belgique)",
    "tur.1": "Süper Lig",
    "sco.1": "Premiership (Écosse)",
}

# Marge supposée quand aucune cote de marché n'est publiée (on retire ~6 %)
MARGE_PAR_DEFAUT = 1.06

MOTS_ABSENCE = ("injur", "ruled out", "out for", "suspend", "doubt", "sidelined",
                "miss ", "misses", "surgery", "blow", "absence", "hamstring", "knee",
                "ankle", "illness", "rested", "rotation")
MOTS_FAUX_POSITIFS = ("out of contract", "contract", "transfer rumor", "loan")


# ---------------------------------------------------------------- utilitaires

def us_vers_decimal(x):
    """Cote américaine ('+600', '-215', 'EVEN', -150) -> cote décimale."""
    if x is None:
        return None
    if isinstance(x, str):
        s = x.strip().upper()
        if s in ("", "OFF", "N/A", "-"):
            return None
        if s == "EVEN":
            return 2.0
        try:
            x = float(s.replace("+", ""))
        except ValueError:
            return None
    x = float(x)
    if x == 0:
        return None
    return 1 + (100 / abs(x) if x < 0 else x / 100)


def parse_utc(s):
    """Date ESPN ('2026-11-26T17:45Z', '2026-11-22T21:13:47Z'...) -> datetime UTC."""
    s = (s or "").strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        d = datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def maintenant_utc():
    return datetime.now(timezone.utc)


# ------------------------------------------------------- noms d'équipes

STOP = {"fc", "cf", "afc", "sc", "ac", "as", "club", "de", "d", "the", "f", "c", "cd",
        "ud", "sk", "fk", "bk", "if", "ssc", "sv", "rc", "rcd", "ca", "cp", "sd",
        "calcio", "football", "futbol", "and", "hove"}

ALIAS = {
    "inter milan": "internazionale",
    "inter milano": "internazionale",
    "copenhague": "kobenhavn",
    "copenhagen": "kobenhavn",
    "wolves": "wolverhampton wanderers",
    "west brom": "west bromwich albion",
    "psg": "paris saint germain",
    "bayern": "bayern munich",
    "bayern munchen": "bayern munich",
    "man united": "manchester united",
    "man utd": "manchester united",
    "man city": "manchester city",
    "atletico": "atletico madrid",
    "atletico de madrid": "atletico madrid",
    "naples": "napoli",
    "naple": "napoli",
    "spurs": "tottenham hotspur",
    "bruges": "club brugge",
    "seville": "sevilla",
    "lisbonne": "sporting",
    "sporting lisbonne": "sporting",
    "marseille": "olympique marseille",
    "om": "olympique marseille",
    "ol": "olympique lyonnais",
    "lyon": "olympique lyonnais",
    "rome": "roma",
    "as roma": "roma",
    "glasgow rangers": "rangers",
}


_SPECIAUX = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "đ": "d",
                           "Đ": "D", "ł": "l", "Ł": "L", "ı": "i", "œ": "oe", "Œ": "OE"})


def _ascii(s):
    s = (s or "").translate(_SPECIAUX)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def jetons(nom):
    s = _ascii(nom)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    s = ALIAS.get(s, s)
    return [t for t in s.split() if t not in STOP]


def _jeton_egal(a, b):
    if a == b:
        return True
    if len(a) >= 2 and len(b) >= 2 and (a.startswith(b) or b.startswith(a)):
        return True
    return False


def similarite(a, b):
    """Similarité entre deux noms d'équipe (0 à 1), tolérante aux abréviations."""
    ta, tb = jetons(a), jetons(b)
    if not ta or not tb:
        return 0.0
    if " ".join(ta) == " ".join(tb):
        return 1.0
    commun_a = sum(1 for x in ta if any(_jeton_egal(x, y) for y in tb))
    commun_b = sum(1 for y in tb if any(_jeton_egal(x, y) for x in ta))
    couverture = min(commun_a / len(ta), commun_b / len(tb))
    inclusion = max(commun_a / len(ta), commun_b / len(tb))
    return 0.6 * couverture + 0.4 * inclusion


# ---------------------------------------------------------- tableaux ESPN

def _cache_path(ligue, ymd):
    return os.path.join(CACHE, f"sb_{ligue}_{ymd}.json")


def scoreboard(ligue, jour):
    """Matchs d'une compétition pour une date ESPN (datetime.date). Les jours
    entièrement terminés sont mis en cache ; les autres sont toujours relus."""
    ymd = jour.strftime("%Y%m%d")
    chemin = _cache_path(ligue, ymd)
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    try:
        data = get_json(SCOREBOARD.format(ligue, ymd))
    except RuntimeError:
        return {"events": []}
    evs = data.get("events") or []
    if evs and all(e["competitions"][0]["status"]["type"].get("completed") for e in evs):
        os.makedirs(CACHE, exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    return data


def _ml(bloc, cote):
    try:
        v = bloc[cote]["close"]["odds"]
    except (KeyError, TypeError):
        v = None
    if v is None:
        try:
            v = bloc[cote]["open"]["odds"]
        except (KeyError, TypeError):
            v = None
    return us_vers_decimal(v)


def _ml_ouverture(bloc, cote):
    try:
        return us_vers_decimal(bloc[cote]["open"]["odds"])
    except (KeyError, TypeError):
        return None


def extraire_cotes(competition):
    """Cotes DraftKings (1N2 + ligne de buts) depuis le tableau ESPN."""
    odds = [o for o in (competition.get("odds") or []) if o]
    if not odds:
        return None
    o = odds[0]
    res = {"bookmaker": (o.get("provider") or {}).get("name", "?")}
    ml = o.get("moneyline")
    if ml:
        c1, cn, c2 = _ml(ml, "home"), _ml(ml, "draw"), _ml(ml, "away")
        if c1 and cn and c2:
            res["1"], res["N"], res["2"] = c1, cn, c2
            res["ouverture"] = {"1": _ml_ouverture(ml, "home"), "N": _ml_ouverture(ml, "draw"),
                                "2": _ml_ouverture(ml, "away")}
    if "1" not in res:
        # ancien format : homeTeamOdds / drawOdds / awayTeamOdds
        h = (o.get("homeTeamOdds") or {}).get("moneyLine")
        a = (o.get("awayTeamOdds") or {}).get("moneyLine")
        d = (o.get("drawOdds") or {}).get("moneyLine")
        if h is not None and a is not None and d is not None:
            res["1"], res["N"], res["2"] = us_vers_decimal(h), us_vers_decimal(d), us_vers_decimal(a)
    tot = o.get("total") or {}
    ligne = o.get("overUnder")
    try:
        plus = us_vers_decimal(tot["over"]["close"]["odds"])
        moins = us_vers_decimal(tot["under"]["close"]["odds"])
        lt = tot["over"]["close"].get("line", "")
        if lt and lt[0] in "ou":
            ligne = float(lt[1:])
    except (KeyError, TypeError, ValueError):
        plus = moins = None
    if ligne is not None and plus and moins:
        res["ligne"], res["plus"], res["moins"] = float(ligne), plus, moins
    if "1" not in res and "ligne" not in res:
        return None
    return res


def probas_marche(cotes):
    """Probabilités « réelles » : cotes du marché, marge retirée."""
    p = {}
    if cotes and "1" in cotes:
        inv = [1 / cotes["1"], 1 / cotes["N"], 1 / cotes["2"]]
        s = sum(inv)
        p["1"], p["N"], p["2"] = inv[0] / s, inv[1] / s, inv[2] / s
        p["1N"], p["N2"], p["12"] = p["1"] + p["N"], p["N"] + p["2"], p["1"] + p["2"]
        p["marge_1n2"] = s - 1
    if cotes and "ligne" in cotes:
        a, b = 1 / cotes["plus"], 1 / cotes["moins"]
        s = a + b
        p[f"+{cotes['ligne']}"] = a / s
        p[f"-{cotes['ligne']}"] = b / s
    return p


def match_depuis_event(e, ligue):
    c = e["competitions"][0]
    h = next(t for t in c["competitors"] if t["homeAway"] == "home")
    a = next(t for t in c["competitors"] if t["homeAway"] == "away")
    st = c["status"]["type"]
    debut = parse_utc(e["date"])

    def score(t):
        try:
            return int(t.get("score"))
        except (TypeError, ValueError):
            return None

    cotes = extraire_cotes(c)
    return {
        "id": e["id"],
        "ligue": ligue,
        "competition": LIGUES.get(ligue, ligue),
        "debut_utc": debut,
        "debut_paris": vers_paris(debut),
        "domicile": h["team"]["displayName"],
        "exterieur": a["team"]["displayName"],
        "id_domicile": h["team"]["id"],
        "id_exterieur": a["team"]["id"],
        "etat": st.get("state"),          # pre / in / post
        "termine": bool(st.get("completed")),
        "statut": st.get("shortDetail") or st.get("detail") or st.get("description"),
        "chrono": c["status"].get("displayClock"),
        "score": (score(h), score(a)),
        "forme": (h.get("form"), a.get("form")),
        "bilan": (_bilan(h), _bilan(a)),
        "cotes": cotes,
        "probas": probas_marche(cotes),
    }


def _bilan(t):
    for r in t.get("records") or []:
        if r.get("summary"):
            return r["summary"]
    return None


def matchs_du_jour_paris(ligue, jour_paris):
    """Tous les matchs d'une compétition dont le coup d'envoi (heure de Paris)
    tombe le jour donné. ESPN groupe par date américaine : on lit J-1, J, J+1."""
    vus, res = set(), []
    for delta in (-1, 0, 1):
        data = scoreboard(ligue, jour_paris + timedelta(days=delta))
        for e in data.get("events") or []:
            if e["id"] in vus:
                continue
            vus.add(e["id"])
            m = match_depuis_event(e, ligue)
            if m["debut_paris"].date() == jour_paris:
                res.append(m)
    return sorted(res, key=lambda m: m["debut_utc"])


def trouver_match(ligue, jour_paris, domicile, exterieur):
    """Retrouve un match à partir des noms (tolérant) ; renvoie (match, score)."""
    meilleur, best = None, 0.0
    for decalage in (0, -1, 1):
        for m in matchs_du_jour_paris(ligue, jour_paris + timedelta(days=decalage)):
            s = min(similarite(domicile, m["domicile"]), similarite(exterieur, m["exterieur"]))
            if decalage:
                s -= 0.05
            if s > best:
                meilleur, best = m, s
        if best >= 0.75:
            break
    return (meilleur, best) if best >= 0.5 else (None, best)


# ------------------------------------------------------------- les paris

LIBELLES = {
    "1": "Victoire {dom}", "N": "Match nul", "2": "Victoire {ext}",
    "1N": "{dom} ou nul", "N2": "{ext} ou nul", "12": "Pas de match nul",
}


def normaliser_pari(pari):
    """'1X2. V2' -> '2', 'V1' -> '1', 'X' -> 'N', '1X' -> '1N', '+2.5' ..."""
    s = pari.upper().replace("1X2.", "").replace("1X2", "").strip()
    s = s.replace("X", "N")
    table = {"V1": "1", "W1": "1", "P1": "1", "V2": "2", "W2": "2", "P2": "2",
             "NUL": "N", "DRAW": "N", "1N": "1N", "N1": "1N", "N2": "N2", "2N": "N2",
             "12": "12", "21": "12"}
    s = table.get(s, s)
    m = re.match(r"^(PLUS|MOINS|OVER|UNDER|\+|-)\s*(DE)?\s*([0-9]+(?:[.,]5)?)", s)
    if m:
        signe = "+" if m.group(1) in ("PLUS", "OVER", "+") else "-"
        return f"{signe}{float(m.group(3).replace(',', '.'))}"
    return s


def libelle_pari(code, m):
    if code in LIBELLES:
        return LIBELLES[code].format(dom=m["domicile"], ext=m["exterieur"])
    if code[0] in "+-":
        return f"{'Plus' if code[0] == '+' else 'Moins'} de {code[1:]} buts"
    return code


def pari_gagnant(code, h, a):
    """Le pari passe-t-il avec ce score ? (None si type inconnu)"""
    if code == "1":
        return h > a
    if code == "N":
        return h == a
    if code == "2":
        return a > h
    if code == "1N":
        return h >= a
    if code == "N2":
        return a >= h
    if code == "12":
        return h != a
    if code[0] in "+-":
        ligne = float(code[1:])
        return (h + a > ligne) if code[0] == "+" else (h + a < ligne)
    return None


def proba_pari(code, m, cote_ticket=None):
    """(probabilité, source) pour un pari sur un match."""
    p = m.get("probas") or {}
    if code in p:
        return p[code], f"marché {m['cotes'].get('bookmaker', '?')} (marge retirée)"
    if cote_ticket:
        return (1 / cote_ticket) / MARGE_PAR_DEFAUT, "estimée depuis ta cote (marge ~6 % retirée)"
    return None, "aucune cote disponible"


def cote_marche(code, m):
    c = m.get("cotes") or {}
    if code in ("1", "N", "2"):
        return c.get(code)
    if code[0] in "+-" and c.get("ligne") is not None and float(code[1:]) == c["ligne"]:
        return c["plus"] if code[0] == "+" else c["moins"]
    return None


def cote_ouverture(code, m):
    c = (m.get("cotes") or {}).get("ouverture") or {}
    return c.get(code)


# --------------------------------------------------------- garde-fou actu

def alertes_actu(ligue, team_id, nom_equipe, jours=7):
    """Titres ESPN récents qui parlent de blessures / suspensions / rotation
    pour l'équipe. À vérifier par un humain : c'est une recherche par mots-clés."""
    try:
        data = get_json(NEWS.format(ligue, team_id))
    except RuntimeError:
        return []
    limite = maintenant_utc() - timedelta(days=jours)
    res = []
    for art in data.get("articles") or []:
        try:
            pub = parse_utc(art.get("published", ""))
        except ValueError:
            continue
        if pub < limite:
            continue
        titre = art.get("headline") or ""
        texte = (titre + " " + (art.get("description") or "")).lower()
        if any(f in texte for f in MOTS_FAUX_POSITIFS):
            continue
        cible = " ".join(jetons(nom_equipe))
        concerne = any(t in _ascii(texte) for t in cible.split() if len(t) >= 4)
        if concerne and any(k in texte for k in MOTS_ABSENCE):
            lien = ((art.get("links") or {}).get("web") or {}).get("href", "")
            res.append({"date": pub.date().isoformat(), "titre": titre, "lien": lien})
    return res


def fmt_pct(x):
    return "—" if x is None else f"{100 * x:.0f} %"


def fmt_heure(m):
    d = m["debut_paris"]
    return f"{d.day:02d}/{d.month:02d} {d.hour:02d}h{d.minute:02d}"
