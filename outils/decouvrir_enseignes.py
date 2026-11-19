#!/usr/bin/env python3
"""
Teste automatiquement une liste d'enseignes (outils/enseignes.yaml) et ajoute au
bot celles dont le catalogue est lisible, dans data/enseignes.yaml :

  1. Boutique Shopify : catalogue public /products.json (prix + prix barré).
  2. Plan du site (sitemap) + fiches produit lisibles (prix schema.org).
  3. Sinon : « bloqué » (anti-robots) ou « prix illisibles » (chargés après coup).

Le bot lit data/enseignes.yaml à chaque passage : rien à copier à la main.
Les sites sont testés en parallèle, poliment (1 page par seconde et par site,
robots.txt respecté).

  python outils/decouvrir_enseignes.py            # teste toute la liste
  python outils/decouvrir_enseignes.py --nouveaux # seulement les sites pas encore testés
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

LISTE = ROOT / "outils" / "enseignes.yaml"
SORTIE = ROOT / "data" / "enseignes.yaml"
FR_URL = r"/fr([-_][a-z]{2})?(/|$)"


def _robots_sitemaps(http: bot.Http, base: str) -> list[str]:
    try:
        r = http.s.get(base + "/robots.txt", timeout=15)
        texte = r.text if r.status_code == 200 else ""
    except Exception:
        texte = ""
    sm = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", texte)
    # d'abord les plans du site français, puis les autres
    sm.sort(key=lambda s: (not re.search(r"(^|[^a-z])fr([^a-z]|$)", s.lower()), len(s)))
    return sm[:4] or [base + "/sitemap.xml", base + "/sitemap_index.xml"]


def tester(e: dict) -> dict:
    nom = e["nom"]
    base = "https://" + re.sub(r"^https?://", "", e["site"].strip()).rstrip("/")
    res = {"nom": nom, "site": base, "interet": e.get("interet")}
    http = bot.Http(1.0)
    try:
        r = http.get(base + "/")
    except Exception as ex:  # noqa: BLE001
        return {**res, "etat": f"injoignable ({ex.__class__.__name__})"}
    if r is None:
        return {**res, "etat": "injoignable"}
    if r.status_code in (401, 403, 429, 503):
        return {**res, "etat": f"bloqué ({r.status_code})"}
    if r.status_code >= 400:
        return {**res, "etat": f"erreur {r.status_code}"}
    base = f"https://{urlsplit(r.url).netloc}"
    accueil = r.text

    # 1) Shopify
    if "cdn.shopify.com" in accueil or "Shopify.shop" in accueil or "shopify" in r.headers.get("x-powered-by", "").lower():
        rj = http.get(base + "/products.json?limit=100")
        if rj is not None and rj.status_code == 200:
            try:
                produits = rj.json().get("products") or []
            except ValueError:
                produits = []
            if produits:
                devise = re.search(r'Shopify\.currency\s*=\s*\{"active":"([A-Z]{3})"', accueil)
                if devise and devise.group(1) != "EUR":
                    return {**res, "etat": f"Shopify en {devise.group(1)} (ignoré)"}
                promo = sum(1 for p in produits for v in (p.get("variants") or [])[:1]
                            if bot.prix_float(v.get("compare_at_price")) and
                            bot.prix_float(v.get("compare_at_price")) > (bot.prix_float(v.get("price")) or 0))
                return {**res, "etat": "ok", "type": "shopify", "url": base, "promo_echantillon": promo}

    # 2) Plan du site + fiches produit
    for sm in _robots_sitemaps(http, base):
        for filtre_sitemap in ("fr", None):
            conf = {"nom": nom, "sitemap": sm, "max_urls": 600, "max_sitemaps": 6}
            if filtre_sitemap:
                conf["filtre_sitemap"] = filtre_sitemap
            try:
                urls = bot.decouvrir_produits(http, conf)
            except Exception:  # noqa: BLE001
                urls = []
            urls = [u for u in urls if urlsplit(u).netloc.endswith(urlsplit(base).netloc.replace("www.", ""))]
            if len(urls) < 10:
                continue
            filtre = None
            fr = [u for u in urls if re.search(FR_URL, u, re.I)]
            if fr and len(fr) < len(urls):
                urls, filtre = fr, FR_URL
            lus = prix_ok = barres = 0
            for u in random.sample(urls, min(5, len(urls))):
                rr = http.get(u)
                if rr is None or rr.status_code != 200:
                    continue
                lus += 1
                prix, barre, _, _ = bot.lire_fiche(rr.text, u, nom)
                prix_ok += bool(prix)
                barres += bool(barre)
            if prix_ok >= 2:
                cat = {"nom": nom, "sitemap": sm, "par_passage": 30}
                if filtre_sitemap:
                    cat["filtre_sitemap"] = filtre_sitemap
                if filtre:
                    cat["filtre"] = filtre
                return {**res, "etat": "ok", "type": "catalogue", "catalogue": cat,
                        "prix_barre": barres > 0, "fiches_testees": lus}
            if lus:
                break  # fiches trouvées mais prix illisibles : inutile d'essayer sans filtre
    return {**res, "etat": "prix illisibles (chargés après coup ou plan du site absent)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nouveaux", action="store_true", help="ne teste que les sites absents du dernier rapport")
    ap.add_argument("--paralleles", type=int, default=16)
    args = ap.parse_args()

    liste = (yaml.safe_load(LISTE.read_text(encoding="utf-8")) or {}).get("enseignes") or []
    ancien = yaml.safe_load(SORTIE.read_text(encoding="utf-8")) if SORTIE.exists() else {}
    deja = {x["site"] for x in (ancien or {}).get("rapport", [])}
    if args.nouveaux:
        a_tester = [e for e in liste if "https://" + re.sub(r"^https?://", "", e["site"]).rstrip("/") not in deja]
    else:
        a_tester = liste
    print(f"{len(a_tester)} enseignes à tester…", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.paralleles) as ex:
        resultats = list(ex.map(tester, a_tester))

    # fusion avec le rapport précédent (mode --nouveaux)
    par_site = {x["site"]: x for x in (ancien or {}).get("rapport", [])} if args.nouveaux else {}
    for r in resultats:
        par_site[r["site"]] = r
    rapport = sorted(par_site.values(), key=lambda x: (x["etat"] != "ok", x["nom"].lower()))

    shopify = [{"nom": r["nom"], "url": r["url"]} for r in rapport if r["etat"] == "ok" and r["type"] == "shopify"]
    catalogues = [r["catalogue"] for r in rapport if r["etat"] == "ok" and r["type"] == "catalogue"]
    interets: dict[str, list[str]] = {}
    for r in rapport:
        if r["etat"] == "ok" and r.get("interet"):
            interets.setdefault(r["interet"], []).append(r["nom"])

    SORTIE.parent.mkdir(parents=True, exist_ok=True)
    entete = ("# Généré par outils/decouvrir_enseignes.py — ne pas modifier à la main.\n"
              f"# {len(shopify)} boutiques Shopify, {len(catalogues)} catalogues, "
              f"{len(rapport) - len(shopify) - len(catalogues)} non lisibles.\n")
    SORTIE.write_text(entete + yaml.safe_dump({"shopify": shopify, "catalogues": catalogues, "interets": interets,
                                               "rapport": [{k: r[k] for k in ("nom", "site", "etat")} for r in rapport]},
                                              allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    ok = [r for r in rapport if r["etat"] == "ok"]
    print(f"\nTerminé en {time.time() - t0:.0f} s : {len(ok)} enseignes lisibles sur {len(rapport)} "
          f"({len(shopify)} Shopify, {len(catalogues)} catalogues).")
    for r in rapport:
        print(f"  {'✅' if r['etat'] == 'ok' else '·'} {r['nom']:<28} {r['etat'] if r['etat'] != 'ok' else r['type']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
