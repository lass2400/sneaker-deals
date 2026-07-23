"""
Sneaker Deal Checker — Version Finale (Revente + Telegram + Boucle)
"""

import feedparser
import requests
import re
import json
import csv
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

FLUX_RSS = [
    "https://www.dealabs.com/rss/groupe/chaussures",
    "https://www.dealabs.com/rss/groupe/nike",
    "https://www.dealabs.com/rss/groupe/new-balance",
    "https://www.dealabs.com/rss/groupe/mode-accessoires",
]

# --- CONFIGURATION TELEGRAM ---
ACTIVER_TELEGRAM = True
TELEGRAM_TOKEN = "8996396273:AAEUJ5xaYb3lNGzLytSmv_NywMZemCYw20o"
TELEGRAM_CHAT_ID = "5343001436"

# --- CONFIGURATION BOUCLE AUTOMATIQUE ---
INTERVALLE_MINUTES = 30  # Vérification toutes les 30 minutes

TAILLES_HOMME = [40, 41, 42, 43, 44]
TAILLES_FEMME = [36, 37, 38, 39]

PRIX_ORIGINAL_MIN = 150      # La paire doit valoir au moins ça neuve
PRIX_PROMO_MIN = 80          # Le prix promo doit rester au-dessus de ça
PRIX_PROMO_MAX = None        # Plafond optionnel

NB_RESULTATS_MIN = 1
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

    def marge_estimee(self) -> Optional[float]:
        if self.prix_promo is not None and self.prix_original is not None:
            return round(self.prix_original - self.prix_promo, 2)
        return None

    def a_une_taille_valide(self) -> bool:
        if not self.tailles_mentionnees:
            return True
        cibles = set(TAILLES_HOMME + TAILLES_FEMME)
        return any(t in cibles for t in self.tailles_mentionnees)

    def passe_le_seuil(self) -> bool:
        if self.prix_promo is None:
            return False
        if self.prix_promo < PRIX_PROMO_MIN:
            return False
        if PRIX_PROMO_MAX is not None and self.prix_promo > PRIX_PROMO_MAX:
            return False
        if self.prix_original is not None and self.prix_original < PRIX_ORIGINAL_MIN:
            return False
        return self.a_une_taille_valide()

    def score_tri(self) -> float:
        marge = self.marge_estimee()
        if marge is not None:
            return marge
        if self.reduction_pct is not None:
            return self.reduction_pct
        return -1


def envoyer_telegram(message: str):
    if not ACTIVER_TELEGRAM:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"⚠️ Erreur d'envoi Telegram : {e}")


def extraire_reduction(texte: str) -> Optional[float]:
    match = re.search(r"-\s*(\d{1,3})\s*%", texte)
    if not match:
        match = re.search(r"(\d{1,3})\s*%\s*(?:de\s*)?(?:réduction|remise|rabais)", texte, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def extraire_prix_promo(texte: str) -> Optional[float]:
    match = re.search(r"(\d+[.,]\d{2})\s*€", texte)
    if not match:
        match = re.search(r"\b(\d{1,4})\s*€", texte)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


def extraire_prix_original(texte: str, prix_promo: Optional[float], reduction: Optional[float]) -> Optional[float]:
    match = re.search(r"(?:au lieu de|prix normal|prix initial)\s*:?\s*(\d+[.,]?\d*)\s*€", texte, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(",", "."))
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


def recuperer_deals(url_flux: str) -> List[Deal]:
    try:
        reponse = requests.get(url_flux, headers=HEADERS, timeout=10)
        reponse.raise_for_status()
    except requests.RequestException as e:
        print(f"⚠️ Erreur réseau sur {url_flux} : {e}")
        return []

    flux = feedparser.parse(reponse.content)
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
        ))
    return deals


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
        print(f"⚠️ Impossible de sauvegarder l'historique : {e}")


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


def executer_verification():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔍 Recherche des meilleures promos chaussures...")
    liens_deja_vus = charger_historique()

    tous_les_deals: List[Deal] = []
    for url in FLUX_RSS:
        deals = recuperer_deals(url)
        tous_les_deals.extend(deals)

    vus = set()
    deals_uniques = []
    for d in tous_les_deals:
        if d.lien and d.lien not in vus:
            vus.add(d.lien)
            deals_uniques.append(d)

    nouveaux_deals = [d for d in deals_uniques if d.lien not in liens_deja_vus]
    bonnes_affaires = [d for d in nouveaux_deals if d.passe_le_seuil()]
    bonnes_affaires.sort(key=lambda d: d.score_tri(), reverse=True)
    bonnes_affaires = bonnes_affaires[:NB_RESULTATS_MAX]

    if not bonnes_affaires:
        print("Aucun nouveau deal correspondant aux critères pour l'instant.")
    else:
        print(f"✅ {len(bonnes_affaires)} bonne(s) affaire(s) trouvée(s) !")
        for d in bonnes_affaires:
            marge = d.marge_estimee()
            marge_txt = f" | Marge: +{marge}€" if marge is not None else ""
            reduc = f" (-{d.reduction_pct:.0f}%)" if d.reduction_pct else ""
            prix = f" — {d.prix_promo}€" if d.prix_promo else ""
            prix_orig = f" (au lieu de {d.prix_original}€)" if d.prix_original else ""
            tailles = f" [tailles: {', '.join(str(t) for t in d.tailles_mentionnees)}]" if d.tailles_mentionnees else ""
            
            print(f"• {d.titre}{prix}{prix_orig}{reduc}{marge_txt}")
            print(f"  {d.lien}\n")

            msg_telegram = f"🔥 <b>{d.titre}</b>{prix}{prix_orig}{reduc}{marge_txt}{tailles}\n🔗 {d.lien}"
            envoyer_telegram(msg_telegram)

        exporter_csv(bonnes_affaires)

    liens_deja_vus.update(d.lien for d in deals_uniques if d.lien)
    sauvegarder_historique(liens_deja_vus)


def main():
    print("🤖 Bot Sneaker Deals démarré en mode continu.")
    print(f"⏱️ Vérification toutes les {INTERVALLE_MINUTES} minutes. (Appuie sur Ctrl+C pour arrêter).\n")
    
    while True:
        try:
            executer_verification()
        except Exception as e:
            print(f"⚠️ Une erreur est survenue : {e}")

        prochaine_verif = INTERVALLE_MINUTES * 60
        print(f"\n⏳ Prochaine vérification dans {INTERVALLE_MINUTES} minutes...\n" + "-"*50)
        time.sleep(prochaine_verif)


if __name__ == "__main__":
    main()