import json
import csv
import os
import logging
import sys
from datetime import datetime, timedelta
import pytz
import traceback
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from validation import validate_busyness_percent, ValidationError

# Configure logging
logger = logging.getLogger(__name__)

# =============================================================================
# ANOMALY DETECTION THRESHOLDS
# =============================================================================

# Option A: Absolute threshold - alert if ANY restaurant hits this %
ABSOLUTE_THRESHOLD = 90  # Alert at 90%+ busyness

# Option B: Divergence detection - alert when pizza is busy AND bars are empty
PIZZA_HIGH_THRESHOLD = 70   # Pizza places considered "busy" at 70%+
BAR_LOW_THRESHOLD = 30      # Bars considered "empty" at 30% or less

# Legacy baseline comparison (kept but disabled with high threshold)
BASELINE_THRESHOLD = 150  # Effectively disabled


def setup_logging():
    """Setup logging with UTF-8 encoding"""
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8')
    except:
        pass

setup_logging()


def check_current_anomalies():
    """
    Check for anomalies using multiple detection methods:
    1. ABSOLUTE: Any restaurant at 90%+ triggers alert
    2. DIVERGENCE: Pizza busy (70%+) AND bars empty (30%-) triggers alert
    """
    est = pytz.timezone('US/Eastern')
    current_time_est = datetime.now(est)
    current_weekday = current_time_est.strftime('%A')
    current_hour = str(current_time_est.hour)

    logger.info(f"🌍 Local time: {datetime.now().strftime('%A %I:%M %p')}")
    logger.info(f"🕐 Current EST time: {current_time_est.strftime('%A %I:%M %p')} (Hour {current_hour})")
    logger.info(f"\n📊 ANOMALY DETECTION THRESHOLDS:")
    logger.info(f"   🍕 Absolute alert: {ABSOLUTE_THRESHOLD}%+")
    logger.info(f"   🍕↑🍸↓ Divergence: Pizza ≥{PIZZA_HIGH_THRESHOLD}% AND Bar ≤{BAR_LOW_THRESHOLD}%\n")

    # Find the most recent current hour data file
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    current_hour_pattern = f"current_hour_{current_time_est.strftime('%Y%m%d_%H')}.csv"
    current_hour_file = os.path.join(data_dir, current_hour_pattern)

    if not os.path.exists(current_hour_file):
        logger.info(f"⚠️ No current hour data file found: {current_hour_file}")
        return False

    # Collect data by venue type
    pizza_data = []  # restaurants
    bar_data = []    # gay_bar + sports_bar

    anomalies_found = False
    absolute_anomalies = []

    with open(current_hour_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            venue_type = row.get('venue_type', 'restaurant')

            # Skip rows with no busyness data
            if not row.get("busyness_percent") or row["busyness_percent"] == "None":
                logger.info(f"ℹ️ No data for {row['restaurant_url']}")
                continue

            try:
                busyness = validate_busyness_percent(row["busyness_percent"])
                if busyness is None:
                    continue
            except ValidationError as e:
                logger.error(f"Invalid busyness data: {e}")
                continue

            data_type = row.get('data_type', 'HISTORICAL')

            # Categorize by venue type
            if venue_type == 'restaurant':
                pizza_data.append({
                    'url': row['restaurant_url'],
                    'busyness': busyness,
                    'data_type': data_type
                })

                # CHECK 1: Absolute threshold (90%+)
                if busyness >= ABSOLUTE_THRESHOLD:
                    absolute_anomalies.append({
                        'url': row['restaurant_url'],
                        'busyness': busyness,
                        'data_type': data_type
                    })
            else:
                # gay_bar or sports_bar
                bar_data.append({
                    'url': row['restaurant_url'],
                    'busyness': busyness,
                    'data_type': data_type,
                    'venue_type': venue_type
                })

    # Calculate averages
    pizza_avg = sum(d['busyness'] for d in pizza_data) / len(pizza_data) if pizza_data else 0
    bar_avg = sum(d['busyness'] for d in bar_data) / len(bar_data) if bar_data else None

    logger.info(f"\n📊 SCAN SUMMARY:")
    logger.info(f"   🍕 Pizza places: {len(pizza_data)} venues, avg {pizza_avg:.1f}%")
    if bar_data:
        logger.info(f"   🍸 Bars: {len(bar_data)} venues, avg {bar_avg:.1f}%")
    else:
        logger.info(f"   🍸 Bars: No data available")

    # ==========================================================================
    # ANOMALY CHECK 1: Absolute threshold (90%+)
    # ==========================================================================
    if absolute_anomalies:
        anomalies_found = True
        logger.info(f"\n🚨🍕 ABSOLUTE THRESHOLD ALERT 🚨")
        logger.info(f"   {len(absolute_anomalies)} restaurant(s) at {ABSOLUTE_THRESHOLD}%+ busyness!")
        for a in absolute_anomalies:
            logger.info(f"   🔥 {a['url']}: {a['busyness']}% [{a['data_type']}]")
        logger.info(f"   🕐 Detected at: {current_time_est.strftime('%Y-%m-%d %H:%M:%S EST')}")

    # ==========================================================================
    # ANOMALY CHECK 2: Divergence (Pizza busy + Bars empty)
    # ==========================================================================
    if bar_avg is not None:
        is_pizza_high = pizza_avg >= PIZZA_HIGH_THRESHOLD
        is_bar_low = bar_avg <= BAR_LOW_THRESHOLD

        if is_pizza_high and is_bar_low:
            anomalies_found = True
            logger.info(f"\n🚨🍕↑🍸↓ DIVERGENCE ALERT 🚨")
            logger.info(f"   Pizza places are BUSY ({pizza_avg:.1f}%) while bars are EMPTY ({bar_avg:.1f}%)!")
            logger.info(f"   ⚠️ This pattern suggests unusual late-night activity (crisis mode?)")
            logger.info(f"   🕐 Detected at: {current_time_est.strftime('%Y-%m-%d %H:%M:%S EST')}")
        else:
            # Log why divergence wasn't triggered
            if is_pizza_high:
                logger.info(f"\n   🍕 Pizza HIGH ({pizza_avg:.1f}% ≥ {PIZZA_HIGH_THRESHOLD}%) but bars not empty ({bar_avg:.1f}%)")
            elif is_bar_low:
                logger.info(f"\n   🍸 Bars LOW ({bar_avg:.1f}% ≤ {BAR_LOW_THRESHOLD}%) but pizza not busy ({pizza_avg:.1f}%)")
            else:
                logger.info(f"\n   ✅ Normal pattern: Pizza {pizza_avg:.1f}%, Bars {bar_avg:.1f}%")
    else:
        logger.info(f"\n   ⚠️ Cannot check divergence: no bar data available")

    # ==========================================================================
    # FINAL STATUS
    # ==========================================================================
    if anomalies_found:
        logger.info(f"\n🚨 ANOMALY DETECTED - Alert triggered!")
    else:
        logger.info(f"\n✅ No anomalies detected - All systems normal")

    return anomalies_found


if __name__ == "__main__":
    try:
        anomalies_found = check_current_anomalies()
        sys.exit(0 if anomalies_found else 1)
    except Exception as e:
        logger.error(f"Anomaly detection failed: {e}")
        logger.error(traceback.format_exc())
        sys.exit(2)
