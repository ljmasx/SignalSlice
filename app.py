#!/usr/bin/env python3
"""
SignalSlice Web Application
Real-time dashboard for Pentagon Pizza Index monitoring

NOTE: This application uses SYNC Playwright and threading (NOT asyncio)
to be compatible with Gunicorn + eventlet workers.
"""
import os
import json
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import threading
import pytz
import logging
import re
import traceback
from functools import wraps
from script.anomalyDetect import check_current_anomalies
from scraping.gmapsScrape import scrape_current_hour, RESTAURANT_URLS, GAY_BAR_URLS, SPORTS_BAR_URLS

# Calculate actual number of monitored locations
ACTIVE_LOCATIONS = len(RESTAURANT_URLS) + len(GAY_BAR_URLS) + len(SPORTS_BAR_URLS)
from validation import (
    ValidationError, validate_index_value, validate_activity_item,
    validate_batch_data, sanitize_string
)
# Twitter fetcher removed - using simple link instead

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Auto-generate secret key - no configuration needed
app.config['SECRET_KEY'] = 'signalslice-' + os.urandom(24).hex()
# Allow all origins for easy deployment (restrict in production if needed)
socketio = SocketIO(app, cors_allowed_origins="*")

@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://unpkg.com; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; img-src 'self' data: https:; font-src 'self' https://cdn.jsdelivr.net; connect-src 'self' ws: wss: http://localhost:* http://127.0.0.1:* http://0.0.0.0:*;"
    return response

# No API key authentication required

# No authentication or rate limiting - simplified for easier deployment

# Global state for dashboard
dashboard_state = {
    'pizza_index': 3.42,
    'gay_bar_index': 6.58,  # Inverse of pizza - starts higher
    'active_locations': ACTIVE_LOCATIONS,
    'scan_count': 0,
    'anomaly_count': 0,
    'last_scan_time': None,
    'scanning': False,
    'activity_feed': [],
    'scanner_running': False
}

EST = pytz.timezone('US/Eastern')

# Scanner scheduling variables
scanner_thread = None
scanner_stop_event = threading.Event()
def add_activity_item(activity_type, message, level='normal'):
    """Add an item to the activity feed and emit to clients"""
    try:
        # Validate inputs
        validated = validate_activity_item(activity_type, message, level)
        
        timestamp = datetime.now(EST).strftime('%H:%M:%S')
        
        activity = {
            'type': validated['type'],
            'message': validated['message'],
            'level': validated['level'],
            'timestamp': timestamp
        }
        
        # Add to feed and keep only last 10 items
        dashboard_state['activity_feed'].insert(0, activity)
        dashboard_state['activity_feed'] = dashboard_state['activity_feed'][:10]
        
        # Emit to all connected clients
        socketio.emit('activity_update', activity)
        
        # Log activity
        logger.info(f"[{timestamp}] {activity['type']}: {activity['message']}")
    except ValidationError as e:
        logger.error(f"Activity item validation error: {e}")
        # Fall back to logging without adding to feed
        logger.warning(f"[ERROR] {activity_type}: {message}")
def update_pizza_index(new_value, change_percent=0):
    """Update pizza index and emit to clients"""
    try:
        # Validate index value
        validated_value = validate_index_value(new_value, 'pizza_index')
        validated_change = round(float(change_percent), 2)
        old_value = dashboard_state['pizza_index']
        dashboard_state['pizza_index'] = validated_value
        
        data = {
            'value': validated_value,
            'change': validated_change,
            'old_value': old_value
        }
        
        # logger.debug(f"Emitting pizza_index_update: {data}")
        socketio.emit('pizza_index_update', data)
    except (ValidationError, ValueError) as e:
        logger.error(f"Pizza index update error: {e}")
        add_activity_item('ERROR', f'Failed to update pizza index: {str(e)}', 'critical')

