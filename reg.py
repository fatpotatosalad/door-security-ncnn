"""
reg.py — Biometric Registration Tool
======================================
Registers users into users.json (shared with main.py).
Also enrolls face images into face_database/<name>/ (used by face.py).

Supported registration modes:
  1. Fingerprint — enrols template into R307S sensor flash memory
  2. RFID Card   — reads UID from RC522
  3. Face        — captures images from webcam into face_database/

Run:  python reg.py
"""

import json
import os
import sys
import time
import datetime
from pathlib import Path

# ── Database paths ─────────────────────────────────────────────────────────────
DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
FACE_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "face_database"

# ── ANSI colours ───────────────────────────────────────────────────────────────
R    = "\033[91m"
G    = "\033[92m"
Y    = "\033[93m"
B    = "\033[94m"
C    = "\033[96m"
W    = "\033[97m"
DIM  = "\033[2m"
RST  = "\033[0m"
BOLD = "\033[1m"

def _c(colour, text): return f"{colour}{text}{RST}"
def ok(msg):   print(_c(G,  f"  ✔  {msg}"))
def err(msg):  print(_c(R,  f"  ✘  {msg}"))
def info(msg): print(_c(C,  f"  ℹ  {msg}"))
def warn(msg): print(_c(Y,  f"  ⚠  {msg}"))

def banner():
    print(_c(B, """
╔══════════════════════════════════════════╗
║      BIOMETRIC REGISTRATION TOOL         ║
║      IOT Access Control System           ║
╚══════════════════════════════════════════╝"""))

def divider(): print(_c(DIM, "  " + "─" * 42))


# ── LCD Initialization (RPLCD) ────────────────────────────────────────────────
lcd = None
try:
    import RPi.GPIO as GPIO
    from RPLCD.i2c import CharLCD
    # Adjust pin configurations here to match your specific hardware setup
    lcd = CharLCD(
    i2c_expander='PCF8574',
    address=0x27,
    port=1,
    cols=16,
    rows=2,
    charmap='A00')
except ImportError:
    warn("RPLCD or RPi.GPIO module missing. Skipping physical LCD initialization.")
except Exception as e:
    warn(f"Could not initialize LCD display: {e}")

def lcd_print(line1: str, line2: str = ""):
    """Safely updates the physical LCD lines with clean padding."""
    if lcd is not None:
        try:
            lcd.clear()
            lcd.cursor_pos = (0, 0)
            lcd.write_string(line1[:16])
            if line2:
                lcd.cursor_pos = (1, 0)
                lcd.write_string(line2[:16])
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE  — users.json
# ══════════════════════════════════════════════════════════════════════════════

def load_db() -> dict:
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r") as f:
            return json.load(f)
    return {"users": []}


def save_db(db: dict):
    with open(DB_PATH, "w") as f:
        json.dump(db, f, indent=2)


def find_user(db: dict, user_id: str) -> dict | None:
    for u in db["users"]:
        if u["id"] == user_id:
            return u
    return None


def get_or_create_user(db: dict, user_id: str, name: str, role: str) -> dict:
    user = find_user(db, user_id)
    if user is None:
        user = {
            "id":         user_id,
            "name":       name,
            "role":       role,
            "registered": datetime.datetime.now().isoformat(timespec="seconds"),
            "methods":    {},
        }
        db["users"].append(user)
        ok(f"New user created: {name}  (ID: {user_id}, Role: {role})")
    else:
        ok(f"Adding method to existing user: {user['name']}")
    return user


# ══════════════════════════════════════════════════════════════════════════════
# INPUT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def prompt(label: str, allow_empty=False) -> str:
    while True:
        val = input(f"  {_c(W, label)}: ").strip()
        if val or allow_empty:
            return val
        warn("This field cannot be empty.")


def prompt_id(db: dict) -> tuple[str, str, str]:
    print()
    user_id  = prompt("User ID (e.g. 001)")
    existing = find_user(db, user_id)
    if existing:
        info(f"Found existing user: {existing['name']} / {existing['role']}")
        info("A new biometric method will be added to this user.")
        return user_id, existing["name"], existing["role"]
    name = prompt("Full name")
    role = prompt("Role  (admin / staff / guest)")
    return user_id, name, role


# ══════════════════════════════════════════════════════════════════════════════
# MODE 1 — FINGERPRINT
# ══════════════════════════════════════════════════════════════════════════════

