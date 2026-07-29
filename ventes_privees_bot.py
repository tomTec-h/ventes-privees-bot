"""
Bot de veille "ventes privées" — sur le même principe que ton scraper Modz.fr.

CE QUE FAIT CE SCRIPT :
1. Scrape https://www.mes-ventes-privees.com/ (ventes DU JOUR) et filtre par % de
   réduction minimum -> envoie une alerte Discord pour chaque NOUVELLE vente qui matche.
2. Scrape https://www.mes-ventes-privees.com/ventes-privees-a-venir (ventes À VENIR)
   -> régénère un fichier calendrier .ics avec toutes les prochaines ventes.
3. Mémorise les ventes déjà notifiées dans seen_deals.json pour ne jamais spammer
   deux fois la même alerte.

À FAIRE AVANT DE LANCER (comme pour Modz.fr) :
- pip install requests beautifulsoup4 ics --break-system-packages   (en local si besoin)
- Sur PythonAnywhere : pip3.10 install --user requests beautifulsoup4 ics
- Configure DISCORD_WEBHOOK_URL ci-dessous (voir GUIDE.md pour le créer)
- Adapte ITEM SELECTOR si le site a changé sa structure HTML (voir GUIDE.md,
  section "si le scraper ne trouve plus rien")
"""

import json
import re
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from ics import Calendar, Event

# ============================ CONFIG À PERSONNALISER ============================

# Récupéré depuis les paramètres Discord de ton serveur (voir GUIDE.md)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Seuil minimum de réduction pour déclencher une alerte (en %)
MIN_DISCOUNT = 30

# Liste blanche de marques à surveiller en priorité (laisser vide [] pour tout suivre)
# Exemple : ["SANDRO", "MAJE", "THE KOOPLES", "BA&SH", "VANESSA BRUNO"]
BRAND_WATCHLIST = []

# Fichiers de suivi (créés automatiquement à côté du script)
BASE_DIR = Path(__file__).parent
SEEN_FILE = BASE_DIR / "seen_deals.json"
ICS_FILE = BASE_DIR / "ventes_privees.ics"  # à héberger pour l'abonnement calendrier

URL_DU_JOUR = "https://www.mes-ventes-privees.com/"
URL_A_VENIR = "https://www.mes-ventes-privees.com/ventes-privees-a-venir"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36"}

# =================================================================================


def fetch_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def parse_deals(soup: BeautifulSoup):
    """
    Extrait chaque vente de la page. On se base sur le lien de détail
    (icône 'i'), qui a un format stable : /vente-privee-{slug}-chez-{site}/{HASH}
    C'est le point d'ancrage le plus fiable pour retrouver le bloc d'une vente,
    même si le design du site change.

    Si le site a changé sa structure et que ça ne matche plus rien,
    voir GUIDE.md section "si le scraper ne trouve plus rien".
    """
    deals = []
    detail_links = soup.select('a[href*="/vente-privee-"]')

    for link in detail_links:
        href = link.get("href", "")
        deal_id = href.rstrip("/").split("/")[-1]  # le HASH final = identifiant unique

        # Le bloc contenant toutes les infos de la vente est le parent commun
        container = link.find_parent(["li", "div"])
        if container is None:
            continue
        block_text = container.get_text(" ", strip=True)

        # Marque : texte du premier lien vers /soldes/... dans le bloc
        brand_link = container.select_one('a[href*="/soldes/"]')
        brand = brand_link.get_text(strip=True) if brand_link else None
        if not brand:
            # fallback : texte alt de l'image
            img = container.find("img")
            brand = img.get("alt", "").split(" en ")[0].split(" à ")[0].strip() if img else "MARQUE INCONNUE"

        # Réduction
        discount_match = re.search(r"-(\d{1,3})%", block_text)
        discount = int(discount_match.group(1)) if discount_match else None

        # Site source (SHOWROOMPRIVÉ, THE BRADERY, BAZARCHIC...)
        site_match = re.search(r"\b(SHOWROOMPRIV[ÉE]|THE BRADERY|BAZARCHIC|VEEPEE|ZALANDO PRIV[ÉE]|PRIVATE SPORT SHOP|BEAUT[ÉE] PRIV[ÉE])\b", block_text, re.IGNORECASE)
        site = site_match.group(1).upper() if site_match else "SITE INCONNU"

        # Timing : "commence aujourd'hui" / "commence demain" / "commence dans X jours"
        if "aujourd'hui" in block_text.lower():
            days_until = 0
        elif "demain" in block_text.lower():
            days_until = 1
        else:
            days_match = re.search(r"dans\s+(\d+)\s+jours?", block_text, re.IGNORECASE)
            days_until = int(days_match.group(1)) if days_match else None

        deals.append({
            "id": deal_id,
            "brand": brand,
            "discount": discount,
            "site": site,
            "days_until": days_until,
            "url": "https://www.mes-ventes-privees.com" + href if href.startswith("/") else href,
        })

    # Dédoublonnage par id (chaque vente peut apparaître via plusieurs liens du même bloc)
    unique = {d["id"]: d for d in deals if d["id"]}
    return list(unique.values())


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen_ids):
    SEEN_FILE.write_text(json.dumps(sorted(seen_ids)), encoding="utf-8")


