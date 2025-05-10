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

_cache = {
    'data': None,
    'timestamp': 0,
    'lock': threading.Lock(),
    'interface_uptime_tracker': {}
}

_rnsd_process = None
_rnsd_thread = None

def run_rnsd():
    """Run rnsd daemon in a separate thread."""
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
    """Execute rnstatus command and return the output."""
    try:
        is_installed, error_msg = check_rnstatus_installation()
        if not is_installed:
            logger.error(f"rnstatus installation check failed: {error_msg}")
            return f"Error: {error_msg}"

        rnstatus_path = shutil.which('rnstatus')
        if not rnstatus_path:
            return "Error: rnstatus command not found in PATH"

        result = subprocess.run([rnstatus_path],  # nosec B603
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
    """Fetch rnstatus data, parse it, update uptime info, and update the cache."""
    raw_output = get_rnstatus_from_command()
    parsed_data, updated_tracker = parse_rnstatus(raw_output, _cache['interface_uptime_tracker'])
    current_time = time.time()

    with _cache['lock']:
        for info in parsed_data.values():
            if info.get('status') == 'Up':
                tracker_key = info['name']
                if tracker_key in _cache['interface_uptime_tracker']:
                    prev_tracker = _cache['interface_uptime_tracker'][tracker_key]
                    if prev_tracker.get('current_status') == 'Up' and prev_tracker.get('first_up_timestamp'):
                        info['first_up_timestamp'] = prev_tracker['first_up_timestamp']
                        updated_tracker[tracker_key]['first_up_timestamp'] = prev_tracker['first_up_timestamp']

        _cache['data'] = parsed_data
        _cache['timestamp'] = current_time
        _cache['interface_uptime_tracker'] = updated_tracker

    return parsed_data, current_time

def get_status_data_with_caching():
    """Get status data, utilizing the cache if available and fresh."""
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
    """Parse the rnstatus output into a structured format."""
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

        elif current_section_title and not is_current_section_ignored and ':' in line:
            key, value_part = line.split(':', 1)
            key = key.strip()
            value = value_part.strip()

            if key == "Status":
                new_status = 'Up' if 'Up' in value else 'Down'
                tracker_key = sections[current_section_title]['name']
                current_status = updated_tracker[tracker_key]['current_status']

                sections[current_section_title]['status'] = new_status

                if new_status == 'Up':
                    if current_status == 'Down':
                        first_up_ts = current_time_for_uptime
                        sections[current_section_title]['first_up_timestamp'] = first_up_ts
                        updated_tracker[tracker_key]['first_up_timestamp'] = first_up_ts
                        updated_tracker[tracker_key]['current_status'] = 'Up'
                        updated_tracker[tracker_key]['last_seen_up'] = current_time_for_uptime
                    else:
                        if not updated_tracker[tracker_key]['first_up_timestamp']:
                            first_up_ts = updated_tracker[tracker_key].get('last_seen_up', current_time_for_uptime)
                            sections[current_section_title]['first_up_timestamp'] = first_up_ts
                            updated_tracker[tracker_key]['first_up_timestamp'] = first_up_ts
                        updated_tracker[tracker_key]['last_seen_up'] = current_time_for_uptime
                else:
                    if current_status == 'Up':
                        sections[current_section_title]['first_up_timestamp'] = None
                        updated_tracker[tracker_key]['first_up_timestamp'] = None
                        updated_tracker[tracker_key]['current_status'] = 'Down'
                        updated_tracker[tracker_key]['last_seen_up'] = None

            if key == "Traffic":
                if idx < len(lines):
                    next_line_stripped = lines[idx].strip()
                    if next_line_stripped.startswith("↓"):
                        value += f"\n{next_line_stripped}"
                        idx += 1

            sections[current_section_title]['details'][key] = value

    return sections, updated_tracker

def create_status_card(section, info):
    """Create HTML for a status card."""
    status_class = 'status-up' if info['status'] == 'Up' else 'status-down'

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

    details_html = ''
    if info.get('details'):
        details_html = ''.join(
            f'<div class="detail-row"><span class="detail-label">{key}</span><span class="detail-value">{value}</span></div>'
            for key, value in info['details'].items()
        )

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
                <h2>
                    <span class="status-indicator {status_class}"></span>
                    {info['name']}
                </h2>
                {uptime_html}
                {details_html}
            </div>
        </div>
    """

def format_duration(seconds):
    """Format duration in seconds to human readable string."""
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
    """Return the current status as HTML via an API endpoint."""
    data = get_status_data_with_caching()
    if data.get('error'):
        return f'<div class="status-card error-card"><div class="error-message">{data["error"]}</div></div>'

    cards_html = ''
    for section, info in data['data'].items():
        if section not in IGNORE_SECTIONS:
            cards_html += create_status_card(section, info)

    return cards_html

@app.route('/api/search')
@limiter.limit("10 per minute")
def search():
    """Search interfaces and return matching cards."""
    query = request.args.get('q', '').lower()
    data = get_status_data_with_caching()

    if data.get('error'):
        return f'<div class="status-card error-card"><div class="error-message">{data["error"]}</div></div>'

    cards_html = ''
    for section, info in data['data'].items():
        if section in IGNORE_SECTIONS:
            continue

        if (query in section.lower() or
            query in info['name'].lower() or
            any(query in str(v).lower() for v in info.get('details', {}).values())):
            cards_html += create_status_card(section, info)

    return cards_html or '<div class="status-card error-card"><div class="error-message">No matching interfaces found</div></div>'

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
        def __init__(self, app, options=None):
            self.options = options or {}
            self.application = app
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                self.cfg.set(key, value)

        def load(self):
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