def _open_fingerprint_sensor():
    try:
        from pyfingerprint.pyfingerprint import PyFingerprint
    except ImportError:
        err("Missing package. Run:  pip install pyfingerprint")
        return None

    for port in ("/dev/ttyUSB0", "/dev/ttyS0", "/dev/ttyAMA0"):
        try:
            sensor = PyFingerprint(port, 57600, 0xFFFFFFFF, 0x00000000)
            if sensor.verifyPassword():
                ok(f"Fingerprint sensor on {port}.")
                return sensor
        except Exception:
            pass
    err("Fingerprint sensor not found on any serial port.")
    return None


def _next_free_template_id(sensor) -> int:
    count = sensor.getTemplateCount()
    if count == 0:
        return 1
    templates = sensor.getTemplateIndex(0)
    for idx, used in enumerate(templates):
        if not used:
            return idx
    return count


def register_fingerprint(user: dict) -> bool:
    sensor = _open_fingerprint_sensor()
    if sensor is None:
        lcd_print("Sensor Error", "Check Connection")
        return False

    template_id = _next_free_template_id(sensor)
    info(f"Will save to template slot #{template_id}.")

    lcd_print("Enroll Finger", "Place Finger...")
    info("Place finger on sensor…")
    while not sensor.readImage():
        time.sleep(0.1)
    sensor.convertImage(0x01)
    ok("First image captured. Remove finger.")
    lcd_print("Remove Finger", "Wait...")
    time.sleep(1.5)

    while sensor.readImage():
        time.sleep(0.1)

    lcd_print("Enroll Finger", "Place Same Again")
    info("Place the SAME finger again…")
    while not sensor.readImage():
        time.sleep(0.1)
    sensor.convertImage(0x02)
    ok("Second image captured.")

    if sensor.compareCharacteristics() == 0:
        err("Fingerprints do not match. Please try again.")
        lcd_print("Match Failed", "Try Again")
        return False

    sensor.createTemplate()
    sensor.storeTemplate(template_id, 0x01)
    ok(f"Fingerprint enrolled in slot #{template_id}.")
    lcd_print("Finger Enrolled", f"Slot #{template_id}")

    user.setdefault("methods", {})["fingerprint"] = {"template_id": template_id}
    return True


# ══════════════════════════════════════════════════════════════════════════════
# MODE 2 — RFID CARD
# ══════════════════════════════════════════════════════════════════════════════

def register_rfid(user: dict) -> bool:
    try:
        from MFRC522 import MFRC522
    except ImportError:
        err("MFRC522.py not found. Place it beside reg.py.")
        return False

    info("Initialising RC522…")
    try:
        reader = MFRC522()
    except Exception as exc:
        err(f"RC522 init failed: {exc}")
        lcd_print("RFID Init Fail", "Check Reader")
        return False

    lcd_print("Scan RFID Card", "Hold Near Reader")
    info("Hold card/fob near the reader…")
    uid      = None
    deadline = time.time() + 15.0

    while time.time() < deadline:
        status, _ = reader.MFRC522_Request(reader.PICC_REQIDL)
        if status == reader.MI_OK:
            status, uid_bytes = reader.MFRC522_SelectTagSN()
            if status == reader.MI_OK and uid_bytes:
                uid = int.from_bytes(uid_bytes, byteorder="big")
                break
        time.sleep(0.1)

    if uid is None:
        err("No card detected within 15 seconds.")
        lcd_print("Scan Timeout", "No Card Detected")
        return False

    ok(f"Card UID: 0x{uid:X}  ({uid})")
    lcd_print("Card Scanned", f"UID: {uid}")

    db = load_db()
    for u in db["users"]:
        rfid_method = u.get("methods", {}).get("rfid")
        if rfid_method and rfid_method.get("uid") == uid and u["id"] != user["id"]:
            warn(f"This card is already registered to: {u['name']} (ID: {u['id']})")

    user.setdefault("methods", {})["rfid"] = {"uid": uid}
    return True


# ══════════════════════════════════════════════════════════════════════════════
# MODE 3 — FACE ENROLLMENT
# ══════════════════════════════════════════════════════════════════════════════

