#!/usr/bin/env python3
"""Outils partagés pour l'analyse et la construction de tickets combinés,
toutes compétitions couvertes par ESPN (cotes DraftKings publiées par ESPN) :
football et sports américains (NFL, NCAA football, NBA, NCAA basket, NHL,
MLB, WNBA).

Principe « factuel » : la probabilité d'un pari est celle du marché, marge du
bookmaker retirée (normalisation proportionnelle des cotes 1N2, moneyline,
handicap et plus/moins). Aucun modèle maison n'intervient ici : c'est la source
la plus fiable d'après nos bilans (voir LECONS.md). Seules exceptions, toujours
annoncées comme des estimations : le « 1X2 temps réglementaire » des sports US
(DraftKings ne le cote pas) et les probabilités recalculées en direct.
"""
import argparse
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from ucl_bot import get_json, vers_paris

# Messages d'argparse en français (aide --help et erreurs de ligne de commande)
_ARGPARSE_FR = {
    "usage: ": "utilisation : ",
    "options": "options",
    "positional arguments": "arguments",
    "show this help message and exit": "afficher cette aide et quitter",
    "%(prog)s: error: %(message)s\n": "%(prog)s : erreur : %(message)s\n",
    "the following arguments are required: %s": "arguments obligatoires manquants : %s",
    "unrecognized arguments: %s": "arguments inconnus : %s",
    "invalid %(type)s value: %(value)r": "valeur invalide (%(type)s attendu) : %(value)r",
    "expected one argument": "une valeur est attendue",
    "argument %(argument_name)s: %(message)s": "option %(argument_name)s : %(message)s",
    "invalid choice: %(value)r (choose from %(choices)s)": "choix invalide : %(value)r (possibles : %(choices)s)",
    "ambiguous option: %(option)s could match %(matches)s": "option ambiguë : %(option)s peut désigner %(matches)s",
    " (default: %(default)s)": " (défaut : %(default)s)",
}
_gettext_argparse = argparse._
argparse._ = lambda texte: _ARGPARSE_FR.get(texte, _gettext_argparse(texte))

API = "https://site.api.espn.com/apis/site/v2/sports"
# Formats historiques (football seulement), gardés pour les scripts existants.
SCOREBOARD = API + "/soccer/{}/scoreboard?dates={}"
SUMMARY = API + "/soccer/{}/summary?event={}"
NEWS = API + "/soccer/{}/news?team={}&limit=50"
CACHE = "cache"

# Compétitions de football suivies (code ESPN -> nom en français)
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
    # deuxièmes divisions et autres championnats européens
    "eng.3": "League One",
    "esp.2": "Liga 2",
    "ita.2": "Serie B",
    "ger.2": "2. Bundesliga",
    "fra.2": "Ligue 2",
    "gre.1": "Super League (Grèce)",
    "aut.1": "Bundesliga (Autriche)",
    "sui.1": "Super League (Suisse)",
    "den.1": "Superliga (Danemark)",
    # reste du monde
    "usa.1": "MLS",
    "mex.1": "Liga MX",
    "bra.1": "Brasileirão",
    "arg.1": "Liga Profesional (Argentine)",
    "col.1": "Primera A (Colombie)",
    "chi.1": "Primera División (Chili)",
    "ksa.1": "Saudi Pro League",
    "jpn.1": "J1 League",
    "aus.1": "A-League",
}

# Sports américains : code -> (sport ESPN, ligue ESPN, nom en français,
# paramètres d'URL en plus pour le tableau des matchs, deux issues).
# « Deux issues » : le vainqueur se joue prolongation incluse (pas de nul).
SPORTS_US = {
    "nfl": ("football", "nfl", "NFL", "", True),
    "ncaaf": ("football", "college-football", "NCAA football (FBS)", "groups=80&limit=300", True),
    "nba": ("basketball", "nba", "NBA", "", True),
    "ncaab": ("basketball", "mens-college-basketball", "NCAA basket (D1)", "groups=50&limit=400", True),
    "nhl": ("hockey", "nhl", "NHL", "", True),
    "mlb": ("baseball", "mlb", "MLB", "", True),
    "wnba": ("basketball", "wnba", "WNBA", "", True),
}

# Registre complet : code -> (sport, ligue, nom, paramètres, deux issues)
SPORTS = {code: ("soccer", code, nom, "", False) for code, nom in LIGUES.items()}
SPORTS.update(SPORTS_US)

# Groupes utilisables dans --ligues
GROUPES = {
    "usa": list(SPORTS_US) + ["usa.1"],
    "foot": list(LIGUES),
    "tout": list(LIGUES) + list(SPORTS_US),
}

# Paramètres des estimations (direct, temps réglementaire) par sport US.
# periodes x duree (s) = temps réglementaire. sigma / sigma_total : écart-type
# du résultat final (écart, total) autour de la ligne du marché — ordres de
# grandeur publics, à recalibrer avec l'historique. nul_tr : part des matchs
# équilibrés à égalité à la fin du temps réglementaire. lambda_ref : buts (ou
# points) moyens par équipe et par match.
PARAMS = {
    "nfl": {"periodes": 4, "duree": 900, "prolongation": 600, "modele": "normal", "sigma": 13.5,
            "sigma_total": 13.5, "nul_tr": 0.06, "unite": "points"},
    "ncaaf": {"periodes": 4, "duree": 900, "prolongation": 600, "modele": "normal", "sigma": 16.0,
              "sigma_total": 16.5, "nul_tr": 0.05, "unite": "points"},
    "nba": {"periodes": 4, "duree": 720, "prolongation": 300, "modele": "normal", "sigma": 12.5,
            "sigma_total": 18.5, "nul_tr": 0.06, "unite": "points"},
    "wnba": {"periodes": 4, "duree": 600, "prolongation": 300, "modele": "normal", "sigma": 11.0,
             "sigma_total": 14.5, "nul_tr": 0.05, "unite": "points"},
    "ncaab": {"periodes": 2, "duree": 1200, "prolongation": 300, "modele": "normal", "sigma": 11.0,
              "sigma_total": 15.5, "nul_tr": 0.05, "unite": "points"},
    "nhl": {"periodes": 3, "duree": 1200, "modele": "poisson", "lambda_ref": 3.05, "nul_tr": 0.23,
            "unite": "buts", "max_buts": 12},
    "mlb": {"periodes": 9, "duree": None, "modele": "poisson", "lambda_ref": 4.4, "nul_tr": 0.09,
            "unite": "points (runs)", "max_buts": 16},
}

# Marge supposée quand aucune cote de marché n'est publiée (on retire ~6 %)
MARGE_PAR_DEFAUT = 1.06

MOTS_ABSENCE = ("injur", "ruled out", "out for", "suspend", "doubt", "sidelined",
                "miss ", "misses", "surgery", "blow", "absence", "hamstring", "knee",
                "ankle", "illness", "rested", "rotation")
MOTS_FAUX_POSITIFS = ("out of contract", "contract", "transfer rumor", "loan")
# Sports US : vocabulaire des comptes rendus de blessures, repos et titularisations
# (mots entiers : « knee » ne doit pas trouver « kneeled »)
MOTS_ABSENCE_US = ("injury", "injuries", "injured", "ruled out", "out for", "suspended", "suspension",
                   "doubtful", "questionable", "day-to-day", "sidelined", "will miss", "misses",
                   "surgery", "concussion", "torn", "fracture", "fractured", "sprain", "sprained",
                   "hamstring", "knee", "ankle", "illness", "load management", "resting", "rested",
                   "released", "waived", "benched", "will start", "to start", "starting goalie",
                   "scratched", "injured reserve")
_RE_ABSENCE_US = re.compile(r"\b(" + "|".join(re.escape(x) for x in MOTS_ABSENCE_US) + r")\b")


# ---------------------------------------------------------------- registre

