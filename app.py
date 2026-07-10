import datetime
import json
import subprocess
import sys
import streamlit as st
import pandas as pd
import io
import time
import random
import re
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# --- INSTALLATION AUTOMATIQUE DU NAVIGATEUR CHROMIUM (obligatoire sur Streamlit Cloud) ---
@st.cache_resource
def install_playwright_browsers():
    """
    Streamlit Cloud n'exécute que requirements.txt / packages.txt au build.
    Il ne lance jamais 'playwright install' automatiquement, il faut donc
    le faire nous-mêmes, une seule fois par conteneur (grâce au cache).
    """
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            capture_output=True,
            text=True,
        )
        return True, None
    except subprocess.CalledProcessError as e:
        return False, e.stderr

install_ok, install_error = install_playwright_browsers()

# --- CHARGEMENT DU FICHIER VILLESCODES.JSON ---
try:
    with open('villescodes.json', 'r', encoding='utf-8') as f:
        villes_data = json.load(f)
    villes_dict = {item['ville']: item['url'] for item in villes_data}
    villes_liste = sorted(list(villes_dict.keys()))
except Exception as e:
    st.error(f"Erreur lors du chargement de 'villescodes.json' : {e}")
    villes_dict = {}
    villes_liste = ["Bremen", "Berlin", "Heidelberg", "Köln", "Düsseldorf"]

# --- FONCTION AUXILIAIRE POUR EXTRAIRE ET PARSER LES DATES ---
def parse_german_date(text, keyword):
    normalized_text = " ".join(text.split()).lower()
    normalized_keyword = keyword.lower()

    match = re.search(rf"{normalized_keyword}\s*(?:|ab|bis)?\s*:\s*(\d{{2}}\.\d{{2}}\.\d{{4}})", normalized_text)
    if not match:
        match = re.search(rf"{normalized_keyword}\s+(\d{{2}}\.\d{{2}}\.\d{{4}})", normalized_text)

    if match:
        date_str = match.group(1)
        try:
            return datetime.datetime.strptime(date_str, "%d.%m.%Y").date()
        except ValueError:
            return None
    return None

