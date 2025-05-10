"""Reticulum Status Page Server.

This script creates a web server that displays Reticulum network status
information using rnstatus command output.
"""
import json
import logging
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, Response, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('status_page.log')
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)

Talisman(app,
    content_security_policy={
        'default-src': "'self'",
        'script-src': "'self' 'unsafe-inline' https://unpkg.com",
        'style-src': "'self' 'unsafe-inline'",
        'img-src': "'self' data:",
        'font-src': "'self'",
        'connect-src': "'self'",
    },
    force_https=False,
    strict_transport_security=True,
    session_cookie_secure=True,
    session_cookie_http_only=True,
    session_cookie_samesite='Lax'
)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

IGNORE_SECTIONS = ["Shared Instance", "AutoInterface"]

CACHE_DURATION_SECONDS = 30
RETRY_INTERVAL_SECONDS = 30
SSE_UPDATE_INTERVAL_SECONDS = 5

UPTIME_FILE_PATH = 'uptime.json'

def load_uptime_tracker(filepath):
    """Load uptime tracker data from a JSON file.

    Args:
        filepath (str): The path to the JSON file.

    Returns:
        dict: The loaded uptime tracker data, or an empty dictionary if loading fails.

    """
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    logger.info(f"Successfully loaded uptime tracker from {filepath}")
                    return data
                else:
                    logger.warning(f"Corrupted uptime tracker file (not a dict): {filepath}. Starting fresh.")
                    return {}
        except json.JSONDecodeError:
            logger.warning(f"Error decoding JSON from uptime tracker file: {filepath}. Starting fresh.")
            return {}
        except Exception as e:
            logger.error(f"Unexpected error loading uptime tracker from {filepath}: {e}. Starting fresh.")
            return {}
    return {}