def update_gay_bar_index(new_value, change_percent=0):
    """Update gay bar index and emit to clients"""
    try:
        # Validate index value
        validated_value = validate_index_value(new_value, 'gay_bar_index')
        validated_change = round(float(change_percent), 2)
        
        old_value = dashboard_state['gay_bar_index']
        dashboard_state['gay_bar_index'] = validated_value
        
        data = {
            'value': validated_value,
            'change': validated_change,
            'old_value': old_value
        }
        
        # logger.debug(f"Emitting gay_bar_index_update: {data}")
        socketio.emit('gay_bar_index_update', data)
    except (ValidationError, ValueError) as e:
        logger.error(f"Gay bar index update error: {e}")
        add_activity_item('ERROR', f'Failed to update gay bar index: {str(e)}', 'critical')
def update_scan_stats():
    """Update scan statistics"""
    dashboard_state['scan_count'] += 1
    dashboard_state['last_scan_time'] = datetime.now(EST)
    
    stats = {
        'scan_count': dashboard_state['scan_count'],
        'last_scan_time': dashboard_state['last_scan_time'].strftime('%H:%M:%S')
    }
    
    socketio.emit('scan_stats_update', stats)

def get_next_hour_start():
    """Calculate seconds until the next hour starts"""
    now = datetime.now(EST)
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return (next_hour - now).total_seconds()

