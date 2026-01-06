import csv
import random
import time
import re
import os
import logging
from playwright.sync_api import sync_playwright
import pytz
from datetime import datetime, timedelta
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from validation import validate_busyness_percent, validate_url, ValidationError
# Configure logging
logger = logging.getLogger(__name__)
RESTAURANT_URLS = [
    "https://maps.app.goo.gl/XfCcjGbFchX6GwbS6",  # We The Pizza
    "https://maps.app.goo.gl/xiBEPisiFWZWbjgH6",  # Wiseguy Pizza Pentagon City
    "https://maps.app.goo.gl/Qtephz6bS1xspR568",  # Extreme Pizza
    "https://maps.app.goo.gl/WbuP6DADmwzyih5J8",  # Pizzato Pizza
    "https://maps.app.goo.gl/hjbdLgKxtpZTg4gGA",  # Matchbox Pentagon City
    "https://maps.app.goo.gl/zywFkKdy27Xa3ixe6",  # Villa Pizza
    "https://maps.app.goo.gl/CM22GsozZupYyqVQ8",  # California Pizza Kitchen
    "https://maps.app.goo.gl/AEEmDgA3ZsbreCgc8",  # Domino's Pizza
]

GAY_BAR_URLS = [
    "https://maps.app.goo.gl/4pvjcqoabLzr1ak87",  # Freddie's Beach Bar
]

SPORTS_BAR_URLS = [
    "https://maps.app.goo.gl/84sX3twWH3MTyZkm6",  # Crystal City Sports Pub
]
OUTPUT_FILE = "structured_popular_times.csv"
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
START_HOUR = 6
HOURS_PER_DAY = 20
EST = pytz.timezone('US/Eastern')
def scrape_popular_times(page, restaurant_url, index_offset):
    """Scrape popular times data (SYNC version for eventlet compatibility)"""
    data = []
    page.goto(restaurant_url, timeout=60000)
    page.wait_for_timeout(4000)  # Let the page settle/load

    elements = page.query_selector_all('div[aria-label*="Popular times"] [aria-label*="at"]')
    aria_labels = []

    for el in elements:
        aria = el.get_attribute('aria-label')
        if aria and re.search(r"\d+% busy", aria):
            aria_labels.append(aria.strip())

    logger.info(f"📊 Found {len(aria_labels)} time entries")
    
    structured = []
    for i, label in enumerate(aria_labels[:140]):  # Max 7 days × 20 hours
        day_index = i // HOURS_PER_DAY
        
        # Debug: print first few labels to see what we're working with
        if i < 5:
            logger.debug(f"🔍 Processing label {i}: '{label}'")
            pattern_with_period = r'at (\d{1,2}) (AM|PM)\.'
            pattern_without_period = r'at (\d{1,2}) (AM|PM)'
            logger.debug(f"   Pattern test with period: {bool(re.search(pattern_with_period, label))}")
            logger.debug(f"   Pattern test without period: {bool(re.search(pattern_without_period, label))}")
          # Extract the actual hour from the aria-label text - this is the source of truth
        # Note: Google Maps uses Unicode narrow no-break space (\u202f) between hour and AM/PM
        time_match = re.search(r"at (\d{1,2})\u202f(AM|PM)\.?", label)
        if not time_match:
            # Fallback: try with regular space in case some use normal spaces
            time_match = re.search(r"at (\d{1,2}) (AM|PM)\.?", label)
        
        if time_match:
            hour_12 = int(time_match.group(1))
            meridiem = time_match.group(2)
            
            # Convert to 24-hour format
            if meridiem == "AM":
                hour_24 = hour_12 if hour_12 != 12 else 0
            else:  # PM
                hour_24 = hour_12 if hour_12 == 12 else hour_12 + 12
            
            hour_label = f"{hour_12} {meridiem}"
            # Debug: print successful extraction
            if i < 5:
                logger.debug(f"✅ Extracted: {hour_12} {meridiem} -> {hour_24}")
            if day_index >= len(DAYS): 
                break
            percent_match = re.search(r"(\d+)%", label)
            busyness_percent = int(percent_match.group(1)) if percent_match else None
            # Validate busyness percent
            if busyness_percent is not None:
                try:
                    busyness_percent = validate_busyness_percent(busyness_percent)
                except ValidationError as e:
                    print(f"⚠️ Invalid busyness value: {e}")
                    busyness_percent = None
            
            structured.append({
                "restaurant_url": restaurant_url,
                "weekday": DAYS[day_index],
                "hour_24": hour_24,
                "hour_label": hour_label,
                "index": index_offset + i,
                "value": label,
                "busyness_percent": busyness_percent
            })
        else:
            # Skip entries where we can't extract the time
            logger.warning(f"⚠️ Could not extract time from: '{label}'")
            logger.warning(f"   Raw bytes: {repr(label)}")
    return structured