def save_uptime_tracker(filepath, data):
    """Save uptime tracker data to a JSON file atomically.

    Args:
        filepath (str): The path to the JSON file.
        data (dict): The uptime tracker data to save.

    """
    temp_filepath = None
    try:
        fd, temp_filepath = tempfile.mkstemp(dir=os.path.dirname(filepath) or '.', prefix=os.path.basename(filepath) + '.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        shutil.move(temp_filepath, filepath)
        logger.debug(f"Successfully saved uptime tracker to {filepath}")
    except Exception as e:
        logger.error(f"Error saving uptime tracker to {filepath}: {e}")
        if temp_filepath and os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
            except Exception as re:
                logger.error(f"Error removing temporary uptime file {temp_filepath}: {re}")

_cache = {
    'data': None,
    'timestamp': 0,
    'lock': threading.Lock(),
    'interface_uptime_tracker': load_uptime_tracker(UPTIME_FILE_PATH)
}

_rnsd_process = None
_rnsd_thread = None

def run_rnsd():
    """Run rnsd daemon in a separate thread.

    Returns:
        bool: True if rnsd started successfully, False otherwise.

    """
    global _rnsd_process

    try:
        rnsd_path = shutil.which('rnsd')
        if not rnsd_path:
            logger.error("rnsd command not found in PATH")
            return False

        logger.info("Starting rnsd daemon...")
        _rnsd_process = subprocess.Popen([rnsd_path],  # nosec B603
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       text=True)

        time.sleep(2)

        if _rnsd_process.poll() is not None:
            stderr = _rnsd_process.stderr.read()
            logger.error(f"rnsd failed to start: {stderr}")
            return False

        logger.info("rnsd daemon started successfully")
        return True

    except Exception as e:
        logger.error(f"Error starting rnsd: {e}")
        return False

def stop_rnsd():
    """Stop the rnsd daemon if it's running."""
    global _rnsd_process

    if _rnsd_process and _rnsd_process.poll() is None:
        logger.info("Stopping rnsd daemon...")
        _rnsd_process.terminate()
        try:
            _rnsd_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _rnsd_process.kill()
        logger.info("rnsd daemon stopped")

def check_rnstatus_installation():
    """Check if rnstatus is properly installed and accessible.
    
    Returns:
        tuple: (bool, str) - (is_installed, error_message)

    """
    rnstatus_path = shutil.which('rnstatus')
    if not rnstatus_path:
        return False, "rnstatus command not found in PATH"

    try:
        result = subprocess.run([rnstatus_path, '--help'],  # nosec B603
                              capture_output=True,
                              text=True,
                              timeout=5)
        if result.returncode != 0:
            return False, f"rnstatus command failed with return code {result.returncode}"
        return True, "rnstatus is installed and accessible"
    except subprocess.TimeoutExpired:
        return False, "rnstatus --help command timed out"
    except Exception as e:
        return False, f"Error checking rnstatus: {str(e)}"

def get_rnstatus_from_command():
    """Execute rnstatus command and return the output.

    Returns:
        str: The output of the rnstatus command, or an error message.

    """
    try:
        is_installed, error_msg = check_rnstatus_installation()
        if not is_installed:
            logger.error(f"rnstatus installation check failed: {error_msg}")
            return f"Error: {error_msg}"

        rnstatus_path = shutil.which('rnstatus')
        if not rnstatus_path:
            return "Error: rnstatus command not found in PATH"

        result = subprocess.run([rnstatus_path, '-A'],  # nosec B603
                              capture_output=True,
                              text=True,
                              timeout=30,
                              env=dict(os.environ, PYTHONUNBUFFERED="1"))

        if result.returncode != 0:
            error_detail = f"rnstatus command failed with return code {result.returncode}"
            if result.stderr:
                error_detail += f"\nError output: {result.stderr.strip()}"
            logger.error(error_detail)
            return f"Error: {error_detail}"

        if not result.stdout.strip():
            logger.warning("rnstatus command returned empty output")
            return "Warning: rnstatus returned empty output"

        return result.stdout
    except subprocess.TimeoutExpired:
        error_msg = "rnstatus command timed out after 30 seconds"
        logger.error(error_msg)
        return f"Error: {error_msg}"
    except FileNotFoundError:
        error_msg = "rnstatus command not found. Please ensure it is installed and in PATH."
        logger.error(error_msg)
        return f"Error: {error_msg}"
    except Exception as e:
        error_msg = f"Unexpected error executing rnstatus: {str(e)}"
        logger.exception(error_msg)
        return f"Error: {error_msg}"

def get_and_cache_rnstatus_data():
    """Fetch rnstatus data, parse it, update uptime info, and update the cache.

    Returns:
        tuple: A tuple containing the parsed data and the current time.

    """
    raw_output = get_rnstatus_from_command()
    parsed_data, updated_tracker = parse_rnstatus(raw_output, _cache['interface_uptime_tracker'])
    current_time = time.time()

    with _cache['lock']:
        if not ("error" in parsed_data or "warning" in parsed_data):
            for info in parsed_data.values():
                if isinstance(info, dict) and info.get('status') == 'Up':
                    tracker_key = info.get('name')
                    if tracker_key and tracker_key in _cache['interface_uptime_tracker']:
                        prev_tracker = _cache['interface_uptime_tracker'][tracker_key]
                        if prev_tracker.get('current_status') == 'Up' and prev_tracker.get('first_up_timestamp'):
                            info['first_up_timestamp'] = prev_tracker['first_up_timestamp']
                            if isinstance(updated_tracker.get(tracker_key), dict):
                                updated_tracker[tracker_key]['first_up_timestamp'] = prev_tracker['first_up_timestamp']

        _cache['data'] = parsed_data
        _cache['timestamp'] = current_time
        _cache['interface_uptime_tracker'] = updated_tracker
        save_uptime_tracker(UPTIME_FILE_PATH, _cache['interface_uptime_tracker'])

    return parsed_data, current_time

def get_status_data_with_caching():
    """Get status data, utilizing the cache if available and fresh.

    Returns:
        dict: A dictionary containing the timestamp, data, and debug information.

    """
    start_process_time = time.time()
    with _cache['lock']:
        cached_data = _cache['data']
        cache_timestamp = _cache['timestamp']

    if cached_data and (time.time() - cache_timestamp < CACHE_DURATION_SECONDS):
        data_to_serve = cached_data
        data_timestamp = cache_timestamp
    else:
        fetched_data, fetched_timestamp = get_and_cache_rnstatus_data()
        data_to_serve = fetched_data
        data_timestamp = fetched_timestamp

    processing_time_ms = (time.time() - start_process_time) * 1000

    return {
        'timestamp': datetime.fromtimestamp(data_timestamp).isoformat(),
        'data': data_to_serve,
        'debug': {
            'processing_time_ms': processing_time_ms,
            'cache_hit': bool(cached_data and (time.time() - cache_timestamp < CACHE_DURATION_SECONDS))
        }
    }

def parse_rnstatus(output, current_uptime_tracker):
    """Parse the rnstatus output into a structured format.

    Args:
        output (str): The raw output from the rnstatus command.
        current_uptime_tracker (dict): The current uptime tracker data.

    Returns:
        tuple: A tuple containing the parsed data and the updated uptime tracker.

    """
    sections = {}
    current_section_title = None
    is_current_section_ignored = False
    updated_tracker = current_uptime_tracker.copy()
    current_time_for_uptime = time.time()

    if output.startswith("Error:") or output.startswith("Warning:"):
        return {"error": output}, updated_tracker

    lines = output.split('\n')
    idx = 0
    while idx < len(lines):
        line_content = lines[idx]
        line = line_content.strip()
        idx += 1

        if not line:
            continue

        if '[' in line and ']' in line:
            section_name_part = line.split('[')[0].strip()
            interface_name_part = line.split('[')[1].split(']')[0]
            current_section_key_for_dict = f"{section_name_part} [{interface_name_part}]"
            current_section_title = current_section_key_for_dict

            if section_name_part in IGNORE_SECTIONS:
                is_current_section_ignored = True
            else:
                is_current_section_ignored = False
                tracker_key = interface_name_part
                previous_record = updated_tracker.get(tracker_key)

                if previous_record and previous_record.get('current_status') == 'Up':
                    first_up_ts = previous_record.get('first_up_timestamp')
                else:
                    first_up_ts = None

                sections[current_section_key_for_dict] = {
                    'name': interface_name_part,
                    'section_type': section_name_part,
                    'status': 'Down',
                    'details': {},
                    'first_up_timestamp': first_up_ts
                }

                if not previous_record:
                    updated_tracker[tracker_key] = {
                        'first_up_timestamp': first_up_ts,
                        'current_status': 'Down',
                        'last_seen_up': None
                    }
                else:
                    updated_tracker[tracker_key]['current_status'] = previous_record.get('current_status', 'Down')
                    updated_tracker[tracker_key]['last_seen_up'] = previous_record.get('last_seen_up')

        elif current_section_title and not is_current_section_ignored and ':' in line:
            key_part, value_part = line.split(':', 1)
            key = key_part.strip()
            value = value_part.strip()

            if key == "Traffic":
                if idx < len(lines):
                    next_line_content = lines[idx]
                    if next_line_content and next_line_content[0].isspace() and "↓" in next_line_content:
                        value += f"\n{next_line_content.strip()}"
                        idx += 1
            elif key == "Announces":
                if idx < len(lines):
                    next_line_content = lines[idx] 
                    if next_line_content and next_line_content[0].isspace() and "↓" in next_line_content:
                        value += f"\n{next_line_content.strip()}"
                        idx += 1
            
            if key == "Status":
                new_status = 'Up' if 'Up' in value else 'Down'
                target_section = sections[current_section_title]
                tracker_key = target_section['name']

                if tracker_key not in updated_tracker:
                    updated_tracker[tracker_key] = {
                        'first_up_timestamp': None,
                        'current_status': 'Down',
                        'last_seen_up': None
                    }

                previous_status_in_tracker = updated_tracker[tracker_key].get('current_status', 'Down')
                persisted_first_up_ts = updated_tracker[tracker_key].get('first_up_timestamp')
                persisted_last_seen_up = updated_tracker[tracker_key].get('last_seen_up')

                target_section['status'] = new_status

                if new_status == 'Up':
                    if previous_status_in_tracker == 'Up':
                        if persisted_first_up_ts:
                            target_section['first_up_timestamp'] = persisted_first_up_ts
                        elif persisted_last_seen_up:
                            target_section['first_up_timestamp'] = persisted_last_seen_up
                            updated_tracker[tracker_key]['first_up_timestamp'] = persisted_last_seen_up
                        else:
                            target_section['first_up_timestamp'] = current_time_for_uptime
                            updated_tracker[tracker_key]['first_up_timestamp'] = current_time_for_uptime
                    else:
                        target_section['first_up_timestamp'] = current_time_for_uptime
                        updated_tracker[tracker_key]['first_up_timestamp'] = current_time_for_uptime

                    updated_tracker[tracker_key]['last_seen_up'] = current_time_for_uptime
                else:
                    target_section['first_up_timestamp'] = None
                    updated_tracker[tracker_key]['first_up_timestamp'] = None

                updated_tracker[tracker_key]['current_status'] = new_status

            sections[current_section_title]['details'][key] = value

    return sections, updated_tracker

def create_status_card(section, info):
    """Create HTML for a status card.

    Args:
        section (str): The section name.
        info (dict): The interface information.

    Returns:
        str: The HTML for the status card.

    """
    status_class = 'status-up' if info['status'] == 'Up' else 'status-down'

    card_title = info['name']
    address_value = None

    if '/' in info['name']:
        parts = info['name'].split('/', 1)
        card_title = parts[0]
        if len(parts) > 1:
            address_value = parts[1]

    uptime_html = ''
    if info.get('first_up_timestamp'):
        now = time.time()
        duration_seconds = now - info['first_up_timestamp']
        start_time = datetime.fromtimestamp(info['first_up_timestamp'])
        uptime_html = f"""
            <div class="detail-row uptime-info">
                <span class="detail-label">Uptime</span>
                <span class="detail-value">{format_duration(duration_seconds)} (since {start_time.strftime('%Y-%m-%d %H:%M:%S')})</span>
            </div>
        """
    elif info['status'] == 'Up':
        uptime_html = """
            <div class="detail-row uptime-info">
                <span class="detail-label">Uptime</span>
                <span class="detail-value">Unknown (interface is up)</span>
            </div>
        """

    details_html_parts = []
    if address_value:
        details_html_parts.append(
            f'<div class="detail-row"><span class="detail-label">Address</span><span class="detail-value" title="{address_value}">{address_value}</span></div>'
        )

    if info.get('details'):
        for key, value in info['details'].items():
            if (key == "Announces" or key == "Traffic") and "\n" in value:
                parts = value.split("\n")
                if len(parts) >= 1:
                    details_html_parts.append(
                        f'<div class="detail-row"><span class="detail-label">{key}</span><span class="detail-value">{parts[0]}</span></div>'
                    )
                if len(parts) >= 2:
                    details_html_parts.append(
                        f'<div class="detail-row"><span class="detail-label">&nbsp;</span><span class="detail-value">{parts[1]}</span></div>'
                    )
            else:
                details_html_parts.append(
                    f'<div class="detail-row"><span class="detail-label">{key}</span><span class="detail-value">{value}</span></div>'
                )
    details_html = ''.join(details_html_parts)

    buttons_html = ''
    if info['section_type'] == 'TCPInterface':
        export_url = f"/api/export/{info['name'].replace('/', '_')}"
        suggested_filename_base = info['name'].split('/')[0]
        buttons_html = f"""
            <a href="{export_url}"
               class="card-export-button export-button"
               title="Export interface configuration"
               download="{suggested_filename_base}.txt">
                <svg viewBox="0 0 24 24" width="16" height="16">
                    <path fill="currentColor" d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>
                </svg>
            </a>
        """

    return f"""
        <div class="status-card" data-section-name="{info['section_type'].lower()}" data-interface-name="{info['name'].lower()}">
            {buttons_html}
            <div class="card-content">
                <h2 title="{info['name']}">
                    <span class="status-indicator {status_class}"></span>
                    {card_title}
                </h2>
                {uptime_html}
                {details_html}
            </div>
        </div>
    """

def format_duration(seconds):
    """Format duration in seconds to human readable string.

    Args:
        seconds (int): Duration in seconds.

    Returns:
        str: Human-readable duration string.

    """
    if seconds <= 0:
        return 'N/A'

    days = int(seconds // (3600 * 24))
    hours = int((seconds % (3600 * 24)) // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")

    return ' '.join(parts)

@app.route('/')
@limiter.exempt
def index():
    """Render the main status page."""
    return render_template('index.html')

@app.route('/api/status')
@limiter.limit("10 per minute")
def status():
    """Return the current status as HTML or JSON via an API endpoint."""
    data_payload = get_status_data_with_caching()

    if request.accept_mimetypes.accept_json and \
       not request.accept_mimetypes.accept_html:
        return jsonify(data_payload)

    if 'error' in data_payload['data'] or 'warning' in data_payload['data']:
        error_or_warning_key = 'error' if 'error' in data_payload['data'] else 'warning'
        message = data_payload['data'][error_or_warning_key]
        return f'<div class="status-card error-card"><div class="error-message">{message}</div></div>'

    cards_html = ''
    for section, info in data_payload['data'].items():
        if section not in IGNORE_SECTIONS:
            cards_html += create_status_card(section, info)

    return cards_html if cards_html else '<div class="status-card error-card"><div class="error-message">No interfaces found or rnstatus output was empty.</div></div>'

@app.route('/api/search')
@limiter.limit("10 per minute")
def search():
    """Search interfaces and return matching cards as HTML or JSON."""
    query = request.args.get('q', '').lower()
    data_payload = get_status_data_with_caching()

    if 'error' in data_payload['data'] or 'warning' in data_payload['data']:
        if request.accept_mimetypes.accept_json and \
           not request.accept_mimetypes.accept_html:
            return jsonify(data_payload)
        else:
            error_or_warning_key = 'error' if 'error' in data_payload['data'] else 'warning'
            message = data_payload['data'][error_or_warning_key]
            return f'<div class="status-card error-card"><div class="error-message">{message}</div></div>'

    filtered_data = {}
    for section, info in data_payload['data'].items():
        if section in IGNORE_SECTIONS:
            continue
        if not isinstance(info, dict):
            continue

        if (query in section.lower() or
            query in info.get('name', '').lower() or
            any(query in str(v).lower() for v in info.get('details', {}).values())):
            filtered_data[section] = info

    if request.accept_mimetypes.accept_json and \
       not request.accept_mimetypes.accept_html:
        return jsonify({
            'timestamp': data_payload['timestamp'],
            'data': filtered_data,
            'debug': data_payload['debug'],
            'query': query
        })
    else:
        cards_html = ''
        if filtered_data:
            for section, info in filtered_data.items():
                cards_html += create_status_card(section, info)
        else:
            cards_html = '<div class="status-card error-card"><div class="error-message">No matching interfaces found for your query.</div></div>'
        return cards_html

@app.route('/api/export/<interface_name>')
@limiter.limit("10 per minute")
def export_interface(interface_name):
    """Export interface configuration."""
    data = get_status_data_with_caching()
    if data.get('error'):
        return f'<div class="status-card error-card"><div class="error-message">{data["error"]}</div></div>'

    interface_name = interface_name.replace('_', '/')

    for info in data['data'].values():
        if info['name'] == interface_name:
            name = info['name'].split('/')[0]
            address = info['name'].split('/')[1] if '/' in info['name'] else ''
            host, port = address.split(':') if ':' in address else ('', '')

            config = f"""[[{name}]]
    type = TCPClientInterface
    interface_enabled = true
    target_host = {host}
    target_port = {port}
"""
            response = Response(config, mimetype='text/plain')
            response.headers['Content-Disposition'] = f'attachment; filename="{name}.txt"'
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response

    return '<div class="status-card error-card"><div class="error-message">Interface not found</div></div>'

@app.route('/api/export-all')
@limiter.limit("10 per minute")
def export_all():
    """Export all interface configurations."""
    data = get_status_data_with_caching()
    if data.get('error'):
        return f'<div class="status-card error-card"><div class="error-message">{data["error"]}</div></div>'

    config = ''
    for info in data['data'].values():
        if info['section_type'] == 'TCPInterface':
            name = info['name'].split('/')[0]
            address = info['name'].split('/')[1] if '/' in info['name'] else ''
            host, port = address.split(':') if ':' in address else ('', '')

            config += f"""[[{name}]]
    type = TCPClientInterface
    interface_enabled = true
    target_host = {host}
    target_port = {port}

"""

    response = Response(config, mimetype='text/plain')
    response.headers['Content-Disposition'] = 'attachment; filename="all_interfaces.txt"'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/events')
@limiter.exempt
def stream_status_events():
    """Streams status updates using Server-Sent Events (SSE)."""
    def event_stream_generator():
        """Generates a stream of server-sent events."""
        last_sent_timestamp = 0
        try:
            while True:
                current_data_payload = get_status_data_with_caching()
                data_timestamp_iso = current_data_payload['timestamp']
                data_timestamp_float = datetime.fromisoformat(data_timestamp_iso).timestamp()

                if data_timestamp_float > last_sent_timestamp:
                    json_data = json.dumps(current_data_payload)
                    yield f"data: {json_data}\n\n"
                    last_sent_timestamp = data_timestamp_float

                time.sleep(SSE_UPDATE_INTERVAL_SECONDS)
        except GeneratorExit:
            pass
        except Exception as e:
            logger.error(f"Error in SSE stream: {e}", exc_info=True)
            error_payload = json.dumps({'error': 'Stream error occurred', 'type': 'SERVER_ERROR'})
            yield f"event: error\ndata: {error_payload}\n\n"

    return Response(event_stream_generator(), mimetype='text/event-stream')

@app.route('/api/debug')
@limiter.limit("5 per minute")
def debug_info():
    """Debug endpoint that returns system information."""
    rnstatus_installed, rnstatus_msg = check_rnstatus_installation()

    return jsonify({
        'timestamp': datetime.now().isoformat(),
        'environment': {
            'python_version': os.sys.version,
            'flask_version': Flask.__version__,
            'hostname': os.uname().nodename if hasattr(os, 'uname') else 'unknown',
            'working_directory': os.getcwd(),
            'rnstatus_installed': rnstatus_installed,
            'rnstatus_message': rnstatus_msg
        },
        'log_level': logging.getLevelName(logger.getEffectiveLevel())
    })

def main():
    """Start the Gunicorn server."""
    port = int(os.getenv('PORT', 5000))
    workers = int(os.getenv('GUNICORN_WORKERS', 4))
    logger.info(f"Starting server on port {port} with {workers} workers")

    global _rnsd_thread
    _rnsd_thread = threading.Thread(target=run_rnsd, daemon=True)
    _rnsd_thread.start()

    time.sleep(3)

    logger.info("Attempting initial population of status cache...")
    get_and_cache_rnstatus_data()

    is_installed, msg = check_rnstatus_installation()
    if not is_installed:
        logger.error(f"rnstatus not properly installed: {msg}")
        stop_rnsd()
        sys.exit(1)
    else:
        logger.info(f"rnstatus check passed: {msg}")

    import gunicorn.app.base

    class StandaloneApplication(gunicorn.app.base.BaseApplication):
        """Gunicorn application."""

        def __init__(self, app, options=None):
            """Initialize the application."""
            self.options = options or {}
            self.application = app
            super().__init__()

        def load_config(self):
            """Load the configuration."""
            for key, value in self.options.items():
                self.cfg.set(key, value)

        def load(self):
            """Load the application."""
            return self.application

    temp_dir = tempfile.mkdtemp(prefix='gunicorn_')
    try:
        options = {
            'bind': f'0.0.0.0:{port}',
            'workers': workers,
            'worker_class': 'sync',
            'timeout': 120,
            'accesslog': None,
            'errorlog': '-',
            'loglevel': 'info',
            'worker_tmp_dir': temp_dir,
            'max_requests': 1000,
            'max_requests_jitter': 50,
            'keepalive': 5,
            'graceful_timeout': 30,
            'preload_app': True,
            'forwarded_allow_ips': '*',
            'proxy_protocol': True,
            'proxy_allow_ips': '*',
            'limit_request_line': 4094,
            'limit_request_fields': 100,
            'limit_request_field_size': 8190,
            'access_log_format': ''
        }

        StandaloneApplication(app, options).run()
    finally:
        stop_rnsd()
        try:
            shutil.rmtree(temp_dir)
            logger.info(f"Successfully cleaned up temporary directory: {temp_dir}")
        except FileNotFoundError:
            logger.info(f"Temporary directory {temp_dir} was not found during cleanup. It might have been removed by another process or Gunicorn.")
        except Exception as e:
            logger.error(f"Unexpected error cleaning up temporary directory {temp_dir}: {e}")

if __name__ == '__main__':
    main()