def info_ligue(code):
    """Description d'un code de compétition. Un code inconnu est traité comme
    une compétition de football ESPN (comportement historique)."""
    sport, ligue, nom, params, deux = SPORTS.get(code, ("soccer", code, LIGUES.get(code, code), "", False))
    return {"code": code, "sport": sport, "ligue": ligue, "nom": nom, "params": params,
            "deux_issues": deux}


def est_deux_issues(code):
    return bool(code) and info_ligue(code)["deux_issues"]


def nom_competition(code):
    return info_ligue(code)["nom"]


def developper_ligues(texte):
    """'usa,eng.1' -> codes ESPN, groupes développés (usa, foot, tout), sans doublon."""
    res = []
    for x in (texte or "").split(","):
        x = x.strip()
        if not x:
            continue
        for code in GROUPES.get(x.lower(), [x]):
            if code not in res:
                res.append(code)
    return res


def _racine(code):
    i = info_ligue(code)
    return f"{API}/{i['sport']}/{i['ligue']}"


def url_scoreboard(code, ymd):
    params = info_ligue(code)["params"]
    return f"{_racine(code)}/scoreboard?dates={ymd}" + (f"&{params}" if params else "")


def url_summary(code, event_id):
    return f"{_racine(code)}/summary?event={event_id}"


def url_news(code, team_id):
    return f"{_racine(code)}/news?team={team_id}&limit=50"


def url_blessures(code):
    return f"{_racine(code)}/injuries"


def url_calendrier(code, team_id):
    return f"{_racine(code)}/teams/{team_id}/schedule"


def url_equipe(code, team_id):
    return f"{_racine(code)}/teams/{team_id}"


_MEMO = {}


def get_json_memo(url):
    """Lecture ESPN gardée en mémoire le temps d'une exécution (None si échec)."""
    if url not in _MEMO:
        try:
            _MEMO[url] = get_json(url)
        except RuntimeError:
            _MEMO[url] = None
    return _MEMO[url]


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


try:
    from zoneinfo import ZoneInfo
    _NEW_YORK = ZoneInfo("America/New_York")
except Exception:  # pas de base des fuseaux : heure de l'Est en hiver
    _NEW_YORK = timezone(timedelta(hours=-5))


def date_us(d_utc):
    """Date du calendrier américain (heure de l'Est) d'un instant UTC."""
    return d_utc.astimezone(_NEW_YORK).date()


def fmt_ligne(x):
    """-5.5 -> '-5.5', 3 -> '+3', 0 -> '+0'."""
    x = float(x)
    if x == 0:
        x = 0.0
    return format(x, "+g")


