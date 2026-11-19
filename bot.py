#!/usr/bin/env python3
"""
Bot d'alertes « gros deals » : Black Friday et cadeaux de Noël.

Trois sources, toutes lues dans le respect de leur robots.txt :
  1. Dealabs : flux RSS + pages de catégorie + page « hot ». La communauté
     y publie les promos de Sephora, Nocibé, Zalando, Fnac, Amazon…, avec une
     note (température) et souvent un prix de référence.
  2. LeDénicheur : comparateur multi-marchands. Le bot parcourt des catégories
     (parfum, maquillage, sacs, sneakers, montres, high-tech…), garde son propre
     historique et vérifie chaque baisse sur l'historique de prix de 3 mois.
  3. Ta liste de produits surveillés (section `surveillance` du config.yaml) :
     une page LeDénicheur ou une fiche produit d'un site marchand.

Les alertes partent sur Telegram, Discord et/ou ntfy, selon les variables
d'environnement définies (voir README.md).

  python bot.py              # un passage normal
  python bot.py --dry-run    # affiche ce qui serait envoyé, n'envoie rien
  python bot.py --test       # envoie une notification de test
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DEALABS = "https://www.dealabs.com"
LDN = "https://ledenicheur.fr"
IMG_CDN = "https://static-pepper.dealabs.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
JOUR = 86400


def log(msg: str) -> None:
    print(msg, flush=True)


# ==========================================================================
# Texte
# ==========================================================================
def normaliser(txt: str) -> str:
    """minuscules, sans accents, ponctuation -> espaces (bordé d'espaces)."""
    txt = unicodedata.normalize("NFKD", txt or "")
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = re.sub(r"[^a-z0-9]+", " ", txt.lower())
    return f" {txt.strip()} "


def _terme_present(terme: str, texte_norm: str) -> bool:
    t = normaliser(terme).strip()
    if not t:
        return False
    motif = r"(?<![a-z0-9])" + re.escape(t) + r"(?:s|x)?(?![a-z0-9])"  # pluriel toléré
    return re.search(motif, texte_norm) is not None


def mot_cle_present(mot_cle: str, texte_norm: str) -> bool:
    """`palette + fards` = les deux termes présents, n'importe où dans le texte."""
    parties = [p for p in str(mot_cle).split("+") if p.strip()]
    return bool(parties) and all(_terme_present(p, texte_norm) for p in parties)


def premier_mot_trouve(mots, texte_norm: str) -> str | None:
    for m in mots or []:
        if mot_cle_present(m, texte_norm):
            return str(m)
    return None


def prix_float(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.]", "", str(v))
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def euros(p: float | None) -> str:
    if p is None:
        return "?"
    s = f"{p:,.2f}".replace(",", " ").replace(".", ",")
    return (s[:-3] if s.endswith(",00") else s) + " €"


# ==========================================================================
# Modèle
# ==========================================================================
@dataclass
class Deal:
    id: str
    titre: str
    url: str
    source: str
    prix: float | None = None
    prix_ref: float | None = None  # prix habituel / barré / médiane historique
    reduction: int | None = None  # %
    temperature: float = 0.0  # Dealabs uniquement
    marchand: str = ""
    image: str | None = None
    publie: float = 0.0
    groupes: set[str] = field(default_factory=set)
    details: str = ""  # texte additionnel pour les mots-clés (ex. « Homme, Eau de parfum »)
    code_promo: str = ""
    livraison_gratuite: bool = False
    expire: bool = False
    plus_bas: str = ""  # ex. « plus bas prix des 3 derniers mois »
    tags: set[str] = field(default_factory=set)  # centres d'intérêt (parfum_femme, gaming…)

    def texte(self) -> str:
        return normaliser(f"{self.titre} {self.marchand} {self.details}")

    def score(self) -> float:
        """Classement équilibré : votes Dealabs plafonnés, réduction, bonus « plus bas prix »."""
        bonus = 40 if re.search(r"plus bas|habituel|relevé", self.plus_bas or "") else 0
        return min(self.temperature, 2000) / 20 + 2 * (self.reduction or 0) + bonus


@dataclass
class Alerte:
    deal: Deal
    profils: list[dict]
    raison: str
    nouvelle_baisse: bool = False


# ==========================================================================
# HTTP poli : délai entre requêtes + respect du robots.txt
# ==========================================================================
class Robots:
    """Lecture de robots.txt avec jokers (* et $), règle la plus longue gagnante."""

    def __init__(self, session: requests.Session):
        self.s = session
        self.cache: dict[str, list[tuple[bool, re.Pattern, int]]] = {}

    def _charger(self, base: str) -> list:
        regles: list = []
        try:
            r = self.s.get(base + "/robots.txt", timeout=15)
            texte = r.text if r.status_code == 200 else ""
        except requests.RequestException:
            texte = ""
        agents, dans_groupe_etoile, vu_regle = [], False, False
        for ligne in texte.splitlines():
            ligne = ligne.split("#", 1)[0].strip()
            if ":" not in ligne:
                continue
            cle, val = (x.strip() for x in ligne.split(":", 1))
            cle = cle.lower()
            if cle == "user-agent":
                if vu_regle:
                    agents, vu_regle = [], False
                agents.append(val)
                dans_groupe_etoile = "*" in agents
            elif cle in ("allow", "disallow"):
                vu_regle = True
                if dans_groupe_etoile and val:
                    motif = re.escape(val).replace(r"\*", ".*")
                    if motif.endswith(r"\$"):
                        motif = motif[:-2] + "$"
                    regles.append((cle == "allow", re.compile(motif), len(val)))
        return regles

    def autorise(self, url: str) -> bool:
        p = urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.cache:
            self.cache[base] = self._charger(base)
        chemin = (p.path or "/") + (f"?{p.query}" if p.query else "")
        meilleur = None
        for allow, motif, longueur in self.cache[base]:
            if motif.match(chemin) and (meilleur is None or longueur > meilleur[1]
                                        or (longueur == meilleur[1] and allow)):
                meilleur = (allow, longueur)
        return True if meilleur is None else meilleur[0]


class Http:
    def __init__(self, delai: float, timeout: float = 25):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
        })
        self.delai = delai
        self.timeout = timeout
        self._dernier: dict[str, float] = {}
        self.robots = Robots(self.s)

    def get(self, url: str) -> requests.Response | None:
        if not self.robots.autorise(url):
            log(f"  ⛔ robots.txt interdit {url} : ignoré")
            return None
        hote = urlsplit(url).netloc
        for essai in range(3):
            attente = self.delai - (time.time() - self._dernier.get(hote, 0))
            if attente > 0:
                time.sleep(attente)
            self._dernier[hote] = time.time()
            try:
                r = self.s.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                log(f"  ⚠ réseau ({e.__class__.__name__}) sur {url}")
                time.sleep(3 * (essai + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (essai + 1))
                continue
            if "charset" not in r.headers.get("content-type", "").lower():
                r.encoding = "utf-8"
            return r
        return None


# ==========================================================================
# Source 1 : Dealabs
# ==========================================================================
_VUE_RE = re.compile(r"data-vue3='(\{[^']*\})'|data-vue3=\"(\{[^\"]*\})\"")
_PCT_RE = re.compile(r"-\s?(\d{1,2})\s?%")
_ID_RE = re.compile(r"-(\d{5,})/?$")


def _reduction(prix, ref, titre="", pct=None) -> int | None:
    if prix and ref and ref > prix:
        return round((1 - prix / ref) * 100)
    if pct:
        return int(pct)
    m = _PCT_RE.search(titre or "")  # ex. code promo « -25% sur tout le site »
    return int(m.group(1)) if m else None


def parser_pages_dealabs(page_html: str, groupe: str | None) -> list[Deal]:
    """Deals présents dans une page Dealabs (données JSON embarquées)."""
    deals = []
    for a, b in _VUE_RE.findall(page_html):
        try:
            data = json.loads(html.unescape(a or b))
        except ValueError:
            continue
        t = (data.get("props") or {}).get("thread")
        if not t or not t.get("threadId"):
            continue
        prix = t.get("price") or None
        ref = t.get("nextBestPrice") or None
        img = t.get("mainImage") or {}
        image = (f"{IMG_CDN}/{img['path']}/{img['name']}/re/300x300/qt/60/{img['name']}.jpg"
                 if img.get("path") and img.get("name") else None)
        groupes = {groupe} if groupe else set()
        mg = (t.get("mainGroup") or {}).get("threadGroupUrlName")
        if mg:
            groupes.add(mg)
        deals.append(Deal(
            id=f"dealabs:{t['threadId']}",
            titre=(t.get("title") or "").strip(),
            url=f"{DEALABS}/bons-plans/{t.get('titleSlug', 'deal')}-{t['threadId']}",
            source="Dealabs",
            prix=prix, prix_ref=ref,
            reduction=_reduction(prix, ref, t.get("title"), t.get("percentage")),
            temperature=float(t.get("temperature") or 0),
            marchand=(t.get("merchant") or {}).get("merchantName") or t.get("linkHost") or "",
            image=image,
            publie=float(t.get("publishedAt") or 0),
            groupes=groupes,
            code_promo=(t.get("voucherCode") or "").strip(),
            livraison_gratuite=bool((t.get("shipping") or {}).get("isFree")),
            expire=bool(t.get("isExpired")) or t.get("status") not in (None, "Activated"),
        ))
    return deals


def parser_rss_dealabs(xml_txt: str, groupe: str) -> list[Deal]:
    """Flux RSS : deals récents (sans température : vérifiée ensuite sur la page)."""
    ns = {"pepper": "http://www.pepper.com/rss", "media": "http://search.yahoo.com/mrss/"}
    try:
        racine = ET.fromstring(xml_txt)
    except ET.ParseError:
        return []
    deals = []
    for it in racine.iter("item"):
        lien = (it.findtext("link") or "").strip()
        m = _ID_RE.search(lien)
        if not m:
            continue
        march = it.find("pepper:merchant", ns)
        prix = prix_float(march.get("price")) if march is not None else None
        img = it.find("media:content", ns)
        try:
            publie = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except (TypeError, ValueError):
            publie = time.time()
        titre = (it.findtext("title") or "").strip()
        deals.append(Deal(
            id=f"dealabs:{m.group(1)}", titre=titre, url=lien, source="Dealabs",
            prix=prix, reduction=_reduction(prix, None, titre),
            marchand=march.get("name", "") if march is not None else "",
            image=img.get("url").replace("/re/150x150/qt/55/", "/re/300x300/qt/60/") if img is not None and img.get("url") else None,
            publie=publie, groupes={groupe},
        ))
    return deals


def collecter_dealabs(http: Http, cfg: dict, etat: dict, profils: list[dict]) -> list[Deal]:
    dcfg = cfg.get("dealabs") or {}
    if not dcfg.get("actif", True):
        return []
    groupes = dcfg.get("groupes", [])
    tous: dict[str, Deal] = {}

    def ajouter(d: Deal):
        if d.id in tous:
            ancien = tous[d.id]
            ancien.groupes |= d.groupes
            if d.temperature and not ancien.temperature:  # la version page est plus riche
                d.groupes |= ancien.groupes
                tous[d.id] = d
        else:
            tous[d.id] = d

    # a) pages de catégorie (deals les plus chauds du mois, toutes les X heures) + page /hot
    pages = [("/hot", None)] if dcfg.get("page_hot", True) else []
    if time.time() - etat.get("dernier_groupes_dealabs", 0) > dcfg.get("pages_groupe_toutes_les_heures", 3) * 3600:
        pages += [(f"/groupe/{g}", g) for g in groupes]
        etat["dernier_groupes_dealabs"] = time.time()
    for chemin, g in pages:
        r = http.get(DEALABS + chemin)
        if r is None or r.status_code != 200:
            code = r.status_code if r is not None else "ignoré"
            log(f"  Dealabs {chemin:<38} pas de page ({code}), flux RSS seulement" if code in (404, 410)
                else f"  ⚠ Dealabs {chemin} : {code}")
            continue
        ds = parser_pages_dealabs(r.text, g)
        for d in ds:
            ajouter(d)
        log(f"  Dealabs {chemin:<38} {len(ds):>3} deals")

    # b) flux RSS (nouveautés) : on retient ceux qui intéressent un profil
    candidats = etat.setdefault("candidats_dealabs", {})
    for g in groupes:
        r = http.get(f"{DEALABS}/rss/groupe/{g}")
        if r is None or r.status_code != 200:
            continue
        ds = parser_rss_dealabs(r.text, g)
        for d in ds:
            if d.id in tous or d.id in candidats or time.time() - d.publie > 48 * 3600:
                continue
            if any(correspond(d, p, cfg) for p in profils):
                candidats[d.id] = {"url": d.url, "t": int(time.time()), "groupe": g, "verif": 0}
        log(f"  Dealabs RSS {g:<34} {len(ds):>3} nouveautés")

    # c) on rafraîchit la page des candidats pour connaître leur température
    duree = dcfg.get("suivi_nouveautes_heures", 72) * 3600
    for k in [k for k, v in candidats.items() if time.time() - v["t"] > duree]:
        del candidats[k]
    a_verifier = sorted((k for k in candidats if k not in tous), key=lambda k: candidats[k]["verif"])
    for k in a_verifier[: dcfg.get("max_pages_deal_par_passage", 25)]:
        c = candidats[k]
        c["verif"] = int(time.time())
        r = http.get(c["url"])
        if r is None or r.status_code != 200:
            continue
        for d in parser_pages_dealabs(r.text, c.get("groupe")):
            if d.id == k:
                ajouter(d)
                if d.expire:
                    del candidats[k]
                break
    log(f"  Dealabs : {len(candidats)} nouveautés en suivi, {min(len(a_verifier), dcfg.get('max_pages_deal_par_passage', 25))} vérifiées")
    return list(tous.values())


# ==========================================================================
# Source 2 : LeDénicheur (comparateur multi-marchands)
# ==========================================================================
def parser_categorie_ldn(page_html: str, categorie: str) -> list[Deal]:
    vus, deals = set(), []
    for carte in page_html.split('data-test="ProductGridCard"')[1:]:
        m = re.search(r'href="/product\.php\?p=(\d+)"', carte)
        nom = re.search(r'data-test="ProductName">([^<]+)<', carte)
        prix = re.search(r'PriceWrapper.*?>([\d\s \xa0.]+(?:,\d{1,2})?)\s?€', carte, re.S)
        if not (m and nom and prix) or m.group(1) in vus:
            continue
        vus.add(m.group(1))
        props = re.search(r'Properties[^>]*><span[^>]*>([^<]*)<', carte)
        img = re.search(r'<img src="(https://[^"]+)"', carte)
        deals.append(Deal(
            id=f"ldn:{m.group(1)}", titre=html.unescape(nom.group(1)).strip(),
            url=f"{LDN}/product.php?p={m.group(1)}", source="LeDénicheur",
            prix=prix_float(prix.group(1)), image=img.group(1) if img else None,
            groupes={f"ldn:{categorie}"}, details=html.unescape(props.group(1)) if props else "",
            publie=time.time(),
        ))
    return deals


def analyser_produit_ldn(page_html: str) -> dict:
    """Prix le plus bas actuel, marchand, et historique 3 mois (minimum par jour)."""
    out: dict = {}
    m = re.search(r'\\"lowestPrice\\":([\d.]+)', page_html)
    if m:
        out["prix"] = float(m.group(1))
    else:
        p, _, _ = extraire_prix_page(page_html)
        out["prix"] = p
    m = re.search(r'children\\":\[\\"chez\\".{0,400}?children\\":\\"([^\\"]+)\\"', page_html)
    out["marchand"] = m.group(1) if m else ""
    m = re.search(r'\\"productName\\":\\"((?:[^"\\]|\\\\.)*)\\"', page_html)
    out["titre"] = m.group(1) if m else None
    jours: dict[str, float] = {}
    for _, date, centimes in re.findall(
            r'\{\\"shopId\\":\d+,[^{}]*?\\"shopName\\":\\"([^{}]*?)\\",\\"date\\":\\"([^\\]+)\\",[^{}]*?\\"price\\":(\d+)',
            page_html):
        j, p = date[:10], int(centimes) / 100
        jours[j] = min(jours.get(j, 1e12), p)
    out["historique"] = jours
    return out


def verdict_historique(prix: float, valeurs: list[float], reduction_min: float) -> tuple[str | None, float | None]:
    """Compare un prix à un historique. Renvoie (raison, prix_habituel)."""
    if not prix or len(valeurs) < 5:
        return None, None
    med = statistics.median(valeurs)
    return verdict_ldn(prix, med, min(valeurs), reduction_min), med


def historique_passe(jours: dict[str, float], marge_jours: int = 2) -> list[float]:
    """Prix de l'historique en excluant les derniers jours (pour comparer le prix du jour au passé)."""
    limite = time.strftime("%Y-%m-%d", time.gmtime(time.time() - marge_jours * JOUR))
    return [p for j, p in jours.items() if j < limite and p]


def verdict_ldn(prix: float, med: float | None, mini: float | None, reduction_min: float) -> str | None:
    if not prix or not med:
        return None
    if prix <= med * (1 - reduction_min / 100):
        return f"-{round((1 - prix / med) * 100)} % vs le prix habituel ({euros(med)})"
    if mini and prix < mini * 0.97 and prix <= med * 0.9:
        return f"plus bas prix des 3 derniers mois (avant : {euros(mini)})"
    return None


def collecter_ldn(http: Http, cfg: dict, etat: dict, profils: list[dict]) -> list[Deal]:
    """Parcourt les catégories, puis compare chaque produit à son historique de 3 mois.

    L'historique est lu sur la fiche produit (quelques dizaines par passage, à tour de rôle),
    ce qui permet de juger un prix dès le premier jour, sans attendre."""
    lcfg = cfg.get("ledenicheur") or {}
    if not lcfg.get("actif", True):
        return []
    maintenant = time.time()
    # comparé à un vrai historique de 3 mois (pas à un prix barré) : un seuil plus bas reste fiable
    r_min = lcfg.get("reduction_min", cfg.get("seuils", {}).get("reduction_min", 30))
    suivi = etat.setdefault("ldn", {})
    seuil_verif = lcfg.get("baisse_pour_verifier", 12)

    # 1) catégories (toutes les X heures) : prix du jour de ~45 produits populaires par catégorie
    baisses: set[str] = set()
    if maintenant - etat.get("dernier_ldn", 0) >= lcfg.get("toutes_les_heures", 3) * 3600:
        etat["dernier_ldn"] = maintenant
        for cat in lcfg.get("categories", []):
            r = http.get(f"{LDN}/c/{cat}")
            if r is None or r.status_code != 200:
                log(f"  ⚠ LeDénicheur {cat} : {r.status_code if r is not None else 'ignoré'}")
                continue
            produits = parser_categorie_ldn(r.text, cat)
            log(f"  LeDénicheur /c/{cat:<34} {len(produits):>3} produits")
            for d in produits:
                pid = d.id.split(":")[1]
                h = suivi.setdefault(pid, {"p": []})
                anciens = [p for t, p in h["p"] if maintenant - t < 45 * JOUR and p]
                h["p"] = (h["p"] + [[int(maintenant), d.prix]])[-40:]
                h.update(n=d.titre[:150], c=cat, i=d.image, d=d.details[:100])
                if anciens and d.prix and d.prix <= statistics.median(anciens) * (1 - seuil_verif / 100):
                    baisses.add(pid)  # baisse vs nos relevés : on revérifie l'historique en priorité
    else:
        log("  LeDénicheur : catégories déjà parcourues récemment")

    # 2) historique 3 mois sur la fiche produit, à tour de rôle
    validite = lcfg.get("historique_valable_jours", 5) * JOUR
    a_lire = [pid for pid, h in suivi.items()
              if pid in baisses or maintenant - h.get("med_t", 0) > validite]
    a_lire.sort(key=lambda pid: (pid not in baisses, suivi[pid].get("med_t", 0)))
    lus = 0
    for pid in a_lire[: lcfg.get("historique_par_passage", 30)]:
        h = suivi[pid]
        r = http.get(f"{LDN}/product.php?p={pid}")
        h["med_t"] = int(maintenant)  # même en cas d'échec, pour ne pas bloquer la file
        if r is None or r.status_code != 200:
            continue
        info = analyser_produit_ldn(r.text)
        passe = historique_passe(info["historique"])
        if len(passe) >= 5:
            h["med"], h["min"] = round(statistics.median(passe), 2), min(passe)
        if info.get("prix"):
            h["p"] = (h["p"] + [[int(maintenant), info["prix"]]])[-40:]
        h["m"] = info.get("marchand") or h.get("m", "")
        lus += 1
    reste = max(0, len(a_lire) - lus)

    # 3) verdict pour chaque produit au prix récent connu
    retenus = []
    for pid, h in suivi.items():
        if not h.get("p") or maintenant - h["p"][-1][0] > 26 * 3600:
            continue
        prix = h["p"][-1][1]
        raison = verdict_ldn(prix, h.get("med"), h.get("min"), r_min)
        if not raison:
            continue
        med = h["med"]
        d = Deal(id=f"ldn:{pid}", titre=h.get("n", ""), url=f"{LDN}/product.php?p={pid}", source="LeDénicheur",
                 prix=prix, prix_ref=med if med > prix else None,
                 reduction=round((1 - prix / med) * 100) if med > prix else None,
                 marchand=h.get("m") or "LeDénicheur", image=h.get("i"), publie=h["p"][-1][0],
                 groupes={f"ldn:{h.get('c', '')}"}, details=h.get("d", ""), plus_bas=raison)
        if any(correspond(d, p, cfg) for p in profils):
            retenus.append(d)

    for k in [k for k, v in suivi.items() if not v["p"] or maintenant - v["p"][-1][0] > 30 * JOUR]:
        del suivi[k]
    avec_hist = sum(1 for h in suivi.values() if h.get("med"))
    log(f"  LeDénicheur : {lus} historiques lus ({reste} en attente), {avec_hist}/{len(suivi)} produits "
        f"avec prix habituel, {len(retenus)} vrais bons prix")
    return retenus


# ==========================================================================
# Source 3 : catalogues et pages soldes des marchands (Nike, Shopify, schema.org)
# ==========================================================================
def parser_nike(page_html: str) -> list[Deal]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    produits: list[dict] = []

    def parcourir(o):
        if isinstance(o, dict):
            if isinstance(o.get("prices"), dict) and "currentPrice" in o["prices"]:
                produits.append(o)
                return
            for v in o.values():
                parcourir(v)
        elif isinstance(o, list):
            for v in o:
                parcourir(v)

    parcourir(data)
    deals = []
    for o in produits:
        prix = o["prices"].get("currentPrice")
        ref = o["prices"].get("initialPrice")
        copie = o.get("copy") or {}
        url = (o.get("pdpUrl") or {}).get("url")
        if not (prix and url and o.get("productCode")):
            continue
        titre = (copie.get("title") or "").replace("\xa0", " ")
        sous_titre = (copie.get("subTitle") or "").replace("\xa0", " ")
        couleur = (o.get("displayColors") or {}).get("colorDescription") or ""
        deals.append(Deal(
            id=f"nike:{o['productCode']}", titre=f"{titre} · {sous_titre}" if sous_titre else titre, url=url,
            source="Marchands", prix=float(prix), prix_ref=float(ref) if ref and ref > prix else None,
            reduction=_reduction(prix, ref), marchand="Nike",
            image=(o.get("colorwayImages") or {}).get("portraitURL"), details=f"{sous_titre} {couleur}",
            publie=time.time(),
        ))
    return deals


def parser_shopify(data: dict, base: str, nom: str) -> list[Deal]:
    """Catalogue public Shopify (/products.json) : prix et prix barré (compare_at_price)."""
    hote = urlsplit(base).netloc
    deals = []
    for p in data.get("products") or []:
        variantes = [v for v in p.get("variants") or [] if v.get("available", True)] or p.get("variants") or []
        meilleur = None
        for v in variantes:
            prix, barre = prix_float(v.get("price")), prix_float(v.get("compare_at_price"))
            if prix and barre and barre > prix and (meilleur is None or prix / barre < meilleur[0] / meilleur[1]):
                meilleur = (prix, barre)
        if not meilleur:
            continue
        tags = p.get("tags") or []
        tags = " ".join(tags[:20]) if isinstance(tags, list) else str(tags)[:200]
        images = p.get("images") or []
        deals.append(Deal(
            id=f"shop:{hote}:{p.get('id')}", titre=(p.get("title") or "").strip(),
            url=f"{base}/products/{p.get('handle')}", source="Marchands",
            prix=meilleur[0], prix_ref=meilleur[1], reduction=_reduction(*meilleur), marchand=nom,
            image=images[0].get("src") if images and isinstance(images[0], dict) else None,
            details=f"{p.get('product_type') or ''} {tags}", publie=time.time(),
        ))
    return deals


def parser_page_jsonld(page_html: str, base: str, nom: str) -> list[Deal]:
    """Page liste dont les produits sont décrits en schema.org (Product + Offer + prix barré)."""
    from urllib.parse import urljoin
    produits: list[dict] = []

    def parcourir(o):
        if isinstance(o, dict):
            if "Product" in str(o.get("@type")) and o.get("offers"):
                produits.append(o)
                return
            for v in o.values():
                parcourir(v)
        elif isinstance(o, list):
            for v in o:
                parcourir(v)

    for bloc in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            parcourir(json.loads(bloc.strip(), strict=False))
        except ValueError:
            continue
    hote = urlsplit(base).netloc
    deals, vus = [], set()
    for o in produits:
        offre = o["offers"][0] if isinstance(o["offers"], list) else o["offers"]
        prix = prix_float(offre.get("price") or offre.get("lowPrice"))
        specs = offre.get("priceSpecification") or []
        specs = specs if isinstance(specs, list) else [specs]
        barres = [prix_float(sp.get("price")) for sp in specs if isinstance(sp, dict)]
        barre = max([b for b in barres if b] or [0])
        url = urljoin(base, o.get("url") or offre.get("url") or "")
        if not (prix and barre > prix and url) or url in vus:
            continue
        vus.add(url)
        img = o.get("image")
        img = img[0] if isinstance(img, list) and img else img
        deals.append(Deal(
            id=f"web:{hote}:{o.get('sku') or url}", titre=(o.get("name") or "").strip(), url=url,
            source="Marchands", prix=prix, prix_ref=barre, reduction=_reduction(prix, barre), marchand=nom,
            image=img if isinstance(img, str) else None, details=str(o.get("color") or ""), publie=time.time(),
        ))
    return deals


def _depuis_cache(etat: dict, cle: str, freq_h: float, charger) -> list[Deal]:
    """Relit une source toutes les `freq_h` heures ; entre-temps, réutilise le dernier relevé."""
    cache = etat.setdefault("cache_marchands", {}).setdefault(cle, {"t": 0, "deals": []})
    if time.time() - cache["t"] >= freq_h * 3600:
        deals = charger()
        cache["t"] = int(time.time())
        cache["deals"] = [{**d.__dict__, "groupes": list(d.groupes), "tags": list(d.tags)} for d in deals]
    return [Deal(**{**v, "groupes": set(v.get("groupes") or []), "tags": set(v.get("tags") or [])})
            for v in cache["deals"]]


PRIX_MIN_MARCHANDS = 20.0  # remplacé par marchands.prix_min dans config.yaml


def _meilleurs(deals: list[Deal], r_min: float, n: int) -> list[Deal]:
    """Les n plus grosses réductions, hors petits accessoires (sous le prix minimum)."""
    gardes = sorted((d for d in deals if (d.reduction or 0) >= r_min and (d.prix or 0) >= PRIX_MIN_MARCHANDS),
                    key=lambda d: -(d.reduction or 0))
    return gardes[:n]


def collecter_marchands(http: Http, cfg: dict, etat: dict) -> list[Deal]:
    global PRIX_MIN_MARCHANDS
    mcfg = cfg.get("marchands") or {}
    n_max = mcfg.get("max_par_marchand", 12)
    PRIX_MIN_MARCHANDS = float(mcfg.get("prix_min", 20))
    etat.pop("nike", None)  # ancien format de cache
    tous: list[Deal] = []

    nike = mcfg.get("nike") or {}
    if nike.get("actif", True) and nike.get("pages"):
        def charger_nike():
            vus: dict[str, Deal] = {}
            for page in nike["pages"]:
                r = http.get(f"https://www.nike.com/fr/w/{page}")
                if r is None or r.status_code != 200:
                    log(f"  ⚠ Nike {page} : {r.status_code if r is not None else 'ignoré'}")
                    continue
                ds = parser_nike(r.text)
                log(f"  Nike /w/{page:<36} {len(ds):>3} produits soldés")
                vus.update({d.id: d for d in ds})
            return list(vus.values())
        ds = _depuis_cache(etat, "nike", nike.get("toutes_les_heures", 2), charger_nike)
        r_min = nike.get("reduction_min", 35)
        gardes = _meilleurs(ds, r_min, nike.get("max", n_max))
        log(f"  Nike : {len(ds)} produits soldés, {len(gardes)} retenus (-{r_min} % ou plus, {nike.get('max', n_max)} max)")
        tous += gardes

    shop = mcfg.get("shopify") or {}
    if shop.get("actif", True):
        r_min = shop.get("reduction_min", 30)

        def une_boutique(b):
            base, nom = b["url"].rstrip("/"), b.get("nom") or urlsplit(b["url"]).netloc
            http_b = Http(http.delai)  # une connexion par boutique : chargées en parallèle

            def charger_shop(base=base, nom=nom):
                ds = []
                for page in range(1, shop.get("pages_max", 4) + 1):
                    r = http_b.get(f"{base}/products.json?limit=250&page={page}")
                    if r is None or r.status_code != 200:
                        if page == 1:
                            log(f"  ⚠ {nom} : {r.status_code if r is not None else 'ignoré'}")
                        break
                    try:
                        data = r.json()
                    except ValueError:
                        break
                    if not data.get("products"):
                        break
                    ds += parser_shopify(data, base, nom)
                log(f"  {nom:<45} {len(ds):>3} produits en promo")
                return ds
            ds = _depuis_cache(etat, f"shop:{base}", shop.get("toutes_les_heures", 3), charger_shop)
            return _meilleurs(ds, b.get("reduction_min", r_min), b.get("max", n_max))

        etat.setdefault("cache_marchands", {})
        with ThreadPoolExecutor(max_workers=16) as ex:
            for gardes in ex.map(une_boutique, shop.get("boutiques") or []):
                tous += gardes

    pages = mcfg.get("pages_promo") or {}
    if pages.get("actif", True):
        r_min = pages.get("reduction_min", 25)
        for pg in pages.get("pages") or []:
            def charger_page(pg=pg):
                r = http.get(pg["url"])
                if r is None or r.status_code != 200:
                    log(f"  ⚠ {pg.get('nom')} : {r.status_code if r is not None else 'ignoré'}")
                    return []
                ds = parser_page_jsonld(r.text, pg["url"], pg.get("nom") or urlsplit(pg["url"]).netloc)
                log(f"  {pg.get('nom', '')[:20]:<20} {urlsplit(pg['url']).path[:24]:<25} {len(ds):>3} produits en promo")
                return ds
            ds = _depuis_cache(etat, f"page:{pg['url']}", pages.get("toutes_les_heures", 3), charger_page)
            tous += _meilleurs(ds, pg.get("reduction_min", r_min), pg.get("max", n_max))

    log(f"  Marchands : {len(tous)} produits retenus (meilleures réductions, {n_max} max par marque)")
    return tous


# ==========================================================================
# Source 3 bis : catalogues complets (plan du site + fiches produit)
#   Le bot lit le plan du site (sitemap) de chaque enseigne, puis parcourt ses
#   fiches produit à tour de rôle (N par passage, plusieurs sites en parallèle).
#   Promo = prix barré indiqué dans la fiche, ou baisse vs les prix relevés par le bot.
# ==========================================================================
CHEMIN_CATALOGUES = ROOT / "cache" / "catalogues.json.gz"


def charger_catalogues(chemin: Path) -> dict:
    """Mémoire des catalogues (cache GitHub Actions) ; au tout premier passage, l'amorce fournie."""
    for c in (chemin, ROOT / "data" / "catalogues-amorce.json.gz"):
        try:
            with gzip.open(c, "rt", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {}


def sauver_catalogues(chemin: Path, data: dict) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(chemin)


def _lire_xml(http: Http, url: str) -> str | None:
    r = http.get(url)
    if r is None or r.status_code != 200:
        return None
    b = r.content
    if b[:2] == b"\x1f\x8b":
        try:
            b = gzip.decompress(b)
        except OSError:
            return None
    return b.decode("utf-8", "ignore")


def _locs(xml_txt: str) -> list[str]:
    return [html.unescape(x) for x in
            re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)\s*(?:\]\]>)?\s*</loc>", xml_txt)]