def matches_watchlist(brand: str) -> bool:
    if not BRAND_WATCHLIST:
        return True
    return any(w.upper() in brand.upper() for w in BRAND_WATCHLIST)


def send_discord_alert(deal: dict):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_WEBHOOK_URL non configuré — alerte non envoyée:", deal["brand"])
        return

    discount_txt = f"-{deal['discount']}%" if deal["discount"] else "réduction non précisée"
    payload = {
        "embeds": [{
            "title": f"🔥 {deal['brand']} — {discount_txt}",
            "description": f"Sur **{deal['site']}**",
            "url": deal["url"],
            "color": 15158332,
        }]
    }
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    if resp.status_code >= 300:
        print(f"❌ Erreur envoi Discord ({resp.status_code}) pour {deal['brand']}: {resp.text}")
    else:
        print(f"✅ Alerte envoyée : {deal['brand']} ({discount_txt}) sur {deal['site']}")


def build_calendar(upcoming_deals: list):
    """Régénère le fichier .ics avec toutes les ventes à venir (le 'planning')."""
    cal = Calendar()
    today = datetime.now().date()

    for deal in upcoming_deals:
        if deal["days_until"] is None:
            continue
        event = Event()
        discount_txt = f" (-{deal['discount']}%)" if deal["discount"] else ""
        event.name = f"{deal['brand']} sur {deal['site']}{discount_txt}"
        event_date = today + timedelta(days=deal["days_until"])
        event.begin = event_date.isoformat()
        event.make_all_day()
        event.description = deal["url"]
        event.uid = f"{deal['id']}@ventes-privees-bot"
        cal.events.add(event)

    ICS_FILE.write_text(str(cal), encoding="utf-8")
    print(f"📅 Calendrier régénéré : {ICS_FILE} ({len(cal.events)} ventes à venir)")


def main():
    seen = load_seen()

    # --- 1. Ventes du jour -> alertes Discord ---
    soup_jour = fetch_soup(URL_DU_JOUR)
    deals_jour = parse_deals(soup_jour)
    print(f"{len(deals_jour)} ventes trouvées aujourd'hui.")

    new_ids = set()
    for deal in deals_jour:
        if deal["id"] in seen:
            continue
        if deal["discount"] is not None and deal["discount"] >= MIN_DISCOUNT and matches_watchlist(deal["brand"]):
            send_discord_alert(deal)
        new_ids.add(deal["id"])

    save_seen(seen | new_ids)

    # --- 2. Ventes à venir -> planning calendrier ---
    soup_a_venir = fetch_soup(URL_A_VENIR)
    deals_a_venir = parse_deals(soup_a_venir)
    print(f"{len(deals_a_venir)} ventes à venir trouvées.")
    build_calendar(deals_a_venir)


if __name__ == "__main__":
    main()