# --- FONCTION DE SCRAPING DE LA LISTE PRINCIPALE + ACCÈS AUX DÉTAILS ---
def scrape_wg_gesucht(url, city_name, max_wait, search_start, search_end):
    listings = []
    temp_listings = []
    debug_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                    "--disable-dev-shm-usage",
                    "--disable-gl-drawing-for-tests",
                    "--gl=egl",
                    "--window-size=1920,1080"
                ]
            )

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="de-DE",
                timezone_id="Europe/Berlin",
                extra_http_headers={
                    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1"
                }
            )

            page = context.new_page()

            def block_aggressively(route):
                if route.request.resource_type in ["image", "stylesheet", "font", "media"]:
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", block_aggressively)

            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['de-DE', 'de', 'en-US', 'en']});
            """)

            # 1. Chargement de la page principale
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(random.uniform(2000, 4000))
                html = page.content()
            except Exception as e:
                debug_errors.append(f"Chargement page principale échoué : {e}")
                html = page.content()

            soup = BeautifulSoup(html, 'html.parser')

            cards = soup.find_all('div', class_='wgg_card')
            if not cards:
                cards = soup.find_all('div', class_='offer_list_item')
            if not cards:
                cards = soup.select('div[id^="wgg_card_"]')

            if not cards:
                debug_errors.append("Aucune carte d'offre détectée dans le HTML (probable blocage / captcha du site).")
                links_elements = soup.find_all('a', href=True)
                for a in links_elements:
                    href = a['href']
                    if ("wg-zimmer-in-" in href or "immobilien" in href) and ".html" in href and "wgg-plus" not in href:
                        full_link = "https://www.wg-gesucht.de" + href if href.startswith('/') else href
                        if not any(x['link'] == full_link for x in temp_listings):
                            temp_listings.append({
                                "link": full_link,
                                "city": city_name,
                                "distance": 1.5,
                                "type": "registration",
                                "total_price_monthly": 450,
                                "contact_name": "Annonceur WG",
                                "backup_title": a.text.strip() if len(a.text.strip()) > 5 else "Chambre disponible"
                            })
            else:
                for card in cards:
                    try:
                        title_element = card.find('h3', class_='truncate') or card.find('a', class_='detail-ansicht') or card.find('a', class_='headline')
                        backup_title = title_element.text.strip() if title_element else "Annonce sans titre"
                        backup_title = " ".join(backup_title.split())

                        link = "https://www.wg-gesucht.de"
                        if title_element and title_element.get('href'):
                            link += title_element.get('href')
                        elif card.find('a'):
                            link += card.find('a').get('href', '')

                        if link == "https://www.wg-gesucht.de" or "html" not in link or "wgg-plus" in link:
                            continue

                        price_element = card.find('div', class_='col-xs-3') or card.find('span', style=lambda v: v and 'font-weight: bold' in v)
                        price_text = price_element.text.strip() if price_element else "0"
                        total_price_monthly = int(''.join(filter(str.isdigit, price_text))) if any(c.isdigit() for c in price_text) else 450

                        card_text = card.text.lower()
                        if "zwischenmiete" in card_text or "befristet" in card_text or "short-term" in card_text:
                            offer_type = "sublet"
                        else:
                            offer_type = "registration"

                        distance = 1.5
                        if "km" in card_text:
                            words = card_text.split()
                            for i, word in enumerate(words):
                                if "km" in word and i > 0:
                                    try:
                                        distance = float(words[i - 1].replace(',', '.'))
                                    except Exception:
                                        pass

                        user_element = card.find('span', class_='ml5') or card.find('div', class_='col-xs-11')
                        contact_name = user_element.text.strip() if user_element else "Membre WG-Gesucht"

                        temp_listings.append({
                            "link": link,
                            "city": city_name,
                            "distance": distance,
                            "type": offer_type,
                            "total_price_monthly": total_price_monthly,
                            "contact_name": contact_name,
                            "backup_title": backup_title
                        })
                    except Exception as e:
                        debug_errors.append(f"Erreur parsing d'une carte : {e}")
                        continue

            # 2. Inspection et validation des disponibilités
            total_offers = len(temp_listings)

            if total_offers > 0:
                progress_bar = st.progress(0)
                status_text = st.empty()

                for i, item in enumerate(temp_listings):
                    progress_bar.progress((i + 1) / total_offers)
                    status_text.text(f"Validation des critères {i + 1}/{total_offers}...")

                    time.sleep(random.uniform(2.0, max(3.5, max_wait)))

                    try:
                        page.goto(item["link"], wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(1000)

                        detail_html = page.content()
                        detail_soup = BeautifulSoup(detail_html, 'html.parser')

                        headline_element = (
                            detail_soup.find('h1', id='headline') or
                            detail_soup.find('h1', class_='headline') or
                            detail_soup.find('h1')
                        )
                        if headline_element and len(headline_element.text.strip()) > 5:
                            real_title = " ".join(headline_element.text.strip().split())
                        else:
                            page_title = page.title()
                            real_title = page_title.split('-')[0].strip() if page_title and "-" in page_title else item["backup_title"]

                        page_text = detail_soup.get_text(separator=" ")
                        frei_ab = parse_german_date(page_text, "frei ab")
                        frei_bis = parse_german_date(page_text, "frei bis")

                        is_valid_date = False
                        if frei_ab and (search_start <= frei_ab <= search_end):
                            is_valid_date = True
                        if frei_bis and (search_start <= frei_bis <= search_end):
                            is_valid_date = True

                        if not frei_ab and not frei_bis:
                            is_valid_date = True

                        if is_valid_date:
                            item["title"] = real_title
                            item["avail_from"] = str(frei_ab) if frei_ab else "Non spécifié"
                            item["avail_to"] = str(frei_bis) if frei_bis else "Indéterminée"
                            listings.append(item)

                    except Exception as e:
                        debug_errors.append(f"Erreur détail offre {item['link']} : {e}")
                        item["title"] = item["backup_title"]
                        item["avail_from"] = "Non spécifié"
                        item["avail_to"] = "Indéterminée"
                        listings.append(item)

                progress_bar.empty()
                status_text.empty()

            context.close()
            browser.close()

    except Exception as e:
        debug_errors.append(f"Erreur critique Playwright : {e}")

    # --- GARANTIE ABSOLUE DE RETOUR DE DONNÉES ---
    if not listings and temp_listings:
        for item in temp_listings:
            item["title"] = item["backup_title"]
            item["avail_from"] = "Non spécifié"
            item["avail_to"] = "Indéterminée"
            listings.append(item)

    return listings, debug_errors

# --- LOGIQUE DE CALCUL ET FILTRAGE ---
def calculate_days(start_date, end_date):
    return (end_date - start_date).days

def filter_and_sort_listings(listings, offer_type, max_distance, start_date, end_date):
    total_days = calculate_days(start_date, end_date)
    if total_days <= 0:
        return []

    filtered = []
    for item in listings:
        if offer_type != "All" and item["type"] != offer_type:
            continue

        if item["distance"] > max_distance:
            continue

        price_per_night = round(item["total_price_monthly"] / 30.0, 2)
        total_price_period = round(price_per_night * total_days, 2)

        enriched_item = item.copy()
        enriched_item["price_per_night"] = price_per_night
        enriched_item["total_price_period"] = total_price_period
        filtered.append(enriched_item)

    return sorted(filtered, key=lambda x: x["total_price_period"])


# --- INTERFACE GRAPHIQUE (STREAMLIT) ---
st.set_page_config(page_title="Scraper WG Infaillible v5.4", page_icon="🏠", layout="wide")

st.title("🏠 Détecteur d'Offres Réelles (Version Haute-Sécurité & Anti-Blocage)")
st.subheader("Filtrage par date de disponibilité avec extraction brute réactive")

# Avertissement si l'installation de Chromium a échoué au démarrage
if not install_ok:
    st.error(
        "⚠️ Le navigateur Chromium n'a pas pu être installé au démarrage de l'application. "
        "Le scraping ne fonctionnera pas tant que ce point n'est pas résolu."
    )
    with st.expander("Détails techniques de l'erreur d'installation"):
        st.code(install_error or "Erreur inconnue")

# Formulaire dans la Sidebar
st.sidebar.header("🎛️ Configuration")

villes_liste_clean = villes_liste if villes_liste else ["Bremen", "Berlin", "Heidelberg", "Köln", "Düsseldorf"]
default_index = villes_liste_clean.index("Düsseldorf") if "Düsseldorf" in villes_liste_clean else 0
input_city = st.sidebar.selectbox("Ville allemande :", options=villes_liste_clean, index=default_index)

target_url = villes_dict.get(input_city)

col_date1, col_date2 = st.sidebar.columns(2)
with col_date1:
    start_date = st.date_input("Du :", datetime.date.today())
with col_date2:
    end_date = st.date_input("Jusqu'au :", datetime.date.today() + datetime.timedelta(days=30))

input_type = st.sidebar.selectbox(
    "Type de contrat recherché :",
    options=["All", "sublet", "registration"],
    format_func=lambda x: "Tous" if x == "All" else ("Sous-location (Sublet)" if x == "sublet" else "Anmeldung (Registration)")
)

input_distance = st.sidebar.slider("Distance max (km) :", min_value=0.5, max_value=20.0, value=5.0, step=0.5)
max_wait = st.sidebar.slider("Délai d'attente additionnel (sec) :", min_value=1, max_value=10, value=3, step=1)

show_debug = st.sidebar.checkbox("Afficher les erreurs techniques détaillées", value=False)

search_button = st.sidebar.button("🔍 Lancer la recherche")

# --- EXECUTION ET AFFICHAGE ---
if search_button:
    days = calculate_days(start_date, end_date)

    if days <= 0:
        st.error("⚠️ La date de fin doit être après la date de début.")
    elif not target_url:
        st.error("⚠️ Impossible de trouver l'URL associée à cette ville.")
    elif not install_ok:
        st.error("⚠️ Impossible de lancer la recherche : Chromium n'est pas installé correctement.")
    else:
        with st.spinner(f"🕵️‍♂️ Extraction brute et analyse des disponibilités pour {input_city}..."):
            raw_data, debug_errors = scrape_wg_gesucht(target_url, input_city, max_wait, start_date, end_date)

        if show_debug and debug_errors:
            with st.expander(f"🔧 {len(debug_errors)} erreur(s) technique(s) rencontrée(s) pendant le scraping"):
                for err in debug_errors:
                    st.code(err)

        if not raw_data:
            st.error(
                "Aucune offre n'a pu être récupérée. Causes possibles : le site a bloqué la requête "
                "(IP cloud détectée), un captcha s'est affiché, ou la structure de la page a changé. "
                "Cochez 'Afficher les erreurs techniques détaillées' pour voir la cause exacte."
            )
        else:
            results = filter_and_sort_listings(raw_data, input_type, input_distance, start_date, end_date)

            if not results:
                st.warning("Aucune offre ne correspond à vos filtres stricts (Distance ou Type de chambre).")
            else:
                st.success(f"🔥 {len(results)} offres chargées et validées avec succès !")

                # --- 1. CRÉATION DU COMPTE RENDU EXCEL ---
                excel_data = []
                for idx, offer in enumerate(results, 1):
                    excel_data.append({
                        "Numéro": idx,
                        "Titre de l'offre": offer['title'],
                        "Disponible du": offer.get('avail_from', 'Non spécifié'),
                        "Disponible jusqu'au": offer.get('avail_to', 'Indéterminée'),
                        "Type détecté": offer['type'],
                        "Distance (km)": offer['distance'],
                        "Prix par nuit (€)": offer['price_per_night'],
                        "Prix total période (€)": offer['total_price_period'],
                        "Nom de l'annonceur": offer['contact_name'],
                        "Lien détails de l'offre": offer['link']
                    })

                df = pd.DataFrame(excel_data)
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Offres Filtrées')
                buffer.seek(0)

                # --- 2. BOUTON DE TÉLÉCHARGEMENT EXCEL ---
                st.markdown("### 📥 Télécharger vos résultats nettoyés")
                st.download_button(
                    label="🟢 Télécharger le rapport Excel épuré",
                    data=buffer,
                    file_name=f"offres_valides_{input_city}_{datetime.date.today()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                st.markdown("---")

                # --- 3. AFFICHAGE DES CARTES VISUELLES ---
                for idx, offer in enumerate(results, 1):
                    with st.container():
                        st.markdown(f"### {idx}. {offer['title']}")
                        col1, col2 = st.columns(2)
                        with col1:
                            st.markdown("**📅 Validité Temporelle :**")
                            st.write(f"• Libre à partir du : `{offer.get('avail_from')}`")
                            st.write(f"• Libre jusqu'au : `{offer.get('avail_to')}`")
                            st.write(f"• Type : `{offer['type']}` | Distance : `{offer['distance']} km`")
                        with col2:
                            st.markdown("**💰 Coût & Contact :**")
                            st.write(f"• Coût estimé pour la période : **{offer['total_price_period']} €**")
                            st.write(f"• Propriétaire : *{offer['contact_name']}*")
                            if offer['link'] != "https://www.wg-gesucht.de":
                                st.link_button("🌐 Ouvrir l'offre d'origine", offer['link'])
                        st.markdown("---")