def decouvrir_produits(http: Http, site: dict) -> list[str]:
    """Liste des fiches produit d'un site à partir de son plan du site."""
    filtre = re.compile(site.get("filtre") or ".")
    exclure = re.compile(site["exclure"]) if site.get("exclure") else None
    max_urls = site.get("max_urls", 20000)
    a_lire, vus, urls = [site["sitemap"]], set(), []
    while a_lire and len(vus) < site.get("max_sitemaps", 40) and len(urls) < max_urls:
        sm = a_lire.pop(0)
        if sm in vus:
            continue
        vus.add(sm)
        t = _lire_xml(http, sm)
        if not t:
            continue
        if "<sitemapindex" in t:
            sous = _locs(t)
            if site.get("filtre_sitemap"):
                sous = [x for x in sous if re.search(site["filtre_sitemap"], x, re.I)]
            produits = [x for x in sous if re.search(r"produ|product|pdp", x, re.I)]
            a_lire += produits or sous
            continue
        for u in _locs(t):
            if re.search(r"\.(jpe?g|png|webp|gif|svg)(\?|$)", u, re.I):
                continue
            if filtre.search(u) and not (exclure and exclure.search(u)):
                urls.append(u)
    return list(dict.fromkeys(urls))[:max_urls]


def redirige_ailleurs(r: requests.Response, url: str) -> bool:
    """Vrai si la fiche a été redirigée vers une autre page (produit retiré → catégorie, accueil…)."""
    if not r.history:
        return False
    avant, apres = urlsplit(url).path.rstrip("/"), urlsplit(r.url).path.rstrip("/")
    return avant != apres


