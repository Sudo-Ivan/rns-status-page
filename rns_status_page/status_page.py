"""Reticulum Status Page Server.

This script creates a web server that displays Reticulum network status
information using rnstatus command output.
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess  # nosec B404
import tempfile
import threading
import time
from datetime import datetime

import bleach
import RNS
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from RNS.Interfaces.Interface import Interface as RNSInterface
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.abspath("status_page.log")),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)

app.wsgi_app = ProxyFix(
    app.wsgi_app, 
    x_for=1,      
    x_proto=1,    
    x_host=1,     
    x_prefix=1,   
    x_port=1      
)

@app.before_request
def log_request_info():
    headers = dict(request.headers)
    scheme_related_headers = {
        k: v for k, v in headers.items() 
        if any(scheme in k.lower() for scheme in ['forwarded', 'scheme', 'proto'])
    }
    if scheme_related_headers:
        logger.info(f"Scheme-related headers: {scheme_related_headers}")
        logger.info(f"Request scheme: {request.scheme}")
        logger.info(f"Request URL: {request.url}")

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": ["*"],
            "methods": ["GET", "OPTIONS"],
            "allow_headers": ["Content-Type", "Accept"],
            "max_age": 3600,
        },
        r"/events": {
            "origins": ["*"],
            "methods": ["GET", "OPTIONS"],
            "allow_headers": ["Content-Type", "Accept"],
            "max_age": 3600,
        },
    },
)

Talisman(
    app,
    content_security_policy={
        "default-src": "'self'",
        "script-src": "'self' 'unsafe-inline'",
        "style-src": "'self' 'unsafe-inline'",
        "img-src": "'self' data:",
        "font-src": "'self'",
        "connect-src": "'self'",
    },
    force_https=False,
    strict_transport_security=True,
    session_cookie_secure=True,
    session_cookie_http_only=True,
    session_cookie_samesite="Lax",
)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["1000 per day", "300 per hour"],
    storage_uri="memory://",
)

IGNORE_SECTIONS = ["Shared Instance", "AutoInterface", "LocalInterface"]

CACHE_DURATION_SECONDS = 30
RETRY_INTERVAL_SECONDS = 30
SSE_UPDATE_INTERVAL_SECONDS = 5

UPTIME_FILE_PATH = os.path.abspath("uptime.json")

STALE_THRESHOLD_MINUTES = 15


def size_str(num, suffix="B"):
    """Format number to human readable size string."""
    if num < 0:
        return "0 B"

    units = ["", "K", "M", "G", "T", "P", "E", "Z"]
    last_unit = "Y"

    if suffix == "b":
        num *= 8

    for unit in units:
        if abs(num) < 1000.0:
            if unit == "":
                return f"{num:.0f} {unit}{suffix}"
            return f"{num:.2f} {unit}{suffix}"
        num /= 1000.0

    return f"{num:.2f} {last_unit}{suffix}"


def speed_str(num, suffix="bps"):
    """Format number to human readable speed string."""
    if num < 0:
        return "0 bps"

    units = ["", "k", "M", "G", "T", "P", "E", "Z"]
    last_unit = "Y"

    if suffix == "Bps":
        num /= 8
        units = ["", "K", "M", "G", "T", "P", "E", "Z"]

    for unit in units:
        if abs(num) < 1000.0:
            return f"{num:.2f} {unit}{suffix}"
        num /= 1000.0

    return f"{num:.2f} {last_unit}{suffix}"


def load_uptime_tracker(filepath):
    """Load uptime tracker data from a JSON file.

    Args:
        filepath (str): The path to the JSON file.

    Returns:
        dict: The loaded uptime tracker data, or an empty dictionary if loading fails.
    """
    if os.path.exists(filepath):
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    logger.info(f"Successfully loaded uptime tracker from {filepath}")
                    return data
                else:
                    logger.warning(
                        f"Corrupted uptime tracker file (not a dict): {filepath}. Starting fresh."
                    )
                    return {}
        except json.JSONDecodeError:
            logger.warning(
                f"Error decoding JSON from uptime tracker file: {filepath}. Starting fresh."
            )
            return {}
        except Exception as e:
            logger.error(
                f"Unexpected error loading uptime tracker from {filepath}: {e}. Starting fresh."
            )
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
        fd, temp_filepath = tempfile.mkstemp(
            dir=os.path.dirname(filepath) or ".",
            prefix=os.path.basename(filepath) + ".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        shutil.move(temp_filepath, filepath)
        logger.debug(f"Successfully saved uptime tracker to {filepath}")
    except Exception as e:
        logger.error(f"Error saving uptime tracker to {filepath}: {e}")
        if temp_filepath and os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
            except Exception as re:
                logger.error(
                    f"Error removing temporary uptime file {temp_filepath}: {re}"
                )


_cache = {
    "data": None,
    "timestamp": 0,
    "lock": threading.Lock(),
    "interface_uptime_tracker": load_uptime_tracker(UPTIME_FILE_PATH),
    "rns_instance": None,
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
        rnsd_path = shutil.which("rnsd")
        if not rnsd_path:
            logger.error("rnsd command not found in PATH")
            return False

        logger.info("Starting rnsd daemon...")
        _rnsd_process = subprocess.Popen(
            [rnsd_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,  # nosec B603
        )

        time.sleep(2)

        if _rnsd_process.poll() is not None:
            stderr = _rnsd_process.stderr.read()
            logger.error(f"rnsd failed to start: {stderr}")
            return False

        logger.info("rnsd daemon started successfully")
        return True

    except FileNotFoundError:
        logger.error(
            "rnsd command not found. Please ensure it is installed and in PATH."
        )
        return False
    except OSError as e:
        logger.error(
            f"Error starting rnsd due to an OS error: {e.strerror} (errno {e.errno})"
        )
        return False
    except Exception as e:
        logger.error(f"Error starting rnsd: {e}")
        return False


def stop_rnsd():
    """Stop the rnsd daemon if it's running and managed by this script."""
    global _rnsd_process

    if not _rnsd_process:
        logger.debug(
            "stop_rnsd called but _rnsd_process is not set, implying RNSD was not started by this script or already stopped."
        )
        return

    if _rnsd_process.poll() is None:
        logger.info("Stopping rnsd daemon...")
        _rnsd_process.terminate()


