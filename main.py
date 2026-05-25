"""
main.py — Integrated Door Lock Controller
==========================================
Components:
  - Webcam          face recognition  (yolo12s ncnn + ArcFace onnx)
  - R307S           fingerprint       (UART)
  - RC522           RFID reader       (SPI)
  - I2C LCD         16×2 display      (I2C 0x27)
  - Buzzer          GPIO 26
  - Relay 1         GPIO 6
  - Relay 2         GPIO 12

Architecture:
  Face detection runs in its own daemon thread (face.py FaceDetector).
  RFID and Fingerprint each run in their own daemon threads.
  All auth events funnel into a single event_queue consumed by one
  EventHandler thread that owns all GPIO — no race conditions possible.
  Main thread runs the cv2 GUI loop (imshow must be on main thread).

MQTT topics (publish):
  door/auth    — every auth attempt (granted or denied)
  door/relay   — relay state changes (locked/unlocked)
  door/system  — startup, shutdown, thread errors
  door/sensors — sensor health (camera, fingerprint, RFID online/offline)

MQTT topics (subscribe):
  door/command/unlock  — timed unlock (seconds payload, capped at MAX_UNLOCK_SECONDS)
  door/command/lock    — force re-lock both relays immediately
  door/command/sensor  — enable/disable a specific sensor

Database:  users.json   (written by reg.py)
Face DB:   face_database/<name>/*.jpg  (written by reg.py enroll-face mode)
MQTT cfg:  mqtt.conf    (INI format — host, port, ws_port, username, password)
"""

import configparser
import json
import logging
import os
import queue
import signal
import ssl
import sys
import threading
import time

# ── lgpio ─────────────────────────────────────────────────────────────────────
try:
    import lgpio
except ImportError:
    raise SystemExit("lgpio not found. Run: pip install lgpio")

# ── Fingerprint ───────────────────────────────────────────────────────────────
try:
    from pyfingerprint.pyfingerprint import PyFingerprint
except ImportError:
    raise SystemExit("pyfingerprint not found. Run: pip install pyfingerprint")

# ── RFID ──────────────────────────────────────────────────────────────────────
try:
    from MFRC522 import MFRC522
except ImportError:
    raise SystemExit("MFRC522.py not found. Place it beside main.py")

# ── LCD ───────────────────────────────────────────────────────────────────────
try:
    from RPLCD.i2c import CharLCD
except ImportError:
    raise SystemExit("RPLCD not found. Run: pip install RPLCD")

# ── MQTT ──────────────────────────────────────────────────────────────────────
try:
    import paho.mqtt.client as mqtt
except ImportError:
    raise SystemExit("paho-mqtt not found. Run: pip install paho-mqtt")

# ── Face detector (local module) ──────────────────────────────────────────────
try:
    from face import FaceDetector
except ImportError:
    raise SystemExit("face.py not found. Place it beside main.py")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

BUZZER_PIN = 26
RELAY1_PIN = 6
RELAY2_PIN = 12

# Maximum seconds an MQTT unlock command may request
MAX_UNLOCK_SECONDS = 30

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

# Path to MQTT credentials file (sits beside main.py)
MQTT_CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mqtt.conf")

# lgpio chip handle — opened once in setup_gpio()
_chip: int = -1