def lire_fiche(page_html: str, url: str, nom: str) -> tuple[float | None, float | None, str | None, str | None]:
    """(prix, prix barré, titre, image) d'une fiche produit."""
    prix, titre, image = extraire_prix_page(page_html)
    barre = None
    avec_barre = parser_page_jsonld(page_html, url, nom)
    if avec_barre:
        d = avec_barre[0]
        prix, barre = d.prix, d.prix_ref
        titre, image = titre or d.titre, image or d.image
    if re.search(r'"availability"\s*:\s*"[^"]*OutOfStock', page_html) and \
            not re.search(r'"availability"\s*:\s*"[^"]*InStock', page_html):
        return None, None, titre, image  # rupture : ignoré
    return prix, barre, (html.unescape(titre).strip() if titre else None), image


def scanner_site(site: dict, s: dict, delai: float, par_defaut: int) -> str:
    """Un passage sur un site : (re)découverte du catalogue si besoin, puis N fiches."""
    http = Http(delai)
    maintenant = time.time()
    if not s.get("urls") or maintenant - s.get("maj", 0) > site.get("rafraichir_jours", 7) * JOUR:
        urls = decouvrir_produits(http, site)
        if urls:
            s["urls"], s["maj"] = urls, int(maintenant)
            s["pos"] = s.get("pos", 0) % len(urls)
            valides = set(urls)
            s["p"] = {k: v for k, v in s.get("p", {}).items() if k in valides}
    urls = s.get("urls") or []
    if not urls:
        return f"  ⚠ {site['nom']} : plan du site illisible"
    produits = s.setdefault("p", {})
    lus = 0
    for _ in range(min(site.get("par_passage", par_defaut), len(urls))):
        u = urls[s["pos"] % len(urls)]
        s["pos"] = (s["pos"] + 1) % len(urls)
        r = http.get(u)
        if r is None:
            continue
        if r.status_code in (404, 410) or (r.status_code == 200 and redirige_ailleurs(r, u)):
            produits.pop(u, None)
            continue
        if r.status_code != 200:
            continue
        prix, barre, titre, image = lire_fiche(r.text, u, site["nom"])
        if not prix:
            produits.pop(u, None)
            continue
        e = produits.setdefault(u, {"h": []})
        e["n"], e["i"] = (titre or "")[:150], image
        e["o"] = barre if barre and barre > prix else None
        e["h"] = (e["h"] + [[int(maintenant), prix]])[-8:]
        lus += 1
    return (f"  Catalogue {site['nom']:<16} {lus:>3} fiches lues · {len(produits):>5}/{len(urls):<5} produits connus "
            f"(tour complet ≈ {len(urls) / max(1, site.get('par_passage', par_defaut)) / 48:.1f} j)")


