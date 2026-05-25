"""
face.py — Face detection using yolo12s ncnn model
===================================================
Converted from InsightFace buffalo_s to ultralytics ncnn.
Registry system (face_database/, build_registry, identify) is unchanged.
Embeddings now come from onnxruntime ArcFace instead of InsightFace.

Model path: /home/x0rg/Desktop/IOT/yolo12s_ncnn_model
"""

import cv2
import time
import logging
import threading
import queue
import numpy as np
from pathlib import Path
from scipy.spatial.distance import cosine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CAMERA_INDEX       = 0
FRAME_WIDTH        = 640
FRAME_HEIGHT       = 480
FRAME_SKIP         = 5
DISTANCE_THRESHOLD = 0.3
UNLOCK_HOLD        = 5.0
DB_DIR             = Path("face_database")
SHOW_PREVIEW       = True
WINDOW_NAME        = "Door System"
NCNN_MODEL_DIR     = "/home/x0rg/Desktop/IOT/yolo12s_ncnn_model"
ARCFACE_MODEL      = "/home/x0rg/Desktop/IOT/models/w600k_mbf.onnx"
DET_CONF           = 0.50    # yolo12s detection confidence threshold
INPUT_SIZE         = (112, 112)
ARCFACE_MEAN       = 127.5
ARCFACE_STD        = 128.0


# ── ArcFace embedder (replaces InsightFace recognition module) ────────────────
class _Embedder:
    def __init__(self, model_path: str):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 3
        opts.inter_op_num_threads = 1
        opts.log_severity_level   = 3
        self._sess       = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_name = self._sess.get_inputs()[0].name
        log.info("ArcFace embedder loaded from %s", model_path)

    def get(self, face_bgr: np.ndarray) -> np.ndarray | None:
        """
        Return L2-normalised 512-d embedding from a BGR face crop.
        Returns None if the crop produces a non-finite (NaN/Inf) embedding.
        """
        resized = cv2.resize(face_bgr, INPUT_SIZE, interpolation=cv2.INTER_LINEAR)
        rgb     = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        blob    = (rgb.astype(np.float32) - ARCFACE_MEAN) / ARCFACE_STD
        blob    = np.transpose(blob, (2, 0, 1))[np.newaxis]
        out     = self._sess.run(None, {self._input_name: blob})[0][0]

        # Guard: reject non-finite raw outputs before normalisation
        if not np.isfinite(out).all():
            log.warning("ArcFace produced non-finite output — discarding embedding.")
            return None

        norm = np.linalg.norm(out)

        # Guard: reject near-zero norm to prevent divide instability
        if norm < 1e-6:
            log.warning("ArcFace embedding norm near zero (%.2e) — discarding.", norm)
            return None

        return out / norm


# ── Registry ──────────────────────────────────────────────────────────────────
def build_registry(embedder: _Embedder):
    """
    Reads face_database/<name>/*.jpg, embeds with ArcFace, averages per person.
    Drop-in replacement for the InsightFace version — same return shape.
    """
    vectors, names = [], []
    if not DB_DIR.exists():
        DB_DIR.mkdir(parents=True)
        log.warning("face_database/ is empty — run register.py first")
        return vectors, names

    for person_dir in sorted(DB_DIR.iterdir()):
        if not person_dir.is_dir():
            continue
        name        = person_dir.name.upper()
        person_vecs = []
        skipped     = 0

        for img_path in sorted(person_dir.glob("*.jpg")):
            img = cv2.imread(str(img_path))
            if img is None:
                log.warning("Could not read %s — skipping", img_path.name)
                skipped += 1
                continue
            if img.size == 0:
                log.warning("Empty image %s — skipping", img_path.name)
                skipped += 1
                continue

            vec = embedder.get(img)

            # Guard: embedder returns None for non-finite or degenerate embeddings
            if vec is None:
                log.warning("Bad embedding from %s — skipping", img_path.name)
                skipped += 1
                continue

            person_vecs.append(vec)
            log.info("Enrolled [%s] from %s", name, img_path.name)

        if skipped:
            log.warning("[%s] skipped %d image(s) due to bad embeddings.", name, skipped)

        if not person_vecs:
            log.warning("No valid images for %s — person not added to registry.", name)
            continue

        avg  = np.mean(person_vecs, axis=0)
        norm = np.linalg.norm(avg)

        # Guard: average vector should always be finite and non-zero
        if not np.isfinite(avg).all() or norm < 1e-6:
            log.error("[%s] averaged embedding is degenerate — skipping person.", name)
            continue

        avg = avg / norm
        vectors.append(avg)
        names.append(name)
        log.info("[%s] registered with %d image(s) (%d skipped).",
                 name, len(person_vecs), skipped)

    log.info("Registry ready — %d person(s)", len(names))
    return vectors, names


