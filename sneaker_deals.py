"""
Sneaker Deal Checker — v3 (orienté revente)
==============================================
Filtre les chaussures dont le prix ORIGINAL dépasse un seuil (ex: 150€)
et dont le prix EN PROMO reste au-dessus d'un plancher (ex: 80€) — logique
"paires à forte valeur, prix d'entrée correct pour la revente".

NOUVEAUTÉS PAR RAPPORT À LA V2 :
- Filtre sur prix d'origine ET prix promo (plus seulement le %)
- Le prix d'origine est déduit automatiquement si Dealabs ne l'affiche
  pas explicitement, via : prix_promo / (1 - reduction / 100)
- Marge brute estimée en € (prix_original - prix_promo), triée en priorité
  car plus pertinente que le % pour la revente
- Historique local pour ne pas revoir 2x le même deal (fichier deals_vus.json,
  créé automatiquement à côté du script)
- Export CSV de chaque session (dossier exports/)
- Requêtes plus robustes : User-Agent + timeout, pour éviter des blocages
  silencieux qui donneraient 0 résultat sans explication

INSTALLATION :
    pip install feedparser requests

USAGE :
    python sneaker_deals.py
"""

import feedparser
import requests
import re
import json
import csv
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional

# ----------------------------------------------------------------------
# TELEGRAM (notifications gratuites, le PC n'a pas besoin d'être allumé
# quand ce script tourne sur GitHub Actions)
# ----------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def envoyer_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ℹ️  Pas de config Telegram (variables d'env manquantes), notification ignorée.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️  Erreur Telegram ({resp.status_code}) : {resp.text}")
    except requests.RequestException as e:
        print(f"⚠️  Erreur d'envoi Telegram : {e}")

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

from urllib.parse import quote

# 1) Flux dédiés Dealabs (bons plans déjà repérés par la communauté)
FLUX_DEALABS = [
    "https://www.dealabs.com/rss/groupe/chaussures",
    "https://www.dealabs.com/rss/groupe/nike",
    "https://www.dealabs.com/rss/groupe/new-balance",
    "https://www.dealabs.com/rss/groupe/mode-accessoires",
    # Pour ajouter d'autres catégories : va sur dealabs.com/groupe,
    # clique sur une catégorie (ex: "Jordan"), note le slug dans l'URL
    # puis ajoute "https://www.dealabs.com/rss/groupe/<slug>" ici.
]

# 2) Recherche large sur tout le web via Google News RSS (un flux par requête)
REQUETES_GOOGLE_NEWS = [
    "chaussures promo -50%",
    "sneakers soldes déstockage",
    "nike promo code réduction",
    "adidas promotion soldes",
    "chaussures destockage taille",
]

# 3) Recherche ciblée sur des revendeurs fiables précis via Bing (supporte site:)
SITES_FIABLES = [
    "nike.com", "adidas.fr", "footlocker.fr", "courir.com",
    "sarenza.com", "spartoo.fr", "zalando.fr", "jdsports.fr",
    "snipes.com", "size.co.uk", "asos.fr", "decathlon.fr",
]
REQUETES_BING = [
    f"chaussures promo ({' OR '.join('site:' + s for s in SITES_FIABLES)})",
    f"sneakers soldes ({' OR '.join('site:' + s for s in SITES_FIABLES)})",
]


def construire_flux_google_news(requete: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(requete)}&hl=fr&gl=FR&ceid=FR:fr"


def construire_flux_bing(requete: str) -> str:
    return f"https://www.bing.com/search?q={quote(requete)}&format=rss"


FLUX_RSS = (
    FLUX_DEALABS
    + [construire_flux_google_news(q) for q in REQUETES_GOOGLE_NEWS]
    + [construire_flux_bing(q) for q in REQUETES_BING]
)

TAILLES_HOMME = [40, 41, 42, 43, 44]
TAILLES_FEMME = [36, 37, 38, 39]

PRIX_ORIGINAL_MIN = 150     # la paire doit valoir plus que ça neuve
PRIX_PROMO_MIN = 80          # le prix promo doit rester au-dessus de ça
PRIX_PROMO_MAX = None        # mets une valeur (ex: 200) si tu veux aussi un plafond, sinon None