def collecter_catalogues(cfg: dict, catalogues: dict) -> list[Deal]:
    ccfg = cfg.get("catalogues") or {}
    if not ccfg.get("actif", True) or not ccfg.get("sites"):
        return []
    delai = cfg.get("delai_entre_requetes", 2)
    par_defaut = ccfg.get("par_passage", 60)
    sites = [x for x in ccfg["sites"] if x.get("actif", True)]
    for site in sites:
        catalogues.setdefault(site["nom"], {})
    with ThreadPoolExecutor(max_workers=min(ccfg.get("paralleles", 16), len(sites) or 1)) as ex:
        for ligne in ex.map(lambda st: scanner_site(st, catalogues[st["nom"]], delai, par_defaut), sites):
            log(ligne)

    maintenant = time.time()
    r_min, baisse = ccfg.get("reduction_min", 25), ccfg.get("baisse_historique", 20)
    tous: list[Deal] = []
    for site in sites:
        candidats = []
        for u, e in (catalogues[site["nom"]].get("p") or {}).items():
            h = e.get("h") or []
            if not h or maintenant - h[-1][0] > 10 * JOUR:
                continue
            prix, ref, raison = h[-1][1], None, ""
            if e.get("o") and e["o"] > prix:
                ref = e["o"]
            elif len(h) >= 2:
                med = statistics.median(p for _, p in h[:-1])
                if prix <= med * (1 - baisse / 100):
                    ref, raison = med, f"-{round((1 - prix / med) * 100)} % vs le prix relevé par le bot ({euros(med)})"
            if not ref:
                continue
            candidats.append(Deal(
                id=f"cat:{urlsplit(u).netloc}:{hashlib.sha1(u.encode()).hexdigest()[:12]}", titre=e.get("n") or u,
                url=u, source="Marchands", prix=prix, prix_ref=ref, reduction=_reduction(prix, ref),
                marchand=site["nom"], image=e.get("i"), publie=h[-1][0], plus_bas=raison,
            ))
        gardes = _meilleurs(candidats, site.get("reduction_min", r_min), site.get("max", ccfg.get("max_par_site", 40)))
        log(f"  Catalogue {site['nom']:<16} {len(candidats):>4} produits en promo, {len(gardes)} retenus")
        tous += gardes
    return tous