def register_face(user: dict) -> bool:
    """
    Opens the webcam, captures CAPTURE_COUNT good frames of the person's
    face, and saves them as JPEGs into face_database/<name>/.
    face.py's build_registry() picks these up on next start.

    Uses the same yolo12s ncnn detector as face.py for consistency —
    enrollment images are guaranteed to contain a detectable face.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        err("opencv-python not installed.")
        return False

    CAPTURE_COUNT  = 25
    CAMERA_INDEX   = 0
    NCNN_MODEL_DIR = "/home/x0rg/Desktop/IOT/yolo12s_ncnn_model"
    DET_CONF       = 0.45
    SHARP_THRESH   = 40.0    # Laplacian variance — reject blurry frames

    # Load the same ncnn detector
    detector = None
    try:
        from ultralytics import YOLO
        detector = YOLO(NCNN_MODEL_DIR)
        info("yolo12s ncnn detector loaded.")
    except Exception as exc:
        warn(f"ncnn detector unavailable ({exc}) — using Haar cascade fallback.")

    # Haar fallback
    cascade = None
    if detector is None:
        haar_paths = [
            "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
            "/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        ]
        for p in haar_paths:
            if os.path.exists(p):
                cascade = cv2.CascadeClassifier(p)
                info(f"Haar fallback loaded from {p}")
                break
        if cascade is None or cascade.empty():
            err("No face detector available — cannot enroll face.")
            lcd_print("Detector Error", "No Model Found")
            return False

    # Output directory — use sanitised name so it matches identify() lookup
    safe_name  = user["name"].upper().replace(" ", "_")
    person_dir = FACE_DIR / safe_name
    person_dir.mkdir(parents=True, exist_ok=True)
    info(f"Saving face images to {person_dir}")

    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        err("Cannot open camera.")
        lcd_print("Camera Error", "Cannot Open Cam")
        return False

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _laplacian(img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def _detect_faces(frame):
        """Returns list of (x1,y1,x2,y2) for detected faces."""
        if detector is not None:
            results = detector(frame, conf=DET_CONF, verbose=False, device="cpu")
            boxes   = []
            for r in results:
                for box in r.boxes:
                    x1,y1,x2,y2 = map(int, box.xyxy[0].tolist())
                    boxes.append((x1,y1,x2,y2))
            return boxes
        else:
            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(60,60))
            return [(x,y,x+w,y+h) for (x,y,w,h) in faces]

    info("Look at the camera. Getting ready…")

    # 3-second countdown
    deadline = time.time() + 3
    while time.time() < deadline:
        ret, frame = cap.read()
        if not ret:
            continue
        remaining = int(deadline - time.time()) + 1
        lcd_print("Face Enrollment", f"Get Ready: {remaining}")
        cv2.putText(frame, f"Get ready: {remaining}", (20, 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 200, 255), 2)
        cv2.imshow("Face Enrollment", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            cap.release()
            cv2.destroyWindow("Face Enrollment")
            info("Aborted.")
            lcd_print("Enroll Aborted")
            return False

    saved = 0
    info(f"Capturing {CAPTURE_COUNT} frames…")

    # Start index after any existing images for this person
    existing = sorted(person_dir.glob("*.jpg"))
    img_idx  = len(existing)

    while saved < CAPTURE_COUNT:
        ret, frame = cap.read()
        if not ret:
            continue

        faces = _detect_faces(frame)
        if not faces:
            status = _c(Y, f"No face  ({saved}/{CAPTURE_COUNT})")
            lcd_print("Face Enrollment", f"No Face: {saved}/{CAPTURE_COUNT}")
        else:
            # Take largest face box
            x1,y1,x2,y2 = max(faces, key=lambda b: (b[2]-b[0])*(b[3]-b[1]))
            crop         = frame[y1:y2, x1:x2]

            if crop.size == 0 or _laplacian(crop) < SHARP_THRESH:
                status = _c(Y, f"Blurry — hold still  ({saved}/{CAPTURE_COUNT})")
                lcd_print("Face Enrollment", f"Blurry! Hold Still")
            else:
                fname = person_dir / f"{img_idx:04d}.jpg"
                cv2.imwrite(str(fname), crop)
                saved   += 1
                img_idx += 1
                status   = _c(G, f"Captured {saved}/{CAPTURE_COUNT}")
                lcd_print("Face Enrollment", f"Saved: {saved}/{CAPTURE_COUNT}")
                cv2.rectangle(frame, (x1,y1), (x2,y2), (0,220,0), 2)

        cv2.putText(frame, status, (10, 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255,255,255), 2)
        cv2.imshow("Face Enrollment", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            info("Aborted early.")
            lcd_print("Enroll Aborted", f"Saved {saved} frames")
            break

    cap.release()
    cv2.destroyWindow("Face Enrollment")

    if saved == 0:
        err("No images captured — face not enrolled.")
        lcd_print("Enroll Failed", "No Frames Saved")
        return False

    ok(f"Saved {saved} face image(s) for {user['name']} in {person_dir}")

    # Mark face method in users.json
    user.setdefault("methods", {})["face"] = {
        "directory": str(person_dir),
        "images":    saved,
    }
    return True


# ══════════════════════════════════════════════════════════════════════════════
# LIST USERS
# ══════════════════════════════════════════════════════════════════════════════

def list_users():
    db    = load_db()
    users = db.get("users", [])
    print()
    if not users:
        warn("No users registered yet.")
        return
    print(_c(BOLD, f"  {'ID':<6} {'Name':<20} {'Role':<12} {'Methods':<30} {'Registered'}"))
    divider()
    for u in users:
        methods = ", ".join(u.get("methods", {}).keys()) or "—"
        reg     = u.get("registered", "?")[:10]
        print(f"  {u['id']:<6} {u['name']:<20} {u.get('role',''):<12} {methods:<30} {reg}")
    divider()
    print()


# ══════════════════════════════════════════════════════════════════════════════
# DELETE USER
# ══════════════════════════════════════════════════════════════════════════════

def delete_user_menu():
    import shutil
    db = load_db()
    list_users()
    user_id = prompt("Enter User ID to delete (or ENTER to cancel)", allow_empty=True)
    if not user_id:
        return

    user = find_user(db, user_id)
    if user is None:
        err(f"No user with ID: {user_id}")
        return

    confirm = input(f"  {_c(R, 'Delete ' + user['name'] + ' and ALL biometrics? [yes/N]: ')}").strip().lower()
    if confirm != "yes":
        info("Cancelled.")
        return

    # Remove face_database folder if it exists
    face_method = user.get("methods", {}).get("face", {})
    face_dir    = face_method.get("directory")
    if face_dir and os.path.isdir(face_dir):
        shutil.rmtree(face_dir)
        ok(f"Removed face images from {face_dir}")

    db["users"] = [u for u in db["users"] if u["id"] != user_id]
    save_db(db)
    ok(f"User {user['name']} deleted.")
    lcd_print("User Deleted", user['name'])


# ══════════════════════════════════════════════════════════════════════════════
# REGISTRATION MENU
# ══════════════════════════════════════════════════════════════════════════════

def registration_menu():
    db = load_db()
    print()
    divider()
    print(_c(BOLD, "  SELECT REGISTRATION TYPE"))
    divider()
    print(f"  {_c(Y,'1')}  Fingerprint        (R307S sensor)")
    print(f"  {_c(Y,'2')}  RFID card/fob      (RC522 reader)")
    print(f"  {_c(Y,'3')}  Face               (webcam)")
    print(f"  {_c(DIM,'0')}  Cancel")
    divider()

    choice = input(f"  {_c(W,'Choice')}: ").strip()
    if choice not in ("1", "2", "3"):
        info("Cancelled.")
        return

    mode_names = {"1": "Fingerprint", "2": "RFID Card", "3": "Face"}
    info(f"Registering: {mode_names[choice]}")
    lcd_print("Registering User", mode_names[choice])

    user_id, name, role = prompt_id(db)
    user = get_or_create_user(db, user_id, name, role)

    print()
    success = False
    if choice == "1":
        success = register_fingerprint(user)
    elif choice == "2":
        success = register_rfid(user)
    elif choice == "3":
        success = register_face(user)

    if success:
        save_db(db)
        print()
        ok(f"Registration complete! Saved to {DB_PATH}")
        lcd_print("Reg Complete!", user['name'])
        divider()
        print(f"  {_c(DIM,'Name    :  ')}{user['name']}")
        print(f"  {_c(DIM,'ID      :  ')}{user['id']}")
        print(f"  {_c(DIM,'Role    :  ')}{user.get('role','')}")
        print(f"  {_c(DIM,'Methods :  ')}{', '.join(user['methods'].keys())}")
        divider()
    else:
        err("Registration failed — no data saved for this method.")
        lcd_print("Reg Failed", "No Data Saved")
        if not user.get("methods"):
            db["users"] = [u for u in db["users"] if u["id"] != user["id"]]
        save_db(db)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN MENU
# ══════════════════════════════════════════════════════════════════════════════

def main():
    banner()
    lcd_print("Biometric System", "Ready...")
    while True:
        print()
        print(_c(BOLD, "  MAIN MENU"))
        divider()
        print(f"  {_c(Y,'1')}  Register a user")
        print(f"  {_c(Y,'2')}  List all users")
        print(f"  {_c(Y,'3')}  Delete a user")
        print(f"  {_c(Y,'0')}  Exit")
        divider()

        choice = input(f"  {_c(W,'Choice')}: ").strip()

        if choice == "1":
            registration_menu()
        elif choice == "2":
            list_users()
        elif choice == "3":
            delete_user_menu()
        elif choice == "0":
            info("Goodbye.")
            lcd_print("System Shutdown", "Goodbye!")
            time.sleep(1.0)
            if lcd is not None:
                lcd.clear()
            sys.exit(0)
        else:
            warn("Invalid choice.")


if __name__ == "__main__":
    main()