# Un deal n'est retenu QUE si son titre/résumé contient au moins un de ces mots.
# C'est ce filtre qui empêche les vestes, t-shirts etc. de passer.
MOTS_CLES_CHAUSSURES = [
    "chaussure", "chaussures", "sneaker", "sneakers", "basket", "baskets",
    "running", "trail", "nike", "adidas", "jordan", "new balance", "asics",
    "puma", "reebok", "converse", "vans", "salomon", "on running", "hoka",
    "skechers", "yeezy", "veja", "timberland", "dr martens", "ugg",
]

NB_RESULTATS_MAX = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

FICHIER_HISTORIQUE = "deals_vus.json"
DOSSIER_EXPORTS = "exports"


@dataclass
class Deal:
    titre: str
    lien: str
    prix_promo: Optional[float]
    prix_original: Optional[float]
    reduction_pct: Optional[float]
    tailles_mentionnees: List[float]
    texte_complet: str = ""

    def marge_estimee(self) -> Optional[float]:
        if self.prix_promo is not None and self.prix_original is not None:
            return round(self.prix_original - self.prix_promo, 2)
        return None

    def concerne_des_chaussures(self) -> bool:
        texte = (self.texte_complet or self.titre).lower()
        return any(mc in texte for mc in MOTS_CLES_CHAUSSURES)

    def a_une_taille_valide(self) -> bool:
        if not self.tailles_mentionnees:
            return True  # pas d'info -> on ne filtre pas dessus
        cibles = set(TAILLES_HOMME + TAILLES_FEMME)
        return any(t in cibles for t in self.tailles_mentionnees)

    def passe_le_seuil(self) -> bool:
        if not self.concerne_des_chaussures():
            return False
        if self.prix_promo is None:
            return False  # sans prix promo connu, impossible de juger le deal
        if self.prix_promo < PRIX_PROMO_MIN:
            return False
        if PRIX_PROMO_MAX is not None and self.prix_promo > PRIX_PROMO_MAX:
            return False
        if self.prix_original is None or self.prix_original < PRIX_ORIGINAL_MIN:
            return False  # le prix d'origine DOIT être connu ET dépasser le seuil
        return self.a_une_taille_valide()

    def score_tri(self) -> float:
        marge = self.marge_estimee()
        if marge is not None:
            return marge
        if self.reduction_pct is not None:
            return self.reduction_pct  # repli si pas de marge calculable
        return -1


# ----------------------------------------------------------------------
# EXTRACTION DEPUIS LE TITRE/RÉSUMÉ
# ----------------------------------------------------------------------