def _init_rns_instance(max_retries=5, retry_delay=1.0):
    """Initialize the RNS.Reticulum instance, with retries if rnsd is managed locally."""
    with _cache["lock"]:
        if _cache["rns_instance"] is not None:
            return _cache["rns_instance"]

        rns_instance = None
        is_rnsd_managed_locally = bool(_rnsd_process and _rnsd_process.poll() is None)
        attempts = max_retries if is_rnsd_managed_locally else 1

        def try_connect_shared():
            """Attempt to connect to a shared RNS instance."""
            try:
                instance = RNS.Reticulum(require_shared_instance=True)
                logger.info("Successfully connected to shared RNS instance.")
                return instance
            except Exception as e:
                logger.warning(f"Could not connect to shared RNS instance: {e}")
                return None

        for attempt in range(attempts):
            rns_instance = try_connect_shared()
            if rns_instance:
                _cache["rns_instance"] = rns_instance
                return rns_instance

            if not is_rnsd_managed_locally:
                break
            if attempt < attempts - 1:
                logger.info(f"Retrying in {retry_delay}s as rnsd is managed locally...")
                time.sleep(retry_delay)

        if not is_rnsd_managed_locally:
            try:
                configdir = os.getenv("RNS_CONFIG_DIR", None)
                rns_instance = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_INFO)
                logger.info("Successfully created a new RNS instance.")
                _cache["rns_instance"] = rns_instance
                return rns_instance
            except Exception as e_new:
                logger.error(f"Failed to create a new RNS instance: {e_new}")
        else:
            logger.error("All attempts to connect to locally managed rnsd failed.")

        _cache["rns_instance"] = None
        return None


def _get_rns_stats_direct(max_retries=3, retry_delay=25):
    """Fetch interface statistics directly using the RNS library.

    Returns:
        dict: A dictionary containing interface statistics, or an error dictionary.
    """
    rns_instance = _init_rns_instance()
    if not rns_instance:
        return {"error": "Reticulum instance not available. Cannot fetch stats."}

    def try_get_stats():
        """Attempt to get RNS statistics."""
        try:
            stats = rns_instance.get_interface_stats()
            if not stats or "interfaces" not in stats:
                logger.warning(
                    "RNS.get_interface_stats() returned empty or invalid data."
                )
                return {
                    "warning": "RNS.get_interface_stats() returned empty or invalid data."
                }
            return stats
        except ConnectionRefusedError as e:
            logger.warning(
                f"RNS instance still initializing or connection refused: {e}. Waiting {retry_delay} seconds before retry."
            )
            return {"error": "connection_refused", "details": str(e)}
        except Exception as e:
            logger.error(f"Error fetching stats directly from RNS: {e}", exc_info=True)
            return {"error": "general_error", "details": str(e)}

    for attempt in range(max_retries):
        result = try_get_stats()

        if "error" not in result:
            return result

        if result["error"] == "connection_refused" and attempt < max_retries - 1:
            logger.info(f"Retrying in {retry_delay}s...")
            time.sleep(retry_delay)
        else:
            if result["error"] == "connection_refused":
                logger.error(
                    "All attempts to fetch RNS stats failed due to ConnectionRefusedError."
                )
                with _cache["lock"]:
                    _cache["rns_instance"] = None
                return {
                    "error": f"Failed to fetch stats from RNS after multiple retries: {result['details']}"
                }
            else:
                with _cache["lock"]:
                    _cache["rns_instance"] = None
                return {
                    "error": f"Error fetching stats directly from RNS: {result['details']}"
                }

    return {
        "error": "Reached end of _get_rns_stats_direct without returning stats, implies max_retries hit for ConnectionRefusedError."
    }