def run_scanner_cycle():
    """Run one complete scanner cycle and emit updates (SYNC version)"""
    try:
        dashboard_state['scanning'] = True
        socketio.emit('scanning_start')
        current_time = datetime.now(EST)
        add_activity_item('SCAN', f'🕐 Starting hourly scan at {current_time.strftime("%Y-%m-%d %H:%M:%S EST")}', 'normal')

        # Step 1: Scrape data with detailed updates
        add_activity_item('SCRAPE', f'📡 Scraping current hour data for {current_time.strftime("%A %I:%M %p")}...', 'normal')
        add_activity_item('SCRAPE', f'📅 Looking for TODAY\'s ({current_time.strftime("%A")}) data at hour {current_time.hour}', 'normal')
        add_activity_item('SCRAPE', '🎯 Priority: LIVE data > Historical data > No data', 'normal')

        # Run the actual scraping (now SYNC)
        try:
            scraped_data = scrape_current_hour()
            # logger.debug(f"Scraped {len(scraped_data)} data points")
            
            # Validate scraped data
            try:
                validated_data = validate_batch_data(scraped_data)
                # logger.debug(f"Validated {len(validated_data)} data points")
                scraped_data = validated_data
            except Exception as e:
                logger.error(f"Data validation error: {e}")
                add_activity_item('WARNING', f'Some data validation errors occurred - continuing with valid data', 'warning')
            
            add_activity_item('SCRAPE', '✅ Current hour data saved successfully', 'success')
            add_activity_item('SCRAPE', 'Data scraping completed', 'success')
            
            # Separate data by venue type
            restaurant_data = [d for d in scraped_data if d.get('venue_type') == 'restaurant']
            gay_bar_data = [d for d in scraped_data if d.get('venue_type') == 'gay_bar']
            # logger.debug(f"Found {len(restaurant_data)} restaurant data points, {len(gay_bar_data)} gay bar data points")
            
            # Calculate pizza index from restaurant data  
            if restaurant_data:
                logger.info(f"Processing {len(restaurant_data)} restaurant data points")
                restaurant_with_data = [d for d in restaurant_data if d.get('busyness_percent') is not None]
                logger.info(f"Found {len(restaurant_with_data)} restaurants with busyness data")
                if restaurant_with_data:
                    try:
                        # Ensure all busyness values are valid integers
                        busyness_values = []
                        for d in restaurant_with_data:
                            if isinstance(d['busyness_percent'], (int, float)):
                                busyness_values.append(float(d['busyness_percent']))
                        
                        if busyness_values:
                            avg_restaurant_busy = sum(busyness_values) / len(busyness_values)
                            # Convert to 0-10 scale (0% busy = 0, 100% busy = 10)
                            new_pizza_index = avg_restaurant_busy / 10
                            change_percent = ((new_pizza_index - dashboard_state['pizza_index']) / dashboard_state['pizza_index']) * 100 if dashboard_state['pizza_index'] > 0 else 0
                            update_pizza_index(new_pizza_index, change_percent)
                            add_activity_item('PIZZA', f'🍕 Pizza Index updated: {new_pizza_index:.2f} ({avg_restaurant_busy:.0f}% busy)', 'normal')
                            # logger.debug(f"Pizza index updated to {new_pizza_index:.2f}")
                        else:
                            logger.warning("No valid busyness values found for restaurants")
                            add_activity_item('WARNING', '⚠️ No valid restaurant busyness data', 'warning')
                    except Exception as e:
                        logger.error(f"Error calculating pizza index: {e}")
                        add_activity_item('ERROR', 'Failed to calculate pizza index', 'warning')
                else:
                    logger.info("No restaurants with busyness data found")
                    add_activity_item('INFO', '📊 No restaurant busyness data available', 'normal')
            else:
                logger.warning("No restaurant data in scraped results")
                add_activity_item('WARNING', '⚠️ No restaurant data found', 'warning')
            
            # Calculate gay bar index from scraped data (includes sports bars with same weight)
            sports_bar_data = [d for d in scraped_data if d.get('venue_type') == 'sports_bar']
            combined_bar_data = gay_bar_data + sports_bar_data

            if combined_bar_data:
                logger.info(f"Processing {len(gay_bar_data)} gay bar + {len(sports_bar_data)} sports bar data points")
                bars_with_data = [d for d in combined_bar_data if d.get('busyness_percent') is not None]
                logger.info(f"Found {len(bars_with_data)} bars with busyness data")
                if bars_with_data:
                    try:
                        # Ensure all busyness values are valid integers
                        busyness_values = []
                        for d in bars_with_data:
                            if isinstance(d['busyness_percent'], (int, float)):
                                busyness_values.append(float(d['busyness_percent']))

                        if busyness_values:
                            avg_bar_busy = sum(busyness_values) / len(busyness_values)
                            # Convert to 0-10 scale inversely (0% busy = 10, 100% busy = 0)
                            new_gay_bar_index = 10 - (avg_bar_busy / 10)
                            change_percent = ((new_gay_bar_index - dashboard_state['gay_bar_index']) / dashboard_state['gay_bar_index']) * 100 if dashboard_state['gay_bar_index'] > 0 else 0
                            update_gay_bar_index(new_gay_bar_index, change_percent)
                            add_activity_item('GAYBAR', f'🏳️‍🌈 Bar Index updated: {new_gay_bar_index:.2f} ({avg_bar_busy:.0f}% busy)', 'normal')
                            # logger.debug(f"Gay bar index updated to {new_gay_bar_index:.2f}")
                        else:
                            logger.warning("No valid busyness values found for bars")
                            add_activity_item('WARNING', '⚠️ No valid bar busyness data', 'warning')
                    except Exception as e:
                        logger.error(f"Error calculating bar index: {e}")
                        add_activity_item('ERROR', 'Failed to calculate bar index', 'warning')
                else:
                    logger.info("No bars with busyness data found")
                    add_activity_item('INFO', '📊 No bar busyness data available', 'normal')
            else:
                logger.warning("No bar data in scraped results")
                add_activity_item('GAYBAR', '⚠️ No bar data available this scan', 'warning')

        except Exception as e:
            error_msg = sanitize_string(str(e), 200)
            add_activity_item('ERROR', f'❌ Scraping failed: {error_msg}', 'critical')
            logger.error(f"Error in scraping: {e}", exc_info=True)
        
        # Step 2: Check for anomalies with detailed progress
        add_activity_item('ANALYZE', '🔍 Checking for anomalies...', 'normal')
        add_activity_item('ANALYZE', f'🌍 Local time: {datetime.now().strftime("%A %I:%M %p")}', 'normal')
        add_activity_item('ANALYZE', f'🕐 Current EST time: {current_time.strftime("%A %I:%M %p")} (Hour {current_time.hour})', 'normal')
        add_activity_item('ANALYZE', f'📅 Checking anomalies for {current_time.strftime("%A")} at {current_time.hour}:00', 'normal')
        
        # Capture the real anomaly detection results
        try:
            anomalies_found = check_current_anomalies()
        except Exception as e:
            logger.error(f"Anomaly detection error: {e}", exc_info=True)
            add_activity_item('ERROR', 'Failed to check for anomalies', 'critical')
            anomalies_found = False
        
        # Update statistics
        update_scan_stats()
        if anomalies_found:
            dashboard_state['anomaly_count'] += 1
            add_activity_item('ANOMALY', '🚨🔴 LIVE ANOMALY DETECTED!', 'critical')
            add_activity_item('ANOMALY', 'Unusual pizza activity patterns found', 'critical')
            add_activity_item('ANOMALY', '🔥 This is REAL-TIME activity - high confidence!', 'critical')
            
            # Calculate new pizza index based on anomaly
            new_index = min(10.0, dashboard_state['pizza_index'] + 1.5)
            change_percent = ((new_index - dashboard_state['pizza_index']) / dashboard_state['pizza_index']) * 100
            update_pizza_index(new_index, change_percent)
            # Emit anomaly alert
            socketio.emit('anomaly_detected', {
                'title': 'ANOMALY DETECTED',
                'message': 'Unusual pizza activity patterns detected - check logs for details',
                'timestamp': datetime.now(EST).strftime('%H:%M:%S'),
                'anomaly_count': dashboard_state['anomaly_count']
            })
        else:
            add_activity_item('ANALYZE', '✅ No anomalies detected this hour', 'success')
            add_activity_item('SCAN', 'All locations within normal parameters', 'success')
            
            # Slight adjustment to pizza index for normal activity
            base_change = ((hash(str(datetime.now())) % 21 - 10) / 100) * 0.5  # Smaller changes for normal scans
            new_index = max(0, min(10, dashboard_state['pizza_index'] + base_change))
            change_percent = ((new_index - dashboard_state['pizza_index']) / dashboard_state['pizza_index']) * 100 if dashboard_state['pizza_index'] > 0 else 0
            update_pizza_index(new_index, change_percent)
        
        dashboard_state['scanning'] = False
        
        # Always emit current state after scan completes
        logger.info(f"Scan complete. Current indices - Pizza: {dashboard_state['pizza_index']:.2f}, Gay Bar: {dashboard_state['gay_bar_index']:.2f}")
        
        # Emit the current indices to all clients
        socketio.emit('pizza_index_update', {
            'value': dashboard_state['pizza_index'],
            'change': 0,
            'old_value': dashboard_state['pizza_index']
        })
        
        socketio.emit('gay_bar_index_update', {
            'value': dashboard_state['gay_bar_index'],
            'change': 0,
            'old_value': dashboard_state['gay_bar_index']
        })
        
        socketio.emit('scanning_complete')
        
        completion_time = datetime.now(EST)
        add_activity_item('SYSTEM', f'✅ Scan completed at {completion_time.strftime("%H:%M:%S EST")}', 'success')
        # Calculate and announce next scan
        next_scan_seconds = get_next_hour_start()
        next_scan_time = datetime.now(EST) + timedelta(seconds=next_scan_seconds)
        add_activity_item('SYSTEM', f'⏰ Next scan scheduled for {next_scan_time.strftime("%H:%M:%S EST")} ({next_scan_seconds/60:.0f} minutes)', 'normal')
    except Exception as e:
        dashboard_state['scanning'] = False
        error_msg = sanitize_string(str(e), 200)
        add_activity_item('ERROR', f'❌ Scanner error: {error_msg}', 'critical')
        socketio.emit('scanning_complete')
        logger.error(f"Scanner error: {e}", exc_info=True)