def extraire_reduction(texte: str) -> Optional[float]:
    match = re.search(r"-\s*(\d{1,3})\s*%", texte)
    if not match:
        match = re.search(r"(\d{1,3})\s*%\s*(?:de\s*)?(?:réduction|remise|rabais)", texte, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def extraire_prix_promo(texte: str) -> Optional[float]:
    """Prend le premier prix en euros mentionné (généralement le prix promo,
    Dealabs affichant le prix final en premier dans le titre)."""
    match = re.search(r"(\d+[.,]\d{2})\s*€", texte)
    if not match:
        match = re.search(r"\b(\d{1,4})\s*€", texte)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


def extraire_prix_original(texte: str, prix_promo: Optional[float], reduction: Optional[float]) -> Optional[float]:
    # 1) Mention explicite : "au lieu de 199,99€" / "prix normal 199€"
    match = re.search(r"(?:au lieu de|prix normal|prix initial)\s*:?\s*(\d+[.,]?\d*)\s*€", texte, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(",", "."))

    # 2) Déduit à partir du prix promo + réduction
    if prix_promo is not None and reduction is not None and reduction < 100:
        return round(prix_promo / (1 - reduction / 100), 2)

    return None


def extraire_tailles(texte: str) -> List[float]:
    tailles = set()

    for m in re.finditer(r"du\s+(\d{2}(?:[.,]\d)?)\s+au\s+(\d{2}(?:[.,]\d)?)", texte, re.IGNORECASE):
        debut = float(m.group(1).replace(",", "."))
        fin = float(m.group(2).replace(",", "."))
        t = debut
        while t <= fin:
            tailles.add(t)
            t += 1

    for m in re.finditer(r"\b(3[4-9]|4[0-9])\s*-\s*(3[4-9]|4[0-9])\b", texte):
        debut, fin = int(m.group(1)), int(m.group(2))
        if debut <= fin:
            for t in range(debut, fin + 1):
                tailles.add(float(t))

    for m in re.finditer(r"(?:taille|pointure)s?\s*:?\s*(3[4-9]|4[0-9])(?:[.,]5)?\b", texte, re.IGNORECASE):
        tailles.add(float(m.group(1)))

    return sorted(tailles)


# ----------------------------------------------------------------------
# RÉCUPÉRATION DES DEALS
# ----------------------------------------------------------------------

def libelle_flux(url: str) -> str:
    if "dealabs.com" in url:
        return f"Dealabs/{url.split('/')[-1]}"
    if "news.google.com" in url:
        return "Google News: " + url.split("q=")[1].split("&")[0][:40]
    if "bing.com" in url:
        return "Bing: " + url.split("q=")[1].split("&")[0][:40]
    return url[:50]


def recuperer_deals(url_flux: str) -> List[Deal]:
    try:
        reponse = requests.get(url_flux, headers=HEADERS, timeout=10)
        reponse.raise_for_status()
    except requests.RequestException as e:
        print(f"⚠️  Erreur réseau sur {url_flux} : {e}")
        return []

    flux = feedparser.parse(reponse.content)

    if flux.bozo and not flux.entries:
        print(f"⚠️  Flux illisible : {url_flux} (format inattendu ou vide).")
        return []

    deals = []
    for entree in flux.entries:
        titre = entree.get("title", "")
        lien = entree.get("link", "")
        resume = entree.get("summary", "")
        texte_complet = f"{titre} {resume}"

        reduction = extraire_reduction(texte_complet)
        prix_promo = extraire_prix_promo(texte_complet)
        prix_original = extraire_prix_original(texte_complet, prix_promo, reduction)

        deals.append(Deal(
            titre=titre,
            lien=lien,
            prix_promo=prix_promo,
            prix_original=prix_original,
            reduction_pct=reduction,
            tailles_mentionnees=extraire_tailles(texte_complet),
            texte_complet=texte_complet,
        ))
    return deals


# ----------------------------------------------------------------------
# HISTORIQUE (éviter de revoir 2x le même deal)
# ----------------------------------------------------------------------

def charger_historique() -> set:
    if os.path.exists(FICHIER_HISTORIQUE):
        try:
            with open(FICHIER_HISTORIQUE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError):
            return set()
    return set()


def sauvegarder_historique(liens_vus: set):
    try:
        with open(FICHIER_HISTORIQUE, "w", encoding="utf-8") as f:
            json.dump(list(liens_vus), f)
    except IOError as e:
        print(f"⚠️  Impossible de sauvegarder l'historique : {e}")


# ----------------------------------------------------------------------
# EXPORT CSV
# ----------------------------------------------------------------------

def exporter_csv(deals: List[Deal]):
    os.makedirs(DOSSIER_EXPORTS, exist_ok=True)
    horodatage = datetime.now().strftime("%Y-%m-%d_%Hh%M")
    chemin = os.path.join(DOSSIER_EXPORTS, f"deals_{horodatage}.csv")

    with open(chemin, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Titre", "Prix promo (€)", "Prix original (€)", "Marge estimée (€)", "Réduction (%)", "Tailles", "Lien"])
        for d in deals:
            writer.writerow([
                d.titre,
                d.prix_promo,
                d.prix_original,
                d.marge_estimee(),
                d.reduction_pct,
                ", ".join(str(t) for t in d.tailles_mentionnees),
                d.lien,
            ])
    print(f"📄 Export CSV : {chemin}")


# ----------------------------------------------------------------------
# MOTEUR PRINCIPAL
# ----------------------------------------------------------------------

def main():
    print("🔍 Recherche des meilleures promos chaussures sur Dealabs...\n")

    liens_deja_vus = charger_historique()

    tous_les_deals: List[Deal] = []
    for url in FLUX_RSS:
        deals = recuperer_deals(url)
        print(f"  → {libelle_flux(url)} : {len(deals)} deal(s) trouvé(s) dans le flux")
        tous_les_deals.extend(deals)

    # dédoublonnage par lien
    vus = set()
    deals_uniques = []
    for d in tous_les_deals:
        if d.lien and d.lien not in vus:
            vus.add(d.lien)
            deals_uniques.append(d)

    print(f"\n{len(deals_uniques)} deal(s) unique(s) au total.\n")

    # On exclut les deals déjà vus lors d'un précédent lancement
    nouveaux_deals = [d for d in deals_uniques if d.lien not in liens_deja_vus]
    if len(nouveaux_deals) < len(deals_uniques):
        print(f"({len(deals_uniques) - len(nouveaux_deals)} deal(s) déjà vus lors d'un précédent lancement, masqués)\n")

    # Filtre strict : chaussures uniquement + valeur d'origine >150€ + promo ≥80€ + taille
    bonnes_affaires = [d for d in nouveaux_deals if d.passe_le_seuil()]
    bonnes_affaires.sort(key=lambda d: d.score_tri(), reverse=True)
    bonnes_affaires = bonnes_affaires[:NB_RESULTATS_MAX]

    if not bonnes_affaires:
        print(f"Aucune chaussure (nouvelle) ne respecte tes critères (>{PRIX_ORIGINAL_MIN}€ neuf, promo ≥{PRIX_PROMO_MIN}€) sur ce passage.")
        print("C'est normal de temps en temps — on retente dans 2h.")
        envoyer_telegram(
            f"👟 Aucune offre chaussures ne correspond à tes critères pour le moment "
            f"(>{PRIX_ORIGINAL_MIN}€ neuf, promo ≥{PRIX_PROMO_MIN}€).\n"
            f"Prochain scan dans ~2h. ✅ Le bot est bien connecté."
        )
        # On met quand même à jour l'historique pour ne pas re-scanner les mêmes deals hors-critères sans fin
        liens_deja_vus.update(d.lien for d in deals_uniques if d.lien)
        sauvegarder_historique(liens_deja_vus)
        return

    print(f"✅ {len(bonnes_affaires)} bonne(s) affaire(s) trouvée(s) :\n")

    messages_telegram = []
    for d in bonnes_affaires:
        marge = d.marge_estimee()
        marge_txt = f" | marge estimée: {marge}€" if marge is not None else ""
        reduc = f" (-{d.reduction_pct:.0f}%)" if d.reduction_pct else ""
        prix = f" — {d.prix_promo}€" if d.prix_promo else ""
        prix_orig = f" (au lieu de {d.prix_original}€)" if d.prix_original else ""
        tailles = f" [tailles: {', '.join(str(t) for t in d.tailles_mentionnees)}]" if d.tailles_mentionnees else ""
        print(f"• {d.titre}{prix}{prix_orig}{reduc}{marge_txt}{tailles}")
        print(f"  {d.lien}\n")

        # Format HTML pour Telegram (gras sur le titre, lien cliquable)
        messages_telegram.append(
            f"👟 <b>{d.titre}</b>\n"
            f"💰 {d.prix_promo}€{prix_orig}{reduc}{marge_txt}\n"
            f"{('📏 Tailles: ' + ', '.join(str(t) for t in d.tailles_mentionnees)) if d.tailles_mentionnees else ''}\n"
            f"<a href='{d.lien}'>Voir le deal</a>"
        )

    # Un message Telegram par deal (plus lisible qu'un pavé unique)
    if messages_telegram:
        entete = f"🔔 {len(messages_telegram)} deal(s) chaussures repéré(s) (>{PRIX_ORIGINAL_MIN}€ neuf, promo ≥{PRIX_PROMO_MIN}€)"
        envoyer_telegram(entete)
        for msg in messages_telegram:
            envoyer_telegram(msg)
            time.sleep(1)  # évite de spammer l'API Telegram

    # Mise à jour de l'historique + export CSV
    liens_deja_vus.update(d.lien for d in deals_uniques if d.lien)
    sauvegarder_historique(liens_deja_vus)
    exporter_csv(bonnes_affaires)


if __name__ == "__main__":
    main()