def _parse_rns_stats_dict(stats_dict, current_uptime_tracker):
    """Parse the RNS interface stats dictionary into a structured format.

    Args:
        stats_dict (dict): The raw stats dictionary from RNS.get_interface_stats().
        current_uptime_tracker (dict): The current uptime tracker data.

    Returns:
        tuple: A tuple containing the parsed data (dict) and the updated uptime tracker (dict).
    """
    parsed_interfaces = {}
    updated_tracker = current_uptime_tracker.copy()
    current_time_for_uptime = time.time()

    if "error" in stats_dict or "warning" in stats_dict:
        return stats_dict, updated_tracker

    if "interfaces" not in stats_dict or not isinstance(stats_dict["interfaces"], list):
        logger.warning("No 'interfaces' list in stats_dict or it's not a list.")
        return {
            "warning": "Invalid or missing interface data in RNS stats."
        }, updated_tracker

    for ifstat in stats_dict["interfaces"]:
        try:
            interface_name_full = ifstat.get("name", "Unknown Interface")
            section_name_part = (
                interface_name_full.split("[")[0].strip()
                if "[" in interface_name_full
                else "Interface"
            )

            current_section_key_for_dict = interface_name_full

            if section_name_part in IGNORE_SECTIONS:
                continue

            name_inside_brackets = ""
            if "[" in interface_name_full and "]" in interface_name_full:
                name_inside_brackets = interface_name_full.split("[", 1)[1].rsplit(
                    "]", 1
                )[0]
            else:
                name_inside_brackets = interface_name_full

            tracker_key = interface_name_full
            previous_record = updated_tracker.get(tracker_key)
            first_up_ts = (
                previous_record.get("first_up_timestamp") if previous_record else None
            )

            interface_details = {
                "name": name_inside_brackets,
                "section_type": section_name_part,
                "status": "Up" if ifstat.get("status") else "Down",
                "details": {},
                "first_up_timestamp": first_up_ts,
            }

            new_status = interface_details["status"]
            if tracker_key not in updated_tracker:
                updated_tracker[tracker_key] = {
                    "first_up_timestamp": None,
                    "current_status": "Down",
                    "last_seen_up": None,
                }

            previous_status_in_tracker = updated_tracker[tracker_key].get(
                "current_status", "Down"
            )
            persisted_first_up_ts = updated_tracker[tracker_key].get(
                "first_up_timestamp"
            )

            if new_status == "Up":
                if previous_status_in_tracker == "Up" and persisted_first_up_ts:
                    interface_details["first_up_timestamp"] = persisted_first_up_ts
                else:
                    interface_details["first_up_timestamp"] = current_time_for_uptime
                    updated_tracker[tracker_key]["first_up_timestamp"] = (
                        current_time_for_uptime
                    )
                updated_tracker[tracker_key]["last_seen_up"] = current_time_for_uptime
            else:
                interface_details["first_up_timestamp"] = None
                updated_tracker[tracker_key]["first_up_timestamp"] = None
            updated_tracker[tracker_key]["current_status"] = new_status

            details_map = {
                "Mode": "mode",
                "Rate": "bitrate",
                "Noise Fl.": "noise_floor",
                "Battery": "battery_percent",
                "Airtime": ["airtime_short", "airtime_long"],
                "Ch. Load": ["channel_load_short", "channel_load_long"],
                "Peers": "peers",
                "I2P": "tunnelstate",
                "Access": "ifac_signature",
                "I2P B32": "i2p_b32",
                "Queued": "announce_queue",
                "Held": "held_announces",
                "Clients": "clients",
                "Network": "ifac_netname",
            }

            for display_key, stat_key_or_keys in details_map.items():
                if isinstance(stat_key_or_keys, list):
                    val_short = ifstat.get(stat_key_or_keys[0])
                    val_long = ifstat.get(stat_key_or_keys[1])
                    if val_short is not None and val_long is not None:
                        interface_details["details"][display_key] = (
                            f"{val_short}% (15s), {val_long}% (1h)"
                        )
                else:
                    stat_value = ifstat.get(stat_key_or_keys)
                    if stat_value is not None:
                        if stat_key_or_keys == "bitrate":
                            interface_details["details"][display_key] = speed_str(
                                stat_value
                            )
                        elif stat_key_or_keys == "noise_floor":
                            interface_details["details"][display_key] = (
                                f"{stat_value} dBm"
                            )
                        elif stat_key_or_keys == "battery_percent":
                            bat_state = ifstat.get("battery_state", "")
                            interface_details["details"][display_key] = (
                                f"{stat_value}% ({bat_state})"
                            )
                        elif (
                            stat_key_or_keys == "ifac_signature"
                            and ifstat.get("ifac_size") is not None
                        ):
                            sig_brief = (
                                RNS.hexrep(stat_value[-5:], delimit=False)
                                if isinstance(stat_value, bytes)
                                else str(stat_value)[-10:]
                            )
                            interface_details["details"][display_key] = (
                                f"{ifstat['ifac_size'] * 8}-bit IFAC by <...{sig_brief}>"
                            )
                        elif stat_key_or_keys == "mode":
                            mode_map = {
                                RNSInterface.MODE_FULL: "Full",
                                RNSInterface.MODE_ACCESS_POINT: "Access Point",
                                RNSInterface.MODE_POINT_TO_POINT: "Point-to-Point",
                                RNSInterface.MODE_ROAMING: "Roaming",
                                RNSInterface.MODE_BOUNDARY: "Boundary",
                                RNSInterface.MODE_GATEWAY: "Gateway",
                            }
                            interface_details["details"][display_key] = mode_map.get(
                                stat_value, "Unknown"
                            )
                        else:
                            interface_details["details"][display_key] = str(stat_value)

            rxb = ifstat.get("rxb", 0)
            txb = ifstat.get("txb", 0)
            rxs = ifstat.get("rxs", 0)
            txs = ifstat.get("txs", 0)
            rxb_str_fmt = f"↓{size_str(rxb)}  {speed_str(rxs)}"
            txb_str_fmt = f"↑{size_str(txb)}  {speed_str(txs)}"
            interface_details["details"]["Traffic"] = f"{txb_str_fmt}\n{rxb_str_fmt}"

            iaf = ifstat.get("incoming_announce_frequency")
            oaf = ifstat.get("outgoing_announce_frequency")
            if iaf is not None and oaf is not None:
                try:
                    iaf_str = (
                        RNS.prettyfrequency(iaf)
                        if RNS and hasattr(RNS, "prettyfrequency")
                        else f"{iaf:.2f} Hz"
                    )
                    oaf_str = (
                        RNS.prettyfrequency(oaf)
                        if RNS and hasattr(RNS, "prettyfrequency")
                        else f"{oaf:.2f} Hz"
                    )
                    interface_details["details"]["Announces"] = (
                        f"{oaf_str}↑\n{iaf_str}↓"
                    )
                except AttributeError:
                    interface_details["details"]["Announces"] = (
                        f"{oaf:.2f} Hz↑\n{iaf:.2f} Hz↓"
                    )

            parsed_interfaces[current_section_key_for_dict] = interface_details
        except Exception as e:
            logger.error(
                f"Error parsing interface stat for {ifstat.get('name', 'N/A')}: {e}",
                exc_info=True,
            )
            parsed_interfaces[ifstat.get("name", f"error_interface_{time.time()}")] = {
                "name": ifstat.get("name", "Error Interface"),
                "section_type": "Error",
                "status": "Unknown",
                "details": {"Error": f"Could not parse details: {e}"},
                "first_up_timestamp": None,
            }

    if "transport_id" in stats_dict and stats_dict["transport_id"] is not None:
        transport_info = {
            "name": "Transport Instance",
            "section_type": "Transport",
            "status": "Up",
            "details": {},
            "first_up_timestamp": None,
        }
        transport_id_str = (
            RNS.prettyhexrep(stats_dict["transport_id"])
            if RNS and hasattr(RNS, "prettyhexrep")
            else str(stats_dict["transport_id"])
        )
        transport_info["details"]["ID"] = transport_id_str
        if (
            "probe_responder" in stats_dict
            and stats_dict["probe_responder"] is not None
        ):
            probe_resp_str = (
                RNS.prettyhexrep(stats_dict["probe_responder"])
                if RNS and hasattr(RNS, "prettyhexrep")
                else str(stats_dict["probe_responder"])
            )
            transport_info["details"]["Probe Responder"] = f"{probe_resp_str} active"
        if (
            "transport_uptime" in stats_dict
            and stats_dict["transport_uptime"] is not None
        ):
            uptime_str = (
                RNS.prettytime(stats_dict["transport_uptime"])
                if RNS and hasattr(RNS, "prettytime")
                else f"{stats_dict['transport_uptime']}s"
            )
            transport_info["details"]["Uptime"] = uptime_str

        parsed_interfaces["Transport Instance Status"] = transport_info

    return parsed_interfaces, updated_tracker


