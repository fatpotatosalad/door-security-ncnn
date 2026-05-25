
# IoT Edge Door Lock Controller

An integrated, edge-computing multi-modal biometric security system built for the Raspberry Pi. This system leverages state-of-the-art computer vision models (`YOLO12s NCNN` + `ArcFace ONNX`) for facial authentication, combined with secondary hardware sensors (`R307S` Fingerprint and `RC522` RFID reader). All components are tightly coupled to an asynchronous backend communicating over HiveMQ Cloud MQTT.

## Key Features
- **Face Authentication:** Face detection via an optimized `YOLO12s NCNN` model combined with `ArcFace ONNX` feature embeddings for extreme reliability on local edge hardware.
- **Biometric Sensors:** Multi-factor authentication via R307S Fingerprint scanner (UART) and RC522 RFID reader (SPI).
- **Asynchronous Event Handler:** Zero-race-condition thread layout funneling all authentication inputs into a thread-safe synchronized GPIO control queue.
- **Centralized Control Ecosystem:** Remote management over MQTT (Subscribe to lock/unlock triggers, publish telemetry/health statuses).
- **Physical Feedback:** High-contrast I2C LCD (16x2) display status updates and a synchronized buzzer warning circuit.

---

###  Project Structure
```text
├── main.py              # Central integrated daemon coordinating hardware threads & MQTT
├── face.py              # Computer vision thread (YOLO12s NCNN + ArcFace ONNX embedder)
├── reg.py               # Biometric User Registry configuration utility (Face, RFID, Fingerprint)
├── MFRC522.py           # Under-the-hood low-level driver interface for RC522 RFID reader
├── users.json           # Local relational database storing validated user credential mappings
├── mqtt.conf            # High-security network configuration bindings for HiveMQ Broker
├── requirements.txt     # Global virtual environment dependency package layout
└── pyvenv.cfg           # Venv runtime anchor configuration
```
### Hardware Requirements & Wiring

|Component|Protocol  | Default
|----|--|--
|GPIO MappingWebcam    |USB  |/dev/video0
|R307S Fingerprint Module|UART|Serial0 (/dev/ttyS0 or /dev/ttyAMA0)
|MFRC522 RFID Reader|SPI|SPI0 (MISO=GPIO9, MOSI=GPIO10, SCLK=GPIO11, CE0=GPIO8)|
|I2C LCD Display (16x2)|I2C|I2C1 (SDA=GPIO2, SCL=GPIO3, Address=0x27)
|BuzzerGPIO Out|GPIO |26
|Relay 1 (Primary Lock)GPIO Out|GPIO |6
|Relay 2 (Secondary Lock)GPIO Out|GPIO |12|

## Machine Learning Models & Conversion Tools
The system separates localization and feature analysis tasks onto dedicated edge-optimized frameworks to guarantee real-time operational thresholds on Raspberry Pi CPUs.
1. Face Embedding EngineModel: ArcFace (MobileFaceNet backbone)
Weights: w600k_mbf.onnx
Download link: https://huggingface.co/WePrompt/buffalo_sc/resolve/229b65be0dc7f8f33b55478772e8d3289580c356/w600k_mbf.onnx?download=true
Target Destination: models/w600k_mbf.onnx

2. Face Detection Engine (YOLO12)Model: 
Target Framework: Ultralytics YOLO12s
Format: NCNN (Optimized compiled .param and .bin pairings).
Target Destination: yolo12s_ncnn_model/

#### Model Exporting Guide (PyTorch .pt to NCNN)
To generate your own custom compiled edge target binaries (yolo12s_ncnn_model), use the official conversion tools provided natively via the Ultralytics engine layout.
#### Conversion Script
Create an isolation file called convert_ncnn.py inside your project environment:
##### Python
```
from ultralytics import YOLO
def export_yolo12_to_ncnn():
    # 1. Load the official or your custom fine-tuned PyTorch YOLO12s model weight file
    model = YOLO("yolo12s.pt")
    # 2. Compile model down to Tencent NCNN framework binaries
    # This automatically compiles fixed-name param and bin files optimized for edge platforms
    model.export(format="ncnn", imgsz=640)
    print("Success: YOLO12s converted to NCNN layout successfully!")

if __name__ == "__main__":
    export_yolo12_to_ncnn()
```

Run the conversion command in your terminal:

```python convert_ncnn.py```

Note: This generates an export directory named yolo12s_ncnn_model/ containing model.ncnn.param, model.ncnn.bin, and a configuration layer.

#### Installation & Environment Setup

1. Clone and Navigate into the Project Repository:
```git clone https://github.com/fatpotatosalad/door-security-ncnn```

2. Initialize Environment Dependencies:E
Ensure your Python virtual environment points correctly to your target distribution and install core libraries:
```
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

3. Verify MQTT Infrastructure Connectivity:
Ensure mqtt.conf points towards your authorized broker credentials:
```
host     = d77552e1720b49d3a4bdea760d0a27a8.s1.eu.hivemq.cloud
port     = 8883
username = raspi
password = [YOUR_PASSWORD_HERE]
```

#### Execution Commands
##### Step 1: Register Authorized System Users
Run the interactive console enrollment menu to populate biometric signatures (users.json) and snap target training images:
```
python reg.py
```
Registration Options include: 
1 (Register user metrics via Face camera, RFID serial swipe, or Fingerprint tracking index), 
2 (Enumerate current database records)
3 (Purge individual user records).

##### Step 2: Fire up Main System Control Matrix
Execute the central event handler daemon on your system:
```
python main.py
```
#### Remote MQTT Control Command Injections
You can bypass terminal configurations manually by sending standard encrypted topic payloads to the broker endpoint:

1. Trigger Remote Unlock: Publish an integer string payload specifying seconds duration to door/command/unlock (e.g., 5).
2. Trigger Emergency Lock Down: Force immediately safe status by sending an empty string to door/command/lock.