def identify(embedding, vectors, names):
    if not vectors:
        return "NO REGISTRY", 1.0
    distances = [cosine(embedding, v) for v in vectors]
    idx  = int(np.argmin(distances))
    dist = distances[idx]
    if dist <= DISTANCE_THRESHOLD:
        return names[idx], dist
    return "UNKNOWN", dist


# ── FaceDetector ──────────────────────────────────────────────────────────────
class FaceDetector:
    def __init__(self, face_detected_event: threading.Event, show_preview=SHOW_PREVIEW):
        self._event        = face_detected_event
        self._show_preview = show_preview
        self._stop         = threading.Event()
        self._thread       = None
        self._preview_q    = queue.Queue(maxsize=2)
        self._model        = None   # ultralytics YOLO ncnn
        self._embedder     = None   # ArcFace onnxruntime
        self._vectors      = []
        self._names        = []

    def start(self):
        log.info("Loading yolo12s ncnn model from %s", NCNN_MODEL_DIR)
        from ultralytics import YOLO
        self._model    = YOLO(NCNN_MODEL_DIR, task="detect")
        self._embedder = _Embedder(ARCFACE_MODEL)
        self._vectors, self._names = build_registry(self._embedder)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="FaceDetector")
        self._thread.start()
        log.info("FaceDetector started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            cv2.destroyWindow(WINDOW_NAME)
        except Exception:
            pass

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def process_gui(self):
        """Call from main thread every loop tick. Returns key pressed or 0."""
        if not self._show_preview:
            return 0
        try:
            frame = self._preview_q.get_nowait()
            cv2.imshow(WINDOW_NAME, frame)
        except queue.Empty:
            pass
        return cv2.waitKey(1) & 0xFF

    # ── Drawing ───────────────────────────────────────────────────────────────
    def _annotate(self, frame, results, hold_until):
        out      = frame.copy()
        unlocked = time.monotonic() < hold_until
        for (x1, y1, x2, y2, name, dist) in results:
            auth  = name not in ("UNKNOWN", "NO REGISTRY")
            color = (0, 220, 0) if auth else (0, 0, 220)
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
            cv2.putText(out, f"{name}  d={dist:.2f}", (x1, max(y1 - 8, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
        bar_color = (0, 130, 0) if unlocked else (30, 30, 30)
        bar_text  = "ACCESS GRANTED" if unlocked else "LOCKED | SCANNING"
        cv2.rectangle(out, (0, 0), (out.shape[1], 38), bar_color, -1)
        cv2.putText(out, bar_text, (12, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
        return out

    # ── Detection + embedding helper ──────────────────────────────────────────
    def _process_frame(self, frame):
        """
        Run yolo12s ncnn detection, then ArcFace embedding on each crop.
        Returns list of (x1, y1, x2, y2, name, dist).
        """
        results = self._model(
            frame,
            conf=DET_CONF,
            verbose=False,
            device="cpu",
        )
        output = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                # Clamp to frame bounds
                x1 = max(0, x1); y1 = max(0, y1)
                x2 = min(frame.shape[1], x2); y2 = min(frame.shape[0], y2)
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue

                emb = self._embedder.get(crop)

                # Guard: skip detections that yield degenerate embeddings
                if emb is None:
                    log.warning("Degenerate embedding from live crop — skipping box.")
                    continue

                name, dist = identify(emb, self._vectors, self._names)
                output.append((x1, y1, x2, y2, name, dist))
        return output

    # ── Main loop (daemon thread) ─────────────────────────────────────────────
    def _run(self):
        cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(CAMERA_INDEX)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        cached_results = []
        hold_until     = 0.0
        frame_count    = 0

        while not self._stop.is_set():
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.05)
                continue

            frame_count += 1
            now          = time.monotonic()

            if frame_count % FRAME_SKIP == 0:
                try:
                    cached_results = self._process_frame(frame)
                except Exception as exc:
                    log.error("Detection error: %s", exc)
                    cached_results = []

                auth_found = any(
                    name not in ("UNKNOWN", "NO REGISTRY")
                    for (_, _, _, _, name, _) in cached_results
                )

                if auth_found:
                    self._event.set()
                    hold_until = now + UNLOCK_HOLD
                elif now >= hold_until:
                    self._event.clear()
                    cached_results = []

            if self._show_preview:
                annotated = self._annotate(frame, cached_results, hold_until)
                if self._preview_q.full():
                    try:
                        self._preview_q.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self._preview_q.put_nowait(annotated)
                except queue.Full:
                    pass

        cap.release()
        log.info("FaceDetector stopped")


# ── Standalone run ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    detected = threading.Event()
    detector = FaceDetector(face_detected_event=detected)
    detector.start()

    print("Running — press Q to quit.")
    try:
        while True:
            key = detector.process_gui()
            if key == ord("q"):
                break
            if detected.wait(timeout=0.01):
                print(">>> FACE AUTHORIZED <<<")
                detected.clear()
    except KeyboardInterrupt:
        pass
    finally:
        detector.stop()