def get_and_cache_rnstatus_data():
    """Fetch RNS stats directly, parse it, update uptime info, and update the cache.

    Returns:
        tuple: A tuple containing the parsed data and the current time.
    """
    raw_stats_dict = _get_rns_stats_direct()

    parsed_data, updated_tracker = _parse_rns_stats_dict(
        raw_stats_dict, _cache["interface_uptime_tracker"]
    )
    current_time = time.time()

    with _cache["lock"]:
        if not ("error" in parsed_data or "warning" in parsed_data):
            for info in parsed_data.values():
                if isinstance(info, dict) and info.get("status") == "Up":
                    tracker_key = info.get("name")
                    if (
                        tracker_key
                        and tracker_key in _cache["interface_uptime_tracker"]
                    ):
                        prev_tracker = _cache["interface_uptime_tracker"][tracker_key]
                        if prev_tracker.get(
                            "current_status"
                        ) == "Up" and prev_tracker.get("first_up_timestamp"):
                            if (
                                "first_up_timestamp" not in info
                                or info["first_up_timestamp"] is None
                            ):
                                info["first_up_timestamp"] = prev_tracker[
                                    "first_up_timestamp"
                                ]

                            if tracker_key in updated_tracker and isinstance(
                                updated_tracker[tracker_key], dict
                            ):
                                if (
                                    "first_up_timestamp"
                                    not in updated_tracker[tracker_key]
                                    or updated_tracker[tracker_key][
                                        "first_up_timestamp"
                                    ]
                                    is None
                                ):
                                    updated_tracker[tracker_key][
                                        "first_up_timestamp"
                                    ] = prev_tracker["first_up_timestamp"]

        _cache["data"] = parsed_data
        _cache["timestamp"] = current_time
        _cache["interface_uptime_tracker"] = updated_tracker
        save_uptime_tracker(UPTIME_FILE_PATH, _cache["interface_uptime_tracker"])

    return parsed_data, current_time