def dedoublonner(deals: list[Deal]) -> list[Deal]:
    """Même produit vu par plusieurs sources (page promo + catalogue) : on garde la meilleure offre."""
    meilleurs: dict[str, Deal] = {}
    for d in deals:
        cle = d.url.split("?")[0].rstrip("/").lower() if d.source == "Marchands" else d.id
        if cle not in meilleurs or (d.reduction or 0) > (meilleurs[cle].reduction or 0):
            meilleurs[cle] = d
    return list(meilleurs.values())


# ==========================================================================
# Source 5 : comptes X (Twitter) de bons plans, via l'API officielle de X
#   Nécessite une clé d'API (variable X_BEARER_TOKEN). Sans clé, la source est ignorée.
#   Seuls les nouveaux posts depuis le dernier passage sont demandés (since_id).
# ==========================================================================
X_API = "https://api.x.com/2"
_PRIX_RE = re.compile(r"(\d{1,5}(?:[.,]\d{1,2})?)\s?(?:€|euros?\b)", re.I)
_AU_LIEU_RE = re.compile(r"(?:au lieu de|au lieu des?|avant|prix barré|ancien prix)\s*:?\s*(\d{1,5}(?:[.,]\d{1,2})?)\s?(?:€|euros?)", re.I)
_URL_RE = re.compile(r"https?://\S+")


def parser_post_x(post: dict, medias: dict, pseudo: str) -> Deal | None:
    """Transforme un post X en deal : prix, prix barré, réduction, lien du marchand, image."""
    texte = html.unescape(post.get("text") or "")
    liens = [u.get("unwound_url") or u.get("expanded_url") for u in (post.get("entities") or {}).get("urls", [])]
    liens = [u for u in liens if u and not re.match(r"https?://(x|twitter)\.com/", u)]
    propre = re.sub(r"\s+", " ", _URL_RE.sub("", texte)).strip()
    if not propre:
        return None
    prix_vus = [prix_float(m) for m in _PRIX_RE.findall(propre)]
    m_ref = _AU_LIEU_RE.search(propre)
    ref = prix_float(m_ref.group(1)) if m_ref else None
    prix = None
    if prix_vus:
        autres = [p for p in prix_vus if p and p != ref]
        prix = min(autres) if autres else None
    if ref and prix and ref <= prix:
        ref = None
    if not ref and len(prix_vus) >= 2 and prix and max(prix_vus) > prix:
        ref = max(prix_vus)  # « 39 € → 79 € » : le plus haut est l'ancien prix
    titre = re.split(r"(?<=[.!?])\s|\n", texte.strip())[0]
    titre = re.sub(r"\s+", " ", _URL_RE.sub("", titre)).strip()[:140] or propre[:140]
    image = None
    for k in (post.get("attachments") or {}).get("media_keys", []):
        m = medias.get(k) or {}
        image = m.get("url") or m.get("preview_image_url")
        if image:
            break
    try:
        publie = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        publie = time.time()
    if not (prix or liens or _PCT_RE.search(propre) or re.search(r"\b(promo|code|soldes?|bon plan|r[ée]duc)", propre, re.I)):
        return None  # pas une promo (annonce de live, discussion…)
    url = liens[0] if liens else f"https://x.com/{pseudo}/status/{post['id']}"
    return Deal(
        id=f"x:{post['id']}", titre=titre, url=url, source="X", prix=prix, prix_ref=ref,
        reduction=_reduction(prix, ref, propre), marchand=(re.sub(r"^www\.", "", urlsplit(url).netloc)
                                                       if liens else f"@{pseudo}"),
        image=image, publie=publie, details=propre[:300], groupes={f"x:{pseudo.lower()}"},
        plus_bas=f"repéré par @{pseudo}",
    )


def collecter_x(cfg: dict, etat: dict) -> list[Deal]:
    xcfg = (cfg.get("reseaux") or {}).get("x") or {}
    jeton = os.getenv("X_BEARER_TOKEN", "").strip()
    if not xcfg.get("actif", True) or not xcfg.get("comptes"):
        return []
    if not jeton:
        log("  X : pas de clé d'API (secret X_BEARER_TOKEN), source ignorée")
        return []
    xetat = etat.setdefault("x", {})
    cache = xetat.setdefault("posts", {})
    if time.time() - xetat.get("dernier", 0) >= xcfg.get("toutes_les_heures", 1) * 3600:
        xetat["dernier"] = int(time.time())
        entetes = {"Authorization": f"Bearer {jeton}", "User-Agent": "radar-cadeaux"}
        for compte in xcfg["comptes"]:
            pseudo = compte["pseudo"].lstrip("@")
            c = xetat.setdefault("comptes", {}).setdefault(pseudo.lower(), {})
            try:
                if not c.get("id"):
                    r = requests.get(f"{X_API}/users/by/username/{pseudo}", headers=entetes, timeout=20)
                    if not r.ok:
                        log(f"  ⚠ X @{pseudo} : {r.status_code} {r.text[:150]}")
                        continue
                    c["id"] = r.json()["data"]["id"]
                params = {"max_results": 20, "exclude": "replies,retweets",
                          "tweet.fields": "created_at,entities,attachments",
                          "expansions": "attachments.media_keys", "media.fields": "url,preview_image_url"}
                if c.get("since_id"):
                    params["since_id"] = c["since_id"]
                r = requests.get(f"{X_API}/users/{c['id']}/tweets", headers=entetes, params=params, timeout=20)
                if r.status_code == 429:
                    log(f"  ⚠ X @{pseudo} : quota atteint, nouvel essai au prochain passage")
                    continue
                if not r.ok:
                    log(f"  ⚠ X @{pseudo} : {r.status_code} {r.text[:150]}")
                    continue
                data = r.json()
                medias = {m["media_key"]: m for m in (data.get("includes") or {}).get("media", [])}
                posts = data.get("data") or []
                if posts:
                    c["since_id"] = max(posts, key=lambda p: int(p["id"]))["id"]
                n = 0
                for post in posts:
                    d = parser_post_x(post, medias, pseudo)
                    if d:
                        cache[d.id] = {**d.__dict__, "groupes": list(d.groupes), "tags": list(d.tags),
                                       "pour": compte.get("pour") or []}
                        n += 1
                log(f"  X @{pseudo:<30} {len(posts):>3} nouveaux posts, {n} deals")
            except (requests.RequestException, ValueError, KeyError) as e:
                log(f"  ⚠ X @{pseudo} : {e.__class__.__name__}")
    limite = time.time() - xcfg.get("garder_jours", 4) * JOUR
    for k in [k for k, v in cache.items() if v.get("publie", 0) < limite]:
        del cache[k]
    return [Deal(**{k: v for k, v in val.items() if k != "pour"}
                 | {"groupes": set(val.get("groupes") or []), "tags": set(val.get("tags") or [])})
            for val in cache.values()]


# ==========================================================================
# Source 4 : produits surveillés (liste perso)
# ==========================================================================
def _offres_jsonld(obj) -> list[float]:
    out = []
    if isinstance(obj, list):
        for o in obj:
            out += _offres_jsonld(o)
    elif isinstance(obj, dict):
        typ = obj.get("@type")
        typ = " ".join(typ) if isinstance(typ, list) else str(typ or "")
        if "Offer" in typ:
            for k in ("price", "lowPrice"):
                if prix_float(obj.get(k)):
                    out.append(prix_float(obj.get(k)))
            ps = obj.get("priceSpecification")
            if isinstance(ps, dict) and prix_float(ps.get("price")):
                out.append(prix_float(ps.get("price")))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                out += _offres_jsonld(v)
    return out