def scrape_current_hour():
    """Scrape only the current hour's data for all restaurants (SYNC version)"""
    # Get current time in EST
    current_time = datetime.now(EST)
    current_weekday = current_time.strftime('%A')
    current_hour_24 = current_time.hour
    # Adjust for Google Maps' day structure: 12 AM belongs to previous day
    if current_hour_24 == 0:
        target_weekday = (current_time - timedelta(days=1)).strftime('%A')
        target_hour = 24
        logger.info(f"🕐 Current EST time: {current_time.strftime('%A %I:%M %p')} (Hour {current_hour_24})")
        logger.info(f"📅 Looking for PREVIOUS day's ({target_weekday}) data at hour 24 (12 AM)")
        logger.info(f"🔍 Logic: 12 AM on {current_weekday} = Hour 24 of {target_weekday}")
    else:
        target_weekday = current_weekday
        target_hour = current_hour_24
        logger.info(f"🕐 Current EST time: {current_time.strftime('%A %I:%M %p')} (Hour {current_hour_24})")
        logger.info(f"📅 Looking for TODAY's ({target_weekday}) data at hour {target_hour}")

    logger.info(f"🎯 Priority: LIVE data > Historical data > No data")

    results = []
    all_scraped_data = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage'
            ]
        )
        # Create context with US locale to bypass EU consent page
        context = browser.new_context(
            locale='en-US',
            timezone_id='America/New_York',
            geolocation={'latitude': 38.8719, 'longitude': -77.0563},
            permissions=['geolocation'],
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        # Set Google consent cookie to bypass consent screen
        context.add_cookies([{
            'name': 'CONSENT',
            'value': 'YES+cb.20231229-04-p0.en+FX+410',
            'domain': '.google.com',
            'path': '/'
        }])
        page = context.new_page()
        # Process both restaurants and gay bars
        all_urls = []
        for url in RESTAURANT_URLS:
            try:
                validated_url = validate_url(url)
                all_urls.append((validated_url, "restaurant"))
            except ValidationError as e:
                print(f"⚠️ Invalid restaurant URL: {e}")
        
        for url in GAY_BAR_URLS:
            try:
                validated_url = validate_url(url)
                all_urls.append((validated_url, "gay_bar"))
            except ValidationError as e:
                print(f"⚠️ Invalid gay bar URL: {e}")

        for url in SPORTS_BAR_URLS:
            try:
                validated_url = validate_url(url)
                all_urls.append((validated_url, "sports_bar"))
            except ValidationError as e:
                print(f"⚠️ Invalid sports bar URL: {e}")

        first_url_processed = False
        consent_handled = False
        for url, venue_type in all_urls:
            try:
                logger.info(f"\n🔍 Checking current hour for: {url} (Type: {venue_type})")
                page.goto(url, timeout=60000)
                # Wait for page to load
                page.wait_for_timeout(3000)

                # Handle Google consent page (GDPR) - only need to do this once per session
                if not consent_handled:
                    try:
                        # Look for consent buttons in multiple languages
                        consent_selectors = [
                            'button:has-text("Tout accepter")',      # French
                            'button:has-text("Accept all")',         # English
                            'button:has-text("Alle akzeptieren")',   # German
                            'button:has-text("Accepteer alles")',    # Dutch
                            'button:has-text("Aceptar todo")',       # Spanish
                            'form[action*="consent"] button',        # Generic form button
                            '[aria-label*="Accept"]',                # Aria label
                        ]

                        for selector in consent_selectors:
                            consent_btn = page.query_selector(selector)
                            if consent_btn:
                                logger.info(f"🍪 Found consent button, clicking: {selector}")
                                consent_btn.click()
                                page.wait_for_timeout(3000)  # Wait for redirect after consent
                                consent_handled = True
                                logger.info(f"✅ Consent accepted, page should reload")
                                break

                        if not consent_handled:
                            # Check if we're on consent page by title
                            title = page.title()
                            if "Avant" in title or "Before" in title or "consent" in title.lower():
                                logger.warning(f"⚠️ On consent page but couldn't find button. Title: {title}")
                            else:
                                consent_handled = True  # Not a consent page
                                logger.info(f"📄 Page title: {title} - No consent needed")
                    except Exception as consent_err:
                        logger.error(f"Error handling consent: {consent_err}")
                        consent_handled = True  # Don't retry

                # Wait for Popular Times to load after consent handling
                page.wait_for_timeout(4000)

                # DEBUG: Save screenshot of first URL
                if not first_url_processed:
                    first_url_processed = True
                    try:
                        debug_path = "/app/data/debug_screenshot.png"
                        page.screenshot(path=debug_path, full_page=True)
                        logger.info(f"📸 DEBUG: Screenshot saved to {debug_path}")
                        title = page.title()
                        logger.info(f"📸 DEBUG: Page title = '{title}'")
                    except Exception as debug_err:
                        logger.error(f"📸 DEBUG: Screenshot failed - {debug_err}")

                # Try to scroll to trigger lazy loading of Popular Times
                try:
                    page.evaluate('window.scrollTo(0, 500)')
                    page.wait_for_timeout(2000)
                except:
                    pass

                # STEP 1: Look for LIVE data first
                logger.info(f"  🔴 Step 1: Searching for LIVE data...")
                live_data = None

                # Look for live text indicators (English + French)
                live_text_patterns = {
                    # English
                    r"busier than usual": {"flag": True, "confidence": "HIGH", "estimated_percentage": 75},
                    r"as busy as it gets": {"flag": True, "confidence": "MAXIMUM", "estimated_percentage": 100},
                    r"not busy": {"flag": False, "confidence": "LOW", "estimated_percentage": 10},
                    r"not too busy": {"flag": False, "confidence": "LOW", "estimated_percentage": 15},
                    r"usually not busy": {"flag": False, "confidence": "LOW", "estimated_percentage": 15},
                    # French
                    r"[Pp]lus anim[ée] que d.habitude": {"flag": True, "confidence": "HIGH", "estimated_percentage": 75},
                    r"[Tt]r[èe]s anim[ée]": {"flag": True, "confidence": "MAXIMUM", "estimated_percentage": 100},
                    r"[Pp]as anim[ée]": {"flag": False, "confidence": "LOW", "estimated_percentage": 10},
                    r"[Pp]eu anim[ée]": {"flag": False, "confidence": "LOW", "estimated_percentage": 15},
                    r"[Hh]abituellement peu anim[ée]": {"flag": False, "confidence": "LOW", "estimated_percentage": 15},
                }
                # Get all text content from the page
                page_text = page.evaluate('document.body.innerText')
                logger.debug(f"    📝 Scanning page text for live indicators...")
                
                live_text_indicator = None
                for pattern, info in live_text_patterns.items():
                    if re.search(pattern, page_text, re.IGNORECASE):
                        live_text_indicator = {
                            "text": pattern,
                            "flag": info["flag"],
                            "confidence": info["confidence"],
                            "estimated_percentage": info["estimated_percentage"]
                        }
                        flag_emoji = "🚨" if info["flag"] else "✅"
                        logger.info(f"    {flag_emoji} FOUND LIVE TEXT: '{pattern}' (Flag: {info['flag']}, Confidence: {info['confidence']})")
                        break
                # Look for live percentage data
                live_percentage_selectors = [
                    '[aria-label*="% busy"], [aria-label*="% Busy"]',
                    '[aria-label*="right now"], [aria-label*="Right now"]',
                    '[aria-label*="currently"], [aria-label*="Currently"]',
                ]
                for selector in live_percentage_selectors:
                    try:
                        elements = page.query_selector_all(selector)
                        logger.debug(f"    📊 Checking selector '{selector}': found {len(elements)} elements")
                        for el in elements:
                            aria = el.get_attribute('aria-label')
                            if not aria:
                                continue
                                
                            logger.debug(f"      Examining: {aria}")
                            
                            # Look for live data patterns (busyness % without time reference)
                            if re.search(r"\d+% busy", aria, re.IGNORECASE) and "at" not in aria.lower():
                                percent_match = re.search(r"(\d+)%", aria)
                                if percent_match:
                                    try:
                                        live_percentage = validate_busyness_percent(int(percent_match.group(1)))
                                    except ValidationError as e:
                                        print(f"⚠️ Invalid live busyness value: {e}")
                                        continue
                                    
                                    live_data = {
                                        "restaurant_url": url,
                                        "weekday": target_weekday,
                                        "hour_24": current_hour_24,
                                        "hour_label": f"{current_hour_24 % 12 or 12} {'AM' if current_hour_24 < 12 else 'PM'}",
                                        "timestamp": current_time.isoformat(),
                                        "value": f"{aria} (LIVE DATA - {current_time.strftime('%I:%M %p')})",
                                        "busyness_percent": live_percentage,
                                        "data_type": "LIVE",
                                        "venue_type": venue_type
                                    }
                                    logger.info(f"      🔴 FOUND LIVE PERCENTAGE: {live_percentage}% busy right now!")
                                    break
                        if live_data:
                            break
                    except Exception as e:
                        logger.info(f"        Error with selector {selector}: {e}")
                # If we found text indicator but no percentage, use text indicator
                if not live_data and live_text_indicator:
                    live_data = {
                        "restaurant_url": url,
                        "weekday": target_weekday,
                        "hour_24": current_hour_24,
                        "hour_label": f"{current_hour_24 % 12 or 12} {'AM' if current_hour_24 < 12 else 'PM'}",
                        "timestamp": current_time.isoformat(),
                        "value": f"Live text indicator: '{live_text_indicator['text']}' (LIVE DATA - {current_time.strftime('%I:%M %p')})",
                        "busyness_percent": live_text_indicator["estimated_percentage"],
                        "data_type": "LIVE",
                        "live_flag": live_text_indicator["flag"],
                        "confidence": live_text_indicator["confidence"],
                        "venue_type": venue_type
                    }
                # STEP 2: If no live data, get historical data (your existing logic)
                historical_data = None
                if not live_data:
                    logger.info(f"  📊 Step 2: No live data found, using historical data...")

                    # Try multiple selectors for different languages
                    elements = []
                    historical_selectors = [
                        # English selector
                        'div[aria-label*="Popular times"] [aria-label*="at"]',
                        # French - look for bars in the Horaires d'affluence section
                        '[aria-label*="Horaires"] [aria-label*="%"]',
                        '[aria-label*="affluence"] [aria-label*="%"]',
                        # Generic - any element with % busy pattern
                        '[aria-label*="% busy"]',
                        '[aria-label*="%"][aria-label*="h"]',
                    ]

                    for selector in historical_selectors:
                        try:
                            elements = page.query_selector_all(selector)
                            if elements:
                                logger.info(f"  📊 Found {len(elements)} elements with selector: {selector}")
                                break
                        except Exception as sel_err:
                            logger.debug(f"  Selector {selector} failed: {sel_err}")

                    logger.info(f"  📊 Found {len(elements)} total time elements")

                    # DEBUG: Log first 5 aria-labels to see the format
                    for i, el in enumerate(elements[:5]):
                        debug_aria = el.get_attribute('aria-label')
                        logger.info(f"  🔎 DEBUG aria-label[{i}]: {debug_aria}")

                    all_time_data = []
                    for i, el in enumerate(elements):
                        aria = el.get_attribute('aria-label')
                        # French format has space: "0 %" vs English "0%"
                        if not aria or not re.search(r"\d+\s*%", aria):
                            continue

                        # Try English format first: "at X AM/PM"
                        time_match = re.search(r"at (\d{1,2})\u202f(AM|PM)\.?", aria)
                        if not time_match:
                            time_match = re.search(r"at (\d{1,2}) (AM|PM)\.?", aria)

                        # Try French format: "X h" or "Xh" (24-hour format)
                        hour_24 = None
                        hour_12 = None
                        meridiem = None

                        if not time_match:
                            # French format: look for "XX h" or "à XXh"
                            french_time_match = re.search(r"(\d{1,2})\s*h", aria)
                            if french_time_match:
                                hour_24 = int(french_time_match.group(1))
                                hour_12 = hour_24 % 12 or 12
                                meridiem = "AM" if hour_24 < 12 else "PM"

                        if time_match:
                            # English format
                            hour_12 = int(time_match.group(1))
                            meridiem = time_match.group(2)
                            if meridiem == "AM":
                                hour_24 = hour_12 if hour_12 != 12 else 0
                            else:
                                hour_24 = hour_12 if hour_12 == 12 else hour_12 + 12

                        # Process if we have valid hour data (from either English or French)
                        if hour_24 is not None:
                            display_hour = 24 if hour_24 == 0 else hour_24
                            # French has space before %: "0 %" vs English "0%"
                            percent_match = re.search(r"(\d+)\s*%", aria)
                            if percent_match:
                                try:
                                    busyness_percent = validate_busyness_percent(int(percent_match.group(1)))
                                except ValidationError as e:
                                    print(f"⚠️ Invalid busyness value: {e}")
                                    busyness_percent = None
                            else:
                                busyness_percent = None
                            data_entry = {
                                "scrape_timestamp": current_time.isoformat(),
                                "restaurant_url": url,
                                "element_index": i,
                                "hour_24": hour_24,
                                "display_hour": display_hour,
                                "hour_12": hour_12,
                                "meridiem": meridiem,
                                "hour_label": f"{hour_12} {meridiem}",
                                "busyness_percent": busyness_percent,
                                "raw_aria_label": aria,
                                "is_target_hour": display_hour == target_hour,
                                "target_weekday": target_weekday,
                                "target_hour": target_hour
                            }

                            all_time_data.append(data_entry)
                            all_scraped_data.append(data_entry)
                    if all_time_data:
                        # Detect day cycles based on hour patterns
                        hour_sequence = [d["display_hour"] for d in all_time_data]
                        logger.info(f"  📋 Raw hour sequence: {hour_sequence}")

                        day_cycles = []
                        current_cycle = []
                        logger.info(f"  🔄 Analyzing hour cycles to detect days...")
                        logger.info(f"     Looking for 6 AM to mark new day boundaries...")
                        
                        for idx, data in enumerate(all_time_data):
                            hour = data["hour_24"]
                            
                            # If we see hour 6 (6 AM) and we already have data, start a new cycle
                            if hour == 6 and current_cycle:
                                logger.info(f"    📅 Day boundary detected at element {data['element_index']} (6 AM)")
                                logger.info(f"       Previous cycle had {len(current_cycle)} hours: {[d['display_hour'] for d in current_cycle]}")
                                day_cycles.append(current_cycle)
                                current_cycle = []
                            
                            current_cycle.append(data)
                        # Add the last cycle
                        if current_cycle:
                            logger.info(f"    📅 Final cycle has {len(current_cycle)} hours: {[d['display_hour'] for d in current_cycle]}")
                            day_cycles.append(current_cycle)

                        logger.info(f"  📊 Detected {len(day_cycles)} day cycles total")
                        
                        # FIXED: Better day assignment logic
                        # Google Maps typically shows data starting from several days ago up to today
                        # The pattern appears to be: [Sunday, Monday, Tuesday, Wednesday, Thursday, Friday, Saturday]
                        # But the first cycle (index 0) is actually Monday (today), not Sunday
                        logger.info(f"  📅 Assigning day names to cycles...")
                        logger.info(f"     Current day: {current_weekday}")
                        logger.info(f"     Target day for search: {target_weekday}")
                        
                        # Calculate what day each cycle should represent
                        # Based on the CSV data, cycle 0 = Monday (today), so we need to work backwards
                        day_names = {}
                        
                        # Find today's cycle by looking for the target weekday's position in the week
                        weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                        current_day_index = weekdays.index(current_weekday)
                        # Assign days to cycles assuming cycle 0 represents today (Monday)
                        for cycle_idx in range(len(day_cycles)):
                            # Calculate the day offset from today
                            # cycle 0 = today (Monday), cycle 1 = tomorrow (Tuesday), etc.
                            # but we need to wrap around the week
                            day_offset = cycle_idx
                            actual_day_index = (current_day_index + day_offset) % 7
                            day_names[cycle_idx] = weekdays[actual_day_index]
                            # Update cycle assignment in scraped data
                            cycle = day_cycles[cycle_idx]
                            cycle_hours = sorted(set(d["display_hour"] for d in cycle))
                            for data in cycle:
                                data["detected_cycle"] = cycle_idx
                                data["cycle_hours_count"] = len(cycle_hours)
                                data["cycle_start_hour"] = min(cycle_hours) if cycle_hours else None
                                data["cycle_end_hour"] = max(cycle_hours) if cycle_hours else None
                                data["assigned_weekday"] = day_names[cycle_idx]
                                data["day_offset"] = day_offset
                                data["is_today_cycle"] = cycle_idx == 0  # Cycle 0 is today
                            logger.info(f"    Cycle {cycle_idx} = {day_names[cycle_idx]} (today + {day_offset} days)")
                            logger.info(f"       Hours: {cycle_hours}")

                        # Find target historical data
                        for cycle_idx, cycle in enumerate(day_cycles):
                            cycle_day_name = day_names.get(cycle_idx, "Unknown")
                            if cycle_day_name == target_weekday:
                                logger.info(f"    ✅ Found target day cycle {cycle_idx} ({target_weekday})")
                                for data in cycle:
                                    if data["display_hour"] == target_hour:
                                        historical_data = {
                                            "restaurant_url": url,
                                            "weekday": target_weekday,
                                            "hour_24": current_hour_24,
                                            "hour_label": f"{data['hour_12']} {data['meridiem']}",
                                            "timestamp": current_time.isoformat(),
                                            "value": data["raw_aria_label"] + f" (HISTORICAL - Cycle {cycle_idx})",
                                            "busyness_percent": data["busyness_percent"],
                                            "data_type": "HISTORICAL",
                                            "venue_type": venue_type
                                        }
                                        logger.info(f"    📊 Found historical data: {data['busyness_percent']}% at {data['hour_12']} {data['meridiem']}")
                                        break
                                break
                # STEP 3: Determine final data to use
                if live_data:
                    final_data = live_data
                    detection_method = "LIVE"
                    confidence = live_data.get("confidence", "N/A")
                    live_flag = live_data.get("live_flag", "N/A")
                    logger.info(f"  ✅ Using LIVE data: {live_data['busyness_percent']}% (Flag: {live_flag})")
                elif historical_data:
                    final_data = historical_data
                    logger.info(f"  ✅ Using HISTORICAL data: {historical_data['busyness_percent']}% (fallback)")
                else:
                    final_data = {
                        "restaurant_url": url,
                        "weekday": target_weekday,
                        "hour_24": current_hour_24,
                        "hour_label": f"{current_hour_24 % 12 or 12} {'AM' if current_hour_24 < 12 else 'PM'}",
                        "timestamp": current_time.isoformat(),
                        "value": f"No data available for {target_weekday} at hour {target_hour}",
                        "busyness_percent": None,
                        "data_type": "NO_DATA",
                        "venue_type": venue_type
                    }
                    logger.info(f"  ❌ No data available for {target_weekday} at hour {target_hour}")
                results.append(final_data)
                            
            except Exception as e:
                logger.info(f"❌ Error scraping {url}: {e}")
            time.sleep(2)
        browser.close()
    # Save all scraped data to CSV
    if all_scraped_data:
        scraped_data_file = f"data/all_scraped_data_{current_time.strftime('%Y%m%d_%H%M%S')}.csv"
        os.makedirs("data", exist_ok=True)
        scraped_fieldnames = [
            "scrape_timestamp", "restaurant_url", "element_index", "hour_24", "display_hour",
            "hour_12", "meridiem", "hour_label", "busyness_percent", "raw_aria_label",
            "is_target_hour", "target_weekday", "target_hour", "detected_cycle",
            "cycle_hours_count", "cycle_start_hour", "cycle_end_hour", "assigned_weekday",
            "day_offset", "is_today_cycle", "is_target_cycle", "selected_as_target"
        ]
        with open(scraped_data_file, "w", newline='', encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=scraped_fieldnames)
            writer.writeheader()
            for data in all_scraped_data:
                # Fill in missing fields with default values
                for field in scraped_fieldnames:
                    if field not in data:
                        data[field] = None
                writer.writerow(data)
        logger.info(f"📊 All scraped data saved to {scraped_data_file}")
    # Save current hour results with data_type field
    current_hour_file = f"data/current_hour_{current_time.strftime('%Y%m%d_%H')}.csv"
    os.makedirs("data", exist_ok=True)
    
    fieldnames = ["restaurant_url", "weekday", "hour_24", "hour_label", "timestamp", "value", "busyness_percent", "data_type", "venue_type"]
    with open(current_hour_file, "w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    logger.info(f"✅ Current hour data saved to {current_hour_file}")
    return results

def main():
    """Main function for standalone scraping (SYNC version)"""
    results = []
    index_offset = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        for url in RESTAURANT_URLS:
            logger.info(f"🔍 Scraping: {url}")
            try:
                data = scrape_popular_times(page, url, index_offset)
                results.extend(data)
                index_offset += len(data)
            except Exception as e:
                logger.info(f"❌ Error scraping {url}: {e}")
            delay = random.uniform(5, 10)
            logger.info(f"⏳ Waiting {delay:.2f} seconds...\n")
            time.sleep(delay)

        browser.close()

    # Save to CSV
    fieldnames = ["restaurant_url", "weekday", "hour_24", "hour_label", "index", "value", "busyness_percent"]
    with open(OUTPUT_FILE, "w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    logger.info(f"✅ Done! Data saved to `{OUTPUT_FILE}`.")


# --- RUN ---
if __name__ == "__main__":
    main()