def get_status_data_with_caching():
    """Get status data, utilizing the cache if available and fresh.

    Returns:
        dict: A dictionary containing the timestamp, data, and debug information.
    """
    start_process_time = time.time()
    with _cache["lock"]:
        cached_data = _cache["data"]
        cache_timestamp = _cache["timestamp"]

    if cached_data and (time.time() - cache_timestamp < CACHE_DURATION_SECONDS):
        data_to_serve = cached_data
        data_timestamp = cache_timestamp
    else:
        fetched_data, fetched_timestamp = get_and_cache_rnstatus_data()
        data_to_serve = fetched_data
        data_timestamp = fetched_timestamp

    processing_time_ms = (time.time() - start_process_time) * 1000

    return {
        "timestamp": datetime.fromtimestamp(data_timestamp).isoformat(),
        "data": data_to_serve,
        "debug": {
            "processing_time_ms": processing_time_ms,
            "cache_hit": bool(
                cached_data and (time.time() - cache_timestamp < CACHE_DURATION_SECONDS)
            ),
            "rns_direct_mode": True,
        },
    }


def sanitize_html(text):
    """Sanitize HTML content to prevent XSS attacks.

    Args:
        text (str): The text to sanitize.

    Returns:
        str: Sanitized text.
    """
    if not isinstance(text, str):
        return str(text)
    return bleach.clean(text, strip=True)


def is_node_stale(info, current_time):
    """Check if a node is stale based on its traffic and announce activity.
    
    Args:
        info (dict): The interface information dictionary
        current_time (float): Current timestamp
        
    Returns:
        tuple: (is_stale, last_activity_time, reason)
    """
    if info["status"] != "Up":
        return False, None, None

    last_activity = None
    reason = []

    traffic = info.get("details", {}).get("Traffic", "")
    if traffic:
        try:
            parts = traffic.split("\n")
            if len(parts) >= 2:
                tx_parts = parts[0].split()
                rx_parts = parts[1].split()
                if len(tx_parts) >= 2 and len(rx_parts) >= 2:
                    tx_speed = float(tx_parts[1])
                    rx_speed = float(rx_parts[1])
                    if tx_speed == 0 and rx_speed == 0:
                        reason.append("no traffic")
        except (ValueError, IndexError):
            pass

    announces = info.get("details", {}).get("Announces", "")
    if announces:
        try:
            parts = announces.split("\n")
            if len(parts) >= 2:
                tx_freq = float(parts[0].split()[0])
                rx_freq = float(parts[1].split()[0])
                if tx_freq == 0 and rx_freq == 0:
                    reason.append("no announces")
        except (ValueError, IndexError):
            pass

    if info.get("first_up_timestamp"):
        last_activity = info["first_up_timestamp"]

    if reason and last_activity:
        time_diff = current_time - last_activity
        if time_diff > STALE_THRESHOLD_MINUTES * 60:
            return True, last_activity, " and ".join(reason)

    return False, None, None


