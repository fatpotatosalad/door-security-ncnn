import sys
import time

def clear_fingerprint_sensor():
    try:
        from pyfingerprint.pyfingerprint import PyFingerprint
    except ImportError:
        print("✘ Missing package. Run: pip install pyfingerprint")
        return

    # Attempt connection on common serial ports
    sensor = None
    for port in ("/dev/ttyUSB0", "/dev/ttyS0", "/dev/ttyAMA0"):
        try:
            print(f"Connecting to sensor on {port}...")
            sensor = PyFingerprint(port, 57600, 0xFFFFFFFF, 0x00000000)
            if sensor.verifyPassword():
                print(f"✔ Successfully connected on {port}")
                break
        except Exception:
            continue

    if sensor is None:
        print("✘ Fingerprint sensor not found on any serial port.")
        return

    # Fetch initial count
    try:
        template_count = sensor.getTemplateCount()
        print(f"ℹ Current templates stored on sensor: {template_count}")
        
        if template_count == 0:
            print("✔ Sensor database is already empty. Nothing to delete.")
            return

        # Double check confirmation via console input
        confirm = input("⚠ WARNING: Are you sure you want to delete ALL fingerprints? [yes/N]: ").strip().lower()
        if confirm != "yes":
            print("Cancelled.")
            return

        print("Clearing flash memory store...")
        if sensor.clearDatabase():
            print("✔ SUCCESS: All templates have been deleted from the sensor.")
        else:
            print("✘ FAIL: Sensor failed to clear its memory template bank.")

    except Exception as e:
        print(f"✘ An error occurred during the operations: {e}")

if __name__ == "__main__":
    clear_fingerprint_sensor()