def fmt_nombre(x):
    return format(float(x), "g")


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _phi_inv(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    lo, hi = -8.0, 8.0
    for _ in range(60):
        mil = (lo + hi) / 2
        if _phi(mil) < p:
            lo = mil
        else:
            hi = mil
    return (lo + hi) / 2


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


# Écritures courantes des noms américains chez les bookmakers
_US_REMPLACEMENTS = ((r"\blos angeles\b", "la"), (r"\bny\b", "new york"), (r"\bsaint\b", "st"),
                     (r"\bst\.", "st"), (r"\bmtl\b", "montreal"), (r"\butah hc\b", "utah"))


def _norm_us(nom):
    s = _ascii(nom)
    for motif, remplace in _US_REMPLACEMENTS:
        s = re.sub(motif, remplace, s)
    return s


def similarite_us(nom, alternatives):
    """Similarité d'un nom saisi avec une équipe US (nom complet, court, surnom,
    abréviation exacte). Utilisée seulement pour les sports américains."""
    if not nom:
        return 0.0
    n = _norm_us(nom)
    best = 0.0
    for i, alt in enumerate(alternatives or []):
        if not alt:
            continue
        if i == len(alternatives) - 1:   # abréviation : seulement à l'identique
            if n.strip().replace(" ", "") == _ascii(alt):
                best = max(best, 1.0)
            continue
        best = max(best, similarite(n, _norm_us(alt)))
    return best


# ---------------------------------------------------------- tableaux ESPN

def _cache_path(ligue, ymd):
    return os.path.join(CACHE, f"sb_{ligue}_{ymd}.json")


_SB_MEMO = {}


def scoreboard(ligue, jour):
    """Matchs d'une compétition pour une date ESPN (datetime.date). Les jours
    entièrement terminés sont mis en cache sur disque ; les autres sont relus
    (gardés en mémoire le temps d'une exécution seulement)."""
    ymd = jour.strftime("%Y%m%d")
    chemin = _cache_path(ligue, ymd)
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    if (ligue, ymd) in _SB_MEMO:
        return _SB_MEMO[(ligue, ymd)]
    try:
        data = get_json(url_scoreboard(ligue, ymd))
    except RuntimeError:
        return {"events": []}
    evs = data.get("events") or []
    if evs and all(e["competitions"][0]["status"]["type"].get("completed") for e in evs):
        os.makedirs(CACHE, exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    _SB_MEMO[(ligue, ymd)] = data
    return data


def _ml(bloc, cote, moment="close"):
    try:
        v = bloc[cote][moment]["odds"]
    except (KeyError, TypeError):
        v = None
    if v is None and moment == "close":
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


def _ligne_cote(bloc, cote, moment):
    """(ligne, cote décimale) d'un bloc ESPN {'line': '+4.5', 'odds': '-110'}."""
    try:
        x = bloc[cote][moment]
        ligne = str(x.get("line", "")).strip().lower().lstrip("ou")
        return float(ligne), us_vers_decimal(x.get("odds"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, None


def extraire_cotes(competition, deux_issues=False, moment="close"):
    """Cotes DraftKings depuis le tableau ESPN (ou le « pickcenter » du résumé).
    Football : 1N2 + ligne de buts. Sports US : moneyline, handicap, total."""
    odds = [o for o in (competition.get("odds") or []) if o]
    if not odds:
        return None
    o = odds[0]
    if deux_issues:
        return _cotes_deux_issues(o, moment)
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


def _cotes_deux_issues(o, moment="close"):
    """Sports US : moneyline (prolongation incluse), handicap (ligne du
    domicile, l'extérieur a la ligne opposée) et total, avec l'ouverture."""
    res = {"bookmaker": (o.get("provider") or {}).get("name", "?"), "deux_issues": True}
    ml = o.get("moneyline") or {}
    c1, c2 = _ml(ml, "home", moment), _ml(ml, "away", moment)
    if not (c1 and c2) and moment == "close":
        # ancien format (pickcenter) : homeTeamOdds / awayTeamOdds
        c1 = us_vers_decimal((o.get("homeTeamOdds") or {}).get("moneyLine"))
        c2 = us_vers_decimal((o.get("awayTeamOdds") or {}).get("moneyLine"))
    if c1 and c2:
        res["1"], res["2"] = c1, c2
        o1, o2 = _ml_ouverture(ml, "home"), _ml_ouverture(ml, "away")
        if o1 and o2:
            res["ouverture"] = {"1": o1, "2": o2}
    ps = o.get("pointSpread") or {}
    lh, ch = _ligne_cote(ps, "home", moment)
    la, ca = _ligne_cote(ps, "away", moment)
    if (lh is None or not ch or not ca) and moment == "close":
        try:
            lh = float(o.get("spread"))
            ch = us_vers_decimal((o.get("homeTeamOdds") or {}).get("spreadOdds"))
            ca = us_vers_decimal((o.get("awayTeamOdds") or {}).get("spreadOdds"))
            la = -lh
        except (TypeError, ValueError):
            lh = None
    if lh is not None and la is not None and ch and ca and abs(lh + la) < 1e-6:
        res["hcp"] = {"ligne": lh, "1": ch, "2": ca}
        loh, coh = _ligne_cote(ps, "home", "open")
        loa, coa = _ligne_cote(ps, "away", "open")
        if loh is not None and loa is not None and coh and coa and abs(loh + loa) < 1e-6:
            res["hcp_ouverture"] = {"ligne": loh, "1": coh, "2": coa}
    tot = o.get("total") or {}
    lt, plus = _ligne_cote(tot, "over", moment)
    _, moins = _ligne_cote(tot, "under", moment)
    if (lt is None or not plus or not moins) and moment == "close":
        try:
            lt = float(o.get("overUnder"))
            plus, moins = us_vers_decimal(o.get("overOdds")), us_vers_decimal(o.get("underOdds"))
        except (TypeError, ValueError):
            lt = None
    if lt is not None and plus and moins:
        res["ligne"], res["plus"], res["moins"] = float(lt), plus, moins
        lo, po = _ligne_cote(tot, "over", "open")
        _, mo = _ligne_cote(tot, "under", "open")
        if lo is not None and po and mo:
            res["total_ouverture"] = {"ligne": lo, "plus": po, "moins": mo}
    if "1" not in res and "hcp" not in res and "ligne" not in res:
        return None
    return res


def code_hcp(cote, ligne):
    return f"{cote} {fmt_ligne(ligne)}"


# Retrait de la marge : « proportionnelle » (défaut historique) ou « puissance »
# (p_i = (1/cote_i)^k avec k tel que la somme fasse 1). La méthode puissance
# reporte davantage de marge sur les outsiders (biais favori-outsider des
# bookmakers) : à privilégier pour les grosses cotes.
METHODE_DEMARGE = "proportionnelle"


def _demarge(inv):
    s = sum(inv)
    if METHODE_DEMARGE != "puissance" or s <= 1:
        return [x / s for x in inv]
    lo, hi = 1.0, 4.0
    for _ in range(60):
        k = (lo + hi) / 2
        if sum(x ** k for x in inv) > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    q = [x ** k for x in inv]
    t = sum(q)
    return [x / t for x in q]


def probas_marche(cotes):
    """Probabilités « réelles » : cotes du marché, marge retirée."""
    p = {}
    if cotes and (cotes.get("deux_issues") or ("1" in cotes and "N" not in cotes)):
        if "1" in cotes and "2" in cotes:
            a, b = 1 / cotes["1"], 1 / cotes["2"]
            p["1"], p["2"] = _demarge([a, b])
            p["marge_ml"] = a + b - 1
        h = cotes.get("hcp")
        if h:
            a, b = 1 / h["1"], 1 / h["2"]
            p[code_hcp("1", h["ligne"])], p[code_hcp("2", -h["ligne"])] = _demarge([a, b])
        if "ligne" in cotes:
            a, b = 1 / cotes["plus"], 1 / cotes["moins"]
            p[f"+{cotes['ligne']}"], p[f"-{cotes['ligne']}"] = _demarge([a, b])
        return p
    if cotes and "1" in cotes:
        inv = [1 / cotes["1"], 1 / cotes["N"], 1 / cotes["2"]]
        s = sum(inv)
        p["1"], p["N"], p["2"] = _demarge(inv)
        p["1N"], p["N2"], p["12"] = p["1"] + p["N"], p["N"] + p["2"], p["1"] + p["2"]
        p["marge_1n2"] = s - 1
    if cotes and "ligne" in cotes:
        a, b = 1 / cotes["plus"], 1 / cotes["moins"]
        p[f"+{cotes['ligne']}"], p[f"-{cotes['ligne']}"] = _demarge([a, b])
    return p


def _score_tr(c, n_periodes):
    """Score à la fin du temps réglementaire (somme des périodes réglementaires)."""
    res = []
    for t in sorted(c["competitors"], key=lambda t: t["homeAway"] != "home"):
        tot, vu = 0.0, False
        for i, ls in enumerate(t.get("linescores") or [], 1):
            per = ls.get("period", i)
            if per <= n_periodes:
                try:
                    tot += float(ls.get("value", ls.get("displayValue")) or 0)
                    vu = True
                except (TypeError, ValueError):
                    pass
        res.append(int(tot) if vu else None)
    return tuple(res)


def _noms_equipe(t):
    eq = t.get("team") or {}
    return [eq.get("displayName"), eq.get("shortDisplayName"), eq.get("name"),
            " ".join(x for x in (eq.get("location"), eq.get("name")) if x), eq.get("abbreviation")]


def _partants(t):
    """Gardien (NHL) ou lanceur (MLB) partant annoncé par ESPN."""
    for p in t.get("probables") or []:
        if p.get("name") in ("probableStartingGoalie", "probableStartingPitcher"):
            a = p.get("athlete") or {}
            return {"id": str(a.get("id", "")), "nom": a.get("displayName"),
                    "statut": (p.get("status") or {}).get("name")}
    return None


def match_depuis_event(e, ligue):
    c = e["competitions"][0]
    h = next(t for t in c["competitors"] if t["homeAway"] == "home")
    a = next(t for t in c["competitors"] if t["homeAway"] == "away")
    st = c["status"]["type"]
    debut = parse_utc(e["date"])
    info = info_ligue(ligue)
    deux = info["deux_issues"]

    def score(t):
        try:
            return int(t.get("score"))
        except (TypeError, ValueError):
            return None

    cotes = extraire_cotes(c, deux)
    m = {
        "id": e["id"],
        "ligue": ligue,
        "competition": info["nom"],
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
    if deux:
        par = PARAMS.get(ligue, {})
        m.update({
            "deux_issues": True,
            "sport": info["sport"],
            "periode": c["status"].get("period"),
            "horloge": c["status"].get("clock"),
            "type_statut": st.get("name"),
            "score_tr": _score_tr(c, par.get("periodes", 99)),
            "neutre": bool(c.get("neutralSite")),
            "noms_dom": _noms_equipe(h),
            "noms_ext": _noms_equipe(a),
            "rangs": ((h.get("curatedRank") or {}).get("current"), (a.get("curatedRank") or {}).get("current")),
            "partants": (_partants(h), _partants(a)),
        })
    return m


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
    """Retrouve un match à partir des noms (tolérant) ; renvoie (match, score).
    Sports US : l'ordre des équipes peut être inversé chez le bookmaker
    (« extérieur @ domicile ») ; le match renvoyé porte alors inverse=True."""
    deux = est_deux_issues(ligue)
    meilleur, best = None, 0.0
    for decalage in (0, -1, 1):
        for m in matchs_du_jour_paris(ligue, jour_paris + timedelta(days=decalage)):
            if deux:
                s_dir = min(similarite_us(domicile, m["noms_dom"]), similarite_us(exterieur, m["noms_ext"]))
                s_inv = min(similarite_us(domicile, m["noms_ext"]), similarite_us(exterieur, m["noms_dom"]))
                m["inverse"] = s_inv > s_dir + 0.05
                s = max(s_dir, s_inv)
            else:
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
LIBELLES_TR = {
    "1": "Victoire {dom} dans le temps réglementaire", "N": "Nul à la fin du temps réglementaire",
    "2": "Victoire {ext} dans le temps réglementaire", "1N": "{dom} ou nul (temps réglementaire)",
    "N2": "{ext} ou nul (temps réglementaire)", "12": "Pas de nul dans le temps réglementaire",
}
MENTION_PROLONGATION = {"hockey": "prolongation et tirs au but inclus",
                        "baseball": "manches supplémentaires incluses"}

_RE_TOTAL = re.compile(r"^[+-]\d+(?:\.\d+)?$")
_RE_HCP = re.compile(r"^([12]) ([+-]\d+(?:\.\d+)?)$")


def decomposer_code(code):
    """'1 -1.5 TR' -> ('1 -1.5', True) ; '+224.5' -> ('+224.5', False)."""
    code = (code or "").strip()
    if code.endswith(" TR"):
        return code[:-3].strip(), True
    return code, False


def type_pari(code, deux_issues=False):
    """'ml', '3issues', 'total', 'handicap' ou None (pari inconnu)."""
    base, tr = decomposer_code(code)
    if base in ("1", "2") and deux_issues and not tr:
        return "ml"
    if base in ("1", "N", "2", "1N", "N2", "12"):
        return "3issues"
    if _RE_TOTAL.match(base):
        return "total"
    if _RE_HCP.match(base):
        return "handicap"
    return None


def _normaliser_foot(pari):
    """Normalisation historique (football) : '1X2. V2' -> '2', 'X' -> 'N'..."""
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


_TABLE_ISSUES = {"V1": "1", "W1": "1", "P1": "1", "1": "1", "V2": "2", "W2": "2", "P2": "2", "2": "2",
                 "N": "N", "X": "N", "NUL": "N", "DRAW": "N", "1N": "1N", "N1": "1N", "1X": "1N",
                 "X1": "1N", "N2": "N2", "2N": "N2", "X2": "N2", "2X": "N2", "12": "12", "21": "12"}


def _normaliser_etendu(pari, deux):
    """Libellés de bookmakers (français ou anglais) pour handicap, total et
    vainqueur. Sports US : vainqueur = prolongation incluse ; « 1X2 », nul,
    double chance ou mention « temps réglementaire / 60 min » -> code « … TR »."""
    s = _ascii(pari).upper().replace(",", ".")
    s = re.sub(r"\s+", " ", s).strip()
    tr = deux and bool(re.search(r"1X2|1N2|TEMPS REG|\bTR\b|60 ?MIN|REGULATION|3 ISSUES|3[ -]WAY", s))
    # mentions sans effet sur le code
    s = re.sub(r"\(?\b(PROLONGATIONS?|PROL\.?|OT|TAB|TIRS AU BUT|OVERTIME)\b( ET (TAB|TIRS AU BUT))?"
               r"( (INCLUSE?S?|INCL\.?|COMPRISE?S?))?\)?", " ", s)
    s = re.sub(r"\(?\b(1X2|1N2|TEMPS REGLEMENTAIRE|TEMPS REG\.?|TR|60 ?MIN(UTES)?|REGULATION|"
               r"3 ISSUES|3[ -]WAY)\b\)?", " ", s)
    s = re.sub(r"\b(PARI|MATCH|DU|LE|LA|RESULTAT|FINAL|SUR)\b", " ", s)
    s = re.sub(r"[:;]", " ", s)
    s = re.sub(r"\bA?H([12])\b", r"HANDICAP \1", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    suffixe = " TR" if tr else ""
    # handicap : côté (1/2) puis ligne signée
    h = re.search(r"(?:\b(?:HANDICAP|HCP|SPREAD|ECART|PUCK ?LINE|RUN ?LINE|H)\b\.?\s*)?\b([12])\s*\(?\s*([+-])\s*"
                  r"(\d+(?:\.\d+)?)\s*\)?", s)
    mot_hcp = re.search(r"HANDICAP|HCP|SPREAD|ECART|LINE|\bH\b", s)
    if h and (mot_hcp or re.fullmatch(r"[12]\s*\(?\s*[+-]\s*\d+(?:\.\d+)?\s*\)?", s)):
        ligne = float(h.group(3)) * (1 if h.group(2) == "+" else -1)
        return code_hcp(h.group(1), ligne) + suffixe
    if mot_hcp:
        # ligne puis côté : « Handicap (-5.5) 1 » ; sinon pari incomplet (jamais lu comme un total)
        h2 = re.search(r"([+-])\s*(\d+(?:\.\d+)?)\s*\)?\s*\b([12])\b", s)
        if h2:
            ligne = float(h2.group(2)) * (1 if h2.group(1) == "+" else -1)
            return code_hcp(h2.group(3), ligne) + suffixe
        return None
    # total : plus / moins
    t = re.search(r"(?:^|\s|\()(PLUS|MOINS|OVER|UNDER|O|U|\+|-)\s*(?:DE\s*)?(\d+(?:\.\d+)?)\b", s)
    if t and not re.search(r"\b[12]\s*[+-]\s*\d", s):
        signe = "+" if t.group(1) in ("PLUS", "OVER", "O", "+") else "-"
        return f"{signe}{float(t.group(2))}" + suffixe
    # vainqueur / 1N2 / double chance
    s2 = re.sub(r"\b(VAINQUEUR|VICTOIRE|GAGNANT|MONEYLINE|ML|FACE A FACE|TETE A TETE|DOUBLE CHANCE|DC|"
                r"WINNER|EQUIPE)\b", " ", s)
    s2 = re.sub(r"[^0-9A-Z]", "", s2)
    issue = _TABLE_ISSUES.get(s2)
    if issue is None:
        return None
    if deux and (tr or issue in ("N", "1N", "N2", "12")):
        return issue + " TR"
    return issue


def normaliser_pari(pari, ligue=None):
    """Code interne d'un pari à partir du libellé du bookmaker.
    Football : '1X2. V2' -> '2', 'V1' -> '1', 'X' -> 'N', '1X' -> '1N', '+2.5'.
    Sports US (si `ligue` est un code US) : 'Vainqueur 1' / 'V1' / 'Moneyline 1'
    -> '1' (prolongation incluse) ; 'Handicap 2 (+5.5)' -> '2 +5.5' ;
    'Total plus de 224.5' -> '+224.5' ; '1X2. V1', 'X', '1X' -> '1 TR', 'N TR',
    '1N TR' (temps réglementaire, 3 issues)."""
    deux = est_deux_issues(ligue)
    if not deux:
        code = _normaliser_foot(pari)
        if type_pari(code) is not None:
            return code
        return _normaliser_etendu(pari, False) or code
    code = _normaliser_etendu(pari, True)
    return code if code else _ascii(pari).upper().strip()


def pari_depuis_equipes(pari, m):
    """Pari libellé avec un nom d'équipe (« Knicks -2.5 », « Vainqueur Heat »)
    -> code, en remplaçant le nom par 1 (domicile ESPN) ou 2 (extérieur ESPN)."""
    if not m:
        return None
    s = _ascii(pari)
    for cote, noms in (("1", m.get("noms_dom") or [m["domicile"]]), ("2", m.get("noms_ext") or [m["exterieur"]])):
        for nom in sorted((n for n in noms[:4] if n), key=len, reverse=True):
            n = _ascii(nom).strip()
            if len(n) >= 3 and re.search(r"\b" + re.escape(n) + r"\b", s):
                remplace = re.sub(r"\b" + re.escape(n) + r"\b", f" {cote} ", s, count=1)
                code = normaliser_pari(remplace, m["ligue"])
                if type_pari(code, m.get("deux_issues")) is not None:
                    return code
    return None


def inverser_code(code):
    """Code vu depuis l'ordre inverse des équipes (1 <-> 2) ; les totaux ne changent pas."""
    base, tr = decomposer_code(code)
    table = {"1": "2", "2": "1", "1N": "N2", "N2": "1N"}
    if base in table:
        base = table[base]
    else:
        h = _RE_HCP.match(base)
        if h:
            base = f"{'2' if h.group(1) == '1' else '1'} {h.group(2)}"
    return base + (" TR" if tr else "")


def libelle_pari(code, m):
    base, tr = decomposer_code(code)
    deux = bool(m and m.get("deux_issues"))
    if not deux and not tr:
        if code in LIBELLES:
            return LIBELLES[code].format(dom=m["domicile"], ext=m["exterieur"])
        if code and code[0] in "+-":
            return f"{'Plus' if code[0] == '+' else 'Moins'} de {code[1:]} buts"
        h = _RE_HCP.match(code or "")
        if h:
            equipe = m["domicile"] if h.group(1) == "1" else m["exterieur"]
            return f"Handicap {equipe} {h.group(2)}"
        return code
    dom, ext = m["domicile"], m["exterieur"]
    sport = m.get("sport") or info_ligue(m["ligue"])["sport"]
    unite = PARAMS.get(m["ligue"], {}).get("unite", "points")
    fin = {"hockey": " (60 min)", "baseball": " (9 manches)"}.get(sport, "") if tr else ""
    if base in ("1", "2") and not tr:
        mention = MENTION_PROLONGATION.get(sport, "prolongation incluse")
        return f"Victoire {dom if base == '1' else ext} ({mention})"
    if base in LIBELLES_TR:
        return LIBELLES_TR[base].format(dom=dom, ext=ext) + fin
    if _RE_TOTAL.match(base):
        txt = f"{'Plus' if base[0] == '+' else 'Moins'} de {fmt_nombre(base[1:])} {unite}"
        return txt + (" (temps réglementaire)" if tr else "")
    h = _RE_HCP.match(base)
    if h:
        txt = f"Handicap {dom if h.group(1) == '1' else ext} {h.group(2)}"
        return txt + (" (temps réglementaire)" if tr else "")
    return code


def resultat_scores(code, score, score_tr=None, deux_issues=False):
    """'gagne', 'perdu', 'rembourse' (ligne entière tombée pile) ou None
    (pari inconnu). `score` = score final (prolongation incluse ; en NHL le
    vainqueur des tirs au but a un but de plus, comme chez ESPN et DraftKings),
    `score_tr` = score à la fin du temps réglementaire."""
    base, tr = decomposer_code(code)
    h, a = (score_tr if tr and score_tr and score_tr[0] is not None else score)
    if h is None or a is None:
        return None
    if tr and not (score_tr and score_tr[0] is not None):
        return None
    if base in ("1", "2") and deux_issues and not tr:
        if h == a:   # nul NFL après prolongation : compté perdu (voir README)
            return "perdu"
        return "gagne" if (h > a) == (base == "1") else "perdu"
    if base in ("1", "N", "2", "1N", "N2", "12"):
        ok = pari_gagnant(base, h, a)
        return "gagne" if ok else "perdu"
    if _RE_TOTAL.match(base):
        ligne = float(base[1:])
        if h + a == ligne:
            return "rembourse"
        return "gagne" if ((h + a > ligne) == (base[0] == "+")) else "perdu"
    hc = _RE_HCP.match(base)
    if hc:
        ligne = float(hc.group(2))
        ecart = (h - a if hc.group(1) == "1" else a - h) + ligne
        if ecart == 0:
            return "rembourse"
        return "gagne" if ecart > 0 else "perdu"
    return None


def resultat_pari(code, m):
    """Règlement d'un pari sur un match terminé (voir resultat_scores)."""
    return resultat_scores(code, m["score"], m.get("score_tr"), bool(m.get("deux_issues")))


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
    if m.get("deux_issues") and decomposer_code(code)[1]:
        est = estimation_tr(code, m)
        if est:
            return est
    if cote_ticket:
        return (1 / cote_ticket) / MARGE_PAR_DEFAUT, "estimée depuis ta cote (marge ~6 % retirée)"
    return None, "aucune cote disponible"


def cote_marche(code, m):
    c = m.get("cotes") or {}
    if c.get("deux_issues"):
        base, tr = decomposer_code(code)
        if tr:
            return None
        if base in ("1", "2"):
            return c.get(base)
        h = _RE_HCP.match(base)
        if h and c.get("hcp"):
            ligne = c["hcp"]["ligne"] if h.group(1) == "1" else -c["hcp"]["ligne"]
            return c["hcp"][h.group(1)] if abs(float(h.group(2)) - ligne) < 1e-9 else None
    if code in ("1", "N", "2"):
        return c.get(code)
    if code and code[0] in "+-" and c.get("ligne") is not None and float(code[1:]) == c["ligne"]:
        return c["plus"] if code[0] == "+" else c["moins"]
    return None


def cote_ouverture(code, m):
    c = m.get("cotes") or {}
    if c.get("deux_issues"):
        base, tr = decomposer_code(code)
        if tr:
            return None
        if base in ("1", "2"):
            return (c.get("ouverture") or {}).get(base)
        h = _RE_HCP.match(base)
        o = c.get("hcp_ouverture")
        if h and o and c.get("hcp") and o["ligne"] == c["hcp"]["ligne"]:
            return o[h.group(1)]
        o = c.get("total_ouverture")
        if _RE_TOTAL.match(base) and o and o["ligne"] == c.get("ligne"):
            return o["plus"] if base[0] == "+" else o["moins"]
        return None
    c = c.get("ouverture") or {}
    return c.get(code)


def ouverture_incoherente(cotes):
    """ESPN publie parfois un handicap d'ouverture dont le signe contredit le
    moneyline d'ouverture (ex. NBA Heat-Knicks du 27/11/2026 : Knicks favoris
    à -198 mais handicap d'ouverture +5.5). Dans ce cas on l'ignore."""
    o, ho = (cotes or {}).get("ouverture") or {}, (cotes or {}).get("hcp_ouverture")
    if not (o.get("1") and o.get("2") and ho) or abs(ho["ligne"]) < 1 or abs(o["1"] - o["2"]) < 0.05:
        return False
    return (o["1"] < o["2"]) != (ho["ligne"] < 0)


def meme_ligne(c, base):
    """La ligne (handicap ou total) n'a pas bougé depuis l'ouverture ?"""
    if _RE_HCP.match(base):
        o = c.get("hcp_ouverture")
        return bool(o and c.get("hcp") and o["ligne"] == c["hcp"]["ligne"] and not ouverture_incoherente(c))
    o = c.get("total_ouverture")
    return bool(o and c.get("ligne") is not None and o["ligne"] == c["ligne"])


def mouvement_marche(code, m):
    """Sports US : (icône, texte) sur l'évolution ouverture -> maintenant, ou None."""
    c = m.get("cotes") or {}
    if not c.get("deux_issues"):
        return None
    base, tr = decomposer_code(code)
    nfl = " (NFL : l'ouverture date souvent de plusieurs semaines, gros écarts fréquents)" \
        if m["ligue"] == "nfl" else ""
    h = _RE_HCP.match(base)
    if base in ("1", "2") or (tr and base in ("1", "2", "1N", "N2")):
        cote = base[0] if base[0] in "12" else base[-1]
        o, now = (c.get("ouverture") or {}).get(cote), c.get(cote)
        if o and now:
            if now < o - 0.03:
                return ("✅", f"moneyline : ouverture {o:.2f} → maintenant {now:.2f}, le marché croit PLUS en "
                              f"{m['domicile'] if cote == '1' else m['exterieur']}{nfl}")
            if now > o + 0.03:
                return ("⚠️", f"moneyline : ouverture {o:.2f} → maintenant {now:.2f}, le marché croit MOINS en "
                              f"{m['domicile'] if cote == '1' else m['exterieur']}{nfl}")
        return None
    if (h or _RE_TOTAL.match(base)) and meme_ligne(c, base):
        o, now = cote_ouverture(code, m), cote_marche(code, m)
        if o and now and abs(now - o) > 0.03:
            plus = now < o
            return ("✅" if plus else "⚠️", f"même ligne, cote : ouverture {o:.2f} → maintenant {now:.2f}, le marché "
                                            f"croit {'PLUS' if plus else 'MOINS'} en ce pari{nfl}")
        return None
    if h and c.get("hcp") and c.get("hcp_ouverture"):
        if ouverture_incoherente(c):
            return ("ℹ️", "handicap d'ouverture ESPN incohérent avec le moneyline d'ouverture (signe inversé) : ignoré")
        cote = h.group(1)
        lo = c["hcp_ouverture"]["ligne"] * (1 if cote == "1" else -1)
        ln = c["hcp"]["ligne"] * (1 if cote == "1" else -1)
        equipe = m["domicile"] if cote == "1" else m["exterieur"]
        if abs(ln - lo) >= 0.5:
            plus = ln < lo
            return ("✅" if plus else "⚠️",
                    f"handicap {equipe} : ouverture {fmt_ligne(lo)} → maintenant {fmt_ligne(ln)}, le marché croit "
                    f"{'PLUS' if plus else 'MOINS'} en {equipe}{nfl}")
        return None
    if _RE_TOTAL.match(base) and c.get("total_ouverture") and c.get("ligne") is not None:
        lo, ln = c["total_ouverture"]["ligne"], c["ligne"]
        if abs(ln - lo) >= 0.5:
            sens = "PLUS" if ln > lo else "MOINS"
            bon = (sens == "PLUS") == (base[0] == "+")
            return ("✅" if bon else "⚠️", f"total : ouverture {fmt_nombre(lo)} → maintenant {fmt_nombre(ln)}, "
                                           f"le marché attend {sens} de {PARAMS.get(m['ligue'], {}).get('unite', 'points')}{nfl}")
    return None


# ------------------------------------------- estimations (sports US)

def _poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


_LAMBDAS = {}


def lambdas_deux_issues(ligue, probas, cotes):
    """(lambda_dom, lambda_ext) d'un sport « à buts » (NHL, MLB) cohérents avec
    le moneyline (prolongation incluse) et le total du marché (loi de Poisson,
    le vainqueur d'une égalité marque un but de plus)."""
    par = PARAMS[ligue]
    ref, nmax = par["lambda_ref"], par["max_buts"]
    p1 = (probas or {}).get("1")
    ligne = (cotes or {}).get("ligne")
    p_plus = (probas or {}).get(f"+{ligne}") if ligne is not None else None
    if p1 is None and p_plus is None:
        return ref, ref
    cle = (ligue, p1, ligne, p_plus)
    if cle not in _LAMBDAS:
        _LAMBDAS[cle] = _ajuster_lambdas(ref, nmax, p1, ligne, p_plus)
    return _LAMBDAS[cle]


def _ajuster_lambdas(ref, nmax, p1, ligne, p_plus):
    meilleur, err_min = (ref, ref), 9.0
    for i in range(41):
        lam = 2 * ref * (0.55 + i * 0.025)
        for j in range(41):
            part = 0.25 + j * 0.0125
            lh, la = lam * part, lam * (1 - part)
            ph = [_poisson(k, lh) for k in range(nmax + 1)]
            pa = [_poisson(k, la) for k in range(nmax + 1)]
            v1 = vn = vplus = 0.0
            for x in range(nmax + 1):
                for y in range(nmax + 1):
                    w = ph[x] * pa[y]
                    if x > y:
                        v1 += w
                    elif x == y:
                        vn += w
                    if ligne is not None and x + y + (1 if x == y else 0) > ligne:
                        vplus += w
            err = 0.0
            if p1 is not None:
                err += (v1 + 0.5 * vn - p1) ** 2
            if p_plus is not None:
                err += (vplus - p_plus) ** 2
            if err < err_min:
                meilleur, err_min = (lh, la), err
    return meilleur


def _part_prolongation(p1):
    """Part des prolongations / tirs au but gagnées par le domicile : proche de
    50 %, un peu en faveur du favori (50 % + 40 % de l'écart du moneyline)."""
    return 0.5 + 0.4 * (p1 - 0.5)


def _moyennes_normales(ligue, cotes, probas):
    """Écart moyen attendu (domicile - extérieur) et total moyen attendu, déduits
    du marché (handicap, sinon moneyline ; total) avec une loi normale."""
    par = PARAMS[ligue]
    mu = tau = None
    h = (cotes or {}).get("hcp")
    if h and probas.get(code_hcp("1", h["ligne"])) is not None:
        mu = par["sigma"] * _phi_inv(probas[code_hcp("1", h["ligne"])]) - h["ligne"]
    elif probas.get("1") is not None:
        mu = par["sigma"] * _phi_inv(probas["1"])
    ligne = (cotes or {}).get("ligne")
    if ligne is not None and probas.get(f"+{ligne}") is not None:
        tau = ligne + par["sigma_total"] * _phi_inv(probas[f"+{ligne}"])
    return mu, tau


def estimation_tr(code, m):
    """(probabilité, source) d'un pari « temps réglementaire » (1X2 à 3 issues)
    en sport US, que DraftKings ne cote pas : estimation à partir du moneyline
    (et du total) du marché. Hockey : nul estimé par une loi de Poisson ajustée
    au moneyline et au total, recalée sur la moyenne NHL (~23 % de matchs à
    égalité après 60 min)."""
    par = PARAMS.get(m["ligue"])
    probas, cotes = m.get("probas") or {}, m.get("cotes") or {}
    base, tr = decomposer_code(code)
    if not par or not tr or probas.get("1") is None:
        return None
    p1, p2 = probas["1"], probas["2"]
    q = _part_prolongation(p1)
    if par["modele"] == "poisson":
        lh, la = lambdas_deux_issues(m["ligue"], probas, cotes)
        nmax = par["max_buts"]
        ph = [_poisson(k, lh) for k in range(nmax + 1)]
        pa = [_poisson(k, la) for k in range(nmax + 1)]
        d = sum(ph[k] * pa[k] for k in range(nmax + 1))
        dref = sum(_poisson(k, par["lambda_ref"]) ** 2 for k in range(nmax + 1))
        pn = min(0.40, par["nul_tr"] * d / dref)
        if _RE_TOTAL.match(base) or _RE_HCP.match(base):
            # total ou handicap sur 60 min : Poisson brute du temps réglementaire
            tot = sum(ph[x] * pa[y] for x in range(nmax + 1) for y in range(nmax + 1)
                      if resultat_scores(base, (x, y)) in ("gagne", "rembourse"))
            return tot, (f"estimation Poisson sur 60 min (buts attendus {lh:.2f} - {la:.2f}, d'après le "
                         f"moneyline et le total DraftKings) : pas de cote de marché pour ce pari")
    else:
        if _RE_TOTAL.match(base) or _RE_HCP.match(base):
            return None
        mu, _ = _moyennes_normales(m["ligue"], cotes, probas)
        pn = par["nul_tr"] * math.exp(-((mu or 0) / par["sigma"]) ** 2 / 2)
    p1tr, p2tr = max(0.01, p1 - pn * q), max(0.01, p2 - pn * (1 - q))
    valeurs = {"1": p1tr, "N": pn, "2": p2tr, "1N": p1tr + pn, "N2": pn + p2tr, "12": p1tr + p2tr}
    if base not in valeurs:
        return None
    ref = "NHL : ~23 % en moyenne" if m["ligue"] == "nhl" else f"moyenne ~{100 * par['nul_tr']:.0f} %"
    return valeurs[base], (f"ESTIMATION, pas une cote de marché : nul après le temps réglementaire estimé à "
                           f"{100 * pn:.0f} % ({ref}), déduit du moneyline DraftKings")


def fraction_restante(m):
    """Part du temps réglementaire qui reste à jouer (0 en prolongation), None si
    le match n'est pas en cours."""
    par = PARAMS.get(m["ligue"])
    if not par or m.get("etat") != "in":
        return None
    n = par["periodes"]
    per = m.get("periode") or 1
    if par["duree"] is None:   # baseball : manches (approximation)
        if per > n:
            return 0.0
        st = (m.get("statut") or "").lower()
        reste = 0.75 if st.startswith("top") else 0.5 if st.startswith("mid") else \
            0.25 if st.startswith("bot") else 0.0
        return max(0.0, (n - per + reste) / n)
    if per > n:
        return 0.0
    horloge = float(m.get("horloge") or 0.0)
    return min(1.0, max(0.0, ((n - per) * par["duree"] + horloge) / (n * par["duree"])))


def proba_direct_us(code, m, cotes, probas, p_depart=None):
    """Probabilité que le pari passe (gagné ou remboursé) vu le score et le temps
    restant. Basket et football américain : loi normale sur l'écart et le total
    (moyennes tirées du marché d'avant-match). Hockey et baseball : loi de
    Poisson sur les buts restants (méthode du football), égalité après le temps
    réglementaire = un but de plus pour le vainqueur."""
    par = PARAMS.get(m["ligue"])
    r = fraction_restante(m)
    h0, a0 = m["score"]
    if par is None or r is None or h0 is None:
        return None
    base, tr = decomposer_code(code)
    probas = dict(probas or {})
    if not probas and p_depart:
        # pas de cote de marché : on part de ta cote pour ce seul pari
        probas[base] = p_depart
    p1 = probas.get("1", 0.5)
    q = _part_prolongation(p1)
    if par["modele"] == "poisson":
        lh, la = lambdas_deux_issues(m["ligue"], probas, cotes)
        if r == 0.0:
            reg = m.get("score_tr") if m.get("score_tr") and m["score_tr"][0] is not None else (h0, a0)
            issues = [((reg[0], reg[1]), 1.0)]
        else:
            nmax = par["max_buts"]
            ph = [_poisson(k, lh * r) for k in range(nmax + 1)]
            pa = [_poisson(k, la * r) for k in range(nmax + 1)]
            issues = [((h0 + x, a0 + y), ph[x] * pa[y]) for x in range(nmax + 1) for y in range(nmax + 1)]
        tot = 0.0
        for (h, a), w in issues:
            if h != a:
                finales = [((h, a), w)]
            else:
                finales = [((h + 1, a), w * q), ((h, a + 1), w * (1 - q))]
            for fin, wf in finales:
                if resultat_scores(code, fin, (h, a), True) in ("gagne", "rembourse"):
                    tot += wf
        return tot
    # loi normale
    mu, tau = _moyennes_normales(m["ligue"], cotes or {}, probas)
    if mu is None and _RE_HCP.match(base) and base in probas:
        hc = _RE_HCP.match(base)
        sens = 1 if hc.group(1) == "1" else -1
        mu = sens * (par["sigma"] * _phi_inv(probas[base]) - float(hc.group(2)))
    if tau is None and _RE_TOTAL.match(base) and base in probas:
        sens = 1 if base[0] == "+" else -1
        tau = float(base[1:]) + sens * par["sigma_total"] * _phi_inv(probas[base])
    mu = mu or 0.0
    total_tr = par["periodes"] * par["duree"]
    if m.get("periode") and m["periode"] > par["periodes"]:
        # prolongation : il reste le temps de la prolongation en cours
        r = max(float(m.get("horloge") or 0.0) / total_tr, 1e-4)
        if tr:
            return 1.0 if resultat_pari(code, m) in ("gagne", "rembourse") else 0.0
    elif r <= 1e-4 and h0 == a0 and not tr:
        r = par["prolongation"] / total_tr   # égalité à la fin : une prolongation va se jouer
    if r <= 1e-4:
        return 1.0 if resultat_scores(code, (h0, a0), (h0, a0), True) in ("gagne", "rembourse") else 0.0
    d = h0 - a0 + mu * r
    s = par["sigma"] * math.sqrt(r)
    if base in ("1", "2") and not tr:
        p = _phi(d / s)
        return p if base == "1" else 1 - p
    if tr and base in LIBELLES_TR:
        pn = _phi((0.5 - d) / s) - _phi((-0.5 - d) / s)
        p1tr, p2tr = 1 - _phi((0.5 - d) / s), _phi((-0.5 - d) / s)
        return {"1": p1tr, "N": pn, "2": p2tr, "1N": p1tr + pn, "N2": pn + p2tr, "12": p1tr + p2tr}[base]
    hc = _RE_HCP.match(base)
    if hc:
        ligne = float(hc.group(2))
        return _phi((d + ligne) / s) if hc.group(1) == "1" else _phi((-d + ligne) / s)
    if _RE_TOTAL.match(base):
        if tau is None:
            return None
        ligne = float(base[1:])
        st = par["sigma_total"] * math.sqrt(r)
        p_plus = 1 - _phi((ligne - (h0 + a0) - tau * r) / st)
        return p_plus if base[0] == "+" else 1 - p_plus
    return None


# ------------------------------------- données complémentaires (sports US)

def resume(m):
    """Résumé ESPN du match (feuille de match, cotes, blessés, météo...)."""
    return get_json_memo(url_summary(m["ligue"], m["id"])) or {}


def cotes_resume(m, moment="close"):
    """Cotes du « pickcenter » du résumé : ESPN les garde après le coup d'envoi
    (le tableau des matchs, lui, les retire), avec les cotes en direct."""
    pc = [o for o in (resume(m).get("pickcenter") or []) if o]
    if not pc:
        return None
    return extraire_cotes({"odds": pc}, True, moment)


STATUTS_FR = {"Out": "absent", "Doubtful": "douteux", "Questionable": "incertain",
              "Day-To-Day": "au jour le jour", "Injured Reserve": "liste des blessés (IR)",
              "Suspension": "suspendu", "Probable": "probable",
              "Physically Unable to Perform": "inapte (PUP)", "Not Active": "inactif"}
ORDRE_STATUT = {"Out": 0, "Suspension": 1, "Doubtful": 2, "Questionable": 3, "Day-To-Day": 4,
                "Probable": 6, "Injured Reserve": 5}
NOTES_COMMENTAIRE = ((r"\bruled out\b|\bwill not play\b|\bwon't play\b|\bwill miss\b", "forfait"),
                     (r"\bdoubtful\b", "douteux"), (r"\bquestionable\b", "incertain"),
                     (r"\bgame-time decision\b", "décision de dernière minute"),
                     (r"\bprobable\b", "probable"), (r"\bexpected to play\b|\bwill play\b", "devrait jouer"))
POSTES_CLES = {"football": ("QB",), "hockey": ("G",), "baseball": ("SP",)}
LIGUES_BLESSURES_FIABLES = ("nfl", "nba", "nhl", "mlb", "wnba")
ROLES = {"pointsPerGame": "meilleur marqueur", "assistsPerGame": "meilleur passeur",
         "reboundsPerGame": "meilleur rebondeur", "passingYards": "QB n°1 (yards à la passe)",
         "rushingYards": "meilleur coureur", "receivingYards": "meilleur receveur",
         "goals": "meilleur buteur", "assists": "meilleur passeur", "points": "meilleur pointeur"}


def etoiles(m):
    """Joueurs clés de chaque équipe d'après les « leaders » ESPN de la saison
    (résumé d'avant-match) : {id_equipe: {id_joueur: rôle}}."""
    res = {}
    for t in resume(m).get("leaders") or []:
        tid = str((t.get("team") or {}).get("id"))
        for cat in t.get("leaders") or []:
            role = ROLES.get(cat.get("name"))
            if not role:
                continue
            for l in (cat.get("leaders") or [])[:1]:
                aid = str((l.get("athlete") or {}).get("id"))
                res.setdefault(tid, {}).setdefault(aid, role)
    return res


def _id_athlete(a):
    """Identifiant ESPN d'un joueur : champ id, sinon lien de sa fiche (/id/123/)."""
    if a.get("id"):
        return str(a["id"])
    for lien in a.get("links") or []:
        r = re.search(r"/id/(\d+)", lien.get("href") or "")
        if r:
            return r.group(1)
    return ""


def blessures_equipe(m, team_id, stars=None):
    """Blessés / absents d'une équipe, du plus important au moins important.
    Source : liste ESPN de la ligue (/injuries, plus complète que les 5 noms
    du résumé) ; à défaut le résumé du match. None si ESPN n'a rien (NCAA)."""
    stars = stars or {}
    liste = None
    data = get_json_memo(url_blessures(m["ligue"]))
    for t in (data or {}).get("injuries") or []:
        if str(t.get("id")) == str(team_id):
            liste = t.get("injuries") or []
            break
    if liste is None:
        for t in resume(m).get("injuries") or []:
            if str((t.get("team") or {}).get("id")) == str(team_id):
                liste = t.get("injuries") or []
    if liste is None:
        # équipe absente de la liste : aucun blessé déclaré, sauf en NCAA où
        # ESPN ne suit presque pas les blessures (liste vide ou partielle)
        if m["ligue"] in LIGUES_BLESSURES_FIABLES and (data or {}).get("injuries"):
            liste = []
        else:
            return None
    maintenant = maintenant_utc()
    sport = info_ligue(m["ligue"])["sport"]
    res = []
    for i in liste or []:
        st = i.get("status") or ""
        if st in ("Active", ""):
            continue
        a = i.get("athlete") or {}
        aid = _id_athlete(a)
        det = i.get("details") or {}
        poste = (a.get("position") or {}).get("abbreviation") or ""
        star = stars.get(aid)
        if det.get("type") == "Coach's Decision" and not star:
            continue   # inactif « choix de l'entraîneur » (liste NFL du jour) : peu informatif
        try:
            d = parse_utc(i.get("date"))
        except (ValueError, TypeError):
            d = maintenant - timedelta(days=99)
        com = i.get("shortComment") or ""
        note = next((fr for motif, fr in NOTES_COMMENTAIRE if re.search(motif, com, re.I)), None)
        res.append({"nom": a.get("displayName") or "?", "poste": poste, "statut": st,
                    "statut_fr": STATUTS_FR.get(st, st.lower()), "partie": det.get("type"),
                    "date": d, "recent": (maintenant - d).days <= 10, "etoile": star,
                    "cle": poste in POSTES_CLES.get(sport, ()), "note": note, "commentaire": com})
    res.sort(key=lambda x: (x["etoile"] is None, not x["cle"], not x["recent"],
                            ORDRE_STATUT.get(x["statut"], 9), -x["date"].timestamp()))
    return res


def note_quarterback(m, team_id):
    """Dernière note ESPN (7 jours) sur le quarterback titulaire d'une équipe NFL
    ou NCAA (« X will start… »), ou None."""
    data = get_json_memo(url_blessures(m["ligue"]))
    for t in (data or {}).get("injuries") or []:
        if str(t.get("id")) != str(team_id):
            continue
        for i in t.get("injuries") or []:
            a = i.get("athlete") or {}
            com = i.get("shortComment") or ""
            if (a.get("position") or {}).get("abbreviation") == "QB" and re.search(r"\bstart", com, re.I):
                try:
                    d = parse_utc(i.get("date"))
                except (ValueError, TypeError):
                    continue
                if (maintenant_utc() - d).days <= 7:
                    return {"nom": a.get("displayName"), "commentaire": com, "date": d}
    return None


def dos_a_dos(m, team_id):
    """Repos avant le match : {'repos': jours sans match, 'veille': bool,
    'dernier': 'MIA @ NY', 'date': datetime} d'après le calendrier ESPN."""
    data = get_json_memo(url_calendrier(m["ligue"], team_id))
    if not data:
        return None
    prec = None
    for e in data.get("events") or []:
        try:
            d = parse_utc(e["date"])
        except (KeyError, ValueError):
            continue
        etat = (((e.get("competitions") or [{}])[0].get("status") or {}).get("type") or {}).get("state")
        if d < m["debut_utc"] - timedelta(hours=3) and etat in ("post", "in"):
            if prec is None or d > prec[0]:
                prec = (d, e.get("shortName") or e.get("name"))
    if prec is None:
        return None
    repos = (date_us(m["debut_utc"]) - date_us(prec[0])).days - 1
    return {"repos": repos, "veille": repos == 0, "dernier": prec[1], "date": prec[0]}


def gardiens(m):
    """NHL : gardiens partants (tableau ESPN, statut Confirmed / Expected) et
    nombre de matchs joués par chaque gardien de l'équipe (résumé)."""
    partants = m.get("partants") or (None, None)
    stats = {}
    g = resume(m).get("goalies") or {}
    for cle in ("homeTeam", "awayTeam"):
        bloc = g.get(cle) or {}
        lst = []
        for a in bloc.get("athletes") or []:
            mj = next((s.get("displayValue") for s in a.get("statistics") or [] if s.get("name") == "games"), None)
            sv = next((s.get("displayValue") for s in a.get("statistics") or [] if s.get("name") == "savePct"), None)
            try:
                mj = int(float(mj))
            except (TypeError, ValueError):
                mj = None
            lst.append({"id": str(a.get("id")), "nom": a.get("displayName"), "matchs": mj, "arrets": sv})
        stats[str(bloc.get("teamId"))] = lst
    return partants, stats


def meteo(m):
    """Météo prévue (AccuWeather via ESPN) : (°C, rafales km/h, risque de
    précipitations %) ou None (stade couvert ou pas de prévision)."""
    w = (resume(m).get("gameInfo") or {}).get("weather") or {}
    t = w.get("temperature")
    if t is None:
        return None
    rafales = w.get("gust")
    return {"temp_c": (float(t) - 32) * 5 / 9,
            "rafales_kmh": None if rafales is None else float(rafales) * 1.609,
            "precipitations": w.get("precipitation")}


def predicteur(m):
    """Probabilités du modèle ESPN (« Matchup Predictor ») : information
    secondaire, jamais utilisée pour les calculs (leçon A : le marché seul)."""
    p = resume(m).get("predictor") or {}
    try:
        return float(p["homeTeam"]["gameProjection"]) / 100, float(p["awayTeam"]["gameProjection"]) / 100
    except (KeyError, TypeError, ValueError):
        return None


def est_fcs(m, team_id):
    """NCAA football : l'équipe est-elle de FCS (division inférieure) ?"""
    t = (get_json_memo(url_equipe(m["ligue"], team_id)) or {}).get("team") or {}
    parent = ((t.get("groups") or {}).get("parent") or {}).get("id")
    return None if parent is None else str(parent) == "81"


# --------------------------------------------------------- garde-fou actu

def alertes_actu(ligue, team_id, nom_equipe, jours=7):
    """Titres ESPN récents qui parlent de blessures / suspensions / rotation
    pour l'équipe. À vérifier par un humain : c'est une recherche par mots-clés."""
    deux = est_deux_issues(ligue)
    try:
        data = get_json(url_news(ligue, team_id) if deux else NEWS.format(ligue, team_id))
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
        signal = bool(_RE_ABSENCE_US.search(texte)) if deux else any(k in texte for k in MOTS_ABSENCE)
        if concerne and signal:
            lien = ((art.get("links") or {}).get("web") or {}).get("href", "")
            res.append({"date": pub.date().isoformat(), "titre": titre, "lien": lien})
    return res


def statut_fin_fr(statut):
    """'Final/SO' -> 'aux tirs au but', 'Final/2OT' -> 'après 2 prolongations',
    'Final/10' (baseball) -> 'après 10 manches'."""
    s = statut or ""
    if "SO" in s:
        return "aux tirs au but"
    r = re.search(r"(\d*)OT", s)
    if r:
        n = r.group(1)
        return "après prolongation" if n in ("", "1") else f"après {n} prolongations"
    r = re.search(r"Final/(\d+)$", s)
    if r:
        return f"après {r.group(1)} manches"
    return "terminé" if s == "Final" else s


def fmt_pct(x):
    return "—" if x is None else f"{100 * x:.0f} %"


def fmt_heure(m):
    d = m["debut_paris"]
    return f"{d.day:02d}/{d.month:02d} {d.hour:02d}h{d.minute:02d}"