def create_status_card(section, info):
    """Create HTML for a status card.

    Args:
        section (str): The section name.
        info (dict): The interface information.

    Returns:
        str: The HTML for the status card.
    """
    current_time = time.time()
    is_stale, last_activity, stale_reason = is_node_stale(info, current_time)

    if is_stale:
        status_class = "status-stale"
        status_text = "Stale"
    else:
        status_class = "status-up" if info["status"] == "Up" else "status-down"
        status_text = info["status"]

    card_title = sanitize_html(info["name"])
    address_value = None

    if "/" in info["name"]:
        parts = info["name"].split("/", 1)
        card_title = sanitize_html(parts[0])
        if len(parts) > 1:
            address_value = sanitize_html(parts[1])

    uptime_html = ""
    if info.get("first_up_timestamp"):
        now = time.time()
        duration_seconds = now - info["first_up_timestamp"]
        start_time = datetime.fromtimestamp(info["first_up_timestamp"])
        uptime_html = f"""
            <div class="detail-row uptime-info">
                <span class="detail-label">Uptime</span>
                <span class="detail-value">{sanitize_html(format_duration(duration_seconds))} (since {sanitize_html(start_time.strftime("%Y-%m-%d %H:%M:%S"))})</span>
            </div>
        """
    elif info["status"] == "Up":
        uptime_html = """
            <div class="detail-row uptime-info">
                <span class="detail-label">Uptime</span>
                <span class="detail-value">Unknown (interface is up)</span>
            </div>
        """

    stale_button_html = ""
    if is_stale and last_activity:
        last_activity_time = datetime.fromtimestamp(last_activity)
        stale_button_html = f"""
            <button class="stale-button" title="Node appears stale">
                <svg viewBox="0 0 24 24">
                    <path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>
                </svg>
                <div class="stale-tooltip">
                    ⚠️ {sanitize_html(stale_reason)} since {sanitize_html(last_activity_time.strftime("%Y-%m-%d %H:%M:%S"))}
                </div>
            </button>
        """

    details_html_parts = []
    if address_value:
        details_html_parts.append(
            f'<div class="detail-row"><span class="detail-label">Address</span><span class="detail-value" title="{sanitize_html(address_value)}">{sanitize_html(address_value)}</span></div>'
        )

    if info.get("details"):
        for key, value in info["details"].items():
            if (key == "Announces" or key == "Traffic") and "\n" in value:
                parts = value.split("\n")
                if len(parts) >= 1:
                    details_html_parts.append(
                        f'<div class="detail-row"><span class="detail-label">{sanitize_html(key)}</span><span class="detail-value">{sanitize_html(parts[0])}</span></div>'
                    )
                if len(parts) >= 2:
                    details_html_parts.append(
                        f'<div class="detail-row"><span class="detail-label">&nbsp;</span><span class="detail-value">{sanitize_html(parts[1])}</span></div>'
                    )
            else:
                details_html_parts.append(
                    f'<div class="detail-row"><span class="detail-label">{sanitize_html(key)}</span><span class="detail-value">{sanitize_html(value)}</span></div>'
                )
    details_html = "".join(details_html_parts)

    buttons_html = ""
    if info["section_type"] == "TCPInterface":
        export_url = f"/api/export/{sanitize_html(info['name'].replace('/', '_'))}"
        suggested_filename_base = sanitize_html(info["name"].split("/")[0])
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
        <div class="status-card" data-section-name="{sanitize_html(info["section_type"].lower())}" data-interface-name="{sanitize_html(info["name"].lower())}">
            {buttons_html}
            {stale_button_html}
            <div class="card-content">
                <h2 title="{sanitize_html(info["name"])}">
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
        return "N/A"

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

    return " ".join(parts)


def count_interfaces(data):
    """Count the number of up and down interfaces.

    Args:
        data (dict): The interface data dictionary.

    Returns:
        tuple: (up_count, down_count, total_count)

    """
    up_count = 0
    down_count = 0
    total_count = 0

    for info in data.values():
        if isinstance(info, dict) and "status" in info:
            total_count += 1
            if info["status"] == "Up":
                up_count += 1
            else:
                down_count += 1

    return up_count, down_count, total_count


def calculate_file_hash(filepath):
    """Calculate SHA-384 hash of a file.

    Args:
        filepath (str): Path to the file.

    Returns:
        str: Base64 encoded hash.

    """
    try:
        with open(filepath, "rb") as f:
            file_hash = hashlib.sha384(f.read()).digest()
            return base64.b64encode(file_hash).decode("utf-8")
    except Exception as e:
        logger.error(f"Error calculating file hash for {filepath}: {e}")
        return None


@app.route("/")
@limiter.exempt
def index():
    """Render the main status page."""
    data = get_status_data_with_caching()
    up_count, down_count, total_count = count_interfaces(data["data"])

    meta_description = f"Reticulum Network Status - Up: {up_count} Down: {down_count} Total: {total_count}"

    htmx_path = os.path.join(app.static_folder, "vendor", "htmx.min.js")
    htmx_integrity = calculate_file_hash(htmx_path)

    return render_template(
        "index.html",
        up_count=up_count,
        down_count=down_count,
        total_count=total_count,
        meta_description=meta_description,
        htmx_integrity=htmx_integrity,
        rns_direct_mode=True,
    )