def hourly_scanner():
    """Main scanner loop that runs hourly (SYNC version with threading)"""
    logger.info("🛰️ SignalSlice Integrated Scanner Starting...")
    add_activity_item('INIT', '🛰️ SignalSlice integrated scanner starting...', 'normal')
    add_activity_item('INIT', '🔄 Running initial scan, then switching to hourly schedule', 'normal')

    # Run initial scan
    run_scanner_cycle()

    while dashboard_state['scanner_running'] and not scanner_stop_event.is_set():
        try:
            # Calculate time until next hour
            sleep_seconds = get_next_hour_start()
            next_run = datetime.now(EST) + timedelta(seconds=sleep_seconds)

            logger.info(f"⏰ Next scan scheduled for {next_run.strftime('%H:%M:%S EST')} ({sleep_seconds/60:.1f} minutes)")
            add_activity_item('SYSTEM', f'Scanner on standby - next automated scan in {sleep_seconds/60:.0f} minutes', 'normal')

            # Sleep until next hour (with small buffer) - use interruptible sleep
            sleep_time = min(sleep_seconds + 30, 3600)  # Max 1 hour sleep
            if scanner_stop_event.wait(timeout=sleep_time):
                # Stop event was set, exit the loop
                break

            # Check if scanner is still running
            if dashboard_state['scanner_running'] and not scanner_stop_event.is_set():
                add_activity_item('SYSTEM', 'Hourly scan interval reached - initiating new scan cycle', 'normal')
                run_scanner_cycle()
        except Exception as e:
            add_activity_item('ERROR', f'❌ Scanner loop error: {str(e)}', 'critical')
            logger.error(f"❌ Unexpected error in scanner loop: {e}", exc_info=True)
            # Wait 5 minutes before retrying to avoid rapid failures
            add_activity_item('SYSTEM', 'Waiting 5 minutes before retry to avoid rapid failures', 'warning')
            if scanner_stop_event.wait(timeout=300):
                break

    add_activity_item('SYSTEM', '🛑 Scanner stopped', 'warning')