# ══════════════════════════════════════════════════════════════════════════════
# MQTT CONFIG LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_mqtt_config(path: str) -> dict:
    """
    Reads mqtt.conf (INI format, [mqtt] section) and returns a dict with:
      host, port (int), ws_port (int), username, password
    Raises SystemExit if the file is missing or a required key is absent.
    """
    if not os.path.exists(path):
        raise SystemExit(
            f"mqtt.conf not found at '{path}'.\n"
            "Create it with [mqtt] section containing: host, port, ws_port, username, password"
        )

    parser = configparser.ConfigParser()
    parser.read(path)

    if "mqtt" not in parser:
        raise SystemExit("mqtt.conf is missing the [mqtt] section.")

    section = parser["mqtt"]
    required_keys = ("host", "port", "username", "password")
    for key in required_keys:
        if key not in section:
            raise SystemExit(f"mqtt.conf is missing required key: '{key}'")

    return {
        "host":     section["host"].strip(),
        "port":     int(section["port"].strip()),
        "ws_port":  int(section.get("ws_port", "8884").strip()),
        "username": section["username"].strip(),
        "password": section["password"].strip(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def load_authorized_ids() -> tuple[set, set]:
    """Return (rfid_uids, fingerprint_template_ids) from users.json."""
    if not os.path.exists(DB_PATH):
        log.warning("users.json not found — no users loaded. Run reg.py first.")
        return set(), set()
    try:
        with open(DB_PATH) as f:
            db = json.load(f)
        rfid_ids   = set()
        finger_ids = set()
        for user in db.get("users", []):
            methods = user.get("methods", {})
            if "rfid" in methods:
                rfid_ids.add(methods["rfid"]["uid"])
            if "fingerprint" in methods:
                finger_ids.add(methods["fingerprint"]["template_id"])
        log.info(
            "Loaded %d RFID uid(s), %d fingerprint template(s) from users.json.",
            len(rfid_ids), len(finger_ids),
        )
        return rfid_ids, finger_ids
    except Exception as exc:
        log.error("Failed to load users.json: %s", exc)
        return set(), set()


AUTHORIZED_RFID_IDS:        set = set()
AUTHORIZED_FINGERPRINT_IDS: set = set()


# ══════════════════════════════════════════════════════════════════════════════
# SHARED STATE
# ══════════════════════════════════════════════════════════════════════════════

# event_queue carries ("RFID"|"FINGER"|"FACE", identifier, authorized: bool)
lcd_queue   = queue.Queue()
event_queue = queue.Queue()
stop_event  = threading.Event()

# Sensor-enable flags — toggled via door/command/sensor
sensor_enabled = {
    "rfid":        True,
    "fingerprint": True,
    "face":        True,
}
sensor_enabled_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# GPIO  (lgpio)
# ══════════════════════════════════════════════════════════════════════════════

def setup_gpio():
    global _chip
    _chip = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_output(_chip, BUZZER_PIN, 0)   # buzzer OFF
    lgpio.gpio_claim_output(_chip, RELAY1_PIN, 1)   # relay LOCKED (HIGH)
    lgpio.gpio_claim_output(_chip, RELAY2_PIN, 1)   # relay LOCKED (HIGH)
    log.info(
        "lgpio ready: buzzer=%d relay1=%d relay2=%d (relays HIGH/locked)",
        BUZZER_PIN, RELAY1_PIN, RELAY2_PIN,
    )


def cleanup_gpio():
    global _chip
    if _chip >= 0:
        lgpio.gpio_write(_chip, BUZZER_PIN, 0)
        lgpio.gpio_write(_chip, RELAY1_PIN, 1)
        lgpio.gpio_write(_chip, RELAY2_PIN, 1)
        lgpio.gpiochip_close(_chip)
        _chip = -1
    log.info("lgpio cleaned up. Relays HIGH (locked).")


# ══════════════════════════════════════════════════════════════════════════════
# BUZZER / RELAY HELPERS  (event-handler thread only)
# ══════════════════════════════════════════════════════════════════════════════

def _beep(times: int, on_ms: int = 100, off_ms: int = 100):
    for _ in range(times):
        lgpio.gpio_write(_chip, BUZZER_PIN, 1)
        time.sleep(on_ms / 1000)
        lgpio.gpio_write(_chip, BUZZER_PIN, 0)
        time.sleep(off_ms / 1000)


def _dual_relay_pulse(sec_relay1: float, sec_relay2: float):
    def relay_worker():
        lgpio.gpio_write(_chip, RELAY1_PIN, 0)
        lgpio.gpio_write(_chip, RELAY2_PIN, 0)
        log.info(
            "Relays UNLOCKED: GPIO%d (%.1fs), GPIO%d (%.1fs)",
            RELAY1_PIN, sec_relay1, RELAY2_PIN, sec_relay2,
        )
        mqtt_publish("door/relay", {
            "state":   "unlocked",
            "relay1_sec": sec_relay1,
            "relay2_sec": sec_relay2,
        })
        start     = time.time()
        r1_active = True
        r2_active = True
        while r1_active or r2_active:
            elapsed = time.time() - start
            if r1_active and elapsed >= sec_relay1:
                lgpio.gpio_write(_chip, RELAY1_PIN, 1)
                log.info("Relay GPIO%d LOCKED", RELAY1_PIN)
                r1_active = False
            if r2_active and elapsed >= sec_relay2:
                lgpio.gpio_write(_chip, RELAY2_PIN, 1)
                log.info("Relay GPIO%d LOCKED", RELAY2_PIN)
                r2_active = False
            time.sleep(0.05)
        mqtt_publish("door/relay", {"state": "locked"})

    # Spin up background thread so it doesn't halt the event handler loop!
    threading.Thread(target=relay_worker, daemon=True).start()


def _force_lock():
    """Immediately locks both relays. Called by door/command/lock handler."""
    lgpio.gpio_write(_chip, RELAY1_PIN, 1)
    lgpio.gpio_write(_chip, RELAY2_PIN, 1)
    log.info("Force-lock: both relays set HIGH immediately.")
    mqtt_publish("door/relay", {"state": "locked", "reason": "force_lock_command"})


# ══════════════════════════════════════════════════════════════════════════════
# LCD THREAD
# ══════════════════════════════════════════════════════════════════════════════

def lcd_thread_fn():
    try:
        lcd = CharLCD(
            i2c_expander="PCF8574",
            address=0x27,
            port=1,
            cols=16,
            rows=2,
            dotsize=8,
        )
        lcd.clear()
        lcd.write_string("System Ready".center(16))
        log.info("LCD ready.")
    except Exception as exc:
        log.error("LCD init failed: %s", exc)
        return

    while not stop_event.is_set():
        try:
            line1, line2, duration = lcd_queue.get(timeout=0.5)
            lcd.clear()
            lcd.write_string(line1[:16].ljust(16))
            lcd.cursor_pos = (1, 0)
            lcd.write_string(line2[:16].ljust(16))
            time.sleep(duration)
            lcd.clear()
            lcd.write_string("Ready".center(16))
        except queue.Empty:
            pass
        except Exception as exc:
            log.warning("LCD error: %s", exc)

    try:
        lcd.clear()
        lcd.close(clear=True)
    except Exception:
        pass


def lcd_show(line1: str, line2: str = "", duration: float = 2.0):
    """Non-blocking — enqueues an LCD message from any thread."""
    lcd_queue.put((line1, line2, duration))


# ══════════════════════════════════════════════════════════════════════════════
# MQTT CLIENT
# ══════════════════════════════════════════════════════════════════════════════

# Module-level client reference — set in setup_mqtt()
_mqtt_client: mqtt.Client | None = None


def mqtt_publish(topic: str, payload: dict):
    """
    Serialize payload to JSON and publish to topic.
    Silently skips if the client is not yet connected.
    """
    global _mqtt_client
    if _mqtt_client is None:
        return
    try:
        message = json.dumps(payload)
        _mqtt_client.publish(topic, message, qos=1, retain=False)
        log.debug("MQTT → %s  %s", topic, message)
    except Exception as exc:
        log.warning("MQTT publish failed [%s]: %s", topic, exc)


# ── Inbound command handlers ──────────────────────────────────────────────────

def _handle_unlock_command(payload_str: str):
    """
    door/command/unlock
    Payload: plain integer seconds, e.g. "10"
    Unlocks both relays for that duration (capped at MAX_UNLOCK_SECONDS).
    The command is routed through event_queue so GPIO stays on one thread.
    """
    try:
        requested = float(payload_str.strip())
    except ValueError:
        log.warning("door/command/unlock — invalid payload: %r", payload_str)
        return

    seconds = min(max(requested, 0.1), MAX_UNLOCK_SECONDS)
    log.info("door/command/unlock — %.1fs requested, %.1fs applied.", requested, seconds)
    # Inject a synthetic auth event so the EventHandler thread drives GPIO
    event_queue.put(("MQTT_UNLOCK", seconds, True))


def _handle_lock_command(_payload_str: str):
    """
    door/command/lock
    Immediately locks both relays regardless of any active unlock window.
    Also routed through event_queue for thread safety.
    """
    log.info("door/command/lock — force-lock received.")
    event_queue.put(("MQTT_LOCK", None, True))


def _handle_sensor_command(payload_str: str):
    """
    door/command/sensor
    Payload JSON: {"sensor": "rfid"|"fingerprint"|"face", "enabled": true|false}
    """
    try:
        data    = json.loads(payload_str)
        sensor  = data["sensor"].lower().strip()
        enabled = bool(data["enabled"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("door/command/sensor — invalid payload %r: %s", payload_str, exc)
        return

    if sensor not in sensor_enabled:
        log.warning("door/command/sensor — unknown sensor: %r", sensor)
        return

    with sensor_enabled_lock:
        sensor_enabled[sensor] = enabled

    state = "enabled" if enabled else "disabled"
    log.info("Sensor %r %s via MQTT command.", sensor, state)
    mqtt_publish("door/sensors", {"sensor": sensor, "enabled": enabled})
    lcd_show(f"Sensor {sensor[:8]}", state.capitalize(), duration=2.0)


# ── Paho callbacks ────────────────────────────────────────────────────────────

def _on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        log.info("MQTT connected to broker.")
        client.subscribe("door/command/unlock", qos=1)
        client.subscribe("door/command/lock",   qos=1)
        client.subscribe("door/command/sensor", qos=1)
        mqtt_publish("door/system", {"event": "startup", "status": "connected"})
    else:
        log.error("MQTT connection failed — reason code %s", reason_code)


def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    if reason_code != 0:
        log.warning("MQTT unexpected disconnect (reason=%s). Will auto-reconnect.", reason_code)


def _on_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace").strip()
    log.info("MQTT ← %s  %r", topic, payload)

    if topic == "door/command/unlock":
        _handle_unlock_command(payload)
    elif topic == "door/command/lock":
        _handle_lock_command(payload)
    elif topic == "door/command/sensor":
        _handle_sensor_command(payload)
    else:
        log.debug("Unhandled MQTT topic: %s", topic)


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_mqtt(cfg: dict) -> mqtt.Client:
    """
    Creates, configures, and connects the Paho MQTT client using TLS (port 8883).
    Credentials are taken from the dict returned by load_mqtt_config().
    Returns the connected client object.
    """
    global _mqtt_client

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="door-controller",
        clean_session=True,
    )
    client.username_pw_set(cfg["username"], cfg["password"])
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)

    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message    = _on_message

    log.info("Connecting to MQTT broker %s:%d …", cfg["host"], cfg["port"])
    client.connect(cfg["host"], cfg["port"], keepalive=60)
    client.loop_start()   # background network thread

    _mqtt_client = client
    return client


def teardown_mqtt():
    """Gracefully disconnect the MQTT client."""
    global _mqtt_client
    if _mqtt_client is not None:
        try:
            mqtt_publish("door/system", {"event": "shutdown"})
            time.sleep(0.3)   # give the publish a moment to flush
            _mqtt_client.loop_stop()
            _mqtt_client.disconnect()
        except Exception as exc:
            log.warning("MQTT teardown error: %s", exc)
        _mqtt_client = None


# ══════════════════════════════════════════════════════════════════════════════
# RFID THREAD
# ══════════════════════════════════════════════════════════════════════════════

def rfid_thread_fn():
    try:
        reader = MFRC522()
        log.info("RC522 RFID reader ready.")
        mqtt_publish("door/sensors", {"sensor": "rfid", "status": "online"})
    except Exception as exc:
        log.error("RFID init failed: %s", exc)
        mqtt_publish("door/sensors", {"sensor": "rfid", "status": "offline", "error": str(exc)})
        return

    while not stop_event.is_set():
        with sensor_enabled_lock:
            rfid_on = sensor_enabled["rfid"]
        if not rfid_on:
            time.sleep(0.2)
            continue

        try:
            # Look for tags in the field
            status, _ = reader.MFRC522_Request(reader.PICC_REQIDL)
            
            if status == reader.MI_OK:
                # Select tag and extract clean UID
                status, uid_bytes = reader.MFRC522_SelectTagSN()
                
                if status == reader.MI_OK and uid_bytes:
                    uid        = int.from_bytes(uid_bytes, byteorder="big")
                    authorized = uid in AUTHORIZED_RFID_IDS
                    log.info("RFID uid=0x%X authorized=%s", uid, authorized)
                    event_queue.put(("RFID", uid, authorized))

                    # Tell the tag to sleep so it doesn't block subsequent reads
                    reader.MFRC522_ToCard(reader.PCD_TRANSCEIVE, [reader.PICC_HALT, 0])
                    reader.MFRC522_StopCrypto1()
                    
                    # Force fully cleaning/re-initializing driver SPI registers
                    reader.MFRC522_Init()
                    
                    time.sleep(2.5)   # Debounce window while door unlocks
                    continue
            else:
                # CRITICAL: If the reader returns an error state or no card is present,
                # forcefully call Init to ensure hardware internal states don't freeze up.
                reader.MFRC522_Init()
                
            time.sleep(0.1)
        except Exception as exc:
            log.warning("RFID error: %s", exc)
            mqtt_publish("door/sensors", {"sensor": "rfid", "status": "error", "error": str(exc)})
            try:
                reader.MFRC522_Init()
            except Exception:
                pass
            time.sleep(0.5)

# ══════════════════════════════════════════════════════════════════════════════
# FINGERPRINT THREAD
# ══════════════════════════════════════════════════════════════════════════════

def fingerprint_thread_fn():
    sensor = None
    for port in ("/dev/ttyUSB0", "/dev/ttyS0", "/dev/ttyAMA0"): # our device uses /dev/ttyAMA0
        try:
            sensor = PyFingerprint(port, 57600, 0xFFFFFFFF, 0x00000000)
            if not sensor.verifyPassword():
                raise ValueError("Wrong sensor password.")
            log.info("R307S fingerprint sensor ready on %s.", port)
            mqtt_publish("door/sensors", {"sensor": "fingerprint", "status": "online", "port": port})
            break
        except Exception:
            sensor = None

    if sensor is None:
        log.error("Fingerprint sensor not found on any serial port.")
        mqtt_publish("door/sensors", {"sensor": "fingerprint", "status": "offline"})
        return

    while not stop_event.is_set():
        with sensor_enabled_lock:
            finger_on = sensor_enabled["fingerprint"]
        if not finger_on:
            time.sleep(0.2)
            continue

        try:
            if sensor.readImage():
                sensor.convertImage(0x01)
                result         = sensor.searchTemplate()
                template_id    = result[0]
                accuracy_score = result[1]

                if template_id >= 0:
                    authorized = template_id in AUTHORIZED_FINGERPRINT_IDS
                    log.info(
                        "Fingerprint match: id=%d score=%d authorized=%s",
                        template_id, accuracy_score, authorized,
                    )
                    event_queue.put(("FINGER", template_id, authorized))
                else:
                    log.info("Fingerprint: no match.")
                    event_queue.put(("FINGER", -1, False))

                time.sleep(1.5)   # debounce
            else:
                time.sleep(0.1)
        except Exception as exc:
            log.warning("Fingerprint error: %s", exc)
            mqtt_publish("door/sensors", {"sensor": "fingerprint", "status": "error", "error": str(exc)})
            time.sleep(0.5)


# ══════════════════════════════════════════════════════════════════════════════
# FACE DETECTION BRIDGE
# ══════════════════════════════════════════════════════════════════════════════

def _face_watcher_fn(face_event: threading.Event):
    """
    Lightweight thread that watches the FaceDetector's threading.Event
    and forwards authorised face detections into the shared event_queue.
    The FaceDetector itself runs its own internal thread; this thread
    only bridges the event → queue gap.
    """
    log.info("Face watcher ready.")
    mqtt_publish("door/sensors", {"sensor": "face", "status": "online"})
    while not stop_event.is_set():
        with sensor_enabled_lock:
            face_on = sensor_enabled["face"]
        if not face_on:
            time.sleep(0.2)
            continue

        triggered = face_event.wait(timeout=0.2)
        if triggered and not stop_event.is_set():
            log.info("Face authorised — posting to event queue.")
            event_queue.put(("FACE", "recognised", True))
            # Don't clear the event here — FaceDetector manages its own hold window.
            # Wait a few seconds before re-checking to avoid flooding the queue.
            time.sleep(4.0)
    log.info("Face watcher stopped.")


# ══════════════════════════════════════════════════════════════════════════════
# EVENT HANDLER THREAD  (sole owner of GPIO)
# ══════════════════════════════════════════════════════════════════════════════

def event_handler_thread_fn():
    log.info("Event handler ready.")

    source_labels = {
        "RFID":   "Card",
        "FINGER": "Finger",
        "FACE":   "Face",
    }

    while not stop_event.is_set():
        try:
            source, uid, authorized = event_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        # ── MQTT timed-unlock command ─────────────────────────────────────────
        if source == "MQTT_UNLOCK":
            seconds = float(uid)   # uid field carries the duration here
            log.info("MQTT unlock command — firing relays for %.1fs.", seconds)
            lcd_show("MQTT UNLOCK", f"{seconds:.0f}s", duration=seconds)
            _beep(1, on_ms=200)
            _dual_relay_pulse(sec_relay1=seconds, sec_relay2=seconds)
            continue

        # ── MQTT force-lock command ───────────────────────────────────────────
        if source == "MQTT_LOCK":
            _force_lock()
            lcd_show("FORCE LOCKED", "MQTT Command", duration=2.0)
            _beep(2, on_ms=100, off_ms=80)
            continue

        # ── Normal biometric auth event ───────────────────────────────────────
        label = source_labels.get(source, source)

        if authorized:
            log.info("%s ACCESS GRANTED (id=%s)", label, uid)
            lcd_show("ACCESS GRANTED", label, duration=3.0)
            mqtt_publish("door/auth", {
                "source":     source,
                "id":         str(uid),
                "result":     "granted",
                "timestamp":  time.time(),
            })
            _beep(1, on_ms=200)
            _dual_relay_pulse(sec_relay1=2.0, sec_relay2=2.0)
        else:
            if uid == -1:
                log.info("Fingerprint: unknown.")
                lcd_show("Unknown Finger", "Try again", duration=2.0)
            else:
                log.info("%s ACCESS DENIED (id=%s)", label, uid)
                lcd_show("ACCESS DENIED", label, duration=2.0)
            mqtt_publish("door/auth", {
                "source":    source,
                "id":        str(uid),
                "result":    "denied",
                "timestamp": time.time(),
            })
            _beep(3, on_ms=100, off_ms=80)


# ══════════════════════════════════════════════════════════════════════════════
# GRACEFUL SHUTDOWN
# ══════════════════════════════════════════════════════════════════════════════

def shutdown(signum, frame):
    log.info("Shutdown signal (%s) — stopping…", signum)
    stop_event.set()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    global AUTHORIZED_RFID_IDS, AUTHORIZED_FINGERPRINT_IDS

    # ── Load MQTT credentials from file before anything else ──────────────────
    mqtt_cfg = load_mqtt_config(MQTT_CONF_PATH)

    setup_gpio()
    AUTHORIZED_RFID_IDS, AUTHORIZED_FINGERPRINT_IDS = load_authorized_ids()

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── MQTT ──────────────────────────────────────────────────────────────────
    setup_mqtt(mqtt_cfg)

    # ── Face detector ─────────────────────────────────────────────────────────
    face_event   = threading.Event()
    face_detector = FaceDetector(
        face_detected_event=face_event,
        show_preview=True,       # set False for headless / no monitor
    )
    face_detector.start()
    mqtt_publish("door/sensors", {"sensor": "face", "status": "online"})

    # ── Background threads ────────────────────────────────────────────────────
    threads = [
        threading.Thread(target=lcd_thread_fn,           name="LCD",          daemon=True),
        threading.Thread(target=rfid_thread_fn,           name="RFID",         daemon=True),
        threading.Thread(target=fingerprint_thread_fn,   name="Fingerprint",  daemon=True),
        threading.Thread(target=event_handler_thread_fn, name="EventHandler", daemon=True),
        threading.Thread(target=_face_watcher_fn,        name="FaceWatcher",
                         args=(face_event,),              daemon=True),
    ]

    for t in threads:
        t.start()
        log.info("Started: %s", t.name)

    mqtt_publish("door/system", {"event": "startup", "threads": [t.name for t in threads]})
    lcd_show("Door System", "Ready", duration=2.0)

    # ── Main thread: GUI loop (imshow MUST run on main thread) ────────────────
    log.info("Main loop running — press Q in camera window to quit.")
    try:
        while not stop_event.is_set():
            key = face_detector.process_gui()   # pumps cv2 event loop
            if key == ord("q"):
                log.info("Q pressed — shutting down.")
                stop_event.set()
                break
            time.sleep(0.01)
    finally:
        stop_event.set()
        mqtt_publish("door/system", {"event": "shutdown"})
        face_detector.stop()
        log.info("Waiting for threads…")
        for t in threads:
            t.join(timeout=3.0)
        teardown_mqtt()
        cleanup_gpio()
        log.info("Shutting down....")


if __name__ == "__main__":
    main()