@app.route("/api/status")
@limiter.limit("30 per minute")
def status():
    """Return the current status as HTML or JSON via an API endpoint."""
    data_payload = get_status_data_with_caching()

    if (
        request.accept_mimetypes.accept_json
        and not request.accept_mimetypes.accept_html
    ):
        return jsonify(data_payload)

    if "error" in data_payload["data"] or "warning" in data_payload["data"]:
        error_or_warning_key = "error" if "error" in data_payload["data"] else "warning"
        message = sanitize_html(data_payload["data"][error_or_warning_key])
        if "connection refused" in message.lower():
            return '<div class="status-card loading-card"><div class="loading-message"><div class="spinner"></div><p>Initializing connection to Reticulum network...</p><p class="loading-subtext">This may take a few moments while the network connection is established.</p></div></div>'
        return f'<div class="status-card error-card"><div class="error-message">{message}</div></div>'

    cards_html = ""
    for section, info in data_payload["data"].items():
        if section not in IGNORE_SECTIONS:
            cards_html += create_status_card(section, info)

    return (
        cards_html
        if cards_html
        else '<div class="status-card error-card"><div class="error-message">No interfaces found or rnstatus output was empty.</div></div>'
    )


@app.route("/api/search")
@limiter.limit("10 per minute")
def search():
    """Search interfaces and return matching cards as HTML or JSON."""
    query = sanitize_html(request.args.get("q", "").lower())
    data_payload = get_status_data_with_caching()

    if "error" in data_payload["data"] or "warning" in data_payload["data"]:
        if (
            request.accept_mimetypes.accept_json
            and not request.accept_mimetypes.accept_html
        ):
            return jsonify(data_payload)
        else:
            error_or_warning_key = (
                "error" if "error" in data_payload["data"] else "warning"
            )
            message = sanitize_html(data_payload["data"][error_or_warning_key])
            return f'<div class="status-card error-card"><div class="error-message">{message}</div></div>'

    filtered_data = {}
    for section, info in data_payload["data"].items():
        if section in IGNORE_SECTIONS:
            continue
        if not isinstance(info, dict):
            continue

        if (
            query in section.lower()
            or query in info.get("name", "").lower()
            or any(query in str(v).lower() for v in info.get("details", {}).values())
        ):
            filtered_data[section] = info

    if (
        request.accept_mimetypes.accept_json
        and not request.accept_mimetypes.accept_html
    ):
        return jsonify(
            {
                "timestamp": data_payload["timestamp"],
                "data": filtered_data,
                "debug": data_payload["debug"],
                "query": query,
            }
        )
    else:
        cards_html = ""
        if filtered_data:
            for section, info in filtered_data.items():
                cards_html += create_status_card(section, info)
        else:
            cards_html = '<div class="status-card error-card"><div class="error-message">No matching interfaces found for your query.</div></div>'
        return cards_html


@app.route("/api/export/<interface_name>")
@limiter.limit("10 per minute")
def export_interface(interface_name):
    """Export interface configuration."""
    data = get_status_data_with_caching()
    if data.get("error"):
        return f'<div class="status-card error-card"><div class="error-message">{data["error"]}</div></div>'

    interface_name = interface_name.replace("_", "/")

    for info in data["data"].values():
        if info["name"] == interface_name:
            name = info["name"].split("/")[0]
            address = info["name"].split("/")[1] if "/" in info["name"] else ""
            host, port = address.split(":") if ":" in address else ("", "")

            config = f"""[[{name}]]
    type = TCPClientInterface
    interface_enabled = true
    target_host = {host}
    target_port = {port}
"""
            response = Response(config, mimetype="text/plain")
            response.headers["Content-Disposition"] = (
                f'attachment; filename="{name}.txt"'
            )
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

    return '<div class="status-card error-card"><div class="error-message">Interface not found</div></div>'