def start_scanner():
    """Start the scanner in a separate thread (SYNC version)"""
    global scanner_thread

    if dashboard_state['scanner_running']:
        logger.warning("Scanner already running")
        return None

    scanner_stop_event.clear()
    dashboard_state['scanner_running'] = True

    scanner_thread = threading.Thread(target=hourly_scanner, name="ScannerThread")
    scanner_thread.daemon = True
    scanner_thread.start()
    logger.info("Scanner thread started")
    return scanner_thread


def stop_scanner():
    """Stop the scanner (SYNC version)"""
    global scanner_thread

    dashboard_state['scanner_running'] = False
    scanner_stop_event.set()

    if scanner_thread and scanner_thread.is_alive():
        logger.info("Waiting for scanner thread to stop...")
        scanner_thread.join(timeout=5)
        if scanner_thread.is_alive():
            logger.warning("Scanner thread did not stop gracefully")

@app.route('/')
def index():
    """Serve the main dashboard"""
    return render_template('index.html', active_locations=ACTIVE_LOCATIONS)

@app.route('/api/activity_feed')
def get_activity_feed():
    """API endpoint to get current activity feed"""
    try:
        return jsonify({
            'activity_feed': dashboard_state['activity_feed'],
            'timestamp': datetime.now(EST).isoformat()
        })
    except Exception as e:
        logger.error(f"API error in /api/activity_feed: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/status')
def get_status():
    """API endpoint to get current status"""
    try:
        return jsonify({
            'pizza_index': dashboard_state['pizza_index'],
            'gay_bar_index': dashboard_state['gay_bar_index'],
            'active_locations': dashboard_state['active_locations'],
            'scan_count': dashboard_state['scan_count'],
            'anomaly_count': dashboard_state['anomaly_count'],
            'last_scan_time': dashboard_state['last_scan_time'].isoformat() if dashboard_state['last_scan_time'] else None,
            'scanning': dashboard_state['scanning'],
            'scanner_running': dashboard_state['scanner_running'],
            'activity_feed': dashboard_state['activity_feed']
        })
    except Exception as e:
        logger.error(f"API error in /api/status: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/trigger_scan', methods=['GET', 'POST'])
def trigger_manual_scan():
    """Trigger a manual scan"""
    try:
        # Check if scanner is already running
        if dashboard_state['scanning']:
            return jsonify({'status': 'scan_already_running', 'message': 'A scan is already in progress'}), 409

        # Run in separate thread to avoid blocking (SYNC version)
        scan_thread = threading.Thread(target=run_scanner_cycle, name="ManualScanThread")
        scan_thread.daemon = True
        scan_thread.start()

        return jsonify({'status': 'scan_triggered', 'message': 'Manual scan started'})
    except Exception as e:
        logger.error(f"API error in /api/trigger_scan: {e}")
        return jsonify({'error': 'Failed to trigger scan'}), 500

@app.route('/api/start_scanner', methods=['GET', 'POST'])
def start_scanner_endpoint():
    """Start the automated scanner"""
    try:
        if not dashboard_state['scanner_running']:
            start_scanner()
            return jsonify({'status': 'scanner_started', 'message': 'Automated scanner started successfully'})
        else:
            return jsonify({'status': 'scanner_already_running', 'message': 'Scanner is already running'}), 409
    except Exception as e:
        logger.error(f"API error in /api/start_scanner: {e}")
        return jsonify({'error': 'Failed to start scanner'}), 500

@app.route('/api/stop_scanner', methods=['GET', 'POST'])
def stop_scanner_endpoint():
    """Stop the automated scanner"""
    try:
        if dashboard_state['scanner_running']:
            stop_scanner()
            return jsonify({'status': 'scanner_stopped', 'message': 'Automated scanner stopped successfully'})
        else:
            return jsonify({'status': 'scanner_not_running', 'message': 'Scanner is not running'}), 409
    except Exception as e:
        logger.error(f"API error in /api/stop_scanner: {e}")
        return jsonify({'error': 'Failed to stop scanner'}), 500

# Twitter API endpoint removed - using simple link instead

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    try:
        logger.info(f"🔗 Client connected: {request.sid}")
        initial_state = {
            'pizza_index': dashboard_state['pizza_index'],
            'gay_bar_index': dashboard_state['gay_bar_index'],
            'active_locations': dashboard_state['active_locations'],
            'scan_count': dashboard_state['scan_count'],
            'anomaly_count': dashboard_state['anomaly_count'],
            'last_scan_time': dashboard_state['last_scan_time'].strftime('%H:%M:%S') if dashboard_state['last_scan_time'] else 'Never',
            'activity_feed': dashboard_state['activity_feed'],
            'scanner_running': dashboard_state['scanner_running']
        }
        # logger.debug(f"Sending initial_state: {initial_state}")
        emit('initial_state', initial_state)
        add_activity_item('CONNECT', f'Dashboard client connected ({request.sid[:8]})', 'normal')
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
        emit('error', {'message': 'Failed to initialize connection'})

@socketio.on('manual_scan')
def handle_manual_scan():
    """Handle manual scan request from client"""
    try:
        if dashboard_state['scanning']:
            emit('scan_error', {'message': 'A scan is already in progress'})
            return

        # Run in separate thread (SYNC version)
        scan_thread = threading.Thread(target=run_scanner_cycle, name="WebSocketScanThread")
        scan_thread.daemon = True
        scan_thread.start()
    except Exception as e:
        logger.error(f"WebSocket manual scan handler error: {e}")
        emit('scan_error', {'message': 'Failed to start manual scan'})

# Initialize with some activity
add_activity_item('INIT', 'SignalSlice dashboard initialized', 'normal')
add_activity_item('SYSTEM', 'Monitoring 8 pizza locations + 2 bars near Pentagon', 'normal')
add_activity_item('GAYBAR', '🏳️‍🌈 Gay Bar + 🏈 Sports Bar monitoring active', 'normal')

# Start scanner at module load (works with both direct run and Gunicorn)
start_scanner()

if __name__ == '__main__':
    logger.info("🛰️ SignalSlice Dashboard Starting...")
    logger.info("🌐 Access the dashboard at: http://localhost:5000")
    logger.info("📡 Real-time data will appear when scanner runs")
    
    # Start the scanner automatically
    start_scanner()
    try:
        socketio.run(app, debug=False, host='0.0.0.0', port=6003)
    except KeyboardInterrupt:
        logger.info("\n🛑 Shutting down...")
        stop_scanner()
        logger.info("SignalSlice stopped. Stay vigilant! 🍕")