def extraire_prix_page(page_html: str) -> tuple[float | None, str | None, str | None]:
    """(prix, titre, image) d'une fiche produit : JSON-LD schema.org, sinon balises meta."""
    titre = image = None
    prix: list[float] = []
    for bloc in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            data = json.loads(bloc.strip(), strict=False)
        except ValueError:
            continue
        prix += _offres_jsonld(data)
        items = data if isinstance(data, list) else (data.get("@graph", [data]) if isinstance(data, dict) else [])
        for it in items:
            if isinstance(it, dict) and "Product" in str(it.get("@type")):
                titre = titre or it.get("name")
                im = it.get("image")
                im = im[0] if isinstance(im, list) and im else im
                image = image or (im if isinstance(im, str) else (im or {}).get("url") if isinstance(im, dict) else None)
    if not prix:
        for motif in (r'(?:property|name)=["\'](?:product|og):price:amount["\'][^>]+content=["\']([^"\']+)',
                      r'itemprop=["\']price["\'][^>]+content=["\']([^"\']+)'):
            m = re.search(motif, page_html, re.I)
            if m and prix_float(m.group(1)):
                prix.append(prix_float(m.group(1)))
    if not titre:
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page_html, re.I)
        titre = html.unescape(m.group(1)) if m else None
    if not image:
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page_html, re.I)
        image = m.group(1) if m else None
    return (min(prix) if prix else None), titre, image


def lire_demandes_github(profils: dict[str, dict]) -> list[dict]:
    """Produits à suivre ajoutés depuis la page (issues GitHub « Suivre : … » ouvertes).

    Corps attendu (pré-rempli par la page) :
        lien: https://…
        prix_cible: 120
        pour: Maman
    Fermer l'issue arrête le suivi."""
    depot, jeton = os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", "")
    if not depot:
        return []
    entetes = {"Accept": "application/vnd.github+json", "User-Agent": "radar-cadeaux"}
    if jeton:
        entetes["Authorization"] = f"Bearer {jeton}"
    try:
        r = requests.get(f"https://api.github.com/repos/{depot}/issues",
                         params={"state": "open", "per_page": 100}, headers=entetes, timeout=20)
        r.raise_for_status()
        issues = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"  ⚠ demandes GitHub illisibles : {e.__class__.__name__}")
        return []
    items = []
    for i in issues:
        titre = (i.get("title") or "").strip()
        if i.get("pull_request") or not re.match(r"(?i)(suivre|surveiller)\b", titre):
            continue
        if i.get("author_association") not in ("OWNER", "MEMBER", "COLLABORATOR"):
            continue  # dépôt public : seules tes demandes (ou celles de tes collaborateurs) comptent
        corps = i.get("body") or ""
        champ = lambda nom: (re.search(rf"(?im)^\s*{nom}\s*:\s*(.+?)\s*$", corps) or [None, ""])[1]
        lien = champ("lien") or (re.search(r"https?://\S+", corps + " " + titre) or [""])[0]
        if not lien:
            continue
        nom = re.sub(r"(?i)^(suivre|surveiller)\s*:?\s*", "", titre).strip() or lien
        pour = [p.strip() for p in re.split(r"[,;/]| et ", champ("pour")) if p.strip()]
        pour = [n for n in pour if n in profils] or [n for n in profils if normaliser(n) in normaliser(champ("pour"))]
        items.append({"nom": nom[:120], "url": lien.strip("<>"), "prix_cible": prix_float(champ("prix_cible")),
                      "pour": pour, "issue": i.get("number"), "issue_url": i.get("html_url"),
                      "origine": "page", "ajoute": i.get("created_at")})
    log(f"  Demandes de suivi depuis la page : {len(items)}")
    return items


def verifier_surveillance(http: Http, cfg: dict, etat: dict, profils: dict[str, dict],
                          items: list[dict] | None = None) -> list[Alerte]:
    alertes = []
    hist = etat.setdefault("surveillance", {})
    r_min = cfg.get("seuils", {}).get("reduction_min", 30)
    items = (cfg.get("surveillance") or []) if items is None else items
    etat["surveillance_actifs"] = [it["url"] for it in items if (it or {}).get("url")]
    for item in items:
        url = (item or {}).get("url")
        if not url:
            continue
        nom = item.get("nom") or url
        r = http.get(url)
        if r is None or r.status_code != 200:
            code = r.status_code if r is not None else "ignoré"
            log(f"  ⚠ {nom[:50]} : {code}. Ce site bloque les robots : utilise plutôt "
                f"la page LeDénicheur du produit ou un mot-clé")
            continue
        if redirige_ailleurs(r, url):
            log(f"  ⚠ {nom[:50]} : la fiche n'existe plus (redirigée vers {urlsplit(r.url).path[:50]})")
            hist.setdefault(url, {"p": [], "alerte": None})["retire"] = int(time.time())
            continue
        h = hist.setdefault(url, {"p": [], "alerte": None})
        h.pop("retire", None)
        anciens = [p for t, p in h["p"] if time.time() - t < 90 * JOUR]
        marchand = re.sub(r"^www\.", "", urlsplit(url).netloc)
        image = None
        if "ledenicheur.fr" in url:
            info = analyser_produit_ldn(r.text)
            prix = info.get("prix")
            anciens = historique_passe(info["historique"]) or anciens
            marchand = info.get("marchand") or marchand
            _, _, image = extraire_prix_page(r.text)
        else:
            prix, barre, titre_page, image = lire_fiche(r.text, url, marchand)
            if barre and prix and barre > prix:
                h["barre"] = barre
            if nom == url and titre_page:
                nom = titre_page[:120]
        if prix is None:
            log(f"  ⚠ {nom[:50]} : prix introuvable sur la page")
            continue
        h["p"] = (h["p"] + [[int(time.time()), prix]])[-500:]
        h.update(nom=nom, marchand=marchand, image=image, cible=item.get("prix_cible"), pour=item.get("pour", []),
                 issue=item.get("issue"), issue_url=item.get("issue_url"),
                 origine=item.get("origine") or "config", ajoute=item.get("ajoute") or h.get("ajoute"))
        h.setdefault("ajoute", datetime.now().isoformat(timespec="seconds"))
        if "ledenicheur.fr" in url and info["historique"]:
            h["hist"] = info["historique"]
        log(f"  Surveillance {nom[:40]:<40} {euros(prix):>10} ({marchand})")

        raison, med = None, (statistics.median(anciens) if anciens else None)
        cible = item.get("prix_cible")
        if cible and prix <= cible:
            raison = f"sous ton prix cible ({euros(cible)})"
        else:
            raison, med = verdict_historique(prix, anciens, item.get("baisse_min", r_min))
        if not raison or (h["alerte"] and prix >= h["alerte"] * 0.97):
            continue
        h["alerte"] = prix
        d = Deal(id=f"watch:{url}", titre=nom, url=url, source="Surveillance", prix=prix,
                 prix_ref=med if med and med > prix else None,
                 reduction=round((1 - prix / med) * 100) if med and med > prix else None,
                 marchand=marchand, image=image, plus_bas=raison)
        alertes.append(Alerte(d, [profils[p] for p in item.get("pour", []) if p in profils], raison))
    return alertes


# ==========================================================================
# Filtrage par profil (Maman, Papa, Sœurs, …)
# ==========================================================================
def etiqueter(deal: Deal, cfg: dict) -> set[str]:
    """Centres d'intérêt d'un deal : mots-clés, catégories (groupes) ou marchands."""
    texte, marchand = deal.texte(), normaliser(deal.marchand)
    tags = set(deal.tags)
    for c in cfg.get("centres_interet") or []:
        if premier_mot_trouve(c.get("mots_cles"), texte) \
                or deal.groupes & set(c.get("groupes") or []) \
                or any(normaliser(m).strip() and normaliser(m).strip() in marchand for m in c.get("marchands") or []):
            tags.add(c["id"])
    return tags


def correspond(deal: Deal, profil: dict, cfg: dict) -> str | None:
    """Le deal intéresse-t-il ce profil (centres d'intérêt, mots-clés, budget, exclusions) ?"""
    texte = deal.texte()
    if deal.expire:
        return None
    if profil.get("groupes") and not (deal.groupes & set(profil["groupes"])):
        return None
    if premier_mot_trouve(cfg.get("exclure_partout"), texte) or premier_mot_trouve(profil.get("exclure"), texte):
        return None
    if deal.prix is not None:
        if profil.get("budget_max") and deal.prix > profil["budget_max"]:
            return None
        if profil.get("budget_min") and deal.prix < profil["budget_min"]:
            return None
    if profil.get("tous"):
        return "coup de cœur"
    noms = {c["id"]: c.get("nom", c["id"]) for c in cfg.get("centres_interet") or []}
    communs = [i for i in profil.get("interets") or [] if i in deal.tags]
    if communs:
        return noms.get(communs[0], communs[0])
    mot = premier_mot_trouve(profil.get("mots_cles") or [], texte)
    if mot:
        return f"« {mot} »"
    marchands = [normaliser(m).strip() for m in profil.get("marchands") or []]
    if any(m and m in normaliser(deal.marchand) for m in marchands):
        return deal.marchand
    return None


def est_gros_deal(deal: Deal, profil: dict, cfg: dict) -> str | None:
    s = {**cfg.get("seuils", {}), **(profil.get("seuils") or {})}
    if deal.publie and time.time() - deal.publie > s.get("age_max_jours", 14) * JOUR:
        return None
    r_min = s.get("reduction_min", 30)
    if deal.source == "Marchands":  # déjà filtré par les seuils de chaque marque / catalogue
        return deal.plus_bas or (f"-{deal.reduction} %" if deal.reduction else None)
    if deal.source != "Dealabs":  # déjà validé par l'historique de prix
        return deal.plus_bas or (f"-{deal.reduction} %" if (deal.reduction or 0) >= r_min else None)
    chaud = deal.temperature >= s.get("temperature_min", 150)
    baisse = (deal.reduction or 0) >= r_min and deal.temperature >= s.get("temperature_plancher", 30)
    if not (chaud or baisse):
        return None
    return " · ".join(x for x in (f"{round(deal.temperature)}°" if chaud else "",
                                  f"-{deal.reduction} %" if baisse else "") if x)