@app.route("/api/export-all")
@limiter.limit("10 per minute")
def export_all():
    """Export all interface configurations."""
    data = get_status_data_with_caching()
    if data.get("error"):
        return f'<div class="status-card error-card"><div class="error-message">{data["error"]}</div></div>'

    config = ""
    for info in data["data"].values():
        if info["section_type"] == "TCPInterface":
            name = info["name"].split("/")[0]
            address = info["name"].split("/")[1] if "/" in info["name"] else ""
            host, port = address.split(":") if ":" in address else ("", "")

            config += f"""[[{name}]]
    type = TCPClientInterface
    interface_enabled = true
    target_host = {host}
    target_port = {port}

"""

    response = Response(config, mimetype="text/plain")
    response.headers["Content-Disposition"] = (
        'attachment; filename="all_interfaces.txt"'
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/events")
@limiter.exempt
def stream_status_events():
    """Streams status updates using Server-Sent Events (SSE)."""

    def event_stream_generator():
        """Generates a stream of server-sent events."""
        last_sent_timestamp = 0
        try:
            while True:
                current_data_payload = get_status_data_with_caching()
                data_timestamp_iso = current_data_payload["timestamp"]
                data_timestamp_float = datetime.fromisoformat(
                    data_timestamp_iso
                ).timestamp()

                if data_timestamp_float > last_sent_timestamp:
                    json_data = json.dumps(current_data_payload)
                    yield f"data: {json_data}\n\n"
                    last_sent_timestamp = data_timestamp_float

                time.sleep(SSE_UPDATE_INTERVAL_SECONDS)
        except GeneratorExit:
            pass
        except Exception as e:
            logger.error(f"Error in SSE stream: {e}", exc_info=True)
            error_payload = json.dumps(
                {"error": "Stream error occurred", "type": "SERVER_ERROR"}
            )
            yield f"event: error\ndata: {error_payload}\n\n"

    return Response(event_stream_generator(), mimetype="text/event-stream")


@app.route("/health")
def health_check():
    """Return a simple health check."""
    return jsonify({"status": "ok"})


def main():
    """Start the Gunicorn server."""
    parser = argparse.ArgumentParser(description="Reticulum Status Page Server.")
    parser.add_argument(
        "--no-rnsd",
        action="store_true",
        help="Do not start or manage the rnsd process. Assumes rnsd is already running.",
    )
    args = parser.parse_args()

    port = int(os.getenv("PORT", 5000))
    workers = int(os.getenv("GUNICORN_WORKERS", 4))
    logger.info(f"Starting server on port {port} with {workers} workers")

    global _rnsd_thread

    if args.no_rnsd:
        managed_rnsd = False
        logger.info("RNSD management disabled by --no-rnsd flag.")
    else:
        managed_rnsd_env = os.getenv("MANAGED_RNSD", "true").lower()
        if managed_rnsd_env == "false":
            managed_rnsd = False
            logger.info(
                "RNSD management disabled by MANAGED_RNSD=false environment variable."
            )
        else:
            managed_rnsd = True
            if managed_rnsd_env != "true":
                logger.warning(
                    f"MANAGED_RNSD environment variable set to '{managed_rnsd_env}', which is not 'false'. Defaulting to managing RNSD. Use 'true' or 'false'."
                )

    if managed_rnsd:
        logger.info("RNSD will be managed by this script.")
        _rnsd_thread = threading.Thread(target=run_rnsd, daemon=True)
        _rnsd_thread.start()
    else:
        logger.info(
            "RNSD is expected to be running externally and will not be managed by this script."
        )

    time.sleep(3)

    logger.info("Attempting initial population of status cache...")
    get_and_cache_rnstatus_data()

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

    temp_dir = os.path.abspath(os.path.join(tempfile.gettempdir(), "gunicorn_rns_status"))
    try:
        os.makedirs(temp_dir, exist_ok=True)
        logger.info(f"Using persistent temporary directory: {temp_dir}")
    except Exception as e:
        logger.error(f"Failed to create persistent temp directory: {e}")
        temp_dir = os.path.abspath(tempfile.mkdtemp(prefix="gunicorn_"))
        logger.info(f"Falling back to temporary directory: {temp_dir}")

    try:
        options = {
            "bind": f"0.0.0.0:{port}",
            "workers": workers,
            "worker_class": "sync",
            "timeout": 120,
            "accesslog": None,
            "errorlog": "-",
            "loglevel": "info",
            "worker_tmp_dir": temp_dir,
            "max_requests": 1000,
            "max_requests_jitter": 50,
            "keepalive": 5,
            "graceful_timeout": 30,
            "preload_app": True,
            "forwarded_allow_ips": "*",
            "secure_scheme_headers": {
                'X-FORWARDED-PROTOCOL': 'https',
                'X-FORWARDED-PROTO': 'https',
                'X-FORWARDED-SSL': 'on'
            },
            "proxy_protocol": True,
            "proxy_allow_ips": "*",
            "limit_request_line": 4094,
            "limit_request_fields": 100,
            "limit_request_field_size": 8190,
            "access_log_format": "",
        }

        StandaloneApplication(app, options).run()
    finally:
        if managed_rnsd:
            stop_rnsd()
        # Only try to remove the temp directory if it's not our persistent one
        if temp_dir != os.path.abspath(os.path.join(tempfile.gettempdir(), "gunicorn_rns_status")):
            try:
                shutil.rmtree(temp_dir)
                logger.info(f"Successfully cleaned up temporary directory: {temp_dir}")
            except FileNotFoundError:
                logger.info(
                    f"Temporary directory {temp_dir} was not found during cleanup. It might have been removed by another process or Gunicorn."
                )
            except Exception as e:
                logger.error(
                    f"Unexpected error cleaning up temporary directory {temp_dir}: {e}"
                )

    with _cache["lock"]:
        if _cache["rns_instance"] is not None:
            _cache["rns_instance"] = None
            logger.info("Cleared local RNS instance reference.")


if __name__ == "__main__":
    main()