def router(deal: Deal, profils: list[dict], cfg: dict) -> tuple[list[dict], str]:
    trouves, raisons = [], []
    for p in profils:
        pourquoi = correspond(deal, p, cfg)
        if not pourquoi:
            continue
        gros = est_gros_deal(deal, p, cfg)
        if gros:
            trouves.append(p)
            if not raisons:
                raisons = [pourquoi, gros]
    return trouves, " · ".join(raisons)


# ==========================================================================
# Notifications
# ==========================================================================
def message_html(a: Alerte) -> str:
    d, e = a.deal, html.escape
    tete = [x for x in (f"<b>-{d.reduction} %</b>" if d.reduction else "",
                        f"{round(d.temperature)}°" if d.temperature else "",
                        e(d.marchand) if d.marchand else "") if x]
    icone = "📉" if d.plus_bas or a.nouvelle_baisse else ("🔥" if d.temperature >= 300 else "🎁")
    lignes = [f"{icone} " + " · ".join(tete), f"<b>{e(d.titre)}</b>"]
    if d.prix is not None:
        p = f"💶 <b>{euros(d.prix)}</b>"
        if d.prix_ref and d.prix_ref > d.prix:
            p += f"  <s>{euros(d.prix_ref)}</s>"
        if d.livraison_gratuite:
            p += "  · livraison offerte"
        lignes.append(p)
    if d.code_promo:
        lignes.append(f"🎟 Code : <code>{e(d.code_promo)}</code>")
    if a.profils:
        lignes.append("🎄 Pour : " + ", ".join(e(f"{p.get('emoji', '')} {p['nom']}".strip()) for p in a.profils))
    if a.nouvelle_baisse:
        lignes.append("⬇️ Encore moins cher depuis la dernière alerte")
    lignes.append(f"<i>{e(a.raison)} · via {e(d.source)}</i>")
    lignes.append(f'<a href="{e(d.url)}">👉 Voir le deal</a>')
    return "\n".join(lignes)


def sans_html(txt: str) -> str:
    txt = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2 \1", txt)
    return html.unescape(re.sub(r"<[^>]+>", "", txt))


class Notifieur:
    def __init__(self, dry_run: bool):
        self.dry = dry_run
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.discord = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.ntfy = os.getenv("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

    def canaux(self) -> list[str]:
        return [n for n, ok in (("Telegram", self.tg_token and self.tg_chat),
                                ("Discord", self.discord), ("ntfy", self.ntfy)) if ok]

    def envoyer(self, a: Alerte) -> None:
        log("\n" + sans_html(message_html(a)) + "\n" + "-" * 60)
        if self.dry:
            return
        if self.tg_token and self.tg_chat:
            txt = message_html(a)
            ok = bool(a.deal.image) and len(txt) <= 1024 and self._tg("sendPhoto", {"photo": a.deal.image, "caption": txt})
            if not ok:
                self._tg("sendMessage", {"text": txt})
            time.sleep(1.1)
        if self.discord:
            d = a.deal
            embed = {"title": d.titre[:250], "url": d.url,
                     "description": sans_html(message_html(a)).split("\n", 2)[-1][:3500],
                     "color": 0xE8452C if d.temperature >= 300 else 0x2E8B57}
            if d.image:
                embed["thumbnail"] = {"url": d.image}
            self._post(self.discord, {"embeds": [embed]})
            time.sleep(0.6)
        if self.ntfy:
            d = a.deal
            payload = {"topic": self.ntfy, "title": (f"-{d.reduction}% " if d.reduction else "") + d.titre[:120],
                       "message": sans_html(message_html(a)), "click": d.url, "tags": ["gift"]}
            if d.image:
                payload["attach"] = d.image
            self._post(self.ntfy_server, payload)

    def texte_simple(self, titre: str, corps_html: str) -> None:
        log(f"\n{titre}\n{sans_html(corps_html)}")
        if self.dry:
            return
        if self.tg_token and self.tg_chat:
            self._tg("sendMessage", {"text": f"<b>{html.escape(titre)}</b>\n{corps_html}",
                                     "disable_web_page_preview": True})
        if self.discord:
            self._post(self.discord, {"content": f"**{titre}**\n{sans_html(corps_html)}"[:1990]})
        if self.ntfy:
            self._post(self.ntfy_server, {"topic": self.ntfy, "title": titre, "message": sans_html(corps_html)[:3500]})

    def _tg(self, methode: str, data: dict) -> bool:
        url = f"https://api.telegram.org/bot{self.tg_token}/{methode}"
        data = {"chat_id": self.tg_chat, "parse_mode": "HTML", **data}
        try:
            r = requests.post(url, json=data, timeout=20)
            if r.status_code == 429:
                time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)) + 1)
                r = requests.post(url, json=data, timeout=20)
            if not r.ok:
                log(f"  ⚠ Telegram {methode} : {r.status_code} {r.text[:200]}")
            return r.ok
        except requests.RequestException as e:
            log(f"  ⚠ Telegram : {e.__class__.__name__}")
            return False

    def _post(self, url: str, payload: dict) -> None:
        try:
            r = requests.post(url, json=payload, timeout=20)
            if not r.ok:
                log(f"  ⚠ envoi {urlsplit(url).netloc} : {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            log(f"  ⚠ envoi : {e.__class__.__name__}")


# ==========================================================================
# État
# ==========================================================================
def charger_etat(chemin: Path) -> dict:
    if chemin.exists():
        try:
            return json.loads(chemin.read_text(encoding="utf-8"))
        except ValueError:
            log("  ⚠ état illisible, on repart de zéro")
    return {}


def sauver_etat(chemin: Path, etat: dict) -> None:
    limite = time.time() - 60 * JOUR
    etat["alertes"] = {k: v for k, v in etat.get("alertes", {}).items() if v.get("t", 0) > limite}
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps(etat, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(chemin)


# ==========================================================================
# Tableau de bord (page HTML statique, publiée par GitHub Pages)
# ==========================================================================
def noter_tableau(tableau: dict, d: Deal, pour: list[dict], raison: str) -> None:
    maintenant = int(time.time())
    ancien = tableau.get(d.id, {})
    tableau[d.id] = {
        "titre": d.titre, "url": d.url, "src": d.source, "prix": d.prix, "ref": d.prix_ref,
        "red": d.reduction, "temp": round(d.temperature), "marchand": d.marchand, "img": d.image,
        "code": d.code_promo, "pour": [p["nom"] for p in pour], "raison": raison,
        "publie": int(d.publie) or None, "vu": ancien.get("vu", maintenant), "maj": maintenant,
        "prix_vu": ancien.get("prix_vu", d.prix),
        "tags": sorted(d.tags), "det": (d.details or "")[:160],
    }


def generer_tableau(cfg: dict, etat: dict, chemin: Path, artifact: bool = False) -> None:
    modele = ROOT / "tableau_modele.html"
    if not modele.exists():
        return
    maintenant = time.time()
    tableau = etat.setdefault("tableau", {})
    for k in [k for k, v in tableau.items() if maintenant - v.get("maj", 0) > 7 * JOUR]:
        del tableau[k]
    deals = [{"id": k, **v} for k, v in tableau.items()]

    surveilles = []
    actifs = set(etat.get("surveillance_actifs") or (etat.get("surveillance") or {}).keys())
    for url, h in (etat.get("surveillance") or {}).items():
        if not h.get("p") or url not in actifs:
            continue
        points = sorted(h.get("hist", {}).items())
        serie = [[int(datetime.fromisoformat(j).timestamp()), p] for j, p in points]
        dernier_hist = serie[-1][0] if serie else 0
        serie += [pt for pt in h["p"] if pt[0] > dernier_hist + 3600]
        surveilles.append({"nom": h.get("nom", url), "url": url, "marchand": h.get("marchand", ""),
                           "img": h.get("image"), "cible": h.get("cible"), "pour": h.get("pour", []),
                           "prix": h["p"][-1][1], "serie": serie[-200:], "barre": h.get("barre"),
                           "issue_url": h.get("issue_url"), "maj": h["p"][-1][0], "retire": h.get("retire"),
                           "origine": h.get("origine", "config"), "ajoute": h.get("ajoute")})

    baisses = []
    for pid, h in (etat.get("ldn") or {}).items():
        pts = h.get("p") or []
        if len(pts) < 3 or not pts[-1][1]:
            continue
        avant = [p for _, p in pts[:-1] if p]
        med, prix = statistics.median(avant), pts[-1][1]
        if med and prix <= med * 0.92 and maintenant - pts[-1][0] < 2 * JOUR:
            baisses.append({"titre": h.get("n", ""), "url": f"{LDN}/product.php?p={pid}", "img": h.get("i"),
                            "cat": h.get("c", ""), "prix": prix, "med": med,
                            "pct": round((1 - prix / med) * 100)})
    baisses.sort(key=lambda b: -b["pct"])

    donnees = {
        "maj": int(maintenant),
        "black_friday": str(cfg.get("black_friday", "2026-11-27")),
        "profils": [{"nom": p["nom"], "emoji": p.get("emoji", ""), "budget": p.get("budget_max"),
                     "interets": p.get("interets") or [], "mots": p.get("mots_cles") or [],
                     "exclure": p.get("exclure") or []}
                    for p in cfg.get("profils") or []],
        "interets": [{"id": c["id"], "nom": c.get("nom", c["id"]), "emoji": c.get("emoji", "")}
                     for c in cfg.get("centres_interet") or []],
        "deals": deals,
        "surveilles": surveilles,
        "baisses": baisses[:40],
        "suivis_ldn": len(etat.get("ldn") or {}),
        "repo": os.getenv("GITHUB_REPOSITORY") or cfg.get("depot_github") or "",
    }
    js = json.dumps(donnees, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = modele.read_text(encoding="utf-8").replace("__DONNEES__", js)
    if artifact:  # la page Claude ajoute elle-même doctype, head et body
        page = re.sub(r"<!doctype html>\s*<html[^>]*>\s*<head>\s*<meta charset[^>]*>\s*<meta name=\"viewport\"[^>]*>\s*", "", page, flags=re.I)
        page = page.replace("</head>\n<body>\n", "").replace("</body>\n</html>\n", "")
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(page, encoding="utf-8")
    log(f"Tableau de bord : {chemin} ({len(deals)} deals, {len(surveilles)} surveillés, {len(baisses)} baisses)")


# ==========================================================================
# Enseignes découvertes automatiquement (outils/decouvrir_enseignes.py)
# ==========================================================================
def fusionner_enseignes(cfg: dict, chemin: Path = ROOT / "data" / "enseignes.yaml") -> None:
    """Ajoute à la config les boutiques Shopify et catalogues détectés comme lisibles."""
    if not chemin.exists():
        return
    try:
        dec = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log("  ⚠ data/enseignes.yaml illisible : ignoré")
        return
    shop = cfg.setdefault("marchands", {}).setdefault("shopify", {})
    boutiques = shop.setdefault("boutiques", [])
    deja = {b["url"].rstrip("/").lower() for b in boutiques}
    for b in dec.get("shopify") or []:
        if b["url"].rstrip("/").lower() not in deja:
            boutiques.append({"nom": b["nom"], "url": b["url"]})
    cats = cfg.setdefault("catalogues", {}).setdefault("sites", [])
    noms = {c["nom"].lower() for c in cats}
    for c in dec.get("catalogues") or []:
        if c["nom"].lower() not in noms:
            cats.append(c)
    par_id = {c["id"]: c for c in cfg.get("centres_interet") or []}
    for ident, marques in (dec.get("interets") or {}).items():
        if ident in par_id:
            par_id[ident].setdefault("marchands", [])
            par_id[ident]["marchands"] += [m for m in marques if m not in par_id[ident]["marchands"]]
    log(f"Enseignes découvertes : {len(dec.get('shopify') or [])} Shopify, {len(dec.get('catalogues') or [])} catalogues")


# ==========================================================================
# Programme principal
# ==========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="Bot d'alertes deals Noël / Black Friday")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--etat", default=str(ROOT / "data" / "etat.json"))
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien et ne sauvegarde pas l'état")
    ap.add_argument("--test", action="store_true", help="envoie une notification de test")
    ap.add_argument("--tableau", default=str(ROOT / "docs" / "index.html"), help="page du tableau de bord")
    ap.add_argument("--catalogues", default=str(CHEMIN_CATALOGUES), help="mémoire des catalogues (gzip)")
    ap.add_argument("--artifact", action="store_true", help="page au format Artifact claude.ai")
    ap.add_argument("--tableau-seul", action="store_true", help="régénère le tableau de bord sans rien collecter")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    fusionner_enseignes(cfg)
    notif = Notifieur(args.dry_run)
    log(f"[{datetime.now():%d/%m %H:%M}] Canaux d'alerte : {', '.join(notif.canaux()) or 'aucun (console uniquement)'}")
    if args.test:
        notif.texte_simple("🎄 Test du bot deals", "Si tu lis ce message, les alertes fonctionnent ✅")
        return 0

    if args.tableau_seul:
        generer_tableau(cfg, charger_etat(Path(args.etat)), Path(args.tableau), args.artifact)
        return 0

    profils = cfg.get("profils") or []
    par_nom = {p["nom"]: p for p in profils}
    chemin_etat = Path(args.etat)
    etat = charger_etat(chemin_etat)
    premier = not etat.get("initialise")
    deja = etat.setdefault("alertes", {})
    http = Http(delai=cfg.get("delai_entre_requetes", 2))

    catalogues = charger_catalogues(Path(args.catalogues))

    def source(nom, fonction):
        log(f"{nom}…")
        try:
            return fonction()
        except Exception as e:  # une source en panne ne doit pas bloquer les autres
            log(f"  ⚠ {nom} : erreur {e.__class__.__name__}: {str(e)[:200]}")
            return []

    deals = source("Dealabs", lambda: collecter_dealabs(http, cfg, etat, profils))
    deals += source("LeDénicheur", lambda: collecter_ldn(http, cfg, etat, profils))
    deals += source("Marchands", lambda: collecter_marchands(http, cfg, etat))
    deals += source("Catalogues complets", lambda: collecter_catalogues(cfg, catalogues))
    deals = dedoublonner(deals)
    deals += source("Comptes X", lambda: collecter_x(cfg, etat))

    tableau = etat.setdefault("tableau", {})
    alertes: list[Alerte] = []
    for d in deals:
        if d.expire:
            tableau.pop(d.id, None)
            continue
        d.tags = etiqueter(d, cfg)
        pour, raison = router(d, profils, cfg)
        if any(p.get("tous") for p in pour):
            d.tags.add("coup_de_coeur")
        if not pour and d.source != "X":
            # aucune personne par défaut : gardé quand même s'il a un centre d'intérêt (pour les
            # personnes que chaque visiteur crée sur la page), sans être mis en avant
            gros = est_gros_deal(d, {}, cfg) if d.tags else None
            if gros and correspond(d, {"tous": True}, cfg):
                noter_tableau(tableau, d, [], gros)
            continue
        if not pour and d.source == "X":  # compte de confiance : affiché même sans profil précis
            noms = (etat.get("x", {}).get("posts", {}).get(d.id) or {}).get("pour") or []
            pour = [p for p in profils if p["nom"] in noms]
            raison = d.plus_bas
            if not pour:
                noter_tableau(tableau, d, [], raison)
                if d.id not in deja:  # nouveau post : mis en avant dans « À ne pas rater »
                    tableau[d.id]["alerte"] = int(time.time())
                    deja[d.id] = {"t": int(time.time()), "prix": d.prix, "titre": d.titre[:80]}
                continue
        if not pour:
            continue
        noter_tableau(tableau, d, pour, raison)
        precedent = deja.get(d.id)
        if precedent:
            if d.prix and precedent.get("prix") and d.prix <= precedent["prix"] * 0.9:
                alertes.append(Alerte(d, pour, raison, nouvelle_baisse=True))
            continue
        alertes.append(Alerte(d, pour, raison))

    suivis = (cfg.get("surveillance") or []) + source("Demandes de suivi", lambda: lire_demandes_github(par_nom))
    if suivis:
        alertes += source("Produits surveillés", lambda: verifier_surveillance(http, cfg, etat, par_nom, suivis))
    else:
        etat["surveillance_actifs"] = []

    for a in alertes:
        if a.deal.source == "Surveillance":
            noter_tableau(tableau, a.deal, a.profils, a.raison)
    alertes.sort(key=lambda a: a.deal.score(), reverse=True)
    max_envoi = cfg.get("premier_lancement_max", 8) if premier else cfg.get("max_alertes_par_passage", 15)
    a_envoyer, surplus, par_marchand = [], [], {}
    for a in alertes:  # au plus 3 alertes par marchand, pour varier
        m = a.deal.marchand or a.deal.source
        if len(a_envoyer) < max_envoi and par_marchand.get(m, 0) < 3:
            a_envoyer.append(a)
            par_marchand[m] = par_marchand.get(m, 0) + 1
        else:
            surplus.append(a)
    log(f"\n→ {len(alertes)} deal(s) retenu(s), {len(a_envoyer)} envoyé(s)"
        + (" (premier lancement : seulement les meilleurs)" if premier else ""))

    for a in a_envoyer:
        notif.envoyer(a)
        if a.deal.id in tableau:
            tableau[a.deal.id]["alerte"] = int(time.time())
    log("\n=== RÉSUMÉ ===")
    if a_envoyer:
        log(f"{len(a_envoyer)} nouveau(x) gros deal(s) :")
        for a in a_envoyer:
            pour = ", ".join(p["nom"] for p in a.profils)
            log(f"- {a.deal.titre[:90]} : {euros(a.deal.prix)}"
                + (f" (-{a.deal.reduction} %)" if a.deal.reduction else "") + f" · {a.deal.marchand} · pour {pour}")
    else:
        log("Aucun nouveau gros deal.")
    if surplus and not premier:
        lignes = [f'• <a href="{html.escape(a.deal.url)}">{html.escape(a.deal.titre[:70])}</a> : '
                  f'{euros(a.deal.prix)}' + (f' (-{a.deal.reduction} %)' if a.deal.reduction else "")
                  for a in surplus[:25]]
        notif.texte_simple(f"➕ {len(surplus)} autres deals", "\n".join(lignes))

    for a in alertes:
        deja[a.deal.id] = {"t": int(time.time()), "prix": a.deal.prix, "titre": a.deal.titre[:80]}
        etat.get("candidats_dealabs", {}).pop(a.deal.id, None)
    etat["initialise"] = True
    etat["dernier_passage"] = int(time.time())
    if not args.dry_run:
        sauver_etat(chemin_etat, etat)
        sauver_catalogues(Path(args.catalogues), catalogues)
        generer_tableau(cfg, etat, Path(args.tableau), args.artifact)
    return 0


if __name__ == "__main__":
    sys.exit(main())
